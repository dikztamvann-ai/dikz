"""
Facebook Server 3 — SMS Recovery Module
Flow aligned with fb/full.har:
  recover_accounts → /{cuid}/recovery_codes → validateCode → auth/login
"""
import json, uuid, hashlib, time, logging, random, threading
from dataclasses import dataclass, field
from typing import Optional, List

log = logging.getLogger(__name__)

# ═══ API KEYS ═══
# RECOVERY: Only FB4A has permission for /recover_accounts + /recovery_codes
RECOVERY_API = {"api_key": "882a8490361da98702bf97a021ddc14d", "access_token": "350685531728|62f8ce9f74b12f84c123cc23437a4a32", "app_secret": "62f8ce9f74b12f84c123cc23437a4a32", "caller": "Fb4aAuthHandler"}

# LOGIN: Multiple methods to rotate (avoid rate limit on login step)
LOGIN_APIS = [
    {"api_key": "882a8490361da98702bf97a021ddc14d", "access_token": "350685531728|62f8ce9f74b12f84c123cc23437a4a32", "app_secret": "62f8ce9f74b12f84c123cc23437a4a32", "caller": "Fb4aAuthHandler"},
    {"api_key": "256002347743983", "access_token": "256002347743983|374e60f8b9bb6b8cbb30f78030438895", "app_secret": "374e60f8b9bb6b8cbb30f78030438895", "caller": "AuthOperations$PasswordAuthOperation"},
    {"api_key": "121876164619130", "access_token": "121876164619130|1ab2c5c902faedd339c14b2d58e929dc", "app_secret": "1ab2c5c902faedd339c14b2d58e929dc", "caller": "AuthOperations$PasswordAuthOperation"},
]

DEFAULT_PASSWORD = "dika2007"

# full.har device fingerprint (Redmi 25062RN2DY — entries 5/7/0)
HAR_FBBV = "696636320"
HAR_FBRV = "698879235"
HAR_HNI = "51001"
HAR_CARRIER = "Indosat Ooredoo"
HAR_DEVICE_GROUP = "1414"
HAR_RECOVERY_UA = (
    "[FBAN/FB4A;FBAV/500.0.0.57.50;FBBV/696636320;"
    "FBDM/{density=2.8125,width=1080,height=2340};"
    "FBLC/id_ID;FBRV/698879235;FBCR/Indosat Ooredoo;"
    "FBMF/Xiaomi;FBBD/Redmi;FBPN/com.facebook.katana;"
    "FBDV/25062RN2DY;FBSV/15;FBOP/1;FBCA/arm64-v8a:;]"
)
HAR_LOGIN_UA = (
    "Dalvik/2.1.0 (Linux; U; Android 15; 25062RN2DY Build/AQ3A.250226.002) "
    "[FBAN/FB4A;FBAV/500.0.0.57.50;FBPN/com.facebook.katana;FBLC/id_ID;"
    "FBBV/696636320;FBCR/Indosat Ooredoo;FBMF/Xiaomi;FBBD/Redmi;"
    "FBDV/25062RN2DY;FBSV/15;FBCA/arm64-v8a:null;"
    "FBDM/{density=2.8125,width=1080,height=2340};FB_FW/1;FBRV/0;]"
)
HAR_ZERO_EH = "664c0faaac849cb891d0a261fbb72a12"
HAR_RMD = (
    "fail=Server:NoUrlMap,Default:INVALID_MAP;v=;ip=;tkn=;"
    "reqTime=1836216166;recvTime=28"
)
SMS_CODE_LENGTHS = (6, 7, 8)

# ═══ CARRIER / HNI DATA ═══
HNI_LIST = ['51001', '51010', '51011', '51089', '51028', '51009']
CARRIER_NAMES = ['Indosat Ooredoo', 'Telkomsel', 'XL Axiata', 'Smartfren', '3 (Tri)', 'AXIS']

