"""
Browser fingerprint untuk request registrasi.

Tujuan: request tidak selalu terlihat berasal dari satu browser/OS/bahasa yang sama.
Setiap profil membawa User-Agent + Client Hints (sec-ch-ua*) yang KONSISTEN — kalau
UA-nya Android, maka `sec-ch-ua-mobile: ?1` dan platform `"Android"`. Header yang
saling bertabrakan (UA Android tapi platform Windows) justru lebih mencurigakan
daripada tidak mengirim client hints sama sekali.

Accept-Language dipilih sesuai negara identitas supaya konsisten.

API:
    random_profile(mobile_ratio=0.5) -> dict
    random_user_agent() -> str
    accept_language_for(locale_code) -> str
    build_headers(locale_code, profile=None, ajax=False, referer=None) -> dict
"""
import random

# ── Profil Android (mayoritas, sesuai permintaan) ────────────────────────────
ANDROID_PROFILES = [
    {
        "ua": "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.104 Mobile Safari/537.36",
        "brand": "Chromium", "version": "131", "platform": "Android", "mobile": True,
    },
    {
        "ua": "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.107 Mobile Safari/537.36",
        "brand": "Chromium", "version": "130", "platform": "Android", "mobile": True,
    },
    {
        "ua": "Mozilla/5.0 (Linux; Android 13; SM-A536E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.146 Mobile Safari/537.36",
        "brand": "Chromium", "version": "128", "platform": "Android", "mobile": True,
    },
    {
        "ua": "Mozilla/5.0 (Linux; Android 14; V2352) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.100 Mobile Safari/537.36",
        "brand": "Chromium", "version": "129", "platform": "Android", "mobile": True,
    },
    {
        "ua": "Mozilla/5.0 (Linux; Android 13; RMX3750) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.103 Mobile Safari/537.36",
        "brand": "Chromium", "version": "127", "platform": "Android", "mobile": True,
    },
    {
        "ua": "Mozilla/5.0 (Linux; Android 14; CPH2557) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.79 Mobile Safari/537.36",
        "brand": "Chromium", "version": "132", "platform": "Android", "mobile": True,
    },
    {
        "ua": "Mozilla/5.0 (Linux; Android 12; Redmi Note 11) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.122 Mobile Safari/537.36",
        "brand": "Chromium", "version": "126", "platform": "Android", "mobile": True,
    },
    {
        "ua": "Mozilla/5.0 (Linux; Android 14; 23021RAAEG) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.81 Mobile Safari/537.36",
        "brand": "Chromium", "version": "131", "platform": "Android", "mobile": True,
    },
    {
        "ua": "Mozilla/5.0 (Linux; Android 13; SM-M146B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.165 Mobile Safari/537.36",
        "brand": "Chromium", "version": "125", "platform": "Android", "mobile": True,
    },
    {
        "ua": "Mozilla/5.0 (Linux; Android 14; Infinix X6871) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.86 Mobile Safari/537.36",
        "brand": "Chromium", "version": "130", "platform": "Android", "mobile": True,
    },
]

# ── Profil desktop (untuk variasi, porsi lebih kecil) ────────────────────────
DESKTOP_PROFILES = [
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "brand": "Chromium", "version": "131", "platform": "Windows", "mobile": False,
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "brand": "Chromium", "version": "130", "platform": "Windows", "mobile": False,
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "brand": "Chromium", "version": "129", "platform": "macOS", "mobile": False,
    },
    {
        "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "brand": "Chromium", "version": "128", "platform": "Linux", "mobile": False,
    },
]

# Kompat lama: daftar UA datar
USER_AGENTS = [p["ua"] for p in ANDROID_PROFILES + DESKTOP_PROFILES]

ACCEPT_LANGUAGES = {
    "ID": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "US": "en-US,en;q=0.9",
    "GB": "en-GB,en;q=0.9",
    "IN": "en-IN,en;q=0.9,hi;q=0.8",
    "MY": "ms-MY,ms;q=0.9,en-US;q=0.8,en;q=0.7",
    "SG": "en-SG,en;q=0.9,zh-CN;q=0.8",
    "AU": "en-AU,en;q=0.9",
    "DE": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "CA": "en-CA,en;q=0.9,fr-CA;q=0.8",
    "PH": "en-PH,en;q=0.9,fil;q=0.8",
    "NL": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "BR": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

_ACCEPT_HTML = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7")


def random_profile(mobile_ratio=0.75):
    """Pilih satu profil browser. Default condong ke Android (75%)."""
    if random.random() < mobile_ratio:
        return dict(random.choice(ANDROID_PROFILES))
    return dict(random.choice(DESKTOP_PROFILES))


def random_user_agent(mobile_ratio=0.75):
    return random_profile(mobile_ratio)["ua"]


def accept_language_for(locale_code):
    return ACCEPT_LANGUAGES.get(locale_code, "en-US,en;q=0.9")


def build_headers(locale_code, profile=None, ajax=False, referer=None):
    """
    Bangun header lengkap & konsisten untuk satu profil browser.

    ajax=True  -> header untuk request XMLHttpRequest (mis. addToCart)
    ajax=False -> header navigasi dokumen biasa (mis. submit form)
    """
    p = profile or random_profile()
    ver = p["version"]
    headers = {
        "User-Agent": p["ua"],
        "Accept-Language": accept_language_for(locale_code),
        "Accept-Encoding": "gzip, deflate, br",
        "sec-ch-ua": f'"Not;A=Brand";v="8", "{p["brand"]}";v="{ver}", "Google Chrome";v="{ver}"',
        "sec-ch-ua-mobile": "?1" if p["mobile"] else "?0",
        "sec-ch-ua-platform": f'"{p["platform"]}"',
    }
    if ajax:
        headers.update({
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        })
    else:
        headers.update({
            "Accept": _ACCEPT_HTML,
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
            "Sec-Fetch-Dest": "document",
        })
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = "https://" + referer.split("/")[2]
    return headers
