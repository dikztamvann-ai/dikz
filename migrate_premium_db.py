"""migrate_premium_db.py — Migrasi data premium dari ivas_bot.db ke premium_bot.db.

Jalankan sekali untuk memisahkan data premium/token ke database terpisah.
"""
import sqlite3
import sys

def migrate():
    src = sqlite3.connect('ivas_bot.db', timeout=30)
    src_cur = src.cursor()

    dst = sqlite3.connect('premium_bot.db', timeout=30)
    dst_cur = dst.cursor()

    # Create tables in premium_bot.db
    dst_cur.execute('''CREATE TABLE IF NOT EXISTS premium_access (
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
    dst_cur.execute('''CREATE TABLE IF NOT EXISTS premium_invoices (
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
    dst_cur.execute('''CREATE INDEX IF NOT EXISTS idx_premium_inv_user
                       ON premium_invoices(user_id, created_at DESC)''')
    dst_cur.execute('''CREATE INDEX IF NOT EXISTS idx_premium_inv_status
                       ON premium_invoices(status)''')
    dst_cur.execute('''CREATE TABLE IF NOT EXISTS premium_tokens (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER NOT NULL DEFAULT 0,
        total_bought INTEGER NOT NULL DEFAULT 0,
        total_spent INTEGER NOT NULL DEFAULT 0,
        total_refund INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    dst_cur.execute('''CREATE TABLE IF NOT EXISTS token_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        delta INTEGER NOT NULL,
        balance_after INTEGER NOT NULL,
        kind TEXT NOT NULL,
        ref TEXT,
        note TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    dst_cur.execute('''CREATE INDEX IF NOT EXISTS idx_token_ledger_user
                       ON token_ledger(user_id, id DESC)''')
    dst_cur.execute('''CREATE INDEX IF NOT EXISTS idx_token_ledger_ref
                       ON token_ledger(ref)''')
    dst_cur.execute('''CREATE TABLE IF NOT EXISTS resetotp_trial (
        user_id INTEGER PRIMARY KEY,
        used INTEGER NOT NULL DEFAULT 0,
        first_used_at TIMESTAMP,
        last_used_at TIMESTAMP
    )''')
    dst.commit()

    # Migrate premium_access
    print("Migrating premium_access...")
    src_cur.execute("SELECT * FROM premium_access")
    rows = src_cur.fetchall()
    for row in rows:
        try:
            dst_cur.execute('''INSERT OR REPLACE INTO premium_access
                (user_id, expired_at, package, days, invoice_id, granted_by, total_paid, buy_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', row)
        except Exception as e:
            print(f"  Error migrating premium_access user_id={row[0]}: {e}")
    dst.commit()
    print(f"  Migrated {len(rows)} rows")

    # Migrate premium_invoices
    print("Migrating premium_invoices...")
    src_cur.execute("SELECT * FROM premium_invoices")
    rows = src_cur.fetchall()
    for row in rows:
        try:
            # row: (id, user_id, invoice_id, package, days, amount, fee, total, status, qris_image, payment_link, expired_at, created_at, paid_at)
            # dst: (id, user_id, invoice_id, package, days, amount, fee, total, status, qris_image, payment_link, expired_at, created_at, paid_at, kind, tokens)
            dst_row = list(row[:14]) + ['days', 0]  # kind='days', tokens=0
            dst_cur.execute('''INSERT OR REPLACE INTO premium_invoices
                (id, user_id, invoice_id, package, days, amount, fee, total, status, qris_image, payment_link, expired_at, created_at, paid_at, kind, tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', dst_row)
        except Exception as e:
            print(f"  Error migrating invoice {row[2]}: {e}")
    dst.commit()
    print(f"  Migrated {len(rows)} rows")

    # Migrate premium_tokens
    print("Migrating premium_tokens...")
    src_cur.execute("SELECT * FROM premium_tokens")
    rows = src_cur.fetchall()
    for row in rows:
        try:
            dst_cur.execute('''INSERT OR REPLACE INTO premium_tokens
                (user_id, balance, total_bought, total_spent, total_refund, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)''', row)
        except Exception as e:
            print(f"  Error migrating premium_tokens user_id={row[0]}: {e}")
    dst.commit()
    print(f"  Migrated {len(rows)} rows")

    # Migrate token_ledger
    print("Migrating token_ledger...")
    src_cur.execute("SELECT * FROM token_ledger")
    rows = src_cur.fetchall()
    for row in rows:
        try:
            dst_cur.execute('''INSERT OR REPLACE INTO token_ledger
                (id, user_id, delta, balance_after, kind, ref, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', row)
        except Exception as e:
            print(f"  Error migrating token_ledger id={row[0]}: {e}")
    dst.commit()
    print(f"  Migrated {len(rows)} rows")

    # Migrate resetotp_trial
    print("Migrating resetotp_trial...")
    src_cur.execute("SELECT * FROM resetotp_trial")
    rows = src_cur.fetchall()
    for row in rows:
        try:
            dst_cur.execute('''INSERT OR REPLACE INTO resetotp_trial
                (user_id, used, first_used_at, last_used_at)
                VALUES (?, ?, ?, ?)''', row)
        except Exception as e:
            print(f"  Error migrating resetotp_trial user_id={row[0]}: {e}")
    dst.commit()
    print(f"  Migrated {len(rows)} rows")

    # Migration: kolom baru di premium_invoices (kind, tokens)
    print("Applying migrations...")
    try:
        dst_cur.execute("PRAGMA table_info(premium_invoices)")
        _inv_cols = {row[1] for row in dst_cur.fetchall()}
        if 'kind' not in _inv_cols:
            dst_cur.execute("ALTER TABLE premium_invoices ADD COLUMN kind TEXT DEFAULT 'days'")
        if 'tokens' not in _inv_cols:
            dst_cur.execute("ALTER TABLE premium_invoices ADD COLUMN tokens INTEGER DEFAULT 0")
        dst.commit()
        print("  Migrations applied")
    except Exception as e:
        print(f"  Migration warning: {e}")

    src.close()
    dst.close()
    print("\n✅ Migration complete! Data premium sekarang di premium_bot.db")

if __name__ == "__main__":
    migrate()
