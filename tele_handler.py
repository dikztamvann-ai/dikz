"""
Telegram Tools Hub — /tele
Style mirror of /wa: blockquote screen, custom emoji, inline styles.

Fitur:
  - Hub semua fitur Telegram (Check, Change, Sender, Add Sender, Extract, …)
  - Extract ID / Masukan Grup: extract member dari link grup (bulk),
    lalu invite ke grup di mana sender adalah admin.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from io import BytesIO
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

# ── Custom emoji (same IDs as dik.py) ─────────────────────────
CE_TELEGRAM = "5285350148451344065"
CE_NOMOR = "5422696450888842691"
CE_AKUN = "5256143829672672750"
CE_LOADING = "5256024382337205926"
CE_LIVE = "5870903672937911120"
CE_HELP = "5870570722778156940"
CE_LOGIN = "5355034377921244938"
CE_ADD = "5348028367138483442"
CE_BACK = "5449847653586188540"
CE_FILE = "5870570722778156940"
CE_DETAIL_ID = "5262690351969215936"
CE_DETAIL_NAME = "5316887736823591263"
CE_WAKTU = "5872756762347573066"
CE_EXPORT = "5258043150110301407"
CE_DETAIL_MSG = "5258500400918587241"   # ✍️ komentar / pesan
CE_HAPUS = "5870875489362513438"        # 🗑 report / hapus
CE_LINK = "5870527201874546272"         # 🔗 link target
CE_MONITOR = "5472239203590888751"      # 🔔 catatan
CE_PROFILE = "5870994129244131212"      # 👤 profil target
E1 = "5796205953913196373"
E2 = "5420323339723881652"
E3 = "4956337889593000947"
E6 = "6003769830564434518"

EXTRACT_WORKERS = 30
INVITE_BATCH = 1          # 1 user / request biar hitungan akurat
INVITE_DELAY = 3.0        # delay antar invite (1 request/user, tanpa verify)
INVITE_DELAY_MIN = 2.0
INVITE_DELAY_MAX = 60.0
FLOOD_COOLDOWN = 60       # tunggu saat PeerFlood, lalu lanjut
MAX_CONSEC_FLOOD = 6      # stop kalau flood beruntun (akun kena limit)
MAX_ADMIN_BUTTONS = 40

_get_pool_all = None

# Injected by dik.py
_screen = None
_em = None
_USER_ID = None
_get_all_addusers = None
_get_pool = None
_pool_count = None
_is_allowed = None
_telethon_ok = False
_TG_API_ID = None
_TG_API_HASH = None
_launch_check = None
_launch_change = None
_launch_sender = None
_launch_addsender_help = None
_launch_listele = None
_launch_sessions = None
_ban_user_fn = None      # dik._ban_user_from_bot → blacklist reporter dari bot

_tele_jobs = {}  # user_id -> job


def tele_init(
    screen_fn,
    em_fn,
    owner_id,
    get_addusers_fn,
    get_pool_fn,
    pool_count_fn,
    is_allowed_fn,
    telethon_ok,
    api_id,
    api_hash,
    launch_check=None,
    launch_change=None,
    launch_sender=None,
    launch_addsender_help=None,
    launch_listele=None,
    launch_sessions=None,
    get_pool_all_fn=None,
    ban_user_fn=None,
    db_cur=None,
    db_conn=None,
):
    global _screen, _em, _USER_ID, _get_all_addusers
    global _get_pool, _pool_count, _is_allowed, _telethon_ok
    global _TG_API_ID, _TG_API_HASH, _get_pool_all
    global _launch_check, _launch_change, _launch_sender
    global _launch_addsender_help, _launch_listele, _launch_sessions
    global _ban_user_fn
    global _db_cur, _db_conn
    _screen = screen_fn
    _em = em_fn
    _USER_ID = owner_id
    _get_all_addusers = get_addusers_fn
    _get_pool = get_pool_fn
    _pool_count = pool_count_fn
    _get_pool_all = get_pool_all_fn
    _is_allowed = is_allowed_fn
    _telethon_ok = bool(telethon_ok)
    _TG_API_ID = api_id
    _TG_API_HASH = api_hash
    _launch_check = launch_check
    _launch_change = launch_change
    _launch_sender = launch_sender
    _launch_addsender_help = launch_addsender_help
    _launch_listele = launch_listele
    _launch_sessions = launch_sessions
    _ban_user_fn = ban_user_fn
    _db_cur = db_cur
    _db_conn = db_conn
    _init_multi_module()


def _init_multi_module():
    try:
        from tele_multi import multi_init
        multi_init(
            _em=em,
            _screen=_screen,
            _batal_kb=_batal_kb,
            _edit_job=_edit_job,
            _connect_sender=_connect_sender,
            _list_admin_groups=_list_admin_groups,
            _invite_one=_invite_one,
            _get_member_count=_get_member_count,
            _entity_info=_entity_info,
            _get_pool=_get_pool,
            _get_pool_all=_get_pool_all,
            _USER_ID=_USER_ID,
            E1=E1, E2=E2, E3=E3, E6=E6,
            CE_AKUN=CE_AKUN, CE_LOADING=CE_LOADING, CE_LIVE=CE_LIVE,
            CE_HELP=CE_HELP, CE_DETAIL_NAME=CE_DETAIL_NAME, CE_DETAIL_ID=CE_DETAIL_ID,
            CE_TELEGRAM=CE_TELEGRAM, CE_WAKTU=CE_WAKTU, CE_FILE=CE_FILE,
        )
    except Exception as e:
        log.warning("tele_multi init later: %s", e)
    try:
        from tele_report import report_init
        report_init(
            _em=em,
            _screen=_screen,
            _batal_kb=_batal_kb,
            _edit_job=_edit_job,
            _connect_sender=_connect_sender,
            _entity_info=_entity_info,
            _get_pool=_get_pool,
            _get_pool_all=_get_pool_all,
            _USER_ID=_USER_ID,
            _JOBS=_tele_jobs,
            _ban_user=_ban_user_fn,
            _db_cur=_db_cur,
            _db_conn=_db_conn,
            E1=E1, E2=E2, E3=E3, E6=E6,
            CE_TELEGRAM=CE_TELEGRAM, CE_AKUN=CE_AKUN, CE_LOADING=CE_LOADING,
            CE_LIVE=CE_LIVE, CE_HELP=CE_HELP, CE_WAKTU=CE_WAKTU,
            CE_DETAIL_ID=CE_DETAIL_ID, CE_DETAIL_NAME=CE_DETAIL_NAME,
            CE_DETAIL_MSG=CE_DETAIL_MSG, CE_FILE=CE_FILE, CE_NOMOR=CE_NOMOR,
            CE_HAPUS=CE_HAPUS, CE_LINK=CE_LINK, CE_MONITOR=CE_MONITOR,
            CE_PROFILE=CE_PROFILE,
        )
    except Exception as e:
        log.warning("tele_report init later: %s", e)


def _is_authorized(user_id: int) -> bool:
    if _is_allowed and _is_allowed(user_id):
        return True
    if user_id == _USER_ID:
        return True
    try:
        return user_id in (_get_all_addusers() or [])
    except Exception:
        return False


def em(eid, fb="⭐"):
    if _em:
        return _em(eid, fb)
    return f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji>'


# ═══════════════════════════════════════
#  LINK PARSE
# ═══════════════════════════════════════
_LINK_SPLIT = re.compile(r"[|\n,;]+")
_TME = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/"
    r"(?:joinchat/|\+)?([A-Za-z0-9_\-/=]+)",
    re.I,
)


def parse_group_links(text: str) -> list[str]:
    """Parse bulk links: t.me/a|t.me/b or newlines. Return unique raw tokens."""
    raw = []
    for part in _LINK_SPLIT.split(text or ""):
        part = part.strip()
        if not part:
            continue
        m = _TME.search(part)
        if m:
            raw.append(part if part.startswith("http") or part.startswith("t.me") else f"t.me/{m.group(1)}")
        elif re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}", part):
            raw.append(part.lstrip("@"))
        elif re.fullmatch(r"-?\d{6,}", part):
            raw.append(part)
    # unique preserve order
    seen, out = set(), []
    for x in raw:
        key = x.lower().rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append(x)
    return out


def _normalize_invite(token: str):
    """Return ('username'|'invite'|'id', value)."""
    token = (token or "").strip()
    if re.fullmatch(r"-?\d{6,}", token):
        return "id", int(token)
    m = _TME.search(token)
    if m:
        path = m.group(1)
        full = token.lower()
        if "/+" in full or "joinchat" in full or token.strip().startswith("+"):
            return "invite", path.lstrip("+")
        # bare hash invite sometimes
        if re.fullmatch(r"[A-Za-z0-9_\-]{16,}", path) and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", path):
            # ambiguous; treat as username if looks like one else invite
            if path[0].isalpha() and "_" not in path[:1]:
                return "username", path
            return "invite", path
        return "username", path
    if token.startswith("+"):
        return "invite", token.lstrip("+")
    return "username", token.lstrip("@")


# ═══════════════════════════════════════
#  HUB UI
# ═══════════════════════════════════════
def _build_tele_hub(user_id: int):
    total, active = (0, 0)
    try:
        total, active = _pool_count(user_id)
    except Exception:
        pass

    body = (
        f"{em(CE_TELEGRAM, '💬')} <b>TELEGRAM TOOLS</b>\n"
        f"────────────────────────────\n\n"
        f"  {em(E1 if active else E3, '✅')} <b>Status:</b> "
        f"{'Siap' if active else 'Belum ada sender aktif'}\n"
        f"  {em(CE_AKUN, '👥')} <b>Sender pool:</b> <code>{total}</code> total · "
        f"<code>{active}</code> siap\n\n"
        f"────────────────────────────\n"
        f"Pilih fitur:"
    )

    kb = [
        [
            InlineKeyboardButton("Check", callback_data=f"tele_hub_check_{user_id}", style="primary"),
            InlineKeyboardButton("Change Nomor", callback_data=f"tele_hub_change_{user_id}", style="primary"),
        ],
        [
            InlineKeyboardButton("Sender Pool", callback_data=f"tele_hub_sender_{user_id}", style="primary"),
            InlineKeyboardButton("Add Sender", callback_data=f"tele_hub_addsender_{user_id}", style="primary"),
        ],
        [
            InlineKeyboardButton(
                "Extract ID / Masukan Grup",
                callback_data=f"tele_hub_extract_{user_id}",
                style="danger"),
            InlineKeyboardButton(
                "Auto Kick Member",
                callback_data=f"tele_hub_kick_{user_id}",
                style="danger"),
        ],
        [
            InlineKeyboardButton(
                "Report Acc / Group / Channel",
                callback_data=f"tele_hub_report_{user_id}",
                style="danger"),
        ],
    ]
    if user_id == _USER_ID:
        kb.append([
            InlineKeyboardButton("Sessions", callback_data=f"tele_hub_sessions_{user_id}", style="primary"),
        ])
    kb.append([InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger")])
    return body, kb


def _batal_kb(user_id: int, extra_rows=None):
    rows = list(extra_rows or [])
    rows.append([InlineKeyboardButton("⏹ Batal", callback_data=f"tele_stop_{user_id}", style="danger")])
    return InlineKeyboardMarkup(rows)


async def _safe_answer(query, text: str = "", *, alert: bool = False):
    """Jawaban callback SEGERA biar tombol tidak freeze."""
    try:
        await query.answer(text[:180] if text else None, show_alert=alert)
    except Exception:
        try:
            await query.answer()
        except Exception:
            pass


async def tele_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /tele — Telegram feature hub."""
    user_id = update.effective_user.id
    if not _is_authorized(user_id):
        await update.message.reply_text(
            _screen("TELEGRAM", f"{em(E2, '❌')} Kamu belum terdaftar. Hubungi owner."),
            parse_mode=ParseMode.HTML,
        )
        return
    if not _telethon_ok:
        await update.message.reply_text(
            _screen("TELEGRAM", f"{em(E2, '❌')} Telethon belum tersedia."),
            parse_mode=ParseMode.HTML,
        )
        return

    body, kb = _build_tele_hub(user_id)
    await update.message.reply_text(
        _screen("TELEGRAM", body, "Home › Telegram"),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def tele_open_hub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Buka hub /tele dari tombol inline (dipakai menu utama dik.py).

    Sama isinya dengan /tele, cuma di-render via edit_message_text supaya
    tombol TELEGRAM di menu utama langsung berubah jadi hub-nya.
    Return True kalau hub berhasil ditampilkan.
    """
    query = update.callback_query
    user_id = update.effective_user.id

    if not _is_authorized(user_id):
        await _safe_answer(query, "Kamu belum terdaftar. Hubungi owner.", alert=True)
        return False
    if not _telethon_ok:
        await _safe_answer(query, "Telethon belum tersedia.", alert=True)
        return False

    await _safe_answer(query)
    context.user_data.pop(f"tele_await_{user_id}", None)
    body, kb = _build_tele_hub(user_id)
    try:
        await query.edit_message_text(
            _screen("TELEGRAM", body, "Home › Telegram"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb),
        )
    except Exception:
        return False
    return True


# ═══════════════════════════════════════
#  SENDER PICK UI
# ═══════════════════════════════════════
def _sender_pick_kb(user_id: int, action: str, senders: list):
    kb, row = [], []
    for i, s in enumerate(senders[:24], 1):
        label = f"{i}. +{s.get('phone', '?')}"
        if s.get("name"):
            label = f"{i}. {(s['name'] or '')[:12]}"
        row.append(InlineKeyboardButton(
            label[:28],
            callback_data=f"tele_{action}_pick_{user_id}_{s['id']}",
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
    return InlineKeyboardMarkup(kb)


# ═══════════════════════════════════════
#  CALLBACK ROUTER
# ═══════════════════════════════════════
async def tele_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle tele_* callbacks. Return True if handled."""
    query = update.callback_query
    data = query.data or ""
    if not data.startswith("tele_"):
        return False

    user_id = update.effective_user.id
    # Kepemilikan tombol: callback_data selalu memuat ID pemiliknya.
    #   tele_*_dest_{uid}_{gid} / tele_rep_one_{uid}_{sid} → segmen ke-4
    #   ..._pick_{uid}_{sid} / ..._pri_{uid}_{sid}         → segmen ke-2 dari belakang
    #   sisanya                                            → segmen terakhir
    owner = None
    try:
        if data.startswith(("tele_ex_dest_", "tele_multi_dest_", "tele_kick_dest_", "tele_rep_one_", "tele_rep_monzosel_")):
            owner = int(data.split("_")[3])
        elif "_pick_" in data or "_pri_" in data:
            owner = int(data.split("_")[-2])
        else:
            owner = int(data.rsplit("_", 1)[-1])
    except Exception:
        owner = None

    # Gagal parse → tolak (jangan fallback ke penekan, itu membuka celah).
    # Owner pun tidak dikecualikan: job/state di-index per ID penekan, jadi
    # menekan tombol user lain akan mengenai data yang salah.
    if owner is None or owner != user_id:
        await _safe_answer(query, "⛔ Bukan milikmu", alert=True)
        return True

    if not _is_authorized(user_id):
        await _safe_answer(query, "Tidak diizinkan", alert=True)
        return True

    # ── STOP / CANCEL (jawab dulu biar tombol tidak stuck) ──
    if data.startswith("tele_stop_") or data.startswith("tele_cancel_"):
        job = _tele_jobs.get(user_id)
        running = job and job.get("phase") in (
            "extracting", "await_links", "list_admin", "inviting", "pick_dest",
            "multi_invite", "multi_pick_primary", "multi_pick_dest", "kicking",
            "reporting", "await_report_targets", "report_preview",
            "report_pick_sender", "report_pick_reason", "report_comment",
            "report_pick_repeat",
        )
        if running:
            job["cancel"] = True
            context.user_data.pop(f"tele_await_{user_id}", None)
            await _safe_answer(query, "⏹ Menghentikan…", alert=True)
            # Jangan edit di sini kalau invite/multi/kick/reporting — runner yang tampilkan ringkasan
            if job.get("phase") in ("inviting", "multi_invite", "extracting", "kicking", "list_admin", "reporting"):
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("⏹ Menghentikan…", callback_data="noop", style="danger")
                        ]])
                    )
                except Exception:
                    pass
                return True
        else:
            await _safe_answer(query, "Dibatalkan")
        job = _tele_jobs.pop(user_id, None)
        if job:
            job["cancel"] = True
        context.user_data.pop(f"tele_await_{user_id}", None)
        try:
            await query.edit_message_text(
                _screen("TELEGRAM", f"{em(E2, '❌')} <b>Dibatalkan.</b>", "Home › Telegram"),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return True

    if data.startswith("tele_back_"):
        await _safe_answer(query)
        context.user_data.pop(f"tele_await_{user_id}", None)
        body, kb = _build_tele_hub(user_id)
        try:
            await query.edit_message_text(
                _screen("TELEGRAM", body, "Home › Telegram"),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(kb),
            )
        except Exception:
            pass
        return True

    # ── hub feature buttons ──
    if data.startswith("tele_hub_check_"):
        await _safe_answer(query)
        context.user_data[f"tele_await_{user_id}"] = "check"
        body = (
            f"{em(CE_TELEGRAM, '💬')} <b>CHECK TELEGRAM</b>\n"
            f"────────────────────────────\n\n"
            f"  {em(CE_HELP, '💡')} Kirim nomor sekarang:\n"
            f"  • <code>628xxx 628yyy</code>\n"
            f"  • Atau reply file <code>.txt</code>\n"
            f"  • Atau ketik langsung <code>/check …</code>\n\n"
            f"────────────────────────────"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("« Kembali", callback_data=f"tele_back_{user_id}", style="primary")],
            [InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger")],
        ])
        await query.edit_message_text(
            _screen("CHECK", body, "Home › Telegram › Check"),
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return True

    if data.startswith("tele_hub_change_"):
        await query.answer()
        context.user_data[f"tele_await_{user_id}"] = "change"
        body = (
            f"{em(CE_TELEGRAM, '💬')} <b>CHANGE NOMOR</b>\n"
            f"────────────────────────────\n\n"
            f"  {em(CE_HELP, '💡')} Kirim nomor baru (max 10):\n"
            f"  • <code>628xxx</code>\n"
            f"  • Reply file <code>.txt</code>\n"
            f"  • Atau <code>/change …</code>\n\n"
            f"  {em(CE_LOGIN, '🔑')} OTP nanti: <code>6012:62542</code>\n\n"
            f"────────────────────────────"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("« Kembali", callback_data=f"tele_back_{user_id}", style="primary")],
            [InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger")],
        ])
        await query.edit_message_text(
            _screen("CHANGE", body, "Home › Telegram › Change"),
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return True

    if data.startswith("tele_hub_sender_"):
        await query.answer()
        if _launch_sender:
            await query.message.reply_text(  # keep hub, show sender below via launcher
                _screen("SENDER", f"{em(CE_LOADING, '⏳')} Membuka sender pool…"),
                parse_mode=ParseMode.HTML,
            )
            # Call launcher with a pseudo: just invoke sender_command on same update
            try:
                await _launch_sender(update, context)
            except Exception as e:
                await query.message.reply_text(
                    _screen("SENDER", f"{em(E2, '❌')} {html.escape(str(e)[:120])}"),
                    parse_mode=ParseMode.HTML,
                )
        return True

    if data.startswith("tele_hub_addsender_"):
        await query.answer()
        body = (
            f"{em(CE_ADD, '✨')} <b>ADD SENDER</b>\n"
            f"────────────────────────────\n\n"
            f"  Tambah akun Telegram ke pool milikmu.\n\n"
            f"  {em(CE_HELP, '💡')} Cara:\n"
            f"  <code>/addsender 628xxxxxxxxxx</code>\n\n"
            f"  Lalu kirim OTP &amp; 2FA jika diminta.\n\n"
            f"────────────────────────────"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("« Kembali", callback_data=f"tele_back_{user_id}", style="primary")],
            [InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger")],
        ])
        await query.edit_message_text(
            _screen("ADD SENDER", body, "Home › Telegram › Add Sender"),
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return True

    if data.startswith("tele_hub_listele_"):
        await query.answer()
        if user_id != _USER_ID:
            await query.answer("Owner only", show_alert=True)
            return True
        if _launch_listele:
            try:
                await _launch_listele(update, context)
            except Exception as e:
                await query.message.reply_text(str(e)[:200])
        return True

    if data.startswith("tele_hub_sessions_"):
        await query.answer()
        if user_id != _USER_ID:
            await query.answer("Owner only", show_alert=True)
            return True
        if _launch_sessions:
            try:
                await _launch_sessions(update, context)
            except Exception as e:
                await query.message.reply_text(str(e)[:200])
        return True

    if data.startswith("tele_hub_extract_"):
        await _safe_answer(query)
        return await _start_extract_pick_sender(query, user_id, context)

    if data.startswith("tele_hub_kick_"):
        await _safe_answer(query)
        from tele_multi import start_kick_pick_sender
        _init_multi_module()
        return await start_kick_pick_sender(query, user_id)

    if data.startswith("tele_hub_report_"):
        await _safe_answer(query)
        from tele_report import start_report
        _init_multi_module()
        return await start_report(query, context, user_id)

    if data.startswith("tele_rep_"):
        from tele_report import report_callback
        _init_multi_module()
        return await report_callback(query, context, user_id, data)

    # ── extract: pick sender ──
    if data.startswith("tele_extract_pick_"):
        sid = int(data.split("_")[-1])
        await _safe_answer(query)
        return await _extract_sender_chosen(query, context, user_id, sid)

    # ── SEMUA / ALL multi invite ──
    if data.startswith("tele_ex_semua_") or data.startswith("tele_ex_all_"):
        job = _tele_jobs.get(user_id)
        if not job or not job.get("members"):
            await _safe_answer(query, "Belum ada hasil extract", alert=True)
            return True
        owner_mode = data.startswith("tele_ex_semua_")
        if owner_mode and user_id != _USER_ID:
            await _safe_answer(query, "SEMUA khusus owner", alert=True)
            return True
        await _safe_answer(query)
        from tele_multi import start_multi_pick_primary
        _init_multi_module()
        return await start_multi_pick_primary(query, context, user_id, job, owner_mode)

    if data.startswith("tele_multi_pri_"):
        sid = int(data.split("_")[-1])
        await _safe_answer(query)
        job = _tele_jobs.get(user_id)
        if not job:
            await _safe_answer(query, "Session hilang", alert=True)
            return True
        from tele_multi import multi_primary_chosen
        _init_multi_module()
        return await multi_primary_chosen(query, context, user_id, job, sid)

    if data.startswith("tele_multi_dest_"):
        rest = data[len("tele_multi_dest_"):]
        _, _, chat_str = rest.partition("_")
        try:
            dest_id = int(chat_str)
        except Exception:
            await _safe_answer(query, "ID invalid", alert=True)
            return True
        job = _tele_jobs.get(user_id)
        if not job:
            await _safe_answer(query, "Session hilang", alert=True)
            return True
        from tele_multi import multi_dest_chosen
        _init_multi_module()
        return await multi_dest_chosen(query, context, user_id, job, dest_id)

    # ── Auto Kick ──
    if data.startswith("tele_kick_pick_"):
        sid = int(data.split("_")[-1])
        from tele_multi import kick_sender_chosen
        _init_multi_module()
        return await kick_sender_chosen(query, context, user_id, _tele_jobs, sid)

    if data.startswith("tele_kick_dest_"):
        rest = data[len("tele_kick_dest_"):]
        _, _, chat_str = rest.partition("_")
        try:
            dest_id = int(chat_str)
        except Exception:
            await _safe_answer(query, "ID invalid", alert=True)
            return True
        from tele_multi import kick_dest_chosen
        _init_multi_module()
        return await kick_dest_chosen(query, context, user_id, _tele_jobs, dest_id)

    # ── extract: pick destination admin group ──
    if data.startswith("tele_ex_dest_"):
        # tele_ex_dest_{uid}_{chatid}  chatid may be negative → split carefully
        rest = data[len("tele_ex_dest_"):]
        # rest = "{uid}_{chatid}"
        uid_str, _, chat_str = rest.partition("_")
        try:
            dest_id = int(chat_str)
        except Exception:
            await _safe_answer(query, "ID grup invalid", alert=True)
            return True
        await _safe_answer(query, "Memulai invite…")
        return await _start_invite(query, context, user_id, dest_id)

    if data.startswith("tele_ex_skip_"):
        await _safe_answer(query)
        job = _tele_jobs.get(user_id)
        if job:
            job["cancel"] = True
        context.user_data.pop(f"tele_await_{user_id}", None)
        body, kb = _build_tele_hub(user_id)
        await query.edit_message_text(
            _screen("TELEGRAM", body, "Home › Telegram"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return True

    if data.startswith("tele_ex_export_"):
        await _safe_answer(query, "Mengirim file…")
        job = _tele_jobs.get(user_id)
        if not job or not job.get("members"):
            await _safe_answer(query, "Belum ada hasil extract", alert=True)
            return True
        await _send_extract_file(query.message.chat_id, context, job)
        return True

    return False


async def _start_extract_pick_sender(query, user_id, context):
    senders = _get_pool(user_id) if _get_pool else []
    senders = [s for s in senders if s.get("status") != "dead" and s.get("session")]
    if not senders:
        body = (
            f"{em(E3, '⚠️')} <b>Belum ada sender.</b>\n\n"
            f"Tambah dulu: <code>/addsender 628xxx</code>\n"
            f"Setiap user hanya memakai sender miliknya sendiri."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("« Kembali", callback_data=f"tele_back_{user_id}", style="primary")],
        ])
        await query.edit_message_text(
            _screen("EXTRACT", body, "Home › Telegram › Extract"),
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return True

    body = (
        f"{em(CE_TELEGRAM, '💬')} <b>EXTRACT ID / MASUKAN GRUP</b>\n"
        f"────────────────────────────\n\n"
        f"  {em(CE_AKUN, '👥')} Pilih sender milikmu:\n"
        f"  {em(CE_HELP, '💡')} <i>Sender harus sudah join grup sumber,\n"
        f"  dan admin di grup tujuan.</i>\n\n"
        f"────────────────────────────"
    )
    await query.edit_message_text(
        _screen("EXTRACT", body, "Home › Telegram › Extract"),
        parse_mode=ParseMode.HTML,
        reply_markup=_sender_pick_kb(user_id, "extract", senders),
    )
    return True


async def _extract_sender_chosen(query, context, user_id, sid):
    senders = _get_pool(user_id) if _get_pool else []
    sender = next((s for s in senders if s["id"] == sid), None)
    if not sender or not sender.get("session"):
        await query.answer("Sender tidak ditemukan / bukan milikmu", show_alert=True)
        return True

    _tele_jobs[user_id] = {
        "user_id": user_id,
        "sender_id": sid,
        "session": sender["session"],
        "sender_phone": sender.get("phone", ""),
        "sender_name": sender.get("name") or sender.get("phone", ""),
        "phase": "await_links",
        "members": [],
        "sources": [],
        "admin_groups": [],
        "cancel": False,
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
    }
    context.user_data[f"tele_await_{user_id}"] = "extract_links"

    body = (
        f"{em(CE_TELEGRAM, '💬')} <b>EXTRACT ID</b>\n"
        f"────────────────────────────\n\n"
        f"  {em(CE_AKUN, '👤')} <b>Sender:</b> "
        f"<code>{html.escape(str(_tele_jobs[user_id]['sender_name'])[:24])}</code>\n"
        f"  {em(CE_NOMOR, '📞')} <code>+{html.escape(str(_tele_jobs[user_id]['sender_phone']))}</code>\n\n"
        f"  {em(CE_HELP, '💡')} Kirim link grup/channel sumber (bisa bulk):\n"
        f"  <code>t.me/grup1|t.me/grup2</code>\n"
        f"  <code>https://t.me/grup</code>\n"
        f"  <code>t.me/+inviteHash</code>\n\n"
        f"  {em(E6, '🚀')} Workers: <b>{EXTRACT_WORKERS}</b>\n\n"
        f"────────────────────────────"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("« Kembali", callback_data=f"tele_back_{user_id}", style="primary")],
        [InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger")],
    ])
    await query.edit_message_text(
        _screen("EXTRACT", body, "Home › Telegram › Extract"),
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return True


# ═══════════════════════════════════════
#  TEXT INPUT (from dik handle_message)
# ═══════════════════════════════════════
def is_tele_waiting(user_id: int, context) -> bool:
    return bool(context.user_data.get(f"tele_await_{user_id}"))


async def handle_tele_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if consumed."""
    user_id = update.effective_user.id
    mode = context.user_data.get(f"tele_await_{user_id}")
    if not mode:
        return False

    text = (update.message.text or "").strip()
    if text.lower() in {"batal", "/batal", "cancel"}:
        context.user_data.pop(f"tele_await_{user_id}", None)
        _tele_jobs.pop(user_id, None)
        await update.message.reply_text(
            _screen("TELEGRAM", f"{em(E2, '❌')} Dibatalkan."),
            parse_mode=ParseMode.HTML,
        )
        return True

    if mode == "check":
        context.user_data.pop(f"tele_await_{user_id}", None)
        if _launch_check:
            await _launch_check(update, context, text)
        else:
            await update.message.reply_text(
                f"Gunakan: <code>/check {html.escape(text[:80])}</code>",
                parse_mode=ParseMode.HTML,
            )
        return True

    if mode == "change":
        context.user_data.pop(f"tele_await_{user_id}", None)
        if _launch_change:
            await _launch_change(update, context, text)
        else:
            await update.message.reply_text(
                f"Gunakan: <code>/change {html.escape(text[:80])}</code>",
                parse_mode=ParseMode.HTML,
            )
        return True

    if mode == "report_targets":
        from tele_report import (
            REPORT_MAX_TARGETS, parse_report_targets, show_target_preview,
        )
        targets = parse_report_targets(text)
        if not targets:
            await update.message.reply_text(
                _screen(
                    "REPORT",
                    f"{em(E2, '❌')} Target tidak valid.\n"
                    f"Contoh: <code>t.me/scam_group</code>, <code>@scam_user</code>, "
                    f"<code>t.me/+abcdefgh</code>, <code>-1001234567890</code>",
                    "Home › Telegram › Report",
                ),
                parse_mode=ParseMode.HTML,
            )
            return True
        context.user_data.pop(f"tele_await_{user_id}", None)
        job = _tele_jobs.get(user_id)
        if not job:
            await update.message.reply_text(
                _screen("REPORT", f"{em(E2, '❌')} Session hilang. Ulangi dari /tele."),
                parse_mode=ParseMode.HTML,
            )
            return True
        # Monzo mode: max 1 target
        if job.get("monzo_mode"):
            job["targets"] = targets[:1]
        else:
            job["targets"] = targets[:REPORT_MAX_TARGETS]
        # Hapus pesan input user biar UI tetap bersih (1 layar inline)
        try:
            await update.message.delete()
        except Exception:
            pass
        await show_target_preview(job, context.bot, user_id)
        return True

    if mode == "report_comment":
        from tele_report import REPORT_COMMENT_MAX, show_reason
        job = _tele_jobs.get(user_id)
        if not job:
            context.user_data.pop(f"tele_await_{user_id}", None)
            await update.message.reply_text(
                _screen("REPORT", f"{em(E2, '❌')} Session hilang. Ulangi dari /tele."),
                parse_mode=ParseMode.HTML,
            )
            return True
        context.user_data.pop(f"tele_await_{user_id}", None)
        job["comment"] = text[:REPORT_COMMENT_MAX]
        try:
            await update.message.delete()
        except Exception:
            pass
        await show_reason(job, context.bot, user_id)
        return True

    if mode == "extract_links":
        links = parse_group_links(text)
        if not links:
            await update.message.reply_text(
                _screen(
                    "EXTRACT",
                    f"{em(E2, '❌')} Link tidak valid.\n"
                    f"Contoh: <code>t.me/grup|t.me/grup2</code>",
                    "Home › Telegram › Extract",
                ),
                parse_mode=ParseMode.HTML,
            )
            return True
        context.user_data.pop(f"tele_await_{user_id}", None)
        job = _tele_jobs.get(user_id)
        if not job:
            await update.message.reply_text(
                _screen("EXTRACT", f"{em(E2, '❌')} Session hilang. Ulangi dari /tele."),
                parse_mode=ParseMode.HTML,
            )
            return True
        job["links"] = links
        job["phase"] = "extracting"
        job["cancel"] = False
        msg = await update.message.reply_text(
            _screen(
                "EXTRACT",
                f"{em(CE_LOADING, '⏳')} <b>Extract dimulai…</b>\n"
                f"Grup: <b>{len(links)}</b> · Workers: <b>{EXTRACT_WORKERS}</b>",
                "Home › Telegram › Extract",
            ),
            parse_mode=ParseMode.HTML,
        )
        job["chat_id"] = msg.chat_id
        job["message_id"] = msg.message_id
        asyncio.create_task(_run_extract(job, context.bot))
        return True

    return False


# ═══════════════════════════════════════
#  TELETHON HELPERS
# ═══════════════════════════════════════
async def _connect_sender(session_str: str):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(session_str), _TG_API_ID, _TG_API_HASH)
    await asyncio.wait_for(client.connect(), timeout=25)
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Session sender tidak valid / belum login")
    return client


async def _resolve_group(client, token: str, *, join: bool = True):
    """Resolve grup/supergroup. join=False = coba tanpa masuk (hanya public/accessible)."""
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
    from telethon.tl.types import ChatInviteAlready, Channel
    from telethon.errors import UserAlreadyParticipantError, InviteHashExpiredError, InviteHashInvalidError

    kind, value = _normalize_invite(token)
    entity = None

    if kind == "id":
        entity = await client.get_entity(value)
    elif kind == "invite":
        try:
            meta = await client(CheckChatInviteRequest(value))
        except (InviteHashExpiredError, InviteHashInvalidError) as e:
            raise RuntimeError(f"Invite invalid/expired: {e}") from e
        if isinstance(meta, ChatInviteAlready):
            entity = meta.chat
        elif join:
            try:
                updates = await client(ImportChatInviteRequest(value))
                entity = updates.chats[0] if updates.chats else None
            except UserAlreadyParticipantError:
                meta = await client(CheckChatInviteRequest(value))
                entity = getattr(meta, "chat", None)
            except (InviteHashExpiredError, InviteHashInvalidError) as e:
                raise RuntimeError(f"Invite invalid/expired: {e}") from e
        else:
            # Tanpa join: CheckChatInvite tidak kasih akses GetParticipants
            raise RuntimeError(
                "Link private — Telegram wajib join dulu sebelum extract member "
                "(tidak ada API resmi tanpa join)."
            )
    else:
        entity = await client.get_entity(value)
        if join and isinstance(entity, Channel):
            try:
                await client(JoinChannelRequest(entity))
            except UserAlreadyParticipantError:
                pass
            except Exception:
                pass

    if entity is None:
        raise RuntimeError("Gagal resolve grup")

    # Broadcast channel bukan sumber member yang berguna
    if getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False):
        raise RuntimeError(
            "Ini channel broadcast — daftar subscriber hanya untuk admin channel, "
            "bukan daftar member grup. Pakai link grup/supergroup."
        )
    return entity


def _entity_info(entity) -> dict:
    """Full group info dict."""
    title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or "?"
    username = getattr(entity, "username", None) or ""
    eid = getattr(entity, "id", 0)
    is_channel = getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False) or hasattr(entity, "access_hash")
    # Telegram bot API style id for channels/supergroups
    if getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False):
        full_id = int(f"-100{eid}")
    elif getattr(entity, "id", 0) and not getattr(entity, "access_hash", None) and not hasattr(entity, "first_name"):
        # basic Chat
        full_id = -abs(eid)
    else:
        full_id = int(f"-100{eid}") if getattr(entity, "access_hash", None) else eid

    return {
        "id": eid,
        "full_id": full_id,
        "title": title,
        "username": username,
        "megagroup": bool(getattr(entity, "megagroup", False)),
        "broadcast": bool(getattr(entity, "broadcast", False)),
        "participants_count": getattr(entity, "participants_count", None),
        "entity": entity,
    }


async def _extract_members(client, entity) -> list[dict]:
    """Extract members. Docs: GetParticipants butuh akses (biasanya sudah join)."""
    from telethon.errors import ChatAdminRequiredError, ChannelPrivateError, ChatWriteForbiddenError

    members = []
    try:
        async for u in client.iter_participants(entity, aggressive=True):
            if getattr(u, "bot", False) or getattr(u, "deleted", False):
                continue
            members.append({
                "id": u.id,
                "access_hash": getattr(u, "access_hash", 0) or 0,
                "username": u.username or "",
                "first_name": u.first_name or "",
                "last_name": u.last_name or "",
                "phone": getattr(u, "phone", None) or "",
            })
    except (ChatAdminRequiredError, ChannelPrivateError, ChatWriteForbiddenError) as e:
        raise RuntimeError(
            f"Tidak bisa baca member tanpa akses: {type(e).__name__}. "
            "Telegram mewajibkan akun sudah di dalam grup (dan members tidak di-hide)."
        ) from e
    return members


async def _list_admin_groups(client) -> list[dict]:
    """Grup + supergroup + channel broadcast di mana sender admin/creator."""
    out = []
    async for d in client.iter_dialogs():
        ent = d.entity
        if getattr(ent, "bot", False):
            continue
        # Grup biasa / megagroup / channel broadcast (asal ada title, bukan user)
        is_mega = bool(getattr(ent, "megagroup", False))
        is_broadcast = bool(getattr(ent, "broadcast", False)) and not is_mega
        is_basic = hasattr(ent, "title") and not hasattr(ent, "first_name") and not is_mega and not is_broadcast
        if not (is_mega or is_broadcast or is_basic):
            continue
        try:
            perms = await client.get_permissions(ent, "me")
            if not (perms.is_creator or perms.is_admin):
                continue
            can_invite = getattr(perms, "invite_users", None)
            if not perms.is_creator and can_invite is False:
                continue
        except Exception:
            continue
        info = _entity_info(ent)
        info["is_creator"] = bool(getattr(perms, "is_creator", False))
        info["is_admin"] = bool(getattr(perms, "is_admin", False))
        info["dialog_name"] = d.name or info["title"]
        try:
            if info["participants_count"] is None:
                full = await client.get_entity(ent)
                info["participants_count"] = getattr(full, "participants_count", None)
        except Exception:
            pass
        out.append(info)
    return out


# ═══════════════════════════════════════
#  EXTRACT RUNNER
# ═══════════════════════════════════════
async def _edit_job(bot, job, body, kb=None, title="EXTRACT", crumb="Home › Telegram › Extract"):
    try:
        await bot.edit_message_text(
            chat_id=job["chat_id"],
            message_id=job["message_id"],
            text=_screen(title, body, crumb),
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
    except Exception:
        pass


async def _run_extract(job: dict, bot):
    user_id = job["user_id"]
    links = job.get("links") or []
    client = None
    t0 = time.time()

    try:
        client = await _connect_sender(job["session"])
        me = await client.get_me()
        job["sender_tg_id"] = me.id

        sem = asyncio.Semaphore(min(EXTRACT_WORKERS, max(1, len(links))))
        lock = asyncio.Lock()
        members_map = {}  # id -> dict
        sources = []

        async def one(link: str):
            if job.get("cancel"):
                return
            async with sem:
                src = {"link": link, "ok": False, "title": "-", "full_id": "-", "count": 0, "err": ""}
                try:
                    async with lock:
                        joined = False
                        # 1) Coba tanpa join dulu (hanya jalan kalau sudah akses / public + members visible)
                        try:
                            entity = await _resolve_group(client, link, join=False)
                            extracted = await _extract_members(client, entity)
                        except Exception as first_err:
                            # 2) Fallback: join lalu extract (sesuai docs Telegram)
                            entity = await _resolve_group(client, link, join=True)
                            joined = True
                            extracted = await _extract_members(client, entity)
                            src["note"] = f"join dulu lalu extract ({type(first_err).__name__})"
                        info = _entity_info(entity)
                        src["title"] = info["title"]
                        src["full_id"] = info["full_id"]
                        src["username"] = info["username"]
                        src["joined"] = joined
                    for m in extracted:
                        members_map[m["id"]] = m
                    src["count"] = len(extracted)
                    src["ok"] = True
                except Exception as e:
                    src["err"] = str(e)[:160]
                    log.warning("[tele_extract] %s → %s", link, e)
                sources.append(src)
                # progress
                done = len(sources)
                body = (
                    f"{em(CE_LOADING, '⏳')} <b>EXTRACT BERJALAN</b>\n"
                    f"────────────────────────────\n\n"
                    f"  {em(CE_LIVE, '📡')} Progress: <b>{done}/{len(links)}</b>\n"
                    f"  {em(CE_AKUN, '👥')} Unique: <b>{len(members_map)}</b>\n"
                    f"  {em(CE_WAKTU, '⏱')} {int(time.time() - t0)}s\n\n"
                    f"  {em(CE_HELP, '💡')} <i>Support grup, supergroup &amp; channel (asal admin).</i>\n"
                    f"────────────────────────────"
                )
                await _edit_job(bot, job, body, kb=_batal_kb(user_id))

        await asyncio.gather(*(one(L) for L in links))

        if job.get("cancel"):
            await _edit_job(
                bot, job,
                f"{em(E2, '❌')} <b>Extract dibatalkan.</b>\n"
                f"Unique terkumpul: <b>{len(members_map)}</b>",
                kb=_batal_kb(user_id, [[InlineKeyboardButton(
                    "« Menu Telegram", callback_data=f"tele_back_{user_id}", style="primary"
                )]]),
            )
            job["members"] = list(members_map.values())
            return

        job["sources"] = sources
        job["members"] = list(members_map.values())
        job["phase"] = "list_admin"

        # list admin groups
        body = (
            f"{em(CE_LOADING, '⏳')} <b>Mencari grup/channel admin…</b>\n"
            f"Member ter-extract: <b>{len(job['members'])}</b>"
        )
        await _edit_job(bot, job, body, kb=_batal_kb(user_id))

        admin_groups = await _list_admin_groups(client)
        job["admin_groups"] = admin_groups
        job["phase"] = "pick_dest"

        # summary body
        ok_src = sum(1 for s in sources if s["ok"])
        lines = [
            f"{em(E1, '✅')} <b>EXTRACT SELESAI</b>",
            "────────────────────────────",
            "",
            f"  {em(CE_FILE, '📁')} Grup OK: <b>{ok_src}/{len(links)}</b>",
            f"  {em(CE_AKUN, '👥')} Unique member: <b>{len(job['members'])}</b>",
            f"  {em(CE_WAKTU, '⏱')} Waktu: <b>{int(time.time() - t0)}s</b>",
            "",
        ]
        for s in sources[:5]:
            if s["ok"]:
                un = f" @{s['username']}" if s.get("username") else ""
                lines.append(
                    f"  {em(E1, '✅')} <b>{html.escape(str(s['title'])[:28])}</b>{html.escape(un)}\n"
                    f"     ID <code>{s['full_id']}</code> · {s['count']} member"
                )
            else:
                lines.append(
                    f"  {em(E2, '❌')} <code>{html.escape(s['link'][:40])}</code>\n"
                    f"     {html.escape(s.get('err') or 'gagal')}"
                )
        if len(sources) > 5:
            lines.append(f"  <i>… +{len(sources) - 5} grup lain</i>")

        lines += [
            "",
            "────────────────────────────",
            f"  {em(CE_HELP, '💡')} Pilih <b>grup/channel tujuan</b> (sender = admin):",
            "",
        ]

        for g in admin_groups[:8]:
            role = "Creator" if g.get("is_creator") else "Admin"
            un = f" @{g['username']}" if g.get("username") else ""
            cnt = g.get("participants_count")
            cnt_s = f" · {cnt} member" if cnt else ""
            title = html.escape(str(g.get("dialog_name") or g.get("title") or "?")[:36])
            lines.append(
                f"  {em(CE_DETAIL_NAME, '🏷')} <b>{title}</b>{html.escape(un)}\n"
                f"     {em(CE_DETAIL_ID, '🆔')} <code>{g['full_id']}</code> · {role}{cnt_s}"
            )
        if len(admin_groups) > 8:
            lines.append(f"  <i>… +{len(admin_groups) - 8} grup (lihat tombol)</i>")
        if not admin_groups:
            lines.append(f"  {em(E3, '⚠️')} <i>Tidak ada grup/channel di mana sender admin.</i>")

        kb_rows = []
        if job["members"]:
            kb_rows.append([InlineKeyboardButton(
                "Export Hasil Extract",
                callback_data=f"tele_ex_export_{user_id}",
                style="primary",
            )])
            # SEMUA (owner = semua sender DB) / ALL (user = sender milik sendiri)
            if user_id == _USER_ID:
                kb_rows.append([InlineKeyboardButton(
                    "SEMUA · Multi Sender",
                    callback_data=f"tele_ex_semua_{user_id}",
                    style="primary",
                )])
            else:
                kb_rows.append([InlineKeyboardButton(
                    "ALL · Multi Sender",
                    callback_data=f"tele_ex_all_{user_id}",
                    style="primary",
                )])

        for g in admin_groups[:MAX_ADMIN_BUTTONS]:
            badge = "👑" if g.get("is_creator") else "🛡"
            if g.get("broadcast") and not g.get("megagroup"):
                kind = "CH"
            elif g.get("megagroup"):
                kind = "SG"
            else:
                kind = "GB"
            title = (g.get("dialog_name") or g.get("title") or "?")[:16]
            label = f"{badge}[{kind}] {title} | {g['full_id']}"
            kb_rows.append([InlineKeyboardButton(
                label[:64],
                callback_data=f"tele_ex_dest_{user_id}_{g['full_id']}",
                style="success",
            )])

        kb_rows.append([
            InlineKeyboardButton("Selesai", callback_data=f"tele_ex_skip_{user_id}", style="danger"),
        ])

        await _edit_job(
            bot, job, "\n".join(lines),
            kb=InlineKeyboardMarkup(kb_rows),
        )

        if job["members"]:
            try:
                await _send_extract_file_bot(bot, job)
            except Exception as e:
                log.warning("[tele_extract] export file: %s", e)

    except Exception as e:
        log.exception("[tele_extract] fatal")
        await _edit_job(
            bot, job,
            f"{em(E2, '❌')} <b>Extract gagal</b>\n\n<code>{html.escape(str(e)[:200])}</code>",
        )
        _tele_jobs.pop(user_id, None)
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


async def _send_extract_file_bot(bot, job):
    lines = []
    for m in job.get("members") or []:
        un = f"@{m['username']}" if m.get("username") else "-"
        name = f"{m.get('first_name', '')} {m.get('last_name', '')}".strip()
        lines.append(f"{m['id']}|{un}|{name}")
    buf = BytesIO("\n".join(lines).encode("utf-8"))
    buf.name = f"extract_{job.get('sender_phone', 'tele')}.txt"
    await bot.send_document(
        chat_id=job["chat_id"],
        document=buf,
        caption=f"Extract {len(lines)} member",
    )


async def _send_extract_file(chat_id, context, job, bot=None):
    await _send_extract_file_bot(bot or context.bot, job)


# ═══════════════════════════════════════
#  INVITE RUNNER
# ═══════════════════════════════════════
_SKIP_ERR_KEYS = (
    "privacy",
    "not mutual",
    "mutual contact",
    "invite request",
    "invitation",
    "can't add",
    "cannot add",
    "user_channels_too_much",
    "userbannedinchannel",
    "user_banned",
    "user_kicked",
    "user_deleted",
    "inputuserdeactivated",
    "user_not_mutual",
    "chat_write_forbidden",
    "user_privacy",
    "previously",
    "missing_invitee",
)


def _is_skip_error(exc: Exception) -> bool:
    msg = str(exc or "").lower()
    return any(k in msg for k in _SKIP_ERR_KEYS)


async def _get_member_count(client, entity) -> int | None:
    """Ambil jumlah member grup saat ini (best-effort)."""
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.messages import GetFullChatRequest
        from telethon.tl.types import Channel

        if isinstance(entity, Channel):
            full = await client(GetFullChannelRequest(entity))
            return int(getattr(full.full_chat, "participants_count", 0) or 0)
        # basic Chat
        full = await client(GetFullChatRequest(entity.id))
        count = getattr(full.full_chat, "participants_count", None)
        if count is not None:
            return int(count)
        participants = getattr(full.full_chat, "participants", None)
        users = getattr(participants, "participants", None) if participants else None
        if users is not None:
            return len(users)
    except Exception:
        pass
    try:
        ent = await client.get_entity(entity)
        c = getattr(ent, "participants_count", None)
        if c is not None:
            return int(c)
    except Exception:
        pass
    return None


async def _invite_one(client, dest_entity, user_peer, user_id: int) -> str:
    """Invite 1 user. Returns: ok | already | skip | fail.

    Docs Telegram: InviteToChannelRequest mengembalikan `missing_invitees`
    (daftar user yang GAGAL). Jadi cukup 1 request per user — TIDAK perlu
    GetParticipant tambahan (itu yang bikin flood 2-3x lipat + skip palsu).
    """
    from telethon.tl.functions.channels import InviteToChannelRequest
    from telethon.tl.functions.messages import AddChatUserRequest
    from telethon.tl.types import Channel
    from telethon.errors import (
        UserPrivacyRestrictedError,
        UserAlreadyParticipantError,
        UserNotMutualContactError,
        UserChannelsTooMuchError,
        ChatAdminRequiredError,
        UserBannedInChannelError,
        InputUserDeactivatedError,
    )

    try:
        if isinstance(dest_entity, Channel):
            result = await client(InviteToChannelRequest(dest_entity, [user_peer]))
            # missing_invitees non-empty → user ini gagal (privacy / butuh link / dll)
            missing = getattr(result, "missing_invitees", None)
            if missing:
                return "skip"
            return "ok"
        else:
            await client(AddChatUserRequest(dest_entity.id, user_peer, fwd_limit=10))
            return "ok"
    except UserAlreadyParticipantError:
        return "already"
    except (
        UserPrivacyRestrictedError,
        UserNotMutualContactError,
        UserChannelsTooMuchError,
        UserBannedInChannelError,
        InputUserDeactivatedError,
    ):
        return "skip"
    except ChatAdminRequiredError:
        raise
    except Exception as e:
        if _is_skip_error(e):
            return "skip"
        raise


async def _start_invite(query, context, user_id, dest_full_id: int):
    job = _tele_jobs.get(user_id)
    if not job or not job.get("members"):
        await query.answer("Tidak ada member hasil extract", show_alert=True)
        return True

    dest = None
    for g in job.get("admin_groups") or []:
        if g["full_id"] == dest_full_id or g["id"] == dest_full_id or g["id"] == abs(dest_full_id):
            dest = g
            break
    if not dest:
        dest = {"full_id": dest_full_id, "title": str(dest_full_id), "id": abs(dest_full_id)}

    job["dest"] = dest
    job["phase"] = "inviting"
    job["cancel"] = False
    job["message_id"] = query.message.message_id
    job["chat_id"] = query.message.chat_id

    if dest.get("broadcast") and not dest.get("megagroup"):
        kind = "Channel"
    elif dest.get("megagroup"):
        kind = "Supergroup"
    else:
        kind = "Grup"
    mc = dest.get("participants_count")
    mc_line = (
        f"  {em(CE_AKUN, '👥')} Member sekarang: <b>{mc}</b>\n"
        if mc is not None else ""
    )
    body = (
        f"{em(CE_LOADING, '⏳')} <b>MASUKAN GRUP</b>\n"
        f"────────────────────────────\n\n"
        f"  {em(CE_DETAIL_NAME, '🏷')} Tujuan: <b>{html.escape(str(dest.get('title') or dest.get('dialog_name') or '?')[:40])}</b>\n"
        f"  {em(CE_DETAIL_ID, '🆔')} ID: <code>{dest.get('full_id')}</code>\n"
        f"  {em(CE_FILE, '📁')} Tipe: <b>{kind}</b>\n"
        f"{mc_line}"
        f"  {em(CE_AKUN, '👥')} Antrian: <b>{len(job['members'])}</b>\n"
        f"  {em(E6, '🚀')} Mode aman + verifikasi member\n\n"
        f"  {em(CE_HELP, '💡')} <i>Docs Telethon: mass-add sering gagal diam-diam.\n"
        f"  Bot cek ulang tiap user benar-benar masuk.</i>\n"
        f"────────────────────────────"
    )
    await query.edit_message_text(
        _screen("MASUKAN GRUP", body, "Home › Telegram › Extract › Invite"),
        parse_mode=ParseMode.HTML,
        reply_markup=_batal_kb(user_id),
    )
    asyncio.create_task(_run_invite(job, context.bot))
    return True


async def _run_invite(job: dict, bot):
    from telethon.tl.types import InputPeerUser
    from telethon.errors import (
        FloodWaitError,
        PeerFloodError,
        ChatAdminRequiredError,
    )

    user_id = job["user_id"]
    client = None
    stats = {
        "ok": 0, "fail": 0, "flood": 0,
        "already": 0, "skip": 0, "done": 0,
    }
    t0 = time.time()
    members = job.get("members") or []
    dest = job.get("dest") or {}
    delay = float(INVITE_DELAY)
    info = {"title": "?", "full_id": dest.get("full_id"), "username": ""}
    member_now = dest.get("participants_count")
    member_start = member_now

    def progress_body(extra=""):
        member_line = (
            f"  {em(CE_AKUN, '👥')} Member sekarang: <b>{member_now}</b>"
            + (f" <i>(awal {member_start})</i>" if member_start is not None and member_now is not None else "")
            + "\n"
            if member_now is not None else
            f"  {em(CE_AKUN, '👥')} Member sekarang: <i>menghitung…</i>\n"
        )
        return (
            f"{em(CE_LIVE, '📡')} <b>INVITE BERJALAN</b>\n"
            f"────────────────────────────\n\n"
            f"  Tujuan: <b>{html.escape(str(info.get('title') or '?')[:36])}</b>\n"
            f"  ID: <code>{info.get('full_id')}</code>\n"
            f"{member_line}\n"
            f"  Progress: <b>{stats['done']}/{len(members)}</b>\n"
            f"  {em(E1, '✅')} Berhasil masuk: <b>{stats['ok']}</b>\n"
            f"  Already: <b>{stats['already']}</b>\n"
            f"  {em(E3, '⏭')} Skip (privacy/butuh link): <b>{stats['skip']}</b>\n"
            f"  {em(E2, '❌')} Gagal: <b>{stats['fail']}</b>\n"
            f"  Flood wait: <b>{stats['flood']}</b>x · delay {delay:.1f}s\n"
            f"{extra}"
            f"────────────────────────────"
        )

    try:
        client = await _connect_sender(job["session"])
        dest_entity = None
        try:
            dest_entity = await client.get_entity(dest.get("full_id") or dest.get("id"))
        except Exception:
            for g in job.get("admin_groups") or []:
                if g.get("full_id") == dest.get("full_id"):
                    if g.get("username"):
                        dest_entity = await client.get_entity(g["username"])
                    else:
                        dest_entity = await client.get_entity(g["id"])
                    break
        if dest_entity is None:
            raise RuntimeError("Grup/channel tujuan tidak ditemukan di session sender")

        info = _entity_info(dest_entity)
        member_now = await _get_member_count(client, dest_entity)
        if member_now is None:
            member_now = info.get("participants_count")
        member_start = member_now
        me_id = (await client.get_me()).id
        consec_flood = 0

        await _edit_job(
            bot, job, progress_body(),
            kb=_batal_kb(user_id),
            title="MASUKAN GRUP",
            crumb="Home › Telegram › Extract › Invite",
        )

        for m in members:
            if job.get("cancel"):
                break
            stats["done"] += 1

            if m.get("id") == me_id:
                stats["skip"] += 1
                continue

            try:
                if m.get("access_hash"):
                    peer = InputPeerUser(m["id"], m["access_hash"])
                else:
                    peer = await client.get_input_entity(m["id"])
            except Exception:
                stats["skip"] += 1
                await _edit_job(
                    bot, job, progress_body(),
                    kb=_batal_kb(user_id),
                    title="MASUKAN GRUP",
                    crumb="Home › Telegram › Extract › Invite",
                )
                continue

            status = None
            tries = 0
            while tries < 2 and status is None:
                tries += 1
                if job.get("cancel"):
                    break
                try:
                    status = await _invite_one(client, dest_entity, peer, m["id"])
                    consec_flood = 0
                except FloodWaitError as e:
                    stats["flood"] += 1
                    wait = int(getattr(e, "seconds", 30) or 30) + 2
                    wait = min(max(wait, 5), 300)
                    delay = min(max(delay * 1.2, INVITE_DELAY_MIN), INVITE_DELAY_MAX)
                    await _edit_job(
                        bot, job,
                        progress_body(
                            f"\n  {em(CE_LOADING, '⏳')} FloodWait <b>{wait}s</b> — lanjut otomatis…\n"
                        ),
                        kb=_batal_kb(user_id),
                        title="MASUKAN GRUP",
                        crumb="Home › Telegram › Extract › Invite",
                    )
                    await asyncio.sleep(wait)
                except PeerFloodError:
                    stats["flood"] += 1
                    consec_flood += 1
                    delay = min(max(delay * 1.6, 8), INVITE_DELAY_MAX)
                    # Jangan retry user yang sama — tandai skip, cooldown, lanjut user berikutnya
                    status = "skip"
                    await _edit_job(
                        bot, job,
                        progress_body(
                            f"\n  {em(E3, '⚠️')} PeerFlood — cooldown <b>{FLOOD_COOLDOWN}s</b>, lalu lanjut…\n"
                        ),
                        kb=_batal_kb(user_id),
                        title="MASUKAN GRUP",
                        crumb="Home › Telegram › Extract › Invite",
                    )
                    await asyncio.sleep(FLOOD_COOLDOWN)
                except ChatAdminRequiredError:
                    raise
                except Exception as e:
                    if _is_skip_error(e):
                        status = "skip"
                    else:
                        log.warning("invite one: %s", e)
                        status = "fail"

            # Akun kena limit beruntun → stop biar tidak makin diblokir
            if consec_flood >= MAX_CONSEC_FLOOD:
                job["flood_stopped"] = True
                break

            if job.get("cancel"):
                break

            if status == "ok":
                stats["ok"] += 1
                delay = max(INVITE_DELAY_MIN, delay * 0.9)
                # Realtime: naikkan lokal (tanpa request tambahan → hemat, anti-flood)
                if isinstance(member_now, int):
                    member_now += 1
                # Sinkron ke server hanya sesekali (jarang) biar akurat tapi tidak flood
                if stats["ok"] % 25 == 0:
                    fresh = await _get_member_count(client, dest_entity)
                    if fresh is not None:
                        member_now = fresh
            elif status == "already":
                stats["already"] += 1
            elif status == "skip":
                stats["skip"] += 1
            else:
                stats["fail"] += 1

            await _edit_job(
                bot, job, progress_body(),
                kb=_batal_kb(user_id),
                title="MASUKAN GRUP",
                crumb="Home › Telegram › Extract › Invite",
            )
            await asyncio.sleep(delay)

        fresh = await _get_member_count(client, dest_entity)
        if fresh is not None:
            member_now = fresh

        job["invite_stats"] = stats
        job["phase"] = "done"
        cancelled = bool(job.get("cancel"))
        flood_stopped = bool(job.get("flood_stopped"))
        title_line = "DIBATALKAN" if cancelled else ("DIHENTIKAN (FLOOD)" if flood_stopped else "SELESAI")
        delta = ""
        if member_start is not None and member_now is not None:
            delta = f" <i>(+{max(0, int(member_now) - int(member_start))})</i>"
        flood_note = ""
        if flood_stopped:
            flood_note = (
                f"\n  {em(E3, '⚠️')} <i>Sender kena limit (PeerFlood) beruntun.\n"
                f"  Pakai <b>ALL/SEMUA · Multi Sender</b> atau tunggu\n"
                f"  beberapa jam sebelum sender ini dipakai lagi.</i>\n"
            )
        body = (
            f"{em(E2 if (cancelled or flood_stopped) else E1, '✅')} <b>MASUKAN GRUP {title_line}</b>\n"
            f"────────────────────────────\n\n"
            f"  {em(CE_DETAIL_NAME, '🏷')} <b>{html.escape(str(info.get('title') or '?')[:40])}</b>\n"
            f"  {em(CE_DETAIL_ID, '🆔')} <code>{info.get('full_id')}</code>\n"
            f"  {em(CE_TELEGRAM, '💬')} @{html.escape(info.get('username') or '-')}\n"
            f"  {em(CE_AKUN, '👥')} Member sekarang: <b>{member_now if member_now is not None else '?'}</b>"
            f"{delta}\n"
            f"  {em(CE_AKUN, '👥')} Member awal: <b>{member_start if member_start is not None else '?'}</b>\n\n"
            f"  {em(E1, '✅')} Berhasil masuk: <b>{stats['ok']}</b>\n"
            f"  Already (sudah di dalam): <b>{stats['already']}</b>\n"
            f"  {em(E3, '⏭')} Skip (privacy / wajib link): <b>{stats['skip']}</b>\n"
            f"  {em(E2, '❌')} Gagal lain: <b>{stats['fail']}</b>\n"
            f"  Flood: <b>{stats['flood']}</b>x\n"
            f"  Diproses: <b>{stats['done']}/{len(members)}</b>\n"
            f"  {em(CE_WAKTU, '⏱')} {int(time.time() - t0)}s\n"
            f"{flood_note}\n"
            f"  {em(CE_HELP, '💡')} <i>Skip = user tak bisa di-add langsung\n"
            f"  (privacy 'siapa yg bisa menambahkan saya').</i>\n"
            f"────────────────────────────"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("« Menu Telegram", callback_data=f"tele_back_{user_id}", style="primary")],
            [InlineKeyboardButton("Batal / Tutup", callback_data=f"tele_cancel_{user_id}", style="danger")],
        ])
        await _edit_job(
            bot, job, body, kb=kb,
            title="MASUKAN GRUP", crumb="Home › Telegram › Extract › Invite",
        )
    except Exception as e:
        log.exception("[tele_invite]")
        await _edit_job(
            bot, job,
            f"{em(E2, '❌')} <b>Invite gagal</b>\n<code>{html.escape(str(e)[:200])}</code>",
            kb=_batal_kb(user_id, [[InlineKeyboardButton(
                "« Menu Telegram", callback_data=f"tele_back_{user_id}", style="primary"
            )]]),
            title="MASUKAN GRUP",
            crumb="Home › Telegram › Extract › Invite",
        )
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
