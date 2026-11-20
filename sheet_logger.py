"""
Google Sheets & Local CSV Logger for Walt Bot (Phase 1 Logging).
Mencatat hasil daily scan, evaluasi Swarm AI (P1-P7), dan eksekusi order ke:
  1. Google Spreadsheet via Webhook (Google Apps Script Web App)
  2. Google Spreadsheet via gsheets CLI (jika terinstall lokal)
  3. Local CSV Audit Log (logs/scan_history.csv & logs/executions_history.csv)
"""

import os
import io
import csv
import json
import time
import logging
import subprocess
from datetime import datetime, timezone
from typing import Any, Optional
import httpx

log = logging.getLogger("sheet_logger")

# R6-1: ID spreadsheet WAJIB dari env `GOOGLE_SHEET_ID`. Sebelumnya ada nilai default hardcoded di
# sini, sehingga (a) repo publik memuat identifier dan (b) kegagalan konfigurasi muncul sebagai
# HTTP 401 senyap, bukan sebagai konfigurasi yang hilang. Sekarang: tanpa ID => status `no_config`
# dan nol percobaan HTTP.
DEFAULT_WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "")
FALLBACK_LOG_DIR = "logs"

# Status yang berarti ledger TIDAK bisa dibaca dari sheet (harus berbunyi, bukan log.debug).
LEDGER_UNREADABLE_STATUSES = frozenset(
    {"http_error", "html_or_unparsable", "header_mismatch", "empty", "no_config"}
)
_LEDGER_WARN_INTERVAL_SEC = 900.0
_ledger_warn_at: dict[str, float] = {}
_ledger_last_status: dict[str, tuple[str, str]] = {}


def get_spreadsheet_id(explicit: Optional[str] = None) -> str:
    """ID spreadsheet dari argumen eksplisit atau env GOOGLE_SHEET_ID (kosong = tidak dikonfigurasi)."""
    return (explicit or os.environ.get("GOOGLE_SHEET_ID") or "").strip()


def sheet_fingerprint() -> dict[str, Any]:
    """
    Identifikasi sheet yang SEDANG dipakai lingkungan ini tanpa membocorkan ID-nya (repo publik).
    sha8 = 8 hex pertama SHA-256 dari ID; cukup untuk mencocokkan dengan ID yang disebut pemilik
    (yang juga dihitung dengan cara yang sama) tanpa menyalin nilainya ke log/Step Summary.
    """
    import hashlib

    sid = get_spreadsheet_id()
    return {
        "configured": bool(sid),
        "length": len(sid),
        "sha8": hashlib.sha256(sid.encode("utf-8")).hexdigest()[:8] if sid else "",
    }


def get_ledger_read_status(sheet_name: str) -> str:
    """Status pembacaan terakhir sheet_name: ok|local_csv|http_error|html_or_unparsable|
    header_mismatch|empty|no_config|unknown (belum pernah membaca pada proses ini)."""
    return _ledger_last_status.get(sheet_name, ("unknown", ""))[0]


def get_ledger_read_detail(sheet_name: str) -> str:
    return _ledger_last_status.get(sheet_name, ("unknown", ""))[1]


def reset_ledger_warning_state() -> None:
    """Kosakkan jendela agregasi (dipakai test; di produksi cukup sekali per proses)."""
    _ledger_warn_at.clear()


def _record_ledger_status(sheet_name: str, status: str, detail: str = "") -> None:
    _ledger_last_status[sheet_name] = (status, detail)


def _redact_sheet_id(text: str, sheet_id: str) -> str:
    """Pesan httpx memuat URL export (berisi ID sheet) — never print it in full (aturan Ronde 6)."""
    if sheet_id and sheet_id in text:
        return text.replace(sheet_id, "<GOOGLE_SHEET_ID>")
    return text


def _warn_ledger_unreadable(sheet_name: str, status: str, detail: str) -> None:
    """Satu baris log.warning per (sheet, status) per 15 menit — kegagalan ledger tidak boleh senyap."""
    if status not in LEDGER_UNREADABLE_STATUSES:
        return
    key = f"{sheet_name}:{status}"
    now = time.time()
    if now - _ledger_warn_at.get(key, 0.0) < _LEDGER_WARN_INTERVAL_SEC:
        return
    _ledger_warn_at[key] = now
    log.warning(
        f"LEDGER TIDAK TERTBACA dari sheet '{sheet_name}' (status={status}): {detail}"
    )


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


