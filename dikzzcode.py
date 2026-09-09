#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DikZzCode BOT — bot mini 3 fitur.

Fitur:
  1. Fix Server 1   (sender pool partisi S1 = 150 mailbox id terkecil)
  2. Fix Server 2   (sender pool partisi S2 = sisanya)
  3. Reset OTP      (banding + Form V1 + Form V2 paralel)

Database SAMA dengan bot utama (ivas_bot.db). Sender email diambil dari tabel
sitepro_mailboxes. Akses/token/trial memakai model yang sama:
  premium (unlimited) -> token (2/run, refund kalau gagal) -> trial (1x).

Pipeline dipinjam dari modul yang sudah ada:
  - sitepro_fix_module.run_fix_multi_pipeline  (Fix Server 1/2)
  - faq_server.run_reset_otp_pipeline          (Reset OTP)
"""
import os
import re
import time
import random
import asyncio
import logging
import sqlite3
import threading
import html as _html
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

import httpx
import requests
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

# ── Pipeline yang dipinjam dari modul utama ──────────────────────────
from sitepro_fix_module import run_fix_multi_pipeline, MAX_SENDS_PER_ACCOUNT
# run_reset_otp_pipeline di-import lazy di dalam handler (sama seperti dik.py)

# ══════════════════════════════════════════════════════════════════════
#  KONFIGURASI
# ══════════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = "8912378621:AAHn0zQipO8ekJUrKnLsnEd3RstLztIeeho"  # token tes
USER_ID = 6446678808                 # owner
BOT_USERNAME = "maklohytam_bot"      # dipakai di footer log grup (auto-update saat start)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ivas_bot.db")
BANNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "start_banner.jpg")
FIX_LOG_GROUP_ID = -1004470331481    # grup monitoring (sama dgn bot utama)
_FIX_LOG_WARNED = False              # True setelah 1x gagal "chat not found"

CAPTCHA_API_KEY = ""
CAPTCHA_SOLVER = "browser"

# Biaya & batas (samakan dgn bot utama)
RESETOTP_TOKEN_COST = 2
RESETOTP_TRIAL_LIMIT = 1
MAX_FIX_NUMBERS = 5

# Database utama ada di web (Cloudflare Worker + D1) — sama dgn bot utama.
# Bot baca lewat GET /api/read dan tulis operasi atomik lewat POST /api/write;
# SQLite lokal hanya cache supaya bot tetap jalan kalau Worker tak terjangkau.
WEB_SYNC_URL = "https://database-ivas.web-ivas.workers.dev"
WEB_SYNC_KEY = "02f6c407b324dc167171898ab2d887a17c704d398712605a"
WEB_SYNC_TIMEOUT = 8
WEB_READ_TIMEOUT = 6
WEB_DB_PRIMARY = True
WEB_FAIL_COOLDOWN = 30

# ── Payment gateway (MG Cloud Pay / QRIS) — sama dgn bot utama ──
MGPAY_API_KEY = "mgcloudpay_691513705d4e4c9f"
MGPAY_BASE    = "https://app.mgcloudpay.my.id"
MGPAY_TIMEOUT = 20
PREMIUM_INVOICE_TTL_MIN = 15

# Paket premium/token — identik dgn bot utama.
PREMIUM_DAILY_BASE = 7000
PREMIUM_PACKAGES = {
    "p1":  ("1 Hari",    7000,  1),
    "p3":  ("3 Hari",   18000,  3),
    "p7":  ("7 Hari",   40000,  7),
    "p30": ("30 Hari", 130000, 30),
}
PREMIUM_ORDER = ("p1", "p3", "p7", "p30")
TOKEN_PACKAGES = {
    "t15":  ("15 Token",   5000,  15,  0),
    "t35":  ("35 Token",  10000,  35,  5),
    "t60":  ("60 Token",  15000,  60, 15),
    "t150": ("150 Token", 30000, 150, 60),
}
TOKEN_ORDER = ("t15", "t35", "t60", "t150")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ══════════════════════════════════════════════════════════════════════
#  DATABASE  (thread-local, sama pola dgn dik.py)
# ══════════════════════════════════════════════════════════════════════
class ThreadLocalConnectionProxy:
    """Koneksi sqlite3 per-thread (aman dipakai dari ThreadPoolExecutor)."""
    def __init__(self, path):
        self._path = path
        self._local = threading.local()

    def _get_conn(self):
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self._path, timeout=30, check_same_thread=False)
            self._local.cur = self._local.conn.cursor()
        return self._local.conn

    def _get_cur(self):
        self._get_conn()
        return self._local.cur

    def commit(self):
        self._get_conn().commit()

    def rollback(self):
        self._get_conn().rollback()

    def cursor(self):
        return self

    def execute(self, *a, **k):
        return self._get_cur().execute(*a, **k)

    def executemany(self, *a, **k):
        return self._get_cur().executemany(*a, **k)

    def fetchone(self, *a, **k):
        return self._get_cur().fetchone(*a, **k)

    def fetchall(self, *a, **k):
        return self._get_cur().fetchall(*a, **k)

    @property
    def description(self):
        return self._get_cur().description

    @property
    def rowcount(self):
        return self._get_cur().rowcount

    @property
    def lastrowid(self):
        return self._get_cur().lastrowid


db_proxy = ThreadLocalConnectionProxy(DB_PATH)
conn = db_proxy
cur = db_proxy

# Pool global buat pipeline (agar event loop tidak ke-block)
_GLOBAL_POOL = ThreadPoolExecutor(max_workers=12, thread_name_prefix="dikzz")

# Batas task paralel per user
_active_locks = {}
_active_lock = threading.Lock()
MAX_CONCURRENT = 3

# Maintenance toggle (in-memory)
MAINTENANCE_MODE = False


def _can_start(user_id):
    with _active_lock:
        cur_n = _active_locks.get(user_id, 0)
        if user_id == USER_ID:
            _active_locks[user_id] = cur_n + 1
            return True, None
        if cur_n >= MAX_CONCURRENT:
            return False, f"Kamu punya {cur_n} task aktif (maks {MAX_CONCURRENT}). Tunggu selesai dulu."
        _active_locks[user_id] = cur_n + 1
        return True, None


def _release(user_id):
    with _active_lock:
        n = _active_locks.get(user_id, 0)
        _active_locks[user_id] = max(0, n - 1)


def _now_utc():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _parse_iso(s):
    if not s:
        return None
    try:
        s = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _ensure_tables():
    """Pastikan tabel yang dipakai ada (bot utama sudah bikin, ini jaga-jaga)."""
    stmts = [
        """CREATE TABLE IF NOT EXISTS allowed_users (
            user_id INTEGER PRIMARY KEY, username TEXT, added_by INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)""",
        """CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY, username TEXT, banned_by INTEGER,
            reason TEXT DEFAULT '', banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS fix_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            total INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'running',
            sent_count INTEGER DEFAULT 0, reply_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS fix_run_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
            phone TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            detail TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, phone))""",
        """CREATE TABLE IF NOT EXISTS premium_access (
            user_id INTEGER PRIMARY KEY, expired_at TEXT NOT NULL, package TEXT,
            days INTEGER DEFAULT 0, invoice_id TEXT, granted_by INTEGER,
            total_paid INTEGER DEFAULT 0, buy_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS premium_tokens (
            user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0,
            total_bought INTEGER NOT NULL DEFAULT 0, total_spent INTEGER NOT NULL DEFAULT 0,
            total_refund INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS token_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            delta INTEGER NOT NULL, balance_after INTEGER NOT NULL, kind TEXT NOT NULL,
            ref TEXT, note TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS resetotp_trial (
            user_id INTEGER PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0,
            first_used_at TIMESTAMP, last_used_at TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS premium_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            invoice_id TEXT UNIQUE, package TEXT, kind TEXT DEFAULT 'days',
            days INTEGER, tokens INTEGER DEFAULT 0, amount INTEGER, fee INTEGER,
            total INTEGER, status TEXT NOT NULL DEFAULT 'pending', qris_image TEXT,
            payment_link TEXT, expired_at TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, paid_at TIMESTAMP)""",
    ]
    for s in stmts:
        try:
            cur.execute(s)
        except Exception as e:
            logging.warning("[DB] create table: %s", e)
    try:
        cur.execute("INSERT OR IGNORE INTO allowed_users (user_id, username, added_by) VALUES (?, ?, ?)",
                    (USER_ID, None, USER_ID))
        conn.commit()
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════
#  WEB SYNC  (Cloudflare Worker + D1) — fire-and-forget
# ══════════════════════════════════════════════════════════════════════
def _web_post(path, payload):
    try:
        if not WEB_SYNC_URL:
            return
        requests.post(WEB_SYNC_URL.rstrip("/") + path, json=payload,
                      headers={"x-sync-key": WEB_SYNC_KEY}, timeout=WEB_SYNC_TIMEOUT)
    except Exception as e:
        logging.debug("[WEB] push %s gagal: %s", path, e)


def _web_push(table, row):
    try:
        threading.Thread(target=_web_post, args=("/ingest", {"table": table, "row": row}),
                         daemon=True).start()
    except Exception:
        pass


# ── Baca/tulis ke remote D1 (source of truth) ─────────────────────────
# Return None = remote tak terjangkau → pemanggil jatuh ke SQLite lokal.
_web_enabled = bool(WEB_SYNC_URL) and WEB_SYNC_KEY != "GANTI_DENGAN_SYNC_KEY"
_web_down_until = 0.0
_web_state_lock = threading.Lock()


def _web_ready():
    if not (_web_enabled and WEB_DB_PRIMARY):
        return False
    with _web_state_lock:
        return time.time() >= _web_down_until


def _web_mark_down(why=""):
    global _web_down_until
    with _web_state_lock:
        first = time.time() >= _web_down_until
        _web_down_until = time.time() + WEB_FAIL_COOLDOWN
    if first:
        logging.warning("[WEB] remote DB tidak terjangkau (%s) — pakai cache lokal %ss",
                        why, WEB_FAIL_COOLDOWN)


def _web_mark_up():
    global _web_down_until
    with _web_state_lock:
        if _web_down_until:
            _web_down_until = 0.0


def _web_read(table, params=None, timeout=None):
    """GET /api/read → dict {'ok':True,'row':...|'rows':[...]} atau None."""
    if not _web_ready():
        return None
    q = {"table": table}
    for k, v in (params or {}).items():
        if v is not None:
            q[k] = v
    try:
        r = requests.get(WEB_SYNC_URL.rstrip("/") + "/api/read", params=q,
                         headers={"x-sync-key": WEB_SYNC_KEY},
                         timeout=timeout or WEB_READ_TIMEOUT)
        if r.status_code != 200:
            _web_mark_down(f"read {table} HTTP {r.status_code}")
            return None
        data = r.json()
        if not isinstance(data, dict) or not data.get("ok"):
            _web_mark_down(f"read {table} bad payload")
            return None
        _web_mark_up()
        return data
    except Exception as e:
        _web_mark_down(f"read {table}: {e}")
        return None


def _web_write(action, payload, timeout=None):
    """POST /api/write → dict response (ok=False tetap dikembalikan) atau None."""
    if not _web_ready():
        return None
    body = dict(payload or {})
    body["action"] = action
    try:
        r = requests.post(WEB_SYNC_URL.rstrip("/") + "/api/write", json=body,
                          headers={"x-sync-key": WEB_SYNC_KEY},
                          timeout=timeout or WEB_SYNC_TIMEOUT)
        if r.status_code not in (200, 400):
            _web_mark_down(f"write {action} HTTP {r.status_code}")
            return None
        data = r.json()
        if not isinstance(data, dict):
            _web_mark_down(f"write {action} bad payload")
            return None
        _web_mark_up()
        return data
    except Exception as e:
        _web_mark_down(f"write {action}: {e}")
        return None


_CACHE_COLS = {
    "premium_access": ("user_id", "expired_at", "package", "days", "total_paid",
                       "buy_count", "invoice_id", "granted_by", "updated_at"),
    "premium_tokens": ("user_id", "balance", "total_bought", "total_spent",
                       "total_refund", "updated_at"),
    "resetotp_trial": ("user_id", "used", "first_used_at", "last_used_at"),
}
_CACHE_PK = {"premium_access": "user_id", "premium_tokens": "user_id",
             "resetotp_trial": "user_id"}


def _cache_local(table, row):
    """Mirror 1 baris hasil baca remote ke SQLite lokal (best-effort)."""
    cols = _CACHE_COLS.get(table)
    pk = _CACHE_PK.get(table)
    if not cols or not row:
        return
    try:
        use = [c for c in cols if c in row]
        if pk not in use:
            return
        setc = [c for c in use if c != pk]
        sql = (f"INSERT INTO {table} ({','.join(use)}) "
               f"VALUES ({','.join('?' * len(use))}) "
               f"ON CONFLICT({pk}) DO UPDATE SET "
               + ", ".join(f"{c}=excluded.{c}" for c in setc))
        cur.execute(sql, tuple(row.get(c) for c in use))
        conn.commit()
    except Exception as e:
        logging.debug("[WEB] cache %s gagal: %s", table, e)


def _local_row_count(table):
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        r = cur.fetchone()
        return int((r[0] if r else 0) or 0)
    except Exception:
        return -1


def web_restore_from_remote(force=False):
    """Tarik data dari remote D1 → isi SQLite lokal sebagai cache.

    Dipanggil sekali saat bot start. Default hanya mengisi tabel yang KOSONG
    di lokal (kasus DB panel hilang/kereset).
    """
    if not _web_ready():
        return {}
    done = {}
    for table in ("premium_access", "premium_tokens", "resetotp_trial"):
        n_local = _local_row_count(table)
        if n_local < 0 or (n_local > 0 and not force):
            continue
        res = _web_read(table, {"limit": 5000}, timeout=25)
        if res is None:
            logging.warning("[WEB] restore %s gagal — remote tidak menjawab", table)
            break
        ok = 0
        for row in (res.get("rows") or []):
            try:
                _cache_local(table, row)
                ok += 1
            except Exception:
                pass
        if ok:
            done[table] = ok
            logging.info("[WEB] restore %s: %s baris dari remote D1", table, ok)
    if done:
        print(f"  Restore dari database website: {sum(done.values())} baris")
    return done


def _web_push_tokens(user_id):
    try:
        cur.execute("SELECT user_id, balance, total_bought, total_spent, total_refund, updated_at "
                    "FROM premium_tokens WHERE user_id = ?", (user_id,))
        r = cur.fetchone()
        if not r:
            return
        _web_push("premium_tokens", {"user_id": r[0], "balance": r[1] or 0,
                  "total_bought": r[2] or 0, "total_spent": r[3] or 0,
                  "total_refund": r[4] or 0, "updated_at": r[5]})
    except Exception:
        pass


def _web_push_ledger(rowid):
    try:
        cur.execute("SELECT id, user_id, delta, balance_after, kind, ref, note, created_at "
                    "FROM token_ledger WHERE id = ?", (rowid,))
        r = cur.fetchone()
        if not r:
            return
        _web_push("token_ledger", {"id": r[0], "user_id": r[1], "delta": r[2],
                  "balance_after": r[3], "kind": r[4], "ref": r[5], "note": r[6],
                  "created_at": r[7]})
    except Exception:
        pass


def _web_push_premium(user_id):
    try:
        cur.execute("SELECT user_id, expired_at, package, days, total_paid, buy_count, updated_at "
                    "FROM premium_access WHERE user_id = ?", (user_id,))
        r = cur.fetchone()
        if not r:
            return
        _web_push("premium_access", {"user_id": r[0], "expired_at": r[1], "package": r[2],
                  "days": r[3] or 0, "total_paid": r[4] or 0, "buy_count": r[5] or 0,
                  "updated_at": r[6]})
    except Exception:
        pass


def _web_push_trial(user_id):
    try:
        cur.execute("SELECT user_id, used, first_used_at, last_used_at "
                    "FROM resetotp_trial WHERE user_id = ?", (user_id,))
        r = cur.fetchone()
        if not r:
            return
        _web_push("resetotp_trial", {"user_id": r[0], "used": r[1] or 0,
                  "first_used_at": r[2], "last_used_at": r[3]})
    except Exception:
        pass


def _web_push_invoice(invoice_id):
    try:
        cur.execute(
            "SELECT invoice_id, user_id, package, kind, days, tokens, amount, fee, "
            "total, status, paid_at, created_at FROM premium_invoices WHERE invoice_id = ?",
            (invoice_id,))
        r = cur.fetchone()
        if not r:
            return
        _web_push("premium_invoices", {
            "invoice_id": r[0], "user_id": r[1], "package": r[2], "kind": r[3],
            "days": r[4] or 0, "tokens": r[5] or 0, "amount": r[6] or 0,
            "fee": r[7] or 0, "total": r[8] or 0, "status": r[9],
            "paid_at": r[10], "created_at": r[11]})
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
#  AKSES / GATE
# ══════════════════════════════════════════════════════════════════════
def is_owner(user_id):
    return user_id == USER_ID


def is_admin(user_id):
    if user_id == USER_ID:
        return True
    try:
        cur.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        return cur.fetchone() is not None
    except Exception:
        return False


def is_banned(user_id):
    try:
        cur.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
        return cur.fetchone() is not None
    except Exception:
        return False


def is_allowed_user(user_id):
    if is_owner(user_id):
        return True
    if is_banned(user_id):
        return False
    if MAINTENANCE_MODE and not is_admin(user_id):
        return False
    try:
        cur.execute("SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,))
        return cur.fetchone() is not None
    except Exception:
        return False


# ── Premium ──
def _premium_from_row(row):
    exp = _parse_iso(row.get("expired_at"))
    return {"expired_at": exp, "package": row.get("package"),
            "days": int(row.get("days") or 0),
            "active": bool(exp and exp > _now_utc()),
            "left": (exp - _now_utc()) if exp else None}


def _get_premium_local(user_id):
    try:
        cur.execute("SELECT expired_at, package, days FROM premium_access WHERE user_id = ?",
                    (user_id,))
        row = cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    return _premium_from_row({"expired_at": row[0], "package": row[1], "days": row[2]})


def get_premium(user_id):
    """Status premium — remote D1 dulu (source of truth), lalu cache lokal."""
    res = _web_read("premium_access", {"user_id": user_id})
    if res is not None:
        row = res.get("row")
        if not row:
            return None
        _cache_local("premium_access", row)
        return _premium_from_row(row)
    return _get_premium_local(user_id)


def has_premium(user_id):
    if is_owner(user_id):
        return True
    p = get_premium(user_id)
    return bool(p and p["active"])


def _human_left(td):
    if not td:
        return "-"
    secs = int(td.total_seconds())
    if secs <= 0:
        return "habis"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} hari {h} jam"
    if h:
        return f"{h} jam {m} menit"
    return f"{m} menit"


def _fmt_wib(dt):
    d = _parse_iso(dt) if not isinstance(dt, datetime) else dt
    if not d:
        return "-"
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (d.astimezone(timezone.utc) + timedelta(hours=7)).strftime("%d %b %Y %H:%M") + " WIB"


def _rp(n):
    try:
        return f"Rp {int(n):,}".replace(",", ".")
    except Exception:
        return f"Rp {n}"


def _pkg_info(pk):
    """Info paket (harian / token) → dict seragam. None kalau tak dikenal."""
    if pk in PREMIUM_PACKAGES:
        label, price, days = PREMIUM_PACKAGES[pk]
        return {"kind": "days", "code": pk, "label": label,
                "price": price, "days": days, "tokens": 0, "bonus": 0}
    if pk in TOKEN_PACKAGES:
        label, price, tokens, bonus = TOKEN_PACKAGES[pk]
        return {"kind": "token", "code": pk, "label": label, "price": price,
                "days": 0, "tokens": tokens, "bonus": bonus}
    return None


def grant_premium(user_id, days, granted_by=None, invoice_id=None, amount=0):
    """Kasih/perpanjang premium. Kalau masih aktif, tambah di atas sisa.

    Ditulis di remote D1 (penambahan hari dihitung di sana), lalu hasilnya
    di-mirror ke cache lokal. Kalau remote mati → tulis lokal + push biasa.
    """
    pkg = next((k for k, v in PREMIUM_PACKAGES.items() if v[2] == days), f"{days}d")
    res = _web_write("grant_premium", {
        "user_id": user_id, "days": int(days), "package": pkg,
        "granted_by": granted_by, "invoice_id": invoice_id,
        "amount": int(amount or 0)})
    if res is not None and res.get("ok"):
        if res.get("row"):
            _cache_local("premium_access", res["row"])
        return _parse_iso(res.get("expired_at")) or _now_utc() + timedelta(days=days)

    now = _now_utc()
    p = _get_premium_local(user_id)
    base = p["expired_at"] if (p and p["active"]) else now
    new_exp = base + timedelta(days=days)
    try:
        cur.execute("""
            INSERT INTO premium_access (user_id, expired_at, package, days, invoice_id,
                granted_by, total_paid, buy_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                expired_at = excluded.expired_at, package = excluded.package,
                days = excluded.days,
                invoice_id = COALESCE(excluded.invoice_id, premium_access.invoice_id),
                granted_by = excluded.granted_by,
                total_paid = premium_access.total_paid + excluded.total_paid,
                buy_count = premium_access.buy_count + 1,
                updated_at = excluded.updated_at
        """, (user_id, _iso(new_exp), pkg, days, invoice_id, granted_by, amount, _iso(now)))
        conn.commit()
    except Exception as e:
        logging.warning("[PREMIUM] grant err: %s", e)
    _web_push_premium(user_id)
    return new_exp


# ── MG Cloud Pay (QRIS) ──
def _mgpay_get(path, params, retries=3):
    q = dict(params or {})
    q["apikey"] = MGPAY_API_KEY
    last = None
    for _ in range(max(1, retries)):
        try:
            r = requests.get(f"{MGPAY_BASE}{path}", params=q, timeout=MGPAY_TIMEOUT)
            data = r.json()
        except Exception as e:
            logging.warning("[MGPAY] %s err: %s", path, e)
            last = None
            time.sleep(2)
            continue
        msg = str(data.get("message") or "")
        if data.get("success") is False and "banyak request" in msg.lower():
            last = data
            time.sleep(6)
            continue
        return data
    return last


def _mgpay_create_invoice(amount):
    data = _mgpay_get("/api/invoice", {"amount": int(amount)})
    if data and data.get("success") and data.get("invoice_id"):
        return data
    if data:
        logging.warning("[MGPAY] create invoice ditolak: %s", data)
    return None


def _mgpay_check_status(invoice_id):
    data = _mgpay_get("/api/invoice/status", {"invoice_id": str(invoice_id)})
    if data and data.get("status"):
        return data
    if data and data.get("success") is False:
        logging.warning("[MGPAY] cek status ditolak: %s", data)
        return None
    return data or None


# ── Token ──
def _get_token_row_local(user_id):
    try:
        cur.execute("SELECT balance, total_bought FROM premium_tokens WHERE user_id = ?",
                    (user_id,))
        row = cur.fetchone()
    except Exception:
        row = None
    if not row:
        return {"balance": 0, "bought": 0}
    return {"balance": int(row[0] or 0), "bought": int(row[1] or 0)}


def get_token_stats(user_id):
    """Saldo + total beli — remote D1 dulu, lalu cache lokal."""
    res = _web_read("premium_tokens", {"user_id": user_id})
    if res is not None:
        row = res.get("row")
        if not row:
            return {"balance": 0, "bought": 0}
        _cache_local("premium_tokens", row)
        return {"balance": int(row.get("balance") or 0),
                "bought": int(row.get("total_bought") or 0)}
    return _get_token_row_local(user_id)


def get_tokens(user_id):
    return get_token_stats(user_id)["balance"]


def _token_log(user_id, delta, balance_after, kind, ref=None, note=None):
    try:
        cur.execute("INSERT INTO token_ledger (user_id, delta, balance_after, kind, ref, note) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (user_id, delta, balance_after, kind, ref, note))
        conn.commit()
        rid = cur._get_cur().lastrowid
        _web_push_ledger(rid)
        _web_push_tokens(user_id)
    except Exception as e:
        logging.warning("[TOKEN] log err: %s", e)


def spend_tokens(user_id, amount=None, ref=None, note=None):
    """Potong token atomik di remote D1 (guard balance >= amount). False = kurang."""
    if is_owner(user_id):
        return True
    amount = int(RESETOTP_TOKEN_COST if amount is None else amount)
    if amount <= 0:
        return True
    res = _web_write("spend_tokens", {
        "user_id": user_id, "amount": amount, "ref": ref, "note": note})
    if res is not None:
        if res.get("row"):
            _cache_local("premium_tokens", res["row"])
        if res.get("ok"):
            return True
        if res.get("error") != "insufficient":
            logging.warning("[TOKEN] spend remote err: %s", res.get("error"))
        return False

    try:
        cur.execute("UPDATE premium_tokens SET balance = balance - ?, total_spent = total_spent + ?, "
                    "updated_at = ? WHERE user_id = ? AND balance >= ?",
                    (amount, amount, _iso(_now_utc()), user_id, amount))
        conn.commit()
        if cur.rowcount == 0:
            return False
    except Exception as e:
        logging.warning("[TOKEN] spend err: %s", e)
        return False
    _token_log(user_id, -amount, _get_token_row_local(user_id)["balance"], "spend", ref, note)
    return True


def refund_tokens(user_id, amount=None, ref=None, note=None):
    """Kembalikan token (idempoten per ref). Return saldo baru."""
    if is_owner(user_id):
        return get_tokens(user_id)
    amount = int(RESETOTP_TOKEN_COST if amount is None else amount)
    if amount <= 0:
        return get_tokens(user_id)
    res = _web_write("refund_tokens", {
        "user_id": user_id, "amount": amount, "ref": ref, "note": note})
    if res is not None:
        if res.get("row"):
            _cache_local("premium_tokens", res["row"])
        if not res.get("ok") and res.get("error") not in (None, "already_refunded"):
            logging.warning("[TOKEN] refund remote err: %s", res.get("error"))
        return int(res.get("balance") or 0)

    if ref:
        try:
            cur.execute("SELECT 1 FROM token_ledger WHERE ref = ? AND kind = 'refund' LIMIT 1", (ref,))
            if cur.fetchone():
                return _get_token_row_local(user_id)["balance"]
        except Exception:
            pass
    now = _iso(_now_utc())
    try:
        cur.execute("""INSERT INTO premium_tokens (user_id, balance, total_refund, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                balance = premium_tokens.balance + excluded.balance,
                total_refund = premium_tokens.total_refund + excluded.total_refund,
                updated_at = excluded.updated_at""", (user_id, amount, amount, now))
        conn.commit()
    except Exception as e:
        logging.warning("[TOKEN] refund err: %s", e)
        return _get_token_row_local(user_id)["balance"]
    bal = _get_token_row_local(user_id)["balance"]
    _token_log(user_id, amount, bal, "refund", ref, note)
    return bal


def add_tokens(user_id, amount, ref=None, note=None):
    """Tambah token. Return saldo baru."""
    amount = int(amount or 0)
    if amount <= 0:
        return get_tokens(user_id)
    res = _web_write("add_tokens", {
        "user_id": user_id, "amount": amount, "kind": "grant",
        "ref": ref, "note": note})
    if res is not None and res.get("ok"):
        if res.get("row"):
            _cache_local("premium_tokens", res["row"])
        return int(res.get("balance") or 0)

    now = _iso(_now_utc())
    try:
        cur.execute("""INSERT INTO premium_tokens (user_id, balance, total_bought, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                balance = premium_tokens.balance + excluded.balance,
                total_bought = premium_tokens.total_bought + excluded.total_bought,
                updated_at = excluded.updated_at""", (user_id, amount, amount, now))
        conn.commit()
    except Exception as e:
        logging.warning("[TOKEN] add err: %s", e)
        return _get_token_row_local(user_id)["balance"]
    bal = _get_token_row_local(user_id)["balance"]
    _token_log(user_id, amount, bal, "grant", ref, note)
    return bal


def _has_paid_history(user_id):
    """Trial hanya untuk user yang belum pernah punya token/premium.

    Dibaca lewat get_premium/get_token_stats supaya ikut remote D1 — kalau
    tidak, user yang datanya cuma ada di remote bisa dapat trial ulang.
    """
    if get_premium(user_id) is not None:
        return True
    st = get_token_stats(user_id)
    return bool(st["balance"] > 0 or st["bought"] > 0)


# ── Trial ──
def _get_trial_local(user_id):
    try:
        cur.execute("SELECT used FROM resetotp_trial WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        used = int((row[0] if row else 0) or 0)
    except Exception:
        used = 0
    return {"used": used, "left": max(0, RESETOTP_TRIAL_LIMIT - used)}


def _trial_from_row(row):
    used = int((row or {}).get("used") or 0)
    return {"used": used, "left": max(0, RESETOTP_TRIAL_LIMIT - used)}


def get_trial(user_id):
    """Status trial — remote D1 dulu, lalu cache lokal."""
    res = _web_read("resetotp_trial", {"user_id": user_id})
    if res is not None:
        row = res.get("row")
        if not row:
            return {"used": 0, "left": RESETOTP_TRIAL_LIMIT}
        _cache_local("resetotp_trial", row)
        return _trial_from_row(row)
    return _get_trial_local(user_id)


def trial_left(user_id):
    if is_owner(user_id):
        return RESETOTP_TRIAL_LIMIT
    return get_trial(user_id)["left"]


def try_reserve_trial(user_id):
    """Pesan 1 jatah trial secara atomik (UPDATE ... WHERE used < LIMIT).

    Guard dijalankan di remote D1; kalau remote mati, guard yang sama
    dipakai di SQLite lokal.
    """
    if is_owner(user_id):
        return True
    res = _web_write("reserve_trial", {
        "user_id": user_id, "limit": RESETOTP_TRIAL_LIMIT})
    if res is not None:
        if res.get("row"):
            _cache_local("resetotp_trial", res["row"])
        if res.get("ok"):
            return True
        if res.get("error") != "limit_reached":
            logging.warning("[TRIAL] reserve remote err: %s", res.get("error"))
        return False

    now = _iso(_now_utc())
    try:
        cur.execute("INSERT OR IGNORE INTO resetotp_trial (user_id, used, first_used_at, last_used_at) "
                    "VALUES (?, 0, ?, ?)", (user_id, now, now))
        cur.execute("UPDATE resetotp_trial SET used = used + 1, "
                    "first_used_at = COALESCE(first_used_at, ?), last_used_at = ? "
                    "WHERE user_id = ? AND used < ?", (now, now, user_id, RESETOTP_TRIAL_LIMIT))
        conn.commit()
        reserved = (cur.rowcount == 1)
    except Exception as e:
        logging.warning("[TRIAL] reserve err: %s", e)
        return False
    if reserved:
        _web_push_trial(user_id)
    return reserved


def refund_trial(user_id):
    """Kembalikan 1 jatah trial yang tadi dipesan (proses gagal)."""
    if is_owner(user_id):
        return RESETOTP_TRIAL_LIMIT
    res = _web_write("refund_trial", {"user_id": user_id})
    if res is not None and res.get("ok"):
        row = res.get("row") or {}
        if row:
            _cache_local("resetotp_trial", row)
        return _trial_from_row(row)["left"]

    now = _iso(_now_utc())
    try:
        cur.execute("UPDATE resetotp_trial SET used = MAX(0, used - 1), last_used_at = ? "
                    "WHERE user_id = ? AND used > 0", (now, user_id))
        conn.commit()
    except Exception as e:
        logging.warning("[TRIAL] refund err: %s", e)
    _web_push_trial(user_id)
    return _get_trial_local(user_id)["left"]


def resetotp_access_mode(user_id):
    """premium -> token -> trial -> none."""
    if has_premium(user_id):
        return "premium"
    if get_tokens(user_id) >= RESETOTP_TOKEN_COST:
        return "token"
    if not _has_paid_history(user_id) and trial_left(user_id) > 0:
        return "trial"
    return "none"

# ══════════════════════════════════════════════════════════════════════
#  UI  — blockquote screens + simbol (emoji custom dgn fallback simbol)
# ══════════════════════════════════════════════════════════════════════
# Emoji custom asli (ID diambil dari dik.py). Format: key -> (emoji_id, emoji_asli).
# PENTING: fallback di dalam <tg-emoji> WAJIB emoji asli (satu emoji), bukan
# simbol teks — kalau simbol teks Telegram tolak (Entity_text_invalid).
CE = {
    "profil": ("5870994129244131212", "👤"),  # profile / id user
    "stat":   ("5415655814079723871", "🔝"),  # statistik / eklusif
    "menu":   ("5256248974767046755", "⚙️"),  # settings / pilih menu
    "fix":    ("5345943173401175849", "❤️"),  # whatsapp (fix merah)
    "otp":    ("5256248974767046755", "🔑"),  # login / reset otp
    "prem":   ("5330237710655306682", "💎"),  # premium
    "owner":  ("5256143829672672750", "👤"),  # owner / akun
    "user":   ("5870994129244131212", "👤"),  # menu user
    "help":   ("5870570722778156940", "❓"),  # bantuan / panduan
    "token":  ("5438436750114439411", "🎟"),  # token
    "email":  ("5472239203590888751", "📩"),  # email (kartu balasan)
    "file":   ("5870570722778156940", "📁"),  # subject (kartu balasan)
    "nomor":  ("5422696450888842691", "📞"),  # nomor (kartu balasan)
    "set":    ("5256248974767046755", "⚙️"),  # settings / mode
    "waktu":  ("5872756762347573066", "⏲"),  # limit jam
    "gift":   ("4956337889593000947", "🎁"),  # trial
    "lock":   ("5253742260401674633", "🔒"),  # akses terkunci
    "ok":     ("5796205953913196373", "✅"),
    "no":     ("5420323339723881652", "❌"),
    "wait":   ("5427181942934088912", "🟠"),  # loading / proses
}
# Simbol teks keren untuk key yang TIDAK punya emoji custom cocok.
SYM = {
    "brand": "⟡", "dot": "⌁",
}


def sym(key):
    """Emoji custom (dgn fallback emoji asli) kalau ada; kalau tidak simbol teks."""
    ce = CE.get(key)
    if ce:
        cid, fb = ce
        return f'<tg-emoji emoji-id="{cid}">{fb}</tg-emoji>'
    return SYM.get(key, "⟡")


# _DOT dipakai sebagai bullet baris detail (sama gaya dik.py).
_DOT = "⌁"


_BAR = "▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰"


def _q(body, expandable=False):
    tag = "blockquote expandable" if expandable else "blockquote"
    return f"<{tag}>{body}</blockquote>"


def _scrub_brand(text):
    """Buang jejak nama layanan email (sitepro/siteprofree/roundcube) dari teks
    yang tampil ke user. Ganti dgn label netral."""
    if not text:
        return text
    import re as _re
    # Site.pro / sitepro / siteprofree / siteprofree.email → WhatsApp
    text = _re.sub(r"(?i)site\.?pro(?:free)?(?:\.email)?", "WhatsApp", text)
    text = _re.sub(r"(?i)roundcube\s*", "", text)
    return text


def _reply_card(newest):
    """Kartu balasan asli WhatsApp Support (metadata: subject + nomor).

    `newest` = dict newest_reply dari pipeline (from/subject/matched_nomor).
    Pengirim selalu ditampilkan sbg support WhatsApp resmi; nama layanan email
    (sitepro/siteprofree) TIDAK boleh bocor. Body tidak ditampilkan.
    """
    if not newest:
        return ""
    subj = _html.escape(_scrub_brand(newest.get("subject", "") or "") or "-")
    matched = newest.get("matched_nomor")
    body = (
        f"{sym('fix')} Balasan dari WhatsApp Support\n"
        f"{sym('email')} Dari: support@support.whatsapp.com\n"
        f"{sym('file')} Subject: {subj}"
    )
    if matched:
        body += f"\n{sym('nomor')} Untuk nomor: +{matched}"
    return _q(body)


def _screen(title_sym, title, body):
    """Layar konsisten: header + blockquote body + footer."""
    head = f"{sym(title_sym)} <b>{title}</b>\n{_BAR}"
    foot = f"{_BAR}\n<i>Powered by</i> <b>DikZz</b>"
    return f"{head}\n{body}\n{foot}"


def _progress_bar(done, total, width=10):
    if total <= 0:
        pct = 0
    else:
        pct = int(done / total * 100)
    filled = int(round((pct / 100) * width))
    return "▰" * filled + "▱" * (width - filled), pct


# ── Tombol dengan style tetap (rapi, tidak acak) ──
#   Aturan: kolom KIRI = success, kolom KANAN = primary, baris BAWAH = danger.
_STYLES = ("primary", "success", "danger")


def _btn(text, cb, style="primary"):
    """InlineKeyboardButton dgn style. Pakai api_kwargs supaya aman di PTB stock."""
    return InlineKeyboardButton(text, callback_data=cb, api_kwargs={"style": style})


def _styled_kb(rows):
    """Bangun keyboard dgn style konsisten dari list baris [(teks, cb), ...].

    - Baris terakhir  → semua tombol DANGER (aksi bawah: Kembali/Tutup).
    - Baris lain      → kiri SUCCESS, kanan PRIMARY (kolom ke-3 dst = primary).
    """
    kb = []
    last = len(rows) - 1
    for ri, row in enumerate(rows):
        line = []
        for ci, (text, cb) in enumerate(row):
            if ri == last:
                style = "danger"
            else:
                style = "success" if ci == 0 else "primary"
            line.append(_btn(text, cb, style))
        kb.append(line)
    return InlineKeyboardMarkup(kb)


def _main_menu_kb(user_id):
    # /start: kiri success, kanan primary, baris bawah danger.
    rows = [
        [("Menu Fix", f"dz_fixmenu_{user_id}"), ("Panduan", f"dz_guide_{user_id}")],
        [("User", f"dz_user_{user_id}")],
    ]
    if is_owner(user_id) or is_admin(user_id):
        rows.append([("Owner Panel", f"dz_owner_{user_id}")])
    rows.append([("Tutup", f"dz_close_{user_id}")])
    return _styled_kb(rows)


def _resetotp_label(user_id):
    """Label tombol Reset OTP kondisional — sama logika dgn dik.py.

    Owner unlimited (tanpa trial). User biasa: premium/token → 'Reset OTP',
    masih ada trial → '(Trial)', selain itu '(Bayar)'.
    """
    if is_owner(user_id) or has_premium(user_id) or get_tokens(user_id) >= RESETOTP_TOKEN_COST:
        return "Reset OTP"
    if not _has_paid_history(user_id) and trial_left(user_id) > 0:
        return "Reset OTP (Trial)"
    return "Reset OTP (Bayar)"


def _fix_menu_kb(user_id):
    # [Server 1][Server 2] / [Reset OTP kondisional] / [Kembali]
    return _styled_kb([
        [("Server 1", f"dz_fix1_{user_id}"), ("Server 2", f"dz_fix2_{user_id}")],
        [(_resetotp_label(user_id), f"dz_otp_{user_id}")],
        [("Kembali", f"dz_home_{user_id}")],
    ])


def _user_menu_kb(user_id):
    # Menu User: aksi basic (buyprem, cek profil/token).
    return _styled_kb([
        [("Beli Premium", f"dz_buyprem_{user_id}"), ("Cek Token", f"dz_toklen_{user_id}")],
        [("Profil Saya", f"dz_profile_{user_id}")],
        [("Kembali", f"dz_home_{user_id}")],
    ])


def _prem_buy_kb(user_id):
    # Paket langganan + token (kolom kiri success), baris bawah danger.
    rows = []
    for pk in PREMIUM_ORDER:
        label, price, _days = PREMIUM_PACKAGES[pk]
        rows.append([(f"{label} · {_rp(price)}", f"dz_prem_buy_{pk}_{user_id}")])
    for pk in TOKEN_ORDER:
        label, price, tokens, bonus = TOKEN_PACKAGES[pk]
        cap = f"{tokens}{('+'+str(bonus)) if bonus else ''} Token · {_rp(price)}"
        rows.append([(cap, f"dz_prem_buy_{pk}_{user_id}")])
    rows.append([("Kembali", f"dz_user_{user_id}"), ("Tutup", f"dz_close_{user_id}")])
    return _styled_kb(rows)


def _prem_price_body():
    lines = [f"{sym('prem')} <b>Langganan (unlimited selama aktif)</b>"]
    for pk in PREMIUM_ORDER:
        label, price, days = PREMIUM_PACKAGES[pk]
        hemat = PREMIUM_DAILY_BASE * days - price
        tag = f"  <i>(hemat {_rp(hemat)})</i>" if hemat > 0 else ""
        lines.append(f"  {_DOT} <b>{label}</b> — {_rp(price)}{tag}")
    lines.append("")
    lines.append(f"{sym('token')} <b>Token (bayar per pakai, tanpa masa aktif)</b>")
    for pk in TOKEN_ORDER:
        label, price, tokens, bonus = TOKEN_PACKAGES[pk]
        total_tok = tokens + bonus
        bonus_txt = f" (+{bonus} bonus)" if bonus else ""
        lines.append(f"  {_DOT} <b>{total_tok} Token</b>{bonus_txt} — {_rp(price)}")
    lines.append("")
    lines.append(f"<i>1x Reset OTP = {RESETOTP_TOKEN_COST} token. "
                 f"Token dikembalikan otomatis kalau proses gagal.</i>")
    lines.append(f"<i>User baru dapat {RESETOTP_TRIAL_LIMIT}x TRIAL gratis "
                 f"(cuma terpotong kalau berhasil).</i>")
    return "\n".join(lines)


def _back_kb(user_id):
    return InlineKeyboardMarkup([[_btn("Kembali", f"dz_home_{user_id}", "danger")]])


def _result_kb(user_id, again_cb):
    return InlineKeyboardMarkup([[
        _btn("Ulangi", again_cb, "success"),
        _btn("Menu", f"dz_home_{user_id}", "primary"),
    ]])


# ── Statistik user dari fix_runs ──
def _user_fix_stats(user_id):
    total = sukses = 0
    try:
        cur.execute("SELECT COALESCE(SUM(total),0), COALESCE(SUM(reply_count),0) "
                    "FROM fix_runs WHERE user_id = ?", (user_id,))
        r = cur.fetchone()
        if r:
            total = int(r[0] or 0)
            sukses = int(r[1] or 0)
    except Exception:
        pass
    rate = int(sukses / total * 100) if total else 0
    return total, sukses, rate


def _status_line(user_id):
    if is_owner(user_id):
        return "OWNER"
    p = get_premium(user_id)
    if p and p["active"]:
        return f"PREMIUM {sym('prem')} {_human_left(p['left'])} lagi"
    return "FREE TIER"


def _welcome_text(user, user_id):
    name = _html.escape(user.full_name or "-")
    uname = f"@{user.username}" if getattr(user, "username", None) else "-"
    total, sukses, rate = _user_fix_stats(user_id)
    toks = get_tokens(user_id)
    tl = trial_left(user_id)
    if is_owner(user_id):
        tok_line = "Akses      : Owner (unlimited, tanpa trial)"
    else:
        tok_line = f"Token/Trial : {toks} token · trial {tl}/{RESETOTP_TRIAL_LIMIT}"
    profil = _q(
        f"{sym('profil')} PROFIL USER\n"
        f"Nama     : {name}\n"
        f"ID       : {user_id}\n"
        f"Username : {_html.escape(uname)}\n"
        f"Status   : {_status_line(user_id)}"
    )
    stat = _q(
        f"{sym('stat')} STATISTIK\n"
        f"Total Fix   : {total}\n"
        f"Sukses Fix  : {sukses}\n"
        f"Rate Sukses : {rate}%\n"
        f"{tok_line}"
    )
    guide = _q(
        f"{sym('menu')} BACA GAMBAR DI ATAS\n"
        f"{_DOT} FIX MERAH — tangani WhatsApp Server 1/2 merah\n"
        f"{_DOT} RESET OTP — reset nomor kena limit (batas waktu)\n"
        f"Lalu pilih menu di bawah untuk mulai."
    )
    return (
        f"{sym('fix')} <b>WELCOME TO DikZzCode BOT</b>\n{_BAR}\n"
        f"{profil}\n{stat}\n{guide}\n"
        f"{_BAR}\n<i>Powered by</i> <b>DikZz</b>"
    )


def _fix_menu_text(user_id):
    body = _q(
        f"{sym('fix')} MENU FIX\n"
        f"{_DOT} Server 1 — 150 email sender teratas\n"
        f"{_DOT} Server 2 — sisa email sender\n"
        f"{_DOT} Reset OTP — banding + form V1/V2"
    ) + _q(_resetotp_status_line(user_id))
    return _screen("fix", "MENU FIX", body)


def _resetotp_status_line(user_id):
    if is_owner(user_id):
        return f"{sym('otp')} Reset OTP: {sym('ok')} Unlimited (Owner)"
    p = get_premium(user_id)
    tok = get_tokens(user_id)
    if p and p["active"]:
        extra = f" · {tok} token" if tok else ""
        return f"{sym('otp')} Reset OTP: {sym('ok')} Langganan aktif ({_human_left(p['left'])}){extra}"
    if tok >= RESETOTP_TOKEN_COST:
        return f"{sym('otp')} Reset OTP: {sym('ok')} {tok} token (~{tok // RESETOTP_TOKEN_COST}x pakai)"
    if tok > 0:
        return f"{sym('otp')} Reset OTP: {sym('no')} {tok} token (kurang, butuh {RESETOTP_TOKEN_COST})"
    tl = trial_left(user_id)
    if tl > 0 and not _has_paid_history(user_id):
        return f"{sym('otp')} Reset OTP: {sym('ok')} Trial gratis {tl}/{RESETOTP_TRIAL_LIMIT}"
    return f"{sym('otp')} Reset OTP: {sym('no')} Belum aktif — silakan beli via User › Beli Premium"


# ── Teks wizard Reset OTP (EASY/HARD → LIMIT JAM → PROSES) — mirip dik.py ──
_OTP_NOTICE = (
    f"{sym('wait')} <b>PEMBERITAHUAN — WAJIB BACA</b>\n"
    f"Kalau setelah diproses belum ada balasan, itu <b>normal</b>.\n\n"
    f"Yang harus kamu lakukan:\n"
    f"{_DOT} Tunggu 2–3 menit dulu\n"
    f"{_DOT} Buka WhatsApp nomor itu, cek apakah sudah bisa dipakai lagi\n"
    f"{_DOT} Progress kelihatan \"stuck\" itu wajar — lagi digempur\n"
    f"   biar jebol atau lagi menunggu balasan WhatsApp\n"
    f"{_DOT} Balasan support WhatsApp sering datang tertunda"
)


def _otp_access_line(user_id):
    access = resetotp_access_mode(user_id)
    if is_owner(user_id):
        return f"{sym('ok')} <b>Owner</b> — akses unlimited"
    if access == "trial":
        return (f"{sym('gift')} <b>Mode TRIAL gratis</b> — sisa "
                f"{trial_left(user_id)}/{RESETOTP_TRIAL_LIMIT} (terpotong hanya kalau berhasil)")
    if access == "token":
        return (f"{sym('token')} <b>Bayar token</b> — {RESETOTP_TOKEN_COST} token/run · "
                f"saldo {get_tokens(user_id)}")
    return f"{sym('ok')} <b>Langganan aktif</b> — unlimited"


def _otp_mode_text(user_id, n):
    body = _q(_OTP_NOTICE) + _q(
        f"{_otp_access_line(user_id)}\n"
        f"{sym('nomor')} Nomor: {n}\n\n"
        f"Pilih mode:\n"
        f"{_DOT} <b>EASY</b> — Banding 10 · Form V1 10 · Form V2 10\n"
        f"{_DOT} <b>HARD</b> — Banding 15 · Form V1 15 · Form V2 15")
    return _screen("otp", "RESET OTP · MODE", body)


def _otp_limit_text(mode, n):
    body = _q(
        f"{sym('set')} Mode: <b>{mode.upper()}</b>\n"
        f"{sym('nomor')} Nomor: {n}\n\n"
        f"Pilih <b>LIMIT JAM</b> (dipakai di teks banding OTP):\n"
        f"{_DOT} OTOMATIS = 24 jam\n"
        f"{_DOT} CUSTOM = ketik sendiri (mis. 5 / 12 / 24)")
    return _screen("otp", "RESET OTP · LIMIT", body)


def _otp_custom_text(mode):
    body = _q(
        f"{sym('set')} Mode: <b>{mode.upper()}</b>\n\n"
        f"Kirim angka limit jam, contoh:\n"
        f"<code>5</code> · <code>12</code> · <code>24</code>\n\n"
        f"<i>Balas pesan ini dengan angkanya saja (1–72).</i>")
    return _screen("otp", "RESET OTP · CUSTOM", body)


def _otp_confirm_text(mode, limit_h, n):
    body = _q(
        f"{sym('set')} Mode: <b>{mode.upper()}</b>\n"
        f"{sym('waktu')} Limit: <b>{limit_h} jam</b>\n"
        f"{sym('nomor')} Nomor: <b>{n}</b>\n\n"
        f"Tekan <b>PROSES</b> untuk mulai.")
    return _screen("otp", "RESET OTP · KONFIRMASI", body)


def _user_menu_text(user, user_id):
    name = _html.escape(getattr(user, "full_name", None) or "-")
    uname = f"@{user.username}" if getattr(user, "username", None) else "-"
    toks = get_tokens(user_id)
    tl = trial_left(user_id)
    tok_line = "Owner (unlimited, tanpa trial)" if is_owner(user_id) else f"{toks} · Trial {tl}/{RESETOTP_TRIAL_LIMIT}"
    body = _q(
        f"{sym('user')} MENU USER\n"
        f"Nama     : {name}\n"
        f"ID       : {user_id}\n"
        f"Username : {_html.escape(uname)}\n"
        f"Status   : {_status_line(user_id)}\n"
        f"Token    : {tok_line}"
    ) + _q(
        f"{sym('menu')} Aksi:\n"
        f"{_DOT} Beli Premium — beli akses/token Reset OTP\n"
        f"{_DOT} Cek Token — lihat saldo token\n"
        f"{_DOT} Profil Saya — detail akun"
    )
    return _screen("user", "MENU USER", body)


def _profile_text(user, user_id):
    name = _html.escape(getattr(user, "full_name", None) or "-")
    uname = f"@{user.username}" if getattr(user, "username", None) else "-"
    total, sukses, rate = _user_fix_stats(user_id)
    toks = get_tokens(user_id)
    tl = trial_left(user_id)
    p = get_premium(user_id)
    prem_line = "Owner (unlimited)" if is_owner(user_id) else (
        f"Aktif · sisa {_human_left(p['left'])}" if (p and p["active"]) else "Tidak aktif")
    body = _q(
        f"{sym('profil')} PROFIL SAYA\n"
        f"Nama     : {name}\n"
        f"ID       : {user_id}\n"
        f"Username : {_html.escape(uname)}\n"
        f"Premium  : {prem_line}\n"
        f"Token    : {'unlimited (Owner)' if is_owner(user_id) else f'{toks} · Trial {tl}/{RESETOTP_TRIAL_LIMIT}'}\n"
        f"Total Fix: {total} · Sukses {sukses} ({rate}%)"
    )
    return _screen("profil", "PROFIL SAYA", body)


def _buyprem_text(user_id):
    status_box = ""
    if is_owner(user_id):
        status_box = f"{sym('ok')} <b>Kamu Owner</b> — akses Reset OTP unlimited.\n\n"
    else:
        p = get_premium(user_id)
        tok = get_tokens(user_id)
        if p and p["active"]:
            status_box = (f"{sym('ok')} <b>Langganan AKTIF</b>\n"
                          f"  {_DOT} Sisa {_human_left(p['left'])}\n"
                          f"  {_DOT} Expired {_fmt_wib(p['expired_at'])}\n\n")
        else:
            status_box = f"{sym('no')} <b>Belum punya langganan Reset OTP</b>\n\n"
        if tok:
            status_box += f"{sym('token')} <b>Saldo token:</b> {tok} (~{tok // RESETOTP_TOKEN_COST}x pakai)\n\n"
    body = (f"{status_box}{_prem_price_body()}\n\n"
            f"<i>Bayar via QRIS. Akses/token masuk otomatis setelah pembayaran terdeteksi.</i>")
    return _screen("prem", "BELI PREMIUM RESET OTP", _q(body))


def _panduan_text(user_id):
    body = _q(
        f"{sym('help')} PANDUAN PEMAKAIAN\n"
        f"1. Buka Menu Fix.\n"
        f"2. Pilih Server 1 / Server 2 untuk fix WhatsApp,\n"
        f"   atau Reset OTP untuk banding + form V1/V2.\n"
        f"3. Kirim nomor target (satu per baris, maks "
        f"{MAX_FIX_NUMBERS} nomor).\n"
        f"4. Tunggu — hasil muncul otomatis & dikirim ke grup log."
    ) + _q(
        f"{sym('otp')} RESET OTP — biaya\n"
        f"{_DOT} PREMIUM: unlimited selama aktif.\n"
        f"{_DOT} TOKEN: {RESETOTP_TOKEN_COST} token / run "
        f"(refund otomatis kalau gagal).\n"
        f"{_DOT} TRIAL: {RESETOTP_TRIAL_LIMIT}x gratis untuk user baru."
    ) + _q(
        f"{sym('user')} Menu User\n"
        f"{_DOT} Beli Premium (/buyprem) — beli akses/token.\n"
        f"{_DOT} Cek Token & Profil Saya."
    )
    return _screen("help", "PANDUAN", body)

# ══════════════════════════════════════════════════════════════════════
#  LOG GRUP  (tabel rich Bot API — sama format dgn bot utama)
# ══════════════════════════════════════════════════════════════════════
def _mask_fix_phone(phone):
    digits = re.sub(r"\D", "", str(phone or ""))
    return f"+{digits}" if digits else "-"


def _new_fix_banding_id():
    return f"LRFM{int(time.time() * 1000)}"


def _parse_numbers(text):
    if not text:
        return []
    parts = re.split(r"[,\n\s]+", text.strip())
    out = []
    for p in parts:
        p = p.strip().replace("+", "").replace("-", "")
        if p and p.isdigit() and len(p) >= 8:
            out.append(p)
    return out


async def _resolve_tg_profile(user_id):
    username = "-"
    full_name = "-"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getChat",
                params={"chat_id": int(user_id)})
            payload = r.json()
            if payload.get("ok"):
                chat = payload.get("result") or {}
                if chat.get("username"):
                    username = f"@{chat['username']}"
                full_name = " ".join(filter(None, [chat.get("first_name"),
                                                    chat.get("last_name")])) or "-"
    except Exception:
        pass
    return username, full_name


def _describe_origin(chat):
    if chat is None:
        return "-"
    ctype = getattr(chat, "type", None)
    ctype = getattr(ctype, "value", ctype) or "unknown"
    cid = getattr(chat, "id", None)
    if ctype == "private":
        return f"Private Chat ({cid})"
    title = getattr(chat, "title", None) or "-"
    return f"{ctype} · {title} ({cid})"


async def _send_rich_label_table(chat_id, *, title, subtitle, rows, footer):
    def cell(text, header=False):
        v = {"text": str(text), "align": "left", "valign": "middle"}
        if header:
            v["is_header"] = True
        return v
    blocks = [
        {"type": "heading", "text": title, "size": 2},
        {"type": "paragraph", "text": {"type": "italic", "text": subtitle}},
        {"type": "table", "cells": [
            [cell("LABEL", True), cell("DETAIL", True)],
            *[[cell(l, True), cell(v)] for l, v in rows],
        ], "is_bordered": True, "is_striped": True},
        {"type": "footer", "text": footer},
    ]
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendRichMessage",
            json={"chat_id": chat_id,
                  "rich_message": {"blocks": blocks, "skip_entity_detection": True}})
        payload = r.json()
        if payload.get("ok"):
            return payload
        mig = (payload.get("parameters") or {}).get("migrate_to_chat_id")
        if mig:
            return {"ok": False, "migrate_to_chat_id": int(mig)}
        raise RuntimeError(payload.get("description") or f"HTTP {r.status_code}")


async def _send_plain_label_table(chat_id, *, title, subtitle, rows, footer_html):
    tbl = "\n".join(f"{l}: {v}" for l, v in rows)
    body = (f"<b>{_html.escape(title)}</b>\n<i>{_html.escape(subtitle)}</i>\n\n"
            f"<pre>{_html.escape(tbl)}</pre>\n\n{footer_html}")
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": body, "parse_mode": "HTML",
                  "disable_web_page_preview": True})
        payload = r.json()
        if payload.get("ok"):
            return payload
        mig = (payload.get("parameters") or {}).get("migrate_to_chat_id")
        if mig:
            return {"ok": False, "migrate_to_chat_id": int(mig)}
        raise RuntimeError(payload.get("description") or f"HTTP {r.status_code}")


async def notify_fix_group_log(*, user_id, phone, status, banding_id=None,
                               server=None, source=None, username=None,
                               full_name=None, chat_origin=None, limit_hours=None):
    global FIX_LOG_GROUP_ID, _FIX_LOG_WARNED
    if _FIX_LOG_WARNED:
        return
    try:
        if not username or username == "-" or not full_name or full_name == "-":
            ru, rn = await _resolve_tg_profile(user_id)
            if not username or username == "-":
                username = ru
            if not full_name or full_name == "-":
                full_name = rn

        banding_id = banding_id or _new_fix_banding_id()
        status_u = str(status or "").upper()
        if status_u in {"SUCCESS", "REPLY", "BALASAN", "REPLIED"}:
            title = "PROSES FIXMERAH BERHASIL"
            status_label = "SUCCESS"
        else:
            title = "PROSES FIXMERAH TERKIRIM"
            status_label = "SENT"

        if username and username != "-" and not str(username).startswith("@"):
            username = f"@{username}"

        rows = [
            ("Telegram ID", str(user_id)),
            ("Username", username or "-"),
            ("Nama", full_name or "-"),
            ("Nomor", _mask_fix_phone(phone)),
            ("Banding", banding_id),
            ("Limit", f"{int(limit_hours or 24)} jam"),
            ("Asal Chat", chat_origin or "-"),
            ("Status", status_label),
        ]
        if server not in (None, "", 0):
            rows.insert(-1, ("Server", str(server)))
        if source:
            rows.insert(-1, ("Sumber", str(source)))

        subtitle = "Home › Fix › Group Log"
        footer = "Powered by DikZz"
        footer_html = "<i>Powered by</i> <b>DikZz</b>"

        async def _dispatch(chat_id):
            try:
                return await _send_rich_label_table(chat_id, title=title,
                    subtitle=subtitle, rows=rows, footer=footer)
            except Exception:
                return await _send_plain_label_table(chat_id, title=title,
                    subtitle=subtitle, rows=rows, footer_html=footer_html)

        result = await _dispatch(FIX_LOG_GROUP_ID)
        if result.get("ok"):
            return
        mig = result.get("migrate_to_chat_id")
        if mig:
            FIX_LOG_GROUP_ID = int(mig)
            result = await _dispatch(FIX_LOG_GROUP_ID)
            if result.get("ok"):
                return
        raise RuntimeError("Gagal kirim fix log")
    except Exception as exc:
        # "chat not found" = bot belum jadi anggota grup monitoring (mis. token
        # tes). Bukan error fatal — cukup catat sekali, jangan spam tiap proses.
        msg = str(exc).lower()
        if "chat not found" in msg or "chat_id is empty" in msg or "bot was kicked" in msg:
            if not _FIX_LOG_WARNED:
                logging.info("[FIX LOG] grup %s tidak tersedia (chat not found) — "
                             "log grup dinonaktifkan untuk sesi ini.", FIX_LOG_GROUP_ID)
                _FIX_LOG_WARNED = True
            return
        logging.warning("[FIX LOG] gagal kirim ke grup %s: %s", FIX_LOG_GROUP_ID, exc)

# ══════════════════════════════════════════════════════════════════════
#  WORKER: FIX SERVER 1 / 2
# ══════════════════════════════════════════════════════════════════════
async def _process_fix(numbers, user_id, message, context, server):
    """Fix Server 1/2: kirim banding ke banyak nomor + cek balasan."""
    import functools
    total = len(numbers)
    main_loop = asyncio.get_running_loop()
    srv_label = f"Server {server}"

    fix_run_id = None
    try:
        cur.execute("INSERT INTO fix_runs (user_id, total, status) VALUES (?, ?, 'running')",
                    (user_id, total))
        fix_run_id = cur._get_cur().lastrowid
        cur.executemany("INSERT INTO fix_run_items (run_id, phone) VALUES (?, ?)",
                        [(fix_run_id, n) for n in numbers])
        conn.commit()
    except Exception as e:
        logging.warning("[FIX] ledger err: %s", e)

    can_start, limit_msg = _can_start(user_id)
    if not can_start:
        try:
            await message.edit_text(
                _screen("fix", f"FIX {srv_label.upper()}",
                        _q(f"{sym('no')} {limit_msg}")),
                parse_mode=ParseMode.HTML, reply_markup=_back_kb(user_id))
        except Exception:
            pass
        return

    try:
        log_username, log_fullname = await _resolve_tg_profile(user_id)
        banding_map = {str(n): _new_fix_banding_id() for n in numbers}
        logged_events = set()
        chat_origin = "-"
        try:
            chat_origin = (context.user_data.get("dz_origin") if context else None) or "-"
        except Exception:
            chat_origin = "-"

        state = {"stage": "starting", "sent": 0, "reply": 0}

        def _schedule_log(phone, status_key):
            pk = str(phone or "").strip()
            if not pk:
                return
            key = (pk, status_key)
            if key in logged_events:
                return
            logged_events.add(key)
            bid = banding_map.get(pk) or _new_fix_banding_id()
            try:
                main_loop.call_soon_threadsafe(
                    lambda p=pk, s=status_key, b=bid: asyncio.create_task(
                        notify_fix_group_log(user_id=user_id, phone=p, status=s,
                            banding_id=b, server=server, source="bot",
                            username=log_username, full_name=log_fullname,
                            chat_origin=chat_origin)))
            except Exception:
                pass

        def _render(label):
            bar, pct = _progress_bar(state["sent"], total)
            body = _q(
                f"{sym('wait')} <b>{label}</b>\n"
                f"{bar} {pct}%\n"
                f"Nomor    : {total}\n"
                f"Terkirim : {state['sent']}/{total}\n"
                f"Balasan  : {state['reply']}"
            )
            return _screen("fix", f"FIX {srv_label.upper()} · PROSES", body)

        async def _safe_edit(text):
            try:
                await message.edit_text(text, parse_mode=ParseMode.HTML)
            except Exception:
                pass

        def _progress_cb(stage, data):
            data = data or {}
            if stage == "account_ready":
                state["stage"] = "kirim"
                label = "Akun sender siap..."
            elif stage == "sent":
                if data.get("ok"):
                    state["sent"] += 1
                    _schedule_log(data.get("nomor"), "SENT")
                label = "Mengirim permintaan ke WhatsApp support..."
            elif stage == "reply":
                state["reply"] += 1
                _schedule_log(data.get("matched") or data.get("nomor"), "SUCCESS")
                label = "Balasan diterima dari WhatsApp!"
            else:
                label = "Memproses..."
            try:
                main_loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(_safe_edit(_render(label))))
            except Exception:
                pass

        await _safe_edit(_render("Menyiapkan akun..."))

        result = await main_loop.run_in_executor(
            _GLOBAL_POOL,
            functools.partial(
                run_fix_multi_pipeline,
                numbers, CAPTCHA_API_KEY, cur, conn, CAPTCHA_SOLVER,
                240, _progress_cb, MAX_SENDS_PER_ACCOUNT, None, server,
            ))

        nomor_status = result.get("numbers", [])
        total_sent = result.get("total_sent", 0)
        total_reply = result.get("total_replied", 0)
        newest = result.get("newest_reply")

        # Fallback log grup
        for ns in nomor_status:
            phone = ns.get("nomor")
            if ns.get("replied"):
                key = (str(phone), "SUCCESS")
                if key not in logged_events:
                    logged_events.add(key)
                    await notify_fix_group_log(user_id=user_id, phone=phone, status="SUCCESS",
                        banding_id=banding_map.get(str(phone)), server=server, source="bot",
                        username=log_username, full_name=log_fullname, chat_origin=chat_origin)
            elif ns.get("sent"):
                key = (str(phone), "SENT")
                if key not in logged_events:
                    logged_events.add(key)
                    await notify_fix_group_log(user_id=user_id, phone=phone, status="SENT",
                        banding_id=banding_map.get(str(phone)), server=server, source="bot",
                        username=log_username, full_name=log_fullname, chat_origin=chat_origin)

        lines = []
        for ns in nomor_status:
            n = ns.get("nomor")
            if ns.get("replied"):
                tag = f"{sym('ok')} balasan diterima"
            elif ns.get("sent"):
                tag = "terkirim (tunggu balasan)"
            else:
                tag = f"{sym('no')} gagal kirim"
            lines.append(f"+{n} → {tag}")

        if fix_run_id:
            try:
                failed = sum(1 for ns in nomor_status if not ns.get("sent"))
                for ns in nomor_status:
                    st = "reply" if ns.get("replied") else ("sent" if ns.get("sent") else "failed")
                    cur.execute("UPDATE fix_run_items SET status=?, updated_at=CURRENT_TIMESTAMP "
                                "WHERE run_id=? AND phone=?", (st, fix_run_id, ns.get("nomor")))
                cur.execute("UPDATE fix_runs SET status='completed', sent_count=?, reply_count=?, "
                            "failed_count=?, completed_at=CURRENT_TIMESTAMP WHERE id=?",
                            (total_sent, total_reply, failed, fix_run_id))
                conn.commit()
            except Exception as e:
                logging.warning("[FIX] update ledger err: %s", e)

        now_hm = datetime.now().strftime("%H:%M")
        body = _q("\n".join(lines) if lines else "-") + _q(
            f"Terkirim {total_sent}/{total} · Balasan {total_reply} · {now_hm}")
        body += _reply_card(newest)
        if total_reply > 0:
            fix_title = f"FIX {srv_label.upper()} · BALASAN DITERIMA"
        elif total_sent > 0:
            fix_title = f"FIX {srv_label.upper()} · SELESAI"
        else:
            fix_title = f"FIX {srv_label.upper()} · GAGAL"
        try:
            await message.edit_text(
                _screen("fix", fix_title, body),
                parse_mode=ParseMode.HTML,
                reply_markup=_result_kb(user_id, f"dz_fix{server}_{user_id}"))
        except Exception:
            pass
    finally:
        _release(user_id)

# ══════════════════════════════════════════════════════════════════════
#  WORKER: RESET OTP  (banding + Form V1 + Form V2)
# ══════════════════════════════════════════════════════════════════════
async def _process_resetotp(numbers, user_id, message, context, mode="hard", limit_hours=24):
    import functools
    try:
        from faq_server import run_reset_otp_pipeline
    except Exception as e:
        try:
            await message.edit_text(
                _screen("otp", "RESET OTP", _q(f"{sym('no')} Modul tidak tersedia: {_html.escape(str(e))}")),
                parse_mode=ParseMode.HTML, reply_markup=_back_kb(user_id))
        except Exception:
            pass
        return

    main_loop = asyncio.get_running_loop()
    mode = (mode or "hard").lower()
    limit_hours = int(limit_hours or 24)

    can_start, limit_msg = _can_start(user_id)
    if not can_start:
        try:
            await message.edit_text(
                _screen("otp", "RESET OTP", _q(f"{sym('no')} {limit_msg}")),
                parse_mode=ParseMode.HTML, reply_markup=_back_kb(user_id))
        except Exception:
            pass
        return

    try:
        # ── Charge di awal (reserve) ──
        access = resetotp_access_mode(user_id)
        token_ref = f"resetotp:{user_id}:{int(time.time()*1000)}"
        token_charged = False
        trial_reserved = False

        if access == "token":
            if not spend_tokens(user_id, RESETOTP_TOKEN_COST, ref=token_ref,
                                note=f"RESET OTP {len(numbers)} nomor"):
                access = resetotp_access_mode(user_id)
            else:
                token_charged = True

        if access == "trial":
            if try_reserve_trial(user_id):
                trial_reserved = True
            else:
                access = resetotp_access_mode(user_id)
                if access == "token":
                    if spend_tokens(user_id, RESETOTP_TOKEN_COST, ref=token_ref,
                                    note=f"RESET OTP {len(numbers)} nomor"):
                        token_charged = True
                    else:
                        access = "none"
                elif access == "trial":
                    access = "none"

        if access == "none":
            tl = trial_left(user_id)
            try:
                await message.edit_text(
                    _screen("otp", "RESET OTP", _q(
                        f"{sym('no')} <b>Akses habis.</b>\n"
                        f"Trial gratis : {tl}/{RESETOTP_TRIAL_LIMIT}\n"
                        f"Token        : {get_tokens(user_id)} (butuh {RESETOTP_TOKEN_COST})")),
                    parse_mode=ParseMode.HTML, reply_markup=_back_kb(user_id))
            except Exception:
                pass
            return

        def _refund_tokens_if_charged(reason):
            if token_charged:
                try:
                    refund_tokens(user_id, RESETOTP_TOKEN_COST, ref=token_ref, note=f"refund: {reason}")
                except Exception:
                    pass

        def _refund_charge_if_failed(reason):
            nonlocal trial_reserved
            _refund_tokens_if_charged(reason)
            if trial_reserved:
                try:
                    refund_trial(user_id)
                except Exception:
                    pass
                trial_reserved = False

        state = {"stage": "Memulai...", "msg": "", "sent": 0, "replied": 0,
                 "nomor": "", "ua": "", "carrier": "", "email_sent": 0,
                 "faq1_ok": 0, "faq2_ok": 0, "waiting": False,
                 "mode": mode, "limit": limit_hours}
        log_username, log_fullname = await _resolve_tg_profile(user_id)
        banding_map = {str(n): _new_fix_banding_id() for n in numbers}
        logged_events = set()
        chat_origin = "-"
        try:
            chat_origin = (context.user_data.get("dz_origin") if context else None) or "-"
        except Exception:
            chat_origin = "-"

        def _schedule_log(phone, status_key):
            pk = str(phone or "").strip()
            if not pk:
                return
            key = (pk, status_key)
            if key in logged_events:
                return
            logged_events.add(key)
            bid = banding_map.get(pk) or _new_fix_banding_id()
            try:
                main_loop.call_soon_threadsafe(
                    lambda p=pk, s=status_key, b=bid: asyncio.create_task(
                        notify_fix_group_log(user_id=user_id, phone=p, status=s,
                            banding_id=b, server="resetotp", source="bot",
                            username=log_username, full_name=log_fullname,
                            chat_origin=chat_origin, limit_hours=limit_hours)))
            except Exception:
                pass

        def _render():
            notice = ""
            if state.get("waiting"):
                notice = (f"{sym('wait')} Belum ada balasan itu <b>normal</b>.\n"
                          f"Tunggu 2–3 menit, cek WhatsApp nomornya:\n"
                          f"sudah bisa dipakai lagi atau belum.\n\n")
            body = _q(
                f"{sym('wait')} <b>{_html.escape(state['stage'])}</b>\n\n"
                f"{notice}"
                f"Banding : {state['email_sent']}  ·  "
                f"Form V1 : {state['faq1_ok']}  ·  Form V2 : {state['faq2_ok']}\n"
                f"Nomor   : +{_html.escape(state['nomor'] or '-')}\n"
                f"Balasan : {state['replied']}/{len(numbers)}"
                + (f"\n\n<i>{_html.escape(state['msg'])}</i>" if state["msg"] else "")
            )
            return _screen("otp", "RESET OTP · PROSES", body)

        async def _safe_edit():
            try:
                await message.edit_text(_render(), parse_mode=ParseMode.HTML)
            except Exception:
                pass

        def _push():
            try:
                main_loop.call_soon_threadsafe(lambda: asyncio.create_task(_safe_edit()))
            except Exception:
                pass

        def _cb(stage, data):
            data = data or {}
            if stage == "reserve":
                state["nomor"] = str(data.get("nomor", ""))
                state["stage"] = "Menyiapkan pengirim..."
            elif stage == "device":
                state["nomor"] = str(data.get("nomor", "")) or state["nomor"]
                state["ua"] = data.get("ua", "")
                state["carrier"] = data.get("carrier", "")
            elif stage == "faq1":
                state["faq1_ok"] = data.get("count", 0)
                state["stage"] = "Form V1 terkirim"
                state["msg"] = f"Form V1: {data.get('count', 0)}/{data.get('total', 0)}"
            elif stage == "send_ok":
                state["sent"] += 1
                state["email_sent"] = len(data.get("sent_to", []))
                state["faq1_ok"] = data.get("faq1", state["faq1_ok"])
                state["faq2_ok"] = data.get("faq2", state["faq2_ok"])
                state["stage"] = "Email banding terkirim"
                state["msg"] = f"Terkirim ke {len(data.get('sent_to', []))} alamat support"
                _schedule_log(data.get("nomor") or state["nomor"], "SENT")
            elif stage == "faq2":
                state["faq2_ok"] = data.get("count", 0)
                state["stage"] = "Form verifikasi V2 terkirim"
                state["msg"] = f"Form V2: {data.get('count', 0)}/{data.get('total', 20)}"
            elif stage == "send_err":
                state["stage"] = "Gagal kirim"
                state["msg"] = f"{sym('no')} {str(data.get('error',''))[:120]}"
            elif stage == "wait_reply":
                state["stage"] = "Menunggu balasan WhatsApp Support..."
                state["waiting"] = True
                state["msg"] = ""
            elif stage == "reply":
                state["replied"] += 1
                state["stage"] = "Balasan diterima!"
                _schedule_log(data.get("nomor") or state["nomor"], "SUCCESS")
            _push()

        await _safe_edit()
        try:
            res = await asyncio.wait_for(
                main_loop.run_in_executor(
                    _GLOBAL_POOL,
                    functools.partial(run_reset_otp_pipeline, [str(n) for n in numbers],
                        db_cur=cur, db_conn=conn, progress_cb=_cb,
                        mode=mode, limit_hours=limit_hours)),
                timeout=900)
        except asyncio.TimeoutError:
            _refund_charge_if_failed("timeout pipeline")
            try:
                await message.edit_text(
                    _screen("otp", "RESET OTP · GAGAL",
                            _q(f"{sym('no')} Proses timeout (15 menit)."
                               + (f"\nToken dikembalikan → sisa {get_tokens(user_id)}" if token_charged else ""))),
                    parse_mode=ParseMode.HTML, reply_markup=_back_kb(user_id))
            except Exception:
                pass
            return
        except Exception as e:
            _refund_charge_if_failed("pipeline error")
            try:
                await message.edit_text(
                    _screen("otp", "RESET OTP · GAGAL",
                            _q(f"{sym('no')} Error: {_html.escape(str(e))[:200]}")),
                    parse_mode=ParseMode.HTML, reply_markup=_back_kb(user_id))
            except Exception:
                pass
            return

        ok = res.get("success_count", 0)
        total_reply = sum(1 for it in res.get("items", []) if it.get("newest_reply"))

        # ── Refund berdasar hasil ──
        if ok <= 0:
            if token_charged:
                _refund_tokens_if_charged("tidak ada yang terkirim")
            if trial_reserved:
                refund_trial(user_id)
                trial_reserved = False

        # Fallback log grup
        for it in res.get("items", []):
            phone = it.get("nomor")
            if it.get("newest_reply"):
                key = (str(phone), "SUCCESS")
                if key not in logged_events:
                    logged_events.add(key)
                    await notify_fix_group_log(user_id=user_id, phone=phone, status="SUCCESS",
                        banding_id=banding_map.get(str(phone)), server="resetotp", source="bot",
                        username=log_username, full_name=log_fullname, chat_origin=chat_origin,
                        limit_hours=limit_hours)
            elif it.get("ok"):
                key = (str(phone), "SENT")
                if key not in logged_events:
                    logged_events.add(key)
                    await notify_fix_group_log(user_id=user_id, phone=phone, status="SENT",
                        banding_id=banding_map.get(str(phone)), server="resetotp", source="bot",
                        username=log_username, full_name=log_fullname, chat_origin=chat_origin,
                        limit_hours=limit_hours)

        lines = []
        for it in res.get("items", []):
            ec = len(it.get("sent_to", []))
            f1 = it.get("faq1_ok") or sum(1 for r in it.get("faq1_results", []) if r.get("ok"))
            f2 = it.get("faq2_ok") or sum(1 for r in it.get("faq2_results", []) if r.get("ok"))
            if it.get("newest_reply"):
                tag = f"{sym('ok')} balasan diterima"
            elif it.get("ok"):
                tag = f"terkirim (banding {ec}, V1 {f1}, V2 {f2})"
            else:
                tag = f"{sym('no')} " + (_html.escape(str(it.get("error", ""))[:50]) or "gagal")
            lines.append(f"+{it.get('nomor')} → {tag}")

        detail = _q("\n".join(lines) if lines else "-")
        if total_reply > 0:
            title = "RESET OTP · BALASAN DITERIMA"
        elif ok > 0:
            title = "RESET OTP · SELESAI"
        else:
            title = "RESET OTP · GAGAL"

        # Baris biaya
        if has_premium(user_id):
            cost_line = "PREMIUM · gratis"
        elif token_charged and ok > 0:
            cost_line = f"Token terpakai {RESETOTP_TOKEN_COST} · sisa {get_tokens(user_id)}"
        elif token_charged and ok <= 0:
            cost_line = f"Token dikembalikan → sisa {get_tokens(user_id)}"
        elif ok > 0 and not trial_reserved and access == "trial":
            cost_line = f"Trial terpakai · sisa {trial_left(user_id)}/{RESETOTP_TRIAL_LIMIT}"
        elif access == "trial":
            cost_line = f"Trial · sisa {trial_left(user_id)}/{RESETOTP_TRIAL_LIMIT}"
        else:
            cost_line = ""

        body = detail + (_q(cost_line) if cost_line else "")
        newest = None
        for it in res.get("items", []):
            nr = it.get("newest_reply")
            if nr:
                newest = dict(nr)
                newest.setdefault("matched_nomor", it.get("nomor"))
                break
        body += _reply_card(newest)
        try:
            await message.edit_text(
                _screen("otp", title, body), parse_mode=ParseMode.HTML,
                reply_markup=_result_kb(user_id, f"dz_otp_{user_id}"))
        except Exception:
            pass
    finally:
        _release(user_id)

# ══════════════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════════════
# pending input: user_id -> ('fix1'|'fix2'|'otp')
_pending = {}


def _gate(user_id):
    return is_allowed_user(user_id)


async def _deny(update_or_msg):
    txt = _screen("brand", "AKSES DITOLAK",
                  _q(f"{sym('no')} Kamu belum terdaftar / diblokir.\nHubungi owner untuk akses."))
    try:
        await update_or_msg.reply_text(txt, parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    if not _gate(user_id):
        await _deny(update.message)
        return
    try:
        context.user_data["dz_origin"] = _describe_origin(update.effective_chat)
    except Exception:
        pass
    caption = _welcome_text(user, user_id)
    kb = _main_menu_kb(user_id)
    sent = False
    if os.path.isfile(BANNER_PATH):
        try:
            with open(BANNER_PATH, "rb") as _bnr:
                await update.message.reply_photo(
                    photo=_bnr, caption=caption, parse_mode=ParseMode.HTML,
                    reply_markup=kb)
            sent = True
        except Exception:
            sent = False
    if not sent:
        await update.message.reply_text(
            caption, parse_mode=ParseMode.HTML, reply_markup=kb)


def _cb_owner_ok(data, user_id):
    """callback_data diakhiri _{owner_id}. Cek pemilik."""
    try:
        owner = int(str(data).rsplit("_", 1)[-1])
        return owner == user_id
    except Exception:
        return False


async def _nav(query, context, text, reply_markup=None):
    """Edit pesan menu. Kalau pesan asal adalah foto banner /start,
    hapus foto lalu kirim pesan teks baru (foto muncul hanya di /start).
    Return objek Message hasil (untuk dihapus nanti kalau perlu)."""
    msg = query.message
    if msg is not None and getattr(msg, "photo", None):
        try:
            await msg.delete()
        except Exception:
            pass
        return await context.bot.send_message(
            chat_id=msg.chat_id, text=text, parse_mode=ParseMode.HTML,
            reply_markup=reply_markup)
    else:
        return await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data or ""
    await query.answer()

    if not _gate(user_id):
        return
    # ownership: hanya pemilik tombol
    if not _cb_owner_ok(data, user_id):
        await query.answer("Ini bukan menu kamu.", show_alert=True)
        return

    try:
        context.user_data["dz_origin"] = _describe_origin(update.effective_chat)
    except Exception:
        pass

    if data.startswith("dz_home_"):
        await _nav(query, context,
            _welcome_text(query.from_user, user_id),
            reply_markup=_main_menu_kb(user_id))
        return

    if data.startswith("dz_close_"):
        try:
            await query.message.delete()
        except Exception:
            try:
                await query.edit_message_text(
                    _screen("brand", "DITUTUP", _q(f"{sym('ok')} Menu ditutup.")),
                    parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return

    if data.startswith("dz_fixmenu_"):
        await _nav(query, context,
            _fix_menu_text(user_id),
            reply_markup=_fix_menu_kb(user_id))
        return

    if data.startswith("dz_guide_"):
        await _nav(query, context,
            _panduan_text(user_id),
            reply_markup=_back_kb(user_id))
        return

    if data.startswith("dz_user_"):
        await _nav(query, context,
            _user_menu_text(query.from_user, user_id),
            reply_markup=_user_menu_kb(user_id))
        return

    if data.startswith("dz_profile_"):
        await query.edit_message_text(
            _profile_text(query.from_user, user_id), parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                _btn("Kembali", f"dz_user_{user_id}", "danger")]]))
        return

    if data.startswith("dz_toklen_"):
        if is_owner(user_id):
            body = _q(
                f"{sym('token')} SALDO TOKEN\n"
                f"Akses    : Owner — unlimited Reset OTP\n"
                f"Token    : tidak diperlukan\n"
                f"Trial    : tidak berlaku")
        else:
            tok = get_tokens(user_id)
            tl = trial_left(user_id)
            pakai = tok // RESETOTP_TOKEN_COST if RESETOTP_TOKEN_COST else 0
            body = _q(
                f"{sym('token')} SALDO TOKEN\n"
                f"Token    : {tok} (~{pakai}x Reset OTP)\n"
                f"Biaya    : {RESETOTP_TOKEN_COST} token / run\n"
                f"Trial    : {tl}/{RESETOTP_TRIAL_LIMIT}")
        await _nav(query, context,
            _screen("token", "CEK TOKEN", body),
            reply_markup=InlineKeyboardMarkup([[
                _btn("Beli Premium", f"dz_buyprem_{user_id}", "success"),
                _btn("Kembali", f"dz_user_{user_id}", "danger")]]))
        return

    if data.startswith("dz_buyprem_"):
        if is_owner(user_id):
            await _nav(query, context,
                _screen("prem", "PREMIUM RESET OTP",
                        _q(f"{sym('ok')} Kamu Owner — akses Reset OTP unlimited.")),
                reply_markup=InlineKeyboardMarkup([[
                    _btn("Kembali", f"dz_user_{user_id}", "danger")]]))
            return
        await _nav(query, context,
            _buyprem_text(user_id),
            reply_markup=_prem_buy_kb(user_id))
        return

    if data.startswith("dz_prem_buy_"):
        await _prem_buy(query, context, data, user_id)
        return

    if data.startswith("dz_prem_check_"):
        await _prem_check(query, context, user_id)
        return

    if data.startswith("dz_prem_cancel_"):
        context.user_data.pop(f"prem_inv_{user_id}", None)
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=_buyprem_text(user_id), parse_mode=ParseMode.HTML,
            reply_markup=_prem_buy_kb(user_id))
        return

    if data.startswith("dz_fix1_") or data.startswith("dz_fix2_"):
        server = 1 if data.startswith("dz_fix1_") else 2
        _pending[user_id] = f"fix{server}"
        prompt_msg = await _nav(query, context,
            _screen("fix", f"FIX SERVER {server}", _q(
                "Kirim nomor WhatsApp target.\n"
                f"Satu per baris · maks {MAX_FIX_NUMBERS} nomor.")
                + "\nContoh:\n<code>628123456789</code>"),
            reply_markup=_back_kb(user_id))
        # Simpan pesan prompt agar bisa di-edit jadi pesan proses (tidak
        # menyisakan pesan "Kirim nomor..." setelah user kirim nomor).
        try:
            context.user_data[f"fix_prompt_{user_id}"] = (
                prompt_msg.chat_id, prompt_msg.message_id)
        except Exception:
            context.user_data.pop(f"fix_prompt_{user_id}", None)
        return

    # ── /fix +nomor → pilih fitur langsung (nomor sudah tersimpan) ──
    if (data.startswith("dz_fixnum1_") or data.startswith("dz_fixnum2_")
            or data.startswith("dz_fixnumotp_")):
        numbers = context.user_data.get(f"fix_numbers_{user_id}", [])
        if not numbers:
            await _nav(query, context,
                _screen("fix", "MENU FIX", _q(f"{sym('no')} Data nomor hilang. Ketik /fix lagi.")),
                reply_markup=_fix_menu_kb(user_id))
            return
        if data.startswith("dz_fixnumotp_"):
            if resetotp_access_mode(user_id) == "none":
                await _nav(query, context,
                    _buyprem_text(user_id), reply_markup=_prem_buy_kb(user_id))
                return
            context.user_data[f"otp_numbers_{user_id}"] = numbers
            context.user_data.pop(f"fix_numbers_{user_id}", None)
            context.user_data.pop(f"otp_mode_{user_id}", None)
            context.user_data.pop(f"otp_limit_{user_id}", None)
            await _nav(query, context,
                _otp_mode_text(user_id, len(numbers)),
                reply_markup=_styled_kb([
                    [("EASY", f"dz_otpmode_easy_{user_id}"), ("HARD", f"dz_otpmode_hard_{user_id}")],
                    [("BATAL", f"dz_otpcancel_{user_id}")]]))
            return
        server = 1 if data.startswith("dz_fixnum1_") else 2
        context.user_data.pop(f"fix_numbers_{user_id}", None)
        _pending.pop(user_id, None)
        msg = query.message
        try:
            if getattr(msg, "photo", None):
                await msg.delete()
                msg = None
        except Exception:
            msg = None
        if msg is None:
            msg = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=_screen("brand", "MEMPROSES", _q(f"{sym('wait')} Menyiapkan {len(numbers)} nomor...")),
                parse_mode=ParseMode.HTML)
        else:
            try:
                await msg.edit_text(
                    _screen("brand", "MEMPROSES", _q(f"{sym('wait')} Menyiapkan {len(numbers)} nomor...")),
                    parse_mode=ParseMode.HTML)
            except Exception:
                pass
        asyncio.create_task(_process_fix(numbers, user_id, msg, context, server))
        return

    if data.startswith("dz_otp_"):
        # Gate akses: kalau semua jatah habis → tampilkan menu beli (Bayar).
        if resetotp_access_mode(user_id) == "none":
            await _nav(query, context,
                _buyprem_text(user_id),
                reply_markup=_prem_buy_kb(user_id))
            return
        _pending[user_id] = "otp"
        prompt_msg = await _nav(query, context,
            _screen("otp", "RESET OTP", _q(
                "Kirim nomor WhatsApp target, satu per baris.\n"
                "Sistem kirim Banding + Form V1 + Form V2 paralel.")
                + _q(_resetotp_status_line(user_id))),
            reply_markup=_back_kb(user_id))
        try:
            context.user_data[f"fix_prompt_{user_id}"] = (
                prompt_msg.chat_id, prompt_msg.message_id)
        except Exception:
            context.user_data.pop(f"fix_prompt_{user_id}", None)
        return

    # ── Wizard Reset OTP: pilih EASY / HARD ──
    if data.startswith("dz_otpmode_easy_") or data.startswith("dz_otpmode_hard_"):
        mode = "easy" if data.startswith("dz_otpmode_easy_") else "hard"
        numbers = context.user_data.get(f"otp_numbers_{user_id}", [])
        if not numbers:
            await _nav(query, context,
                _screen("otp", "RESET OTP", _q(f"{sym('no')} Data nomor hilang. Buka Reset OTP lagi.")),
                reply_markup=_back_kb(user_id))
            return
        context.user_data[f"otp_mode_{user_id}"] = mode
        await _nav(query, context,
            _otp_limit_text(mode, len(numbers)),
            reply_markup=_styled_kb([
                [("OTOMATIS 24 jam", f"dz_otplim_auto_{user_id}")],
                [("CUSTOM", f"dz_otplim_custom_{user_id}")],
                [("BATAL", f"dz_otpcancel_{user_id}")]]))
        return

    # ── Wizard Reset OTP: limit otomatis 24 jam ──
    if data.startswith("dz_otplim_auto_"):
        context.user_data[f"otp_limit_{user_id}"] = 24
        context.user_data.pop(f"otp_waitlim_{user_id}", None)
        mode = context.user_data.get(f"otp_mode_{user_id}", "hard")
        numbers = context.user_data.get(f"otp_numbers_{user_id}", [])
        await _nav(query, context,
            _otp_confirm_text(mode, 24, len(numbers)),
            reply_markup=_styled_kb([
                [("PROSES", f"dz_otpgo_{user_id}")],
                [("BATAL", f"dz_otpcancel_{user_id}")]]))
        return

    # ── Wizard Reset OTP: minta angka limit custom ──
    if data.startswith("dz_otplim_custom_"):
        context.user_data[f"otp_waitlim_{user_id}"] = True
        mode = context.user_data.get(f"otp_mode_{user_id}", "hard")
        await _nav(query, context,
            _otp_custom_text(mode), reply_markup=_back_kb(user_id))
        return

    # ── Wizard Reset OTP: BATAL ──
    if data.startswith("dz_otpcancel_"):
        for k in ("otp_numbers_", "otp_mode_", "otp_limit_", "otp_waitlim_"):
            context.user_data.pop(f"{k}{user_id}", None)
        _pending.pop(user_id, None)
        await _nav(query, context,
            _fix_menu_text(user_id), reply_markup=_fix_menu_kb(user_id))
        return

    # ── Wizard Reset OTP: PROSES ──
    if data.startswith("dz_otpgo_"):
        if resetotp_access_mode(user_id) == "none":
            await _nav(query, context,
                _buyprem_text(user_id), reply_markup=_prem_buy_kb(user_id))
            return
        numbers = context.user_data.pop(f"otp_numbers_{user_id}", [])
        mode = context.user_data.pop(f"otp_mode_{user_id}", "hard")
        limit_h = int(context.user_data.pop(f"otp_limit_{user_id}", 24) or 24)
        context.user_data.pop(f"otp_waitlim_{user_id}", None)
        _pending.pop(user_id, None)
        if not numbers:
            await _nav(query, context,
                _screen("otp", "RESET OTP", _q(f"{sym('no')} Data nomor hilang. Buka Reset OTP lagi.")),
                reply_markup=_back_kb(user_id))
            return
        await _nav(query, context,
            _screen("otp", "RESET OTP · PROSES",
                    _q(f"{sym('wait')} Mode <b>{mode.upper()}</b> · limit {limit_h} jam — "
                       f"memulai {len(numbers)} nomor...")))
        msg = query.message
        # kalau pesan asal foto sudah dihapus oleh _nav, ambil pesan teks baru
        try:
            if getattr(msg, "photo", None):
                msg = await context.bot.send_message(
                    chat_id=msg.chat_id,
                    text=_screen("otp", "RESET OTP · PROSES",
                                 _q(f"{sym('wait')} Menyiapkan {len(numbers)} nomor...")),
                    parse_mode=ParseMode.HTML)
        except Exception:
            pass
        asyncio.create_task(_process_resetotp(
            numbers, user_id, msg, context, mode=mode, limit_hours=limit_h))
        return

    if data.startswith("dz_owner_"):
        if not (is_owner(user_id) or is_admin(user_id)):
            await query.answer("Khusus owner.", show_alert=True)
            return
        await _nav(query, context,
            _owner_panel_text(),
            reply_markup=_owner_kb(user_id))
        return

    # Owner panel actions
    if data.startswith("dzo_"):
        if not (is_owner(user_id) or is_admin(user_id)):
            await query.answer("Khusus owner.", show_alert=True)
            return
        await _owner_action(query, context, data, user_id)
        return


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _gate(user_id):
        return

    # Owner sedang input command panel?
    owner_state = context.user_data.get("dzo_await")
    if owner_state and (is_owner(user_id) or is_admin(user_id)):
        await _owner_text_input(update, context, owner_state)
        return

    action = _pending.get(user_id)
    # Owner/user sedang isi limit jam custom (Reset OTP wizard)?
    if context.user_data.get(f"otp_waitlim_{user_id}"):
        raw = (update.message.text or "").strip()
        m = re.search(r"\d{1,3}", raw)
        if not m:
            await update.message.reply_text(
                _screen("otp", "LIMIT JAM", _q(f"{sym('no')} Kirim angka jam saja, contoh 5 / 12 / 24.")),
                parse_mode=ParseMode.HTML)
            return
        limit_h = max(1, min(72, int(m.group())))
        context.user_data[f"otp_limit_{user_id}"] = limit_h
        context.user_data.pop(f"otp_waitlim_{user_id}", None)
        numbers = context.user_data.get(f"otp_numbers_{user_id}", [])
        mode = context.user_data.get(f"otp_mode_{user_id}", "hard")
        await update.message.reply_text(
            _otp_confirm_text(mode, limit_h, len(numbers)), parse_mode=ParseMode.HTML,
            reply_markup=_styled_kb([
                [("PROSES", f"dz_otpgo_{user_id}")],
                [("BATAL", f"dz_otpcancel_{user_id}")]]))
        return

    if not action:
        return

    numbers = _parse_numbers(update.message.text)
    if not numbers:
        await update.message.reply_text(
            _screen("brand", "NOMOR TIDAK VALID",
                    _q(f"{sym('no')} Kirim minimal 1 nomor (angka, ≥8 digit).")),
            parse_mode=ParseMode.HTML)
        return
    if len(numbers) > MAX_FIX_NUMBERS:
        numbers = numbers[:MAX_FIX_NUMBERS]

    _pending.pop(user_id, None)
    # Rapikan chat: hapus pesan berisi nomor dari user (mirip dik.py).
    try:
        await update.message.delete()
    except Exception:
        pass
    try:
        context.user_data["dz_origin"] = _describe_origin(update.effective_chat)
    except Exception:
        pass

    # Pesan prompt "Kirim nomor..." sebelumnya — kita EDIT jadi pesan berikutnya
    # supaya tidak menyisakan prompt menggantung di chat.
    prompt_ref = context.user_data.pop(f"fix_prompt_{user_id}", None)

    async def _consume_prompt(text):
        """Edit pesan prompt jadi `text` dan kembalikan objeknya. Kalau gagal
        (mis. sudah dihapus), kirim pesan baru."""
        if prompt_ref:
            chat_id, message_id = prompt_ref
            try:
                return await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text=text, parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text, parse_mode=ParseMode.HTML)

    # Reset OTP: buka wizard EASY/HARD dulu (mirip dik.py), jangan langsung proses.
    if action == "otp":
        context.user_data[f"otp_numbers_{user_id}"] = numbers
        context.user_data.pop(f"otp_mode_{user_id}", None)
        context.user_data.pop(f"otp_limit_{user_id}", None)
        context.user_data.pop(f"otp_waitlim_{user_id}", None)
        # Edit prompt jadi wizard EASY/HARD (butuh reply_markup → hapus prompt
        # lalu kirim baru kalau ada; edit_message_text tidak set keyboard di sini).
        if prompt_ref:
            try:
                await context.bot.delete_message(prompt_ref[0], prompt_ref[1])
            except Exception:
                pass
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=_otp_mode_text(user_id, len(numbers)), parse_mode=ParseMode.HTML,
            reply_markup=_styled_kb([
                [("EASY", f"dz_otpmode_easy_{user_id}"), ("HARD", f"dz_otpmode_hard_{user_id}")],
                [("BATAL", f"dz_otpcancel_{user_id}")]]))
        return

    msg = await _consume_prompt(
        _screen("brand", "MEMPROSES", _q(f"{sym('wait')} Menyiapkan {len(numbers)} nomor...")))

    if action in ("fix1", "fix2"):
        server = 1 if action == "fix1" else 2
        asyncio.create_task(_process_fix(numbers, user_id, msg, context, server))


async def fix_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/fix — penjelasan pemakaian. /fix +628xxx → langsung pilih fitur
    dgn nomor sudah terisi."""
    user_id = update.effective_user.id
    if not _gate(user_id):
        await _deny(update.message)
        return
    try:
        context.user_data["dz_origin"] = _describe_origin(update.effective_chat)
    except Exception:
        pass
    raw = update.message.text or ""
    arg = re.sub(r"^/fix(?:@\S+)?", "", raw, count=1).strip()
    numbers = _parse_numbers(arg) if arg else []

    if not numbers:
        body = _q(
            f"{sym('fix')} CARA PAKAI /fix\n"
            f"{_DOT} <code>/fix +628xxxxxxxx</code> — kirim nomor langsung,\n"
            f"   bot lanjut ke pilihan Server 1 / Server 2 / Reset OTP.\n"
            f"{_DOT} <code>/fix</code> saja — buka Menu Fix (pilih fitur dulu).\n"
            f"{_DOT} Banyak nomor: pisah spasi/baris, maks {MAX_FIX_NUMBERS}."
        ) + _q(
            f"{sym('menu')} FITUR\n"
            f"{_DOT} Server 1 / Server 2 — fix WhatsApp merah (banding).\n"
            f"{_DOT} Reset OTP — banding + Form V1/V2 (mode EASY/HARD)."
        )
        await update.message.reply_text(
            _screen("fix", "MENU FIX", body), parse_mode=ParseMode.HTML,
            reply_markup=_fix_menu_kb(user_id))
        return

    if len(numbers) > MAX_FIX_NUMBERS:
        numbers = numbers[:MAX_FIX_NUMBERS]
    context.user_data[f"fix_numbers_{user_id}"] = numbers
    listing = "\n".join(f"{_DOT} +{n}" for n in numbers)
    body = _q(f"{sym('nomor')} {len(numbers)} nomor siap diproses:\n{listing}") \
        + _q(f"{sym('menu')} Pilih fitur di bawah.")
    await update.message.reply_text(
        _screen("fix", "PILIH FITUR", body), parse_mode=ParseMode.HTML,
        reply_markup=_styled_kb([
            [("Server 1", f"dz_fixnum1_{user_id}"), ("Server 2", f"dz_fixnum2_{user_id}")],
            [(_resetotp_label(user_id), f"dz_fixnumotp_{user_id}")],
            [("Batal", f"dz_otpcancel_{user_id}")],
        ]))


async def buyprem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/buyprem — beli akses/token Reset OTP (semua user)."""
    user_id = update.effective_user.id
    if not _gate(user_id):
        await _deny(update.message)
        return
    if is_owner(user_id):
        await update.message.reply_text(
            _screen("prem", "PREMIUM RESET OTP",
                    _q(f"{sym('ok')} Kamu Owner — akses Reset OTP unlimited.")),
            parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(
        _buyprem_text(user_id), parse_mode=ParseMode.HTML,
        reply_markup=_prem_buy_kb(user_id))


# ══════════════════════════════════════════════════════════════════════
#  PEMBAYARAN QRIS (MG Cloud Pay) — sama alur dgn bot utama
# ══════════════════════════════════════════════════════════════════════
async def _prem_buy(query, context, data, user_id):
    import functools
    try:
        pk = data[len("dz_prem_buy_"):].rsplit("_", 1)[0]
    except Exception:
        pk = None
    info = _pkg_info(pk)
    if not info:
        await query.answer("Paket tidak dikenal.", show_alert=True)
        return
    label = info["label"]
    price = info["price"]
    days = info["days"]
    is_token = info["kind"] == "token"
    tokens_credit = (info["tokens"] + info.get("bonus", 0)) if is_token else 0

    # Reuse invoice pending yang masih hidup utk paket sama.
    reuse = None
    try:
        cur.execute(
            "SELECT invoice_id, amount, fee, total, qris_image, payment_link, "
            "expired_at FROM premium_invoices WHERE user_id = ? AND package = ? "
            "AND status = 'pending' ORDER BY id DESC LIMIT 1", (user_id, pk))
        r = cur.fetchone()
        if r:
            exp = _parse_iso(r[6])
            if exp and exp > _now_utc() + timedelta(minutes=1):
                reuse = r
    except Exception:
        reuse = None

    if reuse:
        inv = {"invoice_id": reuse[0], "amount": reuse[1], "fee": reuse[2],
               "total": reuse[3], "qris_image": reuse[4],
               "payment_link": reuse[5], "expired_at": reuse[6]}
    else:
        try:
            await query.edit_message_text(
                _screen("prem", "MEMBUAT INVOICE",
                        _q(f"{sym('wait')} Membuat invoice {label}...")),
                parse_mode=ParseMode.HTML)
        except Exception:
            pass
        inv = await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(_mgpay_create_invoice, price))

    if not inv:
        kb = InlineKeyboardMarkup([
            [_btn("Coba Lagi", f"dz_prem_buy_{pk}_{user_id}", "primary")],
            [_btn("Kembali", f"dz_user_{user_id}", "danger")]])
        try:
            await query.edit_message_text(
                _screen("prem", "GAGAL BUAT INVOICE",
                        _q(f"{sym('no')} Gateway sedang sibuk. Coba lagi sebentar.")),
                parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=_screen("prem", "GAGAL BUAT INVOICE",
                             _q(f"{sym('no')} Gateway sedang sibuk. Coba lagi.")),
                parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    inv_id = inv.get("invoice_id")
    amount = int(inv.get("amount") or price)
    fee = int(inv.get("fee") or 0)
    total = int(inv.get("total") or (amount + fee))
    qris = inv.get("qris_image") or ""
    exp_at = inv.get("expired_at") or ""

    if not reuse:
        try:
            cur.execute("""
                INSERT OR REPLACE INTO premium_invoices
                (user_id, invoice_id, package, kind, days, tokens, amount, fee, total,
                 status, qris_image, payment_link, expired_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """, (user_id, inv_id, pk, info["kind"], days, tokens_credit,
                  amount, fee, total, qris, inv.get("payment_link") or "", exp_at))
            conn.commit()
            _web_push_invoice(inv_id)
        except Exception as e:
            logging.warning("[PREMIUM] simpan invoice err: %s", e)

    context.user_data[f"prem_inv_{user_id}"] = inv_id

    if is_token:
        item_line = f"  {_DOT} Token  {tokens_credit} token"
        aktif_note = f"<i>{tokens_credit} token masuk otomatis setelah terdeteksi.</i>"
    else:
        item_line = f"  {_DOT} Paket  {label} (langganan)"
        aktif_note = f"<i>Langganan {label} aktif otomatis setelah terdeteksi.</i>"

    cap = _screen("prem", "PEMBAYARAN QRIS", _q(
        f"{sym('otp')} Scan QRIS untuk bayar\n"
        f"{item_line}\n"
        f"  {_DOT} Harga  {_rp(amount)}\n"
        f"  {_DOT} Fee  {_rp(fee)}\n"
        f"  {_DOT} Total  {_rp(total)}\n"
        f"  {_DOT} Invoice  {_html.escape(str(inv_id))}\n"
        f"  {_DOT} Expired  {_fmt_wib(exp_at)}\n\n"
        f"Setelah transfer, tekan SUDAH BAYAR.\n{aktif_note}"))

    kb_pay = InlineKeyboardMarkup([
        [_btn("Sudah Bayar", f"dz_prem_check_{user_id}", "success")],
        [_btn("Batal", f"dz_prem_cancel_{user_id}", "danger")]])

    try:
        await query.message.delete()
    except Exception:
        pass
    if qris:
        try:
            await context.bot.send_photo(
                chat_id=query.message.chat_id, photo=qris,
                caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb_pay)
            return
        except Exception as e:
            logging.warning("[PREMIUM] kirim QRIS gagal: %s", e)
    await context.bot.send_message(
        chat_id=query.message.chat_id, text=cap,
        parse_mode=ParseMode.HTML, reply_markup=kb_pay)


async def _prem_check(query, context, user_id):
    import functools
    inv_id = context.user_data.get(f"prem_inv_{user_id}")
    if not inv_id:
        try:
            cur.execute("SELECT invoice_id FROM premium_invoices WHERE user_id = ? "
                        "AND status = 'pending' ORDER BY id DESC LIMIT 1", (user_id,))
            row = cur.fetchone()
            inv_id = row[0] if row else None
        except Exception:
            inv_id = None
    if not inv_id:
        await query.answer("Invoice tidak ditemukan. Ulangi beli.", show_alert=True)
        return

    await query.answer("Mengecek pembayaran...")
    st = await asyncio.get_running_loop().run_in_executor(
        None, functools.partial(_mgpay_check_status, inv_id))
    if not st:
        await query.answer("Gateway tidak merespons, coba lagi.", show_alert=True)
        return

    status = str(st.get("status") or "").lower()
    if status in ("paid", "success", "settled", "completed"):
        try:
            cur.execute("SELECT package, kind, days, tokens, total FROM premium_invoices "
                        "WHERE invoice_id = ?", (inv_id,))
            row = cur.fetchone()
        except Exception:
            row = None
        pk = (row[0] if row else "p1")
        kind = (row[1] if row and row[1] else "days")
        days = int((row[2] if row else 1) or 1)
        tokens = int((row[3] if row else 0) or 0)
        total = int((row[4] if row else 0) or 0)
        info = _pkg_info(pk)
        label = info["label"] if info else f"{days} Hari"
        is_token = (kind == "token")

        claimed = False
        try:
            cur.execute("UPDATE premium_invoices SET status = 'paid', paid_at = ? "
                        "WHERE invoice_id = ? AND status = 'pending'",
                        (_iso(_now_utc()), inv_id))
            conn.commit()
            claimed = cur.rowcount > 0
        except Exception:
            pass
        if claimed:
            _web_push_invoice(inv_id)

        if is_token:
            new_bal = add_tokens(user_id, tokens, ref=inv_id, note=f"beli {label}") if claimed else get_tokens(user_id)
            txt_ok = _screen("prem", "PEMBAYARAN BERHASIL", _q(
                f"{sym('ok')} Token berhasil ditambahkan!\n"
                f"  {_DOT} Paket  {label}\n"
                f"  {_DOT} Token masuk  +{tokens}\n"
                f"  {_DOT} Saldo token  {new_bal}\n"
                f"  {_DOT} Total  {_rp(total)}\n\n"
                f"<i>1x Reset OTP = {RESETOTP_TOKEN_COST} token.</i>"))
        else:
            new_exp = grant_premium(user_id, days, invoice_id=inv_id, amount=total) if claimed else (get_premium(user_id) or {}).get("expired_at")
            txt_ok = _screen("prem", "PEMBAYARAN BERHASIL", _q(
                f"{sym('ok')} Akses Reset OTP aktif!\n"
                f"  {_DOT} Paket  {label}\n"
                f"  {_DOT} Total  {_rp(total)}\n"
                f"  {_DOT} Aktif s/d  {_fmt_wib(new_exp)}\n\n"
                f"<i>Terima kasih! Silakan lanjut proses Reset OTP.</i>"))
        context.user_data.pop(f"prem_inv_{user_id}", None)
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id, text=txt_ok, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                _btn("Menu Fix", f"dz_fixmenu_{user_id}", "success"),
                _btn("Menu", f"dz_home_{user_id}", "primary")]]))
        return

    if status in ("expired", "cancelled", "canceled", "failed"):
        try:
            cur.execute("UPDATE premium_invoices SET status = ? WHERE invoice_id = ?",
                        ("expired" if status.startswith("expire") else "cancelled", inv_id))
            conn.commit()
        except Exception:
            pass
        context.user_data.pop(f"prem_inv_{user_id}", None)
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=_screen("prem", "INVOICE KEDALUWARSA", _q(
                f"{sym('no')} Invoice sudah tidak berlaku.\n"
                f"  {_DOT} Invoice  {_html.escape(str(inv_id))}\n"
                f"  {_DOT} Status  {_html.escape(status)}\n\n"
                f"<i>Silakan buat invoice baru.</i>")),
            parse_mode=ParseMode.HTML, reply_markup=_prem_buy_kb(user_id))
        return

    await query.answer(
        "Pembayaran belum terdeteksi. Pastikan sudah transfer, lalu tekan lagi.",
        show_alert=True)


# ══════════════════════════════════════════════════════════════════════
#  OWNER PANEL
# ══════════════════════════════════════════════════════════════════════
def _owner_stats():
    allowed = banned = premium = 0
    runs_today = 0
    reply_today = sent_today = 0
    try:
        cur.execute("SELECT COUNT(*) FROM allowed_users")
        allowed = int((cur.fetchone() or [0])[0] or 0)
        cur.execute("SELECT COUNT(*) FROM banned_users")
        banned = int((cur.fetchone() or [0])[0] or 0)
        cur.execute("SELECT COUNT(*) FROM premium_access WHERE expired_at > ?",
                    (_iso(_now_utc()),))
        premium = int((cur.fetchone() or [0])[0] or 0)
        cur.execute("SELECT COUNT(*), COALESCE(SUM(sent_count),0), COALESCE(SUM(reply_count),0) "
                    "FROM fix_runs WHERE date(created_at) = date('now')")
        r = cur.fetchone()
        if r:
            runs_today = int(r[0] or 0)
            sent_today = int(r[1] or 0)
            reply_today = int(r[2] or 0)
    except Exception:
        pass
    rate = int(reply_today / sent_today * 100) if sent_today else 0
    return allowed, banned, premium, runs_today, rate


def _owner_panel_text():
    allowed, banned, premium, runs, rate = _owner_stats()
    body = _q(
        f"User    : {allowed} allowed · {banned} banned\n"
        f"Premium : {premium} aktif\n"
        f"Fix hari ini : {runs} run · {rate}% sukses"
    )
    return _screen("owner", "OWNER PANEL — DikZzCode", body)


def _owner_kb(user_id):
    # Kolom kiri success, kanan primary; Maintenance ikut status
    # (ON = danger, OFF = success); baris terakhir (Kembali) danger.
    maint_style = "danger" if MAINTENANCE_MODE else "success"
    maint_label = "Maintenance: ON" if MAINTENANCE_MODE else "Maintenance: OFF"
    return InlineKeyboardMarkup([
        [_btn("Add User", f"dzo_adduser_{user_id}", "success"),
         _btn("Del User", f"dzo_deluser_{user_id}", "primary")],
        [_btn("Ban", f"dzo_ban_{user_id}", "success"),
         _btn("Unban", f"dzo_unban_{user_id}", "primary")],
        [_btn("Set Premium", f"dzo_setprem_{user_id}", "success"),
         _btn("Add Token", f"dzo_addtoken_{user_id}", "primary")],
        [_btn("Broadcast", f"dzo_broadcast_{user_id}", "success"),
         _btn("Statistik", f"dzo_stats_{user_id}", "primary")],
        [_btn(maint_label, f"dzo_maint_{user_id}", maint_style)],
        [_btn("Kembali", f"dz_home_{user_id}", "danger")],
    ])


_OWNER_PROMPTS = {
    "adduser": ("ADD USER", "Kirim user_id yang mau ditambahkan."),
    "deluser": ("DEL USER", "Kirim user_id yang mau dihapus."),
    "ban": ("BAN", "Kirim user_id yang mau di-ban."),
    "unban": ("UNBAN", "Kirim user_id yang mau di-unban."),
    "setprem": ("SET PREMIUM", "Kirim: <code>user_id hari</code> (contoh: 123456 7)."),
    "addtoken": ("ADD TOKEN", "Kirim: <code>user_id jumlah</code> (contoh: 123456 15)."),
    "broadcast": ("BROADCAST", "Kirim teks pesan yang mau dibroadcast ke semua user."),
}


async def _owner_action(query, context, data, user_id):
    # data: dzo_<key>_<owner_id>
    key = data[len("dzo_"):].rsplit("_", 1)[0]
    if key == "maint":
        global MAINTENANCE_MODE
        MAINTENANCE_MODE = not MAINTENANCE_MODE
        await query.edit_message_text(
            _screen("owner", "MAINTENANCE",
                    _q(f"Maintenance mode: <b>{'ON' if MAINTENANCE_MODE else 'OFF'}</b>\n"
                       f"(non-owner {'diblok' if MAINTENANCE_MODE else 'boleh pakai'})")),
            parse_mode=ParseMode.HTML, reply_markup=_owner_kb(user_id))
        return
    if key == "stats":
        await query.edit_message_text(_owner_panel_text(), parse_mode=ParseMode.HTML,
                                      reply_markup=_owner_kb(user_id))
        return
    prompt = _OWNER_PROMPTS.get(key)
    if not prompt:
        return
    context.user_data["dzo_await"] = key
    await query.edit_message_text(
        _screen("owner", prompt[0], _q(prompt[1])),
        parse_mode=ParseMode.HTML, reply_markup=_back_kb(user_id))


async def _owner_text_input(update, context, key):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    context.user_data.pop("dzo_await", None)
    reply = ""
    try:
        if key == "adduser":
            uid = int(text)
            cur.execute("INSERT OR IGNORE INTO allowed_users (user_id, added_by) VALUES (?, ?)",
                        (uid, user_id))
            conn.commit()
            reply = f"{sym('ok')} User {uid} ditambahkan."
        elif key == "deluser":
            uid = int(text)
            cur.execute("DELETE FROM allowed_users WHERE user_id = ?", (uid,))
            conn.commit()
            reply = f"{sym('ok')} User {uid} dihapus."
        elif key == "ban":
            uid = int(text)
            cur.execute("INSERT OR IGNORE INTO banned_users (user_id, banned_by) VALUES (?, ?)",
                        (uid, user_id))
            conn.commit()
            reply = f"{sym('ok')} User {uid} di-ban."
        elif key == "unban":
            uid = int(text)
            cur.execute("DELETE FROM banned_users WHERE user_id = ?", (uid,))
            conn.commit()
            reply = f"{sym('ok')} User {uid} di-unban."
        elif key == "setprem":
            parts = text.split()
            uid, days = int(parts[0]), int(parts[1])
            exp = _iso(_now_utc() + timedelta(days=days))
            cur.execute("""INSERT INTO premium_access (user_id, expired_at, package, days, granted_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET expired_at=excluded.expired_at,
                    package=excluded.package, days=excluded.days, updated_at=excluded.updated_at""",
                (uid, exp, f"{days} Hari", days, user_id, _iso(_now_utc())))
            conn.commit()
            _web_push_premium(uid)
            reply = f"{sym('ok')} Premium {days} hari untuk {uid}."
        elif key == "addtoken":
            parts = text.split()
            uid, amt = int(parts[0]), int(parts[1])
            bal = add_tokens(uid, amt, ref=f"grant:{user_id}", note="owner grant")
            reply = f"{sym('ok')} +{amt} token untuk {uid} (saldo {bal})."
        elif key == "broadcast":
            asyncio.create_task(_do_broadcast(context, text))
            reply = f"{sym('ok')} Broadcast dimulai..."
    except Exception as e:
        reply = f"{sym('no')} Gagal: {_html.escape(str(e))[:120]}"

    await update.message.reply_text(
        _screen("owner", "HASIL", _q(reply)), parse_mode=ParseMode.HTML,
        reply_markup=_main_menu_kb(user_id))


async def _do_broadcast(context, text):
    try:
        cur.execute("SELECT user_id FROM allowed_users")
        ids = [r[0] for r in cur.fetchall()]
    except Exception:
        ids = []
    sent = 0
    body = _screen("brand", "PENGUMUMAN", _q(_html.escape(text)))
    for uid in ids:
        try:
            await context.bot.send_message(uid, body, parse_mode=ParseMode.HTML)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    logging.info("[BROADCAST] terkirim ke %s/%s user", sent, len(ids))


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
async def _post_init(application):
    global BOT_USERNAME
    try:
        me = await application.bot.get_me()
        if me.username:
            BOT_USERNAME = me.username
        logging.info("Bot online: @%s (id=%s)", me.username, me.id)
    except Exception as e:
        logging.warning("get_me gagal: %s", e)


async def _on_error(update, context):
    """Error handler global: telan error jaringan (httpx/telegram) yang tidak
    fatal supaya tidak dump traceback panjang ke console. Error lain diringkas."""
    err = context.error
    name = type(err).__name__
    # Error transient jaringan/polling — cukup 1 baris, tidak perlu traceback.
    transient = (
        "ReadError", "ConnectError", "ConnectTimeout", "ReadTimeout",
        "WriteError", "PoolTimeout", "RemoteProtocolError", "TimedOut",
        "NetworkError", "ConnectTimeoutError",
    )
    if name in transient:
        logging.warning("[NET] %s: %s (diabaikan, akan retry otomatis)", name, err)
        return
    logging.error("[ERROR] %s: %s", name, err)


def main():
    print("=" * 56)
    print("  DikZzCode BOT — Fix Server 1/2 + Reset OTP")
    print(f"  DB: {DB_PATH}")
    print("=" * 56)
    _ensure_tables()

    # DB utama ada di website (Cloudflare D1). Kalau file lokal hilang/kereset,
    # premium/token/trial ditarik ulang dari sana.
    try:
        web_restore_from_remote()
    except Exception as e:
        print(f"  [WEB] restore startup error: {e}")

    request_obj = HTTPXRequest(connection_pool_size=128, connect_timeout=30.0,
                               read_timeout=30.0, write_timeout=30.0, pool_timeout=10.0)
    getup_obj = HTTPXRequest(connection_pool_size=128, connect_timeout=30.0,
                             read_timeout=40.0, write_timeout=30.0, pool_timeout=10.0)

    app = (Application.builder()
           .token(TELEGRAM_BOT_TOKEN)
           .request(request_obj)
           .get_updates_request(getup_obj)
           .concurrent_updates(True)
           .post_init(_post_init)
           .build())

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("fix", fix_command))
    app.add_handler(CommandHandler("buyprem", buyprem_command))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(_on_error)

    print("  Running (polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()







