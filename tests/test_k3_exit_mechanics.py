"""Regresi K3 (2025-11-25): mekanika exit & risiko.

Empat invarian yang dikunci di sini — semuanya berasal dari pengukuran 21-24 Nov 2026
(median hold 0.95 jam, payoff 0.50, expectancy -0.0353/trade, MON -8.8% margin dalam 6 menit):

1. `Funding Flip Sign` (Engine 1) TIDAK boleh menutup posisi sebelum biaya round-trip
   punya waktu tertutup, kecuali rugi harga sudah >= separuh jarak SL.
2. Engine 2 (price_forecast) tetap keluar segera — thesis-nya bukan carry.
3. TP/SL dihitung ulang dari harga FILL bila drift melewati ambang.
4. Lock trailing tier-1 harus di atas biaya round-trip, dan plafon SL dinamis <= 2.5%.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import walt_bot
from walt_bot import Opportunity, Position


def _pos(**kw) -> Position:
    base = dict(
        contract_id=1,
        base_asset="AAVE",
        side="long",
        leverage=2,
        margin=2.5,
        entry_price=100.0,
        current_price=100.0,
        tp=102.0,
        sl=98.0,
        funding_paid=0.0,
        liquidation_price=50.0,
    )
    base.update(kw)
    return Position(**base)


def _opened_hours_ago(now: datetime, hours: float) -> str:
    return (now - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


class _RiskClient:
    def __init__(self):
        self.preview_close_calls: list[int] = []
        self.auto_close_calls: list[dict] = []
        self.execute_tokens: list[str] = []

    def preview_close(self, contract_id):
        self.preview_close_calls.append(contract_id)
        return {"ok": True, "confirmationToken": "tok_close"}

    def preview_auto_close(self, contract_id, tp=None, sl=None):
        self.auto_close_calls.append({"contract_id": contract_id, "tp": tp, "sl": sl})
        return {"ok": True, "confirmationToken": "tok_ac"}

    def execute(self, token):
        self.execute_tokens.append(token)
        return {"ok": True}


# ---------------------------------------------------------------------------
# K3-1: min-hold pada Funding Flip Sign
# ---------------------------------------------------------------------------

def test_funding_flip_ditahan_sebelum_biaya_round_trip_tertutup(monkeypatch):
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **k: None)
    now = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
    # LONG, funding berbalik positif (adverse), umur 1 jam, rugi harga 0.1% (2.9% jarak SL 3.5%)
    pos = _pos(
        opened_at=_opened_hours_ago(now, 1.0),
        current_price=99.9,
        planned_sl_pct=0.035,
    )
    client = _RiskClient()
    res = walt_bot.evaluate_and_apply_position_risk(client, pos, 0.004, now_utc=now)
    assert client.preview_close_calls == [], "flip pada jam ke-1 tidak boleh menutup posisi"
    assert not (res and res.get("action") == "CLOSE")


def test_funding_flip_diizinkan_setelah_min_hold(monkeypatch):
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **k: None)
    now = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
    pos = _pos(
        opened_at=_opened_hours_ago(now, walt_bot.MIN_HOLD_BEFORE_FUNDING_FLIP_EXIT_H + 1.0),
        current_price=99.9,
        planned_sl_pct=0.035,
    )
    client = _RiskClient()
    res = walt_bot.evaluate_and_apply_position_risk(client, pos, 0.004, now_utc=now)
    assert client.preview_close_calls == [1]
    assert res is not None and res["action"] == "CLOSE"


def test_funding_flip_diizinkan_bila_rugi_sudah_separuh_jarak_sl(monkeypatch):
    """Risiko harga sudah lebih besar daripada biaya -> menahan posisi bukan lagi pilihan aman."""
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **k: None)
    now = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
    pos = _pos(
        opened_at=_opened_hours_ago(now, 1.0),
        current_price=98.0,          # rugi 2.0%
        planned_sl_pct=0.020,        # jarak SL 2.0% -> loss_frac = 100%
    )
    client = _RiskClient()
    res = walt_bot.evaluate_and_apply_position_risk(client, pos, 0.004, now_utc=now)
    assert client.preview_close_calls == [1]
    assert res is not None and res["action"] == "CLOSE"


def test_engine2_adverse_funding_tetap_keluar_segera(monkeypatch):
    """Engine 2 adalah posisi directional: adverse funding = thesis batal, bukan sekadar biaya."""
    monkeypatch.setattr(walt_bot, "_record_position_close", lambda *a, **k: None)
    now = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
    pos = _pos(
        base_asset="SOL",
        opened_at=_opened_hours_ago(now, 0.5),
        current_price=99.95,
        signal_source="price_forecast",
        planned_sl_pct=0.03,
    )
    client = _RiskClient()
    funding = walt_bot.FORECAST_ADVERSE_FUNDING_CLOSE_THRESHOLD * 2
    res = walt_bot.evaluate_and_apply_position_risk(client, pos, funding, now_utc=now)
    assert client.preview_close_calls == [1]
    assert res is not None and res["action"] == "CLOSE"


# ---------------------------------------------------------------------------
# K3-2: TP/SL mengikuti harga fill
# ---------------------------------------------------------------------------

def test_plan_reanchor_tidak_bertindak_di_bawah_ambang():
    opp = Opportunity(contract_id=1, base_asset="MON", funding_daily=0.01,
                      volume_24h=1e9, max_leverage=2, side="long")
    assert walt_bot._plan_fill_reanchor(opp, 100.0, 100.05, 0.05, 0.02) is None


def test_plan_reanchor_long_dan_short_memakai_harga_fill():
    opp_long = Opportunity(contract_id=1, base_asset="MON", funding_daily=0.01,
                           volume_24h=1e9, max_leverage=2, side="long")
    plan = walt_bot._plan_fill_reanchor(opp_long, 100.0, 99.5, 0.05, 0.02)
    assert plan is not None
    assert plan["tp"] == pytest.approx(round(99.5 * 1.05, 4))
    assert plan["sl"] == pytest.approx(round(99.5 * 0.98, 4))
    assert plan["drift"] == pytest.approx(0.005)

    opp_short = Opportunity(contract_id=1, base_asset="MON", funding_daily=0.01,
                            volume_24h=1e9, max_leverage=2, side="short")
    plan_s = walt_bot._plan_fill_reanchor(opp_short, 100.0, 100.5, 0.05, 0.02)
    assert plan_s is not None
    assert plan_s["tp"] == pytest.approx(round(100.5 * 0.95, 4))
    assert plan_s["sl"] == pytest.approx(round(100.5 * 1.02, 4))


class _OpenClient:
    """Klien minimal untuk open_position: fill palsu, tanpa jaringan."""

    def __init__(self, fill: float):
        self.fill = fill
        self.auto_close_calls: list[dict] = []
        self.execute_tokens: list[str] = []

    def preview_order(self, contract_id, side, leverage, amount, price, tp=None, sl=None):
        return {
            "ok": True,
            "confirmationToken": "tok_open",
            "action": {"request": {"autoClose": {"takeProfit": {}, "stopLoss": {}}}},
        }

    def execute(self, token):
        self.execute_tokens.append(token)
        if token == "tok_open":
            return {"ok": True, "order": {"order_id": "o1"}}
        return {"ok": True}

    def record_open_slippage(self, contract_id, result, settle_seconds=3.0):
        return {"stage": "open", "contract_id": contract_id, "fill": self.fill}

    def preview_auto_close(self, contract_id, tp=None, sl=None):
        self.auto_close_calls.append({"contract_id": contract_id, "tp": tp, "sl": sl})
        return {"ok": True, "confirmationToken": "tok_ac"}


def _forecast_opp() -> Opportunity:
    return Opportunity(
        contract_id=1, base_asset="MON", funding_daily=0.01, volume_24h=1e9,
        max_leverage=2, side="long", signal_source="price_forecast",
        planned_sl_pct=0.03, planned_tp_pct=0.06,
    )


def test_open_position_menggeser_tp_sl_ke_harga_fill(monkeypatch):
    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda _m: True)
    monkeypatch.setattr(walt_bot.time, "sleep", lambda *_a, **_k: None)
    client = _OpenClient(fill=99.4)  # drift 0.6% dari harga keputusan 100.0
    res = walt_bot.open_position(client, _forecast_opp(), margin=2.5, leverage=2,
                                 resolved=(2.5, 100.0))
    assert res is not None
    assert client.auto_close_calls, "TP/SL wajib digeser ke harga fill"
    call = client.auto_close_calls[-1]
    assert call["sl"] == pytest.approx(round(99.4 * 0.97, 4))
    assert call["tp"] == pytest.approx(round(99.4 * 1.06, 4))
    assert res["sl"] == call["sl"] and res["tp"] == call["tp"]
    assert res["fill_price"] == pytest.approx(99.4)


def test_open_position_tidak_menggeser_bila_drift_kecil(monkeypatch):
    monkeypatch.setattr(walt_bot, "has_enough_job_time", lambda _m: True)
    monkeypatch.setattr(walt_bot.time, "sleep", lambda *_a, **_k: None)
    client = _OpenClient(fill=100.02)  # 2 bps < ambang 15 bps
    res = walt_bot.open_position(client, _forecast_opp(), margin=2.5, leverage=2,
                                 resolved=(2.5, 100.0))
    assert res is not None
    assert client.auto_close_calls == []
    assert client.execute_tokens == ["tok_open"]


# ---------------------------------------------------------------------------
# K3-3 / K3-4: plafon SL dan lock trailing di atas biaya
# ---------------------------------------------------------------------------

def test_plafon_sl_dinamis_tidak_melebihi_dua_setengah_persen():
    _, _, _, sl_crypto = walt_bot.compute_dynamic_tpsl("long", 100.0, asset="MON", atr_24h_pct=0.20)
    _, _, _, sl_calm = walt_bot.compute_dynamic_tpsl("long", 100.0, asset="SPY", atr_24h_pct=0.20)
    assert sl_crypto == pytest.approx(0.025)
    assert sl_calm == pytest.approx(0.020)


def test_lock_trailing_tier1_di_atas_biaya_round_trip():
    fee_plus_slippage = (2 * walt_bot.TAKER_FEE_PER_SIDE) + 0.0026
    lock = walt_bot._TRAILING_HANDLERS["lock_0060"][0]
    assert lock > fee_plus_slippage, "mengunci di bawah biaya membuat 'menang' tetap rugi bersih"
    assert walt_bot.TRAILING_TIERS[0][1] == "lock_0060"