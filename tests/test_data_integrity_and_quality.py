import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import llm_research
import sheet_logger
import walt_bot


def test_d2_dedupe_close_and_unknown_not_counted_as_tp(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_WEBHOOK_URL", "")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")

    snap_path = tmp_path / "open_snapshot.json"
    closed_ids_path = tmp_path / "closed_ids.json"
    closed_csv = tmp_path / "closed_history.csv"
    regime_path = tmp_path / "market_regime.json"

    monkeypatch.setattr(walt_bot, "_set_asset_cooldown", lambda *a, **kw: None)

    stale_snapshot = {
        "pos_nbis_1": {
            "position_id": "pos_nbis_1",
            "contract_id": 189,
            "base_asset": "NBIS",
            "side": "short",
            "leverage": 2,
            "margin": 2.5,
            "entry_price": 108.3,
            "funding_paid": 0.0003,
            "tp": 105.59,
            "sl": 109.92,
            "opened_at": "2026-11-19T00:00:00+00:00",
            "verdict": "GO",
        }
    }
    snap_path.write_text(json.dumps(stale_snapshot), encoding="utf-8")

    # Exchange tidak mengembalikan data close -> harus tercatat UNKNOWN (exit & PnL kosong), BUKAN TP
    client = MagicMock()
    client.list_positions.return_value = {"positions": []}

    _, events1 = walt_bot._detect_and_log_exchange_closes(
        client,
        active_positions=[],
        snapshot_path=str(snap_path),
        closed_ids_path=str(closed_ids_path),
        closed_csv_path=str(closed_csv),
    )
    assert len(events1) == 1
    assert "UNKNOWN" in events1[0]["detail"]
    assert walt_bot._has_recent_tp_close(events1) is False

    # Replay snapshot basi yang sama persis -> dedupe via closed_ids.json, TIDAK boleh log ganda
    snap_path.write_text(json.dumps(stale_snapshot), encoding="utf-8")
    _, events2 = walt_bot._detect_and_log_exchange_closes(
        client,
        active_positions=[],
        snapshot_path=str(snap_path),
        closed_ids_path=str(closed_ids_path),
        closed_csv_path=str(closed_csv),
    )
    assert events2 == []

    # Verifikasi isi CSV: hanya 1 baris data, exit & PnL kosong, bukan entry=exit / PnL=0
    rows = list(csv.reader(closed_csv.open("r", encoding="utf-8")))
    assert len(rows) == 2  # 1 header + 1 data row
    assert rows[1][5] == ""  # Exit Price kosong
    assert rows[1][7] == ""  # PnL kosong
    assert "UNKNOWN" in rows[1][9]
    assert rows[1][10] == "pos_nbis_1"


def test_d3_webhook_payload_sends_header_separately_not_inside_rows(tmp_path, monkeypatch):
    captured = []

    class FakeResp:
        status_code = 200

    def fake_post(url, json=None, **kwargs):
        captured.append(json)
        return FakeResp()

    monkeypatch.setenv("GOOGLE_SHEET_WEBHOOK_URL", "https://script.google.com/macros/s/TEST/exec")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    monkeypatch.setattr(sheet_logger.httpx, "post", fake_post)

    sheet_logger.log_close(
        asset="NVDA", side="short", contract_id=10, entry_price=140.0, exit_price=136.5,
        margin=2.5, pnl_quote=0.12, funding_quote=0.01, reason="Exchange Auto-Close TP",
        position_id="pos_nvda_1", closed_at="2026-11-19T12:00:00+00:00",
        filepath=str(tmp_path / "closed.csv"),
    )
    sheet_logger.log_pnl_tracker(
        {"ts": "2026-11-19T12:00:00+00:00", "total_equity": 15.5, "funding_earned": 0.05},
        filepath=str(tmp_path / "pnl.csv"),
    )
    sheet_logger.log_execution(
        [{"asset": "BTC", "side": "long", "margin": 2.5, "price": 90000.0, "verdict": "EXECUTED", "reason": "ok"}],
        filepath=str(tmp_path / "exec.csv"),
    )
    sheet_logger.log_scan_via_webhook([["2026-11-19T00:00:00Z", "15.0"]], "https://script.google.com/macros/s/TEST/exec")

    assert len(captured) == 4
    for payload in captured:
        assert "header" in payload and isinstance(payload["header"], list) and len(payload["header"]) > 0
        # Header TIDAK pernah dimasukkan ke dalam payload["rows"]
        for row in payload["rows"]:
            assert row != payload["header"]


