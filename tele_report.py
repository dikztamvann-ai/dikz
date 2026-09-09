"""
tele_report.py — Report Akun / Grup / Channel untuk /tele

Alur:
  /tele → Report Acc → input target (bulk) → pilih sender (single / ALL / SEMUA)
        → pilih alasan → (opsional komentar) → jumlah report per sender (1-10)
        → proses → ringkasan berhasil / gagal.

Referensi API resmi (core.telegram.org, layer 223):
  account.reportPeer#c5ba3d86  peer:InputPeer reason:ReportReason message:string = Bool
  messages.report#fc78af9b     peer:InputPeer id:Vector<int> option:bytes message:string
                               = ReportResult
      → reportResultChooseOption (title, options:Vector<messageReportOption>)
      → reportResultAddComment  (flags.0?optional, option:bytes)
      → reportResultReported    (selesai)
  ReportReason: spam, violence, pornography, childAbuse, copyright,
                geoIrrelevant, fake (impersonasi), illegalDrugs,
                personalDetails, other
  Catatan docs: kedua method hanya bisa dipakai akun user (bukan bot),
  karena itu report dijalankan lewat sender pool (session Telethon).
"""
from __future__ import annotations

import asyncio
import html
import logging
import random
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)

# ── Injected dari tele_handler ───────────────────────────────
_em = None
_screen = None
_batal_kb = None
_edit_job = None
_connect_sender = None
_entity_info = None
_get_pool = None
_get_pool_all = None
_USER_ID = None
_JOBS = None
_ban_user = None      # callback(user_id, reason) → blacklist reporter dari bot

E1 = E2 = E3 = E6 = ""
CE_TELEGRAM = CE_AKUN = CE_LOADING = CE_LIVE = CE_HELP = CE_WAKTU = ""
CE_DETAIL_ID = CE_DETAIL_NAME = CE_DETAIL_MSG = CE_FILE = CE_NOMOR = ""
CE_HAPUS = CE_LINK = CE_MONITOR = CE_PROFILE = ""

REPORT_MAX_TARGETS = 20          # target per job
REPORT_COMMENT_MAX = 512         # docs: message:string (komentar moderasi)
REPORT_MAX_REPEAT = 10           # inline 1..10
REPORT_WORKERS = 12              # sender paralel
REPORT_DELAY = 2.0               # jeda antar report per sender
REPORT_DELAY_MAX = 45.0
REPORT_FLOOD_COOLDOWN = 90
REPORT_MAX_FLOOD = 4             # sender di-nonaktifkan kalau flood beruntun
REPORT_MSG_SCAN = 20             # pesan terakhir yang diambil buat messages.report
REPORT_OP_TIMEOUT = 60           # timeout 1 laporan (anti-stuck kalau server ngambang)
REPORT_CONNECT_TIMEOUT = 30      # timeout connect sender
REPORT_PROBE_TIMEOUT = 30        # timeout resolve+deteksi 1 target (preview)
REPORT_PROBE_WORKERS = 6         # target diprobe paralel
REPORT_MAX_CONN_FAIL = 3         # sender mati kalau gagal connect beruntun

# ── Tabel alasan (urut = tampilan inline) ───────────────────
# key, label UI, nama constructor InputReportReason*
REPORT_REASONS = [
    ("spam",      "Spam",                 "InputReportReasonSpam"),
    ("fake",      "Akun Palsu / Impersonasi", "InputReportReasonFake"),
    ("violence",  "Kekerasan",            "InputReportReasonViolence"),
    ("porno",     "Pornografi",           "InputReportReasonPornography"),
    ("child",     "Pelecehan Anak",       "InputReportReasonChildAbuse"),
    ("drugs",     "Narkoba",              "InputReportReasonIllegalDrugs"),
    ("copyright", "Hak Cipta",            "InputReportReasonCopyright"),
    ("personal",  "Data Pribadi",         "InputReportReasonPersonalDetails"),
    ("geo",       "Geo Tidak Relevan",    "InputReportReasonGeoIrrelevant"),
    ("other",     "Lainnya",              "InputReportReasonOther"),
]
_REASON_MAP = {k: (label, cls) for k, label, cls in REPORT_REASONS}

# ── EMAIL REPORT (Monzo) ──────────────────────────────────────
MONZO_REPORT_EMAILS = [
"phishing-reports@monzo.com",
"help@monzo.com",
"support@monzo.com",
"legal@monzo.com",
"fraud@monzo.com",
"ie.support@monzo.com",
"security@monzo.com",
"complaints@monzo.com",
"abuse@cloudflare.com",
"mailabuse@cloudflare.com",
"registrar-abuse@cloudflare.com",
"abuse@telegram.org",
"antiscam@telegram.org",
"dmca@telegram.org",
"scam@netcraft.com",
"report@netcraft.com",
"report-updates@netcraft.com",
"takedown@netcraft.com",
"report@phishing.gov.uk",
"phish@office365.microsoft.com",
"reportphishing@apple.com",
"reportfacetimefraud@apple.com",
"phishing@paypal.com",
"support@telegram.org",
"report@telegram.org",
"security@telegram.org",
"developers@telegram.org",
"support@stel.com",
"abuse@stel.com",
"reclaim@telegram.org",
"copyright@telegram.org",
"complaints@telegram.org",
"legal@telegram.org",
"ios@telegram.org",
"android@telegram.org",
"support@kucoin.com",
"web@telegram.org",
"api@telegram.org",
"feedback@telegram.org",
"spam@telegram.org",
"scam@telegram.org",
"moderator@telegram.org",
"admin@telegram.org",
"noreply@telegram.org",
"sms@telegram.org",
"support@group-ib.com",
"response@cert-gib.com",
]
MONZO_REPORT_WORKERS = 10
MONZO_REPORT_TARGET_SENT = 100  # harus 100 email terkirim (50 sender × 2 email)
MONZO_REPORT_SENDERS_PICK = 50  # ambil 50 sender per batch
MONZO_REPORT_MAX_RETRY = 3     # retry max 3x per email gagal
MONZO_REPORT_SUBJECT_1 = "EMERGENCY PHISHING REPORT - MONZO IMPERSONATION WITH FINANCIAL LOSS"
MONZO_REPORT_BODY_1 = """\
EMERGENCY PHISHING REPORT - MONZO IMPERSONATION WITH FINANCIAL LOSS

To Telegram Safety Team and Monzo Fraud Division,

This report details an active phishing campaign on Telegram where scammers are posing as Monzo representatives to steal funds through OTP interception.

SUSPECT ACCOUNT INFORMATION
Account Name: {name}
Username: @{username}
User ID: {user_id}
Profile URL: https://t.me/{username}

INCIDENT CHRONOLOGY
The fraudster initiated contact by pretending to be Monzo support staff. They created urgency by reporting unauthorized login activity. The victim was guided to a counterfeit website and asked to submit their mobile number. Subsequently, the scammer requested the 6-digit verification code sent via SMS. After the victim disclosed the code, funds were transferred out without authorization.

FRAUDULENT WEBSITE
monzo-secure-update-verification.com

VISUAL EVIDENCE
https://files.catbox.moe/j8bk53.jpg
https://files.catbox.moe/inmz6d.jpg
https://files.catbox.moe/rd63dc.jpg

REQUESTED MEASURES
Telegram is requested to place a SCAM warning label on the mentioned account. Monzo is requested to pursue domain takedown.

This matter requires immediate attention to prevent additional victims.
"""
MONZO_REPORT_SUBJECT_2 = "PHISHING TAKEDOWN REQUEST - MONZO BANK CREDENTIAL HARVESTING OPERATION"
MONZO_REPORT_BODY_2 = """\
To Telegram Safety Team and Monzo Fraud Division,

This report details an active phishing campaign on Telegram where scammers are posing as Monzo representatives to steal funds through OTP (One-Time Password) interception. This is a sophisticated social engineering attack that exploits users' trust in official banking communications.

---

SUSPECT ACCOUNT INFORMATION
Account Name: {name}
Username: @{username}
User ID: {user_id}
Profile URL: https://t.me/{username}
Account Type: Suspected fraudulent/scam account impersonating Monzo

---

INCIDENT CHRONOLOGY

The fraudster initiated contact by pretending to be Monzo support staff. They created a false sense of urgency by reporting unauthorized login activity on the victim's account. This tactic is designed to pressure victims into acting quickly without thinking critically.

The victim was guided to a counterfeit website designed to mimic Monzo's official login page. The victim was asked to submit their mobile number. Subsequently, the scammer requested the 6-digit verification code sent via SMS (OTP). After the victim disclosed the code, the scammer used it to bypass two-factor authentication and transfer funds out of the victim's account without authorization.

This method, known as OTP interception or SIM-swapping related fraud, is one of the most common and dangerous banking scams currently in circulation.

---

FRAUDULENT WEBSITES (KNOWN ASSOCIATED DOMAINS)

| No. | Domain | Description |
|-----|--------|-------------|
| 1 | monzo-secure-update-verification.com | Active phishing domain from the current campaign |
| 2 | monzo-uk-help.com | Previously identified in phishing campaigns (June 2023) |
| 3 | monzo-notice.com | Identified in "Cazanova Morphine" phishing kit campaign |
| 4 | monzo-online-support.com | Identified in "Cazanova Morphine" phishing kit campaign |
| 5 | monzo-check.com | Identified in previous phishing campaigns |

These domains are NOT affiliated with Monzo in any way. Monzo's official domains are strictly limited to @monzo.com, @email.monzo.com, @customercontent.monzo.com, and @research.monzo.com.

---

VISUAL EVIDENCE

Screenshots documenting the scam communication and fraudulent website:
https://files.catbox.moe/j8bk53.jpg
https://files.catbox.moe/inmz6d.jpg
https://files.catbox.moe/rd63dc.jpg

---

ADDITIONAL CONTEXT

1. Official Monzo Policy Violation: According to Monzo's official security guidance, Monzo NEVER requests OTP codes, login details, or personal information via phone calls, SMS, or third-party chat applications. The scammer's actions clearly violate these protocols.

2. Domain Analysis: The fraudulent domain "monzo-secure-update-verification.com" does not match Monzo's legitimate email domains. Any domain containing "monzo" combined with suspicious keywords like "secure," "update," "verification," "help," "notice," or "online-support" should be treated as highly suspicious.

3. Telegram's Role: Telegram is increasingly being used as a platform for financial scams. Scammers create accounts impersonating legitimate businesses to establish trust before moving victims to external phishing websites.

4. Financial Impact: This is not a minor incident. The OTP interception method used here has resulted in actual financial loss, making this a serious fraud case requiring immediate enforcement action.

5. Potential Scale: These campaigns often target multiple victims simultaneously. Takedown of the fraudulent domain(s) and account suspension are critical to prevent additional losses.

---

REQUESTED MEASURES

**Telegram Safety Team is requested to:**
1. Place a SCAM warning label on the mentioned account to alert other users.
2. Suspend/delete the fraudulent account to prevent further scams.
3. Investigate the account's activity for potential links to other scam accounts.
4. Consider blocking the reported domain from being shared within Telegram.

**Monzo Fraud Division is requested to:**
1. Pursue immediate domain takedown for all identified fraudulent domains.
2. Notify relevant hosting providers and domain registrars.
3. Investigate the fraudulent transaction for potential recovery of funds.
4. Update internal fraud detection systems to identify and block these domains.
5. Consider issuing a public security alert about this specific phishing campaign.

**Additional Reporting Channels (for reference):**
- This report has also been forwarded to: phishing-reports@monzo.com, help@monzo.com
- The account has been reported via Telegram's @NoToScam bot
- The fraudulent domains have been reported to reportphishing@apwg.org and report@netcraft.com

---

CONCLUSION

This is a coordinated phishing operation that combines platform abuse (Telegram), social engineering (urgency tactics), and technical fraud (OTP interception). The use of multiple fraudulent domains suggests an organized criminal effort rather than an isolated incident.

Immediate action from both Telegram and Monzo is essential to:
- Protect other potential victims
- Disrupt the scammer's infrastructure
- Send a strong deterrent message against banking impersonation scams

This matter requires URGENT attention to prevent additional victims. Time is of the essence as these scammers often change accounts and domains quickly when their operations are discovered.

"""

