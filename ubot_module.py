"""ubot_module.py — Userbot Telethon manager untuk /ubot di dik.py.

SEMUA kerja Telethon jalan di THREAD + EVENT LOOP TERSENDIRI (bukan event loop
bot). Dengan begini Telethon reconnect/lambat TIDAK mem-block bot utama —
callback, scheduler, dan handler lain tetap responsif.

Fitur userbot (.promote/.promosi/.stop/.addbl/.delbl/.listbl/.setdelay/.ping/.id/.help)
disamakan dengan userbot.py standalone. Owner-only.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import threading
import time
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError,
    FloodWaitError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    ChannelPrivateError,
)
from telethon.sessions import StringSession

# Suppress Telethon logging — tidak perlu tampil di log bot (clutter).
import logging as _logging
_logging.getLogger("telethon").setLevel(_logging.CRITICAL)

_DIR = Path(__file__).resolve().parent
_STATE_FILE = _DIR / "ubot_state.json"

# API credentials dari userbot.py
API_ID = 39191050
API_HASH = "2ee2a563b5e174e6c5f8009992722284"

# Default delay antar grup (detik)
DEFAULT_DELAY_MIN = 5
DEFAULT_DELAY_MAX = 12

# ---------------------------------------------------------------------------
# DEDICATED EVENT LOOP di thread terpisah — kunci anti-blocking.
# ---------------------------------------------------------------------------
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_loop_ready = threading.Event()


def _loop_main():
    """Main function untuk thread event loop Telethon."""
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop_ready.set()
    _loop.run_forever()


def _ensure_loop():
    """Start dedicated event loop kalau belum jalan."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop_main, daemon=True, name="ubot-telethon-loop")
    _thread.start()
    _loop_ready.wait(timeout=5)


_ensure_loop()


def _run_in_ubot_loop(coro, timeout: float = 30):
    """Jalankan coroutine di loop Telethon, tunggu hasilnya (blocking dari sisi pemanggil).

    Ini bridge sync→async yang aman: coroutine dieksekusi di loop khusus,
    jadi TIDAK mem-block event loop bot utama.
    """
    _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout=timeout)


async def _await_in_ubot_loop(coro, timeout: float = 30):
    """Versi async: await coroutine yang jalan di loop Telethon tanpa block loop bot."""
    _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    # wrap_future supaya bisa di-await di loop pemanggil
    return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=timeout)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
_state: dict[str, Any] = {
    "sessions": {},      # {phone: {"string_session": str, "delay_min": int, "delay_max": int, "blacklist": [int]}}
    "active_jobs": {},   # {job_id: {"phone": str, "chat_id": int, "stop": bool}}
    "pending_login": None,  # {"phone": str, "client": TelegramClient, "phone_code_hash": str}
}
_job_seq = {"n": 0}


def _load_state():
    global _state
    if _STATE_FILE.exists():
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                _state["sessions"] = data.get("sessions", {})
        except Exception as e:
            print(f"[UBOT] load state error: {e}")


def _save_state():
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"sessions": _state["sessions"]}, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[UBOT] save state error: {e}")


_load_state()

# Runtime clients: {phone: TelegramClient} — dibuat & dipakai HANYA di loop Telethon.
_clients: dict[str, TelegramClient] = {}


def _bq(text: str) -> str:
    return "<blockquote>" + text + "</blockquote>"


# ---------------------------------------------------------------------------
# Duration helpers (sama seperti userbot.py)
# ---------------------------------------------------------------------------
def _parse_duration(text: str) -> int:
    if not text:
        return 0
    text = text.lower().strip()
    total_seconds = 0
    patterns = [
        (r'(\d+(?:\.\d+)?)\s*(?:jam|hours?|hr|h|j)\b', 3600),
        (r'(\d+(?:\.\d+)?)\s*(?:menit|mins?|minute|m)\b', 60),
        (r'(\d+(?:\.\d+)?)\s*(?:detik|secs?|second|s)\b', 1),
    ]
    matched = False
    for pat, multiplier in patterns:
        m = re.search(pat, text)
        if m:
            matched = True
            total_seconds += float(m.group(1)) * multiplier
    if not matched:
        try:
            return int(float(text) * 60)
        except ValueError:
            return 0
    return int(total_seconds)


