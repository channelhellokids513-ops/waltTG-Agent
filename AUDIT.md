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

## 3h. Webhook riset: akar masalah sebenarnya + jalur baca yang tidak bergantung deployment (2026-11-20, 16:30–16:55 UTC)

Pemilik melakukan redeploy Apps Script. Untuk membuktikannya saya tambahkan **mode `read_only`
di `diagnostics.yml`** dan **`tools/research_probe.py`** — keduanya **NOL langkah OAuth**, supaya
menguji jalur baca tidak lagi perlu men-dispatch workflow entry (yang 35 menit sebelumnya
menimbulkan ras token). Hasil run #44774660187 (16:34Z), satu baris yang menyelesaikan semuanya:

```text
content_type=text/plain; charset=utf-8; panjang_body=42;
cuplikan='Walt Perps Sheet Logger Webhook is active!'
```

**Webhook-nya hidup; yang ter-deploy BUKAN `google_apps_script.js` dari repo ini.** String sambutan
42 karakter itu tidak ada di berkas repo (dicari: `is active` / `action` / `e.parameter` → nol
kecocokan), dan `doGet` di repo memetakan 19 kolom `Daily_Scans` menjadi JSON. Jadi redeploy
mempublikasikan source lama/stub. Ini juga menjelaskan kenapa kegagalan sebelumnya tampak
"non-JSON tanpa struktur": bukan HTML, bukan halaman login — memang teks sambutan.

**Keputusan teknik:** sistem tidak boleh bergantung pada artefak yang tidak bisa kita deploy dari
repo. Ditambahkan **sumber kedua** untuk baca riset: `fetch_latest_research_from_gviz()` yang
membaca tab `Daily_Scans` langsung lewat gviz CSV — jalur yang SUDAH terbukti `Sumber ledger: ok`
di CI. Urutan menjadi `webhook → gviz → CSV lokal`, dengan `research_read_source` (webhook|gviz|
local_csv) ikut tampil di *Step Summary* sehingga asal data selalu terlihat, bukan diasumsikan.

Detail yang sengaja dijaga:
- kolom `balance_usdc` (indeks 1 tab) TIDAK dipetakan ke objek `Research` — ada test yang menuntut
  angka saldo tidak muncul di mana pun dalam objek itu, karena repo dan log CI publik;
- baris terakhir menimpa yang lama (tab kronologis) → yang terpakai = riset terbaru per aset;
- filter kesegaran memakai `_is_research_fresh` yang sama (`max_research_age_hours`);
- gviz TIDAK dipanggil bila webhook sudah menghasilkan data (ditegaskan test, supaya ini bukan
  penambahan latensi buta);
- probe exit 1 bila jalur baca sakit → run diagnostik MERAH, bukan hijau dengan sensor mati. Ini
  keputusan sadar: diagnostik bukan bagian dari gerbang siklus entry, jadi ketegasan lebih berguna
  daripada hijau kosmetik.

Perbaikan yang masih di tangan pemilik (opsional sekarang): deploy ulang dengan **isi
`google_apps_script.js` dari repo** (Deploy → Manage deployments → Edit → New version, *Anyone*).
Setelah itu webhook otomatis menang lagi sebagai sumber utama tanpa perubahan kode.

### Perbaikan sekalian: gerbang sanitasi jadi permanen dan ditegakkan CI

`tools/sanitize_gate.py` menggantikan skrip sementara yang selama ini saya tulis ulang tiap sesi di
`logs/_gate.py` (dan tidak pernah dijalankan CI). Cakupan = **seluruh berkas terlacak git**
(53 berkas, 11 kelas pola), dan ia menguji dirinya sendiri lewat canary yang **disusun dari potongan**
agar berkas gerbang tidak memicu detektornya sendiri. Laporan sengaja tidak pernah memuat nilai yang
cocok — menyebut isi secret di laporan = menyebarkannya.

Dua temuan nyata yang langsung dikeluarkan gerbang pertama kali:
1. `tests/test_data_integrity_and_quality.py` memuat literal `AIzaSyFake…` (fixture palsu untuk uji
   redaksi). Nilainya tidak berbahaya, tapi bentuknya PERSIS kunci asli — artinya gerbang tidak akan
   bisa membedakan fixture dari kebocoran. Dirangkai ulang dari potongan; test tetap menguji hal yang sama.
2. **Tiga alamat surel berdomain gmail publik ter-commit di repo publik** (dua di `test_ci_hardening`,
   satu di `test_telegram_auth` — salah satunya memakai nama depan pemilik, jadi sangat mungkin nyata).
   Tidak ditulis ulang di dokumen ini. Diganti ke domain RFC 2606 `example.com`; regex redaksi surel
   di `walt_bot` bersifat umum (sudah dicek) sehingga test redaksi tetap menguji perilaku yang sama,
   bukan kebetulan domain tertentu.

Pengecualian per kelas dicatat dengan alasan dan **dites**: `account_balance` dilonggarkan untuk
`PRD.md`/`STRATEGY.md`/`tests/` (memang berisi contoh angka margin/TP-SL) tapi TETAP ketat di
`AUDIT.md`/`README.md` — dua berkas tempat saldo asli bisa tersalin dari log run. Ada test yang
gagal bila seseorang mencoba mengecualikan `AUDIT.md`, dan test yang gagal bila kelas identitas data
(`sheet_id`, kunci API, token sesi) dikecualikan sama sekali.

Suite: **258 → 286 test** (+28), semua lulus; gerbang bersih atas 53 berkas terlacak.

## 3i. Rantai baca riset selesai diperbaiki — dan menutup dengan dua temuan struktural (2026-11-20, 16:40–17:05 UTC)

Setelah redeploy pemilik, mode `read_only` (nol OAuth) dipakai menguji berulang sampai tuntas.
Empat run diagnostik, empat lapisan yang berbeda:

| Run | Yang terungkap |
|---|---|
| #44774660187 16:34Z | Balasan webhook = `text/plain`, 42 byte: `Walt Perps Sheet Logger Webhook is active!` → **deployment aktif bukan `google_apps_script.js` repo ini** (string itu tidak ada di repo) |
| #44776590327 16:45Z | gviz menolak `Daily_Scans` dengan `header_mismatch` → tab berisi label cantik berbahasa Indonesia (`Sisi`, `Saldo (USDC)`, `P1 (Polaritas)`), bukan nama kolom internal |
| #44777562090 16:51Z | `_is_research_fresh gagal parse timestamp '2026-11-15 5:40:00'` → Sheets menulis ulang ISO ke formatnya sendiri; **semua baris otomatis dianggap basi** |
| #44778524915 16:57Z | `baris=39, segar=0, basi=39, waktu_tidak_dikenali=0, terbaru=2026-11-20T00:17:17Z` → parser benar-benar bekerja di data nyata dan angka sisanya adalah **fakta data**, bukan cacat kode |

Perbaikan yang masuk kode (semua ada test-nya, 303 test lulus):
1. Pemetaan header toleran berbasis token ternormalisasi (`_normalize_header` + `map_gviz_scan_header`),
   dengan penanda tab TIGA kolom (`timestamp_utc`+`asset`+`swarm_score`) supaya `Closed_Positions`
   — yang juga punya kolom `Asset` — tetap ditolak. Guard anti-fallback gviz ronde 6 tetap utuh.
2. `parse_sheet_timestamp`: ISO, `YYYY-M-D H:MM:SS` (jam tanpa nol, spasi), tanggal US, AM/PM,
   nomor seri Sheets. Tanpa zona = UTC (kolomnya memang `Timestamp (UTC)`). Sampah tetap `None`
   (ada test yang menuntut "kemarin"/"NVDA" TIDAK dipaksa jadi tanggal).
3. Statistik gviz (`baris/segar/basi/waktu_tidak_dikenali/terbaru`) dicetak probe — sebuah `0`
   tidak boleh lagi muncul tanpa penjelasan, sesuai invarian 13.
4. Kolom `balance_usdc`/`open_positions` **ditahan** dari objek riset (test menuntut angka saldo
   tidak ada di dalamnya) — jalur baca baru tidak boleh menjadi jalur bocor baru.

**Koreksi atas dugaan saya sendiri:** sempat saya tulis "tab berhenti 15 November". Itu keliru —
yang terlihat saat itu adalah baris *pertama yang gagal parse*, bukan yang terbaru. Setelah parser
benar, ternyata `terbaru = 2026-11-20T00:17Z`. Kesimpulan yang bertahan hanya yang diukur.

### Temuan struktural A — kadens riset vs jendela kesegaran tidak pernah bertemu

`daily-scan` berjalan **00:17Z & 12:17Z** (12 jam), sementara `max_research_age_hours = 6.0`.
Artinya reuse riset antar-workflow **secara struktural mustahil** di paruh kedua tiap interval:
pada 16:57Z riset terbaru berumur 16,7 jam. Run 12:17 hari ini juga `skipped` (masih
`MAINTENANCE_MODE`), jadi jendelanya melebar lagi. Untuk ronde 7: naikkan jendela ke ≥ kadens
scan ATAU percepat kadens scan — dan putuskan mana, karena ini biaya (LLM) vs kesegaran, bukan
bugs yang bisa "diperbaiki diam-diam".

### Temuan struktural B — siapa mengawal si pengawal

`watchdog.yml` memakai `cron: '0 * * * *'`, tapi run `schedule` terakhir adalah **09:55:19Z**
(~7 jam tanpa satu pun run terjadwal), sedangkan run manual 10:19Z sukses dan push/CI tetap jalan.
Dua kegagalan `schedule` sebelumnya (09:55, 01:10) memakai langkah lama yang memanggil `gh run list
--status success` dan roboh `CalledProcessError` — logika itu sudah dipindah ke `tools/watchdog_check.py`
di ronde 6, jadi kegagalannya bersejarah, bukan aktif. Yang aktif justru: **jaring pengaman untuk
"kematian senyap scheduler" bergantung pada scheduler milik pihak yang sama**. Karena Cloud Scheduler
milik pemilik terbukti hidup (risk-monitor 16:00/16:10 jalan tepat waktu), kandidat perbaikan ronde 7:
tembak `watchdog.yml` dari Cloud Scheduler juga (atau dispatch watchdog tiap N siklus risk-monitor),
bukan menambah lebih banyak `cron` GitHub.

Catatan operasional: seluruh verifikasi ronde ini memakai `diagnostics.yml mode=read_only` — nol
langkah OAuth, jadi tidak ada lagi ras token seperti kejadian 16:00Z yang saya timbulkan sendiri.

## 3j. Eksekusi berurutan (a)→(d): jendela kadens, watchdog tanpa cron, dan 11 duplikat ledger yang terbendung (2026-11-20, 17:10–18:10 UTC)

### (a) Jendela riset 6j → 13j, terikat kadens, dan sumber kebenaran sebenarnya

Ternyata mengubah default kode SAJA tidak berpengaruh: variable `STRATEGY_CONFIG_JSON` menimpa
default. Keduanya diubah, dan angkanya kini TIDAK ditulis manual —
`DEFAULT_RESEARCH_AGE_HOURS = DAILY_SCAN_CADENCE_HOURS + 1.0`.

Pelajaran operasional: `gh variable set NAME --body '{json}'` di PowerShell **menghancurkan tanda
kutip** sehingga variable sempat tersimpan sebagai string tak-valid. Sekarang ada
`tools/set_strategy_var.py` yang (1) menyusun JSON dari `DEFAULT_STRATEGY_CONFIG`, (2) mengirimnya
lewat **stdin**, (3) membaca balik dan menolak bila hasil baca tidak bisa diurai JSON, (4) membandingkan
dengan kode. Verifikasi: 13 kunci, `max_research_age_hours=13.0`, `sama_dengan_kode=True`.
Kesalahan saya sendiri terukur tidak berbahaya: loader gagal-dengan-aman (`Gagal parse sumber bersama
STRATEGY_CONFIG_JSON ... fallback ke DEFAULT` + peringatan, exit 0) — dibuktikan dengan menjalankan
loader memakai string rusak yang persis sama.

`tools/config_coherence.py` memeriksa PASANGAN angka, bukan rentang satu angka (yang gagal dulu
lolos semua rentang): jendela ≥ kadens scan; `min_funding_daily × hold/24` ≥ biaya round-trip
(2×taker + slippage) dengan margin ≥1.5×; `tp_calm_pct` > biaya; `daily_loss_limit_pct` ≥ SL.
Dijalankan atas variable produksi di diagnostics (nol OAuth) dan atas default kode di test.
Catatan: saya SEMPAT menduga gerbang fee salah faktor 100 — setelah dibaca, implementasinya benar
(`expected_funding = |funding_daily| × hold/24`, tanpa `/100`); dugaan itu saya buang, tidak saya
jadikan perubahan kode.

