"""
Multi-Voter AI Research Layer (4-Voter Swarm + Qwen-Max Aggregator).
Implementasi Standar PRD 3.5:
  - 4 Voter dengan Keseimbangan Vendor 50/50:
      V1: Alibaba Qwen-Turbo (Quick Signal Analyst)
      V2: Alibaba Qwen-Plus  (Balanced Equity Analyst)
      V3: Google Gemini-Flash-Latest (Realtime Web & Breaking News Validator)
      V4: Google Gemini-Pro-Latest   (Deep Multi-Angle & Contrarian Reasoner)
  - 7 Parameter Standar Validasi (P1-P7):
      P1: sentiment_polarity    (Weight: 0.25)
      P2: signal_strength       (Weight: 0.10)
      P3: source_credibility    (Weight: 0.10)
      P4: momentum_direction    (Weight: 0.20)
      P5: risk_catalysts        (Weight: 0.15)
      P6: contrarian_check      (Weight: 0.10)
      P7: historical_pattern    (Weight: 0.10)
  - Pre-processing rule-based variance/consensus analysis (UNANIMOUS, STRONG, MIXED, SPLIT)
  - Meta-Aggregator (Qwen-Max) untuk sintesis bobot per-parameter dan deteksi outlier

Environment Variables:
  DASHSCOPE_API_KEY        — Alibaba Cloud Model Studio (Qwen)
  DASHSCOPE_API_GEMINI_KEY — Google AI Studio (Gemini) [fallback: GEMINI_API_KEY]
"""

import os
import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Literal, Optional, Any
import httpx

log = logging.getLogger("llm_research")

DASHSCOPE_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Default Model Specifications
MODEL_V1 = "qwen-turbo"
MODEL_V2 = os.getenv("DASHSCOPE_MODEL", "qwen-plus")
MODEL_V3 = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
MODEL_V4 = "gemini-pro-latest"
MODEL_AGGREGATOR = "qwen-max"

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


# =============================================================================
# API CLIENTS
# =============================================================================

