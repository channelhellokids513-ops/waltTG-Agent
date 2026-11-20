"""
Manajemen konfigurasi strategi dinamis (logs/strategy_config.json) dengan batasan aman.
Digunakan oleh walt_bot.py dan dapat diatur oleh Administrator / Planner lewat Bot Telegram.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("strategy_config")

STRATEGY_CONFIG_FILE = Path("logs") / "strategy_config.json"
SHARED_STRATEGY_VAR_NAME = "STRATEGY_CONFIG_JSON"

# Kadens daily-scan di Cloud Scheduler (0 0,12 * * *) = 12 jam. Dituliskan di sini karena JENDELA
# KEGUNAAN RISET wajib mengikutinya: dengan jendela lebih kecil dari kadens, riset hasil scan
# sendiri dianggap basi sebelum siklus berikutnya tiba — market/crypto-execute SKIP "riset basi"
# selama sisa hari. Dibuktikan CI 16:57Z: riset terbaru 16,7 jam, jendela 6 jam -> 0 aset terpakai.
DAILY_SCAN_CADENCE_HOURS = 12.0

# Satu sumber untuk jendela riset: dipakai default config, fallback pembacaan sheet, dan test
# koherensi. Kalau suatu hari kadens scan diubah, nilai ini ikut dihitung ulang — bukan disalin.
DEFAULT_RESEARCH_AGE_HOURS = DAILY_SCAN_CADENCE_HOURS + 1.0

DEFAULT_STRATEGY_CONFIG: dict[str, Any] = {
    "default_sl_pct": 0.015,        # 1.5%
    "tp_calm_pct": 0.020,           # 2.0% (ETF & Crypto)
    "tp_volatile_pct": 0.025,       # 2.5% (Komoditas & Saham Volatil)
    "min_funding_daily": 0.005,     # 0.50%/hari
    "reentry_cooldown_hours": 3.0,  # 3 jam
    "max_slots_cap": 15,            # batas atas slot elastis
    "max_p5_risk": 0.70,            # E2: P5_risk_catalysts >= 0.70 -> SKIP
    # E3: jendela riset = kadens scan + margin 1 jam (lihat DAILY_SCAN_CADENCE_HOURS di atas).
    "max_research_age_hours": DEFAULT_RESEARCH_AGE_HOURS,
    "max_entries_per_asset_24h": 2, # E4: maks 2 entry per aset per 24 jam
    "max_entries_per_day": 8,       # E4: maks 8 entry total per 24 jam
    "daily_loss_limit_pct": 0.05,   # E4: circuit breaker turun >= 5% dari ekuitas awal hari UTC
    "expected_hold_hours": 24.0,    # E5: asumsi durasi tahan posisi (jam) untuk gerbang funding vs fee
    "dry_run": False,
}

# Batasan aman agar perubahan dari Telegram tidak merusak proteksi modal
STRATEGY_PARAM_SPECS: dict[str, dict[str, Any]] = {
    "default_sl_pct": {"type": "float", "min": 0.005, "max": 0.05, "desc": "Stop Loss seragam (desimal, mis. 0.015 = 1.5%)"},
    "tp_calm_pct": {"type": "float", "min": 0.005, "max": 0.10, "desc": "Take Profit ETF & Crypto (mis. 0.02 = 2.0%)"},
    "tp_volatile_pct": {"type": "float", "min": 0.005, "max": 0.15, "desc": "Take Profit Komoditas & Saham (mis. 0.025 = 2.5%)"},
    "min_funding_daily": {"type": "float", "min": 0.001, "max": 0.05, "desc": "Minimal |funding/hari| (mis. 0.005 = 0.50%)"},
    "reentry_cooldown_hours": {"type": "float", "min": 0.0, "max": 24.0, "desc": "Cooldown re-entry pasca-close (jam)"},
    "max_slots_cap": {"type": "int", "min": 1, "max": 15, "desc": "Batas maksimal slot posisi terbuka (1-15)"},
    "max_p5_risk": {"type": "float", "min": 0.3, "max": 1.0, "desc": "Ambang P5_risk_catalysts untuk SKIP (0.3-1.0)"},
    "max_research_age_hours": {"type": "float", "min": 1.0, "max": 24.0, "desc": "Umur maksimal riset valid sebelum dianggap basi (jam)"},
    "max_entries_per_asset_24h": {"type": "int", "min": 1, "max": 10, "desc": "Maksimal entry per aset dalam 24 jam"},
    "max_entries_per_day": {"type": "int", "min": 1, "max": 30, "desc": "Maksimal total entry dalam 24 jam"},
    "daily_loss_limit_pct": {"type": "float", "min": 0.01, "max": 0.25, "desc": "Batas penurunan ekuitas harian UTC untuk circuit breaker (mis. 0.05 = 5%)"},
    "expected_hold_hours": {"type": "float", "min": 1.0, "max": 168.0, "desc": "Target durasi tahan posisi (jam) untuk gerbang ekonomi funding vs fee"},
    "dry_run": {"type": "bool", "desc": "Mode simulasi tanpa order riil (true/false)"},
}


def validate_strategy_dict(raw: dict[str, Any], strict: bool = False) -> dict[str, Any]:
    """
    Validasi kamus konfigurasi terhadap STRATEGY_PARAM_SPECS.
    - Jika strict=True, nilai di luar batas/tipe salah melempar ValueError.
    - Jika strict=False, nilai tidak valid ditolak dengan log warning dan diganti default aman.
    """
    validated = dict(DEFAULT_STRATEGY_CONFIG)
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("Konfigurasi strategi harus berupa JSON object.")
        log.warning("Konfigurasi strategi tidak valid (bukan dict), fallback ke DEFAULT_STRATEGY_CONFIG.")
        return validated

    for k, spec in STRATEGY_PARAM_SPECS.items():
        if k not in raw:
            continue
        val = raw[k]
        ptype = spec["type"]
        try:
            if ptype == "float":
                if isinstance(val, bool):
                    raise ValueError(f"{k} bukan float")
                fval = float(val)
                if not (spec["min"] <= fval <= spec["max"]):
                    raise ValueError(f"Nilai {k}={fval} di luar batas [{spec['min']}, {spec['max']}]")
                validated[k] = round(fval, 6)
            elif ptype == "int":
                if isinstance(val, bool):
                    raise ValueError(f"{k} bukan int")
                ival = int(val)
                if not (spec["min"] <= ival <= spec["max"]):
                    raise ValueError(f"Nilai {k}={ival} di luar batas [{spec['min']}, {spec['max']}]")
                validated[k] = ival
            elif ptype == "bool":
                if isinstance(val, bool):
                    validated[k] = val
                elif isinstance(val, str) and val.strip().lower() in {"true", "1", "yes", "on"}:
                    validated[k] = True
                elif isinstance(val, str) and val.strip().lower() in {"false", "0", "no", "off"}:
                    validated[k] = False
                else:
                    raise ValueError(f"Nilai {k}={val} bukan bool")
        except Exception as exc:
            if strict:
                raise ValueError(str(exc)) from exc
            log.warning(f"Parameter strategi '{k}' ditolak ({exc}) — pakai default {DEFAULT_STRATEGY_CONFIG[k]}.")

    if "updated_at" in raw and isinstance(raw["updated_at"], str):
        validated["updated_at"] = raw["updated_at"]
    if "updated_by" in raw and isinstance(raw["updated_by"], str):
        validated["updated_by"] = raw["updated_by"]
    return validated


def serialize_shared_strategy_config(cfg: dict[str, Any]) -> str:
    """
    Serialisasi konfigurasi strategi aman untuk GitHub Repository Variable STRATEGY_CONFIG_JSON.
    HANYA menyertakan parameter numerik/boolean di STRATEGY_PARAM_SPECS + updated_at
    (TIDAK menyertakan ID Telegram, email, atau data user_strategies.json).
    """
    clean = validate_strategy_dict(cfg, strict=True)
    payload: dict[str, Any] = {k: clean[k] for k in STRATEGY_PARAM_SPECS}
    if "updated_at" in clean:
        payload["updated_at"] = clean["updated_at"]
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def get_strategy_version_hash(cfg: dict[str, Any] | None = None) -> str:
    """Hash pendek (8 char hex) dari konfigurasi strategi aktif untuk pelacakan versi."""
    active = cfg if cfg is not None else load_strategy_config()
    clean = validate_strategy_dict(active, strict=False)
    canonical = json.dumps({k: clean[k] for k in STRATEGY_PARAM_SPECS}, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


def sync_strategy_config_to_github(
    cfg: dict[str, Any],
    repo: str | None = None,
    token: str | None = None,
) -> tuple[bool, str]:
    """
    Sinkronkan konfigurasi strategi aktif ke GitHub Repository Variable `STRATEGY_CONFIG_JSON`
    agar langsung terbaca oleh GitHub Actions pada run berikutnya.
    """
    gh_token = (
        token
        or os.environ.get("GH_PAT")
        or os.environ.get("GH_PATH")
        or os.environ.get("GITHUB_PAT")
        or ""
    ).strip()
    gh_repo = (repo or os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if not gh_token or not gh_repo:
        return False, "GH_PAT atau GITHUB_REPOSITORY belum disetel (hanya tersimpan lokal)."

    value_json = serialize_shared_strategy_config(cfg)
    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    var_url = f"https://api.github.com/repos/{gh_repo}/actions/variables/{SHARED_STRATEGY_VAR_NAME}"
    create_url = f"https://api.github.com/repos/{gh_repo}/actions/variables"

    try:
        r = httpx.patch(
            var_url,
            headers=headers,
            json={"name": SHARED_STRATEGY_VAR_NAME, "value": value_json},
            timeout=15.0,
        )
        if r.status_code == 404:
            r = httpx.post(
                create_url,
                headers=headers,
                json={"name": SHARED_STRATEGY_VAR_NAME, "value": value_json},
                timeout=15.0,
            )
        if r.status_code in (200, 201, 204):
            return True, f"Tersinkron ke GitHub Variable `{SHARED_STRATEGY_VAR_NAME}` (aktif mulai run berikutnya)."
        return False, f"GitHub API HTTP {r.status_code}: {r.text[:160]}"
    except Exception as exc:
        return False, f"Gagal sync GitHub Variable: {exc}"


def load_strategy_config(
    config_path: Path | None = None,
    env_json: str | None = None,
) -> dict[str, Any]:
    """
    Muat konfigurasi strategi runtime:
    1. Prioritas utama: sumber bersama `STRATEGY_CONFIG_JSON` (GitHub Repository Variable).
    2. Fallback lokal: `logs/strategy_config.json`.
    3. Fallback aman: `DEFAULT_STRATEGY_CONFIG` dengan log warning bila parsing gagal.
    """
    shared_raw = os.environ.get(SHARED_STRATEGY_VAR_NAME) if env_json is None else env_json
    if shared_raw and shared_raw.lstrip("\ufeff").strip():
        try:
            parsed = json.loads(shared_raw.lstrip("\ufeff").strip())
            cfg = validate_strategy_dict(parsed, strict=False)
            cfg["source"] = "github_variable"
            return cfg
        except Exception as exc:
            log.warning(
                f"Gagal parse sumber bersama {SHARED_STRATEGY_VAR_NAME} ({exc}); "
                "fallback ke file lokal / DEFAULT_STRATEGY_CONFIG."
            )

    target = config_path or STRATEGY_CONFIG_FILE
    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            cfg = validate_strategy_dict(raw, strict=False)
            cfg["source"] = "local_file"
            return cfg
        except Exception as exc:
            log.warning(f"Gagal parse {target} ({exc}); fallback ke DEFAULT_STRATEGY_CONFIG.")

    cfg = dict(DEFAULT_STRATEGY_CONFIG)
    cfg["source"] = "default"
    return cfg


def update_strategy_param(
    key: str,
    raw_value: str,
    updated_by: str = "telegram",
    config_path: Path = STRATEGY_CONFIG_FILE,
) -> dict[str, Any]:
    """Validasi dan simpan satu parameter strategi."""
    k = key.strip().lower()
    if k not in STRATEGY_PARAM_SPECS:
        valid = ", ".join(STRATEGY_PARAM_SPECS.keys())
        raise ValueError(f"Parameter '{key}' tidak dikenal. Pilihan valid: {valid}")

    spec = STRATEGY_PARAM_SPECS[k]
    ptype = spec["type"]

    if ptype == "float":
        val_str = raw_value.strip().rstrip("%")
        val = float(val_str)
        # Bantu konversi otomatis jika user menulis persentase seperti "1.5%" untuk field pct/funding
        if raw_value.strip().endswith("%"):
            val = val / 100.0
        if not (spec["min"] <= val <= spec["max"]):
            raise ValueError(f"Nilai {k} harus di antara {spec['min']} dan {spec['max']}.")
        parsed: Any = round(val, 6)
    elif ptype == "int":
        parsed = int(float(raw_value.strip()))
        if not (spec["min"] <= parsed <= spec["max"]):
            raise ValueError(f"Nilai {k} harus di antara {spec['min']} dan {spec['max']}.")
    elif ptype == "bool":
        v = raw_value.strip().lower()
        if v in {"true", "1", "yes", "on"}:
            parsed = True
        elif v in {"false", "0", "no", "off"}:
            parsed = False
        else:
            raise ValueError(f"Nilai {k} harus true atau false.")
    else:
        parsed = raw_value

    cfg = load_strategy_config(config_path)
    cfg[k] = parsed
    cfg["updated_by"] = updated_by
    cfg["updated_at"] = datetime.now(timezone.utc).isoformat()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


def reset_strategy_config(
    updated_by: str = "telegram",
    config_path: Path = STRATEGY_CONFIG_FILE,
) -> dict[str, Any]:
    cfg = dict(DEFAULT_STRATEGY_CONFIG)
    cfg["updated_by"] = updated_by
    cfg["updated_at"] = datetime.now(timezone.utc).isoformat()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


# =============================================================================
# PAKET STRATEGI & PROFIL STRATEGI PER-USER (UNTUK END USER / PLANNER / ADMIN)
# =============================================================================

USER_STRATEGIES_FILE = Path("logs") / "user_strategies.json"

STRATEGY_PACKS: dict[str, dict[str, Any]] = {
    "true_funding_carry": {
        "name": "True Funding Carry (Default Terbaik)",
        "badge": "⭐",
        "description": "Asymmetric R:R (TP +2.0%/+2.5%, SL 1.5%), True Zero-Risk Trailing, Cooldown 3 jam.",
        "params": {
            "default_sl_pct": 0.015,
            "tp_calm_pct": 0.020,
            "tp_volatile_pct": 0.025,
            "min_funding_daily": 0.005,
            "reentry_cooldown_hours": 3.0,
            "max_slots_cap": 15,
        },
    },
    "conservative_income": {
        "name": "Conservative Safe Carry",
        "badge": "🛡️",
        "description": "Risiko ketat (SL 1.2%), seleksi funding lebih tinggi (>=0.60%/hr), cooldown 4 jam, max 5 slot.",
        "params": {
            "default_sl_pct": 0.012,
            "tp_calm_pct": 0.018,
            "tp_volatile_pct": 0.020,
            "min_funding_daily": 0.006,
            "reentry_cooldown_hours": 4.0,
            "max_slots_cap": 5,
        },
    },
    "aggressive_carry": {
        "name": "Aggressive High-Yield Carry",
        "badge": "🚀",
        "description": "Target TP lebar (+2.8%/+3.5%), SL 2.0%, ambang funding 0.40%/hari, cooldown 2 jam.",
        "params": {
            "default_sl_pct": 0.020,
            "tp_calm_pct": 0.028,
            "tp_volatile_pct": 0.035,
            "min_funding_daily": 0.004,
            "reentry_cooldown_hours": 2.0,
            "max_slots_cap": 10,
        },
    },
    "scalp_high_funding": {
        "name": "High-Funding Momentum",
        "badge": "⚡",
        "description": "Hanya masuk saat funding ekstrem >=0.80%/hari dengan TP cepat +1.6%/+2.0% & SL 1.2%.",
        "params": {
            "default_sl_pct": 0.012,
            "tp_calm_pct": 0.016,
            "tp_volatile_pct": 0.020,
            "min_funding_daily": 0.008,
            "reentry_cooldown_hours": 2.0,
            "max_slots_cap": 8,
        },
    },
}


def _parse_strategy_value(key: str, raw_value: str) -> tuple[str, Any]:
    k = key.strip().lower()
    if k not in STRATEGY_PARAM_SPECS:
        valid = ", ".join(STRATEGY_PARAM_SPECS.keys())
        raise ValueError(f"Parameter '{key}' tidak dikenal. Pilihan valid: {valid}")
    spec = STRATEGY_PARAM_SPECS[k]
    ptype = spec["type"]
    if ptype == "float":
        val_str = raw_value.strip().rstrip("%")
        val = float(val_str)
        if raw_value.strip().endswith("%"):
            val = val / 100.0
        if not (spec["min"] <= val <= spec["max"]):
            raise ValueError(f"Nilai {k} harus di antara {spec['min']} dan {spec['max']}.")
        return k, round(val, 6)
    if ptype == "int":
        parsed = int(float(raw_value.strip()))
        if not (spec["min"] <= parsed <= spec["max"]):
            raise ValueError(f"Nilai {k} harus di antara {spec['min']} dan {spec['max']}.")
        return k, parsed
    if ptype == "bool":
        v = raw_value.strip().lower()
        if v in {"true", "1", "yes", "on"}:
            return k, True
        if v in {"false", "0", "no", "off"}:
            return k, False
        raise ValueError(f"Nilai {k} harus true atau false.")
    return k, raw_value


def _load_all_user_strategies(path: Path | None = None) -> dict[str, Any]:
    target = path or USER_STRATEGIES_FILE
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_all_user_strategies(data: dict[str, Any], path: Path | None = None) -> None:
    target = path or USER_STRATEGIES_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_user_strategy_profile(
    telegram_id: int,
    path: Path | None = None,
) -> dict[str, Any]:
    all_profiles = _load_all_user_strategies(path)
    prof = all_profiles.get(str(telegram_id))
    if isinstance(prof, dict):
        return {
            "use_default": bool(prof.get("use_default", True)),
            "enabled_packs": list(prof.get("enabled_packs") or ["true_funding_carry"]),
            "active_pack": prof.get("active_pack") or "true_funding_carry",
            "custom_params": dict(prof.get("custom_params") or {}),
            "updated_at": prof.get("updated_at"),
        }
    return {
        "use_default": True,
        "enabled_packs": ["true_funding_carry"],
        "active_pack": "true_funding_carry",
        "custom_params": {},
        "updated_at": None,
    }


def toggle_user_pack(
    telegram_id: int,
    pack_id: str,
    enable: bool | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Aktifkan (ON) atau nonaktifkan (OFF) paket strategi untuk user tertentu."""
    pid = pack_id.strip().lower()
    if pid not in STRATEGY_PACKS:
        raise ValueError(f"Paket strategi '{pack_id}' tidak dikenal.")

    all_profiles = _load_all_user_strategies(path)
    prof = get_user_strategy_profile(telegram_id, path=path)
    enabled = set(prof.get("enabled_packs") or [])

    target_on = (pid not in enabled) if enable is None else bool(enable)
    if target_on:
        enabled.add(pid)
        prof["active_pack"] = pid
        prof["use_default"] = (pid == "true_funding_carry" and not prof.get("custom_params"))
    else:
        enabled.discard(pid)
        if not enabled:
            # Jika semua paket dimatikan, otomatis fallback ke strategi default terbaik
            enabled.add("true_funding_carry")
            prof["active_pack"] = "true_funding_carry"
            prof["use_default"] = True
        elif prof.get("active_pack") == pid:
            next_pack = sorted(enabled)[0]
            prof["active_pack"] = next_pack
            prof["use_default"] = (next_pack == "true_funding_carry" and not prof.get("custom_params"))

    prof["enabled_packs"] = sorted(enabled)
    prof["updated_at"] = datetime.now(timezone.utc).isoformat()
    all_profiles[str(telegram_id)] = prof
    _save_all_user_strategies(all_profiles, path=path)
    return prof


