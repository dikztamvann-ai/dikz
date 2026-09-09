"""
Site.pro + emailqu + Roundcube Webmail - WhatsApp Fix Module
Modul ini berisi semua helper functions untuk /fix dan /create commands.

Register menggunakan DrissionPage untuk bypass reCAPTCHA v2 invisible (gratis).
Setelah register, semua step (website, mailbox, webmail, send) pakai requests.
"""
import requests
import re
import time
import json
import random
import string
import html as html_module
import threading
from urllib.parse import urlencode, quote
from requests.exceptions import RequestException, ReadTimeout

ROUNDCUBE_CONNECT_TIMEOUT = 12
ROUNDCUBE_READ_TIMEOUT = 50
ROUNDCUBE_COMPOSE_RETRIES = 3
WEBMAIL_OPEN_RETRIES = 3
MAX_SENDER_RETRIES_PER_NOMOR = 3
# Baca body pesan (_action=show) kadang lambat di siteprofree.email. Read timeout
# dibuat lebih pendek + retry bertahap, plus fallback ke _action=viewsource yang
# jauh lebih murah di sisi server. Total waktu per pesan dibatasi budget di
# roundcube_read_inbox supaya polling balasan tidak menggantung.
ROUNDCUBE_BODY_READ_TIMEOUT = 12
ROUNDCUBE_BODY_RETRIES = 1

# DNS override site.pro & siteprofree.email — dipasang sekali saja (hindari nested monkey-patch).
_SITEPRO_DNS_IP = "172.104.174.106"
_SITEPROFREE_EMAIL_DNS_IP = "18.199.24.164"
_SITEPRO_DNS_PATCHED = False
_SITEPRO_ORIG_CREATE_CONNECTION = None

# Cache hasil resolusi live (host -> (candidate_ips, expiry_ts)) supaya tidak
# getaddrinfo tiap koneksi (lambat kalau resolver host lemot/gagal).
_SITEPRO_DNS_CACHE = {}
_SITEPRO_DNS_CACHE_TTL = 300  # detik
_SITEPRO_DNS_CACHE_LOCK = threading.Lock()

# Fallback IP statis per-host (dipakai TERAKHIR, hanya kalau live DNS gagal /
# IP live tak bisa connect). JANGAN jadikan satu-satunya IP: kalau IP ini mati
# di jaringan deploy (mis. Pterodactyl) semua koneksi site.pro timeout 50s.
_SITEPRO_FALLBACK_IPS = {
    "site.pro": ["172.104.174.106"],
    "siteprofree.email": ["18.199.24.164"],
}


def _sitepro_resolve_candidates(host):
    """Daftar IP kandidat untuk `host`: live-DNS dulu (kalau resolver jalan),
    fallback statis DITAMBAHKAN paling belakang. Di-cache dgn TTL.

    Urutan ini penting: host sehat pakai IP live (cepat, geo-benar, ikut kalau
    site.pro ganti IP); host yg resolvernya rusak (alasan awal IP di-pin) tetap
    dapat fallback; dan kalau 1 IP mati, IP lain masih dicoba -> tidak lagi
    "Connection timed out (connect timeout=50)" beruntun sampai gagal kirim.
    """
    import socket as _sock
    now = time.time()
    with _SITEPRO_DNS_CACHE_LOCK:
        cached = _SITEPRO_DNS_CACHE.get(host)
        if cached and cached[1] > now:
            return list(cached[0])

    # IPv4 SAJA (AF_INET). Di container Pterodactyl sering ada AAAA record tapi
    # TIDAK ada route IPv6 -> connect ke IPv6 nggantung sampai timeout habis.
    # Itu persis gejala "Connection to site.pro timed out" di panel.
    live = []
    try:
        for info in _sock.getaddrinfo(host, 443, _sock.AF_INET,
                                      _sock.SOCK_STREAM, _sock.IPPROTO_TCP):
            ip = info[4][0]
            if ip not in live:
                live.append(ip)
    except Exception:
        live = []

    candidates = list(live)
    for ip in _SITEPRO_FALLBACK_IPS.get(host, []):
        if ip not in candidates:
            candidates.append(ip)

    with _SITEPRO_DNS_CACHE_LOCK:
        _SITEPRO_DNS_CACHE[host] = (candidates, now + _SITEPRO_DNS_CACHE_TTL)
    return list(candidates)


# Timeout (connect, read) untuk request site.pro.
# PENTING: connect DIPISAH & pendek. Kalau pakai satu angka (timeout=50),
# requests pakai 50s untuk CONNECT juga -> 1 IP mati = tunggu 50s, coba IP
# kedua = 50s lagi -> total 100s, request mati sebelum fallback berguna.
# connect 8s cukup (SYN sehat < 1s); read tetap panjang (site.pro lambat).
SP_CONNECT_TIMEOUT = 8
SP_READ_TIMEOUT = 50
SP_TIMEOUT = (SP_CONNECT_TIMEOUT, SP_READ_TIMEOUT)


def _ensure_sitepro_dns_patch():
    """Patch urllib3 create_connection once so site.pro & siteprofree.email
    resolve via live DNS dulu, lalu fallback IP statis (multi-kandidat)."""
    global _SITEPRO_DNS_PATCHED, _SITEPRO_ORIG_CREATE_CONNECTION
    if _SITEPRO_DNS_PATCHED:
        return
    import urllib3.util.connection

    orig = urllib3.util.connection.create_connection
    # Sudah patch kita (mis. reload module) — jangan wrap lagi.
    if getattr(orig, "_sitepro_dns_patch", False):
        _SITEPRO_ORIG_CREATE_CONNECTION = getattr(orig, "_sitepro_orig", orig)
        _SITEPRO_DNS_PATCHED = True
        return

    _SITEPRO_ORIG_CREATE_CONNECTION = orig

    def _patched_create_connection(address, *args, **kwargs):
        host, port = address
        if host not in ("site.pro", "siteprofree.email"):
            return _SITEPRO_ORIG_CREATE_CONNECTION(address, *args, **kwargs)
        candidates = _sitepro_resolve_candidates(host)
        if not candidates:
            return _SITEPRO_ORIG_CREATE_CONNECTION(address, *args, **kwargs)
        last_err = None
        for ip in candidates:
            try:
                return _SITEPRO_ORIG_CREATE_CONNECTION((ip, port), *args, **kwargs)
            except Exception as e:
                last_err = e
                continue
        # Semua kandidat gagal -> invalidasi cache biar next call resolve ulang.
        with _SITEPRO_DNS_CACHE_LOCK:
            _SITEPRO_DNS_CACHE.pop(host, None)
        raise last_err

    _patched_create_connection._sitepro_dns_patch = True
    _patched_create_connection._sitepro_orig = _SITEPRO_ORIG_CREATE_CONNECTION
    urllib3.util.connection.create_connection = _patched_create_connection
    _SITEPRO_DNS_PATCHED = True


# ════════════════════════════════════════════════════════════════
#  EMAILQU.COM — Temporary Email Helpers
# ════════════════════════════════════════════════════════════════

def emailqu_get_temp_email(max_retries=3):
    """Buat email temporary dari emailqu.com. Returns (email, domain) or (None, None)."""
    for attempt in range(max_retries):
        try:
            s = requests.Session()
            # Get random username
            r = s.get(f"https://emailqu.com/api/random-username", timeout=10)
            r.raise_for_status()
            username = r.json().get("username")
            if not username:
                continue

            # Get random domain
            r = s.get(f"https://emailqu.com/api/domains/random", timeout=10)
            r.raise_for_status()
            domains = r.json().get("domains", [])
            # Pick a non-subdomain, non-hidden domain
            valid_domains = [d for d in domains if not d.get("is_subdomain") and not d.get("is_hidden")]
            if not valid_domains:
                valid_domains = domains
            if not valid_domains:
                continue
            domain = random.choice(valid_domains)["domain"]

            # Verify domain
            r = s.get(f"https://emailqu.com/api/domain/verify/{domain}", timeout=10)
            if r.status_code != 200:
                continue

            email = f"{username}@{domain}"
            return email, domain
        except Exception as e:
            print(f"[SITEPRO] emailqu_get_temp_email attempt {attempt+1} error: {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
    return None, None


def emailqu_poll_inbox(email, timeout=90, interval=3):
    """Poll emailqu inbox sampai dapat email dari Site.pro. Returns email body_text or None."""
    s = requests.Session()
    encoded = quote(email, safe="")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = s.get(f"https://emailqu.com/api/public/emails/{encoded}?limit=1", timeout=10)
            if r.status_code == 200:
                data = r.json()
                emails = data.get("emails", [])
                if emails:
                    body = emails[0].get("body_text", "")
                    return body
        except Exception as e:
            print(f"[SITEPRO] emailqu_poll_inbox error: {e}")
        time.sleep(interval)
    return None


def emailqu_extract_otp(body_text):
    """Extract 6-digit OTP code from Site.pro verification email."""
    if not body_text:
        return None
    # Pattern: "Kode verifikasi: \n779016" or "Verification code: \n123456"
    m = re.search(r'(?:Kode verifikasi|Verification code)\s*:\s*\n?(\d{6})', body_text)
    if m:
        return m.group(1)
    # Fallback: cari 6 digit standalone
    m = re.search(r'\b(\d{6})\b', body_text)
    if m:
        return m.group(1)
    return None


# ════════════════════════════════════════════════════════════════
#  CAPTCHA SOLVER — reCAPTCHA v2 Invisible (2captcha-compatible API)
#  Supports: 2captcha.com, rucaptcha.com, anti-captcha.com, capsolver.com
# ════════════════════════════════════════════════════════════════

def solve_recaptcha_2captcha(api_key, sitekey, url, api_base="https://2captcha.com", max_wait=180):
    """
    Solve reCAPTCHA v2 invisible via 2captcha-compatible API.
    api_base: "https://2captcha.com" or "https://rucaptcha.com" etc.
    Returns token string or None.
    """
    if not api_key:
        print("[SITEPRO] CAPTCHA_API_KEY belum diset!")
        return None
    try:
        # Step 1: Submit task
        r = requests.post(f"{api_base}/in.php", data={
            "key": api_key,
            "method": "userrecaptcha",
            "googlekey": sitekey,
            "pageurl": url,
            "invisible": "1",
            "json": "1",
        }, timeout=30)
        data = r.json()
        if data.get("status") != 1:
            print(f"[SITEPRO] Captcha submit error: {data}")
            return None
        task_id = data["request"]
        print(f"[SITEPRO] Captcha task created: {task_id}")

        # Step 2: Wait before first poll
        time.sleep(15)

        # Step 3: Poll for result
        deadline = time.time() + max_wait
        while time.time() < deadline:
            r2 = requests.get(f"{api_base}/res.php", params={
                "key": api_key,
                "action": "get",
                "id": task_id,
                "json": "1",
            }, timeout=15)
            d2 = r2.json()
            if d2.get("request") == "CAPCHA_NOT_READY":
                time.sleep(5)
                continue
            if d2.get("status") == 1:
                print(f"[SITEPRO] Captcha solved!")
                return d2["request"]
            print(f"[SITEPRO] Captcha poll error: {d2}")
            return None
        print(f"[SITEPRO] Captcha solve timeout ({max_wait}s)")
        return None
    except Exception as e:
        print(f"[SITEPRO] Captcha solver error: {e}")
        return None


def solve_recaptcha_capsolver(api_key, sitekey, url, max_wait=180):
    """
    Solve reCAPTCHA v2 invisible via CapSolver API.
    Returns token string or None.
    """
    if not api_key:
        print("[SITEPRO] CAPSOLVER_API_KEY belum diset!")
        return None
    try:
        # Create task
        r = requests.post("https://api.capsolver.com/createTask", json={
            "clientKey": api_key,
            "task": {
                "type": "ReCaptchaV2TaskProxyLess",
                "websiteURL": url,
                "websiteKey": sitekey,
                "isInvisible": True,
            }
        }, timeout=30)
        data = r.json()
        if data.get("errorId", 0) != 0:
            print(f"[SITEPRO] CapSolver create error: {data}")
            return None
        task_id = data.get("taskId")
        if not task_id:
            print(f"[SITEPRO] CapSolver no taskId: {data}")
            return None
        print(f"[SITEPRO] CapSolver task: {task_id}")

        time.sleep(10)
        deadline = time.time() + max_wait
        while time.time() < deadline:
            r2 = requests.post("https://api.capsolver.com/getTaskResult", json={
                "clientKey": api_key,
                "taskId": task_id,
            }, timeout=15)
            d2 = r2.json()
            status = d2.get("status")
            if status == "ready":
                token = d2.get("solution", {}).get("gRecaptchaResponse")
                if token:
                    print(f"[SITEPRO] CapSolver solved!")
                    return token
            if d2.get("errorId", 0) != 0:
                print(f"[SITEPRO] CapSolver error: {d2}")
                return None
            time.sleep(5)
        print(f"[SITEPRO] CapSolver timeout ({max_wait}s)")
        return None
    except Exception as e:
        print(f"[SITEPRO] CapSolver error: {e}")
        return None


def solve_recaptcha(api_key, sitekey, url, solver_type="2captcha"):
    """
    Universal captcha solver dispatcher.
    solver_type: "2captcha", "capsolver", "anticaptcha"
    """
    if solver_type in ("2captcha", "rucaptcha"):
        base = "https://2captcha.com" if solver_type == "2captcha" else "https://rucaptcha.com"
        return solve_recaptcha_2captcha(api_key, sitekey, url, api_base=base)
    elif solver_type == "capsolver":
        return solve_recaptcha_capsolver(api_key, sitekey, url)
    elif solver_type == "anticaptcha":
        # Anti-captcha uses same protocol as 2captcha but different base
        return solve_recaptcha_2captcha(api_key, sitekey, url, api_base="https://api.anti-captcha.com")
    else:
        print(f"[SITEPRO] Unknown solver type: {solver_type}")
        return None


# ════════════════════════════════════════════════════════════════
#  SITE.PRO — Registration & Account Helpers
# ════════════════════════════════════════════════════════════════

def _sitepro_session():
    """Create a requests.Session with random Android UA for Site.pro.
    Includes DNS override to bypass VPS DNS resolution issues."""
    from urllib3.util.retry import Retry
    from requests.adapters import HTTPAdapter

    _ensure_sitepro_dns_patch()
    
    _android_uas = [
        'Mozilla/5.0 (Linux; Android 14; SM-A546E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.165 Mobile Safari/537.36',
        'Mozilla/5.0 (Linux; Android 13; Redmi Note 12 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36',
        'Mozilla/5.0 (Linux; Android 14; POCO X5 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.71 Mobile Safari/537.36',
        'Mozilla/5.0 (Linux; Android 13; V2237) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.99 Mobile Safari/537.36',
        'Mozilla/5.0 (Linux; Android 14; CPH2565) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.113 Mobile Safari/537.36',
        'Mozilla/5.0 (Linux; Android 15; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.122 Mobile Safari/537.36',
    ]
    
    s = requests.Session()
    retry = Retry(total=1, connect=0, backoff_factor=1, status_forcelist=[502, 503, 504, 520, 521, 522, 524], raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        'User-Agent': random.choice(_android_uas),
        'Accept-Language': 'id-ID,id;q=0.9,en-US;q=0.6,en;q=0.5',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    })
    return s


