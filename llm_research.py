"""
Dual-AI Research Layer (Alibaba Cloud Qwen + Google Gemini Consensus).
Menggabungkan riset sentimen finansial dari dua model AI terdepan untuk
mencapai konsensus sinyal pasar yang lebih akurat dan aman.

Butuh env:
    DASHSCOPE_API_KEY        — dari Alibaba Cloud Model Studio (Qwen)
    DASHSCOPE_API_GEMINI_KEY — dari Google AI Studio (Gemini) (atau GEMINI_API_KEY)
    (opsional) DASHSCOPE_MODEL — default "qwen-plus"
    (opsional) GEMINI_MODEL    — default "gemini-1.5-flash"
"""

import os
import json
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Literal, Optional
import httpx

DASHSCOPE_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
DEFAULT_QWEN_MODEL = os.getenv("DASHSCOPE_MODEL", "qwen-plus")
DEFAULT_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")


@dataclass
class Research:
    ticker: str
    days_to_earnings: Optional[int]
    news_sentiment: Literal["bullish", "bearish", "neutral", "unknown"]
    narrative: str
    confidence: float  # 0-1
    fetched_at: str
    consensus: str = "SINGLE_MODEL"  # STRONG_CONSENSUS, PARTIAL_CONSENSUS, CONFLICT, SINGLE_MODEL, NONE
    qwen_sentiment: Optional[str] = None
    gemini_sentiment: Optional[str] = None
    qwen_narrative: Optional[str] = None
    gemini_narrative: Optional[str] = None
    qwen_confidence: Optional[float] = None
    gemini_confidence: Optional[float] = None


SENTIMENT_PROMPT = """Kamu analis saham US profesional. Analisis headlines terkini untuk ticker {ticker} dan jawab HANYA dalam format JSON valid.

Headlines (7 hari terakhir):
{headlines}

Jawab tepat dalam format JSON berikut:
{{
  "sentiment": "bullish" | "bearish" | "neutral",
  "confidence": 0.0-1.0,
  "narrative": "1-2 kalimat ringkasan narasi dominan dalam Bahasa Indonesia",
  "key_drivers": ["list", "faktor", "utama"],
  "upcoming_risk_event": "earnings/product launch/lawsuit/other/none"
}}

Jika data tidak cukup atau berimbang, tentukan sentiment: "neutral" dan confidence: 0.35."""


class QwenClient:
    """Alibaba Cloud Qwen Client via DashScope OpenAI-compatible endpoint."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_QWEN_MODEL):
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
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

    def chat_json(self, messages: list[dict]) -> dict:
        """Chat dengan JSON response enforced."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        r = self.client.post("/chat/completions", json=payload)
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)


