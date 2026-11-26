"""Perencana order rule-based multi-pasar: Saham BEI dan Kripto Futures.

Modul ini MURNI. Seluruh fungsi inti (``build_order_plan``, ``evaluasi_trailing_stop_live``,
``round_price_adaptive``, dan kawan-kawan) tidak menyentuh jaringan, berkas, maupun keadaan
global saat diimpor. Efek samping hanya muncul bila pemanggil memang meminta ekspor atau
menjalankan CLI di bawah ``if __name__ == "__main__":``.

Filosofi angka yang dipakai di sini:
  1. ``risk_percent`` adalah risiko BERSIH. Kerugian saat SL sudah memuat fee beli dan fee jual,
     sehingga ``risiko_aktual`` tidak pernah diam-diam membengkak karena biaya.
  2. Daya beli selalu inklusif fee masuk: ``notional + fee_beli <= modal`` (BEI) dan
     ``margin + fee_beli <= modal`` (Futures). Batas ini diuji tepat di harga penuh.
  3. Harga mikro tidak pernah dipaksa ke lantai 0,0001. Presisi mengikuti besaran harga
     (4/6/8 desimal), karena koin berharga di bawah 0,0001 akan rusak oleh lantai tersebut.
  4. Semua kegagalan validasi dilempar sebagai ``OrderPlanValidationError`` (turunan
     ``ValueError``) dengan pesan Bahasa Indonesia. Fungsi library tidak pernah memanggil
     ``exit()`` / ``sys.exit()``.

Pemakaian CLI:
    python tools/order_planner.py                      # mode interaktif
    python tools/order_planner.py --market BEI --modal 10000000 --entry 1000 --sl 950 \
        --prev-close 1000 --symbol BBCA --json-stdout --export none
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any, Literal, Sequence

# =============================================================================
# Konfigurasi rule-based per pasar (satu sumber kebenaran)
# =============================================================================

MARKET_RULES: dict[str, dict[str, Any]] = {
    "BEI": {
        "label": "Saham BEI (Bursa Efek Indonesia)",
        "step_size_default": "100",   # 1 lot = 100 lembar
        "min_unit": 100.0,            # minimum order 1 lot
        "min_notional": 0.0,
        "fee_beli": 0.0015,           # 0,15% broker
        "fee_jual": 0.0025,           # 0,25% broker + pajak jual
        "max_sl_pct": 0.10,           # SL maksimum 10% (warning bila > 7%)
        "harga_min": 50.0,            # batas fraksi harga terendah BEI
        "max_leverage": 1.0,
    },
    "FUTURES": {
        "label": "Kripto Futures (perpetual)",
        "step_size_default": "0.001",
        "min_unit": 0.0,
        "min_notional": 5.0,          # minimum notional order bursa (USD)
        "fee_beli": 0.0009,           # taker 0,09% per sisi (selaras Walt Perps)
        "fee_jual": 0.0009,
        "max_sl_pct": 0.15,           # SL maksimum 15% (warning bila > 6%)
        "harga_min": 1e-12,
        "max_leverage": 50.0,
        "mmr_pct": 0.005,             # 0,5% Maintenance Margin Rate
    },
}

# Rencana multi-TP: (label, kelipatan R, porsi alokasi)
MULTI_TP_PLAN: tuple[tuple[str, float, float], ...] = (
    ("TP1", 1.5, 0.50),
    ("TP2", 3.0, 0.30),
    ("TP3", 5.0, 0.20),
)

# Tangga trailing progresif: (trigger dalam R, jarak trail dalam R). Indeks + 1 = stage.
TRAILING_TIERS: tuple[tuple[float, float], ...] = (
    (3.00, 1.50),   # Stage 1: aktif di +3,00R, kunci +1,50R
    (3.75, 1.00),   # Stage 2: aktif di +3,75R, kunci +1,00R
    (4.50, 0.50),   # Stage 3: aktif di +4,50R, kunci +0,50R
)

BEI_HARGA_MIN = 50.0
MAX_RISK_PERCENT = 5.0
LIQ_SAFETY_MULT = 2.0          # jarak likuidasi wajib >= 2x jarak SL
MIN_NOTIONAL_FUTURES = MARKET_RULES["FUTURES"]["min_notional"]

# Fraksi harga BEI: batas atas eksklusif -> tick (rupiah).
BEI_TICK_TIERS: tuple[tuple[float, int], ...] = (
    (200.0, 1),
    (500.0, 2),
    (2000.0, 5),
    (5000.0, 10),
    (math.inf, 25),
)

HEADER_CSV: list[str] = [
    "Tipe",
    "Label",
    "Harga",
    "Unit",
    "Alokasi_Pct",
    "Notional",
    "Fee_Estimasi",
    "Estimasi_Net_PnL",
    "R_Multiple",
    "Catatan",
]


class OrderPlanValidationError(ValueError):
    """Input rencana order tidak lolos validasi. Pesan selalu Bahasa Indonesia."""


# =============================================================================
# Helper angka murni
# =============================================================================


def get_tick_bei(harga: float) -> int:
    """Tick (fraksi harga) BEI untuk sebuah level harga, mengikuti papan fraksi IDX.

    Batas tier bersifat eksklusif di atas: 199 memakai tick 1, 200 memakai tick 2,
    499 memakai tick 2, 500 memakai tick 5, 1999 tick 5, 2000 tick 10, 4999 tick 10,
    dan 5000 ke atas tick 25.
    """
    h = float(harga)
    for batas, tick in BEI_TICK_TIERS:
        if h < batas:
            return tick
    return 25


def _bei_tick_floor(harga: float) -> int:
    h = float(harga)
    tick = get_tick_bei(h)
    return int(math.floor(h / tick)) * tick


def _bei_tick_ceil(harga: float) -> int:
    h = float(harga)
    tick = get_tick_bei(h)
    return int(math.ceil(h / tick)) * tick


def bulatkan_fraksi_bei(harga: float) -> int:
    """Bulatkan harga ke fraksi BEI yang VALID di tier hasil pembulatan.

    Mengulang pembulatan sampai tick stabil supaya hasil tidak pernah jatuh pada harga
    yang tidak sah (mis. 499 pada tier 200-500 yang mewajibkan kelipatan 2).
    """
    h = float(harga)
    if not math.isfinite(h):
        raise OrderPlanValidationError("Harga tidak terhingga (NaN/inf) tidak bisa dibulatkan.")
    if h < BEI_HARGA_MIN:
        raise OrderPlanValidationError(
            f"Harga {h:.4f} di bawah batas minimum BEI {BEI_HARGA_MIN:.0f}."
        )
    for _ in range(8):
        tick = get_tick_bei(h)
        baru = int(math.floor(h / tick + 0.5)) * tick
        if baru == h:
            return int(baru)
        if baru < BEI_HARGA_MIN:
            raise OrderPlanValidationError(
                f"Pembulatan fraksi menghasilkan {baru} (< {BEI_HARGA_MIN:.0f}) yang tidak sah."
            )
        h = float(baru)
    return int(round(h))


def _desimal_futures(nilai: float) -> int:
    """Jumlah desimal adaptif untuk Futures: 4 (>=1), 6 (>=0,01), 8 (<0,01)."""
    a = abs(float(nilai))
    if a >= 1.0:
        return 4
    if a >= 0.01:
        return 6
    return 8


def round_price_adaptive(val: float, is_bei: bool = False) -> float | int:
    """Bulatkan harga secara adaptif: BEI ke tick rupiah, Futures ke 4/6/8 desimal."""
    v = float(val)
    if not math.isfinite(v):
        raise OrderPlanValidationError("Harga tidak terhingga (NaN/inf) tidak bisa dibulatkan.")
    if is_bei:
        return bulatkan_fraksi_bei(v)
    return round(v, _desimal_futures(v))


def format_harga(val: float, is_bei: bool = False) -> str:
    """Format harga untuk CLI: BEI ribuan titik, Futures desimal dinamis tanpa nol sia-sia."""
    if is_bei:
        return f"{int(round(float(val))):,}".replace(",", ".")
    nilai = float(val)
    teks = f"{nilai:.{_desimal_futures(nilai)}f}"
    if "." in teks:
        teks = teks.rstrip("0").rstrip(".")
    return teks or "0"


def _desimal_step(step: Decimal) -> int:
    eksponen = step.as_tuple().exponent
    if not isinstance(eksponen, int):
        return 0
    return max(0, -eksponen)


def format_unit(qty: float, step: Decimal, is_bei: bool) -> str:
    """Representasi unit yang ramah dibaca, mis. '1.800 lembar (18 lot)'."""
    des = _desimal_step(step)
    if is_bei:
        teks = format_uang(qty, True, des)
        lot = float(qty) / 100.0
        return f"{teks} lembar ({lot:g} lot)"
    return f"{float(qty):.{des}f} unit"


def _angka_csv(nilai: float, desimal: int = 2) -> str:
    return f"{float(nilai):.{desimal}f}"


def format_uang(nilai: float, is_bei: bool = False, desimal: int = 2) -> str:
    """Format nilai uang: BEI memakai ribuan titik + desimal koma, Futures desimal titik."""
    angka = float(nilai)
    teks = f"{abs(angka):,.{desimal}f}"
    tanda = "-" if angka < 0 else ""
    if not is_bei:
        return tanda + teks
    return tanda + teks.replace(",", "\u00a0").replace(".", ",").replace("\u00a0", ".")


def _r6(nilai: float) -> float:
    """Rapikan noise float pada nilai uang tanpa menghapus presisi aset mikro."""
    hasil = round(float(nilai), 6)
    return 0.0 if hasil == 0 else hasil


# =============================================================================
# Input terstruktur
# =============================================================================


@dataclass
class OrderPlanInput:
    """Input tunggal perencana order. Semua field divalidasi di ``build_order_plan``."""

    market_key: Literal["BEI", "FUTURES"]
    modal: float
    harga_entry: float
    harga_sl: float
    risk_percent: float = 1.0
    arah_posisi: Literal["LONG", "SHORT"] = "LONG"
    leverage: float = 1.0
    step_size: str = "100"
    prev_close: float = 0.0
    symbol: str = ""


def _positif(nilai: Any, nama: str) -> float:
    """Konversi ke float dan pastikan > 0 serta berhingga."""
    try:
        hasil = float(nilai)
    except (TypeError, ValueError):
        raise OrderPlanValidationError(f"{nama} harus berupa angka, diterima: {nilai!r}.") from None
    if not math.isfinite(hasil):
        raise OrderPlanValidationError(f"{nama} harus berhingga (bukan NaN/inf).")
    if hasil <= 0:
        raise OrderPlanValidationError(f"{nama} harus > 0, diterima {hasil:g}.")
    return hasil


def _parse_step(teks: Any) -> Decimal:
    """Parse step_size menjadi Decimal > 0; galat apapun menjadi OrderPlanValidationError."""
    mentah = str(teks).strip()
    if not mentah:
        raise OrderPlanValidationError("step_size tidak boleh kosong (contoh: '100' atau '0.001').")
    try:
        step = Decimal(mentah)
    except (InvalidOperation, ValueError):
        raise OrderPlanValidationError(
            f"step_size '{mentah}' bukan angka desimal yang sah (contoh: '100' atau '0.001')."
        ) from None
    if not step.is_finite() or step <= 0:
        raise OrderPlanValidationError(f"step_size harus > 0, diterima '{mentah}'.")
    return step


def _pct_auto_reject(prev_close: float) -> float:
    """Persentase Auto Reject Atas/Bawah BEI berdasarkan band harga."""
    if prev_close <= 200:
        return 0.35
    if prev_close <= 5000:
        return 0.25
    return 0.20


def _batas_ara_arb(prev_close: float) -> tuple[int, int, float]:
    """Kembalikan (ARA, ARB, pct) yang sudah dibulatkan ke fraksi valid.

    ARA dibulatkan ke bawah (tidak boleh melampaui batas bursa) dan ARB ke atas,
    dengan lantai harga minimum BEI supaya tidak pernah menghasilkan level di bawah 50.
    """
    pct = _pct_auto_reject(prev_close)
    ara = float(_bei_tick_floor(prev_close * (1.0 + pct)))
    arb = float(_bei_tick_ceil(max(BEI_HARGA_MIN, prev_close * (1.0 - pct))))
    return int(ara), int(arb), pct


def _floor_step(nilai: float, step: Decimal) -> float:
    """Bulatkan ke bawah ke kelipatan step memakai Decimal (bebas drift float)."""
    if nilai <= 0:
        return 0.0
    langkah = (Decimal(str(nilai)) / step).to_integral_value(rounding=ROUND_FLOOR)
    return float(langkah * step)


def _kurangi_step(nilai: float, step: Decimal) -> float:
    sisa = Decimal(str(nilai)) - step
    return float(sisa) if sisa > 0 else 0.0


def _siapkan_konteks(inp: OrderPlanInput) -> dict[str, Any]:
    """Validasi input dan siapkan konteks hitung. Tidak menulis apa pun."""
    market = str(inp.market_key).strip().upper()
    if market not in MARKET_RULES:
        raise OrderPlanValidationError(
            f"market_key '{inp.market_key}' tidak dikenal; pilih 'BEI' atau 'FUTURES'."
        )
    aturan = MARKET_RULES[market]
    is_bei = market == "BEI"

    modal = _positif(inp.modal, "modal")
    entry_input = _positif(inp.harga_entry, "harga entry")
    sl_input = _positif(inp.harga_sl, "harga SL")

    try:
        risk_pct = float(inp.risk_percent)
    except (TypeError, ValueError):
        raise OrderPlanValidationError("risk_percent harus berupa angka.") from None
    if not math.isfinite(risk_pct) or not (0 < risk_pct <= MAX_RISK_PERCENT):
        raise OrderPlanValidationError(
            f"risk_percent harus di antara 0 dan {MAX_RISK_PERCENT:g}, diterima {risk_pct:g}."
        )

    arah = str(inp.arah_posisi).strip().upper()
    if arah not in ("LONG", "SHORT"):
        raise OrderPlanValidationError(f"arah_posisi harus 'LONG' atau 'SHORT', diterima {arah!r}.")

    step = _parse_step(inp.step_size)
    warnings: list[str] = []

    if is_bei:
        if arah != "LONG":
            raise OrderPlanValidationError("BEI tidak mendukung posisi SHORT; gunakan arah LONG.")
        if step != step.to_integral_value() or step < 1:
            raise OrderPlanValidationError(
                f"step_size BEI harus bilangan bulat lembar >= 1, diterima '{inp.step_size}'."
            )
        try:
            leverage_input = float(inp.leverage)
        except (TypeError, ValueError):
            leverage_input = 1.0
        if leverage_input != 1.0:
            warnings.append(
                f"Leverage {leverage_input:g}x diabaikan untuk BEI (selalu 1x, tanpa margin)."
            )
        leverage = 1.0
    else:
        try:
            leverage = float(inp.leverage)
        except (TypeError, ValueError):
            raise OrderPlanValidationError("leverage harus berupa angka.") from None
        if not math.isfinite(leverage) or leverage < 1.0:
            raise OrderPlanValidationError(f"leverage harus >= 1, diterima {leverage:g}.")
        if leverage > float(aturan["max_leverage"]):
            raise OrderPlanValidationError(
                f"leverage {leverage:g}x melampaui batas pasar {aturan['max_leverage']:g}x."
            )

    # --- harga eksekusi setelah pembulatan fraksi ---
    ara = arb = 0
    pct_auto = 0.0
    liq_harga = 0.0
    max_safe_lev = 1.0
    prev = 0.0
    mmr = float(aturan.get("mmr_pct", 0.0))

    if is_bei:
        prev = _positif(inp.prev_close, "prev_close (wajib untuk BEI)")
        if prev < BEI_HARGA_MIN:
            raise OrderPlanValidationError(
                f"prev_close {prev:g} di bawah harga minimum BEI {BEI_HARGA_MIN:.0f}."
            )
        entry = float(bulatkan_fraksi_bei(entry_input))
        sl = float(bulatkan_fraksi_bei(sl_input))
        if sl >= entry:
            raise OrderPlanValidationError(
                f"SL BEI ({sl:g}) harus di bawah harga entry ({entry:g}) untuk posisi LONG."
            )
        ara, arb, pct_auto = _batas_ara_arb(prev)
        if entry > ara:
            raise OrderPlanValidationError(
                f"Harga entry {entry:g} di atas ARA {ara:g} (prev_close {prev:g})."
            )
        if entry < arb:
            raise OrderPlanValidationError(
                f"Harga entry {entry:g} di bawah ARB {arb:g} (prev_close {prev:g})."
            )
    else:
        entry = float(round_price_adaptive(entry_input))
        sl = float(round_price_adaptive(sl_input))
        if arah == "LONG" and sl >= entry:
            raise OrderPlanValidationError(
                f"SL LONG ({sl:g}) harus di bawah entry ({entry:g})."
            )
        if arah == "SHORT" and sl <= entry:
            raise OrderPlanValidationError(
                f"SL SHORT ({sl:g}) harus di atas entry ({entry:g})."
            )

    jarak = abs(entry - sl)
    sl_pct = jarak / entry
    if sl_pct <= 0:
        raise OrderPlanValidationError("Jarak SL nol: harga entry dan SL tidak boleh sama.")
    if sl_pct > float(aturan["max_sl_pct"]) + 1e-12:
        raise OrderPlanValidationError(
            f"SL {sl_pct * 100:.2f}% melampaui batas {aturan['max_sl_pct'] * 100:.2f}% "
            f"untuk pasar {market}."
        )

    if is_bei:
        if sl < BEI_HARGA_MIN:
            raise OrderPlanValidationError(
                f"Harga SL {sl:g} di bawah batas minimum BEI {BEI_HARGA_MIN:.0f}."
            )
        if sl < arb:
            warnings.append(
                f"SL {sl:g} berada di bawah ARB {arb:g}; order SL berisiko ditolak bursa."
            )
    else:
        if 1.0 / leverage <= mmr:
            raise OrderPlanValidationError(
                f"Leverage {leverage:g}x terlalu tinggi terhadap Maintenance Margin Rate "
                f"{mmr * 100:.2f}% (likuidasi langsung)."
            )
        max_safe_lev = min(
            float(aturan["max_leverage"]),
            1.0 / (sl_pct * LIQ_SAFETY_MULT + mmr),
        )
        if leverage > max_safe_lev + 1e-9:
            raise OrderPlanValidationError(
                f"leverage {leverage:g}x melampaui leverage maksimum aman {max_safe_lev:.2f}x "
                f"untuk SL {sl_pct * 100:.2f}% dan MMR {mmr * 100:.2f}% (risiko likuidasi sebelum SL)."
            )
        liq_harga = (
            entry * (1.0 - (1.0 / leverage) + mmr)
            if arah == "LONG"
            else entry * (1.0 + (1.0 / leverage) - mmr)
        )
        if liq_harga <= 0:
            raise OrderPlanValidationError(
                f"Harga likuidasi {liq_harga:g} tidak valid pada leverage {leverage:g}x."
            )
        if arah == "LONG" and sl <= liq_harga:
            raise OrderPlanValidationError(
                f"SL LONG {sl:g} sudah menyentuh harga likuidasi {liq_harga:.8g}; "
                "perkecil leverage atau rapatkan SL."
            )
        if arah == "SHORT" and sl >= liq_harga:
            raise OrderPlanValidationError(
                f"SL SHORT {sl:g} sudah menyentuh harga likuidasi {liq_harga:.8g}; "
                "perkecil leverage atau rapatkan SL."
            )
        jarak_liq_pct = max(0.0, (1.0 / leverage - mmr) * 100.0)
        if leverage >= 0.8 * max_safe_lev:
            warnings.append(
                f"Leverage {leverage:g}x mendekati batas aman {max_safe_lev:.2f}x "
                f"(jarak likuidasi {jarak_liq_pct:.2f}% vs SL {sl_pct * 100:.2f}%)."
            )

    return {
        "market": market,
        "aturan": aturan,
        "is_bei": is_bei,
        "modal": modal,
        "entry": entry,
        "sl": sl,
        "entry_input": entry_input,
        "sl_input": sl_input,
        "prev": prev,
        "ara": ara,
        "arb": arb,
        "pct_auto": pct_auto,
        "liq": liq_harga,
        "mmr": mmr,
        "max_safe_lev": max_safe_lev,
        "leverage": leverage,
        "arah": arah,
        "step": step,
        "warnings": warnings,
        "risk_pct": risk_pct,
        "jarak": jarak,
        "sl_pct": sl_pct,
        "f_beli": float(aturan["fee_beli"]),
        "f_jual": float(aturan["fee_jual"]),
        "symbol": str(inp.symbol or "").strip(),
    }


def _hitung_eksekusi(ctx: dict[str, Any]) -> dict[str, Any]:
    """Hitung kuantitas, daya beli, fee, dan margin. Batas daya beli inklusif fee masuk."""
    aturan = ctx["aturan"]
    modal = ctx["modal"]
    entry = ctx["entry"]
    sl = ctx["sl"]
    step: Decimal = ctx["step"]
    is_bei = ctx["is_bei"]
    leverage = ctx["leverage"]
    f_beli = ctx["f_beli"]
    f_jual = ctx["f_jual"]

    risk_budget = modal * ctx["risk_pct"] / 100.0
    # Risiko BERSIH per unit: pergerakan harga + fee beli + fee jual.
    loss_per_unit = ctx["jarak"] + entry * f_beli + sl * f_jual
    if loss_per_unit <= 0:
        raise OrderPlanValidationError("Risiko per unit nol; periksa harga entry/SL.")
    qty_risiko = risk_budget / loss_per_unit

    if is_bei:
        # notional * (1 + f_beli) <= modal
        max_buying_power = modal / (1.0 + f_beli)
    else:
        # margin + fee masuk <= modal, dengan margin = notional / leverage
        max_buying_power = modal / ((1.0 / leverage) + f_beli)
    qty_daya_beli = max_buying_power / entry

    qty = _floor_step(min(qty_risiko, qty_daya_beli), step)
    # Jaring pengaman pembulatan float: mundur satu step bila masih melewati plafon.
    while qty > 0 and qty * entry > max_buying_power * (1.0 + 1e-12):
        qty = _kurangi_step(qty, step)

    if qty <= 0:
        raise OrderPlanValidationError(
            f"Alokasi dana tidak mencapai 1 step ({step}); modal {modal:g} terlalu kecil "
            f"untuk entry {entry:g} dengan risiko {ctx['risk_pct']:g}%. "
            "Periksa juga step_size (BEI '100', FUTURES '0.001')."
        )
    if is_bei and qty < float(aturan["min_unit"]):
        raise OrderPlanValidationError(
            f"Alokasi hanya {qty:g} lembar, kurang dari 1 lot (100 lembar). "
            "Perbesar modal atau risk_percent."
        )

    notional = qty * entry
    if not is_bei and notional < float(aturan["min_notional"]) - 1e-9:
        raise OrderPlanValidationError(
            f"Notional {notional:.4f} di bawah minimum order bursa "
            f"{float(aturan['min_notional']):g}. Perbesar modal atau longgarkan SL."
        )

    fee_entry = notional * f_beli
    margin_terpakai = notional if is_bei else notional / leverage
    total_kas = margin_terpakai + fee_entry
    if total_kas > modal * (1.0 + 1e-9):
        # Tidak boleh terjadi; bila terjadi berarti formula daya beli salah.
        raise OrderPlanValidationError(
            f"Daya beli terlampaui: kas terpakai {total_kas:.6f} > modal {modal:g}."
        )

    risiko_aktual = qty * loss_per_unit
    return {
        "risk_budget": _r6(risk_budget),
        "loss_per_unit": _r6(loss_per_unit),
        "qty_risiko": qty_risiko,
        "qty_daya_beli": qty_daya_beli,
        "qty": qty,
        "max_buying_power": _r6(max_buying_power),
        "notional": _r6(notional),
        "fee_entry": _r6(fee_entry),
        "fee_exit_estimasi": _r6(notional * f_jual),
        "margin_terpakai": _r6(margin_terpakai),
        "total_kas": _r6(total_kas),
        "sisa_modal": _r6(modal - total_kas),
        "risiko_aktual": _r6(risiko_aktual),
    }


def _pnl_bruto(unit: float, harga_exit: float, entry: float, arah: str) -> float:
    """PnL harga (belum fee) untuk sejumlah unit."""
    if arah == "LONG":
        return unit * (harga_exit - entry)
    return unit * (entry - harga_exit)


def _pnl_bersih(
    unit: float, harga_exit: float, entry: float, arah: str, f_beli: float, f_jual: float
) -> float:
    """PnL setelah fee masuk dan fee keluar untuk sejumlah unit."""
    if unit <= 0:
        return 0.0
    return _pnl_bruto(unit, harga_exit, entry, arah) - (
        unit * entry * f_beli + unit * harga_exit * f_jual
    )


def _harga_target(raw: float, entry: float, arah: str, is_bei: bool) -> float:
    """Bulatkan target TP/trailing dan jaga arah harga agar tidak melewati entry.

    Lantai ``entry * 1e-6`` mencegah harga nol/negatif pada koin mikro tanpa memaksa
    lantai 0,0001 yang merusak aset berharga jauh di bawah itu.
    """
    lantai = abs(entry) * 1e-6
    nilai = max(lantai, float(raw))
    if is_bei:
        hasil = bulatkan_fraksi_bei(nilai)
        tick = get_tick_bei(hasil)
        if arah == "LONG" and hasil <= entry:
            hasil += tick
        elif arah == "SHORT" and hasil >= entry:
            hasil -= tick
        if hasil < BEI_HARGA_MIN:
            raise OrderPlanValidationError(
                f"Target harga {hasil} di bawah batas minimum BEI {BEI_HARGA_MIN:.0f}."
            )
        return float(hasil)
    hasil = float(round_price_adaptive(nilai))
    des = _desimal_futures(hasil)
    satuan = 10.0 ** (-des)
    if arah == "LONG" and hasil <= entry:
        hasil = round(hasil + satuan, des)
    elif arah == "SHORT" and hasil >= entry:
        hasil = round(hasil - satuan, des)
    if hasil <= 0:
        raise OrderPlanValidationError(
            f"Target harga {hasil:g} tidak valid (harus > 0) untuk entry {entry:g}."
        )
    return hasil


def _harga_bep(entry: float, arah: str, f_beli: float, f_jual: float, is_bei: bool) -> float:
    """Harga impas setelah fee dua sisi; dibulatkan ke arah yang tidak merugikan."""
    if arah == "LONG":
        raw = entry * (1.0 + f_beli) / (1.0 - f_jual)
    else:
        raw = entry * (1.0 - f_beli) / (1.0 + f_jual)
    if is_bei:
        hasil = float(_bei_tick_ceil(raw)) if arah == "LONG" else float(_bei_tick_floor(raw))
    else:
        hasil = float(round_price_adaptive(raw))
        des = _desimal_futures(hasil)
        satuan = 10.0 ** (-des)
        if arah == "LONG" and hasil <= entry:
            hasil = round(hasil + satuan, des)
        elif arah == "SHORT" and hasil >= entry:
            hasil = round(hasil - satuan, des)
    if hasil <= 0:
        raise OrderPlanValidationError(f"Harga BEP {hasil:g} tidak valid (harus > 0).")
    return hasil


def _alokasi_tranche(total: float, step: Decimal) -> list[float]:
    """Bagi total unit ke 3 tranche (50/30/20); 1 step -> TP2 (3R), 2 step -> TP1+TP2."""
    total_d = Decimal(str(total))
    total_steps = int((total_d / step).to_integral_value(rounding=ROUND_FLOOR))
    if total_steps <= 0:
        return [0.0, 0.0, 0.0]
    if total_steps == 1:
        return [0.0, float(total_d), 0.0]
    if total_steps == 2:
        return [float(step), float(step), 0.0]

    s1 = max(1, math.floor(total_steps * MULTI_TP_PLAN[0][2]))
    s2 = max(1, math.floor(total_steps * MULTI_TP_PLAN[1][2]))
    s3 = total_steps - s1 - s2
    if s3 <= 0:
        s1 = max(1, total_steps - 2)
        s2 = 1
        s3 = total_steps - s1 - s2
    return [
        float(Decimal(s1) * step),
        float(Decimal(s2) * step),
        float(Decimal(s3) * step),
    ]


def _bangun_tranches(ctx: dict[str, Any], eksekusi: dict[str, Any]) -> list[dict[str, Any]]:
    """Bangun 3 tranche TP (50/30/20) lengkap dengan harga dan PnL bersih."""
    entry = ctx["entry"]
    arah = ctx["arah"]
    is_bei = ctx["is_bei"]
    jarak = ctx["jarak"]
    f_beli = ctx["f_beli"]
    f_jual = ctx["f_jual"]
    warnings: list[str] = ctx["warnings"]
    total = eksekusi["qty"]
    units = _alokasi_tranche(total, ctx["step"])

    tranches: list[dict[str, Any]] = []
    for (label, r_multiple, porsi), unit in zip(MULTI_TP_PLAN, units):
        raw = entry + (jarak * r_multiple if arah == "LONG" else -jarak * r_multiple)
        harga = _harga_target(raw, entry, arah, is_bei)
        di_atas_ara = bool(is_bei and harga > ctx["ara"])
        if di_atas_ara:
            warnings.append(
                f"{label} {format_harga(harga, True)} melampaui ARA hari ini "
                f"{format_harga(ctx['ara'], True)} — target bersifat multi-hari (tidak dipotong)."
            )
        net = _pnl_bersih(unit, harga, entry, arah, f_beli, f_jual)
        porsi_aktual = (unit / total * 100.0) if total > 0 else 0.0
        tranches.append(
            {
                "label": label,
                "r_multiple": r_multiple,
                "alokasi_pct_rencana": round(porsi * 100.0, 2),
                "alokasi_pct_aktual": round(porsi_aktual, 4),
                "harga": harga,
                "di_atas_ara": di_atas_ara,
                "harga_di_clamp": False,
                "unit": unit,
                "aktif": unit > 0,
                "estimasi_pnl_kotor": _r6(_pnl_bruto(unit, harga, entry, arah)),
                "estimasi_net_pnl": _r6(net),
            }
        )
    return tranches


def _bangun_trailing(
    ctx: dict[str, Any], eksekusi: dict[str, Any], tranches: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Bangun tangga trailing progresif beserta harga trigger, unit runner, dan lock SL."""
    entry = ctx["entry"]
    arah = ctx["arah"]
    is_bei = ctx["is_bei"]
    jarak = ctx["jarak"]
    unit_runner = float(tranches[2]["unit"])
    tanda = 1.0 if arah == "LONG" else -1.0

    aturan_trail: list[dict[str, Any]] = []
    for stage, (trigger_r, trail_r) in enumerate(TRAILING_TIERS, start=1):
        trigger_harga = _harga_target(
            entry + tanda * trigger_r * jarak, entry, arah, is_bei
        )
        # Trail diukur dari harga puncak, bukan dari entry: makin tinggi stage, makin ketat.
        lock_r = trigger_r - trail_r
        lock_harga = _harga_target(entry + tanda * lock_r * jarak, entry, arah, is_bei)
        net = _pnl_bersih(unit_runner, lock_harga, entry, arah, ctx["f_beli"], ctx["f_jual"])
        aturan_trail.append(
            {
                "stage": stage,
                "trigger_r": trigger_r,
                "trigger_harga": trigger_harga,
                "trail_r": trail_r,
                "sl_lock_r": round(lock_r, 4),
                "sl_lock_harga": lock_harga,
                "unit_runner": unit_runner,
                "aktif": unit_runner > 0,
                "estimasi_net_pnl": _r6(net),
                "catatan": (
                    f"Aktif saat profit >= {trigger_r:.2f}R untuk sisa runner ({unit_runner:g} unit) "
                    f"setelah TP1+TP2; SL mengikuti puncak -{trail_r:.2f}R "
                    f"(setara kunci +{lock_r:.2f}R saat trigger)."
                ),
            }
        )
    return aturan_trail


