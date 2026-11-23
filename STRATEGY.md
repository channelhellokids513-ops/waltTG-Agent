# Strategi Walt Perps Funding Arbitrage — E2E

**Pemilik:** Rizal Dicky  
**Dibuat:** 13 November 2026  
**Terakhir Diperbarui:** 22 November 2026 (4-Voter Swarm GLM-5.3/MiniMax M3/Haiku 5.5 + GLM-5.3 CRO + OAK Research MCP + 4-Tier Gear 0–3 + SFT `Data Training`)  
**Platform Eksekusi:** Walt Perps Relay API (`walletbot.me`) via GitHub Actions Autonomous Runner  
**AI Research Engine:** 4-Voter Multi-Vendor Swarm (`glm-5.2-fast-preview`, `glm-5.3-prime`, `minimax-m3`, `claude-haiku-5.5`) + CRO Meta-Aggregator `glm-5.3` + OAK Research Remote MCP Server (`oak_mcp_client.py`)  
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
- **Crypto Bluechip Tier-1 (10 Aset Whitelist, 24/7):**  
  `BTC`, `ETH`, `SOL`, `BNB`, `XRP`, `AVAX`, `SUI`, `LINK`, `NEAR`, `AAVE`

### 2.3 Aturan Eksklusi Ketat (Strict Altcoin Exclusion)
- ❌ **GRAM dan Altcoin/Memecoin Liar Dilarang Keras:**  
  Kontrak `GRAM`, memecoin volatil (`PEPE`, `DOGE`, `SHIB`, `WIF`, `FARTCOIN`, `POPCAT`, `TRUMP`), dan token berlikuiditas tipis **DI-BLACKLIST SECARA PERMANEN**. Universe crypto dibatasi ketat hanya pada 10 aset bluechip/large-cap likuid (`ALLOWED_CRYPTO`) untuk memanen yield funding rate tanpa risiko rugi akibat manipulasi likuiditas tipis.

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

### 3.1 Parameter Posisi & Dynamic Auto-Compounding
- **Leverage Maksimal:** **2x** (konservatif, estimasi pergerakan harga menuju likuidasi ~49%).
- **Dynamic Auto-Compounding (Snowballing):**
  - Bot secara otomatis mengumpulkan seluruh profit Take Profit & yield funding rate yang masuk ke kas dompet untuk ditradingkan kembali (*auto-reinvest*) selama belum di-withdraw oleh user.
  - Alokasi ukuran margin per posisi diskalakan secara dinamis: `base_margin = deployable / remaining_slots` dengan `deployable = available_balance * 85%`.
  - Margin otomatis membesar seiring pertumbuhan modal dan mengecil secara proporsional jika dilakukan penarikan dana (*withdraw*).
- **Dynamic Slot Elasticity (Slot Posisi Fleksibel):**
  - Tidak dibatasi kaku ke 5 slot. Jumlah slot posisi aktif beradaptasi elastis mengikuti saldo kas (`get_dynamic_max_slots`):
    - **Modal Mikro (< $30 USDC):** **5 slot** (konservatif & aman dari margin call, margin ~$2.50/posisi).
    - **Modal Menengah ($30 – $75 USDC):** **5 – 8 slot** (margin ~$5.00 – $7.50/posisi).
    - **Modal Berkembang ($75 – $150 USDC):** **8 – 12 slot** (margin ~$7.50 – $12.00/posisi).
    - **Modal Mapan ($150+ USDC):** hingga **15 slot maksimal** (diversifikasi luas lintas saham US, komoditas, dan crypto bluechip).
- **Conviction-Based Margin Sizing (Skala Margin Berbasis Keyakinan):**
  - Margin posisi disesuaikan secara individual menggunakan sistem pembobotan (*multiplier*):
    1. **Funding Rate Magnitude:** Funding $\ge 0.8\%$/hari $\to \mathbf{1.35\times}$; Funding $\ge 0.4\%$/hari $\to \mathbf{1.15\times}$; Normal $\to \mathbf{1.00\times}$.
    2. **AI Swarm Consensus:** UNANIMOUS / Skor $\ge +0.60 \to \mathbf{1.25\times}$; STRONG $\to \mathbf{1.00\times}$; MIXED / SPLIT $\to \mathbf{0.75\times}$.
    3. **Asset Safety Tier:** Safe-Haven / Liquid Bluechip (`BTC`, `ETH`, `SPY`, `XAU`, `QQQ`, `AAPL`) $\to \mathbf{1.15\times}$; Altcoins / Growth $\to \mathbf{1.00\times}$.
    4. **Verdict Multiplier:** GO $\to \mathbf{1.00\times}$; HALF $\to \mathbf{0.50\times}$.
