# Product Requirements Document (PRD)
# Walt Perps Trading Bot — Phased Evolution: Owner Core & Multi-User SaaS Platform

**Version:** 2.1 (Phased Roadmap Edition)  
**Date:** 2026-11-14  
**Platform Owner & Lead Architect:** Rizal Dicky  
**Status:** In Implementation (Fase 1: Immediate / Fase 2: SaaS Expansion)  

---

## 1. Executive Summary

Dokumen ini mendefinisikan peta jalan pengembangan **Walt Perps Trading Bot** melalui pendekatan bertahap (*phased rollout*). Bot ini berfokus pada strategi *funding rate arbitrage* (delta-neutral dan directional funding capture) di platform Walt (`walletbot.me`) dengan perbantuan riset kecerdasan buatan kolaboratif (**Dual-AI Consensus: Alibaba Cloud Qwen & Google Gemini**).

Untuk menyeimbangkan antara **kebutuhan eksekusi segera** dan **potensi komersialisasi masa depan**, peta jalan dibagi menjadi 2 fase utama:

1. **Fase 1: Owner-Dedicated Core Bot (Infrastruktur Eksisting & Segera Diterapkan)**  
   - Menggunakan infrastruktur eksisting yang sudah berjalan: Python 3.12, GitHub Actions cloud cron, Dual-AI Consensus (Qwen-Plus + Google Gemini via `DASHSCOPE_API_GEMINI_KEY`), dan notifikasi Telegram terarah.  
   - **Eksklusif untuk Platform Owner (Rizal Dicky)**: Menghasilkan *passive income* harian ($50–$100/bulan) dari modal pribadi, memverifikasi stabilitas eksekusi Walt API, dan memperketat risk guardrails dengan biaya infrastruktur $0 (memanfaatkan GitHub Actions free tier).  
   - **Status:** **Prioritas Utama — Siap dan Perlu Segera Diterapkan.**

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
| **Bahasa & Runtime** | Python 3.12 (`walt_bot.py`, `token_manager.py`) | TypeScript / Node.js 20 + Client WebApp (React/Vite) |
| **Mekanisme Scheduler** | GitHub Actions Cron (`.github/workflows/*.yml`) | Google Cloud Scheduler + Pub/Sub (WIB Precision) |
| **Basis Data & State** | Statless / GitHub Actions Artifacts / Memory Cache | Google Cloud Firestore (Multi-Tenant Document Store) |
| **Kredensial & Secrets** | GitHub Repository Secrets & Local `.env` | AES-256-GCM Vault di Firestore + Google Cloud KMS |
| **Riset AI (Dual-AI)** | Qwen-Plus + Google Gemini Consensus per-run | *Shared Dual-AI Engine* (1 pemanggilan global untuk semua user) |
| **Antarmuka Utama** | Notifikasi Broadcast Telegram ke `TELEGRAM_CHAT_ID` | **Dashboard Interaktif (Telegram Mini App)** + Menu Bot |
| **Sistem Bisnis / Billing**| $0 (Self-funded dari hasil funding rate pribadi) | Pembayaran Otomatis: QRIS, Crypto (USDT), Telegram Stars |
| **Koneksi Walt** | 1 Akun Walt Owner via OAuth Refresh Token | Multi-Tenant Dynamic OAuth PKCE per Pengguna |
| **Biaya Infrastruktur** | **Rp 0 / $0 per bulan** (Free tier GitHub Actions) | ~$15–$25 / bulan (Ditutupi oleh subscription revenue) |
| **Target Peluncuran** | **Minggu Ini (Segera Diterapkan)** | Q1 2027 (Peluncuran Komersial Bertahap) |

---

## 3. Fase 1: Owner-Dedicated Core Bot (Infrastruktur Eksisting)

### 3.1 Prinsip Desain Fase 1
- **Zero Rework / Zero Latency:** Memaksimalkan codebase Python yang telah selesai (`walt_bot.py`, `token_manager.py`, `llm_research.py`, `notifier.py`, `setup_walt_oauth.py`).
- **Zero Cost:** Sepenuhnya beroperasi di atas kuota gratis GitHub Actions (2.000 menit/bulan untuk private repository) dan free credits DashScope.
- **Fokus Tunggal:** Menangkap peluang funding rate arbitrage secara konsisten untuk portofolio Owner dengan manajemen risiko konservatif.

