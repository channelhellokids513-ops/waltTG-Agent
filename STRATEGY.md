# Strategi Walt Perps Funding Arbitrage — E2E

**Pemilik:** Rizal Dicky
**Dibuat:** 13 November 2026
**Platform:** Walt (ex-Wallet in Telegram) · MCP via Claude Code
**Target:** $100/bulan dari funding income untuk cover tagihan rutin

---

## 1. Thesis Strategi

Memanfaatkan **funding rate ekstrem** pada Walt Perps sebagai pendapatan pasif, dengan mengambil posisi di sisi yang **dibayar**:

- **SHORT** pada kontrak dengan funding positif tinggi (longs membayar shorts)
- **LONG** pada kontrak dengan funding negatif tinggi (shorts membayar longs)

Funding rate ekstrem adalah sinyal positioning crowded. Kita menjadi counterparty untuk sisi yang crowded, dibayar sambil tetap kena eksposur harga. Risiko utama: pergerakan harga asset yang signifikan melawan arah posisi.

---

## 2. Pemilihan Aset

### Universe
- 127 kontrak perpetual di Walt
- Filter awal: **volume 24h > $1,000,000** (likuid, ~48 kontrak lolos)

### Fokus Aset
**Hanya saham US dan komoditas tradisional** karena arah bisnis jelas:
- **Saham:** NVDA, NBIS, CRCL, MSTR, AAPL, GOOGL, AMD, PLTR, COIN, SNDK, SPY, US100, AMZN, TSLA, META
- **Komoditas:** XAU (emas), XAG (perak), BRENTOIL

**Tidak masuk:** BTC, ETH, SOL, dan token crypto lainnya. Posisi GRAM long yang sudah berjalan tetap dipegang, exit bertahap.

### Ranking Peluang
Setiap pagi (06:39 WIB), funding rate annualized dihitung:
```
funding_annualized = funding_rate_hourly × 24 × 365 × 100
```

Top 5 positif tertinggi (peluang SHORT) dan Top 5 negatif terendah (peluang LONG) dirangking, kemudian 3 terbaik diprioritaskan.

---

## 3. Position Sizing

### Parameter Dasar
- **Leverage maksimal:** 2x (konservatif, liq ~49% movement)
- **Margin per posisi:** $2.50 – $3.00 USDC
- **Exposure per posisi:** $5 – $6 (dengan leverage 2x)
- **Jumlah posisi simultan:** 2 – 4
- **Buffer wajib:** minimal 15% dari saldo untuk margin adjustment

### Kendala Teknis Walt
Minimum size per base asset menetapkan margin minimum efektif di lev 2x:
| Kontrak | Min margin @ 2x |
|---------|-----------------|
| NBIS    | $1.65           |
| NVDA    | $2.12           |
| GRAM    | $2.34           |
| HYPE    | $2.59           |
| XAU     | $2.97           |

---

## 4. Entry Rules

### Kondisi Buka Posisi
1. Funding rate absolute > 0.5%/hari (= 182%/yr annualized)
2. Volume 24h > $1M (likuid)
3. Saldo tersedia memenuhi minimum margin + buffer
4. Market open (untuk saham: Mon-Fri 21:30 - 04:00 WIB)
5. Tidak sudah ada posisi terbuka di kontrak yang sama

### Alur Eksekusi (Walt API)
Walt mewajibkan **preview → confirmation → execute**:
1. `perps_prepare_order_place` — preview (dapat confirmationToken, expire ~90s)
2. Verifikasi `risk.ok = true` dan tidak ada warnings
3. `perps_execute_operation` dengan token
4. `perps_get_order` setelah 5 detik untuk cek status fill

### TP/SL Baseline Otomatis
Set bersamaan dengan order open:
- **SHORT:** SL = entry × 1.07, TP = entry × 0.85
- **LONG:** SL = entry × 0.93, TP = entry × 1.15

Jika order gagal karena TP/SL di luar buffer kontrak, order ulang tanpa autoClose, lalu set terpisah dengan `perps_prepare_auto_close_set`.

---

## 4.5. Research Layer (Khusus Saham)

Sebelum masuk posisi saham, lakukan research otomatis untuk filter kandidat yang funding-nya tinggi tapi kontek bisnisnya tidak mendukung.

### Research Checklist per Kandidat
Untuk setiap saham di shortlist, lakukan 2 WebSearch:

1. **Earnings date lookup**
   - Query: `"<TICKER> earnings date next 2026"`
   - Ekstrak tanggal earnings berikutnya
   - Hitung `days_to_earnings`

2. **News sentiment (7 hari terakhir)**
   - Query: `"<TICKER> stock news last 7 days"`
   - Ringkas narasi dominan dalam 1-2 kalimat
   - Klasifikasikan sentiment: `bullish` / `bearish` / `neutral`

3. **Business thesis sanity check** (opsional, dari pengetahuan)
   - NVDA: AI chips, hyperscaler capex cycle
   - NBIS: AI cloud infra (Nebius post-Yandex spin-off)
   - CRCL: USDC stablecoin issuer (pasca IPO)
   - MSTR: BTC treasury proxy
   - COIN: crypto exchange, sentimen crypto
   - AAPL/GOOGL/AMZN/META: big tech, earnings cycle driven

### Decision Matrix

| Funding Signal | News Sentiment | Days to Earnings | Keputusan |
|---------------|----------------|------------------|-----------|
| Tinggi (≥0.5%/hari) | Confirm arah posisi | > 7 hari | ✅ **GO full size** ($2.50-3 margin) |
| Tinggi | Neutral | > 7 hari | ⚠️ **HALF size** ($1.50 margin) |
| Tinggi | Melawan arah | > 7 hari | ❌ **SKIP** |
| Apapun | Apapun | ≤ 7 hari | ❌ **SKIP** (earnings risk) |
| Rendah (<0.5%/hari) | Apapun | Apapun | ❌ **SKIP** (funding kurang) |

**Alignment rules:**
- `SHORT` + `bearish news` = confirm (bearish mendukung posisi short)
- `LONG` + `bullish news` = confirm (bullish mendukung posisi long)
- `SHORT` + `bullish news` = melawan arah → SKIP
- `LONG` + `bearish news` = melawan arah → SKIP

### Output Example

> *[✅ GO] SHORT NBIS @$130.41, sektor AI cloud infra*
> *Funding: +1.38%/hari (annualized +504%/yr)*
> *Days to earnings: 45 (aman)*
> *News sentiment: bearish*
> *Narasi: Sell-side downgrade dari 3 analis minggu ini, concern tentang margin pressure Q4*
> *Alignment: bearish news + SHORT = confirm thesis → eksekusi $2.50 lev 2x*

### Fallback
- **WebSearch gagal** untuk 1 kandidat → tandai `⚠️ no research data`, boleh rekomendasi tapi ukuran HALF
- **Komoditas** (XAU, XAG, BRENTOIL): research opsional, bisa skip karena mostly macro-driven

---

## 5. Risk Management — 4 Lapis

### Lapis 1: TP/SL Baseline (Statis)
Set saat posisi dibuka. Formula di section 4.

### Lapis 2: Dinamis Berdasarkan Funding
Dicek tiap 4 jam oleh risk monitor.

| Kondisi | Aksi |
|---------|------|
| Funding flip sign (SHORT→funding negatif, LONG→funding positif) | **Tutup posisi**, thesis gugur |
| \|funding_daily\| < 0.1% | Perketat SL ke breakeven (entry price) |
| \|funding_daily\| < 50% dari funding entry | Perketat SL ke entry ± 2% |

### Lapis 3: Volatilitas & Momentum
Dicek bersamaan dengan Lapis 2.

| Kondisi | Aksi |
|---------|------|
| \|price_move_from_entry\| > 5% DAN posisi profit | SL ke breakeven + 1% (lock profit) |
| \|price_move_from_entry\| > 3% DAN posisi loss | Perketat SL ke entry ± 5% |
| Saham 3 hari sebelum earnings | Tutup posisi atau perketat ekstrem |

### Lapis 4: Trailing Stop (Profit Lock Progresif)
Trailing SL untuk kunci profit yang sudah di tangan.

| Profit Unrealized | Aksi SL |
|-------------------|---------|
| ≥ 3% | SL pindah ke entry (breakeven — zero loss dilated) |
| ≥ 5% | SL trail 2.5% di belakang current price |
| ≥ 10% | SL trail 1.5% di belakang current price |
| ≥ 20% | SL trail 1% di belakang current price (sangat ketat) |

**Aturan trailing:**
- SL hanya bergerak ke arah menguntungkan (tidak pernah mundur)
  - LONG: `new_SL = max(current_SL, current_price × (1 - trailing%))`
  - SHORT: `new_SL = min(current_SL, current_price × (1 + trailing%))`