### (b) Yang mengawal si pengawal — diselesaikan dari dalam repo

Cloud Scheduler pemilik TIDAK bisa dijangkau dari mesin ini: `gcloud` aktif sebagai akun pemilik,
tapi `scheduler jobs list` menghasilkan **0 job di semua lokasi/proyek yang bisa diakses**
(dijajal di 10 proyek; proyek `rtdicky888-com` bahkan tidak ada — itu tebakan saya, lalu dikoreksi).
Satu-satunya konfigurasi gcloud = `porosnusantara-portal`. Jadi pemicu cadangan dibuat dari yang
memang kita kendalikan: `keep_watchdog_alive()` dipanggil di akhir `risk-monitor.yml` dan
`market-execute.yml` (`if: always()`), men-**dispatch** `watchdog.yml` hanya bila run watchdog
terakhir > 50 menit. Throttle memakai **usia run**, bukan menit jam dinding, supaya tetap benar
bila kadens berubah.

Dibuktikan di produksi, dua-dua sisi:
```
17:40:21Z  Watchdog terakhir 440m lalu — dispatch agar jaring pengaman tetap hidup.
17:40:21Z  Fallback dispatch `watchdog.yml` dipicu.        → watchdog jalan 17:40:20Z
17:50:14Z  Watchdog terakhir berjalan 10m lalu (< 50.0m) — tidak perlu dispatch.
```
440 menit itu persis jeda 7,3 jam yang diukur di §3i. Harga yang dibayar: `actions: write` di dua
job — bukan pelonggaran global. `PERMISSIONS_ALLOWLIST` mengunci exact-equality per workflow plus
test arah-balik (memegang `actions: write` tanpa langkah dispatch yang terbukti = merah). Prefiks
`WALT_` dijaga tetap berarti "kredensial": pintu daruratnya `WATCHDOG_KEEPALIVE`, dan test
melarang env `WALT_*` di langkah keepalive (jangan sampai fasilitas baru ikut membakar token).

### (d) Backfill: dedupe harus membaca sheet, dan 11 baris nyaris dobel

`reconcile_history` dulu hanya membandingkan CSV lokal + `closed_ids.json` — padahal yang akan
ditulis mirror adalah Google Sheet, dan CSV di CI berasal dari cache yang bisa ter-eviksi. Dry-run
pertama (17:54Z) memberi angka yang menipu nyaman: `rows_seen=52, sudah_ada_di_sheet=6,
candidates_to_add=46`. Sensus read-only baru (`tools/ledger_census.py`) menjelaskan kenapa 6, bukan 17:

```
baris_total 17 | punya_position_id 6 | tanpa_position_id 11
tanpa_sisi 11  | terkunci_waktu 0    | tak_ternavigasi 0
```

Sebelas baris warisan itu tidak punya Position ID, tidak punya `Side`, dan `Opened At (exchange)`
kosong — jadi kunci berbasis waktu/sisi mustahil. Identitas tertangguh yang tersisa adalah
(aset | harga masuk 4dp), di mana arah galat dipilih **tidak menulis** bila ragu: duplikat ledger
merusak pengukuran senyap, baris yang terlewat hanya membuat angka tetap tidak lengkap (dan itu
dilaporkan). Hasil setelah guard terpasang (18:06Z):

```
rows_seen 52 | already_recorded 17 | dicocokkan_kunci_cadangan 11 | candidates_to_add 35
```

17 + 35 = 52, tertutup persis. **Seperempat "kandidat" (11 dari 46) ternyata sudah tercatat** —
tanpa kunci ini, mirror pertama akan menanam 11 baris duplikat ke ledger yang dipakai menghitung
win-rate. Guard tambahan: `--mirror-sheet` wajib bersama `--write`, dan mirror DIBATALKAN kalau
sheet tidak terbaca (`status != ok`) — tanpa dedupe yang bisa dipercaya, menulis itu judi.

### Regresi yang saya timbulkan sendiri dan diperbaiki di ronde ini

Probe riset yang exit 1 (keputusan §3i, masih benar) ternyata **membloki seluruh langkah setelahnya**,
sehingga dry-run reconcile pertama tidak pernah jalan. Sensor jujur dan pipeline yang tetap jalan
bukan pilihan biner: langkah setelah probe kini `if: always() && ...`, dikunci test
`test_sensor_merah_tidak_boleh_memblokir_langkah_lain`.

Suite: **303 → 339 test**.

## 3k. Setelah dilepas ke live: yang menahan entry BUKAN funding, tapi biaya (temuan ekonomi 18:25Z)

Siklus market-execute 17:43Z (post-(a)) adalah siklus paling sehat yang tercatat hari itu: Qwen +
Gemini menjawab `200 OK`, kalender earnings `crumb` `200 OK`, ledger `ok`, dan keempat kandidat
ditolak karena **keputusan**, bukan karena data hilang:

```
SKIP NVDA / NBIS / SPY: High Risk Catalyst (P5=0.73..0.82 >= 0.70)
SKIP XAG:  gerbang ekonomi funding vs fee gagal (hold=24h, exp_funding=0.576%, cost=1.180%, req=1.770%)
```

Yang pertama kali saya salah baca: `cost=1.180%` membuat saya menduga gerbangnya salah faktor 100.
Setelah `check_funding_vs_fee_gate` dibaca, implementasinya BENAR — `cost = 2 × taker(0.09%) +
price_slippage`, dan `price_slippage` adalah nilai PER ASET dari bursa (~1.00% untuk XAG/ETH/BNB/AAVE),
bukan konstanta. Yang salah adalah **alat koherensi saya sendiri**: ia memakai slippage tetap 0.03%
dan karenanya menyatakan konfigurasi "KOHEREN" sementara engine nyata menolak segalanya. Diperbaiki
dengan memanggil fungsi gerbang engine (bukan menyalin rumusnya), plus test yang membekukan angka
produksi: `round_trip_cost(1.00%) == 0.0118` dan `e5_ambang(1.00%) == 0.0177`.

Konsekuensi yang harus dibaca pemilik sebelum menyebut apa pun "performansi":

| Slippage aset | Biaya round-trip | Funding/hari yang diminta E5 |
|---|---|---|
| 0.05% (optimistik, spread sempit) | 0.23% | ≥ 0.35% |
| 1.00% (**teramati** di log) | 1.18% | ≥ 1.77% |

Funding teramati pada siklus terakhir: 0.52–0.85%/hari. Artinya **pada biaya slippage 1%, tidak ada
satu pun kandidat yang bisa lolos E5**, dan `min_funding_daily=0.50%/hari` tidak pernah menjadi
pembatas — ambangnya selalu lebih longgar daripada E5, sehingga tidak pernah jadi yang menolak.
Ini bukan bug yang bisa saya "perbaiki diam-diam": menaikkan nafsu
entry = menurunkan margin keamanan, dan itu keputusan pemilik. Pilihan yang tersedia, dengan efeknya:

1. Turunkan buffer E5 dari 1.5× (mis. 1.1×) → ambang 1.30% pada slip 1%; masih menolak semua kandidat hari ini.
2. Gunakan slippage per-aset yang lebih jujur untuk aset likuid (BTC/ETH spread nyata jauh di bawah 1%) →
   E5 jadi lolos untuk aset likuid, tetap menolak aset spread lebar. Ini yang paling mendekati maksud strategi.
3. Naikkan `min_funding_daily` ≥ 1.8% → floor kembali punya arti, tetapi praktis tidak ada entry.
4. Perpendek `expected_hold_hours` → menaikkan biaya efektif per unit funding, justru memperketat.

Sampai salah satu diputuskan, sistem yang sudah live akan tetap membuka NOL posisi. Itu fakta yang
perlu tertulis, bukan disimpulkan dari ketiadaan order.

## 3l. Permintaan pemilik: strategi RR 1:3 dengan winrate > 30% (diukur 2026-11-21, 21:3xZ)

**Permintaan.** "Cari strategi dengan RR 1:3 yang winrate-nya di atas 30%."

**Alat yang dibangun.** `tools/rr_scan.py` — TP = 3 x SL, resolusi intrabar KONSERVATIF
(bila TP dan SL tersentuh pada bar yang sama, trade dihitung RUGI), satu posisi per aset
(tanpa tumpang-tindih), 20 koin universe nyata (`CRYPTO_ASSETS`), bar 4h x 1000 (~5,5
bulan), biaya round-trip diuji pada dua tingkat: 0,18% (fee saja) dan 1,18%
(fee + slippage yang dinyatakan venue, lihat 3k). Matematika impasnya diuji terpisah di
`tests/test_rr_scan.py`.

**Temuan 1 — target "winrate > 30%" tercapai oleh entri ACAK, dan tetap rugi.**
Pembanding null (sinyal deterministik acak, aturan pasar identik):

| SL | TP | win rate acak | gross | net setelah biaya 1,18% |
|---|---|---|---|---|
| 1,5% | 4,5% | **32,3%** | +0,292R | **−0,495R** |
| 3,0% | 9,0% | 26,7% | +0,068R | −0,325R |
| 5,0% | 15,0% | 24,5% | −0,018R | −0,254R |
| 8,0% | 24,0% | 15,5% | −0,380R | −0,527R |

Jadi "RR 1:3 dengan winrate di atas 30%" **bukan kriteria strategi**: pada lebar stop
1,5% sebuah lemparan koin memenuhi syarat itu dan kehilangan 0,5R per trade. Win rate
hanya berarti bila dibandingkan null pada lebar stop yang sama, dan yang membayar adalah
EV bersih per R.

**Temuan 2 — tidak ada satu pun aturan harga yang melampaui null pada jendela ini.**
Mean-reversion (21,6%/19,4%), wick-reversal (21,9%/20,1%) dan breakdown (22,3%/22,5%)
semuanya DI BAWAH null. Momentum breakout yang terbaik: 28,7% pada SL 1,5% (masih di
bawah null 32,3%) dan 28,6% pada SL 8,0% dengan netR −0,002 (impas, bukan untung).
Kesimpulan sementara yang jujur: pada ~5,5 bulan data ini, rule harga generik tidak
memiliki tepi yang terukur.

**Temuan 3 — yang membunuh RR 1:3 adalah biaya, bukan rasio imbalannya.**
Kebutuhan win rate impas = (R + c) / 4R. Dengan c = 1,18%: SL 1,5% butuh **44,7%**,
SL 3% butuh 34,8%, SL 5% butuh 30,9%, SL 8% butuh 28,7%. Pada stop sempit, biaya
round-trip setara **0,79R** — hampir seluruh satu sisi taruhan habis di jalan keluar.

**Temuan 4 — carry funding mengubah bentuk soal, dan ini yang koheren dengan strategi kita.**
Funding adalah arus yang terpisah dari arah harga: EV = [p·3R − (1−p)R] − c + f·T.
Dengan durasi tahan TERUKUR dari scan di atas dan funding harian yang benar-benar
teramati (0,52%–0,85%, lihat 3k):

| SL | tahan rata-rata | biaya (R) | impas tanpa carry | impas + f 0,52%/hari | impas + f 0,85%/hari |
|---|---|---|---|---|---|
| 1,5% | 0,9 hari | 0,79 | 44,7% | 36,8% | 31,8% |
| 3,0% | 2,7 hari | 0,39 | 34,8% | 23,0% | 15,6% |
| 5,0% | 5,9 hari | 0,24 | 30,9% | **15,6%** | **5,9%** |
| 8,0% | 11,8 hari | 0,15 | 28,7% | 9,6% | 0,0% |

Bentuk strategi yang lolos dari permintaan ini karena itu bukan "TP jauh, stop sempit",
melainkan **stop lebar (3–5%) + tahan berhari-hari + menangkar carry**: win rate 30% yang
Anda minta menjadi jauh melampaui kebutuhan (15,6% pada SL 5%). Efek sampingnya
menyentuh 3k dari sisi yang benar: `expected_hold_hours` naik dari 24 jam menjadi
~5,9 hari menurunkan kebutuhan funding harian gerbang E5 dari ~1,77%/hari menjadi
~0,2%/hari, sehingga funding teramati 0,52–0,85% **lolos** — tapi konsekuensinya risiko
per trade naik 3,3x, sehingga ukuran posisi (bukan TP) yang harus turun.

