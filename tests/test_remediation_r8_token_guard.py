"""Ronde 8: pengaman yang membakar kredensial CI tidak boleh bisa terulang.

Tiga lapis, satu akar masalah yang sama (#44926211013, 16:44:44Z): langkah OAuth dengan
WALT_TOKEN_SYNC=on tapi tanpa GH_PAT/GITHUB_REPOSITORY -> token single-use terbakar,
token baru tidak ada tempatnya, semua run berikutnya invalid_grant.
"""

from __future__ import annotations

import glob
import re

import pytest
import yaml

import token_manager as tm
import walt_bot

WORKFLOWS = sorted(glob.glob(".github/workflows/*.yml"))
REQUIRED_WITH_OAUTH = ("WALT_TOKEN_SYNC", "GH_PAT", "GITHUB_REPOSITORY")


def _steps(doc: dict):
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps", []) or []:
            yield step


@pytest.mark.parametrize("path", WORKFLOWS)
def test_langkah_oauth_wajib_membawa_tiga_kunci_persistensi(path):
    """Invariant CI: env dengan WALT_REFRESH_TOKEN wajib memuat ketiganya sekaligus."""
    doc = yaml.safe_load(open(path, encoding="utf-8").read())
    offenders = []
    for step in _steps(doc):
        env = step.get("env") or {}
        if "WALT_REFRESH_TOKEN" not in env:
            continue
        missing = [k for k in REQUIRED_WITH_OAUTH if k not in env]
        sync = str(env.get("WALT_TOKEN_SYNC", "on")).strip().lower()
        if missing or sync in tm._TOKEN_SYNC_OFF:
            offenders.append((step.get("name", "?"), missing, sync))
    assert not offenders, f"{path}: langkah OAuth tanpa jalur persistensi lengkap: {offenders}"


def test_all_workflows_terbaca_dan_mengandung_diagnostics():
    assert WORKFLOWS, "glob workflow kosong - jalankan dari root repo"
    assert any("diagnostics" in p for p in WORKFLOWS)


def _clear(monkeypatch):
    for k in ("GH_PAT", "GITHUB_REPOSITORY", "GITHUB_ACTIONS"):
        monkeypatch.delenv(k, raising=False)


def test_ci_tanpa_gh_pat_ditolak_sebelum_sentuh_server(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("WALT_TOKEN_SYNC", "on")
    with pytest.raises(RuntimeError) as exc:
        tm.require_persistable_refresh()
    assert "SEBELUM memanggil Walt" in str(exc.value)
    assert "GH_PAT" in str(exc.value)


def test_ci_dengan_pat_lengkap_diperbolehkan(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("WALT_TOKEN_SYNC", "on")
    monkeypatch.setenv("GH_PAT", "pat-test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    tm.require_persistable_refresh()  # tidak boleh melempar


def test_sync_off_tetap_ditolak_lebih_dulu(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("WALT_TOKEN_SYNC", "off")
    monkeypatch.setenv("GH_PAT", "pat-test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    with pytest.raises(RuntimeError):
        tm.require_persistable_refresh()


def test_refresh_tidak_pernah_mengirim_post_saat_guard_merah(monkeypatch):
    """Inti klaim 'fail-closed sebelum HTTP POST': kalau guard menolak, Walt tidak tersentuh."""
    calls = []

    def fake_post(*_a, **_k):
        calls.append("POST")
        raise AssertionError("tidak boleh dipanggil")

    _clear(monkeypatch)
    monkeypatch.setattr(tm.httpx, "post", fake_post)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("WALT_TOKEN_SYNC", "on")
    mgr = tm.TokenManager(client_id="cid", refresh_token="rt")
    with pytest.raises(RuntimeError):
        mgr.get_access_token()
    assert calls == [], "refresh token single-use akan terbakar sebelum guard bekerja"


def test_formatter_monitor_mengescape_kasus_market_universe():
    actions = [{"asset": "MARKET_UNIVERSE", "detail": "GEAR_2_ACTIVE_60M (60m): 0 opened"},
               {"asset": "CRYPTO_UNIVERSE", "layer": "GEAR_2_ACTIVE_60M"}]
    msg = walt_bot.format_monitor_actions_telegram(actions)
    assert "MARKET\\_UNIVERSE" in msg and "GEAR\\_2\\_ACTIVE\\_60M" in msg
    assert "• *CRYPTO\\_UNIVERSE* — GEAR\\_2\\_ACTIVE\\_60M\n" in msg, "fallback layer dipakai"
    # penanda bold milik kita harus tetap seimbang (genap) di luar escape
    assert msg.replace("\\*", "").count("*") % 2 == 0
    assert "*" in msg and "\\*" not in msg.replace("*\n", ""), "bold asli tidak ikut ter-escape"


def test_formatter_monitor_tanpa_asset_tidak_hancur():
    assert "?" in walt_bot.format_monitor_actions_telegram([{"detail": "x"}])
    assert walt_bot.format_monitor_actions_telegram([]).endswith("diperbarui:\n")


def _opp(**kw):
    base = dict(contract_id=17, base_asset="ANTHROPIC", funding_daily=0.01, volume_24h=1e8,
                max_leverage=2, side="long", min_size_base=0.0, price_slippage=0.0001)
    base.update(kw)
    return walt_bot.Opportunity(**base)


class _Rate:
    def __init__(self, rate):
        self.rate = rate

    def __getitem__(self, _k):
        return self.rate


class _PriceClient:
    def __init__(self, price):
        self.price = price

    def latest_rate(self, _market):
        return _Rate(self.price)


def test_max_size_quote_dilampaui_skip_bukan_turunkan(monkeypatch):
    monkeypatch.setattr(walt_bot, "DEFAULT_MARGIN", 2.50)
    client = _PriceClient(100.0)
    assert walt_bot.resolve_order_margin(client, _opp(max_size_quote=4.99), margin=2.50,
                                         leverage=2) is None, "notional 5.00 > 4.99"
    ok = walt_bot.resolve_order_margin(client, _opp(max_size_quote=5.00), margin=2.50, leverage=2)
    assert ok is not None and ok[0] == pytest.approx(2.50), "tepat di plafon = boleh"


def test_max_size_base_dilampaui_skip():
    client = _PriceClient(10.0)  # notional 5.00 @ $10 = 0,5 unit
    assert walt_bot.resolve_order_margin(client, _opp(max_size_base=0.4), margin=2.50,
                                         leverage=2) is None
    assert walt_bot.resolve_order_margin(client, _opp(max_size_base=0.5), margin=2.50,
                                         leverage=2) is not None


def test_plafon_atas_nol_artinya_tidak_dibatasi():
    client = _PriceClient(100.0)
    got = walt_bot.resolve_order_margin(client, _opp(), margin=2.50, leverage=2)
    assert got is not None and got[0] == pytest.approx(2.50)


def test_opportunity_menyimpan_plafon_atas_dari_payload():
    fields = walt_bot.Opportunity.__dataclass_fields__
    assert {"max_size_base", "max_size_quote"} <= set(fields)
    assert re.search(r'c\.get\("max_size_quote"', open("walt_bot.py", encoding="utf-8").read()), \
        "payload kontrak harus benar-benar dibaca, bukan hanya didefinisikan"