- **Portfolio Risk Ceiling & Floor:**
  - **Single-Position Hard Ceiling:** Maksimal **15%** dari total ekuitas saldo akun per posisi agar tidak terjadi over-konsentrasi modal pada satu aset.
  - **Batas Bawah Margin (Floor):** **$2.50 USDC** (syarat minimum order exchange Walt Perps).
  - **Cadangan Kas Wajib (Cash Buffer):** **15%** dari saldo kas selalu dijaga bebas demi ketahanan likuiditas.

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
3. **Saldo:** Saldo tersedia memenuhi margin minimum + alokasi buffer wajib (minimal $3.00 USDC tersedia, menjaga 15% cash buffer).
4. **Filter Jam Bursa Saham AS (`US_STOCK_HOURS_ONLY=true`) vs 24/7 Komoditas & Crypto:**  
   - Saham US (`ALLOWED_STOCKS`) hanya dipindai riset AI & dieksekusi saat **jam bursa reguler NYSE/NASDAQ (`Senin–Jumat 13:30–21:00 UTC`)**.
   - Komoditas (`XAU`, `XAG`, `BRENTOIL`) dan Crypto Bluechip (10 aset Tier-1) tetap berjalan **24/7**. Workflow `risk-monitor.yml` tetap memantau posisi terbuka seluruh aset 24/7.
5. **No Duplicate & 3h Cooldown:** Belum ada posisi aktif pada kontrak yang sama dan aset tidak sedang dalam jeda cooldown 3 jam pasca-close.
6. **AI Research Confirmation:** Lolos verifikasi Swarm AI + OAK Research MCP (`GO` atau `HALF`). Verdict **`GO` otomatis melewati batas frekuensi 24 jam (`E4`)** (`2/aset/24j` & `8/hari`), sedangkan verdict `HALF` tetap dibatasi. Circuit breaker `-5%`, cooldown 3 jam, batas slot, dan cadangan kas 15% tetap berlaku mutlak untuk semua verdict.

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

### 4.3 Parameter TP/SL Baseline Dinamis per Kelas Aset (True Funding Carry & Asymmetric R:R)
Memberi ruang napas bagi harga agar tidak terpicu *noise* jangka pendek sekaligus memastikan *Risk-to-Reward* positif setelah dikurangi biaya taker `0.18%` round-trip + spread:
- **ETF / Saham Kalem (SPY, US100, AAPL):** TP +2.0%, SL 1.5%
- **Crypto Bluechip (BTC, ETH, SOL, dll):** TP +2.0%, SL 1.5%
- **Komoditas Tradisional (XAU, XAG, BRENTOIL):** TP +2.5%, SL 1.5%
- **Saham Volatil (NVDA, TSLA, MSTR, CRCL, dll):** TP +2.5%, SL 1.5%
- **Windfall Rule:** Jika $2.5\times$ funding rate harian berada di rentang $1.5\% \le \text{windfall} < \text{TP baseline}$, TP dimajukan ke level tersebut di server bursa.

---

## 5. Multi-Vendor AI Collaboration Layer (OAK MCP + GLM-5.3 + MiniMax M3 + Claude Haiku 5.5)

Sebelum order dieksekusi, bot memperkaya data pasar real-time dari **OAK Research Remote MCP Server** (`oak_mcp_client.py` $\to$ `https://oakresearch.io/api/mcp`) lalu menjalankan analisis **4-Voter Multi-Vendor Swarm + CRO Meta-Aggregator (`glm-5.3`)** yang dirancang berdasarkan hasil **OAK Trading Benchmark**:
- **Pre-Research MCP Data (`[DATA REAL-TIME OAK RESEARCH MCP]`):** Menyuntikkan *global market overview* (`get_market_overview`), kalender makro/earnings/token-unlock 7 hari (`get_calendar`), berita utama (`get_news`), serta metrik fundamental/on-chain spesifik aset (`get_crypto` / `get_tradfi`) lewat OAuth 2.1 PKCE dengan rotasi token otomatis.
1. **V1 (`glm-5.2-fast-preview` via DashScope `DASHSCOPE_API_KEY`):** *Fast Validator* — deteksi kata kunci dan polaritas permukaan berbiaya rendah (dapat dioverride lewat `DASHSCOPE_MODEL_V1`).
2. **V2 (`glm-5.3-prime` via DashScope `DASHSCOPE_API_KEY`):** *Alpha Signal Analyst* — varian #1 OAK Trading Benchmark (+29.4%, 0 likuidasi) untuk kredibilitas sumber dan konsistensi narasi (override: `DASHSCOPE_MODEL`).
3. **V3 (`minimax-m3` via ClassAI Router `DASHSCOPE_API_GEMINI_KEY`):** *Conservative Risk Guard* — peringkat #7 OAK dengan *Max Drawdown* terendah (`-6.5%`, 0 likuidasi, hemat biaya), berfokus pada risiko katalis dan proteksi modal (override: `GEMINI_MODEL`).
4. **V4 (`claude-haiku-5.5` via ClassAI Router `DASHSCOPE_API_GEMINI_KEY`):** *Fast Multi-Angle Reasoner* — latensi ~1.84s dengan konteks 1M token untuk menganalisis kausalitas, posisi *crowded*, dan preseden historis (override: `GEMINI_MODEL_V4`).
*(Catatan: Apabila `MULTI_OPP_GLM_FALLBACK=true` dan jumlah peluang `> 1`, V3/V4 dapat dialihkan sementara ke GLM DashScope; default produksi `false` agar keragaman 3-vendor tetap aktif sepanjang waktu.)*

