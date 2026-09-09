"""
faq_server.py — Modul Reset OTP via WhatsApp FAQ API (terpisah dari dik.py).

Dipakai oleh /fix (tombol inline "RESET OTP"). Tidak mengandung kode UI Telegram;
hanya logic: device random, submit ke faq.whatsapp.com/client_search.php, dan
reservasi email-slot (cooldown 1 jam) dari sitepro_mailboxes sebagai rate-limit.

Temuan HAR (whatsappfaq.har): client_search.php = FAQ SEARCH endpoint.
Response = array artikel bantuan (JSON). Tidak ada balasan manusia/email dari sini.
Maka "balasan" = bukti HTTP 200 + jumlah/judul artikel FAQ yang di-return.

API publik:
    run_faq_reset_pipeline(numbers, db_cur=None, db_conn=None,
                           message_template=None, progress_cb=None) -> dict

progress_cb(stage, data) mengikuti pola run_fix_multi_pipeline di sitepro_fix_module.
Stages: 'start', 'device', 'slot', 'send_start', 'send_ok', 'send_err', 'done'.
"""
import sys
import json
import uuid
import random
import re
import threading
import time
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Shared session with connection pooling + retry — prevents socket leaks on long runs
_HTTP_SESSION = requests.Session()
_retry = Retry(total=2, backoff_factor=1, status_forcelist=[502, 503, 504])
_HTTP_SESSION.mount("https://", HTTPAdapter(max_retries=_retry, pool_connections=10, pool_maxsize=10))
_HTTP_SESSION.mount("http://", HTTPAdapter(max_retries=_retry, pool_connections=5, pool_maxsize=5))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

FAQ_URL = "https://faq.whatsapp.com/client_search.php"
SENDER_COOLDOWN_SECS = 3600  # 1 jam, selaras dengan sitepro_fix_module

# Lock global supaya reservasi email-slot atomik antar thread (anti tabrakan /fix paralel)
_SLOT_LOCK = threading.Lock()

# ── Pool device Android realistis ────────────────────────────────────────────
_DEVICE_POOL = [
    {"manufacturer": "Xiaomi", "device": "creek", "model": "25062RN2DY",
     "product": "creek_n_global", "board": "bengal", "os": "15",
     "build": "Redmi/creek_n_global/creek:15/AQ3A.250226.002/OS2.0.206.0.VBOIDXM:user/release-keys"},
    {"manufacturer": "Xiaomi", "device": "sapphire", "model": "2311DRK48G",
     "product": "sapphire_global", "board": "kalama", "os": "14",
     "build": "Redmi/sapphire_global/sapphire:14/UKQ1.230804.001/V816.0.6.0:user/release-keys"},
    {"manufacturer": "samsung", "device": "a54x", "model": "SM-A546E",
     "product": "a54xnsxx", "board": "s5e8835", "os": "14",
     "build": "samsung/a54xnsxx/a54x:14/UP1A.231005.007/A546EXXU6CXJ3:user/release-keys"},
    {"manufacturer": "samsung", "device": "dm3q", "model": "SM-S918B",
     "product": "dm3qxxx", "board": "kalama", "os": "14",
     "build": "samsung/dm3qxxx/dm3q:14/UP1A.231005.007/S918BXXU4CWL5:user/release-keys"},
    {"manufacturer": "OPPO", "device": "OP573CL1", "model": "CPH2557",
     "product": "CPH2557", "board": "k6855v1_64", "os": "14",
     "build": "OPPO/CPH2557/OP573CL1:14/UP1A.231005.007/S.123abc:user/release-keys"},
    {"manufacturer": "vivo", "device": "PD2306", "model": "V2324",
     "product": "PD2306F_EX", "board": "k6877v1_64", "os": "14",
     "build": "vivo/PD2306F_EX/PD2306:14/UP1A.231005.007/compiler123:user/release-keys"},
    {"manufacturer": "realme", "device": "RE58B2L1", "model": "RMX3771",
     "product": "RMX3771", "board": "mt6877", "os": "14",
     "build": "realme/RMX3771/RE58B2L1:14/UP1A.231005.007/R.20240101:user/release-keys"},
]
_APP_VERSIONS = ["2.26.27.85", "2.26.25.82", "2.26.23.74", "2.26.20.78"]
_LATEST_APP_VERSION = "2.26.27.85"
_CARRIERS = ["Indosat Ooredoo", "Telkomsel", "XL Axiata", "3 Indonesia", "Smartfren"]
_MCC_MNC = {"Indosat Ooredoo": "510-01", "Telkomsel": "510-10", "XL Axiata": "510-11",
            "3 Indonesia": "510-89", "Smartfren": "510-09"}


def build_random_device(use_latest: bool = False):
    """Profil device + debug_info + useragent random untuk 1 request."""
    d = dict(random.choice(_DEVICE_POOL))
    app_ver = _LATEST_APP_VERSION if use_latest else random.choice(_APP_VERSIONS)
    carrier = random.choice(_CARRIERS)
    anid = str(uuid.uuid4())
    useragent = f"WhatsApp/{app_ver} Android/{d['os']} Device/{d['manufacturer']}-{d['model']}"
    debug_info = {
        "App": "com.whatsapp", "Architecture": "aarch64", "Board": d["board"],
        "Build": d["build"], "CCode": "", "CPU ABI": "arm64-v8a", "Carrier": carrier,
        "Description": app_ver, "Device": d["device"], "Device ID": 0,
        "Is Foldable": False, "Is Tablet": False, "LC": "US", "LG": "en",
        "Manufacturer": d["manufacturer"], "Model": d["model"],
        "Network Type": "U.N.K.N.O.W.N.", "OS": d["os"], "Phone Type": "G.S.M.",
        "Product": d["product"], "Radio MCC-MNC": _MCC_MNC.get(carrier, "510-01"),
        "SIM MCC-MNC": _MCC_MNC.get(carrier, "510-01"), "Target": "release",
        "Version": app_ver, "Context": "register-phone-invalid", "useragent": useragent,
        "Connection": "W.I.F.I.", "Diagnostic Codes": "FE-GDE FE-GDC FE-VIDC FE-SMSRTV ",
        "anid": anid,
    }
    return {"anid": anid, "manufacturer": d["manufacturer"], "os_version": d["os"],
            "app_version": app_ver, "useragent": useragent, "debug_info": debug_info}


# ── Random email generator untuk FAQ2 form flooding ──────────────────────────
_EDU_DOMAINS = [
    "student.harvard.edu", "alumni.stanford.edu", "mail.utexas.edu",
    "usc.edu", "umich.edu", "berkeley.edu", "columbia.edu",
    "mail.ucf.edu", "purdue.edu", "gatech.edu", "nyu.edu",
    "mail.ubc.ca", "student.unsw.edu.au", "cam.ac.uk",
]
_PUBLIC_DOMAINS = [
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "protonmail.com", "icloud.com", "aol.com", "mail.com",
]
_FIRST_NAMES = [
    "james", "emma", "olivia", "liam", "sophia", "noah", "ava", "mason",
    "isabella", "logan", "mia", "lucas", "amelia", "ethan", "harper",
    "aiden", "ella", "caden", "aria", "jackson", "riley", "daniel",
    "zoey", "henry", "lily", "sebastian", "chloe", "owen", "layla",
]
_LAST_NAMES = [
    "smith", "johnson", "williams", "brown", "jones", "garcia", "miller",
    "davis", "rodriguez", "martinez", "hernandez", "lopez", "gonzalez",
    "wilson", "anderson", "thomas", "taylor", "moore", "jackson", "martin",
]

FAQ2_WORKERS = 3   # Concurrent workers for FAQ2 form submissions
FAQ1_WORKERS = 4   # Concurrent workers for FAQ1 form submissions


def _generate_random_emails(count=19):
    """Generate realistic random emails (mix of .edu and public domains)."""
    emails = []
    for _ in range(count):
        first = random.choice(_FIRST_NAMES)
        last = random.choice(_LAST_NAMES)
        # Mix: 40% .edu, 60% public
        if random.random() < 0.4:
            domain = random.choice(_EDU_DOMAINS)
        else:
            domain = random.choice(_PUBLIC_DOMAINS)
        # Vary format: first.last, firstlast, first.last123, first_last
        fmt = random.choice([
            f"{first}.{last}",
            f"{first}{last}",
            f"{first}.{last}{random.randint(1, 99)}",
            f"{first}_{last}{random.randint(10, 999)}",
            f"{first[0]}{last}{random.randint(1, 9)}",
        ])
        emails.append(f"{fmt}@{domain}")
    return emails


_ZOCKSHOP_DOMAIN = "zockshop.com"

def _generate_zockshop_emails(count: int) -> list[str]:
    """Random firstlast@zockshop.com emails untuk FAQ2 filler."""
    out = []
    for _ in range(count):
        first = random.choice(_FIRST_NAMES)
        last = random.choice(_LAST_NAMES)
        fmt = random.choice([
            f"{first}{last}",
            f"{first}.{last}",
            f"{first}{last}{random.randint(1, 99)}",
            f"{first}_{last}",
        ])
        out.append(f"{fmt}@{_ZOCKSHOP_DOMAIN}")
    return out

def _generate_filler_emails(count: int, domain: str = _ZOCKSHOP_DOMAIN) -> list[str]:
    """Random firstlast@domain emails untuk FAQ2 filler — domain dari temp mail API."""
    out = []
    for _ in range(count):
        first = random.choice(_FIRST_NAMES)
        last = random.choice(_LAST_NAMES)
        fmt = random.choice([
            f"{first}{last}",
            f"{first}.{last}",
            f"{first}{last}{random.randint(1, 99)}",
            f"{first}_{last}",
        ])
        out.append(f"{fmt}@{domain}")
    return out

# ── Pool teks panjang multi-bahasa (statis, untuk fallback) ─────────────────
# Fokus: layar "Choose how to verify" — SMS disabled + "Try again in xx jam"
# (lihat assets/form_v3_proof.jpg). Cerita natural: screen → tombol mati →
# timer → udah coba Voice call / Retry other device → SIM fine.
_LONG_TEXT_POOL = [
    # English — santai
    (
        "Hi WhatsApp Support,\n\n"
        "My number +{nomor} can't get verified. On the \"Choose how to "
        "verify\" screen, the SMS option is greyed out with a \"Try again "
        "in {jam} hours\" note under it — WhatsApp won't send me an SMS "
        "code until that runs out.\n\n"
        "Tried \"Retry on other device\", restarting, re-seating my SIM — "
        "same countdown every time. My SIM is fine, other texts arrive "
        "normally.\n\n"
        "Can you reset the SMS hold on +{nomor} so I can request a code? "
        "Thanks."
    ),
    # English variant 2
    (
        "Hey WhatsApp Team,\n\n"
        "Stuck on verification for +{nomor} — the SMS button on the "
        "\"Choose how to verify\" screen is dead, it just says \"Try "
        "again in {jam} hours\". The Voice call option is there but no "
        "call ever comes through when I pick it.\n\n"
        "Waited out the full countdown once, and a fresh one appeared. "
        "Restarted, reinstalled — nothing changes.\n\n"
        "Please unlock the SMS option on +{nomor}. Appreciate it."
    ),
    # Indonesian — santai
    (
        "Halo WhatsApp Support,\n\n"
        "Nomor +{nomor} gak bisa verifikasi. Di layar \"Choose how to "
        "verify\", opsi SMS-nya abu-abu dengan tulisan \"Try again in "
        "{jam} jam\" — WhatsApp gak mau kirim kode SMS sampai hitungan itu "
        "habis.\n\n"
        "Udah coba \"Retry on other device\", restart, cabut pasang SIM — "
        "hitungan yang sama muncul terus. SIM normal, SMS lain masuk.\n\n"
        "Tolong reset penahanan SMS di +{nomor} biar kodenya bisa "
        "dikirim. Makasih."
    ),
    # Indonesian variant 2
    (
        "Halo Tim Support,\n\n"
        "Gak bisa verifikasi di +{nomor} — tombol SMS di layar \"Choose "
        "how to verify\" mati, tulisannya \"Try again in {jam} jam\". Opsi "
        "Voice call ada tapi pas dicoba gak ada telepon masuk.\n\n"
        "Udah nunggu hitungannya habis sekali, pas dibuka lagi malah "
        "muncul hitungan baru. Restart, install ulang — gak ada "
        "perubahan.\n\n"
        "Tolong buka lagi opsi SMS buat +{nomor} ya. Terima kasih."
    ),
]

