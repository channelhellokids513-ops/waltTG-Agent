import os
import sys

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
        position_id="pos_r4_1",
        opened_at="2026-11-19T00:00:00+00:00",
    )


def test_r4_1_missing_registry_key_does_not_raise(monkeypatch, caplog):
    monkeypatch.setattr(walt_bot, "TRAILING_TIERS", [(0.0080, "lock_0035")])
    monkeypatch.setattr(walt_bot, "_TRAILING_HANDLERS", {})
    pos = _pos(side="long", entry=100.0, current=101.0, asset="NVDA")

    with caplog.at_level("ERROR", logger="walt-bot"):
        res = walt_bot._resolve_trailing_sl(pos, 0.0100)

    assert res is None
    assert any(
        rec.levelname == "ERROR"
        and "lock_0035" in rec.message
        and "NVDA" in rec.message
        for rec in caplog.records
    )
