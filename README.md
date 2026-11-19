# ⚡ Walt Perps Autonomous Funding Arbitrage Bot

[![GitHub Actions Status](https://github.com/channelhellokids513-ops/waltTG-Agent/actions/workflows/market-execute.yml/badge.svg)](https://github.com/channelhellokids513-ops/waltTG-Agent/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-green.svg)](https://python.org)
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
- **Alibaba Cloud:** `qwen-turbo` & `qwen-plus`
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

| Komponen / Workflow | Jadwal Cloud Scheduler (WIB) | Jadwal Cron GitHub (UTC) | Deskripsi |
|---|---|---|---|
| [`daily-scan.yml`](.github/workflows/daily-scan.yml) | **07:17 & 19:17 WIB** (2x Sehari) | `17 0,12 * * *` | Master Scan seluruh kontrak, ranking peluang funding, riset Swarm AI mendalam (Qwen+Gemini P1-P7), **eksekusi instan order GO/HALF**, simpan database ke Google Sheet & notif Telegram. |
| [`market-execute.yml`](.github/workflows/market-execute.yml) | **Dinamis 1h / 2h** (Calmed Adaptive + Gatekeeper) | `43 */2 * * *` | Eksekusi adaptif Saham US & Komoditas 24/7: **1 jam (Gear 2)** saat peluang funding melimpah ($\ge 3$ aset $\ge 0.5\%$/hari atau $\ge 2$ ekstrem $\ge 0.8\%$/hari), **2 jam (Gear 1)** saat normal/sepi, dan *fast-exit* (<2 detik) saat slot penuh (Gear 0). Dilengkapi *3-hour re-entry cooldown*. |
| [`crypto-execute.yml`](.github/workflows/crypto-execute.yml) | **Dinamis 1h / 2h** (Calmed Adaptive + Gatekeeper) | `11 */2 * * *` | Eksekusi adaptif 10 Crypto Bluechip (`BTC`, `ETH`, `SOL`, dll) dengan sistem **Calmed Adaptive (1h / 2h)** dan *3-hour re-entry cooldown* agar posisi cukup lama memanen *funding*. |
| [`risk-monitor.yml`](.github/workflows/risk-monitor.yml) | **Dinamis 5–10 Menit** (6x–12x/Jam + Smart Idle) | `*/10 * * * *` | Pengawas risiko adaptif: otomatis *fast-exit* (<2 detik) saat 0 posisi; meningkat hingga **12x/jam (~tiap 5 menit)** jika ada $\ge 3$ posisi aktif, posisi kategori `HALF`, atau harga mendekati TP ($\ge +0.70\%$) / SL ($\le -1.00\%$); dan stabil di **10 menit** untuk posisi `GO` tenang. |

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