_LONG_LANG_WEIGHTS = {"en1": 0.30, "en2": 0.25, "id1": 0.25, "id2": 0.20}


def _pick_long_text() -> str:
    """Legacy wrapper — pool panjang sekarang di faq_appeal_texts."""
    from faq_appeal_texts import _LONG_TEXTS
    import random as _r
    texts = [t[2] for t in _LONG_TEXTS]
    weights = [t[1] for t in _LONG_TEXTS]
    return _r.choices(texts, weights=weights, k=1)[0]


def build_appeal_message(nomor_clean, template=None):
    """Teks appeal reset (fallback). EN/ID santai. Fokus: SMS terkunci limit jam."""
    if template:
        try:
            t = str(template)
            return t.replace("{nomor}", nomor_clean) if "{nomor}" in t else t + f"\n\n+{nomor_clean}"
        except Exception:
            pass
    return (
        f"Hi WhatsApp Support,\n\n"
        f"My number +{nomor_clean} can't get verified. On the \"Choose how "
        "to verify\" screen the SMS option is greyed out with a \"Try "
        "again\" countdown of many hours — I can't even request a code.\n\n"
        f"Please reset the hold on +{nomor_clean} so the SMS code can be "
        "sent. Thanks."
    )


def _norm_number(n):
    c = str(n).strip().replace("+", "").replace("-", "").replace(" ", "")
    if not c.startswith("62") and c.startswith("0"):
        c = "62" + c[1:]
    return c


def reserve_email_slots(db_cur, db_conn, limit=1, cooldown_secs=SENDER_COOLDOWN_SECS):
    """Reservasi email-slot dari sitepro_mailboxes (cooldown 1 jam), atomik.

    Email TIDAK dikirim ke API (client_search.php anonim). Slot dipakai murni
    sebagai pembatas rate: 1 email = 1 request / jam, biar tidak spam endpoint.
    Mengembalikan list dict {row_id, email}. Set last_used_at=now untuk yang dipilih.
    """
    if not db_cur or not db_conn:
        return []
    out = []
    with _SLOT_LOCK:
        try:
            db_cur.execute(
                f"""SELECT id, email FROM sitepro_mailboxes
                    WHERE mailbox_id IS NOT NULL AND mailbox_id > 0
                      AND (last_used_at IS NULL
                           OR last_used_at <= datetime('now', '-{int(cooldown_secs)} seconds'))
                    ORDER BY (last_used_at IS NULL) DESC, last_used_at ASC, id ASC
                    LIMIT ?""",
                (limit,),
            )
            rows = db_cur.fetchall()
            ids = [r[0] for r in rows]
            if ids:
                placeholders = ",".join(["?"] * len(ids))
                db_cur.execute(
                    f"UPDATE sitepro_mailboxes SET last_used_at = CURRENT_TIMESTAMP "
                    f"WHERE id IN ({placeholders})",
                    ids,
                )
                db_conn.commit()
            out = [{"row_id": r[0], "email": r[1]} for r in rows]
        except Exception as e:
            print(f"[FAQ] reserve_email_slots error: {e}")
    return out


def submit_faq(nomor_clean, message, device, timeout=30):
    """POST ke client_search.php (multipart debug_info). Return dict 'balasan API'."""
    params = {
        "platform": "android", "lg": "en", "lc": "US", "eea": "0",
        "query": message, "manufacturer": device["manufacturer"],
        "os_version": device["os_version"], "ccode": "",
        "app_version": device["app_version"], "anid": device["anid"],
    }
    headers = {"User-Agent": device["useragent"], "Accept-Encoding": "gzip"}
    files = {"debug_info": (None, json.dumps(device["debug_info"], ensure_ascii=False))}

    out = {"nomor": nomor_clean, "ok": False, "status": None,
           "article_count": 0, "titles": [], "error": None, "raw_len": 0}
    try:
        r = _HTTP_SESSION.post(FAQ_URL, params=params, headers=headers, files=files, timeout=timeout)
        out["status"] = r.status_code
        out["raw_len"] = len(r.text or "")
        r.raise_for_status()
        try:
            data = r.json()
            if isinstance(data, list):
                out["article_count"] = len(data)
                out["titles"] = [a.get("title", "?") for a in data[:5] if isinstance(a, dict)]
            elif isinstance(data, dict):
                out["article_count"] = 1
                out["titles"] = [data.get("title", "?")]
        except ValueError:
            out["error"] = "Response bukan JSON (mungkin HTML/JS)"
        out["ok"] = (r.status_code == 200)
    except requests.Timeout:
        out["error"] = "Request timeout"
    except requests.HTTPError as e:
        out["error"] = f"HTTP error: {e}"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


FAQ2_URL = "https://www.whatsapp.com/contact/noclient/verification/"
# Form V1 (baru, dari v2whatsapp.har) — contact async/new
FAQ1_URL = "https://www.whatsapp.com/contact/noclient/async/new/"
FAQ1_CONTACT_URL = "https://www.whatsapp.com/contact/?subject=messenger"

# ── Mode preset RESET OTP ────────────────────────────────────────────────────
# Alokasi sender (WAJIB dipatuhi di run_reset_otp_pipeline):
#   easy: senders=7 → banding 5 (1 email/sender) + faq1 1 email kita + faq2 1 email kita
#   hard: senders=12 → banding 10 + faq1 1 + faq2 1
# Form V1/V2: 1 slot = email sitepro kita (biar balasan masuk), sisanya random.
# Form V3: Gmail/iCloud SMTP + logs/foto — selalu 2 kirim (easy/hard), tidak pakai sender sitepro.
RESET_OTP_PRESETS = {
    "easy": {"faq2": 8, "banding": 5, "faq1": 3, "faq3": 2, "faq4": 1, "senders": 7},
    "hard": {"faq2": 12, "banding": 10, "faq1": 5, "faq3": 2, "faq4": 1, "senders": 12},
}

# 3 text pools (FAQ / email banding / form) — random.choice per submit
# Placeholder: {nomor} {jam}
# Hanya Bahasa Indonesia + English, tone santai (gak terlalu sopan, gak kasar)
# Framing (sesuai assets/form_v3_proof.jpg): layar "Choose how to verify",
# opsi SMS abu-abu + "Try again in {jam} jam", Voice call gak nyambung,
# SIM normal.
_TEXT_FAQ = [
    (
        "Hi WhatsApp Support,\n\n"
        "My number +{nomor} can't get verified. On the \"Choose how to "
        "verify\" screen the SMS option is greyed out with a \"Try again "
        "in {jam} hours\" note — I can't even request a code.\n\n"
        "SIM works fine, other texts come through. It's a WhatsApp-side "
        "hold.\n\n"
        "Can you reset the hold so the SMS option works again? Thanks."
    ),
    (
        "Halo WhatsApp Support,\n\n"
        "Nomor +{nomor} gak bisa verifikasi. Di layar \"Choose how to "
        "verify\" opsi SMS-nya abu-abu, tulisannya \"Try again in {jam} "
        "jam\" — jadi saya gak bisa minta kode sama sekali.\n\n"
        "SIM aktif, SMS lain masuk normal — penahannya dari sisi "
        "WhatsApp.\n\n"
        "Tolong reset penahannya biar opsi SMS bisa dipake lagi ya. "
        "Makasih."
    ),
    (
        "Hey WhatsApp Team,\n\n"
        "Number +{nomor} can't request a code — the SMS button on the "
        "\"Choose how to verify\" screen is dead with a \"Try again in "
        "{jam} hours\" label. Restarted my phone, reinstalled the app, "
        "nothing unlocks it.\n\n"
        "Please look into why +{nomor} is on hold and remove it. "
        "Appreciate it."
    ),
    (
        "Halo Tim Support,\n\n"
        "Nomor saya +{nomor} gak bisa request kode — tombol SMS di layar "
        "\"Choose how to verify\" mati, tulisannya \"Try again in {jam} "
        "jam\". SIM aktif dan nomor ini milik saya.\n\n"
        "Mohon bantu cek kenapa nomor saya ditahan dan reset ya. "
        "Terima kasih."
    ),
]
_TEXT_BANDING = [
    (
        "Hi WhatsApp Support,\n\n"
        "My number +{nomor} can't get verified. Every time I reach the "
        "\"Choose how to verify\" screen, the SMS option is greyed out "
        "with a \"Try again in 13:50:11\" note under it — a countdown of "
        "hours. WhatsApp won't send me an SMS code until it runs out.\n\n"
        "My SIM is fine, other texts arrive. The hold is on WhatsApp's "
        "side, not my carrier. I just need to log into my own account.\n\n"
        "Stuff I already tried:\n"
        "- Waited out the full countdown (more than once)\n"
        "- \"Retry on other device\"\n"
        "- Restarted phone\n"
        "- Reinstalled WhatsApp\n"
        "- Checked with carrier — SMS works\n\n"
        "This is my personal number, never violated any rules.\n\n"
        "Please reset the SMS hold on +{nomor} so I can request a code. "
        "Thanks."
    ),
    (
        "Hey WhatsApp Team,\n\n"
        "I need help — number +{nomor} can't get past verification. On the "
        "\"Choose how to verify\" screen the SMS button is dead with a "
        "\"Try again in {jam} hours\" label. Even after waiting the full "
        "count, a fresh one appears. The Voice call option is there, but "
        "no call ever comes through.\n\n"
        "My SIM works, other texts arrive fine — the hold is on "
        "WhatsApp's side. I just want to get back into my own account.\n\n"
        "Already tried: waiting it out, \"Retry on other device\", "
        "reinstalling, restarting. Nothing unlocks it.\n\n"
        "Can you look into why +{nomor} is held and reset it? Appreciate "
        "it."
    ),
    (
        "Halo WhatsApp Support,\n\n"
        "Nomor saya +{nomor} gak bisa verifikasi. Tiap kali sampe layar "
        "\"Choose how to verify\", opsi SMS-nya abu-abu dengan tulisan "
        "\"Try again in 13:50:11\" kayak gitu — hitungan jam. WhatsApp "
        "gak mau kirim kode SMS sampai hitungan itu habis.\n\n"
        "SIM aktif, HP normal, SMS dari layanan lain masuk. Penahannya "
        "dari sisi WhatsApp, bukan operator.\n\n"
        "Yang udah dicoba:\n"
        "- Nunggu hitungannya habis (lebih dari sekali)\n"
        "- \"Retry on other device\"\n"
        "- Restart HP\n"
        "- Install ulang WhatsApp\n"
        "- Cek ke operator — SMS normal\n\n"
        "Ini nomor pribadi saya, gak pernah langgar aturan apapun.\n\n"
        "Tolong reset penahanan SMS di +{nomor} biar kodenya bisa "
        "dikirim. Makasih."
    ),
    (
        "Halo Tim Support,\n\n"
        "Butuh bantuan — nomor +{nomor} gak bisa verifikasi. Tombol SMS "
        "di layar \"Choose how to verify\" mati, tulisannya \"Try again "
        "in {jam} jam\". Pas hitungannya habis dan saya buka lagi, "
        "muncul hitungan baru. Opsi Voice call ada tapi pas dicoba gak "
        "ada telepon masuk.\n\n"
        "SIM berfungsi normal, SMS dari layanan lain masuk — ini "
        "penahanan dari WhatsApp. Saya cuma mau masuk ke akun sendiri.\n\n"
        "Udah coba: nunggu sampai habis, \"Retry on other device\", "
        "install ulang, restart. Gak ada yang buka kuncinya.\n\n"
        "Bisa tolong cek kenapa +{nomor} kena penahanan ini dan reset "
        "ya? Terima kasih."
    ),
]
_TEXT_FORM = [
    (
        "Hi WhatsApp Support,\n\n"
        "Number +{nomor} can't get verified. The \"Choose how to verify\" "
        "screen locks the SMS option with a \"Try again in {jam} hours\" "
        "note — WhatsApp holds the code back, so registration is stuck. "
        "SIM is active, other texts work fine.\n\n"
        "Already tried: waiting out the countdown, \"Retry on other "
        "device\", reinstalling, restarting — the lock always comes "
        "back.\n\n"
        "Please reset the SMS hold on +{nomor} so the code can be sent. "
        "Thanks."
    ),
    (
        "Hey WhatsApp Team,\n\n"
        "My number +{nomor} can't request a code — the SMS option on the "
        "\"Choose how to verify\" screen is greyed out and says \"Try "
        "again in {jam} hours\". Even after waiting the full time, the "
        "same wait shows up again.\n\n"
        "SIM works, other texts arrive. Please look into this.\n\n"
        "Thanks for the help."
    ),
    (
        "Halo WhatsApp Support,\n\n"
        "Nomor +{nomor} gak bisa verifikasi — opsi SMS di layar \"Choose "
        "how to verify\" terkunci dengan tulisan \"Try again in {jam} "
        "jam\". WhatsApp nahan ngirim kodenya jadi registrasi stuck. SIM "
        "aktif, SMS lain masuk normal.\n\n"
        "Udah coba: nunggu hitungannya habis, \"Retry on other device\", "
        "install ulang, restart — pengunciannya selalu balik lagi.\n\n"
        "Tolong reset penahanan SMS di +{nomor} biar kodenya bisa "
        "dikirim. Makasih."
    ),
    (
        "Halo Tim Support,\n\n"
        "Nomor saya +{nomor} gak bisa request kode — tombol SMS di layar "
        "\"Choose how to verify\" dinonaktifin, tulisannya \"Try again "
        "in {jam} jam\".\n\n"
        "SIM normal, SMS lain masuk. Tolong cek kenapa nomor saya "
        "ditahan.\n\n"
        "Terima kasih."
    ),
]

