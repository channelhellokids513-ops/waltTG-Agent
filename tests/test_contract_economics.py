"""Test tools/contract_economics.py.

Dua hal yang tidak boleh bocor: (1) alat ini tidak pernah bisa menulis keadaan bursa,
(2) data yang tidak terukur dilaporkan sebagai TIDAK TERUKUR, bukan sebagai nol.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import walt_bot  # noqa: E402

SPEC = importlib.util.spec_from_file_location("contract_economics", ROOT / "tools" / "contract_economics.py")
ce = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ce)


class FakeClient:
    """WaltClient mini: hanya mengirim apa yang sudah kita ketahui bentuknya."""

    def __init__(self, contracts=None, closed=None):
        self.calls: list[str] = []
        self._payload = {
            "perps_list_contracts": {"contracts": contracts or []},
            "perps_list_positions": {"positions": closed or []},
        }

    def _call(self, tool, args=None, _retry=False):
        self.calls.append(tool)
        return self._payload.get(tool, {})

    def list_contracts(self):
        return self._call("perps_list_contracts")

    def list_positions(self, statuses=None):
        return self._call("perps_list_positions", {"statuses": statuses} if statuses else {})


# --- penegakan read-only ------------------------------------------------------


def test_guard_menolak_mengisi_order_dan_menolak_prepare_sebelumnya():
    client = ce.guard_client(FakeClient())
    for tool in ("perps_execute_operation", "perps_prepare_order_place",
                 "perps_prepare_position_close", "perps_prepare_auto_close_set"):
        with pytest.raises(ce.ReadOnlyViolation):
            client._call(tool, {"confirmationToken": "x"})


def test_guard_menolak_tool_yang_belum_didaftarkan_bukan_hanya_yang_dikenal():
    client = ce.guard_client(FakeClient())
    with pytest.raises(ce.ReadOnlyViolation):
        client._call("perps_withdraw", {})


def test_guard_melewati_tool_baca_saja_dan_meneruskan_hasilnya():
    inner = FakeClient(contracts=[{"base_asset": "BTC"}])
    client = ce.guard_client(inner)
    assert client.list_contracts()["contracts"][0]["base_asset"] == "BTC"
    assert "perps_list_contracts" in inner.calls


def test_daftar_baca_dan_tulis_saling_lepas():
    assert not (ce.READ_ONLY_TOOLS & ce.FORBIDDEN_TOOLS)
    assert "perps_execute_operation" not in ce.READ_ONLY_TOOLS
    assert ce.READ_ONLY_TOOLS, "daftar baca-saja tidak boleh kosong"


# --- parsing defensif ----------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), ("abc", None), (True, None), (float("inf"), None),
    (float("nan"), None), ("1,234.5", 1234.5), ("$3.5", 3.5), ("0", 0.0), (-1.5, -1.5),
])
def test_to_float_tidak_mengubah_sampah_menjadi_nol(value, expected):
    assert ce.to_float(value) == expected


def test_to_time_menerima_z_naive_dan_menolak_bukan_waktu():
    assert ce.to_time("2026-11-20T10:00:00Z").hour == 10
    assert ce.to_time("2026-11-20 10:00:00").tzinfo is not None
    assert ce.to_time("") is None and ce.to_time(None) is None and ce.to_time("kemarin") is None


# --- bagian A: apa yang bursa nyatakan ----------------------------------------


def test_bagian_a_melaporkan_slippage_seragam_dan_sebarannya(capsys):
    contracts = [
        {"base_asset": "ETH", "price_slippage": "0.0100", "funding_rate_hourly": "0.00022",
         "min_size_base": "0.001", "max_leverage": 5},
        {"base_asset": "BTC", "price_slippage": "0.0010", "funding_rate_hourly": "0.00001",
         "min_size_base": "0.0001", "max_leverage": 40},
        {"base_asset": "SOL", "funding_rate_hourly": "0.0003", "min_size_base": "0.01"},
    ]
    result = ce.section_a(ce.guard_client(FakeClient(contracts=contracts)), hold_h=24.0)
    out = capsys.readouterr().out
    by_asset = {row["asset"]: row for row in result["rows"]}
    assert by_asset["ETH"]["slip"] == pytest.approx(0.01)
    assert by_asset["BTC"]["slip"] == pytest.approx(0.001)
    assert by_asset["SOL"]["slip"] is None, "slippage yang tidak dilaporkan harus tetap None"
    assert "TIDAK DILAPORKAN" in out
    assert result["stats"]["median"] == pytest.approx(0.0055)
    assert "sebaran slippage 2 aset" in out


def test_bagian_a_menilai_lolos_dengan_ambang_engine_sendiri(capsys):
    # funding ETH 0.00022/jam -> 0.528%/hari; slip 1% -> cost 1.18% -> req 1.77%/hari -> SKIP
    contracts = [{"base_asset": "ETH", "price_slippage": "0.01", "funding_rate_hourly": "0.00022"}]
    ce.section_a(ce.guard_client(FakeClient(contracts=contracts)), hold_h=24.0)
    out = capsys.readouterr().out
    assert "req=1.770%/hari" in out and "SKIP" in out


# --- ambang harus sama dengan engine (satu sumber kebenaran) ------------------


@pytest.mark.parametrize("slip", [0.0, 0.0005, 0.01, 0.025])
def test_print_cost_berbalik_tepat_di_ambang_gerbang_engine(slip):
    hold = 24.0
    cost = 2 * walt_bot.TAKER_FEE_PER_SIDE + slip
    required = cost * 1.5 / (hold / 24.0)
    lulus_engine = walt_bot.check_funding_vs_fee_gate(required, slip, hold)[0]
    assert lulus_engine is True, "garis uji harus tepat di ambang engine"
    assert "LOLOS" in ce.print_cost(slip, required, hold)
    assert "SKIP" in ce.print_cost(slip, required * 0.999, hold)
    assert lulus_engine is True
    assert walt_bot.check_funding_vs_fee_gate(required * 0.999, slip, hold)[0] is False


def test_print_cost_tidak_mengarang_biaya_bila_slippage_tidak_ada():
    text = ce.print_cost(None, 0.05, 24.0)
    assert "TIDAK DILAPORKAN" in text and "req=" not in text


# --- bagian B: carry nyata -----------------------------------------------------


def test_bagian_b_menghitung_carry_per_hari_dari_notional(capsys):
    closed = [{
        "contract_base_asset": "ETH", "funding_paid_quote": "0.01", "margin_quote": "3.0",
        "leverage": 2, "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-03T00:00:00Z",
    }]
    result = ce.section_b(ce.guard_client(FakeClient(closed=closed)))
    stats = result["per_day_notional"]
    # 0.01 / (3.0 * 2) / 2 hari * 100 = 0.08333%/hari
    assert stats["median"] == pytest.approx(0.0833333, abs=1e-5)
    assert result["measured"] is True
    assert "posisi tertutup terbaca=1 terpakai=1" in capsys.readouterr().out


def test_bagian_b_gagal_terbuka_saat_bidang_tidak_lengkap(capsys):
    closed = [{"contract_base_asset": "ETH", "funding_paid_quote": None, "margin_quote": "3.0",
               "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-02T00:00:00Z"}]
    result = ce.section_b(ce.guard_client(FakeClient(closed=closed)))
    assert result == {"measured": False}
    assert "TIDAK TERUKUR" in capsys.readouterr().out


# --- bagian C: fill vs keputusan ---------------------------------------------


SCAN_HEADER = ["timestamp_utc", "asset", "side", "price"]
CLOSED_OK = [{
    "contract_base_asset": "ETH", "avg_entry_price": "101.0",
    "created_at": "2026-11-20T12:00:00Z",
}]


def test_bagian_c_memilih_scan_terdekat_sebelum_fill_dalam_jendela(monkeypatch, capsys):
    rows = [SCAN_HEADER,
            ["2026-11-20 11:00:00", "ETH", "SHORT", "100.0"],   # 1 jam sebelum fill -> ini
            ["2026-11-19 12:00:00", "ETH", "SHORT", "90.0"],    # 24 jam -> masih dalam jendela
            ["2026-11-20 12:30:00", "ETH", "SHORT", "120.0"]]   # SESUDAH fill -> dilarang
    monkeypatch.setattr(ce.sheet_logger, "fetch_sheet_csv_rows_ex", lambda name: (rows, "ok"))
    result = ce.section_c(ce.guard_client(FakeClient(closed=CLOSED_OK)))
    stats = result["stats"]
    assert stats["median"] == pytest.approx(1.0), "deviasi terhadap 100.0, bukan 90.0 atau 120.0"
    assert result["measured"] is True
    assert "pasangan terukur=1" in capsys.readouterr().out


def test_bagian_c_tidak_memakai_scan_setelah_fill(monkeypatch, capsys):
    rows = [SCAN_HEADER, ["2026-11-20 13:00:00", "ETH", "SHORT", "100.0"]]  # sesudah fill
    monkeypatch.setattr(ce.sheet_logger, "fetch_sheet_csv_rows_ex", lambda name: (rows, "ok"))
    result = ce.section_c(ce.guard_client(FakeClient(closed=CLOSED_OK)))
    assert result["measured"] is False
    out = capsys.readouterr().out
    assert "TIDAK TERUKUR" in out and "tanpa padanan=1" in out


def test_bagian_c_gagal_terbuka_saat_sheet_tidak_terbaca(monkeypatch, capsys):
    monkeypatch.setattr(ce.sheet_logger, "fetch_sheet_csv_rows_ex", lambda name: ([], "http_error"))
    result = ce.section_c(ce.guard_client(FakeClient(closed=CLOSED_OK)))
    assert result["measured"] is False and result["sheet_status"] == "http_error"
    assert "TIDAK TERUKUR" in capsys.readouterr().out


def test_bagian_c_menolak_header_yang_kehilangan_kolom(monkeypatch, capsys):
    rows = [["timestamp_utc", "asset"], ["2026-11-20 11:00:00", "ETH"]]
    monkeypatch.setattr(ce.sheet_logger, "fetch_sheet_csv_rows_ex", lambda name: (rows, "ok"))
    result = ce.section_c(ce.guard_client(FakeClient(closed=CLOSED_OK)))
    assert result["measured"] is False
    assert "kolom wajib tidak ada" in capsys.readouterr().out.lower()


def test_alat_tidak_pernah_memanggil_tool_tulis_saati_semua_bagian_dijalankan():
    contracts = [{"base_asset": "ETH", "price_slippage": "0.01", "funding_rate_hourly": "0.00022"}]
    closed = [{"contract_base_asset": "ETH", "funding_paid_quote": "0.01", "margin_quote": "3.0",
               "leverage": 2, "avg_entry_price": "101.0",
               "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-03T00:00:00Z"}]
    inner = FakeClient(contracts=contracts, closed=closed)
    client = ce.guard_client(inner)
    ce.section_a(client, 24.0)
    ce.section_b(client)
    assert set(inner.calls) <= ce.READ_ONLY_TOOLS, inner.calls
