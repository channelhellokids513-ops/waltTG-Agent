# Market Gravity — Port Specification (Pine v6 → Python)

Source: "Market Gravity" TradingView Pine Script v6, read from `MG.txt` (4630 lines).
This file is the authoritative transcription of every constant and formula used by
`market_gravity.py`. Nothing here is written from memory: each item carries its
source line number. Status: Fase 0 (in progress); §3 lists what is still UNREAD.

## 1. Constants (verified from source)

### Liquidity Detection (L27–L84)
| Name | Default | Line | Meaning |
|---|---|---|---|
| `pivotLeft` | 5 | L28 | bars left of extreme to confirm a swing pivot |
| `pivotRight` | 5 | L36 | bars right required before pivot is confirmed |
| `clusterATR` | 0.15 | L44 | structures within this ATR distance merge into one pool |
| `sweepBufferATR` | 0.05 | L53 | price must exceed level by this ATR amount to count as swept |
| `maxAgeBars` | 1500 | L62 | older pools are dropped |
| `maxStoredLevels` | 40 | L70 | pool storage cap |
| `maxTargetDistanceATR` | 12.0 | L78 | target must sit within this ATR distance |

### Liquidity Sources (L87–L171)
| Name | Default | Line |
|---|---|---|
| `useSwingLiquidity` | true | L88 |
| `useEqualHighLow` | true | L95 |
| `useMajorSwings` | true | L102 |
| `usePreviousDay` | true | L109 |
| `usePreviousWeek` | true | L116 |
| `useRepeatedRejections` | true | L123 |
| `equalToleranceATR` | 0.15 | L130 |
| `majorSwingATR` | 2.50 | L138 |
| `rejectionMinSeparationBars` | 8 | L146 |
| `rejectionWickRatio` | 0.30 | L154 |
| `recentPivotMemory` | 30 | L164 |

Note L113: Previous Day is used "when the chart timeframe is daily or lower";
L120: Previous Week "when the chart timeframe is weekly or lower".

### Pool Engine (L173–L207)
| Name | Default | Line | Note |
|---|---|---|---|
| `independentTouchMinSeparationBars` | 8 | L174 | tooltip: structural confirmations may join a pool *without* inflating touch count |
| `clusterZonePaddingATR` | 0.02 | L182 | padding outside member min/max prices |
| `isolatedZoneHalfATR` | 0.05 | L191 | half-width of a one-member pool |
| `maxZoneWidthATR` | 0.50 | L200 | safety cap on zone width |

### Gravity Model (L209–L286)
| Name | Default | Line |
|---|---|---|
| `atrLength` | 14 | L210 |
| `fastEMALength` | 21 | L217 |
| `slowEMALength` | 55 | L224 |
| `volumeLength` | 50 | L231 |
| `approachingATR` | 0.75 | L238 |
| `accessibilityKATR` | 2.0 | L251 |
| `swingFreshnessHalfLifeBars` | 150 | L260 |
| `clusterFreshnessHalfLifeBars` | 250 | L267 |
| `previousDayFreshnessHalfLifeBars` | 300 | L274 |
| `previousWeekFreshnessHalfLifeBars` | 500 | L281 |

Rank weights are hardcoded (L246–L249): primary 1.00, secondary 0.65, tertiary 0.40.

### Lifecycle (L288–L324)
| Name | Default | Line | Note |
|---|---|---|---|
| `activeLiquidityStrengthThreshold` | 40.0 | L289 | below this a pool stays `Forming` and is **ineligible for target ranking** |
| `rejectionWindowBars` | 2 | L299 | after a confirmed sweep, a close back on the original side of the full zone within N bars ⇒ `Rejected` |
| `acceptanceCloseCount` | 2 | L308 | consecutive confirmed closes beyond the outer zone boundary ⇒ `Accepted` |
| `lifecycleEventDisplayBars` | 3 | L317 | bars a resolved pool is held before transitioning to `Retired` |

