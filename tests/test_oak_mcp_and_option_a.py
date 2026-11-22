"""Unit tests untuk Opsi A (Top-4 OAK Benchmark Swarm) dan OAK Research Remote MCP Client."""
from __future__ import annotations

import json

import llm_research
import oak_mcp_client


def test_option_a_default_models_and_classai_aggregator(monkeypatch):
    # Pastikan default Opsi A sesuai Top-4 OAK AI Trading Benchmark (semua 0 likuidasi)
    assert llm_research.MODEL_V1 == "glm-5.2-fast-preview"
    assert llm_research.MODEL_V2 == "glm-5.3-prime"
    assert llm_research.MODEL_V3 == "gpt-6.1-sol"
    assert llm_research.MODEL_V4 == "claude-opus-5.5"
    assert llm_research.MODEL_AGG_CLASSAI == "gpt-6.1-sol"
    assert llm_research.CLASSAI_AGGREGATOR_CHAIN == ["gpt-6.1-sol", "claude-opus-5.5"]

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
                "justification": "Sintesis CRO GPT-6.1 Sol solid.",
                "disagreement_summary": [],
            }

    class _FailIfCalledDashScope:
        def chat_json(self, *a, **kw):
            raise AssertionError("DashScope aggregator tidak boleh dipanggil jika ClassAI gpt-6.1-sol sukses!")

    verdicts = {
        "V1_qwen_turbo": {"params": {"P1_sentiment_polarity": {"score": 0.6}}, "overall_voter_confidence": 0.8},
        "V3_gemini_flash": {"params": {"P1_sentiment_polarity": {"score": 0.6}}, "overall_voter_confidence": 0.8},
    }
    pre = llm_research.analyze_voter_variance(verdicts)
    fake_ca = _FakeClassAI()
    agg = llm_research.run_meta_aggregator(_FailIfCalledDashScope(), "BTC", verdicts, pre, gemini=fake_ca)
    assert fake_ca.called == ["gpt-6.1-sol"]
    assert agg["aggregator_model"] == "gpt-6.1-sol"


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

    out = cli.call_tool("get_crypto", {"query": "BTC"})
    assert "BTC TVL $66B" in out
    assert cli.session_id == "sess-oak-1"
    # Urutan panggilan: token refresh -> initialize -> notifications/initialized -> tools/call
    assert calls[0]["url"] == oak_mcp_client.OAK_TOKEN_URL
    assert calls[1]["headers"]["Authorization"] == "Bearer oak-fresh-tok"
    assert calls[3]["headers"]["Mcp-Session-Id"] == "sess-oak-1"


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
