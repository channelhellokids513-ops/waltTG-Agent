"""Tes modul account_lock + wiring CLI (backend difake, tanpa jaringan)."""
import time

import pytest

import account_lock as al
import walt_bot


@pytest.fixture()
def backend(monkeypatch):
    """In-memory store menggantikan transport GitHub Variables."""
    store = {"value": ""}
    calls = {"read": 0, "write": 0}

    def fake_read():
        import json as _json
        calls["read"] += 1
        return (_json.loads(store["value"]) if store["value"] else {}), ""

    def fake_write(value_json):
        calls["write"] += 1
        store["value"] = value_json
        return True, ""

    monkeypatch.setattr(al, "_read_state", fake_read)
    monkeypatch.setattr(al, "_write_state", fake_write)
    monkeypatch.setenv("ACCOUNT_LOCK", "on")
    monkeypatch.setenv("GH_PAT", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    al._LEASES.clear()
    # percepat settle pada tes
    real_sleep = time.sleep
    monkeypatch.setattr(time, "sleep", lambda s: real_sleep(min(s, 0.01)))
    return store, calls


def _state(store):
    import json
    return json.loads(store["value"]) if store["value"] else {}


def test_acquire_free_then_release(backend):
    store, _calls = backend
    ok, why = al.acquire("ci:monitor", ttl_sec=60, wait_sec=0)
    assert ok and why == "acquired"
    st = _state(store)
    assert st["holder"] == "ci:monitor" and st["token"]
    al.release("ci:monitor")
    assert _state(store)["holder"] == ""
    assert "ci:monitor" not in al._LEASES


def test_held_by_other_blocks_and_takeover_when_expired(backend):
    store, _ = backend
    import json
    fresh = json.dumps({"holder": "telegram", "token": "abc", "acquired_at": time.time(), "ttl_sec": 3600})
    store["value"] = fresh
    ok, why = al.acquire("ci:monitor", ttl_sec=60, wait_sec=0.05)
    assert not ok and "held:telegram" in why

    stale = json.dumps({"holder": "telegram", "token": "abc", "acquired_at": time.time() - 7200, "ttl_sec": 3600})
    store["value"] = stale
    ok2, why2 = al.acquire("ci:monitor", ttl_sec=60, wait_sec=0)
    assert ok2 and why2 == "acquired"
    assert _state(store)["holder"] == "ci:monitor"


def test_reacquire_same_holder_refreshes(backend):
    store, _ = backend
    assert al.acquire("ci:monitor", ttl_sec=60, wait_sec=0)[0] is True
    tok1 = _state(store)["token"]
    assert al.acquire("ci:monitor", ttl_sec=60, wait_sec=0)[0] is True
    assert _state(store)["token"] == tok1  # lease holder sama -> token dipertahankan


def test_race_lost_on_readback_fails_acquire(backend, monkeypatch):
    store, _ = backend
    real_read = al._read_state
    seq = []

    def poisoned_read():
        st, r = real_read()
        if st.get("holder") == "ci:monitor" and not seq:
            seq.append(1)
            return {"holder": "telegram", "token": "zz", "acquired_at": time.time(), "ttl_sec": 600}, ""
        return st, r

    monkeypatch.setattr(al, "_read_state", poisoned_read)
    ok, why = al.acquire("ci:monitor", ttl_sec=60, wait_sec=0)
    assert not ok and "contested" in why


def test_backend_unreadable_fails_open(backend, monkeypatch):
    monkeypatch.setattr(al, "_read_state", lambda: (None, "backend_error"))
    ok, why = al.acquire("telegram", ttl_sec=60, wait_sec=0)
    assert ok and why == "backend_error"
    assert al.peek() is None


def test_disabled_switch_skips_transport(backend, monkeypatch):
    monkeypatch.setenv("ACCOUNT_LOCK", "off")
    _store, calls = backend
    ok, why = al.acquire("telegram", ttl_sec=60, wait_sec=0)
    assert ok and why == "disabled" and calls["read"] == 0


def test_release_only_clears_our_token(backend):
    store, _ = backend
    import json
    assert al.acquire("ci:monitor", ttl_sec=60, wait_sec=0)[0]
    # proses lain menimpa lock setelah lease kita
    store["value"] = json.dumps({"holder": "telegram", "token": "zzz",
                                 "acquired_at": time.time(), "ttl_sec": 600})
    al.release("ci:monitor")  # token tidak cocok -> tidak boleh menulis kosong
    assert _state(store)["holder"] == "telegram"


def test_cli_account_lock_helpers(backend, monkeypatch):
    seen = []
    monkeypatch.setattr(al, "acquire", lambda holder, ttl_sec, wait_sec=0.0, settle_sec=1.2:
                        seen.append(("acquire", holder, ttl_sec)) or (True, "acquired"))
    monkeypatch.setattr(al, "release", lambda holder: seen.append(("release", holder)))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    holder = walt_bot.cli_account_lock_enter("monitor")
    assert holder == "ci:monitor"
    assert ("acquire", "ci:monitor", 1800) in seen

    walt_bot.cli_account_lock_exit(holder)
    assert ("release", "ci:monitor") in seen

    assert walt_bot.cli_account_lock_enter("stats") is None
    walt_bot.cli_account_lock_exit(None)
    assert len([s for s in seen if s[0] == "acquire"]) == 1  # stats tanpa lock


def test_cli_lock_warns_but_proceeds_when_held(backend, monkeypatch):
    # acquire gagal (telegram memegang lock) -> CLI tetap jalan BEST-EFFORT dengan warning;
    # release di akhir harus no-op karena kita tidak pernah memegang lease.
    monkeypatch.setattr(al, "acquire", lambda holder, ttl_sec, wait_sec=0.0, settle_sec=1.2:
                        (False, "held:telegram"))
    released = []
    monkeypatch.setattr(al, "release", lambda holder: released.append(holder))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    holder = walt_bot.cli_account_lock_enter("market_execute")
    assert holder == "ci:market_execute"
    walt_bot.cli_account_lock_exit(holder)
    # release NO-OP aman: lease tidak pernah kita pegang -> state telegram tak tersentuh
    assert released == ["ci:market_execute"]

