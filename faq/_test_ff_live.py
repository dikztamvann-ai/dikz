# -*- coding: utf-8 -*-
"""
Free Fire LIVE API Test — replay HAR requests with real token
=============================================================
Endpoint: clientbp.ggpolarbear.com (internal FF API, protobuf)
Target: UID 2167058509
"""
import base64
import json
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests as req

HAR_PATH = Path("e:/ivas/roblox/epep.har")
TARGET_UID = 1654006463
BASE = "https://clientbp.ggpolarbear.com"

# ─── AES encryption (same key as FF client) ──────────────────────────────────
AES_KEY = b'Yg&tc%DEuh6%Zc^8'
AES_IV  = b'6oyZDr22E3ychjM%'

def aes_encrypt(plaintext: bytes) -> bytes:
    from Crypto.Cipher import AES as _AES
    pad_len = _AES.block_size - (len(plaintext) % _AES.block_size)
    padded = plaintext + bytes([pad_len] * pad_len)
    cipher = _AES.new(AES_KEY, _AES.MODE_CBC, AES_IV)
    return cipher.encrypt(padded)

# ─── Protobuf minimal encoder/decoder ────────────────────────────────────────

def encode_varint(value):
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)

def read_varint(data, pos):
    value = shift = 0
    while pos < len(data):
        b = data[pos]; pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80): return value, pos
        shift += 7
    raise ValueError("truncated varint")

def decode_proto(data):
    """Decode protobuf bytes → list of (field_num, wire_type, value)."""
    fields = []
    pos = 0
    while pos < len(data):
        key, pos = read_varint(data, pos)
        fnum, wtype = key >> 3, key & 7
        if wtype == 0:
            val, pos = read_varint(data, pos)
        elif wtype == 1:
            val = data[pos:pos+8]; pos += 8
        elif wtype == 2:
            length, pos = read_varint(data, pos)
            val = data[pos:pos+length]; pos += length
        elif wtype == 5:
            val = data[pos:pos+4]; pos += 4
        else:
            break
        fields.append((fnum, wtype, val))
    return fields

def proto_to_dict(data, depth=0):
    """Best-effort protobuf → dict conversion."""
    if depth > 5: return {"_hex": data[:32].hex()}
    try:
        fields = decode_proto(data)
    except:
        return {"_raw": data[:32].hex()}
    result = {}
    for fnum, wtype, val in fields:
        key = str(fnum)
        if wtype == 0:
            parsed = val
        elif wtype in (1, 5):
            parsed = int.from_bytes(val, "little")
        elif wtype == 2:
            # try string
            try:
                s = val.decode("utf-8")
                if all(c.isprintable() or c in "\r\n\t" for c in s):
                    parsed = s
                else:
                    parsed = proto_to_dict(val, depth+1)
            except:
                parsed = proto_to_dict(val, depth+1)
        else:
            parsed = val.hex()
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(parsed)
        else:
            result[key] = parsed
    return result

# ─── Extract token + headers from HAR ────────────────────────────────────────

def load_har_auth():
    """Get full auth headers from HAR entry."""
    har = json.loads(HAR_PATH.read_text(encoding="utf-8"))
    for entry in har["log"]["entries"]:
        r = entry["request"]
        if "clientbp.ggpolarbear.com" in r.get("url", ""):
            headers = {}
            for h in r.get("headers", []):
                name = h["name"]
                if name.startswith(":"):
                    continue
                headers[name] = h["value"]
            if "Authorization" in headers or "authorization" in headers:
                return headers
    return None

# ─── Build protobuf request body ─────────────────────────────────────────────

def make_uid_request(uid):
    """Encode simple protobuf: field 1 = uint64 uid, then AES encrypt."""
    proto = encode_varint((1 << 3) | 0) + encode_varint(uid)
    return aes_encrypt(proto)

def make_stats_request(uid, matchmode=0):
    """Encode: field 1 = uid, field 2 = matchmode, then AES encrypt."""
    proto = encode_varint((1 << 3) | 0) + encode_varint(uid)
    if matchmode:
        proto += encode_varint((2 << 3) | 0) + encode_varint(matchmode)
    return aes_encrypt(proto)

def make_personal_show_request(uid, call_sign_src=7):
    """Encode: field 1 = uid, field 2 = callSignSrc, then AES encrypt."""
    proto = encode_varint((1 << 3) | 0) + encode_varint(uid)
    proto += encode_varint((2 << 3) | 0) + encode_varint(call_sign_src)
    return aes_encrypt(proto)

# ─── API caller ──────────────────────────────────────────────────────────────

