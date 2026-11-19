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


def test_p0_1_time_guard_skips_candidates_with_partial_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda *a, **kw: False)
    monkeypatch.setattr(walt_bot, "get_job_remaining_minutes", lambda *a, **kw: 2.5)
    open_calls = []
    tg_calls = []
    monkeypatch.setattr(walt_bot, "open_position", lambda *a, **kw: open_calls.append(a))
    monkeypatch.setattr(walt_bot, "send_telegram", lambda msg, **kw: tg_calls.append(msg))
    monkeypatch.setattr(walt_bot, "_update_regime_state", lambda *a, **kw: ("GEAR_1_CONSERVATIVE_120M", 120, "ok"))
    monkeypatch.setattr(walt_bot, "check_daily_circuit_breaker", lambda *a, **kw: (False, "OK"))
    monkeypatch.setattr(walt_bot, "_is_asset_in_cooldown", lambda *a, **kw: (False, 0.0))
    monkeypatch.setattr(walt_bot, "check_entry_frequency_limits", lambda *a, **kw: (True, "OK"))
    monkeypatch.setattr(walt_bot, "resolve_order_margin", lambda *a, **kw: (2.5, 100.0))

    import sheet_logger
    monkeypatch.setattr(sheet_logger, "fetch_latest_research_from_sheet", lambda **kw: {})
    monkeypatch.setattr(sheet_logger, "log_execution", lambda *a, **kw: {"executions_count": 0})

    client = MagicMock()
    client.balance.return_value = {"accounts": [{"balance_available": "15.0"}]}
    client.list_contracts.return_value = {
        "contracts": [{
            "contract_id": 1,
            "base_asset": "XAU",
            "volume_24h_usd": "50000000",
            "funding_rate_hourly": "0.0005",
            "max_leverage": 2,
            "min_size_base": "0.01",
            "price_slippage": "0.0002",
        }]
    }
    client.list_positions.return_value = {"positions": []}

    res = walt_bot.market_execute(client, dry_run=False)
    assert open_calls == []
    assert res.get("status") == "partial_timeout"
    assert "XAU" in (res.get("skipped") or [])
    assert any("Sisa waktu job tinggal" in m for m in tg_calls)


def test_p0_1_open_position_always_attempts_autoclose_after_order_filled(monkeypatch):
    monkeypatch.setattr(walt_bot.time, "sleep", lambda s: None)
    monkeypatch.setattr(walt_bot, "resolve_order_margin", lambda *a, **kw: (2.5, 100.0))

    order_filled = {"done": False}
    monkeypatch.setattr(
        walt_bot,
        "has_enough_job_time",
        lambda *a, **kw: not order_filled["done"],
    )

    client = MagicMock()
    client.preview_order.return_value = {
        "ok": True,
        "confirmationToken": "tok_open",
        "action": {"request": {}},  # tanpa autoClose -> harus set terpisah
    }

    def fake_exec(tok):
        if tok == "tok_open":
            order_filled["done"] = True
            return {"ok": True, "order": {"order_id": "ord_1"}}
        return {"ok": True}

    client.execute.side_effect = fake_exec
    client.preview_auto_close.return_value = {"ok": True, "confirmationToken": "tok_ac"}

    opp = walt_bot.Opportunity(
        contract_id=1, base_asset="XAU", funding_daily=0.006,
        volume_24h=50_000_000, max_leverage=2, side="short",
        min_size_base=0.01, price_slippage=0.0002, min_margin_2x=1.0,
    )
    res = walt_bot.open_position(client, opp, margin=2.5, leverage=2)
    assert res is not None
    assert client.preview_auto_close.call_count >= 1
    assert hasattr(walt_bot, "AUTO_CLOSE_RETRY_BUDGET_MIN")

    # Jika waktu habis SEBELUM execute order -> order tidak dikirim
    client_low = MagicMock()
    client_low.preview_order.return_value = {"ok": True, "confirmationToken": "tok_low", "action": {"request": {}}}
    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda *a, **kw: False)
    monkeypatch.setattr(walt_bot, "get_job_remaining_minutes", lambda *a, **kw: 0.5)
    assert walt_bot.open_position(client_low, opp, margin=2.5, leverage=2) is None
    assert client_low.execute.call_count == 0