DAILY_SCANS_HEADERS = [
    "timestamp_utc", "balance_usdc", "open_positions", "asset", "side",
    "funding_daily_pct", "price", "swarm_score", "sentiment", "consensus",
    "vendor_alignment", "p1_polarity", "p4_momentum", "p5_catalysts", "p6_contrarian",
    "verdict", "margin_allocated", "reason", "narrative"
]

EXECUTIONS_LOG_HEADERS = [
    "Timestamp (UTC)", "Asset", "Sisi", "Order Type", "Leverage", "Margin ($)",
    "Entry Price ($)", "TP ($)", "SL ($)", "Order ID", "Status", "Keterangan",
    "Strategy Version"
]


def log_scan_to_csv(summary: dict, researches: dict[str, Any], filepath: str = "logs/scan_history.csv"):
    """Catat baris scan ke file CSV lokal."""
    _ensure_dir(filepath)
    file_exists = os.path.exists(filepath)

    headers = DAILY_SCANS_HEADERS

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


def _write_webhook_fallback_csv(payload: dict) -> Optional[str]:
    """Tulis baris gagal kirim webhook ke CSV lokal fallback agar jejak audit tidak hilang."""
    rows = payload.get("rows") or []
    if not rows:
        return None
    sheet_name = str(payload.get("sheet") or payload.get("action") or "webhook").strip()
    sheet_slug = "".join(c.lower() if c.isalnum() or c == "_" else "_" for c in sheet_name).strip("_") or "webhook"
    fallback_path = os.path.join(FALLBACK_LOG_DIR, f"{sheet_slug}_fallback.csv")
    try:
        _ensure_dir(fallback_path)
        file_exists = os.path.exists(fallback_path) and os.path.getsize(fallback_path) > 0
        header = payload.get("header")
        with open(fallback_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists and header:
                writer.writerow(header)
            writer.writerows(rows)
        log.warning(
            f"_write_webhook_fallback_csv: {len(rows)} baris sheet '{sheet_name}' "
            f"disimpan ke fallback lokal {fallback_path}"
        )
        return fallback_path
    except Exception as e:
        log.warning(
            f"_write_webhook_fallback_csv gagal menulis {fallback_path} ({type(e).__name__}): {e}"
        )
        return None


def _post_webhook(payload: dict, url: str) -> bool:
    """
    Kirim payload ke Google Sheets Webhook dengan maks 3 percobaan (jeda 1s lalu 2s).
    Bila semua percobaan gagal, tulis baris ke CSV lokal fallback di FALLBACK_LOG_DIR dan kembalikan False.
    """
    norm_url = _normalize_webhook_url(url)
    if not norm_url:
        return False

    backoff_delays = (1, 2)
    sheet_label = str(payload.get("sheet") or payload.get("action") or "unknown")
    last_err = "unknown"

    for attempt in range(1, 4):
        try:
            r = httpx.post(norm_url, json=payload, follow_redirects=True, timeout=20.0)
            if r.status_code in (200, 302):
                log.info(f"Google Sheet Webhook ({sheet_label}) status: {r.status_code}")
                return True
            last_err = f"HTTP {r.status_code}"
            log.warning(
                f"_post_webhook({sheet_label}) attempt {attempt}/3 gagal ({last_err})"
            )
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            log.warning(
                f"_post_webhook({sheet_label}) attempt {attempt}/3 error ({type(e).__name__}): {e}"
            )
        if attempt < 3:
            time.sleep(backoff_delays[attempt - 1])

    log.warning(
        f"_post_webhook({sheet_label}) gagal setelah 3 percobaan ({last_err}); menulis fallback CSV lokal"
    )
    _write_webhook_fallback_csv(payload)
    return False


def log_scan_via_webhook(rows: list[list[Any]], webhook_url: str) -> bool:
    """Kirim data scan ke Google Sheets melalui Google Apps Script Web App."""
    url = _normalize_webhook_url(webhook_url)
    if not url:
        return False
    payload = {
        "action": "log_scan",
        "sheet": "Daily_Scans",
        "header": DAILY_SCANS_HEADERS,
        "rows": rows,
    }
    return _post_webhook(payload, url)


def log_scan_via_gsheets_cli(rows: list[list[Any]], spreadsheet_id: str) -> bool:
    """Kirim data scan ke Google Sheets menggunakan gsheets CLI jika tersedia."""
    try:
        batch_req = [{"op": "append", "range": "Daily_Scans!A1", "data": rows}]
        # R6-5: path lewat modul agar fixture isolasi test bisa mengarahkan file ini (sebelumnya
        # literal "logs/..." sehingga tidak bisa diisolasi dan tidak terpantau guard state).
        tmp_json = os.path.join(FALLBACK_LOG_DIR, "_tmp_sheet_batch.json")
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
            except Exception as e:
                log.warning(f"log_scan_via_gsheets_cli gagal hapus tmp file ({type(e).__name__}): {e}")


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
    sheet_id = get_spreadsheet_id()
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

    headers = EXECUTIONS_LOG_HEADERS

    from strategy_config import get_strategy_version_hash
    strat_ver = get_strategy_version_hash()

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
            ex.get("strategy_version") or strat_ver,
        ])

    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(headers)
        writer.writerows(rows)

    webhook_ok = False
    webhook_url = _normalize_webhook_url(os.environ.get("GOOGLE_SHEET_WEBHOOK_URL", DEFAULT_WEBHOOK_URL))
    if webhook_url and rows:
        webhook_ok = _post_webhook(
            {
                "action": "log_execution",
                "sheet": "Executions_Log",
                "header": EXECUTIONS_LOG_HEADERS,
                "rows": rows,
            },
            webhook_url,
        )

    return {"executions_count": len(rows), "csv_logged": True, "webhook_logged": webhook_ok}