# ═══ DEVICE DATABASE — Vertu / Infinix / Redmi (random choice) ═══
DEVICES = [
    # ── Vertu (luxury Android) ──
    {"model": "VERTU-iVERTU-5G",       "brand": "VERTU", "mf": "Vertu", "versions": ["12","13"],     "density": "2.75", "width": "1080", "height": "2400"},
    {"model": "VERTU-METAVERTU",        "brand": "VERTU", "mf": "Vertu", "versions": ["12","13"],     "density": "2.75", "width": "1080", "height": "2400"},
    {"model": "VERTU-METAVERTU-2",      "brand": "VERTU", "mf": "Vertu", "versions": ["13","14"],     "density": "2.75", "width": "1080", "height": "2400"},
    {"model": "VERTU-CONSTELLATION-X",  "brand": "VERTU", "mf": "Vertu", "versions": ["11","12"],     "density": "2.75", "width": "1080", "height": "2340"},
    {"model": "VERTU-ASTER-P",          "brand": "VERTU", "mf": "Vertu", "versions": ["11","12"],     "density": "2.75", "width": "1080", "height": "2160"},
    {"model": "VERTU-iVERTU-Folding",   "brand": "VERTU", "mf": "Vertu", "versions": ["13","14"],     "density": "2.75", "width": "1080", "height": "2400"},
    # ── Infinix Hot/Note/Zero ──
    {"model": "Infinix-X6835",  "brand": "Infinix", "mf": "Infinix", "versions": ["13","14"],       "density": "2.0",  "width": "720",  "height": "1612"},
    {"model": "Infinix-X6837",  "brand": "Infinix", "mf": "Infinix", "versions": ["13","14"],       "density": "2.0",  "width": "720",  "height": "1612"},
    {"model": "Infinix-X6833B", "brand": "Infinix", "mf": "Infinix", "versions": ["12","13","14"],  "density": "2.0",  "width": "720",  "height": "1612"},
    {"model": "Infinix-X6711",  "brand": "Infinix", "mf": "Infinix", "versions": ["13","14","15"],  "density": "2.75", "width": "1080", "height": "2400"},
    {"model": "Infinix-X6739",  "brand": "Infinix", "mf": "Infinix", "versions": ["14","15"],       "density": "2.75", "width": "1080", "height": "2400"},
    {"model": "Infinix-X6852",  "brand": "Infinix", "mf": "Infinix", "versions": ["14","15"],       "density": "2.0",  "width": "720",  "height": "1612"},
    {"model": "Infinix-X6850",  "brand": "Infinix", "mf": "Infinix", "versions": ["14","15"],       "density": "2.75", "width": "1080", "height": "2400"},
    # ── Redmi / Xiaomi ──
    {"model": "23053RN02A",  "brand": "Redmi",   "mf": "Xiaomi", "versions": ["13","14"],     "density": "2.75",   "width": "1080", "height": "2400"},
    {"model": "22041219G",   "brand": "Redmi",   "mf": "Xiaomi", "versions": ["12","13"],     "density": "2.75",   "width": "1080", "height": "2400"},
    {"model": "M2101K6G",    "brand": "Redmi",   "mf": "Xiaomi", "versions": ["11","12"],     "density": "2.75",   "width": "1080", "height": "2340"},
    {"model": "23106RN0DA",  "brand": "Redmi",   "mf": "Xiaomi", "versions": ["14","15"],     "density": "2.75",   "width": "1080", "height": "2400"},
    {"model": "2201116SG",   "brand": "Redmi",   "mf": "Xiaomi", "versions": ["12","13"],     "density": "2.75",   "width": "1080", "height": "2400"},
    {"model": "23028RNCAG",  "brand": "Redmi",   "mf": "Xiaomi", "versions": ["13","14"],     "density": "2.75",   "width": "1080", "height": "2400"},
    {"model": "25062RN2DY",  "brand": "Redmi",   "mf": "Xiaomi", "versions": ["15"],          "density": "2.8125", "width": "1080", "height": "2340"},
]

