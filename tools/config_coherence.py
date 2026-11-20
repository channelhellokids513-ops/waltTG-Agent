"""Pemeriksa KOHERENSI konfigurasi strategi: apakah angka-angka yang dipasang saling mungkin.

Bedanya dengan validasi rentang (`strategy_config.STRATEGY_PARAM_SPECS`): rentang memeriksa satu
angka, sedangkan kegagalan nyata di sini adalah PASANGAN angka yang masing-masing sah tapi juntos
mustahil. Contoh yang benar-benar terjadi: jendela riset 6 jam dengan kadens scan 12 jam membuat
reuse riset struktural mati (dibuktikan CI #44778524915 16:57Z: `segar=0, basi=39`), tanpa satu pun
nilai itu di luar rentangnya.

Dipakai sebagai test (default kode) dan sebagai langkah CI tanpa-OAuth di diagnostics.yml
(membaca variable produksi `STRATEGY_CONFIG_JSON`, yang MENIMPA default kode).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_config import DAILY_SCAN_CADENCE_HOURS  # noqa: E402

TAKER_FEE_PER_SIDE = 0.0009   # disalin dari walt_bot.TAKER_FEE_PER_SIDE dan dikunci oleh test:
                              # mengimpor walt_bot di sini berat (menyentuh jalur bursa saat import)
SPREAD_SLIPPAGE = 0.0003      # estimasi slippage round-trip
MIN_EDGE_MULTIPLE = 1.5       # funding minimum harus menutup biaya dengan margin >= 1.5x


def check(cfg: dict[str, Any]) -> list[str]:
    """Kembalikan daftar kalimat pelanggaran (kosong = koheren)."""
    galat: list[str] = []
    get = lambda k, d=None: cfg.get(k, d)

    jendela = float(get("max_research_age_hours") or 0)
    if jendela < DAILY_SCAN_CADENCE_HOURS:
        galat.append(
            f"max_research_age_hours={jendela:.1f}h < kadens daily-scan={DAILY_SCAN_CADENCE_HOURS:.0f}h: "
            f"riset hasil scan dianggap basi sebelum scan berikutnya -> entry SKIP 'riset basi' "
            f"selama {DAILY_SCAN_CADENCE_HOURS - jendela:.1f}h tiap siklus. Naikkan jendela atau percepat scan."
        )

    hold = float(get("expected_hold_hours") or 0)
    min_funding = float(get("min_funding_daily") or 0)
    biaya = 2.0 * TAKER_FEE_PER_SIDE + SPREAD_SLIPPAGE
    if hold <= 0 or min_funding <= 0:
        galat.append(f"expected_hold_hours={hold} / min_funding_daily={min_funding} wajib > 0")
    else:
        funding_tertutup = min_funding * (hold / 24.0)
        if funding_tertutup < biaya:
            galat.append(
                f"Gerbang funding meloloskan ekspektasi NEGATIF: funding terkecil yang diterima "
                f"({min_funding:.4f}/hari x {hold:.0f}h = {funding_tertutup:.4f}) < biaya round-trip "
                f"({biaya:.4f}). Naikkan min_funding_daily >= {biaya / (hold / 24.0):.4f} atau hold."
            )
        elif funding_tertutup < biaya * MIN_EDGE_MULTIPLE:
            galat.append(
                f"Ambang funding terlalu tipis: margin {funding_tertutup / biaya:.2f}x biaya "
                f"(disarankan >= {MIN_EDGE_MULTIPLE}x) -> hasil bersih didominasi asumsi slippage."
            )

    sl = float(get("default_sl_pct") or 0)
    tp_min = float(get("tp_calm_pct") or 0)
    if tp_min and tp_min <= biaya:
        galat.append(
            f"tp_calm_pct={tp_min:.4f} tidak menutup biaya round-trip {biaya:.4f} -> TP bersih negatif."
        )
    if sl and float(get("daily_loss_limit_pct") or 1) < sl:
        galat.append(
            f"daily_loss_limit_pct={get('daily_loss_limit_pct')} < default_sl_pct={sl}: satu stop-loss "
            f"biasa langsung memicu circuit breaker harian."
        )
    return galat


def main(argv: Optional[list[str]] = None) -> int:
    mentah = os.environ.get("STRATEGY_CONFIG_JSON", "").strip()
    sumber = "STRATEGY_CONFIG_JSON (variable produksi — menimpa default kode)" if mentah else \
             "DEFAULT_STRATEGY_CONFIG (kode)"
    if mentah:
        try:
            cfg = json.loads(mentah)
        except json.JSONDecodeError as e:
            print(f"sumber             = {sumber}")
            print(f"verdict            = SAKIT: STRATEGY_CONFIG_JSON tidak bisa diurai ({e})")
            return 1
    else:
        from strategy_config import DEFAULT_STRATEGY_CONFIG
        cfg = dict(DEFAULT_STRATEGY_CONFIG)

    galat = check(cfg)
    print(f"sumber             = {sumber}")
    print(
        "angka_dipakai      = "
        + ", ".join(f"{k}={cfg.get(k)}" for k in (
            "max_research_age_hours", "min_funding_daily", "expected_hold_hours",
            "default_sl_pct", "tp_calm_pct", "daily_loss_limit_pct", "max_entries_per_day",
        ))
    )
    print(f"biaya_round_trip   = {2.0 * TAKER_FEE_PER_SIDE + SPREAD_SLIPPAGE:.4f} "
          f"(2 x taker {TAKER_FEE_PER_SIDE:.4f} + slippage {SPREAD_SLIPPAGE:.4f})")
    for g in galat:
        print(f"PELANGGARAN        = {g}")
    print("verdict            = " + ("KOHEREN" if not galat else f"TIDAK KOHEREN ({len(galat)})"))
    return 1 if galat else 0


if __name__ == "__main__":
    raise SystemExit(main())
