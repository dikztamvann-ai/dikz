"""form_v4_mail.py — Outlook/Hotmail SMTP + IMAP (test basic auth).

NOTE: Microsoft disable basic auth untuk IMAP/SMTP sejak Okt 2022.
App password hanya jalan kalau akun masih enable legacy auth atau pakai
workaround tertentu. Script ini untuk TEST dulu — kalau gagal, perlu OAuth2.

Config: form_v4.env atau env OS
  FORM_V4_USER=xxx@outlook.com
  FORM_V4_PASS=app_password_here
  FORM_V4_SMTP_HOST=smtp-mail.outlook.com  (default)
  FORM_V4_IMAP_HOST=outlook.office365.com  (default)
"""
from __future__ import annotations

import email
import imaplib
import os
import random
import smtplib
import ssl
import time
import uuid
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_ENV_PATH = _DIR / "form_v4.env"
_ASSETS = _DIR / "assets"

FORM_V4_SUBJECT = "Question about WhatsApp Business for iPhone"
# iCloud strict soal recipients — kirim 1 per 1, urut dari yang paling jarang
# di-reject. smb-iphone sering kena HM108 → taruh terakhir.
FORM_V4_RECIPIENTS = [
    "smb@support.whatsapp.com",
    "android_web@support.whatsapp.com",
    "iphone_web@support.whatsapp.com",
    "smb_web@support.whatsapp.com",
    "support@support.whatsapp.com",
    "smb-iphone@support.whatsapp.com",
]

_PROVIDERS = {
    "icloud.com": {
        "smtp_host": "smtp.mail.me.com",
        "smtp_port": 587,
        "imap_host": "imap.mail.me.com",
        "imap_port": 993,
    },
    "me.com": {
        "smtp_host": "smtp.mail.me.com",
        "smtp_port": 587,
        "imap_host": "imap.mail.me.com",
        "imap_port": 993,
    },
    "mac.com": {
        "smtp_host": "smtp.mail.me.com",
        "smtp_port": 587,
        "imap_host": "imap.mail.me.com",
        "imap_port": 993,
    },
    "outlook.com": {
        "smtp_host": "smtp-mail.outlook.com",
        "smtp_port": 587,
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
    },
    "hotmail.com": {
        "smtp_host": "smtp-mail.outlook.com",
        "smtp_port": 587,
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
    },
    "live.com": {
        "smtp_host": "smtp-mail.outlook.com",
        "smtp_port": 587,
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
    },
    "msn.com": {
        "smtp_host": "smtp-mail.outlook.com",
        "smtp_port": 587,
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
    },
}


# ── Random device/locale data untuk Support Info ──
_IPHONE_MODELS = [
    "iPhone 13 Pro Max", "iPhone 14 Pro", "iPhone 14 Pro Max",
    "iPhone 15 Pro", "iPhone 15 Pro Max", "iPhone 16 Pro",
]
_IOS_VERSIONS = ["18.4", "18.5", "18.6", "18.7", "26.0", "26.1"]
_WA_SMB_VERSIONS = ["2.26.25.77", "2.26.26.80", "2.26.27.81", "2.26.28.85"]

# Locale pairs: (LC country, LG language) — random biar tidak selalu VN/en.
_LOCALES = [
    ("ID", "id"), ("US", "en"), ("GB", "en"), ("IN", "en"),
    ("PH", "en"), ("MY", "ms"), ("SG", "en"), ("TH", "th"),
    ("VN", "vi"), ("BR", "pt"), ("MX", "es"), ("EG", "ar"),
    ("PK", "ur"), ("BD", "bn"), ("LK", "si"), ("MM", "my"),
    ("KH", "km"), ("LA", "lo"), ("NP", "ne"), ("NG", "en"),
    ("KE", "sw"), ("ZA", "en"), ("AU", "en"), ("NZ", "en"),
    ("CA", "en"), ("IE", "en"), ("DE", "de"), ("FR", "fr"),
    ("IT", "it"), ("ES", "es"), ("PT", "pt"), ("NL", "nl"),
    ("SE", "sv"), ("NO", "no"), ("DK", "da"), ("FI", "fi"),
    ("JP", "ja"), ("KR", "ko"), ("TW", "zh"), ("HK", "zh"),
]