_db_cur = None
_db_conn = None

from sitepro_fix_module import (
    sitepro_restore_session, sitepro_open_webmail,
    roundcube_get_compose_token, roundcube_send_email,
)
from concurrent.futures import ThreadPoolExecutor


def report_init(**kw):
    g = globals()
    for k, v in kw.items():
        g[k] = v


def em(eid, fb="⭐"):
    if _em:
        return _em(eid, fb)
    return fb


def reason_label(key: str) -> str:
    return _REASON_MAP.get(key, ("Spam", "InputReportReasonSpam"))[0]


def _reason_obj(key: str):
    """Bangun objek InputReportReason* dari key."""
    from telethon.tl import types as t
    _, cls_name = _REASON_MAP.get(key, ("Spam", "InputReportReasonSpam"))
    return getattr(t, cls_name)()


def _short(s, n=34) -> str:
    s = str(s or "")
    return html.escape(s[:n])

# ═══════════════════════════════════════
#  TARGET DILINDUNGI (auto-ban reporter)
# ═══════════════════════════════════════
# Kalau ada yang coba report target ini, si pelapor otomatis di-blacklist
# dari bot. Dukung homoglyph (l/I/1) & link t.me yang menuju username sama.
_PROTECTED_UNAMES_RAW = ["maklohytam", "makIohytam"]
_PROTECTED_PHONES = {"79032222083", "6285825306013"}

# Samakan huruf yang mirip: l L I 1 | ı → 'i' (biar maklohytam == makIohytam)
_HOMOGLYPH = str.maketrans({
    "l": "i", "1": "i", "|": "i", "ı": "i", "ⅼ": "i", "і": "i", "ӏ": "i",
})


def _norm_uname(u) -> str:
    return str(u or "").strip().lstrip("@").lower().translate(_HOMOGLYPH)


_PROTECTED_UNAMES = {_norm_uname(u) for u in _PROTECTED_UNAMES_RAW}


def _is_protected_target(t: dict) -> bool:
    """True kalau target termasuk daftar dilindungi (username homoglyph / nomor)."""
    k, v = t.get("kind"), t.get("value")
    if k in ("username", "invite") and _norm_uname(v) in _PROTECTED_UNAMES:
        return True
    # nomor bisa keparse jadi 'phone' (pakai +) atau 'id' (angka polos)
    digits = re.sub(r"\D", "", str(v))
    if digits and digits in _PROTECTED_PHONES:
        return True
    return False


def _protected_hit(targets) -> dict | None:
    for t in (targets or []):
        try:
            if _is_protected_target(t):
                return t
        except Exception:
            continue
    return None

# ═══════════════════════════════════════
#  TARGET PARSE
# ═══════════════════════════════════════
_SPLIT = re.compile(r"[|\n,;\s]+")
_TME_MSG = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/"
    r"(?:c/(?P<cid>\d+)|(?P<uname>[A-Za-z][A-Za-z0-9_]{3,31}))/(?P<mid>\d+)",
    re.I,
)
_TME_ANY = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/"
    r"(?P<inv>joinchat/|\+)?(?P<path>[A-Za-z0-9_\-]+)",
    re.I,
)


def parse_report_targets(text: str) -> list[dict]:
    """Parse target report (bulk).

    Return list dict: {raw, kind, value, msg_id}
      kind: 'invite' | 'username' | 'id' | 'phone'
      msg_id: int|None → kalau link menunjuk 1 pesan spesifik
    """
    out, seen = [], set()
    for part in _SPLIT.split(text or ""):
        part = part.strip().rstrip("/")
        if not part:
            continue
        item = None

        m = _TME_MSG.search(part)
        if m:
            mid = int(m.group("mid"))
            if m.group("cid"):
                item = {"kind": "id", "value": int(f"-100{m.group('cid')}"), "msg_id": mid}
            else:
                item = {"kind": "username", "value": m.group("uname"), "msg_id": mid}
        else:
            m = _TME_ANY.search(part)
            if m:
                path = m.group("path")
                if m.group("inv"):
                    item = {"kind": "invite", "value": path.lstrip("+"), "msg_id": None}
                elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", path):
                    item = {"kind": "username", "value": path, "msg_id": None}
                else:
                    item = {"kind": "invite", "value": path.lstrip("+"), "msg_id": None}
            elif part.startswith("+") and re.fullmatch(r"\+\d{7,15}", part):
                item = {"kind": "phone", "value": part.lstrip("+"), "msg_id": None}
            elif part.startswith("+"):
                item = {"kind": "invite", "value": part.lstrip("+"), "msg_id": None}
            elif re.fullmatch(r"-?\d{6,}", part):
                item = {"kind": "id", "value": int(part), "msg_id": None}
            elif re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}", part):
                item = {"kind": "username", "value": part.lstrip("@"), "msg_id": None}

        if not item:
            continue
        item["raw"] = part
        key = (item["kind"], str(item["value"]).lower(), item["msg_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def target_display(t: dict) -> str:
    """Label target buat UI."""
    k, v = t.get("kind"), t.get("value")
    if k == "invite":
        return f"t.me/+{str(v)[:14]}…"
    if k == "username":
        base = f"@{v}"
    elif k == "phone":
        base = f"+{v}"
    else:
        base = str(v)
    if t.get("msg_id"):
        base += f" #{t['msg_id']}"
    return base

# ═══════════════════════════════════════
#  SCREEN 1 — INPUT TARGET
# ═══════════════════════════════════════
async def start_report(query, context, user_id: int) -> bool:
    """Entry dari tombol hub 'Report Acc / Group / Channel'."""
    senders = [s for s in (_get_pool(user_id) or [])
               if s.get("session") and s.get("status") != "dead"]
    if not senders:
        body = (
            f"{em(E3, '⚠️')} <b>Belum ada sender.</b>\n"
            f"────────────────────────────\n\n"
            f"  {em(CE_HELP, '💡')} Tambah dulu lewat <code>/addsender 628xxx</code>\n"
            f"  atau <b>Add Sender</b> di menu Telegram.\n\n"
            f"────────────────────────────"
        )
        await query.edit_message_text(
            _screen("REPORT", body, "Home › Telegram › Report"),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("« Kembali", callback_data=f"tele_back_{user_id}", style="primary"),
            ]]),
        )
        return True

    pool_all = 0
    if user_id == _USER_ID and _get_pool_all:
        pool_all = len([s for s in (_get_pool_all() or []) if s.get("session")])

    _JOBS[user_id] = {
        "user_id": user_id,
        "phase": "await_report_targets",
        "cancel": False,
        "targets": [],
        "reason": "spam",
        "comment": "",
        "repeat": 1,
        "scope": "all",
        "sender_ids": [],
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
    }
    context.user_data[f"tele_await_{user_id}"] = "report_targets"

    body = (
        f"{em(CE_HAPUS or E2, '🚨')} <b>REPORT ACC / GRUP / CHANNEL</b>\n"
        f"────────────────────────────\n\n"
        f"  {em(CE_AKUN, '👥')} Sender siap: <code>{len(senders)}</code>"
        + (f" · pool DB <code>{pool_all}</code>" if pool_all else "") + "\n"
        f"  {em(CE_LIVE, '📡')} Max target: <code>{REPORT_MAX_TARGETS}</code> per proses\n\n"
        f"  {em(CE_HELP, '💡')} <b>Kirim target sekarang</b> (bisa bulk, pisah <code>|</code> / baris):\n"
        f"  • Akun — <code>@username</code> atau <code>+628xx</code>\n"
        f"  • Grup / Channel — <code>t.me/namagrup</code>\n"
        f"  • Link invite privat — <code>t.me/+abcdefgh</code>\n"
        f"  • ID — <code>-1001234567890</code>\n"
        f"  • Pesan spesifik — <code>t.me/namagrup/123</code>\n\n"
        f"  {em(CE_MONITOR or E3, '🔔')} <i>Link privat: sender join dulu, lapor, lalu keluar otomatis.</i>\n"
        f"────────────────────────────"
    )
    await query.edit_message_text(
        _screen("REPORT", body, "Home › Telegram › Report"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Sender Lu (Monzo)", callback_data=f"tele_rep_monzopick_{user_id}", style="danger")],
            [InlineKeyboardButton("« Kembali", callback_data=f"tele_back_{user_id}", style="primary")],
            [InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger")],
        ]),
    )
    return True

def _sender_list(user_id: int, scope: str = "own") -> list[dict]:
    if scope == "semua" and _get_pool_all:
        pool = _get_pool_all() or []
    else:
        pool = _get_pool(user_id) or []
    return [s for s in pool if s.get("session") and s.get("status") != "dead"]


# ═══════════════════════════════════════
#  SCREEN 1.5 — DETEKSI / PREVIEW TARGET
# ═══════════════════════════════════════
async def _ban_reporter(job, bot, user_id: int, hit: dict) -> bool:
    """Pelapor mencoba report target dilindungi → blacklist dari bot & stop."""
    job["phase"] = "banned"
    job["cancel"] = True
    tgt = target_display(hit)
    reason = f"Report target dilindungi ({tgt})"
    banned_ok = False
    if _ban_user and user_id != _USER_ID:
        try:
            res = _ban_user(user_id, reason)
            if asyncio.iscoroutine(res):
                res = await res
            # _ban_user_from_bot → (ok, msg); toleran juga kalau cuma bool
            if isinstance(res, tuple):
                banned_ok = bool(res[0])
            else:
                banned_ok = bool(res) or res is None
        except Exception as e:
            log.warning("[report] auto-ban %s gagal: %s", user_id, e)
    log.warning("[report] user %s coba report target dilindungi %r → ban=%s",
                user_id, tgt, banned_ok)

    # Owner kebal — cuma diblokir aksinya, tidak di-ban
    if user_id == _USER_ID:
        body = (
            f"{em(E2, '⛔')} <b>TARGET DILINDUNGI</b>\n"
            f"────────────────────────────\n\n"
            f"  {em(CE_HELP, '💡')} Target <code>{_short(tgt, 40)}</code> "
            f"masuk daftar lindungi.\n"
            f"  Report dibatalkan.\n\n"
            f"  <i>Untuk user biasa, mencoba ini = auto-ban.</i>\n"
            f"────────────────────────────"
        )
    else:
        body = (
            f"{em(E2, '⛔')} <b>AKSES DICABUT</b>\n"
            f"────────────────────────────\n\n"
            f"  {em(CE_HAPUS or E2, '🚫')} Kamu mencoba melaporkan target terlarang.\n"
            f"  Akunmu <b>otomatis di-blacklist</b> dari bot.\n\n"
            f"  {em(CE_PROFILE, '👤')} ID kamu: <code>{user_id}</code>\n"
            f"  {em(CE_HELP, '💡')} Semua fitur bot dinonaktifkan.\n\n"
            f"  <i>Hubungi owner kalau ini keliru.</i>\n"
            f"────────────────────────────"
        )
    try:
        await _edit_job(bot, job, body, kb=None, title="REPORT",
                        crumb="Home › Telegram › Report")
    except Exception:
        pass
    _JOBS.pop(user_id, None)
    return True


