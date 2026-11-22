"""
Pipeline Persiapan Data Training SFT untuk Independent Market Research LLM Agent.

Menghasilkan sampel berformat chat-messages JSONL:
{
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "<think>...</think>..."}
  ]
}

Mencatat otomatis setiap riset valid ke:
  1. Tab Google Sheet 'Data Training' (via Apps Script webhook v2 dengan dedupe Sample ID)
  2. File lokal JSONL (`logs/training_dataset.jsonl`) yang langsung siap pakai untuk fine-tuning.

Target awal: minimum 1.000 baris data bersih (`TRAINING_TARGET_ROWS = 1000`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("training_data")

TRAINING_SHEET_NAME = "Data Training"
TRAINING_TARGET_ROWS = 1000
DEFAULT_JSONL_PATH = "logs/training_dataset.jsonl"

DATA_TRAINING_HEADERS = [
    "Sample ID",
    "Timestamp (UTC)",
    "Asset",
    "Sisi",
    "Funding/Hari",
    "Vonis",
    "Sentimen",
    "Final Score",
    "Konsensus",
    "Quality Tier",
    "Source",
    "Messages JSONL",
]

TRAINING_SYSTEM_PROMPT = (
    "Kamu adalah Walt Independent Market Research Agent, analis riset pasar & manajemen risiko "
    "independen untuk strategi perpetual funding-carry (horizon taktis 24-72 jam, SL 1.5%, TP 2.0%-2.5%). "
    "Evaluasi konteks pasar dan berita secara objektif menggunakan kerangka 7 parameter (P1 Polaritas Sentimen, "
    "P2 Kekuatan Sinyal, P3 Kredibilitas Sumber, P4 Arah Momentum, P5 Katalis Risiko, P6 Contrarian/Crowdedness, "
    "P7 Pola Historis). Tuliskan penalaran kuantitatif & evaluasi risiko secara sistematis di dalam tag "
    "<think>...</think>, lalu berikan kesimpulan keputusan terstruktur (Final Score, Sentimen, Konsensus, "
    "Vonis GO/HALF/SKIP, dan Justifikasi Risiko 24-72 jam)."
)


def is_training_logging_enabled() -> bool:
    """True bila pengumpulan data training aktif (default: on)."""
    return os.getenv("TRAINING_DATA_LOGGING", "on").strip().lower() not in (
        "off", "0", "false", "no", "disabled"
    )


def _safe_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    if val is None:
        return default
    text = str(val).strip().replace("+", "").replace("$", "").replace(",", "")
    if not text or text in ("—", "-", "N/A", "none", "None"):
        return default
    if text.endswith("%"):
        try:
            return round(float(text[:-1]) / 100.0, 6)
        except ValueError:
            return default
    try:
        return float(text)
    except ValueError:
        return default


def _format_funding_pct(funding_decimal: Optional[float]) -> str:
    if funding_decimal is None:
        return "N/A"
    return f"{funding_decimal * 100:+.3f}%/hari"


def _make_sample_id(timestamp_utc: str, asset: str, side: str, final_score: float, narrative: str) -> str:
    """ID deterministik 12-hex untuk mencegah duplikasi lintas retry/backfill."""
    ts_bucket = str(timestamp_utc or "").strip()[:16]  # resolusi menit (YYYY-MM-DDTHH:MM)
    raw = "|".join([
        ts_bucket,
        str(asset or "").strip().upper(),
        str(side or "").strip().upper(),
        f"{final_score:+.3f}",
        str(narrative or "").strip()[:120],
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _classify_quality_tier(
    *,
    has_headlines: bool,
    voters_count: int,
    consensus: str,
    narrative: str,
    is_backfill: bool = False,
) -> str:
    cons = str(consensus or "").strip().upper()
    nar_len = len(str(narrative or "").strip())
    if (
        not is_backfill
        and has_headlines
        and voters_count >= 4
        and cons in ("UNANIMOUS", "STRONG")
        and nar_len >= 120
    ):
        return "GOLD"
    if nar_len >= 80 and cons in ("UNANIMOUS", "STRONG", "MIXED", "SPLIT"):
        return "SILVER"
    return "BRONZE"


def _build_user_prompt(
    *,
    asset: str,
    side: str,
    funding_decimal: Optional[float],
    price: Optional[float],
    days_to_earnings: Optional[int],
    headlines: list[str],
    p1_to_p7: dict[str, float],
    mg_context: Optional[dict[str, Any]] = None,
    is_backfill: bool = False,
) -> str:
    lines = [
        f"Evaluasi kelayakan posisi perpetual funding-carry untuk aset {asset} (horizon taktis 24-72 jam).",
        f"- Ticker: {asset}",
        f"- Rencana Sisi: {side}",
        f"- Funding Harian: {_format_funding_pct(funding_decimal)}",
    ]
    if price is not None and price > 0:
        lines.append(f"- Harga Live: ${price:.4f}")
    if days_to_earnings is not None:
        lines.append(f"- Jadwal Earnings: H-{days_to_earnings}")
    if mg_context and str(mg_context.get("mg_status", "")).startswith("ok"):
        mg_parts = []
        if mg_context.get("mg_regime"):
            mg_parts.append(f"regime={mg_context['mg_regime']}")
        if mg_context.get("mg_trend_bias"):
            mg_parts.append(f"trend_bias={mg_context['mg_trend_bias']}")
        if mg_context.get("mg_net_gravity") not in (None, ""):
            mg_parts.append(f"net_gravity={mg_context['mg_net_gravity']}")
        if mg_parts:
            lines.append(f"- Struktur Likuiditas (Market Gravity): {', '.join(mg_parts)}")

    clean_headlines = [str(h).strip() for h in (headlines or []) if str(h).strip()]
    if clean_headlines:
        lines.append("\nHeadlines Berita 7 Hari Terakhir:")
        for h in clean_headlines[:10]:
            lines.append(f"- {h}")
    elif is_backfill:
        # Data historis dari Daily_Scans menyertakan metrik sinyal P1/P4/P5/P6 terukur
        metric_bits = []
        for key, label in (
            ("P1_sentiment_polarity", "P1 Polaritas"),
            ("P4_momentum_direction", "P4 Momentum"),
            ("P5_risk_catalysts", "P5 Katalis Risiko"),
            ("P6_contrarian_check", "P6 Crowdedness"),
        ):
            if key in p1_to_p7:
                metric_bits.append(f"{label}={p1_to_p7[key]:+.2f}")
        if metric_bits:
            lines.append(f"- Sinyal Terukur Pra-Sintesis: {', '.join(metric_bits)}")
    lines.append(
        "\nBerikan analisa P1-P7, evaluasi risiko terhadap SL 1.5% / TP 2.0%-2.5%, "
        "dan tentukan vonis akhir (GO, HALF, atau SKIP)."
    )
    return "\n".join(lines)


def _build_think_block(
    *,
    asset: str,
    side: str,
    funding_decimal: Optional[float],
    sentiment: str,
    final_score: float,
    consensus: str,
    vendor_alignment: str,
    verdict: str,
    decision_reason: str,
    p1_to_p7: dict[str, float],
    days_to_earnings: Optional[int],
    voter_verdicts: Optional[dict[str, Any]] = None,
    disagreement_summary: Optional[list[str]] = None,
) -> str:
    steps = [
        f"1. Konteks Strategi: Rencana {side} {asset} dengan funding {_format_funding_pct(funding_decimal)} "
        f"(target panen funding 24-72 jam, proteksi SL 1.5%).",
    ]
    p1 = p1_to_p7.get("P1_sentiment_polarity")
    p2 = p1_to_p7.get("P2_signal_strength")
    p3 = p1_to_p7.get("P3_source_credibility")
    p4 = p1_to_p7.get("P4_momentum_direction")
    p5 = p1_to_p7.get("P5_risk_catalysts")
    p6 = p1_to_p7.get("P6_contrarian_check")
    p7 = p1_to_p7.get("P7_historical_pattern")

    score_bits = []
    for lbl, val in (
        ("P1_polarity", p1), ("P2_strength", p2), ("P3_credibility", p3),
        ("P4_momentum", p4), ("P5_risk", p5), ("P6_contrarian", p6), ("P7_history", p7),
    ):
        if val is not None:
            score_bits.append(f"{lbl}={val:+.2f}")
    if score_bits:
        steps.append(f"2. Evaluasi Parameter P1-P7: {', '.join(score_bits)}.")
    else:
        steps.append(f"2. Evaluasi Arah Sentimen: final_score={final_score:+.2f} ({sentiment}).")

    # Ringkasan alasan dari voter (jika berasal dari riset live)
    if voter_verdicts:
        voter_notes = []
        for vk, vv in sorted(voter_verdicts.items()):
            params = (vv or {}).get("params") or {}
            p1_r = str((params.get("P1_sentiment_polarity") or {}).get("reasoning") or "").strip()
            p5_r = str((params.get("P5_risk_catalysts") or {}).get("reasoning") or "").strip()
            note = p1_r or p5_r
            if note:
                voter_notes.append(f"{vk}: {note[:140]}")
        if voter_notes:
            steps.append("3. Perspektif Multi-Voter: " + " | ".join(voter_notes[:4]))

    guard_notes = []
    if days_to_earnings is not None:
        guard_notes.append(
            f"earnings H-{days_to_earnings} ({'RISIKO TINGGI <=7h' if days_to_earnings <= 7 else 'aman >7h'})"
        )
    if p5 is not None:
        guard_notes.append(f"P5={p5:.2f} ({'melewati batas risiko 0.70' if p5 >= 0.70 else 'terkendali'})")
    if p6 is not None:
        guard_notes.append(f"P6={p6:.2f} ({'crowded >=0.85' if p6 >= 0.85 else 'tidak crowded'})")
    guard_notes.append(f"konsensus={consensus}, vendor_alignment={vendor_alignment}")
    if disagreement_summary:
        clean_dis = [str(d).strip() for d in disagreement_summary if str(d).strip()][:3]
        if clean_dis:
            guard_notes.append("divergensi: " + "; ".join(clean_dis))

    steps.append("4. Pemeriksaan Guard Risiko: " + "; ".join(guard_notes) + ".")
    steps.append(
        f"5. Penetapan Keputusan: Arah sentimen '{sentiment}' (skor {final_score:+.2f}) terhadap rencana "
        f"{side} menghasilkan vonis {verdict} ({decision_reason})."
    )
    return "\n".join(steps)


def _build_assistant_answer(
    *,
    asset: str,
    side: str,
    sentiment: str,
    final_score: float,
    consensus: str,
    vendor_alignment: str,
    verdict: str,
    decision_reason: str,
    narrative: str,
    p1_to_p7: dict[str, float],
) -> str:
    structured = {
        "ticker": asset,
        "planned_side": side,
        "sentiment": sentiment,
        "final_score": round(final_score, 3),
        "consensus": consensus,
        "vendor_alignment": vendor_alignment,
        "verdict": verdict,
        "decision_reason": decision_reason,
        "p1_to_p7": {k: round(float(v), 3) for k, v in p1_to_p7.items()},
        "narrative": narrative.strip(),
    }
    return json.dumps(structured, ensure_ascii=False)


def validate_training_jsonl_line(line: str) -> tuple[bool, str]:
    """Validasi ketat satu baris JSONL sesuai format SFT `{"messages": [...]}`."""
    try:
        obj = json.loads(line)
    except Exception as exc:
        return False, f"json_invalid: {exc}"
    if not isinstance(obj, dict) or set(obj.keys()) != {"messages"}:
        return False, "root harus berupa objek dengan satu key 'messages'"
    msgs = obj.get("messages")
    if not isinstance(msgs, list) or len(msgs) != 3:
        return False, "messages harus berisi tepat 3 pesan (system, user, assistant)"
    expected_roles = ("system", "user", "assistant")
    for idx, exp_role in enumerate(expected_roles):
        m = msgs[idx]
        if not isinstance(m, dict) or m.get("role") != exp_role:
            return False, f"pesan[{idx}] harus memiliki role='{exp_role}'"
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            return False, f"pesan[{idx}].content kosong"
    asst = msgs[2]["content"]
    if not asst.startswith("<think>") or "</think>" not in asst:
        return False, "assistant content wajib memiliki blok <think>...</think>"
    after_think = asst.split("</think>", 1)[1].strip()
    if not after_think:
        return False, "jawaban final setelah </think> tidak boleh kosong"
    return True, "ok"


def build_training_sample(
    *,
    timestamp_utc: str,
    asset: str,
    side: str,
    funding_daily: Optional[float],
    price: Optional[float],
    verdict: str,
    decision_reason: str,
    research: Any,
    mg_context: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Konversi satu objek `Research` + keputusan bot menjadi 1 baris siap tulis ke sheet 'Data Training'."""
    if research is None:
        return None
    sentiment = str(getattr(research, "news_sentiment", "unknown") or "unknown").strip().lower()
    consensus = str(
        getattr(research, "overall_consensus_level", None)
        or getattr(research, "consensus", "NONE")
        or "NONE"
    ).strip().upper()
    narrative = str(getattr(research, "narrative", "") or "").strip()

    # Saring data tidak bermutu (tanpa berita / semua voter gagal / narasi kosong)
    if sentiment not in ("bullish", "bearish", "neutral"):
        return None
    if consensus in ("NONE", "NO_DATA", "FALLBACK_RULE_BASED", ""):
        return None
    if len(narrative) < 30:
        return None

    ticker = str(asset or getattr(research, "ticker", "")).strip().upper()
    side_u = str(side or "").strip().upper() or "SHORT"
    verdict_u = str(verdict or "").strip().upper() or "SKIP"
    final_score = round(float(getattr(research, "final_score", 0.0) or 0.0), 3)
    vendor_align = str(getattr(research, "vendor_alignment", "split") or "split").strip()
    p1_to_p7 = dict(getattr(research, "p1_to_p7_scores", None) or {})
    headlines = list(getattr(research, "headlines", None) or [])
    days_earnings = getattr(research, "days_to_earnings", None)
    voters_count = int(getattr(research, "voters_count", 0) or 0)
    voter_verdicts = getattr(research, "voter_verdicts", None) or {}
    disagreements = getattr(research, "disagreement_summary", None) or []
    sec_provider = str(getattr(research, "secondary_provider", "gemini") or "gemini").strip().lower()
    source = "live_swarm_glm" if sec_provider == "glm" else "live_swarm_gemini"

    ts_iso = str(timestamp_utc or getattr(research, "fetched_at", "") or datetime.now(timezone.utc).isoformat()).strip()
    funding_dec = _safe_float(funding_daily, default=None)
    if funding_dec is not None:
        funding_dec = round(funding_dec, 6)
    price_f = _safe_float(price, default=None)

    user_prompt = _build_user_prompt(
        asset=ticker,
        side=side_u,
        funding_decimal=funding_dec,
        price=price_f,
        days_to_earnings=days_earnings,
        headlines=headlines,
        p1_to_p7=p1_to_p7,
        mg_context=mg_context,
        is_backfill=False,
    )
    think_text = _build_think_block(
        asset=ticker,
        side=side_u,
        funding_decimal=funding_dec,
        sentiment=sentiment,
        final_score=final_score,
        consensus=consensus,
        vendor_alignment=vendor_align,
        verdict=verdict_u,
        decision_reason=decision_reason,
        p1_to_p7=p1_to_p7,
        days_to_earnings=days_earnings,
        voter_verdicts=voter_verdicts,
        disagreement_summary=disagreements,
    )
    answer_text = _build_assistant_answer(
        asset=ticker,
        side=side_u,
        sentiment=sentiment,
        final_score=final_score,
        consensus=consensus,
        vendor_alignment=vendor_align,
        verdict=verdict_u,
        decision_reason=decision_reason,
        narrative=narrative,
        p1_to_p7=p1_to_p7,
    )

    messages_obj = {
        "messages": [
            {"role": "system", "content": TRAINING_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": f"<think>\n{think_text}\n</think>\n{answer_text}"},
        ]
    }
    jsonl_line = json.dumps(messages_obj, ensure_ascii=False)
    ok, err = validate_training_jsonl_line(jsonl_line)
    if not ok:
        log.warning(f"build_training_sample({ticker}) ditolak validator: {err}")
        return None

    quality = _classify_quality_tier(
        has_headlines=bool(headlines),
        voters_count=voters_count,
        consensus=consensus,
        narrative=narrative,
        is_backfill=False,
    )
    sample_id = _make_sample_id(ts_iso, ticker, side_u, final_score, narrative)

    return {
        "Sample ID": sample_id,
        "Timestamp (UTC)": ts_iso,
        "Asset": ticker,
        "Sisi": side_u,
        "Funding/Hari": "" if funding_dec is None else funding_dec,
        "Vonis": verdict_u,
        "Sentimen": sentiment,
        "Final Score": final_score,
        "Konsensus": consensus,
        "Quality Tier": quality,
        "Source": source,
        "Messages JSONL": jsonl_line,
    }


def build_training_sample_from_daily_scan_row(row_dict: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Konversi satu baris bersih dari tab `Daily_Scans` menjadi sampel `Data Training` (backfill)."""
    ts_iso = str(row_dict.get("Timestamp (UTC)") or row_dict.get("timestamp_utc") or "").strip()
    asset = str(row_dict.get("Asset") or row_dict.get("asset") or "").strip().upper()
    side = str(row_dict.get("Sisi") or row_dict.get("side") or "").strip().upper()
    sentiment = str(row_dict.get("Sentimen") or row_dict.get("sentiment") or "").strip().lower()
    consensus = str(row_dict.get("Konsensus") or row_dict.get("consensus") or "").strip().upper()
    vendor_align = str(row_dict.get("Vendor Alignment") or row_dict.get("vendor_alignment") or "both_agree").strip()
    verdict = str(row_dict.get("Vonis") or row_dict.get("verdict") or "").strip().upper()
    reason = str(row_dict.get("Alasan Keputusan") or row_dict.get("reason") or "").strip()
    narrative = str(row_dict.get("Narasi Swarm AI") or row_dict.get("narrative") or "").strip()

    if not asset or sentiment not in ("bullish", "bearish", "neutral"):
        return None
    if consensus in ("", "—", "NONE", "NO_DATA") or verdict not in ("GO", "HALF", "SKIP"):
        return None
    if len(narrative) < 30:
        return None

    funding_dec = _safe_float(row_dict.get("Funding/Hari ()") or row_dict.get("funding_daily_pct"), default=None)
    if funding_dec is not None:
        funding_dec = round(funding_dec, 6)
    price_f = _safe_float(row_dict.get("Live Price ($)") or row_dict.get("price"), default=None)
    final_score = _safe_float(row_dict.get("Swarm Score") or row_dict.get("swarm_score"), default=0.0) or 0.0
    final_score = round(final_score, 3)

    p1_to_p7: dict[str, float] = {}
    for col_a, col_b, pkey in (
        ("P1 (Polaritas)", "p1_polarity", "P1_sentiment_polarity"),
        ("P4 (Momentum)", "p4_momentum", "P4_momentum_direction"),
        ("P5 (Katalis)", "p5_catalysts", "P5_risk_catalysts"),
        ("P6 (Crowded)", "p6_contrarian", "P6_contrarian_check"),
    ):
        val = _safe_float(row_dict.get(col_a) if col_a in row_dict else row_dict.get(col_b), default=None)
        if val is not None:
            p1_to_p7[pkey] = round(val, 3)

    mg_context = {
        k: row_dict.get(k, "")
        for k in ("mg_status", "mg_net_gravity", "mg_bull_pressure", "mg_regime", "mg_trend_bias")
        if row_dict.get(k) not in (None, "")
    }

    user_prompt = _build_user_prompt(
        asset=asset,
        side=side or "SHORT",
        funding_decimal=funding_dec,
        price=price_f,
        days_to_earnings=None,
        headlines=[],
        p1_to_p7=p1_to_p7,
        mg_context=mg_context or None,
        is_backfill=True,
    )
    think_text = _build_think_block(
        asset=asset,
        side=side or "SHORT",
        funding_decimal=funding_dec,
        sentiment=sentiment,
        final_score=final_score,
        consensus=consensus,
        vendor_alignment=vendor_align,
        verdict=verdict,
        decision_reason=reason or f"Keputusan {verdict} berdasarkan skor {final_score:+.2f}",
        p1_to_p7=p1_to_p7,
        days_to_earnings=None,
    )
    answer_text = _build_assistant_answer(
        asset=asset,
        side=side or "SHORT",
        sentiment=sentiment,
        final_score=final_score,
        consensus=consensus,
        vendor_alignment=vendor_align,
        verdict=verdict,
        decision_reason=reason,
        narrative=narrative,
        p1_to_p7=p1_to_p7,
    )

    messages_obj = {
        "messages": [
            {"role": "system", "content": TRAINING_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": f"<think>\n{think_text}\n</think>\n{answer_text}"},
        ]
    }
    jsonl_line = json.dumps(messages_obj, ensure_ascii=False)
    ok, _ = validate_training_jsonl_line(jsonl_line)
    if not ok:
        return None

    quality = _classify_quality_tier(
        has_headlines=False,
        voters_count=4 if consensus in ("UNANIMOUS", "STRONG") else 2,
        consensus=consensus,
        narrative=narrative,
        is_backfill=True,
    )
    sample_id = _make_sample_id(ts_iso, asset, side or "SHORT", final_score, narrative)

    return {
        "Sample ID": sample_id,
        "Timestamp (UTC)": ts_iso,
        "Asset": asset,
        "Sisi": side or "SHORT",
        "Funding/Hari": "" if funding_dec is None else funding_dec,
        "Vonis": verdict,
        "Sentimen": sentiment,
        "Final Score": final_score,
        "Konsensus": consensus,
        "Quality Tier": quality,
        "Source": "backfill_daily_scans",
        "Messages JSONL": jsonl_line,
    }


def _append_local_jsonl(samples: list[dict[str, Any]], jsonl_path: str = DEFAULT_JSONL_PATH) -> int:
    """Append sampel unik (berdasarkan konten baris JSONL) ke berkas lokal JSONL."""
    if not samples:
        return 0
    parent = os.path.dirname(jsonl_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    existing_hashes: set[str] = set()
    if os.path.exists(jsonl_path):
        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s:
                        existing_hashes.add(hashlib.sha256(s.encode("utf-8")).hexdigest()[:16])
        except Exception as exc:
            log.debug(f"Gagal baca existing JSONL {jsonl_path}: {exc}")

    written = 0
    with open(jsonl_path, "a", encoding="utf-8") as f:
        for sample in samples:
            line = str(sample.get("Messages JSONL") or "").strip()
            if not line:
                continue
            h = hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]
            if h in existing_hashes:
                continue
            f.write(line + "\n")
            existing_hashes.add(h)
            written += 1
    return written


def log_training_samples(
    samples: list[dict[str, Any]],
    *,
    webhook_url: Optional[str] = None,
    jsonl_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Catat batch sampel training ke:
      1. File lokal JSONL (`logs/training_dataset.jsonl`)
      2. Tab Google Sheet 'Data Training' via webhook v2 (dengan dedupe Sample ID).
    """
    if not is_training_logging_enabled():
        return {"enabled": False, "samples_count": 0, "jsonl_written": 0, "webhook_logged": False}

    target_jsonl = DEFAULT_JSONL_PATH if jsonl_path is None else jsonl_path

    # Dedupe dalam batch
    seen: set[str] = set()
    unique_samples: list[dict[str, Any]] = []
    for s in samples or []:
        sid = str(s.get("Sample ID") or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        unique_samples.append(s)

    if not unique_samples:
        return {"enabled": True, "samples_count": 0, "jsonl_written": 0, "webhook_logged": False}

    jsonl_written = _append_local_jsonl(unique_samples, jsonl_path=target_jsonl)

    import sheet_logger

    url = sheet_logger._normalize_webhook_url(
        webhook_url
        if webhook_url is not None
        else os.environ.get("GOOGLE_SHEET_WEBHOOK_URL", sheet_logger.DEFAULT_WEBHOOK_URL)
    )
    webhook_ok = False
    if url:
        rows = [[s.get(col, "") for col in DATA_TRAINING_HEADERS] for s in unique_samples]
        payload = {
            "action": "log_training_data",
            "sheet": TRAINING_SHEET_NAME,
            "header": DATA_TRAINING_HEADERS,
            "records": unique_samples,
            "rows": rows,
            "dedupe": {"field": "Sample ID", "scan_rows": 1500},
        }
        webhook_ok = sheet_logger._post_webhook(payload, url)

    return {
        "enabled": True,
        "samples_count": len(unique_samples),
        "jsonl_written": jsonl_written,
        "webhook_logged": webhook_ok,
        "sheet": TRAINING_SHEET_NAME,
        "target_rows": TRAINING_TARGET_ROWS,
    }
