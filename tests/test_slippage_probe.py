"""Test pintu keselamatan tools/slippage_probe.py.

Yang diuji di sini bukan hasilnya (hasilnya satu sampel nyata), tapi fakta bahwa alat ini
TIDAK BISA membuka order tanpa tiga syarat, dan tidak berhenti sambil meninggalkan posisi.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys_env = {"SLIPPAGE_PROBE_CONFIRM": "saya-ersetuju-order-nyata", "MAINTENANCE_MODE": "true",
           "SLIPPAGE_PROBE_REAL_ORDER": "YA_ORDER_NYATA"}
GATE_KEYS = ("SLIPPAGE_PROBE_CONFIRM", "MAINTENANCE_MODE", "DRY_RUN", "SLIPPAGE_PROBE_REAL_ORDER")

SPEC = importlib.util.spec_from_file_location("slippage_probe", ROOT / "tools" / "slippage_probe.py")
sp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sp)


def set_env(monkeypatch, **overrides):
    env = dict(sys_env)
    env.update(overrides)
    for key in GATE_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_empat_pintu_semuanya_wajib(monkeypatch):
    set_env(monkeypatch)
    assert sp.check_gates() == []

    set_env(monkeypatch, SLIPPAGE_PROBE_CONFIRM="")
    assert any("SLIPPAGE_PROBE_CONFIRM" in r for r in sp.check_gates())

    set_env(monkeypatch, MAINTENANCE_MODE="false")
    assert any("MAINTENANCE_MODE" in r for r in sp.check_gates())

    set_env(monkeypatch, DRY_RUN="true")
    assert any("DRY_RUN" in r for r in sp.check_gates())

    # pintu kedua: mode oauth saja tidak cukup, harus bilang order nyata
    set_env(monkeypatch, SLIPPAGE_PROBE_REAL_ORDER="TIDAK_ORDER")
    assert any("real_order" in r for r in sp.check_gates())


def test_frasiskonfirmasi_harus_persis_bukan_seadanya_kata(monkeypatch):
    set_env(monkeypatch, SLIPPAGE_PROBE_CONFIRM="ya")
    assert sp.check_gates(), "frasa asal-asalan tidak boleh membuka pintu"


def test_fill_price_mengutamakan_isi_dalam_order_dan_ditolak_bila_hanya_sampah():
    assert sp.fill_price_from({"order": {"avg_fill_price": "101.25", "avg_close_price": "99.0"}}) == 101.25
    assert sp.fill_price_from({"position": {"avg_entry_price": 100.5}}) == 100.5
    assert sp.fill_price_from({"order": {"avg_fill_price": "n/a"}}) is None
    assert sp.fill_price_from(None) is None
    assert sp.fill_price_from("bukan dict") is None


def test_close_berhenti_setelah_percobaan_habis_dan_lapor_gagal():
    class DullClient:
        def __init__(self):
            self.calls = 0

        def preview_close(self, contract_id):
            self.calls += 1
            raise RuntimeError("relay mati")

    client = DullClient()
    ok, result, note = sp.close_position(client, 7)
    assert ok is False and result is None
    assert client.calls == sp.MAX_ATTEMPTS, "retry harus berhenti di MAX_ATTEMPTS"
    assert "attempt 3" in note


def test_close_sukses_mengembalikan_respons_asli_untuk_dibaca_harganya():
    class OkClient:
        def preview_close(self, contract_id):
            return {"ok": True, "confirmationToken": "tok"}

        def execute(self, token):
            return {"ok": True, "order": {"avg_close_price": "102.5"}}

    ok, result, note = sp.close_position(OkClient(), 7)
    assert ok is True
    assert sp.fill_price_from(result) == 102.5