def test_p1_1_single_margin_resolution_per_order_and_over_buffer_flag(monkeypatch):
    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda *a, **kw: True)
    monkeypatch.setattr(walt_bot, "_update_regime_state", lambda *a, **kw: ("GEAR_1_CONSERVATIVE_120M", 120, "ok"))
    monkeypatch.setattr(walt_bot, "check_daily_circuit_breaker", lambda *a, **kw: (False, "OK"))
    monkeypatch.setattr(walt_bot, "_is_asset_in_cooldown", lambda *a, **kw: (False, 0.0))
    monkeypatch.setattr(walt_bot, "check_entry_frequency_limits", lambda *a, **kw: (True, "OK"))
    monkeypatch.setattr(walt_bot, "_record_open_position_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "record_entry_execution", lambda *a, **kw: None)

    tg_calls = []
    monkeypatch.setattr(walt_bot, "send_telegram", lambda msg, **kw: tg_calls.append(msg))

    import sheet_logger
    monkeypatch.setattr(sheet_logger, "fetch_latest_research_from_sheet", lambda **kw: {})
    monkeypatch.setattr(sheet_logger, "log_execution", lambda *a, **kw: {"executions_count": 1})

    prices = iter([100.0, 200.0, 300.0])
    rate_calls = []

    client = MagicMock()
    client.balance.return_value = {"accounts": [{"balance_available": "15.0"}]}
    client.list_contracts.return_value = {
        "contracts": [{
            "contract_id": 1,
            "base_asset": "XAU",
            "volume_24h_usd": "50000000",
            "funding_rate_hourly": "0.0005",
            "max_leverage": 2,
            "min_size_base": "0.01",
            "price_slippage": "0.0002",
        }]
    }
    client.list_positions.return_value = {"positions": []}

    def fake_latest_rate(cid):
        p = next(prices)
        rate_calls.append(p)
        return {"rate": str(p)}

    client.latest_rate.side_effect = fake_latest_rate
    client.preview_order.return_value = {
        "ok": True,
        "confirmationToken": "tok_open",
        "action": {"request": {"autoClose": {"tp": 97.5, "sl": 101.5}}},
    }
    client.execute.return_value = {"ok": True, "order": {"order_id": "ord_single"}}

    res = walt_bot.market_execute(client, dry_run=False)
    assert len(rate_calls) == 1, f"latest_rate harus dipanggil tepat 1x, aktual {len(rate_calls)}"
    assert any(e.get("verdict") == "EXECUTED" for e in res["executions"])

    # Kasus over_buffer: open_position mengembalikan actual_margin melebihi target_deploy
    tg_calls.clear()
    monkeypatch.setattr(walt_bot, "resolve_order_margin", lambda *a, **kw: (2.5, 100.0))
    monkeypatch.setattr(
        walt_bot,
        "open_position",
        lambda *a, **kw: {"ok": True, "actual_margin": 14.0, "price": 100.0, "tp": 97.5, "sl": 101.5, "order": {"order_id": "ord_ob"}},
    )
    res_ob = walt_bot.market_execute(client, dry_run=False)
    exec_entry = [e for e in res_ob["executions"] if e.get("verdict") == "EXECUTED"][0]
    assert exec_entry.get("over_buffer") is True
    assert any("Buffer kas terlampaui" in m for m in tg_calls)


def test_p1_2_sheet_webhook_retries_then_succeeds_and_fallback_csv(monkeypatch, tmp_path):
    import sheet_logger

    assert hasattr(sheet_logger, "_post_webhook"), "_post_webhook helper harus ada di sheet_logger"
    monkeypatch.setattr(sheet_logger.time, "sleep", lambda s: None)
    monkeypatch.setattr(sheet_logger, "FALLBACK_LOG_DIR", str(tmp_path))

    class _Resp:
        def __init__(self, code):
            self.status_code = code

    calls_ok = []
    codes = iter([500, 500, 200])

    def fake_post_retry(url, **kw):
        calls_ok.append((url, kw))
        return _Resp(next(codes))

    monkeypatch.setattr(sheet_logger.httpx, "post", fake_post_retry)
    payload = {
        "action": "log_close",
        "sheet": "Closed_Positions",
        "header": sheet_logger.CLOSED_POSITIONS_HEADERS,
        "rows": [["2026-11-19T00:00:00+00:00", "NVDA", "LONG", 1, 100.0, 102.0, 2.5, 0.1, 0.01, "TP", "p1", "", "v1", ""]],
    }
    ok = sheet_logger._post_webhook(payload, "https://script.google.com/macros/s/test/exec")
    assert ok is True
    assert len(calls_ok) == 3

    # Gagal total 3x -> False & tulis file CSV fallback di FALLBACK_LOG_DIR
    calls_fail = []

    def fake_post_fail(url, **kw):
        calls_fail.append(url)
        return _Resp(500)

    monkeypatch.setattr(sheet_logger.httpx, "post", fake_post_fail)
    ok_fail = sheet_logger._post_webhook(payload, "https://script.google.com/macros/s/test/exec")
    assert ok_fail is False
    assert len(calls_fail) == 3
    fallback_files = list(tmp_path.glob("*closed_positions*fallback*.csv"))
    assert len(fallback_files) == 1
    content = fallback_files[0].read_text(encoding="utf-8")
    assert "Asset" in content and "NVDA" in content


@pytest.mark.parametrize(
    "side,profit_pct,expected_sl",
    [
        ("long", 0.0079, None),
        ("short", 0.0079, None),
        ("long", 0.0080, 100.35),
        ("short", 0.0080, 99.65),
        ("long", 0.0139, 100.35),
        ("short", 0.0139, 99.65),
        ("long", 0.0140, 100.85),
        ("short", 0.0140, 99.15),
        ("long", 0.0200, round(102.0 * 0.9940, 4)),
        ("short", 0.0200, round(98.0 * 1.0060, 4)),
        ("long", 0.0250, round(102.5 * 0.9940, 4)),
        ("short", 0.0250, round(97.5 * 1.0060, 4)),
    ],
)
def test_p2_1_unified_trailing_sl_12_cases(side, profit_pct, expected_sl):
    entry = 100.0
    current = round(entry * (1.0 + profit_pct), 6) if side == "long" else round(entry * (1.0 - profit_pct), 6)
    pos = _pos(side=side, entry=entry, current=current, tp=105.0 if side == "long" else 95.0, sl=98.5 if side == "long" else 101.5)
    resolved = walt_bot._resolve_trailing_sl(pos, profit_pct)
    if expected_sl is None:
        assert resolved is None
    else:
        assert resolved is not None
        sl_price, desc = resolved
        assert sl_price == pytest.approx(expected_sl, abs=1e-4)
        assert "L4" in desc






