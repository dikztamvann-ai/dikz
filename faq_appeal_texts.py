"""Pool teks banding/form — ringkas, ID + EN only, tone santai.

Placeholder: {nomor} {jam}
Dipakai faq_server.pick_appeal_text (dan fallback compose lama).

FRAMING WAJIB (sesuai bukti assets/form_v3_proof.jpg):
Layar "Choose how to verify" — opsi SMS abu-abu/disabled dengan tulisan
"Try again in 13:50:11" (hitungan jam:menit:detik). Ada juga pilihan
Voice call & "Retry on other device" tapi SMS-nya terkunci. Nomor kena
limit: WhatsApp nahan ngirim kode SMS sampai hitungan itu selesai.

Struktur cerita natural (kayak orang asli nyeritain):
1. Buka screen "Choose how to verify"
2. SMS gak bisa diklik — cuma ada "Try again in XX jam"
3. Udah nunggu / coba Voice call juga gak bisa
4. SIM fine, nomor milik sendiri
5. Minta di-reset penahannya

JANGAN pakai istilah teknis: rate limit, OTP, "terlalu sering", "menebak
kode". Kutip teks layar apa adanya ("Try again in ...").

Tone: santai, gak terlalu sopan, gak kasar. Kayak chat ke CS biasa.
"""

