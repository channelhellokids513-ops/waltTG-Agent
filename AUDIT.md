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
| Nomor ID sheet yang telanjur tertulis sebagai default di kode | ikut disanitasi di sini meskipun nilainya sudah ada di berkas kode publik; lihat §7 |

Angka parameter strategi (SL, TP, tier trailing, buffer kas, fee) **tidak** disanitasi karena sudah
publik di `STRATEGY.md`, `PRD.md`, `README.md`, dan kode sumber.

## 1. Ringkasan eksekusi

| Ronde | Rentang commit | Fokus | Hasil verifikasi independen |
|---|---|---|---|
| 2 | `aa545d2`..`2ff2c35` (13 commit) | 4 defect kritis jalur-dingin + 5 tinggi + 5 sedang | 51 → 81 test lulus; 0 run `failure`; bukti runtime: risk-monitor success pada commit akhir |
| 3 | `b3d8577`..`f8234a3` termasuk 1 commit tambahan (6 commit) | Hardening registry trailing, kejujuran exit price, noise log, kedaulatan kredensial, kebersihan repo | 81 → 112 test lulus; diff guard 0 nilai strategi berubah; 0 test lama diedit |
| 4 | `0927fb9`..`f8234a3` (6 commit) | Kill-switch maintenance, alert `invalid_grant`, workflow diagnostik read-only, `.gitignore` artefak audit | 112 lulus; 14 run CI terakhir success; diagnostics menghasilkan bukti skema API riil |
| 5 | `8e72599`..`e68e0eb` (5 commit) | Integritas data ledger + jalur yang masih bisa rusak senyap | 112 → **124 lulus**; `ledger_delta = 0` setelah satu `pytest` penuh; guard anti-pencemaran terbukti **MERAH** (`exit=1` saat 125 test hijau); migrasi ledger **0 nilai lama berubah** + idempoten; purge 301 baris (0 kandidat riil); kill-switch terbukti `skipped`→`success` |
| 6 | belum dieksekusi | Kebenaran data: ledger dari API bursa, integritas cache CI, cakupan watchdog | lihat §3b dan §5b |

Metode verifikasi yang dipakai di setiap ronde (dan yang akan dipakai lagi untuk ronde 6):
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

## 3b. Temuan pasca-ronde 5 (bahan ronde 6 — semuanya terukur, bukan dugaan)

Ronde 5 menutup N1-N7. Verifikasi independen terhadap ronde 5 kemudian membuka lapisan **sistem dan
workflow** yang belum pernah diaudit ronde 2-5, dan menemukan bahwa masalah terbesarnya justru bukan
di Python.

