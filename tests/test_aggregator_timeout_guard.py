"""Unit tests untuk timeout guard & fast failover Meta-Aggregator serta guard waktu scan_with_research."""
from __future__ import annotations

import httpx

import llm_research
import walt_bot


def _sample_verdicts_and_pre():
    verdicts = {
        "V1_qwen_turbo": {"params": {"P1_sentiment_polarity": {"score": 0.55}}, "overall_voter_confidence": 0.8},
        "V3_gemini_flash": {"params": {"P1_sentiment_polarity": {"score": 0.60}}, "overall_voter_confidence": 0.8},
    }
    pre = llm_research.analyze_voter_variance(verdicts)
    return verdicts, pre


def _valid_agg_payload(score: float = 0.55) -> dict:
    return {
        "per_parameter_final": {
            p: {
                "final_score": score if p in ("P1_sentiment_polarity", "P4_momentum_direction", "P7_historical_pattern") else 0.7,
                "trusted_voter": "CONSENSUS",
                "rationale": "ok",
            }
            for p in llm_research.PARAM_WEIGHTS
        },
        "final_score": score,
        "overall_confidence": 0.82,
        "overall_consensus_level": "strong",
        "vendor_alignment": "both_agree",
        "most_influential_parameter": "P1_sentiment_polarity",
        "justification": "Failover ClassAI berhasil cepat.",
        "disagreement_summary": [],
    }


def test_meta_aggregator_timeout_failover_directly_to_classai():
    """Saat DashScope ReadTimeout dan ClassAI tersedia, langsung failover ke ClassAI tanpa mencoba model DashScope kedua."""
    verdicts, pre = _sample_verdicts_and_pre()

    class _TimeoutDashScope:
        def __init__(self):
            self.called = []

        def chat_json(self, messages, model=None):
            self.called.append(model)
            raise httpx.ReadTimeout("timed out")

    class _FastClassAI:
        def __init__(self):
            self.called = []

        def chat_json(self, prompt, system_instruction=None, model=None):
            self.called.append(model)
            return _valid_agg_payload(0.6)

    ds = _TimeoutDashScope()
    ca = _FastClassAI()
    agg = llm_research.run_meta_aggregator(ds, "NVDA", verdicts, pre, gemini=ca)
    assert ds.called == [llm_research.AGGREGATOR_MODEL_CHAIN[0]]
    assert ca.called == [llm_research.CLASSAI_AGGREGATOR_CHAIN[0]]
    assert agg["aggregator_model"] == llm_research.CLASSAI_AGGREGATOR_CHAIN[0]


def test_meta_aggregator_non_timeout_error_continues_dashscope_chain():
    """Error non-timeout (mis. HTTP 429) tetap melanjutkan fallback model DashScope berikutnya."""
    verdicts, pre = _sample_verdicts_and_pre()

    class _RateLimitedFirstDashScope:
        def __init__(self):
            self.called = []

        def chat_json(self, messages, model=None):
            self.called.append(model)
            if len(self.called) == 1:
                raise RuntimeError("HTTP 429")
            return _valid_agg_payload(0.5)

    class _FailIfCalledClassAI:
        def chat_json(self, *a, **kw):
            raise AssertionError("ClassAI tidak perlu dipanggil jika fallback DashScope kedua berhasil")

    ds = _RateLimitedFirstDashScope()
    agg = llm_research.run_meta_aggregator(ds, "BTC", verdicts, pre, gemini=_FailIfCalledClassAI())
    assert ds.called == llm_research.AGGREGATOR_MODEL_CHAIN[:2]
    assert agg["aggregator_model"] == llm_research.AGGREGATOR_MODEL_CHAIN[1]


def test_meta_aggregator_budget_exhausted_falls_back_to_rule_based(monkeypatch):
    """Bila budget total Meta-Aggregator habis, langsung jatuh ke sintesis rule-based tanpa memanggil LLM."""
    verdicts, pre = _sample_verdicts_and_pre()
    monkeypatch.setattr(llm_research, "AGGREGATOR_TOTAL_BUDGET_SEC", 0.0)

    class _ShouldNotCall:
        def chat_json(self, *a, **kw):
            raise AssertionError("Tidak boleh memanggil LLM saat budget waktu sudah habis")

    agg = llm_research.run_meta_aggregator(_ShouldNotCall(), "SPY", verdicts, pre, gemini=_ShouldNotCall())
    assert "aggregator_model" not in agg
    assert "Sintesis Swarm AI" in agg["justification"]


def test_scan_with_research_stops_research_loop_when_job_time_low(monkeypatch):
    """scan_with_research menghentikan loop riset AI jika has_enough_job_time(2.5) False, tapi tetap menyelesaikan summary."""
    research_calls = []

    monkeypatch.setattr(walt_bot, "daily_scan", lambda client, now_utc=None: {
        "accounts": [],
        "balance": [{"balance_available": 100.0, "currency": "USDC"}],
        "positions_count": 0,
        "funding_earned": 0.0,
        "target": 10.0,
        "progress_pct": 0.0,
        "top_opportunities": [
            {"asset": "NVDA", "funding_daily": 0.005, "funding_daily_pct": 0.5, "price": 180.0, "side": "long", "recommended_side": "long"},
            {"asset": "SPY", "funding_daily": 0.005, "funding_daily_pct": 0.5, "price": 590.0, "side": "long", "recommended_side": "long"},
        ],
        "_raw_opportunities": [],
    })
    monkeypatch.setattr(walt_bot, "is_us_stock_market_open_utc", lambda now_utc=None: (True, "open"))
    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda _m=6.0: False)
    monkeypatch.setattr(walt_bot, "research_stock", lambda *a, **kw: research_calls.append(a[0]))
    monkeypatch.setattr(walt_bot, "send_telegram", lambda *a, **kw: True)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dummy-ds-key")
    monkeypatch.setenv("OAK_MCP_ENABLED", "off")

    import sheet_logger
    monkeypatch.setattr(sheet_logger, "log_scan", lambda *a, **kw: {"status": "ok"})

    class _FakeClient:
        def list_positions(self, statuses=None):
            return {"positions": []}

    res = walt_bot.scan_with_research(_FakeClient(), auto_execute=True, dry_run=False)
    assert research_calls == []
    assert "summary" in res and "top_opportunities" in res["summary"]