def _bangun_skenario(
    ctx: dict[str, Any],
    eksekusi: dict[str, Any],
    tranches: list[dict[str, Any]],
    trailing: list[dict[str, Any]],
) -> dict[str, Any]:
    """Matriks skenario: SL awal, TP1 lalu BEP, TP1+TP2 lalu trailing, semua TP."""
    entry = ctx["entry"]
    arah = ctx["arah"]
    qty = eksekusi["qty"]
    modal = ctx["modal"]
    f_beli = ctx["f_beli"]
    f_jual = ctx["f_jual"]

    net_sl = _pnl_bersih(qty, ctx["sl"], entry, arah, f_beli, f_jual)
    sl_kena = {
        "aktif": True,
        "harga": ctx["sl"],
        "net_pnl": _r6(net_sl),
        "net_pnl_pct_modal": round(net_sl / modal * 100.0, 4),
        "catatan": "Rugi bersih bila SL awal tersentuh (sudah termasuk fee dua sisi).",
    }

    unit_tp1 = float(tranches[0]["unit"])
    unit_tp2 = float(tranches[1]["unit"])
    bep = _harga_bep(entry, arah, f_beli, f_jual, ctx["is_bei"])
    sisa_tp1 = qty - unit_tp1
    net_tp1 = _pnl_bersih(unit_tp1, tranches[0]["harga"], entry, arah, f_beli, f_jual)
    net_bep_sisa = _pnl_bersih(sisa_tp1, bep, entry, arah, f_beli, f_jual)
    auto_bep = unit_tp1 > 0
    tp1_lalu_bep = {
        "aktif": auto_bep,
        "harga_bep": bep,
        "unit_di_tp1": unit_tp1,
        "net_pnl": _r6(net_tp1 + net_bep_sisa) if auto_bep else None,
        "catatan": (
            "TP1 terisi lalu SL digeser ke BEP; sisa posisi ditutup impas."
            if auto_bep
            else "TP1 tidak aktif (unit 0) — AUTO_BEP dimatikan agar tidak menyesatkan."
        ),
    }

    net_tp2 = _pnl_bersih(unit_tp2, tranches[1]["harga"], entry, arah, f_beli, f_jual)
    sisa_tp12 = qty - unit_tp1 - unit_tp2
    lock_trail = trailing[0]["sl_lock_harga"]
    net_trail = _pnl_bersih(sisa_tp12, lock_trail, entry, arah, f_beli, f_jual)
    aktif_tp12 = unit_tp1 > 0 and unit_tp2 > 0 and sisa_tp12 > 0
    tp1_tp2_lalu_trail = {
        "aktif": aktif_tp12,
        "harga_trailing": lock_trail,
        "unit_sisa": sisa_tp12,
        "net_pnl": _r6(net_tp1 + net_tp2 + net_trail) if aktif_tp12 else None,
        "catatan": (
            "TP1 dan TP2 terisi; sisa posisi ditutup di lock trailing Stage 1."
            if aktif_tp12
            else "TP2 atau sisa runner TP3 tidak punya unit — skenario trailing dilewati."
        ),
    }

    net_semua = sum(float(t["estimasi_net_pnl"]) for t in tranches)
    semua_tp = {
        "aktif": qty > 0,
        "net_pnl": _r6(net_semua),
        "net_pnl_pct_modal": round(net_semua / modal * 100.0, 4),
        "catatan": "Seluruh tranche TP terisi sesuai rencana.",
    }

    return {
        "sl_kena": sl_kena,
        "tp1_lalu_bep": tp1_lalu_bep,
        "tp1_tp2_lalu_trail": tp1_tp2_lalu_trail,
        "semua_tp": semua_tp,
    }


