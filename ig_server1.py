"""
Instagram Server 1 — SMS Recovery via CAA Bloks (mobile API)
Flow from igforget.har:
  search → search.async → auth_method → auth_method.async → auth_option_selection.async (SMS)
  submit_code.async → validate
"""
import json, uuid, time, logging, random, urllib.parse, re
from dataclasses import dataclass
from typing import Optional, Dict

log = logging.getLogger(__name__)

# ═══ CONFIG ═══
IG_APP_ID = "567067343352427"
DEFAULT_PASSWORD = "dika2007"
BLOKS_VER = "9fc6a7a4a577456e492c189810755fe22a6300efc23e4532268bca150fe3e27a"
BASE = "https://i.instagram.com"

# Optional proxy (rotating region)
GATE_HOST = "150.109.18.221"
GATE_PORT = "4950"
GATE_USER_TPL = "gateproxymay30l6b33o-zone-abc-region-{cc}"
GATE_PASS = "GPBOTQ76"
PROXY_COUNTRIES = ["ID", "SG", "MY", "PH", "TH", "VN", "GH", "NG"]

BLOKS_VERSIONS = [
    "edf962326770574232e3938baf0c2faebdbb23703933345b000509f560bd9965",
    "c55a52bd095e76d9a88e2142eaaaf567c093da6c0c7802e7a2f101603d8a7d49",
    "9fc6a7a4a577456e492c189810755fe22a6300efc23e4532268bca150fe3e27a",
]

DEVICES = [
    ("Xiaomi/Redmi", "25062RN2DY", "creek", "qcom", "33/13", "480dpi", "1080x2400"),
    ("Xiaomi/Redmi", "23053RN02A", "ice", "qcom", "34/14", "420dpi", "1080x2340"),
    ("samsung", "SM-S918B", "dm3q", "qcom", "34/14", "640dpi", "1080x2400"),
    ("TECNO", "TECNO CK8n", "CK8n", "mt6789", "34/14", "320dpi", "720x1612"),
]
IG_VERSIONS = ['275.0.0.27.98', '320.0.0.43.109', '330.0.0.40.92']
IG_CODES = ['458229257', '579988320', '600000000']

# Active recovery sessions (phone → session) for code validation
_sessions: Dict[str, "IGRecoverySession"] = {}


@dataclass
class IGSearchResult:
    phone: str = ""
    found: bool = False
    error: str = ""


@dataclass
class IGSmsResult:
    success: bool = False
    error: str = ""
    raw: str = ""


@dataclass
class IGValidateResult:
    success: bool = False
    uid: str = ""
    username: str = ""
    error: str = ""
    raw: str = ""


def _random_ua():
    brand, model, code, chip, android, dpi, res = random.choice(DEVICES)
    ver = random.choice(IG_VERSIONS)
    c = random.choice(IG_CODES)
    return f"Instagram {ver} Android ({android}; {dpi}; {res}; {brand}; {model}; {code}; {chip}; id_ID; {c})"


def _new_identity():
    return {
        "device_id": str(uuid.uuid4()),
        "family_id": str(uuid.uuid4()),
        "android_id": f"android-{uuid.uuid4().hex[:16]}",
        "phone_id": str(uuid.uuid4()),
        "ua": _random_ua(),
        "bloks": random.choice(BLOKS_VERSIONS),
    }


def _headers(ident):
    return {
        'user-agent': ident["ua"],
        'x-ig-app-locale': 'in_ID', 'x-ig-device-locale': 'in_ID',
        'x-ig-mapped-locale': 'id_ID',
        'x-bloks-version-id': ident["bloks"],
        'x-ig-www-claim': '0', 'x-bloks-is-layout-rtl': 'false',
        'x-ig-device-id': ident["device_id"],
        'x-ig-family-device-id': ident["family_id"],
        'x-ig-android-id': ident["android_id"],
        'x-ig-timezone-offset': '25200',
        'x-fb-connection-type': 'MOBILE.LTE',
        'x-ig-connection-type': 'MOBILE(LTE)',
        'x-ig-capabilities': '3brTv10=',
        'x-ig-app-id': IG_APP_ID,
        'ig-intended-user-id': '0',
        'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'x-fb-http-engine': 'Liger',
        'x-fb-client-ip': 'True',
        'x-fb-server-cluster': 'True',
        'accept-language': 'id-ID, en-US',
    }


