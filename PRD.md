# Product Requirements Document (PRD)
# Walt Perps Trading Bot — Phased Evolution: Owner Core & Multi-User SaaS Platform

**Version:** 2.2 (Production Alignment — 2026-11-22)  
**Date:** 2026-11-22  
**Platform Owner & Lead Architect:** Rizal Dicky  
**Status:** Production Live (Fase 1 Owner Core Aktif + OAK MCP + SFT Data Collection / Fase 2 SaaS Expansion)  

---

## 1. Executive Summary

Dokumen ini mendefinisikan peta jalan pengembangan **Walt Perps Trading Bot** melalui pendekatan bertahap (*phased rollout*). Bot ini berfokus pada strategi *funding rate arbitrage* (delta-neutral dan directional funding capture) di platform Walt (`walletbot.me`) dengan perbantuan riset kecerdasan buatan kolaboratif lintas vendor (**Multi-Vendor AI Research Swarm: GLM-5.2-Fast, GLM-5.3-Prime, MiniMax M3, Claude Haiku 5.5 + CRO Meta-Aggregator `glm-5.3`**) yang diperkaya data real-time dari **OAK Research Remote MCP Server** (`https://oakresearch.io/api/mcp`).

Untuk menyeimbangkan antara **kebutuhan eksekusi segera** dan **potensi komersialisasi masa depan**, peta jalan dibagi menjadi 2 fase utama:

1. **Fase 1: Owner-Dedicated Core Bot (Produksi Live)**  
   - Menggunakan infrastruktur Python 3.14, GitHub Actions cloud runners + Google Cloud Scheduler, OAK Research Remote MCP Server (`oak_mcp_client.py`), Swarm AI (`glm-5.3` CRO Meta-Aggregator via `DASHSCOPE_API_KEY` + ClassAI Router via `DASHSCOPE_API_GEMINI_KEY`), serta pengumpulan dataset SFT `<think>` otomatis ke Google Sheet tab **`Data Training`** (`training_data.py`).  
   - **Eksklusif untuk Platform Owner (Rizal Dicky)**: Menghasilkan *passive income* harian ($50–$100/bulan) dari modal pribadi, memverifikasi stabilitas eksekusi Walt API, dan memperketat risk guardrails dengan biaya infrastruktur $0 (memanfaatkan GitHub Actions free tier).  
   - **Status:** **LIVE di Produksi (Terverifikasi CI + Webhook v2 + OAK MCP).**

2. **Fase 2: Multi-User SaaS, Sistem Bisnis & Dashboard Interaktif (Komersialisasi Publik)**  
   - Membuka akses bot kepada pengguna publik (*Subscribers / Retail Traders*) dengan arsitektur *non-custodial* (OAuth 2.0 PKCE, tanpa akses private key).  
   - Membangun **Sistem Bisnis & Monetisasi Lengkap**: Tingkatan langganan (Free Trial 7 Hari, Standard $15/bln, Pro $35/bln), gateway pembayaran multi-channel (Instant QRIS, Crypto USDT/USDC Arbitrum/Base, dan Telegram Stars), serta *automated dunning & webhook settlement*.  
   - Membangun **Dashboard Interaktif**: Antarmuka visual Telegram Mini App (TWA) responsif dan menu interaktif Telegram Bot untuk memantau portofolio, PnL real-time, status posisi, konfigurasi margin, serta Admin Console terpusat bagi Owner.  
   - Skalabilitas infrastruktur ke lingkungan serverless multi-tenant (Firebase Cloud Functions Gen 2 / Google Cloud Run + Firestore) dengan *Shared AI Core* untuk efisiensi biaya API.

---

## 2. Matriks Perbandingan Arsitektur: Fase 1 vs Fase 2

| Dimensi | Fase 1: Owner Core (Immediate) | Fase 2: Multi-User SaaS & Dashboard |
|---|---|---|
| **Target Pengguna** | 1 Orang (Platform Owner: Rizal Dicky) | Multi-User (Public Subscribers & Traders) |
| **Infrastruktur Komputasi** | GitHub Actions Cloud Runners (Ubuntu) + Local Python | Firebase Cloud Functions Gen 2 / Cloud Run + Node.js 20 |
| **Bahasa & Runtime** | Python 3.14 (`walt_bot.py`, `token_manager.py`, `oak_mcp_client.py`) | TypeScript / Node.js 20 + Client WebApp (React/Vite) |
| **Mekanisme Scheduler** | Google Cloud Scheduler + GitHub Actions (`.github/workflows/*.yml`, Gear 0–3) | Google Cloud Scheduler + Pub/Sub (WIB Precision) |
| **Basis Data & State** | GitHub Actions Cache + Google Sheets Webhook v2 (5 tab incl. `Data Training`) | Google Cloud Firestore (Multi-Tenant Document Store) |
| **Kredensial & Secrets** | GitHub Repository Secrets & Local `.env` (auto-rotate Walt & OAK MCP) | AES-256-GCM Vault di Firestore + Google Cloud KMS |
| **Riset AI (Swarm Voting + Aggregator)** | 4 Voters lintas vendor (`glm-5.2-fast-preview`, `glm-5.3-prime`, `minimax-m3`, `claude-haiku-5.5`) + OAK MCP $\to$ CRO `glm-5.3` | *Shared Swarm Engine* (1 pemanggilan global untuk semua user + model SFT Fase 2) |
| **Antarmuka Utama** | Notifikasi Broadcast Telegram ke `TELEGRAM_CHAT_ID` | **Dashboard Interaktif (Telegram Mini App)** + Menu Bot |
| **Sistem Bisnis / Billing**| $0 (Self-funded dari hasil funding rate pribadi) | Pembayaran Otomatis: QRIS, Crypto (USDT), Telegram Stars |
| **Koneksi Walt** | 1 Akun Walt Owner via OAuth Refresh Token | Multi-Tenant Dynamic OAuth PKCE per Pengguna |
| **Biaya Infrastruktur** | **Rp 0 / $0 per bulan** (Free tier GitHub Actions) | ~$15–$25 / bulan (Ditutupi oleh subscription revenue) |
| **Target Peluncuran** | **Minggu Ini (Segera Diterapkan)** | Q1 2027 (Peluncuran Komersial Bertahap) |

---

## 3. Fase 1: Owner-Dedicated Core Bot (Infrastruktur Eksisting)

### 3.1 Prinsip Desain Fase 1
- **Zero Rework / Zero Latency:** Memaksimalkan codebase Python yang telah berjalan di produksi (`walt_bot.py`, `token_manager.py`, `oak_mcp_client.py`, `llm_research.py`, `training_data.py`, `notifier.py`, `sheet_logger.py`).
- **Zero Cost:** Sepenuhnya beroperasi di atas kuota gratis GitHub Actions (2.000 menit/bulan untuk private repository) dan free credits DashScope.
- **Fokus Tunggal:** Menangkap peluang funding rate arbitrage secara konsisten untuk portofolio Owner dengan manajemen risiko konservatif.

### 3.2 Diagram Alur Kerja Fase 1 (Infrastruktur Eksisting)

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        FASE 1: OWNER RUNTIME WORKFLOW                                  │
└────────────────────────────────────────────────────────────────────────────────────────┘

    Google Cloud Scheduler & GitHub Actions
    ├─ 07:17 & 19:17 WIB (daily-scan.yml — Scan, OAK MCP + Swarm & Execute)
    ├─ Tiap 10 Menit (risk-monitor.yml — 4-Layer Risk & Smart Fast-Exit Idle <2s)
    ├─ Tiap 2 Jam @ menit 43 (market-execute.yml — Gear 0-3: Saham US [13:30-21:00 UTC] & Komoditas 24/7)
    └─ Tiap 2 Jam @ menit 11 (crypto-execute.yml — Gear 0-3: Crypto Bluechip 24/7)
                   │
                   ▼
     ┌───────────────────────────┐
     │  GitHub Actions Runner    │
     │  (Ubuntu / Python 3.14)   │
     └─────────────┬─────────────┘
                   │
         [Inject Secrets via Env]
         • WALT_CLIENT_ID & WALT_REFRESH_TOKEN (Auto-Sync via Libsodium)
         • OAK_MCP_CLIENT_ID & OAK_MCP_REFRESH_TOKEN (Auto-Sync Rotated RT)
         • DASHSCOPE_API_KEY (DashScope: GLM-5.3 Agg + V1/V2)
         • DASHSCOPE_API_GEMINI_KEY (ClassAI Router: V3 MiniMax M3 + V4 Haiku 5.5)
         • TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID
         • GOOGLE_SHEET_WEBHOOK_URL & GOOGLE_SHEET_ID
                   │
                   ▼
     ┌───────────────────────────┐
     │   walt_bot.py (Orchestrator)
     │                           │
     │   1. token_manager.py ────┼───> Walt OAuth Server (Get/Refresh Access Token)
     │   2. Walt Perps API ──────┼───> Fetch Contracts, Real-Time Positions & Funding
     │   3. oak_mcp_client.py ───┼───> OAK Research Remote MCP (Macro, Cal, News, Asset)
     │   4. llm_research.py ─────┼───> 4-Voter Swarm + GLM-5.3 CRO Meta-Aggregator
     │   5. training_data.py ────┼───> Log SFT <think> JSONL ke Tab 'Data Training'
     │   6. Execute Position ────┼───> Dynamic Slot (5-15) & Conviction Sizing (@ 2x Lev)
     │   7. notifier.py & sheets ┼───> Telegram Bot API & Google Sheets Webhook v2
     └───────────────────────────┘
                   │
                   ▼
     ┌───────────────────────────┐
     │  Platform Owner Telegram  │
     │  - Laporan Scan Harian    │
     │  - Eksekusi Arbitrage     │
     │  - Peringatan Margin/Risk │
     └───────────────────────────┘
