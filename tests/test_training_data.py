"""Unit tests untuk pipeline persiapan Data Training SFT ('Data Training' sheet + JSONL)."""
from __future__ import annotations

import json

import llm_research
import sheet_logger
import training_data


def _sample_research(**overrides):
    base = dict(
        ticker="NVDA",
        days_to_earnings=18,
        news_sentiment="bearish",
        narrative=(
            "Tekanan jual jangka pendek mendominasi NVDA setelah pembatasan ekspor terbaru, "
            "dengan konsensus kuat antar voter pada horizon 24-72 jam dan risiko short squeeze terkendali."
        ),
        confidence=0.84,
        fetched_at="2026-11-22T10:00:00+00:00",
        consensus="STRONG",
        overall_consensus_level="STRONG",
        vendor_alignment="both_agree",
        final_score=-0.62,
        p1_to_p7_scores={
            "P1_sentiment_polarity": -0.7,
            "P2_signal_strength": 0.8,
            "P3_source_credibility": 0.85,
            "P4_momentum_direction": -0.65,
            "P5_risk_catalysts": 0.35,
            "P6_contrarian_check": 0.30,
            "P7_historical_pattern": -0.5,
        },
        voters_count=4,
        voter_verdicts={
            "V1_qwen_turbo": {"params": {"P1_sentiment_polarity": {"score": -0.7, "reasoning": "headline negatif"}}},
            "V3_glm_fast": {"params": {"P1_sentiment_polarity": {"score": -0.6, "reasoning": "tekanan berlanjut"}}},
        },
        secondary_provider="glm",
        headlines=["NVDA faces tighter chip export curbs", "Semiconductor ETF slips 2.1%"],
    )
    base.update(overrides)
    return llm_research.Research(**base)


def test_build_training_sample_format_messages_and_think():
    res = _sample_research()
    sample = training_data.build_training_sample(
        timestamp_utc="2026-11-22T10:00:00+00:00",
        asset="NVDA",
        side="SHORT",
        funding_daily=0.0085,
        price=142.5,
        verdict="GO",
        decision_reason="Swarm Strong Consensus (-0.62) aligns with SHORT",
        research=res,
        mg_context={"mg_status": "ok", "mg_regime": "pull_down", "mg_trend_bias": "bearish", "mg_net_gravity": -0.4},
    )

    assert sample is not None
    assert list(sample.keys()) == training_data.DATA_TRAINING_HEADERS
    assert sample["Asset"] == "NVDA"
    assert sample["Sisi"] == "SHORT"
    assert sample["Funding/Hari"] == 0.0085
    assert sample["Vonis"] == "GO"
    assert sample["Quality Tier"] == "GOLD"
    assert sample["Source"] == "live_swarm_glm"

    ok, err = training_data.validate_training_jsonl_line(sample["Messages JSONL"])
    assert ok, err

    obj = json.loads(sample["Messages JSONL"])
    assert set(obj.keys()) == {"messages"}
    msgs = obj["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
    assert "NVDA faces tighter chip export curbs" in msgs[1]["content"]
    assert msgs[2]["content"].startswith("<think>")
    assert "</think>" in msgs[2]["content"]

    after_think = msgs[2]["content"].split("</think>", 1)[1].strip()
    parsed_ans = json.loads(after_think)
    assert parsed_ans["verdict"] == "GO"
    assert parsed_ans["sentiment"] == "bearish"
    assert parsed_ans["final_score"] == -0.62


def test_build_training_sample_rejects_unknown_or_no_data():
    bad = _sample_research(news_sentiment="unknown", consensus="NO_DATA", overall_consensus_level="NO_DATA")
    assert training_data.build_training_sample(
        timestamp_utc="2026-11-22T10:00:00+00:00",
        asset="NVDA",
        side="SHORT",
        funding_daily=0.008,
        price=100.0,
        verdict="HALF",
        decision_reason="no data",
        research=bad,
    ) is None


def test_backfill_from_daily_scan_row_is_valid_and_deterministic():
    row = {
        "Timestamp (UTC)": "2026-11-22T07:34:58.611486+00:00",
        "Asset": "NBIS",
        "Sisi": "LONG",
        "Funding/Hari ()": "-0.00595",
        "Live Price ($)": "84.95",
        "Swarm Score": "-0.65",
        "Sentimen": "bearish",
        "Konsensus": "STRONG",
        "Vendor Alignment": "both_agree",
        "P1 (Polaritas)": "-0.8",
        "P4 (Momentum)": "-0.75",
        "P5 (Katalis)": "0.4",
        "P6 (Crowded)": "0.35",
        "Vonis": "SKIP",
        "Alasan Keputusan": "bearish news (Score: -0.65) bertolak belakang dengan LONG",
        "Narasi Swarm AI": (
            "Sentimen NBIS sangat bearish dalam horizon 24-72 jam didorong tekanan jual dan momentum negatif."
        ),
    }
    s1 = training_data.build_training_sample_from_daily_scan_row(row)
    s2 = training_data.build_training_sample_from_daily_scan_row(row)
    assert s1 is not None and s2 is not None
    assert s1["Sample ID"] == s2["Sample ID"]
    assert s1["Source"] == "backfill_daily_scans"
    assert s1["Quality Tier"] == "SILVER"
    assert s1["Funding/Hari"] == -0.00595
    ok, err = training_data.validate_training_jsonl_line(s1["Messages JSONL"])
    assert ok, err


def test_log_training_samples_posts_to_data_training_sheet_with_dedupe(monkeypatch, tmp_path):
    captured = []

    def _fake_post(payload, url):
        captured.append((payload, url))
        return True

    monkeypatch.setattr(sheet_logger, "_post_webhook", _fake_post)
    sample = training_data.build_training_sample(
        timestamp_utc="2026-11-22T10:00:00+00:00",
        asset="NVDA",
        side="SHORT",
        funding_daily=0.0085,
        price=142.5,
        verdict="GO",
        decision_reason="align",
        research=_sample_research(),
    )
    assert sample is not None

    jsonl_file = str(tmp_path / "train.jsonl")
    # Kirim 2 salinan identik -> harus di-dedupe menjadi 1
    out = training_data.log_training_samples(
        [sample, sample],
        webhook_url="https://script.google.com/macros/s/fake/exec",
        jsonl_path=jsonl_file,
    )

    assert out["samples_count"] == 1
    assert out["jsonl_written"] == 1
    assert out["webhook_logged"] is True
    assert out["sheet"] == "Data Training"
    assert out["target_rows"] == 1000
    assert len(captured) == 1
    payload, _ = captured[0]
    assert payload["sheet"] == "Data Training"
    assert payload["header"] == training_data.DATA_TRAINING_HEADERS
    assert payload["dedupe"]["field"] == "Sample ID"
    assert len(payload["records"]) == 1

    # Menulis lagi baris yang sama ke JSONL lokal -> 0 baris baru (idempotent)
    out2 = training_data.log_training_samples(
        [sample],
        webhook_url="",
        jsonl_path=jsonl_file,
    )
    assert out2["jsonl_written"] == 0
