"""
Gmail Server 1 — Auto Create Gmail Account
============================================
Module for dik.py bot integration.
Uses Playwright with real iPhone device UA to create Gmail accounts.
15 parallel workers. Progress callback for live updates.

Flow:
1. User sends /gmail (nomor) or reply .txt
2. Bot opens signup, fills name/birthday/username/password
3. Bot sends SMS verification  
4. User replies with OTP: (nomor):(kode)
5. Bot verifies OTP → account created
6. Bot returns email/password/name
"""
import asyncio
import random
import string
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable
from pathlib import Path

log = logging.getLogger("gmail_s1")

# ============ CONSTANTS ============
GMAIL_WORKERS = 15
SIGNUP_URL = "https://accounts.google.com/signup/v2/createaccount?flowName=GlifWebSignIn&flowEntry=SignUp"
DEFAULT_FIRST_NAME = "DikZz"
DEFAULT_LAST_NAME = "Tmvn"
DEFAULT_PASSWORD = "Dika2007@"

# Custom emoji IDs
CE_GMAIL = "5292170346063995499"    # ✉️ gmail
CE_PASSWORD = "5256248974767046755"  # 🔒 password

# ============ Real iPhone User Agents ============
IPHONE_MODELS = [
    ("iPhone 15 Pro Max", "iPhone16,2", "18_0", "18.0"),
    ("iPhone 15 Pro", "iPhone16,1", "18_0", "18.0"),
    ("iPhone 15 Plus", "iPhone15,5", "17_4", "17.4"),
    ("iPhone 15", "iPhone15,4", "17_4", "17.4"),
    ("iPhone 14 Pro Max", "iPhone15,3", "17_3", "17.3"),
    ("iPhone 14 Pro", "iPhone15,2", "17_2", "17.2"),
    ("iPhone 14", "iPhone14,7", "17_1", "17.1"),
    ("iPhone 14 Plus", "iPhone14,8", "17_1", "17.1"),
    ("iPhone 13 Pro Max", "iPhone14,3", "17_0", "17.0"),
    ("iPhone 13 Pro", "iPhone14,2", "17_0", "17.0"),
    ("iPhone 13", "iPhone14,5", "16_6", "16.6"),
    ("iPhone SE 3rd", "iPhone14,6", "17_2", "17.2"),
]


def random_iphone_ua() -> tuple:
    """Returns (device_name, user_agent_string)."""
    name, model, ios, safari = random.choice(IPHONE_MODELS)
    ua = (
        f"Mozilla/5.0 (iPhone; CPU iPhone OS {ios} like Mac OS X) "
        f"AppleWebKit/605.1.15 (KHTML, like Gecko) "
        f"Version/{safari} Mobile/15E148 Safari/604.1"
    )
    return name, ua


# ============ Data Classes ============
@dataclass
class GmailConfig:
    """Per-user Gmail settings."""
    first_name: str = DEFAULT_FIRST_NAME
    last_name: str = DEFAULT_LAST_NAME
    password: str = DEFAULT_PASSWORD
    birthday: str = "2000-08-08"
    gender: int = 1  # 1=Male, 2=Female


@dataclass
class GmailResult:
    """Result of a Gmail creation attempt."""
    phone: str
    status: str = "pending"
    email: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    device: Optional[str] = None
    error: Optional[str] = None
    time_taken: float = 0.0
    step: str = ""
    response_json: Optional[dict] = None  # key responses from Google
    google_number: Optional[str] = None   # number user must send SMS TO
    verify_code: Optional[str] = None      # SMS text user must send


# Progress callback type
ProgressCallback = Optional[Callable[[str, str, str], Awaitable[None]]]
# callback(phone, step_name, status_emoji)


def generate_username(first_name: str) -> str:
    """Generate random Gmail username."""
    base = first_name.lower().replace(" ", "")
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(4, 7)))
    return f"{base}{suffix}"


