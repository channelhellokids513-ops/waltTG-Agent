"""Test perencana order multi-pasar (`tools/order_planner.py`).

Yang dikunci di sini:
  1. Batas daya beli inklusif fee masuk tidak pernah jebol (BEI maupun Futures).
  2. Risiko bersih saat SL (termasuk fee dua sisi) tidak melewati `risk_percent` modal.
  3. Harga mikro tidak pernah dipaksa ke lantai 0,0001 dan tidak menjadi nol.
  4. AUTO_BEP tidak pernah aktif/ditulis saat TP1 tidak punya unit.
  5. Trailing stop memakai ratchet: SL tidak pernah melonggar.
  6. Validasi buruk gagal sebagai `OrderPlanValidationError`, bukan `SystemExit`.
  7. Ekspor JSON/CSV aman, 10 kolom konsisten, dan CLI non-interaktif mengembalikan 0.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

import pytest

from tools import order_planner as op

WAKTU = "2026-11-27T00:00:00Z"


def _rencana(**kwargs):
    return op.build_order_plan(op.OrderPlanInput(**kwargs), waktu=WAKTU)


# ---------------------------------------------------------------------------
# 1. BEI normal
# ---------------------------------------------------------------------------


def test_bei_normal_fraksi_tick_batas_ara_arb_dan_tiga_tranche():
    p = _rencana(
        market_key="BEI",
        modal=10_000_000,
        harga_entry=1000,
        harga_sl=950,
        prev_close=1000,
        symbol="BBCA",
    )
    md, rp, oe = p["metadata"], p["risk_parameters"], p["order_execution"]

    assert md["market"] == "BEI"
    assert md["arah_posisi"] == "LONG"
    assert md["leverage"] == 1.0
    # Harga sudah bulat pada fraksi tick BEI (1000 ada di tier tick 5).
    assert op.get_tick_bei(1000) == 5
    assert oe["harga_entry_eksekusi"] == 1000
    assert oe["harga_sl_eksekusi"] == 950
    assert rp["harga_ara"] == 1250   # prev_close 1000 + 25%, dibulatkan ke bawah
    assert rp["harga_arb"] == 750    # prev_close 1000 - 25%, dibulatkan ke atas

    tranches = p["take_profit_tranches"]
    assert [t["alokasi_pct_rencana"] for t in tranches] == [50.0, 30.0, 20.0]
    assert [t["harga"] for t in tranches] == [1075.0, 1150.0, 1250.0]
    assert sum(t["unit"] for t in tranches) == oe["jumlah_unit_total"]
    # 18 lot = 1800 lembar; TP1 tepat 50%, sisa dibagi sesuai kelipatan 1 lot.
    assert oe["jumlah_unit_total"] == 1800
    assert tranches[0]["unit"] == 900
    assert tranches[0]["unit"] / oe["jumlah_unit_total"] == pytest.approx(0.5)

    # Rugi bersih saat SL (termasuk fee dua sisi) <= 1% modal.
    assert rp["risiko_aktual"] <= 0.01 * 10_000_000 + 1e-6
    assert rp["risiko_aktual"] == pytest.approx(96_975.0)
    assert p["scenario_matrix"]["sl_kena"]["net_pnl"] == pytest.approx(-96_975.0)
    assert oe["notional_aktual"] + oe["fee_entry_total"] <= 10_000_000


# ---------------------------------------------------------------------------
# 2. BEI tepat di plafon daya beli
# ---------------------------------------------------------------------------


def test_bei_batas_daya_beli_inklusif_fee_tidak_overflow():
    modal = 10_000_000
    p = _rencana(
        market_key="BEI",
        modal=modal,
        harga_entry=1000,
        harga_sl=995,
        prev_close=1000,
    )
    oe = p["order_execution"]
    f_beli = op.MARKET_RULES["BEI"]["fee_beli"]

    # Formula lama (max_buying_power = modal) akan memilih 100 lot dan jebol:
    overflow_lama = 100 * 100 * 1000 * (1.0 + f_beli)
    assert overflow_lama > modal

    assert oe["max_buying_power"] == pytest.approx(modal / (1.0 + f_beli))
    assert oe["jumlah_unit_total"] == 9900  # 99 lot, bukan 100 lot
    assert oe["notional_aktual"] == pytest.approx(9_900_000)
    assert oe["notional_aktual"] + oe["fee_entry_total"] <= modal
    assert oe["total_kas_terpakai"] <= modal
    assert oe["sisa_modal"] >= 0


# ---------------------------------------------------------------------------
# 3. BEI penolakan & peringatan
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, potongan_pesan",
    [
        (dict(prev_close=40), "minimum BEI"),
        (dict(harga_entry=1300, harga_sl=1250), "ARA"),
        (dict(harga_entry=700, harga_sl=650), "ARB"),
        (dict(harga_sl=1000), "di bawah harga entry"),
        (dict(modal=50_000), "1 step"),
        (dict(prev_close=0), "prev_close"),
    ],
)
def test_bei_menolak_input_tidak_sah(kwargs, potongan_pesan):
    dasar = dict(
        market_key="BEI",
        modal=10_000_000,
        harga_entry=1000,
        harga_sl=950,
        prev_close=1000,
    )
    dasar.update(kwargs)
    with pytest.raises(op.OrderPlanValidationError) as exc:
        _rencana(**dasar)
    assert potongan_pesan in str(exc.value)


def test_bei_peringatan_sl_di_bawah_arb_dan_tp_di_atas_ara():
    at_arb = _rencana(
        market_key="BEI", modal=10_000_000, harga_entry=750, harga_sl=700, prev_close=1000
    )
    assert any("ARB" in w for w in at_arb["warnings"])
    assert at_arb["risk_parameters"]["harga_arb"] == 750

    dekat_ara = _rencana(
        market_key="BEI", modal=10_000_000, harga_entry=1240, harga_sl=1200, prev_close=1000
    )
    assert any("ARA" in w for w in dekat_ara["warnings"])
    ara = dekat_ara["risk_parameters"]["harga_ara"]
    assert ara == 1250
    assert [t["harga"] for t in dekat_ara["take_profit_tranches"]] == [1300.0, 1360.0, 1440.0]
    assert all(t["di_atas_ara"] for t in dekat_ara["take_profit_tranches"])
    assert all(t["harga_di_clamp"] is False for t in dekat_ara["take_profit_tranches"])


# ---------------------------------------------------------------------------
# 4. FUTURES LONG normal & capped
# ---------------------------------------------------------------------------


def test_futures_long_normal_dan_capped_daya_beli():
    normal = _rencana(
        market_key="FUTURES",
        modal=100,
        harga_entry=100,
        harga_sl=98,
        leverage=5,
        step_size="0.001",
    )
    oe = normal["order_execution"]
    assert oe["margin_terpakai"] + oe["fee_entry_total"] <= 100
    assert oe["margin_terpakai"] == pytest.approx(oe["notional_aktual"] / 5)
    assert oe["qty_dibatasi_daya_beli"] is False
    assert all(t["harga"] > 100 for t in normal["take_profit_tranches"])

    # SL sangat rapat + leverage tinggi: kuantitas HARUS dibatasi daya beli, bukan risiko.
    capped = _rencana(
        market_key="FUTURES",
        modal=100,
        harga_entry=100,
        harga_sl=99.99,
        leverage=10,
        risk_percent=5.0,
        step_size="0.001",
    )
    oe2 = capped["order_execution"]
    max_bp = 100 / ((1.0 / 10.0) + op.MARKET_RULES["FUTURES"]["fee_beli"])
    assert oe2["max_buying_power"] == pytest.approx(max_bp)
    assert oe2["qty_dibatasi_daya_beli"] is True
    assert oe2["jumlah_unit_total"] == pytest.approx(9.91)
    assert oe2["margin_terpakai"] + oe2["fee_entry_total"] <= 100
    assert oe2["margin_terpakai"] + oe2["fee_entry_total"] == pytest.approx(99.99, abs=0.02)


# ---------------------------------------------------------------------------
# 5. FUTURES SHORT normal
# ---------------------------------------------------------------------------


def test_futures_short_normal_harga_bep_dan_net_pnl_positif():
    p = _rencana(
        market_key="FUTURES",
        modal=1000,
        harga_entry=100,
        harga_sl=102,
        arah_posisi="SHORT",
        leverage=5,
        step_size="0.001",
    )
    oe, scn = p["order_execution"], p["scenario_matrix"]
    tranches = p["take_profit_tranches"]

    assert [t["harga"] for t in tranches] == [97.0, 94.0, 90.0]
    assert all(t["harga"] < 100 for t in tranches)
    assert scn["tp1_lalu_bep"]["harga_bep"] < 100
    assert p["risk_parameters"]["harga_likuidasi"] > 102
    assert scn["tp1_lalu_bep"]["net_pnl"] > 0
    assert scn["tp1_tp2_lalu_trail"]["net_pnl"] > 0
    assert scn["semua_tp"]["net_pnl"] > 0
    assert scn["sl_kena"]["net_pnl"] < 0
    assert all(t["estimasi_net_pnl"] > 0 for t in tranches if t["unit"] > 0)
    assert oe["margin_terpakai"] + oe["fee_entry_total"] <= 1000


# ---------------------------------------------------------------------------
# 6. FUTURES: risiko likuidasi
# ---------------------------------------------------------------------------


def test_futures_menolak_leverage_di_atas_batas_aman():
    with pytest.raises(op.OrderPlanValidationError) as exc:
        _rencana(
            market_key="FUTURES",
            modal=100,
            harga_entry=100,
            harga_sl=96,
            leverage=15,
            step_size="0.001",
        )
    assert "maksimum aman" in str(exc.value)

    # Dengan MMR 0,5% dan SL 4%, batas aman = 1/(0.04*2 + 0.005) = 11.7647x -> 10x lolos.
    aman = _rencana(
        market_key="FUTURES",
        modal=100,
        harga_entry=100,
        harga_sl=96,
        leverage=10,
        step_size="0.001",
    )
    rp = aman["risk_parameters"]
    assert rp["leverage_maks_aman"] == pytest.approx(11.7647, rel=1e-4)
    assert rp["harga_likuidasi"] == pytest.approx(90.5)
    assert rp["harga_likuidasi"] < 96
    assert (100 - rp["harga_likuidasi"]) / 100 >= 2 * rp["sl_pct"] - 1e-9


def test_futures_guard_likuidasi_tetap_menolak_saat_safety_dilonggarkan(monkeypatch):
    # Pertahanan berlapis: bila faktor keamanan dilonggarkan, guard likuidasi tetap menolak.
    monkeypatch.setattr(op, "LIQ_SAFETY_MULT", 0.1)
    with pytest.raises(op.OrderPlanValidationError) as exc:
        _rencana(
            market_key="FUTURES",
            modal=100,
            harga_entry=100,
            harga_sl=97,
            leverage=50,
            step_size="0.001",
        )
    assert "likuidasi" in str(exc.value)


# ---------------------------------------------------------------------------
# 7. FUTURES harga mikro
# ---------------------------------------------------------------------------


def test_futures_harga_mikro_tidak_dipaksa_ke_lantai_00001():
    p = _rencana(
        market_key="FUTURES",
        modal=100,
        harga_entry=0.00002,
        harga_sl=0.000021,
        arah_posisi="SHORT",
        leverage=1,
        step_size="1",
    )
    tranches = p["take_profit_tranches"]
    harga = [t["harga"] for t in tranches]

    assert harga == pytest.approx([0.0000185, 0.000017, 0.000015], rel=1e-9)
    assert all(0 < h < 0.00002 for h in harga)
    assert all(abs(h - 0.0001) > 1e-9 for h in harga), "target tidak boleh dipaksa ke 0,0001"
    assert p["scenario_matrix"]["tp1_lalu_bep"]["harga_bep"] < 0.00002
    assert p["progressive_trailing_rules"][0]["sl_lock_harga"] == pytest.approx(0.0000185)
    assert op.round_price_adaptive(0.00001854) == pytest.approx(0.00001854)


# ---------------------------------------------------------------------------
# 8. Tranche kecil -> AUTO_BEP dimatikan
# ---------------------------------------------------------------------------


def test_tranche_satu_step_mematikan_auto_bep_dan_tidak_menulis_baris_fiktif(tmp_path):
    satu = _rencana(
        market_key="BEI",
        modal=600_000,
        harga_entry=1000,
        harga_sl=950,
        prev_close=1000,
        symbol="TEST1",
    )
    oe = satu["order_execution"]
    assert oe["jumlah_unit_total"] == 100
    assert [t["unit"] for t in satu["take_profit_tranches"]] == [0.0, 100.0, 0.0]
    assert oe["auto_bep_aktif"] is False
    assert satu["scenario_matrix"]["tp1_lalu_bep"]["aktif"] is False
    assert satu["scenario_matrix"]["tp1_lalu_bep"]["net_pnl"] is None
    assert any("AUTO_BEP" in w for w in satu["warnings"])

    jalur = op.ekspor_rencana_order(satu, output_dir=tmp_path)
    baris = list(csv.reader(Path(jalur["csv"]).read_text(encoding="utf-8").splitlines()))
    assert not [r for r in baris if r[0] == "AUTO_BEP"], "AUTO_BEP fiktif tidak boleh ditulis"

    dua = _rencana(
        market_key="BEI",
        modal=1_200_000,
        harga_entry=1000,
        harga_sl=950,
        prev_close=1000,
    )
    assert dua["order_execution"]["jumlah_unit_total"] == 200
    assert [t["unit"] for t in dua["take_profit_tranches"]] == [100.0, 100.0, 0.0]
    assert dua["order_execution"]["auto_bep_aktif"] is True
    assert dua["scenario_matrix"]["tp1_tp2_lalu_trail"]["aktif"] is False

    tiga = _rencana(
        market_key="BEI",
        modal=1_800_000,
        harga_entry=1000,
        harga_sl=950,
        prev_close=1000,
    )
    assert tiga["order_execution"]["jumlah_unit_total"] == 300
    assert [t["unit"] for t in tiga["take_profit_tranches"]] == [100.0, 100.0, 100.0]
    assert tiga["order_execution"]["auto_bep_aktif"] is True
    assert tiga["scenario_matrix"]["tp1_tp2_lalu_trail"]["aktif"] is True


# ---------------------------------------------------------------------------
# 9. Trailing stop progresif + ratchet
# ---------------------------------------------------------------------------


def test_trailing_stop_long_progresif_dan_ratchet():
    belum = op.evaluasi_trailing_stop_live(100, 90, 129, 90, "LONG")
    assert belum["status"] == "BELUM_AKTIF"
    assert belum["sl_baru"] == 90

    stage1 = op.evaluasi_trailing_stop_live(100, 90, 130, 90, "LONG")
    assert stage1["status"] == "AKTIF" and stage1["stage"] == 1
    assert stage1["sl_baru"] == pytest.approx(115.0)

    stage2 = op.evaluasi_trailing_stop_live(100, 90, 137.5, 90, "LONG")
    assert stage2["stage"] == 2
    assert stage2["sl_baru"] == pytest.approx(127.5)
    assert stage2["sl_baru"] > stage1["sl_baru"]

    stage3 = op.evaluasi_trailing_stop_live(100, 90, 145, 90, "LONG")
    assert stage3["stage"] == 3
    assert stage3["sl_baru"] == pytest.approx(140.0)
    assert stage3["sl_baru"] > stage2["sl_baru"]

    # Ratchet: kandidat 116 lebih rendah dari SL saat ini 140 -> SL tidak boleh turun.
    ditahan = op.evaluasi_trailing_stop_live(100, 90, 131, 140, "LONG")
    assert ditahan["sl_kandidat"] == pytest.approx(116.0)
    assert ditahan["sl_baru"] == 140
    assert ditahan["berubah"] is False


def test_trailing_stop_short_progresif_dan_ratchet():
    stage1 = op.evaluasi_trailing_stop_live(100, 110, 70, 110, "SHORT")
    assert stage1["status"] == "AKTIF" and stage1["stage"] == 1
    assert stage1["sl_baru"] == pytest.approx(85.0)

    stage2 = op.evaluasi_trailing_stop_live(100, 110, 62.5, 85, "SHORT")
    assert stage2["stage"] == 2
    assert stage2["sl_baru"] == pytest.approx(72.5)
    assert stage2["sl_baru"] < stage1["sl_baru"]

    stage3 = op.evaluasi_trailing_stop_live(100, 110, 55, 72.5, "SHORT")
    assert stage3["stage"] == 3
    assert stage3["sl_baru"] == pytest.approx(60.0)

    # Ratchet: kandidat 85 lebih tinggi dari SL saat ini 80 -> SL tidak boleh naik.
    ditahan = op.evaluasi_trailing_stop_live(100, 110, 70, 80, "SHORT")
    assert ditahan["sl_kandidat"] == pytest.approx(85.0)
    assert ditahan["sl_baru"] == 80


def test_trailing_stop_guard_input_tidak_valid():
    nol = op.evaluasi_trailing_stop_live(100, 90, 130, 0, "LONG")
    assert nol["status"] == "ERROR" and nol["sl_baru"] == 0

    entry_nol = op.evaluasi_trailing_stop_live(0, 90, 130, 90, "LONG")
    assert entry_nol["status"] == "ERROR"

    arah_salah = op.evaluasi_trailing_stop_live(100, 90, 130, 90, "SIDEWAYS")
    assert arah_salah["status"] == "ERROR"

    sl_nol = op.evaluasi_trailing_stop_live(100, 0, 130, 90, "LONG")
    assert sl_nol["status"] == "ERROR"

    puncak_nol = op.evaluasi_trailing_stop_live(100, 90, 0, 90, "LONG")
    assert puncak_nol["status"] == "ERROR"


# ---------------------------------------------------------------------------
# 10. Validasi input buruk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, potongan_pesan",
    [
        (dict(market_key="BEI", modal=0, harga_entry=1000, harga_sl=950, prev_close=1000), "modal"),
        (
            dict(market_key="BEI", modal=1e6, harga_entry=1000, harga_sl=950, prev_close=1000,
                 step_size="0"),
            "step_size",
        ),
        (
            dict(market_key="BEI", modal=1e6, harga_entry=1000, harga_sl=950, prev_close=1000,
                 step_size="abc"),
            "step_size",
        ),
        (
            dict(market_key="BEI", modal=1e6, harga_entry=1000, harga_sl=950, prev_close=1000,
                 risk_percent=0),
            "risk_percent",
        ),
        (
            dict(market_key="BEI", modal=1e6, harga_entry=1000, harga_sl=950, prev_close=1000,
                 risk_percent=6),
            "risk_percent",
        ),
        (dict(market_key="BEI", modal=1e6, harga_entry=1000, harga_sl=890, prev_close=1000),
         "melampaui batas"),
        (dict(market_key="FUTURES", modal=100, harga_entry=100, harga_sl=84,
              step_size="0.001"), "melampaui batas"),
        (dict(market_key="BEI", modal=1e6, harga_entry=0, harga_sl=950, prev_close=1000),
         "harga entry"),
        (dict(market_key="BEI", modal=1e6, harga_entry=1000, harga_sl=950, prev_close=1000,
              arah_posisi="SHORT"), "SHORT"),
        (dict(market_key="FUTURES", modal=100, harga_entry=100, harga_sl=98, leverage=0.5,
              step_size="0.001"), "leverage"),
        (dict(market_key="BEI", modal=1e6, harga_entry=1000, harga_sl=950, prev_close=1000,
              step_size="0.5"), "bilangan bulat"),
    ],
)
def test_validasi_input_buruk_melempar_exception_bukan_exit(kwargs, potongan_pesan):
    with pytest.raises(op.OrderPlanValidationError) as exc:
        _rencana(**kwargs)
    assert potongan_pesan in str(exc.value)
    assert isinstance(exc.value, ValueError)


# ---------------------------------------------------------------------------
# 11. Ekspor JSON & CSV
# ---------------------------------------------------------------------------


def test_ekspor_json_csv_lengkap_dan_tanda_negatif_sl(tmp_path):
    p = _rencana(
        market_key="BEI",
        modal=10_000_000,
        harga_entry=1000,
        harga_sl=950,
        prev_close=1000,
        symbol="BBCA",
    )
    jalur = op.ekspor_rencana_order(p, output_dir=tmp_path)
    assert set(jalur) == {"json", "csv"}
    assert Path(jalur["json"]).exists() and Path(jalur["csv"]).exists()
    assert Path(jalur["json"]).parent == tmp_path

    data = json.loads(Path(jalur["json"]).read_text(encoding="utf-8"))
    assert data["metadata"]["market"] == "BEI"
    assert data["metadata"]["symbol"] == "BBCA"
    assert data["order_execution"]["jumlah_unit_total"] == 1800

    baris = list(csv.reader(Path(jalur["csv"]).read_text(encoding="utf-8").splitlines()))
    assert baris[0] == op.HEADER_CSV
    assert len(op.HEADER_CSV) == 10
    assert all(len(r) == 10 for r in baris)

    sl_rows = [r for r in baris if r[0] == "SL_INITIAL"]
    assert len(sl_rows) == 1
    nilai = sl_rows[0][7]
    assert nilai.startswith("-")
    assert not nilai.startswith("--")
    assert float(nilai) == pytest.approx(-96_975.0)
    assert any(r[0] == "AUTO_BEP" for r in baris)

    hanya_json = op.ekspor_rencana_order(p, output_dir=tmp_path, formats="json", nama_dasar="uji")
    assert set(hanya_json) == {"json"}
    assert op.ekspor_rencana_order(p, output_dir=tmp_path, formats="none") == {}
    with pytest.raises(op.OrderPlanValidationError):
        op.ekspor_rencana_order(p, output_dir=tmp_path, formats="xlsx")


def test_payload_deterministik_dan_json_serializable():
    satu = _rencana(market_key="BEI", modal=10_000_000, harga_entry=1000, harga_sl=950, prev_close=1000)
    dua = _rencana(market_key="BEI", modal=10_000_000, harga_entry=1000, harga_sl=950, prev_close=1000)
    assert satu == dua
    assert json.loads(json.dumps(satu)) == satu


# ---------------------------------------------------------------------------
# 12. CLI non-interaktif
# ---------------------------------------------------------------------------


def test_cli_non_interaktif_json_stdout(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    kode = op.main(
        [
            "--market", "BEI",
            "--modal", "10000000",
            "--entry", "1000",
            "--sl", "950",
            "--prev-close", "1000",
            "--symbol", "BBCA",
            "--json-stdout",
            "--export", "none",
        ]
    )
    assert kode == 0
    payload = json.loads(buf.getvalue())
    assert payload["metadata"]["market"] == "BEI"
    assert payload["order_execution"]["jumlah_unit_total"] == 1800


def test_cli_non_interaktif_futures_dan_validasi_gagal(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    kode = op.main(
        [
            "--market", "FUTURES",
            "--modal", "1000",
            "--entry", "100",
            "--sl", "102",
            "--side", "SHORT",
            "--leverage", "5",
            "--step-size", "0.001",
            "--json-stdout",
            "--export", "none",
        ]
    )
    assert kode == 0
    payload = json.loads(buf.getvalue())
    assert payload["metadata"]["arah_posisi"] == "SHORT"

    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    kode_gagal = op.main(
        ["--market", "BEI", "--modal", "0", "--entry", "1000", "--sl", "950",
         "--prev-close", "1000", "--json-stdout", "--export", "none"]
    )
    assert kode_gagal == 2
    assert "VALIDASI GAGAL" in err.getvalue()


# ---------------------------------------------------------------------------
# 13. Fraksi tick BEI, presisi adaptif, dan format harga
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "harga, tick",
    [
        (50, 1), (199, 1), (199.99, 1),
        (200, 2), (499, 2), (499.99, 2),
        (500, 5), (1999, 5),
        (2000, 10), (4999, 10),
        (5000, 25), (9999, 25),
    ],
)
def test_get_tick_bei_pada_titik_batas(harga, tick):
    assert op.get_tick_bei(harga) == tick


@pytest.mark.parametrize(
    "harga", [199.4, 199.6, 299.5, 499.5, 501.0, 1999.5, 2001.0, 4999.5, 5001.0, 7777.0]
)
def test_bulatkan_fraksi_bei_selalu_menghasilkan_harga_valid(harga):
    hasil = op.bulatkan_fraksi_bei(harga)
    assert isinstance(hasil, int)
    assert hasil >= 50
    assert hasil != 499, "499 bukan fraksi sah pada tier 200-500 (wajib kelipatan 2)"
    assert hasil % op.get_tick_bei(hasil) == 0
    assert op.get_tick_bei(harga) == op.get_tick_bei(hasil) or abs(hasil - harga) <= 25


def test_bulatkan_fraksi_bei_menolak_harga_di_bawah_minimum_dan_nan():
    with pytest.raises(op.OrderPlanValidationError):
        op.bulatkan_fraksi_bei(49)
    with pytest.raises(op.OrderPlanValidationError):
        op.bulatkan_fraksi_bei(float("nan"))


def test_round_price_adaptive_dan_format_harga_dinamis():
    assert op.round_price_adaptive(1003, is_bei=True) == 1005
    assert isinstance(op.round_price_adaptive(1003, is_bei=True), int)
    assert op.round_price_adaptive(1.234567) == pytest.approx(1.2346)
    assert op.round_price_adaptive(0.1234567) == pytest.approx(0.123457)
    assert op.round_price_adaptive(0.00001854) == pytest.approx(0.00001854)

    assert op.format_harga(1000, is_bei=True) == "1.000"
    assert op.format_harga(1_000_000, is_bei=True) == "1.000.000"
    assert op.format_harga(0.0000185) == "0.0000185"
    assert op.format_harga(0.1234567) == "0.123457"
    assert op.format_harga(1.23456) == "1.2346"
    assert op.format_harga(100.0) == "100"


def test_cetak_rencana_tidak_melempar_dan_memuat_angka_kunci():
    p = _rencana(
        market_key="FUTURES",
        modal=1000,
        harga_entry=100,
        harga_sl=102,
        arah_posisi="SHORT",
        leverage=5,
        step_size="0.001",
    )
    buf = io.StringIO()
    op.cetak_rencana(p, buf)
    teks = buf.getvalue()
    assert "RENCANA ORDER" in teks
    assert "TP1" in teks and "Stage 3" in teks
    assert "99.8202" in teks  # harga BEP SHORT < entry, tampil dengan presisi 4 desimal


def test_bei_batas_persen_ara_arb_tepat_200_dan_5000():
    p200 = _rencana(
        market_key="BEI", modal=10_000_000, harga_entry=200, harga_sl=190, prev_close=200
    )
    assert p200["risk_parameters"]["auto_reject_pct"] == pytest.approx(0.35)
    assert p200["risk_parameters"]["harga_ara"] == 270
    assert p200["risk_parameters"]["harga_arb"] == 130

    p5000 = _rencana(
        market_key="BEI", modal=10_000_000, harga_entry=5000, harga_sl=4800, prev_close=5000
    )
    assert p5000["risk_parameters"]["auto_reject_pct"] == pytest.approx(0.25)
    assert p5000["risk_parameters"]["harga_ara"] == 6250
    assert p5000["risk_parameters"]["harga_arb"] == 3750

    p5025 = _rencana(
        market_key="BEI", modal=10_000_000, harga_entry=5025, harga_sl=4800, prev_close=5025
    )
    assert p5025["risk_parameters"]["auto_reject_pct"] == pytest.approx(0.20)
    assert p5025["risk_parameters"]["harga_ara"] == 6025
    assert p5025["risk_parameters"]["harga_arb"] == 4020


def test_trailing_rules_dan_csv_memakai_unit_runner_bukan_total(tmp_path):
    p = _rencana(
        market_key="BEI",
        modal=10_000_000,
        harga_entry=1000,
        harga_sl=950,
        prev_close=1000,
        symbol="BBCA",
    )
    unit_tp3 = p["take_profit_tranches"][2]["unit"]
    assert unit_tp3 == 400.0
    assert unit_tp3 < p["order_execution"]["jumlah_unit_total"]

    trail1 = p["progressive_trailing_rules"][0]
    assert trail1["unit_runner"] == unit_tp3
    assert trail1["aktif"] is True
    assert trail1["estimasi_net_pnl"] < p["order_execution"]["jumlah_unit_total"] * 100

    jalur = op.ekspor_rencana_order(p, output_dir=tmp_path, formats="csv")
    baris = list(csv.reader(Path(jalur["csv"]).read_text(encoding="utf-8").splitlines()))
    trail_rows = [r for r in baris if r[0] == "TRAIL_1"]
    assert len(trail_rows) == 1
    assert float(trail_rows[0][3]) == pytest.approx(unit_tp3)
    assert float(trail_rows[0][7]) == pytest.approx(trail1["estimasi_net_pnl"])