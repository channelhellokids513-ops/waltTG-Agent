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
import time
import json
import random
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import httpx  # pip install httpx

from token_manager import TokenManager
from llm_research import QwenClient, GeminiClient, research_stock, Research
from notifier import send_telegram


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

try:
    from strategy_config import load_strategy_config as _load_strategy_config
    _rt_cfg = _load_strategy_config()
    DEFAULT_SL_PCT = float(_rt_cfg.get("default_sl_pct", DEFAULT_SL_PCT))
    DEFAULT_TP_PCT = float(_rt_cfg.get("tp_volatile_pct", DEFAULT_TP_PCT))
    DEFAULT_CALM_TP_PCT = float(_rt_cfg.get("tp_calm_pct", DEFAULT_CALM_TP_PCT))
    MIN_FUNDING_DAILY = float(_rt_cfg.get("min_funding_daily", MIN_FUNDING_DAILY))
    REENTRY_COOLDOWN_SEC = int(float(_rt_cfg.get("reentry_cooldown_hours", 3.0)) * 3600)
except Exception as _cfg_exc:
    logging.getLogger("walt-bot").warning(
        f"Gagal memuat konfigurasi strategi bersama ({_cfg_exc}); fallback ke DEFAULT_STRATEGY_CONFIG."
    )

# Tangga Trailing Stop (True Zero-Risk — surplus bersih setelah fee 0.18% + spread)
TRAILING_TIERS = [
    (0.0080, "lock_0035"),  # Tier 1: profit >= +0.80% → SL di entry +0.35% (surplus bersih setelah fee)
    (0.0140, "lock_0085"),  # Tier 2: profit >= +1.40% → SL di entry +0.85% (kunci cuan menengah)
    (0.0200, 0.0060),       # Tier 3: profit >= +2.00% → Trailing stop 0.60% di bawah running price
]


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
# CLIENT — thin wrapper for Walt MCP over HTTP
# =============================================================================

class WaltClient:
    """MCP client untuk Walt Perps dengan auto-refresh OAuth token."""

    def __init__(self, base_url: str = WALT_MCP_URL, token_manager: TokenManager | None = None):
        self.base_url = base_url
        self.tokens = token_manager or TokenManager()
        self.client = httpx.Client(timeout=30.0)

    def _headers(self, force_refresh: bool = False) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.tokens.get_access_token(force_refresh=force_refresh)}",
        }

    def _call(self, tool: str, args: dict | None = None, _retry: bool = False) -> Any:
        """Panggil satu tool MCP via JSON-RPC. Auto-retry sekali kalau 401."""
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        }
        r = self.client.post(self.base_url, json=payload, headers=self._headers())
        if r.status_code == 401 and not _retry:
            # Access token mungkin expired, force refresh + retry
            self.tokens.get_access_token(force_refresh=True)
            return self._call(tool, args, _retry=True)
        r.raise_for_status()
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
        r = self.client.post(self.base_url, json=payload, headers=self._headers())
        if r.status_code == 401:
            self.tokens.get_access_token(force_refresh=True)
            r = self.client.post(self.base_url, json=payload, headers=self._headers())
        r.raise_for_status()
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
        return self._call("perps_prepare_position_close", {"contractId": contract_id})

    def execute(self, token: str):
        return self._call("perps_execute_operation", {"confirmationToken": token})


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
    price_slippage: float = 0.0
    min_margin_2x: float = 0.0   # minimum margin efektif di lev 2x

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

    @property
    def pnl_pct_margin(self) -> float:
        """PnL unrealized sebagai % dari margin."""
        if self.side == "long":
            move = (self.current_price - self.entry_price) / self.entry_price
        else:
            move = (self.entry_price - self.current_price) / self.entry_price
        return move * self.leverage

    @property
    def profit_pct_price(self) -> float:
        """Profit sebagai % pergerakan harga searah posisi."""
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
    return opps


# =============================================================================
# ENTRY
# =============================================================================

# =============================================================================
# RESEARCH LAYER — fundamentals & news check untuk saham
# =============================================================================

def check_earnings_date(ticker: str) -> Optional[int]:
    """
    Cek days_to_earnings via Yahoo Finance API (gratis, no key).
    Return None kalau tidak ketemu atau error.
    """
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
        params = {"modules": "calendarEvents"}
        r = httpx.get(url, params=params, timeout=10.0,
                      headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
        earnings = (
            data.get("quoteSummary", {})
            .get("result", [{}])[0]
            .get("calendarEvents", {})
            .get("earnings", {})
            .get("earningsDate", [])
        )
        if not earnings:
            return None
        earnings_ts = earnings[0].get("raw")
        if not earnings_ts:
            return None
        from datetime import datetime as dt
        days = (dt.fromtimestamp(earnings_ts) - dt.now()).days
        return max(0, days)
    except Exception as e:
        log.warning(f"earnings lookup failed for {ticker}: {e}")
        return None


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
) -> Research:
    """Full research bundle untuk 1 saham dengan kolaborasi Dual-AI (Qwen + Gemini)."""
    log.info(f"🔍 Researching {ticker}...")
    if qwen or gemini:
        import llm_research
        return llm_research.research_stock(
            ticker,
            qwen=qwen,
            gemini=gemini,
            funding_daily=funding_daily,
            recommended_side=recommended_side,
        )
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


