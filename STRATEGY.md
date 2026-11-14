# Strategi Walt Perps Funding Arbitrage — E2E

**Pemilik:** Rizal Dicky  
**Dibuat:** 13 November 2026  
**Terakhir Diperbarui:** 14 November 2026 (Sinkronisasi Live GitHub Actions & Exclude Crypto/GRAM)  
**Platform Eksekusi:** Walt Perps Relay API (`walletbot.me`) via GitHub Actions Autonomous Runner  
**AI Research Engine:** Dual-AI Collaboration (Alibaba Cloud Qwen-Plus & Google Gemini via `DASHSCOPE_API_GEMINI_KEY`)  
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

### 2.3 Aturan Eksklusi Ketat (Strict Crypto Exclusion)
- ❌ **Seluruh token crypto dieksklusi secara permanen:**  
  Token seperti `BTC`, `ETH`, `SOL`, dan altcoin lainnya **TIDAK DIMASUKKAN** ke dalam universe trading.
- ❌ **GRAM Dihapus Sepenuhnya:**  
  Kontrak `GRAM` **TIDAK LAGI DIMASUKKAN** ke dalam universe bot. Seluruh pemindaian (*daily scan*), riset sentimen, dan eksekusi order secara ketat mengabaikan kontrak GRAM dan token kripto spekulatif lainnya.

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
4. **Waktu Eksekusi (Saham):** Dijalankan pada saat pembukaan pasar saham AS (Senin–Jumat pukul 21:40 WIB / 14:40 UTC).
5. **No Duplicate:** Belum ada posisi aktif yang terbuka pada kontrak/aset yang sama.
6. **AI Research Confirmation:** Lolos verifikasi model Qwen (skor keputusan **GO** atau **HALF**).

### 4.2 Alur Eksekusi API Walt (Preview → Confirm → Execute)
Walt mewajibkan pola 2-tahap dengan *time-limited confirmation token*:
1. `perps_prepare_order_place` — Meminta pratinjau order dengan parameter: `contractId`, `side`, `type: "market"`, `leverage: 2`, `amount`, `price`, dan `autoClose` (TP/SL).
2. Verifikasi respon: Memastikan `ok = true` dan parameter risiko disetujui.
3. `perps_execute_operation` — Mengirimkan `confirmationToken` (berlaku ~90 detik) untuk konfirmasi eksekusi order di bursa.
4. **Fallback TP/SL Otomatis:** Jika bursa menolak order awal karena batasan parameter TP/SL di luar toleransi slippage kontrak, bot mengeksekusi order terbuka tanpa `autoClose`, kemudian mengatur TP/SL secara terpisah via `perps_prepare_auto_close_set`.

### 4.3 Parameter TP/SL Baseline Statis
Setiap posisi baru langsung dilindungi oleh batas pengaman baseline:
- **Posisi SHORT:**
  - Stop Loss (SL): `entry_price × 1.07` (+7% toleransi rugi harga)
  - Take Profit (TP): `entry_price × 0.85` (-15% pergerakan harga menguntungkan)
- **Posisi LONG:**
  - Stop Loss (SL): `entry_price × 0.93` (-7% toleransi rugi harga)
  - Take Profit (TP): `entry_price × 1.15` (+15% pergerakan harga menguntungkan)

---

## 5. Dual-AI Collaboration Layer (Qwen-Plus + Google Gemini)

Khusus untuk instrumen saham US, sebelum order dieksekusi, bot menjalankan lapisan analisis fundamental dan sentimen menggunakan arsitektur **Dual-AI Collaboration & Consensus** yang memadukan dua model kecerdasan buatan terdepan:
1. **Model 1: Alibaba Cloud Qwen-Plus** (via `DASHSCOPE_API_KEY`)
2. **Model 2: Google Gemini 1.5 Flash** (via `DASHSCOPE_API_GEMINI_KEY` / `GEMINI_API_KEY`)

Kedua model mengevaluasi berita pasar secara independen, kemudian disintesiskan oleh konsensus engine untuk menghasilkan keputusan yang lebih tajam dan objektif.