```

### 3.3 Jadwal & Spesifikasi Eksekusi Fase 1
1. **Daily Scan (`daily-scan.yml` — 07:17 & 19:17 WIB / 00:17 & 12:17 UTC via `walt-daily-scan` Scheduler):**
   - Mengambil seluruh kontrak perpetual di Walt (disaring ke 31 aset diizinkan: 18 Saham US, 3 Komoditas, 10 Crypto Tier-1).
   - Memfilter Saham US (`ALLOWED_STOCKS`) ke **jam bursa reguler NYSE/NASDAQ (`Senin–Jumat 13:30–21:00 UTC`, `US_STOCK_HOURS_ONLY=true`)**; Komoditas dan Crypto diproses 24/7.
   - Mengambil konteks real-time dari **OAK Research Remote MCP Server** (`oak_mcp_client.py`) lalu memanggil 4-Voter Swarm + CRO `glm-5.3` (`llm_research.py`) dan mencatat dataset `<think>` ke tab **`Data Training`** (`training_data.py`).
   - Eksekusi instan order berstatus `GO` / `HALF` (dimana **`'GO'` otomatis melewati batas frekuensi 24 jam `E4`**) dan menyimpan database ke `Daily_Scans` (30 kolom) & Telegram.
2. **Risk Monitor (`risk-monitor.yml` — Dinamis 5–10 Menit / 6x–12x per Jam via `walt-risk-monitor` Scheduler):**
   - **Opsi A Smart Fast-Exit Idle:** Jika 0 posisi aktif dan tidak ada perubahan snapshot penutupan, proses langsung selesai (*fast-exit*) dalam <2 detik sehingga hemat kuota runner hingga 95%.
   - **Dynamic Adaptive Cadence (5 Menit vs 10 Menit):**
     - **Mode Intensif ~5 Menit (hingga 12x/jam):** Otomatis aktif dengan *random jitter* (240–290 detik) apabila: (a) terdapat $\ge 3$ posisi aktif, (b) terdapat posisi dengan kategori keputusan `HALF`, atau (c) harga mendekati zona TP/Trailing ($\ge +0.70\%$ / jarak TP $\le 0.35\%$) maupun mendekati SL ($\le -1.00\%$ / jarak SL $\le 0.50\%$).
     - **Mode Normal 10 Menit (6x/jam):** Untuk posisi kategori `GO` yang berjumlah $< 3$ dan masih tenang jauh dari TP/SL, monitor berjalan stabil tiap 10 menit tanpa *spot check* acak yang boros.
   - **Deteksi Penutupan Posisi (`Closed_Positions`):** Membandingkan posisi aktif dengan `logs/open_snapshot.json` (dipersist via `actions/cache`), mencatat setiap penutupan posisi ke sheet `Closed_Positions` serta `logs/closed_history.csv`, dan mengaktifkan **3-Hour Re-Entry Cooldown** untuk aset yang baru ditutup.
   - Jika terdapat posisi aktif, bot memantau intensif tanpa jeda dan menerapkan **Proteksi Risiko Terpadu**:
     - **Lapis 1:** Asymmetric R:R Baseline TP (+2.0% ETF & Crypto, +2.5% Komoditas & Saham + Windfall $2.5\times$ di rentang $1.5\% \le \text{windfall} < \text{TP}$) & **Uniform SL 1.5%** di semua kelas aset, dilengkapi guard validasi sisi harga dan migrasi penutupan otomatis bagi posisi lama yang melampaui rugi 1.5%.
     - **Lapis 2:** Funding Integrity dengan **Hysteresis 0.1%/hari** (*emergency close* jika funding berlawanan arah $\ge 0.1\%$/hari, atau geser SL ke breakeven jika $< 0.1\%$/hari).
     - **Lapis 3:** *Deprecated* (digantikan oleh SL baseline 1.5%).
     - **Lapis 4:** **True Zero-Risk Trailing Tiers:** Profit $\ge +0.80\% \to$ lock SL di `Entry + 0.35%` (`lock_0035`); Profit $\ge +1.40\% \to$ lock SL di `Entry + 0.85%` (`lock_0085`); Profit $\ge +2.00\% \to$ Trailing Stop dinamis `0.60%`.
   - **Hybrid Cost-Aware Off-Hours Policy (`OFF_HOURS_HOLD_POLICY=hybrid`, Opsi B):** Berlaku untuk **Saham AS** (overnight Sen–Kam & weekend) dan **Komoditas** (`XAU`, `XAG`, `BRENTOIL` khusus weekend Jum `21:00`–Min `22:00 UTC`; Crypto tetap 24/7). Menutup posisi segera bila funding menjadi biaya inap (`< 0%`), mewajibkan kelayakan pre-close `20:35–21:00 UTC` (overnight `>= +0.20%/hari`, weekend `>= +0.50%/hari` & PnL `>= -0.50%`, atau sudah profit-locked), serta mengunci SL dini ke `Entry +/- 0.25%` saat profit `>= +0.40%`.
3. **Market Execute (`market-execute.yml` — Sistem 4-Tier Execution Gear `0..3`):**
   - Saham US (`ALLOWED_STOCKS`) hanya diproses saat **jam bursa reguler NYSE/NASDAQ (`Senin–Jumat 13:30–21:00 UTC`, `US_STOCK_HOURS_ONLY=true`)**, sementara Komoditas (`XAU`, `XAG`, `BRENTOIL`) aktif 24/7 memakai **4-Tier Execution Gear (`evaluate_execution_gear`)**:
     - **Gear 0 (`GEAR_0_IDLE_2X_US` — `0` peluang `'GO'`):** Berjalan maksimal **2 kali selama jam operasional Saham AS** (`13:30–21:00 UTC`, interval `225` menit).
     - **Gear 1 (`GEAR_1_CONSERVATIVE_120M` — `< 2` peluang `'GO'`):** Interval `120` menit (2 jam) agar posisi berkesempatan memanen funding tanpa *over-trading*.
     - **Gear 2 (`GEAR_2_ACTIVE_60M` — `2..5` peluang `'GO'`):** Interval `60` menit (1 jam) aktif menangkap peluang kuat.
     - **Gear 3 (`GEAR_3_FULL_RESEARCH_60M` — `> 5` peluang `'GO'` atau `GEAR_3_FULL_RESEARCH=true`):** Interval `60` menit (1 jam) + membuka izin **riset & analisa AI penuh untuk setiap keputusan trading** (mengangkat cap `max_otf_calls` & `CRYPTO_RESEARCH_TOP_N`).
   - **3-Hour Re-Entry Cooldown, `'GO'` Bypass E4, & Dynamic Slot Elasticity:** Verdict **`GO`** melewati batas frekuensi `2/aset/24j` & `8/hari` (sirkuit `-5%`, cooldown 3 jam, batas slot 5–15, & buffer kas 15% tetap ketat).
4. **Crypto Execute (`crypto-execute.yml` — Dual-Engine + 4-Tier Gear `0..3`, 24/7 via Cloud Scheduler):**
   - Modul mandiri 24/7 yang menggabungkan **Engine 1 (Funding Carry `>= 0.50%/hari`, khusus 10 crypto bluechip `ALLOWED_CRYPTO`)** dan **Engine 2 (`CRYPTO_FORECAST_ENGINE=on`, `CRYPTO_FORECAST_UNIVERSE=all` — Mean-Reversion Fade teruji OOS `V5 4h Rejected->Comfort` & `V4 1h Extreme Fade` untuk seluruh kontrak crypto likuid di Walt, top 50 volume >= $1M, maks 5 sinyal/siklus)** dengan 4-Tier Gear + *3-hour re-entry cooldown*.
   - Manajemen risiko Engine 2: ATR SL (`1.5%–4.5%`), Time-Stop `24h/72h` (kecuali trailing L4 aktif), serta tutup darurat bila *adverse funding* `>= 0.30%/hari`.
   - Engine 1 tetap membatasi aset ke 10 crypto bluechip (`ALLOWED_CRYPTO`); non-crypto (saham, komoditas, FX, stablecoin) tetap dikecualikan dari Engine 2.
5. **Watchdog & CI Safety (`watchdog.yml` & `tests.yml`):**
   - **Cloud Scheduler Satu-Satunya Trigger Trading:** Blok `schedule:` dihapus dari 4 workflow trading agar tidak terjadi eksekusi dobel/terlambat. Verifikasi job Cloud Scheduler dilakukan dengan `gcloud scheduler jobs list --location=asia-southeast2`.
   - **Watchdog (`watchdog.yml`, `0 * * * *`, timeout `3m`):** Mengecek apakah `Walt Risk Monitor` sukses dalam 30 menit terakhir; jika tidak, mengirim alert Telegram dan memicu 1x `workflow_dispatch` fallback (tanpa mengakses Walt API).
   - **Timeout & Guard 6 Menit:** `risk-monitor` memakai `timeout-minutes: 20` (lainnya `15`), dengan guard di `walt_bot.py` yang melewati *sub-cycle* & *adaptive execute* jika sisa waktu `< 6 menit` sebelum `JOB_TIMEOUT_MIN`.
   - **Cache Tahan Error & Integritas Data (`D1`–`D4`):** Memakai pasangan `actions/cache/restore@v4` + `save@v4` (`if: always()`), cache terpisah `walt-snapshot-*` eksklusif untuk `risk-monitor.yml`, dedupe penutupan berbasis unique `Position ID` (`logs/closed_ids.json`), pelabelan jujur `UNKNOWN` bila data close exchange tidak ada, pengelolaan header tunggal di Apps Script `doPost` (+ fungsi manual `cleanupClosedPositions()`, versi produksi `2026-11-21.1` untuk 5 tab termasuk `Data Training`), kolom `Strategy Version`, serta `LOG_REDACT=true` + header `Authorization: Bearer ...` (`K1`).
   - **Gerbang Kualitas Entry & Proteksi Modal (`E1`–`E5`):** `HALF` di bawah floor `$2.50` → `SKIP`, `P5 >= max_p5_risk` (`0.70`) → `SKIP`, riset basi `> max_research_age_hours` (`6` jam) diabaikan, batas `2 entry/aset/24h` & `8 entry/24h` untuk `HALF` (**verdict `'GO'` otomatis melewati batas frekuensi 24h**), *daily circuit breaker* `-5%` ekuitas UTC, serta gerbang ekonomi `expected_funding >= 1.5 × (fee + slippage)` + metrik *median hold time* pada `stats`.
   - **Sinkronisasi Konfigurasi Strategi Bersama:** Telegram `/set_strategy` menyinkronkan parameter aman (tervalidasi `STRATEGY_PARAM_SPECS`) ke GitHub Repository Variable `STRATEGY_CONFIG_JSON` untuk dibaca `walt_bot.py` pada run berikutnya (tanpa mengekspor `user_strategies.json` / ID Telegram).
   - **CI Test (`tests.yml`):** Menjalankan `pytest -q` (`608 passed`) dan `tools/sanitize_gate.py` pada setiap push/PR ke `main` di concurrency terpisah (`walt-tests`).

### 3.4 Langkah Penerapan Segera (Fase 1 Action Plan)
- [x] Pastikan GitHub Secret terkonfigurasi lengkap di repository (`WALT_CLIENT_ID`, `WALT_REFRESH_TOKEN`, `OAK_MCP_CLIENT_ID`, `OAK_MCP_REFRESH_TOKEN`, `DASHSCOPE_API_KEY`, `DASHSCOPE_API_GEMINI_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GOOGLE_SHEET_WEBHOOK_URL`, `GH_PAT`).
- [x] Lakukan verifikasi uji coba manual (*manual trigger*) untuk workflow `daily-scan.yml`, `risk-monitor.yml`, `market-execute.yml`, dan `crypto-execute.yml`.
- [x] Pasang Google Cloud Scheduler Bridge (Jakarta `asia-southeast2`) untuk trigger tepat detik tanpa delay cron GitHub.
- [x] Implementasikan Asymmetric R:R TP (+2.0%/+2.5%) & Uniform SL 1.5% per Asset Class, Windfall Rule 2.5x, True Zero-Risk Trailing Tiers (`+0.35%` / `+0.85%` / `0.60%`), dan 3-Hour Re-Entry Cooldown.
- [x] Terapkan Minimum Order Size Clamping (`min_size_base`) untuk mencegah kegagalan risk check Walt.
- [x] Hubungkan real-time logging ke Google Sheets via Google Apps Script Webhook.
- [x] Aktifkan mode eksekusi riil pada saldo terdedikasi di Walt Perps.

### 3.5 Arsitektur Riset AI: OAK MCP Enrichment + 4-Voter Swarm + GLM-5.3 CRO Meta-Aggregator

Sistem analisis AI berbasis **OAK Research MCP + 4 Voter Lintas Vendor + 1 CRO Meta-Aggregator (`glm-5.3`)** yang aktif di produksi Fase 1 (dan mengumpulkan dataset SFT `<think>` untuk Fase 2 di tab `'Data Training'`).

#### 3.5.1 Prinsip Desain
- **OAK Research MCP Grounding (`oak_mcp_client.py`):** Sebelum voter menganalisa, bot menyuntikkan blok `[DATA REAL-TIME OAK RESEARCH MCP]` berisi *market overview*, kalender makro/earnings/unlocks 7 hari (`get_calendar`), berita utama (`get_news`), serta metrik aset (`get_crypto`/`get_tradfi`) dari `https://oakresearch.io/api/mcp`.
- **Standardized Validation Parameters:** Semua 4 voter menilai **7 parameter yang SAMA** (P1-P7). Perbedaannya pada kekuatan model lintas vendor (**Zhipu GLM**, **MiniMax M3**, dan **Claude Haiku 5.5**) dan gaya reasoning per peran.
- **Apples-to-Apples Comparability:** Dengan parameter sama, aggregator bisa compare per-parameter dari tiap voter dan mengidentifikasi outlier berbasis data MCP.
- **CRO Meta-Aggregator (`glm-5.3`):** Pemenang #1 **OAK Trading Benchmark** (`+29.4%`, 0 likuidasi) melalui DashScope bertindak sebagai Chief Risk Officer yang memproduksi `final_score` berbobot.
- **SFT Dataset Pipeline (`training_data.py`):** Setiap keputusan riset yang lolos validasi otomatis disusun dalam format chat-messages `<think>...</think>` + JSON ke Google Sheet tab **`Data Training`** (`12` kolom, target $\ge 1.000$ baris) untuk persiapan fine-tuning Fase 2.