# ── Komponen komposisi (bikin teks near-unique tanpa marker spammy) ──────────
# Digabung random per kiriman → kombinasi ribuan, tetap natural & santai.
# Semua EN/ID only. Tone: gak terlalu sopan, gak kasar.
# ── Komponen komposisi (English) ──────────────────────────────────────────────
_C_GREETING_EN = [
    "Hi WhatsApp Support,",
    "Hello WhatsApp Team,",
    "Hey WhatsApp Support,",
    "Hi there,",
]
_C_INTRO_EN = [
    "I need help with my number +{nomor}.",
    "Having a verification issue with +{nomor}.",
    "The SMS option on +{nomor} is locked — need help.",
    "Reaching out because +{nomor} can't request a code.",
]
_C_PROBLEM_EN = [
    "On the \"Choose how to verify\" screen, the SMS option is greyed out with a \"Try again in {jam} hours\" note — the code can't be requested at all.",
    "The SMS button on the verify screen is dead — it just says \"Try again in {jam} hours\".",
    "WhatsApp locks the SMS option on my verify screen with a \"{jam} hours\" countdown, so registration is completely stuck.",
]
_C_DETAIL_EN = [
    "SIM is active and other texts arrive fine — the hold is on WhatsApp's side, not my carrier.",
    "Waited out the full countdown and a fresh one appeared.",
    "Tried \"Retry on other device\", restarted, reinstalled — the SMS option stays locked.",
]
_C_LIMIT_EN = [
    "It's been {jam} hours and I still can't request a code.",
    "The countdown has been sitting at {jam} hours — nothing I do unlocks the SMS option.",
    "The hold has been on my number for {jam} hours with the same \"Try again\" note.",
]
_C_ASSURE_EN = [
    "This number is active, never violated any rules.",
    "It's my personal number I use daily. No violations.",
    "Active SIM, legit number, no issues on my end.",
]
_C_REQUEST_EN = [
    "Please reset the SMS hold on my number so the code can be sent.",
    "Can you look into why my number can't request a verification code?",
    "Please unlock SMS verification on +{nomor}.",
]
_C_CLOSING_EN = [
    "Thanks for the help.",
    "Appreciate any help you can give. Thanks.",
    "Thanks for looking into this.",
]

# ── Komponen komposisi (Indonesian) — santai ─────────────────────────────────
_C_GREETING_ID = [
    "Halo WhatsApp Support,",
    "Hi WhatsApp Support,",
    "Halo Tim Support,",
    "Halo WhatsApp,",
]
_C_INTRO_ID = [
    "Butuh bantuan untuk nomor +{nomor}.",
    "Ada masalah verifikasi di nomor +{nomor}.",
    "Opsi verifikasi SMS di +{nomor} terkunci, butuh bantuan.",
    "Mau nanya soal nomor +{nomor} yang gak bisa minta kode.",
]
_C_PROBLEM_ID = [
    "Di layar \"Choose how to verify\", opsi SMS-nya abu-abu dengan tulisan \"Try again in {jam} jam\" — kodenya gak bisa diminta sama sekali.",
    "Tombol SMS di layar verifikasi mati — cuma ada tulisan \"Try again in {jam} jam\".",
    "WhatsApp ngunci opsi SMS di layar verifikasi saya dengan hitungan \"{jam} jam\", jadi registrasi stuck total.",
]
_C_DETAIL_ID = [
    "SIM aktif, SMS layanan lain masuk normal — penahannya dari WhatsApp, bukan operator.",
    "Udah nunggu hitungannya habis dan muncul hitungan baru lagi.",
    "Udah coba \"Retry on other device\", restart, install ulang — opsi SMS-nya tetap terkunci.",
]
_C_LIMIT_ID = [
    "Udah {jam} jam dan saya tetap gak bisa minta kode.",
    "Hitungannya mentok di {jam} jam — gak ada cara buka kuncinya.",
    "Penahannya nyangkut {jam} jam dengan tulisan \"Try again\" yang sama.",
]
_C_ASSURE_ID = [
    "Nomor ini aktif, gak pernah langgar aturan WhatsApp.",
    "Ini nomor pribadi yang dipake sehari-hari. Gak ada pelanggaran.",
    "SIM aktif, nomor sah milik sendiri, gak ada masalah.",
]
_C_REQUEST_ID = [
    "Tolong reset penahanan SMS di nomor saya biar kodenya bisa dikirim.",
    "Bisa tolong cek kenapa nomor saya gak bisa minta kode verifikasi?",
    "Tolong buka lagi verifikasi SMS di +{nomor}.",
]
_C_CLOSING_ID = [
    "Makasih atas bantuannya.",
    "Terima kasih banyak.",
    "Thanks ya, ditunggu bantuannya.",
]

# ── Komponen komposisi multi-bahasa (near-unique, teks panjang) ──────────────
# Tiap bahasa punya set komponen sendiri; dirakit jadi surat 4-5 paragraf.
# Placeholder: {nomor} {jam}. English & internasional diprioritaskan.
_LANG_COMPONENTS = {
    "en": {
        "greeting": _C_GREETING_EN,
        "intro": _C_INTRO_EN,
        "problem": _C_PROBLEM_EN,
        "detail": _C_DETAIL_EN,
        "limit": _C_LIMIT_EN,
        "assure": _C_ASSURE_EN,
        "request": _C_REQUEST_EN,
        "closing": _C_CLOSING_EN,
    },
    "id": {
        "greeting": _C_GREETING_ID,
        "intro": _C_INTRO_ID,
        "problem": _C_PROBLEM_ID,
        "detail": _C_DETAIL_ID,
        "limit": _C_LIMIT_ID,
        "assure": _C_ASSURE_ID,
        "request": _C_REQUEST_ID,
        "closing": _C_CLOSING_ID,
    },
}
_COMPOSE_LANG_WEIGHTS = {"en": 0.50, "id": 0.50}


def _compose_appeal(kind: str, nomor_clean: str, jam: str) -> str:
    """Rakit teks banding panjang dari komponen random multi-bahasa → near-unique.

    Form V1/V2 juga pakai versi panjang (jangan dipendekkan) — masalah SMS/voice
    lock di layar Choose how to verify perlu dijelaskan lengkap.
    """
    langs = list(_COMPOSE_LANG_WEIGHTS.keys())
    weights = list(_COMPOSE_LANG_WEIGHTS.values())
    lang = random.choices(langs, weights=weights, k=1)[0]
    c = _LANG_COMPONENTS[lang]

    g = random.choice(c["greeting"])
    intro = random.choice(c["intro"])
    prob = random.choice(c["problem"])
    limit = random.choice(c["limit"])
    assure = random.choice(c["assure"])
    req = random.choice(c["request"])
    close = random.choice(c["closing"])
    detail = random.choice(c["detail"])
    # Fokus: layar "Choose how to verify" — SMS disabled + hitungan jam.
    screen = (
        "Every time I try to log in or re-register, I get to the \"Choose "
        "how to verify\" screen and the SMS option is greyed out — it just "
        "says \"Try again in {jam} hours\" with a countdown. WhatsApp won't "
        "send me an SMS code until that runs out. I've waited out the full "
        "countdown more than once, tried \"Retry on other device\", "
        "restarted my phone, reinstalled the app — the SMS option stays "
        "locked. The Voice call option is there too, but no call ever came "
        "through. My SIM is active and every other text reaches me "
        "normally, so this is clearly a WhatsApp-side hold on my number, "
        "not a network issue."
        if lang == "en" else
        "Setiap kali saya coba login atau daftar ulang, di layar \"Choose "
        "how to verify\" opsi SMS-nya abu-abu — cuma ada tulisan \"Try "
        "again in {jam} jam\" dengan hitungan mundur. WhatsApp gak mau "
        "kirim kode SMS sampai hitungan itu selesai. Udah lebih dari "
        "sekali saya nunggu sampai habis, coba \"Retry on other device\", "
        "restart HP, install ulang aplikasi — opsi SMS-nya tetap "
        "terkunci. Opsi Voice call juga ada, tapi pas dicoba gak ada "
        "telepon yang masuk. SIM saya aktif dan SMS lain masuk normal, "
        "jadi jelas ini penahanan dari sisi WhatsApp, bukan masalah "
        "jaringan."
    )
    body = (
        f"{g}\n\n{intro} {prob}\n\n{screen}\n\n{detail} {limit}\n\n"
        f"{assure} {req}\n\n{close}"
    )
    return body.replace("{nomor}", nomor_clean).replace("{jam}", jam)