def validate_password(pw: str) -> bool:
    """Validate: 11+ chars, uppercase, digit, special (!@#$%)."""
    if len(pw) < 11:
        return False
    if not re.search(r'[A-Z]', pw):
        return False
    if not re.search(r'[0-9]', pw):
        return False
    if not re.search(r'[!@#$%]', pw):
        return False
    return True


def generate_strong_password(length: int = 12) -> str:
    """Generate password meeting requirements."""
    upper = random.choices(string.ascii_uppercase, k=2)
    lower = random.choices(string.ascii_lowercase, k=4)
    digits = random.choices(string.digits, k=3)
    special = random.choices("!@#$%", k=2)
    extra = random.choices(string.ascii_letters + string.digits, k=max(0, length - 11))
    all_chars = upper + lower + digits + special + extra
    random.shuffle(all_chars)
    return ''.join(all_chars)


class GmailCreator:
    """Creates Gmail accounts via Playwright + iPhone UA. 15 workers."""

    def __init__(self, config: GmailConfig = None):
        self.config = config or GmailConfig()

    async def create_account(self, phone: str, country_code: str = "id",
                             otp_future: asyncio.Future = None,
                             on_progress: ProgressCallback = None) -> GmailResult:
        """
        Full Gmail creation flow with live progress updates.
        """
        start = time.time()
        from playwright.async_api import async_playwright, TimeoutError as PwTimeout

        device_name, ua = random_iphone_ua()
        username = generate_username(self.config.first_name)
        email = f"{username}@gmail.com"
        password = self.config.password

        result = GmailResult(
            phone=phone, email=email, username=username,
            password=password, first_name=self.config.first_name,
            last_name=self.config.last_name, device=device_name,
        )

        async def progress(step: str, emoji: str = "🟠"):
            result.step = step
            if on_progress:
                try:
                    await on_progress(phone, step, emoji, email)
                except:
                    pass

        await progress("Memulai registrasi...", "🚀")

        # Parse birthday
        parts = self.config.birthday.split("-")
        yr = parts[0] if len(parts) == 3 else "2000"
        mo = parts[1].lstrip("0") if len(parts) == 3 else "8"
        dy = parts[2].lstrip("0") if len(parts) == 3 else "8"

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                       "--disable-dev-shm-usage"],
            )
            ctx = await browser.new_context(
                user_agent=ua, viewport={"width": 390, "height": 844}, locale="en-GB",
            )
            page = await ctx.new_page()

            # Capture device verification details (Google number + SMS code)
            _captured = {"google_number": None, "verify_code": None}

            async def _on_response(response):
                try:
                    rurl = response.url
                    if "accounts.google" in rurl and "batchexecute" in rurl:
                        body = await response.text()
                        if "MTflnb" in body:
                            m = re.search(r'\\"(\+\d+)\\",\\"([^\\]+)\\"', body)
                            if m:
                                _captured["google_number"] = m.group(1)
                                _captured["verify_code"] = m.group(2)
                except Exception:
                    pass

            page.on("response", _on_response)

            try:
                # === STEP 1: Open Signup ===
                await progress(f"Membuka halaman signup ({device_name})...", "📱")
                await page.goto(SIGNUP_URL, wait_until="networkidle", timeout=30000)
                await asyncio.sleep(2)

                # === STEP 2: Name ===
                await progress(f"Mengisi nama: {self.config.first_name} {self.config.last_name}", "✍️")
                await page.wait_for_selector('input[name="firstName"]', timeout=15000)
                await page.fill('input[name="firstName"]', self.config.first_name)
                await asyncio.sleep(0.3)
                await page.fill('input[name="lastName"]', self.config.last_name)
                await asyncio.sleep(0.3)
                await page.locator('button:has-text("Next"), span:has-text("Next")').first.click()
                await asyncio.sleep(3)

                # === STEP 3: Birthday & Gender ===
                await progress("Mengisi tanggal lahir & gender...", "📅")
                month_div = page.locator('#month')
                await month_div.wait_for(timeout=10000)
                await month_div.click()
                await asyncio.sleep(0.5)
                await page.locator(f'#month [data-value="{mo}"]').click()
                await asyncio.sleep(0.3)
                await page.locator('input#day, input[name="day"]').fill(dy)
                await asyncio.sleep(0.3)
                await page.locator('input#year, input[name="year"]').fill(yr)
                await asyncio.sleep(0.3)
                await page.locator('#gender').click()
                await asyncio.sleep(0.5)
                gender_name = "Male" if self.config.gender == 1 else "Female"
                await page.get_by_role("option", name=gender_name, exact=True).click()
                await asyncio.sleep(0.5)
                await page.locator('button:has-text("Next"), span:has-text("Next")').first.click()
                await asyncio.sleep(3)

                # === STEP 4: Username ===
                await progress(f"Memilih username: {username}@gmail.com", "📧")
                create_own = page.locator('text="Create your own Gmail address"')
                if await create_own.count() > 0:
                    await create_own.click()
                    await asyncio.sleep(1)
                uinput = page.locator('input[name="Username"], input[type="text"]').first
                await uinput.wait_for(timeout=10000)
                await uinput.fill(username)
                await asyncio.sleep(1)
                await page.locator('button:has-text("Next"), span:has-text("Next")').first.click()
                await asyncio.sleep(3)

                # === STEP 5: Password ===
                await progress(f"Menyetel password...", "🔒")
                pw_input = page.locator('input[name="Passwd"], input[type="password"]').first
                await pw_input.wait_for(timeout=10000)
                await pw_input.fill(password)
                await asyncio.sleep(0.5)
                confirm = page.locator('input[name="PasswdAgain"], input[name="ConfirmPasswd"]').first
                if await confirm.count() > 0:
                    await confirm.fill(password)
                await asyncio.sleep(0.5)
                await page.locator('button:has-text("Next"), span:has-text("Next")').first.click()
                await asyncio.sleep(3)

                # === STEP 6: Phone Verification ===
                await progress("Mengirim SMS verifikasi...", "📡")
                sms_btn = page.locator('button:has-text("Send SMS"), span:has-text("Send SMS")')
                if await sms_btn.count() > 0:
                    await sms_btn.first.click()
                    result.status = "sms_sent"
                    await asyncio.sleep(3)
                    # Wait for transition
                    for _ in range(6):
                        await asyncio.sleep(3)
                        txt = await page.locator('body').inner_text()
                        if "Try Again" in txt or "try another" in txt.lower():
                            tr = page.locator('button:has-text("Try Again"), button:has-text("Try another way")')
                            if await tr.count() > 0:
                                await tr.first.click()
                                await asyncio.sleep(2)
                            break
                        if "Enter the code" in txt or "verification code" in txt.lower():
                            break
                else:
                    ph_input = page.locator('input#phoneNumberId, input[type="tel"]').first
                    if await ph_input.count() > 0:
                        full = f"+{phone}" if not phone.startswith("+") else phone
                        await ph_input.fill(full)
                        await asyncio.sleep(1)
                        await page.locator('button:has-text("Next"), span:has-text("Next")').first.click()
                        result.status = "sms_sent"
                        await asyncio.sleep(5)
                    else:
                        result.status = "failed"
                        result.error = "Tidak ada opsi verifikasi SMS"
                        result.time_taken = time.time() - start
                        return result

                # Capture device verification details
                await asyncio.sleep(2)
                if _captured["google_number"]:
                    result.google_number = _captured["google_number"]
                    result.verify_code = _captured["verify_code"]
                    result.status = "device_verify"

                # Capture page state as response
                try:
                    page_txt = await page.locator('body').inner_text()
                    result.response_json = {
                        "sms_step": result.status,
                        "page_hint": page_txt[:150].strip(),
                        "phone": phone,
                        "device": device_name,
                        "google_number": result.google_number,
                        "verify_code": result.verify_code,
                    }
                except:
                    pass

                if result.status == "device_verify":
                    await progress("Verifikasi device — kirim SMS manual!", "📲")
                    result.time_taken = time.time() - start
                    await browser.close()
                    return result

                await progress("SMS terkirim! Menunggu kode OTP...", "⏳")

                # === STEP 7: Wait for OTP ===
                if otp_future:
                    try:
                        otp_code = await asyncio.wait_for(otp_future, timeout=180)
                    except asyncio.TimeoutError:
                        result.status = "waiting_otp"
                        result.error = "Timeout 3 menit — OTP tidak diterima"
                        result.time_taken = time.time() - start
                        return result

                    if not otp_code or not str(otp_code).strip().isdigit():
                        result.status = "failed"
                        result.error = f"OTP tidak valid: {otp_code}"
                        result.time_taken = time.time() - start
                        return result

                    # === STEP 8: Verify OTP ===
                    await progress(f"Memverifikasi kode OTP: {otp_code}...", "🔐")
                    otp_selectors = [
                        'input[type="tel"]', 'input#code', 'input[name="code"]',
                        'input[name*="code"]', 'input[id*="code"]',
                        'input[aria-label*="code"]', 'input[aria-label*="Enter"]',
                    ]
                    otp_input = None
                    for sel in otp_selectors:
                        loc = page.locator(sel)
                        if await loc.count() > 0:
                            otp_input = loc.first
                            break

                    if otp_input:
                        await otp_input.fill(str(otp_code).strip())
                        await asyncio.sleep(1)
                        vbtn = page.locator('button:has-text("Verify"), button:has-text("Next"), span:has-text("Verify"), span:has-text("Next")')
                        await vbtn.first.click()
                        await asyncio.sleep(5)

                        # Check result
                        txt = await page.locator('body').inner_text()
                        if "I agree" in txt or "Terms" in txt or "Privacy" in txt:
                            agree = page.locator('button:has-text("I agree"), button:has-text("Agree")')
                            if await agree.count() > 0:
                                await agree.first.click()
                                await asyncio.sleep(3)
                            result.status = "created"
                            await progress("Akun berhasil dibuat! ✅", "✅")
                        elif "wrong" in txt.lower() or "incorrect" in txt.lower():
                            result.status = "failed"
                            result.error = "Kode OTP salah/ditolak Google"
                        else:
                            result.status = "created"
                            await progress("Akun berhasil dibuat! ✅", "✅")
                    else:
                        result.status = "failed"
                        result.error = "Input OTP tidak ditemukan di halaman"
                else:
                    result.status = "sms_sent"

            except PwTimeout as e:
                result.status = "failed"
                result.error = f"Timeout: {str(e)[:80]}"
                log.error(f"[Gmail] Timeout: {e}")
            except Exception as e:
                result.status = "failed"
                result.error = f"{str(e)[:120]}"
                log.error(f"[Gmail] Error: {e}")
            finally:
                await browser.close()

        result.time_taken = time.time() - start
        return result


