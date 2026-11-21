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


def main() -> int:
    parser = argparse.ArgumentParser(description="Verifikasi deployment Apps Script (read-only).")
    parser.add_argument("--expect-version", default="", help="mis. 2026-11-21.1")
    parser.add_argument("--post-probe", action="store_true",
                        help="TULIS satu baris penanda ke tab uji (default: tidak menulis apa pun)")
    parser.add_argument("--sheet", default="Webhook_Probe", help="tab tujuan untuk --post-probe")
    args = parser.parse_args()

    load_env()
    url = (os.environ.get("GOOGLE_SHEET_WEBHOOK_URL") or "").strip().strip('"')
    if not url:
        print("GOOGLE_SHEET_WEBHOOK_URL tidak ada di environment maupun .env - tidak ada yang dicek.")
        print("Set dulu (jangan di-commit): GOOGLE_SHEET_WEBHOOK_URL=<URL /exec dari Apps Script deployment>")
        return 1
    print("URL webhook terbaca dari environment/.env dan sengaja TIDAK ditampilkan.")

    probe_url = url + ("&probe=1" if "?" in url else "?probe=1")
    with httpx.Client(timeout=25.0, follow_redirects=True) as client:
        probe = describe("GET ?probe=1", client.get(probe_url))
        plain = describe("GET tanpa probe", client.get(url))

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