class QwenClient:
    """Client untuk Alibaba Cloud Qwen via DashScope OpenAI-compatible endpoint."""

    def __init__(self, api_key: Optional[str] = None, model: str = MODEL_V2):
        self.api_key = (api_key or os.environ.get("DASHSCOPE_API_KEY", "")).strip()
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_KEY belum di-set!")
        self.model = model
        self.client = httpx.Client(
            base_url=DASHSCOPE_BASE,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    def chat_json(self, messages: list[dict], model: Optional[str] = None) -> dict:
        """Kirim request ke DashScope dengan response_format JSON."""
        target_model = model or self.model
        payload = {
            "model": target_model,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        r = self.client.post("/chat/completions", json=payload)
        
        # Tangani jika qwen-turbo kena rate limit (429) -> fallback otomatis ke qwen-plus
        if r.status_code == 429 and target_model == MODEL_V1:
            log.warning(f"Qwen model {target_model} rate limited (429). Fallback ke {MODEL_V2}...")
            payload["model"] = MODEL_V2
            r = self.client.post("/chat/completions", json=payload)

        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        
        # Clean markdown wrappers if any
        text = content.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return json.loads(text.strip())


class GeminiClient:
    """Client untuk Google Gemini via endpoint resmi generateContent."""

    def __init__(self, api_key: Optional[str] = None, model: str = MODEL_V3):
        self.api_key = (
            api_key
            or os.environ.get("DASHSCOPE_API_GEMINI_KEY")
            or os.environ.get("GEMINI_API_KEY", "")
        ).strip()
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_GEMINI_KEY / GEMINI_API_KEY belum di-set!")

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

    def chat_json(self, prompt: str, system_instruction: Optional[str] = None, model: Optional[str] = None) -> dict:
        """Kirim prompt ke Gemini generateContent API."""
        target_model = self._clean_model_name(model or self.model)
        url = f"{GEMINI_BASE}/models/{target_model}:generateContent"
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

        r = self.client.post(url, headers=headers, json=payload, timeout=45.0)
        r.raise_for_status()
        return self._parse_response(r.json())

    def _parse_response(self, res_data: dict) -> dict:
        candidates = res_data.get("candidates", [])
        if not candidates:
            raise RuntimeError(f"Gemini API empty response: {res_data}")
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            raise RuntimeError(f"Gemini API empty parts: {res_data}")
        text = parts[0].get("text", "").strip()

        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return json.loads(text.strip())


# =============================================================================
# DATA FETCHING UTILITIES
# =============================================================================

def fetch_headlines(ticker: str, limit: int = 10) -> list[str]:
    """Ambil headlines berita 7 hari terakhir dari Yahoo Finance."""
    try:
        r = httpx.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": ticker, "newsCount": limit},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15.0,
        )
        r.raise_for_status()
        news = r.json().get("news", [])
        return [n.get("title", "") for n in news[:limit] if n.get("title")]
    except Exception as e:
        log.warning(f"fetch_headlines {ticker}: {e}")
        return []


def fetch_earnings_days(ticker: str) -> Optional[int]:
    """Hitung hari menuju jadwal rilis earnings berikutnya via Yahoo Finance."""
    try:
        r = httpx.get(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
            params={"modules": "calendarEvents"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15.0,
        )
        r.raise_for_status()
        earnings = (
            r.json().get("quoteSummary", {})
            .get("result", [{}])[0]
            .get("calendarEvents", {})
            .get("earnings", {})
            .get("earningsDate", [])
        )
        if not earnings:
            return None
        ts = earnings[0].get("raw")
        if not ts:
            return None
        return max(0, (datetime.fromtimestamp(ts) - datetime.now()).days)
    except Exception as e:
        log.warning(f"fetch_earnings_days {ticker}: {e}")
        return None


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
    return (
        f"Konteks Strategi: Bot sedang mempertimbangkan posisi {side_txt} pada {ticker} "
        f"untuk memanen funding rate ({fr_txt}) dengan SL 1.5% & TP 2.0%-2.5% (horizon 24-72 jam). "
        f"{risk_note}"
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


def run_meta_aggregator(
    qwen: Optional[QwenClient],
    ticker: str,
    voter_verdicts: dict[str, dict],
    pre_analysis: dict[str, Any],
) -> dict[str, Any]:
    """
    Jalankan Meta-Aggregator Qwen-Max untuk sintesis keputusan.
    Jika Qwen-Max tidak tersedia atau gagal, gunakan deterministic rule-based synthesis.
    """
    if qwen and len(voter_verdicts) > 0:
        try:
            sys_msg = (
                "Kamu adalah Chief Risk Officer dan Meta-Analyst portofolio finansial independen. "
                "Output HANYA JSON valid sesuai skema yang diminta."
            )
            prompt = (
                AGGREGATOR_PROMPT
                .replace("{ticker}", ticker)
                .replace("{pre_analysis_json}", json.dumps(pre_analysis, indent=2))
                .replace("{raw_voters_json}", json.dumps(voter_verdicts, indent=2))
            )
            agg_res = qwen.chat_json(
                [{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt}],
                model=MODEL_AGGREGATOR,
            )
            if "final_score" in agg_res and "per_parameter_final" in agg_res:
                # Enforce rumus arah murni agar tidak ada bias positif dari P2/P3/P5/P6
                agg_res["final_score"] = compute_directional_final_score(agg_res["per_parameter_final"])
                return agg_res
        except Exception as e:
            log.warning(f"Meta-Aggregator (Qwen-Max) call failed for {ticker}: {e}. Fallback to rule-based.")

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

    # Evaluasi Vendor Alignment (Qwen vs Gemini)
    qwen_scores = [
        v["params"]["P1_sentiment_polarity"]["score"]
        for k, v in voter_verdicts.items()
        if "qwen" in k.lower() and "P1_sentiment_polarity" in v.get("params", {})
    ]
    gemini_scores = [
        v["params"]["P1_sentiment_polarity"]["score"]
        for k, v in voter_verdicts.items()
        if "gemini" in k.lower() and "P1_sentiment_polarity" in v.get("params", {})
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


# =============================================================================
# SYNTHESIS & ORCHESTRATION LAYER
# =============================================================================

def research_stock(
    ticker: str,
    qwen: Optional[QwenClient] = None,
    gemini: Optional[GeminiClient] = None,
    funding_daily: Optional[float] = None,
    recommended_side: Optional[str] = None,
) -> Research:
    """
    Eksekusi pipeline riset saham lengkap:
      1. Ambil data pasar & headlines berita 7 hari
      2. Jalankan 4 Voter Swarm secara independen (2 Qwen + 2 Gemini) dengan konteks taktis 24-72 jam
      3. Analisis varians & konsensus P1-P7
      4. Eksekusi Meta-Aggregator (Qwen-Max) dengan skor arah murni (bebas bias positif)
      5. Kembalikan objek Research terstandarisasi
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    strategy_context = _build_strategy_context_block(
        ticker,
        funding_daily=funding_daily,
        recommended_side=recommended_side,
    )

    # Inisialisasi API clients jika belum tersedia
    if qwen is None and os.getenv("DASHSCOPE_API_KEY"):
        try:
            qwen = QwenClient()
        except Exception as e:
            log.warning(f"Inisialisasi Qwen gagal: {e}")

    if gemini is None and (os.getenv("DASHSCOPE_API_GEMINI_KEY") or os.getenv("GEMINI_API_KEY")):
        try:
            gemini = GeminiClient()
        except Exception as e:
            log.warning(f"Inisialisasi Gemini gagal: {e}")

    days_earnings = fetch_earnings_days(ticker)
    headlines = fetch_headlines(ticker)

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
        )

    # 1. Jalankan 4 Voters secara Paralel (Swarm Voting)
    from concurrent.futures import ThreadPoolExecutor, as_completed

    voter_verdicts: dict[str, dict] = {}
    tasks = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        if qwen:
            tasks[executor.submit(run_voter_1, qwen, ticker, headlines, strategy_context)] = "V1_qwen_turbo"
            tasks[executor.submit(run_voter_2, qwen, ticker, headlines, strategy_context)] = "V2_qwen_plus"
        if gemini:
            tasks[executor.submit(run_voter_3, gemini, ticker, headlines, strategy_context)] = "V3_gemini_flash"
            tasks[executor.submit(run_voter_4, gemini, ticker, headlines, strategy_context)] = "V4_gemini_pro"

        for future in as_completed(tasks):
            voter_name = tasks[future]
            try:
                res = future.result()
                if res:
                    voter_verdicts[voter_name] = res
            except Exception as e:
                log.warning(f"Execution {voter_name} error: {e}")

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
        )

    # 2. Pre-processing Varians & Konsensus P1-P7
    pre_analysis = analyze_voter_variance(voter_verdicts)

    # 3. Meta-Aggregator Sintesis
    agg_result = run_meta_aggregator(qwen, ticker, voter_verdicts, pre_analysis)

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
        if "gemini" in k.lower() and "P1_sentiment_polarity" in v.get("params", {})
    ]

    q_sent = "bullish" if qwen_scores and sum(qwen_scores)/len(qwen_scores) > 0.15 else ("bearish" if qwen_scores and sum(qwen_scores)/len(qwen_scores) < -0.15 else "neutral") if qwen_scores else None
    g_sent = "bullish" if gemini_scores and sum(gemini_scores)/len(gemini_scores) > 0.15 else ("bearish" if gemini_scores and sum(gemini_scores)/len(gemini_scores) < -0.15 else "neutral") if gemini_scores else None

    # Ringkasan P1-P7 final scores untuk kemudahan akses bot
    p1_to_p7 = {}
    for p in PARAM_WEIGHTS.keys():
        p_data = agg_result.get("per_parameter_final", {}).get(p, {})
        p1_to_p7[p] = float(p_data.get("final_score", pre_analysis.get(p, {}).get("avg", 0.0)))

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
        gemini_narrative=voter_verdicts.get("V3_gemini_flash", {}).get("params", {}).get("P4_momentum_direction", {}).get("reasoning"),
        qwen_confidence=voter_verdicts.get("V2_qwen_plus", {}).get("overall_voter_confidence"),
        gemini_confidence=voter_verdicts.get("V3_gemini_flash", {}).get("overall_voter_confidence"),
        final_score=final_score,
        overall_consensus_level=consensus_level,
        vendor_alignment=vendor_align,
        most_influential_parameter=influential_param,
        disagreement_summary=disagreements,
        p1_to_p7_scores=p1_to_p7,
        voters_count=voters_count,
        voter_verdicts=voter_verdicts,
        aggregator_details=agg_result,
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
