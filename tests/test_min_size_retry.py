"""Test retry min-size (§3r.1, keputusan pemilik).

Empat hal yang harus mati-mati dijaga: retry hanya SEKALI, hanya untuk kode error
min-size bursa, selalu lewat plafon max_allowed_margin dan gerbang sisa waktu job,
dan margin yang dicatat ke caller adalah margin yang benar-benar terpakai.
"""

from __future__ import annotations

import pytest

import walt_bot
from walt_bot import Opportunity

MIN_SIZE_MSG = ('Walt MCP error (perps_execute_operation): {"error":{"code":'
                '"agent.incorrect.order_params","message":"Incorrect data for creating '
                'order: margin < contract.min_size_base","retryable":false}}')

# Persis seperti yang muncul di produksi #44900424439 (SHORT ANTHROPIC): kode error sama,
# arahnya berlawanan - margin kita yang TERLALU BESAR. Tidak boleh pernah di-retry naik.
MAX_SIZE_MSG = ('Walt MCP error (perps_execute_operation): {"error":'
                '{"code":"agent.incorrect.order_params","message":"Incorrect data for '
                'creating order: margin > contract.max_size_quote","retryable":false,'
                '"details":[]}}')


def make_opp(asset="BNB"):
    return Opportunity(contract_id=17, base_asset=asset, funding_daily=0.0056,
                       volume_24h=8e8, max_leverage=2, side="long", min_size_base=0.01,
                       price_slippage=0.0001)


class RetryClient:
    def __init__(self, reject_times=1, error_text=MIN_SIZE_MSG):
        self.reject_times = reject_times
        self.error_text = error_text
        self.preview_margins = []
        self.execute_tokens = []
        self.slippage_calls = []

    def preview_order(self, contract_id, side, leverage, amount, price, tp=None, sl=None):
        self.preview_margins.append(float(amount))
        return {"ok": True, "confirmationToken": f"tok{len(self.preview_margins)}",
                "action": {"request": {"autoClose": {"takeProfit": {}, "stopLoss": {}}}}}

    def execute(self, token):
        self.execute_tokens.append(token)
        if len(self.execute_tokens) <= self.reject_times:
            raise RuntimeError(self.error_text)
        return {"ok": True, "order": {"order_id": "o1", "avg_fill_price": "524.10"}}

    def record_open_slippage(self, contract_id, result, settle_seconds=3.0):
        # dicatat pada titik yang sama dengan kode produksi: setelah percobaan yang terisi
        self.slippage_calls.append(len(self.execute_tokens))
        return None


def test_retry_sekali_dan_mencatat_margin_yang_pakai(monkeypatch):
    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda _m: True)
    client = RetryClient()
    result = walt_bot.open_position(client, make_opp(), margin=2.50, leverage=2,
                                    resolved=(2.50, 524.94))
    assert result is not None
    assert client.preview_margins == [2.50, 2.75], "retry = x1,10 dibulatkan 2 desimal"
    assert len(client.execute_tokens) == 2
    assert result["actual_margin"] == pytest.approx(2.75), "caller harus pakai margin terisi"
    assert result["tp"] and result["sl"]


def test_retry_hanya_satu_kali_kalau_masih_ditolak(monkeypatch):
    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda _m: True)
    client = RetryClient(reject_times=5)
    assert walt_bot.open_position(client, make_opp(), margin=2.50, leverage=2,
                                  resolved=(2.50, 524.94)) is None
    assert len(client.execute_tokens) == 2, "tidak boleh ada retry ketiga"
    assert client.preview_margins == [2.50, 2.75]


def test_plafon_max_allowed_margin_tetap_mengikat(monkeypatch):
    """requested 2.50 -> plafon 3.75; margin resolved 4.00 -> retry 4.40 melewati plafon."""
    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda _m: True)
    client = RetryClient()
    assert walt_bot.open_position(client, make_opp(), margin=2.50, leverage=2,
                                  resolved=(4.00, 524.94)) is None
    assert client.preview_margins == [4.00], "retry tidak boleh menembus plafon risiko"
    assert len(client.execute_tokens) == 1


def test_sisa_waktu_job_membatalkan_retry(monkeypatch):
    calls = {"n": 0}

    def fake_time(_minutes):
        calls["n"] += 1
        return calls["n"] == 1  # gerbang pra-execute lolos, gerbang retry tidak

    monkeypatch.setattr(walt_bot, "has_enough_job_time", fake_time)
    client = RetryClient()
    assert walt_bot.open_position(client, make_opp(), margin=2.50, leverage=2,
                                  resolved=(2.50, 524.94)) is None
    assert len(client.execute_tokens) == 1
    assert client.preview_margins == [2.50]


def test_error_lain_tidak_pernah_di_retry():
    for text in ("network timeout", "insufficient_balance", "agent.not_logged_in",
                 "margin < contract.min_size but fine"):
        client = RetryClient(error_text=text)
        assert walt_bot.open_position(client, make_opp(), margin=2.50, leverage=2,
                                      resolved=(2.50, 524.94)) is None, text
        assert len(client.execute_tokens) == 1, f"retry tidak sah untuk: {text}"


def test_pengenalan_penolakan_min_size():
    assert walt_bot.is_min_size_rejection(RuntimeError(MIN_SIZE_MSG))
    assert not walt_bot.is_min_size_rejection(RuntimeError("timeout"))
    assert not walt_bot.is_min_size_rejection(ValueError(""))


def test_max_size_quote_tidak_boleh_di_retry_naik_regresi_44900424439():
    """Kode error generik yang sama + arah pelanggaran berlawanan = BUKAN min-size."""
    assert not walt_bot.is_min_size_rejection(RuntimeError(MAX_SIZE_MSG))
    client = RetryClient(error_text=MAX_SIZE_MSG)
    assert walt_bot.open_position(client, make_opp(asset="ANTHROPIC"), margin=2.50,
                                  leverage=50, resolved=(2.50, 100.0)) is None
    assert len(client.execute_tokens) == 1, "retry naik justru memperbesar pelanggaran"
    assert client.preview_margins == [2.50]
    # kedua pesan dalam satu string (mis./details) tetap tidak boleh memicu retry naik
    assert not walt_bot.is_min_size_rejection(RuntimeError(MIN_SIZE_MSG + MAX_SIZE_MSG))


def test_pengukuran_slippage_hanya_untuk_percobaan_yang_terisi(monkeypatch):
    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda _m: True)
    client = RetryClient()
    walt_bot.open_position(client, make_opp(), margin=2.50, leverage=2, resolved=(2.50, 524.94))
    assert client.slippage_calls == [2], (
        "slippage hanya boleh diukur setelah execute yang berhasil, bukan yang ditolak")
