"""tempmail_module.py — Temp mail mob2.temp-mail.org untuk FAQ2.

Alur (dari faq2.har — app TempMail iOS):
  POST https://mob2.temp-mail.org/mailbox/        → mailbox BARU (anonim, tanpa token)
                                                      return {"token", "mailbox"}
  GET  https://mob2.temp-mail.org/mailbox/        → mailbox aktif (Bearer token)
  POST /mailbox/ dengan token lama               → ROTASI ke mailbox baru
  GET  /messages/                                 → list pesan:
        {"mailbox", "messages": [{"_id","receivedAt","from","subject","bodyPreview"}]}
  GET  /messages/{_id}/                          → 1 pesan lengkap (bodyHtml)

UA app asli: TempMail/1036 CFNetwork/3888.100.1 Darwin/27.0.0
Semua request blocking (requests) — panggil via run_in_executor / thread pool.
"""
from __future__ import annotations

import re
import time
import html as _html

import requests
import urllib3

# Suppress InsecureRequestWarning — mob2.temp-mail.org cert sering gagal di panel
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TEMPMAIL_BASE = "https://mob2.temp-mail.org"
TEMPMAIL_UA = "TempMail/1036 CFNetwork/3888.100.1 Darwin/27.0.0"

_TIMEOUT = 20
_VERIFY_SSL = False   # mob2 cert sering invalid di server panel
_POLL_GAP = 4          # detik antar poll
_POLL_TRIES = 15       # total ~60 detik menunggu balasan


def tempmail_new_mailbox(timeout: int = _TIMEOUT) -> dict:
    """Buat mailbox temp baru (anonim). Return {'ok','mailbox','token','error'}."""
    out = {"ok": False, "mailbox": None, "token": None, "error": None}
    try:
        r = requests.post(
            f"{TEMPMAIL_BASE}/mailbox/",
            headers={"User-Agent": TEMPMAIL_UA, "Accept": "*/*"},
            timeout=timeout, verify=_VERIFY_SSL,
        )
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}"
            return out
        d = r.json()
        if d.get("mailbox") and d.get("token"):
            out["ok"] = True
            out["mailbox"] = d["mailbox"]
            out["token"] = d["token"]
        else:
            out["error"] = f"bad response: {r.text[:120]}"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def tempmail_list_messages(token: str, timeout: int = _TIMEOUT) -> list[dict]:
    """List pesan di mailbox. Return list dict {_id, from, subject, bodyPreview, receivedAt}."""
    try:
        r = requests.get(
            f"{TEMPMAIL_BASE}/messages/",
            headers={"User-Agent": TEMPMAIL_UA,
                     "Authorization": f"Bearer {token}", "Accept": "*/*"},
            timeout=timeout, verify=_VERIFY_SSL,
        )
        if r.status_code != 200:
            return []
        return r.json().get("messages") or []
    except Exception:
        return []


def tempmail_get_body(token: str, message_id: str, timeout: int = _TIMEOUT) -> dict:
    """Ambil 1 pesan lengkap. Return {'subject','from','text','html','error'}."""
    out = {"subject": None, "from": None, "text": None, "html": None, "error": None}
    try:
        r = requests.get(
            f"{TEMPMAIL_BASE}/messages/{message_id}/",
            headers={"User-Agent": TEMPMAIL_UA,
                     "Authorization": f"Bearer {token}", "Accept": "*/*"},
            timeout=timeout, verify=_VERIFY_SSL,
        )
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}"
            return out
        j = r.json()
        out["subject"] = j.get("subject")
        out["from"] = j.get("from")
        out["html"] = j.get("bodyHtml")
        out["text"] = _html_to_text(j.get("bodyHtml") or j.get("bodyPreview") or "")
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out


def tempmail_wait_whatsapp_reply(token: str, max_tries: int = _POLL_TRIES,
                                 gap: float = _POLL_GAP) -> dict | None:
    """Poll mailbox sampai ada email dari WhatsApp Support.

    Return pesan WA pertama {'_id','from','subject','bodyPreview',...} atau None
    kalau habis waktu. Blocking — jalankan di thread.
    """
    for _ in range(max(1, int(max_tries))):
        time.sleep(gap)
        msgs = tempmail_list_messages(token)
        for m in msgs:
            low = (str(m.get("from") or "") + " " + str(m.get("subject") or "")).lower()
            if "whatsapp" in low or "support" in low:
                return m
    return None


def _html_to_text(html_str: str) -> str:
    """HTML email → plain text bersih (buang style/script/tag/entities)."""
    s = re.sub(r"<style[^>]*>.*?</style>", " ", html_str, flags=re.S | re.I)
    s = re.sub(r"<script[^>]*>.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


if __name__ == "__main__":
    # Test mandiri: buat mailbox, poll 60 detik
    res = tempmail_new_mailbox()
    print(f"mailbox: {res['mailbox']} ok={res['ok']} err={res['error']}")
    if res["ok"]:
        print("Menunggu email WhatsApp (60 detik)...")
        m = tempmail_wait_whatsapp_reply(res["token"])
        if m:
            print(f"from: {m['from']}")
            print(f"subject: {m['subject']}")
            full = tempmail_get_body(res["token"], m["_id"])
            print(f"text: {full['text'][:400]}")
        else:
            print("Tidak ada email WhatsApp masuk.")