def build_order_plan(inp: OrderPlanInput, *, waktu: str | None = None) -> dict[str, Any]:
    """Bangun rencana order lengkap dari satu input. Fungsi murni (tanpa I/O).

    ``waktu`` opsional agar hasil bisa sepenuhnya deterministik (dipakai unit test);
    bila kosong, dipakai waktu UTC saat pemanggilan.
    """
    ctx = _siapkan_konteks(inp)
    eksekusi = _hitung_eksekusi(ctx)
    tranches = _bangun_tranches(ctx, eksekusi)
    trailing = _bangun_trailing(ctx, eksekusi, tranches)
    skenario = _bangun_skenario(ctx, eksekusi, tranches, trailing)
    warnings: list[str] = ctx["warnings"]

    auto_bep = bool(tranches[0]["unit"] > 0)
    if not auto_bep:
        warnings.append(
            "TP1 tidak mendapat unit (posisi hanya 1 step) — AUTO_BEP dimatikan dan tidak "
            "ditulis ke CSV agar tidak menyesatkan."
        )
    if ctx["risk_pct"] > 2.0:
        warnings.append(
            f"Risiko {ctx['risk_pct']:g}% modal per trade tergolong agresif; "
            "pertimbangkan <= 2%."
        )
    if ctx["is_bei"] and ctx["sl_pct"] > 0.07:
        warnings.append(
            f"SL {ctx['sl_pct'] * 100:.2f}% di atas 7% tergolong agresif untuk BEI."
        )
    if not ctx["is_bei"] and ctx["sl_pct"] > 0.06:
        warnings.append(
            f"SL {ctx['sl_pct'] * 100:.2f}% di atas 6% tergolong agresif untuk Futures."
        )
    if not ctx["is_bei"] and ctx["sl_pct"] < 3.0 * (ctx["f_beli"] + ctx["f_jual"]):
        warnings.append(
            f"SL {ctx['sl_pct'] * 100:.2f}% sangat dekat; fee round-trip "
            f"{(ctx['f_beli'] + ctx['f_jual']) * 100:.2f}% mengambil porsi besar dari risiko."
        )
    if ctx["is_bei"] and eksekusi["qty"] < 200:
        warnings.append(
            f"Posisi hanya {eksekusi['qty']:g} lembar; pembagian 3 tranche TP tidak proporsional."
        )
    if ctx["arah"] == "SHORT":
        warnings.append(
            "Posisi SHORT membayar funding bila rate positif; pantau funding harian."
        )

    waktu_rencana = waktu or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    metadata: dict[str, Any] = {
        "versi_skema": "1.0",
        "market": ctx["market"],
        "market_label": ctx["aturan"]["label"],
        "symbol": ctx["symbol"] or "-",
        "waktu_rencana": waktu_rencana,
        "arah_posisi": ctx["arah"],
        "leverage": ctx["leverage"],
        "modal": _r6(ctx["modal"]),
        "harga_entry_input": ctx["entry_input"],
        "harga_sl_input": ctx["sl_input"],
        "harga_entry_eksekusi": ctx["entry"],
        "harga_sl_eksekusi": ctx["sl"],
        "prev_close": ctx["prev"],
        "risk_percent": ctx["risk_pct"],
        "step_size": str(ctx["step"]),
        "auto_bep_aktif": auto_bep,
    }

    jml = eksekusi["qty"]
    risk_parameters: dict[str, Any] = {
        "risiko_rencana": eksekusi["risk_budget"],
        "risiko_aktual": eksekusi["risiko_aktual"],
        "risiko_aktual_pct_modal": round(eksekusi["risiko_aktual"] / ctx["modal"] * 100.0, 4),
        "loss_per_unit": eksekusi["loss_per_unit"],
        "sl_distance": _r6(ctx["jarak"]),
        "sl_pct": round(ctx["sl_pct"], 6),
        "sl_percent": round(ctx["sl_pct"] * 100.0, 4),
        "max_sl_pct": ctx["aturan"]["max_sl_pct"],
        "rr_tp": [r for _label, r, _pct in MULTI_TP_PLAN],
        "leverage": ctx["leverage"],
        "leverage_maks_aman": round(ctx["max_safe_lev"], 4),
        "harga_likuidasi": _r6(ctx["liq"]) if not ctx["is_bei"] else None,
        "jarak_likuidasi_pct": (
            round(max(0.0, (1.0 / ctx["leverage"] - ctx["mmr"]) * 100.0), 4)
            if not ctx["is_bei"]
            else None
        ),
        "harga_ara": ctx["ara"] if ctx["is_bei"] else None,
        "harga_arb": ctx["arb"] if ctx["is_bei"] else None,
        "auto_reject_pct": ctx["pct_auto"] if ctx["is_bei"] else None,
    }

    order_execution: dict[str, Any] = {
        "max_buying_power": eksekusi["max_buying_power"],
        "notional_aktual": eksekusi["notional"],
        "fee_beli_pct": ctx["f_beli"],
        "fee_jual_pct": ctx["f_jual"],
        "fee_entry_total": eksekusi["fee_entry"],
        "fee_exit_total_estimasi": eksekusi["fee_exit_estimasi"],
        "margin_terpakai": eksekusi["margin_terpakai"],
        "total_kas_terpakai": eksekusi["total_kas"],
        "sisa_modal": eksekusi["sisa_modal"],
        "jumlah_unit_total": jml,
        "jumlah_unit_display": format_unit(jml, ctx["step"], ctx["is_bei"]),
        "jumlah_lot": round(jml / 100.0, 4) if ctx["is_bei"] else None,
        "step_size": str(ctx["step"]),
        "harga_entry_eksekusi": ctx["entry"],
        "harga_sl_eksekusi": ctx["sl"],
        "qty_dibatasi_daya_beli": bool(eksekusi["qty_daya_beli"] < eksekusi["qty_risiko"]),
        "auto_bep_aktif": auto_bep,
    }

    return {
        "metadata": metadata,
        "risk_parameters": risk_parameters,
        "order_execution": order_execution,
        "take_profit_tranches": tranches,
        "progressive_trailing_rules": trailing,
        "scenario_matrix": skenario,
        "warnings": warnings,
    }


