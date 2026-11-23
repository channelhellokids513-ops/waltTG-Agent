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
import re
import time
import logging
import subprocess
from datetime import datetime, timedelta, timezone
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


# Ronde 7: jalur BACA RISET memakai registrasi status yang sama dengan label khusus, supaya
# "fallback senyap ke CSV lokal" tidak lagi mirip pembacaan webhook yang sukses. Webhook Apps
# Script terbukti membalas 200 + non-JSON pada 2026-11-20 dan itu tidak muncul di mana pun.
RESEARCH_READ_SHEET = "Research_Read"
_research_read_diagnosis: str = ""
_research_read_source: str = ""


def _set_research_source(value: str) -> None:
    global _research_read_source
    _research_read_source = value


def get_research_read_status() -> str:
    """ok | local_csv | empty | http_error | html_or_unparsable | no_config | unknown."""
    return get_ledger_read_status(RESEARCH_READ_SHEET)


def get_research_read_detail() -> str:
    return get_ledger_read_detail(RESEARCH_READ_SHEET)


def _diagnose_non_json(body: str) -> str:
    """Namakan penyebab balasan non-JSON supaya tindakannya jelas bagi operator.

    Kondisi yang dibedakan (dari isi halaman, bukan tebakan):
      - body kosong                  -> `kosong` (deployment tanpa output / salah path)
      - tanpa doGet                  -> `deployment_tanpa_doGet` (Deploy → New version)
      - halaman sign-in/konsen       -> `butuh_akses_publik` (Who has access ≠ Anyone)
      - teks error Apps Script       -> `error_apps_script`
      - HTML lain                    -> `halaman_html_apps_script`
      - mulai seperti JSON tapi rusak -> `json_rusak`
    """
    text = (body or "").strip()
    if not text:
        return "kosong"
    low = text[:4000].lower()
    if "script function not found" in low and "doget" in low:
        return "deployment_tanpa_doGet"
    if "sign in" in low or "accounts.google.com" in low or "consent" in low:
        return "butuh_akses_publik"
    if text[0] in "{[":
        return "json_rusak"
    if "<!doctype html" in low or "<html" in low:
        # diperiksa SEBELUM penanda error teks: halaman error Apps Script juga HTML
        return "halaman_html_apps_script"
    if "exception" in low or low.startswith("error:") or "quota" in low:
        return "error_apps_script"
    return "bukan_json_tanpa_struktur"


def _body_cuplikan(body: str, limit: int = 48) -> str:
    """Bentuk ringkas satu baris dari awalan body, untuk diagnosis di Step Summary."""
    return " ".join((body or "")[:limit].split())


def get_research_read_diagnosis() -> str:
    """Diagnosis kegagalan pembacaan riset terakhir (`''` bila berhasil / belum mencoba)."""
    return _research_read_diagnosis


def _parse_research_response(resp: Any) -> Optional[dict]:
    """Decode balasan webhook riset; kembalikan None (dan catat status) bila bukan JSON sukses.

    200 + HTML adalah KEFAILAN SENSOR, bukan keberhasilan: halaman consent/error Apps Script
    juga bernilai 200. Status tercatat agar pemanggil bisa membunyikannya di Step Summary.
    """
    global _research_read_diagnosis
    try:
        payload = resp.json()
    except Exception as exc:
        diagnosis = _diagnose_non_json(getattr(resp, "text", "") or "")
        _research_read_diagnosis = diagnosis
        body = getattr(resp, "text", "") or ""
        detail = (
            f"content_type={resp.headers.get('content-type', '?')}; diagnosa={diagnosis}; "
            f"panjang_body={len(body)}; cuplikan='{_body_cuplikan(body)}'; "
            f"galat={_redact_sheet_id(str(exc), get_spreadsheet_id())[:60]}"
        )
        _record_ledger_status(RESEARCH_READ_SHEET, "html_or_unparsable", detail)
        _warn_ledger_unreadable(RESEARCH_READ_SHEET, "html_or_unparsable", "webhook riset membalas non-JSON")
        return None
    _research_read_diagnosis = ""
    if not isinstance(payload, dict) or payload.get("status") != "success":
        _record_ledger_status(RESEARCH_READ_SHEET, "status_not_success", f"payload={str(payload)[:60]}")
        return None
    return payload


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
    "verdict", "margin_allocated", "reason", "narrative",
    # 11 kolom diagnostik Market Gravity (v2 webhook menambahkannya di ujung kanan
    # berdasarkan nama, sehingga baris lama tetap pada kolomnya sendiri).
    "mg_status", "mg_net_gravity", "mg_bull_pressure", "mg_regime", "mg_trend_bias",
    "mg_upper_1_price", "mg_upper_1_score", "mg_upper_1_state",
    "mg_lower_1_price", "mg_lower_1_score", "mg_lower_1_state",
]

