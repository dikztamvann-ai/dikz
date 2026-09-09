"""
Cloudflare handling untuk iVASMS (www.ivasms.com).

Dua hal berbeda yang sering dikira sama:

1. Turnstile widget di FORM /login  → butuh TOKEN (sitekey 0x4AAAAAACqVmW6ncA-jc10z).
   Token didapat dari solver publik kyzznekoo.zone.id. Dikirim sebagai field
   `cf-turnstile-response` saat POST /login.

2. Managed challenge WAF ("Just a moment...") → butuh COOKIE cf_clearance.
   cf_clearance TERIKAT ke IP + User-Agent. Jadi:
     - clearance dari solver TIDAK BISA dipakai (IP beda → 403)
     - clearance dari HP user TIDAK BISA dipakai di server (IP beda → 403)
   Satu-satunya cara: lewati challenge dari IP mesin ini sendiri, pakai browser
   asli (Playwright headed — headless selalu gagal). Hasilnya di-cache proses
   dan dipakai ulang untuk semua request requests.Session.
"""

from __future__ import annotations

import os
import time
import json
import logging
import threading

import requests

log = logging.getLogger(__name__)

BASE_URL = os.getenv("IVAS_BASE_URL", "https://www.ivasms.com")
LOGIN_URL = f"{BASE_URL}/login"

# Sitekey widget Turnstile di form login ivasms (juga dipakai managed challenge).
TURNSTILE_SITEKEY = os.getenv("IVAS_TURNSTILE_SITEKEY", "0x4AAAAAACqVmW6ncA-jc10z")

# Solver publik. turnstileMin -> {"data": {"token": "..."}}
TS_SOLVER_URL = os.getenv("TS_SOLVER_URL", "https://kyzznekoo.zone.id/api/cloudflare/turnstileMin")
TS_TIMEOUT = int(os.getenv("TS_TIMEOUT", "120"))
TS_RETRY = int(os.getenv("TS_RETRY", "3"))

# cf_clearance biasanya valid ~30 menit; refresh lebih cepat supaya aman.
CLEARANCE_TTL = int(os.getenv("IVAS_CLEARANCE_TTL", "1500"))
CLEARANCE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cf_clearance.json")

_clearance_lock = threading.Lock()
_clearance_cache: dict | None = None   # {"cookies": {...}, "ua": str, "ts": float}


# ---------------------------------------------------------------------------
# 1. Turnstile token (untuk form login)
# ---------------------------------------------------------------------------

def solve_turnstile(page_url: str = LOGIN_URL, sitekey: str = "",
                    retries: int = 0, on_status=None) -> str | None:
    """Minta token Turnstile ke solver. Return token (~730 chars) atau None."""
    sitekey = sitekey or TURNSTILE_SITEKEY
    retries = retries or TS_RETRY

    for attempt in range(1, retries + 1):
        t0 = time.time()
        try:
            r = requests.get(
                TS_SOLVER_URL,
                params={"url": page_url, "sitekey": sitekey},
                headers={"Accept": "application/json"},
                timeout=TS_TIMEOUT,
            )
            body = r.json() if r.content else {}
        except Exception as e:
            _report(on_status, f"[turnstile] error {attempt}/{retries}: {e}")
            time.sleep(2)
            continue

        data = body.get("data") if isinstance(body, dict) else None
        token = data if isinstance(data, str) else (data or {}).get("token")
        if token:
            _report(on_status, f"[turnstile] solved {len(token)} chars in {time.time()-t0:.1f}s")
            return token

        _report(on_status, f"[turnstile] gagal {attempt}/{retries}: HTTP {r.status_code}")
        time.sleep(2)

    return None