def _format_duration(seconds: int) -> str:
    if seconds >= 3600:
        jam = seconds // 3600
        menit = (seconds % 3600) // 60
        return f"{jam} jam {menit} menit" if menit else f"{jam} jam"
    if seconds >= 60:
        menit = seconds // 60
        sisa = seconds % 60
        return f"{menit} menit {sisa} detik" if sisa else f"{menit} menit"
    return f"{seconds} detik"


def _parse_promote_args(arg_text: str):
    if not arg_text:
        return "", None, None
    arg_text = arg_text.strip()
    pattern = (r'^(?:([\s\S]+?)\s+)?(\d+)\s+'
               r'(\d+(?:\.\d+)?\s*(?:jam|hours?|hr|h|j|menit|mins?|minute|m|detik|secs?|second|s)(?:[\s\S]*)?)$')
    m = re.match(pattern, arg_text, re.IGNORECASE)
    if m:
        text_part = (m.group(1) or "").strip()
        repeat_count = int(m.group(2))
        interval_secs = _parse_duration(m.group(3).strip())
        if repeat_count > 0 and interval_secs > 0:
            return text_part, repeat_count, interval_secs
    return arg_text, None, None


def _in_blacklist(sess_data, chat_id) -> bool:
    try:
        return int(chat_id) in [int(x) for x in sess_data.get("blacklist", [])]
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Telethon helpers — SEMUA async di bawah ini jalan di loop Telethon.
# ---------------------------------------------------------------------------
async def _get_client(phone: str) -> TelegramClient | None:
    """Get or create connected client (HARUS dipanggil dari loop Telethon)."""
    if phone in _clients:
        c = _clients[phone]
        try:
            if c.is_connected():
                return c
            await asyncio.wait_for(c.connect(), timeout=15)
            return c
        except Exception:
            pass

    sess_data = _state["sessions"].get(phone)
    if not sess_data or not sess_data.get("string_session"):
        return None

    client = TelegramClient(StringSession(sess_data["string_session"]), API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=20)
        if await client.is_user_authorized():
            _clients[phone] = client
            _register_handlers(client, phone)
            return client
        await client.disconnect()
    except Exception as e:
        print(f"[UBOT] connect {phone} failed: {e}")
    return None


async def _disconnect_client(phone: str):
    c = _clients.pop(phone, None)
    if c:
        try:
            await c.disconnect()
        except Exception:
            pass


async def _resolve_target_id(client, ref):
    ref = str(ref).strip()
    try:
        if ref.lstrip("-").isdigit():
            ent = await client.get_entity(int(ref))
        else:
            ent = await client.get_entity(ref)
        return ent.id
    except Exception:
        return None


async def _wait_user_input(client, chat_id, timeout=60):
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    async def _handler(e):
        if e.chat_id == chat_id and not fut.done():
            fut.set_result(e)

    client.add_event_handler(_handler, events.NewMessage(chats=chat_id, outgoing=True))
    try:
        resp_event = await asyncio.wait_for(fut, timeout=timeout)
        text = (resp_event.text or "").strip()
        try:
            await resp_event.delete()
        except Exception:
            pass
        return text, resp_event
    except asyncio.TimeoutError:
        return None, None
    finally:
        client.remove_event_handler(_handler)


async def _send_promo_to(client, target, source_msg, plain_text):
    if source_msg is not None:
        await client.send_message(
            target,
            message=source_msg.message or "",
            formatting_entities=source_msg.entities or None,
            file=source_msg.media if source_msg.media else None,
        )
    else:
        await client.send_message(target, plain_text)


