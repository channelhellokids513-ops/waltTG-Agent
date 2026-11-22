"""Utilitas ekspor & cek progres dataset SFT dari tab Google Sheet 'Data Training' ke file JSONL siap fine-tune.

Contoh pemakaian:
    python tools/export_training_data.py --output logs/walt_research_sft.jsonl --min-quality SILVER
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sheet_logger
from training_data import (
    TRAINING_SHEET_NAME,
    TRAINING_TARGET_ROWS,
    validate_training_jsonl_line,
)

QUALITY_ORDER = {"BRONZE": 0, "SILVER": 1, "GOLD": 2}


def export_dataset(
    output_path: str = "logs/walt_research_sft.jsonl",
    min_quality: str = "BRONZE",
    local_jsonl_fallback: str = "logs/training_dataset.jsonl",
) -> dict:
    min_rank = QUALITY_ORDER.get(min_quality.upper(), 0)
    rows, status = sheet_logger.fetch_sheet_csv_rows_ex(
        TRAINING_SHEET_NAME,
        expected_col_name="Messages JSONL",
    )

    lines: list[str] = []
    quality_counts: Counter[str] = Counter()
    verdict_counts: Counter[str] = Counter()
    invalid_count = 0

    if rows and len(rows) > 1:
        header = [c.strip() for c in rows[0]]
        col_idx = {name: i for i, name in enumerate(header)}
        msg_i = col_idx.get("Messages JSONL", -1)
        qual_i = col_idx.get("Quality Tier", -1)
        verd_i = col_idx.get("Vonis", -1)
        for r in rows[1:]:
            if msg_i < 0 or len(r) <= msg_i:
                continue
            q = (r[qual_i].strip().upper() if qual_i >= 0 and len(r) > qual_i else "SILVER") or "SILVER"
            if QUALITY_ORDER.get(q, 0) < min_rank:
                continue
            raw_line = r[msg_i].strip()
            ok, _ = validate_training_jsonl_line(raw_line)
            if not ok:
                invalid_count += 1
                continue
            lines.append(raw_line)
            quality_counts[q] += 1
            if verd_i >= 0 and len(r) > verd_i and r[verd_i].strip():
                verdict_counts[r[verd_i].strip().upper()] += 1
        source = f"sheet:{TRAINING_SHEET_NAME}"
    elif os.path.exists(local_jsonl_fallback):
        with open(local_jsonl_fallback, "r", encoding="utf-8") as f:
            for ln in f:
                s = ln.strip()
                if not s:
                    continue
                ok, _ = validate_training_jsonl_line(s)
                if ok:
                    lines.append(s)
                else:
                    invalid_count += 1
        source = f"local:{local_jsonl_fallback}"
    else:
        source = f"empty ({status})"

    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")

    progress_pct = round((len(lines) / TRAINING_TARGET_ROWS) * 100.0, 2)
    return {
        "source": source,
        "output_path": output_path,
        "exported_rows": len(lines),
        "target_rows": TRAINING_TARGET_ROWS,
        "progress_pct": progress_pct,
        "remaining_to_target": max(0, TRAINING_TARGET_ROWS - len(lines)),
        "invalid_skipped": invalid_count,
        "quality_breakdown": dict(quality_counts),
        "verdict_breakdown": dict(verdict_counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export Data Training SFT JSONL dari Google Sheet")
    parser.add_argument("--output", default="logs/walt_research_sft.jsonl")
    parser.add_argument("--min-quality", default="BRONZE", choices=["BRONZE", "SILVER", "GOLD"])
    args = parser.parse_args()
    summary = export_dataset(output_path=args.output, min_quality=args.min_quality)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