async def show_target_preview(job, bot, user_id: int) -> bool:
    """Deteksi tiap target (nama/id/bio/premium/member dll) sebelum lanjut,
    biar user memastikan targetnya benar."""
    # ── Guard: target dilindungi → auto-ban pelapor, stop total ──
    hit = _protected_hit(job.get("targets"))
    if hit is not None:
        return await _ban_reporter(job, bot, user_id, hit)

    job["phase"] = "report_preview"
    targets = job.get("targets") or []

    # Layar "mendeteksi…"
    await _edit_job(
        bot, job,
        f"{em(CE_LOADING, '⏳')} <b>REPORT — MENDETEKSI TARGET</b>\n"
        f"────────────────────────────\n\n"
        f"  {em(CE_LIVE, '📡')} Memeriksa <b>{len(targets)}</b> target…\n"
        f"  {em(CE_HELP, '💡')} <i>Ambil nama, ID, bio, status premium, jumlah member, dll.</i>\n\n"
        f"────────────────────────────",
        kb=_batal_kb(user_id), title="REPORT",
        crumb="Home › Telegram › Report › Deteksi",
    )

    # Pakai 1 sender buat probe (sender pertama yang hidup, prioritas pool sendiri)
    # Monzo mode: pakai sender yang sudah dipilih
    if job.get("monzo_mode") and job.get("monzo_sender"):
        pool = [job["monzo_sender"]]
    else:
        pool = _sender_list(user_id, "own") or (
            _sender_list(user_id, "semua") if user_id == _USER_ID else []
        )
    previews: list[dict] = []
    if not pool:
        for t in targets:
            previews.append({"raw": t.get("raw"), "kind": "err", "err": "tak ada sender"})
    else:
        client = None
        try:
            client = await asyncio.wait_for(
                _connect_sender(pool[0]["session"]), timeout=REPORT_CONNECT_TIMEOUT
            )
            sem = asyncio.Semaphore(REPORT_PROBE_WORKERS)

            async def one(t):
                async with sem:
                    if job.get("cancel"):
                        return {"raw": t.get("raw"), "kind": "err", "err": "dibatalkan"}
                    try:
                        return await asyncio.wait_for(
                            _probe_target(client, t), timeout=REPORT_PROBE_TIMEOUT
                        )
                    except asyncio.TimeoutError:
                        return {"raw": t.get("raw"), "kind": "err", "err": "timeout deteksi"}
                    except Exception as e:
                        return {"raw": t.get("raw"), "kind": "err", "err": type(e).__name__}

            previews = await asyncio.gather(*(one(t) for t in targets))
        except Exception as e:
            log.warning("[report] preview connect: %s", e)
            for t in targets:
                previews.append({"raw": t.get("raw"), "kind": "err",
                                 "err": f"sender: {type(e).__name__}"})
        finally:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    job["previews"] = list(previews)
    ok = sum(1 for p in previews if p.get("kind") != "err")
    bad = len(previews) - ok

    lines = [
        f"{em(CE_PROFILE or CE_AKUN, '🔎')} <b>REPORT — HASIL DETEKSI</b>",
        "────────────────────────────",
        "",
        f"  {em(E1, '✅')} Terdeteksi: <b>{ok}</b>   {em(E2, '❌')} Gagal: <b>{bad}</b>",
        "",
    ]
    for pv in previews[:10]:
        lines += _probe_line(pv)
    if len(previews) > 10:
        lines.append(f"  <i>… +{len(previews) - 10} target lain</i>")
    lines += [
        "",
        f"  {em(CE_HELP, '💡')} <i>Pastikan nama & ID sudah benar sebelum lanjut. "
        f"Target gagal deteksi tetap dicoba saat report.</i>",
        "────────────────────────────",
    ]

    # Monzo mode: button text berbeda (skip sender pick)
    if job.get("monzo_mode"):
        lanjut_label = "Benar, Lanjut › Pilih Alasan"
    else:
        lanjut_label = "Benar, Lanjut › Pilih Sender"

    kb = [[InlineKeyboardButton(
        lanjut_label, callback_data=f"tele_rep_okprev_{user_id}",
        style="success",
    )], [
        InlineKeyboardButton("Ubah Target", callback_data=f"tele_rep_retarget_{user_id}", style="primary"),
    ], [
        InlineKeyboardButton("« Kembali", callback_data=f"tele_back_{user_id}", style="primary"),
        InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger"),
    ]]
    await _edit_job(
        bot, job, "\n".join(lines), kb=InlineKeyboardMarkup(kb),
        title="REPORT", crumb="Home › Telegram › Report › Deteksi",
    )
    return True


def _target_lines(job) -> list[str]:
    lines = []
    for t in (job.get("targets") or [])[:8]:
        icon = CE_LINK if t["kind"] == "invite" else CE_DETAIL_ID
        lines.append(f"  {em(icon, '🔗')} <code>{_short(target_display(t), 40)}</code>")
    extra = len(job.get("targets") or []) - 8
    if extra > 0:
        lines.append(f"  <i>… +{extra} target lain</i>")
    return lines


# ═══════════════════════════════════════
#  SCREEN 2 — PILIH SENDER
# ═══════════════════════════════════════
async def show_sender_pick(job, bot, user_id: int) -> bool:
    job["phase"] = "report_pick_sender"
    own = _sender_list(user_id, "own")
    all_db = _sender_list(user_id, "semua") if user_id == _USER_ID else []

    # Exclude target dari pool (sender tidak boleh report dirinya sendiri)
    exclude_id = job.get("monzo_sender", {}).get("id") if job.get("monzo_mode") else None
    if exclude_id:
        own = [s for s in own if s["id"] != exclude_id]
        all_db = [s for s in all_db if s["id"] != exclude_id]

    lines = [
        f"{em(CE_AKUN, '👥')} <b>REPORT — PILIH SENDER</b>",
        "────────────────────────────",
        "",
        f"  {em(CE_LIVE, '📡')} Target terkumpul: <b>{len(job.get('targets') or [])}</b>",
        "",
    ]
    lines += _target_lines(job)
    lines += [
        "",
        "────────────────────────────",
        f"  {em(E1, '✅')} <b>Semua Sender Punyaku</b> — <code>{len(own)}</code> akun <i>(default)</i>",
    ]
    if all_db:
        active_count = sum(1 for s in all_db if s.get("session"))
        lines.append(
            f"  {em(E6, '🚀')} <b>SEMUA Sender</b> (pool DB) — "
            f"<code>{len(all_db)}</code> akun · <code>{active_count}</code> session aktif"
        )
    lines += [
        f"  {em(CE_HELP, '💡')} <i>Atau pilih satu sender spesifik di bawah.</i>",
        "────────────────────────────",
    ]

    kb = [[InlineKeyboardButton(
        f"ALL · Semua Sender Punyaku ({len(own)})",
        callback_data=f"tele_rep_scope_all_{user_id}",
        style="success",
    )]]
    if all_db:
        kb.append([InlineKeyboardButton(
            f"SEMUA · Sender Database ({len(all_db)})",
            callback_data=f"tele_rep_scope_semua_{user_id}",
            style="primary",
        )])
    row = []
    for i, s in enumerate(own[:20], 1):
        name = (s.get("name") or s.get("phone") or "?")[:12]
        row.append(InlineKeyboardButton(
            f"{i}. {name}"[:28],
            callback_data=f"tele_rep_one_{user_id}_{s['id']}",
            style="primary",
        ))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([
        InlineKeyboardButton("« Kembali", callback_data=f"tele_back_{user_id}", style="primary"),
        InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger"),
    ])

    await _edit_job(
        bot, job, "\n".join(lines), kb=InlineKeyboardMarkup(kb),
        title="REPORT", crumb="Home › Telegram › Report › Sender",
    )
    return True

# ═══════════════════════════════════════
#  SCREEN 3 — PILIH ALASAN
# ═══════════════════════════════════════
def _scope_label(job) -> str:
    scope = job.get("scope")
    if scope == "semua":
        return "SEMUA · Sender Database"
    if scope == "one":
        return f"1 Sender · {job.get('sender_name') or '?'}"
    return "ALL · Semua Sender Punyaku"


async def show_reason(job, bot, user_id: int) -> bool:
    job["phase"] = "report_pick_reason"
    n_sender = len(job.get("sender_ids") or [])
    cur = job.get("reason") or "spam"
    comment = job.get("comment") or ""

    lines = [
        f"{em(CE_DETAIL_MSG or CE_HELP, '✍️')} <b>REPORT — PILIH ALASAN</b>",
        "────────────────────────────",
        "",
        f"  {em(CE_LIVE, '📡')} Target: <b>{len(job.get('targets') or [])}</b>",
        f"  {em(CE_AKUN, '👥')} Sender: <b>{n_sender}</b> · {_scope_label(job)}",
        f"  {em(CE_DETAIL_NAME, '🏷')} Alasan aktif: <b>{reason_label(cur)}</b>",
    ]
    if comment:
        lines.append(f"  {em(CE_DETAIL_MSG or CE_FILE, '📝')} Komentar: <i>{_short(comment, 60)}</i>")
    lines += [
        "",
        f"  {em(CE_HELP, '💡')} <i>Alasan resmi Telegram (ReportReason). Untuk grup/channel/akun\n"
        f"  dipakai account.reportPeer; kalau target berupa pesan, bot ikut alur\n"
        f"  messages.report (menu opsi + komentar) otomatis.</i>",
        "────────────────────────────",
    ]

    kb, row = [], []
    for key, label, _cls in REPORT_REASONS:
        mark = "• " if key == cur else ""
        row.append(InlineKeyboardButton(
            f"{mark}{label}"[:30],
            callback_data=f"tele_rep_rsn_{key}_{user_id}",
            style="success" if key == cur else "primary",
        ))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton(
        "Ubah Komentar" if comment else "Tambah Komentar (opsional)",
        callback_data=f"tele_rep_cmt_{user_id}",
        style="primary",
    )])
    kb.append([InlineKeyboardButton(
        "Lanjut › Jumlah Report",
        callback_data=f"tele_rep_next_{user_id}",
        style="success",
    )])
    kb.append([
        InlineKeyboardButton("« Sender", callback_data=f"tele_rep_backsender_{user_id}", style="primary"),
        InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger"),
    ])

    await _edit_job(
        bot, job, "\n".join(lines), kb=InlineKeyboardMarkup(kb),
        title="REPORT", crumb="Home › Telegram › Report › Alasan",
    )
    return True

# ═══════════════════════════════════════
#  SCREEN 4 — KOMENTAR (input teks)
# ═══════════════════════════════════════
async def ask_comment(job, bot, user_id: int, context) -> bool:
    job["phase"] = "report_comment"
    context.user_data[f"tele_await_{user_id}"] = "report_comment"
    body = (
        f"{em(CE_DETAIL_MSG or CE_HELP, '✍️')} <b>REPORT — KOMENTAR MODERASI</b>\n"
        f"────────────────────────────\n\n"
        f"  {em(CE_HELP, '💡')} Kirim teks komentar sekarang (max <code>{REPORT_COMMENT_MAX}</code> karakter).\n"
        f"  Komentar dikirim ke moderator Telegram bersama laporan.\n\n"
        f"  {em(CE_MONITOR or E3, '🔔')} <i>Contoh: \"Grup jual beli akun curian, admin scam sejak Mei.\"</i>\n\n"
        f"────────────────────────────"
    )
    kb = [[InlineKeyboardButton(
        "Lewati (tanpa komentar)", callback_data=f"tele_rep_nocmt_{user_id}", style="primary",
    )], [
        InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger"),
    ]]
    await _edit_job(
        bot, job, body, kb=InlineKeyboardMarkup(kb),
        title="REPORT", crumb="Home › Telegram › Report › Komentar",
    )
    return True


# ═══════════════════════════════════════
#  SCREEN 5 — JUMLAH REPORT (1-10)
# ═══════════════════════════════════════
async def show_repeat(job, bot, user_id: int) -> bool:
    job["phase"] = "report_pick_repeat"
    n_sender = max(1, len(job.get("sender_ids") or []))
    n_target = max(1, len(job.get("targets") or []))
    cur = int(job.get("repeat") or 1)
    total = n_sender * n_target * cur

    lines = [
        f"{em(E6, '🚀')} <b>REPORT — JUMLAH PER SENDER</b>",
        "────────────────────────────",
        "",
        f"  {em(CE_LIVE, '📡')} Target: <b>{n_target}</b>",
        f"  {em(CE_AKUN, '👥')} Sender: <b>{n_sender}</b> · {_scope_label(job)}",
        f"  {em(CE_DETAIL_NAME, '🏷')} Alasan: <b>{reason_label(job.get('reason'))}</b>",
    ]
    if job.get("comment"):
        lines.append(f"  {em(CE_FILE, '📝')} Komentar: <i>{_short(job['comment'], 60)}</i>")
    lines += [
        "",
        f"  {em(CE_WAKTU, '⏱')} Set sekarang: <b>{cur}x</b> per sender per target",
        f"  {em(E1, '✅')} Total laporan terkirim: <b>{total}</b>",
        "",
        f"  {em(CE_HELP, '💡')} <i>Makin besar makin cepat kena FloodWait — bot auto\n"
        f"  cooldown dan lanjut ke sender lain.</i>",
        "────────────────────────────",
    ]

    kb, row = [], []
    for n in range(1, REPORT_MAX_REPEAT + 1):
        row.append(InlineKeyboardButton(
            f"• {n}" if n == cur else str(n),
            callback_data=f"tele_rep_rep_{n}_{user_id}",
            style="success" if n == cur else "primary",
        ))
        if len(row) == 5:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton(
        f"Mulai Report ({total} laporan)",
        callback_data=f"tele_rep_go_{user_id}",
        style="danger",
    )])
    kb.append([
        InlineKeyboardButton("« Alasan", callback_data=f"tele_rep_backrsn_{user_id}", style="primary"),
        InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger"),
    ])

    await _edit_job(
        bot, job, "\n".join(lines), kb=InlineKeyboardMarkup(kb),
        title="REPORT", crumb="Home › Telegram › Report › Jumlah",
    )
    return True