Display/alert inputs (L326–L454) are **out of scope** — the Python port has no
rendering layer, so `showCurves`, `projectionBars`, `displayLevels`,
`lineHistoryBars`, `showGlow`, `showTable`, `showLifecycleEvents`,
`labelCollisionMode`, `labelCollisionThresholdATR`, `rankMarkerSpacingBars`,
`lifecycleLabelGapBars`, `tablePositionInput`, `primaryGravityAlertLevel` and
`gravityAlertLevel` are deliberately not ported.

## 2. Formulas (transcribed verbatim)

### Helpers (L630–L650)
`clamp01(x)` ⇒ [0,1]; `clampSigned(x)` ⇒ [−1,1].

### Trend bias (L2702–L2716)
```
trendDistance = atr > 0 ? (fastEMA - slowEMA) / (atr * 2.0) : 0.0
trendBias     = clampSigned(trendDistance)
```

### Regime (L2718–L2743) — comment L2718: "not display-only", it is a modifier
```
[plusDI, minusDI, adx] = ta.dmi(14, 14)
atrAverage             = ta.sma(atr, 50)
regime = "Trend Expansion" if adx >= 25 and atr >  atrAverage * 1.05
         "Trend"            if adx >= 25
         "Compression"      if atr  <  atrAverage * 0.85
         "Range"            otherwise
```

### Liquidity strength components (L1130–L1246)
```
densityScore(n members)  = 0.0                                    if n <= 0
                           = clamp01(0.20 + 0.80*(1 - e^(-0.70*max(n-1,0))))
touchQuality(t touches)  = 0.0                                    if t <= 0
                           = clamp01(0.20 + 0.80*(1 - e^(-0.80*max(t-1,0))))
timeframeImportance      = 1.00 if sources contain Previous Week High/Low
                           0.70 elif Previous Day High/Low
                           0.35 otherwise
swingProminenceScore     = clamp01(maxSwingProminence) if swingEvidenceCount > 0 else 0.0
formationVolumeScore     = clamp01(volumeEvidenceSum / volumeEvidenceCount) if count > 0 else 0.0

liquidityStrength = 100 * (0.30*density + 0.25*swingProminence
                           + 0.20*touchQuality + 0.15*timeframeImportance
                           + 0.10*formationVolume)
```
Design note (L560): structural evidence is "deliberately independent of current
price so Liquidity Strength remains stable unless pool structure changes".

### Freshness (L1331–L1396)
```
age = max(bar_index - activeBar, 0)
baseHalfLife = 500 prev-week | 300 prev-day | 250 cluster | 150 swing  (L1331-L1357)
strengthNormalized         = clamp01(liquidityStrength / 100)
strengthHalfLifeMultiplier = 0.75 + 0.75 * strengthNormalized          # 0.75x .. 1.50x
effectiveHalfLife          = max(1.0, baseHalfLife * strengthHalfLifeMultiplier)
freshness                  = exp(-0.6931471805599453 * age / effectiveHalfLife)
```
Selection order in `getBaseFreshnessHalfLife` (L1352–L1357) is prev-week, prev-day,
cluster, swing; the exact predicate for "cluster" vs "swing" is in the unread region
(§3) and must be transcribed, not inferred.