def sitepro_restore_session(phpsessid, email=None, password=None):
    """
    Restore session dari PHPSESSID yg tersimpan.
    Returns (session, ok) — kalau PHPSESSID expired tapi email+password ada,
    coba re-login. Kalau gagal total, return (None, False).
    """
    if not phpsessid:
        return None, False

    # Retry loop for session check (50s timeout, max 2 attempts)
    s = _sitepro_session()
    s.cookies.set('PHPSESSID', phpsessid, domain='site.pro')
    for attempt in range(2):
        try:
            r = s.get(
                "https://site.pro/id/Website-Saya/load-init/",
                headers={'X-Requested-With': 'XMLHttpRequest'},
                timeout=SP_TIMEOUT,
            )
            try:
                data = r.json()
                if data.get('error') != 'no auth':
                    return s, True
            except Exception:
                pass
            break  # Respon diterima tapi session expired -> stop retry, lanjut re-login
        except Exception as e:
            if attempt < 1:
                time.sleep(1)
            else:
                pass

    # PHPSESSID expired — coba re-login pakai email+password kalau ada (max 2 attempts)
    if email and password:
        for attempt in range(2):
            try:
                s2 = _sitepro_session()
                r = s2.post(
                    "https://site.pro/id/ul/",
                    data={
                        'redir_url': '', 'form_type': '1', 'social_submit': '',
                        'loginBuyDomain': '', 'loginOrderService': '',
                        'plan_id': '', 'plan_cycle': '',
                        'user_login': email, 'user_password': password,
                    },
                    headers={
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'Origin': 'https://site.pro',
                        'Referer': 'https://site.pro/id/',
                    },
                    allow_redirects=True, timeout=SP_TIMEOUT,
                )
                # verify
                r2 = s2.get(
                    "https://site.pro/id/Website-Saya/load-init/",
                    headers={'X-Requested-With': 'XMLHttpRequest'},
                    timeout=SP_TIMEOUT,
                )
                try:
                    data = r2.json()
                    if data.get('error') != 'no auth':
                        return s2, True
                except Exception:
                    pass
                break  # Auth gagal -> stop
            except Exception as e:
                if attempt < 1:
                    time.sleep(1)
                else:
                    pass

    return None, False



def sitepro_get_csrf(session):
    """Ambil CSRF token (lc) dari halaman Site.pro. Returns (csrf, session) or (None, session)."""
    try:
        # Try Website-Saya page first (for logged-in users)
        r = session.get("https://site.pro/id/Website-Saya/", timeout=15)
        if r.status_code == 200:
            # Pattern: input.val("e5ba40389bfbaa4362f24baa6a7bcb75")
            m = re.search(r'input\.val\(["\']([a-f0-9]{32})["\']\)', r.text)
            if m:
                return m.group(1), session
            # Fallback: name="lc" value="..."
            m = re.search(r'name=["\']lc["\'][\s>]*value=["\']([a-f0-9]{32})["\']', r.text)
            if m:
                return m.group(1), session
            # Fallback: csrfToken in JS
            m = re.search(r'["\']csrfToken["\']\s*[=:]\s*["\']([a-f0-9]{32})["\']', r.text)
            if m:
                return m.group(1), session
            # Fallback: token in JSON-like data
            m = re.search(r'"token"\s*:\s*"([a-f0-9]{32})"', r.text)
            if m:
                return m.group(1), session

        # Fallback: main page
        r2 = session.get("https://site.pro/id/", timeout=15)
        if r2.status_code == 200:
            m = re.search(r'input\.val\(["\']([a-f0-9]{32})["\']\)', r2.text)
            if m:
                return m.group(1), session
            m = re.search(r'name=["\']lc["\'][\s>]*value=["\']([a-f0-9]{32})["\']', r2.text)
            if m:
                return m.group(1), session
    except Exception as e:
        print(f"[SITEPRO] get_csrf error: {e}")
    return None, session


def _random_name():
    """Generate random full name for registration."""
    first_names = ["Alex", "Ryan", "Jordan", "Casey", "Taylor", "Morgan", "Riley", "Quinn", "Avery", "Blake"]
    last_names = ["Smith", "Lee", "Park", "Kim", "Chen", "Wang", "Silva", "Santos", "Lopez", "Garcia"]
    return f"{random.choice(first_names)} {random.choice(last_names)}"


def _random_password():
    """Generate random password meeting Site.pro requirements."""
    chars = string.ascii_letters + string.digits
    pw = ''.join(random.choices(chars, k=12))
    return pw + "@" + random.choice(string.digits)


def _random_email_user():
    """Generate email username: wa_fix_XXXXX."""
    return f"wa_fix_{random.randint(10000, 99999)}"


def sitepro_register(session, temp_email, name, password, csrf, captcha_token):
    """Register akun Site.pro via requests (requires valid captcha_token).
    Returns (success: bool, session, message: str)."""
    try:
        data = {
            'redir_url': '',
            'refId': '',
            'plan': '0',
            'social_submit': '',
            'forced_user_mtype': '',
            'user_mtype': '0',
            'plan_id': '',
            'plan_cycle': '',
            'regPageId': '700',
            'regUrl': 'https://site.pro/id/',
            'regBtn': 'create-new-website-C',
            'regType': '0',
            'regTag': '',
            'regBuyDomain': '',
            'coupon': '',
            'open_from_facebook': '0',
            'open_from_facebook_asia': '0',
            'g-recaptcha-response': captcha_token,
            'create_email': temp_email,
            'create_name': name,
            'create_pass': password,
            'lc': csrf,
        }
        r = session.post(
            "https://site.pro/id/ul/register/",
            data=data,
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': 'https://site.pro',
                'Referer': 'https://site.pro/id/',
            },
            allow_redirects=False,
            timeout=30,
        )
        if r.status_code == 302:
            loc = r.headers.get('Location', '')
            if loc:
                session.get(loc, timeout=15)
            return True, session, "Register berhasil, menunggu OTP..."
        else:
            return False, session, f"Register gagal (status {r.status_code})"
    except Exception as e:
        return False, session, f"Register error: {e}"


# Shared state for profile cloning (template = hasil clone profil utama 1x)
_cloned_profile_cache = {'path': None, 'lock': threading.Lock()}

# NopeCHA extension ID (untuk minimal profile clone)
NOPECHA_EXT_ID = "dknlfmjaanfblgfdfebhijalfmhmjjjo"


def _local_appdata():
    """LOCALAPPDATA yang robust — JANGAN pakai os.environ['LOCALAPPDATA'].

    Kalau proses jalan TANPA env LOCALAPPDATA (service Windows, scheduled task,
    atau spawn yang gak mewarisi environment shell), bracket-access lempar
    KeyError('LOCALAPPDATA') -> register crash: "Browser register error:
    'LOCALAPPDATA'" -> pool sender kosong -> "Tidak ada sender siap".
    Fallback: %USERPROFILE%\\AppData\\Local, lalu ~/AppData/Local.
    """
    import os
    p = os.environ.get('LOCALAPPDATA')
    if p:
        return p
    up = os.environ.get('USERPROFILE') or os.path.expanduser('~')
    return os.path.join(up, 'AppData', 'Local')


def _chrome_user_data_dir():
    """Path profil Chrome utama (User Data) lintas-environment Windows."""
    import os
    return os.path.join(_local_appdata(), 'Google', 'Chrome', 'User Data')


_port_lock = threading.Lock()
_port_next = [9333]  # counter port debug, dinaikkan berurutan (race-free)


def _free_port():
    """Alokasikan port debug UNIK & bebas untuk tiap browser.

    PENTING: pakai counter + lock, BUKAN bind(port 0). bind(0) bisa kasih port
    yang sama ke 2 thread (OS dipakai-ulang setelah close) -> 2 Chrome rebutan
    port yang sama -> DrissionPage error "the user folder does conflict".
    """
    import socket
    with _port_lock:
        for _ in range(5000):
            port = _port_next[0]
            _port_next[0] += 1
            if _port_next[0] > 60000:
                _port_next[0] = 9333
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(('127.0.0.1', port))
                s.close()
                return port  # port ini belum dipakai & belum dibagikan thread lain
            except OSError:
                s.close()
                continue
    # fallback terakhir
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _build_min_template():
    """Bangun template profil MINIMAL: hanya file yang dibutuhkan agar NopeCHA
    terdaftar & aktif. Sangat cepat (~0.1s, ~6MB) vs clone profil penuh (53s+169s).
    Dipakai bersama semua instance (di-cache di _cloned_profile_cache).
    """
    import os
    import shutil
    import tempfile
    main_profile = _chrome_user_data_dir()

    def cp_file(src, dst):
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass

    def cp_tree(src, dst):
        if os.path.isdir(src):
            try:
                shutil.copytree(src, dst, dirs_exist_ok=True)
            except Exception:
                pass

    tmpl = os.path.join(tempfile.gettempdir(), 'sitepro_min_clone')
    if os.path.isdir(tmpl):
        shutil.rmtree(tmpl, ignore_errors=True)
    # Local State (top-level) + prefs Default -> registrasi & MAC extension
    cp_file(os.path.join(main_profile, 'Local State'),
            os.path.join(tmpl, 'Local State'))
    for f in ('Preferences', 'Secure Preferences'):
        cp_file(os.path.join(main_profile, 'Default', f),
                os.path.join(tmpl, 'Default', f))
    # File extension NopeCHA + setting tersimpan (API key/config)
    cp_tree(os.path.join(main_profile, 'Default', 'Extensions', NOPECHA_EXT_ID),
            os.path.join(tmpl, 'Default', 'Extensions', NOPECHA_EXT_ID))
    cp_tree(os.path.join(main_profile, 'Default', 'Local Extension Settings', NOPECHA_EXT_ID),
            os.path.join(tmpl, 'Default', 'Local Extension Settings', NOPECHA_EXT_ID))
    for d in ('Extension State', 'Extension Rules', 'Extension Scripts'):
        cp_tree(os.path.join(main_profile, 'Default', d),
                os.path.join(tmpl, 'Default', d))
    return tmpl

# ── Window tiling: tiap browser dapat slot grid (kiri→kanan, lalu turun) ──
WIN_W, WIN_H = 480, 380     # ukuran window kecil & rapi
WIN_COLS = 4                # 4 kolom per baris
WIN_X0, WIN_Y0 = 20, 20     # offset awal dari pojok kiri-atas
_window_slot = {'n': 0, 'lock': threading.Lock()}


def _next_window_pos():
    """Alokasikan posisi window berikutnya dalam grid rapi. Return (x, y)."""
    with _window_slot['lock']:
        slot = _window_slot['n']
        _window_slot['n'] = (_window_slot['n'] + 1) % (WIN_COLS * 3)  # 3 baris siklus
    col = slot % WIN_COLS
    row = slot // WIN_COLS
    x = WIN_X0 + col * (WIN_W + 10)
    y = WIN_Y0 + row * (WIN_H + 10)
    return x, y


def _cleanup_clone_profile():
    """Reset template clone cache so next attempt re-clones from main profile.

    Dipanggil setelah register fail/timeout — sebab template lama bisa kena
    state aneh (cookies, recaptcha cache) yang bikin NopeCHA stuck.
    """
    import os
    import shutil
    with _cloned_profile_cache['lock']:
        path = _cloned_profile_cache.get('path')
        _cloned_profile_cache['path'] = None
        if path and os.path.isdir(path):
            try:
                shutil.rmtree(path, ignore_errors=True)
                print("[SITEPRO] Cleared cloned profile template")
            except Exception as e:
                print(f"[SITEPRO] Cleanup warn: {e}")