def evaluasi_trailing_stop_live(
    harga_entry: float,
    harga_sl_awal: float,
    harga_puncak: float,
    sl_saat_ini: float,
    arah: str = "LONG",
    is_bei: bool = False,
    tiers: Sequence[tuple[float, float]] = TRAILING_TIERS,
) -> dict[str, Any]:
    """Evaluasi trailing stop progresif dengan aturan ratchet yang ketat.

    LONG  : ``sl_baru = max(sl_saat_ini, kandidat)`` — SL tidak pernah turun.
    SHORT : ``sl_baru = min(sl_saat_ini, kandidat)`` — SL tidak pernah naik dan tidak <= 0.
    Input tidak valid dikembalikan sebagai ``status = "ERROR"`` (tidak melempar exception)
    agar pemanggil live-loop tidak mati karena satu baris data rusak.
    """
    arah_up = str(arah).strip().upper()

    def _error(pesan: str) -> dict[str, Any]:
        return {
            "status": "ERROR",
            "reason": pesan,
            "sl_baru": sl_saat_ini,
            "sl_kandidat": None,
            "stage": 0,
            "profit_r": None,
        }

    for nama, nilai in (
        ("harga_entry", harga_entry),
        ("harga_sl_awal", harga_sl_awal),
        ("harga_puncak", harga_puncak),
        ("sl_saat_ini", sl_saat_ini),
    ):
        try:
            angka = float(nilai)
        except (TypeError, ValueError):
            return _error(f"{nama} bukan angka: {nilai!r}.")
        if not math.isfinite(angka) or angka <= 0:
            return _error(f"{nama} harus > 0 dan berhingga, diterima {nilai!r}.")
    if arah_up not in ("LONG", "SHORT"):
        return _error(f"arah harus 'LONG' atau 'SHORT', diterima {arah!r}.")
    if not tiers:
        return _error("Tangga TRAILING_TIERS kosong; tidak ada aturan trailing yang bisa dievaluasi.")

    entry = float(harga_entry)
    sl_awal = float(harga_sl_awal)
    puncak = float(harga_puncak)
    sl_kini = float(sl_saat_ini)
    r_unit = abs(entry - sl_awal)
    if r_unit <= 0:
        return _error("Jarak SL awal nol: tidak ada satuan R untuk menghitung trailing.")

    profit_r = (puncak - entry) / r_unit if arah_up == "LONG" else (entry - puncak) / r_unit

    terpilih: tuple[int, float, float] | None = None
    for indeks, (trigger_r, trail_r) in enumerate(tiers, start=1):
        if profit_r + 1e-12 >= float(trigger_r):
            terpilih = (indeks, float(trigger_r), float(trail_r))
    if terpilih is None:
        alasan = (
            f"Profit {profit_r:.2f}R belum mencapai trigger pertama "
            f"{float(tiers[0][0]):.2f}R; SL tetap."
        )
        return {
            "status": "BELUM_AKTIF",
            "reason": alasan,
            "sl_baru": sl_kini,
            "sl_kandidat": None,
            "stage": 0,
            "profit_r": round(profit_r, 4),
        }

    stage, trigger_r, trail_r = terpilih
    tanda = 1.0 if arah_up == "LONG" else -1.0
    # Trail diukur dari harga puncak: LONG puncak - trail, SHORT puncak + trail.
    raw = puncak - tanda * trail_r * r_unit
    lantai = BEI_HARGA_MIN if is_bei else max(entry * 1e-6, 1e-8)
    kandidat = float(round_price_adaptive(max(lantai, raw), is_bei))
    if kandidat <= 0:
        return _error(f"Kandidat SL {kandidat:g} tidak valid (harus > 0).")

    if arah_up == "LONG":
        sl_baru = max(sl_kini, kandidat)
    else:
        sl_baru = min(sl_kini, kandidat)
        if sl_baru <= 0:
            sl_baru = kandidat

    if sl_baru != sl_kini:
        alasan = (
            f"Stage {stage} aktif di {profit_r:.2f}R (trigger {trigger_r:.2f}R); "
            f"SL digeser {'naik' if arah_up == 'LONG' else 'turun'} "
            f"dari {sl_kini:g} ke {sl_baru:g}."
        )
    else:
        alasan = (
            f"Stage {stage} aktif di {profit_r:.2f}R, tetapi kandidat {kandidat:g} "
            "tidak memperbaiki SL (ratchet menahan)."
        )
    return {
        "status": "AKTIF",
        "reason": alasan,
        "sl_baru": sl_baru,
        "sl_kandidat": kandidat,
        "stage": stage,
        "trigger_r": trigger_r,
        "trail_r": trail_r,
        "profit_r": round(profit_r, 4),
        "berubah": bool(sl_baru != sl_kini),
    }


