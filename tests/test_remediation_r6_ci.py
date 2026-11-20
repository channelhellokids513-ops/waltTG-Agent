"""Ronde 6 — Item 4 [R6-4]: least-privilege CI, build reproducible, diagnostics benar-benar read-only.

Guard di sini mengunci bentuk konfigurasi (bukan angka performansi): kalau seseorang melepas blok
`permissions:` atau mengembalikan `uses:` ke tag mayor, test ini merah — bukan selalu hijau.
"""
from __future__ import annotations

import glob
import re
from pathlib import Path

import pytest
import yaml
import token_manager

WORKFLOW_GLOB = ".github/workflows/*.yml"
PINNED = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "actions/cache/restore": "0057852bfaa89a56745cba8c7296529d2fc39830",
    "actions/cache/save": "0057852bfaa89a56745cba8c7296529d2fc39830",
}


def _load(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _steps(wf: dict) -> list[dict]:
    return next(iter(wf["jobs"].values()))["steps"]


def _env_of_run_steps(wf: dict) -> dict:
    merged: dict[str, str] = {}
    for step in _steps(wf):
        if step.get("run"):
            merged.update(step.get("env") or {})
    return merged


# ---------------------------------------------------------------------------
# 1) permissions eksplisit di 7 workflow + tidak ada uses bertag
# ---------------------------------------------------------------------------
# Workflow yang DIIZINKAN memegang scope lebih dari `contents: read`, dengan alasan dan ruang
# lingkup. Daftar putih (bukan pelonggaran global) supaya workflow baru yang meminta scope aneh
# tetap merah, dan scope yang sudah ada tidak bisa diam-diam melebar.
PERMISSIONS_ALLOWLIST = {
    # watchdog dispatch workflow lain sebagai fallback -> butuh actions: write
    "watchdog.yml": {"actions": "write", "contents": "read"},
    # [R7b] keepalive: risk-monitor & market-execute menembak watchdog.yml karena cron GitHub
    # terbukti bisa menguap ~7 jam (2026-11-20) dan Cloud Scheduler pemilik tak terjangkau repo.
    "risk-monitor.yml": {"actions": "write", "contents": "read"},
    "market-execute.yml": {"actions": "write", "contents": "read"},
}


def test_semua_workflow_punya_blok_permissions_eksplisit():
    files = sorted(glob.glob(WORKFLOW_GLOB))
    assert len(files) == 7, f"diharapkan 7 workflow, dapat {len(files)}"
    for f in files:
        wf = _load(f)
        assert "permissions" in wf, f"{f}: blok permissions tidak ada (token mewarisi default)"
        nama = f.split("\\")[-1].split("/")[-1]
        diharapkan = PERMISSIONS_ALLOWLIST.get(nama, {"contents": "read"})
        assert wf["permissions"] == diharapkan, (
            f"{nama}: permissions {wf['permissions']} != {diharapkan} "
            f"(tambah ke PERMISSIONS_ALLOWLIST hanya dengan alasan, jangan perlebar global)"
        )


def test_hanya_workflow_yang_punya_langkah_dispatch_yang_memegang_actions_write():
    """Kunci arah sebaliknya: scope `actions: write` harus benar-benar dipakai, bukan sisa izin.

    Dispatch bisa terjadi langsung (`gh workflow run ...`) atau lewat tool yang dipanggil langkah itu
    (watchdog dispatch di dalam `tools/watchdog_check.py`), jadi sumber tool ikut dibaca.
    """
    root = Path(__file__).resolve().parent.parent
    for nama, scope in PERMISSIONS_ALLOWLIST.items():
        if scope.get("actions") != "write":
            continue
        wf = yaml.safe_load((root / ".github" / "workflows" / nama).read_text(encoding="utf-8"))
        steps = next(iter(wf["jobs"].values()))["steps"]
        teks = "\n".join((s.get("run") or "") for s in steps)
        alat = re.findall(r"tools/([a-z_]+\.py)", teks)
        isi = "".join((root / "tools" / a).read_text(encoding="utf-8") for a in alat)
        terbukti = any(p in (teks + isi) for p in ("workflow run", "dispatch_workflow", "keep_watchdog_alive"))
        assert terbukti, (
            f"{nama}: memegang actions: write tanpa langkah dispatch yang terbukti — cabut scope-nya"
        )


def test_semua_uses_sudah_di_pin_sha_dengan_komentar_versi():
    offenders: list[str] = []
    for f in sorted(glob.glob(WORKFLOW_GLOB)):
        for lineno, line in enumerate(Path(f).read_text(encoding="utf-8").splitlines(), start=1):
            m = re.search(r"uses:\s*([\w./@-]+)@(\S+)", line.strip())
            if not m:
                continue
            action, ref = m.group(1), m.group(2)
            if ref.startswith("v") or re.match(r"^\d+$", ref):
                offenders.append(f"{f}:{lineno} masih memakai tag {ref}")
                continue
            assert re.fullmatch(r"[0-9a-f]{40}", ref.split()[0]), f"{f}:{lineno} ref bukan SHA 40 karakter"
            assert "#" in line, f"{f}:{lineno} pin SHA tanpa komentar versi"
            if action in PINNED:
                assert ref.split()[0] == PINNED[action], f"{f}:{lineno} SHA != yang diambil dari API GitHub"
    assert offenders == [], f"action bertag mayor masih dipakai: {offenders}"


# ---------------------------------------------------------------------------
# 2) requirements terkunci
# ---------------------------------------------------------------------------
def test_requirements_dikunci_dengan_equal():
    for fname in ("requirements.txt", "requirements-dev.txt"):
        lines = [
            ln.strip()
            for ln in Path(fname).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith(("#", "-r"))
        ]
        assert lines, f"{fname} tidak punya dependensi"
        for ln in lines:
            assert "==" in ln and ">" not in ln, f"{fname}: {ln} masih rentang versi"


def test_versi_terkunci_sama_dengan_yang_terpasang():
    import importlib.metadata as md

    expected = {
        "httpx": "0.28.1",
        "pynacl": "1.6.2",
        "pytest": "9.2.0",
        "pyyaml": "6.0.3",
    }
    for dist, want in expected.items():
        assert md.version(dist) == want, f"{dist} terpasang {md.version(dist)} != pin {want}"


# ---------------------------------------------------------------------------
# 3) diagnostics read-only: WALT_TOKEN_SYNC off, jalur trading tetap on
# ---------------------------------------------------------------------------
def test_diagnostics_wajib_menulis_balik_token_bukan_mematikannya():
    """
    R6-fix (dibuktikan run nyata): diagnostics.yml pernah diset WALT_TOKEN_SYNC=off. Itu membakar
    refresh token single-use tanpa menyimpan token baru -> secret mati -> risk-monitor gagal
    invalid_grant pada siklus berikutnya. Karena itu diagnostics HARUS on (tulis-balik).
    """
    env = _env_of_run_steps(_load(".github/workflows/diagnostics.yml"))
    assert env.get("WALT_TOKEN_SYNC") == "on", (
        "diagnostics.yml wajib WALT_TOKEN_SYNC=on; nilai off membuat kredensial CI mati"
    )


def test_mode_off_menolak_refresh_sebelum_http(monkeypatch):
    """Invarian: tidak bisa persist => tidak boleh consume. Nol panggilan OAuth."""
    calls: list[str] = []

    def forbidden(*a, **k):
        calls.append("httpx.post dipanggil")
        raise AssertionError("OAuth refresh tidak boleh terjadi saat WALT_TOKEN_SYNC=off")

    monkeypatch.setenv("WALT_TOKEN_SYNC", "off")
    monkeypatch.setenv("WALT_CLIENT_ID", "client-test")
    monkeypatch.setenv("WALT_REFRESH_TOKEN", "refresh-test")
    monkeypatch.setattr(token_manager.httpx, "post", forbidden)

    tm = token_manager.TokenManager()
    with pytest.raises(RuntimeError, match="single-use"):
        tm._refresh()
    assert calls == []


def test_mode_on_mengizinkan_refresh(monkeypatch):
    monkeypatch.setenv("WALT_TOKEN_SYNC", "on")
    monkeypatch.setattr(token_manager, "allow_refresh", lambda: None)
    monkeypatch.setattr(token_manager.httpx, "post", lambda *a, **k: _RotatingResponse())
    monkeypatch.setattr(token_manager.TokenManager, "_update_local_env", lambda self, tok: None)
    monkeypatch.setattr(token_manager, "update_github_secret", lambda *a, **k: True)
    monkeypatch.setenv("WALT_CLIENT_ID", "c")
    monkeypatch.setenv("WALT_REFRESH_TOKEN", "r")
    token_manager.TokenManager()._refresh()


@pytest.mark.parametrize(
    "fname", ["daily-scan.yml", "market-execute.yml", "crypto-execute.yml", "risk-monitor.yml"]
)
def test_jalur_trading_tidak_mematikan_rotasi_secret(fname):
    env = _env_of_run_steps(_load(f".github/workflows/{fname}"))
    assert env.get("WALT_TOKEN_SYNC") not in ("off", "false", "0", "no"), (
        f"{fname}: rotasi refresh token CI tidak boleh dimatikan"
    )


def test_token_sync_enabled_dapat_dimatikan_ekspisit(monkeypatch):
    monkeypatch.delenv("WALT_TOKEN_SYNC", raising=False)
    assert token_manager.token_sync_enabled() is True, "default harus on (perilaku lama)"
    for off_value in ("off", "OFF", "false", "0", "no", "read-only"):
        monkeypatch.setenv("WALT_TOKEN_SYNC", off_value)
        assert token_manager.token_sync_enabled() is False
    monkeypatch.setenv("WALT_TOKEN_SYNC", "on")
    assert token_manager.token_sync_enabled() is True


class _RotatingResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"access_token": "ACC", "expires_in": 3600, "refresh_token": "REFRESH_BARU"}

    @property
    def text(self) -> str:
        return '{"access_token":"ACC"}'


