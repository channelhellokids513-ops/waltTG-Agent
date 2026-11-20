"""Market Gravity - deterministic liquidity-magnet scorer (Python port).

Ported from the "Market Gravity" TradingView Pine Script v6 indicator supplied by the
owner. Source notice retained verbatim as required by the licence:

    This Pine Script(TM) code is subject to the terms of the Mozilla Public License 2.0
    at https://mozilla.org/MPL/2.0/
    Join our channel for more free tools: https://t.me/simpleforextools

Every constant and formula here is transcribed from that source with line numbers
recorded in ``MG_PORT_SPEC.md``; nothing was written from memory.

Semantics preserved deliberately:
* Gravity scores are RELATIVE ATTRACTION metrics, NOT probabilities. They must never
  be presented, scaled or logged as an edge estimate.
* Structural Liquidity Strength is independent of current price; distance, freshness,
  trend and regime are applied only as separate modifiers on top of it.
* Pivots only exist ``pivot_right`` bars after they form (no lookahead).
* Higher-timeframe structures use only COMPLETED day/week buckets.

Deliberate deviations from Pine (documented, measured in tests):
* Stateless: the whole window is replayed from bar 0 each call instead of keeping
  ``var`` arrays across bars. Same final state, no stale memory between CI runs.
* Warm-up differences in recursive indicators (Pine seeds EMA/ATR differently), so
  parity with TradingView is asserted within a tolerance, not bit-exactly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Parameters (MG_PORT_SPEC.md section 1; defaults are the indicator's defaults)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GravityParams:
    # Liquidity detection (L27-L84)
    pivot_left: int = 5
    pivot_right: int = 5
    cluster_atr: float = 0.15
    sweep_buffer_atr: float = 0.05
    max_age_bars: int = 1500
    max_stored_levels: int = 40
    max_target_distance_atr: float = 12.0
    # Liquidity sources (L87-L171)
    use_swing_liquidity: bool = True
    use_equal_high_low: bool = True
    use_major_swings: bool = True
    use_previous_day: bool = True
    use_previous_week: bool = True
    use_repeated_rejections: bool = True
    equal_tolerance_atr: float = 0.15
    major_swing_atr: float = 2.50
    rejection_min_separation_bars: int = 8
    rejection_wick_ratio: float = 0.30
    recent_pivot_memory: int = 30
    # Pool engine (L173-L207)
    independent_touch_min_separation_bars: int = 8
    cluster_zone_padding_atr: float = 0.02
    isolated_zone_half_atr: float = 0.05
    max_zone_width_atr: float = 0.50
    # Gravity model (L209-L286)
    atr_length: int = 14
    fast_ema_length: int = 21
    slow_ema_length: int = 55
    volume_length: int = 50
    approaching_atr: float = 0.75
    accessibility_k_atr: float = 2.0
    swing_half_life_bars: int = 150
    cluster_half_life_bars: int = 250
    previous_day_half_life_bars: int = 300
    previous_week_half_life_bars: int = 500
    # Lifecycle (L288-L324)
    active_strength_threshold: float = 40.0
    rejection_window_bars: int = 2
    acceptance_close_count: int = 2
    lifecycle_event_display_bars: int = 3

    # Rank weights are hardcoded in the source (L246-L249)
    primary_rank_weight: float = 1.00
    secondary_rank_weight: float = 0.65
    tertiary_rank_weight: float = 0.40

    # Structure member weights (L2879-L2882, L3164, L3256)
    swing_member_weight: float = 1.00
    major_member_weight: float = 1.25
    previous_day_member_weight: float = 1.15
    previous_week_member_weight: float = 1.30

    # Strength component weights (L1236-L1244)
    w_density: float = 0.30
    w_prominence: float = 0.25
    w_touch: float = 0.20
    w_timeframe: float = 0.15
    w_volume: float = 0.10


# Lifecycle state codes (L528-L534)
FORMING, ACTIVE, APPROACHING, SWEEPING, REJECTED, ACCEPTED, RETIRED = range(7)
STATE_NAMES = {
    FORMING: "Forming",
    ACTIVE: "Active",
    APPROACHING: "Approaching",
    SWEEPING: "Sweeping",
    REJECTED: "Rejected",
    ACCEPTED: "Accepted",
    RETIRED: "Retired",
}
# Only these may merge with a new structure (L868-L871)
MERGEABLE_STATES = (FORMING, ACTIVE, APPROACHING)
# Only these may be ranked as targets (L1632-L1634)
RANK_ELIGIBLE_STATES = (ACTIVE, APPROACHING)

_LN2 = 0.6931471805599453  # constant used verbatim by the source (L1391)


def clamp01(value: float) -> float:
    """``clamp01`` (L630)."""
    return max(0.0, min(1.0, value))


def clamp_signed(value: float) -> float:
    """``clampSigned`` (L636)."""
    return max(-1.0, min(1.0, value))


def contains_source(sources: str, name: str) -> bool:
    """``containsSource`` (L1111-L1128): " + " delimited membership test."""
    return f" + {name} + " in f" + {sources} + "


def append_source(current: str, new: str) -> str:
    """``appendSource`` (L657): join with " + ", skipping empties and duplicates."""
    if not new:
        return current
    if not current:
        return new
    parts = [piece for piece in current.split(" + ") if piece]
    if new in parts:
        return current
    return f"{current} + {new}"


# ---------------------------------------------------------------------------
# Bar-series indicators (Pine ``ta.*`` equivalents)
# ---------------------------------------------------------------------------


# Candle keys differ by producer: market_chart.py emits ``t/o/h/l/c`` while other feeds
# use full names. Aliasing here keeps the model usable without inventing fields - and a
# feed without volume simply produces no volume evidence, which the strength model
# already handles as a zero component rather than a fabricated one.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c"),
    "volume": ("volume", "v", "vol", "quote_volume", "volumeUsd"),
    "time": ("time", "timestamp", "ts", "t"),
}


def _field(candle: Any, key: str) -> Any:
    for name in FIELD_ALIASES.get(key, (key,)):
        if isinstance(candle, Mapping):
            if name in candle:
                return candle[name]
        else:
            value = getattr(candle, name, None)
            if value is not None:
                return value
    return None


def _series(candles: Sequence[Any], key: str) -> list[float]:
    values: list[float] = []
    for candle in candles:
        try:
            values.append(float(_field(candle, key)))
        except (TypeError, ValueError):
            values.append(float("nan"))
    return values



def sma(values: Sequence[float], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    window = 0.0
    for index, value in enumerate(values):
        if math.isnan(value):
            continue
        window += value
        if index >= length:
            window -= values[index - length]
        if index >= length - 1:
            out[index] = window / length
    return out


def ema(values: Sequence[float], length: int) -> list[float | None]:
    """SMA-seeded EMA. Pine seeds recursion slightly differently; a warm-up deviation."""
    out: list[float | None] = [None] * len(values)
    if length <= 0 or len(values) < length:
        return out
    alpha = 2.0 / (length + 1.0)
    previous = values[length - 1]
    out[length - 1] = previous
    for index in range(length, len(values)):
        previous = alpha * values[index] + (1 - alpha) * previous
        out[index] = previous
    return out


def rma(values: Sequence[float], length: int) -> list[float | None]:
    """Wilder smoothing - the basis of ``ta.atr`` and ``ta.dmi``."""
    out: list[float | None] = [None] * len(values)
    if length <= 0 or len(values) < length:
        return out
    previous = sum(values[:length]) / length
    out[length - 1] = previous
    for index in range(length, len(values)):
        previous = (previous * (length - 1) + values[index]) / length
        out[index] = previous
    return out


def true_range(high, low, close) -> list[float]:
    out = [high[0] - low[0]]
    for index in range(1, len(high)):
        prev_close = close[index - 1]
        out.append(max(high[index] - low[index], abs(high[index] - prev_close), abs(low[index] - prev_close)))
    return out


def atr_series(high, low, close, length: int) -> list[float | None]:
    """``ta.atr(length)`` (L482): RMA of true range."""
    return rma(true_range(high, low, close), length)


def adx_series(high, low, close, length: int = 14) -> list[float | None]:
    """Third return value of ``ta.dmi(14, 14)`` (L2720-L2723)."""
    size = len(high)
    plus_dm = [0.0] * size
    minus_dm = [0.0] * size
    for index in range(1, size):
        up = high[index] - high[index - 1]
        down = low[index - 1] - low[index]
        plus_dm[index] = up if (up > down and up > 0) else 0.0
        minus_dm[index] = down if (down > up and down > 0) else 0.0
    smoothed_tr = rma(true_range(high, low, close), length)
    smoothed_plus = rma(plus_dm, length)
    smoothed_minus = rma(minus_dm, length)
    dx: list[float] = []
    for index in range(size):
        base = smoothed_tr[index]
        if base is None or base <= 0:
            dx.append(0.0)
            continue
        plus_di = 100.0 * (smoothed_plus[index] or 0.0) / base
        minus_di = 100.0 * (smoothed_minus[index] or 0.0) / base
        total = plus_di + minus_di
        dx.append(0.0 if total <= 0 else 100.0 * abs(plus_di - minus_di) / total)
    return rma(dx, length)


# ---------------------------------------------------------------------------
# Structural Liquidity Strength (L1130-L1246) - price-independent by design
# ---------------------------------------------------------------------------


def density_score(member_count: int) -> float:
    if member_count <= 0:
        return 0.0
    return clamp01(0.20 + 0.80 * (1.0 - math.exp(-0.70 * max(member_count - 1, 0))))


def touch_quality_score(independent_touches: int) -> float:
    if independent_touches <= 0:
        return 0.0
    return clamp01(0.20 + 0.80 * (1.0 - math.exp(-0.80 * max(independent_touches - 1, 0))))


def timeframe_importance_score(sources: str) -> float:
    if contains_source(sources, "Previous Week High") or contains_source(sources, "Previous Week Low"):
        return 1.00
    if contains_source(sources, "Previous Day High") or contains_source(sources, "Previous Day Low"):
        return 0.70
    return 0.35


def base_half_life(pool: "LiquidityPool", params: GravityParams) -> float:
    """``getBaseFreshnessHalfLife`` (L1331-L1357)."""
    if contains_source(pool.sources, "Previous Week High") or contains_source(pool.sources, "Previous Week Low"):
        return float(params.previous_week_half_life_bars)
    if contains_source(pool.sources, "Previous Day High") or contains_source(pool.sources, "Previous Day Low"):
        return float(params.previous_day_half_life_bars)
    clustered = (
        pool.member_count >= 2
        or pool.independent_touches >= 2
        or contains_source(pool.sources, "Equal High")
        or contains_source(pool.sources, "Equal Low")
        or contains_source(pool.sources, "Repeated Rejection")
    )
    return float(params.cluster_half_life_bars) if clustered else float(params.swing_half_life_bars)


def distance_to_zone_atr(zone_low: float, zone_high: float, close: float, current_atr: float) -> float:
    """``getDistanceToZoneATR`` (L1314-L1329): current close to the nearer zone edge."""
    if current_atr <= 0:
        return 0.0
    if close < zone_low:
        return (zone_low - close) / current_atr
    if close > zone_high:
        return (close - zone_high) / current_atr
    return 0.0


def adaptive_trend_scale(timeframe_seconds: float) -> float:
    """``getAdaptiveTrendInfluenceScale`` (L1406-L1428)."""
    if not timeframe_seconds or timeframe_seconds <= 0:
        return 1.00
    five_minutes = 300.0
    fifteen_minutes = 15.0 * 60.0
    four_hours = 4.0 * 60.0 * 60.0
    one_day = 24.0 * 60.0 * 60.0
    if timeframe_seconds <= five_minutes:
        scale = 2.00
    elif timeframe_seconds < fifteen_minutes:
        scale = 2.00 + 0.50 * math.log(timeframe_seconds / five_minutes) / math.log(fifteen_minutes / five_minutes)
    elif timeframe_seconds <= four_hours:
        scale = 2.50
    elif timeframe_seconds < one_day:
        scale = 2.50 - 1.50 * math.log(timeframe_seconds / four_hours) / math.log(one_day / four_hours)
    else:
        scale = 1.00
    return max(1.00, min(2.50, scale))


def trend_modifier(trend_bias: float, direction: int, timeframe_seconds: float) -> float:
    """``getTrendModifier`` (L1430-L1448)."""
    directional_trend = clamp_signed(float(direction) * trend_bias)
    return max(0.75, min(1.25, 1.0 + 0.10 * adaptive_trend_scale(timeframe_seconds) * directional_trend))


def regime_modifier(regime: str, trend_bias: float, direction: int, sources: str) -> float:
    """``getRegimeModifier`` (L1450-L1494)."""
    directional_trend = clamp_signed(float(direction) * trend_bias)
    is_range_structure = (
        contains_source(sources, "Equal High")
        or contains_source(sources, "Equal Low")
        or contains_source(sources, "Repeated Rejection")
    )
    if regime == "Trend Expansion":
        raw = 1.0 + 0.10 * directional_trend
    elif regime == "Trend":
        raw = 1.0 + 0.05 * directional_trend
    elif regime == "Range":
        raw = 1.05 if is_range_structure else 1.00
    elif regime == "Compression":
        raw = 0.92
    else:
        raw = 1.00
    bounded = max(0.90, min(1.10, raw))
    return max(0.80, min(1.20, 1.0 + (bounded - 1.0) * 2.00))


def strength_components(pool: "LiquidityPool", params: GravityParams) -> dict[str, float]:
    """``getLiquidityStrengthComponents`` (L1197-L1246)."""
    density = density_score(pool.member_count)
    prominence = clamp01(pool.max_swing_prominence) if pool.swing_evidence_count > 0 else 0.0
    touch = touch_quality_score(pool.independent_touches)
    timeframe = timeframe_importance_score(pool.sources)
    volume = (
        clamp01(pool.volume_evidence_sum / pool.volume_evidence_count)
        if pool.volume_evidence_count > 0
        else 0.0
    )
    total = 100.0 * (
        density * params.w_density
        + prominence * params.w_prominence
        + touch * params.w_touch
        + timeframe * params.w_timeframe
        + volume * params.w_volume
    )
    return {
        "density": density,
        "prominence": prominence,
        "touch": touch,
        "timeframe": timeframe,
        "volume": volume,
        "liquidity_strength": total,
    }


def freshness_value(pool: "LiquidityPool", strength: float, bar_index: int, params: GravityParams) -> dict[str, float]:
    """``getFreshnessComponents`` (L1359-L1396)."""
    age = max(bar_index - pool.active_bar, 0)
    base = base_half_life(pool, params)
    effective = max(1.0, base * (0.75 + 0.75 * clamp01(strength / 100.0)))
    return {
        "age": age,
        "base_half_life": base,
        "effective_half_life": effective,
        "freshness": clamp01(math.exp(-_LN2 * age / effective)),
    }


def gravity_components(
    pool: "LiquidityPool",
    bar_index: int,
    close: float,
    current_atr: float,
    trend_bias: float,
    direction: int,
    regime: str,
    timeframe_seconds: float,
    params: GravityParams,
) -> dict[str, float]:
    """``getGravityComponents`` (L1496-L1567)."""
    parts = strength_components(pool, params)
    fresh = freshness_value(pool, parts["liquidity_strength"], bar_index, params)
    distance = distance_to_zone_atr(pool.zone_low, pool.zone_high, close, current_atr)
    normalized = distance / params.accessibility_k_atr if params.accessibility_k_atr > 0 else distance
    accessibility = 1.0 / (1.0 + normalized * normalized)
    distance_multiplier = 0.55 + 0.45 * accessibility
    freshness_multiplier = 0.70 + 0.30 * fresh["freshness"]
    trend = trend_modifier(trend_bias, direction, timeframe_seconds)
    regime_mult = regime_modifier(regime, trend_bias, direction, pool.sources)
    gravity = max(
        0.0,
        min(100.0, parts["liquidity_strength"] * distance_multiplier * freshness_multiplier * trend * regime_mult),
    )
    return {
        **parts,
        **fresh,
        "distance_atr": distance,
        "accessibility": accessibility,
        "distance_multiplier": distance_multiplier,
        "freshness_multiplier": freshness_multiplier,
        "trend_modifier": trend,
        "regime_modifier": regime_mult,
        "gravity": gravity,
    }


# ---------------------------------------------------------------------------
# Pool engine (L742-L1130)
# ---------------------------------------------------------------------------


@dataclass
class LiquidityPool:
    """One pool - the Python equivalent of one row of Pine's parallel arrays."""

    centroid: float
    origin_bar: int          # ``bars``: earliest structural origin (L1024-L1032)
    active_bar: int          # ``activeBars``: earliest confirmation (L1034-L1043)
    legacy_strength: float   # ``strengths``: 0..1 swing strength, NOT used by gravity
    legacy_volume: float     # ``volumes``: 0..1 relative volume, NOT used by gravity
    sources: str
    zone_low: float
    zone_high: float
    member_min: float
    member_max: float
    member_count: int
    total_weight: float
    last_touch_bar: int
    pool_atr: float
    independent_touches: int
    max_swing_prominence: float
    swing_evidence_count: int
    volume_evidence_sum: float
    volume_evidence_count: int
    state: int = FORMING
    sweep_bar: int = -1
    beyond_close_count: int = 0
    resolution_bar: int = -1
    lifecycle_touch_bar: int = -1


