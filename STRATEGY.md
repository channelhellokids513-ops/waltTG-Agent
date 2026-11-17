# Strategi Walt Perps Funding Arbitrage — E2E

**Pemilik:** Rizal Dicky  
**Dibuat:** 13 November 2026  
**Terakhir Diperbarui:** 15 November 2026 (Swarm AI 4-Voter P1-P7 & Qwen-Max Aggregator Architecture)  
**Platform Eksekusi:** Walt Perps Relay API (`walletbot.me`) via GitHub Actions Autonomous Runner  
**AI Research Engine:** 4-Voter Swarm (2 Qwen + 2 Gemini, Vendor Balance 50/50) + Meta-Aggregator Qwen-Max  
**Notifikasi:** Telegram Bot API (`notifier.py` → `TELEGRAM_CHAT_ID`)  
**Target:** $100/bulan dari funding income untuk cover tagihan rutin  

---

## 1. Thesis Strategi

Memanfaatkan **funding rate ekstrem** pada platform perpetual futures Walt sebagai sumber pendapatan pasif (*passive yield*), dengan mengambil posisi di sisi yang **dibayar oleh pasar**:

- **SHORT** pada kontrak dengan funding rate positif tinggi (posisi Long membayar posisi Short)
- **LONG** pada kontrak dengan funding rate negatif tinggi (posisi Short membayar posisi Long)

Funding rate ekstrem mencerminkan ketidakseimbangan posisi pasar yang terlalu padat (*crowded positioning*). Bot bertindak sebagai *liquidity/counterparty provider* untuk sisi yang crowded tersebut, menerima aliran pembayaran funding reguler (per jam) dengan proteksi leverage rendah dan manajemen risiko dinamis.

Risiko utama adalah pergerakan harga aset yang bergerak melawan arah posisi (*directional risk*), yang dimitigasi secara berlapis menggunakan leverage konservatif (1x–2x), filter sentimen AI Qwen, dan sistem *Risk Monitor* 4-lapis.

---

## 2. Pemilihan Aset (Universe & Exclusion Rules)

### 2.1 Filter Likuiditas Universe
- Total ~127 kontrak perpetual di platform Walt.
- **Filter Likuiditas Wajib:** Volume 24 jam > **$1.000.000 USD** (hanya aset likuid dengan spread ketat, rata-rata ~48 kontrak lolos filter awal).

### 2.2 Fokus Aset (Strict Inclusions)
**Hanya saham US terkemuka dan komoditas tradisional** karena memiliki underlying fundamental yang jelas dan jam likuiditas terprediksi:
- **Saham US (Equities & ETF):**  
  `NVDA`, `NBIS`, `CRCL`, `MSTR`, `AAPL`, `GOOGL`, `AMD`, `PLTR`, `COIN`, `SNDK`, `SPY`, `US100`, `AMZN`, `TSLA`, `META`, `LIT`, `VVV`, `ANTHROPIC`
- **Komoditas Tradisional:**  
  `XAU` (Emas), `XAG` (Perak), `BRENTOIL` (Minyak Mentah Brent)
- **Crypto Bluechip (Likuiditas Tinggi & Siklus Funding 8 Jam):**  
  `BTC` (Bitcoin), `ETH` (Ethereum), `SOL` (Solana)

### 2.3 Aturan Eksklusi Ketat (Strict Altcoin Exclusion)
- ❌ **GRAM dan Altcoin/Memecoin Liar Dilarang Keras:**  
  Kontrak `GRAM` dan token spekulatif bervolatilitas liar **DIHAPUS & DI-BLACKLIST SECARA PERMANEN**. Universe crypto dibatasi ketat hanya pada aset bluechip likuiditas raksasa (`BTC`, `ETH`, `SOL`) untuk memanen yield funding rate tanpa risiko rugi akibat manipulasi likuiditas tipis.

### 2.4 Formula Ranking Peluang
Setiap pagi pukul **06:39 WIB**, annualized funding rate dihitung otomatis oleh bot:
```
funding_daily = funding_rate_hourly × 24
funding_annualized = funding_daily × 365 × 100
```
- Kontrak dengan `funding_daily > +0.5%/hari` dirangking sebagai kandidat **SHORT**.
- Kontrak dengan `funding_daily < -0.5%/hari` dirangking sebagai kandidat **LONG**.
- Tiga peluang terbaik dengan nilai absolut funding rate tertinggi diprioritaskan untuk riset AI dan eksekusi.