# ---------------------------------------------------------------------------
# Command handlers — di-register per client, jalan di loop Telethon.
# ---------------------------------------------------------------------------
def _register_handlers(client: TelegramClient, phone: str):
    sess_data = _state["sessions"].get(phone, {})

    def cmd(pattern):
        return events.NewMessage(outgoing=True, pattern=pattern)

    @client.on(cmd(r"^\.ping$"))
    async def _ping(event):
        await event.edit(_bq("<b>Pong!</b> Userbot aktif."), parse_mode="html")

    @client.on(cmd(r"^\.id$"))
    async def _id(event):
        me = await event.get_sender()
        text = (f"<b>Info</b>\nChat ID : <code>{event.chat_id}</code>\n"
                f"User ID : <code>{getattr(me, 'id', '-')}</code>")
        await event.edit(_bq(text), parse_mode="html")

    @client.on(cmd(r"^\.help$"))
    async def _help(event):
        text = (
            "<b>Daftar Perintah Userbot</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "- <code>.promote &lt;teks&gt;</code> atau reply pesan\n"
            "  <i>Menu: [1] Kirim 1x, [2] Manual, [0] Batal</i>\n"
            "- <code>.promote &lt;teks&gt; &lt;kali&gt; &lt;interval&gt;</code>\n"
            "  <i>Contoh: .promote 5 1 jam</i>\n"
            "- <code>.addbl [@user/id]</code> — Blacklist grup\n"
            "- <code>.delbl [@user/id]</code> — Hapus blacklist\n"
            "- <code>.listbl</code> — Lihat blacklist\n"
            "- <code>.setdelay &lt;min&gt; &lt;max&gt;</code> — Atur jeda\n"
            "- <code>.stop</code> — Hentikan semua promosi\n"
            "- <code>.ping</code> / <code>.id</code>"
        )
        await event.edit(_bq(text), parse_mode="html")

    @client.on(cmd(r"^\.setdelay(?:\s+(\d+)\s+(\d+))?$"))
    async def _setdelay(event):
        m = event.pattern_match
        if not m.group(1):
            d_min = sess_data.get("delay_min", DEFAULT_DELAY_MIN)
            d_max = sess_data.get("delay_max", DEFAULT_DELAY_MAX)
            await event.edit(
                _bq(f"Delay sekarang: <b>{d_min}–{d_max} detik</b>.\n"
                    "Ubah: <code>.setdelay &lt;min&gt; &lt;max&gt;</code>"),
                parse_mode="html")
            return
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        sess_data["delay_min"], sess_data["delay_max"] = lo, hi
        _save_state()
        await event.edit(_bq(f"Delay diset <b>{lo}–{hi} detik</b>."), parse_mode="html")

    @client.on(cmd(r"^\.stop$"))
    async def _stop(event):
        active = [j for j in _state["active_jobs"].values() if not j.get("stop")]
        if not active:
            await event.edit(_bq("Tidak ada promosi yang berjalan."), parse_mode="html")
            return
        for j in _state["active_jobs"].values():
            j["stop"] = True
        await event.edit(
            _bq(f"Menghentikan <b>{len(active)}</b> promosi yang berjalan..."),
            parse_mode="html")

    @client.on(cmd(r"^\.addbl(?:\s+(.+))?$"))
    async def _addbl(event):
        arg = event.pattern_match.group(1)
        if arg:
            tid = await _resolve_target_id(client, arg.strip())
            if tid is None:
                await event.edit(_bq("Grup/target tidak ditemukan."), parse_mode="html")
                return
        else:
            tid = event.chat_id
        bl = sess_data.setdefault("blacklist", [])
        if int(tid) in [int(x) for x in bl]:
            await event.edit(_bq("Grup itu sudah di blacklist."), parse_mode="html")
            return
        bl.append(int(tid))
        _save_state()
        await event.edit(_bq(f"Ditambahkan ke blacklist: <code>{tid}</code>"), parse_mode="html")

    @client.on(cmd(r"^\.delbl(?:\s+(.+))?$"))
    async def _delbl(event):
        arg = event.pattern_match.group(1)
        if arg:
            tid = await _resolve_target_id(client, arg.strip())
            if tid is None:
                await event.edit(_bq("Grup/target tidak ditemukan."), parse_mode="html")
                return
        else:
            tid = event.chat_id
        bl = sess_data.setdefault("blacklist", [])
        if int(tid) not in [int(x) for x in bl]:
            await event.edit(_bq("Grup itu tidak ada di blacklist."), parse_mode="html")
            return
        sess_data["blacklist"] = [x for x in bl if int(x) != int(tid)]
        _save_state()
        await event.edit(_bq(f"Dihapus dari blacklist: <code>{tid}</code>"), parse_mode="html")

    @client.on(cmd(r"^\.listbl$"))
    async def _listbl(event):
        bl = sess_data.get("blacklist", [])
        if not bl:
            await event.edit(_bq("Blacklist kosong."), parse_mode="html")
            return
        lines = ["<b>Daftar Blacklist:</b>"]
        for i, x in enumerate(bl, 1):
            lines.append(f"{i}. <code>{x}</code>")
        await event.edit(_bq("\n".join(lines)), parse_mode="html")

    @client.on(cmd(r"^\.(?:promote|promosi)(?:\s+([\s\S]+))?$"))
    async def _promote(event):
        reply_msg = await event.get_reply_message()
        raw_arg = event.pattern_match.group(1)

        if reply_msg is None and not raw_arg:
            text = (
                "<b>Cara Pakai Promosi:</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "<b>Menu Interaktif:</b>\n"
                "  Reply pesan lalu ketik <code>.promote</code>\n\n"
                "<b>Format Cepat:</b>\n"
                "  <code>.promote &lt;teks&gt; &lt;kali&gt; &lt;interval&gt;</code>\n"
                "  <i>Contoh: .promote 5 1 jam</i>\n"
                "  <i>Contoh: .promote Jual Diamond 3 30 menit</i>"
            )
            await event.edit(_bq(text), parse_mode="html")
            return

        text_parsed, repeat_parsed, interval_parsed = _parse_promote_args(raw_arg)
        plain_text = (text_parsed or "").strip()
        chat_id = event.chat_id

        # KASUS 1: parameter lengkap — langsung jalan
        if repeat_parsed is not None and interval_parsed is not None:
            _job_seq["n"] += 1
            job_id = _job_seq["n"]
            info = (
                f"<b>Promosi #{job_id} Dimulai!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Jumlah Kirim : <b>{repeat_parsed}x</b>\n"
                f"Interval     : <b>{_format_duration(interval_parsed)}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<i>Berjalan di background. Ketik <code>.stop</code> untuk batal.</i>"
            )
            await event.edit(_bq(info), parse_mode="html")
            task = asyncio.create_task(
                _run_promo(client, phone, chat_id, job_id, reply_msg,
                           plain_text, repeat_parsed, interval_parsed))
            _state["active_jobs"][job_id] = {
                "phone": phone, "chat_id": chat_id, "stop": False, "task": task,
            }
            return

        # KASUS 2: Menu Interaktif
        menu_text = (
            "<b>PILIHAN PROMOSI</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<b>[ 1 ]</b> Kirim 1 Kali\n"
            "<b>[ 2 ]</b> Manual <i>(Set Jumlah & Interval)</i>\n"
            "<b>[ 0 ]</b> Batal\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>Balas / ketik: <b>1</b>, <b>2</b>, atau <b>0</b></i>"
        )
        await event.edit(_bq(menu_text), parse_mode="html")

        ans, _ = await _wait_user_input(client, chat_id, timeout=60)
        if not ans:
            await event.edit(_bq("Waktu habis. Promosi dibatalkan."), parse_mode="html")
            return
        if ans in ("0", "batal", "cancel", "b"):
            await event.edit(_bq("<b>Promosi Dibatalkan.</b>"), parse_mode="html")
            return

        if ans == "1":
            _job_seq["n"] += 1
            job_id = _job_seq["n"]
            await event.edit(
                _bq(f"<b>Promosi #{job_id} (1x) Dimulai!</b>\n"
                    f"<i>Ketik <code>.stop</code> untuk batal.</i>"),
                parse_mode="html")
            task = asyncio.create_task(
                _run_promo(client, phone, chat_id, job_id, reply_msg, plain_text, 1, 0))
            _state["active_jobs"][job_id] = {
                "phone": phone, "chat_id": chat_id, "stop": False, "task": task,
            }
            return

        if ans == "2":
            await event.edit(_bq(
                "<b>Langkah 1/2 — Frekuensi Kirim</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Mau berapa kali pesan dikirim?\n"
                "<i>(Contoh: <b>2</b>, <b>5</b>, <b>10</b> — atau <b>0</b> batal)</i>"),
                parse_mode="html")
            ans_kali, _ = await _wait_user_input(client, chat_id, timeout=60)
            if not ans_kali or ans_kali in ("0", "batal"):
                await event.edit(_bq("<b>Promosi Dibatalkan.</b>"), parse_mode="html")
                return
            try:
                repeat_count = int(ans_kali)
                if repeat_count <= 0:
                    raise ValueError
            except ValueError:
                await event.edit(_bq("Jumlah kirim harus angka positif."), parse_mode="html")
                return

            await event.edit(_bq(
                f"<b>Langkah 2/2 — Interval ({repeat_count}x kirim)</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Kirim setiap berapa lama?\n"
                "<i>(Contoh: <b>1 jam</b>, <b>30 menit</b>, <b>10m</b> — atau <b>0</b> batal)</i>"),
                parse_mode="html")
            ans_waktu, _ = await _wait_user_input(client, chat_id, timeout=60)
            if not ans_waktu or ans_waktu in ("0", "batal"):
                await event.edit(_bq("<b>Promosi Dibatalkan.</b>"), parse_mode="html")
                return
            interval_secs = _parse_duration(ans_waktu)
            if interval_secs <= 0:
                await event.edit(_bq("Format waktu salah. Contoh: <code>1 jam</code>"), parse_mode="html")
                return

            _job_seq["n"] += 1
            job_id = _job_seq["n"]
            info = (
                f"<b>Promosi Terjadwal #{job_id}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Jumlah Kirim : <b>{repeat_count}x</b>\n"
                f"Interval     : <b>{_format_duration(interval_secs)}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<i>Ketik <code>.stop</code> untuk berhenti.</i>"
            )
            await event.edit(_bq(info), parse_mode="html")
            task = asyncio.create_task(
                _run_promo(client, phone, chat_id, job_id, reply_msg,
                           plain_text, repeat_count, interval_secs))
            _state["active_jobs"][job_id] = {
                "phone": phone, "chat_id": chat_id, "stop": False, "task": task,
            }
            return

        await event.edit(_bq("Pilihan tidak valid."), parse_mode="html")


