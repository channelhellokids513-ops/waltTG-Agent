"""
Walt Perps Funding Arbitrage Bot — End-to-End Implementation
=============================================================

Implementasi dari STRATEGY.md:
- Pemilihan aset (saham US + komoditas, skip crypto)
- Position sizing & leverage (max 2x, margin $2.50-$3)
- Entry rules + TP/SL baseline
- Risk monitor 4 lapis (baseline / funding / volatilitas / trailing)
- Scheduler-friendly: fungsi utama bisa dipanggil terpisah untuk cron

Pemilik: Rizal Dicky
Target: $100/bulan dari funding income
"""

from __future__ import annotations

import os
import csv
import math
import time
import json
import random
import logging
import re  # Ronde 8: probe struktur respons close (belum pernah diimpor di berkas ini)
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import httpx  # pip install httpx

from token_manager import TokenManager
import account_lock
from llm_research import (
    QwenClient,
    GeminiClient,
    research_stock,
    Research,
    fetch_earnings_days_with_status,
)
from notifier import send_telegram, md_escape, md_italic


# =============================================================================
# KONFIGURASI
# =============================================================================

WALT_MCP_URL = "https://walletbot.me/bot-relay/public-api/mcp"
# OAuth via TokenManager — butuh env WALT_CLIENT_ID + WALT_REFRESH_TOKEN

# Universe aset yang diperbolehkan (saham US + komoditas + crypto bluechip)
ALLOWED_STOCKS = {
    "NVDA", "NBIS", "CRCL", "MSTR", "AAPL", "GOOGL", "AMD", "PLTR",
    "COIN", "SNDK", "SPY", "US100", "AMZN", "TSLA", "META", "LIT", "VVV",
    "ANTHROPIC",
}
ALLOWED_COMMODITIES = {"XAU", "XAG", "BRENTOIL"}
ALLOWED_CRYPTO = {
    "BTC", "ETH", "SOL",            # Bluechip
    "BNB", "XRP", "AVAX",           # Tier-1 Large Cap
    "SUI", "LINK", "NEAR", "AAVE",  # High-Liquidity Ecosystem Leaders
}
ALLOWED_ASSETS = ALLOWED_STOCKS | ALLOWED_COMMODITIES | ALLOWED_CRYPTO

# Ronde 8 (permintaan pemilik): AI swarm dulu hanya dinilai untuk saham; crypto & komoditas
# dipotong langsung GO tanpa riset. Sekarang mereka ikut dinilai, TETAPI dengan aturan yang
# sama pentingnya: AI hanya boleh MENURUNKAN (HALF/SKIP), tidak pernah menciptakan entry, dan
# riset yang hilang untuk crypto TIDAK memblokir (jalur baca riset sedang sakit - memblokir
# berarti berhenti trading diam-diam). Saham tetap wajib riset segar (E3 tidak berubah).
RESEARCH_ELIGIBLE = ALLOWED_ASSETS

# Position sizing
MAX_LEVERAGE = 2
DEFAULT_MARGIN = 2.50       # USDC per posisi minimum (floor)
MARGIN_RANGE = (2.50, 3.00)
MIN_BUFFER_PCT = 0.15       # 15% dari saldo tidak boleh dipakai (cash buffer)
SAFE_HAVEN_ASSETS = {"BTC", "ETH", "SPY", "QQQ", "XAU", "AAPL"}


def get_total_equity(balance_data: Any, positions: Any = None) -> float:
    """
    Hitung total ekuitas = balance_available + jumlah margin_quote dari posisi open.
    Mendukung input balance_data berupa dict {'accounts': [...]}, list accounts, atau float.
    """
    available = 0.0
    if isinstance(balance_data, (int, float)):
        available = float(balance_data)
    elif isinstance(balance_data, dict):
        accounts = balance_data.get("accounts", [])
        if accounts:
            available = float(accounts[0].get("balance_available", 0.0))
        elif "balance_available" in balance_data:
            available = float(balance_data.get("balance_available", 0.0))
    elif isinstance(balance_data, list) and balance_data:
        available = float(balance_data[0].get("balance_available", 0.0))

    pos_list = []
    if isinstance(positions, dict):
        pos_list = positions.get("positions", [])
    elif isinstance(positions, list):
        pos_list = positions

    open_margin = sum(
        float(p.get("margin_quote", p.get("margin", 0.0)) or 0.0)
        for p in pos_list
        if isinstance(p, dict) and str(p.get("status", "open")).lower() in ("open", "active")
    )
    return round(available + open_margin, 6)


def get_dynamic_max_slots(available_balance: float) -> int:
    """
    Hitung batas maksimal slot posisi aktif secara elastis berdasarkan ekuitas:
    - Modal mikro (< $30): 5 slot (konservatif & aman dari margin call).
    - Modal menengah ($30 - $75): 5 - 8 slot.
    - Modal berkembang ($75 - $150): 8 - 12 slot.
    - Modal mapan ($150+): hingga 15 slot maksimal (diversifikasi luas).
    """
    if available_balance < 30.0:
        return 5
    elif available_balance < 75.0:
        return min(8, max(5, int(available_balance / 7.0)))
    elif available_balance < 150.0:
        return min(12, max(8, int(available_balance / 10.0)))
    else:
        return min(15, max(10, int(available_balance / 15.0)))


def calculate_dynamic_margin(
    available_balance: float,
    active_slots: int = 0,
    max_slots: Optional[int] = None,
    opp: Optional[Any] = None,
    research: Optional[Any] = None,
    verdict_mult: float = 1.0,
    total_equity: Optional[float] = None,
) -> float:
    """
    Kalkulasi ukuran margin per posisi berbasis Auto-Compounding & Conviction Sizing:
    1. Base Margin:
       - 85% available balance dialokasikan untuk trading (15% cash buffer).
       - Modal dibagi rata ke kuota slot kosong yang tersisa (max_slots elastis 5 s.d 15).
       - Batas bawah $2.50 USDC (floor exchange).
    2. Conviction Multipliers:
       - Funding Rate magnitude:
         * abs(funding_daily) >= 0.008 (>= 0.8%/hari): 1.35x
         * abs(funding_daily) >= 0.004 (>= 0.4%/hari): 1.15x
         * normal (< 0.4%/hari): 1.0x
       - AI Swarm Consensus:
         * UNANIMOUS atau skor final >= +0.60: 1.25x
         * STRONG atau skor final +0.20 s/d +0.59: 1.0x
         * MIXED / SPLIT atau skor final < +0.20: 0.75x
       - Asset Safety Tier:
         * Safe-haven / Liquid (BTC, ETH, SPY, XAU, QQQ, AAPL): 1.15x
         * High-growth / Altcoins: 1.0x
       - Verdict Multiplier (GO = 1.0x, HALF = 0.5x, dll): verdict_mult
    3. Portfolio Risk Constraints:
       - Single Position Hard Ceiling: Maksimal 15% dari total ekuitas (atau floor $2.50).
       - Floor: $2.50 USDC minimum order bursa.
    """
    equity = total_equity if (total_equity is not None and total_equity > 0) else available_balance
    if max_slots is None:
        max_slots = get_dynamic_max_slots(equity)

    remaining_slots = max(1, max_slots - active_slots)
    deployable = available_balance * (1.0 - MIN_BUFFER_PCT)
    if deployable <= 0:
        return DEFAULT_MARGIN

    base_margin = deployable / remaining_slots
    mult = float(verdict_mult)

    if opp:
        # Funding rate magnitude
        funding_daily = getattr(opp, "funding_daily", 0.0)
        funding_abs = abs(funding_daily)
        if funding_abs >= 0.008:
            mult *= 1.35
        elif funding_abs >= 0.004:
            mult *= 1.15

        # Asset safety tier
        base_asset = getattr(opp, "base_asset", "")
        if base_asset in SAFE_HAVEN_ASSETS:
            mult *= 1.15

    if research:
        score = getattr(research, "final_score", 0.0)
        consensus = getattr(research, "overall_consensus_level", "")
        if consensus == "UNANIMOUS" or score >= 0.60:
            mult *= 1.25
        elif consensus == "STRONG" or (0.20 <= score < 0.60):
            mult *= 1.0
        elif consensus in ("MIXED", "SPLIT") or score < 0.20:
            mult *= 0.75

    sized_margin = base_margin * mult

    # Single position risk ceiling: max 15% dari total ekuitas (tetap izinkan floor $2.50 untuk GO)
    single_position_cap = max(DEFAULT_MARGIN, round(equity * 0.15, 2))
    final_margin = min(sized_margin, single_position_cap)

    # E1: HALF yang benar-benar HALF — jika setelah multiplier < floor $2.50, JANGAN dinaikkan ke floor
    if float(verdict_mult) < 1.0 and round(final_margin, 2) < DEFAULT_MARGIN:
        return 0.0

    return max(DEFAULT_MARGIN, round(final_margin, 2))

# Filter likuiditas & Cooldown
MIN_VOL_24H = 1_000_000     # $1M
MIN_FUNDING_DAILY = 0.005   # 0.5%/hari
FUNDING_FLIP_CLOSE_THRESHOLD = 0.001  # 0.1%/hari hysteresis untuk tutup posisi saat funding flip
REENTRY_COOLDOWN_SEC = 3 * 3600       # Cooldown 3 jam pasca penutupan aset (cegah whipsaw & boros fee)

# Target income
MONTHLY_TARGET = 100.0      # USDC

# Risk management parameters (True Funding Carry + Asymmetric R:R)
DEFAULT_SL_PCT = 0.015      # 1.5% default SL (beri ruang napas noise intraday, R:R >= 1:1.33)
DEFAULT_TP_PCT = 0.025      # 2.5% default TP (biarkan profit berlari & tutup fee jauh)
DEFAULT_CALM_TP_PCT = 0.020 # 2.0% default TP ETF & Crypto
MAX_P5_RISK = 0.70          # E2: P5_risk_catalysts >= 0.70 -> SKIP
MAX_ENTRIES_PER_ASSET_24H = 2   # E4: maks 2 entry per aset per 24 jam
MAX_ENTRIES_PER_DAY = 8         # E4: maks 8 entry total per 24 jam
DAILY_LOSS_LIMIT_PCT = 0.05     # E4: circuit breaker penurunan ekuitas harian >= 5%
EXPECTED_HOLD_HOURS = 24.0      # E5: asumsi hold time (jam) untuk gerbang funding vs fee

try:
    from strategy_config import load_strategy_config as _load_strategy_config
    _rt_cfg = _load_strategy_config()
    DEFAULT_SL_PCT = float(_rt_cfg.get("default_sl_pct", DEFAULT_SL_PCT))
    DEFAULT_TP_PCT = float(_rt_cfg.get("tp_volatile_pct", DEFAULT_TP_PCT))
    DEFAULT_CALM_TP_PCT = float(_rt_cfg.get("tp_calm_pct", DEFAULT_CALM_TP_PCT))
    MIN_FUNDING_DAILY = float(_rt_cfg.get("min_funding_daily", MIN_FUNDING_DAILY))
    REENTRY_COOLDOWN_SEC = int(float(_rt_cfg.get("reentry_cooldown_hours", 3.0)) * 3600)
    MAX_P5_RISK = float(_rt_cfg.get("max_p5_risk", MAX_P5_RISK))
    MAX_ENTRIES_PER_ASSET_24H = int(_rt_cfg.get("max_entries_per_asset_24h", MAX_ENTRIES_PER_ASSET_24H))
    MAX_ENTRIES_PER_DAY = int(_rt_cfg.get("max_entries_per_day", MAX_ENTRIES_PER_DAY))
    DAILY_LOSS_LIMIT_PCT = float(_rt_cfg.get("daily_loss_limit_pct", DAILY_LOSS_LIMIT_PCT))
    EXPECTED_HOLD_HOURS = float(_rt_cfg.get("expected_hold_hours", EXPECTED_HOLD_HOURS))
except Exception as _cfg_exc:
    logging.getLogger("walt-bot").warning(
        f"Gagal memuat konfigurasi strategi bersama ({_cfg_exc}); fallback ke DEFAULT_STRATEGY_CONFIG."
    )

# Tangga Trailing Stop Progresif Baseline (ruang napas lebih lega agar tidak mudah tersapu noise)
TRAILING_TIERS = [
    (0.0100, "lock_0035"),  # Tier 1: profit >= +1.00% → SL di entry +0.35% (ruang napas 0.65%)
    (0.0180, "lock_0095"),  # Tier 2: profit >= +1.80% → SL di entry +0.95% (ruang napas 0.85%)
    (0.0250, 0.0070),       # Tier 3: profit >= +2.50% → Trailing stop 0.70% di bawah running price
]
_DEFAULT_TRAILING_TIERS = list(TRAILING_TIERS)

DYNAMIC_SL_MODE_ENV = "DYNAMIC_SL_MODE"
DYNAMIC_SL_ATR_MULT = 0.85
DYNAMIC_SL_CALM_CAP_PCT = 0.025        # Maks 2.5% untuk aset kalem (SPY, US100, AAPL, XAU)
DYNAMIC_SL_VOLATILE_CAP_PCT = 0.035    # Maks 3.5% untuk aset volatil & crypto
DYNAMIC_TP_RR_MULT = 1.35              # TP minimal 1.35x SL dinamis (menjaga R:R asimetris)
L2_BREAKEVEN_MIN_PROFIT_PCT = 0.0050   # Syarat profit minimal +0.50% sebelum L2 geser SL ke entry
CALM_SL_ASSETS: frozenset[str] = frozenset({"SPY", "US100", "AAPL", "XAU"})


def round_price(val: float) -> float:
    """Bulatkan harga adaptif: 4 desimal >= $1, 6 desimal $0.01-$1, 8 desimal < $0.01."""
    aval = abs(float(val))
    if aval >= 1.0:
        return round(float(val), 4)
    if aval >= 0.01:
        return round(float(val), 6)
    return round(float(val), 8)


def is_dynamic_sl_enabled() -> bool:
    """True bila SL dinamis berbasis ATR aktif (DYNAMIC_SL_MODE != fixed/off)."""
    return os.getenv(DYNAMIC_SL_MODE_ENV, "atr").strip().lower() not in (
        "fixed", "off", "false", "0", "no", "disabled"
    )


def get_asset_baseline_tpsl_pct(asset: Optional[str] = None) -> tuple[float, float]:
    """
    Return (tp_pct, sl_pct) dengan Asymmetric R:R (TP >= 1.33x SL) agar surplus bersih setelah fee:
    - SL seragam 1.5% (0.015) untuk semua kelas aset guna menghindari whipsaw noise intraday.
    - ETF / Saham Kalem (SPY, US100, AAPL): TP +2.0%, SL 1.5%
    - Crypto Tier-1 (BTC, ETH, SOL, BNB, XRP, AVAX, SUI, LINK, NEAR, AAVE): TP +2.0%, SL 1.5%
    - Komoditas (XAU, XAG, BRENTOIL): TP +2.5%, SL 1.5%
    - Saham Volatil (NVDA, TSLA, MSTR, CRCL, SNDK, VVV, dll): TP +2.5%, SL 1.5%
    """
    if not asset:
        return DEFAULT_TP_PCT, DEFAULT_SL_PCT
    asset_up = asset.upper()
    if asset_up in {"SPY", "US100", "AAPL"}:
        return DEFAULT_CALM_TP_PCT, DEFAULT_SL_PCT
    elif asset_up in ALLOWED_CRYPTO:
        return DEFAULT_CALM_TP_PCT, DEFAULT_SL_PCT
    elif asset_up in ALLOWED_COMMODITIES:
        return DEFAULT_TP_PCT, DEFAULT_SL_PCT
    else:
        return DEFAULT_TP_PCT, DEFAULT_SL_PCT

# Logging dengan dukungan LOG_REDACT untuk repo publik
import re as _re


def is_log_redact_enabled() -> bool:
    return os.getenv("LOG_REDACT", "false").strip().lower() in ("1", "true", "yes", "on")


_REDACT_PATTERNS = [
    (_re.compile(r"(?i)(\b(?:balance(?:\s+available)?|equity|available|margin|pnl|realized(?:\s+pnl)?|unrealized(?:\s+pnl)?|buffer|saldo)\s*[:=]?\s*)(?:[+-]?\$[+-]?[0-9,.]+(?:/[+-]?\$[+-]?[0-9,.]+)?)"), r"\1***"),
    (_re.compile(r"(?i)(\border_id\s*[:=]\s*)([a-zA-Z0-9_-]+)"), r"\1***"),
    (_re.compile(r"(?i)('sheet_id'\s*:\s*')([^']+)(')"), r"\1***\3"),
    (_re.compile(r"(?i)\b([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b"), "***@***"),
]


_SECRET_REDACT_PATTERNS = [
    (_re.compile(r"(?i)([?&](?:key|api_key|token|access_token|secret)=)([^&\s'\"]+)"), r"\1***"),
    (_re.compile(r"(?i)(\bBearer\s+)([A-Za-z0-9._~+/=-]{8,})"), r"\1***"),
    (_re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "AIza***"),
    (_re.compile(r"\bsk-[0-9A-Za-z_-]{16,}\b"), "sk-***"),
    (_re.compile(r"\b(?:ghp|gho|ghu|ghs)_[0-9A-Za-z]{20,}\b"), "gh_***"),
    (_re.compile(r"\bgithub_pat_[0-9A-Za-z_]{20,}\b"), "github_pat_***"),
    (_re.compile(r"\bbot\d{6,}:[0-9A-Za-z_-]{20,}\b"), "bot***"),
]


def redact_secrets(text: str) -> str:
    """Selalu sensor pola mirip API key / Bearer token / bot token di log stdout."""
    out = text
    for pat, repl in _SECRET_REDACT_PATTERNS:
        out = pat.sub(repl, out)
    return out


def redact_sensitive_text(text: str) -> str:
    """Samarkan angka saldo/margin/PnL/order_id/email + secret pada log stdout."""
    out = redact_secrets(text)
    for pat, repl in _REDACT_PATTERNS:
        out = pat.sub(repl, out)
    return out


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        rendered = redact_secrets(rendered)
        return redact_sensitive_text(rendered) if is_log_redact_enabled() else rendered


