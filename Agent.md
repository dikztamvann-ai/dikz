# PRD: Peningkatan Kecerdasan & Keandalan Agent Orchestration

| | |
|---|---|
| **Dokumen** | Product Requirements Document |
| **Versi** | 1.0 (Draft) |
| **Tanggal** | 9 September 2026 |
| **Status** | 🟡 Menunggu keputusan stack & baseline script |
| **Pemilik** | *(isi nama/tim)* |

---

## ⚠️ Catatan Penting Sebelum Membaca

Dokumen ini adalah **PRD perencanaan**, bukan laporan bug. Karena belum ada script existing yang bisa dianalisis, PRD ini mendefinisikan:
1. **Apa** yang harus dibangun (requirements)
2. **Prinsip kerja** yang wajib dipatuhi saat implementasi nanti (termasuk aturan "jangan ubah struktur" dan "wajib temukan bug valid sebelum fix")
3. **Kriteria selesai** yang objektif dan bisa diverifikasi

Begitu script utama tersedia, Bagian 6 (Audit & Bug-Fix Protocol) menjadi **wajib dijalankan lebih dulu** sebelum fitur baru apa pun ditambahkan.

---

## 1. Latar Belakang & Masalah

Sistem agent/AI orchestration saat ini (atau yang akan dibangun) berjalan, namun ada tiga masalah inti yang ingin diselesaikan:

| # | Masalah | Dampak |
|---|---|---|
| 1 | Agent kurang "pintar" — reasoning lemah, mudah berhalusinasi, keputusan tool-calling tidak tepat | Output tidak reliable, butuh koreksi manual berulang |
| 2 | Fitur-fitur yang sudah ada (tools/function calling) tidak dimanfaatkan maksimal atau tidak konsisten dipanggil | Waste kapabilitas yang sudah dibangun, user tidak dapat value penuh |
| 3 | Tidak ada disiplin dalam menemukan bug nyata sebelum melakukan perubahan — perbaikan sering bersifat tambal-sulam tanpa root cause | Bug berulang, regresi baru muncul dari fix yang tidak lengkap |

**Constraint keras dari stakeholder:**
- ❌ **Tidak boleh mengubah struktur script utama** (arsitektur file, module boundaries, urutan fungsi inti) — hanya isi/logika di dalamnya yang boleh diperbaiki
- ✅ **Wajib menyelesaikan masalah sampai tuntas** — bukan workaround atau silent catch yang menyembunyikan gejala
- ✅ **Wajib menemukan bug yang benar-benar valid** (root cause terverifikasi) sebelum mengubah kode apa pun
- ✅ **Test harus lolos** sebagai syarat definisi selesai (Definition of Done), bukan opsional

---

## 2. Tujuan (Goals)

| Goal | Metrik Sukses |
|---|---|
| G1. Reasoning agent lebih akurat | Tingkat halusinasi/keputusan salah turun ≥30% dibanding baseline, diukur via eval set |
| G2. Semua fitur/tools dipakai optimal | 100% tools yang tersedia punya trigger condition jelas & teruji; tidak ada "dead tool" (tool terdaftar tapi tidak pernah terpanggil secara valid) |
| G3. Struktur script tidak berubah | Diff struktural (file tree, module exports, fungsi publik) = 0 dibanding baseline pra-perbaikan |
| G4. Bug-fix berbasis root cause | Setiap fix punya bukti reproduksi bug + penjelasan root cause tertulis, bukan asumsi |
| G5. Semua test lolos | 100% test suite (existing + baru) hijau sebelum PR/perubahan dianggap selesai |

### Non-Goals (Di Luar Cakupan)
- Migrasi ke stack/framework baru
- Menambah fitur di luar yang sudah direncanakan tanpa persetujuan
- Optimasi biaya/token (kecuali sebagai efek samping dari fix reasoning)
- UI/UX perubahan tampilan output ke end-user

---

## 3. Target Pengguna

| Persona | Kebutuhan |
|---|---|
| **Developer/maintainer script** | Panduan jelas kapan boleh ubah kode, protokol audit bug, checklist test |
| **End user dari agent** | Jawaban lebih akurat, fitur (tools) benar-benar terpakai saat relevan, lebih sedikit error/timeout |
| **Reviewer/QA** | Kriteria objektif untuk approve perubahan (bukti bug + test pass) |

---

## 4. Ruang Lingkup Fitur

### 4.1 Reasoning & Intelligence (Prioritas: Reasoning Quality)

| Fitur | Deskripsi | Prioritas |
|---|---|---|
| F1. Explicit reasoning step sebelum tool call | Agent wajib menjelaskan *mengapa* memanggil tool tertentu sebelum eksekusi (chain-of-thought terstruktur, bukan bebas) | P0 |
| F2. Self-verification pada output kritis | Sebelum jawaban final dikirim, agent melakukan pengecekan konsistensi terhadap data yang sudah dikumpulkan (mencegah halusinasi menimpa fakta dari tool) | P0 |
| F3. Ambiguity handling yang konsisten | Ketika request ambigu, agent punya aturan tetap: pilih interpretasi paling masuk akal, nyatakan asumsi, lanjut kerja — bukan berhenti bertanya tanpa arah | P1 |
| F4. Grounding check untuk klaim faktual | Klaim yang butuh data terkini/spesifik wajib melalui tool call, tidak dijawab dari memori model jika tool tersedia | P0 |

