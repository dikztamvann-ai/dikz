"""
tele_report_cmd.py — /report (owner-only)

Report akun/channel/grup ke daftar alamat anti-fraud untuk suspend/freeze.
Alur interaktif:
  /report @username | t.me/channel | t.me/+invite | -100xxx | 12345
  → Deteksi target (user/channel/group)
  → Preview + [Benar] [Kembali]
  → Pilih alasan (5 opsi, beda per tipe target)
  → Proses:
    Method 1: 100 sender × 2 text = 200 email
    Method 2: POST telegram.org/support × 20 (Turnstile bypass)
  Kedua method jalan bersamaan (concurrent).
"""
from __future__ import annotations

import asyncio
import functools
import html
import logging
import random
import re
import string
import time

import requests

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

# ── Injected globals ──
_screen = None
_em_fn = None
_USER_ID = None
_get_pool = None
_connect_sender = None
_db_cur = None
_db_conn = None

# ── Constants ──
REPORT_CMD_WORKERS = 10
REPORT_CMD_SENDERS = 100
REPORT_CMD_MAX_RETRY = 3
REPORT_CMD_TARGET_SENT = 200  # 100 sender × 2 text

# ── Method 2: telegram.org/support (Turnstile bypass) ──
TG_SUPPORT_URL = "https://telegram.org/support"
TG_CAPTCHA_URL = "https://telegram.org/support/captcha"
TG_SITEKEY = "0x4AAAAAABeXKow67DnvUBPD"
TG_SOLVER_URL = "https://api.ikyyxd.my.id/bypass/turnstile-cf-min"
TG_SUPPORT_TIMES = 20  # jumlah submit ke telegram.org/support

REPORT_CMD_EMAILS = [
    # ── Telegram Official (verified from docs & DSA page) ──
    "abuse@telegram.org",
    "dmca@telegram.org",
    "stopCA@telegram.org",
    "recover@telegram.org",
    "support@telegram.org",
    "security@telegram.org",
    "legal@telegram.org",
    "copyright@telegram.org",
    "complaints@telegram.org",
    # Telegram DSA (EU Digital Services Act representative)
    "dsa.telegram@edsr.eu",
    # Telegram misc (known subdomains)
    "support@stel.com",
    "abuse@stel.com",
    # ── Cloudflare (verified from cloudflare.com/trust-hub) ──
    "abuse@cloudflare.com",
    "registrar-abuse@cloudflare.com",
    # ── UK Government / NCSC ──
    "report@phishing.gov.uk",
    "phishing@hmrc.gov.uk",
    # ── Netcraft (anti-phishing/takedown) ──
    "scam@netcraft.com",
    "report@netcraft.com",
    "takedown@netcraft.com",
    "support@netcraft.com",
    # ── APWG (Anti-Phishing Working Group) ──
    "reportphishing@apwg.org",
    # ── Microsoft ──
    "phish@office365.microsoft.com",
    "abuse@microsoft.com",
    "reportphish@microsoft.com",
    # ── Apple ──
    "reportphishing@apple.com",
    "reportfacetimefraud@apple.com",
    # ── PayPal ──
    "spoof@paypal.com",
    # ── Meta / Facebook ──
    "phish@fb.com",
    "abuse@instagram.com",
    # ── Google ──
    "abuse@google.com",
    # ── Group-IB / CERT-GIB (cybercrime response) ──
    "support@group-ib.com",
    "response@cert-gib.com",
    "info@group-ib.com",
    # ── Banking / Financial (non-Monzo) ──
    "phishing@ulsterbank.com",
    "internetsecurity@barclays.co.uk",
    "emailscams@lloydsbanking.com",
    "phishing@hsbc.com",
    "phishing@natwest.com",
    # ── Interpol / EU ──
    "cybercrime@interpol.int",
    # ── Domain registrars ──
    "abuse@namecheap.com",
    "abuse@godaddy.com",
    "abuse@tucows.com",
]

# ── Reason definitions per target type ──
REASONS_USER = [
    ("scam", "Scam/Penipuan"),
    ("impersonasi", "Impersonasi"),
    ("spam", "Spam"),
    ("pelecehan", "Pelecehan"),
    ("other", "Lainnya"),
]
REASONS_CHANNEL = [
    ("fake_channel", "Fake Channel"),
    ("scam", "Scam/Penipuan"),
    ("copyright", "Hak Cipta"),
    ("illegal", "Konten Ilegal"),
    ("other", "Lainnya"),
]
REASONS_GROUP = [
    ("scam", "Scam/Penipuan"),
    ("pelecehan", "Pelecehan"),
    ("illegal", "Konten Ilegal"),
    ("spam_massal", "Spam Massal"),
    ("other", "Lainnya"),
]

def _reasons_for(target_type: str) -> list:
    if target_type == "channel":
        return REASONS_CHANNEL
    if target_type == "group":
        return REASONS_GROUP
    return REASONS_USER

# ── Session state ──
_REPORT_SESSIONS: dict = {}

# ── Random US name/phone generators for Method 2 ──
_US_FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael",
    "Linda", "David", "Elizabeth", "William", "Barbara", "Richard", "Susan",
    "Joseph", "Jessica", "Thomas", "Sarah", "Christopher", "Karen",
    "Daniel", "Lisa", "Matthew", "Nancy", "Anthony", "Betty", "Mark",
    "Margaret", "Donald", "Sandra", "Steven", "Ashley", "Andrew", "Dorothy",
    "Paul", "Kimberly", "Joshua", "Emily", "Kenneth", "Donna",
]
_US_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark",
    "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
    "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
]
_US_AREA_CODES = [
    "201", "202", "212", "213", "214", "215", "216", "224", "225", "234",
    "240", "248", "253", "254", "256", "260", "267", "269", "270", "281",
    "301", "302", "303", "304", "305", "310", "312", "313", "314", "315",
    "316", "317", "318", "319", "320", "321", "323", "325", "330", "331",
    "347", "351", "352", "360", "361", "386", "401", "402", "404", "405",
    "407", "408", "409", "410", "412", "413", "414", "415", "417", "419",
    "423", "424", "425", "430", "432", "434", "435", "440", "443", "469",
    "470", "475", "478", "479", "480", "484", "501", "502", "503", "504",
    "505", "507", "508", "509", "510", "512", "513", "515", "516", "517",
]


def _random_us_name() -> str:
    return f"{random.choice(_US_FIRST_NAMES)} {random.choice(_US_LAST_NAMES)}"