#### 3.5.2 Konfigurasi Voter Produksi Fase 1 (Multi-Vendor OAK Benchmark Panel)

| Voter | Model | Vendor / Endpoint | Role | Bobot Dasar | Keunggulan |
|---|---|---|---|---|---|
| **V1** | `glm-5.2-fast-preview` (`DASHSCOPE_MODEL_V1`) | Alibaba DashScope (`DASHSCOPE_API_KEY`) | Fast Validator | 0.25 | Super cepat & hemat untuk scanning polaritas awal |
| **V2** | `glm-5.3-prime` (`DASHSCOPE_MODEL`) | Alibaba DashScope (`DASHSCOPE_API_KEY`) | Alpha Signal Analyst | 0.25 | Varian #1 OAK Trading Benchmark (`+29.4%`, 0 likuidasi) |
| **V3** | `minimax-m3` (`GEMINI_MODEL`) | ClassAI Router (`DASHSCOPE_API_GEMINI_KEY`) | Conservative Risk Guard | 0.25 | #7 OAK dengan Max Drawdown terendah (`-6.5%`, 0 liq, biaya sangat hemat) |
| **V4** | `claude-haiku-5.5` (`GEMINI_MODEL_V4`) | ClassAI Router (`DASHSCOPE_API_GEMINI_KEY`) | Fast Multi-Angle Reasoner | 0.25 | Latensi ~1.84s, konteks 1M, tajam untuk causal & contrarian check |

*(Opsional: Variabel `MULTI_OPP_GLM_FALLBACK=true` mengizinkan V3/V4 dialihkan ke GLM DashScope saat peluang `> 1`; default produksi `false` agar keragaman 3 vendor selalu aktif.)*

