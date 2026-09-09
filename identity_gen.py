"""
Identity Generator — data akun random & realistis untuk form registrasi.

Dipakai oleh dewabiz_fix_module.py (dan modul lain) supaya setiap akun yang dibuat
punya nama manusia, alamat, kota/state/postcode yang konsisten dengan negaranya,
nomor telepon sesuai format lokal, serta domain email yang bervariasi.

Kenapa multi-locale: kalau semua akun pakai 1 lokasi/1 pola yang sama (mis. selalu
"Dika Preset", Maros, Sulawesi Tengah, @gmail.com), pola itu gampang terdeteksi dan
pendaftaran bisa mulai ditolak. Generator ini menyebar identitas ke banyak negara.

API utama:
    generate_identity(locale=None) -> dict
    random_password(name=None) -> str
    random_pin(6) -> str
"""
import random
import string
import unicodedata

# ── Domain email ──────────────────────────────────────────────────────────────
EMAIL_DOMAINS = ["gmail.com"]

# ── Nama depan per-locale (campur pria & wanita) ────────────────────────────
FIRST_NAMES = {
    "ID": ["Andi", "Budi", "Citra", "Dewi", "Eka", "Fajar", "Gita", "Hendra",
           "Indah", "Joko", "Kartika", "Lestari", "Maya", "Nanda", "Putri",
           "Rizki", "Sari", "Taufik", "Wahyu", "Yuni", "Bagus", "Ratna"],
    "US": ["James", "Mary", "Robert", "Jennifer", "Michael", "Linda", "David",
           "Sarah", "William", "Jessica", "Ethan", "Olivia", "Daniel", "Emily",
           "Matthew", "Ashley", "Joshua", "Amanda", "Tyler", "Megan"],
    "GB": ["Oliver", "Amelia", "Harry", "Isla", "George", "Ava", "Noah",
           "Emily", "Jack", "Sophie", "Charlie", "Grace", "Thomas", "Freya",
           "Oscar", "Poppy", "William", "Ruby", "Henry", "Chloe"],
    "IN": ["Aarav", "Ananya", "Vivaan", "Diya", "Aditya", "Saanvi", "Vihaan",
           "Ishita", "Arjun", "Kavya", "Rohan", "Meera", "Karthik", "Priya",
           "Rahul", "Neha", "Siddharth", "Pooja", "Manish", "Divya"],
    "MY": ["Aiman", "Nurul", "Haziq", "Farah", "Zulkifli", "Aisyah", "Danial",
           "Syafiqah", "Iskandar", "Balqis", "Hafiz", "Alia", "Amirul", "Liyana"],
    "SG": ["Wei", "Xin", "Jun", "Mei", "Kai", "Ying", "Zhi", "Ling",
           "Aravind", "Shanti", "Ryan", "Chloe", "Marcus", "Rachel"],
    "AU": ["Jack", "Charlotte", "Liam", "Mia", "Cooper", "Zoe", "Lachlan",
           "Ella", "Riley", "Sienna", "Hunter", "Layla", "Archie", "Harper"],
    "DE": ["Lukas", "Hanna", "Felix", "Emma", "Jonas", "Mia", "Leon", "Lena",
           "Paul", "Sophie", "Maximilian", "Marie", "Elias", "Laura"],
    "CA": ["Liam", "Emma", "Noah", "Olivia", "Lucas", "Ava", "Benjamin",
           "Sophia", "Ethan", "Charlotte", "Nathan", "Zoe", "Owen", "Leah"],
    "PH": ["Juan", "Maria", "Jose", "Ana", "Mark", "Grace", "Paulo", "Angel",
           "Carlo", "Jasmine", "Miguel", "Trisha", "Rico", "Bea"],
    "NL": ["Daan", "Emma", "Sem", "Julia", "Lucas", "Sophie", "Milan", "Anna",
           "Levi", "Tess", "Bram", "Lotte", "Thijs", "Eva"],
    "BR": ["Miguel", "Alice", "Arthur", "Sophia", "Gabriel", "Julia", "Pedro",
           "Isabella", "Lucas", "Manuela", "Matheus", "Laura", "Rafael", "Beatriz"],
}