def _load_env_file(path: Path = _ENV_PATH) -> dict:
    out = {}
    if not path.exists():
        return out
    try:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def get_form_v4_config() -> dict:
    file_cfg = _load_env_file()
    user = (
        os.environ.get("FORM_V4_USER")
        or file_cfg.get("FORM_V4_USER")
        or ""
    ).strip()
    password = (
        os.environ.get("FORM_V4_PASS")
        or file_cfg.get("FORM_V4_PASS")
        or ""
    ).strip()

    domain = user.split("@")[-1].lower() if "@" in user else "icloud.com"
    prov = dict(_PROVIDERS.get(domain, _PROVIDERS["icloud.com"]))

    smtp_host = (
        os.environ.get("FORM_V4_SMTP_HOST")
        or file_cfg.get("FORM_V4_SMTP_HOST")
        or prov["smtp_host"]
    )
    imap_host = (
        os.environ.get("FORM_V4_IMAP_HOST")
        or file_cfg.get("FORM_V4_IMAP_HOST")
        or prov["imap_host"]
    )
    smtp_port = int(os.environ.get("FORM_V4_SMTP_PORT") or file_cfg.get("FORM_V4_SMTP_PORT") or prov["smtp_port"])
    imap_port = int(os.environ.get("FORM_V4_IMAP_PORT") or file_cfg.get("FORM_V4_IMAP_PORT") or prov["imap_port"])

    return {
        "user": user,
        "password": password.replace(" ", ""),
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "imap_host": imap_host,
        "imap_port": imap_port,
        "domain": domain,
    }


def _dialing_code_and_pn(nomor_clean: str) -> tuple[str, str]:
    """CCode + pn untuk Support Info iOS."""
    n = "".join(c for c in str(nomor_clean or "") if c.isdigit())
    try:
        import phonenumbers
        pn = phonenumbers.parse("+" + n, None)
        return str(pn.country_code), str(pn.national_number)
    except Exception:
        if n.startswith("62") and len(n) >= 10:
            return "62", n[2:]
        if n.startswith("94"):
            return "94", n[2:] if len(n) > 2 else n
        return n[:2] or "62", n


def build_v4_support_info(nomor_clean: str) -> str:
    """Blok Support Info mirip dump WhatsApp Business iPhone — random per kirim."""
    cc, national = _dialing_code_and_pn(nomor_clean)
    model = random.choice(_IPHONE_MODELS)
    ios = random.choice(_IOS_VERSIONS)
    ver = random.choice(_WA_SMB_VERSIONS)
    lc, lg = random.choice(_LOCALES)
    model_ua = model.replace(" ", "_")
    anid = str(uuid.uuid4()).upper()
    build_id = str(random.randint(1008000000, 1009999999))
    hash_id = str(random.randint(100000000000, 999999999999))
    free_bytes = random.randint(60_000_000_000, 128_000_000_000)
    free_mb = free_bytes // (1024 * 1024)
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + "+0000"

    ua = f"WhatsApp/{ver} SMB iOS/{ios} Device/{model_ua}"
    lines = [
        "",
        "",
        "--[[ ‎Support info ]]--",
        "Debug info: unregistered",
        f"CCode: {cc}",
        f"pn: {national}",
        f"Hash: {hash_id}",
        f"Mobile Build Id: {build_id}",
        f"Version: {ver}",
        "Target: release",
        f"LC: {lc}",
        f"LG: {lg}",
        "Context: deeplink",
        "Carrier: No subscription",
        "Manufacturer: Apple",
        f"Model: {model}",
        f"OS: {ios}",
        f"UserAgent: {ua}",
        "Socket Conn: DN",
        "Connection: none",
        "Last VoIP call blocked: No",
        "Network Type: Unknown",
        "Datacenter: atn",
        "Radio MCC-MNC: N/A",
        "SIM MCC-MNC: 000-000",
        f"Free Space Built-In: {free_bytes} ({free_mb} MB)",
        "Free Space Removable: Not Present",
        "Server Status: unknown",
        "FAQ Results Returned: 10",
        "FAQ Results Read: 0",
        "Cached Connection FAQ: no",
        "Cached Connection FAQ Time Read: n/a",
        f"Device ISO8601: {now}",
        f"Smb count: {random.randint(50, 500)}",
        f"Ent count: {random.randint(10, 80)}",
        "Interface: WiFi/None",
        f"DA: {random.randint(5, 30)}",
        "DB corrupted: false",
        "Backup: off",
        "Video Calls: enabled",
        "Payments: false",
        "VoiceOver: false",
        "Larger Text: false",
        "DeviceID: 0",
        "MDEnabled: true",
        "HasMdCompanion: false",
        "Native Mac Client: false",
        "XPMigration: false",
        "i2aAttempted: false",
        "wfl_state: 4",
        "LID Completed Migrations: error",
        "Status Infra State: wr:N rd:N wr_lg:N mgr_mt:N ntf:N snd:N rcv:N stz_snd:N stz_rcv:N",
        "saga_copy: true",
        f"anid: {anid}",
    ]
    return "\n".join(lines)