---

## 3. Position Sizing & Money Management

### 3.1 Parameter Posisi
- **Leverage Maksimal:** **2x** (konservatif, estimasi pergerakan harga menuju likuidasi ~49%).
- **Margin per Posisi:** **$2.50 – $3.00 USDC**.
- **Notional Exposure per Posisi:** **$5.00 – $6.00 USDC** (pada leverage 2x).
- **Jumlah Posisi Simultan:** **2 – 4 posisi** (tergantung saldo tersedia).
- **Buffer Modal Wajib:** Minimal **15% – 20%** dari saldo akun harus selalu disisakan dalam bentuk saldo bebas (USDC) untuk menjaga ketahanan margin.

### 3.2 Margin Minimum Efektif di Walt (Leverage 2x)
Setiap kontrak di Walt memiliki ketentuan *minimum order size (min_size_base)*. Berikut adalah acuan margin minimum efektif untuk aset prioritas pada leverage 2x:

| Kontrak | Kategori | Min Margin Efektif @ 2x |
|---|---|---|
| **NBIS** | Saham AI Cloud | ~$1.65 USDC |
| **NVDA** | Saham Semikonduktor | ~$2.12 USDC |
| **CRCL** | Saham Fintech/Stablecoin | ~$2.20 USDC |
| **PLTR** | Saham AI Enterprise | ~$2.45 USDC |
| **XAU**  | Komoditas Emas | ~$2.97 USDC |
| **XAG**  | Komoditas Perak | ~$2.50 USDC |

---

## 4. Entry Rules & Mekanisme Eksekusi

### 4.1 Syarat Pembukaan Posisi
1. **Funding Rate:** Nilai absolut funding rate harian `|funding_daily| ≥ 0.5%/hari` (annualized ≥ 182%/tahun).
2. **Likuiditas:** Volume 24 jam > $1.000.000 USD.
3. **Saldo:** Saldo tersedia memenuhi margin minimum + alokasi buffer wajib (minimal $3.00 USDC tersedia).
4. **Waktu Eksekusi (Saham & Komoditas):** Berjalan 24 Jam x 7 Hari tanpa henti (14:43 & 21:43 WIB Setiap Hari, plus auto-eksekusi instan saat scan 07:17 WIB).
5. **No Duplicate:** Belum ada posisi aktif yang terbuka pada kontrak/aset yang sama.
6. **AI Research Confirmation:** Lolos verifikasi model Qwen (skor keputusan **GO** atau **HALF**).

### 4.2 Alur Eksekusi API Walt & Minimum Order Sizing
Walt mewajibkan pola 2-tahap dengan *time-limited confirmation token*:
1. `perps_prepare_order_place` — Meminta pratinjau order dengan parameter: `contractId`, `side`, `type: "market"`, `leverage: 2`, `amount`, `price`, dan `autoClose` (TP/SL).
2. **Validasi & Clamping Minimum Order Size (`min_size_base`):**
   - Bursa Walt mensyaratkan: $\text{Derived Size} = (\text{amount} \times \text{leverage} / \text{price}) \ge \text{min\_size\_base}$.
   - Pada aset berharga tinggi (misal `NVDA` @ $140, `min_size_base = 0.025 NVDA`), keputusan `HALF` size ($1.25 margin) menghasilkan ukuran order di bawah batas bursa ($0.0178$ NVDA) dan ditolak bursa.
   - **Solusi Clamping Otomatis:** Bot menghitung margin minimum bursa:
     $$\text{margin}_{\min} = \frac{\text{min\_size\_base} \times \text{price}}{\text{leverage}} \times (1 + \text{slippage}) + 0.02$$
     Jika `margin < margin_min` dan masih di bawah toleransi risiko portofolio ($\le \text{DEFAULT\_MARGIN} \times 1.5$), margin otomatis dinaikkan ke batas minimum kontrak (misal $1.79 untuk NVDA). Jika melebihi batas risiko, posisi diskip dengan alasan transparan.