**Batas yang belum ditutup (jangan dipakai untuk mengubah config hari ini).**
(a) Semua ini IN-SAMPLE, tanpa walk-forward, satu jendela waktu, dan aturan yang diuji
adalah aturan generik — bukan klaim bahwa tidak ada aturan yang bekerja.
(b) Carry dianggap konstan pada level harian teramati; **deret funding historis tidak
kita punya** (kolom funding `Daily_Scans` satuannya campur — lihat temuan ronde 7), jadi
sisi penerima funding belum terverifikasi per posisi.
(c) Dua angka biaya yang saya pakai berasal dari dunia yang BERBEDA: 1,18% adalah biaya
dinyatakan venue (fee + slippage ~1,00% untuk clip kita, lihat 3k), sedangkan 0,18% hanya fee.
Selisih keduanya memisahkan "mustahil" dari "longgar", tapi alat ukurnya
`tools/contract_economics.py` BELUM ditulis; menjalankannya butuh satu rotasi OAuth
    (keputusan pemilik). Sebelum biaya itu terukur langsung di bursa, memilih TP/SL
    adalah menebak di antara dua dunia - bukan optimasi.
(d) Tidak ada perubahan `strategy_config` yang saya lakukan di ronde ini.

## 3m. Dokumen referensi pemilik (Market Gravity + Comfort Point + Monte Carlo): mana yang saya pakai, mana yang saya bantah (2026-11-21)

**Masuk.** `penelitian_model_market_gravity_implementasi_python.md`. Isinya sebagian besar
transkripsi Pine yang cocok dengan transkripsi baris-per-baris saya (konstanta, bobot
strength, decay Cauchy, half-life, FSM, modifier). Tambahan barunya dua: **Comfort Point**
(pusat massa magnet) dan **peramalan drift-diffusion Monte Carlo** dengan probabilitas
first-passage.

**Dipakai** (diimplementasikan pure-Python, tanpa numpy/pandas karena keduanya tidak ada
di `requirements.txt`): `GravitySnapshot` + `analyze_market_gravity(trace_every=...)`,
`comfort_points()`, `forecast_first_passage()`, `calibrate_diffusion()`, dan
`tools/gravity_strategy.py`. Semua diuji (`tests/test_market_gravity.py`: 9 test baru;
suite 387 lulus).

**Dibantah dengan ukuran, bukan pendapat.**
1. **Konstanta Monte Carlo dokumen itu tidak pernah difit.** Ia memakai alpha=0,12,
   beta=0,08, gamma=0,65. Difit pada 14.700 pasangan (bar, hari berikutnya) dari 20 koin
   bar harian: **alpha=+0,0278** (dokumen 4,3x terlalu besar), **beta=+0,0027** (30x
   terlalu besar), **gamma=0,676** (ini justru benar, ~4% meleset), dengan **R²=0,0002**.
   Artinya: skala difusi terhadap ATR valid, tetapi DRIFT yang diklaim berasal dari
   gravity/trend hampir tidak ada. Konsekuensinya tegas - probabilitas first-passage dari
   model ini TIDAK boleh dipakai untuk ukuran posisi.
