"""Unit tests untuk Opsi B (OAK-First + Single Deep Analyst paralel antar-aset) & Data Training OAK."""
from __future__ import annotations

import json

import llm_research
import oak_mcp_client
import training_data
import walt_bot


def _valid_single_payload(score: float = 0.55, consensus: str = "strong", p5: float = 0.25) -> dict:
    return {
        "thinking": "1. Data OAK menunjukkan arus ETF positif. 2. P5 terkendali. 3. Layak 24-72 jam.",
        "per_parameter_final": {
            p: {
                "final_score": (
                    score if p in ("P1_sentiment_polarity", "P4_momentum_direction", "P7_historical_pattern")
                    else (p5 if p == "P5_risk_catalysts" else (0.3 if p == "P6_contrarian_check" else 0.8))
                ),
                "rationale": "terukur",
            }
            for p in llm_research.PARAM_WEIGHTS
        },
        "overall_confidence": 0.84,
        "overall_consensus_level": consensus,
        "most_influential_parameter": "P1_sentiment_polarity",
        "justification": (
            "Sentimen OAK Research dan katalis pasar mendukung arah posisi dalam horizon 24-72 jam "
            "tanpa ancaman event makro besar atau crowding ekstrem."
        ),
        "disagreement_summary": ["Volatilitas intraday tetap perlu dijaga dengan ATR SL"],
    }


def test_default_research_mode_is_swarm_with_oak_single_option(monkeypatch):
    monkeypatch.delenv("RESEARCH_MODE", raising=False)
    assert llm_research.get_research_mode() == "swarm"
    monkeypatch.setenv("RESEARCH_MODE", "oak_single")
    assert llm_research.get_research_mode() == "oak_single"


def test_oak_single_skips_yahoo_for_crypto_and_uses_classai_single_pass(monkeypatch):
    monkeypatch.setenv("RESEARCH_MODE", "oak_single")
    oak_mcp_client.reset_oak_cache()

    yahoo_calls = []
    monkeypatch.setattr(oak_mcp_client, "is_oak_mcp_configured", lambda: True)
    monkeypatch.setattr(
        oak_mcp_client,
        "fetch_oak_market_context",
        lambda ticker: {
            "available": True,
            "ticker": ticker,
            "asset_type": "crypto",
            "asset_data": "AAVE (crypto) | price=$210.00 | 24h=+4.50%",
            "market_overview": "Crypto MCap=$2.8T",
            "calendar": "2026-11-26 [macro/high] FOMC",
            "headlines": ["OAK News 1: Aave V4 liquidity grows", "OAK News 2: DeFi TVL hits high"],
        },
    )
    monkeypatch.setattr(
        llm_research,
        "fetch_earnings_days_with_status",
        lambda t: yahoo_calls.append(("earnings", t)) or (None, "no_data"),
    )
    monkeypatch.setattr(
        llm_research,
        "fetch_headlines",
        lambda t, limit=10: yahoo_calls.append(("headlines", t)) or [],
    )

    class _FakeClassAI:
        def __init__(self):
            self.called_models = []

        def chat_json(self, prompt, system_instruction=None, model=None):
            self.called_models.append(model)
            return _valid_single_payload(0.60, "strong", p5=0.20)

    class _FailQwen:
        def chat_json(self, *a, **kw):
            raise AssertionError("DashScope fallback tidak boleh dipanggil bila ClassAI sukses")

    ca = _FakeClassAI()
    res = llm_research.research_stock("AAVE", qwen=_FailQwen(), gemini=ca)
    assert yahoo_calls == []
    assert ca.called_models == [llm_research.SINGLE_ANALYST_CLASSAI_CHAIN[0]]
    assert res.secondary_provider == "oak_single"
    assert res.vendor_alignment == "oak_single_analyst"
    assert res.voters_count == 1
    assert res.news_sentiment == "bullish"
    assert res.overall_consensus_level == "STRONG"
    assert "[DATA REAL-TIME OAK RESEARCH MCP]" in res.oak_context_block
    assert "Data OAK menunjukkan" in res.analyst_thinking


