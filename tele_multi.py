"""
tele_multi.py — Multi-sender invite (SEMUA/ALL) + Auto Kick for /tele
"""
from __future__ import annotations

import asyncio
import html
import logging
import random
import time
from typing import Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)

MULTI_WORKERS = 40
KICK_WORKERS = 20
INVITE_DELAY = 2.5

# Injected from tele_handler
_em = None
_screen = None
_batal_kb = None
_edit_job = None
_connect_sender = None
_list_admin_groups = None
_invite_one = None
_get_member_count = None
_entity_info = None
_get_pool = None
_get_pool_all = None
_USER_ID = None
E1 = E2 = E3 = E6 = ""
CE_AKUN = CE_LOADING = CE_LIVE = CE_HELP = CE_DETAIL_NAME = CE_DETAIL_ID = CE_TELEGRAM = CE_WAKTU = CE_FILE = ""


def multi_init(**kw):
    g = globals()
    for k, v in kw.items():
        g[k] = v


def em(eid, fb="⭐"):
    if _em:
        return _em(eid, fb)
    return fb


# ═══════════════════════════════════════
#  SEMUA / ALL — multi sender invite
# ═══════════════════════════════════════
async def start_multi_pick_primary(query, context, user_id, job, owner_mode: bool):
    """Pilih sender utama (admin grup tujuan)."""
    senders = _get_pool(user_id) if _get_pool else []
    senders = [s for s in senders if s.get("session") and s.get("status") != "dead"]
    if not senders:
        await query.answer("Belum ada sender milikmu", show_alert=True)
        return True

    job["multi_mode"] = "semua" if owner_mode else "all"
    job["phase"] = "multi_pick_primary"

    pool_n = 0
    if owner_mode and _get_pool_all:
        pool_n = len([s for s in _get_pool_all() if s.get("session")])
    else:
        pool_n = len(senders)

    label = "SEMUA" if owner_mode else "ALL"
    body = (
        f"{em(CE_TELEGRAM, '💬')} <b>{label} — MULTI SENDER</b>\n"
        f"────────────────────────────\n\n"
        f"  {em(CE_AKUN, '👥')} Worker pool: <b>{pool_n}</b> sender\n"
        f"  {em(E6, '🚀')} Workers paralel: <b>{MULTI_WORKERS}</b>\n"
        f"  Member antrian: <b>{len(job.get('members') or [])}</b>\n\n"
        f"  {em(CE_HELP, '💡')} Pilih <b>sender utama</b> (admin grup tujuan).\n"
        f"  Sender lain bantu add, lalu keluar otomatis.\n"
        f"  Sender utama tetap di grup.\n\n"
        f"────────────────────────────"
    )
    kb, row = [], []
    for i, s in enumerate(senders[:24], 1):
        name = (s.get("name") or s.get("phone") or "?")[:12]
        row.append(InlineKeyboardButton(
            f"{i}. {name}"[:28],
            callback_data=f"tele_multi_pri_{user_id}_{s['id']}",
            style="primary",
        ))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger")])
    await query.edit_message_text(
        _screen("MULTI INVITE", body, "Home › Telegram › Extract › Multi"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return True


async def multi_primary_chosen(query, context, user_id, job, sid: int):
    senders = _get_pool(user_id) if _get_pool else []
    primary = next((s for s in senders if s["id"] == sid), None)
    if not primary or not primary.get("session"):
        await query.answer("Sender tidak ditemukan", show_alert=True)
        return True

    await query.answer("Memuat grup admin…")
    job["primary_sender"] = primary
    job["phase"] = "multi_pick_dest"

    client = None
    try:
        client = await _connect_sender(primary["session"])
        admin_groups = await _list_admin_groups(client)
    except Exception as e:
        await query.edit_message_text(
            _screen("MULTI INVITE", f"{em(E2, '❌')} Gagal: <code>{html.escape(str(e)[:160])}</code>"),
            parse_mode="HTML",
            reply_markup=_batal_kb(user_id),
        )
        return True
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    job["admin_groups"] = admin_groups
    body_lines = [
        f"{em(CE_TELEGRAM, '💬')} <b>MULTI INVITE — PILIH GRUP</b>",
        "────────────────────────────",
        "",
        f"  Sender utama: <b>{html.escape(str(primary.get('name') or primary.get('phone'))[:24])}</b>",
        f"  Member: <b>{len(job.get('members') or [])}</b>",
        "",
        f"  {em(CE_HELP, '💡')} Pilih grup tujuan (sender = admin):",
        "",
    ]
    kb = []
    for g in admin_groups[:40]:
        title = (g.get("dialog_name") or g.get("title") or "?")[:18]
        cnt = g.get("participants_count")
        cnt_s = f" · {cnt}" if cnt else ""
        badge = "👑" if g.get("is_creator") else "🛡"
        if g.get("broadcast") and not g.get("megagroup"):
            kind = "CH"
        elif g.get("megagroup"):
            kind = "SG"
        else:
            kind = "GB"
        body_lines.append(
            f"  {badge}[{kind}] <b>{html.escape(title)}</b> · <code>{g['full_id']}</code>{cnt_s}"
        )
        kb.append([InlineKeyboardButton(
            f"{badge}[{kind}] {title} | {g['full_id']}"[:64],
            callback_data=f"tele_multi_dest_{user_id}_{g['full_id']}",
            style="success",
        )])
    if not admin_groups:
        body_lines.append(f"  {em(E3, '⚠️')} Tidak ada grup admin.")
    kb.append([InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger")])

    await query.edit_message_text(
        _screen("MULTI INVITE", "\n".join(body_lines), "Home › Telegram › Extract › Multi"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return True


async def multi_dest_chosen(query, context, user_id, job, dest_full_id: int):
    dest = None
    for g in job.get("admin_groups") or []:
        if g["full_id"] == dest_full_id or g["id"] == abs(dest_full_id):
            dest = g
            break
    if not dest:
        dest = {"full_id": dest_full_id, "title": str(dest_full_id), "id": abs(dest_full_id)}

    primary = job.get("primary_sender")
    if not primary:
        await query.answer("Primary sender hilang", show_alert=True)
        return True

    # Build helper pool
    if job.get("multi_mode") == "semua" and _get_pool_all:
        pool = [s for s in _get_pool_all() if s.get("session") and s.get("status") != "dead"]
    else:
        pool = [s for s in (_get_pool(user_id) or []) if s.get("session") and s.get("status") != "dead"]

    # Dedup by id, ensure primary included
    by_id = {s["id"]: s for s in pool}
    by_id[primary["id"]] = primary
    helpers = list(by_id.values())

    job["dest"] = dest
    job["helper_pool"] = helpers
    job["phase"] = "multi_invite"
    job["cancel"] = False
    job["message_id"] = query.message.message_id
    job["chat_id"] = query.message.chat_id

    await query.answer("Mulai multi-invite…")
    body = (
        f"{em(CE_LOADING, '⏳')} <b>MULTI INVITE DIMULAI</b>\n"
        f"────────────────────────────\n\n"
        f"  Tujuan: <b>{html.escape(str(dest.get('title') or dest.get('dialog_name') or '?')[:36])}</b>\n"
        f"  ID: <code>{dest.get('full_id')}</code>\n"
        f"  Sender pool: <b>{len(helpers)}</b> · Workers: <b>{MULTI_WORKERS}</b>\n"
        f"  Antrian: <b>{len(job.get('members') or [])}</b>\n\n"
        f"────────────────────────────"
    )
    await query.edit_message_text(
        _screen("MULTI INVITE", body, "Home › Telegram › Extract › Multi"),
        parse_mode="HTML",
        reply_markup=_batal_kb(user_id),
    )
    asyncio.create_task(_run_multi_invite(job, context.bot))
    return True


async def _ensure_joined(client, dest_entity, invite_hash: str | None):
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest
    from telethon.tl.types import Channel
    from telethon.errors import UserAlreadyParticipantError

    try:
        if invite_hash:
            try:
                await client(ImportChatInviteRequest(invite_hash))
            except UserAlreadyParticipantError:
                pass
            return
        if isinstance(dest_entity, Channel):
            try:
                await client(JoinChannelRequest(dest_entity))
            except UserAlreadyParticipantError:
                pass
    except Exception as e:
        log.warning("ensure join: %s", e)


async def _leave_chat(client, dest_entity):
    from telethon.tl.functions.channels import LeaveChannelRequest
    from telethon.tl.functions.messages import DeleteChatUserRequest
    from telethon.tl.types import Channel

    try:
        if isinstance(dest_entity, Channel):
            await client(LeaveChannelRequest(dest_entity))
        else:
            me = await client.get_me()
            await client(DeleteChatUserRequest(dest_entity.id, me.id))
    except Exception as e:
        log.warning("leave: %s", e)


async def _export_invite_hash(client, dest_entity) -> str | None:
    from telethon.tl.functions.messages import ExportChatInviteRequest
    try:
        inv = await client(ExportChatInviteRequest(dest_entity))
        link = getattr(inv, "link", "") or ""
        # https://t.me/+HASH or joinchat/HASH
        if "/+" in link:
            return link.rsplit("/+", 1)[-1]
        if "joinchat/" in link:
            return link.rsplit("joinchat/", 1)[-1]
    except Exception as e:
        log.warning("export invite: %s", e)
    return None


async def _run_multi_invite(job: dict, bot):
    from telethon.tl.types import InputPeerUser
    from telethon.errors import FloodWaitError, PeerFloodError

    user_id = job["user_id"]
    members = list(job.get("members") or [])
    random.shuffle(members)
    primary = job["primary_sender"]
    helpers = list(job.get("helper_pool") or [])
    dest = job.get("dest") or {}
    t0 = time.time()

    stats = {"ok": 0, "skip": 0, "fail": 0, "already": 0, "flood": 0, "done": 0}
    added_ids = set()
    queue = asyncio.Queue()
    for m in members:
        await queue.put(m)

    # sender state
    sender_state = {}
    for s in helpers:
        sender_state[s["id"]] = {
            "sender": s,
            "flood_until": 0,
            "busy": False,
            "ok": 0,
            "client": None,
            "joined": False,
        }

    member_now = dest.get("participants_count")
    member_start = member_now
    lock = asyncio.Lock()
    invite_hash = None
    dest_entity_cache = {}

    def progress(extra=""):
        alive = sum(1 for st in sender_state.values() if st["flood_until"] <= time.time())
        return (
            f"{em(CE_LIVE, '📡')} <b>MULTI INVITE BERJALAN</b>\n"
            f"────────────────────────────\n\n"
            f"  Tujuan: <b>{html.escape(str(dest.get('title') or dest.get('dialog_name') or '?')[:32])}</b>\n"
            f"  ID: <code>{dest.get('full_id')}</code>\n"
            f"  {em(CE_AKUN, '👥')} Member sekarang: <b>{member_now if member_now is not None else '?'}</b>\n"
            f"  Sender aktif: <b>{alive}/{len(sender_state)}</b>\n\n"
            f"  Progress: <b>{stats['done']}/{len(members)}</b>\n"
            f"  {em(E1, '✅')} Masuk: <b>{stats['ok']}</b>\n"
            f"  Already: <b>{stats['already']}</b>\n"
            f"  Skip: <b>{stats['skip']}</b> · Fail: <b>{stats['fail']}</b>\n"
            f"  Flood: <b>{stats['flood']}</b>x\n"
            f"{extra}"
            f"────────────────────────────"
        )

    async def refresh_ui(extra=""):
        await _edit_job(
            bot, job, progress(extra),
            kb=_batal_kb(user_id),
            title="MULTI INVITE",
            crumb="Home › Telegram › Extract › Multi",
        )

    async def get_client_for(sid: int):
        st = sender_state[sid]
        if st["client"] and st["client"].is_connected():
            return st["client"]
        c = await _connect_sender(st["sender"]["session"])
        st["client"] = c
        return c

    try:
        # Primary connects, resolves dest, exports invite
        pclient = await get_client_for(primary["id"])
        try:
            dest_entity = await pclient.get_entity(dest.get("full_id") or dest.get("id"))
        except Exception:
            dest_entity = await pclient.get_entity(dest.get("id"))
        dest_entity_cache["e"] = dest_entity
        info = _entity_info(dest_entity)
        dest["title"] = info.get("title") or dest.get("title")
        member_now = await _get_member_count(pclient, dest_entity)
        member_start = member_now
        invite_hash = await _export_invite_hash(pclient, dest_entity)
        sender_state[primary["id"]]["joined"] = True

        await refresh_ui()

        async def worker(wid: int):
            nonlocal member_now
            while not job.get("cancel"):
                try:
                    m = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                if m["id"] in added_ids:
                    stats["done"] += 1
                    queue.task_done()
                    continue

                # pick free sender
                sid = None
                async with lock:
                    now = time.time()
                    candidates = [
                        s_id for s_id, st in sender_state.items()
                        if not st["busy"] and st["flood_until"] <= now
                    ]
                    if not candidates:
                        # put back and wait
                        await queue.put(m)
                        # jangan task_done di sini sebelum sleep — tetap tandai selesai get
                        queue.task_done()
                        await asyncio.sleep(2)
                        continue
                    sid = random.choice(candidates)
                    sender_state[sid]["busy"] = True

                status = "fail"
                try:
                    client = await get_client_for(sid)
                    st = sender_state[sid]
                    if not st["joined"]:
                        await _ensure_joined(client, dest_entity_cache["e"], invite_hash)
                        # re-resolve on this client
                        try:
                            ent = await client.get_entity(dest.get("full_id") or dest.get("id"))
                        except Exception:
                            ent = dest_entity_cache["e"]
                        st["joined"] = True
                    else:
                        try:
                            ent = await client.get_entity(dest.get("full_id") or dest.get("id"))
                        except Exception:
                            ent = dest_entity_cache["e"]

                    try:
                        if m.get("access_hash"):
                            peer = InputPeerUser(m["id"], m["access_hash"])
                        else:
                            peer = await client.get_input_entity(m["id"])
                    except Exception:
                        status = "skip"
                        peer = None

                    if peer is not None:
                        try:
                            status = await _invite_one(client, ent, peer, m["id"])
                        except FloodWaitError as e:
                            stats["flood"] += 1
                            wait = min(int(getattr(e, "seconds", 60) or 60) + 2, 300)
                            sender_state[sid]["flood_until"] = time.time() + wait
                            await queue.put(m)  # retry with other sender
                            status = "retry"
                        except PeerFloodError:
                            stats["flood"] += 1
                            sender_state[sid]["flood_until"] = time.time() + 90
                            await queue.put(m)
                            status = "retry"
                        except Exception as e:
                            log.warning("multi invite: %s", e)
                            status = "fail"

                except Exception as e:
                    log.warning("worker %s: %s", wid, e)
                    status = "fail"
                finally:
                    sender_state[sid]["busy"] = False

                async with lock:
                    if status == "retry":
                        pass
                    else:
                        stats["done"] += 1
                        if status == "ok":
                            stats["ok"] += 1
                            added_ids.add(m["id"])
                            sender_state[sid]["ok"] += 1
                            if isinstance(member_now, int):
                                member_now += 1
                        elif status == "already":
                            stats["already"] += 1
                            added_ids.add(m["id"])
                        elif status == "skip":
                            stats["skip"] += 1
                            added_ids.add(m["id"])
                        else:
                            stats["fail"] += 1
                            added_ids.add(m["id"])

                    # Sync jumlah member dari server jarang saja (hindari flood),
                    # sisanya sudah realtime lewat increment lokal di atas.
                    if stats["ok"] > 0 and stats["ok"] % 25 == 0:
                        try:
                            pc = sender_state[primary["id"]].get("client")
                            if pc and pc.is_connected():
                                fresh = await _get_member_count(pc, dest_entity_cache["e"])
                                if fresh is not None:
                                    member_now = fresh
                        except Exception:
                            pass
                    # Refresh tampilan (edit pesan bot = murah, bukan request akun)
                    if stats["done"] % 3 == 0:
                        await refresh_ui()

                queue.task_done()
                await asyncio.sleep(INVITE_DELAY)

        n_workers = min(MULTI_WORKERS, max(1, len(helpers)), max(1, len(members)))
        await asyncio.gather(*(worker(i) for i in range(n_workers)))

        # Final count
        try:
            pc = sender_state[primary["id"]].get("client")
            if pc:
                fresh = await _get_member_count(pc, dest_entity_cache["e"])
                if fresh is not None:
                    member_now = fresh
        except Exception:
            pass

        # Leave all helpers except primary
        left = 0
        for sid, st in sender_state.items():
            if sid == primary["id"]:
                continue
            if not st.get("joined"):
                continue
            try:
                c = st.get("client") or await get_client_for(sid)
                await _leave_chat(c, dest_entity_cache["e"])
                left += 1
            except Exception as e:
                log.warning("leave helper: %s", e)

        job["phase"] = "done"
        cancelled = bool(job.get("cancel"))
        delta = ""
        if member_start is not None and member_now is not None:
            delta = f" <i>(+{max(0, int(member_now) - int(member_start))})</i>"
        body = (
            f"{em(E2 if cancelled else E1, '✅')} <b>MULTI INVITE "
            f"{'DIBATALKAN' if cancelled else 'SELESAI'}</b>\n"
            f"────────────────────────────\n\n"
            f"  {em(CE_DETAIL_NAME, '🏷')} <b>{html.escape(str(dest.get('title') or '?')[:36])}</b>\n"
            f"  {em(CE_DETAIL_ID, '🆔')} <code>{dest.get('full_id')}</code>\n"
            f"  {em(CE_AKUN, '👥')} Member sekarang: <b>{member_now if member_now is not None else '?'}</b>{delta}\n"
            f"  Member awal: <b>{member_start if member_start is not None else '?'}</b>\n\n"
            f"  {em(E1, '✅')} Masuk: <b>{stats['ok']}</b>\n"
            f"  Already: <b>{stats['already']}</b> · Skip: <b>{stats['skip']}</b>\n"
            f"  Fail: <b>{stats['fail']}</b> · Flood: <b>{stats['flood']}</b>x\n"
            f"  Helper keluar: <b>{left}</b> · Primary tetap di grup\n"
            f"  {em(CE_WAKTU, '⏱')} {int(time.time() - t0)}s\n"
            f"────────────────────────────"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("« Menu Telegram", callback_data=f"tele_back_{user_id}", style="primary")],
            [InlineKeyboardButton("Tutup", callback_data=f"tele_cancel_{user_id}", style="danger")],
        ])
        await _edit_job(bot, job, body, kb=kb, title="MULTI INVITE", crumb="Home › Telegram › Extract › Multi")

    except Exception as e:
        log.exception("[multi_invite]")
        await _edit_job(
            bot, job,
            f"{em(E2, '❌')} Multi invite gagal\n<code>{html.escape(str(e)[:200])}</code>",
            kb=_batal_kb(user_id),
            title="MULTI INVITE",
            crumb="Home › Telegram › Extract › Multi",
        )
    finally:
        for st in sender_state.values():
            c = st.get("client")
            if c:
                try:
                    await c.disconnect()
                except Exception:
                    pass


# ═══════════════════════════════════════
#  AUTO KICK
# ═══════════════════════════════════════
async def start_kick_pick_sender(query, user_id):
    senders = _get_pool(user_id) if _get_pool else []
    senders = [s for s in senders if s.get("session") and s.get("status") != "dead"]
    if not senders:
        await query.answer("Belum ada sender", show_alert=True)
        return True
    body = (
        f"{em(CE_TELEGRAM, '💬')} <b>AUTO KICK MEMBER</b>\n"
        f"────────────────────────────\n\n"
        f"  {em(CE_HELP, '💡')} Pilih sender (harus admin di grup target).\n"
        f"  Semua member non-admin akan di-kick.\n\n"
        f"────────────────────────────"
    )
    kb, row = [], []
    for i, s in enumerate(senders[:24], 1):
        name = (s.get("name") or s.get("phone") or "?")[:12]
        row.append(InlineKeyboardButton(
            f"{i}. {name}"[:28],
            callback_data=f"tele_kick_pick_{user_id}_{s['id']}",
            style="primary",
        ))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([
        InlineKeyboardButton("« Kembali", callback_data=f"tele_back_{user_id}", style="primary"),
        InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger"),
    ])
    await query.edit_message_text(
        _screen("AUTO KICK", body, "Home › Telegram › Auto Kick"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return True


async def kick_sender_chosen(query, context, user_id, jobs: dict, sid: int):
    senders = _get_pool(user_id) if _get_pool else []
    sender = next((s for s in senders if s["id"] == sid), None)
    if not sender:
        await query.answer("Sender tidak ditemukan", show_alert=True)
        return True

    await query.answer("Memuat grup…")
    client = None
    try:
        client = await _connect_sender(sender["session"])
        groups = await _list_admin_groups(client)
    except Exception as e:
        await query.edit_message_text(
            _screen("AUTO KICK", f"{em(E2, '❌')} {html.escape(str(e)[:160])}"),
            parse_mode="HTML",
            reply_markup=_batal_kb(user_id),
        )
        return True
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    jobs[user_id] = {
        "user_id": user_id,
        "phase": "kick_pick_dest",
        "kick_sender": sender,
        "admin_groups": groups,
        "cancel": False,
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
        "members": [],
    }
    kb = []
    lines = [
        f"{em(CE_TELEGRAM, '💬')} <b>AUTO KICK — PILIH GRUP</b>",
        "────────────────────────────",
        "",
        f"  Sender: <b>{html.escape(str(sender.get('name') or sender.get('phone'))[:24])}</b>",
        "",
    ]
    for g in groups[:40]:
        title = (g.get("dialog_name") or g.get("title") or "?")[:20]
        if g.get("broadcast") and not g.get("megagroup"):
            kind = "CH"
        elif g.get("megagroup"):
            kind = "SG"
        else:
            kind = "GB"
        lines.append(f"  [{kind}] <b>{html.escape(title)}</b> · <code>{g['full_id']}</code>")
        kb.append([InlineKeyboardButton(
            f"Kick [{kind}] · {title}"[:64],
            callback_data=f"tele_kick_dest_{user_id}_{g['full_id']}",
            style="danger",
        )])
    if not groups:
        lines.append(f"  {em(E3, '⚠️')} Tidak ada grup admin.")
    kb.append([InlineKeyboardButton("Batal", callback_data=f"tele_cancel_{user_id}", style="danger")])
    await query.edit_message_text(
        _screen("AUTO KICK", "\n".join(lines), "Home › Telegram › Auto Kick"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return True


async def kick_dest_chosen(query, context, user_id, jobs: dict, dest_full_id: int):
    job = jobs.get(user_id)
    if not job or not job.get("kick_sender"):
        await query.answer("Session hilang", show_alert=True)
        return True
    dest = None
    for g in job.get("admin_groups") or []:
        if g["full_id"] == dest_full_id or g["id"] == abs(dest_full_id):
            dest = g
            break
    if not dest:
        dest = {"full_id": dest_full_id, "title": str(dest_full_id)}

    job["dest"] = dest
    job["phase"] = "kicking"
    job["cancel"] = False
    job["message_id"] = query.message.message_id
    job["chat_id"] = query.message.chat_id

    await query.answer("Mulai kick…")
    await query.edit_message_text(
        _screen(
            "AUTO KICK",
            f"{em(CE_LOADING, '⏳')} <b>AUTO KICK DIMULAI</b>\n"
            f"Grup: <b>{html.escape(str(dest.get('title') or dest.get('dialog_name') or '?')[:36])}</b>",
            "Home › Telegram › Auto Kick",
        ),
        parse_mode="HTML",
        reply_markup=_batal_kb(user_id),
    )
    asyncio.create_task(_run_auto_kick(job, context.bot))
    return True


async def _run_auto_kick(job: dict, bot):
    from telethon.tl.functions.channels import EditBannedRequest
    from telethon.tl.types import ChatBannedRights, Channel
    from telethon.errors import FloodWaitError, UserAdminInvalidError, ChatAdminRequiredError

    user_id = job["user_id"]
    sender = job["kick_sender"]
    dest = job["dest"]
    t0 = time.time()
    stats = {"kicked": 0, "skip": 0, "fail": 0, "done": 0}
    client = None

    def body(extra=""):
        return (
            f"{em(CE_LIVE, '📡')} <b>AUTO KICK BERJALAN</b>\n"
            f"────────────────────────────\n\n"
            f"  Grup: <b>{html.escape(str(dest.get('title') or '?')[:36])}</b>\n"
            f"  ID: <code>{dest.get('full_id')}</code>\n\n"
            f"  Progress: <b>{stats['done']}</b>\n"
            f"  {em(E1, '✅')} Kicked: <b>{stats['kicked']}</b>\n"
            f"  Skip (admin/self): <b>{stats['skip']}</b>\n"
            f"  Fail: <b>{stats['fail']}</b>\n"
            f"{extra}"
            f"────────────────────────────"
        )

    try:
        client = await _connect_sender(sender["session"])
        me = await client.get_me()
        entity = await client.get_entity(dest.get("full_id") or dest.get("id"))
        info = _entity_info(entity)
        dest["title"] = info.get("title") or dest.get("title")

        targets = []
        async for u in client.iter_participants(entity):
            if job.get("cancel"):
                break
            if u.id == me.id:
                continue
            # skip admins/creator
            try:
                perms = await client.get_permissions(entity, u)
                if perms.is_admin or perms.is_creator:
                    stats["skip"] += 1
                    continue
            except Exception:
                pass
            if getattr(u, "bot", False):
                stats["skip"] += 1
                continue
            targets.append(u)

        ban_rights = ChatBannedRights(until_date=None, view_messages=True)

        for u in targets:
            if job.get("cancel"):
                break
            stats["done"] += 1
            try:
                if isinstance(entity, Channel):
                    await client(EditBannedRequest(entity, u, ban_rights))
                    # unban immediately = kick
                    await client(EditBannedRequest(
                        entity, u,
                        ChatBannedRights(until_date=None, view_messages=False),
                    ))
                else:
                    from telethon.tl.functions.messages import DeleteChatUserRequest
                    await client(DeleteChatUserRequest(entity.id, u.id))
                stats["kicked"] += 1
            except FloodWaitError as e:
                await asyncio.sleep(min(int(e.seconds) + 1, 120))
                try:
                    if isinstance(entity, Channel):
                        await client(EditBannedRequest(entity, u, ban_rights))
                        await client(EditBannedRequest(
                            entity, u,
                            ChatBannedRights(until_date=None, view_messages=False),
                        ))
                    stats["kicked"] += 1
                except Exception:
                    stats["fail"] += 1
            except (UserAdminInvalidError, ChatAdminRequiredError):
                stats["skip"] += 1
            except Exception as e:
                log.warning("kick: %s", e)
                stats["fail"] += 1

            if stats["done"] % 5 == 0:
                await _edit_job(
                    bot, job, body(),
                    kb=_batal_kb(user_id),
                    title="AUTO KICK",
                    crumb="Home › Telegram › Auto Kick",
                )
            await asyncio.sleep(0.4)

        job["phase"] = "done"
        cancelled = bool(job.get("cancel"))
        final = (
            f"{em(E2 if cancelled else E1, '✅')} <b>AUTO KICK "
            f"{'DIBATALKAN' if cancelled else 'SELESAI'}</b>\n"
            f"────────────────────────────\n\n"
            f"  Grup: <b>{html.escape(str(dest.get('title') or '?')[:36])}</b>\n"
            f"  ID: <code>{dest.get('full_id')}</code>\n\n"
            f"  {em(E1, '✅')} Kicked: <b>{stats['kicked']}</b>\n"
            f"  Skip: <b>{stats['skip']}</b> · Fail: <b>{stats['fail']}</b>\n"
            f"  {em(CE_WAKTU, '⏱')} {int(time.time() - t0)}s\n"
            f"────────────────────────────"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("« Menu Telegram", callback_data=f"tele_back_{user_id}", style="primary")],
        ])
        await _edit_job(bot, job, final, kb=kb, title="AUTO KICK", crumb="Home › Telegram › Auto Kick")
    except Exception as e:
        log.exception("[auto_kick]")
        await _edit_job(
            bot, job,
            f"{em(E2, '❌')} Auto kick gagal\n<code>{html.escape(str(e)[:200])}</code>",
            kb=_batal_kb(user_id),
            title="AUTO KICK",
            crumb="Home › Telegram › Auto Kick",
        )
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
