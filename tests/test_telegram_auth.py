from pathlib import Path
from unittest.mock import MagicMock

import pytest

from market_chart import analyze_market_chart, compute_ema, compute_rsi, get_actual_price_sources, make_sparkline
from strategy_config import load_strategy_config, reset_strategy_config, update_strategy_param
from telegram_auth import AuthManager
from telegram_bot import WaltTelegramBot


def test_unknown_user_is_blocked_and_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111111")
    monkeypatch.setenv("REQUIRE_GOOGLE_AUTH", "false")
    auth = AuthManager(auth_file=tmp_path / "auth.json", audit_file=tmp_path / "audit.jsonl")

    allowed, user, reason = auth.authorize(999999, "view_finance")
    assert allowed is False
    assert user is None
    assert "belum terdaftar" in reason


def test_rbac_permissions_for_admin_planner_end_user(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1001")
    monkeypatch.setenv("REQUIRE_GOOGLE_AUTH", "false")
    auth = AuthManager(auth_file=tmp_path / "auth.json", audit_file=tmp_path / "audit.jsonl")

    auth.add_or_update_user("advisor@example.com", "planner", telegram_id=2002)
    auth.add_or_update_user("viewer@example.com", "end_user", telegram_id=3003)

    # Admin: full access
    assert auth.authorize(1001, "manage_users")[0] is True
    assert auth.authorize(1001, "trade_control")[0] is True
    assert auth.authorize(1001, "edit_strategy")[0] is True

    # Planner (Financial Advisor): boleh edit strategi & lihat keuangan, tidak boleh manage users / trade
    assert auth.authorize(2002, "view_finance")[0] is True
    assert auth.authorize(2002, "view_chart")[0] is True
    assert auth.authorize(2002, "edit_strategy")[0] is True
    assert auth.authorize(2002, "manage_users")[0] is False
    assert auth.authorize(2002, "trade_control")[0] is False

    # End User: hanya baca
    assert auth.authorize(3003, "view_finance")[0] is True
    assert auth.authorize(3003, "view_chart")[0] is True
    assert auth.authorize(3003, "view_strategy")[0] is True
    assert auth.authorize(3003, "edit_strategy")[0] is False
    assert auth.authorize(3003, "manage_users")[0] is False
    assert auth.authorize(3003, "trade_control")[0] is False


def test_google_oauth_bind_and_strict_mode_and_logout(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1001")
    monkeypatch.setenv("REQUIRE_GOOGLE_AUTH", "true")
    auth = AuthManager(auth_file=tmp_path / "auth.json", audit_file=tmp_path / "audit.jsonl")

    # Dalam strict mode, bootstrap_owner belum dianggap google_oauth sampai /login
    assert auth.authorize(1001, "view_finance")[0] is False

    # Simulasikan user whitelist login via Google OAuth
    auth.add_or_update_user("rizal@gmail.com", "admin")
    status, rec, _ = auth._bind_google_email(telegram_id=5555, chat_id=5555, email="rizal@gmail.com")
    assert status == "success"
    assert rec["role"] == "admin"
    assert auth.authorize(5555, "manage_users")[0] is True

    # Logout mencabut sesi
    assert auth.logout(5555) is True
    assert auth.authorize(5555, "view_finance")[0] is False


def test_bot_notifies_admin_on_unauthorized_intrusion(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1001")
    monkeypatch.setenv("REQUIRE_GOOGLE_AUTH", "false")
    auth = AuthManager(auth_file=tmp_path / "auth.json", audit_file=tmp_path / "audit.jsonl")
    api = MagicMock()
    bot = WaltTelegramBot(api=api, auth=auth)

    bot.handle_text(888888, 888888, "stranger", "/finance")

    sent_targets = [call.args[0] for call in api.send_message.call_args_list]
    assert 1001 in sent_targets
    assert 888888 in sent_targets
    assert any("Peringatan Keamanan" in call.args[1] for call in api.send_message.call_args_list)


def test_strategy_config_validation_and_reset(tmp_path):
    cfg_path = tmp_path / "strategy_config.json"
    cfg = update_strategy_param("default_sl_pct", "2.0%", updated_by="rizal", config_path=cfg_path)
    assert cfg["default_sl_pct"] == 0.02

    with pytest.raises(ValueError):
        update_strategy_param("default_sl_pct", "0.50", config_path=cfg_path)  # > max 5%

    reset = reset_strategy_config(config_path=cfg_path)
    assert reset["default_sl_pct"] == 0.015
    assert load_strategy_config(cfg_path)["tp_calm_pct"] == 0.020


def test_market_chart_indicators_and_sources():
    prices = [100 + i * 0.5 for i in range(30)]
    assert len(make_sparkline(prices, width=20)) == 20
    assert compute_ema(prices, 20)[-1] > 100
    assert compute_rsi(prices, 14) == 100.0
    assert "hyperliquid.xyz/trade/BTC" in get_actual_price_sources("BTC")
    assert "finance.yahoo.com/quote/GC%3DF" in get_actual_price_sources("XAU")