def zone_bounds(
    member_min: float,
    member_max: float,
    member_count: int,
    centroid: float,
    pool_atr: float,
    params: GravityParams,
) -> tuple[float, float]:
    """``getPoolZoneBounds`` (L742-L768). Returns (nan, nan) when ATR is unusable."""
    if pool_atr <= 0:
        return float("nan"), float("nan")
    if member_count <= 1:
        zone_low = centroid - pool_atr * params.isolated_zone_half_atr
        zone_high = centroid + pool_atr * params.isolated_zone_half_atr
    else:
        zone_low = member_min - pool_atr * params.cluster_zone_padding_atr
        zone_high = member_max + pool_atr * params.cluster_zone_padding_atr
    maximum_width = pool_atr * params.max_zone_width_atr
    if maximum_width > 0 and (zone_high - zone_low) > maximum_width:
        midpoint = (zone_high + zone_low) / 2.0
        zone_low = midpoint - maximum_width / 2.0
        zone_high = midpoint + maximum_width / 2.0
    return zone_low, zone_high


class SidePools:
    """One side's pool set plus the confirmed-pivot memory used to classify them."""

    def __init__(self, params: GravityParams) -> None:
        self.params = params
        self.pools: list[LiquidityPool] = []
        self.recent_prices: list[float] = []
        self.recent_bars: list[int] = []

    def recent_match_count(self, price: float, bar: int, tolerance: float, min_separation: int) -> int:
        """``getRecentMatchCount``: pivots near this price and far enough apart in time."""
        return sum(
            1
            for stored_price, stored_bar in zip(self.recent_prices, self.recent_bars)
            if abs(stored_price - price) <= tolerance and abs(bar - stored_bar) >= min_separation
        )

    def store_recent_pivot(self, price: float, bar: int) -> None:
        """``storeRecentPivot``: bounded memory, classification only (L164-L171)."""
        self.recent_prices.append(price)
        self.recent_bars.append(bar)
        overflow = len(self.recent_prices) - self.params.recent_pivot_memory
        if overflow > 0:
            del self.recent_prices[:overflow]
            del self.recent_bars[:overflow]

    def _nearest_index(self, price: float, tolerance: float) -> int:
        """Nearest mergeable pool within tolerance (L865-L882)."""
        best_index, best_distance = -1, float("inf")
        for index, pool in enumerate(self.pools):
            if pool.state not in MERGEABLE_STATES:
                continue
            distance = abs(pool.centroid - price)
            if distance <= tolerance and distance < best_distance:
                best_index, best_distance = index, distance
        return best_index

    def add_member(
        self,
        *,
        price: float,
        new_bar: int,
        new_active_bar: int,
        strength: float,
        volume: float,
        swing_prominence: float | None,
        volume_score: float | None,
        source: str,
        member_weight: float,
        new_atr: float,
        counts_as_touch: bool,
        touch_bar: int,
        tolerance: float,
    ) -> None:
        """``addPoolMember`` (L830-L1106): merge into the nearest eligible pool or open one."""
        params = self.params
        safe_weight = max(member_weight, 0.01)
        safe_atr = new_atr if new_atr > 0 else float("nan")
        has_swing = swing_prominence is not None and not math.isnan(swing_prominence)
        has_volume = volume_score is not None and not math.isnan(volume_score)
        prominence = clamp01(swing_prominence) if has_swing else 0.0
        evidence = clamp01(volume_score) if has_volume else 0.0
        nearest = self._nearest_index(price, tolerance)

        if nearest < 0:
            zone_low, zone_high = zone_bounds(price, price, 1, price, safe_atr, params)
            self.pools.append(
                LiquidityPool(
                    centroid=price,
                    origin_bar=new_bar,
                    active_bar=new_active_bar,
                    legacy_strength=strength,
                    legacy_volume=volume,
                    sources=source,
                    zone_low=zone_low,
                    zone_high=zone_high,
                    member_min=price,
                    member_max=price,
                    member_count=1,
                    total_weight=safe_weight,
                    # Every structure begins with one observed formation event (L915-L918).
                    independent_touches=1,
                    last_touch_bar=touch_bar if counts_as_touch else new_bar,
                    pool_atr=safe_atr,
                    max_swing_prominence=prominence,
                    swing_evidence_count=1 if has_swing else 0,
                    volume_evidence_sum=evidence,
                    volume_evidence_count=1 if has_volume else 0,
                )
            )
        else:
            pool = self.pools[nearest]
            if math.isnan(pool.pool_atr):
                pool.pool_atr = safe_atr
            combined = pool.total_weight + safe_weight
            weight = pool.total_weight
            pool.centroid = (pool.centroid * weight + price * safe_weight) / combined
            pool.legacy_strength = (pool.legacy_strength * weight + strength * safe_weight) / combined
            pool.legacy_volume = (pool.legacy_volume * weight + volume * safe_weight) / combined
            pool.pool_atr = (pool.pool_atr * weight + safe_atr * safe_weight) / combined
            pool.member_min = min(pool.member_min, price)
            pool.member_max = max(pool.member_max, price)
            pool.member_count += 1
            if counts_as_touch and abs(touch_bar - pool.last_touch_bar) >= params.independent_touch_min_separation_bars:
                pool.independent_touches += 1
                pool.last_touch_bar = touch_bar
            if has_swing:
                pool.max_swing_prominence = max(pool.max_swing_prominence, prominence)
                pool.swing_evidence_count += 1
            if has_volume:
                pool.volume_evidence_sum += evidence
                pool.volume_evidence_count += 1
            pool.sources = append_source(pool.sources, source)
            pool.total_weight = combined
            # Earliest origin and earliest confirmation are preserved (L1024-L1043).
            pool.origin_bar = min(pool.origin_bar, new_bar)
            pool.active_bar = min(pool.active_bar, new_active_bar)
            pool.zone_low, pool.zone_high = zone_bounds(
                pool.member_min, pool.member_max, pool.member_count, pool.centroid, pool.pool_atr, params
            )

        while len(self.pools) > params.max_stored_levels:
            oldest = min(range(len(self.pools)), key=lambda index: self.pools[index].active_bar)
            del self.pools[oldest]

    # -- lifecycle (L1927-L2075) --------------------------------------------
    def update_lifecycle(
        self,
        *,
        bar_index: int,
        high: float,
        low: float,
        close: float,
        current_atr: float,
        direction: int,
    ) -> list[str]:
        """``updateLifecycle`` for one confirmed bar. Returns the events raised."""
        params = self.params
        events: list[str] = []
        for pool in self.pools:
            if math.isnan(pool.zone_low) or math.isnan(pool.zone_high) or math.isnan(pool.pool_atr):
                continue
            strength = strength_components(pool, params)["liquidity_strength"]
            distance = distance_to_zone_atr(pool.zone_low, pool.zone_high, close, current_atr)
            zone_touched = high >= pool.zone_low if direction == 1 else low <= pool.zone_high
            boundary = (
                pool.zone_high + current_atr * params.sweep_buffer_atr
                if direction == 1
                else pool.zone_low - current_atr * params.sweep_buffer_atr
            )
            swept = high >= boundary if direction == 1 else low <= boundary
            close_beyond = close > pool.zone_high if direction == 1 else close < pool.zone_low
            close_original_side = close < pool.zone_low if direction == 1 else close > pool.zone_high

            if pool.state in MERGEABLE_STATES:
                if swept and bar_index >= pool.active_bar:
                    pool.sweep_bar = bar_index
                    pool.beyond_close_count = 1 if close_beyond else 0
                    events.append("Sweep")
                    if pool.beyond_close_count >= params.acceptance_close_count:
                        pool.state = ACCEPTED
                        pool.resolution_bar = bar_index
                        events.append("Accepted")
                    else:
                        pool.state = SWEEPING
                else:
                    if strength < params.active_strength_threshold:
                        pool.state = FORMING
                    elif distance <= params.approaching_atr:
                        pool.state = APPROACHING
                    else:
                        pool.state = ACTIVE
                    if (
                        zone_touched
                        and not swept
                        and bar_index > pool.active_bar
                        and (
                            pool.lifecycle_touch_bar < 0
                            or bar_index - pool.lifecycle_touch_bar >= params.independent_touch_min_separation_bars
                        )
                    ):
                        pool.lifecycle_touch_bar = bar_index
                        events.append("Touch")

            elif pool.state == SWEEPING:
                bars_since_sweep = bar_index - pool.sweep_bar
                pool.beyond_close_count = pool.beyond_close_count + 1 if close_beyond else 0
                if pool.beyond_close_count >= params.acceptance_close_count:
                    pool.state = ACCEPTED
                    pool.resolution_bar = bar_index
                    events.append("Accepted")
                elif bars_since_sweep <= params.rejection_window_bars and close_original_side:
                    pool.state = REJECTED
                    pool.resolution_bar = bar_index
                    events.append("Rejected")
                elif bars_since_sweep > params.rejection_window_bars:
                    pool.state = RETIRED
                    pool.resolution_bar = bar_index

            elif pool.state in (REJECTED, ACCEPTED):
                if pool.resolution_bar >= 0 and bar_index - pool.resolution_bar >= params.lifecycle_event_display_bars:
                    pool.state = RETIRED
                    pool.resolution_bar = bar_index
        return events

    def expire(self, bar_index: int) -> None:
        """Hard expiry and one-bar-after-retirement removal (L3369-L3449)."""
        keep: list[LiquidityPool] = []
        for pool in self.pools:
            expired = bar_index - pool.active_bar > self.params.max_age_bars
            retired_out = pool.state == RETIRED and pool.resolution_bar >= 0 and bar_index > pool.resolution_bar
            if not (expired or retired_out):
                keep.append(pool)
        self.pools = keep

    def eligible(self, close: float, current_atr: float, direction: int) -> list[LiquidityPool]:
        """``getBestIndex`` eligibility (L1631-L1647)."""
        params = self.params
        result = []
        for pool in self.pools:
            if pool.state not in RANK_ELIGIBLE_STATES:
                continue
            if math.isnan(pool.zone_low) or math.isnan(pool.zone_high):
                continue
            if direction == 1 and pool.zone_high < close:
                continue
            if direction == -1 and pool.zone_low > close:
                continue
            if distance_to_zone_atr(pool.zone_low, pool.zone_high, close, current_atr) > params.max_target_distance_atr:
                continue
            result.append(pool)
        return result


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------