def _baris_csv(rencana: dict[str, Any]) -> list[list[str]]:
    """Susun baris CSV 10 kolom dari payload. Baris AUTO_BEP hanya bila benar-benar aktif."""
    md = rencana["metadata"]
    oe = rencana["order_execution"]
    rp = rencana["risk_parameters"]
    scn = rencana["scenario_matrix"]
    is_bei = md["market"] == "BEI"
    step = Decimal(str(md["step_size"]))
    total = float(oe["jumlah_unit_total"])

    def _harga(nilai: float) -> str:
        return _angka_csv(nilai, 0 if is_bei else _desimal_futures(nilai))

    def _unit(nilai: float) -> str:
        return _angka_csv(nilai, _desimal_step(step))

    f_jual = float(oe["fee_jual_pct"])
    baris: list[list[str]] = [
        [
            "ENTRY",
            "Entry",
            _harga(oe["harga_entry_eksekusi"]),
            _unit(total),
            "100.00",
            _angka_csv(oe["notional_aktual"]),
            _angka_csv(oe["fee_entry_total"]),
            "0.00",
            "0.00",
            "Beli sesuai rencana eksekusi",
        ]
    ]

    sl_harga = float(oe["harga_sl_eksekusi"])
    sl_net = float(scn["sl_kena"]["net_pnl"])
    baris.append(
        [
            "SL_INITIAL",
            "Stop Loss Awal",
            _harga(sl_harga),
            _unit(total),
            "100.00",
            _angka_csv(total * sl_harga),
            _angka_csv(total * sl_harga * f_jual),
            f"{-abs(sl_net):.2f}",
            "-1.00",
            f"Rugi bersih termasuk fee (SL {rp['sl_percent']:.2f}%)",
        ]
    )

    for tranche in rencana["take_profit_tranches"]:
        harga = float(tranche["harga"])
        unit = float(tranche["unit"])
        net = float(tranche["estimasi_net_pnl"])
        baris.append(
            [
                str(tranche["label"]),
                f"Take Profit {float(tranche['r_multiple']):g}R",
                _harga(harga),
                _unit(unit),
                f"{float(tranche['alokasi_pct_aktual']):.2f}",
                _angka_csv(unit * harga),
                _angka_csv(unit * harga * f_jual),
                f"{net:+.2f}",
                f"+{float(tranche['r_multiple']):.2f}",
                "Tranche aktif" if tranche["aktif"] else "Tranche tanpa unit",
            ]
        )

    tp1 = scn["tp1_lalu_bep"]
    if oe["auto_bep_aktif"] and tp1["net_pnl"] is not None:
        unit_sisa = total - float(tp1["unit_di_tp1"])
        bep = float(tp1["harga_bep"])
        baris.append(
            [
                "AUTO_BEP",
                "SL ke BEP setelah TP1",
                _harga(bep),
                _unit(unit_sisa),
                "0.00",
                _angka_csv(unit_sisa * bep),
                _angka_csv(unit_sisa * bep * f_jual),
                f"{float(tp1['net_pnl']):+.2f}",
                "0.00",
                "Sisa posisi ditutup impas setelah TP1",
            ]
        )

    for aturan in rencana["progressive_trailing_rules"]:
        lock = float(aturan["sl_lock_harga"])
        unit_trail = float(aturan["unit_runner"])
        alokasi_trail = (unit_trail / total * 100.0) if total > 0 else 0.0
        net = float(aturan["estimasi_net_pnl"])
        baris.append(
            [
                f"TRAIL_{int(aturan['stage'])}",
                f"Trailing Stage {int(aturan['stage'])}",
                _harga(lock),
                _unit(unit_trail),
                f"{alokasi_trail:.2f}",
                _angka_csv(unit_trail * lock),
                _angka_csv(unit_trail * lock * f_jual),
                f"{net:+.2f}",
                f"+{float(aturan['sl_lock_r']):.2f}",
                str(aturan["catatan"]),
            ]
        )
    return baris


