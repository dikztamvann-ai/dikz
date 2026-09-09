"""
Instagram Handler — /ig command for dik.py
Usage: /ig 6285825306013
       /ig (reply .txt file)
Flow: search → send SMS → user reply nomor:kode → validate → reset password
"""
import asyncio, threading, re, logging, time
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

# Custom Emoji IDs (must match dik.py)
CE_INSTAGRAM = "5348533850429470257"
CE_NOMOR = "5422696450888842691"
CE_LOGIN = "5355034377921244938"
CE_LOADING = "5256024382337205926"
CE_LIVE = "5870903672937911120"
CE_HELP = "5870570722778156940"
CE_EMAIL = "5472239203590888751"
CE_DETAIL_NAME = "5316887736823591263"
CE_DETAIL_ID = "5262690351969215936"
CE_WAKTU = "5872756762347573066"
CE_FILE = "5870570722778156940"
E1 = "5796205953913196373"
E2 = "5420323339723881652"
E6 = "6003769830564434518"

# Will be set by dik.py on import
_screen = None
_em = None
_EMOJI = None
_USER_ID = None
_get_all_addusers = None
_get_ig_password = None
_maintenance_guard = None

# IG Server import
try:
    from ig_server1 import ig_search, ig_send_sms, ig_validate, ig_clean_phone
    IG_OK = True
except ImportError:
    IG_OK = False

# ═══ STATE ═══
_ig_active = {}   # user_id -> True/False
_ig_data = {}     # user_id -> {phone: {status, ...}}
IG_WORKERS = 15
IG_USER_LIMIT = 20
IG_ADDUSER_LIMIT = 150


def ig_init(screen_fn, em_fn, emoji_dict, owner_id, get_addusers_fn, get_pw_fn, guard_fn):
    """Initialize IG handler with dik.py dependencies."""
    global _screen, _em, _EMOJI, _USER_ID, _get_all_addusers, _get_ig_password, _maintenance_guard
    _screen = screen_fn
    _em = em_fn
    _EMOJI = emoji_dict
    _USER_ID = owner_id
    _get_all_addusers = get_addusers_fn
    _get_ig_password = get_pw_fn
    _maintenance_guard = guard_fn