### Accessibility, modifiers, gravity (L1406–L1567)
```
normalizedDistance  = accessibilityKATR > 0 ? distanceATR / accessibilityKATR : distanceATR
accessibility       = 1 / (1 + normalizedDistance^2)          # 0.50 at exactly K ATR
distanceMultiplier  = 0.55 + 0.45 * accessibility             # [0.55, 1.00]
freshnessMultiplier = 0.70 + 0.30 * freshness                 # [0.70, 1.00]

adaptiveTrendInfluenceScale(tfSeconds):                        # L1406-L1428
    <= 300s  -> 2.00
    <  15m   -> 2.00 + 0.50 * log(tf/5m)  / log(15m/5m)
    <= 4h    -> 2.50
    <  1d    -> 2.50 - 1.50 * log(tf/4h)  / log(1d/4h)
    else     -> 1.00,  clamped to [1.00, 2.50]

trendModifier = clamp(0.75, 1.25, 1 + 0.10 * adaptiveScale * clampSigned(direction * trendBias))

regimeModifier (L1450-L1494):
    isRangeStructure = sources contain "Equal High" | "Equal Low" | "Repeated Rejection"
    raw = 1 + 0.10*dirTrend                       if "Trend Expansion"
          1 + 0.05*dirTrend                       if "Trend"
          1.05 if isRangeStructure else 1.00      if "Range"
          0.92                                    if "Compression"
          1.00                                    otherwise
    bounded = clamp(0.90, 1.10, raw)
    scaled  = 1 + (bounded - 1) * 2.00
    regimeModifier = clamp(0.80, 1.20, scaled)

gravity = clamp(0.0, 100.0, liquidityStrength * distanceMultiplier
                * freshnessMultiplier * trendModifier * regimeModifier)
```
`direction` is +1/-1 for upper/lower structures; `dirTrend = clampSigned(direction * trendBias)`.
`distanceATR` comes from `getDistanceToZoneATR(zoneLow, zoneHigh, currentATR)` — body
still unread (§3): whether it measures to the nearer zone edge is an open question.

### Numerical anchors for parity tests (derived from the formulas above)
- `densityScore(n) = clamp01(0.20 + 0.80*(1-e^(-0.70*(n-1))))`:
  n=1 -> 0.200, n=2 -> 0.603, n=3 -> 0.803, n=5 -> 0.951 (asymptotic to 1.0, never equal)
- `touchQuality(t) = clamp01(0.20 + 0.80*(1-e^(-0.80*(t-1))))`:
  t=1 -> 0.200, t=2 -> 0.641, t=3 -> 0.839
- `accessibility` at distance 2.0 ATR with K=2.0 -> 0.500 exactly
- `freshness` at age == effectiveHalfLife -> 0.500 exactly
- a fully-saturated pool reaches strength ~99.76, not 100 (the components are
  asymptotic); gravity still clamps at 100 because trend x regime can reach 1.50

Correction note: an earlier draft of this file listed density(2)=0.497 / (3)=0.669 /
(5)=0.821, which mistook `e^(-0.70*(n-1))` for the score itself. `tests/test_market_gravity.py`
caught it. The values above are the ones the tests assert.

## 2b. Indicators & detection inputs (transcribed)
```
atr            = ta.atr(atrLength)                 # L482, atrLength=14
volumeAverage  = ta.sma(volume, volumeLength)      # L487, volumeLength=50
pivotWindow    = pivotLeft + pivotRight + 1        # L501 -> 11 at defaults
pivotHigh      = ta.pivothigh(high, pivotLeft, pivotRight)  # non-na pivotRight bars later
pivotBar       = bar_index - pivotRight            # L2748 the pivot itself
activeBar      = bar_index                         # L2752 the confirming bar
```
Confirmation lag is exactly `pivotRight` bars: a pivot at bar *p* first exists on bar
*p + pivotRight*. The port must reproduce this and never let a pivot affect a score
dated earlier than its confirmation.