- Buffer 0.1%: update hanya jika selisih SL baru vs lama > 0.1%
- Lapis 2 (funding flip) memiliki prioritas: kalau funding flip → close langsung, bypass trailing

---

## 6. Monitoring Schedule

### Scheduled Tasks (Claude Code)

| Task | Schedule | Fungsi |
|------|----------|--------|
| Walt Perps funding scan | 06:39 WIB tiap hari | Scan peluang + income tracker → Google Calendar 08:00 |
| Walt eksekusi posisi | On demand (one-shot) | Open posisi saat market buka dengan TP/SL baseline |
| Walt risk monitor | Tiap 4 jam (00:35, 04:35, 08:35, 12:35, 16:35, 20:35 WIB) | Apply Lapis 2, 3, 4 ke semua posisi terbuka |

### Output Notifikasi
- **Push notification** ke HP setiap task selesai dengan aksi
- **Google Calendar event** untuk scan harian (08:00 WIB) dan eksekusi/aksi penting
- **Reminder popup** 10 menit sebelum event

### Metrics yang Di-track
- Total funding diterima bulan ini (`funding_paid_quote` sum)
- Progress vs target $100/bulan
- Estimasi akhir bulan (prorata dari rata-rata harian)
- Status: ON TRACK / BEHIND / AHEAD
- PnL unrealized per posisi

---

## 7. Roadmap Scaling

### Phase 1: Learning (Bulan 1-2)
- Modal: $10 – $50
- Posisi: 2-3 kecil
- Target: pahami mekanika, funding timing, TP/SL behavior
- Income realistis: $2 – $10/bulan

### Phase 2: Validation (Bulan 3-4)
- Modal: $100 – $300
- Posisi: 3-5 diversifikasi
- Target: track konsistensi funding, uji risk monitor
- Income realistis: $18 – $54/bulan

### Phase 3: Target (Bulan 5-6)
- Modal: $560
- Posisi: 5-6 cross-sector
- Target: $100/bulan konsisten
- Avg funding asumsi 0.6%/hari = $3.36/hari = $100.80/bulan

### Phase 4: Scale (Bulan 7+)
- Modal: $1,000+
- Buffer untuk opportunistic rebalancing
- Income surplus $180+/bulan

---

## 8. Resiko yang Diterima

1. **Asset price shock** — 1 bulan funding bisa hilang kalau asset bergerak lawan 10-20%
2. **Funding rate normalization** — Yang ekstrem sekarang bisa drop ke 0.1%/hari dalam seminggu
3. **Market closure (saham)** — NBIS, NVDA, dll tidak tradable overnight/weekend US
4. **Platform risk** — Walt custody risk, server downtime
5. **Liquidation (meskipun minim)** — Di lev 2x, asset harus bergerak ~49% untuk liq

### Mitigasi
- Diversifikasi minimal 2 asset uncorrelated
- Buffer 15-20% dari modal
- Risk monitor tiap 4 jam
- SL tidak lebih dari 7% dari entry
- Tidak pernah leverage > 2x

---

## 9. Metrics Keberhasilan

Review bulanan:
- **Income delivered** vs target $100
- **Win rate** per posisi (ditutup profit vs loss)
- **Average holding period** per posisi
- **Realized vs unrealized PnL**
- **Fee impact** (target < 10% dari funding income)

---

## 10. Appendix: Kontrak Likuid Reference

Daftar kontrak saham + komoditas likuid dengan contract ID:

| Base | ID | Max Lev | Fee Taker |
|------|----|---------|-----------|
| NBIS | 189 | 10x | 0.09% |
| NVDA | 110 | 20x | 0.09% |
| CRCL | - | 10x | 0.09% |
| MSTR | - | 10x | 0.09% |
| AAPL | - | 20x | 0.09% |
| GOOGL | - | 20x | 0.09% |
| AMD | - | 10x | 0.09% |
| PLTR | - | 10x | 0.09% |
| SPY | - | 50x | 0.09% |
| US100 | - | 30x | 0.09% |
| XAU | 92 | 25x | 0.09% |
| XAG | - | 20x | 0.09% |
| BRENTOIL | - | 20x | 0.09% |

(ID yang kosong akan diisi dari `perps_list_contracts` saat pertama kali dibutuhkan)

---

**File terkait:**
- `walt_bot.py` — Implementasi Python dari semua logika di atas
- Claude Code scheduled tasks — Automasi berjalan di cloud Anthropic