def _new_session(use_proxy=False):
    import requests
    s = requests.Session()
    s.verify = False
    if use_proxy:
        cc = random.choice(PROXY_COUNTRIES)
        user = GATE_USER_TPL.replace("{cc}", cc)
        proxy = f"http://{user}:{GATE_PASS}@{GATE_HOST}:{GATE_PORT}"
        s.proxies = {"http": proxy, "https": proxy}
    return s


def _email_only_recovery(text):
    """Search response shows email as only/default recovery option."""
    return '"initial":"email"' in text and '"initial":"phone"' not in text


def _extract_tokens(text):
    return re.findall(r'(Ad[A-Za-z0-9_\-=|+/]{30,})', text)


def _pick_context(tokens):
    arm = [t for t in tokens if t.endswith('|arm')]
    return arm[-1] if arm else (tokens[-1] if tokens else None)


def _pick_short(tokens):
    for t in tokens:
        if 150 < len(t) < 350 and not t.endswith('|arm'):
            return t
    return None


def _sms_sent(text):
    return ('NOTIF_DELIVERY' in text or 'polling_start_time' in text or
            'CAA_ACCOUNT_RECOVERY_CODE_ENTRY' in text or
            'authentication_confirmation' in text or 'submit_code' in text)


def _rate_limited(text):
    low = text.lower()
    return 'spam' in low or 'terlalu banyak' in low or 'try again later' in low


def _dialog_error(text):
    m = re.search(r'40, "([^"]{5,120})"', text)
    if m:
        return m.group(1)
    if _rate_limited(text):
        return 'rate_limit'
    return ''


def _account_not_found(text):
    low = text.lower()
    return ('tidak ditemukan' in low or 'not found' in low or
            'no users found' in low or 'couldn\'t find' in low)


def _qpl():
    return {
        "INTERNAL__latency_qpl_marker_id": 36707139,
        "INTERNAL__latency_qpl_instance_id": str(random.randint(1000000000000, 9999999999999)),
    }