EXECUTIONS_LOG_HEADERS = [
    "Timestamp (UTC)", "Asset", "Sisi", "Order Type", "Leverage", "Margin ($)",
    "Entry Price ($)", "TP ($)", "SL ($)", "Order ID", "Status", "Keterangan",
    "Strategy Version"
]

# --- Diagnostik Market Gravity (lapisan logging SAJA; bukan input keputusan) -----
# Nilai ini adalah skor tarikan likuiditas RELATIF, bukan probabilitas, dan sengaja
# tidak pernah dibaca scan_with_research / market_execute / gerbang E5.
MG_COLUMNS = [
    "mg_status", "mg_net_gravity", "mg_bull_pressure", "mg_regime", "mg_trend_bias",
    "mg_upper_1_price", "mg_upper_1_score", "mg_upper_1_state",
    "mg_lower_1_price", "mg_lower_1_score", "mg_lower_1_state",
]
MG_BLANK = dict.fromkeys(MG_COLUMNS, "")

# Label kolom `Daily_Scans` yang SEBENARNYA ada di sheet live (19 kolom lama, ditulis
# Apps Script dengan label cantik berbahasa Indonesia). Webhook v2 membaca record dengan
# `record[header[c]]` - pencarian KEY PERSIS, bukan ternormalisasi - jadi mengirim nama
# internal snake_case membuat 4 kolom tertulis KOSONG dan 15 kolom lain dianggap baru lalu
# DITAMBAHKAN DOBEL di kanan sheet (kerusakan permanen pada header sheet).
DAILY_SCANS_SHEET_HEADERS = [
    "Timestamp (UTC)", "Saldo (USDC)", "Posisi Terbuka", "Asset", "Sisi",
    "Funding/Hari ()", "Live Price ($)", "Swarm Score", "Sentimen", "Konsensus",
    "Vendor Alignment", "P1 (Polaritas)", "P4 (Momentum)", "P5 (Katalis)",
    "P6 (Crowded)", "Vonis", "Alokasi Margin ($)", "Alasan Keputusan",
    "Narasi Swarm AI",
    *MG_COLUMNS,
]

# Nama internal -> label sheet, untuk satu-satunya jalur tulis ke `Daily_Scans`.
_SHEET_LABEL_TO_INTERNAL = dict(zip(DAILY_SCANS_SHEET_HEADERS, DAILY_SCANS_HEADERS))
_INTERNAL_TO_SHEET_LABEL = {v: k for k, v in _SHEET_LABEL_TO_INTERNAL.items()}

# Kolom persen: satu-satunya nilai yang format teksnya berbeda dari satuan di sheet.
FUNDING_SHEET_COLUMN = "Funding/Hari ()"


def _normalize_research_item(item: Any) -> dict[str, Any]:
    """Petakan key label-sheet (v2 `doGet`) kembali ke nama internal yang dipakai pembaca.

    v2 mengembalikan record dengan key header sheet ("Timestamp (UTC)", "Swarm Score",
    "Sentimen", ...), sementara pembaca lama mencari "timestamp"/"swarm_score"/"sentiment".
    Sebelum ada pemetaan ini, SEMUA aset terbaca basi dan jalur baca selalu jatuh ke gviz
    meskipun deployment webhook sudah benar. Key internal yang sudah benar (payload lama)
    dibiarkan apa adanya, jadi fungsi ini tetap kompatibel ke belakang.
    """
    if not isinstance(item, dict):
        return {}
    out = dict(item)
    for key, value in item.items():
        internal = _SHEET_LABEL_TO_INTERNAL.get(str(key))
        if internal is not None and out.get(internal) in (None, ""):
            out[internal] = value
    if out.get("timestamp") in (None, "") and out.get("timestamp_utc") not in (None, ""):
        # Pembaca riset memakai nama kanonik "timestamp" (sama seperti jalur gviz).
        out["timestamp"] = out["timestamp_utc"]
    return out


_ledger_write_status: dict[str, tuple[str, str]] = {}


def get_ledger_write_status(sheet_name: str) -> str:
    """ok|unverified_legacy_response|not_success|not_verified|http_error|no_config|unknown."""
    entry = _ledger_write_status.get(sheet_name)
    return entry[0] if entry else "unknown"


def get_ledger_write_detail(sheet_name: str) -> str:
    entry = _ledger_write_status.get(sheet_name)
    return entry[1] if entry else "belum pernah menulis pada proses ini"


def _record_ledger_write(sheet_name: str, status: str, detail: str = "") -> None:
    _ledger_write_status[sheet_name] = (status, detail)


_gravity_cache: dict[str, dict[str, str]] = {}


