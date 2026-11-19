"""
Manajemen konfigurasi strategi dinamis (logs/strategy_config.json) dengan batasan aman.
Digunakan oleh walt_bot.py dan dapat diatur oleh Administrator / Planner lewat Bot Telegram.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STRATEGY_CONFIG_FILE = Path("logs") / "strategy_config.json"

DEFAULT_STRATEGY_CONFIG: dict[str, Any] = {
    "default_sl_pct": 0.015,        # 1.5%
    "tp_calm_pct": 0.020,           # 2.0% (ETF & Crypto)
    "tp_volatile_pct": 0.025,       # 2.5% (Komoditas & Saham Volatil)
    "min_funding_daily": 0.005,     # 0.50%/hari
    "reentry_cooldown_hours": 3.0,  # 3 jam
    "max_slots_cap": 15,            # batas atas slot elastis
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
    "dry_run": {"type": "bool", "desc": "Mode simulasi tanpa order riil (true/false)"},
}


def load_strategy_config(config_path: Path = STRATEGY_CONFIG_FILE) -> dict[str, Any]:
    """Muat konfigurasi strategi runtime dengan fallback aman ke nilai default."""
    cfg = dict(DEFAULT_STRATEGY_CONFIG)
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, spec in STRATEGY_PARAM_SPECS.items():
                    if k in raw:
                        cfg[k] = raw[k]
                if "updated_by" in raw:
                    cfg["updated_by"] = raw["updated_by"]
                if "updated_at" in raw:
                    cfg["updated_at"] = raw["updated_at"]
        except Exception:
            pass
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
