"""Guard anti-tabrak token single-use + ReplyKeyboard menu owner (tes tanpa jaringan)."""
import json
from unittest.mock import MagicMock

import telegram_bot as tb
from telegram_auth import AuthManager

OWNER = 1001


class _Resp:
    def __init__(self, runs, code=200):
        self._runs = runs
        self.status_code = code

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"workflow_runs": self._runs}


def _mk(tmp_path, monkeypatch, runs=None, api_error=False, pat="pat-x", repo="o/r"):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(OWNER))
    monkeypatch.setenv("REQUIRE_GOOGLE_AUTH", "false")
    if pat is not None:
        monkeypatch.setenv("GH_PAT", pat)
    else:
        monkeypatch.delenv("GH_PAT", raising=False)
        monkeypatch.delenv("GH_PATH", raising=False)
        monkeypatch.delenv("GITHUB_PAT", raising=False)
    if repo is not None:
        monkeypatch.setenv("GITHUB_REPOSITORY", repo)
    else:
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    def fake_get(url, **kw):
        if api_error:
            raise RuntimeError("network down")
        return _Resp(runs if runs is not None else [])

    monkeypatch.setattr(tb.httpx, "get", fake_get)
    auth = AuthManager(auth_file=tmp_path / "auth.json", audit_file=tmp_path / "audit.jsonl")
    api = MagicMock()
    bot = tb.WaltTelegramBot(api=api, auth=auth)
    return bot, api, auth


def _runs_daily_scan():
    return [{"path": "o/r/refs/heads/main/.github/workflows/daily-scan.yml",
             "status": "in_progress", "created_at": "2026-11-23T18:33:01Z"}]


def _audit_events(auth):
    if not auth.audit_file.exists():
        return []
    return [json.loads(l)["event"] for l in auth.audit_file.read_text(encoding="utf-8").splitlines()]


def test_helper_classifies_busy_idle_ci_and_error(tmp_path, monkeypatch):
    bot, _api, _auth = _mk(tmp_path, monkeypatch, runs=_runs_daily_scan())
    busy, detail = tb.is_trading_run_active()
    assert busy is True and "daily-scan" in detail

    bot, _api, _auth = _mk(tmp_path, monkeypatch, runs=[
        {"path": "o/r/.github/workflows/ci-tests.yml", "status": "in_progress", "created_at": "x"},
        {"path": "o/r/.github/workflows/watchdog.yml", "status": "queued", "created_at": "x"},
        {"path": "o/r/.github/workflows/risk-monitor.yml", "status": "completed", "created_at": "x"},
    ])
    assert tb.is_trading_run_active() == (False, "idle")


def test_finance_blocked_while_trading_run_active(tmp_path, monkeypatch):
    bot, api, auth = _mk(tmp_path, monkeypatch, runs=_runs_daily_scan())
    calls = []
    monkeypatch.setattr(tb.WaltTelegramBot, "_cmd_finance", lambda self, chat_id: calls.append(chat_id))

    bot.handle_text(OWNER, OWNER, "owner", "/finance")

    assert calls == []
    assert any("⏳" in c.args[1] and "daily-scan" in c.args[1] for c in api.send_message.call_args_list)
    assert "RUN_GUARD_BLOCKED" in _audit_events(auth)


def test_finance_proceeds_when_queue_idle(tmp_path, monkeypatch):
    bot, api, _auth = _mk(tmp_path, monkeypatch, runs=[])
    calls = []
    monkeypatch.setattr(tb.WaltTelegramBot, "_cmd_finance", lambda self, chat_id: calls.append(chat_id))

    bot.handle_text(OWNER, OWNER, "owner", "/finance")

    assert calls == [OWNER]
    assert not any("⏳" in c.args[1] for c in api.send_message.call_args_list)


