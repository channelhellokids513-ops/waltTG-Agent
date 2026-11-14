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
from llm_research import QwenClient, research_stock, Research
from notifier import send_telegram


# =============================================================================
# KONFIGURASI
# =============================================================================

WALT_MCP_URL = "https://walletbot.me/bot-relay/public-api/mcp"
# OAuth via TokenManager — butuh env WALT_CLIENT_ID + WALT_REFRESH_TOKEN

# Universe aset yang diperbolehkan (saham US + komoditas)
ALLOWED_STOCKS = {
    "NVDA", "NBIS", "CRCL", "MSTR", "AAPL", "GOOGL", "AMD", "PLTR",
    "COIN", "SNDK", "SPY", "US100", "AMZN", "TSLA", "META", "LIT", "VVV",
    "ANTHROPIC",
}
ALLOWED_COMMODITIES = {"XAU", "XAG", "BRENTOIL"}
ALLOWED_ASSETS = ALLOWED_STOCKS | ALLOWED_COMMODITIES

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

# Risk management parameters
BASELINE_SL_PCT = 0.07      # 7% dari entry
BASELINE_TP_PCT = 0.15      # 15% dari entry

# Lapis 4 (trailing stop)
TRAILING_TIERS = [
    # (profit_threshold, trailing_pct) — profit=unrealized profit sebagai % dari entry
    (0.03, None),   # ≥3%: breakeven (SL ke entry)
    (0.05, 0.025),  # ≥5%: trailing 2.5%
    (0.10, 0.015),  # ≥10%: trailing 1.5%
    (0.20, 0.010),  # ≥20%: trailing 1.0%
]

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
    min_margin_2x: float   # minimum margin efektif di lev 2x

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

def rank_opportunities(contracts: list[dict]) -> list[Opportunity]:
    """Filter + rank kontrak likuid sesuai policy."""
    opps = []
    for c in contracts:
        asset = c["base_asset"]
        if asset not in ALLOWED_ASSETS:
            continue
        vol = float(c["volume_24h_usd"])
        if vol < MIN_VOL_24H:
            continue
        fr_hourly = float(c["funding_rate_hourly"])
        fr_daily = fr_hourly * 24
        if abs(fr_daily) < MIN_FUNDING_DAILY:
            continue

        # Minimum margin efektif di 2x (perlu latest price untuk akurat;
        # di sini perkiraan dari min_size_base × asumsi price)
        min_base = float(c["min_size_base"])
        slip = float(c["price_slippage"])
        # Approximation: butuh price dari latest_rate untuk exact
        min_margin_2x = min_base * 100 * (1 + slip) / MAX_LEVERAGE  # placeholder

        side: Literal["long", "short"] = "short" if fr_daily > 0 else "long"
        opps.append(Opportunity(
            contract_id=c["contract_id"],
            base_asset=asset,
            funding_daily=fr_daily,
            volume_24h=vol,
            max_leverage=c["max_leverage"],
            side=side,
            min_margin_2x=min_margin_2x,
        ))
    # Rank by absolute funding, descending
    opps.sort(key=lambda o: abs(o.funding_daily), reverse=True)
    return opps


# =============================================================================
# ENTRY
# =============================================================================

# =============================================================================
# RESEARCH LAYER — fundamentals & news check untuk saham
# =============================================================================

@dataclass
class Research:
    ticker: str
    days_to_earnings: Optional[int]  # None kalau tidak ketemu
    news_sentiment: Literal["bullish", "bearish", "neutral", "unknown"]
    narrative: str  # 1-2 kalimat ringkas
    fetched_at: str


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