def test_oak_single_checks_earnings_for_us_stock_and_falls_back_on_error(monkeypatch):
    monkeypatch.setenv("RESEARCH_MODE", "oak_single")
    earnings_checked = []

    monkeypatch.setattr(oak_mcp_client, "is_oak_mcp_configured", lambda: True)
    monkeypatch.setattr(
        oak_mcp_client,
        "fetch_oak_market_context",
        lambda ticker: {
            "available": True,
            "ticker": ticker,
            "asset_type": "tradfi",
            "asset_data": "NVDA (tradfi) | price=$140.00 | 24h=-2.50%",
            "market_overview": "Macro calm",
            "calendar": "",
            "headlines": ["OAK 1: NVDA dips on profit taking", "OAK 2: Hyperscaler capex intact"],
        },
    )
    monkeypatch.setattr(
        llm_research,
        "fetch_earnings_days_with_status",
        lambda t: earnings_checked.append(t) or (21, "ok"),
    )

    class _FlakyClassAI:
        def __init__(self):
            self.called = []

        def chat_json(self, prompt, system_instruction=None, model=None):
            self.called.append(model)
            if len(self.called) == 1:
                raise RuntimeError("primary overloaded")
            return _valid_single_payload(0.50, "strong", p5=0.25)

    ca = _FlakyClassAI()
    res = llm_research.research_stock("NVDA", qwen=None, gemini=ca)
    assert earnings_checked == ["NVDA"]
    assert ca.called == llm_research.SINGLE_ANALYST_CLASSAI_CHAIN[:2]
    assert res.days_to_earnings == 21
    assert res.news_sentiment == "bullish"


def test_decide_go_nogo_handles_oak_single_verdicts():
    opp = walt_bot.Opportunity(
        contract_id=101,
        base_asset="NVDA",
        funding_daily=0.0060,
        volume_24h=1_000_000.0,
        max_leverage=5,
        side="long",
        signal_source="price_forecast",
        forecast_setup="S1_1H_LONG_DIP",
        expected_net_edge_pct=0.005,
    )
    res_go = llm_research.Research(
        ticker="NVDA", days_to_earnings=20, news_sentiment="bullish",
        narrative="kuat", confidence=0.85, fetched_at="2026-11-23T15:00:00Z",
        consensus="STRONG", overall_consensus_level="STRONG",
        vendor_alignment="oak_single_analyst", final_score=0.55,
        p1_to_p7_scores={"P5_risk_catalysts": 0.25}, secondary_provider="oak_single",
    )
    v, _, mult = walt_bot.decide_go_nogo(opp, res_go)
    assert v == "GO" and mult == 1.0

    res_split = llm_research.Research(
        ticker="NVDA", days_to_earnings=20, news_sentiment="neutral",
        narrative="bercampur", confidence=0.50, fetched_at="2026-11-23T15:00:00Z",
        consensus="SPLIT", overall_consensus_level="SPLIT",
        vendor_alignment="oak_single_analyst", final_score=0.05,
        p1_to_p7_scores={"P5_risk_catalysts": 0.25}, secondary_provider="oak_single",
    )
    v2, _, mult2 = walt_bot.decide_go_nogo(opp, res_split)
    assert v2 == "HALF" and mult2 == 0.5