def gravity_diagnostics(asset: str) -> dict[str, str]:
    """Skor struktur Market Gravity untuk satu aset, untuk kolom diagnostik.

    Fail-closed secara eksplisit: bila bar tidak bisa diambil atau jendela terlalu
    pendek, kolom diisi KOSONG dengan mg_status penyebabnya - tidak pernah 0.0,
    karena nol akan terbaca sebagai "tidak ada magnet" padahal yang tidak ada adalah data.
    Sekali dihitung per aset per proses (cache) karena nilai ini deterministik.
    """
    key = str(asset or "").upper()
    if not key:
        return dict(MG_BLANK)
    if key in _gravity_cache:
        return dict(_gravity_cache[key])
    if os.environ.get("MG_DIAGNOSTICS", "on").strip().lower() in {"off", "0", "false", "no"}:
        blank = dict(MG_BLANK)
        blank["mg_status"] = "disabled"
        _gravity_cache[key] = blank
        return dict(blank)

    try:
        limit = int(os.environ.get("MG_DIAG_LIMIT", "600"))
        interval = os.environ.get("MG_DIAG_INTERVAL", "1h")
    except ValueError:
        limit, interval = 600, "1h"

    payload = dict(MG_BLANK)
    try:
        from market_chart import fetch_candles
        from market_gravity import analyze_market_gravity

        candles, _source = fetch_candles(key, interval=interval, limit=limit)
        result = analyze_market_gravity(candles)
        fields = result.to_scan_fields()
        payload.update(MG_BLANK)
        payload.update({name: fields.get(name, "") for name in MG_COLUMNS})
        if result.notes:
            payload["mg_status"] = f"{result.status}:{';'.join(result.notes)[:60]}"
    except Exception as exc:  # jaringan/venue apa pun: diagnostik tidak boleh menjatuhkan scan
        payload["mg_status"] = f"error:{type(exc).__name__}"
    _gravity_cache[key] = payload
    return dict(payload)


