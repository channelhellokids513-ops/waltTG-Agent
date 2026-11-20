import functools
import hashlib
import inspect
import os
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import sheet_logger
import walt_bot

_PROD_LOGS_DIR = REPO_ROOT / "logs"
_STATE_FILES = (
    "closed_history.csv",
    "closed_ids.json",
    "open_snapshot.json",
    "market_regime.json",
    "last_safe_summary.json",
    "executions_history.csv",
    "scan_history.csv",
    "pnl_tracker.csv",
    "strategy_config.json",
    "telegram_auth.json",
    "user_strategies.json",
    # R6-5 [V1]: dua jalur tulis produksi yang sebelumnya di luar pengawasan guard ronde 5.
    "security_audit.jsonl",
    "_tmp_sheet_batch.json",
)
_SESSION_STATE_SNAPSHOT: dict[str, tuple[int, str]] = {}


def _snapshot_prod_logs() -> dict[str, tuple[int, str]]:
    snap: dict[str, tuple[int, str]] = {}
    if not _PROD_LOGS_DIR.exists():
        return snap
    for name in _STATE_FILES:
        p = _PROD_LOGS_DIR / name
        if p.exists() and p.is_file():
            raw = p.read_bytes()
            lines = len(raw.splitlines())
            digest = hashlib.sha256(raw).hexdigest()
            snap[name] = (lines, digest)
    return snap


def pytest_sessionstart(session: pytest.Session) -> None:
    _SESSION_STATE_SNAPSHOT.clear()
    _SESSION_STATE_SNAPSHOT.update(_snapshot_prod_logs())


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    after = _snapshot_prod_logs()
    violations: list[str] = []
    all_names = sorted(set(_SESSION_STATE_SNAPSHOT) | set(after))
    for name in all_names:
        before_val = _SESSION_STATE_SNAPSHOT.get(name)
        after_val = after.get(name)
        if before_val == after_val:
            continue
        if before_val is None and after_val is not None:
            violations.append(f"LEDGER TERCEMAR: logs/{name} +{after_val[0]} baris (file baru dibuat)")
        elif before_val is not None and after_val is not None:
            delta = after_val[0] - before_val[0]
            if delta > 0:
                violations.append(f"LEDGER TERCEMAR: logs/{name} +{delta} baris")
            else:
                violations.append(
                    f"LEDGER TERCEMAR: logs/{name} berubah (delta={delta:+d} baris, hash berubah)"
                )
        elif before_val is not None and after_val is None:
            violations.append(f"LEDGER TERCEMAR: logs/{name} terhapus selama test")

    if violations:
        for msg in violations:
            print(f"\n{msg}", file=sys.stderr)
        session.exitstatus = 1


def _is_default_logs_path(value: Any) -> bool:
    if not isinstance(value, (str, os.PathLike)):
        return False
    norm = str(value).replace("\\", "/").lstrip("./")
    return norm.startswith("logs/")


def _wrap_default_path_args(fn, iso_logs: Path, path_param_names: tuple[str, ...], capture_list: list[dict] | None = None):
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        bound = sig.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        for pname in path_param_names:
            if pname in bound.arguments and _is_default_logs_path(bound.arguments[pname]):
                orig_name = Path(str(bound.arguments[pname])).name
                bound.arguments[pname] = str(iso_logs / orig_name)
        if capture_list is not None:
            capture_list.append(dict(bound.arguments))
        return fn(*bound.args, **bound.kwargs)

    return _wrapped


@pytest.fixture
def captured_closes() -> list[dict]:
    """Daftar panggilan sheet_logger.log_close yang tertangkap pada test aktif."""
    return []


@pytest.fixture
def captured_executions() -> list[dict]:
    """Daftar panggilan sheet_logger.log_execution yang tertangkap pada test aktif."""
    return []


@pytest.fixture(autouse=True)
def isolate_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_closes: list[dict], captured_executions: list[dict]):
    """
    Isolasi seluruh penulisan state produksi ke tmp_path/logs secara otomatis.
    Argumen path eksplisit dari test individual (mis. tmp_path sendiri) tetap dihormati.
    """
    iso_logs = tmp_path / "isolated_logs"
    iso_logs.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(walt_bot, "CLOSED_IDS_PATH", str(iso_logs / "closed_ids.json"))
    monkeypatch.setattr(sheet_logger, "FALLBACK_LOG_DIR", str(iso_logs))

    # Bungkus fungsi tulis sheet_logger (tetap mengeksekusi fungsi asli ke tmp_path + merekam payload)
    monkeypatch.setattr(
        sheet_logger,
        "log_close",
        _wrap_default_path_args(sheet_logger.log_close, iso_logs, ("filepath",), capture_list=captured_closes),
    )
    monkeypatch.setattr(
        sheet_logger,
        "log_execution",
        _wrap_default_path_args(sheet_logger.log_execution, iso_logs, ("filepath",), capture_list=captured_executions),
    )
    monkeypatch.setattr(
        sheet_logger,
        "log_scan_to_csv",
        _wrap_default_path_args(sheet_logger.log_scan_to_csv, iso_logs, ("filepath",)),
    )
    monkeypatch.setattr(
        sheet_logger,
        "log_pnl_tracker",
        _wrap_default_path_args(sheet_logger.log_pnl_tracker, iso_logs, ("filepath",)),
    )

    # Bungkus fungsi state walt_bot agar default logs/... diarahkan ke iso_logs
    for fn_name, param_names in (
        ("_set_asset_cooldown", ("regime_path",)),
        ("_is_asset_in_cooldown", ("regime_path",)),
        ("_load_execution_regime", ("regime_path",)),
        ("_save_execution_regime", ("regime_path",)),
        ("record_entry_execution", ("regime_path",)),
        ("check_entry_frequency_limits", ("regime_path",)),
        ("check_daily_circuit_breaker", ("regime_path",)),
        ("_update_regime_state", ("regime_path",)),
        ("_save_open_snapshot", ("snapshot_path",)),
        ("_record_open_position_verdict", ("snapshot_path",)),
        ("_load_closed_ids", ("path",)),
        ("_record_closed_id", ("path",)),
        ("_detect_and_log_exchange_closes", ("snapshot_path", "closed_ids_path", "closed_csv_path")),
    ):
        orig_fn = getattr(walt_bot, fn_name, None)
        if callable(orig_fn):
            monkeypatch.setattr(
                walt_bot,
                fn_name,
                _wrap_default_path_args(orig_fn, iso_logs, param_names),
            )

    return iso_logs