def _random_us_phone() -> str:
    area = random.choice(_US_AREA_CODES)
    mid = random.randint(200, 999)
    last = random.randint(1000, 9999)
    return f"+1{area}{mid}{last}"


def _random_email_addr() -> str:
    first = random.choice(_US_FIRST_NAMES).lower()
    last = random.choice(_US_LAST_NAMES).lower()
    num = random.randint(10, 999)
    domain = random.choice(["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"])
    return f"{first}.{last}{num}@{domain}"


# ── Turnstile solver ──
def _solve_turnstile(timeout: int = 90, max_attempts: int = 3) -> str | None:
    """Solve Cloudflare Turnstile via external solver API. Returns token or None.
    Retries up to max_attempts times since the solver API is unreliable."""
    import time as _time
    for attempt in range(max_attempts):
        try:
            resp = requests.get(
                TG_SOLVER_URL,
                params={"url": TG_SUPPORT_URL, "sitekey": TG_SITEKEY},
                headers={"Accept": "application/json"},
                timeout=timeout,
            )
            if resp.status_code != 200:
                if attempt < max_attempts - 1:
                    _time.sleep(3)
                continue

            ct = resp.headers.get("content-type", "")
            if not ct.startswith("application/json"):
                raw = resp.text.strip()
                if raw.startswith(("0.", "1.")) and len(raw) > 50:
                    return raw
                if attempt < max_attempts - 1:
                    _time.sleep(3)
                continue

            data = resp.json()

            # Check status field first
            if isinstance(data, dict) and data.get("status") is False:
                log.debug("[report_cmd] turnstile attempt %d failed: %s",
                          attempt + 1, data.get("error", "unknown"))
                if attempt < max_attempts - 1:
                    _time.sleep(3)
                continue

            # Try common response keys
            for key in ("token", "data", "result", "solution", "response", "turnstile"):
                val = data.get(key) if isinstance(data, dict) else None
                if isinstance(val, str) and len(val) > 50:
                    return val
                if isinstance(val, dict):
                    for subkey in ("token", "value", "response"):
                        sv = val.get(subkey)
                        if isinstance(sv, str) and len(sv) > 50:
                            return sv

            if attempt < max_attempts - 1:
                _time.sleep(3)
        except Exception as e:
            log.debug("[report_cmd] turnstile solve attempt %d error: %s", attempt + 1, e)
            if attempt < max_attempts - 1:
                _time.sleep(3)
    return None


# ── telegram.org/support submission ──
def _submit_tg_support(message: str, name: str, email: str, phone: str) -> bool:
    """Submit report to telegram.org/support with Turnstile bypass.
    Flow: solve captcha → POST /support/captcha (pending) → wait → POST again (ok) → POST /support form."""
    import time as _time

    token = _solve_turnstile()
    if not token:
        return False

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        "Origin": "https://telegram.org",
        "Referer": "https://telegram.org/support",
    })

    # Step 1: POST /support/captcha with token → {"pending":true}
    try:
        r1 = sess.post(TG_CAPTCHA_URL, data={"token": token}, timeout=30)
        if r1.status_code != 200:
            return False
        j1 = r1.json() if "json" in r1.headers.get("content-type", "") else {}
        if j1.get("ok"):
            # Sometimes server responds ok immediately, skip step 2
            pass
        elif j1.get("pending"):
            # Wait for server-side validation
            _time.sleep(2)
        else:
            # Unknown response, try continuing anyway
            _time.sleep(1)
    except Exception:
        return False

    # Step 2: POST /support/captcha again → {"ok":true}
    try:
        r2 = sess.post(TG_CAPTCHA_URL, data={"token": token}, timeout=30)
        if r2.status_code != 200:
            return False
        j2 = r2.json() if "json" in r2.headers.get("content-type", "") else {}
        if not j2.get("ok"):
            # Retry once more after short wait
            _time.sleep(2)
            r2b = sess.post(TG_CAPTCHA_URL, data={"token": token}, timeout=30)
            j2b = r2b.json() if "json" in r2b.headers.get("content-type", "") else {}
            if not j2b.get("ok"):
                return False
    except Exception:
        return False

    # Step 3: POST /support with form data
    try:
        r3 = sess.post(TG_SUPPORT_URL, data={
            "message": message,
            "legal_name": name,
            "email": email,
            "phone": phone,
            "setln": "",
            "cf-turnstile-response": token,
        }, timeout=30)
        if "alert-success" in r3.text or "Thanks for your report" in r3.text:
            return True
        return r3.status_code == 200
    except Exception:
        return False
def _get_bodies(target_type: str, reason: str) -> tuple:
    """Return (subject1, body1, subject2, body2) for given type+reason."""
    key = f"{target_type}_{reason}"
    pair = _BODY_MAP.get(key) or _BODY_MAP.get(f"user_other")
    return pair

_BODY_MAP: dict = {}

# -- Short, human-like email templates per reason per type --
# Each entry: (subject1, body1, subject2, body2)

_BODY_MAP["user_scam"] = (
    "Abuse Report: Telegram Account Running Financial Scam",
    """Hi,

I'm reporting a Telegram account that is actively scamming people out of money.

{account_block}

This account contacts people pretending to be an investment advisor or support agent, builds trust, then asks for payments or credentials. Multiple people have lost money. The scam is ongoing and new victims are being contacted daily.

The account uses fake identity, fake promises of returns, and urgency tactics to pressure victims into transferring funds before they can verify anything.

Please suspend this account immediately and preserve any data for law enforcement. Every day it stays up means more people get scammed.

Thank you.""",
    "Report: Telegram Account Fraud - Terms of Service Violation",
    """Hello,

I want to formally report a Telegram account that violates your Terms of Service through systematic fraud.

{account_block}

This account exists solely to defraud users. It impersonates legitimate businesses, sends phishing links, and manipulates people into sending money or sharing login credentials.

This violates your ToS provisions against fraud, impersonation, and deceptive conduct. The account should be permanently terminated and the phone number banned to prevent recreation.

I request confirmation that action has been taken.

Regards.""",
)