| ID | Sev | Temuan | Bukti keras (2026-11-20) |
|---|---|---|---|
| **S1** | P0 | **Tidak ada satu pun tempat yang bisa membaca riwayat penutupan, padahal trading memakai uang riil.** | Run diagnostik pada SHA tip: `"closed_positions_count": 52` dan `vars.STRATEGY_CONFIG_JSON` berisi `"dry_run":false`. Baca sheet: di CI **HTTP 404** (`Closed_Positions`, `PnL_Tracker`), di lokal **HTTP 401** untuk keempat tab. `stats` mencetak `Total Trade Valid: 0` di kedua dunia. Dua ID sheet yang dipakai dua lingkungan berperilaku **berlainan** — belum dirumuskan mana yang otoritatif. |
| **S1b** | P0 | **Kegagalan baca itu senyap.** | `sheet_logger.py:383-384` dan `walt_bot.py:2029-2030` hanya `log.debug`, lalu mengembalikan kosong. Efek lanjutan: dedupe jatuh ke `closed_ids.json` (cache saja) → risiko pencatatan ganda setelah cache dipangkas. |
| **S2** | P0 | **State risiko hidup di Actions cache tanpa deteksi cache-miss.** | Keempat workflow trading restore `logs/` (`walt-logs-state-`) + save `if: always()`; `risk-monitor` menambah `walt-snapshot-`. Nol langkah yang membaca `cache-hit`. Yang hilang saat cache dipangkas GitHub (7 hari tak diakses / kuota): cooldown re-entry, counter 8 entry/hari & 2 entry/aset/24 jam, jangkar *daily circuit breaker*, `closed_ids`, `open_snapshot`. Efek terburuk yang belum ada tamengnya: snapshot kosong dibaca sebagai "semua posisi tertutup" (banjiri baris `UNKNOWN`) dan limit harian kembali nol. |
| **W1** | P1 | **Watchdog hanya mengawasi satu workflow.** | `watchdog.yml` cek sukses risk-monitor ≤30 menit. `market-execute`, `crypto-execute`, `daily-scan` tidak punya cron GitHub — dipicu Google Cloud Scheduler eksternal. Job Scheduler mati = entry berhenti tanpa satu pun alert. |
| **V1** | P1 | Dua jalur tulis produksi di luar daftar pengawasan guard ronde 5. | `logs/security_audit.jsonl` (`telegram_auth.py:61`) dan `logs/_tmp_sheet_batch.json` (`sheet_logger.py:201`) tidak ada di `_STATE_FILES`; jalur kedua juga memakai literal path sehingga tidak bisa diarahkan fixture isolasi. |
| **V2** | P2 | `notifier.py:19` bisa mematikan pemanggilnya di konsol Windows. | Preview dicetak pada cabang kredensial `dummy` (jalur lokal paling umum). Repro: rc=1 `UnicodeEncodeError` dengan `PYTHONIOENCODING=cp1252`, rc=0 dengan UTF-8. Pre-existing, tidak disentuh ronde 5. |
| **V3** | P2 | README §172 mengklaim "Tanpa Hardcode Sheet ID", padahal `sheet_logger.py:22` masih memakai ID sebagai default. | Klaim dokumentasi vs kode bertentangan; dan `.env` lokal tidak memuat `GOOGLE_SHEET_ID` maupun `GOOGLE_SHEET_WEBHOOK_URL` → tulis lokal tidak pernah sampai ke sheet. |
| **C1** | P2 | Workflow berlabel read-only **menulis kredensial**. | Log diagnostik: `PUT .../actions/secrets/WALT_REFRESH_TOKEN` → `204`. Inilah mekanisme yang membuat `.env` lokal selalu tertinggal satu rotasi. |
| **C2** | P2 | Least-privilege & reproducibility belum dibereskan. | `permissions:` hanya ada di 1 dari 7 workflow; `requirements*.txt` memakai rentang `>=`; 23 rujukan `uses:` masih tag mayor. |
| **D1** | Info | Baseline ronde 5 **sah** berubah: ledger lokal kini hanya header, `stats` = 0 trade. | Bukan regresi — konsekuensi purge yang benar. Tapi artinya nol rekam jejak riil tersimpan di lokal, dan yang riil ada di bursa (S1). |

**Yang justru terverifikasi baik:** queue `concurrency` satu group tanpa cancel ✓, `set -o pipefail`
di 5 workflow ✓, nol `continue-on-error` ✓, nol `pull_request_target` ✓, `tests.yml` tanpa secrets ✓,
redaksi bekerja di log publik ✓, `python-version` konsisten ✓.

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

## 5. Ronde 5 — ruang lingkup dan hasil (dieksekusi 2026-11-20, diverifikasi independen)

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

**Hasil verifikasi ronde 5** (semuanya saya ukur ulang, bukan membaca laporan agen):

- 124 test lulus; delta ledger **0** dan hash `closed_history.csv` tidak berubah setelah `pytest`.
- Guard anti-pencemaran saya pancing dengan menulis 1 baris langsung ke ledger: **125 test hijau
  tapi `exit=1`** + pesan `LEDGER TERCEMAR` → guard tidak vakum.
- Premis ronde 5 saya reproduksi di worktree commit lama: di `f8234a3` suite menciptakan **5 baris**
  sintetis dan `pnl_pct_margin` benar-benar melempar `ZeroDivisionError` → N1 & N6 nyata.
