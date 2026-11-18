"""
Google Sheets & Local CSV Logger for Walt Bot (Phase 1 Logging).
Mencatat hasil daily scan, evaluasi Swarm AI (P1-P7), dan eksekusi order ke:
  1. Google Spreadsheet via Webhook (Google Apps Script Web App)
  2. Google Spreadsheet via gsheets CLI (jika terinstall lokal)
  3. Local CSV Audit Log (logs/scan_history.csv & logs/executions_history.csv)
"""

import os
import csv
import json
import logging
import subprocess
from datetime import datetime, timezone
from typing import Any, Optional
import httpx

log = logging.getLogger("sheet_logger")

DEFAULT_SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID", "1ag6MqYnOLdZgkFwk_cs3W-22EOX5e5DGQTVvPWENHTg")
DEFAULT_WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "")


def _ensure_dir(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _normalize_webhook_url(url: str) -> str:
    """Menjamin format Webhook URL selalu lengkap dengan protocol https:// dan format Web App."""
    url = (url or "").strip().strip('"').strip("'")
    if not url:
        return ""
    if url.startswith("AKfycb"):
        return f"https://script.google.com/macros/s/{url}/exec"
    if not url.startswith(("http://", "https://")):
        if "script.google.com" in url:
            return f"https://{url}"
        elif "/" not in url:
            return f"https://script.google.com/macros/s/{url}/exec"
        return f"https://{url}"
    return url


def log_scan_to_csv(summary: dict, researches: dict[str, Any], filepath: str = "logs/scan_history.csv"):
    """Catat baris scan ke file CSV lokal."""
    _ensure_dir(filepath)
    file_exists = os.path.exists(filepath)

    headers = [
        "timestamp_utc", "balance_usdc", "open_positions", "asset", "side",
        "funding_daily_pct", "price", "swarm_score", "sentiment", "consensus",
        "vendor_alignment", "p1_polarity", "p4_momentum", "p5_catalysts", "p6_contrarian",
        "verdict", "margin_allocated", "reason", "narrative"
    ]

    ts = summary.get("ts") or datetime.now(timezone.utc).isoformat()
    balance = "0.0"
    accounts = summary.get("balance", [])
    if accounts:
        balance = str(accounts[0].get("balance_available", "0.0"))
    pos_count = summary.get("positions_count", 0)

    rows = []
    for opp in summary.get("top_opportunities", []):
        asset = opp["asset"]
        side = opp["side"].upper()
        funding = opp["funding_daily_pct"]
        price = opp.get("price", 0.0)
        res = researches.get(asset)

        # Decision
        from walt_bot import decide_go_nogo, DEFAULT_MARGIN
        from types import SimpleNamespace
        pseudo_opp = SimpleNamespace(base_asset=asset, side=opp["side"], funding_daily=funding/100)
        verdict, reason, mult = decide_go_nogo(pseudo_opp, res)
        margin = DEFAULT_MARGIN * mult

        score = f"{res.final_score:+.2f}" if res and hasattr(res, "final_score") else "—"
        sent = res.news_sentiment if res else "—"
        cons = getattr(res, "overall_consensus_level", "—") if res else "—"
        align = getattr(res, "vendor_alignment", "—") if res else "—"
        p1_7 = getattr(res, "p1_to_p7_scores", {}) if res else {}
        p1 = f"{p1_7.get('P1_sentiment_polarity', 0):+.2f}" if p1_7 else "—"
        p4 = f"{p1_7.get('P4_momentum_direction', 0):+.2f}" if p1_7 else "—"
        p5 = f"{p1_7.get('P5_risk_catalysts', 0):.2f}" if p1_7 else "—"
        p6 = f"{p1_7.get('P6_contrarian_check', 0):.2f}" if p1_7 else "—"
        narrative = getattr(res, "narrative", "") if res else ""

        rows.append([
            ts, balance, pos_count, asset, side, f"{funding:+.3f}%",
            f"{price:.4f}", score, sent, cons, align, p1, p4, p5, p6,
            verdict, f"{margin:.2f}", reason, narrative
        ])

    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(headers)
        writer.writerows(rows)

    return rows


def log_scan_via_webhook(rows: list[list[Any]], webhook_url: str) -> bool:
    """Kirim data scan ke Google Sheets melalui Google Apps Script Web App."""
    url = _normalize_webhook_url(webhook_url)
    if not url:
        return False
    try:
        payload = {"action": "log_scan", "sheet": "Daily_Scans", "rows": rows}
        r = httpx.post(url, json=payload, follow_redirects=True, timeout=20.0)
        log.info(f"Google Sheet Webhook response status: {r.status_code}")
        return r.status_code in (200, 302)
    except Exception as e:
        log.warning(f"Gagal kirim ke Google Sheets Webhook ({url[:35]}...): {e}")
        return False


def log_scan_via_gsheets_cli(rows: list[list[Any]], spreadsheet_id: str) -> bool:
    """Kirim data scan ke Google Sheets menggunakan gsheets CLI jika tersedia."""
    try:
        batch_req = [{"op": "append", "range": "Daily_Scans!A1", "data": rows}]
        tmp_json = "logs/_tmp_sheet_batch.json"
        _ensure_dir(tmp_json)
        with open(tmp_json, "w", encoding="utf-8") as f:
            json.dump(batch_req, f)

        cmd = f'gsheets mutate batch {spreadsheet_id} -f "{tmp_json}"'
        res = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=25.0
        )
        return res.returncode == 0
    except Exception as e:
        log.warning(f"gsheets CLI logging failed: {e}")
        return False
    finally:
        if os.path.exists(tmp_json):
            try:
                os.remove(tmp_json)
            except Exception:
                pass


