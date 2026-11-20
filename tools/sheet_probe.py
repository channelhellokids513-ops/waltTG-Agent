#!/usr/bin/env python3
"""
Probe baca Google Sheet lewat gviz CSV export. READ-ONLY dan TANPA kredensial Walt:
jalur baca ini tidak menyentuh OAuth sama sekali, jadi hasilnya tetap bisa diamati bahkan
ketika refresh token mati (kondisi 2026-11-20).

Keluaran disanitasi: ID sheet hanya sebagai sha8, tanpa nilai saldo/harga per akun, tanpa baris mentah.
Dipakai di diagnostics.yml dan bisa dijalankan lokal:
    GOOGLE_SHEET_ID=... python tools/sheet_probe.py
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

TABS = ("Daily_Scans", "Executions_Log", "Closed_Positions", "PnL_Tracker")

# Status HTTP terakhir per panggilan fetch (untuk membedakan "ID salah" (404) dari
# "sheet privat" (401) tanpa pernah mencetak ID-nya).
LAST_HTTP_STATUS: dict[str, str] = {}

# Indeks baku Closed_Positions (sama dengan sheet_logger._CLOSED_POSITIONS_CANONICAL_IDX)
IDX_ENTRY, IDX_EXIT, IDX_PNL, IDX_REASON, IDX_PID, IDX_CLOSED_AT, IDX_STRATEGY, IDX_OPENED_AT, IDX_SRC = (
    4, 5, 7, 9, 10, 11, 12, 13, 14,
)
MEASURED_SOURCES = ("exchange_fill", "response_position", "mark_at_detection", "mark_price_proxy",
                    "exchange_history_no_exit", "snapshot_estimate")


def sheet_fingerprint(sheet_id: str) -> dict[str, Any]:
    sid = (sheet_id or "").strip()
    return {
        "configured": bool(sid),
        "length": len(sid),
        "sha8": hashlib.sha256(sid.encode("utf-8")).hexdigest()[:8] if sid else "",
    }


def fetch_gviz_rows(sheet_id: str, tab: str, timeout: float = 20.0) -> tuple[list[list[str]], str]:
    """(rows, status) dengan status ok|http_error|html_or_unparsable|empty|no_config."""
    sid = (sheet_id or "").strip()
    if not sid:
        return [], "no_config"
    url = f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&sheet={tab}"
    LAST_HTTP_STATUS.clear()
    try:
        r = httpx.get(url, follow_redirects=True, timeout=timeout)
    except Exception as exc:  # jaringan/DNS — status jujur, bukan kosong
        LAST_HTTP_STATUS["detail"] = f"exception {type(exc).__name__}"
        return [], "http_error"
    LAST_HTTP_STATUS["detail"] = f"HTTP {getattr(r, 'status_code', '?')}"
    if int(getattr(r, "status_code", 0)) != 200:
        return [], "http_error"
    text = getattr(r, "text", "") or ""
    if not text.strip() or text.lstrip().startswith("<"):
        return [], "html_or_unparsable"
    rows = list(csv.reader(io.StringIO(text)))
    return (rows, "ok") if rows else ([], "empty")


def _cell(row: list[str], idx: int) -> str:
    return str(row[idx]).strip() if 0 <= idx < len(row) else ""


def _as_float(value: str) -> float | None:
    try:
        return float(value.replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def census_closed(rows: list[list[str]]) -> dict[str, Any]:
    """
    Klasifikasi baris Closed_Positions tanpa menambah skema baru:
      measured   : ada PnL dan sumber harga yang nyata (atau angka yang bukan nol)
      estimated  : harga keluar berupa estimasi/proxy (dinamai jujur, tetap terhitung)
      disputed   : PnL 0.00 dengan Exit == Entry dan TANPA sumber harga (warisan pra-Ronde 5)
      unknown    : Exit & PnL kosong, atau alasan mengandung UNKNOWN
      backfill   : sumber exchange_history_no_exit (riwayat bursa tanpa harga keluar)
    """
    data = rows[1:] if rows else []
    out = {
        "rows_data": len(data),
        "measured": 0,
        "estimated": 0,
        "disputed_zero": 0,
        "unknown": 0,
        "backfill_no_exit": 0,
        "row_widths": sorted({len(r) for r in data}) or [],
        "last_closed_at": "",
        "strategy_versions": sorted({_cell(r, IDX_STRATEGY) for r in data if _cell(r, IDX_STRATEGY)})[:4],
    }
    estimated_sources = {"mark_at_detection", "mark_price_proxy", "snapshot_estimate"}
    for r in data:
        src = _cell(r, IDX_SRC).lower()
        raw_exit = _cell(r, IDX_EXIT)
        raw_pnl = _cell(r, IDX_PNL)
        reason = _cell(r, IDX_REASON).lower()
        if src == "exchange_history_no_exit":
            out["backfill_no_exit"] += 1
            continue
        if (not raw_exit and not raw_pnl) or "unknown" in reason:
            out["unknown"] += 1
            continue
        entry = _as_float(_cell(r, IDX_ENTRY))
        exit_v = _as_float(raw_exit)
        pnl_v = _as_float(raw_pnl)
        if (
            entry is not None and exit_v is not None and pnl_v is not None
            and entry > 0 and abs(exit_v - entry) <= 1e-9 and abs(pnl_v) <= 1e-9
            and src in ("", "entry_fallback")
        ):
            out["disputed_zero"] += 1
            continue
        if src in estimated_sources:
            out["estimated"] += 1
        else:
            out["measured"] += 1
        closed_at = _cell(r, IDX_CLOSED_AT)
        if closed_at > out["last_closed_at"]:
            out["last_closed_at"] = closed_at
    return out


def probe(sheet_id: str, tabs: tuple[str, ...] = TABS) -> dict[str, Any]:
    result: dict[str, Any] = {"fingerprint": sheet_fingerprint(sheet_id), "tabs": {}}
    for tab in tabs:
        rows, status = fetch_gviz_rows(sheet_id, tab)
        header = [c.strip() for c in rows[0]] if rows else []
        info: dict[str, Any] = {
            "status": status,
            "http": LAST_HTTP_STATUS.get("detail", "") if status != "no_config" else "tanpa HTTP",
            "data_rows": max(0, len(rows) - 1) if status == "ok" else 0,
            "header_cols": len(header),
            "named_header_cols": sum(1 for c in header if c),
        }
        if tab == "Closed_Positions" and status == "ok":
            info["census"] = census_closed(rows)
        if tab == "Executions_Log" and status == "ok" and len(rows) > 1:
            stamps = [str(r[0]).strip() for r in rows[1:] if r and str(r[0]).strip()]
            info["last_timestamp"] = max(stamps)[-25:] if stamps else ""
        result["tabs"][tab] = info
    return result


def load_env_fallback(path: str = ".env") -> str:
    """Ambil GOOGLE_SHEET_ID dari .env bila environment tidak menyediakannya (paritas dengan walt_bot)."""
    sid = (os.environ.get("GOOGLE_SHEET_ID") or "").strip()
    if sid or not os.path.exists(path):
        return sid
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                if raw.lstrip().startswith("#") or "=" not in raw:
                    continue
                key, _, val = raw.partition("=")
                if key.strip() == "GOOGLE_SHEET_ID":
                    return val.strip().strip('"').strip("'")
    except Exception:
        return ""
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe baca gviz Google Sheet (read-only, tanpa Walt).")
    parser.add_argument("--sheet-id", default=None)
    parser.add_argument("--json", action="store_true", help="Cetak JSON lengkap (tetap tersanitasi).")
    args = parser.parse_args(argv)
    # Sumber ID: argumen > environment. Berkas .env pengembang TIDAK dibaca di sini — itu khusus
    # blok __main__ (lihat bawah), supaya proses test tidak pernah menyerap kredensial nyata dan
    # tidak pernah mengirim HTTP sungguhan.
    sheet_id = (args.sheet_id or os.environ.get("GOOGLE_SHEET_ID") or "").strip()

    fp = sheet_fingerprint(sheet_id)
    if not fp["configured"]:
        print("LEDGER SHEET TIDAK DIKONFIGURASI (GOOGLE_SHEET_ID kosong) — nol percobaan HTTP")
        return 2
    print(f"Sheet fingerprint: sha8={fp['sha8']} length={fp['length']} (ID tidak pernah dicetak)")

    result = probe(sheet_id)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    for tab, info in result["tabs"].items():
        http_note = "" if info["status"] == "ok" else f" ({info.get('http', '?')})"
        line = f"  {tab:<17} {info['status']:<10}{http_note} rows={info['data_rows']:<4} header={info['header_cols']}kol"
        if info.get("named_header_cols") is not None:
            line += f" ({info['named_header_cols']} bernama)"
        if "census" in info:
            c = info["census"]
            line += (
                f" | measured={c['measured']} estimated={c['estimated']} disputed={c['disputed_zero']}"
                f" unknown={c['unknown']} backfill={c['backfill_no_exit']}"
            )
        if info.get("last_timestamp"):
            line += f" | terakhir={info['last_timestamp']}"
        print(line)
    return 0


if __name__ == "__main__":
    # Hanya di jalur CLI: kembalikan kenyamanan membaca .env tanpa menyeretnya ke dalam proses test.
    if not (os.environ.get("GOOGLE_SHEET_ID") or "").strip():
        _fb = load_env_fallback()
        if _fb:
            os.environ["GOOGLE_SHEET_ID"] = _fb
    sys.exit(main(sys.argv[1:]))