## 2c. Upper-side structure detection (L2744-L2933)
```
lowestAroundPivot    = ta.lowest(low, pivotWindow)               # at the pivot bar
rawSwingStrength     = (pivotHigh - lowestAroundPivot) / atr
swingStrength        = clamp01(rawSwingStrength / 4.0)           # prominence: 4 ATR -> 1.0
structuralVolumeStrength = clamp01((volume[pivotRight] / volumeAverage[pivotRight]) / 2.0)  # else na
volumeStrength       = na(structuralVolumeStrength) ? 0.50 : structuralVolumeStrength  # legacy only
pivotRange     = high[pivotRight] - low[pivotRight]
upperWick      = high[pivotRight] - max(open[pivotRight], close[pivotRight])
upperWickRatio = pivotRange > 0 ? upperWick / pivotRange : 0.0

equalMatches     = getRecentMatchCount(recentP, recentB, pivotHigh, pivotBar,
                                       atr * equalToleranceATR, max(1, pivotRight))
rejectionMatches = getRecentMatchCount(... same tolerance ..., rejectionMinSeparationBars)

isEqualHigh              = equalMatches > 0
isMajorHigh              = rawSwingStrength >= majorSwingATR        # 2.50
isRepeatedUpperRejection = rejectionMatches > 0 and upperWickRatio >= rejectionWickRatio  # 0.30
sources       = "Swing High" [+ "Equal High"] [+ "Major Swing High"] [+ "Repeated Rejection"]
memberWeight  = 1.25 if (useMajorSwings and isMajorHigh) else 1.00
addPoolMember(newPrice=pivotHigh, newBar=pivotBar, newActiveBar=activeBar,
              newStrength=swingStrength, newVolume=volumeStrength,
              newStructuralSwingProminence=swingStrength,
              newStructuralVolumeScore=structuralVolumeStrength, newSource=sources,
              newWeight=memberWeight, newATR=atr, countsAsTouch=true, touchBar=pivotBar,
              tolerance=atr * clusterATR, maximumLevels=maxStoredLevels)
storeRecentPivot(..., recentPivotMemory)   # 30 confirmed pivots, classification only
```
L2789-L2790: the pool `strengths` array holds the LEGACY 0..1 swing strength "for
continuity"; Gravity does **not** use it. Gravity uses `getPoolLiquidityStrengthAtIndex`
(L1248), which recomputes the 0..100 structural strength from member count, touches,
prominence, sources and volume evidence. Had this been guessed, every score would have
been scaled 100x wrong.

## 2d. Lifecycle FSM (updateLifecycle, L1927-L2075) - transcribed in full
Direction: `+1` for upper pools, `-1` for lower pools. Runs every bar but only commits
when the candle is confirmed (L3309-L3310). For each pool:
```
zoneLow, zoneHigh       = pool zone
distanceATR             = getDistanceToZoneATR(zoneLow, zoneHigh, currentATR)
[... liquidityStrength] = getLiquidityStrengthComponents(memberCount, independentTouches,
                          maxSwingProminences, swingEvidenceCounts, volumeEvidenceSums,
                          volumeEvidenceCounts, poolSources)

zoneTouched  = (dir == 1) ? high >= zoneLow        : low <= zoneHigh
sweepBoundary= (dir == 1) ? zoneHigh + ATR*sweepBufferATR : zoneLow - ATR*sweepBufferATR
swept        = (dir == 1) ? high >= sweepBoundary  : low  <= sweepBoundary
closeBeyond  = (dir == 1) ? close >  zoneHigh      : close <  zoneLow
closeOriginalSide = (dir == 1) ? close < zoneLow   : close > zoneHigh

if state in {FORMING, ACTIVE, APPROACHING}:
    if swept and bar_index >= activeBar:
        sweepBar = bar_index
        beyondCount = 1 if closeBeyond else 0
        if beyondCount >= acceptanceCloseCount:  state = ACCEPTED; resolutionBar = bar   # acceptedEvent
        else:                                    state = SWEEPING
    else:
        state = FORMING     if liquidityStrength < activeLiquidityStrengthThreshold   # 40.0
              APPROACHING   elif distanceATR <= approachingATR                        # 0.75
              ACTIVE        otherwise
        touchEvent if zoneTouched and not swept and bar_index > activeBar
                   and (lifecycleTouchBar < 0 or bar_index - lifecycleTouchBar >= 8)   # independentTouchMinSeparationBars

elif state == SWEEPING:
    beyondCount = (closeBeyond ? previous + 1 : 0)                # consecutive, resets on any close inside
    if beyondCount >= acceptanceCloseCount:      state = ACCEPTED; resolutionBar = bar  # acceptedEvent
    elif barsSinceSweep <= rejectionWindowBars and closeOriginalSide:
                                                 state = REJECTED; resolutionBar = bar  # rejectedEvent
    elif barsSinceSweep > rejectionWindowBars:   state = RETIRED;  resolutionBar = bar

elif state in {REJECTED, ACCEPTED}:
    if resolutionBar >= 0 and bar_index - resolutionBar >= lifecycleEventDisplayBars:   # 3
        state = RETIRED; resolutionBar = bar          # RETIRED re-stamps, so removal is next bar
```
Removal (L3369-L3449): `bar_index - activeBar > maxAgeBars` (1500) OR
(`state == RETIRED and resolutionBar >= 0 and bar_index > resolutionBar`) — i.e. a
retired pool survives exactly one more bar. Comment L3369: hard expiry is "a resource
bound, not the primary freshness model".