def _make_manager(monkeypatch):
    monkeypatch.setenv("WALT_CLIENT_ID", "client-test")
    monkeypatch.setenv("WALT_REFRESH_TOKEN", "refresh-lama")
    monkeypatch.setattr(token_manager.httpx, "post", lambda *a, **k: _RotatingResponse())
    written: list[tuple] = []
    monkeypatch.setattr(
        token_manager, "update_github_secret",
        lambda repo, name, value, pat: written.append((repo, name, value)) or True,
    )
    local: list[str] = []
    monkeypatch.setattr(
        token_manager.TokenManager, "_update_local_env", lambda self, tok: local.append(tok)
    )
    alerts: list[str] = []
    monkeypatch.setattr(token_manager, "_notify_telegram_safe", lambda msg: alerts.append(msg))
    tm = token_manager.TokenManager()
    return tm, written, local, alerts


def test_rotasi_saat_on_masih_menulis_secret(monkeypatch):
    monkeypatch.setenv("WALT_TOKEN_SYNC", "on")
    monkeypatch.setenv("GH_PAT", "pat-test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    tm, written, local, alerts = _make_manager(monkeypatch)

    tm._refresh()

    assert written == [("owner/repo", "WALT_REFRESH_TOKEN", "REFRESH_BARU")]
    assert local == ["REFRESH_BARU"]
    assert alerts == []


def test_mode_off_tidak_menulis_secret_karena_menolak_refresh(monkeypatch, caplog):
    """
    Dulu (R6-4) mode off berarti 'refresh tapi jangan simpan' — justru itulah yang mematikan
    kredensial. Sekarang off = menolak refresh sama sekali, sehingga tidak ada token yang terbakar.
    """
    monkeypatch.setenv("WALT_TOKEN_SYNC", "off")
    monkeypatch.setenv("GH_PAT", "pat-test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    tm, written, local, alerts = _make_manager(monkeypatch)

    with pytest.raises(RuntimeError, match="single-use"):
        tm._refresh()

    assert written == [], "mode read-only tidak boleh menulis GitHub Secret"
    assert local == [], "mode read-only tidak menyentuh .env lokal"
    assert alerts == [], "penolakan terjadi sebelum apa pun: tidak ada alert"
    assert tm.refresh_token == "refresh-lama", "token tidak boleh terbuang diam-diam"