CLOSED_POSITIONS_HEADERS = [
    "Timestamp (UTC)", "Asset", "Sisi", "Contract ID", "Entry Price ($)",
    "Exit Price ($)", "Margin ($)", "PnL ($)", "Funding ($)", "Alasan Close",
    "Position ID", "Closed At (exchange)", "Strategy Version", "Opened At (exchange)",
    "Exit Price Source"
]

_CLOSED_POSITIONS_CANONICAL_IDX: dict[str, int] = {
    h.strip().lower(): i for i, h in enumerate(CLOSED_POSITIONS_HEADERS)
}
_CLOSED_POSITIONS_REQUIRED_COLS = (
    "asset",
    "exit price ($)",
    "margin ($)",
    "pnl ($)",
    "alasan close",
)


def resolve_closed_positions_columns(
    rows: list[list[str]],
) -> tuple[dict[str, int], list[list[str]], bool]:
    """
    Petakan nama kolom header Closed_Positions (case-insensitive) ke indeks kolom.
    Mengembalikan (col_map, data_rows, used_fallback_indices).
    Jika baris pertama tidak mengandung kolom inti yang diharapkan (header rusak/hilang),
    fallback ke indeks CLOSED_POSITIONS_HEADERS baku dengan used_fallback_indices=True.
    """
    if not rows:
        return dict(_CLOSED_POSITIONS_CANONICAL_IDX), [], False

    raw_header = [str(c).strip().lower() for c in rows[0]]
    header_idx = {name: i for i, name in enumerate(raw_header) if name}
    has_required = all(col in header_idx for col in _CLOSED_POSITIONS_REQUIRED_COLS)

    if has_required:
        # Mulai dari indeks baku untuk kolom tambahan (10..14) agar file campuran header 10-kolom
        # + baris 14-kolom tetap terbaca konsisten sebelum & sesudah migrasi, lalu timpa dengan
        # posisi aktual yang tertulis pada header.
        col_map = dict(_CLOSED_POSITIONS_CANONICAL_IDX)
        col_map.update(header_idx)
        return col_map, rows[1:], False

    # Header rusak / hilang -> fallback indeks lama; lewati baris pertama jika jelas bukan data trade
    first_is_non_data_header = len(rows[0]) < 10 or ("timestamp" in (raw_header[0] if raw_header else ""))
    data_rows = rows[1:] if first_is_non_data_header else rows
    return dict(_CLOSED_POSITIONS_CANONICAL_IDX), data_rows, True