### 4.2 Feature/Tool Reliability

| Fitur | Deskripsi | Prioritas |
|---|---|---|
| F5. Tool selection audit | Setiap tool yang terdaftar di script harus punya minimal 1 test case yang membuktikan tool tersebut *bisa* terpanggil pada kondisi yang tepat | P0 |
| F6. Fallback path untuk tool failure | Ketika tool call gagal (timeout, error, rate limit), agent punya jalur fallback eksplisit (retry terbatas → degrade gracefully → informasikan ke user), bukan crash diam-diam | P0 |
| F7. Tool usage logging/observability | Setiap tool call dicatat: kapan dipanggil, kenapa (reasoning singkat), hasil sukses/gagal — untuk memudahkan audit fitur mana yang under-utilized | P1 |
| F8. Idle/unused tool detection | Mekanisme untuk mendeteksi tool yang terdaftar di script tapi tidak pernah punya jalur logis untuk terpanggil (dead code pada level tool-routing) | P2 |

### 4.3 Struktur & Non-Intrusive Constraint

| Requirement | Detail |
|---|---|
| R1. Struktur file tidak berubah | Nama file, lokasi file, urutan import/module tetap sama persis dengan versi sebelum perbaikan |
| R2. Public interface tidak berubah (kecuali disepakati) | Nama fungsi publik, signature (parameter, return type) tetap sama — perubahan hanya di *implementasi internal* fungsi |
| R3. Perubahan bersifat surgical | Setiap fix menyentuh baris/blok kode seminimal mungkin yang diperlukan untuk menyelesaikan root cause — tidak "sekalian refactor" tanpa alasan |
| R4. Dokumentasi perubahan wajib | Setiap perubahan disertai catatan: file, baris, root cause, cara fix, test pembuktian |

---

## 5. User Stories

1. **Sebagai developer**, saya ingin protokol audit yang jelas, sehingga saya tidak asal menebak penyebab bug dan malah menambah bug baru.
2. **Sebagai end user**, saya ingin agent benar-benar memakai tool pencarian/API saat dibutuhkan, sehingga saya tidak dapat jawaban basi atau mengarang.
3. **Sebagai reviewer**, saya ingin setiap PR perbaikan disertai bukti test lolos dan penjelasan root cause, sehingga saya bisa approve dengan percaya diri tanpa harus re-trace semua logika sendiri.
4. **Sebagai maintainer**, saya ingin struktur script tetap stabil, sehingga integrasi lain yang bergantung pada script ini tidak ikut rusak.

---

## 6. Audit & Bug-Fix Protocol (WAJIB — Bukan Opsional)

Ini adalah bagian paling kritis dari PRD ini, sesuai permintaan eksplisit stakeholder. **Tidak ada perubahan kode yang boleh dilakukan tanpa melewati protokol ini secara berurutan.**

```
┌─────────────────────────────────────────────────────────────┐
│ STEP 1: BASELINE                                              │
│ - Snapshot struktur script saat ini (file tree, exports)      │
│ - Jalankan test suite existing, catat status awal (pass/fail) │
│ - Simpan sebagai reference untuk deteksi structural drift     │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ STEP 2: REPRODUKSI BUG (WAJIB sebelum sentuh kode)             │
│ - Bug harus direproduksi secara konsisten (bukan sekali muncul)│
│ - Tulis langkah reproduksi + input yang memicu                │
│ - Jika tidak bisa direproduksi → BUKAN bug valid, jangan fix   │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ STEP 3: ROOT CAUSE ANALYSIS                                    │
│ - Trace ke baris kode/logika spesifik penyebab bug             │
│ - Bedakan: bug logika vs bug data vs bug integrasi eksternal   │
│ - Tulis root cause dalam 1-2 kalimat yang bisa diverifikasi    │
│   orang lain (bukan "kayaknya karena...")                      │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ STEP 4: MINIMAL FIX                                            │
│ - Perbaiki HANYA logika/isi yang jadi root cause               │
│ - JANGAN ubah nama file, lokasi file, signature fungsi publik  │
│ - Jika fix "butuh" ubah struktur → STOP, eskalasi ke stakeholder│
│   sebelum lanjut (constraint ini hard, bukan guideline)         │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ STEP 5: TEST — WAJIB HIJAU SEMUA                                │
│ - Tulis test baru yang spesifik membuktikan bug ini FIXED       │
│ - Jalankan ULANG seluruh test suite existing (regresi check)    │
│ - Kriteria lolos: 100% pass, tidak ada "skip" untuk sembunyikan │
│   test yang gagal                                               │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ STEP 6: VERIFIKASI STRUKTUR TIDAK BERUBAH                       │
│ - Diff file tree vs baseline STEP 1 → harus identik             │
│ - Diff public exports/signature vs baseline → harus identik     │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ STEP 7: DOKUMENTASI PERUBAHAN                                   │
│ - File + baris yang diubah                                     │
│ - Root cause (dari STEP 3)                                     │
│ - Bukti test (dari STEP 5)                                      │
│ - Konfirmasi struktur tidak berubah (dari STEP 6)               │
└─────────────────────────────────────────────────────────────┘
```

