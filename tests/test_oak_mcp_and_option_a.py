"""Unit tests untuk Opsi A (Top-4 OAK Benchmark Swarm) dan OAK Research Remote MCP Client."""
from __future__ import annotations

import json

import llm_research
import oak_mcp_client


def test_option_a_default_models_and_classai_aggregator(monkeypatch):
    # Voter hemat (GLM + MiniMax M3 + Claude Haiku 5.5) + Meta-Aggregator terbaik (Claude Opus 5.5 -> GPT-6.1 Sol)
    assert llm_research.MODEL_V1 == "glm-5.2-fast-preview"
    assert llm_research.MODEL_V2 == "glm-5.3-prime"
    assert llm_research.MODEL_V3 == "minimax-m3"
    assert llm_research.MODEL_V4 == "claude-haiku-5.5"
    assert llm_research.MODEL_AGG_CLASSAI == "claude-opus-5.5"
    assert llm_research.CLASSAI_AGGREGATOR_CHAIN == ["claude-opus-5.5", "gpt-6.1-sol"]

    class _FakeClassAI:
        def __init__(self):
            self.called = []

        def chat_json(self, prompt, system_instruction=None, model=None):
            self.called.append(model)
            return {
                "per_parameter_final": {
                    p: {"final_score": 0.6 if p in ("P1_sentiment_polarity", "P4_momentum_direction", "P7_historical_pattern") else 0.8, "trusted_voter": "CONSENSUS", "rationale": "ok"}
                    for p in llm_research.PARAM_WEIGHTS
                },
                "final_score": 0.6,
                "overall_confidence": 0.86,
                "overall_consensus_level": "strong",
                "vendor_alignment": "both_agree",
                "most_influential_parameter": "P1_sentiment_polarity",
                "justification": "Sintesis CRO Claude Opus 5.5 solid.",
                "disagreement_summary": [],
            }

    class _FailIfCalledDashScope:
        def chat_json(self, *a, **kw):
            raise AssertionError("DashScope aggregator tidak boleh dipanggil jika ClassAI claude-opus-5.5 sukses!")

    verdicts = {
        "V1_qwen_turbo": {"params": {"P1_sentiment_polarity": {"score": 0.6}}, "overall_voter_confidence": 0.8},
        "V3_gemini_flash": {"params": {"P1_sentiment_polarity": {"score": 0.6}}, "overall_voter_confidence": 0.8},
    }
    pre = llm_research.analyze_voter_variance(verdicts)
    fake_ca = _FakeClassAI()
    agg = llm_research.run_meta_aggregator(_FailIfCalledDashScope(), "BTC", verdicts, pre, gemini=fake_ca)
    assert fake_ca.called == ["claude-opus-5.5"]
    assert agg["aggregator_model"] == "claude-opus-5.5"


