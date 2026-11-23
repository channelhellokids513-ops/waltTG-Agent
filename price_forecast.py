"""
Modul Deteksi Selisih Harga 24 Jam & Perkiraan Harga 24 Jam Ke Depan (3-Pillar Ensemble).

Dirancang sebagai fondasi Engine 2 (Directional Breakout + Mean-Reversion Fade) untuk
Crypto 24/7 dan Saham AS (jam reguler UTC), 100% kausal (tanpa lookahead).

Tiga Pilar Perkiraan 24 Jam:
  1. Statistical Drift & Momentum (35%): EMA(21/55), ROC(4h/12h/24h), deviasi VWAP 24h, z_move_24h ATR.
  2. Structural Liquidity (`market_gravity`, 35%): net_gravity, jarak ke Comfort Point, serta sinyal
     Rejected / Accepted pada magnet likuiditas.
  3. Derivatives & AI Catalyst (30%, opsional): funding_daily, ai_score Swarm (P1-P7), dan veto P5 >= 0.70.
     Bila input Pilar 3 tidak tersedia (misal pada backtest historis murni OHLC), bobot Pilar 1 & 2
     otomatis direnormalisasi menjadi 50% / 50%.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import statistics
import time
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Sequence

from market_chart import compute_ema, compute_rsi
from market_gravity import GravitySnapshot, analyze_market_gravity, comfort_points

CRYPTO_SYMBOLS: frozenset[str] = frozenset({
    "BTC", "ETH", "SOL", "BNB", "XRP", "AVAX", "SUI", "LINK", "NEAR", "AAVE",
    "ARB", "OP", "DOGE", "ADA", "DOT", "ATOM", "LTC", "UNI", "APT", "SEI", "HYPE",
})
COMMODITY_SYMBOLS: frozenset[str] = frozenset({"XAU", "XAG", "BRENTOIL"})
NON_CRYPTO_SYMBOLS: frozenset[str] = frozenset({
    # Saham AS, ETF, Indeks, & Pre-IPO
    "NVDA", "NBIS", "CRCL", "MSTR", "AAPL", "GOOGL", "AMD", "PLTR", "COIN", "SNDK",
    "SPY", "QQQ", "US100", "SPX", "NDX", "DJI", "VIX", "DXY", "AMZN", "TSLA", "META",
    "LIT", "VVV", "ANTHROPIC", "MSFT", "NFLX", "ORCL", "INTC", "MU", "ARM", "SMCI",
    "UBER", "HOOD", "GME", "AMC", "RIVN", "DIS",
    # Komoditas & Logam
    "XAU", "XAG", "BRENTOIL", "WTI", "USOIL", "GOLD", "SILVER", "OIL", "COPPER",
    "NATGAS", "PAXG", "XAUT",
    # Forex & Stablecoin
    "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "TRY", "BRL",
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "USDE", "USDD", "PYUSD",
})
_NON_CRYPTO_META_HINTS: tuple[str, ...] = (
    "stock", "equity", "equities", "commodity", "commodities", "forex", "fx",
    "index", "indices", "etf", "pre_ipo", "rwa", "metal", "energy", "fiat",
)

FORECAST_MAX_SCAN_ASSETS = 50
FORECAST_MAX_SIGNALS = 5

DEFAULT_ROUND_TRIP_COST_PCT = 0.0028  # 0.18% round-trip taker + 0.10% floor slippage terukur
DEFAULT_MIN_NET_EDGE_PCT = 0.0080     # +0.80% minimal net expected edge 24h


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(val)))


def infer_asset_class(asset: str) -> Literal["crypto", "commodity", "stock"]:
    sym = str(asset or "").strip().upper()
    if sym in CRYPTO_SYMBOLS:
        return "crypto"
    if sym in COMMODITY_SYMBOLS:
        return "commodity"
    return "stock"


def bars_per_trading_day(interval: str = "1h", asset_class: str = "crypto") -> int:
    iv = str(interval or "1h").strip().lower()
    ac = str(asset_class or "crypto").strip().lower()
    is_stock = ac == "stock"
    if iv == "1d":
        return 1
    if iv == "4h":
        return 2 if is_stock else 6
    if iv == "15m":
        return 26 if is_stock else 96
    return 7 if is_stock else 24


def _wilder_atr(candles: Sequence[Mapping[str, Any]], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(candles)):
        h = float(candles[i]["h"])
        l = float(candles[i]["l"])
        prev_c = float(candles[i - 1]["c"])
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = ((atr * (period - 1)) + tr) / period
    return atr if math.isfinite(atr) and atr > 0 else None


@dataclass(frozen=True)
class PriceDelta24h:
    asset: str
    asset_class: str
    interval: str
    bars_24h: int
    close: float
    price_24h_ago: float
    ret_24h_pct: float
    high_24h: float
    low_24h: float
    range_24h_pct: float
    range_pos_24h: float
    atr_14_pct: float
    atr_24h_pct: float
    z_move_24h: float
    vol_ratio_24h: float
    ema_fast: float
    ema_slow: float
    rsi_14: float
    vwap_24h: float
    vwap_dev_pct: float
    roc_4h_pct: float
    roc_12h_pct: float


@dataclass(frozen=True)
class Forecast24hResult:
    asset: str
    asset_class: str
    setup: Literal["MOMENTUM_BREAKOUT", "MEAN_REVERSION_FADE", "NONE"]
    side: Literal["long", "short", "neutral"]
    tradeable: bool
    confidence: float
    expected_move_24h_pct: float
    expected_gross_24h_pct: float
    round_trip_cost_pct: float
    adverse_funding_24h_pct: float
    net_edge_24h_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    forecast_score: float
    pillar_momentum: float
    pillar_gravity: float
    pillar_catalyst: Optional[float]
    delta_24h: PriceDelta24h
    comfort_price: float
    mg_net_gravity: float
    rejected_up: bool
    rejected_down: bool
    reason: str


def compute_price_delta_24h(
    asset: str,
    candles: Sequence[Mapping[str, Any]],
    interval: str = "1h",
    asset_class: Optional[str] = None,
) -> Optional[PriceDelta24h]:
    """Hitung metrik selisih harga 24 jam ternormalisasi ATR secara kausal dari deret candle."""
    ac = asset_class or infer_asset_class(asset)
    bars_24h = bars_per_trading_day(interval, ac)
    min_bars = max(60, bars_24h + 20)
    if not candles or len(candles) < min_bars:
        return None

    close = float(candles[-1]["c"])
    price_24h_ago = float(candles[-1 - bars_24h]["c"])
    if close <= 0 or price_24h_ago <= 0 or not (math.isfinite(close) and math.isfinite(price_24h_ago)):
        return None

    closes = [float(c["c"]) for c in candles]
    if any(not math.isfinite(x) or x <= 0 for x in closes):
        return None

    ret_24h = (close - price_24h_ago) / price_24h_ago
    window_24h = candles[-bars_24h:]
    high_24h = max(float(c["h"]) for c in window_24h)
    low_24h = min(float(c["l"]) for c in window_24h)
    range_24h_pct = (high_24h - low_24h) / close if close > 0 else 0.0
    span_24h = high_24h - low_24h
    range_pos_24h = ((close - low_24h) / span_24h) if span_24h > 0 else 0.5

    atr_14 = _wilder_atr(candles, period=14)
    if atr_14 is None or atr_14 <= 0:
        return None
    atr_14_pct = atr_14 / close

    day_ranges: list[float] = []
    idx = len(candles)
    while idx - bars_24h >= 1 and len(day_ranges) < 14:
        chunk = candles[idx - bars_24h : idx]
        c_hi = max(float(c["h"]) for c in chunk)
        c_lo = min(float(c["l"]) for c in chunk)
        c_prev = float(candles[idx - bars_24h - 1]["c"])
        c_end = float(chunk[-1]["c"])
        if c_end > 0 and c_prev > 0:
            tr_day = max(c_hi - c_lo, abs(c_hi - c_prev), abs(c_lo - c_prev))
            day_ranges.append(tr_day / c_end)
        idx -= bars_24h

    if len(day_ranges) >= 5:
        atr_24h_pct = max(1e-4, float(statistics.median(day_ranges)))
    else:
        atr_24h_pct = max(1e-4, atr_14_pct * math.sqrt(max(1, bars_24h)))

    z_move_24h = ret_24h / atr_24h_pct

    # Volume ratio 24 jam dibanding rata-rata hingga 20 blok 24h sebelumnya
    cur_vol = sum(max(0.0, float(c.get("v") or 0.0)) for c in window_24h)
    hist_vols: list[float] = []
    v_end = len(candles) - bars_24h
    while v_end - bars_24h >= 0 and len(hist_vols) < 20:
        v_chunk = sum(max(0.0, float(c.get("v") or 0.0)) for c in candles[v_end - bars_24h : v_end])
        if v_chunk > 0:
            hist_vols.append(v_chunk)
        v_end -= bars_24h
    if cur_vol > 0 and hist_vols:
        avg_hist_vol = sum(hist_vols) / len(hist_vols)
        vol_ratio_24h = (cur_vol / avg_hist_vol) if avg_hist_vol > 0 else 1.0
    else:
        vol_ratio_24h = 1.0

    ema_fast_series = compute_ema(closes, 21)
    ema_slow_series = compute_ema(closes, 55)
    ema_fast = float(ema_fast_series[-1])
    ema_slow = float(ema_slow_series[-1])
    rsi_val = compute_rsi(closes, period=14)
    rsi_14 = float(rsi_val) if rsi_val is not None else 50.0

    # VWAP 24 jam (atau rata-rata typical price bila volume tidak tersedia)
    typ_prices = [(float(c["h"]) + float(c["l"]) + float(c["c"])) / 3.0 for c in window_24h]
    vols = [max(0.0, float(c.get("v") or 0.0)) for c in window_24h]
    total_v = sum(vols)
    if total_v > 0:
        vwap_24h = sum(tp * v for tp, v in zip(typ_prices, vols)) / total_v
    else:
        vwap_24h = sum(typ_prices) / len(typ_prices)
    vwap_dev_pct = ((close - vwap_24h) / vwap_24h) if vwap_24h > 0 else 0.0

    bars_4h = max(1, round(bars_24h / 6))
    bars_12h = max(1, round(bars_24h / 2))
    p_4h = float(candles[-1 - min(bars_4h, len(candles) - 1)]["c"])
    p_12h = float(candles[-1 - min(bars_12h, len(candles) - 1)]["c"])
    roc_4h_pct = ((close - p_4h) / p_4h) if p_4h > 0 else 0.0
    roc_12h_pct = ((close - p_12h) / p_12h) if p_12h > 0 else 0.0

    return PriceDelta24h(
        asset=str(asset).strip().upper(),
        asset_class=ac,
        interval=interval,
        bars_24h=bars_24h,
        close=close,
        price_24h_ago=price_24h_ago,
        ret_24h_pct=ret_24h,
        high_24h=high_24h,
        low_24h=low_24h,
        range_24h_pct=range_24h_pct,
        range_pos_24h=_clamp(range_pos_24h, 0.0, 1.0),
        atr_14_pct=atr_14_pct,
        atr_24h_pct=atr_24h_pct,
        z_move_24h=z_move_24h,
        vol_ratio_24h=vol_ratio_24h,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        rsi_14=rsi_14,
        vwap_24h=vwap_24h,
        vwap_dev_pct=vwap_dev_pct,
        roc_4h_pct=roc_4h_pct,
        roc_12h_pct=roc_12h_pct,
    )


def _resolve_gravity_metrics(
    candles: Sequence[Mapping[str, Any]],
    close: float,
    atr_24h_pct: float,
    gravity_snapshot: Optional[GravitySnapshot],
) -> tuple[float, float, float, bool, bool]:
    """Ekstrak (pillar_gravity, comfort_price, mg_net_gravity, rejected_up, rejected_down)."""
    snap = gravity_snapshot
    if snap is None:
        n = len(candles)
        res = analyze_market_gravity(candles, trace_every=max(1, n - 1))
        if not res.usable:
            return 0.0, close, 0.0, False, False
        if res.trace and res.trace[-1].bar_index == n - 1:
            snap = res.trace[-1]
        else:
            # Fallback bila warm-up pendek
            grav_dir = math.tanh(float(res.net_gravity) / 50.0)
            return _clamp(0.55 * grav_dir, -1.0, 1.0), close, float(res.net_gravity), False, False

    comfort = comfort_points(snap)
    comfort_price = float(comfort.get("structural", close))
    mg_net = float(getattr(snap, "net_gravity", 0.0) or 0.0)
    rejected_up = bool(getattr(snap, "rejected_up", None))
    rejected_down = bool(getattr(snap, "rejected_down", None))

    grav_dir = math.tanh(mg_net / 50.0)
    comfort_dist_pct = ((comfort_price - close) / close) if close > 0 else 0.0
    comfort_pull = math.tanh(comfort_dist_pct / max(atr_24h_pct, 1e-4)) if math.isfinite(comfort_dist_pct) else 0.0

    rej_push = 0.0
    if rejected_up and not rejected_down:
        rej_push = -0.55
    elif rejected_down and not rejected_up:
        rej_push = +0.55

    pillar_gravity = _clamp((0.45 * grav_dir) + (0.35 * comfort_pull) + rej_push, -1.0, 1.0)
    return pillar_gravity, comfort_price, mg_net, rejected_up, rejected_down


def forecast_24h(
    asset: str,
    candles: Sequence[Mapping[str, Any]],
    *,
    interval: str = "1h",
    asset_class: Optional[str] = None,
    funding_daily: Optional[float] = None,
    ai_score: Optional[float] = None,
    p5_risk: float = 0.0,
    round_trip_cost_pct: float = DEFAULT_ROUND_TRIP_COST_PCT,
    min_net_edge_pct: float = DEFAULT_MIN_NET_EDGE_PCT,
    sl_mode: Literal["atr", "fixed_15"] = "atr",
    gravity_snapshot: Optional[GravitySnapshot] = None,
    enabled_setups: tuple[str, ...] = ("MOMENTUM_BREAKOUT", "MEAN_REVERSION_FADE"),
) -> Optional[Forecast24hResult]:
    """Perkirakan arah, potensi gerak bersih 24 jam, serta kelayakan entry Engine 2."""
    delta = compute_price_delta_24h(asset, candles, interval=interval, asset_class=asset_class)
    if delta is None:
        return None

    close = delta.close
    # Pilar 1: Statistical Drift & Momentum
    ema_diff = delta.ema_fast - delta.ema_slow
    ema_mag = min(1.0, abs(ema_diff) / max(close * delta.atr_14_pct, 1e-6))
    ema_trend = ema_mag if ema_diff >= 0 else -ema_mag
    roc_blend = math.tanh(
        (0.40 * delta.roc_4h_pct + 0.35 * delta.roc_12h_pct + 0.25 * delta.ret_24h_pct)
        / max(delta.atr_24h_pct, 1e-4)
    )
    vwap_sig = math.tanh(delta.vwap_dev_pct / max(0.5 * delta.atr_24h_pct, 1e-4))
    pillar_momentum = _clamp((0.45 * ema_trend) + (0.35 * roc_blend) + (0.20 * vwap_sig), -1.0, 1.0)

    # Pilar 2: Structural Liquidity (Market Gravity)
    pillar_gravity, comfort_price, mg_net, rej_up, rej_down = _resolve_gravity_metrics(
        candles, close, delta.atr_24h_pct, gravity_snapshot
    )

    # Pilar 3: Derivatives & AI Catalyst (opsional)
    has_funding = funding_daily is not None
    has_ai = ai_score is not None
    pillar_catalyst: Optional[float] = None
    if has_funding or has_ai:
        funding_skew = math.tanh(-float(funding_daily or 0.0) / 0.005) if has_funding else 0.0
        ai_clamped = _clamp(float(ai_score or 0.0), -1.0, 1.0) if has_ai else 0.0
        if has_funding and has_ai:
            pillar_catalyst = _clamp((0.65 * ai_clamped) + (0.35 * funding_skew), -1.0, 1.0)
        elif has_ai:
            pillar_catalyst = ai_clamped
        else:
            pillar_catalyst = funding_skew

    if pillar_catalyst is None:
        forecast_score = _clamp((0.50 * pillar_momentum) + (0.50 * pillar_gravity), -1.0, 1.0)
    else:
        forecast_score = _clamp(
            (0.35 * pillar_momentum) + (0.35 * pillar_gravity) + (0.30 * pillar_catalyst),
            -1.0,
            1.0,
        )

    stop_loss_pct = 0.015 if sl_mode == "fixed_15" else round(_clamp(0.75 * delta.atr_24h_pct, 0.015, 0.035), 4)
    take_profit_pct = 0.025 if sl_mode == "fixed_15" else round(max(0.020, 1.5 * stop_loss_pct), 4)

    def _no_trade(reason_txt: str, *, side_hint: Literal["long", "short", "neutral"] = "neutral") -> Forecast24hResult:
        return Forecast24hResult(
            asset=delta.asset,
            asset_class=delta.asset_class,
            setup="NONE",
            side=side_hint,
            tradeable=False,
            confidence=0.0,
            expected_move_24h_pct=0.0,
            expected_gross_24h_pct=0.0,
            round_trip_cost_pct=round_trip_cost_pct,
            adverse_funding_24h_pct=0.0,
            net_edge_24h_pct=-round_trip_cost_pct,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            forecast_score=round(forecast_score, 4),
            pillar_momentum=round(pillar_momentum, 4),
            pillar_gravity=round(pillar_gravity, 4),
            pillar_catalyst=None if pillar_catalyst is None else round(pillar_catalyst, 4),
            delta_24h=delta,
            comfort_price=comfort_price,
            mg_net_gravity=round(mg_net, 2),
            rejected_up=rej_up,
            rejected_down=rej_down,
            reason=reason_txt,
        )

    if p5_risk >= 0.70:
        return _no_trade(f"Veto risiko katalis P5={p5_risk:.2f} >= 0.70")

    setup: Literal["MOMENTUM_BREAKOUT", "MEAN_REVERSION_FADE", "NONE"] = "NONE"
    side: Literal["long", "short", "neutral"] = "neutral"
    setup_reason = "tidak memenuhi syarat Momentum Breakout maupun Mean-Reversion Fade"

    # Setup A: Extreme Mean-Reversion Fade (>= 2.0 ATR atau sweep Rejected ke arah Comfort Point)
    if "MEAN_REVERSION_FADE" in enabled_setups:
        fade_short = (
            (delta.z_move_24h >= 2.0 and (delta.rsi_14 >= 75.0 or rej_up))
            or (delta.z_move_24h >= 1.5 and rej_up and comfort_price < close)
        )
        fade_long = (
            (delta.z_move_24h <= -2.0 and (delta.rsi_14 <= 25.0 or rej_down))
            or (delta.z_move_24h <= -1.5 and rej_down and comfort_price > close)
        )
        if fade_short and not fade_long:
            setup = "MEAN_REVERSION_FADE"
            side = "short"
            setup_reason = f"Mean-Reversion Fade SHORT (z={delta.z_move_24h:+.2f}, RSI={delta.rsi_14:.1f}, rej_up={rej_up})"
        elif fade_long and not fade_short:
            setup = "MEAN_REVERSION_FADE"
            side = "long"
            setup_reason = f"Mean-Reversion Fade LONG (z={delta.z_move_24h:+.2f}, RSI={delta.rsi_14:.1f}, rej_dn={rej_down})"

    # Setup B: Momentum Breakout Continuation (0.75 - 1.80 ATR searah tren & volume)
    if setup == "NONE" and "MOMENTUM_BREAKOUT" in enabled_setups:
        mom_long = (
            0.75 <= delta.z_move_24h <= 1.80
            and delta.ema_fast > delta.ema_slow
            and delta.range_pos_24h >= 0.70
            and delta.vol_ratio_24h >= 1.15
            and delta.rsi_14 < 75.0
            and not rej_up
        )
        mom_short = (
            -1.80 <= delta.z_move_24h <= -0.75
            and delta.ema_fast < delta.ema_slow
            and delta.range_pos_24h <= 0.30
            and delta.vol_ratio_24h >= 1.15
            and delta.rsi_14 > 25.0
            and not rej_down
        )
        if mom_long:
            setup = "MOMENTUM_BREAKOUT"
            side = "long"
            setup_reason = f"Momentum Breakout LONG (z={delta.z_move_24h:+.2f}, pos={delta.range_pos_24h:.2f}, vol={delta.vol_ratio_24h:.2f}x)"
        elif mom_short:
            setup = "MOMENTUM_BREAKOUT"
            side = "short"
            setup_reason = f"Momentum Breakout SHORT (z={delta.z_move_24h:+.2f}, pos={delta.range_pos_24h:.2f}, vol={delta.vol_ratio_24h:.2f}x)"

    if setup == "NONE" or side == "neutral":
        return _no_trade(setup_reason)

    # Veto bila sentimen AI berlawanan tajam
    if ai_score is not None:
        if side == "long" and ai_score <= -0.35:
            return _no_trade(f"Ditolak AI berlawanan arah LONG (ai_score={ai_score:+.2f})", side_hint=side)
        if side == "short" and ai_score >= 0.35:
            return _no_trade(f"Ditolak AI berlawanan arah SHORT (ai_score={ai_score:+.2f})", side_hint=side)

    dir_sign = 1.0 if side == "long" else -1.0
    # Confidence [0.35 .. 0.90] berbasis kekuatan sinyal & keselarasan pilar
    if setup == "MOMENTUM_BREAKOUT":
        align_mom = max(0.0, dir_sign * pillar_momentum)
        align_grav = max(0.0, dir_sign * pillar_gravity) if pillar_gravity * dir_sign > -0.25 else 0.0
        strength_z = min(1.0, abs(delta.z_move_24h) / 1.80)
        base_conf = 0.48 + (0.20 * align_mom) + (0.12 * align_grav) + (0.10 * strength_z)
    else:
        # Untuk Fade, konfirmasi berasal dari Pilar Gravity / Rejection + ekstremitas RSI/z
        align_grav = max(0.0, dir_sign * pillar_gravity)
        extremity = min(1.0, max(0.0, abs(delta.z_move_24h) - 1.5) / 1.5)
        rej_bonus = 0.14 if ((side == "short" and rej_up) or (side == "long" and rej_down)) else 0.0
        base_conf = 0.45 + (0.20 * align_grav) + (0.12 * extremity) + rej_bonus

    if pillar_catalyst is not None:
        base_conf += 0.10 * max(-0.5, dir_sign * pillar_catalyst)

    confidence = _clamp(base_conf, 0.30, 0.90)

    # Estimasi expected move 24 jam
    if setup == "MOMENTUM_BREAKOUT":
        raw_move = 0.85 * delta.atr_24h_pct * (0.70 + 0.30 * min(abs(delta.z_move_24h), 1.80) / 1.80)
        expected_move = _clamp(raw_move, 0.012, 0.080)
    else:
        comfort_move = abs(comfort_price - close) / close if close > 0 else 0.0
        comfort_same_side = (side == "long" and comfort_price > close) or (side == "short" and comfort_price < close)
        if comfort_same_side and comfort_move > 0:
            raw_move = min(comfort_move, 1.25 * delta.atr_24h_pct)
        else:
            raw_move = 0.80 * delta.atr_24h_pct
        expected_move = _clamp(raw_move, 0.012, 0.080)

    favorable_funding = (-float(funding_daily) if side == "long" else float(funding_daily)) if funding_daily is not None else 0.0
    adverse_funding_24h = max(0.0, -favorable_funding)
    expected_gross = expected_move * confidence
    net_edge = expected_gross - round_trip_cost_pct - adverse_funding_24h

    tradeable = net_edge >= min_net_edge_pct
    reason = setup_reason if tradeable else f"{setup_reason} | net_edge={net_edge*100:+.2f}% < req={min_net_edge_pct*100:.2f}%"

    return Forecast24hResult(
        asset=delta.asset,
        asset_class=delta.asset_class,
        setup=setup,
        side=side,
        tradeable=tradeable,
        confidence=round(confidence, 4),
        expected_move_24h_pct=round(expected_move, 6),
        expected_gross_24h_pct=round(expected_gross, 6),
        round_trip_cost_pct=round_trip_cost_pct,
        adverse_funding_24h_pct=round(adverse_funding_24h, 6),
        net_edge_24h_pct=round(net_edge, 6),
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        forecast_score=round(forecast_score, 4),
        pillar_momentum=round(pillar_momentum, 4),
        pillar_gravity=round(pillar_gravity, 4),
        pillar_catalyst=None if pillar_catalyst is None else round(pillar_catalyst, 4),
        delta_24h=delta,
        comfort_price=comfort_price,
        mg_net_gravity=round(mg_net, 2),
        rejected_up=rej_up,
        rejected_down=rej_down,
        reason=reason,
    )


# =============================================================================
# ENGINE 2 PRODUKSI CRYPTO: V5 (4h Rejected -> Comfort) + V4 (1h Extreme Fade)
# Terkalibrasi 4-Fold Walk-Forward OOS pada 10 Koin Tier-1 Hyperliquid
# =============================================================================

FORECAST_SL_CAP_PCT = 0.045              # Maks 4.5% price SL (maks 9% risiko margin @ 2x)
FORECAST_MAX_ADVERSE_FUNDING_DAILY = 0.0030  # Tolak bila harus membayar funding > 0.30%/hari


def _extract_gravity_fade_state(
    candles: Sequence[Mapping[str, Any]],
) -> tuple[float, bool, bool, Optional[list[float]], Optional[list[float]]]:
    """Ambil (comfort_structural, rejected_up, rejected_down, rejected_up_prices, rejected_down_prices)."""
    n = len(candles)
    close = float(candles[-1]["c"])
    mg = analyze_market_gravity(candles, trace_every=max(1, n - 1))
    if not mg.usable or not mg.trace or mg.trace[-1].bar_index != n - 1:
        return close, False, False, None, None
    snap = mg.trace[-1]
    cp = float(comfort_points(snap).get("structural", close))
    rej_up = list(snap.rejected_up or [])
    rej_dn = list(snap.rejected_down or [])
    return cp, bool(rej_up), bool(rej_dn), rej_up, rej_dn


def evaluate_crypto_live_fade_signal(
    asset: str,
    candles_4h: Sequence[Mapping[str, Any]],
    candles_1h: Optional[Sequence[Mapping[str, Any]]] = None,
    *,
    funding_daily: float = 0.0,
    round_trip_cost_pct: float = DEFAULT_ROUND_TRIP_COST_PCT,
) -> Optional[dict[str, Any]]:
    """Evaluasi sinyal Engine 2 teruji OOS khusus Crypto Tier-1:
    1) `V4_1H_EXTREME_FADE`: 1h extreme fade (`z >= 2.2 & RSI >= 78` atau `Rejected` + `z >= 1.5` menuju Comfort).
    2) `V5_4H_REJ_COMFORT`: 4h structural magnet `Rejected` searah `Comfort Point`.
    """
    sym = str(asset or "").strip().upper()
    candidate: Optional[dict[str, Any]] = None

    # Prioritas 1: V4_1H_EXTREME_FADE (OOS 1h: 58.3% win, +1.027% gross, PF 1.73)
    if candles_1h and len(candles_1h) >= 75:
        d1 = compute_price_delta_24h(sym, candles_1h, interval="1h", asset_class="crypto")
        if d1 is not None:
            cp1, rej_up1, rej_dn1, _ru1, _rd1 = _extract_gravity_fade_state(candles_1h)
            side1: Optional[Literal["long", "short"]] = None
            if (d1.z_move_24h >= 2.2 and d1.rsi_14 >= 78.0) or (rej_up1 and cp1 < d1.close and d1.z_move_24h >= 1.5):
                side1 = "short"
            elif (d1.z_move_24h <= -2.2 and d1.rsi_14 <= 22.0) or (rej_dn1 and cp1 > d1.close and d1.z_move_24h <= -1.5):
                side1 = "long"
            if side1 is not None:
                sl_pct = round(_clamp(1.0 * d1.atr_24h_pct, 0.015, FORECAST_SL_CAP_PCT), 4)
                tp_pct = round(max(0.020, 1.2 * sl_pct), 4)
                candidate = {
                    "asset": sym,
                    "side": side1,
                    "setup": "V4_1H_EXTREME_FADE",
                    "sl_pct": sl_pct,
                    "tp_pct": tp_pct,
                    "max_hold_hours": 24.0,
                    "expected_gross_pct": 0.0102,
                    "z_move_24h": round(d1.z_move_24h, 3),
                    "rsi_14": round(d1.rsi_14, 2),
                    "reason": (
                        f"V4 1h Fade {side1.upper()} (z={d1.z_move_24h:+.2f}, RSI={d1.rsi_14:.1f}, "
                        f"SL={sl_pct*100:.2f}%, TP={tp_pct*100:.2f}%, hold=24h)"
                    ),
                }

    # Prioritas 2: V5_4H_REJ_COMFORT (OOS 4h: 340 trade, 54.7% win, +0.470% gross, PF 1.12)
    if candidate is None and candles_4h and len(candles_4h) >= 75:
        d4 = compute_price_delta_24h(sym, candles_4h, interval="4h", asset_class="crypto")
        if d4 is not None:
            cp4, rej_up4, rej_dn4, _ru4, _rd4 = _extract_gravity_fade_state(candles_4h)
            side4: Optional[Literal["long", "short"]] = None
            if rej_up4 and cp4 < d4.close and d4.rsi_14 >= 50.0:
                side4 = "short"
            elif rej_dn4 and cp4 > d4.close and d4.rsi_14 <= 50.0:
                side4 = "long"
            if side4 is not None:
                sl_pct = round(_clamp(1.0 * d4.atr_24h_pct, 0.015, FORECAST_SL_CAP_PCT), 4)
                tp_pct = round(max(0.020, 1.0 * sl_pct), 4)
                candidate = {
                    "asset": sym,
                    "side": side4,
                    "setup": "V5_4H_REJ_COMFORT",
                    "sl_pct": sl_pct,
                    "tp_pct": tp_pct,
                    "max_hold_hours": 72.0,
                    "expected_gross_pct": 0.0050,
                    "z_move_24h": round(d4.z_move_24h, 3),
                    "rsi_14": round(d4.rsi_14, 2),
                    "reason": (
                        f"V5 4h Fade {side4.upper()} (Rejected->Comfort, z={d4.z_move_24h:+.2f}, "
                        f"RSI={d4.rsi_14:.1f}, SL={sl_pct*100:.2f}%, TP={tp_pct*100:.2f}%, hold=72h)"
                    ),
                }

    if candidate is None:
        return None

    side = candidate["side"]
    favorable_funding = (-float(funding_daily or 0.0)) if side == "long" else float(funding_daily or 0.0)
    adverse_funding = max(0.0, -favorable_funding)
    if adverse_funding > FORECAST_MAX_ADVERSE_FUNDING_DAILY:
        return None

    expected_net = round(candidate["expected_gross_pct"] - float(round_trip_cost_pct) - adverse_funding, 6)
    if expected_net <= 0.0:
        return None

    candidate["adverse_funding_daily"] = round(adverse_funding, 6)
    candidate["expected_net_edge_pct"] = expected_net
    return candidate


def _is_non_crypto_contract(
    contract: Mapping[str, Any],
    extra_exclude: Optional[set[str] | frozenset[str]] = None,
) -> bool:
    """True bila kontrak teridentifikasi sebagai saham, indeks, komoditas, FX, atau stablecoin."""
    asset = str(contract.get("base_asset") or "").strip().upper()
    if not asset or asset in NON_CRYPTO_SYMBOLS:
        return True
    if extra_exclude and asset in {str(x).strip().upper() for x in extra_exclude}:
        return True
    for field in ("category", "asset_class", "market_type", "type", "sector", "group"):
        val = str(contract.get(field) or "").strip().lower()
        if val and any(hint in val for hint in _NON_CRYPTO_META_HINTS):
            return True
    return False


def scan_crypto_price_opportunities(
    contracts: Sequence[Mapping[str, Any]],
    *,
    allowed_assets: Optional[set[str] | frozenset[str]] = None,
    exclude_assets: Optional[set[str]] = None,
    exclude_non_crypto: Optional[set[str] | frozenset[str]] = None,
    min_vol_24h: float = 1_000_000.0,
    max_candidates: int = FORECAST_MAX_SCAN_ASSETS,
    max_signals: int = FORECAST_MAX_SIGNALS,
    max_scan_seconds: float = 45.0,
    fetcher: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Pindai kontrak Crypto likuid (`allowed_assets=None` -> seluruh crypto di Walt) memakai Engine 2."""
    from market_chart import fetch_crypto_candles as _default_fetch

    candle_fetcher = fetcher or _default_fetch
    target = {str(a).strip().upper() for a in allowed_assets} if allowed_assets is not None else None
    excluded = {str(a).strip().upper() for a in (exclude_assets or set())}

    candidates_by_asset: dict[str, tuple[float, float, float, Mapping[str, Any]]] = {}
    for c in contracts:
        asset = str(c.get("base_asset") or "").strip().upper()
        if not asset or asset in excluded:
            continue
        if _is_non_crypto_contract(c, extra_exclude=exclude_non_crypto):
            continue
        if target is not None and asset not in target:
            continue
        try:
            vol = float(c.get("volume_24h_usd") or 0.0)
        except (TypeError, ValueError):
            vol = 0.0
        if vol < min_vol_24h:
            continue
        try:
            fr_daily = float(c.get("funding_rate_hourly") or 0.0) * 24.0
        except (TypeError, ValueError):
            fr_daily = 0.0
        try:
            slip = float(c.get("price_slippage") or 0.0)
        except (TypeError, ValueError):
            slip = 0.0
        prev = candidates_by_asset.get(asset)
        if prev is None or vol > prev[0]:
            candidates_by_asset[asset] = (vol, fr_daily, slip, c)

    ordered = sorted(
        ((vol, asset, fr_daily, slip, c) for asset, (vol, fr_daily, slip, c) in candidates_by_asset.items()),
        key=lambda item: item[0],
        reverse=True,
    )
    if max_candidates > 0:
        ordered = ordered[: int(max_candidates)]

    def _eval_candidate(item: tuple[float, str, float, float, Mapping[str, Any]]) -> Optional[dict[str, Any]]:
        vol, asset, fr_daily, slip, c = item
        rt_cost = max(DEFAULT_ROUND_TRIP_COST_PCT, 0.0018 + min(max(0.0, slip), 0.0020))
        try:
            candles_4h, _src4 = candle_fetcher(asset, "4h", 180)
        except Exception:
            candles_4h = []
        try:
            candles_1h, _src1 = candle_fetcher(asset, "1h", 180)
        except Exception:
            candles_1h = []

        sig = evaluate_crypto_live_fade_signal(
            asset,
            candles_4h,
            candles_1h,
            funding_daily=fr_daily,
            round_trip_cost_pct=rt_cost,
        )
        if sig is None:
            return None

        sig.update({
            "contract_id": c["contract_id"],
            "funding_daily": fr_daily,
            "volume_24h": vol,
            "max_leverage": int(c.get("max_leverage") or 2),
            "min_size_base": float(c.get("min_size_base") or 0.0),
            "max_size_base": float(c.get("max_size_base") or 0.0),
            "max_size_quote": float(c.get("max_size_quote") or 0.0),
            "price_slippage": slip,
        })
        return sig

    found: list[dict[str, Any]] = []
    if fetcher is None and len(ordered) > 1:
        deadline = time.monotonic() + max(1.0, float(max_scan_seconds))
        pool = ThreadPoolExecutor(max_workers=6)
        try:
            futures = {pool.submit(_eval_candidate, item): item[1] for item in ordered}
            for fut in as_completed(futures, timeout=max(1.0, deadline - time.monotonic())):
                try:
                    res = fut.result(timeout=max(0.1, deadline - time.monotonic()))
                    if res is not None:
                        found.append(res)
                except Exception:
                    continue
        except TimeoutError:
            pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    else:
        for item in ordered:
            res = _eval_candidate(item)
            if res is not None:
                found.append(res)

    found.sort(key=lambda item: float(item.get("expected_net_edge_pct") or 0.0), reverse=True)
    if max_signals > 0:
        return found[: int(max_signals)]
    return found