ANDROID_BUILDS = {
    "11": ["RP1A.200720.011", "RKQ1.210503.001", "RQ3A.211001.001"],
    "12": ["SP1A.210812.016", "SQ1A.220205.002", "SKQ1.211103.001"],
    "13": ["TP1A.220624.014", "TKQ1.220829.002", "TQ3A.230805.001"],
    "14": ["UP1A.231005.007", "UQ1A.240205.004", "AP2A.240605.024"],
    "15": ["AQ3A.250226.002", "AP3A.240905.015", "BP1A.250105.020"],
}

# ═══ GATE PROXY ═══
GATE_HOST = "150.109.18.221"
GATE_PORT = "4950"
GATE_USER_TPL = "gateproxymay30l6b33o-zone-abc-region-{cc}"
GATE_PASS = "GPBOTQ76"
WORKING_COUNTRIES = ["GH", "NG", "TZ", "CI", "BD", "PK", "IN", "PH", "VN"]

def _get_proxy():
    """Get random gate proxy from working country."""
    cc = random.choice(WORKING_COUNTRIES)
    user = GATE_USER_TPL.replace("{cc}", cc)
    return f"http://{user}:{GATE_PASS}@{GATE_HOST}:{GATE_PORT}"


# ═══ HTTP Setup ═══
import requests as http


@dataclass
class S3Account:
    cuid: str = ""
    name: str = ""
    phone_cp_id: str = ""
    phone_display: str = ""
    email_display: str = ""
    can_show: bool = False
    wa_first: bool = False

S3_MAX_RETRIES = 3
S3_RETRY_WAIT = (4, 8)


def _is_rate_limited(err: str) -> bool:
    if not err:
        return False
    e = err.lower().replace(" ", "")
    return '"code":368' in e or "deemedabusive" in e.replace("_", "")


def _is_retryable(err: str) -> bool:
    if not err:
        return False
    e = err.lower()
    return (
        _is_rate_limited(err)
        or "ssl" in e
        or "wrong_version_number" in e
        or "max retries exceeded" in e
        or "connection" in e
    )


@dataclass
class S3SearchResult:
    phone: str = ""
    found: bool = False
    accounts: List[S3Account] = field(default_factory=list)
    total_count: int = 0
    error: str = ""
    rate_limited: bool = False

@dataclass
class S3SmsResult:
    success: bool = False
    code_length: int = 0
    channel: str = ""
    real_sms: bool = False
    auto_conf: str = ""
    error: str = ""
    raw: str = ""

@dataclass
class S3ValidateResult:
    success: bool = False
    uid: str = ""
    contact: str = ""
    error: str = ""
    raw: str = ""

@dataclass
class S3LoginResult:
    result: str = ""
    uid: str = ""
    access_token: str = ""
    session_key: str = ""
    auth_token: str = ""
    error: str = ""


def _new_identity():
    """HAR-stable device per recovery session (full.har Redmi 25062RN2DY)."""
    api = RECOVERY_API
    device_id = str(uuid.uuid4())
    family_id = str(uuid.uuid4())
    secure_family_id = str(uuid.uuid4())
    machine_id = uuid.uuid4().hex[:22]
    return {
        "ua": HAR_RECOVERY_UA,
        "login_ua": HAR_LOGIN_UA,
        "device_id": device_id,
        "family_id": family_id,
        "secure_family_id": secure_family_id,
        "machine_id": machine_id,
        "hni": HAR_HNI,
        "carrier": HAR_CARRIER,
        "api": api,
    }


def _compute_sig(params: dict, secret: str) -> str:
    sorted_keys = sorted(k for k in params.keys() if k != "sig")
    raw = "".join(f"{k}={params[k]}" for k in sorted_keys)
    return hashlib.md5((raw + secret).encode()).hexdigest()


