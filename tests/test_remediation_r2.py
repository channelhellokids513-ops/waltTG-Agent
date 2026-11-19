import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import walt_bot


def _pos(side="long", entry=100.0, current=100.0, tp=None, sl=None, asset="NVDA"):
    return walt_bot.Position(
        contract_id=10,
        base_asset=asset,
        side=side,
        leverage=2,
        margin=2.5,
        entry_price=entry,
        current_price=current,
        tp=tp,
        sl=sl,
        funding_paid=0.0,
        liquidation_price=50.0,
        position_id="pos_test_1",
        opened_at="2026-11-19T00:00:00+00:00",
    )


def test_p0_3_none_preview_does_not_raise_attribute_error():
    client = MagicMock()
    client.preview_auto_close.return_value = None

    p_base = _pos(side="long", entry=100.0, current=100.1, tp=None, sl=None)
    res_eval = walt_bot.evaluate_and_apply_position_risk(client, p_base, current_funding_daily=-0.006)
    assert isinstance(res_eval, dict)
    assert res_eval["action"] == "FAILED"

    res_l1 = walt_bot.apply_layer1_baseline(client, p_base)
    assert res_l1 is None

    p_trail = _pos(side="long", entry=100.0, current=101.0, tp=102.5, sl=98.5)
    res_l4 = walt_bot.apply_layer4_trailing(client, p_trail)
    assert res_l4 is None


def test_p0_2_failed_close_execute_does_not_log_close_or_set_cooldown(monkeypatch):
    import sheet_logger

    log_close_calls = []
    cooldown_calls = []
    closed_id_calls = []
    tg_calls = []

    monkeypatch.setattr(sheet_logger, "log_close", lambda **kw: log_close_calls.append(kw))
    monkeypatch.setattr(walt_bot, "_set_asset_cooldown", lambda *a, **kw: cooldown_calls.append(a))
    monkeypatch.setattr(walt_bot, "_record_closed_id", lambda *a, **kw: closed_id_calls.append(a))
    monkeypatch.setattr(walt_bot, "send_telegram", lambda msg, **kw: tg_calls.append(msg))

    client = MagicMock()
    client.preview_close.return_value = {"ok": True, "confirmationToken": "tok_close"}
    client.execute.return_value = {"ok": False, "error": "busy"}

    # Funding flip close gagal -> tidak boleh log_close / cooldown / closed_ids
    p = _pos(side="long", entry=100.0, current=100.0, tp=102.5, sl=98.5)
    res_fail = walt_bot.evaluate_and_apply_position_risk(client, p, current_funding_daily=0.005)
    assert res_fail["action"] == "FAILED"
    assert log_close_calls == []
    assert cooldown_calls == []
    assert closed_id_calls == []
    assert any("GAGAL TUTUP: NVDA" in m for m in tg_calls)

    # Execute sukses -> log_close dipanggil tepat 1x
    client.execute.return_value = {"ok": True, "order_id": "ord_ok"}
    res_ok = walt_bot.evaluate_and_apply_position_risk(client, p, current_funding_daily=0.005)
    assert res_ok["action"] == "CLOSE"
    assert len(log_close_calls) == 1
    assert len(cooldown_calls) == 1
    assert len(closed_id_calls) == 1

