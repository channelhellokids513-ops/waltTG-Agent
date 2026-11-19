import os
import sys
from unittest.mock import MagicMock

import httpx
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import walt_bot


def _make_client():
    tm = MagicMock()
    tm.get_access_token.return_value = "tok_secret_xyz"
    return walt_bot.WaltClient(token_manager=tm)


def _ok_mcp_response(payload_obj: dict) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": __import__("json").dumps(payload_obj)}]}}
    return httpx.Response(200, json=body, request=httpx.Request("POST", walt_bot.WALT_MCP_URL))


def test_p0_4_call_retries_connect_error_twice_then_succeeds(monkeypatch):
    client = _make_client()
    sleeps = []
    monkeypatch.setattr(walt_bot.time, "sleep", lambda s: sleeps.append(s))

    attempts = {"n": 0}

    def fake_post(url, json=None, headers=None):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise httpx.ConnectError("connection reset")
        return _ok_mcp_response({"ok": True, "value": 42})

    monkeypatch.setattr(client.client, "post", fake_post)
    res = client._call("perps_list_contracts")
    assert res == {"ok": True, "value": 42}
    assert attempts["n"] == 3
    assert sleeps == [1, 2]


def test_p0_4_call_does_not_retry_http_400(monkeypatch):
    client = _make_client()
    sleeps = []
    monkeypatch.setattr(walt_bot.time, "sleep", lambda s: sleeps.append(s))

    attempts = {"n": 0}

    def fake_post(url, json=None, headers=None):
        attempts["n"] += 1
        return httpx.Response(400, text="bad request", request=httpx.Request("POST", url))

    monkeypatch.setattr(client.client, "post", fake_post)
    with pytest.raises(RuntimeError) as exc_info:
        client._call("perps_preview_order")
    assert attempts["n"] == 1
    assert sleeps == []
    assert "perps_preview_order" in str(exc_info.value)
    assert "tok_secret_xyz" not in str(exc_info.value)


def test_p0_4_call_429_respects_retry_after_capped_at_20s_and_list_tools_retries(monkeypatch):
    client = _make_client()
    sleeps = []
    monkeypatch.setattr(walt_bot.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def fake_post(url, json=None, headers=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "99"},
                text="rate limited",
                request=httpx.Request("POST", url),
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "perps_list_positions"}]}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(client.client, "post", fake_post)
    tools = client.list_tools()
    assert len(tools) == 1
    assert sleeps == [20.0]