def log_scan(summary: dict, researches: dict[str, Any]) -> dict:
    """
    Eksekusi logging lengkap untuk Daily Scan:
    1. CSV lokal
    2. Google Apps Script Webhook (jika URL tersedia)
    3. gsheets CLI (jika terinstall di environment)
    """
    rows = log_scan_to_csv(summary, researches)

    webhook_ok = False
    webhook_url = os.environ.get("GOOGLE_SHEET_WEBHOOK_URL", DEFAULT_WEBHOOK_URL)
    if webhook_url:
        webhook_ok = log_scan_via_webhook(rows, webhook_url)

    cli_ok = False
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", DEFAULT_SPREADSHEET_ID)
    if not webhook_ok and sheet_id:
        cli_ok = log_scan_via_gsheets_cli(rows, sheet_id)

    return {
        "rows_count": len(rows),
        "csv_logged": True,
        "webhook_logged": webhook_ok,
        "cli_logged": cli_ok,
        "sheet_id": sheet_id,
    }


def log_execution(executions: list[dict], filepath: str = "logs/executions_history.csv") -> dict:
    """
    Catat eksekusi order ke CSV lokal dan Google Sheet (Executions_Log).
    Catatan: baris di sheet sebelum fix ini bergeser 2 kolom karena skema lama belum menyertakan kolom Order Type dan Leverage.
    """
    _ensure_dir(filepath)
    file_exists = os.path.exists(filepath)

    headers = [
        "Timestamp (UTC)", "Asset", "Sisi", "Order Type", "Leverage", "Margin ($)",
        "Entry Price ($)", "TP ($)", "SL ($)", "Order ID", "Status", "Keterangan"
    ]

    ts = datetime.now(timezone.utc).isoformat()
    rows = []
    for ex in executions:
        side_str = str(ex.get("side", "") or "").upper()
        rows.append([
            ts,
            ex.get("asset", ""),
            side_str,
            "MARKET",
            ex.get("leverage", 2),
            ex.get("margin", 0.0),
            ex.get("price", 0.0),
            ex.get("tp", ""),
            ex.get("sl", ""),
            ex.get("order_id", ""),
            ex.get("verdict", ""),
            ex.get("reason", ""),
        ])

    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(headers)
        writer.writerows(rows)

    webhook_url = _normalize_webhook_url(os.environ.get("GOOGLE_SHEET_WEBHOOK_URL", DEFAULT_WEBHOOK_URL))
    if webhook_url and rows:
        try:
            httpx.post(webhook_url, json={"action": "log_execution", "sheet": "Executions_Log", "rows": rows}, follow_redirects=True, timeout=20.0)
        except Exception as e:
            log.warning(f"Gagal kirim eksekusi ke Google Sheets Webhook: {e}")

    return {"executions_count": len(rows), "csv_logged": True}


