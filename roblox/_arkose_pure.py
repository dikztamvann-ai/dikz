# -*- coding: utf-8 -*-
"""
Pure-API Arkose FunCaptcha for Roblox login (no browser window).

1) POST arkoselabs gt2 with dataExchangeBlob → token (hope suppressed)
2) If visual challenge: fetch images via Arkose HTTP, solve with NopeCHA Recognition API, answer rounds
3) Return final captchaToken for challenge/v1/continue
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import secrets
import string
import time
import urllib.parse
from typing import Optional

NOPECHA_KEY = os.getenv("NOPECHA_KEY", "kann78ydn7279pck")
ARKOSE_PK = "476068BF-9607-4799-B53D-966BE98E2B81"
ARKOSE_SURL = "https://arkoselabs.roblox.com"
SITE = "https://www.roblox.com"


def _md5(b: bytes) -> bytes:
    return hashlib.md5(b).digest()


def _aes_encrypt_bda(data: str, key: str) -> str:
    """Arkose-style OpenSSL EVP_BytesToKey + AES-CBC (same as common BDA gens)."""
    try:
        from Crypto.Cipher import AES
    except ImportError:
        from Cryptodome.Cipher import AES

    data_pad = data + chr(16 - len(data) % 16) * (16 - len(data) % 16)
    salt = "".join(random.choice(string.ascii_lowercase) for _ in range(8)).encode()
    salted, dx = b"", b""
    while len(salted) < 48:
        dx = _md5(dx + key.encode() + salt)
        salted += dx
    aes_key, iv = salted[:32], salted[32:48]
    aes = AES.new(aes_key, AES.MODE_CBC, iv)
    ct = base64.b64encode(aes.encrypt(data_pad.encode())).decode()
    return json.dumps({"ct": ct, "iv": iv.hex(), "s": salt.hex()}, separators=(",", ":"))


def _build_bda(user_agent: str) -> str:
    """Minimal browser fingerprint blob (BDA) for gt2."""
    ts = time.time()
    timeframe = int(ts - (ts % 21600))
    key = user_agent + str(timeframe)

    fe = [
        "DNT:unknown",
        "L:en-US",
        "D:24",
        "PR:1",
        "S:390,844",
        "AS:390,844",
        "TO:-420",
        "SS:true",
        "LS:true",
        "IDB:true",
        "B:false",
        "ODB:false",
        "CPUC:unknown",
        "PK:iPhone",
        f"CFP:{random.randint(-2000000000, 2000000000)}",
        "FR:false",
        "FOS:false",
        "FB:false",
        "JSF:Arial",
        "P:",
        "T:0,false,false",
        "H:4",
        "SWF:false",
    ]
    fp = secrets.token_hex(16)
    # x64hash128 substitute — md5 of fe string is good enough for attempt
    ife = hashlib.md5(", ".join(fe).encode()).hexdigest()
    wh = secrets.token_hex(16) + "|" + secrets.token_hex(16)

    bda_obj = [
        {"key": "api_type", "value": "js"},
        {"key": "p", "value": 1},
        {"key": "f", "value": fp},
        {"key": "n", "value": base64.b64encode(str(int(ts)).encode()).decode()},
        {"key": "wh", "value": wh},
        {"key": "enhanced_fp", "value": []},
        {"key": "fe", "value": fe},
        {"key": "ife_hash", "value": ife},
        {"key": "jsbd", "value": json.dumps({"HL": 1, "NCE": True, "DT": "", "NWD": "false", "DMTO": 1, "DOTO": 1})},
    ]
    raw = json.dumps(bda_obj, separators=(",", ":"))
    enc = _aes_encrypt_bda(raw, key)
    return base64.b64encode(enc.encode()).decode()


def _nopecha_funcaptcha(task: str, image_b64: str, key: str = NOPECHA_KEY) -> Optional[list]:
    """Solve one FunCaptcha grid via NopeCHA Recognition API. Returns bool list len 6."""
    import urllib.request

    payload = json.dumps(
        {"key": key, "type": "funcaptcha", "task": task, "image_data": [image_b64]}
    ).encode()
    req = urllib.request.Request(
        "https://api.nopecha.com/",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    job = data.get("data")
    if not job or data.get("error"):
        return None

    for _ in range(40):
        time.sleep(1.0)
        q = urllib.parse.urlencode({"key": key, "id": job})
        with urllib.request.urlopen(f"https://api.nopecha.com/?{q}", timeout=30) as resp:
            out = json.loads(resp.read().decode())
        if out.get("error") == 14:
            continue
        if isinstance(out.get("data"), list):
            return out["data"]
        return None
    return None


def arkose_token_from_blob(sess, blob: str, user_agent: str) -> tuple[str, str]:
    """
    Fetch Arkose token for Roblox login blob.
    Returns (token, error). token empty on failure.
    """
    bda = _build_bda(user_agent)
    form = {
        "bda": bda,
        "public_key": ARKOSE_PK,
        "site": SITE,
        "userbrowser": user_agent,
        "capi_version": "2.11.6",
        "capi_mode": "inline",
        "style_theme": "default",
        "rnd": str(random.random()),
        "data[blob]": blob,
        "language": "en",
    }
    body = urllib.parse.urlencode(form)
    r = sess.post(
        f"{ARKOSE_SURL}/fc/gt2/public_key/{ARKOSE_PK}",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": ARKOSE_SURL,
            "Referer": f"{ARKOSE_SURL}/v2/{ARKOSE_PK}/1.5.5/enforcement.html",
            "User-Agent": user_agent,
            "Accept": "*/*",
        },
        timeout=45,
    )
    if r.status_code != 200:
        return "", f"arkose gt2 {r.status_code}"
    try:
        data = json.loads(r.text)
    except Exception:
        return "", "arkose gt2 bad json"

    token = data.get("token") or ""
    if not token:
        return "", f"arkose no token: {r.text[:200]}"

    # Only suppressed tokens (sup=1) are usable without solving a puzzle
    if "|sup=1|" in token:
        return token, ""

    # Visual challenge required — try NopeCHA recognition via Arkose game HTTP
    tok_err = _solve_visual_challenge(sess, token, user_agent)
    if tok_err:
        return "", tok_err
    return token, ""


def _solve_visual_challenge(sess, token: str, user_agent: str) -> str:
    """
    Attempt to clear Arkose visual rounds using NopeCHA.
    Returns "" on success (token mutated server-side), else error string.
    Note: Arkose game endpoints change often — best-effort.
    """
    # session token form: region.session|...
    session_token = token.split("|")[0] if "|" in token else token
    # Load challenge
    r = sess.get(
        f"{ARKOSE_SURL}/fc/gfct/",
        params={
            "token": token,
            "sid": session_token,
            "render_type": "canvas",
            "lang": "en",
            "isAudioGame": "false",
            "is_compatibility_mode": "false",
            "apiBreakerVersion": "green",
        },
        headers={"User-Agent": user_agent, "Referer": ARKOSE_SURL},
        timeout=30,
    )
    if r.status_code != 200:
        return f"gfct {r.status_code}: need CapSolver for visual captcha"
    try:
        game = json.loads(r.text)
    except Exception:
        return "gfct bad json"

    game_data = game.get("game_data") or {}
    waves = int(game_data.get("waves") or 1)
    instruction = (
        (game_data.get("instruction_string") or game_data.get("game_variant") or "Pick the correct image")
    )
    # Many modern Roblox challenges use encrypted customGUI — hard without breaker.
    # If we can't get a clear image URL, bail with clear message.
    gui = (game_data.get("customGUI") or {})
    img_url = None
    if isinstance(gui, dict):
        imgs = gui.get("_challenge_imgs") or gui.get("challenge_imgs") or []
        if imgs:
            img_url = imgs[0]

    if not img_url:
        return (
            "visual FunCaptcha (no CapSolver key). "
            "Set env CAPSOLVER_API_KEY for pure-API solve — NopeCHA has no FunCaptcha token API"
        )

    for wave in range(waves):
        ir = sess.get(img_url, headers={"User-Agent": user_agent}, timeout=30)
        if ir.status_code != 200:
            return f"image fetch {ir.status_code}"
        img_b64 = base64.b64encode(ir.content).decode()
        clicks = _nopecha_funcaptcha(instruction, img_b64)
        if not clicks:
            return "nopecha failed to solve grid"
        # index of True
        try:
            answer = clicks.index(True)
        except ValueError:
            return "nopecha returned no click"
        # Answer endpoint (legacy)
        ar = sess.post(
            f"{ARKOSE_SURL}/fc/ca/",
            data=urllib.parse.urlencode(
                {
                    "session_token": session_token,
                    "game_token": game.get("challengeID") or game.get("challenge_id") or "",
                    "sid": session_token,
                    "guess": json.dumps([answer]),
                }
            ),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": user_agent,
            },
            timeout=30,
        )
        if ar.status_code != 200:
            return f"answer {ar.status_code}"
    return ""
