"""
DewaBiz Free Domain Module for dik.py
Registrasi & Checkout domain otomatis di DewaBiz + 2FA Security PIN & Change Nameservers.
Mendukung TLD: .my.id (default), .web.id, .biz.id, .co.id, .or.id, .ac.id, .sch.id, .ponpes.id.

Data akun (nama, email, alamat, kota, state, postcode, negara, telepon, password)
dihasilkan oleh identity_gen.generate_identity() — multi-locale & acak, supaya tiap
akun tidak berpola sama.
"""
import re
import random
import string
import time
import requests
import html

from identity_gen import (
    generate_identity,
    random_digits,
    random_string,
    LOCALE_CODES,
)
from browser_fp import (
    random_profile,
    random_user_agent,
    accept_language_for,
    build_headers,
)

BASE = "https://my.dewabiz.com"

# TLD yang didukung oleh cart WHMCS DewaBiz (harga murah: .my.id & .web.id Rp 5.000)
SUPPORTED_TLDS = (
    '.ponpes.id', '.sch.id', '.biz.id', '.co.id', '.or.id', '.ac.id',
    '.web.id', '.my.id',
)


def random_phone():
    """Kompat lama: nomor gaya ID. Alur baru pakai generate_identity()."""
    return f"858-{random.randint(1000,9999)}-{random.randint(1000,9999)}"


def extract_csrf(html_text):
    if not html_text:
        return None
    m = re.search(r'name=["\']csrfToken["\']\s*value=["\']([a-f0-9]+)["\']', html_text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r'csrfToken\s*=\s*["\']([a-f0-9]+)["\']', html_text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r'name=["\']token["\']\s*value=["\']([a-f0-9]+)["\']', html_text, re.IGNORECASE)
    if m:
        return m.group(1)
    return None

def _visible_alerts(html_text, kind):
    """
    Ambil teks alert Bootstrap yang BENAR-BENAR tampil.

    Halaman WHMCS punya template alert tersembunyi (`class="alert alert-danger hidden"`,
    mis. "Please enter a number between 8 and 64 for the password length") yang selalu
    ada di HTML. Kalau ikut dibaca, promo yang sukses bisa salah dianggap gagal.
    """
    out = []
    if not html_text:
        return out
    for m in re.finditer(
        r'<div[^>]*class=["\']([^"\']*alert[^"\']*)["\'][^>]*>(.*?)</div>',
        html_text, re.IGNORECASE | re.DOTALL,
    ):
        cls = m.group(1).lower()
        if kind not in cls:
            continue
        if 'hidden' in cls or 'w-hidden' in cls:
            continue
        txt = re.sub(r'<[^>]+>', ' ', m.group(2))
        txt = re.sub(r'\s+', ' ', txt).strip()
        if txt:
            out.append(txt)
    return out


def is_security_gate(html_text):
    """
    True kalau halaman dipaksa ke /user/security karena 2FA belum aktif.

    Selama akun belum mengaktifkan two-factor, SEMUA request cart.php dijawab
    dengan halaman security ini — sehingga promo/checkout tidak akan pernah jalan.
    """
    if not html_text:
        return False
    return bool(re.search(
        r'you must configure two-factor authentication',
        html_text, re.IGNORECASE,
    ))