### 5.1 Parameter Riset AI
1. **Deteksi Jadwal Rilis Laporan Keuangan (Earnings Date):**
   - Menghitung jumlah hari menuju rilis earnings berikutnya (`days_to_earnings`).
   - Mencegah risiko lonjakan volatilitas harga ekstrem (*binary event risk*).
2. **Dual-Model Sentiment Analysis & Cross-Validation:**
   - Mengambil intisari berita finansial terkini (Yahoo Finance Feed).
   - Qwen dan Gemini masing-masing mengklasifikasikan sentimen: `bullish`, `bearish`, atau `neutral` beserta confidence score dan ringkasan narasi.
3. **Logika Konsensus Kolaborasi AI:**
   - **STRONG_CONSENSUS (Konsensus Kuat):** Qwen dan Gemini sepakat pada arah yang sama (keduanya *bullish* atau keduanya *bearish*). Skor keyakinan (*confidence*) ditingkatkan dan rekomendasi eksekusi menjadi `GO (Full Size)`.
   - **PARTIAL_CONSENSUS (Konsensus Parsial):** Satu model menentukan arah (*directional*) sedangkan model lainnya menilai *neutral*. Arah dari model aktif dijadikan acuan dengan alokasi modal terukur.
   - **CONFLICT (Proteksi Konflik ⚡):** Qwen dan Gemini saling berlawanan arah (satu *bullish*, satu *bearish*). Pasar dinilai dalam kondisi polarisasi tinggi / bias tak terduga. Sistem otomatis mengaktifkan **circuit breaker proteksi modal**: sentimen diturunkan ke `neutral` dan ukuran posisi dibatasi ke `HALF Size` (atau `SKIP`).
   - **RESILIENT FALLBACK:** Jika salah satu API key mengalami limit atau gangguan jaringan, sistem otomatis berjalan dengan mode *Single-Model* tanpa memutus alur trading.

### 5.2 Decision Matrix (Keputusan Eksekusi AI)

| Sinyal Funding | Status Konsensus AI | Sentimen Gabungan | Hari Earnings | Keputusan Bot | Aksi & Margin |
|---|---|---|---|---|---|
| Tinggi (≥0.5%/hari) | **STRONG_CONSENSUS** | Aligned dg posisi | > 7 hari | ✅ **GO (Full Size)** | Buka posisi full ($2.50 – $3.00 USDC) |
| Tinggi (≥0.5%/hari) | **PARTIAL_CONSENSUS** | Aligned dg posisi | > 7 hari | ✅ **GO / HALF** | Buka posisi ($1.50 – $2.50 USDC) |
| Tinggi (≥0.5%/hari) | **CONFLICT (Qwen vs Gemini)** | Netral (Proteksi) | > 7 hari | ⚠️ **HALF / SKIP** | Proteksi risiko: alokasi separuh ($1.25) |
| Tinggi (≥0.5%/hari) | Model Manapun | Melawan arah posisi | > 7 hari | ❌ **SKIP** | Batalkan eksekusi, hindari melawan tren |
| Berapapun | Model Manapun | Berapapun | **≤ 7 hari** | ❌ **SKIP** | Batalkan eksekusi (hindari risiko earnings) |
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

1. **Lapis 1 (Baseline TP/SL):**  
   Memastikan tidak ada posisi yang tidak terlindungi batas stop loss 7% dan take profit 15%.
2. **Lapis 2 (Dinamika Funding Rate — Prioritas Utama):**  
   - Jika arah funding berbalik (*funding flip sign*), misal posisi SHORT namun funding rate berubah menjadi negatif (artinya posisi kita yang harus membayar pasar), **posisi seketika ditutup pasar**. Thesis awal dinyatakan batal.  
   - Jika funding rate meluruh tajam hingga `< 0.1%/hari`, stop loss otomatis dipindahkan ke harga entry (*breakeven*).
3. **Lapis 3 (Volatilitas & Momentum Harga):**  
   - Jika posisi merugi > 3% dari entry price, SL diperketat dari 7% menjadi 5% untuk memangkas potensi drawdown lebih lanjut.
   - Jika posisi berbalik untung > 5%, SL langsung digeser ke harga entry + 1% untuk mengamankan keuntungan bersih di atas biaya transaksi.
