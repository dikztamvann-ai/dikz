"""
WhatsApp Handler — /pair + /cekgb commands for dik.py
Communicates with wa_server.js via HTTP API (localhost:3891)
"""
import asyncio, re, logging, json
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

# Custom Emoji IDs (from dik.py)
CE_WHATSAPP = "5345943173401175849"
CE_NOMOR = "5422696450888842691"
CE_LOGIN = "5355034377921244938"
CE_LOADING = "5256024382337205926"
CE_LIVE = "5870903672937911120"
CE_HELP = "5870570722778156940"
CE_DETAIL_NAME = "5316887736823591263"
CE_DETAIL_ID = "5262690351969215936"
CE_AKUN = "5256143829672672750"
CE_ADD = "5348028367138483442"
CE_BACK = "5449847653586188540"
CE_FILE = "5870570722778156940"
CE_PROFILE = "5870994129244131212"
E1 = "5796205953913196373"
E2 = "5420323339723881652"
E6 = "6003769830564434518"

# Will be set by dik.py on import
_screen = None
_em = None
_USER_ID = None
_get_all_addusers = None

WA_API = "http://127.0.0.1:3891"
BATCH_SIZE = 5
_cekbio_cancelled = set()  # user_ids that cancelled cekbio
CEKBIO_SETTINGS_FILE = "wa_cekbio_settings.json"


def _get_cekbio_worker(user_id):
    """Get stored worker count for user (default 70)."""
    try:
        with open(CEKBIO_SETTINGS_FILE, "r") as f:
            data = json.load(f)
        return data.get(str(user_id), {}).get("worker", 70)
    except:
        return 70


def _set_cekbio_worker(user_id, worker):
    """Save worker setting for user."""
    try:
        with open(CEKBIO_SETTINGS_FILE, "r") as f:
            data = json.load(f)
    except:
        data = {}
    if str(user_id) not in data:
        data[str(user_id)] = {}
    data[str(user_id)]["worker"] = worker
    with open(CEKBIO_SETTINGS_FILE, "w") as f:
        json.dump(data, f)


def wa_init(screen_fn, em_fn, owner_id, get_addusers_fn):
    """Initialize WA handler with dik.py dependencies."""
    global _screen, _em, _USER_ID, _get_all_addusers
    _screen = screen_fn
    _em = em_fn
    _USER_ID = owner_id
    _get_all_addusers = get_addusers_fn


def _is_authorized(user_id):
    """Only owner + adduser can use WA features."""
    if user_id == _USER_ID:
        return True
    return user_id in _get_all_addusers()


async def _api(method, path, body=None):
    """Call wa_server.js HTTP API."""
    import urllib.request
    url = f"{WA_API}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if body else {}
    # Disconnect/logout bisa lebih lama
    timeout = 60 if path.startswith("/disconnect") or path == "/pair" else 30

    loop = asyncio.get_event_loop()
    def _do():
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            return {"error": str(e)}
    return await loop.run_in_executor(None, _do)


def _progress_bar(current, total, width=20):
    """Generate a cool progress bar."""
    pct = int((current / total) * 100) if total > 0 else 0
    filled = int(width * current / total) if total > 0 else 0
    bar = "▓" * filled + "░" * (width - filled)
    return f"[{bar}] {pct}%"


def _clean_phone(text):
    """Clean phone number from input."""
    phone = re.sub(r'[^0-9+]', '', text)
    phone = phone.lstrip('+')
    if phone.startswith("0"):
        phone = "62" + phone[1:]
    return phone


def _build_wa_hub(user_id, status):
    """Build WhatsApp hub body + keyboard from /status response."""
    connected = status.get("connected", False)
    wa_num = status.get("waNumber", "?")
    accounts_count = int(status.get("accountsCount") or (1 if connected else 0))

    if connected:
        status_line = (
            f"  {_em(E1,'✅')} <b>Status:</b> Connected\n"
            f"  {_em(CE_NOMOR,'📞')} <b>Sender:</b> <code>+{wa_num}</code>\n"
        )
        if accounts_count > 1:
            status_line += f"  {_em(CE_AKUN,'👥')} <b>Akun tersimpan:</b> {accounts_count}\n"
        body = (
            f"{_em(CE_WHATSAPP,'💬')} <b>WHATSAPP TOOLS</b>\n"
            f"────────────────────────────\n\n"
            f"{status_line}\n"
            f"────────────────────────────\n"
            f"Pilih fitur:"
        )
        kb = [
            [InlineKeyboardButton("Cek Bio", callback_data=f"wa_hub_cekbio_{user_id}", style="primary"),
             InlineKeyboardButton("Buat Grup", callback_data=f"wa_hub_buatgrup_{user_id}", style="primary")],
            [InlineKeyboardButton("Cek Grup", callback_data=f"wa_hub_cekgb_{user_id}", style="primary"),
             InlineKeyboardButton("Tambah Akun WA", callback_data=f"wa_hub_addakun_{user_id}", style="primary")],
        ]
        if user_id == _USER_ID:
            kb.append([InlineKeyboardButton("Citer GB", callback_data=f"wa_hub_citergb_{user_id}", style="primary")])
        kb.append([
            InlineKeyboardButton("Akun WA", callback_data=f"wa_hub_akun_{user_id}", style="primary"),
            InlineKeyboardButton("Disconnect", callback_data=f"wa_disconnect_{user_id}", style="danger"),
        ])
        kb.append([InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")])
    else:
        body = (
            f"{_em(CE_WHATSAPP,'💬')} <b>WHATSAPP TOOLS</b>\n"
            f"────────────────────────────\n\n"
            f"  {_em(E2,'❌')} <b>Status:</b> Disconnected\n"
            f"  {_em(CE_HELP,'💡')} <i>Pair WhatsApp dulu untuk menggunakan fitur.</i>\n\n"
            f"────────────────────────────"
        )
        kb = [
            [InlineKeyboardButton("Pair", callback_data=f"wa_hub_pair_{user_id}", style="primary")],
            [InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")],
        ]
    return body, kb


# ═══════════════════════════════════════
#  /wa COMMAND (HUB MENU)
# ═══════════════════════════════════════
async def wa_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /wa — show WhatsApp feature hub."""
    user_id = update.effective_user.id
    if not _is_authorized(user_id):
        return

    status = await _api("GET", f"/status/{user_id}")
    body, kb = _build_wa_hub(user_id, status)

    await update.message.reply_text(
        _screen('WHATSAPP', body, 'Home › WhatsApp'),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb))


# ═══════════════════════════════════════
#  /c COMMAND (QUICK CEKBIO)
# ═══════════════════════════════════════
async def c_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /c [nomor] — quick cek bio."""
    user_id = update.effective_user.id

    # Check connection
    status = await _api("GET", f"/status/{user_id}")
    if not status.get("connected"):
        await update.message.reply_text(
            f"{_em(E2,'❌')} WhatsApp belum terhubung. /pair dulu.",
            parse_mode=ParseMode.HTML)
        return

    # Parse phones from args, reply, or document
    phones = []
    args = context.args or []
    if args:
        tokens = re.split(r'[\s,;\n]+', " ".join(args))
        phones = [t for t in tokens if re.match(r'^\+?[\d]{8,}$', t)]

    if not phones and update.message.reply_to_message:
        reply = update.message.reply_to_message
        if reply.document:
            fname = reply.document.file_name or ""
            if fname.endswith(".txt") or fname.endswith(".ctc"):
                file = await context.bot.get_file(reply.document.file_id)
                raw = (await file.download_as_bytearray()).decode('utf-8', errors='ignore')
                for line in raw.splitlines():
                    found = re.findall(r'\+?(\d{6,15})', line)
                    for num in found:
                        if len(num) >= 8:
                            phones.append(num)
                phones = list(dict.fromkeys(phones))
        elif reply.text:
            tokens = re.split(r'[\s,;\n]+', reply.text)
            phones = [t for t in tokens if re.match(r'^\+?[\d]{8,}$', t)]

    if not phones and update.message.document:
        fname = update.message.document.file_name or ""
        if fname.endswith(".txt") or fname.endswith(".ctc"):
            file = await context.bot.get_file(update.message.document.file_id)
            raw = (await file.download_as_bytearray()).decode('utf-8', errors='ignore')
            for line in raw.splitlines():
                found = re.findall(r'\+?(\d{6,15})', line)
                for num in found:
                    if len(num) >= 8:
                        phones.append(num)
            phones = list(dict.fromkeys(phones))

    if not phones:
        await update.message.reply_text(
            _screen('CEK BIO', (
                f"{_em(CE_PROFILE,'👤')} <b>CEK BIO</b>\n"
                f"────────────────────────────\n\n"
                f"  <b>Format:</b>\n"
                f"  <code>/c 6285xxx</code>\n"
                f"  <code>/c 6285xxx 6281xxx</code>\n"
                f"  Reply file .txt dengan /c\n\n"
                f"────────────────────────────"
            ), 'Home › WhatsApp › Cek Bio'),
            parse_mode=ParseMode.HTML)
        return

    batch = _get_cekbio_worker(user_id)
    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Proses", callback_data=f"cekbio_start_{user_id}", style="success"),
         InlineKeyboardButton("Batal", callback_data=f"cekbio_abort_{user_id}", style="danger")]
    ])
    msg = await update.message.reply_text(
        _screen('CEK BIO', (
            f"{_em(CE_LOADING,'🔄')} <b>CEK {len(phones)} NOMOR?</b>\n"
            f"────────────────────────────\n\n"
            f"  {_em(CE_NOMOR,'📞')} Total: {len(phones)}\n"
            f"  {_em(E6,'🚀')} Worker: {batch} threads\n\n"
            f"────────────────────────────\n"
            f"{_em(CE_HELP,'💡')} <i>Klik Proses untuk mulai.</i>"
        ), 'Home › WhatsApp › Cek Bio'),
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb)

    # Store params for callback
    context.user_data[f"cekbio_params_{user_id}"] = {
        "phones": phones, "batch": batch, "msg_id": msg.message_id, "chat_id": update.effective_chat.id
    }


