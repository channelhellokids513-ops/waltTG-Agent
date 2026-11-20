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


@pytest.mark.parametrize(
    "side,current",
    [
        ("long", 102.0),   # long profit
        ("long", 98.0),    # long loss
        ("short", 98.0),   # short profit
        ("short", 102.0),  # short loss
    ],
)
def test_r4_2_snapshot_pnl_equivalence_long_short_profit_loss(monkeypatch, side, current):
    import sheet_logger

    logged = []
    monkeypatch.setattr(sheet_logger, "log_close", lambda **kw: logged.append(kw))
    monkeypatch.setattr(walt_bot, "_set_asset_cooldown", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "_record_closed_id", lambda *a, **kw: None)

    pos = _pos(side=side, entry=100.0, current=current)
    walt_bot._record_position_close(pos, reason="TP breached", exit_price=pos.current_price)

    assert len(logged) == 1
    expected_pnl = pos.pnl_pct_margin * pos.margin
    assert logged[0]["pnl_quote"] == pytest.approx(expected_pnl, abs=1e-9)
    assert logged[0]["exit_price"] == pytest.approx(round(current, 4), abs=1e-6)


def test_r4_2_fill_price_computes_consistent_pnl_and_round4(monkeypatch):
    from unittest.mock import MagicMock
    import sheet_logger

    logged = []
    monkeypatch.setattr(sheet_logger, "log_close", lambda **kw: logged.append(kw))
    monkeypatch.setattr(walt_bot, "_set_asset_cooldown", lambda *a, **kw: None)
    monkeypatch.setattr(walt_bot, "_record_closed_id", lambda *a, **kw: None)

    client = MagicMock()
    client.execute.return_value = {"ok": True, "order": {"price": "97.654321"}}
    pos = _pos(side="short", entry=100.0, current=98.5)
    walt_bot._execute_validated_close(
        client, pos, {"ok": True, "confirmationToken": "tok_f"}, reason="TP breached"
    )
    assert len(logged) == 1
    fill_rounded = round(97.654321, 4)
    expected_move = (pos.entry_price - fill_rounded) / pos.entry_price
    expected_pnl = expected_move * pos.leverage * pos.margin
    assert logged[0]["exit_price"] == pytest.approx(fill_rounded, abs=1e-6)
    assert logged[0]["pnl_quote"] == pytest.approx(expected_pnl, abs=1e-9)


def test_r4_2_mark_price_is_labeled_mark_price_proxy():
    pos = _pos(side="long", entry=100.0, current=101.0)
    price, src = walt_bot._extract_close_exit_price({"position": {"mark_price": "101.5"}}, pos)
    assert price == pytest.approx(101.5, abs=1e-6)
    assert src == "mark_price_proxy"


def test_r4_2_entry_fallback_is_labeled_entry_fallback():
    pos = _pos(side="long", entry=100.0, current=0.0)
    price, src = walt_bot._extract_close_exit_price({}, pos)
    assert price == pytest.approx(100.0, abs=1e-6)
    assert src == "entry_fallback"


def test_r4_2_sanity_guard_rejects_extreme_fill_price_and_falls_back_to_snapshot():
    pos = _pos(side="long", entry=100.0, current=101.25)
    # 200.0 menyimpang 100% dari entry (>= 50%) -> harus ditolak sanity guard, jatuh ke snapshot
    price, src = walt_bot._extract_close_exit_price({"order": {"price": "200.0"}}, pos)
    assert price == pytest.approx(101.25, abs=1e-6)
    assert src == "snapshot_estimate"

    # 30.0 menyimpang 70% dari entry (>= 50%) -> ditolak sanity guard, jatuh ke snapshot
    price2, src2 = walt_bot._extract_close_exit_price({"position": {"avg_close_price": "30.0"}}, pos)
    assert price2 == pytest.approx(101.25, abs=1e-6)
    assert src2 == "snapshot_estimate"


def test_r4_2_unverified_price_keys_quarantined_in_module_constants():
    assert hasattr(walt_bot, "_CLOSE_FILL_PRICE_KEYS")
    assert hasattr(walt_bot, "_CLOSE_POSITION_PRICE_KEYS")
    assert hasattr(walt_bot, "_CLOSE_MARK_PRICE_KEYS")
    assert "mark_price" not in walt_bot._CLOSE_POSITION_PRICE_KEYS
    assert "mark_price" in walt_bot._CLOSE_MARK_PRICE_KEYS