def sitepro_register_browser(temp_email, name, password, headless=False, timeout=300):
    """
    Register akun Site.pro menggunakan DrissionPage (Chromium automation).
    Menggunakan CHROME PROFILE UTAMA (bukan temp profile).
    reCAPTCHA v2 invisible auto-solves via NopeCHA extension.
    Window kecil & rapi (800x600).

    Flow:
    1. Load site.pro/id/ di Chrome utama + NopeCHA
    2. Click "Mulai Gratis" -> "Daftar dengan Email"
    3. Fill form (email, name, password)
    4. Click "Daftar" submit -> reCAPTCHA invisible auto-executes
    5. Wait for redirect to confirm page (NopeCHA solves challenge if needed)
    6. Poll OTP dari emailqu -> confirm
    7. Login via requests for proper session

    Returns (success: bool, requests_session, message: str)
    - requests_session sudah logged-in via email/password login setelah register
    """
    from DrissionPage import Chromium, ChromiumOptions
    import os
    import glob
    import tempfile
    import shutil

    # NopeCHA extension base dir (auto-detect version folder) — pakai path
    # robust lintas-environment, jangan hardcode C:\Users\USER\...
    NOPECHA_BASE = os.path.join(_chrome_user_data_dir(), 'Default',
                                'Extensions', NOPECHA_EXT_ID)

    def _find_nopecha_ext():
        """Find the NopeCHA extension folder (any version)."""
        if not os.path.isdir(NOPECHA_BASE):
            return None
        candidates = sorted(glob.glob(os.path.join(NOPECHA_BASE, "*")), reverse=True)
        for c in candidates:
            if os.path.isfile(os.path.join(c, "manifest.json")):
                return c
        return None

    browser = None
    tmp_profile = None
    try:
        # PROFIL STRATEGY (Chrome 149+ blokir --load-extension, NopeCHA sudah
        # ter-install di profil utama; 2 Chromium TIDAK boleh share user-data-dir):
        #   1) Bangun TEMPLATE minimal (cuma NopeCHA + prefs) 1x -> ~0.1s, ~6MB.
        #   2) Tiap instance copy template -> dir unik (cepat, aman paralel).
        cache = _cloned_profile_cache
        with cache['lock']:
            if cache['path'] is None or not os.path.isdir(cache['path']):
                print(f"[SITEPRO] Membangun template profil minimal (NopeCHA)...")
                cache['path'] = _build_min_template()
                print(f"[SITEPRO] Template profil siap (NopeCHA included).")
            template_dir = cache['path']

        win_x, win_y = _next_window_pos()

        # Launch dengan RETRY: kalau bentrok ("user folder does conflict" / port
        # rebutan), coba lagi dengan port + folder BARU. ChromiumOptions dibuat
        # ulang tiap attempt biar state address-nya bersih.
        launch_err = None
        for attempt in range(1, 4):
            port = _free_port()
            instance_dir = tempfile.mkdtemp(prefix='sitepro_inst_')
            try:
                shutil.copytree(template_dir, instance_dir, dirs_exist_ok=True)
            except Exception as e:
                print(f"[SITEPRO] copy template warn: {e}")
            tmp_profile = instance_dir  # hapus saat finally

            co = ChromiumOptions()
            co.set_argument('--no-sandbox')
            co.set_argument('--disable-dev-shm-usage')
            co.set_argument('--lang=id-ID')
            co.set_argument(f'--window-size={WIN_W},{WIN_H}')
            co.set_argument(f'--window-position={win_x},{win_y}')
            co.set_local_port(port)
            co.set_user_data_path(instance_dir)
            co.set_argument('--profile-directory=Default')
            if headless:
                co.set_argument('--headless=new')
                co.set_argument('--disable-blink-features=AutomationControlled')

            print(f"[SITEPRO] Starting browser @ {win_x},{win_y} port={port} (attempt {attempt})...")
            try:
                browser = Chromium(co)
                tab = browser.latest_tab
                launch_err = None
                break
            except Exception as e:
                launch_err = e
                print(f"[SITEPRO] Launch gagal (attempt {attempt}): {e}")
                browser = None
                try:
                    shutil.rmtree(instance_dir, ignore_errors=True)
                except Exception:
                    pass
                tmp_profile = None
                time.sleep(1.5)

        if browser is None:
            return False, None, f"Browser gagal start (3x coba): {launch_err}"

        # Step 1: Load page, then clear cookies & reload to ensure logged-out state
        # (main Chrome profile may already be logged in to Site.pro from prior runs)
        print(f"[SITEPRO] Loading site.pro/id/...")
        tab.get("https://site.pro/id/", timeout=30)
        try:
            tab.set.cookies.clear()
            tab.get("https://site.pro/id/", timeout=30)
        except Exception as e:
            print(f"[SITEPRO] clear cookies warn: {e}")
        # Tunggu lebih lama supaya NopeCHA extension sempat inject ke DOM
        time.sleep(6)

        # Step 2: Open registration modal via JS-click.
        # native .click() gagal "This element has no location or size" di window
        # kecil (viewport sempit). JS-click jalan di elemen apapun.
        tab.run_js(r"""
            document.querySelectorAll('a,button,div,span').forEach(function(e){
                if((e.textContent||'').trim().indexOf('Mulai Gratis')===0){ e.click(); }
            });
        """)
        time.sleep(2)
        tab.run_js(r"""
            document.querySelectorAll('a,button,div,span').forEach(function(e){
                if((e.textContent||'').trim().indexOf('Daftar dengan Email')>=0){ e.click(); }
            });
        """)
        time.sleep(2)

        # Step 3: Fill form via JS
        print(f"[SITEPRO] Filling form...")
        fill_ok = tab.run_js(f"""
            var e = document.querySelector('input[name="create_email"]');
            var n = document.querySelector('input[name="create_name"]');
            var p = document.querySelector('input[name="create_pass"]');
            if (e) {{ e.focus(); e.value = '{temp_email}'; e.dispatchEvent(new Event('input', {{bubbles:true}})); e.dispatchEvent(new Event('change', {{bubbles:true}})); }}
            if (n) {{ n.focus(); n.value = '{name}'; n.dispatchEvent(new Event('input', {{bubbles:true}})); n.dispatchEvent(new Event('change', {{bubbles:true}})); }}
            if (p) {{ p.focus(); p.value = '{password}'; p.dispatchEvent(new Event('input', {{bubbles:true}})); p.dispatchEvent(new Event('change', {{bubbles:true}})); }}
            return e && n && p ? 'ok' : 'missing';
        """)
        if fill_ok != 'ok':
            return False, None, "Form fields not found"
        time.sleep(1)

        # Step 4: Click submit (triggers CaptchaExecCaptchaField0)
        print(f"[SITEPRO] Submitting form (captcha auto-solve via NopeCHA)...")
        tab.run_js("var b = document.querySelector('.btn-register-submit'); if(b) b.click();")

        # Step 4b: Beri NopeCHA waktu mulai deteksi reCAPTCHA.
        print(f"[SITEPRO] Menunggu NopeCHA mulai solve (5s)...")
        time.sleep(5)

        # Step 4c: Tunggu captcha SELESAI — deteksi aktif sampai tidak ada captcha
        # lagi (token g-recaptcha terisi & popup challenge hilang). Jendela 30-60s.
        print(f"[SITEPRO] Menunggu captcha selesai (deteksi sampai hilang, maks 60s)...")
        captcha_deadline = time.time() + 60
        while time.time() < captcha_deadline:
            try:
                url = tab.url
                html = tab.html
            except Exception:
                time.sleep(2)
                continue
            # Sudah pindah halaman = captcha pasti sudah lewat
            if ('confirmCode' in html or 'check-activated' in url
                    or 'Pilih-layanan' in url or 'Website-Saya' in url):
                break
            try:
                cap = tab.run_js("""
                    var token = '';
                    var ta = document.querySelector('textarea[name="g-recaptcha-response"]');
                    if (ta) token = ta.value || '';
                    var bf = document.querySelector('.b-frame');
                    var challenge = bf ? (bf.offsetHeight > 200) : false;
                    return { solved: token.length > 0, challenge: challenge };
                """) or {}
            except Exception:
                cap = {}
            if cap.get('challenge'):
                print("    [CAPTCHA] Challenge popup tampil, NopeCHA solving...")
                time.sleep(4)
                continue
            if cap.get('solved'):
                print("    [CAPTCHA] Token terisi & tidak ada captcha lagi -> lanjut.")
                break
            time.sleep(3)

        # Step 5: Wait for captcha + confirm page
        deadline = time.time() + timeout
        registered = False
        challenge_seen = False
        csrf_retries = 0
        MAX_CSRF_RETRIES = 3
        while time.time() < deadline:
            try:
                html = tab.html
                url = tab.url
            except Exception:
                time.sleep(3)
                continue

            if 'confirmCode' in html or 'check-activated' in url:
                print(f"[SITEPRO] Register berhasil! (confirm page)")
                registered = True
                break

            if 'Pilih-layanan' in url or 'Website-Saya' in url:
                print(f"[SITEPRO] Register berhasil! (redirected)")
                registered = True
                break

            # CSRF token mismatch: muncul kalau "Daftar" diklik SEBELUM captcha
            # selesai. Fix: tunggu ~20 detik (biar NopeCHA selesai), lalu klik
            # tombol submit lagi -> halaman redirect ke halaman captcha.
            html_low = html.lower()
            if ('csrf' in html_low or 'token mismatch' in html_low
                    or 'token tidak' in html_low or 'token tidak cocok' in html_low):
                if csrf_retries < MAX_CSRF_RETRIES:
                    csrf_retries += 1
                    print(f"    [CSRF] Token mismatch terdeteksi (captcha belum selesai). "
                          f"Tunggu 20s lalu klik ulang (retry {csrf_retries}/{MAX_CSRF_RETRIES})...")
                    time.sleep(20)
                    try:
                        tab.run_js("var b = document.querySelector('.btn-register-submit'); if(b) b.click();")
                    except Exception:
                        pass
                    time.sleep(8)
                    continue
                else:
                    print(f"    [CSRF] Token mismatch terus berulang, menyerah.")
                    break

            # Check captcha challenge popup (image grid)
            try:
                status = tab.run_js("""
                    var frame = document.querySelector('.b-frame');
                    return { frameH: frame ? frame.offsetHeight : 0, url: window.location.href };
                """)
                challenge_visible = bool(status and status.get('frameH', 0) > 500)
                if challenge_visible:
                    if not challenge_seen:
                        print(f"    [CAPTCHA] Challenge popup (h={status['frameH']}), NopeCHA sedang solve...")
                        challenge_seen = True
                    # Beri waktu lebih lama untuk image challenge
                    time.sleep(5)
                    continue
            except Exception:
                pass

            time.sleep(3)

        if not registered:
            # Cleanup browser dulu agar lock dilepas sebelum hapus profile cache
            try:
                if browser:
                    browser.quit()
            except Exception:
                pass
            browser = None
            _cleanup_clone_profile()
            return False, None, f"Register timeout ({timeout}s)"

        # Step 6: Poll OTP and confirm via browser
        print(f"[SITEPRO] Menunggu OTP email...")
        body = emailqu_poll_inbox(temp_email, timeout=90, interval=3)
        if not body:
            return False, None, "OTP email timeout (90s)"

        otp = emailqu_extract_otp(body)
        if not otp:
            return False, None, "Gagal extract OTP dari email"
        print(f"[SITEPRO] OTP: {otp}")

        # Submit OTP
        tab.run_js(f"""
            var forms = document.querySelectorAll('form');
            for (var i = 0; i < forms.length; i++) {{
                var cf = forms[i].querySelector('input[name="confirmCode"]');
                if (cf) {{
                    cf.value = '{otp}';
                    forms[i].submit();
                    break;
                }}
            }}
        """)
        time.sleep(5)
        print(f"[SITEPRO] OTP submitted! URL: {tab.url}")

    except Exception as e:
        return False, None, f"Browser register error: {e}"
    finally:
        if browser:
            try:
                browser.quit()
            except:
                pass
        if tmp_profile:
            import shutil
            try:
                shutil.rmtree(tmp_profile, ignore_errors=True)
            except:
                pass

    # Step 7: Login via requests using registered email/password
    print(f"[SITEPRO] Login via requests (email={temp_email})...")
    session = _sitepro_session()
    csrf, session = sitepro_get_csrf(session)
    if not csrf:
        return False, None, "Gagal ambil CSRF untuk login"

    try:
        r = session.post(
            "https://site.pro/id/ul/",
            data={
                'redir_url': '',
                'form_type': '1',
                'social_submit': '',
                'loginBuyDomain': '',
                'loginOrderService': '',
                'plan_id': '',
                'plan_cycle': '',
                'user_login': temp_email,
                'user_password': password,
            },
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': 'https://site.pro',
                'Referer': 'https://site.pro/id/',
            },
            allow_redirects=True,
            timeout=30,
        )
        phpsessid = session.cookies.get('PHPSESSID', 'none')
        print(f"[SITEPRO] Login! PHPSESSID={phpsessid[:16]}...")

        # Verify by hitting load-init
        r2 = session.get("https://site.pro/id/Website-Saya/load-init/", timeout=20,
                         headers={'X-Requested-With': 'XMLHttpRequest'})
        try:
            data = r2.json()
            if data.get("error") == "no auth":
                print(f"[SITEPRO] load-init failed, navigating to Website-Saya...")
                session.get("https://site.pro/id/Website-Saya/", timeout=20)
                r3 = session.get("https://site.pro/id/Website-Saya/load-init/", timeout=20,
                                 headers={'X-Requested-With': 'XMLHttpRequest'})
                print(f"[SITEPRO] load-init retry: {r3.text[:100]}")
            else:
                print(f"[SITEPRO] load-init OK!")
        except:
            pass

        return True, session, "Register + login berhasil"
    except Exception as e:
        return False, None, f"Login error: {e}"


def sitepro_confirm_code(session, otp_code):
    """Konfirmasi OTP code di Site.pro. Returns (success, session, message)."""
    try:
        r = session.post(
            "https://site.pro/id/",
            data={'confirmCode': otp_code},
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': 'https://site.pro',
                'Referer': 'https://site.pro/id/',
            },
            allow_redirects=True,
            timeout=30,
        )
        if r.status_code == 200 and ('Pilih-layanan' in r.url or 'Website-Saya' in r.url or 'choose' in r.url.lower()):
            return True, session, "Akun berhasil diaktifkan!"
        # Check if redirected to a good page
        if r.status_code == 200:
            return True, session, "Konfirmasi dikirim"
        return False, session, f"Konfirmasi gagal (status {r.status_code})"
    except Exception as e:
        return False, session, f"Konfirmasi error: {e}"


def sitepro_create_website(session, csrf):
    """Buat website di Site.pro. Returns (website_id, domain_name, message)."""
    try:
        r = session.post(
            "https://site.pro/id/Website-Saya/create-website",
            json={"csrfToken": csrf},
            headers={
                'Content-Type': 'application/json;charset=UTF-8',
                'Accept': 'application/json, text/plain, */*',
                'Origin': 'https://site.pro',
                'Referer': 'https://site.pro/id/Website-Saya/',
                'X-Requested-With': 'XMLHttpRequest',
            },
            timeout=30,
        )
        data = r.json()
        if data.get("ok"):
            ws_data = data.get("data", {})
            ws_id = ws_data.get("id")
            domains = ws_data.get("domains", [])
            domain_name = domains[0]["name"] if domains else "unknown"
            return ws_id, domain_name, "Website dibuat"
        return None, None, f"Create website gagal: {data}"
    except Exception as e:
        return None, None, f"Create website error: {e}"


