"""
fb_module.py - Facebook Account Lookup Module for dik.py
=========================================================
Lookup Facebook accounts by phone number using limited.facebook.com
Uses curl_cffi for Chrome TLS impersonation (anti rate limit)

Data yang bisa diambil:
- Nama akun (semua akun di 1 nomor)
- Status (active/deactivated/disabled)
- Recovery options (WhatsApp/SMS/Email)
- Profile pic (custom/default)
- Masked email/phone
"""
import re
import time
import random
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from curl_cffi import requests as cffi_requests
    CFFI_OK = True
except ImportError:
    CFFI_OK = False

log = logging.getLogger(__name__)

# ============================================================
# Config
# ============================================================
FB_WORKERS = 40
FB_DELAY_MIN = 3
FB_DELAY_MAX = 6
FB_MAX_RETRIES = 3
FB_BASE = "https://limited.facebook.com"
PROXYSCRAPE_URL = "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all"

# Proxy pool
_proxy_pool = []
_proxy_loaded = False
_proxy_lock = threading.Lock()

def _load_proxies():
    """Load free HTTP proxies from proxyscrape."""
    global _proxy_pool, _proxy_loaded
    if _proxy_loaded:
        return
    with _proxy_lock:
        if _proxy_loaded:
            return
        try:
            import requests as _req
            r = _req.get(PROXYSCRAPE_URL, timeout=10, verify=False)
            for line in r.text.strip().split('\n'):
                line = line.strip()
                if line and ':' in line:
                    _proxy_pool.append(f"http://{line}" if not line.startswith('http') else line)
            log.info(f"[FB] Loaded {len(_proxy_pool)} proxies")
        except Exception as e:
            log.warning(f"[FB] Proxy load failed: {e}")
        _proxy_loaded = True

def _get_proxy():
    """Get random proxy from pool."""
    if not _proxy_pool:
        _load_proxies()
    return random.choice(_proxy_pool) if _proxy_pool else None

DEVICES = [
    ("SM-G998B", "12", "131.0.6778.200"),
    ("SM-S918B", "14", "137.0.7151.100"),
    ("Pixel 8 Pro", "15", "149.0.7827.91"),
    ("25062RN2DY", "15", "149.0.7827.91"),
    ("M2102K1G", "13", "133.0.6905.60"),
    ("SM-A546B", "14", "136.0.7103.125"),
    ("22101316G", "14", "138.0.7204.50"),
    ("CPH2449", "13", "135.0.7049.38"),
    ("V2254A", "14", "140.0.7272.100"),
    ("RMX3630", "14", "139.0.7240.80"),
]
BUILDS = ["AQ3A.250226.002", "UP1A.231005.007", "TQ3A.230901.001",
           "TP1A.220624.014", "SP1A.210812.016"]
IMPERSONATE = ["chrome120", "chrome124"]

DEFAULT_PIC_ID = "84628273_176159830277856"

def _random_ua():
    dev, android, chrome = random.choice(DEVICES)
    build = random.choice(BUILDS)
    return (f"Mozilla/5.0 (Linux; Android {android}; {dev} Build/{build}) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome} Mobile Safari/537.36")


# ============================================================
# Data models
# ============================================================
@dataclass
class FBAccount:
    name: str = ""
    status: str = "active"
    profile_pic: Optional[str] = None
    masked_phone: Optional[str] = None
    masked_email: Optional[str] = None
    recovery: Optional[str] = None
    has_custom_pic: bool = False
    uid: Optional[str] = None  # FB cuid_ (encrypted UID)
    network_info: Optional[str] = None
    can_show_name: bool = False  # FB privacy flag
    is_honeypot_likely: bool = False  # True if name is just masked phone

@dataclass
class FBResult:
    phone: str
    found: bool = False
    status: str = "unknown"
    accounts: List = field(default_factory=list)
    error: Optional[str] = None
    time_taken: float = 0.0


def normalize_phone(phone: str) -> str:
    phone = phone.strip().replace("+", "").replace("-", "").replace(" ", "")
    if phone.startswith("0"):
        phone = "62" + phone[1:]
    return phone


