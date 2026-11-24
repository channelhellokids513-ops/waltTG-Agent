"""Pelacak kesiapan menuju target $100/bulan (read-only).

Menjawab satu pertanyaan: **apakah sistem masih di jalur menuju $100/bulan?**
Sumber: `Closed_Positions` + `PnL_Tracker` (Google Sheet via `sheet_logger`, fallback CSV lokal).
Tidak memanggil API Walt, tidak membuka/menutup posisi.

Kriteria lulus (sama dengan §7 laporan pengawasan 2026-11-25):
  1. >= 20 trade tertutup
  2. expectancy >= +$0.01/trade
  3. funding diterima >= 50% dari PnL bruto (bukti thesis carry, bukan arah harga)
  4. max drawdown ekuitas <= 5%
  5. median umur posisi >= 6 jam
  6. tidak ada SL dengan rugi > 3% margin

Exit code: 0 = semua lulus, 1 = ada yang gagal / data belum cukup.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MONTHLY_TARGET = 100.0
FEE_ROUNDTRIP = 0.0018      # taker 0.09% x 2 sisi
SLIPPAGE_PROXY = 0.0026     # slippage terukur (median) yang dipakai gerbang E5
CARRY_DAILY = 0.008         # asumsi carry bersih 0.8%/hari pada ambang funding baru 1.0%/hari
MAX_LEVERAGE = 2
CASH_BUFFER = 0.15

MIN_TRADES = 20
MIN_EXPECTANCY = 0.01
MIN_FUNDING_SHARE = 0.50
MAX_DRAWDOWN = 0.05
MIN_MEDIAN_HOLD_H = 6.0
MAX_SL_LOSS_FRAC = 0.03


def _num(value: Any) -> Optional[float]:
    text = str(value or "").strip().replace("$", "").replace("%", "")
    if not text:
        return None
    try:
        val = float(text)
    except ValueError:
        return None
    return val if val == val and abs(val) != float("inf") else None


def _dt(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def compute_metrics(trades: list[dict]) -> dict[str, Any]:
    """Statistik inti dari daftar trade (baris yang punya PnL)."""
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    net = sum(pnls)
    funding = sum(t.get("funding", 0.0) for t in trades)
    n = len(trades)
    win_rate = len(wins) / n if n else 0.0
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss) if n else 0.0

    holds = [
        (t["closed"] - t["opened"]).total_seconds() / 3600.0
        for t in trades
        if t.get("opened") and t.get("closed")
    ]
    sl_losses = [
        abs(t["pnl"]) / t["margin"]
        for t in trades
        if t.get("margin") and t["pnl"] < 0 and "sl" in str(t.get("reason", "")).lower()
    ]
    inflow = gross_win + funding
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "net": net,
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": (avg_win / avg_loss) if avg_loss else 0.0,
        "expectancy": expectancy,
        "funding": funding,
        "funding_share": (funding / inflow) if inflow > 0 else 0.0,
        "median_hold_h": statistics.median(holds) if holds else None,
        "hold_samples": len(holds),
        "worst_sl_loss_frac": max(sl_losses) if sl_losses else 0.0,
        "sl_loss_samples": len(sl_losses),
    }


def equity_series(pnl_rows: list[list[str]]) -> list[tuple[str, float]]:
    """Ambil (tanggal, ekuitas) dari PnL_Tracker; dukung header lama & baru."""
    if not pnl_rows:
        return []
    header = [c.strip().lower() for c in pnl_rows[0]]
    idx_date = header.index("tanggal") if "tanggal" in header else 0
    idx_eq = header.index("total ekuitas ($)") if "total ekuitas ($)" in header else 1
    out: list[tuple[str, float]] = []
    for row in pnl_rows[1:]:
        if len(row) <= max(idx_date, idx_eq):
            continue
        date_txt = str(row[idx_date]).strip()
        eq = _num(row[idx_eq])
        if date_txt and eq and eq > 0:
            out.append((date_txt, eq))
    return out


def max_drawdown(series: list[tuple[str, float]]) -> float:
    """Drawdown maksimum (fraksi) dari seri ekuitas berurutan."""
    peak = 0.0
    worst = 0.0
    for _date, eq in series:
        peak = max(peak, eq)
        if peak > 0:
            worst = max(worst, (peak - eq) / peak)
    return worst


def compute_capacity(equity: float, turns_per_day: float = 1.0) -> dict[str, Any]:
    """Kapasitas ekonomi pada ekuitas tertentu (leverage 2, buffer kas 15%)."""
    notional = max(0.0, equity) * (1 - CASH_BUFFER) * MAX_LEVERAGE
    net_daily_pct = CARRY_DAILY - (FEE_ROUNDTRIP + SLIPPAGE_PROXY) * turns_per_day
    monthly_net = notional * net_daily_pct * 30
    needed_notional = MONTHLY_TARGET / (30 * net_daily_pct) if net_daily_pct > 0 else float("inf")
    needed_equity = (
        needed_notional / ((1 - CASH_BUFFER) * MAX_LEVERAGE) if needed_notional else float("inf")
    )
    return {
        "notional": notional,
        "net_daily_pct": net_daily_pct,
        "monthly_net": monthly_net,
        "needed_notional": needed_notional,
        "needed_equity": needed_equity,
    }


def evaluate(
    metrics: dict[str, Any], drawdown: float, equity: float, turns_per_day: float = 1.0
) -> list[dict[str, Any]]:
    """Daftar gate §7 dengan status lulus/gagal."""
    hold = metrics.get("median_hold_h")
    gates = [
        {"name": "jumlah trade", "value": metrics["n"], "target": f">= {MIN_TRADES}",
         "ok": metrics["n"] >= MIN_TRADES},
        {"name": "expectancy/trade", "value": metrics["expectancy"],
         "target": f">= ${MIN_EXPECTANCY:.2f}", "ok": metrics["expectancy"] >= MIN_EXPECTANCY},
        {"name": "porsi funding", "value": metrics["funding_share"],
         "target": f">= {MIN_FUNDING_SHARE*100:.0f}%",
         "ok": metrics["funding_share"] >= MIN_FUNDING_SHARE},
        {"name": "max drawdown", "value": drawdown, "target": f"<= {MAX_DRAWDOWN*100:.0f}%",
         "ok": drawdown <= MAX_DRAWDOWN},
        {"name": "median hold", "value": hold, "target": f">= {MIN_MEDIAN_HOLD_H:.0f} jam",
         "ok": (hold or 0.0) >= MIN_MEDIAN_HOLD_H},
        {"name": "rugi SL terburuk", "value": metrics["worst_sl_loss_frac"],
         "target": f"<= {MAX_SL_LOSS_FRAC*100:.0f}% margin",
         "ok": metrics["worst_sl_loss_frac"] <= MAX_SL_LOSS_FRAC},
    ]
    cap = compute_capacity(equity, turns_per_day=turns_per_day)
    gates.append({
        "name": "kapasitas bulanan", "value": cap["monthly_net"],
        "target": f">= ${MONTHLY_TARGET:.0f}", "ok": cap["monthly_net"] >= MONTHLY_TARGET,
    })
    return gates


def parse_closed(rows: list[list[str]]) -> list[dict]:
    """Baris Closed_Positions -> trade ber-PnL. Baris tanpa PnL = belum terukur, dilewati."""
    if not rows:
        return []
    header = [c.strip() for c in rows[0]]

    def idx(name: str, default: int) -> int:
        return header.index(name) if name in header else default

    i_asset = idx("Asset", 1)
    i_entry = idx("Entry Price ($)", 4)
    i_exit = idx("Exit Price ($)", 5)
    i_margin = idx("Margin ($)", 6)
    i_pnl = idx("PnL ($)", 7)
    i_fund = idx("Funding ($)", 8)
    i_reason = idx("Alasan Close", 9)
    i_closed = idx("Closed At (exchange)", 11)
    i_opened = idx("Opened At (exchange)", 13)

    def cell(row: list[str], i: int) -> str:
        return str(row[i]).strip() if 0 <= i < len(row) else ""

    trades: list[dict] = []
    for row in rows[1:]:
        pnl = _num(cell(row, i_pnl))
        if pnl is None:
            continue
        trades.append({
            "asset": cell(row, i_asset),
            "margin": _num(cell(row, i_margin)) or 0.0,
            "pnl": pnl,
            "funding": _num(cell(row, i_fund)) or 0.0,
            "reason": cell(row, i_reason),
            "opened": _dt(cell(row, i_opened)),
            "closed": _dt(cell(row, i_closed)),
            "entry": _num(cell(row, i_entry)),
            "exit": _num(cell(row, i_exit)),
        })
    return trades


def _load(rows_source: Any) -> tuple[list[list[str]], list[list[str]]]:
    rows, status = rows_source.fetch_sheet_csv_rows_ex(
        "Closed_Positions", expected_col_name="Exit Price ($)",
        local_csv_fallback="logs/closed_history.csv",
    )
    pnl_rows, pnl_status = rows_source.fetch_sheet_csv_rows_ex(
        "PnL_Tracker", expected_col_name="Tanggal"
    )
    print(f"sumber ledger: Closed_Positions={status} ({max(0, len(rows)-1)} baris), "
          f"PnL_Tracker={pnl_status} ({max(0, len(pnl_rows)-1)} baris)")
    return rows, pnl_rows


def _load_sheet_id_from_env_file() -> None:
    """CLI lokal: ambil HANYA GOOGLE_SHEET_ID dari .env bila env belum diset.

    Tidak menyentuh kredensial Walt sama sekali — tool ini read-only terhadap sheet.
    """
    import os

    if (os.environ.get("GOOGLE_SHEET_ID") or "").strip():
        return
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        clean = line.strip()
        if clean.startswith("GOOGLE_SHEET_ID="):
            os.environ.setdefault("GOOGLE_SHEET_ID", clean.split("=", 1)[1].strip().strip('"').strip("'"))
            return


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Pelacak kesiapan target 100 USD/bulan (read-only).")
    parser.add_argument("--turns", type=float, default=1.0,
                        help="round-trip per hari untuk estimasi kapasitas (default 1 = hold ~24 jam)")
    args = parser.parse_args(argv)

    _load_sheet_id_from_env_file()
    import sheet_logger

    rows, pnl_rows = _load(sheet_logger)
    trades = parse_closed(rows)
    if not trades:
        print("BELUM ADA TRADE TERUKUR — jangan menyimpulkan apa pun dari angka nol.")
        return 1

    metrics = compute_metrics(trades)
    series = equity_series(pnl_rows)
    equity = series[-1][1] if series else 0.0
    dd = max_drawdown(series)

    print(f"\n=== KINERJA ({metrics['n']} trade ber-PnL) ===")
    print(f"win rate        : {metrics['win_rate']*100:.1f}%  "
          f"({metrics['wins']}W / {metrics['losses']}L)")
    print(f"net             : ${metrics['net']:+.4f}")
    print(f"avg win/loss    : ${metrics['avg_win']:.4f} / ${metrics['avg_loss']:.4f} "
          f"(payoff {metrics['payoff']:.2f})")
    print(f"expectancy      : ${metrics['expectancy']:+.4f}/trade")
    print(f"funding diterima: ${metrics['funding']:.4f} "
          f"({metrics['funding_share']*100:.1f}% dari inflow)")
    hold = metrics["median_hold_h"]
    print(f"median hold     : {'n/a' if hold is None else f'{hold:.2f} jam'} "
          f"({metrics['hold_samples']} sampel)")
    print(f"SL terburuk     : {metrics['worst_sl_loss_frac']*100:.2f}% margin "
          f"({metrics['sl_loss_samples']} sampel)")

    cap = compute_capacity(equity, turns_per_day=args.turns)
    print(f"\n=== KAPASITAS pada ekuitas ${equity:.2f} (turns/hari={args.turns}) ===")
    print(f"notional maks   : ${cap['notional']:.2f}")
    print(f"net yield/hari  : {cap['net_daily_pct']*100:.3f}%")
    print(f"proyeksi/bulan  : ${cap['monthly_net']:.2f}")
    print(f"butuh ekuitas   : ${cap['needed_equity']:.2f} untuk ${MONTHLY_TARGET:.0f}/bulan")

    gates = evaluate(metrics, dd, equity, turns_per_day=args.turns)
    print("\n=== GATE 7 ===")
    for g in gates:
        val = g["value"]
        shown = f"{val:.4f}" if isinstance(val, float) else str(val)
        print(f"[{'LULUS' if g['ok'] else 'GAGAL'}] {g['name']:<18} {shown:<10} "
              f"(syarat {g['target']})")

    failed = [g["name"] for g in gates if not g["ok"]]
    print("\nVERDICT:", "ON TRACK — semua gate lulus" if not failed
          else f"BELUM ON TRACK — {len(failed)} gate gagal: {', '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())