def sitepro_create_mailbox(session, csrf, email_user, email_password):
    """Buat mailbox di siteprofree.email. Returns (mailbox_id, email, message)."""
    try:
        r = session.post(
            "https://site.pro/id/mailbox/add-mailbox",
            json={
                "domainId": 3,
                "emailUser": email_user,
                "emailPassword": email_password,
                "token": csrf,
            },
            headers={
                'Content-Type': 'application/json;charset=UTF-8',
                'Accept': 'application/json, text/plain, */*',
                'Origin': 'https://site.pro',
                'Referer': 'https://site.pro/id/Website-Saya/',
                'X-Requested-With': 'XMLHttpRequest',
            },
            timeout=30,
        )
        data = r.json()
        if data.get("ok"):
            mb = data.get("data", {})
            return mb.get("id"), mb.get("email"), "Mailbox dibuat"
        return None, None, f"Create mailbox gagal: {data}"
    except Exception as e:
        return None, None, f"Create mailbox error: {e}"


def sitepro_create_mailboxes(session, csrf, password, count=5):
    """
    Buat `count` mailbox (sender) sekaligus untuk 1 akun Site.pro.
    Tiap mailbox = alamat pengirim terpisah di siteprofree.email.
    Returns list of dict: [{'mailbox_id': int, 'email': str}, ...]
    """
    out = []
    for i in range(count):
        email_user = _random_email_user()
        mb_id, sitepro_email, msg = sitepro_create_mailbox(session, csrf, email_user, password)
        if mb_id:
            out.append({'mailbox_id': mb_id, 'email': sitepro_email})
            print(f"[SITEPRO] Mailbox {i+1}/{count}: {sitepro_email}")
        else:
            print(f"[SITEPRO] Mailbox {i+1}/{count} gagal: {msg}")
        time.sleep(1)
    return out


# ════════════════════════════════════════════════════════════════
#  ROUNDCUBE WEBMAIL — Login & Send Email
# ════════════════════════════════════════════════════════════════

def _sitepro_open_webmail_once(session, mailbox_id):
    """Satu kali percobaan login Roundcube via SSO Site.pro."""
    # Step 1: Get SSO token from Site.pro
    r = session.post(
        "https://site.pro/id/mailbox/open-webmail",
        json={"mailboxId": mailbox_id},
        headers={
            'Content-Type': 'application/json;charset=UTF-8',
            'Accept': 'application/json, text/plain, */*',
            'Origin': 'https://site.pro',
            'Referer': 'https://site.pro/id/Website-Saya/',
        },
        timeout=(ROUNDCUBE_CONNECT_TIMEOUT, 30),
    )
    data = r.json()
    if not data.get("ok"):
        return None, f"open-webmail gagal: {data}"

    sso = data["data"]
    action_url = sso["action"]
    fields = sso["fields"]

    # Step 2: POST to splogin
    ws = requests.Session()
    ws.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36',
        'Accept-Language': 'id-ID,id;q=0.9,en-US;q=0.6,en;q=0.5',
    })
    r2 = ws.post(
        action_url,
        data={
            'spkey': fields['spkey'],
            'spsign': fields['spsign'],
            'splocale': fields.get('splocale', 'en_US'),
        },
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': 'https://site.pro',
            'Referer': 'https://site.pro/id/Website-Saya/',
        },
        allow_redirects=False,
        timeout=(ROUNDCUBE_CONNECT_TIMEOUT, 30),
    )

    # Step 3: Follow redirect chain to get roundcube_sessid
    for _ in range(5):
        loc = r2.headers.get('Location')
        if not loc:
            break
        if loc.startswith('/'):
            loc = f"https://siteprofree.email{loc}"
        r2 = ws.get(loc, allow_redirects=False, timeout=(ROUNDCUBE_CONNECT_TIMEOUT, 20))

    ws.get(
        "https://siteprofree.email/webmail/?_task=mail&_mbox=INBOX",
        timeout=(ROUNDCUBE_CONNECT_TIMEOUT, 20),
    )
    return ws, "Webmail login berhasil"


def sitepro_open_webmail(session, mailbox_id):
    """Get SSO credentials and login to Roundcube webmail. Returns (webmail_session, message) or (None, error)."""
    last_error = "unknown error"
    for attempt in range(1, WEBMAIL_OPEN_RETRIES + 1):
        try:
            ws, msg = _sitepro_open_webmail_once(session, mailbox_id)
            if ws:
                return ws, msg
            last_error = msg or last_error
        except ReadTimeout:
            last_error = "Read timed out saat login webmail"
        except RequestException as e:
            last_error = str(e)
        except Exception as e:
            last_error = f"Webmail login error: {e}"
        if attempt < WEBMAIL_OPEN_RETRIES:
            time.sleep(min(2 * attempt, 5))
    print(f"[SITEPRO] open_webmail gagal setelah {WEBMAIL_OPEN_RETRIES}x: {last_error}")
    return None, last_error


def roundcube_get_compose_token(webmail_session):
    """Open compose page and extract _token and _from identity. Returns (token, from_id, compose_id) or (None,None,None)."""
    compose_url = "https://siteprofree.email/webmail/?_task=mail&_mbox=INBOX&_action=compose"
    inbox_url = "https://siteprofree.email/webmail/?_task=mail&_mbox=INBOX"
    last_error = "unknown error"

    for attempt in range(1, ROUNDCUBE_COMPOSE_RETRIES + 1):
        try:
            r = webmail_session.get(
                compose_url,
                allow_redirects=True,
                timeout=(ROUNDCUBE_CONNECT_TIMEOUT, ROUNDCUBE_READ_TIMEOUT),
            )
            text = r.text

            if r.status_code == 403:
                last_error = "HTTP 403 saat buka compose"
            elif r.status_code != 200:
                last_error = f"HTTP {r.status_code} saat buka compose"
            else:
                # Extract _token
                m = re.search(r'name=["\']_token["\']\s*value=["\']([^"\']+)["\']', text)
                if not m:
                    m = re.search(r'"request_token"\s*:\s*"([^"]+)"', text)
                token = m.group(1) if m else None

                # Extract compose ID from URL
                m2 = re.search(r'_id=([a-f0-9]+)', r.url)
                compose_id = m2.group(1) if m2 else None

                # Extract _from identity ID.
                # `<option value="0" ... selected` juga muncul di select lain
                # pada halaman compose (mis. prioritas), dan pencarian bebas
                # mengambil yang itu — bukan identitas pengirim. Potong dulu
                # blok <select name="_from"> lalu cari option terpilih di dalamnya.
                from_id = None
                sel = re.search(
                    r'<select[^>]*name=["\']_from["\'][^>]*>(.*?)</select>',
                    text, re.S | re.I,
                )
                if sel:
                    block = sel.group(1)
                    m3 = (re.search(r'<option[^>]*value=["\'](\d+)["\'][^>]*\bselected', block, re.I)
                          or re.search(r'<option[^>]*\bselected[^>]*value=["\'](\d+)["\']', block, re.I)
                          or re.search(r'<option[^>]*value=["\'](\d+)["\']', block, re.I))
                    from_id = m3.group(1) if m3 else None
                if not from_id:
                    m3 = (re.search(r'name=["\']_from["\']\s*value=["\'](\d+)["\']', text)
                          or re.search(r'"identity"\s*:\s*"?(\d+)', text))
                    from_id = m3.group(1) if m3 else None

                if token:
                    return token, from_id, compose_id

                if "login-form" in text or "_task=login" in r.url:
                    last_error = "session webmail tidak valid / kembali ke login"
                else:
                    last_error = "token compose tidak ditemukan"
        except ReadTimeout:
            last_error = f"Read timed out. (read timeout={ROUNDCUBE_READ_TIMEOUT})"
        except RequestException as e:
            last_error = str(e)
        except Exception as e:
            last_error = str(e)

        if attempt < ROUNDCUBE_COMPOSE_RETRIES:
            try:
                webmail_session.get(
                    inbox_url,
                    timeout=(ROUNDCUBE_CONNECT_TIMEOUT, 20),
                )
            except Exception:
                pass
            time.sleep(min(2 * attempt, 5))

    print(f"[SITEPRO] get_compose_token error: {last_error}")
    return None, None, None


def _roundcube_error_from_body(text):
    """Ambil pesan error Roundcube dari body respons `_framed=1`.

    Roundcube SELALU membalas HTTP 200 untuk compose/send — sukses maupun gagal.
    Kegagalan dikirim sebagai panggilan JS `this.display_message('...','error')`
    di dalam body. Tanpa membaca ini, penolakan SMTP (mis. rate limit per
    mailbox) tampil ke pemakai cuma sebagai "status 200" dan tidak bisa
    dibedakan dari masalah jaringan.

    Return string pesan (sudah di-unescape) atau "" kalau tidak ada.
    """
    if not text:
        return ""
    cands = []
    for m in re.finditer(
        r"""display_message\(\s*(['"])(.*?)\1\s*,\s*(['"])(error|warning)\3""",
        text, re.S,
    ):
        cands.append(m.group(2))
    if not cands:
        for m in re.finditer(
            r"""display_message\(\s*(['"])(.*?)\1""", text, re.S,
        ):
            cands.append(m.group(2))
    for raw in cands:
        msg = raw.replace('\\n', ' ').replace("\\'", "'").replace('\\"', '"')
        msg = html_module.unescape(html_module.unescape(msg))
        msg = re.sub(r'<[^>]+>', ' ', msg)
        msg = re.sub(r'\s+', ' ', msg).strip()
        if msg:
            return msg
    return ""


# Penanda penolakan SMTP karena kecepatan/kuota kirim (bukan sender rusak).
_SMTP_RATE_PATTERNS = (
    'too many emails too fast',
    'sending too many',
    'rate limit',
    'ratelimit',
    'try again later',
    'quota exceeded',
    'too many recipients',
    'sasl login name rejected',
    '4.2.1',
    '4.7.1',
    '450',
    '451',
    '452',
)


def _is_smtp_rate_limit(msg):
    """True kalau pesan error SMTP menandakan rate limit / kuota, bukan sender mati."""
    low = (msg or '').lower()
    return any(p in low for p in _SMTP_RATE_PATTERNS)


_BOUNCE_FROM_MARKERS = (
    'mailer-daemon', 'postmaster', 'daemon@', 'mail delivery subsystem',
)
_BOUNCE_SUBJ_MARKERS = (
    'undelivered', 'returned to sender', 'returned mail',
    'delivery status', 'mail delivery failed', 'failure notice',
    'delivery failure', 'delivery notification',
)


def is_whatsapp_support_reply(msg):
    """True jika `msg` tampak balasan ASLI WhatsApp Support (bukan bounce).

    Dipakai Server 1/2 dan RESET OTP. Bounce MAILER-DAEMON sering meng-echo
    isi email kita (mengandung 'whatsapp') → harus ditolak dulu.
    """
    if not msg:
        return False
    frm = str(msg.get('from') or '').lower()
    frm_name = str(msg.get('from_name') or '').lower()
    subj = str(msg.get('subject') or '').lower()
    ctype = str(msg.get('ctype') or '').lower()

    if (any(b in frm for b in _BOUNCE_FROM_MARKERS)
            or any(b in subj for b in _BOUNCE_SUBJ_MARKERS)
            or 'multipart/report' in ctype):
        return False

    # Domain/nama pengirim WhatsApp (utama)
    if 'whatsapp' in frm or 'whatsapp' in frm_name:
        return True
    # Beberapa balasan Meta memakai domain lain tapi subject jelas
    if 'whatsapp' in subj and ('meta' in frm or 'facebook' in frm):
        return True
    return False