def _make_scan_contracts(n: int) -> list[dict]:
    assets = sorted(walt_bot.ALLOWED_ASSETS)[:n]
    return [
        {
            "contract_id": idx + 1,
            "base_asset": asset,
            "volume_24h_usd": "50000000",
            "funding_rate_hourly": "0.0005",
            "max_leverage": 2,
            "min_size_base": "0.01",
            "price_slippage": "0.0002",
        }
        for idx, asset in enumerate(assets)
    ]


def test_r4_3_per_candidate_failures_are_aggregated(monkeypatch, caplog):
    from unittest.mock import MagicMock
    import sheet_logger

    tg_calls = []
    monkeypatch.setattr(walt_bot, "send_telegram", lambda msg, **kw: tg_calls.append(msg))
    monkeypatch.setattr(walt_bot, "get_dynamic_max_slots", lambda eq: 25)
    monkeypatch.setattr(walt_bot, "_update_regime_state", lambda *a, **kw: ("GEAR_1", 120, "ok"))
    monkeypatch.setattr(sheet_logger, "get_closed_funding_current_month", lambda **kw: 0.0)

    client = MagicMock()
    client.readiness.return_value = {"ok": True}
    client.balance.return_value = {"accounts": [{"balance_available": "200.0"}]}
    client.list_positions.return_value = {"positions": []}
    client.list_contracts.return_value = {"contracts": _make_scan_contracts(25)}
    client.latest_rate.side_effect = RuntimeError("timeout upstream")

    with caplog.at_level("DEBUG", logger="walt-bot"):
        res = walt_bot.daily_scan(client)

    assert len(res["top_opportunities"]) == 25
    assert all(o["price"] == 0.0 for o in res["top_opportunities"])

    warn_recs = [r for r in caplog.records if r.levelname == "WARNING"]
    debug_rate_recs = [r for r in caplog.records if r.levelname == "DEBUG" and "latest_rate" in r.message]
    assert len(warn_recs) == 1, f"Harus tepat 1 WARNING agregasi, aktual {len(warn_recs)}"
    assert len(debug_rate_recs) == 25
    assert "25" in warn_recs[0].message
    assert "..." in warn_recs[0].message
    # Maksimal 5 nama aset di pesan ringkasan
    sample_part = warn_recs[0].message.split(":", 1)[-1]
    assert len([x for x in sample_part.split(",") if x.strip() and x.strip() != "..."]) <= 5
    assert len(tg_calls) in (0, 1)
    assert len(tg_calls) == 1


def test_r4_3_minor_failures_do_not_alert(monkeypatch, caplog):
    from unittest.mock import MagicMock
    import sheet_logger

    tg_calls = []
    monkeypatch.setattr(walt_bot, "send_telegram", lambda msg, **kw: tg_calls.append(msg))
    monkeypatch.setattr(walt_bot, "get_dynamic_max_slots", lambda eq: 20)
    monkeypatch.setattr(walt_bot, "_update_regime_state", lambda *a, **kw: ("GEAR_1", 120, "ok"))
    monkeypatch.setattr(sheet_logger, "get_closed_funding_current_month", lambda **kw: 0.0)

    client = MagicMock()
    client.readiness.return_value = {"ok": True}
    client.balance.return_value = {"accounts": [{"balance_available": "200.0"}]}
    client.list_positions.return_value = {"positions": []}
    contracts = _make_scan_contracts(20)
    client.list_contracts.return_value = {"contracts": contracts}
    fail_markets = {f"{contracts[0]['base_asset']}-USD-PERP", f"{contracts[1]['base_asset']}-USD-PERP"}

    def fake_rate(market_id):
        if market_id in fail_markets:
            raise RuntimeError("transient error")
        return {"rate": "100.0"}

    client.latest_rate.side_effect = fake_rate

    with caplog.at_level("DEBUG", logger="walt-bot"):
        walt_bot.daily_scan(client)

    warn_recs = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warn_recs) == 1
    assert "2/20" in warn_recs[0].message
    assert tg_calls == []