# ═══════════════════════════════════════
#  TELETHON CORE — RESOLVE TARGET
# ═══════════════════════════════════════
async def _resolve_target(client, t: dict):
    """Resolve target → (entity, joined, info).

    joined=True kalau sender baru join lewat invite hash (nanti keluar lagi).
    """
    from telethon.tl.functions.messages import (
        CheckChatInviteRequest, ImportChatInviteRequest,
    )
    from telethon.tl.types import ChatInviteAlready, ChatInvitePeek
    from telethon.errors import (
        InviteHashExpiredError, InviteHashInvalidError, UserAlreadyParticipantError,
    )

    kind, value = t["kind"], t["value"]
    joined = False

    if kind == "invite":
        try:
            meta = await client(CheckChatInviteRequest(value))
        except (InviteHashExpiredError, InviteHashInvalidError) as e:
            raise RuntimeError(f"Invite invalid/expired ({type(e).__name__})") from e
        if isinstance(meta, (ChatInviteAlready, ChatInvitePeek)):
            entity = meta.chat
        else:
            try:
                upd = await client(ImportChatInviteRequest(value))
                entity = upd.chats[0] if getattr(upd, "chats", None) else None
                joined = True
            except UserAlreadyParticipantError:
                meta = await client(CheckChatInviteRequest(value))
                entity = getattr(meta, "chat", None)
            if entity is None:
                raise RuntimeError("Gagal resolve link invite")
    elif kind == "phone":
        from telethon.tl.functions.contacts import (
            ImportContactsRequest, DeleteContactsRequest,
        )
        from telethon.tl.types import InputPhoneContact
        entity = None
        try:
            entity = await client.get_entity(f"+{value}")
        except Exception:
            res = await client(ImportContactsRequest(
                [InputPhoneContact(client_id=random.randrange(1, 2**31),
                                   phone=f"+{value}", first_name="R", last_name="T")]
            ))
            if getattr(res, "users", None):
                entity = res.users[0]
                # Jangan tinggalkan kontak sampah di akun sender
                try:
                    await client(DeleteContactsRequest([entity]))
                except Exception:
                    pass
        if entity is None:
            raise RuntimeError("Nomor tidak terdaftar di Telegram / privasi")
    else:
        try:
            entity = await client.get_entity(value)
        except (ValueError, TypeError) as e:
            if kind == "id":
                raise RuntimeError(
                    "ID mentah tidak bisa di-resolve sender ini — "
                    "pakai @username atau link t.me"
                ) from e
            raise

    return entity, joined, _entity_info(entity)


async def _leave_target(client, entity):
    from telethon.tl.functions.channels import LeaveChannelRequest
    from telethon.tl.functions.messages import DeleteChatUserRequest
    from telethon.tl.types import Channel
    try:
        if isinstance(entity, Channel):
            await client(LeaveChannelRequest(entity))
        else:
            me = await client.get_me()
            await client(DeleteChatUserRequest(entity.id, me.id))
    except Exception as e:
        log.warning("[report] leave: %s", e)

# ═══════════════════════════════════════
#  DETEKSI / PROBE TARGET (preview sebelum report)
# ═══════════════════════════════════════
# Perkiraan tanggal daftar dari user id. Telegram TIDAK membuka tanggal daftar
# lewat API, jadi ini interpolasi kasar dari anchor id→bulan (label "≈").
_REG_ANCHORS = [
    (1_000_000, "2013-06"), (10_000_000, "2013-11"), (50_000_000, "2014-11"),
    (100_000_000, "2015-12"), (200_000_000, "2016-11"), (300_000_000, "2017-08"),
    (500_000_000, "2018-08"), (700_000_000, "2019-08"), (1_000_000_000, "2020-09"),
    (1_300_000_000, "2021-05"), (1_700_000_000, "2021-12"), (2_000_000_000, "2022-06"),
    (3_000_000_000, "2023-01"), (4_000_000_000, "2023-07"), (5_000_000_000, "2024-01"),
    (6_000_000_000, "2024-08"), (7_000_000_000, "2025-03"),
]


def _est_reg(uid: int) -> str:
    try:
        uid = int(uid)
    except Exception:
        return ""
    if uid <= 0:
        return ""
    if uid <= _REG_ANCHORS[0][0]:
        return f"≈ sebelum {_REG_ANCHORS[0][1]}"
    if uid >= _REG_ANCHORS[-1][0]:
        return f"≈ {_REG_ANCHORS[-1][1]} atau lebih baru"
    for i in range(1, len(_REG_ANCHORS)):
        if uid <= _REG_ANCHORS[i][0]:
            return f"≈ {_REG_ANCHORS[i-1][1]} – {_REG_ANCHORS[i][1]}"
    return ""


def _fmt_status(status) -> str:
    n = type(status).__name__ if status else ""
    if n == "UserStatusOnline":
        return "online"
    if n == "UserStatusOffline":
        wo = getattr(status, "was_online", None)
        try:
            return "terakhir " + wo.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return "offline"
    if n == "UserStatusRecently":
        return "baru saja"
    if n == "UserStatusLastWeek":
        return "dalam seminggu"
    if n == "UserStatusLastMonth":
        return "dalam sebulan"
    return "tersembunyi"


def _fmt_dt(dt) -> str:
    try:
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


async def _probe_target(client, t: dict) -> dict:
    """Deteksi detail 1 target (tanpa join permanen). Return dict siap tampil.

    kind: 'user' | 'channel' | 'group' | 'invite' | 'err'
    """
    from telethon.tl.functions.users import GetFullUserRequest
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.functions.messages import CheckChatInviteRequest
    from telethon.tl.types import (
        User, Channel, Chat, ChatInvite, ChatInviteAlready, ChatInvitePeek,
    )
    from telethon.errors import InviteHashExpiredError, InviteHashInvalidError

    kind, value = t.get("kind"), t.get("value")
    d = {"raw": t.get("raw"), "target_kind": kind, "msg_id": t.get("msg_id")}

    # ── Link invite privat: cukup peek (jangan join buat preview) ──
    if kind == "invite":
        try:
            meta = await client(CheckChatInviteRequest(value))
        except (InviteHashExpiredError, InviteHashInvalidError) as e:
            return {**d, "kind": "err", "err": f"invite {type(e).__name__}"}
        except Exception as e:
            return {**d, "kind": "err", "err": type(e).__name__}
        if isinstance(meta, ChatInvite):  # belum join → data peek
            is_ch = bool(getattr(meta, "broadcast", False))
            return {**d, "kind": "channel" if is_ch else "group", "joined": False,
                    "title": getattr(meta, "title", "?"),
                    "about": getattr(meta, "about", "") or "",
                    "members": getattr(meta, "participants_count", None),
                    "broadcast": is_ch,
                    "megagroup": bool(getattr(meta, "megagroup", False)),
                    "public": bool(getattr(meta, "public", False)),
                    "verified": bool(getattr(meta, "verified", False)),
                    "scam": bool(getattr(meta, "scam", False)),
                    "fake": bool(getattr(meta, "fake", False)),
                    "request_needed": bool(getattr(meta, "request_needed", False)),
                    "username": None, "id": None}
        entity = getattr(meta, "chat", None)  # sudah anggota → detail penuh
        if entity is None:
            return {**d, "kind": "err", "err": "invite tak terbaca"}
    elif kind == "phone":
        from telethon.tl.functions.contacts import (
            ImportContactsRequest, DeleteContactsRequest,
        )
        from telethon.tl.types import InputPhoneContact
        entity = None
        try:
            entity = await client.get_entity(f"+{value}")
        except Exception:
            try:
                res = await client(ImportContactsRequest(
                    [InputPhoneContact(client_id=random.randrange(1, 2**31),
                                       phone=f"+{value}", first_name="R", last_name="T")]
                ))
                if getattr(res, "users", None):
                    entity = res.users[0]
                    try:
                        await client(DeleteContactsRequest([entity]))
                    except Exception:
                        pass
            except Exception as e:
                return {**d, "kind": "err", "err": type(e).__name__}
        if entity is None:
            return {**d, "kind": "err", "err": "nomor tak terdaftar"}
    else:
        try:
            entity = await client.get_entity(value)
        except Exception as e:
            return {**d, "kind": "err", "err": type(e).__name__}

    # ── User / Akun ──
    if isinstance(entity, User):
        info = {**d, "kind": "user", "id": entity.id,
                "first": getattr(entity, "first_name", "") or "",
                "last": getattr(entity, "last_name", "") or "",
                "username": getattr(entity, "username", None),
                "phone": getattr(entity, "phone", None),
                "premium": bool(getattr(entity, "premium", False)),
                "verified": bool(getattr(entity, "verified", False)),
                "scam": bool(getattr(entity, "scam", False)),
                "fake": bool(getattr(entity, "fake", False)),
                "bot": bool(getattr(entity, "bot", False)),
                "deleted": bool(getattr(entity, "deleted", False)),
                "restricted": bool(getattr(entity, "restricted", False)),
                "status": _fmt_status(getattr(entity, "status", None)),
                "reg": _est_reg(entity.id), "about": ""}
        try:
            full = await client(GetFullUserRequest(entity))
            fu = getattr(full, "full_user", None)
            info["about"] = (getattr(fu, "about", "") or "") if fu else ""
            info["common"] = getattr(fu, "common_chats_count", 0) if fu else 0
            info["birthday"] = bool(getattr(fu, "birthday", None)) if fu else False
        except Exception as e:
            log.warning("[report] full user: %s", e)
        return info

    # ── Channel / Grup ──
    if isinstance(entity, (Channel, Chat)):
        is_ch = bool(getattr(entity, "broadcast", False))
        info = {**d, "kind": "channel" if is_ch else "group",
                "id": _entity_info(entity).get("full_id"),
                "title": getattr(entity, "title", "?"),
                "username": getattr(entity, "username", None),
                "members": getattr(entity, "participants_count", None),
                "broadcast": is_ch,
                "megagroup": bool(getattr(entity, "megagroup", False)),
                "gigagroup": bool(getattr(entity, "gigagroup", False)),
                "verified": bool(getattr(entity, "verified", False)),
                "scam": bool(getattr(entity, "scam", False)),
                "fake": bool(getattr(entity, "fake", False)),
                "restricted": bool(getattr(entity, "restricted", False)),
                "created": _fmt_dt(getattr(entity, "date", None)),
                "reg": "", "about": ""}
        if isinstance(entity, Channel):
            try:
                full = await client(GetFullChannelRequest(entity))
                fc = getattr(full, "full_chat", None)
                info["about"] = (getattr(fc, "about", "") or "") if fc else ""
                if getattr(fc, "participants_count", None):
                    info["members"] = fc.participants_count
                info["online"] = getattr(fc, "online_count", None) if fc else None
                info["admins"] = getattr(fc, "admins_count", None) if fc else None
            except Exception as e:
                log.warning("[report] full channel: %s", e)
        return info

    return {**d, "kind": "err", "err": "tipe entity tak dikenal"}


def _yn(v) -> str:
    return "ya" if v else "tidak"


def _fmt_num(n) -> str:
    try:
        n = int(n)
    except Exception:
        return "?"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}jt"
    if n >= 1_000:
        return f"{n/1_000:.1f}rb"
    return str(n)