def decide_go_nogo(
    opp: Opportunity,
    research: Optional[Research],
) -> tuple[Literal["GO", "HALF", "SKIP"], str, float]:
    """
    Apply decision matrix dengan pertimbangan konsensus 4-Voter Swarm AI (P1-P7).
    Return (verdict, reason, margin_multiplier).
    margin_multiplier = 1.0 (full) atau 0.5 (half) atau 0 (skip).
    """
    # Minimum funding threshold
    if abs(opp.funding_daily) < MIN_FUNDING_DAILY:
        return "SKIP", "funding kurang (<0.5%/hari)", 0.0

    # Whitelist check
    if opp.base_asset not in ALLOWED_ASSETS:
        return "SKIP", f"aset {opp.base_asset} tidak masuk whitelist", 0.0

    # Komoditas / Crypto Bluechip: langsung GO dengan Dynamic TP/SL (no stock research needed)
    if opp.base_asset in ALLOWED_COMMODITIES or opp.base_asset in ALLOWED_CRYPTO:
        label = "crypto bluechip" if opp.base_asset in ALLOWED_CRYPTO else "komoditas"
        return "GO", f"{label}, dynamic TP/SL carry capture", 1.0

    # Saham perlu research
    if research is None:
        return "HALF", "no research data (fallback)", 0.5

    # Earnings risk (H-7)
    if research.days_to_earnings is not None and research.days_to_earnings <= 7:
        return "SKIP", f"earnings H-{research.days_to_earnings}", 0.0

    # Ketiadaan Berita / Sentimen Unknown
    if research.consensus == "NO_DATA" or research.news_sentiment == "unknown":
        return "HALF", "tidak ada berita/headlines → proteksi HALF size (panen yield funding konservatif)", 0.5

    # P5 Risk Catalysts Check (katalis event resiko tinggi >= 0.70)
    p5_risk = research.p1_to_p7_scores.get("P5_risk_catalysts", 0.0) if research.p1_to_p7_scores else 0.0
    if p5_risk >= 0.70:
        return "HALF", f"High Risk Catalyst (P5={p5_risk:.2f} >= 0.70) → proteksi HALF size", 0.5

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
    - Asymmetric R:R: TP +2.0% s/d +2.5% vs SL 1.5% agar surplus bersih jauh melampaui fee 0.18%
    - Windfall Take Profit: Jika 2.5x funding harian berada di 1.5% <= windfall < tp_pct, TP dimajukan
    """
    tp_pct, sl_pct = get_asset_baseline_tpsl_pct(asset)

    if abs(funding_daily) >= 0.005:
        windfall_tp = round(abs(funding_daily) * 2.5, 4)
        if 0.015 <= windfall_tp < tp_pct:
            tp_pct = windfall_tp

    if side == "long":
        tp = round(entry * (1 + tp_pct), 4)
        sl = round(entry * (1 - sl_pct), 4)
    else:
        tp = round(entry * (1 - tp_pct), 4)
        sl = round(entry * (1 + sl_pct), 4)
    return tp, sl


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

    return margin, price


def open_position(
    client: WaltClient,
    opp: Opportunity,
    margin: float = DEFAULT_MARGIN,
    leverage: int = MAX_LEVERAGE,
) -> Optional[dict]:
    """Preview → execute buka posisi dengan Dynamic TP/SL dan minimum order size enforcement."""
    resolved = resolve_order_margin(client, opp, margin, leverage)
    if resolved is None:
        return None
    margin, price = resolved

    tp, sl = compute_baseline_tpsl(opp.side, price, asset=opp.base_asset, funding_daily=opp.funding_daily)

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

    token = preview["confirmationToken"]
    try:
        result = client.execute(token)
    except Exception as e:
        log.error(f"{opp.base_asset}: execute(token) gagal: {e}")
        return None

    if not result or (
        isinstance(result, dict)
        and (result.get("ok") is False or "error" in result or result.get("status") == "failed")
    ):
        log.error(f"{opp.base_asset}: execute(token) mengembalikan kegagalan: {result}")
        return None

    order_id = result.get("order", {}).get("order_id") if isinstance(result, dict) else None
    log.info(
        f"✅ {opp.side.upper()} {opp.base_asset} opened: margin ${margin} @ ${price:.4f}, "
        f"TP ${tp}, SL ${sl}, order_id={order_id}"
    )

    # Jika preview berhasil tanpa TP/SL, set terpisah dengan retry maksimal 3x
    if "autoClose" not in preview.get("action", {}).get("request", {}):
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
        result["actual_margin"] = margin
        result["tp"] = tp
        result["sl"] = sl
        result["price"] = price
    return result


# =============================================================================
# RISK MONITOR — 4 LAPIS
# =============================================================================

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


def _record_position_close(pos: Position, reason: str):
    """Catat penutupan posisi ke Closed_Positions (Google Sheet), logs/closed_history.csv, closed_ids, dan set cooldown."""
    pos_key = make_position_dedupe_key({
        "position_id": pos.position_id,
        "contract_id": pos.contract_id,
        "entry_price": pos.entry_price,
        "opened_at": pos.opened_at,
    })
    try:
        from sheet_logger import log_close
        pnl_quote = pos.pnl_pct_margin * pos.margin
        log_close(
            asset=pos.base_asset,
            side=pos.side,
            contract_id=pos.contract_id,
            entry_price=pos.entry_price,
            exit_price=pos.current_price,
            margin=pos.margin,
            pnl_quote=pnl_quote,
            funding_quote=pos.funding_paid,
            reason=reason,
            position_id=pos_key,
            closed_at=datetime.now(timezone.utc).isoformat(),
        )
        _record_closed_id(pos_key)
    except Exception as e:
        log.warning(f"Gagal mencatat log_close untuk {pos.base_asset}: {e}")
    _set_asset_cooldown(pos.base_asset)


def evaluate_and_apply_position_risk(
    client: WaltClient,
    pos: Position,
    current_funding_daily: float,
) -> Optional[dict]:
    """
    Evaluasi 4 Lapis Risiko Terpadu & Eksekusi Atomik Tunggal:
    1. Lapis 2 (Funding Flip Sign): Jika funding berbalik arah negatif → TUTUP POSISI SEGERA.
    2. TP Dinamis: Ambil target TP baseline/windfall, perketat jika target baru lebih dekat ke entry.
    3. SL Dinamis: Komparasikan kandidat SL dari:
       - Baseline 1% (Lapis 1)
       - Funding rendah <0.1%/hari (Lapis 2)
       - Progressive Trailing Stop / Early Zero-Risk Lock (Lapis 4)
       Pilih SL yang paling aman & menguntungkan (strictly better).
    4. Eksekusi Atomik: Kirim SATU request auto_close_set ke exchange untuk menghindari
       error 'auto_close_already_pending'.
    """
    # --- 1. Lapis 2: Funding Flip Sign (dengan Hysteresis) → Tutup Posisi Segera ---
    funding_flipped = (
        (pos.side == "long" and current_funding_daily > 0)
        or (pos.side == "short" and current_funding_daily < 0)
    )
    if funding_flipped and abs(current_funding_daily) >= FUNDING_FLIP_CLOSE_THRESHOLD:
        log.warning(
            f"[L2] {pos.base_asset}: funding flip sign ({current_funding_daily*100:+.3f}%/hari >= "
            f"{FUNDING_FLIP_CLOSE_THRESHOLD*100:.2f}%) → tutup posisi riil"
        )
        preview = client.preview_close(pos.contract_id)
        if preview and preview.get("ok"):
            res = client.execute(preview["confirmationToken"])
            _record_position_close(pos, "Funding Flip Sign")
            return {
                "action": "CLOSE",
                "detail": "Funding Flip Sign → Close Posisi",
                "reason": "Funding Flip Sign",
                "result": res,
            }
        return None

    # --- 2. Evaluasi Target TP (Lapis 1 Baseline & Windfall) ---
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

    # A. Kandidat dari Baseline 1% (Lapis 1)
    if not pos.sl or _sl_is_improvement(pos, target_sl):
        candidate_sl = target_sl
        sl_reason = "L1 Baseline"

    # B. Kandidat dari Funding Lemah / Flip di bawah threshold <0.1% (Lapis 2)
    if abs(current_funding_daily) < FUNDING_FLIP_CLOSE_THRESHOLD:
        l2_sl = round(pos.entry_price, 4)
        l2_valid_side = (
            (pos.side == "long" and l2_sl < pos.current_price)
            or (pos.side == "short" and l2_sl > pos.current_price)
        )
        if l2_valid_side and _sl_is_better(pos.side, l2_sl, candidate_sl) and _sl_is_improvement(pos, l2_sl):
            candidate_sl = l2_sl
            sl_reason = "L2 Breakeven"

    # D. Kandidat dari Trailing Stop (Lapis 4 — True Zero-Risk surplus bersih setelah fee)
    profit = pos.profit_pct_price
    if profit >= TRAILING_TIERS[0][0]:
        active_tier = None
        for threshold, action in TRAILING_TIERS:
            if profit >= threshold:
                active_tier = (threshold, action)
        if active_tier:
            threshold, action = active_tier
            # Cabang aktif True Zero-Risk: LONG di atas entry, SHORT di bawah entry
            if action == "lock_0035":
                l4_sl = pos.entry_price * 1.0035 if pos.side == "long" else pos.entry_price * 0.9965
                tier_desc = f"L4 True Zero-Risk +0.35% (Profit {profit*100:+.2f}%)"
            elif action == "lock_0085":
                l4_sl = pos.entry_price * 1.0085 if pos.side == "long" else pos.entry_price * 0.9915
                tier_desc = f"L4 Lock Cuan +0.85% (Profit {profit*100:+.2f}%)"
            # Cabang lama dipertahankan untuk kompatibilitas mundur
            elif action == "lock_0010":
                l4_sl = pos.entry_price * 1.0010 if pos.side == "long" else pos.entry_price * 0.9990
                tier_desc = f"L4 Zero-Risk +0.10% (Profit {profit*100:+.2f}%)"
            elif action == "lock_0022":
                l4_sl = pos.entry_price * 1.0022 if pos.side == "long" else pos.entry_price * 0.9978
                tier_desc = f"L4 Lock +0.22% (Profit {profit*100:+.2f}%)"
            elif action == "lock_0030":
                l4_sl = pos.entry_price * 1.0030 if pos.side == "long" else pos.entry_price * 0.9970
                tier_desc = f"L4 Lock Cuan +0.30% (Profit {profit*100:+.2f}%)"
            elif action in ("lock_0020", "lock_0025"):
                l4_sl = pos.entry_price * 1.0020 if pos.side == "long" else pos.entry_price * 0.9980
                tier_desc = f"L4 Zero-Risk +0.20% (Profit {profit*100:+.2f}%)"
            elif action in ("lock_0050", "lock_008"):
                l4_sl = pos.entry_price * 1.0050 if pos.side == "long" else pos.entry_price * 0.9950
                tier_desc = f"L4 Lock Cuan +0.50% (Profit {profit*100:+.2f}%)"
            elif action in ("lock_0100", "lock_015"):
                l4_sl = pos.entry_price * 1.0100 if pos.side == "long" else pos.entry_price * 0.9900
                tier_desc = f"L4 Lock Cuan +1.00% (Profit {profit*100:+.2f}%)"
            elif isinstance(action, (int, float)):
                trailing_pct = action
                l4_sl = pos.current_price * (1 - trailing_pct) if pos.side == "long" else pos.current_price * (1 + trailing_pct)
                tier_desc = f"L4 Trailing {trailing_pct*100:.1f}% (Profit {profit*100:+.2f}%)"
            else:
                l4_sl = pos.entry_price
                tier_desc = "L4 Breakeven"

            l4_sl = round(l4_sl, 4)
            if _sl_is_better(pos.side, l4_sl, candidate_sl) and _sl_is_improvement(pos, l4_sl):
                candidate_sl = l4_sl
                sl_reason = tier_desc

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
        log.warning(
            f"[GUARD] {pos.base_asset}: SL 1.5% breached (current=${pos.current_price:.4f}, sl=${sl_check:.4f}) → tutup posisi"
        )
        preview = client.preview_close(pos.contract_id)
        if preview and preview.get("ok"):
            res = client.execute(preview["confirmationToken"])
            _record_position_close(pos, "SL 1.5% breached (migrasi)")
            return {
                "action": "CLOSE",
                "detail": "SL 1.5% breached (migrasi)",
                "reason": "SL 1.5% breached (migrasi)",
                "result": res,
            }
        return None

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
        if preview and preview.get("ok"):
            res = client.execute(preview["confirmationToken"])
            _record_position_close(pos, "TP breached")
            return {
                "action": "CLOSE",
                "detail": "TP breached",
                "reason": "TP breached",
                "result": res,
            }
        return None

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
    if preview.get("ok"):
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
        return {"action": "FAILED", "detail": f"Preview failed: {preview.get('code')}", "result": preview}


def apply_layer1_baseline(client: WaltClient, pos: Position) -> Optional[dict]:
    """Lapis 1: Set Dynamic TP/SL baseline (wrapper kompatibilitas)."""
    target_tp, target_sl = compute_baseline_tpsl(pos.side, pos.entry_price, asset=pos.base_asset)
    new_tp = target_tp if not pos.tp else None
    new_sl = target_sl if not pos.sl or _sl_is_improvement(pos, target_sl) else None
    if new_tp is None and new_sl is None:
        return None
    preview = client.preview_auto_close(pos.contract_id, tp=new_tp, sl=new_sl)
    if preview.get("ok"):
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
    if abs(current_funding_daily) < FUNDING_FLIP_CLOSE_THRESHOLD:
        new_sl = pos.entry_price
        if _sl_is_improvement(pos, new_sl):
            preview = client.preview_auto_close(pos.contract_id, sl=new_sl)
            if preview and preview.get("ok"):
                return client.execute(preview["confirmationToken"])
    return None


def apply_layer3_volatility(client: WaltClient, pos: Position) -> Optional[dict]:
    """deprecated: digantikan SL baseline 1%"""
    return None


def apply_layer4_trailing(client: WaltClient, pos: Position) -> Optional[dict]:
    """Lapis 4: Trailing stop progresif (wrapper kompatibilitas)."""
    profit = pos.profit_pct_price
    if profit < TRAILING_TIERS[0][0]:
        return None
    active_tier = None
    for threshold, action in TRAILING_TIERS:
        if profit >= threshold:
            active_tier = (threshold, action)
    if active_tier is None:
        return None
    threshold, action = active_tier
    # Cabang aktif True Zero-Risk: LONG di atas entry, SHORT di bawah entry
    if action == "lock_0035":
        new_sl = pos.entry_price * 1.0035 if pos.side == "long" else pos.entry_price * 0.9965
    elif action == "lock_0085":
        new_sl = pos.entry_price * 1.0085 if pos.side == "long" else pos.entry_price * 0.9915
    elif action == "lock_0010":
        new_sl = pos.entry_price * 1.0010 if pos.side == "long" else pos.entry_price * 0.9990
    elif action == "lock_0022":
        new_sl = pos.entry_price * 1.0022 if pos.side == "long" else pos.entry_price * 0.9978
    elif action == "lock_0030":
        new_sl = pos.entry_price * 1.0030 if pos.side == "long" else pos.entry_price * 0.9970
    # Cabang lama dipertahankan untuk kompatibilitas mundur
    elif action in ("lock_0020", "lock_0025"):
        new_sl = pos.entry_price * 1.0020 if pos.side == "long" else pos.entry_price * 0.9980
    elif action in ("lock_0050", "lock_008"):
        new_sl = pos.entry_price * 1.0050 if pos.side == "long" else pos.entry_price * 0.9950
    elif action in ("lock_0100", "lock_015"):
        new_sl = pos.entry_price * 1.0100 if pos.side == "long" else pos.entry_price * 0.9900
    elif isinstance(action, (int, float)):
        trailing_pct = action
        new_sl = pos.current_price * (1 - trailing_pct) if pos.side == "long" else pos.current_price * (1 + trailing_pct)
    else:
        new_sl = pos.entry_price
    new_sl = round(new_sl, 4)
    if not _sl_is_improvement(pos, new_sl, buffer=0.0005):
        return None
    preview = client.preview_auto_close(pos.contract_id, sl=new_sl)
    if preview.get("ok"):
        return client.execute(preview["confirmationToken"])
    return None


def _infer_position_verdict(base_asset: str, exec_csv_path: str = "logs/executions_history.csv") -> str:
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
    except Exception:
        pass
    return "GO"


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


def evaluate_execution_gear(
    opps: list[Opportunity],
    recent_tp_closed: bool = False,
    rng: Optional[Any] = None,
) -> tuple[str, int, str]:
    """
    Tentukan Gigi Kecepatan (Gear) dan interval eksekusi (60 atau 120 menit)
    untuk Market Execute & Crypto Execute agar tidak over-trading / boros fee:
    - GEAR 2 (Aktif Normal 60 Menit / 1 Jam):
      * Terdapat >= 3 aset dengan |funding_daily| >= 0.5%/hari, ATAU
      * Terdapat >= 2 aset dengan funding ekstrem (|funding_daily| >= 0.8%/hari).
    - GEAR 1 (Konservatif 120 Menit / 2 Jam):
      * Peluang funding moderat/tipis (<= 2 aset yang memenuhi >= 0.5%/hari).
    Return (gear_label, interval_minutes, reason).
    """
    qualifying = [o for o in opps if abs(o.funding_daily) >= MIN_FUNDING_DAILY]
    extreme = [o for o in qualifying if abs(o.funding_daily) >= 0.008]

    if len(qualifying) >= 3 or len(extreme) >= 2:
        return (
            "GEAR_2_ACTIVE_60M",
            60,
            f"Aktif ({len(qualifying)} peluang >=0.5%, {len(extreme)} ekstrem >=0.8%/hari) → interval 60 menit (1 jam)",
        )

    return (
        "GEAR_1_CONSERVATIVE_120M",
        120,
        f"Konservatif ({len(qualifying)} peluang >=0.5%/hari) → interval 120 menit (2 jam)",
    )


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


def _update_regime_state(
    category: Literal["market", "crypto"],
    opps: list[Opportunity],
    mark_executed: bool = False,
    recent_tp_closed: bool = False,
    regime_path: str = "logs/market_regime.json",
    rng: Optional[Any] = None,
) -> tuple[str, int, str]:
    """Update Gear, interval, dan timestamp eksekusi untuk 'market' atau 'crypto'."""
    state = _load_execution_regime(regime_path)
    gear, interval_min, reason = evaluate_execution_gear(
        opps, recent_tp_closed=recent_tp_closed, rng=rng
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
    except Exception:
        pass
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
        from sheet_logger import fetch_sheet_csv_rows
        rows = fetch_sheet_csv_rows(
            "Closed_Positions",
            expected_col_name="Exit Price ($)",
            local_csv_fallback=csv_path,
        )
        if not rows:
            return recorded
        header = [str(c).strip().lower() for c in rows[0]]
        pid_idx = header.index("position id") if "position id" in header else (10 if len(header) > 10 else -1)
        data_rows = rows[1:] if ("timestamp" in header[0] or "asset" in str(header)) else rows
        for r in data_rows:
            if pid_idx >= 0 and len(r) > pid_idx and str(r[pid_idx]).strip():
                recorded.add(str(r[pid_idx]).strip())
    except Exception as e:
        log.debug(f"Gagal membaca Position ID dari Closed_Positions: {e}")
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


def _detect_and_log_exchange_closes(
    client: WaltClient,
    active_positions: list[dict],
    snapshot_path: str = "logs/open_snapshot.json",
    closed_ids_path: str = CLOSED_IDS_PATH,
    closed_csv_path: str = "logs/closed_history.csv",
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
    for p in active_positions:
        if str(p.get("status", "open")).lower() not in ("open", "active"):
            continue
        pos_key = make_position_dedupe_key(p)
        cid_str = str(p["contract_id"])
        base_asset = p.get("contract_base_asset") or p.get("base_asset", "")
        prev_info = prev_snapshot.get(pos_key) or prev_snapshot.get(cid_str) or {}
        prev_verdict = prev_info.get("verdict")
        verdict = prev_verdict if prev_verdict in ("GO", "HALF") else _infer_position_verdict(base_asset)
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
            raw_exit = (
                cp.get("avg_close_price")
                or cp.get("close_price")
                or cp.get("exit_price")
            )
            exit_price: Optional[float] = float(raw_exit) if raw_exit is not None and float(raw_exit) > 0 else None

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
            closed_at = str(cp.get("closed_at") or cp.get("updated_at") or "")

            raw_pnl = (
                cp.get("realized_pnl_quote")
                if cp.get("realized_pnl_quote") is not None
                else (cp.get("pnl_quote") if cp.get("pnl_quote") is not None else cp.get("realized_pnl"))
            )
            if raw_pnl is not None:
                pnl_quote: Optional[float] = float(raw_pnl)
            elif entry_price > 0 and exit_price is not None:
                move = (exit_price - entry_price) / entry_price if side == "long" else (entry_price - exit_price) / entry_price
                pnl_quote = margin * leverage * move
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
                    filepath=closed_csv_path,
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
        )
        current_funding_daily = float(contract["funding_rate_hourly"]) * 24

        closed_now = False
        try:
            res = evaluate_and_apply_position_risk(client, pos, current_funding_daily)
            if res and res.get("action") != "FAILED":
                if res.get("action") == "CLOSE":
                    current_snapshot.pop(pos_key, None)
                    current_snapshot.pop(str(cid), None)
                    closed_now = True
                elif res.get("action") == "UPDATE_AUTO_CLOSE":
                    if res.get("tp") is not None:
                        pos.tp = float(res["tp"])
                    if res.get("sl") is not None:
                        pos.sl = float(res["sl"])
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
JOB_TIME_GUARD_MIN = 6.0  # Sisakan minimal 6 menit agar order + TP/SL tidak pernah terpotong timeout


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

def daily_scan(client: WaltClient) -> dict:
    """Scan harian — return summary untuk Calendar event / push notif."""
    readiness = client.readiness()
    balance = client.balance()
    positions = client.list_positions(statuses=["open"])
    contracts = client.list_contracts()

    opps = rank_opportunities(contracts["contracts"])
    market_opps = rank_opportunities(contracts["contracts"], allowed_assets=(ALLOWED_STOCKS | ALLOWED_COMMODITIES))
    crypto_opps = rank_opportunities(contracts["contracts"], allowed_assets=ALLOWED_CRYPTO)
    m_gear, m_int, m_reason = _update_regime_state("market", market_opps, mark_executed=False)
    c_gear, c_int, c_reason = _update_regime_state("crypto", crypto_opps, mark_executed=False)
    log.info(f"⚙️ [EXECUTION GEAR] Market: {m_gear} ({m_int}m) | Crypto: {c_gear} ({c_int}m)")

    total_equity = get_total_equity(balance, positions)
    scan_limit = max(5, get_dynamic_max_slots(total_equity))
    top_candidates = opps[:scan_limit]

    # Fetch live price untuk top candidates
    for opp in top_candidates:
        try:
            r = client.latest_rate(f"{opp.base_asset}-USD-PERP")
            opp_price = float(r["rate"])
        except Exception:
            opp_price = 0.0
        setattr(opp, "live_price", opp_price)
        if opp_price > 0 and opp.min_size_base > 0:
            opp.min_margin_2x = round(
                (opp.min_size_base * opp_price / MAX_LEVERAGE) * (1.0 + opp.price_slippage), 2
            )

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
                "funding_daily_pct": o.funding_daily * 100,
                "price": getattr(o, "live_price", 0),
                "min_margin_2x": o.min_margin_2x,
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
    for i, opp in enumerate(summary["top_opportunities"][:5], 1):
        asset = opp["asset"]
        side = opp["side"].upper()
        funding = opp["funding_daily_pct"]
        price = opp["price"]
        research = researches.get(asset)

        # Decision
        from types import SimpleNamespace
        pseudo_opp = SimpleNamespace(
            base_asset=asset, side=opp["side"],
            funding_daily=funding / 100,
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
                lines.append(f"   🤖 Swarm AI: `{research.overall_consensus_level}` (Vendor: `{research.vendor_alignment}`)")
                if research.qwen_sentiment and research.gemini_sentiment:
                    lines.append(f"   ├ Qwen: `{research.qwen_sentiment}` | Gemini: `{research.gemini_sentiment}`")
                p1_7 = research.p1_to_p7_scores
                if p1_7:
                    lines.append(
                        f"   ├ P1(`{p1_7.get('P1_sentiment_polarity', 0.0):+.2f}`) "
                        f"P4(`{p1_7.get('P4_momentum_direction', 0.0):+.2f}`) "
                        f"P5(`{p1_7.get('P5_risk_catalysts', 0.0):.2f}`) "
                        f"P6(`{p1_7.get('P6_contrarian_check', 0.0):.2f}`)"
                    )
            if research.narrative:
                lines.append(f"   _{research.narrative[:180]}_")
        lines.append(f"   → Keputusan: *{verdict}* ({reason})")

    executed_list = [e for e in (executions or []) if e.get("verdict") == "EXECUTED"]
    if executed_list:
        lines.append(f"\n🚀 *Posisi Baru Dibuka ({len(executed_list)}):*")
        for e in executed_list:
            order_id = e.get("order_id", "N/A")
            oid_short = f"`{order_id[:8]}...`" if order_id != "N/A" else "N/A"
            lines.append(f"• *{e['side'].upper()} {e['asset']}*: margin `${e['margin']:.2f}` @ `${e.get('price', 0):.4f}` (Order ID: {oid_short})")

    lines.append(f"\n_Fee taker 0.09%/sisi | Max lev 2x | Proteksi Auto TP/SL_")
    return "\n".join(lines)


def scan_with_research(
    client: WaltClient,
    auto_execute: Optional[bool] = None,
    dry_run: Optional[bool] = None,
) -> dict:
    """Daily scan + Dual-AI research (Qwen + Gemini) + Instant Auto-Execution pada sinyal GO/HALF + Telegram notif."""
    if auto_execute is None:
        auto_execute = os.getenv("AUTO_EXECUTE_ON_SCAN", "true").lower() == "true"
    if dry_run is None:
        dry_run = os.getenv("DRY_RUN", "false").lower() == "true"

    log.info(f"📊 Running daily scan + research (auto_execute={auto_execute}, dry_run={dry_run})...")
    summary = daily_scan(client)

    # Research tiap stock candidate
    qwen = QwenClient() if os.getenv("DASHSCOPE_API_KEY") else None
    gemini = (
        GeminiClient()
        if (os.getenv("DASHSCOPE_API_GEMINI_KEY") or os.getenv("GEMINI_API_KEY"))
        else None
    )
    researches: dict[str, Research] = {}

    for opp in summary["top_opportunities"]:
        asset = opp["asset"]
        if asset in ALLOWED_STOCKS and (qwen or gemini):
            try:
                researches[asset] = research_stock(
                    asset,
                    qwen=qwen,
                    gemini=gemini,
                    funding_daily=opp.get("funding_daily"),
                    recommended_side=opp.get("recommended_side"),
                )
                log.info(
                    f"  ✓ research {asset}: {researches[asset].news_sentiment} "
                    f"(Consensus: {researches[asset].consensus})"
                )
            except Exception as e:
                log.warning(f"  ✗ research {asset} failed: {e}")

    # Eksekusi instan jika auto_execute aktif
    executions = []
    deployed = 0.0
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

        raw_opps = summary.get("_raw_opportunities", [])
        for opp in raw_opps:
            if active_slot_count >= max_slots:
                log.info(f"  Slot maksimal tercapai ({active_slot_count}/{max_slots}), hentikan alokasi.")
                break
            if deployed >= target_deploy or (available - deployed) < 3.0:
                log.info(f"  Buffer saldo tercapai (${deployed:.2f}/${target_deploy:.2f}), hentikan alokasi.")
                break
            asset = opp.base_asset
            if asset in ALLOWED_STOCKS and asset not in researches and (qwen or gemini):
                try:
                    researches[asset] = research_stock(
                        asset,
                        qwen=qwen,
                        gemini=gemini,
                        funding_daily=opp.funding_daily,
                        recommended_side=opp.side,
                    )
                except Exception as e:
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

                try:
                    res = open_position(client, opp, margin=margin, leverage=MAX_LEVERAGE)
                    if res:
                        actual_margin = res.get("actual_margin", margin) if isinstance(res, dict) else margin
                        order_id = res.get("order", {}).get("order_id") if isinstance(res, dict) else None
                        order_price = res.get("price") or getattr(opp, "live_price", 0.0)
                        tp = res.get("tp")
                        sl = res.get("sl")
                        executions.append({
                            "asset": asset, "side": opp.side, "leverage": MAX_LEVERAGE, "margin": actual_margin,
                            "price": order_price, "tp": tp, "sl": sl,
                            "verdict": "EXECUTED", "reason": reason,
                            "order_id": order_id,
                        })
                        _record_open_position_verdict(
                            opp.contract_id, asset, opp.side, verdict, actual_margin, order_price
                        )
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

    return {
        "summary": summary,
        "researches": {k: v.__dict__ for k, v in researches.items()},
        "executions": executions,
        "deployed": deployed,
    }


# =============================================================================
# MARKET EXECUTE — buka posisi saham saat US market open
# =============================================================================

def market_execute(
    client: WaltClient,
    dry_run: bool = False,
    universe: Optional[set[str]] = None,
    title: str = "🚀 Walt Market Execute",
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
    opps = rank_opportunities(contracts_data["contracts"], allowed_assets=target_universe)

    category_key: Literal["market", "crypto"] = "crypto" if target_universe == ALLOWED_CRYPTO else "market"
    gear_label, gear_interval_min, gear_reason = _update_regime_state(
        category_key, opps, mark_executed=True
    )
    log.info(f"⚙️ [ADAPTIVE GEAR - {category_key.upper()}] {gear_label} ({gear_interval_min}m): {gear_reason}")

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
    from sheet_logger import fetch_latest_research_from_sheet
    cached_researches = fetch_latest_research_from_sheet()

    qwen = QwenClient() if os.getenv("DASHSCOPE_API_KEY") else None
    gemini = (
        GeminiClient()
        if (os.getenv("DASHSCOPE_API_GEMINI_KEY") or os.getenv("GEMINI_API_KEY"))
        else None
    )
    researches: dict[str, Research] = {}
    otf_research_calls = 0
    max_otf_calls = max_slots * 2
    for opp in opps:
        if opp.base_asset in ALLOWED_STOCKS:
            if opp.base_asset in cached_researches:
                cached = cached_researches[opp.base_asset]
                log.info(f"  ✓ Pakai riset spreadsheet {opp.base_asset}: skor {cached.final_score:+.2f}, sentimen {cached.news_sentiment}")
                researches[opp.base_asset] = cached
            elif (qwen or gemini) and otf_research_calls < max_otf_calls:
                try:
                    otf_research_calls += 1
                    log.info(
                        f"  🔍 Riset on-the-fly ({otf_research_calls}/{max_otf_calls}) untuk {opp.base_asset} (belum ada di spreadsheet)..."
                    )
                    researches[opp.base_asset] = research_stock(
                        opp.base_asset,
                        qwen=qwen,
                        gemini=gemini,
                        funding_daily=opp.funding_daily,
                        recommended_side=opp.side,
                    )
                except Exception as e:
                    log.warning(f"research {opp.base_asset}: {e}")

    # Decide + execute
    executions = []
    deployed = 0.0
    target_deploy = available * (1.0 - MIN_BUFFER_PCT)  # 15% cash buffer dijaga konsisten dari available

    active_slot_count = len(open_contract_ids)
    log.info(
        f"Dynamic Elastic Slot & Conviction Sizing: Equity ${total_equity:.2f} (Available ${available:.2f}) | "
        f"Posisi Aktif {active_slot_count}/{max_slots} | Max Allowed Slots: {max_slots}"
    )

    for opp in opps:
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

        try:
            result = open_position(client, opp, margin=margin, leverage=MAX_LEVERAGE)
            if result:
                open_contract_ids.add(opp.contract_id)
                active_slot_count += 1
                actual_margin = result.get("actual_margin", margin) if isinstance(result, dict) else margin
                order_id = result.get("order", {}).get("order_id") if isinstance(result, dict) else None
                order_price = result.get("price") or getattr(opp, "live_price", 0.0)
                tp = result.get("tp")
                sl = result.get("sl")
                executions.append({
                    "asset": opp.base_asset, "side": opp.side, "leverage": MAX_LEVERAGE, "margin": actual_margin,
                    "price": order_price, "tp": tp, "sl": sl,
                    "verdict": "EXECUTED", "reason": reason,
                    "order_id": order_id,
                })
                _record_open_position_verdict(
                    opp.contract_id, opp.base_asset, opp.side, verdict, actual_margin, order_price
                )
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
            msg_lines.append(f"   _{e['reason']}_")
    if skipped_list:
        msg_lines.append("\n*❌ Skipped:*")
        for e in skipped_list[:5]:
            msg_lines.append(f"• {e['asset']}: {e['reason']}")

    send_telegram("\n".join(msg_lines))

    # Logging eksekusi ke Google Sheet / CSV
    try:
        from sheet_logger import log_execution
        log_res = log_execution(executions)
        log.info(f"📊 Logging executions result: {log_res}")
    except Exception as e:
        log.warning(f"Logging execution to sheet failed: {e}")

    return {"executions": executions, "deployed": deployed, "available": available}


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
    - estimasi fee (0.09%/sisi × notional), total funding, net PnL, breakdown alasan close.
    """
    from sheet_logger import fetch_sheet_csv_rows

    rows = fetch_sheet_csv_rows(
        "Closed_Positions",
        expected_col_name="Exit Price ($)",
        local_csv_fallback=csv_path,
    )
    data_rows = []
    if rows:
        first_cell = str(rows[0][0]).strip().lower() if rows[0] else ""
        data_rows = rows[1:] if ("timestamp" in first_cell or "asset" in str(rows[0]).lower()) else rows

    trades = []
    unknown_count = 0
    seen_keys: set[str] = set()
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
        if str(r[0]).strip().lower() == "timestamp (utc)" or str(r[1]).strip().upper() == "ASSET":
            continue

        pos_id = str(r[10]).strip() if len(r) > 10 else ""
        dedupe_key = pos_id if pos_id else f"{str(r[1]).strip().upper()}|{str(r[3]).strip()}|{str(r[4]).strip()}"
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        reason_raw = str(r[9] or "").strip()
        reason_low = reason_raw.lower()
        raw_exit = str(r[5]).strip()
        raw_pnl = str(r[7]).strip()

        is_unknown = ("unknown" in reason_low) or (raw_exit == "" and raw_pnl == "")
        if is_unknown:
            unknown_count += 1
            reason_breakdown["UNKNOWN"] = reason_breakdown.get("UNKNOWN", 0) + 1
            continue

        try:
            margin = float(r[6] or 0.0)
            pnl_usd = float(raw_pnl or 0.0)
            funding_usd = float(r[8] or 0.0)
        except Exception:
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
        fee_usd = 2 * 0.0009 * notional  # 0.09%/sisi (open + close)

        trades.append({
            "asset": r[1],
            "side": r[2],
            "margin": margin,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
            "funding_usd": funding_usd,
            "fee_usd": fee_usd,
            "reason": reason_raw,
            "category": cat,
            "position_id": pos_id,
            "closed_at": str(r[11]).strip() if len(r) > 11 else "",
            "timestamp_utc": str(r[0]).strip(),
        })

    # Pembanding perubahan ekuitas dari PnL_Tracker
    equity_start: Optional[float] = None
    equity_latest: Optional[float] = None
    equity_change: Optional[float] = None
    pnl_rows = fetch_sheet_csv_rows("PnL_Tracker", local_csv_fallback=pnl_tracker_path)
    eq_vals: list[float] = []
    for pr in pnl_rows:
        if len(pr) < 2:
            continue
        if str(pr[0]).strip().lower() in ("tanggal", "date"):
            continue
        try:
            eq_vals.append(float(str(pr[1]).replace("$", "").strip()))
        except Exception:
            continue
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
        "strategy_version": get_strategy_version_hash(),
        "breakdown_by_reason": reason_breakdown,
    }

    eq_line = (
        f"Perubahan Ekuitas (PnL\\_Tracker): `${equity_change:+.4f}` (`${equity_start:.2f}` → `${equity_latest:.2f}`)"
        if equity_change is not None
        else "Perubahan Ekuitas (PnL\\_Tracker): `N/A`"
    )
    msg_lines = [
        f"*📈 Walt Perps — Closed Positions Stats* (Ver: `{stats['strategy_version']}`)",
        f"Total Trade Valid: *{total_trades}* (Win: {len(wins)} | Loss: {len(losses)}) | UNKNOWN: *{unknown_count}*",
        f"Win Rate (tanpa UNKNOWN): *{win_rate_pct:.1f}%*",
        eq_line,
        f"Avg Win: `${avg_win_usd:+.4f}` (`{avg_win_pct:+.2f}%`)",
        f"Avg Loss: `${avg_loss_usd:+.4f}` (`{avg_loss_pct:+.2f}%`)",
        f"Gross Price PnL: `${gross_pnl_usd:+.4f}`",
        f"Total Funding: `${total_funding_usd:+.4f}`",
        f"Est. Fee (0.09%/sisi): `-${estimated_fee_usd:.4f}`",
        f"Net PnL: *`${net_pnl_usd:+.4f}`*\n",
        "*Breakdown Alasan Close:*",
    ]
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
    }
    return {k: v for k, v in summary.items() if v is not None}


def _emit_cli_result(command: str, result: Any) -> None:
    safe_summary = build_safe_stdout_summary(command, result)
    try:
        os.makedirs("logs", exist_ok=True)
        with open("logs/last_safe_summary.json", "w", encoding="utf-8") as f:
            json.dump(safe_summary, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
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


def main(command: str = "scan"):
    """
    CLI entry point.

    Usage:
        python walt_bot.py scan                  # daily scan
        python walt_bot.py scan_with_research    # daily scan + AI research
        python walt_bot.py monitor               # risk monitor run
        python walt_bot.py market_execute        # execute US stocks & commodities
        python walt_bot.py crypto_execute        # execute bluechip crypto (BTC, ETH, SOL)
        python walt_bot.py stats                 # closed positions performance stats
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
            msg = f"*🛡️ Walt Risk Monitor*\n{len(result['actions'])} proteksi risiko diperbarui:\n"
            for a in result["actions"]:
                info = a.get("detail") or a.get("layer") or a.get("action")
                msg += f"• *{a['asset']}* — {info}\n"
            send_telegram(msg)
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
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    main(cmd)