Keempat voter menilai **7 Parameter Standar Validasi (P1-P7)** yang sama secara independen:
- **P1 `sentiment_polarity` (Weight: 0.25):** Nada sentimen 7 hari (-1.0 bearish ekstrem hingga +1.0 bullish ekstrem).
- **P2 `signal_strength` (Weight: 0.10):** Tingkat keselarasan arah berbagai headline (0.0 divergen hingga 1.0 konsensus penuh).
- **P3 `source_credibility` (Weight: 0.10):** Bobot kualitas reputasi sumber (Tier 1 seperti Reuters/WSJ vs media umum).
- **P4 `momentum_direction` (Weight: 0.20):** Arah momentum sentimen 48 jam terakhir (-1.0 reversal hingga +1.0 akseleratif).
- **P5 `risk_catalysts` (Weight: 0.15):** Event risiko mendatang seperti rilis earnings, FOMC, atau regulasi (0.0 aman hingga 1.0 event major <7 hari).
- **P6 `contrarian_check` (Weight: 0.10):** Posisi pasar terlalu padat/crowded (0.0 sehat hingga 1.0 sangat crowded berisiko reversal).
- **P7 `historical_pattern` (Weight: 0.10):** Analogi terhadap preseden pola historis pasar serupa (-1.0 negatif hingga +1.0 positif).

### 5.1 Pre-Processing Varians, CRO Meta-Aggregator (`glm-5.3`), & SFT Dataset Hook
1. **Analisis Varians Parameter (Rule-Based):**
   - Menghitung rata-rata (*mean*) dan varians untuk setiap parameter P1–P7.
   - Mengklasifikasikan level konsensus: `UNANIMOUS` (<0.05), `STRONG` (<0.15), `MIXED` (<0.30), atau `SPLIT` (≥0.30).
   - Mendeteksi voter outlier bila varians > 0.15.
2. **Meta-Aggregator CRO (`glm-5.3` via DashScope `DASHSCOPE_API_KEY`):**
   - Pemenang #1 **OAK Trading Benchmark** (`glm-5.3`, override `DASHSCOPE_MODEL_AGG`) bertindak sebagai **Chief Risk Officer (CRO)** yang menyintesiskan skor berbobot P1–P7, data OAK Research MCP, dan deteksi outlier.
   - Menghasilkan `final_score` terbobot, `overall_confidence`, `overall_consensus_level`, dan `vendor_alignment`.
   - Mengaktifkan proteksi bila terjadi `SPLIT` konsensus atau divergensi tajam antar-vendor.
3. **Rantai Fallback Berlapis & SFT Training Pipeline (`training_data.py`):**
   - Bila `glm-5.3` mengalami kendala sementara, aggregator otomatis fallback ke rantai DashScope (`glm-5.3-prime` $\to$ `glm-5.2-fast-preview` $\to$ `qwen-plus` $\to$ `qwen-flash`), lalu ke ClassAI Router (`claude-opus-5.5` via `CLASSAI_MODEL_AGG` $\to$ `gpt-6.1-sol` via `CLASSAI_MODEL_AGG_FALLBACK`), hingga akhirnya sintesis deterministik berbasis bobot P1–P7.
   - Setiap output riset AI tervalidasi otomatis disimpan dalam format chat-messages `<think>...</think>` + JSON ke Google Sheet tab **`Data Training`** (`12` kolom, deduped oleh `Sample ID`, target $\ge 1.000$ baris) dan `logs/training_dataset.jsonl` untuk fine-tuning model Fase 2.

### 5.2 Decision Matrix (Keputusan Eksekusi Swarm AI)

| Sinyal Funding | Status Konsensus Swarm | Skor Final & Arah | Risk Catalyst (P5) | Keputusan Bot | Aksi & Margin |
|---|---|---|---|---|---|
| Tinggi (≥0.5%/hari) | **UNANIMOUS / STRONG** | Aligned dg posisi | P5 < 0.70 | ✅ **GO (Full Size)** | Buka posisi full ($2.50 – $3.00 USDC) |
| Tinggi (≥0.5%/hari) | **MIXED** | Aligned dg posisi | P5 < 0.70 | ⚠️ **HALF** | Buka separuh *hanya jika* hasil $\ge \$2.50$; jika $< \$2.50 \to$ **SKIP** (*HALF di bawah floor exchange*) |
| Tinggi (≥0.5%/hari) | Model Manapun | Aligned dg posisi | **P5 ≥ 0.70** (`max_p5_risk`) | ❌ **SKIP** | Katalis risiko tinggi → batalkan entry |
| Tinggi (≥0.5%/hari) | **SPLIT / Vendor Conflict** | Netral / Terpecah | Berapapun | ⚠️ **HALF** | Setengah ukuran (atau **SKIP** jika di bawah floor `$2.50`) |
| Tinggi (≥0.5%/hari) | Model Manapun | Crowded (**P6 ≥ 0.85**) | Berapapun | ⚠️ **HALF** | Proteksi contrarian (atau **SKIP** jika di bawah floor `$2.50`) |
| Tinggi (≥0.5%/hari) | Model Manapun | Melawan arah posisi | Berapapun | ❌ **SKIP** | Batalkan eksekusi, hindari melawan tren |
| Berapapun | Riset Basi (> 6 jam) | — | — | ❌ **SKIP** | Abaikan riset basi (`max_research_age_hours`); saham tanpa riset segar di-**SKIP** |
| Berapapun | Model Manapun | Berapapun | **Earnings ≤ 7 hari** | ❌ **SKIP** | Batalkan eksekusi (hindari risiko earnings) |
| Rendah (<0.5%/hari)| Model Manapun | Berapapun | Berapapun | ❌ **SKIP** | Batalkan eksekusi (funding rate kurang menarik) |