#### 3.5.3 Konfigurasi CRO Meta-Aggregator
- **Model Utama:** `glm-5.3` (`DASHSCOPE_MODEL_AGG`, via Alibaba DashScope `DASHSCOPE_API_KEY` — #1 OAK Trading Benchmark Winner)
- **Rantai Fallback:** DashScope (`glm-5.3-prime` $\to$ `glm-5.2-fast-preview` $\to$ `qwen-plus` $\to$ `qwen-flash`) $\to$ ClassAI Router (`claude-opus-5.5` via `CLASSAI_MODEL_AGG` $\to$ `gpt-6.1-sol` via `CLASSAI_MODEL_AGG_FALLBACK`) $\to$ sintesis deterministik rule-based P1–P7.
- **Alasan Pemilihan:** Karena seluruh data fundamental/makro telah valid dari MCP OAK Research, voter dapat tetap ultra-hemat sementara CRO Meta-Aggregator memakai model juara trading langsung dari OAK Benchmark (`glm-5.3`) untuk menjaga disiplin eksekusi dan nol likuidasi.
- **Input:** Headlines + context + 4 voter verdicts (score + reasoning)
- **Output JSON:**
  ```json
  {
    "final_score": -1.0 to +1.0,
    "confidence": 0.0 to 1.0,
    "agreement_level": "unanimous|strong|mixed|split",
    "vendor_alignment": "both_agree|qwen_dominant|gemini_dominant|split",
    "trusted_voter": "V1/V2/V3/V4",
    "justification": "1-2 paragraf reasoning",
    "disagreement_areas": ["...", "..."]
  }
  ```

#### 3.5.3a Standardized Validation Parameters (P1-P7)

Semua voter menilai 7 parameter yang sama. Inilah **kontrak input/output** tiap voter.

| # | Parameter | Definisi | Scale | Weight dalam Final Score |
|---|---|---|---|---|
| **P1** | `sentiment_polarity` | Overall tone dari news 7 hari | -1.0 (bearish ekstrem) → +1.0 (bullish ekstrem) | 0.25 |
| **P2** | `signal_strength` | Berapa banyak headlines align ke arah sama (convergence) | 0.0 (divergen) → 1.0 (unanimous) | 0.10 |
| **P3** | `source_credibility` | Weighted score berdasarkan quality sources (Reuters/WSJ vs blog) | 0.0 (low) → 1.0 (high tier1) | 0.10 |
| **P4** | `momentum_direction` | Apakah sentiment akseleratif atau mereda 48 jam terakhir? | -1.0 (reversal) → +1.0 (akseleratif) | 0.20 |
| **P5** | `risk_catalysts` | Upcoming events yang bisa invalidate thesis (earnings, FOMC, Fed) | 0.0 (no catalyst) → 1.0 (major event <7d) | 0.15 |
| **P6** | `contrarian_check` | Apakah positioning crowded? (semua long?) | 0.0 (not crowded) → 1.0 (very crowded, reversal risk) | 0.10 |
| **P7** | `historical_pattern` | Analog ke pattern lampau | -1.0 (pattern negatif) → +1.0 (pattern positif) | 0.10 |

**Total weight = 1.0**

**Final score per voter:** `Σ (P_i × weight_i)`
**Final decision score (across voters):** per-parameter consensus analysis (lihat Aggregator)

#### 3.5.4 Unified Prompt Template (Semua Voter)

Semua voter dapat prompt dengan shape sama. Perbedaannya hanya di `[VOTER_ROLE]` dan `[VOTER_PERSPECTIVE]`.

##### Template Umum
```
[SYSTEM]
Kamu {VOTER_ROLE}. Analisa ticker berikut dan score 7 parameter validasi
berikut HARUS dengan JSON valid. Gunakan {VOTER_PERSPECTIVE} untuk reasoning,
tapi SEMUA voter menilai parameter yang sama.

[USER]
Ticker: {ticker}
Sektor: {sector}
Headlines 7 hari: {headlines_with_sources}
Harga sekarang: {price}
Context tambahan: {extra_context}

Score 7 parameter berikut (HARUS output JSON valid):
{
  "P1_sentiment_polarity": {
    "score": -1.0..+1.0,
    "reasoning": "1-2 kalimat kenapa score ini",
    "metadata": {"dominant_tone": "bullish|bearish|neutral", "confidence_level": 0..1}
  },
  "P2_signal_strength": {
    "score": 0.0..1.0,
    "reasoning": "1-2 kalimat",
    "metadata": {"aligned_count": N, "total_count": N}
  },
  "P3_source_credibility": {
    "score": 0.0..1.0,
    "reasoning": "1-2 kalimat",
    "metadata": {"tier1_count": N, "tier2_count": N, "tier3_count": N}
  },
  "P4_momentum_direction": {
    "score": -1.0..+1.0,
    "reasoning": "1-2 kalimat (fokus 48 jam terakhir)",
    "metadata": {"recent_48h_tone": "...", "shift_from_7d_avg": number}
  },
  "P5_risk_catalysts": {
    "score": 0.0..1.0,
    "reasoning": "1-2 kalimat",
    "metadata": {"catalyst_list": [{"type":"...", "date":"YYYY-MM-DD", "impact":"low|med|high"}], "days_to_nearest": N}
  },
  "P6_contrarian_check": {
    "score": 0.0..1.0,
    "reasoning": "1-2 kalimat",
    "metadata": {"crowdedness_indicators": ["...", "..."]}
  },
  "P7_historical_pattern": {
    "score": -1.0..+1.0,
    "reasoning": "1-2 kalimat",
    "metadata": {"analog_description": "...", "historical_outcome": "bullish|bearish|mixed"}
  },
  "overall_voter_confidence": 0.0..1.0,
  "voter_id": "{voter_id}"
}
```

##### Perbedaan Tiap Voter (Hanya di VOTER_ROLE + VOTER_PERSPECTIVE)

| Voter | `VOTER_ROLE` | `VOTER_PERSPECTIVE` | Kekhususan Reasoning |
|---|---|---|---|
| **V1 (`glm-5.2-fast-preview`)** | "Fast signal analyst" | "Fokus keyword scanning & polaritas permukaan, cepat & hemat" | Kekuatan di P1-P2 (tone polarity + signal strength) |
| **V2 (`glm-5.3-prime`)** | "Alpha signal & narrative analyst" | "Fokus kredibilitas sumber, konteks OAK MCP & konsistensi narasi (#1 OAK Benchmark)" | Kekuatan di P3 (source cred) + P1 nuance |
| **V3 (`minimax-m3`)** | "Conservative risk guard" | "Verifikasi breaking catalyst & proteksi modal (#7 OAK Max DD -6.5%, 0 likuidasi)" | Kekuatan di P4 (momentum 48h) + P5 (risk catalysts) |
| **V4 (`claude-haiku-5.5`)** | "Fast multi-angle reasoner" | "Causal reasoning cepat (1.84s, 1M ctx), pattern recognition & contrarian analysis" | Kekuatan di P6 (contrarian) + P7 (historical pattern) |

##### Contoh Isi Prompt untuk V3 (`minimax-m3` via ClassAI Router)
```
[SYSTEM]
Kamu conservative risk guard (`minimax-m3`). Gunakan [DATA REAL-TIME OAK RESEARCH MCP]
dan headlines untuk memverifikasi katalis risiko 24-48h serta proteksi modal.
Score 7 parameter validasi dengan JSON valid.

[USER]
Ticker: NVDA
Sektor: AI Chips / Semiconductor
[DATA REAL-TIME OAK RESEARCH MCP]: <market_overview, 7-day calendar, news, get_tradfi(NVDA)>
Headlines 7 hari: [list]
Harga sekarang: $485.30

Score 7 parameter P1-P7 dengan reasoning dan metadata (schema sama seperti voter lain).
```

Hasilnya: setiap voter mengevaluasi headlines sekaligus data real-time dari **OAK Research Remote MCP Server** dengan **schema output P1-P7 yang sama persis**. Aggregator dapat membandingkan `V1..V4` langsung per parameter.

---

##### Voter 1 — `glm-5.2-fast-preview` (Fast Signal Analyst)

**Yang dicari:** Keyword sentiment, dominant tone, polarity cepat berbiaya rendah.
**Yang divalidasi:** Apakah sentimen jelas dari permukaan? Berapa banyak headline align?

```
[SYSTEM]
Kamu quick signal analyst. Tugas: identifikasi sentiment DOMINAN dari
headlines dengan cepat (<2 detik). Fokus pada KATA KUNCI dan POLARITY.

[USER]
Ticker: {ticker}
Headlines 7 hari terakhir:
{headlines}

Tugas:
1. Scan keyword: "surge/crash/beat/miss/upgrade/downgrade/sued/launched"
2. Hitung headline bullish vs bearish vs neutral
3. Kalau 4+ dari 5 headline align → confidence tinggi

Output JSON:
{
  "score": -1.0..+1.0,
  "confidence": 0.0..1.0,
  "reasoning": "1 kalimat: dominant tone + jumlah headlines align",
  "key_keywords": ["surge", "upgrade", "beat"],
  "voter": "V1_glm_fast"
}
```

---

##### Voter 2 — `glm-5.3-prime` (Alpha Signal & Narrative Analyst — #1 OAK)

**Yang dicari:** Sentimen + reasoning + source credibility + konsistensi narasi.
**Yang divalidasi:** Apakah sentimen punya rasional kuat di balik data MCP?

```
[SYSTEM]
Kamu balanced equity/crypto alpha analyst (#1 OAK Trading Benchmark variant).
Weight headlines berdasarkan source credibility (Reuters/WSJ/Bloomberg/OAK MCP > blog random).
Cari CONSISTENCY narrative.

[USER]
Ticker: {ticker}
Sektor: {sector}
Headlines 7 hari dengan source:
{headlines_with_sources}

Tugas:
1. Weighted sentiment: Reuters/WSJ/OAK MCP 2x bobot, lain 1x
2. Cek konsistensi: ada headline yang kontradiksi?
3. Short-term signal vs long-term thesis

Output JSON:
{
  "score": -1.0..+1.0,
  "confidence": 0.0..1.0,
  "reasoning": "2-3 kalimat: dominant narrative + support/contradict",
  "credible_sources": ["Reuters: ...", "OAK MCP: ..."],
  "contradiction_found": true/false,
  "voter": "V2_glm_prime"
}
```

---

##### Voter 3 — `minimax-m3` (Conservative Risk Guard — #7 OAK Max DD -6.5%) ⭐

**Yang dicari:** Katalis risiko jangka pendek (FOMC, CPI, earnings, token unlock) dari `[DATA REAL-TIME OAK RESEARCH MCP]` dan proteksi downside.
**Yang divalidasi:** Apakah ada ancaman mendadak 24-48 jam yang dapat membatalkan thesis carry?

```
[SYSTEM]
Kamu conservative risk guard (#7 OAK Benchmark dengan Max Drawdown terendah -6.5%).
Analisa [DATA REAL-TIME OAK RESEARCH MCP] dan headlines 24-48 jam terakhir.

[USER]
Ticker: {ticker}
Headlines historis (7 hari) + OAK MCP Brief: {headlines}

Tugas:
1. Cek kalender OAK MCP (`get_calendar`): Fed/FOMC, CPI, earnings, token unlock
2. Cek OAK News (`get_news`) & metrik aset (`get_crypto`/`get_tradfi`)
3. Verifikasi apakah event terkini CONFIRM atau CONTRADICT thesis entry
4. Nilai P5 (risk_catalysts) secara konservatif

Output JSON:
{
  "score": -1.0..+1.0,
  "confidence": 0.0..1.0,
  "reasoning": "2 kalimat: berita 24h/OAK MCP + apakah ubah risiko",
  "breaking_news": [
    {"time": "2h ago", "source": "OAK/CNBC", "headline": "...", "impact": "high"}
  ],
  "sentiment_overridden": true/false,
  "voter": "V3_minimax_m3"
}
```

---

##### Voter 4 — `claude-haiku-5.5` (Fast Multi-Angle Reasoner — 1.84s, 1M Ctx)

**Yang dicari:** Causal chain, second-order effects, contrarian signal, hidden risks.
**Yang divalidasi:** Apakah narasi sustainable? Ada crowded trade / elephant in the room?

```
[SYSTEM]
Kamu senior portfolio manager & fast multi-angle reasoner. Analisa CAUSAL CHAIN
dan HIDDEN RISKS yang voter dangkal miss berbasis konteks penuh 1M token.

[USER]
Ticker: {ticker}
Sektor: {sector}
Market cap: {cap}
Headlines 7 hari + OAK MCP Data: {headlines}
Related stocks/coins performance: {peers}

Tugas:
1. Kenapa sentimen bullish/bearish? (causal reasoning)
2. Second-order effects: dampak ke sektor, suppliers, competitors
3. Contrarian check: apakah positioning crowded?
4. Hidden risk: lawsuit, SEC probe, token unlock besar, insider selling
5. Historical analog: situasi serupa dulu outcome apa?

Output JSON:
{
  "score": -1.0..+1.0,
  "confidence": 0.0..1.0,
  "reasoning": "3-4 kalimat: causal explanation + risk assessment",
  "causal_chain": ["...", "..."],
  "hidden_risks": ["lawsuit pending", "token unlock"],
  "contrarian_signal": true/false,
  "historical_analog": "Similar to Q2 2024 when ...",
  "voter": "V4_haiku_55"
}
```

---

#### 3.5.4b CRO Meta-Aggregator — `glm-5.3` (Per-Parameter Analysis)

Aggregator **tidak** cuma rata-rata vote. Dia analisa per-parameter berdasarkan konvensi CRO serta data real-time `[DATA REAL-TIME OAK RESEARCH MCP]`: siapa agree, siapa outlier, siapa paling kredibel per dimensi.

##### Pre-Processing (Rule-Based, Sebelum LLM Call)

```python
# Input: 4 voter verdicts, masing-masing berisi P1-P7 scores
voter_scores = {
    "V1": {"P1": 0.5, "P2": 0.7, "P3": 0.4, "P4": 0.3, "P5": 0.2, "P6": 0.5, "P7": 0.1},
    "V2": {"P1": 0.6, "P2": 0.8, "P3": 0.6, "P4": 0.4, "P5": 0.3, "P6": 0.5, "P7": 0.2},
    "V3": {"P1": 0.7, "P2": 0.6, "P3": 0.5, "P4": 0.8, "P5": 0.7, "P6": 0.4, "P7": 0.1},  # V3 beda jauh di P4, P5
    "V4": {"P1": 0.4, "P2": 0.5, "P3": 0.5, "P4": 0.3, "P5": 0.3, "P6": 0.7, "P7": 0.4},  # V4 beda di P6
}

# Per-parameter analysis
per_parameter_analysis = {}
for param in ["P1", "P2", ..., "P7"]:
    scores = [v[param] for v in voter_scores.values()]
    avg = mean(scores)
    variance = var(scores)
    
    if variance < 0.05:
        consensus_level = "UNANIMOUS"
    elif variance < 0.15:
        consensus_level = "STRONG"
    elif variance < 0.30:
        consensus_level = "MIXED"
    else:
        consensus_level = "SPLIT"
    
    # Identify outlier
    outlier_voter = max(voter_scores.items(),
                        key=lambda kv: abs(kv[1][param] - avg))
    
    per_parameter_analysis[param] = {
        "avg": avg,
        "variance": variance,
        "consensus_level": consensus_level,
        "outlier_voter": outlier_voter[0] if variance > 0.15 else None,
    }
```

##### LLM Prompt untuk Aggregator (`glm-5.3` CRO)

Aggregator LLM diberi per-parameter breakdown + raw voter reasoning + konteks `[DATA REAL-TIME OAK RESEARCH MCP]`, lalu diminta justifikasi mana yang trusted per parameter.

```
[SYSTEM]
Kamu Chief Risk Officer (CRO) & senior portfolio meta-analyst (`glm-5.3`).
Terima breakdown 7 parameter dari 4 voter lintas vendor (GLM-5.2, GLM-5.3-Prime,
MiniMax M3, Claude Haiku 5.5) serta [DATA REAL-TIME OAK RESEARCH MCP].
Analisa per-parameter: siapa paling kredibel, apa consensus, apa outlier.

[USER]
Ticker: {ticker}
Konteks: {sector, price, date}

Per-Parameter Breakdown (hasil pre-processing):
{per_parameter_analysis}

Raw Voter Reasoning (untuk konteks):
V1 glm-5.2-fast-preview:
  P1: {score} — "{reasoning}"
  P2: {score} — "{reasoning}"
  ... (P3-P7)
V2 glm-5.3-prime: ... (same structure)
V3 minimax-m3: ... (same structure, fokus risk/OAK MCP catalysts)
V4 claude-haiku-5.5: ... (same structure, contrarian + historical reasoning)

Tugas:
1. Untuk tiap parameter SPLIT atau MIXED:
   - Identifikasi outlier voter dan ALASAN kenapa outlier
   - Decide: outlier lebih kredibel atau konsensus lebih kredibel untuk situasi ini?
   - Contoh: V3 `minimax-m3` score P5 = 0.7 jauh dari V1-V2 (0.2, 0.3).
     V3 kredibel kalau didukung data kalender/news OAK MCP.
     Konsensus (V1-V2 avg 0.25) kredibel kalau alasan V3 cuma noise.

2. Untuk tiap parameter dengan STRONG/UNANIMOUS consensus:
   - Pakai average sebagai score final parameter tersebut

3. Compute final_score weighted:
   final_score = Σ(P_i.final × weight_i)
   weights: P1=0.25, P2=0.10, P3=0.10, P4=0.20, P5=0.15, P6=0.10, P7=0.10

4. Output JSON structured breakdown.

Return JSON:
{
  "per_parameter_final": {
    "P1": {"final_score": -1..1, "trusted_voter": "V1|V2|V3|V4|CONSENSUS", "rationale": "..."},
    "P2": {...},
    ... (P3-P7)
  },
  "final_score": -1.0..+1.0,
  "overall_confidence": 0.0..1.0,
  "overall_consensus_level": "unanimous|strong|mixed|split",
  "vendor_alignment": "both_agree|qwen_dominant|gemini_dominant|split",
  "most_influential_parameter": "P1..P7",
  "justification": "2-3 paragraf: kenapa final score begini, parameter mana paling influential, voter mana paling berperan",
  "disagreement_summary": ["...", "..."]
}
```

##### Output Aggregator — Contoh Nyata

Dengan input sebelumnya (V3 outlier di P4/P5, V4 outlier di P6), aggregator return:

```json
{
  "per_parameter_final": {
    "P1": {"final_score": 0.55, "trusted_voter": "CONSENSUS", "rationale": "4 voter agree bullish 0.4-0.7, avg 0.55"},
    "P4": {"final_score": 0.6, "trusted_voter": "V3", "rationale": "V3 catch Fed statement 2h ago yang shift momentum — konteks realtime trust V3"},
    "P5": {"final_score": 0.6, "trusted_voter": "V3", "rationale": "V3 identify upcoming CPI release Rabu — valid catalyst risk"},
    "P6": {"final_score": 0.55, "trusted_voter": "CONSENSUS", "rationale": "V4 flag crowded tapi reasoning lemah, konsensus lebih reliable"},
    ...
  },
  "final_score": 0.42,
  "overall_confidence": 0.72,
  "overall_consensus_level": "mixed",
  "vendor_alignment": "both_agree",
  "most_influential_parameter": "P4_momentum_direction",
  "justification": "Sentiment bullish disetujui semua voter (P1 avg 0.55), tapi momentum 48h shift bearish setelah Fed statement 2h ago (V3 catch, V1-V2 miss karena cuma baca headlines 7 hari). P5 risk catalysts (CPI Rabu) juga depress score. Final: GO HALF karena mix signal.",
  "disagreement_summary": [
    "V3 vs V1-V2 di P4/P5: V3 realtime context lebih trusted",
    "V4 contrarian signal di P6 tidak didukung voter lain"
  ]
}
```

##### Keunggulan Per-Parameter Analysis

| Feature | Benefit |
|---|---|
| **Granular trust** | Tiap parameter trust voter yang paling kompeten (bukan trust voter secara total) |
| **Outlier detection** | Outlier bukan otomatis dibuang — di-evaluate dulu reasoning-nya |
| **Audit trail** | Final score bisa di-trace per-parameter: "P4 push bearish karena Fed statement" |
| **ML-ready** | Dataset `(P1-P7 scores, voter, outcome)` bisa dipakai train model akurasi voter per parameter |
| **Debug friendly** | Kalau final decision salah, bisa identifikasi di parameter mana yang mis-scored |

#### 3.5.4a Ringkasan Validasi Per-Voter Produksi (Quick Reference)

| Voter | Input Utama | Horizon | Depth | Fokus Validasi | Kelebihan Unik |
|---|---|---|---|---|---|
| **V1 `glm-5.2-fast-preview`** | Headlines + OAK MCP Brief | 7 hari | Cepat | Keyword sentiment & polaritas | Latensi sangat rendah & hemat |
| **V2 `glm-5.3-prime`** | Headlines + OAK MCP (`get_crypto`/`get_tradfi`) | 7 hari | Tinggi | Alpha signal + narasi | #1 OAK Trading Benchmark (+29.4%, 0 liq) |
| **V3 `minimax-m3`** | Headlines + OAK MCP (`get_news`, `get_calendar`) | 24–48 jam | Medium | Risiko katalis & batas downside | #7 OAK Max DD terendah (-6.5%, 0 liq, biaya ultra-hemat) |
| **V4 `claude-haiku-5.5`** | Headlines + OAK MCP + wider context | 7–30 hari | Deep | Causal chain + contrarian check | Latensi 1.84s, 1M context, penalaran terstruktur |

**Kenapa setup produksi ini kuat dan hemat biaya?**
- Seluruh data valid, berita, kalender makro/earnings, dan metrik aset sudah dipasok langsung secara real-time oleh **OAK Research MCP Server** (`oak_mcp_client.py`), sehingga V3 & V4 tidak perlu mahal mencari web mentah dan bisa memakai model hemat berakurasi tinggi (`minimax-m3` & `claude-haiku-5.5`).
- CRO Meta-Aggregator (`glm-5.3` via DashScope) memakai model juara trading #1 OAK Benchmark untuk putusan final yang disiplin dan anti-likuidasi.

#### 3.5.5 Peran dalam 5 Agents (Fase 2 Full Multi-Agent)

Pada Fase 2, Swarm Voting ini jadi **NewsAgent** dalam sistem 5-agent lebih besar:

| Agent | Mekanisme | Bobot Agent |
|---|---|---|
| **FundingAgent** | Pure calc dari Walt funding rate history (no LLM) | 0.20 |
| **NewsAgent** | 4-Voter Swarm + `glm-5.3` CRO Aggregator + OAK MCP (section 3.5) | 0.20 |
| **FundamentalAgent** | `glm-5.3-prime` / `claude-haiku-5.5` analisa earnings & OAK `get_tradfi` | 0.20 |
| **TechnicalAgent** | RSI/MACD/SMA dari Yahoo OHLC + OAK Market Overview | 0.20 |
| **RegimeAgent** | `glm-5.2-fast-preview` klasifikasi macro regime | 0.20 |

Final decision = `Σ(agent.score × agent.confidence × 0.20)`.

#### 3.5.6 Biaya & Latency per Scan

| Config | Cost/scan | Latency | Vendor Diversity | Robustness |
|---|---|---|---|---|
| **2 Qwen + 2 Gemini + Qwen aggregator** ⭐ | ~$0.004-0.006 | ~4-5s | 50/50 balance | Sangat Tinggi |
| (Alt) Qwen 3x + Qwen aggregator | ~$0.002-0.003 | ~3-4s | 100% Alibaba | Medium |
| (Alt) Qwen + Gemini dual (current) | ~$0.002 | ~3s | 50/50 tapi no aggregator | Medium |
| (Alt) 1 model saja | ~$0.001 | ~2s | Single vendor | Rendah |

Delta biaya ~$0.004/scan × 100 scan/bulan = **$0.40/bulan**. Marginal cost, significant quality gain + eliminated vendor bias.

#### 3.5.7 Fallback Strategy
Jika salah satu voter gagal (API down / rate limit):
- **3 voter tetap bisa jadi dasar keputusan** dengan bobot re-normalized (33% × 3)
- Jika < 3 voter responsif → fallback dengan voter yang tersedia + DashScope GLM/Qwen atau `SINGLE_MODEL`
- Aggregator: `glm-5.3` $\to$ DashScope fallback (`glm-5.3-prime`, `glm-5.2-fast-preview`, `qwen-plus`, `qwen-flash`) $\to$ ClassAI (`claude-opus-5.5` $\to$ `gpt-6.1-sol`) $\to$ sintesis deterministik rule-based
- Log warning + Telegram alert ke owner + input health tracking di Step Summary

---

## 4. Fase 2: Multi-User Expansion, Sistem Bisnis & Dashboard Interaktif

Setelah strategi dan eksekusi pada Fase 1 terbukti andal dan menghasilkan imbal hasil konsisten bagi Owner, sistem dikembangkan ke **Fase 2** untuk melayani publik secara komersial.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        FASE 2: MULTI-TENANT SAAS ARCHITECTURE                          │
└────────────────────────────────────────────────────────────────────────────────────────┘

              Google Cloud Scheduler (Timezone Asia/Jakarta)
                                   │
                                   ▼
      ┌─────────────────────────────────────────────────────────┐
      │           Firebase Cloud Functions Gen 2 (Node.js 20)   │
      │                                                         │
      │   [Scheduled Cron]              [HTTPS Endpoints]       │
      │   • dailyScanScheduler          • telegramWebhook       │
      │   • riskMonitorScheduler        • paymentWebhook        │
      │   • marketExecuteScheduler      • waltOAuthCallback     │
      │   • subscriptionDunningJob      • apiMiniAppBackend     │
      │                                                         │
      │   ┌─────────────────────────────────────────────────┐   │
      │   │      Shared Multi-Agent AI Core (Zero Waste)    │   │
      │   │      - Market Regime Engine (VIX, DXY, SPY)     │   │
      │   │      - News Sentiment Swarm + OAK Research MCP:       │   │
      │   │         Voter 1: glm-5.2-fast-preview (DashScope)    │   │
      │   │         Voter 2: glm-5.3-prime        (DashScope #1) │   │
      │   │         Voter 3: minimax-m3           (ClassAI #7)   │   │
      │   │         Voter 4: claude-haiku-5.5     (ClassAI Fast) │   │
      │   │      - CRO Meta-Aggregator: `glm-5.3` (DashScope)    │   │
      │   │        * Output: score, confidence, justification│   │
      │   │        * Bukan averaging — reasoning-based      │   │
      │   │        * Vendor-neutral meta-level              │   │
      │   │      - 1x Call -> Dispatched to All Active Users│   │
      │   └────────────────────────┬────────────────────────┘   │
      │                            │ Sinyal Global              │
      │                            ▼                            │
      │   ┌─────────────────────────────────────────────────┐   │
      │   │        Multi-Tenant Execution Engine            │   │
      │   │  Iterasi: Hanya User Status Active Paid / Trial │   │
      │   │  - Ambil Kredensial & Dekripsi AES-256          │   │
      │   │  - Validasi Limit Risiko Per-User               │   │
      │   │  - Eksekusi API Walt Terisolasi                 │   │
      │   └──────┬──────────────────┬──────────────┬────────┘   │
      └──────────┼──────────────────┼──────────────┼────────────┘
                 │                  │              │
                 ▼                  ▼              ▼
      ┌──────────────────────┐ ┌──────────────┐ ┌──────────────┐
      │ Walt API (User A)    │ │Walt API (B)  │ │Walt API (Adm)│
      └──────────────────────┘ └──────────────┘ └──────────────┘
                 │                  │              │
                 ▼                  ▼              ▼
      ┌────────────────────────────────────────────────────────┐
      │             Firestore Multi-Tenant Database            │
      │  users/{uid} | trades/* | invoices/* | metrics/*       │
      └────────────────────────────┬───────────────────────────┘
                                   │
               ┌───────────────────┴───────────────────┐
               ▼                                       ▼
     ┌────────────────────────────┐      ┌────────────────────────────┐
     │ Telegram Mini App (TWA)    │      │ Telegram Bot Dispatcher    │
     │ Interactive Web Dashboard  │      │ & Interactive Menus        │
     │ • Real-time Charts & PnL   │      │ • Inline Buttons           │
     │ • Active Positions Card    │      │ • Quick Action Controls    │
     │ • Margin & Risk Sliders    │      │ • Instant Notifications    │
     │ • 1-Click Invoice Checkout │      │ • Owner Admin Console      │
     └─────────────┬──────────────┘      └─────────────┬──────────────┘
                   │                                   │
                   └─────────────────┬─────────────────┘
                                     │
                     ┌───────────────┴───────────────┐
                     ▼                               ▼
              [ User Device ]               [ Platform Owner ]
             (Subscriber Phone)            (Superadmin Console)
```

---

## 5. Sistem Bisnis & Monetisasi (Fase 2)

### 5.1 Struktur Paket Langganan (*Subscription Tiers*)

| Fitur | 🆓 Free Trial | ⚡ Standard Trader | 👑 Pro / VIP Trader |
|---|---|---|---|
| **Biaya Langganan** | **Rp 0** ($0) | **$15 / bulan** (~Rp 235.000) | **$35 / bulan** (~Rp 550.000) |
| **Opsi Tahunan** | - | **$150 / tahun** (Hemat $30) | **$350 / tahun** (Hemat $70) |
| **Durasi Akses** | 7 Hari Penuh | 30 Hari / 365 Hari | 30 Hari / 365 Hari |
| **Maksimal Posisi Simultan** | 1 Posisi | 3 Posisi | 6 Posisi |
| **Maksimal Leverage** | 1x (Delta-Neutral) | 2x | 2x (Dynamic Regime Adjust) |
| **AI Decision Engine** | Qwen-Plus (1 Call) | 3-Model Consensus | Full 5-Agent Consensus |
| **Akses Dashboard Interaktif**| Basic Mini App | Full Mini App + Live Chart | Full Mini App + Priority Alert |
| **Dukungan Pelanggan** | Komunitas Telegram | Standar (Response < 24 Jam) | Direct Priority Support |

### 5.2 Saluran Pembayaran (*Payment Channels*)
1. **Fiat Indonesia (Instant QRIS & Virtual Account):**
   - Integrasi Payment Gateway lokal (Midtrans / Xendit / Tripay).
   - Mendukung seluruh aplikasi perbankan dan e-wallet (BCA, Mandiri, BRI, GoPay, OVO, Dana, ShopeePay).
   - Settlement instan otomatis dalam hitungan detik via Webhook.
2. **Crypto Native (USDT & USDC):**
   - Mendukung jaringan berbiaya gas rendah: Arbitrum One, Base, Polygon (POL), atau Solana.
   - Integrasi gateway pembayaran kripto terverifikasi (NowPayments / CoinGate / Helio) dengan verifikasi hash on-chain otomatis.
3. **Telegram Stars (In-App Purchase):**
   - Pembayaran resmi langsung di dalam Telegram via Apple Pay / Google Pay tanpa perlu membuka aplikasi perbankan eksternal.

### 5.3 Siklus Tagihan, Masa Tenggang & Dunning Management
- **H-3 Sebelum Berakhir:** Bot mengirimkan pesan pengingat sopan di Telegram dan banner pengingat pada Dashboard Mini App.
- **H-1 Sebelum Berakhir:** Peringatan mendesak dengan tombol 1-klik untuk perpanjangan instan.
- **Hari H (Expired):** Status akun beralih ke `grace_period` (masa tenggang 48 jam). Bot memantau dan menjaga posisi yang ada, namun menolak pembukaan posisi baru.
- **H+2 (Masa Tenggang Habis):** Akun disuspensi (`suspended`). Bot trading dinonaktifkan sementara hingga user melakukan pembayaran faktur baru.

---

## 6. Dashboard Interaktif & Pengalaman Pengguna (Fase 2)

### 6.1 Telegram Mini App (TWA) Interactive Dashboard
Untuk memberikan pengalaman pengguna kelas institusi tanpa mengharuskan instalasi aplikasi terpisah, Fase 2 menghadirkan **Telegram Mini App (TWA)** yang dibuka langsung dari jendela obrolan Telegram.

```
┌────────────────────────────────────────────────────────┐
│  ⚡ Walt Perps AI — Interactive Dashboard (TWA)        │
├────────────────────────────────────────────────────────┤
│  Hai, Alex! 👋                [Status: Active Pro 👑] │
│                                                        │
│  📊 Total Saldo Walt      📈 Akumulasi Funding (Bln Ini)
│     $148.50 USDC              +$34.20 USDC (+23.0%)    │
│                                                        │
│  [ Grafik Performa PnL & Funding (7D / 30D / All) ]   │
│  ┌──────────────────────────────────────────────────┐  │
│  │     ╭───────╮     ╭─*                            │  │
│  │   ╭─╯       ╰─────╯                              │  │
│  │───┴──────────────────────────────────────────────│  │
│  └──────────────────────────────────────────────────┘  │
│                                                        │
│  🔥 Posisi Aktif (2 / 6)                               │
│  ┌──────────────────────────────────────────────────┐  │
│  │ NVDA-PERP [SHORT 2x]               +$4.80 PnL    │  │
│  │ Funding Rate: +0.038%/hr        [Tombol Tutup]   │  │
│  ├──────────────────────────────────────────────────┤  │
│  │ XAU-PERP  [SHORT 1.5x]             +$2.10 PnL    │  │
│  │ Funding Rate: +0.025%/hr        [Tombol Tutup]   │  │
│  └──────────────────────────────────────────────────┘  │
│                                                        │
│  ⚙️ Pengaturan Risiko Cepat                            │
│  • Alokasi Margin/Posisi: [━●━━━━━━━] $3.00 USDC       │
│  • Batas Leverage:        [●━━━━━━━━] 2x Max           │
│  • Mode Auto-Trade:       [ Toggle: ON 🟢 ]            │
│                                                        │
│  💳 Langganan: Berakhir 14 Jan 2027  [Perpanjang 1-Klik]│
└────────────────────────────────────────────────────────┘
```

#### Fitur Utama Dashboard Interaktif:
1. **Portfolio & Equity Overview:** Menampilkan saldo terkoneksi dari Walt, total margin terpakai, dan akumulasi perolehan *funding yield*.
2. **Chart Grafik Interaktif:** Visualisasi kurva pertumbuhan PnL bersih dan histori yield harian berbasis *lightweight mobile chart* (Chart.js / ApexCharts).
3. **Live Position Cards:** Setiap posisi terbuka ditampilkan dalam kartu visual lengkap dengan ukuran leverage, funding rate berjalan, estimasi PnL, dan tombol aksi manual (`Tutup Posisi`).
4. **Interactive Risk Sliders:** Slider interaktif untuk menyesuaikan alokasi margin per posisi ($2.50 – $10.00) dan batas leverage tanpa perlu mengetik teks perintah.
5. **Koneksi OAuth 1-Klik:** Indikator visual status koneksi Walt (`Terhubung 🟢` / `Perlu Re-Auth 🔴`) dengan tombol instan untuk menyambungkan ulang.
6. **Billing & Invoice Generator:** Antarmuka pemilihan paket langganan dan tampilan kode QRIS dinamis lengkap dengan waktu hitung mundur 60 menit.

### 6.2 Antarmuka Telegram Bot (Rich Inline Menus)
Selain Mini App, interaksi cepat tetap didukung via pesan bot berfitur tombol interaktif (*inline keyboards*):
- `/start` — Menampilkan kartu sambutan, status akun, dan tombol buka Dashboard Mini App.
- `/menu` — Menu utama interaktif untuk navigasi tanpa mengetik perintah manual.
- `/status` — Kartu ringkasan instan saldo, posisi, dan sisa masa aktif langganan.
- `/positions` — Daftar posisi terbuka disertai tombol aksi `[Tutup Posisi]`.
- `/subscribe` — Menampilkan opsi paket dan tombol pembayaran langsung.
- `/settings` — Konfigurasi preferensi risiko pribadi dan jam tenang notifikasi.

### 6.3 Platform Owner Admin Console (Fase 2)
Platform Owner memiliki akses eksklusif untuk memantau ekosistem secara menyeluruh:
- `/admin stats` — Ringkasan metrik: Total pengguna aktif, total volume trading yang diproses, estimasi MRR, dan konsumsi token AI.
- `/admin users` — Daftar seluruh subscriber, status paket, dan tanggal kadaluarsa.
- `/admin revenue` — Rincian faktur terbayar berdasarkan kanal pembayaran (QRIS, Crypto, Stars).
- `/admin broadcast <pesan>` — Mengirimkan pengumuman serentak ke seluruh pengguna terdaftar.
- `/admin halt_all` — **Emergency Kill-Switch**: Menghentikan pembukaan posisi baru untuk seluruh tenant jika terjadi anomali pasar global.

---

## 7. Skema Registrasi & Login Non-Custodial (Fase 2)

Sistem dirancang mengutamakan keamanan dan kemudahan (*zero-friction*), memadukan identitas Telegram dengan otorisasi non-custodial Walt OAuth 2.0 PKCE:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        USER ONBOARDING & OAUTH PKCE WORKFLOW                           │
└────────────────────────────────────────────────────────────────────────────────────────┘

 [User]                 [Telegram Bot]             [Firebase Functions]         [Walt Auth Server]
    │                          │                            │                            │
 1. │── /start ───────────────>│                            │                            │
    │                          │── Cek Telegram ID ────────>│                            │
    │                          │<─ Status: Belum Terdaftar ─│                            │
    │                          │                            │                            │
 2. │<─ Tampilkan Welcome, ────│                            │                            │
    │   TOS & Tombol [Daftar]  │                            │                            │
    │                          │                            │                            │
 3. │── Klik [Daftar Akun] ───>│                            │                            │
    │                          │── Buat User di Firestore ─>│                            │
    │                          │   Generate State Token ───>│                            │
    │                          │<─ Return Auth URL ─────────│                            │
    │                          │                            │                            │
 4. │<─ Kirim Link Otorisasi ──│                            │                            │
    │   "Hubungkan Akun Walt"  │                            │                            │
    │                          │                            │                            │
 5. │── Buka Link Otorisasi (Web / Mini App) ───────────────┼───────────────────────────>│
    │   User Login di Walt & Setujui Scope: [perps.trade]   │                            │
    │                                                       │<── Callback Code + State ──│
    │                                                       │                            │
 6. │                                                       │── Tukar Code dg Token ────>│
    │                                                       │<─ Return refresh_token ────│
    │                                                       │                            │
 7. │                                                       │── Enkripsi AES-256-GCM     │
    │                                                       │── Simpan ke Vault Firestore│
    │                                                       │── Aktifkan 7-Day Free Trial│
    │                                                       │                            │
 8. │<─ Notifikasi Telegram & Dashboard Update: ────────────│                            │
    │   "Akun Walt Berhasil Terhubung! 7-Hari Trial Aktif."                              │
```

---

## 8. Skema Basis Data Multi-Tenant (Fase 2 Firestore Models)

### 8.1 Koleksi Pengguna: `users/{userId}`
```typescript
interface UserDocument {
  userId: string;                   // UUID / Firestore Document ID
  telegramId: number;               // ID Unik Telegram Pengguna
  telegramUsername?: string;        // @username Telegram
  displayName: string;              // Nama profil
  role: 'user' | 'admin';           // 'admin' untuk Rizal Dicky
  status: 'pending_walt_link' | 'trial_active' | 'active_paid' | 'grace_period' | 'suspended' | 'banned';
  
  // Konfigurasi Risiko Pribadi
  settings: {
    maxActivePositions: number;     // e.g. 1 (Trial), 3 (Standard), 6 (Pro)
    marginPerPosition: number;      // $2.50 - $10.00 USDC
    maxLeverage: number;            // 1x - 2x
    allowDirectionalBias: boolean;  // Izinkan swing long/short
    quietHoursNotification: boolean;// Nonaktifkan notif 00:00 - 05:00 WIB
  };

  // Status Langganan
  subscription: {
    tier: 'trial' | 'standard' | 'pro';
    status: 'trial' | 'active' | 'grace' | 'expired';
    startedAt: FirebaseFirestore.Timestamp;
    currentPeriodEnd: FirebaseFirestore.Timestamp;
    gracePeriodEnd?: FirebaseFirestore.Timestamp;
    autoRenew: boolean;
  };

  createdAt: FirebaseFirestore.Timestamp;
  updatedAt: FirebaseFirestore.Timestamp;
}
```

### 8.2 Subkoleksi Vault Token: `users/{userId}/credentials/walt`
```typescript
interface UserCredentialDocument {
  encryptedRefreshToken: string;    // Hasil enkripsi AES-256-GCM
  iv: string;                       // Initialization Vector (Hex)
  authTag: string;                  // GCM Auth Tag (Hex)
  tokenExpiresAt: number;           // Unix timestamp kadaluarsa access_token
  scopes: string[];                 // ['perps.trade', 'perps.readonly']
  updatedAt: FirebaseFirestore.Timestamp;
}
```

### 8.3 Koleksi Invoice & Pembayaran: `invoices/{invoiceId}`
```typescript
interface InvoiceDocument {
  invoiceId: string;                // Format: INV-YYYYMM-XXXX
  userId: string;                   // Relasi ke users/{userId}
  telegramId: number;
  tier: 'standard' | 'pro';
  billingCycle: 'monthly' | 'yearly';
  amount: number;                   // Nominal pembayaran
  currency: 'IDR' | 'USDT' | 'STARS';
  paymentChannel: 'qris' | 'va_bca' | 'crypto_arbitrum' | 'crypto_base' | 'telegram_stars';
  
  status: 'PENDING' | 'PAID' | 'EXPIRED' | 'FAILED';
  paymentUrl?: string;              // URL checkout atau QR Code image
  externalReference?: string;       // ID transaksi payment gateway
  
  createdAt: FirebaseFirestore.Timestamp;
  expiresAt: FirebaseFirestore.Timestamp;  // Batas bayar 60 menit
  paidAt?: FirebaseFirestore.Timestamp;
}
```

### 8.4 Subkoleksi Riwayat Transaksi: `users/{userId}/trades/{tradeId}`
```typescript
interface TradeRecordDocument {
  tradeId: string;
  asset: string;                    // e.g. "NVDA-PERP", "XAU-PERP"
  side: 'LONG' | 'SHORT';
  entryPrice: number;
  exitPrice?: number;
  leverage: number;
  margin: number;
  fundingCaptured: number;          // Total funding didapatkan ($)
  netPnL?: number;                  // PnL bersih setelah biaya transaksi
  status: 'OPEN' | 'CLOSED';
  
  aiRationale: {
    regime: string;                 // "BULL_EXPANSION", "HIGH_VOLATILITY", dll.
    consensusScore: number;         // 0.0 - 1.0
    fundingRateHourly: number;
  };

  openedAt: FirebaseFirestore.Timestamp;
  closedAt?: FirebaseFirestore.Timestamp;
}
```

---

## 9. Functional Requirements (Kebutuhan Fungsional)

### 9.1 Kebutuhan Fungsional Fase 1 (Owner Core — Prioritas Segera)

| ID | Modul / Fungsi | Deskripsi Teknis | Status & Prioritas |
|---|---|---|---|
| **F1-01** | Daily Scheduled Scan | Eksekusi 07:17 & 19:17 WIB via `daily-scan.yml` memindai 31 aset whitelist (Saham AS saat `13:30–21:00 UTC`), OAK MCP, & Swarm. | **✅ Selesai (P0)** |
| **F1-02** | Automated Token Refresh | `token_manager.py` memperbarui Bearer token secara otomatis sebelum tiap eksekusi. | **✅ Selesai (P0)** |
| **F1-03** | OAuth Auto-Sync Secret | Auto-encrypt token baru dengan Libsodium (`pynacl`) & sync ke GitHub Secrets (Walt & OAK MCP). | **✅ Selesai (P0)** |
| **F1-04** | Multi-Vendor Swarm + OAK MCP | `glm-5.2-fast-preview`, `glm-5.3-prime`, `minimax-m3`, `claude-haiku-5.5` + CRO `glm-5.3` + OAK MCP (`Data Training`). | **✅ Selesai (P0)** |
| **F1-05** | 4-Tier Gear Market Exec | Eksekusi Gear 0–3 (`market-execute.yml`) dg filter jam Saham AS UTC (`13:30–21:00 UTC`) & Komoditas 24/7 + `'GO'` bypass E4. | **✅ Selesai (P0)** |
| **F1-06** | Continuous Risk Monitor | Eksekusi tiap 10 menit (`risk-monitor.yml`) dg 4 lapis proteksi & Smart Fast-Exit Idle <2 detik. | **✅ Selesai (P0)** |
| **F1-07** | Owner Telegram Alert | Laporan scan, eksekusi, PnL, dan SFT `Data Training` (Webhook v2 5 tab) ke Telegram & Google Sheets. | **✅ Selesai (P0)** |
| **F1-08** | Order Fallback & Anti-Dup | Fallback TP/SL terpisah bursa + filter defensif anti-duplicate posisi terbuka (`statuses=["open"]`). | **✅ Selesai (P0)** |
| **F1-09** | 24/7 Crypto Bluechip Exec | Eksekusi tiap 2 jam @ menit 11 (`crypto-execute.yml`) pada 10 koin Tier-1 dg 4-Tier Gear 0–3 via Cloud Scheduler. | **✅ Selesai (P0)** |

### 9.2 Kebutuhan Fungsional Fase 2 (Multi-User SaaS, Sistem Bisnis & Dashboard)

| ID | Modul / Fitur | Deskripsi Teknis | Prioritas |
|---|---|---|---|
| **F2-01** | Telegram User Onboarding | Registrasi otomatis akun baru saat user mengirim `/start` ke `@WaltPerpsBot`. | **P1 (Fase 2)** |
| **F2-02** | Non-Custodial Walt OAuth | Alur penyambungan akun via browser/TWA menggunakan standar OAuth 2.0 PKCE. | **P1 (Fase 2)** |
| **F2-03** | AES-256 Credential Vault | Enkripsi simetris token refresh tiap pengguna sebelum disimpan di Google Cloud Firestore. | **P1 (Fase 2)** |
| **F2-04** | Subscription Billing Engine | Perintah `/subscribe` & invoice generator multi-saluran (QRIS, Crypto, Stars). | **P1 (Fase 2)** |
| **F2-05** | Instant Webhook Settlement | Webhook listener dengan verifikasi HMAC signature & proteksi idempoten transaksi. | **P1 (Fase 2)** |
| **F2-06** | Automated Dunning Job | Cron harian 07:00 WIB untuk pengingat masa aktif (H-3, H-1), masa tenggang, dan suspend. | **P1 (Fase 2)** |
| **F2-07** | Interactive TWA Dashboard | Mini App visual untuk monitoring PnL, live charts, posisi aktif, dan slider margin/risiko. | **P1 (Fase 2)** |
| **F2-08** | Rich Bot Inline Menus | Keyboard interaktif Telegram untuk status cepat, daftar posisi, dan penutupan manual. | **P1 (Fase 2)** |
| **F2-09** | Shared AI Decision Core | 1 pemanggilan Qwen per siklus scan global; sinyal didistribusikan ke seluruh tenant aktif. | **P1 (Fase 2)** |
| **F2-10** | Tenant Isolation Engine | Kegagalan eksekusi order pada User A tidak boleh mengganggu eksekusi User B. | **P1 (Fase 2)** |
| **F2-11** | Owner Admin Console | Akses khusus Owner untuk ringkasan subscriber, metrik MRR, broadcast, & kill-switch. | **P1 (Fase 2)** |

---

## 10. Non-Functional Requirements (Keamanan & Keandalan)

1. **Non-Custodial Enforcement:** Sistem tidak pernah meminta, mengakses, atau menyimpan private key maupun seed phrase pengguna. Scope OAuth dibatasi hanya pada `perps.trade` dan `perps.readonly`. Penarikan aset (*withdrawal*) tetap hanya dapat dilakukan langsung oleh pengguna di situs resmi Walt.
2. **Keamanan Kredensial (Envelope Encryption):** Seluruh refresh token pengguna dienkripsi dengan algoritma standar perbankan AES-256-GCM. Kunci utama disimpan aman di Google Cloud KMS / Secret Manager.
3. **Efisiensi Biaya AI (Zero Waste):** Sinyal pasar dan analisis sentimen AI diproses secara tersentralisasi pada tingkat sistem (1 pemanggilan untuk seluruh pengguna aktif), bukan memanggil LLM berulang kali per pengguna. Pendekatan ini memangkas biaya API hingga 95%.
4. **Keandalan & Concurrency Control:** Eksekusi API Walt untuk multi-user dilakukan dengan antrian bertahap (*concurrency limit* maksimal 5 panggilan simultan per detik) untuk menghindari pemblokiran HTTP 429 *rate limit* dari Walt Relay API.

---

## 11. Tech Stack Terpadu (Fase 1 vs Fase 2)

| Layer | Fase 1 (Owner Core — Eksisting) | Fase 2 (Multi-User SaaS & Dashboard) |
|---|---|---|
| **Core Compute** | GitHub Actions Runner (Ubuntu Latest) | Firebase Cloud Functions Gen 2 / Cloud Run |
| **Language & SDK** | Python 3.12 (`httpx`, `apscheduler`) | TypeScript / Node.js 20 (Strict Mode) |
| **Database** | Stateless / Local JSON Cache / Env | Google Cloud Firestore (NoSQL Multi-Tenant) |
| **Scheduler** | GitHub Actions Cron Workflows | Google Cloud Scheduler + Pub/Sub |
| **Kredensial Vault** | GitHub Encrypted Secrets | Node.js `crypto` (AES-256-GCM) + Google Secret Manager |
| **Riset AI** | Alibaba Cloud Qwen-Plus (DashScope SDK) | Qwen Multi-Agent Consensus (Shared Engine) |
| **Dashboard / UI** | Notifikasi Telegram ke `TELEGRAM_CHAT_ID` | **Telegram Mini App (React + Vite + Tailwind)** |
| **Payment Gateway** | - (Belum diperlukan untuk Owner) | Midtrans / Xendit (QRIS/VA) & NowPayments (Crypto) |
| **Testing** | Manual testing & Python unittest | Vitest + MSW (Mock Service Worker) |

---

## 12. Roadmap & Rencana Rilis Bertahap

```
                      ROADMAP EVOLUSI WALT PERPS BOT
 
 ┌──────────────────────────────────────┐     ┌──────────────────────────────────────┐
 │ FASE 1: OWNER CORE BOT (SEGERA)      │     │ FASE 2: SAAS, BISNIS & DASHBOARD     │
 │ Durasi: Minggu 1 - Minggu 2          │────>│ Durasi: Bulan 1 - Bulan 2            │
 │                                      │     │                                      │
 │ • Verifikasi GitHub Secrets          │     │ • Setup Firebase Gen 2 & Firestore   │
 │ • Eksekusi live akun Walt Owner      │     │ • Migrasi OAuth PKCE & AES-256 Vault │
 │ • Pantau funding capture harian      │     │ • Sistem Billing (QRIS, Crypto, Stars│
 │ • Tuning risk buffer & TP/SL         │     │ • Bangun Telegram Mini App Dashboard │
 │ • Target: $50-$100/bln passive yield │     │ • Closed Beta (5-10 Users) & Launch  │
 └──────────────────────────────────────┘     └──────────────────────────────────────┘
```

### Detail Rencana Eksekusi:

- **Fase 1: Penerapan Segera untuk Owner (Minggu 1 – Minggu 2)**
  - Uji coba live eksekusi `daily-scan.yml` (06:39 WIB) dan `market-execute.yml` (21:40 WIB) dengan saldo trading riil Owner.
  - Verifikasi ketahanan mekanisme refresh token dan pemantauan akumulasi margin tiap 4 jam.
  - Memastikan pencapaian target return funding rate harian secara stabil dan aman.

- **Fase 2A: Pondasi Multi-Tenant & Sistem Bisnis (Bulan 1)**
  - Inisialisasi backend Firebase Cloud Functions Gen 2 & struktur Firestore multi-tenant.
  - Implementasi alur registrasi Telegram `/start` dan otorisasi Walt OAuth PKCE.
  - Integrasi gateway pembayaran QRIS (Midtrans/Xendit) dan Crypto (NowPayments) beserta webhook verifikasi otomatis.
  - Implementasi dunning cycle (H-3, H-1, Grace Period 48 jam, Auto-suspend).

- **Fase 2B: Dashboard Interaktif & Peluncuran SaaS (Bulan 2)**
  - Pembangunan antarmuka **Telegram Mini App (TWA)**: Chart visual PnL, kartu monitor posisi aktif, slider pengaturan margin/risiko, dan tombol pembayaran 1-klik.
  - Pengembangan rich inline menu pada Telegram Bot dan Admin Console khusus Platform Owner.
  - Implementasi *Shared AI Core* untuk mendistribusikan sinyal harian ke seluruh user aktif.
  - Peluncuran *Closed Beta* (5–10 pengguna perdana / early adopters) dilanjutkan dengan *Public Commercial Launch*.

---

## 13. Key Performance Indicators (KPIs) & Acceptance Criteria

### 13.1 Kriteria Keberhasilan Fase 1 (Owner Core — Segera Diterapkan)
- [x] Workflow GitHub Actions (`daily-scan.yml`, `risk-monitor.yml`, `market-execute.yml`, `crypto-execute.yml`) berjalan otomatis sesuai jadwal tanpa intervensi manual.
- [x] Bot berhasil membuka dan menutup posisi funding rate arbitrage di akun Walt milik Owner dengan leverage maksimal 2x.
- [x] Notifikasi harian, rincian perolehan funding rate, dan alert margin terkirim akurat ke Telegram pribadi Owner.
- [x] Mekanisme pembaruan token di `token_manager.py` berjalan mulus tanpa kegagalan otentikasi HTTP 401 dan auto-sync ke GitHub Secrets.
- [x] Dynamic Slot Elasticity (5 s.d 15 slot) & Conviction-Based Margin Sizing aktif mengelola akumulasi saldo secara autonomous.
- [x] Menghasilkan pendapatan pasif stabil dengan target $50–$100/bulan dari akun pribadi Owner.

### 13.2 Kriteria Keberhasilan Fase 2 (Multi-User SaaS, Bisnis & Dashboard)
- [ ] Pengguna baru dapat mendaftar mandiri via Telegram `/start`, membaca TOS, dan menyambungkan akun Walt via OAuth PKCE non-custodial.
- [ ] Kredensial refresh token setiap pengguna tersimpan dengan enkripsi AES-256-GCM.
- [ ] Pengguna dapat mengakses **Telegram Mini App Dashboard** interaktif untuk melihat grafik PnL, posisi aktif, dan mengatur alokasi margin secara real-time.
- [ ] Transaksi pembayaran paket (QRIS, Crypto, Stars) terverifikasi instan via webhook dan otomatis memperpanjang masa aktif akun.
- [ ] Job harian Cloud Scheduler mengeksekusi bot secara terisolasi hanya untuk pengguna berstatus langganan aktif (`active_paid` atau `trial_active`).
- [ ] Platform Owner memiliki kendali penuh melalui `/admin` untuk melihat analitik MRR, daftar pengguna, dan *emergency kill-switch*.
- [ ] Target bisnis komersial tercapai: >20 pengguna aktif berbayar dan MRR >$300/bulan dalam 3 bulan setelah rilis publik.

---

## 14. Dokumen Rujukan & Referensi Teknis
- **Walt Perps Relay API:** `https://walletbot.me/bot-relay/public-api`
- **Walt OAuth 2.0 PKCE Specification:** RFC 7591 & RFC 7636
- **Telegram Mini Apps (TWA) Documentation:** `https://core.telegram.org/bots/webapps`
- **Telegram Bot API & Payments:** `https://core.telegram.org/bots/payments`
- **Firebase Functions Gen 2:** `https://firebase.google.com/docs/functions`
- **Alibaba Cloud DashScope API (Qwen):** `https://dashscope-intl.aliyuncs.com`
- **Strategi Trading E2E:** [STRATEGY.md](file:///c:/Users/Rizal%20Dicky/.qoderwork/walt-trading/STRATEGY.md)