def call_ff_api(endpoint, body_bytes, headers):
    """POST to FF API with AES-encrypted protobuf body, return decoded response."""
    url = f"{BASE}/{endpoint}"
    h = dict(headers)
    h["Content-Type"] = "application/x-www-form-urlencoded"
    h["Accept"] = "*/*"
    h["x-unity-version"] = "2022.3.47f1"
    h["releaseversion"] = "OB54"
    h["x-ga"] = "v1 1"
    h["User-Agent"] = "Free%20Fire%20MAX/2019118039 CFNetwork/3888.100.1 Darwin/27.0.0"

    r = req.post(url, data=body_bytes, headers=h, timeout=30)
    print(f"  [{r.status_code}] {endpoint} response={len(r.content)}b")

    if r.status_code != 200:
        print(f"  ERROR: {r.text[:200] if r.text else r.content[:100].hex()}")
        return None

    if len(r.content) == 0:
        return {}

    try:
        return proto_to_dict(r.content)
    except Exception as e:
        print(f"  Decode error: {e}")
        return {"_raw_hex": r.content[:100].hex()}

# ─── Item lookup (reuse from test script) ────────────────────────────────────

ITEM_DATA_URL = "https://raw.githubusercontent.com/jinix6/ItemID/main/assets/itemData.json"
ICON_CDN = "https://raw.githubusercontent.com/ashqking/FF-Items/main/ICONS/{}.png"
_DB = None

def load_items():
    global _DB
    if _DB is not None: return _DB
    try:
        r = req.get(ITEM_DATA_URL, timeout=30)
        _DB = {str(it["itemID"]): it.get("description","?") for it in r.json()}
        print(f"[*] Item DB: {len(_DB)} items")
    except:
        _DB = {}
    return _DB

def iname(iid):
    db = load_items()
    return db.get(str(iid), f"#{iid}")

