"""Pemeriksa KOHERENSI konfigurasi strategi: apakah angka-angka yang dipasang saling mungkin.

Bedanya dengan validasi rentang (`strategy_config.STRATEGY_PARAM_SPECS`): rentang memeriksa satu
angka, sedangkan kegagalan nyata di sini adalah PASANGAN angka yang masing-masing sah tapi juntos
mustahil. Dua contoh nyata dari ronde ini:
  - jendela riset 6 jam vs kadens scan 12 jam (CI #44778524915: `segar=0, basi=39`);
  - `min_funding_daily` 0.50%/hari vs ambang gerbang E5 ~1.77%/hari (CI #44780797377 17:12Z:
    SEMUA kandidat SKIP) -> floor funding adalah knob mati.

Matematika biaya TIDAK dimodelkan ulang di sini: fungsi gerbang `walt_bot` yang dipanggil, supaya
alat ini tidak bisa hijau sambil salah.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_config import DAILY_SCAN_CADENCE_HOURS  # noqa: E402

# Slippage REFERENSI dipakai dari yang TERAMATI, bukan dikira. Log produksi crypto-execute
# 17:12Z: ETH/BNB/AAVE semuanya `cost=1.180%` -> slippage aset-aset itu ~1.00% (0.18% taker
# round-trip + 1.00%). Angka 0.03% yang dulu dipakai alat ini menghasilkan "KOHEREN" palsu.
# Catatan Ronde 8: 1,00% itu ternyata KONSTANTA DEFAULT bursa (p10==p90 di 139 kontrak), dan
# engine sekarang memakai dasar TERUKUR (2 x p90 sisi masuk, floor 0,10%). Karena itu angka
# di bawah hanya boleh dipakai sebagai pembanding "dasar venue"; laporan utama alat ini WAJIB
# memakai basis yang sama dengan engine, atau ia akan berbohong seperti dulu.
SLIPPAGE_TERAMATI = 0.0100
SLIPPAGE_OPTIMISTIS = 0.0005


def active_slippage_basis() -> float:
    """Dasar slippage yang benar-benar dipakai gerbang E5 engine hari ini."""
    from walt_bot import effective_price_slippage
    return effective_price_slippage(SLIPPAGE_TERAMATI)


def _gate():
    from walt_bot import check_funding_vs_fee_gate
    return check_funding_vs_fee_gate


def round_trip_cost(slippage: float = SLIPPAGE_TERAMATI, hold: float = 24.0) -> float:
    """Biaya round-trip menurut gerbang engine itu sendiri (2 x taker + slippage)."""
    _ok, _exp, cost, _note = _gate()(0.0, slippage, hold)
    return float(cost)


def e5_ambang(slippage: float = SLIPPAGE_TERAMATI, hold: float = 24.0) -> float:
    """Funding/hari terkecil yang diluluskan gerbang asli — dicari dengan probe, bukan rumus salinan.

    Kalau buffer E5 di engine diubah, angka ini ikut berubah: alatnya tidak bisa tertinggal dan
    berbohong.
    """
    gate = _gate()
    lo, hi = 0.0, max(round_trip_cost(slippage, hold) * 12.0, 0.05)
    for _ in range(48):
        tengah = (lo + hi) / 2.0
        lulus, _e, _c, _n = gate(tengah, slippage, hold)
        if lulus:
            hi = tengah
        else:
            lo = tengah
    return hi


def check(cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(galat, catatan).

    `galat` = struktur yang benar-benar rusak (data hilang senyap, expectancy negatif) -> exit 1.
    `catatan` = kenyataan tuning (knob yang ternyata tidak pernah jadi pembatas) -> tetap dicetak
    supaya tidak ada yang mengira angkanya bekerja, tapi tidak membuat pipeline merah permanen
    hanya karena sikap strategi.
    """
    galat: list[str] = []
    catatan: list[str] = []
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
    if hold <= 0 or min_funding <= 0:
        galat.append(f"expected_hold_hours={hold} / min_funding_daily={min_funding} wajib > 0")
        return galat, catatan

    gate = _gate()
    basis = active_slippage_basis()
    lulus, exp_funding, biaya, _note = gate(min_funding, basis, hold)
    ambang = e5_ambang(basis, hold)

    # Dilaporkan TANPA SYARAT. Sebelumnya catatan ini hanya muncul saat ada ketidakcocokan,
    # sehingga setelah dasar biaya diganti (hibrida terukur) laporan jadi diam dan tidak lagi
    # menjelaskan angka mana yang sebenarnya sedang menjaga pintu.
    from walt_bot import E5_SLIPPAGE_BASIS_ENV, measured_slippage_basis
    _basis_x, basis_note = measured_slippage_basis()
    sumber = (f"pernyataan venue (kill switch {E5_SLIPPAGE_BASIS_ENV}=venue)"
              if os.getenv(E5_SLIPPAGE_BASIS_ENV, "measured").strip().lower() == "venue"
              else f"terukur: {basis_note}")
    catatan.append(
        f"E5 memakai dasar slippage AKTIF {basis * 100:.2f}% — sumber: {sumber}. "
        f"Biaya round-trip {biaya * 100:.2f}% -> req {ambang * 100:.2f}%/hari, "
        f"floor min_funding_daily={min_funding * 100:.2f}%/hari. "
        f"Yang menahan entry otomatis adalah gerbang E5 di scan_with_research + "
        f"market_execute; jalur CLI `open` manual TIDAK melewati E5."
    )

    if exp_funding < biaya:
        # BUKAN galat selama gerbang E5 berdiri di SEMUA jalur entry otomatis: floor ini longgar,
        # tapi E5 (req = 1.5 x biaya) yang sebenarnya menahan. Invarian itu dikunci test
        # `test_setiap_jalur_entry_otomatis_digerbang_e5` — kalau someday ada jalur baru tanpa E5,
        # catatan ini berubah menjadi lubang rugi nyata.
        catatan.append(
            f"min_funding_daily={min_funding * 100:.2f}%/hari berada DI BAWAH biaya round-trip "
            f"({biaya * 100:.2f}% @ slippage aktif {basis * 100:.2f}%): floor ini sendiri "
            f"akan meloloskan expectancy negatif. Yang menahan adalah gerbang E5 "
            f"(req {ambang * 100:.2f}%/hari) di scan_with_research + market_execute. Jalur CLI "
            f"`open` manual TIDAK melewati E5."
        )
    elif not lulus:
        catatan.append(
            f"Yang menyaring entry BUKAN min_funding_daily ({min_funding * 100:.2f}%/hari) melainkan "
            f"gerbang E5: butuh funding >= {ambang * 100:.2f}%/hari pada slippage aktif "
            f"{basis * 100:.2f}%. Selama floor < ambang, floor tidak pernah jadi pembatas."
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
    return galat, catatan


def e5_tabel(hold: float = 24.0) -> list[tuple[str, float, float]]:
    """'Butuh funding berapa per jenis aset' — jawaban langsung atas SKIP massal 17:12Z."""
    return [
        (label, round_trip_cost(slip, hold), e5_ambang(slip, hold))
        for label, slip in (
            ("optimistik (spread sempit)", SLIPPAGE_OPTIMISTIS),
            ("teramati (log 17:12Z)", SLIPPAGE_TERAMATI),
        )
    ]


def main(argv: Optional[list[str]] = None) -> int:
    mentah = os.environ.get("STRATEGY_CONFIG_JSON", "").strip()
    sumber = ("STRATEGY_CONFIG_JSON (variable produksi — menimpa default kode)" if mentah
              else "DEFAULT_STRATEGY_CONFIG (kode)")
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

    print(f"sumber             = {sumber}")
    print(
        "angka_dipakai      = "
        + ", ".join(f"{k}={cfg.get(k)}" for k in (
            "max_research_age_hours", "min_funding_daily", "expected_hold_hours",
            "default_sl_pct", "tp_calm_pct", "daily_loss_limit_pct",
        ))
    )
    for label, biaya, ambang in e5_tabel(float(cfg.get("expected_hold_hours") or 24.0)):
        print(f"e5[{label:<24}] = biaya {biaya * 100:.2f}% -> butuh funding >= {ambang * 100:.2f}%/hari")
    galat, catatan = check(cfg)
    for c in catatan:
        print(f"CATATAN            = {c}")
    for g in galat:
        print(f"PELANGGARAN        = {g}")
    if galat:
        print(f"verdict            = TIDAK KOHEREN ({len(galat)} galat, {len(catatan)} catatan)")
        return 1
    print("verdict            = KOHEREN" + (f" ({len(catatan)} catatan)" if catatan else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
