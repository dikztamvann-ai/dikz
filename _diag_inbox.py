#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnostic: open webmail for mailbox 116483 (wa_fix_32845) and dump inbox."""
import sqlite3
import sitepro_fix_module as S

MBOX_EMAIL = 'wa_fix_32845@siteprofree.email'

c = sqlite3.connect('ivas_bot.db')
cur = c.cursor()
cur.execute("""SELECT m.mailbox_id, m.account_id, a.phpsessid, a.sitepro_email,
                      a.temp_email, a.sitepro_password
               FROM sitepro_mailboxes m JOIN sitepro_accounts a ON a.id=m.account_id
               WHERE m.email = ?""", (MBOX_EMAIL,))
row = cur.fetchone()
if not row:
    print("mailbox tidak ditemukan"); raise SystemExit
mailbox_id, account_id, phpsessid, sp_email, tmp_email, pw = row
print(f"mailbox_id={mailbox_id} account_id={account_id} sp_email={sp_email}")

sess, ok = S.sitepro_restore_session(phpsessid, email=sp_email or tmp_email, password=pw)
print("restore_session ok=", ok)
if not ok:
    raise SystemExit

ws, msg = S.sitepro_open_webmail(sess, mailbox_id)
print("open_webmail:", msg)
if not ws:
    raise SystemExit

msgs, err = S.roundcube_read_inbox(ws, timeout=60, fetch_body=True, body_limit=10)
print("read_inbox err=", err, "count=", len(msgs))
print("=" * 60)
for m in msgs:
    print(f"UID={m.get('uid')}")
    print(f"  subject : {m.get('subject')!r}")
    print(f"  from    : {m.get('from')!r}")
    print(f"  from_name: {m.get('from_name')!r}")
    print(f"  date    : {m.get('date')!r}")
    body = (m.get('body') or '')
    print(f"  body[:300]: {body[:300]!r}")
    print("-" * 60)
