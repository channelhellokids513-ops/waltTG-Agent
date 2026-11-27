"""
Multi-Voter AI Research Layer (4-Voter Multi-Vendor Swarm + GLM-5.3 CRO Meta-Aggregator).
Implementasi Standar PRD 3.5 (Diselaraskan dengan OAK Benchmark & MCP Integration):
  - 4 Voter Lintas Vendor:
      V1: Zhipu GLM-5.2-Fast-Preview (`glm-5.2-fast-preview` via DashScope — Fast Validator)
      V2: Zhipu GLM-5.3-Prime        (`glm-5.3-prime` via DashScope — #1 OAK +29.4%, 0 liq)
      V3: MiniMax M3                 (`minimax-m3` via ClassAI Router — #7 OAK Max DD -6.5%, 0 liq)
      V4: Anthropic Claude Haiku 5.5 (`claude-haiku-5.5` via ClassAI Router — Fast Reasoner, 1M ctx)
  - Pengayaan Data Real-Time via OAK Research Remote MCP Server (`oak_mcp_client.py`):
      Injeksi blok [DATA REAL-TIME OAK RESEARCH MCP] (`get_market_overview`, `get_calendar`,
      `get_news`, `get_crypto`, `get_tradfi`) sebelum seluruh voter & aggregator mengambil keputusan.
  - 7 Parameter Standar Validasi (P1-P7):
      P1: sentiment_polarity    (Weight: 0.25)
      P2: signal_strength       (Weight: 0.10)
      P3: source_credibility    (Weight: 0.10)
      P4: momentum_direction    (Weight: 0.20)
      P5: risk_catalysts        (Weight: 0.15)
      P6: contrarian_check      (Weight: 0.10)
      P7: historical_pattern    (Weight: 0.10)
  - Pre-processing rule-based variance/consensus analysis (UNANIMOUS, STRONG, MIXED, SPLIT)
  - Meta-Aggregator CRO: Utama `glm-5.3` (#1 OAK Winner via DashScope `DASHSCOPE_API_KEY`)
      -> fallback DashScope (`glm-5.3-prime`, `glm-5.2-fast-preview`, `qwen-plus`, `qwen-flash`)
      -> fallback ClassAI (`claude-opus-5.5` -> `gpt-6.1-sol`) -> sintesis deterministik rule-based.
  - SFT Data Training Hook (`training_data.py`): Log <think>...</think> + JSON ke tab 'Data Training' & JSONL.

Environment Variables:
  DASHSCOPE_API_KEY        — Alibaba Cloud Model Studio / DashScope (GLM-5.3 Aggregator, V1, V2, fallback Qwen)
  DASHSCOPE_API_GEMINI_KEY — ClassAI Router (`https://router.classai.id/v1`, V3 minimax-m3, V4 claude-haiku-5.5, fallback agg)
  OAK_MCP_CLIENT_ID / OAK_MCP_REFRESH_TOKEN — OAuth 2.1 PKCE untuk OAK Research MCP Server
"""

import os
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Literal, Optional, Any
import httpx

log = logging.getLogger("llm_research")