def test_d4_and_e5_honest_stats_excludes_unknown_dedupes_and_warns_short_hold(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    closed_csv = tmp_path / "closed_history.csv"
    pnl_csv = tmp_path / "pnl_tracker.csv"

    with closed_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(sheet_logger.CLOSED_POSITIONS_HEADERS)
        # 1 win riil (hold 1 jam -> jauh di bawah 24 jam)
        w.writerow([
            "2026-11-19T02:00:00+00:00", "NVDA", "SHORT", 10, 140.0, 137.0, 2.5, 0.10, 0.002,
            "Exchange Auto-Close TP", "pos_1", "2026-11-19T02:00:00+00:00", "abcd1234", "2026-11-19T01:00:00+00:00"
        ])
        # Duplikat pos_1 -> harus di-dedupe
        w.writerow([
            "2026-11-19T02:05:00+00:00", "NVDA", "SHORT", 10, 140.0, 137.0, 2.5, 0.10, 0.002,
            "Exchange Auto-Close TP", "pos_1", "2026-11-19T02:00:00+00:00", "abcd1234", "2026-11-19T01:00:00+00:00"
        ])
        # Header ganda di tengah -> harus dilewati
        w.writerow(sheet_logger.CLOSED_POSITIONS_HEADERS)
        # 1 UNKNOWN -> dikecualikan dari win-rate
        w.writerow([
            "2026-11-19T03:00:00+00:00", "NBIS", "SHORT", 189, 108.3, "", 2.5, "", 0.0,
            "UNKNOWN (data close tidak tersedia)", "pos_unk", "", "abcd1234", ""
        ])

    with pnl_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(sheet_logger.PNL_TRACKER_HEADERS)
        w.writerow(["2026-11-18", 15.66, 0.005, 0.01, 100.0, "JAUH"])
        w.writerow(["2026-11-19", 15.40, 0.008, 0.01, 100.0, "JAUH"])

    stats = walt_bot.compute_closed_stats(csv_path=str(closed_csv), pnl_tracker_path=str(pnl_csv), send_tg=False)
    assert stats["total_trades"] == 1
    assert stats["unknown_count"] == 1
    assert stats["win_rate_pct"] == 100.0
    assert stats["equity_change_usd"] == pytest.approx(-0.26)
    assert stats["median_hold_hours"] == pytest.approx(1.0)
    assert stats["hold_time_warning"] is not None


def test_k1_gemini_uses_header_not_url_query_key_and_global_secret_redaction(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return FakeResp()

    client = llm_research.GeminiClient(api_key="AIzaSyFakeSecretKey123456789012345")
    monkeypatch.setattr(client.client, "post", fake_post)
    res = client.chat_json("test prompt")

    assert res == {"ok": True}
    assert "?key=" not in captured["url"]
    assert "AIzaSyFakeSecretKey" not in captured["url"]
    assert captured["headers"].get("x-goog-api-key") == "AIzaSyFakeSecretKey123456789012345"

    # Global secret filter menyensor key=, Bearer, AIza
    noisy = "GET https://api.example.com?key=SECRET123 Authorization: Bearer eyJhbGciOiJIUzI1NiJ9 AIzaSyFakeSecretKey123456789012345"
    clean = walt_bot.redact_secrets(noisy)
    assert "SECRET123" not in clean
    assert "eyJhbGciOiJIUzI1NiJ9" not in clean
    assert "AIzaSyFakeSecretKey123456789012345" not in clean


def test_e1_half_below_floor_returns_zero_skip():
    opp = SimpleNamespace(funding_daily=0.005, base_asset="NBIS")
    res = SimpleNamespace(final_score=0.0, overall_consensus_level="MIXED")
    # Dengan saldo $15.40, HALF (mult 0.5) menghasilkan < $2.50 -> harus return 0.0 (SKIP)
    margin = walt_bot.calculate_dynamic_margin(
        available_balance=10.0, active_slots=1, max_slots=5,
        opp=opp, research=res, verdict_mult=0.5, total_equity=15.40,
    )
    assert margin == 0.0


def test_e2_high_p5_catalyst_returns_skip():
    opp = SimpleNamespace(base_asset="NVDA", side="short", funding_daily=0.006)
    res = llm_research.Research(
        ticker="NVDA", days_to_earnings=20, news_sentiment="bearish",
        narrative="high risk", confidence=0.8, fetched_at="2026-11-19T00:00:00Z",
        consensus="UNANIMOUS", final_score=-0.5, overall_consensus_level="UNANIMOUS",
        vendor_alignment="both_agree", p1_to_p7_scores={"P5_risk_catalysts": 0.75},
    )
    verdict, reason, mult = walt_bot.decide_go_nogo(opp, res)
    assert verdict == "SKIP"
    assert mult == 0.0
    assert "P5" in reason


def test_e3_stale_research_ignored_and_stock_skips(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_WEBHOOK_URL", "")
    csv_path = tmp_path / "scan_history.csv"
    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
    stale_ts = (now - timedelta(hours=10)).isoformat()
    fresh_ts = (now - timedelta(hours=2)).isoformat()

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(sheet_logger.DAILY_SCANS_HEADERS)
        w.writerow([stale_ts, "15", "0", "NBIS", "SHORT", "+0.6%", "100", "+0.03", "neutral", "STRONG", "both_agree", "0", "0", "0.2", "0.2", "HALF", "2.5", "ok", "old"])
        w.writerow([fresh_ts, "15", "0", "NVDA", "SHORT", "+0.6%", "140", "-0.50", "bearish", "UNANIMOUS", "both_agree", "-0.5", "-0.5", "0.2", "0.2", "GO", "2.5", "ok", "fresh"])

    loaded = sheet_logger.fetch_latest_research_from_sheet(csv_path=str(csv_path), max_age_hours=6.0, now_utc=now)
    assert "NBIS" not in loaded  # 10 jam > 6 jam -> basi
    assert "NVDA" in loaded      # 2 jam <= 6 jam -> segar

    # Saham tanpa riset segar -> SKIP "riset basi"
    opp = SimpleNamespace(base_asset="NBIS", side="short", funding_daily=0.006)
    verdict, reason, mult = walt_bot.decide_go_nogo(opp, None)
    assert verdict == "SKIP"
    assert mult == 0.0
    assert "riset basi" in reason.lower()


def test_e4_frequency_limits_and_daily_circuit_breaker(tmp_path):
    regime = str(tmp_path / "market_regime.json")
    t0 = 1_700_000_000.0

    # Batas per aset = 2 dalam 24 jam
    assert walt_bot.check_entry_frequency_limits("NBIS", now_ts=t0, regime_path=regime)[0] is True
    walt_bot.record_entry_execution("NBIS", now_ts=t0, regime_path=regime)
    walt_bot.record_entry_execution("NBIS", now_ts=t0 + 60, regime_path=regime)
    ok_asset, reason_asset = walt_bot.check_entry_frequency_limits("NBIS", now_ts=t0 + 120, regime_path=regime)
    assert ok_asset is False
    assert "NBIS" in reason_asset

    # Batas total harian = 8 dalam 24 jam
    for sym in ("NVDA", "NVDA", "SPY", "SPY", "XAU", "XAU"):
        walt_bot.record_entry_execution(sym, now_ts=t0 + 180, regime_path=regime)
    ok_day, reason_day = walt_bot.check_entry_frequency_limits("BTC", now_ts=t0 + 240, regime_path=regime)
    assert ok_day is False
    assert "8/8" in reason_day

    # Circuit breaker: ekuitas turun >= 5% dari awal hari UTC -> tripped
    day1 = datetime(2026, 11, 19, 1, 0, tzinfo=timezone.utc)
    tripped0, _ = walt_bot.check_daily_circuit_breaker(15.50, now_utc=day1, regime_path=regime, send_alert=False)
    assert tripped0 is False
    tripped1, cb_msg = walt_bot.check_daily_circuit_breaker(14.70, now_utc=day1 + timedelta(hours=3), regime_path=regime, send_alert=False)
    assert tripped1 is True
    assert "circuit breaker" in cb_msg.lower()

    # Reset otomatis di hari UTC berikutnya (00:00 UTC)
    day2 = datetime(2026, 11, 20, 0, 5, tzinfo=timezone.utc)
    tripped2, _ = walt_bot.check_daily_circuit_breaker(14.70, now_utc=day2, regime_path=regime, send_alert=False)
    assert tripped2 is False


def test_e5_funding_vs_fee_economic_gate():
    # Fee round-trip = 0.18%, slippage = 0.05% -> cost = 0.23%, butuh 1.5x = 0.345%
    ok_low, _, _, _ = walt_bot.check_funding_vs_fee_gate(funding_daily=0.002, price_slippage=0.0005, expected_hold_hours=24.0)
    assert ok_low is False

    ok_high, exp_fr, cost, note = walt_bot.check_funding_vs_fee_gate(funding_daily=0.006, price_slippage=0.0005, expected_hold_hours=24.0)
    assert ok_high is True
    assert exp_fr >= cost * 1.5
    assert "hold=24h" in note