def fetch_latest_research_from_sheet(
    webhook_url: Optional[str] = None,
    csv_path: str = "logs/scan_history.csv"
) -> dict[str, Any]:
    """
    Mengambil ringkasan riset terbaru per aset dari:
    1. Google Sheets Webhook via GET request (online/terpusat)
    2. File CSV audit lokal (offline fallback)
    
    Mengembalikan dict[asset, Research] yang siap digunakan untuk decide_go_nogo.
    """
    from llm_research import Research

    url = _normalize_webhook_url(webhook_url or os.environ.get("GOOGLE_SHEET_WEBHOOK_URL", DEFAULT_WEBHOOK_URL))
    if url:
        try:
            r = httpx.get(url, follow_redirects=True, timeout=12.0)
            if r.status_code == 200:
                res = r.json()
                if isinstance(res, dict) and res.get("status") == "success":
                    data = res.get("data", {})
                    researches: dict[str, Research] = {}
                    for asset, item in data.items():
                        score = 0.0
                        try:
                            raw_score = str(item.get("swarm_score", "0")).replace("+", "")
                            score = float(raw_score) if raw_score and raw_score != "—" else 0.0
                        except Exception:
                            score = 0.0
                        
                        sent = str(item.get("sentiment") or "unknown").lower()
                        if sent not in ("bullish", "bearish", "neutral", "unknown"):
                            sent = "unknown"
                        
                        p1_to_p7 = {}
                        for p_key in ("p1_polarity", "p4_momentum", "p5_catalysts", "p6_contrarian"):
                            val = item.get(p_key)
                            if val is not None and str(val) != "—":
                                try:
                                    clean_k = "P1_sentiment_polarity" if p_key == "p1_polarity" else (
                                        "P4_momentum_direction" if p_key == "p4_momentum" else (
                                            "P5_risk_catalysts" if p_key == "p5_catalysts" else "P6_contrarian_check"
                                        )
                                    )
                                    p1_to_p7[clean_k] = float(str(val).replace("+", ""))
                                except Exception:
                                    pass

                        researches[asset] = Research(
                            ticker=asset,
                            days_to_earnings=None,
                            news_sentiment=sent,
                            narrative=str(item.get("narrative") or item.get("reason") or ""),
                            confidence=0.80 if score != 0.0 else 0.50,
                            fetched_at=str(item.get("timestamp") or ""),
                            consensus=str(item.get("consensus") or "SINGLE_MODEL"),
                            final_score=score,
                            overall_consensus_level=str(item.get("consensus") or "SINGLE_MODEL"),
                            vendor_alignment=str(item.get("vendor_alignment") or "both_agree"),
                            p1_to_p7_scores=p1_to_p7,
                        )
                    if researches:
                        log.info(f"✓ Berhasil memuat {len(researches)} data riset dari Google Sheets Webhook ({list(researches.keys())})")
                        return researches
        except Exception as e:
            log.warning(f"Gagal mengambil riset dari Google Sheets Webhook: {e}")

    # Fallback ke CSV lokal jika ada
    if os.path.exists(csv_path):
        try:
            with open(csv_path, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                researches = {}
                for row in reader:
                    asset = row.get("asset", "").strip().upper()
                    if not asset:
                        continue
                    score = 0.0
                    try:
                        raw = row.get("swarm_score", "0").replace("+", "")
                        score = float(raw) if raw and raw != "—" else 0.0
                    except Exception:
                        score = 0.0
                    
                    sent = (row.get("sentiment") or "unknown").lower()
                    if sent not in ("bullish", "bearish", "neutral", "unknown"):
                        sent = "unknown"

                    researches[asset] = Research(
                        ticker=asset,
                        days_to_earnings=None,
                        news_sentiment=sent,
                        narrative=row.get("narrative") or row.get("reason") or "",
                        confidence=0.80 if score != 0.0 else 0.50,
                        fetched_at=row.get("timestamp_utc", ""),
                        consensus=row.get("consensus", "SINGLE_MODEL"),
                        final_score=score,
                        overall_consensus_level=row.get("consensus", "SINGLE_MODEL"),
                        vendor_alignment=row.get("vendor_alignment", "both_agree"),
                    )
                if researches:
                    log.info(f"✓ Berhasil memuat {len(researches)} data riset dari file CSV lokal ({list(researches.keys())})")
                    return researches
        except Exception as e:
            log.warning(f"Gagal membaca riset dari CSV lokal: {e}")

    return {}