def pick_appeal_text(kind: str, nomor_clean: str, limit_hours: int = 24, template: str | None = None) -> str:
    """Ambil teks banding panjang multi-bahasa untuk FAQ / email / form.

    kind: 'faq' | 'banding' | 'form'. template custom (/set) override semua.
    Default: 90% pool panjang internasional (faq_appeal_texts), 10% komposisi
    komponen (variasi kecil). Placeholder {nomor} {jam} / {hours}.
    """
    jam = str(int(limit_hours) if limit_hours else 24)
    if template:
        t = str(template)
        t = t.replace("{nomor}", nomor_clean).replace("{jam}", jam).replace("{hours}", jam)
        if "{nomor}" not in str(template) and nomor_clean not in t:
            t = t + f"\n\n+{nomor_clean}"
        return t
    # Utama: surat panjang (banding / Form V1 / V2 / V3) — jangan singkat
    if random.random() < 0.95:
        try:
            from faq_appeal_texts import pick_long_appeal
            return pick_long_appeal(nomor_clean, jam)
        except Exception:
            pass
    # Cadangan: komposisi komponen (tetap panjang, EN/ID)
    return _compose_appeal(kind, nomor_clean, jam)


def split_phone_country(nomor_clean: str) -> tuple[str, str]:
    """Return (ISO2 country_selector, national_number) for Form V1."""
    n = _norm_number(nomor_clean)
    try:
        import phonenumbers
        pn = phonenumbers.parse("+" + n, None)
        cc = phonenumbers.region_code_for_number(pn) or "ID"
        return cc, str(pn.national_number)
    except Exception:
        if n.startswith("62"):
            return "ID", n[2:]
        if n.startswith("258"):
            return "MZ", n[3:]
        return "ID", n


def _faq1_extract_tokens(html: str) -> dict:
    """Ambil LSD + token FB dari HTML contact page (harus cocok dengan HAR)."""
    out = {}
    pats = {
        "lsd": [
            r'"LSD",\[\],\{"token":"([^"]+)"\}',
            r'name="lsd"\s+value="([^"]+)"',
            r'"token":"(Ad[^"]+)"',
        ],
        "jazoest": [
            r'name="jazoest"\s+value="([^"]+)"',
            r'"jazoest":"(\d+)"',
        ],
        "__hs": [r'"haste_session":"([^"]+)"', r'"__hs":"([^"]+)"'],
        "__rev": [r'"server_revision":(\d+)', r'"__rev":(\d+)', r'"client_revision":(\d+)'],
        "__hsi": [r'"hsi":"(\d+)"', r'"__hsi":"(\d+)"'],
        "__dyn": [r'"__dyn":"([^"]+)"'],
        "__s": [r'"__s":"([^"]+)"'],
    }
    for key, plist in pats.items():
        for p in plist:
            m = re.search(p, html)
            if m:
                out[key] = m.group(1)
                break
    if out.get("lsd") and not out.get("jazoest"):
        out["jazoest"] = "2" + str(sum(ord(c) for c in out["lsd"]))
    return out


_FAQ1_MOZILLA_UAS = [
    (
        "Mozilla/5.0 (Linux; Android 15; 25062RN2DY Build/AQ3A.250226.002; wv) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.124 "
        "Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Linux; Android 13; Pixel 8) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.6167.101 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.124 Mobile Safari/537.36"
    ),
]


def _faq1_pick_ua() -> tuple[str, str]:
    """random.choice Mozilla browser UA atau WhatsApp app UA. Return (ua, kind)."""
    if random.choice(("mozilla", "wa")) == "wa":
        return build_random_device()["useragent"], "wa"
    return random.choice(_FAQ1_MOZILLA_UAS), "mozilla"


def _faq1_load_proxies(limit: int = 30, db_cur=None, test_all: bool = False) -> list[str]:
    """Load OwlProxy residential proxies from DB with randomized ISO2 countries.
    Falls back to proxy_ok.txt if DB unavailable. Returns proxy URI strings.

    test_all=True  → probe SEMUA proxy, kembalikan hanya yang hidup (dipakai
                     Form V1 yang wajib pakai proxy).
    test_all=False → probe sampel kecil; kalau sehat kembalikan pool penuh."""

    # ISO2 country pool for residential rotation (diverse, excluding US for variety)
    ISO2_POOL = [
        "GB", "DE", "FR", "IT", "ES", "NL", "BE", "SE", "NO", "FI", "DK", "PL",
        "CZ", "AT", "CH", "IE", "PT", "GR", "RO", "HU", "BG", "HR", "SK", "SI",
        "CA", "MX", "BR", "AR", "CL", "CO", "PE", "VE",
        "AU", "NZ", "SG", "MY", "TH", "PH", "ID", "VN", "IN", "PK", "BD",
        "JP", "KR", "TW", "HK",
        "ZA", "NG", "KE", "EG", "MA", "TN", "GH", "UG",
        "AE", "SA", "IL", "TR", "QA", "KW",
    ]

    if db_cur:
        try:
            # Read-only proxy query lewat koneksi SENDIRI (short-lived) supaya
            # aman dari concurrent-cursor-sharing saat 3 fase jalan paralel.
            # (db_cur bisa berupa cursor sqlite biasa yg TIDAK thread-safe.)
            rows = []
            try:
                import sqlite3 as _sq
                _db_path = Path(__file__).resolve().parent / "ivas_bot.db"
                _c = _sq.connect(str(_db_path), timeout=20)
                try:
                    _cur = _c.execute(
                        "SELECT proto, host, port, username, password "
                        "FROM user_proxies "
                        "WHERE host LIKE '%owlproxy.com%' "
                        "ORDER BY id DESC LIMIT 200"
                    )
                    rows = _cur.fetchall()
                finally:
                    _c.close()
            except Exception:
                # Fallback terakhir: pakai db_cur langsung (single-thread saja)
                db_cur.execute(
                    "SELECT proto, host, port, username, password "
                    "FROM user_proxies "
                    "WHERE host LIKE '%owlproxy.com%' "
                    "ORDER BY id DESC LIMIT 200"
                )
                rows = db_cur.fetchall()

            if rows:
                pool = []
                for proto, host, port, username, password in rows:
                    # Randomize country: replace _zone_XX_ with random ISO2
                    # Format: xSRVBhZ6dD20_custom_zone_US_st__city_sid_63142168_time_5
                    new_country = random.choice(ISO2_POOL)
                    new_username = re.sub(
                        r'(_custom_zone_|_zone_)([A-Z]{2})(_)',
                        rf'\g<1>{new_country}\g<3>',
                        username
                    )

                    # Generate fresh session ID to avoid conflicts
                    new_username = re.sub(
                        r'(_sid_)\d+',
                        rf'\g<1>{random.randint(10000000, 99999999)}',
                        new_username
                    )

                    scheme = "socks5h" if proto == "socks5" else "http"
                    uri = f"{scheme}://{new_username}:{password}@{host}:{port}"
                    pool.append(uri)

                if pool:
                    random.shuffle(pool)

                    # Liveness gate. Mode:
                    #  - test_all=True  (Form V1): probe SEMUA proxy, kembalikan
                    #    HANYA yang benar-benar hidup (proxy mati dibuang, jadi
                    #    worker V1 tidak buang waktu ke cred mati).
                    #  - test_all=False (default): probe sampel kecil; kalau ada
                    #    yang hidup, anggap pool sehat & kembalikan semua.
                    def _quick_test(uri: str) -> bool:
                        try:
                            r = requests.get("https://api.ipify.org?format=json",
                                             proxies={"http": uri, "https": uri},
                                             timeout=8, verify=False)
                            return r.status_code == 200
                        except Exception:
                            return False

                    from concurrent.futures import ThreadPoolExecutor, as_completed

                    if test_all:
                        # Di panel datacenter, SOCKS5 sangat lambat — test semua 34
                        # proxy satu per satu bisa timeout. Strategi: test batch 12
                        # proxy paralel, ambil yang hidup dalam 12 detik. Kalau dapat
                        # minimal 1, pakai. Sisanya (yang belum dites) JUGA dimasukkan
                        # ke pool karena kemungkinan besar provider sama → hidup juga.
                        batch_size = min(12, len(pool))
                        batch = pool[:batch_size]
                        alive_list = []
                        with ThreadPoolExecutor(max_workers=min(12, batch_size)) as executor:
                            futs = {executor.submit(_quick_test, u): u for u in batch}
                            for fut in as_completed(futs):
                                if fut.result():
                                    alive_list.append(futs[fut])
                        print(f"[PROXY] owl pool={len(pool)} tested={batch_size} alive={len(alive_list)}", flush=True)
                        if alive_list:
                            # Proxy provider hidup → kembalikan pool PENUH (shuffle),
                            # tidak hanya yang lolos tes (session ID beda = IP beda).
                            random.shuffle(pool)
                            return pool[:max(limit, len(pool))]
                        return []

                    probe = pool[:min(4, len(pool))]
                    alive = 0
                    with ThreadPoolExecutor(max_workers=len(probe)) as executor:
                        futs = [executor.submit(_quick_test, u) for u in probe]
                        for fut in as_completed(futs):
                            if fut.result():
                                alive += 1
                    print(f"[PROXY] owl pool={len(pool)} probe={len(probe)} alive={alive}", flush=True)
                    # Akun OwlProxy mati total (kuota habis) → skip proxy, biar
                    # FAQ1/FAQ2 langsung direct (tidak buang waktu retry proxy mati).
                    if alive == 0:
                        return []
                    # Sehat → kembalikan pool penuh (rotasi handle IP-negara yg mati).
                    return pool[:max(limit, len(pool))]

        except Exception as e:
            print(f"[PROXY] DB load failed: {e}", flush=True)

    # Fallback: load from files
    base_dir = Path(__file__).resolve().parent
    paths = [
        base_dir / "haizhu-register" / "proxy_ok.txt",
        base_dir / "proxy_ok.txt",
        base_dir / "proxies_won.txt",
    ]
    path = None
    for p in paths:
        if p.exists():
            path = p
            break
    if not path:
        return []

    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        proxy_url = line.split()[0].strip()
        if proxy_url.startswith(("socks5://", "socks5h://", "http://", "https://")):
            out.append(proxy_url)
        else:
            parts = proxy_url.split(":")
            if len(parts) >= 4:
                host, port, user = parts[0], parts[1], parts[2]
                password = ":".join(parts[3:])
                out.append(f"http://{user}:{password}@{host}:{port}")
        if len(out) >= limit:
            break
    random.shuffle(out)
    return out


def _faq1_proxy_kwargs(proxy: str | None) -> dict:
    if not proxy:
        return {}
    return {"proxies": {"http": proxy, "https": proxy}}


def _faq1_bootstrap(ua: str, timeout=30, proxy: str | None = None):
    """GET contact page → session cookies + lsd/FB tokens (wajib untuk Form V1)."""
    try:
        from curl_cffi import requests as creq
        try:
            sess = creq.Session(impersonate="chrome120")
        except Exception:
            sess = creq.Session(impersonate="chrome110")
    except Exception:
        sess = requests.Session()
    sess.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    })
    r = sess.get(FAQ1_CONTACT_URL, timeout=timeout, **_faq1_proxy_kwargs(proxy))
    meta = _faq1_extract_tokens(r.text or "")
    meta["status"] = r.status_code
    meta["proxy"] = bool(proxy)
    return sess, meta


