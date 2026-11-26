"""Unit test untuk penyambungan max_slots_cap & saklar tunggal DRY_RUN."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import strategy_config
import walt_bot


def test_get_effective_max_slots_respects_cap(monkeypatch):
    assert walt_bot.get_dynamic_max_slots(250.0) == 15
    assert walt_bot.get_effective_max_slots(250.0, cap=4) == 4
    assert walt_bot.get_effective_max_slots(250.0, cap=15) == 15

    monkeypatch.setattr(walt_bot, "MAX_SLOTS_CAP", 4)
    assert walt_bot.get_effective_max_slots(250.0) == 4
    # Saat slot dinamis (5 untuk < $30) masih di atas cap 4, cap tetap menang.
    assert walt_bot.get_effective_max_slots(20.0) == 4


def test_market_execute_stops_when_max_slots_cap_reached(monkeypatch):
    monkeypatch.setattr(walt_bot, "MAX_SLOTS_CAP", 4)
    monkeypatch.setattr(walt_bot, "is_state_warm", lambda: True)
    monkeypatch.setattr(walt_bot, "_update_regime_state", lambda *a, **kw: ("GEAR_1", 120, "ok"))

    client = MagicMock()
    client.readiness.return_value = {"ok": True}
    client.balance.return_value = {"accounts": [{"balance_available": "230.0", "currency": "USDC"}]}
    client.list_positions.return_value = {
        "positions": [
            {"contract_id": i, "contract_base_asset": f"A{i}", "margin_quote": "5.0", "status": "open"}
            for i in range(1, 5)
        ]
    }
    client.list_contracts.return_value = {"contracts": []}

    us_open = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
    res = walt_bot.market_execute(client, dry_run=True, now_utc=us_open)
    assert res["status"] == "slots_full"
    assert res["active_slots"] == 4
    assert res["max_slots"] == 4


def test_dry_run_is_single_source_of_truth_outside_strategy_specs():
    assert "dry_run" not in strategy_config.DEFAULT_STRATEGY_CONFIG
    assert "dry_run" not in strategy_config.STRATEGY_PARAM_SPECS
    assert "max_slots_cap" in strategy_config.STRATEGY_PARAM_SPECS
