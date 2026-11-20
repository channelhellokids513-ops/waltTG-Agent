"""Ronde 6 — Item 3 [R6-3]: watchdog mengawasi keempat workflow trading dengan ambang per-cadensi.

Logika watchdog pindah dari heredoc inline ke `tools/watchdog_check.py` supaya bisa diuji tanpa
menyentuh GitHub API nyata (RUNNER modul disuntik). Test ini tidak memanggil jaringan sama sekali.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

WORKFLOWS = Path(".github/workflows")
TRADING_WORKFLOWS = ["daily-scan.yml", "market-execute.yml", "crypto-execute.yml", "risk-monitor.yml"]


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("watchdog_check", Path("tools/watchdog_check.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wd = _load_watchdog()


def _steps(wf: dict) -> list[dict]:
    job = next(iter(wf["jobs"].values()))
    return job["steps"]


def _step_with_id(steps: list[dict], step_id: str) -> dict:
    found = [s for s in steps if s.get("id") == step_id]
    assert found, f"langkah dengan id={step_id!r} tidak ada"
    return found[0]


class FakeGh:
    """Papan ketik keluaran `gh run list` / `gh workflow run`."""

    def __init__(self, success_by_workflow: dict[str, list[dict]], active: dict[str, list[str]] | None = None):
        self.success = success_by_workflow
        self.active = active or {}
        self.dispatched: list[str] = []
        self.calls: list[str] = []

    def __call__(self, cmd, capture_output=False, text=False, timeout=None):
        joined = " ".join(cmd)
        self.calls.append(joined)
        if "workflow run" in joined:
            self.dispatched.append(cmd[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        workflow = cmd[cmd.index("--workflow") + 1]
        if "--status" in cmd and cmd[cmd.index("--status") + 1] == "success":
            rows = self.success.get(workflow, [])
        else:
            rows = [{"status": s} for s in self.active.get(workflow, [])]
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")


def _runs(minutes_ago: list[float]) -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"updatedAt": (now - timedelta(minutes=m)).isoformat(), "databaseId": 1}
        for m in minutes_ago
    ]


def _healthy_gh() -> FakeGh:
    return FakeGh(
        {
            "risk-monitor.yml": _runs([3.0]),
            "market-execute.yml": _runs([100.0]),
            "crypto-execute.yml": _runs([100.0]),
            "daily-scan.yml": _runs([400.0]),
        }
    )


def test_watchdog_mencatat_keempat_workflow_dengan_ambang_sendiri():
    assert [s["workflow"] for s in wd.SPECS] == [
        "risk-monitor.yml", "market-execute.yml", "crypto-execute.yml", "daily-scan.yml",
    ]
    for spec in wd.SPECS:
        assert spec["threshold_min"] >= spec["cadence_min"], "ambang harus >= cadence"
        assert spec["source"], "ambang wajib punya rujukan dokumentasi"
    assert wd.SPECS[0]["threshold_min"] == 30, "nilai warisan risk-monitor tidak boleh berubah diam-diam"
    assert wd.SPECS[1]["threshold_min"] == 2 * wd.SPECS[1]["cadence_min"] + 60
    assert wd.SPECS[2]["threshold_min"] == 2 * wd.SPECS[2]["cadence_min"] + 60
    assert wd.SPECS[3]["threshold_min"] == wd.SPECS[3]["cadence_min"] + 120


def test_ambang_cadence_terjejak_ke_dokumentasi():
    """Angka cadence tidak boleh karangan: cron-nya harus ada di STRATEGY.md §7.1."""
    doc = Path("STRATEGY.md").read_text(encoding="utf-8")
    expect = {
        "risk-monitor.yml": ("*/10 * * * *", 10),
        "market-execute.yml": ("43 */2 * * *", 120),
        "crypto-execute.yml": ("11 */2 * * *", 120),
        "daily-scan.yml": ("17 0,12 * * *", 720),
    }
    for workflow, (cron, cadence) in expect.items():
        assert cron in doc, f"cron {cron!r} untuk {workflow} tidak ditemukan di STRATEGY.md"
        spec = next(s for s in wd.SPECS if s["workflow"] == workflow)
        assert spec["cadence_min"] == cadence, f"{workflow}: cadence tidak sesuai dokumentasi"


def test_market_execute_stale_hanya_itu_yang_disebut_dan_didispatch(monkeypatch, tmp_path):
    sent: list[str] = []
    monkeypatch.setattr(wd, "send_alert", lambda text: sent.append(text) or True)
    monkeypatch.setenv("MAINTENANCE_MODE", "false")
    monkeypatch.setenv("WALT_WATCHDOG_DISPATCH_ENTRY", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))

    gh = FakeGh(
        {
            "risk-monitor.yml": _runs([4.0]),
            "market-execute.yml": _runs([900.0]),
            "crypto-execute.yml": _runs([50.0]),
            "daily-scan.yml": _runs([100.0]),
        }
    )
    monkeypatch.setattr(wd, "RUNNER", gh)

    assert wd.main([]) == 0
    assert len(sent) == 1
    alert = sent[0]
    assert "market-execute.yml" in alert
    assert "crypto-execute.yml" not in alert, "workflow sehat tidak boleh ikut disebut"
    assert "daily-scan.yml" not in alert and "risk-monitor.yml" not in alert
    assert gh.dispatched == ["market-execute.yml"], "hanya yang stale yang di-dispatch"
    table = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "market-execute.yml" in table and "STALE" in table


def test_semua_sehat_tidak_menghasilkan_alert_ataupun_dispatch(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(wd, "send_alert", lambda text: calls.append("alert") or True)
    monkeypatch.setattr(
        wd, "dispatch_workflow", lambda wf, run=None: calls.append(f"dispatch:{wf}") or "dispatched"
    )
    monkeypatch.setenv("MAINTENANCE_MODE", "false")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(wd, "RUNNER", _healthy_gh())

    assert wd.main([]) == 0
    assert calls == [], "watchdog tidak boleh mengirim alert/dispatch saat semua sehat"


def test_main_maintenance_mode_dilarang_alert_dan_dispatch(monkeypatch, capsys):
    calls: list[str] = []
    monkeypatch.setattr(wd, "send_alert", lambda text: calls.append("alert") or True)
    monkeypatch.setattr(wd, "dispatch_workflow", lambda wf, run=None: calls.append(f"dispatch:{wf}") or "x")

    def exploding_run(*a, **k):
        raise AssertionError("watchdog tidak boleh memanggil gh sama sekali saat MAINTENANCE_MODE=true")

    monkeypatch.setattr(wd, "RUNNER", exploding_run)
    monkeypatch.setenv("MAINTENANCE_MODE", "true")

    assert wd.main([]) == 0
    assert calls == []
    assert "MAINTENANCE_MODE=true" in capsys.readouterr().out


def test_main_stale_alert_satu_kali_dan_dispatch_entry_default_off(monkeypatch, tmp_path):
    sent: list[str] = []
    monkeypatch.setattr(wd, "send_alert", lambda text: sent.append(text) or True)
    monkeypatch.setenv("MAINTENANCE_MODE", "false")
    monkeypatch.delenv("WALT_WATCHDOG_DISPATCH_ENTRY", raising=False)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    gh = FakeGh(
        {
            "risk-monitor.yml": _runs([500.0]),
            "market-execute.yml": _runs([900.0]),
            "crypto-execute.yml": _runs([10.0]),
            "daily-scan.yml": _runs([10.0]),
        }
    )
    monkeypatch.setattr(wd, "RUNNER", gh)

    assert wd.main([]) == 0
    assert len(sent) == 1, "satu alert teragregasi untuk semua workflow stale"
    assert "risk-monitor.yml" in sent[0] and "market-execute.yml" in sent[0]
    # risk-monitor di-dispatch (warisan terbukti); workflow entry HANYA alert kecuali diaktifkan.
    assert gh.dispatched == ["risk-monitor.yml"]
    table = summary.read_text(encoding="utf-8")
    for wf in TRADING_WORKFLOWS:
        assert wf in table, f"Step Summary kehilangan {wf}"


def test_dispatch_entry_bisa_diaktifkan_lewat_variable(monkeypatch):
    monkeypatch.setenv("WALT_WATCHDOG_DISPATCH_ENTRY", "true")
    assert wd.dispatch_enabled_for("market-execute.yml") is True
    monkeypatch.setenv("WALT_WATCHDOG_DISPATCH_ENTRY", "false")
    assert wd.dispatch_enabled_for("market-execute.yml") is False
    monkeypatch.delenv("WALT_WATCHDOG_DISPATCH_ENTRY")
    assert wd.dispatch_enabled_for("market-execute.yml") is False, "default harus aman"
    assert wd.dispatch_enabled_for("risk-monitor.yml") is True, "perilaku warisan tidak hilang"


def test_guard_antrean_ganda_tetap_ada_per_workflow():
    gh = FakeGh({"market-execute.yml": _runs([900.0])}, active={"market-execute.yml": ["in_progress"]})
    assert wd.has_active_run("market-execute.yml", run=gh) is True
    assert wd.dispatch_workflow("market-execute.yml", run=gh) == "skipped_active"
    assert gh.dispatched == []


def test_watchdog_yml_memakai_modul_tanpa_heredoc_dan_tanpa_walt_api():
    raw = (WORKFLOWS / "watchdog.yml").read_text(encoding="utf-8")
    wf = yaml.safe_load(raw)
    assert wf["permissions"] == {"actions": "write", "contents": "read"}
    steps = _steps(wf)
    assert any("actions/checkout" in (s.get("uses") or "") for s in steps), "modul butuh checkout"
    runs = [s.get("run", "") for s in steps]
    assert any("python3 tools/watchdog_check.py" in r for r in runs)
    assert not any("- <<'PY'" in r for r in runs), "logika harus di modul teruji, bukan heredoc"
    assert "walletbot.me" not in raw and "walt_bot.py" not in raw, "watchdog hanya boleh API GitHub"
    src = Path("tools/watchdog_check.py").read_text(encoding="utf-8")
    assert "walletbot.me" not in src and "WaltClient" not in src


def test_watchdog_tidak_permnah_mengubah_timeout_atau_concurrency_group():
    """timeout-minutes 3 dan group walt-watchdog adalah invarian ronde 2-5 yang harus utuh."""
    wf = yaml.safe_load((WORKFLOWS / "watchdog.yml").read_text(encoding="utf-8"))
    job = next(iter(wf["jobs"].values()))
    assert job["timeout-minutes"] == 3
    assert wf["concurrency"]["group"] == "walt-watchdog"
    assert wf["concurrency"]["cancel-in-progress"] is False
    assert wf[True]["schedule"][0]["cron"] == "0 * * * *" or wf["on"]["schedule"][0]["cron"] == "0 * * * *"