_BODY_MAP["user_impersonasi"] = (
    "Abuse Report: Identity Impersonation on Telegram",
    """Hi,

I'm reporting a Telegram account that is impersonating a real person/organization.

{account_block}

This account uses stolen photos, names, and branding from a real entity to trick people into trusting them. They pose as official support, a known public figure, or a company representative to extract money or personal information from victims.

The real person/org being impersonated has no connection to this account. People are being deceived daily.

Please remove this account and ban the associated phone/device.

Thank you.""",
    "Formal Report: Identity Theft and Impersonation",
    """Hello,

I'm submitting a report about a Telegram account engaged in identity impersonation.

{account_block}

This account has copied the name, photo, and bio of a real person or organization without permission. It uses this stolen identity to gain trust and then exploits that trust for fraud or other harmful purposes.

This violates your policies on impersonation. Please suspend permanently and preserve registration data.

Regards.""",
)

_BODY_MAP["user_spam"] = (
    "Abuse Report: Mass Spam Operation on Telegram",
    """Hi,

I'm reporting a Telegram account sending mass unsolicited messages to users.

{account_block}

This account sends bulk spam messages to hundreds of users, promoting scam links, fake services, or unwanted content. It operates like a bot, messaging people who never opted in.

The spam is ongoing. Please suspend this account and block recreation.

Thank you.""",
    "Report: Spam Account Violating Telegram ToS",
    """Hello,

Reporting a Telegram account for systematic spam operations.

{account_block}

This account mass-messages users with unsolicited promotional or deceptive content. It violates your anti-spam policies and degrades the platform experience for legitimate users.

Please take action to terminate this account.

Regards.""",
)

_BODY_MAP["user_pelecehan"] = (
    "Abuse Report: Harassment and Threats on Telegram",
    """Hi,

I'm reporting a Telegram account that is harassing and threatening people.

{account_block}

This account sends threatening messages, engages in targeted harassment, and intimidates victims. The behavior is persistent and escalating. Victims feel unsafe.

Please suspend this account immediately. This is a safety issue.

Thank you.""",
    "Report: Targeted Harassment - Telegram Account",
    """Hello,

I'm reporting a Telegram account for ongoing harassment.

{account_block}

This account repeatedly targets individuals with abusive messages, threats, and intimidation. The harassment is coordinated and deliberate, not a one-time dispute.

This violates your policies against harassment and abuse. Please remove the account and preserve evidence.

Regards.""",
)

_BODY_MAP["user_other"] = (
    "Abuse Report: Telegram Account Violating Terms of Service",
    """Hi,

I'm reporting a Telegram account for violating platform rules.

{account_block}

This account is engaged in activity that violates Telegram's Terms of Service. The behavior is harmful to other users and the platform community.

Please review and take appropriate action.

Thank you.""",
    "Report: Terms of Service Violation",
    """Hello,

Submitting a report about a Telegram account.

{account_block}

This account is violating your Terms of Service through harmful conduct. Please review the account activity and take action as appropriate.

Regards.""",
)

_BODY_MAP["channel_fake_channel"] = (
    "Abuse Report: Fake/Impersonation Channel on Telegram",
    """Hi,

I'm reporting a Telegram channel that impersonates a legitimate channel or organization.

{account_block}

This channel copies the name, logo, and branding of a real channel to mislead users. People join thinking it's official and get exposed to scams, malware links, or misinformation.

The real organization has no affiliation with this channel. Please remove it.

Thank you.""",
    "Report: Fraudulent Telegram Channel Impersonating Brand",
    """Hello,

Reporting a Telegram channel for brand impersonation and fraud.

{account_block}

This channel uses stolen branding to appear official. It distributes scam links and misleads subscribers. This violates your impersonation and fraud policies.

Please terminate this channel and prevent recreation.

Regards.""",
)

_BODY_MAP["channel_scam"] = (
    "Abuse Report: Scam Channel on Telegram",
    """Hi,

I'm reporting a Telegram channel that is used to distribute scams.

{account_block}

This channel posts fake investment schemes, phishing links, and fraudulent offers. Subscribers are lured into sending money or sharing credentials. Multiple people have been defrauded.

Please shut down this channel immediately.

Thank you.""",
    "Report: Telegram Channel Used for Financial Fraud",
    """Hello,

Reporting a Telegram channel for systematic fraud operations.

{account_block}

This channel exists to distribute scam content and defraud subscribers. It promotes fake services, bogus investments, and phishing schemes.

This violates your policies. Please remove permanently.

Regards.""",
)

_BODY_MAP["channel_copyright"] = (
    "DMCA/Copyright Report: Telegram Channel Distributing Pirated Content",
    """Hi,

I'm reporting a Telegram channel for distributing copyrighted material without authorization.

{account_block}

This channel shares pirated software, movies, music, or other copyrighted content. The rights holders have not authorized this distribution.

Please remove the infringing content and take action against the channel.

Thank you.""",
    "Copyright Infringement Notice: Telegram Channel",
    """Hello,

This is a formal copyright infringement notice regarding a Telegram channel.

{account_block}

This channel is distributing copyrighted works without permission from the rights holders. This constitutes copyright infringement under applicable law including the DMCA.

Please remove the channel and preserve records.

Regards.""",
)

_BODY_MAP["channel_illegal"] = (
    "Abuse Report: Illegal Content on Telegram Channel",
    """Hi,

I'm reporting a Telegram channel that distributes illegal content.

{account_block}

This channel shares content related to illegal activities including drugs, weapons, stolen data, or other prohibited material. This is not borderline content - it is clearly illegal.

Please remove this channel immediately and report to authorities if appropriate.

Thank you.""",
    "Report: Telegram Channel Distributing Illegal Material",
    """Hello,

Reporting a Telegram channel for distribution of illegal content.

{account_block}

This channel openly distributes material related to illegal services and goods. It violates both your Terms of Service and applicable law.

Please shut down immediately and preserve data for law enforcement.

Regards.""",
)

_BODY_MAP["channel_other"] = (
    "Abuse Report: Telegram Channel Violating Terms",
    """Hi,

I'm reporting a Telegram channel for violating platform policies.

{account_block}

This channel engages in activity that violates Telegram's Terms of Service and harms the community.

Please review and take action.

Thank you.""",
    "Report: Channel Terms of Service Violation",
    """Hello,

Reporting a Telegram channel for policy violations.

{account_block}

This channel violates your Terms of Service. Please review and take appropriate action.

Regards.""",
)

