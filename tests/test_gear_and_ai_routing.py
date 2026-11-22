"""Unit tests untuk Sistem Gear 4 Tingkatan (0, 1, 2, 3) dan Kebijakan Routing AI (Gemini Idle vs GLM Multi-Peluang)."""
from __future__ import annotations

from datetime import datetime, timezone

import llm_research
import walt_bot


def _dummy_raw_voter_payload(score: float = -0.5) -> dict:
    """Format JSON mentah sesuai UNIFIED_VOTER_SCHEMA_PROMPT sebelum _extract_voter_params."""
    return {
        "P1_sentiment_polarity": {"score": score, "reasoning": "arah jelas"},
        "P2_signal_strength": {"score": 0.7, "reasoning": "kuat"},
        "P3_source_credibility": {"score": 0.8, "reasoning": "kredibel"},
        "P4_momentum_direction": {"score": score, "reasoning": "momentum searah"},
        "P5_risk_catalysts": {"score": 0.1, "reasoning": "rendah"},
        "P6_contrarian_check": {"score": 0.2, "reasoning": "wajar"},
        "P7_historical_pattern": {"score": score, "reasoning": "konsisten"},
        "overall_voter_confidence": 0.8,
    }


def _dummy_extracted_voter_payload(score: float = -0.5) -> dict:
    return {
        "params": {
            k: v for k, v in _dummy_raw_voter_payload(score).items() if k.startswith("P")
        },
        "overall_voter_confidence": 0.8,
    }


class _FakeQwenClient:
    def __init__(self):
        self.called_models: list[str] = []

    def chat_json(self, messages, model=None):
        self.called_models.append(model or "default")
        if model in llm_research.AGGREGATOR_MODEL_CHAIN:
            return {
                "per_parameter_final": {
                    p: {"final_score": -0.5 if p in ("P1_sentiment_polarity", "P4_momentum_direction", "P7_historical_pattern") else 0.7, "trusted_voter": "CONSENSUS", "rationale": "ok"}
                    for p in llm_research.PARAM_WEIGHTS
                },
                "final_score": -0.5,
                "overall_confidence": 0.82,
                "overall_consensus_level": "strong",
                "vendor_alignment": "both_agree",
                "most_influential_parameter": "P1_sentiment_polarity",
                "justification": "Sintesis GLM + Qwen solid.",
                "disagreement_summary": [],
            }
        return _dummy_raw_voter_payload(-0.5)


class _FakeGeminiClient:
    def __init__(self):
        self.called_models: list[str] = []

    def chat_json(self, prompt, system_instruction=None, model=None):
        self.called_models.append(model or "default")
        return _dummy_raw_voter_payload(-0.4)


def test_ai_routing_policy_helper():
    assert llm_research.should_use_glm_for_secondary_voters(opportunity_count=0) is False
    assert llm_research.should_use_glm_for_secondary_voters(opportunity_count=1) is False
    assert llm_research.should_use_glm_for_secondary_voters(opportunity_count=2) is True
    assert llm_research.should_use_glm_for_secondary_voters(opportunity_count=5) is True
    # Eksplisit is_idle=True selalu memaksa Gemini (bukan GLM)
    assert llm_research.should_use_glm_for_secondary_voters(opportunity_count=5, is_idle=True) is False


def test_research_stock_uses_gemini_when_idle(monkeypatch):
    monkeypatch.setattr(llm_research, "fetch_earnings_days_with_status", lambda t: (25, "ok"))
    monkeypatch.setattr(llm_research, "fetch_headlines", lambda t, limit=10: ["Headline 1", "Headline 2"])

    qwen = _FakeQwenClient()
    gemini = _FakeGeminiClient()

    res = llm_research.research_stock(
        "NVDA",
        qwen=qwen,
        gemini=gemini,
        opportunity_count=0,
        is_idle=True,
    )

    assert res.secondary_provider == "gemini"
    assert llm_research.MODEL_V3 in gemini.called_models
    assert llm_research.MODEL_V4 in gemini.called_models
    assert llm_research.MODEL_V3_GLM not in qwen.called_models
    assert llm_research.MODEL_V4_GLM not in qwen.called_models
    assert "V3_gemini_flash" in res.voter_verdicts
    assert "V4_gemini_pro" in res.voter_verdicts
    assert res.voters_count == 4


