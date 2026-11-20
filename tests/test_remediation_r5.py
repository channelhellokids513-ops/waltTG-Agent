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