def fetch_sheet_csv_rows_ex(
    sheet_name: str,
    spreadsheet_id: Optional[str] = None,
    expected_col_name: Optional[str] = None,
    local_csv_fallback: Optional[str] = None,
) -> tuple[list[list[str]], str]:
    """
    Sama seperti fetch_sheet_csv_rows, tapi mengembalikan (rows, status) sehingga pemanggil bisa
    membedakan "sheet benar-benar kosong" dari "sheet tidak bisa dibaca".

    status: ok | local_csv | http_error | html_or_unparsable | header_mismatch | empty | no_config
    Kegagalan baca sheet dicatat sebagai log.warning TERAGREGASI (1x per 15 menit per status),
    bukan log.debug (R6-1: ledger mati total tidak boleh senyap).
    """
    sheet_id = get_spreadsheet_id(spreadsheet_id)
    if not sheet_id:
        # Nol percobaan HTTP bila ID tidak dikonfigurasi.
        if local_csv_fallback and os.path.exists(local_csv_fallback):
            try:
                with open(local_csv_fallback, mode="r", encoding="utf-8") as f:
                    rows = list(csv.reader(f))
                _record_ledger_status(
                    sheet_name, "local_csv", "GOOGLE_SHEET_ID tidak diset; memakai fallback CSV lokal"
                )
                return rows, "local_csv"
            except Exception as e:
                _warn_ledger_unreadable(sheet_name, "no_config", f"fallback CSV lokal gagal dibaca: {e}")
        _record_ledger_status(sheet_name, "no_config", "GOOGLE_SHEET_ID tidak diset dan CSV lokal tidak ada")
        _warn_ledger_unreadable(sheet_name, "no_config", "GOOGLE_SHEET_ID tidak diset")
        return [], "no_config"

    export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={sheet_name}"
    sheet_rows: list[list[str]] = []
    failure_detail = ""
    try:
        r = httpx.get(export_url, follow_redirects=True, timeout=12.0)
        status_code = int(getattr(r, "status_code", 0) or 0)
        text = getattr(r, "text", "") or ""
        if status_code != 200:
            failure_detail = f"HTTP {status_code} pada gviz export sheet '{sheet_name}'"
            status = "http_error"
        elif not text.strip() or text.lstrip().startswith("<"):
            failure_detail = f"HTTP {status_code} tapi body bukan CSV (kemungkinan halaman login/HTML)"
            status = "html_or_unparsable"
        else:
            parsed = list(csv.reader(io.StringIO(text)))
            if not parsed:
                failure_detail = "CSV gviz mengembalikan 0 baris"
                status = "empty"
            elif expected_col_name and expected_col_name not in [c.strip() for c in parsed[0]]:
                # gviz fallback ke sheet pertama bila nama tab tidak ada -> jangan dianggap data.
                failure_detail = (
                    f"kolom '{expected_col_name}' tidak ditemukan di header (tab '{sheet_name}' mungkin tidak ada)"
                )
                status = "header_mismatch"
            else:
                _record_ledger_status(sheet_name, "ok", f"HTTP {status_code}, {len(parsed)} baris")
                return parsed, "ok"
    except Exception as e:
        status = "http_error"
        failure_detail = f"{type(e).__name__}: {_redact_sheet_id(str(e)[:200], sheet_id)}"

    _warn_ledger_unreadable(sheet_name, status, failure_detail)
    _record_ledger_status(sheet_name, status, failure_detail)

    if local_csv_fallback and os.path.exists(local_csv_fallback):
        try:
            with open(local_csv_fallback, mode="r", encoding="utf-8") as f:
                sheet_rows = list(csv.reader(f))
            _record_ledger_status(
                sheet_name, "local_csv", f"fallback CSV lokal setelah {status}: {failure_detail}"
            )
            return sheet_rows, "local_csv"
        except Exception as e:
            _record_ledger_status(sheet_name, status, f"{failure_detail} | fallback lokal gagal: {e}")
            return [], status

    return [], status


