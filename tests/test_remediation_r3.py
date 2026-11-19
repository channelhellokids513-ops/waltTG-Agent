import math
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import walt_bot


def _pos(side="long", entry=100.0, current=101.0, tp=102.5, sl=98.5, asset="NVDA"):
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
        funding_paid=0.01,
        liquidation_price=50.0,
        position_id="pos_r3_1",
        opened_at="2026-11-19T00:00:00+00:00",
    )


def test_r3_1_uses_exchange_fill_price_when_present(monkeypatch):
    import sheet_logger

    logged = []
    monkeypatch.setattr(sheet_logger, "log_close", lambda **kw: logged.append(kw))
    monkeypatch.setattr(walt_bot, "_set_asset_cooldown", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "_record_closed_id", lambda *a, **kw: None)

    client = MagicMock()
    client.execute.return_value = {
        "ok": True,
        "order": {"order_id": "ord_close_1", "avg_fill_price": "102.34567"},
    }
    pos = _pos(side="long", entry=100.0, current=101.0)
    res = walt_bot._execute_validated_close(
        client, pos, {"ok": True, "confirmationToken": "tok_1"}, reason="TP breached"
    )
    assert res["action"] == "CLOSE"
    assert len(logged) == 1
    assert logged[0]["exit_price"] == pytest.approx(102.3457, abs=1e-4)
    assert logged[0]["exit_price"] != pos.current_price


def test_r3_1_falls_back_to_snapshot_when_fill_absent(monkeypatch):
    import sheet_logger

    logged = []
    monkeypatch.setattr(sheet_logger, "log_close", lambda **kw: logged.append(kw))
    monkeypatch.setattr(walt_bot, "_set_asset_cooldown", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "_record_closed_id", lambda *a, **kw: None)

    client = MagicMock()
    client.execute.return_value = {"ok": True, "order": {"order_id": "ord_close_2"}}
    pos = _pos(side="long", entry=100.0, current=98.12345)
    res = walt_bot._execute_validated_close(
        client, pos, {"ok": True, "confirmationToken": "tok_2"}, reason="Funding Flip Sign"
    )
    assert res["action"] == "CLOSE"
    assert len(logged) == 1
    assert logged[0]["exit_price"] == pytest.approx(98.1235, abs=1e-4)


@pytest.mark.parametrize(
    "bad_val",
    [0, 0.0, -5.0, None, "", "not_a_number", float("nan"), float("inf"), True],
)
def test_r3_1_never_writes_zero_or_none_exit_price(monkeypatch, bad_val):
    import sheet_logger

    logged = []
    monkeypatch.setattr(sheet_logger, "log_close", lambda **kw: logged.append(kw))
    monkeypatch.setattr(walt_bot, "_set_asset_cooldown", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "_record_closed_id", lambda *a, **kw: None)

    client = MagicMock()
    client.execute.return_value = {
        "ok": True,
        "order": {"avg_fill_price": bad_val, "price": bad_val},
        "position": {"avg_close_price": bad_val},
        "price": bad_val,
    }
    pos = _pos(side="long", entry=100.0, current=99.25)
    res = walt_bot._execute_validated_close(
        client, pos, {"ok": True, "confirmationToken": "tok_bad"}, reason="TP breached"
    )
    assert res["action"] == "CLOSE"
    assert len(logged) == 1
    exit_p = logged[0]["exit_price"]
    assert exit_p is not None and exit_p > 0 and math.isfinite(exit_p)
    assert exit_p == pytest.approx(99.25, abs=1e-4)


def test_r3_1_close_reason_strings_unchanged(monkeypatch):
    import sheet_logger

    reasons = []
    monkeypatch.setattr(sheet_logger, "log_close", lambda **kw: reasons.append(kw["reason"]))
    monkeypatch.setattr(walt_bot, "_set_asset_cooldown", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "_record_closed_id", lambda *a, **kw: None)

    client = MagicMock()
    client.preview_close.return_value = {"ok": True, "confirmationToken": "tok_c"}
    client.execute.return_value = {"ok": True, "price": 100.5}

    # 1. Funding Flip Sign
    p_flip = _pos(side="long", entry=100.0, current=100.0, tp=102.5, sl=98.5)
    walt_bot.evaluate_and_apply_position_risk(client, p_flip, current_funding_daily=0.005)

    # 2. SL breached (migrasi)
    p_sl = _pos(side="long", entry=100.0, current=98.0, tp=102.5, sl=98.5)
    walt_bot.evaluate_and_apply_position_risk(client, p_sl, current_funding_daily=-0.006)

    # 3. TP breached
    p_tp = _pos(side="long", entry=100.0, current=103.0, tp=102.5, sl=98.5)
    walt_bot.evaluate_and_apply_position_risk(client, p_tp, current_funding_daily=-0.006)

    assert reasons == [
        "Funding Flip Sign",
        f"SL {walt_bot.DEFAULT_SL_PCT*100:.2f}% breached (migrasi)",
        "TP breached",
    ]