# ═══════════════════════════════════════
#  /listwa COMMAND (OWNER ONLY)
# ═══════════════════════════════════════
async def listwa_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /listwa — list all active WA senders."""
    user_id = update.effective_user.id
    if user_id != _USER_ID:
        return

    result = await _api("GET", "/sessions")
    sessions = result.get("sessions", [])

    if not sessions:
        await update.message.reply_text(
            _screen('LIST WA', (
                f"{_em(E2,'❌')} <b>Tidak ada sender aktif.</b>\n"
                f"────────────────────────────\n"
                f"<i>Pair dulu dengan /pair</i>"
            ), 'Home › WhatsApp › List Sender'),
            parse_mode=ParseMode.HTML)
        return

    body = (
        f"{_em(CE_WHATSAPP,'💬')} <b>LIST SENDER WHATSAPP</b>\n"
        f"────────────────────────────\n\n"
        f"  {_em(CE_AKUN,'👥')} <b>Total:</b> {len(sessions)}\n\n"
    )
    kb = []
    row = []
    for i, s in enumerate(sessions):
        status_em = _em(E1,'✅') if s.get("connected") else _em(E2,'❌')
        wa_num = s.get("waNumber") or "?"
        uid = s.get("uid", "?")
        body += f"  {i+1}. {status_em} <code>+{wa_num}</code> (uid: {uid})\n"
        row.append(InlineKeyboardButton(str(i+1), callback_data=f"wa_switch_{user_id}_{uid}", style="primary"))
        if len(row) == 3:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")])

    body += (
        f"\n────────────────────────────\n"
        f"<i>Klik nomor untuk switch sender.</i>"
    )

    await update.message.reply_text(
        _screen('LIST WA', body, 'Home › WhatsApp › List Sender'),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb))


# ═══════════════════════════════════════
#  /pair COMMAND
# ═══════════════════════════════════════
async def pair_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /pair [nomor] — connect WhatsApp via pairing code."""
    user_id = update.effective_user.id

    # Auth check
    if not _is_authorized(user_id):
        return

    # Check if already connected
    status = await _api("GET", f"/status/{user_id}")
    if status.get("connected"):
        await update.message.reply_text(
            _screen('WHATSAPP', (
                f"{_em(CE_WHATSAPP,'💬')} <b>WHATSAPP PAIRED</b>\n"
                f"────────────────────────────\n\n"
                f"  {_em(E1,'✅')} <b>Status:</b> Connected\n"
                f"  {_em(CE_NOMOR,'📞')} <b>Nomor:</b> <code>+{status.get('waNumber','?')}</code>\n\n"
                f"────────────────────────────\n"
                f"{_em(CE_HELP,'💡')} <i>Sudah terhubung. Gunakan /cekgb untuk cek grup.</i>"
            ), 'Home › WhatsApp'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Disconnect", callback_data=f"wa_disconnect_{user_id}", style="danger")],
            ]))
        return

    # Check if phone number provided inline: /pair 628xxx
    args = context.args
    if args and len(args) > 0:
        phone = _clean_phone(args[0])
        if len(phone) >= 8:
            await _do_pairing(update, context, user_id, phone)
            return

    # Ask for phone number
    context.user_data[f"wa_pairing_{user_id}"] = True
    await update.message.reply_text(
        _screen('PAIRING WHATSAPP', (
            f"{_em(CE_WHATSAPP,'💬')} <b>PAIRING WHATSAPP</b>\n"
            f"────────────────────────────\n\n"
            f"  {_em(CE_NOMOR,'📞')} Masukkan nomor WhatsApp:\n\n"
            f"  <b>Format:</b> <code>/pair 628xxxxxxxxxx</code>\n"
            f"  atau kirim nomor langsung\n\n"
            f"────────────────────────────\n"
            f"{_em(CE_HELP,'💡')} <i>Semua negara didukung.</i>"
        ), 'Home › WhatsApp › Pairing'),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")],
        ]))


async def _do_pairing(update, context, user_id, phone, add_account=False):
    """Execute pairing flow. add_account=True → simpan sebagai akun cadangan."""
    title = 'TAMBAH AKUN WA' if add_account else 'PAIRING WHATSAPP'
    msg = await update.message.reply_text(
        _screen(title, (
            f"{_em(CE_LOADING,'🔄')} <b>Generating Pairing Code...</b>\n"
            f"────────────────────────────\n\n"
            f"  {_em(CE_NOMOR,'📞')} Nomor: <code>+{phone}</code>\n"
            f"  {_em(CE_LOADING,'🔄')} <i>Connecting to WhatsApp servers...</i>\n\n"
            f"────────────────────────────\n"
            f"{_em(CE_HELP,'💡')} <i>Proses ~5 detik.</i>"
        ), f'Home › WhatsApp › {"Tambah Akun" if add_account else "Pairing"}'),
        parse_mode=ParseMode.HTML)

    payload = {"userId": str(user_id), "phone": phone}
    if add_account:
        payload["addAccount"] = True
    result = await _api("POST", "/pair", payload)

    if result.get("error"):
        await msg.edit_text(
            _screen(title, (
                f"{_em(E2,'❌')} <b>GAGAL</b>\n"
                f"────────────────────────────\n\n"
                f"  Error: {result['error']}\n\n"
                f"{_em(CE_HELP,'💡')} <i>Pastikan wa_server.js berjalan.</i>"
            ), f'Home › WhatsApp › {"Tambah Akun" if add_account else "Pairing"}'),
            parse_mode=ParseMode.HTML)
        return

    code = result.get("code", "????-????")
    if code and len(code) == 8 and "-" not in code:
        code = f"{code[:4]}-{code[4:]}"

    mode_line = ('  ' + _em(CE_AKUN,'👥') + ' <b>Mode:</b> Akun cadangan\n') if add_account else ''

    await msg.edit_text(
        _screen(title, (
            f"{_em(CE_WHATSAPP,'💬')} <b>PAIRING CODE</b>\n"
            f"────────────────────────────\n\n"
            f"  {_em(CE_NOMOR,'📞')} <code>+{phone}</code>\n"
            f"{mode_line}"
            f"\n  {_em(CE_LOGIN,'🔐')} Code:\n"
            f"  <code>{code}</code>\n\n"
            f"────────────────────────────\n"
            f"{_em(CE_HELP,'💡')} <b>Linked Devices → Link with Phone Number</b>\n"
            f"<i>Kode berlaku 60 detik.</i>"
        ), f'Home › WhatsApp › {"Tambah Akun" if add_account else "Pairing"}'),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cek Status", callback_data=f"wa_checkstatus_{user_id}", style="primary")],
            [InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")],
        ]))

async def pair_handle_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle phone number input for pairing."""
    user_id = update.effective_user.id
    if not context.user_data.get(f"wa_pairing_{user_id}"):
        return False

    text = update.message.text.strip()
    phone = _clean_phone(text)
    if len(phone) < 8:
        return False

    add_account = bool(context.user_data.pop(f"wa_add_account_{user_id}", False))
    context.user_data.pop(f"wa_pairing_{user_id}", None)
    await _do_pairing(update, context, user_id, phone, add_account=add_account)
    return True


# ═══════════════════════════════════════
#  /cekgb COMMAND
# ═══════════════════════════════════════
async def cekgb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cekgb — check WhatsApp groups where user is admin."""
    user_id = update.effective_user.id

    # Auth check
    if not _is_authorized(user_id):
        return

    # Check connection
    status = await _api("GET", f"/status/{user_id}")
    if not status.get("connected"):
        await update.message.reply_text(
            _screen('CEK GRUP', (
                f"{_em(E2,'❌')} <b>WhatsApp belum terhubung!</b>\n"
                f"────────────────────────────\n\n"
                f"  Gunakan <code>/pair nomor</code> untuk connect dulu.\n\n"
                f"{_em(CE_HELP,'💡')} <i>Pairing diperlukan sebelum cek grup.</i>"
            ), 'Home › WhatsApp › Cek Grup'),
            parse_mode=ParseMode.HTML)
        return

    msg = await update.message.reply_text(
        _screen('CEK GRUP', (
            f"{_em(CE_LOADING,'🔄')} <i>Mengambil data grup...</i>"
        ), 'Home › WhatsApp › Cek Grup'),
        parse_mode=ParseMode.HTML)

    result = await _api("GET", f"/groups/{user_id}")

    if result.get("error"):
        await msg.edit_text(
            _screen('CEK GRUP', (
                f"{_em(E2,'❌')} <b>Gagal mengambil data grup</b>\n"
                f"────────────────────────────\n\n"
                f"  Error: {result['error']}"
            ), 'Home › WhatsApp › Cek Grup'),
            parse_mode=ParseMode.HTML)
        return

    groups = result.get("groups", [])
    total = result.get("total", 0)

    if not groups:
        await msg.edit_text(
            _screen('CEK GRUP', (
                f"{_em(E2,'❌')} <b>Tidak ada grup ditemukan</b>\n"
                f"────────────────────────────\n\n"
                f"  Total grup: {total}\n\n"
                f"{_em(CE_HELP,'💡')} <i>Pastikan WA sudah terhubung dengan benar.</i>"
            ), 'Home › WhatsApp › Cek Grup'),
            parse_mode=ParseMode.HTML)
        return

    context.user_data[f"wa_groups_{user_id}"] = groups

    body = (
        f"{_em(CE_WHATSAPP,'💬')} <b>GRUP WHATSAPP</b>\n"
        f"────────────────────────────\n\n"
        f"  {_em(CE_AKUN,'👥')} <b>Total:</b> {total}\n"
        f"  {_em(E1,'✅')} <b>Ditampilkan:</b> {len(groups)}\n\n"
        f"────────────────────────────\n"
        f"Pilih grup:"
    )

    kb = []
    for i, g in enumerate(groups[:20]):
        lid_count = sum(1 for p in g.get("participants", []) if p.get("isLid"))
        label = f"{g['name'][:22]} ({g['members']}m)"
        if lid_count > 0:
            label += f" L:{lid_count}"
        kb.append([InlineKeyboardButton(label, callback_data=f"wa_grup_{user_id}_{i}", style="primary")])

    kb.append([InlineKeyboardButton("Tutup", callback_data=f"wa_cancel_{user_id}", style="danger")])

    await msg.edit_text(
        _screen('CEK GRUP', body, 'Home › WhatsApp › Cek Grup'),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb))


# ═══════════════════════════════════════
#  AUTO-REFRESH PROGRESS (every BATCH_SIZE)
# ═══════════════════════════════════════
async def _auto_refresh_citer(context, chat_id, message_id, user_id, gid, gname):
    """Background task: auto-refresh protect group progress."""
    bot = context.bot
    steps = ["Lock Info", "Join Approval", "Announce Test", "Invite Code", "Verify"]
    last_progress = -1
    while True:
        await asyncio.sleep(2)
        status = await _api("GET", f"/citer-status/{user_id}/{gid}")
        if not status or not status.get("running", False):
            progress = status.get("progress", 0)
            total = status.get("total", 5)
            results = status.get("error", "") or ""
            bar = _progress_bar(progress, total)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text=_screen('PROTECT GROUP', (
                        f"{_em(E1,'✅')} <b>PROTECT SELESAI</b>\n"
                        f"────────────────────────────\n\n"
                        f"  {_em(CE_AKUN,'👥')} <b>Grup:</b> {gname[:22]}\n\n"
                        f"  {bar}\n\n"
                        f"  {_em(E1,'✅')} Lock Info: Set\n"
                        f"  {_em(E1,'✅')} Join Approval: ON\n"
                        f"  {_em(E1,'✅')} Announcement: Tested\n"
                        f"  {_em(E1,'✅')} Invite Code: Fresh\n"
                        f"  {_em(E1,'✅')} Verified\n\n"
                        f"────────────────────────────\n"
                        f"{_em(CE_HELP,'💡')} <i>Grup dilindungi. Anti mass-report.</i>"
                    ), 'Home › WhatsApp › Protect'),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Kembali", callback_data=f"wa_back_groups_{user_id}", style="primary")],
                    ]))
            except: pass
            break

        progress = status.get("progress", 0)
        total = status.get("total", 5)
        if progress > last_progress:
            last_progress = progress
            bar = _progress_bar(progress, total)
            step_lines = ""
            for idx, s in enumerate(steps):
                if idx < progress:
                    step_lines += f"  {_em(E1,'✅')} {s}: Done\n"
                elif idx == progress:
                    step_lines += f"  {_em(CE_LOADING,'🔄')} {s}: Running...\n"
                else:
                    step_lines += f"  ░ {s}: Pending\n"
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text=_screen('PROTECT GROUP', (
                        f"{_em(CE_LOADING,'🔄')} <b>PROTECTING...</b>\n"
                        f"────────────────────────────\n\n"
                        f"  {_em(CE_AKUN,'👥')} <b>Grup:</b> {gname[:22]}\n\n"
                        f"  {bar}\n\n"
                        f"{step_lines}\n"
                        f"────────────────────────────"
                    ), 'Home › WhatsApp › Protect'),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Stop", callback_data=f"wa_citerstop_{user_id}_0", style="danger")],
                    ]))
            except: pass