def test_research_stock_switches_to_glm_when_many_opportunities(monkeypatch):
    monkeypatch.setattr(llm_research, "fetch_earnings_days_with_status", lambda t: (25, "ok"))
    monkeypatch.setattr(llm_research, "fetch_headlines", lambda t, limit=10: ["Headline 1", "Headline 2"])
    # Pastikan GeminiClient TIDAK dipanggil / diinisialisasi saat banyak peluang (> 1)
    monkeypatch.setattr(
        llm_research,
        "GeminiClient",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("GeminiClient tidak boleh dipakai saat peluang > 1!")),
    )

    qwen = _FakeQwenClient()
    gemini = _FakeGeminiClient()

    res = llm_research.research_stock(
        "NVDA",
        qwen=qwen,
        gemini=gemini,
        opportunity_count=3,
        is_idle=False,
    )

    assert res.secondary_provider == "glm"
    assert gemini.called_models == []
    assert llm_research.MODEL_V3_GLM == "glm-5.2-fast-preview"
    assert llm_research.MODEL_V4_GLM == "glm-5.3-prime"
    assert "glm-5.2-fast-preview" in qwen.called_models
    assert "glm-5.3-prime" in qwen.called_models
    assert "V3_glm_fast" in res.voter_verdicts
    assert "V4_glm_prime" in res.voter_verdicts
    assert res.voters_count == 4
    assert res.gemini_sentiment == "bearish"  # secondary vendor tetap terpetakan untuk kompatibilitas


def test_meta_aggregator_rule_based_aligns_qwen_and_glm():
    verdicts = {
        "V1_qwen_turbo": _dummy_extracted_voter_payload(0.6),
        "V2_qwen_plus": _dummy_extracted_voter_payload(0.5),
        "V3_glm_fast": _dummy_extracted_voter_payload(0.55),
        "V4_glm_prime": _dummy_extracted_voter_payload(0.65),
    }
    pre = llm_research.analyze_voter_variance(verdicts)
    agg = llm_research.run_meta_aggregator(None, "NVDA", verdicts, pre)
    assert agg["vendor_alignment"] == "both_agree"


