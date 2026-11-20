import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import sheet_logger
import walt_bot


def test_r5_1_isolate_writes_captures_closes_without_touching_prod_ledger(
    isolate_writes, captured_closes, monkeypatch
):
    monkeypatch.setenv("GOOGLE_SHEET_WEBHOOK_URL", "")
    prod_csv = Path(__file__).resolve().parent.parent / "logs" / "closed_history.csv"
    before_bytes = prod_csv.read_bytes() if prod_csv.exists() else None

    pos = walt_bot.Position(
        contract_id=99,
        base_asset="NVDA",
        side="short",
        leverage=2,
        margin=2.5,
        entry_price=100.0,
        current_price=101.8,
        tp=97.5,
        sl=101.5,
        funding_paid=0.05,
        liquidation_price=150.0,
        position_id="pos_iso_test",
        opened_at="2026-11-20T00:00:00+00:00",
    )
    walt_bot._record_position_close(pos, "SL 1.50% breached (migrasi)", exit_price=101.8)

    assert len(captured_closes) == 1
    assert captured_closes[0]["asset"] == "NVDA"
    assert Path(captured_closes[0]["filepath"]).parent == isolate_writes
    after_bytes = prod_csv.read_bytes() if prod_csv.exists() else None
    assert after_bytes == before_bytes


def _real_17_key_closed_pos(
    asset="NVDA",
    contract_id=10,
    side="long",
    avg_entry_price="100.0",
    value_base="0.10",
    margin_quote="2.5",
    leverage=2,
    updated_at=None,
    created_at=None,
):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    upd = updated_at or (now - timedelta(minutes=5)).isoformat()
    crt = created_at or (now - timedelta(hours=2)).isoformat()
    return {
        "auto_close": None,
        "avg_entry_price": avg_entry_price,
        "contract_base_asset": asset,
        "contract_id": contract_id,
        "contract_name": f"{asset}-USD-PERP",
        "contract_quote_asset": "USD",
        "created_at": crt,
        "funding_paid_quote": "0.01",
        "leverage": leverage,
        "liquidation_price": "50.0",
        "margin_quote": margin_quote,
        "margin_type": "isolated",
        "side": side,
        "status": "closed",
        "synced_at": upd,
        "updated_at": upd,
        "value_base": value_base,
    }


def test_r5_2_exchange_close_uses_17_key_payload_and_live_mark_at_detection(
    tmp_path, captured_closes
):
    import json
    from unittest.mock import MagicMock

    cp = _real_17_key_closed_pos(
        asset="NVDA", contract_id=10, side="long",
        avg_entry_price="100.0", value_base="0.05", margin_quote="2.5", leverage=2,
    )
    assert len(cp) == 17  # persis 17 kunci diagnostics.yml tanpa field harga keluar

    dedupe_key = walt_bot.make_position_dedupe_key(cp)
    snap_path = tmp_path / "open_snapshot.json"
    snap_path.write_text(json.dumps({
        dedupe_key: {
            "position_id": dedupe_key,
            "contract_id": 10,
            "base_asset": "NVDA",
            "side": "long",
            "leverage": 2,
            "margin": 2.5,
            "entry_price": 100.0,
            "funding_paid": 0.01,
            "tp": 102.5,
            "sl": 98.5,
            "opened_at": cp["created_at"],
            "verdict": "GO",
        }
    }), encoding="utf-8")

    client = MagicMock()
    client.list_positions.return_value = {"positions": [cp]}
    client.latest_rate.return_value = {"rate": "102.6"}

    _, events = walt_bot._detect_and_log_exchange_closes(
        client,
        active_positions=[],
        snapshot_path=str(snap_path),
        closed_ids_path=str(tmp_path / "closed_ids.json"),
        closed_csv_path=str(tmp_path / "closed_history.csv"),
    )
    assert len(events) == 1
    assert len(captured_closes) == 1
    c = captured_closes[0]
    assert c["exit_price"] == pytest.approx(102.6, abs=1e-4)
    assert c["pnl_quote"] == pytest.approx((102.6 - 100.0) * 0.05, abs=1e-6)
    assert c["exit_source"] == "mark_at_detection"
    assert "mark@deteksi" in c["reason"]