def _timestamps(candles: Sequence[Any]) -> list[float | None]:
    """Bar open times in seconds; None when the feed carries no usable timestamp."""
    out: list[float | None] = []
    for candle in candles:
        raw = _field(candle, "time")
        value: float | None = None
        if isinstance(raw, (int, float)):
            value = float(raw) / 1000.0 if float(raw) > 1e12 else float(raw)
        elif isinstance(raw, str) and raw:
            try:
                from datetime import datetime

                value = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                value = None
        out.append(value)
    return out



def _timeframe_seconds(times: Sequence[float | None]) -> float | None:
    deltas = sorted(
        {right - left for left, right in zip(times, times[1:]) if left and right and right > left}
    )
    if not deltas:
        return None
    return deltas[len(deltas) // 2]


def _bucket_index(time: float | None, timeframe_seconds: float | None, mode: str) -> int | None:
    """Calendar bucket for a bar: 'day' or 'week'. None when it cannot be determined."""
    if time is None:
        return None
    if mode == "day":
        return int(time // 86_400)
    return int(time // 604_800)


def _pivot_indexes(
    high: Sequence[float], low: Sequence[float], left: int, right: int
) -> tuple[dict[int, int], dict[int, int]]:
    """Confirmed pivot highs/lows: ``{confirmation_bar: pivot_bar}`` (L489-L499).

    A pivot at bar *c* is only knowable on bar *c + right*, which is what keeps the
    port free of lookahead. Ties resolve to the first strict maximum on the right
    side; Pine's plateau handling is not fully specified, so equal-high/low
    classification carries plateaus instead.
    """
    highs: dict[int, int] = {}
    lows: dict[int, int] = {}
    size = len(high)
    for center in range(left, size - right):
        window = range(center - left, center + right + 1)
        center_high = high[center]
        if any(math.isnan(high[i]) for i in window):
            continue
        if center_high >= max(high[i] for i in window) and all(center_high > high[i] for i in range(center + 1, center + right + 1)):
            highs[center + right] = center
        center_low = low[center]
        if any(math.isnan(low[i]) for i in window):
            continue
        if center_low <= min(low[i] for i in window) and all(center_low < low[i] for i in range(center + 1, center + right + 1)):
            lows[center + right] = center
    return highs, lows


def _sma_of_series(values: Sequence[float | None], length: int) -> list[float | None]:
    """SMA over a series that may contain ``None`` (needs `length` real values)."""
    out: list[float | None] = [None] * len(values)
    for index in range(length - 1, len(values)):
        window = values[index - length + 1 : index + 1]
        usable = [value for value in window if value is not None and not math.isnan(value)]
        if len(usable) == length:
            out[index] = sum(usable) / length
    return out


def _record_pivot(
    side: SidePools,
    direction: int,
    *,
    index: int,
    pivot_bar: int,
    current_atr: float,
    high: Sequence[float],
    low: Sequence[float],
    open_: Sequence[float],
    close: Sequence[float],
    volume: Sequence[float],
    volume_average: Sequence[float | None],
    params: GravityParams,
) -> None:
    """One confirmed pivot -> classified structure -> pool member (L2744-L3123).

    ``direction`` +1 is the upper side, -1 the lower side; the source names below are
    the exact strings ``contains_source`` matches on, so they are load-bearing.
    """
    upper = direction == 1
    price = high[pivot_bar] if upper else low[pivot_bar]
    span = range(pivot_bar - params.pivot_left, pivot_bar + params.pivot_right + 1)
    around = (min if upper else max)((low if upper else high)[bar] for bar in span)
    raw_strength = ((price - around) if upper else (around - price)) / current_atr
    swing_strength = clamp01(raw_strength / 4.0)

    bar_volume = volume[pivot_bar]
    average_volume = volume_average[pivot_bar]
    structural_volume = float("nan")
    if average_volume is not None and not math.isnan(average_volume) and average_volume > 0 and not math.isnan(bar_volume):
        structural_volume = clamp01((bar_volume / average_volume) / 2.0)

    pivot_range = high[pivot_bar] - low[pivot_bar]
    body_extreme = max(open_[pivot_bar], close[pivot_bar]) if upper else min(open_[pivot_bar], close[pivot_bar])
    wick = (high[pivot_bar] - body_extreme) if upper else (body_extreme - low[pivot_bar])
    wick_ratio = wick / pivot_range if pivot_range > 0 else 0.0

    tolerance = current_atr * params.equal_tolerance_atr
    equal_matches = side.recent_match_count(price, pivot_bar, tolerance, max(1, params.pivot_right))
    rejection_matches = side.recent_match_count(price, pivot_bar, tolerance, params.rejection_min_separation_bars)
    is_major = params.use_major_swings and raw_strength >= params.major_swing_atr

    source = ""
    if params.use_swing_liquidity:
        source = append_source(source, "Swing High" if upper else "Swing Low")
    if params.use_equal_high_low and equal_matches > 0:
        source = append_source(source, "Equal High" if upper else "Equal Low")
    if is_major:
        source = append_source(source, "Major Swing High" if upper else "Major Swing Low")
    if params.use_repeated_rejections and rejection_matches > 0 and wick_ratio >= params.rejection_wick_ratio:
        source = append_source(source, "Repeated Rejection")

    if source:
        side.add_member(
            price=price,
            new_bar=pivot_bar,
            new_active_bar=index,
            strength=swing_strength,
            volume=0.50 if math.isnan(structural_volume) else structural_volume,
            swing_prominence=swing_strength,
            volume_score=structural_volume,
            source=source,
            member_weight=params.major_member_weight if is_major else params.swing_member_weight,
            new_atr=current_atr,
            counts_as_touch=True,
            touch_bar=pivot_bar,
            tolerance=current_atr * params.cluster_atr,
        )
    side.store_recent_pivot(price, pivot_bar)


@dataclass
class Magnet:
    """One ranked liquidity magnet."""

    price: float
    zone_low: float
    zone_high: float
    score: float
    state: str
    strength: float
    distance_atr: float
    accessibility: float
    freshness: float
    member_count: int
    touches: int
    age: int
    sources: str


@dataclass
class GravitySnapshot:
    """Per-bar causal read-out, produced by ``analyze_market_gravity(trace_every=...)``.

    Only information available up to ``bar_index`` is used, so a snapshot can feed a
    backtest without leaking the future.
    """

    bar_index: int
    close: float
    atr: float
    trend_bias: float
    regime: str
    net_gravity: float
    bullish_pressure: float
    upper: list[Magnet] = field(default_factory=list)
    lower: list[Magnet] = field(default_factory=list)
    rejected_up: list[float] = field(default_factory=list)
    rejected_down: list[float] = field(default_factory=list)
    accepted_up: list[float] = field(default_factory=list)
    accepted_down: list[float] = field(default_factory=list)


def comfort_points(snapshot: GravitySnapshot) -> dict[str, float]:
    """Centre of mass of the ranked magnets - the reference document's key extension.

    ``structural`` is the pure balance point of upper and lower magnets weighted by
    rank weight x gravity score; ``anchored`` adds the current close as an inertial
    anchor so the equilibrium cannot fly off when one side is thin. Weighting by score
    is deliberate: score is relative attraction, the only quantity the source model
    claims to measure. It is NOT a probability.
    """
    weights = (1.00, 0.65, 0.40)
    numerator = 0.0
    denominator = 0.0
    for side in (snapshot.upper, snapshot.lower):
        for position, magnet in enumerate(side[:3]):
            weight = weights[position] * magnet.score
            numerator += weight * magnet.price
            denominator += weight
    structural = (numerator / denominator) if denominator > 0 else snapshot.close
    anchor_weight = 50.0
    anchored = ((numerator + anchor_weight * snapshot.close) / (denominator + anchor_weight)
                if denominator > 0 else snapshot.close)
    atr = snapshot.atr
    return {
        "structural": structural,
        "anchored": anchored,
        "distance_atr": (structural - snapshot.close) / atr if atr > 0 else 0.0,
        "evidence_weight": denominator,
    }



@dataclass
class MarketGravityResult:
    """Deterministic structural read-out. Gravity scores are relative attraction, NOT probabilities."""

    status: str
    notes: list[str] = field(default_factory=list)
    bars: int = 0
    close: float | None = None
    atr: float | None = None
    timeframe_seconds: float | None = None
    trend_bias: float = 0.0
    regime: str = "Range"
    upper: list[Magnet] = field(default_factory=list)
    lower: list[Magnet] = field(default_factory=list)
    upper_pull: float = 0.0
    lower_pull: float = 0.0
    bullish_pressure: float = 50.0
    bearish_pressure: float = 50.0
    net_gravity: float = 0.0
    pools_upper: int = 0
    pools_lower: int = 0
    trace: list[GravitySnapshot] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.status == "ok"

    def to_scan_fields(self) -> dict[str, str]:
        """Diagnostic columns only - deliberately not consumed by any entry decision."""
        if not self.usable:
            return {"mg_status": self.status, "mg_net_gravity": ""}
        top_up = self.upper[0] if self.upper else None
        top_dn = self.lower[0] if self.lower else None
        return {
            "mg_status": self.status,
            "mg_net_gravity": f"{self.net_gravity:.1f}",
            "mg_bull_pressure": f"{self.bullish_pressure:.1f}",
            "mg_regime": self.regime,
            "mg_trend_bias": f"{self.trend_bias:.3f}",
            "mg_upper_1_price": "" if top_up is None else f"{top_up.price:.6g}",
            "mg_upper_1_score": "" if top_up is None else f"{top_up.score:.1f}",
            "mg_upper_1_state": "" if top_up is None else top_up.state,
            "mg_lower_1_price": "" if top_dn is None else f"{top_dn.price:.6g}",
            "mg_lower_1_score": "" if top_dn is None else f"{top_dn.score:.1f}",
            "mg_lower_1_state": "" if top_dn is None else top_dn.state,
        }


def _context_at(index, fast_ema, slow_ema, adx_values, atr_average, atr_value) -> tuple[float, str]:
    """Trend bias and regime at one bar (L2702-L2743), reused by trace and final read-out."""
    bias = 0.0
    if fast_ema[index] is not None and slow_ema[index] is not None and atr_value > 0:
        bias = clamp_signed((fast_ema[index] - slow_ema[index]) / (atr_value * 2.0))
    regime = "Range"
    adx_value, atr_avg = adx_values[index], atr_average[index]
    if adx_value is not None and atr_avg is not None:
        if adx_value >= 25 and atr_value > atr_avg * 1.05:
            regime = "Trend Expansion"
        elif adx_value >= 25:
            regime = "Trend"
        elif atr_value < atr_avg * 0.85:
            regime = "Compression"
    return bias, regime


def _rank_side(
    side: SidePools,
    bar_index: int,
    close: float,
    current_atr: float,
    trend_bias: float,
    direction: int,
    regime: str,
    timeframe_seconds: float,
    params: GravityParams,
) -> tuple[list[Magnet], float]:
    """Top-3 magnets for one side, ranked by gravity (exclusion by argmax picks, as Pine)."""
    weights = (params.primary_rank_weight, params.secondary_rank_weight, params.tertiary_rank_weight)
    scored = [
        (gravity_components(pool, bar_index, close, current_atr, trend_bias, direction, regime,
                            timeframe_seconds, params), pool)
        for pool in side.eligible(close, current_atr, direction)
    ]
    scored.sort(key=lambda pair: pair[0]["gravity"], reverse=True)
    magnets: list[Magnet] = []
    pull = 0.0
    for position, (parts, pool) in enumerate(scored[:3]):
        pull += parts["gravity"] * weights[position]
        magnets.append(
            Magnet(price=pool.centroid, zone_low=pool.zone_low, zone_high=pool.zone_high,
                   score=parts["gravity"], state=STATE_NAMES[pool.state],
                   strength=parts["liquidity_strength"], distance_atr=parts["distance_atr"],
                   accessibility=parts["accessibility"], freshness=parts["freshness"],
                   member_count=pool.member_count, touches=pool.independent_touches,
                   age=int(parts["age"]), sources=pool.sources)
        )
    return magnets, pull


def _add_htf(
    side: SidePools, price: float, source: str, weight: float, index: int, current_atr: float, params: GravityParams
) -> None:
    """Previous day/week structure (L3124-L3307): neutral evidence, no touch credit."""
    if math.isnan(price):
        return
    side.add_member(
        price=price, new_bar=index, new_active_bar=index, strength=0.50, volume=0.50,
        swing_prominence=None, volume_score=None, source=source, member_weight=weight,
        new_atr=current_atr, counts_as_touch=False, touch_bar=index,
        tolerance=current_atr * params.cluster_atr,
    )


def analyze_market_gravity(
    candles: Sequence[Any],
    params: GravityParams | None = None,
    timeframe_seconds: float | None = None,
    trace_every: int = 0,
) -> MarketGravityResult:
    """Replay ``candles`` through the Market Gravity model, reading the final bar.

    Stateless: identical bars always produce identical numbers, which is what makes the
    diagnostic columns reproducible across CI runs.
    """
    params = params or GravityParams()
    size = len(candles)
    minimum = max(params.slow_ema_length, params.volume_length, params.atr_length) + params.pivot_right + 1
    if size < minimum:
        return MarketGravityResult(
            status="insufficient_data", bars=size,
            notes=[f"need >= {minimum} bars for ATR/EMA/volume warm-up plus pivot confirmation"],
        )

    notes: list[str] = []
    high, low = _series(candles, "high"), _series(candles, "low")
    close, open_ = _series(candles, "close"), _series(candles, "open")
    volume = _series(candles, "volume")
    times = _timestamps(candles)

    atr_values = atr_series(high, low, close, params.atr_length)
    fast_ema = ema(close, params.fast_ema_length)
    slow_ema = ema(close, params.slow_ema_length)
    volume_average = sma(volume, params.volume_length)
    adx_values = adx_series(high, low, close, 14)
    atr_average = _sma_of_series(atr_values, params.volume_length)

    if timeframe_seconds is None:
        timeframe_seconds = _timeframe_seconds(times)
        if timeframe_seconds is None:
            timeframe_seconds = 3600.0
            notes.append("timeframe_seconds assumed 3600: bar timestamps unavailable")
    stamped = all(stamp is not None for stamp in times)
    can_use_day = params.use_previous_day and timeframe_seconds <= 86_400
    can_use_week = params.use_previous_week and timeframe_seconds <= 604_800
    if (params.use_previous_day or params.use_previous_week) and not stamped:
        can_use_day = can_use_week = False
        notes.append("previous day/week structures disabled: bar timestamps incomplete")

    pivot_highs, pivot_lows = _pivot_indexes(high, low, params.pivot_left, params.pivot_right)
    upper, lower = SidePools(params), SidePools(params)
    day_bucket = _bucket_index(times[0], timeframe_seconds, "day")
    week_bucket = _bucket_index(times[0], timeframe_seconds, "week")
    day_high = day_low = week_high = week_low = float("nan")
    trace: list[GravitySnapshot] = []

    for index in range(size):
        current_atr = atr_values[index]
        if current_atr is None or current_atr <= 0:
            continue  # warm-up: Pine gates structure detection on not na(atr)

        if index in pivot_highs:
            _record_pivot(upper, 1, index=index, pivot_bar=pivot_highs[index], current_atr=current_atr,
                          high=high, low=low, open_=open_, close=close, volume=volume,
                          volume_average=volume_average, params=params)
        if index in pivot_lows:
            _record_pivot(lower, -1, index=index, pivot_bar=pivot_lows[index], current_atr=current_atr,
                          high=high, low=low, open_=open_, close=close, volume=volume,
                          volume_average=volume_average, params=params)

        bucket = _bucket_index(times[index], timeframe_seconds, "day")
        if can_use_day and bucket is not None and day_bucket is not None and bucket != day_bucket:
            _add_htf(upper, day_high, "Previous Day High", params.previous_day_member_weight, index, current_atr, params)
            _add_htf(lower, day_low, "Previous Day Low", params.previous_day_member_weight, index, current_atr, params)
            day_high = day_low = float("nan")
        if bucket is not None:
            day_bucket = bucket
            day_high = high[index] if math.isnan(day_high) else max(day_high, high[index])
            day_low = low[index] if math.isnan(day_low) else min(day_low, low[index])

        wbucket = _bucket_index(times[index], timeframe_seconds, "week")
        if can_use_week and wbucket is not None and week_bucket is not None and wbucket != week_bucket:
            _add_htf(upper, week_high, "Previous Week High", params.previous_week_member_weight, index, current_atr, params)
            _add_htf(lower, week_low, "Previous Week Low", params.previous_week_member_weight, index, current_atr, params)
            week_high = week_low = float("nan")
        if wbucket is not None:
            week_bucket = wbucket
            week_high = high[index] if math.isnan(week_high) else max(week_high, high[index])
            week_low = low[index] if math.isnan(week_low) else min(week_low, low[index])

        upper.update_lifecycle(bar_index=index, high=high[index], low=low[index], close=close[index],
                               current_atr=current_atr, direction=1)
        lower.update_lifecycle(bar_index=index, high=high[index], low=low[index], close=close[index],
                               current_atr=current_atr, direction=-1)
        upper.expire(index)
        lower.expire(index)

        if trace_every > 0 and index % trace_every == 0:
            bias_i, regime_i = _context_at(index, fast_ema, slow_ema, adx_values, atr_average, current_atr)
            up_magnets, up_pull = _rank_side(upper, index, close[index], current_atr, bias_i, 1,
                                             regime_i, timeframe_seconds, params)
            dn_magnets, dn_pull = _rank_side(lower, index, close[index], current_atr, bias_i, -1,
                                             regime_i, timeframe_seconds, params)
            total_i = up_pull + dn_pull
            trace.append(
                GravitySnapshot(
                    bar_index=index, close=close[index], atr=current_atr, trend_bias=bias_i,
                    regime=regime_i,
                    net_gravity=((up_pull - dn_pull) / total_i * 100.0) if total_i > 0 else 0.0,
                    bullish_pressure=(up_pull / total_i * 100.0) if total_i > 0 else 50.0,
                    upper=up_magnets, lower=dn_magnets,
                    rejected_up=[pool.centroid for pool in upper.pools
                                 if pool.state == REJECTED and pool.resolution_bar == index],
                    rejected_down=[pool.centroid for pool in lower.pools
                                   if pool.state == REJECTED and pool.resolution_bar == index],
                    accepted_up=[pool.centroid for pool in upper.pools
                                 if pool.state == ACCEPTED and pool.resolution_bar == index],
                    accepted_down=[pool.centroid for pool in lower.pools
                                   if pool.state == ACCEPTED and pool.resolution_bar == index],
                )
            )

    last = size - 1
    final_atr = atr_values[last]
    if final_atr is None or final_atr <= 0:
        return MarketGravityResult(status="insufficient_data", bars=size,
                                   notes=notes + ["ATR unavailable on the last bar"])

    trend_bias, regime = _context_at(last, fast_ema, slow_ema, adx_values, atr_average, final_atr)
    upper_magnets, upper_pull = _rank_side(upper, last, close[last], final_atr, trend_bias, 1,
                                           regime, timeframe_seconds, params)
    lower_magnets, lower_pull = _rank_side(lower, last, close[last], final_atr, trend_bias, -1,
                                           regime, timeframe_seconds, params)
    total_pull = upper_pull + lower_pull
    bullish = (upper_pull / total_pull * 100.0) if total_pull > 0 else 50.0
    net = ((upper_pull - lower_pull) / total_pull * 100.0) if total_pull > 0 else 0.0

    return MarketGravityResult(
        status="ok", notes=notes, bars=size, close=close[last], atr=final_atr,
        timeframe_seconds=timeframe_seconds, trend_bias=trend_bias, regime=regime,
        upper=upper_magnets, lower=lower_magnets, upper_pull=upper_pull, lower_pull=lower_pull,
        bullish_pressure=bullish, bearish_pressure=100.0 - bullish, net_gravity=net,
        pools_upper=len(upper.pools), pools_lower=len(lower.pools), trace=trace,
    )


def forecast_first_passage(
    snapshot: GravitySnapshot,
    *,
    alpha: float,
    beta: float,
    gamma: float,
    vol_scale: float = 1.0,
    horizon_bars: int = 20,
    simulations: int = 2000,
    seed: int = 1234,
) -> dict[str, float]:
    """Drift-diffusion Monte Carlo driven by net gravity and trend bias.

    ``alpha`` (gravity pull per bar, in ATR units), ``beta`` (trend drift per bar) and
    ``gamma`` (diffusion scale on ATR) MUST come from :func:`calibrate_diffusion`.
    The reference document's 0.12 / 0.08 / 0.65 were never fitted to any data; feeding
    unfitted constants in here would print confident-looking probabilities that carry
    no information.

    First passage is measured against the nearer ZONE EDGE of each primary magnet,
    because that is what a sweep actually consumes.
    """
    import random

    start, atr = snapshot.close, snapshot.atr
    if atr <= 0 or simulations <= 0 or horizon_bars <= 0:
        return {"usable": 0.0}
    rng = random.Random(seed)
    gaussian = rng.gauss
    drift = (alpha * (snapshot.net_gravity / 100.0) + beta * snapshot.trend_bias) * atr
    sigma = gamma * vol_scale * atr

    upper_target = snapshot.upper[0].zone_low if snapshot.upper else None
    lower_target = snapshot.lower[0].zone_high if snapshot.lower else None

    u_first = l_first = both = 0
    ends: list[float] = []
    for _ in range(simulations):
        price = start
        hit_u = hit_l = 0
        for _bar in range(horizon_bars):
            price += drift + sigma * gaussian(0.0, 1.0)
            if not hit_u and upper_target is not None and price >= upper_target:
                hit_u = 1
            if not hit_l and lower_target is not None and price <= lower_target:
                hit_l = 1
            if hit_u and hit_l:
                break
        ends.append(price)
        if hit_u and hit_l:
            both += 1
        elif hit_u:
            u_first += 1
        elif hit_l:
            l_first += 1
    ends.sort()
    high_water = simulations - u_first - l_first - both
    return {
        "usable": 1.0,
        "median_end": ends[len(ends) // 2],
        "expected_move_pct": (ends[len(ends) // 2] / start - 1.0) * 100.0 if start else 0.0,
        "p68_low": ends[int(len(ends) * 0.16)],
        "p68_high": ends[int(len(ends) * 0.84)],
        "p95_low": ends[int(len(ends) * 0.025)],
        "p95_high": ends[min(len(ends) - 1, int(len(ends) * 0.975))],
        "prob_upper_magnet_first": u_first / simulations * 100.0,
        "prob_lower_magnet_first": l_first / simulations * 100.0,
        "prob_ambiguous": both / simulations * 100.0,
        "prob_no_magnet": max(0, high_water) / simulations * 100.0,
    }


def calibrate_diffusion(samples: Sequence[tuple[float, float, float, float]]) -> dict[str, float]:
    """Least-squares fit of a realised 1-bar price change on gravity and trend bias.

    Each sample is ``(net_gravity, trend_bias, atr, forward_change)``. Returns alpha,
    beta, gamma (residual sigma in ATR units) and ``r2`` - the last reported on purpose,
    so a weak regressor cannot be mistaken for a validated forecasting edge.
    """
    count = len(samples)
    if count < 30:
        return {"alpha": 0.0, "beta": 0.0, "gamma": 0.0, "r2": 0.0, "n": count}
    matrix = [(gravity / 100.0, trend) for gravity, trend, _atr, _y in samples]
    target = [y / atr if atr > 0 else 0.0 for _g, _t, atr, y in samples]
    sx1 = sum(row[0] for row in matrix)
    sx2 = sum(row[1] for row in matrix)
    sxx11 = sum(row[0] * row[0] for row in matrix)
    sxx12 = sum(row[0] * row[1] for row in matrix)
    sxx22 = sum(row[1] * row[1] for row in matrix)
    sy = sum(target)
    sxy1 = sum(row[0] * y for row, y in zip(matrix, target))
    sxy2 = sum(row[1] * y for row, y in zip(matrix, target))
    rows = [
        [float(count), sx1, sx2, sy],
        [sx1, sxx11, sxx12, sxy1],
        [sx2, sxx12, sxx22, sxy2],
    ]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda r: abs(rows[r][column]))
        rows[column], rows[pivot] = rows[pivot], rows[column]
        base = rows[column][column]
        if abs(base) < 1e-12:
            return {"alpha": 0.0, "beta": 0.0, "gamma": 0.0, "r2": 0.0, "n": count}
        rows[column] = [value / base for value in rows[column]]
        for other in range(3):
            if other == column:
                continue
            factor = rows[other][column]
            rows[other] = [a - factor * b for a, b in zip(rows[other], rows[column])]
    intercept, alpha, beta = (row[3] for row in rows)
    mean_y = sy / count
    residual = sum((y - (intercept + alpha * row[0] + beta * row[1])) ** 2 for row, y in zip(matrix, target))
    total = sum((y - mean_y) ** 2 for y in target)
    return {
        "alpha": alpha,
        "beta": beta,
        "gamma": math.sqrt(residual / max(1, count - 3)),
        "intercept": intercept,
        "r2": 0.0 if total <= 0 else 1.0 - residual / total,
        "n": count,
    }