def _headers(friendly_name: str, ident: dict, login: bool = False) -> dict:
    """Headers exactly matching fb/full.har recovery + login entries."""
    ua = ident.get("login_ua", ident["ua"]) if login else ident["ua"]
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": ua,
        "Authorization": "OAuth null",
        "X-FB-Friendly-Name": friendly_name,
        "X-FB-Connection-Quality": "EXCELLENT",
        "X-FB-Connection-Type": "WIFI",
        "X-FB-Net-HNI": ident["hni"],
        "X-FB-SIM-HNI": ident["hni"],
        "X-FB-RMD": HAR_RMD,
        "X-Zero-EH": HAR_ZERO_EH,
        "X-Zero-State": "unknown",
        "X-FB-HTTP-Engine": "Tigon/Liger",
        "X-FB-Client-IP": "True",
        "X-FB-Server-Cluster": "True",
        "X-FB-Device-Group": HAR_DEVICE_GROUP,
        "X-FB-Request-Analytics-Tags": (
            '{"network_tags":{"product":"350685531728","retry_attempt":"0"},'
            '"application_tags":"unknown"}'
        ),
        "X-Tigon-Is-Retry": "False",
        "Accept-Encoding": "gzip, deflate",
    }


def _new_session(proxy=None):
    """Create session with optional proxy."""
    s = http.Session()
    s.verify = False
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def _classify_delivery(code_length: int, method: str) -> tuple:
    """SMS codes are 6-8 digits per FB API. success=true means SMS requested."""
    if method == "whatsapp":
        ok = code_length in SMS_CODE_LENGTHS or code_length >= 6
        return "whatsapp", ok
    if code_length in SMS_CODE_LENGTHS:
        return "sms", True
    if code_length >= 6:
        return "sms", True
    if code_length > 0:
        return "sms", False
    return "none", False


# ═══ STEP 1: Search (recover_accounts) — full.har entry 7 ═══
def s3_search(phone: str, session=None, identity=None, use_proxy=True,
              force_real_sms: bool = True) -> S3SearchResult:
    ident = identity or _new_identity()
    api = ident["api"]
    proxy = _get_proxy() if use_proxy else None
    s = session or _new_session(proxy)
    result = S3SearchResult(phone=phone)

    params = {
        "q": phone,
        "friend_name": "",
        "qs": "",
        "summary": "true",
        "device_id": ident["device_id"],
        "src": "fb4a_account_recovery",
        "machine_id": ident["machine_id"],
        "sfdid": ident["secure_family_id"],
        "fdid": ident["device_id"],
        "sim_serials": "[]",
        "msgr_sso_uids": "[]",
        "sms_retriever": "false",
        "cds_experiment_group": "-1",
        "shared_phone_test_group": "",
        "allowlist_email_exp_name": "",
        "shared_phone_exp_name": "",
        "shared_phone_cp_nonce_code": "",
        "shared_phone_number": "",
        "is_auto_search": "false",
        "is_feo2_api_level_enabled": "false",
        "is_sso_like_oauth_search": "false",
        "encrypted_msisdn": "",
        "locale": "id_ID",
        "client_country_code": "ID",
        "method": "GET",
        "fb_api_req_friendly_name": "accountRecoverySearch",
        "fb_api_caller_class": "AccountSearchHelper",
        "api_key": api["api_key"],
        "access_token": api["access_token"],
    }
    params["sig"] = _compute_sig(params, api["app_secret"])

    try:
        r = s.post("https://b-graph.facebook.com/recover_accounts",
                   data=params, headers=_headers("accountRecoverySearch", ident), timeout=15)
        if r.status_code != 200:
            result.error = f"HTTP {r.status_code}: {r.text[:150]}"
            result.rate_limited = _is_rate_limited(result.error)
            return result

        data = r.json()
        result.total_count = data.get("summary", {}).get("total_count", 0)
        for acc in data.get("data", []):
            a = S3Account(
                cuid=acc.get("id", ""),
                name=acc.get("name", ""),
                can_show=acc.get("can_show_profile_pic_and_name", False),
                wa_first=bool(acc.get("wa_first")),
            )
            for cp in acc.get("contactpoints", {}).get("data", []):
                if cp["type"] == "PHONE" and not a.phone_cp_id:
                    a.phone_cp_id = cp["id"]
                    a.phone_display = cp.get("display", "")
                elif cp["type"] == "EMAIL":
                    a.email_display = cp.get("display", "")
            result.accounts.append(a)

        result.found = len(result.accounts) > 0
    except Exception as e:
        result.error = str(e)
        result.rate_limited = _is_rate_limited(result.error)

    return result