# ── Nama belakang per-locale ────────────────────────────────────────────────
LAST_NAMES = {
    "ID": ["Santoso", "Wijaya", "Pratama", "Nugroho", "Kusuma", "Hidayat",
           "Saputra", "Permana", "Wibowo", "Maulana", "Firmansyah", "Rahayu",
           "Setiawan", "Halim", "Anggraini", "Purnomo"],
    "US": ["Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis",
           "Wilson", "Anderson", "Taylor", "Thomas", "Moore", "Jackson",
           "Martin", "Lee", "Clark", "Lewis", "Walker"],
    "GB": ["Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Johnson",
           "Davies", "Patel", "Robinson", "Wright", "Thompson", "Evans",
           "Walker", "White", "Roberts", "Green", "Hall"],
    "IN": ["Sharma", "Verma", "Patel", "Reddy", "Nair", "Iyer", "Singh",
           "Gupta", "Mehta", "Kapoor", "Chopra", "Joshi", "Desai", "Rao",
           "Malhotra", "Bhat"],
    "MY": ["Abdullah", "Ismail", "Rahman", "Hassan", "Yusof", "Ibrahim",
           "Salleh", "Omar", "Zainal", "Aziz", "Karim", "Bakar"],
    "SG": ["Tan", "Lim", "Lee", "Ng", "Wong", "Chan", "Koh", "Teo", "Goh",
           "Ong", "Chua", "Sim", "Kumar", "Rajan"],
    "AU": ["Smith", "Jones", "Williams", "Brown", "Wilson", "Taylor",
           "Nguyen", "Martin", "White", "Anderson", "Thompson", "Walker"],
    "DE": ["Muller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer",
           "Wagner", "Becker", "Schulz", "Hoffmann", "Koch", "Richter"],
    "CA": ["Smith", "Brown", "Tremblay", "Martin", "Roy", "Wilson", "Gagnon",
           "Johnson", "MacDonald", "Taylor", "Campbell", "Anderson"],
    "PH": ["Santos", "Reyes", "Cruz", "Bautista", "Ocampo", "Garcia",
           "Mendoza", "Torres", "Ramos", "Aquino", "Villanueva", "Castillo"],
    "NL": ["De Jong", "Jansen", "De Vries", "Van Dijk", "Bakker", "Visser",
           "Smit", "Meijer", "Mulder", "De Boer", "Bos", "Vos"],
    "BR": ["Silva", "Santos", "Oliveira", "Souza", "Lima", "Pereira",
           "Costa", "Rodrigues", "Almeida", "Nascimento", "Carvalho", "Gomes"],
}

