"""Ronde 6 pasca-verifikasi: sheet akhirnya terbaca (akses Viewer diberikan pemilik).

Berkas ini mengurus dua hal yang muncul begitu jalur baca hidup:
  1. sensus kualitas baris Closed_Positions (read-only, TANPA kredensial Walt) — tools/sheet_probe.py
  2. klasifikasi jujur "PnL 0.00 tanpa sumber harga" (Exit=Entry warisan) di compute_closed_stats
"""
from __future__ import annotations

import csv
import importlib.util
import io
import json
from pathlib import Path

import pytest
import yaml

import sheet_logger
import walt_bot

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_probe():
    spec = importlib.util.spec_from_file_location("sheet_probe", REPO_ROOT / "tools" / "sheet_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sp = _load_probe()

HEADER = sheet_logger.CLOSED_POSITIONS_HEADERS


def _row(entry, exit_, pnl, reason, pid, src="", closed_at="2026-11-19T23:38:33Z", strategy="d11f2da8"):
    row = [""] * len(HEADER)
    row[0] = "2026-11-19T23:40:15+00:00"
    row[1] = "XAG"
    row[2] = "SHORT"
    row[3] = "93"
    row[4] = str(entry)
    row[5] = str(exit_)
    row[6] = "2.3757"
    row[7] = str(pnl)
    row[8] = "0.0005"
    row[9] = reason
    row[10] = pid
    row[11] = closed_at
    row[12] = strategy
    row[13] = "2026-11-19T22:52:45Z"
    row[14] = src
    return row


# ---------------------------------------------------------------------------
# 1) sensus kualitas baris
# ---------------------------------------------------------------------------
def test_census_membedakan_lima_kualitas_baris():
    rows = [HEADER] + [
        _row(43.15, 43.20, 0.03, "TP breached", "93:43.15:a", src="exchange_fill"),
        _row(43.15, 43.16, 0.01, "trailing lock", "93:43.15:b", src="mark_at_detection"),
        _row(43.15, 43.15, 0, "Exchange Auto-Close TP/Trailing", "93:43.15:c"),  # tanpa sumber
        _row(43.15, "", "", "UNKNOWN (data close tidak tersedia)", "93:43.15:d"),
        _row(43.15, "", "", walt_bot.BACKFILL_CLOSE_REASON, "93:43.15:e",
             src=walt_bot.EXCHANGE_HISTORY_EXIT_SOURCE),
    ]
    c = sp.census_closed(rows)
    assert (c["rows_data"], c["measured"], c["estimated"], c["disputed_zero"], c["unknown"],
            c["backfill_no_exit"]) == (5, 1, 1, 1, 1, 1)
    assert c["strategy_versions"] == ["d11f2da8"]
    assert c["last_closed_at"] == "2026-11-19T23:38:33Z"


def test_census_breakeven_riil_bukan_baris_diragukan():
    """Non-vakuum: PnL 0 dengan Exit==Entry TAPI sumbernya fill riil = trade impas sejati."""
    rows = [HEADER, _row(43.15, 43.15, 0, "SL breached", "93:43.15:x", src="exchange_fill")]
    c = sp.census_closed(rows)
    assert c["disputed_zero"] == 0 and c["measured"] == 1


def test_census_menghitung_lebar_baris_yang_campur():
    legacy = ["2026-11-18", "CRCL", "SHORT", "121", "56.976", "56.976", "2.4576", "0", "0.0003",
              "Exchange Auto-Close TP/Trailing"]
    rows = [HEADER[:10], legacy + ["", "", "", ""]]
    c = sp.census_closed(rows)
    assert c["disputed_zero"] == 1
    assert 14 in c["row_widths"]


# ---------------------------------------------------------------------------
# 2) jalur baca gviz: status jujur + nol HTTP saat tanpa konfigurasi
# ---------------------------------------------------------------------------
def test_fetch_gviz_no_config_nol_percobaan_http(monkeypatch):
    calls = {"n": 0}

    def forbidden(*a, **k):
        calls["n"] += 1
        raise AssertionError("httpx.get dipanggil padahal GOOGLE_SHEET_ID kosong")

    monkeypatch.setattr(sp.httpx, "get", forbidden)
    rows, status = sp.fetch_gviz_rows("", "Closed_Positions")
    assert rows == [] and status == "no_config"
    assert calls["n"] == 0


def test_fetch_gviz_membedakan_http_error_dan_html(monkeypatch):
    monkeypatch.setattr(sp.httpx, "get", lambda *a, **k: type("R", (), {"status_code": 404, "text": "not found"})())
    assert sp.fetch_gviz_rows("abc", "Closed_Positions")[1] == "http_error"

    monkeypatch.setattr(sp.httpx, "get", lambda *a, **k: type("R", (), {"status_code": 200, "text": "<html>login</html>"})())
    assert sp.fetch_gviz_rows("abc", "Closed_Positions")[1] == "html_or_unparsable"

    csv_text = ",".join(HEADER[:10]) + "\n"
    monkeypatch.setattr(sp.httpx, "get", lambda *a, **k: type("R", (), {"status_code": 200, "text": csv_text})())
    rows, status = sp.fetch_gviz_rows("abc", "Closed_Positions")
    assert status == "ok" and len(rows) == 1


def test_probe_keluaran_tersanitasi_tanpa_id_penuh(monkeypatch):
    sid = "1FAkEsheEtIdForTestsOnly_000000000000000000"
    body = ",".join(HEADER[:10]) + "\n2026-11-18,CRCL,SHORT,121,56.976,56.976,2.4576,0,0.0003,Auto\n"
    monkeypatch.setattr(sp.httpx, "get", lambda *a, **k: type("R", (), {"status_code": 200, "text": body})())
    out = json.dumps(sp.probe(sid), ensure_ascii=False)
    assert sid not in out
    assert '"sha8"' in out and '"census"' in out
    assert sp.sheet_fingerprint(sid)["sha8"] == "f8daaee3"


def test_main_prove_exit_code_jujur(monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    assert sp.main([]) == 2
    assert "TIDAK DIKONFIGURASI" in capsys.readouterr().out

    monkeypatch.setattr(sp.httpx, "get", lambda *a, **k: type("R", (), {"status_code": 200, "text": "a,b\n1,2\n"})())
    monkeypatch.setenv("GOOGLE_SHEET_ID", "1FAkEsheEtIdForTestsOnly_000000000000000000")
    assert sp.main([]) == 0


# ---------------------------------------------------------------------------
# 3) compute_closed_stats tidak lagi menghitung nol-tanpa-sumber sebagai kalah
# ---------------------------------------------------------------------------
def _write_closed(tmp_path, rows):
    p = tmp_path / "closed.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for r in rows:
            w.writerow(r)
    return p


def test_stats_memisahkan_baris_diragukan(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(walt_bot, "send_telegram", lambda *a, **k: False)
    csv_path = _write_closed(tmp_path, [
        _row(43.15, 43.20, 0.03, "TP breached", "93:43.15:a", src="exchange_fill"),
        _row(43.15, 43.15, 0, "Exchange Auto-Close TP/Trailing", "93:43.15:c"),
        _row(43.15, "", "", "UNKNOWN (data close tidak tersedia)", "93:43.15:d"),
    ])

    stats = walt_bot.compute_closed_stats(
        csv_path=str(csv_path), pnl_tracker_path=str(tmp_path / "none.csv"), send_tg=False
    )
    out = capsys.readouterr().out

    assert stats["total_trades"] == 1 and stats["losses"] == 0
    assert stats["unknown_count"] == 1
    assert stats["pnl_diragukan"] == 1
    assert stats["diragukan_alasan"] == {"Exchange Auto-Close TP/Trailing": 1}
    assert "diragukan (PnL=0 tanpa sumber harga): `1`" in out
    assert "⚠️ 1 baris ledger ber-PnL 0.00" in out


def test_stats_tetap_menghitung_impas_dari_fill(tmp_path, monkeypatch, capsys):
    """Non-vakuum untuk test di atas: impas dengan sumber fill riil tetap dihitung 1 trade."""
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(walt_bot, "send_telegram", lambda *a, **k: False)
    csv_path = _write_closed(tmp_path, [_row(43.15, 43.15, 0, "SL breached", "93:43.15:z", src="exchange_fill")])

    stats = walt_bot.compute_closed_stats(
        csv_path=str(csv_path), pnl_tracker_path=str(tmp_path / "none.csv"), send_tg=False
    )
    assert stats["total_trades"] == 1 and stats["pnl_diragukan"] == 0
    assert "⚠️" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 4) wiring CI: diagnostics menjalankan probe SEBELUM langkah yang butuh token Walt
# ---------------------------------------------------------------------------
def test_diagnostics_menjalankan_sheet_probe_sebelum_langkah_walt():
    raw = (REPO_ROOT / ".github/workflows/diagnostics.yml").read_text(encoding="utf-8")
    wf = yaml.safe_load(raw)
    steps = next(iter(wf["jobs"].values()))["steps"]
    probe_idx = next((i for i, s in enumerate(steps) if "tools/sheet_probe.py" in (s.get("run") or "")), None)
    walt_idx = next((i for i, s in enumerate(steps) if "dump_closed" in (s.get("run") or "")), None)
    assert probe_idx is not None, "sheet probe belum dipasang di diagnostics.yml"
    assert walt_idx is not None
    assert probe_idx < walt_idx, "probe harus jalan walau kredensial Walt mati"

    env = steps[probe_idx].get("env") or {}
    assert "GOOGLE_SHEET_ID" in env
    assert not any(k.startswith("WALT_") for k in env), "langkah probe tidak boleh butuh kredensial Walt"
    assert wf["permissions"] == {"contents": "read"}
    src = (REPO_ROOT / "tools" / "sheet_probe.py").read_text(encoding="utf-8")
    assert "WaltClient" not in src and "walletbot.me" not in src
