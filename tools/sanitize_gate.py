"""Gerbang sanitasi untuk repo PUBLIK: tidak ada kredensial/sidik jari data yang boleh ikut ter-commit.

Ditulis permanen (bukan skrip sekali pakai di `logs/`) karena pemeriksaan ini berulang dipakai tiap
ronde dan selama ini tidak ditegakkan di CI — jadi ia bisa basi tanpa ada yang tahu.

Cakupan: SELURUH berkas yang dilacak git (bukan daftar manual yang mudah terlupakan), dengan
placeholder yang sah (domain RFC 2606, `your-deployment-id`) diizinkan.

Detektor menguji dirinya sendiri lewat canary: bila satu kelas pola berhenti berfungsi (mis. regex
dirusak), gerbang gagal meski repo bersih — pencegahan "hijau karena buta".
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from typing import Optional

# Nilai yang DIHARAMKAN muncul apa adanya di berkas terlacak. Nilainya diambil dari .env saat run,
# sehingga rotasi kredensial tidak membutuhkan perubahan kode.
BENIGN_ENV = {
    "DASHSCOPE_MODEL", "DASHSCOPE_MODEL_V1", "DASHSCOPE_MODEL_AGG", "GEMINI_MODEL", "GEMINI_MODEL_V4",
    "DRY_RUN", "AUTO_EXECUTE_ON_SCAN", "LOG_REDACT", "MAINTENANCE_MODE", "WALT_COLD_REHYDRATE",
    "WALT_TOKEN_SYNC", "WALT_WATCHDOG_DISPATCH_ENTRY", "STRATEGY_CONFIG_JSON", "PYTHON_VERSION",
}
KEY_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PLACEHOLDER_VALUES = {"dummy", "false", "true", "none", "changeme", "xxx", "-"}

PATTERNS = {
    "sheet_id": re.compile(r"1ag6Mq[0-9A-Za-z_\-]{20,}"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    "dashscope_key": re.compile(r"\bsk-[0-9A-Za-z_.\-]{16,}"),
    "github_token": re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[0-9A-Za-z_]{16,}"),
    "telegram_bot": re.compile(r"\bbot?\d{6,}:[0-9A-Za-z_\-]{20,}"),
    "apps_script_deployment": re.compile(r"script\.google\.com/macros/s/[0-9A-Za-z_\-]{16,}"),
    "private_key": re.compile(r"BEGIN (?:RSA )?PRIVATE KEY"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"),
    "cjk_surrogate": re.compile(r"[\u3000-\u30ff\u3400-\u9fff\uff00-\uffef]"),
    "account_balance": re.compile(r"\$[1-9]\d{1,3}\.\d{2}\b"),
    "session_token_param": re.compile(r"(?:crumb|user_content_key)=(?![Nn]one|%s)[A-Za-z0-9%/+\-_]{6,}"),
}

ALLOW_EXACT = {
    "apps_script_deployment": {"script.google.com/macros/s/your-deployment-id"},
    "account_balance": {"$100.00", "$0.00"},
}
# Kelas yang diabaikan per cakupan berkas, dengan alasan (BUKAN untuk menutupi temuan):
#   account_balance — PRD/STRATEGY dan fixture test memang memuat angka contoh margin/TP-SL.
#   Yang dilarang adalah angka SALDO ASLI yang tersalin dari log run — itu hanya bisa muncul di
#   AUDIT.md / README.md, dan dua berkas itu TETAP diperiksa penuh untuk kelas ini.
CLASS_SKIP = {
    "account_balance": {"PRD.md", "STRATEGY.md"},
}
CLASS_SKIP_PREFIX = {"account_balance": ("tests/",)}
ALLOW_DOMAIN = re.compile(r"@(?:example\.(?:com|net|org)|localhost)$")

# Canary disusun dari POTONGAN agar berkas ini sendiri tidak pernah memuat string yang cocok dengan
# detektornya (kalau tidak, gerbang selalu gagal pada dirinya sendiri — dan godaan "kecualikan
# berkas pemeriksa" adalah awal berakhirnya sebuah gerbang sanitasi).
_CANARY_PIECES = (
    "1ag6Mq" + "CanaryNilaiSintetisBukanIDAsli00000",
    "AIzaSy" + "CanaryDUMMYDUMMYDUMMY12345",
    "crumb=" + "D%2FcanaryCrumbValue",
    "user_content_key=" + "CanaryUserContentKeyValue",
    "sk-" + "canaryAbcdefghij1234567890",
    "ghp_" + "CanaryNotARealTokenValue1234",
    "bot" + "123456789:CanaryBotTokenValueNotReal",
    "canary" + "@reais.com",
    chr(0x65E5) + chr(0x672C),                      # dua karakter CJK, tanpa menuliskannya langsung
    "$" + "15.26",
    "BEGIN RSA " + "PRIVATE KEY",
    "script.google.com/macros/s/" + "CanaryDeploymentIdNotReal123456",
)
SELF_TEST_CANARY = "|".join(_CANARY_PIECES)
SELF_TEST_MIN_CLASSES = 10


def _is_allowed(name: str, value: str) -> bool:
    if value in ALLOW_EXACT.get(name, ()):
        return True
    return name == "email" and bool(ALLOW_DOMAIN.search(value))


def _forbidden_values() -> dict[str, str]:
    """Nilai mentah dari .env (kredensial aktif) — tidak pernah dicetak, hanya dicocokkan."""
    out: dict[str, str] = {}
    for path in [".env"] + [p for p in os.listdir(".") if p.endswith(".env")]:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for raw in fh:
                if raw.lstrip().startswith("#"):
                    continue
                key, sep, val = raw.partition("=")
                if not sep:
                    continue
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if (KEY_RX.match(key) and key not in BENIGN_ENV and len(val) >= 12
                        and val.lower() not in PLACEHOLDER_VALUES):
                    out[key] = val
    return out


def _tracked_files() -> Optional[list[str]]:
    try:
        res = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    except Exception:
        return None
    return [p for p in res.stdout.splitlines() if p and os.path.isfile(p)]


def _skipped(name: str, rel: str) -> bool:
    if rel in CLASS_SKIP.get(name, ()):
        return True
    return any(rel.startswith(p) for p in CLASS_SKIP_PREFIX.get(name, ()))


def _first_line(text: str, rx: re.Pattern) -> int:
    for i, line in enumerate(text.splitlines(), 1):
        if rx.search(line):
            return i
    return 0


def scan_files(paths: list[str], forbidden: dict[str, str]) -> list[str]:
    """Temuan TIDAK pernah memuat nilai yang cocok — melaporkan isi secret = menyebarkannya."""
    problems: list[str] = []
    for rel in paths:
        try:
            with open(rel, encoding="utf-8") as fh:
                text = fh.read()
        except (UnicodeDecodeError, OSError):
            continue  # biner / tidak terbaca: bukan teks, tidak diperiksa
        for name, rx in PATTERNS.items():
            if _skipped(name, rel):
                continue
            hits = [m for m in rx.findall(text) if not _is_allowed(name, m)]
            if hits:
                problems.append(f"{rel}:{_first_line(text, rx)}: {name} x{len(hits)}")
        for key, val in forbidden.items():
            if val in text:
                problems.append(f"{rel}:{_first_line(text, re.compile(re.escape(val)))}: nilai .env `{key}`")
    return sorted(problems)


def self_test() -> list[str]:
    found = {n for n, rx in PATTERNS.items() if rx.findall(SELF_TEST_CANARY)}
    errors = []
    if len(found) < SELF_TEST_MIN_CLASSES:
        errors.append(
            f"detektor buta: canary hanya memicu {len(found)}/{SELF_TEST_MIN_CLASSES} kelas: {sorted(found)}"
        )
    # Dirangkai dari potongan supaya berkas ini tidak memicu detektornya sendiri.
    bukan_placeholder = "budi" + "@" + "reais.com"
    placeholder_sah = "u" + "@" + "example.com"
    if _is_allowed("email", bukan_placeholder) or not _is_allowed("email", placeholder_sah):
        errors.append("allowlist email placeholder salah kerja")
    # Pengabaian per-berkas tidak boleh melebar ke berkas yang justru rawan saldo asli.
    for strict in ("AUDIT.md", "README.md"):
        if _skipped("account_balance", strict):
            errors.append(f"{strict} tidak boleh dikecualikan dari pemeriksaan saldo")
    if not _skipped("account_balance", "tests/test_ci_hardening.py"):
        errors.append("fixture test seharusnya dikecualikan dari kelas account_balance")
    if "AUDIT.md" in CLASS_SKIP.get("sheet_id", ()) or "sheet_id" in CLASS_SKIP:
        errors.append("kelas identitas data tidak boleh dikecualikan sama sekali")
    # Laporan tidak boleh pernah memuat potongan nilai yang dicocokkan. Probe memakai berkas
    # sementara berisi rahasia sintetis — bukan berkas ini, yang memang sengaja tidak cocok.
    rahasia = "AIzaSy" + "CanaryProbeTidakNyata1234567"
    with tempfile.TemporaryDirectory() as td:
        jalur = os.path.join(td, "p.txt")
        with open(jalur, "w", encoding="utf-8") as fh:
            fh.write("baris biasa\n" + rahasia + "\n")
        probe = scan_files([jalur], {"CANARY": rahasia})
    if not probe:
        errors.append("probe kebocoran laporan tidak terpicu (pemeriksaannya kosong)")
    elif any(rahasia in p for p in probe):
        errors.append("laporan gerbang membocorkan nilai yang dicocokkan")
    elif not any("CANARY" in p for p in probe):
        errors.append("laporan gerbang harus menyebut nama kunci, bukan nilainya")
    if scan_files([__file__], {}) and any("sanitize_gate" in p for p in scan_files([__file__], {})):
        errors.append("canary gerbang cocok dengan detektornya sendiri (berkas ini memicu temuan)")
    return errors


def main(argv: Optional[list[str]] = None) -> int:
    paths = _tracked_files()
    scope = "berkas terlacak git"
    if paths is None:
        scope = "berkas level root (git tidak tersedia)"
        paths = [p for p in sorted(os.listdir(".")) if os.path.isfile(p)]
    problems = scan_files(paths, _forbidden_values())
    detector_errors = self_test()
    print(f"cakupan            = {scope} ({len(paths)} berkas)")
    print(f"pola_dipantau      = {len(PATTERNS)} kelas")
    print(f"self_test_detektor = {'GAGAL: ' + '; '.join(detector_errors) if detector_errors else 'LULUS'}")
    print(f"BERKAS_BERMASALAH  = {problems if problems else 0}")
    return 1 if (problems or detector_errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