async def _run_promo(client, phone, chat_id, job_id, reply_msg, plain_text,
                     repeat_count=1, interval_secs=0):
    job = _state["active_jobs"].get(job_id)
    if not job:
        return
    sess_data = _state["sessions"].get(phone, {})
    delay_min = sess_data.get("delay_min", DEFAULT_DELAY_MIN)
    delay_max = sess_data.get("delay_max", DEFAULT_DELAY_MAX)

    total_sukses = total_gagal = total_dilewati = 0
    current_round = 0
    try:
        for current_round in range(1, repeat_count + 1):
            if job["stop"]:
                break
            groups = []
            async for dialog in client.iter_dialogs():
                if dialog.is_group and not _in_blacklist(sess_data, dialog.id):
                    groups.append(dialog)
            if not groups:
                await client.send_message(
                    chat_id, _bq(f"<b>Promosi #{job_id}</b>: Tidak ada grup / semua blacklist."),
                    parse_mode="html")
                return
            if repeat_count > 1:
                await client.send_message(
                    chat_id,
                    _bq(f"<b>Promosi #{job_id} — Putaran {current_round}/{repeat_count}</b>\n"
                        f"Kirim ke {len(groups)} grup..."),
                    parse_mode="html")

            sukses = gagal = dilewati = 0
            for idx, dialog in enumerate(groups, 1):
                if job["stop"]:
                    break
                try:
                    await _send_promo_to(client, dialog.id, reply_msg, plain_text)
                    sukses += 1
                except FloodWaitError as e:
                    await asyncio.sleep(min(e.seconds + 2, 300))
                    try:
                        await _send_promo_to(client, dialog.id, reply_msg, plain_text)
                        sukses += 1
                    except Exception:
                        gagal += 1
                except (ChatWriteForbiddenError, UserBannedInChannelError, ChannelPrivateError):
                    dilewati += 1
                except Exception:
                    gagal += 1
                if idx < len(groups) and not job["stop"]:
                    await asyncio.sleep(random.uniform(delay_min, delay_max))

            total_sukses += sukses
            total_gagal += gagal
            total_dilewati += dilewati
            await client.send_message(
                chat_id,
                _bq(f"<b>Promosi #{job_id} — Putaran {current_round}/{repeat_count}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Berhasil : {sukses}\nGagal    : {gagal}\n"
                    f"Dilewati : {dilewati}\nTotal    : {len(groups)} grup"),
                parse_mode="html")

            if current_round < repeat_count and not job["stop"]:
                await client.send_message(
                    chat_id,
                    _bq(f"<b>Promosi #{job_id}</b>\n"
                        f"Menunggu <b>{_format_duration(interval_secs)}</b> "
                        f"sebelum putaran ke-{current_round + 1}..."),
                    parse_mode="html")
                for _ in range(int(interval_secs)):
                    if job["stop"]:
                        break
                    await asyncio.sleep(1)

        status_final = ("<b>Promosi Dihentikan</b>" if job["stop"]
                        else "<b>Semua Putaran Promosi Selesai!</b>")
        await client.send_message(
            chat_id,
            _bq(f"{status_final} — Job #{job_id}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Total Putaran : {current_round}/{repeat_count}\n"
                f"Total Sukses  : {total_sukses}\n"
                f"Total Gagal   : {total_gagal}\n"
                f"Total Dilewati: {total_dilewati}"),
            parse_mode="html")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        try:
            await client.send_message(chat_id, _bq(f"<b>Promosi #{job_id} Error:</b> {e}"),
                                      parse_mode="html")
        except Exception:
            pass
    finally:
        _state["active_jobs"].pop(job_id, None)


