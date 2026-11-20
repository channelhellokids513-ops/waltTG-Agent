# AUDIT — walt-trading: Jejak Perbaikan Ronde 2 sampai 5

Dokumen ini adalah ringkasan audit yang **sudah disanitasi** untuk repo publik. Nilai rahasia,
identifikator akun, dan angka saldo sengaja dibuang. Detail instruksi per ronde disimpan sebagai
`Antigravity_prompt.md` + arsipnya di mesin pemilik (di-`gitignore`, tidak pernah di-commit).

## 0. Catatan sanitasi

| Yang dibuang | Alasan |
|---|---|
| Nilai `WALT_CLIENT_ID` / `WALT_REFRESH_TOKEN` / API key riset / token & chat Telegram | kredensial aktif; refresh token bersifat single-use dan rotasinya aktif |
| ID spreadsheet Google & URL webhook Apps Script | URL webhook adalah kredensial tulis (siapa pun yang punya bisa menulis ke sheet) |
| Nama secret di GitHub Actions | memudahkan pihak luar memetakan apa yang ada/tidak ada di konfigurasi |
| Saldo akun riil (USDC) | data keuangan pribadi; tidak perlu untuk jejak audit |
| Alamat email penulis commit | PII, sudah ada di metadata git, tidak perlu digandakan di dokumen |
| Nomor ID sheet yang telanjur tertulis sebagai default di kode | ikut disanitasi di sini meski pun sudah ada di sumber; lihat §6.3 |

Angka parameter strategi (SL, TP, tier trailing, buffer kas, fee) **tidak** disanitasi karena sudah
publik di `STRATEGY.md`, `PRD.md`, `README.md`, dan kode sumber.

## 1. Ringkasan eksekusi

| Ronde | Rentang commit | Fokus | Hasil verifikasi independen |
|---|---|---|---|
| 2 | `aa545d2`..`2ff2c35` (13 commit) | 4 defect kritis jalur-dingin + 5 tinggi + 5 sedang | 51 → 81 test lulus; 0 run `failure`; bukti runtime: risk-monitor success pada commit akhir |
| 3 | `b3d8577`..`f8234a3` termasuk 1 commit tambahan (6 commit) | Hardening registry trailing, kejujuran exit price, noise log, kedaulatan kredensial, kebersihan repo | 81 → 112 test lulus; diff guard 0 nilai strategi berubah; 0 test lama diedit |
| 4 | `0927fb9`..`f8234a3` (6 commit) | Kill-switch maintenance, alert `invalid_grant`, workflow diagnostik read-only, `.gitignore` artefak audit | 112 lulus; 14 run CI terakhir success; diagnostics menghasilkan bukti skema API riil |
| 5 | belum dieksekusi | Integritas data ledger + jalur yang masih bisa rusak senyap | lihat §5 |

Metode verifikasi yang dipakai di setiap ronde (dan yang akan dipakai lagi untuk ronde 5):
bukan membaca laporan agen, tapi menjalankan ulang — `pytest -q`, diff guard untuk tiap konstanta
strategi, diff guard untuk `timeout-minutes`/`concurrency`/`python-version`, scan pola `except`,
scan literal angka, reproduksi mandiri skenario crash, pembacaan metadata CI dari GitHub API,
dan pengukuran delta berkas state.

## 2. Temuan kritis yang sudah ditutup (ronde 2)

Keempatnya adalah **cold path**: tidak tersentuh satu pun test saat pertama ditemukan, dan hanya
muncul saat gagal jaringan / respons API anomali / job kena timeout — yaitu saat bot sedang
memegang uang riil.

1. **Tidak ada time-guard di jalur pemasangan order.** Guard waktu hanya dipakai monitor. Jika
   GitHub membunuh job di antara order terisi dan pemasangan TP/SL, posisi tertinggal tanpa
   stop-loss dan kode alert tidak pernah terjangkau. Sekarang: guard dipanggil di semua jalur
   kandidat + ambang anggaran retry autoClose, dan **kode tidak boleh berhenti di antara "order
   terisi" dan "TP/SL terpasang"**.