def test_execute_and_close_callbacks_gated(tmp_path, monkeypatch):
    bot, api, _auth = _mk(tmp_path, monkeypatch, runs=[
        {"path": "o/r/.github/workflows/crypto-execute.yml", "status": "queued",
         "created_at": "2026-11-23T19:11:02Z"}])
    walts = MagicMock()
    monkeypatch.setattr(tb.walt_bot, "WaltClient", walts)

    bot.handle_update({
        "update_id": 5,
        "callback_query": {
            "id": "cb", "from": {"id": OWNER, "username": "owner"},
            "message": {"chat": {"id": OWNER}}, "data": "confirm_close:SPY",
        },
    })
    assert any("⏳" in c.args[1] for c in api.send_message.call_args_list)
    walts.assert_not_called()


def test_guard_fail_open_without_pat_or_api_error(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tb.WaltTelegramBot, "_cmd_positions", lambda self, chat_id: calls.append(chat_id))

    bot, api, _auth = _mk(tmp_path, monkeypatch, pat=None, repo=None)
    bot.handle_text(OWNER, OWNER, "owner", "/positions")
    assert calls == [OWNER]
    assert any("GH_PAT" in c.args[1] for c in api.send_message.call_args_list)

    calls2 = []
    monkeypatch.setattr(tb.WaltTelegramBot, "_cmd_positions", lambda self, chat_id: calls2.append(chat_id))
    bot2, _api2, auth2 = _mk(tmp_path, monkeypatch, api_error=True)
    bot2.handle_text(OWNER, OWNER, "owner", "/positions")
    assert calls2 == [OWNER]
    assert "RUN_GUARD_UNAVAILABLE" in _audit_events(auth2)


def test_keyboard_labels_route_to_commands(tmp_path, monkeypatch):
    bot, _api, _auth = _mk(tmp_path, monkeypatch, runs=[])
    finance_calls, strat_calls = [], []
    monkeypatch.setattr(tb.WaltTelegramBot, "_cmd_finance", lambda self, chat_id: finance_calls.append(chat_id))
    monkeypatch.setattr(tb.WaltTelegramBot, "_cmd_strategy", lambda self, chat_id, role: strat_calls.append(chat_id))

    bot.handle_text(OWNER, OWNER, "owner", "💰 Keuangan ⚡")
    assert finance_calls == [OWNER]

    bot.handle_text(OWNER, OWNER, "owner", "⚙️ Strategi Global")
    assert strat_calls == [OWNER]


def test_retry_button_replays_last_command(tmp_path, monkeypatch):
    bot, _api, _auth = _mk(tmp_path, monkeypatch, runs=[
        {"path": "o/r/.github/workflows/market-execute.yml", "status": "in_progress", "created_at": "x"}])
    finance_calls = []
    monkeypatch.setattr(tb.WaltTelegramBot, "_cmd_finance", lambda self, chat_id: finance_calls.append(chat_id))

    bot.handle_text(OWNER, OWNER, "owner", "/finance")
    assert finance_calls == []
    assert bot._last_cmd[OWNER] == "/finance"

    bot.handle_text(OWNER, OWNER, "owner", "🔁 Coba Lagi")
    assert finance_calls == []  # masih dijeda selama run aktif
    assert bot._last_cmd[OWNER] == "/finance"


def test_hide_keyboard_button(tmp_path, monkeypatch):
    bot, api, _auth = _mk(tmp_path, monkeypatch)

    bot.handle_text(OWNER, OWNER, "owner", "🧹 Tutup Menu")

    kw = api.send_message.call_args.kwargs
    assert kw.get("reply_markup", {}).get("remove_keyboard") is True


def test_main_keyboard_shape_and_mapping():
    kb = tb._build_main_keyboard()
    labels = [l for row in kb["keyboard"] for l in row]
    assert len(labels) == len(set(labels)), "label keyboard harus unik"
    assert all(l in tb._LABEL_TO_CMD for l in labels), "semua tombol punya rute perintah"
    assert kb.get("resize_keyboard") is True and kb.get("one_time_keyboard") is False
    safe_prefixes = ("/strategy", "/my_strategy", "/packs", "/chart", "/sources",
                     "/history", "/help", "/whoami", "/auth_setup")
    for label, target in tb._LABEL_TO_CMD.items():
        if "⚡" not in label and not target.startswith("__"):
            assert target.startswith(safe_prefixes), f"menu non-⚡ `{label}` harus bebas token"