async def _auto_refresh_addmember(context, chat_id, message_id, user_id, gid, gname):
    """Background task: auto-refresh add member progress every batch."""
    bot = context.bot
    last_count = -1
    while True:
        await asyncio.sleep(5)
        status = await _api("GET", f"/addmember-status/{user_id}/{gid}")
        if not status or not status.get("running", False):
            # Final update
            added = status.get("added", 0)
            failed = status.get("failed", 0)
            total = status.get("total", 0)
            skipped = status.get("skipped", 0)
            bar = _progress_bar(added + failed, total)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text=_screen('ADD MEMBER', (
                        f"{_em(E1,'✅')} <b>ADD MEMBER SELESAI</b>\n"
                        f"────────────────────────────\n\n"
                        f"  {_em(CE_AKUN,'👥')} <b>Grup:</b> {gname[:22]}\n\n"
                        f"  {bar}\n\n"
                        f"  {_em(E1,'✅')} Added: <b>{added}</b>\n"
                        f"  {_em(E2,'❌')} Failed: <b>{failed}</b>\n"
                        f"  Skipped: {skipped}\n\n"
                        f"────────────────────────────\n"
                        f"{_em(CE_HELP,'💡')} <i>Selesai. {added} member berhasil ditambahkan.</i>"
                    ), 'Home › WhatsApp › Add Member'),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Kembali", callback_data=f"wa_back_groups_{user_id}", style="primary")],
                    ]))
            except: pass
            break

        added = status.get("added", 0)
        failed = status.get("failed", 0)
        total = status.get("total", 0)
        current = added + failed

        # Update every BATCH_SIZE processed
        if current // BATCH_SIZE > last_count // BATCH_SIZE:
            last_count = current
            bar = _progress_bar(current, total)
            batch_num = current // BATCH_SIZE
            total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text=_screen('ADD MEMBER', (
                        f"{_em(CE_ADD,'✨')} <b>ADD MEMBER RUNNING</b>\n"
                        f"────────────────────────────\n\n"
                        f"  {_em(CE_AKUN,'👥')} <b>Grup:</b> {gname[:22]}\n"
                        f"  {_em(E6,'🚀')} <b>Wave:</b> {batch_num}/{total_batches}\n\n"
                        f"  {bar}\n\n"
                        f"  {_em(E1,'✅')} Added: <b>{added}</b>\n"
                        f"  {_em(E2,'❌')} Failed: <b>{failed}</b>\n"
                        f"  Remaining: {total - current}\n\n"
                        f"────────────────────────────\n"
                        f"{_em(CE_HELP,'💡')} <i>Auto-refresh setiap {BATCH_SIZE} nomor.</i>"
                    ), 'Home › WhatsApp › Add Member'),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Stop", callback_data=f"wa_addstop_{user_id}", style="danger")],
                    ]))
            except: pass