def submit_faq1(
    nomor_clean, email, message, ua=None, timeout=40, sess=None,
    lsd=None, jazoest=None, tokens=None, proxy: str | None = None,
):
    """Form V1: POST contact/noclient/async/new/ (dari v2whatsapp.har).

    Sukses jika payload.step == 'submit'. 1 email sender + random emails seperti FAQ2.
    WA sering balas payload.error = 'Something went wrong...' saat IP kena rate-limit.
    Pakai proxy (OwlProxy) + rotate UA Mozilla/WhatsApp untuk bypass.
    """
    ua = ua or _faq1_pick_ua()[0]
    out = {"ok": False, "status": None, "resp": None, "error": None, "ua": ua[:80]}
    try:
        meta = tokens or {}
        if sess is None or not (lsd or meta.get("lsd")):
            sess, meta = _faq1_bootstrap(ua, timeout=timeout, proxy=proxy)
        lsd = lsd or meta.get("lsd")
        jazoest = jazoest or meta.get("jazoest")
        if not lsd:
            out["error"] = f"no lsd (page {meta.get('status')})"
            return out
        cc, national = split_phone_country(nomor_clean)
        data = {
            "country_selector": cc,
            "email": email,
            "email_confirm": email,
            "phone_number": national,
            "platform": "ANDROID",
            "your_message": message,
            "form_type": "",
            "step": "submit",
            "__user": "0",
            "__a": "1",
            "__req": "4",
            "dpr": "3",
            "__ccg": "UNKNOWN",
            "lsd": lsd,
            "jazoest": jazoest or "22121",
        }
        for k in ("__hs", "__rev", "__hsi", "__dyn", "__s"):
            if meta.get(k):
                data[k] = meta[k]
        headers = {
            "User-Agent": ua,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.whatsapp.com",
            "Referer": FAQ1_CONTACT_URL,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "x-fb-lsd": lsd,
            "x-asbd-id": "359341",
            "x-requested-with": "mark.via.gp",
            "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Android WebView";v="150"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }
        r = sess.post(
            FAQ1_URL, data=data, headers=headers, timeout=timeout,
            **_faq1_proxy_kwargs(proxy),
        )
        out["status"] = r.status_code
        text = r.text or ""
        out["resp"] = text[:240]
        clean = text[len("for (;;);"):] if text.startswith("for (;;);") else text
        try:
            j = json.loads(clean)
            payload = j.get("payload") or {}
            out["ok"] = (r.status_code == 200) and (payload.get("step") == "submit")
            # WA sering taruh error di payload.error (bukan top-level)
            if not out["ok"]:
                if isinstance(payload, dict) and payload.get("error"):
                    out["error"] = str(payload.get("error"))[:160]
                elif j.get("error"):
                    out["error"] = f"{j.get('error')}: {j.get('errorSummary', '')}"
        except Exception:
            out["ok"] = False
            out["error"] = "bad json"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def submit_faq2(nomor_clean, email, message, device, timeout=40, proxy: str | None = None):
    """FAQ API ke-2 / Form V2: submit kontak LANGSUNG (tanpa compose) ke WA.

    Field `email` diisi email site.pro kita -> balasan WA support masuk ke mailbox itu.
    Mendukung nomor semua negara (dikirim apa adanya, digit saja). Return dict ringkas.
    """
    debug_json = json.dumps(device["debug_info"], ensure_ascii=False)
    files = {
        "email": (None, email),
        "message": (None, message),
        "phone_number": (None, nomor_clean),
        "platform": (None, "ANDROID"),
        "debug_info": (None, debug_json),
    }
    headers = {"User-Agent": device["useragent"]}
    out = {"ok": False, "status": None, "resp": None, "error": None, "ratelimited": False}
    proxy_kwargs = {"proxies": {"http": proxy, "https": proxy}} if proxy else {}
    try:
        r = _HTTP_SESSION.post(FAQ2_URL, files=files, headers=headers, timeout=timeout, **proxy_kwargs)
        out["status"] = r.status_code
        out["resp"] = (r.text or "")[:200]

        # HTTP 429 = rate-limit regardless of body content (bisa JSON, HTML, atau kosong)
        if r.status_code == 429:
            out["error"] = f"HTTP 429 rate-limited"
            out["ratelimited"] = True
            return out

        try:
            j = r.json()
            # WA Form V2 endpoint bisa balas berbagai format:
            # - {"status": "ok"} → sukses
            # - {"error": "Something went wrong..."} → rate limit
            # - {"errorSummary": "..."} → gagal lain
            # - Redirect ke HTML / empty response
            out["ok"] = (j.get("status") == "ok")
            if not out["ok"]:
                err = j.get("error") or j.get("errorSummary") or ""
                if err:
                    out["error"] = str(err)[:160]
                    low = err.lower()
                    if ("wait" in low) or ("try again" in low) or ("something went wrong" in low):
                        out["ratelimited"] = True
                else:
                    # Tidak ada field error tapi status bukan ok — log full response
                    out["error"] = f"unexpected response: {str(j)[:160]}"
        except (ValueError, KeyError):
            # Response bukan JSON — bisa HTML redirect atau empty
            text = (r.text or "")[:200]
            if r.status_code == 200 and text.strip().startswith(("{", "[")):
                out["ok"] = True
            elif r.status_code in (429, 503):
                # Non-JSON 429/503 tetap dianggap rate-limit
                out["error"] = f"non-json rate-limit (HTTP {r.status_code})"
                out["ratelimited"] = True
            else:
                out["error"] = f"non-json response (HTTP {r.status_code}): {text[:100]}"
    except requests.Timeout:
        out["error"] = "Request timeout"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


RESET_OTP_SUBJECT = "Question about WhatsApp for Android"
# Semua alamat support resmi WhatsApp (banding + form).
SUPPORT_RECIPIENTS = [
    "support@support.whatsapp.com",
    "smb@support.whatsapp.com",
    "smb-iphone@support.whatsapp.com",
    "android_web@support.whatsapp.com",
    "iphone_web@support.whatsapp.com",
    "webclient_web@support.whatsapp.com",
    "smb_web@support.whatsapp.com",
    "businesscomplaints@support.whatsapp.com",
    "accessibility@support.whatsapp.com",
    "ip@whatsapp.com",
    "log_whatsapp@records.whatsapp.com",
]
# Field debug_info yang dimasukkan ke blok --Support Info-- (urut, mirip email asli WA).
_SUPPORT_INFO_KEYS = [
    "App", "Architecture", "Board", "Build", "CPU ABI", "Carrier",
    "Device", "Manufacturer", "Model", "OS", "Product",
    "Radio MCC-MNC", "SIM MCC-MNC", "Version", "useragent",
]


def build_support_info_block(device):
    """Render blok --Support Info-- dari debug_info device (format mirip email asli WA)."""
    d = device["debug_info"]
    lines = ["", "", "--Support Info--"]
    for k in _SUPPORT_INFO_KEYS:
        if k in d:
            lines.append(f"{k}: {d[k]}")
    return "\n".join(lines)


def build_random_support_info(nomor_clean: str, device: dict | None = None) -> str:
    """Random Support Info block — Android atau iOS format, biar bervariasi."""
    from form_v3_mail import build_ios_smb_support_info
    from form_v4_mail import build_v4_support_info
    builders = [
        lambda: build_ios_smb_support_info(nomor_clean),
        lambda: build_v4_support_info(nomor_clean),
    ]
    if device:
        builders.append(lambda: build_support_info_block(device))
    return random.choice(builders)()