2. **Penutupan dicatat walau eksekusi gagal (phantom close).** Satu funnel validasi kini menjadi
   satu-satunya jalan mencatat close; close gagal → `FAILED`, tanpa log close, tanpa cooldown,
   tanpa ID dedupe.
3. **Akses `.get()` pada objek `preview` yang bisa `None`** → `AttributeError` mematikan seluruh
   siklus monitor. Semua titik sudah berguard.
4. **Nol retry jaringan di klien MCP** (hanya HTTP 401 yang ditangani). Kini retry eksponensial
   untuk error koneksi/timeout/429/5xx dengan hormat `Retry-After`, tanpa pernah menyentuh
   header autentikasi di pesan error.

Temuan tinggi ronde 2: resolusi margin ganda yang bisa menaikkan margin **setelah** cek buffer
dijalankan; webhook sheet tanpa retry (kini dengan fallback CSV lokal); `pynacl` dideklarasikan
tapi tidak terpasang sehingga auto-sync secret mati senyap; `.env` lokal disinkron lewat regex
tanpa penanganan error; parsing OAuth yang mengasumsikan field selalu ada.


---

## 3. Temuan yang MASIH TERBUKA (bahan ronde 5)

Semuanya ditemukan lewat verifikasi independen, bukan dari laporan agen. Nomor N dipertahankan
agar cocok dengan prompt ronde 5.

| ID | Sev | Temuan | Bukti keras |
|---|---|---|---|
| **N1** | P1 | **Test suite menulis ke ledger produksi.** Sekali `pytest -q` menambah 5 baris trade fiktif ke `logs/closed_history.csv`. | Delta diukur per berkas test: `test_risk.py` = +5, delapan berkas lainnya = 0. Lima test guard sisi salah SL/TP memanggil jalur close dengan path default. Dampak terkurangi: CI test tidak menyuntik secret dan URL webhook default kosong → sheet produksi tidak ikut tercemar; kerusakan terbatas state lokal |
| **N2** | P1 | **Angka statistik yang selama ini dipakai sebagai "invarian output" adalah artefak test, bukan performa.** | Dedupe berbasis Position ID meremas ±300 baris sintetis (semua ber-ID sama) menjadi sedikit kelompok; hasil akhir hanya berisi fixture (contract id 1, entry 100.0, margin floor) |
| **N3** | P1 | **Titik buta PnL untuk close yang dieksekusi bursa — struktural, bukan kondisional.** | Workflow diagnostik read-only membuktikan skema riil: 50 posisi closed, 17 kunci, identik di semua sampel, dan **tidak ada satu pun** field harga keluar maupun PnL terealisasi. Akibatnya jalur rekonsiliasi selalu menulis exit/PnL kosong → dihitung sebagai UNKNOWN → tidak pernah masuk win rate, net PnL, maupun fee. Padahal TP/SL justru dipasang sebagai autoClose di bursa |
| **N4** | P2 | **Bentuk ledger tidak konsisten.** Header 10 kolom sementara konstanta header kini 15 arah (14 kolom, akan jadi 15), dan header hanya ditulis saat berkas kosong. | Pengukuran berkas: 1 header + 301 baris data = 121 baris 10 kolom + 180 baris 14 kolom. Pembacaan memakai indeks tetap; baris lama kehilangan `closed_at`/`opened_at` → median hold time tidak menghitung riwayat |
| **N5** | P2 | **Fallback exit price paling akhir tidak jujur.** Mengembalikan harga yang baru saja ditolak sanity guard, dilabeli `entry_fallback` padahal sumbernya snapshot, plus potensi TypeError pada nilai None. | Difuskan oleh validasi ulang di hilir (sheet tidak bisa tercemar nol), dan diekspresikan identik sejak ronde 3 — jadi bukan regresi, tapi lolos dari kriteria ronde 4 |
| **N6** | P1 | **Rantai crash `entry_price` nol — belum pernah muncul di laporan ronde mana pun.** Dua properti `Position` membagi oleh `entry_price` tanpa penjagaan. | Lokasi properti + 4 konsumen di jalur produksi (termasuk pemanggilan di dalam siklus monitor). Satu posisi dengan harga masuk kosong = monitor 10 menit mati sebelum sempat mengevaluasi SL/TP posisi lain. Semua pembagian harga lain sudah terjaga; tinggal dua ini |
| **N7** | Info | Kill-switch pemeliharaan baru terbukti di unit test (parse YAML), belum terbukti membuat run CI benar-benar `skipped`. | Variabel repo belum pernah diset; guard inert secara default (perilaku yang memang dirancang aman) |