Ranking (L3451-L3515): `getBestIndex(..., exclude1, exclude2, atr, trendBias, dir, regime)`
three times per side, excluding earlier picks -> rank weights 1.00 / 0.65 / 0.40.
Only FORMING/ACTIVE/APPROACHING pools may merge; ranking eligibility is in
`getBestIndex` (transcribed in §2f).

## 2e. Pool engine tail (L1049-L1128)
```
on merge: sources = appendSource(oldSources, newSource)   # " + " delimited
          zoneLows/Highs = recomputed bounds; memberMins/Maxs/Counts set
          totalWeights = combinedWeight; lastTouchBars, poolATRs,
          maxSwingProminences, swingEvidenceCounts, volumeEvidenceSums/Counts updated
after push/merge (L1068-L1106):
    while array.size(prices) > maximumLevels:            # maxStoredLevels = 40
        remove the pool with the SMALLEST activeBar (oldest), repeatedly
containsSource(sources, name) = (" + " + sources + " + ").contains(" + " + name + " + ")
```

## 2f. Distance & ranking eligibility
```
getDistanceToZoneATR(zoneLow, zoneHigh, currentATR):                      # L1314-L1329
    if currentATR <= 0: 0.0
    close <  zoneLow  -> (zoneLow  - close) / currentATR
    close >  zoneHigh -> (close - zoneHigh) / currentATR
    otherwise (inside the zone) -> 0.0
    # measured from the CURRENT CLOSE to the nearer zone edge

getBestIndex(...):                                                        # L1598-L1676
    rankEligible = state in {ACTIVE, APPROACHING}      # FORMING and any resolved state are excluded
    correctSide  = (dir == 1) ? zoneHigh >= close : zoneLow <= close
    within reach = distanceATR <= maxTargetDistanceATR # 12.0
    -> argmax of getGravityScore(...) among eligible, skipping exclude1/exclude2
```

## 2g. Non-swing structure sources (L3124-L3307)
```
previousDayHigh/Low : only when usePreviousDay and canUsePreviousDay and newDay and not na(atr)
    addPoolMember(price=previousDayHigh, newBar=bar_index, newActiveBar=bar_index,
                  newStrength=0.50, newVolume=0.50, prominence=na, volumeScore=na,
                  source="Previous Day High", memberWeight=1.15, atr,
                  countsAsTouch=false, touchBar=bar_index, tolerance=atr*clusterATR)
previousWeekHigh/Low: identical but memberWeight=1.30 and source="Previous Week High/Low"
```
So HTF structures enter with neutral legacy strength/volume (0.50), NO swing prominence
and NO volume evidence (`na` ⇒ swingEvidenceCount 0, volumeEvidenceCount 0 ⇒ those two
components score 0.0), but they carry the highest timeframeImportance (0.70 / 1.00) and
the longest half-lives (300 / 500 bars), and they do **not** add a touch when merging.