def test_training_data_records_oak_single_context_thinking_and_gold_tier():
    res = llm_research.Research(
        ticker="NVDA",
        days_to_earnings=20,
        news_sentiment="bullish",
        narrative=(
            "Sentimen OAK Research dan katalis pasar mendukung posisi long NVDA untuk horizon 24-72 jam "
            "dengan risiko earnings H-20 masih jauh dan katalis P5 tetap terkendali."
        ),
        confidence=0.86,
        fetched_at="2026-11-23T15:00:00+00:00",
        consensus="STRONG",
        overall_consensus_level="STRONG",
        vendor_alignment="oak_single_analyst",
        final_score=0.58,
        p1_to_p7_scores={"P1_sentiment_polarity": 0.6, "P4_momentum_direction": 0.55, "P5_risk_catalysts": 0.2},
        voters_count=1,
        secondary_provider="oak_single",
        headlines=["OAK News 1: NVDA demand remains strong"],
        oak_context_block="[DATA REAL-TIME OAK RESEARCH MCP]\n- Profil & Metrik Aset (NVDA): NVDA (tradfi) | 24h=-2.10%",
        analyst_thinking="Permintaan GPU tetap solid menurut ringkasan OAK MCP dan P5 jauh di bawah batas 0.70.",
    )
    sample = training_data.build_training_sample(
        timestamp_utc="2026-11-23T15:00:00+00:00",
        asset="NVDA",
        side="LONG",
        funding_daily=0.001,
        price=140.0,
        verdict="GO",
        decision_reason="Engine 2 lolos filter AI",
        research=res,
    )
    assert sample is not None
    assert sample["Quality Tier"] == "GOLD"
    assert sample["Source"] == "live_oak_single"
    parsed = json.loads(sample["Messages JSONL"])
    user_msg = parsed["messages"][1]["content"]
    asst_msg = parsed["messages"][2]["content"]
    assert "[DATA REAL-TIME OAK RESEARCH MCP]" in user_msg
    assert "Permintaan GPU tetap solid menurut ringkasan OAK MCP" in asst_msg


def test_scan_with_research_parallelizes_across_assets(monkeypatch):
    researched = []
    monkeypatch.setenv("RESEARCH_MODE", "oak_single")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dummy-ds-key")
    monkeypatch.setenv("DASHSCOPE_API_GEMINI_KEY", "dummy-ca-key")
    monkeypatch.setenv("OAK_MCP_ENABLED", "off")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda _m=6.0: True)
    monkeypatch.setattr(walt_bot, "is_us_stock_market_open_utc", lambda now_utc=None: (True, "open"))
    monkeypatch.setattr(walt_bot, "send_telegram", lambda *a, **kw: True)
    monkeypatch.setattr(walt_bot, "daily_scan", lambda client, now_utc=None: {
        "accounts": [],
        "balance": [{"balance_available": 100.0, "currency": "USDC"}],
        "positions_count": 0,
        "funding_earned": 0.0,
        "target": 10.0,
        "progress_pct": 0.0,
        "top_opportunities": [
            {"asset": "NVDA", "funding_daily": 0.006, "funding_daily_pct": 0.6, "price": 140.0, "side": "long", "recommended_side": "long"},
            {"asset": "SPY", "funding_daily": 0.006, "funding_daily_pct": 0.6, "price": 590.0, "side": "long", "recommended_side": "long"},
            {"asset": "AAVE", "funding_daily": 0.006, "funding_daily_pct": 0.6, "price": 210.0, "side": "short", "recommended_side": "short"},
        ],
        "_raw_opportunities": [],
    })

    def _fake_res(asset, **kw):
        researched.append(asset)
        return llm_research.Research(
            ticker=asset, days_to_earnings=None, news_sentiment="neutral",
            narrative="Netral terukur.", confidence=0.8, fetched_at="2026-11-23T15:00:00Z",
            consensus="STRONG", overall_consensus_level="STRONG",
            vendor_alignment="oak_single_analyst", secondary_provider="oak_single",
        )

    monkeypatch.setattr(walt_bot, "research_stock", _fake_res)
    import sheet_logger
    monkeypatch.setattr(sheet_logger, "log_scan", lambda *a, **kw: {"status": "ok"})

    class _FakeClient:
        def list_positions(self, statuses=None):
            return {"positions": []}

    out = walt_bot.scan_with_research(_FakeClient(), auto_execute=True, dry_run=False)
    assert set(researched) == {"NVDA", "SPY", "AAVE"}
    assert set(out["researches"].keys()) == {"NVDA", "SPY", "AAVE"}


def test_parse_json_lenient_handles_think_fence_trailing_comma_and_raw_newlines():
    noisy = (
        "<think>\nanalisa internal...\n</think>\n"
        "```json\n"
        '{\n  "ok": true,\n  "note": "baris 1\nbaris 2",\n  "score": 0.45,\n}\n'
        "```"
    )
    parsed = llm_research._parse_json_lenient(noisy)
    assert parsed["ok"] is True
    assert parsed["score"] == 0.45
    assert "baris 1" in parsed["note"]