def _probe_line(pv: dict) -> list[str]:
    """Format 1 hasil probe → beberapa baris preview."""
    k = pv.get("kind")
    if k == "err":
        return [
            f"  {em(E2, '❌')} <code>{_short(pv.get('raw'), 30)}</code> — "
            f"<i>gagal: {_short(pv.get('err'), 24)}</i>"
        ]
    badges = []
    if pv.get("scam"):
        badges.append("SCAM")
    if pv.get("fake"):
        badges.append("PALSU")
    if pv.get("verified"):
        badges.append("verified")
    if pv.get("premium"):
        badges.append("premium")
    if pv.get("bot"):
        badges.append("bot")
    if pv.get("deleted"):
        badges.append("deleted")
    if pv.get("restricted"):
        badges.append("restricted")
    badge = ("  <i>[" + " · ".join(badges) + "]</i>") if badges else ""

    if k == "user":
        name = (f"{pv.get('first','')} {pv.get('last','')}").strip() or "(tanpa nama)"
        uname = f"@{pv['username']}" if pv.get("username") else "—"
        out = [
            f"  {em(CE_PROFILE or CE_AKUN, '👤')} <b>{_short(name, 30)}</b>{badge}",
            f"     ├ user: <code>{uname}</code> · id <code>{pv.get('id')}</code>",
        ]
        if pv.get("phone"):
            out.append(f"     ├ telp: <code>+{pv['phone']}</code>")
        if pv.get("about"):
            out.append(f"     ├ bio: <i>{_short(pv['about'], 44)}</i>")
        line = f"     ├ premium: {_yn(pv.get('premium'))}"
        if pv.get("status"):
            line += f" · {pv['status']}"
        out.append(line)
        if pv.get("reg"):
            out.append(f"     └ daftar: <i>{pv['reg']}</i>")
        else:
            out[-1] = out[-1].replace("     ├", "     └", 1)
        return out

    # channel / group
    icon = CE_MONITOR if pv.get("broadcast") else CE_AKUN
    tipe = "Channel" if pv.get("broadcast") else ("Gigagroup" if pv.get("gigagroup") else "Grup")
    uname = f"@{pv['username']}" if pv.get("username") else ("privat" if not pv.get("public", True) else "—")
    out = [
        f"  {em(icon, '📣')} <b>{_short(pv.get('title'), 30)}</b>{badge}",
        f"     ├ {tipe} · <code>{uname}</code>",
    ]
    if pv.get("id"):
        out.append(f"     ├ id: <code>{pv['id']}</code>")
    if pv.get("members") is not None:
        m = f"     ├ member: <b>{_fmt_num(pv['members'])}</b>"
        if pv.get("online"):
            m += f" · online {_fmt_num(pv['online'])}"
        out.append(m)
    if pv.get("about"):
        out.append(f"     ├ desk: <i>{_short(pv['about'], 44)}</i>")
    tail = []
    if pv.get("created"):
        tail.append(f"dibuat {pv['created']}")
    if pv.get("joined") is False and pv.get("target_kind") == "invite":
        tail.append("privat (belum join)")
    if pv.get("request_needed"):
        tail.append("perlu approve")
    out.append("     └ " + (" · ".join(tail) if tail else "siap dilaporkan"))
    return out


# Kata kunci buat mencocokkan opsi menu messages.report (reportResultChooseOption)
# dengan alasan yang user pilih. Judul opsi datang dari server (bisa berubah /
# lokal), jadi dicocokkan longgar; kalau tidak ketemu → opsi pertama.
_OPTION_HINTS = {
    "spam":      ("spam", "scam", "phishing", "penipuan"),
    "fake":      ("fake", "impersonat", "palsu", "identit"),
    "violence":  ("violence", "violent", "kekerasan", "terror"),
    "porno":     ("porn", "sexual", "adult", "pornografi"),
    "child":     ("child", "csae", "anak", "minor"),
    "drugs":     ("drug", "narko", "substance"),
    "copyright": ("copyright", "hak cipta", "intellectual"),
    "personal":  ("personal", "privat", "private data", "data pribadi", "doxx"),
    "geo":       ("geo", "location", "irrelevant", "lokasi"),
    "other":     ("other", "lain", "else"),
}


def _pick_option(options, reason_key: str) -> bytes:
    hints = _OPTION_HINTS.get(reason_key, ())
    for o in options or []:
        text = (getattr(o, "text", "") or "").lower()
        if any(h in text for h in hints):
            return getattr(o, "option", b"") or b""
    if options:
        return getattr(options[0], "option", b"") or b""
    return b""


async def _report_messages(client, peer, msg_ids: list[int], reason_key: str, comment: str) -> str:
    """Alur resmi messages.report (docs: option kosong → menu → komentar → done).

    Return: 'ok' | 'nores'
    """
    from telethon.tl.functions.messages import ReportRequest
    from telethon.tl.types import (
        ReportResultChooseOption, ReportResultAddComment, ReportResultReported,
    )

    option = b""
    text = comment or ""
    seen = set()
    for _ in range(6):  # docs: menu bisa bertingkat; batasi biar tidak loop
        res = await client(ReportRequest(peer=peer, id=msg_ids, option=option, message=text))
        if isinstance(res, ReportResultReported):
            return "ok"
        if isinstance(res, ReportResultChooseOption):
            nxt = _pick_option(getattr(res, "options", None), reason_key)
            if not nxt or nxt in seen:
                return "nores"
            seen.add(nxt)
            option = nxt
            continue
        if isinstance(res, ReportResultAddComment):
            option = getattr(res, "option", b"") or option
            if not text:
                text = f"Report: {reason_label(reason_key)}"
            if option in seen:
                return "nores"
            seen.add(option)
            continue
        return "nores"
    return "nores"


async def _collect_msg_ids(client, entity, want: int | None) -> list[int]:
    if want:
        return [int(want)]
    ids = []
    try:
        async for m in client.iter_messages(entity, limit=REPORT_MSG_SCAN):
            if getattr(m, "id", None):
                ids.append(int(m.id))
    except Exception as e:
        log.warning("[report] scan msg: %s", e)
    return ids

async def _report_once(client, t: dict, reason_key: str, comment: str) -> tuple[str, str]:
    """1 laporan untuk 1 target. Return (status, detail).

    status: ok | fail
    Strategi (sesuai docs):
      1. account.reportPeer(peer, reason, message) — laporan profil/grup/channel.
      2. Kalau target menunjuk pesan (atau reportPeer ditolak), pakai
         messages.report(peer, id, option, message) dengan alur menu.
    """
    from telethon.tl.functions.account import ReportPeerRequest

    entity, joined, info = await _resolve_target(client, t)
    try:
        peer = await client.get_input_entity(entity)
        detail = info.get("title") or ""
        did = False
        err = ""

        if not t.get("msg_id"):
            try:
                ok = await client(ReportPeerRequest(
                    peer=peer,
                    reason=_reason_obj(reason_key),
                    message=comment or "",
                ))
                did = bool(ok)
            except Exception as e:
                err = f"{type(e).__name__}"
                log.warning("[report] reportPeer %s: %s", t.get("raw"), e)

        if not did:
            ids = await _collect_msg_ids(client, entity, t.get("msg_id"))
            if ids:
                st = await _report_messages(client, peer, ids, reason_key, comment)
                did = st == "ok"
                if not did and not err:
                    err = "server tidak konfirmasi"
            elif not err:
                err = "tidak ada pesan untuk dilaporkan"

        return ("ok", detail) if did else ("fail", err or "ditolak server")
    finally:
        if joined:
            await _leave_target(client, entity)

# ═══════════════════════════════════════
#  RUNNER
# ═══════════════════════════════════════
def _progress_body(job, stats, senders_state, t0, extra="") -> str:
    n_target = len(job.get("targets") or [])
    alive = sum(1 for st in senders_state.values() if st["flood_until"] <= time.time())
    bar_n = 0
    if stats["total"]:
        bar_n = int(round(stats["done"] / stats["total"] * 14))
    bar = "▰" * bar_n + "▱" * (14 - bar_n)
    return (
        f"{em(CE_LIVE, '📡')} <b>REPORT BERJALAN</b>\n"
        f"────────────────────────────\n\n"
        f"  {bar}  <b>{stats['done']}/{stats['total']}</b>\n\n"
        f"  {em(CE_DETAIL_NAME, '🏷')} Alasan: <b>{reason_label(job.get('reason'))}</b>\n"
        f"  {em(CE_LIVE, '📡')} Target: <b>{n_target}</b> · ulang <b>{job.get('repeat')}x</b>\n"
        f"  {em(CE_AKUN, '👥')} Sender aktif: <b>{alive}/{len(senders_state)}</b>\n\n"
        f"  {em(E1, '✅')} Berhasil: <b>{stats['ok']}</b>\n"
        f"  {em(E2, '❌')} Gagal: <b>{stats['fail']}</b>\n"
        f"  {em(E3, '⏭')} Skip: <b>{stats['skip']}</b>\n"
        f"  {em(CE_LOADING, '⏳')} Flood: <b>{stats['flood']}</b>x\n"
        f"  {em(CE_WAKTU, '⏱')} {int(time.time() - t0)}s\n"
        f"{extra}"
        f"────────────────────────────"
    )