# ═══════════════════════════════════════
#  CALLBACKS
# ═══════════════════════════════════════
async def wa_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all wa_ callbacks."""
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    # Auth check
    if not _is_authorized(user_id):
        return False

    # cekbio_cancel_ (during processing)
    if data.startswith("cekbio_cancel_"):
        _cekbio_cancelled.add(user_id)
        await query.answer("Dibatalkan, mengirim hasil partial...")
        return True

    # cekbio_start_ (confirm to start)
    if data.startswith("cekbio_start_"):
        await query.answer()
        params = context.user_data.pop(f"cekbio_params_{user_id}", None)
        if not params:
            return True
        phones = params["phones"]
        batch = params["batch"]
        msg_id = params["msg_id"]
        chat_id = params["chat_id"]

        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data=f"cekbio_cancel_{user_id}", style="danger")]])
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=_screen('CEK BIO', (
                    f"{_em(CE_LOADING,'🔄')} <b>CHECKING {len(phones)} NOMOR...</b>\n"
                    f"────────────────────────────\n\n"
                    f"  {_progress_bar(0, len(phones))}\n\n"
                    f"  {_em(E1,'✅')} Checked: 0/{len(phones)}\n"
                    f"  {_em(E6,'🚀')} Worker: {batch} threads\n\n"
                    f"────────────────────────────"
                ), 'Home › WhatsApp › Cek Bio'),
                parse_mode=ParseMode.HTML,
                reply_markup=cancel_kb)
        except: pass

        result = await _api("POST", f"/cekbio/{user_id}", {"phones": phones, "batch": batch})
        if result.get("error"):
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id,
                    text=_screen('CEK BIO', f"{_em(E2,'❌')} <b>Gagal:</b> {result['error']}", 'Home › WhatsApp › Cek Bio'),
                    parse_mode=ParseMode.HTML)
            except: pass
            return True

        asyncio.create_task(_cekbio_monitor(context, chat_id, msg_id, user_id, len(phones), batch))
        return True

    # cekbio_abort_ (cancel before start)
    if data.startswith("cekbio_abort_"):
        await query.answer("Dibatalkan.")
        context.user_data.pop(f"cekbio_params_{user_id}", None)
        try:
            await query.edit_message_text(
                text=_screen('CEK BIO', f"{_em(E2,'❌')} <b>Dibatalkan.</b>", 'Home › WhatsApp › Cek Bio'),
                parse_mode=ParseMode.HTML)
        except: pass
        return True

    # wa_cancel_
    if data.startswith("wa_cancel_"):
        await query.answer()
        context.user_data.pop(f"wa_pairing_{user_id}", None)
        context.user_data.pop(f"wa_add_account_{user_id}", None)
        context.user_data.pop(f"wa_awaiting_members_{user_id}", None)
        context.user_data.pop(f"wa_buatgrup_custom_{user_id}", None)
        context.user_data.pop(f"wa_buatgrup_waiting_{user_id}", None)
        await query.message.delete()
        return True

    # wa_hub_addakun_ → pair akun cadangan
    if data.startswith("wa_hub_addakun_"):
        await query.answer()
        context.user_data[f"wa_pairing_{user_id}"] = True
        context.user_data[f"wa_add_account_{user_id}"] = True
        await query.message.edit_text(
            _screen('TAMBAH AKUN WA', (
                f"{_em(CE_WHATSAPP,'💬')} <b>TAMBAH AKUN CADANGAN</b>\n"
                f"────────────────────────────\n\n"
                f"  {_em(CE_NOMOR,'📞')} Masukkan nomor WhatsApp:\n\n"
                f"  <b>Format:</b> <code>628xxxxxxxxxx</code>\n\n"
                f"  {_em(CE_HELP,'💡')} Akun aktif tetap tersimpan."
            ), 'Home › WhatsApp › Tambah Akun'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")],
            ]))
        return True

    # wa_hub_akun_ → list akun + switch/hapus
    if data.startswith("wa_hub_akun_"):
        await query.answer()
        result = await _api("GET", f"/accounts/{user_id}")
        accounts = result.get("accounts") or []
        if not accounts:
            await query.message.edit_text(
                _screen('AKUN WA', (
                    f"{_em(E2,'❌')} <b>Belum ada akun WA.</b>"
                ), 'Home › WhatsApp › Akun'),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Kembali", callback_data=f"wa_hub_back_{user_id}", style="danger")],
                ]))
            return True
        body = (
            f"{_em(CE_AKUN,'👥')} <b>AKUN WHATSAPP</b>\n"
            f"────────────────────────────\n\n"
            f"  Total: <b>{len(accounts)}</b>\n\n"
        )
        kb = []
        context.user_data[f"wa_acc_keys_{user_id}"] = [a.get("key") for a in accounts]
        for i, acc in enumerate(accounts):
            num = acc.get("waNumber") or "?"
            active = "🟢" if acc.get("active") else "⚪️"
            conn = "✅" if acc.get("connected") else "❌"
            body += f"  {active} {conn} <code>+{num}</code>\n"
            row = []
            if not acc.get("active"):
                row.append(InlineKeyboardButton(
                    f"Pakai #{i+1}", callback_data=f"wa_accswitch_{user_id}_{i}", style="primary"))
            row.append(InlineKeyboardButton(
                f"Hapus #{i+1}", callback_data=f"wa_delakun_{user_id}_{i}", style="danger"))
            if row:
                kb.append(row)
        body += (
            f"\n────────────────────────────\n"
            f"<i>Pakai = set aktif. Hapus = disconnect + hapus session.</i>"
        )
        kb.append([InlineKeyboardButton("Kembali", callback_data=f"wa_hub_back_{user_id}", style="danger")])
        await query.message.edit_text(
            _screen('AKUN WA', body, 'Home › WhatsApp › Akun'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb))
        return True

    # wa_hub_back_
    if data.startswith("wa_hub_back_"):
        await query.answer()
        status = await _api("GET", f"/status/{user_id}")
        body, kb = _build_wa_hub(user_id, status)
        try:
            await query.message.edit_text(
                _screen('WHATSAPP', body, 'Home › WhatsApp'),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            pass
        return True

    # wa_hub_pair_ → trigger pairing flow
    if data.startswith("wa_hub_pair_"):
        await query.answer()
        context.user_data[f"wa_pairing_{user_id}"] = True
        context.user_data.pop(f"wa_add_account_{user_id}", None)
        await query.message.edit_text(
            _screen('PAIRING WHATSAPP', (
                f"{_em(CE_WHATSAPP,'💬')} <b>PAIRING WHATSAPP</b>\n"
                f"────────────────────────────\n\n"
                f"  {_em(CE_NOMOR,'📞')} Masukkan nomor WhatsApp:\n\n"
                f"  <b>Format:</b> <code>628xxxxxxxxxx</code>\n"
                f"  atau kirim nomor langsung\n\n"
                f"────────────────────────────\n"
                f"{_em(CE_HELP,'💡')} <i>Semua negara didukung.</i>"
            ), 'Home › WhatsApp › Pairing'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")],
            ]))
        return True

    # wa_hub_buatgrup_ → trigger buat grup flow
    if data.startswith("wa_hub_buatgrup_"):
        await query.answer()
        status = await _api("GET", f"/status/{user_id}")
        if not status.get("connected"):
            await query.answer("WhatsApp belum terhubung! Pair dulu.", show_alert=True)
            return True
        kb = [
            [InlineKeyboardButton("1 Grup", callback_data=f"wa_buatgrup_{user_id}_1", style="primary"),
             InlineKeyboardButton("2 Grup", callback_data=f"wa_buatgrup_{user_id}_2", style="primary"),
             InlineKeyboardButton("3 Grup", callback_data=f"wa_buatgrup_{user_id}_3", style="primary")],
            [InlineKeyboardButton("4 Grup", callback_data=f"wa_buatgrup_{user_id}_4", style="primary"),
             InlineKeyboardButton("5 Grup", callback_data=f"wa_buatgrup_{user_id}_5", style="primary"),
             InlineKeyboardButton("Custom", callback_data=f"wa_buatgrup_{user_id}_custom", style="primary")],
            [InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")],
        ]
        await query.message.edit_text(
            _screen('BUAT GRUP', (
                f"{_em(CE_ADD,'✨')} <b>BUAT GRUP BARU</b>\n"
                f"────────────────────────────\n\n"
                f"  Buat grup baru dengan member langsung\n"
                f"  di dalamnya (anti suspend).\n\n"
                f"  {_em(CE_HELP,'💡')} <b>Kenapa ini lebih aman?</b>\n"
                f"  WhatsApp gak suspend grup yang\n"
                f"  DIBUAT dengan member dari awal.\n\n"
                f"────────────────────────────\n"
                f"Pilih jumlah grup:"
            ), 'Home › WhatsApp › Buat Grup'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb))
        return True

    # wa_hub_cekgb_ → trigger cek grup flow
    if data.startswith("wa_hub_cekgb_"):
        await query.answer()
        status = await _api("GET", f"/status/{user_id}")
        if not status.get("connected"):
            await query.answer("WhatsApp belum terhubung! Pair dulu.", show_alert=True)
            return True
        await query.message.edit_text(
            _screen('CEK GRUP', (
                f"{_em(CE_LOADING,'🔄')} <i>Mengambil data grup...</i>"
            ), 'Home › WhatsApp › Cek Grup'),
            parse_mode=ParseMode.HTML)
        result = await _api("GET", f"/groups/{user_id}")
        if result.get("error"):
            await query.message.edit_text(
                _screen('CEK GRUP', (
                    f"{_em(E2,'❌')} <b>Gagal mengambil data grup</b>\n"
                    f"────────────────────────────\n\n"
                    f"  Error: {result['error']}"
                ), 'Home › WhatsApp › Cek Grup'),
                parse_mode=ParseMode.HTML)
            return True
        groups = result.get("groups", [])
        if not groups:
            await query.message.edit_text(
                _screen('CEK GRUP', (
                    f"{_em(E2,'❌')} <b>Tidak ada grup ditemukan</b>\n"
                    f"────────────────────────────\n\n"
                    f"{_em(CE_HELP,'💡')} <i>Pastikan WA sudah terhubung dengan benar.</i>"
                ), 'Home › WhatsApp › Cek Grup'),
                parse_mode=ParseMode.HTML)
            return True
        context.user_data[f"wa_groups_{user_id}"] = groups
        body = (
            f"{_em(CE_WHATSAPP,'💬')} <b>GRUP WHATSAPP</b>\n"
            f"────────────────────────────\n\n"
            f"  {_em(CE_AKUN,'👥')} <b>Total:</b> {len(groups)}\n\n"
            f"────────────────────────────\n"
            f"Pilih grup untuk Citer GB / Add Member:"
        )
        kb = []
        for i, g in enumerate(groups[:20]):
            label = f"{g['name'][:22]} ({g['members']}m)"
            kb.append([InlineKeyboardButton(label, callback_data=f"wa_grup_{user_id}_{i}", style="primary")])
        kb.append([InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")])
        await query.message.edit_text(
            _screen('CEK GRUP', body, 'Home › WhatsApp › Cek Grup'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb))
        return True

    # wa_hub_citergb_ → owner only, show groups for Citer GB
    if data.startswith("wa_hub_citergb_"):
        if user_id != _USER_ID:
            await query.answer("Fitur ini hanya untuk owner.", show_alert=True)
            return True
        await query.answer()
        await query.message.edit_text(
            _screen('CITER GB', (
                f"{_em(CE_LOADING,'🔄')} <i>Mengambil data grup...</i>"
            ), 'Home › WhatsApp › Citer GB'),
            parse_mode=ParseMode.HTML)
        result = await _api("GET", f"/groups/{user_id}")
        groups = result.get("groups", [])
        if not groups:
            await query.message.edit_text(
                _screen('CITER GB', (
                    f"{_em(E2,'❌')} Tidak ada grup ditemukan."
                ), 'Home › WhatsApp › Citer GB'),
                parse_mode=ParseMode.HTML)
            return True
        context.user_data[f"wa_groups_{user_id}"] = groups
        kb = []
        for i, g in enumerate(groups[:20]):
            label = f"{g['name'][:22]} ({g['members']}m)"
            kb.append([InlineKeyboardButton(label, callback_data=f"wa_citer_{user_id}_{i}", style="primary")])
        kb.append([InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")])
        await query.message.edit_text(
            _screen('CITER GB', (
                f"{_em(CE_WHATSAPP,'💬')} <b>CITER GB — PILIH GRUP</b>\n"
                f"────────────────────────────\n\n"
                f"  {_em(CE_AKUN,'👥')} Total: {len(groups)}\n\n"
                f"────────────────────────────\n"
                f"Pilih grup untuk protect:"
            ), 'Home › WhatsApp › Citer GB'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb))
        return True

    # wa_hub_cekbio_ → trigger cek bio flow (ALL users)
    if data.startswith("wa_hub_cekbio_"):
        await query.answer()
        status = await _api("GET", f"/status/{user_id}")
        if not status.get("connected"):
            await query.answer("WhatsApp belum terhubung! Pair dulu.", show_alert=True)
            return True
        worker = _get_cekbio_worker(user_id)
        kb = [
            [InlineKeyboardButton("Setting", callback_data=f"wa_cekbio_setting_{user_id}", style="primary"),
             InlineKeyboardButton("Cek Bio", callback_data=f"wa_cekbio_start_{user_id}", style="primary")],
            [InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")],
        ]
        await query.message.edit_text(
            _screen('CEK BIO', (
                f"{_em(CE_PROFILE,'👤')} <b>CEK BIO WHATSAPP</b>\n"
                f"────────────────────────────\n\n"
                f"  {_em(CE_HELP,'💡')} Analisis nomor WhatsApp:\n"
                f"  • Nama, Bio, Business Meta\n"
                f"  • Tier: Eklusif/Standart/Low/Suite\n"
                f"  • WA Biasa / Tidak Terdaftar\n\n"
                f"  {_em(E6,'🚀')} <b>Worker:</b> {worker} threads\n\n"
                f"────────────────────────────\n"
                f"<i>Atau langsung: /c nomor</i>"
            ), 'Home › WhatsApp › Cek Bio'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb))
        return True

    # wa_cekbio_setting_ → show worker setting
    if data.startswith("wa_cekbio_setting_"):
        await query.answer()
        kb = [
            [InlineKeyboardButton("10", callback_data=f"wa_cekbio_setw_{user_id}_10", style="primary"),
             InlineKeyboardButton("50", callback_data=f"wa_cekbio_setw_{user_id}_50", style="primary"),
             InlineKeyboardButton("95", callback_data=f"wa_cekbio_setw_{user_id}_95", style="primary")],
            [InlineKeyboardButton("Custom", callback_data=f"wa_cekbio_setw_{user_id}_custom", style="primary"),
             InlineKeyboardButton("Kembali", callback_data=f"wa_hub_cekbio_{user_id}", style="danger")],
        ]
        worker = _get_cekbio_worker(user_id)
        await query.message.edit_text(
            _screen('CEK BIO', (
                f"{_em(CE_PROFILE,'👤')} <b>SETTING WORKER</b>\n"
                f"────────────────────────────\n\n"
                f"  {_em(E6,'🚀')} Worker saat ini: <b>{worker}</b>\n\n"
                f"  Pilih jumlah threads:\n\n"
                f"────────────────────────────\n"
                f"{_em(CE_HELP,'💡')} <i>Makin tinggi = makin cepat.</i>"
            ), 'Home › WhatsApp › Cek Bio › Setting'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb))
        return True

    # wa_cekbio_setw_{uid}_{val}
    if data.startswith("wa_cekbio_setw_"):
        await query.answer()
        parts = data.split("_")
        if len(parts) >= 5:
            val = parts[4]
            if val == "custom":
                context.user_data[f"wa_cekbio_custom_batch_{user_id}"] = True
                await query.message.edit_text(
                    _screen('CEK BIO', (
                        f"{_em(CE_PROFILE,'👤')} <b>CUSTOM THREADS</b>\n"
                        f"────────────────────────────\n\n"
                        f"  Ketik jumlah threads (1-200):"
                    ), 'Home › WhatsApp › Cek Bio › Setting'),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Batal", callback_data=f"wa_hub_cekbio_{user_id}", style="danger")],
                    ]))
            else:
                _set_cekbio_worker(user_id, int(val))
                await query.answer(f"Worker diset ke {val}!", show_alert=True)
                # Re-show cekbio menu
                worker = _get_cekbio_worker(user_id)
                kb = [
                    [InlineKeyboardButton("Setting", callback_data=f"wa_cekbio_setting_{user_id}", style="primary"),
                     InlineKeyboardButton("Cek Bio", callback_data=f"wa_cekbio_start_{user_id}", style="primary")],
                    [InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")],
                ]
                await query.message.edit_text(
                    _screen('CEK BIO', (
                        f"{_em(CE_PROFILE,'👤')} <b>CEK BIO WHATSAPP</b>\n"
                        f"────────────────────────────\n\n"
                        f"  {_em(E1,'✅')} Worker diset: <b>{worker}</b> threads\n\n"
                        f"────────────────────────────\n"
                        f"<i>Atau langsung: /c nomor</i>"
                    ), 'Home › WhatsApp › Cek Bio'),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(kb))
        return True

    # wa_cekbio_confirm_ → user approved, start actual processing
    if data.startswith("wa_cekbio_confirm_"):
        await query.answer("Memproses...")
        phones = context.user_data.pop(f"wa_cekbio_phones_{user_id}", None)
        batch = context.user_data.pop(f"wa_cekbio_batch_{user_id}", None)
        if not phones:
            await query.message.edit_text(
                _screen('CEK BIO',
                        f"{_em(E2,'❌')} Sesi kadaluarsa. Silakan mulai lagi.",
                        'Home › WhatsApp › Cek Bio'),
                parse_mode=ParseMode.HTML)
            return True
        await _start_cekbio_process(context, query, user_id, phones, batch or 70)
        return True

    # wa_cekbio_abort_ → cancel before processing
    if data.startswith("wa_cekbio_abort_"):
        await query.answer("Dibatalkan")
        context.user_data.pop(f"wa_cekbio_phones_{user_id}", None)
        context.user_data.pop(f"wa_cekbio_batch_{user_id}", None)
        await query.message.edit_text(
            _screen('CEK BIO',
                    f"{_em(E2,'❌')} <b>Dibatalkan.</b>",
                    'Home › WhatsApp › Cek Bio'),
            parse_mode=ParseMode.HTML)
        return True

    # wa_cekbio_start_ → ask for numbers
    if data.startswith("wa_cekbio_start_"):
        await query.answer()
        context.user_data[f"wa_cekbio_waiting_{user_id}"] = True
        worker = _get_cekbio_worker(user_id)
        await query.message.edit_text(
            _screen('CEK BIO', (
                f"{_em(CE_PROFILE,'👤')} <b>CEK BIO — {worker} THREADS</b>\n"
                f"────────────────────────────\n\n"
                f"  Kirim nomor yang mau dicek:\n\n"
                f"  {_em(CE_HELP,'💡')} <b>Format:</b>\n"
                f"  • <code>6285xxx 6281xxx</code>\n"
                f"  • Satu per baris\n"
                f"  • Kirim file <code>.txt</code> / <code>.ctc</code>\n\n"
                f"────────────────────────────\n"
                f"<i>Worker: {worker} | Semua negara didukung.</i>"
            ), 'Home › WhatsApp › Cek Bio'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")],
            ]))
        return True


    # wa_checkstatus_
    if data.startswith("wa_checkstatus_"):
        await query.answer()
        status = await _api("GET", f"/status/{user_id}")
        if status.get("connected"):
            await query.message.edit_text(
                _screen('WHATSAPP', (
                    f"{_em(E1,'✅')} <b>CONNECTED!</b>\n"
                    f"────────────────────────────\n\n"
                    f"  {_em(CE_NOMOR,'📞')} <b>Nomor:</b> <code>+{status.get('waNumber','?')}</code>\n\n"
                    f"────────────────────────────\n"
                    f"{_em(CE_HELP,'💡')} <i>WhatsApp terhubung. Gunakan /cekgb.</i>"
                ), 'Home › WhatsApp'),
                parse_mode=ParseMode.HTML)
        else:
            await query.answer("Belum terhubung, tunggu pairing...", show_alert=True)
        return True

    # wa_disconnect_ — hapus akun aktif dari disk (session)
    if data.startswith("wa_disconnect_"):
        await query.answer("Memutus & menghapus session...")
        result = await _api("POST", f"/disconnect/{user_id}", {})
        remaining = result.get("remaining") or []
        if remaining and result.get("connected"):
            body = (
                f"{_em(E1,'✅')} <b>Akun aktif dihapus</b>\n"
                f"────────────────────────────\n\n"
                f"  Session lama sudah dihapus dari disk.\n"
                f"  {_em(CE_AKUN,'👥')} Beralih ke cadangan:\n"
                f"  <code>+{result.get('waNumber', '?')}</code>\n"
                f"  Sisa akun: <b>{len(remaining)}</b>"
            )
            status = await _api("GET", f"/status/{user_id}")
            _, kb = _build_wa_hub(user_id, status)
        elif remaining:
            body = (
                f"{_em(E2,'❌')} <b>Akun aktif dihapus</b>\n"
                f"────────────────────────────\n\n"
                f"  Session sudah dihapus.\n"
                f"  Ada <b>{len(remaining)}</b> akun cadangan.\n"
                f"  Buka <b>Akun WA</b> untuk pakai cadangan."
            )
            kb = [
                [InlineKeyboardButton("Akun WA", callback_data=f"wa_hub_akun_{user_id}", style="primary")],
                [InlineKeyboardButton("Pair Baru", callback_data=f"wa_hub_pair_{user_id}", style="primary")],
            ]
        else:
            body = (
                f"{_em(E2,'❌')} <b>Disconnected</b>\n"
                f"────────────────────────────\n\n"
                f"  WhatsApp terputus.\n"
                f"  Nomor & session sudah dihapus.\n"
                f"  Gunakan Pair untuk connect ulang."
            )
            kb = [
                [InlineKeyboardButton("Pair", callback_data=f"wa_hub_pair_{user_id}", style="primary")],
                [InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")],
            ]
        await query.message.edit_text(
            _screen('WHATSAPP', body, 'Home › WhatsApp'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb))
        return True

    # wa_accswitch_{uid}_{idx}
    if data.startswith("wa_accswitch_"):
        await query.answer()
        parts = data.split("_")
        try:
            idx = int(parts[-1])
        except Exception:
            return True
        keys = context.user_data.get(f"wa_acc_keys_{user_id}") or []
        if idx < 0 or idx >= len(keys):
            await query.answer("Akun tidak ditemukan", show_alert=True)
            return True
        result = await _api("POST", f"/switch/{user_id}", {"accountKey": keys[idx]})
        if result.get("error"):
            await query.answer(f"Gagal: {result['error']}", show_alert=True)
            return True
        status = await _api("GET", f"/status/{user_id}")
        body, kb = _build_wa_hub(user_id, status)
        await query.message.edit_text(
            _screen('WHATSAPP', (
                f"{_em(E1,'✅')} <b>Akun diganti</b>\n\n"
                f"{body}"
            ), 'Home › WhatsApp'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb))
        return True

    # wa_delakun_{uid}_{idx} — hapus akun spesifik
    if data.startswith("wa_delakun_"):
        await query.answer("Menghapus session...")
        parts = data.split("_")
        try:
            idx = int(parts[-1])
        except Exception:
            return True
        keys = context.user_data.get(f"wa_acc_keys_{user_id}") or []
        if idx < 0 or idx >= len(keys):
            await query.answer("Akun tidak ditemukan", show_alert=True)
            return True
        await _api("POST", f"/disconnect/{user_id}", {"accountKey": keys[idx]})
        # refresh list
        result = await _api("GET", f"/accounts/{user_id}")
        accounts = result.get("accounts") or []
        if not accounts:
            status = {"connected": False}
            body, kb = _build_wa_hub(user_id, status)
            await query.message.edit_text(
                _screen('WHATSAPP', (
                    f"{_em(E1,'✅')} Akun dihapus. Tidak ada sisa akun.\n\n{body}"
                ), 'Home › WhatsApp'),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(kb))
            return True
        context.user_data[f"wa_acc_keys_{user_id}"] = [a.get("key") for a in accounts]
        body = (
            f"{_em(E1,'✅')} <b>Akun dihapus</b>\n"
            f"────────────────────────────\n\n"
            f"  Sisa: <b>{len(accounts)}</b>\n\n"
        )
        kb = []
        for i, acc in enumerate(accounts):
            num = acc.get("waNumber") or "?"
            active = "🟢" if acc.get("active") else "⚪️"
            conn = "✅" if acc.get("connected") else "❌"
            body += f"  {active} {conn} <code>+{num}</code>\n"
            row = []
            if not acc.get("active"):
                row.append(InlineKeyboardButton(
                    f"Pakai #{i+1}", callback_data=f"wa_accswitch_{user_id}_{i}", style="primary"))
            row.append(InlineKeyboardButton(
                f"Hapus #{i+1}", callback_data=f"wa_delakun_{user_id}_{i}", style="danger"))
            if row:
                kb.append(row)
        kb.append([InlineKeyboardButton("Kembali", callback_data=f"wa_hub_back_{user_id}", style="danger")])
        await query.message.edit_text(
            _screen('AKUN WA', body, 'Home › WhatsApp › Akun'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb))
        return True

    # wa_grup_{uid}_{index}
    if data.startswith("wa_grup_"):
        await query.answer()
        parts = data.split("_")
        if len(parts) >= 4:
            idx = int(parts[3])
            groups = context.user_data.get(f"wa_groups_{user_id}", [])
            if idx < len(groups):
                g = groups[idx]
                lid_count = sum(1 for p in g.get("participants", []) if p.get("isLid"))
                body = (
                    f"{_em(CE_WHATSAPP,'💬')} <b>{g['name'][:28]}</b>\n"
                    f"────────────────────────────\n\n"
                    f"  {_em(CE_AKUN,'👥')} <b>Member:</b> {g['members']}\n"
                    f"  {_em(CE_DETAIL_ID,'🆔')} <b>ID:</b> <code>{g['id']}</code>\n"
                    f"  {_em(CE_PROFILE,'👤')} <b>LID:</b> {lid_count}\n\n"
                    f"────────────────────────────\n"
                    f"{_em(CE_HELP,'💡')} Pilih aksi:"
                )
                kb = []
                row1 = []
                if user_id == _USER_ID:
                    row1.append(InlineKeyboardButton("Citer GB", callback_data=f"wa_citer_{user_id}_{idx}", style="primary"))
                row1.append(InlineKeyboardButton("Add Member", callback_data=f"wa_addmem_{user_id}_{idx}", style="primary"))
                kb.append(row1)
                kb.append([InlineKeyboardButton("Batal", callback_data=f"wa_back_groups_{user_id}", style="danger")])
                await query.message.edit_text(
                    _screen('GRUP', body, 'Home › WhatsApp › Cek Grup › Detail'),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(kb))
        return True

    # wa_back_groups_
    if data.startswith("wa_back_groups_"):
        await query.answer()
        groups = context.user_data.get(f"wa_groups_{user_id}", [])
        if not groups:
            await query.message.delete()
            return True
        body = (
            f"{_em(CE_WHATSAPP,'💬')} <b>GRUP WHATSAPP</b>\n"
            f"────────────────────────────\n\n"
            f"  {_em(E1,'✅')} <b>Grup:</b> {len(groups)}\n\n"
            f"────────────────────────────\n"
            f"Pilih grup:"
        )
        kb = []
        for i, g in enumerate(groups[:20]):
            label = f"{g['name'][:22]} ({g['members']}m)"
            kb.append([InlineKeyboardButton(label, callback_data=f"wa_grup_{user_id}_{i}", style="primary")])
        kb.append([InlineKeyboardButton("Tutup", callback_data=f"wa_cancel_{user_id}", style="danger")])
        await query.message.edit_text(
            _screen('CEK GRUP', body, 'Home › WhatsApp › Cek Grup'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb))
        return True

    # wa_citer_{uid}_{idx} — OWNER ONLY
    if data.startswith("wa_citer_"):
        if user_id != _USER_ID:
            await query.answer("Fitur ini hanya untuk owner.", show_alert=True)
            return True
        await query.answer()
        parts = data.split("_")
        if len(parts) >= 4:
            idx = int(parts[3])
            groups = context.user_data.get(f"wa_groups_{user_id}", [])
            if idx < len(groups):
                g = groups[idx]
                gid = g["id"]
                gname = g["name"]

                result = await _api("POST", f"/citer/{user_id}/{gid}")

                if result.get("error") == "already_running":
                    await query.answer("Citer masih berjalan!", show_alert=True)
                    return True
                if result.get("error"):
                    await query.message.edit_text(
                        _screen('CITER GB', (
                            f"{_em(E2,'❌')} <b>Gagal</b>\n"
                            f"────────────────────────────\n\n"
                            f"  Error: {result['error']}"
                        ), 'Home › WhatsApp › Citer GB'),
                        parse_mode=ParseMode.HTML)
                    return True

                total = result.get("total", 500)
                total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
                sent = await query.message.edit_text(
                    _screen('CITER GB', (
                        f"{_em(CE_LOADING,'🔄')} <b>CITER GB STARTED</b>\n"
                        f"────────────────────────────\n\n"
                        f"  {_em(CE_AKUN,'👥')} <b>Grup:</b> {gname[:22]}\n"
                        f"  {_em(E6,'🚀')} <b>Total:</b> {total}x ({total_batches} batch)\n\n"
                        f"  {_progress_bar(0, total)}\n"
                        f"  0/{total} iterations\n\n"
                        f"────────────────────────────\n"
                        f"{_em(CE_HELP,'💡')} <i>Auto-refresh setiap {BATCH_SIZE} iterasi.</i>"
                    ), 'Home › WhatsApp › Citer GB'),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Stop", callback_data=f"wa_citerstop_{user_id}_{idx}", style="danger")],
                    ]))

                # Start auto-refresh background task
                asyncio.create_task(_auto_refresh_citer(
                    context, query.message.chat_id, sent.message_id, user_id, gid, gname))
        return True

    # wa_citerstop_
    if data.startswith("wa_citerstop_"):
        await query.answer("Stopping...")
        parts = data.split("_")
        if len(parts) >= 4:
            idx = int(parts[3])
            groups = context.user_data.get(f"wa_groups_{user_id}", [])
            if idx < len(groups):
                await _api("POST", f"/stop-citer/{user_id}/{groups[idx]['id']}")
        return True

    # wa_addmem_{uid}_{idx}
    if data.startswith("wa_addmem_"):
        await query.answer()
        parts = data.split("_")
        if len(parts) >= 4:
            idx = int(parts[3])
            groups = context.user_data.get(f"wa_groups_{user_id}", [])
            if idx < len(groups):
                g = groups[idx]
                context.user_data[f"wa_addmem_gid_{user_id}"] = g["id"]
                context.user_data[f"wa_addmem_gname_{user_id}"] = g["name"]
                context.user_data[f"wa_addmem_idx_{user_id}"] = idx
                context.user_data[f"wa_awaiting_members_{user_id}"] = True

                await query.message.edit_text(
                    _screen('ADD MEMBER', (
                        f"{_em(CE_ADD,'✨')} <b>ADD MEMBER</b>\n"
                        f"────────────────────────────\n\n"
                        f"  {_em(CE_AKUN,'👥')} <b>Grup:</b> {g['name'][:22]}\n\n"
                        f"  Kirim nomor yang mau di-add:\n\n"
                        f"  {_em(CE_HELP,'💡')} <b>Format:</b>\n"
                        f"  • <code>6285xxx 6281xxx</code>\n"
                        f"  • Satu per baris\n"
                        f"  • Reply/kirim file <code>.txt</code> / <code>.ctc</code>\n\n"
                        f"────────────────────────────\n"
                        f"<i>Anti-ban: wave {BATCH_SIZE}/batch + smart delay</i>"
                    ), 'Home › WhatsApp › Add Member'),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Batal", callback_data=f"wa_back_groups_{user_id}", style="danger")],
                    ]))
        return True

    # wa_addstop_
    if data.startswith("wa_addstop_"):
        await query.answer("Stopping...")
        gid = context.user_data.get(f"wa_addmem_gid_{user_id}", "")
        if gid:
            await _api("POST", f"/stop-add/{user_id}/{gid}")
        return True
    # wa_buatgrup_{uid}_{count}
    if data.startswith("wa_buatgrup_"):
        await query.answer()
        parts = data.split("_")
        if len(parts) >= 4:
            count_str = parts[3]
            if count_str == "custom":
                # Ask for custom number
                context.user_data[f"wa_buatgrup_custom_{user_id}"] = True
                await query.message.edit_text(
                    _screen('BUAT GRUP', (
                        f"{_em(CE_ADD,'✨')} <b>CUSTOM JUMLAH</b>\n"
                        f"────────────────────────────\n\n"
                        f"  Ketik jumlah grup yang mau dibuat:\n\n"
                        f"  <i>Contoh: 10</i>"
                    ), 'Home › WhatsApp › Buat Grup'),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")],
                    ]))
            else:
                count = int(count_str)
                context.user_data[f"wa_buatgrup_waiting_{user_id}"] = True
                context.user_data[f"wa_buatgrup_count_{user_id}"] = count
                await query.message.edit_text(
                    _screen('BUAT GRUP', (
                        f"{_em(CE_ADD,'✨')} <b>BUAT {count} GRUP</b>\n"
                        f"────────────────────────────\n\n"
                        f"  Kirim nomor member:\n\n"
                        f"  {_em(CE_HELP,'💡')} <b>Format:</b>\n"
                        f"  • <code>6285xxx 6281xxx</code>\n"
                        f"  • Satu per baris\n"
                        f"  • Kirim file <code>.txt</code> / <code>.ctc</code>\n\n"
                        f"────────────────────────────\n"
                        f"<i>Nama: DikZz -{{random}}\nMember otomatis jadi admin.</i>"
                    ), 'Home › WhatsApp › Buat Grup'),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")],
                    ]))
        return True

    return False


# ═══════════════════════════════════════
#  ADD MEMBER INPUT HANDLER
# ═══════════════════════════════════════
async def handle_addmember_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle phone numbers input for add member."""
    user_id = update.effective_user.id
    if not context.user_data.get(f"wa_awaiting_members_{user_id}"):
        return False

    gid = context.user_data.get(f"wa_addmem_gid_{user_id}")
    gname = context.user_data.get(f"wa_addmem_gname_{user_id}", "?")
    if not gid:
        return False

    # Parse phones from text or file
    phones = []

    if update.message.document:
        fname = update.message.document.file_name or ""
        if fname.endswith(".txt") or fname.endswith(".ctc"):
            file = await context.bot.get_file(update.message.document.file_id)
            raw = (await file.download_as_bytearray()).decode('utf-8', errors='ignore')
            phones = [l.strip() for l in raw.splitlines() if re.match(r'^\+?[\d\s\-]{8,}$', l.strip())]

    if not phones:
        reply = update.message.reply_to_message
        if reply and reply.document:
            fname = reply.document.file_name or ""
            if fname.endswith(".txt") or fname.endswith(".ctc"):
                file = await context.bot.get_file(reply.document.file_id)
                raw = (await file.download_as_bytearray()).decode('utf-8', errors='ignore')
                phones = [l.strip() for l in raw.splitlines() if re.match(r'^\+?[\d\s\-]{8,}$', l.strip())]

    if not phones:
        text = update.message.text or ""
        tokens = re.split(r'[\s,;\n]+', text)
        phones = [t for t in tokens if re.match(r'^\+?[\d]{8,}$', t)]

    if not phones:
        await update.message.reply_text(
            f"{_em(E2,'❌')} Tidak ada nomor valid ditemukan.",
            parse_mode=ParseMode.HTML)
        return True

    context.user_data.pop(f"wa_awaiting_members_{user_id}", None)

    total_batches = (len(phones) + BATCH_SIZE - 1) // BATCH_SIZE
    msg = await update.message.reply_text(
        _screen('ADD MEMBER', (
            f"{_em(CE_ADD,'✨')} <b>STARTING ADD MEMBER</b>\n"
            f"────────────────────────────\n\n"
            f"  {_em(CE_AKUN,'👥')} <b>Grup:</b> {gname[:22]}\n"
            f"  {_em(CE_NOMOR,'📞')} <b>Input:</b> {len(phones)} nomor\n"
            f"  {_em(E6,'🚀')} <b>Batches:</b> ~{total_batches}\n\n"
            f"  {_em(CE_LOADING,'🔄')} <i>Validating + strengthening sender...</i>\n\n"
            f"────────────────────────────\n"
            f"{_em(CE_HELP,'💡')} <i>Anti-ban: {BATCH_SIZE}/wave + smart delay</i>"
        ), 'Home › WhatsApp › Add Member'),
        parse_mode=ParseMode.HTML)

    result = await _api("POST", f"/addmember/{user_id}/{gid}", {"phones": phones})

    if result.get("error"):
        await msg.edit_text(
            _screen('ADD MEMBER', (
                f"{_em(E2,'❌')} <b>Gagal</b>\n"
                f"────────────────────────────\n\n"
                f"  Error: {result['error']}"
            ), 'Home › WhatsApp › Add Member'),
            parse_mode=ParseMode.HTML)
        return True

    valid = result.get("valid", 0)
    total = result.get("total", 0)
    skipped = result.get("skipped", 0)

    await msg.edit_text(
        _screen('ADD MEMBER', (
            f"{_em(CE_ADD,'✨')} <b>ADD MEMBER RUNNING</b>\n"
            f"────────────────────────────\n\n"
            f"  {_em(CE_AKUN,'👥')} <b>Grup:</b> {gname[:22]}\n"
            f"  {_em(CE_NOMOR,'📞')} Input: {len(phones)} | Valid: {valid}\n"
            f"  Skipped: {skipped} | To Add: {total}\n\n"
            f"  {_progress_bar(0, total)}\n\n"
            f"────────────────────────────\n"
            f"{_em(CE_HELP,'💡')} <i>Auto-refresh setiap {BATCH_SIZE} nomor.</i>"
        ), 'Home › WhatsApp › Add Member'),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Stop", callback_data=f"wa_addstop_{user_id}", style="danger")],
        ]))

    # Start auto-refresh background task
    asyncio.create_task(_auto_refresh_addmember(
        context, update.effective_chat.id, msg.message_id, user_id, gid, gname))

    return True


