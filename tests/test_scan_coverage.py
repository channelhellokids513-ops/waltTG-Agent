"""Penajaman cakupan pemeriksaan: seluruh ticker Walt tersentuh (scan cap, riset cap, near-miss)."""
import json

import walt_bot


def _set_cfg(monkeypatch, **kwargs):
    monkeypatch.setenv("STRATEGY_CONFIG_JSON", json.dumps(kwargs))


def test_crypto_scan_uses_configurable_max_candidates(monkeypatch):
    captured = {}

    def fake_scan(contracts, **kw):
        captured.update(kw)
        return []

    import price_forecast
    monkeypatch.setattr(price_forecast, "scan_crypto_price_opportunities", fake_scan)
    monkeypatch.setenv("CRYPTO_FORECAST_ENGINE", "on")
    _set_cfg(monkeypatch, forecast_max_scan_assets=0)

    walt_bot._scan_crypto_engine2_opportunities([], set(), set())
    assert captured.get("max_candidates") == 0  # 0 = SELURUH kontrak likuid

    _set_cfg(monkeypatch, forecast_max_scan_assets=120)
    walt_bot._scan_crypto_engine2_opportunities([], set(), set())
    assert captured.get("max_candidates") == 120


def test_stock_scan_uses_configurable_max_signals(monkeypatch):
    captured = {}

    def fake_scan(contracts, **kw):
        captured.update(kw)
        return []

    import price_forecast
    monkeypatch.setattr(price_forecast, "scan_stock_price_opportunities", fake_scan)
    monkeypatch.setenv("STOCK_FORECAST_ENGINE", "on")
    _set_cfg(monkeypatch, stock_forecast_max_signals=5)

    walt_bot._scan_stock_engine2_opportunities([], {"NVDA"}, set())
    assert captured.get("max_signals") == 5


def test_crypto_research_cap_from_config(monkeypatch):
    allowed = ["BTC", "ETH", "SOL", "BNB", "XRP", "AVAX", "SUI", "LINK", "NEAR", "AAVE"]
    pairs = [(a, 0.01) for a in allowed]
    _set_cfg(monkeypatch, crypto_research_top_n=8)
    assert len(walt_bot.crypto_research_set(pairs)) == 8
    _set_cfg(monkeypatch, crypto_research_top_n=3)
    assert len(walt_bot.crypto_research_set(pairs)) == 3


def test_engine1_near_miss_outside_whitelist_is_reported(monkeypatch, caplog):
    import logging as _logging
    contracts = [
        # di luar whitelist, lolos filter funding + volume -> WAJIB muncul di log
        {"contract_id": 900, "base_asset": "OBSCURE", "volume_24h_usd": "5000000",
         "funding_rate_hourly": "0.001", "max_leverage": 2},   # 2.4%/hari
        # di luar whitelist tetapi volume kecil -> tidak dilaporkan
        {"contract_id": 901, "base_asset": "TINY", "volume_24h_usd": "1000",
         "funding_rate_hourly": "0.001", "max_leverage": 2},
    ]
    with caplog.at_level(_logging.INFO, logger="walt_bot"):
        walt_bot.rank_opportunities(contracts, allowed_assets={"BTC"})
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "DI LUAR whitelist" in msgs
    assert "OBSCURE" in msgs
    assert "TINY" not in msgs


def test_rank_opportunities_still_enforces_whitelist_entries():
    contracts = [
        {"contract_id": 1, "base_asset": "BTC", "volume_24h_usd": "50000000",
         "funding_rate_hourly": "0.001", "max_leverage": 2, "min_size_base": "0.001"},
        {"contract_id": 900, "base_asset": "OBSCURE", "volume_24h_usd": "5000000",
         "funding_rate_hourly": "0.001", "max_leverage": 2},
    ]
    opps = walt_bot.rank_opportunities(contracts)  # whitelist penuh
    assert [o.base_asset for o in opps] == ["BTC"]