def s3_search_retry(phone: str, max_retries: int = S3_MAX_RETRIES, use_proxy: bool = False,
                    force_real_sms: bool = False):
    """Search with retry on rate-limit / network errors. Returns (result, identity, session)."""
    last = S3SearchResult(phone=phone)
    ident = None
    sess = None
    for attempt in range(max_retries):
        ident = _new_identity()
        sess = _new_session(None)
        last = s3_search(phone, session=sess, identity=ident, use_proxy=use_proxy,
                         force_real_sms=force_real_sms)
        if last.found:
            return last, ident, sess
        if _is_retryable(last.error) and attempt < max_retries - 1:
            wait = random.uniform(*S3_RETRY_WAIT) + attempt * 2
            log.info("[S3] %s: retry %d/%d in %.1fs", phone, attempt + 2, max_retries, wait)
            time.sleep(wait)
            continue
        if _is_rate_limited(last.error):
            last.rate_limited = True
        break
    return last, ident, sess


# ═══ STEP 2: Send SMS — full.har entry 5/9/14 ═══
def s3_send_sms(cuid: str, contactpoint_id: str, method: str = "sms",
                session=None, identity=None, use_proxy=True) -> S3SmsResult:
    """Send recovery SMS via accountRecoverySendConfirmationCode (HAR exact)."""
    ident = identity or _new_identity()
    api = ident["api"]
    proxy = _get_proxy() if use_proxy else None
    s = session or _new_session(proxy)
    result = S3SmsResult()

    if method == "whatsapp":
        fn = "accountRecoverySendWhatsappCode"
        caller = "SendWhatsappCodeHelper"
        params = {
            "contactpoints": json.dumps([contactpoint_id]),
            "device_id": ident["device_id"], "src": "",
            "use_google_sms_retriever_content": "false",
            "is_whatsapp_installed": "true",
            "client_rate_limiting_rejected_nonce": "false",
            "client_has_permission": "false",
            "should_use_flash_call": "false",
            "auto_conf_flow_type": "",
            "family_device_id": ident["family_id"],
            "locale": "id_ID", "client_country_code": "ID",
            "fb_api_req_friendly_name": fn,
            "fb_api_caller_class": caller,
            "api_key": api["api_key"], "access_token": api["access_token"],
        }
    else:
        fn = "accountRecoverySendConfirmationCode"
        caller = "SendConfirmationCodeHelper"
        params = {
            "contactpoints": json.dumps([contactpoint_id]),
            "device_id": ident["device_id"],
            "src": "",
            "use_google_sms_retriever_content": "true",
            "client_rate_limiting_rejected_nonce": "false",
            "client_has_permission": "false",
            "should_use_flash_call": "false",
            "auto_conf_flow_type": "",
            "family_device_id": ident["family_id"],
            "locale": "id_ID",
            "client_country_code": "ID",
            "fb_api_req_friendly_name": fn,
            "fb_api_caller_class": caller,
            "api_key": api["api_key"],
            "access_token": api["access_token"],
        }

    params["sig"] = _compute_sig(params, api["app_secret"])

    try:
        url = f"https://b-graph.facebook.com/{cuid}/recovery_codes"
        r = s.post(url, data=params, headers=_headers(fn, ident), timeout=20)
        result.raw = r.text[:300]
        if r.status_code == 200:
            j = r.json()
            code_len = int(j.get("code_length", 0) or 0)
            channel, ok_len = _classify_delivery(code_len, method)
            result.code_length = code_len
            result.channel = channel
            result.auto_conf = j.get("auto_conf_flow_type", "")
            api_ok = bool(j.get("success", False))
            result.success = api_ok and ok_len
            result.real_sms = result.success if method == "sms" else False
            if api_ok and not result.success:
                result.error = f"code_length={code_len} tidak valid (harus 6-8)"
        else:
            result.error = f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        result.error = str(e)

    return result