def fetch_sheet_csv_rows(
    sheet_name: str,
    spreadsheet_id: Optional[str] = None,
    expected_col_name: Optional[str] = None,
    local_csv_fallback: Optional[str] = None,
) -> list[list[str]]:
    """
    Ambil seluruh baris dari sheet tertentu via CSV export URL Google Sheets.
    Jika expected_col_name diberikan, memvalidasi bahwa header baris pertama mengandung kolom tersebut
    (mencegah false-positive saat Google Sheets gviz fallback ke sheet pertama jika sheet belum ada).
    Fallback ke file CSV lokal jika Google Sheet tidak dapat diakses publik.

    Wrapper kompatibilitas: status pembacaan tersedia via fetch_sheet_csv_rows_ex /
    get_ledger_read_status().
    """
    rows, _status = fetch_sheet_csv_rows_ex(
        sheet_name,
        spreadsheet_id=spreadsheet_id,
        expected_col_name=expected_col_name,
        local_csv_fallback=local_csv_fallback,
    )
    return rows


def log_close(
    asset: str,
    side: str,
    contract_id: int,
    entry_price: float,
    exit_price: Optional[float],
    margin: float,
    pnl_quote: Optional[float],
    funding_quote: float,
    reason: str,
    position_id: str = "",
    closed_at: str = "",
    opened_at: str = "",
    filepath: str = "logs/closed_history.csv",
    exit_source: str = "",
    webhook: bool = True,
) -> dict:
    """
    Catat penutupan posisi ke sheet 'Closed_Positions' via webhook dan ke logs/closed_history.csv.
    Jika exit_price / pnl_quote None (data close tidak tersedia), kolom ditulis kosong ("").

    webhook=False dipakai oleh backfill riwayat dari API bursa (R6-1): menulis 52 baris lama ke
    spreadsheet bukan operasi read-only dan belum disetujui, sehingga cukup masuk ledger lokal.
    """
    _ensure_dir(filepath)
    file_exists = os.path.exists(filepath) and os.path.getsize(filepath) > 0

    from strategy_config import get_strategy_version_hash
    strat_ver = get_strategy_version_hash()

    ts = datetime.now(timezone.utc).isoformat()
    exit_cell: Any = "" if exit_price is None else round(float(exit_price), 6)
    pnl_cell: Any = "" if pnl_quote is None else round(float(pnl_quote), 4)
    row = [
        ts,
        asset,
        str(side or "").upper(),
        contract_id,
        round(float(entry_price or 0.0), 6),
        exit_cell,
        round(float(margin or 0.0), 4),
        pnl_cell,
        round(float(funding_quote or 0.0), 4),
        reason,
        str(position_id or ""),
        str(closed_at or ""),
        strat_ver,
        str(opened_at or ""),
        str(exit_source or ""),
    ]

    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(CLOSED_POSITIONS_HEADERS)
        writer.writerow(row)

    webhook_ok = False
    webhook_url = _normalize_webhook_url(os.environ.get("GOOGLE_SHEET_WEBHOOK_URL", DEFAULT_WEBHOOK_URL))
    if webhook and webhook_url:
        webhook_ok = _post_webhook(
            {
                "action": "log_close",
                "sheet": "Closed_Positions",
                "header": CLOSED_POSITIONS_HEADERS,
                "rows": [row],
            },
            webhook_url,
        )

    return {"csv_logged": True, "webhook_logged": webhook_ok, "row": row}


PNL_TRACKER_HEADERS = [
    "Tanggal", "Total Ekuitas ($)", "Funding Earned Bulan Ini ($)",
    "Progress (%)", "Target ($)", "Status"
]


def get_closed_funding_current_month(
    month_prefix: Optional[str] = None,
    closed_csv_path: str = "logs/closed_history.csv",
) -> float:
    """
    Hitung total funding_quote dari posisi yang sudah ditutup pada bulan UTC berjalan
    (dari sheet Closed_Positions via CSV export atau fallback ke logs/closed_history.csv).
    """
    if not month_prefix:
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")

    rows = fetch_sheet_csv_rows(
        "Closed_Positions",
        expected_col_name="Exit Price ($)",
        local_csv_fallback=closed_csv_path,
    )
    if not rows:
        return 0.0

    col_map, data_rows, used_fallback = resolve_closed_positions_columns(rows)
    if used_fallback:
        log.warning(
            "get_closed_funding_current_month: header Closed_Positions rusak/kolom hilang, memakai fallback indeks baku"
        )
    ts_idx = col_map.get("timestamp (utc)", 0)
    funding_idx = col_map.get("funding ($)", 8)
    total = 0.0
    for r in data_rows:
        if len(r) <= max(ts_idx, funding_idx):
            continue
        ts_val = str(r[ts_idx]).strip()
        if ts_val.startswith(month_prefix):
            try:
                raw_f = str(r[funding_idx]).replace("$", "").replace("+", "").strip()
                total += float(raw_f) if raw_f else 0.0
            except Exception as e:
                log.warning(f"get_closed_funding_current_month gagal parse funding ({type(e).__name__}): {e}")
    return round(total, 6)