async def bulk_create(phones: list, config: GmailConfig = None,
                      otp_futures: dict = None,
                      on_progress: ProgressCallback = None) -> list:
    """
    Create multiple Gmail accounts with semaphore (max GMAIL_WORKERS).
    Returns list of GmailResult.
    """
    sem = asyncio.Semaphore(GMAIL_WORKERS)
    creator = GmailCreator(config=config)
    results = []

    async def _worker(phone, cc, future):
        async with sem:
            return await creator.create_account(
                phone=phone, country_code=cc,
                otp_future=future, on_progress=on_progress,
            )

    tasks = []
    for phone in phones:
        phone = re.sub(r'[^\d]', '', phone)
        if phone.startswith('62'):
            cc = 'id'
        elif phone.startswith('63'):
            cc = 'ph'
        elif phone.startswith('95'):
            cc = 'mm'
        elif phone.startswith('1') and len(phone) == 11:
            cc = 'us'
        else:
            cc = 'id'
        future = otp_futures.get(phone) if otp_futures else None
        tasks.append(_worker(phone, cc, future))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Convert exceptions to GmailResult
    final = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            final.append(GmailResult(
                phone=phones[i], status="failed",
                error=str(r)[:120], time_taken=0,
            ))
        else:
            final.append(r)
    return final
