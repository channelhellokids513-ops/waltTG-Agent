"""slippage_probe - satu order nyata untuk mengukur slippage MURNI (bagian C yang gagal).

contract_economics membuktikan bahwa selisih fill-vs-keputusan tidak bisa dipakai:
jedanya median 1,52 jam. Satu-satunya jalan mengukur slippage adalah membandingkan
`latest_rate` yang kita catat DETIK SEBELUM eksekusi dengan harga fill yang dilaporkan
bursa, di kedua sisi (open dan close).

Ini MEMBUKA DAN MENUTUP SATU POSISI NYATA dengan ukuran terkecil yang bursa izinkan.
Karena itu pintunya dua lapis dan gagal-tertutup (fail-closed):

  1. env SLIPPAGE_PROBE_CONFIRM harus persis "saya-ersetuju-order-nyata";
  2. env MAINTENANCE_MODE harus "true" - supaya tidak ada jalur otomatis (scan/execute/
     monitor) yang ikut bermain dengan posisi probe;
  3. kalau sudah ada posisi terbuka apa pun -> BERHENTI (probe tidak menumpuk risiko);
  4. setiap langkah dibatasi retry; dan yang paling penting:
  5. di akhir, posisi HARUS terbukti tertutup. Bila tidak, alat ini mengirim alert
     Telegram dan keluar dengan kode NON-NOL. Tidak ada jalan keluar yang meninggalkan
     posisi tanpa pengawasan.

Ukuran clip = apa yang PRODUKSI pakai (resolve_order_margin + enforcement min_size_base),
jadi angka yang keluar berlaku untuk clip kita, bukan clip imajiner.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CONFIRM_PHRASE = "saya-ersetuju-order-nyata"
PROBE_ASSETS = ("BTC", "ETH")          # likuiditas terbesar = estimasi slippage TERBAIK
SETTLE_SECONDS = 6                     # beri bursa waktu menuliskan fill
MIN_AGE_SECONDS = 60                   # guard umur-minimum bursa sebelum close (pelajaran run 1)
MAX_ATTEMPTS = 3


def check_gates() -> list[str]:
    """Kembalikan daftar alasan penolakan; kosong = boleh jalan."""
    reasons: list[str] = []
    if os.environ.get("SLIPPAGE_PROBE_CONFIRM", "").strip() != CONFIRM_PHRASE:
        reasons.append("SLIPPAGE_PROBE_CONFIRM != 'saya-ersetuju-order-nyata'")
    if os.environ.get("SLIPPAGE_PROBE_REAL_ORDER", "").strip() != "YA_ORDER_NYATA":
        reasons.append("input real_order != 'YA_ORDER_NYATA' (pintu kedua untuk order nyata)")
    if os.environ.get("MAINTENANCE_MODE", "").strip().lower() != "true":
        reasons.append("MAINTENANCE_MODE harus 'true' (variabel repo) sebelum order nyata")
    if os.environ.get("DRY_RUN", "").strip().lower() == "true":
        reasons.append("DRY_RUN=true - tidak ada order yang bisa dibuat")
    return reasons


def open_positions(client: Any) -> list[dict]:
    import walt_bot
    return walt_bot._extract_positions(client.list_positions(statuses=["open"]))


def pick_contract(client: Any, asset_hint: str = "") -> Optional[dict]:
    payload = client.list_contracts()
    contracts = payload.get("contracts") if isinstance(payload, dict) else payload
    by_asset = {str(c.get("base_asset", "")).upper(): c for c in (contracts or []) if isinstance(c, dict)}
    wanted = [asset_hint.strip().upper()] if asset_hint.strip() else list(PROBE_ASSETS)
    for asset in wanted:
        contract = by_asset.get(asset)
        if contract:
            return contract
    return None


def fill_price_from(node: Any) -> Optional[float]:
    """Harga fill yang DILAPORKAN BURSA, bukan snapshot lokal.

    Presedensi mengikuti yang dipakai jalur close: fill price mengalahkan close price,
    dan keduanya dicari di sub-objek `order`/`position` sebelum top-level.
    """
    import walt_bot
    if not isinstance(node, dict):
        return None
    keys = (*walt_bot._CLOSE_FILL_PRICE_KEYS, *walt_bot._CLOSE_POSITION_PRICE_KEYS,
            "avg_entry_price", "price")
    for scope in (node.get("order"), node.get("position"), node):
        if not isinstance(scope, dict):
            continue
        for key in keys:
            value = walt_bot._parse_positive_finite_price(scope.get(key))
            if value is not None:
                return value
    return None


def exchange_fill_for_contract(client: Any, contract_id: int) -> tuple[Optional[float], float]:
    """(avg_entry_price, margin) posisi terbuka menurut bursa."""
    for pos in open_positions(client):
        if int(pos.get("contract_id") or -1) == contract_id:
            return fill_price_from(pos), float(str(pos.get("margin_quote") or pos.get("margin") or 0.0) or 0.0)
    return None, 0.0


def risk_summary(preview: Any) -> str:
    """Ringkas penolakan preview TANPA memotong nama risk check - detail 'open_posi…' yang
    terpotong pada run pertama membuat diagnosisnya tidak mungkin dilakukan."""
    if not isinstance(preview, dict):
        return str(preview)[:200]
    risk = preview.get("risk") or {}
    checks = risk.get("checks") or []
    parts = []
    for check in checks[:6]:
        if isinstance(check, dict):
            parts.append(f"{check.get('name')}={check.get('ok')}"
                         + (f"({check.get('message')})" if check.get("message") else ""))
    return (f"ok={preview.get('ok')} risk_ok={risk.get('ok')} checks=[{', '.join(parts)}] "
            f"token={bool(preview.get('confirmationToken'))} expires={preview.get('expiresAt')}")


def close_position(client: Any, contract_id: int) -> tuple[bool, Any, str]:
    """Preview + execute close, maksimal MAX_ATTEMPTS. Return (ok, respons, catatan)."""
    last = "tidak dicoba"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            preview = client.preview_close(contract_id)
            if not preview or not preview.get("ok"):
                last = f"attempt {attempt} preview menolak: {risk_summary(preview)}"
            else:
                result = client.execute(preview["confirmationToken"])
                if isinstance(result, dict) and result.get("ok") is not False and not result.get("error"):
                    return True, result, f"attempt {attempt}: {str(result)[:200]}"
                last = f"attempt {attempt} execute gagal: {str(result)[:180]}"
        except Exception as exc:
            last = f"attempt {attempt} exception: {type(exc).__name__}: {exc}"
        time.sleep(3)
    return False, None, last


def position_conflict(positions: list[dict], contract_id: int) -> list[str]:
    """Posisi terbuka pada KONTRAK yang akan diprobe - inilah yang benar-benar berbahaya.

    Penjaga lama menolak bila ada posisi terbuka apa pun. Itu aman tapi terlalu kasar:
    ia membuat probe tidak bisa dijalankan sama sekali selama akun memegang posisi live
    yang tidak berhubungan, dan godaannya jadi "tutup dulu posisi pemilik" - yang justru
    menambah risiko/biaya. Aturan baru: konflik hanya kalau kontrak probe sendiri sudah
    terbuka; posisi lain dilaporkan supaya bisa diaudit, tidak memblokir.
    """
    clash = []
    others = []
    for pos in positions:
        try:
            cid = int(pos.get("contract_id") or -1)
        except (TypeError, ValueError):
            continue
        label = f"{pos.get('contract_base_asset')}:{pos.get('side')}:margin={pos.get('margin_quote')}"
        if cid == contract_id:
            clash.append(label)
        else:
            others.append(label)
    return clash + [f"lain:{o}" for o in others]


def main() -> int:
    reasons = check_gates()
    if reasons:
        print("DITOLAK SEBELUM SENTUH BURSA (fail-closed):")
        for reason in reasons:
            print(f"  - {reason}")
        print("Tidak ada order yang dibuat.")
        return 1

    import walt_bot
    from walt_bot import WaltClient

    import argparse
    parser = argparse.ArgumentParser(description="Satu order nyata untuk mengukur slippage (write path).")
    parser.add_argument("--margin", type=float, default=walt_bot.DEFAULT_MARGIN,
                        help="margin USD per posisi; pakai untuk menguji ambang clip bursa (mis. 3.00)")
    parser.add_argument("--hold-seconds", type=int, default=120,
                        help="berapa lama memantau posisi tetap terbuka sebelum mencoba menutup")
    parser.add_argument("--allow-other-positions", action="store_true",
                        help="boleh memprobe selama kontrak yang diprobe sendiri tidak terbuka "
                             "(posisi live yang tidak berhubungan TIDAK pernah disentuh)")
    parser.add_argument("--asset", default=os.environ.get("PROBE_ASSET", ""),
                        help="kontrak yang diprobe (mis. XAG/ETH). "
                             "Default kosong = daftar PROBE_ASSETS. Pilih aset yang BERTAHAN di live "
                             "kalau tujuan utamanya mengukur slippage keluar.")
    parser.add_argument("--no-tpsl", action="store_true",
                        default=os.environ.get("PROBE_NO_TP_SL", "").strip() == "1",
                        help="buka posisi TANPA TP/SL (isolasi efek autoClose); bisa lewat "
                             "env PROBE_NO_TP_SL=1 untuk CI")
    parser.add_argument("--observe-survival", action="store_true",
                        help="pantau kelangsungan posisi sampai --hold-seconds lalu berhenti "
                             "(tidak mengukur slippage keluar; default = langsung close)")
    args = parser.parse_args()

    client = WaltClient()
    print("slippage_probe - SATU order nyata untuk mengukur slippage murni")
    print("=" * 88)

    contract = pick_contract(client, args.asset)
    if not contract:
        print(f"DITOLAK: kontrak {args.asset or 'BTC/ETH'} tidak ditemukan di daftar bursa.")
        return 1
    asset = str(contract["base_asset"]).upper()
    contract_id = int(contract["contract_id"])
    leverage = min(2, int(contract.get("max_leverage") or 2))

    # Konflik hanya dihitung pada KONTRAK yang akan diprobe. Posisi live pemilik yang tidak
    # berhubungan dilaporkan (audit) tapi tidak diblokir - dan probe TIDAK PERNAH menutupnya.
    conflicts = position_conflict(open_positions(client), contract_id)
    same_contract = [c for c in conflicts if not c.startswith("lain:")]
    others = [c for c in conflicts if c.startswith("lain:")]
    if same_contract:
        print(f"DITOLAK: {asset} sudah terbuka ({', '.join(same_contract)}). "
              "Probe tidak menumpuk posisi pada kontrak yang sama.")
        return 1
    if others:
        if not args.allow_other_positions:
            print(f"DITOLAK: {len(others)} posisi lain terbuka dan --allow-other-positions tidak diset:")
            for item in others:
                print(f"  - {item}")
            return 1
        print(f"Catatan audit: {len(others)} posisi lain sedang terbuka (tidak disentuh probe ini):")
        for item in others:
            print(f"  - {item}")

    opp = walt_bot.Opportunity(
        contract_id=contract_id, base_asset=asset,
        funding_daily=float(contract.get("funding_rate_hourly") or 0.0) * 24.0,
        volume_24h=float(contract.get("volume_24h_usd") or 0.0),
        max_leverage=leverage, side="long",
        min_size_base=float(contract.get("min_size_base") or 0.0),
        price_slippage=float(contract.get("price_slippage") or 0.0),
    )

    rate_before = float(client.latest_rate(f"{asset}-USD-PERP")["rate"])
    resolved = walt_bot.resolve_order_margin(client, opp, args.margin, leverage)
    if not resolved:
        print(f"DITOLAK: margin minimum bursa tidak terpenuhi bahkan dengan ${args.margin:.2f} "
              f"(clip ${args.margin * leverage:.2f} notional) - itu sendiri sudah jawaban P1.")
        return 1
    margin, _price = resolved
    print(f"Clip PRODUKSI: {asset} long lev {leverage} margin ${margin:.2f} "
          f"(diminta ${args.margin:.2f}, min_size_base={opp.min_size_base}, "
          f"rate sebelum open={rate_before:.6f})")

    if args.no_tpsl:
        # Pembeda terakhir: posisi dibuka TANPA autoClose. Kalau yang ini bertahan sementara
        # yang dengan TP/SL tertutup dalam <1 menit, maka pemasangan autoClose-lah pemicunya -
        # dan itu bug produksi yang menimpa SEMUA entry, bukan sekadar soal probe.
        print("Mode --no-tpsl: order dibuka TANPA TP/SL untuk mengisolasi efek autoClose.")
        preview = client.preview_order(contract_id, "long", leverage, margin, rate_before)
        if not preview or not preview.get("ok"):
            print(f"DITOLAK bursa sebelum execute: {risk_summary(preview)}")
            return 1
        if not walt_bot.has_enough_job_time(2.0):
            print("Sisa waktu job tidak cukup aman untuk execute - berhenti sebelum order.")
            return 1
        opened = client.execute(preview["confirmationToken"])
    else:
        opened = walt_bot.open_position(client, opp, margin, leverage, resolved=resolved)
    if not opened:
        print("GAGAL OPEN - dan tidak ada posisi yang terbentuk (open_position mengembalikan None).")
        return 1
    opened_at_wall = time.time()
    time.sleep(SETTLE_SECONDS)
    fill_in, _margin_seen = exchange_fill_for_contract(client, contract_id)
    if fill_in is None:
        fill_in = fill_price_from(opened)
    if fill_in is None:
        print("POSISI TERBUKA TETAPI FILL ENTRY TIDAK TERBACA - menutup dulu sebelum berhenti.")
        ok, _resp, note = close_position(client, contract_id)
        print(f"close_ok={ok} {note}")
        return 2 if ok else 3

    slip_in = (fill_in - rate_before) / rate_before * 100.0
    print(f"ENTRY: fill bursa={fill_in:.6f} vs rate sebelum execute={rate_before:.6f} "
          f"-> slippage masuk {slip_in:+.4f}%")

    # Run pertama: preview_close ditolak 3x dengan risk check terpotong. Riwayat akun pemilik
    # (screenshot 2026-11-21) membuktikan penyebabnya bukan guard umur: posisi tertutup OLEH
    # BURSA dalam ~1 menit, tanpa label "Take Profit executed"/"Stop Loss", PnL ~0,001 USD
    # (≈0,02% notional). Hipotesis yang diuji di bawah: clip $2,50 (notional $5) terlalu kecil
    # untuk dipertahankan bursa. Karena itu kita lacak kelangsungan hidup posisi per detik,
    # baru mencoba menutup sendiri.
    if args.observe_survival:
        survived = True
        for elapsed in range(20, max(60, int(args.hold_seconds)) + 1, 20):
            time.sleep(20)
            still = [p for p in open_positions(client) if int(p.get("contract_id") or -1) == contract_id]
            print(f"   +{elapsed}s: {'MASIH TERBUKA' if still else 'SUDAH TIDAK ADA (ditutup pihak lain)'}")
            if not still:
                survived = False
                break
        if not survived:
            print("Posisi hilang sebelum kita sempat menutup sendiri -> slippage keluar TIDAK TERUKUR.")
            print("Mode ini menjawab 'berapa lama clip bertahan', BUKAN 'berapa slippage keluar'.")
            return 5
    else:
        # Default: LANGSUNG close. Run 08:15Z membuktikan bahwa menunggu 90 detik membuat kita
        # kalah balapan dengan penutup tak terlihat, sehingga sisi keluar tidak pernah terukur.
        print("Langsung mencoba close (mode default; pakai --observe-survival untuk menguji "
              "kelangsungan posisi tanpa mengukur slippage keluar).")

    rate_before_close = float(client.latest_rate(f"{asset}-USD-PERP")["rate"])
    ok, close_result, note = close_position(client, contract_id)
    time.sleep(SETTLE_SECONDS)
    remaining = [p for p in open_positions(client) if int(p.get("contract_id") or -1) == contract_id]
    if not ok or remaining:
        print("⚠️ POSISI MUNGKIN MASIH TERBUKA - ini harus ditangani, bukan diabaikan.")
        print(f"   close_ok={ok} posisi_sisa={len(remaining)} {note}")
        try:
            walt_bot.send_telegram(f"🚨 SLIPPAGE PROBE: posisi {asset} id={contract_id} belum terbukti "
                                   f"tertutup (close_ok={ok}, sisa={len(remaining)}). CEK MANUAL. "
                                   f"{note[:120]}")
        except Exception as exc:
            print(f"   alert Telegram gagal dikirim: {exc}")
        return 4

    fill_out = fill_price_from(close_result)
    if fill_out is None:
        print("EXIT tertutup, tetapi harga fill exit TIDAK TERBACA dari respons.")
        print("Slippage keluar dinyatakan TIDAK TERUKUR - tidak diisi nol, tidak diestimasikan.")
        print(f"   potongan respons: {note[:220]}")
        print("=" * 88)
        print(f"Terukur: slippage MASUK {slip_in:+.4f}% ({slip_in * 100:+.1f} bps) pada clip ${margin:.2f}.")
        print("Kesimpulan hanya boleh memakai satu sisi, dan itu jujur: satu sampel, satu arah.")
        return 0

    slip_out = (rate_before_close - fill_out) / rate_before_close * 100.0
    round_trip = abs(slip_in) + abs(slip_out)
    fee_round_trip = 2 * walt_bot.TAKER_FEE_PER_SIDE * 100
    print(f"EXIT : fill bursa={fill_out:.6f} vs rate sebelum close={rate_before_close:.6f} "
          f"-> slippage keluar {slip_out:+.4f}%")
    print("=" * 88)
    print(f"Posisi terbukti tertutup. Tidak ada posisi tersisa di {asset} (id={contract_id}).")
    print(f"SLIPPAGE MURNI TERUKUR round-trip = {round_trip:.4f}%   (fee saja {fee_round_trip:.2f}%)")
    print(f"Biaya round-trip sesungguhnya ≈ {round_trip + fee_round_trip:.4f}%")
    venue = opp.price_slippage * 100.0
    print(f"Pembanding: konstanta slippage yang bursa nyatakan = {venue:.2f}% "
          f"({'TERLALU BESAR' if venue > round_trip * 1.5 else 'orde sama' if venue < round_trip * 2 else 'konservatif'} "
          "relatif terhadap hasil ukur nyata pada clip ini)")
    print("BATAS JUJUR: 1 sampel, clip terkecil, satu arah (long), satu momen pasar. "
          "Ini titik data pertama, bukan distribusi. Ulangi beberapa kali sebelum dipakai "
          "mengganti COSTS di simulasi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