def s3_send_sms_retry(cuid: str, contactpoint_id: str, method: str = "sms",
                      session=None, identity=None, use_proxy: bool = False,
                      max_retries: int = S3_MAX_RETRIES) -> S3SmsResult:
    """Retry only on rate-limit / network errors — same HAR session."""
    last = S3SmsResult()
    for attempt in range(max_retries):
        last = s3_send_sms(
            cuid, contactpoint_id, method=method,
            session=session, identity=identity, use_proxy=use_proxy,
        )
        if last.success:
            return last
        if _is_retryable(last.error) and attempt < max_retries - 1:
            time.sleep(random.uniform(2, 5) + attempt * 1.5)
            continue
        break
    return last


def _validate_request(cuid: str, code: str, ident: dict, session,
                      phone: str, new_password: str, caller: str) -> S3ValidateResult:
    """Single validateCode request to /{cuid} per full.har."""
    api = ident["api"]
    result = S3ValidateResult()
    params = {
        "code": code,
        "new_password": new_password,
        "device_id": ident["device_id"],
        "terminate_other_sessions": "false",
        "ar_entry_source": "shared_phone_fallbacks",
        "ar_credential_type": "nonce_sms",
        "code_submit_type": "manual",
        "shared_phone_number": "",
        "auto_conf_client_start_message": "",
        "auto_conf_auth_response": "",
        "auto_conf_flow_type": "",
        "auto_conf_contactpoint": phone if new_password else "",
        "auto_conf_verifier_data": "",
        "auto_conf_consent_state": "",
        "auto_conf_metadata": "",
        "family_device_id": ident["family_id"],
        "locale": "id_ID",
        "client_country_code": "ID",
        "fb_api_req_friendly_name": "accountRecoveryValidateCode",
        "fb_api_caller_class": caller,
        "api_key": api["api_key"],
        "access_token": api["access_token"],
    }
    params["sig"] = _compute_sig(params, api["app_secret"])
    try:
        url = f"https://b-graph.facebook.com/{cuid}"
        r = session.post(url, data=params,
                         headers=_headers("accountRecoveryValidateCode", ident), timeout=20)
        result.raw = r.text[:500]
        if r.status_code == 200:
            j = r.json()
            result.uid = str(j.get("id", "") or "")
            result.contact = j.get("ar_contact_point", "")
            result.success = bool(result.uid)
        else:
            try:
                j = r.json()
                err = j.get("error", {})
                result.error = err.get("error_user_title", err.get("message", f"HTTP {r.status_code}"))
            except Exception:
                result.error = f"HTTP {r.status_code}"
    except Exception as e:
        result.error = str(e)
    return result