4. **Lapis 4 (Trailing Stop Progresif):**  
   - Stop Loss hanya digeser ke arah yang menguntungkan (tidak pernah ditarik mundur).
   - Menggunakan ambang batas buffer minimal 0.1% untuk menghindari perubahan order yang tidak efisien.

---

## 7. Arsitektur Deployment & Jadwal Otomasi GitHub Actions

Sistem beroperasi 100% secara otonom di cloud GitHub Actions runners dengan nol biaya server:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        LIVE DEPLOYMENT ARCHITECTURE                                    │
└────────────────────────────────────────────────────────────────────────────────────────┘

    GitHub Cloud Scheduler (cron triggers)
    ├── daily-scan.yml      (06:39 WIB / 23:39 UTC)   → Scan 127 kontrak + Qwen AI + Notif
    ├── risk-monitor.yml    (Tiap 4 jam di menit 35)   → Evaluasi 4-lapis risiko tiap posisi
    └── market-execute.yml  (21:40 WIB Mon-Fri)        → Eksekusi saham saat US market open
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

### 7.1 Jadwal Workflow GitHub Actions

| Workflow File | Jadwal Cron (UTC) | Waktu Lokal (WIB) | Fungsi Utama |
|---|---|---|---|
| [daily-scan.yml](file:///.github/workflows/daily-scan.yml) | `39 23 * * *` | **06:39 WIB Tiap Hari** | Pemindaian seluruh kontrak, ranking peluang funding harian, riset sentimen AI Qwen, dan pengiriman ringkasan ke Telegram. |
| [risk-monitor.yml](file:///.github/workflows/risk-monitor.yml) | `35 */4 * * *` | **Tiap 4 Jam** (00:35, 04:35, 08:35, 12:35, 16:35, 20:35 WIB) | Evaluasi 4 lapis risiko pada semua posisi terbuka, update trailing stop, dan *emergency close* jika funding berbalik. |
| [market-execute.yml](file:///.github/workflows/market-execute.yml) | `40 14 * * 1-5` | **21:40 WIB Senin–Jumat** | Eksekusi order pembukaan posisi saham US bertepatan dengan likuiditas tinggi pembukaan pasar Wall Street. |

### 7.2 Otomasi Rotasi Token OAuth (`token_manager.py`)
- Akses ke Walt API menggunakan protokol OAuth 2.0 PKCE dengan Bearer token yang memiliki masa berlaku terbatas.
- `token_manager.py` memeriksa masa aktif token secara otomatis dan melakukan refresh ke endpoint OAuth Walt sebelum menjalankan operasi trading.
- **Auto-Sync GitHub Secret:** Jika Walt merotasi `refresh_token`, sistem mengenkripsi token baru menggunakan libsodium (`pynacl`) dan memperbarui secret repository GitHub secara otomatis menggunakan `GH_PAT`. Hal ini memastikan bot beroperasi tanpa risiko terhenti karena token kadaluarsa.

### 7.3 Kanal Notifikasi & Laporan
- **Telegram Push Alert:** Notifikasi lengkap dikirimkan ke Telegram pribadi Owner (`TELEGRAM_CHAT_ID`) memuat status saldo, posisi aktif, akumulasi funding yang diterima, keputusan AI, dan aksi pengubahan stop loss.
- **GitHub Step Summary:** Setiap eksekusi mencatat audit log terformat rapi pada antarmuka *Actions Step Summary* di repository GitHub.

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

- [`walt_bot.py`](file:///walt_bot.py) — Core bot orchestrator (scan, research, market execute, 4-layer risk monitor).
- [`token_manager.py`](file:///token_manager.py) — Otomasi refresh OAuth token dan sinkronisasi secret GitHub via PyNaCl.
- [`llm_research.py`](file:///llm_research.py) — Client Alibaba Cloud Qwen untuk inferensi sentimen berita & jadwal earnings.
- [`notifier.py`](file:///notifier.py) — Pengirim pesan notifikasi format Markdown ke Telegram Bot API.
- [`.github/workflows/`](file:///.github/workflows/) — Definisi cron scheduler cloud runners (daily scan, risk monitor, market execute).
- [PRD.md](file:///PRD.md) — Dokumen spesifikasi kebutuhan produk untuk Roadmap Evolusi SaaS Fase 1 & Fase 2.
