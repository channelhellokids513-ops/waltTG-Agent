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
import time
import json
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
DEFAULT_MARGIN = 2.50       # USDC per posisi
MARGIN_RANGE = (2.50, 3.00)
MIN_BUFFER_PCT = 0.15       # 15% dari saldo tidak boleh dipakai

# Filter likuiditas
MIN_VOL_24H = 1_000_000     # $1M
MIN_FUNDING_DAILY = 0.005   # 0.5%/hari

# Target income
MONTHLY_TARGET = 100.0      # USDC

# Risk management parameters (Ultra-Fast Capital Turnover — Cuan Kilat & Modal $15 Cepat Bebas)
DEFAULT_SL_PCT = 0.030       # 3.0% default SL (ketat & batasi drawdown)
DEFAULT_TP_PCT = 0.012       # 1.2% default TP (cepat hit & modal kembali cair)

# Tangga Trailing Stop & Early Breakeven (Ultra-Fast Scalp & Zero-Risk Lock)
TRAILING_TIERS = [
    # (profit_threshold, action_or_trailing_pct)
    # Tier 1: Profit >= +0.6% -> Lock Breakeven + 0.20% (Fee-cover / Zero-Risk)
    (0.006, "lock_0020"),
    # Tier 2: Profit >= +1.0% -> Lock Cuan Awal + 0.50%
    (0.010, "lock_0050"),
    # Tier 3: Profit >= +1.5% -> Lock Cuan Menengah + 1.00%
    (0.015, "lock_0100"),
    # Tier 4: Profit >= +2.0% -> Trailing Super Rapat 0.5% di bawah running price
    (0.020, 0.005),
]