_BODY_MAP["group_scam"] = (
    "Abuse Report: Telegram Group Used for Organized Scam",
    """Hi,

I'm reporting a Telegram group that coordinates fraud operations.

{account_block}

This group is used to organize scams, share victim lists, distribute phishing kits, and coordinate fraud against innocent people. Members actively discuss how to defraud targets.

Please shut down this group and ban the administrators.

Thank you.""",
    "Report: Fraud Coordination Group on Telegram",
    """Hello,

Reporting a Telegram group for coordinated fraud.

{account_block}

This group serves as a hub for organizing scam operations. Participants share tools, victim information, and techniques for defrauding people.

This violates your policies. Please terminate and preserve data.

Regards.""",
)

_BODY_MAP["group_pelecehan"] = (
    "Abuse Report: Harassment Group on Telegram",
    """Hi,

I'm reporting a Telegram group that coordinates harassment campaigns.

{account_block}

This group is used to organize targeted harassment against individuals. Members share personal info of targets, coordinate attacks, and encourage abuse. Victims are being actively harmed.

Please remove this group immediately. This is a safety emergency.

Thank you.""",
    "Report: Coordinated Harassment via Telegram Group",
    """Hello,

Reporting a Telegram group for coordinated harassment.

{account_block}

This group organizes harassment campaigns against individuals. It facilitates doxxing, threats, and coordinated abuse.

Please terminate immediately and preserve evidence.

Regards.""",
)

_BODY_MAP["group_illegal"] = (
    "Abuse Report: Illegal Activity in Telegram Group",
    """Hi,

I'm reporting a Telegram group involved in illegal activities.

{account_block}

This group is used to trade illegal goods or services, share illegal content, or coordinate criminal activity. The nature of the content is clearly unlawful.

Please remove this group and report to authorities as required.

Thank you.""",
    "Report: Criminal Activity in Telegram Group",
    """Hello,

Reporting a Telegram group for illegal activity.

{account_block}

This group facilitates illegal trade and criminal coordination. It violates your Terms of Service and applicable laws.

Please shut down and preserve records for law enforcement.

Regards.""",
)

_BODY_MAP["group_spam_massal"] = (
    "Abuse Report: Mass Spam Coordination Group",
    """Hi,

I'm reporting a Telegram group that coordinates mass spam campaigns.

{account_block}

This group is used to organize and distribute mass spam across Telegram. Members share spam tools, target lists, and coordinate bulk messaging that degrades the platform.

Please shut down this group and ban its operators.

Thank you.""",
    "Report: Spam Hub Group on Telegram",
    """Hello,

Reporting a Telegram group for spam coordination.

{account_block}

This group serves as a hub for mass spam operations on your platform. It violates your anti-spam policies.

Please terminate and block recreation.

Regards.""",
)

_BODY_MAP["group_other"] = (
    "Abuse Report: Telegram Group Violating Terms",
    """Hi,

I'm reporting a Telegram group for violating platform policies.

{account_block}

This group engages in activity that violates Telegram's Terms of Service and causes harm.

Please review and take action.

Thank you.""",
    "Report: Group Terms of Service Violation",
    """Hello,

Reporting a Telegram group for policy violations.

{account_block}

This group violates your Terms of Service. Please review and take appropriate action.

Regards.""",
)

# -- Short message templates for telegram.org/support form (Method 2) --
_TG_SUPPORT_MESSAGES: dict = {}

_TG_SUPPORT_MESSAGES["user_scam"] = [
    "This Telegram account is scamming people. They pretend to be a legit business, build trust, then steal money. Multiple victims already. Account: {info}. Please ban them.",
    "Reporting a scam account on Telegram. They trick people into sending money with fake investment promises. Still active, still scamming. {info}",
    "Hi, this user is running financial fraud on Telegram. They contact random people, pretend to be support agents, and steal credentials/money. Info: {info}",
    "Please investigate this Telegram account for fraud. They've scammed several people I know. Operating under false identity. {info}",
]

_TG_SUPPORT_MESSAGES["user_impersonasi"] = [
    "This account is impersonating a real person/company on Telegram. Using stolen photos and name to trick people. {info}. Please remove.",
    "Reporting identity theft on Telegram. This account copies a real person's identity to scam others. {info}",
    "Someone is pretending to be an official representative using this Telegram account. It's fake. {info}",
    "This account uses stolen identity material to deceive people on Telegram. The real person has nothing to do with it. {info}",
]

_TG_SUPPORT_MESSAGES["user_spam"] = [
    "This Telegram account is mass-spamming users with unwanted messages. Hundreds of people affected. {info}",
    "Reporting a spam bot account on Telegram. Sends bulk messages to random users. {info}",
    "This account spams everyone with scam links and promotions. Nobody asked for this. {info}",
    "Please ban this spam account. It messages people non-stop with unwanted content. {info}",
]

_TG_SUPPORT_MESSAGES["user_pelecehan"] = [
    "This Telegram account is harassing and threatening people. Sending abusive messages repeatedly. {info}. Please take action.",
    "Reporting harassment. This account sends threats and intimidating messages to multiple people. {info}",
    "This user won't stop harassing people on Telegram. Threats, abuse, stalking behavior. {info}",
    "Please help. This account is threatening people on Telegram. It's getting worse. {info}",
]

_TG_SUPPORT_MESSAGES["user_other"] = [
    "Reporting this Telegram account for violating your terms. Harmful behavior affecting multiple users. {info}",
    "This account is breaking Telegram rules and harming people. Please review. {info}",
    "Please investigate this Telegram account. It's engaged in harmful activity. {info}",
    "Reporting a problematic Telegram account that's causing harm. {info}",
]

_TG_SUPPORT_MESSAGES["channel_fake_channel"] = [
    "This Telegram channel is impersonating a real brand/organization. Misleading subscribers with fake identity. {info}",
    "Fake channel alert. This channel copies a legit channel's branding to scam people. {info}",
    "Reporting a fraudulent channel impersonating an official source. {info}. People are being misled.",
    "This channel uses stolen branding. It's not affiliated with the real org. Scamming subscribers. {info}",
]

_TG_SUPPORT_MESSAGES["channel_scam"] = [
    "This Telegram channel distributes scam links and fake investment schemes. People are losing money. {info}",
    "Reporting a scam channel. Posts phishing links and fraudulent offers daily. {info}",
    "This channel is a scam operation. Fake giveaways, fake investments, real losses. {info}",
    "Please shut down this fraud channel. Subscribers are being scammed. {info}",
]

_TG_SUPPORT_MESSAGES["channel_copyright"] = [
    "This Telegram channel distributes pirated content without authorization. Copyright infringement. {info}",
    "Reporting copyright violation. This channel shares pirated material. {info}",
    "This channel is distributing copyrighted content illegally. DMCA violation. {info}",
    "Please remove this channel for distributing pirated software/media. {info}",
]