- Migrasi (pada salinan file campuran 10/14 kolom): semua jadi 15 kolom, **0 nilai lama berubah**
  sel-demi-sel, apply kedua `changed=False`, digest reader by-name identik pra/pasca → netral-output.
- Purge: 301 baris = 7 signature, semuanya pola fixture (`cid=1`, `entry=100.0`, `NVDA`, `margin=2.5`),
  termasuk 20 baris dalam satu detik yang sama → **0 kandidat trade riil** yang hilang.
- Assert lama: 9 dari 9 berkas test warisan jumlah assert-nya identik; nol nilai strategi berubah;
  nol perubahan `timeout-minutes`/`concurrency`/`python-version`; 1.375 baris diff bersih dari rahasia.
- CI: 25 run terakhir = 24 success + 1 skipped, **0 failure**; kill-switch terbukti nyata lalu `false`.

## 5b. Ronde 6 — ruang lingkup (prompt sudah ditulis, belum dieksekusi)

1. **[R6-1][P0]** Jadikan API bursa sumber kebenaran: `walt_bot.py reconcile_history` (read-only,
   kunci dedupe **memakai ulang** `make_position_dedupe_key` supaya backfill dan deteksi live
   collapse ke identitas yang sama), baris backfill jujur (`Exit Price Source =
   "exchange_history_no_exit"`, PnL kosong — endpoint tidak punya harga keluar), plus
   `ledger_source` dan pemisahan `pnl_terukur` vs `belum_terukur_pnl` di `stats`. Kegagalan baca
   naik dari `log.debug` ke `log.warning` teragregasi, dan `DEFAULT_SPREADSHEET_ID` dihapus dari
   kode. (Menutup S1, S1b, V3, dan sebagian §7.)
2. **[R6-2][P0]** Deteksi `cache-hit` di keempat workflow + perilaku start dingin yang konservatif:
   snapshot kosong tidak boleh memicu banjir `UNKNOWN`, rehidrasi dedupe dari API, dan jalur entry
   **menolak** membuka posisi ketika limit harian/cooldown tidak diketahui (`WALT_ALLOW_COLD_ENTRY`
   sebagai override sadar). Termasuk bukti nyata dengan cache yang dihapus. (Menutup S2.)
3. **[R6-3][P1]** Watchdog mengawasi keempat workflow dengan ambang usia per-cadensi (diambil dari
   tabel jadwal di dokumentasi, bukan angka karangan), tabel status ke Step Summary, fallback
   dispatch hanya untuk yang stale, `MAINTENANCE_MODE` tetap lebih dulu keluar. (Menutup W1.)
4. **[R6-4][P2]** `permissions:` eksplisit di 7 workflow, `requirements*.txt` dikunci `==`,
   `uses:` di-pin ke SHA, dan mode read-only untuk sinkronisasi token (`diagnostics.yml` tidak lagi
   menulis secret; jalur trading tetap berotasi). (Menutup C1, C2.)
5. **[R6-5][P3]** Sisa kebersihan: dua jalur tulis ke `_STATE_FILES` dengan **bukti merah** untuk
   masing-masing, `notifier` aman-encoding (repro cp1252 jadi rc 0), `purge-fixture-ids` untuk
   `closed_ids.json`, dan penghapusan artefak sisa probe. (Menutup V1, V2, V3-sisa.)

FASE AKHIR ronde 6 mengikuti pola ronde 5 (satu item satu commit, push, empat bukti remote identik,
CI terminal 0 failure) ditambah bukti hidup per item — termasuk grep log `diagnostics.yml` yang
**tidak** memuat `PUT .../actions/secrets/`, dan pengukuran delta ledger = 0 selama uji cache dingin.


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

## 7. ID spreadsheet: status terbaru (yang disanitasi hanya nilainya, bukan keberadaannya)

ID spreadsheet Google masih dipakai sebagai **nilai default di dalam kode sumber**
(`sheet_logger.py:22`), sehingga berkas kode publik memuatnya. Dokumen ini tidak mengulang nilainya;
bila perlu dirujuk, gunakan sidik jarinya: `len=44`, `sha8=06bded7f`.

