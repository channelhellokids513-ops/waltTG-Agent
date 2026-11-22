"""Unit tests untuk filter jam reguler pasar Saham AS (Senin-Jumat 13:30-21:00 UTC)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import walt_bot


@pytest.mark.parametrize(
    "dt_utc,expected",
    [
        # Senin 13:29 UTC -> belum buka
        (datetime(2026, 11, 23, 13, 29, tzinfo=timezone.utc), False),
        # Senin 13:30 UTC -> tepat buka (EDT open)
        (datetime(2026, 11, 23, 13, 30, tzinfo=timezone.utc), True),
        # Rabu 17:00 UTC -> di tengah jam reguler AS
        (datetime(2026, 11, 25, 17, 0, tzinfo=timezone.utc), True),
        # Jumat 20:59 UTC -> masih buka (1 menit sebelum tutup EST)
        (datetime(2026, 11, 27, 20, 59, tzinfo=timezone.utc), True),
        # Jumat 21:00 UTC -> sudah tutup
        (datetime(2026, 11, 27, 21, 0, tzinfo=timezone.utc), False),
        # Selasa 00:17 UTC -> di luar jam reguler AS
        (datetime(2026, 11, 24, 0, 17, tzinfo=timezone.utc), False),
        # Sabtu 15:00 UTC -> akhir pekan tutup
        (datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc), False),
        # Minggu 16:00 UTC -> akhir pekan tutup
        (datetime(2026, 11, 22, 16, 0, tzinfo=timezone.utc), False),
    ],
)
def test_is_us_stock_market_open_utc_boundaries(monkeypatch, dt_utc, expected):
    monkeypatch.delenv("US_STOCK_HOURS_ONLY", raising=False)
    is_open, note = walt_bot.is_us_stock_market_open_utc(now_utc=dt_utc)
    assert is_open is expected, f"{dt_utc.isoformat()} -> {note}"


def test_us_stock_hours_kill_switch_false(monkeypatch):
    monkeypatch.setenv("US_STOCK_HOURS_ONLY", "false")
    sunday = datetime(2026, 11, 22, 16, 0, tzinfo=timezone.utc)
    is_open, note = walt_bot.is_us_stock_market_open_utc(now_utc=sunday)
    assert is_open is True
    assert "US_STOCK_HOURS_ONLY=false" in note


def test_market_execute_skips_us_stocks_outside_hours_but_runs_commodities(monkeypatch):
    monkeypatch.delenv("US_STOCK_HOURS_ONLY", raising=False)
    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda *a, **kw: True)
    monkeypatch.setattr(walt_bot, "_update_regime_state", lambda *a, **kw: ("GEAR_1_CONSERVATIVE_120M", 120, "ok"))
    monkeypatch.setattr(walt_bot, "check_daily_circuit_breaker", lambda *a, **kw: (False, "OK"))
    monkeypatch.setattr(walt_bot, "check_cold_state_entry_gate", lambda *a, **kw: (True, "OK"))
    monkeypatch.setattr(walt_bot, "_is_asset_in_cooldown", lambda *a, **kw: (False, 0.0))
    monkeypatch.setattr(walt_bot, "check_entry_frequency_limits", lambda *a, **kw: (True, "OK"))
    monkeypatch.setattr(walt_bot, "resolve_order_margin", lambda *a, **kw: (2.5, 100.0))
    monkeypatch.setattr(walt_bot, "_record_open_position_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "record_entry_execution", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "send_telegram", lambda *a, **kw: True)

    research_calls = []
    monkeypatch.setattr(walt_bot, "research_stock", lambda asset, **kw: research_calls.append(asset))

    opened_assets = []

    def fake_open(client, opp, **kw):
        opened_assets.append(opp.base_asset)
        return {"ok": True, "actual_margin": 2.5, "price": 100.0, "tp": 97.5, "sl": 101.5, "order": {"order_id": "ord_1"}}

    monkeypatch.setattr(walt_bot, "open_position", fake_open)

    import sheet_logger
    monkeypatch.setattr(sheet_logger, "fetch_latest_research_from_sheet", lambda **kw: {})
    monkeypatch.setattr(sheet_logger, "log_execution", lambda *a, **kw: {"executions_count": 1})

    client = MagicMock()
    client.balance.return_value = {"accounts": [{"balance_available": "20.0", "currency": "USDC"}]}
    client.list_positions.return_value = {"positions": []}
    client.list_contracts.return_value = {
        "contracts": [
            {
                "contract_id": 10,
                "base_asset": "NVDA",  # US stock -> harus SKIP di luar jam pasar AS
                "volume_24h_usd": "50000000",
                "funding_rate_hourly": "0.0006",
                "max_leverage": 2,
                "min_size_base": "0.01",
                "price_slippage": "0.0002",
            },
            {
                "contract_id": 11,
                "base_asset": "XAU",   # Komoditas -> tetap jalan 24/7
                "volume_24h_usd": "50000000",
                "funding_rate_hourly": "0.0005",
                "max_leverage": 2,
                "min_size_base": "0.01",
                "price_slippage": "0.0002",
            },
        ]
    }

    sunday_utc = datetime(2026, 11, 22, 15, 0, tzinfo=timezone.utc)
    res = walt_bot.market_execute(client, dry_run=False, now_utc=sunday_utc)

    assert "NVDA" not in opened_assets
    assert "NVDA" not in research_calls
    assert "XAU" in opened_assets
    nvda_exec = [e for e in res["executions"] if e["asset"] == "NVDA"]
    assert len(nvda_exec) == 1
    assert nvda_exec[0]["verdict"] == "SKIP"
    assert "Saham AS" in nvda_exec[0]["reason"]


def test_scan_with_research_skips_us_stocks_outside_hours_keeps_crypto(monkeypatch):
    monkeypatch.delenv("US_STOCK_HOURS_ONLY", raising=False)
    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda *a, **kw: True)
    monkeypatch.setattr(walt_bot, "_update_regime_state", lambda *a, **kw: ("GEAR_1_CONSERVATIVE_120M", 120, "ok"))
    monkeypatch.setattr(walt_bot, "check_daily_circuit_breaker", lambda *a, **kw: (False, "OK"))
    monkeypatch.setattr(walt_bot, "check_cold_state_entry_gate", lambda *a, **kw: (True, "OK"))
    monkeypatch.setattr(walt_bot, "_is_asset_in_cooldown", lambda *a, **kw: (False, 0.0))
    monkeypatch.setattr(walt_bot, "check_entry_frequency_limits", lambda *a, **kw: (True, "OK"))
    monkeypatch.setattr(walt_bot, "resolve_order_margin", lambda *a, **kw: (2.5, 100.0))
    monkeypatch.setattr(walt_bot, "_record_open_position_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "record_entry_execution", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "send_telegram", lambda *a, **kw: True)

    research_calls = []
    monkeypatch.setattr(walt_bot, "research_stock", lambda asset, **kw: research_calls.append(asset))

    opened_assets = []

    def fake_open(client, opp, **kw):
        opened_assets.append(opp.base_asset)
        return {"ok": True, "actual_margin": 2.5, "price": 100.0, "tp": 98.0, "sl": 101.5, "order": {"order_id": "ord_2"}}

    monkeypatch.setattr(walt_bot, "open_position", fake_open)

    import sheet_logger
    monkeypatch.setattr(sheet_logger, "get_closed_funding_current_month", lambda **kw: 0.0)
    monkeypatch.setattr(sheet_logger, "log_scan", lambda *a, **kw: {"rows_count": 1})
    monkeypatch.setattr(sheet_logger, "log_pnl_tracker", lambda *a, **kw: {"logged": False})
    monkeypatch.setattr(sheet_logger, "log_execution", lambda *a, **kw: {"executions_count": 1})

    client = MagicMock()
    client.readiness.return_value = {"ok": True}
    client.balance.return_value = {"accounts": [{"balance_available": "20.0", "currency": "USDC"}]}
    client.list_positions.return_value = {"positions": []}
    client.latest_rate.return_value = {"rate": "100.0"}
    client.list_contracts.return_value = {
        "contracts": [
            {
                "contract_id": 20,
                "base_asset": "NVDA",
                "volume_24h_usd": "50000000",
                "funding_rate_hourly": "0.0006",
                "max_leverage": 2,
                "min_size_base": "0.01",
                "price_slippage": "0.0002",
            },
            {
                "contract_id": 21,
                "base_asset": "SOL",
                "volume_24h_usd": "50000000",
                "funding_rate_hourly": "-0.0005",
                "max_leverage": 2,
                "min_size_base": "0.01",
                "price_slippage": "0.0002",
            },
        ]
    }

    tuesday_off_hours = datetime(2026, 11, 24, 0, 17, tzinfo=timezone.utc)
    res = walt_bot.scan_with_research(client, auto_execute=True, dry_run=False, now_utc=tuesday_off_hours)

    assert res["summary"]["us_stock_market_open"] is False
    assert [o["asset"] for o in res["summary"]["top_opportunities"]] == ["SOL"]
    assert "NVDA" not in research_calls
    assert "NVDA" not in opened_assets
    assert "SOL" in opened_assets