def _slug(teks: str) -> str:
    bersih = re.sub(r"[^A-Za-z0-9._-]+", "_", str(teks)).strip("_")
    return bersih or "rencana"


def _stempel(waktu: str) -> str:
    cocok = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", str(waktu or ""))
    if cocok:
        return "".join(cocok.groups()[:3]) + "_" + "".join(cocok.groups()[3:])
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def ekspor_rencana_order(
    rencana: dict[str, Any],
    output_dir: str | Path = ".",
    formats: str = "both",
    nama_dasar: str | None = None,
) -> dict[str, str]:
    """Tulis rencana ke JSON dan/atau CSV. ``output_dir`` bisa diarahkan ke tmp_path."""
    mode = str(formats or "both").strip().lower()
    if mode not in ("none", "json", "csv", "both"):
        raise OrderPlanValidationError(
            f"formats '{formats}' tidak dikenal; pilih none|json|csv|both."
        )
    if not isinstance(rencana, dict) or "metadata" not in rencana or "order_execution" not in rencana:
        raise OrderPlanValidationError("Payload rencana tidak lengkap untuk diekspor.")
    if mode == "none":
        return {}

    folder = Path(output_dir)
    folder.mkdir(parents=True, exist_ok=True)
    md = rencana["metadata"]
    dasar = nama_dasar or f"order_plan_{_slug(md.get('symbol') or md['market'])}_{_stempel(md.get('waktu_rencana', ''))}"
    hasil: dict[str, str] = {}

    if mode in ("json", "both"):
        jalur = folder / f"{dasar}.json"
        jalur.write_text(
            json.dumps(rencana, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        hasil["json"] = str(jalur)
    if mode in ("csv", "both"):
        jalur = folder / f"{dasar}.csv"
        with jalur.open("w", encoding="utf-8", newline="") as fh:
            penulis = csv.writer(fh)
            penulis.writerow(HEADER_CSV)
            penulis.writerows(_baris_csv(rencana))
        hasil["csv"] = str(jalur)
    return hasil


def cetak_rencana(rencana: dict[str, Any], stream: Any = None) -> None:
    """Cetak ringkasan rencana yang ramah dibaca (Bahasa Indonesia)."""
    out = stream if stream is not None else sys.stdout
    md = rencana["metadata"]
    rp = rencana["risk_parameters"]
    oe = rencana["order_execution"]
    scn = rencana["scenario_matrix"]
    is_bei = md["market"] == "BEI"
    step = Decimal(str(md["step_size"]))

    def h(nilai: float) -> str:
        return format_harga(nilai, is_bei)

    def u(nilai: float) -> str:
        return format_unit(nilai, step, is_bei)

    def baris(teks: str = "") -> None:
        print(teks, file=out)

    baris("=" * 74)
    baris(f"RENCANA ORDER - {md['market_label']}")
    baris(f"Simbol        : {md['symbol']}    Arah: {md['arah_posisi']}    "
          f"Leverage: {md['leverage']:g}x")
    baris(f"Waktu rencana : {md['waktu_rencana']}    Skema: v{md['versi_skema']}")
    baris("-" * 74)
    baris(f"Modal         : {format_uang(md['modal'], is_bei)}")
    baris(f"Entry / SL    : {h(oe['harga_entry_eksekusi'])} / {h(oe['harga_sl_eksekusi'])}"
          f"   (SL {rp['sl_percent']:.2f}%)")
    if is_bei:
        baris(f"ARA / ARB     : {h(rp['harga_ara'])} / {h(rp['harga_arb'])}"
              f"   (auto reject {rp['auto_reject_pct'] * 100:.0f}%)")
    else:
        baris(f"Likuidasi     : {h(rp['harga_likuidasi'])} "
              f"(jarak {rp['jarak_likuidasi_pct']:.2f}%, batas aman "
              f"{rp['leverage_maks_aman']:.2f}x)")
    baris(f"Risiko        : rencana {format_uang(rp['risiko_rencana'], is_bei)} | aktual "
          f"{format_uang(rp['risiko_aktual'], is_bei)} ({rp['risiko_aktual_pct_modal']:.3f}% modal)")
    baris("-" * 74)
    baris(f"Kuantitas     : {u(oe['jumlah_unit_total'])}")
    baris(f"Notional      : {format_uang(oe['notional_aktual'], is_bei)} | fee masuk "
          f"{format_uang(oe['fee_entry_total'], is_bei)}")
    baris(f"Margin        : {format_uang(oe['margin_terpakai'], is_bei)} | total kas "
          f"{format_uang(oe['total_kas_terpakai'], is_bei)} | sisa "
          f"{format_uang(oe['sisa_modal'], is_bei)}")
    baris(f"Daya beli max : {format_uang(oe['max_buying_power'], is_bei)}"
          + ("  (kuantitas dibatasi daya beli)" if oe["qty_dibatasi_daya_beli"] else ""))
    baris("-" * 74)
    baris("Tranche TP:")
    for tranche in rencana["take_profit_tranches"]:
        aktif = "" if tranche["aktif"] else "  [TANPA UNIT]"
        baris(f"  {tranche['label']}  {float(tranche['r_multiple']):+.2f}R  "
              f"harga {h(tranche['harga'])}  unit {u(tranche['unit'])}  "
              f"alokasi {float(tranche['alokasi_pct_aktual']):.2f}%  "
              f"net {format_uang(tranche['estimasi_net_pnl'], is_bei)}{aktif}")
    baris("-" * 74)
    baris("Trailing progresif:")
    for aturan in rencana["progressive_trailing_rules"]:
        aktif = "" if aturan["aktif"] else "  [TANPA UNIT RUNNER]"
        baris(f"  Stage {int(aturan['stage'])}: "
              f"trigger {float(aturan['trigger_r']):.2f}R ({h(aturan['trigger_harga'])}), "
              f"lock {float(aturan['sl_lock_r']):.2f}R ({h(aturan['sl_lock_harga'])}), "
              f"unit {u(aturan['unit_runner'])}, "
              f"net {format_uang(aturan['estimasi_net_pnl'], is_bei)}{aktif}")
    baris("-" * 74)
    baris("Skenario:")
    baris(f"  SL awal kena      : {format_uang(scn['sl_kena']['net_pnl'], is_bei)} "
          f"({float(scn['sl_kena']['net_pnl_pct_modal']):+.3f}% modal)")
    tp1 = scn["tp1_lalu_bep"]
    if tp1["aktif"]:
        baris(f"  TP1 lalu BEP      : {format_uang(tp1['net_pnl'], is_bei)} "
              f"(BEP {h(tp1['harga_bep'])})")
    else:
        baris("  TP1 lalu BEP      : TIDAK AKTIF (TP1 tanpa unit)")
    tp12 = scn["tp1_tp2_lalu_trail"]
    if tp12["aktif"]:
        baris(f"  TP1+TP2 lalu trail: {format_uang(tp12['net_pnl'], is_bei)} "
              f"(lock {h(tp12['harga_trailing'])})")
    else:
        baris("  TP1+TP2 lalu trail: TIDAK AKTIF (TP2 tanpa unit)")
    baris(f"  Semua TP          : {format_uang(scn['semua_tp']['net_pnl'], is_bei)} "
          f"({float(scn['semua_tp']['net_pnl_pct_modal']):+.3f}% modal)")
    baris("-" * 74)
    peringatan = rencana.get("warnings") or []
    if peringatan:
        baris("Peringatan:")
        for pesan in peringatan:
            baris(f"  - {pesan}")
    else:
        baris("Peringatan: tidak ada.")
    baris("=" * 74)


def _parse_angka_manual(teks: str) -> float:
    """Terima '10000000', '10.000.000', '10.000.000,50', atau '0.00002'."""
    bersih = str(teks).strip().replace(" ", "")
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", bersih):
        bersih = bersih.replace(".", "").replace(",", ".")
    else:
        bersih = bersih.replace(",", ".")
    return float(bersih)


def _tanya_teks(prompt: str, default: str | None = None, pilihan: Sequence[str] | None = None) -> str:
    label = f"{prompt} [{default}]" if default is not None else prompt
    opsi = f" ({'/'.join(pilihan)})" if pilihan else ""
    while True:
        try:
            mentah = input(f"{label}{opsi}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nInput dibatalkan.", file=sys.stderr)
            raise SystemExit(2) from None
        if not mentah and default is not None:
            mentah = str(default)
        if pilihan:
            mentah = mentah.upper()
            if mentah not in pilihan:
                print(f"  Pilihan tidak dikenal: {mentah!r}. Gunakan salah satu: {', '.join(pilihan)}.")
                continue
        return mentah


def _tanya_angka(
    prompt: str,
    default: str | None = None,
    minimum: float | None = None,
    maksimum: float | None = None,
) -> float:
    while True:
        mentah = _tanya_teks(prompt, default)
        try:
            nilai = _parse_angka_manual(mentah)
        except ValueError:
            print(f"  '{mentah}' bukan angka. Contoh: 1000 atau 10.000.000 atau 0.00002.")
            continue
        if not math.isfinite(nilai):
            print("  Angka harus berhingga (bukan NaN/inf).")
            continue
        if minimum is not None and nilai < minimum:
            print(f"  Nilai harus >= {minimum:g}.")
            continue
        if maksimum is not None and nilai > maksimum:
            print(f"  Nilai harus <= {maksimum:g}.")
            continue
        return nilai


def _mode_interaktif() -> int:
    """Mode prompt interaktif dengan validasi ulang yang ramah."""
    print("=" * 74)
    print("PERENCANA ORDER RULE-BASED - Saham BEI dan Kripto Futures")
    print("Tekan Enter untuk memakai nilai default di dalam kurung siku.")
    print("=" * 74)
    market = _tanya_teks("Pasar", "BEI", ("BEI", "FUTURES"))
    is_bei = market == "BEI"
    modal = _tanya_angka(
        "Modal" + (" (rupiah)" if is_bei else " (USD)"), None, minimum=1e-9
    )
    entry = _tanya_angka("Harga entry", None, minimum=1e-12)
    sl = _tanya_angka("Harga SL", None, minimum=1e-12)
    risk = _tanya_angka("Risiko per trade (% modal)", "1", minimum=1e-9, maksimum=MAX_RISK_PERCENT)

    arah = "LONG"
    leverage = 1.0
    prev = 0.0
    if is_bei:
        prev = _tanya_angka("Harga penutupan sebelumnya (prev_close)", None, minimum=1e-12)
        step = str(int(_tanya_angka("Step size (lembar)", "100", minimum=1)))
    else:
        arah = _tanya_teks("Arah posisi", "LONG", ("LONG", "SHORT"))
        leverage = _tanya_angka("Leverage", "1", minimum=1)
        step = _tanya_teks("Step size", MARKET_RULES["FUTURES"]["step_size_default"])
    symbol = _tanya_teks("Simbol (opsional)", "")
    mode_ekspor = _tanya_teks("Ekspor", "both", ("NONE", "JSON", "CSV", "BOTH")).lower()

    try:
        rencana = build_order_plan(
            OrderPlanInput(
                market_key=market,  # type: ignore[arg-type]
                modal=modal,
                harga_entry=entry,
                harga_sl=sl,
                risk_percent=risk,
                arah_posisi=arah,  # type: ignore[arg-type]
                leverage=leverage,
                step_size=step,
                prev_close=prev,
                symbol=symbol,
            )
        )
    except OrderPlanValidationError as exc:
        print(f"\nVALIDASI GAGAL: {exc}", file=sys.stderr)
        return 2

    print()
    cetak_rencana(rencana)
    try:
        hasil = ekspor_rencana_order(rencana, output_dir=".", formats=mode_ekspor)
    except OSError as exc:
        print(f"GAGAL MENULIS BERKAS: {exc}", file=sys.stderr)
        return 2
    for jenis, jalur in hasil.items():
        print(f"Ekspor {jenis.upper()}: {jalur}")
    return 0


def _mode_argparse(daftar: list[str]) -> int:
    """Mode non-interaktif/otomasi. Mengembalikan exit code (0 sukses, 2 validasi gagal)."""
    parser = argparse.ArgumentParser(
        prog="order_planner.py",
        description=(
            "Perencana order rule-based multi-pasar (Saham BEI + Kripto Futures). "
            "Tanpa argumen = mode interaktif."
        ),
    )
    parser.add_argument("--market", type=str.upper, choices=("BEI", "FUTURES"), default="BEI")
    parser.add_argument("--modal", type=float, required=True, help="modal (rupiah BEI / USD Futures)")
    parser.add_argument("--entry", type=float, required=True, help="harga entry")
    parser.add_argument("--sl", type=float, required=True, help="harga stop loss")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="risiko %% modal (0-5, default 1)")
    parser.add_argument("--side", type=str.upper, choices=("LONG", "SHORT"), default="LONG")
    parser.add_argument("--leverage", type=float, default=1.0, help="leverage Futures (default 1)")
    parser.add_argument("--step-size", default=None, help="step kuantitas (default per pasar)")
    parser.add_argument("--prev-close", type=float, default=0.0, help="wajib untuk BEI (ARA/ARB)")
    parser.add_argument("--symbol", default="", help="simbol untuk metadata dan nama berkas")
    parser.add_argument("--export", type=str.lower, choices=("none", "json", "csv", "both"), default="both")
    parser.add_argument("--output-dir", default=".", help="folder ekspor (default direktori aktif)")
    parser.add_argument(
        "--json-stdout",
        action="store_true",
        help="cetak payload JSON ke stdout (cocok untuk pipeline), tanpa laporan manusia",
    )
    args = parser.parse_args(daftar)

    market = str(args.market).upper()
    step = args.step_size or MARKET_RULES[market]["step_size_default"]
    inp = OrderPlanInput(
        market_key=market,  # type: ignore[arg-type]
        modal=args.modal,
        harga_entry=args.entry,
        harga_sl=args.sl,
        risk_percent=args.risk_pct,
        arah_posisi=args.side,  # type: ignore[arg-type]
        leverage=args.leverage,
        step_size=str(step),
        prev_close=args.prev_close,
        symbol=args.symbol,
    )
    try:
        rencana = build_order_plan(inp)
    except OrderPlanValidationError as exc:
        print(f"VALIDASI GAGAL: {exc}", file=sys.stderr)
        return 2

    if args.json_stdout:
        print(json.dumps(rencana, ensure_ascii=False, indent=2))
    else:
        cetak_rencana(rencana)

    try:
        hasil = ekspor_rencana_order(
            rencana, output_dir=args.output_dir, formats=args.export
        )
    except (OrderPlanValidationError, OSError) as exc:
        print(f"GAGAL EKSPOR: {exc}", file=sys.stderr)
        return 2
    tujuan = sys.stderr if args.json_stdout else sys.stdout
    for jenis, jalur in hasil.items():
        print(f"Ekspor {jenis.upper()}: {jalur}", file=tujuan)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entrypoint CLI. Tanpa argumen menjalankan mode interaktif."""
    argumen = list(sys.argv[1:] if argv is None else argv)
    if not argumen:
        return _mode_interaktif()
    return _mode_argparse(argumen)


if __name__ == "__main__":
    raise SystemExit(main())