### 3.2 Diagram Alur Kerja Fase 1 (Infrastruktur Eksisting)

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        FASE 1: OWNER RUNTIME WORKFLOW                                  │
└────────────────────────────────────────────────────────────────────────────────────────┘

    GitHub Actions Scheduled Cron
    ├─ 06:39 WIB (daily-scan.yml)
    ├─ Tiap 4 Jam (risk-monitor.yml)
    └─ 21:40 WIB Mon-Fri (market-execute.yml)
                   │
                   ▼
     ┌───────────────────────────┐
     │  GitHub Actions Runner    │
     │  (Ubuntu / Python 3.12)   │
     └─────────────┬─────────────┘
                   │
         [Inject Secrets via Env]
         • WALT_CLIENT_ID
         • WALT_REFRESH_TOKEN
         • DASHSCOPE_API_KEY
         • TELEGRAM_BOT_TOKEN
         • TELEGRAM_CHAT_ID
                   │
                   ▼
     ┌───────────────────────────┐
     │   walt_bot.py (Orchestrator)
     │                           │
     │   1. token_manager.py ────┼───> Walt OAuth Server (Get/Refresh Access Token)
     │   2. Walt Perps API ──────┼───> Fetch 127 Contracts, Volume & Funding Rates
     │   3. llm_research.py ─────┼───> DashScope Qwen-Plus (Sentiment Filter & Thesis)
     │   4. Execute Position ────┼───> Walt Perps Trade API (Open Short/Long @ 2x Lev)
     │   5. notifier.py ─────────┼───> Telegram Bot API
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
1. **Daily Scan (`daily-scan.yml` — 06:39 WIB / 23:39 UTC):**
   - Mengambil seluruh 127 kontrak perpetual di Walt.
   - Menyaring volume 24 jam > $1.000.000 USD.
   - Menghitung *annualized funding rate* untuk SHORT (funding positif tinggi) dan LONG (funding negatif ekstrim).
   - Memanggil `llm_research.py` (Qwen-Plus) untuk menganalisis sentimen berita & makro aset pilihan.
   - Mengirimkan rangkuman terstruktur peluang terbaik langsung ke Telegram Owner.
2. **Risk Monitor (`risk-monitor.yml` — Tiap 4 Jam):**
   - Mengecek seluruh posisi aktif Owner.
   - Menghitung akumulasi funding rate yang sudah diperoleh.
   - Memverifikasi rasio margin dan batas likuidasi.
   - Mengirim peringatan jika rasio risiko mendekati ambang batas (*buffer* minimum 15%).
3. **Market Execute (`market-execute.yml` — 21:40 WIB Senin–Jumat):**
   - Bertepatan dengan pembukaan pasar saham AS (US Market Open).
   - Membuka posisi terukur pada saham US (misal: NVDA, MSTR, PLTR, TSLA) atau komoditas (XAU, XAG, BRENTOIL).
   - Parameter ketat: Leverage maksimal 2x, alokasi margin $2.50 – $3.00 USDC per posisi, maksimal 2–4 posisi simultan.
   - Penanganan fallback: Jika order dengan TP/SL gagal karena buffer exchange, bot mengeksekusi order terbuka dan mengatur penutupan terpisah.

### 3.4 Langkah Penerapan Segera (Fase 1 Action Plan)
- [x] Pastikan GitHub Secret terkonfigurasi lengkap di repository (`WALT_CLIENT_ID`, `WALT_REFRESH_TOKEN`, `DASHSCOPE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
- [ ] Lakukan verifikasi uji coba manual (*manual trigger*) untuk workflow `daily-scan.yml`, `risk-monitor.yml`, dan `market-execute.yml` (mode `DRY_RUN=true`).
- [ ] Aktifkan mode eksekusi riil pada saldo terdedikasi di Walt Perps.
- [ ] Pantau notifikasi Telegram selama 7 hari berturut-turut untuk kalibrasi funding rate capture dan stabilitas rotasi token.

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
      │   │      - News Sentiment (3x Qwen Consensus)       │   │
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

| ID | Modul / Fungsi | Deskripsi Teknis | Prioritas |
|---|---|---|---|
| **F1-01** | Daily Scheduled Scan | Eksekusi harian 06:39 WIB via `daily-scan.yml` untuk memindai 127 kontrak Walt. | **P0 (Segera)** |
| **F1-02** | Automated Token Refresh | `token_manager.py` memperbarui Bearer token secara otomatis sebelum tiap eksekusi. | **P0 (Segera)** |
| **F1-03** | Token Rotation Alert | Mengirim alert darurat ke Telegram jika Walt me-rotate refresh token. | **P0 (Segera)** |
| **F1-04** | AI Sentiment Research | Pemanggilan Qwen-Plus (`llm_research.py`) untuk memvalidasi sentimen aset top funding. | **P0 (Segera)** |
| **F1-05** | US Market Execution | Eksekusi otomatis 21:40 WIB Senin–Jumat (`market-execute.yml`) pada saham/komoditas. | **P0 (Segera)** |
| **F1-06** | Continuous Risk Monitor | Eksekusi tiap 4 jam (`risk-monitor.yml`) untuk memantau margin ratio & funding capture. | **P0 (Segera)** |
| **F1-07** | Owner Telegram Alert | Rangkuman scan, konfirmasi order, dan log PnL dikirim langsung ke `TELEGRAM_CHAT_ID`. | **P0 (Segera)** |
| **F1-08** | Order Fallback Handling | Fallback otomatis jika order autoClose ditolak parameter exchange (buka dulu, set SL terpisah). | **P0 (Segera)** |

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
- [ ] Workflow GitHub Actions (`daily-scan.yml`, `risk-monitor.yml`, `market-execute.yml`) berjalan otomatis sesuai jadwal tanpa intervensi manual.
- [ ] Bot berhasil membuka dan menutup posisi funding rate arbitrage di akun Walt milik Owner dengan leverage maksimal 2x.
- [ ] Notifikasi harian, rincian perolehan funding rate, dan alert margin terkirim akurat ke Telegram pribadi Owner.
- [ ] Mekanisme pembaruan token di `token_manager.py` berjalan mulus tanpa kegagalan otentikasi HTTP 401.
- [ ] Menghasilkan pendapatan pasif stabil dengan target $50–$100/bulan dari akun pribadi Owner.

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
