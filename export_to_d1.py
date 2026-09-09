"""export_to_d1.py — Export users + premium data from ivas_bot.db to D1 import SQL.

Reads E:\\ivas\\script\\ivas_bot.db and writes import.sql with INSERT statements
matching the D1 schema (see web_ivas/schema.sql). Run then:
    npx wrangler d1 execute ivas_db --remote --file import.sql
"""
import sqlite3
import os

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ivas_bot.db")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_ivas", "import.sql")

# table -> (columns in D1, sqlite source columns)
EXPORT = {
    "allowed_users":   ["user_id", "username", "added_by", "added_at"],
    "premium_access":  ["user_id", "expired_at", "package", "days", "total_paid", "buy_count", "updated_at"],
    "premium_tokens":  ["user_id", "balance", "total_bought", "total_spent", "total_refund", "updated_at"],
    "token_ledger":    ["id", "user_id", "delta", "balance_after", "kind", "ref", "note", "created_at"],
    "premium_invoices":["invoice_id", "user_id", "package", "kind", "days", "tokens", "amount", "fee", "total", "status", "paid_at", "created_at"],
}


def sql_val(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    lines = ["-- Auto-generated import from ivas_bot.db", "PRAGMA foreign_keys=OFF;"]
    total = 0

    for table, cols in EXPORT.items():
        # Check table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not cur.fetchone():
            lines.append(f"-- SKIP (missing): {table}")
            continue

        # Get available source columns
        cur.execute(f"PRAGMA table_info({table})")
        avail = {r[1] for r in cur.fetchall()}
        use_cols = [c for c in cols if c in avail]

        cur.execute(f"SELECT {','.join(use_cols)} FROM {table}")
        rows = cur.fetchall()
        if not rows:
            lines.append(f"-- {table}: 0 rows")
            continue

        lines.append(f"-- {table}: {len(rows)} rows")
        for r in rows:
            vals = ", ".join(sql_val(r[c]) for c in use_cols)
            lines.append(
                f"INSERT OR REPLACE INTO {table} ({','.join(use_cols)}) VALUES ({vals});"
            )
            total += 1

    con.close()

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Wrote {total} INSERT statements to {OUT}")


if __name__ == "__main__":
    main()