# ============================================================
# Token extraction
# ============================================================
def _extract_token(html, name):
    m = re.search(rf'name="{name}"[^>]*value="([^"]*)"', html)
    if m: return m.group(1)
    m = re.search(rf'value="([^"]*)"[^>]*name="{name}"', html)
    if m: return m.group(1)
    m = re.search(rf'"{name.upper()}"[^{{]*\{{\s*"token"\s*:\s*"([^"]+)"', html)
    if m: return m.group(1)
    m = re.search(rf'"{name}"\s*:\s*"([^"]+)"', html)
    if m: return m.group(1)
    return ""

def _extract_all_hidden(html):
    form_data = {}
    for m in re.finditer(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', html):
        form_data[m.group(1)] = m.group(2)
    for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"[^>]*type="hidden"', html):
        if m.group(1) not in form_data:
            form_data[m.group(1)] = m.group(2)
    return form_data

def _extract_form_action(html):
    m = re.search(r'<form[^>]*action="([^"]*identify[^"]*)"', html)
    if m:
        return m.group(1).replace("&amp;", "&")
    return None


# ============================================================
# Parse accounts from response
# ============================================================
SKIP_WORDS = {'facebook', 'terlupa', 'kata laluan', 'log masuk', 'batal',
              'cuba lagi', 'sedang memuatkan', 'di sini', 'ralat',
              'sorry, something went wrong', 'pilih akun anda',
              'cari akun anda', 'masukkan nomor ponsel anda',
              'lanjutkan', 'cari berdasarkan email', 'buat akun baru',
              'memuat', 'memuat...', 'nomor ponsel', 'akaun dinyahdayakan',
              'lupa kata sandi', 'tidak dapat masuk', 'teruskan', 'hantar',
              'kembali', 'back', 'continue', 'coba lagi', '-->', 'atau',
              'lupa kata laluan', 'tidak boleh log masuk',
              'permintaan anda tidak dapat diproses', 'sertai',
              'akun dinonaktifkan', 'akaun dinyahdayakan',
              'sertai facebook atau log masuk untuk meneruskan'}


def _parse_accounts(html, result):
    """Parse all accounts from FB response HTML."""
    phone_masks = []

    # Find "Pilih akun" section
    pilih_idx = html.find('Pilih akun Anda')
    if pilih_idx < 0:
        pilih_idx = html.find('Pilih akun anda')
    if pilih_idx < 0:
        pilih_idx = html.lower().find('pilih akun')

    if pilih_idx >= 0:
        section = html[pilih_idx:pilih_idx+30000]
        # aria-label (most reliable)
        for label in re.findall(r'aria-label="([^"]{2,50})"', section):
            if re.match(r'^\+?\d[\d*\s-]+$', label):
                phone_masks.append(label); continue
            if label.lower() in SKIP_WORDS: continue
            if not any(a.name == label for a in result.accounts):
                result.accounts.append(FBAccount(name=label))
        # text between tags
        for t in re.findall(r'>([^<]{2,60})<', section):
            t = t.strip()
            if not t or t.lower() in SKIP_WORDS: continue
            if t.startswith(('{', '//', '1&&', 'http')): continue
            if re.match(r'^\+?\d[\d*\s-]+$', t):
                if t not in phone_masks: phone_masks.append(t)
                continue
            if re.match(r'^[\d.]+$', t): continue
            if 2 < len(t) < 50 and not re.match(r'^[\d.+*\s-]+$', t):
                if not any(a.name == t for a in result.accounts):
                    result.accounts.append(FBAccount(name=t))

    # Fallback: no accounts from "Pilih akun" - single account redirect
    if not result.accounts:
        # Try to extract name from recovery page (sometimes in aria-label or heading)
        name_candidates = re.findall(r'aria-label="([^"]{2,40})"', html[:5000])
        name_candidates = [n for n in name_candidates if n.lower() not in SKIP_WORDS
                          and not re.match(r'^[\d.+*\s-]+$', n)]
        
        # Check if page is SPECIFICALLY a deactivation notice (heading level)
        is_deactivated = bool(re.search(
            r'<(h[1-3]|p)[^>]*>[^<]*(Dinyahdayakan|dinonaktifkan|deactivated|dibekukan)[^<]*</',
            html[:10000], re.I
        ))
        
        if name_candidates:
            acct = FBAccount(name=name_candidates[0], status="deactivated" if is_deactivated else "active")
            result.accounts.append(acct)
        elif is_deactivated:
            result.accounts.append(FBAccount(name="[Dinonaktifkan]", status="deactivated"))
        else:
            # Account exists (we got redirected) but can't get name
            result.accounts.append(FBAccount(name="[Terdaftar]", status="active"))

    # Recovery options
    recovery_m = re.search(r'recover_method[^>]*value="([^"]+)"', html)
    if recovery_m:
        rv = recovery_m.group(1)
        for a in result.accounts:
            if 'whatsapp' in rv.lower():
                a.recovery = "WhatsApp"
            elif 'sms' in rv.lower():
                a.recovery = "SMS"
            elif 'email' in rv.lower():
                a.recovery = "Email"

    # Check for WhatsApp/SMS mentions in page text
    if not recovery_m:
        if 'Kirim ke WhatsApp' in html or 'send_whatsapp' in html:
            for a in result.accounts:
                if not a.recovery: a.recovery = "WhatsApp"
        elif 'Kirim SMS' in html or 'send_sms' in html:
            for a in result.accounts:
                if not a.recovery: a.recovery = "SMS"

    # Profile pics
    pics = re.findall(r'src="(https://[^"]*scontent[^"]*)"', html)
    pics = [p for p in pics if DEFAULT_PIC_ID not in p and 'pixel' not in p and len(p) > 60]
    for i, pic in enumerate(pics):
        if i < len(result.accounts):
            result.accounts[i].profile_pic = pic
            result.accounts[i].has_custom_pic = True

    # Masked contacts
    if phone_masks:
        for a in result.accounts:
            if not a.masked_phone: a.masked_phone = phone_masks[0]
    else:
        mp = re.findall(r'(\+?\d{1,3}\*+\d{1,6})', html)
        if mp:
            for a in result.accounts:
                if not a.masked_phone: a.masked_phone = mp[0]

    me = [m for m in re.findall(r'[\w*]+@[\w.]+', html) if '*' in m]
    if me:
        for a in result.accounts:
            if not a.masked_email: a.masked_email = me[0]


# ============================================================
# Main lookup function
# ============================================================
def lookup_facebook(phone_number, retry=0):
    """Lookup FB account by phone. Returns FBResult."""
    if not CFFI_OK:
        return FBResult(phone=phone_number, error="curl_cffi not installed")

    start = time.time()
    phone = normalize_phone(phone_number)
    result = FBResult(phone=phone)

    ua = _random_ua()
    imp = random.choice(IMPERSONATE)
    session = cffi_requests.Session(impersonate=imp)
    session.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Ch-Ua-Mobile": "?1",
        "Upgrade-Insecure-Requests": "1",
    })

    # Get proxy for this request (different proxy each retry)
    proxy = _get_proxy()
    px = {"http": proxy, "https": proxy} if proxy else None

    # Step 1: GET identify
    try:
        r1 = session.get(f"{FB_BASE}/login/identify/",
                         params={"ctx": "recover", "c": "/login/",
                                 "multiple_results": "1", "ars": "facebook_login",
                                 "from_login_screen": "0", "lwv": "100"},
                         timeout=12, allow_redirects=True,
                         proxies=px)
    except Exception as e:
        # Proxy failed - retry with different proxy
        if retry < 5:
            return lookup_facebook(phone_number, retry + 1)
        result.error = f"Network: {e}"; result.status = "error"
        result.time_taken = time.time() - start
        return result

    if r1.status_code != 200:
        if retry < FB_MAX_RETRIES:
            time.sleep(FB_DELAY_MIN + random.uniform(1, 3))
            return lookup_facebook(phone_number, retry + 1)
        result.error = f"HTTP {r1.status_code}"; result.status = "error"
        result.time_taken = time.time() - start
        return result

    # Extract tokens
    lsd = _extract_token(r1.text, "lsd")
    jazoest = _extract_token(r1.text, "jazoest")
    form_data = _extract_all_hidden(r1.text)
    if lsd and "lsd" not in form_data: form_data["lsd"] = lsd
    if jazoest and "jazoest" not in form_data: form_data["jazoest"] = jazoest
    form_data["email"] = phone
    # Get submit button value dynamically
    btn = re.search(r'name="did_submit"[^>]*value="([^"]*)"', r1.text)
    form_data["did_submit"] = btn.group(1) if btn else "Cari"

    action = _extract_form_action(r1.text)
    post_url = f"{FB_BASE}{action}" if action and action.startswith("/") else f"{FB_BASE}/login/identify/"
    if action and action.startswith("http"): post_url = action

    # Step 2: POST phone (don't follow redirect to detect 302 = found)
    time.sleep(random.uniform(1.0, 2.0))
    try:
        r2 = session.post(post_url, data=form_data, timeout=15,
                          allow_redirects=False, proxies=px)
    except Exception as e:
        if retry < 5:
            return lookup_facebook(phone_number, retry + 1)
        result.error = f"POST: {e}"; result.status = "error"
        result.time_taken = time.time() - start
        return result

    # 302 redirect = account found
    if r2.status_code in (301, 302, 303, 307):
        result.found = True
        result.status = "found"
        loc = r2.headers.get('Location', '')
        # Try to follow redirect for names
        try:
            redir_url = f"{FB_BASE}{loc}" if loc.startswith('/') else loc
            r3 = session.get(redir_url, timeout=12, allow_redirects=True, proxies=px)
            if r3.status_code == 200:
                _parse_accounts(r3.text, result)
        except:
            pass
        # If no names from redirect, mark as found with generic name
        if not result.accounts:
            result.accounts.append(FBAccount(name="[Terdaftar]", status="active"))
        result.time_taken = time.time() - start
        return result

    # Follow redirect for 200 responses
    if r2.status_code == 200:
        html = r2.text
    elif r2.status_code == 500:
        if retry < FB_MAX_RETRIES:
            time.sleep(FB_DELAY_MIN + random.uniform(2, 4))
            return lookup_facebook(phone_number, retry + 1)
        result.error = "Server 500"; result.status = "error"
        result.time_taken = time.time() - start
        return result
    else:
        result.error = f"HTTP {r2.status_code}"; result.status = "error"
        result.time_taken = time.time() - start
        return result

    # Step 3: Parse accounts
    
    # Check if page is still the search form = NOT FOUND
    is_search_form = bool(
        re.search(r'Masukkan nomor ponsel|Cari berdasarkan|identify_search_description', html) and
        'Pilih akun' not in html
    )
    
    # Only count as redirect if URL path changed (not just query params)
    url_path = r2.url.split('?')[0]
    redirected = any(x in url_path for x in ["device-based/ar/login", "/recover/", "/reset/"])
    has_accounts = "account_list" in html or "Pilih akun" in html
    
    if is_search_form and not has_accounts and not redirected:
        # Still on search page = number not found
        result.status = "not_found"
    elif redirected or has_accounts:
        result.found = True
        result.status = "found"
        _parse_accounts(html, result)
    else:
        # Check page content for account indicators
        _parse_accounts(html, result)
        if result.accounts:
            result.found = True
            result.status = "found"
        else:
            result.status = "not_found"


    result.time_taken = time.time() - start
    return result