# ─── Rank names ──────────────────────────────────────────────────────────────
RANKS = {
    301:"Bronze I",302:"Bronze II",303:"Bronze III",
    304:"Silver I",305:"Silver II",306:"Silver III",
    307:"Gold I",308:"Gold II",309:"Gold III",310:"Gold IV",
    311:"Platinum I",312:"Platinum II",313:"Platinum III",314:"Platinum IV",
    315:"Diamond I",316:"Diamond II",317:"Diamond III",318:"Diamond IV",
    319:"Heroic I",320:"Heroic II",321:"Heroic III",
    322:"Master",323:"Grandmaster I",324:"Grandmaster II",
    325:"Grandmaster III",326:"Grandmaster",
}
def rk(c): return RANKS.get(c, f"Rank#{c}") if isinstance(c, int) else str(c)
def ts(v):
    try: return datetime.fromtimestamp(int(v), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except: return str(v)

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else TARGET_UID
    print(f"[+] Free Fire Live API Test — UID {uid}")
    print(f"[+] Server: {BASE}")

    # Load auth
    headers = load_har_auth()
    if not headers:
        print("[!] No auth headers found in HAR!")
        return

    token = headers.get("Authorization", headers.get("authorization", ""))
    # Decode JWT to check expiry
    try:
        parts = token.replace("Bearer ", "").split(".")
        pad = parts[1] + "=" * (4 - len(parts[1]) % 4)
        jwt = json.loads(base64.urlsafe_b64decode(pad))
        exp = jwt.get("exp", 0)
        remaining = exp - int(time.time())
        owner = jwt.get("account_id")
        print(f"[+] Token owner: UID {owner}")
        print(f"[+] Token expires: {ts(exp)} ({remaining/3600:.1f}h remaining)")
        if remaining <= 0:
            print("[!] TOKEN EXPIRED!")
            return
    except Exception as e:
        print(f"[!] JWT decode warn: {e}")

    load_items()

    # ── 1) GetAccountInfoByAccountID ──
    print(f"\n{'='*60}")
    print(f"  1) GetAccountInfoByAccountID")
    print(f"{'='*60}")
    body = make_uid_request(uid)
    data = call_ff_api("GetAccountInfoByAccountID", body, headers)
    if data:
        print(f"\n  Nickname     : {data.get('3', '?')}")
        print(f"  Region       : {data.get('5', '?')}")
        print(f"  Level        : {data.get('6', '?')}")
        print(f"  EXP          : {data.get('7', '?')}")
        print(f"  Likes        : {data.get('21', '?')}")
        print(f"  BR Rank      : {rk(data.get('14'))} ({data.get('15','?')} pts)")
        print(f"  CS Rank      : {rk(data.get('30'))} ({data.get('31','?')} pts)")
        print(f"  Head Pic     : {iname(data.get('12',''))} [{data.get('12')}]")
        print(f"  Clan Name    : {data.get('13', '?')}")
        print(f"  Season       : {data.get('20', '?')}")
        print(f"  Created      : {ts(data.get('44', 0))}")
        print(f"  Last Login   : {ts(data.get('24', 0))}")
        print(f"  Version      : {data.get('50', '?')}")
        print(f"  PIN          : {iname(data.get('33',''))} [{data.get('33')}]")

    # ── 2) GetPlayerPersonalShow ──
    print(f"\n{'='*60}")
    print(f"  2) GetPlayerPersonalShow")
    print(f"{'='*60}")
    body2 = make_personal_show_request(uid)
    data2 = call_ff_api("GetPlayerPersonalShow", body2, headers)
    if data2:
        # basicinfo is field 1
        basic = data2.get("1", {})
        profile = data2.get("2", {})
        clan = data2.get("6", {})
        captain = data2.get("7", {})
        pet = data2.get("8", {})
        social = data2.get("9", {})
        diamond = data2.get("10", {})
        credit = data2.get("11", {})

        if isinstance(basic, dict):
            print(f"\n  Nickname     : {basic.get('3', '?')}")
            print(f"  Level        : {basic.get('6', '?')}")
            print(f"  Likes        : {basic.get('21', '?')}")

        if isinstance(pet, dict):
            pid = pet.get("1")
            print(f"\n  Pet          : {iname(pid)} [{pid}]")
            print(f"  Pet Level    : {pet.get('3', '?')}")
            print(f"  Pet Skin     : {iname(pet.get('6',''))} [{pet.get('6')}]")
            print(f"  Pet Skill    : {iname(pet.get('9',''))} [{pet.get('9')}]")

        if isinstance(social, dict):
            print(f"\n  Bio          : {social.get('9', '?')}")

        if isinstance(credit, dict):
            print(f"  Credit Score : {credit.get('1', '?')}")

        if isinstance(diamond, dict):
            print(f"  Diamond Cost : {diamond.get('1', '?')}")

        if isinstance(clan, dict):
            print(f"\n  Clan         : {clan.get('2', '?')} (Lv{clan.get('4','?')}, {clan.get('6','?')}/{clan.get('5','?')})")

        if isinstance(captain, dict):
            print(f"  Captain      : {captain.get('3', '?')} (UID {captain.get('1', '?')})")

    # ── 3) GetPlayerStats ──
    print(f"\n{'='*60}")
    print(f"  3) GetPlayerStats (BR)")
    print(f"{'='*60}")
    body3 = make_stats_request(uid, matchmode=0)
    stats = call_ff_api("GetPlayerStats", body3, headers)
    if stats:
        for mode_key, mode_name in [("1","Solo"), ("2","Duo"), ("3","Squad")]:
            ms = stats.get(mode_key, {})
            if isinstance(ms, dict):
                games = ms.get("2", 0)
                wins = ms.get("3", 0)
                kills = ms.get("4", 0)
                det = ms.get("5", {})
                deaths = det.get("1", 0) if isinstance(det, dict) else 0
                kd = kills/deaths if deaths else 0
                wr = wins/games*100 if games else 0
                hs = det.get("11", 0) if isinstance(det, dict) else 0
                print(f"  {mode_name:6} : {games} games | {wins} wins ({wr:.1f}%) | {kills} kills | K/D {kd:.2f} | HS kills {hs}")

    # ── 4) GetPlayerGalleryShowInfo ──
    print(f"\n{'='*60}")
    print(f"  4) GetPlayerGalleryShowInfo")
    print(f"{'='*60}")
    body4 = make_uid_request(uid)
    gallery = call_ff_api("GetPlayerGalleryShowInfo", body4, headers)
    if gallery:
        print(f"  Gallery data: {json.dumps(gallery, indent=2, ensure_ascii=False, default=str)[:500]}")

    # ── 5) GetWorkshopAuthorInfo ──
    print(f"\n{'='*60}")
    print(f"  5) GetWorkshopAuthorInfo")
    print(f"{'='*60}")
    body5 = make_uid_request(uid)
    workshop = call_ff_api("GetWorkshopAuthorInfo", body5, headers)
    if workshop:
        print(f"  Workshop EXP : {workshop}")

    # ── 6) GetFriend (friend list) ──
    print(f"\n{'='*60}")
    print(f"  6) GetFriend (friend count)")
    print(f"{'='*60}")
    body6 = make_uid_request(uid)
    friends = call_ff_api("GetFriend", body6, headers)
    if friends:
        fl = friends.get("1", [])
        if isinstance(fl, list):
            print(f"  Friends: {len(fl)} players")
        elif isinstance(fl, dict):
            print(f"  Friends: 1+ (single entry)")
        else:
            print(f"  Friends data: {str(friends)[:200]}")

    # ── 7) FB profile pic ──
    print(f"\n{'='*60}")
    print(f"  7) Facebook Profile Pic (public)")
    print(f"{'='*60}")
    fb_url = "https://graph.facebook.com/v24.0/201734289499465/picture?width=160&height=160"
    try:
        r = req.get(fb_url, timeout=15, allow_redirects=False)
        redirect = r.headers.get("Location", "no redirect")
        print(f"  Status: {r.status_code}")
        print(f"  Redirect: {redirect[:150]}")
    except Exception as e:
        print(f"  Error: {e}")

    # ── Summary ──
    print(f"\n{'='*60}")
    print(f"  ✅ ALL API CALLS COMPLETE")
    print(f"{'='*60}")

    # Save raw responses
    out = {
        "uid": uid,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "account_info": data,
        "personal_show": data2,
        "stats": stats,
        "gallery": gallery,
        "workshop": workshop,
    }
    outpath = Path("e:/ivas/roblox") / f"ff_live_{uid}.json"
    outpath.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\n[+] Saved raw data → {outpath}")


if __name__ == "__main__":
    main()
