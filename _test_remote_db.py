"""Tes logika remote-first tanpa menyentuh Worker asli.

Menjalankan _web_read/_web_write dik.py terhadap Worker tiruan (fake) yang
meniru jawaban /api/read dan /api/write, plus kasus remote mati supaya
jalur fallback ke SQLite lokal ikut terverifikasi.
"""
import json
import sys
import types

# ── Stub minimal supaya blok fungsi dik.py bisa dijalankan sendiri ──
import logging
import threading
import time
from datetime import datetime, timedelta

logging.basicConfig(level=logging.CRITICAL)

WEB_SYNC_URL = "https://fake.workers.dev"
WEB_SYNC_KEY = "testkey"
WEB_SYNC_TIMEOUT = 8
WEB_READ_TIMEOUT = 6
WEB_DB_PRIMARY = True
WEB_FAIL_COOLDOWN = 2
RESETOTP_TOKEN_COST = 2
RESETOTP_TRIAL_LIMIT = 1


class FakeResp:
    def __init__(self, code, payload):
        self.status_code = code
        self._p = payload

    def json(self):
        return self._p


class FakeRequests:
    """Worker tiruan: menyimpan state di memori, meniru guard atomik D1."""

    def __init__(self):
        self.down = False
        self.tokens = {}     # user_id -> dict
        self.trial = {}      # user_id -> dict
        self.premium = {}    # user_id -> dict
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", params.get("table")))
        if self.down:
            raise OSError("connection refused")
        assert headers.get("x-sync-key") == WEB_SYNC_KEY, "auth header hilang"
        t, uid = params["table"], params.get("user_id")
        store = {"premium_tokens": self.tokens, "resetotp_trial": self.trial,
                 "premium_access": self.premium}[t]
        if uid is not None:
            return FakeResp(200, {"ok": True, "row": store.get(int(uid))})
        return FakeResp(200, {"ok": True, "rows": list(store.values())})

    def post(self, url, json=None, headers=None, timeout=None):
        body = json
        self.calls.append(("POST", body.get("action")))
        if self.down:
            raise OSError("connection refused")
        assert headers.get("x-sync-key") == WEB_SYNC_KEY, "auth header hilang"
        a, uid = body["action"], int(body["user_id"])

        if a == "add_tokens":
            row = self.tokens.setdefault(uid, {
                "user_id": uid, "balance": 0, "total_bought": 0,
                "total_spent": 0, "total_refund": 0, "updated_at": "now"})
            row["balance"] += body["amount"]
            row["total_bought"] += body["amount"]
            return FakeResp(200, {"ok": True, "balance": row["balance"], "row": row})

        if a == "spend_tokens":
            row = self.tokens.get(uid)
            if not row or row["balance"] < body["amount"]:
                return FakeResp(200, {"ok": False, "error": "insufficient",
                                      "balance": (row or {}).get("balance", 0),
                                      "row": row})
            row["balance"] -= body["amount"]
            row["total_spent"] += body["amount"]
            return FakeResp(200, {"ok": True, "balance": row["balance"], "row": row})

        if a == "reserve_trial":
            row = self.trial.setdefault(uid, {
                "user_id": uid, "used": 0, "first_used_at": "now", "last_used_at": "now"})
            if row["used"] >= body["limit"]:
                return FakeResp(200, {"ok": False, "error": "limit_reached", "row": row})
            row["used"] += 1
            return FakeResp(200, {"ok": True, "row": row})

        if a == "refund_trial":
            row = self.trial.setdefault(uid, {"user_id": uid, "used": 0})
            row["used"] = max(0, row["used"] - 1)
            return FakeResp(200, {"ok": True, "row": row})

        if a == "grant_premium":
            exp = (datetime.utcnow() + timedelta(days=body["days"])
                   ).strftime("%Y-%m-%d %H:%M:%S")
            row = self.premium.setdefault(uid, {"user_id": uid, "buy_count": 0,
                                                "total_paid": 0})
            row.update({"expired_at": exp, "package": body["package"],
                        "days": body["days"], "updated_at": "now"})
            row["buy_count"] += 1
            row["total_paid"] += body.get("amount") or 0
            return FakeResp(200, {"ok": True, "expired_at": exp, "row": row})

        return FakeResp(400, {"ok": False, "error": "unknown action"})