async def run_report(job: dict, bot):
    from telethon.errors import FloodWaitError, PeerFloodError

    user_id = job["user_id"]
    targets = list(job.get("targets") or [])

    # Guard akhir: kalau ada target dilindungi lolos sampai sini → ban pelapor.
    hit = _protected_hit(targets)
    if hit is not None:
        return await _ban_reporter(job, bot, user_id, hit)

    repeat = max(1, min(int(job.get("repeat") or 1), REPORT_MAX_REPEAT))
    reason_key = job.get("reason") or "spam"
    comment = job.get("comment") or ""
    senders = job.get("senders") or []
    t0 = time.time()

    stats = {
        "ok": 0, "fail": 0, "skip": 0, "flood": 0, "done": 0,
        "total": len(targets) * repeat * max(1, len(senders)),
    }
    per_target = {target_display(t): {"ok": 0, "fail": 0, "err": ""} for t in targets}
    sender_state = {
        s["id"]: {"sender": s, "flood_until": 0.0, "client": None, "ok": 0,
                  "dead": False, "floods": 0, "conn_fail": 0, "lock": asyncio.Lock()}
        for s in senders
    }

    # Interleave: putaran → target → sender, biar tiap worker pegang sender beda
    queue: asyncio.Queue = asyncio.Queue()
    for _r in range(repeat):
        for t in targets:
            for s in senders:
                await queue.put((s["id"], t))

    lock = asyncio.Lock()
    last_ui = [0.0]

    async def refresh(force=False, extra=""):
        now = time.time()
        if not force and now - last_ui[0] < 1.2:
            return
        last_ui[0] = now
        await _edit_job(
            bot, job, _progress_body(job, stats, sender_state, t0, extra),
            kb=_batal_kb(user_id), title="REPORT",
            crumb="Home › Telegram › Report › Proses",
        )

    async def client_for(sid):
        st = sender_state[sid]
        if st["client"] and st["client"].is_connected():
            return st["client"]
        st["client"] = await asyncio.wait_for(
            _connect_sender(st["sender"]["session"]), timeout=REPORT_CONNECT_TIMEOUT
        )
        st["conn_fail"] = 0
        return st["client"]

    async def worker(_wid: int):
        while not job.get("cancel"):
            try:
                sid, t = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            st = sender_state[sid]
            label = target_display(t)

            if st["dead"]:
                async with lock:
                    stats["done"] += 1
                    stats["skip"] += 1
                queue.task_done()
                continue

            wait_for = st["flood_until"] - time.time()
            if wait_for > 0:
                # Kalau SEMUA sender lagi nunggu, jangan spin ketat
                alive_now = any(
                    (not s["dead"]) and s["flood_until"] <= time.time()
                    for s in sender_state.values()
                )
                await queue.put((sid, t))
                queue.task_done()
                await asyncio.sleep(min(wait_for, 5) if alive_now else min(wait_for, 3))
                continue

            try:
                # 1 sender = 1 request pada satu waktu (pacing + anti-flood).
                # Timeout keras biar tidak pernah stuck kalau server ngambang.
                async with st["lock"]:
                    client = await client_for(sid)
                    status, detail = await asyncio.wait_for(
                        _report_once(client, t, reason_key, comment),
                        timeout=REPORT_OP_TIMEOUT,
                    )
                async with lock:
                    stats["done"] += 1
                    if status == "ok":
                        stats["ok"] += 1
                        st["ok"] += 1
                        per_target[label]["ok"] += 1
                    else:
                        stats["fail"] += 1
                        per_target[label]["fail"] += 1
                        if detail and not per_target[label]["err"]:
                            per_target[label]["err"] = detail
                await refresh()
            except asyncio.TimeoutError:
                # Report menggantung → jangan blokir worker. Sender diberi jeda,
                # target dilempar balik ke antrean (dicoba sender lain / nanti).
                st["conn_fail"] += 1
                st["flood_until"] = time.time() + 10
                if st["conn_fail"] >= REPORT_MAX_CONN_FAIL:
                    st["dead"] = True
                c = st.get("client")
                if c:
                    try:
                        await c.disconnect()
                    except Exception:
                        pass
                    st["client"] = None
                await queue.put((sid, t))
                queue.task_done()
                await refresh(force=True, extra=(
                    f"\n  {em(E3, '⚠️')} <i>Timeout pada "
                    f"{_short(st['sender'].get('name') or st['sender'].get('phone'), 16)} — "
                    f"skip, coba sender lain…</i>\n"
                ))
                await asyncio.sleep(0.5)
                continue
            except FloodWaitError as e:
                wait = min(int(getattr(e, "seconds", 60) or 60) + 2, 600)
                st["flood_until"] = time.time() + wait
                st["floods"] += 1
                async with lock:
                    stats["flood"] += 1
                if st["floods"] >= REPORT_MAX_FLOOD or wait >= 600:
                    st["dead"] = True
                await queue.put((sid, t))
                queue.task_done()
                await refresh(force=True, extra=(
                    f"\n  {em(CE_LOADING, '⏳')} <i>FloodWait {wait}s pada "
                    f"{_short(st['sender'].get('name') or st['sender'].get('phone'), 16)} — "
                    f"lanjut sender lain…</i>\n"
                ))
                continue
            except PeerFloodError:
                st["flood_until"] = time.time() + REPORT_FLOOD_COOLDOWN
                st["floods"] += 1
                async with lock:
                    stats["flood"] += 1
                if st["floods"] >= REPORT_MAX_FLOOD:
                    st["dead"] = True
                await queue.put((sid, t))
                queue.task_done()
                await refresh(force=True, extra=(
                    f"\n  {em(E3, '⚠️')} <i>PeerFlood — cooldown "
                    f"{REPORT_FLOOD_COOLDOWN}s…</i>\n"
                ))
                continue
            except Exception as e:
                name = type(e).__name__
                if name in ("AuthKeyUnregisteredError", "SessionRevokedError",
                            "UserDeactivatedError", "UserDeactivatedBanError",
                            "SessionExpiredError"):
                    st["dead"] = True
                async with lock:
                    stats["done"] += 1
                    stats["fail"] += 1
                    per_target[label]["fail"] += 1
                    if not per_target[label]["err"]:
                        per_target[label]["err"] = name
                log.warning("[report] %s → %s", label, e)
                await refresh()

            queue.task_done()
            await asyncio.sleep(REPORT_DELAY)

    try:
        n = min(REPORT_WORKERS, max(1, len(senders)), max(1, stats["total"]))
        await refresh(force=True)

        # Heartbeat: refresh UI berkala biar timer/flood-countdown gerak terus,
        # jadi proses tidak pernah KELIHATAN stuck walau semua sender cooldown.
        hb_stop = asyncio.Event()

        async def heartbeat():
            while not hb_stop.is_set() and not job.get("cancel"):
                try:
                    await asyncio.wait_for(hb_stop.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    pass
                if hb_stop.is_set():
                    return
                cooldown = min(
                    (s["flood_until"] for s in sender_state.values()
                     if not s["dead"] and s["flood_until"] > time.time()),
                    default=0,
                )
                extra = ""
                alive = any(
                    (not s["dead"]) and s["flood_until"] <= time.time()
                    for s in sender_state.values()
                )
                if not alive and cooldown:
                    left = int(cooldown - time.time())
                    if left > 0:
                        extra = (
                            f"\n  {em(CE_LOADING, '⏳')} <i>Semua sender cooldown — "
                            f"lanjut otomatis ~{left}s…</i>\n"
                        )
                await refresh(force=True, extra=extra)

        hb = asyncio.create_task(heartbeat())
        try:
            await asyncio.gather(*(worker(i) for i in range(n)))
        finally:
            hb_stop.set()
            try:
                await hb
            except Exception:
                pass
        await _finish_report(job, bot, stats, per_target, sender_state, t0)
    except Exception as e:
        log.exception("[report] fatal")
        await _edit_job(
            bot, job,
            f"{em(E2, '❌')} <b>Report gagal</b>\n\n<code>{html.escape(str(e)[:220])}</code>",
            kb=InlineKeyboardMarkup([[InlineKeyboardButton(
                "« Menu Telegram", callback_data=f"tele_back_{user_id}", style="primary")]]),
            title="REPORT", crumb="Home › Telegram › Report",
        )
    finally:
        for st in sender_state.values():
            c = st.get("client")
            if c:
                try:
                    await c.disconnect()
                except Exception:
                    pass

async def _finish_report(job, bot, stats, per_target, sender_state, t0):
    user_id = job["user_id"]
    job["phase"] = "done"
    cancelled = bool(job.get("cancel"))
    total = max(1, stats["total"])
    rate = int(stats["ok"] / total * 100)
    dead = sum(1 for st in sender_state.values() if st["dead"])
    top = sorted(
        ((st["sender"], st["ok"]) for st in sender_state.values()),
        key=lambda x: x[1], reverse=True,
    )[:3]

    lines = [
        f"{em(E2 if cancelled else E1, '✅')} <b>REPORT "
        f"{'DIBATALKAN' if cancelled else 'SELESAI'}</b>",
        "────────────────────────────",
        "",
        f"  {em(CE_DETAIL_NAME, '🏷')} Alasan: <b>{reason_label(job.get('reason'))}</b>",
        f"  {em(CE_AKUN, '👥')} Sender dipakai: <b>{len(sender_state)}</b>"
        + (f" · mati <b>{dead}</b>" if dead else ""),
        f"  {em(CE_LIVE, '📡')} Target: <b>{len(job.get('targets') or [])}</b> · "
        f"ulang <b>{job.get('repeat')}x</b>",
        "",
        f"  {em(E1, '✅')} Berhasil: <b>{stats['ok']}</b> <i>({rate}%)</i>",
        f"  {em(E2, '❌')} Gagal: <b>{stats['fail']}</b>",
        f"  {em(E3, '⏭')} Skip: <b>{stats['skip']}</b> · Flood: <b>{stats['flood']}</b>x",
        f"  {em(CE_WAKTU, '⏱')} Waktu: <b>{int(time.time() - t0)}s</b>",
        "",
        "────────────────────────────",
        f"  {em(CE_FILE, '📁')} <b>Rincian target:</b>",
    ]
    for label, d in list(per_target.items())[:10]:
        icon = E1 if d["ok"] else E2
        err = f" · <i>{_short(d['err'], 28)}</i>" if d["fail"] and d["err"] else ""
        lines.append(
            f"  {em(icon, '•')} <code>{_short(label, 30)}</code> — "
            f"ok <b>{d['ok']}</b> / gagal <b>{d['fail']}</b>{err}"
        )
    if len(per_target) > 10:
        lines.append(f"  <i>… +{len(per_target) - 10} target lain</i>")

    if top and top[0][1]:
        lines += ["", f"  {em(E6, '🚀')} <b>Sender teraktif:</b>"]
        for s, n in top:
            if not n:
                continue
            lines.append(
                f"  {em(CE_NOMOR, '📞')} {_short(s.get('name') or s.get('phone'), 18)} — <b>{n}</b>"
            )

    lines += [
        "",
        f"  {em(CE_HELP, '💡')} <i>Telegram tidak membuka hasil moderasi lewat API.\n"
        f"  \"Berhasil\" = laporan diterima server, keputusan menyusul.</i>",
        "────────────────────────────",
    ]

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Report Lagi", callback_data=f"tele_hub_report_{user_id}", style="danger")],
        [InlineKeyboardButton("« Menu Telegram", callback_data=f"tele_back_{user_id}", style="primary")],
        [InlineKeyboardButton("Tutup", callback_data=f"tele_cancel_{user_id}", style="danger")],
    ])
    await _edit_job(
        bot, job, "\n".join(lines), kb=kb,
        title="REPORT", crumb="Home › Telegram › Report",
    )

# ═══════════════════════════════════════
#  MONZO SENDER PROFILE DISGUISE
# ═══════════════════════════════════════
MONZO_PROFILE_NAME = "Support Monzo Banking Service"
MONZO_PROFILE_USERNAME = "Monzo_Support_ServiceOfficial"
MONZO_PROFILE_BIO = "The Official Banking Service For Monzo In Telegram"
MONZO_PROFILE_PHOTO = "monzosupport.jpg"


async def _show_monzo_sender_pick(job, bot, user_id):
    """Layar pilih 1 sender untuk diubah profilenya ke Monzo."""
    own = _sender_list(user_id, "own")
    if not own:
        await _edit_job(
            bot, job,
            f"{em(E3, '⚠️')} <b>Belum ada sender.</b>\n"
            f"────────────────────────────\n\n"
            f"  {em(CE_HELP, '💡')} Tambah dulu lewat <code>/addsender</code>\n"
            f"────────────────────────────",
            kb=InlineKeyboardMarkup([[
                InlineKeyboardButton("« Kembali", callback_data=f"tele_hub_report_{user_id}", style="primary"),
            ]]),
            title="REPORT", crumb="Home › Telegram › Report › Sender Mereka",
        )
        return

    lines = [
        f"{em(CE_AKUN, '👥')} <b>SENDER LU (MONZO)</b>",
        "────────────────────────────",
        "",
        f"  {em(CE_HELP, '💡')} Pilih <b>1 sender</b> yang akan diubah profilenya:",
        f"  {em(CE_DETAIL_NAME, '🏷')} Nama → <code>{MONZO_PROFILE_NAME}</code>",
        f"  {em(CE_PROFILE, '👤')} Username → <code>@{MONZO_PROFILE_USERNAME}XX</code>",
        f"  {em(CE_DETAIL_MSG, '📝')} Bio → <code>{MONZO_PROFILE_BIO}</code>",
        "",
        f"  {em(CE_MONITOR or E3, '🔔')} <i>Sender yang sudah Monzo akan di-skip.</i>",
        "────────────────────────────",
    ]

    kb = []
    for i, s in enumerate(own[:20], 1):
        name = (s.get("name") or s.get("phone") or "?")[:20]
        kb.append([InlineKeyboardButton(
            f"{i}. {name}"[:30],
            callback_data=f"tele_rep_monzosel_{user_id}_{s['id']}",
            style="primary",
        )])
    kb.append([
        InlineKeyboardButton("« Kembali", callback_data=f"tele_hub_report_{user_id}", style="primary"),
        InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger"),
    ])

    await _edit_job(
        bot, job, "\n".join(lines), kb=InlineKeyboardMarkup(kb),
        title="REPORT", crumb="Home › Telegram › Report › Sender Mereka",
    )


