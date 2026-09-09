"""
Nokos Server 1 — opxotp.vercel.app Integration
================================================
For dik.py bot integration.

Commands:
- /nokos <prefix> <count> — get phone numbers
- /trafic — show top active services/countries

API Endpoints:
- POST /api/proxy?endpoint=getnum  body: {"rid": "prefix"}
- GET  /api/proxy?endpoint=console
- GET  /api/proxy?endpoint=success-otp
"""
import asyncio
import httpx
import re
import json
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, List
from collections import Counter

log = logging.getLogger("nokos_s1")

BASE_URL = "https://opxotp.vercel.app/api/proxy"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://opxotp.vercel.app",
    "Referer": "https://opxotp.vercel.app/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# Country code mapping (prefix -> (name, flag, alpha2))
COUNTRY_CODES = {
    '1': ('USA/Canada', '🇺🇸', 'US'), '7': ('Russia', '🇷🇺', 'RU'),
    '20': ('Egypt', '🇪🇬', 'EG'), '27': ('South Africa', '🇿🇦', 'ZA'),
    '30': ('Greece', '🇬🇷', 'GR'), '31': ('Netherlands', '🇳🇱', 'NL'),
    '33': ('France', '🇫🇷', 'FR'), '34': ('Spain', '🇪🇸', 'ES'),
    '39': ('Italy', '🇮🇹', 'IT'), '44': ('UK', '🇬🇧', 'GB'),
    '49': ('Germany', '🇩🇪', 'DE'), '55': ('Brazil', '🇧🇷', 'BR'),
    '60': ('Malaysia', '🇲🇾', 'MY'), '62': ('Indonesia', '🇮🇩', 'ID'),
    '63': ('Philippines', '🇵🇭', 'PH'), '65': ('Singapore', '🇸🇬', 'SG'),
    '66': ('Thailand', '🇹🇭', 'TH'), '77': ('Kazakhstan', '🇰🇿', 'KZ'),
    '81': ('Japan', '🇯🇵', 'JP'), '82': ('South Korea', '🇰🇷', 'KR'),
    '84': ('Vietnam', '🇻🇳', 'VN'), '86': ('China', '🇨🇳', 'CN'),
    '90': ('Turkey', '🇹🇷', 'TR'), '91': ('India', '🇮🇳', 'IN'),
    '92': ('Pakistan', '🇵🇰', 'PK'), '95': ('Myanmar', '🇲🇲', 'MM'),
    '98': ('Iran', '🇮🇷', 'IR'), '212': ('Morocco', '🇲🇦', 'MA'),
    '213': ('Algeria', '🇩🇿', 'DZ'), '221': ('Senegal', '🇸🇳', 'SN'),
    '225': ("Côte d'Ivoire", '🇨🇮', 'CI'), '233': ('Ghana', '🇬🇭', 'GH'),
    '234': ('Nigeria', '🇳🇬', 'NG'), '254': ('Kenya', '🇰🇪', 'KE'),
    '255': ('Tanzania', '🇹🇿', 'TZ'), '256': ('Uganda', '🇺🇬', 'UG'),
    '992': ('Tajikistan', '🇹🇯', 'TJ'), '993': ('Turkmenistan', '🇹🇲', 'TM'),
    '994': ('Azerbaijan', '🇦🇿', 'AZ'), '996': ('Kyrgyzstan', '🇰🇬', 'KG'),
    '998': ('Uzbekistan', '🇺🇿', 'UZ'), '236': ('CAR', '🇨🇫', 'CF'),
    '237': ('Cameroon', '🇨🇲', 'CM'), '380': ('Ukraine', '🇺🇦', 'UA'),
}


def get_country_info(phone: str) -> tuple:
    """Get (name, flag, alpha2) from phone number."""
    clean = re.sub(r'[^\d]', '', phone)
    for i in range(4, 0, -1):
        prefix = clean[:i]
        if prefix in COUNTRY_CODES:
            return COUNTRY_CODES[prefix]
    return ('Unknown', '🌎', 'UN')


def detect_service(message: str, sender: str = "") -> str:
    """Detect service from message/sender."""
    full = f"{sender} {message}".upper()
    if "WHATSAPP" in full or "WA CODE" in full: return "WhatsApp"
    if "TELEGRAM" in full or "TG CODE" in full: return "Telegram"
    if "FACEBOOK" in full or "FB CODE" in full or "META" in full: return "Facebook"
    if "INSTAGRAM" in full or "IG CODE" in full: return "Instagram"
    if "GOOGLE" in full or "G-" in full: return "Google"
    if "TIKTOK" in full: return "TikTok"
    if "TWITTER" in full or "X CODE" in full: return "Twitter"
    if "DISCORD" in full: return "Discord"
    if "SNAPCHAT" in full: return "Snapchat"
    return sender[:10] if sender else "Other"


@dataclass
class NokosNumber:
    """A single allocated number."""
    full_number: str
    national_number: str
    no_plus: str
    country: str
    operator: str
    rid: str


@dataclass
class TrafficItem:
    """A single traffic/console hit."""
    message: str
    range: str
    sid: str
    time: int
    service: str = ""
    country: str = ""
    alpha2: str = ""
    flag: str = ""


async def get_numbers(prefix: str, count: int = 10) -> List[NokosNumber]:
    """Allocate phone numbers by prefix from opxotp API."""
    numbers = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(count):
            try:
                resp = await client.post(
                    f"{BASE_URL}?endpoint=getnum",
                    json={"rid": prefix},
                    headers=HEADERS,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("meta", {}).get("code") == 200:
                        d = data["data"]
                        numbers.append(NokosNumber(
                            full_number=d["full_number"],
                            national_number=d["national_number"],
                            no_plus=d["no_plus_number"],
                            country=d["country"],
                            operator=d.get("operator", ""),
                            rid=data.get("rid", ""),
                        ))
            except Exception as e:
                log.error(f"getnum error: {e}")
            await asyncio.sleep(0.05)
    return numbers


async def get_traffic() -> List[TrafficItem]:
    """Get live traffic from console endpoint."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(f"{BASE_URL}?endpoint=console", headers=HEADERS)
            if resp.status_code == 200:
                data = resp.json()
                hits = data.get("data", {}).get("hits", [])
                items = []
                for h in hits:
                    msg = h.get("message", "")
                    rng = h.get("range", "")
                    sid = h.get("sid", "")
                    t = h.get("time", 0)
                    svc = detect_service(msg, sid)
                    name, flag, alpha2 = get_country_info(rng.replace("X", "0"))
                    items.append(TrafficItem(
                        message=msg, range=rng, sid=sid, time=t,
                        service=svc, country=name, alpha2=alpha2, flag=flag,
                    ))
                return items
        except Exception as e:
            log.error(f"console error: {e}")
    return []


async def get_success_otps() -> list:
    """Get successful OTPs."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(f"{BASE_URL}?endpoint=success-otp", headers=HEADERS)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("data", {}).get("otps", [])
        except Exception as e:
            log.error(f"success-otp error: {e}")
    return []