def test_r4_4_api_touching_workflows_are_maintenance_gated():
    from pathlib import Path
    import yaml

    wf_dir = Path(__file__).resolve().parent.parent / ".github" / "workflows"
    api_workflows = [
        "daily-scan.yml",
        "market-execute.yml",
        "crypto-execute.yml",
        "risk-monitor.yml",
    ]
    for fname in api_workflows:
        raw = (wf_dir / fname).read_text(encoding="utf-8")
        doc = yaml.safe_load(raw)
        jobs = doc.get("jobs") or {}
        assert jobs, f"{fname} tidak memiliki jobs"
        for job_name, job_cfg in jobs.items():
            cond = str(job_cfg.get("if", ""))
            assert "MAINTENANCE_MODE" in cond, f"{fname} job '{job_name}' wajib memiliki guard MAINTENANCE_MODE"

    # tests.yml TIDAK boleh di-gate oleh MAINTENANCE_MODE
    tests_raw = (wf_dir / "tests.yml").read_text(encoding="utf-8")
    assert "MAINTENANCE_MODE" not in tests_raw

    # watchdog.yml wajib berhenti sebelum alert/dispatch saat MAINTENANCE_MODE aktif.
    # Ronde 6 [R6-3] memindahkan logika watchdog dari heredoc inline ke tools/watchdog_check.py agar
    # bisa diuji; invarian yang dijaga test ini TIDAK berubah, hanya lokatornya ikut pindah, dan
    # diperiksa lebih ketat: urutan DIEKSEKUSI di dalam main(), bukan posisi teks di YAML.
    watchdog_raw = (wf_dir / "watchdog.yml").read_text(encoding="utf-8")
    watchdog_mod = (wf_dir.parent.parent / "tools" / "watchdog_check.py").read_text(encoding="utf-8")
    assert "MAINTENANCE_MODE" in watchdog_raw, "watchdog.yml harus meneruskan MAINTENANCE_MODE"
    assert "tools/watchdog_check.py" in watchdog_raw, "watchdog.yml harus memakai modul teruji"

    assert "MAINTENANCE_MODE" in watchdog_mod
    assert "sendMessage" in watchdog_mod, "jalur alert Telegram harus tetap ada"
    assert '"workflow", "run"' in watchdog_mod, "jalur fallback dispatch harus tetap ada"

    main_body = watchdog_mod[watchdog_mod.find("def main("):]
    maint_pos = main_body.find("MAINTENANCE_MODE")
    alert_pos = main_body.find("send_alert(")
    dispatch_pos = main_body.find("dispatch_workflow(")
    assert maint_pos != -1 and alert_pos != -1 and dispatch_pos != -1
    assert maint_pos < alert_pos and maint_pos < dispatch_pos, (
        "MAINTENANCE_MODE harus diperiksa sebelum alert maupun dispatch"
    )

    # diagnostics.yml ada, read-only (hanya workflow_dispatch), tanpa job-level if MAINTENANCE_MODE
    diag_path = wf_dir / "diagnostics.yml"
    assert diag_path.exists(), "diagnostics.yml harus ada"
    diag_doc = yaml.safe_load(diag_path.read_text(encoding="utf-8"))
    on_block = diag_doc.get("on") or diag_doc.get(True) or {}
    assert "workflow_dispatch" in on_block and "schedule" not in on_block
    for job_cfg in (diag_doc.get("jobs") or {}).values():
        assert "MAINTENANCE_MODE" not in str(job_cfg.get("if", ""))


def test_r4_4_invalid_grant_sends_one_actionable_alert_then_raises(monkeypatch):
    import httpx
    import notifier
    import token_manager

    tg_calls = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg, **kw: tg_calls.append(msg))

    secret_rt = "rt_super_secret_should_never_leak_999"
    req = httpx.Request("POST", token_manager.TOKEN_URL)
    resp = httpx.Response(
        400,
        json={"error": "invalid_grant", "error_description": "Refresh token is invalid or expired."},
        request=req,
    )
    monkeypatch.setattr(token_manager.httpx, "post", lambda *a, **kw: resp)

    tm = token_manager.TokenManager(client_id="cid_test", refresh_token=secret_rt)
    with pytest.raises(httpx.HTTPStatusError):
        tm._refresh()

    assert len(tg_calls) == 1
    alert = tg_calls[0]
    assert "invalid_grant" in alert.lower()
    assert "Jendela Pemeliharaan" in alert or "README" in alert
    assert secret_rt not in alert