# ── Nama jalan per-locale ───────────────────────────────────────────────────
STREETS = {
    "ID": ["Jl. Melati", "Jl. Kenanga", "Jl. Merdeka", "Jl. Diponegoro",
           "Jl. Sudirman", "Jl. Cendrawasih", "Jl. Anggrek", "Jl. Pahlawan"],
    "US": ["Main St", "Oak Ave", "Maple Dr", "Cedar Ln", "Pine St",
           "Elm St", "Washington Ave", "Lakeview Dr"],
    "GB": ["High Street", "Station Road", "Church Lane", "Victoria Road",
           "Green Lane", "Mill Lane", "Kings Road", "Queens Avenue"],
    "IN": ["MG Road", "Nehru Street", "Gandhi Nagar", "Park Road",
           "Station Road", "Temple Street", "Lake View Road", "Ring Road"],
    "MY": ["Jalan Ampang", "Jalan Bukit Bintang", "Jalan Tun Razak",
           "Lebuh Pasar", "Jalan Kenari", "Persiaran Surian"],
    "SG": ["Orchard Road", "Bedok North Ave", "Tampines Street",
           "Jurong West Ave", "Serangoon Central", "Clementi Road"],
    "AU": ["George St", "Collins St", "Queen St", "Beach Rd",
           "Park Ave", "Victoria St", "Bourke St", "Hay St"],
    "DE": ["Hauptstrasse", "Bahnhofstrasse", "Schulstrasse", "Gartenweg",
           "Lindenallee", "Ringstrasse", "Bergstrasse", "Am Markt"],
    "CA": ["Yonge St", "King St", "Queen St W", "Maple Ave",
           "Bay St", "Lakeshore Rd", "Elgin St", "Portage Ave"],
    "PH": ["Rizal Street", "Bonifacio Ave", "Mabini Street", "Quezon Ave",
           "Del Pilar Street", "Aguinaldo Road", "Magsaysay Blvd"],
    "NL": ["Kerkstraat", "Dorpsstraat", "Molenweg", "Stationsweg",
           "Schoolstraat", "Julianalaan", "Beatrixstraat"],
    "BR": ["Rua das Flores", "Avenida Paulista", "Rua do Comercio",
           "Avenida Brasil", "Rua Sao Jose", "Travessa Central"],
}