### Aturan Ketat Tambahan

- 🚫 **Dilarang** melakukan fix berdasarkan dugaan ("mungkin ini penyebabnya") tanpa bukti reproduksi dari STEP 2.
- 🚫 **Dilarang** menandai bug "selesai" jika ada test yang di-skip, dikomentari, atau diberi `xfail` untuk menyembunyikan kegagalan.
- 🚫 **Dilarang** melakukan perubahan struktural "sekalian" meskipun terlihat lebih rapi — harus permintaan/perubahan terpisah dengan approval sendiri.
- ✅ **Wajib** setiap bug fix menghasilkan minimal 1 regression test baru yang akan gagal jika bug ini muncul lagi di masa depan.

---

## 7. Kriteria Keberhasilan (Definition of Done)

Sebuah task/PR dianggap **selesai** hanya jika SEMUA poin berikut terpenuhi:

- [ ] Bug/masalah sudah direproduksi dan root cause tertulis (bukan asumsi)
- [ ] Fix bersifat minimal — tidak menyentuh struktur file/fungsi publik
- [ ] Diff struktural terhadap baseline = 0 (file tree & public interface identik)
- [ ] Seluruh test suite (lama + baru) **PASS 100%**, tidak ada yang di-skip
- [ ] Minimal 1 regression test baru ditambahkan khusus untuk bug ini
- [ ] Tool/fitur yang relevan terverifikasi benar-benar terpanggil pada skenario yang tepat (bukan hanya "terdaftar")
- [ ] Dokumentasi perubahan (file, baris, root cause, bukti test) tersedia untuk review

---

## 8. Metrik & Evaluasi Berkelanjutan

| Metrik | Cara Ukur | Target |
|---|---|---|
| Reasoning accuracy | Eval set berisi kasus yang butuh tool call vs tidak; ukur % keputusan tepat | ≥90% |
| Tool utilization rate | % tool yang terdaftar dan punya bukti terpanggil dalam eval set | 100% (tidak ada dead tool) |
| Test pass rate | CI pipeline | 100% wajib sebelum merge |
| Structural drift | Automated diff check (file tree + exports) pada setiap PR | 0 perubahan tak disetujui |
| Bug recurrence rate | % bug yang muncul kembali setelah pernah "difix" | 0% (regression test mencegah ini) |

---

## 9. Risiko & Mitigasi

| Risiko | Mitigasi |
|---|---|
| Fix root cause ternyata butuh ubah struktur (constraint bertabrakan dengan solusi teknis yang benar) | Eskalasi ke stakeholder dengan opsi: (a) fix parsial tanpa ubah struktur + catatan limitasi, atau (b) minta persetujuan khusus untuk exception terbatas |
| Test suite existing lemah/tidak lengkap, sehingga "100% pass" tidak berarti bebas bug | Tambahkan test coverage sebagai bagian dari STEP 5, bukan hanya menjalankan yang sudah ada |
| Reasoning improvement (F1-F4) berpotensi menambah latensi/token | Ukur trade-off, laporkan sebagai bagian evaluasi, jangan silent trade-off |
| Tanpa stack yang ditentukan, implementasi tertunda | Rekomendasi: tentukan stack di sesi berikutnya sebelum Fase 1 dimulai (lihat Bagian 10) |

---

## 10. Langkah Selanjutnya (Open Items)

Untuk PRD ini bisa dieksekusi, hal berikut perlu diputuskan/disediakan:

1. **Stack/bahasa** — Python, JS/TS, atau campuran (masih "belum ditentukan")
2. **Script baseline** — file aktual untuk diaudit (saat ini "belum ada, masih rencana")
3. **Daftar tools/fitur existing** — inventory lengkap tools yang sudah/akan didaftarkan di agent
4. **Test framework** yang dipakai (pytest, jest, dll) agar Bagian 6 & 7 bisa dieksekusi dengan tooling konkret
5. **Eval set** untuk mengukur reasoning accuracy (G1) — perlu disusun terpisah

> Begitu script/repo tersedia, dokumen ini bisa di-refine dengan referensi nama file, fungsi, dan tool yang aktual — dan Bagian 6 bisa langsung dijalankan sebagai audit pertama.

---

*Dokumen ini adalah draft kerja — silakan revisi bagian mana pun sesuai kebutuhan tim.*