def test_oak_mcp_not_configured_is_zero_network_fail_safe(monkeypatch):
    oak_mcp_client.reset_oak_cache()
    monkeypatch.delenv("OAK_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("OAK_MCP_CLIENT_ID", raising=False)
    monkeypatch.delenv("OAK_MCP_REFRESH_TOKEN", raising=False)

    assert oak_mcp_client.is_oak_mcp_configured() is False
    brief = oak_mcp_client.fetch_oak_global_brief()
    assert brief["available"] is False
    ctx = oak_mcp_client.fetch_oak_market_context("BTC")
    assert ctx["available"] is False
    assert oak_mcp_client.format_oak_context_for_prompt(ctx) == ""


def test_oak_mcp_client_oauth_refresh_sse_and_tool_call(monkeypatch):
    oak_mcp_client.reset_oak_cache()

    class _FakeResp:
        def __init__(self, status_code, text="", headers=None, json_data=None):
            self.status_code = status_code
            self.text = text
            self.headers = headers or {"content-type": "application/json"}
            self._json_data = json_data

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            if self._json_data is not None:
                return self._json_data
            return json.loads(self.text)

    calls = []

    def _fake_post(url, json=None, data=None, headers=None):
        calls.append({"url": url, "json": json, "data": data, "headers": dict(headers or {})})
        if url == oak_mcp_client.OAK_TOKEN_URL:
            return _FakeResp(200, json_data={"access_token": "oak-fresh-tok", "expires_in": 3600})
        method = (json or {}).get("method")
        if method == "initialize":
            return _FakeResp(
                200,
                text='{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26","capabilities":{}}}',
                headers={"content-type": "application/json", "Mcp-Session-Id": "sess-oak-1"},
            )
        if method == "notifications/initialized":
            return _FakeResp(202, text="")
        if method == "tools/call":
            # Simulasikan respons SSE (text/event-stream) dari MCP Streamable HTTP
            sse_payload = (
                "event: message\n"
                'data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"BTC TVL $66B, ETF inflow positif"}]}}\n\n'
            )
            return _FakeResp(200, text=sse_payload, headers={"content-type": "text/event-stream"})
        raise AssertionError(f"Unexpected call: {url} {method}")

    cli = oak_mcp_client.OakMCPClient(
        client_id="oak-cid",
        refresh_token="oak-rt",
    )
    monkeypatch.setattr(cli.client, "post", _fake_post)

    out = cli.call_tool("get_crypto", {"id": "bitcoin", "locale": "en"})
    assert "BTC TVL $66B" in out
    assert cli.session_id == "sess-oak-1"
    # Urutan panggilan: token refresh -> initialize -> notifications/initialized -> tools/call
    assert calls[0]["url"] == oak_mcp_client.OAK_TOKEN_URL
    assert calls[1]["headers"]["Authorization"] == "Bearer oak-fresh-tok"
    assert calls[3]["headers"]["Mcp-Session-Id"] == "sess-oak-1"
    assert calls[3]["json"]["params"]["arguments"] == {"id": "bitcoin", "locale": "en"}


def test_oak_mcp_fetch_global_and_asset_uses_verified_schemas(monkeypatch):
    oak_mcp_client.reset_oak_cache()
    tool_calls = []

    class _FakeCli:
        def call_tool(self, name, arguments=None):
            tool_calls.append((name, dict(arguments or {})))
            if name == "get_market_overview":
                return json.dumps({
                    "totalMarketCapUsd": {"value": 2.1e12, "change30dPct": -12.5},
                    "defiTvlUsd": {"value": 66.6e9, "change30dPct": -10.0},
                    "stablecoinSupplyUsd": {"value": 284.2e9},
                })
            if name == "get_calendar":
                assert "from" in arguments and "to" in arguments
                return json.dumps({
                    "events": [
                        {"startsAt": "2026-11-24T19:00:00Z", "kind": "macro", "importance": "high", "name": "FOMC Minutes"},
                        {"startsAt": "2026-11-25T21:00:00Z", "kind": "earnings", "importance": "high", "name": "NVIDIA Q3", "symbol": "NVDA"},
                    ]
                })
            if name == "get_news":
                return json.dumps({
                    "items": [
                        {"title": "Bitcoin returns above $60,000 after steep selloff", "summary": "BTC rebounds"},
                        {"title": "NVIDIA launches new AI cluster", "summary": "NVDA demand stays high"},
                    ]
                })
            if name == "get_crypto":
                assert arguments == {"id": "bitcoin", "locale": "en"}
                return json.dumps({"name": "Bitcoin", "symbol": "BTC", "current_price": 60943, "price_change_percentage_24h_in_currency": 4.5, "market_cap": 1.22e12})
            if name == "get_tradfi":
                assert arguments == {"ticker": "NVDA"}
                return json.dumps({"name": "NVIDIA Corporation", "symbol": "NVDA", "current_price": 125.53, "price_change_percentage_24h": -2.1, "market_cap": 3.03e12})
            raise AssertionError(f"Unexpected tool: {name} {arguments}")

    cli = _FakeCli()
    ctx_btc = oak_mcp_client.fetch_oak_market_context("BTC", is_crypto=True, client=cli)
    ctx_nvda = oak_mcp_client.fetch_oak_market_context("NVDA", is_crypto=False, client=cli)

    assert ctx_btc["available"] is True and ctx_btc["errors"] == []
    assert "Bitcoin" in ctx_btc["asset_data"]
    assert "Bitcoin returns above $60,000" in ctx_btc["headlines"][0]
    assert "FOMC Minutes" in ctx_btc["calendar"]

    assert ctx_nvda["available"] is True and ctx_nvda["errors"] == []
    assert "NVIDIA Corporation" in ctx_nvda["asset_data"]
    assert "NVIDIA launches new AI cluster" in ctx_nvda["headlines"][0]
    assert "NVIDIA Q3" in ctx_nvda["calendar"]

    # Global brief hanya ditarik 1x walau 2 aset diteliti (cache bekerja)
    assert sum(1 for n, _ in tool_calls if n == "get_market_overview") == 1
    assert sum(1 for n, _ in tool_calls if n == "get_calendar") == 1


def test_research_stock_enriches_context_and_headlines_from_oak_mcp(monkeypatch):
    oak_mcp_client.reset_oak_cache()
    captured_contexts = []
    captured_headlines = []

    monkeypatch.setattr(oak_mcp_client, "is_oak_mcp_configured", lambda: True)
    monkeypatch.setattr(
        oak_mcp_client,
        "fetch_oak_market_context",
        lambda ticker: {
            "available": True,
            "ticker": ticker,
            "asset_data": "BTC dominance 57.8%, ETF inflow kuat.",
            "market_overview": "Global liquidity meningkat, stablecoin supply $284B.",
            "calendar": "FOMC Minutes Besok 19:00 UTC.",
            "headlines": ["OAK Alpha: BTC institutional inflow surges"],
        },
    )
    monkeypatch.setattr(llm_research, "fetch_earnings_days_with_status", lambda t: (None, "no_data"))
    monkeypatch.setattr(llm_research, "fetch_headlines", lambda t, limit=10: ["Yahoo: BTC holds $60k"])

    def _capture_voter(client, ticker, headlines, strategy_context=""):
        captured_contexts.append(strategy_context)
        captured_headlines.append(list(headlines))
        return {
            "voter_id": "V1_qwen_turbo",
            "overall_voter_confidence": 0.8,
            "params": {
                p: {"score": 0.5, "reasoning": "ok", "metadata": {}}
                for p in llm_research.PARAM_WEIGHTS
            },
        }

    monkeypatch.setattr(llm_research, "run_voter_1", _capture_voter)
    monkeypatch.setattr(llm_research, "run_voter_2", _capture_voter)
    monkeypatch.setattr(
        llm_research,
        "run_meta_aggregator",
        lambda *a, **k: {
            "final_score": 0.5,
            "overall_confidence": 0.8,
            "overall_consensus_level": "STRONG",
            "vendor_alignment": "both_agree",
            "most_influential_parameter": "P1_sentiment_polarity",
            "justification": "Konteks OAK MCP + berita positif.",
            "disagreement_summary": [],
            "per_parameter_final": {},
        },
    )

    res = llm_research.research_stock("BTC", qwen=object(), gemini=None)
    assert res.news_sentiment == "bullish"
    assert any("[DATA REAL-TIME OAK RESEARCH MCP]" in c for c in captured_contexts)
    assert any("FOMC Minutes Besok 19:00 UTC" in c for c in captured_contexts)
    assert captured_headlines[0][0] == "OAK Alpha: BTC institutional inflow surges"
    assert "Yahoo: BTC holds $60k" in captured_headlines[0]