_handler = logging.StreamHandler()
_handler.setFormatter(_RedactingFormatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
log = logging.getLogger("walt-bot")


# =============================================================================
# STATE WARMTH (R6-2) — deteksi cache-miss Actions agar start dingin tidak menyamar
# =============================================================================

STATE_WARM_ENV = "WALT_STATE_WARM"
ALLOW_COLD_ENTRY_ENV = "WALT_ALLOW_COLD_ENTRY"
_cold_state_alerted: set[str] = set()


_WARM_TRUTHY = ("1", "true", "yes", "on")


def is_state_warm() -> bool:
    """
    True HANYA bila ada bukti kehangatan yang eksplisit: `WALT_STATE_WARM` diisi `true` oleh
    langkah `state_probe` workflow (atau oleh probe CLI di bawah).

    Default lama "true" adalah fail-open dan sudah dibuktikan berbahaya: marker yang hilang atau
    string kosong (mis. `state_probe` gagal, atau langkah baru lupa meneruskan output probe) membuat
    gerbang start-dingin dan proteksi deteksi-close mati tanpa suara. Prinsip ronde ini:
    **ketiadaan bukti = dingin**, dan dingin selalu boleh dibuka sadar-sadar lewat
    `WALT_ALLOW_COLD_ENTRY` atau dengan ditemukannya memori risiko nyata.
    """
    raw = (os.getenv(STATE_WARM_ENV) or "").strip().lower()
    return raw in _WARM_TRUTHY


def probe_local_state_warm(regime_path: str = "logs/market_regime.json",
                           snapshot_path: str = "logs/open_snapshot.json") -> str:
    """
    Setara dengan `state_probe` di CI untuk pemakaian CLI lokal: kehangatan disimpulkan dari berkas
    state yang benar-benar ada, bukan dari asumsi. Return "true"/"false" (format GITHUB_OUTPUT).
    """
    return "true" if (os.path.exists(regime_path) and os.path.exists(snapshot_path)) else "false"


def cold_start_override_enabled() -> bool:
    """Override sadar untuk keadaan darurat: WALT_ALLOW_COLD_ENTRY=true mengizinkan entry dingin."""
    return (os.getenv(ALLOW_COLD_ENTRY_ENV, "").strip().lower() in ("1", "true", "yes", "on"))


def risk_memory_available(regime_path: str = "logs/market_regime.json") -> bool:
    """
    False hanya bila benar-benar tidak ada memori risiko: file tidak ada ATAU kosong
    tanpa cooldown, tanpa riwayat entry, dan tanpa jangkar circuit breaker.
    """
    state = _load_execution_regime(regime_path)
    if not isinstance(state, dict) or not state:
        return False
    if isinstance(state.get("cooldowns"), dict) and state["cooldowns"]:
        return True
    if isinstance(state.get("entry_history_24h"), list) and state["entry_history_24h"]:
        return True
    if isinstance(state.get("daily_circuit_breaker"), dict) and state["daily_circuit_breaker"]:
        return True
    return False


def check_cold_state_entry_gate(regime_path: str = "logs/market_regime.json") -> tuple[bool, str]:
    """
    Gerbang entry saat start dingin. Aturan desainnya: **kehilangan memori risiko harus mengurangi
    eksposur, bukan menambah** — karena counter 8 entry/hari & 2 entry/aset bisa kembali nol dan
    jangkar daily circuit breaker hilang, entry baru DILEWATI sampai state hangat lagi.

    Return (allowed, reason).
    """
    if is_state_warm():
        return True, "OK"
    if cold_start_override_enabled():
        return True, "state dingin tapi override WALT_ALLOW_COLD_ENTRY aktif (atas keputusan pemilik)"
    if risk_memory_available(regime_path):
        return True, "OK (memori risiko ditemukan walau cache miss)"
    return False, "state dingin: batas harian/cooldown tidak diketahui"


def _notify_cold_state_once(reason: str) -> None:
    """Satu alert Telegram per alasan per proses (bukan per kandidat, bukan 8 alert)."""
    if not reason or reason in _cold_state_alerted:
        return
    _cold_state_alerted.add(reason)
    log.warning(f"🧊 [STATE DINGIN] {reason} — entry baru dilewati untuk siklus ini")
    try:
        send_telegram(
            f"🧊 *STATE DINGIN TERDETEKSI*\n{md_escape(reason)}\n"
            "Entry baru *dilewati* sampai cache state pulih (aturan: kehilangan memori risiko "
            "mengurangi eksposur).\n"
            "Bila ini memang disengaja, set variable `WALT_ALLOW_COLD_ENTRY=true`."
        )
    except Exception as exc:
        log.warning(f"Gagal kirim alert state dingin ({type(exc).__name__}): {exc}")


def reset_cold_state_alert_state() -> None:
    """Kosongkan jendela dedupe alert (dipakai test; di CI satu proses = satu siklus)."""
    _cold_state_alerted.clear()


def rehydrate_closed_keys_from_exchange(client: WaltClient) -> set[str]:
    """
    Bangun kembali kunci dedupe dari riwayat closed bursa (read-only, SATU skema kunci dengan
    jalur live karena memakai build_backfill_rows / make_position_dedupe_key).
    """
    resp = client.list_positions(statuses=["closed"])
    return {row["position_id"] for row in build_backfill_rows(_extract_positions(resp))}


def _log_failure_summary(
    context: str,
    failures: list[str],
    total: int,
    warn_ratio: float = 0.3,
    warn_min: int = 3,
) -> None:
    """
    Agregasi kegagalan per-item menjadi tepat satu baris log.warning (maks 5 contoh + '...'),
    dan kirim maksimal satu alert Telegram bila jumlah >= warn_min dan rasio >= warn_ratio.
    """
    if not failures:
        return
    count = len(failures)
    sample = ", ".join(failures[:5])
    if count > 5:
        sample = f"{sample}, ..."
    denom = max(1, int(total))
    ratio = count / denom
    msg = f"[{context}] {count}/{denom} item gagal ({ratio*100:.0f}%): {sample}"
    log.warning(msg)
    if count >= warn_min and ratio >= warn_ratio:
        try:
            send_telegram(f"⚠️ {msg}")
        except Exception as exc:
            log.debug(f"Gagal kirim alert agregasi Telegram ({context}) ({type(exc).__name__}): {exc}")


# --- Register kesehatan input (Ronde 7) --------------------------------------
# Prinsip: "run success" harus berarti "semua input yang dipakai benar-benar hidup".
# Kegagalan voter, sumber earnings, dan objek riset yang hilang dicatat per siklus, lalu
# dikeluarkan SEKALI sebagai baris ringkasan (via _log_failure_summary) + blok di Step Summary.
# Tanpa ini, swarm 3/4 voter atau kalender earnings 401 tetap menghasilkan siklus hijau.
_INPUT_FAILURES: dict[str, list[str]] = {}
_INPUT_PROBED: dict[str, int] = {}

# Status earnings yang berarti sumber datanya terganggu (bukan sekadar kondisi data).
EARNINGS_OUTAGE_PREFIXES = ("http_error", "exception", "no_crumb")


def record_input_failure(context: str, item: str) -> None:
    """Catat satu kegagalan untuk sebuah konteks; duplikat (siklus berulang) diabaikan."""
    bucket = _INPUT_FAILURES.setdefault(context, [])
    if item not in bucket:
        bucket.append(item)


def reset_input_health() -> None:
    """Kosongkan register (dipakai test dan setiap akhir siklus CLI)."""
    _INPUT_FAILURES.clear()
    _INPUT_PROBED.clear()
    _INPUT_META.clear()


def record_research_health(asset: str, research: Optional[Research]) -> None:
    """Konsumsi satu hasil riset: catat degradasi swarm dan gangguan sumber earnings."""
    _INPUT_PROBED["research_assets"] = _INPUT_PROBED.get("research_assets", 0) + 1
    if research is None:
        record_input_failure("input.riset_hilang", asset)
        return
    for voter in getattr(research, "voter_failures", None) or []:
        record_input_failure("input.voter_swarm", f"{asset}:{voter}")
    status = str(getattr(research, "earnings_status", "ok") or "ok")
    if status.startswith(EARNINGS_OUTAGE_PREFIXES):
        record_input_failure("input.earnings_source", f"{asset}:{status}")


def input_health_snapshot() -> dict[str, Any]:
    """Ringkasan aman-untuk-republik-public: hanya konteks, jumlah, dan maksimal 5 nama."""
    return {
        context: {"failed": len(items), "examples": items[:5]}
        for context, items in sorted(_INPUT_FAILURES.items())
    }


# Status input yang bukan kegagalan tetap, tapi wajib TERLIHAT (mis. riset dibaca dari CSV lokal
# karena webhook tidak membalas JSON): dialirkan ke Step Summary tanpa memicu alert Telegram.
_INPUT_META: dict[str, Any] = {}


def set_input_meta(key: str, value: Any) -> None:
    _INPUT_META[key] = value


def input_meta_snapshot() -> dict[str, Any]:
    return dict(sorted(_INPUT_META.items()))


def inputs_probed_snapshot() -> dict[str, int]:
    """Berapa kali tiap input benar-benar DIPERIKSA siklus ini.

    Penting untuk menafsirkan `input_health`: `{}` berarti 'tidak ada kegagalan TERUKUR', yang
    berbeda maknanya dengan 'tidak ada yang diukur' (mis. siklus SMART IDLE tanpa riset).
    """
    return dict(sorted(_INPUT_PROBED.items()))


def flush_input_health() -> dict[str, Any]:
    """Rangkum register menjadi peringatan teragregasi (plus alert Telegram bila lewat ambang)."""
    if not _INPUT_FAILURES:
        return {}
    total = max(1, _INPUT_PROBED.get("research_assets", 0))
    for context in sorted(_INPUT_FAILURES):
        _log_failure_summary(context, _INPUT_FAILURES[context], total=total)
    return input_health_snapshot()


# =============================================================================
# CLIENT — thin wrapper for Walt MCP over HTTP
# =============================================================================

SLIPPAGE_OBSERVED_PATH = "logs/slippage_observed.jsonl"


def _append_slippage_log(record: dict) -> None:
    """Satu baris JSON per pengukuran - bahan perhitungan berkelanjutan, bukan log hiasan.

    Sengaja memakai os.path (Path tidak diimpor modul ini). Bila penulisannya gagal,
    itu HARUS terdengar: pemanggil menelan exception hanya untuk melindungi order, jadi
    kita catat sebagai warning di sini, bukan debug.
    """
    directory = os.path.dirname(SLIPPAGE_OBSERVED_PATH) or "."
    os.makedirs(directory, exist_ok=True)
    try:
        with open(SLIPPAGE_OBSERVED_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        log.warning(f"_append_slippage_log: pengukuran TIDAK tersimpan ({type(exc).__name__}): {exc}")
        raise


# --- Dasar biaya E5 (Ronde 8, keputusan pemilik: hibrida konservatif) ----------
# Konstanta slippage seragam 1,00% yang bursa nyatakan terbukti konstanta default
# (p10==p90 di 139 kontrak), dan memakainya membuat E5 menuntut 1,77%/hari sehingga
# 100% order produksi diblokir - termasuk pengukuran itu sendiri tidak pernah terjadi.
# Mulai sekarang E5 memakai 2 x p90 |slippage sisi masuk| dari fill NYATA, dengan floor
# konservatif 0,10% (= 2 x 5 bps). Kill switch: E5_SLIPPAGE_BASIS=venue mengembalikan
# perilaku lama tanpa mengubah kode.
E5_SLIPPAGE_BASIS_ENV = "E5_SLIPPAGE_BASIS"
MIN_MEASURED_SLIPPAGE_BASIS = 0.0010

# Knob yang WAJIB tersambung ke blok env workflow entry. Daftarnya lahir dari lubang
# nyata: E5_SLIPPAGE_BASIS sempat hanya terbaca dari os.getenv sehingga `gh variable
# set` tidak ada efeknya di CI - knob yang hidup di kode tapi mati di produksi.
US_STOCK_HOURS_ONLY_ENV = "US_STOCK_HOURS_ONLY"
US_STOCK_MARKET_OPEN_UTC = (13, 30)   # Senin-Jumat 13:30 UTC (EDT open)
US_STOCK_MARKET_CLOSE_UTC = (21, 0)   # sampai 21:00 UTC (EST close)
OFF_HOURS_HOLD_POLICY_ENV = "OFF_HOURS_HOLD_POLICY"
CRYPTO_FORECAST_ENGINE_ENV = "CRYPTO_FORECAST_ENGINE"
CRYPTO_FORECAST_UNIVERSE_ENV = "CRYPTO_FORECAST_UNIVERSE"
STOCK_FORECAST_ENGINE_ENV = "STOCK_FORECAST_ENGINE"
RESEARCH_MODE_ENV = "RESEARCH_MODE"
FORECAST_ADVERSE_FUNDING_CLOSE_THRESHOLD = 0.0030  # 0.30%/hari batas funding merugikan untuk Engine 2 Crypto
STOCK_FORECAST_ADVERSE_FUNDING_CLOSE_THRESHOLD = 0.0025  # 0.25%/hari batas funding merugikan Engine 2 Saham AS

CI_ENV_KNOBS: tuple[str, ...] = (
    "E5_SLIPPAGE_BASIS",
    "US_STOCK_HOURS_ONLY",
    CRYPTO_FORECAST_ENGINE_ENV,
    CRYPTO_FORECAST_UNIVERSE_ENV,
    STOCK_FORECAST_ENGINE_ENV,
    DYNAMIC_SL_MODE_ENV,
    RESEARCH_MODE_ENV,
)
MONITOR_ENV_KNOBS: tuple[str, ...] = (OFF_HOURS_HOLD_POLICY_ENV,)
ENTRY_WORKFLOWS: tuple[str, ...] = ("crypto-execute.yml", "market-execute.yml", "daily-scan.yml")


def is_crypto_forecast_engine_enabled() -> bool:
    """True bila Engine 2 Crypto Mean-Reversion Fade (V5 4h + V4 1h) aktif (default on)."""
    return os.getenv(CRYPTO_FORECAST_ENGINE_ENV, "on").strip().lower() not in (
        "off", "false", "0", "no", "disabled"
    )


def is_stock_forecast_engine_enabled() -> bool:
    """True bila Engine 2 Saham AS LONG-ONLY Dip-Buy (S1 1h + S2 1d) aktif (default on)."""
    return os.getenv(STOCK_FORECAST_ENGINE_ENV, "on").strip().lower() not in (
        "off", "false", "0", "no", "disabled"
    )


def get_crypto_forecast_universe_mode() -> Literal["all", "tier1"]:
    """Mode cakupan aset Crypto Engine 2: `all` (seluruh crypto likuid Walt, default) atau `tier1`."""
    raw = os.getenv(CRYPTO_FORECAST_UNIVERSE_ENV, "all").strip().lower()
    if raw in ("tier1", "bluechip", "allowed_crypto"):
        return "tier1"
    return "all"

US_STOCK_PRECLOSE_START_UTC = (20, 35)       # 25 menit sebelum tutup 21:00 UTC (cukup untuk siklus risk-monitor 5-10m)
COMMODITY_WEEKEND_REOPEN_UTC = (22, 0)       # Minggu 22:00 UTC pembukaan futures komoditas global
OFF_HOURS_EARLY_LOCK_TRIGGER_PCT = 0.0070    # +0.70% profit harga memicu early lock (ruang napas 0.45%)
OFF_HOURS_EARLY_LOCK_OFFSET_PCT = 0.0025     # Kunci SL di Entry +/- 0.25% (menutup fee 0.18% + buffer)
OVERNIGHT_MIN_HOLD_FUNDING_DAILY = 0.0020    # +0.20%/hari carry searah minimal untuk inap overnight Saham AS
WEEKEND_MIN_HOLD_FUNDING_DAILY = 0.0050      # +0.50%/hari carry searah minimal untuk inap akhir pekan bila belum profit-locked
WEEKEND_MAX_LOSS_HOLD_PCT = -0.0050          # -0.50% batas floating loss maks untuk menahan lewat akhir pekan


def is_off_hours_policy_enabled() -> bool:
    """True bila kebijakan hybrid penahanan posisi saat pasar tutup aktif (OFF_HOURS_HOLD_POLICY != off)."""
    return os.getenv(OFF_HOURS_HOLD_POLICY_ENV, "hybrid").strip().lower() not in (
        "off", "false", "0", "no", "disabled"
    )


def classify_asset_market_session(asset: str, now_utc: Optional[datetime] = None) -> tuple[str, str]:
    """Klasifikasikan sesi pasar aktif untuk aset tertentu pada waktu UTC tertentu.

    Cakupan:
    - Saham AS (`ALLOWED_STOCKS`): jam reguler Senin–Jumat 13:30–21:00 UTC;
      mengenal `PRE_CLOSE_OVERNIGHT` (Sen–Kam 20:35–21:00), `CLOSED_OVERNIGHT`,
      `PRE_CLOSE_WEEKEND` (Jum 20:35–21:00), dan `CLOSED_WEEKEND` (Jum 21:00–Sen 13:30).
    - Komoditas (`ALLOWED_COMMODITIES`): aktif 24 jam Senin–Jumat, mengenal
      `PRE_CLOSE_WEEKEND` (Jum 20:35–21:00) dan `CLOSED_WEEKEND` (Jum 21:00–Min 22:00).
    - Crypto / lainnya: selalu `OPEN` (24/7).
    """
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    sym = str(asset or "").strip().upper()
    wd = now.weekday()  # 0=Senin .. 4=Jumat, 5=Sabtu, 6=Minggu
    cur_min = now.hour * 60 + now.minute
    open_min = US_STOCK_MARKET_OPEN_UTC[0] * 60 + US_STOCK_MARKET_OPEN_UTC[1]
    preclose_min = US_STOCK_PRECLOSE_START_UTC[0] * 60 + US_STOCK_PRECLOSE_START_UTC[1]
    close_min = US_STOCK_MARKET_CLOSE_UTC[0] * 60 + US_STOCK_MARKET_CLOSE_UTC[1]
    comm_reopen_min = COMMODITY_WEEKEND_REOPEN_UTC[0] * 60 + COMMODITY_WEEKEND_REOPEN_UTC[1]

    if sym in ALLOWED_STOCKS:
        if wd in (5, 6) or (wd == 4 and cur_min >= close_min) or (wd == 0 and cur_min < open_min):
            return "CLOSED_WEEKEND", "pasar Saham AS tutup akhir pekan (libur 64,5 jam)"
        if wd == 4 and preclose_min <= cur_min < close_min:
            return "PRE_CLOSE_WEEKEND", "pre-close akhir pekan Saham AS (libur 64,5 jam)"
        if wd in (0, 1, 2, 3) and preclose_min <= cur_min < close_min:
            return "PRE_CLOSE_OVERNIGHT", "pre-close overnight Saham AS (tutup 16,5 jam)"
        if open_min <= cur_min < preclose_min:
            return "OPEN", "pasar Saham AS buka reguler"
        return "CLOSED_OVERNIGHT", "pasar Saham AS tutup overnight"

    if sym in ALLOWED_COMMODITIES:
        if wd == 4 and preclose_min <= cur_min < close_min:
            return "PRE_CLOSE_WEEKEND", "pre-close akhir pekan Komoditas (libur akhir pekan)"
        if (wd == 4 and cur_min >= close_min) or wd == 5 or (wd == 6 and cur_min < comm_reopen_min):
            return "CLOSED_WEEKEND", "pasar Komoditas tutup akhir pekan"
        return "OPEN", "pasar Komoditas aktif"

    return "OPEN", "pasar 24/7"


def evaluate_off_hours_position_policy(
    pos: Position,
    current_funding_daily: float,
    now_utc: Optional[datetime] = None,
) -> dict[str, Any]:
    """Kebijakan Hybrid Cost-Aware untuk menahan posisi saat pasar tutup (Opsi B).

    Mempertimbangkan biaya inap (`funding_rate_hourly`) vs biaya tutup-buka ulang
    (`~0.28%-0.45%` notional) dan risiko gap pembukaan:
    1. Sesi `OPEN` / kill-switch `off`: tidak mengubah perilaku 24/7.
    2. Di seluruh sesi non-OPEN (`PRE_CLOSE_*` & `CLOSED_*`):
       - **Zero-tolerance biaya inap:** bila funding berbalik menjadi beban biaya
         (`favorable_funding < 0`), langsung rekomendasikan `CLOSE`.
       - **Early Profit Lock:** bila profit harga `>= +0.40%` (sebelum Tier 1 `+0.80%`),
         kunci SL dini ke `Entry +/- 0.25%` untuk menutup round-trip taker fee `0.18%` + buffer.
    3. `PRE_CLOSE_OVERNIGHT` (Sen–Kam 20:35–21:00 UTC, Saham AS):
       - Tahan bila carry searah `>= +0.20%/hari` ATAU sudah profit-locked; selain itu `CLOSE`.
    4. `PRE_CLOSE_WEEKEND` (Jum 20:35–21:00 UTC, Saham AS & Komoditas):
       - Tahan melewati akhir pekan hanya bila sudah profit-locked ATAU
         (carry searah `>= +0.50%/hari` DAN `profit_pct_price >= -0.50%`); selain itu `CLOSE`.
    """
    if not is_off_hours_policy_enabled():
        return {
            "action": "NONE",
            "session": "DISABLED",
            "early_lock_sl": None,
            "reason": "",
            "favorable_funding_daily": 0.0,
        }

    session, session_note = classify_asset_market_session(pos.base_asset, now_utc=now_utc)
    favorable_funding = (-float(current_funding_daily or 0.0)) if pos.side == "long" else float(current_funding_daily or 0.0)
    if session == "OPEN":
        return {
            "action": "NONE",
            "session": "OPEN",
            "early_lock_sl": None,
            "reason": "",
            "favorable_funding_daily": favorable_funding,
        }

    profit = pos.profit_pct_price
    early_lock_sl: Optional[float] = None
    if profit >= OFF_HOURS_EARLY_LOCK_TRIGGER_PCT and pos.entry_price > 0:
        if pos.side == "long":
            cand = round_price(pos.entry_price * (1.0 + OFF_HOURS_EARLY_LOCK_OFFSET_PCT))
            if cand < pos.current_price:
                early_lock_sl = cand
        else:
            cand = round_price(pos.entry_price * (1.0 - OFF_HOURS_EARLY_LOCK_OFFSET_PCT))
            if cand > pos.current_price:
                early_lock_sl = cand

    existing_locked = False
    if pos.sl is not None and pos.entry_price > 0:
        if pos.side == "long":
            existing_locked = pos.sl >= pos.entry_price * (1.0 + OFF_HOURS_EARLY_LOCK_OFFSET_PCT - 1e-6)
        else:
            existing_locked = pos.sl <= pos.entry_price * (1.0 - OFF_HOURS_EARLY_LOCK_OFFSET_PCT + 1e-6)
    if existing_locked or (early_lock_sl is not None):
        is_profit_locked = True
    else:
        is_profit_locked = False

    is_forecast_stock = (
        getattr(pos, "signal_source", "funding") == "price_forecast"
        and pos.base_asset in ALLOWED_STOCKS
    )
    if is_forecast_stock:
        adverse_funding = max(0.0, -favorable_funding)
        if adverse_funding >= STOCK_FORECAST_ADVERSE_FUNDING_CLOSE_THRESHOLD:
            return {
                "action": "CLOSE",
                "session": session,
                "early_lock_sl": None,
                "favorable_funding_daily": favorable_funding,
                "reason": (
                    f"Engine 2 Saham AS Adverse Funding ({session}): "
                    f"biaya inap {adverse_funding*100:.3f}%/hari >= "
                    f"{STOCK_FORECAST_ADVERSE_FUNDING_CLOSE_THRESHOLD*100:.2f}%/hari"
                ),
            }
        if session in ("PRE_CLOSE_WEEKEND", "CLOSED_WEEKEND"):
            if not is_profit_locked:
                return {
                    "action": "CLOSE",
                    "session": session,
                    "early_lock_sl": None,
                    "favorable_funding_daily": favorable_funding,
                    "reason": "Pre-Close Weekend Engine 2 Saham AS: belum profit-locked, hindari weekend gap 64.5h",
                }
            return {
                "action": "HOLD_PROTECT",
                "session": session,
                "early_lock_sl": early_lock_sl,
                "favorable_funding_daily": favorable_funding,
                "reason": session_note,
            }
        return {
            "action": "HOLD_PROTECT",
            "session": session,
            "early_lock_sl": early_lock_sl,
            "favorable_funding_daily": favorable_funding,
            "reason": f"Engine 2 Saham AS overnight hold ({session_note})",
        }

    if favorable_funding < -1e-9:
        return {
            "action": "CLOSE",
            "session": session,
            "early_lock_sl": None,
            "favorable_funding_daily": favorable_funding,
            "reason": (
                f"Off-Hours Biaya Inap ({session}): funding berlawanan "
                f"({favorable_funding*100:+.3f}%/hari)"
            ),
        }

    if session == "PRE_CLOSE_OVERNIGHT":
        if favorable_funding < OVERNIGHT_MIN_HOLD_FUNDING_DAILY and not is_profit_locked:
            return {
                "action": "CLOSE",
                "session": session,
                "early_lock_sl": None,
                "favorable_funding_daily": favorable_funding,
                "reason": (
                    f"Pre-Close Overnight: carry lemah ({favorable_funding*100:+.3f}%/hari < "
                    f"+{OVERNIGHT_MIN_HOLD_FUNDING_DAILY*100:.2f}%/hari) & belum profit-locked"
                ),
            }
        return {
            "action": "HOLD_PROTECT",
            "session": session,
            "early_lock_sl": early_lock_sl,
            "favorable_funding_daily": favorable_funding,
            "reason": session_note,
        }

    if session == "PRE_CLOSE_WEEKEND":
        strong_weekend_carry = (
            favorable_funding >= WEEKEND_MIN_HOLD_FUNDING_DAILY
            and profit >= WEEKEND_MAX_LOSS_HOLD_PCT
        )
        if not (is_profit_locked or strong_weekend_carry):
            return {
                "action": "CLOSE",
                "session": session,
                "early_lock_sl": None,
                "favorable_funding_daily": favorable_funding,
                "reason": (
                    f"Pre-Close Weekend: carry ({favorable_funding*100:+.3f}%/hari) / "
                    f"PnL ({profit*100:+.2f}%) tidak layak inap akhir pekan"
                ),
            }
        return {
            "action": "HOLD_PROTECT",
            "session": session,
            "early_lock_sl": early_lock_sl,
            "favorable_funding_daily": favorable_funding,
            "reason": session_note,
        }

    return {
        "action": "HOLD_PROTECT",
        "session": session,
        "early_lock_sl": early_lock_sl,
        "favorable_funding_daily": favorable_funding,
        "reason": session_note,
    }


def is_us_stock_market_open_utc(now_utc: Optional[datetime] = None) -> tuple[bool, str]:
    """Cek apakah jendela reguler pasar Saham AS sedang buka (Senin-Jumat 13:30-21:00 UTC).

    Mencakup pembukaan EDT (13:30 UTC) hingga penutupan EST (21:00 UTC).
    Di luar jendela ini (termasuk Sabtu-Minggu UTC), riset & entry baru Saham AS
    (ALLOWED_STOCKS) dilewati; Crypto & Komoditas tetap 24/7, dan risk-monitor
    tetap mengawasi posisi terbuka 24/7.
    Kill switch: US_STOCK_HOURS_ONLY=false menonaktifkan filter tanpa ubah kode.
    """
    if os.getenv(US_STOCK_HOURS_ONLY_ENV, "true").strip().lower() in ("false", "0", "no", "off"):
        return True, "filter jam pasar Saham AS dimatikan (US_STOCK_HOURS_ONLY=false)"
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    if now.weekday() >= 5:
        day_name = "Sabtu" if now.weekday() == 5 else "Minggu"
        return False, f"pasar Saham AS tutup ({day_name} {now.strftime('%H:%M')} UTC; aktif Sen-Jum 13:30-21:00 UTC)"
    cur_min = now.hour * 60 + now.minute
    open_min = US_STOCK_MARKET_OPEN_UTC[0] * 60 + US_STOCK_MARKET_OPEN_UTC[1]
    close_min = US_STOCK_MARKET_CLOSE_UTC[0] * 60 + US_STOCK_MARKET_CLOSE_UTC[1]
    if open_min <= cur_min < close_min:
        return True, f"pasar Saham AS buka ({now.strftime('%H:%M')} UTC)"
    return False, f"di luar jam pasar Saham AS ({now.strftime('%H:%M')} UTC; aktif Sen-Jum 13:30-21:00 UTC)"


def measured_slippage_basis(path: Optional[str] = None) -> tuple[float, str]:
    """2 x p90 slippage sisi masuk terukur, dengan floor konservatif."""
    target = path or SLIPPAGE_OBSERVED_PATH
    try:
        with open(target, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
    except FileNotFoundError:
        return MIN_MEASURED_SLIPPAGE_BASIS, "belum ada sampel -> floor 0,10%"
    except (OSError, json.JSONDecodeError) as exc:
        return MIN_MEASURED_SLIPPAGE_BASIS, f"log tidak terbaca ({type(exc).__name__}) -> floor"
    values = sorted(abs(float(row["slippage_pct"])) for row in rows
                    if isinstance(row, dict) and row.get("stage") == "open"
                    and isinstance(row.get("slippage_pct"), (int, float)))
    if not values:
        return MIN_MEASURED_SLIPPAGE_BASIS, "belum ada sisi masuk -> floor 0,10%"
    index = min(len(values) - 1, max(0, int(round(0.90 * (len(values) - 1)))))
    basis = 2.0 * values[index] / 100.0
    return max(MIN_MEASURED_SLIPPAGE_BASIS, basis), (
        f"2 x p90 sisi masuk = {values[index]:.2f} bps x2 dari {len(values)} sampel")


def effective_price_slippage(venue_slippage: float) -> float:
    """Slippage yang dipakai gerbang E5: terukur (default) atau pernyataan venue."""
    if os.getenv(E5_SLIPPAGE_BASIS_ENV, "measured").strip().lower() == "venue":
        return float(venue_slippage or 0.0)
    basis, _note = measured_slippage_basis()
    return basis


class WaltClient:
    """MCP client untuk Walt Perps dengan auto-refresh OAuth token."""

    def __init__(self, base_url: str = WALT_MCP_URL, token_manager: TokenManager | None = None):
        self.base_url = base_url
        self.tokens = token_manager or TokenManager()
        self.client = httpx.Client(timeout=30.0)
        # Status pengukuran dibuat eksplisit di sini (bukan lahir lewat getattr di tengah
        # jalan) supaya tidak ada state tersembunyi yang sulit dilacak saat insiden.
        # _contract_assets sengaja None, bukan {}: dict kosong berarti "sudah di-cache".
        self._quotes: dict[tuple[str, int], tuple[float, float]] = {}
        self._contract_assets: dict[int, str] | None = None

    def _headers(self, force_refresh: bool = False) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.tokens.get_access_token(force_refresh=force_refresh)}",
        }

    _RETRYABLE_EXCEPTIONS = (
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
    )
    _RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def _post_mcp_with_retry(self, tool_label: str, payload: dict, _auth_retried: bool = False) -> httpx.Response:
        """
        Kirim HTTP POST ke MCP relay dengan retry eksponensial (maks 3 percobaan, jeda 1s lalu 2s)
        untuk error jaringan dan HTTP 429/5xx. Hormati Retry-After (cap 20s) dan pertahankan
        auto-refresh sekali untuk HTTP 401. Tidak pernah mencetak header Authorization / token.
        """
        backoff_delays = (1, 2)
        last_err: str = "unknown error"

        for attempt in range(1, 4):
            try:
                r = self.client.post(self.base_url, json=payload, headers=self._headers())
            except self._RETRYABLE_EXCEPTIONS as exc:
                last_err = f"{type(exc).__name__}: {redact_secrets(str(exc))}"
                if attempt < 3:
                    delay = backoff_delays[attempt - 1]
                    log.warning(
                        f"WaltClient._post_mcp_with_retry({tool_label}) attempt {attempt}/3 gagal ({type(exc).__name__}); retry {delay}s"
                    )
                    time.sleep(delay)
                    continue
                raise RuntimeError(
                    f"Walt MCP network error ({tool_label}) setelah 3 percobaan: {last_err}"
                ) from exc

            if r.status_code == 401 and not _auth_retried:
                self.tokens.get_access_token(force_refresh=True)
                return self._post_mcp_with_retry(tool_label, payload, _auth_retried=True)

            if r.status_code in self._RETRYABLE_STATUS_CODES:
                last_err = f"HTTP {r.status_code}"
                if attempt < 3:
                    delay: float = backoff_delays[attempt - 1]
                    if r.status_code == 429:
                        ra = (r.headers.get("Retry-After") or "").strip()
                        try:
                            if ra:
                                delay = min(20.0, max(0.0, float(ra)))
                        except ValueError:
                            delay = backoff_delays[attempt - 1]
                    log.warning(
                        f"WaltClient._post_mcp_with_retry({tool_label}) HTTP {r.status_code} (attempt {attempt}/3); retry {delay}s"
                    )
                    time.sleep(delay)
                    continue
                raise RuntimeError(
                    f"Walt MCP HTTP error ({tool_label}) setelah 3 percobaan: {last_err}"
                )

            if r.status_code >= 400:
                raise RuntimeError(
                    f"Walt MCP HTTP error ({tool_label}): HTTP {r.status_code}"
                )

            return r

        raise RuntimeError(f"Walt MCP error ({tool_label}): {last_err}")

    def _call(self, tool: str, args: dict | None = None, _retry: bool = False) -> Any:
        """Panggil satu tool MCP via JSON-RPC dengan retry jaringan/5xx/429 dan auto-refresh 401."""
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        }
        r = self._post_mcp_with_retry(tool, payload, _auth_retried=_retry)
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"Walt error: {data['error']}")
        result = data.get("result") or {}
        content = result.get("content", [])
        if isinstance(result, dict) and result.get("isError"):
            err_text = content[0].get("text", str(result)) if content else str(result)
            raise RuntimeError(f"Walt MCP error ({tool}): {err_text}")
        if content and content[0].get("type") == "text":
            raw_text = content[0]["text"]
            try:
                return json.loads(raw_text)
            except Exception as e:
                raise RuntimeError(
                    f"Gagal parse JSON dari MCP ({tool}): {e} | respons: {str(raw_text)[:200]}"
                ) from e
        return data.get("result")

    # --- READ-only ---
    def list_tools(self) -> list[dict]:
        """Panggil MCP tools/list untuk inspeksi tool yang tersedia di relay Walt."""
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "tools/list",
            "params": {},
        }
        r = self._post_mcp_with_retry("tools/list", payload)
        data = r.json()
        return (data.get("result") or {}).get("tools", [])

    def list_contracts(self):
        return self._call("perps_list_contracts")

    def latest_rate(self, market_id: str):
        return self._call("perps_latest_rate", {"marketId": market_id})

    def balance(self):
        return self._call("balance_get")

    def list_positions(self, statuses: list[str] | None = None):
        args = {"statuses": statuses} if statuses else {}
        return self._call("perps_list_positions", args)

    def readiness(self):
        return self._call("perps_readiness")

    # --- WRITE (preview + execute pair) ---
    def preview_order(
        self,
        contract_id: int,
        side: Literal["long", "short"],
        leverage: int,
        amount: float,
        price: float,
        tp: Optional[float] = None,
        sl: Optional[float] = None,
    ):
        args = {
            "contractId": contract_id,
            "side": side,
            "type": "market",
            "leverage": leverage,
            "amount": f"{amount:.4f}",
            "price": f"{price:.6g}",
        }
        auto_close = {}
        if tp is not None:
            auto_close["takeProfit"] = {"priceQuote": f"{tp:.6g}"}
        if sl is not None:
            auto_close["stopLoss"] = {"priceQuote": f"{sl:.6g}"}
        if auto_close:
            args["autoClose"] = auto_close
        self.note_quote("open", contract_id, price)
        return self._call("perps_prepare_order_place", args)

    def preview_auto_close(
        self,
        contract_id: int,
        tp: Optional[float] = None,
        sl: Optional[float] = None,
        delete_tp: bool = False,
        delete_sl: bool = False,
    ):
        args: dict = {"contractId": contract_id}
        if delete_tp:
            args["takeProfit"] = {"action": "delete"}
        elif tp is not None:
            args["takeProfit"] = {"action": "set", "priceQuote": f"{tp:.6g}"}
        if delete_sl:
            args["stopLoss"] = {"action": "delete"}
        elif sl is not None:
            args["stopLoss"] = {"action": "set", "priceQuote": f"{sl:.6g}"}
        return self._call("perps_prepare_auto_close_set", args)

    def preview_close(self, contract_id: int):
        result = self._call("perps_prepare_position_close", {"contractId": contract_id})
        # Kuotasi pra-close: inilah pembanding fill penutupan. Kegagalan mengambilnya tidak
        # boleh menghentikan penutupan - posisi tanpa SL jauh lebih berbahaya daripada satu
        # baris pengukuran yang hilang.
        try:
            asset = self._asset_for_contract(contract_id)
            if asset:
                self.note_quote("close", contract_id, self.latest_rate(f"{asset}-USD-PERP")["rate"])
        except Exception as exc:
            log.debug(f"preview_close: kuotasi pra-close tidak tercatat ({type(exc).__name__})")
        return result

    def _asset_for_contract(self, contract_id: int) -> Optional[str]:
        if not isinstance(getattr(self, "_contract_assets", None), dict):
            try:
                payload = self.list_contracts()
                items = payload.get("contracts") if isinstance(payload, dict) else payload
                self._contract_assets = {
                    int(c.get("contract_id")): str(c.get("base_asset") or "").upper()
                    for c in (items or []) if isinstance(c, dict) and c.get("contract_id") is not None
                }
            except Exception as exc:
                log.debug(f"_asset_for_contract: daftar kontrak gagal ({type(exc).__name__})")
                self._contract_assets = {}
        return self._contract_assets.get(int(contract_id))

    # --- Pengukuran slippage nyata (Ronde 8) -------------------------------------
    # Kuotasi dicatat saat preview, fill dibaca saat execute. Semua jalur produksi
    # (open_position, keempat jalur close monitor) melewati dua titik ini, jadi tidak
    # ada keputusan trading yang disentuh - hanya pengukuran. Kegagalan pengukuran harus
    # selalu diabaikan diam-diam (log.debug), TIDAK PERNAH menjatuhkan order.
    _FILL_PRICE_KEYS = ("avg_fill_price", "fill_price", "execution_price",
                        "avg_entry_price", "avg_close_price", "close_price", "exit_price")

    def note_quote(self, kind: str, contract_id: int, price: Any) -> None:
        try:
            value = float(price)
        except (TypeError, ValueError):
            return
        if value > 0:
            if not isinstance(getattr(self, "_quotes", None), dict):
                self._quotes = {}
            self._quotes[(kind, int(contract_id))] = (value, time.time())

    def _walk_fill(self, node: Any, depth: int = 0) -> tuple[Optional[int], Optional[float]]:
        """Cari (contract_id, harga fill) di dalam respons execute."""
        if depth > 4 or not isinstance(node, dict):
            return None, None
        cid = node.get("contract_id")
        try:
            cid = int(cid) if cid is not None else None
        except (TypeError, ValueError):
            cid = None
        for key in self._FILL_PRICE_KEYS:
            if key in node:
                try:
                    price = float(str(node[key]))
                except (TypeError, ValueError):
                    continue
                if price > 0:
                    return cid, price
        for value in node.values():
            found_cid, found_price = self._walk_fill(value, depth + 1)
            if found_price is not None:
                return found_cid if found_cid is not None else cid, found_price
        return cid, None

    def record_execution_slippage(self, result: Any) -> Optional[dict]:
        """Bandingkan fill bursa dengan kuotasi terakhir untuk kontrak yang sama."""
        try:
            cid, fill = self._walk_fill(result)
            if fill is None or cid is None:
                return None
            pending = getattr(self, "_quotes", {}) or {}
            kind = None
            for candidate in ("open", "close"):
                if (candidate, cid) in pending:
                    kind = candidate
                    break
            if kind is None:
                return None
            quote, quoted_at = pending.pop((kind, cid))
            side_hint = str((result or {}).get("side") or "").lower()
            # positif = lebih buruk dari kuotasi (long terisi lebih mahal / short lebih murah)
            if side_hint == "short":
                signed = (quote - fill) / quote
            elif side_hint == "long":
                signed = (fill - quote) / quote
            else:
                signed = abs(fill - quote) / quote
            record = {
                "stage": kind, "contract_id": cid, "quote": quote, "fill": fill,
                "slippage_pct": signed * 100.0, "quote_age_s": round(time.time() - quoted_at, 3),
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            log.info(f"📐 SLIPPAGE {kind} contract={cid} quote={quote:.6f} fill={fill:.6f} "
                     f"-> {signed * 10000:+.2f} bps (umur kuotasi {record['quote_age_s']}s)")
            try:
                _append_slippage_log(record)
            except Exception as exc:
                log.debug(f"record_execution_slippage: simpan log gagal ({type(exc).__name__})")
            return record
        except Exception as exc:
            log.debug(f"record_execution_slippage diabaikan ({type(exc).__name__})")
            return None

    def record_open_slippage(self, contract_id: int, result: Any,
                             settle_seconds: float = 3.0) -> Optional[dict]:
        """Tambalan untuk celah yang terbukti di produksi #44893261886.

        Respons `perps_execute_operation` saat open hanya memuat order_id dan harga
        kuotasi; `avg_entry_price` baru muncul di `list_positions(statuses=["open"])`
        setelah order settled. Tanpa pembacaan ulang ini, stage=open tidak akan PERNAH
        terukur di jalur produksi walaupun order nyata terjadi - dan dasar biaya E5
        hibrida akan selamanya memakai floor.
        """
        measured = self.record_execution_slippage(result)
        if measured is not None:
            return measured
        pending = getattr(self, "_quotes", None) or {}
        if ("open", int(contract_id)) not in pending:
            return None
        try:
            time.sleep(settle_seconds)
            for pos in _extract_positions(self.list_positions(statuses=["open"])):
                try:
                    if int(pos.get("contract_id") or -1) != int(contract_id):
                        continue
                except (TypeError, ValueError):
                    continue
                fill = _parse_positive_finite_price(pos.get("avg_entry_price"))
                if fill is None:
                    continue
                quote, quoted_at = pending.pop(("open", int(contract_id)))
                side = str(pos.get("side") or "").lower()
                signed = (quote - fill) / quote if side == "short" else (fill - quote) / quote
                record = {
                    "stage": "open", "contract_id": int(contract_id), "side": side,
                    "quote": quote, "fill": fill, "slippage_pct": signed * 100.0,
                    "quote_age_s": round(time.time() - quoted_at, 3), "source": "list_positions",
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                log.info(f"📐 SLIPPAGE open contract={contract_id} quote={quote:.6f} "
                         f"fill={fill:.6f} -> {signed * 10000:+.2f} bps "
                         f"(sumber: list_positions setelah settle)")
                _append_slippage_log(record)
                return record
        except Exception as exc:
            log.warning(f"record_open_slippage gagal (order tetap diproses): "
                        f"{type(exc).__name__}: {str(exc)[:160]}")
        return None

    def execute(self, token: str):
        result = self._call("perps_execute_operation", {"confirmationToken": token})
        try:
            self.record_execution_slippage(result)
        except Exception as exc:
            # Janji desain: pengukuran tidak boleh menjatuhkan order/penutupan, bahkan bila
            # sensor-nya sendiri rusak. Ini ditegakkan di sini, bukan hanya di dalam helper.
            log.warning(f"pengukuran slippage gagal (order tetap diproses): {type(exc).__name__}: {exc}")
        return result


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class Opportunity:
    contract_id: int
    base_asset: str
    funding_daily: float   # sebagai fraction, misal 0.0133 = 1.33%/hari
    volume_24h: float
    max_leverage: int
    side: Literal["long", "short"]
    min_size_base: float = 0.0
    max_size_base: float = 0.0   # plafon ATAS ukuran (aset dasar) dari payload kontrak
    max_size_quote: float = 0.0  # plafon ATAS nilai notional (quote/USD) dari payload kontrak
    price_slippage: float = 0.0
    min_margin_2x: float = 0.0   # minimum margin efektif di lev 2x
    signal_source: str = "funding"  # "funding" | "price_forecast"
    forecast_setup: str = ""        # "V5_4H_REJ_COMFORT" | "V4_1H_EXTREME_FADE"
    grav_bull_pressure: Optional[float] = None  # bullish_pressure % Market Gravity saat sinyal terbentuk
    planned_sl_pct: Optional[float] = None
    planned_tp_pct: Optional[float] = None
    max_hold_hours: Optional[float] = None
    expected_net_edge_pct: Optional[float] = None
    forecast_reason: str = ""

    @property
    def funding_annualized_pct(self) -> float:
        return self.funding_daily * 365 * 100


@dataclass
class Position:
    contract_id: int
    base_asset: str
    side: Literal["long", "short"]
    leverage: int
    margin: float
    entry_price: float
    current_price: float
    tp: Optional[float]
    sl: Optional[float]
    funding_paid: float
    liquidation_price: float
    position_id: str = ""
    opened_at: str = ""
    signal_source: str = "funding"
    planned_sl_pct: Optional[float] = None
    planned_tp_pct: Optional[float] = None
    max_hold_hours: Optional[float] = None

    @property
    def pnl_pct_margin(self) -> float:
        """
        PnL unrealized sebagai % dari margin.
        Jika entry_price <= 0 atau non-finite (atau current_price tidak valid), kembalikan 0.0
        (berarti 'tidak ada sinyal' sehingga guard/trailing tidak menembak dan monitor tidak crash).
        """
        if (
            self.entry_price is None
            or self.current_price is None
            or not math.isfinite(self.entry_price)
            or not math.isfinite(self.current_price)
            or self.entry_price <= 0
        ):
            return 0.0
        if self.side == "long":
            move = (self.current_price - self.entry_price) / self.entry_price
        else:
            move = (self.entry_price - self.current_price) / self.entry_price
        return move * self.leverage

    @property
    def profit_pct_price(self) -> float:
        """
        Profit sebagai % pergerakan harga searah posisi.
        Jika entry_price <= 0 atau non-finite (atau current_price tidak valid), kembalikan 0.0
        (berarti 'tidak ada sinyal' sehingga trailing/TP tidak terpicu palsu).
        """
        if (
            self.entry_price is None
            or self.current_price is None
            or not math.isfinite(self.entry_price)
            or not math.isfinite(self.current_price)
            or self.entry_price <= 0
        ):
            return 0.0
        if self.side == "long":
            return (self.current_price - self.entry_price) / self.entry_price
        return (self.entry_price - self.current_price) / self.entry_price


# =============================================================================
# ASSET SELECTION & RANKING
# =============================================================================

def rank_opportunities(
    contracts: list[dict], allowed_assets: Optional[set[str]] = None
) -> list[Opportunity]:
    """Filter + rank kontrak likuid sesuai policy (saham, komoditas, atau crypto)."""
    target_assets = allowed_assets if allowed_assets is not None else ALLOWED_ASSETS
    opps = []
    for c in contracts:
        asset = c["base_asset"]
        if asset not in target_assets:
            continue
        vol = float(c["volume_24h_usd"])
        if vol < MIN_VOL_24H:
            continue
        fr_hourly = float(c["funding_rate_hourly"])
        fr_daily = fr_hourly * 24
        if abs(fr_daily) < MIN_FUNDING_DAILY:
            continue

        min_base = float(c.get("min_size_base", 0.0))
        max_base = float(c.get("max_size_base", 0.0) or 0.0)
        max_quote = float(c.get("max_size_quote", 0.0) or 0.0)
        slip = float(c.get("price_slippage", 0.0))
        min_margin_2x = min_base * 100 * (1 + slip) / MAX_LEVERAGE  # placeholder awal

        side: Literal["long", "short"] = "short" if fr_daily > 0 else "long"
        opps.append(Opportunity(
            contract_id=c["contract_id"],
            base_asset=asset,
            funding_daily=fr_daily,
            volume_24h=vol,
            max_leverage=c["max_leverage"],
            side=side,
            min_size_base=min_base,
            max_size_base=max_base,
            max_size_quote=max_quote,
            price_slippage=slip,
            min_margin_2x=min_margin_2x,
        ))
    # Rank by absolute funding, descending
    opps.sort(key=lambda o: abs(o.funding_daily), reverse=True)
    if not opps:
        candidates = []
        for c in contracts:
            if c.get("base_asset") in target_assets:
                candidates.append((
                    c["base_asset"],
                    float(c.get("volume_24h_usd", 0)),
                    float(c.get("funding_rate_hourly", 0)) * 24
                ))
        candidates.sort(key=lambda x: abs(x[2]), reverse=True)
        log.info(
            f"0 opps lolos filter (MIN_VOL=${MIN_VOL_24H:,.0f}, MIN_FR={MIN_FUNDING_DAILY*100:.2f}%/hari). "
            f"Top kandidat: {[(a, f'vol=${v:,.0f}', f'fr={fr*100:+.3f}%/hari') for a, v, fr in candidates[:5]]}"
        )
    # Penajaman pemeriksaan (keputusan pemilik 2026-11-24): SELURUH kontrak Walt dievaluasi
    # filter funding/volume — termasuk yang berada DI LUAR whitelist — supaya peluang yang
    # sengaja diabaikan tetap terlihat di log (bukan tersembunyi).
    outside: list[tuple[str, float, float]] = []
    for c in contracts:
        asset = str(c.get("base_asset") or "").strip().upper()
        if not asset or asset in target_assets:
            continue
        try:
            v = float(c.get("volume_24h_usd") or 0.0)
            frd = float(c.get("funding_rate_hourly") or 0.0) * 24.0
        except (TypeError, ValueError):
            continue
        if v >= MIN_VOL_24H and abs(frd) >= MIN_FUNDING_DAILY:
            outside.append((asset, v, frd))
    if outside:
        outside.sort(key=lambda x: abs(x[2]), reverse=True)
        log.info(
            f"🔎 [ENGINE 1] {len(outside)} kontrak lolos filter funding DI LUAR whitelist "
            f"(dari {len(contracts)} kontrak Walt diperiksa); top: "
            + ", ".join(f"{a} {fr*100:+.2f}%/hari vol=${v:,.0f}" for a, v, fr in outside[:5])
        )
    return opps


# =============================================================================
# ENTRY
# =============================================================================

# =============================================================================
# RESEARCH LAYER — fundamentals & news check untuk saham
# =============================================================================

def check_earnings_date(ticker: str) -> Optional[int]:
    """
    Hari menuju earnings berikutnya, atau None bila tidak diketahui.

    Ronde 7: dulu endpoint ini punya salinan parser sendiri dan menjawab 401 (Yahoo menuntut
    cookie+crumb). Sekarang sumbernya SATU dengan jalur swarm (llm_research) — dua salinan
    parser berarti dua salinan bug. Status sumber (`no_data`/`past_only`/`http_error:*`)
    dilaporkan oleh fetch_earnings_days_with_status untuk agregasi kesehatan input.
    """
    days, _status = fetch_earnings_days_with_status(ticker)
    return days


def get_news_sentiment(ticker: str) -> tuple[str, str]:
    """
    Ringkas news sentiment 7 hari terakhir.
    Return (sentiment, narrative_1_2_sentences).
    
    Implementasi minimal: pakai Yahoo Finance news feed (gratis).
    Untuk production, evaluasi dilakukan via Qwen DashScope (llm_research.py)
    atau fallback ke news feed / news API (Yahoo Finance, Finnhub, dll).
    """
    try:
        url = f"https://query1.finance.yahoo.com/v1/finance/search"
        params = {"q": ticker, "newsCount": 5}
        r = httpx.get(url, params=params, timeout=10.0,
                      headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        news = r.json().get("news", [])
        if not news:
            return "unknown", "no recent news"
        
        headlines = [n.get("title", "") for n in news[:5]]
        # Naive sentiment: hitung kata kunci
        bullish_words = {"beat", "surge", "rally", "gain", "upgrade", "strong",
                         "bullish", "record", "outperform", "soar"}
        bearish_words = {"miss", "drop", "fall", "plunge", "downgrade", "weak",
                         "bearish", "cut", "underperform", "slump", "lawsuit", "fraud"}
        
        b_count = sum(1 for h in headlines for w in bullish_words if w in h.lower())
        s_count = sum(1 for h in headlines for w in bearish_words if w in h.lower())
        
        if b_count > s_count + 1:
            sentiment = "bullish"
        elif s_count > b_count + 1:
            sentiment = "bearish"
        else:
            sentiment = "neutral"
        
        narrative = f"Top headlines: {headlines[0]}" + (f"; {headlines[1]}" if len(headlines) > 1 else "")
        return sentiment, narrative[:200]
    except Exception as e:
        log.warning(f"news sentiment failed for {ticker}: {e}")
        return "unknown", f"fetch error: {e}"


def research_stock(
    ticker: str,
    qwen: Optional[QwenClient] = None,
    gemini: Optional[GeminiClient] = None,
    funding_daily: Optional[float] = None,
    recommended_side: Optional[str] = None,
    opportunity_count: int = 0,
    is_idle: Optional[bool] = None,
    grav_pressure: Optional[float] = None,
) -> Research:
    """Full research bundle untuk 1 aset (Qwen + Gemini saat Idle, Qwen + GLM saat banyak peluang > 1)."""
    log.info(f"🔍 Researching {ticker}...")
    if qwen or gemini:
        import llm_research
        _rkw: dict[str, Any] = {
            "qwen": qwen,
            "gemini": gemini,
            "funding_daily": funding_daily,
            "recommended_side": recommended_side,
            "opportunity_count": opportunity_count,
            "is_idle": is_idle,
        }
        if grav_pressure is not None:
            _rkw["grav_pressure"] = grav_pressure
        return llm_research.research_stock(ticker, **_rkw)
    days = check_earnings_date(ticker)
    sentiment, narrative = get_news_sentiment(ticker)
    return Research(
        ticker=ticker,
        days_to_earnings=days,
        news_sentiment=sentiment,  # type: ignore
        narrative=narrative,
        confidence=0.5,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        consensus="FALLBACK_RULE_BASED",
    )


# Ronde 8 (keputusan pemilik): riset AI untuk crypto/komoditas dibatasi supaya biaya LLM tidak
# tumbuh tanpa kendali - hanya kandidat ber-|funding| terbesar, maksimal N ini. Saham TIDAK
# dibatasi (jalur mereka sudah lama begitu dan E3 menuntut riset).
CRYPTO_RESEARCH_TOP_N = 5


def _cfg_int(key: str, default: int) -> int:
    """Baca parameter int dari STRATEGY_CONFIG_JSON dengan fallback aman (tanpa cache, murah)."""
    try:
        from strategy_config import load_strategy_config
        val = int(float(load_strategy_config().get(key, default)))
        return val
    except Exception as exc:
        log.debug(f"_cfg_int({key}) gagal ({type(exc).__name__}); pakai default {default}")
        return int(default)


def crypto_research_set(pairs, limit: Optional[int] = None) -> set[str]:
    """Aset crypto/komoditas yang boleh diteliti AI: |funding| terbesar, dibatasi `limit`.

    `pairs` = iterable (asset, funding_daily). Hanya aset yang sudah melewati floor
    `MIN_FUNDING_DAILY` yang dipertimbangkan, sama seperti gerbang entry.
    """
    rows = [(str(asset).upper(), abs(funding or 0.0)) for asset, funding in pairs
            if abs(funding or 0.0) >= MIN_FUNDING_DAILY
            and str(asset).upper() in (ALLOWED_CRYPTO | ALLOWED_COMMODITIES)]
    rows.sort(key=lambda row: row[1], reverse=True)
    cap = _cfg_int("crypto_research_top_n", CRYPTO_RESEARCH_TOP_N) if limit is None else limit
    return {asset for asset, _f in rows[:max(1, cap)]}


def _grav_amplify_decision(
    opp: Opportunity, verdict: str, reason: str, mult: float
) -> tuple[Literal["GO", "HALF", "SKIP"], str, float]:
    """Market Gravity = PENGUAT berkurva, BUKAN pemutus (keputusan pemilik 2026-11-24).

    Poin selalu netral-100: bull pressure + bear pressure = 100. Aturan:
      - AI (Berita + OAKresearch MCP) tetap pemutus utama verdict.
      - Arah harus searah dulu (bull >= ambang LONG / bear >= ambang SHORT).
      - Pengaruh BARU AKTIF bila skor arah > grav_boost_min_score (default 80):
            boost = grav_boost_max_pct * ((skor - min_score) / (100 - min_score)) ** curve
        dengan curve > 1 -> makin ekstrem skor, makin kuat dorongannya; cap = max_pct.
      - SKIP tidak pernah dinaikkan; struktur berlawanan tidak memblokir/memberi penalti.
    """
    grav = getattr(opp, "grav_bull_pressure", None)
    if grav is None or verdict == "SKIP":
        return verdict, reason, mult
    thr_l = thr_s = 50.0
    min_score, max_pct, curve = 80.0, 0.25, 1.5
    try:
        from strategy_config import load_strategy_config
        _cfg = load_strategy_config()
        thr_l = float(_cfg.get("grav_long_min_bull_pressure", 0.0) or 0.0) or 50.0
        thr_s = float(_cfg.get("grav_short_min_bear_pressure", 0.0) or 0.0) or 50.0
        min_score = float(_cfg.get("grav_boost_min_score", 80.0) or 80.0)
        max_pct = float(_cfg.get("grav_boost_max_pct", 0.25) or 0.25)
        curve = float(_cfg.get("grav_boost_curve", 1.5) or 1.5)
    except Exception as exc:
        log.debug(f"grav amplify: pembacaan ambang gagal ({type(exc).__name__}: {exc}); pakai 50/50, 80, 25%, ^1.5")
    bull = max(0.0, min(100.0, float(grav)))
    bear = 100.0 - bull
    side_score = bull if opp.side == "long" else bear
    direction_thr = thr_l if opp.side == "long" else thr_s
    if side_score < direction_thr:
        return (
            verdict,
            f"{reason} | 🧲 struktur tak searah ({bull:.1f}/100 bull) → tanpa boost; keputusan tetap milik riset AI",
            mult,
        )
    if side_score <= min_score:
        return (
            verdict,
            f"{reason} | 🧲 struktur searah namun skor {side_score:.1f} ≤ ambang pengaruh {min_score:.0f} → tanpa boost",
            mult,
        )
    span = max(1e-6, 100.0 - min_score)
    frac = max(0.0, min(1.0, (side_score - min_score) / span))
    boost_pct = round(max_pct * (frac ** max(1.0, curve)), 6)
    boosted = round(min(1.0 + max_pct, mult * (1.0 + boost_pct)), 4) if mult > 0 else mult
    return (
        verdict,
        f"{reason} | 🧲 struktur kuat searah AI (skor {side_score:.1f}/100, kurva ^{curve:g}) "
        f"→ conviction +{boost_pct * 100:.1f}%",
        boosted,
    )


def decide_go_nogo(
    opp: Opportunity,
    research: Optional[Research],
) -> tuple[Literal["GO", "HALF", "SKIP"], str, float]:
    """Verdict utama dari riset AI, lalu dikuatkan (bukan diputuskan ulang) oleh Market Gravity."""
    verdict, reason, mult = _decide_go_nogo_ai(opp, research)
    return _grav_amplify_decision(opp, verdict, reason, mult)


def _decide_go_nogo_ai(
    opp: Opportunity,
    research: Optional[Research],
) -> tuple[Literal["GO", "HALF", "SKIP"], str, float]:
    """
    Apply decision matrix dengan pertimbangan konsensus 4-Voter Swarm AI (P1-P7).
    Return (verdict, reason, margin_multiplier).
    margin_multiplier = 1.0 (full) atau 0.5 (half) atau 0 (skip).
    """
    is_forecast = getattr(opp, "signal_source", "funding") == "price_forecast"

    # Minimum funding threshold (hanya berlaku untuk jalur Engine 1 funding carry)
    if not is_forecast and abs(opp.funding_daily) < MIN_FUNDING_DAILY:
        return "SKIP", "funding kurang (<0.5%/hari)", 0.0

    # Whitelist check: Engine 1 tetap 31 aset terkurasi; Engine 2 mencakup kontrak crypto likuid
    # dan Saham AS (`ALLOWED_STOCKS`, LONG-ONLY Dip-Buy); Komoditas tetap hanya di Engine 1.
    if not is_forecast:
        if opp.base_asset not in ALLOWED_ASSETS:
            return "SKIP", f"aset {opp.base_asset} tidak masuk whitelist", 0.0
    elif opp.base_asset in ALLOWED_COMMODITIES:
        return "SKIP", f"Engine 2 belum diaktifkan untuk komoditas ({opp.base_asset})", 0.0

    # Engine 2 Saham AS: LONG-ONLY Dip-Buy terverifikasi OOS + wajib riset segar & Earnings H-7
    if is_forecast and opp.base_asset in ALLOWED_STOCKS:
        setup_lbl = opp.forecast_setup or "STOCK_DIP_LONG"
        if opp.side != "long":
            return "SKIP", f"Engine 2 Saham AS hanya mengizinkan LONG dip-buy ({opp.side.upper()} ditolak)", 0.0
        if research is None:
            return "SKIP", f"Engine 2 ({setup_lbl}): riset basi / tidak tersedia (E3 wajib untuk Saham AS)", 0.0
        if research.days_to_earnings is not None and research.days_to_earnings <= 7:
            return "SKIP", f"Engine 2 ({setup_lbl}): earnings H-{research.days_to_earnings} -> SKIP", 0.0
        if research.consensus == "NO_DATA" or research.news_sentiment == "unknown":
            return "SKIP", f"Engine 2 ({setup_lbl}): tidak ada data berita / sentimen unknown -> SKIP", 0.0
        p5 = research.p1_to_p7_scores.get("P5_risk_catalysts", 0.0) if research.p1_to_p7_scores else 0.0
        if p5 >= MAX_P5_RISK:
            return "SKIP", f"Engine 2 ({setup_lbl}): katalis risiko tinggi (P5={p5:.2f} >= {MAX_P5_RISK:.2f}) -> SKIP", 0.0
        if research.news_sentiment == "bearish":
            return (
                "SKIP",
                f"Engine 2 ({setup_lbl}): berita `bearish` (skor {research.final_score:+.2f}) berisiko falling knife -> SKIP",
                0.0,
            )
        if (
            research.overall_consensus_level == "SPLIT"
            or research.vendor_alignment == "split"
            or research.consensus == "CONFLICT"
        ):
            return "HALF", f"Engine 2 ({setup_lbl}): swarm SPLIT -> proteksi HALF size", 0.5
        return "GO", f"Engine 2 ({setup_lbl}) lolos filter AI ({research.news_sentiment} {research.final_score:+.2f})", 1.0

    # Engine 2 Crypto: Mean-Reversion Fade 2 arah terverifikasi OOS (AI sebagai filter P5/SPLIT/lawan arah)
    if is_forecast:
        setup_lbl = opp.forecast_setup or "ENGINE2_FADE"
        if research is None:
            return "GO", f"Engine 2 ({setup_lbl}) sinyal OOS (riset tidak tersedia -> lolos)", 1.0
        p5 = research.p1_to_p7_scores.get("P5_risk_catalysts", 0.0) if research.p1_to_p7_scores else 0.0
        if p5 >= MAX_P5_RISK:
            return "SKIP", f"Engine 2 ({setup_lbl}): katalis risiko tinggi (P5={p5:.2f} >= {MAX_P5_RISK:.2f}) -> SKIP", 0.0
        opposed = (
            (opp.side == "short" and research.news_sentiment == "bullish")
            or (opp.side == "long" and research.news_sentiment == "bearish")
        )
        if opposed:
            return (
                "SKIP",
                f"Engine 2 ({setup_lbl}): berita `{research.news_sentiment}` (skor {research.final_score:+.2f}) "
                f"bertentangan dengan {opp.side.upper()} -> SKIP",
                0.0,
            )
        if (
            research.overall_consensus_level == "SPLIT"
            or research.vendor_alignment == "split"
            or research.consensus == "CONFLICT"
        ):
            return (
                "HALF",
                f"Engine 2 ({setup_lbl}): swarm SPLIT -> proteksi HALF size",
                0.5,
            )
        return "GO", f"Engine 2 ({setup_lbl}) lolos filter AI ({research.news_sentiment} {research.final_score:+.2f})", 1.0

    # Komoditas / Crypto Bluechip: funding & struktur yang memutuskan; AI ikut menilai tetapi
    # hanya boleh MENURUNKAN (HALF/SKIP), tidak pernah membuat entry baru. (Ronde 8, permintaan
    # pemilik.) Riset yang HILANG tidak memblokir crypto - beda dengan saham - karena jalur BACA
    # riset sedang sakit (webhook masih stub): memblokir saat itu berarti berhenti trading diam-diam.
    if opp.base_asset in ALLOWED_COMMODITIES or opp.base_asset in ALLOWED_CRYPTO:
        label = "crypto bluechip" if opp.base_asset in ALLOWED_CRYPTO else "komoditas"
        base_reason = f"{label}, dynamic TP/SL carry capture"
        if research is None:
            return "GO", f"{base_reason} (riset tidak tersedia -> status quo, tidak diblokir)", 1.0
        p5 = research.p1_to_p7_scores.get("P5_risk_catalysts", 0.0) if research.p1_to_p7_scores else 0.0
        if p5 >= MAX_P5_RISK:
            return "SKIP", f"{label}: katalis risiko tinggi (P5={p5:.2f} >= {MAX_P5_RISK:.2f}) -> SKIP", 0.0
        opposed = (
            (opp.side == "short" and research.news_sentiment == "bullish")
            or (opp.side == "long" and research.news_sentiment == "bearish")
        )
        if opposed:
            return ("SKIP",
                    f"{label}: berita `{research.news_sentiment}` (skor {research.final_score:+.2f}) "
                    f"bertentangan dengan {opp.side.upper()} -> SKIP", 0.0)
        if (research.overall_consensus_level == "SPLIT" or research.vendor_alignment == "split"
                or research.consensus == "CONFLICT"):
            return ("HALF",
                    f"{label}: swarm SPLIT (Qwen:{research.qwen_sentiment} vs "
                    f"Gemini:{research.gemini_sentiment}) -> proteksi HALF size", 0.5)
        return "GO", f"{base_reason} (riset tidak bertentangan)", 1.0

    # E3: Saham wajib memiliki riset segar (<= max_research_age_hours); jika tidak ada -> SKIP "riset basi"
    if research is None:
        return "SKIP", "riset basi", 0.0

    # Earnings risk (H-7)
    if research.days_to_earnings is not None and research.days_to_earnings <= 7:
        return "SKIP", f"earnings H-{research.days_to_earnings}", 0.0

    # Ketiadaan Berita / Sentimen Unknown
    if research.consensus == "NO_DATA" or research.news_sentiment == "unknown":
        return "HALF", "tidak ada berita/headlines → proteksi HALF size (panen yield funding konservatif)", 0.5

    # E2: P5 Risk Catalysts Check (katalis event resiko tinggi >= max_p5_risk -> SKIP)
    p5_risk = research.p1_to_p7_scores.get("P5_risk_catalysts", 0.0) if research.p1_to_p7_scores else 0.0
    if p5_risk >= MAX_P5_RISK:
        return "SKIP", f"High Risk Catalyst (P5={p5_risk:.2f} >= {MAX_P5_RISK:.2f}) → SKIP", 0.0

    # Proteksi Divergensi Konsensus (SPLIT atau Vendor Conflict)
    if (
        research.overall_consensus_level == "SPLIT"
        or research.vendor_alignment == "split"
        or research.consensus == "CONFLICT"
    ):
        return "HALF", f"Swarm Consensus SPLIT (Qwen:{research.qwen_sentiment} vs Gemini:{research.gemini_sentiment}) → proteksi HALF size", 0.5

    # Sentiment alignment
    if research.news_sentiment == "unknown":
        return "HALF", "sentiment unknown", 0.5

    align = (
        (opp.side == "short" and research.news_sentiment == "bearish") or
        (opp.side == "long" and research.news_sentiment == "bullish")
    )
    opposite = (
        (opp.side == "short" and research.news_sentiment == "bullish") or
        (opp.side == "long" and research.news_sentiment == "bearish")
    )

    if align:
        # P6 Contrarian crowdedness check
        p6_crowded = research.p1_to_p7_scores.get("P6_contrarian_check", 0.0) if research.p1_to_p7_scores else 0.0
        if p6_crowded >= 0.85:
            return "HALF", f"Align tapi Crowded (P6={p6_crowded:.2f}) → HALF size", 0.5

        if research.overall_consensus_level in ("UNANIMOUS", "STRONG"):
            return "GO", f"Swarm Strong Consensus ({research.final_score:+.2f}) aligns with {opp.side.upper()}", 1.0
        return "HALF", f"Swarm Mixed Consensus ({research.final_score:+.2f}) aligns with {opp.side.upper()} → HALF size", 0.5

    if opposite:
        return "SKIP", f"{research.news_sentiment} news (Score: {research.final_score:+.2f}) bertolak belakang dengan {opp.side.upper()}", 0.0

    # neutral
    return "HALF", f"neutral sentiment (Score: {research.final_score:+.2f}) → half size", 0.5


# =============================================================================
# ENTRY (continued)
# =============================================================================

def compute_baseline_tpsl(
    side: str,
    entry: float,
    asset: Optional[str] = None,
    funding_daily: float = 0.0,
) -> tuple[float, float]:
    """
    Return (tp, sl) dinamis disesuaikan dengan profil volatilitas aset & windfall funding:
    - Asymmetric R:R: TP +2.0% s/d +2.5% vs SL DEFAULT_SL_PCT agar surplus bersih melampaui fee 0.18% (2 * TAKER_FEE_PER_SIDE)
    - Windfall Take Profit: Jika abs(funding_daily) >= MIN_FUNDING_DAILY dan
      DEFAULT_SL_PCT <= 2.5x funding harian < tp_pct, TP dimajukan
    """
    tp_pct, sl_pct = get_asset_baseline_tpsl_pct(asset)

    # MIN_FUNDING_DAILY & DEFAULT_SL_PCT keduanya ikut konfigurasi STRATEGY_CONFIG_JSON
    if abs(funding_daily) >= MIN_FUNDING_DAILY:
        windfall_tp = round(abs(funding_daily) * 2.5, 4)
        if DEFAULT_SL_PCT <= windfall_tp < tp_pct:
            tp_pct = windfall_tp

    if side == "long":
        tp = round_price(entry * (1 + tp_pct))
        sl = round_price(entry * (1 - sl_pct))
    else:
        tp = round_price(entry * (1 - tp_pct))
        sl = round_price(entry * (1 + sl_pct))
    return tp, sl


def _estimate_live_atr_24h_pct(asset: str) -> Optional[float]:
    """Ambil estimasi ATR 24 jam secara non-blocking untuk kalkulasi SL dinamis."""
    try:
        import price_forecast
        return price_forecast.estimate_asset_atr_24h_pct(asset)
    except Exception as exc:
        log.debug(f"Gagal estimasi ATR 24h {asset} ({type(exc).__name__}): {exc}")
        return None


def compute_dynamic_tpsl(
    side: str,
    entry: float,
    *,
    asset: Optional[str] = None,
    funding_daily: float = 0.0,
    atr_24h_pct: Optional[float] = None,
) -> tuple[float, float, float, float]:
    """Hitung `(tp, sl, tp_pct, sl_pct)` adaptif terhadap volatilitas ATR 24 jam."""
    base_tp_pct, floor_sl = get_asset_baseline_tpsl_pct(asset)
    sym = str(asset or "").strip().upper()
    if not is_dynamic_sl_enabled():
        sl_pct = floor_sl
        tp_pct = base_tp_pct
    else:
        cap_sl = DYNAMIC_SL_CALM_CAP_PCT if sym in CALM_SL_ASSETS else DYNAMIC_SL_VOLATILE_CAP_PCT
        if atr_24h_pct is not None and math.isfinite(atr_24h_pct) and atr_24h_pct > 0:
            sl_pct = round(max(floor_sl, min(cap_sl, DYNAMIC_SL_ATR_MULT * float(atr_24h_pct))), 4)
        else:
            if sym in CALM_SL_ASSETS:
                sl_pct = floor_sl
            elif sym in ALLOWED_CRYPTO or sym in ALLOWED_COMMODITIES:
                sl_pct = max(floor_sl, 0.022)
            else:
                sl_pct = max(floor_sl, 0.025)
        tp_pct = max(base_tp_pct, round(DYNAMIC_TP_RR_MULT * sl_pct, 4))

    if abs(funding_daily) >= MIN_FUNDING_DAILY:
        windfall_tp = round(abs(funding_daily) * 2.5, 4)
        if sl_pct <= windfall_tp < tp_pct:
            tp_pct = windfall_tp

    if side == "long":
        tp = round_price(entry * (1.0 + tp_pct))
        sl = round_price(entry * (1.0 - sl_pct))
    else:
        tp = round_price(entry * (1.0 - tp_pct))
        sl = round_price(entry * (1.0 + sl_pct))
    return tp, sl, tp_pct, sl_pct


def resolve_order_margin(
    client: WaltClient,
    opp: Opportunity,
    margin: float = DEFAULT_MARGIN,
    leverage: int = MAX_LEVERAGE,
) -> Optional[tuple[float, float]]:
    """
    Ambil harga terkini & sesuaikan margin dengan batas minimum order exchange (min_size_base).
    Mengembalikan (final_margin, price) atau None jika harga tidak valid / melampaui batas risiko.
    """
    market_id = f"{opp.base_asset}-USD-PERP"
    rate_resp = client.latest_rate(market_id)
    price = float(rate_resp["rate"])
    if price <= 0:
        log.warning(f"{opp.base_asset}: price invalid ({price}), skip")
        return None

    # Validasi & penyesuaian batas minimum order exchange:
    # Syarat exchange: derived_size = (amount * leverage / price) >= min_size_base
    if opp.min_size_base > 0:
        # Buffer 1.5% + 0.02 USD untuk toleransi slippage dan pembulatan perps relay
        min_margin_req = (opp.min_size_base * price / leverage) * (1.0 + max(opp.price_slippage, 0.015))
        min_margin_req = round(min_margin_req + 0.02, 2)
        max_allowed_margin = max(margin * 1.5, DEFAULT_MARGIN * 1.5)
        if min_margin_req > max_allowed_margin:
            log.warning(
                f"{opp.base_asset}: min margin yang disyaratkan exchange (${min_margin_req:.2f}) "
                f"melampaui batas risiko portofolio (${max_allowed_margin:.2f}), skip"
            )
            return None
        if margin < min_margin_req:
            log.info(
                f"{opp.base_asset}: margin ${margin:.2f} disesuaikan ke batas minimum kontrak "
                f"${min_margin_req:.2f} (min_size_base={opp.min_size_base} @ ${price:.2f})"
            )
            margin = min_margin_req

    # Plafon ATAS bursa (terverifikasi #44926211013: max_size_base & max_size_quote ada di
    # 139/139 kontrak). ANTHROPIC ditolak dengan "margin > contract.max_size_quote" padahal
    # clip kita baru 2,50 USD @ lev 2 (notional 5,00) - artinya ada kontrak yang plafon
    # atasnya lebih kecil daripada clip minimum kita.
    #
    # Keputusan pemilik (§3s): SKIP, jangan pernah menurunkan margin di bawah DEFAULT_MARGIN.
    # Alasannya bukan estetika: clip remah membuat fee tetap memakan carry, dan floor 2,50 USD
    # ada justru supaya kita tidak membuka posisi yang secara ekonomi tidak masuk akal.
    #
    # Bursa menulis pesannya sebagai "margin > max_size_quote", tetapi nama field-nya `quote`
    # (= notional). Kami menegakkan bacaan PALING KETAT: notional = margin x leverage.
    notional = margin * leverage
    if opp.max_size_quote > 0 and notional > opp.max_size_quote:
        log.warning(
            f"{opp.base_asset}: SKIP — notional ${notional:.2f} (margin ${margin:.2f} x lev "
            f"{leverage}) melewati plafon kontrak max_size_quote=${opp.max_size_quote:.2f}; "
            f"tidak dibuka di bawah floor ${DEFAULT_MARGIN:.2f}"
        )
        return None
    if opp.max_size_base > 0 and price > 0:
        derived_size = notional / price
        if derived_size > opp.max_size_base:
            log.warning(
                f"{opp.base_asset}: SKIP — ukuran {derived_size:.6f} melewati plafon kontrak "
                f"max_size_base={opp.max_size_base} (notional ${notional:.2f} @ ${price:.4f})"
            )
            return None

    return margin, price


def format_monitor_actions_telegram(actions: list[dict]) -> str:
    """Ringkasan proteksi risk-monitor untuk Telegram (Markdown v1).

    Diekstrak dari cabang CLI `monitor` supaya bisa dites. Bukti produksi #44915963109
    (14:55:52Z): aset "MARKET_UNIVERSE" + detail "GEAR_2_ACTIVE_60M (60m): 0 opened"
    menghasilkan `*..._...*..._` - entitas tumpang-tindih yang parser Markdown v1 tolak
    dengan HTTP 400. Semua teks dinamis karena itu di-escape; hanya penanda bold milik
    kami yang boleh mentah.
    """
    msg = f"*🛡️ Walt Risk Monitor*\n{len(actions)} proteksi risiko diperbarui:\n"
    for a in actions:
        info = a.get("detail") or a.get("layer") or a.get("action")
        asset = md_escape(str(a.get("asset", "?")))
        msg += f"• *{asset}* — {md_escape(str(info or ''))}\n"
    return msg


_MIN_SIZE_MARKER = "margin < contract.min_size_base"


def is_min_size_rejection(exc: BaseException) -> bool:
    """Hanya penolakan min-size bursa yang boleh memicu retry margin.

    `agent.incorrect.order_params` adalah kode GENERIK untuk semua pelanggaran parameter
    order, jadi ia tidak pernah cocok berdiri sendiri. Produksi #44900424439 membuktikannya:
    penolakan "margin > contract.max_size_quote" (margin TERLALU BESAR) ikut dicocokkan oleh
    aturan lama dan di-retry dengan margin yang lebih besar lagi - arah yang salah.
    Aturan baru: wajib menyebut `min_size_base`, dan `max_size` berapa pun membatalkannya.
    """
    text = str(exc).lower()
    if "max_size" in text:
        return False
    return _MIN_SIZE_MARKER in text


def open_position(
    client: WaltClient,
    opp: Opportunity,
    margin: float = DEFAULT_MARGIN,
    leverage: int = MAX_LEVERAGE,
    resolved: Optional[tuple[float, float]] = None,
) -> Optional[dict]:
    """Preview → execute buka posisi dengan Dynamic TP/SL dan minimum order size enforcement.

    Retry terbatas (§3r.1, keputusan pemilik): bila bursa menolak dengan
    `agent.incorrect.order_params` / "margin < contract.min_size_base" - terjadi pada BNB,
    saat margin kita sudah DI ATAS `min_size_base x harga / leverage` tetapi bursa menuntut
    cadangan internal yang tidak diekspos ke kita - kita coba SEKALI dengan margin x1,10.
    Plafon `max_allowed_margin` dan sisa waktu job tetap mengikat; error lain tidak pernah
    di-retry. Margin yang benar-benar terpakai dicatat di `actual_margin` supaya `deployed`,
    buffer kas, dan Executions_Log presisi.
    """
    requested_margin = float(margin)
    if resolved is None:
        resolved = resolve_order_margin(client, opp, margin, leverage)
        if resolved is None:
            return None
    margin, price = resolved

    if getattr(opp, "signal_source", "funding") == "price_forecast" and opp.planned_sl_pct and opp.planned_tp_pct:
        sl_pct = min(0.045, max(DEFAULT_SL_PCT, float(opp.planned_sl_pct)))
        tp_pct = max(0.020, float(opp.planned_tp_pct))
        if opp.side == "long":
            tp = round_price(price * (1.0 + tp_pct))
            sl = round_price(price * (1.0 - sl_pct))
        else:
            tp = round_price(price * (1.0 - tp_pct))
            sl = round_price(price * (1.0 + sl_pct))
    else:
        atr_24h = _estimate_live_atr_24h_pct(opp.base_asset) if is_dynamic_sl_enabled() else None
        tp, sl, tp_pct, sl_pct = compute_dynamic_tpsl(
            opp.side,
            price,
            asset=opp.base_asset,
            funding_daily=opp.funding_daily,
            atr_24h_pct=atr_24h,
        )

    # Coba preview dengan TP/SL
    try:
        preview = client.preview_order(
            opp.contract_id, opp.side, leverage, margin, price, tp=tp, sl=sl
        )
    except Exception as e:
        log.warning(f"{opp.base_asset}: preview with TP/SL gagal ({e}), coba tanpa")
        preview = client.preview_order(
            opp.contract_id, opp.side, leverage, margin, price
        )

    if not preview or not preview.get("ok"):
        risk_detail = preview.get("risk", {}) if isinstance(preview, dict) else preview
        log.error(f"{opp.base_asset}: risk check gagal: {risk_detail}")
        return None

    if not has_enough_job_time(AUTO_CLOSE_RETRY_BUDGET_MIN):
        rem_min = get_job_remaining_minutes()
        log.warning(
            f"{opp.base_asset}: skip open_position sebelum execute(token) — sisa waktu job "
            f"{rem_min:.1f}m < ambang {AUTO_CLOSE_RETRY_BUDGET_MIN:.1f}m"
        )
        return None

    token = preview["confirmationToken"]
    applied_margin = margin
    try:
        result = client.execute(token)
    except Exception as exc:
        if not is_min_size_rejection(exc):
            log.error(f"{opp.base_asset}: execute(token) gagal: {exc}")
            return None
        cap = max(requested_margin * 1.5, DEFAULT_MARGIN * 1.5)
        retry_margin = round(applied_margin * 1.10, 2)
        if retry_margin > cap:
            log.error(f"{opp.base_asset}: retry min-size DITOLAK — margin {retry_margin:.2f} "
                      f"melebihi plafon {cap:.2f}; tidak ada posisi dibuka")
            return None
        if not has_enough_job_time(AUTO_CLOSE_RETRY_BUDGET_MIN):
            log.warning(f"{opp.base_asset}: retry min-size dibatalkan — sisa waktu job tidak "
                        f"cukup untuk memasang TP/SL setelahnya")
            return None
        log.warning(f"{opp.base_asset}: bursa menolak margin {applied_margin:.2f} "
                    f"(min-size internal), retry SEKALI dengan {retry_margin:.2f}")
        try:
            retry_preview = client.preview_order(
                opp.contract_id, opp.side, leverage, retry_margin, price, tp=tp, sl=sl)
            if not retry_preview or not retry_preview.get("ok"):
                detail = retry_preview.get("risk", {}) if isinstance(retry_preview, dict) else retry_preview
                log.error(f"{opp.base_asset}: retry min-size preview gagal: {detail}")
                return None
            result = client.execute(retry_preview["confirmationToken"])
        except Exception as retry_exc:
            log.error(f"{opp.base_asset}: retry min-size gagal: {retry_exc}")
            return None
        preview = retry_preview
        applied_margin = retry_margin

    if not result or (
        isinstance(result, dict)
        and (result.get("ok") is False or "error" in result or result.get("status") == "failed")
    ):
        log.error(f"{opp.base_asset}: execute(token) mengembalikan kegagalan: {result}")
        return None

    order_id = result.get("order", {}).get("order_id") if isinstance(result, dict) else None
    # Pengukuran slippage entry: respons execute tidak selalu memuat harga fill, jadi
    # helper ini membaca ulang posisi terbuka bila perlu (3 detik). Kegagalannya tidak
    # pernah boleh menghentikan jalur entry.
    try:
        client.record_open_slippage(opp.contract_id, result)
    except Exception as exc:
        log.debug(f"open_position: pengukuran slippage dilewati ({type(exc).__name__})")
    log.info(
        f"✅ {opp.side.upper()} {opp.base_asset} opened: margin ${applied_margin} @ ${price:.4f}, "
        f"TP ${tp}, SL ${sl}, order_id={order_id}"
    )

    # Jika preview berhasil tanpa TP/SL, set terpisah dengan retry maksimal 3x.
    # Cek sisa waktu setelah order terisi hanya untuk warning; order sudah terisi sehingga
    # autoClose WAJIB tetap dicoba sampai selesai (tidak boleh berhenti di tengah).
    if "autoClose" not in preview.get("action", {}).get("request", {}):
        if not has_enough_job_time(AUTO_CLOSE_RETRY_BUDGET_MIN):
            rem_min = get_job_remaining_minutes()
            log.warning(
                f"{opp.base_asset}: sisa waktu job tinggal {rem_min:.1f}m (< {AUTO_CLOSE_RETRY_BUDGET_MIN:.1f}m), "
                f"tetapi order sudah terisi — tetap lanjutkan pemasangan TP/SL autoClose hingga tuntas"
            )
        ac_ok = False
        for attempt in range(1, 4):
            time.sleep(3)
            try:
                ac_preview = client.preview_auto_close(opp.contract_id, tp=tp, sl=sl)
                if ac_preview and ac_preview.get("ok"):
                    ac_res = client.execute(ac_preview["confirmationToken"])
                    if ac_res and not (isinstance(ac_res, dict) and ac_res.get("ok") is False):
                        log.info(f"   TP/SL set terpisah (attempt {attempt}): TP=${tp}, SL=${sl}")
                        ac_ok = True
                        break
                log.warning(f"   Attempt {attempt}/3 set TP/SL terpisah belum berhasil: {ac_preview}")
            except Exception as e:
                log.warning(f"   Gagal set TP/SL terpisah (attempt {attempt}/3): {e}")
        if not ac_ok:
            log.error(f"⚠️ POSISI TANPA SL: {opp.base_asset}")
            send_telegram(f"⚠️ POSISI TANPA SL: {opp.base_asset}")

    if isinstance(result, dict):
        result["actual_margin"] = applied_margin
        result["tp"] = tp
        result["sl"] = sl
        result["sl_pct"] = sl_pct
        result["tp_pct"] = tp_pct
        result["price"] = price
    return result


# =============================================================================
# RISK MONITOR — 4 LAPIS
# =============================================================================

# Registrasi handler untuk tier lock string di TRAILING_TIERS: {action_name: (lock_pct, label)}
# Satu-satunya tempat untuk mendaftarkan parameter lock tier string baru.
_TRAILING_HANDLERS: dict[str, tuple[float, str]] = {
    "lock_0035": (0.0035, "L4 True Zero-Risk"),
    "lock_0095": (0.0095, "L4 Lock Cuan"),
}


def _get_position_trailing_trigger_pct(pos: Position) -> float:
    """Ambang profit minimal sebelum trailing/lock Lapis 4 aktif (proporsional terhadap planned_sl_pct)."""
    if (
        TRAILING_TIERS == _DEFAULT_TRAILING_TIERS
        and pos.planned_sl_pct is not None
        and float(pos.planned_sl_pct) > DEFAULT_SL_PCT + 1e-6
    ):
        sl_ref = min(0.045, float(pos.planned_sl_pct))
        return max(0.0100, round(0.55 * sl_ref, 4))
    return float(TRAILING_TIERS[0][0]) if TRAILING_TIERS else 0.0100


def _resolve_trailing_sl(pos: Position, profit: float) -> Optional[tuple[float, str]]:
    """
    Hitung target SL dan deskripsi alasan dari tangga TRAILING_TIERS atau proporsional ATR.
    Mengembalikan (round_price(sl_price), deskripsi_reason) atau None jika profit di bawah tier pertama.
    """
    if (
        TRAILING_TIERS == _DEFAULT_TRAILING_TIERS
        and pos.planned_sl_pct is not None
        and float(pos.planned_sl_pct) > DEFAULT_SL_PCT + 1e-6
    ):
        sl_ref = min(0.045, float(pos.planned_sl_pct))
        t1 = max(0.0100, round(0.55 * sl_ref, 4))
        lock1 = max(0.0035, round(0.20 * sl_ref, 4))
        t2 = max(0.0180, round(0.90 * sl_ref, 4))
        lock2 = max(0.0095, round(0.50 * sl_ref, 4))
        t3 = max(0.0250, round(1.20 * sl_ref, 4))
        trail3 = min(0.0140, max(0.0070, round(0.40 * sl_ref, 4)))
        if profit < t1:
            return None
        if profit >= t3:
            sl_price = (
                pos.current_price * (1.0 - trail3)
                if pos.side == "long"
                else pos.current_price * (1.0 + trail3)
            )
            return round_price(sl_price), f"L4 Trailing {trail3*100:.2f}% (Profit {profit*100:+.2f}%)"
        lock_pct = lock2 if profit >= t2 else lock1
        lbl = "L4 Lock Cuan" if profit >= t2 else "L4 True Zero-Risk"
        sl_price = (
            pos.entry_price * (1.0 + lock_pct)
            if pos.side == "long"
            else pos.entry_price * (1.0 - lock_pct)
        )
        return round_price(sl_price), f"{lbl} +{lock_pct*100:.2f}% (Profit {profit*100:+.2f}%)"

    if not TRAILING_TIERS or profit < TRAILING_TIERS[0][0]:
        return None
    active_tier = None
    for threshold, action in TRAILING_TIERS:
        if profit >= threshold:
            active_tier = (threshold, action)
    if active_tier is None:
        return None
    _threshold, action = active_tier
    handler_spec = _TRAILING_HANDLERS.get(action) if isinstance(action, str) else None
    if handler_spec is not None:
        lock_pct, label = handler_spec
        _is_zero_risk_tier = True if action == "lock_0035" else False
        sl_price = (
            pos.entry_price * (1.0 + lock_pct)
            if pos.side == "long"
            else pos.entry_price * (1.0 - lock_pct)
        )
        tier_desc = f"{label} +{lock_pct*100:.2f}% (Profit {profit*100:+.2f}%)"
    elif isinstance(action, (int, float)) and not isinstance(action, bool):
        trailing_pct = float(action)
        sl_price = (
            pos.current_price * (1.0 - trailing_pct)
            if pos.side == "long"
            else pos.current_price * (1.0 + trailing_pct)
        )
        tier_desc = f"L4 Trailing {trailing_pct*100:.2f}% (Profit {profit*100:+.2f}%)"
    else:
        log.error(
            f"Trailing tier action tidak dikenal: action={action!r} "
            f"(asset={pos.base_asset}, profit={profit*100:+.2f}%, TRAILING_TIERS={TRAILING_TIERS!r})"
        )
        return None
    return round_price(sl_price), tier_desc


def _sl_is_better(side: str, new_sl: float, old_sl: Optional[float]) -> bool:
    """Cek apakah new_sl memberikan proteksi risiko yang lebih baik dibanding old_sl."""
    if old_sl is None:
        return True
    if side == "long":
        # LONG: SL lebih tinggi = proteksi modal / kunci cuan lebih besar
        return new_sl > old_sl
    else:
        # SHORT: SL lebih rendah = proteksi modal / kunci cuan lebih besar
        return new_sl < old_sl


def _sl_is_improvement(pos: Position, new_sl: float, buffer: float = 0.0005) -> bool:
    """SL hanya bergerak ke arah menguntungkan + melewati buffer dibanding pos.sl saat ini."""
    if pos.sl is None:
        return True
    if pos.side == "long":
        # LONG: SL yang lebih tinggi = lebih bagus
        return new_sl > pos.sl * (1 + buffer)
    else:
        # SHORT: SL yang lebih rendah = lebih bagus
        return new_sl < pos.sl * (1 - buffer)


TAKER_FEE_PER_SIDE = 0.0009  # 0.09% per sisi (0.18% round-trip)
# Catatan: memindahkan konstanta fee ke strategy_config.py (config-driven) adalah pekerjaan terpisah di luar ronde 3.


def check_funding_vs_fee_gate(
    funding_daily: float,
    price_slippage: float = 0.0,
    expected_hold_hours: Optional[float] = None,
) -> tuple[bool, float, float, str]:
    """
    E5: Gerbang ekonomi Funding vs Fee sebelum open_position:
      expected_funding = |funding_daily| * (expected_hold_hours / 24)
      biaya = 2 * TAKER_FEE_PER_SIDE (fee round-trip) + price_slippage
    Entry hanya lolos jika expected_funding >= biaya * 1.5.
    Return (passed, expected_funding, cost, summary_note).
    """
    hold_h = EXPECTED_HOLD_HOURS if expected_hold_hours is None else float(expected_hold_hours)
    exp_funding = abs(float(funding_daily or 0.0)) * (hold_h / 24.0)
    cost = (2.0 * TAKER_FEE_PER_SIDE) + max(0.0, float(price_slippage or 0.0))
    required = cost * 1.5
    passed = exp_funding >= required
    note = (
        f"hold={hold_h:.0f}h, exp_funding={exp_funding*100:.3f}%, "
        f"cost={cost*100:.3f}%, req={required*100:.3f}%"
    )
    return passed, round(exp_funding, 6), round(cost, 6), note


def _close_executed_ok(res: Any) -> bool:
    """
    Validasi hasil client.execute() untuk penutupan posisi (P0-2).
    Return False jika res None, atau dict dengan ok==False, memuat key 'error', atau status=='failed'.
    """
    if res is None:
        return False
    if isinstance(res, dict):
        if res.get("ok") is False:
            return False
        if "error" in res and res.get("error") is not None:
            return False
        if str(res.get("status", "")).strip().lower() == "failed":
            return False
    return True


def _parse_positive_finite_price(raw: Any) -> Optional[float]:
    """Konversi nilai harga ke float positif terhingga (round 4 desimal), atau None jika tidak valid."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val <= 0.0:
        return None
    return round(val, 4)


# Vonis final (terverifikasi CI diagnostics.yml #44565856937): endpoint list positions(statuses=["closed"])
# TIDAK punya field harga keluar (hanya 17 kunci). Daftar kunci fill/position/mark di bawah dikhususkan
# untuk respons execute() close yang hingga kini belum terverifikasi dari satu penutupan guard riil.
_CLOSE_FILL_PRICE_KEYS: tuple[str, ...] = ("avg_fill_price", "fill_price", "execution_price", "price")
_CLOSE_POSITION_PRICE_KEYS: tuple[str, ...] = ("avg_close_price", "close_price", "exit_price")
_CLOSE_MARK_PRICE_KEYS: tuple[str, ...] = ("mark_price",)
CLOSE_EXIT_PROXY_MAX_AGE_H = 6.0  # Batas kesegaran (jam) updated_at untuk proksi latest_rate pada exchange-close


def _parse_positive_finite_float(raw: Any) -> Optional[float]:
    """Konversi nilai numerik positif terhingga tanpa pembulatan (mis. value_base/size kontrak)."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val <= 0.0:
        return None
    return val


def _is_closed_timestamp_fresh(
    ts_str: str,
    max_age_h: float = CLOSE_EXIT_PROXY_MAX_AGE_H,
    now_utc: Optional[datetime] = None,
) -> bool:
    """True hanya jika timestamp close/updated_at valid dan berumur <= max_age_h jam."""
    raw = str(ts_str or "").strip()
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = now_utc or datetime.now(timezone.utc)
        age_h = (now - dt).total_seconds() / 3600.0
        return age_h <= max_age_h
    except Exception as exc:
        log.debug(f"Gagal parse timestamp close '{raw}' ({type(exc).__name__}): {exc}")
        return False


def _parse_sane_exit_price(raw: Any, entry_price: float) -> Optional[float]:
    """Validasi harga keluar: harus finite > 0 dan menyimpang < 50% dari entry_price (sanity guard)."""
    val = _parse_positive_finite_price(raw)
    if val is None:
        return None
    if entry_price > 0 and abs(val - entry_price) / entry_price >= 0.5:
        return None
    return val


def _extract_close_exit_price(res: Any, pos: Position) -> tuple[Optional[float], str]:
    """
    Ekstrak harga keluar dengan rantai fallback eksplisit (dari paling akurat ke paling lemah):
      1. exchange_fill     : harga fill riil dari objek order/top-level respons execute close.
      2. response_position : harga posisi penutupan pada respons close (jika dikembalikan API).
      3. mark_price_proxy  : mark price dari respons close (proxy, bukan harga fill riil).
      4. snapshot_estimate : pos.current_price (estimasi snapshot, bukan fill riil).
      5. entry_fallback    : pos.entry_price (hanya jika entry_price benar-benar positif & finite).
      6. unavailable       : (None, "unavailable") bila tidak ada satu pun kandidat yang valid.
    """
    entry = float(pos.entry_price) if _parse_positive_finite_float(pos.entry_price) is not None else 0.0
    if isinstance(res, dict):
        # Kandidat per kelas kunci, mempertahankan presedensi lama (sub-objek diperiksa lebih dulu)
        # DAN menambah cakupan yang dulu bolong: kunci harga penutupan di dalam `order`, serta
        # kunci fill di dalam `position`. Label sumber mengikuti KELAS kunci, bukan lokasi.
        order_obj = res.get("order") if isinstance(res.get("order"), dict) else None
        pos_obj = res.get("position") if isinstance(res.get("position"), dict) else None
        fill_candidates = [d for d in (order_obj, res) if d is not None]
        pos_candidates = [d for d in (pos_obj, order_obj, res) if d is not None]

        # 1. Harga fill riil dari objek order/top-level hasil close
        for cand in fill_candidates:
            for key in _CLOSE_FILL_PRICE_KEYS:
                parsed = _parse_sane_exit_price(cand.get(key), entry)
                if parsed is not None:
                    return parsed, "exchange_fill"

        # 2. Harga posisi penutupan pada respons close (jika ada)
        for cand in pos_candidates:
            for key in _CLOSE_POSITION_PRICE_KEYS:
                parsed = _parse_sane_exit_price(cand.get(key), entry)
                if parsed is not None:
                    return parsed, "response_position"

        # 3. Mark price (dipisahkan dari fill/response_position karena mark price bukan harga fill)
        for cand in pos_candidates:
            for key in _CLOSE_MARK_PRICE_KEYS:
                parsed = _parse_sane_exit_price(cand.get(key), entry)
                if parsed is not None:
                    return parsed, "mark_price_proxy"

    # 4. Fallback snapshot: pos.current_price  # estimasi snapshot, bukan fill riil
    snap_price = _parse_sane_exit_price(pos.current_price, entry)  # estimasi snapshot, bukan fill riil
    if snap_price is not None:
        return snap_price, "snapshot_estimate"

    # 5. Fallback terakhir jujur: entry_price bila valid, selain itu (None, "unavailable")
    entry_fallback = _parse_positive_finite_price(pos.entry_price)
    if entry_fallback is not None:
        return entry_fallback, "entry_fallback"
    return None, "unavailable"


# Ronde 6: sumber yang berarti harga datang dari DATA LOKAL, bukan dari respons execute.
CLOSE_FALLBACK_SOURCES = ("snapshot_estimate", "entry_fallback", "unavailable")
CLOSE_SCHEMA_PROBED: set[str] = set()
CLOSE_FILL_MISSING_COUNT = 0


def _response_shape(res: Any) -> dict[str, str]:
    """Nama kunci + tipe respons execute (TIDAK PERNAH memuat nilai/harga/order_id/token)."""
    if isinstance(res, str):
        # Ronde 8 (#44910823095): execute() untuk close ternyata mengembalikan STR, bukan dict,
        # jadi level 1-3 rantai _extract_close_exit_price tidak mungkin pernah terpicu.
        # Yang dijawab di sini hanya satu hal: apakah string itu MEMUAT angka desimal yang
        # layak disebut harga. Kita tidak pernah log kontennya - hanya struktur & hitungannya.
        decimals = re.findall(r"\d+\.\d+", res)
        return {
            "__type__": "str",
            "__len__": str(len(res)),
            "__decimal_tokens__": str(len(decimals)),
            "__has_plausible_price__": str(
                any(_parse_positive_finite_float(d) is not None for d in decimals)),
        }
    if not isinstance(res, dict):
        return {"__type__": type(res).__name__}
    shape: dict[str, str] = {}
    for k, v in res.items():
        key = str(k)
        if isinstance(v, dict):
            shape[key] = "dict{" + ",".join(sorted(str(x) for x in v)) + "}"
        elif isinstance(v, list):
            shape[key] = f"list[{len(v)}]"
        else:
            shape[key] = type(v).__name__
    return shape


def _probe_close_response_shape(res: Any, source: str, asset: str) -> None:
    """
    Menutup pertanyaan terbuka ronde ini: apakah respons `execute` untuk close memuat harga fill?
    Jawabannya hanya bisa datang dari satu penutupan RIIL, jadi bentuk respons direkam otomatis
    setiap kali ekstraksi harga jatuh ke fallback lokal (artinya respons tidak memberi harga).

    Satu baris per BENTUK unik (bukan per kejadian) supaya tidak spam; hanya nama kunci & tipe.
    """
    global CLOSE_FILL_MISSING_COUNT
    if source not in CLOSE_FALLBACK_SOURCES:
        return
    CLOSE_FILL_MISSING_COUNT += 1
    sig = json.dumps(_response_shape(res), sort_keys=True, ensure_ascii=False)[:600]
    if sig in CLOSE_SCHEMA_PROBED:
        return
    CLOSE_SCHEMA_PROBED.add(sig)
    log.info(
        f"🔎 [CLOSE-SHAPE] respons execute close TIDAK memuat harga fill untuk {asset} "
        f"(sumber={source}); bentuk={sig}"
    )


def _execute_validated_close(
    client: WaltClient,
    pos: Position,
    preview: Any,
    reason: str,
    detail: Optional[str] = None,
) -> Optional[dict]:
    """Eksekusi penutupan posisi dan validasi hasilnya sebelum mencatat close/cooldown."""
    if not (preview and preview.get("ok")):
        return None
    res = client.execute(preview["confirmationToken"])
    if not _close_executed_ok(res):
        res_summary = redact_sensitive_text(str(res))[:200]
        log.error(f"⚠️ GAGAL TUTUP {pos.base_asset} ({reason}): {res_summary}")
        send_telegram(f"⚠️ GAGAL TUTUP: {pos.base_asset} — posisi masih terbuka")
        return {
            "action": "FAILED",
            "detail": f"Gagal tutup ({reason}): {res_summary}",
            "reason": reason,
            "result": res,
        }
    final_exit_price, exit_price_source = _extract_close_exit_price(res, pos)
    _probe_close_response_shape(res, exit_price_source, pos.base_asset)
    price_lbl = f"{final_exit_price:.4f}" if final_exit_price is not None else "None"
    log.debug(
        f"Close exit price {pos.base_asset}: {price_lbl} (sumber={exit_price_source})"
    )
    _record_position_close(pos, reason, exit_price=final_exit_price, exit_source=exit_price_source)
    return {
        "action": "CLOSE",
        "detail": detail or reason,
        "reason": reason,
        "result": res,
    }


def _record_position_close(
    pos: Position,
    reason: str,
    exit_price: Optional[float] = None,
    exit_source: str = "",
) -> None:
    """Catat penutupan posisi ke Closed_Positions (Google Sheet), logs/closed_history.csv, closed_ids, dan set cooldown."""
    pos_key = make_position_dedupe_key({
        "position_id": pos.position_id,
        "contract_id": pos.contract_id,
        "entry_price": pos.entry_price,
        "opened_at": pos.opened_at,
    })
    resolved_exit_price = _parse_positive_finite_price(exit_price)
    resolved_source = exit_source
    if resolved_exit_price is None:
        resolved_exit_price = _parse_positive_finite_price(pos.current_price)  # estimasi snapshot, bukan fill riil
        if resolved_exit_price is not None and not resolved_source:
            resolved_source = "snapshot_estimate"
    if resolved_exit_price is None:
        resolved_exit_price = _parse_positive_finite_price(pos.entry_price)
        if resolved_exit_price is not None and not resolved_source:
            resolved_source = "entry_fallback"
    if resolved_exit_price is None and not resolved_source:
        resolved_source = "unavailable"
    try:
        from sheet_logger import log_close
        entry_ok = _parse_positive_finite_float(pos.entry_price) is not None
        if resolved_exit_price is not None and entry_ok:
            move = (
                (resolved_exit_price - pos.entry_price) / pos.entry_price
                if pos.side == "long"
                else (pos.entry_price - resolved_exit_price) / pos.entry_price
            )
            pnl_quote: Optional[float] = move * pos.leverage * pos.margin
        elif entry_ok and resolved_exit_price is not None:
            pnl_quote = pos.pnl_pct_margin * pos.margin
        else:
            pnl_quote = None
        log_close(
            asset=pos.base_asset,
            side=pos.side,
            contract_id=pos.contract_id,
            entry_price=pos.entry_price,
            exit_price=resolved_exit_price,
            margin=pos.margin,
            pnl_quote=pnl_quote,
            funding_quote=pos.funding_paid,
            reason=reason,
            position_id=pos_key,
            closed_at=datetime.now(timezone.utc).isoformat(),
            opened_at=pos.opened_at,
            exit_source=resolved_source,
        )
        _record_closed_id(pos_key)
    except Exception as e:
        log.warning(f"Gagal mencatat log_close untuk {pos.base_asset}: {e}")
    _set_asset_cooldown(pos.base_asset)


def _parse_position_age_hours(opened_at: str, now_utc: datetime) -> Optional[float]:
    raw = str(opened_at or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = now_utc.replace(tzinfo=timezone.utc) if now_utc.tzinfo is None else now_utc.astimezone(timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 3600.0)
    except (ValueError, TypeError) as exc:
        log.debug(f"Gagal parse position opened_at={opened_at!r}: {exc}")
        return None


def evaluate_and_apply_position_risk(
    client: WaltClient,
    pos: Position,
    current_funding_daily: float,
    now_utc: Optional[datetime] = None,
) -> Optional[dict]:
    """
    Evaluasi 4 Lapis Risiko Terpadu + Kebijakan Off-Hours + Engine 2 Time-Stop & Eksekusi Atomik Tunggal:
    1. Lapis 2 (Funding Flip Sign): Tutup posisi bila funding flip melampaui ambang
       (0.10%/hari untuk Engine 1 carry, atau 0.30%/hari untuk Engine 2 price_forecast).
    1a. Engine 2 Time-Stop: Untuk `signal_source == "price_forecast"`, tutup posisi bila umur
        sudah melewati `max_hold_hours` (24h/72h) dan belum masuk trailing profit (>= +0.80%).
    1b. Off-Hours Hybrid Cost-Aware Policy (saat `now_utc` diberikan / di risk-monitor):
        Evaluasi biaya inap & kelayakan pre-close overnight/weekend untuk Saham AS & Komoditas.
    2. TP Dinamis: Ambil target TP baseline/windfall (atau planned TP Engine 2), perketat bila perlu.
    3. SL Dinamis: Komparasikan kandidat SL dari:
       - Baseline DEFAULT_SL_PCT (atau planned ATR SL Engine 2 <= 4.5%)
       - Funding rendah <0.1%/hari (Lapis 2, khusus Engine 1)
       - Progressive Trailing Stop / Early Zero-Risk Lock (Lapis 4)
       - Off-Hours Early Lock (+0.25% saat profit >= +0.40% di sesi non-OPEN)
       Pilih SL yang paling aman & menguntungkan (strictly better).
    4. Eksekusi Atomik: Kirim SATU request auto_close_set ke exchange untuk menghindari
       error 'auto_close_already_pending'.
    """
    # --- Guard entry_price tidak valid (<= 0 / non-finite) ---
    if _parse_positive_finite_float(pos.entry_price) is None:
        log.debug(f"Skip evaluasi risiko {pos.base_asset}: entry_price tidak valid ({pos.entry_price!r})")
        return {
            "action": "SKIP",
            "reason": "entry_price tidak valid",
            "detail": "entry_price tidak valid",
        }

    is_forecast_pos = getattr(pos, "signal_source", "funding") == "price_forecast"
    if is_forecast_pos and pos.base_asset in ALLOWED_STOCKS:
        flip_threshold = STOCK_FORECAST_ADVERSE_FUNDING_CLOSE_THRESHOLD
    elif is_forecast_pos:
        flip_threshold = FORECAST_ADVERSE_FUNDING_CLOSE_THRESHOLD
    else:
        flip_threshold = FUNDING_FLIP_CLOSE_THRESHOLD

    # --- 1. Lapis 2: Funding Flip Sign (dengan Hysteresis) -> Tutup Posisi Segera ---
    funding_flipped = (
        (pos.side == "long" and current_funding_daily > 0)
        or (pos.side == "short" and current_funding_daily < 0)
    )
    if funding_flipped and abs(current_funding_daily) >= flip_threshold:
        close_lbl = (
            f"Engine 2 Adverse Funding (>={flip_threshold*100:.2f}%/hari)"
            if is_forecast_pos
            else "Funding Flip Sign"
        )
        log.warning(
            f"[L2] {pos.base_asset}: funding flip sign ({current_funding_daily*100:+.3f}%/hari >= "
            f"{flip_threshold*100:.2f}%) -> tutup posisi riil"
        )
        preview = client.preview_close(pos.contract_id)
        return _execute_validated_close(
            client, pos, preview, reason=close_lbl, detail=f"{close_lbl} -> Close Posisi"
        )

    # --- 1a. Engine 2 Time-Stop (khusus posisi directional price_forecast) ---
    if is_forecast_pos and now_utc is not None and pos.max_hold_hours:
        age_h = _parse_position_age_hours(pos.opened_at, now_utc)
        trailing_trigger = _get_position_trailing_trigger_pct(pos)
        if age_h is not None and age_h >= float(pos.max_hold_hours) and pos.profit_pct_price < trailing_trigger:
            ts_reason = f"Engine 2 Time-Stop ({float(pos.max_hold_hours):.0f}h, hold={age_h:.1f}h)"
            log.warning(f"[TIME-STOP] {pos.base_asset}: {ts_reason} -> tutup posisi riil")
            preview = client.preview_close(pos.contract_id)
            return _execute_validated_close(
                client, pos, preview, reason=ts_reason, detail=f"{ts_reason} -> Close Posisi"
            )

    # --- 1b. Hybrid Cost-Aware Off-Hours & Pre-Close Policy ---
    off_hours_policy: Optional[dict[str, Any]] = None
    if now_utc is not None:
        off_hours_policy = evaluate_off_hours_position_policy(
            pos, current_funding_daily, now_utc=now_utc
        )
        if off_hours_policy.get("action") == "CLOSE":
            close_reason = str(off_hours_policy.get("reason") or "Off-Hours Policy Close")
            log.warning(f"[OFF-HOURS] {pos.base_asset}: {close_reason} -> tutup posisi riil")
            preview = client.preview_close(pos.contract_id)
            return _execute_validated_close(
                client,
                pos,
                preview,
                reason=close_reason,
                detail=f"{close_reason} -> Close Posisi",
            )

    # --- 2. Evaluasi Target TP (Lapis 1 Baseline & Windfall, atau Dynamic ATR / Engine 2 Planned TP/SL) ---
    has_dynamic_plan = is_forecast_pos or (pos.planned_sl_pct is not None and float(pos.planned_sl_pct) > 0)
    if has_dynamic_plan:
        plan_sl_pct = min(0.045, max(DEFAULT_SL_PCT, float(pos.planned_sl_pct or 0.035)))
        plan_tp_pct = max(0.020, float(pos.planned_tp_pct or round(DYNAMIC_TP_RR_MULT * plan_sl_pct, 4)))
        if not is_forecast_pos and abs(current_funding_daily) >= MIN_FUNDING_DAILY:
            windfall_tp = round(abs(current_funding_daily) * 2.5, 4)
            if plan_sl_pct <= windfall_tp < plan_tp_pct:
                plan_tp_pct = windfall_tp
        if pos.side == "long":
            target_tp = round_price(pos.entry_price * (1.0 + plan_tp_pct))
            target_sl = round_price(pos.entry_price * (1.0 - plan_sl_pct))
        else:
            target_tp = round_price(pos.entry_price * (1.0 - plan_tp_pct))
            target_sl = round_price(pos.entry_price * (1.0 + plan_sl_pct))
    else:
        target_tp, target_sl = compute_baseline_tpsl(
            pos.side, pos.entry_price, asset=pos.base_asset, funding_daily=current_funding_daily
        )

    new_tp = None
    tp_reason = None
    if not pos.tp:
        new_tp = target_tp
        tp_reason = "L1 Initial"
    else:
        # Perketat TP jika target baru lebih dekat ke entry (lebih cepat tercapai)
        if pos.side == "long" and target_tp < pos.tp * 0.999:
            new_tp = target_tp
            tp_reason = "L1 Tighten"
        elif pos.side == "short" and target_tp > pos.tp * 1.001:
            new_tp = target_tp
            tp_reason = "L1 Tighten"

    # --- 3. Evaluasi Target SL (Komparasi L1, L2, L4) ---
    candidate_sl = None
    sl_reason = None

    # A. Kandidat dari Baseline SL (DEFAULT_SL_PCT atau Dynamic ATR / Engine 2 planned_sl_pct)
    if not pos.sl or _sl_is_improvement(pos, target_sl):
        candidate_sl = target_sl
        if is_forecast_pos:
            sl_reason = "Engine 2 ATR SL"
        elif has_dynamic_plan:
            sl_reason = "L1 Dynamic ATR"
        else:
            sl_reason = "L1 Baseline"

    # B. Kandidat dari Funding Lemah / Flip di bawah threshold <0.1% (Lapis 2 — khusus Engine 1, wajib profit >= +0.50%)
    if (
        not is_forecast_pos
        and abs(current_funding_daily) < FUNDING_FLIP_CLOSE_THRESHOLD
        and pos.profit_pct_price >= L2_BREAKEVEN_MIN_PROFIT_PCT
    ):
        l2_sl = round_price(pos.entry_price)
        l2_valid_side = (
            (pos.side == "long" and l2_sl < pos.current_price)
            or (pos.side == "short" and l2_sl > pos.current_price)
        )
        if l2_valid_side and _sl_is_better(pos.side, l2_sl, candidate_sl) and _sl_is_improvement(pos, l2_sl):
            candidate_sl = l2_sl
            sl_reason = "L2 Breakeven"

    # D. Kandidat dari Trailing Stop (Lapis 4 — True Zero-Risk surplus bersih setelah fee)
    profit = pos.profit_pct_price
    trailing_res = _resolve_trailing_sl(pos, profit)
    if trailing_res is not None:
        l4_sl, tier_desc = trailing_res
        if _sl_is_better(pos.side, l4_sl, candidate_sl) and _sl_is_improvement(pos, l4_sl):
            candidate_sl = l4_sl
            sl_reason = tier_desc

    # E. Kandidat Early Profit Lock Off-Hours (+0.25% saat profit >= +0.40% di sesi non-OPEN)
    if off_hours_policy and off_hours_policy.get("early_lock_sl") is not None:
        early_sl = float(off_hours_policy["early_lock_sl"])
        early_valid_side = (
            (pos.side == "long" and early_sl < pos.current_price)
            or (pos.side == "short" and early_sl > pos.current_price)
        )
        if (
            early_valid_side
            and _sl_is_better(pos.side, early_sl, candidate_sl)
            and _sl_is_improvement(pos, early_sl)
        ):
            candidate_sl = early_sl
            sl_reason = f"Off-Hours Early Lock +0.25% ({off_hours_policy.get('session')})"

    # Pastikan candidate_sl adalah improvement nyata dibanding pos.sl saat ini
    final_sl = candidate_sl if (candidate_sl is not None and _sl_is_improvement(pos, candidate_sl)) else None
    final_tp = new_tp

    # --- Guard SL/TP Sisi Salah + Migrasi Posisi Lama ---
    # LONG : final_sl < current_price dan final_tp > current_price
    # SHORT: final_sl > current_price dan final_tp < current_price
    sl_check = final_sl if final_sl is not None else target_sl
    sl_breached = (
        (pos.side == "long" and sl_check >= pos.current_price)
        or (pos.side == "short" and sl_check <= pos.current_price)
    )
    if sl_breached:
        sl_breach_label = (
            "SL Engine 2 breached"
            if is_forecast_pos
            else f"SL {DEFAULT_SL_PCT*100:.2f}% breached (migrasi)"
        )
        log.warning(
            f"[GUARD] {pos.base_asset}: {sl_breach_label} (current=${pos.current_price:.4f}, sl=${sl_check:.4f}) → tutup posisi"
        )
        preview = client.preview_close(pos.contract_id)
        return _execute_validated_close(
            client, pos, preview, reason=sl_breach_label, detail=sl_breach_label
        )

    tp_check = final_tp if final_tp is not None else (pos.tp if pos.tp is not None else target_tp)
    tp_breached = (
        (pos.side == "long" and tp_check <= pos.current_price)
        or (pos.side == "short" and tp_check >= pos.current_price)
    )
    if tp_breached:
        log.warning(
            f"[GUARD] {pos.base_asset}: TP breached (current=${pos.current_price:.4f}, tp=${tp_check:.4f}) → tutup posisi"
        )
        preview = client.preview_close(pos.contract_id)
        return _execute_validated_close(client, pos, preview, reason="TP breached")

    if final_tp is None and final_sl is None:
        return None

    # --- 4. Eksekusi Atomik Tunggal ---
    details = []
    if final_tp is not None:
        details.append(f"TP ${final_tp:.4f} ({tp_reason})")
    if final_sl is not None:
        details.append(f"SL ${final_sl:.4f} ({sl_reason})")
    detail_str = " | ".join(details)
    log.info(f"🛡️ [ATOMIC RISK] {pos.base_asset}: {detail_str}")

    preview = client.preview_auto_close(pos.contract_id, tp=final_tp, sl=final_sl)
    if preview and preview.get("ok"):
        res = client.execute(preview["confirmationToken"])
        return {
            "action": "UPDATE_AUTO_CLOSE",
            "detail": detail_str,
            "tp": final_tp,
            "sl": final_sl,
            "result": res,
        }
    else:
        log.warning(f"⚠️ [ATOMIC RISK] {pos.base_asset}: preview auto close gagal: {preview}")
        err_code = preview.get("code") if isinstance(preview, dict) else None
        return {"action": "FAILED", "detail": f"Preview failed: {err_code}", "result": preview}


def apply_layer1_baseline(client: WaltClient, pos: Position) -> Optional[dict]:
    """Lapis 1: Set Dynamic TP/SL baseline (wrapper kompatibilitas)."""
    target_tp, target_sl = compute_baseline_tpsl(pos.side, pos.entry_price, asset=pos.base_asset)
    new_tp = target_tp if not pos.tp else None
    new_sl = target_sl if not pos.sl or _sl_is_improvement(pos, target_sl) else None
    if new_tp is None and new_sl is None:
        return None
    preview = client.preview_auto_close(pos.contract_id, tp=new_tp, sl=new_sl)
    if preview and preview.get("ok"):
        return client.execute(preview["confirmationToken"])
    return None


def apply_layer2_funding(
    client: WaltClient, pos: Position, current_funding_daily: float
) -> Optional[dict]:
    """Lapis 2: Dinamis berdasarkan funding sekarang dengan hysteresis (wrapper kompatibilitas)."""
    funding_flipped = (
        (pos.side == "long" and current_funding_daily > 0)
        or (pos.side == "short" and current_funding_daily < 0)
    )
    if funding_flipped and abs(current_funding_daily) >= FUNDING_FLIP_CLOSE_THRESHOLD:
        preview = client.preview_close(pos.contract_id)
        if preview and preview.get("ok"):
            return client.execute(preview["confirmationToken"])
        return None
    if abs(current_funding_daily) < FUNDING_FLIP_CLOSE_THRESHOLD and pos.profit_pct_price >= L2_BREAKEVEN_MIN_PROFIT_PCT:
        new_sl = round_price(pos.entry_price)
        if _sl_is_improvement(pos, new_sl):
            preview = client.preview_auto_close(pos.contract_id, sl=new_sl)
            if preview and preview.get("ok"):
                return client.execute(preview["confirmationToken"])
    return None


def apply_layer3_volatility(client: WaltClient, pos: Position) -> Optional[dict]:
    """deprecated: digantikan SL baseline DEFAULT_SL_PCT"""
    return None


def apply_layer4_trailing(client: WaltClient, pos: Position) -> Optional[dict]:
    """Lapis 4: Trailing stop progresif (wrapper kompatibilitas)."""
    resolved = _resolve_trailing_sl(pos, pos.profit_pct_price)
    if resolved is None:
        return None
    new_sl, _desc = resolved
    if not _sl_is_improvement(pos, new_sl, buffer=0.0005):
        return None
    preview = client.preview_auto_close(pos.contract_id, sl=new_sl)
    if preview and preview.get("ok"):
        return client.execute(preview["confirmationToken"])
    return None


def _infer_position_verdict(
    base_asset: str,
    exec_csv_path: str = "logs/executions_history.csv",
    failures: Optional[list[str]] = None,
) -> str:
    """
    Infer kategori keputusan pembukaan posisi ('HALF' atau 'GO') dari riwayat eksekusi terakhir.
    Digunakan oleh Dynamic Risk Monitor untuk menentukan frekuensi pengawasan (5 menit vs 10 menit).
    """
    if not base_asset or not os.path.exists(exec_csv_path):
        return "GO"
    try:
        with open(exec_csv_path, "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        for r in reversed(rows):
            if len(r) >= 12 and str(r[1]).strip().upper() == base_asset.upper() and str(r[10]).strip().upper() == "EXECUTED":
                reason_low = str(r[11]).lower()
                if any(k in reason_low for k in ("half", "high risk catalyst", "split", "mixed", "neutral", "no research", "tidak ada berita")):
                    return "HALF"
                return "GO"
    except Exception as exc:
        log.debug(
            f"Gagal baca verdict historis {base_asset} dari {exec_csv_path} ({type(exc).__name__}): {exc}"
        )
        if failures is not None:
            failures.append(base_asset)
    return "GO"


def _record_forecast_position_meta(
    contract_id: int,
    opp: Opportunity,
    regime_path: str = "logs/market_regime.json",
    *,
    sl_pct: Optional[float] = None,
    tp_pct: Optional[float] = None,
) -> None:
    """Simpan metadata SL/TP dinamis & max_hold_hours posisi (Engine 1 ATR / Engine 2) ke market_regime.json."""
    try:
        state = _load_execution_regime(regime_path)
        meta_map = state.get("forecast_positions")
        if not isinstance(meta_map, dict):
            meta_map = {}
        sig_src = str(getattr(opp, "signal_source", "funding") or "funding")
        meta_map[str(contract_id)] = {
            "asset": opp.base_asset,
            "setup": opp.forecast_setup if sig_src == "price_forecast" else "dynamic_atr_sl",
            "signal_source": sig_src,
            "planned_sl_pct": float(sl_pct) if sl_pct is not None else opp.planned_sl_pct,
            "planned_tp_pct": float(tp_pct) if tp_pct is not None else opp.planned_tp_pct,
            "max_hold_hours": opp.max_hold_hours if sig_src == "price_forecast" else None,
        }
        state["forecast_positions"] = meta_map
        _save_execution_regime(state, regime_path=regime_path)
    except Exception as exc:
        log.debug(f"Gagal simpan forecast_positions meta cid={contract_id}: {exc}")


def _load_forecast_position_meta(regime_path: str = "logs/market_regime.json") -> dict[str, dict]:
    try:
        state = _load_execution_regime(regime_path)
        raw = state.get("forecast_positions")
        return dict(raw) if isinstance(raw, dict) else {}
    except Exception as exc:
        log.debug(f"Gagal load forecast_positions meta: {exc}")
        return {}


def _clear_forecast_position_meta(
    contract_id: int,
    regime_path: str = "logs/market_regime.json",
) -> None:
    try:
        state = _load_execution_regime(regime_path)
        meta_map = state.get("forecast_positions")
        if isinstance(meta_map, dict) and str(contract_id) in meta_map:
            meta_map.pop(str(contract_id), None)
            state["forecast_positions"] = meta_map
            _save_execution_regime(state, regime_path=regime_path)
    except Exception as exc:
        log.debug(f"Gagal hapus forecast_positions meta cid={contract_id}: {exc}")


def _record_open_position_verdict(
    contract_id: int,
    base_asset: str,
    side: str,
    verdict: str,
    margin: float,
    entry_price: float,
    snapshot_path: str = "logs/open_snapshot.json",
):
    """Catat kategori verdict ('GO' atau 'HALF') posisi yang baru dibuka ke open_snapshot.json."""
    try:
        os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
        snap: dict[str, dict] = {}
        if os.path.exists(snapshot_path):
            with open(snapshot_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    snap = loaded
        cid_str = str(contract_id)
        entry = snap.get(cid_str, {})
        entry.update({
            "contract_id": contract_id,
            "base_asset": base_asset,
            "side": side,
            "margin": margin,
            "entry_price": entry_price,
            "verdict": verdict if verdict in ("GO", "HALF") else "GO",
        })
        snap[cid_str] = entry
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2)
    except Exception as e:
        log.debug(f"Gagal mencatat verdict posisi ke open_snapshot.json: {e}")


def decide_dynamic_monitor_cadence(
    positions: list[Position],
    verdicts_by_cid: Optional[dict[int, str]] = None,
    rng: Optional[Any] = None,
) -> tuple[bool, int, str]:
    """
    Tentukan secara dinamis apakah risk monitor perlu menjalankan sub-cycle ke-2 (~5 menit kemudian)
    di dalam jendela 10 menit saat ini (sehingga frekuensi menjadi hingga 12x/jam dari normal 6x/jam):
    1. 0 posisi aktif -> Smart Idle Mode (False, 0).
    2. Proximity TP/SL -> Jika ada posisi yang sudah dekat TP/Trailing (profit >= +0.15% atau jarak TP <= 0.25%)
       atau dekat SL (loss <= -0.50% atau jarak SL <= 0.35%) -> mode intensif 5 menit (True, 240-280s).
    3. Multi-Position (>= 3 posisi aktif) -> mode intensif 5 menit / 12x per jam (True, 245-290s).
    4. Kategori Keputusan 'HALF' -> perlu perhatian ekstra per 5 menit (True, 245-290s).
    5. Kategori Keputusan 'GO' (< 3 posisi & jauh dari TP/SL) -> default per 10 menit, namun sesekali
       secara random (probabilitas 35%) dicek 5 menit (True, 250-295s), lalu kembali ke 10 menit.
    Return (should_run_subcycle, delay_seconds, reason).
    """
    if not positions:
        return False, 0, "0 posisi aktif → Smart Idle Mode"

    rand = rng if rng is not None else random
    v_map = verdicts_by_cid or {}

    # 1. Cek seberapa dekat dengan parameter TP / Trailing / Pinch Lock atau SL
    for pos in positions:
        profit = pos.profit_pct_price
        tp_pct, sl_pct = get_asset_baseline_tpsl_pct(pos.base_asset)

        if pos.tp and pos.current_price > 0:
            dist_to_tp = abs(pos.tp - pos.current_price) / pos.current_price
        else:
            dist_to_tp = max(0.0, tp_pct - profit)

        if pos.sl and pos.current_price > 0:
            dist_to_sl = abs(pos.current_price - pos.sl) / pos.current_price
        else:
            dist_to_sl = max(0.0, sl_pct + profit)

        if profit >= 0.0070 or dist_to_tp <= 0.0035:
            delay = rand.randint(240, 280)
            return (
                True,
                delay,
                f"Proximity TP/Trailing ({pos.base_asset} profit {profit*100:+.2f}%, dist_tp {dist_to_tp*100:.2f}%)",
            )

        if profit <= -0.0100 or dist_to_sl <= 0.0050:
            delay = rand.randint(240, 280)
            return (
                True,
                delay,
                f"Proximity SL ({pos.base_asset} {profit*100:+.2f}%, dist_sl {dist_to_sl*100:.2f}%)",
            )

    # 2. Cek jika ada >= 3 posisi aktif (skala hingga 12x dalam 60 menit)
    if len(positions) >= 3:
        delay = rand.randint(245, 290)
        return True, delay, f"Multi-Position Active ({len(positions)} posisi >= 3 → mode 12x/jam)"

    # 3. Cek kategori pengambilan keputusan: jika ada posisi HALF -> perhatikan tiap 5 menit
    half_assets = [
        p.base_asset
        for p in positions
        if str(v_map.get(p.contract_id, "GO")).upper() == "HALF"
    ]
    if half_assets:
        delay = rand.randint(245, 290)
        return True, delay, f"Kategori HALF ({', '.join(half_assets)}) → pengawasan intensif 5 menit"

    # 4. Kategori GO (< 3 posisi & kondisi tenang): hemat API, tetap per 10 menit
    return False, 0, "Kategori GO stabil (interval normal 10 menit)"


GEAR_0_US_MARKET_INTERVAL_MIN = 225  # 2x selama jam operasional Saham AS (7,5 jam = 450m / 2)
GEAR_0_US_MAX_RUNS_PER_DAY = 2       # Maksimal 2 run Gear 0 per hari trading Saham AS (UTC)
GEAR_0_INTERVAL_MIN = 720            # 12 jam (2x sehari) saat 0 peluang / idle untuk crypto 24/7


def is_gear3_full_research(gear_label: str) -> bool:
    """True bila Gear 3 aktif: setiap keputusan trading diizinkan melakukan penelitian & analisa AI."""
    return str(gear_label or "").startswith("GEAR_3")


def count_go_opportunities(
    opps: list[Opportunity],
    researches: Optional[dict[str, Research]] = None,
) -> int:
    """
    Hitung jumlah peluang berpotensi / bervonis 'GO':
    - Jika `researches` diberikan: hitung aset yang vonis aktualnya 'GO' melalui `decide_go_nogo`
      (saham yang belum memiliki cache riset namun memenuhi >= MIN_FUNDING_DAILY tetap dihitung
      sebagai kandidat potensial sebelum riset dijalankan).
    - Jika `researches` None: hitung jumlah peluang yang memenuhi |funding_daily| >= MIN_FUNDING_DAILY.
    """
    qualifying = [
        o for o in opps
        if abs(o.funding_daily) >= MIN_FUNDING_DAILY
        or getattr(o, "signal_source", "funding") == "price_forecast"
    ]
    if researches is None:
        return len(qualifying)
    go_total = 0
    for o in qualifying:
        res = researches.get(o.base_asset)
        if res is None and o.base_asset in ALLOWED_STOCKS:
            go_total += 1
            continue
        verdict, _, _ = decide_go_nogo(o, res)
        if verdict == "GO":
            go_total += 1
    return go_total


def evaluate_execution_gear(
    opps: list[Opportunity],
    recent_tp_closed: bool = False,
    rng: Optional[Any] = None,
    go_count: Optional[int] = None,
    category: Literal["market", "crypto"] = "market",
    full_research_mode: Optional[bool] = None,
) -> tuple[str, int, str]:
    """
    Sistem 4 Tingkatan Execution Gear:
    - GEAR 0 (Idle):
      * 0 peluang 'GO' / qualifying.
      * Saham AS (`market`): 2 kali selama jam operasional Saham AS (Senin–Jumat 13:30–21:00 UTC,
        interval 225 menit, maks 2x/sesi).
      * Crypto 24/7 (`crypto`): 2x sehari (interval 720 menit / 12 jam).
    - GEAR 1 (Conservative — 2 jam / 120 menit):
      * Kurang dari 2 peluang 'GO' (tepat 1 peluang 'GO').
    - GEAR 2 (Active — 1 jam / 60 menit):
      * 2 s/d 5 peluang 'GO' (mencakup tepat 5 peluang 'GO').
    - GEAR 3 (Full Research Permission — 1 jam / 60 menit):
      * > 5 peluang 'GO' ATAU override `GEAR_3_FULL_RESEARCH=true`.
      * Memberikan izin setiap keputusan trading untuk melakukan penelitian & analisa AI penuh
        (mengangkat batas `max_otf_calls` & `CRYPTO_RESEARCH_TOP_N`).
    Return (gear_label, interval_minutes, reason).
    """
    qualifying = [
        o for o in opps
        if abs(o.funding_daily) >= MIN_FUNDING_DAILY
        or getattr(o, "signal_source", "funding") == "price_forecast"
    ]
    effective_go = len(qualifying) if go_count is None else max(0, int(go_count))
    gear3_override = (
        full_research_mode
        if full_research_mode is not None
        else os.getenv("GEAR_3_FULL_RESEARCH", "false").strip().lower() in ("1", "true", "yes", "on")
    )

    if gear3_override or effective_go > 5:
        trigger = (
            f"override GEAR_3_FULL_RESEARCH aktif ({effective_go} peluang GO)"
            if gear3_override and effective_go <= 5
            else f"{effective_go} peluang GO (> 5)"
        )
        return (
            "GEAR_3_FULL_RESEARCH_60M",
            60,
            f"Gear 3 Full Research ({trigger}) → izin riset & analisa AI di setiap keputusan trading (60m)",
        )

    if effective_go == 0:
        if category == "market":
            return (
                "GEAR_0_IDLE_2X_US",
                GEAR_0_US_MARKET_INTERVAL_MIN,
                f"Gear 0 Idle (0 peluang GO) → 2x selama jam operasional Saham AS "
                f"(interval {GEAR_0_US_MARKET_INTERVAL_MIN}m, 13:30-21:00 UTC)",
            )
        return (
            "GEAR_0_IDLE_720M",
            GEAR_0_INTERVAL_MIN,
            f"Gear 0 Idle (0 peluang GO) → interval {GEAR_0_INTERVAL_MIN} menit (12 jam / 2x sehari)",
        )

    if effective_go < 2:
        return (
            "GEAR_1_CONSERVATIVE_120M",
            120,
            f"Gear 1 Conservative ({effective_go} peluang GO < 2) → interval 120 menit (2 jam)",
        )

    return (
        "GEAR_2_ACTIVE_60M",
        60,
        f"Gear 2 Active ({effective_go} peluang GO, 2-5 peluang) → interval 60 menit (1 jam)",
    )


def _should_run_gear0_cycle(
    category: Literal["market", "crypto"],
    now_ts: Optional[float] = None,
    regime_path: str = "logs/market_regime.json",
    now_utc: Optional[datetime] = None,
) -> tuple[bool, float]:
    """
    Batasi siklus Gear 0 (Idle):
    - `market`: maksimal 2 kali selama jam operasional Saham AS (Senin–Jumat 13:30–21:00 UTC),
      dengan jeda minimal 225 menit (3,75 jam) antar-run. Di luar jam pasar AS, ditahan.
    - `crypto`: maksimal 2 kali sehari (setiap 720 menit / 12 jam).
    """
    if now_utc is not None:
        dt_utc = now_utc.astimezone(timezone.utc) if now_utc.tzinfo else now_utc.replace(tzinfo=timezone.utc)
        now = dt_utc.timestamp() if now_ts is None else float(now_ts)
    else:
        now = time.time() if now_ts is None else float(now_ts)
        dt_utc = datetime.fromtimestamp(now, tz=timezone.utc)

    state = _load_execution_regime(regime_path)
    key = f"last_{category}_gear0_ts"
    last_ts = float(state.get(key, 0.0) or 0.0)
    elapsed_min = (now - last_ts) / 60.0 if last_ts > 0 else float("inf")

    if category == "market":
        us_open, _ = is_us_stock_market_open_utc(now_utc=dt_utc)
        if not us_open:
            return False, elapsed_min
        today_str = dt_utc.strftime("%Y-%m-%d")
        saved_date = str(state.get("gear0_market_date", "") or "")
        runs_today = int(state.get("gear0_market_runs", 0) or 0) if saved_date == today_str else 0
        if runs_today >= GEAR_0_US_MAX_RUNS_PER_DAY:
            return False, elapsed_min
        if runs_today >= 1 and last_ts > 0 and elapsed_min < (GEAR_0_US_MARKET_INTERVAL_MIN - 2.0):
            return False, elapsed_min
        state["gear0_market_date"] = today_str
        state["gear0_market_runs"] = runs_today + 1
        state[key] = now
        _save_execution_regime(state, regime_path=regime_path)
        return True, elapsed_min

    if last_ts > 0 and elapsed_min < (GEAR_0_INTERVAL_MIN - 2.0):
        return False, elapsed_min
    state[key] = now
    _save_execution_regime(state, regime_path=regime_path)
    return True, elapsed_min


def _set_asset_cooldown(
    asset: str,
    cooldown_sec: int = REENTRY_COOLDOWN_SEC,
    regime_path: str = "logs/market_regime.json",
):
    """Catat waktu cooldown re-entry untuk aset yang baru ditutup (cegah whipsaw & boros fee)."""
    if not asset:
        return
    state = _load_execution_regime(regime_path)
    cooldowns = state.get("cooldowns")
    if not isinstance(cooldowns, dict):
        cooldowns = {}
    cooldowns[asset.upper()] = time.time() + cooldown_sec
    state["cooldowns"] = cooldowns
    _save_execution_regime(state, regime_path=regime_path)


def _is_asset_in_cooldown(
    asset: str,
    regime_path: str = "logs/market_regime.json",
) -> tuple[bool, float]:
    """Cek apakah aset masih dalam masa cooldown. Return (in_cooldown, remaining_minutes)."""
    if not asset:
        return False, 0.0
    state = _load_execution_regime(regime_path)
    cooldowns = state.get("cooldowns") or {}
    until_ts = float(cooldowns.get(asset.upper(), 0.0) or 0.0)
    remaining = until_ts - time.time()
    if remaining > 0:
        return True, round(remaining / 60.0, 1)
    return False, 0.0


def _load_execution_regime(regime_path: str = "logs/market_regime.json") -> dict:
    """Baca state rezim eksekusi (Gear & timestamp eksekusi terakhir) dari disk."""
    if os.path.exists(regime_path):
        try:
            with open(regime_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            log.debug(f"Gagal membaca market_regime.json: {e}")
    return {}


def _save_execution_regime(data: dict, regime_path: str = "logs/market_regime.json"):
    """Simpan state rezim eksekusi ke disk."""
    try:
        os.makedirs(os.path.dirname(regime_path), exist_ok=True)
        with open(regime_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.debug(f"Gagal menyimpan market_regime.json: {e}")


def record_entry_execution(
    asset: str,
    now_ts: Optional[float] = None,
    regime_path: str = "logs/market_regime.json",
) -> None:
    """Catat timestamp entry baru (disimpan rolling 24 jam di logs/market_regime.json)."""
    if not asset:
        return
    now = time.time() if now_ts is None else float(now_ts)
    cutoff = now - 86400.0
    state = _load_execution_regime(regime_path)
    hist = state.get("entry_history_24h")
    if not isinstance(hist, list):
        hist = []
    hist = [
        item for item in hist
        if isinstance(item, dict) and float(item.get("ts", 0.0) or 0.0) >= cutoff
    ]
    hist.append({"asset": asset.upper(), "ts": now})
    state["entry_history_24h"] = hist
    _save_execution_regime(state, regime_path=regime_path)


def check_entry_frequency_limits(
    asset: str,
    now_ts: Optional[float] = None,
    regime_path: str = "logs/market_regime.json",
    max_per_asset: Optional[int] = None,
    max_per_day: Optional[int] = None,
    verdict: Optional[str] = None,
) -> tuple[bool, str]:
    """
    E4: Cek batas frekuensi entry dalam 24 jam terakhir:
    - Vonis GO melewati rem frekuensi 24 jam (keputusan pemilik: peluang GO tidak diblokir
      batas kuota harian/per-aset; circuit breaker -5%, cooldown, slot, & buffer kas tetap aktif).
    - Untuk HALF / tanpa verdict: maksimal max_entries_per_asset_24h (default 2) per aset
      dan max_entries_per_day (default 8) total seluruh aset.
    Return (allowed, reason).
    """
    if str(verdict or "").strip().upper() == "GO":
        return True, "OK (GO bypass batas frekuensi 24 jam)"
    now = time.time() if now_ts is None else float(now_ts)
    cutoff = now - 86400.0
    lim_asset = MAX_ENTRIES_PER_ASSET_24H if max_per_asset is None else int(max_per_asset)
    lim_day = MAX_ENTRIES_PER_DAY if max_per_day is None else int(max_per_day)

    state = _load_execution_regime(regime_path)
    hist = state.get("entry_history_24h")
    if not isinstance(hist, list):
        hist = []
    recent = [
        item for item in hist
        if isinstance(item, dict) and float(item.get("ts", 0.0) or 0.0) >= cutoff
    ]
    if len(recent) >= lim_day:
        return False, f"batas harian tercapai ({len(recent)}/{lim_day} entry dalam 24 jam)"

    asset_up = asset.upper()
    asset_cnt = sum(1 for item in recent if str(item.get("asset", "")).upper() == asset_up)
    if asset_cnt >= lim_asset:
        return False, f"batas entry aset {asset_up} tercapai ({asset_cnt}/{lim_asset} dalam 24 jam)"

    return True, "OK"


def check_daily_circuit_breaker(
    total_equity: float,
    now_utc: Optional[datetime] = None,
    regime_path: str = "logs/market_regime.json",
    limit_pct: Optional[float] = None,
    send_alert: bool = True,
) -> tuple[bool, str]:
    """
    E4: Circuit breaker harian UTC.
    Jika total ekuitas turun >= daily_loss_limit_pct (default 5%) dari ekuitas awal hari UTC,
    hentikan entry baru sampai 00:00 UTC berikutnya dan kirim alert Telegram (1x per hari).
    Risk monitor tetap berjalan normal.
    Return (tripped, reason).
    """
    if total_equity <= 0:
        return False, "OK"
    now = now_utc or datetime.now(timezone.utc)
    today_utc = now.strftime("%Y-%m-%d")
    lim = DAILY_LOSS_LIMIT_PCT if limit_pct is None else float(limit_pct)

    state = _load_execution_regime(regime_path)
    cb = state.get("daily_circuit_breaker")
    if not isinstance(cb, dict) or cb.get("date_utc") != today_utc:
        cb = {
            "date_utc": today_utc,
            "start_equity": round(float(total_equity), 6),
            "tripped": False,
            "alert_sent": False,
        }
        state["daily_circuit_breaker"] = cb
        _save_execution_regime(state, regime_path=regime_path)
        return False, "OK"

    start_eq = float(cb.get("start_equity") or total_equity)
    if start_eq <= 0:
        return False, "OK"

    drop_pct = (start_eq - total_equity) / start_eq
    if cb.get("tripped") or drop_pct >= lim:
        cb["tripped"] = True
        reason = (
            f"circuit breaker harian aktif (ekuitas turun {drop_pct*100:.2f}% >= {lim*100:.1f}% "
            f"dari awal UTC ${start_eq:.2f} → ${total_equity:.2f}; jeda entry sampai 00:00 UTC)"
        )
        if send_alert and not cb.get("alert_sent"):
            cb["alert_sent"] = True
            try:
                send_telegram(
                    f"🚨 *CIRCUIT BREAKER HARIAN AKTIF*\n"
                    f"Ekuitas turun `{drop_pct*100:.2f}%` (batas `{lim*100:.1f}%`)\n"
                    f"Awal hari UTC: `${start_eq:.2f}` → Sekarang: `${total_equity:.2f}`\n"
                    f"Entry baru dihentikan sampai `00:00 UTC` berikutnya. Risk monitor tetap aktif."
                )
            except Exception as exc:
                log.warning(f"Gagal kirim alert circuit breaker: {exc}")
        state["daily_circuit_breaker"] = cb
        _save_execution_regime(state, regime_path=regime_path)
        return True, reason

    return False, "OK"


def _update_regime_state(
    category: Literal["market", "crypto"],
    opps: list[Opportunity],
    mark_executed: bool = False,
    recent_tp_closed: bool = False,
    regime_path: str = "logs/market_regime.json",
    rng: Optional[Any] = None,
    go_count: Optional[int] = None,
) -> tuple[str, int, str]:
    """Update Gear, interval, dan timestamp eksekusi untuk 'market' atau 'crypto'."""
    state = _load_execution_regime(regime_path)
    gear, interval_min, reason = evaluate_execution_gear(
        opps,
        recent_tp_closed=recent_tp_closed,
        rng=rng,
        go_count=go_count,
        category=category,
    )
    state[f"{category}_gear"] = gear
    state[f"{category}_interval_min"] = interval_min
    state[f"{category}_reason"] = reason
    state[f"{category}_opps_count"] = len(opps)
    if mark_executed:
        state[f"last_{category}_exec_ts"] = time.time()
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_execution_regime(state, regime_path=regime_path)
    return gear, interval_min, reason


CLOSED_IDS_PATH = "logs/closed_ids.json"
MAX_CLOSED_IDS = 500

# R6-1: penanda jujur untuk baris hasil backfill riwayat bursa. Endpoint
# list_positions(statuses=["closed"]) tidak menyediakan harga keluar maupun realized PnL,
# sehingga baris ini TIDAK BOLEH diisi PnL hasil turunan/aritmetika.
EXCHANGE_HISTORY_EXIT_SOURCE = "exchange_history_no_exit"
BACKFILL_CLOSE_REASON = "backfill riwayat bursa (harga keluar tidak tersedia di endpoint)"


def make_position_dedupe_key(pos_dict: dict) -> str:
    """
    Kunci dedupe unik posisi: prioritaskan ID posisi dari exchange (position_id/id/uuid).
    Jika tidak ada, fallback ke (contract_id, entry_price, opened_at).
    """
    for field in ("position_id", "id", "uuid", "pos_id"):
        val = pos_dict.get(field)
        if val is not None and str(val).strip():
            return str(val).strip()
    cid = str(pos_dict.get("contract_id", "0"))
    entry = round(float(pos_dict.get("avg_entry_price") or pos_dict.get("entry_price") or 0.0), 6)
    opened = str(
        pos_dict.get("opened_at")
        or pos_dict.get("created_at")
        or pos_dict.get("open_timestamp")
        or ""
    ).strip()
    return f"{cid}:{entry}:{opened}"


def _load_closed_ids(path: str = CLOSED_IDS_PATH) -> list[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return [str(x) for x in data if x]
    except Exception as exc:
        log.warning(f"Gagal memuat daftar closed_ids dari {path} ({type(exc).__name__}): {exc}")
    return []


def _record_closed_id(pos_key: str, path: str = CLOSED_IDS_PATH) -> None:
    if not pos_key:
        return
    ids = _load_closed_ids(path)
    if pos_key in ids:
        return
    ids.append(pos_key)
    ids = ids[-MAX_CLOSED_IDS:]
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ids, f, indent=2)
    except Exception as e:
        log.warning(f"Gagal menyimpan {path}: {e}")


def _fetch_recorded_closed_ids(csv_path: str = "logs/closed_history.csv") -> set[str]:
    """Ambil set Position ID (dan kunci fallback) yang sudah tercatat di Closed_Positions."""
    recorded: set[str] = set()
    try:
        from sheet_logger import fetch_sheet_csv_rows, resolve_closed_positions_columns
        rows = fetch_sheet_csv_rows(
            "Closed_Positions",
            expected_col_name="Exit Price ($)",
            local_csv_fallback=csv_path,
        )
        if not rows:
            # R6-1: kegagalan membaca ledger tidak boleh senyap. sheet_logger sudah mencatat
            # log.warning teragregasi untuk status sheet; ini kasus "0 baris tanpa status jelas".
            log.warning(
                f"_fetch_recorded_closed_ids: 0 baris terbaca dari Closed_Positions/CSV {csv_path} — "
                "dedupe hanya bergantung pada closed_ids.json"
            )
            return recorded
        col_map, data_rows, used_fallback = resolve_closed_positions_columns(rows)
        if used_fallback:
            log.warning(
                "_fetch_recorded_closed_ids: header Closed_Positions rusak/tidak lengkap, memakai fallback indeks baku"
            )
        pid_idx = col_map.get("position id", 10)
        for r in data_rows:
            if pid_idx >= 0 and len(r) > pid_idx and str(r[pid_idx]).strip():
                recorded.add(str(r[pid_idx]).strip())
    except Exception as e:
        log.warning(f"Gagal membaca Position ID dari Closed_Positions ({type(e).__name__}): {e}")
    return recorded


def _infer_close_reason_from_price(
    side: str,
    entry_price: float,
    exit_price: Optional[float],
    tp: Optional[float],
    sl: Optional[float],
    pnl_quote: Optional[float],
) -> str:
    """Tentukan alasan close HANYA dari harga keluar asli vs TP/SL atau realized PnL asli."""
    if exit_price is None and pnl_quote is None:
        return "UNKNOWN (data close tidak tersedia)"
    if exit_price is not None and exit_price > 0:
        if side == "long":
            if tp and exit_price >= tp * 0.998:
                return "Exchange Auto-Close TP"
            if sl and exit_price <= sl * 1.002:
                return "Exchange Auto-Close Trailing/Lock" if sl >= entry_price else "Exchange Auto-Close SL"
        else:
            if tp and exit_price <= tp * 1.002:
                return "Exchange Auto-Close TP"
            if sl and exit_price >= sl * 0.998:
                return "Exchange Auto-Close Trailing/Lock" if sl <= entry_price else "Exchange Auto-Close SL"
    if pnl_quote is not None:
        if pnl_quote > 0:
            return "Exchange Auto-Close TP/Trailing"
        if pnl_quote < 0:
            return "Exchange Auto-Close SL"
    return "UNKNOWN (data close tidak tersedia)"


COLD_REHYDRATE_ENV = "WALT_COLD_REHYDRATE"


def cold_rehydrate_mode() -> str:
    """
    Kebijakan start dingin terhadap jarak ledger-vs-bursa (Ronde 7).

    `report` (default) = ukur & bunyikan, nol penulisan — perilaku warisan Ronde 6.
    `write`            = tutup jarak memakai `reconcile_history(write=True)`: backup lebih dulu,
                         label jujur `exchange_history_no_exit`, PnL tetap kosong (belum terukur).
    Keadaan dingin tidak lagi berarti kehilangan data permanen, TAPI menulis riwayat tetap harus
    keputusan yang dinyatakan operator — bukan efek samping sebuah siklus monitoring.
    """
    mode = (os.environ.get(COLD_REHYDRATE_ENV, "report").strip().lower() or "report")
    return mode if mode in ("report", "write") else "report"


def _dry_run_requested() -> bool:
    return os.getenv("DRY_RUN", "false").strip().lower() == "true"


def _handle_cold_detect_cycle(
    client: WaltClient,
    current_snapshot: dict[str, dict],
    closed_ids_path: str,
    closed_csv_path: str,
) -> tuple[dict[str, dict], list[dict]]:
    """
    R6-2: cache Actions miss. Snapshot lama tidak bisa dipercaya (hilang, atau basi dari
    restore-keys parsial), jadi diff snapshot DILEWATI dan nol baris close ditulis jalur ini.

    Yang selalu dikerjakan: ukur jarak antara riwayat bursa dan ledger memakai SATU skema kunci.
    R7: jarak itu kini bisa DITUTUP otomatis bila operator menyetel WALT_COLD_REHYDRATE=write —
    sebelumnya setiap eviksi cache meninggalkan lubang permanen di ledger (bukti: 52 vs 17 baris).
    Penulisan tetap diblokir saat DRY_RUN, dan tidak pernah terjadi pada mode `report`.
    """
    try:
        api_keys = rehydrate_closed_keys_from_exchange(client)
    except Exception as exc:
        log.warning(
            f"🧊 [START DINGIN] rehidrasi riwayat dari API gagal ({type(exc).__name__}: {str(exc)[:120]}) "
            "— siklus deteksi dilewati, nol baris close ditulis"
        )
        set_input_meta("cold_rehydrate", {"status": "exchange_unreachable"})
        return current_snapshot, []

    known = set(_load_closed_ids(closed_ids_path)) | _fetch_recorded_closed_ids(closed_csv_path)
    gap = sorted(api_keys - known)
    mode = cold_rehydrate_mode()
    log.warning(
        f"🧊 [START DINGIN] cache miss terdeteksi: {len(api_keys)} posisi closed di bursa, "
        f"{len(gap)} belum tercatat di ledger. Diff snapshot dilewati (nol baris UNKNOWN). "
        f"mode={mode}"
        + ("" if mode == "write" else " — backfill manual: `python walt_bot.py reconcile_history --write`.")
    )

    if not gap:
        set_input_meta("cold_rehydrate", {"status": "clean", "exchange_closed": len(api_keys), "gap": 0})
        return current_snapshot, []

    if mode != "write":
        set_input_meta(
            "cold_rehydrate",
            {"status": "report_only", "exchange_closed": len(api_keys), "gap": len(gap)},
        )
        return current_snapshot, []

    if _dry_run_requested():
        set_input_meta(
            "cold_rehydrate",
            {"status": "blocked_dry_run", "exchange_closed": len(api_keys), "gap": len(gap)},
        )
        log.warning("🧊 [START DINGIN] WALT_COLD_REHYDRATE=write tetapi DRY_RUN aktif — nol penulisan.")
        return current_snapshot, []

    try:
        result = reconcile_history(
            client, write=True, csv_path=closed_csv_path, closed_ids_path=closed_ids_path
        )
    except Exception as exc:
        record_input_failure("input.cold_rehydrate", type(exc).__name__)
        log.warning(f"🧊 [START DINGIN] backfill gagal ({type(exc).__name__}: {str(exc)[:120]})")
        return current_snapshot, []

    set_input_meta(
        "cold_rehydrate",
        {
            "status": "written",
            "exchange_closed": len(api_keys),
            "gap": len(gap),
            "written": result.get("written"),
            "backup": result.get("backup"),
        },
    )
    log.warning(
        f"🧊 [START DINGIN] rehidrasi write selesai: {result.get('written')} baris kandidat masuk "
        f"ke {closed_csv_path} (backup={result.get('backup')}); PnL baris tersebut BELUM TERUKUR."
    )
    return current_snapshot, []


def _detect_and_log_exchange_closes(
    client: WaltClient,
    active_positions: list[dict],
    snapshot_path: str = "logs/open_snapshot.json",
    closed_ids_path: str = CLOSED_IDS_PATH,
    closed_csv_path: str = "logs/closed_history.csv",
    state_warm: Optional[bool] = None,
) -> tuple[dict[str, dict], list[dict]]:
    """
    Bandingkan posisi open saat ini dengan snapshot run sebelumnya (logs/open_snapshot.json).
    Dedupe memakai unique Position ID (bukan contract_id) + logs/closed_ids.json + sheet Closed_Positions.
    Jika data close dari exchange tidak tersedia, catat UNKNOWN dengan exit/PnL kosong (bukan TP/PnL=0).
    """
    os.makedirs(os.path.dirname(snapshot_path) or ".", exist_ok=True)
    prev_snapshot: dict[str, dict] = {}
    if os.path.exists(snapshot_path):
        try:
            with open(snapshot_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    prev_snapshot = loaded
                elif isinstance(loaded, list):
                    prev_snapshot = {str(cid): {"contract_id": cid} for cid in loaded}
        except Exception as e:
            log.warning(f"Gagal membaca open_snapshot.json: {e}")

    current_snapshot: dict[str, dict] = {}
    verdict_failures: list[str] = []
    for p in active_positions:
        if str(p.get("status", "open")).lower() not in ("open", "active"):
            continue
        pos_key = make_position_dedupe_key(p)
        cid_str = str(p["contract_id"])
        base_asset = p.get("contract_base_asset") or p.get("base_asset", "")
        prev_info = prev_snapshot.get(pos_key) or prev_snapshot.get(cid_str) or {}
        prev_verdict = prev_info.get("verdict")
        verdict = (
            prev_verdict
            if prev_verdict in ("GO", "HALF")
            else _infer_position_verdict(base_asset, failures=verdict_failures)
        )
        ac = p.get("auto_close") or {}
        tp_val = (ac.get("take_profit") or {}).get("trigger_price_quote") or p.get("take_profit_price") or p.get("tp_price") or p.get("tp")
        sl_val = (ac.get("stop_loss") or {}).get("trigger_price_quote") or p.get("stop_loss_price") or p.get("sl_price") or p.get("sl")
        current_snapshot[pos_key] = {
            "position_id": pos_key,
            "contract_id": p["contract_id"],
            "base_asset": base_asset,
            "side": p.get("side", "long"),
            "leverage": int(p.get("leverage", MAX_LEVERAGE) or MAX_LEVERAGE),
            "margin": float(p.get("margin_quote", p.get("margin", 0.0)) or 0.0),
            "entry_price": float(p.get("avg_entry_price", p.get("entry_price", 0.0)) or 0.0),
            "funding_paid": float(p.get("funding_paid_quote", p.get("funding_paid", 0.0)) or 0.0),
            "tp": float(tp_val) if tp_val else None,
            "sl": float(sl_val) if sl_val else None,
            "opened_at": str(p.get("opened_at") or p.get("created_at") or prev_info.get("opened_at") or ""),
            "verdict": verdict,
        }
    _log_failure_summary("detect_exchange_closes.infer_verdict", verdict_failures, total=len(active_positions))

    # R6-2: cache Actions miss (deteksi dari `cache-hit`, bukan dari tebakan). Snapshot lama tidak
    # boleh dipakai sebagai dasar deteksi — itu yang membuat close fiktif berlabel UNKNOWN.
    warm = is_state_warm() if state_warm is None else bool(state_warm)
    if not warm:
        return _handle_cold_detect_cycle(client, current_snapshot, closed_ids_path, closed_csv_path)

    # Hindari false missing jika prev_snapshot masih format lama (key = contract_id)
    active_cids = {str(v.get("contract_id")) for v in current_snapshot.values()}
    missing_keys = [
        k for k in prev_snapshot.keys()
        if k not in current_snapshot and not (k.isdigit() and k in active_cids)
    ]
    closed_events: list[dict] = []

    if missing_keys:
        known_closed_ids = set(_load_closed_ids(closed_ids_path)) | _fetch_recorded_closed_ids(closed_csv_path)
        closed_list: list[dict] = []
        try:
            closed_resp = client.list_positions(statuses=["closed"])
            closed_list = closed_resp.get("positions", []) if isinstance(closed_resp, dict) else (closed_resp or [])
        except Exception as e:
            log.warning(f"Gagal fetch closed positions untuk snapshot diff: {e}")

        closed_by_key = {make_position_dedupe_key(cp): cp for cp in closed_list if isinstance(cp, dict)}
        closed_by_cid_entry: dict[tuple[str, float], dict] = {}
        for cp in closed_list:
            if isinstance(cp, dict):
                c_cid = str(cp.get("contract_id", ""))
                c_ent = round(float(cp.get("avg_entry_price") or cp.get("entry_price") or 0.0), 6)
                closed_by_cid_entry[(c_cid, c_ent)] = cp

        from sheet_logger import log_close
        rate_failures: list[str] = []
        for prev_key in missing_keys:
            prev_info = prev_snapshot.get(prev_key, {})
            cid_int = int(prev_info.get("contract_id") or (prev_key if prev_key.isdigit() else 0))
            prev_entry = round(float(prev_info.get("entry_price") or 0.0), 6)

            cp = closed_by_key.get(prev_key) or closed_by_cid_entry.get((str(cid_int), prev_entry)) or {}
            dedupe_id = make_position_dedupe_key(cp) if cp else (prev_info.get("position_id") or prev_key)

            if dedupe_id in known_closed_ids or prev_key in known_closed_ids:
                log.info(f"  SKIP duplicate close log untuk position_id={dedupe_id}")
                continue

            asset = (
                cp.get("contract_base_asset")
                or cp.get("base_asset")
                or prev_info.get("base_asset")
                or f"CID_{cid_int}"
            )
            side = str(cp.get("side") or prev_info.get("side") or "long").lower()
            entry_price = float(
                cp.get("avg_entry_price")
                or cp.get("entry_price")
                or prev_info.get("entry_price")
                or 0.0
            )
            margin = float(
                cp.get("margin_quote")
                or cp.get("margin")
                or prev_info.get("margin")
                or 0.0
            )
            leverage = int(cp.get("leverage") or prev_info.get("leverage") or MAX_LEVERAGE)
            funding_quote = float(
                cp.get("funding_paid_quote")
                or cp.get("funding_paid")
                or prev_info.get("funding_paid")
                or 0.0
            )
            closed_at = str(cp.get("updated_at") or cp.get("closed_at") or "")

            raw_exit = (
                cp.get("avg_close_price")
                or cp.get("close_price")
                or cp.get("exit_price")
            )
            exit_price: Optional[float] = _parse_positive_finite_price(raw_exit)
            exit_source = "exchange_closed_field" if exit_price is not None else ""

            # Endpoint list positions(statuses=["closed"]) tidak mengembalikan harga keluar (17 kunci).
            # Bila record closed ditemukan & updated_at masih segar (<= CLOSE_EXIT_PROXY_MAX_AGE_H),
            # ambil mark live saat deteksi via latest_rate sebagai proksi jujur.
            if exit_price is None and cp and asset and not str(asset).startswith("CID_"):
                if not _is_closed_timestamp_fresh(closed_at, max_age_h=CLOSE_EXIT_PROXY_MAX_AGE_H):
                    exit_source = "stale"
                    log.debug(
                        f"Close {asset} stale/tanpa updated_at valid ('{closed_at}') — tidak memakai proksi latest_rate"
                    )
                else:
                    try:
                        rate_resp = client.latest_rate(f"{asset}-USD-PERP")
                        raw_rate = rate_resp.get("rate") if isinstance(rate_resp, dict) else None
                        proxy_exit = _parse_sane_exit_price(raw_rate, entry_price)
                        if proxy_exit is not None:
                            exit_price = proxy_exit
                            exit_source = "mark_at_detection"
                    except Exception as exc:
                        log.debug(
                            f"Gagal fetch latest_rate proksi close {asset} ({type(exc).__name__}): {exc}"
                        )
                        rate_failures.append(str(asset))

            raw_pnl = (
                cp.get("realized_pnl_quote")
                if cp.get("realized_pnl_quote") is not None
                else (cp.get("pnl_quote") if cp.get("pnl_quote") is not None else cp.get("realized_pnl"))
            )
            if raw_pnl is not None:
                pnl_quote: Optional[float] = float(raw_pnl)
            elif entry_price > 0 and exit_price is not None:
                # Perpetual PnL linear terhadap ukuran kontrak (size_base = value_base), bukan turunan margin.
                size_base = _parse_positive_finite_float(cp.get("value_base"))
                if size_base is not None:
                    pnl_quote = (
                        (exit_price - entry_price) * size_base
                        if side == "long"
                        else (entry_price - exit_price) * size_base
                    )
                else:
                    move = (exit_price - entry_price) / entry_price if side == "long" else (entry_price - exit_price) / entry_price
                    pnl_quote = margin * leverage * move
                    log.debug(
                        f"PnL close {asset} memakai fallback margin*leverage*move (value_base tidak valid)"
                    )
            else:
                pnl_quote = None

            reason = str(cp.get("close_reason") or cp.get("reason") or "").strip()
            if not reason:
                reason = _infer_close_reason_from_price(
                    side=side,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    tp=prev_info.get("tp"),
                    sl=prev_info.get("sl"),
                    pnl_quote=pnl_quote,
                )
            if exit_source == "mark_at_detection" and "[exit≈mark@deteksi]" not in reason:
                reason = f"{reason} [exit≈mark@deteksi]"

            try:
                log_close(
                    asset=asset,
                    side=side,
                    contract_id=cid_int,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    margin=margin,
                    pnl_quote=pnl_quote,
                    funding_quote=funding_quote,
                    reason=reason,
                    position_id=dedupe_id,
                    closed_at=closed_at,
                    opened_at=str(cp.get("created_at") or cp.get("opened_at") or prev_info.get("opened_at") or ""),
                    filepath=closed_csv_path,
                    exit_source=exit_source,
                )
                _record_closed_id(dedupe_id, path=closed_ids_path)
                known_closed_ids.add(dedupe_id)
                _set_asset_cooldown(asset)
                pnl_label = f"PnL ${pnl_quote:+.4f}" if pnl_quote is not None else "PnL N/A"
                closed_events.append({
                    "asset": asset,
                    "action": "EXCHANGE_CLOSE_DETECTED",
                    "detail": f"{reason} ({pnl_label})",
                    "position_id": dedupe_id,
                })
            except Exception as err:
                log.warning(f"Gagal log_close untuk {asset}: {err}")
        _log_failure_summary("detect_exchange_closes.latest_rate", rate_failures, total=len(missing_keys))

    return current_snapshot, closed_events


def _save_open_snapshot(snapshot: dict[str, dict], snapshot_path: str = "logs/open_snapshot.json"):
    """Simpan snapshot posisi open terkini ke disk."""
    try:
        os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
    except Exception as e:
        log.warning(f"Gagal menyimpan open_snapshot.json: {e}")


def _run_single_monitor_pass(
    client: WaltClient,
    contracts_by_id: dict[int, dict],
    snapshot_path: str = "logs/open_snapshot.json",
    cycle_label: str = "",
    now_utc: Optional[datetime] = None,
) -> tuple[list[Position], dict[str, dict], list[dict], int]:
    """
    Jalankan 1 putaran pemeriksaan risk monitor terhadap seluruh posisi terbuka.
    Return (remaining_open_positions, current_snapshot, actions, raw_active_count).
    """
    positions_data = client.list_positions(statuses=["open"])
    active_positions = positions_data.get("positions", [])

    current_snapshot, closed_actions = _detect_and_log_exchange_closes(
        client, active_positions, snapshot_path=snapshot_path
    )

    if not active_positions:
        _save_open_snapshot(current_snapshot, snapshot_path=snapshot_path)
        return [], current_snapshot, closed_actions, 0

    if not contracts_by_id:
        contracts_data = client.list_contracts()
        for c in contracts_data.get("contracts", []):
            contracts_by_id[c["contract_id"]] = c

    actions = list(closed_actions)
    remaining_positions: list[Position] = []
    invalid_entry_assets: list[str] = []
    effective_now = now_utc or datetime.now(timezone.utc)
    forecast_meta = _load_forecast_position_meta()
    active_cids_str = {str(p_raw.get("contract_id")) for p_raw in active_positions}
    for stale_cid in [k for k in list(forecast_meta.keys()) if k not in active_cids_str]:
        try:
            _clear_forecast_position_meta(int(stale_cid))
        except (TypeError, ValueError):
            pass

    for p_raw in active_positions:
        cid = p_raw["contract_id"]
        contract = contracts_by_id.get(cid)
        if not contract:
            continue

        market_id = f"{p_raw['contract_base_asset']}-USD-PERP"
        try:
            current_price = float(client.latest_rate(market_id)["rate"])
        except Exception as e:
            log.warning(f"Skip {p_raw['contract_base_asset']}: {e}")
            continue

        ac = p_raw.get("auto_close") or {}
        tp_val = ac.get("take_profit", {}).get("trigger_price_quote")
        sl_val = ac.get("stop_loss", {}).get("trigger_price_quote")

        pos_key = make_position_dedupe_key(p_raw)
        fc_info = forecast_meta.get(str(cid)) or {}
        pos = Position(
            contract_id=cid,
            base_asset=p_raw["contract_base_asset"],
            side=p_raw["side"],
            leverage=int(p_raw["leverage"]),
            margin=float(p_raw["margin_quote"]),
            entry_price=float(p_raw["avg_entry_price"]),
            current_price=current_price,
            tp=float(tp_val) if tp_val else None,
            sl=float(sl_val) if sl_val else None,
            funding_paid=float(p_raw.get("funding_paid_quote", 0)),
            liquidation_price=float(p_raw["liquidation_price"]),
            position_id=pos_key,
            opened_at=str(p_raw.get("opened_at") or p_raw.get("created_at") or ""),
            signal_source=str(fc_info.get("signal_source") or "funding"),
            planned_sl_pct=fc_info.get("planned_sl_pct"),
            planned_tp_pct=fc_info.get("planned_tp_pct"),
            max_hold_hours=fc_info.get("max_hold_hours"),
        )
        current_funding_daily = float(contract["funding_rate_hourly"]) * 24

        closed_now = False
        try:
            res = evaluate_and_apply_position_risk(
                client, pos, current_funding_daily, now_utc=effective_now
            )
            if res and res.get("action") != "FAILED":
                if res.get("action") == "CLOSE":
                    current_snapshot.pop(pos_key, None)
                    current_snapshot.pop(str(cid), None)
                    _clear_forecast_position_meta(cid)
                    closed_now = True
                elif res.get("action") == "UPDATE_AUTO_CLOSE":
                    if res.get("tp") is not None:
                        pos.tp = float(res["tp"])
                    if res.get("sl") is not None:
                        pos.sl = float(res["sl"])
                elif res.get("action") == "SKIP" and "entry_price" in str(res.get("reason", "")):
                    invalid_entry_assets.append(pos.base_asset)
                detail_str = res.get("detail") or ""
                if cycle_label:
                    detail_str = f"[{cycle_label}] {detail_str}"
                actions.append({
                    "asset": pos.base_asset,
                    "action": res.get("action"),
                    "detail": detail_str,
                    "result": res,
                })
        except Exception as e:
            log.error(f"{pos.base_asset} evaluate_and_apply_position_risk error: {e}")

        if not closed_now:
            remaining_positions.append(pos)

    _log_failure_summary(
        "risk_monitor.invalid_entry_price",
        invalid_entry_assets,
        total=len(active_positions),
    )
    _save_open_snapshot(current_snapshot, snapshot_path=snapshot_path)
    return remaining_positions, current_snapshot, actions, len(active_positions)


def _maybe_run_adaptive_executions(
    client: WaltClient,
    contracts_by_id: dict[int, dict],
    active_positions_count: int,
    recent_tp_closed: bool = False,
    regime_path: str = "logs/market_regime.json",
    rng: Optional[Any] = None,
) -> list[dict]:
    """
    Evaluasi apakah Market Execute (Saham & Komoditas) atau Crypto Execute perlu dipicu lebih cepat
    (30 menit pada Gear 3 / 60 menit pada Gear 2 / 120 menit pada Gear 1) berdasarkan parameter funding
    dan ketersediaan slot posisi.
    """
    state = _load_execution_regime(regime_path)
    now = time.time()

    last_market_ts = float(state.get("last_market_exec_ts", 0.0) or 0.0)
    last_crypto_ts = float(state.get("last_crypto_exec_ts", 0.0) or 0.0)

    # Jika belum pernah tercatat di cache, inisialisasi timestamp sekarang agar mulai menghitung interval
    if last_market_ts <= 0 or last_crypto_ts <= 0:
        if last_market_ts <= 0:
            state["last_market_exec_ts"] = now
            last_market_ts = now
        if last_crypto_ts <= 0:
            state["last_crypto_exec_ts"] = now
            last_crypto_ts = now
        _save_execution_regime(state, regime_path=regime_path)
        if not recent_tp_closed:
            return []

    elapsed_market_min = (now - last_market_ts) / 60.0
    elapsed_crypto_min = (now - last_crypto_ts) / 60.0

    # Pengecekan tercepat 60m (toleransi 55m), atau 45m khusus jika baru saja benar-benar tutup TP profit
    min_wait = 45.0 if recent_tp_closed else 55.0
    if elapsed_market_min < min_wait and elapsed_crypto_min < min_wait:
        return []

    # Ambil daftar kontrak jika belum ada untuk mengevaluasi peluang funding terkini
    if not contracts_by_id:
        try:
            c_data = client.list_contracts()
            for c in c_data.get("contracts", []):
                contracts_by_id[c["contract_id"]] = c
        except Exception as e:
            log.warning(f"Gagal ambil contracts untuk adaptive execution check: {e}")
            return []

    all_contracts = list(contracts_by_id.values())
    market_opps = rank_opportunities(all_contracts, allowed_assets=(ALLOWED_STOCKS | ALLOWED_COMMODITIES))
    crypto_opps = rank_opportunities(all_contracts, allowed_assets=ALLOWED_CRYPTO)

    m_gear, m_interval, m_reason = _update_regime_state(
        "market", market_opps, mark_executed=False, recent_tp_closed=recent_tp_closed, regime_path=regime_path, rng=rng
    )
    c_gear, c_interval, c_reason = _update_regime_state(
        "crypto", crypto_opps, mark_executed=False, recent_tp_closed=recent_tp_closed, regime_path=regime_path, rng=rng
    )

    m_tp_boost = recent_tp_closed and elapsed_market_min >= 45.0 and (
        len(market_opps) >= 3 or any(abs(o.funding_daily) >= 0.008 for o in market_opps)
    )
    c_tp_boost = recent_tp_closed and elapsed_crypto_min >= 45.0 and (
        len(crypto_opps) >= 3 or any(abs(o.funding_daily) >= 0.008 for o in crypto_opps)
    )

    due_market = elapsed_market_min >= (m_interval - 2.0) or m_tp_boost
    due_crypto = elapsed_crypto_min >= (c_interval - 2.0) or c_tp_boost

    if not due_market and not due_crypto:
        return []

    # Cek balance & kuota slot sebelum mengeksekusi (Gear 0 Smart Gatekeeper)
    try:
        bal = client.balance()
        accounts = bal.get("accounts", [])
        available = float(accounts[0].get("balance_available", 0.0)) if accounts else 0.0
        open_pos = client.list_positions(statuses=["open"])
        open_list = [
            p for p in open_pos.get("positions", [])
            if str(p.get("status", "open")).lower() in ("open", "active")
        ]
        total_eq = get_total_equity(bal, open_pos)
        max_slots = get_dynamic_max_slots(total_eq)
        if len(open_list) >= max_slots or available < 3.0:
            log.info(
                f"💤 [GEAR 0 GATEKEEPER] Adaptive execute ditunda: Slot {len(open_list)}/{max_slots}, Available ${available:.2f}"
            )
            return []
    except Exception as e:
        log.warning(f"Gagal verifikasi saldo/slot untuk adaptive execution: {e}")
        return []

    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    triggered_actions: list[dict] = []

    if due_market:
        log.info(
            f"🚀 [ADAPTIVE EXECUTE - {m_gear}] Memicu Market Execute "
            f"(elapsed {elapsed_market_min:.1f}m >= {m_interval}m | {m_reason})"
        )
        try:
            m_res = market_execute(client, dry_run=dry_run, title=f"🚀 Walt Market Execute ({m_gear})")
            exec_cnt = len([e for e in m_res.get("executions", []) if e.get("verdict") in ("EXECUTED", "DRY_RUN")])
            triggered_actions.append({
                "asset": "MARKET_UNIVERSE",
                "action": "ADAPTIVE_MARKET_EXECUTE",
                "detail": f"{m_gear} ({m_interval}m): {exec_cnt} opened",
            })
        except Exception as e:
            log.error(f"Adaptive market_execute error: {e}")

    if due_crypto:
        log.info(
            f"⚡ [ADAPTIVE EXECUTE - {c_gear}] Memicu Crypto Execute "
            f"(elapsed {elapsed_crypto_min:.1f}m >= {c_interval}m | {c_reason})"
        )
        try:
            c_res = crypto_execute(client, dry_run=dry_run)
            exec_cnt = len([e for e in c_res.get("executions", []) if e.get("verdict") in ("EXECUTED", "DRY_RUN")])
            triggered_actions.append({
                "asset": "CRYPTO_UNIVERSE",
                "action": "ADAPTIVE_CRYPTO_EXECUTE",
                "detail": f"{c_gear} ({c_interval}m): {exec_cnt} opened",
            })
        except Exception as e:
            log.error(f"Adaptive crypto_execute error: {e}")

    return triggered_actions


JOB_START_MONO = time.monotonic()
JOB_TIME_GUARD_MIN = 6.0  # Guard waktu minimum (menit): dipakai di jalur monitor DAN jalur eksekusi sebelum membuka order baru
AUTO_CLOSE_RETRY_BUDGET_MIN = 1.5  # Ambang minimum (menit) sebelum mengirim order agar tersedia anggaran retry autoClose TP/SL


def get_job_remaining_minutes(
    start_mono: Optional[float] = None,
    now_mono: Optional[float] = None,
) -> float:
    """Hitung sisa waktu job (menit) terhadap batas env JOB_TIMEOUT_MIN (default 20 menit)."""
    try:
        timeout_min = float(os.getenv("JOB_TIMEOUT_MIN", "20"))
    except ValueError:
        timeout_min = 20.0
    s = JOB_START_MONO if start_mono is None else start_mono
    n = time.monotonic() if now_mono is None else now_mono
    elapsed_min = max(0.0, (n - s) / 60.0)
    return timeout_min - elapsed_min


def has_enough_job_time(
    min_remaining_min: float = JOB_TIME_GUARD_MIN,
    start_mono: Optional[float] = None,
    now_mono: Optional[float] = None,
) -> bool:
    """True jika sisa waktu job >= min_remaining_min (default 6 menit)."""
    return get_job_remaining_minutes(start_mono=start_mono, now_mono=now_mono) >= min_remaining_min


def _has_recent_tp_close(actions: list[dict]) -> bool:
    """True hanya jika ada penutupan posisi riil (CLOSE / EXCHANGE_CLOSE_DETECTED) karena TP/Trailing (bukan UNKNOWN)."""
    for a in actions:
        if a.get("action") not in ("CLOSE", "EXCHANGE_CLOSE_DETECTED"):
            continue
        detail = str(a.get("detail", "")).lower()
        if "unknown" in detail:
            continue
        if "tp" in detail or "trailing" in detail:
            return True
    return False


def risk_monitor_run(
    client: WaltClient,
    snapshot_path: str = "logs/open_snapshot.json",
    enable_dynamic_subcycle: Optional[bool] = None,
    enable_adaptive_execute: Optional[bool] = None,
    sleep_fn: Any = time.sleep,
    rng: Optional[Any] = None,
) -> dict:
    """
    Dispatcher utama risk monitor — dipicu tiap 10 menit dari scheduled task,
    dengan kemampuan Dynamic Adaptive Cadence (5 menit vs 10 menit / 6x hingga 12x per jam):
    - Jika 0 posisi aktif: Smart Fast-Exit Idle (<2 detik).
    - Jika >= 3 posisi aktif, ada posisi kategori HALF, atau posisi sudah dekat TP/SL:
      otomatis menjalankan sub-cycle ke-2 setelah jeda random ~5 menit (240-290 detik).
    - Jika posisi kategori GO (< 3 posisi & jauh dari TP/SL): interval normal 10 menit,
      namun sesekali (35% random) tetap menjalankan spot-check 5 menit.
    """
    if enable_dynamic_subcycle is None:
        enable_dynamic_subcycle = os.getenv("DYNAMIC_MONITOR_SUBCYCLE", "true").lower() == "true"
    if enable_adaptive_execute is None:
        enable_adaptive_execute = os.getenv("ADAPTIVE_EXECUTION_ENABLED", "true").lower() == "true"

    contracts_by_id: dict[int, dict] = {}
    remaining_positions, current_snapshot, actions, checked_count = _run_single_monitor_pass(
        client, contracts_by_id, snapshot_path=snapshot_path
    )

    recent_tp_closed = _has_recent_tp_close(actions)

    if checked_count == 0:
        if enable_adaptive_execute:
            if not has_enough_job_time():
                log.warning("skip: waktu job hampir habis (adaptive execute saat idle dilewati)")
            else:
                adaptive_acts = _maybe_run_adaptive_executions(
                    client,
                    contracts_by_id,
                    active_positions_count=0,
                    recent_tp_closed=recent_tp_closed,
                    rng=rng,
                )
                actions.extend(adaptive_acts)
        log.info("💤 [SMART IDLE] 0 posisi aktif. Semua order aman/kas cair. Masuk mode tidur (fast-exit).")
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": "idle",
            "positions_checked": 0,
            "cycles_run": 1,
            "cadence_mode": "idle_10m",
            "actions": actions,
        }

    verdicts_by_cid = {}
    for cid_k, info in current_snapshot.items():
        raw_cid = info.get("contract_id", cid_k)
        if str(raw_cid).isdigit():
            verdicts_by_cid[int(raw_cid)] = str(info.get("verdict", "GO")).upper()
    should_subcycle, delay_sec, cadence_reason = decide_dynamic_monitor_cadence(
        remaining_positions, verdicts_by_cid=verdicts_by_cid, rng=rng
    )

    cycles_run = 1
    cadence_mode = "normal_10m"

    if should_subcycle and enable_dynamic_subcycle:
        if not has_enough_job_time():
            log.warning("skip: waktu job hampir habis (sub-cycle 5 menit dilewati)")
        else:
            cadence_mode = "dynamic_5m"
            log.info(
                f"⚡ [DYNAMIC MONITOR] Mode 5-Menit Aktif ({cadence_reason}) → "
                f"tunggu {delay_sec}s (~{delay_sec/60:.1f} menit) untuk evaluasi sub-cycle ke-2..."
            )
            sleep_fn(delay_sec)
            remaining_positions, current_snapshot, actions_2, checked_2 = _run_single_monitor_pass(
                client, contracts_by_id, snapshot_path=snapshot_path, cycle_label="5m-SubCycle"
            )
            actions.extend(actions_2)
            cycles_run = 2
            log.info(
                f"✅ [DYNAMIC MONITOR] Sub-cycle 5 menit selesai (checked={checked_2}, new_actions={len(actions_2)})."
            )
    else:
        log.info(f"⏱️ [DYNAMIC MONITOR] Interval normal 10 menit ({cadence_reason}).")

    if enable_adaptive_execute:
        if not has_enough_job_time():
            log.warning("skip: waktu job hampir habis (adaptive execute dilewati)")
        else:
            recent_tp_closed = _has_recent_tp_close(actions)
            adaptive_acts = _maybe_run_adaptive_executions(
                client,
                contracts_by_id,
                active_positions_count=len(remaining_positions),
                recent_tp_closed=recent_tp_closed,
                rng=rng,
            )
            actions.extend(adaptive_acts)

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "positions_checked": checked_count,
        "cycles_run": cycles_run,
        "cadence_mode": cadence_mode,
        "cadence_reason": cadence_reason,
        "delay_seconds": delay_sec if should_subcycle else 0,
        "actions": actions,
    }


# =============================================================================
# DAILY SCAN — rekomendasi + income tracker
# =============================================================================

def _convert_forecast_dicts_to_opps(fc_raw: list[dict], log_label: str) -> list[Opportunity]:
    fc_opps_out: list[Opportunity] = []
    for fc in fc_raw:
        fc_opps_out.append(Opportunity(
            contract_id=int(fc["contract_id"]),
            base_asset=str(fc["asset"]),
            funding_daily=float(fc.get("funding_daily") or 0.0),
            volume_24h=float(fc.get("volume_24h") or MIN_VOL_24H),
            max_leverage=int(fc.get("max_leverage") or MAX_LEVERAGE),
            side=fc["side"],
            min_size_base=float(fc.get("min_size_base") or 0.0),
            max_size_base=float(fc.get("max_size_base") or 0.0),
            max_size_quote=float(fc.get("max_size_quote") or 0.0),
            price_slippage=float(fc.get("price_slippage") or 0.0),
            signal_source="price_forecast",
            forecast_setup=str(fc.get("setup") or ""),
            planned_sl_pct=float(fc["sl_pct"]),
            planned_tp_pct=float(fc["tp_pct"]),
            max_hold_hours=float(fc["max_hold_hours"]),
            expected_net_edge_pct=float(fc["expected_net_edge_pct"]),
            forecast_reason=str(fc.get("reason") or ""),
            grav_bull_pressure=fc.get("bull_pressure"),
        ))
    if fc_raw:
        log.info(
            f"📈 [{log_label}] Menambahkan {len(fc_raw)} kandidat Engine 2 OOS: "
            + ", ".join(f"{x['asset']}({x['setup']}/{x['side']})" for x in fc_raw)
        )
    return fc_opps_out


def _scan_crypto_engine2_opportunities(
    contracts: list[dict],
    target_universe: set[str],
    exclude_assets: set[str],
    *,
    log_label: str = "ENGINE 2 CRYPTO",
) -> list[Opportunity]:
    """Pindai kandidat Engine 2 Crypto (`all` seluruh crypto likuid atau `tier1` 10 aset) bila aktif."""
    if not is_crypto_forecast_engine_enabled():
        return []
    try:
        import price_forecast

        universe_mode = get_crypto_forecast_universe_mode()
        scan_allowed: Optional[set[str]] = None if universe_mode == "all" else target_universe
        ep_crypto = {
            str(c.get("base_asset") or "").strip().upper() for c in contracts
        } - {str(x).strip().upper() for x in exclude_assets} - (ALLOWED_STOCKS | ALLOWED_COMMODITIES)
        ep_crypto.discard("")
        if scan_allowed is not None:
            ep_crypto &= scan_allowed
        scan_cap = max(0, _cfg_int("forecast_max_scan_assets", 50))
        fc_raw = price_forecast.scan_crypto_price_opportunities(
            contracts,
            allowed_assets=scan_allowed,
            exclude_assets=exclude_assets,
            exclude_non_crypto=(ALLOWED_STOCKS | ALLOWED_COMMODITIES),
            min_vol_24h=MIN_VOL_24H,
            max_candidates=scan_cap,
        )
        log.info(
            f"🔎 [COVERAGE {log_label}] kandidat crypto likuid diperiksa: {len(ep_crypto)} "
            f"(scan_max={'ALL' if scan_cap == 0 else scan_cap}, mode={universe_mode}) → {len(fc_raw)} sinyal"
        )
        return _convert_forecast_dicts_to_opps(fc_raw, log_label)
    except Exception as exc:
        log.warning(f"Engine 2 Crypto scan dilewati: {exc}")
        return []


def _scan_stock_engine2_opportunities(
    contracts: list[dict],
    target_stocks: set[str],
    exclude_assets: set[str],
    *,
    log_label: str = "ENGINE 2 STOCK",
) -> list[Opportunity]:
    """Pindai kandidat Engine 2 Saham AS (`ALLOWED_STOCKS`, LONG-ONLY Dip-Buy) saat jam bursa AS buka."""
    if not is_stock_forecast_engine_enabled():
        return []
    try:
        import price_forecast

        target_syms = {str(x).strip().upper() for x in target_stocks}
        ep_stock = {
            str(c.get("base_asset") or "").strip().upper() for c in contracts
        } & target_syms - {str(x).strip().upper() for x in exclude_assets}
        sig_cap = max(1, _cfg_int("stock_forecast_max_signals", 3))
        fc_raw = price_forecast.scan_stock_price_opportunities(
            contracts,
            allowed_assets=target_stocks,
            exclude_assets=exclude_assets,
            min_vol_24h=MIN_VOL_24H,
            max_signals=sig_cap,
        )
        log.info(
            f"🔎 [COVERAGE {log_label}] kandidat saham diperiksa: {len(ep_stock)} "
            f"(max_signals={sig_cap}) → {len(fc_raw)} sinyal"
        )
        return _convert_forecast_dicts_to_opps(fc_raw, log_label)
    except Exception as exc:
        log.warning(f"Engine 2 Saham AS scan dilewati: {exc}")
        return []


def daily_scan(client: WaltClient, now_utc: Optional[datetime] = None) -> dict:
    """Scan harian — return summary untuk Calendar event / push notif."""
    readiness = client.readiness()
    balance = client.balance()
    positions = client.list_positions(statuses=["open"])
    contracts = client.list_contracts()

    log.info(f"🔎 [COVERAGE] {len(contracts.get('contracts') or [])} kontrak Walt dimuat untuk daily_scan")
    opps = rank_opportunities(contracts["contracts"])
    market_opps = rank_opportunities(contracts["contracts"], allowed_assets=(ALLOWED_STOCKS | ALLOWED_COMMODITIES))
    crypto_opps = rank_opportunities(contracts["contracts"], allowed_assets=ALLOWED_CRYPTO)
    fc_crypto_opps = _scan_crypto_engine2_opportunities(
        contracts["contracts"],
        ALLOWED_CRYPTO,
        exclude_assets={o.base_asset for o in opps},
        log_label="DAILY_SCAN ENGINE 2",
    )
    us_open_scan, _ = is_us_stock_market_open_utc(now_utc=now_utc)
    fc_stock_opps = (
        _scan_stock_engine2_opportunities(
            contracts["contracts"],
            ALLOWED_STOCKS,
            exclude_assets={o.base_asset for o in opps} | {o.base_asset for o in fc_crypto_opps},
            log_label="DAILY_SCAN ENGINE 2 STOCK",
        )
        if us_open_scan
        else []
    )
    fc_all_opps = fc_crypto_opps + fc_stock_opps
    if fc_crypto_opps:
        opps.extend(fc_crypto_opps)
        crypto_opps.extend(fc_crypto_opps)
    if fc_stock_opps:
        opps.extend(fc_stock_opps)
        market_opps.extend(fc_stock_opps)
    m_gear, m_int, m_reason = _update_regime_state("market", market_opps, mark_executed=False)
    c_gear, c_int, c_reason = _update_regime_state("crypto", crypto_opps, mark_executed=False)
    log.info(f"⚙️ [EXECUTION GEAR] Market: {m_gear} ({m_int}m) | Crypto: {c_gear} ({c_int}m)")

    total_equity = get_total_equity(balance, positions)
    scan_limit = max(5, get_dynamic_max_slots(total_equity))
    top_candidates = list(opps[:scan_limit])
    if fc_all_opps:
        seen_top = {o.base_asset for o in top_candidates}
        for fc_opp in fc_all_opps:
            if fc_opp.base_asset not in seen_top:
                top_candidates.append(fc_opp)
                seen_top.add(fc_opp.base_asset)

    # Fetch live price untuk top candidates (agregasi kegagalan per-kandidat menjadi 1 ringkasan)
    rate_failures: list[str] = []
    for opp in top_candidates:
        try:
            r = client.latest_rate(f"{opp.base_asset}-USD-PERP")
            opp_price = float(r["rate"])
        except Exception as exc:
            log.debug(f"Gagal fetch latest_rate untuk {opp.base_asset} ({type(exc).__name__}): {exc}")
            rate_failures.append(opp.base_asset)
            opp_price = 0.0
        setattr(opp, "live_price", opp_price)
        if opp_price > 0 and opp.min_size_base > 0:
            opp.min_margin_2x = round(
                (opp.min_size_base * opp_price / MAX_LEVERAGE) * (1.0 + opp.price_slippage), 2
            )
    _log_failure_summary("daily_scan.latest_rate", rate_failures, total=len(top_candidates))

    # Income tracker (akumulasi bulan berjalan: posisi closed bulan ini + posisi open saat ini)
    open_funding = sum(
        float(p.get("funding_paid_quote", 0) or 0.0) for p in positions.get("positions", [])
    )
    closed_funding = 0.0
    try:
        from sheet_logger import get_closed_funding_current_month
        closed_funding = get_closed_funding_current_month()
    except Exception as e:
        log.warning(f"Gagal mengambil closed funding bulan berjalan: {e}")
    total_funding = round(open_funding + closed_funding, 6)
    progress_pct = (total_funding / MONTHLY_TARGET) * 100

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "balance": balance.get("accounts", []),
        "total_equity": total_equity,
        "positions_count": len(positions.get("positions", [])),
        "funding_earned": total_funding,
        "target": MONTHLY_TARGET,
        "progress_pct": progress_pct,
        "top_opportunities": [
            {
                "contract_id": o.contract_id,
                "asset": o.base_asset,
                "side": o.side,
                "funding_daily": o.funding_daily,
                "funding_daily_pct": o.funding_daily * 100,
                "price": getattr(o, "live_price", 0),
                "min_margin_2x": o.min_margin_2x,
                "signal_source": getattr(o, "signal_source", "funding"),
                "forecast_setup": getattr(o, "forecast_setup", ""),
                "planned_sl_pct": getattr(o, "planned_sl_pct", 0.0),
                "planned_tp_pct": getattr(o, "planned_tp_pct", 0.0),
                "max_hold_hours": getattr(o, "max_hold_hours", 0.0),
                "expected_net_edge_pct": getattr(o, "expected_net_edge_pct", 0.0),
                "forecast_reason": getattr(o, "forecast_reason", ""),
                "grav_bull_pressure": getattr(o, "grav_bull_pressure", None),
            }
            for o in top_candidates
        ],
        "_raw_opportunities": opps,
        "execution_regime": {
            "market_gear": m_gear,
            "market_interval_min": m_int,
            "market_reason": m_reason,
            "crypto_gear": c_gear,
            "crypto_interval_min": c_int,
            "crypto_reason": c_reason,
        },
        "readiness": readiness,
    }


# =============================================================================
# SCAN WITH RESEARCH — daily scan + Qwen/Gemini analysis + Instant Auto-Execution
# =============================================================================

def format_summary_telegram(
    summary: dict,
    researches: dict[str, Research],
    executions: Optional[list[dict]] = None,
) -> str:
    """Format summary jadi Telegram-ready markdown dengan status eksekusi riil."""
    lines = []
    lines.append("*📊 Walt Perps — Daily Scan & Auto-Execute*")
    ts = summary.get("ts") or summary.get("scanned_at") or datetime.now(timezone.utc).isoformat()
    lines.append(f"_{ts[:16].replace('T', ' ')} UTC_\n")

    # Balance
    accounts = summary.get("balance", [])
    if accounts:
        for a in accounts:
            lines.append(f"💰 Saldo: `${a['balance_available']}` {a['currency']}")
    else:
        lines.append("💰 Saldo: `$0` (belum deposit)")

    lines.append(f"📍 Posisi terbuka: *{summary['positions_count']}*")

    # Income tracker
    earned = summary["funding_earned"]
    target = summary["target"]
    pct = summary["progress_pct"]
    lines.append(f"\n*💵 Income Tracker Bulan Ini*")
    lines.append(f"Progress: `${earned:.4f} / ${target:.0f}` ({pct:.1f}%)")
    status = "🟢 ON TRACK" if pct >= 50 else "🟡 BEHIND" if pct >= 20 else "🔴 JAUH"
    lines.append(f"Status: {status}")

    # Top opportunities + research + execution
    lines.append(f"\n*🎯 Top Peluang & Prosedur Eksekusi*")
    top_opps_tg = list(summary["top_opportunities"][:5])
    seen_tg = {o["asset"] for o in top_opps_tg}
    for o in summary["top_opportunities"][5:]:
        if o.get("signal_source") == "price_forecast" and o["asset"] not in seen_tg and len(top_opps_tg) < 8:
            top_opps_tg.append(o)
            seen_tg.add(o["asset"])
    for i, opp in enumerate(top_opps_tg, 1):
        asset = opp["asset"]
        side = opp["side"].upper()
        funding = opp["funding_daily_pct"]
        price = opp["price"]
        research = researches.get(asset)

        # Decision
        from types import SimpleNamespace
        pseudo_opp = SimpleNamespace(
            base_asset=asset,
            side=opp["side"],
            funding_daily=funding / 100,
            signal_source=opp.get("signal_source", "funding"),
            forecast_setup=opp.get("forecast_setup", ""),
            expected_net_edge_pct=float(opp.get("expected_net_edge_pct") or 0.0),
            forecast_reason=opp.get("forecast_reason", ""),
            planned_sl_pct=float(opp.get("planned_sl_pct") or 0.0),
            planned_tp_pct=float(opp.get("planned_tp_pct") or 0.0),
            max_hold_hours=float(opp.get("max_hold_hours") or 0.0),
            grav_bull_pressure=opp.get("grav_bull_pressure"),
        )
        verdict, reason, mult = decide_go_nogo(pseudo_opp, research)  # type: ignore

        # Cek execution status jika ada
        ex = next((e for e in (executions or []) if e.get("asset") == asset), None)
        if ex and ex.get("verdict") == "EXECUTED":
            badge = "✅ [EXECUTED]"
        elif ex and ex.get("verdict") == "DRY_RUN":
            badge = "🧪 [DRY RUN]"
        elif ex and ex.get("verdict") == "SKIP" and "posisi sudah" in ex.get("reason", ""):
            badge = "⏭️ [SKIP - AKTIF]"
        elif ex and ex.get("verdict") in ("SKIP", "FAILED", "ERROR"):
            badge = f"⚠️ [{ex.get('verdict')}]"
        else:
            badge = {"GO": "✅ GO", "HALF": "⚠️ HALF", "SKIP": "❌ SKIP"}.get(verdict, "❓")

        lines.append(f"\n{i}. *{badge}* • *{side} {asset}* @ `${price:.4f}`")
        if opp.get("signal_source") == "price_forecast":
            lines.append(
                f"   Engine 2: `{opp.get('forecast_setup', 'FADE')}` "
                f"(net `{float(opp.get('expected_net_edge_pct') or 0.0)*100:+.2f}%`) | Funding: `{funding:+.3f}%/hari`"
            )
        else:
            lines.append(f"   Funding: `{funding:+.3f}%/hari`")
        if ex and ex.get("verdict") == "EXECUTED":
            order_id = ex.get("order_id")
            order_str = f" | Order: `{order_id[:8]}...`" if order_id else ""
            lines.append(f"   ⚡ Order: Margin `${ex.get('margin', 0):.2f}` (lev 2x){order_str}")
            if ex.get("tp") and ex.get("sl"):
                lines.append(f"   🎯 TP: `${ex['tp']:.4f}` | 🛡️ SL: `${ex['sl']:.4f}`")
        elif ex and ex.get("verdict") == "SKIP" and "posisi sudah" in ex.get("reason", ""):
            lines.append(f"   ℹ️ _Posisi untuk kontrak ini sudah terbuka di akun._")

        if research:
            days = research.days_to_earnings
            earn_str = f"{days}d" if days is not None else "—"
            lines.append(f"   Earnings: {earn_str} | Sentiment: *{research.news_sentiment}* (Score: `{research.final_score:+.2f}`)")
            if research.consensus and research.consensus not in ("NONE", "NO_DATA"):
                sec_prov = getattr(research, "secondary_provider", "gemini")
                if sec_prov == "oak_single":
                    lines.append(f"   🤖 OAK Deep Analyst: `{research.overall_consensus_level}` (Single-Pass)")
                else:
                    lines.append(f"   🤖 Swarm AI: `{research.overall_consensus_level}` (Vendor: `{research.vendor_alignment}`)")
                    if research.qwen_sentiment and research.gemini_sentiment:
                        sec_label = "GLM" if sec_prov == "glm" else "Gemini"
                        lines.append(f"   ├ Qwen: `{research.qwen_sentiment}` | {sec_label}: `{research.gemini_sentiment}`")
                p1_7 = research.p1_to_p7_scores
                if p1_7:
                    lines.append(
                        f"   ├ P1(`{p1_7.get('P1_sentiment_polarity', 0.0):+.2f}`) "
                        f"P4(`{p1_7.get('P4_momentum_direction', 0.0):+.2f}`) "
                        f"P5(`{p1_7.get('P5_risk_catalysts', 0.0):.2f}`) "
                        f"P6(`{p1_7.get('P6_contrarian_check', 0.0):.2f}`)"
                    )
            if research.narrative:
                lines.append(f"   {md_italic(research.narrative[:180])}")
        lines.append(f"   → Keputusan: *{verdict}* ({md_escape(reason)})")

    executed_list = [e for e in (executions or []) if e.get("verdict") == "EXECUTED"]
    if executed_list:
        lines.append(f"\n🚀 *Posisi Baru Dibuka ({len(executed_list)}):*")
        for e in executed_list:
            order_id = e.get("order_id", "N/A")
            oid_short = f"`{order_id[:8]}...`" if order_id != "N/A" else "N/A"
            lines.append(f"• *{e['side'].upper()} {e['asset']}*: margin `${e['margin']:.2f}` @ `${e.get('price', 0):.4f}` (Order ID: {oid_short})")

    lines.append(f"\n_Fee taker {TAKER_FEE_PER_SIDE*100:.2f}%/sisi | Max lev {MAX_LEVERAGE}x | Proteksi Auto TP/SL_")
    return "\n".join(lines)


def scan_with_research(
    client: WaltClient,
    auto_execute: Optional[bool] = None,
    dry_run: Optional[bool] = None,
    now_utc: Optional[datetime] = None,
) -> dict:
    """Daily scan + Dual-AI research (Qwen + Gemini) + Instant Auto-Execution pada sinyal GO/HALF + Telegram notif."""
    if auto_execute is None:
        auto_execute = os.getenv("AUTO_EXECUTE_ON_SCAN", "true").lower() == "true"
    if dry_run is None:
        dry_run = os.getenv("DRY_RUN", "false").lower() == "true"

    log.info(f"📊 Running daily scan + research (auto_execute={auto_execute}, dry_run={dry_run})...")
    summary = daily_scan(client, now_utc=now_utc)

    us_stocks_open, us_stocks_note = is_us_stock_market_open_utc(now_utc=now_utc)
    summary["us_stock_market_open"] = us_stocks_open
    summary["us_stock_market_note"] = us_stocks_note
    if not us_stocks_open:
        log.info(
            f"🕒 [US STOCK HOURS] {us_stocks_note} — Saham AS dilewati pada daily-scan ini; "
            "Crypto & Komoditas tetap 24/7."
        )
        summary["top_opportunities"] = [
            o for o in summary["top_opportunities"] if o["asset"] not in ALLOWED_STOCKS
        ]

    # Evaluasi Gear & kebijakan routing AI (Opsi A: GLM DashScope + GPT-6.1 Sol & Claude Opus 5.5 ClassAI)
    import llm_research as _lr

    # Prefetch konteks makro & kalender OAK Research MCP 1x per siklus (non-blocking)
    try:
        import oak_mcp_client

        if oak_mcp_client.is_oak_mcp_configured():
            oak_brief = oak_mcp_client.fetch_oak_global_brief()
            summary["oak_mcp_connected"] = bool(oak_brief.get("available"))
            log.info(f"🌳 [OAK MCP] Global brief connected={summary['oak_mcp_connected']}")
    except Exception as exc:
        log.warning(f"OAK MCP global prefetch dilewati: {exc}")

    active_top_opps = [
        o for o in summary["top_opportunities"]
        if abs(float(o.get("funding_daily", float(o.get("funding_daily_pct", 0.0) or 0.0) / 100.0) or 0.0)) >= MIN_FUNDING_DAILY
        or o.get("signal_source") == "price_forecast"
    ]
    opp_count = len(active_top_opps)
    scan_gear, _, scan_gear_reason = evaluate_execution_gear(
        summary.get("_raw_opportunities", []),
        go_count=opp_count,
        category="market",
    )
    research_mode = _lr.get_research_mode()
    full_research_gear3 = is_gear3_full_research(scan_gear)
    scan_is_idle = scan_gear.startswith("GEAR_0") or opp_count <= 1
    use_glm_routing = (research_mode == "swarm") and _lr.should_use_glm_for_secondary_voters(
        opportunity_count=opp_count, is_idle=scan_is_idle
    )
    ai_mode_label = (
        "OAK-First Single Deep Analyst"
        if research_mode == "oak_single"
        else ("GLM (DashScope)" if use_glm_routing else "Gemini (Idle)")
    )
    log.info(
        f"⚙️ [SCAN GEAR] {scan_gear}: {scan_gear_reason} | "
        f"AI Mode={ai_mode_label} (peluang={opp_count})"
    )

    qwen = QwenClient() if os.getenv("DASHSCOPE_API_KEY") else None
    gemini = (
        GeminiClient()
        if (research_mode == "oak_single" or not use_glm_routing or qwen is None)
        and (os.getenv("DASHSCOPE_API_GEMINI_KEY") or os.getenv("GEMINI_API_KEY"))
        else None
    )
    researches: dict[str, Research] = {}

    crypto_research = crypto_research_set(
        [(o["asset"], o.get("funding_daily")) for o in summary["top_opportunities"]]
    )
    if full_research_gear3:
        log.info("  🧠 [GEAR 3] Izin riset & analisa AI aktif untuk SETIAP kandidat keputusan trading.")
    elif crypto_research:
        _cap_txt = _cfg_int("crypto_research_top_n", CRYPTO_RESEARCH_TOP_N)
        log.info(f"  🧠 riset AI crypto/komoditas dibatasi {_cap_txt} |funding| "
                 f"terbesar: {', '.join(sorted(crypto_research))}")

    eligible_opps: list[dict] = []
    seen_research_assets: set[str] = set()
    for opp in summary["top_opportunities"]:
        asset = opp["asset"]
        if asset in seen_research_assets:
            continue
        is_forecast = opp.get("signal_source") == "price_forecast"
        # Gear 3: setiap kandidat keputusan trading boleh diteliti AI; selain itu saham + top-N crypto + Engine 2.
        worth_calling = (
            full_research_gear3
            or asset in ALLOWED_STOCKS
            or asset in crypto_research
            or is_forecast
        )
        if (asset in RESEARCH_ELIGIBLE or is_forecast) and worth_calling and (qwen or gemini):
            seen_research_assets.add(asset)
            eligible_opps.append(opp)

    if eligible_opps:
        if not has_enough_job_time(2.5):
            log.warning(
                f"⏳ Sisa waktu job tinggal {get_job_remaining_minutes():.1f} menit; "
                "hentikan loop riset AI agar kandidat yang sudah diriset sempat dieksekusi & dilog."
            )
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            max_w = min(2 if research_mode == "swarm" else 4, len(eligible_opps))
            log.info(f"  ⚡ Riset paralel {len(eligible_opps)} aset (workers={max_w}, mode={research_mode})...")

            def _run_one_asset(item: dict) -> Research:
                return research_stock(
                    item["asset"],
                    qwen=qwen,
                    gemini=gemini,
                    funding_daily=item.get("funding_daily", float(item.get("funding_daily_pct", 0.0) or 0.0) / 100.0),
                    recommended_side=item.get("recommended_side") or item.get("side"),
                    opportunity_count=opp_count,
                    is_idle=scan_is_idle,
                )

            with ThreadPoolExecutor(max_workers=max_w) as pool:
                fut_map = {pool.submit(_run_one_asset, item): item["asset"] for item in eligible_opps}
                for fut in as_completed(fut_map):
                    asset = fut_map[fut]
                    try:
                        res_obj = fut.result()
                        researches[asset] = res_obj
                        record_research_health(asset, res_obj)
                        log.info(
                            f"  ✓ research {asset}: {res_obj.news_sentiment} "
                            f"(Consensus: {res_obj.consensus}, Mode: {res_obj.secondary_provider})"
                        )
                    except Exception as e:
                        record_input_failure("input.riset_error", f"{asset}:{type(e).__name__}")
                        log.warning(f"  ✗ research {asset} failed: {e}")

    # Eksekusi instan jika auto_execute aktif
    executions = []
    deployed = 0.0
    skipped_timeout: list[str] = []
    accounts = summary.get("balance", [])
    available = float(accounts[0]["balance_available"]) if accounts else 0.0
    target_deploy = available * (1.0 - MIN_BUFFER_PCT)  # 15% cash buffer dijaga

    if auto_execute:
        existing = {"positions": []}
        try:
            existing = client.list_positions(statuses=["open"])
            existing_contracts = {
                p["contract_id"]
                for p in existing.get("positions", [])
                if str(p.get("status", "open")).lower() in ("open", "active")
            }
        except Exception as e:
            log.warning(f"Gagal fetch existing positions: {e}")
            existing_contracts = set()

        total_equity = get_total_equity(accounts, existing)
        active_slot_count = len(existing_contracts)
        max_slots = get_dynamic_max_slots(total_equity)
        log.info(
            f"Dynamic Elastic Slot & Conviction Sizing: Equity ${total_equity:.2f} (Available ${available:.2f}) | "
            f"Posisi Aktif {active_slot_count}/{max_slots} | Max Allowed Slots: {max_slots}"
        )

        cb_tripped, cb_reason = check_daily_circuit_breaker(total_equity)
        if cb_tripped:
            log.warning(f"🚨 [CIRCUIT BREAKER] {cb_reason}")
            raw_opps = []
        else:
            raw_opps = summary.get("_raw_opportunities", [])

        # R6-2: start dingin (cache Actions miss) TANPA memori risiko => lewati entry siklus ini.
        cold_allowed, cold_reason = check_cold_state_entry_gate()
        if not cold_allowed:
            _notify_cold_state_once(cold_reason)
            raw_opps = []
        for opp_idx, opp in enumerate(raw_opps):
            if active_slot_count >= max_slots:
                log.info(f"  Slot maksimal tercapai ({active_slot_count}/{max_slots}), hentikan alokasi.")
                break
            if deployed >= target_deploy or (available - deployed) < 3.0:
                log.info(f"  Buffer saldo tercapai (${deployed:.2f}/${target_deploy:.2f}), hentikan alokasi.")
                break
            asset = opp.base_asset
            if asset in ALLOWED_STOCKS and not us_stocks_open:
                log.info(f"  SKIP {asset}: {us_stocks_note}")
                executions.append({
                    "asset": asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "SKIP",
                    "reason": us_stocks_note,
                })
                continue
            is_forecast = getattr(opp, "signal_source", "funding") == "price_forecast"
            if (asset in RESEARCH_ELIGIBLE or is_forecast) and asset not in researches and (qwen or gemini):
                if not has_enough_job_time(2.5):
                    log.warning(
                        f"⏳ Sisa waktu job tinggal {get_job_remaining_minutes():.1f} menit; "
                        f"riset OTF {asset} dilewati."
                    )
                else:
                    try:
                        researches[asset] = research_stock(
                            asset,
                            qwen=qwen,
                            gemini=gemini,
                            funding_daily=opp.funding_daily,
                            recommended_side=opp.side,
                            opportunity_count=opp_count,
                            is_idle=scan_is_idle,
                            **({"grav_pressure": _gp_scan} if (_gp_scan := getattr(opp, "grav_bull_pressure", None)) is not None else {}),
                        )
                        record_research_health(asset, researches[asset])
                    except Exception as e:
                        record_input_failure("input.riset_error", f"{asset}:{type(e).__name__}")
                        log.warning(f"  ✗ research {asset} failed: {e}")
            research = researches.get(asset)
            verdict, reason, mult = decide_go_nogo(opp, research)

            if verdict in ("GO", "HALF") and mult > 0:
                if opp.contract_id in existing_contracts:
                    log.info(f"  SKIP {asset}: posisi sudah aktif")
                    executions.append({
                        "asset": asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "SKIP",
                        "reason": "posisi sudah aktif",
                    })
                    continue

                in_cd, cd_min = _is_asset_in_cooldown(asset)
                if in_cd:
                    log.info(f"  SKIP {asset}: cooldown {cd_min:.0f}m pasca penutupan")
                    executions.append({
                        "asset": asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "SKIP",
                        "reason": f"cooldown {cd_min:.0f}m pasca penutupan (anti-whipsaw)",
                    })
                    continue

                freq_ok, freq_reason = check_entry_frequency_limits(asset, verdict=verdict)
                if not freq_ok:
                    log.info(f"  SKIP {asset}: {freq_reason}")
                    executions.append({
                        "asset": asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "SKIP",
                        "reason": freq_reason,
                    })
                    continue

                if getattr(opp, "signal_source", "funding") == "price_forecast":
                    net_edge_fc = float(getattr(opp, "expected_net_edge_pct", 0.0) or 0.0)
                    if net_edge_fc < 0.0015:
                        skip_msg = f"gerbang ekonomi Engine 2 gagal (net_edge={net_edge_fc*100:+.2f}% < +0.15%)"
                        log.info(f"  SKIP {asset}: {skip_msg}")
                        executions.append({
                            "asset": asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "SKIP",
                            "reason": skip_msg,
                        })
                        continue
                    reason = f"{reason} | {opp.forecast_setup} net_edge={net_edge_fc*100:+.2f}%"
                else:
                    econ_ok, _exp_fr, _cost, econ_note = check_funding_vs_fee_gate(
                        opp.funding_daily, effective_price_slippage(opp.price_slippage))
                    if not econ_ok:
                        skip_msg = f"gerbang ekonomi funding vs fee gagal ({econ_note})"
                        log.info(f"  SKIP {asset}: {skip_msg}")
                        executions.append({
                            "asset": asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "SKIP",
                            "reason": skip_msg,
                        })
                        continue
                    reason = f"{reason} | {econ_note}"

                margin = calculate_dynamic_margin(
                    available_balance=available,
                    active_slots=active_slot_count,
                    max_slots=max_slots,
                    opp=opp,
                    research=research,
                    verdict_mult=mult,
                    total_equity=total_equity,
                )
                if margin < DEFAULT_MARGIN:
                    log.info(f"  SKIP {asset}: HALF di bawah floor exchange")
                    executions.append({
                        "asset": asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "SKIP",
                        "reason": "HALF di bawah floor exchange",
                    })
                    continue

                resolved = resolve_order_margin(client, opp, margin=margin, leverage=MAX_LEVERAGE)
                if resolved is None:
                    executions.append({
                        "asset": asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "FAILED",
                        "reason": "risk / min size check gagal",
                    })
                    continue
                margin, resolved_price = resolved
                setattr(opp, "live_price", resolved_price)

                if deployed + margin > target_deploy or (available - deployed) < 3.0:
                    log.info(f"  SKIP {asset}: buffer saldo tercapai (${deployed:.2f}/${target_deploy:.2f})")
                    executions.append({
                        "asset": asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "SKIP",
                        "reason": f"buffer saldo tercapai (${deployed:.2f}/${target_deploy:.2f})",
                    })
                    break

                if dry_run:
                    log.info(f"  🧪 DRY_RUN {opp.side.upper()} {asset} margin ${margin:.2f}")
                    executions.append({
                        "asset": asset, "side": opp.side, "leverage": MAX_LEVERAGE, "margin": margin,
                        "price": getattr(opp, "live_price", 0.0),
                        "verdict": "DRY_RUN", "reason": reason,
                    })
                    deployed += margin
                    existing_contracts.add(opp.contract_id)
                    active_slot_count += 1
                    continue

                if not has_enough_job_time():
                    rem_min = get_job_remaining_minutes()
                    remaining_assets = [o.base_asset for o in raw_opps[opp_idx:]]
                    skipped_timeout.extend(remaining_assets)
                    log.warning(
                        f"⏳ Sisa waktu job tinggal {rem_min:.1f} menit, "
                        f"{len(remaining_assets)} kandidat dilewati ({', '.join(remaining_assets)})"
                    )
                    send_telegram(
                        f"⏳ Sisa waktu job tinggal {rem_min:.1f} menit, "
                        f"{len(remaining_assets)} kandidat dilewati"
                    )
                    break

                try:
                    res = open_position(
                        client,
                        opp,
                        margin=margin,
                        leverage=MAX_LEVERAGE,
                        resolved=(margin, resolved_price),
                    )
                    if res:
                        actual_margin = res.get("actual_margin", margin) if isinstance(res, dict) else margin
                        order_id = res.get("order", {}).get("order_id") if isinstance(res, dict) else None
                        order_price = res.get("price") or getattr(opp, "live_price", 0.0)
                        tp = res.get("tp")
                        sl = res.get("sl")
                        exec_entry = {
                            "asset": asset, "side": opp.side, "leverage": MAX_LEVERAGE, "margin": actual_margin,
                            "price": order_price, "tp": tp, "sl": sl,
                            "verdict": "EXECUTED", "reason": reason,
                            "order_id": order_id,
                        }
                        if deployed + actual_margin > target_deploy:
                            log.warning(
                                f"⚠️ Buffer kas terlampaui pada {asset}: "
                                f"${deployed + actual_margin:.2f} > target ${target_deploy:.2f}"
                            )
                            send_telegram(
                                f"⚠️ Buffer kas terlampaui: {asset} "
                                f"(${deployed + actual_margin:.2f} > ${target_deploy:.2f})"
                            )
                            exec_entry["over_buffer"] = True
                        executions.append(exec_entry)
                        _record_open_position_verdict(
                            opp.contract_id, asset, opp.side, verdict, actual_margin, order_price
                        )
                        res_sl_pct = res.get("sl_pct") if isinstance(res, dict) else None
                        res_tp_pct = res.get("tp_pct") if isinstance(res, dict) else None
                        if getattr(opp, "signal_source", "funding") == "price_forecast" or (
                            res_sl_pct is not None and float(res_sl_pct) > DEFAULT_SL_PCT + 1e-6
                        ):
                            _record_forecast_position_meta(
                                opp.contract_id, opp, sl_pct=res_sl_pct, tp_pct=res_tp_pct
                            )
                        record_entry_execution(asset)
                        deployed += actual_margin
                        existing_contracts.add(opp.contract_id)
                        active_slot_count += 1
                    else:
                        executions.append({
                            "asset": asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "FAILED",
                            "reason": "risk / min size check gagal",
                        })
                except Exception as e:
                    log.error(f"  ERROR {asset}: {e}")
                    executions.append({
                        "asset": asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "ERROR", "reason": str(e),
                    })
            else:
                executions.append({
                    "asset": asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "SKIP", "reason": reason,
                })

    # Logging eksekusi ke Google Sheet & CSV jika ada yang dieksekusi
    executed_items = [e for e in executions if e.get("verdict") == "EXECUTED"]
    if executed_items:
        try:
            from sheet_logger import log_execution
            exec_res = log_execution(executions)
            log.info(f"📊 Logging executions result: {exec_res}")
        except Exception as e:
            log.warning(f"Logging execution to sheet failed: {e}")

    # Format & send Telegram
    msg = format_summary_telegram(summary, researches, executions=executions)
    sent = send_telegram(msg)
    log.info(f"Telegram sent: {sent}")

    # Logging scan & PnL_Tracker harian ke Google Sheet / CSV
    try:
        from sheet_logger import log_scan, log_pnl_tracker
        log_res = log_scan(summary, researches)
        log.info(f"📊 Logging scan result: {log_res}")
        pnl_res = log_pnl_tracker(summary)
        log.info(f"📈 Logging PnL_Tracker result: {pnl_res}")
    except Exception as e:
        log.warning(f"Logging scan/PnL to sheet failed: {e}")

    # Logging otomatis sampel riset ke tab 'Data Training' & logs/training_dataset.jsonl (non-blocking)
    try:
        from training_data import build_training_sample, log_training_samples
        from types import SimpleNamespace

        ts_scan = summary.get("ts") or datetime.now(timezone.utc).isoformat()
        train_samples = []
        for opp in summary.get("top_opportunities", []):
            asset = opp["asset"]
            res_obj = researches.get(asset)
            if not res_obj:
                continue
            funding_dec = float(opp.get("funding_daily", opp.get("funding_daily_pct", 0.0) / 100.0))
            pseudo = SimpleNamespace(
                base_asset=asset,
                side=opp["side"],
                funding_daily=funding_dec,
                signal_source=opp.get("signal_source", "funding"),
                forecast_setup=opp.get("forecast_setup", ""),
                expected_net_edge_pct=float(opp.get("expected_net_edge_pct") or 0.0),
                forecast_reason=opp.get("forecast_reason", ""),
                planned_sl_pct=float(opp.get("planned_sl_pct") or 0.0),
                planned_tp_pct=float(opp.get("planned_tp_pct") or 0.0),
                max_hold_hours=float(opp.get("max_hold_hours") or 0.0),
                grav_bull_pressure=opp.get("grav_bull_pressure"),
            )
            v_str, r_str, _ = decide_go_nogo(pseudo, res_obj)
            sample = build_training_sample(
                timestamp_utc=ts_scan,
                asset=asset,
                side=opp["side"],
                funding_daily=funding_dec,
                price=opp.get("price"),
                verdict=v_str,
                decision_reason=r_str,
                research=res_obj,
            )
            if sample:
                train_samples.append(sample)
        if train_samples:
            tr_res = log_training_samples(train_samples)
            log.info(f"🎓 Logging Data Training result: {tr_res}")
    except Exception as e:
        log.warning(f"Logging Data Training failed (non-blocking): {e}")

    result_payload = {
        "summary": summary,
        "researches": {k: v.__dict__ for k, v in researches.items()},
        "executions": executions,
        "deployed": deployed,
    }
    if skipped_timeout:
        result_payload["status"] = "partial_timeout"
        result_payload["skipped"] = skipped_timeout
    return result_payload


# =============================================================================
# MARKET EXECUTE — buka posisi saham saat US market open
# =============================================================================

def market_execute(
    client: WaltClient,
    dry_run: bool = False,
    universe: Optional[set[str]] = None,
    title: str = "🚀 Walt Market Execute",
    now_utc: Optional[datetime] = None,
) -> dict:
    """Eksekusi posisi baru dengan Dynamic TP/SL.
    
    Flow:
      1. Check balance
      2. Get ranked opportunities (sesuai universe)
      3. Research stocks → decide GO/HALF/SKIP
      4. Execute top 2 (atau sesuai saldo)
      5. Report via Telegram
    """
    target_universe = universe or (ALLOWED_STOCKS | ALLOWED_COMMODITIES)
    us_stocks_open, us_stocks_note = is_us_stock_market_open_utc(now_utc=now_utc)
    if not us_stocks_open and (target_universe & ALLOWED_STOCKS):
        log.info(
            f"🕒 [US STOCK HOURS] {us_stocks_note} — Saham AS dilewati; Komoditas/Crypto tetap 24/7."
        )
    log.info(f"{title} (Target Universe: {len(target_universe)} aset)...")
    balance_data = client.balance()
    accounts = balance_data.get("accounts", [])
    if not accounts:
        send_telegram(f"⚠️ *{title} SKIP* — saldo kosong, deposit USDC dulu.")
        return {"status": "no_balance"}

    available = float(accounts[0]["balance_available"])
    log.info(f"Balance available: ${available}")
    if available < 3.0:
        send_telegram(f"⚠️ *{title} SKIP* — saldo terlalu kecil (${available:.2f} < $3).")
        return {"status": "insufficient_balance", "balance": available}

    # Scan + rank
    contracts_data = client.list_contracts()
    log.info(f"🔎 [COVERAGE] {len(contracts_data.get('contracts') or [])} kontrak Walt dimuat untuk eksekusi")
    opps = rank_opportunities(contracts_data["contracts"], allowed_assets=target_universe)

    category_key: Literal["market", "crypto"] = "crypto" if target_universe == ALLOWED_CRYPTO else "market"

    # Engine 2 (Crypto 24/7 Mean-Reversion Fade + Saham AS LONG-ONLY Dip-Buy saat bursa AS buka)
    if category_key == "crypto":
        fc_opps = _scan_crypto_engine2_opportunities(
            contracts_data["contracts"],
            target_universe,
            exclude_assets={o.base_asset for o in opps},
            log_label="ENGINE 2 CRYPTO",
        )
        if fc_opps:
            opps.extend(fc_opps)
    elif category_key == "market" and us_stocks_open and (target_universe & ALLOWED_STOCKS):
        fc_stock_opps = _scan_stock_engine2_opportunities(
            contracts_data["contracts"],
            target_universe & ALLOWED_STOCKS,
            exclude_assets={o.base_asset for o in opps},
            log_label="ENGINE 2 STOCK",
        )
        if fc_stock_opps:
            opps.extend(fc_stock_opps)

    gear_label, gear_interval_min, gear_reason = _update_regime_state(
        category_key, opps, mark_executed=True
    )
    log.info(f"⚙️ [ADAPTIVE GEAR - {category_key.upper()}] {gear_label} ({gear_interval_min}m): {gear_reason}")

    # GEAR 0 Idle Cap:
    # - market: maks 2x selama jam operasional Saham AS (13:30-21:00 UTC)
    # - crypto: maks 2x sehari (tiap 12 jam)
    if gear_label.startswith("GEAR_0"):
        allow_g0, g0_elapsed_min = _should_run_gear0_cycle(category_key, now_utc=now_utc)
        if not allow_g0:
            log.info(
                f"💤 [GEAR 0 IDLE] {gear_reason} — batas 2x siklus Gear 0 sudah terpenuhi "
                f"(atau di luar jam operasional Saham AS). Fast-exit tanpa spam."
            )
            return {
                "status": "gear_0_idle",
                "gear": gear_label,
                "interval_minutes": gear_interval_min,
                "executions": [],
                "deployed": 0.0,
                "available": available,
            }

    # Verifikasi posisi yang benar-benar aktif (statuses=["open"]) secara defensif
    open_contract_ids = set()
    position_check_failed = False
    existing = {"positions": []}
    try:
        existing = client.list_positions(statuses=["open"])
        open_contract_ids = {
            p["contract_id"]
            for p in existing.get("positions", [])
            if str(p.get("status", "open")).lower() in ("open", "active")
        }
        log.info(
            f"Open positions verified: "
            f"{[(p.get('contract_base_asset', p.get('contract_id')), p.get('status', 'open')) for p in existing.get('positions', [])]}"
        )
    except Exception as exc:
        log.warning(f"Pemeriksaan posisi awal gagal: {exc}; lewati pembukaan demi keamanan")
        position_check_failed = True

    # Dynamic Slot Elasticity berbasis Total Ekuitas (available + margin open positions)
    total_equity = get_total_equity(balance_data, existing)
    max_slots = get_dynamic_max_slots(total_equity)
    active_slot_count = len(open_contract_ids)

    # GEAR 0 Smart Idle Gatekeeper: Jika slot sudah penuh, langsung fast-exit tanpa memanggil riset AI!
    if active_slot_count >= max_slots:
        log.info(
            f"💤 [GEAR 0 SMART GATEKEEPER] Slot posisi sudah penuh ({active_slot_count}/{max_slots}). "
            "Fast-exit tanpa riset AI."
        )
        return {
            "status": "slots_full",
            "active_slots": active_slot_count,
            "max_slots": max_slots,
            "gear": gear_label,
            "interval_minutes": gear_interval_min,
            "executions": [],
            "deployed": 0.0,
            "available": available,
        }

    # Research saham: Utamakan hasil penelitian dari Spreadsheet/CSV terlebih dahulu (hemat API & cepat)
    from sheet_logger import (
        fetch_latest_research_from_sheet,
        get_research_read_diagnosis,
        get_research_read_source,
        get_research_read_status,
    )
    cached_researches = fetch_latest_research_from_sheet()
    # Ronde 7: jalur baca riset dicatat di Step Summary. 200 + non-JSON dari Apps Script pernah
    # membuat semua siklus diam-diam memakai CSV lokal tanpa jejak keterlacakan di ringkasan.
    research_read = get_research_read_status()
    set_input_meta("research_read_status", research_read)
    set_input_meta("research_read_source", get_research_read_source() or "-")
    diagnosis = get_research_read_diagnosis()
    if diagnosis:
        set_input_meta("research_read_diagnosis", diagnosis)
    if research_read in ("http_error", "html_or_unparsable", "no_config", "exception", "status_not_success"):
        record_input_failure("input.riset_webhook", research_read)

    import llm_research as _lr

    # Prefetch konteks makro & kalender OAK Research MCP 1x sebelum keputusan trading (non-blocking)
    try:
        import oak_mcp_client

        if oak_mcp_client.is_oak_mcp_configured():
            oak_brief = oak_mcp_client.fetch_oak_global_brief()
            log.info(f"🌳 [OAK MCP - {category_key.upper()}] Global brief connected={bool(oak_brief.get('available'))}")
    except Exception as exc:
        log.warning(f"OAK MCP global prefetch dilewati: {exc}")

    # Evaluasi jumlah peluang 'GO' (dengan cache riset) untuk Gear 3 & kebijakan routing AI (Gemini vs GLM)
    eligible_now = [
        o for o in opps
        if not (o.base_asset in ALLOWED_STOCKS and not us_stocks_open)
    ]
    go_count_now = count_go_opportunities(eligible_now, researches=cached_researches)
    if go_count_now != len([
        o for o in opps
        if abs(o.funding_daily) >= MIN_FUNDING_DAILY or getattr(o, "signal_source", "funding") == "price_forecast"
    ]) or os.getenv("GEAR_3_FULL_RESEARCH", "false").strip().lower() in ("1", "true", "yes", "on"):
        gear_label, gear_interval_min, gear_reason = _update_regime_state(
            category_key, eligible_now, mark_executed=True, go_count=go_count_now
        )
        log.info(f"⚙️ [ADAPTIVE GEAR - {category_key.upper()} REFINED] {gear_label} ({gear_interval_min}m): {gear_reason}")

    full_research_gear3 = is_gear3_full_research(gear_label)
    exec_opp_count = len([
        o for o in eligible_now
        if abs(o.funding_daily) >= MIN_FUNDING_DAILY or getattr(o, "signal_source", "funding") == "price_forecast"
    ])
    exec_is_idle = gear_label.startswith("GEAR_0") or exec_opp_count <= 1
    use_glm_routing = _lr.should_use_glm_for_secondary_voters(
        opportunity_count=exec_opp_count, is_idle=exec_is_idle
    )
    log.info(
        f"🧠 [AI ROUTING - {category_key.upper()}] Secondary={'GLM (DashScope)' if use_glm_routing else 'Gemini (Idle)'} "
        f"(peluang={exec_opp_count}, go={go_count_now}, gear={gear_label})"
    )

    qwen = QwenClient() if os.getenv("DASHSCOPE_API_KEY") else None
    gemini = (
        GeminiClient()
        if (not use_glm_routing or qwen is None)
        and (os.getenv("DASHSCOPE_API_GEMINI_KEY") or os.getenv("GEMINI_API_KEY"))
        else None
    )
    researches: dict[str, Research] = {}
    otf_research_calls = 0
    # Gear 3 memberi izin setiap keputusan trading melakukan penelitian & analisa AI tanpa cap
    max_otf_calls = len(opps) if full_research_gear3 else (max_slots * 2)
    cb_tripped, cb_reason = check_daily_circuit_breaker(total_equity)
    if cb_tripped:
        log.warning(f"🚨 [CIRCUIT BREAKER] {cb_reason}")
        opps = []

    # R6-2: start dingin tanpa memori risiko => lewati entry siklus ini (dan hemat panggilan riset).
    cold_allowed, cold_reason = check_cold_state_entry_gate()
    if not cold_allowed:
        _notify_cold_state_once(cold_reason)
        opps = []

    crypto_research = crypto_research_set([(o.base_asset, o.funding_daily) for o in opps])
    for opp in opps:
        if opp.base_asset in ALLOWED_STOCKS and not us_stocks_open:
            continue
        is_forecast = getattr(opp, "signal_source", "funding") == "price_forecast"
        if opp.base_asset in RESEARCH_ELIGIBLE or is_forecast:
            if opp.base_asset in cached_researches:
                cached = cached_researches[opp.base_asset]
                log.info(f"  ✓ Pakai riset spreadsheet {opp.base_asset}: skor {cached.final_score:+.2f}, sentimen {cached.news_sentiment}")
                researches[opp.base_asset] = cached
            elif (qwen or gemini) and otf_research_calls < max_otf_calls and (
                    full_research_gear3 or opp.base_asset in ALLOWED_STOCKS or opp.base_asset in crypto_research or is_forecast):
                try:
                    otf_research_calls += 1
                    log.info(
                        f"  🔍 Riset on-the-fly ({otf_research_calls}/{max_otf_calls}) untuk {opp.base_asset} "
                        f"({'Gear 3 Full Research' if full_research_gear3 else 'belum ada di spreadsheet'})..."
                    )
                    researches[opp.base_asset] = research_stock(
                        opp.base_asset,
                        qwen=qwen,
                        gemini=gemini,
                        funding_daily=opp.funding_daily,
                        recommended_side=opp.side,
                        opportunity_count=exec_opp_count,
                        is_idle=exec_is_idle,
                        **({"grav_pressure": _gp_exec} if (_gp_exec := getattr(opp, "grav_bull_pressure", None)) is not None else {}),
                    )
                    record_research_health(opp.base_asset, researches[opp.base_asset])
                except Exception as e:
                    record_input_failure("input.riset_error", f"{opp.base_asset}:{type(e).__name__}")
                    log.warning(f"research {opp.base_asset}: {e}")

    # Decide + execute
    executions = []
    deployed = 0.0
    skipped_timeout: list[str] = []
    target_deploy = available * (1.0 - MIN_BUFFER_PCT)  # 15% cash buffer dijaga konsisten dari available

    active_slot_count = len(open_contract_ids)
    log.info(
        f"Dynamic Elastic Slot & Conviction Sizing: Equity ${total_equity:.2f} (Available ${available:.2f}) | "
        f"Posisi Aktif {active_slot_count}/{max_slots} | Max Allowed Slots: {max_slots}"
    )

    for opp_idx, opp in enumerate(opps):
        if opp.base_asset in ALLOWED_STOCKS and not us_stocks_open:
            log.info(f"  SKIP {opp.base_asset}: {us_stocks_note}")
            executions.append({
                "asset": opp.base_asset, "side": opp.side, "leverage": MAX_LEVERAGE,
                "verdict": "SKIP", "reason": us_stocks_note,
            })
            continue
        if active_slot_count >= max_slots:
            log.info(f"  Slot maksimal tercapai ({active_slot_count}/{max_slots}), hentikan pembukaan posisi baru.")
            break
        if deployed >= target_deploy or (available - deployed) < 3.0:
            break
        research = researches.get(opp.base_asset)
        verdict, reason, mult = decide_go_nogo(opp, research)
        if verdict == "SKIP" or mult <= 0:
            log.info(f"  SKIP {opp.base_asset}: {reason}")
            executions.append({
                "asset": opp.base_asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "SKIP", "reason": reason,
            })
            continue

        # Cek apakah sudah ada posisi aktif di kontrak ini (Fail-Safe & Anti-Duplicate)
        if position_check_failed:
            log.warning(f"  SKIP {opp.base_asset}: gagal memverifikasi posisi terbuka")
            executions.append({
                "asset": opp.base_asset,
                "side": opp.side,
                "leverage": MAX_LEVERAGE,
                "verdict": "SKIP",
                "reason": "gagal memverifikasi posisi terbuka",
            })
            continue

        if opp.contract_id in open_contract_ids:
            log.info(f"  SKIP {opp.base_asset}: posisi sudah ada")
            executions.append({
                "asset": opp.base_asset,
                "side": opp.side,
                "leverage": MAX_LEVERAGE,
                "verdict": "SKIP",
                "reason": "posisi sudah aktif",
            })
            continue

        in_cd, cd_min = _is_asset_in_cooldown(opp.base_asset)
        if in_cd:
            log.info(f"  SKIP {opp.base_asset}: cooldown {cd_min:.0f}m pasca penutupan")
            executions.append({
                "asset": opp.base_asset,
                "side": opp.side,
                "leverage": MAX_LEVERAGE,
                "verdict": "SKIP",
                "reason": f"cooldown {cd_min:.0f}m pasca penutupan (anti-whipsaw)",
            })
            continue

        freq_ok, freq_reason = check_entry_frequency_limits(opp.base_asset, verdict=verdict)
        if not freq_ok:
            log.info(f"  SKIP {opp.base_asset}: {freq_reason}")
            executions.append({
                "asset": opp.base_asset,
                "side": opp.side,
                "leverage": MAX_LEVERAGE,
                "verdict": "SKIP",
                "reason": freq_reason,
            })
            continue

        if getattr(opp, "signal_source", "funding") == "price_forecast":
            net_edge_fc = float(getattr(opp, "expected_net_edge_pct", 0.0) or 0.0)
            if net_edge_fc < 0.0015:
                skip_msg = f"gerbang ekonomi Engine 2 gagal (net_edge={net_edge_fc*100:+.2f}% < +0.15%)"
                log.info(f"  SKIP {opp.base_asset}: {skip_msg}")
                executions.append({
                    "asset": opp.base_asset,
                    "side": opp.side,
                    "leverage": MAX_LEVERAGE,
                    "verdict": "SKIP",
                    "reason": skip_msg,
                })
                continue
            reason = f"{reason} | {opp.forecast_setup} net_edge={net_edge_fc*100:+.2f}%"
        else:
            econ_ok, _exp_fr, _cost, econ_note = check_funding_vs_fee_gate(
                opp.funding_daily, effective_price_slippage(opp.price_slippage))
            if not econ_ok:
                skip_msg = f"gerbang ekonomi funding vs fee gagal ({econ_note})"
                log.info(f"  SKIP {opp.base_asset}: {skip_msg}")
                executions.append({
                    "asset": opp.base_asset,
                    "side": opp.side,
                    "leverage": MAX_LEVERAGE,
                    "verdict": "SKIP",
                    "reason": skip_msg,
                })
                continue
            reason = f"{reason} | {econ_note}"

        margin = calculate_dynamic_margin(
            available_balance=available,
            active_slots=active_slot_count,
            max_slots=max_slots,
            opp=opp,
            research=research,
            verdict_mult=mult,
            total_equity=total_equity,
        )
        if margin < DEFAULT_MARGIN:
            log.info(f"  SKIP {opp.base_asset}: HALF di bawah floor exchange")
            executions.append({
                "asset": opp.base_asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "SKIP",
                "reason": "HALF di bawah floor exchange",
            })
            continue

        resolved = resolve_order_margin(client, opp, margin=margin, leverage=MAX_LEVERAGE)
        if resolved is None:
            log.warning(f"  FAILED {opp.base_asset}: min size / risk check gagal")
            executions.append({
                "asset": opp.base_asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "FAILED",
                "reason": f"risk check / min size check gagal (verdict={verdict})",
            })
            continue
        margin, resolved_price = resolved
        setattr(opp, "live_price", resolved_price)

        if deployed + margin > target_deploy or (available - deployed) < 3.0:
            log.info(f"  SKIP {opp.base_asset}: buffer saldo tercapai (${deployed:.2f}/${target_deploy:.2f})")
            break

        log.info(f"  {verdict} {opp.side.upper()} {opp.base_asset} margin ${margin:.2f} (mult {mult:.2f}) — {reason}")
        if dry_run:
            executions.append({
                "asset": opp.base_asset, "side": opp.side, "leverage": MAX_LEVERAGE, "margin": margin,
                "price": getattr(opp, "live_price", 0.0),
                "verdict": "DRY_RUN", "reason": reason,
            })
            deployed += margin
            open_contract_ids.add(opp.contract_id)
            active_slot_count += 1
            continue

        if not has_enough_job_time():
            rem_min = get_job_remaining_minutes()
            remaining_assets = [o.base_asset for o in opps[opp_idx:]]
            skipped_timeout.extend(remaining_assets)
            log.warning(
                f"⏳ Sisa waktu job tinggal {rem_min:.1f} menit, "
                f"{len(remaining_assets)} kandidat dilewati ({', '.join(remaining_assets)})"
            )
            send_telegram(
                f"⏳ Sisa waktu job tinggal {rem_min:.1f} menit, "
                f"{len(remaining_assets)} kandidat dilewati"
            )
            break

        try:
            result = open_position(
                client,
                opp,
                margin=margin,
                leverage=MAX_LEVERAGE,
                resolved=(margin, resolved_price),
            )
            if result:
                open_contract_ids.add(opp.contract_id)
                active_slot_count += 1
                actual_margin = result.get("actual_margin", margin) if isinstance(result, dict) else margin
                order_id = result.get("order", {}).get("order_id") if isinstance(result, dict) else None
                order_price = result.get("price") or getattr(opp, "live_price", 0.0)
                tp = result.get("tp")
                sl = result.get("sl")
                exec_entry = {
                    "asset": opp.base_asset, "side": opp.side, "leverage": MAX_LEVERAGE, "margin": actual_margin,
                    "price": order_price, "tp": tp, "sl": sl,
                    "verdict": "EXECUTED", "reason": reason,
                    "order_id": order_id,
                }
                if deployed + actual_margin > target_deploy:
                    log.warning(
                        f"⚠️ Buffer kas terlampaui pada {opp.base_asset}: "
                        f"${deployed + actual_margin:.2f} > target ${target_deploy:.2f}"
                    )
                    send_telegram(
                        f"⚠️ Buffer kas terlampaui: {opp.base_asset} "
                        f"(${deployed + actual_margin:.2f} > ${target_deploy:.2f})"
                    )
                    exec_entry["over_buffer"] = True
                executions.append(exec_entry)
                _record_open_position_verdict(
                    opp.contract_id, opp.base_asset, opp.side, verdict, actual_margin, order_price
                )
                res_sl_pct = result.get("sl_pct") if isinstance(result, dict) else None
                res_tp_pct = result.get("tp_pct") if isinstance(result, dict) else None
                if getattr(opp, "signal_source", "funding") == "price_forecast" or (
                    res_sl_pct is not None and float(res_sl_pct) > DEFAULT_SL_PCT + 1e-6
                ):
                    _record_forecast_position_meta(
                        opp.contract_id, opp, sl_pct=res_sl_pct, tp_pct=res_tp_pct
                    )
                record_entry_execution(opp.base_asset)
                deployed += actual_margin
            else:
                log.warning(f"  FAILED {opp.base_asset}: preview / risk check gagal")
                executions.append({
                    "asset": opp.base_asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "FAILED",
                    "reason": f"risk check / min size check gagal (verdict={verdict})",
                })
        except Exception as e:
            log.error(f"  ERROR {opp.base_asset}: {e}")
            executions.append({
                "asset": opp.base_asset, "side": opp.side, "leverage": MAX_LEVERAGE, "verdict": "ERROR", "reason": str(e),
            })

    # Report
    executed_list = [e for e in executions if e["verdict"] == "EXECUTED"]
    skipped_list = [e for e in executions if e["verdict"] in ("SKIP", "FAILED", "ERROR")]

    msg_lines = [
        f"*{title}*",
        f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC_\n",
        f"Saldo: `${available:.2f}` | Deploy: `${deployed:.2f}` | Slot: *{active_slot_count}/{max_slots}*",
        f"Executed: *{len(executed_list)}* | Skipped: *{len(skipped_list)}*\n",
    ]
    if executed_list:
        msg_lines.append("*✅ Opened Positions:*")
        for e in executed_list:
            msg_lines.append(f"• {e['side'].upper()} {e['asset']} — margin `${e['margin']:.2f}`")
            msg_lines.append(f"   {md_italic(e['reason'])}")
    if skipped_list:
        msg_lines.append("\n*❌ Skipped:*")
        for e in skipped_list[:5]:
            msg_lines.append(f"• {e['asset']}: {md_escape(e['reason'])}")

    send_telegram("\n".join(msg_lines))

    # Logging eksekusi ke Google Sheet / CSV
    try:
        from sheet_logger import log_execution
        log_res = log_execution(executions)
        log.info(f"📊 Logging executions result: {log_res}")
    except Exception as e:
        log.warning(f"Logging execution to sheet failed: {e}")

    # Logging otomatis riset on-the-fly baru (yang tidak berasal dari cache) ke tab 'Data Training'
    try:
        from training_data import build_training_sample, log_training_samples

        ts_exec = datetime.now(timezone.utc).isoformat()
        fresh_samples = []
        for opp in opps:
            if opp.base_asset in cached_researches:
                continue
            res_obj = researches.get(opp.base_asset)
            if not res_obj:
                continue
            v_str, r_str, _ = decide_go_nogo(opp, res_obj)
            sample = build_training_sample(
                timestamp_utc=ts_exec,
                asset=opp.base_asset,
                side=opp.side,
                funding_daily=opp.funding_daily,
                price=None,
                verdict=v_str,
                decision_reason=r_str,
                research=res_obj,
            )
            if sample:
                fresh_samples.append(sample)
        if fresh_samples:
            tr_res = log_training_samples(fresh_samples)
            log.info(f"🎓 Logging Data Training (on-the-fly) result: {tr_res}")
    except Exception as e:
        log.warning(f"Logging Data Training (on-the-fly) failed (non-blocking): {e}")

    result_payload = {
        "gear": gear_label,
        "interval_minutes": gear_interval_min,
        "executions": executions,
        "deployed": deployed,
        "available": available,
    }
    if skipped_timeout:
        result_payload["status"] = "partial_timeout"
        result_payload["skipped"] = skipped_timeout
    return result_payload


def crypto_execute(client: WaltClient, dry_run: bool = False) -> dict:
    """Eksekusi posisi crypto bluechip (BTC, ETH, SOL) dengan Dynamic TP/SL."""
    return market_execute(
        client=client,
        dry_run=dry_run,
        universe=ALLOWED_CRYPTO,
        title="⚡ Walt Crypto Execute (Bluechip)",
    )


def compute_closed_stats(
    csv_path: str = "logs/closed_history.csv",
    pnl_tracker_path: str = "logs/pnl_tracker.csv",
    send_tg: bool = True,
) -> dict:
    """
    Baca logs/closed_history.csv (atau sheet Closed_Positions via fetch CSV) dan hitung:
    - dedupe dengan kunci D2 (Position ID atau Asset+ContractID+EntryPrice);
    - kecualikan baris UNKNOWN dari win-rate (tampilkan terpisah di unknown_count);
    - pembanding perubahan ekuitas riil dari PnL_Tracker;
    - estimasi fee (0.09%/sisi dari TAKER_FEE_PER_SIDE × 2 × notional), total funding, net PnL, breakdown alasan close.
    """
    from sheet_logger import fetch_sheet_csv_rows, resolve_closed_positions_columns
    import sheet_logger as _sheet_logger

    rows = fetch_sheet_csv_rows(
        "Closed_Positions",
        expected_col_name="Exit Price ($)",
        local_csv_fallback=csv_path,
    )
    # R6-1: bedakan "sheet tidak terbaca" dari "sheet terbaca tapi kosong".
    sheet_status = _sheet_logger.get_ledger_read_status("Closed_Positions")
    sheet_detail = _sheet_logger.get_ledger_read_detail("Closed_Positions")
    ledger_unreadable = (not rows) and sheet_status != "ok"
    if ledger_unreadable:
        banner = (
            f"LEDGER TIDAK TERTBACA ({sheet_status}) — angka 0 di bawah BUKAN berarti tidak ada trade"
        )
        log.warning(f"{banner} | detail: {sheet_detail}")  # teksnya juga dicetak ke stdout via quality_line
    col_map, data_rows, used_fallback = resolve_closed_positions_columns(rows)
    if rows and used_fallback:
        log.warning(
            "compute_closed_stats: header Closed_Positions rusak/kolom inti hilang, memakai fallback indeks baku"
        )

    idx_ts = col_map.get("timestamp (utc)", 0)
    idx_asset = col_map.get("asset", 1)
    idx_side = col_map.get("sisi", 2)
    idx_cid = col_map.get("contract id", 3)
    idx_entry = col_map.get("entry price ($)", 4)
    idx_exit = col_map.get("exit price ($)", 5)
    idx_margin = col_map.get("margin ($)", 6)
    idx_pnl = col_map.get("pnl ($)", 7)
    idx_funding = col_map.get("funding ($)", 8)
    idx_reason = col_map.get("alasan close", 9)
    idx_pid = col_map.get("position id", 10)
    idx_closed_at = col_map.get("closed at (exchange)", 11)
    idx_opened_at = col_map.get("opened at (exchange)", 13)
    idx_exit_src = col_map.get("exit price source", 14)

    def _cell(row: list[str], idx: int) -> str:
        return str(row[idx]).strip() if 0 <= idx < len(row) else ""

    trades = []
    unknown_count = 0
    unmeasured_backfill_count = 0
    disputed_zero_count = 0
    disputed_reasons: dict[str, int] = {}
    seen_keys: set[str] = set()
    trade_parse_failures: list[str] = []
    reason_breakdown: dict[str, int] = {
        "TP": 0,
        "SL": 0,
        "trailing": 0,
        "funding flip": 0,
        "migrasi": 0,
        "UNKNOWN": 0,
    }

    for r in data_rows:
        if len(r) < 10:
            continue
        ts_cell = _cell(r, idx_ts)
        asset_cell = _cell(r, idx_asset)
        if ts_cell.lower() == "timestamp (utc)" or asset_cell.upper() == "ASSET":
            continue

        pos_id = _cell(r, idx_pid)
        dedupe_key = pos_id if pos_id else f"{asset_cell.upper()}|{_cell(r, idx_cid)}|{_cell(r, idx_entry)}"
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        reason_raw = _cell(r, idx_reason)
        reason_low = reason_raw.lower()
        raw_exit = _cell(r, idx_exit)
        raw_pnl = _cell(r, idx_pnl)

        is_unknown = ("unknown" in reason_low) or (raw_exit == "" and raw_pnl == "")
        if is_unknown:
            unknown_count += 1
            reason_breakdown["UNKNOWN"] = reason_breakdown.get("UNKNOWN", 0) + 1
            # R6-1: baris hasil backfill riwayat bursa sengaja tanpa exit/PnL (endpoint list
            # positions(statuses=["closed"]) tidak menyediakan harga keluar). Ini "belum terukur",
            # BUKAN kegagalan parsing dan BUKAN trade rugi.
            if _cell(r, idx_exit_src) == EXCHANGE_HISTORY_EXIT_SOURCE:
                unmeasured_backfill_count += 1
            continue

        # R6-6: angka nol yang menyamar sebagai data. Baris warisan pra-Ronde 5 menulis
        # Exit Price = Entry Price (fallback harga masuk) dengan PnL 0 dan tanpa kolom sumber
        # harga. Ia selama ini dihitung sebagai LOSS, padahal tidak ada harga keluar yang diketahui.
        # Breakeven RIIL (sumber exchange_fill/response_position) tetap dihitung normal.
        src_lbl = _cell(r, idx_exit_src).strip().lower()
        try:
            entry_cell_val = float(_cell(r, idx_entry) or 0.0)
            exit_cell_val = float(raw_exit or 0.0)
            pnl_cell_val = float(raw_pnl or 0.0)
        except ValueError:
            entry_cell_val = exit_cell_val = pnl_cell_val = None
        if (
            entry_cell_val is not None
            and exit_cell_val is not None
            and entry_cell_val > 0
            and abs(exit_cell_val - entry_cell_val) <= 1e-9
            and abs(pnl_cell_val) <= 1e-9
            and src_lbl in ("", "entry_fallback")
        ):
            disputed_zero_count += 1
            disputed_reasons[reason_raw or "(tanpa alasan)"] = (
                disputed_reasons.get(reason_raw or "(tanpa alasan)", 0) + 1
            )
            continue

        try:
            margin = float(_cell(r, idx_margin) or 0.0)
            pnl_usd = float(raw_pnl or 0.0)
            funding_usd = float(_cell(r, idx_funding) or 0.0)
        except Exception as exc:
            asset_lbl = asset_cell or "?"
            log.debug(
                f"Gagal parse angka trade {asset_lbl} pada Closed_Positions ({type(exc).__name__}): {exc}"
            )
            trade_parse_failures.append(str(asset_lbl))
            unknown_count += 1
            reason_breakdown["UNKNOWN"] = reason_breakdown.get("UNKNOWN", 0) + 1
            continue

        if "migrasi" in reason_low:
            cat = "migrasi"
        elif "funding flip" in reason_low:
            cat = "funding flip"
        elif "trailing" in reason_low or "lock" in reason_low:
            cat = "trailing"
        elif "tp" in reason_low:
            cat = "TP"
        elif "sl" in reason_low:
            cat = "SL"
        else:
            cat = reason_raw or "other"
            reason_breakdown.setdefault(cat, 0)

        reason_breakdown[cat] = reason_breakdown.get(cat, 0) + 1
        pnl_pct = (pnl_usd / margin * 100.0) if margin > 0 else 0.0
        notional = margin * MAX_LEVERAGE
        fee_usd = 2.0 * TAKER_FEE_PER_SIDE * notional  # round-trip: open + close

        trades.append({
            "asset": asset_cell,
            "side": _cell(r, idx_side),
            "margin": margin,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
            "funding_usd": funding_usd,
            "fee_usd": fee_usd,
            "reason": reason_raw,
            "category": cat,
            "position_id": pos_id,
            "closed_at": _cell(r, idx_closed_at),
            "opened_at": _cell(r, idx_opened_at),
            "timestamp_utc": ts_cell,
            "exit_source": _cell(r, idx_exit_src),
        })

    _log_failure_summary("compute_closed_stats.trade_parse", trade_parse_failures, total=len(data_rows))

    # E5: Hitung median hold time dari selisih (closed_at - opened_at)
    import statistics as _statistics
    hold_hours_samples: list[float] = []
    hold_parse_failures: list[str] = []
    for t in trades:
        o_raw = t.get("opened_at") or ""
        c_raw = t.get("closed_at") or t.get("timestamp_utc") or ""
        if not o_raw or not c_raw:
            continue
        try:
            o_dt = datetime.fromisoformat(o_raw.replace("Z", "+00:00"))
            c_dt = datetime.fromisoformat(c_raw.replace("Z", "+00:00"))
            if o_dt.tzinfo is None:
                o_dt = o_dt.replace(tzinfo=timezone.utc)
            if c_dt.tzinfo is None:
                c_dt = c_dt.replace(tzinfo=timezone.utc)
            diff_h = (c_dt - o_dt).total_seconds() / 3600.0
            if diff_h >= 0:
                hold_hours_samples.append(diff_h)
        except Exception as exc:
            log.debug(f"Gagal parse durasi hold Closed_Positions ({type(exc).__name__}): {exc}")
            hold_parse_failures.append(str(t.get("asset", "?")))
            continue
    _log_failure_summary("compute_closed_stats.hold_time", hold_parse_failures, total=len(trades))
    median_hold_hours: Optional[float] = (
        round(float(_statistics.median(hold_hours_samples)), 2) if hold_hours_samples else None
    )
    hold_time_warning: Optional[str] = None
    if median_hold_hours is not None and median_hold_hours < (EXPECTED_HOLD_HOURS * 0.5):
        hold_time_warning = (
            f"⚠️ Median hold ({median_hold_hours:.2f} jam) jauh di bawah target "
            f"expected_hold_hours ({EXPECTED_HOLD_HOURS:.0f} jam) — strategi aktual "
            "cenderung scalping arah harga, bukan funding carry."
        )

    # Pembanding perubahan ekuitas dari PnL_Tracker
    equity_start: Optional[float] = None
    equity_latest: Optional[float] = None
    equity_change: Optional[float] = None
    pnl_rows = fetch_sheet_csv_rows("PnL_Tracker", local_csv_fallback=pnl_tracker_path)
    eq_vals: list[float] = []
    equity_parse_failures: list[str] = []
    for pr in pnl_rows:
        if len(pr) < 2:
            continue
        if str(pr[0]).strip().lower() in ("tanggal", "date"):
            continue
        try:
            eq_vals.append(float(str(pr[1]).replace("$", "").strip()))
        except Exception as exc:
            log.debug(f"Gagal parse ekuitas PnL_Tracker tanggal {pr[0]} ({type(exc).__name__}): {exc}")
            equity_parse_failures.append(str(pr[0]))
            continue
    _log_failure_summary("compute_closed_stats.pnl_tracker_equity", equity_parse_failures, total=len(pnl_rows))
    if eq_vals:
        equity_start = round(eq_vals[0], 4)
        equity_latest = round(eq_vals[-1], 4)
        equity_change = round(equity_latest - equity_start, 4)

    total_trades = len(trades)
    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]

    win_rate_pct = (len(wins) / total_trades * 100.0) if total_trades > 0 else 0.0
    avg_win_usd = (sum(t["pnl_usd"] for t in wins) / len(wins)) if wins else 0.0
    avg_win_pct = (sum(t["pnl_pct"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss_usd = (sum(t["pnl_usd"] for t in losses) / len(losses)) if losses else 0.0
    avg_loss_pct = (sum(t["pnl_pct"] for t in losses) / len(losses)) if losses else 0.0

    gross_pnl_usd = sum(t["pnl_usd"] for t in trades)
    total_funding_usd = sum(t["funding_usd"] for t in trades)
    estimated_fee_usd = sum(t["fee_usd"] for t in trades)
    net_pnl_usd = gross_pnl_usd + total_funding_usd - estimated_fee_usd

    from strategy_config import get_strategy_version_hash
    stats = {
        "total_trades": total_trades,
        "unknown_count": unknown_count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(win_rate_pct, 2),
        "avg_win_usd": round(avg_win_usd, 4),
        "avg_win_pct": round(avg_win_pct, 2),
        "avg_loss_usd": round(avg_loss_usd, 4),
        "avg_loss_pct": round(avg_loss_pct, 2),
        "gross_pnl_usd": round(gross_pnl_usd, 4),
        "total_funding_usd": round(total_funding_usd, 4),
        "estimated_fee_usd": round(estimated_fee_usd, 4),
        "net_pnl_usd": round(net_pnl_usd, 4),
        "equity_start_usd": equity_start,
        "equity_latest_usd": equity_latest,
        "equity_change_usd": equity_change,
        "median_hold_hours": median_hold_hours,
        "expected_hold_hours": EXPECTED_HOLD_HOURS,
        "hold_time_warning": hold_time_warning,
        "strategy_version": get_strategy_version_hash(),
        "breakdown_by_reason": reason_breakdown,
        # R6-1: kejujuran sumber data. "0 trade" dari sheet yang tidak terbaca bukan performa nol.
        "ledger_source": {
            "sheet_status": sheet_status,
            "sheet_detail": sheet_detail,
            "spreadsheet_configured": bool(_sheet_logger.get_spreadsheet_id()),
            "rows_from": "none" if not rows else (sheet_status if sheet_status in ("ok", "local_csv") else "unknown"),
            "unreadable": ledger_unreadable,
        },
        "pnl_terukur": total_trades,
        "belum_terukur_pnl": unmeasured_backfill_count,
        # R6-6: baris PnL=0 tanpa sumber harga (Exit=Entry warisan) tidak boleh dihitung kalah.
        "pnl_diragukan": disputed_zero_count,
        "diragukan_alasan": disputed_reasons,
    }

    if disputed_zero_count:
        note = (
            f"⚠️ {disputed_zero_count} baris ledger ber-PnL 0.00 karena Exit=Entry TANPA sumber harga "
            f"(warisan pra-Ronde 5) — dikeluarkan dari win-rate; alasan: {disputed_reasons}"
        )
        print(note)
        log.warning(note)

    quality_line = (
        f"LEDGER TIDAK TERTBACA ({sheet_status}) — angka 0 di bawah BUKAN berarti tidak ada trade"
        if ledger_unreadable
        else (
            f"Sumber ledger: `{sheet_status}` | PnL terukur: `{total_trades}` | "
            f"belum terukur (backfill bursa): `{unmeasured_backfill_count}` | "
            f"diragukan (PnL=0 tanpa sumber harga): `{disputed_zero_count}`"
        )
    )
    print(quality_line)

    eq_line = (
        f"Perubahan Ekuitas (PnL\\_Tracker): `${equity_change:+.4f}` (`${equity_start:.2f}` → `${equity_latest:.2f}`)"
        if equity_change is not None
        else "Perubahan Ekuitas (PnL\\_Tracker): `N/A`"
    )
    hold_line = (
        f"Median Hold Time: `{median_hold_hours:.2f} jam` (target: `{EXPECTED_HOLD_HOURS:.0f} jam`)"
        if median_hold_hours is not None
        else f"Median Hold Time: `N/A` (target: `{EXPECTED_HOLD_HOURS:.0f} jam`)"
    )
    msg_lines = [
        f"*📈 Walt Perps — Closed Positions Stats* (Ver: `{stats['strategy_version']}`)",
        (
            f"LEDGER TIDAK TERTBACA ({sheet_status}) — angka 0 di bawah BUKAN berarti tidak ada trade"
            if ledger_unreadable
            else quality_line
        ),
        f"Total Trade Valid: *{total_trades}* (Win: {len(wins)} | Loss: {len(losses)}) | UNKNOWN: *{unknown_count}*",
        f"Win Rate (tanpa UNKNOWN): *{win_rate_pct:.1f}%*",
        eq_line,
        hold_line,
        f"Avg Win: `${avg_win_usd:+.4f}` (`{avg_win_pct:+.2f}%`)",
        f"Avg Loss: `${avg_loss_usd:+.4f}` (`{avg_loss_pct:+.2f}%`)",
        f"Gross Price PnL: `${gross_pnl_usd:+.4f}`",
        f"Total Funding: `${total_funding_usd:+.4f}`",
        f"Est. Fee ({TAKER_FEE_PER_SIDE*100:.2f}%/sisi): `-${estimated_fee_usd:.4f}`",
        f"Net PnL: *`${net_pnl_usd:+.4f}`*\n",
        "*Breakdown Alasan Close:*",
    ]
    if hold_time_warning:
        msg_lines.insert(5, hold_time_warning)
    for k, v in reason_breakdown.items():
        msg_lines.append(f"• {k}: {v}")

    if send_tg:
        send_telegram("\n".join(msg_lines))

    return stats


# =============================================================================
# MAIN DISPATCHER
# =============================================================================

def build_safe_stdout_summary(command: str, result: Any) -> dict[str, Any]:
    """Ringkasan aman untuk stdout & Step Summary repo publik (tanpa saldo/margin/PnL/order_id)."""
    if not isinstance(result, dict):
        return {"command": command, "status": "ok"}
    actions = result.get("actions")
    execs = result.get("executions")
    opps = result.get("top_opportunities") or result.get("opportunities")
    regime = result.get("execution_regime") or {}
    summary: dict[str, Any] = {
        "command": command,
        "ts": result.get("ts") or datetime.now(timezone.utc).isoformat(),
        "status": result.get("status", "ok"),
        "positions_checked": result.get("positions_checked"),
        "cycles_run": result.get("cycles_run"),
        "cadence_mode": result.get("cadence_mode"),
        "actions_count": len(actions) if isinstance(actions, list) else 0,
        "executions_count": len([e for e in execs if isinstance(e, dict) and e.get("verdict") in ("EXECUTED", "DRY_RUN")]) if isinstance(execs, list) else 0,
        "opportunities_count": len(opps) if isinstance(opps, list) else None,
        "market_gear": regime.get("market_gear") or result.get("gear"),
        "crypto_gear": regime.get("crypto_gear"),
        # R6-2: marker hangat/dingin terlihat di Step Summary tanpa membocorkan isi state.
        "state_warm": is_state_warm(),
        # Ronde 6: jawaban atas "execute close memberi harga fill atau tidak" — hanya nama kunci.
        "close_fill_missing_count": CLOSE_FILL_MISSING_COUNT,
        "close_response_shapes": sorted(CLOSE_SCHEMA_PROBED)[:2],
        # Ronde 7: dict kosong = semua input terukur hidup; non-kosong = ada input yang mati,
        # dan itu harus terbaca di Step Summary, bukan tenggelam di baris log.
        "input_health": input_health_snapshot(),
        "input_meta": input_meta_snapshot(),
        "inputs_probed": inputs_probed_snapshot(),
    }
    return {k: v for k, v in summary.items() if v is not None}


def _emit_cli_result(command: str, result: Any) -> None:
    # Ronde 7: satu rangkuman kegagalan input per siklus (warning teragregasi + alert Telegram
    # bila lewat ambang) SEBELUM ringkasan dibuat, supaya health-nya ikut tersimpan & tercetak.
    flush_input_health()
    safe_summary = build_safe_stdout_summary(command, result)
    try:
        os.makedirs("logs", exist_ok=True)
        with open("logs/last_safe_summary.json", "w", encoding="utf-8") as f:
            json.dump(safe_summary, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        log.warning(
            f"Gagal menulis logs/last_safe_summary.json untuk command={command} ({type(exc).__name__}): {exc}"
        )
    if is_log_redact_enabled():
        print(json.dumps(safe_summary, indent=2, default=str))
    else:
        print(json.dumps(result, indent=2, default=str))


def dump_closed_schema(client: WaltClient, limit: int = 3) -> dict[str, Any]:
    """
    Inspeksi struktur field posisi closed dari Walt tanpa mencetak nilai sensitif.
    Mencetak nama KEY dan tipe data dari <= 3 posisi pertama; jika kosong, inspeksi MCP tools/list.
    """
    resp = client.list_positions(statuses=["closed"])
    positions = resp.get("positions", resp) if isinstance(resp, dict) else (resp or [])
    if not isinstance(positions, list):
        positions = []
    from sheet_logger import sheet_fingerprint

    sample_schemas = []
    for idx, p in enumerate(positions[:limit]):
        if isinstance(p, dict):
            sample_schemas.append({
                "index": idx,
                "keys": {k: type(v).__name__ for k, v in sorted(p.items())},
            })

    out: dict[str, Any] = {
        "closed_positions_count": len(positions),
        "sample_schemas": sample_schemas,
        # R6-1: pengukuran yang sama dipakai reconcile_history — dicetak di diagnostics CI agar
        # kelengkapan riwayat (termasuk kemungkinan TERPOTONG) terlihat tanpa kredensial lokal.
        "history_measurement": describe_closed_history(client, positions=positions, resp=resp),
        # Ronde 6: cocokkan sheet yang dipakai CI dengan yang disebut pemilik, tanpa membocorkan ID.
        "sheet_fingerprint": sheet_fingerprint(),
    }
    if not positions:
        try:
            tools = client.list_tools()
            out["mcp_tools"] = [
                {"name": t.get("name"), "description": str(t.get("description", ""))[:120]}
                for t in tools
                if isinstance(t, dict)
            ]
        except Exception as e:
            out["mcp_tools_error"] = str(e)
    return out


def _extract_positions(resp: Any) -> list[dict]:
    """Ambil daftar posisi dari respons MCP (dict berisi 'positions' atau list langsung)."""
    if isinstance(resp, dict):
        pos = resp.get("positions", [])
        return [p for p in pos if isinstance(p, dict)] if isinstance(pos, list) else []
    if isinstance(resp, list):
        return [p for p in resp if isinstance(p, dict)]
    return []


def _is_valid_number(value: Any) -> bool:
    try:
        num = float(str(value).strip())
    except (TypeError, ValueError):
        return False
    return num == num and num not in (float("inf"), float("-inf"))


def describe_closed_history(
    client: WaltClient,
    positions: Optional[list[dict]] = None,
    resp: Any = None,
    probe_pagination: bool = True,
) -> dict[str, Any]:
    """
    R6-1 langkah 1: UKUR dulu isi riwayat closed di bursa sebelum menulis kode apa pun.
    READ-ONLY: hanya list_positions (+ tools/list untuk mendeteksi kemampuan paginasi).
    Tidak pernah memanggil prepare/execute order.
    """
    import inspect

    if positions is None:
        resp = client.list_positions(statuses=["closed"])
        positions = _extract_positions(resp)

    wrapper_keys = sorted(resp.keys()) if isinstance(resp, dict) else []
    pagination_hints = {
        k: resp.get(k)
        for k in ("total", "count", "limit", "page", "pageSize", "cursor", "nextCursor", "hasMore")
        if isinstance(resp, dict) and k in resp
    }

    created_vals = sorted(v for v in (str(p.get("created_at") or "").strip() for p in positions) if v)
    updated_vals = sorted(v for v in (str(p.get("updated_at") or "").strip() for p in positions) if v)

    invalid_fields: dict[str, int] = {}
    for field in ("avg_entry_price", "value_base", "margin_quote", "funding_paid_quote", "contract_id", "leverage"):
        invalid_fields[field] = sum(1 for p in positions if not _is_valid_number(p.get(field)))

    client_params = list(inspect.signature(client.list_positions).parameters)
    tool_pagination_params: list[str] = []
    if probe_pagination:
        try:
            for tool in client.list_tools():
                if tool.get("name") != "perps_list_positions":
                    continue
                schema = tool.get("inputSchema") or {}
                props = list((schema.get("properties") or {}).keys()) if isinstance(schema, dict) else []
                tool_pagination_params = [
                    p for p in props
                    if any(h in p.lower() for h in ("limit", "offset", "page", "cursor", "since"))
                ]
        except Exception as exc:
            log.warning(f"describe_closed_history: gagal inspeksi tools/list ({type(exc).__name__}): {exc}")

    total_hint = None
    for key in ("total", "count", "totalCount"):
        if isinstance(pagination_hints.get(key), int):
            total_hint = pagination_hints[key]
            break

    if total_hint is not None and total_hint == len(positions):
        completeness_verdict = f"LENGKAP (bursa melaporkan total={total_hint} == jumlah item diterima)"
    elif tool_pagination_params:
        completeness_verdict = (
            "TIDAK BISA DIVERIFIKASI: endpoint mendukung paginasi "
            f"{tool_pagination_params} tapi klien tidak memakainya"
        )
    else:
        completeness_verdict = (
            "TIDAK BISA DIVERIFIKASI: kemungkinan terpotong — respons tidak memuat total/cursor "
            "dan klien tidak punya parameter paginasi"
        )

    return {
        "closed_positions_count": len(positions),
        "response_wrapper_keys": wrapper_keys,
        "pagination_hints_in_response": pagination_hints,
        "client_list_positions_params": client_params,
        "tool_pagination_params": tool_pagination_params,
        "created_at_min": created_vals[0] if created_vals else None,
        "created_at_max": created_vals[-1] if created_vals else None,
        "updated_at_min": updated_vals[0] if updated_vals else None,
        "updated_at_max": updated_vals[-1] if updated_vals else None,
        "invalid_number_field_counts": invalid_fields,
        "completeness_verdict": completeness_verdict,
    }


def build_backfill_rows(closed_positions: list[dict]) -> list[dict]:
    """
    Konversi posisi closed dari bursa menjadi baris ledger.
    KUNCI DEDUPE: memakai ulang make_position_dedupe_key (SATU skema kunci untuk backfill & live),
    dengan pemetaan contract_id <- contract_id, entry_price <- avg_entry_price,
    opened_at <- created_at, position_id <- "" — jalur live membangun kunci yang sama dari
    snapshot open sehingga backfill dan deteksi live collapse ke satu identitas.
    """
    rows: list[dict] = []
    for cp in closed_positions:
        key = make_position_dedupe_key(
            {
                "position_id": "",
                "contract_id": cp.get("contract_id"),
                "entry_price": cp.get("avg_entry_price"),
                "opened_at": cp.get("created_at"),
            }
        )
        rows.append(
            {
                "position_id": key,
                "asset": cp.get("contract_base_asset") or cp.get("contract_name") or "",
                "side": str(cp.get("side") or "").upper(),
                "contract_id": cp.get("contract_id"),
                "entry_price": cp.get("avg_entry_price"),
                "exit_price": None,
                "margin": cp.get("margin_quote"),
                "pnl_quote": None,
                "funding_quote": cp.get("funding_paid_quote"),
                "reason": BACKFILL_CLOSE_REASON,
                "closed_at": str(cp.get("updated_at") or ""),
                "opened_at": str(cp.get("created_at") or ""),
                "exit_source": EXCHANGE_HISTORY_EXIT_SOURCE,
            }
        )
    return rows


def _recorded_closed_ids_from_sheet() -> tuple[set[str], set[str], str]:
    """(position_id, kunci_identitas_cadangan, status) dari sheet `Closed_Positions`.

    Kenapa perlu: `reconcile_history` dulu hanya dedupe terhadap CSV lokal + closed_ids.json.
    Di CI, CSV itu berasal dari Actions cache yang bisa ter-eviksi, sedangkan mirror backfill
    justru MENULIS ke sheet. Tersensus 17:54Z: sheet berisi 17 baris tapi cuma 6 yang punya
    Position ID -> dedupe berbasis id buta terhadap 11 sisanya.
    Status ikut dikembalikan supaya kegagalan baca TIDAK dianggap "sheet kosong"
    (prinsip: tidak ada data = None/status, bukan nol).
    """
    try:
        from sheet_logger import (
            close_identity_keys,
            fetch_sheet_csv_rows_ex,
            resolve_closed_positions_columns,
        )
    except Exception as e:  # pragma: no cover - jalur impor
        return set(), set(), f"import_failed:{type(e).__name__}"
    rows, status = fetch_sheet_csv_rows_ex("Closed_Positions", expected_col_name="Asset")
    if status not in ("ok", "local_csv") or not rows:
        return set(), set(), status
    try:
        col_map, data_rows, _fallback = resolve_closed_positions_columns(rows)
    except Exception as e:  # pragma: no cover
        return set(), set(), f"parse_failed:{type(e).__name__}"

    def sel(r, kunci):
        i = col_map.get(kunci)
        return r[i] if i is not None and len(r) > i else ""

    id_idx = col_map.get("position id")
    ids: set[str] = set()
    keys: set[str] = set()
    for r in data_rows:
        pid = str(r[id_idx]).strip() if id_idx is not None and len(r) > id_idx else ""
        if pid:
            ids.add(pid)
        else:
            keys.update(close_identity_keys(
                sel(r, "asset"), sel(r, "side"), sel(r, "entry price ($)"),
            ))
    return ids, keys, status


def reconcile_history(
    client: WaltClient,
    write: bool = False,
    limit: Optional[int] = None,
    csv_path: str = "logs/closed_history.csv",
    closed_ids_path: Optional[str] = None,
    mirror_sheet: bool = False,
) -> dict[str, Any]:
    """
    Sinkronkan ledger lokal dengan riwayat closed API bursa. READ-ONLY terhadap bursa
    (hanya list_positions); tidak ada prepare/execute order. Default = dry-run.

    write=True menyimpan kandidat ke logs/closed_history.csv + closed_ids.json dengan backup
    bertimestamp lebih dulu. mirror_sheet=True MENAMBAH penulisan webhook ke Google Sheet —
    operasi yang tidak bisa dibatalkan, jadi ia wajib diminta eksplisit bersama write=True.
    Kandidat yang sudah ada di sheet tidak ditulis ulang (dedupe tiga sumber).
    """
    if mirror_sheet and not write:
        raise ValueError("mirror_sheet=True butuh write=True: mirror berarti menulis ke sheet")
    ids_path = closed_ids_path or CLOSED_IDS_PATH
    resp = client.list_positions(statuses=["closed"])
    positions = _extract_positions(resp)
    measurement = describe_closed_history(client, positions=positions, resp=resp)

    rows = build_backfill_rows(positions)
    if limit is not None:
        rows = rows[: max(0, int(limit))]

    sheet_ids, sheet_keys, sheet_status = _recorded_closed_ids_from_sheet()
    if mirror_sheet and sheet_status != "ok":
        # Mirror = menulis ke sheet. Kalau isi sheet tidak bisa dibaca, dedupe tidak mungkin
        # dilakukan dan menulis = berjudi dengan baris duplikat permanen di ledger performansi.
        # Lebih baik menolak dan meminta dijalankan ulang.
        summary_abort = {
            "mode": "write",
            "aborted": f"sheet tidak terbaca (status={sheet_status}) — mirror dibatalkan untuk hindari duplikat",
            "measurement": measurement,
            "rows_seen": len(rows),
            "already_recorded": 0,
            "candidates_to_add": 0,
            "candidate_preview": [],
            "written": 0,
            "mirrored_ke_sheet": 0,
            "backup": None,
            "sheet_dedupe": {"status": sheet_status, "punya_position_id": 0, "kunci_cadangan": 0,
                             "sumber": "gviz Closed_Positions"},
            "completeness_verdict": measurement["completeness_verdict"],
        }
        log.warning(summary_abort["aborted"])
        return summary_abort

    from sheet_logger import close_identity_keys

    def _sudah_tercatat(row: dict[str, Any]) -> bool:
        if row["position_id"] in sheet_ids:
            return True
        return bool(set(close_identity_keys(row.get("asset"), row.get("side"), row.get("entry_price")))
                    & sheet_keys)

    existing = _fetch_recorded_closed_ids(csv_path) | set(_load_closed_ids(ids_path))
    candidates = [r for r in rows if r["position_id"] not in existing and not _sudah_tercatat(r)]
    kena_kunci = sum(
        1 for r in rows
        if r["position_id"] not in existing and _sudah_tercatat(r)
    )

    summary: dict[str, Any] = {
        "mode": "write" if write else "dry_run",
        "measurement": measurement,
        "rows_seen": len(rows),
        "already_recorded": len(rows) - len(candidates),
        "sheet_dedupe": {
            "status": sheet_status,
            "punya_position_id": len(sheet_ids),
            "kunci_cadangan": len(sheet_keys),
            "dicocokkan_kunci_cadangan": kena_kunci,
            "sumber": "gviz Closed_Positions",
        },
        "candidates_to_add": len(candidates),
        "candidate_preview": [
            {
                "position_id": c["position_id"],
                "asset": c["asset"],
                "side": c["side"],
                "opened_at": c["opened_at"],
                "closed_at": c["closed_at"],
                "pnl": "BELUM TERUKUR (endpoint tanpa harga keluar)",
            }
            for c in candidates[:10]
        ],
        "written": 0,
        "backup": None,
        "completeness_verdict": measurement["completeness_verdict"],
    }

    if not write:
        summary["note"] = (
            f"dry-run: tidak ada file yang diubah. Pakai --write untuk menyimpan {len(candidates)} "
            f"baris kandidat ke {csv_path} + {ids_path}."
        )
        return summary

    from sheet_logger import log_close

    if os.path.exists(csv_path) and candidates:
        import shutil

        backup_path = f"{csv_path}.bak-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        shutil.copy2(csv_path, backup_path)
        summary["backup"] = os.path.basename(backup_path)

    for row in candidates:
        log_close(
            asset=row["asset"],
            side=row["side"],
            contract_id=int(row["contract_id"] or 0),
            entry_price=float(row["entry_price"] or 0.0),
            exit_price=None,
            margin=float(row["margin"] or 0.0),
            pnl_quote=None,
            funding_quote=float(row["funding_quote"] or 0.0),
            reason=row["reason"],
            position_id=row["position_id"],
            closed_at=row["closed_at"],
            opened_at=row["opened_at"],
            filepath=csv_path,
            exit_source=row["exit_source"],
            webhook=mirror_sheet,
        )
        _record_closed_id(row["position_id"], path=ids_path)
        summary["written"] += 1
        if mirror_sheet:
            summary["mirrored_ke_sheet"] = int(summary.get("mirrored_ke_sheet", 0)) + 1

    return summary


def load_local_env(path: str = ".env") -> dict[str, bool]:
    """
    Muat .env untuk pemakaian CLI lokal. Aturan keras:
      - nilai yang SUDAH ada di environment tidak pernah ditimpa (CI menyuntik env dari secrets);
      - tidak pernah mencetak nilai, hanya nama kunci yang diterapkan;
      - tidak dipakai saat import modul (khusus `__main__`), supaya proses test tidak pernah
        menyerap kredensial nyata.
    Return: {kunci_applied: True} — nama saja, tanpa nilai.
    """
    applied: dict[str, bool] = {}
    if not os.path.exists(path):
        log.info(f"load_local_env: {path} tidak ditemukan — memakai environment apa adanya")
        return applied
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                if raw.lstrip().startswith("#") or "=" not in raw:
                    continue
                key, _, val = raw.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if not key or key in os.environ:
                    continue
                os.environ[key] = val
                applied[key] = True
    except Exception as exc:
        log.warning(f"load_local_env gagal ({type(exc).__name__}): {str(exc)[:120]}")
    if applied:
        log.info(f"load_local_env: {len(applied)} kunci dimuat dari .env ({', '.join(sorted(applied))})")
    return applied


def cli_account_lock_enter(command: str) -> Optional[str]:
    """Genggam kunci dua-arah akun untuk perintah CLI yang menyentuh token Walt/OAK.

    `stats` hanya membaca ledger lokal -> tanpa lock. Fail-open: kegagalan backend
    lock TIDAK pernah menghentikan kerja trading (lihat account_lock.acquire).
    """
    if command == "stats":
        return None
    prefix = "ci" if (os.getenv("GITHUB_ACTIONS") or "").strip().lower() in {"1", "true"} else "cli"
    holder = f"{prefix}:{command}"
    ok, why = account_lock.acquire(holder, ttl_sec=1800, wait_sec=300)
    if not ok:
        log.warning(f"🔐 [ACCOUNT-LOCK] {why} — lanjut best-effort; proses lain mungkin masih aktif")
    elif why not in ("acquired", "disabled"):
        log.info(f"🔐 [ACCOUNT-LOCK] {why} — berjalan tanpa lock")
    return holder


def cli_account_lock_exit(holder: Optional[str]) -> None:
    if holder:
        account_lock.release(holder)


def main(command: str = "scan"):
    """
    CLI entry point.

    Usage:
        python walt_bot.py scan                  # daily scan
        python walt_bot.py scan_with_research    # daily scan + AI research
        python walt_bot.py monitor               # risk monitor run
        python walt_bot.py market_execute        # execute US stocks & commodities
        python walt_bot.py crypto_execute        # execute bluechip crypto (BTC, ETH, SOL)
        python walt_bot.py stats                 # closed positions performance stats (dengan ledger_source)
        python walt_bot.py reconcile_history     # dry-run: ukur & bandingkan riwayat closed bursa vs ledger
        python walt_bot.py reconcile_history --write   # simpan kandidat backfill ke ledger lokal (backup dulu)
        python walt_bot.py reconcile_history --write --mirror-sheet  # + mirror ke Google Sheet (TIDAK bisa dibatalkan)
        python walt_bot.py reconcile_history --limit 5 # uji parsial
        python walt_bot.py open NBIS short 2.50  # buka posisi manual
        python walt_bot.py close NBIS            # tutup posisi
    """
    import sys

    if command == "stats":
        result = compute_closed_stats()
        _emit_cli_result(command, result)
        return

    client = WaltClient()

    if command == "dump_closed":
        schema = dump_closed_schema(client)
        print(json.dumps(schema, indent=2, default=str))
        return

    if command == "reconcile_history":
        argv = sys.argv[2:]
        write = "--write" in argv
        mirror_sheet = "--mirror-sheet" in argv
        if mirror_sheet and not write:
            # Guard CLI: pesan yang jelas lebih murah daripada ValueError di tengah job.
            print("Penolakan: --mirror-sheet butuh --write (mirror = menulis ke Google Sheet).")
            return
        limit: Optional[int] = None
        if "--limit" in argv:
            pos = argv.index("--limit")
            if pos + 1 >= len(argv):
                print("Penggunaan: reconcile_history [--write] [--mirror-sheet] [--limit N]")
                return
            limit = int(argv[pos + 1])
        result = reconcile_history(client, write=write, limit=limit, mirror_sheet=mirror_sheet)
        print(json.dumps(result, indent=2, default=str))
        print(f"\nKELENGKAPAN: {result['completeness_verdict']}")
        return

    if command == "scan":
        result = daily_scan(client)
        _emit_cli_result(command, result)

    elif command == "scan_with_research":
        dry = os.getenv("DRY_RUN", "false").lower() == "true"
        auto_exec = os.getenv("AUTO_EXECUTE_ON_SCAN", "true").lower() == "true"
        result = scan_with_research(client, auto_execute=auto_exec, dry_run=dry)
        _emit_cli_result(command, result)

    elif command == "monitor":
        result = risk_monitor_run(client)
        # Send Telegram kalau ada aksi
        if result.get("actions"):
            send_telegram(format_monitor_actions_telegram(result["actions"]))
        _emit_cli_result(command, result)

    elif command in ("market_execute", "execute"):
        dry = os.getenv("DRY_RUN", "false").lower() == "true"
        result = market_execute(client, dry_run=dry)
        _emit_cli_result(command, result)

    elif command == "crypto_execute":
        dry = os.getenv("DRY_RUN", "false").lower() == "true"
        result = crypto_execute(client, dry_run=dry)
        _emit_cli_result(command, result)

    elif command == "open":
        if len(sys.argv) < 5:
            print("Usage: open <ASSET> <side> <margin>")
            return
        asset, side, margin = sys.argv[2], sys.argv[3], float(sys.argv[4])
        contracts = client.list_contracts()
        contract = next(
            (c for c in contracts["contracts"] if c["base_asset"] == asset),
            None,
        )
        if not contract:
            print(f"Asset {asset} tidak ditemukan")
            return
        fr = float(contract["funding_rate_hourly"]) * 24
        opp = Opportunity(
            contract_id=contract["contract_id"],
            base_asset=asset,
            funding_daily=fr,
            volume_24h=float(contract["volume_24h_usd"]),
            max_leverage=contract["max_leverage"],
            side=side,  # type: ignore
            min_size_base=float(contract.get("min_size_base", 0.0)),
            price_slippage=float(contract.get("price_slippage", 0.0)),
            min_margin_2x=0,
        )
        open_position(client, opp, margin=margin)

    else:
        print(f"Unknown command: {command}")


if __name__ == "__main__":
    import sys
    load_local_env()  # hanya untuk CLI lokal; impor modul (dan pytest) tidak pernah menyerap .env
    # Kekhangatan state tidak boleh diasumsikan: bila CI belum menyetel marker, simpulkan dari berkas
    # state yang nyata ada (paritas dengan `state_probe` di workflow) — bukan default "hangat".
    os.environ.setdefault(STATE_WARM_ENV, probe_local_state_warm())
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    _lock_holder = cli_account_lock_enter(cmd)
    try:
        main(cmd)
    finally:
        cli_account_lock_exit(_lock_holder)