def get_asset_baseline_tpsl_pct(asset: Optional[str] = None) -> tuple[float, float]:
    """
    Return (tp_pct, sl_pct) disesuaikan dengan profil volatilitas aset:
    - Target tipis & kilat agar sering kena TP, turnover cepat, dan modal $15 terus berputar:
    - Crypto Tier-1 (BTC, ETH, SOL, BNB, XRP, AVAX, SUI, LINK, NEAR, AAVE): TP +1.0%, SL 2.5%
    - ETF / Saham Kalem (SPY, US100, AAPL): TP +1.0%, SL 2.5%
    - Komoditas (XAU, XAG, BRENTOIL): TP +1.2%, SL 3.0%
    - Saham Volatil (NVDA, TSLA, MSTR, CRCL, SNDK, VVV, dll): TP +1.2%, SL 3.0%
    """
    if not asset:
        return 0.012, 0.030
    asset_up = asset.upper()
    if asset_up in {"SPY", "US100", "AAPL"}:
        return 0.010, 0.025
    elif asset_up in ALLOWED_CRYPTO:
        return 0.010, 0.025
    elif asset_up in ALLOWED_COMMODITIES:
        return 0.012, 0.030
    else:
        return 0.012, 0.030

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
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
        content = data.get("result", {}).get("content", [])
        if content and content[0].get("type") == "text":
            return json.loads(content[0]["text"])
        return data.get("result")

    # --- READ-only ---
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
) -> Research:
    """Full research bundle untuk 1 saham dengan kolaborasi Dual-AI (Qwen + Gemini)."""
    log.info(f"🔍 Researching {ticker}...")
    if qwen or gemini:
        import llm_research
        return llm_research.research_stock(ticker, qwen=qwen, gemini=gemini)
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
    - Target ultra-tipis & terukur (+1.0% s/d +1.2%) agar cepat hit TP & modal $15 terus berputar
    - Windfall Take Profit: Jika 2x funding harian realistis (>= 0.8%), TP dimajukan
    """
    tp_pct, sl_pct = get_asset_baseline_tpsl_pct(asset)

    if abs(funding_daily) >= 0.005:
        windfall_tp = round(abs(funding_daily) * 2.0, 3)
        if 0.008 <= windfall_tp < tp_pct:
            tp_pct = windfall_tp

    if side == "long":
        tp = round(entry * (1 + tp_pct), 4)
        sl = round(entry * (1 - sl_pct), 4)
    else:
        tp = round(entry * (1 - tp_pct), 4)
        sl = round(entry * (1 + sl_pct), 4)
    return tp, sl


def open_position(
    client: WaltClient,
    opp: Opportunity,
    margin: float = DEFAULT_MARGIN,
    leverage: int = MAX_LEVERAGE,
) -> Optional[dict]:
    """Preview → execute buka posisi dengan Dynamic TP/SL dan minimum order size enforcement."""
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
        if min_margin_req > DEFAULT_MARGIN * 1.5:
            log.warning(
                f"{opp.base_asset}: min margin yang disyaratkan exchange (${min_margin_req:.2f}) "
                f"melampaui batas risiko portofolio (${DEFAULT_MARGIN * 1.5:.2f}), skip"
            )
            return None
        if margin < min_margin_req:
            log.info(
                f"{opp.base_asset}: margin ${margin:.2f} disesuaikan ke batas minimum kontrak "
                f"${min_margin_req:.2f} (min_size_base={opp.min_size_base} @ ${price:.2f})"
            )
            margin = min_margin_req

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

    if not preview.get("ok"):
        risk_detail = preview.get("risk", {})
        log.error(f"{opp.base_asset}: risk check gagal: {risk_detail}")
        return None

    token = preview["confirmationToken"]
    result = client.execute(token)
    order_id = result.get("order", {}).get("order_id") if isinstance(result, dict) else None
    log.info(
        f"✅ {opp.side.upper()} {opp.base_asset} opened: margin ${margin} @ ${price:.4f}, "
        f"TP ${tp}, SL ${sl}, order_id={order_id}"
    )

    # Jika preview berhasil tanpa TP/SL, set terpisah
    if "autoClose" not in preview.get("action", {}).get("request", {}):
        time.sleep(3)
        try:
            ac_preview = client.preview_auto_close(opp.contract_id, tp=tp, sl=sl)
            if ac_preview.get("ok"):
                client.execute(ac_preview["confirmationToken"])
                log.info(f"   TP/SL set terpisah: TP=${tp}, SL=${sl}")
        except Exception as e:
            log.warning(f"   Gagal set TP/SL terpisah: {e}")

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
       - Baseline (Lapis 1)
       - Volatilitas/Drawdown >2.5% (Lapis 3)
       - Funding rendah <0.1%/hari (Lapis 2)
       - Progressive Trailing Stop / Early Zero-Risk Lock (Lapis 4)
       Pilih SL yang paling aman & menguntungkan (strictly better).
    4. Eksekusi Atomik: Kirim SATU request auto_close_set ke exchange untuk menghindari
       error 'auto_close_already_pending'.
    """
    # --- 1. Lapis 2: Funding Flip Sign → Tutup Posisi Segera ---
    if (pos.side == "long" and current_funding_daily > 0) or \
       (pos.side == "short" and current_funding_daily < 0):
        log.warning(f"[L2] {pos.base_asset}: funding flip sign → tutup posisi riil")
        preview = client.preview_close(pos.contract_id)
        if preview.get("ok"):
            res = client.execute(preview["confirmationToken"])
            return {"action": "CLOSE", "detail": "Funding Flip Sign → Close Posisi", "result": res}
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

    # --- 3. Evaluasi Target SL (Komparasi L1, L3, L2, L4) ---
    candidate_sl = None
    sl_reason = None

    # A. Kandidat dari Baseline (Lapis 1)
    if not pos.sl or _sl_is_improvement(pos, target_sl):
        candidate_sl = target_sl
        sl_reason = "L1 Baseline"

    # B. Kandidat dari Proteksi Volatilitas (Lapis 3)
    move = abs((pos.current_price - pos.entry_price) / pos.entry_price)
    profitable = pos.profit_pct_price > 0
    if move > 0.020 and not profitable:
        l3_sl = round(pos.entry_price * 0.975 if pos.side == "long" else pos.entry_price * 1.025, 4)
        if _sl_is_better(pos.side, l3_sl, candidate_sl) and _sl_is_improvement(pos, l3_sl):
            candidate_sl = l3_sl
            sl_reason = "L3 Volatility Cut"

    # C. Kandidat dari Funding Lemah <0.1% (Lapis 2)
    if abs(current_funding_daily) < 0.001:
        l2_sl = round(pos.entry_price, 4)
        if _sl_is_better(pos.side, l2_sl, candidate_sl) and _sl_is_improvement(pos, l2_sl):
            candidate_sl = l2_sl
            sl_reason = "L2 Breakeven"

    # D. Kandidat dari Trailing Stop & Early Breakeven Lock (Lapis 4)
    profit = pos.profit_pct_price
    if profit >= TRAILING_TIERS[0][0]:
        active_tier = None
        for threshold, action in TRAILING_TIERS:
            if profit >= threshold:
                active_tier = (threshold, action)
        if active_tier:
            threshold, action = active_tier
            if action in ("lock_0020", "lock_0025"):
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
    """Lapis 2: Dinamis berdasarkan funding sekarang (wrapper kompatibilitas)."""
    if (pos.side == "long" and current_funding_daily > 0) or \
       (pos.side == "short" and current_funding_daily < 0):
        preview = client.preview_close(pos.contract_id)
        if preview.get("ok"):
            return client.execute(preview["confirmationToken"])
        return None
    if abs(current_funding_daily) < 0.001:
        new_sl = pos.entry_price
        if _sl_is_improvement(pos, new_sl):
            preview = client.preview_auto_close(pos.contract_id, sl=new_sl)
            if preview.get("ok"):
                return client.execute(preview["confirmationToken"])
    return None