def log_pnl_tracker(summary: dict, filepath: str = "logs/pnl_tracker.csv") -> dict:
    """
    Catat 1 baris harian (idempotent per tanggal UTC) ke sheet 'PnL_Tracker' dan logs/pnl_tracker.csv.
    Kolom: tanggal, total ekuitas, funding earned bulan berjalan, progress %, target, status.
    """
    ts = str(summary.get("ts") or datetime.now(timezone.utc).isoformat())
    date_utc = ts[:10]

    existing_rows = fetch_sheet_csv_rows(
        "PnL_Tracker",
        local_csv_fallback=filepath,
    )
    if existing_rows:
        last_row = existing_rows[-1]
        if last_row and str(last_row[0]).strip().startswith(date_utc):
            log.info(f"PnL_Tracker untuk tanggal {date_utc} sudah tercatat (idempotent skip).")
            return {"logged": False, "reason": "already_logged_today", "date": date_utc}

    total_equity = float(summary.get("total_equity", 0.0) or 0.0)
    if total_equity <= 0:
        accounts = summary.get("balance", [])
        if accounts:
            total_equity = float(accounts[0].get("balance_available", 0.0) or 0.0)

    funding_earned = round(float(summary.get("funding_earned", 0.0) or 0.0), 4)
    target = round(float(summary.get("target", 100.0) or 100.0), 2)
    progress_pct = round(float(summary.get("progress_pct", (funding_earned / target * 100) if target else 0.0)), 2)
    status = "ON TRACK" if progress_pct >= 50 else ("BEHIND" if progress_pct >= 20 else "JAUH")

    row = [
        date_utc,
        round(total_equity, 2),
        funding_earned,
        progress_pct,
        target,
        status,
    ]

    _ensure_dir(filepath)
    file_exists = os.path.exists(filepath) and os.path.getsize(filepath) > 0
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(PNL_TRACKER_HEADERS)
        writer.writerow(row)

    webhook_ok = False
    webhook_url = _normalize_webhook_url(os.environ.get("GOOGLE_SHEET_WEBHOOK_URL", DEFAULT_WEBHOOK_URL))
    if webhook_url:
        webhook_ok = _post_webhook(
            {
                "action": "log_pnl",
                "sheet": "PnL_Tracker",
                "header": PNL_TRACKER_HEADERS,
                "rows": [row],
            },
            webhook_url,
        )

    return {"logged": True, "webhook_logged": webhook_ok, "date": date_utc, "row": row}


def _is_research_fresh(
    timestamp_str: Any,
    max_age_hours: float,
    now_utc: Optional[datetime] = None,
) -> bool:
    """Return True hanya jika timestamp riset valid dan umurnya <= max_age_hours."""
    raw = str(timestamp_str or "").strip()
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = now_utc or datetime.now(timezone.utc)
        age_hours = (now - dt).total_seconds() / 3600.0
        return 0.0 <= age_hours <= max_age_hours or age_hours < 0.0
    except Exception as e:
        log.warning(f"_is_research_fresh gagal parse timestamp '{raw}' ({type(e).__name__}): {e}")
        return False


