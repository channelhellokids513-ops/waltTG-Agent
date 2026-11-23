"""
backtest_forecast_24h - Walk-Forward OOS Backtester untuk 24h Price-Delta & 3-Pillar Forecast.

Read-only: tidak menyentuh order, ledger produksi, atau Google Sheet.
Mengambil data historis OHLCV publik melalui `market_chart.fetch_candles`
(Crypto via Hyperliquid/Binance; Saham AS via Yahoo Finance), menghitung snapshot
`market_gravity` secara kausal (`trace_every=1`), lalu mengevaluasi sinyal OOS lintas
4 fold dengan purge 1 hari trading antar fold dan resolusi intrabar konservatif
(SL diperiksa lebih dulu sebelum TP pada bar yang sama, ditambah Time-Stop 24 jam).

Pemakaian:
    python tools/backtest_forecast_24h.py
    python tools/backtest_forecast_24h.py --interval 1h --limit 720 --sl-mode fixed_15
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_chart import fetch_candles  # noqa: E402
from market_gravity import GravityParams, GravitySnapshot, analyze_market_gravity  # noqa: E402
from price_forecast import (  # noqa: E402
    DEFAULT_MIN_NET_EDGE_PCT,
    bars_per_trading_day,
    forecast_24h,
    infer_asset_class,
)

CRYPTO_UNIVERSE = ["BTC", "ETH", "SOL", "BNB", "XRP", "AVAX", "SUI", "LINK", "NEAR", "AAVE"]
STOCK_UNIVERSE = ["NVDA", "AAPL", "TSLA", "AMD", "META", "AMZN", "GOOGL", "PLTR", "COIN", "SPY"]
COSTS = (0.0028, 0.0045)


def simulate_trade_24h(
    candles: Sequence[Mapping[str, Any]],
    entry_index: int,
    side: Literal["long", "short"],
    tp_pct: float,
    sl_pct: float,
    max_hold_bars: int,
) -> Optional[dict[str, Any]]:
    """Simulasikan satu trade dari close bar `entry_index` dengan resolusi intrabar konservatif.

    Aturan ketat:
      - Tidak ada lookahead: entry pada `close[entry_index]`, resolusi mulai `entry_index + 1`.
      - Intrabar SL dicek LEBIH DULU daripada TP bila keduanya tersentuh di bar yang sama.
      - Jika hingga `entry_index + max_hold_bars` belum menyentuh SL maupun TP -> Time-Stop
        keluar pada harga close bar terakhir jendela hold.
    """
    if entry_index < 0 or entry_index >= len(candles) - 1:
        return None
    entry = float(candles[entry_index]["c"])
    if entry <= 0:
        return None

    direction = 1.0 if side == "long" else -1.0
    sl_price = entry * (1.0 - sl_pct) if side == "long" else entry * (1.0 + sl_pct)
    tp_price = entry * (1.0 + tp_pct) if side == "long" else entry * (1.0 - tp_pct)

    last_idx = min(len(candles) - 1, entry_index + max(1, max_hold_bars))
    for i in range(entry_index + 1, last_idx + 1):
        bar = candles[i]
        hi = float(bar["h"])
        lo = float(bar["l"])
        cl = float(bar["c"])

        hit_sl = (lo <= sl_price) if side == "long" else (hi >= sl_price)
        hit_tp = (hi >= tp_price) if side == "long" else (lo <= tp_price)

        if hit_sl:
            return {
                "outcome": "SL",
                "gross_pct": -abs(sl_pct),
                "bars_held": i - entry_index,
                "exit_index": i,
            }
        if hit_tp:
            return {
                "outcome": "TP",
                "gross_pct": abs(tp_pct),
                "bars_held": i - entry_index,
                "exit_index": i,
            }

        if i == last_idx:
            gross_pct = direction * ((cl - entry) / entry)
            return {
                "outcome": "TIME_STOP",
                "gross_pct": gross_pct,
                "bars_held": i - entry_index,
                "exit_index": i,
            }
    return None


def collect_trades_for_asset(
    asset: str,
    candles: Sequence[Mapping[str, Any]],
    snap_by_bar: Mapping[int, GravitySnapshot],
    *,
    interval: str = "1h",
    bounds: Optional[tuple[int, int]] = None,
    sl_mode: Literal["atr", "fixed_15"] = "atr",
    min_net_edge_pct: float = DEFAULT_MIN_NET_EDGE_PCT,
    enabled_setups: tuple[str, ...] = ("MOMENTUM_BREAKOUT", "MEAN_REVERSION_FADE"),
    flip_side: bool = False,
) -> list[dict[str, Any]]:
    """Kumpulkan trade kausal non-overlapping untuk satu aset pada rentang bar `bounds`."""
    ac = infer_asset_class(asset)
    bars_24h = bars_per_trading_day(interval, ac)
    warmup = max(70, bars_24h + 25)
    lo = max(warmup, bounds[0]) if bounds else warmup
    hi = min(len(candles) - 2, bounds[1]) if bounds else (len(candles) - 2)

    trades: list[dict[str, Any]] = []
    open_until = -1
    for i in range(lo, hi + 1):
        if i <= open_until:
            continue
        snap = snap_by_bar.get(i)
        fc = forecast_24h(
            asset,
            candles[: i + 1],
            interval=interval,
            asset_class=ac,
            min_net_edge_pct=min_net_edge_pct,
            sl_mode=sl_mode,
            gravity_snapshot=snap,
            enabled_setups=enabled_setups,
        )
        if fc is None or not fc.tradeable or fc.side not in ("long", "short"):
            continue

        sim_side: Literal["long", "short"] = (
            ("short" if fc.side == "long" else "long") if flip_side else fc.side
        )
        sim = simulate_trade_24h(
            candles,
            entry_index=i,
            side=sim_side,
            tp_pct=fc.take_profit_pct,
            sl_pct=fc.stop_loss_pct,
            max_hold_bars=bars_24h,
        )
        if sim is None:
            continue
        open_until = int(sim["exit_index"])
        trades.append({
            "asset": asset,
            "asset_class": ac,
            "setup": fc.setup,
            "side": sim_side,
            "entry_index": i,
            "confidence": fc.confidence,
            "expected_net_pct": fc.net_edge_24h_pct,
            "z_move_24h": fc.delta_24h.z_move_24h,
            **sim,
        })
    return trades


def summarise_trades(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Hitung metrik performa jujur (gross, net biaya 0.28%, net biaya 0.45%, PF, MaxDD%)."""
    n = len(trades)
    if n == 0:
        return {
            "trades": 0,
            "win_rate_pct": 0.0,
            "avg_gross_pct": 0.0,
            "avg_net_28_pct": 0.0,
            "avg_net_45_pct": 0.0,
            "profit_factor_28": 0.0,
            "max_dd_28_pct": 0.0,
            "tp_pct": 0.0,
            "sl_pct": 0.0,
            "time_stop_pct": 0.0,
        }

    gross = [float(t["gross_pct"]) for t in trades]
    net_28 = [g - COSTS[0] for g in gross]
    net_45 = [g - COSTS[1] for g in gross]

    wins_28 = [x for x in net_28 if x > 0]
    losses_28 = [abs(x) for x in net_28 if x < 0]
    pf_28 = (sum(wins_28) / sum(losses_28)) if losses_28 else (float("inf") if wins_28 else 0.0)

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in net_28:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    outcomes = [str(t.get("outcome", "")) for t in trades]
    return {
        "trades": n,
        "win_rate_pct": round(100.0 * sum(1 for x in net_28 if x > 0) / n, 2),
        "avg_gross_pct": round(100.0 * statistics.mean(gross), 4),
        "avg_net_28_pct": round(100.0 * statistics.mean(net_28), 4),
        "avg_net_45_pct": round(100.0 * statistics.mean(net_45), 4),
        "profit_factor_28": round(pf_28, 3) if math.isfinite(pf_28) else 999.0,
        "max_dd_28_pct": round(100.0 * max_dd, 2),
        "tp_pct": round(100.0 * outcomes.count("TP") / n, 1),
        "sl_pct": round(100.0 * outcomes.count("SL") / n, 1),
        "time_stop_pct": round(100.0 * outcomes.count("TIME_STOP") / n, 1),
    }