def apply_layer3_volatility(client: WaltClient, pos: Position) -> Optional[dict]:
    """Lapis 3: Proteksi pergerakan harga negatif (wrapper kompatibilitas)."""
    move = abs((pos.current_price - pos.entry_price) / pos.entry_price)
    profitable = pos.profit_pct_price > 0
    if move > 0.020 and not profitable:
        new_sl = round(pos.entry_price * 0.975 if pos.side == "long" else pos.entry_price * 1.025, 4)
        if _sl_is_improvement(pos, new_sl):
            preview = client.preview_auto_close(pos.contract_id, sl=new_sl)
            if preview.get("ok"):
                return client.execute(preview["confirmationToken"])
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
    if action in ("lock_0020", "lock_0025"):
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


def risk_monitor_run(client: WaltClient) -> dict:
    """
    Dispatcher utama risk monitor — jalan tiap 1 jam dari scheduled task.
    Evaluasi terpadu secara atomik untuk setiap posisi aktif.
    """
    positions_data = client.list_positions(statuses=["open"])
    contracts_data = client.list_contracts()
    contracts_by_id = {c["contract_id"]: c for c in contracts_data["contracts"]}

    actions = []
    for p_raw in positions_data.get("positions", []):
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
        )
        current_funding_daily = float(contract["funding_rate_hourly"]) * 24

        try:
            res = evaluate_and_apply_position_risk(client, pos, current_funding_daily)
            if res and res.get("action") != "FAILED":
                actions.append({
                    "asset": pos.base_asset,
                    "action": res.get("action"),
                    "detail": res.get("detail"),
                    "result": res,
                })
        except Exception as e:
            log.error(f"{pos.base_asset} evaluate_and_apply_position_risk error: {e}")

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "positions_checked": len(positions_data.get("positions", [])),
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
    top3 = opps[:3]

    # Fetch live price untuk top 3
    for opp in top3:
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

    # Income tracker
    total_funding = sum(
        float(p.get("funding_paid_quote", 0)) for p in positions.get("positions", [])
    )
    progress_pct = (total_funding / MONTHLY_TARGET) * 100

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "balance": balance.get("accounts", []),
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
            for o in top3
        ],
        "_raw_opportunities": top3,
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
    for i, opp in enumerate(summary["top_opportunities"], 1):
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
                researches[asset] = research_stock(asset, qwen=qwen, gemini=gemini)
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
        try:
            existing = client.list_positions(statuses=["open"])
            existing_contracts = {p["contract_id"] for p in existing.get("positions", [])}
        except Exception as e:
            log.warning(f"Gagal fetch existing positions: {e}")
            existing_contracts = set()

        raw_opps = summary.get("_raw_opportunities", [])
        for opp in raw_opps:
            asset = opp.base_asset
            research = researches.get(asset)
            verdict, reason, mult = decide_go_nogo(opp, research)
            margin = DEFAULT_MARGIN * mult

            if verdict in ("GO", "HALF") and mult > 0:
                if opp.contract_id in existing_contracts:
                    log.info(f"  SKIP {asset}: posisi sudah aktif")
                    executions.append({
                        "asset": asset, "side": opp.side, "verdict": "SKIP",
                        "reason": "posisi sudah aktif",
                    })
                    continue

                if deployed + margin > target_deploy or (available - deployed) < 3.0:
                    log.info(f"  SKIP {asset}: buffer saldo tercapai (${deployed:.2f}/${target_deploy:.2f})")
                    executions.append({
                        "asset": asset, "side": opp.side, "verdict": "SKIP",
                        "reason": f"buffer saldo tercapai (${deployed:.2f}/${target_deploy:.2f})",
                    })
                    continue

                if dry_run:
                    log.info(f"  🧪 DRY_RUN {opp.side.upper()} {asset} margin ${margin}")
                    executions.append({
                        "asset": asset, "side": opp.side, "margin": margin,
                        "price": getattr(opp, "live_price", 0.0),
                        "verdict": "DRY_RUN", "reason": reason,
                    })
                    deployed += margin
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
                            "asset": asset, "side": opp.side, "margin": actual_margin,
                            "price": order_price, "tp": tp, "sl": sl,
                            "verdict": "EXECUTED", "reason": reason,
                            "order_id": order_id,
                        })
                        deployed += actual_margin
                        existing_contracts.add(opp.contract_id)
                    else:
                        executions.append({
                            "asset": asset, "side": opp.side, "verdict": "FAILED",
                            "reason": "risk / min size check gagal",
                        })
                except Exception as e:
                    log.error(f"  ERROR {asset}: {e}")
                    executions.append({
                        "asset": asset, "side": opp.side, "verdict": "ERROR", "reason": str(e),
                    })
            else:
                executions.append({
                    "asset": asset, "side": opp.side, "verdict": "SKIP", "reason": reason,
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

    # Logging scan ke Google Sheet / CSV
    try:
        from sheet_logger import log_scan
        log_res = log_scan(summary, researches)
        log.info(f"📊 Logging scan result: {log_res}")
    except Exception as e:
        log.warning(f"Logging scan to sheet failed: {e}")

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

    # Research saham (hanya yang masuk shortlist 5 teratas)
    qwen = QwenClient() if os.getenv("DASHSCOPE_API_KEY") else None
    gemini = (
        GeminiClient()
        if (os.getenv("DASHSCOPE_API_GEMINI_KEY") or os.getenv("GEMINI_API_KEY"))
        else None
    )
    researches: dict[str, Research] = {}
    for opp in opps[:5]:
        if opp.base_asset in ALLOWED_STOCKS and (qwen or gemini):
            try:
                researches[opp.base_asset] = research_stock(opp.base_asset, qwen=qwen, gemini=gemini)
            except Exception as e:
                log.warning(f"research {opp.base_asset}: {e}")

    # Decide + execute
    executions = []
    deployed = 0.0
    target_deploy = available * 0.80  # sisakan 20% buffer

    for opp in opps[:5]:
        if deployed >= target_deploy:
            break
        research = researches.get(opp.base_asset)
        verdict, reason, mult = decide_go_nogo(opp, research)
        margin = DEFAULT_MARGIN * mult
        if verdict == "SKIP" or margin <= 0:
            log.info(f"  SKIP {opp.base_asset}: {reason}")
            executions.append({
                "asset": opp.base_asset, "verdict": "SKIP", "reason": reason,
            })
            continue

        if deployed + margin > target_deploy:
            log.info(f"  SKIP {opp.base_asset}: budget habis")
            continue

        # Cek apakah sudah ada posisi di kontrak ini
        try:
            existing = client.list_positions()
            if any(p["contract_id"] == opp.contract_id for p in existing.get("positions", [])):
                log.info(f"  SKIP {opp.base_asset}: posisi sudah ada")
                executions.append({
                    "asset": opp.base_asset, "verdict": "SKIP", "reason": "posisi sudah ada",
                })
                continue
        except Exception:
            pass

        log.info(f"  {verdict} {opp.side.upper()} {opp.base_asset} margin ${margin} — {reason}")
        if dry_run:
            executions.append({
                "asset": opp.base_asset, "side": opp.side, "margin": margin,
                "verdict": "DRY_RUN", "reason": reason,
            })
            deployed += margin
            continue

        try:
            result = open_position(client, opp, margin=margin, leverage=MAX_LEVERAGE)
            if result:
                actual_margin = result.get("actual_margin", margin) if isinstance(result, dict) else margin
                order_id = result.get("order", {}).get("order_id") if isinstance(result, dict) else None
                order_price = result.get("price") or getattr(opp, "live_price", 0.0)
                tp = result.get("tp")
                sl = result.get("sl")
                executions.append({
                    "asset": opp.base_asset, "side": opp.side, "margin": actual_margin,
                    "price": order_price, "tp": tp, "sl": sl,
                    "verdict": "EXECUTED", "reason": reason,
                    "order_id": order_id,
                })
                deployed += actual_margin
            else:
                log.warning(f"  FAILED {opp.base_asset}: preview / risk check gagal")
                executions.append({
                    "asset": opp.base_asset, "verdict": "FAILED",
                    "reason": f"risk check / min size check gagal (verdict={verdict})",
                })
        except Exception as e:
            log.error(f"  ERROR {opp.base_asset}: {e}")
            executions.append({
                "asset": opp.base_asset, "verdict": "ERROR", "reason": str(e),
            })

    # Report
    executed_list = [e for e in executions if e["verdict"] == "EXECUTED"]
    skipped_list = [e for e in executions if e["verdict"] in ("SKIP", "FAILED", "ERROR")]

    msg_lines = [
        f"*{title}*",
        f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC_\n",
        f"Saldo awal: `${available:.2f}` | Deploy: `${deployed:.2f}`",
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


# =============================================================================
# MAIN DISPATCHER
# =============================================================================

def main(command: str = "scan"):
    """
    CLI entry point.

    Usage:
        python walt_bot.py scan                  # daily scan
        python walt_bot.py scan_with_research    # daily scan + AI research
        python walt_bot.py monitor               # risk monitor run
        python walt_bot.py market_execute        # execute US stocks & commodities
        python walt_bot.py crypto_execute        # execute bluechip crypto (BTC, ETH, SOL)
        python walt_bot.py open NBIS short 2.50  # buka posisi manual
        python walt_bot.py close NBIS            # tutup posisi
    """
    import sys

    client = WaltClient()

    if command == "scan":
        result = daily_scan(client)
        print(json.dumps(result, indent=2, default=str))

    elif command == "scan_with_research":
        dry = os.getenv("DRY_RUN", "false").lower() == "true"
        auto_exec = os.getenv("AUTO_EXECUTE_ON_SCAN", "true").lower() == "true"
        result = scan_with_research(client, auto_execute=auto_exec, dry_run=dry)
        print(json.dumps(result, indent=2, default=str))

    elif command == "monitor":
        result = risk_monitor_run(client)
        # Send Telegram kalau ada aksi
        if result.get("actions"):
            msg = f"*🛡️ Walt Risk Monitor*\n{len(result['actions'])} proteksi risiko diperbarui:\n"
            for a in result["actions"]:
                info = a.get("detail") or a.get("layer") or a.get("action")
                msg += f"• *{a['asset']}* — {info}\n"
            send_telegram(msg)
        print(json.dumps(result, indent=2, default=str))

    elif command in ("market_execute", "execute"):
        dry = os.getenv("DRY_RUN", "false").lower() == "true"
        result = market_execute(client, dry_run=dry)
        print(json.dumps(result, indent=2, default=str))

    elif command == "crypto_execute":
        dry = os.getenv("DRY_RUN", "false").lower() == "true"
        result = crypto_execute(client, dry_run=dry)
        print(json.dumps(result, indent=2, default=str))

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