## 4. Bukti eksternal yang mendukung kesimpulan

- Status CI dibaca lewat GitHub API, bukan lewat klaim laporan: daftar run `main` (status/kesimpulan),
  dan metadata secret (hanya `updated_at`, nilai tidak pernah bisa maupun perlu dibaca).
- Bukti bahwa rotasi kredensial di sisi CI nyata dan berulang: timestamp `updated_at` pada secret
  refresh token jatuh **di dalam** jendela sebuah run risk-monitor (bukan jam manual operator).
  Inilah akar penyebab `.env` lokal selalu tertinggal satu rotasi dan `dump_closed` lokal mati
  dengan `invalid_grant`.
- Bukti skema API: keluaran workflow diagnostik read-only pada commit ronde 4 (50 posisi closed,
  daftar kunci + tipe, redaksi aktif). Ini yang mengubah status Item 1 ronde 3 dari "PARSIAL"
  menjadi keputusan final: tidak bisa diverifikasi penuh, dan fallback estimasi dipertahankan.
- Guard diffs yang dijalankan ulang setiap ronde: 0 baris untuk nilai strategi, 0 baris untuk
  `timeout-minutes`/`concurrency`/`python-version` pada workflow lama, 0 berkas test lama
  yang berubah isinya (kecuali penambahan fixture yang diwajibkan ronde 5).

## 5. Ruang lingkup ronde 5 (sudah ditulis ke prompt, belum dieksekusi)

1. **[R5-1]** Karantina seluruh tulis test ke direktori sementara + hook yang membuat pytest
   **gagal** bila berkas state produksi bertambah. Syarat lulus: delta baris ledger = 0.
2. **[R5-2]** Hilangkan N3: estimasi harga keluar dari kurs live saat deteksi dengan label sumber
   eksplisit, PnL berbasis ukuran posisi, batas kesegaran bernama, dan larangan fabrikasi
   (gagal/stale harus tetap kosong, bukan nol).
3. **[R5-3]** Satukan bentuk ledger: migrasi header (dry-run default, backup, idempoten) dan
   pembacaan berbasis nama kolom. Syarat lulus: netral-output — angka statistik tidak boleh
   berubah karena migrasi bentuk.
4. **[R5-4]** Tutup N6 dan N5: penjagaan `entry_price <= 0` di kedua properti, jalur keputusan
   yang jujur ("tidak valid" alih-alih diam), dan fallback akhir mengembalikan `None` dengan
   label `"unavailable"`. Perilaku untuk data valid wajib tetap identik.
5. **[R5-5]** Bersihkan baris sintetis (dry-run + backup, lokal saja), luruskan klaim statistik di
   README agar tidak lagi dibaca sebagai rekam jejak, tempelkan daftar kunci API yang terverifikasi,
   buktikan kill-switch sekali secara nyata (`skipped` lalu `success`), dan tambahkan `pipefail`
   di langkah diagnostik yang memakai `tee`.

**FASE AKHIR ronde 5 (wajib):** tiap item = satu commit bertag, lalu push ke `origin/main` dengan
empat bukti remote yang harus menunjuk SHA identik (lokal, tracking ref, daftar commit tertinggal,
dan sudut pandang GitHub), kemudian menunggu CI pada SHA terakhir sampai terminal dengan 0
`failure`. Dilarang force-push, `reset --hard`, dan revert diam-diam. Tanpa bukti push, sebuah item
tidak boleh dilaporkan selesai.


---

## 6. Invarian yang harus dijaga selamanya

Ini adalah kesimpulan keputusan, bukan pekerjaan tertunda. Setiap ronde berikutnya wajib
mempertahankan semuanya:

1. **Keputusan pemilik atas parameter.** SL seragam 1.5%, TP 2.0% (ETF/crypto) / 2.5% (komoditas/saham),
   buffer kas 15%, floor margin per posisi, leverage maksimum 2x, tier trailing, dan cooldown
   re-entry 3 jam. Hardening boleh mengubah **cara nilai dibaca**, tidak pernah nilainya.
   Log L3 (volatility cut) sengaja dideprecated, bukan dihapus, sebagai dokumentasi keputusan.
2. **Eksekusi workflow diantri, bukan paralel.** Satu group concurrency untuk keempat workflow
   trading dengan `cancel-in-progress: false`. Konsekuensi yang diterima sadar: risk monitor bisa
   tertunda maksimum 12 menit di belakang scan/execute. Yang dilindungi: tidak ada dua job
   membuka order bersamaan dan tidak ada tabrakan rotasi token.
3. **Kredensial boleh ditulis dua arah, tapi hanya satu penulis pada satu waktu.** Sinkron
   lokal→GitHub dan CI→GitHub keduanya sudah ada; kill-switch pemeliharaan adalah mekanisme
   anti-tabrakan, bukan mekanisme perbaikan token.
4. **Estimasi harus selalu menyebut dirinya estimasi.** Exit price, PnL, dan sumbernya wajib
   membawa label (`exchange_fill` / `response_position` / `mark_price_proxy` / `mark_at_detection`
   / `snapshot_estimate` / `entry_fallback` / `unavailable`). Yang dilarang adalah angka tanpa
   sumber, dan angka nol yang menyamar sebagai data.
5. **Guard yang tidak bisa merah bukan guard.** Setiap ronde menambahkan test yang lebih dulu
   dibuktikan gagal sebelum perbaikan; itu syarat, bukan formalitas.

### 6.3 Catatan kebersihan yang masih terbuka (sengaja tidak disanitasi dari daftar, hanya dari nilai)

ID spreadsheet Google dipakai sebagai **nilai default di dalam kode sumber**, sehingga berkas kode
yang publik sudah memuatnya. Dokumen ini tidak mengulang nilai itu. Yang perlu dilakukan suatu hari
(kandidat ronde berikutnya, belum dieksekusi):

- jadikan ID spreadsheet wajib dari konfigurasi (env/secret) tanpa default di kode, sehingga
  kegagalan baca muncul sebagai error jelas dan bukan sebagai 404 senyap seperti yang terjadi
  pada `stats` di runner CI;
- putuskan apakah ID sheet boleh memang publik (jika sheet hanya bisa ditulis lewat webhook,
  membaca lewat ekspor CSV tetap butuh akses; 404 di CI menunjukkan batas ini belum dirumuskan);
- simpan jejak keputusan itu di dokumen ini supaya tidak menjadi misteri lintas ronde.

## 7. Cara memakai jejak ini

- **Ingin tahu apa yang berubah dan kenapa:** §1 dan §3; SHA commit ada di `git log` dengan tag
  `[P0-*]`, `[P1-*]`, `[P2-*]` (ronde 2), `[R3-n]` (ronde 3), `[R4-n]` (ronde 4), `[R5-n]` (ronde 5).
- **Ingin memverifikasi klaim:** §1 metode + §4 bukti. Jangan mengandalkan laporan agen; jalankan
  ulang `pytest -q`, guard diff, dan pembacaan CI dari GitHub API.
- **Ingin melanjutkan pekerjaan:** prompt eksekusi per ronde berada di mesin pemilik
  (`Antigravity_prompt.md` + arsip), dan sengaja tidak di-commit karena repo ini publik.
- **Angka di §3 (N2) bukan laporan performa.** Rekam jejak trading riil baru bermakna setelah
  ronde 5 Item 2 dan 5 selesai, karena sampai saat itu penutupan oleh bursa tidak terukur.

---

*Dokumen ini dibuat otomatis dari hasil verifikasi ronde 2-5 dan disanitasi oleh scrubber yang
memuat nilai konfigurasi lokal ke memori semata-mata sebagai pola penghapusan; tidak ada nilai
rahasia yang pernah ditulis ke berkas ini.*