# ── Kota + state/provinsi + pola postcode + kode telepon per-locale ─────────
# postcode: "D" akan diganti digit random. phone_prefixes: awalan nomor lokal.
LOCALES = {
    "ID": {
        "calling_code": "62",
        "postcode": "DDDDD",
        "phone_prefixes": ["811", "812", "813", "821", "852", "857", "858", "877", "895"],
        "phone_len": 11,
        "cities": [
            ("Jakarta Selatan", "DKI Jakarta"), ("Bandung", "Jawa Barat"),
            ("Surabaya", "Jawa Timur"), ("Semarang", "Jawa Tengah"),
            ("Medan", "Sumatera Utara"), ("Makassar", "Sulawesi Selatan"),
            ("Denpasar", "Bali"), ("Yogyakarta", "DI Yogyakarta"),
            ("Palembang", "Sumatera Selatan"), ("Balikpapan", "Kalimantan Timur"),
            ("Pekanbaru", "Riau"), ("Malang", "Jawa Timur"),
        ],
    },
    "US": {
        "calling_code": "1",
        "postcode": "DDDDD",
        "phone_prefixes": ["212", "310", "312", "404", "415", "512", "617", "702", "917"],
        "phone_len": 10,
        "cities": [
            ("New York", "New York"), ("Los Angeles", "California"),
            ("Chicago", "Illinois"), ("Houston", "Texas"),
            ("Phoenix", "Arizona"), ("Philadelphia", "Pennsylvania"),
            ("Seattle", "Washington"), ("Denver", "Colorado"),
            ("Atlanta", "Georgia"), ("Boston", "Massachusetts"),
            ("Miami", "Florida"), ("Portland", "Oregon"),
        ],
    },
    "GB": {
        "calling_code": "44",
        "postcode": "GB",  # ditangani khusus
        "phone_prefixes": ["7400", "7500", "7700", "7800", "7900"],
        "phone_len": 10,
        "cities": [
            ("London", "Greater London"), ("Manchester", "Greater Manchester"),
            ("Birmingham", "West Midlands"), ("Leeds", "West Yorkshire"),
            ("Liverpool", "Merseyside"), ("Bristol", "Bristol"),
            ("Sheffield", "South Yorkshire"), ("Newcastle", "Tyne and Wear"),
            ("Nottingham", "Nottinghamshire"), ("Brighton", "East Sussex"),
        ],
    },
    "IN": {
        "calling_code": "91",
        "postcode": "DDDDDD",
        "phone_prefixes": ["70", "80", "81", "88", "90", "93", "97", "98", "99"],
        "phone_len": 10,
        "cities": [
            ("Mumbai", "Maharashtra"), ("Delhi", "Delhi"),
            ("Bengaluru", "Karnataka"), ("Chennai", "Tamil Nadu"),
            ("Hyderabad", "Telangana"), ("Pune", "Maharashtra"),
            ("Kolkata", "West Bengal"), ("Ahmedabad", "Gujarat"),
            ("Jaipur", "Rajasthan"), ("Kochi", "Kerala"),
        ],
    },
    "MY": {
        "calling_code": "60",
        "postcode": "DDDDD",
        "phone_prefixes": ["11", "12", "13", "14", "16", "17", "18", "19"],
        "phone_len": 9,
        "cities": [
            ("Kuala Lumpur", "Kuala Lumpur"), ("Petaling Jaya", "Selangor"),
            ("Johor Bahru", "Johor"), ("George Town", "Penang"),
            ("Ipoh", "Perak"), ("Kota Kinabalu", "Sabah"),
            ("Kuching", "Sarawak"), ("Melaka", "Melaka"),
        ],
    },
    "SG": {
        "calling_code": "65",
        "postcode": "DDDDDD",
        "phone_prefixes": ["8", "9"],
        "phone_len": 8,
        "cities": [
            ("Singapore", "Central"), ("Jurong", "West"),
            ("Tampines", "East"), ("Woodlands", "North"),
            ("Bedok", "East"), ("Clementi", "West"),
        ],
    },
    "AU": {
        "calling_code": "61",
        "postcode": "DDDD",
        "phone_prefixes": ["4"],
        "phone_len": 9,
        "cities": [
            ("Sydney", "New South Wales"), ("Melbourne", "Victoria"),
            ("Brisbane", "Queensland"), ("Perth", "Western Australia"),
            ("Adelaide", "South Australia"), ("Hobart", "Tasmania"),
            ("Canberra", "Australian Capital Territory"), ("Darwin", "Northern Territory"),
        ],
    },
    "DE": {
        "calling_code": "49",
        "postcode": "DDDDD",
        "phone_prefixes": ["151", "152", "157", "160", "170", "171", "176"],
        "phone_len": 10,
        "cities": [
            ("Berlin", "Berlin"), ("Munich", "Bavaria"),
            ("Hamburg", "Hamburg"), ("Cologne", "North Rhine-Westphalia"),
            ("Frankfurt", "Hesse"), ("Stuttgart", "Baden-Wurttemberg"),
            ("Dresden", "Saxony"), ("Bremen", "Bremen"),
        ],
    },
    "CA": {
        "calling_code": "1",
        "postcode": "CA",  # ditangani khusus
        "phone_prefixes": ["416", "437", "604", "778", "514", "613", "902"],
        "phone_len": 10,
        "cities": [
            ("Toronto", "Ontario"), ("Vancouver", "British Columbia"),
            ("Montreal", "Quebec"), ("Calgary", "Alberta"),
            ("Ottawa", "Ontario"), ("Edmonton", "Alberta"),
            ("Winnipeg", "Manitoba"), ("Halifax", "Nova Scotia"),
        ],
    },
    "PH": {
        "calling_code": "63",
        "postcode": "DDDD",
        "phone_prefixes": ["905", "906", "915", "917", "926", "935", "945", "995"],
        "phone_len": 10,
        "cities": [
            ("Manila", "Metro Manila"), ("Quezon City", "Metro Manila"),
            ("Cebu City", "Cebu"), ("Davao City", "Davao del Sur"),
            ("Makati", "Metro Manila"), ("Iloilo City", "Iloilo"),
            ("Baguio", "Benguet"), ("Cagayan de Oro", "Misamis Oriental"),
        ],
    },
    "NL": {
        "calling_code": "31",
        "postcode": "NL",  # ditangani khusus
        "phone_prefixes": ["6"],
        "phone_len": 9,
        "cities": [
            ("Amsterdam", "North Holland"), ("Rotterdam", "South Holland"),
            ("The Hague", "South Holland"), ("Utrecht", "Utrecht"),
            ("Eindhoven", "North Brabant"), ("Groningen", "Groningen"),
            ("Tilburg", "North Brabant"), ("Almere", "Flevoland"),
        ],
    },
    "BR": {
        "calling_code": "55",
        "postcode": "DDDDDDDD",
        "phone_prefixes": ["11", "21", "31", "41", "51", "61", "71", "81"],
        "phone_len": 11,
        "cities": [
            ("Sao Paulo", "Sao Paulo"), ("Rio de Janeiro", "Rio de Janeiro"),
            ("Brasilia", "Distrito Federal"), ("Salvador", "Bahia"),
            ("Belo Horizonte", "Minas Gerais"), ("Curitiba", "Parana"),
            ("Recife", "Pernambuco"), ("Porto Alegre", "Rio Grande do Sul"),
        ],
    },
}