# ═══════════════════════════════════════
#  /buatgrup COMMAND
# ═══════════════════════════════════════
async def buatgrup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /buatgrup — create new group with members (avoids suspension)."""
    user_id = update.effective_user.id
    if not _is_authorized(user_id):
        return

    status = await _api("GET", f"/status/{user_id}")
    if not status.get("connected"):
        await update.message.reply_text(
            _screen('BUAT GRUP', (
                f"{_em(E2,'❌')} <b>WhatsApp belum terhubung!</b>\n"
                f"────────────────────────────\n\n"
                f"  Gunakan <code>/pair nomor</code> untuk connect dulu."
            ), 'Home › WhatsApp › Buat Grup'),
            parse_mode=ParseMode.HTML)
        return

    # Show how many groups to create
    kb = [
        [InlineKeyboardButton("1 Grup", callback_data=f"wa_buatgrup_{user_id}_1", style="primary"),
         InlineKeyboardButton("2 Grup", callback_data=f"wa_buatgrup_{user_id}_2", style="primary"),
         InlineKeyboardButton("3 Grup", callback_data=f"wa_buatgrup_{user_id}_3", style="primary")],
        [InlineKeyboardButton("4 Grup", callback_data=f"wa_buatgrup_{user_id}_4", style="primary"),
         InlineKeyboardButton("5 Grup", callback_data=f"wa_buatgrup_{user_id}_5", style="primary"),
         InlineKeyboardButton("Custom", callback_data=f"wa_buatgrup_{user_id}_custom", style="primary")],
        [InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")],
    ]
    await update.message.reply_text(
        _screen('BUAT GRUP', (
            f"{_em(CE_ADD,'✨')} <b>BUAT GRUP BARU</b>\n"
            f"────────────────────────────\n\n"
            f"  Buat grup baru dengan member langsung\n"
            f"  di dalamnya (anti suspend).\n\n"
            f"  {_em(CE_HELP,'💡')} <b>Kenapa ini lebih aman?</b>\n"
            f"  WhatsApp gak suspend grup yang\n"
            f"  DIBUAT dengan member dari awal.\n\n"
            f"────────────────────────────\n"
            f"Pilih jumlah grup:"
        ), 'Home › WhatsApp › Buat Grup'),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb))


async def handle_buatgrup_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom count input OR phone numbers input for buat grup."""
    user_id = update.effective_user.id

    # ── Step 1: Handle custom count input ──
    if context.user_data.get(f"wa_buatgrup_custom_{user_id}"):
        text = (update.message.text or "").strip()
        if not text.isdigit() or int(text) < 1 or int(text) > 50:
            await update.message.reply_text(
                f"{_em(E2,'❌')} Masukkan angka 1-50.", parse_mode=ParseMode.HTML)
            return True
        count = int(text)
        context.user_data.pop(f"wa_buatgrup_custom_{user_id}", None)
        context.user_data[f"wa_buatgrup_waiting_{user_id}"] = True
        context.user_data[f"wa_buatgrup_count_{user_id}"] = count
        await update.message.reply_text(
            _screen('BUAT GRUP', (
                f"{_em(CE_ADD,'✨')} <b>BUAT {count} GRUP</b>\n"
                f"────────────────────────────\n\n"
                f"  Kirim nomor member:\n\n"
                f"  {_em(CE_HELP,'💡')} <b>Format:</b>\n"
                f"  • <code>6285xxx 6281xxx</code>\n"
                f"  • Satu per baris\n"
                f"  • Kirim file <code>.txt</code> / <code>.ctc</code>\n\n"
                f"────────────────────────────\n"
                f"Semua nomor akan dimasukkan ke {count} grup baru.\n"
                f"<i>Nama: DikZz -{{random}}\nMember otomatis jadi admin.</i>"
            ), 'Home › WhatsApp › Buat Grup'),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Batal", callback_data=f"wa_cancel_{user_id}", style="danger")],
            ]))
        return True

    # ── Step 2: Handle phone numbers input ──
    if not context.user_data.get(f"wa_buatgrup_waiting_{user_id}"):
        return False

    count = context.user_data.get(f"wa_buatgrup_count_{user_id}", 1)

    # Parse phones
    phones = []
    if update.message.document:
        fname = update.message.document.file_name or ""
        if fname.endswith(".txt") or fname.endswith(".ctc"):
            file = await context.bot.get_file(update.message.document.file_id)
            raw = (await file.download_as_bytearray()).decode('utf-8', errors='ignore')
            phones = [l.strip() for l in raw.splitlines() if re.match(r'^\+?[\d\s\-]{8,}$', l.strip())]

    if not phones:
        text = update.message.text or ""
        tokens = re.split(r'[\s,;\n]+', text)
        phones = [t for t in tokens if re.match(r'^\+?[\d]{8,}$', t)]

    if not phones:
        await update.message.reply_text(
            f"{_em(E2,'❌')} Tidak ada nomor valid.", parse_mode=ParseMode.HTML)
        return True

    context.user_data.pop(f"wa_buatgrup_waiting_{user_id}", None)
    context.user_data.pop(f"wa_buatgrup_count_{user_id}", None)

    msg = await update.message.reply_text(
        _screen('BUAT GRUP', (
            f"{_em(CE_ADD,'✨')} <b>CREATING {count} GROUP(S)...</b>\n"
            f"────────────────────────────\n\n"
            f"  {_em(CE_NOMOR,'📞')} Members: {len(phones)}\n"
            f"  {_em(CE_AKUN,'👥')} Groups: {count}\n\n"
            f"  {_em(CE_LOADING,'🔄')} <i>Membuat grup + proteksi...</i>"
        ), 'Home › WhatsApp › Buat Grup'),
        parse_mode=ParseMode.HTML)

    import random
    results = []

    for i in range(count):
        rand_id = random.randint(1000, 9999)
        name = f"DikZz -{rand_id}"
        result = await _api("POST", f"/creategroup/{user_id}", {"name": name, "phones": phones})
        results.append(result)

        if result.get("error") and "blocked" in str(result.get("error", "")):
            # Account restricted, stop immediately
            break

        if i < count - 1:
            delay = random.randint(60, 120)
            try:
                await msg.edit_text(
                    _screen('BUAT GRUP', (
                        f"{_em(CE_LOADING,'🔄')} <b>CREATING GROUPS...</b>\n"
                        f"────────────────────────────\n\n"
                        f"  {_progress_bar(i+1, count)}\n\n"
                        f"  {_em(E1,'✅')} Created: {i+1}/{count}\n"
                        f"  {_em(CE_LOADING,'🔄')} Next in {delay}s...\n\n"
                        f"────────────────────────────\n"
                        f"{_em(CE_HELP,'💡')} <i>Anti-ban: delay random per grup.</i>"
                    ), 'Home › WhatsApp › Buat Grup'),
                    parse_mode=ParseMode.HTML)
            except: pass
            await asyncio.sleep(delay)

    # Show results
    success_count = sum(1 for r in results if r.get("success"))
    body = (
        f"{_em(E1,'✅')} <b>GRUP DIBUAT</b>\n"
        f"────────────────────────────\n\n"
        f"  {_em(E1,'✅')} Berhasil: {success_count}/{count}\n"
        f"  {_em(CE_NOMOR,'📞')} Members: {len(phones)}\n\n"
    )
    for i, r in enumerate(results):
        if r.get("success"):
            body += f"  {i+1}. {_em(E1,'✅')} {r.get('subject','?')} ({r.get('participants',0)} member)\n"
        else:
            body += f"  {i+1}. {_em(E2,'❌')} Error: {r.get('error','?')[:30]}\n"

    body += (
        f"\n────────────────────────────\n"
        f"{_em(CE_HELP,'💡')} <i>Grup dibuat + auto protect (locked + approval).\nMember otomatis jadi admin.</i>"
    )

    await msg.edit_text(
        _screen('BUAT GRUP', body, 'Home › WhatsApp › Buat Grup'),
        parse_mode=ParseMode.HTML)
    return True