def set_user_default_mode(
    telegram_id: int,
    path: Path | None = None,
) -> dict[str, Any]:
    """Kembalikan profil user ke Strategi Default Terbaik (True Funding Carry)."""
    all_profiles = _load_all_user_strategies(path)
    prof = {
        "use_default": True,
        "enabled_packs": ["true_funding_carry"],
        "active_pack": "true_funding_carry",
        "custom_params": {},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    all_profiles[str(telegram_id)] = prof
    _save_all_user_strategies(all_profiles, path=path)
    return prof


def update_user_custom_param(
    telegram_id: int,
    key: str,
    raw_value: str,
    path: Path | None = None,
) -> dict[str, Any]:
    """Izinkan user (termasuk End User) mengatur parameter strategi pribadinya dalam batas aman."""
    k, parsed = _parse_strategy_value(key, raw_value)
    all_profiles = _load_all_user_strategies(path)
    prof = get_user_strategy_profile(telegram_id, path=path)
    custom = dict(prof.get("custom_params") or {})
    custom[k] = parsed
    prof["custom_params"] = custom
    prof["use_default"] = False
    prof["updated_at"] = datetime.now(timezone.utc).isoformat()
    all_profiles[str(telegram_id)] = prof
    _save_all_user_strategies(all_profiles, path=path)
    return prof


def get_effective_user_strategy(
    telegram_id: int,
    user_path: Path | None = None,
    global_path: Path | None = None,
) -> dict[str, Any]:
    """Gabungkan Default / Paket Aktif / Custom Override milik user."""
    base = load_strategy_config(global_path or STRATEGY_CONFIG_FILE)
    prof = get_user_strategy_profile(telegram_id, path=user_path)
    if prof["use_default"] and prof["active_pack"] == "true_funding_carry" and not prof["custom_params"]:
        return {
            **base,
            "mode_label": "Default Terbaik (True Funding Carry)",
            "active_pack": "true_funding_carry",
            "enabled_packs": prof["enabled_packs"],
            "custom_params": {},
        }
    pack_id = prof.get("active_pack") or "true_funding_carry"
    pack = STRATEGY_PACKS.get(pack_id, STRATEGY_PACKS["true_funding_carry"])
    merged = {**base, **pack["params"], **prof.get("custom_params", {})}
    mode = f"Paket {pack['name']}" if not prof.get("custom_params") else f"Custom ({pack['name']})"
    return {
        **merged,
        "mode_label": mode,
        "active_pack": pack_id,
        "enabled_packs": prof["enabled_packs"],
        "custom_params": prof.get("custom_params", {}),
    }