def _extract_total(html_text):
    """
    Ambil "Total Due Today" dari halaman checkout.
    Contoh HTML: <strong id="totalCartPrice">Rp 0</strong>
    Returns string (mis. "Rp 0") atau None.
    """
    if not html_text:
        return None
    m = re.search(r'id=["\']totalCartPrice["\'][^>]*>\s*([^<]+?)\s*<', html_text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'Total Due Today\s*:?.{0,120}?>\s*(Rp[\s\d.,]+)', html_text, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else None


def _promo_state(html_text):
    """
    Baca hasil validasi promo dari halaman cart.

    Berdasarkan HAR + uji live:
      • div.view-cart-promotion-code berisi nama promo -> promo tercatat di cart.
      • alert-info "...no items qualify..." -> promo tercatat tapi cart belum
        punya item yang memenuhi syarat (belum jadi diskon).
      • alert-warning "has already been used"  -> kuota kode promo sudah habis.
      • alert-warning "does not exist"         -> kode promo salah/tidak ada.
      • alert-danger yang TAMPIL -> promo ditolak.
    Returns (applied: bool, message: str)
    """
    if not html_text:
        return False, "Tidak ada respons dari halaman cart"
    if is_security_gate(html_text):
        return False, "Terhalang halaman security (2FA belum aktif)"

    m_code = re.search(
        r'class=["\']view-cart-promotion-code["\'][^>]*>\s*([^<]+?)\s*<',
        html_text, re.IGNORECASE,
    )
    no_qualify = re.search(r'no items qualify', html_text, re.IGNORECASE)

    if m_code and not no_qualify:
        return True, f"Promo aktif: {m_code.group(1).strip()}"
    if m_code and no_qualify:
        return False, "Promo tercatat, menunggu item yang memenuhi syarat"

    # Pesan penolakan WHMCS muncul sebagai alert-warning (bukan alert-danger)
    for kind in ('alert-danger', 'alert-warning'):
        for txt in _visible_alerts(html_text, kind):
            low = txt.lower()
            if 'two-factor' in low or 'is being logged' in low:
                continue
            if 'already been used' in low:
                return False, ("Kuota kode promo sudah habis terpakai "
                               "(server: \"has already been used\")")
            if 'does not exist' in low:
                return False, "Kode promo tidak terdaftar di server"
            if 'expired' in low or 'no longer' in low:
                return False, "Kode promo sudah kedaluwarsa"
            if 'promo' in low or kind == 'alert-danger':
                return False, f"Promo ditolak: {txt[:160]}"

    if re.search(r'(not valid|tidak valid|invalid promo|expired)', html_text, re.IGNORECASE):
        return False, "Kode promo tidak valid / kedaluwarsa"
    return False, "Status promo tidak terbaca"


def _apply_promo(session, token, promo_code, headers=None, attempts=3):
    """
    Terapkan kode promo ke cart yang SUDAH berisi item.

    Server WHMCS menolak diskon kalau cart masih kosong, jadi fungsi ini harus
    dipanggil setelah addToCart. Dicoba beberapa kali karena token bisa berputar.
    Returns (applied: bool, message: str)
    """
    last_msg = "Belum dicoba"
    cur_token = token
    for i in range(attempts):
        r = session.post(f"{BASE}/cart.php?a=view", data={
            'token': cur_token,
            'promocode': promo_code,
            'validatepromo': 'Validate Code',
        }, headers=headers, timeout=20)
        applied, last_msg = _promo_state(r.text)
        if applied:
            return True, last_msg
        # Penolakan final dari server — mengulang tidak akan mengubah hasil
        if ('habis terpakai' in last_msg or 'tidak terdaftar' in last_msg
                or 'kedaluwarsa' in last_msg):
            return False, last_msg
        new_token = extract_csrf(r.text)
        if new_token:
            cur_token = new_token
        if i < attempts - 1:
            time.sleep(1.2)
    return False, last_msg


def _enable_2fa_pin(session, token, pin_code, locale_code, fp):
    """
    Aktifkan 2FA Security PIN. WAJIB berhasil, karena selama 2FA belum aktif
    WHMCS memaksa semua halaman (termasuk cart.php) ke /user/security — itulah
    yang membuat kode promo & checkout gagal.

    Urutan sesuai HAR:
      GET  /user/security                              -> ambil token
      POST /account/security/two-factor/enable         -> buka dialog
      POST /account/security/two-factor/enable/configure (module=pin)
      POST /account/security/two-factor/enable/verify   (pin + pinconfirm)

    Ketiga endpoint terakhir adalah AJAX dan menjawab JSON {"body": "<html>"}.
    Returns (ok: bool, message: str, token: str)
    """
    ajax = build_headers(locale_code, fp, ajax=True, referer=f"{BASE}/user/security")

    r_sec = session.get(f"{BASE}/user/security", timeout=20)
    tok = extract_csrf(r_sec.text) or token

    # Buka dialog 2FA (tanpa module) — mengikuti urutan browser sebenarnya
    session.post(f"{BASE}/account/security/two-factor/enable",
                 data={'token': tok}, headers=ajax, timeout=20)

    r_conf = session.post(f"{BASE}/account/security/two-factor/enable/configure",
                          data={'token': tok, 'module': 'pin'},
                          headers=ajax, timeout=20)
    # Form verify membawa token-nya sendiri di dalam JSON body
    tok_verify = extract_csrf(r_conf.text) or tok

    r_ver = session.post(f"{BASE}/account/security/two-factor/enable/verify", data={
        'token': tok_verify,
        'step': 'verify',
        'module': 'pin',
        'pin': pin_code,
        'pinconfirm': pin_code,
    }, headers=ajax, timeout=20)

    if r_ver.status_code != 200:
        return False, f"HTTP {r_ver.status_code} saat verify PIN", tok

    body = r_ver.text or ''
    if re.search(r'now enabled|berhasil diaktifkan|Backup Code', body, re.IGNORECASE):
        # Konfirmasi ulang: halaman lain tidak lagi dipaksa ke security gate
        r_check = session.get(f"{BASE}/user/security", timeout=20)
        return True, "2FA PIN aktif", extract_csrf(r_check.text) or tok_verify

    # Gagal — ambil pesan error yang tampil biar jelas penyebabnya
    txt = re.sub(r'<[^>]+>', ' ', body)
    txt = re.sub(r'\s+', ' ', txt).strip()
    return False, txt[:200] or "Respons verify tidak dikenali", tok_verify


def register_dewabiz_domain(domain_name, promo_code="HITECH2026", progress_cb=None,
                            locale=None, require_promo=True):
    """
    Eksekusi penuh alur registrasi DewaBiz -> 2FA PIN -> Promo -> Cart -> Checkout -> DB.
    `domain_name` mendukung semua TLD di SUPPORTED_TLDS (.my.id, .web.id, .biz.id, dll).
    Kalau tidak ada TLD yang dikenal, default ke .my.id.
    `locale` opsional: kode negara 2 huruf (mis. "US", "GB"). Default random.
    `require_promo` (default True): kalau promo TIDAK menempel, proses DIBATALKAN
    sebelum checkout supaya tidak pernah terbit order berbayar. Set False hanya
    kalau user memang sengaja mau bayar.
    Returns (success: bool, data_dict, error_msg)
    """
    clean_name = domain_name.strip().lower()
    if clean_name.startswith(("http://", "https://")):
        clean_name = clean_name.split("//", 1)[1]
    clean_name = clean_name.split("/")[0]

    if not any(clean_name.endswith(tld) for tld in SUPPORTED_TLDS):
        # Strip ekstensi tak dikenal, default ke .my.id
        if "." in clean_name:
            clean_name = clean_name.rsplit(".", 1)[0]
        clean_name = f"{clean_name}.my.id"

    def _notify(step_msg):
        if progress_cb:
            try:
                progress_cb(step_msg)
            except Exception:
                pass

    # Identitas acak per akun (nama, email, alamat, kota/state/postcode, telepon)
    ident = generate_identity(locale)
    # Satu profil browser dipakai konsisten sepanjang sesi (Android mayoritas)
    fp = random_profile()

    _notify("1/6 Membuka pendaftaran akun...")
    session = requests.Session()
    session.headers.update(build_headers(ident['locale'], fp))

    try:
        r1 = session.get("https://my.dewabiz.com/register.php", timeout=20)
        if r1.status_code != 200:
            return False, None, f"Gagal membuka register.php (Status {r1.status_code})"
        token = extract_csrf(r1.text)
        if not token:
            return False, None, "Gagal mendapatkan CSRF Token pendaftaran"

        email = ident['email']
        fname = ident['firstname']
        lname = ident['lastname']
        password = ident['password']
        phone = ident['phone']
        pin_code = ident['pin']

        _notify("2/6 Mengisi data pendaftaran...")
        reg_payload = {
            'token': token,
            'register': 'true',
            'firstname': fname,
            'lastname': lname,
            'email': email,
            'country-calling-code-phonenumber': ident['calling_code'],
            'phonenumber': phone,
            'companyname': '',
            'address1': ident['address1'],
            'address2': ident['address2'],
            'city': ident['city'],
            'state': ident['state'],
            'postcode': ident['postcode'],
            'country': ident['country'],
            'tax_id': '',
            'currency': '1',
            'password': password,
            'password2': password,
            'accepttos': 'on',
        }

        r2 = session.post("https://my.dewabiz.com/register.php", data=reg_payload, allow_redirects=True, timeout=25)
        if "register.php" in r2.url and "error" in r2.text.lower():
            return False, None, "Pendaftaran akun gagal (Form rejected)"

        _notify("3/6 Mengaktifkan Security PIN...")
        pin_ok, pin_msg, token_sec = _enable_2fa_pin(
            session, token, pin_code, ident['locale'], fp
        )
        if not pin_ok:
            return False, None, f"Gagal mengaktifkan 2FA Security PIN: {pin_msg}"

        _notify("4/6 Menambahkan domain ke keranjang...")
        r_cart_init = session.get(f"{BASE}/cart.php?a=add&domain=register", timeout=20)
        if is_security_gate(r_cart_init.text):
            return False, None, "Cart terhalang halaman security — 2FA belum benar-benar aktif"
        token_cart = extract_csrf(r_cart_init.text) or token_sec

        # Domain HARUS masuk cart dulu. Kalau promo divalidasi saat cart masih kosong,
        # server menjawab "no items qualify for the discount yet" dan diskon tidak
        # menempel (inilah penyebab promo terlihat gagal).
        add_headers = build_headers(
            ident['locale'], fp, ajax=True,
            referer=f"{BASE}/cart.php?a=add&domain=register",
        )
        r_add = session.post(f"{BASE}/cart.php", data={
            'a': 'addToCart',
            'domain': clean_name,
            'token': token_cart,
            'whois': '0',
            'sideorder': '0',
            'idnlanguage': '',
        }, headers=add_headers, timeout=20)

        added_ok = False
        try:
            added_ok = 'added' in (r_add.json() or {}).get('result', '')
        except Exception:
            added_ok = 'added' in (r_add.text or '')
        if not added_ok:
            return False, None, f"Domain gagal masuk keranjang: {r_add.text[:200]}"

        # Konfirmasi konfigurasi domain (periode 1 tahun) — halaman ini juga menyegarkan
        # token cart sebelum promo diterapkan.
        r_conf = session.get(f"{BASE}/cart.php?a=confdomains", timeout=20)
        token_cart = extract_csrf(r_conf.text) or token_cart

        _notify("5/6 Menerapkan kode promo...")
        promo_headers = build_headers(
            ident['locale'], fp, referer=f"{BASE}/cart.php?a=view"
        )
        promo_applied, promo_msg = _apply_promo(
            session, token_cart, promo_code, promo_headers
        )
        # Ambil token terbaru dari halaman cart setelah promo
        r_view = session.get(f"{BASE}/cart.php?a=view", timeout=20)
        token_cart = extract_csrf(r_view.text) or token_cart

        # PENGAMAN: tanpa promo, checkout akan menerbitkan invoice BERBAYAR.
        # Lebih baik berhenti di sini daripada diam-diam bikin order berbayar.
        if require_promo and not promo_applied:
            try:
                # Kosongkan keranjang (form "Empty Cart": POST /cart.php a=empty)
                session.post(f"{BASE}/cart.php",
                             data={'token': token_cart, 'a': 'empty'},
                             headers=promo_headers, timeout=15)
            except Exception:
                pass
            return False, None, (
                f"Kode promo '{promo_code}' tidak bisa dipakai — {promo_msg}. "
                "Checkout dibatalkan supaya tidak muncul tagihan. "
                "Ganti dengan kode promo yang masih aktif."
            )

        _notify("6/6 Menyelesaikan checkout...")
        r_chk_page = session.get(f"{BASE}/cart.php?a=checkout&e=false", timeout=20)
        token_chk = extract_csrf(r_chk_page.text) or token_cart

        # Total yang benar-benar ditagih. Kalau promo gagal, ini bukan Rp 0.
        total_due = _extract_total(r_chk_page.text)

        acc_id_m = re.search(r'name=["\']account_id["\']\s*value=["\'](\d+)["\']', r_chk_page.text)
        account_id = acc_id_m.group(1) if acc_id_m else ''

        checkout_payload = {
            'token': token_chk,
            'checkout': 'true',
            'custtype': 'existing',
            'account_id': account_id,
            'loginemail': '',
            'loginpassword': '',
            'firstname': fname,
            'lastname': lname,
            'email': email,
            'country-calling-code-phonenumber': ident['calling_code'],
            'phonenumber': phone,
            'companyname': '',
            'address1': reg_payload['address1'],
            'address2': reg_payload['address2'],
            'city': reg_payload['city'],
            'state': reg_payload['state'],
            'postcode': reg_payload['postcode'],
            'country': reg_payload['country'],
            'tax_id': '',
            'contact': '',
            'domaincontactfirstname': '',
            'domaincontactlastname': '',
            'domaincontactemail': '',
            'country-calling-code-domaincontactphonenumber': ident['calling_code'],
            'domaincontactphonenumber': '',
            'domaincontactcompanyname': '',
            'domaincontactaddress1': '',
            'domaincontactaddress2': '',
            'domaincontactcity': '',
            'domaincontactstate': '',
            'domaincontactpostcode': '',
            'domaincontactcountry': reg_payload['country'],
            'domaincontacttax_id': '',
            'applycredit': '1',
            'paymentmethod': 'billingotomatis',
            'ccinfo': 'new',
            'ccnumber': '',
            'ccexpirydate': '',
            'cccvv': '',
            'ccdescription': '',
            'nostore': '0',
            'notes': '',
            'accepttos': 'on',
        }

        chk_headers = build_headers(
            ident['locale'], fp, referer=f"{BASE}/cart.php?a=checkout&e=false"
        )
        r_submit = session.post(f"{BASE}/cart.php?a=checkout", data=checkout_payload,
                                headers=chk_headers, allow_redirects=True, timeout=30)

        order_num = None
        m_order = re.search(r'Order Number is:\s*<b>(\d+)</b>|Order Number:\s*(\d+)', r_submit.text, re.IGNORECASE)
        if m_order:
            order_num = m_order.group(1) or m_order.group(2)

        if "a=complete" not in r_submit.url and "Order Number" not in r_submit.text and "Pesanan" not in r_submit.text:
            return False, None, f"Checkout tidak selesai: URL {r_submit.url}"

        # Dapatkan ID domain dari Client Area
        domain_id = None
        r_domains = session.get("https://my.dewabiz.com/clientarea.php?action=domains", timeout=20)
        m_dom = re.search(r'href=["\']clientarea\.php\?action=domaindetails&amp;id=(\d+)["\'][^>]*>\s*' + re.escape(clean_name), r_domains.text, re.IGNORECASE)
        if not m_dom:
            m_dom = re.search(r'domaindetails&amp;id=(\d+)', r_domains.text)
        if m_dom:
            domain_id = m_dom.group(1)

        result_data = {
            'domain': clean_name,
            'email': email,
            'password': password,
            'pin_code': pin_code,
            'domain_id': domain_id or '',
            'order_num': order_num or 'Sukses',
            'status': 'pending',
            # Info identitas (berguna untuk laporan/TXT & debug)
            'fullname': ident['fullname'],
            'locale': ident['locale'],
            'phone': f"+{ident['calling_code']}{phone.replace('-', '')}",
            'city': ident['city'],
            'state': ident['state'],
            # Status promo & total tagihan — bukti diskon benar-benar kepakai
            'promo_applied': promo_applied,
            'promo_msg': promo_msg,
            'promo_code': promo_code,
            'total_due': total_due or '-',
            'device': 'Android' if fp['mobile'] else fp['platform'],
        }

        return True, result_data, None

    except Exception as e:
        return False, None, f"Error proses: {e}"


def parse_domain_status(html_text):
    """
    Baca halaman clientarea.php?action=domaindetails dan tentukan status domain.

    Sumber kebenaran (berdasarkan HAR dewabiz.har):
      1. Field ringkasan `<strong>Status:</strong></h4> Pending|Active` -> paling akurat.
      2. class link tab `href="#tabNameservers"` -> kalau mengandung `disabled`,
         berarti domain belum aktif dan Nameserver belum bisa diubah.

    Returns dict: {status: 'active'|'pending'|'unknown', is_active, ns_disabled, status_word}
    """
    if not html_text:
        return {'status': 'unknown', 'is_active': False, 'ns_disabled': True, 'status_word': ''}

    # 1. Field Status di ringkasan domain
    m_status = re.search(
        r'<strong>\s*Status\s*:\s*</strong>\s*</h4>\s*([A-Za-z]+)',
        html_text, re.IGNORECASE,
    )
    status_word = m_status.group(1).strip().lower() if m_status else ''
    is_active = status_word in ('active', 'aktif')

    # 2. Tab "Modify Nameservers" — disabled selama domain belum aktif
    m_nstab = re.search(
        r'href=["\']#tabNameservers["\'][^>]*class=["\']([^"\']*)["\']',
        html_text, re.IGNORECASE,
    )
    if m_nstab:
        ns_disabled = 'disabled' in m_nstab.group(1).lower()
    else:
        # Tidak ketemu tab NS: percaya field Status.
        ns_disabled = not is_active

    # Kalau field Status tidak terbaca, jadikan tab NS sebagai penentu.
    if not m_status:
        is_active = not ns_disabled
        status_word = 'active' if is_active else 'pending'

    final = 'active' if (is_active and not ns_disabled) else 'pending'
    return {
        'status': final,
        'is_active': is_active,
        'ns_disabled': ns_disabled,
        'status_word': status_word,
    }


def _dewabiz_login(session, email, password, pin_code=None):
    """Login ke DewaBiz + selesaikan 2FA PIN challenge. Returns (ok: bool, error_msg: str|None)."""
    r_login_page = session.get("https://my.dewabiz.com/login.php", timeout=20)
    token = extract_csrf(r_login_page.text)
    if not token:
        return False, "Gagal mengambil token login"

    r_login = session.post("https://my.dewabiz.com/dologin.php", data={
        'token': token,
        'username': email,
        'password': password,
    }, allow_redirects=True, timeout=20)

    if "incorrect" in r_login.text.lower():
        return False, "Login gagal: Email/Password tidak cocok"

    # Handle 2FA challenge — WHMCS redirect ke /login/challenge
    if "/login/challenge" in r_login.url or "second factor" in r_login.text.lower():
        if not pin_code:
            return False, "Login butuh 2FA PIN tapi pin_code tidak diberikan"
        tok2 = extract_csrf(r_login.text)
        if not tok2:
            return False, "Gagal mengambil token 2FA challenge"
        r_2fa = session.post("https://my.dewabiz.com/login/challenge/verify", data={
            'token': tok2,
            'pin': pin_code,
        }, allow_redirects=True, timeout=20)
        # Setelah PIN benar, redirect ke clientarea.php
        if "/login" in r_2fa.url:
            return False, "2FA PIN salah atau challenge gagal"

    return True, None


def _resolve_domain_id(session, domain_id, domain_name=None):
    """Ambil domain_id dari client area kalau belum ada."""
    if domain_id:
        return domain_id
    r_doms = session.get("https://my.dewabiz.com/clientarea.php?action=domains", timeout=20)
    if domain_name:
        m = re.search(
            r'domaindetails&(?:amp;)?id=(\d+)["\'][^>]*>\s*' + re.escape(domain_name),
            r_doms.text, re.IGNORECASE,
        )
        if m:
            return m.group(1)
    m_id = re.search(r'domaindetails&(?:amp;)?id=(\d+)', r_doms.text)
    return m_id.group(1) if m_id else None


def check_domain_status(email, password, domain_id, domain_name=None, pin_code=None):
    """
    Cek status domain (active/pending) TANPA mengubah nameserver.
    Dipakai oleh /listdomain untuk auto-refresh status pending.
    Returns (ok: bool, status: 'active'|'pending'|'unknown', message: str)
    """
    session = requests.Session()
    session.headers.update({
        'User-Agent': random_user_agent(),
    })
    try:
        ok, err = _dewabiz_login(session, email, password, pin_code)
        if not ok:
            return False, 'unknown', err

        domain_id = _resolve_domain_id(session, domain_id, domain_name)
        if not domain_id:
            return False, 'pending', "Domain belum memiliki Domain ID di sistem"

        r_detail = session.get(
            f"https://my.dewabiz.com/clientarea.php?action=domaindetails&id={domain_id}",
            timeout=20,
        )
        if r_detail.status_code != 200:
            return False, 'unknown', f"Gagal membuka detail domain (HTTP {r_detail.status_code})"

        info = parse_domain_status(r_detail.text)
        if info['status'] == 'active':
            return True, 'active', "Domain sudah AKTIF."
        return True, 'pending', "Domain masih PENDING / belum aktif (aktivasi ±15 menit)."
    except Exception as e:
        return False, 'unknown', f"Error cek status: {e}"


def check_and_update_nameservers(email, password, domain_id, ns1, ns2, pin_code=None):
    """
    Login ulang ke DewaBiz -> Cek status domain (Active/Pending) -> Update Nameservers jika Active.
    Returns (success: bool, status_str: str, message: str)
    """
    session = requests.Session()
    session.headers.update({
        'User-Agent': random_user_agent(),
    })

    try:
        # Login
        ok, err = _dewabiz_login(session, email, password, pin_code)
        if not ok:
            return False, 'unknown', err

        # Buka detail domain
        domain_id = _resolve_domain_id(session, domain_id, None)
        if not domain_id:
            return False, 'pending', "Domain belum memiliki Domain ID di sistem"

        r_detail = session.get(f"https://my.dewabiz.com/clientarea.php?action=domaindetails&id={domain_id}", timeout=20)
        if r_detail.status_code != 200:
            return False, 'unknown', f"Gagal membuka detail domain (HTTP {r_detail.status_code})"

        html_text = r_detail.text
        info = parse_domain_status(html_text)

        if info['status'] != 'active':
            return False, 'pending', "Domain masih PENDING / belum aktif (sekitar 15 menit)."

        # Jika sudah Active & tombol Nameservers tidak disabled -> ganti NS
        token_ns = extract_csrf(html_text)
        ns_payload = {
            'token': token_ns,
            'id': domain_id,
            'sub': 'savens',
            'nschoice': 'custom',
            'ns1': ns1.strip(),
            'ns2': ns2.strip(),
            'ns3': '',
            'ns4': '',
            'ns5': '',
        }
        r_ns = session.post("https://my.dewabiz.com/clientarea.php?action=domaindetails", data=ns_payload, timeout=20)
        
        if r_ns.status_code == 200:
            return True, 'active', f"Nameserver berhasil diubah ke <code>{html.escape(ns1)}</code> & <code>{html.escape(ns2)}</code>!"
        return False, 'active', "Gagal mengirimkan perubahan Nameserver"

    except Exception as e:
        return False, 'unknown', f"Error ganti nameserver: {e}"