def test_r5_2_exchange_close_latest_rate_failure_keeps_none_and_unknown(
    tmp_path, captured_closes
):
    import json
    from unittest.mock import MagicMock

    cp = _real_17_key_closed_pos(asset="NVDA", contract_id=10, side="long")
    dedupe_key = walt_bot.make_position_dedupe_key(cp)
    snap_path = tmp_path / "open_snapshot.json"
    snap_path.write_text(json.dumps({
        dedupe_key: {
            "position_id": dedupe_key,
            "contract_id": 10,
            "base_asset": "NVDA",
            "side": "long",
            "leverage": 2,
            "margin": 2.5,
            "entry_price": 100.0,
            "funding_paid": 0.01,
            "tp": 102.5,
            "sl": 98.5,
            "opened_at": cp["created_at"],
            "verdict": "GO",
        }
    }), encoding="utf-8")

    client = MagicMock()
    client.list_positions.return_value = {"positions": [cp]}
    client.latest_rate.side_effect = RuntimeError("rate endpoint down")

    _, events = walt_bot._detect_and_log_exchange_closes(
        client,
        active_positions=[],
        snapshot_path=str(snap_path),
        closed_ids_path=str(tmp_path / "closed_ids.json"),
        closed_csv_path=str(tmp_path / "closed_history.csv"),
    )
    assert len(events) == 1
    assert len(captured_closes) == 1
    c = captured_closes[0]
    assert c["exit_price"] is None
    assert c["pnl_quote"] is None
    assert "UNKNOWN" in c["reason"]


def test_r5_2_exchange_close_stale_updated_at_rejects_mark_proxy(
    tmp_path, captured_closes
):
    import json
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock

    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    cp = _real_17_key_closed_pos(asset="NVDA", contract_id=10, side="long", updated_at=stale_ts)
    dedupe_key = walt_bot.make_position_dedupe_key(cp)
    snap_path = tmp_path / "open_snapshot.json"
    snap_path.write_text(json.dumps({
        dedupe_key: {
            "position_id": dedupe_key,
            "contract_id": 10,
            "base_asset": "NVDA",
            "side": "long",
            "leverage": 2,
            "margin": 2.5,
            "entry_price": 100.0,
            "funding_paid": 0.01,
            "tp": 102.5,
            "sl": 98.5,
            "opened_at": cp["created_at"],
            "verdict": "GO",
        }
    }), encoding="utf-8")

    client = MagicMock()
    client.list_positions.return_value = {"positions": [cp]}
    client.latest_rate.return_value = {"rate": "102.6"}

    walt_bot._detect_and_log_exchange_closes(
        client,
        active_positions=[],
        snapshot_path=str(snap_path),
        closed_ids_path=str(tmp_path / "closed_ids.json"),
        closed_csv_path=str(tmp_path / "closed_history.csv"),
    )
    assert len(captured_closes) == 1
    c = captured_closes[0]
    assert c["exit_price"] is None
    assert c["pnl_quote"] is None
    assert "UNKNOWN" in c["reason"]
    assert client.latest_rate.call_count == 0


def test_r5_2_exchange_close_pnl_uses_value_base_not_margin_leverage_move(
    tmp_path, captured_closes
):
    import json
    from unittest.mock import MagicMock

    # value_base=0.50 sengaja jauh berbeda dari (margin*leverage)/entry = 5.0/100 = 0.05
    cp = _real_17_key_closed_pos(
        asset="NVDA", contract_id=10, side="short",
        avg_entry_price="100.0", value_base="0.50", margin_quote="2.5", leverage=2,
    )
    dedupe_key = walt_bot.make_position_dedupe_key(cp)
    snap_path = tmp_path / "open_snapshot.json"
    snap_path.write_text(json.dumps({
        dedupe_key: {
            "position_id": dedupe_key,
            "contract_id": 10,
            "base_asset": "NVDA",
            "side": "short",
            "leverage": 2,
            "margin": 2.5,
            "entry_price": 100.0,
            "funding_paid": 0.01,
            "tp": 97.5,
            "sl": 101.5,
            "opened_at": cp["created_at"],
            "verdict": "GO",
        }
    }), encoding="utf-8")

    client = MagicMock()
    client.list_positions.return_value = {"positions": [cp]}
    client.latest_rate.return_value = {"rate": "98.0"}

    walt_bot._detect_and_log_exchange_closes(
        client,
        active_positions=[],
        snapshot_path=str(snap_path),
        closed_ids_path=str(tmp_path / "closed_ids.json"),
        closed_csv_path=str(tmp_path / "closed_history.csv"),
    )
    assert len(captured_closes) == 1
    c = captured_closes[0]
    pnl_by_size_base = (100.0 - 98.0) * 0.50          # = +1.00
    pnl_by_margin_move = 2.5 * 2 * ((100.0 - 98.0) / 100.0)  # = +0.10
    assert pnl_by_size_base != pytest.approx(pnl_by_margin_move)
    assert c["pnl_quote"] == pytest.approx(pnl_by_size_base, abs=1e-6)

