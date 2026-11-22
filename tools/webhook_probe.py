"""webhook_probe - cek langsung deployment Apps Script (read-only, tanpa OAuth).

Ini menjawab pertanyaan "sudah benar belum redeploy-nya" dengan bukti, bukan dengan perasaan.
Tiga hal yang dibaca:

  1. GET <webhook>?probe=1  -> harus JSON berisi `version` + header tiap tab (kode repo v2).
     Kalau yang balik teks 42 byte "Walt Perps Sheet Logger Webhook is active!", berarti
     deployment aktif BUKAN kode repo (versi lama masih melayani /exec).
  2. GET <webhook> tanpa probe -> jalur baca riset (doGet normal).
  3. POST opsional (--post-probe) -> menulis satu baris PENANDAN ke tab uji yang disebutkan,
     supaya bisa dibuktikan bahwa tulis benar-benar mendarat. DEFAULT MATI: tidak ada
     penulisan kecuali Anda mengetik benderanya sendiri.

URL dibaca dari environment (GOOGLE_SHEET_WEBHOOK_URL) atau .env, dan TIDAK PERNAH dicetak -
URL webhook adalah kredensial tulis.

Pemakaian:
    python tools/webhook_probe.py
    python tools/webhook_probe.py --expect-version 2026-11-21.1
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sheet_logger  # noqa: E402  (memakai _normalize_webhook_url yang sama dengan jalur produksi)

STUB_GREETING = "walt perps sheet logger webhook is active!"


def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in io.open(env_file, encoding="utf-8"):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip())
        if match:
            os.environ.setdefault(match.group(1), match.group(2))


def describe(label: str, response: httpx.Response) -> dict:
    body = response.text
    content_type = response.headers.get("content-type", "")
    print(f"--- {label}: HTTP {response.status_code} | content-type={content_type or '-'} "
          f"| panjang={len(body)}")
    parsed = None
    if body.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
    if parsed is None:
        print(f"    BUKAN JSON. cuplikan: {body[:160]!r}")
        if STUB_GREETING in body.lower():
            print("    => ini SALAH SATU stub lama: deployment aktif bukan kode repo v2.")
            print("       Fix: Apps Script -> Deploy -> Manage deployments -> Edit -> New version,")
            print("       paste ulang google_apps_script.js dari repo, Who has access = Anyone.")
    else:
        compact = json.dumps(parsed, ensure_ascii=False)[:700]
        print(f"    JSON ok: {compact}")
    return {"json": parsed, "text": body, "status": response.status_code}


MG_COLUMNS = ("mg_status", "mg_net_gravity", "mg_regime", "mg_trend_bias", "mg_bull_pressure")


def mg_fill_report(payload: dict | None) -> tuple[int, dict[str, int]]:
    """Berapa aset terbaru yang benar-benar PUNYA nilai di kolom diagnostik Market Gravity.

    Dipisah dari main() supaya teruji tanpa jaringan: masukannya persis bentuk respons
    doGet(sheet=Daily_Scans), keluarannya (jumlah_aset, {kolom: jumlah_terisi}). Ini menjawab
    pertanyaan yang TIDAK bisa dijawab oleh cek versi: deployment v2 bisa hijau sambil kolom
    mg_* kosong, dan kolom kosong membuat P8 menjadi ornamen yang diam (AUDIT 3p.4).
    """
    data = (payload or {}).get("data") or {}
    filled = {col: 0 for col in MG_COLUMNS}
    for record in data.values():
        if not isinstance(record, dict):
            continue
        for col in MG_COLUMNS:
            text = str(record.get(col) if record.get(col) is not None else "").strip()
            if text and text.lower() not in ("-", "nan", "none", "null"):
                filled[col] += 1
    return len(data), filled


def main() -> int:
    parser = argparse.ArgumentParser(description="Verifikasi deployment Apps Script (read-only).")
    parser.add_argument("--expect-version", default="", help="mis. 2026-11-21.1")
    parser.add_argument("--post-probe", action="store_true",
                        help="TULIS satu baris penanda ke tab uji (default: tidak menulis apa pun)")
    parser.add_argument("--sheet", default="Webhook_Probe", help="tab tujuan untuk --post-probe")
    parser.add_argument("--require-mg", action="store_true",
                        help="vonis BELUM BERSIH jika kolom mg_* ada tetapi kosong di semua aset "
                             "(default: hanya info, karena P8 sengaja belum dinyalakan)")
    args = parser.parse_args()

    load_env()
    raw_url = (os.environ.get("GOOGLE_SHEET_WEBHOOK_URL") or "").strip().strip('"')
    # Normalisasi lewat sheet_logger: secret kadang berisi ID `AKfycb...` atau URL tanpa
    # skema. Membaca mentah membuat httpx melempar UnsupportedProtocol dalam ~125ms dan
    # hasilnya tidak terbaca sama sekali - itu yang terjadi pada run #44888952461.
    url = sheet_logger._normalize_webhook_url(raw_url) if raw_url else ""
    if not url:
        print("GOOGLE_SHEET_WEBHOOK_URL tidak ada di environment maupun .env - tidak ada yang dicek.")
        print("Set dulu (jangan di-commit): GOOGLE_SHEET_WEBHOOK_URL=<URL /exec dari Apps Script deployment>")
        return 1
    print("URL webhook terbaca dari environment/.env (disamakan dengan normalisasi jalur produksi)")
    print(f"host yang dipakai = {httpx.URL(url).host}")

    probe_url = url + ("&probe=1" if "?" in url else "?probe=1")
    try:
        with httpx.Client(timeout=25.0, follow_redirects=True) as client:
            probe = describe("GET ?probe=1", client.get(probe_url))
            plain = describe("GET tanpa probe", client.get(url))
    except httpx.HTTPError as exc:
        print(f"GET webhook GAGAL: {type(exc).__name__}: {str(exc)[:180]}")
        print("Ini masalah jaringan/protokol, BUKAN bukti deployment salah. Ulangi, atau cek")
        print("URL dari Apps Script -> Deploy -> Manage deployments (harus diakhiri /exec).")
        return 2

    verdict = []
    payload = probe["json"] if isinstance(probe["json"], dict) else None
    version = (payload or {}).get("version") or (payload or {}).get("scriptVersion") or ""
    if payload is None:
        verdict.append("doGet(probe) TIDAK mengembalikan JSON -> deployment aktif bukan kode repo")
    else:
        print(f"    versi deployment terdeteksi = {version or '(field version tidak ada)'}")
        if args.expect_version and version != args.expect_version:
            verdict.append(f"versi {version!r} != yang diharapkan {args.expect_version!r}")
        if probe["json"].get("probe") is not True:
            verdict.append("respons tidak menandai probe:true - mungkin cabang doGet lain yang kena")
    if plain["json"] is None:
        verdict.append("doGet(normal) juga non-JSON -> jalur baca riset webhook tetap tidak bisa dipakai "
                       "(gviz fallback yang menyelamatkan)")

    # Bukti yang BUKAN berasal dari cek versi: kolom diagnostik harus berisi, bukan sekadar ada.
    n_assets, filled = mg_fill_report(plain["json"] if isinstance(plain["json"], dict) else None)
    if n_assets:
        detail = ", ".join(f"{col}={filled[col]}/{n_assets}" for col in MG_COLUMNS)
        print(f"    isi mg_* pada {n_assets} aset terbaru: {detail}")
        if all(filled[col] == 0 for col in MG_COLUMNS):
            if args.require_mg:
                verdict.append("kolom mg_* ADA tetapi KOSONG di semua aset terbaru -> siklus tulis "
                               "belum menulis Market Gravity; prasyarat P8 (a) belum terpenuhi")
            else:
                print("    (info, tidak memvonis) mg_* masih kosong -> P8 tetap ornamen; "
                      "tambahkan --require-mg bila ingin menegakkannya")
    elif isinstance(plain["json"], dict):
        print("    (info) respons tidak memuat field data -> fill-rate mg_* TIDAK TERUKUR")

    if args.post_probe:
        body = {"sheet": args.sheet, "header": ["timestamp_utc", "marker"],
                "records": [{"timestamp_utc": "PROBE", "marker": "webhook_probe_cli"}]}
        written = describe("POST penanda (MENULIS)", client.post(url, json=body))
        if written["json"] is None:
            verdict.append("POST juga non-JSON -> doPost belum definitely pulih")

    print()
    if verdict:
        print("VERDIK: BELUM BERSIH")
        for line in verdict:
            print(f"  - {line}")
        return 2
    print("VERDIK: BERSIH - doGet probe JSON, versi cocok, deployment = kode repo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