# ============================================================
# Bulk lookup with workers
# ============================================================
def bulk_lookup(phones, max_workers=FB_WORKERS, progress_cb=None):
    """Bulk lookup phones. Returns list of FBResult. progress_cb(done, total)."""
    results = [None] * len(phones)
    done_count = [0]
    lock = threading.Lock()

    def worker(idx, phone):
        time.sleep(random.uniform(0.2, 1.0) * (idx % max_workers))  # stagger start
        r = lookup_facebook(phone)
        results[idx] = r
        with lock:
            done_count[0] += 1
            if progress_cb:
                try:
                    progress_cb(done_count[0], len(phones))
                except:
                    pass

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = []
        for i, phone in enumerate(phones):
            futures.append(pool.submit(worker, i, phone))
        for f in as_completed(futures):
            try:
                f.result()
            except:
                pass

    # Fill any None results
    for i in range(len(results)):
        if results[i] is None:
            results[i] = FBResult(phone=phones[i], error="Worker failed", status="error")

    return results


# ============================================================
# Server 2: b-graph.facebook.com/recover_accounts (FB App API)
# ============================================================
import uuid
import hashlib

FB_API_KEY = "882a8490361da98702bf97a021ddc14d"
FB_APP_TOKEN = "350685531728|62f8ce9f74b12f84c123cc23437a4a32"
FB_APP_SECRET = "62f8ce9f74b12f84c123cc23437a4a32"
FB_GRAPH_URL = "https://b-graph.facebook.com/recover_accounts"