*Aturan Gerbang Kualitas Entry & Ekonomi Tambahan (E1–E5):*
- **HALF di Bawah Floor (`E1`):** Jika verdict `HALF` menghasilkan margin `< $2.50`, bot otomatis **SKIP** (`HALF di bawah floor exchange`), tidak lagi dinaikkan paksa ke `$2.50`.
- **Batas Frekuensi 24 Jam (`E4`):** Maksimal **2 entry/aset/24 jam** (`max_entries_per_asset_24h`) dan **8 entry total/24 jam** (`max_entries_per_day`) untuk verdict `HALF` (dilacak di `logs/market_regime.json`), sedangkan **verdict `'GO'` otomatis melewati batas frekuensi 24 jam ini**.
- **Circuit Breaker Harian UTC (`E4`):** Jika ekuitas turun $\ge 5\%$ (`daily_loss_limit_pct`) dari ekuitas awal hari UTC, seluruh entry baru dihentikan hingga `00:00 UTC` berikutnya disertai alert Telegram (risk monitor tetap berjalan normal).
- **Gerbang Ekonomi Funding vs Fee (`E5`):** Entry hanya lolos jika `expected_funding = |funding_daily| × (expected_hold_hours / 24)` memenuhi `expected_funding >= (2 × 0.0009 + price_slippage) × 1.5` (`expected_hold_hours` default `24` jam). Laporan `stats` juga memantau *median hold time* dan memberi peringatan jika jauh di bawah target *carry*.

*Aturan Alignment:*
- `SHORT` + sentimen `bearish` = **Confirm** (berita bearish mendukung posisi Short).
- `LONG` + sentimen `bullish` = **Confirm** (berita bullish mendukung posisi Long).
- `SHORT` + sentimen `bullish` = **Melawan Arah** → **SKIP**.
- `LONG` + sentimen `bearish` = **Melawan Arah** → **SKIP**.
- Instrumen komoditas (`XAU`, `XAG`, `BRENTOIL`) langsung diproses tanpa kewajiban filter earnings saham.

---

## 6. Risk Management Dinamis — 4 Lapis Pengaman

Manajemen risiko berjalan otomatis secara adaptif (dinamis 5–10 menit dengan *Smart Fast-Exit Idle* saat 0 posisi) melalui workflow `risk-monitor.yml`:

```
┌────────────────────────────────────────────────────────────────────────┐
│                   4-LAYER RISK MANAGEMENT PIPELINE                     │
└────────────────────────────────────────────────────────────────────────┘

  [ Posisi Terbuka ]
          │
          ▼
  [ LAPIS 2: FUNDING RATE INTEGRITY (Hysteresis 0.1%/hari) ]
  • Funding flip sign & |funding| ≥ 0.1%/hari? ─────> [ TUTUP POSISI SEGERA ]
  • Funding flip < 0.1%/hari atau meluruh? ─────────> [ Geser SL ke Breakeven ]
          │ (Thesis funding masih valid)
          ▼
  [ LAPIS 1: ASYMMETRIC R:R BASELINE PROTECTION ]
  • Belum ada TP/SL? ───────────────────────────────> [ Set SL 1.5% & TP 2.0%/2.5% ]
  • Posisi lama melewati batas SL 1.5%? ────────────> [ Tutup (SL 1.5% migrasi) ]
          │
          ▼
  [ LAPIS 4: TRUE ZERO-RISK TRAILING STOP ]
  • Profit ≥ +0.80% ────────────────────────────────> [ Lock SL Entry + 0.35% ]
  • Profit ≥ +1.40% ────────────────────────────────> [ Lock SL Entry + 0.85% ]
  • Profit ≥ +2.00% ────────────────────────────────> [ Trailing SL 0.60% ]
          │
          ▼
  [ LAPIS 3: VOLATILITY CUT — DEPRECATED ]
  • Digantikan sepenuhnya oleh SL baseline ketat 1.5% + 3h Cooldown pasca-close
```

### Penjelasan Detil Tiap Lapis:

1. **Lapis 1 (Asymmetric R:R Baseline TP & Uniform 1.5% SL):**  
   Target TP asimetris (+2.0% s/d +2.5%) dipadukan dengan Stop Loss seragam **1.5%** di seluruh kelas aset:
   - ETF / Indeks (`SPY`, `US100`, `AAPL`): TP +2.0%, SL 1.5%
   - Crypto Tier-1 (`BTC`, `ETH`, `SOL`, `BNB`, `XRP`, `AVAX`, `SUI`, `LINK`, `NEAR`, `AAVE`): TP +2.0%, SL 1.5%
   - Komoditas (`XAU`, `XAG`, `BRENTOIL`): TP +2.5%, SL 1.5%
   - Saham Volatil (`NVDA`, `TSLA`, `MSTR`, `CRCL`, `SNDK`, `VVV`): TP +2.5%, SL 1.5%
   - *Windfall Rule:* Jika $2.5\times$ funding harian berada di $1.5\% \le \text{windfall} < \text{TP baseline}$, TP dimajukan ke level tersebut.
   - *Guard Sisi Salah & Migrasi:* Memvalidasi level SL/TP terhadap `current_price` sebelum mengirim `auto_close`. Posisi lama yang sudah melewati batas rugi 1.5% langsung ditutup (`SL 1.5% breached (migrasi)`).