# Bobot relatif: ID & EN seimbang.
_LONG_TEXTS = [
    # ── Indonesian — santai, kayak orang beneran frustrasi ──
    (
        "id", 1.8,
        "Halo WhatsApp Support,\n\n"
        "Nomor saya +{nomor} gak bisa verifikasi. Tiap kali sampe layar "
        "\"Choose how to verify\", opsi SMS-nya gak bisa diklik — abu-abu "
        "dan di bawahnya cuma ada tulisan \"Try again in 13:50:11\" kayak "
        "gitu, hitungan jam. Jadi WhatsApp gak mau kirim kode SMS ke nomor "
        "saya sampai hitungan itu habis.\n\n"
        "Udah saya coba \"Retry on other device\", restart HP, cabut pasang "
        "SIM, tetap aja muncul hitungan yang sama. SIM saya normal — SMS "
        "dari bank dan aplikasi lain masuk semua.\n\n"
        "Nomor ini saya pake buat kerjaan, pelanggan saya hubungin lewat "
        "WA. Tolong reset penahanan SMS di +{nomor} biar saya bisa minta "
        "kode lagi. Makasih."
    ),
    (
        "id", 1.4,
        "Halo,\n\n"
        "Saya gak bisa masuk WhatsApp di +{nomor}. Pas di layar pilih cara "
        "verifikasi, tombol SMS-nya mati — gak bisa ditekan, tulisannya "
        "\"Try again in {jam} jam\" lebih kurang. Cuma opsi Voice call "
        "yang kebuka, tapi pas saya coba juga gak ada telepon yang masuk.\n\n"
        "Ini udah seharian kayak gini. Nomor ini nomor utama saya, "
        "keluarga dan kantor pada nyariin saya lewat WA semua.\n\n"
        "Tolong dibantu reset ya, biar opsi SMS-nya bisa dipake lagi. "
        "Makasih banyak."
    ),
    (
        "id", 1.0,
        "Hi Tim Support,\n\n"
        "Mau lapor — verifikasi di +{nomor} kena masalah. Layar "
        "\"Choose how to verify\" muncul normal, tapi opsi SMS-nya "
        "nonaktif. Cuma ada teks kecil \"Try again in 13:50:11\" di "
        "sampingnya, jadi saya gak bisa request kode via SMS sama sekali.\n\n"
        "Saya udah tunggu sampai hitungannya habis sekali, pas buka lagi "
        "malah muncul hitungan baru yang mirip. Udah coba Voice call juga "
        "gak nyambung.\n\n"
        "SIM aktif, sinyal bagus, SMS lain jalan normal. Tolong cek kenapa "
        "nomor saya ditahan dan reset ya. Terima kasih."
    ),
    (
        "id", 0.9,
        "Hi WhatsApp,\n\n"
        "Udah sehari lebih +{nomor} gak bisa verifikasi WA. Tiap kali "
        "sampe layar \"Choose how to verify\", opsi SMS-nya selalu "
        "disabled dengan tulisan \"Try again in {jam} jam\" di bawahnya. "
        "Saya gak ngerti kenapa — saya gak salah masukin kode apa-apa "
        "karena kodenya emang gak pernah dikirim.\n\n"
        "Voice call juga udah saya coba sekali tapi gak ada yang masuk.\n\n"
        "SIM aktif, sinyal bagus, SMS dari aplikasi lain lancar. Tolong "
        "dong reset hitungannya / buka lagi opsi SMS buat nomor saya. "
        "Thanks."
    ),
    (
        "id", 1.2,
        "Halo Support,\n\n"
        "Ada masalah verifikasi di +{nomor}: di layar \"Choose how to "
        "verify\", opsi SMS terkunci. Tulisannya \"Try again in {jam} jam\" "
        "kira-kira, jadi selama itu saya gak bisa minta kode SMS dan gak "
        "bisa daftar atau login sama sekali.\n\n"
        "Nomor ini dipake buat bisnis saya — pelanggan hubungin lewat WA "
        "tiap hari, dan tiap jam gak bisa akses itu rugi. \"Retry on "
        "other device\" udah saya coba juga, hasilnya sama.\n\n"
        "Tolong cek dan benerin ya. Terima kasih."
    ),
    # ── English — casual, sounds like a real person ──
    (
        "en", 1.8,
        "Hi WhatsApp Support,\n\n"
        "I can't verify my number +{nomor}. Every time I get to the "
        "\"Choose how to verify\" screen, the SMS option is greyed out — "
        "there's just a \"Try again in 13:50:11\" note under it, counting "
        "down for hours. So WhatsApp won't send me an SMS code until that "
        "timer runs out.\n\n"
        "I've tried \"Retry on other device\", restarted my phone, "
        "re-seated my SIM — the same countdown comes back every time. My "
        "SIM is fine, texts from my bank and other apps arrive normally.\n\n"
        "This is my work number, my clients reach me on WhatsApp all day. "
        "Please reset the SMS hold on +{nomor} so I can request a code "
        "again. Thanks."
    ),
    (
        "en", 1.4,
        "Hey Support,\n\n"
        "I'm stuck on verification for +{nomor}. On the \"Choose how to "
        "verify\" screen the SMS button is dead — can't tap it, it just "
        "says \"Try again in {jam} hours\" or so. The Voice call option "
        "is there, but when I tried it no call ever came through.\n\n"
        "It's been like this for a whole day. This is my main number — "
        "my family and my office reach me through WhatsApp.\n\n"
        "Please help reset it so the SMS option works again. I'd really "
        "appreciate it."
    ),
    (
        "en", 1.0,
        "Hello,\n\n"
        "Reporting a verification issue with +{nomor}: the \"Choose how "
        "to verify\" screen shows up fine, but the SMS option is "
        "inactive. There's a small \"Try again in 13:50:11\" label next "
        "to it, so I can't request a code via SMS at all.\n\n"
        "I already waited out the full countdown once, and when I opened "
        "the screen again there was a fresh countdown. Tried the Voice "
        "call option too — nothing came through.\n\n"
        "SIM is active, signal is fine, other texts work. Please check "
        "why my number is on hold and reset it. Thanks."
    ),
    (
        "en", 0.9,
        "Hi there,\n\n"
        "My +{nomor} has been unable to verify WhatsApp for over a day "
        "now. Every time I reach the \"Choose how to verify\" screen, the "
        "SMS option is always disabled with a \"Try again in {jam} hours\" "
        "note underneath. I don't understand why — I never mistyped any "
        "code because a code was never sent in the first place.\n\n"
        "I tried the Voice call option once too, but no call arrived.\n\n"
        "SIM is active, signal is fine, texts from other apps come through "
        "normally. Please reset the countdown / unlock the SMS option for "
        "my number. Thanks."
    ),
    (
        "en", 1.2,
        "Hey WhatsApp,\n\n"
        "Verification issue on +{nomor}: on the \"Choose how to verify\" "
        "screen, the SMS option is locked. The label says \"Try again in "
        "{jam} hours\" roughly, which means for all that time I can't "
        "request an SMS code and can't register or log in at all.\n\n"
        "This number runs my small business — customers contact me on "
        "WhatsApp daily, and every locked hour costs me. I've tried "
        "\"Retry on other device\" too, same result.\n\n"
        "Please look into this and fix it. Thank you."
    ),
]


def pick_long_appeal(nomor_clean: str, jam: str) -> str:
    """Pilih 1 teks ringkas berbobot, isi {nomor}/{jam}."""
    import random
    texts = [t[2] for t in _LONG_TEXTS]
    weights = [t[1] for t in _LONG_TEXTS]
    raw = random.choices(texts, weights=weights, k=1)[0]
    return raw.replace("{nomor}", str(nomor_clean)).replace("{jam}", str(jam))