class _CrumbRedactingFilter(logging.Filter):
    """Jangan pernah menuliskan token sesi pihak ketiga ke log (CI publik menyimpan teks log ini).

    Dua kasus yang TERBUKA muncul di log nyata 2026-11-20:
      - Yahoo:   `crumb=...`  (terikat cookie sesi; bukan kredensial akun, tapi token sesi)
      - Apps Script: `user_content_key=...` pada URL `script.googleusercontent.com/macros/echo`
        — GitHub Actions hanya membungkam nilai secret yang terdaftar, jadi parameter ini lolos
        apa adanya ke log yang bisa dibaca publik.
    """

    PATTERN = re.compile(r"((?:crumb|user_content_key)=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "crumb=" in msg or "user_content_key=" in msg:
            record.msg = self.PATTERN.sub(r"\1<redacted>", msg)
            record.args = ()
        return True


def _install_crumb_redaction() -> None:
    for name in ("httpx", "httpcore"):
        target = logging.getLogger(name)
        if not any(isinstance(f, _CrumbRedactingFilter) for f in target.filters):
            target.addFilter(_CrumbRedactingFilter())


_install_crumb_redaction()

DASHSCOPE_BASE_DEFAULT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_TOKEN_PLAN_BASE = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_BASE = (os.getenv("DASHSCOPE_BASE_URL") or DASHSCOPE_BASE_DEFAULT).strip().rstrip("/")
GEMINI_BASE = os.getenv("GEMINI_BASE_URL", "https://router.classai.id/v1").rstrip("/")
GEMINI_CLASSAI_FALLBACK_MODEL = "claude-haiku-5.5"

# Default Model Specifications (Voter Hemat + Aggregator Pemenang #1 OAK via DashScope):
#   V1: glm-5.2-fast-preview (DashScope `DASHSCOPE_API_KEY` — cepat & hemat)
#   V2: glm-5.3-prime        (DashScope `DASHSCOPE_API_KEY` — #1 OAK +29.4%, 0 likuidasi)
#   V3: minimax-m3           (ClassAI Router — #7 OAK Max DD terendah -6.5%, 0 liq, Rp 900/3.465)
#   V4: claude-haiku-5.5     (ClassAI Router — 1.84s cepat & hemat Rp 1.620/4.950, 1M context)
#   Meta-Aggregator (CRO):
#     1) Utama via DashScope (`DASHSCOPE_API_KEY`): glm-5.3 (#1 OAK Winner +29.4%, 0 liq)
#        -> fallback glm-5.3-prime -> glm-5.2-fast-preview -> qwen-plus -> qwen-flash
#     2) Cadangan via ClassAI Router: claude-opus-5.5 -> gpt-6.1-sol -> rule-based.
MODEL_V1 = os.getenv("DASHSCOPE_MODEL_V1", "glm-5.2-fast-preview")
MODEL_V2 = os.getenv("DASHSCOPE_MODEL", "glm-5.3-prime")
MODEL_V3 = os.getenv("GEMINI_MODEL", "minimax-m3")
MODEL_V4 = os.getenv("GEMINI_MODEL_V4", "claude-haiku-5.5")
# Model GLM via DashScope untuk mode hemat burst (bila MULTI_OPP_GLM_FALLBACK=true).
MODEL_V3_GLM = os.getenv("DASHSCOPE_MODEL_V3_GLM", "glm-5.2-fast-preview")
MODEL_V4_GLM = os.getenv("DASHSCOPE_MODEL_V4_GLM", "glm-5.3-prime")
MODEL_AGGREGATOR = os.getenv("DASHSCOPE_MODEL_AGG", "glm-5.3")
MODEL_AGG_CLASSAI = os.getenv("CLASSAI_MODEL_AGG", "claude-opus-5.5")
MODEL_AGG_CLASSAI_FALLBACK = os.getenv("CLASSAI_MODEL_AGG_FALLBACK", "gpt-6.1-sol")

CLASSAI_AGGREGATOR_CHAIN: list[str] = []
for _c in (MODEL_AGG_CLASSAI, MODEL_AGG_CLASSAI_FALLBACK):
    if _c and _c not in CLASSAI_AGGREGATOR_CHAIN:
        CLASSAI_AGGREGATOR_CHAIN.append(_c)


def should_use_glm_for_secondary_voters(
    opportunity_count: int = 0,
    is_idle: Optional[bool] = None,
) -> bool:
    """
    Tentukan apakah Voter 3 & Voter 4 dialihkan ke GLM DashScope:
    - Default (`MULTI_OPP_GLM_FALLBACK=false`): V3 (`minimax-m3`) & V4 (`claude-haiku-5.5`)
      via ClassAI Router selalu aktif agar keragaman 3-vendor tetap terjaga.
    - Jika `MULTI_OPP_GLM_FALLBACK=true`: saat banyak peluang (`opportunity_count > 1` & tidak Idle),
      alihkan V3 & V4 ke GLM via DashScope (`glm-5.2-fast-preview` & `glm-5.3-prime`).
    """
    burst_fallback = os.getenv("MULTI_OPP_GLM_FALLBACK", "false").strip().lower() in (
        "1", "true", "yes", "on"
    )
    if not burst_fallback:
        return False
    if is_idle is True:
        return False
    return int(opportunity_count or 0) > 1
# Rantai utama Meta-Aggregator via DashScope (`DASHSCOPE_API_KEY`): glm-5.3 -> fallback GLM/Qwen.
AGGREGATOR_MODEL_CHAIN: list[str] = []
for _candidate in [MODEL_AGGREGATOR] + [
    m.strip() for m in (os.getenv("DASHSCOPE_MODEL_AGG_FALLBACK") or "glm-5.3-prime,glm-5.2-fast-preview,qwen-plus,qwen-flash").split(",")
]:
    if _candidate and _candidate not in AGGREGATOR_MODEL_CHAIN:
        AGGREGATOR_MODEL_CHAIN.append(_candidate)

AGGREGATOR_CALL_TIMEOUT_SEC = 35.0
AGGREGATOR_TOTAL_BUDGET_SEC = 75.0
V2_PRIMARY_TIMEOUT_SEC = 25.0
V2_FALLBACK_TIMEOUT_SEC = 20.0
_DASHSCOPE_SEMAPHORE = threading.Semaphore(2)
_V2_TIMED_OUT_MODELS: set[str] = set()

RESEARCH_MODE_ENV = "RESEARCH_MODE"
SINGLE_ANALYST_CALL_TIMEOUT_SEC = 30.0
SINGLE_ANALYST_TOTAL_BUDGET_SEC = 45.0

SINGLE_ANALYST_CLASSAI_CHAIN: list[str] = []
for _m in (MODEL_AGG_CLASSAI, MODEL_AGG_CLASSAI_FALLBACK, MODEL_V4):
    if _m and _m not in SINGLE_ANALYST_CLASSAI_CHAIN:
        SINGLE_ANALYST_CLASSAI_CHAIN.append(_m)

SINGLE_ANALYST_DASHSCOPE_FALLBACK: list[str] = ["glm-5.2-fast-preview", "qwen-plus", "qwen-flash"]

_NO_EARNINGS_TICKERS = {
    "SPY", "QQQ", "IWM", "DIA", "XAU", "XAG", "CL", "HG", "NG",
    "BTC", "ETH", "SOL", "XRP", "BNB", "LINK", "AVAX", "AAVE",
    "SUI", "NEAR", "HYPE", "VVV", "DOGE", "ADA", "LIT",
}


def get_research_mode() -> Literal["oak_single", "swarm"]:
    """Mode riset aktif: default `swarm` (4-Voter Swarm), opsional `oak_single` (Single Deep Analyst)."""
    raw = os.getenv(RESEARCH_MODE_ENV, "swarm").strip().lower()
    if raw in ("oak_single", "single", "oak_fast"):
        return "oak_single"
    return "swarm"


def _parse_json_lenient(raw_text: str) -> dict[str, Any]:
    """Parse JSON dari respons LLM secara tangguh (<think>, markdown fence, trailing comma, raw newline)."""
    text = str(raw_text or "").strip()
    if "<think>" in text:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if text.startswith("```json"):
        text = text[7:].strip()
    elif text.startswith("```"):
        text = text[3:].strip()
    if text.endswith("```"):
        text = text[:-3].strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    try:
        parsed = json.loads(text, strict=False)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        sub = text[start:end + 1]
        sub_clean = re.sub(r",\s*([}\]])", r"\1", sub)
        parsed = json.loads(sub_clean, strict=False)
        if isinstance(parsed, dict):
            return parsed

    raise json.JSONDecodeError("Respons LLM bukan objek JSON valid", text, 0)

# P1-P7 Weights
PARAM_WEIGHTS: dict[str, float] = {
    "P1_sentiment_polarity": 0.25,
    "P2_signal_strength": 0.10,
    "P3_source_credibility": 0.10,
    "P4_momentum_direction": 0.20,
    "P5_risk_catalysts": 0.15,
    "P6_contrarian_check": 0.10,
    "P7_historical_pattern": 0.10,
}


@dataclass
class Research:
    ticker: str
    days_to_earnings: Optional[int]
    news_sentiment: Literal["bullish", "bearish", "neutral", "unknown"]
    narrative: str
    confidence: float  # 0.0 - 1.0
    fetched_at: str
    consensus: str = "SINGLE_MODEL"  # UNANIMOUS, STRONG, MIXED, SPLIT, SINGLE_MODEL, NONE
    qwen_sentiment: Optional[str] = None
    gemini_sentiment: Optional[str] = None
    qwen_narrative: Optional[str] = None
    gemini_narrative: Optional[str] = None
    qwen_confidence: Optional[float] = None
    gemini_confidence: Optional[float] = None
    # Extended Swarm Fields (PRD 3.5)
    final_score: float = 0.0
    overall_consensus_level: str = "SINGLE_MODEL"
    vendor_alignment: str = "split"
    most_influential_parameter: str = "P1_sentiment_polarity"
    disagreement_summary: list[str] = field(default_factory=list)
    p1_to_p7_scores: dict[str, float] = field(default_factory=dict)
    voters_count: int = 0
    voter_verdicts: dict[str, Any] = field(default_factory=dict)
    aggregator_details: dict[str, Any] = field(default_factory=dict)
    # Ronde 7: kegagalan input TIDAK BOLEH senyap. Daftar voter yang gagal (nama voter) dan
    # status sumber kalender earnings dipakai pemanggil untuk agregasi + Step Summary, sehingga
    # siklus 3/4 voter tidak lagi dilaporkan sebagai siklus sehat.
    voter_failures: list[str] = field(default_factory=list)
    earnings_status: str = "unknown"  # unknown | ok | no_data | past_only | http_error:<kode> | exception:<tipe> | no_crumb
    secondary_provider: str = "gemini"  # "oak_single" | "gemini" (Idle) | "glm" (multi-opportunity > 1)
    headlines: list[str] = field(default_factory=list)
    oak_context_block: str = ""
    analyst_thinking: str = ""


# =============================================================================
# API CLIENTS
# =============================================================================

def resolve_dashscope_base_url(api_key: str, base_url: Optional[str] = None) -> str:
    """Pilih endpoint DashScope OpenAI-compatible:
    1) Argumen `base_url` eksplisit (bila diisi).
    2) Env `DASHSCOPE_BASE_URL` (bila diisi non-kosong).
    3) Auto-detect Token Plan: key berawalan `sk-sp-` -> `DASHSCOPE_TOKEN_PLAN_BASE`.
    4) Default pay-as-you-go `DASHSCOPE_BASE`.
    """
    if base_url and base_url.strip():
        return base_url.strip().rstrip("/")
    env_url = (os.getenv("DASHSCOPE_BASE_URL") or "").strip()
    if env_url:
        return env_url.rstrip("/")
    if (api_key or "").strip().startswith("sk-sp-"):
        return DASHSCOPE_TOKEN_PLAN_BASE
    return DASHSCOPE_BASE


class QwenClient:
    """Client untuk Alibaba Cloud Qwen via DashScope OpenAI-compatible endpoint."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = MODEL_V2,
        base_url: Optional[str] = None,
    ):
        self.api_key = (api_key or os.environ.get("DASHSCOPE_API_KEY", "")).strip()
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_KEY belum di-set!")
        self.model = model
        self.base_url = resolve_dashscope_base_url(self.api_key, base_url=base_url)
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    def chat_json(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        """Kirim request ke DashScope dengan response_format JSON (mendukung Qwen & GLM)."""
        target_model = model or self.model
        is_real_http = isinstance(self.client, httpx.Client)
        active_model = target_model
        if is_real_http and active_model == MODEL_V2:
            if active_model in _V2_TIMED_OUT_MODELS and MODEL_V1 and MODEL_V1 != MODEL_V2:
                active_model = MODEL_V1
                timeout = float(timeout) if timeout is not None else V2_FALLBACK_TIMEOUT_SEC
            else:
                timeout = min(float(timeout), V2_PRIMARY_TIMEOUT_SEC) if timeout is not None else V2_PRIMARY_TIMEOUT_SEC

        payload = {
            "model": active_model,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        post_kwargs: dict[str, Any] = {"json": payload}
        if timeout is not None:
            post_kwargs["timeout"] = float(timeout)

        def _do_post(req_payload: dict[str, Any]) -> Any:
            post_kwargs["json"] = req_payload
            if is_real_http:
                with _DASHSCOPE_SEMAPHORE:
                    return self.client.post("/chat/completions", **post_kwargs)
            return self.client.post("/chat/completions", **post_kwargs)

        def _post_and_parse(req_payload: dict[str, Any]) -> dict[str, Any]:
            r = _do_post(req_payload)

            # Fallback otomatis V1 -> V2 dan V4 GLM -> V3 GLM saat 404/429 upstream.
            if r.status_code in (404, 429) and req_payload.get("model") == MODEL_V1:
                reason = "rate limited (429)" if r.status_code == 429 else "model not found (404)"
                log.warning(f"Qwen model {MODEL_V1} {reason}. Fallback ke {MODEL_V2}...")
                req_payload["model"] = MODEL_V2
                r = _do_post(req_payload)
            elif r.status_code in (404, 429) and req_payload.get("model") == MODEL_V4_GLM and MODEL_V3_GLM != MODEL_V4_GLM:
                reason = "rate limited (429)" if r.status_code == 429 else "model not found (404)"
                log.warning(f"GLM model {MODEL_V4_GLM} {reason}. Fallback ke {MODEL_V3_GLM}...")
                req_payload["model"] = MODEL_V3_GLM
                r = _do_post(req_payload)

            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            return _parse_json_lenient(content)

        try:
            return _post_and_parse(payload)
        except (json.JSONDecodeError, httpx.TimeoutException) as exc:
            if active_model == MODEL_V2 and MODEL_V1 and MODEL_V1 != MODEL_V2:
                if is_real_http and isinstance(exc, httpx.TimeoutException):
                    _V2_TIMED_OUT_MODELS.add(target_model)
                if is_real_http:
                    post_kwargs["timeout"] = V2_FALLBACK_TIMEOUT_SEC
                log.info(f"DashScope {active_model} retry cepat via {MODEL_V1} ({type(exc).__name__})...")
                payload["model"] = MODEL_V1
                try:
                    return _post_and_parse(payload)
                except httpx.TimeoutException:
                    if is_real_http and MODEL_V1 != "qwen-plus":
                        log.info("DashScope %s timeout -> fallback cepat ke qwen-plus...", MODEL_V1)
                        payload["model"] = "qwen-plus"
                        return _post_and_parse(payload)
                    raise
            raise


class GeminiClient:
    """Client untuk Voter Gemini via OpenAI-compatible router (default: https://router.classai.id/v1)
    dengan dukungan fallback otomatis untuk endpoint Google native generateContent bila base_url
    diarahkan ke `generativelanguage.googleapis.com`."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = MODEL_V3,
        base_url: Optional[str] = None,
    ):
        self.api_key = (
            api_key
            or os.environ.get("DASHSCOPE_API_GEMINI_KEY")
            or os.environ.get("GEMINI_API_KEY", "")
        ).strip()
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_GEMINI_KEY / GEMINI_API_KEY belum di-set!")

        self.base_url = (
            base_url
            or os.environ.get("GEMINI_BASE_URL")
            or GEMINI_BASE
        ).strip().rstrip("/")
        self.model = self._clean_model_name(model or MODEL_V3)
        self.client = httpx.Client(timeout=45.0)

    @staticmethod
    def _clean_model_name(model_name: str) -> str:
        name = model_name.strip()
        if name.startswith("models/"):
            name = name[7:]
        if name.endswith(":generateContent"):
            name = name[:-16]
        return name

    def _is_google_native(self) -> bool:
        return "generativelanguage.googleapis.com" in self.base_url.lower()

    def chat_json(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        """Kirim prompt ke ClassAI Router OpenAI-compatible (`/chat/completions`) atau Google native."""
        target_model = self._clean_model_name(model or self.model)
        req_timeout = float(timeout) if timeout is not None else 45.0

        if self._is_google_native():
            url = f"{self.base_url}/models/{target_model}:generateContent"
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            }
            contents_text = prompt
            if system_instruction:
                contents_text = f"[SYSTEM INSTRUCTION]\n{system_instruction}\n\n[USER REQUEST]\n{prompt}"
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": f"{contents_text}\n\nOUTPUT HANYA JSON VALID SESUAI SKEMA."}
                        ]
                    }
                ],
                "generationConfig": {
                    "temperature": 0.2,
                    "responseMimeType": "application/json",
                },
            }
            r = self.client.post(url, headers=headers, json=payload, timeout=req_timeout)
            r.raise_for_status()
            return self._parse_response(r.json())

        # Default: OpenAI-compatible endpoint (https://router.classai.id/v1/chat/completions)
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "WaltBot/1.0",
        }
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({
            "role": "user",
            "content": f"{prompt}\n\nOUTPUT HANYA JSON VALID SESUAI SKEMA.",
        })
        payload = {
            "model": target_model,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        try:
            r = self.client.post(url, headers=headers, json=payload, timeout=req_timeout)

            # Pengaman anti-404: bila env/variable lama masih memakai nama model Google native,
            # otomatis fallback ke model fallback ClassAI.
            if getattr(r, "status_code", 200) == 404 and target_model != GEMINI_CLASSAI_FALLBACK_MODEL:
                log.warning(
                    f"Gemini router model '{target_model}' tidak ditemukan (404) di {self.base_url}. "
                    f"Fallback ke {GEMINI_CLASSAI_FALLBACK_MODEL}..."
                )
                payload["model"] = GEMINI_CLASSAI_FALLBACK_MODEL
                r = self.client.post(url, headers=headers, json=payload, timeout=req_timeout)

            r.raise_for_status()
            return self._parse_response(r.json())
        except (json.JSONDecodeError, httpx.HTTPError) as exc:
            if payload.get("model") != GEMINI_CLASSAI_FALLBACK_MODEL:
                log.info(
                    f"ClassAI {payload.get('model')} gagal parse/HTTP ({type(exc).__name__}), "
                    f"retry 1x via {GEMINI_CLASSAI_FALLBACK_MODEL}..."
                )
                payload["model"] = GEMINI_CLASSAI_FALLBACK_MODEL
                r = self.client.post(url, headers=headers, json=payload, timeout=req_timeout)
                r.raise_for_status()
                return self._parse_response(r.json())
            raise

    def _parse_response(self, res_data: dict) -> dict:
        text = ""
        choices = res_data.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") or {}
            text = str(msg.get("content") or "").strip()
        else:
            candidates = res_data.get("candidates", [])
            if not candidates:
                raise RuntimeError(f"Gemini API empty response: {res_data}")
            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                raise RuntimeError(f"Gemini API empty parts: {res_data}")
            text = str(parts[0].get("text") or "").strip()

        if not text:
            raise RuntimeError(f"Gemini API empty text content: {res_data}")

        return _parse_json_lenient(text)


# =============================================================================
# DATA FETCHING UTILITIES
# =============================================================================

_CRYPTO_NEWS_TICKERS: frozenset[str] = frozenset({
    "BTC", "ETH", "SOL", "BNB", "XRP", "AVAX", "SUI", "LINK", "NEAR", "AAVE",
    "ARB", "OP", "DOGE", "ADA", "DOT", "ATOM", "LTC", "UNI", "APT", "SEI", "HYPE",
})


def fetch_headlines(ticker: str, limit: int = 10) -> list[str]:
    """Ambil headlines berita 7 hari terakhir dari Yahoo Finance (fallback `<TICKER>-USD` untuk crypto)."""
    sym = str(ticker or "").strip().upper()
    queries = [f"{sym}-USD", f"{sym} crypto", sym] if sym in _CRYPTO_NEWS_TICKERS else [ticker]
    for query in queries:
        try:
            r = httpx.get(
                "https://query1.finance.yahoo.com/v1/finance/search",
                params={"q": query, "newsCount": limit},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15.0,
            )
            r.raise_for_status()
            news = r.json().get("news", [])
            titles = [n.get("title", "") for n in news[:limit] if n.get("title")]
            if titles:
                return titles
        except Exception as e:
            log.warning(f"fetch_headlines {ticker} (q={query}): {e}")
    return []


# --- Sesi Yahoo bersama (cookie + crumb) -------------------------------------
# Ronde 7: `v10/quoteSummary` menolak tanpa crumb (401 "Invalid Crumb") — inilah sebabnya
# proteksi "earnings H-7" buta data. Crumb TERIKAT pada cookie sesi: probe 2026-11-20
# membuktikan crumb yang sama + klien baru tetap 401. Karena itu cookie dan crumb diambil
# pada SATU klien yang dipakai ulang sepanjang proses.
YAHOO_BASE = "https://query1.finance.yahoo.com"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
_yahoo_state: Optional[tuple[Optional[httpx.Client], Optional[str]]] = None


def _yahoo_session() -> tuple[Optional[httpx.Client], Optional[str]]:
    """Bangun (klien ber-cookie, crumb) sekali per proses; (None, None) bila crumb tak diperoleh."""
    global _yahoo_state
    if _yahoo_state is not None:
        return _yahoo_state
    client: Optional[httpx.Client] = None
    try:
        client = httpx.Client(headers=YAHOO_HEADERS, timeout=15.0, follow_redirects=True)
        client.get("https://fc.yahoo.com")  # status diabaikan; yang dicari header Set-Cookie
        r = client.get(f"{YAHOO_BASE}/v1/test/getcrumb")
        crumb = r.text.strip() if r.status_code == 200 else None
        _yahoo_state = (client, crumb or None)
    except Exception as exc:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        log.warning(f"_yahoo_session gagal: {type(exc).__name__}: {str(exc)[:120]}")
        _yahoo_state = (None, None)
    return _yahoo_state


def _parse_next_earnings_days(payload: dict, now: datetime) -> tuple[Optional[int], str]:
    """Ambil tanggal earnings BERIKUTNYA. Status: ok | no_data | past_only.

    `past_only` penting: bila jadwal berikutnya belum dipublikasi, Yahoo mengisi slot
    "earningsDate" dengan tanggal rilis TERAKHIR (dibuktikan NVDA 2026-11-20: 2026-11-17).
    `max(0, hari)` atas tanggal seperti itu akan menjadi 0 dan gerbang "earnings H-7" mem-SKIP aset
    itu selamanya — jadi tanggal di masa lalu BUKAN "H-0", melainkan "belum diketahui".
    """
    events = ((payload.get("quoteSummary") or {}).get("result") or [{}])[0].get("calendarEvents") or {}
    earnings = events.get("earnings") or {}
    if not earnings:
        return None, "no_data"
    parsed: list[datetime] = []
    for item in earnings.get("earningsDate") or []:
        ts = item.get("raw") if isinstance(item, dict) else item
        try:
            parsed.append(datetime.fromtimestamp(float(ts)))
        except (TypeError, ValueError, OSError):
            continue
    if not parsed:
        return None, "no_data"
    future = [d for d in parsed if d >= now]
    if not future:
        return None, "past_only"
    return max(0, (min(future) - now).days), "ok"


def fetch_earnings_days_with_status(
    ticker: str,
    client: Optional[httpx.Client] = None,
    crumb: Optional[str] = None,
) -> tuple[Optional[int], str]:
    """Hari menuju earnings berikutnya + status sumber yang jujur.

    Status: `ok`, `no_data` (mis. ETF memang tanpa earnings), `past_only` (jadwal berikutnya
    belum dipublikasi), `no_crumb`, `http_error:<kode>`, `exception:<tipe>`. Tiga terakhir
    = gangguan sumber data dan wajib dialarm; dua pertama = kondisi data, bukan outage.
    """
    if client is None:
        client, crumb = _yahoo_session()
    if client is None or not crumb:
        return None, "no_crumb"
    try:
        r = client.get(
            f"{YAHOO_BASE}/v10/finance/quoteSummary/{ticker}",
            params={"modules": "calendarEvents", "crumb": crumb},
        )
        if r.status_code == 404:
            # Bukan gangguan: instrumen tanpa kalender earnings (ETF seperti SPY) memang
            # menjawab "Not Found" di quoteSummary. 401/403/5xx tetap dihitung outage.
            log.info(f"fetch_earnings_days {ticker}: tidak ada kalender earnings (HTTP 404)")
            return None, "no_data"
        if r.status_code != 200:
            log.warning(f"fetch_earnings_days {ticker}: HTTP {r.status_code}")
            return None, f"http_error:{r.status_code}"
        return _parse_next_earnings_days(r.json(), datetime.now())
    except Exception as exc:
        log.warning(f"fetch_earnings_days {ticker}: {type(exc).__name__}: {str(exc)[:120]}")
        return None, f"exception:{type(exc).__name__}"


def fetch_earnings_days(ticker: str) -> Optional[int]:
    """Kontrak lama dipertahankan: hanya jumlah hari (None bila tidak diketahui)."""
    return fetch_earnings_days_with_status(ticker)[0]


_ORIG_FETCH_EARNINGS_WITH_STATUS = fetch_earnings_days_with_status


# =============================================================================
# 4-VOTER SWARM IMPLEMENTATION (P1 - P7 VALIDATION)
# =============================================================================

UNIFIED_VOTER_SCHEMA_PROMPT = """
ATURAN PENILAIAN TAKTIS (WAJIB DIPATUHI):
1. Horizon Analisa: Fokus pada dampak harga 24-72 JAM ke depan (strategi Perpetual Funding Carry dengan SL 1.5% & TP 2.0%-2.5%), BUKAN tesis investasi jangka panjang 1-5 tahun.
2. Rubrik Kalibrasi Skor P1 & P4 (-1.0 s/d +1.0):
   - +0.70 s/d +1.00 : Katalis positif kuat & terkonfirmasi (beat earnings besar, upgrade institusi, breakout makro/ETF).
   - +0.25 s/d +0.69 : Sentimen positif moderat / pemulihan bertahap.
   - -0.20 s/d +0.20 : Netral, berita campuran (mixed), atau tidak berdampak signifikan ke harga 1-3 hari.
   - -0.69 s/d -0.25 : Tekanan jual moderat, downgrade ringan, atau kekhawatiran makro.
   - -1.00 s/d -0.70 : Katalis negatif berat (miss earnings, regulasi keras, hack/insiden, crash).
3. Anti-Halusinasi: Jika headline tidak relevan dengan ticker, hanya sedikit (<2 berita relevan), atau tidak ada katalis jelas, WAJIB beri P1 di rentang [-0.10, +0.10] dan overall_voter_confidence <= 0.40. JANGAN mengarang tanggal earnings atau event yang tidak tertulis di headline.

Score 7 parameter validasi P1-P7 secara objektif dalam format JSON valid berikut:
{
  "P1_sentiment_polarity": {
    "score": -1.0 to 1.0,
    "reasoning": "1-2 kalimat analisa sentimen jangka pendek (24-72 jam) berdasarkan fakta headline",
    "metadata": {"dominant_tone": "bullish|bearish|neutral", "confidence_level": 0.0 to 1.0}
  },
  "P2_signal_strength": {
    "score": 0.0 to 1.0,
    "reasoning": "1-2 kalimat tingkat keselarasan headlines (1.0 = semua searah, 0.0 = saling bertentangan)",
    "metadata": {"aligned_count": 0, "total_count": 0}
  },
  "P3_source_credibility": {
    "score": 0.0 to 1.0,
    "reasoning": "1-2 kalimat evaluasi reputasi sumber (Reuters/Bloomberg/WSJ/CNBC = tinggi, blog/opini = rendah)",
    "metadata": {"tier1_count": 0, "tier2_count": 0}
  },
  "P4_momentum_direction": {
    "score": -1.0 to 1.0,
    "reasoning": "1-2 kalimat arah momentum berita 48 jam terakhir vs 7 hari (menguat/melemah/berbalik)",
    "metadata": {"recent_48h_tone": "accelerating|fading|reversing"}
  },
  "P5_risk_catalysts": {
    "score": 0.0 to 1.0,
    "reasoning": "1-2 kalimat risiko lonjakan volatilitas mendadak 1-3 hari (earnings, FOMC, CPI, regulasi, short squeeze)",
    "metadata": {"catalyst_list": [], "days_to_nearest": 0}
  },
  "P6_contrarian_check": {
    "score": 0.0 to 1.0,
    "reasoning": "1-2 kalimat apakah posisi pasar sudah terlalu ramai (crowded) sehingga rawan pembalikan arah",
    "metadata": {"crowdedness_level": "low|medium|high"}
  },
  "P7_historical_pattern": {
    "score": -1.0 to 1.0,
    "reasoning": "1-2 kalimat analogi pola reaksi harga historis dalam 1-3 hari untuk katalis serupa",
    "metadata": {"historical_outcome": "bullish|bearish|mixed"}
  },
  "overall_voter_confidence": 0.0 to 1.0,
  "voter_id": "{voter_id}"
}
"""


def _build_strategy_context_block(
    ticker: str,
    funding_daily: Optional[float] = None,
    recommended_side: Optional[str] = None,
    grav_pressure: Optional[float] = None,
) -> str:
    if funding_daily is None and not recommended_side:
        return (
            f"Konteks Strategi: Evaluasi arah harga taktis 24-72 jam untuk {ticker} "
            "(strategi Perpetual Funding Carry dengan SL 1.5% & TP 2.0%-2.5%)."
        )
    fr_txt = f"{funding_daily * 100:+.3f}%/hari" if funding_daily is not None else "N/A"
    side_txt = (recommended_side or "").upper() or "N/A"
    risk_note = (
        "Waspadai katalis reli mendadak / short squeeze yang bisa memicu SL 1.5%."
        if side_txt == "SHORT"
        else "Waspadai katalis aksi jual / flash crash yang bisa memicu SL 1.5%."
    )
    grav_txt = ""
    if grav_pressure is not None:
        try:
            from strategy_config import load_strategy_config
            _cfg = load_strategy_config()
            _lt = float(_cfg.get("grav_long_min_bull_pressure", 0.0) or 0.0)
            _st = float(_cfg.get("grav_short_min_bear_pressure", 0.0) or 0.0)
            _min_s = float(_cfg.get("grav_boost_min_score", 80.0) or 80.0)
            _max_p = float(_cfg.get("grav_boost_max_pct", 0.25) or 0.25)
            _curve = float(_cfg.get("grav_boost_curve", 1.5) or 1.5)
        except Exception:
            _lt = _st = 0.0
            _min_s, _max_p, _curve = 80.0, 0.25, 1.5
        bias = "BULLISH (magnet atas dominan)" if grav_pressure >= 55.0 else (
            "BEARISH (magnet bawah dominan)" if grav_pressure <= 45.0 else "SEIMBANG")
        gate_txt = (
            f"Ambang arah: LONG bila bull >= {_lt:.0f}, SHORT bila bear >= {_st:.0f}. "
            f"Pengaruh struktur BARU aktif bila skor > {_min_s:.0f}, lalu menguat mengikuti kurva "
            f"^{_curve:g} hingga maksimum +{_max_p * 100:.0f}% keyakinan pada skor 100."
            if (_lt > 0 or _st > 0) else
            f"Ambang arah default 50/100; pengaruh baru aktif bila skor > {_min_s:.0f} (kurva ^{_curve:g})."
        )
        grav_txt = (
            f" Data Market Gravity terkini: bullish_pressure={grav_pressure:.1f}/100 "
            f"(bearish {100.0 - grav_pressure:.1f}/100, total selalu 100) — struktur {bias}. {gate_txt} "
            "PEDOMAN: berita + OAKresearch MCP adalah pemutus UTAMA; bila kesimpulanmu searah struktur ini, "
            "tingkatkan keyakinan P4; bila berlawanan, sampaikan alasan kuat dari berita/MCP — "
            "struktur hanya PENGUAT keputusan, bukan penentu."
        )
    return (
        f"Konteks Strategi: Bot sedang mempertimbangkan posisi {side_txt} pada {ticker} "
        f"untuk memanen funding rate ({fr_txt}) dengan SL 1.5% & TP 2.0%-2.5% (horizon 24-72 jam). "
        f"{risk_note}{grav_txt}"
    )


def compute_directional_final_score(per_param_final: dict[str, Any]) -> float:
    """
    Hitung final_score arah sentimen murni (-1.0 bearish s/d +1.0 bullish) tanpa bias positif:
    - Arah ditentukan hanya oleh parameter bertanda: P1 (45%), P4 (35%), P7 (20%).
    - P2 (signal strength) & P3 (source credibility) bertindak sebagai pengali kualitas [0.5 .. 1.0].
    - P5 (risk catalysts) & P6 (contrarian) tetap menjadi guard risiko terpisah (tidak menarik skor ke +).
    """
    def _g(name: str, default: float = 0.0) -> float:
        obj = per_param_final.get(name) or {}
        val = obj.get("final_score", obj.get("score", default))
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    p1 = max(-1.0, min(1.0, _g("P1_sentiment_polarity", 0.0)))
    p4 = max(-1.0, min(1.0, _g("P4_momentum_direction", 0.0)))
    p7 = max(-1.0, min(1.0, _g("P7_historical_pattern", 0.0)))
    p2 = max(0.0, min(1.0, _g("P2_signal_strength", 0.5)))
    p3 = max(0.0, min(1.0, _g("P3_source_credibility", 0.5)))

    raw_dir = (0.45 * p1) + (0.35 * p4) + (0.20 * p7)
    quality_mult = 0.5 + 0.5 * ((p2 + p3) / 2.0)
    return round(max(-1.0, min(1.0, raw_dir * quality_mult)), 3)


def _normalize_score(val: Any, default: float, min_val: float, max_val: float) -> float:
    try:
        f = float(val)
        return max(min_val, min(max_val, f))
    except Exception:
        return default


def _extract_voter_params(raw: dict, voter_id: str) -> dict[str, Any]:
    """Normalisasi hasil voter agar sesuai dengan struktur parameter P1-P7."""
    extracted = {
        "voter_id": voter_id,
        "overall_voter_confidence": _normalize_score(raw.get("overall_voter_confidence"), 0.5, 0.0, 1.0),
        "params": {},
    }

    param_ranges = {
        "P1_sentiment_polarity": (-1.0, 1.0, 0.0),
        "P2_signal_strength": (0.0, 1.0, 0.5),
        "P3_source_credibility": (0.0, 1.0, 0.5),
        "P4_momentum_direction": (-1.0, 1.0, 0.0),
        "P5_risk_catalysts": (0.0, 1.0, 0.2),
        "P6_contrarian_check": (0.0, 1.0, 0.3),
        "P7_historical_pattern": (-1.0, 1.0, 0.0),
    }

    for param_name, (min_v, max_v, def_v) in param_ranges.items():
        p_obj = raw.get(param_name, {})
        if not isinstance(p_obj, dict):
            p_obj = {"score": def_v, "reasoning": str(p_obj), "metadata": {}}
        score = _normalize_score(p_obj.get("score"), def_v, min_v, max_v)
        reasoning = str(p_obj.get("reasoning", "")).strip()
        metadata = p_obj.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        extracted["params"][param_name] = {
            "score": score,
            "reasoning": reasoning,
            "metadata": metadata,
        }

    return extracted


def run_voter_1(
    qwen: QwenClient,
    ticker: str,
    headlines: list[str],
    strategy_context: str = "",
) -> Optional[dict]:
    """Voter 1: Qwen-Turbo (Quick Signal Analyst) — Fokus keyword scanning dan surface polarity."""
    sys_prompt = (
        "Kamu Voter 1: Quick Signal Analyst. Tugasmu adalah analisa cepat (<2 detik) "
        "terhadap headline berita pasar modal untuk horizon 24-72 jam. Fokus pada deteksi katalis riil dan polaritas."
    )
    user_prompt = (
        f"Ticker: {ticker}\n"
        f"{strategy_context}\n"
        f"Headlines 7 hari:\n" + "\n".join(f"- {h}" for h in headlines) + "\n\n"
        + UNIFIED_VOTER_SCHEMA_PROMPT.replace("{voter_id}", "V1_qwen_turbo")
    )
    try:
        raw = qwen.chat_json(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
            model=MODEL_V1,
        )
        return _extract_voter_params(raw, "V1_qwen_turbo")
    except Exception as e:
        log.warning(f"Voter 1 (Qwen-Turbo) error for {ticker}: {e}")
        return None


def run_voter_2(
    qwen: QwenClient,
    ticker: str,
    headlines: list[str],
    strategy_context: str = "",
) -> Optional[dict]:
    """Voter 2: Qwen-Plus (Balanced Equity Analyst) — Fokus kredibilitas sumber & konsistensi narasi."""
    sys_prompt = (
        "Kamu Voter 2: Balanced Equity Analyst. Weight headlines berdasarkan kredibilitas sumber "
        "(Reuters/Bloomberg/WSJ bernilai lebih tinggi) untuk horizon 24-72 jam. Identifikasi konsistensi narasi dan kontradiksi."
    )
    user_prompt = (
        f"Ticker: {ticker}\n"
        f"{strategy_context}\n"
        f"Headlines 7 hari:\n" + "\n".join(f"- {h}" for h in headlines) + "\n\n"
        + UNIFIED_VOTER_SCHEMA_PROMPT.replace("{voter_id}", "V2_qwen_plus")
    )
    try:
        raw = qwen.chat_json(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
            model=MODEL_V2,
        )
        return _extract_voter_params(raw, "V2_qwen_plus")
    except Exception as e:
        log.warning(f"Voter 2 (Qwen-Plus) error for {ticker}: {e}")
        return None


def run_voter_3(
    gemini: GeminiClient,
    ticker: str,
    headlines: list[str],
    strategy_context: str = "",
) -> Optional[dict]:
    """Voter 3: Gemini-Flash (Realtime Web Validator) — Fokus breaking news 24 jam & katalis terkini."""
    sys_prompt = (
        "Kamu Voter 3: Realtime Market Validator Google Gemini. Evaluasi apakah ada breaking news atau "
        "katalis makro 24-48 jam terakhir yang mengonfirmasi atau membalikkan tren berita 7 hari."
    )
    user_prompt = (
        f"Ticker: {ticker}\n"
        f"{strategy_context}\n"
        f"Headlines 7 hari terakhir:\n" + "\n".join(f"- {h}" for h in headlines) + "\n\n"
        + UNIFIED_VOTER_SCHEMA_PROMPT.replace("{voter_id}", "V3_gemini_flash")
    )
    try:
        raw = gemini.chat_json(user_prompt, system_instruction=sys_prompt, model=MODEL_V3)
        return _extract_voter_params(raw, "V3_gemini_flash")
    except Exception as e:
        log.warning(f"Voter 3 (Gemini-Flash) error for {ticker}: {e}")
        return None


def run_voter_4(
    gemini: GeminiClient,
    ticker: str,
    headlines: list[str],
    strategy_context: str = "",
) -> Optional[dict]:
    """Voter 4: Gemini-Pro (Deep Multi-Angle Reasoner) — Analisa sebab-akibat, contrarian check, & hidden risks."""
    sys_prompt = (
        "Kamu Voter 4: Senior Portfolio Manager & Deep Multi-Angle Reasoner Google Gemini. "
        "Tugasmu menganalisa rantai kausalitas 1-3 hari, risiko tersembunyi (hidden risks), posisi crowded (contrarian), "
        "dan preseden historis agar bot terhindar dari jebakan harga."
    )
    user_prompt = (
        f"Ticker: {ticker}\n"
        f"{strategy_context}\n"
        f"Headlines 7 hari terakhir:\n" + "\n".join(f"- {h}" for h in headlines) + "\n\n"
        + UNIFIED_VOTER_SCHEMA_PROMPT.replace("{voter_id}", "V4_gemini_pro")
    )
    try:
        raw = gemini.chat_json(user_prompt, system_instruction=sys_prompt, model=MODEL_V4)
        return _extract_voter_params(raw, "V4_gemini_pro")
    except Exception as e:
        log.warning(f"Voter 4 (Gemini-Pro) error for {ticker}: {e}")
        return None


def run_voter_3_glm(
    qwen: QwenClient,
    ticker: str,
    headlines: list[str],
    strategy_context: str = "",
) -> Optional[dict]:
    """Voter 3 (Multi-Peluang): GLM Fast (`glm-5.2-fast-preview`) via DashScope (pengganti Gemini Flash)."""
    sys_prompt = (
        "Kamu Voter 3: Realtime Market Validator (GLM Fast). Evaluasi apakah ada breaking news atau "
        "katalis makro 24-48 jam terakhir yang mengonfirmasi atau membalikkan tren berita 7 hari."
    )
    user_prompt = (
        f"Ticker: {ticker}\n"
        f"{strategy_context}\n"
        f"Headlines 7 hari terakhir:\n" + "\n".join(f"- {h}" for h in headlines) + "\n\n"
        + UNIFIED_VOTER_SCHEMA_PROMPT.replace("{voter_id}", "V3_glm_fast")
    )
    try:
        raw = qwen.chat_json(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
            model=MODEL_V3_GLM,
        )
        return _extract_voter_params(raw, "V3_glm_fast")
    except Exception as e:
        log.warning(f"Voter 3 ({MODEL_V3_GLM}) error for {ticker}: {e}")
        return None


def run_voter_4_glm(
    qwen: QwenClient,
    ticker: str,
    headlines: list[str],
    strategy_context: str = "",
) -> Optional[dict]:
    """Voter 4 (Multi-Peluang): GLM Prime (`glm-5.3-prime`) via DashScope (pengganti Gemini Pro)."""
    sys_prompt = (
        "Kamu Voter 4: Senior Portfolio Manager & Deep Multi-Angle Reasoner (GLM Prime). "
        "Tugasmu menganalisa rantai kausalitas 1-3 hari, risiko tersembunyi (hidden risks), posisi crowded (contrarian), "
        "dan preseden historis agar bot terhindar dari jebakan harga."
    )
    user_prompt = (
        f"Ticker: {ticker}\n"
        f"{strategy_context}\n"
        f"Headlines 7 hari terakhir:\n" + "\n".join(f"- {h}" for h in headlines) + "\n\n"
        + UNIFIED_VOTER_SCHEMA_PROMPT.replace("{voter_id}", "V4_glm_prime")
    )
    try:
        raw = qwen.chat_json(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
            model=MODEL_V4_GLM,
        )
        return _extract_voter_params(raw, "V4_glm_prime")
    except Exception as e:
        log.warning(f"Voter 4 ({MODEL_V4_GLM}) error for {ticker}: {e}")
        return None


# =============================================================================
# PRE-PROCESSING & VARIANCE ANALYSIS (RULE-BASED)
# =============================================================================

def analyze_voter_variance(voter_verdicts: dict[str, dict]) -> dict[str, Any]:
    """
    Hitung mean dan variance per-parameter across responding voters.
    Klasifikasi level konsensus:
      - variance < 0.05: UNANIMOUS
      - variance < 0.15: STRONG
      - variance < 0.30: MIXED
      - variance >= 0.30: SPLIT
    """
    param_analysis = {}
    voters = list(voter_verdicts.values())
    voters_count = len(voters)

    if voters_count == 0:
        return {}

    for param_name in PARAM_WEIGHTS.keys():
        scores = [v["params"][param_name]["score"] for v in voters if param_name in v.get("params", {})]
        if not scores:
            scores = [0.0]

        mean_val = sum(scores) / len(scores)
        variance_val = sum((s - mean_val) ** 2 for s in scores) / len(scores)

        if variance_val < 0.05:
            consensus_level = "UNANIMOUS"
        elif variance_val < 0.15:
            consensus_level = "STRONG"
        elif variance_val < 0.30:
            consensus_level = "MIXED"
        else:
            consensus_level = "SPLIT"

        outlier_voter = None
        if variance_val > 0.15 and voters_count > 2:
            outlier_entry = max(
                voter_verdicts.items(),
                key=lambda kv: abs(kv[1]["params"].get(param_name, {}).get("score", mean_val) - mean_val)
            )
            outlier_voter = outlier_entry[0]

        param_analysis[param_name] = {
            "avg": round(mean_val, 3),
            "variance": round(variance_val, 4),
            "consensus_level": consensus_level,
            "outlier_voter": outlier_voter,
        }

    return param_analysis


# =============================================================================
# META-AGGREGATOR (QWEN-MAX & FALLBACK)
# =============================================================================

AGGREGATOR_PROMPT = """
Kamu adalah Chief Risk Officer dan Meta-Aggregator portofolio finansial.
Tugasmu adalah menganalisis evaluasi parameter P1-P7 dari 4 voter AI (2 Qwen Alibaba + 2 Gemini Google).

Ticker: {ticker}
Per-Parameter Variance Analysis:
{pre_analysis_json}

Detail Evaluasi Voter:
{raw_voters_json}

Tugasmu:
1. Untuk parameter dengan konsensus SPLIT atau MIXED: teliti apakah outlier memiliki alasan realtime/kredibel yang valid atau sekadar noise. Tentukan trusted_voter dan final_score untuk parameter tersebut.
2. Untuk parameter dengan konsensus UNANIMOUS atau STRONG: gunakan rata-rata konsensus sebagai final_score parameter tersebut.
3. Hitung final_score arah sentimen murni (-1.0 bearish s/d +1.0 bullish) tanpa bias positif:
   - Arah dasar: (0.45 × P1_sentiment_polarity) + (0.35 × P4_momentum_direction) + (0.20 × P7_historical_pattern)
   - Pengali kualitas: 0.5 + 0.5 × ((P2_signal_strength + P3_source_credibility) / 2)
   - final_score = arah_dasar × pengali_kualitas
   - P5_risk_catalysts dan P6_contrarian_check dievaluasi sebagai guard risiko terpisah (JANGAN menambahkannya ke arah sentimen).
4. Berikan justifikasi komprehensif dalam Bahasa Indonesia (1-2 paragraf padat) yang menyebut apakah aman/berisiko untuk horizon 24-72 jam.

Output HANYA JSON valid dengan skema:
{
  "per_parameter_final": {
    "P1_sentiment_polarity": {"final_score": 0.0, "trusted_voter": "CONSENSUS|V1|V2|V3|V4", "rationale": "..."},
    "P2_signal_strength": {"final_score": 0.0, "trusted_voter": "...", "rationale": "..."},
    "P3_source_credibility": {"final_score": 0.0, "trusted_voter": "...", "rationale": "..."},
    "P4_momentum_direction": {"final_score": 0.0, "trusted_voter": "...", "rationale": "..."},
    "P5_risk_catalysts": {"final_score": 0.0, "trusted_voter": "...", "rationale": "..."},
    "P6_contrarian_check": {"final_score": 0.0, "trusted_voter": "...", "rationale": "..."},
    "P7_historical_pattern": {"final_score": 0.0, "trusted_voter": "...", "rationale": "..."}
  },
  "final_score": -1.0 to 1.0,
  "overall_confidence": 0.0 to 1.0,
  "overall_consensus_level": "unanimous|strong|mixed|split",
  "vendor_alignment": "both_agree|qwen_dominant|gemini_dominant|split",
  "most_influential_parameter": "P1_sentiment_polarity|P4_momentum_direction|...",
  "justification": "Paragraf ringkasan keputusan...",
  "disagreement_summary": ["poin 1", "poin 2"]
}
"""


def _compact_voters_for_aggregator(voter_verdicts: dict[str, dict]) -> dict[str, dict]:
    """Ringkas output voter agar prompt Meta-Aggregator tetap padat dan cepat diproses."""
    compact: dict[str, dict] = {}
    for vk, vv in voter_verdicts.items():
        if not isinstance(vv, dict):
            continue
        p_out: dict[str, Any] = {}
        for pk, pv in (vv.get("params") or {}).items():
            if isinstance(pv, dict):
                p_out[pk] = {
                    "score": pv.get("score", 0.0),
                    "reasoning": str(pv.get("reasoning") or "")[:140],
                }
        compact[vk] = {
            "overall_voter_confidence": vv.get("overall_voter_confidence", 0.5),
            "params": p_out,
        }
    return compact


def _call_agg_with_budget(
    client: Any,
    is_gemini: bool,
    prompt_or_msgs: Any,
    sys_msg: str,
    model: str,
    remaining_sec: float,
) -> dict:
    """Panggil client Meta-Aggregator dengan timeout terikat; tetap kompatibel dengan mock test."""
    call_timeout = min(AGGREGATOR_CALL_TIMEOUT_SEC, remaining_sec)
    if is_gemini:
        if isinstance(client, GeminiClient):
            return client.chat_json(prompt_or_msgs, system_instruction=sys_msg, model=model, timeout=call_timeout)
        return client.chat_json(prompt_or_msgs, system_instruction=sys_msg, model=model)
    if isinstance(client, QwenClient):
        return client.chat_json(prompt_or_msgs, model=model, timeout=call_timeout)
    return client.chat_json(prompt_or_msgs, model=model)


def _finalize_agg_result(agg_res: Any, candidate: str) -> bool:
    """Validasi respons aggregator dan normalisasi final_score arah murni secara in-place."""
    if isinstance(agg_res, dict) and "final_score" in agg_res and "per_parameter_final" in agg_res:
        agg_res["final_score"] = compute_directional_final_score(agg_res["per_parameter_final"])
        agg_res["aggregator_model"] = candidate
        return True
    return False


def run_meta_aggregator(
    qwen: Optional[QwenClient],
    ticker: str,
    voter_verdicts: dict[str, dict],
    pre_analysis: dict[str, Any],
    gemini: Optional[GeminiClient] = None,
    prefer_classai: bool = False,
) -> dict[str, Any]:
    """
    Jalankan Meta-Aggregator Chief Risk Officer untuk sintesis keputusan P1-P7.
    Prioritas:
      1. Jika `prefer_classai=True` & `gemini` tersedia: ClassAI Router (`claude-opus-5.5` -> `gpt-6.1-sol`)
         lebih dulu agar DashScope hanya melayani Voter V1 & V2 tanpa antrean.
      2. DashScope (`DASHSCOPE_API_KEY`): `AGGREGATOR_MODEL_CHAIN` -> fallback ClassAI (jika belum dicoba).
      3. Deterministic rule-based synthesis bila semua LLM gagal.
    """
    if (qwen or gemini) and len(voter_verdicts) > 0:
        try:
            sys_msg = (
                "Kamu adalah Chief Risk Officer dan Meta-Analyst portofolio finansial independen. "
                "Output HANYA JSON valid sesuai skema yang diminta."
            )
            prompt = (
                AGGREGATOR_PROMPT
                .replace("{ticker}", ticker)
                .replace("{pre_analysis_json}", json.dumps(pre_analysis, indent=2))
                .replace("{raw_voters_json}", json.dumps(_compact_voters_for_aggregator(voter_verdicts), indent=2))
            )
            last_error = ""
            agg_deadline = time.monotonic() + AGGREGATOR_TOTAL_BUDGET_SEC
            ds_msgs = [{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt}]

            # 0. Opsi prioritas ClassAI terlebih dulu agar bebas antrean DashScope
            if prefer_classai and gemini is not None:
                for candidate in CLASSAI_AGGREGATOR_CHAIN:
                    rem = agg_deadline - time.monotonic()
                    if rem <= 5.0:
                        last_error = "budget waktu habis"
                        break
                    try:
                        agg_res = _call_agg_with_budget(gemini, True, prompt, sys_msg, candidate, rem)
                    except Exception as exc:
                        last_error = f"{type(exc).__name__}: {exc}"
                        log.warning(f"Meta-Aggregator ClassAI {candidate} gagal {ticker}: {last_error[:170]}")
                        continue
                    if _finalize_agg_result(agg_res, candidate):
                        return agg_res
                    last_error = f"respons {candidate} tidak lengkap: {sorted(agg_res)[:6] if isinstance(agg_res, dict) else type(agg_res).__name__}"
                    log.warning(f"Meta-Aggregator ClassAI {candidate} untuk {ticker}: {last_error}")

            # 1. DashScope Aggregator (`glm-5.3` / `glm-5.2-fast-preview` -> ...) memakai DASHSCOPE_API_KEY
            if qwen is not None:
                for candidate in AGGREGATOR_MODEL_CHAIN:
                    rem = agg_deadline - time.monotonic()
                    if rem <= 5.0:
                        last_error = "budget waktu habis"
                        break
                    try:
                        agg_res = _call_agg_with_budget(qwen, False, ds_msgs, sys_msg, candidate, rem)
                    except Exception as exc:
                        last_error = f"{type(exc).__name__}: {exc}"
                        log.warning(f"Meta-Aggregator DashScope {candidate} gagal {ticker}: {last_error[:140]}")
                        if isinstance(exc, httpx.TimeoutException) and gemini is not None:
                            break
                        continue
                    if _finalize_agg_result(agg_res, candidate):
                        return agg_res
                    last_error = f"respons {candidate} tidak lengkap: {sorted(agg_res)[:6] if isinstance(agg_res, dict) else type(agg_res).__name__}"
                    log.warning(f"Meta-Aggregator DashScope {candidate} untuk {ticker}: {last_error}")
                    break

            # 2. Cadangan: ClassAI Aggregator (`claude-opus-5.5` -> `gpt-6.1-sol`) bila belum dicoba di awal
            if not prefer_classai and gemini is not None:
                for candidate in CLASSAI_AGGREGATOR_CHAIN:
                    rem = agg_deadline - time.monotonic()
                    if rem <= 5.0:
                        last_error = "budget waktu habis"
                        log.warning(f"Meta-Aggregator ClassAI berhenti untuk {ticker}: {last_error}")
                        break
                    try:
                        agg_res = _call_agg_with_budget(gemini, True, prompt, sys_msg, candidate, rem)
                    except Exception as exc:
                        last_error = f"{type(exc).__name__}: {exc}"
                        log.warning(f"Meta-Aggregator ClassAI {candidate} gagal untuk {ticker}: {last_error[:170]}")
                        continue
                    if _finalize_agg_result(agg_res, candidate):
                        return agg_res
                    last_error = f"respons {candidate} tidak lengkap: {sorted(agg_res)[:6] if isinstance(agg_res, dict) else type(agg_res).__name__}"
                    log.warning(f"Meta-Aggregator ClassAI {candidate} untuk {ticker}: {last_error}")

            log.warning(f"Meta-Aggregator {ticker} -> fallback rule-based "
                        f"(percobaan terakhir: {last_error[:180]})")
        except Exception as e:
            log.warning(f"Meta-Aggregator {ticker} error tak terduga, fallback rule-based: "
                        f"{type(e).__name__}: {str(e)[:160]}")

    # --- DETERMINISTIC RULE-BASED FALLBACK SYNTHESIS ---
    per_param_final = {}

    for param, weight in PARAM_WEIGHTS.items():
        avg_score = pre_analysis.get(param, {}).get("avg", 0.0)
        per_param_final[param] = {
            "final_score": round(avg_score, 3),
            "trusted_voter": "CONSENSUS",
            "rationale": f"Rata-rata konsensus {len(voter_verdicts)} voter (avg: {avg_score:.2f})",
        }

    final_score = compute_directional_final_score(per_param_final)

    # Hitung rata-rata variance untuk overall consensus level
    all_vars = [v.get("variance", 0.0) for v in pre_analysis.values()]
    mean_var = sum(all_vars) / len(all_vars) if all_vars else 0.0

    if mean_var < 0.05:
        overall_consensus = "unanimous"
    elif mean_var < 0.15:
        overall_consensus = "strong"
    elif mean_var < 0.30:
        overall_consensus = "mixed"
    else:
        overall_consensus = "split"

    # Evaluasi Vendor Alignment (Qwen vs Secondary Vendor: Gemini atau GLM)
    qwen_scores = [
        v["params"]["P1_sentiment_polarity"]["score"]
        for k, v in voter_verdicts.items()
        if "qwen" in k.lower() and "P1_sentiment_polarity" in v.get("params", {})
    ]
    gemini_scores = [
        v["params"]["P1_sentiment_polarity"]["score"]
        for k, v in voter_verdicts.items()
        if ("gemini" in k.lower() or "glm" in k.lower()) and "P1_sentiment_polarity" in v.get("params", {})
    ]

    q_avg = sum(qwen_scores) / len(qwen_scores) if qwen_scores else None
    g_avg = sum(gemini_scores) / len(gemini_scores) if gemini_scores else None

    if q_avg is not None and g_avg is not None:
        if (q_avg > 0 and g_avg > 0) or (q_avg < 0 and g_avg < 0) or (abs(q_avg) < 0.15 and abs(g_avg) < 0.15):
            vendor_alignment = "both_agree"
        elif abs(q_avg) > abs(g_avg) + 0.3:
            vendor_alignment = "qwen_dominant"
        elif abs(g_avg) > abs(q_avg) + 0.3:
            vendor_alignment = "gemini_dominant"
        else:
            vendor_alignment = "split"
    elif q_avg is not None:
        vendor_alignment = "qwen_dominant"
    elif g_avg is not None:
        vendor_alignment = "gemini_dominant"
    else:
        vendor_alignment = "split"

    # Rata-rata confidence dari seluruh voter
    voter_confs = [v.get("overall_voter_confidence", 0.5) for v in voter_verdicts.values()]
    base_conf = sum(voter_confs) / len(voter_confs) if voter_confs else 0.5
    overall_confidence = max(0.2, min(0.95, round(base_conf - (mean_var * 0.5), 2)))

    # Parameter paling berpengaruh
    influential = max(
        PARAM_WEIGHTS.items(),
        key=lambda kv: abs(per_param_final.get(kv[0], {}).get("final_score", 0.0)) * kv[1]
    )[0]

    justification = (
        f"Sintesis Swarm AI 4-Voter menghasilkan skor akhir {final_score:+.2f} "
        f"dengan tingkat konsensus '{overall_consensus}' dan vendor alignment '{vendor_alignment}'. "
        f"Parameter paling dominan adalah {influential}."
    )

    return {
        "per_parameter_final": per_param_final,
        "final_score": final_score,
        "overall_confidence": overall_confidence,
        "overall_consensus_level": overall_consensus,
        "vendor_alignment": vendor_alignment,
        "most_influential_parameter": influential,
        "justification": justification,
        "disagreement_summary": [
            f"{p}: level konsensus {d['consensus_level']}"
            for p, d in pre_analysis.items()
            if d.get("consensus_level") in ("MIXED", "SPLIT")
        ],
    }


SINGLE_ANALYST_PROMPT = """
Kamu adalah Chief Risk Officer & Quantitative Research Analyst untuk strategi perpetual 24-72 jam.
Evaluasi aset `{ticker}` berdasarkan [DATA REAL-TIME OAK RESEARCH MCP], konteks taktis strategi, dan headlines berita.

Lakukan penalaran kuantitatif terstruktur pada 7 parameter P1-P7 (tanpa bias positif):
- P1_sentiment_polarity (-1.0 bearish s/d +1.0 bullish): arah katalis & berita dominan.
- P2_signal_strength (0.0 s/d 1.0): kekuatan bukti & materialitas berita/metrik OAK.
- P3_source_credibility (0.0 s/d 1.0): kredibilitas & kepadatan sumber data.
- P4_momentum_direction (-1.0 bearish s/d +1.0 bullish): bias arah pergerakan harga/likuiditas 24-72 jam.
- P5_risk_catalysts (0.0 aman s/d 1.0 bahaya): risiko event mendadak (earnings, token unlock, regulasi, makro FOMC/CPI).
- P6_contrarian_check (0.0 aman s/d 1.0 crowded): risiko posisi pasar terlalu searah / short-long squeeze.
- P7_historical_pattern (-1.0 bearish s/d +1.0 bullish): preseden pergerakan pasca katalis serupa.

Tentukan `overall_consensus_level` ("unanimous" bila semua sinyal sangat searah, "strong" bila mayoritas jelas, "mixed" bila bercampur, "split" bila sinyal saling bertabrakan tajam).

Output HANYA JSON valid dengan skema:
{{
  "thinking": "Penalaran kuantitatif 3-5 poin ringkas mencakup data OAK, katalis risiko P5, crowdedness P6, dan prospek 24-72 jam.",
  "per_parameter_final": {{
    "P1_sentiment_polarity": {{"final_score": 0.0, "rationale": "..."}},
    "P2_signal_strength": {{"final_score": 0.0, "rationale": "..."}},
    "P3_source_credibility": {{"final_score": 0.0, "rationale": "..."}},
    "P4_momentum_direction": {{"final_score": 0.0, "rationale": "..."}},
    "P5_risk_catalysts": {{"final_score": 0.0, "rationale": "..."}},
    "P6_contrarian_check": {{"final_score": 0.0, "rationale": "..."}},
    "P7_historical_pattern": {{"final_score": 0.0, "rationale": "..."}}
  }},
  "overall_confidence": 0.0,
  "overall_consensus_level": "unanimous|strong|mixed|split",
  "most_influential_parameter": "P1_sentiment_polarity|P4_momentum_direction|P5_risk_catalysts|...",
  "justification": "Paragraf Bahasa Indonesia padat (1-2 paragraf) tentang kelayakan horizon 24-72 jam.",
  "disagreement_summary": ["faktor risiko/kontra 1", "faktor risiko/kontra 2"]
}}
"""


def run_single_deep_analyst(
    ticker: str,
    headlines: list[str],
    strategy_context: str = "",
    gemini: Optional[GeminiClient] = None,
    qwen: Optional[QwenClient] = None,
) -> Optional[dict[str, Any]]:
    """Single-Pass Deep Analyst berbasis OAK MCP (1 call cepat via ClassAI -> fallback DashScope fast)."""
    if gemini is None and qwen is None:
        return None
    sys_msg = (
        "Kamu adalah Chief Risk Officer dan Quantitative Research Analyst independen. "
        "Output HANYA JSON valid sesuai skema."
    )
    headline_lines = "\n".join(f"- {h}" for h in (headlines or []))
    prompt = (
        f"Ticker: {ticker}\n"
        f"{strategy_context}\n\n"
        f"Headlines Berita Relevan:\n{headline_lines}\n\n"
        + SINGLE_ANALYST_PROMPT.format(ticker=ticker)
    )
    ds_msgs = [{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt}]
    deadline = time.monotonic() + SINGLE_ANALYST_TOTAL_BUDGET_SEC
    last_error = ""

    def _finish_single(res_obj: Any, model_name: str) -> Optional[dict[str, Any]]:
        if not isinstance(res_obj, dict) or not isinstance(res_obj.get("per_parameter_final"), dict):
            return None
        res_obj["final_score"] = compute_directional_final_score(res_obj["per_parameter_final"])
        res_obj["vendor_alignment"] = "oak_single_analyst"
        res_obj["analyst_model"] = model_name
        return res_obj

    if gemini is not None:
        for candidate in SINGLE_ANALYST_CLASSAI_CHAIN:
            rem = deadline - time.monotonic()
            if rem <= 4.0:
                last_error = "budget waktu Single Deep Analyst habis"
                break
            try:
                call_t = min(SINGLE_ANALYST_CALL_TIMEOUT_SEC, rem)
                res = (
                    gemini.chat_json(prompt, system_instruction=sys_msg, model=candidate, timeout=call_t)
                    if isinstance(gemini, GeminiClient)
                    else gemini.chat_json(prompt, system_instruction=sys_msg, model=candidate)
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning(f"Single Deep Analyst ClassAI {candidate} gagal {ticker}: {last_error[:150]}")
                continue
            done = _finish_single(res, candidate)
            if done is not None:
                return done
            last_error = f"respons {candidate} tidak lengkap"
            log.warning(f"Single Deep Analyst ClassAI {candidate} {ticker}: {last_error}")

    if qwen is not None:
        for candidate in SINGLE_ANALYST_DASHSCOPE_FALLBACK:
            rem = deadline - time.monotonic()
            if rem <= 4.0:
                last_error = "budget waktu Single Deep Analyst habis"
                break
            try:
                call_t = min(SINGLE_ANALYST_CALL_TIMEOUT_SEC, rem)
                res = (
                    qwen.chat_json(ds_msgs, model=candidate, timeout=call_t)
                    if isinstance(qwen, QwenClient)
                    else qwen.chat_json(ds_msgs, model=candidate)
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning(f"Single Deep Analyst DashScope {candidate} gagal {ticker}: {last_error[:150]}")
                continue
            done = _finish_single(res, candidate)
            if done is not None:
                return done
            last_error = f"respons {candidate} tidak lengkap"
            log.warning(f"Single Deep Analyst DashScope {candidate} {ticker}: {last_error}")

    log.warning(f"Single Deep Analyst {ticker} gagal seluruh rantai ({last_error[:160]})")
    return None


# =============================================================================
# SYNTHESIS & ORCHESTRATION LAYER
# =============================================================================

def research_stock(
    ticker: str,
    qwen: Optional[QwenClient] = None,
    gemini: Optional[GeminiClient] = None,
    funding_daily: Optional[float] = None,
    recommended_side: Optional[str] = None,
    opportunity_count: int = 0,
    is_idle: Optional[bool] = None,
    mode: Optional[str] = None,
    grav_pressure: Optional[float] = None,
) -> Research:
    """
    Eksekusi pipeline riset aset lengkap:
      - Mode default `oak_single` (RESEARCH_MODE=oak_single):
          1. OAK-First (`fetch_oak_market_context`: overview, calendar, news, crypto/tradfi metrics)
          2. Skip Yahoo headlines bila OAK sudah menyuplai >= 2 berita; skip Yahoo earnings untuk crypto/ETF
          3. Single Deep Analyst 1-pass (ClassAI `claude-opus-5.5`/`gpt-6.1-sol` -> fallback DashScope fast)
      - Mode `swarm` (RESEARCH_MODE=swarm): 4-Voter Swarm + Meta-Aggregator.
      - `grav_pressure`: bullish_pressure % Market Gravity terkini -> ikut menjadi bahan konteks
        voters/analyst untuk memutuskan LONG vs SHORT (gerbang tekanan ada di engine 2).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    active_mode = (mode or get_research_mode()).strip().lower()
    if active_mode not in ("oak_single", "swarm"):
        active_mode = "oak_single"

    strategy_context = _build_strategy_context_block(
        ticker,
        funding_daily=funding_daily,
        recommended_side=recommended_side,
        grav_pressure=grav_pressure,
    )

    # OAK-First: tarik konteks makro, kalender, berita, & metrik aset dari OAK MCP lebih dulu
    oak_block = ""
    oak_news: list[str] = []
    oak_asset_type = ""
    oak_configured = False
    oak_days_earnings: Optional[int] = None
    oak_earnings_status = "no_data"
    try:
        import oak_mcp_client

        if oak_mcp_client.is_oak_mcp_configured():
            oak_configured = True
            oak_ctx = oak_mcp_client.fetch_oak_market_context(ticker)
            oak_asset_type = str(oak_ctx.get("asset_type") or "").strip().lower()
            oak_block = oak_mcp_client.format_oak_context_for_prompt(oak_ctx)
            if oak_block:
                strategy_context = f"{strategy_context}\n\n{oak_block}"
            oak_news = [str(h).strip() for h in (oak_ctx.get("headlines") or []) if str(h).strip()]
            raw_d = oak_ctx.get("days_to_earnings")
            oak_days_earnings = int(raw_d) if raw_d is not None else None
            oak_earnings_status = str(oak_ctx.get("earnings_status") or "no_data")
    except Exception as exc:
        log.warning(f"OAK MCP enrichment dilewati untuk {ticker}: {exc}")

    # Earnings check: lewati untuk crypto/ETF agar bebas 404 latency; untuk saham individual pakai
    # kalender OAKresearch MCP (tanpa Yahoo fc.yahoo.com/quoteSummary 404), tetap hormati monkeypatch test.
    sym_u = str(ticker or "").strip().upper()
    if oak_asset_type == "crypto" or sym_u in _NO_EARNINGS_TICKERS:
        days_earnings, earnings_status = None, "not_applicable" if active_mode == "oak_single" else "no_data"
    elif fetch_earnings_days_with_status is not _ORIG_FETCH_EARNINGS_WITH_STATUS:
        days_earnings, earnings_status = fetch_earnings_days_with_status(ticker)
    elif oak_configured:
        days_earnings, earnings_status = oak_days_earnings, oak_earnings_status
    else:
        days_earnings, earnings_status = fetch_earnings_days_with_status(ticker)

    # Headlines: OAK-first pada mode oak_single bila sudah cukup (>= 2), selain itu lengkapi dari Yahoo
    if active_mode == "oak_single" and len(oak_news) >= 2:
        headlines = oak_news[:10]
    else:
        headlines = fetch_headlines(ticker)
        if oak_news:
            merged_h: list[str] = []
            seen_h: set[str] = set()
            for h in oak_news + list(headlines or []):
                k = h.lower()
                if k and k not in seen_h:
                    seen_h.add(k)
                    merged_h.append(h)
            headlines = merged_h[:12]

    if not headlines and oak_block:
        headlines = [
            f"[OAK Research MCP Real-Time Data untuk {ticker}] "
            "Analisa berdasarkan blok [DATA REAL-TIME OAK RESEARCH MCP] pada konteks strategi."
        ]
        log.info(
            f"🌳 {ticker}: Yahoo headlines kosong, lanjutkan riset memakai data real-time OAK Research MCP."
        )

    if active_mode == "oak_single":
        if not headlines:
            return Research(
                ticker=ticker,
                days_to_earnings=days_earnings,
                news_sentiment="unknown",
                narrative="Tidak ada berita/headlines yang ditemukan.",
                confidence=0.0,
                fetched_at=now_iso,
                consensus="NO_DATA",
                overall_consensus_level="NO_DATA",
                vendor_alignment="oak_single_analyst",
                earnings_status=earnings_status,
                secondary_provider="oak_single",
                oak_context_block=oak_block,
            )

        if gemini is None and (os.getenv("DASHSCOPE_API_GEMINI_KEY") or os.getenv("GEMINI_API_KEY")):
            try:
                gemini = GeminiClient()
            except Exception as e:
                log.warning(f"Inisialisasi Gemini (Single Analyst) gagal: {e}")
        if qwen is None and os.getenv("DASHSCOPE_API_KEY"):
            try:
                qwen = QwenClient()
            except Exception as e:
                log.warning(f"Inisialisasi Qwen (fallback Single Analyst) gagal: {e}")

        agg_result = run_single_deep_analyst(
            ticker, headlines, strategy_context, gemini=gemini, qwen=qwen
        )
        if not agg_result:
            return Research(
                ticker=ticker,
                days_to_earnings=days_earnings,
                news_sentiment="unknown",
                narrative="Single Deep Analyst gagal memberikan evaluasi.",
                confidence=0.0,
                fetched_at=now_iso,
                consensus="NONE",
                overall_consensus_level="NONE",
                vendor_alignment="oak_single_analyst",
                earnings_status=earnings_status,
                voter_failures=["single_deep_analyst"],
                secondary_provider="oak_single",
                headlines=list(headlines),
                oak_context_block=oak_block,
            )

        final_score = float(agg_result.get("final_score", 0.0))
        overall_conf = float(agg_result.get("overall_confidence", 0.5))
        raw_cons = str(agg_result.get("overall_consensus_level", "")).strip().upper()
        if raw_cons in ("UNANIMOUS", "STRONG", "MIXED", "SPLIT"):
            consensus_level = raw_cons
        elif overall_conf >= 0.70:
            consensus_level = "STRONG"
        elif overall_conf >= 0.45:
            consensus_level = "MIXED"
        else:
            consensus_level = "SPLIT"

        if final_score > 0.15:
            sentiment: Literal["bullish", "bearish", "neutral", "unknown"] = "bullish"
        elif final_score < -0.15:
            sentiment = "bearish"
        else:
            sentiment = "neutral"

        p1_to_p7 = {}
        for p in PARAM_WEIGHTS.keys():
            p_data = agg_result.get("per_parameter_final", {}).get(p, {})
            p1_to_p7[p] = float(p_data.get("final_score", 0.0))

        thinking_txt = str(agg_result.get("thinking") or "").strip()
        justification = str(agg_result.get("justification", "")).strip()
        influential_param = str(agg_result.get("most_influential_parameter", "P1_sentiment_polarity"))
        disagreements = agg_result.get("disagreement_summary", [])

        return Research(
            ticker=ticker,
            days_to_earnings=days_earnings,
            news_sentiment=sentiment,
            narrative=justification,
            confidence=overall_conf,
            fetched_at=now_iso,
            consensus=consensus_level,
            qwen_sentiment=sentiment,
            gemini_sentiment=sentiment,
            final_score=final_score,
            overall_consensus_level=consensus_level,
            vendor_alignment="oak_single_analyst",
            most_influential_parameter=influential_param,
            disagreement_summary=disagreements if isinstance(disagreements, list) else [],
            p1_to_p7_scores=p1_to_p7,
            voters_count=1,
            voter_verdicts={},
            aggregator_details=agg_result,
            earnings_status=earnings_status,
            voter_failures=[],
            secondary_provider="oak_single",
            headlines=list(headlines),
            oak_context_block=oak_block,
            analyst_thinking=thinking_txt,
        )

    # --- LEGACY SWARM MODE (4-VOTER + META-AGGREGATOR) ---
    use_glm = should_use_glm_for_secondary_voters(
        opportunity_count=opportunity_count,
        is_idle=is_idle,
    )

    # Inisialisasi API clients jika belum tersedia
    if qwen is None and os.getenv("DASHSCOPE_API_KEY"):
        try:
            qwen = QwenClient()
        except Exception as e:
            log.warning(f"Inisialisasi Qwen gagal: {e}")

    # Gemini hanya diinisialisasi & digunakan saat kondisi Idle (atau fallback bila DashScope tidak tersedia)
    if (not use_glm or qwen is None) and gemini is None and (
        os.getenv("DASHSCOPE_API_GEMINI_KEY") or os.getenv("GEMINI_API_KEY")
    ):
        try:
            gemini = GeminiClient()
        except Exception as e:
            log.warning(f"Inisialisasi Gemini gagal: {e}")

    active_secondary = "glm" if (use_glm and qwen is not None) else "gemini"
    log.info(
        f"🧠 [AI ROUTING] {ticker}: secondary_provider={active_secondary.upper()} "
        f"(opportunity_count={opportunity_count}, is_idle={is_idle})"
    )

    if not headlines:
        return Research(
            ticker=ticker,
            days_to_earnings=days_earnings,
            news_sentiment="unknown",
            narrative="Tidak ada berita/headlines yang ditemukan.",
            confidence=0.0,
            fetched_at=now_iso,
            consensus="NO_DATA",
            overall_consensus_level="NO_DATA",
            earnings_status=earnings_status,
            secondary_provider=active_secondary,
            oak_context_block=oak_block,
        )

    # 1. Jalankan 4 Voters secara Paralel (Swarm Voting)
    from concurrent.futures import ThreadPoolExecutor, as_completed

    voter_verdicts: dict[str, dict] = {}
    voter_failures: list[str] = []
    tasks = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        if qwen:
            tasks[executor.submit(run_voter_1, qwen, ticker, headlines, strategy_context)] = "V1_qwen_turbo"
            tasks[executor.submit(run_voter_2, qwen, ticker, headlines, strategy_context)] = "V2_qwen_plus"
        if active_secondary == "glm" and qwen:
            tasks[executor.submit(run_voter_3_glm, qwen, ticker, headlines, strategy_context)] = "V3_glm_fast"
            tasks[executor.submit(run_voter_4_glm, qwen, ticker, headlines, strategy_context)] = "V4_glm_prime"
        elif gemini:
            tasks[executor.submit(run_voter_3, gemini, ticker, headlines, strategy_context)] = "V3_gemini_flash"
            tasks[executor.submit(run_voter_4, gemini, ticker, headlines, strategy_context)] = "V4_gemini_pro"

        for future in as_completed(tasks):
            voter_name = tasks[future]
            try:
                res = future.result()
            except Exception as e:
                voter_failures.append(voter_name)
                log.warning(f"Execution {voter_name} error: {e}")
                continue
            if res:
                voter_verdicts[voter_name] = res
            else:
                # run_voter_* menelan exception-nya sendiri dan mengembalikan None: tanpa cabang
                # ini, voter yang mati 100% (kasus qwen-turbo 404) tidak pernah tercatat gagal.
                voter_failures.append(voter_name)

    if voter_failures:
        log.warning(
            f"Swarm terdegradasi untuk {ticker}: {len(voter_verdicts)}/{len(tasks)} voter berhasil; "
            f"gagal: {', '.join(sorted(voter_failures))}"
        )

    voters_count = len(voter_verdicts)
    if voters_count == 0:
        return Research(
            ticker=ticker,
            days_to_earnings=days_earnings,
            news_sentiment="unknown",
            narrative="Semua voter AI gagal memberikan evaluasi.",
            confidence=0.0,
            fetched_at=now_iso,
            consensus="NONE",
            overall_consensus_level="NONE",
            earnings_status=earnings_status,
            voter_failures=voter_failures,
        )

    # 2. Pre-processing Varians & Konsensus P1-P7
    pre_analysis = analyze_voter_variance(voter_verdicts)

    # 3. Meta-Aggregator Sintesis (prioritas ClassAI claude-opus-5.5/gpt-6.1-sol -> cadangan DashScope)
    agg_result = run_meta_aggregator(
        qwen,
        ticker,
        voter_verdicts,
        pre_analysis,
        gemini=gemini if active_secondary != "glm" else None,
        prefer_classai=True,
    )

    final_score = float(agg_result.get("final_score", 0.0))
    overall_conf = float(agg_result.get("overall_confidence", 0.5))
    consensus_level = str(agg_result.get("overall_consensus_level", "mixed")).upper()
    vendor_align = str(agg_result.get("vendor_alignment", "split"))
    influential_param = str(agg_result.get("most_influential_parameter", "P1_sentiment_polarity"))
    justification = str(agg_result.get("justification", "")).strip()
    disagreements = agg_result.get("disagreement_summary", [])

    # Klasifikasi arah sentimen dari final_score
    if final_score > 0.15:
        sentiment: Literal["bullish", "bearish", "neutral", "unknown"] = "bullish"
    elif final_score < -0.15:
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    # Mapping individual vendor sentiment untuk backward compatibility
    qwen_scores = [
        v["params"]["P1_sentiment_polarity"]["score"]
        for k, v in voter_verdicts.items()
        if "qwen" in k.lower() and "P1_sentiment_polarity" in v.get("params", {})
    ]
    gemini_scores = [
        v["params"]["P1_sentiment_polarity"]["score"]
        for k, v in voter_verdicts.items()
        if ("gemini" in k.lower() or "glm" in k.lower()) and "P1_sentiment_polarity" in v.get("params", {})
    ]

    q_sent = "bullish" if qwen_scores and sum(qwen_scores)/len(qwen_scores) > 0.15 else ("bearish" if qwen_scores and sum(qwen_scores)/len(qwen_scores) < -0.15 else "neutral") if qwen_scores else None
    g_sent = "bullish" if gemini_scores and sum(gemini_scores)/len(gemini_scores) > 0.15 else ("bearish" if gemini_scores and sum(gemini_scores)/len(gemini_scores) < -0.15 else "neutral") if gemini_scores else None

    # Ringkasan P1-P7 final scores untuk kemudahan akses bot
    p1_to_p7 = {}
    for p in PARAM_WEIGHTS.keys():
        p_data = agg_result.get("per_parameter_final", {}).get(p, {})
        p1_to_p7[p] = float(p_data.get("final_score", pre_analysis.get(p, {}).get("avg", 0.0)))

    secondary_voter = (
        voter_verdicts.get("V3_gemini_flash")
        or voter_verdicts.get("V3_glm_fast")
        or voter_verdicts.get("V4_glm_prime")
        or voter_verdicts.get("V4_gemini_pro")
        or {}
    )

    return Research(
        ticker=ticker,
        days_to_earnings=days_earnings,
        news_sentiment=sentiment,
        narrative=justification,
        confidence=overall_conf,
        fetched_at=now_iso,
        consensus=consensus_level,
        qwen_sentiment=q_sent,
        gemini_sentiment=g_sent,
        qwen_narrative=voter_verdicts.get("V2_qwen_plus", {}).get("params", {}).get("P1_sentiment_polarity", {}).get("reasoning"),
        gemini_narrative=secondary_voter.get("params", {}).get("P4_momentum_direction", {}).get("reasoning"),
        qwen_confidence=voter_verdicts.get("V2_qwen_plus", {}).get("overall_voter_confidence"),
        gemini_confidence=secondary_voter.get("overall_voter_confidence"),
        final_score=final_score,
        overall_consensus_level=consensus_level,
        vendor_alignment=vendor_align,
        most_influential_parameter=influential_param,
        disagreement_summary=disagreements,
        p1_to_p7_scores=p1_to_p7,
        voters_count=voters_count,
        voter_verdicts=voter_verdicts,
        aggregator_details=agg_result,
        earnings_status=earnings_status,
        voter_failures=voter_failures,
        secondary_provider=active_secondary,
        headlines=list(headlines),
        oak_context_block=oak_block,
    )


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    test_ticker = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    print(f"Testing Swarm AI Research (4-Voter + Qwen-Max Aggregator) untuk {test_ticker}...")
    res = research_stock(test_ticker)
    print("\n--- HASIL RISET SWARM AI ---")
    print(f"Ticker: {res.ticker}")
    print(f"Sentiment: {res.news_sentiment} (Final Score: {res.final_score:+.2f})")
    print(f"Confidence: {res.confidence:.0%}")
    print(f"Consensus Level: {res.overall_consensus_level} | Vendor Alignment: {res.vendor_alignment}")
    print(f"Most Influential Parameter: {res.most_influential_parameter}")
    print(f"P1-P7 Scores: {res.p1_to_p7_scores}")
    print(f"Narrative:\n{res.narrative}")
    print(f"Disagreements: {res.disagreement_summary}")
    print(f"Voters Participated: {res.voters_count}")