S2_HNI_LIST = ['51001', '51010', '51011', '51089', '51028', '51009']
S2_CARRIERS = ['Indosat Ooredoo', 'Telkomsel', 'XL Axiata', 'Smartfren', '3 (Tri)', 'AXIS']

S2_DEVICES = [
    # Vertu
    {"model": "VERTU-iVERTU-5G",       "brand": "VERTU", "mf": "Vertu", "ver": ["12","13"], "d": "2.75", "w": "1080", "h": "2400"},
    {"model": "VERTU-METAVERTU",        "brand": "VERTU", "mf": "Vertu", "ver": ["12","13"], "d": "2.75", "w": "1080", "h": "2400"},
    {"model": "VERTU-CONSTELLATION-X",  "brand": "VERTU", "mf": "Vertu", "ver": ["11","12"], "d": "2.75", "w": "1080", "h": "2340"},
    # Infinix
    {"model": "Infinix-X6835",  "brand": "Infinix", "mf": "Infinix", "ver": ["13","14"], "d": "2.0",  "w": "720",  "h": "1612"},
    {"model": "Infinix-X6711",  "brand": "Infinix", "mf": "Infinix", "ver": ["13","14","15"], "d": "2.75", "w": "1080", "h": "2400"},
    {"model": "Infinix-X6739",  "brand": "Infinix", "mf": "Infinix", "ver": ["14","15"], "d": "2.75", "w": "1080", "h": "2400"},
    # Redmi
    {"model": "25062RN2DY",  "brand": "Redmi", "mf": "Xiaomi", "ver": ["15"],      "d": "2.8125", "w": "1080", "h": "2340"},
    {"model": "23053RN02A",  "brand": "Redmi", "mf": "Xiaomi", "ver": ["13","14"], "d": "2.75",   "w": "1080", "h": "2400"},
    {"model": "M2101K6G",    "brand": "Redmi", "mf": "Xiaomi", "ver": ["11","12"], "d": "2.75",   "w": "1080", "h": "2340"},
    {"model": "23028RNCAG",  "brand": "Redmi", "mf": "Xiaomi", "ver": ["13","14"], "d": "2.75",   "w": "1080", "h": "2400"},
    {"model": "23106RN0DA",  "brand": "Redmi", "mf": "Xiaomi", "ver": ["14","15"], "d": "2.75",   "w": "1080", "h": "2400"},
]