def _attach_file(msg: MIMEMultipart, path: str, fallback_name: str):
    p = Path(path)
    if not p.is_file():
        return False
    data = p.read_bytes()
    name = p.name or fallback_name
    suffix = p.suffix.lower()
    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        subtype = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg",
                   ".gif": "gif", ".webp": "webp"}.get(suffix, "octet-stream")
        part = MIMEImage(data, _subtype=subtype)
    else:
        part = MIMEApplication(data, Name=name)
    part.add_header("Content-Disposition", "attachment", filename=name)
    msg.attach(part)
    return True


def send_form_v4_email(to_addr, body_text: str, *, subject: str | None = None,
                       cfg: dict | None = None,
                       attach_logs: bool = False, attach_photo: bool = True) -> dict:
    """Kirim 1 email via SMTP (iCloud/Outlook) + lampiran foto.

    Retry sekali tanpa attachment kalau kena HM108/550.
    """
    cfg = cfg or get_form_v4_config()
    if isinstance(to_addr, (list, tuple)):
        rcpt_list = [str(x).strip() for x in to_addr if str(x).strip()]
    else:
        rcpt_list = [x.strip() for x in str(to_addr).split(",") if x.strip()]
    to_header = ", ".join(rcpt_list)
    out = {"ok": False, "error": None, "to": to_header, "from": cfg.get("user")}
    user = cfg.get("user") or ""
    password = cfg.get("password") or ""
    if not user or not password:
        out["error"] = "FORM_V4_USER/FORM_V4_PASS belum di-set"
        return out
    if not rcpt_list:
        out["error"] = "no recipients"
        return out

    for attempt in range(2):
        use_photo = attach_photo and (attempt == 0)
        use_logs = attach_logs and (attempt == 0)

        msg = MIMEMultipart()
        msg["From"] = user
        msg["To"] = to_header
        msg["Subject"] = subject or FORM_V4_SUBJECT
        msg["Message-ID"] = f"<{uuid.uuid4()}@{cfg['domain']}>"
        msg.attach(MIMEText(body_text, "plain", "utf-8"))

        attached = []
        logs_path = str(_ASSETS / "logs.tar.gz")
        photo_path = str(_ASSETS / "form_v3_proof.png")
        if use_logs and _attach_file(msg, logs_path, "logs.tar.gz"):
            attached.append("logs")
        if use_photo and _attach_file(msg, photo_path, "proof.png"):
            attached.append("photo")
        out["attached"] = attached

        try:
            context = ssl.create_default_context()
            with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=45) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                smtp.login(user, password)
                smtp.sendmail(user, rcpt_list, msg.as_string())
            out["ok"] = True
            return out
        except smtplib.SMTPDataError as e:
            out["error"] = f"SMTP data error: {e}"
            if attempt == 0 and ("550" in str(e) or "HM108" in str(e) or "rejected" in str(e).lower()):
                time.sleep(random.uniform(3, 6))
                continue  # retry without attachment
            return out
        except smtplib.SMTPAuthenticationError as e:
            out["error"] = f"SMTP auth failed: {e}"
            return out
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
            if attempt == 0:
                time.sleep(random.uniform(3, 6))
                continue
            return out
    return out