def test_swarm_mode_uses_oak_calendar_without_yahoo_404_and_scores_news_oak(monkeypatch):
    monkeypatch.delenv("RESEARCH_MODE", raising=False)
    oak_mcp_client.reset_oak_cache()

    def _forbid_yahoo_session():
        raise AssertionError("_yahoo_session tidak boleh dipanggil saat OAKresearch aktif!")

    monkeypatch.setattr(llm_research, "_yahoo_session", _forbid_yahoo_session)
    monkeypatch.setattr(llm_research, "fetch_headlines", lambda t, limit=10: [f"Yahoo: {t} climbs 3%"])
    monkeypatch.setattr(oak_mcp_client, "is_oak_mcp_configured", lambda: True)
    monkeypatch.setattr(
        oak_mcp_client,
        "fetch_oak_market_context",
        lambda t: {
            "available": True,
            "ticker": t,
            "asset_type": "tradfi",
            "asset_data": "NVDA (tradfi) | price=$142.00 | 24h=+3.10%",
            "market_overview": "Risk-on sentiment",
            "calendar": "2026-12-10 [earnings/high] NVDA Q3",
            "days_to_earnings": 16,
            "earnings_status": "ok",
            "headlines": ["OAK: NVDA AI demand stays robust"],
        },
    )

    def _fake_voter(v_id):
        def _run(client, ticker, headlines, strategy_context=""):
            assert "[DATA REAL-TIME OAK RESEARCH MCP]" in strategy_context
            return {
                "voter_id": v_id,
                "overall_voter_confidence": 0.85,
                "params": {
                    p: {"score": 0.6 if p in ("P1_sentiment_polarity", "P4_momentum_direction", "P7_historical_pattern") else 0.75, "reasoning": "ok"}
                    for p in llm_research.PARAM_WEIGHTS
                },
            }
        return _run

    monkeypatch.setattr(llm_research, "run_voter_1", _fake_voter("V1_qwen_turbo"))
    monkeypatch.setattr(llm_research, "run_voter_2", _fake_voter("V2_qwen_plus"))
    monkeypatch.setattr(llm_research, "run_voter_3", _fake_voter("V3_gemini_flash"))
    monkeypatch.setattr(llm_research, "run_voter_4", _fake_voter("V4_gemini_pro"))
    monkeypatch.setattr(
        llm_research,
        "run_meta_aggregator",
        lambda *a, **k: {
            "final_score": 0.52,
            "overall_confidence": 0.85,
            "overall_consensus_level": "STRONG",
            "vendor_alignment": "both_agree",
            "most_influential_parameter": "P1_sentiment_polarity",
            "justification": "Swarm menyetujui sinyal berita dan OAK MCP positif.",
            "disagreement_summary": [],
            "per_parameter_final": {
                p: {"final_score": 0.6 if p in ("P1_sentiment_polarity", "P4_momentum_direction", "P7_historical_pattern") else 0.75, "trusted_voter": "CONSENSUS", "rationale": "ok"}
                for p in llm_research.PARAM_WEIGHTS
            },
        },
    )

    res = llm_research.research_stock("NVDA", qwen=object(), gemini=object())
    assert res.days_to_earnings == 16
    assert res.earnings_status == "ok"
    assert res.voters_count == 4
    assert res.news_sentiment == "bullish"
    assert res.overall_consensus_level == "STRONG"
    assert res.p1_to_p7_scores["P1_sentiment_polarity"] == 0.6
    assert "[DATA REAL-TIME OAK RESEARCH MCP]" in res.oak_context_block


def test_market_chart_guards_unlisted_yahoo_and_hl_universe(monkeypatch):
    import market_chart

    assert market_chart._fetch_yahoo_candles("ANTHROPIC") == []
    monkeypatch.setattr(market_chart, "get_hyperliquid_universe_map", lambda: {"BTC": "BTC"})
    hl_called = []
    monkeypatch.setattr(market_chart, "_fetch_hyperliquid_candles", lambda s, *a: hl_called.append(s) or [])
    candles, src = market_chart.fetch_crypto_candles("UNKNOWNCOIN_XYZ", interval="1h", limit=24)
    assert candles == [] and src == "Tidak tersedia"
    assert hl_called == []