S2_BUILDS = {
    "11": ["RP1A.200720.011", "RKQ1.210503.001"],
    "12": ["SP1A.210812.016", "SKQ1.211103.001"],
    "13": ["TP1A.220624.014", "TKQ1.220829.002"],
    "14": ["UP1A.231005.007", "UQ1A.240205.004"],
    "15": ["AQ3A.250226.002", "BP1A.250105.020"],
}


def _s2_compute_sig(params: dict, secret: str) -> str:
    sorted_keys = sorted(k for k in params.keys() if k != "sig")
    raw = "".join(f"{k}={params[k]}" for k in sorted_keys)
    return hashlib.md5((raw + secret).encode()).hexdigest()


def _fb_app_ua():
    """Generate FB4A UA with random Vertu/Infinix/Redmi device."""
    dev = random.choice(S2_DEVICES)
    ver = random.choice(dev["ver"])
    builds = S2_BUILDS.get(ver, ["AQ3A.250226.002"])
    build = random.choice(builds)
    bv = random.randint(690000000, 720000000)
    ci = random.randint(0, len(S2_CARRIERS) - 1)
    carrier = S2_CARRIERS[ci]
    return (
        f'Dalvik/2.1.0 (Linux; U; Android {ver}; {dev["model"]} Build/{build}) '
        f'[FBAN/FB4A;FBAV/500.0.0.57.50;FBPN/com.facebook.katana;FBLC/id_ID;'
        f'FBBV/{bv};FBCR/{carrier};FBMF/{dev["mf"]};FBBD/{dev["brand"]};'
        f'FBDV/{dev["model"]};FBSV/{ver};FBCA/arm64-v8a:null;'
        f'FBDM={{density={dev["d"]},width={dev["w"]},height={dev["h"]}}};'
        f'FB_FW/1;FBRV/0;]'
    )


