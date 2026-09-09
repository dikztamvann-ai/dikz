"""
OwlProxy auto-register + claim 200MB + extract proxy.
Email handler: emailqu.com (anon temp mail).

Flow (matching real browser HAR exactly):
  1. GET proxy.owlproxy.com/buy  (pre-login landing page)
  2. Generate email at emailqu.com
  3. POST /sms/smsSend  (try empty captcha first; fall back to Aliyun)
  4. Poll emailqu inbox for the 6-digit OTP
  5. POST /user/login  -> token + userId
  6. GET proxy.owlproxy.com/  (POST-LOGIN PAGE REFRESH — critical!)
  7. Batch 1: getUserInfo + getCommonConfig + vcProxy calls
  8. Sleep 8s  (mimic user reading page)
  9. Batch 2: getUserInfo + getNewUserGuideConfig_V2 + getUserActiveBanner + vcProxy calls
  10. Sleep 3s  (user sees popup, clicks claim)
  11. GET /newUserGuide/getNewUserReceiveTraffic?guideId=<from step 9>
  12. POST /vcDynamicGood/createProxy  -> proxy creds

Run:  python owlproxy_auto.py [--proto socks5|http] [--country SN] [--cycles 0]
      (--cycles 0 = infinite loop until success)
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import random
import re
import string
import sys
import time
import uuid
from urllib.parse import quote

try:
    from curl_cffi import requests as cf_requests
    _IMPERSONATE = "chrome124"
    HAS_CFFI = True
except ImportError:
    import requests as cf_requests
    _IMPERSONATE = None
    HAS_CFFI = False

# ── Config ─────────────────────────────────────────────────────────────
EMAILQU = "https://emailqu.com"
OWL_API = "https://api.owlproxy.com/owlproxy/api"
OWL_REF = "https://proxy.owlproxy.com/"
ALIYUN_SCENE_ID = "5jvar3wp"
GUIDE_ID = 10003          # 200 MB free traffic guide

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"

# Base headers for OwlProxy API.
# NOTE: "channel" is intentionally NOT here — the real browser only sends
# it on the /user/login call, NOT on any subsequent API calls.
HEADERS_OWL = {
    "user-agent": UA,
    "content-type": "application/json",
    "accept": "application/json, text/plain, */*",
    "accept-language": "en",
    "origin": "https://proxy.owlproxy.com",
    "referer": OWL_REF,
    "requestsource": "wechat-miniapp",
    "suppliertype": "0",
    "appversion": "2007502",
    "clienttype": "web",
    "userid": "0",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}

HEADERS_EMAILQU = {
    "user-agent": UA,
    "accept-language": "en-US,en;q=0.9",
}


def _seed_session_cookies(s) -> None:
    """Seed realistic fingerprint cookies matching real browser."""
    visitor = uuid.uuid4().hex
    now = int(time.time())
    cookies = {
        "uuid": visitor,
        "_ga": f"GA1.1.{random.randint(10**8, 10**10)}.{now}",
        "_ga_390DLYES4J": f"GS2.1.s{now}$o1$g0$t{now}$j17$l0$h0",
        "_ga_QFJM8SB9Q4": f"GS2.1.s{now}$o1$g0$t{now}$j17$l0$h0",
        "_gcl_au": f"1.1.{random.randint(10**9, 10**10)}.{now}",
        "_clck": (f"{uuid.uuid4().hex[:6]}%5E2%5E"
                  f"{uuid.uuid4().hex[:3]}%5E0%5E{random.randint(1000, 9999)}"),
        "_clsk": (f"{uuid.uuid4().hex[:6]}%5E{now*1000}%5E1%5E1%5E"
                  "r.clarity.ms%2Fcollect"),
    }
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=".owlproxy.com")


log = logging.getLogger("owl")
# Only configure root logger when run as script (not when imported)
if __name__ == "__main__" or not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


# ── Email (emailqu.com) ────────────────────────────────────────────────
def _clean_text(s: str) -> str:
    """Strip zero-width characters that emailqu sometimes embeds in domains."""
    return re.sub(r'[\u200b\u200c\u200d\u200e\u200f\ufeff]', '', s).strip()


def emailqu_pick_address(s) -> str:
    u = s.get(f"{EMAILQU}/api/random-username", headers=HEADERS_EMAILQU, timeout=20).json()
    user = _clean_text(u.get("username") or _rand_user())

    d = s.get(f"{EMAILQU}/api/domains/random", headers=HEADERS_EMAILQU, timeout=20).json()
    domains = [_clean_text(x["domain"]) for x in d.get("domains", [])
               if not x.get("is_subdomain")]
    if not domains:
        domains = ["capemain.games"]
    domain = random.choice(domains)
    addr = f"{user}{random.randint(10, 999)}@{domain}"
    log.info("email = %s", addr)
    return addr


def emailqu_wait_otp(s, addr: str, timeout: int = 120) -> str:
    """Poll the public inbox until a 6-digit OTP arrives."""
    deadline = time.time() + timeout
    seen = set()
    url = f"{EMAILQU}/api/public/emails/{quote(addr)}?limit=10"

    while time.time() < deadline:
        try:
            r = s.get(url, headers=HEADERS_EMAILQU, timeout=20).json()
        except Exception as e:
            log.warning("inbox poll err: %s", e)
            time.sleep(3)
            continue

        for em in r.get("emails", []):
            mid = em.get("id") or em.get("uid") or em.get("messageId")
            if mid in seen:
                continue
            seen.add(mid)
            blob = json.dumps(em, ensure_ascii=False)
            m = (re.search(r"(?:Verification[^0-9]{0,40}|Code[^0-9]{0,40})(\d{4,8})",
                           blob, re.I)
                 or re.search(r"\b(\d{6})\b", blob))
            if m:
                code = m.group(1)
                log.info("OTP = %s", code)
                return code
        time.sleep(3)
    raise TimeoutError("OTP timed out - inbox stayed empty")


# ── Aliyun captcha ───────────────────────────────────────────────────
ALIYUN_AK = "LTAI5tSEBwYMwVKAQGpxmvTd"
ALIYUN_HOST = "https://69r0rc.captcha-open-southeast.aliyuncs.com"


def _aliyun_sign(params: dict, secret: str = "") -> str:
    import hmac, hashlib as _h, base64
    from urllib.parse import quote as _q
    canon = "&".join(f"{_q(k, safe='')}={_q(v, safe='')}"
                     for k, v in sorted(params.items()))
    string_to_sign = f"POST&{_q('/', safe='')}&{_q(canon, safe='')}"
    digest = hmac.new((secret + "&").encode(),
                      string_to_sign.encode(), _h.sha1).digest()
    return base64.b64encode(digest).decode()


def aliyun_init_captcha(s) -> dict | None:
    params = {
        "AccessKeyId": ALIYUN_AK,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "Format": "JSON",
        "Timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": "2023-03-05",
        "Action": "InitCaptcha",
        "SceneId": ALIYUN_SCENE_ID,
        "Language": "en",
        "Mode": "popup",
        "SignatureNonce": uuid.uuid4().hex,
    }
    params["Signature"] = _aliyun_sign(params)
    try:
        r = s.post(ALIYUN_HOST,
                   headers={"content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                            "origin": "https://proxy.owlproxy.com",
                            "referer": "https://proxy.owlproxy.com/"},
                   data=params, timeout=20).json()
        if r.get("Success"):
            log.info("aliyun cert=%s", r.get("CertifyId"))
            return r
    except Exception as e:
        log.debug("aliyun init err: %s", e)
    return None


# ── OwlProxy: send OTP ───────────────────────────────────────────────
def owl_send_otp(s, email: str, captcha_override: str | None = None) -> None:
    attempts: list[dict] = []
    if captcha_override:
        attempts.append({"label": "override", "captcha": captcha_override})
    init = aliyun_init_captcha(s)
    if init:
        token_blob = ("U0dfV0VCIzM3OTVkMjgyNDJhMTE2MTliYzI1Zjc4NmY4NGU1M2Q0"
                      f"LWgtMTc4MTkwMTQ4NTY1MS0"
                      f"{uuid.uuid4().hex[:32]}#{uuid.uuid4().hex}")
        attempts.append({
            "label": "aliyun-real",
            "captcha": json.dumps({
                "sceneId": ALIYUN_SCENE_ID,
                "certifyId": init["CertifyId"],
                "deviceToken": token_blob,
            }),
        })
    attempts += [
        {"label": "empty", "captcha": ""},
        {"label": "stub", "captcha": json.dumps({
            "sceneId": ALIYUN_SCENE_ID, "certifyId": "", "deviceToken": ""})},
    ]
    # smsSend uses channel header (like login)
    sms_headers = {**HEADERS_OWL, "channel": "website"}
    last = None
    for at in attempts:
        body = {"smsType": 2, "mobilePhone": email,
                "captchaVerifyParam": at["captcha"]}
        r = s.post(f"{OWL_API}/sms/smsSend", headers=sms_headers,
                   data=json.dumps(body), timeout=30)
        try:
            j = r.json()
        except Exception:
            j = {"msg": r.text[:200], "code": r.status_code}
        log.info("sms[%s] -> %s", at["label"], j)
        last = j
        code = j.get("code")
        if code == 200:
            return
        if code == 1059:
            raise RuntimeError(f"IP_FLAG_1059: {j}")
    raise RuntimeError(f"All captcha bypass attempts failed: {last}")


# ── OwlProxy: login ────────────────────────────────────────────────────
def owl_login(s, email: str, otp: str) -> str:
    pwd = hashlib.md5(f"owl_{uuid.uuid4().hex}".encode()).hexdigest()
    body = {
        "mobilePhone": email,
        "loginType": 0,
        "verifyCode": otp,
        "channel": "website",
        "password": pwd,
    }
    # Login call specifically includes "channel": "website" in headers
    login_headers = {**HEADERS_OWL, "channel": "website"}
    r = s.post(f"{OWL_API}/user/login", headers=login_headers,
               data=json.dumps(body), timeout=30).json()
    log.info("login -> code=%s msg=%s", r.get("code"), r.get("msg"))
    if r.get("code") != 200:
        raise RuntimeError(f"login failed: {r}")
    token = r["data"]["token"]
    user_id = r["data"].get("userId", 0)
    s._owl_userid = str(user_id)
    log.info("token=%s userId=%s", token, user_id)
    return token


# ── OwlProxy: claim 200 MB (HAR-accurate two-batch flow) ──────────────
def _make_auth_headers(token: str, userid: str) -> dict:
    """Build per-call headers with token + userid (no channel!)."""
    return {**HEADERS_OWL, "token": token, "userid": userid}


def _batch1_post_login(s, h: dict) -> None:
    """First batch of API calls immediately after login (HAR: +0.6s).
    This is what the browser fires on the first page render after login."""
    calls = [
        ("GET",  "/user/getUserInfo", None),
        ("GET",  "/configure/getCommonConfig", None),
        ("POST", "/vcProxy/queryList", {"current": 1, "size": 20,
                                         "proxyName": "", "proxyBuyStatusId": "",
                                         "groupIdList": []}),
        ("POST", "/vcProxy/queryCount", {"proxyBuyStatusId": 1}),
        ("GET",  "/vcProxyGroup/list", None),
    ]
    for method, path, body in calls:
        try:
            if method == "GET":
                s.get(f"{OWL_API}{path}", headers=h,
                      params=body or {}, timeout=20)
            else:
                s.post(f"{OWL_API}{path}", headers=h,
                       data=json.dumps(body or {}), timeout=20)
        except Exception as e:
            log.debug("batch1 %s err: %s", path, e)


def _batch2_dashboard(s, h: dict) -> int | None:
    """Second batch of API calls (HAR: +8.6s after login).
    The user navigated to dashboard — this triggers getNewUserGuideConfig_V2
    which makes the claim button appear. Returns the discovered guideId."""
    found_guide: int | None = None
    calls = [
        ("GET",  "/user/getUserInfo", None),
        ("GET",  "/configure/getCommonConfig", None),
        ("POST", "/vcProxy/queryList", {"current": 1, "size": 20,
                                         "proxyName": "", "proxyBuyStatusId": "",
                                         "groupIdList": []}),
        ("GET",  "/vpopDialog/getUserActiveBanner", None),
        ("GET",  "/newUserGuide/getNewUserGuideConfig_V2", None),
        ("POST", "/vcProxy/queryCount", {"proxyBuyStatusId": 1}),
        ("GET",  "/vcProxyGroup/list", None),
    ]
    for method, path, body in calls:
        try:
            if method == "GET":
                r = s.get(f"{OWL_API}{path}", headers=h,
                          params=body or {}, timeout=20).json()
            else:
                r = s.post(f"{OWL_API}{path}", headers=h,
                           data=json.dumps(body or {}), timeout=20).json()
            if path.endswith("getNewUserGuideConfig_V2"):
                log.info("guideConfig raw: code=%s data=%s",
                         r.get("code"), str(r.get("data"))[:300])
                if r.get("code") == 200:
                    data = r.get("data") or {}
                    gid = data.get("guideId")
                    show = data.get("showNewUserPop")
                    log.info("guideConfig: guideId=%s showNewUserPop=%s", gid, show)
                    if gid:
                        found_guide = int(gid)
        except Exception as e:
            log.debug("batch2 %s err: %s", path, e)
    return found_guide


def owl_claim_traffic(s, token: str, max_attempts: int = 15) -> dict:
    """
    Claim 200MB free traffic using the exact browser flow from HAR:
      1. Batch 1 API calls (immediate post-login)
      2. Sleep 8s (user reads page)
      3. Batch 2 API calls (user navigates to dashboard)
      4. Sleep 3s (user sees popup, clicks claim)
      5. Hit the claim endpoint
    """
    userid = getattr(s, "_owl_userid", "0")
    h = _make_auth_headers(token, userid)

    # ── Batch 1: immediate post-login calls
    log.info("batch1: post-login API calls...")
    _batch1_post_login(s, h)

    # ── Sleep 8s: mimic user reading the page (HAR gap: 8.6s)
    wait1 = 7 + random.random() * 2  # 7-9s
    log.info("sleeping %.0fs (mimic page read)...", wait1)
    time.sleep(wait1)

    # ── Batch 2: dashboard/guide calls — this is what makes claim eligible
    log.info("batch2: dashboard API calls...")
    discovered_guide = _batch2_dashboard(s, h)
    guide_id = discovered_guide or GUIDE_ID

    # ── Sleep 3s: user sees new-user popup and clicks claim (HAR gap: 3s)
    wait2 = 2.5 + random.random() * 1.5  # 2.5-4s
    log.info("sleeping %.0fs (mimic click claim)...", wait2)
    time.sleep(wait2)

    # ── Claim!
    last = None
    for attempt in range(1, max_attempts + 1):
        r = s.get(f"{OWL_API}/newUserGuide/getNewUserReceiveTraffic",
                  headers=h, params={"guideId": guide_id}, timeout=30).json()
        log.info("claim200MB[%d/%d] -> code=%s msg=%s",
                 attempt, max_attempts, r.get("code"), r.get("msg"))
        last = r
        if r.get("code") == 200:
            break
        if r.get("code") == 3316:
            wait = random.randint(5, 15)
            log.info("3316 queue, retry in %ds...", wait)
            time.sleep(wait)
            # Every 5 attempts, re-run batch2 to keep session warm
            if attempt % 5 == 0:
                log.info("refreshing session state...")
                _batch2_dashboard(s, h)
                time.sleep(2 + random.random() * 2)
            continue
        # Any other error code is hard failure
        break

    # Check balance regardless
    bal = s.get(f"{OWL_API}/vcDynamicGood/queryCurrentTrafficBalance",
                headers=h, timeout=30).json()
    log.info("balance -> %s", bal)
    remaining = (bal.get("data") or {}).get("remainingTraffic", 0)
    if remaining <= 0:
        raise RuntimeError(f"claim failed, no traffic: claim={last} bal={bal}")
    log.info("traffic claimed! remaining=%sMB", remaining)
    return bal


# ── OwlProxy: list hosts ──────────────────────────────────────────────
def owl_get_hosts(s, token: str) -> list[dict]:
    h = _make_auth_headers(token, getattr(s, "_owl_userid", "0"))
    r = s.get(f"{OWL_API}/vcDynamicGood/getDynamicProxyHost",
              headers=h, timeout=30).json()
    return r.get("data", [])


# ── OwlProxy: create proxy ─────────────────────────────────────────────
def owl_create_proxy(s, token: str,
                     proto: str = "socks5",
                     country: str = "SN",
                     time_min: int = 5,
                     count: int = 1,
                     host: str | None = None) -> list[dict]:
    h = _make_auth_headers(token, getattr(s, "_owl_userid", "0"))

    if not host:
        hosts = owl_get_hosts(s, token)
        host = hosts[0]["value"] if hosts else "change4.owlproxy.com:7778"

    body = {
        "proxyType": proto,
        "proxyHost": host,
        "countryCode": country,
        "state": "",
        "city": "",
        "time": time_min,
        "goodNum": count,
        "format": "ip:port@user:pass",
    }
    r = s.post(f"{OWL_API}/vcDynamicGood/createProxy",
               headers=h, data=json.dumps(body), timeout=30).json()
    log.info("createProxy -> code=%s msg=%s", r.get("code"), r.get("msg"))
    if r.get("code") != 200:
        raise RuntimeError(f"createProxy failed: {r}")
    return r.get("data", [])


# ── Helpers ────────────────────────────────────────────────────────────
def _rand_user() -> str:
    return "".join(random.choices(string.ascii_lowercase, k=8)) + str(random.randint(10, 999))


# ── Orchestrator ─────────────────────────────────────────────────────
def run_once(proto: str, country: str, count: int, time_min: int,
             host: str | None, http_proxy: str | None = None,
             captcha_override: str | None = None) -> list[dict]:
    # Owl session — routed through upstream proxy (for owlproxy.com only)
    if HAS_CFFI:
        s = cf_requests.Session(impersonate=_IMPERSONATE)
    else:
        s = cf_requests.Session()
    if http_proxy:
        s.proxies = {"http": http_proxy, "https": http_proxy}
        log.info("routing owlproxy through %s", http_proxy)
    _seed_session_cookies(s)

    # Email session — ALWAYS direct (no proxy), emailqu.com doesn't need it
    if HAS_CFFI:
        s_email = cf_requests.Session(impersonate=_IMPERSONATE)
    else:
        s_email = cf_requests.Session()

    # ── PRE-LOGIN: fetch landing page (HAR: proxy.owlproxy.com/buy)
    try:
        s.get("https://proxy.owlproxy.com/buy?channel=website&utm_source=website",
              headers={"user-agent": UA, "accept": "text/html,application/xhtml+xml",
                       "accept-language": "en-US,en;q=0.9"},
              timeout=20)
        log.info("pre-login landing page fetched")
    except Exception as e:
        log.debug("landing fetch err: %s", e)

    # ── Email + OTP + Login  (emailqu via direct session)
    email = emailqu_pick_address(s_email)
    owl_send_otp(s, email, captcha_override=captcha_override)
    otp = emailqu_wait_otp(s_email, email, timeout=180)
    token = owl_login(s, email, otp)

    # ── POST-LOGIN PAGE REFRESH (the critical missing step!)
    # HAR shows browser loads proxy.owlproxy.com/ right after login.
    # This triggers server-side session state initialization.
    try:
        s.get("https://proxy.owlproxy.com/",
              headers={"user-agent": UA, "accept": "text/html,application/xhtml+xml",
                       "accept-language": "en-US,en;q=0.9",
                       "referer": "https://proxy.owlproxy.com/buy"},
              timeout=20)
        log.info("post-login page refresh done")
    except Exception as e:
        log.debug("post-login refresh err: %s", e)

    # ── Claim traffic (two-batch HAR-accurate flow)
    owl_claim_traffic(s, token)

    # ── Create proxy
    proxies = owl_create_proxy(s, token, proto=proto,
                               country=country, count=count,
                               time_min=time_min, host=host)

    log.info("Got %d proxies!", len(proxies))
    for p in proxies:
        line = f"{p['proxyHost']}:{p['proxyPort']}:{p['userName']}:{p['password']}"
        if proto == "http":
            uri = f"http://{p['userName']}:{p['password']}@{p['proxyHost']}:{p['proxyPort']}"
        else:
            uri = f"socks5://{p['userName']}:{p['password']}@{p['proxyHost']}:{p['proxyPort']}"
        log.info("  %s", line)
        log.info("  %s", uri)
    return proxies


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", choices=["socks5", "http"], default="socks5")
    ap.add_argument("--country", default="SN", help="ISO country code")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--time", dest="time_min", type=int, default=5,
                    help="sticky session minutes")
    ap.add_argument("--host", default=None)
    ap.add_argument("--retries", type=int, default=3,
                    help="retries per cycle on hard failure")
    ap.add_argument("--cycles", type=int, default=0,
                    help="fresh-account cycles (0 = infinite until success)")
    ap.add_argument("--cycle-delay", type=int, default=8,
                    help="seconds between cycles")
    ap.add_argument("--proxy", default=None,
                    help="upstream proxy e.g. socks5://user:pass@host:port")
    ap.add_argument("--captcha", default=None,
                    help="paste captchaVerifyParam JSON from DevTools")
    args = ap.parse_args()

    import pathlib
    out_file = pathlib.Path(__file__).parent / "proxies_won.txt"

    last_err = None
    cycle = 0
    max_cycles = args.cycles if args.cycles > 0 else 999999
    while cycle < max_cycles:
        cycle += 1
        cycle_label = f"{cycle}/{args.cycles}" if args.cycles > 0 else f"{cycle}/inf"
        log.info("======== CYCLE %s ========", cycle_label)
        flagged = False
        for i in range(1, args.retries + 1):
            try:
                log.info("----- attempt %d/%d -----", i, args.retries)
                proxies = run_once(args.proto, args.country, args.count,
                                   args.time_min, args.host,
                                   http_proxy=args.proxy,
                                   captcha_override=args.captcha)
                # Persist!
                with out_file.open("a", encoding="utf-8") as f:
                    f.write(f"# cycle {cycle}  {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                    for p in proxies:
                        if args.proto == "http":
                            uri = f"http://{p['userName']}:{p['password']}@{p['proxyHost']}:{p['proxyPort']}"
                        else:
                            uri = f"socks5://{p['userName']}:{p['password']}@{p['proxyHost']}:{p['proxyPort']}"
                        f.write(uri + "\n")
                log.info("wrote %d proxies to %s", len(proxies), out_file)
                print(json.dumps(proxies, indent=2))
                return 0
            except Exception as e:
                last_err = e
                log.error("attempt %d failed: %s", i, e)
                if "IP_FLAG_1059" in str(e):
                    flagged = True
                    break
                time.sleep(2 + random.random() * 3)

        if flagged:
            wait = 120 + random.randint(0, 60)
            log.info("IP soft-ban (1059), cooling down %ds...", wait)
        else:
            wait = args.cycle_delay + random.randint(0, args.cycle_delay)
            log.info("cycle %s burned, sleeping %ds before fresh cycle", cycle_label, wait)
        time.sleep(wait)

    log.error("all %d cycles failed: %s", cycle, last_err)
    return 1


if __name__ == "__main__":
    sys.exit(main())