Fakta terukur 2026-11-20 yang mengubah status item ini dari "kebersihan" menjadi **korelan S1**:

- membaca lewat ekspor CSV dari lingkungan lokal terhadap ID default itu → **HTTP 401 untuk keempat
  tab** (artinya seluruh spreadsheet tidak untuk baca publik);
- membaca di runner CI (ID dari secret) → **HTTP 404** untuk `Closed_Positions` dan `PnL_Tracker`;
- `README` §172 mengklaim "Tanpa Hardcode Sheet ID" — kontradiksi dengan kode, dan harus dikoreksi;
- `.env` lokal tidak memuat `GOOGLE_SHEET_ID` maupun `GOOGLE_SHEET_WEBHOOK_URL` → penulisan dari
  lokal tidak pernah mencapai sheet mana pun;
- karena 401 (lokal) dan 404 (CI) adalah dua perilaku berbeda, **belum terbukti** kedua lingkungan
  memakai spreadsheet yang sama.

Yang akan dilakukan (sudah masuk ronde 6, [R6-1]): hapus default dari kode, `GOOGLE_SHEET_ID` wajib
dari konfigurasi, kegagalan baca menjadi warning teragregasi yang menyebut status HTTP, dan koreksi
README. Keputusan yang masih harus diambil pemilik: mana yang jadi sumber kebenaran — spreadsheet,
CSV lokal, atau API bursa. Arah yang dipilih ronde 6: **API bursa** sebagai otoritas riwayat,
sheet sebagai cermin baca-manusia.

## 8. Cara memakai jejak ini

- **Ingin tahu apa yang berubah dan kenapa:** §1, §3, dan §3b; SHA commit ada di `git log` dengan tag
  `[P0-*]`, `[P1-*]`, `[P2-*]` (ronde 2), `[R3-n]` (ronde 3), `[R4-n]` (ronde 4), `[R5-n]` (ronde 5),
  `[R6-n]` (ronde 6, belum).
- **Ingin memverifikasi klaim:** §1 metode + §4 bukti. Jangan mengandalkan laporan agen; jalankan
  ulang `pytest -q`, guard diff, dan pembacaan CI dari GitHub API.
- **Ingin melanjutkan pekerjaan:** prompt eksekusi per ronde berada di mesin pemilik
  (`Antigravity_prompt.md` + arsip), dan sengaja tidak di-commit karena repo ini publik.
- **Angka di §3 (N2) bukan laporan performa, dan §5 tidak membuatnya jadi performa.** Ronde 5 sudah
  menghapus artefak fixture (ledger lokal kini hanya header), jadi `stats` menunjuk 0 trade. Yang riil
  justru **belum terbaca sama sekali**: bursa melaporkan 52 posisi closed (S1) sementara sheet tidak
  bisa dibaca di kedua dunia. Rekam jejak baru bisa dihitung setelah [R6-1], dan untuk riwayat hasil
  backfill PnL-nya jujur berstatus **belum terukur** (endpoint tidak menyediakan harga keluar).
- **Sebelum menyetel strategi market, dua hal ini harus hijau:** [R6-1] (riwayat terbaca + kegagalan
  berbunyi) dan [R6-2] (state risiko tidak bisa hilang senyap). Tanpa keduanya, efek perubahan SL/TP/
  cadence tidak mungkin diukur, dan limit harian bisa tereset tanpa siapa pun tahu.

---

*Dokumen ini disusun dari hasil verifikasi independen ronde 2-5 dan rencana ronde 6, lalu disanitasi
oleh scrubber yang memuat nilai konfigurasi lokal ke memori semata-mata sebagai pola penghapusan.
Scrubber-nya sendiri diuji dengan menanam satu nilai `.env` asli + sembilan pola rahasia sintetis:
seluruhnya terdeteksi (10 kecocokan pola + 1 literal, verdict LOLOS), jadi "0 kecocokan" pada dokumen
ini berarti bersih — bukan berarti patrolinya mati. Sidik jari (`sha8`) sengaja dipakai sebagai
pengganti nilai agar jejak audit tetap bisa dicocokkan tanpa membocorkan identifier.*
