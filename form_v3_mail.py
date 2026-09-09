"""
Form V3 — banding WhatsApp via Gmail/iCloud SMTP + lampiran (logs + foto).

Kirim: SMTP (bukan IMAP). IMAP dipakai untuk cek balasan support.
Config: form_v3.env di folder yang sama, atau env OS:
  IMAP_USER / IMAP_PASS  (app password)
  IMAP_HOST / SMTP_HOST  (opsional; auto dari domain)
  FORM_V3_LOGS / FORM_V3_PHOTO
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
_ENV_PATH = _DIR / "form_v3.env"
_ASSETS = _DIR / "assets"

FORM_V3_SUBJECT = "Question about WhatsApp Business for iPhone"
FORM_V3_RECIPIENTS = [
    "smb-iphone@support.whatsapp.com",
    "support@support.whatsapp.com",
    "smb@support.whatsapp.com",
    "android_web@support.whatsapp.com",
    "iphone_web@support.whatsapp.com",
    "webclient_web@support.whatsapp.com",
    "smb_web@support.whatsapp.com",
    "businesscomplaints@support.whatsapp.com",
    "accessibility@support.whatsapp.com",
    "ip@whatsapp.com",
    "log_whatsapp@records.whatsapp.com",
]

# Preset provider (SMTP kirim + IMAP baca)
_PROVIDERS = {
    "gmail.com": {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
    },
    "googlemail.com": {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
    },
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
}

_IPHONE_MODELS = [
    "iPhone 13 Pro Max",
    "iPhone 14 Pro",
    "iPhone 14 Pro Max",
    "iPhone 15 Pro",
    "iPhone 15 Pro Max",
    "iPhone 16 Pro",
]
_IOS_VERSIONS = ["18.5", "18.6", "18.7", "27.0"]
_WA_SMB_VERSIONS = ["2.26.25.77", "2.26.26.80", "2.26.27.81"]


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


def get_form_v3_config() -> dict:
    """Merge form_v3.env + OS env. Password tidak di-hardcode di source."""
    file_cfg = _load_env_file()
    user = (
        os.environ.get("IMAP_USER")
        or os.environ.get("FORM_V3_USER")
        or file_cfg.get("IMAP_USER")
        or file_cfg.get("FORM_V3_USER")
        or ""
    ).strip()
    password = (
        os.environ.get("IMAP_PASS")
        or os.environ.get("FORM_V3_PASS")
        or file_cfg.get("IMAP_PASS")
        or file_cfg.get("FORM_V3_PASS")
        or ""
    ).strip()
    # App password Gmail sering ada spasi — hapus spasi aman
    password_compact = password.replace(" ", "")

    domain = user.split("@")[-1].lower() if "@" in user else "gmail.com"
    prov = dict(_PROVIDERS.get(domain, _PROVIDERS["gmail.com"]))

    smtp_host = (
        os.environ.get("SMTP_HOST")
        or file_cfg.get("SMTP_HOST")
        or prov["smtp_host"]
    )
    imap_host = (
        os.environ.get("IMAP_HOST")
        or file_cfg.get("IMAP_HOST")
        or prov["imap_host"]
    )
    smtp_port = int(os.environ.get("SMTP_PORT") or file_cfg.get("SMTP_PORT") or prov["smtp_port"])
    imap_port = int(os.environ.get("IMAP_PORT") or file_cfg.get("IMAP_PORT") or prov["imap_port"])

    logs = (
        os.environ.get("FORM_V3_LOGS")
        or file_cfg.get("FORM_V3_LOGS")
        or str(_ASSETS / "logs.tar.gz")
    )
    photo = (
        os.environ.get("FORM_V3_PHOTO")
        or file_cfg.get("FORM_V3_PHOTO")
        or str(_ASSETS / "form_v3_proof.png")
    )
    return {
        "user": user,
        "password": password_compact or password,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "imap_host": imap_host,
        "imap_port": imap_port,
        "logs": logs,
        "photo": photo,
        "domain": domain,
        "provider": "icloud" if domain in ("icloud.com", "me.com", "mac.com") else "gmail",
    }


def dialing_code_and_pn(nomor_clean: str) -> tuple[str, str]:
    """CCode (calling code) + pn (full digits) untuk Support Info iOS."""
    n = "".join(c for c in str(nomor_clean or "") if c.isdigit())
    try:
        import phonenumbers
        pn = phonenumbers.parse("+" + n, None)
        return str(pn.country_code), str(pn.national_number)
    except Exception:
        # fallback kasar ID 62
        if n.startswith("62") and len(n) >= 10:
            return "62", n[2:]
        if n.startswith("94"):
            return "94", n[2:] if len(n) > 2 else n
        return n[:2] or "62", n


def build_ios_smb_support_info(nomor_clean: str) -> str:
    """Blok Support Info mirip dump WhatsApp Business iPhone."""
    cc, national = dialing_code_and_pn(nomor_clean)
    model = random.choice(_IPHONE_MODELS)
    ios = random.choice(_IOS_VERSIONS)
    ver = random.choice(_WA_SMB_VERSIONS)
    model_ua = model.replace(" ", "_")
    anid = str(uuid.uuid4()).upper()
    build_id = str(random.randint(1008000000, 1009999999))
    hash_id = str(random.randint(100000000000, 999999999999))
    free_bytes = random.randint(80_000_000_000, 120_000_000_000)
    free_mb = free_bytes // (1024 * 1024)
    now = time.strftime("%Y-%m-%d %H:%M:%S.000%z")
    if len(now) >= 5 and (now[-5] in "+-" or True):
        # pastikan ada offset
        now = time.strftime("%Y-%m-%d %H:%M:%S") + time.strftime("%z")
        if len(now) >= 2 and now[-5] not in "+-":
            now = time.strftime("%Y-%m-%d %H:%M:%S") + "+0700"
        elif len(now) >= 5 and now[-2] != ":":
            # +0700 ok
            pass

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
        "LC: VN",
        "LG: en",
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
        f"Smb count: {random.randint(100, 900)}",
        f"Ent count: {random.randint(10, 80)}",
        "Interface: WiFi/None",
        f"DA: {random.randint(10, 30)}",
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


def send_form_v3_email(
    to_addr,
    body_text: str,
    *,
    subject: str | None = None,
    cfg: dict | None = None,
    attach_logs: bool = True,
    attach_photo: bool = True,
) -> dict:
    """Kirim 1 email banding Form V3 via SMTP. to_addr: str atau list recipient."""
    cfg = cfg or get_form_v3_config()
    if isinstance(to_addr, (list, tuple)):
        rcpt_list = [str(x).strip() for x in to_addr if str(x).strip()]
    else:
        rcpt_list = [x.strip() for x in str(to_addr).split(",") if x.strip()]
    to_header = ", ".join(rcpt_list)
    out = {"ok": False, "error": None, "to": to_header, "from": cfg.get("user")}
    user = cfg.get("user") or ""
    password = cfg.get("password") or ""
    if not user or not password:
        out["error"] = "IMAP_USER/IMAP_PASS belum di-set (form_v3.env)"
        return out
    if not rcpt_list:
        out["error"] = "no recipients"
        return out

    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = to_header
    msg["Subject"] = subject or FORM_V3_SUBJECT
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    attached = []
    if attach_logs and _attach_file(msg, cfg.get("logs") or "", "logs.tar.gz"):
        attached.append("logs")
    if attach_photo and _attach_file(msg, cfg.get("photo") or "", "proof.png"):
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
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def submit_form_v3(
    nomor_clean: str,
    appeal_text: str,
    *,
    send_count: int = 1,
    recipients: list | None = None,
    subject: str | None = None,
    cfg: dict | None = None,
) -> dict:
    """Kirim Form V3. Default 1 email ke semua recipient (To: multi)."""
    cfg = cfg or get_form_v3_config()
    rcpts = list(recipients or FORM_V3_RECIPIENTS)
    n = max(0, int(send_count))
    try:
        from faq_server import build_random_support_info
        body = (appeal_text or "").rstrip() + build_random_support_info(nomor_clean)
    except ImportError:
        body = (appeal_text or "").rstrip() + build_ios_smb_support_info(nomor_clean)
    results = []
    ok_n = 0
    for i in range(n):
        # 1 kirim = semua recipient di satu email (mirip HAR site.pro)
        to_addr = rcpts if n == 1 else rcpts[i % len(rcpts)]
        r = send_form_v3_email(
            to_addr, body, subject=subject or FORM_V3_SUBJECT, cfg=cfg,
        )
        results.append(r)
        if r.get("ok"):
            ok_n += 1
        else:
            print(f"[FAQ3] send fail -> {to_addr}: {r.get('error')}", flush=True)
        if i + 1 < n:
            time.sleep(random.uniform(1.5, 3.5))
    return {
        "ok": ok_n > 0,
        "ok_count": ok_n,
        "total": n,
        "results": results,
        "from": cfg.get("user"),
        "error": None if ok_n else (results[-1].get("error") if results else "no send"),
    }


def imap_max_uid(cfg: dict | None = None, mailbox: str = "INBOX") -> int:
    """UID tertinggi di mailbox (baseline sebelum kirim)."""
    cfg = cfg or get_form_v3_config()
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
        print(f"[FAQ3] imap baseline fail: {e}", flush=True)
        return 0


def imap_fetch_new_support_replies(
    since_uid: int,
    cfg: dict | None = None,
    mailbox: str = "INBOX",
    limit: int = 20,
) -> list[dict]:
    """Ambil email baru (UID > since_uid) dari support WhatsApp."""
    cfg = cfg or get_form_v3_config()
    out = []
    if not cfg.get("user") or not cfg.get("password"):
        return out
    try:
        M = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg["imap_port"]), timeout=40)
        M.login(cfg["user"], cfg["password"])
        M.select(mailbox)
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
                    ctype = part.get_content_type()
                    if ctype == "text/plain":
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
                "body": (body or "")[:4000],
            })
        M.logout()
    except Exception as e:
        print(f"[FAQ3] imap poll fail: {e}", flush=True)
    return out