def roundcube_read_inbox_folders(webmail_session, timeout=60, fetch_body=True,
                                 body_limit=5, min_uid=0,
                                 folders=('INBOX', 'Junk', 'Spam')):
    """Baca beberapa folder Roundcube (INBOX + Junk/Spam). WA kadang masuk Junk."""
    all_msgs = []
    last_err = None
    # Bagi budget waktu rata ke jumlah folder
    n = max(1, len(folders))
    per = max(12, int(timeout or 60) // n)
    for folder in folders:
        try:
            msgs, err = roundcube_read_inbox(
                webmail_session, timeout=per, fetch_body=fetch_body,
                mbox=folder, body_limit=body_limit, min_uid=min_uid,
            )
            if err and err != 'No messages':
                last_err = err
            for m in (msgs or []):
                m['mbox'] = m.get('mbox') or folder
                all_msgs.append(m)
        except Exception as e:
            last_err = str(e)[:120]
    return all_msgs, last_err



def roundcube_upload_attachment(webmail_session, compose_id, file_path, timeout=60):
    """Upload 1 file ke compose Roundcube. Return attachment id (tanpa prefix rcmfile) atau None.

    HAR siteprofree (2026-08-27):
      POST ?_task=mail&_remote=1&_from=compose&_id=...&_uploadid=upload{ts}&_action=upload
      multipart field: _attachments[] = file
      response exec: add2attachment_list("rcmfile{ID}", ...)
    """
    import os
    import time as _time
    from pathlib import Path as _P
    p = _P(file_path)
    if not p.is_file():
        return None
    ts = int(_time.time() * 1000)
    url = (
        f"https://siteprofree.email/webmail/?_task=mail&_remote=1"
        f"&_from=compose&_id={compose_id}&_uploadid=upload{ts}&_action=upload"
    )
    try:
        with open(p, "rb") as f:
            files = {"_attachments[]": (p.name, f)}
            r = webmail_session.post(
                url,
                files=files,
                headers={
                    "Origin": "https://siteprofree.email",
                    "Referer": (
                        f"https://siteprofree.email/webmail/"
                        f"?_task=mail&_action=compose&_id={compose_id}"
                    ),
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=timeout,
            )
        body = r.text or ""
        # rcmfile223911787835951030399000 -> id = 2239...
        m = re.search(r'add2attachment_list\("rcmfile(\d+)"', body)
        if not m:
            m = re.search(r"rcmfile(\d+)", body)
        if m:
            return m.group(1)
        return None
    except Exception:
        return None


def roundcube_send_email(webmail_session, token, from_id, compose_id, to_email, message, subject="", attachments=None):
    """Send email via Roundcube webmail. Returns (success, message).

    `subject` opsional: default "" supaya pemanggil lama (Server 1/2) tidak berubah
    perilakunya. RESET OTP mengisinya dengan 'Question about WhatsApp for Android'.

    CATATAN: Roundcube membalas HTTP 200 baik sukses maupun gagal, jadi status
    code TIDAK bisa dipakai untuk menilai hasil. Alasan gagal yang sebenarnya
    ada di body (`display_message`) dan diangkat lewat
    _roundcube_error_from_body() supaya pemanggil bisa membedakan rate limit
    SMTP (tunggu / ganti sender) dari sender yang benar-benar rusak.
    """
    try:
        ts = int(time.time() * 1000)
        data = {
            '_token': token,
            '_task': 'mail',
            '_action': 'send',
            '_id': compose_id or '',
            '_attachments': ','.join(attachments) if attachments else '',
            '_from': from_id or '',
            '_to': f"{to_email},",
            '_cc': '',
            '_bcc': '',
            '_replyto': '',
            '_followupto': '',
            '_subject': subject or '',
            '_draft_saveid': '',
            '_draft': '',
            '_is_html': '0',
            '_framed': '1',
            '_message': message,
            'editorSelector': 'plain',
            '_priority': '0',
            '_store_target': 'Sent',
        }
        r = webmail_session.post(
            f"https://siteprofree.email/webmail/?_task=mail&_unlock=loading{ts}&_framed=1&_lang=en",
            data=data,
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': 'https://siteprofree.email',
                'Referer': f'https://siteprofree.email/webmail/?_task=mail&_action=compose&_id={compose_id}',
            },
            timeout=30,
        )
        body = r.text or ""
        if r.status_code == 200 and ('sent_successfully' in body
                                     or 'berhasil dikirim' in body.lower()):
            return True, "Email berhasil dikirim!"

        detail = _roundcube_error_from_body(body)
        if detail:
            return False, detail
        return False, f"Kirim email gagal (status {r.status_code})"
    except Exception as e:
        return False, f"Kirim email error: {e}"



def roundcube_send_email_with_files(webmail_session, token, from_id, compose_id, to_email, message,
                                    subject="", file_paths=None, timeout=60):
    """Upload file_paths lalu kirim email. file_paths = list path (logs/foto)."""
    attach_ids = []
    for fp in (file_paths or []):
        aid = roundcube_upload_attachment(webmail_session, compose_id, fp, timeout=timeout)
        if aid:
            attach_ids.append(aid)
    return roundcube_send_email(
        webmail_session, token, from_id, compose_id, to_email, message,
        subject=subject, attachments=attach_ids,
    )


def roundcube_read_inbox(webmail_session, timeout=120, fetch_body=True, mbox='INBOX',
                         body_limit=5, min_uid=0):
    """
    Baca inbox webmail Roundcube.

    Roundcube `_remote=1` list response menaruh data pesan di field `exec`
    sebagai JavaScript `this.add_message_row(UID, header_obj, info_obj, flagged)`.
    Kita parse panggilan-panggilan itu lewat regex.

    `timeout` = budget total (detik) untuk list + fetch body. Kalau budget habis,
    fungsi tetap mengembalikan pesan yang sudah didapat (body boleh kosong) supaya
    polling balasan tidak menggantung karena satu pesan lambat.
    `body_limit` = maksimum pesan yang body-nya diambil (terbaru dulu).
    `min_uid` = hanya ambil body untuk UID > nilai ini (0 = semua).

    Returns (list_of_messages, error_str_or_None).
    Tiap message: {uid, subject, from, date, size, body (opsional)}.
    """
    deadline = time.time() + max(5, int(timeout or 0))
    list_read_timeout = max(10, min(20, int(timeout or 20)))
    try:
        r = webmail_session.get(
            f"https://siteprofree.email/webmail/?_task=mail&_action=list&_mbox={mbox}&_remote=1&_unlock=0&_page=1",
            timeout=(ROUNDCUBE_CONNECT_TIMEOUT, list_read_timeout),
        )
        if r.status_code != 200:
            return [], f"List gagal: {r.status_code}"

        try:
            data = r.json()
        except Exception:
            return [], "Response bukan JSON"

        exec_js = data.get('exec', '') or ''
        msg_count = data.get('env', {}).get('messagecount', 0)
        if not exec_js or msg_count == 0:
            return [], "No messages"

        # Pattern: this.add_message_row(UID, {header_json}, {info_json}, flagged);
        # Iterasi semua panggilan, extract JSON pakai counter brace.
        import re as _re
        results = []
        for m in _re.finditer(r'this\.add_message_row\(\s*(\d+)\s*,\s*', exec_js):
            uid = int(m.group(1))
            j = m.end()
            header_obj = _extract_json_obj(exec_js, j)
            if not header_obj:
                continue
            j_after = _find_obj_end(exec_js, j) + 1
            while j_after < len(exec_js) and exec_js[j_after] in ', \t\n':
                j_after += 1
            info_obj = _extract_json_obj(exec_js, j_after) or {}

            fromto_html = header_obj.get('fromto', '') or ''
            ma = _re.search(r'title="([^"]+)"', fromto_html)
            from_addr = html_module.unescape(ma.group(1)).strip() if ma else ''
            # Fallback: ekstrak email mentah dari HTML kalau title kosong/aneh
            if not from_addr or '@' not in from_addr:
                ma2 = _re.search(
                    r'([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})',
                    fromto_html,
                )
                if ma2:
                    from_addr = ma2.group(1)
            from_name = _re.sub(r'<[^>]+>', '', fromto_html).strip()
            from_name = html_module.unescape(from_name)

            results.append({
                'uid': uid,
                'subject': header_obj.get('subject', ''),
                'from': from_addr or from_name,
                'from_name': from_name,
                'date': header_obj.get('date', ''),
                'size': header_obj.get('size', ''),
                'mbox': (info_obj or {}).get('mbox', mbox),
                'ctype': (info_obj or {}).get('ctype', ''),
                'body': '',
            })

        # Optional: fetch body. Ambil pesan TERBARU dulu (uid desc) dan dibatasi
        # body_limit + budget waktu, karena _action=show di siteprofree.email
        # sering lambat/timeout kalau dipanggil untuk seluruh inbox.
        if fetch_body and results:
            targets = [msg for msg in results if int(msg['uid']) > int(min_uid or 0)]
            targets.sort(key=lambda x: int(x['uid']), reverse=True)
            if body_limit:
                targets = targets[:int(body_limit)]
            for msg in targets:
                remaining = deadline - time.time()
                if remaining <= 3:
                    print(f"[SITEPRO] read_inbox: budget habis, body {len(targets)} pesan tidak lengkap")
                    break
                msg['body'] = _fetch_message_body(
                    webmail_session, msg['uid'], msg['mbox'], deadline=deadline
                )

        return results, None
    except ReadTimeout:
        return [], f"Read timed out saat list inbox (read timeout={list_read_timeout})"
    except RequestException as e:
        return [], f"Read inbox error: {e}"
    except Exception as e:
        return [], f"Read inbox error: {e}"


def _extract_json_obj(s, start):
    """Extract JSON object starting at position `start` in string `s`. Returns dict or None."""
    if start >= len(s) or s[start] != '{':
        return None
    end = _find_obj_end(s, start)
    if end < 0:
        return None
    try:
        return json.loads(s[start:end + 1])
    except Exception:
        return None


def _find_obj_end(s, start):
    """Find position of closing brace matching opening at `start`. Returns index or -1."""
    if start >= len(s) or s[start] != '{':
        return -1
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(s)):
        c = s[i]
        if escape:
            escape = False
            continue
        if c == '\\':
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i
    return -1


def _strip_html_to_text(body_html, limit=3000):
    """Bersihkan HTML jadi plain text (dipakai body email Roundcube)."""
    import re as _re
    text = _re.sub(r'<style[^>]*>.*?</style>', '', body_html or '', flags=_re.DOTALL)
    text = _re.sub(r'<script[^>]*>.*?</script>', '', text, flags=_re.DOTALL)
    text = _re.sub(r'<br\s*/?>', '\n', text)
    text = _re.sub(r'</p>', '\n\n', text)
    text = _re.sub(r'<[^>]+>', ' ', text)
    text = html_module.unescape(text)
    text = _re.sub(r'[ \t]+', ' ', text)
    text = _re.sub(r'\n{3,}', '\n\n', text).strip()
    return text[:limit]


def _fetch_message_source(webmail_session, uid, mbox='INBOX', read_timeout=15):
    """Fallback murah: ambil raw source pesan (_action=viewsource) lalu jadikan text.

    Dipakai kalau `_action=show` timeout — viewsource tidak merender template
    Roundcube jadi jauh lebih cepat. Body base64 didecode supaya keyword
    (mis. 'whatsapp') tetap terbaca oleh pemanggil.
    """
    try:
        r = webmail_session.get(
            f"https://siteprofree.email/webmail/?_task=mail&_action=viewsource&_mbox={mbox}&_uid={uid}",
            timeout=(ROUNDCUBE_CONNECT_TIMEOUT, read_timeout),
        )
        if r.status_code != 200:
            return ""
        raw = r.text
        import re as _re
        if _re.search(r'Content-Transfer-Encoding:\s*base64', raw, _re.IGNORECASE):
            import base64 as _b64
            decoded_parts = []
            for chunk in _re.split(r'\r?\n\r?\n', raw):
                candidate = _re.sub(r'\s+', '', chunk)
                if len(candidate) < 24 or not _re.fullmatch(r'[A-Za-z0-9+/=]+', candidate):
                    continue
                try:
                    decoded_parts.append(
                        _b64.b64decode(candidate + '=' * (-len(candidate) % 4)).decode('utf-8', 'ignore')
                    )
                except Exception:
                    continue
            if decoded_parts:
                raw = raw + "\n" + "\n".join(decoded_parts)
        # Quoted-printable soft line break + hex escape
        raw = raw.replace('=\r\n', '').replace('=\n', '')
        raw = _re.sub(r'=([0-9A-Fa-f]{2})', lambda m: chr(int(m.group(1), 16)), raw)
        # Raw source = header + body. Header dipangkas (sisakan From/Subject saja)
        # supaya isi pesan tidak terpotong oleh limit karakter.
        parts = _re.split(r'\r?\n\r?\n', raw, maxsplit=1)
        if len(parts) == 2 and len(parts[0]) < 8000:
            keep = [
                ln for ln in parts[0].splitlines()
                if _re.match(r'^(From|Subject|Return-Path|Reply-To)\s*:', ln, _re.IGNORECASE)
            ]
            raw = "\n".join(keep) + "\n\n" + parts[1]
        # `<support@whatsapp.com>` bakal ikut kebuang oleh strip tag HTML, padahal
        # alamat pengirim dipakai pemanggil untuk mendeteksi balasan WhatsApp.
        raw = _re.sub(r'<([^<>@\s]+@[^<>@\s]+)>', r'\1', raw)
        return _strip_html_to_text(raw)
    except Exception as e:
        print(f"[SITEPRO] _fetch_message_source uid={uid}: {e}")
        return ""


def _fetch_message_body(webmail_session, uid, mbox='INBOX', deadline=None):
    """Fetch full HTML body of a message via _action=show, then strip to text.

    `_action=show` di siteprofree.email kadang lambat (>30s) dan bikin read timeout.
    Karena itu: read timeout dipendekkan, dicoba beberapa kali, dan kalau tetap
    timeout dipakai fallback `_action=viewsource` yang lebih ringan. Kalau semua
    gagal, kembalikan "" (pemanggil menganggap body belum tersedia, bukan error fatal).
    `deadline` (epoch detik) membatasi total waktu supaya polling tidak menggantung.
    """
    import re as _re
    last_error = None

    for attempt in range(1, ROUNDCUBE_BODY_RETRIES + 1):
        if deadline is not None:
            remaining = deadline - time.time()
            if remaining <= 3:
                break
            read_timeout = max(5, min(ROUNDCUBE_BODY_READ_TIMEOUT, int(remaining) - 2))
        else:
            read_timeout = ROUNDCUBE_BODY_READ_TIMEOUT

        try:
            r = webmail_session.get(
                f"https://siteprofree.email/webmail/?_task=mail&_action=show&_mbox={mbox}&_uid={uid}&_extwin=0",
                timeout=(ROUNDCUBE_CONNECT_TIMEOUT, read_timeout),
            )
            if r.status_code != 200:
                last_error = f"HTTP {r.status_code}"
                continue

            html = r.text
            # Roundcube taruh body di <div id="message-htmlpart1"> atau di iframe part.
            m = _re.search(r'<div[^>]*id="message-htmlpart\d+"[^>]*>(.*?)</div>\s*</div>', html, _re.DOTALL)
            body_html = m.group(1) if m else html

            # Fallback: cari iframe body src dan fetch itu
            if not m:
                mif = _re.search(r'<iframe[^>]+src="([^"]+_action=get[^"]+)"', html)
                if mif:
                    src = mif.group(1).replace('&amp;', '&')
                    if src.startswith('/'):
                        src = 'https://siteprofree.email' + src
                    try:
                        r2 = webmail_session.get(
                            src, timeout=(ROUNDCUBE_CONNECT_TIMEOUT, read_timeout)
                        )
                        if r2.status_code == 200:
                            body_html = r2.text
                    except (ReadTimeout, RequestException) as e:
                        print(f"[SITEPRO] body iframe uid={uid}: {e}")

            return _strip_html_to_text(body_html)

        except ReadTimeout:
            last_error = f"Read timed out (read timeout={read_timeout})"
        except RequestException as e:
            last_error = str(e)
        except Exception as e:
            last_error = str(e)

        if attempt < ROUNDCUBE_BODY_RETRIES:
            time.sleep(1)

    # Semua percobaan _action=show gagal → coba raw source yang lebih ringan.
    if deadline is None or (deadline - time.time()) > 6:
        fb_timeout = 15
        if deadline is not None:
            fb_timeout = max(5, min(15, int(deadline - time.time()) - 2))
        text = _fetch_message_source(webmail_session, uid, mbox, read_timeout=fb_timeout)
        if text:
            print(f"[SITEPRO] body uid={uid}: pakai fallback viewsource ({last_error})")
            return text

    print(f"[SITEPRO] _fetch_message_body error uid={uid}: {last_error}")
    return ""


# ════════════════════════════════════════════════════════════════
#  PIPELINE — Full /fix flow orchestrator
# ════════════════════════════════════════════════════════════════

