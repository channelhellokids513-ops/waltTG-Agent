"""gravity_probe - measure Market Gravity on real bars, read-only.

Purpose (Fase 1/5 of the port): the model needs a wide bar window, and the cost of
fetching it is a fact to be measured, not assumed. This tool reports, per asset:
bars actually returned, which source answered, wall-clock seconds, and the resulting
structural read-out (net gravity, nearest magnets, regime). It writes nothing.

Usage:
    python tools/gravity_probe.py BTC ETH            # default 1h, 1000 bars
    python tools/gravity_probe.py --interval 4h --limit 500 BTC
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_chart import fetch_candles  # noqa: E402
from market_gravity import GravityParams, analyze_market_gravity  # noqa: E402


def probe(asset: str, interval: str, limit: int, params: GravityParams) -> dict:
    started = time.perf_counter()
    candles, source = fetch_candles(asset, interval=interval, limit=limit)
    fetched = time.perf_counter() - started
    started = time.perf_counter()
    result = analyze_market_gravity(candles, params)
    computed = time.perf_counter() - started
    top_up = result.upper[0] if result.upper else None
    top_dn = result.lower[0] if result.lower else None
    return {
        "asset": asset,
        "source": source,
        "bars": len(candles),
        "seconds_fetch": round(fetched, 2),
        "seconds_compute": round(computed, 3),
        "status": result.status,
        "net_gravity": round(result.net_gravity, 1),
        "bull_pressure": round(result.bullish_pressure, 1),
        "regime": result.regime,
        "trend_bias": round(result.trend_bias, 3),
        "magnets_up": len(result.upper),
        "magnets_down": len(result.lower),
        "upper_1": None if top_up is None else f"{top_up.price:.6g} ({top_up.score:.1f}, {top_up.state}, {top_up.distance_atr:.2f} ATR)",
        "lower_1": None if top_dn is None else f"{top_dn.price:.6g} ({top_dn.score:.1f}, {top_dn.state}, {top_dn.distance_atr:.2f} ATR)",
        "pools": f"{result.pools_upper}u/{result.pools_lower}d",
        "notes": result.notes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure Market Gravity on live bars (read-only).")
    parser.add_argument("assets", nargs="*", default=["BTC"])
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()

    params = GravityParams()
    minimum = max(params.slow_ema_length, params.volume_length, params.atr_length) + params.pivot_right + 1
    print(f"Market Gravity probe | interval={args.interval} limit={args.limit} model_min_bars={minimum}")
    total_fetch = 0.0
    for asset in args.assets:
        row = probe(asset, args.interval, args.limit, params)
        total_fetch += row["seconds_fetch"]
        print(
            f"\n{row['asset']:<8} source={row['source']:<15} bars={row['bars']:<5} "
            f"fetch={row['seconds_fetch']}s compute={row['seconds_compute']}s status={row['status']}"
        )
        print(f"  net_gravity={row['net_gravity']}  bull_pressure={row['bull_pressure']}  regime={row['regime']}  trend_bias={row['trend_bias']}")
        print(f"  magnets: {row['magnets_up']} up / {row['magnets_down']} down   pools stored: {row['pools']}")
        if row["upper_1"]:
            print(f"  upside magnet : {row['upper_1']}")
        if row["lower_1"]:
            print(f"  downside magnet: {row['lower_1']}")
        if row["notes"]:
            print(f"  notes: {'; '.join(row['notes'])}")
    print(f"\ntotal fetch seconds for {len(args.assets)} asset(s): {round(total_fetch, 2)}s")
    print("read-only: nothing was written to the ledger, the sheet, or a position")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