def research_stock(ticker: str, qwen: Optional[QwenClient] = None) -> Research:
    """Full research bundle untuk 1 saham."""
    log.info(f"🔍 Researching {ticker}...")
    if qwen:
        import llm_research
        return llm_research.research_stock(ticker, qwen)
    days = check_earnings_date(ticker)
    sentiment, narrative = get_news_sentiment(ticker)
    return Research(
        ticker=ticker,
        days_to_earnings=days,
        news_sentiment=sentiment,  # type: ignore
        narrative=narrative,
        confidence=0.5,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def decide_go_nogo(
    opp: Opportunity,
    research: Optional[Research],
) -> tuple[Literal["GO", "HALF", "SKIP"], str, float]:
    """
    Apply decision matrix.
    Return (verdict, reason, margin_multiplier).
    margin_multiplier = 1.0 (full) atau 0.5 (half) atau 0 (skip).
    """
    # Minimum funding threshold
    if abs(opp.funding_daily) < MIN_FUNDING_DAILY:
        return "SKIP", "funding kurang (<0.5%/hari)", 0.0

    # Komoditas / token: skip research, langsung GO
    if opp.base_asset in ALLOWED_COMMODITIES or opp.base_asset not in ALLOWED_STOCKS:
        return "GO", "komoditas/non-saham, no research needed", 1.0

    # Saham perlu research
    if research is None:
        return "HALF", "no research data (fallback)", 0.5

    # Earnings risk
    if research.days_to_earnings is not None and research.days_to_earnings <= 7:
        return "SKIP", f"earnings H-{research.days_to_earnings}", 0.0

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
        return "GO", f"{research.news_sentiment} news aligns with {opp.side.upper()}", 1.0
    if opposite:
        return "SKIP", f"{research.news_sentiment} news bertolak belakang dengan {opp.side.upper()}", 0.0
    # neutral
    return "HALF", "neutral sentiment → half size", 0.5


# =============================================================================
# ENTRY (continued)
# =============================================================================

def compute_baseline_tpsl(side: str, entry: float) -> tuple[float, float]:
    """Return (tp, sl) baseline untuk posisi baru."""
    if side == "long":
        tp = round(entry * (1 + BASELINE_TP_PCT), 4)
        sl = round(entry * (1 - BASELINE_SL_PCT), 4)
    else:
        tp = round(entry * (1 - BASELINE_TP_PCT), 4)
        sl = round(entry * (1 + BASELINE_SL_PCT), 4)
    return tp, sl


def open_position(
    client: WaltClient,
    opp: Opportunity,
    margin: float = DEFAULT_MARGIN,
    leverage: int = MAX_LEVERAGE,
) -> Optional[dict]:
    """Preview → execute buka posisi dengan TP/SL baseline."""
    market_id = f"{opp.base_asset}-USD-PERP"
    rate_resp = client.latest_rate(market_id)
    price = float(rate_resp["rate"])
    if price <= 0:
        log.warning(f"{opp.base_asset}: price invalid ({price}), skip")
        return None

    tp, sl = compute_baseline_tpsl(opp.side, price)

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
        log.error(f"{opp.base_asset}: risk check gagal: {preview.get('risk')}")
        return None

    token = preview["confirmationToken"]
    result = client.execute(token)
    order_id = result.get("order", {}).get("order_id")
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

    return result


# =============================================================================
# RISK MONITOR — 4 LAPIS
# =============================================================================

def apply_layer1_baseline(client: WaltClient, pos: Position) -> Optional[dict]:
    """Lapis 1: Set TP/SL baseline kalau belum ada."""
    if pos.tp and pos.sl:
        return None
    tp, sl = compute_baseline_tpsl(pos.side, pos.entry_price)
    new_tp = tp if not pos.tp else None
    new_sl = sl if not pos.sl else None
    log.info(f"[L1] {pos.base_asset}: set baseline TP={new_tp}, SL={new_sl}")
    preview = client.preview_auto_close(pos.contract_id, tp=new_tp, sl=new_sl)
    if preview.get("ok"):
        return client.execute(preview["confirmationToken"])
    return None


def apply_layer2_funding(
    client: WaltClient, pos: Position, current_funding_daily: float
) -> Optional[dict]:
    """Lapis 2: Dinamis berdasarkan funding sekarang."""
    # Funding flip sign → close
    if (pos.side == "long" and current_funding_daily > 0) or \
       (pos.side == "short" and current_funding_daily < 0):
        log.warning(f"[L2] {pos.base_asset}: funding flip sign → tutup posisi")
        preview = client.preview_close(pos.contract_id)
        if preview.get("ok"):
            return client.execute(preview["confirmationToken"])
        return None

    # Funding hampir hilang → SL ke breakeven
    if abs(current_funding_daily) < 0.001:  # 0.1%/hari
        new_sl = pos.entry_price
        if _sl_is_improvement(pos, new_sl):
            log.info(f"[L2] {pos.base_asset}: funding <0.1% → SL ke breakeven ${new_sl}")
            preview = client.preview_auto_close(pos.contract_id, sl=new_sl)
            if preview.get("ok"):
                return client.execute(preview["confirmationToken"])
    return None


def apply_layer3_volatility(client: WaltClient, pos: Position) -> Optional[dict]:
    """Lapis 3: Pergerakan harga signifikan."""
    move = abs((pos.current_price - pos.entry_price) / pos.entry_price)
    profitable = pos.profit_pct_price > 0

    if move > 0.05 and profitable:
        # Profit >5%: lock dengan geser SL ke breakeven + 1%
        if pos.side == "long":
            new_sl = pos.entry_price * 1.01
        else:
            new_sl = pos.entry_price * 0.99
        if _sl_is_improvement(pos, new_sl):
            log.info(f"[L3] {pos.base_asset}: profit >5% → SL lock ${new_sl:.4f}")
            preview = client.preview_auto_close(pos.contract_id, sl=round(new_sl, 4))
            if preview.get("ok"):
                return client.execute(preview["confirmationToken"])

    if move > 0.03 and not profitable:
        # Loss >3%: perketat SL ke entry ± 5%
        if pos.side == "long":
            new_sl = pos.entry_price * 0.95
        else:
            new_sl = pos.entry_price * 1.05
        if _sl_is_improvement(pos, new_sl):
            log.info(f"[L3] {pos.base_asset}: loss >3% → SL diperketat ${new_sl:.4f}")
            preview = client.preview_auto_close(pos.contract_id, sl=round(new_sl, 4))
            if preview.get("ok"):
                return client.execute(preview["confirmationToken"])
    return None


def apply_layer4_trailing(client: WaltClient, pos: Position) -> Optional[dict]:
    """Lapis 4: Trailing stop progresif."""
    profit = pos.profit_pct_price  # fraction, bisa negatif
    if profit < TRAILING_TIERS[0][0]:
        return None

    # Cari tier tertinggi yang sudah dicapai
    active_tier = None
    for threshold, trailing_pct in TRAILING_TIERS:
        if profit >= threshold:
            active_tier = (threshold, trailing_pct)

    if active_tier is None:
        return None

    threshold, trailing_pct = active_tier
    if trailing_pct is None:
        # Breakeven lock
        new_sl = pos.entry_price
    else:
        if pos.side == "long":
            new_sl = pos.current_price * (1 - trailing_pct)
        else:
            new_sl = pos.current_price * (1 + trailing_pct)

    new_sl = round(new_sl, 4)
    if not _sl_is_improvement(pos, new_sl, buffer=0.001):
        return None

    log.info(
        f"[L4] {pos.base_asset}: profit {profit*100:.1f}% ≥ {threshold*100:.0f}% "
        f"→ trailing SL ke ${new_sl}"
    )
    preview = client.preview_auto_close(pos.contract_id, sl=new_sl)
    if preview.get("ok"):
        return client.execute(preview["confirmationToken"])
    return None


def _sl_is_improvement(pos: Position, new_sl: float, buffer: float = 0.001) -> bool:
    """SL hanya bergerak ke arah menguntungkan + melewati buffer."""
    if pos.sl is None:
        return True
    if pos.side == "long":
        # LONG: SL yang lebih tinggi = lebih bagus
        return new_sl > pos.sl * (1 + buffer)
    else:
        # SHORT: SL yang lebih rendah = lebih bagus
        return new_sl < pos.sl * (1 - buffer)


def risk_monitor_run(client: WaltClient) -> dict:
    """
    Dispatcher utama risk monitor — jalan tiap 4 jam dari scheduled task.
    Return: summary aksi yang dilakukan.
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

        # Apply lapis secara berurutan; stop kalau ada close (Lapis 2)
        for fn, args in [
            (apply_layer2_funding, (client, pos, current_funding_daily)),  # prioritas tinggi
            (apply_layer1_baseline, (client, pos)),
            (apply_layer4_trailing, (client, pos)),
            (apply_layer3_volatility, (client, pos)),
        ]:
            try:
                result = fn(*args)
                if result:
                    actions.append({"asset": pos.base_asset, "layer": fn.__name__, "result": result})
                    # Jika Lapis 2 close, re-fetch position (akan hilang)
                    if fn == apply_layer2_funding and "funding_rate" in str(result):
                        break
            except Exception as e:
                log.error(f"{pos.base_asset} {fn.__name__} error: {e}")

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
                "asset": o.base_asset,
                "side": o.side,
                "funding_daily_pct": o.funding_daily * 100,
                "price": getattr(o, "live_price", 0),
                "min_margin_2x": o.min_margin_2x,
            }
            for o in top3
        ],
        "readiness": readiness,
    }


# =============================================================================
# SCAN WITH RESEARCH — daily scan + Qwen analysis + Telegram notif
# =============================================================================

def format_summary_telegram(summary: dict, researches: dict[str, Research]) -> str:
    """Format summary jadi Telegram-ready markdown."""
    lines = []
    lines.append("*📊 Walt Perps — Daily Scan*")
    lines.append(f"_{summary['ts'][:16].replace('T', ' ')} UTC_\n")

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

    # Top opportunities + research
    lines.append(f"\n*🎯 Top 3 Peluang (lev 2x, margin $2.50)*")
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
        emoji = {"GO": "✅", "HALF": "⚠️", "SKIP": "❌"}[verdict]

        lines.append(f"\n{i}. {emoji} *{side} {asset}* @ `${price:.4f}`")
        lines.append(f"   Funding: `{funding:+.3f}%/hari`")
        if research:
            days = research.days_to_earnings
            earn_str = f"{days}d" if days is not None else "—"
            lines.append(f"   Earnings: {earn_str} | Sentiment: {research.news_sentiment}")
            if research.narrative:
                lines.append(f"   _{research.narrative[:150]}_")
        lines.append(f"   → {verdict}: {reason}")

    lines.append(f"\n_Fee taker 0.09%/sisi | Max lev 2x | Fokus saham+komoditas_")
    return "\n".join(lines)


def scan_with_research(client: WaltClient) -> dict:
    """Daily scan + Qwen research + Telegram notif."""
    log.info("📊 Running daily scan + research...")
    summary = daily_scan(client)

    # Research tiap stock candidate
    qwen = QwenClient() if os.getenv("DASHSCOPE_API_KEY") else None
    researches: dict[str, Research] = {}

    for opp in summary["top_opportunities"]:
        asset = opp["asset"]
        if asset in ALLOWED_STOCKS and qwen:
            try:
                researches[asset] = research_stock(asset, qwen)
                log.info(f"  ✓ research {asset}: {researches[asset].news_sentiment}")
            except Exception as e:
                log.warning(f"  ✗ research {asset} failed: {e}")

    # Format & send
    msg = format_summary_telegram(summary, researches)
    sent = send_telegram(msg)
    log.info(f"Telegram sent: {sent}")

    return {"summary": summary, "researches": {k: v.__dict__ for k, v in researches.items()}}


# =============================================================================
# MARKET EXECUTE — buka posisi saham saat US market open
# =============================================================================

def market_execute(client: WaltClient, dry_run: bool = False) -> dict:
    """Eksekusi posisi baru saat US stock market buka.
    
    Flow:
      1. Check balance
      2. Get ranked opportunities
      3. Research stocks → decide GO/HALF/SKIP
      4. Execute top 2 (atau sesuai saldo)
      5. Report via Telegram
    """
    log.info("🚀 Market execute...")
    balance_data = client.balance()
    accounts = balance_data.get("accounts", [])
    if not accounts:
        send_telegram("⚠️ *Walt Execute SKIP* — saldo kosong, deposit USDC dulu.")
        return {"status": "no_balance"}

    available = float(accounts[0]["balance_available"])
    log.info(f"Balance available: ${available}")
    if available < 3.0:
        send_telegram(f"⚠️ *Walt Execute SKIP* — saldo terlalu kecil (${available:.2f} < $3).")
        return {"status": "insufficient_balance", "balance": available}

    # Scan + rank
    contracts_data = client.list_contracts()
    opps = rank_opportunities(contracts_data["contracts"])

    # Research saham (hanya yang masuk shortlist 5 teratas)
    qwen = QwenClient() if os.getenv("DASHSCOPE_API_KEY") else None
    researches: dict[str, Research] = {}
    for opp in opps[:5]:
        if opp.base_asset in ALLOWED_STOCKS and qwen:
            try:
                researches[opp.base_asset] = research_stock(opp.base_asset, qwen)
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
                executions.append({
                    "asset": opp.base_asset, "side": opp.side, "margin": margin,
                    "verdict": "EXECUTED", "reason": reason,
                    "order_id": result.get("order", {}).get("order_id"),
                })
                deployed += margin
        except Exception as e:
            log.error(f"  ERROR {opp.base_asset}: {e}")
            executions.append({
                "asset": opp.base_asset, "verdict": "ERROR", "reason": str(e),
            })

    # Report
    executed_list = [e for e in executions if e["verdict"] == "EXECUTED"]
    skipped_list = [e for e in executions if e["verdict"] == "SKIP"]

    msg_lines = [
        "*🚀 Walt Market Execute*",
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
    return {"executions": executions, "deployed": deployed, "available": available}


# =============================================================================
# MAIN DISPATCHER
# =============================================================================

def main(command: str = "scan"):
    """
    CLI entry point.

    Usage:
        python walt_bot.py scan           # daily scan
        python walt_bot.py monitor        # risk monitor run
        python walt_bot.py open NBIS short 2.50  # buka posisi manual
        python walt_bot.py close NBIS     # tutup posisi
    """
    import sys

    client = WaltClient()

    if command == "scan":
        result = daily_scan(client)
        print(json.dumps(result, indent=2, default=str))

    elif command == "scan_with_research":
        result = scan_with_research(client)
        print(json.dumps(result, indent=2, default=str))

    elif command == "monitor":
        result = risk_monitor_run(client)
        # Send Telegram kalau ada aksi
        if result.get("actions"):
            msg = f"*🛡️ Walt Risk Monitor*\n{len(result['actions'])} aksi dilakukan:\n"
            for a in result["actions"]:
                msg += f"• {a['asset']} — {a['layer']}\n"
            send_telegram(msg)
        print(json.dumps(result, indent=2, default=str))

    elif command == "market_execute":
        dry = os.getenv("DRY_RUN", "false").lower() == "true"
        result = market_execute(client, dry_run=dry)
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
            min_margin_2x=0,
        )
        open_position(client, opp, margin=margin)

    else:
        print(f"Unknown command: {command}")


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    main(cmd)
