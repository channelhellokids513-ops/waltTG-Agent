# Walt Perps Trading Bot — GitHub Actions

Fully autonomous funding rate arbitrage bot untuk Walt Perps, jalan di GitHub Actions cloud (nggak perlu PC kamu aktif).

## 🏗️ Arsitektur

```
GitHub Actions (cloud cron) → walt_bot.py → Walt MCP API (OAuth)
                                         ↘ Qwen DashScope (research)
                                         ↘ Telegram (notifications)
```

## 📂 File Structure

```
walt-trading/
├── STRATEGY.md           # dokumentasi strategi end-to-end
├── walt_bot.py           # main bot (scan, monitor, execute)
├── token_manager.py      # Walt OAuth token auto-refresh
├── llm_research.py       # Qwen client untuk news sentiment
├── notifier.py           # Telegram notification
├── setup_walt_oauth.py   # one-time: dapat refresh_token
├── requirements.txt
└── .github/workflows/
    ├── daily-scan.yml        # 06:39 WIB tiap hari
    ├── risk-monitor.yml      # tiap 4 jam
    └── market-execute.yml    # 21:40 WIB Mon-Fri (US market open)
```

## 🚀 Setup (Sekali)

### 1. Dapat Walt OAuth refresh_token
Jalankan di lokal:
```bash
cd walt-trading
pip install httpx
python setup_walt_oauth.py
```
Script akan:
1. Register OAuth client
2. Buka browser → kamu login Walt + authorize
3. Print `WALT_CLIENT_ID` + `WALT_REFRESH_TOKEN`

Copy keduanya untuk GitHub Secrets.

### 2. Dapat DashScope API key (Qwen)
Buka: https://modelstudio.console.alibabacloud.com/ap-southeast-1/model/settings/api-key
Generate API key baru → copy sebagai `DASHSCOPE_API_KEY`.

### 3. Setup Telegram bot (notifications)
1. Chat `@BotFather` di Telegram → `/newbot` → dapat `TELEGRAM_BOT_TOKEN`
2. Chat `@userinfobot` → dapat `TELEGRAM_CHAT_ID` kamu
3. Start chat dengan bot baru kamu (biar bisa kirim pesan)

### 4. Push ke GitHub (private repo)
```bash
git init
git add .
git commit -m "init walt trading bot"
gh repo create walt-trading-bot --private --source=. --push
```

### 5. Set GitHub Secrets
Di repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `WALT_CLIENT_ID` | dari step 1 |
| `WALT_REFRESH_TOKEN` | dari step 1 |
| `DASHSCOPE_API_KEY` | dari step 2 |
| `TELEGRAM_BOT_TOKEN` | dari step 3 |
| `TELEGRAM_CHAT_ID` | dari step 3 |

Opsional (sebagai variable, bukan secret):
| Variable | Value |
|---|---|
| `DASHSCOPE_MODEL` | `qwen-plus` (default), bisa `qwen-turbo`, `qwen-max` |

### 6. Test
Di repo GitHub → Actions tab → pilih workflow → "Run workflow" (manual trigger).

Kalau sukses, cek Telegram untuk notif.

## ⏰ Jadwal Default

| Workflow | Cron (UTC) | Jadwal Lokal (Asia/Jakarta) |
|---|---|---|
| daily-scan.yml | `39 23 * * *` | 06:39 WIB tiap hari |
| risk-monitor.yml | `35 */4 * * *` | 00:35, 04:35, 08:35, 12:35, 16:35, 20:35 WIB |
| market-execute.yml | `40 14 * * 1-5` | 21:40 WIB Mon-Fri |

## 🛡️ Security Notes

- Semua secret disimpan di GitHub Secrets (encrypted at rest)
- `walt_bot.py` akses Walt API via Bearer token OAuth, bukan password
- Walt API scope: `perps.trade` (dibatasi ke perps, nggak akses main wallet)
- `.github/workflows/` gratis untuk private repo (2000 menit/bulan limit — jauh lebih dari cukup)

## 🧪 Local Testing

Buat file `.env` lokal (JANGAN commit):
```env
WALT_CLIENT_ID=...
WALT_REFRESH_TOKEN=...
DASHSCOPE_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Test:
```bash
# Load env
export $(cat .env | xargs)

# Daily scan with research
python walt_bot.py scan_with_research

# Risk monitor
python walt_bot.py monitor

# Market execute (dry-run)
DRY_RUN=true python walt_bot.py market_execute
```

## 🐛 Troubleshooting

**"401 Unauthorized" dari Walt:**
- Refresh token mungkin expired. Jalankan `setup_walt_oauth.py` lagi, update secret `WALT_REFRESH_TOKEN`.

**"WALT_CLIENT_ID env not set":**
- Belum set GitHub Secret, atau workflow tidak mendefinisikan env mapping.

**Scheduled task nggak jalan tepat waktu:**
- GitHub cron punya delay 5-20 menit normal. Untuk eksekusi presisi (misal market open), buffer 10-15 menit cukup.

**"agent.incorrect.order_params" saat execute:**
- TP/SL di luar buffer kontrak. Bot akan fallback: open tanpa autoClose, lalu set terpisah.

## 📊 Monitoring

- **GitHub Actions UI** → tab Actions untuk lihat log tiap run
- **Telegram** → notifikasi tiap scan/monitor/execute
- Email alert kalau workflow fail: enable di repo → Settings → Notifications

## 🔄 Rotation & Maintenance

Refresh token Walt bisa di-rotate kapan saja. Kalau `token_manager.py` print:
```
⚠️  Refresh token di-rotate oleh Walt. Update GitHub Secret WALT_REFRESH_TOKEN:
    <new_token>
```
Update manual via GitHub UI.

Untuk zero-maintenance, bisa implement auto-update secret via GitHub API (nanti).