class GeminiClient:
    """Google Gemini Client via OpenAI-compatible endpoint with native REST fallback."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_GEMINI_MODEL):
        self.api_key = (
            api_key
            or os.environ.get("DASHSCOPE_API_GEMINI_KEY")
            or os.environ.get("GEMINI_API_KEY", "")
        ).strip()
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_GEMINI_KEY / GEMINI_API_KEY belum di-set!")
        self.model = model
        self.client = httpx.Client(timeout=60.0)

    def chat_json(self, prompt: str) -> dict:
        """Kirim prompt analisis ke Gemini dan kembalikan JSON dict."""
        # 1. Coba endpoint resmi Google OpenAI-compatible
        try:
            url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Kamu analis saham US profesional. Output HANYA JSON valid.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            }
            r = self.client.post(url, json=payload, headers=headers)
            if r.status_code == 200:
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                return json.loads(content)
        except Exception:
            pass

        # 2. Fallback ke endpoint Native REST Generative Language API
        native_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        native_payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": f"Kamu analis saham US profesional. Output HANYA JSON valid.\n\n{prompt}"
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
            },
        }
        r = self.client.post(native_url, json=native_payload, timeout=60.0)
        r.raise_for_status()
        res_data = r.json()
        candidates = res_data.get("candidates", [])
        if not candidates:
            raise RuntimeError(f"Gemini API empty response: {res_data}")
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            raise RuntimeError(f"Gemini API empty parts: {res_data}")
        text = parts[0].get("text", "").strip()

        # Bersihkan format markdown jika ada ```json ... ```
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return json.loads(text.strip())


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
        print(f"[WARN] fetch_headlines {ticker}: {e}")
        return []


def fetch_earnings_days(ticker: str) -> Optional[int]:
    """Days to next earnings via Yahoo Finance quoteSummary."""
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
        print(f"[WARN] fetch_earnings_days {ticker}: {e}")
        return None


def synthesize_dual_ai(
    ticker: str,
    qwen_res: Optional[dict],
    gemini_res: Optional[dict],
    days_to_earnings: Optional[int],
) -> Research:
    """
    Sintesis konsensus keputusan kolaborasi AI (Qwen + Gemini).
    Menghitung kesepakatan sentimen, skor confidence gabungan, dan deteksi konflik.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    # KASUS 1: Kedua Model AI Berhasil Mengevaluasi (Full Dual Collaboration)
    if qwen_res and gemini_res:
        q_sent = qwen_res.get("sentiment", "neutral").lower()
        g_sent = gemini_res.get("sentiment", "neutral").lower()
        q_conf = float(qwen_res.get("confidence", 0.5))
        g_conf = float(gemini_res.get("confidence", 0.5))
        q_nar = qwen_res.get("narrative", "").strip()
        g_nar = gemini_res.get("narrative", "").strip()

        # Normalisasi ke enum resmi
        if q_sent not in ("bullish", "bearish", "neutral"):
            q_sent = "neutral"
        if g_sent not in ("bullish", "bearish", "neutral"):
            g_sent = "neutral"

        # Skenario 1.1: Kesepakatan Penuh (Strong Consensus)
        if q_sent == g_sent:
            if q_sent in ("bullish", "bearish"):
                consensus = "STRONG_CONSENSUS"
                final_conf = min(0.95, round((q_conf + g_conf) / 2 + 0.10, 2))
                narrative = f"[Konsensus Kuat Dual-AI: {q_sent.upper()}] Qwen & Gemini sepakat. Qwen: {q_nar} | Gemini: {g_nar}"
            else:
                consensus = "NEUTRAL_CONSENSUS"
                final_conf = round((q_conf + g_conf) / 2, 2)
                narrative = f"[Konsensus Netral] Kedua AI menilai tidak ada tren dominan. {q_nar}"
            final_sent = q_sent

        # Skenario 1.2: Konsensus Parsial (1 Model Directional, 1 Model Netral)
        elif "neutral" in (q_sent, g_sent):
            consensus = "PARTIAL_CONSENSUS"
            final_sent = q_sent if q_sent != "neutral" else g_sent
            dominant_conf = q_conf if q_sent != "neutral" else g_conf
            final_conf = round(max(0.40, dominant_conf * 0.85), 2)
            narrative = f"[Konsensus Parsial] {q_sent.upper()} (Qwen) vs {g_sent.upper()} (Gemini). {q_nar if q_sent != 'neutral' else g_nar}"

        # Skenario 1.3: Konflik Arah (Bullish vs Bearish) -> Auto-Protection Risk Circuit
        else:
            consensus = "CONFLICT"
            final_sent = "neutral"  # Downgrade ke netral demi keamanan modal
            final_conf = 0.30
            narrative = f"[Konflik Sinyal AI ⚡] Qwen ({q_sent.upper()}) berlawanan arah dengan Gemini ({g_sent.upper()}). Rekomendasi proteksi: kurangi ukuran posisi (HALF/SKIP)."

        return Research(
            ticker=ticker,
            days_to_earnings=days_to_earnings,
            news_sentiment=final_sent,
            narrative=narrative,
            confidence=final_conf,
            fetched_at=now_iso,
            consensus=consensus,
            qwen_sentiment=q_sent,
            gemini_sentiment=g_sent,
            qwen_narrative=q_nar,
            gemini_narrative=g_nar,
            qwen_confidence=q_conf,
            gemini_confidence=g_conf,
        )

    # KASUS 2: Hanya Qwen yang Aktif / Berhasil
    elif qwen_res:
        q_sent = qwen_res.get("sentiment", "neutral").lower()
        if q_sent not in ("bullish", "bearish", "neutral"):
            q_sent = "neutral"
        q_conf = float(qwen_res.get("confidence", 0.5))
        q_nar = qwen_res.get("narrative", "").strip()
        return Research(
            ticker=ticker,
            days_to_earnings=days_to_earnings,
            news_sentiment=q_sent,
            narrative=f"[Qwen AI] {q_nar}",
            confidence=q_conf,
            fetched_at=now_iso,
            consensus="SINGLE_MODEL (Qwen)",
            qwen_sentiment=q_sent,
            qwen_narrative=q_nar,
            qwen_confidence=q_conf,
        )

    # KASUS 3: Hanya Gemini yang Aktif / Berhasil
    elif gemini_res:
        g_sent = gemini_res.get("sentiment", "neutral").lower()
        if g_sent not in ("bullish", "bearish", "neutral"):
            g_sent = "neutral"
        g_conf = float(gemini_res.get("confidence", 0.5))
        g_nar = gemini_res.get("narrative", "").strip()
        return Research(
            ticker=ticker,
            days_to_earnings=days_to_earnings,
            news_sentiment=g_sent,
            narrative=f"[Gemini AI] {g_nar}",
            confidence=g_conf,
            fetched_at=now_iso,
            consensus="SINGLE_MODEL (Gemini)",
            gemini_sentiment=g_sent,
            gemini_narrative=g_nar,
            gemini_confidence=g_conf,
        )

    # KASUS 4: Keduanya Tidak Tersedia
    else:
        return Research(
            ticker=ticker,
            days_to_earnings=days_to_earnings,
            news_sentiment="unknown",
            narrative="Tidak ada evaluasi AI yang berhasil.",
            confidence=0.0,
            fetched_at=now_iso,
            consensus="NONE",
        )