def run_fix_pipeline(nomor, captcha_key="", db_cur=None, db_conn=None, solver_type="browser"):
    """
    Jalankan full pipeline untuk 1 nomor:
    1. Buat temp email (emailqu)
    2. Register akun Site.pro (browser = undetected-chromedriver, atau captcha API)
    3. Poll OTP dan konfirmasi
    4. Buat website
    5. Buat mailbox siteprofree.email
    6. Login webmail
    7. Kirim email ke WhatsApp support

    solver_type: "browser" (default, gratis), "2captcha", "capsolver", "anticaptcha"

    Returns dict dengan hasil setiap step.
    """
    result = {
        'nomor': nomor,
        'success': False,
        'steps': [],
        'temp_email': None,
        'sitepro_email': None,
        'error': None,
    }

    def log(step, ok, msg):
        status = "\u2705" if ok else "\u274c"
        result['steps'].append(f"{status} {step}: {msg}")

    # Step 1: Buat temp email
    temp_email, domain = emailqu_get_temp_email()
    if not temp_email:
        log("Temp Email", False, "Gagal buat email temporary")
        result['error'] = "Gagal buat email temporary"
        return result
    result['temp_email'] = temp_email
    log("Temp Email", True, temp_email)

    # Step 2: Register + OTP (browser or captcha API)
    name = _random_name()
    password = _random_password()

    if solver_type == "browser":
        # DrissionPage: register + auto-solve captcha + OTP in one step (GRATIS)
        ok, session, msg = sitepro_register_browser(temp_email, name, password, headless=False, timeout=300)
        if not ok:
            log("Register (Browser)", False, msg)
            result['error'] = msg
            return result
        log("Register + OTP (Browser)", True, msg)
    else:
        # Use captcha API (paid)
        session = _sitepro_session()
        csrf, session = sitepro_get_csrf(session)
        if not csrf:
            log("CSRF Token", False, "Gagal ambil CSRF token")
            result['error'] = "Gagal ambil CSRF token"
            return result
        log("CSRF Token", True, f"{csrf[:8]}...")

        captcha_token = solve_recaptcha(
            captcha_key,
            "6LeKkToUAAAAAHd9EiB6BaSXazFQ5CFIxmyLFm1Z",
            "https://site.pro/id/",
            solver_type=solver_type,
        )
        if not captcha_token:
            log("reCAPTCHA", False, "Gagal solve captcha")
            result['error'] = "Gagal solve captcha"
            return result
        log("reCAPTCHA", True, "Solved")

        ok, session, msg = sitepro_register(session, temp_email, name, password, csrf, captcha_token)
        if not ok:
            log("Register", False, msg)
            result['error'] = msg
            return result
        log("Register", True, msg)

        # Poll OTP (only needed for API solver mode, browser mode does it internally)
        body = emailqu_poll_inbox(temp_email, timeout=90, interval=3)
        if not body:
            log("OTP Email", False, "Timeout menunggu email OTP (90s)")
            result['error'] = "Timeout OTP"
            return result
        otp = emailqu_extract_otp(body)
        if not otp:
            log("OTP Extract", False, "Gagal extract kode OTP dari email")
            result['error'] = "Gagal extract OTP"
            return result
        log("OTP", True, f"Kode: {otp}")

        ok, session, msg = sitepro_confirm_code(session, otp)
        if not ok:
            log("Konfirmasi", False, msg)
            result['error'] = msg
            return result
        log("Konfirmasi", True, msg)

    # Refresh CSRF after login
    csrf, _ = sitepro_get_csrf(session)

    # Step 6: Buat website (diperlukan sebelum mailbox)
    ws_id, domain_name, msg = sitepro_create_website(session, csrf)
    if not ws_id:
        log("Website", False, msg)
        result['error'] = msg
        return result
    log("Website", True, f"{domain_name}")

    # Step 7: Buat mailbox
    email_user = _random_email_user()
    mb_id, sitepro_email, msg = sitepro_create_mailbox(session, csrf, email_user, password)
    if not mb_id:
        log("Mailbox", False, msg)
        result['error'] = msg
        return result
    result['sitepro_email'] = sitepro_email
    log("Mailbox", True, sitepro_email)

    # Step 8: Login webmail
    ws_session, msg = sitepro_open_webmail(session, mb_id)
    if not ws_session:
        log("Webmail Login", False, msg)
        result['error'] = msg
        return result
    log("Webmail Login", True, msg)

    # Step 9: Compose & kirim email
    token, from_id, compose_id = roundcube_get_compose_token(ws_session)
    if not token:
        log("Compose", False, "Gagal ambil token compose")
        result['error'] = "Gagal ambil token compose"
        return result
    log("Compose", True, f"Token didapat")

    # Format nomor (pastikan format +62xxx)
    nomor_clean = nomor.strip().replace("+", "").replace("-", "").replace(" ", "")
    if not nomor_clean.startswith("62") and nomor_clean.startswith("0"):
        nomor_clean = "62" + nomor_clean[1:]

    message = (
        "Olá, Equipe de Suporte do WhatsApp,\n\n"
        "Estou entrando em contato porque não consigo fazer login na minha conta do WhatsApp. "
        "Toda vez que tento, recebo a seguinte mensagem: \"Login Not Available Right Now.\"\n\n"
        "Já tentei reiniciar o aparelho, verificar minha conexão com a internet e tentar novamente "
        "várias vezes, mas o problema continua sem solução.\n\n"
        "Essa conta é muito importante para mim, pois contém meus grupos de estudo da universidade, "
        "materiais de aula e comunicações acadêmicas importantes. A perda de acesso está afetando meus estudos.\n\n"
        "Número de telefone : "
        f"+{nomor_clean}\n\n"
        "Agradeceria muito se pudessem me ajudar a restaurar o acesso o mais rápido possível. "
        "Obrigado pela atenção e pelo suporte.\n\n"
        "Atenciosamente,\n"
        "DikZz"
    )

    ok, msg = roundcube_send_email(ws_session, token, from_id, compose_id, "support@support.whatsapp.com", message)
    if not ok:
        log("Kirim Email", False, msg)
        result['error'] = msg
        return result
    log("Kirim Email", True, f"Terkirim ke support@support.whatsapp.com")

    # Save to DB if available
    if db_cur and db_conn:
        try:
            phpsessid = session.cookies.get('PHPSESSID', '')
            db_cur.execute(
                """INSERT INTO sitepro_accounts 
                   (user_id, temp_email, sitepro_email, sitepro_password, phpsessid, csrf_token, mailbox_id, website_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sent')""",
                (0, temp_email, sitepro_email, password, phpsessid, csrf or '', mb_id, ws_id)
            )
            db_conn.commit()
        except Exception as e:
            print(f"[SITEPRO] DB save error: {e}")

    result['success'] = True
    return result


def run_create_pipeline(captcha_key="", db_cur=None, db_conn=None, solver_type="browser"):
    """
    Buat 1 akun Site.pro (tanpa kirim email).
    solver_type: "browser" (default, gratis), "2captcha", "capsolver", "anticaptcha"
    Returns dict dengan hasil.
    """
    result = {
        'success': False,
        'steps': [],
        'temp_email': None,
        'sitepro_email': None,
        'error': None,
    }

    def log(step, ok, msg):
        status = "\u2705" if ok else "\u274c"
        result['steps'].append(f"{status} {step}: {msg}")

    # Step 1: Temp email
    temp_email, domain = emailqu_get_temp_email()
    if not temp_email:
        log("Temp Email", False, "Gagal")
        result['error'] = "Gagal buat email temporary"
        return result
    result['temp_email'] = temp_email
    log("Temp Email", True, temp_email)

    # Step 2: Register + OTP
    name = _random_name()
    password = _random_password()

    if solver_type == "browser":
        ok, session, msg = sitepro_register_browser(temp_email, name, password, headless=False, timeout=300)
        if not ok:
            log("Register (Browser)", False, msg)
            result['error'] = msg
            return result
        log("Register + OTP (Browser)", True, msg)
    else:
        session = _sitepro_session()
        csrf, session = sitepro_get_csrf(session)
        if not csrf:
            log("CSRF", False, "Gagal")
            result['error'] = "Gagal ambil CSRF"
            return result
        log("CSRF", True, "OK")

        captcha_token = solve_recaptcha(captcha_key, "6LeKkToUAAAAAHd9EiB6BaSXazFQ5CFIxmyLFm1Z", "https://site.pro/id/", solver_type=solver_type)
        if not captcha_token:
            log("reCAPTCHA", False, "Gagal")
            result['error'] = "Gagal solve captcha"
            return result
        log("reCAPTCHA", True, "Solved")

        ok, session, msg = sitepro_register(session, temp_email, name, password, csrf, captcha_token)
        if not ok:
            log("Register", False, msg)
            result['error'] = msg
            return result
        log("Register", True, msg)

        body = emailqu_poll_inbox(temp_email, timeout=150, interval=3)
        if not body:
            log("OTP", False, "Timeout")
            result['error'] = "Timeout OTP"
            return result
        otp = emailqu_extract_otp(body)
        if not otp:
            log("OTP", False, "Gagal extract")
            result['error'] = "Gagal extract OTP"
            return result
        log("OTP", True, f"Kode: {otp}")

        ok, session, msg = sitepro_confirm_code(session, otp)
        if not ok:
            log("Konfirmasi", False, msg)
            result['error'] = msg
            return result
        log("Konfirmasi", True, msg)

    # Refresh CSRF
    csrf, _ = sitepro_get_csrf(session)

    # Step 5: Create website
    ws_id, domain_name, msg = sitepro_create_website(session, csrf)
    log("Website", ws_id is not None, msg if ws_id else msg)

    # Step 6: Create 5 mailbox (sender) — retry sampai 3x kalau gagal.
    mboxes = None
    for _mb_try in range(3):
        mboxes = sitepro_create_mailboxes(session, csrf, password, count=MAX_MAILBOXES_PER_ACCOUNT)
        if mboxes:
            break
        # Refresh CSRF sebelum coba lagi (token bisa basi).
        try:
            csrf, session = sitepro_get_csrf(session)
        except Exception:
            pass
        time.sleep(2)
    if mboxes:
        result['sitepro_email'] = mboxes[0]['email']
        result['mailboxes'] = [m['email'] for m in mboxes]
        log("Mailbox", True, f"{len(mboxes)} sender dibuat")
    else:
        log("Mailbox", False, "Gagal buat mailbox (3x)")

    # Step 7: Health-check — pastikan webmail mailbox pertama bisa login.
    # Kalau gagal, akun tetap disimpan tapi ditandai 'unhealthy' biar /fix
    # tidak memilih sender yang sebenarnya mati.
    healthy = True
    if mboxes:
        try:
            ws_session, _hc_msg = sitepro_open_webmail(session, mboxes[0]['mailbox_id'])
            healthy = bool(ws_session)
            log("Health-check", healthy, "webmail OK" if healthy else "webmail gagal login")
        except Exception as _hc_e:
            healthy = False
            log("Health-check", False, f"error: {_hc_e}")
    result['healthy'] = healthy

    # Save to DB (akun + semua sender)
    if db_cur and db_conn and mboxes:
        try:
            phpsessid = session.cookies.get('PHPSESSID', '')
            acc_id = _save_account_with_mailboxes(
                db_cur, db_conn,
                {
                    'user_id': 0, 'temp_email': temp_email,
                    'sitepro_email': mboxes[0]['email'],
                    'sitepro_password': password,
                    'phpsessid': phpsessid, 'csrf_token': csrf or '',
                    'website_id': ws_id,
                },
                mboxes,
            )
            # Tandai status akun sesuai hasil health-check.
            if acc_id and not healthy:
                try:
                    db_cur.execute(
                        "UPDATE sitepro_accounts SET status='unhealthy' WHERE id=?",
                        (acc_id,),
                    )
                    db_conn.commit()
                except Exception:
                    pass
        except Exception as e:
            print(f"[SITEPRO] DB save error: {e}")
            result['error'] = f"DB save gagal: {e}"

    result['success'] = bool(mboxes)
    return result


# ════════════════════════════════════════════════════════════════
#  PIPELINE — Multi-send /fix (1 akun → kirim ke banyak nomor)
# ════════════════════════════════════════════════════════════════

MAX_SENDS_PER_ACCOUNT = 5  # Batas compose ke WhatsApp support per mailbox (legacy)
MAX_MAILBOXES_PER_ACCOUNT = 5  # 1 akun Site.pro = maksimal 5 mailbox (sender)
SENDER_COOLDOWN_SECS = 3600    # Sender bisa dipakai lagi setelah 1 jam
# Kalau sender siap tersisa <= ambang ini, otomatis reset cooldown SEMUA
# sender (setara /refresh). Biar /fix & RESET OTP tidak kehabisan pool.
AUTO_REFRESH_READY_THRESHOLD = 10
SENDER_REFRESH_SECS = 600      # Session di-refresh tiap 10 menit biar tidak mati


def _last_insert_rowid(db_cur):
    """Ambil ID baris terakhir (proxy DB tidak punya .lastrowid)."""
    try:
        db_cur.execute("SELECT last_insert_rowid()")
        return db_cur.fetchone()[0]
    except Exception:
        return None


def _save_account_with_mailboxes(db_cur, db_conn, account_fields, mailboxes):
    """
    Simpan akun baru + daftar mailbox-nya.
    account_fields = dict(user_id, temp_email, sitepro_email, sitepro_password,
                          phpsessid, csrf_token, website_id)
    mailboxes = [{'mailbox_id': int, 'email': str}, ...]
    Returns account_id (int) atau None.
    """
    if not db_cur or not db_conn:
        return None
    try:
        first_mb = mailboxes[0]['mailbox_id'] if mailboxes else 0
        db_cur.execute(
            """INSERT INTO sitepro_accounts
               (user_id, temp_email, sitepro_email, sitepro_password,
                phpsessid, csrf_token, mailbox_id, website_id, status, send_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'created', 0)""",
            (
                account_fields.get('user_id', 0),
                account_fields.get('temp_email', ''),
                account_fields.get('sitepro_email', ''),
                account_fields.get('sitepro_password', ''),
                account_fields.get('phpsessid', ''),
                account_fields.get('csrf_token', ''),
                first_mb,
                account_fields.get('website_id', 0),
            ),
        )
        account_id = _last_insert_rowid(db_cur)
        for mb in mailboxes:
            db_cur.execute(
                """INSERT INTO sitepro_mailboxes
                   (account_id, mailbox_id, email, used, compose_count)
                   VALUES (?, ?, ?, 0, 0)""",
                (account_id, mb['mailbox_id'], mb['email']),
            )
        db_conn.commit()
        return account_id
    except Exception as e:
        print(f"[SITEPRO] _save_account_with_mailboxes error: {e}")
        return None