def imap_test_login(cfg: dict | None = None) -> dict:
    """Test IMAP login. Return {'ok': bool, 'error': str|None, 'folders': list}."""
    cfg = cfg or get_form_v4_config()
    out = {"ok": False, "error": None, "folders": []}
    if not cfg.get("user") or not cfg.get("password"):
        out["error"] = "credentials missing"
        return out
    try:
        M = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg["imap_port"]), timeout=30)
        M.login(cfg["user"], cfg["password"])
        typ, data = M.list()
        if typ == "OK" and data:
            out["folders"] = [d.decode() for d in data if d]
        M.logout()
        out["ok"] = True
    except imaplib.IMAP4.error as e:
        out["error"] = f"IMAP auth failed (basic auth disabled? pakai OAuth2): {e}"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def imap_max_uid(cfg: dict | None = None, mailbox: str = "INBOX") -> int:
    """UID tertinggi di mailbox (baseline sebelum kirim)."""
    cfg = cfg or get_form_v4_config()
    if not cfg.get("user") or not cfg.get("password"):
        return 0
    try:
        M = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg["imap_port"]), timeout=30)
        M.login(cfg["user"], cfg["password"])
        M.select(mailbox)
        typ, data = M.uid("search", None, "ALL")
        max_uid = 0
        if typ == "OK" and data and data[0]:
            for u in data[0].split():
                try:
                    max_uid = max(max_uid, int(u))
                except Exception:
                    pass
        M.logout()
        return max_uid
    except Exception as e:
        print(f"[FAQ4] imap baseline fail: {e}", flush=True)
        return 0