def test_gear_four_tiers_and_gear3_lifts_research_caps(monkeypatch, tmp_path):
    # Uji transisi 4 tingkatan Gear berbasis jumlah peluang GO
    def _opp(i: int, asset: str, funding: float = 0.006):
        return walt_bot.Opportunity(
            contract_id=i,
            base_asset=asset,
            funding_daily=funding,
            volume_24h=5_000_000.0,
            max_leverage=2,
            side="short",
        )

    assert walt_bot.evaluate_execution_gear([], go_count=0, category="market", full_research_mode=False)[:2] == (
        "GEAR_0_IDLE_2X_US", 225
    )
    assert walt_bot.evaluate_execution_gear([], go_count=1, category="market", full_research_mode=False)[:2] == (
        "GEAR_1_CONSERVATIVE_120M", 120
    )
    assert walt_bot.evaluate_execution_gear([], go_count=5, category="market", full_research_mode=False)[:2] == (
        "GEAR_2_ACTIVE_60M", 60
    )
    assert walt_bot.evaluate_execution_gear([], go_count=6, category="market", full_research_mode=False)[:2] == (
        "GEAR_3_FULL_RESEARCH_60M", 60
    )

    # Uji Gear 3 pada market_execute(): mengizinkan riset AI di semua kandidat (> max_slots*2 dan > CRYPTO_RESEARCH_TOP_N)
    assets = sorted(walt_bot.ALLOWED_CRYPTO)[:7]
    contracts = [
        {
            "contract_id": idx + 1,
            "base_asset": a,
            "is_active": True,
            "funding_rate_hourly": "0.0004",  # ~0.96%/hari -> GO
            "volume_24h_usd": "10000000",
            "max_leverage": 5,
            "mark_price": "100",
            "last_price": "100",
        }
        for idx, a in enumerate(assets)
    ]

    class _FakeClient:
        def balance(self):
            return {"accounts": [{"balance_available": "100.0", "currency": "USDC"}]}

        def list_contracts(self):
            return {"contracts": contracts}

        def list_positions(self, statuses=None):
            return {"positions": []}

        def price(self, contract_id):
            return {"mark_price": "100.0", "index_price": "100.0"}

        def latest_rate(self, market_id):
            return {"rate": "100.0", "mark_price": "100.0", "index_price": "100.0"}

    researched_calls: list[tuple[str, int, bool | None]] = []

    def _fake_research(ticker, qwen=None, gemini=None, funding_daily=None, recommended_side=None, opportunity_count=0, is_idle=None):
        researched_calls.append((ticker, opportunity_count, is_idle))
        return llm_research.Research(
            ticker=ticker,
            days_to_earnings=None,
            news_sentiment="neutral",
            narrative="ok",
            confidence=0.8,
            fetched_at="2026-11-23T15:00:00+00:00",
            consensus="STRONG",
            overall_consensus_level="STRONG",
            vendor_alignment="both_agree",
            secondary_provider="glm",
        )

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    monkeypatch.delenv("DASHSCOPE_API_GEMINI_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(walt_bot, "QwenClient", lambda *a, **kw: _FakeQwenClient())
    monkeypatch.setattr(walt_bot, "research_stock", _fake_research)
    monkeypatch.setattr(walt_bot, "send_telegram", lambda *a, **kw: True)
    monkeypatch.setattr(walt_bot, "check_cold_state_entry_gate", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(walt_bot, "check_daily_circuit_breaker", lambda *a, **kw: (False, "ok"))
    monkeypatch.setattr(walt_bot, "get_dynamic_max_slots", lambda eq: 2)  # normal max_otf_calls = 4, tapi Gear 3 harus angkat!

    import sheet_logger
    monkeypatch.setattr(sheet_logger, "fetch_latest_research_from_sheet", lambda: {})
    monkeypatch.setattr(sheet_logger, "get_research_read_status", lambda: "ok")
    monkeypatch.setattr(sheet_logger, "get_research_read_source", lambda: "webhook")
    monkeypatch.setattr(sheet_logger, "get_research_read_diagnosis", lambda: "")
    monkeypatch.setattr(sheet_logger, "log_execution", lambda *a, **kw: {"csv_logged": False, "webhook_logged": False})

    res = walt_bot.market_execute(
        _FakeClient(),
        dry_run=True,
        universe=walt_bot.ALLOWED_CRYPTO,
        now_utc=datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc),
    )

    assert res["gear"] == "GEAR_3_FULL_RESEARCH_60M"
    # Semua 8 aset crypto (> max_slots*2=4 dan > CRYPTO_RESEARCH_TOP_N=5) diizinkan melakukan riset AI!
    assert len(researched_calls) == len(assets)
    # Karena ada 8 peluang (> 1), semua panggilan riset membawa opportunity_count=8 & is_idle=False (-> GLM)
    assert all(cnt == len(assets) and idle is False for _, cnt, idle in researched_calls)


def test_gemini_client_uses_classai_router_and_falls_back_on_404(monkeypatch):
    calls = []

    class _Resp:
        def __init__(self, code, data):
            self.status_code = code
            self._data = data

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return self._data

    responses = [
        _Resp(404, {"error": {"code": "model_not_found"}}),
        _Resp(200, {"choices": [{"message": {"content": '{"status": "ok"}'}}]}),
    ]

    def _fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": dict(headers or {}), "payload": dict(json or {})})
        return responses.pop(0)

    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    client = llm_research.GeminiClient(api_key="classai-test-key", model="gemini-flash-latest")
    monkeypatch.setattr(client.client, "post", _fake_post)

    out = client.chat_json("hello", system_instruction="sys")
    assert out == {"status": "ok"}
    assert len(calls) == 2
    assert calls[0]["url"] == "https://router.classai.id/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer classai-test-key"
    assert calls[0]["payload"]["model"] == "gemini-flash-latest"
    assert calls[1]["payload"]["model"] == "gemini-4-argon"