3. Verifikasi respon: Memastikan `ok = true` dan parameter risiko disetujui.
4. `perps_execute_operation` — Mengirimkan `confirmationToken` (berlaku ~90 detik) untuk konfirmasi eksekusi order di bursa.
5. **Fallback TP/SL Otomatis:** Jika bursa menolak order awal karena batasan parameter TP/SL di luar toleransi slippage kontrak, bot mengeksekusi order terbuka tanpa `autoClose`, kemudian mengatur TP/SL secara terpisah via `perps_prepare_auto_close_set`.

### 4.3 Parameter TP/SL Baseline Dinamis per Kelas Aset
Menghindari target TP kaku yang sering berbalik sebelum tersentuh (target tipis & high turnover):
- **ETF / Saham Kalem (SPY, US100, AAPL):** TP +2.5%, SL 3.0%
- **Crypto Bluechip (BTC, ETH, SOL):** TP +2.5%, SL 3.5% (cepat amankan sebelum dump)
- **Komoditas Tradisional (XAU, XAG, BRENTOIL):** TP +3.0%, SL 3.5%
- **Saham Volatil (NVDA, TSLA, MSTR, CRCL, dll):** TP +3.5%, SL 4.0%
- **Windfall Rule:** Jika keuntungan harga mencapai $\ge 2.5\times$ funding rate harian, TP diturunkan lebih awal agar akumulasi funding multi-hari segera terkunci tanpa risiko pembalikan harga.

---

## 5. Dual-AI Collaboration Layer (Qwen-Plus + Google Gemini)

Khusus untuk instrumen saham US, sebelum order dieksekusi, bot menjalankan lapisan analisis fundamental dan sentimen menggunakan arsitektur **4-Voter Swarm AI + Qwen-Max Aggregator** (sesuai PRD 3.5) yang memadukan 4 model kecerdasan buatan dengan keseimbangan vendor 50/50:
1. **V1: Alibaba Qwen-Turbo** — *Quick Signal Analyst* (deteksi kata kunci dan polaritas permukaan).
2. **V2: Alibaba Qwen-Plus** — *Balanced Equity Analyst* (kredibilitas sumber berita dan konsistensi narasi).
3. **V3: Google Gemini-Flash-Latest** — *Realtime Market Validator* (cek breaking news 24 jam dan katalis terkini).
4. **V4: Google Gemini-Pro-Latest** — *Deep Multi-Angle Reasoner* (analisis kausalitas, contrarian crowdedness, dan pola historis).

Keempat voter menilai **7 Parameter Standar Validasi (P1-P7)** yang sama secara independen:
- **P1 `sentiment_polarity` (Weight: 0.25):** Nada sentimen 7 hari (-1.0 bearish ekstrem hingga +1.0 bullish ekstrem).
- **P2 `signal_strength` (Weight: 0.10):** Tingkat keselarasan arah berbagai headline (0.0 divergen hingga 1.0 konsensus penuh).
- **P3 `source_credibility` (Weight: 0.10):** Bobot kualitas reputasi sumber (Tier 1 seperti Reuters/WSJ vs media umum).
- **P4 `momentum_direction` (Weight: 0.20):** Arah momentum sentimen 48 jam terakhir (-1.0 reversal hingga +1.0 akseleratif).
- **P5 `risk_catalysts` (Weight: 0.15):** Event risiko mendatang seperti rilis earnings, FOMC, atau regulasi (0.0 aman hingga 1.0 event major <7 hari).
- **P6 `contrarian_check` (Weight: 0.10):** Posisi pasar terlalu padat/crowded (0.0 sehat hingga 1.0 sangat crowded berisiko reversal).
- **P7 `historical_pattern` (Weight: 0.10):** Analogi terhadap preseden pola historis pasar serupa (-1.0 negatif hingga +1.0 positif).

### 5.1 Pre-Processing Varians & Meta-Aggregator (Qwen-Max)
1. **Analisis Varians Parameter (Rule-Based):**
   - Menghitung rata-rata (*mean*) dan varians untuk setiap parameter P1–P7.
   - Mengklasifikasikan level konsensus: `UNANIMOUS` (<0.05), `STRONG` (<0.15), `MIXED` (<0.30), atau `SPLIT` (≥0.30).
   - Mendeteksi voter outlier bila varians > 0.15.