_TG_SUPPORT_MESSAGES["channel_illegal"] = [
    "This Telegram channel shares illegal content - drugs, weapons, stolen data. {info}. Please remove immediately.",
    "Reporting a channel distributing illegal material on Telegram. {info}",
    "This channel is openly sharing illegal goods/services. Clearly unlawful. {info}",
    "Please investigate this channel for illegal content distribution. {info}",
]

_TG_SUPPORT_MESSAGES["channel_other"] = [
    "Reporting this Telegram channel for policy violations. Harmful content. {info}",
    "This channel violates Telegram's terms. Please review. {info}",
    "Please investigate this channel for harmful activity. {info}",
    "Reporting a Telegram channel that's causing harm. {info}",
]

_TG_SUPPORT_MESSAGES["group_scam"] = [
    "This Telegram group coordinates fraud operations. Members organize scams together. {info}",
    "Reporting a scam coordination group. They share victim lists and fraud tools. {info}",
    "This group is a fraud hub. Organized scam ring operating through it. {info}",
    "Please shut down this group - it's used to coordinate financial fraud. {info}",
]

_TG_SUPPORT_MESSAGES["group_pelecehan"] = [
    "This Telegram group coordinates harassment campaigns against people. Doxxing and threats. {info}",
    "Reporting a harassment group. They target individuals with coordinated abuse. {info}",
    "This group organizes attacks on people. Sharing personal info, making threats. {info}",
    "Please remove this group - it's used for coordinated harassment. {info}",
]

_TG_SUPPORT_MESSAGES["group_illegal"] = [
    "This Telegram group facilitates illegal trade and criminal activity. {info}",
    "Reporting a group involved in illegal operations on Telegram. {info}",
    "This group trades illegal goods and coordinates criminal activity. {info}",
    "Please investigate this group for illegal activity. {info}",
]

_TG_SUPPORT_MESSAGES["group_spam_massal"] = [
    "This Telegram group coordinates mass spam campaigns across the platform. {info}",
    "Reporting a spam coordination group. They organize bulk messaging attacks. {info}",
    "This group is a spam hub - sharing tools and targets for mass spam. {info}",
    "Please shut down this spam coordination group. {info}",
]

_TG_SUPPORT_MESSAGES["group_other"] = [
    "Reporting this Telegram group for policy violations. {info}",
    "This group violates Telegram's terms. Please review. {info}",
    "Please investigate this group for harmful activity. {info}",
    "Reporting a Telegram group causing harm. {info}",
]



# ══════════════════════════════════════════════
#  TARGET PARSING & DETECTION
# ══════════════════════════════════════════════
_SPLIT_RE = re.compile(r"[|\n,;\s]+")
_TME_MSG = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/"
    r"(?:c/(?P<cid>\d+)|(?P<uname>[A-Za-z][A-Za-z0-9_]{3,31}))/(?P<mid>\d+)",
    re.I,
)
_TME_ANY = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/"
    r"(?P<inv>joinchat/|\+)?(?P<path>[A-Za-z0-9_\-]+)",
    re.I,
)


def _parse_target(text: str) -> dict | None:
    """Parse single target input. Returns {kind, value} or None."""
    text = text.strip().rstrip("/")
    if not text:
        return None

    # t.me/channel/123 (message link)
    m = _TME_MSG.search(text)
    if m:
        if m.group("cid"):
            return {"kind": "id", "value": int(f"-100{m.group('cid')}"), "msg_id": int(m.group("mid"))}
        return {"kind": "username", "value": m.group("uname"), "msg_id": int(m.group("mid"))}

    # t.me/username or t.me/+invite
    m = _TME_ANY.search(text)
    if m:
        path = m.group("path")
        if m.group("inv"):
            return {"kind": "invite", "value": path.lstrip("+")}
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", path):
            return {"kind": "username", "value": path}
        return {"kind": "invite", "value": path.lstrip("+")}

    # +invite
    if text.startswith("+") and not re.fullmatch(r"\+\d{7,15}", text):
        return {"kind": "invite", "value": text.lstrip("+")}

    # Numeric ID
    if re.fullmatch(r"-?\d{6,}", text):
        return {"kind": "id", "value": int(text)}

    # @username
    if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}", text):
        return {"kind": "username", "value": text.lstrip("@")}

    return None


def report_cmd_init(**kw):
    """Inject dependencies from dik.py."""
    g = globals()
    for k, v in kw.items():
        g[k] = v


def _load_email_senders():
    """Load all mailbox senders from DB."""
    if not _db_cur:
        return []
    try:
        _db_cur.execute(
            "SELECT m.id, m.email, m.mailbox_id, "
            "a.phpsessid, a.sitepro_email AS acc_email, a.sitepro_password "
            "FROM sitepro_mailboxes m "
            "JOIN sitepro_accounts a ON m.account_id = a.id "
            "WHERE m.mailbox_id IS NOT NULL AND m.mailbox_id > 0"
        )
        rows = _db_cur.fetchall()
        cols = [d[0] for d in _db_cur.description]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log.warning("[report_cmd] load senders: %s", e)
        return []


def _build_account_block(info: dict) -> str:
    """Build account info block — only include detected fields."""
    lines = []
    ttype = info.get("type") or "user"
    type_label = {"user": "User", "channel": "Channel", "group": "Group"}.get(ttype, "Unknown")
    lines.append(f"Type          : {type_label}")

    name = info.get("name") or info.get("title") or ""
    if not name:
        first = info.get("first") or ""
        last = info.get("last") or ""
        name = f"{first} {last}".strip()
    if name:
        lines.append(f"Name          : {name}")

    username = info.get("username")
    if username:
        lines.append(f"Username      : @{username}")
        lines.append(f"Link          : https://t.me/{username}")

    uid = info.get("id")
    if uid:
        lines.append(f"ID            : {uid}")

    members = info.get("members")
    if members and ttype in ("channel", "group"):
        lines.append(f"Members       : {members}")

    bio = info.get("about") or ""
    if bio.strip():
        lines.append(f"Bio           : {bio.strip()[:100]}")

    return "\n".join(lines) if lines else "Account information unavailable"


