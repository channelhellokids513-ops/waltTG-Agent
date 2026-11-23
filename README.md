# ⚡ Walt Perps Autonomous Funding Arbitrage Bot

[![GitHub Actions Status](https://github.com/channelhellokids513-ops/waltTG-Agent/actions/workflows/market-execute.yml/badge.svg)](https://github.com/channelhellokids513-ops/waltTG-Agent/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/Python-3.14-green.svg)](https://python.org)
[![Zero-Cost Infrastructure](https://img.shields.io/badge/Infra%20Cost-%240%20(Free%20Tier)-brightgreen.svg)](#-biaya-infrastruktur-100-zero-cost)

Autonomous delta-neutral & directional funding rate arbitrage trading system untuk platform **Walt Perps** (`walletbot.me`). Ditenagai oleh **4-Voter Multi-Vendor AI Research Swarm (GLM-5.3 + MiniMax M3 + Claude Haiku 5.5)**, **OAK Research MCP Intelligence**, **Google Cloud Scheduler Precision Bridge**, sistem **Risk Monitor 4-Lapis**, serta pelaporan *real-time* ke **Telegram** dan **Google Sheets** (termasuk pengumpulan dataset SFT ke tab `Data Training`).

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
│    • walt-market-execute (Tiap 2 Jam di menit 43 / Adaptive Gear 0-3)  │
│    • walt-daily-scan     (07:17 & 19:17 WIB Daily)                     │
│    • walt-risk-monitor   (Tiap 10 Menit)                               │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ (Exact-second HTTP POST trigger)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   GITHUB ACTIONS CLOUD RUNNERS                         │
│   ├─ daily-scan.yml      ──> Scan, OAK MCP + Swarm AI & Auto-Execute   │
│   ├─ market-execute.yml  ──> Saham US (Jam Bursa UTC) & Komoditas 24/7 │
│   ├─ crypto-execute.yml  ──> Crypto Bluechip 24/7 (10 Tier-1 Aset)     │
│   └─ risk-monitor.yml    ──> Evaluasi 4 Lapis Risiko & Trailing Stop   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
               ┌────────────────────┼────────────────────┐
               ▼                    ▼                    ▼
     ┌───────────────────┐┌───────────────────┐┌───────────────────┐
     │ Walt Perps Relay  ││ 4-Voter Swarm AI  ││  Telemetry & SFT  │
     │  (MCP & OAuth)    ││ + OAK Research MCP││ (Telegram Alerts &│
     │ • Bearer Auto-Sync││ • GLM-5.3 CRO Agg ││  5 Tab Sheets +   │
     │ • Min Size Clamp  ││ • P1–P7 Scoring   ││  Data Training)   │
     └───────────────────┘└───────────────────┘└───────────────────┘
```

---

## 💎 Fitur Utama & Guardrails

### 1. 4-Voter Multi-Vendor AI Research Swarm + OAK Research MCP (`llm_research.py` & `oak_mcp_client.py`)
Analisis sentimen berita, katalis makro/earnings, dan risiko posisi dipandu oleh **OAK Benchmark** dan data real-time dari **OAK Research Remote MCP Server** (`https://oakresearch.io/api/mcp`):
- **Pre-Research MCP Context (`oak_mcp_client.py`):** Sebelum voter menilai aset, bot mengambil *global market overview*, *7-day calendar* (macro/earnings/unlocks), berita terkini, serta metrik spesifik aset (`get_crypto` / `get_tradfi`) via OAuth 2.1 PKCE dengan rotasi `OAK_MCP_REFRESH_TOKEN` otomatis.
- **DashScope (`DASHSCOPE_API_KEY`):**
  - **V1 (Fast Validator):** `glm-5.2-fast-preview`
  - **V2 (Alpha Signal — #1 OAK Benchmark):** `glm-5.3-prime`
- **ClassAI Router (`DASHSCOPE_API_GEMINI_KEY`, `https://router.classai.id/v1`):**
  - **V3 (Conservative Risk Guard — #7 OAK Max DD -6.5%, 0 likuidasi):** `minimax-m3`
  - **V4 (Fast Multi-Angle Reasoner — 1.84s, 1M ctx):** `claude-haiku-5.5`
- **CRO Meta-Aggregator:** **`glm-5.3`** (#1 OAK Winner via `DASHSCOPE_API_KEY`, fallback `glm-5.3-prime` -> `glm-5.2-fast-preview` -> `qwen-plus` -> `qwen-flash` -> ClassAI `claude-opus-5.5` -> `gpt-6.1-sol` -> rule-based) mengevaluasi konsensus 7 parameter standar (**P1–P7**):
  - `GO` (Full size $2.50 – $3.00 margin — **bypass batas frekuensi 24 jam `E4`**)
  - `HALF` (Half size hanya bila masih $\ge \$2.50$ margin setelah clamp/buffer; jika di bawah floor $\to$ **SKIP**)
  - `SKIP` (Batalkan jika melawan sentimen / `P5 >= 0.70` / earnings $\le 7$ hari)
- **SFT Data Training Collector (`training_data.py`):** Setiap riset AI tervalidasi otomatis dicatat dalam format chat-messages `<think>...</think>` + JSON ke Google Sheet tab **`Data Training`** (`12` kolom, target 1,000 baris) dan `logs/training_dataset.jsonl`.

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
- **Kebijakan Menahan Posisi Saat Pasar Tutup (`OFF_HOURS_HOLD_POLICY=hybrid`, Opsi B):**
  - *Cakupan:* **Saham AS** (*overnight* Sen–Kam `21:00–13:30 UTC` & *weekend* Jum `21:00`–Sen `13:30 UTC`) dan **Komoditas** (`XAU`, `XAG`, `BRENTOIL` khusus *weekend* Jum `21:00`–Min `22:00 UTC`); **Crypto** tetap 24/7.
  - *Nol Biaya Inap:* Jika *funding rate* berbalik arah menjadi beban biaya inap (`< 0%`, berapa pun nilainya) saat pre-close/pasar tutup, posisi langsung ditutup tanpa menunggu ambang `0.10%/hari`.
  - *Pre-Close Overnight (`20:35–21:00 UTC` Sen–Kam, Saham AS):* Tetap ditahan bila *carry* searah `>= +0.20%/hari` atau sudah profit-locked (menghindari *fee bleed* tutup-buka `0.28%–0.45%`); bila profit `>= +0.40%` (di bawah Tier 1 `+0.80%`), SL otomatis dikunci lebih dini ke `Entry +/- 0.25%`.
  - *Pre-Close Weekend (`20:35–21:00 UTC` Jum, Saham AS & Komoditas):* Hanya menginap melewati libur akhir pekan bila sudah profit-locked (`SL >= Entry + 0.25%`) atau *carry* kuat `>= +0.50%/hari` dengan floating PnL `>= -0.50%`.
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
| [`daily-scan.yml`](.github/workflows/daily-scan.yml) | **Cloud Scheduler (`workflow_dispatch`)** — 07:17 & 19:17 WIB | `12m` (`JOB_TIMEOUT_MIN=12`, guard `< 6m`) | Master Scan **31 aset diizinkan** (`18 Saham US + 3 Komoditas + 10 Crypto`), filter **Jam Bursa Reguler Saham AS (`Sen–Jum 13:30–21:00 UTC`, `US_STOCK_HOURS_ONLY=true`)** untuk `ALLOWED_STOCKS`, pengayaan OAK Research MCP + Swarm AI (`GLM-5.3` CRO), eksekusi GO/HALF, simpan ke Google Sheets (30 kolom `Daily_Scans` + SFT `Data Training`) & notif Telegram. |
| [`market-execute.yml`](.github/workflows/market-execute.yml) | **Cloud Scheduler (`workflow_dispatch`)** — Tiap 2 jam (menit 43) | `12m` (`JOB_TIMEOUT_MIN=12`, guard `< 6m`) | Eksekusi **4-Tier Execution Gear (`0..3`)** Saham US (hanya saat jam bursa reguler AS `13:30–21:00 UTC`) & Komoditas (24/7): **Gear 0 (`0` GO)** = maks 2x selama jam bursa AS (`225m`); **Gear 1 (`< 2` GO)** = `120m` (2 jam, konservatif); **Gear 2 (`2..5` GO)** = `60m` (1 jam, aktif); **Gear 3 (`> 5` GO atau `GEAR_3_FULL_RESEARCH=true`)** = `60m` + izin riset & analisa AI penuh untuk setiap keputusan trading. Dilengkapi *3-hour re-entry cooldown*. |
| [`crypto-execute.yml`](.github/workflows/crypto-execute.yml) | **Cloud Scheduler (`workflow_dispatch`)** — Tiap 2 jam (menit 11) | `12m` (`JOB_TIMEOUT_MIN=12`, guard `< 6m`) | Eksekusi mandiri 24/7 Dual-Engine untuk 10 Crypto Bluechip: Engine 1 Funding Carry + **Engine 2 Mean-Reversion Fade teruji OOS (`CRYPTO_FORECAST_ENGINE=on`, `V5 4h Rejected->Comfort` + `V4 1h Extreme Fade`, ATR SL `<= 4.5%`, Time-Stop `24h/72h`)** dg 4-Tier Gear `0..3` & *3-hour re-entry cooldown*. |
| [`risk-monitor.yml`](.github/workflows/risk-monitor.yml) | **Cloud Scheduler (`workflow_dispatch`)** — Tiap 10 menit | `20m` (`JOB_TIMEOUT_MIN=20`, guard sisa `< 6m`) | Pengawas risiko adaptif (5–10 menit + *Smart Fast-Exit Idle*). Guard waktu otomatis melewati *sub-cycle* & *adaptive execute* jika sisa waktu job `< 6 menit` agar order tidak terpotong sebelum TP/SL terpasang. |
| [`watchdog.yml`](.github/workflows/watchdog.yml) | **GitHub Cron (`0 * * * *`) + keepalive dari workflow entry (Ronde 7b)** + `workflow_dispatch` | `3m` (`walt-watchdog`) | Pengaman pengganti cron (Ronde 6 `[R6-3]`): memeriksa usia run `success` **keempat** workflow trading (`risk-monitor` 30 m, `market-execute`/`crypto-execute` 300 m, `daily-scan` 840 m; ambang diturunkan dari tabel jadwal §7.1 STRATEGY.md), mencetak tabel ke Step Summary, mengirim satu alert teragregasi yang menyebut workflow stale, lalu fallback `workflow_dispatch` — otomatis hanya untuk `risk-monitor`; untuk workflow entry perlu `vars.WALT_WATCHDOG_DISPATCH_ENTRY=true`. Tanpa akses Walt API. **Catatan Ronde 7b:** cron GitHub terbukti bisa menguap (~7 jam pada 2026-11-20, §3i) — karena itu `risk-monitor.yml` dan `market-execute.yml` memanggil `keep_watchdog_alive()` di akhir job (`if: always()`, nol OAuth) dan men-dispatch watchdog bila run terakhir > 50 menit. Cloud Scheduler tidak bisa dipakai dari repo ini (0 job pada semua proyek yang terjangkau akun). |
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
   # mundur satu perintah, kapan saja:
   gh variable set MAINTENANCE_MODE --body "true"
   ```
   Gerbang yang dipakai untuk memutuskan naik ke trading nyata: siklus `risk-monitor` hijau beruntun,
   `state_warm=true` dari `state_probe`, dan **tidak ada** agregat kegagalan input di Step Summary
   (voter LLM / kalender earnings / webhook riset). Selama masih ada input yang mati, hasil apa pun
   bukan bukti performansi strategi.

   **Status 2026-11-20 18:27Z — `DRY_RUN` telah DILEPAS (sistem live).** Ini dilakukan dengan satu
   penyimpangan sadar dari gerbang di atas, dan itu perlu tertulis, bukan dikubur: pada siklus
   17:43Z `input_health` masih memuat `input.riset_webhook: html_or_unparsable`. Yang membuatnya
   aman untuk tetap naik:
   - kegagalan itu ada di **jalur BACA ulang** riset, bukan jalur pembuatannya — siklus yang sama
     memanggil Qwen + Gemini (`200 OK`) dan kalender earnings (`crumb 200`), lalu menolak kandidat
     karena **keputusan strategi** (`P5 >= 0.70`; `E5 req 1.77%/hari`), bukan karena data hilang;
   - jalur baca sudah punya sumber kedua yang tahan-lama (gviz `Daily_Scans`), dan `reconcile_write`
     sudah membuktikan `research_read_source` bisa `gviz`;
   - **0 posisi terbuka** saat dilepas (`Open positions verified: []`), jadi tidak ada eksposur yang
     langsung diurus dunia nyata;
   - `WALT_WATCHDOG_DISPATCH_ENTRY` sengaja TIDAK diset: fallback watchdog untuk workflow entry
     tetap alert-only, karena sejak `DRY_RUN` hilang, men-dispatch entry = meminta order nyata.
   Yang masih harus diputuskan pemilik sebelum menyebut apa pun "performansi": ambang ekonomi E5
   (lihat AUDIT §3k) — saat ini ia menolak SEMUA kandidat, jadi sistem live tetapi praktis tidak
   membuka posisi pada funding 0.5–0.85%/hari.


---

## 🔒 Kebijakan Privasi Log (Repo Publik) & Alur Konfigurasi Strategi

### Kebijakan Log & State di Repo Publik
- **`LOG_REDACT=true` & Global Secret Filter:** Seluruh angka saldo, ekuitas, margin, PnL, `order_id`, dan email disamarkan (`***`) pada *stdout* log publik, sementara pola mirip API key (`key=`, `Bearer`, `AIza...`, `sk-...`, token Telegram) selalu disensor di log global. Kredensial ClassAI Router (`DASHSCOPE_API_GEMINI_KEY`), DashScope (`DASHSCOPE_API_KEY`), maupun OAK MCP OAuth dikirim lewat header `Authorization: Bearer ...` (bukan query parameter). Notifikasi **Telegram** dan **Google Sheets** tetap menerima angka lengkap.
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

- **Filter Kualitas Entry (`E1`–`E5`):** `HALF` di bawah floor `$2.50` di-**SKIP**, `P5_risk_catalysts >= max_p5_risk` (`0.70`) di-**SKIP**, riset lebih tua dari `max_research_age_hours` (`6` jam) diabaikan, batas frekuensi **2 entry/aset/24 jam** & **8 entry/24 jam** berlaku untuk `HALF` (sementara verdict **`'GO'` otomatis melewati batas frekuensi 24 jam tersebut**), **circuit breaker harian** (`-5%` ekuitas dari awal hari UTC), cooldown re-entry 3 jam, batas slot dinamis, buffer kas 15%, serta gerbang ekonomi `expected_funding >= (2 × 0.0009 + slippage) × 1.5` (`expected_hold_hours = 24`).
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
├── AUDIT.md                    # Audit independen & kronologi remedi Ronde 2–9+
├── walt_bot.py                 # Core bot orchestrator (Gear 0–3, Engine 2 Crypto, Off-Hours, 4-layer monitor)
├── price_forecast.py           # Engine 2 Crypto: selisih harga 24j & 3-Pillar Fade (`V5 4h` + `V4 1h`)
├── token_manager.py            # OAuth PKCE auto-refresh & auto-sync GitHub Secrets
├── llm_research.py             # Multi-Vendor 4-Voter Swarm (GLM-5.2/5.3, MiniMax M3, Haiku 5.5) + GLM-5.3 CRO
├── oak_mcp_client.py           # Klien MCP Remote OAK Research (`https://oakresearch.io/api/mcp`) + auto-rotate RT
├── setup_oak_oauth.py          # Skrip interaktif otentikasi OAuth 2.1 PKCE OAK Research MCP
├── training_data.py            # Pengumpul dataset SFT `<think>` JSONL -> tab Google Sheet `Data Training`
├── notifier.py                 # Telegram notifier dengan 3x retry exponential backoff
├── sheet_logger.py             # Logger 5 tab Google Sheets via Webhook v2 & gviz reader
├── google_apps_script.js       # Script Webhook v2 (2026-11-21.1) untuk Google Sheets (5 tab)
├── setup_walt_oauth.py         # Skrip interaktif otentikasi awal Walt OAuth
├── tools/
│   ├── export_training_data.py # Backfill/ekspor dataset SFT `Data Training` (target >= 1.000 baris)
│   └── ...                     # Probe sheet, reconcile, sanitize_gate, watchdog_check
└── requirements.txt            # Dependensi Python (pinned)
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
| `WALT_REFRESH_TOKEN` | OAuth Refresh Token awal dari Walt (rotasi otomatis oleh CI) |
| `DASHSCOPE_API_KEY` | Alibaba Cloud DashScope API Key (Aggregator utama `glm-5.3`, V1 `glm-5.2-fast-preview`, V2 `glm-5.3-prime`, fallback Qwen) |
| `DASHSCOPE_API_GEMINI_KEY` | ClassAI Router API Key (`https://router.classai.id/v1` — V3 `minimax-m3`, V4 `claude-haiku-5.5`, & fallback aggregator `claude-opus-5.5` / `gpt-6.1-sol`) |
| `OAK_MCP_CLIENT_ID` | OAuth 2.1 Client ID untuk OAK Research Remote MCP Server (dari `python setup_oak_oauth.py`) |
| `OAK_MCP_REFRESH_TOKEN` | OAuth 2.1 Refresh Token OAK Research MCP (auto-rotate & tersinkronisasi balik via `GH_PAT`) |
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
| `DASHSCOPE_MODEL_V1` | `glm-5.2-fast-preview` | Model Voter 1 (DashScope cepat & hemat). |
| `DASHSCOPE_MODEL` | `glm-5.3-prime` | Model Voter 2 (DashScope #1 OAK Benchmark). |
| `DASHSCOPE_MODEL_AGG` | `glm-5.3` | Model utama CRO Meta-Aggregator P1–P7 (#1 OAK Winner via `DASHSCOPE_API_KEY`). |
| `GEMINI_BASE_URL` | `https://router.classai.id/v1` | Endpoint router OpenAI-compatible untuk Voter 3, Voter 4, & fallback aggregator. |
| `GEMINI_MODEL` | `minimax-m3` | Model Voter 3 via ClassAI Router (#7 OAK Max DD terendah `-6.5%`, 0 likuidasi). |
| `GEMINI_MODEL_V4` | `claude-haiku-5.5` | Model Voter 4 via ClassAI Router (1.84s cepat & hemat, konteks 1M). |
| `CLASSAI_MODEL_AGG` | `claude-opus-5.5` | Fallback aggregator lapis ke-2 via ClassAI Router (cadangan ke-2: `CLASSAI_MODEL_AGG_FALLBACK=gpt-6.1-sol`). |
| `MULTI_OPP_GLM_FALLBACK` | `false` | Jika `true`, saat `opportunity_count > 1` alihkan V3/V4 ke DashScope GLM. Default `false` menjaga keragaman 3 vendor. |
| `OAK_MCP_ENABLED` | `on` | Aktifkan pengayaan konteks real-time dari OAK Research MCP Server (`OAK_MCP_URL=https://oakresearch.io/api/mcp`, opsional `OAK_MCP_PREMIUM=false`). |
| `US_STOCK_HOURS_ONLY` | `true` | Memfilter pemindaian & eksekusi `ALLOWED_STOCKS` ke jam bursa reguler AS (`Sen–Jum 13:30–21:00 UTC`). Crypto & Komoditas tetap 24/7. |
| `OFF_HOURS_HOLD_POLICY` | `hybrid` | Kebijakan Opsi B di `risk-monitor.yml`: nol toleransi biaya inap (`< 0%`), filter pre-close overnight/weekend (Saham AS overnight+weekend; Komoditas weekend), & early SL lock `+0.25%` (`>= +0.40%`). Set `off` untuk menonaktifkan. |
| `CRYPTO_FORECAST_ENGINE` | `on` | Aktifkan Engine 2 Crypto Mean-Reversion Fade teruji OOS (`V5 4h Rejected->Comfort` + `V4 1h Extreme Fade`, ATR SL `<= 4.5%`, Time-Stop `24h/72h`) di `crypto_execute`. |
| `GEAR_3_FULL_RESEARCH` | `false` | Jika `true` (atau peluang `GO > 5`), mengaktifkan **Gear 3** agar setiap kandidat trading mendapat riset AI penuh tanpa cap. |
| `TRAINING_DATA_LOGGING` | `on` | Otomatis menyimpan dataset SFT `<think>...</think>` ke Google Sheet tab `Data Training` dan `logs/training_dataset.jsonl`. |
| `WALT_TOKEN_SYNC` | `on` | `off` **menolak refresh** (bukan "refresh lalu buang hasilnya") — wajib `on` untuk workflow yang butuh OAuth. |
| `WALT_WATCHDOG_DISPATCH_ENTRY` | `false` | `true` mengizinkan watchdog men-dispatch ulang workflow entry; default *alert-only* karena dispatch membuka order nyata. |
| `GOOGLE_SHEET_ID` | — | ID spreadsheet ledger (yang benar bersidik jari `06bded7f`). |
| `STRATEGY_CONFIG_JSON` | berkas `strategy_config.json` | Override parameter strategi dari CI tanpa commit. |


### 2c. Status Webhook v2 & Panduan Redeploy Apps Script

Webhook adalah satu-satunya penulis ledger ke Google Sheets. **Status Produksi (2026-11-22):** Apps Script Webhook v2 (`"version":"2026-11-21.1"`) **sudah ter-deploy dan terverifikasi aktif di produksi** untuk seluruh **5 tab** (`Daily_Scans` tepat 30 kolom bernama tanpa duplikat, `Executions_Log`, `Closed_Positions`, `PnL_Tracker`, serta tab baru **`Data Training`** dengan 12 kolom SFT yang sudah terisi 48 baris awal tervalidasi).

Jika di masa depan `google_apps_script.js` dimodifikasi, gunakan langkah redeploy berikut (±5 menit, tidak perlu menyentuh bot):

1. Buka spreadsheet: `https://docs.google.com/spreadsheets/d/<GOOGLE_SHEET_ID>/edit`
   (ID diambil dari secret `GOOGLE_SHEET_ID`, jangan ditulis di tempat publik).
2. Menu **Extensions → Apps Script**.
3. Di editor: buka `Code.js`, **select-all → hapus**, lalu tempel SELURUH isi
   `google_apps_script.js` dari repo (jangan sebagian — fungsi helper dipakai `doPost`).
4. **Ctrl+S** (Simpan). Beri nama proyek mis. `walt-webhook-v2`.
5. Menu **Deploy → Manage deployments** → klik **✏️ Edit** pada deployment Web app yang
   sudah ada → **Version: New version** → tulis description `v2 2026-11-21` →
   **Deploy**. Memilih "New version" pada deployment yang sama **menjaga URL `/exec`
   tetap sama**, jadi tidak ada secret yang perlu diganti.
   (Bila tidak ada deployment lama: **Deploy → New deployment → Web app**,
   *Execute as: Me*, *Who has access: Anyone*, lalu Deploy.)
6. Bila diminta otorisasi: **Authorize access** → akun Anda → *Advanced* →
   *Go to project (unsafe)* → **Allow**. Tanpa langkah ini `/exec` balas 401.
7. **Uji tanpa menulis apa pun** di browser:
   `<WEBHOOK_URL>?probe=1`
   Yang harus terlihat: `"version":"2026-11-21.1"` dan daftar tab + header. Jika yang
   muncul `Webhook is active!` atau HTML, deployment baru belum aktif — ulangi langkah 5.
8. Uji tulis sekali dari terminal (payload minimal, satu baris ke tab uji, bukan data asli):
   ```powershell
   $body = @{ sheet="Daily_Scans"; header=@("timestamp_utc","asset"); records=@(@{ timestamp_utc="PROBE"; asset="PROBE" }) } | ConvertTo-Json -Depth 5
   Invoke-RestMethod -Method Post -Uri "<WEBHOOK_URL>" -Body $body -ContentType "application/json"
   ```
   Balasan wajib memuat `rows_added: 1`, `verified: true`, dan `resolved_header` berisi
   kolom `mg_*` (kolom baru ditambahkan di ujung kanan). Baris `PROBE` boleh dihapus
   manual dari sheet setelah yakin.
9. Rollback: **Deploy → Manage deployments → 🗑️** versi baru, atau tempel kembali kode
   lama. Tidak ada migrasi data; kolom `mg_*` yang sudah terbentuk hanya akan berisi kosong.

Perubahan perilaku yang perlu diketahui:

- Header sheet tidak pernah ditulis ulang sembarangan. Bila baris 1 punya label, kolom
  baru hanya menempati sel header kosong yang **kolom datanya juga kosong**, sisanya
  ditambahkan di kanan. Kolom lama tidak digeser, tidak dibalik, tidak dihapus.
- Sheet dengan lebar default 26 kolom otomatis diperlebar (header kita 30 kolom).
- Balasan menyertakan `columns_added`, `dropped_keys`, `rows_skipped`, `mismatch_columns`
  — kalau suatu saat kolom hilang, penyebabnya terbaca dari respons, bukan dari dugaan.
- Uji logika ini berjalan di CI lewat `tests/test_apps_script.py` (butuh `node`;
  di-skip dengan alasan bila node tidak ada).

### 2d. Angka pembanding dari Google Finance (bukan TradingView)

Google Finance **tidak** menampilkan nilai indikator Market Gravity dan (terverifikasi
pada Ronde 8) tidak menyematkan deret historis di halaman quotenya. Jadi yang bisa
dibandingkan adalah **jangkar harga** yang dipakai model — bukan skor gravitasinya.
Untuk satu aset dan satu tanggal, minta angka berikut (mis. lewat Gemini):

| Bidang | Padanan di sistem kita |
|---|---|
| `spot` harga terakhir | `price` pada baris scan / candle terakhir |
| `prev_close` penutupan hari sebelumnya | `previousDayClose` pada `Daily_Scans` |
| `day_high`, `day_low` hari berjalan | high/low hari yang sama di bar venue |
| `asof_utc` waktu pengambilan | `timestamp_utc` |

Alat `tools/gravity_parity_check.py` membandingkan angka-angka itu dengan bar yang kita
pakai dan keluar merah bila selisihnya melebihi toleransi — karena salah sumber harga
akan membuat seluruh skor gravitasi salah tanpa terlihat. Paritas SKOR terhadap
TradingView tetap terbuka dan tidak bisa dinyatakan dari Google Finance.

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