2. **Meta-Aggregator (Qwen-Max):**
   - Menganalisis alasan outlier dan bobot per-parameter secara objektif tanpa bias vendor.
   - Menghasilkan `final_score` terbobot, `overall_confidence`, `overall_consensus_level`, dan `vendor_alignment`.
   - Mengaktifkan proteksi bila terjadi `SPLIT` konsensus atau divergensi tajam antara kubu Qwen dan Gemini.
3. **Fallback Synthesis:**
   - Bila salah satu voter mengalami rate limit atau gangguan koneksi, sistem tetap berjalan dengan voter yang aktif secara re-normalized.
   - Jika Meta-Aggregator offline, sistem mengeksekusi rumus sintesis deterministik berbasis bobot P1–P7.

### 5.2 Decision Matrix (Keputusan Eksekusi Swarm AI)

| Sinyal Funding | Status Konsensus Swarm | Skor Final & Arah | Risk Catalyst (P5) | Keputusan Bot | Aksi & Margin |
|---|---|---|---|---|---|
| Tinggi (≥0.5%/hari) | **UNANIMOUS / STRONG** | Aligned dg posisi | P5 < 0.70 | ✅ **GO (Full Size)** | Buka posisi full ($2.50 – $3.00 USDC) |
| Tinggi (≥0.5%/hari) | **MIXED** | Aligned dg posisi | P5 < 0.70 | ⚠️ **HALF** | Buka posisi separuh ($1.25 – $1.50 USDC) |
| Tinggi (≥0.5%/hari) | Model Manapun | Aligned dg posisi | **P5 ≥ 0.70** (Katalis Tinggi) | ⚠️ **HALF / SKIP** | Proteksi event katalis: batas ukuran posisi |
| Tinggi (≥0.5%/hari) | **SPLIT / Vendor Conflict** | Netral / Terpecah | Berapapun | ⚠️ **HALF** | Proteksi divergensi sinyal AI: setengah posisi |
| Tinggi (≥0.5%/hari) | Model Manapun | Crowded (**P6 ≥ 0.85**) | Berapapun | ⚠️ **HALF** | Proteksi contrarian crowded positioning |
| Tinggi (≥0.5%/hari) | Model Manapun | Melawan arah posisi | Berapapun | ❌ **SKIP** | Batalkan eksekusi, hindari melawan tren |
| Berapapun | Model Manapun | Berapapun | **Earnings ≤ 7 hari** | ❌ **SKIP** | Batalkan eksekusi (hindari risiko earnings) |
| Rendah (<0.5%/hari)| Model Manapun | Berapapun | Berapapun | ❌ **SKIP** | Batalkan eksekusi (funding rate kurang menarik) |

*Aturan Alignment:*
- `SHORT` + sentimen `bearish` = **Confirm** (berita bearish mendukung posisi Short).
- `LONG` + sentimen `bullish` = **Confirm** (berita bullish mendukung posisi Long).
- `SHORT` + sentimen `bullish` = **Melawan Arah** → **SKIP**.
- `LONG` + sentimen `bearish` = **Melawan Arah** → **SKIP**.
- Instrumen komoditas (`XAU`, `XAG`, `BRENTOIL`) langsung diproses tanpa kewajiban filter earnings saham.

---

## 6. Risk Management Dinamis — 4 Lapis Pengaman

Manajemen risiko berjalan otomatis tiap 4 jam melalui workflow `risk-monitor.yml`:

```
┌────────────────────────────────────────────────────────────────────────┐
│                   4-LAYER RISK MANAGEMENT PIPELINE                     │
└────────────────────────────────────────────────────────────────────────┘

  [ Posisi Terbuka ]
          │
          ▼
  [ LAPIS 2: FUNDING RATE INTEGRITY (Prioritas Tertinggi) ]
  • Funding berubah arah (flip sign)? ──────────────> [ TUTUP POSISI SEGERA ]
  • Funding meluruh tajam (<0.1%/hari)? ────────────> [ Geser SL ke Breakeven ]
          │ (Thesis funding masih valid)
          ▼
  [ LAPIS 1: BASELINE PROTECTION ]
  • Belum ada TP/SL? ───────────────────────────────> [ Set SL 7% & TP 15% ]
          │
          ▼
  [ LAPIS 4: PROGRESSIVE TRAILING STOP ]
  • Profit ≥ 3% ────────────────────────────────────> [ Kunci SL di Breakeven ]
  • Profit ≥ 5% ────────────────────────────────────> [ Trailing SL 2.5% di belakang ]
  • Profit ≥ 10% ───────────────────────────────────> [ Trailing SL 1.5% di belakang ]
  • Profit ≥ 20% ───────────────────────────────────> [ Trailing SL 1.0% di belakang ]
          │
          ▼
  [ LAPIS 3: VOLATILITY & ADVERSE MOMENTUM ]
  • Pergerakan harga melawan posisi > 3% ───────────> [ Perketat SL ke Entry ± 5% ]
  • Pergerakan harga searah posisi > 5% ────────────> [ Kunci SL ke Breakeven + 1% ]
```

### Penjelasan Detil Tiap Lapis:

1. **Lapis 1 (Dynamic Baseline TP/SL — Ultra-Fast Scalp & High Turnover):**  
   Target TP dan SL dibuat ultra-tipis agar perputaran modal $15 sangat tinggi dan segera cair:
   - Crypto Tier-1 (BTC, ETH, SOL, BNB, XRP, AVAX, SUI, LINK, NEAR, AAVE): TP +1.0%, SL 2.5%
   - ETF / Indeks (SPY, US100, AAPL): TP +1.0%, SL 2.5%
   - Komoditas (XAU, XAG, BRENTOIL): TP +1.2%, SL 3.0%
   - Saham Volatil (NVDA, TSLA, MSTR, CRCL, SNDK, VVV): TP +1.2%, SL 3.0%
   - *Windfall Rule:* Jika keuntungan harga $\ge 2\times$ funding harian (>=0.8%), TP dimajukan lebih cepat agar cuan segera terkunci.
2. **Lapis 2 (Dinamika Funding Rate — Prioritas Utama):**  
   - Jika arah funding berbalik (*funding flip sign*), misal posisi SHORT namun funding rate berubah menjadi negatif (artinya posisi kita yang harus membayar pasar), **posisi seketika ditutup pasar**. Thesis awal dinyatakan batal.  
   - Jika funding rate meluruh tajam hingga `< 0.1%/hari`, stop loss otomatis dipindahkan ke harga entry (*breakeven*).
3. **Lapis 3 (Volatilitas & Momentum Harga):**  
   - Jika posisi merugi > 2.0% dari entry price, SL diperketat menjadi 2.5% untuk memangkas potensi drawdown lebih lanjut.
4. **Lapis 4 (Trailing Stop & Early Breakeven Lock — Ultra-Fast Scalp):**  
   Mencegah posisi yang sempat hijau berbalik menjadi rugi:
   - **Tier 1 (Profit $\ge +0.6\%$):** Geser SL otomatis ke **Entry + 0.20%** (Zero-Risk: kunci modal dan cover fee 0.18% sejak dini!).
   - **Tier 2 (Profit $\ge +1.0\%$):** Geser SL ke **Entry + 0.50%** (Kunci Cuan Awal 0.5%).
   - **Tier 3 (Profit $\ge +1.5\%$):** Geser SL ke **Entry + 1.00%** (Kunci Cuan Menengah 1.0%).
   - **Tier 4 (Profit $\ge +2.0\%$):** Pasang trailing stop super rapat **0.5%** di bawah harga running.

---

## 7. Arsitektur Deployment & Jadwal Otomasi GitHub Actions