requests = FakeRequests()

# ── Ambil blok fungsi web dari dik.py yang asli ──
src = open("dik.py", encoding="utf-8").read()
start = src.index("_web_enabled = bool(WEB_SYNC_URL)")
end = src.index("def _parse_iso(s):")
block = src[start:end]
# _cache_local butuh cur/conn — pakai stub no-op (fokus tes jalur remote).
cached = []


class _NoDB:
    def execute(self, *a, **k):
        cached.append(a[0].split()[2] if len(a) else "?")

    def commit(self):
        pass

    def fetchone(self):
        return None


cur = conn = _NoDB()
exec(compile(block, "dik.py:webblock", "exec"), globals())

# ── Jalankan tes ──
U = 12345
fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'OK ' if ok else 'GAGAL'} {label}: {got!r}" + ("" if ok else f" (harap {want!r})"))
    if not ok:
        fails.append(label)


print("\n[1] Remote hidup — tulis & baca")
r = _web_write("add_tokens", {"user_id": U, "amount": 10, "kind": "buy"})
check("add_tokens saldo", r["balance"], 10)
r = _web_read("premium_tokens", {"user_id": U})
check("read balance", r["row"]["balance"], 10)

print("\n[2] spend_tokens atomik")
check("spend 4 -> ok", _web_write("spend_tokens", {"user_id": U, "amount": 4})["ok"], True)
check("saldo sisa", requests.tokens[U]["balance"], 6)
r = _web_write("spend_tokens", {"user_id": U, "amount": 99})
check("spend 99 -> tolak", r["ok"], False)
check("alasan", r["error"], "insufficient")
check("saldo tak minus", requests.tokens[U]["balance"], 6)

print("\n[3] reserve_trial tidak boleh lewat limit")
check("reserve #1", _web_write("reserve_trial", {"user_id": U, "limit": 1})["ok"], True)
r = _web_write("reserve_trial", {"user_id": U, "limit": 1})
check("reserve #2 ditolak", r["error"], "limit_reached")
check("used tetap 1", requests.trial[U]["used"], 1)
check("refund_trial", _web_write("refund_trial", {"user_id": U})["ok"], True)
check("used balik 0", requests.trial[U]["used"], 0)

print("\n[4] grant_premium")
r = _web_write("grant_premium", {"user_id": U, "days": 7, "package": "p7", "amount": 40000})
check("grant ok", r["ok"], True)
check("buy_count", requests.premium[U]["buy_count"], 1)
r = _web_read("premium_access", {"user_id": U})
check("premium terbaca", r["row"]["package"], "p7")

print("\n[5] Remote mati -> return None (pemanggil jatuh ke lokal)")
requests.down = True
_web_mark_up()
check("read saat mati", _web_read("premium_tokens", {"user_id": U}), None)
check("write saat mati", _web_write("spend_tokens", {"user_id": U, "amount": 1}), None)
check("cooldown aktif", _web_ready(), False)
n_before = len(requests.calls)
_web_read("premium_tokens", {"user_id": U})
check("skip saat cooldown (tanpa request baru)", len(requests.calls), n_before)

print("\n[6] Remote hidup lagi setelah cooldown")
requests.down = False
time.sleep(WEB_FAIL_COOLDOWN + 0.2)
r = _web_read("premium_tokens", {"user_id": U})
check("read pulih", r["row"]["balance"], 6)
check("cooldown mati", _web_ready(), True)

print("\n[7] Dump seluruh tabel (dipakai startup restore)")
r = _web_read("premium_access", {"limit": 5000})
check("rows premium_access", len(r["rows"]), 1)

print("\n" + "=" * 50)
if fails:
    print(f"ADA {len(fails)} TES GAGAL: {fails}")
    sys.exit(1)
print("SEMUA TES LULUS")