class IGRecoverySession:
    """Maintains bloks context across recovery steps."""

    def __init__(self, identity=None, use_proxy=False):
        import requests
        self.ident = identity or _new_identity()
        self.sess = _new_session(use_proxy)
        self.use_proxy = use_proxy
        self.context_data = None
        self.auth_async_params = None
        self._warmed = False

    def _warm(self):
        if self._warmed:
            return
        sync = urllib.parse.urlencode({
            'id': self.ident['device_id'], 'server_config_retrieval': '1', '_csrftoken': 'missing',
        })
        try:
            self.sess.post(f"{BASE}/api/v1/launcher/sync/", data=sync,
                           headers=_headers(self.ident), timeout=15)
        except Exception:
            pass
        self._warmed = True

    def _bloks(self, endpoint, inner):
        self._warm()
        bk_ctx = json.dumps({"bloks_version": self.ident["bloks"], "styles_id": "instagram"})
        outer = json.dumps({"params": json.dumps(inner)})
        data = (f"params={urllib.parse.quote(outer)}"
                f"&bk_client_context={urllib.parse.quote(bk_ctx)}"
                f"&bloks_versioning_id={self.ident['bloks']}")
        url = f"{BASE}/api/v1/bloks/apps/{endpoint}"
        return self.sess.post(url, data=data, headers=_headers(self.ident), timeout=25)

    def _update_ctx(self, resp_text):
        tokens = _extract_tokens(resp_text)
        ctx = _pick_context(tokens)
        if ctx:
            self.context_data = ctx
        short = _pick_short(tokens)
        if short:
            self.auth_async_params = short

    def search_account(self, phone: str) -> IGSearchResult:
        result = IGSearchResult(phone=phone)
        try:
            aid = self.ident["android_id"]
            # search view
            r0 = self._bloks("com.bloks.www.caa.ar.search/", {
                "server_params": {"device_id": aid, "is_platform_login": 0, "is_from_logged_out": 0,
                    "access_flow_version": "pre_mt_behavior", "login_surface": "login_home",
                    "login_entry_point": "logged_out", "context_data": None},
                "client_input_params": {}
            })
            self._update_ctx(r0.text)
            time.sleep(0.3)

            r1 = self._bloks("com.bloks.www.caa.ar.search.async/", {
                "server_params": {
                    "device_id": aid, "event_request_id": str(uuid.uuid4()), **_qpl(),
                    "family_device_id": None, "waterfall_id": None,
                    "offline_experiment_group": None, "layered_homepage_experiment_group": None,
                    "is_platform_login": 0, "is_from_logged_in_switcher": 0, "is_from_logged_out": 0,
                    "access_flow_version": "pre_mt_behavior",
                    "login_surface": "login_home", "login_entry_point": "logged_out",
                    "context_data": self.context_data,
                },
                "client_input_params": {
                    "search_query": phone, "search_screen_type": "mobile",
                    "text_input_id": "muqe9:103", "encrypted_msisdn": "",
                    "fetched_email_list": [], "fetched_email_token_list": {},
                    "sso_accounts_auth_data": [], "sfdid": "",
                    "headers_infra_flow_id": "", "was_headers_prefill_available": 0,
                    "was_headers_prefill_used": 0, "ig_oauth_token": [],
                    "android_build_type": "", "is_whatsapp_installed": 0,
                    "device_network_info": None, "accounts_list": [],
                    "is_oauth_without_permission": 0, "ig_vetted_device_nonce": "",
                    "gms_incoming_call_retriever_eligibility": "client_not_supported",
                    "auth_secure_device_id": "", "blocked_uids": [],
                    "cloud_trust_token": None, "network_bssid": None,
                    "lois_settings": {"lois_token": ""}, "aac": "", "zero_balance_state": None,
                }
            })
            self._update_ctx(r1.text)

            if r1.status_code != 200:
                result.error = f"HTTP {r1.status_code}"
                return result
            if _account_not_found(r1.text):
                result.found = False
                result.error = "account_not_found"
                return result
            if _email_only_recovery(r1.text):
                result.found = True
                result.error = "email_only_recovery"
                return result
            result.found = True
        except Exception as e:
            result.error = str(e)[:100]
        return result

    def send_sms(self, phone: str) -> IGSmsResult:
        result = IGSmsResult()
        try:
            sr = self.search_account(phone)
            if not sr.found:
                result.error = sr.error or "account_not_found"
                return result
            if sr.error == "email_only_recovery":
                result.error = "Akun pakai recovery email, SMS ke nomor tidak tersedia"
                return result

            aid = self.ident["android_id"]
            time.sleep(0.3)

            # auth_method view
            r2 = self._bloks("com.bloks.www.caa.ar.auth_method/", {
                "server_params": {"device_id": aid, "is_platform_login": 0, "is_from_logged_out": 0,
                    "access_flow_version": "pre_mt_behavior", "login_surface": "account_recovery",
                    "login_entry_point": "account_recovery", "context_data": self.context_data},
                "client_input_params": {}
            })
            self._update_ctx(r2.text)
            time.sleep(0.3)

            # auth_method.async — reject flash_call, force SMS
            r3 = self._bloks("com.bloks.www.caa.ar.auth_method.async/", {
                "server_params": {
                    "device_id": aid, "auth_method": "phone", "is_auth_method_rejected": 1,
                    "auth_method_async_params": self.auth_async_params or "",
                    "context_data": self.context_data, **_qpl(),
                    "family_device_id": None, "waterfall_id": None,
                    "offline_experiment_group": None, "layered_homepage_experiment_group": None,
                    "is_platform_login": 0, "is_from_logged_in_switcher": 0, "is_from_logged_out": 0,
                    "access_flow_version": "pre_mt_behavior",
                    "login_surface": "account_recovery", "login_entry_point": "account_recovery",
                },
                "client_input_params": {
                    "zero_balance_state": "", "android_build_type": "",
                    "cloud_trust_token": None, "network_bssid": None,
                    "lois_settings": {"lois_token": ""}, "aac": "",
                }
            })
            self._update_ctx(r3.text)
            err3 = _dialog_error(r3.text)
            if err3:
                result.error = err3
                return result
            time.sleep(0.3)

            # initiate_view (HAR shows this between auth_method.async and auth_option)
            r_init = self._bloks("com.bloks.www.caa.ar.initiate_view/", {
                "server_params": {"device_id": aid, "is_platform_login": 0, "is_from_logged_out": 0,
                    "access_flow_version": "pre_mt_behavior", "login_surface": "account_recovery",
                    "login_entry_point": "account_recovery", "context_data": self.context_data},
                "client_input_params": {}
            })
            self._update_ctx(r_init.text)
            time.sleep(0.3)

            # auth_option_selection.async — SEND SMS
            r4 = self._bloks("com.bloks.www.caa.ar.auth_option_selection.async/", {
                "server_params": {
                    "device_id": aid, "event_request_id": str(uuid.uuid4()),
                    "auth_options": ["email", "phone", "password"], "lara_usage": 0, **_qpl(),
                    "family_device_id": None, "waterfall_id": None,
                    "offline_experiment_group": None, "layered_homepage_experiment_group": None,
                    "is_platform_login": 0, "is_from_logged_in_switcher": 0, "is_from_logged_out": 0,
                    "access_flow_version": "pre_mt_behavior",
                    "login_surface": "account_recovery", "login_entry_point": "account_recovery",
                    "context_data": self.context_data,
                },
                "client_input_params": {
                    "family_device_id": "", "machine_id": "", "zero_balance_state": "",
                    "auth_option": "phone", "android_build_type": "",
                    "selected_phone_number_index": 0,
                    "selected_xapp_contactpoint_index": None,
                    "selected_encrypted_bloks_xapp_cp_lookup_data": None,
                    "cloud_trust_token": None, "network_bssid": None,
                    "lois_settings": {"lois_token": ""}, "aac": "",
                }
            })
            self._update_ctx(r4.text)
            result.raw = r4.text[:300]

            if _sms_sent(r4.text):
                result.success = True
                _sessions[phone] = self
            else:
                err4 = _dialog_error(r4.text)
                if 'Maaf' in r4.text or 'ada masalah' in r4.text:
                    result.error = "IG menolak SMS ke nomor ini (coba lagi nanti)"
                else:
                    result.error = err4 or "sms_failed"
        except Exception as e:
            result.error = str(e)[:100]
        return result

    def validate_code(self, phone: str, code: str, password: str = DEFAULT_PASSWORD) -> IGValidateResult:
        result = IGValidateResult()
        try:
            aid = self.ident["android_id"]
            r = self._bloks("com.bloks.www.caa.ar.submit_code.async/", {
                "server_params": {
                    "device_id": aid, "event_request_id": str(uuid.uuid4()),
                    "code_submit_source": "manual",
                    "text_input_id": str(random.randint(1000000000000, 9999999999999)),
                    "auto_clear_on_error": 0,
                    "context_data": self.context_data, **_qpl(),
                    "family_device_id": None, "waterfall_id": None,
                    "offline_experiment_group": None, "layered_homepage_experiment_group": None,
                    "is_platform_login": 0, "is_from_logged_in_switcher": 0, "is_from_logged_out": 0,
                    "access_flow_version": "pre_mt_behavior",
                    "login_surface": "account_recovery", "login_entry_point": "account_recovery",
                },
                "client_input_params": {
                    "machine_id": "", "cloud_trust_token": None,
                    "block_store_machine_id": "", "nonce": code,
                    "auth_secure_device_id": "", "encrypted_msisdn": "",
                    "nonce_length": len(code), "is_sms_retriever_success": 0,
                    "network_bssid": None,
                    "lois_settings": {"lois_token": ""}, "aac": "",
                }
            })
            result.raw = r.text[:300]
            self._update_ctx(r.text)

            if 'authentication_confirmation' in r.text or 'password' in r.text.lower():
                # Code accepted — try password reset via legacy endpoint
                pw_data = urllib.parse.urlencode({
                    "query": phone, "recovery_code": code,
                    "new_password": password,
                    "device_id": self.ident["device_id"],
                    "guid": self.ident["device_id"],
                    "waterfall_id": str(uuid.uuid4()),
                })
                r2 = self.sess.post(f"{BASE}/api/v1/accounts/account_recovery_code_verify/",
                                    data=pw_data, headers=_headers(self.ident), timeout=15)
                result.raw = r2.text[:300]
                if r2.status_code == 200:
                    try:
                        j = r2.json()
                        if j.get("status") == "ok" or "access_token" in r2.text:
                            result.success = True
                            result.uid = str(j.get("user_id", j.get("pk", "")))
                            result.username = j.get("username", "")
                            return result
                    except Exception:
                        pass
                # Code valid even if password reset fails
                if not _dialog_error(r.text):
                    result.success = True
                    um = re.search(r'"username"\s*:\s*"([^"]+)"', r.text)
                    uidm = re.search(r'"user_id"\s*:\s*"?(\d+)"?', r.text)
                    if um:
                        result.username = um.group(1)
                    if uidm:
                        result.uid = uidm.group(1)
                    if not result.username:
                        result.error = r2.text[:80] if r2.status_code != 200 else "code_ok_reset_failed"
                    return result

            err = _dialog_error(r.text)
            result.error = err or "invalid_code"
        except Exception as e:
            result.error = str(e)[:100]
        return result


