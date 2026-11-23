"""
Modul data market, indikator teknikal (EMA20/50, RSI14), sparkline, gambar chart PNG,
dan referensi sumber cek harga aktual untuk Bot Telegram Walt Trading.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from typing import Any, Optional

import httpx

YAHOO_TICKER_MAP: dict[str, str] = {
    "US100": "NQ=F",
    "XAU": "GC=F",
    "XAG": "SI=F",
    "BRENTOIL": "BZ=F",
}

_YAHOO_UNLISTED_ASSETS: frozenset[str] = frozenset({
    "ANTHROPIC", "ANT", "SPCX", "SPACEX", "OPENAI", "STRIPE", "XAI", "DATABRICKS", "ANDURIL",
})

_HL_UNIVERSE_CACHE: Optional[dict[str, str]] = None
_HL_UNIVERSE_LOCK = threading.Lock()

CRYPTO_ASSETS = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "AVAX", "SUI", "LINK", "NEAR", "AAVE",
    "ARB", "OP", "DOGE", "ADA", "DOT", "ATOM", "LTC", "UNI", "APT", "SEI",
    "HYPE", "PEPE", "WIF", "BONK", "SHIB", "TAO", "TON", "ENA", "PENDLE",
    "RENDER", "FET", "INJ", "TIA", "JUP", "PYTH", "WLD", "ONDO", "TRX", "BCH",
    "ETC", "XLM", "FIL", "ICP", "STX", "RUNE", "MKR", "LDO", "CRV", "SNX",
    "COMP", "PENGU", "VIRTUAL", "FARTCOIN", "TRUMP", "BERA", "IP", "KAITO",
    "ZEC", "XMR", "WLFI", "PUMP", "MON",
}

SPARK_CHARS = "▁▂▃▄▅▆▇█"


def make_sparkline(values: list[float], width: int = 24) -> str:
    if not values:
        return "-"
    pts = values[-width:]
    lo, hi = min(pts), max(pts)
    span = hi - lo
    if span <= 0:
        return SPARK_CHARS[3] * len(pts)
    return "".join(
        SPARK_CHARS[min(len(SPARK_CHARS) - 1, int((v - lo) / span * (len(SPARK_CHARS) - 1)))]
        for v in pts
    )


def compute_ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    emas = [values[0]]
    for v in values[1:]:
        emas.append(v * k + emas[-1] * (1.0 - k))
    return emas


def compute_rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _fetch_hyperliquid_candles(asset: str, interval: str = "1h", limit: int = 48) -> list[dict[str, Any]]:
    import time
    interval_sec = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}.get(interval, 3600)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - limit * interval_sec * 1000
    r = httpx.post(
        "https://api.hyperliquid.xyz/info",
        json={
            "type": "candleSnapshot",
            "req": {
                "coin": asset.upper(),
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
            },
        },
        timeout=10.0,
    )
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        return []
    candles = []
    for row in rows[-limit:]:
        # Hyperliquid returns the in-progress candle too; a forming bar would leak
        # the current (unclosed) high/low into structure detection.
        if int(row.get("T", 0)) > end_ms:
            continue
        candles.append({
            "t": int(row["t"]),
            "o": float(row["o"]),
            "h": float(row["h"]),
            "l": float(row["l"]),
            "c": float(row["c"]),
            "v": float(row.get("v") or 0.0),
        })
    return candles


def _fetch_binance_candles(asset: str, interval: str = "1h", limit: int = 48) -> list[dict[str, Any]]:
    symbol = f"{asset.upper()}USDT"
    limit = max(1, min(int(limit), 1000))  # the klines endpoint rejects limit > 1000
    r = httpx.get(
        "https://data-api.binance.vision/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10.0,
    )
    r.raise_for_status()
    rows = r.json()
    now_ms = int(time.time() * 1000)
    candles = []
    for row in rows:
        # row[6] is the candle's close time: skip the still-open bar (lookahead guard).
        if int(row[6]) > now_ms:
            continue
        candles.append({
            "t": int(row[0]),
            "o": float(row[1]),
            "h": float(row[2]),
            "l": float(row[3]),
            "c": float(row[4]),
            "v": float(row[5]),
        })
    return candles


def _fetch_yahoo_candles(asset: str, interval: str = "1h", limit: int = 48) -> list[dict[str, Any]]:
    sym_u = str(asset or "").strip().upper()
    if sym_u in _YAHOO_UNLISTED_ASSETS:
        return []
    ticker = YAHOO_TICKER_MAP.get(sym_u, sym_u)
    # A 48-bar chart only ever needed a few days; a liquidity-structure window needs
    # hundreds of bars, so the range has to scale with the requested limit.
    if limit > 700:
        deep_map = {"15m": "5d", "1h": "3mo", "4h": "6mo", "1d": "2y"}
    elif limit > 200:
        deep_map = {"15m": "5d", "1h": "1mo", "4h": "3mo", "1d": "1y"}
    else:
        deep_map = {"15m": "5d", "1h": "5d", "4h": "1mo", "1d": "3mo"}
    range_map = deep_map
    yf_interval = "1h" if interval == "4h" else interval
    r = httpx.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}",
        params={"interval": yf_interval, "range": range_map.get(interval, "5d")},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15.0,
    )
    r.raise_for_status()
    data = r.json()
    res = (data.get("chart") or {}).get("result") or []
    if not res:
        return []
    timestamps = res[0].get("timestamp") or []
    quote = ((res[0].get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    step_ms = 3_600_000
    if len(timestamps) > 1:
        step_ms = max(1, (int(timestamps[-1]) - int(timestamps[0])) * 1000 // (len(timestamps) - 1))
    now_ms = int(time.time() * 1000)
    candles = []
    for index, (t, o, h, l, c) in enumerate(zip(timestamps, opens, highs, lows, closes)):
        if None in (t, o, h, l, c):
            continue
        if int(t) * 1000 + step_ms > now_ms:
            continue  # the newest bucket is still forming: never score a partial bar
        candle = {
            "t": int(t) * 1000,
            "o": float(o),
            "h": float(h),
            "l": float(l),
            "c": float(c),
        }
        if index < len(volumes) and volumes[index] is not None:
            candle["v"] = float(volumes[index])
        candles.append(candle)
    return candles[-limit:]


_ORIG_FETCH_HL = _fetch_hyperliquid_candles
_ORIG_FETCH_BN = _fetch_binance_candles


def get_hyperliquid_universe_map() -> dict[str, str]:
    """Ambil dan cache peta simbol resmi Hyperliquid (`UPPER -> nama asli`) sekali per proses."""
    global _HL_UNIVERSE_CACHE
    with _HL_UNIVERSE_LOCK:
        if _HL_UNIVERSE_CACHE is not None:
            return dict(_HL_UNIVERSE_CACHE)
        try:
            r = httpx.post("https://api.hyperliquid.xyz/info", json={"type": "meta"}, timeout=10.0)
            r.raise_for_status()
            data = r.json()
            mapping: dict[str, str] = {}
            for u in (data.get("universe") or []):
                if isinstance(u, dict) and u.get("name"):
                    raw_name = str(u["name"]).strip()
                    if raw_name:
                        mapping[raw_name.upper()] = raw_name
            _HL_UNIVERSE_CACHE = mapping
        except Exception:
            _HL_UNIVERSE_CACHE = {}
        return dict(_HL_UNIVERSE_CACHE)


_ORIG_GET_HL_MAP = get_hyperliquid_universe_map


def fetch_crypto_candles(asset: str, interval: str = "1h", limit: int = 48) -> tuple[list[dict[str, Any]], str]:
    """Ambil candle OHLCV khusus kontrak Crypto dari Hyperliquid/Binance (tanpa Yahoo Finance)."""
    sym = asset.upper().strip()
    if interval not in {"15m", "1h", "4h", "1d"}:
        interval = "1h"

    hl_candidates = [sym]
    if sym.startswith("1000") and len(sym) > 4:
        hl_candidates.append(f"k{sym[4:]}")
    elif not sym.startswith("K"):
        hl_candidates.append(f"k{sym}")

    hl_patched = _fetch_hyperliquid_candles is not _ORIG_FETCH_HL
    hl_map_patched = get_hyperliquid_universe_map is not _ORIG_GET_HL_MAP
    hl_map = get_hyperliquid_universe_map() if (hl_map_patched or not hl_patched) else {}
    if hl_map:
        hl_run_list = [hl_map[c.upper()] for c in hl_candidates if c.upper() in hl_map]
    elif hl_patched:
        hl_run_list = hl_candidates
    else:
        hl_run_list = [sym] if (sym in CRYPTO_ASSETS or sym.lstrip("K") in CRYPTO_ASSETS) else []

    for hl_sym in hl_run_list:
        try:
            candles = _fetch_hyperliquid_candles(hl_sym, interval, limit)
            if candles:
                return candles, "Hyperliquid"
        except Exception:
            continue

    bn_patched = _fetch_binance_candles is not _ORIG_FETCH_BN
    base_no_prefix = sym[4:] if sym.startswith("1000") else (sym[1:] if sym.startswith("K") else sym)
    if not bn_patched and sym not in CRYPTO_ASSETS and base_no_prefix not in CRYPTO_ASSETS:
        return [], "Tidak tersedia"

    bn_candidates = [sym]
    if sym.startswith("K") and len(sym) > 1:
        bn_candidates.append(sym[1:])
        bn_candidates.append(f"1000{sym[1:]}")

    for bn_sym in bn_candidates:
        try:
            candles = _fetch_binance_candles(bn_sym, interval, limit)
            if candles:
                return candles, "Binance"
        except Exception:
            continue

    return [], "Tidak tersedia"


def fetch_candles(asset: str, interval: str = "1h", limit: int = 48) -> tuple[list[dict[str, Any]], str]:
    """Ambil candle OHLCV dan nama sumber datanya."""
    sym = asset.upper().strip()
    if interval not in {"15m", "1h", "4h", "1d"}:
        interval = "1h"

    if sym in CRYPTO_ASSETS:
        return fetch_crypto_candles(sym, interval, limit)
    for name, fn in [("Yahoo Finance", _fetch_yahoo_candles), ("Hyperliquid", _fetch_hyperliquid_candles)]:
        try:
            candles = fn(sym, interval, limit)
            if candles:
                return candles, name
        except Exception:
            continue
    return [], "Tidak tersedia"


def build_quickchart_url(asset: str, interval: str, candles: list[dict[str, Any]]) -> str:
    """Bangun URL QuickChart PNG untuk dikirim ke Telegram sendPhoto."""
    from datetime import datetime, timezone

    closes = [round(c["c"], 4) for c in candles]
    emas = [round(v, 4) for v in compute_ema(closes, 20)]
    labels = [
        datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc).strftime("%d/%m %H:%M")
        for c in candles
    ]
    chart_cfg = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": f"{asset.upper()} ({interval})",
                    "data": closes,
                    "borderColor": "#2563eb",
                    "backgroundColor": "rgba(37, 99, 235, 0.10)",
                    "fill": True,
                    "borderWidth": 2,
                    "pointRadius": 0,
                },
                {
                    "label": "EMA 20",
                    "data": emas,
                    "borderColor": "#f59e0b",
                    "fill": False,
                    "borderWidth": 1.5,
                    "borderDash": [4, 4],
                    "pointRadius": 0,
                },
            ],
        },
        "options": {
            "plugins": {
                "title": {
                    "display": True,
                    "text": f"Walt Market Chart: {asset.upper()} ({interval})",
                }
            },
            "scales": {
                "x": {"ticks": {"maxTicksLimit": 6}},
            },
        },
    }
    encoded = urllib.parse.quote(json.dumps(chart_cfg, separators=(",", ":")))
    return f"https://quickchart.io/chart?w=800&h=400&bkg=white&c={encoded}"


def analyze_market_chart(asset: str, interval: str = "1h") -> dict[str, Any]:
    """Hitung ringkasan teknikal & URL chart PNG untuk satu aset."""
    sym = asset.upper().strip()
    candles, source = fetch_candles(sym, interval=interval, limit=48)
    if not candles:
        return {"asset": sym, "interval": interval, "ok": False, "source": source}

    closes = [c["c"] for c in candles]
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    first_c, last_c = closes[0], closes[-1]
    change_pct = ((last_c - first_c) / first_c * 100.0) if first_c else 0.0

    ema20 = compute_ema(closes, 20)[-1]
    ema50 = compute_ema(closes, 50)[-1]
    rsi14 = compute_rsi(closes, 14)

    if last_c > ema20 and ema20 >= ema50:
        trend = "BULLISH 📈"
    elif last_c < ema20 and ema20 <= ema50:
        trend = "BEARISH 📉"
    else:
        trend = "SIDEWAYS / MIXED ↔️"

    return {
        "asset": sym,
        "interval": interval,
        "ok": True,
        "source": source,
        "last_price": last_c,
        "high": max(highs),
        "low": min(lows),
        "change_pct": change_pct,
        "ema20": ema20,
        "ema50": ema50,
        "rsi14": rsi14,
        "trend": trend,
        "sparkline": make_sparkline(closes),
        "chart_url": build_quickchart_url(sym, interval, candles),
    }


def get_actual_price_sources(asset: str | None = None) -> str:
    """Daftar sumber terpercaya untuk memverifikasi harga aktual & funding rate."""
    sym = (asset or "BTC").upper().strip()
    if sym in CRYPTO_ASSETS:
        tv_sym = f"BINANCE:{sym}USDT.P"
        hl_url = f"https://app.hyperliquid.xyz/trade/{sym}"
        cg_url = "https://www.coinglass.com/FundingRate"
        return (
            f"🔍 *Sumber Cek Harga Aktual ({sym}):*\n"
            f"1. *Walt / Hyperliquid Perps (Mark Price & Funding Riil):*\n   {hl_url}\n"
            f"2. *TradingView (Chart Candlestick Real-Time):*\n   https://www.tradingview.com/chart/?symbol={tv_sym}\n"
            f"3. *CoinGlass (Perbandingan Funding Rate & OI):*\n   {cg_url}\n"
            f"4. *Binance Spot/Perps:*\n   https://www.binance.com/en/futures/{sym}USDT"
        )
    yf_ticker = YAHOO_TICKER_MAP.get(sym, sym)
    tv_map = {
        "US100": "NASDAQ:NDX",
        "XAU": "OANDA:XAUUSD",
        "XAG": "OANDA:XAGUSD",
        "BRENTOIL": "TVC:UKOIL",
    }
    tv_sym = tv_map.get(sym, sym)
    return (
        f"🔍 *Sumber Cek Harga Aktual ({sym}):*\n"
        f"1. *Walt / Hyperliquid Perps (Harga Eksekusi & Funding Bot):*\n   https://app.hyperliquid.xyz/trade/{sym}\n"
        f"2. *TradingView (Chart Saham/Komoditas Global):*\n   https://www.tradingview.com/chart/?symbol={tv_sym}\n"
        f"3. *Yahoo Finance (Spot/Futures Referensi):*\n   https://finance.yahoo.com/quote/{urllib.parse.quote(yf_ticker)}"
    )
