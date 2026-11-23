"""Gerbang BOT_OWNER_ONLY — bot pribadi hanya menjawab Owner (fail-closed, diam total ke orang lain)."""
import json
from unittest.mock import MagicMock

from telegram_auth import AuthManager, get_owner_chat_id, owner_only_mode
from telegram_bot import WaltTelegramBot

OWNER = 1001


def _mk(tmp_path, monkeypatch, owner=str(OWNER), strict="true"):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", owner)
    monkeypatch.setenv("BOT_OWNER_ONLY", strict)
    monkeypatch.setenv("REQUIRE_GOOGLE_AUTH", "false")
    auth = AuthManager(auth_file=tmp_path / "auth.json", audit_file=tmp_path / "audit.jsonl")
    api = MagicMock()
    bot = WaltTelegramBot(api=api, auth=auth)
    return bot, api, auth


def _msg(update_id, tg_id, text, chat_id=None):
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            "from": {"id": tg_id, "username": f"u{tg_id}"},
            "chat": {"id": chat_id if chat_id is not None else tg_id},
        },
    }


def _audit_events(auth):
    return [json.loads(l)["event"] for l in auth.audit_file.read_text(encoding="utf-8").splitlines()]


def _sent_chats(api):
    return [c.args[0] for c in api.send_message.call_args_list]


def test_helpers_owner_only_and_owner_id(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_OWNER_ONLY", "true")
    assert owner_only_mode() is True
    monkeypatch.setenv("BOT_OWNER_ONLY", "false")
    assert owner_only_mode() is False
    monkeypatch.delenv("BOT_OWNER_ONLY", raising=False)
    assert owner_only_mode() is True  # default ON untuk bot pribadi
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "8588461740")
    assert get_owner_chat_id() == 8588461740
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "dummy")
    assert get_owner_chat_id() is None
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    assert get_owner_chat_id() is None


def test_non_owner_is_silenced_and_logged(tmp_path, monkeypatch):
    bot, api, auth = _mk(tmp_path, monkeypatch)

    bot.handle_update(_msg(1, 999, "/whoami"))

    # Tidak ada balasan apa pun ke orang asing...
    assert 999 not in _sent_chats(api)
    # ...hanya satu alert ke owner.
    assert 1001 in _sent_chats(api)
    assert any("dibisukan" in c.args[1] for c in api.send_message.call_args_list)
    assert "DISCARDED_NON_OWNER" in _audit_events(auth)

    # Perintah /finance asing pun tidak pernah menyentuh handler ekonomi
    before = api.send_message.call_count
    bot.handle_update(_msg(2, 999, "/finance"))
    assert 999 not in _sent_chats(api)
    # alert owner TIDAK diulang untuk ID yang sama
    assert api.send_message.call_count == before


def test_owner_private_chat_still_works(tmp_path, monkeypatch):
    bot, api, _auth = _mk(tmp_path, monkeypatch)

    bot.handle_update(_msg(10, OWNER, "/whoami"))

    assert any("Profil Akun Aktif" in c.args[1] for c in api.send_message.call_args_list)


def test_non_owner_callback_query_fully_ignored(tmp_path, monkeypatch):
    bot, api, _auth = _mk(tmp_path, monkeypatch)

    bot.handle_update({
        "update_id": 20,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": 888, "username": "sniff"},
            "message": {"chat": {"id": OWNER}},
            "data": "cmd:/finance",
        },
    })

    api.answer_callback.assert_not_called()
    assert 888 not in _sent_chats(api)
    # hanya satu alert ke owner; perintah /finance TIDAK dieksekusi
    assert not any("Laporan Kondisi Keuangan" in c.args[1] for c in api.send_message.call_args_list)


def test_owner_in_group_chat_is_rejected_to_prevent_leaks(tmp_path, monkeypatch):
    bot, api, _auth = _mk(tmp_path, monkeypatch)

    # Owner mengirim dari grup (chat_id negatif) — respons bot bisa dibaca anggota grup lain.
    bot.handle_update(_msg(30, OWNER, "/finance", chat_id=-100777))

    assert _sent_chats(api) == []


def test_owner_only_disabled_restores_legacy_rbac(tmp_path, monkeypatch):
    bot, api, _auth = _mk(tmp_path, monkeypatch, strict="false")

    bot.handle_update(_msg(40, 999, "/whoami"))

    # Perilaku lama: penolakan sopan + notif admin
    assert 999 in _sent_chats(api)
    assert any("belum terdaftar" in c.args[1] or "belum terdaftar" in str(c.args) for c in api.send_message.call_args_list)


def test_fail_closed_when_owner_id_unset(tmp_path, monkeypatch):
    bot, api, auth = _mk(tmp_path, monkeypatch, owner="")

    bot.handle_update(_msg(50, OWNER, "/whoami"))

    assert _sent_chats(api) == []
    assert "OWNER_GATE_MISCONFIGURED" in _audit_events(auth)