async def _detect_target(target_str: str) -> dict | None:
    """Detect target via Telethon. Returns info dict with 'type' field or None."""
    pool = _get_pool(_USER_ID) if _get_pool else []
    senders = [s for s in (pool or []) if s.get("session") and s.get("status") != "dead"]
    if not senders:
        return None

    sender = senders[0]
    client = await _connect_sender(sender["session"])
    if not client:
        return None

    try:
        from telethon.tl.functions.users import GetFullUserRequest
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.types import User, Channel, Chat

        parsed = _parse_target(target_str)
        if not parsed:
            # Fallback: try raw string
            parsed = {"kind": "username", "value": target_str.strip().lstrip("@")}

        entity = None
        value = parsed["value"]

        if parsed["kind"] == "username":
            try:
                entity = await client.get_entity(value)
            except Exception:
                pass
        elif parsed["kind"] == "id":
            try:
                entity = await client.get_entity(int(value))
            except Exception:
                pass
        elif parsed["kind"] == "invite":
            from telethon.tl.functions.messages import CheckChatInviteRequest
            from telethon.tl.types import ChatInviteAlready
            try:
                meta = await client(CheckChatInviteRequest(value))
                if isinstance(meta, ChatInviteAlready):
                    entity = getattr(meta, "chat", None)
                else:
                    # ChatInvite (not joined) — extract basic info
                    is_ch = bool(getattr(meta, "broadcast", False))
                    return {
                        "type": "channel" if is_ch else "group",
                        "id": None,
                        "name": getattr(meta, "title", "?"),
                        "title": getattr(meta, "title", "?"),
                        "username": None,
                        "about": getattr(meta, "about", "") or "",
                        "members": getattr(meta, "participants_count", None),
                    }
            except Exception:
                pass

        if entity is None:
            return None

        # ── User ──
        if isinstance(entity, User):
            info = {
                "type": "user",
                "id": entity.id,
                "first": getattr(entity, "first_name", "") or "",
                "last": getattr(entity, "last_name", "") or "",
                "username": getattr(entity, "username", None),
                "about": "",
            }
            try:
                full = await client(GetFullUserRequest(entity))
                fu = getattr(full, "full_user", None)
                info["about"] = (getattr(fu, "about", "") or "") if fu else ""
            except Exception:
                pass
            info["name"] = f"{info['first']} {info['last']}".strip()
            return info

        # ── Channel / Group ──
        if isinstance(entity, (Channel, Chat)):
            is_ch = bool(getattr(entity, "broadcast", False))
            info = {
                "type": "channel" if is_ch else "group",
                "id": entity.id,
                "title": getattr(entity, "title", "?"),
                "name": getattr(entity, "title", "?"),
                "username": getattr(entity, "username", None),
                "about": "",
                "members": getattr(entity, "participants_count", None),
            }
            if isinstance(entity, Channel):
                try:
                    full = await client(GetFullChannelRequest(entity))
                    fc = getattr(full, "full_chat", None)
                    info["about"] = (getattr(fc, "about", "") or "") if fc else ""
                    if getattr(fc, "participants_count", None):
                        info["members"] = fc.participants_count
                except Exception:
                    pass
            return info

        return {"type": "user", "id": getattr(entity, "id", None),
                "name": str(entity), "username": None, "about": ""}
    except Exception as e:
        log.warning("[report_cmd] detect error: %s", e)
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ══════════════════════════════════════════════
#  COMMAND HANDLER
# ══════════════════════════════════════════════
async def report_command(update, context):
    """Handle /report — owner only. Step 1: parse + detect."""
    user_id = update.effective_user.id
    if user_id != _USER_ID:
        await update.message.reply_text(
            _screen("REPORT", "⌁ Fitur ini khusus owner."),
            parse_mode=ParseMode.HTML,
        )
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            _screen("REPORT", (
                "⌁ <b>Report — Suspend/Freeze Target</b>\n\n"
                "  Penggunaan:\n"
                "  <code>/report @username</code>\n"
                "  <code>/report t.me/channel</code>\n"
                "  <code>/report t.me/c/123/456</code>\n"
                "  <code>/report -1001234567890</code>\n\n"
                "  ⌁ Deteksi → Konfirmasi → Pilih Alasan → Kirim\n"
                f"  Method 1: 100 sender · 2 text · 200 email\n"
                f"  Method 2: telegram.org/support × {TG_SUPPORT_TIMES}\n"
                f"  → {len(REPORT_CMD_EMAILS)} alamat anti-fraud/abuse"
            )),
            parse_mode=ParseMode.HTML,
        )
        return

    target_str = " ".join(args)
    msg = await update.message.reply_text(
        _screen("REPORT", (
            f"⌁ <b>Detecting...</b>\n\n"
            f"  Target: <code>{html.escape(target_str)}</code>\n"
            f"  Resolving entity..."
        )),
        parse_mode=ParseMode.HTML,
    )

    info = await _detect_target(target_str)
    if not info:
        await msg.edit_text(
            _screen("REPORT", (
                f"⌁ <b>Target Tidak Ditemukan</b>\n\n"
                f"  <code>{html.escape(target_str)}</code>\n\n"
                f"  Pastikan username/ID/link valid\n"
                f"  dan sender Telethon tersedia."
            )),
            parse_mode=ParseMode.HTML,
        )
        return

    # Store session
    _REPORT_SESSIONS[user_id] = {
        "info": info,
        "target_type": info.get("type", "user"),
        "target_str": target_str,
        "reason": None,
        "msg": msg,
    }

    # Show preview
    await _show_preview(msg, user_id, info)


