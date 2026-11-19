import builtins
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import notifier
import token_manager


def test_p1_3_missing_pynacl_alerts_telegram_and_returns_false(monkeypatch):
    tg_calls = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg, **kw: tg_calls.append(msg))

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "nacl" or name.startswith("nacl."):
            raise ImportError("No module named 'nacl'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ok = token_manager.update_github_secret("owner/repo", "WALT_REFRESH_TOKEN", "tok_secret", "ghp_dummy")
    assert ok is False
    assert any("pynacl tidak terpasang" in m for m in tg_calls)


def test_p1_4_update_local_env_lambda_and_missing_file_or_key_alerts(monkeypatch, tmp_path):
    tg_calls = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg, **kw: tg_calls.append(msg))

    # Simulasi lokasi modul di tmp_path agar tidak menyentuh .env asli repo
    fake_mod_file = tmp_path / "token_manager.py"
    monkeypatch.setattr(token_manager, "__file__", str(fake_mod_file))

    tm = token_manager.TokenManager(client_id="cid", refresh_token="old_tok")

    # 1. File .env tidak ada -> alert Telegram tanpa membocorkan token
    secret_tok = r"rt_special_\1\g<0>/abc"
    tm._update_local_env(secret_tok)
    assert len(tg_calls) == 1
    assert ".env" in tg_calls[0]
    assert secret_tok not in tg_calls[0]

    # 2. File .env ada tapi tanpa kunci WALT_REFRESH_TOKEN -> alert Telegram tanpa bocor token
    tg_calls.clear()
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER_KEY=123\n", encoding="utf-8")
    tm._update_local_env(secret_tok)
    assert len(tg_calls) == 1
    assert secret_tok not in tg_calls[0]

    # 3. File .env ada dengan kunci -> diganti aman walau token punya backslash \1\g<0>/
    tg_calls.clear()
    env_file.write_text("WALT_CLIENT_ID=cid\nWALT_REFRESH_TOKEN=old_tok\n", encoding="utf-8")
    tm._update_local_env(secret_tok)
    assert tg_calls == []
    assert f"WALT_REFRESH_TOKEN={secret_tok}" in env_file.read_text(encoding="utf-8")


def test_p1_5_refresh_invalid_grant_raises_runtime_error_and_rotation_sync_fail_alerts(monkeypatch, tmp_path):
    tg_calls = []
    monkeypatch.setattr(notifier, "send_telegram", lambda msg, **kw: tg_calls.append(msg))
    monkeypatch.setattr(token_manager, "__file__", str(tmp_path / "token_manager.py"))
    (tmp_path / ".env").write_text("WALT_REFRESH_TOKEN=old_tok\n", encoding="utf-8")

    # 1. Body {"error": "invalid_grant"} -> RuntimeError (BUKAN KeyError)
    resp_err = MagicMock()
    resp_err.status_code = 200
    resp_err.raise_for_status.return_value = None
    resp_err.json.return_value = {"error": "invalid_grant"}
    resp_err.text = '{"error":"invalid_grant"}'
    monkeypatch.setattr(token_manager.httpx, "post", lambda *a, **kw: resp_err)

    tm = token_manager.TokenManager(client_id="cid", refresh_token="old_tok")
    with pytest.raises(RuntimeError) as exc_info:
        tm._refresh()
    assert "access_token" in str(exc_info.value)

    # 2. Body non-JSON -> RuntimeError dengan <= 200 char body
    resp_bad_json = MagicMock()
    resp_bad_json.status_code = 200
    resp_bad_json.raise_for_status.return_value = None
    resp_bad_json.json.side_effect = ValueError("not json")
    resp_bad_json.text = "<html>" + ("X" * 400) + "</html>"
    monkeypatch.setattr(token_manager.httpx, "post", lambda *a, **kw: resp_bad_json)
    with pytest.raises(RuntimeError) as exc_json:
        tm._refresh()
    assert "X" * 205 not in str(exc_json.value)

    # 3. Rotation dengan update_github_secret False -> send_telegram dipanggil
    monkeypatch.setenv("GH_PAT", "ghp_dummy_pat")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(token_manager, "update_github_secret", lambda *a, **kw: False)

    resp_rot = MagicMock()
    resp_rot.status_code = 200
    resp_rot.raise_for_status.return_value = None
    resp_rot.json.return_value = {
        "access_token": "acc_new",
        "expires_in": 3600,
        "refresh_token": "rt_rotated_new",
    }
    monkeypatch.setattr(token_manager.httpx, "post", lambda *a, **kw: resp_rot)

    tm2 = token_manager.TokenManager(client_id="cid", refresh_token="old_tok")
    tm2._refresh()
    assert any("TOKEN ROTATION" in m for m in tg_calls)