async def ig_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ig command - Instagram SMS recovery."""
    user_id = update.effective_user.id
    if not IG_OK:
        await update.message.reply_text(f"{_em(E2,'❌')} ig_server1 module not available.", parse_mode=ParseMode.HTML)
        return

    # Parse input: reply .txt or args
    reply = update.message.reply_to_message
    if reply and reply.document and reply.document.file_name.endswith('.txt'):
        file = await context.bot.get_file(reply.document.file_id)
        raw = (await file.download_as_bytearray()).decode('utf-8', errors='ignore')
        phones = [l.strip() for l in raw.splitlines() if l.strip() and re.match(r'^\+?[\d\s-]{8,}$', l.strip())]
        if not phones:
            await update.message.reply_text(f"{_em(E2,'❌')} Tidak ada nomor valid di file.", parse_mode=ParseMode.HTML)
            return
    else:
        args = context.args
        if not args:
            await update.message.reply_text(
                _screen('INSTAGRAM', (
                    f"{_em(CE_INSTAGRAM,'📸')} <b>INSTAGRAM SMS RECOVERY</b>\n"
                    f"────────────────────────────\n\n"
                    f"Reset password akun Instagram via SMS.\n\n"
                    f"<b>{_em(CE_HELP,'📖')} Cara pakai:</b>\n"
                    f"  • <code>/ig 6285825306013</code>\n"
                    f"  • <code>/ig 628xxx 628yyy 628zzz</code>\n"
                    f"  • Reply file <code>.txt</code> dengan <code>/ig</code>\n\n"
                    f"<b>{_em(CE_LIVE,'📡')} Flow:</b>\n"
                    f"  1. Kirim SMS reset ke semua nomor\n"
                    f"  2. Reply dengan <code>nomor:kode</code>\n"
                    f"  3. Password di-reset + dapet session\n\n"
                    f"<b>{_em(E1,'✅')} Data yang didapat:</b>\n"
                    f"  {_em(CE_DETAIL_NAME,'👤')} Username\n"
                    f"  {_em(CE_EMAIL,'📩')} Email (masked)\n"
                    f"  {_em(CE_NOMOR,'📞')} Phone (masked)\n"
                    f"  {_em(CE_LOGIN,'🔐')} Password baru\n\n"
                    f"{_em(CE_WAKTU,'⏲')} <i>Format: 628xxx / +628xxx / 08xxx</i>"
                ), 'Home › Instagram'),
                parse_mode=ParseMode.HTML)
            return
        phones = [a.strip() for a in args if re.match(r'^\+?[\d]{8,}$', a.strip())]
        if not phones:
            await update.message.reply_text(f"{_em(E2,'❌')} Format nomor tidak valid.", parse_mode=ParseMode.HTML)
            return

    # Clean phones
    phones = [ig_clean_phone(p) for p in phones]

    # Limit enforcement
    is_owner = (user_id == _USER_ID)
    is_adduser = user_id in _get_all_addusers() if not is_owner else False
    if is_owner:
        limit = len(phones)
    elif is_adduser:
        limit = IG_ADDUSER_LIMIT
    else:
        limit = IG_USER_LIMIT
    if len(phones) > limit:
        phones = phones[:limit]

    # Store and start
    context.user_data[f"ig_phones_{user_id}"] = phones
    await _ig_start(update, context, user_id, phones)


async def _ig_start(update, context, user_id, phones):
    """Start parallel SMS send for all phones."""
    if _ig_active.get(user_id):
        await update.message.reply_text(f"{_em(E2,'⚠️')} IG masih aktif! Tunggu selesai.", parse_mode=ParseMode.HTML)
        return

    _ig_active[user_id] = True
    pw = _get_ig_password(user_id)

    msg = await update.message.reply_text(
        _screen('IG SERVER 1', (
            f"{_em(CE_INSTAGRAM,'📸')} <b>SMS RECOVERY</b>\n"
            f"────────────────────────────\n\n"
            f"  {_em(CE_NOMOR,'📞')} <b>Target:</b> {len(phones)} nomor\n"
            f"  {_em(E6,'🚀')} <b>Workers:</b> {IG_WORKERS}\n"
            f"  {_em(CE_LOGIN,'🔐')} <b>Password:</b> <code>{pw}</code>\n\n"
            f"{_em(CE_LOADING,'🔄')} <i>Mengirim SMS ke semua nomor...</i>"
        ), 'Home › Instagram › Server 1'),
        parse_mode=ParseMode.HTML)

    # Parallel send
    loop = asyncio.get_event_loop()
    results = {}
    lock = threading.Lock()

    def _process_one(phone):
        try:
            sms = ig_send_sms(phone)
            if sms.success:
                return phone, {"status": "sms_sent", "phone": phone}
            err = sms.error or "unknown"
            if err in ("account_not_found",) or "tidak ditemukan" in err.lower():
                return phone, {"status": "not_found", "error": err}
            if "recovery email" in err.lower() or err == "email_only_recovery":
                return phone, {"status": "sms_failed", "error": "Akun IG pakai email recovery, bukan SMS"}
            return phone, {"status": "sms_failed", "error": err}
        except Exception as e:
            return phone, {"status": "error", "error": str(e)[:60]}

    def _run_all():
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(IG_WORKERS, len(phones))) as pool:
            futs = []
            for i, p in enumerate(phones):
                if i > 0:
                    time.sleep(0.5)
                futs.append(pool.submit(_process_one, p))
            for f in futs:
                phone, res = f.result()
                with lock:
                    results[phone] = res

    await loop.run_in_executor(None, _run_all)

    # Categorize
    sms_sent = {p: r for p, r in results.items() if r["status"] == "sms_sent"}
    failed = {p: r for p, r in results.items() if r["status"] != "sms_sent"}

    _ig_data[user_id] = sms_sent

    if not sms_sent:
        _ig_active[user_id] = False
        body = f"{_em(E2,'❌')} <b>Tidak ada SMS terkirim!</b>\n────────────────────────────\n\n"
        for p, r in list(failed.items())[:20]:
            body += f"  {_em(E2,'❌')} +{p} — {r.get('error', r['status'])}\n"
        await msg.edit_text(
            _screen('IG SERVER 1', body, 'Home › Instagram › Server 1'),
            parse_mode=ParseMode.HTML)
        return

    # Show results
    body = (
        f"{_em(CE_INSTAGRAM,'📸')} <b>SMS TERKIRIM!</b>\n"
        f"────────────────────────────\n\n"
        f"  {_em(E1,'✅')} <b>Terkirim:</b> {len(sms_sent)}\n"
        f"  {_em(E2,'❌')} <b>Gagal:</b> {len(failed)}\n\n"
        f"<b>Nomor yang menunggu kode:</b>\n"
    )
    for i, (p, r) in enumerate(list(sms_sent.items())[:30], 1):
        body += f"  {i}. <code>+{p}</code>\n"
    if len(sms_sent) > 30:
        body += f"  <i>... +{len(sms_sent) - 30} lainnya</i>\n"

    body += (
        f"\n────────────────────────────\n"
        f"<b>Reply dengan format:</b>\n"
        f"<code>nomor:kode</code>\n\n"
        f"<b>Contoh:</b>\n"
        f"<code>6285825306013:123456</code>\n\n"
        f"<i>Bisa kirim banyak sekaligus (1 per baris)</i>"
    )

    code_msg = await msg.edit_text(
        _screen('IG SERVER 1', body, 'Home › Instagram › Server 1 › Waiting'),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Batal", callback_data=f"ig_cancel_{user_id}", style="danger"),
        ]]))
    context.user_data[f"ig_waiting_{user_id}"] = code_msg.message_id


async def ig_validate_codes(context, user_id: int, chat_id: int, codes_text: str):
    """Validate codes submitted by user. Format: phone:code per line."""
    sms_data = _ig_data.get(user_id, {})
    if not sms_data:
        return

    bot = context.bot
    lines = [l.strip() for l in codes_text.strip().splitlines() if ':' in l]
    if not lines:
        return

    # Normalize keys
    normalized_map = {}
    for k in sms_data:
        nk = k.replace('+', '').replace('-', '').replace(' ', '')
        normalized_map[nk] = k

    pairs = []
    for line in lines:
        parts = line.split(':', 1)
        if len(parts) != 2:
            continue
        phone = parts[0].strip().replace('+', '').replace('-', '').replace(' ', '')
        code = parts[1].strip()
        orig_key = normalized_map.get(phone)
        if orig_key and len(code) >= 4:
            pairs.append((orig_key, code))

    if not pairs:
        await bot.send_message(chat_id=chat_id,
            text=f"{_em(E2,'❌')} Format salah atau nomor tidak cocok. Pakai: <code>nomor:kode</code>",
            parse_mode=ParseMode.HTML)
        return

    progress = await bot.send_message(chat_id=chat_id,
        text=_screen('IG SERVER 1', f"{_em(CE_LOADING,'🔄')} <b>Memvalidasi {len(pairs)} kode...</b>",
                     'Home › Instagram › Server 1 › Validating'),
        parse_mode=ParseMode.HTML)

    loop = asyncio.get_event_loop()
    results = []
    pw = _get_ig_password(user_id)

    def _validate_one(phone, code):
        val = ig_validate(phone, code, password=pw)
        if val.success:
            return {"phone": phone, "status": "success", "uid": val.uid,
                    "username": val.username}
        else:
            return {"phone": phone, "status": "failed", "error": val.error or "Kode salah"}

    def _run():
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=IG_WORKERS) as pool:
            futs = [pool.submit(_validate_one, p, c) for p, c in pairs]
            for f in futs:
                results.append(f.result())

    await loop.run_in_executor(None, _run)

    # Remove validated
    for r in results:
        if r["status"] == "success":
            sms_data.pop(r["phone"], None)

    if not sms_data:
        _ig_active[user_id] = False
        context.user_data.pop(f"ig_waiting_{user_id}", None)
        _ig_data.pop(user_id, None)

    # Build results
    success = [r for r in results if r["status"] == "success"]
    failed_v = [r for r in results if r["status"] != "success"]

    summary = (
        f"{_em(CE_INSTAGRAM,'📸')} <b>HASIL VALIDASI</b>\n"
        f"────────────────────────────\n\n"
        f"  {_em(E1,'✅')} <b>Berhasil:</b> {len(success)}\n"
        f"  {_em(E2,'❌')} <b>Gagal:</b> {len(failed_v)}\n"
    )
    if success:
        summary += "\n"
        for r in success:
            summary += f"  {_em(E1,'✅')} +{r['phone']}\n"
            summary += f"     {_em(CE_DETAIL_NAME,'👤')} Username: <code>{r['username']}</code>\n"
            summary += f"     {_em(CE_DETAIL_ID,'🆔')} UID: <code>{r['uid']}</code>\n"
    if failed_v:
        summary += "\n"
        for r in failed_v:
            summary += f"  {_em(E2,'❌')} +{r['phone']} — {r['error']}\n"

    if sms_data:
        remaining = list(sms_data.keys())
        summary += "\n────────────────────────────\n"
        summary += f"<b>Masih menunggu kode ({len(remaining)}):</b>\n"
        for p in remaining:
            summary += f"  • <code>+{p}</code>\n"
        summary += "\n<i>Reply lagi dengan format nomor:kode</i>"

    try:
        await progress.edit_text(
            _screen('IG SERVER 1', summary, 'Home › Instagram › Server 1 › Results'),
            parse_mode=ParseMode.HTML)
    except:
        pass

    # Send .txt if success
    if success:
        from io import BytesIO
        file_lines = []
        file_lines.append(f"{'═'*50}")
        file_lines.append(f"  IG SERVER 1 RESULTS")
        file_lines.append(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        file_lines.append(f"  Password: {pw}")
        file_lines.append(f"{'═'*50}\n")
        for r in success:
            file_lines.append(f"  Phone    : +{r['phone']}")
            file_lines.append(f"  Username : {r['username']}")
            file_lines.append(f"  UID      : {r['uid']}")
            file_lines.append(f"  Pass     : {pw}")
            file_lines.append("")
        bio = BytesIO("\n".join(file_lines).encode('utf-8'))
        fname = f"ig_s1_{datetime.now().strftime('%H%M%S')}.txt"
        bio.name = fname
        await bot.send_document(chat_id=chat_id, document=bio, filename=fname,
            caption=f"{_em(CE_INSTAGRAM,'📸')} IG Server 1 | {_em(E1,'✅')} {len(success)} berhasil",
            parse_mode=ParseMode.HTML)


def is_ig_waiting(context, user_id: int) -> bool:
    """Check if user has active IG session waiting for codes."""
    return f"ig_waiting_{user_id}" in context.user_data and _ig_active.get(user_id, False)


def ig_cancel(user_id: int):
    """Cancel active IG session."""
    _ig_active[user_id] = False
    _ig_data.pop(user_id, None)