def gravity_diagnostics_summary() -> dict[str, int]:
    """Ringkasan kesehatan diagnostik (dipakai CI): berapa aset terukur vs blank."""
    tally: dict[str, int] = {}
    for payload in _gravity_cache.values():
        status = str(payload.get("mg_status") or "unknown").split(":")[0]
        tally[status] = tally.get(status, 0) + 1
    return tally



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
        pseudo_opp = SimpleNamespace(
            base_asset=asset,
            side=opp["side"],
            funding_daily=funding / 100,
            signal_source=opp.get("signal_source", "funding"),
            forecast_setup=opp.get("forecast_setup", ""),
            expected_net_edge_pct=float(opp.get("expected_net_edge_pct") or 0.0),
            forecast_reason=opp.get("forecast_reason", ""),
            planned_sl_pct=float(opp.get("planned_sl_pct") or 0.0),
            planned_tp_pct=float(opp.get("planned_tp_pct") or 0.0),
            max_hold_hours=float(opp.get("max_hold_hours") or 0.0),
        )
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

        mg = gravity_diagnostics(asset)
        rows.append([
            ts, balance, pos_count, asset, side, f"{funding:+.3f}%",
            f"{price:.4f}", score, sent, cons, align, p1, p4, p5, p6,
            verdict, f"{margin:.2f}", reason, narrative,
            *[mg.get(name, "") for name in MG_COLUMNS],
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


def _interpret_write_response(response, expected_rows: int, sheet_label: str) -> tuple[bool, str, str]:
    """Putuskan apakah tulis ke sheet benar-benar TERBUKTI, bukan sekadar HTTP 200.

    (ok, status, detail). Webhook v2 membalas JSON berisi verified + rows_added;
    v1 hanya rows_added; deployment stub lama membalas HTML dan itu BUKAN bukti.
    """
    body = getattr(response, "text", "") or ""
    try:
        payload = response.json()
    except Exception:
        return False, "unverified_legacy_response", (
            f"balasan webhook {sheet_label} bukan JSON (status HTTP {response.status_code}); "
            "ini gejala deployment Apps Script lama/stub - kolom dan baris tidak terbukti tertulis. "
            "Redeploy google_apps_script.js (v2) lalu cek ?probe=1"
        )
    if not isinstance(payload, dict):
        return False, "not_success", f"balasan webhook {sheet_label} bukan objek: {str(payload)[:60]}"
    if payload.get("status") != "success":
        return False, "not_success", f"{payload.get('status')}/{payload.get('code')}: {str(payload.get('message'))[:80]}"

    skipped = payload.get("rows_skipped") or []
    added = int(payload.get("rows_added") or 0)
    expected_added = max(0, int(expected_rows) - len(skipped))
    if added != expected_added:
        return False, "not_verified", (
            f"{sheet_label}: rows_added={added} != expected={expected_added} "
            f"(skipped={len(skipped)}, verified={payload.get('verified')})"
        )
    mismatches = payload.get("mismatch_columns") or []
    if mismatches:
        return False, "not_verified", f"{sheet_label}: read-back beda di {len(mismatches)} kolom: {str(mismatches[0])[:90]}"
    if payload.get("verified") is True:
        return True, "ok", f"{added} baris ditulis dan terbaca kembali (versi webhook {payload.get('version', '?')})"
    if expected_rows == 0:
        return True, "ok", "header direkonsiliasi (tidak ada baris)"
    return True, "ok_v1_rows_added", (
        f"{added} baris dilaporkan tertulis; read-back tidak tersedia di versi webhook ini "
        f"(versi={payload.get('version', 'v1')})"
    )


def _post_webhook(payload: dict, url: str) -> bool:
    """
    Kirim payload ke Google Sheets Webhook dengan maks 3 percobaan (jeda 1s lalu 2s).
    Sukses HANYA bila balasan membuktikan baris tertulis (lihat _interpret_write_response);
    bila semua percobaan gagal, tulis baris ke CSV lokal fallback dan kembalikan False.
    """
    norm_url = _normalize_webhook_url(url)
    if not norm_url:
        sheet_label = str(payload.get("sheet") or payload.get("action") or "unknown")
        _record_ledger_write(sheet_label, "no_config", "GOOGLE_SHEET_WEBHOOK_URL kosong")
        return False

    backoff_delays = (1, 2)
    sheet_label = str(payload.get("sheet") or payload.get("action") or "unknown")
    expected_rows = len(payload.get("rows") or payload.get("records") or [])
    last_err = "unknown"

    for attempt in range(1, 4):
        try:
            r = httpx.post(norm_url, json=payload, follow_redirects=True, timeout=20.0)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            _record_ledger_write(sheet_label, "http_error", last_err)
            log.warning(f"_post_webhook({sheet_label}) attempt {attempt}/3 error ({last_err})")
            if attempt < 3:
                time.sleep(backoff_delays[attempt - 1])
            continue

        if r.status_code not in (200, 302):
            last_err = f"HTTP {r.status_code}"
            _record_ledger_write(sheet_label, "http_error", last_err)
            log.warning(f"_post_webhook({sheet_label}) attempt {attempt}/3 gagal ({last_err})")
            if attempt < 3:
                time.sleep(backoff_delays[attempt - 1])
            continue

        ok, status, detail = _interpret_write_response(r, expected_rows, sheet_label)
        _record_ledger_write(sheet_label, status, detail)
        if ok:
            log.info(f"Google Sheet Webhook ({sheet_label}) terbukti: {detail}")
            return True
        last_err = detail
        if status == "unverified_legacy_response":
            # Redeploy tidak akan berhasil dengan sendirinya di dalam proses yang sama.
            log.warning(f"_post_webhook({sheet_label}): {last_err}")
            break
        log.warning(f"_post_webhook({sheet_label}) attempt {attempt}/3 tidak terbukti ({last_err})")
        if attempt < 3:
            time.sleep(backoff_delays[attempt - 1])

    log.warning(
        f"_post_webhook({sheet_label}) tidak terbukti berhasil ({last_err}); menulis fallback CSV lokal"
    )
    _write_webhook_fallback_csv(payload)
    return False


def _funding_to_decimal(value: Any) -> Any:
    """`"-0.979%"` -> `-0.00979`, satuan yang SEBENARNYA tersimpan di Google Sheets.

    Sheets mengonversi string persen menjadi angka desimal saat menulis, jadi read-back v2
    (`sameValue_()`) selalu menemukan selisih tipe untuk kolom ini: write dilaporkan
    `not_verified`, `_post_webhook` retry, dan setiap retry MENAMBAH BARIS di ledger
    (terukur di #44998198762: 6 baris ditulis untuk 2 baris yang seharusnya). Mengirim nilai
    dalam satuan sheet membuat verifikasi lulus pada percobaan pertama.

    Nilai yang tidak berbentuk persen atau tidak bisa diurai dikirim APA ADANYA - tidak pernah
    diubah menjadi 0, karena nol di kolom ini bermakna "funding netral", bukan "tidak terukur".
    """
    text = str(value if value is not None else "").strip()
    if not text.endswith("%"):
        return value
    try:
        return round(float(text[:-1].replace("+", "").replace(",", "")) / 100.0, 6)
    except ValueError:
        return value


def log_scan_via_webhook(rows: list[list[Any]], webhook_url: str) -> bool:
    """Kirim data scan ke Google Sheets melalui Google Apps Script Web App.

    Dikirim sebagai `records` (bernama kolom) supaya urutan kolom di sheet tidak
    pernah bisa menggeser nilai; `header` ikut dikirim agar webhook v2 dapat
    merekonsiliasi kolom baru (mg_*) di ujung kanan tanpa menyentuh data lama.
    """
    url = _normalize_webhook_url(webhook_url)
    if not url:
        return False
    # Kunci record HARUS label sheet live, bukan nama internal: doPost v2 memakai
    # `record[header[c]]` (exact key). Urutan kolom tetap sama, jadi `rows` tetap valid
    # untuk deployment lama yang hanya menerima angka-angka posisi.
    records = [dict(zip(DAILY_SCANS_SHEET_HEADERS, row)) for row in rows]
    for rec in records:
        # Kolom ini satu-satunya yang dikirim dengan format %: sheet menyimpan desimal,
        # jadi membandingkan format yang salah = menulis baris ganda lewat retry.
        rec[FUNDING_SHEET_COLUMN] = _funding_to_decimal(rec.get(FUNDING_SHEET_COLUMN))
    payload = {
        "action": "log_scan",
        "sheet": "Daily_Scans",
        "header": DAILY_SCANS_SHEET_HEADERS,
        "records": records,
        "rows": rows,  # kompatibilitas deployment v1 yang belum menerima `records`
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
        "webhook_write_status": get_ledger_write_status("Daily_Scans"),
        "webhook_write_detail": get_ledger_write_detail("Daily_Scans"),
        "cli_logged": cli_ok,
        "sheet_id": sheet_id,
        "gravity_diag": gravity_diagnostics_summary(),
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


_last_header_mismatch: dict[str, list[str]] = {}


def get_last_header_mismatch(sheet_name: str) -> list[str]:
    """Nama kolom yang BENAR-BENAR terlihat saat tab `sheet_name` gagal cocok (kosong = belum ada).

    Dibutuhkan ketika gviz menjawab `header_mismatch`: itu bisa berarti "tab belum dibuat"
    (gviz jatuh ke tab pertama) atau "tab ada tapi skemanya beda" — dua penyebab dengan dua
    tindakan berbeda, dan tanpa daftar kolomnya keduanya tampak identik di Step Summary.
    """
    return list(_last_header_mismatch.get(sheet_name, []))


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
                _last_header_mismatch[sheet_name] = [str(c).strip() for c in parsed[0]][:24]
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


# --- Toleransi format waktu sheet (Ronde 7) ----------------------------------
# Dibuktikan CI #44777562090 16:51:50Z: `_is_research_fresh gagal parse timestamp
# '2026-11-15 5:40:00'`. Google Sheets menulis ulang ISO yang kita kirim ke bentuknya sendiri
# (pemisah spasi, jam tanpa nol, tanpa zona). Filter kesegaran yang gagal parse SELALU berarti
# "basi", jadi efeknya sama dengan riset yang tidak pernah ada — tanpa satu pun peringatan yang
# menjelaskan. Parser ini menerima bentuk yang benar-benar terlihat, bukan hanya ISO yang rapi.
_PARTIAL_ISO = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2})(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?$",
    re.IGNORECASE,
)
_US_DATE = re.compile(
    r"^(\d{1,2})/(\d{1,2})/(\d{4})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?$", re.IGNORECASE
)
_SERIAL = re.compile(r"^\d{5}(?:\.\d+)?$")
_EXCEL_EPOCH = datetime(1899, 12, 30, tzinfo=timezone.utc)

_gviz_scan_stats: dict[str, Any] = {}


def get_gviz_scan_stats() -> dict[str, Any]:
    """Statistik pembacaan gviz Daily_Scans terakhir (baris/segar/basi/waktu tak dikenal/terbaru)."""
    return dict(_gviz_scan_stats)


def parse_sheet_timestamp(raw: Any) -> Optional[datetime]:
    """Ubah nilai waktu dari sheet menjadi datetime aware UTC, atau None bila benar-benar tak dikenal.

    Tanpa zona dianggap UTC — kolomnya memang bernama `Timestamp (UTC)`.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    m = _PARTIAL_ISO.match(text)
    if m:
        y, mo, d, hh, mm, ss, tz = m.groups()
        base = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss or 0))
        if not tz:
            return base.replace(tzinfo=timezone.utc)
        norm = tz.replace("Z", "+00:00")
        if re.match(r"^[+-]\d{4}$", norm):
            norm = norm[:3] + ":" + norm[3:]
        return base.replace(tzinfo=datetime.fromisoformat(norm).tzinfo)

    m = _US_DATE.match(text)
    if m:
        a, b, y, hh, mm, ss, ampm = m.groups()
        a, b = int(a), int(b)
        # Ambigu: lembar berbahasa Inggris-US menulis bulan lebih dulu. Bila angka pertama > 12
        # itu mustahil bulan, jadi otomatis day-first. Selain itu ikuti asumsi locale sheet (US).
        month, day = (b, a) if a > 12 else (a, b)
        hour = int(hh) % 12
        if ampm and ampm.upper() == "PM":
            hour += 12
        try:
            return datetime(int(y), month, day, hour, int(mm), int(ss or 0), tzinfo=timezone.utc)
        except ValueError:
            return None

    if _SERIAL.match(text):
        try:
            return _EXCEL_EPOCH + timedelta(days=float(text))
        except (OverflowError, ValueError):
            return None
    return None


def _is_research_fresh(
    timestamp_str: Any,
    max_age_hours: float,
    now_utc: Optional[datetime] = None,
) -> bool:
    """Return True hanya jika timestamp riset valid dan umurnya <= max_age_hours."""
    raw = str(timestamp_str or "").strip()
    if not raw:
        return False
    dt = parse_sheet_timestamp(raw)
    if dt is None:
        log.warning(f"_is_research_fresh: format waktu tidak dikenal '{raw[:40]}' -> dianggap basi")
        return False
    now = now_utc or datetime.now(timezone.utc)
    age_hours = (now - dt).total_seconds() / 3600.0
    return 0.0 <= age_hours <= max_age_hours or age_hours < 0.0


def get_research_read_source() -> str:
    """Dari mana hasil baca riset terakhir diambil: webhook | gviz | local_csv | '' (belum ada)."""
    return _research_read_source


def _research_from_row(item: dict[str, str]) -> Any:
    """Bangun satu objek Research dari baris Daily_Scans (dipakai jalur webhook, gviz, dan CSV).

    Kolom `balance_usdc` sengaja TIDAK dibaca ke objek: repo dan log CI publik, dan tidak ada
    jalur riset yang membutuhkan saldo.
    """
    from llm_research import Research

    score = 0.0
    raw_score = str(item.get("swarm_score", "0") or "").replace("+", "")
    try:
        score = float(raw_score) if raw_score and raw_score != "—" else 0.0
    except ValueError:
        score = 0.0

    sent = str(item.get("sentiment") or "unknown").lower()
    if sent not in ("bullish", "bearish", "neutral", "unknown"):
        sent = "unknown"

    p1_to_p7: dict[str, float] = {}
    for col_k, norm_k in (
        ("p1_polarity", "P1_sentiment_polarity"),
        ("p4_momentum", "P4_momentum_direction"),
        ("p5_catalysts", "P5_risk_catalysts"),
        ("p6_contrarian", "P6_contrarian_check"),
    ):
        val = item.get(col_k)
        if val is not None and str(val) not in ("", "—"):
            try:
                p1_to_p7[norm_k] = float(str(val).replace("+", ""))
            except ValueError:
                continue

    consensus = str(item.get("consensus") or "SINGLE_MODEL")
    return Research(
        ticker=str(item.get("asset") or "").upper(),
        days_to_earnings=None,
        news_sentiment=sent,  # type: ignore[arg-type]
        narrative=str(item.get("narrative") or item.get("reason") or ""),
        confidence=0.80 if score != 0.0 else 0.50,
        fetched_at=str(item.get("timestamp_utc") or item.get("timestamp") or ""),
        consensus=consensus,
        final_score=score,
        overall_consensus_level=consensus,
        vendor_alignment=str(item.get("vendor_alignment") or "both_agree"),
        p1_to_p7_scores=p1_to_p7,
    )


# Header tab Daily_Scans yang tertulis oleh Apps Script memakai nama cantik berbahasa Indonesia
# ("Sisi", "Saldo (USDC)", "P1 (Polaritas)"), sementara konstanta kita memakai nama kolom internal.
# Kecocokan dilakukan pada TOKEN hasil normalisasi, bukan string penuh, supaya perubahan label kecil
# di sheet tidak kembali membuat jalur baca mati tanpa suara (dibuktikan CI 16:45Z: header_mismatch).
GVIS_SCAN_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp_utc": ("timestamp", "waktu"),
    "balance_usdc": ("saldo", "balance"),
    "open_positions": ("posisi terbuka", "open positions"),
    "asset": ("asset", "simbol", "ticker"),
    "side": ("sisi", "side"),
    "funding_daily_pct": ("funding",),
    "price": ("price", "harga"),
    "swarm_score": ("swarm",),
    "sentiment": ("sentimen", "sentiment"),
    "consensus": ("konsensus", "consensus"),
    "vendor_alignment": ("vendor",),
    "p1_polarity": ("p1",),
    "p4_momentum": ("p4",),
    "p5_catalysts": ("p5",),
    "p6_contrarian": ("p6",),
    "verdict": ("verdict", "keputusan"),
    "margin_allocated": ("margin",),
    "reason": ("reason", "alasan"),
    "narrative": ("narrative", "narasi"),
}

# Kolom yang TIDAK pernah diteruskan ke objek riset (repo & log CI publik).
GVIS_SCAN_WITHHELD = frozenset({"balance_usdc", "open_positions"})

# Keberadaan ketiga kunci ini yang menandai "ini benar-benar tab Daily_Scans" — tab Closed_Positions
# juga memuat "Asset", jadi satu kolom saja tidak cukup sebagai penanda.
GVIS_SCAN_REQUIRED = ("timestamp_utc", "asset", "swarm_score")


def _normalize_header(value: str) -> str:
    """'Saldo (USDC)' -> 'saldo'; 'P1 (Polaritas)' -> 'p1'; 'Funding/Hari ()' -> 'funding hari'."""
    text = re.sub(r"\([^)]*\)", " ", str(value or "")).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return text


def map_gviz_scan_header(header: list[str]) -> dict[str, int]:
    """Petakan nama kolom internal -> indeks pada baris header sheet (cocok presisi atau token)."""
    dinorm = [_normalize_header(h) for h in header]
    out: dict[str, int] = {}
    for canon, aliases in GVIS_SCAN_ALIASES.items():
        for i, name in enumerate(dinorm):
            if not name or i in out.values():
                continue
            if name == canon or any(
                alias == name or name.startswith(alias + " ") or (" " + alias + " ") in f" {name} "
                for alias in aliases
            ):
                out[canon] = i
                break
    return out


def fetch_latest_research_from_gviz(
    max_age_hours: float = 6.0,
    spreadsheet_id: Optional[str] = None,
    now_utc: Optional[datetime] = None,
) -> tuple[dict[str, Any], str]:
    """Baca riset terbaru per aset LANGSUNG dari tab `Daily_Scans` lewat gviz.

    Ronde 7: jalur webhook Apps Script terbukti membalas teks sambutan ("... is active!") karena
    deployment yang aktif bukan isi repo. Tab itu sendiri sudah tahan-lama dan terbaca (dipakai
    ledger), jadi gviz menjadi sumber kedua yang tidak bergantung pada deployment pihak ketiga —
    sesuai invarian "state yang harus bertahan lintas eviksi wajib punya sumber tahan-lama".

    Header sheet dibandingkan lewat `map_gviz_scan_header` (token ternormalisasi), bukan string
    persis: tab ini ditulis Apps Script dengan label cantik ("Sisi", "Swarm Score"), dan pencocokan
    kaku membuat jalur baca mati tanpa suara meski datanya ada.
    """
    rows, status = fetch_sheet_csv_rows_ex("Daily_Scans", spreadsheet_id=spreadsheet_id)
    if status != "ok" or len(rows) < 2:
        return {}, status if status != "ok" else "empty"

    header = [str(c) for c in rows[0]]
    idx = map_gviz_scan_header(header)
    hilang = [k for k in GVIS_SCAN_REQUIRED if k not in idx]
    if hilang:
        # Guard anti-fallback tetap utuh: tab yang bukan Daily_Scans (mis. Closed_Positions, yang
        # juga punya kolom "Asset") tetap ditolak, hanya saja penolakannya kini berdasarkan
        # penanda tiga kolom, bukan satu nama string.
        _last_header_mismatch["Daily_Scans"] = [c.strip() for c in header if c.strip()][:24]
        _warn_ledger_unreadable(
            "Daily_Scans", "header_mismatch",
            f"kolom penanda {hilang} tidak dikenali dari header {header[:6]}",
        )
        return {}, "header_mismatch"

    researches: dict[str, Any] = {}
    stats: dict[str, Any] = {
        "baris": len(rows) - 1, "segar": 0, "basi": 0,
        "waktu_tidak_dikenali": 0, "tanpa_asset": 0, "terbaru": None,
    }
    terbaru: Optional[datetime] = None
    for row in rows[1:]:
        item = {
            name: (row[i] if i < len(row) else "")
            for name, i in idx.items()
            if name not in GVIS_SCAN_WITHHELD
        }
        dt = parse_sheet_timestamp(item.get("timestamp_utc"))
        if dt is not None and (terbaru is None or dt > terbaru):
            terbaru = dt
        asset = str(item.get("asset") or "").strip().upper()
        if not asset:
            stats["tanpa_asset"] += 1
            continue
        # Keputusan kesegaran tetap SATU sumber (_is_research_fresh); parse di atas hanya untuk
        # membedakan "basi" dari "format waktu tidak dikenal" — dua diagnosis yang berbeda.
        if not _is_research_fresh(item.get("timestamp_utc"), max_age_hours, now_utc=now_utc):
            stats["basi" if dt is not None else "waktu_tidak_dikenali"] += 1
            continue
        stats["segar"] += 1
        # baris berikutnya menimpa: urutan tab = kronologis, jadi yang tersisa = paling baru
        researches[asset] = _research_from_row(item)

    stats["terbaru"] = terbaru.isoformat() if terbaru else None
    _gviz_scan_stats.clear()
    _gviz_scan_stats.update(stats)
    return researches, "ok" if researches else "empty"


def close_identity_keys(asset: Any, side: Any, entry: Any) -> list[str]:
    """Kunci identitas cadangan untuk baris `Closed_Positions` tanpa Position ID.

    Mengembalikan [aset|sisi|harga, aset|harga] — varian TANPA sisi dibutuhkan karena 11 baris
    warisan (tersensus 2026-11-20 17:58Z) tidak punya Position ID, tidak punya `Side`, dan tidak
    punya `Opened At (exchange)`; kunci berbasis waktu/sisi tidak akan pernah cocok untuk mereka.

    Arah galat dipilih sengaja: kalau ragu, anggap SUDAH tercatat. Menulis baris ledger ganda
    merusak pengukuran performansi secara senyap, sedangkan melewatkan satu baris hanya membuat
    angka tetap tidak lengkap — dan ketidaklengkapan itu dilaporkan, bukan disembunyikan.
    """
    a = str(asset or "").strip().upper()
    s = str(side or "").strip().upper()
    try:
        e = round(float(str(entry).replace(",", "")), 4)
    except (TypeError, ValueError):
        return []
    if not a:
        return []
    keys = [f"{a}|{e:.4f}"]
    if s:
        keys.insert(0, f"{a}|{s}|{e:.4f}")
    return keys


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
    Mengabaikan riset yang lebih tua dari `max_research_age_hours` (sumber default:
    strategy_config.DEFAULT_RESEARCH_AGE_HOURS, terikat kadens daily-scan).
    """
    from llm_research import Research
    from strategy_config import DEFAULT_RESEARCH_AGE_HOURS
    _set_research_source("")
    if max_age_hours is None:
        try:
            from strategy_config import load_strategy_config
            max_age_hours = float(load_strategy_config().get("max_research_age_hours", DEFAULT_RESEARCH_AGE_HOURS))
        except Exception as e:
            log.warning(f"fetch_latest_research_from_sheet gagal baca max_research_age_hours ({type(e).__name__}): {e}")
            max_age_hours = DEFAULT_RESEARCH_AGE_HOURS

    url = _normalize_webhook_url(webhook_url or os.environ.get("GOOGLE_SHEET_WEBHOOK_URL", DEFAULT_WEBHOOK_URL))
    if not url:
        _record_ledger_status(RESEARCH_READ_SHEET, "no_config", "GOOGLE_SHEET_WEBHOOK_URL tidak diset")
    if url:
        try:
            r = httpx.get(url, follow_redirects=True, timeout=12.0)
            if r.status_code != 200:
                _record_ledger_status(RESEARCH_READ_SHEET, "http_error", f"HTTP {r.status_code}")
                _warn_ledger_unreadable(RESEARCH_READ_SHEET, "http_error", f"webhook riset HTTP {r.status_code}")
            if r.status_code == 200:
                res = _parse_research_response(r)
                if isinstance(res, dict) and res.get("status") == "success":
                    data = res.get("data", {})
                    researches: dict[str, Research] = {}
                    stale_skipped = 0
                    for asset, item in data.items():
                        item = _normalize_research_item(item)
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
                        _set_research_source("webhook")
                        _record_ledger_status(RESEARCH_READ_SHEET, "ok", f"{len(researches)} aset segar dari webhook")
                        log.info(f"✓ Berhasil memuat {len(researches)} data riset segar dari Google Sheets Webhook ({list(researches.keys())})")
                        return researches
                    _record_ledger_status(
                        RESEARCH_READ_SHEET,
                        "empty",
                        f"webhook sukses tapi 0 riset segar (basi>{max_age_hours:.1f}j: {stale_skipped})",
                    )
        except Exception as e:
            _record_ledger_status(
                RESEARCH_READ_SHEET, "exception",
                _redact_sheet_id(f"{type(e).__name__}: {e}", get_spreadsheet_id())[:120],
            )
            log.warning(f"Gagal mengambil riset dari Google Sheets Webhook: {e}")

    # Ronde 7: SUMBER KEDUA = tab Daily_Scans lewat gviz. Jalur ini tahan-lama (sheet adalah
    # sumber kebenarannya) dan tidak bergantung pada deployment Apps Script — yang terbukti
    # membalas teks sambutan "Walt Perps Sheet Logger Webhook is active!" karena versi aktif di
    # Google bukan isi google_apps_script.js di repo ini.
    gviz_researches, gviz_status = fetch_latest_research_from_gviz(max_age_hours=max_age_hours)
    if gviz_researches:
        _set_research_source("gviz")
        _record_ledger_status(
            RESEARCH_READ_SHEET, "ok", f"{len(gviz_researches)} aset segar dari gviz Daily_Scans"
        )
        log.info(
            f"✓ {len(gviz_researches)} data riset segar dibaca dari gviz Daily_Scans "
            f"({sorted(gviz_researches)[:6]}) — jalur webhook tidak terpakai."
        )
        return gviz_researches
    if gviz_status not in ("ok", "empty", "no_config"):
        log.warning(f"gviz Daily_Scans tidak menghasilkan riset (status={gviz_status}) — lanjut ke CSV lokal.")

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
                    prev_status = get_research_read_status()  # status jalur webhook, sebelum ditimpa
                    _set_research_source("local_csv")
                    _record_ledger_status(RESEARCH_READ_SHEET, "local_csv", f"{len(researches)} aset dari CSV lokal")
                    log.info(
                        f"✓ Berhasil memuat {len(researches)} data riset segar dari file CSV lokal "
                        f"({list(researches.keys())}) | webhook riset TIDAK terpakai "
                        f"(status_webhook={prev_status})"
                    )
                    return researches
        except Exception as e:
            log.warning(f"Gagal membaca riset dari CSV lokal: {e}")

    return {}