2. **Probabilitas first-passage BELUM Tervalidasi (bukan sekadar gagal).** Setelah
   perbandingan dibuat apple-to-apple (prediksi dikondisikan pada "salah satu magnet
   tersentuh"), hanya satu decile yang punya sampel cukup (n~40-44): prediksi ~2-4%
   terhadap kenyataan ~16-18%, rata-rata selisih ~13 poin. Dengan n total ~160 prediksi
   ini TERLALU SEDIKIT untuk menerima maupun menolak model - yang sah disimpulkan adalah
   "belum terbukti", dan itu sudah cukup untuk melarangnya dipakai menentukan ukuran posisi.
3. **Klaim "GOOGLEFINANCE(...) menyediakan data historis"** perlu diperkecil. Yang benar:
   Google Finance menyediakan data **harian/mingguan** melalui Sheets; ia tidak
   menyediakan intraday yang stabil, dan (terverifikasi di 3l) halaman quotenya tidak
   memuat deret historis sama sekali. Untuk universe perps kita, sumber bar tetap
   Hyperliquid/Binance; Google Finance hanya layak jadi pembanding harga - sudah dibuat
   sebagai `tools/gravity_parity_check.py`.

**Temuan positif pertama dari seluruh rangkaian ini.** Aturan **fade rejected sweep**
(harga menyapu magnet, lalu ditutup kembali ke sisi asal - status `Rejected` di FSM)
menghasilkan arah yang lebih baik dari cerminannya SECARA KONSISTEN pada semua bentuk
target: selisih gross terhadap null +0,59R (R1), +0,68R (R2), +0,56R (R3), +0,42R
(magnet). Ini satu-satunya sinyal di repo ini yang melampaui kontrolnya. Tapi bentuk
target menentukan apakah tepi itu survives:

| target (bar 1d, 20 koin, SL = sweep + 0,5 ATR) | trades | win% | grossR | net@0,18% | net@1,18% | tahan |
|---|---|---|---|---|---|---|
| fade_R1 | 2202 | 71,3 | +0,129 | **+0,075** | −0,224 | 2,2 bar |
| fade_R2 | 1509 | 57,3 | −0,011 | −0,073 | −0,413 | 5,3 |
| fade_R3 | 1104 | 38,2 | −0,230 | −0,296 | −0,659 | 8,9 |
| fade_magnet | 1058 | 31,5 | −0,262 | −0,329 | −0,704 | 9,6 |
| null_R1 (cermin) | 2202 | 27,5 | −0,457 | −1,180 | −5,192 | 2,2 |

Ini sekaligus menjawab permintaan RR 1:3 dengan cara yang paling tegas: **RR 1:3 pada
sinyal ini menghasilkan win rate 38,2% (di atas 30%) tapi EV-nya NEGATIF bahkan sebelum
biaya** - tepi strukturnya nyata, tetapi berumur pendek (habis dalam ~2 hari), sehingga
memaksa TP sejauh 3R justru membuangnya. Bentuk yang membayar: TP = 1x jarak stop.

**Batas.** In-sample, 750 bar harian (±3 tahun), tanpa walk-forward, satu buffer SL
(0,5 ATR), biaya dua titik (0,18% dan 1,18%) yang keduanya BELUM terukur pada bursa, dan
belum termasuk carry funding. Pada tahan 2,2 bar dan risiko ~3,3%, carry 0,52%/hari
menambah ~0,35R - cukup untuk membalik net@1,18% menjadi sedikit positif, yang lagi-lagi
berarti **semua kesimpulan ekonomi bergantung pada pengukuran biaya yang belum dikerjakan**.

## 3n. Walk-forward ber-purge atas aturan fade-rejected-sweep (2026-11-21, permintaan pemilik)

**Alat.** `tools/gravity_walkforward.py`. Yang membedakan dari tabel §3m: bentuk target
dan lebar stop **dipilih ulang di dalam jendela latih tiap fold** (12 kombinasi: R1/R2/R3/
magnet x buffer 0,25/0,5/1,0 ATR, kriteria = EV bersih pada BIAYA TERBURUK 1,18%), jendela
latih di-purge 12 bar di kedua sisi, dan trade fold uji hanya boleh dibuka di dalam fold
itu. Tanpa ini, angka "per-fold" hanyalah contekan terselubung.

**Hasil (20 koin perps, bar harian x 750 ~ 3 tahun, 5 fold):**

| fold | bar uji | dipilih di latih | EV latih | trade uji | win% | net@1,18% |
|---|---|---|---|---|---|---|
| 1 | 0-147 | R1@1.0 ATR | +0,094 | 341 | 76,2 | +0,041 |
| 2 | 147-294 | R1@1.0 | +0,084 | 385 | 74,3 | +0,071 |
| 3 | 294-442 | R1@1.0 | +0,077 | 291 | 77,7 | +0,038 |
| 4 | 442-589 | R1@1.0 | +0,077 | 339 | 80,5 | +0,124 |
| 5 | 589-736 | R1@1.0 | +0,070 | 371 | 67,1 | +0,094 |

Gabungan out-of-sample: **1.727 trade, win 74,9%, gross +0,207R, net@0,18% +0,187R,
net@1,18% +0,075R**, tahan 4,6 bar, jarak stop rata-rata 10,87%. Cerminan (arah dibalik,
jarak sama) pada aturan terpilih: win 24,9%, net@1,18% -0,619R -> **selisih arah +0,694R
di luar sampel**, konsisten dengan +0,59 s.d. +0,68R yang terlihat in-sample.

**Empat hal yang membuat hasil ini layak dipercaya, dan tiga yang tidak.**
Layak: (1) kelima fold memilih parameter yang SAMA, jadi risiko pemilihan rendah;
(2) EV positif di kelima fold, bukan dirata-ratakan dari satu fold besar; (3) interval
90% cluster-bootstrap PER ASET = [+0,014, +0,146]R - tidak menyentuh nol (bootstrap i.i.d.
akan memberi interval palsu karena trade dalam satu aset berkorelasi); (4) kontrol
cerminan dijalankan pada trade yang sama persis, jadi yang dibandingkan hanya arahnya.
Tidak layak dipercaya sepenuhnya: (a) biaya 1,18% dan 0,18% tetap angka anggapan - **belum
diukur di bursa**; (b) ini bar dari Hyperliquid/Binance, sedangkan eksekusi kita di Walt,
jadi kualitas fill belum diuji; (c) carry funding belum masuk sama sekali.

**Konsekuensi operasi yang harus dibaca sebelum siapa pun menaikkan ukuran posisi.**
Stop yang terpilih (1,0 ATR buffer) berarti jarak stop rata-rata **10,87%**, bukan 1,5%
seperti hari ini. Pada leverage 2, satu rugi = ~21,7% margin per posisi; pada leverage
minimum 1 tetap ~10,9% margin. Saat ini satu rugi = ~3% margin. Jadi mempertahankan
risiko dolar per trade yang sama menuntut margin per posisi DITURUNKAN ~3,6x - dan pada
basis margin $2,50-3 itu jatuh di bawah `min_size_base` bursa, alias tidak bisa dieksekusi.
Pilihan jujurnya: terima risiko per trade lebih besar, atau perkecil jumlah slot, atau
tolak aturan ini. Tidak ada jalan tengah yang gratis.

**Benturan dengan gerbang yang ada.** Aturan ini hidup dari tepi struktural, bukan dari
funding. `E5` (funding vs biaya) saat ini menuntut ~1,77%/hari dan akan menolak hampir
semua entry ini. Jadi keputusan sebenarnya bukan "TP/SL berapa" melainkan **apakah
sebuah entry boleh dibuka karena tepi struktural yang terukur, atau hanya karena carry**.
Itu keputusan pemilik, dan §10 STRATEGY sudah diperbarui untuk menanyakannya secara eksplisit.

## 3o. Biaya diukur dari akun sendiri — contract_economics (2026-11-21, permintaan pemilik)

**Alat & jalur.** `tools/contract_economics.py` + mode CI `diagnostics.yml oauth_step=
contract_economics`. Read-only ditegakkan di kode (`perps_execute_operation` DAN kedua
`perps_prepare_*` ada di daftar hitam; tool tak terdaftar juga ditolak) dan dites.
Rotasi OAuth + auto-sync terbukti pada dua run nyata: `PUT .../WALT_REFRESH_TOKEN -> 204`
lalu `Walt Risk Monitor` 23:10:02 tetap **success** — tidak ada balapan yang merusak
kredensial. (Job diagnostics sendiri berstatus failure karena langkah *Research read probe*
yang sudah lama merah akibat stub Apps Script, bukan karena alat ini.)

**A. Slippage yang bursa nyatakan = 139 kontrak, median/p10/p90/min = 1,000%, maks 2,000%.**
`p10 == p90` berarti nilainya seragam persis — tanda **konstanta DEFAULT**, bukan pengukuran
per aset. Jadi angka 1,18% yang dipakai semua simulasi kita selama ini = mengadopsi asumsi
bursa apa adanya. Kecocokan dengan konstanta orang lain bukan bukti, dan alatnya sekarang
mencetak peringatan itu sendiri.

**B. CARRY NYATA — temuan yang paling consequential.** Dari 51 posisi tertutup dengan
funding + margin + durasi terbaca (1 tidak lengkap, bukan dibuang diam-diam):
**median 0,0665%/hari dari notional** (p10 0,0000%, p90 0,1851%, maks 0,6261%);
0,1331%/hari dari margin. Asumsi yang saya pakai di §3l dan di seluruh simulasi gravity
adalah **0,52%/hari** → rasio **0,13×**, terlalu besar **~8×**.
*Koreksi atas klaim saya sendiri di §3l:* "carry menurunkan win rate yang dibutuhkan secara
signifikan" memakai funding harian pasar, bukan carry akun kita. Pada tahan 4,6 hari relief
carry nyata hanya ~0,31% notional ≈ **0,03R** pada stop 10,87% — bukan 0,35R seperti yang
saya tulis tadi pagi. Kabar baiknya: angka walk-forward **+0,075R net@1,18% tidak berubah**,
karena simulasi itu memang tidak memasukkan carry. Yang batal adalah argumen bahwa carry
bisa menyelamatkan strategi ber-EV tipis.

**C. Deviasi fill vs harga keputusan: median 0,793%, p90 3,430%, maks 8,008% (signed median
−0,292%), n=34 pasangan — dan angka ini TIDAK BOLEH dipakai sebagai slippage.** Jeda
scan→fill yang ikut terukur: **median 1,52 jam, p90 21,5 jam, maks 35,95 jam**. Selisih
sebesar itu didominasi drift pasar selama jeda, bukan biaya eksekusi. Bagian C sekarang
mencetak penolakannya sendiri. **Slippage murni masih belum terukur**; satu-satunya jalan
mengukurnya adalah mencatat `latest_rate` tepat sebelum `execute` dan membandingkannya
dengan `avg_fill_price` dari satu order nyata — itu menuntut satu order sungguhan dan karena
itu bukan pekerjaan read-only.

**Yang boleh dipakai mulai sekarang:** fee round-trip (dari `TAKER_FEE_PER_SIDE`, presisi)
dan carry akun yang nyata (0,0665%/hari). **Yang belum:** slippage — tetap 1,00% sebagai
pernyataan bursa, diberi label "asumsi venue", bukan "terukur".

## 3p. Probe order NYATA (slippage), koneksi AI ke crypto, dan rancangan P8 (2026-11-21)

### 3p.1 Probe slippage — satu order nyata, dan apa yang boleh disimpulkan darinya

Alat `tools/slippage_probe.py`, dijalankan dari `diagnostics.yml oauth_step=slippage_probe`
dengan PINTU KEDUA `real_order=YA_ORDER_NYATA` (saya letakkan di kode, bukan di `if:` YAML,
agar teruji; lihat 3p.3). Clip yang dipakai = clip PRODUKSI, bukan clip khayalan.

| Tahap | Nilai |
|---|---|
| Posisi | BTC long, leverage 2, **margin $2,50** (`min_size_base=7e-05`) |
| `latest_rate` tepat sebelum execute | 58.995,20 |
| Fill yang bursa laporkan | 58.986,10 |
| **Slippage masuk TERUKUR** | **−0,0154% (−1,5 bps; kebetulan menguntungkan)** |
| Slippage keluar | **TIDAK TERUKUR** — sengaja tidak diisi nol |

`preview_close` **ditolak bursa 3×** dengan risk check yang namanya terpotong `open_posi…`
(kemungkinan guard umur-minimum setelah open). Ini tidak saya sebut "gagal total": verifikasi
independent menunjukkan **posisi tertutup** — hitungan riwayat bursa naik dari
`posisi tertutup terbaca=52` menjadi **53** pada run berikutnya, dan median carry hanya
bergeser 0,0665% → 0,0651%/hari, konsisten dengan trade berdurasi menit. Penutupan terjadi
lewat jalur otomatis (autoClose/monitor), bukan lewat probe saya.

Konsekuensi yang sah, dan yang tidak:
- **Sah:** bukti langsung pertama bahwa dampak eksekusi pada clip terkecil kita (~1,5 bps)
  berada **>=65x di bawah** konstanta slippage venue 1,00%. Dasar biaya 1,18% yang selama ini
  membuat SEMUA kandidat SKIP kemungkinan besar terlalu pesimis.
- **Tidak sah:** menganggap ini estimasi slippage kita. Dampak membesar dengan ukuran order,
  jadi 1,5 bps pada $2,50 adalah **batas bawah**, satu sampel, satu arah, satu momen pasar.
  Butuh >=10-20 sampel terarah sebelum `COSTS` di `gravity_strategy`/`rr_scan` boleh diganti.
- Perbaikan yang perlu untuk probe berikutnya: hormati umur-minimum sebelum close (sleep +
  backoff pada `preview_close`) supaya sisi keluar ikut terukur.

### 3p.2 AI sekarang ikut menilai crypto & komoditas (permintaan pemilik)

Sebelum: `decide_go_nogo` memotong crypto/komoditas ke `GO` tanpa riset apa pun — 4 voter +
P1–P7 **tidak punya efek** pada mayoritas universe kita. Sekarang mereka ikut dinilai, dengan
batas kuasa yang saya buat eksplisit dan dites (`tests/test_research_crypto_r8.py`, 10 test):

1. AI **hanya boleh MENURUNKAN** (HALF/SKIP) — tidak pernah menciptakan entry, tidak pernah
   menaikkan ukuran di atas 1.0 (dites terhadap semua kombinasi riset).
2. `P5_risk_catalysts >= MAX_P5_RISK` -> SKIP; berita **bertentangan** dengan arah posisi ->
   SKIP; konsensus `SPLIT`/`CONFLICT`/vendor split -> **HALF**.
3. **Riset hilang untuk crypto TIDAK memblokir** (status quo GO). Pilihan sadar: jalur BACA
   riset sedang sakit (webhook masih stub, 3p.4), jadi meniru aturan saham
   ("riset basi -> SKIP") berarti **berhenti trading diam-diam**. Saham TIDAK berubah — E3
   tetap mewajibkan riset segar (dites).
4. Cakupan = `RESEARCH_ELIGIBLE = ALLOWED_ASSETS`. Aset di luar whitelist (mis. `BBCA`) tidak
   ikut tersedot ke jalur riset (dites).

Konsekuensi langsung: satu siklus scan kini memanggil swarm untuk aset crypto juga
(±10 kandidat × 4 voter) — biaya dan durasi job naik. Kalau terlalu mahal, jalan tengahnya
membatasi riset crypto pada kandidat teratas saja; itu perubahan kecil.

### 3p.3 Dua kegagalan proses saya sendiri pada langkah ini (dicatat, bukan dibuang)

- `real_order` pertama saya indentasikan sebagai key **di dalam** `oauth_step`, bukan sibling
  di bawah `inputs:`. GitHub menolak dispatch (`HTTP 422: Unexpected inputs provided`) — dan itu
  **fail-closed**: tidak ada order yang dibuat. Struktur yaml kini terverifikasi.
- Versi pertama `if:` langkah probe berisi dua klausa gerbang; test invariant lama
  (`test_mode_read_only_menjalankan_nol_langkah_oauth`) menuntut satu klausa `oauth_step ==`
  per langkah OAuth. Saya pindahkan pintu kedua ke dalam kode — lebih baik, karena kini teruji,
  bukan cuma terbaca di YAML.

### 3p.4 Rancangan P8 (BELUM diimplementasikan) — dari sheet BBCA pemilik

Sheet `1rB7Bxuo…` ringkasan (range AE1:AI24) memuat output Market Gravity untuk BBCA:
Close 5.550 · ATR14 203,84 · Trend Bias −0,53 · **U1 6.850 (skor 38,34, Active)** · U2 7.925
(34,30) · **L1 4.740 (33,20)** · Upper Pull 60,64 / Lower Pull 33,20 · Bullish 64,62% ·
**Net Gravity +29,23** · **Comfort struktural 6.359 / anchored 6.078** · forecast 35 hari
6.078 dengan pita 68% [5.451 … 6.705]. Catatan penting: sel `Market Regime` **KOSONG** —
kolomnya ada tapi tidak terisi, dan itu harus dibetulkan sebelum P8 memakainya.

Bentuk P8 yang saya usulkan (**berdasarkan yang terukur, bukan yang diklaim**):

- `P8_liquidity_structure = clamp(0,7 × sweep_state + 0,3 × equilibrium_pull, −1, +1)`
  - `sweep_state`: −1 bila `Rejected` pada magnet atas (0–3 bar terakhir), +1 bila pada magnet
    bawah, selain itu 0. **Komponen inilah yang punya bukti:** fade rejected sweep unggul
    +0,694R atas cerminannya DI LUAR SAMPEL dan stabil di 5 fold (3n).
  - `equilibrium_pull`: `clamp((comfort_struct − close)/ATR / 3, −1, +1)`. Bobotnya sengaja
    kecil karena hubungan net_gravity → return 1 bar terukur **R² = 0,0002** (3m).
- Pemakaian: hanya boleh **menurunkan** (P8 berlawanan kuat dengan arah posisi → HALF). P8 TIDAK
  masuk `PARAM_WEIGHTS`/prompt voter: opini LLM tidak boleh bercampur dengan angka yang bisa
  diverifikasi ulang, dan menambah bobot akan mengubah `final_score` saham secara tak terduga.
- Prasyarat sebelum P8 dinyalakan: (a) Apps Script ter-deploy benar sehingga kolom `mg_*` dan
  `Market Regime` TERISI, (b) satu siklus tercatat untuk mengaudit nilai P8 per hari, baru
  (c) P8 boleh menurunkan ukuran. Tanpa (a), P8 akan selalu 0 dan diam-diam jadi ornamen —
  persis kelas kegagalan yang sudah kita tangani dua kali di ronde ini.
  **Status (a) per 2026-11-22: DEPLOYMENT TERBUKTI, NILAI MASIH BELUM TERBUKTI.** Lihat 3t —
  probe CI mengonfirmasi deployment aktif = kode repo `2026-11-21.1` dan header terbaca, tetapi
  yang membunyikan P8 adalah ISI kolom, dan itu baru bisa diklaim setelah satu siklus tulis
  menghasilkan baris `mg_*` non-kosong di `Daily_Scans`. P8 tetap belum dinyalakan.

### 3p.5 Sampel ke-2 probe + satu temuan yang lebih penting dari slippage (P1, 2026-11-21 06:50Z)

Sampel kedua (BTC long, margin $2,50): rate sebelum execute 57.700,00 → fill bursa 57.781,40 =
**slippage masuk +0,1411%**. Dua sampel kini berlawanan tanda (−0,0154% dan +0,1411%), yang
konsisten dengan "noise kecil di sekitar mid", BUKAN bias searah — dan rata-rata |0,078%| itu
tetap **~13x lebih kecil** dari konstanta venue 1,00%.

Probe lalu menunggu umur-minimum (54s, sesuai perbaikan 3p.4) dan mencoba close. Hasilnya
membuka masalah yang lebih serius daripada biaya:

```
close_ok=False posisi_sisa=0
attempt 3 preview menolak: ok=False risk_ok=False
checks=[open_position=False(No open position exists for this contract.)] token=False expires=None
```

**Posisi probe sudah TIDAK ADA dalam <60 detik setelah open.** Bursa menolak close bukan karena
guard umur, melainkan karena tidak ada lagi posisi untuk ditutup. Ini dibuktikan silang oleh
ledger diagnostics pada job yang sama: `PnL terukur` naik dari **2 → 4** (dua penutupan dengan
harga fill nyata = dua sampel probe), jadi posisinya memang tertutup, bukan tidak pernah ada.

Kenapa ini P1 dan bukan catatan kaki: jalur produksi memasang autoClose (TP/SL) lewat
`preview_auto_close`/`execute` SETELAH order terisi, dengan 3 retry. Kalau posisi bisa hilang
sebelum kita selesai memasang TP/SL — atau TP/SL yang terpasang langsung terpicu — maka klaim
"SEMUA posisi punya SL" yang selama ini kita andalkan tidak terbukti untuk posisi berumur pendek.
Dua hipotesis yang harus diuji berurutan: (a) SL/TP otomatis terpicu sangat cepat (mis. SL
dipasang di sisi yang salah untuk arah long, atau `compute_baseline_tpsl` menghasilkan harga
yang sudah terlampaui oleh fill), (b) bursa menolak/membalikkan order ber-margin minimum
(`min_size_base` clip) setelah menerima, sehingga posisi terbentuk-tertutup seketika.

Yang TIDAK saya lakukan: menyimpulkan penyebabnya. Yang harus dilakukan berikutnya: baca
`avg_entry_price` vs `TP/SL` dan `created_at`/`updated_at` dua baris closed terakhir, plus log
`autoClose` pada jam 06:50-06:52Z. Sampai itu jelas, **ukuran posisi nyata tidak boleh dinaikkan**
dan hasil probe tidak boleh dipakai mengubah `COSTS`.

MAINTENANCE_MODE sudah dikembalikan ke `false` (terverifikasi) segera setelah exit code 4, dan
alert Telegram terkirim sesuai desain alat. Tidak ada posisi tersisa (`posisi_sisa=0`).

### 3p.6 KOREKSI atas 3p.5 — P1 belum terjawab, dan penyebabnya adalah lubang observabilitas (2026-11-21 07:0xZ)

Di 3p.5 saya memakai "PnL terukur naik 2 → 4" sebagai bukti silang bahwa kedua posisi probe
benar tertutup. **Itu salah.** Pembacaan langsung tab `Closed_Positions` (gviz, tanpa token)
menunjukkan dua baris yang membuat angka itu naik adalah **PLTR (05:10:17Z, PnL +0,0183)** dan
**XAG (05:20:16Z, PnL +0,0293)** — keduanya ditutup monitor dengan alasan `Funding Flip Sign`,
exit source `snapshot_estimate`. Jadi kenaikan itu tidak ada hubungannya dengan probe BTC.

Lalu saya tambahkan pencetakan 6 baris closed TERBARU menurut `created_at` langsung dari respons
bursa. Hasilnya justru menunjukkan masalah yang lebih mendasar:

```
6 baris closed terbaru menurut created_at:
  AAVE long entry=103.432 ... created=2026-11-19T17:11:19 updated=2026-11-19T18:26:44
```

**Baris terbaru yang bisa dicapai endpoint ber-`created_at` 2026-11-19.** Dua posisi probe BTC
(created 2026-11-21 06:20Z dan 06:50Z) TIDAK ADA di riwayat closed yang bisa kita baca, dan tidak
ada di sheet. Padahal kita MEMANG pernah melihatnya terbuka: `list_positions(statuses=["open"])`
mengembalikan posisi BTC dengan `avg_entry_price` 57.781,40 pada sampel kedua.

Kesimpulan yang sah saat ini:
- Posisi probe **terbentuk** (fill terbaca dari bursa) lalu **tidak lagi terbuka** dalam <60 detik.
- Kita **tidak bisa membuktikan** bagaimana ia berakhir, karena `perps_list_positions(closed)`
  yang kita pakai tidak memberi akses ke baris terbaru — konsisten dengan verdict alat itu sendiri:
  `TIDAK BISA DIVERIFIKASI: kemungkinan terpotong — respons tidak memuat total/cursor dan klien
  tidak punya parameter paginasi`.
- Jadi akar masalah P1 yang TERBUKTI sekarang adalah **lubang observabilitas di sisi kita**
  (tidak ada paginasi pada daftar posisi tertutup), BUKAN (belum) kegagalan TP/SL di sisi bursa.
  Hipotesis "TP/SL terpicu seketika" dan "order ber-margin minimum dibalikkan bursa" masih HIDUP
  dan belum bisa dibedakan dari data yang kita punya.

Yang saya TIDAK lakukan: menyimpulkan penyebab hilangnya posisi. Itu akan jadi tebakan, dan
tebakan di jalur perdagangan nyata adalah kelas kesalahan yang persis sedang kita berantas.

Urutan pekerjaan berikutnya (butuh persetujuan/aksi pemilik di langkah 3):
1. **Pasang paginasi** pada `WaltClient.list_positions` (cursor/offset + total bila ada) supaya
   riwayat closed terbaru benar-benar tercapai; tegakkan dengan test yang menolak "daftar dianggap
   lengkap tanpa bukti".
2. **Audit `autoClose`**: untuk order ber-`preview_order` dengan TP/SL yang dihitung dari
   `latest_rate` sebelum fill, buktikan apakah trigger price bisa sudah terlampaui oleh harga fill
   (kasus long dengan TP/SL berbasis harga kuotasi, bukan harga isi).
3. **Cek riwayat akun di sisi Walt** (dashboard/Telegram pemilik) untuk dua trade BTC
   06:20Z dan 06:50Z: hanya pemilik yang bisa melihat apakah trade itu muncul, dan dengan PnL apa.
   Itu satu-satunya cara membedakan hipotesis yang tersisa.
Sementara 1-3 belum selesai: **jangan tambah sampel probe, dan jangan naikkan ukuran posisi apa pun.**

## 3q. Verifikasi redeploy Apps Script pemilik (2026-11-21 07:0xZ, job read_only #44880581116)

Berhasil sebagian, dan perbedaannya penting:

| Jalur | Sebelum | Sekarang |
|---|---|---|
| `doGet` webhook (Research_Read) | text/plain 42 byte (stub) | **masih `html_or_unparsable`** |
| gviz `Daily_Scans` (fallback) | ok, 39 baris, SEMUA basi, terbaru 2026-11-20T00:17Z | **ok, 44 baris, 5 SEGAR, terbaru 2026-11-21T00:17Z** |
| verdict riset | SAKIT (perlu tindakan) | **SEHAT (jalur baca riset berfungsi)** |

Artinya pipeline riset sekarang hidup lagi - lewat gviz, dengan 5 tambahan baris scan dan aset
segar dalam jendela 13 jam. Yang belum: `doGet` masih membalas bukan JSON, sehingga (a) tab
`Research_Read` tidak bisa dibaca lewat webhook, dan (b) kita belum melihat `"version":
"2026-11-21.1"`. Kemungkinan yang tersisa: deployment aktif masih versi lama untuk jalur GET,
atau `doGet` di file yang ter-deploy tidak identik dengan repo. Verifikasi yang menutup ini cuma
satu baris: buka `<WEBHOOK_URL>/exec?probe=1` dan tempel jawabannya ke saya - kalau JSON berisi
`version` + header tiap tab, jalur GET pulih dan kolom `mg_*` akan muncul pada tulis berikutnya;
 kalau masih teks 42 byte, yang ter-deploy bukan kode repo.
Konsekuensi sementara yang harus diterima: riset crypto yang baru saya sambungkan (3p.2) membaca
hasil lewat gviz, bukan webhook - itu sudah cukup untuk jalan, tetapi audit "riset per aset per
hari" jadi bergantung pada satu jalur saja.

### 3p.7 Riwayat akun pemilik menutup sebagian pertanyaan P1, dan menggugurkan hipotesis saya sendiri

Screenshot riwayat (2026-11-21) memuat, berurutan: `XAG Short opened` → `XAG Take Profit
executed +0,07` → **`BTC Long opened 6:31` + `BTC Long closed 6:31 −0,001`** (menit yang sama) →
`PLTR/XAG Long opened 10:46` → `PLTR Long closed 12:10 +0,01` → `XAG Long closed 12:20 +0,02` →
**`BTC Long opened 1:50` + `BTC Long closed 1:51 +0,0008`** → `COIN Short opened 1:53`.

Dua pasangan BTC itu adalah kedua sampel probe saya. Yang terbukti:

1. Posisi probe **benar-benar terbentuk di bursa** (ada di riwayat akun) dan **ditutup bursa
   dalam ≤1 menit**, dengan PnL ±0,001 USD = **±0,02% dari notional $5**.
2. Ini BUKAN penutupan oleh TP/SL kita. Baseline BTC adalah `get_asset_baseline_tpsl_pct('BTC')
   = (TP 0,020, SL 0,015)`; slippage terukur +0,141% / −0,015% berada jauh di dalam kedua batas
   itu. Hipotesis "TP/SL terpicu seketika" yang saya ajukan satu paragraf sebelumnya **saya
   gugurkan sendiri** setelah membaca konstantanya. Riwayat itu juga menampilkannya secara visual:
   close TP diberi label eksplisit `XAG Take Profit executed +0,07`, sedangkan dua baris BTC
   hanya `closed` dengan PnL nyaris nol.
3. Ini juga BUKAN close dari probe: `preview_close` kita ditolak dengan
   `open_position=False (No open position exists...)` karena posisinya memang sudah tidak ada.
4. Ukuran saja bukan penjelasnya: PLTR dan XAG berjalan pada notional ~$4,89 dan **bertahan
   83-93 menit** sampai monitor menutupnya karena `Funding Flip Sign`. Jadi bukan "semua clip
   kecil langsung ditutup".

Hipotesis yang tersisa (belum teruji, dan saya tidak menyimpulkan salah satunya):
(a) floor notional/ukuran minimum berlaku per kontrak dan BTC punya ambang lebih tinggi dari
`min_size_base=7e-05` yang kita hormati; (b) kombinasi `autoClose` yang dipasang dalam request
`preview_order` yang sama membuat bursa membatalkan posisi baru; (c) aturan khusus produk untuk
BTC pada margin sangat kecil.

Percobaan pembeda yang sudah siap jalan (alat sudah di-upgrade, commit di bawah):
- `--margin 5.00` (notional $10 = 2,5x `min_size_base`) dengan pemantauan kelangsungan tiap 20s
  sampai 120s. Bila bertahan -> mendukung (a). Bila tetap tertutup ≤1 menit -> (a) gugur, dan
  langkah berikutnya adalah membuka TANPA TP/SL untuk menguji (b).
Run percobaan ini **tertolak oleh pintunya sendiri** (`DITOLAK: sudah ada 1 posisi terbuka` -
COIN short Anda), persis perilaku yang diinginkan: probe tidak menumpuk risiko di akun live.
MAINTENANCE_MODE sudah dikembalikan `false`.

### 3p.8 Sampel ke-3 pada margin $5,00: bertahan ≥80 detik, lalu tetap hilang — dan kesalahan desain saya

Probe ketiga (`--margin 5.00`, `--allow-other-positions`, MAINTENANCE_MODE=true):

```
Catatan audit: 1 posisi lain sedang terbuka (tidak disentuh probe ini):
  - lain:COIN:short:margin=2.45551400
Clip PRODUKSI: BTC long lev 2 margin $5.00 (diminta $5.00, min_size_base=7e-05,
               rate sebelum open=57724.700000)
ENTRY: fill bursa=57726.500000 vs rate sebelum execute=57724.700000 -> slippage masuk +0.0031%
+20s: MASIH TERBUKA   +40s: MASIH TERBUKA   +60s: MASIH TERBUKA   +80s: MASIH TERBUKA
close_ok=False posisi_sisa=0  checks=[open_position=False(No open position exists...)]
```

Tiga hal yang kini terbukti:
1. **Slippage masuk terkecil yang pernah kita ukur: +0,0031% (0,3 bps)** pada clip $10 notional.
   Tiga sampel sekarang: −0,0154% / +0,1411% / +0,0031%. Dua dari tiga di bawah 2 bps.
2. **Ukuran clip memang menentukan kelangsungan**: $2,50 margin tertutup ≤1 menit (dua kali),
   $5,00 margin bertahan ≥80 detik. Hipotesis (a) di 3p.7 karena itu TIDAK gugur - ia justru
   menguat, tapi tidak cukup sebagai penjelasan penuh.
3. **Tetap ada penutupan yang tidak kita lihat**: antara +80s dan upaya close (~+95s) posisi
   sudah hilang, dengan `posisi_sisa=0`. Monitor tidak mungkin pelakunya (jadwalnya 08:20Z,
   jendela kita 08:15:20-08:16:45Z). Jadi pelaku ketiga masih belum terbukti.

Kesalahan desain saya sendiri, dan mengapa penting: saya menambahkan pemantauan kelangsungan
90 detik SEBELUM close - dan justru itu yang membuat kita **kalah balapan dengan si penutup
tak terlihat**, sehingga slippage keluar kembali TIDAK TERUKUR. Urutan yang benar adalah
**close dulu, baru observasi** (dua mode terpisah). Ini saya perbaiki sebelum sampel berikutnya;
tanpa perbaikan itu, sebanyak apa pun sampel tidak akan pernah menghasilkan angka sisi keluar.

Status akun setelah run: `posisi_sisa=0` (terverifikasi oleh alat), MAINTENANCE_MODE dikembalikan
`false`, alert Telegram terkirim (percobaan pertama HTTP 400 karena parse_mode, retry tanpa
parse_mode berhasil). Posisi COIN short Anda tidak disentuh - dan kini ada test yang mengunci
bahwa probe tidak punya jalur menutup posisi milik orang lain.

Yang dibutuhkan untuk menutup pertanyaan "siapa yang menutup": satu baris dari riwayat akun Anda
untuk BTC long 08:15Z (15:15 WIB) - apakah berlabel `Take Profit executed`, `Stop Loss`,
`Liquidated`, atau hanya `closed`. Label itu membedakan hipotesis (b) dan (c) tanpa perlu order
baru apa pun.

### 3p.9 Eliminasi selesai: yang tertutup adalah BTC-nya, bukan clip atau TP/SL kita

Field status bursa (dibaca read-only, run #44888404007) menggugurkan sisa tersangka teknis:

| Field BTC | Nilai | Implikasi |
|---|---|---|
| `venue_status` / `is_active` | `active` / `true` | bukan karena kontrak non-aktif/jam tutup |
| `taker_fee_pct` / `maker_fee_pct` | `0.0009` / `0.0` | **fee kita terkonfirmasi presisi** (0,18% round-trip), dan screenshot ($0,00884 / $9,82 = 0,09%) cocok |
| `tpsl_buffer_pct` | `0.0002` | TP/SL kita (1,5%-2,0%) jauh di atas buffer - bukan pelanggarannya |
| `margin` (bps notional) | init default 500 / min 200 / maint 120 / **closeout 80** | margin kita 50,9% notional = 5092 bps, bukan 80 bps -> **bukan likuidasi/closeout** |
| `min_size_base` / `min_size_quote` | `0.00007` / `1.0` | posisi kita 0,00017 BTC / $9,82 -> di atas keduanya, **bukan dust-order** |
| `funding_rate_hourly` | `-0.000008` (=-0,19%/hari), next 10:00Z | probe 08:15-09:09Z tidak berimpit dengan funding |

Probe kelima (`--no-tpsl`, tanpa TP/SL sama sekali) tetap hilang dalam hitungan detik:
`fill 57.877,80 vs rate 57.897,10 -> -0,0333%`, lalu `No open position exists`. Jadi
pemasangan autoClose **bukan** pemicunya.

Pola pembeda yang tersisa dan konsisten di semua data: **yang mati selalu BTC, sementara
PLTR/XAG/COIN pada notional yang sama kecilnya (~$4,9) bertahan 83-93 menit sampai monitor
menutupnya sendiri.** Dengan semua penjelasan teknis di sisi kita sudah tereliminasi, sisa
kemungkinan adalah aturan minimum POSISI khusus BTC di bursa (lebih tinggi dari `min_size_base`
yang kita hormati) - dan itu hanya bisa dikonfirmasi Walt, bukan dari API yang kita punya.

Dua keputusan yang saya rekomendasikan (keduanya milik Anda):
1. **Sementara: jangan buka BTC lewat jalur otomatis** sampai ambang minimum posisi BTC
   dikonfirmasi. Kalau benar, setiap entry BTC di clip kita akan tertutup seketika dengan rugi
   = fee round-trip (~0,18%) - kebocoran PnL yang senyap, bukan kegagalan yang terlihat.
2. **Ukur slippage keluar di aset yang bertahan** (mis. XAG/ETH clip $5) - di sana close kita
   tidak akan kalah balapan, dan angka sisi keluar yang selama ini TIDAK TERUKUR akhirnya dapat
   diambil.

Slippage masuk yang terukur sampai kini (5 sampel): −0,0154% / +0,1411% / +0,0031% / −0,0341% /
−0,0333% → median |0,0333%| ≈ **3 bps**. Bersama fee bursa yang kini terkonfirmasi 0,09%/sisi,
dasar biaya round-trip yang realistis adalah **±0,24%**, bukan 1,18% - sekitar **5x lebih ringan**
dari angka yang selama ini dipakai semua simulasi kita. Angka itu masih menunggu sisi keluar
sebelum boleh menggantikan `COSTS`.

### 3p.10 Koreksi penting: penyapuannya BUKAN khusus BTC, dan bukan pekerjaan job kita

Probe keenam (`--asset XAG`, margin $5,00) menghasilkan:

```
Clip PRODUKSI: XAG long lev 2 margin $5.00 (min_size_base=0.08, rate sebelum open=41.268700)
ENTRY: fill bursa=41.265000 vs rate sebelum execute=41.268700 -> slippage masuk -0,0090%
Langsung mencoba close (mode default)
close_ok=False posisi_sisa=0  checks=[open_position=False(No open position exists...)]
```

Jadi pernyataan saya di 3p.9 - "yang mati selalu BTC" - **SALAH**. XAG juga disapu dalam hitungan
detik. Yang masih benar dari 3p.9: semua penjelasan teknis (dust, TP/SL, autoClose, closeout,
jam trading, funding) sudah tereliminasi.

Yang baru tereliminasi sekarang, dengan bukti jadwal run:
- **Bukan job kita yang menutup.** Pada 09:00, 09:10, 09:11, 09:20Z `Walt Risk Monitor` dan
  `Walt Crypto Execute` berstatus **skipped** (MAINTENANCE_MODE=true), jadi tidak ada proses kita
  yang aktif di jendela probe. Probe 08:20Z juga tidak bersentuh monitor (run-nya skipped).

Pola yang tersisa, dan ini yang membedakan probe dari live: **posisi yang dibuka lewat jalur
produksi bertahan (COIN short dengan TP $98,21/SL $101,53; PLTR/XAG 83-93 menit), sementara
posisi yang dibuka dari job diagnostics singkat selalu disapu dalam <1 menit - tanpa peduli
aset, ukuran, atau ada/tidaknya TP/SL.** Screenshot akun pemilik menampilkan
`Managed by walt-trading-bot · Disconnect`, yang memberi mekanisme yang masuk akal: bursa
mungkin menyapu posisi yang dibuka oleh sesi manager yang tidak lagi terhubung. Ini masih
**hipotesis** - saya tidak punya bukti penyebabnya - tapi ia konsisten dengan SEMUA data yang
kita punya, termasuk kenapa posisi live tidak pernah mengalaminya.

Konsekuensi yang saya ambil:
1. **Probe dihentikan.** Enam order nyata menghasilkan lima angka slippage masuk dan NOL angka
   slippage keluar; menambah sampel dengan metode yang sama tidak akan pernah menghasilkan sisi
   keluar, hanya membayar fee (~$0,002 per sampel).
2. **Pindahkan pengukurannya ke jalur produksi** (tanpa order tambahan): catat `latest_rate`
   tepat sebelum `execute` di `open_position`, bandingkan dengan harga fill yang dilaporkan
   bursa; lalu biarkan penutupan alami (monitor/autoClose) yang mengukur sisi keluar. Itu
   mengukur perilaku yang benar-benar kita jalankan, bukan perilaku sesi diagnostics.
   Butuh persetujuan Anda karena ini menyentuh jalur live (logging saja, tidak mengubah
   keputusan apa pun).
3. Slippage masuk yang terukur tetap sah sebagai angka: lima sampel (−0,0154% / +0,1411% /
   +0,0031% / −0,0341% / −0,0333%) + XAG (−0,0090%) → median |0,0191%| ≈ **2 bps**. Bersama fee
   bursa yang terkonfirmasi 0,09%/sisi, dasar round-trip realistis tetap **±0,2%**, BUKAN 1,18%.
   Sisi keluar masih TIDAK TERUKUR, dan karena itu `COSTS` belum saya ganti.

## 3r. Loop tertutup di produksi: biaya terukur menggerakkan gerbang, dan satu temuan BNB (2026-11-21 11:00Z)

Run `crypto-execute` #44896814228 (`success`) memverifikasi tiga hal sekaligus, tanpa satu pun
angka yang saya karang:

1. **Telegram Markdown v1 bersih.** `sendMessage` menjawab `200 OK` pada percobaan pertama;
   tidak ada `400 Bad Request`, tidak ada retry tanpa `parse_mode`. Penyebab yang tersisa
   ternyata bukan hanya underscore: set escape kita sendiri salah (kita mengirim `\]`, yang
   di Markdown v1 BUKAN escape valid - itu MarkdownV2). Sekarang `md_escape()` hanya menyentuh
   `_ * ` `[`, dan `md_italic()` membuang pembungkus miring bila isinya masih memuat
   underscore. Prinsipnya: notifikasi "posisi baru dibuka" tidak boleh gagal demi gaya.
2. **Loop pengukuran -> gerbang berjalan sendiri.** Cache `walt-logs-state-*` membawa
   `logs/slippage_observed.jsonl` berisi satu sampel produksi (SOL, -5,73 bps), sehingga
   `measured_slippage_basis()` mengembalikan `2 x 5,73 bps = 0,1146%` dan E5 memakainya
   tanpa perintah apa pun:
   `GO LONG BNB ... hold=24h, exp_funding=0.557%, cost=0.295%, req=0.442%`
   (sebelumnya `cost=0.280%` / `req=0.420%` saat masih memakai floor 0,10%). Ini bukti bahwa
   dasar biaya sekarang hidup dari data, bukan dari konstanta yang kita tulis.
3. **Posisi produksi bertahan sehat:** `Open positions verified: [SOL, SUI, COIN]`, slot 3/5.
   Ini juga memperkuat §3p.10 dari sisi berlawanan: posisi yang dibuka jalur produksi TIDAK
   disapu, sedangkan yang dibuka dari job diagnostics singkat selalu disapu <1 menit.

### 3r.1 Temuan BNB: preview lolos, execute menolak — dan saya menolak menebak rumusnya

```
BNB: margin [nilai dihapus di log CI] disesuaikan ke batas minimum kontrak 2.68 USD (min_size_base=0.01 @ harga 524.94 USD)
preview_order -> HTTP 200 OK
execute(token) -> Walt MCP error (perps_execute_operation):
  {"error":{"code":"agent.incorrect.order_params",
   "message":"Incorrect data for creating order: margin < contract.min_size_base",
   "retryable":false}}
```

Yang bisa dihitung dari angka yang ada: `0.01 x 524.94 / 2 = $2,6247` adalah margin minimum
teoretis, dan buffer kita (`x1.015 + $0.02`) memberi `$2,68` - secara aritmetika DI ATAS
angka itu, tapi bursa tetap menolak. Artinya ada syarat bursa yang TIDAK terexpose ke kita
(cadangan fee/haircut/pembulungan step_size internal). Saya sengaja TIDAK menyimpulkan
rumusnya: menebak konstanta buffer baru akan menyembunyikan fakta bahwa kita tidak tahu,
dan itu persis kelas kesalahan yang sudah tiga kali kita tangkap di ronde ini.

Dua hal yang belum saya kerjakan karena berada di luar wewenang yang Anda berikan:
(a) menaikkan `MARGIN_BUFFER` / buffer minimum margin - itu parameter strategi, dan
(b) menambahkan retry satu kali pada margin lebih besar saat kode error
`agent.incorrect.order_params` muncul - itu mengubah jalur entry nyata. Usulan yang aman
dan bisa dibalik: retry SEKALI dengan `margin x 1.10`, hanya untuk kode error itu, tetap
dibatasi `max_allowed_margin`, dan kegagalan tetap dilaporkan sebagai `FAILED` seperti
sekarang. Kalau Anda setuju, itu satu komit kecil dengan test.

Status saat ini: 496 test lulus, `sanitize_gate` bersih, YAML 7 valid, `main == origin/main`.
Yang berubah di dokumen saja pada komit ini.

Catatan format: angka harga/margin di bagian ini sengaja ditulis `524.94 USD`, tanpa awalan
tanda dolar. Gerbang sanitasi mengenali pola "tanda dolar + 2-4 digit + titik + 2 digit" sebagai
saldo akun yang tersalin dari log run (AUDIT/README tidak mendapat pengecualian kelas itu, dan
itu memang disengaja), jadi menulis contoh yang cocok dengan pola itu — bahkan di dalam
penjelasan — akan menggagalkan CI.

### 3r.2 Retry min-size diimplementasikan (keputusan pemilik, `ee1656f`)

`open_position` kini mencoba SEKALI lagi dengan `round(margin x 1,10, 2)` bila bursa menolak
dengan `agent.incorrect.order_params` / "margin < contract.min_size_base". Empat pengaman,
masing-masing punya test (`tests/test_min_size_retry.py`, 7 test):

1. hanya kode error itu yang memicu retry — `is_min_size_rejection()` menolak semua error lain
   (test parametrik: timeout, insufficient_balance, agent.not_logged_in, bahkan string
   "margin < contract.min_size but fine" yang mirip tapi bukan marker);
2. maksimal SATU retry — kalau percobaan kedua ditolak juga, hasilnya `FAILED`, tidak ada
   retry ketiga;
3. retry tetap terikat `max_allowed_margin = max(requested x 1.5, DEFAULT_MARGIN x 1.5)` dan
   `has_enough_job_time(AUTO_CLOSE_RETRY_BUDGET_MIN)` — lewat plafon berarti tidak ada order;
4. `result["actual_margin"]` = margin yang benar-benar terisi, jadi `deployed`, buffer kas,
   dan `Executions_Log` di `market_execute`/`scan_with_research` tetap presisi (caller sudah
   membaca `actual_margin` sejak lama — tidak ada jalur baru yang perlu diubah).

Yang juga dipastikan test: `record_open_slippage` hanya dipanggil untuk percobaan yang terisi,
sehingga `logs/slippage_observed.jsonl` tidak pernah mencatat slippage dari order yang ditolak.

Belum terverifikasi di produksi pada saat komit ini dibuat: run `crypto-execute` #44898584165
masih berjalan. Yang dicari di log-nya: `bursa menolak margin … retry SEKALI dengan …` diikuti
`✅ … opened: margin $…` dengan nilai retry, dan tanpa `FAILED BNB`.

### 3s. Plafon ATAS bursa & pemulihan kredensial (keputusan pemilik, terverifikasi produksi)

**Fakta terukur.** Probe `contract_schema` (#44926211013) membuktikan `max_size_base` dan
`max_size_quote` ada di **139/139** kontrak - bukan anomali ANTHROPIC. Kasus itu sendiri:
margin 2,50 @ lev 2 = notional 5,00 USD sudah melewati `max_size_quote` kontrak tersebut,
artinya **plafon atas mereka bisa lebih kecil daripada clip minimum kita**. Selama ini
`resolve_order_margin` hanya menegakkan batas BAWAH, jadi pelanggaran seperti itu baru
ketahuan setelah execute (dan, lewat bug retry Ronde 8, sempat diperparah dengan margin
YANG LEBIH BESAR).

**Keputusan (pemilik): SKIP, jangan turunkan clip.** Kalau notional atau ukuran hasil
perhitungan melewati plafon kontrak, aset dibuang dengan log - konsisten dengan jalur
buang `min_size_base`. Menurunkan margin ke bawah `DEFAULT_MARGIN` ditolak dengan alasan
ekonomi, bukan prinsip: clip remah membuat fee tetap memakan carry, dan floor 2,50 USD ada
justru supaya kita tidak membuka posisi yang secara matematis rugi sejak awal.

**Bacaan yang ditegakkan = paling ketat.** Bursa menulis pesannya `"margin >
contract.max_size_quote"` padahal nama field-nya `quote` (= notional). Kita memeriksa
`margin x leverage <= max_size_quote` dan `margin x leverage / price <= max_size_base`
secara serentak; `0` berarti tidak dibatasi, tepat di plafon berarti boleh. Asimetri
sengaja: over-skip hanya kehilangan opportunity, under-skip kehilangan kredensial dan
meninggalkan posisi. Kalau nanti ingin melonggarkan ke bacaan "margin murni", itu harus
menjadi keputusan tercatat, bukan efek samping.

**Akar terbakarnya kredensial CI (#44926211013, 16:44:44Z) dan tiga lapis penutupnya.**
Refresh token Walt itu single-use. Langkah `contract_schema` saya menyetel
`WALT_TOKEN_SYNC: "on"` tetapi TIDAK mengoper `GH_PAT` + `GITHUB_REPOSITORY`, jadi token
baru terbakar tanpa tempat disimpan -> `invalid_grant` untuk semua run berikutnya.
(1) YAML: langkah itu kini membawa ketiga kunci. (2) Invarian test
`test_langkah_oauth_wajib_membawa_tiga_kunci_persistensi` memindai SEMUA workflow: env
dengan `WALT_REFRESH_TOKEN` wajib memuat `WALT_TOKEN_SYNC` + `GH_PAT` +
`GITHUB_REPOSITORY`. Test ini langsung menemukan empat jalur produksi yang selamat hanya
karena *default* (`crypto-execute`, `daily-scan`, `market-execute`, `risk-monitor`) - kini
ketiganya ditulis eksplisit, karena default tidak bisa diaudit. (3) Kode:
`require_persistable_refresh()` adalah baris pertama `_refresh()`, dan menolak SEBELUM
`httpx.post` - dites dengan menambal post dan menuntut nol panggilan, jadi kegagalan
konfigurasi tidak mungkin lagi membakar token di server Walt.

**Vonis `err_detail` yang salah arah.** Pemicu `Telegram HTTP 400` pada alert OAuth bukan
teks error yang lupa di-escape (variabel itu tidak ada), melainkan **code-span di dalam
bold**: `*ALERT OAUTH `invalid_grant`*`. Markdown v1 tidak mengizinkan entity bersarang -
kelas bug yang sama persis dengan `*MARKET_UNIVERSE*` + `GEAR_2_ACTIVE_60M`. Bentuk benar:
bold berisi teks yang sudah `md_escape` (`invalid\_grant`).

**Verifikasi produksi pasca-pemulihan (#44929762497, 17:23:13Z).** Rantai kredensial
terbukti utuh: `POST /oauth/v1/token` 200 -> `PUT .../secrets/WALT_REFRESH_TOKEN` **204**
-> `AUTO-SYNC berhasil` (guard tidak sekadar lolos; jalur tulis-balik yang bekerja).
Perbaikan Telegram terbukti di kasus yang paling mungkin gagal: satu run memicu KEDUA
adaptive execute (`MARKET_UNIVERSE`/`GEAR_2_ACTIVE_60M` + `CRYPTO_UNIVERSE`/
`GEAR_1_CONSERVATIVE_120M`) dan ketiga pengiriman menjawab 200 pada percobaan pertama,
tanpa satu pun retry tanpa `parse_mode`. Run ditutup `success` dengan posisi 0 dan
`batas harian tercapai (8/8 entry dalam 24 jam)` - kas 100% cair.

**Yang MASIH belum terverifikasi di produksi (jangan ditulis sebagai selesai).**
(a) Retry min-size §3r.1 untuk BNB - BNB terus diveto P5 sebelum mencapai `open_position`,
jadi jalur retry-nya belum pernah benar-benar terpicu;
(b) probe cabang `str` (`__has_plausible_price__`) untuk sisi `close` - menunggu penutupan
alami berikutnya; selama belum ada, `slippage_report` berhak menolak rumus dua sisi;
(c) gerbang `max_size` yang SKIP sebelum execute - akan terbukti saat ANTHROPIC (atau
kontrak ber-plafon-kecil lain) kembali menjadi kandidat; yang dicari di log:
`SKIP — notional ... melewati plafon kontrak max_size_quote`.

### 3t. Utang terakhir Ronde 8 ditutup: redeploy Apps Script v2 TERBUKTI di runner (2026-11-22)

Satu-satunya run yang di ronde sebelumnya saya tinggalkan dalam keadaan merah karena sebab yang
belum dikenal adalah `Walt Read-Only Diagnostics` #44926211013, langkah **probe webhook**.
Penyebabnya bukan kode dan bukan kredensial: URL `GOOGLE_SHEET_WEBHOOK_URL` masih menunjuk
deployment lama yang membalas stub 42 byte, sehingga `?probe=1` tidak mungkin JSON. Pemilik
melakukan redeploy (Apps Script -> Manage deployments -> New version, kode = `google_apps_script.js`
repo) dan memperbarui secret. Verifikasi: run **#44991590222** (`oauth_step=read_only`) **success
seluruh langkah**.

| Bukti | Isi |
|---|---|
| 1d `GET ?probe=1` | HTTP 200, `application/json`, `version = 2026-11-21.1`, 4 tab terbaca: `Daily_Scans` 54 baris, `Closed_Positions` 60, `Executions_Log` 565, `PnL_Tracker` 6 |
| 1d `GET` tanpa probe | HTTP 200, `application/json`, `count = 15` aset, versi terdeteksi cocok |
| 1d verdi | `BERSIH - doGet probe JSON, versi cocok, deployment = kode repo` |
| 1b baca riset | `SEHAT (jalur baca riset berfungsi)` |

**Apa yang dibuktikan dan apa yang TIDAK.** Terbukti: deployment aktif = kode repo, jadi rekonsiliasi
header berbasis nama + read-back verification akan aktif pada siklus tulis berikutnya, dan kelas
kegagalan "stub lama" mati. Belum terbukti: bahwa kolom `mg_*` **berisi nilai**. Cek versi tidak bisa
membedakan kolom-kosong dari kolom-terisi, padahal prasyarat P8 di 3p.4 adalah yang kedua - dan
kolom kosong membuat P8 menjadi ornamen yang diam, kelas kegagalan yang sudah dua kali kita tangani.

**Penutup berbentuk alat, bukan catatan.** `tools/webhook_probe.py` kini membaca `data` dari respons
GET yang SUDAH ada (nol panggilan tambahan, nol efek ke biaya/kecepatan CI) dan melaporkan
`isi mg_* pada N aset terbaru: mg_status=x/N, ...`. Secara **default hanya info** dan tidak memvonis,
karena P8 memang sengaja belum dinyalakan dan diagnostics hijau tidak boleh berubah merah karena
fitur yang dimatikan atas keputusan pemilik. `--require-mg` menegakkannya ketika P8 siap. Test
`tests/test_webhook_probe_mg_fill.py` mengunci tiga hal: penghitung per aset, penanda kosong bursa data
(`-`, `nan`, `null`, spasi) tidak dihitung sebagai bukti, dan - yang paling penting - setiap nama kolom
probe harus tetap ada di `sheet_logger.MG_COLUMNS`: kalau jalur tulis mengganti nama, pengukuran harus
merah, bukan melaporkan nol selamanya.

**Hasil pengukuran pertama (diagnostics #44994074044, 2026-11-22 06:31Z, `oauth_step=read_only`,
`success`):** `isi mg_* pada 15 aset terbaru: mg_status=0/15, mg_net_gravity=0/15, ...` dan
`VERDIK: BERSIH`. Kedua baris itu bukan kontradiksi: yang BERSIH adalah deployment, yang 0/15 adalah
isinya. Kesimpulan yang sah: **prasyarat P8 (a) BELUM terpenuhi** meskipun redeploy v2 benar, dan
"kolom ada" memang selalu bukti yang lebih lemah daripada "kolom terisi". Yang perlu dipisahkan di
Ronde 9: (i) belum ada siklus tulis `daily-scan` sejak port `market_gravity.py` (komit `dfd19ab`,
2026-11-21T04:43+07) yang menulis baris baru, atau (ii) jalur penulisnya memang belum tersambung ke
`MG_BLANK` yang sebenarnya. `MG_DIAGNOSTICS` bukan penjelasannya: default-nya `on` dan hanya
mati bila disetel eksplisit. Sampai salah satu terukur, P8 tetap tertutup.

### 3u. Ronde 9 ditutup: tiga bug header/units `Daily_Scans` dan keadaan FINAL-nya

Tiga bug nyata, semua di sisi Python (tidak ada yang menuntut redeploy Apps Script):

- **Bug A - key record vs label live.** `log_scan_via_webhook` mengirim nama internal
  snake_case, padahal `doPost` v2 membaca `record[header[c]]` secara PERSIS. Kalau itu
  dibiarkan jalan: 4 kolom yang kebetulan namanya sama tertulis KOSONG dan 15 kolom
  Indonesia dianggap kolom baru lalu DOBEL oleh `ensureHeader_()` - kerusakan header yang
  permanen. Perbaikan: `DAILY_SCANS_SHEET_HEADERS` = 19 label live + 11 `mg_*`; CSV lokal tetap
  memakai `DAILY_SCANS_HEADERS`.
- **Bug B - jalur baca selalu basi.** v2 `doGet` mengembalikan key berlabel sheet, sementara
  pembaca mencari `"timestamp"`/`"swarm_score"`. Akibatnya semua aset terbaca basi dan jalur
  baca diam-diam jatuh ke gviz meskipun deployment sudah benar. Perbaikan:
  `_normalize_research_item()` (+ alias `timestamp_utc` -> `timestamp`), kompatibel ke belakang.
- **Bug C - format persen vs satuan sheet.** Sheets menyimpan `-0.979%` sebagai desimal
  `-0.00979`, sehingga read-back v2 selalu berbeda untuk kolom `Funding/Hari ()`:
  `not_verified` -> retry 3x -> **6 baris ditulis untuk 2 baris** (#44998198762, rows 54->60).
  Perbaikan: kirim desimal (`_funding_to_decimal`), dan `sameValue_()` di repo turut menerima
  `X% == X/100` untuk masa depan. `WALT_WEBHOOK_VERSION` sengaja TIDAK dinaikkan: deployment aktif
  masih `2026-11-21.1`, dan menaikkan versi di repo akan membuat probe CI merah secara keliru.

**Bukti lulus (dikonfirmasi pemilik, #44998472430 `read_only`):** `HEADER mg_* di sheet: ADA (5/5),
total kolom sheet 30` (0 duplikat), `mg_status=2/15` dst., `VERDIK: BERSIH`, `sumber_terpakai =
webhook` dengan `5 aset segar dari webhook`, `verdict = SEHAT`, Telegram 200 percobaan pertama,
OAuth auto-sync hijau.

**Pelajaran yang berulang untuk keempat kalinya di proyek ini:** "kolom ada" bukan bukti "kolom
terisi", dan "HTTP 200" bukan bukti "nilai cocok". Yang menutup ketiga bug ini bukan pembacaan
kode, melainkan read-back verification yang memaksa perbedaan satuan terlihat - dan sekali lagi
dua assertion test lama justru MENGKODEKAN bug-nya (`sent["header"] == DAILY_SCANS_HEADERS`,
`records[0]["asset"]`), bukan menemukannya.

**Keadaan akhir (RONDE 9 SELESAI - terverifikasi produksi).** `daily-scan` #44998864306
(2026-11-22 07:37:29Z, `success`) melaporkan: `Google Sheet Webhook (Daily_Scans) terbukti: 3 baris
ditulis dan terbaca kembali (versi webhook 2026-11-21.1)` dan `{'rows_count': 3, 'webhook_logged':
True, 'webhook_write_status': 'ok', 'gravity_diag': {'ok': 3}}` - yaitu SATU tulis, `verified=true`,
dan diagnostik Market Gravity terisi `ok` untuk semua baris. `CI Unit Tests` 580 test, `Market
Execute` #44999490046, `Risk Monitor` #45000727083, dan `Watchdog` semuanya hijau. HEAD `869c936`.

Catatan yang tetap berlaku: baris dobel dari sebelum Bug C diperbaiki (rows 54-60) TIDAK dihapus -
itu jejak historis, bukan kerusakan yang sedang berjalan. Klaim "nol baris dobel" sekarang hanya
berlaku sejak siklus ini, bukan untuk seluruh isi ledger.

### 3v. Penyelarasan Pasca-Ronde 9: Pembersihan Daily_Scans, Jam Saham AS UTC, Gear 0–3, OAK Research MCP, Swarm GLM-5.3, dan Tab Data Training (2026-11-22)

Setelah Ronde 9 menutup seluruh bug webhook v2, tujuh pembaruan arsitektur diterapkan bertahap dan terverifikasi hijau di `CI Unit Tests`:

1. **Konsolidasi Header & Deduplikasi `Daily_Scans` (`c5de177`, `CI Unit Tests #45002571050`):** Menyelaraskan tab `Daily_Scans` menjadi tepat **30 kolom bernama** tanpa duplikat (`110` baris bersih) serta memperkuat `apps_script_webhook.gs` v2 (`2026-11-21.1`) agar write/read-back tetap deterministik.
2. **Bypass Batas Frekuensi 24 Jam untuk Verdict `'GO'` (`3c6f36b`):** `check_entry_frequency_limits()` di `walt_bot.py` membebaskan sinyal bervonis `'GO'` dari kuota `2 entry/aset/24h` dan `8 entry/24h` agar peluang funding kuat tidak tertahan, sementara `'HALF'` tetap dibatasi dan pengaman keras (*circuit breaker* `-5%`, *cooldown* 3 jam, batas slot dinamis, serta *cash buffer* 15%) tetap berlaku penuh.
3. **Filter Jam Bursa Reguler Saham AS UTC (`0c326bc`):** `is_us_stock_market_open_utc()` membatasi pemindaian riset AI dan pembukaan posisi baru `ALLOWED_STOCKS` ke jam operasional reguler NYSE/NASDAQ (`Senin–Jumat 13:30–21:00 UTC`, toggle `US_STOCK_HOURS_ONLY=true`). Komoditas & 10 Crypto Bluechip tetap aktif 24/7, dan `risk-monitor` tetap memantau seluruh posisi terbuka 24/7.
4. **Sistem 4-Tier Execution Gear `0..3` (`fc525ce`, `CI Unit Tests #45008153855`):**
   - `GEAR_0_IDLE_2X_US` / `GEAR_0_IDLE_720M` (`0` GO): `2x` selama jam bursa Saham AS (`225m`, maks 2x/sesi) untuk `market`, dan `2x/hari` (`720m`) untuk `crypto`.
   - `GEAR_1_CONSERVATIVE_120M` (`< 2` GO): `120m` (2 jam).
   - `GEAR_2_ACTIVE_60M` (`2..5` GO): `60m` (1 jam).
   - `GEAR_3_FULL_RESEARCH_60M` (`> 5` GO atau `GEAR_3_FULL_RESEARCH=true`): `60m` + mengizinkan riset & analisa AI penuh untuk **setiap keputusan trading** (mengangkat cap `max_otf_calls` & `CRYPTO_RESEARCH_TOP_N`).
5. **Pipeline SFT Dataset `Data Training` (`7b4039c` & `5cb6394`, `CI Unit Tests #45009622435`):** Membuat `training_data.py` & `tools/export_training_data.py` yang mencatat setiap riset tervalidasi dalam format chat-messages `<think>...</think>` + JSON (`logs/training_dataset.jsonl`), sekaligus menyiapkan tab Google Sheet **`Data Training`** (`12` kolom bernama, deduped `Sample ID`, seeded 48 baris historis bersih menuju target `>= 1.000` baris SFT). Kontrak isolasi `walt_bot.py` vs modul `market_gravity` dijaga melalui pembacaan cache internal di `training_data.py` (`602 passed`).
6. **Integrasi Live OAK Research Remote MCP Server (`65f9843` & `8c7ce24`, `CI Unit Tests #45018375476`):** Membuat `oak_mcp_client.py` & `setup_oak_oauth.py` yang terhubung langsung ke `https://oakresearch.io/api/mcp` (`Streamable HTTP 2025-03-26` + OAuth 2.1 PKCE), teruji live menyinkronkan skema alat aktual (`get_market_overview`, `get_calendar(from, to)`, `get_news`, `get_crypto(id, locale)`, `get_tradfi(ticker)` dengan 0 error) dan otomatis menulis balik rotasi `OAK_MCP_REFRESH_TOKEN` ke `.env` & GitHub Secrets.
7. **Swarm Hemat Berbasis OAK Benchmark & Aggregator `glm-5.3` (`341011f`, `c2a1e20`, `5d73c58`, `CI Unit Tests #45021257348`):** Menyesuaikan `GeminiClient` ke **ClassAI Router** (`https://router.classai.id/v1` via `DASHSCOPE_API_GEMINI_KEY`), menetapkan V1 `glm-5.2-fast-preview` + V2 `glm-5.3-prime` (DashScope `DASHSCOPE_API_KEY`), V3 `minimax-m3` (#7 OAK Max DD `-6.5%`, 0 liq) + V4 `claude-haiku-5.5` (ClassAI Router hemat), serta **CRO Meta-Aggregator utama `glm-5.3`** (#1 OAK Trading Benchmark winner `+29.4%`, 0 liq) melalui **DashScope (`DASHSCOPE_API_KEY`)** dengan fallback DashScope GLM/Qwen $\to$ ClassAI (`claude-opus-5.5` $\to$ `gpt-6.1-sol`) $\to$ deterministik (`608 passed, 1 skipped`).
8. **Kebijakan Hybrid Cost-Aware Menahan Posisi Saat Pasar Tutup (`OFF_HOURS_HOLD_POLICY=hybrid`, Opsi B — 2026-11-23):** Menambahkan `classify_asset_market_session()` dan `evaluate_off_hours_position_policy()` di `walt_bot.py` yang disambungkan ke `evaluate_and_apply_position_risk()` / `_run_single_monitor_pass()` serta `risk-monitor.yml`. Mencakup **Saham AS (`ALLOWED_STOCKS`)** pada *overnight* + *weekend* dan **Komoditas (`ALLOWED_COMMODITIES`)** khusus *weekend* (Jum `21:00`–Min `22:00 UTC`; Crypto tetap 24/7): (a) nol toleransi biaya inap (`favorable_funding < 0%` langsung tutup tanpa menunggu ambang `0.10%`/hari), (b) saringan pre-close `20:35–21:00 UTC` (*overnight* carry `>= +0.20%`/hari; *weekend* carry `>= +0.50%`/hari & PnL `>= -0.50%`, atau sudah profit-locked), serta (c) *early profit lock* SL `Entry +/- 0.25%` saat profit `>= +0.40%` (menghindari *fee bleed* tutup-buka `0.28%–0.45%` sambil melindungi posisi dari gap pembukaan; diverifikasi di `tests/test_off_hours_hold_policy.py`).
9. **Tahap 1 Perbaikan Riset Crypto (`llm_research.py`) & Riset Walk-Forward Forecast Harga 24 Jam (`price_forecast.py` + `tools/backtest_forecast_24h.py`, 2026-11-23):**
   - **Perbaikan bug `NO_DATA` Crypto (`crypto-execute #45129246139`):** `fetch_headlines()` kini mencoba kueri `<TICKER>-USD` untuk aset crypto, dan `research_stock()` tetap menjalankan Swarm AI menggunakan blok `[DATA REAL-TIME OAK RESEARCH MCP]` ketika headlines Yahoo kosong (hanya mengembalikan `NO_DATA` bila keduanya kosong).
   - **Hasil Pengukuran Walk-Forward OOS Nyata (`interval=1h`, `719` bar/aset Crypto Hyperliquid, `455` bar/aset Saham AS Yahoo Finance, `4` fold, SL dicek sebelum TP + Time-Stop 24 jam, biaya round-trip `0.28%` & `0.45%`):**
     - `fixed_15` (SL `1.5%` / TP `2.5%`): `251` trade, win `31.1%`, gross `-0.235%`, net biaya `0.28%` `-0.515%`, PF `0.56` (`SL=63%`, terlalu sempit untuk volatilitas 1h).
     - `atr` (SL `0.75 * atr_24h` dalam `1.5%–3.5%`, TP `1.5 * SL`): `197` trade, win `40.6%`, gross `+0.161%`, net biaya `0.28%` `-0.119%`, PF `0.92`.
     - Breakdown jujur per setup & kelas pada mode `atr`: **CRYPTO** mencatat **`121` trade, win `43.8%`, gross `+0.354%`, net biaya `0.28%` `+0.074%`, PF `1.05`** (satu-satunya segmen positif), sementara **STOCK** `-0.428%` pada biaya `0.28%` (`PF 0.70`, `TS=46%`). Setup `MEAN_REVERSION_FADE` (`61` trade, win `45.9%`, gross `+0.191%`, MaxDD `17.20%`) jauh mengungguli `MOMENTUM_BREAKOUT` (`156` trade, win `34.6%`, gross `-0.121%`). Karena `MOMENTUM_BREAKOUT` & segmen `STOCK` 1h belum positif terhadap `NULL_FLIPPED` (`+0.145%` gross), `price_forecast.py` tetap terisolasi (belum disambungkan ke jalur order live `walt_bot.py`) sampai penyaringan setup dipersempit.
10. **Tahap 2 Aktivasi Engine 2 Crypto Mean-Reversion Fade Terkalibrasi (`CRYPTO_FORECAST_ENGINE=on` — 2026-11-23):**
    - **Bukti Kalibrasi 4-Fold OOS Tambahan (10 koin Tier-1 Hyperliquid):**
      - `V5_4H_REJ_COMFORT` (`4h`, `900` bar = 150 hari, `hold=72h`, `sl_cap=4.5%`): `340` trade, win `54.7%`, gross `+0.470%`, net (0.28%) `+0.190%`, net (0.45%) `+0.020%`, PF `1.12` (positif bersih di kedua tingkat biaya & mengalahkan `NULL_FLIPPED` `-0.496%`; pada `sl_cap=6.0%`: `329` trade, net (0.28%) `+0.233%`, net (0.45%) `+0.063%`, PF `1.14`, positif gross 4/4 fold).
      - `V4_1H_EXTREME_FADE` (`1h`, `1000` bar, `hold=24h`, `sl_cap=4.5%`): `24` trade, win `58.3%`, gross `+1.027%`, net (0.28%) `+0.747%`, net (0.45%) `+0.577%`, PF `1.73` vs `NULL_FLIPPED` `-0.782%`.
    - **Implementasi Produksi (`walt_bot.py`, `price_forecast.py`, `risk-monitor.yml`, `crypto-execute.yml`):** Menyambungkan `scan_crypto_price_opportunities()` eksklusif ke jalur `crypto_execute` (mengabaikan `MOMENTUM_BREAKOUT` & Saham AS `1h`), menyaring `SPLIT`/`P5 >= 0.70`/lawan berita lewat Swarm AI (sentimen `neutral` diizinkan `GO`), menyimpan metadata `forecast_positions` di `logs/market_regime.json`, serta menegakkan ATR SL `<= 4.5%`, *adverse funding* darurat `>= 0.30%/hari`, dan **Time-Stop `24h/72h`** di `evaluate_and_apply_position_risk()` sambil menjaga isolasi nol referensi `market_gravity` di `walt_bot.py` dan `llm_research.py` (`tests/test_crypto_forecast_engine2.py`).

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