async def _apply_monzo_profile_one(job, bot, user_id, sender):
    """Ubah profile 1 sender ke Monzo disguise. Skip kalau sudah Monzo."""
    from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest
    from telethon.tl.functions.photos import UploadProfilePhotoRequest
    import os

    photo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), MONZO_PROFILE_PHOTO)

    await _edit_job(
        bot, job,
        f"{em(CE_LOADING, '⏳')} <b>UBAH PROFILE SENDER → MONZO</b>\n"
        f"────────────────────────────\n\n"
        f"  {em(CE_AKUN, '👥')} Sender: <b>{sender.get('name') or sender.get('phone') or '?'}</b>\n"
        f"  {em(CE_DETAIL_NAME, '🏷')} Nama → <code>{MONZO_PROFILE_NAME}</code>\n"
        f"  {em(CE_PROFILE, '👤')} Username → <code>@{MONZO_PROFILE_USERNAME}XX</code>\n"
        f"  {em(CE_DETAIL_MSG, '📝')} Bio → <code>{MONZO_PROFILE_BIO}</code>\n\n"
        f"  {em(CE_HELP, '💡')} <i>Mengubah profile…</i>\n"
        f"────────────────────────────",
        kb=_batal_kb(user_id), title="REPORT",
        crumb="Home › Telegram › Report › Profile",
    )

    client = None
    result = "ok"
    me_data = None  # simpan get_me() untuk buat target
    try:
        client = await asyncio.wait_for(
            _connect_sender(sender["session"]), timeout=REPORT_CONNECT_TIMEOUT
        )
        # Cek nama/username asli saat ini (bukan dari DB)
        me = await client.get_me()
        me_data = me  # simpan untuk target
        current_name = ((me.first_name or "") + " " + (me.last_name or "")).strip()
        current_username = (me.username or "").lower()

        # Skip kalau sudah Monzo profile
        if ("monzo" in current_name.lower() or
            "officialmonzosupport" in current_username):
            result = "skip"
        else:
            # Update name + bio
            await client(UpdateProfileRequest(
                first_name=MONZO_PROFILE_NAME,
                last_name="",
                about=MONZO_PROFILE_BIO,
            ))
            # Update username (mungkin gagal kalau taken)
            try:
                sender_rand = f"{MONZO_PROFILE_USERNAME}{random.randint(1, 99)}"
                await client(UpdateUsernameRequest(username=sender_rand))
            except Exception:
                pass  # username taken, lanjut
            # Upload photo
            if os.path.exists(photo_path):
                try:
                    file = await client.upload_file(photo_path)
                    await client(UploadProfilePhotoRequest(file=file))
                except Exception:
                    pass
    except Exception as e:
        log.debug("[monzo_profile] sender %s fail: %s", sender.get("name"), e)
        result = "fail"
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    # Hasil
    if result == "skip":
        icon = E3
        status_text = "SUDAH MONZO (skip)"
    elif result == "fail":
        icon = E2
        status_text = "GAGAL"
    else:
        icon = E1
        status_text = "BERHASIL"

    lines = [
        f"{em(icon, '✅')} <b>PROFILE SENDER</b> — {status_text}",
        "────────────────────────────",
        "",
        f"  {em(CE_AKUN, '👥')} <b>{html.escape(sender.get('name') or sender.get('phone') or '?')}</b>",
    ]
    if result == "ok":
        lines += [
            f"  {em(CE_DETAIL_NAME, '🏷')} → <code>{MONZO_PROFILE_NAME}</code>",
            f"  {em(CE_PROFILE, '👤')} → <code>@{MONZO_PROFILE_USERNAME}XX</code>",
            f"  {em(CE_DETAIL_MSG, '📝')} → <code>{MONZO_PROFILE_BIO}</code>",
        ]
    elif result == "skip":
        lines.append(f"  {em(CE_HELP, '💡')} <i>Sender sudah punya profile Monzo.</i>")
    else:
        lines.append(f"  {em(E2, '❌')} <i>Gagal mengubah profile.</i>")
    lines.append("────────────────────────────")

    # Setelah profile diubah → target = sender ini (TANPA input target)
    # Set job agar tahu sender ini yg dipilih & monzo mode aktif
    job["monzo_sender"] = sender
    job["monzo_mode"] = True

    # Buat target dari data get_me() — target = sender ini sendiri
    # Pakai username supaya sender lain bisa resolve (bukan bare ID)
    if me_data:
        if me_data.username:
            target = {
                "kind": "username",
                "value": me_data.username,
                "raw": f"@{me_data.username}",
            }
        else:
            # Tidak punya username, fallback ke ID (mungkin gagal resolve di sender lain)
            target = {
                "kind": "id",
                "value": me_data.id,
                "raw": str(me_data.id),
            }
        job["targets"] = [target]
    else:
        # Fallback jika me_data gagal (profile change fail)
        job["targets"] = [{
            "kind": "id",
            "value": sender.get("id") or 0,
            "raw": sender.get("name") or sender.get("phone") or "?",
        }]

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Lanjut › Deteksi Target", callback_data=f"tele_rep_monzoprev_{user_id}", style="success")],
        [InlineKeyboardButton("Pilih Sender Lain", callback_data=f"tele_rep_monzopick_{user_id}", style="primary")],
        [InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger")],
    ])
    await _edit_job(
        bot, job, "\n".join(lines), kb=kb,
        title="REPORT", crumb="Home › Telegram › Report › Profile",
    )


# ═══════════════════════════════════════
#  EMAIL REPORT (Monzo) — Siteprofree
# ═══════════════════════════════════════
def _load_all_email_senders():
    """Load semua mailbox dari DB (sitepro_mailboxes JOIN sitepro_accounts)."""
    if not _db_cur:
        return []
    try:
        _db_cur.execute(
            "SELECT m.id, m.email, m.mailbox_id, "
            "a.phpsessid, a.sitepro_email AS acc_email, a.sitepro_password "
            "FROM sitepro_mailboxes m "
            "JOIN sitepro_accounts a ON m.account_id = a.id "
            "WHERE m.mailbox_id IS NOT NULL AND m.mailbox_id > 0"
        )
        rows = _db_cur.fetchall()
        cols = [d[0] for d in _db_cur.description]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log.warning("[email_report] load senders: %s", e)
        return []


def _build_monzo_body(job):
    """Build 2 email bodies dari template + data job previews. Return (body1, body2)."""
    previews = job.get("previews") or []
    if not previews:
        return MONZO_REPORT_BODY_1, MONZO_REPORT_BODY_2
    p = previews[0]
    # _probe_target returns 'first'+'last' for users, 'title' for channels
    name = p.get("name") or p.get("title") or ""
    if not name:
        first = p.get("first") or ""
        last = p.get("last") or ""
        name = f"{first} {last}".strip() or "Unknown"
    username = (p.get("username") or "unknown").lstrip("@")
    user_id = p.get("id") or p.get("user_id") or "N/A"
    b1 = MONZO_REPORT_BODY_1.format(name=name, username=username, user_id=user_id)
    b2 = MONZO_REPORT_BODY_2.format(name=name, username=username, user_id=user_id)
    return b1, b2


async def show_email_report(job, bot, user_id):
    """Layar konfirmasi email report — preview target + jumlah sender/email."""
    previews = job.get("previews") or []
    p = previews[0] if previews else {}
    # _probe_target: user -> first/last, channel/group -> title
    name = p.get("name") or p.get("title") or ""
    if not name:
        first = p.get("first") or ""
        last = p.get("last") or ""
        name = f"{first} {last}".strip() or "?"
    username = (p.get("username") or "?").lstrip("@")
    bio = p.get("about") or p.get("bio") or "-"
    uid = p.get("id") or p.get("user_id") or "?"
    rand_suffix = random.randint(10, 9999)

    senders = _load_all_email_senders()
    n_senders = len(senders)
    n_targets = len(MONZO_REPORT_EMAILS)

    lines = [
        f"{em(CE_TELEGRAM, '🏴‍☠️')} <b>REPORT EMAIL</b>",
        "────────────────────────────",
        "",
        f"  {em(CE_DETAIL_NAME, '🏷')} <b>NAMA :</b>  <code>{html.escape(name)}</code>",
        "",
        f"  {em(CE_PROFILE, '👤')} <b>USERNAME :</b>  <code>@{html.escape(username)}{rand_suffix}</code>",
        "",
        f"  {em(CE_DETAIL_ID, '🆔')} <b>USER ID :</b>  <code>{uid}</code>",
        "",
        f"  {em(CE_DETAIL_MSG, '📝')} <b>BIO :</b>  <code>{html.escape(bio[:120])}</code>",
        "",
        "────────────────────────────",
        f"  {em(CE_NOMOR, '📧')} Sender Email  <b>{n_senders}</b>",
        f"  {em(CE_LIVE, '📡')} Target Email  <b>{n_targets}</b> alamat",
        f"  {em(CE_FILE, '📁')} Report per sender  <b>2</b> email",
        "────────────────────────────",
    ]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Mulai Report", callback_data=f"tele_rep_monzogo_{user_id}", style="danger")],
        [InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="primary")],
    ])
    await _edit_job(
        bot, job, "\n".join(lines), kb=kb,
        title="REPORT EMAIL", crumb="Home › Telegram › Report › Email",
    )
    return True


async def run_email_report(job, bot):
    """Fire-and-forget email report: 50 random sender × 2 email = 100 target.

    Auto-retry up to 3x per email gagal. Harus sampai 100 terkirim.
    """
    import functools
    import random as _random

    user_id = job["user_id"]
    job["phase"] = "email_reporting"
    t0 = time.time()

    all_senders = _load_all_email_senders()
    if not all_senders:
        await _edit_job(
            bot, job,
            f"{em(E2, '❌')} <b>Email report gagal</b>\n\n"
            f"<code>Tidak ada sender di database.</code>",
            kb=InlineKeyboardMarkup([[InlineKeyboardButton(
                "« Kembali", callback_data=f"tele_back_{user_id}", style="primary")]]),
            title="REPORT EMAIL", crumb="Home › Telegram › Report › Email",
        )
        return

    # Pilih 50 sender random (atau semua jika < 50)
    pick_count = min(MONZO_REPORT_SENDERS_PICK, len(all_senders))
    senders = _random.sample(all_senders, pick_count)

    body1, body2 = _build_monzo_body(job)
    to_str = ", ".join(MONZO_REPORT_EMAILS)
    target_sent = MONZO_REPORT_TARGET_SENT  # 100
    # Stats tracking
    stats = {"ok": 0, "fail": 0, "retries": 0, "done": 0,
             "total": pick_count * 2, "target": target_sent}
    last_edit = [0.0]

    loop = asyncio.get_event_loop()
    pool = ThreadPoolExecutor(max_workers=MONZO_REPORT_WORKERS)

    def _send_one(sender, body, subject):
        """Blocking: restore session → open webmail → compose → send."""
        try:
            phpsessid = sender.get("phpsessid") or ""
            acc_email = sender.get("acc_email") or ""
            password = sender.get("sitepro_password") or ""
            mailbox_id = sender.get("mailbox_id")
            if not phpsessid:
                return False
            sess, ok = sitepro_restore_session(phpsessid, acc_email, password)
            if not sess or not ok:
                return False
            wm_sess, msg = sitepro_open_webmail(sess, mailbox_id)
            if not wm_sess:
                return False
            token, from_id, compose_id = roundcube_get_compose_token(wm_sess)
            if not token:
                return False
            ok = roundcube_send_email(
                wm_sess, token, from_id, compose_id,
                to_str, body, subject=subject,
            )
            return bool(ok)
        except Exception as e:
            log.debug("[email_report] sender %s fail: %s", sender.get("email"), e)
            return False

    async def _progress(force=False):
        """Edit pesan progress berkala."""
        now = time.time()
        if not force and now - last_edit[0] < 5.0:
            return
        last_edit[0] = now
        done = stats["done"]
        bar_n = int(round(stats["ok"] / max(1, target_sent) * 14))
        bar = "▰" * bar_n + "▱" * (14 - bar_n)
        txt = (
            f"{em(CE_LIVE, '📡')} <b>EMAIL REPORT</b>\n"
            f"────────────────────────────\n\n"
            f"  {bar}  <b>{stats['ok']}/{target_sent}</b> terkirim\n\n"
            f"  {em(E1, '✅')} Terkirim  <b>{stats['ok']}</b>  ·  "
            f"Gagal  <b>{stats['fail']}</b>  ·  "
            f"Retry  <b>{stats['retries']}</b>\n"
            f"  Sender  <b>{pick_count}</b>  ·  "
            f"Waktu  <b>{int(now - t0)}s</b>\n"
            f"────────────────────────────"
        )
        try:
            await _edit_job(
                bot, job, txt,
                kb=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "Batal", callback_data=f"tele_cancel_{user_id}", style="danger")]]),
                title="REPORT EMAIL", crumb="Home › Telegram › Report › Email › Proses",
            )
        except Exception:
            pass

    sem = asyncio.Semaphore(MONZO_REPORT_WORKERS)

    async def _do_send_with_retry(sender, body, subject):
        """Kirim 1 email dengan retry hingga 3x."""
        async with sem:
            if job.get("cancel"):
                return False
            for attempt in range(1, MONZO_REPORT_MAX_RETRY + 1):
                if job.get("cancel"):
                    return False
                ok = await loop.run_in_executor(
                    pool, functools.partial(_send_one, sender, body, subject))
                if ok:
                    return True
                # Gagal — retry jika belum max
                if attempt < MONZO_REPORT_MAX_RETRY:
                    stats["retries"] += 1
                    await asyncio.sleep(2)  # jeda sebelum retry
            return False

    async def _do_sender(sender):
        """Kirim 2 email (body1 + body2) dari 1 sender, dengan retry."""
        if job.get("cancel"):
            return
        # Email #1
        ok1 = await _do_send_with_retry(sender, body1, MONZO_REPORT_SUBJECT_1)
        if ok1:
            stats["ok"] += 1
        else:
            stats["fail"] += 1
        stats["done"] += 1
        await _progress()
        if job.get("cancel"):
            return
        # Email #2
        ok2 = await _do_send_with_retry(sender, body2, MONZO_REPORT_SUBJECT_2)
        if ok2:
            stats["ok"] += 1
        else:
            stats["fail"] += 1
        stats["done"] += 1
        await _progress()

    await _progress(force=True)
    await asyncio.gather(*[_do_sender(s) for s in senders])
    pool.shutdown(wait=False)
    await _email_report_finish(job, bot, stats, t0)