LOCALE_CODES = list(LOCALES.keys())

_ASCII_LETTERS = string.ascii_lowercase
_UPPER = string.ascii_uppercase


def _ascii_slug(text):
    """Ubah nama jadi slug ASCII lowercase (buang aksen/spasi/tanda baca)."""
    norm = unicodedata.normalize("NFKD", text)
    out = "".join(c for c in norm if not unicodedata.combining(c))
    return "".join(c for c in out.lower() if c in _ASCII_LETTERS or c.isdigit())


def random_digits(length=6):
    return "".join(random.choices(string.digits, k=length))


def random_string(length=8):
    return "".join(random.choices(_ASCII_LETTERS + string.digits, k=length))


def _gen_postcode(pattern):
    """Bangun postcode sesuai pola negara."""
    if pattern == "GB":
        # Contoh: M1 4BT / SW19 3AB
        area = "".join(random.choices("ABDEFGHKLMNPRSTWY", k=random.choice([1, 2])))
        district = str(random.randint(1, 30))
        unit = "".join(random.choices("ABDEFGHJLNPQRSTUWXYZ", k=2))
        return f"{area}{district} {random.randint(0, 9)}{unit}"
    if pattern == "CA":
        # Contoh: M5V 3L9 (Kanada tidak memakai D, F, I, O, Q, U)
        letters = "ABCEGHJKLMNPRSTVXY"
        return (f"{random.choice(letters)}{random.randint(0,9)}{random.choice(letters)} "
                f"{random.randint(0,9)}{random.choice(letters)}{random.randint(0,9)}")
    if pattern == "NL":
        # Contoh: 1012 AB
        return f"{random.randint(1000, 9999)} {''.join(random.choices(_UPPER, k=2))}"
    return "".join(random.choice(string.digits) for _ in pattern)


def _gen_phone(cfg):
    """
    Nomor telepon lokal (tanpa kode negara).

    `phone_len` = total digit nomor nasional (termasuk prefix), supaya panjangnya
    realistis per negara. Prefix jadi grup pertama, sisanya dibelah dua rata.
    """
    prefix = random.choice(cfg["phone_prefixes"])
    rest = random_digits(max(0, cfg["phone_len"] - len(prefix)))
    if len(rest) <= 4:
        return f"{prefix}-{rest}" if rest else prefix
    half = (len(rest) + 1) // 2
    return f"{prefix}-{rest[:half]}-{rest[half:]}"