def _pick_available_senders(db_cur, limit=5, cooldown_secs=SENDER_COOLDOWN_SECS, server=None):
    """
    Ambil sender dari SEMUA akun yang sudah lewat cooldown (1 jam).
    Sender belum pernah dipakai (last_used_at NULL) didahulukan, lalu yang
    paling lama tidak dipakai. Tiap item membawa kredensial akun-nya supaya
    session bisa di-restore.

    server: None = semua sender; 1 = m.id <= 150; 2 = m.id > 150.

    Return list of dict: {row_id, mailbox_id, email, account_id, phpsessid,
                          sitepro_email, temp_email, sitepro_password}
    """
    if not db_cur:
        return []
    out = []
    try:
        # Filter partition sesuai pilihan server (BERBASIS RANK, bukan id mentah).
        # Server 1 = 150 sender dengan id TERKECIL (paling lama dibuat).
        # Server 2 = sisanya. Tahan terhadap autoincrement yang naik tinggi
        # setelah penghapusan.
        _s1_ids = (
            "SELECT id FROM sitepro_mailboxes "
            "WHERE mailbox_id IS NOT NULL AND mailbox_id > 0 "
            "ORDER BY id ASC LIMIT 150"
        )
        if server == 1:
            partition_clause = f" AND m.id IN ({_s1_ids})"
        elif server == 2:
            partition_clause = f" AND m.id NOT IN ({_s1_ids})"
        else:
            partition_clause = ""
        db_cur.execute(
            f"""SELECT m.id, m.mailbox_id, m.email, m.account_id,
                       a.phpsessid, a.sitepro_email, a.temp_email, a.sitepro_password
                FROM sitepro_mailboxes m
                JOIN sitepro_accounts a ON a.id = m.account_id
                WHERE m.mailbox_id IS NOT NULL AND m.mailbox_id > 0
                  AND (m.last_used_at IS NULL
                       OR m.last_used_at <= datetime('now', '-{int(cooldown_secs)} seconds'))
                  {partition_clause}
                ORDER BY (m.last_used_at IS NULL) DESC, m.last_used_at ASC, m.id ASC
                LIMIT ?""",
            (limit,),
        )
        for r in db_cur.fetchall():
            out.append({
                'row_id': r[0], 'mailbox_id': r[1], 'email': r[2], 'account_id': r[3],
                'phpsessid': r[4], 'sitepro_email': r[5], 'temp_email': r[6],
                'sitepro_password': r[7],
            })
    except Exception as e:
        print(f"[SITEPRO] _pick_available_senders error: {e}")
    return out


# Lock global: serialize fase pilih+reservasi sender supaya /fix paralel
# tidak menarik sender yang sama (anti-tabrakan).
_SENDER_RESERVE_LOCK = threading.Lock()


def _reserve_available_senders(db_cur, db_conn, limit=5,
                               cooldown_secs=SENDER_COOLDOWN_SECS, server=None):
    """Pilih DAN reservasi sender secara atomik (anti-tabrakan).

    Berbeda dari _pick_available_senders yang hanya membaca, fungsi ini:
      1. Mengunci _SENDER_RESERVE_LOCK (serialize antar thread di pool global).
      2. SELECT sender siap-pakai (cooldown lewat / belum pernah dipakai),
         dengan filter partisi server berbasis rank.
      3. LANGSUNG set last_used_at = now untuk sender terpilih (reservasi) +
         commit, sebelum lock dilepas.

    Efeknya: begitu sender terpilih, cooldown langsung aktif, jadi pemanggil
    lain (run /fix paralel) tidak akan memilih sender yang sama. Kalau
    pengiriman nanti gagal, sender tetap "terpakai" untuk window cooldown —
    trade-off aman demi mencegah tabrakan.

    Return list of dict sama seperti _pick_available_senders.

    Catatan: SQLite sering raise "database is locked" saat /fix, /create, dan
    refresh-session jalan barengan. Dulu exception itu ditelan → return [] →
    UI salah bilang "Tidak ada sender siap" padahal /show masih banyak yang
    ready. Karena itu SELECT+UPDATE di-retry singkat untuk error lock saja.
    """
    if not db_cur or not db_conn:
        return []

    # Partisi berbasis rank (lihat _pick_available_senders).
    _s1_ids = (
        "SELECT id FROM sitepro_mailboxes "
        "WHERE mailbox_id IS NOT NULL AND mailbox_id > 0 "
        "ORDER BY id ASC LIMIT 150"
    )
    if server == 1:
        partition_clause = f" AND m.id IN ({_s1_ids})"
    elif server == 2:
        partition_clause = f" AND m.id NOT IN ({_s1_ids})"
    else:
        partition_clause = ""

    select_sql = (
        f"""SELECT m.id, m.mailbox_id, m.email, m.account_id,
                   a.phpsessid, a.sitepro_email, a.temp_email, a.sitepro_password
            FROM sitepro_mailboxes m
            JOIN sitepro_accounts a ON a.id = m.account_id
            WHERE m.mailbox_id IS NOT NULL AND m.mailbox_id > 0
              AND (m.last_used_at IS NULL
                   OR m.last_used_at <= datetime('now', '-{int(cooldown_secs)} seconds'))
              {partition_clause}
            ORDER BY (m.last_used_at IS NULL) DESC, m.last_used_at ASC, m.id ASC
            LIMIT ?"""
    )

    out = []
    last_err = None
    with _SENDER_RESERVE_LOCK:
        for attempt in range(1, 4):
            out = []
            try:
                # IMMEDIATE = kunci tulis sejak awal, biar SELECT+UPDATE atomik.
                # Kalau DB sedang ditulis thread lain → OperationalError locked → retry.
                try:
                    db_cur.execute("BEGIN IMMEDIATE")
                except Exception:
                    # Beberapa koneksi sudah di dalam transaksi implisit — lanjut saja.
                    pass

                # Auto-refresh: stok siap menipis → buka cooldown semua sender dulu.
                db_cur.execute(
                    f"""SELECT COUNT(*) FROM sitepro_mailboxes
                        WHERE mailbox_id IS NOT NULL AND mailbox_id > 0
                          AND (last_used_at IS NULL
                               OR last_used_at <= datetime('now', '-{int(cooldown_secs)} seconds'))"""
                )
                ready_n = int((db_cur.fetchone() or [0])[0] or 0)
                if ready_n <= AUTO_REFRESH_READY_THRESHOLD:
                    db_cur.execute(
                        """UPDATE sitepro_mailboxes
                           SET last_used_at = NULL
                           WHERE mailbox_id IS NOT NULL AND mailbox_id > 0"""
                    )
                    print(
                        f"[SITEPRO] auto-refresh: ready={ready_n} "
                        f"<= {AUTO_REFRESH_READY_THRESHOLD} → cooldown semua di-reset",
                        flush=True,
                    )

                db_cur.execute(select_sql, (limit,))
                rows = db_cur.fetchall()
                picked_ids = [r[0] for r in rows]

                # Reservasi atomik: set cooldown sekarang juga.
                if picked_ids:
                    placeholders = ",".join(["?"] * len(picked_ids))
                    db_cur.execute(
                        f"""UPDATE sitepro_mailboxes
                            SET last_used_at = CURRENT_TIMESTAMP
                            WHERE id IN ({placeholders})""",
                        picked_ids,
                    )
                db_conn.commit()

                for r in rows:
                    out.append({
                        'row_id': r[0], 'mailbox_id': r[1], 'email': r[2], 'account_id': r[3],
                        'phpsessid': r[4], 'sitepro_email': r[5], 'temp_email': r[6],
                        'sitepro_password': r[7],
                    })
                return out
            except Exception as e:
                last_err = e
                err_l = str(e).lower()
                is_lock = ("database is locked" in err_l
                           or "database table is locked" in err_l
                           or "busy" in err_l)
                try:
                    db_conn.rollback()
                except Exception:
                    pass
                if is_lock and attempt < 3:
                    print(
                        f"[SITEPRO] _reserve_available_senders locked "
                        f"(try {attempt}/3), retry...",
                        flush=True,
                    )
                    time.sleep(0.4 * attempt)
                    continue
                print(f"[SITEPRO] _reserve_available_senders error: {e}", flush=True)
                break
    if last_err and not out:
        # Jangan tipu pemanggil: bedakan pool kosong vs gagal DB.
        print(
            f"[SITEPRO] reserve gagal total (bukan pool kosong): {last_err}",
            flush=True,
        )
    return out


