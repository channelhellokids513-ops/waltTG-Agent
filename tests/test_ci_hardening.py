import logging
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import strategy_config
import walt_bot


def test_w3_job_time_guard_skips_subcycle_and_adaptive_execute(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("JOB_TIMEOUT_MIN", "20")
    # Simulasikan job sudah berjalan 15 menit (sisa 5 menit < 6 menit)
    assert walt_bot.has_enough_job_time(start_mono=0.0, now_mono=15.0 * 60.0) is False
    assert walt_bot.has_enough_job_time(start_mono=0.0, now_mono=10.0 * 60.0) is True

    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda *a, **kw: False)
    sleep_called = []
    adaptive_called = []
    monkeypatch.setattr(walt_bot, "_maybe_run_adaptive_executions", lambda *a, **kw: adaptive_called.append(True) or [])

    dummy_pos = MagicMock()
    dummy_pos.contract_id = 1
    dummy_pos.base_asset = "SPY"
    dummy_pos.side = "long"
    dummy_pos.entry_price = 100.0
    dummy_pos.current_price = 100.0
    dummy_pos.profit_pct_price = 0.0
    dummy_pos.tp = 102.0
    dummy_pos.sl = 98.5
    monkeypatch.setattr(
        walt_bot,
        "_run_single_monitor_pass",
        lambda *a, **kw: ([dummy_pos], {"1": {"verdict": "HALF"}}, [], 1),
    )

    with caplog.at_level(logging.WARNING, logger="walt-bot"):
        res = walt_bot.risk_monitor_run(
            MagicMock(),
            snapshot_path=str(tmp_path / "snap.json"),
            enable_dynamic_subcycle=True,
            enable_adaptive_execute=True,
            sleep_fn=lambda s: sleep_called.append(s),
        )

    assert res["cycles_run"] == 1
    assert sleep_called == []
    assert adaptive_called == []
    assert any("skip: waktu job hampir habis" in r.message for r in caplog.records)


def test_w5_log_redact_hides_balance_margin_pnl_and_order_id(monkeypatch):
    monkeypatch.setenv("LOG_REDACT", "true")
    raw = (
        "Balance available: $10.37 | Equity $15.43 (Available $10.37) | "
        "margin $2.52 | PnL $+0.45 | order_id=01a1f2df-2d85-7341 | user@example.com"
    )
    redacted = walt_bot.redact_sensitive_text(raw)
    assert "$10.37" not in redacted
    assert "$15.43" not in redacted
    assert "$2.52" not in redacted
    assert "$+0.45" not in redacted
    assert "01a1f2df-2d85-7341" not in redacted
    assert "user@example.com" not in redacted
    assert "***" in redacted

    # Formatter mengikuti LOG_REDACT=true
    fmt = walt_bot._RedactingFormatter("%(message)s")
    rec = logging.LogRecord("walt-bot", logging.INFO, "", 0, "Balance available: $99.99", (), None)
    assert "$99.99" not in fmt.format(rec)
    assert "***" in fmt.format(rec)

    # Ringkasan stdout aman tidak membocorkan balance/order_id/margin
    summary = walt_bot.build_safe_stdout_summary(
        "monitor",
        {
            "positions_checked": 3,
            "cycles_run": 2,
            "cadence_mode": "dynamic_5m",
            "balance": [{"balance_available": "10.37"}],
            "total_equity": 15.43,
            "actions": [{"asset": "SPY", "action": "UPDATE_AUTO_CLOSE"}],
        },
    )
    assert "balance" not in summary
    assert "total_equity" not in summary
    assert summary["actions_count"] == 1
    assert summary["cadence_mode"] == "dynamic_5m"


def test_s1_shared_strategy_config_loader_fallback_and_bounds(monkeypatch, tmp_path):
    missing_local = tmp_path / "missing.json"

    # 1. Tidak tersedia -> fallback ke DEFAULT_STRATEGY_CONFIG
    monkeypatch.delenv("STRATEGY_CONFIG_JSON", raising=False)
    cfg_def = strategy_config.load_strategy_config(config_path=missing_local)
    assert cfg_def["source"] == "default"
    assert cfg_def["default_sl_pct"] == 0.015

    # 2. JSON rusak -> fallback aman ke default
    monkeypatch.setenv("STRATEGY_CONFIG_JSON", "{invalid json")
    cfg_bad = strategy_config.load_strategy_config(config_path=missing_local)
    assert cfg_bad["source"] == "default"
    assert cfg_bad["default_sl_pct"] == 0.015

    # 3. Sumber bersama valid + satu nilai di luar batas -> nilai valid dipakai, nilai ekstrem ditolak ke default
    monkeypatch.setenv(
        "STRATEGY_CONFIG_JSON",
        '{"default_sl_pct": 0.02, "tp_calm_pct": 0.99, "max_slots_cap": 999}',
    )
    cfg_shared = strategy_config.load_strategy_config(config_path=missing_local)
    assert cfg_shared["source"] == "github_variable"
    assert cfg_shared["default_sl_pct"] == 0.02
    assert cfg_shared["tp_calm_pct"] == 0.020   # ditolak (> 0.10), kembali ke default
    assert cfg_shared["max_slots_cap"] == 15    # ditolak (> 15), kembali ke default

    # 4. Validasi strict menolak nilai di luar batas
    with pytest.raises(ValueError):
        strategy_config.validate_strategy_dict({"default_sl_pct": 0.50}, strict=True)

    # 5. Serialisasi shared config tidak membocorkan ID Telegram atau email
    serialized = strategy_config.serialize_shared_strategy_config(
        {**strategy_config.DEFAULT_STRATEGY_CONFIG, "updated_by": "admin@example.com", "telegram_id": 123456}
    )
    assert "admin@example.com" not in serialized
    assert "123456" not in serialized