async def _show_preview(msg, user_id: int, info: dict):
    """Step 2: Show detected target + [Benar] [Kembali]."""
    block = _build_account_block(info)
    ttype = info.get("type", "user")
    type_icons = {"user": "👤", "channel": "📢", "group": "👥"}
    type_icon = type_icons.get(ttype, "📋")
    type_label = {"user": "User", "channel": "Channel", "group": "Group"}.get(ttype, "?")

    text = (
        f"⌁ <b>Report — Target Detected</b>\n\n"
        f"  {type_icon} <b>{type_label}</b>\n\n"
        f"<blockquote>{html.escape(block)}</blockquote>\n\n"
        f"  Method 1: 100 sender · 2 text · 200 email\n"
        f"  Method 2: telegram.org/support × {TG_SUPPORT_TIMES}\n"
        f"  → {len(REPORT_CMD_EMAILS)} alamat anti-fraud/abuse"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Benar, Lanjut", callback_data=f"rpt_confirm_{user_id}", style="success")],
        [InlineKeyboardButton("« Kembali", callback_data=f"rpt_back_{user_id}", style="danger")],
    ])

    await msg.edit_text(
        _screen("REPORT", text),
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def _show_reasons(msg, user_id: int, session: dict):
    """Step 3: Show 5 reason buttons based on target type."""
    info = session["info"]
    ttype = session["target_type"]
    reasons = _reasons_for(ttype)

    name = info.get("name") or info.get("title") or info.get("username") or "?"
    type_label = {"user": "User", "channel": "Channel", "group": "Group"}.get(ttype, "?")

    text = (
        f"⌁ <b>Report — Pilih Alasan</b>\n\n"
        f"  Target: <b>{html.escape(name[:30])}</b> ({type_label})\n\n"
        f"  Pilih alasan report di bawah.\n"
        f"  Setiap alasan menggunakan text email\n"
        f"  yang berbeda dan spesifik."
    )

    kb = []
    row = []
    for key, label in reasons:
        row.append(InlineKeyboardButton(
            label, callback_data=f"rpt_reason_{key}_{user_id}", style="primary",
        ))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("« Kembali", callback_data=f"rpt_back_{user_id}", style="danger")])

    await msg.edit_text(
        _screen("REPORT", text),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def _show_confirm_send(msg, user_id: int, session: dict):
    """Step 3.5: Show summary before sending."""
    info = session["info"]
    ttype = session["target_type"]
    reason = session["reason"]
    reasons = _reasons_for(ttype)
    reason_label = next((l for k, l in reasons if k == reason), reason)

    name = info.get("name") or info.get("title") or info.get("username") or "?"
    type_label = {"user": "User", "channel": "Channel", "group": "Group"}.get(ttype, "?")

    text = (
        f"⌁ <b>Report — Konfirmasi Kirim</b>\n\n"
        f"  Target  : <b>{html.escape(name[:30])}</b>\n"
        f"  Tipe    : {type_label}\n"
        f"  Alasan  : <b>{html.escape(reason_label)}</b>\n\n"
        f"  <b>Method 1</b>: {REPORT_CMD_SENDERS} sender · 2 text · {REPORT_CMD_TARGET_SENT} email\n"
        f"  <b>Method 2</b>: telegram.org/support × {TG_SUPPORT_TIMES}\n"
        f"  Alamat  : <b>{len(REPORT_CMD_EMAILS)}</b>"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⌁ Mulai Report", callback_data=f"rpt_go_{user_id}", style="danger")],
        [InlineKeyboardButton("« Ubah Alasan", callback_data=f"rpt_confirm_{user_id}", style="primary")],
        [InlineKeyboardButton("Batal", callback_data=f"rpt_back_{user_id}", style="danger")],
    ])

    await msg.edit_text(
        _screen("REPORT", text),
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


# ══════════════════════════════════════════════
#  CALLBACK HANDLER
# ══════════════════════════════════════════════
async def report_callback(update, context):
    """Handle rpt_ callbacks."""
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    if user_id != _USER_ID:
        await query.answer("Owner only", show_alert=True)
        return

    session = _REPORT_SESSIONS.get(user_id)

    if data.startswith("rpt_confirm_"):
        if not session:
            await query.answer("Session expired, ulang /report", show_alert=True)
            return
        await query.answer()
        await _show_reasons(session["msg"], user_id, session)
        return

    if data.startswith("rpt_reason_"):
        if not session:
            await query.answer("Session expired", show_alert=True)
            return
        # Extract reason key: rpt_reason_{key}_{user_id}
        rest = data[len("rpt_reason_"):]
        reason_key = rest.rsplit("_", 1)[0]
        session["reason"] = reason_key
        await query.answer()
        await _show_confirm_send(session["msg"], user_id, session)
        return

    if data.startswith("rpt_go_"):
        if not session:
            await query.answer("Session expired", show_alert=True)
            return
        if not session.get("reason"):
            await query.answer("Pilih alasan dulu", show_alert=True)
            return
        await query.answer("Report dimulai...")
        asyncio.create_task(_run_report_emails(session))
        return

    if data.startswith("rpt_back_"):
        if session:
            _REPORT_SESSIONS.pop(user_id, None)
        await query.answer("Dibatalkan")
        try:
            await query.message.edit_text(
                _screen("REPORT", (
                    "⌁ <b>Report Dibatalkan</b>\n\n"
                    "  Ketik /report untuk mulai lagi."
                )),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return


# ══════════════════════════════════════════════
#  EMAIL SENDING ENGINE
# ══════════════════════════════════════════════
async def _run_report_emails(session: dict):
    """Run both methods concurrently:
    Method 1: 100 sender × 2 texts = 200 emails
    Method 2: POST telegram.org/support × 20 (Turnstile bypass)
    """
    from sitepro_fix_module import (
        sitepro_restore_session, sitepro_open_webmail,
        roundcube_get_compose_token, roundcube_send_email,
    )

    msg = session["msg"]
    info = session["info"]
    user_id = list(k for k, v in _REPORT_SESSIONS.items() if v is session)
    user_id = user_id[0] if user_id else _USER_ID

    ttype = session["target_type"]
    reason = session["reason"]

    # Get body templates for this reason
    bodies = _get_bodies(ttype, reason)
    if not bodies:
        bodies = _get_bodies("user", "other")
    subject1, body_tpl1, subject2, body_tpl2 = bodies

    # Build account block and fill templates
    block = _build_account_block(info)
    body1 = body_tpl1.replace("{account_block}", block)
    body2 = body_tpl2.replace("{account_block}", block)

    # Build info string for Method 2
    tg_info_str = block.replace("\n", " | ")

    t0 = time.time()
    all_senders = _load_email_senders()
    if not all_senders:
        await msg.edit_text(
            _screen("REPORT", (
                "⌁ <b>Gagal</b>\n\n"
                "  Tidak ada sender email di database.\n"
            )),
            parse_mode=ParseMode.HTML,
        )
        return

    pick_count = min(REPORT_CMD_SENDERS, len(all_senders))
    senders = random.sample(all_senders, pick_count)

    to_str = ", ".join(REPORT_CMD_EMAILS)
    stats = {"ok": 0, "fail": 0, "retries": 0, "done": 0,
             "total": pick_count * 2, "target": REPORT_CMD_TARGET_SENT,
             "tg_ok": 0, "tg_fail": 0}
    last_edit = [0.0]

    loop = asyncio.get_event_loop()
    pool = ThreadPoolExecutor(max_workers=REPORT_CMD_WORKERS)

    # Get reason label for display
    reasons = _reasons_for(ttype)
    reason_label = next((l for k, l in reasons if k == reason), reason)
    name = info.get("name") or info.get("title") or info.get("username") or "?"

    def _send_one(sender, body, subject):
        try:
            phpsessid = sender.get("phpsessid") or ""
            acc_email = sender.get("acc_email") or ""
            password = sender.get("sitepro_password") or ""
            mailbox_id = sender.get("mailbox_id")
            if not phpsessid:
                return False
            sess, ok = sitepro_restore_session(phpsessid, acc_email, password)
            if not sess or not ok:
                return False
            wm_sess, _msg = sitepro_open_webmail(sess, mailbox_id)
            if not wm_sess:
                return False
            token, from_id, compose_id = roundcube_get_compose_token(wm_sess)
            if not token:
                return False
            ok = roundcube_send_email(
                wm_sess, token, from_id, compose_id,
                to_str, body, subject=subject,
            )
            return bool(ok)
        except Exception as e:
            log.debug("[report_cmd] send fail: %s", e)
            return False

    async def _progress(force=False):
        now = time.time()
        if not force and now - last_edit[0] < 5.0:
            return
        last_edit[0] = now
        total_target = REPORT_CMD_TARGET_SENT + TG_SUPPORT_TIMES
        total_ok = stats["ok"] + stats["tg_ok"]
        bar_n = int(round(total_ok / max(1, total_target) * 14))
        bar = "▰" * bar_n + "▱" * (14 - bar_n)
        pct = int(total_ok / max(1, total_target) * 100)
        elapsed = int(now - t0)
        txt = (
            f"⌁ <b>Report — Sending</b>\n\n"
            f"  {bar}  <b>{pct}%</b>\n\n"
            f"  Target  : <b>{html.escape(name[:25])}</b>\n"
            f"  Alasan  : {html.escape(reason_label)}\n\n"
            f"  <b>Method 1</b> (Email)\n"
            f"  Sent <b>{stats['ok']}</b>  ·  "
            f"Fail <b>{stats['fail']}</b>  ·  "
            f"Retry <b>{stats['retries']}</b>\n\n"
            f"  <b>Method 2</b> (telegram.org)\n"
            f"  OK <b>{stats['tg_ok']}</b>  ·  "
            f"Fail <b>{stats['tg_fail']}</b>  ·  "
            f"Total <b>{TG_SUPPORT_TIMES}</b>\n\n"
            f"  Elapsed <b>{elapsed}s</b>"
        )
        try:
            await msg.edit_text(
                _screen("REPORT", txt),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    sem = asyncio.Semaphore(REPORT_CMD_WORKERS)

    async def _do_send_with_retry(sender, body, subject):
        async with sem:
            for attempt in range(1, REPORT_CMD_MAX_RETRY + 1):
                ok = await loop.run_in_executor(
                    pool, functools.partial(_send_one, sender, body, subject))
                if ok:
                    return True
                if attempt < REPORT_CMD_MAX_RETRY:
                    stats["retries"] += 1
                    await asyncio.sleep(2)
            return False

    async def _do_sender(sender):
        ok1 = await _do_send_with_retry(sender, body1, subject1)
        if ok1:
            stats["ok"] += 1
        else:
            stats["fail"] += 1
        stats["done"] += 1
        await _progress()
        ok2 = await _do_send_with_retry(sender, body2, subject2)
        if ok2:
            stats["ok"] += 1
        else:
            stats["fail"] += 1
        stats["done"] += 1
        await _progress()

    # ── Method 2: telegram.org/support ──
    async def _run_tg_support():
        """POST to telegram.org/support TG_SUPPORT_TIMES times with random identity."""
        key = f"{ttype}_{reason}"
        msgs = _TG_SUPPORT_MESSAGES.get(key) or _TG_SUPPORT_MESSAGES.get("user_other")
        for i in range(TG_SUPPORT_TIMES):
            msg_text = random.choice(msgs).replace("{info}", tg_info_str)
            rname = _random_us_name()
            rphone = _random_us_phone()
            remail = _random_email_addr()
            try:
                ok = await loop.run_in_executor(
                    pool, functools.partial(_submit_tg_support, msg_text, rname, remail, rphone)
                )
                if ok:
                    stats["tg_ok"] += 1
                else:
                    stats["tg_fail"] += 1
            except Exception:
                stats["tg_fail"] += 1
            await _progress()
            if i < TG_SUPPORT_TIMES - 1:
                await asyncio.sleep(random.uniform(2, 5))

    # Run both methods concurrently
    await _progress(force=True)
    await asyncio.gather(
        asyncio.gather(*[_do_sender(s) for s in senders]),
        _run_tg_support(),
    )
    pool.shutdown(wait=False)

    # Final summary
    elapsed = int(time.time() - t0)
    total_ok = stats["ok"] + stats["tg_ok"]
    total_target = REPORT_CMD_TARGET_SENT + TG_SUPPORT_TIMES
    achieved = total_ok >= total_target * 0.5
    status_icon = "✓" if achieved else "⚠"
    block_display = _build_account_block(info)

    final_text = (
        f"⌁ <b>Report — Done {status_icon}</b>\n\n"
        f"<blockquote>{html.escape(block_display)}</blockquote>\n\n"
        f"  Alasan  : <b>{html.escape(reason_label)}</b>\n\n"
        f"  <b>Method 1</b> (Email)\n"
        f"  Sent    : <b>{stats['ok']}/{REPORT_CMD_TARGET_SENT}</b>\n"
        f"  Fail    : <b>{stats['fail']}</b>\n"
        f"  Retry   : <b>{stats['retries']}</b>\n"
        f"  Alamat  : <b>{len(REPORT_CMD_EMAILS)}</b>\n\n"
        f"  <b>Method 2</b> (telegram.org/support)\n"
        f"  OK      : <b>{stats['tg_ok']}/{TG_SUPPORT_TIMES}</b>\n"
        f"  Fail    : <b>{stats['tg_fail']}</b>\n\n"
        f"  Time    : <b>{elapsed}s</b>"
    )

    await msg.edit_text(
        _screen("REPORT", final_text),
        parse_mode=ParseMode.HTML,
    )

    # Cleanup session
    _REPORT_SESSIONS.pop(user_id, None)