# ═══ PUBLIC API ═══

def ig_search(phone: str, session=None, identity=None, use_proxy=False) -> IGSearchResult:
    """Check if IG account exists for phone (CAA search.async)."""
    rec = IGRecoverySession(identity, use_proxy=use_proxy)
    return rec.search_account(phone)


def ig_send_sms(phone: str, session=None, identity=None, use_proxy=False) -> IGSmsResult:
    """Send password-reset SMS via CAA bloks flow (with retry)."""
    last = IGSmsResult()
    for attempt in range(3):
        if attempt:
            time.sleep(2 + attempt)
        proxy = use_proxy or (attempt > 0)
        rec = IGRecoverySession(identity, use_proxy=proxy)
        last = rec.send_sms(phone)
        if last.success:
            return last
        if last.error in ('rate_limit',) or 'rate' in (last.error or '').lower():
            time.sleep(5)
            continue
        if last.error in ('account_not_found', 'Akun pakai recovery email, SMS ke nomor tidak tersedia'):
            return last
    return last


def ig_validate(phone: str, code: str, password: str = DEFAULT_PASSWORD,
                session=None, identity=None, use_proxy=False) -> IGValidateResult:
    """Validate SMS code and reset password."""
    rec = _sessions.get(phone)
    if rec:
        return rec.validate_code(phone, code, password)
    # Fallback: new session (may fail without context)
    rec = IGRecoverySession(identity)
    return rec.validate_code(phone, code, password)


def ig_clean_phone(phone: str) -> str:
    phone = phone.strip().replace("+", "").replace("-", "").replace(" ", "")
    if phone.startswith("0") and len(phone) > 5:
        phone = "62" + phone[1:]
    return phone
