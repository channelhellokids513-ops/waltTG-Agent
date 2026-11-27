"""Unit test dukungan Alibaba Cloud Model Studio Token Plan pada llm_research & workflow."""
from __future__ import annotations

from pathlib import Path

import yaml

import llm_research


def test_resolve_dashscope_base_url_auto_detect_dan_override(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
    assert (
        llm_research.resolve_dashscope_base_url("sk-sp-example")
        == llm_research.DASHSCOPE_TOKEN_PLAN_BASE
    )
    assert (
        llm_research.resolve_dashscope_base_url("sk-ws-example")
        == llm_research.DASHSCOPE_BASE_DEFAULT
    )

    monkeypatch.setenv("DASHSCOPE_BASE_URL", "")
    assert (
        llm_research.resolve_dashscope_base_url("sk-sp-example")
        == llm_research.DASHSCOPE_TOKEN_PLAN_BASE
    )
    assert (
        llm_research.resolve_dashscope_base_url("sk-ws-example")
        == llm_research.DASHSCOPE_BASE_DEFAULT
    )

    custom_url = "https://custom.example.com/v1/"
    monkeypatch.setenv("DASHSCOPE_BASE_URL", custom_url)
    assert llm_research.resolve_dashscope_base_url("sk-ws-example") == "https://custom.example.com/v1"
    assert llm_research.resolve_dashscope_base_url("sk-sp-example") == "https://custom.example.com/v1"

    assert (
        llm_research.resolve_dashscope_base_url("sk-ws-example", base_url="https://explicit.example/v1/")
        == "https://explicit.example/v1"
    )


def test_qwen_client_memilih_base_url_token_plan(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
    qc = llm_research.QwenClient(api_key="sk-sp-dummy")
    assert qc.base_url == llm_research.DASHSCOPE_TOKEN_PLAN_BASE
    assert str(qc.client.base_url).rstrip("/") == llm_research.DASHSCOPE_TOKEN_PLAN_BASE


def test_semua_workflow_meneruskan_dashscope_base_dan_agg_fallback():
    for nama in ("daily-scan.yml", "market-execute.yml", "crypto-execute.yml", "risk-monitor.yml"):
        wf = yaml.safe_load(Path(f".github/workflows/{nama}").read_text(encoding="utf-8"))
        steps = next(iter(wf["jobs"].values()))["steps"]
        merged: dict[str, str] = {}
        for step in steps:
            if step.get("run"):
                merged.update(step.get("env") or {})
        assert "vars.DASHSCOPE_BASE_URL" in str(merged.get("DASHSCOPE_BASE_URL", "")), (
            f"{nama}: wajib meneruskan DASHSCOPE_BASE_URL dari vars.DASHSCOPE_BASE_URL"
        )
        assert "vars.DASHSCOPE_MODEL_AGG_FALLBACK" in str(
            merged.get("DASHSCOPE_MODEL_AGG_FALLBACK", "")
        ), f"{nama}: wajib meneruskan DASHSCOPE_MODEL_AGG_FALLBACK dari vars.DASHSCOPE_MODEL_AGG_FALLBACK"
