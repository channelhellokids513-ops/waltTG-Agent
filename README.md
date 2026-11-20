# ⚡ Walt Perps Autonomous Funding Arbitrage Bot

[![GitHub Actions Status](https://github.com/channelhellokids513-ops/waltTG-Agent/actions/workflows/market-execute.yml/badge.svg)](https://github.com/channelhellokids513-ops/waltTG-Agent/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/Python-3.14-green.svg)](https://python.org)
[![Zero-Cost Infrastructure](https://img.shields.io/badge/Infra%20Cost-%240%20(Free%20Tier)-brightgreen.svg)](#-biaya-infrastruktur-100-zero-cost)

Autonomous delta-neutral & directional funding rate arbitrage trading system untuk platform **Walt Perps** (`walletbot.me`). Ditenagai oleh **Dual-AI Research Swarm (Qwen + Gemini)**, **Google Cloud Scheduler Precision Bridge**, sistem **Risk Monitor 4-Lapis**, dan pelaporan *real-time* ke **Telegram** serta **Google Sheets**.

---

## 🎯 Tujuan Repository

1. **Passive Yield Harvest:** Menangkap funding rate ekstrem pada bursa perpetual futures Walt sebagai sumber pendapatan pasif terukur (Target: **$100.00 USDC / bulan**) dengan leverage konservatif (1x–2x).
2. **Zero Directional Disaster:** Melindungi modal dengan model **True Funding Carry & Asymmetric R:R** (TP `+2.0%/+2.5%`, SL seragam `1.5%`, **True Zero-Risk Trailing Tiers** `+0.80% → +0.35%`, `+1.40% → +0.85%`, `+2.00% → Trailing 0.60%`, serta **3-Hour Re-Entry Cooldown**) sehingga profit terlindungi bersih setelah biaya *taker* (`0.18%` *round-trip* + *spread*).
3. **100% Zero-Cost & Autonomous:** Beroperasi penuh 24/7 di atas Free Tier cloud (GitHub Actions, Google Cloud Scheduler, Google Sheets, Telegram Bot API) tanpa memerlukan PC/server lokal aktif.
4. **Independent Asset Modules:** Memisahkan workflow eksekusi saham US/komoditas dengan crypto bluechip agar keduanya dapat diaktifkan/dinonaktifkan secara independen sesuai kondisi pasar.

---

## 🏗️ Arsitektur Sistem

```
┌────────────────────────────────────────────────────────────────────────┐
│               GOOGLE CLOUD SCHEDULER (Jakarta asia-southeast2)        │
│    • walt-market-execute (14:43 & 21:43 WIB Setiap Hari 24/7)          │
│    • walt-daily-scan     (07:17 WIB Daily)                             │
│    • walt-risk-monitor   (Tiap 1 Jam di menit 23 WIB)                  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ (Exact-second HTTP POST trigger)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   GITHUB ACTIONS CLOUD RUNNERS                         │
│   ├─ daily-scan.yml      ──> Scan, Swarm AI & Instant Auto-Execute     │
│   ├─ market-execute.yml  ──> Buka Posisi Saham US & Komoditas          │
│   ├─ crypto-execute.yml  ──> Buka Posisi Crypto Bluechip (BTC/ETH/SOL) │
│   └─ risk-monitor.yml    ──> Evaluasi 4 Lapis Risiko & Trailing Stop   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
               ┌────────────────────┼────────────────────┐
               ▼                    ▼                    ▼
     ┌───────────────────┐┌───────────────────┐┌───────────────────┐
     │ Walt Perps Relay  ││  Dual-AI Swarm    ││  Telemetry & Log  │
     │  (MCP & OAuth)    ││ (Qwen 50/50 Gemini││ (Telegram Alerts &│
     │ • Bearer Auto-Sync││ • P1–P7 Scoring   ││  Google Sheets)   │
     │ • Min Size Clamp  ││ • Meta-Aggregator ││ • 3x Auto-Retry   │
     └───────────────────┘└───────────────────┘└───────────────────┘
```

---

## 💎 Fitur Utama & Guardrails

### 1. Dual-AI Swarm Consensus (50/50 Vendor Balance)
Analisis sentimen berita, katalis earnings, dan momentum makro dilakukan oleh 4 voter seimbang dari dua vendor terdepan:
- **Alibaba Cloud:** `qwen-flash` & `qwen-plus` (V1 dulunya `qwen-turbo` — dipensiunkan upstream; lihat *Jendela Pemeliharaan & Pemulihan Kredensial*)
- **Google Cloud:** `gemini-flash-latest` & `gemini-pro-latest`
- **Meta-Aggregator:** `qwen-max` mengevaluasi konsensus 7 parameter standar (**P1–P7**) untuk menghasilkan keputusan:
  - `GO` (Full size $2.50 – $3.00 margin)
  - `HALF` (Half size $1.25 – $1.50 margin jika katalis risiko P5 $\ge 0.70$)
  - `SKIP` (Batalkan jika melawan sentimen / earnings $\le 7$ hari)

### 2. Asymmetric R:R Take Profit (TP) & Uniform 1.5% Stop Loss (SL) — True Funding Carry
Menghindari *fee bleed* (`0.18%` round-trip taker fee + spread) dengan *Risk-to-Reward* asimetris dan ruang napas yang cukup untuk memanen *funding rate*:
- **ETF / Saham Kalem (SPY, US100, AAPL):** TP +2.0%, SL 1.5%
- **Crypto Tier-1 (BTC, ETH, SOL, BNB, XRP, AVAX, SUI, LINK, NEAR, AAVE):** TP +2.0%, SL 1.5%
- **Komoditas (XAU, XAG, BRENTOIL):** TP +2.5%, SL 1.5%
- **Saham Volatil (NVDA, TSLA, MSTR, CRCL, SNDK, VVV, dll):** TP +2.5%, SL 1.5%
- **Windfall Rule:** Jika $2.5\times$ funding harian berada di rentang $1.5\% \le \text{windfall} < \text{TP baseline}$, TP dimajukan ke level tersebut di server bursa.

### 3. True Zero-Risk Trailing Stop & 3-Hour Re-Entry Cooldown
- **Tier 1 (Profit $\ge +0.80\%$):** Lock SL otomatis ke `Entry + 0.35%` (`lock_0035` — surplus bersih setelah menutup round-trip taker fee `0.18%` + spread).
- **Tier 2 (Profit $\ge +1.40\%$):** Lock SL otomatis ke `Entry + 0.85%` (`lock_0085`).
- **Tier 3 (Profit $\ge +2.00\%$):** Trailing stop dinamis `0.60%` di bawah puncak harga berjalan (tanpa *Pinch TP* atau *Instant Market-TP* agar profit tidak terpotong terlalu dini).
- **3-Hour Re-Entry Cooldown:** Setiap aset yang posisinya baru ditutup otomatis masuk jeda 3 jam (tercatat di `logs/market_regime.json`) agar bot tidak melakukan *churning* membuka ulang aset yang sama berkali-kali.
- **Lapis 3 (Volatility Cut):** *Deprecated* (digantikan sepenuhnya oleh SL baseline 1.5%).
- **Funding Flip Hysteresis (`0.1%/hari`):** Posisi hanya ditutup karena funding berbalik arah jika `abs(funding_daily) >= 0.1%/hari`. Jika berbalik arah tetapi `< 0.1%/hari`, SL digeser ke *breakeven* (`Entry`).
- **Guard Sisi Salah & Migrasi Posisi Lama:** Memvalidasi SL/TP terhadap harga berjalan sebelum mengirim `auto_close` dan otomatis menutup posisi lama yang sudah melewati batas SL 1.5% (`SL 1.5% breached (migrasi)`).
- **Audit Penutupan & Statistik (`Closed_Positions` & `PnL_Tracker`):** Seluruh penutupan posisi (baik oleh monitor maupun TP/SL bursa) tercatat ke sheet `Closed_Positions` (`logs/closed_history.csv`), progres harian ke `PnL_Tracker`, dan dapat dianalisis via `python walt_bot.py stats`.

### 4. Contract Minimum Order Size Enforcement (`min_size_base`)
Mengatasi limit bursa Walt pada aset berharga tinggi (seperti `NVDA` atau `MSTR`):
- Menghitung syarat bursa: $\text{Derived Size} = (\text{margin} \times \text{leverage} / \text{price}) \ge \text{min\_size\_base}$.
- Jika order `HALF` menghasilkan margin di bawah batas kontrak, bot otomatis meng-clamp margin ke batas minimum (contoh: dari $1.25 ke $1.79 untuk NVDA) selama masih dalam batas toleransi risiko portofolio.

### 5. Tier-1 Crypto Whitelist & Strict Memecoin Blacklist
- **Crypto Tier-1 Whitelist:** Aset dengan kapitalisasi pasar besar dan likuiditas solid (`BTC`, `ETH`, `SOL`, `BNB`, `XRP`, `AVAX`, `SUI`, `LINK`, `NEAR`, `AAVE`) dengan filter volume 24 jam $> \$1.000.000$.
- **Memecoin & Koin Micin Dilarang Keras:** Koin meme volatil (`PEPE`, `DOGE`, `SHIB`, `WIF`, `FARTCOIN`, `POPCAT`, `TRUMP`) di-blacklist total untuk mencegah risiko likuidasi liar dan slippage ekstrim.

### 6. Zero-Maintenance OAuth Token Auto-Sync
- Walt OAuth token kadaluarsa secara berkala dan dirotasi oleh bursa.
- `token_manager.py` secara otomatis mengenkripsi token baru dengan libsodium (`pynacl`) dan meng-update GitHub Secret `WALT_REFRESH_TOKEN` via REST API repository. Bot tidak akan pernah berhenti karena token expired.

### 7. Autonomous Auto-Compounding & Dynamic Slot Elasticity
- **Auto-Reinvest:** Mengumpulkan seluruh pendapatan Take Profit dan yield funding harian yang masuk ke kas dompet secara otomatis untuk diputar kembali ke modal trading (*reinvest*).
- **Dynamic Slot Elasticity:** Kuota slot posisi tidak kaku, melainkan elastis beradaptasi dengan ketersediaan modal kas (5 slot pada modal mikro <$30, hingga 15 slot simultan pada modal berkembang).
- **Conviction-Based Sizing:** Margin dihitung berdasarkan besaran yield funding (hingga $1.35\times$), konsensus Swarm AI (hingga $1.25\times$), dan tier keamanan aset (Safe-Haven bluechip $1.15\times$).
- **Portfolio Risk Ceiling:** Margin tiap posisi dibatasi maksimal 15% dari total ekuitas dengan floor aman $2.50 USDC dan cash buffer 15%.
- **Fleksibel:** Jika pemilik akun melakukan penarikan dana (*withdraw*), ukuran margin dan slot otomatis menyesuaikan mengecil ke batas aman tanpa perlu restart atau konfigurasi manual.

---

## ⏰ Jadwal Eksekusi Sistem

Seluruh 4 workflow trading dipicu **eksklusif oleh Google Cloud Scheduler** (`workflow_dispatch`) untuk menghindari keterlambatan cron GitHub Actions yang bisa mencapai 7–8 jam. Verifikasi status job Cloud Scheduler sebelum/sesudah perubahan:

```bash
gcloud scheduler jobs list --location=asia-southeast2
```

| Komponen / Workflow | Trigger Resmi | Timeout / Guard | Deskripsi |
|---|---|---|---|
| [`daily-scan.yml`](.github/workflows/daily-scan.yml) | **Cloud Scheduler (`workflow_dispatch`)** — 07:17 & 19:17 WIB | `12m` (`JOB_TIMEOUT_MIN=12`, guard `< 6m`) | Master Scan seluruh kontrak, ranking peluang funding, riset Swarm AI mendalam (Qwen+Gemini P1–P7), eksekusi order GO/HALF, simpan database ke Google Sheet & notif Telegram. |
| [`market-execute.yml`](.github/workflows/market-execute.yml) | **Cloud Scheduler (`workflow_dispatch`)** — Tiap 2 jam (menit 43) | `12m` (`JOB_TIMEOUT_MIN=12`, guard `< 6m`) | Eksekusi adaptif Saham US & Komoditas 24/7: **1 jam (Gear 2)** saat peluang funding melimpah ($\ge 3$ aset $\ge 0.5\%$/hari atau $\ge 2$ ekstrem $\ge 0.8\%$/hari), **2 jam (Gear 1)** saat normal/sepi, dan *fast-exit* saat slot penuh (Gear 0). Dilengkapi *3-hour re-entry cooldown*. |
| [`crypto-execute.yml`](.github/workflows/crypto-execute.yml) | **Cloud Scheduler (`workflow_dispatch`)** — Tiap 2 jam (menit 11) | `12m` (`JOB_TIMEOUT_MIN=12`, guard `< 6m`) | Eksekusi adaptif 10 Crypto Bluechip (`BTC`, `ETH`, `SOL`, dll) dengan sistem **Calmed Adaptive (1h / 2h)** dan *3-hour re-entry cooldown*. |
| [`risk-monitor.yml`](.github/workflows/risk-monitor.yml) | **Cloud Scheduler (`workflow_dispatch`)** — Tiap 10 menit | `20m` (`JOB_TIMEOUT_MIN=20`, guard sisa `< 6m`) | Pengawas risiko adaptif (5–10 menit + *Smart Fast-Exit Idle*). Guard waktu otomatis melewati *sub-cycle* & *adaptive execute* jika sisa waktu job `< 6 menit` agar order tidak terpotong sebelum TP/SL terpasang. |
| [`watchdog.yml`](.github/workflows/watchdog.yml) | **GitHub Cron (`0 * * * *`)** + `workflow_dispatch` | `3m` (`walt-watchdog`) | Pengaman pengganti cron (Ronde 6 `[R6-3]`): memeriksa usia run `success` **keempat** workflow trading (`risk-monitor` 30 m, `market-execute`/`crypto-execute` 300 m, `daily-scan` 840 m; ambang diturunkan dari tabel jadwal §7.1 STRATEGY.md), mencetak tabel ke Step Summary, mengirim satu alert teragregasi yang menyebut workflow stale, lalu fallback `workflow_dispatch` — otomatis hanya untuk `risk-monitor`; untuk workflow entry perlu `vars.WALT_WATCHDOG_DISPATCH_ENTRY=true`. Tanpa akses Walt API. |
| [`tests.yml`](.github/workflows/tests.yml) | **Push / PR ke `main`** | `5m` (`walt-tests`) | CI otomatis menjalankan import check modul inti, `pytest -q`, dan validasi sintaks seluruh file `.github/workflows/*.yml` tanpa secrets. |
| [`diagnostics.yml`](.github/workflows/diagnostics.yml) | **Manual (`workflow_dispatch`, input `oauth_step`: `dump_closed` \| `reconcile_history`)** | `5m` (`walt-diagnostics`) | Diagnostik *read-only* berurutan: probe sheet → `stats` (keduanya **tanpa** OAuth) → **tepat satu** langkah bursa (refresh token single-use hanya cukup untuk satu langkah per *job*). Sengaja **TIDAK** dibungkam oleh `MAINTENANCE_MODE` agar bisa dipakai saat jendela pemeliharaan. |

### Trade-Off Concurrency & Anti-Kelaparan Risk Monitor
Keempat workflow trading (`daily-scan.yml`, `market-execute.yml`, `crypto-execute.yml`, dan `risk-monitor.yml`) sengaja berbagi satu concurrency group `walt-trading-account` dengan `cancel-in-progress: false` untuk mencegah *race condition* pembukaan order ganda dan konflik state akun:
- **Trade-off sadar:** eksekusi workflow diantri, bukan paralel; konsekuensinya `risk-monitor` dapat tertunda maksimum **12 menit** saat mengantre di belakang `daily-scan`, `market-execute`, atau `crypto-execute` (`timeout-minutes: 12`, `JOB_TIMEOUT_MIN=12`, masih memiliki ruang aman di atas `JOB_TIME_GUARD_MIN=6` menit).
- **Anti-antrean ganda:** `watchdog.yml` memeriksa keberadaan run `risk-monitor.yml` berstatus `queued`/`in_progress` sebelum memicu *fallback dispatch* agar tidak memperpanjang antrean.

### Jendela Pemeliharaan & Pemulihan Kredensial (`MAINTENANCE_MODE`)
GitHub Actions me-rotate `WALT_REFRESH_TOKEN` secara otomatis (± setiap jam) dan menulis ulang GitHub Secret. Karena secret tidak bisa dibaca kembali ke mesin lokal, menjalankan bot secara lokal berbarengan dengan CI dapat memicu tabrakan rotasi (`invalid_grant`).

> **Penting:** Kill-switch `MAINTENANCE_MODE` berfungsi untuk **menghindari tabrakan dua penulis** (CI vs lokal) atas satu refresh token, **bukan** untuk memperbaiki token yang sudah kedaluwarsa.

Langkah pemeliharaan aman:
1. Aktifkan kill-switch (menghentikan job API `daily-scan`, `market-execute`, `crypto-execute`, `risk-monitor`, serta membungkam alarm palsu `watchdog.yml`):
   ```bash
   gh variable set MAINTENANCE_MODE --body "true"
   ```
2. Tunggu seluruh run yang sedang berjalan selesai secara alami (jangan membatalkan run yang mungkin sedang memasang order/TP/SL).
3. Jalankan operasi lokal sekali (atau jalankan `diagnostics.yml` dari GitHub Actions yang sengaja tetap aktif selama maintenance):
   ```bash
   python walt_bot.py dump_closed
   ```
   Satu refresh sukses di lokal akan me-rotate token dan otomatis menulis ulang GitHub Secret bila token GitHub tersedia.
4. Matikan kill-switch setelah selesai:
   ```bash
   gh variable set MAINTENANCE_MODE --body "false"
   # atau: gh variable delete MAINTENANCE_MODE
   ```
5. Bila `GH_PAT`/alias tidak tersedia di lingkungan lokal, perbarui secret secara manual sebelum menyalakan ulang bot:
   ```bash
   gh secret set WALT_REFRESH_TOKEN
   ```
6. **Ras antar-konsumen OAuth (TERJADI 2026-11-20 16:00Z, didokumentasikan supaya tidak disalahartikan).**
   Refresh token bersifat single-use dan `concurrency:` GitHub hanya berlaku **dalam satu workflow** —
   `risk-monitor` (tiap 10 menit) dengan `market-execute`/`crypto-execute`/dispatch manual bisa start
   berdekatan. Yang start belakangan membaca secret yang baru saja dibakar konsumen lain → `invalid_grant`.
   Cirinya: **run yang gagal sendirian, dan ada run OAuth hijau dalam ~15 menit terakhir.** Itu bukan
   kematian kredensial — siklus berikutnya pulih sendiri, **jangan** melakukan re-auth (re-auth justru
   membuat `.env` lokal tertinggal satu rotasi). Yang boleh dilakukan: hindari dispatch manual pada
   menit kelipatan 10 (:00, :10, :20, ...), dan baca alert Telegram — ia kini menyebut dua kemungkinan
   beserta perintah pembedanya (`gh run list --limit 6`).
7. **Lepas bertahap, jangan sekaligus** (praktik yang terbukti perlu pada 2026-11-20 — sistem yang
   "sudah dibunyikan" langsung membuka jalur order nyata tanpa satu pun siklus pengamatan): set
   `DRY_RUN=true` **sebelum** `MAINTENANCE_MODE=false`, sehingga guard risiko bekerja nyata tetapi
   entry masih disimulasikan. Ketiga workflow entry (`daily-scan`, `market-execute`, `crypto-execute`)
   menghormati variable ini; input `dry_run` saat dispatch manual tetap menang atas variable.
   ```bash
   gh variable set DRY_RUN --body "true"          # tahap pantau-dulu (entry disimulasikan)
   gh variable set MAINTENANCE_MODE --body "false"
   # setelah >=3 siklus risk-monitor hijau dan Step Summary bersih dari kegagalan input:
   gh variable delete DRY_RUN                     # baru di sini order nyata bisa keluar
   ```
   Gerbang yang dipakai untuk memutuskan naik ke trading nyata: siklus `risk-monitor` hijau beruntun,
   `state_warm=true` dari `state_probe`, dan **tidak ada** agregat kegagalan input di Step Summary
   (voter LLM / kalender earnings / webhook riset). Selama masih ada input yang mati, hasil apa pun
   bukan bukti performansi strategi.


---

## 🔒 Kebijakan Privasi Log (Repo Publik) & Alur Konfigurasi Strategi

### Kebijakan Log & State di Repo Publik
- **`LOG_REDACT=true` & Global Secret Filter:** Seluruh angka saldo, ekuitas, margin, PnL, `order_id`, dan email disamarkan (`***`) pada *stdout* log publik, sementara pola mirip API key (`key=`, `Bearer`, `AIza...`, `sk-...`, token Telegram) selalu disensor di log global. Gemini API key dikirim lewat header `x-goog-api-key` (bukan query `?key=`). Notifikasi **Telegram** dan **Google Sheets** tetap menerima angka lengkap.
- **Tanpa Artifact Publik & Ringkasan Aman:** Langkah *Upload Logs Artifact* dihapus; *Step Summary* hanya menampilkan status job, jumlah aksi/eksekusi, *cadence mode*, dan *gear*.
- **Integritas Snapshot & Dedupe Close (`D1`–`D4`):**
  - `logs/open_snapshot.json` diisolasi ke cache terpisah (`walt-snapshot-*`) yang **hanya** disimpan oleh `risk-monitor.yml` (workflow lain menghapus file ini sebelum menyimpan cache `walt-logs-state-*`).
  - Dedupe penutupan posisi memakai **Position ID** unik (`logs/closed_ids.json`, maks 500 ID + pengecekan kolom `Position ID` di `Closed_Positions`). Jika data close dari exchange tidak tersedia, dicatat `UNKNOWN (data close tidak tersedia)` dengan exit/PnL kosong (tidak lagi dianggap TP) dan dikecualikan dari win-rate di `python walt_bot.py stats` (dilengkapi pembanding perubahan ekuitas dari `PnL_Tracker`, *median hold time*, serta hash `Strategy Version`).
  - Gunakan `python walt_bot.py dump_closed` (atau workflow [`diagnostics.yml`](.github/workflows/diagnostics.yml)) untuk inspeksi nama *key* & tipe data posisi closed dari MCP tanpa mencetak nilai sensitif.
  - **Skema Nyata 17 Kunci `perps_list_positions(statuses=["closed"])` (terverifikasi CI `diagnostics.yml #44565856937`):** `auto_close` (`NoneType`), `avg_entry_price` (`str`), `contract_base_asset` (`str`), `contract_id` (`int`), `contract_name` (`str`), `contract_quote_asset` (`str`), `created_at` (`str`), `funding_paid_quote` (`str`), `leverage` (`int`), `liquidation_price` (`str`), `margin_quote` (`str`), `margin_type` (`str`), `side` (`str`), `status` (`str`), `synced_at` (`str`), `updated_at` (`str`), `value_base` (`str`). Endpoint list ini **tidak** mengembalikan field harga keluar maupun `realized_pnl_quote`.
  - **Asal Data Statistik & Reconcile PnL (Ronde 5):**
    - Angka `python walt_bot.py stats` lokal sebelum Ronde 5 (`2 trade / Win Rate 50% / Net +$0.0370`) berasal dari **artefak fixture test** (`tests/test_risk.py` pra-isolasi), bukan rekam jejak performa live.
    - Sampai Ronde 4, penutupan otomatis yang dieksekusi bursa tercatat `UNKNOWN` tanpa PnL karena endpoint list tidak menyertakan harga keluar. Mulai Ronde 5, `_detect_and_log_exchange_closes` memakai proksi mark live saat deteksi (`latest_rate`, batas kesegaran `updated_at <= 6 jam`, `Exit Price Source = mark_at_detection`) dengan PnL linear berbasis `value_base`; bila *stale* atau `latest_rate` gagal, exit/PnL tetap kosong (`UNKNOWN`).
  - Untuk penutupan berbasis guard (`_execute_validated_close`), kolom `Exit Price ($)` memprioritaskan harga fill/posisi dari respons `execute` dan masih jatuh kembali ke estimasi snapshot bursa (`pos.current_price`) apabila API tidak menyediakan harga fill. Mark price (`mark_price_proxy`) dipisahkan secara eksplisit karena mark price bukan harga fill riil.
- **Header Google Sheets & Deployment Apps Script (`D3`):**
  - Header tiap sheet (`Daily_Scans`, `Executions_Log`, `Closed_Positions`, `PnL_Tracker`) dikelola tunggal oleh `google_apps_script.js` `doPost` melalui field `header` terpisah (tidak lagi dikirim di dalam `rows`).
  - Tersedia fungsi manual `cleanupClosedPositions()` di editor Apps Script untuk membersihkan baris header ganda dan duplikat lama.
  - **Penting:** Setiap kali `google_apps_script.js` diperbarui, pemilik **wajib membuat Deployment baru** (*Deploy → New deployment*) di editor Apps Script, lalu memperbarui secret `GOOGLE_SHEET_WEBHOOK_URL` jika URL Web App berubah.
- **Tanpa Hardcode Sheet ID (Ronde 6, `[R6-1]`):** `GOOGLE_SHEET_ID` **wajib** dari GitHub Secrets/Variables (`vars.GOOGLE_SHEET_ID || secrets.GOOGLE_SHEET_ID`) atau env lokal. Sebelumnya `sheet_logger.py` memuat ID sebagai *default* di dalam kode, sehingga klaim ini tidak berlaku; kini default itu **dihapus**. Bila ID tidak diset, pembacaan sheet berstatus `no_config` dengan **nol percobaan HTTP** dan ledger jatuh ke `logs/closed_history.csv`.
- **Konfigurasi CLI lokal (`walt_bot.py` + `tools/sheet_probe.py`):** keduanya memuat `.env` **hanya saat dijalankan sebagai perintah** (blok `__main__`), tidak pernah saat modul diimpor — supaya proses test tidak pernah menyerap kredensial nyata maupun mengirim HTTP sungguhan. Nilai yang sudah ada di environment tidak pernah ditimpa (workflow CI menyuntik secrets/variables), dan yang dicetak hanyalah nama kunci. Karena itu `GOOGLE_SHEET_ID` kini tersedia di tiga tempat yang konsisten: `.env` lokal, secret GitHub Actions (dikoreksi 2026-11-20 — sebelumnya menunjuk sheet yang tidak ada), dan tidak pernah direpo.
- **Kejujuran sumber ledger (Ronde 6, `[R6-1]`):** kegagalan baca `Closed_Positions`/`PnL_Tracker` tidak lagi senyap (`log.debug`) — sekarang satu `log.warning` teragregasi per (sheet, status) per 15 menit, dan `python walt_bot.py stats` mencetak `LEDGER TIDAK TERTBACA (<status>) — angka 0 di bawah BUKAN berarti tidak ada trade`. Payload `stats` dan `last_safe_summary.json` memuat `ledger_source` (`sheet_status`, `rows_from`, `unreadable`) serta `pnl_terukur` vs `belum_terukur_pnl`.
- **Sumber kebenaran riwayat penutupan = API bursa (Ronde 6, `[R6-1]`):** `python walt_bot.py reconcile_history` (default dry-run, read-only) membandingkan `list_positions(statuses=["closed"])` dengan ledger lokal dan mencetak hasil pengukuran (jumlah, rentang `created_at`, field tidak valid, **verdict kelengkapan**). `--write` menyimpan baris backfill ke `logs/closed_history.csv` + `logs/closed_ids.json` (backup bertimestamp lebih dulu, **tanpa** menulis ke Google Sheet). Baris backfill sengaja **tanpa** `Exit Price ($)`/`PnL ($)` dengan `Exit Price Source = exchange_history_no_exit` karena endpoint list tidak menyediakan harga keluar — PnL historis hasil backfill berstatus *belum terukur*, bukan nol.
- **Cache Actions = memori risiko (Ronde 6, `[R6-2]` + koreksi `[R6-fix]`):** keempat workflow trading memeriksa **keberadaan berkas state** (`logs/market_regime.json`, dan untuk `risk-monitor` juga `logs/open_snapshot.json`) lewat langkah `state_probe`, lalu meneruskan hasilnya sebagai `WALT_STATE_WARM`. Catatan penting hasil run nyata: output `cache-hit` pada `actions/cache/restore` hanya `true` untuk **exact key**, sedangkan key kita memuat `github.run_id` sehingga praktis selalu `false` — memakai `cache-hit` sebagai sinyal akan membuat sistem selamanya menganggap dirinya dingin dan menolak semua entry. Saat state benar-benar dingin, sistem konservatif: siklus deteksi close **dilewati** (nol baris `UNKNOWN`; selisih riwayat bursa dilaporkan agar bisa di-*backfill* sadar), dan jalur entry **menolak membuka posisi** karena batas 8 entry/hari, 2 entry/aset/24 jam, cooldown, dan jangkar *daily circuit breaker* tidak diketahui. Override darurat: `WALT_ALLOW_COLD_ENTRY=true` (default tidak set; risiko: limit harian tereset senyap). Marker `state_warm` tampil di Step Summary tiap workflow.
- **Watchdog penuh (Ronde 6, `[R6-3]`):** `watchdog.yml` memeriksa usia run `success` **keempat** workflow dengan ambang per-cadensi yang diturunkan dari tabel jadwal (`risk-monitor` 30 m; `market-execute`/`crypto-execute` 300 m; `daily-scan` 840 m) — logikanya pindah ke [`tools/watchdog_check.py`](tools/watchdog_check.py) agar teruji. Hasil selalu menjadi tabel di Step Summary; bila ada yang *stale*, satu alert Telegram teragregasi menyebut nama workflow-nya. Fallback `workflow_dispatch` otomatis **hanya** untuk `risk-monitor` (perilaku warisan); untuk workflow entry defaultnya *alert-only* karena dispatch akan membuka order nyata — aktifkan dengan variable `WALT_WATCHDOG_DISPATCH_ENTRY=true`. `MAINTENANCE_MODE=true` tetap keluar sebelum alert maupun dispatch.
- **CI least-privilege & build reproducible (Ronde 6, `[R6-4]` + koreksi `[R6-fix]`):** ketujuh workflow mendeklarasikan `permissions:` eksplisit, seluruh `uses:` di-pin ke SHA action (dengan komentar versi), dan `requirements*.txt` dikunci `==` pada versi yang terpasang (Python 3.14.7). Variabel `WALT_TOKEN_SYNC` mengontrol persistensi kredensial: **`off` kini menolak melakukan refresh sama sekali** (bukan "refresh lalu buang hasilnya"). Pergeseran ini dipaksa oleh run nyata — versi awal Ronde 6 menyetel `off` pada `diagnostics.yml`, refresh OAuth tetap membakar refresh token single-use, token baru tidak disimpan, dan `risk-monitor` berikutnya gagal `invalid_grant`: kredensial CI mati. Karena itu `diagnostics.yml` kini `on` (tulis-balik secret); konsekuensinya yang sebenarnya: **setiap kali `diagnostics.yml` dijalankan, `.env` lokal akan tertinggal satu rotasi** dan perlu penyegaran (lihat *Jendela Pemeliharaan & Pemulihan Kredensial*). Nilai `on` juga berarti menjalankan operasi bursa apa pun di CI selalu menyimpan rotasinya.
- **Kebersihan state & notifikasi (Ronde 6, `[R6-5]`):** guard test kini memantau 13 berkas state (termasuk `logs/security_audit.jsonl` dan `logs/_tmp_sheet_batch.json`, dan path-nya bisa diarahkan fixture isolasi); `notifier` mencetak lewat `_safe_print` sehingga konsol cp1252 tidak lagi mematikan pemanggil; dan `python tools/migrate_closed_ledger.py purge-fixture-ids [--apply]` membuang kunci dedupe ber-pola fixture dari `logs/closed_ids.json` (dry-run default, backup `.bak-<UTC>`, hanya pola terdaftar `1:100.0[:]` dan `ASSET|1|100.0`).

- **Kualitas data ledger & sensus sheet (pasca-Ronde 6):** setelah pemilik memberi akses *Viewer* pada spreadsheet, gviz export akhirnya **terbaca** (sebelumnya HTTP 401 untuk keempat tab). Sensus nyata `Closed_Positions`: 17 baris data — **2 terukur** (ada harga keluar & PnL), **9 diragukan**, **6 UNKNOWN**, 0 backfill; `Executions_Log` 215 baris; `Daily_Scans` 39 baris; `PnL_Tracker` 4 baris. `stats` kini memisahkan tiga kategori (`pnl_terukur`, `pnl_diragukan` + `diragukan_alasan`, `belum_terukur_pnl`) dan **tidak lagi menghitung baris ber-PnL 0.00 dengan `Exit Price == Entry Price` tanpa sumber harga sebagai LOSS** — itu warisan penulis pra-Ronde 5 yang memakai harga masuk sebagai fallback harga keluar. Konsekuensi yang harus dibaca jujur: *win-rate 2 trade bukan bukti strategi apa pun*, dan estimasi fee/net PnL kini hanya menutupi trade terukur sehingga tidak bisa langsung dibandingkan dengan perubahan ekuitas.
- **Probe sheet tanpa kredensial (`python tools/sheet_probe.py`):** baca gviz untuk keempat tab dan mencetak sidik jari sheet (`sha8`, panjang ID — ID penuh tidak pernah ditampilkan), jumlah baris, bentuk header (kolom total vs kolom bernama), census kualitas `Closed_Positions`, dan stempel waktu eksekusi terakhir. Karena jalur ini tidak menyentuh OAuth Walt, `diagnostics.yml` menjalankannya **sebelum** langkah `dump_closed` — jadi fakta baca-sheet tetap teramati bahkan ketika refresh token mati.

### Gerbang Kualitas Entry & Alur Sinkronisasi Strategi (Telegram ↔ GitHub Actions)

- **Filter Kualitas Entry (`E1`–`E5`):** `HALF` di bawah floor `$2.50` di-**SKIP**, `P5_risk_catalysts >= max_p5_risk` (`0.70`) di-**SKIP**, riset lebih tua dari `max_research_age_hours` (`6` jam) diabaikan, batas frekuensi **2 entry/aset/24 jam** & **8 entry/24 jam**, **circuit breaker harian** (`-5%` ekuitas dari awal hari UTC), serta gerbang ekonomi `expected_funding >= (2 × 0.0009 + slippage) × 1.5` (`expected_hold_hours = 24`).
- Perubahan strategi lewat Bot Telegram (`/set_strategy` / `/reset_strategy`) divalidasi terhadap batas aman `STRATEGY_PARAM_SPECS`, disimpan lokal di `logs/strategy_config.json`, dan langsung disinkronkan via `GH_PAT` ke **GitHub Repository Variable `STRATEGY_CONFIG_JSON`**.
- Setiap workflow GitHub Actions meneruskan `STRATEGY_CONFIG_JSON: ${{ vars.STRATEGY_CONFIG_JSON }}` ke `walt_bot.py`. Jika parsing gagal atau ada nilai di luar batas aman, bot mencatat *warning* dan *fallback* otomatis ke `DEFAULT_STRATEGY_CONFIG`.
- Profil pribadi End User (`logs/user_strategies.json`) dan data auth (`logs/telegram_auth.json`) **tidak pernah** disinkronkan ke variable atau sheet publik.

---

## 📂 Struktur Berkas

```
walt-trading/
├── .github/workflows/
│   ├── daily-scan.yml          # Workflow pemindaian & riset harian
│   ├── market-execute.yml      # Workflow eksekusi saham US & komoditas
│   ├── crypto-execute.yml      # Workflow eksekusi mandiri crypto bluechip
│   └── risk-monitor.yml        # Workflow pemantau risiko 4-lapis
├── STRATEGY.md                 # Dokumentasi strategi finansial & kuantitatif end-to-end
├── PRD.md                      # Product Requirements Document (Fase 1 Owner & Fase 2 SaaS)
├── walt_bot.py                 # Core bot orchestrator (ranking, execution, 4-layer monitor)
├── token_manager.py            # OAuth PKCE auto-refresh & auto-sync GitHub Secrets
├── llm_research.py             # Dual-AI Swarm Client (Qwen + Gemini) & 7-parameter voter
├── notifier.py                 # Telegram notifier dengan 3x retry exponential backoff
├── sheet_logger.py             # Logger eksekusi & scan ke Google Sheets via Webhook
├── google_apps_script.js       # Script Webhook endpoint untuk Google Sheets
├── setup_walt_oauth.py         # Skrip interaktif otentikasi awal Walt OAuth
└── requirements.txt            # Dependensi Python
```

---

## 🚀 Panduan Setup Singkat

### 1. Registrasi Walt OAuth (Sekali Saja)
```bash
pip install -r requirements.txt
python setup_walt_oauth.py
```
Ikuti instruksi di terminal dan browser untuk mendapatkan `WALT_CLIENT_ID` dan `WALT_REFRESH_TOKEN`.

### 2. Konfigurasi GitHub Repository Secrets
Di GitHub: **Settings -> Secrets and variables -> Actions -> New repository secret**:

| Secret Key | Keterangan |
|---|---|
| `WALT_CLIENT_ID` | OAuth Client ID dari Walt |
| `WALT_REFRESH_TOKEN` | OAuth Refresh Token awal dari Walt |
| `DASHSCOPE_API_KEY` | Alibaba Cloud DashScope API Key (untuk Qwen) |
| `DASHSCOPE_API_GEMINI_KEY` | Google AI Studio Gemini API Key (atau `GEMINI_API_KEY`) |
| `TELEGRAM_BOT_TOKEN` | Token Bot Telegram dari `@BotFather` |
| `TELEGRAM_CHAT_ID` | ID Chat Telegram Anda dari `@userinfobot` |
| `GOOGLE_SHEET_WEBHOOK_URL`| URL Webhook Google Apps Script (lihat `google_apps_script.js`) |
| `GH_PAT` | GitHub Personal Access Token (`repo` scope) untuk auto-update secret |

- **Bila `stats`/`reconcile_history` gagal dengan `invalid_grant` dan run CI juga gagal** — berarti
  refresh token di secret DAN `.env` sudah **sama-sama terbakar** (rotasi single-use). Kill-switch
  tidak memperbaiki token; yang diperlukan adalah otentikasi ulang:
  1. `gh variable set MAINTENANCE_MODE --body "true"` lalu tunggu run yang sedang berjalan selesai.
  2. `python setup_walt_oauth.py` — skrip ini **mendaftarkan klien OAuth baru** (DCR RFC 7591), membuka
     browser, dan mendengarkan `http://localhost:8765/callback` sekali. Karena klien baru ikut dibuat,
     **dua secret** harus diperbarui: `WALT_CLIENT_ID` **dan** `WALT_REFRESH_TOKEN` (nilai yang dicetak skrip).
  3. Tulis ke GitHub Actions, lalu matikan kill-switch dan verifikasi:
     ```bash
     gh secret set WALT_CLIENT_ID
     gh secret set WALT_REFRESH_TOKEN
     gh variable set MAINTENANCE_MODE --body "false"
     gh workflow run risk-monitor.yml && gh run watch   # wajib success
     ```
  4. Setelah kredensial hidup, jalankan satu `python walt_bot.py reconcile_history` (dry-run) untuk
     memverifikasi kelengkapan riwayat, baru operasinya yang mengubah strategi.
- **Catatan arsitektur pemicu:** proyek GCP ini **tidak menjalankan VM/Compute** — yang aktif hanya
  **Cloud Scheduler** (region `asia-southeast2`) yang menembak REST `workflow_dispatch` GitHub.
  Tidak ada proses lokal yang harus hidup. Cara memeriksa keberadaan & jadwal job-nya (butuh `gcloud`
  ter-autentikasi, tidak butuh VM):
  ```bash
  gcloud scheduler jobs list --location=asia-southeast2
  gcloud scheduler jobs describe walt-risk-monitor --location=asia-southeast2   # cek state + jadwal + terakhir sukses
  ```
  Karena sisi Scheduler tidak bisa diamati dari GitHub, ronde 6 menutup risikonya dari sisi gejala:
  `watchdog.yml` membandingkan usia run `success` tiap workflow terhadap ambang berbasis cadensi
  (lihat §"Watchdog penuh") dan akan membunyikan alert walau Scheduler berhenti menembak sama sekali.
- **Membaca Google Sheet lewat gviz butuh sheet bisa diakses publik (link: Viewer).** Jalur ini
  **TERBUKTI BERFUNGSI** sejak 2026-11-20 13:53:42Z: `diagnostics.yml` membaca CSV gviz untuk keempat
  tab dan `stats` melaporkan `Sumber ledger: ok` (sidik jari `sha8=06bded7f`, panjang 44 — ID tidak
  pernah dicetak). Penyebab kegagalan sebelumnya **bukan** level berbagi, melainkan secret/variable CI
  yang menunjuk ID sheet lain (`sha8=c8899991`, panjang 45) sehingga menjawab 401/404. Bila kelak
  muncul lagi `401` dengan halaman login, urutannya: (1) cocokkan sidik jari `sha8` variable
  `GOOGLE_SHEET_ID` dengan sheet yang Anda buka, (2) baru periksa level berbagi. Alternatif bila sheet
  ingin tetap privat: tambahkan endpoint **baca** (`doGet`) di Apps Script dan panggil lewat
  `GOOGLE_SHEET_WEBHOOK_URL` — script berjalan sebagai pemilik. Apapun itu, sumber kebenaran riwayat
  tetap **API bursa** (`reconcile_history`), dan `stats` menandai ledger yang tidak terbaca secara
  eksplisit — angka performansi tidak pernah dihitung dari asumsi.

### 2b. Konfigurasi GitHub Repository Variables

Beda dengan secret, **variable** terlihat nilainya di UI — dipakai untuk *knop* operasional sehingga
status sistem bisa dibaca tanpa menebak. Di GitHub: **Settings → Secrets and variables → Actions →
→ Variables → New repository variable** (atau `gh variable set`).

| Variable | Default (bila tidak diset) | Fungsi |
|---|---|---|
| `MAINTENANCE_MODE` | `false` | Kill-switch: membungkam workflow entry + risiko saat pemulihan kredensial. |
| `DRY_RUN` | `false` | `true` = pantau nyata, entry disimulasikan (`verdict: DRY_RUN`). Dipakai untuk pelepasan bertahap. |
| `WALT_COLD_REHYDRATE` | `report` | Start dingin (cache Actions ter-eviksi): `report` = ukur & bunyikan jarak ledger-vs-bursa; `write` = tutup jarak lewat jalur backfill (backup dulu, PnL tetap *belum terukur*, dan diblokir selama `DRY_RUN=true`). |
| `DASHSCOPE_MODEL_V1` | `qwen-flash` | Model Voter 1. `qwen-turbo` sudah dipensiunkan upstream (404) — jangan dipakai. |
| `GEMINI_MODEL_V4` | `gemini-pro-latest` | Model Voter 4. |
| `DASHSCOPE_MODEL_AGG` | `qwen-max` | Model meta-aggregator P1–P7. |
| `DASHSCOPE_MODEL` | `qwen-plus` | Model Voter 2. |
| `GEMINI_MODEL` | `gemini-flash-latest` | Model Voter 3. |
| `WALT_TOKEN_SYNC` | `on` | `off` **menolak refresh** (bukan "refresh lalu buang hasilnya") — wajib `on` untuk workflow yang butuh OAuth. |
| `WALT_WATCHDOG_DISPATCH_ENTRY` | `false` | `true` mengizinkan watchdog men-dispatch ulang workflow entry; default *alert-only* karena dispatch membuka order nyata. |
| `GOOGLE_SHEET_ID` | — | ID spreadsheet ledger (yang benar bersidik jari `06bded7f`). |
| `STRATEGY_CONFIG_JSON` | berkas `strategy_config.json` | Override parameter strategi dari CI tanpa commit. |


### 3. Hubungkan Google Cloud Scheduler Bridge (Opsional tapi Direkomendasikan)

Gunakan project GCP gratis Anda untuk menjadwalkan trigger presisi:
```bash
gcloud scheduler jobs create http walt-market-execute \
  --location=asia-southeast2 \
  --schedule="43 21 * * 1-5" \
  --time-zone="Asia/Jakarta" \
  --uri="https://api.github.com/repos/<OWNER>/<REPO>/actions/workflows/market-execute.yml/dispatches" \
  --http-method=POST \
  --headers="Accept=application/vnd.github.v3+json,Content-Type=application/json,Authorization=Bearer <GH_PAT>" \
  --message-body='{"ref":"main"}'
```

---

## 💰 Biaya Infrastruktur: 100% Zero-Cost

| Layanan Cloud | Batas Kuota Gratis Bulanan | Penggunaan Bot Kami | Biaya |
|---|---|---|---|
| **GitHub Actions** | 2.000 menit runner / bulan | ~120 menit / bulan | **$0.00** |
| **Google Cloud Scheduler** | 3 job gratis per akun billing | Tepat 3 job | **$0.00** |
| **Google Sheets** | Tidak terbatas (Personal Drive) | Logging per run | **$0.00** |
| **Telegram Bot API** | Tidak terbatas | Notifikasi berkala | **$0.00** |
| **Total Biaya Bulanan** | — | — | **$0.00 (Rp 0)** |

---

## 📄 Lisensi & Disclaimer

Proyek ini dirilis di bawah lisensi MIT.

*Disclaimer: Perdagangan perpetual futures memiliki risiko kerugian modal. Bot ini dirancang dengan manajemen leverage ketat (max 2x) dan stop-loss berlapis untuk tujuan capture funding rate, bukan spekulasi arah harga.*