def fetch_latest_research_from_sheet(
    webhook_url: Optional[str] = None,
    csv_path: str = "logs/scan_history.csv",
    max_age_hours: Optional[float] = None,
    now_utc: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Mengambil ringkasan riset terbaru per aset dari:
    1. Google Sheets Webhook via GET request (online/terpusat)
    2. File CSV audit lokal (offline fallback)
    Mengabaikan riset yang lebih tua dari max_age_hours (default 6 jam).
    """
    from llm_research import Research
    if max_age_hours is None:
        try:
            from strategy_config import load_strategy_config
            max_age_hours = float(load_strategy_config().get("max_research_age_hours", 6.0))
        except Exception as e:
            log.warning(f"fetch_latest_research_from_sheet gagal baca max_research_age_hours ({type(e).__name__}): {e}")
            max_age_hours = 6.0

    url = _normalize_webhook_url(webhook_url or os.environ.get("GOOGLE_SHEET_WEBHOOK_URL", DEFAULT_WEBHOOK_URL))
    if url:
        try:
            r = httpx.get(url, follow_redirects=True, timeout=12.0)
            if r.status_code == 200:
                res = r.json()
                if isinstance(res, dict) and res.get("status") == "success":
                    data = res.get("data", {})
                    researches: dict[str, Research] = {}
                    stale_skipped = 0
                    for asset, item in data.items():
                        if not _is_research_fresh(item.get("timestamp"), max_age_hours, now_utc=now_utc):
                            stale_skipped += 1
                            continue
                        score = 0.0
                        try:
                            raw_score = str(item.get("swarm_score", "0")).replace("+", "")
                            score = float(raw_score) if raw_score and raw_score != "—" else 0.0
                        except Exception as e:
                            log.warning(f"fetch_latest_research_from_sheet gagal parse swarm_score {asset} ({type(e).__name__}): {e}")
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
                                except Exception as e:
                                    log.warning(f"fetch_latest_research_from_sheet gagal parse {p_key} {asset} ({type(e).__name__}): {e}")

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
                    if stale_skipped:
                        log.info(f"⏳ {stale_skipped} data riset dari Webhook diabaikan karena basi (> {max_age_hours:.1f} jam).")
                    if researches:
                        log.info(f"✓ Berhasil memuat {len(researches)} data riset segar dari Google Sheets Webhook ({list(researches.keys())})")
                        return researches
        except Exception as e:
            log.warning(f"Gagal mengambil riset dari Google Sheets Webhook: {e}")

    # Fallback ke CSV lokal jika ada
    if os.path.exists(csv_path):
        try:
            with open(csv_path, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                researches = {}
                stale_skipped = 0
                for row in reader:
                    asset = row.get("asset", "").strip().upper()
                    if not asset:
                        continue
                    if not _is_research_fresh(row.get("timestamp_utc"), max_age_hours, now_utc=now_utc):
                        stale_skipped += 1
                        continue
                    score = 0.0
                    try:
                        raw = row.get("swarm_score", "0").replace("+", "")
                        score = float(raw) if raw and raw != "—" else 0.0
                    except Exception as e:
                        log.warning(f"fetch_latest_research_from_sheet(csv) gagal parse swarm_score {asset} ({type(e).__name__}): {e}")
                        score = 0.0
                    
                    sent = (row.get("sentiment") or "unknown").lower()
                    if sent not in ("bullish", "bearish", "neutral", "unknown"):
                        sent = "unknown"

                    p1_to_p7 = {}
                    for col_k, norm_k in (
                        ("p1_polarity", "P1_sentiment_polarity"),
                        ("p4_momentum", "P4_momentum_direction"),
                        ("p5_catalysts", "P5_risk_catalysts"),
                        ("p6_contrarian", "P6_contrarian_check"),
                    ):
                        raw_v = row.get(col_k)
                        if raw_v and raw_v != "—":
                            try:
                                p1_to_p7[norm_k] = float(str(raw_v).replace("+", ""))
                            except Exception as e:
                                log.warning(f"fetch_latest_research_from_sheet(csv) gagal parse {col_k} {asset} ({type(e).__name__}): {e}")

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
                        p1_to_p7_scores=p1_to_p7,
                    )
                if stale_skipped:
                    log.info(f"⏳ {stale_skipped} baris riset CSV lokal diabaikan karena basi (> {max_age_hours:.1f} jam).")
                if researches:
                    log.info(f"✓ Berhasil memuat {len(researches)} data riset segar dari file CSV lokal ({list(researches.keys())})")
                    return researches
        except Exception as e:
            log.warning(f"Gagal membaca riset dari CSV lokal: {e}")

    return {}