def _mark_sender_composed(db_cur, db_conn, mailbox_row_id, used_for=None):
    """Catat 1 compose: compose_count++, set last_used_at=now (mulai cooldown)."""
    if not db_cur or not db_conn or not mailbox_row_id:
        return
    try:
        db_cur.execute(
            """UPDATE sitepro_mailboxes
               SET compose_count = COALESCE(compose_count, 0) + 1,
                   used = 1, used_for = ?,
                   last_used_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (used_for or '', mailbox_row_id),
        )
        db_cur.execute(
            """UPDATE sitepro_accounts
               SET send_count = COALESCE(send_count, 0) + 1,
                   last_used_at = CURRENT_TIMESTAMP
               WHERE id = (SELECT account_id FROM sitepro_mailboxes WHERE id = ?)""",
            (mailbox_row_id,),
        )
        db_conn.commit()
    except Exception as e:
        print(f"[SITEPRO] _mark_sender_composed error: {e}")


def _release_sender_cooldown(db_cur, db_conn, mailbox_row_id,
                             cooldown_secs=SENDER_COOLDOWN_SECS):
    """Kembalikan sender ke pool jika kirim gagal sebelum compose sukses."""
    if not db_cur or not db_conn or not mailbox_row_id:
        return
    try:
        db_cur.execute(
            f"""UPDATE sitepro_mailboxes
                SET last_used_at = datetime('now', '-{int(cooldown_secs)} seconds')
                WHERE id = ?""",
            (mailbox_row_id,),
        )
        db_conn.commit()
    except Exception as e:
        print(f"[SITEPRO] release cooldown error: {e}")


def refresh_all_sessions(db_cur, db_conn):
    """
    Dipanggil periodik (tiap 10 menit) oleh daemon di bot.
    Restore session tiap akun aktif (yg punya mailbox / healthy) secara paralel.
    Update phpsessid di DB kalau berubah. Return (ok_count, total).
    """
    if not db_cur:
        return (0, 0)
    rows = []
    try:
        # Hanya refresh akun yang memiliki mailbox atau berstatus healthy
        db_cur.execute(
            """SELECT a.id, a.phpsessid, a.sitepro_email, a.temp_email, a.sitepro_password
               FROM sitepro_accounts a
               WHERE (a.sitepro_email IS NOT NULL AND a.sitepro_email != '')
                 AND (a.status IS NULL OR a.status != 'unhealthy')
                 AND a.id IN (SELECT DISTINCT account_id FROM sitepro_mailboxes WHERE account_id IS NOT NULL)"""
        )
        rows = db_cur.fetchall()
        # Fallback jika kueri mailbox kosong: ambil 10 akun terbaru
        if not rows:
            db_cur.execute(
                """SELECT id, phpsessid, sitepro_email, temp_email, sitepro_password
                   FROM sitepro_accounts
                   WHERE (sitepro_email IS NOT NULL AND sitepro_email != '')
                     AND (status IS NULL OR status != 'unhealthy')
                   ORDER BY id DESC LIMIT 10"""
            )
            rows = db_cur.fetchall()
    except Exception as e:
        print(f"[SITEPRO] refresh_all_sessions query error: {e}")
        return (0, 0)

    if not rows:
        return (0, 0)

    from concurrent.futures import ThreadPoolExecutor

    def _worker(r):
        acct_id, phpsessid, sp_email, tmp_email, pw = r
        try:
            sess, valid = sitepro_restore_session(
                phpsessid, email=sp_email or tmp_email, password=pw,
            )
            if valid and sess:
                new_sid = sess.cookies.get('PHPSESSID', '')
                return acct_id, True, new_sid, None
            else:
                return acct_id, False, None, 'auth_failed'
        except Exception as ex:
            return acct_id, False, None, str(ex)

    ok = 0
    with ThreadPoolExecutor(max_workers=min(3, len(rows))) as pool:
        results = list(pool.map(_worker, rows))

    for acct_id, is_valid, new_sid, err in results:
        if is_valid:
            ok += 1
            if new_sid and db_conn:
                try:
                    db_cur.execute(
                        "UPDATE sitepro_accounts SET phpsessid = ?, status = 'healthy' WHERE id = ?",
                        (new_sid, acct_id),
                    )
                    db_conn.commit()
                except Exception:
                    pass
        else:
            if err == 'auth_failed' and db_conn:
                try:
                    db_cur.execute(
                        "UPDATE sitepro_accounts SET status = 'unhealthy' WHERE id = ?",
                        (acct_id,),
                    )
                    db_conn.commit()
                except Exception:
                    pass

    return (ok, len(rows))


def run_fix_multi_pipeline(
    numbers,
    captcha_key="",
    db_cur=None,
    db_conn=None,
    solver_type="browser",
    reply_wait=240,
    progress_cb=None,
    max_sends_per_account=MAX_SENDS_PER_ACCOUNT,
    message_template=None,
    server=None,
):
    """
    Pipeline /fix versi 3 (model 5-mailbox):
      - 1 akun Site.pro = maksimal 5 mailbox (sender). Tiap nomor dikirim dari
        mailbox/sender yang BERBEDA.
      - Coba reuse akun yang masih punya mailbox belum terpakai (used=0).
      - Kalau habis / tidak ada -> register akun baru + buat 5 mailbox sekaligus.
      - Kirim 1 email per nomor dari mailbox berbeda; tandai mailbox terpakai.
      - Polling inbox tiap mailbox -> deteksi balasan baru mengandung 'whatsapp',
        stop begitu balasan pertama masuk.

    Returns dict (+'account_reused', +'sends_left', +'newest_reply').
    """
    out = {
        'success': False,
        'account_ready': False,
        'account_reused': False,
        'sends_left': 0,
        'numbers': [],
        'total_sent': 0,
        'total_replied': 0,
        'replies': [],
        'newest_reply': None,
        'error': None,
        'steps': [],
    }

    def step(ok, label):
        out['steps'].append(("OK " if ok else "X  ") + label)

    def _cb(stage, data):
        if progress_cb:
            try:
                progress_cb(stage, data)
            except Exception:
                pass

    # Normalisasi nomor di awal
    def _norm(n):
        c = str(n).strip().replace("+", "").replace("-", "").replace(" ", "")
        if not c.startswith("62") and c.startswith("0"):
            c = "62" + c[1:]
        return c

    numbers = [_norm(n) for n in numbers]

    def _wa_message(nomor_clean):
        # Kalau user kasih template custom via /set, pakai itu (replace {nomor}).
        if message_template:
            try:
                tpl = str(message_template)
                if "{nomor}" in tpl:
                    return tpl.replace("{nomor}", nomor_clean)
                # Kalau placeholder hilang (harusnya tidak terjadi karena divalidasi di /set),
                # tetap append nomor di akhir biar pesan tetap nyambung.
                return tpl + f"\n\n+{nomor_clean}"
            except Exception:
                pass
        return (
            "Olá, Equipe de Suporte do WhatsApp,\n\n"
            "Estou entrando em contato porque não consigo fazer login na minha conta do WhatsApp. "
            "Toda vez que tento, recebo a seguinte mensagem: \"Login Not Available Right Now.\"\n\n"
            "Já tentei reiniciar o aparelho, verificar minha conexão com a internet e tentar novamente "
            "várias vezes, mas o problema continua sem solução.\n\n"
            "Essa conta é muito importante para mim, pois contém meus grupos de estudo da universidade, "
            "materiais de aula e comunicações acadêmicas importantes. A perda de acesso está afetando meus estudos.\n\n"
            "Número de telefone : "
            f"+{nomor_clean}\n\n"
            "Agradeceria muito se pudessem me ajudar a restaurar o acesso o mais rápido possível. "
            "Obrigado pela atenção e pelo suporte.\n\n"
            "Atenciosamente,\n"
            "DikZz"
        )

    # ── Session cache per akun (restore sekali, pakai untuk semua mailbox-nya) ─
    session_cache = {}

    def _session_for(sender):
        aid = sender['account_id']
        if aid in session_cache:
            return session_cache[aid]
        sess, ok = sitepro_restore_session(
            sender.get('phpsessid'),
            email=sender.get('sitepro_email') or sender.get('temp_email'),
            password=sender.get('sitepro_password'),
        )
        if ok and sess:
            session_cache[aid] = sess
            return sess
        return None

    # ── Fallback: register akun baru + 5 mailbox, kembalikan list sender ──
    def _register_new_account():
        temp_email, _d = emailqu_get_temp_email()
        if not temp_email:
            return None, "Gagal buat alamat sementara"
        name = _random_name()
        password = _random_password()

        if solver_type == "browser":
            ok, sess, msg = sitepro_register_browser(temp_email, name, password, headless=False, timeout=300)
            if not ok:
                return None, f"register: {msg}"
        else:
            sess = _sitepro_session()
            csrf, sess = sitepro_get_csrf(sess)
            if not csrf:
                return None, "Gagal ambil CSRF"
            captcha_token = solve_recaptcha(
                captcha_key, "6LeKkToUAAAAAHd9EiB6BaSXazFQ5CFIxmyLFm1Z",
                "https://site.pro/id/", solver_type=solver_type,
            )
            if not captcha_token:
                return None, "Gagal solve captcha"
            ok, sess, msg = sitepro_register(sess, temp_email, name, password, csrf, captcha_token)
            if not ok:
                return None, f"register: {msg}"
            body = emailqu_poll_inbox(temp_email, timeout=90, interval=3)
            otp = emailqu_extract_otp(body) if body else None
            if not otp:
                return None, "Timeout/Gagal OTP"
            ok, sess, msg = sitepro_confirm_code(sess, otp)
            if not ok:
                return None, f"konfirmasi: {msg}"

        csrf, _ = sitepro_get_csrf(sess)
        ws_id, _domain, msg = sitepro_create_website(sess, csrf)
        if not ws_id:
            return None, f"siapkan akun: {msg}"

        mboxes = sitepro_create_mailboxes(sess, csrf, password, count=MAX_MAILBOXES_PER_ACCOUNT)
        if not mboxes:
            return None, "Gagal buat mailbox"

        phpsessid = sess.cookies.get('PHPSESSID', '')
        acct_id = _save_account_with_mailboxes(
            db_cur, db_conn,
            {
                'user_id': 0, 'temp_email': temp_email,
                'sitepro_email': mboxes[0]['email'],
                'sitepro_password': password,
                'phpsessid': phpsessid, 'csrf_token': csrf or '',
                'website_id': ws_id,
            },
            mboxes,
        )
        if acct_id:
            session_cache[acct_id] = sess  # session sudah hidup, pakai ulang

        new_senders = []
        if acct_id and db_cur:
            db_cur.execute(
                "SELECT id, mailbox_id, email FROM sitepro_mailboxes WHERE account_id = ? ORDER BY id ASC",
                (acct_id,),
            )
            for m in db_cur.fetchall():
                new_senders.append({
                    'row_id': m[0], 'mailbox_id': m[1], 'email': m[2],
                    'account_id': acct_id, 'phpsessid': phpsessid,
                    'sitepro_email': mboxes[0]['email'], 'temp_email': temp_email,
                    'sitepro_password': password,
                })
        else:
            for m in mboxes:
                new_senders.append({
                    'row_id': None, 'mailbox_id': m['mailbox_id'], 'email': m['email'],
                    'account_id': acct_id, 'phpsessid': phpsessid,
                    'sitepro_email': mboxes[0]['email'], 'temp_email': temp_email,
                    'sitepro_password': password,
                })
        if not new_senders:
            return None, "Akun dibuat tapi tidak ada sender tersimpan"
        return new_senders, None

    # ── Kirim: 1 nomor = 1 sender berbeda (cooldown 1 jam) ────────────────
    nums_status = []
    used_sends = []  # {nomor, mailbox_id, row_id, ws_session, account_id}
    remaining = list(numbers)

    # Reservasi sender secara ATOMIK (lock + langsung set cooldown), supaya
    # /fix paralel tidak menarik sender yang sama. Filter partisi server 1/2.
    sender_queue = _reserve_available_senders(
        db_cur, db_conn, limit=len(remaining), server=server
    ) if (db_cur and db_conn) else []
    out['account_reused'] = len(sender_queue) > 0
    if sender_queue:
        print(f"[SITEPRO] Pakai {len(sender_queue)} sender dari pool (reuse, cooldown ok)")

    first_ready = False
    abort = False

    def _take_sender():
        nonlocal abort
        if sender_queue:
            return sender_queue.pop(0)
        extra = _reserve_available_senders(
            db_cur, db_conn, limit=1, server=server,
        ) if (db_cur and db_conn) else []
        if extra:
            return extra[0]
        new_senders, err = _register_new_account()
        if new_senders:
            sender_queue.extend(new_senders)
            return sender_queue.pop(0)
        return None, err

    while remaining and not abort:
        nomor = remaining.pop(0)
        sent_ok = False
        last_err = ""
        ws_session = None
        used_sender = None

        for attempt in range(1, MAX_SENDER_RETRIES_PER_NOMOR + 1):
            picked = _take_sender()
            if isinstance(picked, tuple):
                sender, err = None, picked[1]
            else:
                sender, err = picked, None
            if not sender:
                last_err = err or "Tidak ada sender"
                if not first_ready:
                    out['error'] = last_err
                    step(False, f"siapkan sender: {last_err}")
                break

            if not first_ready:
                first_ready = True
                out['account_ready'] = True
                step(True, "pakai sender pool" if out['account_reused'] else "siapkan sender baru")
                _cb('account_ready', {'reused': out['account_reused']})

            print(
                f"[SITEPRO] Kirim +{nomor} via {sender.get('email')} "
                f"(percobaan {attempt}/{MAX_SENDER_RETRIES_PER_NOMOR})"
            )

            sess = _session_for(sender)
            if not sess:
                last_err = "Session sender tidak valid"
                _release_sender_cooldown(db_cur, db_conn, sender.get('row_id'))
                print(f"[SITEPRO]   ✗ {last_err}")
                continue

            ws_session, msg = sitepro_open_webmail(sess, sender['mailbox_id'])
            if not ws_session:
                last_err = f"login kotak masuk gagal: {msg}"
                _release_sender_cooldown(db_cur, db_conn, sender.get('row_id'))
                print(f"[SITEPRO]   ✗ {last_err}")
                continue

            # Baseline UID SEBELUM kirim — per folder (INBOX/Junk/Spam).
            # Kalau diambil setelah kirim, balasan WA yang sudah masuk ikut ter-skip.
            _pre_uid_map = {}
            try:
                pre_msgs, _e = roundcube_read_inbox_folders(
                    ws_session, timeout=20, fetch_body=False, body_limit=0,
                )
                for m in (pre_msgs or []):
                    folder = (m.get('mbox') or 'INBOX')
                    try:
                        uid_i = int(m.get('uid', 0))
                    except Exception:
                        continue
                    if uid_i > _pre_uid_map.get(folder, 0):
                        _pre_uid_map[folder] = uid_i
            except Exception:
                _pre_uid_map = {}

            token, from_id, compose_id = roundcube_get_compose_token(ws_session)
            if not token:
                last_err = "Gagal ambil token kirim"
                _release_sender_cooldown(db_cur, db_conn, sender.get('row_id'))
                print(f"[SITEPRO]   ✗ {last_err}")
                continue

            ok, smsg = roundcube_send_email(
                ws_session, token, from_id, compose_id,
                "support@support.whatsapp.com", _wa_message(nomor),
            )
            if ok:
                sent_ok = True
                used_sender = sender
                last_err = smsg or ""
                sender['_pre_uid_map'] = dict(_pre_uid_map)
                print(f"[SITEPRO]   ✓ Terkirim +{nomor}")
                break

            last_err = smsg or "Kirim email gagal"
            # Rate limit SMTP per mailbox: sender-nya SEHAT, cuma sedang
            # dijatah. Jangan dilepas ke pool (nanti terpilih lagi di
            # percobaan berikutnya dan gagal dengan alasan yang sama) —
            # biarkan reservasi cooldown-nya berjalan dan ambil sender lain.
            if _is_smtp_rate_limit(last_err):
                print(f"[SITEPRO]   ✗ {last_err} (rate limit — sender ditahan, ganti sender)")
            else:
                _release_sender_cooldown(db_cur, db_conn, sender.get('row_id'))
                print(f"[SITEPRO]   ✗ {last_err}")
            ws_session = None

        nums_status.append({'nomor': nomor, 'sent': sent_ok,
                            'sent_msg': last_err, 'replied': False})
        if sent_ok and used_sender and ws_session:
            out['total_sent'] += 1
            _mark_sender_composed(db_cur, db_conn, used_sender.get('row_id'), used_for=nomor)
            used_sends.append({
                'nomor': nomor, 'mailbox_id': used_sender['mailbox_id'],
                'row_id': used_sender.get('row_id'), 'ws_session': ws_session,
                'account_id': used_sender['account_id'],
                'pre_uid_map': dict(used_sender.get('_pre_uid_map') or {}),
            })
            _cb('sent', {'nomor': nomor, 'ok': True})
            time.sleep(1)
            continue

        _cb('sent', {'nomor': nomor, 'ok': False})
        if not first_ready and last_err:
            for n in remaining:
                nums_status.append({'nomor': n, 'sent': False,
                                    'sent_msg': last_err, 'replied': False})
            remaining = []
            abort = True

    out['numbers'] = nums_status

    # ── Polling balasan dari tiap mailbox sender ─────────────────────────
    # Pakai baseline per-folder yang diambil SEBELUM kirim.
    deadline = time.time() + reply_wait
    replies_seen = set()
    matched_mailboxes = set()

    while used_sends and time.time() < deadline:
        for us in used_sends:
            if us['mailbox_id'] in matched_mailboxes:
                continue
            pre_map = us.get('pre_uid_map') or {}
            # min_uid=0 lalu filter per-folder sendiri (UID folder tidak sebanding)
            msgs, _err = roundcube_read_inbox_folders(
                us['ws_session'], timeout=45,
                body_limit=5, min_uid=0,
            )
            if not msgs:
                continue
            msgs_sorted = sorted(msgs, key=lambda x: int(x.get('uid', 0)))
            for m in msgs_sorted:
                try:
                    uid_int = int(m.get('uid', 0))
                except Exception:
                    continue
                folder = m.get('mbox') or 'INBOX'
                base = int(pre_map.get(folder, 0) or 0)
                if uid_int <= base:
                    continue
                key = f"{us['mailbox_id']}:{folder}:{uid_int}"
                if key in replies_seen:
                    continue
                if not is_whatsapp_support_reply(m):
                    continue
                replies_seen.add(key)
                reply_obj = {
                    'uid': str(uid_int),
                    'subject': m.get('subject', ''),
                    'from': m.get('from', ''),
                    'body': m.get('body', ''),
                    'matched_nomor': us['nomor'],
                }
                out['replies'].append(reply_obj)
                out['newest_reply'] = reply_obj
                for ns in nums_status:
                    if ns['nomor'] == us['nomor'] and not ns['replied']:
                        ns['replied'] = True
                        break
                _cb('reply', {'uid': str(uid_int), 'matched': us['nomor']})
                matched_mailboxes.add(us['mailbox_id'])
                break
        if len(matched_mailboxes) >= len(used_sends):
            break
        time.sleep(5)

    out['total_replied'] = sum(1 for ns in nums_status if ns['replied'])

    # sends_left = jumlah sender di SELURUH pool yang siap pakai sekarang
    # (belum pernah dipakai atau sudah lewat cooldown 1 jam).
    out['sends_left'] = 0
    try:
        if db_cur:
            db_cur.execute(
                f"""SELECT COUNT(*) FROM sitepro_mailboxes
                    WHERE mailbox_id IS NOT NULL AND mailbox_id > 0
                      AND (last_used_at IS NULL
                           OR last_used_at <= datetime('now', '-{int(SENDER_COOLDOWN_SECS)} seconds'))"""
            )
            out['sends_left'] = db_cur.fetchone()[0]
    except Exception:
        pass

    out['success'] = out['total_sent'] > 0
    return out