def _report(cb, msg):
    log.info(msg)
    print(msg)
    if cb:
        try:
            cb(msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 2. cf_clearance (untuk lewat managed challenge WAF)
# ---------------------------------------------------------------------------

def _load_clearance_file() -> dict | None:
    try:
        with open(CLEARANCE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("cookies", {}).get("cf_clearance") and d.get("ua"):
            return d
    except Exception:
        pass
    return None


def _save_clearance_file(d: dict):
    try:
        with open(CLEARANCE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception as e:
        log.warning("gagal simpan clearance: %s", e)


def _fresh(d: dict | None) -> bool:
    return bool(d) and (time.time() - d.get("ts", 0)) < CLEARANCE_TTL


def _browser_clearance(on_status=None, timeout: int = 45) -> dict | None:
    """Lewati managed challenge pakai Playwright (HEADED — headless selalu gagal)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _report(on_status, "[cf] playwright belum terinstall (pip install playwright)")
        return None

    _report(on_status, "[cf] buka browser untuk lewati challenge...")
    try:
        with sync_playwright() as p:
            # CATATAN: window HARUS benar-benar ter-render di layar.
            # headless=True maupun --window-position offscreen bikin challenge
            # tidak pernah lolos (tetap "Just a moment...").
            browser = p.chromium.launch(
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
            ctx = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 800})
            page = ctx.new_page()

            # UA diambil sebelum navigasi — saat challenge reload, evaluate()
            # bisa gagal dengan "Execution context was destroyed".
            ua = page.evaluate("navigator.userAgent")

            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout * 1000)
            except Exception as e:
                log.warning("[cf] goto: %s", str(e)[:80])

            deadline = time.time() + timeout
            cookies, passed = {}, False
            while time.time() < deadline:
                time.sleep(1.2)
                try:
                    cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                    title = (page.title() or "").lower()
                except Exception:
                    continue  # sedang navigasi, coba lagi
                if cookies.get("cf_clearance") and "just a moment" not in title:
                    passed = True
                    break

            browser.close()
    except Exception as e:
        _report(on_status, f"[cf] browser error: {e}")
        return None

    if not (passed and cookies.get("cf_clearance") and ua):
        _report(on_status, "[cf] gagal dapat cf_clearance")
        return None

    data = {"cookies": cookies, "ua": ua, "ts": time.time()}
    _report(on_status, f"[cf] cf_clearance OK (cookies: {', '.join(cookies)})")
    return data


def get_clearance(force: bool = False, on_status=None) -> dict | None:
    """Return {"cookies": {...}, "ua": str} yang masih valid, atau None.

    Cache di memori + file supaya challenge tidak dilewati berulang kali.
    """
    global _clearance_cache
    with _clearance_lock:
        if not force:
            if _fresh(_clearance_cache):
                return _clearance_cache
            disk = _load_clearance_file()
            if _fresh(disk):
                _clearance_cache = disk
                return disk

        data = _browser_clearance(on_status=on_status)
        if data:
            _clearance_cache = data
            _save_clearance_file(data)
        return data


def clear_clearance_cache():
    """Buang clearance yang tersimpan (dipakai saat kena 403)."""
    global _clearance_cache
    with _clearance_lock:
        _clearance_cache = None
        try:
            os.remove(CLEARANCE_FILE)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 3. Helper untuk requests.Session
# ---------------------------------------------------------------------------

def apply_clearance(session, force: bool = False, on_status=None) -> bool:
    """Suntik cf_clearance + UA yang cocok ke session. Return True kalau berhasil.

    UA WAJIB sama dengan UA browser saat clearance dibuat, kalau tidak → 403.
    """
    data = get_clearance(force=force, on_status=on_status)
    if not data:
        return False

    for name, value in data["cookies"].items():
        # Jangan timpa session/XSRF login user yang sudah ada — cukup cf_clearance.
        if name != "cf_clearance" and session.cookies.get(name):
            continue
        session.cookies.set(name, value, domain="www.ivasms.com")

    session.headers.update({
        "User-Agent": data["ua"],
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
    })
    return True


def get_ivas_ua() -> str | None:
    """UA yang cocok dengan cf_clearance saat ini (None kalau belum ada)."""
    data = _clearance_cache if _fresh(_clearance_cache) else _load_clearance_file()
    return data.get("ua") if _fresh(data) else None


# ---------------------------------------------------------------------------
# 4. Login email + password (turnstile + clearance sekaligus)
# ---------------------------------------------------------------------------

def login_password(email: str, password: str, on_status=None):
    """Login pakai email/password. Return (session, csrf, error) — error None kalau OK."""
    import re

    session = requests.Session()
    if not apply_clearance(session, on_status=on_status):
        return None, None, "cf_clearance gagal didapat (challenge tidak terlewati)"

    try:
        r = session.get(LOGIN_URL, timeout=25, verify=False)
    except Exception as e:
        return None, None, f"GET /login error: {e}"

    if r.status_code != 200:
        clear_clearance_cache()
        return None, None, f"GET /login HTTP {r.status_code}"

    m = re.search(r'name="_token"[^>]*value="([^"]+)"', r.text)
    if not m:
        return None, None, "form _token tidak ditemukan"
    form_token = m.group(1)

    token = solve_turnstile(LOGIN_URL, on_status=on_status)
    if not token:
        return None, None, "Turnstile solve gagal"

    try:
        resp = session.post(
            LOGIN_URL,
            data={
                "_token": form_token,
                "email": email,
                "password": password,
                "remember": "on",
                "_vt": "",
                "submit": "register",
                "cf-turnstile-response": token,
            },
            headers={
                "Referer": LOGIN_URL,
                "Origin": BASE_URL,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
            allow_redirects=True,
            verify=False,
        )
    except Exception as e:
        return None, None, f"POST /login error: {e}"

    if "/portal" not in resp.url:
        return None, None, f"Login ditolak (HTTP {resp.status_code}, url={resp.url})"

    csrf = None
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', resp.text)
    if m:
        csrf = m.group(1)
    _report(on_status, f"[login] OK sebagai {email}")
    return session, csrf, None


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) >= 3:
        s, csrf, err = login_password(sys.argv[1], sys.argv[2])
        print("error:", err, "| csrf:", (csrf or "")[:20])
        if s:
            print("cookies:", list(s.cookies.get_dict()))
    else:
        d = get_clearance()
        print("clearance:", bool(d), list((d or {}).get("cookies", {})))
        print("turnstile:", (solve_turnstile() or "")[:50], "...")