# ═══════════════════════════════════════
#  CEK BIO INPUT HANDLER
# ═══════════════════════════════════════
async def handle_cekbio_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle cekbio custom batch or phone numbers input."""
    user_id = update.effective_user.id

    # Step 1: Custom batch input (from Setting > Custom)
    if context.user_data.get(f"wa_cekbio_custom_batch_{user_id}"):
        text = (update.message.text or "").strip()
        if not text.isdigit() or int(text) < 1 or int(text) > 200:
            await update.message.reply_text(
                f"{_em(E2,'❌')} Masukkan angka 1-200.", parse_mode=ParseMode.HTML)
            return True
        batch = int(text)
        context.user_data.pop(f"wa_cekbio_custom_batch_{user_id}", None)
        _set_cekbio_worker(user_id, batch)
        await update.message.reply_text(
            f"{_em(E1,'✅')} Worker diset ke <b>{batch}</b> threads.",
            parse_mode=ParseMode.HTML)
        return True

    # Step 2: Phone numbers input
    if not context.user_data.get(f"wa_cekbio_waiting_{user_id}"):
        return False

    batch = _get_cekbio_worker(user_id)

    # Parse phones
    phones = []
    # Check document: either in this message or in the replied message
    doc = update.message.document
    if not doc and update.message.reply_to_message:
        doc = update.message.reply_to_message.document
    if doc:
        fname = doc.file_name or ""
        if fname.endswith(".txt") or fname.endswith(".ctc"):
            file = await context.bot.get_file(doc.file_id)
            raw = (await file.download_as_bytearray()).decode('utf-8', errors='ignore')
            # Extract phone numbers from any line format (handles result files with mixed text)
            for line in raw.splitlines():
                # Find all sequences of 6+ digits (with optional + prefix)
                found = re.findall(r'\+?(\d{6,15})', line)
                for num in found:
                    # Skip numbers that look like dates/timestamps (4-digit year patterns)
                    if len(num) >= 8:
                        phones.append(num)
            # Deduplicate (result files list same numbers in multiple sections)
            phones = list(dict.fromkeys(phones))

    if not phones:
        text = update.message.text or ""
        tokens = re.split(r'[\s,;\n]+', text)
        phones = [t for t in tokens if re.match(r'^\+?[\d]{8,}$', t)]

    if not phones:
        await update.message.reply_text(
            f"{_em(E2,'❌')} Tidak ada nomor valid.", parse_mode=ParseMode.HTML)
        return True

    # Store phones and switch to confirmation state (don't process yet)
    context.user_data.pop(f"wa_cekbio_waiting_{user_id}", None)
    context.user_data[f"wa_cekbio_phones_{user_id}"] = phones
    context.user_data[f"wa_cekbio_batch_{user_id}"] = batch

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"Proses ({len(phones)})",
            callback_data=f"wa_cekbio_confirm_{user_id}", style="success"),
        InlineKeyboardButton("Batal",
            callback_data=f"wa_cekbio_abort_{user_id}", style="danger"),
    ]])

    await update.message.reply_text(
        _screen('CEK BIO', (
            f"{_em(CE_PROFILE,'👤')} <b>KONFIRMASI CEK BIO</b>\n"
            f"────────────────────────────\n\n"
            f"  {_em(CE_NOMOR,'📞')} Total nomor: <b>{len(phones)}</b>\n"
            f"  {_em(E6,'🚀')} Worker: <b>{batch}</b> threads\n\n"
            f"────────────────────────────\n"
            f"{_em(CE_HELP,'💡')} <i>Lanjut proses?</i>"
        ), 'Home › WhatsApp › Cek Bio'),
        parse_mode=ParseMode.HTML,
        reply_markup=kb)
    return True


async def _start_cekbio_process(context, query, user_id, phones, batch):
    """Actually kick off /cekbio backend + monitor after user confirms."""
    chat_id = query.message.chat_id
    await query.message.edit_text(
        _screen('CEK BIO', (
            f"{_em(CE_LOADING,'🔄')} <b>CHECKING {len(phones)} NOMOR...</b>\n"
            f"────────────────────────────\n\n"
            f"  {_em(CE_NOMOR,'📞')} Total: {len(phones)}\n"
            f"  {_em(E6,'🚀')} Worker: {batch} threads\n\n"
            f"  {_progress_bar(0, len(phones))}\n\n"
            f"────────────────────────────\n"
            f"{_em(CE_HELP,'💡')} <i>Processing...</i>"
        ), 'Home › WhatsApp › Cek Bio'),
        parse_mode=ParseMode.HTML)

    result = await _api("POST", f"/cekbio/{user_id}", {"phones": phones, "batch": batch})

    if result.get("error"):
        await query.message.edit_text(
            _screen('CEK BIO', (
                f"{_em(E2,'❌')} <b>Gagal</b>\n"
                f"────────────────────────────\n\n"
                f"  Error: {result['error']}"
            ), 'Home › WhatsApp › Cek Bio'),
            parse_mode=ParseMode.HTML)
        return

    asyncio.create_task(_cekbio_monitor(
        context, chat_id, query.message.message_id, user_id, len(phones), batch))


async def _cekbio_monitor(context, chat_id, message_id, user_id, total, batch):
    """Background: monitor cekbio progress and send results when done."""
    import io, tempfile, os
    bot = context.bot
    last_pct = -1
    _cekbio_cancelled.discard(user_id)
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data=f"cekbio_cancel_{user_id}", style="danger")]])

    while True:
        await asyncio.sleep(3)
        if user_id in _cekbio_cancelled:
            break
        status = await _api("GET", f"/cekbio-status/{user_id}")
        if not status or not status.get("running", False):
            break
        progress = status.get("progress", 0)
        pct = int((progress / total) * 100) if total > 0 else 0
        if pct > last_pct + 4:
            last_pct = pct
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text=_screen('CEK BIO', (
                        f"{_em(CE_LOADING,'🔄')} <b>CHECKING...</b>\n"
                        f"────────────────────────────\n\n"
                        f"  {_progress_bar(progress, total)}\n\n"
                        f"  {_em(E1,'✅')} Checked: {progress}/{total}\n"
                        f"  {_em(E6,'🚀')} Worker: {batch} threads\n\n"
                        f"────────────────────────────"
                    ), 'Home › WhatsApp › Cek Bio'),
                    parse_mode=ParseMode.HTML,
                    reply_markup=cancel_kb)
            except: pass

    # Get final results (partial or full)
    status = await _api("GET", f"/cekbio-status/{user_id}")
    results = status.get("results", [])
    if not results:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=_screen('CEK BIO', (
                    f"{_em(E2,'❌')} Tidak ada hasil."
                ), 'Home › WhatsApp › Cek Bio'),
                parse_mode=ParseMode.HTML)
        except: pass
        return

    # Compute stats
    terdaftar = sum(1 for r in results if r.get("exists"))
    tidak = len(results) - terdaftar
    with_bio = sum(1 for r in results if r.get("bio"))
    bisnis = [r for r in results if r.get("type") == "wa_bisnis"]
    eklusif = sum(1 for r in bisnis if r.get("tier") == "eklusif")
    standart = sum(1 for r in bisnis if r.get("tier") == "standart")
    low = sum(1 for r in bisnis if r.get("tier") == "low")
    suite = sum(1 for r in bisnis if r.get("tier") == "suite")
    wa_biasa = sum(1 for r in results if r.get("type") == "wa_biasa")
    ai_agent = sum(1 for r in results if r.get("hasAiAgent"))

    summary = (
        f"{_em(CE_PROFILE,'👤')} <b>HASIL CEK BIO</b>\n"
        f"────────────────────────────\n\n"
        f"  {_em(CE_NOMOR,'📞')} Total Dicek: <b>{len(results)}</b>\n"
        f"  {_em(E1,'✅')} Terdaftar: <b>{terdaftar}</b>\n"
        f"  {_em(E2,'❌')} Tidak Terdaftar: <b>{tidak}</b>\n\n"
        f"  {_em(CE_PROFILE,'👤')} Punya Bio: <b>{with_bio}</b>\n"
        f"  {_em(CE_AKUN,'👥')} WA Biasa: <b>{wa_biasa}</b>\n"
        f"  {_em(CE_DETAIL_NAME,'🏢')} Business: <b>{len(bisnis)}</b>\n"
        f"  {_em('5870994129244131212','🤖')} AI Agent: <b>{ai_agent}</b>\n"
        f"  Eklusif: <b>{eklusif}</b> | Standart: <b>{standart}</b>\n"
        f"  Low: <b>{low}</b> | Suite: <b>{suite}</b>\n\n"
        f"────────────────────────────\n"
    )

    # If < 5 results, show inline
    if len(results) <= 5:
        detail = ""
        for i, r in enumerate(results):
            idx = i + 1
            if not r.get("exists"):
                detail += f"  {_em(E2,'❌')} [{idx}] <code>{r['number']}</code>\n"
                detail += f"  <i>Tidak Terdaftar</i>\n"
            elif r.get("type") == "wa_bisnis":
                tier = (r.get("tier") or "?").upper()
                if r.get("isSuite") and tier != "SUITE":
                    tier = f"{tier} + SUITE"
                name = r.get("name") or "-"
                desc = (r.get("description") or "-")[:80]
                cat = r.get("category") or "-"
                email = r.get("email") or "-"
                tz = r.get("timezone") or "-"
                catalog = r.get("catalogCount") or 0
                catalog_str = str(catalog) if catalog > 0 else "Tidak ada katalog"
                cover = "Ya" if r.get("cover") else "-"
                since = r.get("bizSince") or "-"
                bio = (r.get("bio") or "-")[:60]
                bio_set = r.get("bioSetAt") or "-"
                detail += f"  {_em(E1,'✅')} [{idx}] <code>{r['number']}</code> — <b>{tier}</b>\n"
                ai_tag = " 🤖 AI Agent" if r.get("hasAiAgent") else ""
                bq = (
                    f"├ Nama: {name}\n"
                    f"├ Bio: {bio}\n"
                    f"├ Set: {bio_set}\n"
                    f"├ Kategori: {cat}\n"
                    f"├ Deskripsi: {desc}\n"
                    f"├ Timezone: {tz}\n"
                    f"├ Since: {since}\n"
                    f"├ Katalog: {catalog_str}\n"
                    f"├ Email: {email}\n"
                    f"├ Cover: {cover}\n"
                    f"└ AI Agent: {'Aktif' + ai_tag if r.get('hasAiAgent') else 'Tidak'}"
                )
                detail += f"  <blockquote>{bq}</blockquote>\n"
            else:
                detail += f"  {_em(E1,'✅')} [{idx}] <code>{r['number']}</code>\n"
        summary += detail
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=_screen('CEK BIO', summary, 'Home › WhatsApp › Cek Bio'),
                parse_mode=ParseMode.HTML)
        except: pass
    else:
        # Send as .txt file — Opsi A aesthetic
        from datetime import datetime as _dt
        _now = _dt.now().strftime("%d/%m/%Y %H:%M")
        lines = [
            "HASIL LASO MU SAYANG",
            f"{_now}",
            "",
            "· · · · · · · · · · · · · · · · · · · · ·",
            "",
            "RINGKASAN",
            "",
            f"  Terdaftar ............. {terdaftar}",
            f"  Tidak Terdaftar ....... {tidak}",
            f"  Bio ................... {with_bio}",
            f"  Business .............. {len(bisnis)}",
            f"     Exclusive .......... {eklusif}",
            f"     Suite .............. {suite}",
            f"     Standard ........... {standart}",
            f"     Low ................ {low}",
            f"  AI Agent .............. {ai_agent}",
            f"  WA Biasa .............. {wa_biasa}",
            "",
            "· · · · · · · · · · · · · · · · · · · · ·",
        ]

        # Tier list for section headers
        def _tier_label(r):
            t = (r.get("tier") or "?").capitalize()
            if r.get("isSuite") and t.lower() != "suite":
                t += " + Suite"
            return f"{t} Meta Business"

        # Section: Business list (compact, sorted by date)
        if bisnis:
            lines.append("")
            lines.append("")
            lines.append(f"LOW META BUSINESS ({len(bisnis)})")
            lines.append("")
            for i, r in enumerate(bisnis):
                since_short = (r.get("bizSince") or "-").replace("Joined in ", "")
                prefix = "├" if i < len(bisnis) - 1 else "└"
                lines.append(f"{prefix} +{r['number']} ({(r.get('tier') or '?').capitalize()}) ......... {since_short}")

        # Section: Business with Bio (detail)
        biz_with_bio = [r for r in bisnis if r.get("bio")]
        if biz_with_bio:
            lines.append("")
            lines.append("")
            lines.append(f"WA BISNIS + BIO ({len(biz_with_bio)})")
            lines.append("")
            for i, r in enumerate(biz_with_bio, 1):
                catalog = r.get("catalogCount") or 0
                catalog_str = f"{catalog} produk" if catalog > 0 else "-"
                is_last = (i == len(biz_with_bio))
                branch = "└" if is_last else "├"
                cont = " " if is_last else "│"
                lines.append(f"{branch}── [{i}] +{r['number']}")
                lines.append(f"{cont}      {_tier_label(r)}")
                if r.get("name"):
                    lines.append(f"{cont}      ├ Nama       {r.get('name')}")
                lines.append(f"{cont}      ├ Bio        {(r.get('bio') or '-')[:80]}")
                lines.append(f"{cont}      ├ Set        {r.get('bioSetAt') or '-'}")
                if r.get("description"):
                    lines.append(f"{cont}      ├ Desc       {(r.get('description') or '-')[:80]}")
                if r.get("category"):
                    lines.append(f"{cont}      ├ Category   {r.get('category')}")
                if r.get("timezone"):
                    lines.append(f"{cont}      ├ Timezone   {r.get('timezone')}")
                lines.append(f"{cont}      ├ Since      {(r.get('bizSince') or '-').replace('Joined in ', '')}")
                lines.append(f"{cont}      ├ Katalog    {catalog_str}")
                if r.get("email"):
                    lines.append(f"{cont}      ├ Email      {r.get('email')}")
                lines.append(f"{cont}      ├ Cover      {'Ya' if r.get('cover') else '-'}")
                lines.append(f"{cont}      └ AI Agent   {'aktif' if r.get('hasAiAgent') else '-'}")
                lines.append(f"{cont}")

        # Section: Business without Bio (detail)
        biz_no_bio = [r for r in bisnis if not r.get("bio")]
        if biz_no_bio:
            lines.append("")
            lines.append("")
            lines.append(f"WA BISNIS TANPA BIO ({len(biz_no_bio)})")
            lines.append("")
            for i, r in enumerate(biz_no_bio, 1):
                catalog = r.get("catalogCount") or 0
                catalog_str = f"{catalog} produk" if catalog > 0 else "-"
                is_last = (i == len(biz_no_bio))
                branch = "└" if is_last else "├"
                cont = " " if is_last else "│"
                lines.append(f"{branch}── [{i}] +{r['number']}")
                lines.append(f"{cont}      {_tier_label(r)}")
                if r.get("name"):
                    lines.append(f"{cont}      ├ Nama       {r.get('name')}")
                if r.get("description"):
                    lines.append(f"{cont}      ├ Desc       {(r.get('description') or '-')[:80]}")
                if r.get("category"):
                    lines.append(f"{cont}      ├ Category   {r.get('category')}")
                if r.get("timezone"):
                    lines.append(f"{cont}      ├ Timezone   {r.get('timezone')}")
                lines.append(f"{cont}      ├ Since      {(r.get('bizSince') or '-').replace('Joined in ', '')}")
                lines.append(f"{cont}      ├ Katalog    {catalog_str}")
                if r.get("email"):
                    lines.append(f"{cont}      ├ Email      {r.get('email')}")
                lines.append(f"{cont}      ├ Cover      {'Ya' if r.get('cover') else '-'}")
                lines.append(f"{cont}      └ AI Agent   {'aktif' if r.get('hasAiAgent') else '-'}")
                lines.append(f"{cont}")

        # Section: WA Biasa (Ori)
        ori = [r for r in results if r.get("exists") and r.get("type") != "wa_bisnis"]
        if ori:
            lines.append("")
            lines.append("")
            lines.append(f"TERDAFTAR ORI ({len(ori)})")
            lines.append("")
            for i, r in enumerate(ori):
                prefix = "├" if i < len(ori) - 1 else "└"
                lines.append(f"{prefix} +{r['number']}")

        # Section: Tidak Terdaftar
        tidak_list = [r for r in results if not r.get("exists")]
        if tidak_list:
            lines.append("")
            lines.append("")
            lines.append(f"TIDAK TERDAFTAR ({len(tidak_list)})")
            lines.append("")
            for i, r in enumerate(tidak_list):
                prefix = "├" if i < len(tidak_list) - 1 else "└"
                lines.append(f"{prefix} +{r['number']}")

        lines.append("")
        lines.append("")
        lines.append("Powered by DikZz")

        content = "\n".join(lines)
        buf = io.BytesIO(content.encode("utf-8"))
        buf.name = f"cekbio_{len(results)}.txt"

        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except: pass

        try:
            await bot.send_document(
                chat_id=chat_id, document=buf,
                caption=_screen('CEK BIO', summary + f"{_em(CE_HELP,'💡')} <i>File hasil dikirim.</i>",
                             'Home › WhatsApp › Cek Bio'),
                parse_mode=ParseMode.HTML)
        except: pass


def is_wa_pairing(context, user_id: int) -> bool:
    """Check if user is in WA pairing flow."""
    return context.user_data.get(f"wa_pairing_{user_id}", False)


def is_wa_adding(context, user_id: int) -> bool:
    """Check if user is awaiting add member, buat grup, or cekbio input."""
    return (context.user_data.get(f"wa_awaiting_members_{user_id}", False) or
            context.user_data.get(f"wa_buatgrup_waiting_{user_id}", False) or
            context.user_data.get(f"wa_buatgrup_custom_{user_id}", False) or
            context.user_data.get(f"wa_cekbio_waiting_{user_id}", False) or
            context.user_data.get(f"wa_cekbio_custom_batch_{user_id}", False))