def _walk_forward_fold_bounds(total_bars: int, warmup: int, folds: int) -> list[tuple[int, int]]:
    usable = total_bars - warmup - 1
    if usable < folds * 10 or folds <= 0:
        return [(warmup, total_bars - 2)]
    step = usable // folds
    bounds: list[tuple[int, int]] = []
    for k in range(folds):
        lo = warmup + k * step
        hi = (warmup + (k + 1) * step - 1) if k < folds - 1 else (total_bars - 2)
        if lo < hi:
            bounds.append((lo, hi))
    return bounds


def run_walk_forward(
    assets: Sequence[str],
    *,
    interval: str = "1h",
    limit: int = 720,
    folds: int = 4,
    sl_mode: Literal["atr", "fixed_15"] = "atr",
    min_net_edge_pct: float = DEFAULT_MIN_NET_EDGE_PCT,
) -> dict[str, Any]:
    """Jalankan backtest walk-forward OOS pada daftar aset nyata lewat public OHLC."""
    params = GravityParams()
    loaded: list[tuple[str, list[dict[str, Any]], dict[int, GravitySnapshot], str]] = []
    for asset in assets:
        candles, source = fetch_candles(asset, interval=interval, limit=limit)
        if len(candles) < 120:
            print(f"  {asset:<6} dilewati: hanya {len(candles)} bar ({source})")
            continue
        mg = analyze_market_gravity(candles, params, trace_every=1)
        snap_by_bar = {s.bar_index: s for s in mg.trace} if mg.usable else {}
        loaded.append((asset, candles, snap_by_bar, source))

    results: dict[str, list[dict[str, Any]]] = {
        "COMBINED": [],
        "MOMENTUM_BREAKOUT": [],
        "MEAN_REVERSION_FADE": [],
        "NULL_FLIPPED": [],
    }

    for asset, candles, snap_by_bar, _source in loaded:
        ac = infer_asset_class(asset)
        bars_24h = bars_per_trading_day(interval, ac)
        warmup = max(70, bars_24h + 25)
        fold_ranges = _walk_forward_fold_bounds(len(candles), warmup, folds)
        for lo, hi in fold_ranges:
            for key, setups, flip in (
                ("COMBINED", ("MOMENTUM_BREAKOUT", "MEAN_REVERSION_FADE"), False),
                ("MOMENTUM_BREAKOUT", ("MOMENTUM_BREAKOUT",), False),
                ("MEAN_REVERSION_FADE", ("MEAN_REVERSION_FADE",), False),
                ("NULL_FLIPPED", ("MOMENTUM_BREAKOUT", "MEAN_REVERSION_FADE"), True),
            ):
                results[key].extend(
                    collect_trades_for_asset(
                        asset,
                        candles,
                        snap_by_bar,
                        interval=interval,
                        bounds=(lo, hi),
                        sl_mode=sl_mode,
                        min_net_edge_pct=min_net_edge_pct,
                        enabled_setups=setups,
                        flip_side=flip,
                    )
                )

    summary = {k: summarise_trades(v) for k, v in results.items()}
    by_class = {
        "crypto": summarise_trades([t for t in results["COMBINED"] if t["asset_class"] == "crypto"]),
        "stock": summarise_trades([t for t in results["COMBINED"] if t["asset_class"] == "stock"]),
    }
    return {
        "assets_loaded": [(a, len(c), src) for a, c, _, src in loaded],
        "summary": summary,
        "by_class": by_class,
        "trades": results,
    }


