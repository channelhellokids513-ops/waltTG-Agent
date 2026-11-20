#!/usr/bin/env python3
"""
Watchdog Ronde 6 [R6-3]: mengawasi KEEMPAT workflow trading, bukan hanya risk-monitor.

Kenapa perlu: daily-scan / market-execute / crypto-execute tidak punya cron GitHub — pemicunya
Google Cloud Scheduler eksternal. Kalau job Scheduler mati, bot berhenti membuka posisi dan
watchdog lama tetap hijau karena hanya memeriksa risk-monitor.

Ambang usia per workflow diambil dari tabel jadwal terdokumentasi (STRATEGY.md §7.1 dan
README §113-116), bukan angka karangan:

    workflow           cadence (dokumentasi)      ambang  alasan
    risk-monitor.yml   */10 menit (10m)           30m     nilai lama yang sudah terbukti, dipertahankan
    market-execute.yml 43 */2  (120m)            300m     2x cadence + grace 60m
    crypto-execute.yml 11 */2  (120m)            300m     2x cadence + grace 60m
    daily-scan.yml     17 0,12 (720m)            840m     1x cadence + grace 120m (slot berikut ±12h)

Hanya memakai API GitHub (`gh`). Tidak ada akses Walt API di sini.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SPECS: list[dict] = [
    {
        "workflow": "risk-monitor.yml",
        "label": "Walt Risk Monitor",
        "cadence_min": 10,
        "threshold_min": 30,
        "source": "STRATEGY §7.1 `*/10 * * * *`; README §116; ambang warisan 30 menit",
    },
    {
        "workflow": "market-execute.yml",
        "label": "Walt Market Execute",
        "cadence_min": 120,
        "threshold_min": 300,
        "source": "STRATEGY §7.1 `43 */2 * * *`; README §114; 2x cadence + grace 60m",
    },
    {
        "workflow": "crypto-execute.yml",
        "label": "Walt Crypto Execute",
        "cadence_min": 120,
        "threshold_min": 300,
        "source": "STRATEGY §7.1 `11 */2 * * *`; README §115; 2x cadence + grace 60m",
    },
    {
        "workflow": "daily-scan.yml",
        "label": "Daily Walt Perps Funding Scan",
        "cadence_min": 720,
        "threshold_min": 840,
        "source": "STRATEGY §7.1 `17 0,12 * * *`; README §113; cadence + grace 120m",
    },
]

ACTIVE_STATES = ("queued", "in_progress", "waiting", "pending")

# Keepalive (Ronde 7b): workflow entry menembak watchdog bila run terakhirnya lebih tua dari ini.
# 50 menit < kadens cron 60 menit yang "dijanjikan", jadi dispatch hanya terjadi saat cron menguap.
WATCHDOG_WORKFLOW = "watchdog.yml"
WATCHDOG_KEEPALIVE_MIN = 50.0

# Late binding: fungsi memakai RUNNER saat dipanggil (bukan saat modul di-import), sehingga test
# bisa menyuntik tanpa menyentuh subprocess global. Default argumen yang mengikat subprocess.run
# ketika definisi dibuat akan membuat stub test tidak efektif (dan sempat benar-benar memanggil gh).
RUNNER = subprocess.run


def _runner(run=None):
    return run if run is not None else RUNNER


def _gh_json(args: list[str], run=None) -> list[dict]:
    proc = _runner(run)(["gh", *args], capture_output=True, text=True, timeout=60)
    if getattr(proc, "returncode", 1) != 0:
        print(f"[warn] gh gagal ({' '.join(args)}): {(getattr(proc, 'stderr', '') or '').strip()[:160]}")
        return []
    try:
        data = json.loads(getattr(proc, "stdout", "") or "[]")
    except json.JSONDecodeError:
        print("[warn] keluaran gh bukan JSON")
        return []
    return data if isinstance(data, list) else []


def latest_success_age_minutes(workflow: str, now: datetime, run=None) -> float | None:
    """Usia (menit) run `success` terbaru untuk workflow; None bila belum pernah sukses."""
    runs = _gh_json(
        ["run", "list", "--workflow", workflow, "--status", "success", "--limit", "5",
         "--json", "createdAt,updatedAt,databaseId"],
        run=run,
    )
    ages: list[float] = []
    for r in runs:
        ts_str = r.get("updatedAt") or r.get("createdAt")
        if not ts_str:
            continue
        try:
            dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        except ValueError:
            continue
        ages.append(max(0.0, (now - dt).total_seconds() / 60.0))
    return min(ages) if ages else None


def has_active_run(workflow: str, run=None) -> bool:
    """Guard anti-antrean-ganda (dipertahankan dari watchdog lama, kini per workflow)."""
    runs = _gh_json(
        ["run", "list", "--workflow", workflow, "--limit", "10", "--json", "status,databaseId"],
        run=run,
    )
    return any(str(r.get("status", "")).lower() in ACTIVE_STATES for r in runs)


def evaluate(rows: list[dict]) -> list[dict]:
    for row in rows:
        age = row["age_min"]
        if age is None:
            row["verdict"] = "STALE (belum pernah sukses)"
        elif age <= row["threshold_min"]:
            row["verdict"] = "OK"
        else:
            row["verdict"] = f"STALE (+{age - row['threshold_min']:.0f}m)"
    return rows


def collect_rows(now: datetime, run=None) -> list[dict]:
    rows = []
    for spec in SPECS:
        rows.append(
            {
                "workflow": spec["workflow"],
                "label": spec["label"],
                "cadence_min": spec["cadence_min"],
                "threshold_min": spec["threshold_min"],
                "age_min": latest_success_age_minutes(spec["workflow"], now, run=run),
                "source": spec["source"],
            }
        )
    return evaluate(rows)


def render_table(rows: list[dict]) -> str:
    lines = [
        "## 🐕 Watchdog — keempat workflow trading",
        "",
        "| Workflow | Cadence (dok) | Usia run sukses terakhir | Ambang | Verdict |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        age = "belum pernah sukses" if r["age_min"] is None else f"{r['age_min']:.1f} mnt"
        lines.append(
            f"| `{r['workflow']}` | {r['cadence_min']} mnt | {age} | {r['threshold_min']} mnt | {r['verdict']} |"
        )
    lines.append("")
    lines.append("_Ambang diturunkan dari tabel jadwal terdokumentasi (STRATEGY §7.1 / README §113-117)._")
    return "\n".join(lines)


def send_alert(text: str) -> bool:
    """Satu alert teragregasi untuk SEMUA workflow yang stale (bukan satu per workflow)."""
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        print("[warn] kredensial Telegram tidak diset — alert watchdog tidak dikirim")
        return False
    if token.startswith("bot"):
        token = token[3:]
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        print(f"[warn] gagal kirim alert Telegram: {type(exc).__name__}: {exc}")
        return False


def build_alert_text(stale_rows: list[dict]) -> str:
    names = ", ".join(f"`{r['workflow']}`" for r in stale_rows)
    lines = [f"⚠️ *WATCHDOG: {len(stale_rows)} workflow trading tidak sukses tepat waktu*", ""]
    for r in stale_rows:
        age = "belum pernah sukses" if r["age_min"] is None else f"{r['age_min']:.0f} menit lalu"
        lines.append(
            f"• {r['label']} (`{r['workflow']}`): {age}; ambang {r['threshold_min']} mnt "
            f"(cadence {r['cadence_min']} mnt)"
        )
    lines.append("")
    lines.append(
        "Karena ketiga workflow entry dipicu Google Cloud Scheduler eksternal, cek juga: "
        "`gcloud scheduler jobs list --location=asia-southeast2`."
    )
    return "\n".join(lines)


def dispatch_enabled_for(workflow: str) -> bool:
    """
    risk-monitor: dipertahankan dari watchdog lama (sudah terbukti, dan hanya mengurus TP/SL).
    workflow entry: fallback dispatch MENULIS ORDER nyata, jadi defaultnya ALERT-ONLY. Owner bisa
    mengaktifkan fallback otomatis lewat variable repository `WALT_WATCHDOG_DISPATCH_ENTRY=true`.
    """
    if workflow == "risk-monitor.yml":
        return True
    return (os.environ.get("WALT_WATCHDOG_DISPATCH_ENTRY") or "").strip().lower() in ("1", "true", "yes", "on")


def dispatch_workflow(workflow: str, run=None) -> str:
    """Fallback dispatch satu workflow. Return status: dispatched / skipped_active / failed."""
    if has_active_run(workflow, run=run):
        print(f"SKIP fallback dispatch: `{workflow}` sudah queued/in_progress — hindari antrean ganda.")
        return "skipped_active"
    proc = _runner(run)(["gh", "workflow", "run", workflow], capture_output=True, text=True, timeout=60)
    if getattr(proc, "returncode", 1) == 0:
        print(f"Fallback dispatch `{workflow}` dipicu.")
        return "dispatched"
    print(f"[warn] dispatch `{workflow}` gagal: {(getattr(proc, 'stderr', '') or '').strip()[:160]}")
    return "failed"


def latest_run_age_minutes(workflow: str, now: datetime, run=None) -> float | None:
    """Usia run TERBARU apa pun statusnya (bukan hanya `success`).

    Dipakai untuk menentukan apakah watchdog masih HIDUP (sudah berjalan belum), bukan apakah
    ia hijau. Run yang gagal tapi baru saja jalan tetap bukti Kehidupan; run sukses berumur 7 jam
    bukan.
    """
    runs = _gh_json(
        ["run", "list", "--workflow", workflow, "--limit", "5", "--json", "createdAt,updatedAt,status"],
        run=run,
    )
    ages: list[float] = []
    for r in runs:
        ts_str = r.get("updatedAt") or r.get("createdAt")
        if not ts_str:
            continue
        try:
            dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        except ValueError:
            continue
        ages.append(max(0.0, (now - dt).total_seconds() / 60.0))
    return min(ages) if ages else None


def keep_watchdog_alive(now: datetime | None = None, run=None) -> int:
    """Dipanggil dari workflow entry untuk menjaga watchdog tetap berjalan.

    Kenapa perlu (terukur, bukan asumsi): README §281 sudah mencatat cron GitHub bisa telat 7-8 jam
    dan karena itu ENTRY ditembak Cloud Scheduler — tapi `watchdog.yml` sendiri masih bergantung pada
    cron itu. Pada 2026-11-20 run `schedule` terakhir tercatat 09:55Z: ~7 jam tanpa satu pun
    pemeriksaan. Cloud Scheduler pemilik berada di proyek GCP yang tidak bisa dijangkau dari repo
    ini (0 job di semua proyek yang bisa diakses), jadi pemicu cadangan yang benar-benar kita kendali
    = siklus workflow trading itu sendiri.

    Throttle memakai USIA run watchdog terakhir (bukan menit pada jam dinding), sehingga tetap benar
    bila kadens workflow berubah. Tidak pernah mengembalikan kode keluar != 0: kegagalan keepalive
    tidak boleh menjatuhkan siklus trading yang sedang berjalan.
    """
    now = now or datetime.now(timezone.utc)
    if (os.environ.get("MAINTENANCE_MODE") or "").strip().lower() == "true":
        print("MAINTENANCE_MODE=true — keepalive watchdog dilewati.")
        return 0
    if (os.environ.get("WATCHDOG_KEEPALIVE") or "true").strip().lower() in ("0", "false", "no", "off"):
        print("WATCHDOG_KEEPALIVE=false — keepalive watchdog dimatikan manual.")
        return 0

    usia = latest_run_age_minutes(WATCHDOG_WORKFLOW, now, run=run)
    if usia is not None and usia < WATCHDOG_KEEPALIVE_MIN:
        print(f"Watchdog terakhir berjalan {usia:.0f}m lalu (< {WATCHDOG_KEEPALIVE_MIN}m) — tidak perlu dispatch.")
        return 0

    alasan = "belum pernah ada run" if usia is None else f"terakhir {usia:.0f}m lalu"
    print(f"Watchdog {alasan} — dispatch agar jaring pengaman tetap hidup.")
    status = dispatch_workflow(WATCHDOG_WORKFLOW, run=run)
    if status != "dispatched":
        # Bukan kegagalan entry: dicatat agar terlihat di Step Summary / log.
        print(f"[warn] keepalive watchdog status={status} — periksa `actions: write` pada job ini.")
    return 0


def main(argv: list[str] | None = None) -> int:
    now = datetime.now(timezone.utc)

    # URUTAN PENTING (dibuktikan test ronde 4): jendela pemeliharaan keluar PALING AWAL,
    # sebelum alert maupun dispatch apa pun.
    if (os.environ.get("MAINTENANCE_MODE") or "").strip().lower() == "true":
        print("MAINTENANCE_MODE=true — watchdog melewati alert & fallback dispatch.")
        return 0

    rows = collect_rows(now)
    table = render_table(rows)
    print(table)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(table + "\n")
        except Exception as exc:
            print(f"[warn] gagal menulis Step Summary: {exc}")

    stale = [r for r in rows if r["verdict"] != "OK"]
    if not stale:
        print("SEMUA Workflow SEHAT — tidak ada alert, tidak ada dispatch.")
        return 0

    sent = send_alert(build_alert_text(stale))
    print(f"Alert Telegram {'terkirim' if sent else 'GAGAL'} untuk {len(stale)} workflow stale.")

    actions: dict[str, str] = {}
    for r in stale:
        wf = r["workflow"]
        if not dispatch_enabled_for(wf):
            print(
                f"ALERT-ONLY untuk `{wf}` (fallback dispatch entry dimatikan by default; "
                "set WALT_WATCHDOG_DISPATCH_ENTRY=true untuk mengaktifkan)."
            )
            actions[wf] = "alert_only"
            continue
        actions[wf] = dispatch_workflow(wf)
    print("Tindakan fallback: " + json.dumps(actions, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
