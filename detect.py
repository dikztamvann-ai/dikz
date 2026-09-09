"""
detect.py — Deteksi status WhatsApp & ketersediaan OTP untuk sebuah nomor
===========================================================================
Port Python dari flow registrasi WhatsApp (iOS plaintext API, curve25519-js
Waves-XEdDSA variant — identik dengan test_exist.js).

Usage:
  python detect.py +6285757411154              # exist check + deteksi OTP
  python detect.py +6285757411154 --code sms   # request kirim OTP via /v2/code
  python detect.py +6285757411154 --code voice # request OTP via voice call
  python detect.py +6285757411154 --watch 60   # poll exist tiap 60 detik
  python detect.py +6285757411154 --json       # output JSON mentah

Referensi (otp.har):
  - GET /v2/exist  -> status akun + jendela OTP (sms_wait, voice_wait, dst)
  - GET /v2/code   -> minta OTP dikirim (butuh param token)
"""

import argparse
import hashlib
import json
import os
import random
import struct
import sys
import time

import requests

try:
    import phonenumbers
except ImportError:
    phonenumbers = None

# ---------------------------------------------------------------------------
# Curve25519 / Ed25519 math (RFC 8032 reference, extended coordinates)
# ---------------------------------------------------------------------------

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, P - 2, P)) % P
_I = pow(2, (P - 1) // 4, P)


def _inv(x):
    return pow(x, P - 2, P)


def _recover_x(y, sign):
    if y >= P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * _I % P
    if (x * x - x2) % P != 0:
        return None
    return x if (x & 1) == sign else P - x


_By = 4 * _inv(5) % P
_Bx = _recover_x(_By, 0)
B = (_Bx, _By, 1, _Bx * _By % P)


def _point_add(p1, p2):
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = 2 * t1 * t2 * _D % P
    d = 2 * z1 * z2 % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _point_mul(pt, n):
    q = (0, 1, 1, 0)
    while n > 0:
        if n & 1:
            q = _point_add(q, pt)
        pt = _point_add(pt, pt)
        n >>= 1
    return q


def _point_compress(pt):
    x, y, z, _ = pt
    zi = _inv(z)
    x = x * zi % P
    y = y * zi % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


# ---------------------------------------------------------------------------
# Key generation (identik dengan curve25519-js generateKeyPair + Waves sign)
# ---------------------------------------------------------------------------

def _clamp(sk: bytes) -> bytes:
    b = bytearray(sk)
    b[0] &= 248
    b[31] &= 127
    b[31] |= 64
    return bytes(b)


def _x25519_public(seed: bytes) -> bytes:
    """Public key dari X25519 (clamp internal, sama dgn curve25519-js)."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    priv = X25519PrivateKey.from_private_bytes(seed)
    return priv.public_key().public_bytes_raw()


def create_key_pair() -> dict:
    seed = os.urandom(32)
    return {"private": _clamp(seed), "public": _x25519_public(seed)}


def generate_signal_pub_key(pub: bytes) -> bytes:
    return pub if len(pub) == 33 else b"\x05" + pub


def xed25519_sign(sk: bytes, msg: bytes) -> bytes:
    """XEdDSA ala curve25519-js (crypto_sign_direct): bit sign x disimpan di
    byte ke-63 signature, r = SHA512(sk_clamped || msg), h = SHA512(R||A||msg)."""
    a = _clamp(sk)
    a_int = int.from_bytes(a, "little")
    aed = _point_compress(_point_mul(B, a_int))
    sign_bit = aed[31] & 128
    r = int.from_bytes(hashlib.sha512(a + msg).digest(), "little") % L
    r_enc = _point_compress(_point_mul(B, r))
    h = int.from_bytes(hashlib.sha512(r_enc + aed + msg).digest(), "little") % L
    s = (r + h * a_int) % L
    sig = bytearray(r_enc + s.to_bytes(32, "little"))
    sig[63] |= sign_bit
    return bytes(sig)


def signed_key_pair(identity: dict) -> dict:
    pre = create_key_pair()
    pub = generate_signal_pub_key(pre["public"])
    return {"keyPair": pre, "signature": xed25519_sign(identity["private"], pub)}


# ---------------------------------------------------------------------------
# Nomor telepon
# ---------------------------------------------------------------------------

def parse_number(number: str):
    """Return (cc, in) atau None kalau tidak valid."""
    digits = "".join(c for c in number if c.isdigit())
    if not digits:
        return None
    if phonenumbers is not None:
        parsed = phonenumbers.parse("+" + digits)
        if not phonenumbers.is_valid_number(parsed):
            return None
        return str(parsed.country_code), str(parsed.national_number)
    return digits[:2], digits[2:]  # fallback kasar


# ---------------------------------------------------------------------------
# WhatsApp registration API
# ---------------------------------------------------------------------------

UA = "WhatsApp/2.26.23.74 iOS/17.5.1 Device/Apple-iPhone_13"
BASE = "https://v.whatsapp.net/v2"
TOKEN_SALT = "Pd0As4oDTQ9KOPzY"  # salt token historis API registrasi


def _b64url(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _url_hex(b: bytes) -> str:
    return "".join("%%%02x" % x for x in b)


def _common_params(cc: str, ind: str) -> dict:
    identity = create_key_pair()
    noise = create_key_pair()
    spk = signed_key_pair(identity)

    reg_id = struct.pack(">I", struct.unpack(">H", os.urandom(2))[0] & 16383)
    skey_id = b"\x00" + struct.pack(">H", 1)

    return {
        "cc": cc,
        "in": ind,
        "lg": "en",
        "lc": "GB",
        "authkey": _b64url(noise["public"]),
        "e_regid": _b64url(reg_id),
        "e_keytype": "BQ",
        "e_ident": _b64url(identity["public"]),
        "e_skey_id": _b64url(skey_id),
        "e_skey_val": _b64url(spk["keyPair"]["public"]),
        "e_skey_sig": _b64url(spk["signature"]),
        "id": _url_hex(os.urandom(20)),
    }


def _get(path: str, params: dict) -> dict:
    """GET dengan query string dibangun manual (persis ala versi JS) supaya
    nilai yang sudah mengandung %xx (param id) tidak ter-encode ganda."""
    qs = "&".join(k + "=" + v for k, v in params.items())
    r = requests.get(f"{BASE}/{path}?{qs}", headers={"User-Agent": UA}, timeout=30)
    return r.json()


def check_exist(number: str) -> dict:
    parsed = parse_number(number)
    if parsed is None:
        return {"status": "fail", "reason": "invalid_number"}
    cc, ind = parsed
    return _get("exist", _common_params(cc, ind))


def check_code(number: str, method: str = "sms", mcc: str = "510", mnc: str = "000") -> dict:
    parsed = parse_number(number)
    if parsed is None:
        return {"status": "fail", "reason": "invalid_number"}
    cc, ind = parsed
    full = cc + ind
    params = _common_params(cc, ind)
    params.update({
        "to": full,
        "method": method,
        "mcc": mcc,
        "mnc": mnc,
        "token": hashlib.md5((full + TOKEN_SALT).encode()).hexdigest().upper(),
    })
    return _get("code", params)


# ---------------------------------------------------------------------------
# Presentasi hasil
# ---------------------------------------------------------------------------

def fmt_wait(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    if seconds <= 0:
        return "bisa sekarang (0s)"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} jam")
    if m:
        parts.append(f"{m} menit")
    if s and not h:
        parts.append(f"{s} detik")
    return " + ".join(parts) + f" ({seconds}s)"


def detect_otp(res: dict) -> dict:
    """Ekstrak informasi deteksi OTP dari respons exist/code."""
    out = {
        "login": res.get("login"),
        "status": res.get("status"),
        "reason": res.get("reason"),
    }
    for key in ("sms_wait", "voice_wait", "flash_wait", "email_otp_wait",
                "send_sms_wait", "wa_old_wait", "retry_after"):
        if key in res:
            out[key] = res[key]
    if "fallback_methods" in res:
        out["fallback_methods"] = res["fallback_methods"]
    return out


def print_exist(res: dict) -> None:
    print("=" * 56)
    print(" DETECT OTP — /v2/exist")
    print("=" * 56)
    print(f"  Nomor        : +{res.get('login', '?')}")
    registered = res.get("reason") == "incorrect"
    if res.get("status") == "ok":
        registered = True
    print(f"  Akun WA      : {'TERDAFTAR (akun eksis)' if registered else 'Belum terdaftar / ' + str(res.get('reason'))}")
    print(f"  Status       : {res.get('status')} ({res.get('reason')})")
    if "sms_wait" in res:
        print(f"  OTP SMS      : {fmt_wait(res.get('sms_wait'))} — panjang kode {res.get('sms_length', '?')} digit")
        print(f"  OTP Voice    : {fmt_wait(res.get('voice_wait'))} — panjang kode {res.get('voice_length', '?')} digit")
        print(f"  Flash call   : {fmt_wait(res.get('flash_wait'))} (type {res.get('flash_type')})")
        print(f"  Email OTP    : {fmt_wait(res.get('email_otp_wait'))} (eligible {res.get('email_otp_eligible')})")
        fb = res.get("fallback_methods") or []
        if fb:
            print(f"  Fallback     : {', '.join(fb)}")
    print("=" * 56)


def print_code(res: dict) -> None:
    print("=" * 56)
    print(" REQUEST OTP — /v2/code")
    print("=" * 56)
    print(f"  Nomor        : +{res.get('login', '?')}")
    reason = res.get("reason")
    print(f"  Status       : {res.get('status')} ({reason})")
    if reason == "sent":
        print("  >>> OTP TERKIRIM! Cek SMS di nomor tersebut.")
    elif reason == "no_routes":
        print(f"  >>> Route SMS/voice belum tersedia untuk nomor ini.")
        print(f"      Retry setelah: {fmt_wait(res.get('retry_after') or res.get('sms_wait'))}")
    elif reason == "too_recent" or reason == "too_many_guesses":
        print(f"  >>> Terlalu sering. Tunggu: {fmt_wait(res.get('retry_after') or res.get('sms_wait'))}")
    for key in ("sms_wait", "voice_wait", "flash_wait", "email_otp_wait",
                "send_sms_wait", "wa_old_wait", "retry_after"):
        if key in res:
            print(f"  {key:<20}: {fmt_wait(res.get(key))}")
    print("=" * 56)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Deteksi status WA & OTP untuk nomor")
    ap.add_argument("number", help="nomor, contoh +6285757411154")
    ap.add_argument("--code", choices=["sms", "voice"], metavar="METHOD",
                    help="request OTP via /v2/code (sms/voice)")
    ap.add_argument("--mcc", default="510", help="mcc device (default 510)")
    ap.add_argument("--mnc", default="000", help="mnc device (default 000)")
    ap.add_argument("--watch", type=int, metavar="SECONDS",
                    help="poll exist berulang tiap N detik sampai sms_wait habis")
    ap.add_argument("--json", action="store_true", help="output JSON mentah")
    args = ap.parse_args()

    if args.code:
        res = check_code(args.number, args.code, args.mcc, args.mnc)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print_code(res)
        return 0

    if args.watch:
        print(f"[watch] polling exist tiap {args.watch}s — Ctrl+C untuk stop")
        while True:
            res = check_exist(args.number)
            sms_wait = int(res.get("sms_wait") or 0)
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] sms_wait={sms_wait}s reason={res.get('reason')} status={res.get('status')}")
            if args.json:
                print(json.dumps(res, indent=2))
            if sms_wait <= 0:
                print("[watch] OTP SMS bisa diminta sekarang!")
                print_exist(res)
                return 0
            time.sleep(args.watch)
        return 0

    res = check_exist(args.number)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print_exist(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