def imap_fetch_replies(since_uid: int = 0, cfg: dict | None = None,
                       limit: int = 10) -> list[dict]:
    """Fetch email baru dari support WhatsApp."""
    cfg = cfg or get_form_v4_config()
    out = []
    if not cfg.get("user") or not cfg.get("password"):
        return out
    try:
        M = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg["imap_port"]), timeout=40)
        M.login(cfg["user"], cfg["password"])
        M.select("INBOX")
        typ, data = M.uid("search", None, "ALL")
        uids = []
        if typ == "OK" and data and data[0]:
            for u in data[0].split():
                try:
                    ui = int(u)
                except Exception:
                    continue
                if ui > int(since_uid or 0):
                    uids.append(ui)
        uids = sorted(uids)[-limit:]
        for ui in uids:
            typ, msg_data = M.uid("fetch", str(ui), "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            em = email.message_from_bytes(raw)
            frm = em.get("From", "") or ""
            subj = em.get("Subject", "") or ""
            low = (frm + " " + subj).lower()
            if not any(x in low for x in (
                "whatsapp", "support@support", "smb-iphone", "@support.whatsapp",
            )):
                continue
            body = ""
            if em.is_multipart():
                for part in em.walk():
                    if part.get_content_type() == "text/plain":
                        try:
                            body = part.get_payload(decode=True).decode(
                                part.get_content_charset() or "utf-8", errors="replace"
                            )
                        except Exception:
                            body = ""
                        break
            else:
                try:
                    body = em.get_payload(decode=True).decode(
                        em.get_content_charset() or "utf-8", errors="replace"
                    )
                except Exception:
                    body = ""
            out.append({
                "uid": ui, "from": frm, "subject": subj,
                "body": (body or "")[:2000],
            })
        M.logout()
    except Exception as e:
        print(f"[FORM_V4] imap poll fail: {e}", flush=True)
    return out


def submit_form_v4(nomor_clean: str, appeal_text: str, *, send_count: int = 1,
                   recipients: list | None = None, subject: str | None = None,
                   cfg: dict | None = None) -> dict:
    """Kirim Form V4 (iCloud SMTP) + Support Info + lampiran logs/foto.

    1 email per recipient — iCloud reject kalau >5 recipients per email.
    Body = appeal text + random Support Info block (iPhone SMB dump).
    """
    cfg = cfg or get_form_v4_config()
    rcpts = list(recipients or FORM_V4_RECIPIENTS)
    random.shuffle(rcpts)   # random order to avoid always hitting same recipient first
    n = max(0, int(send_count))
    try:
        from faq_server import build_random_support_info
        body = (appeal_text or "").rstrip() + build_random_support_info(nomor_clean)
    except ImportError:
        body = (appeal_text or "").rstrip() + build_v4_support_info(nomor_clean)
    results = []
    ok_n = 0
    for i, to_addr in enumerate(rcpts[:n]):
        r = send_form_v4_email(to_addr, body, subject=subject or FORM_V4_SUBJECT, cfg=cfg)
        results.append(r)
        if r.get("ok"):
            ok_n += 1
        else:
            print(f"[FORM_V4] send fail -> {to_addr}: {r.get('error')}", flush=True)
        if i + 1 < min(n, len(rcpts)):
            time.sleep(random.uniform(4, 8))     # more breathing room for iCloud
    return {
        "ok": ok_n > 0,
        "ok_count": ok_n,
        "total": n,
        "results": results,
        "from": cfg.get("user"),
        "error": None if ok_n else (results[-1].get("error") if results else "no send"),
    }


if __name__ == "__main__":
    import sys
    cfg = get_form_v4_config()
    print(f"User: {cfg['user']}")
    print(f"SMTP: {cfg['smtp_host']}:{cfg['smtp_port']}")
    print(f"IMAP: {cfg['imap_host']}:{cfg['imap_port']}")
    print()

    if not cfg["user"] or not cfg["password"]:
        print("ERROR: Set FORM_V4_USER dan FORM_V4_PASS dulu di form_v4.env atau env")
        print("Contoh form_v4.env:")
        print("  FORM_V4_USER=xxx@outlook.com")
        print("  FORM_V4_PASS=your_app_password")
        sys.exit(1)

    print("--- TEST IMAP LOGIN ---")
    imap_res = imap_test_login(cfg)
    print(f"  IMAP login: {'OK' if imap_res['ok'] else 'FAIL'}")
    if imap_res.get("error"):
        print(f"  Error: {imap_res['error']}")
    if imap_res.get("folders"):
        print(f"  Folders: {imap_res['folders'][:5]}")
    print()

    print("--- TEST SMTP SEND ---")
    test_body = "Test email from Form V4 Outlook module. Ignore."
    smtp_res = send_form_v4_email(cfg["user"], test_body, cfg=cfg)
    print(f"  SMTP send: {'OK' if smtp_res['ok'] else 'FAIL'}")
    if smtp_res.get("error"):
        print(f"  Error: {smtp_res['error']}")
    print()

    if imap_res["ok"] and smtp_res["ok"]:
        print("✓✓ Basic auth WORKS! Bisa dipakai.")
    else:
        print("✗✗ Basic auth FAILED. Perlu OAuth2.")
        print()
        print("Cara enable app password (kalau masih bisa):")
        print("  1. Login outlook.com → Settings → Security → App passwords")
        print("  2. Atau coba disable 2FA lalu enable 'less secure apps'")
        print("  3. Kalau tetap gagal → harus pakai OAuth2 (Azure AD app)")
