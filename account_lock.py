"""Proteksi dua-arah penuh untuk akun Walt (bot Telegram <-> GitHub Actions <-> CLI lokal).

Kedua dunia memakai `WALT_REFRESH_TOKEN` yang sama dan BERLAKI SEKALI PAKAI (single-use).
Tabrakan = `invalid_grant` di tengah job: terburuknya posisi terbuka tanpa TP/SL. Guard satu
arah (bot memeriksa run CI via API) sudah ada di `telegram_bot._idle_gate`; modul ini menutup
arah sebaliknya: CI & CLI memeriksa lock bersama sebelum menyentuh Walt.

Mekanisme: GitHub repository variable `ACCOUNT_LOCK_STATE` berisi JSON
    {"holder": "ci:monitor", "token": "<uuid>", "acquired_at": <epoch>, "ttl_sec": <int>}
Akuisisi = baca -> bebas/kedaluwarsa/milik-sendiri? -> tulis token kita -> tunggu settle ->
baca-balik; pegang hanya jika token yang terbaca == token kita. Bukan CAS atomik (API variable
GitHub tidak mendukung If-Match), tapi jendela tabrakan menyempit ke ±2 detik dan TTL
membersihkan lock basi bila proses mati — jauh lebih ketat daripada tanpa lock sama sekali.

Kebijakan kegagalan: SEMUA error backend (401/404/tanpa kredensial/jaringan) = fail-open
(True, "...") — menghentikan eksekusi trading karena lock tak terbaca lebih berbahaya
daripada risiko tabrakan kecil yang sudah dimitigasi auto-resync TokenManager.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Optional

import httpx

log = logging.getLogger("walt_bot")

VAR_NAME = "ACCOUNT_LOCK_STATE"


def enabled() -> bool:
    """Default ON; matikan eksplisit dengan ACCOUNT_LOCK=off di env."""
    return (os.environ.get("ACCOUNT_LOCK") or "on").strip().lower() not in {"0", "false", "no", "off"}


def _creds() -> tuple[str, str]:
    token = (
        os.environ.get("GH_PAT") or os.environ.get("GH_PATH") or os.environ.get("GITHUB_PAT")
        or os.environ.get("GITHUB_TOKEN") or ""
    ).strip()
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    return token, repo


def _headers() -> dict[str, str]:
    token, _repo = _creds()
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# --- transport (di-monkeypatch di test) --------------------------------------------------
_DIAG_LOGGED = False


def _log_backend_diag(op: str, code: int) -> None:
    """Sekali per proses: perjelas kenapa lock tak terbaca (biasanya PAT tanpa izin Variables)."""
    global _DIAG_LOGGED
    if _DIAG_LOGGED:
        return
    _DIAG_LOGGED = True
    log.warning(
        f"account_lock: GitHub API {op} HTTP {code} — lock tidak terbaca, berjalan fail-open. "
        f"Berikan izin 'Variables: Read and write' pada GH_PAT untuk mengaktifkan proteksi dua-arah."
    )


def _read_state() -> tuple[Optional[dict[str, Any]], str]:
    """(state|None, reason). None + reason 'backend_error'/'backend_unavailable' = tak terbaca."""
    token, repo = _creds()
    if not token or not repo:
        return None, "backend_unavailable"
    try:
        r = httpx.get(f"https://api.github.com/repos/{repo}/actions/variables/{VAR_NAME}",
                      headers=_headers(), timeout=10.0)
        if r.status_code == 404:
            return {}, ""  # variabel belum pernah dibuat -> bebas
        if r.status_code != 200:
            _log_backend_diag("GET variable", r.status_code)
            return None, "backend_error"
        value = (r.json() or {}).get("value") or ""
        try:
            state = json.loads(value) if value else {}
        except Exception:
            state = {}
        return (state if isinstance(state, dict) else {}), ""
    except Exception:
        return None, "backend_error"


def _write_state(value_json: str) -> tuple[bool, str]:
    token, repo = _creds()
    if not token or not repo:
        return False, "backend_unavailable"
    url = f"https://api.github.com/repos/{repo}/actions/variables/{VAR_NAME}"
    body = {"name": VAR_NAME, "value": value_json}
    try:
        r = httpx.patch(url, headers=_headers(), json=body, timeout=10.0)
        if r.status_code in (200, 204):
            return True, ""
        if r.status_code == 404:
            r = httpx.post(url, headers=_headers(), json=body, timeout=10.0)
            if r.status_code in (200, 201):
                return True, ""
        _log_backend_diag("PATCH variable", r.status_code)
        return False, "backend_error"
    except Exception:
        return False, "backend_error"


# --- lease per proses --------------------------------------------------------------------
_LEASES: dict[str, str] = {}  # holder -> token uuid yang sedang dipegang proses ini


def _is_free_or_expired(state: dict[str, Any]) -> bool:
    holder = str(state.get("holder") or "")
    if not holder:
        return True
    try:
        acquired = float(state.get("acquired_at") or 0.0)
        ttl = float(state.get("ttl_sec") or 0.0)
    except (TypeError, ValueError):
        return True
    return time.time() - acquired > ttl


def peek() -> Optional[dict[str, Any]]:
    """Status lock saat ini (untuk pesan ⏳ & /auth_setup). None bila tidak terbaca."""
    if not enabled():
        return None
    state, _reason = _read_state()
    if state is None:
        return None
    holder = str(state.get("holder") or "")
    if not holder:
        return {"holder": "", "remaining_sec": 0.0, "free": True}
    try:
        remaining = float(state.get("ttl_sec") or 0) - (time.time() - float(state.get("acquired_at") or 0))
    except (TypeError, ValueError):
        remaining = 0.0
    return {"holder": holder, "remaining_sec": max(0.0, remaining), "free": remaining <= 0.0}


def _encode(holder: str, token: str, ttl_sec: float) -> str:
    return json.dumps({"holder": holder, "token": token,
                       "acquired_at": round(time.time(), 1), "ttl_sec": int(ttl_sec)},
                      separators=(",", ":"))


def acquire(holder: str, ttl_sec: float, wait_sec: float = 0.0, settle_sec: float = 1.2) -> tuple[bool, str]:
    """Coba pegang lock atas nama `holder`. Selalu fail-open (True) saat backend tak terbaca."""
    if not enabled():
        return True, "disabled"
    state0, reason0 = _read_state()
    if state0 is None:
        return True, reason0  # backend_unavailable / backend_error -> jangan blokir
    deadline = time.time() + max(0.0, wait_sec)
    first = True
    while True:
        state, reason = (state0, reason0) if first else _read_state()
        first = False
        if state is None:
            return True, reason  # fail-open
        theirs = str(state.get("holder") or "")
        if theirs and theirs != holder and not _is_free_or_expired(state):
            if time.time() >= deadline:
                return False, f"held:{theirs}"
            time.sleep(min(1.5, max(0.1, deadline - time.time())))
            continue
        my_token = _LEASES.get(holder) or uuid.uuid4().hex
        ok, reason = _write_state(_encode(holder, my_token, ttl_sec))
        if not ok:
            return True, reason  # gagal menulis = backend error -> fail-open, jangan macet
        time.sleep(settle_sec)
        check, _cr = _read_state()
        if check is None or str(check.get("token") or "") == my_token:
            _LEASES[holder] = my_token
            if check is None:
                log.warning("account_lock: verifikasi read-back gagal; lanjut dengan lease lokal")
            return True, "acquired"
        if time.time() >= deadline:
            return False, f"contested:{str(check.get('holder') or '?')}"
        time.sleep(min(1.2, max(0.1, deadline - time.time())))


def release(holder: str) -> None:
    """Lepaskan lock bila token lease kita masih yang terpasang. Best-effort, tanpa exception."""
    my_token = _LEASES.pop(holder, None)
    if not my_token or not enabled():
        return
    state, _reason = _read_state()
    if state is None or str(state.get("token") or "") != my_token:
        return
    _write_state(json.dumps({"holder": "", "token": "", "acquired_at": 0, "ttl_sec": 0},
                            separators=(",", ":")))