def research_stock(
    ticker: str,
    qwen: Optional[QwenClient] = None,
    gemini: Optional[GeminiClient] = None,
) -> Research:
    """Full research bundle dengan kolaborasi dual AI (Qwen + Gemini)."""
    # Auto-initialize jika belum dipassing tapi env key tersedia
    if qwen is None and os.getenv("DASHSCOPE_API_KEY"):
        try:
            qwen = QwenClient()
        except Exception as e:
            print(f"[WARN] Inisialisasi Qwen gagal: {e}")

    if gemini is None and (os.getenv("DASHSCOPE_API_GEMINI_KEY") or os.getenv("GEMINI_API_KEY")):
        try:
            gemini = GeminiClient()
        except Exception as e:
            print(f"[WARN] Inisialisasi Gemini gagal: {e}")

    days = fetch_earnings_days(ticker)
    headlines = fetch_headlines(ticker)

    if not headlines:
        return Research(
            ticker=ticker,
            days_to_earnings=days,
            news_sentiment="unknown",
            narrative="No headlines available",
            confidence=0.0,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            consensus="NO_DATA",
        )

    headline_text = "\n".join(f"- {h}" for h in headlines)
    prompt = SENTIMENT_PROMPT.format(ticker=ticker, headlines=headline_text)

    qwen_res: Optional[dict] = None
    if qwen:
        try:
            qwen_res = qwen.chat_json([
                {"role": "system", "content": "Kamu analis saham US profesional. Output HANYA JSON valid."},
                {"role": "user", "content": prompt},
            ])
        except Exception as e:
            print(f"[ERROR] Qwen analysis error {ticker}: {e}")

    gemini_res: Optional[dict] = None
    if gemini:
        try:
            gemini_res = gemini.chat_json(prompt)
        except Exception as e:
            print(f"[ERROR] Gemini analysis error {ticker}: {e}")

    return synthesize_dual_ai(ticker, qwen_res, gemini_res, days)


if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    print(f"Testing Dual-AI research (Qwen + Gemini) untuk {ticker}...")
    r = research_stock(ticker)
    print(json.dumps(r.__dict__, indent=2, ensure_ascii=False))