def _print_summary_block(label: str, stats: dict[str, Any]) -> None:
    print(
        f"  {label:<21} | trades={stats['trades']:>4} | win_28={stats['win_rate_pct']:>5.1f}% | "
        f"gross={stats['avg_gross_pct']:>+6.3f}% | net_0.28%={stats['avg_net_28_pct']:>+6.3f}% | "
        f"net_0.45%={stats['avg_net_45_pct']:>+6.3f}% | PF={stats['profit_factor_28']:>5.2f} | "
        f"MaxDD={stats['max_dd_28_pct']:>5.2f}% | TP/SL/TS={stats['tp_pct']:.0f}/{stats['sl_pct']:.0f}/{stats['time_stop_pct']:.0f}%"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Walk-forward OOS backtester untuk price_forecast.py")
    parser.add_argument("--interval", default="1h", choices=("15m", "1h", "4h", "1d"))
    parser.add_argument("--limit", type=int, default=720)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--sl-mode", default="both", choices=("atr", "fixed_15", "both"))
    parser.add_argument("--min-net-edge", type=float, default=DEFAULT_MIN_NET_EDGE_PCT)
    args = parser.parse_args(argv)

    modes = ["fixed_15", "atr"] if args.sl_mode == "both" else [args.sl_mode]
    assets = CRYPTO_UNIVERSE + STOCK_UNIVERSE
    print(f"=== Walk-Forward 24h Price-Delta & Forecast Backtest (interval={args.interval}, limit={args.limit}, folds={args.folds}) ===")
    for mode in modes:
        print(f"\n--- SL/TP Mode: {mode} (min_net_edge={args.min_net_edge*100:.2f}%) ---")
        report = run_walk_forward(
            assets,
            interval=args.interval,
            limit=args.limit,
            folds=args.folds,
            sl_mode=mode,  # type: ignore[arg-type]
            min_net_edge_pct=args.min_net_edge,
        )
        print(f"Aset dimuat ({len(report['assets_loaded'])}/{len(assets)}): " + ", ".join(f"{a}({n},{s})" for a, n, s in report["assets_loaded"]))
        for k in ("COMBINED", "MOMENTUM_BREAKOUT", "MEAN_REVERSION_FADE", "NULL_FLIPPED"):
            _print_summary_block(k, report["summary"][k])
        print("Breakdown per Kelas Aset (COMBINED):")
        for cls_name in ("crypto", "stock"):
            _print_summary_block(cls_name.upper(), report["by_class"][cls_name])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