def random_password(name=None):
    """Password kuat & random: huruf besar/kecil, digit, simbol. Tidak berpola tetap."""
    base = (_ascii_slug(name)[:6].capitalize() if name else
            "".join(random.choices(_ASCII_LETTERS, k=5)).capitalize())
    mid = "".join(random.choices(_ASCII_LETTERS, k=random.randint(2, 4)))
    digits = random_digits(random.randint(3, 4))
    symbol = random.choice("!@#$%&*?")
    tail = [mid, digits, symbol]
    random.shuffle(tail)  # urutan bagian belakang diacak; base tetap di depan
    pwd = base + "".join(tail)
    # Jaminan komposisi
    if not any(c.isupper() for c in pwd):
        pwd = pwd.capitalize()
    if not any(c.isdigit() for c in pwd):
        pwd += random_digits(2)
    if not any(c in "!@#$%&*?" for c in pwd):
        pwd += random.choice("!@#$%&*?")
    return pwd


def random_pin(length=6):
    return random_digits(length)


def _gen_email(first, last):
    """Email dengan pola & domain bervariasi (bukan selalu gmail)."""
    f = _ascii_slug(first) or random_string(5)
    l = _ascii_slug(last) or random_string(5)
    num = random.randint(1, 9999)
    sep = random.choice(["", ".", "_", "-"])
    patterns = [
        f"{f}{sep}{l}{num}",
        f"{f}{sep}{l}",
        f"{f[0]}{sep}{l}{num}",
        f"{f}{sep}{l[0]}{num}",
        f"{f}{num}",
        f"{l}{sep}{f}{num}",
        f"{f}{sep}{l}{random.choice(['x', 'z', 'q', 'id', 'official'])}{num}",
    ]
    local = random.choice(patterns)
    # Local-part email tidak boleh diawali/diakhiri titik
    local = local.strip(".").replace("..", ".")
    return f"{local}@{random.choice(EMAIL_DOMAINS)}"


def generate_identity(locale=None):
    """
    Hasilkan satu identitas lengkap & konsisten (nama, email, alamat, telepon).

    Args:
        locale: kode negara 2 huruf (mis. "ID", "US", "GB"). Kalau None → random.

    Returns dict:
        locale, country, firstname, lastname, fullname, email, password, pin,
        phone, calling_code, address1, address2, city, state, postcode
    """
    code = locale if locale in LOCALES else random.choice(LOCALE_CODES)
    cfg = LOCALES[code]

    first = random.choice(FIRST_NAMES[code])
    last = random.choice(LAST_NAMES[code])
    city, state = random.choice(cfg["cities"])
    street = random.choice(STREETS[code])

    # Format alamat mengikuti kebiasaan negara
    if code == "ID":
        address1 = f"{street} No. {random.randint(1, 250)}"
    elif code in ("GB", "US", "CA", "AU", "SG", "MY", "IN", "PH"):
        address1 = f"{random.randint(1, 999)} {street}"
    else:
        address1 = f"{street} {random.randint(1, 199)}"

    address2 = ""
    if random.random() < 0.30:  # kadang isi baris kedua, biar tidak selalu kosong
        address2 = random.choice([
            f"Apt {random.randint(1, 40)}{random.choice('ABCD')}",
            f"Unit {random.randint(1, 60)}",
            f"Suite {random.randint(100, 900)}",
            f"Blok {random.choice('ABCDEF')}{random.randint(1, 30)}",
            f"Floor {random.randint(2, 25)}",
        ])

    return {
        "locale": code,
        "country": code,
        "firstname": first,
        "lastname": last,
        "fullname": f"{first} {last}",
        "email": _gen_email(first, last),
        "password": random_password(first),
        "pin": random_pin(6),
        "phone": _gen_phone(cfg),
        "calling_code": cfg["calling_code"],
        "address1": address1,
        "address2": address2,
        "city": city,
        "state": state,
        "postcode": _gen_postcode(cfg["postcode"]),
    }


if __name__ == "__main__":
    for _ in range(8):
        idn = generate_identity()
        print(f"[{idn['locale']}] {idn['fullname']:<24} {idn['email']:<38} "
              f"+{idn['calling_code']} {idn['phone']:<16} "
              f"{idn['address1']}, {idn['city']}, {idn['state']} {idn['postcode']} "
              f"| pw={idn['password']} pin={idn['pin']}")
