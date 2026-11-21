"""Test pengukuran slippage di jalur produksi (WaltClient).

Yang dijaga: (1) angka dihitung dengan tanda yang benar, (2) pengukuran TIDAK PERNAH bisa
menjatuhkan order atau penutupan, (3) data yang tidak ada tidak diisi nol, dan (4) hasilnya
benar-benar tertulis ke berkas - bug `Path` tidak diimpor dulu akan hilang diam-diam di
dalam try/except, dan itu justru yang kita kunci di sini.
"""

from __future__ import annotations

import json

import pytest

import walt_bot


def make_client(monkeypatch, responses=None, rate=None, rate_error=None):
    """WaltClient tanpa __init__ (tidak menyentuh TokenManager/httpx)."""
    client = walt_bot.WaltClient.__new__(walt_bot.WaltClient)
    client._quotes = {}
    client.calls = []

    def fake_call(tool, args=None, _retry=False):
        client.calls.append((tool, args))
        return (responses or {}).get(tool, {"ok": True})

    client._call = fake_call
    if rate_error is not None:
        def boom(market_id):
            raise rate_error
        client.latest_rate = boom
    elif rate is not None:
        client.latest_rate = lambda market_id: {"rate": str(rate)}
    return client


@pytest.fixture(autouse=True)
def slippage_log(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "slippage_observed.jsonl"
    monkeypatch.setattr(walt_bot, "SLIPPAGE_OBSERVED_PATH", str(path))
    return path


def test_open_pengukuran_tercatat_dan_tertulis_ke_berkas(slippage_log):
    client = make_client(None)
    client.note_quote("open", 1, 100.0)
    result = {"ok": True, "side": "long", "order": {"contract_id": 1, "avg_fill_price": "100.25"}}
    record = client.record_execution_slippage(result)
    assert record["stage"] == "open"
    assert record["slippage_pct"] == pytest.approx(0.25)
    assert slippage_log.exists(), "pengukuran harus tertulis, bukan hanya di log"
    line = json.loads(slippage_log.read_text(encoding="utf-8").strip())
    assert line["fill"] == 100.25 and line["quote"] == 100.0
    assert line["quote_age_s"] >= 0


def test_short_dihitung_dengan_tanda_yang_benar():
    client = make_client(None)
    client.note_quote("open", 7, 100.0)
    record = client.record_execution_slippage(
        {"side": "short", "contract_id": 7, "avg_fill_price": "99.80"})
    # short terisi 99,80 padahal kuotasinya 100,00 = kita MENJUAL lebih murah = BURUK -> +0,20%
    assert record["slippage_pct"] == pytest.approx(0.20)
    client.note_quote("open", 8, 100.0)
    good = client.record_execution_slippage(
        {"side": "short", "contract_id": 8, "avg_fill_price": "100.30"})
    assert good["slippage_pct"] == pytest.approx(-0.30), "menjual lebih tinggi dari kuotasi = menguntungkan"


def test_tanpa_kuotasi_tersimpan_tidak_ada_angka_yang_diarang():
    client = make_client(None)
    assert client.record_execution_slippage({"side": "long", "contract_id": 1,
                                             "avg_fill_price": "100.0"}) is None
    # kuotasi kontrak lain tidak boleh dipakai untuk kontrak ini
    client.note_quote("open", 2, 100.0)
    assert client.record_execution_slippage({"side": "long", "contract_id": 1,
                                             "avg_fill_price": "100.0"}) is None


@pytest.mark.parametrize("payload", [None, "bukan dict", {}, {"order": None},
                                     {"contract_id": 1}, {"contract_id": 1, "order": {"price": "n/a"}}])
def test_sampah_masuk_tanpa_lemparan_dan_tanpa_hasil_palsu(payload):
    client = make_client(None)
    client.note_quote("open", 1, 100.0)
    assert client.record_execution_slippage(payload) is None


def test_fill_di_dalam_order_ditemukan_meski_contract_id_di_induk():
    client = make_client(None)
    cid, price = client._walk_fill({"contract_id": 3, "order": {"avg_fill_price": "41.26"}})
    assert (cid, price) == (3, 41.26)


def test_execute_tetap_mengembalikan_hasil_walau_pengukuran_meledak(monkeypatch):
    client = make_client(None, {"perps_execute_operation": {"ok": True, "order": {"order_id": "X"}}})
    monkeypatch.setattr(client, "record_execution_slippage",
                        lambda result: (_ for _ in ()).throw(RuntimeError("sensor mati")))
    assert client.execute("tok") == {"ok": True, "order": {"order_id": "X"}}


def test_execute_mencatat_setelah_call_sejati():
    client = make_client(None, {"perps_execute_operation": {
        "ok": True, "side": "long", "contract_id": 1, "avg_fill_price": "101.0"}})
    client.note_quote("open", 1, 100.0)
    out = client.execute("tok")
    assert out["ok"] is True
    assert ("open", 1) not in client._quotes, "kuotasi harus dikonsumsi sekali pakai"


def test_preview_order_mencatat_kuotasi_pra_isi():
    client = make_client(None)
    out = client.preview_order(1, "long", 2, 5.0, 57700.0, tp=58800.0, sl=56800.0)
    assert out["ok"] is True
    assert client._quotes[("open", 1)][0] == 57700.0


def test_preview_close_mencatat_kuotasi_dari_rate_terbaru():
    responses = {
        "perps_prepare_position_close": {"ok": True, "confirmationToken": "t"},
        "perps_list_contracts": {"contracts": [{"contract_id": 9, "base_asset": "XAG"}]},
    }
    client = make_client(None, responses, rate=41.2687)
    out = client.preview_close(9)
    assert out["ok"] is True
    assert client._quotes[("close", 9)][0] == pytest.approx(41.2687)
    # daftar kontrak diambil sekali lalu di-cache (test berikutnya tidak memanggil ulang)
    assert ("perps_list_contracts", None) in client.calls
    client.preview_close(9)
    assert client._contract_assets == {9: "XAG"}


def test_gagal_ambil_rate_tidak_permah_menghalangi_penutupan():
    """Posisi tanpa SL jauh lebih berbahaya daripada satu baris pengukuran yang hilang."""
    responses = {
        "perps_prepare_position_close": {"ok": True, "confirmationToken": "t"},
        "perps_list_contracts": {"contracts": [{"contract_id": 9, "base_asset": "XAG"}]},
    }
    client = make_client(None, responses, rate_error=RuntimeError("relay mati"))
    out = client.preview_close(9)
    assert out["ok"] is True, "penutupan harus tetap berjalan"
    assert ("close", 9) not in client._quotes


def test_daftar_kontrak_gagal_tidak_menghancurkan_close():
    client = make_client(None, {"perps_prepare_position_close": {"ok": True}},
                         rate=10.0)
    def broken_list():
        raise RuntimeError("oauth mati")
    client.list_contracts = broken_list
    out = client.preview_close(5)
    assert out["ok"] is True