2. **Lapis 2 (Dinamika Funding Rate dengan Hysteresis 0.1%/hari):**  
   - **Hysteresis (`FUNDING_FLIP_CLOSE_THRESHOLD = 0.001`):** Jika arah funding berbalik (*funding flip sign*) DAN `abs(funding_daily) >= 0.1%/hari`, **posisi seketika ditutup pasar**.
   - Jika funding berbalik arah tetapi masih `< 0.1%/hari`, atau meluruh di bawah `< 0.1%/hari`, stop loss digeser ke harga entry (*breakeven*).
3. **Lapis 3 (Volatilitas & Momentum Harga — Deprecated):**  
   - *Deprecated*: Digantikan sepenuhnya oleh SL baseline 1.5% (tidak ada kode yang melonggarkan SL di atas 1.5%).
4. **Lapis 4 (True Zero-Risk Trailing Stop & 3-Hour Cooldown):**  
   Mengunci surplus bersih di atas biaya transaksi (`0.18%` taker round-trip + spread) tanpa memotong profit terlalu dini:
   - **Tier 1 (Profit $\ge +0.80\%$):** Geser SL otomatis ke **Entry + 0.35%** (`lock_0035` — True Zero-Risk Lock setelah fee + spread).
   - **Tier 2 (Profit $\ge +1.40\%$):** Geser SL ke **Entry + 0.85%** (`lock_0085`).
   - **Tier 3 (Profit $\ge +2.00\%$):** Aktifkan Trailing Stop dinamis **0.60%** di bawah puncak harga berjalan.
   - **3-Hour Re-Entry Cooldown:** Setelah posisi ditutup (`_record_position_close` atau `_detect_and_log_exchange_closes`), aset dikunci selama 3 jam di `logs/market_regime.json` sebelum boleh dibuka ulang.

### 6.1 Kebijakan Menahan Posisi Saat Pasar Tutup (*Hybrid Cost-Aware Off-Hours Policy*, Opsi B)

Di Walt Perps, biaya atau pendapatan inap berasal dari `funding_rate_hourly` yang berjalan 24/7. Karena durasi bursa AS hanya **7,5 jam/hari** dan biaya *round-trip* tutup-lalu-buka mencapai **`0.28%–0.45%` notional** (`2 × 0.09%` taker + slippage min `0.10%`), menutup paksa setiap posisi setiap malam akan menimbulkan *fee bleed*. Sebaliknya, menahan posisi yang membayar biaya inap atau lemah menjelang libur akhir pekan berisiko tergerus carry negatif dan *opening gap*. Karena itu (`OFF_HOURS_HOLD_POLICY=hybrid`), berlaku aturan berikut pada `risk-monitor`:

- **Cakupan Aset:** **Saham AS (`ALLOWED_STOCKS`)** pada *overnight* (Sen–Kam `21:00–13:30 UTC`) & *weekend* (Jum `21:00`–Sen `13:30 UTC`), serta **Komoditas (`XAU`, `XAG`, `BRENTOIL`)** khusus **akhir pekan** (Jum `21:00`–Min `22:00 UTC`). **Crypto Tier-1 tetap berjalan 24/7.**
- **Zero-Tolerance Biaya Inap:** Begitu *funding* berubah menjadi beban biaya inap (`favorable_funding < 0%`, berapa pun nilainya) pada sesi *pre-close* maupun saat pasar tutup, posisi ditutup seketika tanpa menunggu ambang hysteresis `0.10%/hari`.
- **Early Profit Lock (`>= +0.40%`):** Pada seluruh sesi *pre-close* & tutup pasar, jika profit harga mencapai `+0.40%` (sebelum Tier 1 `+0.80%`), SL otomatis dikunci lebih dini ke **`Entry +/- 0.25%`** (cukup menutup fee `0.18%` + buffer).
- **Pre-Close Overnight (`20:35–21:00 UTC`, Sen–Kam, Saham AS):** Tetap ditahan jika *carry* searah `>= +0.20%/hari` atau posisi sudah terkunci profit (`SL >= Entry + 0.25%`); jika *carry* lemah `< +0.20%/hari` dan belum terkunci profit, posisi ditutup sebelum penutupan pasar.
- **Pre-Close Weekend (`20:35–21:00 UTC`, Jum, Saham AS & Komoditas):** Posisi hanya diizinkan menginap melewati libur akhir pekan bila **sudah terkunci profit** ATAU *carry* searah kuat **`>= +0.50%/hari`** dengan floating PnL masih aman (`>= -0.50%`). Selain itu posisi ditutup aman menjelang akhir pekan.

---

## 7. Arsitektur Deployment & Jadwal Otomasi GitHub Actions

