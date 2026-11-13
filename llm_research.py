"""
Qwen (Alibaba DashScope International) client untuk research layer.
OpenAI-compatible mode.

Butuh env:
    DASHSCOPE_API_KEY — dari https://modelstudio.console.alibabacloud.com/ap-southeast-1/
    (opsional) DASHSCOPE_MODEL — default "qwen-plus"
"""

import os
import json
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Literal, Optional
import httpx

DASHSCOPE_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = os.getenv("DASHSCOPE_MODEL", "qwen-plus")


@dataclass
class Research:
    ticker: str
    days_to_earnings: Optional[int]
    news_sentiment: Literal["bullish", "bearish", "neutral", "unknown"]
    narrative: str
    confidence: float  # 0-1
    fetched_at: str


class QwenClient:
    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL):
        self.api_key = api_key or os.environ["DASHSCOPE_API_KEY"]
        self.model = model
        self.client = httpx.Client(
            base_url=DASHSCOPE_BASE,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    def chat(self, messages: list[dict], temperature: float = 0.3) -> str:
        """Standard OpenAI-compatible chat completion."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        r = self.client.post("/chat/completions", json=payload)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]

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


SENTIMENT_PROMPT = """Kamu analis saham US. Analisis headlines terakhir untuk ticker {ticker} dan jawab dalam JSON.

Headlines (7 hari terakhir):
{headlines}

Jawab dalam format JSON:
{{
  "sentiment": "bullish" | "bearish" | "neutral",
  "confidence": 0.0-1.0,
  "narrative": "1-2 kalimat ringkasan narasi dominan dalam Bahasa Indonesia",
  "key_drivers": ["list", "faktor", "utama"],
  "upcoming_risk_event": "earnings/product launch/lawsuit/other/none"
}}

Jika data tidak cukup untuk evaluasi, set sentiment: "neutral" dan confidence: 0.3."""


def fetch_headlines(ticker: str, limit: int = 10) -> list[str]:
    """Ambil headlines dari Yahoo Finance search endpoint (gratis, no key)."""
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


def research_stock(ticker: str, qwen: Optional[QwenClient] = None) -> Research:
    """Full research bundle dengan Qwen sebagai sentiment engine."""
    qwen = qwen or QwenClient()
    days = fetch_earnings_days(ticker)
    headlines = fetch_headlines(ticker)

    if not headlines:
        return Research(
            ticker=ticker,
            days_to_earnings=days,
            news_sentiment="unknown",
            narrative="no headlines available",
            confidence=0.0,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )

    headline_text = "\n".join(f"- {h}" for h in headlines)
    prompt = SENTIMENT_PROMPT.format(ticker=ticker, headlines=headline_text)

    try:
        result = qwen.chat_json([
            {"role": "system", "content": "Kamu analis saham US profesional. Output HANYA JSON valid."},
            {"role": "user", "content": prompt},
        ])
        return Research(
            ticker=ticker,
            days_to_earnings=days,
            news_sentiment=result.get("sentiment", "neutral"),
            narrative=result.get("narrative", ""),
            confidence=float(result.get("confidence", 0.5)),
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as e:
        print(f"[ERROR] Qwen analysis failed for {ticker}: {e}")
        return Research(
            ticker=ticker,
            days_to_earnings=days,
            news_sentiment="unknown",
            narrative=f"analysis error: {e}",
            confidence=0.0,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )


if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    print(f"Testing research untuk {ticker}...")
    r = research_stock(ticker)
    print(json.dumps(r.__dict__, indent=2, ensure_ascii=False))