# ---------------------------------------------------------------------------
# Public API untuk dik.py — semua bridge ke loop Telethon.
# ---------------------------------------------------------------------------
async def ubot_login_start(phone: str) -> dict:
    """Start login. Aman dipanggil dari loop bot (tidak blocking)."""
    phone = phone.strip().replace("+", "").replace(" ", "").replace("-", "")
    if not phone.startswith("62"):
        phone = "62" + phone.lstrip("0")

    async def _do():
        sess_data = _state["sessions"].get(phone)
        if sess_data and sess_data.get("string_session"):
            client = await _get_client(phone)
            if client:
                me = await client.get_me()
                return {"ok": True, "needs_code": False, "name": me.first_name or phone}

        client = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await asyncio.wait_for(client.connect(), timeout=20)
            result = await asyncio.wait_for(
                client.send_code_request(f"+{phone}"), timeout=15)
            _state["pending_login"] = {
                "phone": phone, "client": client,
                "phone_code_hash": result.phone_code_hash,
            }
            return {"ok": True, "needs_code": True, "phone": phone}
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            return {"ok": False, "error": str(e)}

    try:
        return await _await_in_ubot_loop(_do(), timeout=35)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Koneksi Telegram timeout. Coba lagi."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def ubot_login_verify(code: str) -> dict:
    """Verify OTP."""
    async def _do():
        pending = _state.get("pending_login")
        if not pending:
            return {"ok": False, "error": "Tidak ada login pending. /ubot dulu."}
        client = pending["client"]
        phone = pending["phone"]
        try:
            await asyncio.wait_for(
                client.sign_in(phone=f"+{phone}", code=code,
                               phone_code_hash=pending["phone_code_hash"]),
                timeout=15)
        except SessionPasswordNeededError:
            return {"ok": False, "needs_password": True, "phone": phone}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        me = await client.get_me()
        _state["sessions"][phone] = {
            "string_session": client.session.save(),
            "delay_min": DEFAULT_DELAY_MIN,
            "delay_max": DEFAULT_DELAY_MAX,
            "blacklist": [],
        }
        _save_state()
        _clients[phone] = client
        _register_handlers(client, phone)
        _state["pending_login"] = None
        return {"ok": True, "name": me.first_name or phone, "phone": phone}

    try:
        return await _await_in_ubot_loop(_do(), timeout=25)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Verifikasi timeout. Coba lagi."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def ubot_login_password(password: str) -> dict:
    """Submit 2FA password."""
    async def _do():
        pending = _state.get("pending_login")
        if not pending:
            return {"ok": False, "error": "Tidak ada login pending."}
        client = pending["client"]
        phone = pending["phone"]
        try:
            await asyncio.wait_for(client.sign_in(password=password), timeout=15)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        me = await client.get_me()
        _state["sessions"][phone] = {
            "string_session": client.session.save(),
            "delay_min": DEFAULT_DELAY_MIN,
            "delay_max": DEFAULT_DELAY_MAX,
            "blacklist": [],
        }
        _save_state()
        _clients[phone] = client
        _register_handlers(client, phone)
        _state["pending_login"] = None
        return {"ok": True, "name": me.first_name or phone, "phone": phone}

    try:
        return await _await_in_ubot_loop(_do(), timeout=25)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Timeout. Coba lagi."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def ubot_list_sessions() -> list[dict]:
    """List sessions. Cepat — tidak connect kalau belum pernah connect."""
    async def _do():
        out = []
        for phone, data in _state["sessions"].items():
            name = "?"
            connected = False
            client = _clients.get(phone)
            if client and client.is_connected():
                try:
                    me = await asyncio.wait_for(client.get_me(), timeout=5)
                    name = me.first_name or phone
                    connected = True
                except Exception:
                    pass
            out.append({
                "phone": phone,
                "name": name,
                "connected": connected,
                "delay": (f"{data.get('delay_min', DEFAULT_DELAY_MIN)}"
                          f"-{data.get('delay_max', DEFAULT_DELAY_MAX)}s"),
            })
        return out

    try:
        return await _await_in_ubot_loop(_do(), timeout=10)
    except Exception:
        return []


async def ubot_remove_session(phone: str) -> bool:
    """Remove session."""
    phone = phone.strip().replace("+", "").replace(" ", "").replace("-", "")
    if not phone.startswith("62"):
        phone = "62" + phone.lstrip("0")

    async def _do():
        await _disconnect_client(phone)

    try:
        await _await_in_ubot_loop(_do(), timeout=10)
    except Exception:
        pass
    removed = _state["sessions"].pop(phone, None) is not None
    if removed:
        _save_state()
    return removed


def ubot_get_state() -> dict:
    """Raw state untuk keyboard building."""
    return {
        "sessions": list(_state["sessions"].keys()),
        "active_jobs": len(_state["active_jobs"]),
        "pending_login": bool(_state.get("pending_login")),
    }