Sistem beroperasi 100% secara otonom di cloud GitHub Actions runners dengan nol biaya server:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        LIVE DEPLOYMENT ARCHITECTURE                                    │
└────────────────────────────────────────────────────────────────────────────────────────┘

    GitHub & Google Cloud Scheduler (Precision Triggers)
    ├── daily-scan.yml      (07:17 & 19:17 WIB / 2x sehari) → Master Scan + OAK MCP + Swarm AI + Sheet
    ├── risk-monitor.yml    (Tiap 10 menit WIB)              → Pengawas 4-lapis risiko intensif + Smart Fast-Exit Idle
    ├── market-execute.yml  (Tiap 2 jam di menit 43 WIB)     → Saham US (13:30–21:00 UTC) & Komoditas 24/7 (Gear 0–3)
    └── crypto-execute.yml  (Tiap 2 jam di menit 11 WIB)     → Eksekusi Crypto Tier-1 (10 aset 24/7, Gear 0–3)
                   │
                   ▼
     ┌───────────────────────────┐
     │   GitHub Actions Runner   │
     │   (Ubuntu Latest / Py 3.14│
     └─────────────┬─────────────┘
                   │
      [Injeksi Secrets & Environment]
      • WALT_CLIENT_ID & WALT_REFRESH_TOKEN
      • OAK_MCP_CLIENT_ID & OAK_MCP_REFRESH_TOKEN
      • DASHSCOPE_API_KEY & DASHSCOPE_API_GEMINI_KEY
      • TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID
      • GH_PAT (Untuk rotasi secret otomatis Walt & OAK MCP)
                   │
                   ▼
     ┌───────────────────────────┐
     │        walt_bot.py        │
     │  - token_manager.py       │ ──> Auto-refresh Bearer token & auto-sync Secret
     │  - oak_mcp_client.py      │ ──> Live macro, calendar, news & metric OAK MCP
     │  - llm_research.py        │ ──> GLM-5.2/5.3 + MiniMax M3 + Haiku 5.5 + GLM-5.3 CRO
     │  - training_data.py       │ ──> Simpan <think> SFT JSONL ke tab Data Training
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
| [daily-scan.yml](file:///.github/workflows/daily-scan.yml) | **07:17 & 19:17 WIB** (2x Sehari) | `17 0,12 * * *` | `walt-daily-scan` | Master Scan 31 aset diizinkan (Saham US hanya di jam reguler AS `13:30–21:00 UTC`), pengayaan OAK Research MCP, Swarm AI (`GLM-5.3` CRO), eksekusi GO/HALF, simpan 5 tab Google Sheets (`Daily_Scans` 30 kolom & `Data Training`). |
| [risk-monitor.yml](file:///.github/workflows/risk-monitor.yml) | **Dinamis 5–10 Menit** (6x–12x/Jam + Smart Idle) | `*/10 * * * *` | `walt-risk-monitor` | Evaluasi risiko adaptif 24/7 (True Zero-Risk Trailing & Asymmetric R:R). Fast-exit <2 detik saat 0 posisi; otomatis mempercepat ke **tiap ~5 menit (12x/jam)** jika $\ge 3$ posisi aktif, kategori `HALF`, atau dekat TP ($\ge +0.70\%$) / SL ($\le -1.00\%$); dan stabil **10 menit** untuk posisi `GO` tenang. |
| [market-execute.yml](file:///.github/workflows/market-execute.yml) | **4-Tier Gear 0–3** (Dinamis) | `43 */2 * * *` | `walt-market-execute` | Eksekusi Saham US (`13:30–21:00 UTC`) & Komoditas (24/7) dengan **4-Tier Execution Gear**: **Gear 0 (`0` GO)** = maks 2x selama jam bursa AS (`225m`); **Gear 1 (`< 2` GO)** = `120m` (2 jam); **Gear 2 (`2..5` GO)** = `60m` (1 jam); **Gear 3 (`> 5` GO / `GEAR_3_FULL_RESEARCH=true`)** = `60m` + izin riset AI penuh untuk setiap keputusan trading. |
| [crypto-execute.yml](file:///.github/workflows/crypto-execute.yml) | **4-Tier Gear 0–3** (Dinamis) | `11 */2 * * *` | `walt-crypto-execute` | Eksekusi mandiri Crypto Bluechip (10 Tier-1, 24/7) memakai **4-Tier Execution Gear** (`Gear 0` `720m` / 2x sehari; `Gear 1` `120m`; `Gear 2` `60m`; `Gear 3` `60m` Full AI Research) + *3-hour re-entry cooldown*. |

### 7.2 Otomasi Rotasi Token OAuth (`token_manager.py`)
- Akses ke Walt API menggunakan protokol OAuth 2.0 PKCE dengan Bearer token yang memiliki masa berlaku terbatas.
- `token_manager.py` memeriksa masa aktif token secara otomatis dan melakukan refresh ke endpoint OAuth Walt sebelum menjalankan operasi trading.
- **Auto-Sync GitHub Secret:** Jika Walt merotasi `refresh_token`, sistem mengenkripsi token baru menggunakan libsodium (`pynacl`) dan memperbarui secret repository GitHub secara otomatis menggunakan `GH_PAT`. Hal ini memastikan bot beroperasi tanpa risiko terhenti karena token kadaluarsa.

### 7.3 Kanal Notifikasi & Laporan (Telegram 3x Auto-Retry)
- **Telegram Push Alert:** Notifikasi lengkap dikirimkan ke Telegram pribadi Owner (`TELEGRAM_CHAT_ID`) memuat status saldo, posisi aktif, akumulasi funding yang diterima, keputusan AI, dan aksi pengubahan stop loss.
- **Fault-Tolerant Exponential Backoff:** `notifier.py` dilengkapi sistem 3x percobaan ulang (*retry*) dengan jeda eksponensial untuk mengantisipasi gangguan jaringan sementara (`Connection reset by peer`).
- **GitHub Step Summary:** Setiap eksekusi mencatat audit log terformat rapi pada antarmuka *Actions Step Summary* di repository GitHub.

### 7.4 Sinkronisasi Real-Time Google Sheets (`sheet_logger.py` & `training_data.py`)
- Setiap hasil scan, eksekusi order, penutupan posisi, ringkasan PnL harian, dan dataset pelatihan SFT otomatis dikirimkan ke Google Sheets melalui Apps Script Webhook v2 (`google_apps_script.js`, versi `2026-11-21.1` terverifikasi aktif untuk **5 tab**):
  - **`Daily_Scans`:** Tepat **30 kolom bernama** (19 kolom scan + konsensus Swarm AI P1–P7 + 11 kolom diagnostik Market Gravity `mg_*`), dengan dedupe read-back verification.
  - **`Executions_Log`:** Log terstandarisasi untuk setiap order (`EXECUTED`, `SKIP`, `FAILED`, `ERROR`).
  - **`Closed_Positions`:** Riwayat penutupan posisi (baik yang ditutup oleh monitor karena funding flip/breach maupun auto-close TP/SL exchange yang terdeteksi lewat diff snapshot `logs/open_snapshot.json`).
  - **`PnL_Tracker`:** Rekapitulasi harian idempotent (1 baris per tanggal UTC) berisi total ekuitas, funding bulan berjalan, dan progres terhadap target $100/bulan.
  - **`Data Training`:** Tab khusus SFT dataset (`12` kolom bernama, deduped oleh `Sample ID`, terisi baris awal tervalidasi menuju target $\ge 1.000$ baris `<think>...</think>` + JSON untuk fine-tuning Fase 2).
- Evaluasi statistik performa keseluruhan trade dapat dijalankan kapan saja melalui perintah CLI `python walt_bot.py stats`.

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

- [`walt_bot.py`](file:///walt_bot.py) — Core bot orchestrator (Gear 0–3, US stock hours UTC, 4-layer risk monitor, order size clamping).
- [`token_manager.py`](file:///token_manager.py) — Otomasi refresh OAuth token Walt dan sinkronisasi secret GitHub via PyNaCl.
- [`oak_mcp_client.py`](file:///oak_mcp_client.py) — Klien Streamable HTTP OAK Research MCP (`https://oakresearch.io/api/mcp`) + auto-rotate refresh token ke `.env` & GitHub Secrets.
- [`setup_oak_oauth.py`](file:///setup_oak_oauth.py) — Skrip interaktif otentikasi awal OAuth 2.1 PKCE OAK Research MCP.
- [`llm_research.py`](file:///llm_research.py) — Client 4-Voter Multi-Vendor Swarm (`glm-5.2-fast-preview`, `glm-5.3-prime`, `minimax-m3`, `claude-haiku-5.5`) + CRO Meta-Aggregator `glm-5.3` + injeksi OAK MCP.
- [`training_data.py`](file:///training_data.py) — Pengumpul otomatis dataset SFT `<think>` ke tab Google Sheet `Data Training` & `logs/training_dataset.jsonl`.
- [`tools/export_training_data.py`](file:///tools/export_training_data.py) — Utilitas backfill/ekspor dataset `Data Training` menuju target >= 1.000 baris SFT.
- [`notifier.py`](file:///notifier.py) — Pengirim pesan notifikasi format Markdown ke Telegram Bot API dengan 3x exponential backoff retry.
- [`sheet_logger.py`](file:///sheet_logger.py) — Modul pengirim telemetry 5 tab Google Sheets via Webhook v2 & gviz reader.
- [`google_apps_script.js`](file:///google_apps_script.js) — Skrip Google Apps Script Webhook v2 (`2026-11-21.1`) penampung 5 tab spreadsheet.
- [`.github/workflows/`](file:///.github/workflows/) — Definisi scheduler cloud runners (daily scan, risk monitor, market execute, crypto execute).
- [PRD.md](file:///PRD.md) — Dokumen spesifikasi kebutuhan produk untuk Roadmap Evolusi SaaS Fase 1 & Fase 2.
- [AUDIT.md](file:///AUDIT.md) — Log audit independen dan rekam jejak verifikasi teknis Ronde 2–9+.

## 10. USULAN strategi berbasis struktur likuiditas (Ronde 8) - **BELUM AKTIF**

Status: **usulan**. Tidak ada satu pun parameter di `strategy_config.py` yang diubah untuk
ini. Angka-angkanya berasal dari `tools/gravity_strategy.py` (in-sample, 20 koin perps,
750 bar harian, resolusi intrabar konservatif, satu posisi per aset) dan detailnya ada di
`AUDIT.md` 3l-3m.

### 10.1 Aturan yang diusulkan

| Tahap | Aturan | Dasar ukur |
|---|---|---|
| Sinyal | FSM `market_gravity` mencatat status **`Rejected`** pada sebuah magnet (harga menyapu zona + buffer 0,05 ATR, lalu ditutup kembali ke sisi asal dalam 2 bar) | selisih gross terhadap cerminnya **+0,42R s.d. +0,68R** pada semua bentuk target |
| Arah | **Fade**: rejected di atas -> short; rejected di bawah -> long | fade_R1 71,3% vs null_R1 27,5% |
| Stop | ekstrem sweep **+/- 1,0 ATR(14)** di luar sisi yang tersapu | buffer ini yang DIPILIH jendela latih di kelima fold walk-forward; jarak stop rata-rata 10,87% |
| Target | **1x jarak stop** (BUKAN 3R, BUKAN magnet seberang) | grossR +0,207R out-of-sample; R3 tetap negatif |
| Durasi | biarkan bekerja ~5 bar; jangan memaksa target jauh | rata-rata tahan 4,6 bar (out-of-sample) |
| Carry | hanya bila sisi posisi MENANGKAR funding (gerbang E5 tetap berlaku) | terukur 0,0665%/hari dari notional (bukan 0,52% yang semula diasumsikan) -> relief ~0,03R, praktis kecil |

### 10.2 Status dua syarat sebelum ini menyentuh order

1. **SEBAGIAN SELESAI - `tools/contract_economics.py` sudah jalan dari CI (read-only,
   read-only ditegakkan + dites).** Terukur dari akun sendiri: carry nyata **0,0665%/hari**
   (bukan 0,52% yang saya pakai di simulasi sebelumnya - terlalu besar ~8x), dan slippage
   yang bursa nyatakan = 1,00% **seragam di 139 kontrak** (p10==p90 => konstanta default,
   bukan pengukuran). Deviasi fill-vs-keputusan (median 0,793%) TIDAK dipakai: jeda
   scan->fill median 1,52 jam / p90 21,5 jam, jadi itu drift, bukan slippage. **Slippage
   murni masih belum terukur** dan satu-satunya cara mengukurnya butuh satu order nyata.
   Rinciannya di AUDIT 3o.
2. **SELESAI, dengan catatan** - `tools/gravity_walkforward.py` (5 fold, purge 12 bar,
   parameter dipilih ulang di jendela latih tiap fold): 1.727 trade out-of-sample,
   win 74,9%, gross +0,207R, net@1,18% **+0,075R**, positif di kelima fold, pilihan
   parameter identik di kelima fold, dan interval 90% cluster-bootstrap per aset
   [+0,014, +0,146]R tidak menyentuh nol. Catatan: bar sumbernya Hyperliquid/Binance,
   eksekusinya Walt - kualitas fill belum diuji; carry funding belum masuk.

### 10.3 Konsekuensi risiko yang harus diterima lebih dulu

- Stop terpilih rata-rata **10,87%** harga. Pada leverage 2 itu = ~21,7% margin per posisi
  (leverage minimum 1 pun tetap ~10,9%); hari ini satu rugi = ~3% margin. Mempertahankan
  risiko dolar yang sama menuntut margin per posisi turun ~3,6x, dan pada basis $2,50-3 itu
  jatuh di bawah `min_size_base` - tidak bisa dieksekusi. Jadi pilihan jujurnya: terima
  risiko per trade lebih besar, ATAU perkecil jumlah slot, ATAU tolak aturan ini.
- `max_slots` 15 pada 20 koin yang saling berkorelasi bukan diversifikasi - saat satu faktor
  jatuh, sebagian besar posisi kena stop bersamaan, dan dengan stop lebar kerugiannya
  ikut lebar.
- **Benturan kebijakan yang harus Anda putuskan:** aturan ini hidup dari tepi struktural,
  bukan dari funding. Gerbang E5 (funding vs biaya) menuntut ~1,77%/hari dan akan menolak
  hampir semua entry ini. Jadi pertanyaannya bukan "TP/SL berapa", melainkan: bolehkah
  sebuah posisi dibuka karena tepi struktural yang terukur, atau hanya karena carry?
- Probabilitas first-passage Monte Carlo **tidak** dipakai sebagai dasar ukuran posisi:
  kalibrasinya R2=0,0002 dan probabilitasnya belum tervalidasi (n ~160 prediksi, satu
  decile terisi, selisih ~13 poin - AUDIT 3m). Boleh dipakai untuk menggambarkan sebaran,
  bukan untuk menentukan besar posisi.
- Comfort Point tetap dihitung dan ditulis ke kolom diagnostik sebagai konteks target
  mean-reversion, tetapi TP aturan ini diambil dari jarak stop, bukan dari Comfort Point,
  karena pada pengukuran di atas target Comfort Point terlalu dekat untuk membayar biaya.

### 10.4 Kriteria promosi (biarkan angka yang memutuskan, bukan narasi)

Walk-forward sudah lolos (10.2 butir 2). Yang tersisa sebelum aturan ini boleh menembus
jalur entry: (a) biaya round-trip nyata terukur di Walt untuk clip kita, (b) kualitas fill
Walt diuji pada beberapa sinyal pertama dengan `DRY_RUN=true`, dan (c) keputusan pemilik
tentang hubungan aturan ini dengan E5 - diganti perannya, dijalankan paralel sebagai
jalur kedua, atau ditolak. Tanpa ketiganya, `strategy_config` tetap tidak disentuh.