# ═══ STEP 3: Validate Code + Set Password — full.har entry 1/3 ═══
def s3_validate(cuid: str, code: str, phone: str = "",
                password: str = DEFAULT_PASSWORD, session=None, identity=None,
                use_proxy=False) -> S3ValidateResult:
    ident = identity or _new_identity()
    proxy = _get_proxy() if use_proxy else None
    s = session or _new_session(proxy)

    # HAR entry 1: validate + set password (RecoveryResetPasswordFragment)
    if password:
        r = _validate_request(
            cuid, code, ident, s, phone, password,
            "RecoveryResetPasswordFragment",
        )
        if r.success:
            return r

    # HAR entry 3: validate code only (ValidateConfirmationCodeHelper)
    r = _validate_request(
        cuid, code, ident, s, phone, "",
        "ValidateConfirmationCodeHelper",
    )
    if r.success:
        return r

    # Retry validate + password with contactpoint set (HAR entry 1 style)
    if password:
        return _validate_request(
            cuid, code, ident, s, phone, password,
            "RecoveryResetPasswordFragment",
        )
    return r


# ═══ STEP 4: Login — full.har entry 0 ═══
def s3_login(uid: str, password: str = DEFAULT_PASSWORD, session=None, identity=None,
             use_proxy=False) -> S3LoginResult:
    ident = identity or _new_identity()
    login_api = RECOVERY_API
    proxy = _get_proxy() if use_proxy else None
    s = session or _new_session(proxy)
    result = S3LoginResult()

    params = {
        "adid": uuid.uuid4().hex[:16],
        "format": "json",
        "device_id": ident["device_id"],
        "email": uid,
        "password": password,
        "generate_analytics_claim": "1",
        "community_id": "",
        "linked_guest_account_userid": "",
        "cpl": "true",
        "try_num": "1",
        "family_device_id": ident["family_id"],
        "secure_family_device_id": ident["secure_family_id"],
        "sso_source_to_userid": "{}",
        "account_switcher_uids": "[]",
        "fb4a_shared_phone_cpl_experiment": "fb4a_shared_phone_nonce_cpl_at_risk_v3",
        "fb4a_shared_phone_cpl_group": "enable_v3_at_risk",
        "generate_session_cookies": "1",
        "error_detail_type": "button_with_disabled",
        "source": "account_recovery",
        "machine_id": ident["machine_id"],
        "jazoest": "22303",
        "meta_inf_fbmeta": "",
        "encrypted_msisdn": "",
        "currently_logged_in_userid": "0",
        "locale": "id_ID",
        "client_country_code": "ID",
        "fb_api_req_friendly_name": "authenticate",
        "fb_api_caller_class": "Fb4aAuthHandler",
        "api_key": login_api["api_key"],
        "access_token": login_api["access_token"],
    }
    params["sig"] = _compute_sig(params, login_api["app_secret"])

    try:
        r = s.post("https://b-graph.facebook.com/auth/login",
                   data=params, headers=_headers("authenticate", ident, login=True), timeout=20)
        j = r.json()

        if "access_token" in j:
            result.result = "SUCCESS"
            result.access_token = j["access_token"]
            result.session_key = j.get("session_key", "")
            result.uid = j.get("uid", str(uid))
        elif "error" in j:
            err = j["error"]
            code = err.get("code", 0)
            if code == 405:
                result.result = "CHECKPOINT"
                ed = err.get("error_data", {})
                if isinstance(ed, str):
                    try:
                        ed = json.loads(ed)
                    except Exception:
                        ed = {}
                result.uid = str(ed.get("uid", uid))
                result.auth_token = ed.get("auth_token", "")
            elif code == 401:
                result.result = "WRONG_PASSWORD"
            else:
                result.result = f"ERROR_{code}"
                result.error = err.get("message", "")
        elif "error_code" in j:
            ec = j["error_code"]
            if ec == 405:
                result.result = "CHECKPOINT"
                ed = j.get("error_data", "{}")
                if isinstance(ed, str):
                    try:
                        ed = json.loads(ed)
                    except Exception:
                        ed = {}
                result.uid = str(ed.get("uid", uid))
                result.auth_token = ed.get("auth_token", "")
            elif ec == 401:
                result.result = "WRONG_PASSWORD"
            else:
                result.result = f"ERROR_{ec}"
    except Exception as e:
        result.result = "ERROR"
        result.error = str(e)

    return result
