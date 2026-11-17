# ⚡ Walt Perps Autonomous Funding Arbitrage Bot

[![GitHub Actions Status](https://github.com/channelhellokids513-ops/waltTG-Agent/actions/workflows/market-execute.yml/badge.svg)](https://github.com/channelhellokids513-ops/waltTG-Agent/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-green.svg)](https://python.org)
[![Zero-Cost Infrastructure](https://img.shields.io/badge/Infra%20Cost-%240%20(Free%20Tier)-brightgreen.svg)](#-biaya-infrastruktur-100-zero-cost)

Autonomous delta-neutral & directional funding rate arbitrage trading system untuk platform **Walt Perps** (`walletbot.me`). Ditenagai oleh **Dual-AI Research Swarm (Qwen + Gemini)**, **Google Cloud Scheduler Precision Bridge**, sistem **Risk Monitor 4-Lapis**, dan pelaporan *real-time* ke **Telegram** serta **Google Sheets**.

---

## 🎯 Tujuan Repository

1. **Passive Yield Harvest:** Menangkap funding rate ekstrem pada bursa perpetual futures Walt sebagai sumber pendapatan pasif terukur (Target: **$100.00 USDC / bulan**) dengan leverage konservatif (1x–2x).
2. **Zero Directional Disaster:** Melindungi modal dengan model **Dynamic Take Profit & Early Breakeven Trailing Tiers (+1.5% Zero-Risk Lock, +2.5% Lock Cuan 1.2%)** sehingga posisi yang sempat profit terlindungi dari pembalikan harga mendadak.
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

### 2. Dynamic Take Profit (TP) per Asset Class (Ultra-Fast Scalp & High Turnover)
Mengunci keuntungan kecil secara berulang untuk mempercepat perputaran modal $15 agar segera cair kembali:
- **Crypto Tier-1 (BTC, ETH, SOL, BNB, XRP, AVAX, SUI, LINK, NEAR, AAVE):** TP +1.0%, SL 2.5% (cepat cair sebelum dump)
- **ETF / Saham Kalem (SPY, US100, AAPL):** TP +1.0%, SL 2.5%
- **Komoditas (XAU, XAG, BRENTOIL):** TP +1.2%, SL 3.0%
- **Saham Volatil (NVDA, TSLA, MSTR, CRCL, SNDK, VVV, dll):** TP +1.2%, SL 3.0%
- **Windfall Rule:** Jika keuntungan harga $\ge 2\times$ funding harian (>=0.8%), TP dimajukan lebih awal untuk mengunci profit multi-hari dalam hitungan jam.

### 3. Tangga Trailing Stop & Early Breakeven (Ultra-Fast Scalp — 4 Tiers)
- **Tier 1 (Profit $\ge +0.6\%$):** Lock SL otomatis ke `Entry + 0.20%` (**Zero-Risk:** modal dan fee taker 0.18% terkunci aman sejak dini!).
- **Tier 2 (Profit $\ge +1.0\%$):** Lock SL ke `Entry + 0.50%` (Kunci cuan awal).
- **Tier 3 (Profit $\ge +1.5\%$):** Lock SL ke `Entry + 1.00%` (Kunci cuan menengah).
- **Tier 4 (Profit $\ge +2.0\%$):** Trailing stop super rapat **0.5%** di bawah running price.

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

---

## ⏰ Jadwal Eksekusi Sistem

| Komponen / Workflow | Jadwal Cloud Scheduler (WIB) | Jadwal Cron GitHub (UTC) | Deskripsi |
|---|---|---|---|
| [`daily-scan.yml`](.github/workflows/daily-scan.yml) | **07:17 WIB** (Daily) | `17 0 * * *` | Scan seluruh kontrak, ranking peluang funding harian, riset Swarm AI, **eksekusi instan order bersinyal GO/HALF** ke Walt Perps, kirim push notif Telegram & simpan ke Google Sheet. |
| [`market-execute.yml`](.github/workflows/market-execute.yml) | **14:43 & 21:43 WIB** (Setiap Hari 24/7) | `43 7,14 * * *` | Eksekusi order saham US & komoditas secara kontinu 24/7 (dengan proteksi *anti-duplicate*). |
| [`crypto-execute.yml`](.github/workflows/crypto-execute.yml) | *Manual / Cron Independen* | `11 0,8,16 * * *` | Eksekusi posisi Crypto Bluechip (BTC, ETH, SOL) tiap 8 jam siklus funding. Dapat dimatikan/dinyalakan mandiri. |
| [`risk-monitor.yml`](.github/workflows/risk-monitor.yml) | **Tiap 1 Jam di menit 23** | `23 * * * *` | Evaluasi 4 lapis risiko, geser SL ke breakeven, pasang trailing stop, dan emergency close jika arah funding berbalik (*flip sign*). |

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