Sistem beroperasi 100% secara otonom di cloud GitHub Actions runners dengan nol biaya server:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        LIVE DEPLOYMENT ARCHITECTURE                                    │
└────────────────────────────────────────────────────────────────────────────────────────┘

    GitHub & Google Cloud Scheduler (Precision Triggers)
    ├── daily-scan.yml      (07:17 & 19:17 WIB / 2x sehari) → Master Scan 127 kontrak + Swarm AI + Sheet
    ├── risk-monitor.yml    (Tiap 1 jam di menit 23 WIB)     → Evaluasi 4-lapis risiko & Early Breakeven (+0.6%)
    ├── market-execute.yml  (Tiap 2 jam di menit 43 WIB)     → Eksekusi Saham US & Komoditas (Prioritas Sheet Cache)
    └── crypto-execute.yml  (Tiap 2 jam di menit 11 WIB)     → Eksekusi Crypto Tier-1 Scalp Turn-over (24/7)
                   │
                   ▼
     ┌───────────────────────────┐
     │   GitHub Actions Runner   │
     │   (Ubuntu Latest / Py 3.12│
     └─────────────┬─────────────┘
                   │
      [Injeksi Secrets & Environment]
      • WALT_CLIENT_ID & WALT_REFRESH_TOKEN
      • DASHSCOPE_API_KEY & DASHSCOPE_MODEL
      • TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID
      • GH_PAT (Untuk rotasi secret otomatis)
                   │
                   ▼
     ┌───────────────────────────┐
     │        walt_bot.py        │
     │  - token_manager.py       │ ──> Auto-refresh Bearer token & auto-sync Secret
     │  - llm_research.py        │ ──> Qwen-Plus financial reasoning & sentiment
     │  - Walt MCP Relay API     │ ──> Eksekusi order & data market Walt Perps
     │  - notifier.py            │ ──> Notifikasi laporan terstruktur Telegram
     └─────────────┬─────────────┘
                   │
                   ▼
     ┌───────────────────────────┐
     │   Platform Owner Phone    │
     │   Telegram Instant Alert  │
     └───────────────────────────┘
```

### 7.1 Jadwal Workflow & Google Cloud Scheduler Bridge

Sistem beroperasi dengan arsitektur hibrida: cron internal GitHub Actions didukung oleh **Google Cloud Scheduler Bridge** (Region `asia-southeast2` Jakarta, Project `gen-lang-client-0705791867`) yang menembak REST API `workflow_dispatch` dengan akurasi detik waktu Indonesia Barat (WIB):

| Workflow File | Jadwal Scheduler (WIB) | Jadwal Cron (UTC) | Target Cloud Scheduler | Fungsi Utama |
|---|---|---|---|---|
| [daily-scan.yml](file:///.github/workflows/daily-scan.yml) | **07:17 & 19:17 WIB** (2x Sehari) | `17 0,12 * * *` | `walt-daily-scan` | Master Scan seluruh kontrak, ranking peluang funding harian, riset sentimen Swarm AI (Qwen+Gemini), eksekusi instan sinyal GO/HALF, simpan database ke Google Sheet. |
| [risk-monitor.yml](file:///.github/workflows/risk-monitor.yml) | **Tiap 1 Jam di menit 23** | `23 * * * *` | `walt-risk-monitor` | Evaluasi 4 lapis risiko, geser SL breakeven di +0.60% (Zero-Risk), trailing stop tier 1–4, dan *emergency close* jika funding berbalik. |
| [market-execute.yml](file:///.github/workflows/market-execute.yml) | **Tiap 2 Jam di menit 43** (24/7) | `43 */2 * * *` | `walt-market-execute` | Eksekusi cepat posisi Saham US & Komoditas 24/7. Mengutamakan riset tervalidasi dari Google Sheet (hemat API) dengan proteksi *anti-duplicate*. |
| [crypto-execute.yml](file:///.github/workflows/crypto-execute.yml) | **Tiap 2 Jam di menit 11** (24/7) | `11 */2 * * *` | `walt-crypto-execute` | Eksekusi cepat posisi Crypto Bluechip (10 Tier-1) menangkap funding carry & perputaran modal cepat. |

### 7.2 Otomasi Rotasi Token OAuth (`token_manager.py`)
- Akses ke Walt API menggunakan protokol OAuth 2.0 PKCE dengan Bearer token yang memiliki masa berlaku terbatas.
- `token_manager.py` memeriksa masa aktif token secara otomatis dan melakukan refresh ke endpoint OAuth Walt sebelum menjalankan operasi trading.
- **Auto-Sync GitHub Secret:** Jika Walt merotasi `refresh_token`, sistem mengenkripsi token baru menggunakan libsodium (`pynacl`) dan memperbarui secret repository GitHub secara otomatis menggunakan `GH_PAT`. Hal ini memastikan bot beroperasi tanpa risiko terhenti karena token kadaluarsa.

### 7.3 Kanal Notifikasi & Laporan (Telegram 3x Auto-Retry)
- **Telegram Push Alert:** Notifikasi lengkap dikirimkan ke Telegram pribadi Owner (`TELEGRAM_CHAT_ID`) memuat status saldo, posisi aktif, akumulasi funding yang diterima, keputusan AI, dan aksi pengubahan stop loss.
- **Fault-Tolerant Exponential Backoff:** `notifier.py` dilengkapi sistem 3x percobaan ulang (*retry*) dengan jeda eksponensial untuk mengantisipasi gangguan jaringan sementara (`Connection reset by peer`).
- **GitHub Step Summary:** Setiap eksekusi mencatat audit log terformat rapi pada antarmuka *Actions Step Summary* di repository GitHub.

### 7.4 Sinkronisasi Real-Time Google Sheets (`sheet_logger.py`)
- Setiap hasil scan dan eksekusi order secara otomatis dikirimkan ke spreadsheet Google Sheets pribadi Owner melalui Google Apps Script Webhook (`google_apps_script.js`).
- Spreadsheet mencatat riwayat funding accrual, PnL transaksi, status order ID, dan saldo harian secara permanen untuk audit keuangan bulanan tanpa biaya database tambahan.

---

## 8. Target Finansial & Roadmap Skalabilitas

### 8.1 Target Finansial Owner
- **Target Bulanan:** **$100.00 USDC / bulan** dari pendapatan funding rate.
- **Estimasi Modal Minimum:** $300 – $500 USDC dengan rata-rata *funding yield* 0.6%/hari (kombinasi 3–4 posisi).
- **Struktur Biaya Transaksi:** Fee taker Walt sebesar 0.09% per sisi (ditutupi oleh akumulasi funding dalam waktu ~3–5 jam pertama).

### 8.2 Tahapan Pertumbuhan (Scaling Roadmap)

1. **Fase 1: Validasi Operasional Live (Minggu 1 – Minggu 2)**
   - Modal aktif: $20 – $50 USDC.
   - Posisi: 2–3 posisi mikro ($2.50 margin per posisi).
   - Tujuan: Memverifikasi akurasi funding accrual di bursa, ketepatan trailing stop, dan keandalan webhook/cron GitHub Actions.
2. **Fase 2: Stabilisasi Yield Target (Bulan 1 – Bulan 2)**
   - Modal aktif: $150 – $300 USDC.
   - Posisi: 3–4 posisi terdiversifikasi lintas sektor (Semikonduktor, Komoditas, Fintech).
   - Target yield: $30 – $60 USDC per bulan.
3. **Fase 3: Skalabilitas Komersial Multi-User (Fase 2 PRD)**
   - Transisi ke arsitektur multi-tenant SaaS sesuai spesifikasi [PRD.md](file:///PRD.md).
   - Pembagian sinyal trading terpusat kepada para pelanggan (*subscribers*) dengan sistem langganan bulanan dan dashboard interaktif Telegram Mini App.

---

## 9. Ringkasan File & Komponen Terkait

- [`walt_bot.py`](file:///walt_bot.py) — Core bot orchestrator (scan, research, market execute, crypto execute, 4-layer risk monitor, order size clamping).
- [`token_manager.py`](file:///token_manager.py) — Otomasi refresh OAuth token dan sinkronisasi secret GitHub via PyNaCl.
- [`llm_research.py`](file:///llm_research.py) — Client Dual-AI Swarm (Alibaba Qwen + Google Gemini) 4-voter consensus & 7-parameter scoring.
- [`notifier.py`](file:///notifier.py) — Pengirim pesan notifikasi format Markdown ke Telegram Bot API dengan 3x exponential backoff retry.
- [`sheet_logger.py`](file:///sheet_logger.py) — Modul pengirim telemetry eksekusi dan scan ke Google Sheets Webhook.
- [`google_apps_script.js`](file:///google_apps_script.js) — Skrip Google Apps Script endpoint penampung data spreadsheet.
- [`.github/workflows/`](file:///.github/workflows/) — Definisi cron scheduler cloud runners (daily scan, risk monitor, market execute, crypto execute).
- [PRD.md](file:///PRD.md) — Dokumen spesifikasi kebutuhan produk untuk Roadmap Evolusi SaaS Fase 1 & Fase 2.