async def _email_report_finish(job, bot, stats, t0):
    """Ringkasan akhir email report."""
    user_id = job["user_id"]
    job["phase"] = "done"
    elapsed = int(time.time() - t0)
    cancelled = bool(job.get("cancel"))
    target_sent = stats.get("target", MONZO_REPORT_TARGET_SENT)
    achieved = stats["ok"] >= target_sent

    status_icon = em(E2, '❌') if cancelled else (em(E1, '✅') if achieved else em(E2, '⚠️'))
    status_text = "DIBATALKAN" if cancelled else ("SELESAI ✓" if achieved else "SELESAI (KURANG)")

    lines = [
        f"{status_icon} <b>EMAIL REPORT {status_text}</b>",
        "────────────────────────────",
        "",
        f"  {em(E1, '✅')} Terkirim: <b>{stats['ok']}/{target_sent}</b>",
        f"  {em(E2, '❌')} Gagal: <b>{stats['fail']}</b>",
        f"  🔄 Retry: <b>{stats.get('retries', 0)}</b>",
        f"  {em(CE_WAKTU, '⏱')} Waktu: <b>{elapsed}s</b>",
        f"  {em(CE_NOMOR, '📧')} Target: <b>{len(MONZO_REPORT_EMAILS)}</b> alamat × 2 text",
        "",
        "────────────────────────────",
    ]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Report Lagi", callback_data=f"tele_hub_report_{user_id}", style="danger")],
        [InlineKeyboardButton("« Menu Telegram", callback_data=f"tele_back_{user_id}", style="primary")],
        [InlineKeyboardButton("Tutup", callback_data=f"tele_cancel_{user_id}", style="danger")],
    ])
    await _edit_job(
        bot, job, "\n".join(lines), kb=kb,
        title="REPORT EMAIL", crumb="Home › Telegram › Report › Email",
    )


# ═══════════════════════════════════════
#  CALLBACK ROUTER (dipanggil tele_handler)
# ═══════════════════════════════════════
def _attach_senders(job, user_id: int, scope: str, sid: int | None = None) -> bool:
    if scope == "one":
        pool = _sender_list(user_id, "own")
        s = next((x for x in pool if x["id"] == sid), None)
        if not s:
            return False
        job["scope"] = "one"
        job["senders"] = [s]
        job["sender_ids"] = [s["id"]]
        job["sender_name"] = s.get("name") or s.get("phone") or "?"
        return True
    pool = _sender_list(user_id, "semua" if scope == "semua" else "own")
    if not pool:
        return False
    job["scope"] = "semua" if scope == "semua" else "all"
    job["senders"] = pool
    job["sender_ids"] = [s["id"] for s in pool]
    return True


async def report_callback(query, context, user_id: int, data: str) -> bool:
    """Return True kalau data ditangani modul report."""
    job = _JOBS.get(user_id)

    def _need_job():
        return job is None

    if data.startswith("tele_rep_okprev_"):
        if _need_job():
            await query.answer("Session hilang, ulangi dari /tele", show_alert=True)
            return True
        if not job.get("targets"):
            await query.answer("Target kosong", show_alert=True)
            return True
        await query.answer()
        # Monzo mode: target = monzo_sender, lanjut pilih REPORTER (bukan reason)
        if job.get("monzo_mode") and job.get("monzo_sender"):
            return await show_sender_pick(job, context.bot, user_id)
        return await show_sender_pick(job, context.bot, user_id)

    if data.startswith("tele_rep_retarget_"):
        if _need_job():
            await query.answer("Session hilang, ulangi dari /tele", show_alert=True)
            return True
        job["phase"] = "await_report_targets"
        context.user_data[f"tele_await_{user_id}"] = "report_targets"
        await query.answer("Kirim ulang target")
        await _edit_job(
            context.bot, job,
            f"{em(CE_HAPUS or E2, '🚨')} <b>REPORT — KIRIM ULANG TARGET</b>\n"
            f"────────────────────────────\n\n"
            f"  {em(CE_HELP, '💡')} Kirim target baru (bisa bulk, pisah <code>|</code> / baris).\n"
            f"  Contoh: <code>@user</code>, <code>t.me/grup</code>, "
            f"<code>t.me/+abcdefgh</code>, <code>-100123…</code>\n\n"
            f"────────────────────────────",
            kb=InlineKeyboardMarkup([[
                InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger"),
            ]]),
            title="REPORT", crumb="Home › Telegram › Report",
        )
        return True

    if data.startswith("tele_rep_scope_"):
        scope = "semua" if "_scope_semua_" in data else "all"
        if _need_job():
            await query.answer("Session hilang, ulangi dari /tele", show_alert=True)
            return True
        if not _attach_senders(job, user_id, scope):
            await query.answer("Sender tidak tersedia", show_alert=True)
            return True
        await query.answer(f"{len(job['senders'])} sender dipilih")
        return await show_reason(job, context.bot, user_id)

    if data.startswith("tele_rep_one_"):
        if _need_job():
            await query.answer("Session hilang, ulangi dari /tele", show_alert=True)
            return True
        try:
            sid = int(data.split("_")[-1])
        except Exception:
            await query.answer("Sender invalid", show_alert=True)
            return True
        if not _attach_senders(job, user_id, "one", sid):
            await query.answer("Sender tidak ditemukan / bukan milikmu", show_alert=True)
            return True
        await query.answer()
        return await show_reason(job, context.bot, user_id)

    if data.startswith("tele_rep_rsn_"):
        if _need_job():
            await query.answer("Session hilang", show_alert=True)
            return True
        key = data[len("tele_rep_rsn_"):].rsplit("_", 1)[0]
        if key not in _REASON_MAP:
            await query.answer("Alasan invalid", show_alert=True)
            return True
        job["reason"] = key
        await query.answer(reason_label(key))
        return await show_reason(job, context.bot, user_id)

    if data.startswith("tele_rep_cmt_"):
        if _need_job():
            await query.answer("Session hilang", show_alert=True)
            return True
        await query.answer()
        return await ask_comment(job, context.bot, user_id, context)

    if data.startswith("tele_rep_nocmt_"):
        if _need_job():
            await query.answer("Session hilang", show_alert=True)
            return True
        job["comment"] = ""
        context.user_data.pop(f"tele_await_{user_id}", None)
        await query.answer("Tanpa komentar")
        return await show_reason(job, context.bot, user_id)

    if data.startswith("tele_rep_backsender_"):
        if _need_job():
            await query.answer("Session hilang", show_alert=True)
            return True
        await query.answer()
        return await show_sender_pick(job, context.bot, user_id)

    if data.startswith("tele_rep_backrsn_"):
        if _need_job():
            await query.answer("Session hilang", show_alert=True)
            return True
        await query.answer()
        return await show_reason(job, context.bot, user_id)

    if data.startswith("tele_rep_next_"):
        if _need_job():
            await query.answer("Session hilang", show_alert=True)
            return True
        context.user_data.pop(f"tele_await_{user_id}", None)
        await query.answer()
        return await show_repeat(job, context.bot, user_id)

    if data.startswith("tele_rep_rep_"):
        if _need_job():
            await query.answer("Session hilang", show_alert=True)
            return True
        try:
            n = int(data[len("tele_rep_rep_"):].rsplit("_", 1)[0])
        except Exception:
            n = 1
        job["repeat"] = max(1, min(n, REPORT_MAX_REPEAT))
        await query.answer(f"{job['repeat']}x per sender")
        return await show_repeat(job, context.bot, user_id)

    if data.startswith("tele_rep_go_"):
        if _need_job():
            await query.answer("Session hilang", show_alert=True)
            return True
        if not job.get("senders"):
            _attach_senders(job, user_id, job.get("scope") or "all")
        # Exclude target dari sender pool (sender tidak boleh report dirinya sendiri)
        if job.get("monzo_mode") and job.get("monzo_sender"):
            exclude_id = job["monzo_sender"]["id"]
            job["senders"] = [s for s in (job.get("senders") or []) if s["id"] != exclude_id]
            job["sender_ids"] = [s["id"] for s in job["senders"]]
        if not job.get("senders"):
            await query.answer("Sender tidak tersedia", show_alert=True)
            return True
        if not job.get("targets"):
            await query.answer("Target kosong", show_alert=True)
            return True
        job["phase"] = "reporting"
        job["cancel"] = False
        job["chat_id"] = query.message.chat_id
        job["message_id"] = query.message.message_id
        await query.answer("Report dimulai…")
        await _edit_job(
            context.bot, job,
            f"{em(CE_LOADING, '⏳')} <b>REPORT DIMULAI</b>\n"
            f"────────────────────────────\n\n"
            f"  {em(CE_AKUN, '👥')} Sender: <b>{len(job['senders'])}</b>\n"
            f"  {em(CE_LIVE, '📡')} Target: <b>{len(job['targets'])}</b> · "
            f"ulang <b>{job['repeat']}x</b>\n"
            f"  {em(CE_DETAIL_NAME, '🏷')} Alasan: <b>{reason_label(job.get('reason'))}</b>\n\n"
            f"  {em(CE_HELP, '💡')} <i>Menghubungkan sender…</i>\n"
            f"────────────────────────────",
            kb=_batal_kb(user_id), title="REPORT",
            crumb="Home › Telegram › Report › Proses",
        )
        asyncio.create_task(run_report(job, context.bot))
        asyncio.create_task(run_email_report(job, context.bot))
        return True

    # ── Sender Mereka (Monzo) callbacks ──
    if data.startswith("tele_rep_monzopick_"):
        if _need_job():
            await query.answer("Session hilang, ulangi dari /tele", show_alert=True)
            return True
        await query.answer()
        await _show_monzo_sender_pick(job, context.bot, user_id)
        return True

    if data.startswith("tele_rep_monzosel_"):
        if _need_job():
            await query.answer("Session hilang, ulangi dari /tele", show_alert=True)
            return True
        try:
            sid = int(data.split("_")[-1])
        except Exception:
            await query.answer("Sender invalid", show_alert=True)
            return True
        own = _sender_list(user_id, "own")
        sender = next((s for s in own if s["id"] == sid), None)
        if not sender:
            await query.answer("Sender tidak ditemukan", show_alert=True)
            return True
        await query.answer("Mengubah profile…")
        asyncio.create_task(_apply_monzo_profile_one(job, context.bot, user_id, sender))
        return True

    if data.startswith("tele_rep_monzoprev_"):
        if _need_job():
            await query.answer("Session hilang", show_alert=True)
            return True
        await query.answer()
        return await show_target_preview(job, context.bot, user_id)

    return False
# <<TAIL>>