def _extract_uid_from_pic(pic_url: str) -> str:
    """Try to extract numeric Facebook UID from custom profile pic URL.
    FB CDN filename format: {photo_id}_{user_id}_{hash}_n.jpg
    User IDs are 15-17 digit numbers typically starting with 100.
    """
    if not pic_url or DEFAULT_PIC_ID in pic_url:
        return ""
    fname_m = re.search(r'/([^/?]+\.jpg)', pic_url, re.I)
    if not fname_m:
        return ""
    fname = fname_m.group(1)
    parts = fname.replace('_n.jpg', '').split('_')
    for p in parts:
        if p.isdigit() and 14 <= len(p) <= 17 and p.startswith('100'):
            return p
    for p in parts:
        if p.isdigit() and 10 <= len(p) <= 17 and not p.startswith('846'):
            return p
    return ""


def lookup_facebook_s2(phone_number):
    """Server 2: Direct FB Graph API lookup with sig. Returns UID."""
    if not CFFI_OK:
        return FBResult(phone=phone_number, error="curl_cffi not installed")

    start = time.time()
    phone = normalize_phone(phone_number)
    result = FBResult(phone=phone)

    device_id = str(uuid.uuid4())
    family_id = str(uuid.uuid4())
    machine_id = uuid.uuid4().hex[:22]
    ci = random.randint(0, len(S2_HNI_LIST) - 1)
    hni = S2_HNI_LIST[ci]

    s = cffi_requests.Session(impersonate=random.choice(IMPERSONATE))

    data = {
        "q": phone,
        "summary": "true",
        "device_id": device_id,
        "src": "fb4a_account_recovery",
        "machine_id": machine_id,
        "sfdid": str(uuid.uuid4()),
        "fdid": device_id,
        "sim_serials": "[]",
        "msgr_sso_uids": "[]",
        "sms_retriever": "true",
        "cds_experiment_group": "-1",
        "shared_phone_test_group": "",
        "shared_phone_number": "",
        "is_auto_search": "false",
        "encrypted_msisdn": "",
        "locale": "id_ID",
        "client_country_code": "ID",
        "method": "GET",
        "fb_api_req_friendly_name": "accountRecoverySearch",
        "fb_api_caller_class": "AccountSearchHelper",
        "api_key": FB_API_KEY,
        "access_token": FB_APP_TOKEN,
    }
    data["sig"] = _s2_compute_sig(data, FB_APP_SECRET)

    sess_nid = uuid.uuid4().hex[:12].upper()
    headers = {
        "User-Agent": _fb_app_ua(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": "OAuth null",
        "X-FB-Friendly-Name": "accountRecoverySearch",
        "X-FB-Connection-Quality": "EXCELLENT",
        "X-FB-Connection-Type": "MOBILE.LTE",
        "X-FB-Net-HNI": hni,
        "X-FB-SIM-HNI": hni,
        "X-FB-HTTP-Engine": "Tigon/Liger",
        "X-FB-Client-IP": "True",
        "X-FB-Server-Cluster": "True",
        "X-FB-Device-Group": str(random.randint(70, 100)),
        "X-FB-Session-Id": f"nid={sess_nid};tid={random.randint(1000,9999)};nc=0;fc=0;bc=0",
        "X-Tigon-Is-Retry": "False",
        "Accept-Encoding": "gzip, deflate",
    }

    try:
        r = s.post(FB_GRAPH_URL, data=data, headers=headers, timeout=12)
    except Exception as e:
        result.error = f"Network: {e}"; result.status = "error"
        result.time_taken = time.time() - start
        return result

    # Retry once on HTTP 500 (FB anti-spam can hit valid lookups)
    if r.status_code == 500:
        time.sleep(random.uniform(1.5, 3))
        try:
            r = s.post(FB_GRAPH_URL, data=data, headers=headers, timeout=12)
        except Exception:
            pass

    if r.status_code != 200:
        result.error = f"HTTP {r.status_code}"; result.status = "error"
        result.time_taken = time.time() - start
        return result

    try:
        resp = r.json()
    except:
        result.error = "Invalid JSON"; result.status = "error"
        result.time_taken = time.time() - start
        return result

    accounts = resp.get("data", [])
    summary = resp.get("summary", {})
    total_count = summary.get("total_count", 0)

    if not accounts:
        # total_count > 0 but data empty = account exists but FB hides it
        if total_count > 0:
            result.found = True
            result.status = "uncertain"  # not 100% confirmed
            acct = FBAccount(name="[Tersembunyi]", status="hidden")
            acct.is_honeypot_likely = True
            result.accounts.append(acct)
        else:
            result.status = "not_found"
        result.time_taken = time.time() - start
        return result

    # Process all accounts
    confirmed_real = False
    for acct_data in accounts:
        raw_name = acct_data.get("name", "Unknown")
        first_name = acct_data.get("first_name", "")
        can_show = bool(acct_data.get("can_show_profile_pic_and_name", False))
        network_info = acct_data.get("network_info", "")
        cuid = acct_data.get("id", "")

        # Detect if "name" is just masked phone (e.g. "+628*******013")
        is_masked_phone = bool(re.match(r'^\+?\d[\d*\s-]+$', raw_name))

        # Real name candidates: prefer first_name if name is masked
        if first_name:
            display_name = first_name
        elif is_masked_phone:
            display_name = "[Tersembunyi]"  # honeypot or privacy-locked
        else:
            display_name = raw_name

        acct = FBAccount(name=display_name)
        acct.uid = cuid
        acct.can_show_name = can_show
        acct.network_info = network_info
        acct.is_honeypot_likely = (is_masked_phone and not first_name and not can_show)

        if can_show or first_name:
            acct.status = "confirmed"
            confirmed_real = True
        elif is_masked_phone:
            acct.status = "uncertain"
        else:
            acct.status = "active"
            confirmed_real = True

        cps = acct_data.get("contactpoints", {}).get("data", [])
        for cp in cps:
            cp_type = cp.get("type", "")
            cp_display = cp.get("display", "")
            if cp_type == "PHONE" and not acct.masked_phone:
                acct.masked_phone = cp_display
            elif cp_type == "EMAIL" and not acct.masked_email:
                acct.masked_email = cp_display

        pic_url = acct_data.get("profile_pic_uri", "")
        if pic_url and DEFAULT_PIC_ID not in pic_url:
            acct.has_custom_pic = True
            acct.profile_pic = pic_url
            uid_from_pic = _extract_uid_from_pic(pic_url)
            if uid_from_pic:
                acct.uid = uid_from_pic

        methods = []
        if acct_data.get("wa_first"):
            methods.append("WhatsApp")
        for cp in cps:
            if cp.get("type") == "PHONE" and "SMS" not in methods:
                methods.append("SMS")
            elif cp.get("type") == "EMAIL" and "Email" not in methods:
                methods.append("Email")
        acct.recovery = " · ".join(methods) if methods else None

        result.accounts.append(acct)

    # Overall result status
    result.found = True
    if confirmed_real:
        result.status = "found"  # real registered account
    else:
        result.status = "uncertain"  # only honeypot/masked results

    result.time_taken = time.time() - start
    return result




def bulk_lookup_s2(phones, max_workers=FB_WORKERS, progress_cb=None):
    """Bulk Server 2 lookup. Fast parallel."""
    results = [None] * len(phones)
    done_count = [0]
    lock = threading.Lock()

    def worker(idx, phone):
        time.sleep(random.uniform(0.1, 0.5) * (idx % max_workers))
        r = lookup_facebook_s2(phone)
        results[idx] = r
        with lock:
            done_count[0] += 1
            if progress_cb:
                try: progress_cb(done_count[0], len(phones))
                except: pass

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(worker, i, phone) for i, phone in enumerate(phones)]
        for f in as_completed(futures):
            try: f.result()
            except: pass

    for i in range(len(results)):
        if results[i] is None:
            results[i] = FBResult(phone=phones[i], error="Worker failed", status="error")

    return results