def run_reset_otp_pipeline(numbers, db_cur=None, db_conn=None, message_template=None,
                           progress_cb=None, recipients=None, reply_wait=60,
                           senders_per_nomor=None, faq2_total=None, subject=None,
                           mode: str = "hard", limit_hours: int = 24,
                           faq_total=None, faq1_total=None, banding_total=None,
                           faq3_total=None, faq4_total=None):
    """Orchestrator RESET OTP (email banding + Form V1/V2/V3/V4).

    Form V3: Gmail/iCloud SMTP + lampiran logs/foto, default 2 kirim (easy & hard).
    Form V4: iCloud SMTP (dikzxinxz), default 1 kirim (easy & hard).

    progress_cb stages: start, reserve, device, faq1, faq2, faq3, faq4, send_ok, send_err,
    wait_reply, reply, done.
    """
    from sitepro_fix_module import (
        _reserve_available_senders, sitepro_restore_session, sitepro_open_webmail,
        roundcube_get_compose_token, roundcube_send_email, roundcube_send_email_with_files,
        roundcube_read_inbox_folders, _mark_sender_composed, is_whatsapp_support_reply,
    )
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from form_v3_mail import (
        submit_form_v3, imap_max_uid, imap_fetch_new_support_replies,
        get_form_v3_config, FORM_V3_SUBJECT, FORM_V3_RECIPIENTS,
    )
    from form_v4_mail import (
        submit_form_v4, imap_max_uid as imap_max_uid_v4,
        imap_fetch_replies as imap_fetch_replies_v4,
        get_form_v4_config, FORM_V4_SUBJECT, FORM_V4_RECIPIENTS,
    )

    preset = RESET_OTP_PRESETS.get((mode or "hard").lower(), RESET_OTP_PRESETS["hard"])
    faq2_n = int(faq2_total if faq2_total is not None else preset["faq2"])
    faq1_n = int(faq1_total if faq1_total is not None else preset["faq1"])
    faq3_n = int(faq3_total if faq3_total is not None else preset.get("faq3", 1))
    faq4_n = int(faq4_total if faq4_total is not None else preset.get("faq4", 1))
    banding_n = int(banding_total if banding_total is not None else preset["banding"])
    SENDERS_PER_NOMOR = max(1, int(senders_per_nomor if senders_per_nomor else preset["senders"]))
    jam = int(limit_hours) if limit_hours else 24

    rcpts = recipients or SUPPORT_RECIPIENTS
    email_subject = subject or RESET_OTP_SUBJECT

    def _cb(stage, data=None):
        if progress_cb:
            try:
                progress_cb(stage, data or {})
            except Exception:
                pass

    nums = [_norm_number(n) for n in numbers]
    result = {
        "total": len(nums), "success_count": 0, "fail_count": 0, "items": [],
        "mode": (mode or "hard").lower(), "limit_hours": jam,
        "faq1_n": faq1_n, "faq2_n": faq2_n, "faq3_n": faq3_n, "faq4_n": faq4_n,
    }
    _cb("start", {"total": len(nums), "mode": result["mode"], "limit_hours": jam,
                  "faq1": faq1_n, "faq2": faq2_n, "faq3": faq3_n, "faq4": faq4_n,
                  "banding": banding_n})

    senders = (_reserve_available_senders(db_cur, db_conn, limit=len(nums) * SENDERS_PER_NOMOR, server=None)
               if (db_cur and db_conn) else [])
    # Kalau reserve kosong tapi pool masih ada yang ready → biasanya SQLite lock
    # yang sudah ditelan. Coba sekali lagi sebelum anggap pool benar-benar habis.
    if not senders and db_cur and db_conn:
        try:
            from sitepro_fix_module import _pick_available_senders
            probe = _pick_available_senders(db_cur, limit=1, server=None)
            if probe:
                time.sleep(0.5)
                senders = _reserve_available_senders(
                    db_cur, db_conn, limit=len(nums) * SENDERS_PER_NOMOR, server=None
                )
        except Exception:
            pass

    # ── Worker per nomor — jalan paralel ──────────────────────────────────────
    NOMOR_WORKERS = min(len(nums), 10)

    def _process_one_nomor(i, nomor):
        """Worker: proses 1 nomor (email banding + V1 + V2 + poll reply).

        Alokasi sender (easy=7 / hard=12):
          [0 .. banding_n)  → banding, 1 email per sender
          [banding_n]       → email kita untuk Form V1
          [banding_n+1]     → email kita untuk Form V2
        """
        item = {
            "nomor": nomor, "ok": False, "sent_to": [], "error": None,
            "newest_reply": None, "ua": "", "carrier": "",
            "faq1_ok": 0, "faq2_ok": 0, "faq3_ok": 0, "faq4_ok": 0,
            "faq1_results": [], "faq2_results": [], "faq3_results": [], "faq4_results": [],
            "mode": result["mode"], "limit_hours": jam,
        }
        base_idx = i * SENDERS_PER_NOMOR
        nomor_senders = senders[base_idx:base_idx + SENDERS_PER_NOMOR]
        v3_only = False
        if not nomor_senders:
            # Tanpa sender sitepro: tetap bisa Form V3 (Gmail) saja.
            pool_ready = False
            try:
                from sitepro_fix_module import _pick_available_senders
                pool_ready = bool(_pick_available_senders(db_cur, limit=1, server=None)) if db_cur else False
            except Exception:
                pool_ready = False
            if pool_ready:
                item["error"] = "Gagal reservasi sender (database sibuk). Coba ulangi sebentar lagi."
                _cb("send_err", item)
                return item
            if faq3_n <= 0:
                item["error"] = "Tidak ada sender siap (cooldown/pool kosong)"
                _cb("send_err", item)
                return item
            v3_only = True
            print(f"[RESET] {nomor} sender kosong -> Form V3 only", flush=True)

        # Split pool: banding vs form catchers
        if v3_only:
            banding_senders = []
            faq1_sender = faq2_sender = {}
            faq1_email = faq2_email = ""
        else:
            n_band = min(banding_n, max(1, len(nomor_senders) - 2)) if len(nomor_senders) >= 3 else len(nomor_senders)
            banding_senders = nomor_senders[:n_band]
            faq1_sender = nomor_senders[n_band] if len(nomor_senders) > n_band else nomor_senders[0]
            faq2_sender = (
                nomor_senders[n_band + 1] if len(nomor_senders) > n_band + 1
                else (nomor_senders[n_band] if len(nomor_senders) > n_band else nomor_senders[0])
            )
            faq1_email = faq1_sender.get("email") or faq1_sender.get("temp_email") or ""
            faq2_email = faq2_sender.get("email") or faq2_sender.get("temp_email") or ""

        _cb("reserve", {"nomor": nomor, "row_id": (nomor_senders[0].get("row_id") if nomor_senders else None)})


        # Device info untuk UI (segera, biar tidak nunggu banding)
        _ui_dev = build_random_device(use_latest=True)
        item["ua"] = _ui_dev["useragent"]
        item["carrier"] = _ui_dev["debug_info"]["Carrier"]
        _cb("device", {"nomor": nomor, "ua": item["ua"], "carrier": item["carrier"]})

        _sent_lock = threading.Lock()
        # Sender yang sudah gagal send di run ini — skip di nomor berikutnya.
        _failed_senders = set()
        _failed_senders_lock = threading.Lock()
        # Baseline UID per (mailbox_id, folder) — diambil SEBELUM kirim
        base_uid_map = {}  # {(mailbox_id, folder): max_uid}
        sender_ws = {}     # mailbox_id -> webmail session (untuk poll)

        def _open_ws(sender):
            sess, ok = sitepro_restore_session(
                sender.get("phpsessid"),
                email=sender.get("sitepro_email") or sender.get("temp_email"),
                password=sender.get("sitepro_password"),
            )
            ws = None
            if ok and sess:
                ws, _msg = sitepro_open_webmail(sess, sender["mailbox_id"])
            if not ws:
                try:
                    sess2, ok2 = sitepro_restore_session(
                        None,
                        email=sender.get("sitepro_email") or sender.get("temp_email"),
                        password=sender.get("sitepro_password"),
                    )
                    if ok2 and sess2:
                        ws, _msg = sitepro_open_webmail(sess2, sender["mailbox_id"])
                except Exception:
                    pass
            return ws

        def _snapshot_baseline(sender, ws):
            mid = sender.get("mailbox_id")
            try:
                pre_msgs, _ = roundcube_read_inbox_folders(
                    ws, timeout=20, fetch_body=False, body_limit=0,
                )
                for m in (pre_msgs or []):
                    folder = m.get("mbox") or "INBOX"
                    try:
                        uid_i = int(m.get("uid", 0))
                    except Exception:
                        continue
                    key = (mid, folder)
                    if uid_i > base_uid_map.get(key, 0):
                        base_uid_map[key] = uid_i
            except Exception:
                pass

        # Buka webmail + baseline paralel (bukan sequensial) → hemat 10-30s
        watch_senders = []
        seen_mids = set()
        for s in list(banding_senders) + [faq1_sender, faq2_sender]:
            if not s or not s.get("mailbox_id"):
                continue
            mid = s.get("mailbox_id")
            if mid in seen_mids:
                continue
            seen_mids.add(mid)
            watch_senders.append(s)

        def _open_and_baseline(s):
            mid = s.get("mailbox_id")
            ws0 = _open_ws(s)
            if ws0:
                sender_ws[mid] = ws0
                _snapshot_baseline(s, ws0)

        with ThreadPoolExecutor(max_workers=min(len(watch_senders), 5)) as ws_pool:
            list(ws_pool.map(_open_and_baseline, watch_senders))

        # ── FASE BANDING (email) — 1 email per banding_sender ───────────────
        def _do_banding():
            if not banding_senders or banding_n <= 0:
                return

            def _one_sender(s_idx, sender):
                mid = sender.get("mailbox_id")
                with _failed_senders_lock:
                    if mid in _failed_senders:
                        print(f"[BANDING] sender#{s_idx} skipped (sudah gagal di run ini)", flush=True)
                        return
                # Stagger start: 0.5s per sender index supaya SMTP tidak blast sekaligus
                if s_idx > 0:
                    time.sleep(s_idx * 0.5)
                ws = sender_ws.get(mid) or _open_ws(sender)
                if not ws:
                    with _failed_senders_lock:
                        _failed_senders.add(mid)
                    print(f"[BANDING] sender#{s_idx} webmail gagal login, skip", flush=True)
                    return
                sender_ws[mid] = ws
                if (mid, "INBOX") not in base_uid_map:
                    _snapshot_baseline(sender, ws)
                dev = build_random_device(use_latest=True)
                # 1 email per sender banding; recipient round-robin
                rcpt = rcpts[s_idx % len(rcpts)]
                token, from_id, compose_id = roundcube_get_compose_token(ws)
                if not token:
                    print(f"[BANDING] sender#{s_idx} compose token gagal", flush=True)
                    return
                appeal = pick_appeal_text("banding", nomor, jam, message_template)
                body = appeal + build_random_support_info(nomor, dev)
                ok_send = False
                send_err = ""
                # Lampiran sama Form V3 (logs + foto) — dari HAR site.pro upload
                try:
                    from form_v3_mail import get_form_v3_config
                    _v3a = get_form_v3_config()
                    _band_files = [x for x in (_v3a.get("logs"), _v3a.get("photo")) if x]
                except Exception:
                    _band_files = []
                for _retry in range(2):
                    try:
                        if _band_files:
                            ok_send, _smsg = roundcube_send_email_with_files(
                                ws, token, from_id, compose_id, rcpt, body,
                                subject=(FORM_V3_SUBJECT if _band_files else email_subject), file_paths=_band_files,
                            )
                        else:
                            ok_send, _smsg = roundcube_send_email(
                                ws, token, from_id, compose_id, rcpt, body, subject=email_subject,
                            )
                        if not ok_send:
                            send_err = _smsg or "unknown"
                    except Exception as exc:
                        ok_send = False
                        send_err = str(exc)[:80]
                    if ok_send:
                        break
                    time.sleep(1.0)
                if ok_send:
                    with _sent_lock:
                        item["sent_to"].append(rcpt)
                    print(f"[BANDING] sender#{s_idx} sent -> {rcpt}", flush=True)
                else:
                    with _failed_senders_lock:
                        _failed_senders.add(mid)
                    print(f"[BANDING] sender#{s_idx} send failed: {send_err}", flush=True)
                with _SLOT_LOCK:
                    _mark_sender_composed(db_cur, db_conn, sender.get("row_id"), used_for=nomor)

            with ThreadPoolExecutor(max_workers=min(len(banding_senders), 3)) as bpool:
                bfuts = [bpool.submit(_one_sender, k, s)
                         for k, s in enumerate(banding_senders)]
                for bf in as_completed(bfuts):
                    try:
                        bf.result()
                    except Exception:
                        pass

        # ── Form V1 (async/new) — concurrent, WAJIB via proxy ──
        def _do_faq1():
            if not (faq1_email and faq1_n > 0):
                return
            emails_v1 = [faq1_email] + _generate_random_emails(max(0, faq1_n - 1))
            proxy_pool = _faq1_load_proxies(max(faq1_n * 2, 10), db_cur=db_cur, test_all=True)
            faq1_ok = 0
            last_err = None
            _proxy_lock = threading.Lock()
            _proxy_idx = [0]
            print(f"[FAQ1] {nomor} start n={len(emails_v1)} live_proxies={len(proxy_pool)} "
                  f"catcher={faq1_email}", flush=True)

            if not proxy_pool:
                item["faq1_ok"] = 0
                item["faq1_error"] = "proxy habis"
                _cb("faq1", {
                    "nomor": nomor, "ok": False, "count": 0,
                    "total": len(emails_v1), "error": "proxy habis",
                })
                return

            def _next_proxy():
                if not proxy_pool:
                    return None
                with _proxy_lock:
                    px = proxy_pool[_proxy_idx[0] % len(proxy_pool)]
                    _proxy_idx[0] += 1
                    return px

            def _one_faq1(em_addr):
                web_ua, ua_kind = _faq1_pick_ua()
                proxy = _next_proxy()
                err_local = None
                sess1 = meta1 = lsd1 = None
                for _try in range(min(2, len(proxy_pool))):
                    try:
                        sess1, meta1 = _faq1_bootstrap(web_ua, proxy=proxy)
                        lsd1 = meta1.get("lsd")
                        if lsd1:
                            break
                    except Exception as e:
                        err_local = str(e)[:120]
                        sess1 = meta1 = lsd1 = None
                    proxy = _next_proxy()
                if not (sess1 and lsd1):
                    return {"ok": False, "error": err_local or "no lsd",
                            "ua_kind": ua_kind, "proxy": bool(proxy)}
                msg_v1 = pick_appeal_text("faq", nomor, jam, message_template)
                r = submit_faq1(
                    nomor, em_addr, msg_v1, ua=web_ua,
                    sess=sess1, lsd=lsd1, jazoest=meta1.get("jazoest"),
                    tokens=meta1, proxy=proxy,
                )
                if not r.get("ok"):
                    try:
                        proxy2 = _next_proxy()
                        web_ua2, ua_kind2 = _faq1_pick_ua()
                        sess2, meta2 = _faq1_bootstrap(web_ua2, proxy=proxy2)
                        if meta2.get("lsd"):
                            r = submit_faq1(
                                nomor, em_addr, msg_v1, ua=web_ua2,
                                sess=sess2, lsd=meta2.get("lsd"),
                                jazoest=meta2.get("jazoest"), tokens=meta2, proxy=proxy2,
                            )
                            ua_kind, proxy = ua_kind2, proxy2
                    except Exception:
                        pass
                return {"ok": r.get("ok"), "status": r.get("status"),
                        "error": r.get("error"), "ua_kind": ua_kind, "proxy": bool(proxy)}

            with ThreadPoolExecutor(max_workers=FAQ1_WORKERS) as pool:
                futs = {pool.submit(_one_faq1, em): em for em in emails_v1}
                for fut in as_completed(futs):
                    try:
                        res1 = fut.result()
                    except Exception as e:
                        res1 = {"ok": False, "error": str(e)[:120]}
                    item["faq1_results"].append(res1)
                    if res1.get("error"):
                        last_err = res1["error"]
                    if res1.get("ok"):
                        faq1_ok += 1
            item["faq1_ok"] = faq1_ok
            if last_err and faq1_ok == 0:
                item["faq1_error"] = last_err
            with _SLOT_LOCK:
                _mark_sender_composed(db_cur, db_conn, faq1_sender.get("row_id"), used_for=nomor)
            _cb("faq1", {
                "nomor": nomor, "ok": faq1_ok > 0, "count": faq1_ok,
                "total": len(emails_v1), "error": last_err,
            })

        # ── Form V2 (verifikasi) — TEMP MAIL: 1 mailbox temp-mail.org per submit ──
        # Tiap submit pakai identitas email berbeda (biar tidak pola 1-email-spam),
        # balasan WA dipoll dari semua mailbox yang dipakai. Fallback ke catcher
        # sitepro kalau temp-mail.org tidak terjangkau.
        def _do_faq2():
            if not (faq2_n > 0):
                return

            # Import malam-malam biar modul optional (fallback tetap jalan).
            try:
                from tempmail_module import (tempmail_new_mailbox,
                                             tempmail_list_messages,
                                             tempmail_get_body)
            except Exception as _tm_err:
                tempmail_new_mailbox = None
                print(f"[FAQ2] tempmail module unavailable: {_tm_err}", flush=True)

            # ── Proxy pool (sama kayak FAQ1 — residential rotate) ──
            faq2_proxy_pool = _faq1_load_proxies(max(faq2_n * 2, 8), db_cur=db_cur,
                                                  test_all=False)
            _faq2_px_lock = threading.Lock()
            _faq2_px_idx = [0]

            def _next_faq2_proxy():
                if not faq2_proxy_pool:
                    return None
                with _faq2_px_lock:
                    px = faq2_proxy_pool[_faq2_px_idx[0] % len(faq2_proxy_pool)]
                    _faq2_px_idx[0] += 1
                    return px

            # ── Email pool: 3 temp mail (pollable) + selebihnya filler ──
            TEMPMAIL_SLOTS = min(3, faq2_n)
            FILLER_SLOTS = max(0, faq2_n - TEMPMAIL_SLOTS)
            _tempmail_emails = []   # list of (mailbox, token) — bisa di-poll balasan

            # Pre-create temp mailboxes (parallel, cepat)
            if tempmail_new_mailbox is not None and TEMPMAIL_SLOTS > 0:
                def _mk_mb(_i):
                    try:
                        return tempmail_new_mailbox()
                    except Exception:
                        return {"ok": False}
                with ThreadPoolExecutor(max_workers=3) as mb_pool:
                    for res in mb_pool.map(_mk_mb, range(TEMPMAIL_SLOTS)):
                        if res.get("ok"):
                            _tempmail_emails.append((res["mailbox"], res["token"]))

            # Extract domain from first temp mailbox for filler emails
            _filler_domain = _ZOCKSHOP_DOMAIN  # fallback
            if _tempmail_emails:
                _first_mb = _tempmail_emails[0][0]
                if "@" in _first_mb:
                    _filler_domain = _first_mb.split("@")[-1]
            _filler_emails = _generate_filler_emails(FILLER_SLOTS, _filler_domain)

            # Build ordered email list: temp mails first, then filler
            _email_queue = []
            for mb, tok in _tempmail_emails:
                _email_queue.append({"addr": mb, "token": tok, "kind": "tempmail"})
            for fk in _filler_emails:
                _email_queue.append({"addr": fk, "token": None, "kind": "filler"})
            # Fallback: kalau semua gagal, isi slot kosong pakai catcher/filler
            while len(_email_queue) < faq2_n:
                fallback = faq2_email or _generate_filler_emails(1, _filler_domain)[0]
                _email_queue.append({"addr": fallback, "token": None, "kind": "fallback"})

            n_tm = len(_tempmail_emails)
            n_filler = len(_filler_emails)
            print(f"[FAQ2] {nomor} start n={faq2_n} tempmail={n_tm} filler={n_filler} "
                  f"domain={_filler_domain} proxies={len(faq2_proxy_pool)}", flush=True)

            _submit_lock = threading.Lock()
            _last_submit = [0.0]
            MIN_GAP = 1.0
            _consecutive_rl = [0]
            _faq2_start = time.time()
            FAQ2_TOTAL_TIMEOUT = max(120, int(faq2_n * 10))
            _mailboxes = []          # [(mailbox, token)] temp mails yang submit OK
            _mb_lock = threading.Lock()

            def _throttled_submit(idx):
                """1 submit per email slot. Return dict hasil."""
                slot = _email_queue[idx]
                email_addr = slot["addr"]

                msg = pick_appeal_text("form", nomor, jam, message_template)
                r = {"ok": False, "error": "no attempt", "mailbox": email_addr}
                for attempt in range(3):
                    if time.time() - _faq2_start > FAQ2_TOTAL_TIMEOUT:
                        r["error"] = "faq2 total timeout"
                        return r
                    with _submit_lock:
                        dyn_gap = MIN_GAP + (_consecutive_rl[0] * 0.6)
                        gap = min(dyn_gap, 4.0) + random.uniform(0.1, 0.5)
                        wait = _last_submit[0] + gap - time.time()
                        if wait > 0:
                            time.sleep(wait)
                        _last_submit[0] = time.time()
                    px = _next_faq2_proxy()
                    try:
                        r = submit_faq2(nomor, email_addr, msg,
                                        build_random_device(), proxy=px)
                        r["mailbox"] = email_addr
                    except Exception as e:
                        r = {"ok": False, "mailbox": email_addr,
                             "error": str(e)[:120], "ratelimited": True}
                    if r.get("ok"):
                        with _submit_lock:
                            _consecutive_rl[0] = max(0, _consecutive_rl[0] - 1)
                        # Track temp-mail yang sukses (buat poll balasan nanti)
                        if slot["token"]:
                            with _mb_lock:
                                _mailboxes.append((email_addr, slot["token"]))
                        return r
                    if r.get("ratelimited"):
                        with _submit_lock:
                            _consecutive_rl[0] += 1
                        backoff = min((2 ** (attempt + 1)) + random.uniform(0.3, 1.0), 6)
                        time.sleep(backoff)
                        continue
                    return r
                return r

            faq2_ok = 0
            rl_count = 0
            non_rl_errors = []
            with ThreadPoolExecutor(max_workers=FAQ2_WORKERS) as pool:
                futs = {pool.submit(_throttled_submit, i): i for i in range(faq2_n)}
                for fut in as_completed(futs):
                    try:
                        r = fut.result()
                        item["faq2_results"].append(
                            {"ok": r.get("ok"), "status": r.get("status"),
                             "mailbox": r.get("mailbox"),
                             "error": r.get("error"), "resp": r.get("resp", "")[:100]})
                        if r.get("ok"):
                            faq2_ok += 1
                        elif r.get("ratelimited"):
                            rl_count += 1
                        else:
                            non_rl_errors.append(r.get("error") or r.get("resp", "")[:80])
                    except Exception as exc:
                        item["faq2_results"].append({"ok": False, "error": str(exc)[:80]})
                        non_rl_errors.append(str(exc)[:80])
            item["faq2_ok"] = faq2_ok

            # Jawaban WA ditampung ke sini untuk tahap balasan.
            item.setdefault("faq2_replies", [])

            # ── Poll balasan WA dari semua temp mailbox (parallel, 1x per mailbox) ──
            if _mailboxes and faq2_ok > 0:
                def _poll_one(mb_tok):
                    mailbox, token = mb_tok
                    deadline = time.time() + 75     # poll max ~75 detik
                    while time.time() < deadline:
                        time.sleep(5)
                        try:
                            for m in tempmail_list_messages(token):
                                low = (str(m.get("from") or "") + " " +
                                       str(m.get("subject") or "")).lower()
                                if "whatsapp" in low or "support" in low:
                                    return {"mailbox": mailbox, "token": token, "msg": m}
                        except Exception:
                            return None
                    return None

                try:
                    with ThreadPoolExecutor(max_workers=4) as poll_pool:
                        for res in poll_pool.map(_poll_one, list(_mailboxes)):
                            if not res:
                                continue
                            try:
                                full = tempmail_get_body(res["token"], res["msg"]["_id"])
                                item["faq2_replies"].append({
                                    "mailbox": res["mailbox"],
                                    "subject": res["msg"].get("subject"),
                                    "from": res["msg"].get("from"),
                                    "text": (full.get("text") or "")[:1500],
                                })
                            except Exception:
                                pass
                    if item["faq2_replies"]:
                        print(f"[FAQ2] {nomor} {len(item['faq2_replies'])} balasan WA "
                              f"via temp-mail", flush=True)
                except Exception as e:
                    print(f"[FAQ2] poll temp-mail error: {e}", flush=True)

            if faq2_ok == 0 and rl_count:
                item["faq2_error"] = "WA rate-limit (IP panel) — coba lagi nanti"
            elif faq2_ok == 0 and non_rl_errors:
                item["faq2_error"] = f"V2 gagal (bukan ratelimit): {non_rl_errors[0]}"
            err_sample = non_rl_errors[0] if non_rl_errors else ""
            n_mb = len(_mailboxes)
            print(f"[FAQ2] {nomor} ok={faq2_ok}/{faq2_n} ratelimited={rl_count} "
                  f"tempmail={n_mb} mailboxes"
                  f"{' err=' + err_sample if err_sample else ''}", flush=True)
            _cb("faq2", {"nomor": nomor, "ok": faq2_ok > 0, "count": faq2_ok, "total": faq2_n})

        # ── Form V3 (Gmail/iCloud SMTP + logs/foto) — 2 kirim ──
        v3_cfg = get_form_v3_config()
        v3_imap_base = [0]

        def _do_faq3():
            if faq3_n <= 0:
                return
            if not (v3_cfg.get("user") and v3_cfg.get("password")):
                item["faq3_error"] = "form_v3.env belum diisi (IMAP_USER/IMAP_PASS)"
                print(f"[FAQ3] {nomor} skip: no credentials", flush=True)
                _cb("faq3", {"nomor": nomor, "ok": False, "count": 0, "total": faq3_n,
                             "error": item["faq3_error"]})
                return
            try:
                v3_imap_base[0] = imap_max_uid(v3_cfg)
            except Exception:
                v3_imap_base[0] = 0
            appeal = pick_appeal_text("banding", nomor, jam, message_template)
            print(f"[FAQ3] {nomor} start n={faq3_n} from={v3_cfg.get('user')} "
                  f"imap_base={v3_imap_base[0]}", flush=True)
            r = submit_form_v3(
                nomor, appeal, send_count=faq3_n,
                recipients=FORM_V3_RECIPIENTS,
                subject=FORM_V3_SUBJECT, cfg=v3_cfg,
            )
            item["faq3_ok"] = int(r.get("ok_count") or 0)
            item["faq3_results"] = r.get("results") or []
            if r.get("ok"):
                for rr in (r.get("results") or []):
                    if rr.get("ok") and rr.get("to"):
                        with _sent_lock:
                            item["sent_to"].append(rr["to"])
            else:
                item["faq3_error"] = r.get("error") or "Form V3 gagal"
            print(f"[FAQ3] {nomor} ok={item['faq3_ok']}/{faq3_n}", flush=True)
            _cb("faq3", {
                "nomor": nomor, "ok": item["faq3_ok"] > 0,
                "count": item["faq3_ok"], "total": faq3_n,
                "error": item.get("faq3_error"),
            })

        # ── Form V4 (iCloud SMTP — dikzxinxz) — 1 kirim ──
        v4_cfg = get_form_v4_config()
        v4_imap_base = [0]

        def _do_faq4():
            if faq4_n <= 0:
                return
            if not (v4_cfg.get("user") and v4_cfg.get("password")):
                item["faq4_error"] = "form_v4.env belum diisi (FORM_V4_USER/FORM_V4_PASS)"
                print(f"[FAQ4] {nomor} skip: no credentials", flush=True)
                _cb("faq4", {"nomor": nomor, "ok": False, "count": 0, "total": faq4_n,
                             "error": item["faq4_error"]})
                return
            try:
                v4_imap_base[0] = imap_max_uid_v4(v4_cfg)
            except Exception:
                v4_imap_base[0] = 0
            appeal = pick_appeal_text("banding", nomor, jam, message_template)
            print(f"[FAQ4] {nomor} start n={faq4_n} from={v4_cfg.get('user')} "
                  f"imap_base={v4_imap_base[0]}", flush=True)
            r = submit_form_v4(
                nomor, appeal, send_count=faq4_n,
                recipients=FORM_V4_RECIPIENTS,
                subject=FORM_V4_SUBJECT, cfg=v4_cfg,
            )
            item["faq4_ok"] = int(r.get("ok_count") or 0)
            item["faq4_results"] = r.get("results") or []
            if r.get("ok"):
                for rr in (r.get("results") or []):
                    if rr.get("ok") and rr.get("to"):
                        with _sent_lock:
                            item["sent_to"].append(rr["to"])
            else:
                item["faq4_error"] = r.get("error") or "Form V4 gagal"
            print(f"[FAQ4] {nomor} ok={item['faq4_ok']}/{faq4_n}", flush=True)
            _cb("faq4", {
                "nomor": nomor, "ok": item["faq4_ok"] > 0,
                "count": item["faq4_ok"], "total": faq4_n,
                "error": item.get("faq4_error"),
            })

        # ── Jalankan 5 fase paralel: banding + FAQ1 + FAQ2 + FAQ3 + FAQ4 ──
        with ThreadPoolExecutor(max_workers=5) as phase_pool:
            phase_futs = [
                phase_pool.submit(_do_banding),
                phase_pool.submit(_do_faq1),
                phase_pool.submit(_do_faq2),
                phase_pool.submit(_do_faq3),
                phase_pool.submit(_do_faq4),
            ]
            for pf in as_completed(phase_futs):
                try:
                    pf.result()
                except Exception as e:
                    print(f"[PHASE] error: {e}", flush=True)

        if not item["sent_to"] and not item.get("faq3_ok") and not item.get("faq4_ok") and not item.get("faq1_ok") and not item.get("faq2_ok"):
            item["error"] = "Email/form tidak terkirim ke alamat manapun"
            _cb("send_err", item)
            return item

        item["ok"] = True
        _cb("send_ok", {"nomor": nomor, "sent_to": item["sent_to"],
                        "faq1": item["faq1_ok"], "faq2": item["faq2_ok"],
                        "faq3": item["faq3_ok"], "faq4": item["faq4_ok"]})

        _cb("wait_reply", {"nomor": nomor})
        # Refresh session webmail yang putus sebelum polling
        for s in watch_senders:
            mid = s.get("mailbox_id")
            if mid not in sender_ws or not sender_ws.get(mid):
                ws = _open_ws(s)
                if ws:
                    sender_ws[mid] = ws

        deadline = time.time() + reply_wait
        _imap_v3_last = [0.0]  # last poll timestamp
        _imap_v4_last = [0.0]
        _imap_v3_fails = [0]
        _imap_v4_fails = [0]
        IMAP_POLL_GAP = 12       # min seconds between IMAP polls
        IMAP_MAX_FAILS = 3       # stop polling after N consecutive fails

        while time.time() < deadline and not item["newest_reply"]:
            # Balasan yang sudah tertangkap _do_faq2 via temp-mail → langsung pakai.
            if item.get("faq2_replies"):
                r0 = item["faq2_replies"][0]
                item["newest_reply"] = {
                    "subject": r0.get("subject") or "WhatsApp Support",
                    "from": r0.get("from") or "WhatsApp Support",
                    "body": r0.get("text") or "",
                }
                _cb("reply", {"nomor": nomor,
                              "subject": r0.get("subject") or "WhatsApp Support",
                              "via": "faq2_tempmail"})
                break
            # Poll Gmail/iCloud Form V3 dulu (balasan masuk ke IMAP_USER)
            if (v3_cfg.get("user") and v3_cfg.get("password")
                    and _imap_v3_fails[0] < IMAP_MAX_FAILS
                    and time.time() - _imap_v3_last[0] >= IMAP_POLL_GAP):
                _imap_v3_last[0] = time.time()
                try:
                    for m in imap_fetch_new_support_replies(v3_imap_base[0], v3_cfg):
                        item["newest_reply"] = {
                            "subject": m.get("subject", ""),
                            "from": m.get("from", ""),
                            "body": m.get("body", ""),
                        }
                        _cb("reply", {"nomor": nomor, "subject": m.get("subject", ""),
                                      "via": "form_v3_imap"})
                        break
                    _imap_v3_fails[0] = 0
                except Exception as e:
                    _imap_v3_fails[0] += 1
                    if _imap_v3_fails[0] >= IMAP_MAX_FAILS:
                        print(f"[FAQ3] IMAP poll disabled after {IMAP_MAX_FAILS} fails: {e}", flush=True)
            if item["newest_reply"]:
                break
            # Poll iCloud Form V4 (balasan masuk ke FORM_V4_USER)
            if (v4_cfg.get("user") and v4_cfg.get("password")
                    and _imap_v4_fails[0] < IMAP_MAX_FAILS
                    and time.time() - _imap_v4_last[0] >= IMAP_POLL_GAP):
                _imap_v4_last[0] = time.time()
                try:
                    for m in imap_fetch_replies_v4(v4_imap_base[0], v4_cfg):
                        item["newest_reply"] = {
                            "subject": m.get("subject", ""),
                            "from": m.get("from", ""),
                            "body": m.get("body", ""),
                        }
                        _cb("reply", {"nomor": nomor, "subject": m.get("subject", ""),
                                      "via": "form_v4_imap"})
                        break
                    _imap_v4_fails[0] = 0
                except Exception as e:
                    _imap_v4_fails[0] += 1
                    if _imap_v4_fails[0] >= IMAP_MAX_FAILS:
                        print(f"[FAQ4] IMAP poll disabled after {IMAP_MAX_FAILS} fails: {e}", flush=True)
            if item["newest_reply"]:
                break
            for s in watch_senders:
                if item["newest_reply"]:
                    break
                mid = s.get("mailbox_id")
                ws_s = sender_ws.get(mid)
                if not ws_s:
                    ws_s = _open_ws(s)
                    if ws_s:
                        sender_ws[mid] = ws_s
                if not ws_s:
                    continue
                try:
                    msgs, _ = roundcube_read_inbox_folders(
                        ws_s, timeout=30, body_limit=2, min_uid=0,
                    )
                except Exception:
                    try:
                        ws_s = _open_ws(s)
                        if ws_s:
                            sender_ws[mid] = ws_s
                            msgs, _ = roundcube_read_inbox_folders(
                                ws_s, timeout=30, body_limit=2, min_uid=0,
                            )
                        else:
                            msgs = []
                    except Exception:
                        msgs = []
                for m in sorted(msgs or [], key=lambda x: int(x.get("uid", 0))):
                    try:
                        uid_i = int(m.get("uid", 0))
                    except Exception:
                        continue
                    folder = m.get("mbox") or "INBOX"
                    base = int(base_uid_map.get((mid, folder), 0) or 0)
                    if uid_i <= base:
                        continue
                    if not is_whatsapp_support_reply(m):
                        continue
                    item["newest_reply"] = {
                        "subject": m.get("subject", ""), "from": m.get("from", ""),
                        "body": m.get("body", ""),
                    }
                    _cb("reply", {"nomor": nomor, "subject": m.get("subject", "")})
                    break
            if not item["newest_reply"]:
                time.sleep(5)
        return item

    # ── Dispatch workers ──────────────────────────────────────────────────────
    if len(nums) == 1:
        item = _process_one_nomor(0, nums[0])
        if item.get("ok"):
            result["success_count"] += 1
        else:
            result["fail_count"] += 1
        result["items"].append(item)
    else:
        with ThreadPoolExecutor(max_workers=NOMOR_WORKERS) as nomor_pool:
            futs = {nomor_pool.submit(_process_one_nomor, i, n): n for i, n in enumerate(nums)}
            for fut in as_completed(futs):
                try:
                    item = fut.result()
                except Exception as e:
                    n = futs[fut]
                    item = {"nomor": n, "ok": False, "error": str(e), "sent_to": [],
                            "faq1_ok": 0, "faq2_ok": 0, "faq3_ok": 0, "faq4_ok": 0,
                            "faq1_results": [], "faq2_results": [], "faq3_results": [], "faq4_results": [],
                            "newest_reply": None, "ua": "", "carrier": ""}
                if item.get("ok"):
                    result["success_count"] += 1
                else:
                    result["fail_count"] += 1
                result["items"].append(item)

    _cb("done", result)
    return result


if __name__ == "__main__":
    import sqlite3
    nums = sys.argv[1:] or ["6285825306013"]
    try:
        _conn = sqlite3.connect(r"e:\ivas\ivas_bot.db", timeout=20, check_same_thread=False)
        _cur = _conn.cursor()
    except Exception:
        _conn = _cur = None

    def _p(stage, data):
        print(f"  [{stage}] {data}")

    out = run_reset_otp_pipeline(nums, db_cur=_cur, db_conn=_conn, progress_cb=_p)
    print("\nRESULT:", json.dumps({k: v for k, v in out.items() if k != "items"}, indent=2))
    for it in out["items"]:
        rep = it.get("newest_reply")
        print(f"  +{it['nomor']}: ok={it['ok']} sent={len(it['sent_to'])} "
              f"reply={'YES' if rep else 'no'}"
              + (f" subj={rep['subject']}" if rep else ""))
    if _conn:
        _conn.close()
