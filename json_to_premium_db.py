"""json_to_premium_db.py — Convert JSON download dari web dashboard menjadi premium_bot.db.

Usage:
    python json_to_premium_db.py premium_bot_2026-09-03.json

Akan menghasilkan premium_bot.db di folder yang sama.
"""
import json
import sqlite3
import sys
from pathlib import Path


def json_to_db(json_path: str, output_path: str = "premium_bot.db"):
    json_path = Path(json_path)
    if not json_path.exists():
        print(f"Error: File {json_path} tidak ditemukan")
        return False

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if data.get("format") != "premium_bot_db_v1":
        print("Error: Format JSON tidak dikenal (bukan premium_bot_db_v1)")
        return False

    tables = data.get("tables", {})
    print(f"Loading data dari {json_path.name}...")
    print(f"  Exported at: {data.get('exported_at', 'unknown')}")

    # Create/open database
    conn = sqlite3.connect(output_path, timeout=30)
    cur = conn.cursor()

    # Create tables
    cur.execute('''CREATE TABLE IF NOT EXISTS premium_access (
        user_id INTEGER PRIMARY KEY,
        expired_at TEXT NOT NULL,
        package TEXT,
        days INTEGER DEFAULT 0,
        invoice_id TEXT,
        granted_by INTEGER,
        total_paid INTEGER DEFAULT 0,
        buy_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS premium_invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        invoice_id TEXT UNIQUE,
        package TEXT,
        days INTEGER,
        amount INTEGER,
        fee INTEGER,
        total INTEGER,
        status TEXT NOT NULL DEFAULT 'pending',
        qris_image TEXT,
        payment_link TEXT,
        expired_at TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        paid_at TIMESTAMP
    )''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_premium_inv_user
                   ON premium_invoices(user_id, created_at DESC)''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_premium_inv_status
                   ON premium_invoices(status)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS premium_tokens (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER NOT NULL DEFAULT 0,
        total_bought INTEGER NOT NULL DEFAULT 0,
        total_spent INTEGER NOT NULL DEFAULT 0,
        total_refund INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS token_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        delta INTEGER NOT NULL,
        balance_after INTEGER NOT NULL,
        kind TEXT NOT NULL,
        ref TEXT,
        note TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_token_ledger_user
                   ON token_ledger(user_id, id DESC)''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_token_ledger_ref
                   ON token_ledger(ref)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS resetotp_trial (
        user_id INTEGER PRIMARY KEY,
        used INTEGER NOT NULL DEFAULT 0,
        first_used_at TIMESTAMP,
        last_used_at TIMESTAMP
    )''')
    conn.commit()

    # Migration: kolom baru di premium_invoices
    try:
        cur.execute("PRAGMA table_info(premium_invoices)")
        _inv_cols = {row[1] for row in cur.fetchall()}
        if 'kind' not in _inv_cols:
            cur.execute("ALTER TABLE premium_invoices ADD COLUMN kind TEXT DEFAULT 'days'")
        if 'tokens' not in _inv_cols:
            cur.execute("ALTER TABLE premium_invoices ADD COLUMN tokens INTEGER DEFAULT 0")
        conn.commit()
    except Exception as e:
        print(f"  Migration warning: {e}")

    # Insert data
    counts = {}

    # premium_access
    rows = tables.get("premium_access", [])
    for row in rows:
        try:
            cur.execute('''INSERT OR REPLACE INTO premium_access
                (user_id, expired_at, package, days, invoice_id, granted_by, total_paid, buy_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (row.get("user_id"), row.get("expired_at"), row.get("package"),
                 row.get("days", 0), row.get("invoice_id"), row.get("granted_by"),
                 row.get("total_paid", 0), row.get("buy_count", 0),
                 row.get("created_at"), row.get("updated_at")))
        except Exception as e:
            print(f"  Error premium_access user_id={row.get('user_id')}: {e}")
    counts["premium_access"] = len(rows)

    # premium_invoices
    rows = tables.get("premium_invoices", [])
    for row in rows:
        try:
            cur.execute('''INSERT OR REPLACE INTO premium_invoices
                (id, user_id, invoice_id, package, days, amount, fee, total, status, qris_image, payment_link, expired_at, created_at, paid_at, kind, tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (row.get("id"), row.get("user_id"), row.get("invoice_id"),
                 row.get("package"), row.get("days", 0), row.get("amount", 0),
                 row.get("fee", 0), row.get("total", 0), row.get("status", "pending"),
                 row.get("qris_image"), row.get("payment_link"), row.get("expired_at"),
                 row.get("created_at"), row.get("paid_at"),
                 row.get("kind", "days"), row.get("tokens", 0)))
        except Exception as e:
            print(f"  Error invoice {row.get('invoice_id')}: {e}")
    counts["premium_invoices"] = len(rows)

    # premium_tokens
    rows = tables.get("premium_tokens", [])
    for row in rows:
        try:
            cur.execute('''INSERT OR REPLACE INTO premium_tokens
                (user_id, balance, total_bought, total_spent, total_refund, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (row.get("user_id"), row.get("balance", 0), row.get("total_bought", 0),
                 row.get("total_spent", 0), row.get("total_refund", 0),
                 row.get("created_at"), row.get("updated_at")))
        except Exception as e:
            print(f"  Error premium_tokens user_id={row.get('user_id')}: {e}")
    counts["premium_tokens"] = len(rows)

    # token_ledger
    rows = tables.get("token_ledger", [])
    for row in rows:
        try:
            cur.execute('''INSERT OR REPLACE INTO token_ledger
                (id, user_id, delta, balance_after, kind, ref, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (row.get("id"), row.get("user_id"), row.get("delta", 0),
                 row.get("balance_after", 0), row.get("kind", ""),
                 row.get("ref"), row.get("note"), row.get("created_at")))
        except Exception as e:
            print(f"  Error token_ledger id={row.get('id')}: {e}")
    counts["token_ledger"] = len(rows)

    # resetotp_trial
    rows = tables.get("resetotp_trial", [])
    for row in rows:
        try:
            cur.execute('''INSERT OR REPLACE INTO resetotp_trial
                (user_id, used, first_used_at, last_used_at)
                VALUES (?, ?, ?, ?)''',
                (row.get("user_id"), row.get("used", 0),
                 row.get("first_used_at"), row.get("last_used_at")))
        except Exception as e:
            print(f"  Error resetotp_trial user_id={row.get('user_id')}: {e}")
    counts["resetotp_trial"] = len(rows)

    conn.commit()
    conn.close()

    print(f"\n✅ Berhasil convert ke {output_path}:")
    for table, count in counts.items():
        print(f"   {table}: {count} rows")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python json_to_premium_db.py <json_file> [output.db]")
        print("Contoh: python json_to_premium_db.py premium_bot_2026-09-03.json")
        sys.exit(1)

    json_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else "premium_bot.db"
    success = json_to_db(json_file, output_file)
    sys.exit(0 if success else 1)
