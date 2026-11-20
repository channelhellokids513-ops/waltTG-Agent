# AUDIT — walt-trading: Jejak Perbaikan Ronde 2 sampai 6

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
| 6 | `67438a8`..`f7ba62d` (6 commit + 3 `[R6-fix]`) — **dieksekusi sendiri oleh inspector**, bukan agen | Kebenaran data: ledger dari API bursa, integritas cache CI, cakupan watchdog, least-privilege CI, sisa kebersihan | 124 → **194 lulus**; `ledger_delta = 0` & hash ledger tidak berubah setelah `pytest`; guard anti-pencemaran **MERAH** untuk 2 jalur state baru; watchdog pertama yang success dalam sejarah (#44718306490); **dua keputusan ronde ini terbukti salah oleh run nyata dan sudah dikoreksi** (lihat §3c) |

### 1.1 STATUS OPERASIONAL SAAT INI — butuh tindakan pemilik (P0)

Sistem **tidak bisa trading** sejak 2026-11-20 10:20 UTC: `WALT_REFRESH_TOKEN` di GitHub Secret
sudah terbakar dan tidak ada salinan yang hidup (`.env` lokal + berkas cadangannya adalah token yang
sama dan juga sudah mati). Penyebabnya adalah keputusan saya di Item 4 ronde ini — lihat **S3** di §3c.

**Status per 11:23:25 UTC:** `MAINTENANCE_MODE=true` sudah saya set (atas laporan pemilik bahwa alert
`invalid_grant` terulang tiap 10 menit). Efeknya run API di-*skip*, bukan gagal berisik — jadi
**jangan lupa** mengembalikannya ke `false` setelah token baru dipasang, atau sistem berhenti tanpa
suara. Posisi terbuka nol (`positions_checked: 0` pada run success terakhir), jadi tidak ada proteksi
trailing yang sedang tertunda karena kill-switch ini.

**Per 12:10 UTC:** operasi bursa MASIH mati (butuh `setup_walt_oauth.py` oleh pemilik — lihat §3d),
tapi jalur baca ledger SUDAH HIDUP di kedua dunia: pemilik memberi akses *Viewer* dan secret
`GOOGLE_SHEET_ID` di CI ternyata menunjuk sheet yang salah (sudah dikoreksi). `diagnostics.yml` kini
mencetak probe sheet + `stats` **sebelum** langkah bursa, jadi laporan performansi tetap keluar
walaupun token mati (#44733612689: dua langkah pertama success, langkah bursa failure).

Pemulihan (butuh interaksi pemilik, tidak bisa saya kerjakan):

```powershell
cd 'c:\Users\...\walt-trading'
gh variable set MAINTENANCE_MODE --body "true"   # hentikan badai run gagal + alert tiap 10 menit
python setup_walt_oauth.py                        # login OAuth baru (browser) -> token segar
python -c "import re,io;p='.env';s=open(p,encoding='utf-8').read();print(bool(re.search('^WALT_REFRESH_TOKEN=',s,re.M)))"
gh secret set WALT_REFRESH_TOKEN                  # tempel token BARU (sama dengan .env hasil login)
gh variable set MAINTENANCE_MODE --body "false"
gh workflow run risk-monitor.yml                  # harus success; siklus berikutnya juga success
```

Catatan: posisi yang sedang terbuka tetap terlindungi TP/SL yang terpasang di bursa; yang berhenti
hanya siklus pemantauan (trailing tidak akan mengencang) dan entry baru.

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

**Status setelah ronde 6:** S1, S1b, S2, W1, V1, V2, V3, C2 **ditutup**; C1 ditutup dengan substansi
berubah (mode read-only kini menolak refresh, bukan membakar lalu membuang — lihat S3); D1 tetap
berlaku dan kini punya penjelasan dua lapis (`ledger_source` + `belum_terukur_pnl`). Yang muncul
sebagai gantinya adalah empat temuan baru di §3c — tiga di antaranya P0, dan satu (S3) adalah outage
yang sedang berlangsung.

## 3c. Temuan SAAT eksekusi ronde 6 (2026-11-20, semuanya dari run nyata)

Ronde ini dijalankan sendiri oleh inspector (Antigravity kehabisan kredit). Empat temuan baru
muncul **hanya** karena pekerjaan dijalankan dan bukan hanya direncanakan; dua di antaranya adalah
kesalahan keputusan ronde ini sendiri yang dipaksa keluar oleh bukti.

| ID | Sev | Temuan | Bukti & status |
|---|---|---|---|
| **S3** | **P0** | **Keputusan "diagnostics read-only" di Item 4 mematikan kredensial CI.** Menyetel `WALT_TOKEN_SYNC=off` menghentikan *penulisan balik* secret, tetapi refresh OAuth tetap **membakar** refresh token single-use. Token baru dibuang → secret memegang token mati → `risk-monitor` gagal `invalid_grant` pada siklus berikutnya, dan tidak bisa menyembuhkan diri. | Run `diagnostics.yml` #44718293679 (10:19:48Z) lalu `risk-monitor` #44718326190 (10:20:02Z) `failure` dengan `TokenManager._refresh HTTP 400: invalid_grant`. **Dikoreksi**: `token_manager.allow_refresh()` kini **menolak refresh** bila persistensi dimatikan (invarian "tidak bisa menyimpan ⇒ jangan membakar"), dan `diagnostics.yml` kembali `on`. Test: `test_mode_off_menolak_refresh_sebelum_http` (nol panggilan OAuth). Konsekuensi jujur yang sekarang tercatat di README: *setiap* run diagnostics mengonsumsi satu rotasi dan menulisnya balik, sehingga `.env` lokal akan selalu tertinggal satu rotasi setelah diagnostik. |
| **S4** | **P0** | **`cache-hit` bukan sinyal yang valid untuk "state kembali utuh".** `actions/cache/restore` hanya memberi `cache-hit=true` bila key **persis** sama; key kita memuat `${{ github.run_id }}` sehingga tidak pernah persis → sinyal selalu `false`. Kalau dibiarkan, gerbang dingin ronde ini akan menolak **semua** entry secara permanen dan menyatakan setiap siklus "cache miss". | Terlihat di log #44718326190: `WALT_STATE_WARM: false` padahal state cache ada (run 10:00/10:10 success dengan cache yang sama). **Dikoreksi**: langkah `state_probe` memeriksa keberadaan `logs/market_regime.json` (+ `logs/open_snapshot.json` untuk risk-monitor) dan output-nya yang dipakai; test `test_workflow_mengekspos_kestabilan_state_lewat_probe_keberadaan` mengunci bentuk ini dan menolak `cache-hit`. |
| **W1b** | **P0** | **Watchdog lama tidak pernah berhasil.** Semua run terjadwalnya `failure`: `gh run list` dipanggil di workspace tanpa `.git` → `failed to determine base repo: fatal: not a git repository`. Artinya perlindungan "monitor macet → alert + dispatch" yang selama ini kita anggap ada, sebenarnya mati — kelas kegagalan senyap yang persis sedang kita berantas. | Riwayat workflow watchdog: 3 run (`2026-11-19T18:11Z`, `2026-11-20T01:10Z`, `2026-11-20T09:55Z`) semuanya `failure`. **Diperbaiki oleh Item 3** (checkout + modul): run pertama yang success dalam sejarah #44718306490 dengan tabel empat workflow. Test regresi: `test_watchdog_yml_checkout_mendahului_langkah_gh` + `test_bukti_historis_defek_watchdog_lama_tidak_ada_checkout` (mengunci fakta bahwa baseline `e68e0eb` memang tanpa checkout). |
| **C3** | **P2** | **Kredensial lokal mati dua-duanya.** Telegram: `TELEGRAM_BOT_TOKEN` di `.env` menghasilkan HTTP 401 → alert lokal tidak pernah sampai. Walt: `.env` dan cadangan `WALT_REFRESH_TOKEN.env` memuat token yang **sama** (sha8 identik, `len=43`) dan sudah `invalid_grant`. | Probe ronde ini mati justru karena **V2**: `notifier.py` mencetak preview ber-emoji di konsol cp1252 → `UnicodeEncodeError` → rc=1 (repro kini jadi test `test_repro_cp1252_jadi_rc_nol`, rc=0). Token: pesan `ALERT OAUTH invalid_grant` tercetak saat percobaan baca bursa. Tidak ada yang bisa dibaca dari bursa secara lokal sampai pemilik login ulang. |

**Yang terverifikasi baik selama eksekusi (tidak perlu diubah):**

- Least-privilege tidak mengorbankan fungsionalitas: dengan `permissions: contents: read` saja,
  langkah `actions/cache/save` tetap menulis cache (`Cache saved with key: walt-logs-state-…`), dan
  `actions/cache/restore` tetap bekerja; Step Summary juga tetap bisa ditulis (bukan scope token).
- Pin SHA action yang diambil dari API GitHub berfungsi di runner (hanya peringatan deprecated
  Node.js 20 dari action-nya sendiri, bukan kegagalan).
- Pengukuran riwayat bursa (Item 1 langkah 1) menghasilkan: **52** posisi closed, rentang
  `created_at` 2026-11-17T08:49:04Z → 2026-11-19T22:52:45Z, `updated_at` sampai 2026-11-19T23:38:33Z,
  0 field angka tidak valid, pembungkus respons hanya `{positions}` tanpa `total`/`cursor`, dan
  skema tool `perps_list_positions` **tidak** mengekspos parameter paginasi →
  **verdict kelengkapan: TIDAK BISA DIVERIFIKASI (mungkin terpotong)**, dan kalimat itu kini dicetak
  oleh perintah, bukan hanya oleh laporan.
- Kegagalan baca ledger kini berbunyi: di CI tercetak `LEDGER TIDAK TERTBACA (http_error) — angka 0
  di bawah BUKAN berarti tidak ada trade` + dua warning teragregasi (Closed_Positions, PnL_Tracker).

**Deviasi dari rencana ronde 6 yang saya lakukan sadar (dan alasannya):**

1. Item 4 butir 2 ("diagnostics benar-benar read-only") diganti menjadi "tolak refresh tanpa
   persistensi" — versi orisinal terbukti merusak kredensial (S3).
2. Item 2 butir 1 ("ekspos `cache-hit`") diganti menjadi probe keberadaan berkas (S4).
3. Item 3 butir 3c ("fallback dispatch untuk workflow stale") dibuat **default alert-only** untuk
   workflow entry, karena dispatch akan membuka order nyata; hanya risk-monitor yang tetap
   di-dispatch otomatis seperti perilaku warisan. Aktifkan lewat `vars.WALT_WATCHDOG_DISPATCH_ENTRY`.
4. Item 2 butir 3 (bukti cache-miss nyata) **belum bisa dikerjakan** sampai kredensial pulih —
   setiap run bursa gagal sebelum tahap deteksi. Ini dicatat sebagai PARSIAL, bukan pura-pura selesai.
5. Satu test ronde 4 (`test_r4_4_...maintenance_gated`) ikut berubah: lokatornya mengunci teks
   heredoc lama dari `watchdog.yml`; invarian yang dijaga (maintenance sebelum alert/dispatch)
   tetap dan kini diperiksa lebih ketat di dalam modul + ada test perilaku terpisah.

## 3d. Jawaban atas lima pertanyaan pemilik (2026-11-20, 11:2x UTC)

| # | Pertanyaan pemilik | Jawaban + bukti |
|---|---|---|
| 1 | "Seharusnya benar itu closed; tidak ada aset dalam posisi" | **Terkonfirmasi.** Run risk-monitor success terakhir #44716888020 (10:10:15Z) mencetak `💤 [SMART IDLE] 0 posisi aktif. Semua order aman/kas cair` dan `positions_checked: 0`. Riwayat closed juga **tidak terpotong pada angka bulat**: `closed_positions_count` = 50 pada diagnostics 2026-11-19T16:25 (#44565856937), lalu 52 pada tiga run berikutnya (06:57 #44691413854, 08:00 #44699103468, 10:19 #44718293679) — angka naik melewati 50 dan kemudian plateau, konsisten dengan "tidak ada penutupan baru + tidak ada posisi terbuka", bukan dengan cap daftar. Sisa batasan jujur: `created_at` tertua yang terlihat adalah 2026-11-17T08:49:04Z, jadi penutupan sebelum tanggal itu tetap tidak bisa dibuktikan dari endpoint ini. |
| 2 | "Butuh debug untuk memastikan strategi berjalan baik" | Instrumen dipasang **dan langsung menemukan bug nyata**. (a) `_probe_close_response_shape` kini mencatat BENTUK respons `execute` close (nama kunci + tipe, nol nilai) setiap kali harga keluar jatuh ke fallback lokal — satu baris per bentuk unik, plus penghitung di Step Summary; pertanyaannya jadi terjawab oleh penutupan riil berikutnya tanpa perlu DEBUG manual. (b) Saat menulis test bentuk respons itu, terbukti respons dengan bentuk `order.avg_close_price` **tidak terbaca** oleh `_extract_close_exit_price`: kunci harga penutupan hanya dicari di top-level dan di objek `position`, tidak di dalam `order` — sehingga sistem diam-diam memakai `snapshot_estimate` (harga lokal) sebagai harga keluar, bukan harga dari bursa. Diperbaiki (presedensi kelas kunci dipertahankan: fill > response_position > mark > snapshot > entry), dengan test `test_regresi_harga_penutupan_di_dalam_order_tidak_lagi_dilewatkan` + 201 test lama-baru hijau. Tidak ada nilai strategi yang disentuh. |
| 3 | "Beritahu langkah pemulihan kredensial" | Ditulis di README §"Panduan Setup" (butiran baru, baris 234+). Inti yang tadinya tidak terdokumentasi: bila secret **dan** `.env` sudah sama-sama terbakar, kill-switch tidak menolong — harus `python setup_walt_oauth.py`, yang **mendaftarkan klien OAuth baru (DCR)** sehingga **`WALT_CLIENT_ID` juga berubah** dan ikut harus di-`gh secret set`. `MAINTENANCE_MODE=true` sudah saya set pada 11:23:25Z untuk menghentikan badai alert; **jangan lupa kembalikan ke `false`** setelah token diganti, atau sistem berhenti diam-diam. |
| 4 | "GCP tidak pakai VM, hanya Scheduler yang aktif" | Dicatat. Konsekuensi audit: tidak ada proses pemantau di sisi Google, sehingga satu-satunya sinyal hidup pemicu adalah ada/tidaknya run GitHub — persis yang kini diukur watchdog per workflow (ambang berbasis cadensi). Verifikasi job langsung (dari laptop pemilik, `gcloud`, tanpa VM): `gcloud scheduler jobs list --location=asia-southeast2` dan `gcloud scheduler jobs describe walt-risk-monitor --location=asia-southeast2`. |
| 5 | "Sheet ID ini yang konek" | ID yang Anda kirim adalah yang dulu di-hardcode: panjang 44, **sha8 `06bded7f`**. Saya uji jalur bacanya **tanpa kredensial Walt** (gviz tidak butuh token Walt): keempat tab menjawab **HTTP 401 + halaman login** → sheet-nya **privat**. Jadi penyebab ledger tidak terbaca bukan ID salah, tapi level berbagi; daftar nama secret di CI menunjukkan `GOOGLE_SHEET_ID` memang ada sebagai secret di CI, dan `dump_closed` sekarang mencetak `sheet_fingerprint` (configured/length/sha8) supaya ID yang dipakai CI bisa dibandingkan dengan `06bded7f` tanpa pernah menampilkan nilainya. Dua opsi keputusan pemilik ada di README; sementara itu API bursa tetap sumber kebenaran, dan tulis ke sheet tetap jalan karena Apps Script berjalan sebagai pemilik (webhook = kredensial tulis, bukan baca publik). |

**Status kill-switch saat ini:** `MAINTENANCE_MODE=true` (diset 2026-11-20T11:23:25Z oleh inspector untuk membungkam alert `invalid_grant` tiap 10 menit). Efeknya terverifikasi: run risk-monitor 11:20:02Z masih `failure`, sedangkan **#44728094801 pada 11:30:02Z `skipped`** — tidak ada lagi alert ber-ulang. `CI Unit Tests` pada SHA kerja terakhir (#44728910443) **success** dengan 201 test.

Ini **bukan** keadaan normal: sistem tidak akan memantau atau membuka posisi sampai kredensial dipulihkan dan variabel dikembalikan ke `false`.

## 3e. Jalur baca sheet HIDUP — dan langsung mengubah kesimpulan (2026-11-20, 11:4x-12:0x UTC)

Pemilik memberi akses **Viewer** pada spreadsheet. gviz export kini menjawab `HTTP 200 CSV` untuk
keempat tab (sebelumnya 401). Ini menjawab tiga pertanyaan yang selama ini berstatus
"TIDAK BISA DIVERIFIKASI", dan sekaligus membuka satu temuan akuntansi baru.

| Yang sebelumnya tidak terjawab | Sekarang | Bukti |
|---|---|---|
| Apakah tab `Closed_Positions` ada di target Apps Script? | **ADA** | `probe`: `Closed_Positions ok rows=17 header=14kol (10 bernama)` — header hanya menamai 10 kolom pertama, tapi baris data punya 14 field (kolom 11-14: Position ID, Closed At, Strategy Version, Opened At). |
| Apakah webhook Apps Script pernah menulis `Closed_Positions`? | **PERNAH, tapi tidak lengkap** | 17 baris nyata, stempel terakhir 2026-11-19T23:40Z, hash strategi `d11f2da8` terisi di baris baru. |
| Berapa riwayat riil yang tercatat? | **Bursa 52 closed vs sheet 17 baris → selisih 35 penutupan tidak pernah masuk ledger.** | Angka 52 dari `history_measurement` diagnostics; 17 dari census sheet. Inilah bukti operasional bahwa ketergantungan pada cache Actions (S2) benar-benar memakan rekam jejak, bukan hanya risiko teoretis. |
| Apakah sheet yang dipakai pemilik sama dengan yang dipakai CI? | **TIDAK.** Inilah punca sebenarnya dari beda perilaku 401-vs-404 di brief ronde 6: secret CI memegang ID yang berbeda (sha8 `c8899991`, panjang **45**), sedangkan sheet yang konek punya pemilik adalah sha8 `06bded7f`, panjang **44**. Dari ID CI itu keempat tab menjawab `http_error` (bukan 401) — sheet-nya memang tidak ada. | `diagnostics.yml` #44731850062 mencetak `Sheet fingerprint: sha8=c8899991 length=45` + `http_error` keempat tab, sementara probe lokal dengan ID pemilik mencetak `sha8=06bded7f length=44` + `ok rows=17`. Secret kini dikoreksi (`gh secret set GOOGLE_SHEET_ID`) — nilai lama tidak bisa dicadang karena secret GitHub write-only, tapi sidik jarinya tercatat di baris ini. |

**Perbaikan ikutan yang ditemukan oleh test saya sendiri:** `tools/sheet_probe.py` sempat membaca `.env`
pengembang di dalam `main()`, sehingga satu test mengirim **HTTP sungguhan** ke sheet asli (read-only,
tanpa efek, tapi itu bahaya bocornya kredensial ke proses test). Sekarang: `.env` hanya dibaca pada blok
`__main__` (CLI), `main()` murni argumen+environment, dan guard `test_main_tidak_pernah_membaca_env_file_pengembang`
mengunci bentuk itu. Bersamaan itu `walt_bot.py` akhirnya punya pemuat `.env` yang jujur
(`load_local_env()`, **khusus CLI `__main__`**, tidak pernah menimpa env yang sudah disuntik CI dan tidak
pernah mencetak nilai) — tanpa ini, perintah lokal yang saya minta Anda jalankan (`reconcile_history`,
`stats`) akan gagal karena tidak ada konfigurasi sama sekali. Suite: **215 lulus**.

**Temuan akuntansi baru (`S5`, P1): angka nol yang menyamar sebagai data.** Dari 17 baris
`Closed_Positions`, **9 baris** memiliki `Exit Price ($) == Entry Price ($)`, `PnL ($) = 0`, dan
**tanpa** kolom sumber harga (baris 14 kolom, warisan penulis pra-Ronde 5 yang memakai harga masuk
sebagai fallback harga keluar). Sebelum ronde ini, `stats` menghitungnya sebagai **LOSS**: laporan
sebelumnya membaca `Total Trade Valid: 11 (Win 2 | Loss 9)`, `Avg Loss: $0.0000 (0.00%)` — yaitu 9
"kerugian" yang sebenarnya *tidak ada datanya*. Setelah koreksi:

```
Sumber ledger: ok | PnL terukur: 2 | belum terukur (backfill bursa): 0 | diragukan (PnL=0 tanpa sumber harga): 9
Total Trade Valid: 2 (Win: 2 | Loss: 0) | UNKNOWN: 6
```

**Yang perlu digarisbawahi:** **impas riil tetap dihitung** — syarat `diragukan` memerlukan
`Exit == Entry` **dan** `PnL == 0` **dan** sumber harga kosong/`entry_fallback`. Test
`test_census_breakeven_riil_bukan_baris_diragukan` dan
`test_stats_tetap_menghitung_impas_dari_fill` mengunci sisi itu; 211 test hijau.

**Terverifikasi di CI setelah secret dikoreksi** (#44733612689, 2026-11-20T12:10:47Z — langkah
`Sheet read probe` dan `Ledger stats` **success**, hanya langkah bursa yang gagal karena token mati):

```
Sheet fingerprint: sha8=06bded7f length=44 (ID tidak pernah dicetak)
Sumber ledger: ok | PnL terukur: 2 | belum terukur (backfill bursa): 0 | diragukan (PnL=0 tanpa sumber harga): 9
Total Trade Valid: 2 (Win: 2 | Loss: 0) | UNKNOWN: 6
```

Ini membalikkan keadaan brief ronde 6: kalimat "LEDGER TIDAK TERTBACA" masih ada di kode (dan akan
kembali muncul kalau sheet/secret bermasalah), tapi hari ini `stats` di CI **membaca sumber asli**
tanpa menyentuh OAuth. Konsekuensi yang harus dibaca dingin: perubahan ekuitas yang tercatat
`PnL_Tracker` pada periode ini ≈ **-2,05%** dari saldo awal (4 baris harian), sedangkan `Net PnL`
hasil kalkulasi hanya **+$0,0356** karena ia cuma menutupi 2 trade terukur — selisihnya adalah 35
penutupan yang tidak ada di ledger + 9 baris nol-tanpa-sumber. **Itu bukan bug kalkulasi, itu
lubang data.** Ronde 7 tidak boleh menyimpulkan apa pun tentang kualitas strategi sebelum lubang ini
ditutup (`reconcile_history --write` setelah kredensial pulih), dan baris-warisan `diragukan`
dipertahankan apa adanya — tidak diedit senyap.


**Ringkasan akhir:** dengan 2 trade terukur, 6 UNKNOWN, dan 35 penutupan yang tidak ada di ledger sama sekali, **performa strategi saat ini BELUM TERUKUR**.
Perubahan ekuitas periode berjalan (± -2% dari saldo awal) juga tidak bisa dijajarkan dengan
`Net PnL` laporan, karena estimasi fee kini hanya menutupi trade terukur. Ronde 7 (strategi market)
**wajib** dimulai dari kelengkapan riwayat, bukan dari tuning SL/TP: jalankan
`reconcile_history --write` setelah kredensial pulih (52 baris masuk sebagai `belum_terukur_pnl`),
lalu biarkan satu sampai dua minggu penutupan berjalan dengan `exchange_fill`/`response_position`
terisi sebelum menilai efek perubahan parameter.



**Rekap run sejak baseline `e68e0eb` (diukur 12:13 UTC, 63 run):** 14 `failure`, semuanya terjelaskan
dan tidak ada yang misterius — 7× `risk-monitor` + 1× `crypto-execute` (outage kredensial S3,
10:20–11:20Z), 4× `diagnostics` (gagal **hanya** di langkah bursa; langkah probe & stats success —
inilah perilaku yang sekarang diuji `test_diagnostics_stats_langkah_tanpa_token_dan_terbaca_di_log`),
1× `CI Unit Tests` (bug harness decode cp1252, sudah diperbaiki pada commit berikutnya), 1×
`watchdog` (cacat historis W1b yang justru ditemukan karena kegagalan ini). Kriteria ronde "0 failure
sejak baseline" **tidak terpenuhi**, dan penyebab dominannya adalah keputusan saya sendiri di Item 4.

## 3f. Sistem HIDUP kembali — dan tiga siklus pertama berbicara keras (2026-11-20, 13:35–14:10 UTC)

Pemilik menyelesaikan OAuth (secret `WALT_CLIENT_ID` 13:34:54Z, `WALT_REFRESH_TOKEN` 13:35:30Z).
Verifikasi berurutan, semuanya dari run nyata:

| Bukti | Hasil |
|---|---|
| Rantai rotasi token single-use (S3/R6-4) di produksi | `POST oauth/token 200` → `WARNING refresh_token di-rotate` → `PUT actions/secrets/WALT_REFRESH_TOKEN 204` ✅ write-back bekerja |
| `diagnostics.yml` pertama yang **seluruhnya hijau** | #44748280440 (`success`): probe sheet ✅, stats ✅, dump_closed ✅, reconcile `skipped` oleh gerbang mode ✅ |
| Baseline riwayat dari API bursa | #44748514800 mode `reconcile_history`: `rows_seen=52`, `already_recorded=6`, `candidates_to_add=46`, `written=0`, `backup=null` — dry-run dihormati |
| Guard risiko hidup kembali | 3 siklus `success` beruntun: 13:58 (5m21s), 14:00 terjadwal (3m49s, **mengantre** bukan membatalkan — `cancel-in-progress: false` terbukti), 14:07 (29s) |
| Pelepasan kill-switch | `MAINTENANCE_MODE=false` 13:57:51Z **bersama** `DRY_RUN=true` 13:57:48Z — monitoring nyata, entry masih simulasi |

**Tiga cacat yang baru terlihat begitu sistem hidup** (ini gunanya menjalankan, bukan hanya membaca kode):

1. **Voter 1 swarm mati 100%.** `Voter 1 (Qwen-Turbo) error: 404 Not Found` untuk SETIAP aset
   (NBIS, NVDA, SPY, VVV), sementara V2 (`qwen-plus`) memakai endpoint yang sama dan **tidak** error
   → punca = model `qwen-turbo` tidak lagi dilayani di `dashscope-intl`, bukan URL/kunci. Swarm 4 voter
   kini berjalan 3 voter; ini juga menjelaskan kolom `Sentimen/Konsensus/Vendor Alignment` berisi `—`
   pada baris `Daily_Scans` tanggal 20-11. Yang paling mengganggu: kegagalan ini **tidak masuk
   `_log_failure_summary`** sehingga siklusnya tetap `success` — kelas "diduga hidup padahal mati".
2. **`fetch_earnings_days` 401 untuk semua ticker** (`query1.finance.yahoo.com/v10/finance/quoteSummary`)
   → proteksi "High Risk Catalyst (earnings)" buta data. Perlu sumber kalender earnings yang sah.
3. **Webhook riset Apps Script mengembalikan non-JSON** (`Expecting value: line 1 column 1`) → jalur
   baca riset jatuh ke fallback senyap.

**Akar masalah durability yang akhirnya terjelaskan (temuan terpenting ronde ini).** Percobaan membuktikan
start dingin dengan menghapus SELURUH cache `walt-snapshot-*` justru menghasilkan `snapshot ADA`: cache
`logs/` (`path: logs/`, baris 34 risk-monitor) sudah memuat `open_snapshot.json`, sehingga cache snapshot
khusus itu **redundan**. Kesimpulan yang lebih penting: **ledger, `closed_ids`, dan snapshot semuanya
hidup di Actions cache** — dan cache di-eviksi (7 hari tak terpakai / kuota 10 GB; daftar cache hari ini
hanya berisi entri 09:30→14:03, yang lebih tua sudah lenyap). Setiap eviksi = amnesia total = close tidak
bisa di-diff = **tidak pernah tercatat**. Itulah penjelasan kuantitatif 52 (bursa) vs 17 (sheet):
bukan teori, melainkan cara kerja sistem. Perbaikannya bukan menambah deteksi, tapi **rehidrasi memori
dari sumber tahan-lama saat cold start** (ID tertutup dari kolom `Position ID` sheet yang kini terbaca,
dan/atau backfill sadar dari riwayat bursa) — diusulkan sebagai Item 1 Ronde 7, dengan label jujur
`exchange_history_no_exit`, tanpa PnL karangan.

**Koreksi diri atas rencana verifikasi:** bukti cache-miss yang tertunda sejak Ronde 6 TIDAK bisa
diperoleh dengan menghapus cache snapshot saja (karena redundansi di atas), dan menghapus cache `logs/`
berarti memusnahkan satu-satunya salinan ledger di CI (6 close nyata) — jadi sengaja TIDAK saya
lakukan. Path dingin tetap terbukti di level unit (`test_gerbang_entry_dingin_konservatif_dan_override`,
marker fail-closed baru); bukti end-to-end ditunda sampai ada jalur rehidrasi, yang justru membuat
keadaan dingin tidak lagi berbahaya.

## 3g. Pra-Ronde 7 — input yang mati dihidupkan, dan nol palsu diburu (2026-11-20, 14:30–15:45 UTC)

Pemilik memilih **Opsi B**: perbaiki input dulu, baru lepas `DRY_RUN`. Semua keputusan di bawah
didasarkan pada **probe langsung**, bukan asumsi.

### Probe model DashScope (penyebab voter V1 mati)

| Model | HTTP | Kesimpulan |
|---|---|---|
| `qwen-turbo` | **404** `{"code":"model_not_found"}` | persis penyebab `Voter 1 (Qwen-Turbo) error` di 4/4 aset |
| `qwen-turbo-latest` | 403 `access_denied` | bukan jalan keluar (akun tidak punya akses) |
| **`qwen-flash`** | **200** | pengganti yang benar (generasi penerus turbo) |
| `qwen-plus` / `qwen-max` | 200 / 200 | V2 dan meta-aggregator sehat sejak awal |
| `qwen-plus-latest` | 403 "free quota exhausted" | varian `-latest` dihindari sebagai default |

Perbaikan: `MODEL_V1 = os.getenv("DASHSCOPE_MODEL_V1", "qwen-flash")`, V4/aggregator ikut dapat
dioverride, dan fallback 429 diperluas ke **404** (dengan alasan berbeda di log). 500 sengaja TIDAK
memicu ganti model — itu bukan sinyal "model mati". **Hasil terverifikasi langsung:**
`voters_count = 4 | voter_failures = []` untuk NVDA (V1–V4 + aggregator `qwen-max`, semua 200).

### Jebakan "nol" pada kalender earnings (kelas bug yang lebih umum dari 401)

`fetch_earnings_days` lama memetakan tanggal berapa pun ke `max(0, hari)`. Setelah 401 beres
(alur: `fc.yahoo.com` untuk cookie → `/v1/test/getcrumb` → `quoteSummary?...&crumb=` **pada klien
yang sama**; terbukti crumb + klien baru tetap 401), data asli menunjukkan:

| Ticker | `earningsDate` | Interpretasi lama | Sekarang |
|---|---|---|---|
| NVDA | 2026-11-17 (**sudah lewat**) | `H-0` → **SKIP permanen** | `(None, "past_only")` |
| AAPL | 2027-01-28 | `H-69` | `(69, "ok")` |
| SPY | 404 (ETF tanpa kalender) | `no_data` diam-diam | `(None, "no_data")` — BUKAN outage |

Ini menjelaskan sebagian SKIP yang selama ini tampak "bijak": tanggal rilis terakhir yang mengisi
slot "berikutnya" akan membekukan aset dari entry selamanya. Aturan yang sama menjerat ETF: 404
adalah "tidak ada kalender", bukan "sumber data mati", sehingga tidak ikut dialarm.

### Register kesehatan input (prinsip: hijau = semua yang dipakai benar-benar hidup)

`walt_bot.record_research_health()` / `flush_input_health()` mengumpulkan, sekali per siklus:
`input.voter_swarm`, `input.earnings_source`, `input.riset_error`, `input.riset_hilang`,
`input.riset_webhook` — lewat `_log_failure_summary` (satu baris + satu alert bila ≥3 item dan
≥30%), lalu tampil di `logs/last_safe_summary.json` dan *Step Summary* sebagai `input_health`
(dict kosong = bersih) dan `input_meta` (mis. `research_read_status`, `cold_rehydrate`).
`no_data`/`past_only` sengaja TIDAK dialarm: itu kondisi data, bukan kerusakan sensor.

Jalur baca riset Apps Script juga tidak lagi senyap: balasan 200-non-JSON kini tercatat
`html_or_unparsable` (dengan peringatan 15-menit), fallback ke CSV lokal terlihat sebagai
`local_csv`, dan webhook sukses-tapi-basi semua terlihat sebagai `empty`.

### Start dingin tidak lagi berarti lubang permanen di ledger

`WALT_COLD_REHYDRATE` (default `report` = perilaku lama). Mode `write` menutup jarak memakai
**satu jalur yang sudah ada** (`reconcile_history(write=True)`: backup → label jujur → tanpa webhook),
tetap **diblokir saat `DRY_RUN=true`**, dan nilai env tak dikenal jatuh ke `report` (fail-closed untuk
penulisan). Variable sudah diset eksplisit di repo: `WALT_COLD_REHYDRATE=report`, `DASHSCOPE_MODEL_V1=qwen-flash`.

### Koreksi diri selama ronde ini (semuanya tertangkap oleh guard, bukan oleh keberuntungan)

1. Karakter CJK menyelinap ke docstring saya sendiri → dihapus; gerbang sanitasi sekarang juga
   memeriksa berkas Python yang disentuh, bukan hanya dokumen.
2. Test fallback 404 saya **awalnya salah** (lolos karena menyimpan referensi payload yang sama):
   diperbaiki dengan menyalin payload.
3. Non-vakuitas dibuktikan dengan mutasi: `_dry_run_requested()` saya paksa `False` →
   tepat `test_mode_write_diblokir_saat_dry_run` yang gagal, lalu mutasi dikembalikan.
4. Dua test higiene milik ronde 6 menegur saya dan **saya yang menyesuaikan diri**, bukan test-nya:
   env baru wajib terdokumentasi (`.env.example` + tabel variable README) dan skrip probe tidak boleh
   tertinggal di `logs/` (probe kini ditulis ke temp luar repo).
5. Crumb sesi Yahoo hampir tercetak di log CI publik lewat INFO `httpx` → filter redaksi dipasang.
6. README saya perbaiki: klaim "sheet privat → gviz 401" sudah **basi**; fakta terverifikasi
   13:53:42Z adalah `Sumber ledger: ok` (punca lama = ID sheet salah, `sha8=c8899991`).

### Yang masih terbuka setelah ronde ini

- `DRY_RUN=true` (keputusan pemilik berikutnya, sesudah >=1 siklus hijau dengan swarm 4/4).
- Backfill 46 baris belum ditulis; `WALT_COLD_REHYDRATE=report`.
- Penjelasan 4 SKIP siklus 13:58Z (bukan bug): 3× `gerbang ekonomi funding vs fee`
  (exp_funding 0.61–0.85% < cost 1.18% untuk hold 24h) dan 1× `High Risk Catalyst (P5=0.81)`.

Ukuran suite: **223 → 253 test** (+30), semuanya lulus; ledger lokal tidak disentuh
(`logs/closed_history.csv` tetap `C9BFC1D811B6`).

### Verifikasi di produksi (setelah `752d674` di-push, run `market-execute` #44766704052, 15:47–15:48 UTC)

| Hal | Sebelum (13:58Z) | Sesudah (15:47Z) |
|---|---|---|
| Voter V1 | `Voter 1 (Qwen-Turbo) error: 404` untuk 4/4 aset | **tidak ada satu pun galat voter**; `input_health` bersih dari `input.voter_swarm` |
| Kalender earnings | `fetch_earnings_days ...: 401` untuk 4/4 ticker | `GET /v1/test/getcrumb → 200` lalu `quoteSummary/NVDA`, `/NBIS` terambil; **bersih dari `input.earnings_source`** |
| Gelombang kegagalan input | tidak ada | `"input_health": {"input.riset_webhook": {"failed": 1, "examples": ["html_or_unparsable"]}}` + `"input_meta": {"research_read_status": "html_or_unparsable"}` |
| Ledger | — | `Sumber ledger: ok` (jalur gviz tetap sehat) |

Dua hal yang justru **baru terlihat** karena sensornya dipasang:

1. **Webhook riset Apps Script memang mati, dan kini terbukti di CI** (sebelumnya hanya fallback
   senyap). `GET /exec → 302 → script.googleusercontent.com/macros/echo` dijawab dengan HTML,
   sehingga pembacaan riset selalu jatuh ke CSV lokal. Sumber di repo (`google_apps_script.js`)
   **sudah memiliki `doGet` yang lengkap** — jadi yang berbeda adalah *deployment*-nya, bukan
   kodenya. Diagnosis kini diberi nama (`deployment_tanpa_doGet` / `butuh_akses_publik` /
   `halaman_html_apps_script`) dan muncul di *Step Summary* lewat `research_read_diagnosis`;
   tindakan untuk pemilik: **Deploy → Manage deployments → Edit → New version** dan pastikan
   *Who has access = Anyone*. Alternatif yang lebih tahan lama (usulan ronde berikutnya): baca
   `Daily_Scans` lewat **gviz CSV** yang sudah terbukti `ok`, sehingga jalur baca tidak bergantung
   pada deployment Apps Script sama sekali.
2. **Token sesi Apps Script ikut tercetak di log CI.** `user_content_key=...` pada URL
   `googleusercontent` muncul apa adanya karena GitHub hanya membungkam nilai secret yang terdaftar,
   bukan parameter hasil redirect. Filter redaksi log diperluas dari `crumb=` menjadi
   `crumb=|user_content_key=` (+test). Catatan jujur: nilai token yang saya pakai di test adalah
   **sintetis** — sampel nyata yang sempat saya salin dari log sengaja diganti agar tidak masuk
   repo publik.

Setelah penambahan ini: **256 test** (253 + 3 test diagnosis/redaksi), semuanya lulus.

### Insiden 16:00Z — ras antar-konsumen OAuth yang SAYA timbulkan sendiri, dan apa yang itu ajarkan

Dispatch manual `market-execute` yang saya kirim pada **16:00:10Z** start 8 detik setelah
`risk-monitor` terjadwal 16:00:02Z. Run saya gagal `invalid_grant` (HTTP 400) — **bukan** karena
perubahan kode, melainkan karena token refresh bersifat single-use: risk-monitor sudah membakarnya
dan menulis token baru, sedangkan secret di job saya sudah di-resolve sebelum rotasi terjadi.

Fakta penting yang tercatat dari run nyata:
- run gagal **sendirian**; `risk-monitor` 16:00:02Z **sukses** → secret sehat, sistem pulih sendiri
  pada siklus berikutnya. Tidak ada order (masih `DRY_RUN`), tidak ada baris ledger yang tertulis.
- `concurrency:` GitHub hanya mengurusi **satu workflow** — jadi lintas-workflow tidak pernah
  terlindungi. Perbaikan "tepat satu konsumen OAuth per job" (R6-followup) hanya menutup separuh
  masalah; separuh lainnya adalah *antar* job.
- membaca kembali secret untuk pemulihan otomatis **tidak mungkin** (API GitHub hanya mengembalikan
  nilai terenkripsi), sehingga "self-heal" dengan mengambil token terbaru bukan opsi yang jujur.
  Yang realistis: jangan salah baca gejalanya.

Tindakan yang saya ambil: alert `invalid_grant` kini **membedakan dua sebab** dan memberi perintah
pembedanya (`gh run list --limit 6`), karena pesan lama mengarahkan operator ke re-auth — padahal
re-auth justru membuat `.env` lokal tertinggal satu rotasi. README memuat ciri-ciri ras dan
larangan dispatch manual pada menit kelipatan 10. Saya berhenti men-dispatch manual setelah kejadian
ini; diagnosis webhook riset akan datang dari jadwal alami 16:43Z.

Lesson yang saya catat untuk diri sendiri: **saya adalah sumber tabrakan baru** di sistem yang
token-nya single-use. Verifikasi produksi harus memakai siklus terjadwal, bukan mempercepatnya
dengan dispatch beruntun.

Setelah ini: **257 test**, semuanya lulus.

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

## 5b. Ronde 6 — ruang lingkup (DIEKSEKUSI 2026-11-20 oleh inspector; hasil per item di bawah)

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

**Hasil per item (diukur, bukan dilaporkan):**

| Item | Status | Bukti utama |
|---|---|---|
| **[R6-1]** | **SELESAI** | `reconcile_history` + `describe_closed_history` terpasang dan **sudah dicetak dari CI**: 52 closed, `created_at` 2026-11-17T08:49:04Z→2026-11-19T22:52:45Z, wrapper `{positions}` tanpa total/cursor, `tool_pagination_params=[]`, 0 field angka tak valid → verdict **TIDAK BISA DIVERIFIKASI (mungkin terpotong)**. `stats` di CI kini mencetak `LEDGER TIDAK TERTBACA (http_error) — angka 0 di bawah BUKAN berarti tidak ada trade` + 2 warning teragregasi. `DEFAULT_SPREADSHEET_ID` **0 kemunculan** di seluruh berkas yang di-track (dulu 2: `sheet_logger.py:22`, `google_apps_script.js:5`). Test baru Item 1: 17, termasuk `--write` dua kali → delta baris 0, dan `no_config` → nol panggilan `httpx`. |
| **[R6-2]** | **PARSIAL (karena S3/S4)** | Kode + gerbang terpasang dan teruji: nol baris close dari start dingin **dengan snapshot basi** (dan non-vakumnya dibuktikan: dalam kondisi hangat snapshot basi memang menghasilkan 1 pencatatan), alert dingin 1x untuk 8 kandidat, override `WALT_ALLOW_COLD_ENTRY`, marker `state_warm` di Step Summary keempat workflow. **Dua koreksi dipaksa run nyata**: sinyal harus dari probe keberadaan berkas, bukan `cache-hit` (S4), dan bukti cache-miss nyata belum bisa diambil karena setiap run bursa gagal di OAuth (S3). |
| **[R6-3]** | **SELESAI** | Run watchdog **#44718306490 = success pertama dalam sejarah workflow itu** (3 run terjadwal sebelumnya semuanya `failure`, W1b). Tabel di Step Summary berisi keempat workflow + usia nyata: risk-monitor 9.7m/30m OK, market-execute 34.0m/300m OK, crypto-execute 68.7m/300m OK, daily-scan 600.1m/840m OK → "SEMUA Workflow SEHAT — tidak ada alert, tidak ada dispatch". Ambang punya rujukan cron yang diuji terhadap `STRATEGY.md` §7.1, bukan angka karangan. |
| **[R6-4]** | **SELESAI dengan substansi diubah** | `permissions:` **7/7** workflow; `uses:` bertag mayor **0**; requirements `==` (httpx 0.28.1, pynacl 1.6.2, pytest 9.2.0, pyyaml 6.0.3; sama dengan `pip freeze` Python 3.14.7 dan CI hijau dengan kunci itu). TAPI BUTIR 2 ORISINAL TERBUKA SALAH: `off` pada diagnostics mematikan kredensial CI → kini `off` = **tolak refresh**, diagnostics `on`. Bukti negatif tercapai (0 `PUT .../actions/secrets` di log diagnostics #44718293679) tapi harganya nyata, dan itulah yang mengubah desain. Bonus terverifikasi: `actions/cache` tetap berfungsi dengan `contents: read` saja. |
| **[R6-5]** | **SELESAI** | `_STATE_FILES` 11 → **13**, dan **bukti merah untuk masing-masing jalur baru**: menulis 1 baris ke `logs/security_audit.jsonl` → `exit=1` + `LEDGER TERCEMAR: logs/security_audit.jsonl +1 baris`; sama untuk `logs/_tmp_sheet_batch.json`. Repro cp1252 menjadi rc **0** (sebelumnya rc 1 dan benar-benar membunuh probe saya). `purge-fixture-ids`: sebelum `["1:100.0:"]` → sesudah `[]`, backup `closed_ids.json.bak-20261120T100910Z`, apply kedua `matched=0`. Artefak `logs/_ch_backup.csv` + seluruh skrip probe terhapus (test kebersihan menangkap skrip saya sendiri dua kali — guard hidup). |

**FASE AKHIR ronde 6:** 6 commit item + 3 commit `[R6-fix]`, `e68e0eb` → `f7ba62d`, empat bukti remote
identik di setiap push, CI `success` pada SHA terakhir (#44719415241). `ledger_delta = 0` dan hash
`closed_history.csv` **tidak berubah** (`C9BFC1D811B6`) sebelum/sesudah suite; 124 → **194 lulus**;
nol assert lama hilang (satu-satunya test warisan yang disentuh adalah lokator teks `watchdog.yml`
di ronde 4, dengan invarian tetap dan diperketat).

**Status CI sejak baseline `e68e0eb`:** 39 run, **4 `failure`** — dan keempatnya terjelaskan, dua di
antaranya adalah temuan ronde ini, bukan noise: `#44714780994` watchdog 09:55 (cacat historis W1b),
`#44717547994` CI 10:14 (bug harness decode cp1252, diperbaiki 10:18), `#44718326190` +
`#44719740669` risk-monitor 10:20 & 10:30 (outage kredensial S3 — **masih hidup sampai pemilik
login ulang**). Kriteria "0 failure" ronde ini **tidak terpenuhi**, dan penyebabnya adalah keputusan
saya sendiri di Item 4; itulah alasan kriteria itu sekarang ditegakkan di kode, bukan di checklist.



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
6. **(Ronde 6) Refresh token single-use: tidak ada refresh tanpa persistensi.** Siapa pun yang
   me-refresh OAuth harus bisa menyimpan hasilnya. Mode "read-only" yang tetap memanggil refresh
   adalah cara paling rapi untuk mematikan seluruh sistem; kini ditegakkan oleh
   `token_manager.allow_refresh()`, bukan oleh imbauan.
7. **(Ronde 6) Sinyal "state pulih" hanya dari keberadaan berkas state, bukan `cache-hit`.**
   Key cache yang memuat `run_id` membuat `cache-hit` praktis selalu false; memakainya sebagai
   sinyal akan membuat sistem menganggap dirinya dingin selamanya dan menolak entry tanpa sebab.
8. **(Ronde 6) Apa pun yang memanggil `gh` wajib punya langkah `actions/checkout` lebih dulu.**
   inilah penyebab watchdog gagal di 3/3 run terjadwalnya selama ini; test urutan kini menguncinya.
9. **(Ronde 6) Angka dari riwayat hasil backfill bukan performansi.** Baris `exchange_history_no_exit`
   sengaja tanpa PnL dan tampil sebagai `belum_terukur_pnl`; mencampurkannya ke win-rate adalah
   fabrikasi. Ledger tidak boleh ditulis massal sebagai efek samping monitoring — itu tugas
   `reconcile_history --write` yang eksplisit.
10. **(Ronde 6, pasca-akses Viewer) Nol tanpa sumber harga bukan hasil, melainkan data yang hilang.**
    Baris dengan `Exit == Entry`, `PnL == 0`, dan kolom sumber harga kosong/`entry_fallback` wajib
    tampil sebagai `pnl_diragukan` dan dikeluarkan dari win/loss. Impas **riil** (sumber
    `exchange_fill`/`response_position`) tetap dihitung. Dan sebelum strategi mana pun dinilai,
    sensus tiga sumber wajib dibaca bersama: hitungan bursa (`history_measurement`), census
    `Closed_Positions` (`tools/sheet_probe.py`), dan ledger lokal — ketiganya boleh berbeda, dan
    perbedaan itu adalah temuannya, bukan angka yang harus dirapikan.

11. **(Ronde 6, pasca-hidup-kembali) Ketiadaan bukti = kondisi berbahaya, bukan kondisi aman.** Penanda
    kesehatan (`WALT_STATE_WARM`) hanya boleh bernilai benar bila ada bukti eksplisit dari probe yang
    memeriksa berkas state; default "hangat" membuat gerbang start-dingin mati tanpa suara bila probe
    rusak atau langkah lupa meneruskan output. Semua kegagalan input (voter LLM, kalender earnings,
    webhook riset) wajib lewat agregator kegagalan yang muncul di Step Summary — siklus `success` dengan
    1 dari 4 voter mati adalah laporan yang MENIPU.
12. **(Ronde 6) Cache Actions bukan tempat menyimpan kebenaran.** Ledger, `closed_ids`, dan snapshot yang
    hidup hanya di cache akan hilang saat eviksi, dan setiap kehilangan itu berarti ada penutupan yang
    tidak pernah tercatat (bukti: 52 di bursa vs 17 di sheet). State yang harus bertahan lintas eviksi
    wajib punya sumber tahan-lama (API bursa sebagai kebenaran, sheet sebagai mirror) dan jalur rehidrasi
    sadar saat cold start — bukan didiamkan menjadi amnesia, dan bukan pula diisi dengan PnL karangan.
13. **(Pra-Ronde 7) "Tidak ada data" tidak pernah boleh menjadi angka nol.** `max(0, hari)` atas tanggal
    lampau, PnL kosong jadi `0.0`, atau harga keluar tak terbaca jadi `0` — semuanya terdengar aman
    tetapi menggeser perilaku sistem secara diam-diam. Yang benar: `None` + status (`past_only`,
    `no_data`, `unavailable`), dan pemanggil memutuskan dengan gerbang yang eksplisit. Nol adalah
    nilai; `None` adalah pengakuan.
14. **(Pra-Ronde 7) Aset pihak ketiga bukan fakta permanen.** Nama model, endpoint, dan syarat
    autentikasi (cookie+crumb) bisa dicabut upstream kapan pun; kode yang menyimpannya sebagai konstanta
    akan mati tanpa suara. Setiap input eksternal wajib: (a) dapat dioverride lewat variable tanpa
    deploy, (b) melaporkan statusnya per siklus, (c) kegagalannya masuk agregator kesehatan input —
    bukan hanya baris log yang tidak dibaca siapa pun.


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

Yang sudah dikerjakan (ronde 6, `[R6-1]`): default ID **dihapus dari kode** (`sheet_logger.py` dan
komentar di `google_apps_script.js`), `GOOGLE_SHEET_ID` wajib dari env/secret, kegagalan baca menjadi
`log.warning` teragregasi yang menyebut status HTTP, README §172 dikoreksi, dan `fetch_sheet_csv_rows_ex`
membedakan `no_config` / `http_error` / `html_or_unparsable` / `header_mismatch` / `empty` / `local_csv` / `ok`.
Perhatian yang masih berlaku: **riwayat git masih memuat ID itu** (ia pernah di-commit sebagai default).
**Terjawab (2026-11-20, §3e):** pemilik mengonfirmasi satu spreadsheet yang benar-benar terhubung
(sidik jari `sha8=06bded7f`, panjang 44) dan memberinya akses *Viewer*, sehingga gviz kini terbaca;
secret CI ternyata memegang ID lain (`sha8=c8899991`, panjang 45) yang tidak ada → itulah sumber
`http_error`/404 di CI waktu lokal mendapat 401. Secret sudah disamakan ke ID yang benar.
Keputusan yang masih harus diambil pemilik: mana yang jadi sumber kebenaran — spreadsheet, CSV lokal,
atau API bursa. Arah yang dipilih ronde 6: **API bursa** sebagai otoritas riwayat, sheet sebagai
cermin baca-manusia; `reconcile_history --write` sengaja **tidak** menulis ke sheet.

## 8. Cara memakai jejak ini

- **Ingin tahu apa yang berubah dan kenapa:** §1, §3, §3b, dan §3c; SHA commit ada di `git log` dengan tag
  `[P0-*]`, `[P1-*]`, `[P2-*]` (ronde 2), `[R3-n]` (ronde 3), `[R4-n]` (ronde 4), `[R5-n]` (ronde 5),
  `[R6-n]` + `[R6-fix]` (ronde 6, `67438a8`..`f7ba62d`).
- **Sedang hidup sekarang? Tidak.** Baca §1.1 lebih dulu: kredensial CI terbakar oleh keputusan ronde 6
  dan sistem gagal `invalid_grant` setiap 10 menit sampai pemilik login ulang. Tidak ada angka performansi
  yang bisa diambil dari rentang ini, dan tidak ada perubahan strategi yang boleh dieksekusi sebelum itu.
- **Ingin memverifikasi klaim:** §1 metode + §4 bukti. Jangan mengandalkan laporan agen; jalankan
  ulang `pytest -q` (194), guard diff, dan pembacaan CI dari GitHub API.
- **Ingin melanjutkan pekerjaan:** prompt eksekusi per ronde berada di mesin pemilik
  (`Antigravity_prompt.md` + arsip), dan sengaja tidak di-commit karena repo ini publik.
- **Angka di §3 (N2) bukan laporan performa.** Ronde 5 menghapus artefak fixture (ledger lokal kini
  hanya header) sehingga `stats` menunjuk 0 trade, dan kini ada dua lapis kejujuran baru: banner
  `LEDGER TIDAK TERTBACA (<status>)` saat sheet tidak terbaca, dan `belum_terukur_pnl` untuk baris
  hasil backfill bursa (endpoint tidak menyediakan harga keluar → PnL historis itu memang belum ada).
- **Syarat ronde 7 (strategi market) sudah berubah setelah ronde 6.** Dulu: [R6-1] dan [R6-2] hijau.
  Sekarang: (a) kredensial pulih dan tiga siklus risk-monitor `success` berturut-turut, (b) satu
  `reconcile_history` dry-run dari CI menunjukkan 52 closed + verdict kelengkapan, (c) satu
  `--write` sadar (dengan backup) bila pemilik ingin 52 riwayat itu masuk ledger, dan (d) satu bukti
  cache-miss nyata untuk [R6-2] yang masih tertunda. Tanpa itu, efek perubahan SL/TP/cadence tidak
  akan terukur oleh sistem yang sama yang baru saja membuktikan bahwa asuransinya bisa mati senyap.

---

*Dokumen ini disusun dari hasil verifikasi independen ronde 2-6 — termasuk ronde 6 yang saya
eksekusi sendiri dan kemudian harus saya koreksi tiga kali karena run nyata membantah asumsi saya
(§3c) — lalu disanitasi oleh scrubber yang memuat nilai konfigurasi lokal ke memori semata-mata
sebagai pola penghapusan. Scrubber-nya sendiri diuji dengan menanam satu nilai `.env` asli +
sembilan pola rahasia sintetis: seluruhnya terdeteksi (10 kecocokan pola + 1 literal, verdict
LOLOS), jadi "0 kecocokan" pada dokumen ini berarti bersih — bukan berarti patrolinya mati. Sidik
jari (`sha8`) sengaja dipakai sebagai pengganti nilai agar jejak audit tetap bisa dicocokkan tanpa
membocorkan identifier.*