## 2h. Directional pull (L3752-L3810)
```
rankContribution(score, w) = 0.0 if na(score) else score * w
upperPull = u1*1.00 + u2*0.65 + u3*0.40      lowerPull = l1*1.00 + l2*0.65 + l3*0.40
totalPull = upperPull + lowerPull
bullishPressure = totalPull > 0 ? upperPull / totalPull * 100.0 : 50.0
bearishPressure = 100.0 - bullishPressure
```
Design note (L3753): "Gravity already contains accessibility/distance. Applying another
distance divisor here would double-penalize remote targets, so only rank weights are
applied."

## 3. Transcription coverage
Transcribed and ported (implemented in `market_gravity.py`, asserted by
`tests/test_market_gravity.py`): section 1 constants; section 2 helpers / trend /
regime / strength / freshness / accessibility / modifiers / gravity; 2b detection
inputs; 2c structure detection on both sides (verified as a true mirror, including the
shared `"Repeated Rejection"` label); 2d the lifecycle FSM; 2e pool merge, pruning and
`containsSource`; 2f zone distance and ranking eligibility; 2g previous day/week
structures; 2h rank-weighted pull and `netGravity`.

Deliberately NOT ported (rendering, not scoring): L326-L454 display/alert inputs,
L455-L557 colors, and every `plot`/`line`/`label`/`table`/`polyline` routine (gravity
projection curves, collision lanes, `drawLevel`, `getLatestLifecycleEventIndex`,
`showLifecycleEvents`). The port exposes the same facts as data instead of pixels.

`request.security(ticker, "1D", [high[1], low[1]], lookahead_on)` (L505-L518) is
replaced by aggregating COMPLETED 1D/1W buckets from the fetched window, preserving the
"previous completed bar, never the developing one" property.

Two behavioural caveats carried into the port, both tested:
1. Warm-up. Pine emits `na` for ATR/EMA/ADX until enough bars exist; the port skips
   detection on those bars the same way. A window shorter than
   `max(slowEMA, volumeSMA, ATR) + pivotRight + 1` returns `status="insufficient_data"`
   with blank diagnostic fields - never a plausible-looking zero.
2. Plateau pivots. Pine's exact tie-breaking inside `ta.pivothigh` is not specified in
   the source; the port requires the centre bar to be strictly beyond its right
   neighbours, and flat tops/bottoms are handled by the dedicated `Equal High` /
   `Equal Low` classifier, which is the model's own mechanism for plateaus.

## 4. Provenance & license
Source file header, verbatim (L1–L2):

```
// This Pine Script(TM) code is subject to the terms of the Mozilla Public License 2.0 at https://mozilla.org/MPL/2.0/
// Join our channel for more free tools: https://t.me/simpleforextools
```

`market_gravity.py` must retain this notice verbatim in its module docstring (MPL-2.0
requires the notice to stay in the Original Code / derivatives of it). Indicator name
"Market Gravity", Pine `//@version=6` (L5). No warranty is granted by the source;
Gravity output is documented by the author as "relative attraction metrics, not
probabilities" (L23) — the port keeps that wording wherever the score is surfaced.

## 5. Walt integration decisions (owner-approved)
- Placement **(a)**: deterministic **parallel structural score**, NOT a swarm voter.
  Rationale: `PARAM_WEIGHTS` P1..P7 sums to 1.00 and `overall_consensus_level`
  (UNANIMOUS/STRONG/MIXED/SPLIT) is derived from variance between *independent LLM
  opinions*. A deterministic formula has zero variance and always agrees with
  itself, so admitting it as "voter P8" would inflate UNANIMOUS structurally.
- First landing = **diagnostic only**: new columns in `Daily_Scans`; no entry, no E5,
  no TP/SL, no sizing, no consensus effect. Guarded by a test.
- `contract_economics` measurement runs **after** this port (owner's ordering).
- Bar window prerequisite: model assumes up to `maxAgeBars=1500` and half-lives to
  500 bars; `market_chart.fetch_candles` default is 48 bars, so a large-window fetch
  plus ATR(14), DMI(14,14) and SMA(ATR,50) must be added first.
