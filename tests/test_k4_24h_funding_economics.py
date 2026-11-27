"""K4 (2026-11-27): menyelaraskan ekonomi strategi dengan horizon 24 jam.

Dasar keputusan (evaluasi 18-26 Nov 2026, 28 trade terukur):
  - net -$1.0092; funding $0.0217 vs fee $0.2616 (8.3%)
  - median hold 2.9 jam, sementara E5 membenarkan biaya dengan asumsi 24 jam
  - 16 SHORT hanya menang 2x (net -$0.879 = 87% kerugian)
  - 4 posisi ditutup 26 Nov demi biaya inap 0.077%/hari (butuh ~87 jam untuk impas fee)
  - target $100/bulan mustahil pada ekuitas $19.45 (plafon funding ~$5/bulan)

Invarian yang dikunci:
  1. Entry ditolak bila funding tidak menutup biaya round-trip dalam 24 jam.
  2. Exit funding-flip menunggu biaya tertutup, dengan batas horizon 24 jam.
  3. Off-hours hanya menutup posisi bila biaya inap >= biaya menutup posisi 24 jam.
  4. Leg SHORT Engine 1 nonaktif secara default.
  5. Harga exit dipulihkan dari realized PnL bursa (fill nyata) saat respons tidak memuatnya.
  6. Target dilaporkan dalam dua satuan + ekuitas yang dibutuhkan untuk $100/bulan.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import walt_bot
from walt_bot import Position


# ---------------------------------------------------------------------------
# 1. Gerbang ekonomi: break-even hours <= 24 jam
# ---------------------------------------------------------------------------

def test_breakeven_hours_memakai_biaya_round_trip_dan_slippage():
    # cost = 0.18% + 0.10% = 0.28%; funding 0.23%/hari -> 29.2 jam
    assert walt_bot.funding_breakeven_hours(0.0023, 0.0010) == pytest.approx(29.217, abs=0.01)
    # funding 0.50%/hari -> 13.4 jam
    assert walt_bot.funding_breakeven_hours(0.0050, 0.0010) == pytest.approx(13.44, abs=0.01)
    # funding nol tidak akan pernah menutup biaya
    assert walt_bot.funding_breakeven_hours(0.0, 0.0010) is None


def test_kandidat_023_persen_ditolak_karena_butuh_29_jam():
    """BNB/NEAR/XRP 27 Nov = 0.23%/hari. Bila MIN_FUNDING diturunkan, gerbang break-even
    tetap menahannya -> dua lapis proteksi, bukan satu."""
    passed, _exp, _cost, note = walt_bot.check_funding_vs_fee_gate(
        funding_daily=0.0023, price_slippage=0.0010, expected_hold_hours=24.0
    )
    assert passed is False
    assert "breakeven=29.2h" in note and "maks 24h" in note


def test_funding_050_persen_lolos_dalam_24_jam():
    passed, exp_funding, cost, note = walt_bot.check_funding_vs_fee_gate(
        funding_daily=0.0050, price_slippage=0.0010, expected_hold_hours=24.0
    )
    assert passed is True
    assert exp_funding >= cost * walt_bot.FUNDING_COVER_MULTIPLIER
    assert "breakeven=13.4h" in note


def test_funding_di_bawah_1_5x_biaya_tetap_ditolak():
    cost = walt_bot.round_trip_cost_pct(0.0010)
    assert walt_bot.check_funding_vs_fee_gate(cost * 1.5, 0.0010, 24.0)[0] is True
    assert walt_bot.check_funding_vs_fee_gate(cost * 1.4, 0.0010, 24.0)[0] is False


def test_ambang_off_hours_sama_dengan_biaya_round_trip_24_jam():
    """Angka 0.28%/hari BUKAN pilihan rasa: ia sama dengan biaya menutup posisi."""
    assert walt_bot.OFF_HOURS_MIN_ADVERSE_FUNDING_DAILY == pytest.approx(
        walt_bot.round_trip_cost_pct(0.0010), abs=1e-9
    )


# ---------------------------------------------------------------------------
# 2. Leg SHORT Engine 1 nonaktif (default) + bisa dihidupkan
# ---------------------------------------------------------------------------

def _opp(**kw) -> SimpleNamespace:
    base = dict(
        contract_id=1,
        base_asset="AAVE",
        funding_daily=-0.006,
        volume_24h=5_000_000.0,
        max_leverage=2,
        side="long",
        min_size_base=0.0,
        price_slippage=0.0,
        signal_source="funding",
        forecast_setup="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_allow_engine1_short_default_nonaktif(monkeypatch):
    monkeypatch.delenv("ALLOW_ENGINE1_SHORT", raising=False)
    assert walt_bot.allow_engine1_short() is False
    for value, expected in (("true", True), ("1", True), ("on", True), ("false", False), ("0", False)):
        monkeypatch.setenv("ALLOW_ENGINE1_SHORT", value)
        assert walt_bot.allow_engine1_short() is expected


def test_short_engine1_diskip_saat_nonaktif(monkeypatch):
    monkeypatch.delenv("ALLOW_ENGINE1_SHORT", raising=False)
    opp = _opp(base_asset="NVDA", side="short", funding_daily=0.006)
    verdict, reason, mult = walt_bot.decide_go_nogo(opp, None)
    assert (verdict, mult) == ("SKIP", 0.0)
    assert "SHORT Engine 1 nonaktif" in reason


def test_long_engine1_tetap_berjalan(monkeypatch):
    monkeypatch.delenv("ALLOW_ENGINE1_SHORT", raising=False)
    verdict, _reason, mult = walt_bot.decide_go_nogo(_opp(), None)
    assert (verdict, mult) == ("GO", 1.0)


def test_short_engine1_bisa_dihidupkan_lewat_env(monkeypatch):
    monkeypatch.setenv("ALLOW_ENGINE1_SHORT", "true")
    opp = _opp(base_asset="AAVE", side="short", funding_daily=0.006)
    verdict, _reason, mult = walt_bot.decide_go_nogo(opp, None)
    assert (verdict, mult) == ("GO", 1.0)
def test_workflow_entry_meneruskan_knob_allow_engine1_short():
    """Knob yang tidak diteruskan workflow = knob yang tidak pernah aktif di produksi."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in ("daily-scan.yml", "market-execute.yml", "crypto-execute.yml", "risk-monitor.yml"):
        text = (root / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "ALLOW_ENGINE1_SHORT: ${{ vars.ALLOW_ENGINE1_SHORT" in text, name
# ---------------------------------------------------------------------------
# 3. Harga exit dari realized PnL bursa (fill nyata)
# ---------------------------------------------------------------------------

def test_exit_price_diturunkan_dari_realized_pnl_long_dan_short():
    rec = {"value_base": "2.0", "realized_pnl_quote": "0.40"}
    assert walt_bot._exit_price_from_realized_pnl(rec, 100.0, "long") == pytest.approx(100.2)
    assert walt_bot._exit_price_from_realized_pnl(rec, 100.0, "short") == pytest.approx(99.8)


def test_exit_price_menolak_record_tanpa_ukuran_kontrak():
    assert walt_bot._exit_price_from_realized_pnl({"realized_pnl_quote": "0.4"}, 100.0, "long") is None
    assert walt_bot._exit_price_from_realized_pnl({"value_base": "0"}, 100.0, "long") is None


def test_recover_fill_mencocokkan_position_id_dan_mengembalikan_sumber_jujur():
    pos = Position(
        contract_id=7, base_asset="AAVE", side="long", leverage=2, margin=2.5,
        entry_price=100.0, current_price=100.1, tp=102.5, sl=98.5, funding_paid=0.0,
        liquidation_price=50.0, position_id="pos-abc",
    )

    class _Client:
        def list_positions(self, statuses=None):
            return {"positions": [
                {"position_id": "lain", "contract_id": 7, "value_base": "1.0", "realized_pnl_quote": "9.9"},
                {"position_id": "pos-abc", "contract_id": 7, "value_base": "2.0", "realized_pnl_quote": "0.40"},
            ]}

    recovered = walt_bot._recover_realized_exit_fill(_Client(), pos)
    assert recovered is not None
    assert recovered[0] == pytest.approx(100.2)
    assert recovered[1] == "exchange_realized_pnl"


def test_recover_fill_tidak_melempar_saat_bursa_gagal():
    class _Broken:
        def list_positions(self, statuses=None):
            raise RuntimeError("down")

    pos = Position(
        contract_id=7, base_asset="AAVE", side="long", leverage=2, margin=2.5,
        entry_price=100.0, current_price=100.0, tp=None, sl=None, funding_paid=0.0,
        liquidation_price=50.0,
    )
    assert walt_bot._recover_realized_exit_fill(_Broken(), pos) is None


def test_detect_exchange_close_pulihkan_exit_dari_realized_pnl_sebelum_mark(
    tmp_path, captured_closes
):
    import json
    from datetime import timedelta
    from unittest.mock import MagicMock

    now = datetime.now(timezone.utc)
    upd = (now - timedelta(minutes=2)).isoformat()
    crt = (now - timedelta(hours=1)).isoformat()
    cp = {
        "position_id": "pos-auto-close-1",
        "contract_base_asset": "NVDA",
        "contract_id": 10,
        "side": "long",
        "leverage": 2,
        "margin_quote": "2.5",
        "avg_entry_price": "100.0",
        "value_base": "2.0",
        "realized_pnl_quote": "0.40",
        "funding_paid_quote": "0.01",
        "status": "closed",
        "created_at": crt,
        "updated_at": upd,
    }
    dedupe_key = walt_bot.make_position_dedupe_key(cp)
    snap_path = tmp_path / "open_snapshot.json"
    snap_path.write_text(json.dumps({
        dedupe_key: {
            "position_id": dedupe_key,
            "contract_id": 10,
            "base_asset": "NVDA",
            "side": "long",
            "leverage": 2,
            "margin": 2.5,
            "entry_price": 100.0,
            "funding_paid": 0.01,
            "tp": 102.5,
            "sl": 98.5,
            "opened_at": crt,
            "verdict": "GO",
        }
    }), encoding="utf-8")

    client = MagicMock()
    client.list_positions.return_value = {"positions": [cp]}
    walt_bot._detect_and_log_exchange_closes(
        client,
        active_positions=[],
        snapshot_path=str(snap_path),
        closed_ids_path=str(tmp_path / "closed_ids.json"),
        closed_csv_path=str(tmp_path / "closed_history.csv"),
    )
    assert len(captured_closes) == 1
    assert captured_closes[0]["exit_price"] == pytest.approx(100.2, abs=1e-4)
    assert captured_closes[0]["exit_source"] == "exchange_realized_pnl"
    client.latest_rate.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Target: satuan ekuitas + ekuitas yang dibutuhkan untuk $100/bulan
# ---------------------------------------------------------------------------

def test_ekuitas_dibutuhkan_untuk_100_dolar_per_bulan_pada_funding_05_persen():
    assert walt_bot.required_equity_for_nominal_target(100.0, 0.50) == pytest.approx(392.16, abs=0.5)


def test_progress_target_dua_satuan():
    info = walt_bot.compute_target_progress(funding_earned=0.0309, total_equity=19.45)
    assert info["progress_nominal_pct"] == pytest.approx(0.0309, abs=1e-4)
    assert info["target_by_equity"] == pytest.approx(0.9725, abs=1e-4)
    assert info["progress_equity_pct"] == pytest.approx(3.18, abs=0.01)
    assert info["required_equity_for_nominal"] == pytest.approx(392.16, abs=0.5)


# ---------------------------------------------------------------------------
# 5. Audit universe (read-only): peluang di luar whitelist + drift likuiditas
# ---------------------------------------------------------------------------

def test_summarize_funding_window_menghitung_cover_24_jam():
    """Jendela 24 jam berjalan: funding vs fee harus terlihat, bukan diasumsikan."""
    now = datetime(2026, 11, 26, 22, 30, tzinfo=timezone.utc)
    trades = [
        {"closed_at": "2026-11-26T21:00:00Z", "funding_usd": 0.004, "fee_usd": 0.010, "pnl_usd": -0.02},
        {"closed_at": "2026-11-26T10:00:00Z", "funding_usd": 0.002, "fee_usd": 0.010, "pnl_usd": 0.01},
        {"closed_at": "2026-11-24T10:00:00Z", "funding_usd": 0.5, "fee_usd": 0.5, "pnl_usd": 5.0},
    ]
    out = walt_bot.summarize_funding_window(trades, hours=24, now_utc=now)
    assert out["trades"] == 2
    assert out["funding_usd"] == pytest.approx(0.006)
    assert out["fee_usd"] == pytest.approx(0.020)
    assert out["funding_cover_ratio"] == pytest.approx(0.3)
    assert out["funding_covered_fees"] is False
    assert out["net_usd"] == pytest.approx(-0.024)


def test_summarize_funding_window_kosong_tidak_mengarang_rasio():
    out = walt_bot.summarize_funding_window([], hours=24)
    assert out["trades"] == 0
    assert out["funding_cover_ratio"] is None
    assert out["funding_covered_fees"] is None


def test_audit_universe_menemukan_peluang_di_luar_whitelist_dan_drift():
    contracts = [
        {"base_asset": "PRL", "volume_24h_usd": "1723813", "funding_rate_hourly": str(0.0488 / 24)},
        {"base_asset": "MU", "volume_24h_usd": "4344006", "funding_rate_hourly": str(0.0060 / 24)},
        {"base_asset": "AAVE", "volume_24h_usd": "2000000", "funding_rate_hourly": str(-0.0072 / 24)},
        {"base_asset": "NVDA", "volume_24h_usd": "5000000", "funding_rate_hourly": str(0.0048 / 24)},
        {"base_asset": "MON", "volume_24h_usd": "526208", "funding_rate_hourly": str(0.0023 / 24)},
    ]
    allowed = {"AAVE", "NVDA", "MON"}
    result = walt_bot.audit_universe(contracts, allowed_assets=allowed, top_n=4)

    assert result["total_contracts"] == 5
    assert result["liquid_contracts"] == 4  # MON di bawah floor $1M
    assert [row["asset"] for row in result["top_by_abs_funding"]] == ["PRL", "AAVE", "MU", "NVDA"]
    outside = {row["asset"] for row in result["top_outside_whitelist_passing_funding"]}
    assert outside == {"PRL", "MU"}
    assert result["missed_top_pct"] == pytest.approx(50.0)
    assert result["eligible_inside_whitelist"] == 1  # hanya AAVE (0.72%/hari)
    assert [row["asset"] for row in result["whitelisted_below_volume_floor"]] == ["MON"]
    assert "DI LUAR whitelist" in result["verdict"]