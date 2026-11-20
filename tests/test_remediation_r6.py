"""Ronde 6 — Item 1: API bursa sebagai sumber kebenaran + kegagalan baca ledger harus berbunyi.

Semua test hermetis: tidak ada panggilan jaringan nyata (httpx di-monkeypatch), tidak ada
penulisan ke state produksi (fixture isolate_writes conftest + path tmp_path eksplisit).
"""
from __future__ import annotations

import csv
import io

import pytest

import sheet_logger
import walt_bot

# ------------------------------------------------------------------------------
# Fiksasi: 17 kunci persis seperti respons list_positions(statuses=["closed"])
# yang terverifikasi CI (butir 0.4 prompt Ronde 6). Tidak ada field harga keluar.
# ------------------------------------------------------------------------------
CLOSED_KEYS = [
    "auto_close", "avg_entry_price", "contract_base_asset", "contract_id", "contract_name",
    "contract_quote_asset", "created_at", "funding_paid_quote", "leverage", "liquidation_price",
    "margin_quote", "margin_type", "side", "status", "synced_at", "updated_at", "value_base",
]


def _closed_payload(n: int = 3) -> list[dict]:
    rows = []
    for i in range(n):
        row = {k: None for k in CLOSED_KEYS}
        row.update({
            "avg_entry_price": f"{100.0 + i * 7:.6f}",
            "contract_base_asset": ["NVDA", "BTC", "ETH"][i % 3],
            "contract_id": 500 + i,
            "contract_name": f"{'NVDA BTC ETH'.split()[i % 3]}-USD-PERP",
            "created_at": f"2026-11-1{i}T07:00:00Z",
            "updated_at": f"2026-11-1{i}T09:00:00Z",
            "funding_paid_quote": f"{0.5 + i:.6f}",
            "leverage": 5,
            "margin_quote": "2.50",
            "side": ["long", "short"][i % 2],
            "status": "closed",
            "value_base": f"{0.1 * (i + 1):.6f}",
        })
        rows.append(row)
    return rows


class FakeClient:
    """Klient bursa tiruan: hanya read-only yang tersedia (menegakkan aturan ronde)."""

    def __init__(self, closed: list[dict], tools: list[dict] | None = None):
        self._closed = closed
        self._tools = tools or [
            {"name": "perps_list_positions", "inputSchema": {"properties": {"statuses": {"type": "array"}}}},
        ]
        self.calls: list[str] = []

    def list_positions(self, statuses=None):
        self.calls.append(f"list_positions:{tuple(statuses or [])}")
        if list(statuses or []) == ["closed"]:
            return {"positions": self._closed}
        return {"positions": []}

    def list_tools(self):
        self.calls.append("list_tools")
        return self._tools

    def __getattr__(self, name):  # guard: jalur order tidak boleh tersentuh
        raise AssertionError(f"reconcile_history READ-ONLY, tidak boleh memanggil {name!r}")


# ------------------------------------------------------------------------------
# 1) Konversi payload 17 kunci -> baris backfill yang JUJUR
# ------------------------------------------------------------------------------
def test_build_backfill_rows_jujur_dari_payload_17_kunci():
    rows = walt_bot.build_backfill_rows(_closed_payload(3))
    assert len(rows) == 3
    assert len({r["position_id"] for r in rows}) == 3, "satu kunci dedupe per posisi"
    for idx, r in enumerate(rows):
        assert r["exit_price"] is None, "endpoint tidak punya harga keluar -> jangan mengarang"
        assert r["pnl_quote"] is None
        assert r["exit_source"] == walt_bot.EXCHANGE_HISTORY_EXIT_SOURCE
        assert r["funding_quote"] == f"{0.5 + idx:.6f}"
        assert r["margin"] == "2.50"
        assert r["opened_at"] == f"2026-11-1{idx}T07:00:00Z"
        assert r["closed_at"] == f"2026-11-1{idx}T09:00:00Z"
        assert r["side"] in ("LONG", "SHORT")


def test_build_backfill_rows_punya_satu_kunci_per_posisi_tanpa_position_id():
    payload = _closed_payload(2)
    for p in payload:
        p.pop("position_id", None)
    rows = walt_bot.build_backfill_rows(payload)
    assert rows[0]["position_id"] == "500:100.0:2026-11-10T07:00:00Z"


# ------------------------------------------------------------------------------
# 2) KUNCI DEDUPE TUNGGAL: backfill == live untuk trade yang sama.
#    Test ini HARUS MERAH bila Item 1 membuat skema kunci kedua.
# ------------------------------------------------------------------------------
def test_kunci_backfill_sama_dengan_kunci_live():
    exchange_closed = _closed_payload(1)[0]
    snapshot_open = {
        "position_id": "",
        "contract_id": exchange_closed["contract_id"],
        "entry_price": float(exchange_closed["avg_entry_price"]),
        "opened_at": exchange_closed["created_at"],
    }
    live_key = walt_bot.make_position_dedupe_key(snapshot_open)
    backfill_key = walt_bot.build_backfill_rows([exchange_closed])[0]["position_id"]
    assert backfill_key == live_key


def test_kunci_backfill_peka_terhadap_pemetaan_soal_test_2_tidak_vakum(monkeypatch):
    """
    Non-vacuity proof untuk test_kunci_backfill_sama_dengan_kunci_live:
    bila opened_at ditukar (salah pemetaan created_at <- opened_at), kunci HARUS berbeda.
    Artinya test #2 benar-benar menguji pemetaan, bukan perbandingan string konstan.
    """
    base = _closed_payload(1)[0]
    other = dict(base, created_at="2026-11-10T07:00:01Z")

    def live_key_of(pos: dict) -> str:
        return walt_bot.make_position_dedupe_key({
            "position_id": "",
            "contract_id": pos["contract_id"],
            "entry_price": float(pos["avg_entry_price"]),
            "opened_at": pos["created_at"],
        })

    assert walt_bot.build_backfill_rows([base])[0]["position_id"] == live_key_of(base)
    assert walt_bot.build_backfill_rows([other])[0]["position_id"] == live_key_of(other)
    assert walt_bot.build_backfill_rows([base])[0]["position_id"] != live_key_of(other)

    # Bila entry_price tidak dibulatkan sama seperti jalur live, kunci juga harus berubah.
    mismatched = walt_bot.make_position_dedupe_key({
        "position_id": "",
        "contract_id": base["contract_id"],
        "entry_price": float(base["avg_entry_price"]) + 0.000001,
        "opened_at": base["created_at"],
    })
    assert mismatched != walt_bot.build_backfill_rows([base])[0]["position_id"]



# ------------------------------------------------------------------------------
# 3) reconcile_history: dry-run default, idempoten, read-only, backup sebelum tulis
# ------------------------------------------------------------------------------
def _csv_lines(path: str) -> list[list[str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def test_reconcile_history_dry_run_default_tidak_menyentuh_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    csv_path = tmp_path / "closed_history.csv"
    ids_path = tmp_path / "closed_ids.json"
    client = FakeClient(_closed_payload(3))

    out = walt_bot.reconcile_history(
        client, csv_path=str(csv_path), closed_ids_path=str(ids_path)
    )

    assert out["mode"] == "dry_run"
    assert out["rows_seen"] == 3 and out["candidates_to_add"] == 3
    assert out["written"] == 0
    assert not csv_path.exists() and not ids_path.exists()
    assert "--write" in out["note"]
    assert all(c.startswith("list_positions") or c == "list_tools" for c in client.calls)


def test_reconcile_history_write_idempoten_delta_nol(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    csv_path = tmp_path / "closed_history.csv"
    ids_path = tmp_path / "closed_ids.json"
    client = FakeClient(_closed_payload(3))

    first = walt_bot.reconcile_history(
        client, write=True, csv_path=str(csv_path), closed_ids_path=str(ids_path)
    )
    assert first["written"] == 3
    after_first = _csv_lines(str(csv_path))
    assert len(after_first) == 4  # header + 3
    assert all(row[14] == walt_bot.EXCHANGE_HISTORY_EXIT_SOURCE for row in after_first[1:])
    assert all(row[5] == "" and row[7] == "" for row in after_first[1:]), "exit & PnL kosong"

    second = walt_bot.reconcile_history(
        client, write=True, csv_path=str(csv_path), closed_ids_path=str(ids_path)
    )
    after_second = _csv_lines(str(csv_path))
    assert second["written"] == 0
    assert second["already_recorded"] == 3
    assert len(after_second) == len(after_first), "delta baris harus 0 saat dijalankan dua kali"


def test_reconcile_history_backup_sebelum_write_dan_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    csv_path = tmp_path / "closed_history.csv"
    ids_path = tmp_path / "closed_ids.json"
    client = FakeClient(_closed_payload(4))

    walt_bot.reconcile_history(client, write=True, csv_path=str(csv_path), closed_ids_path=str(ids_path))
    out = walt_bot.reconcile_history(
        client, write=True, limit=1, csv_path=str(csv_path), closed_ids_path=str(ids_path)
    )
    assert out["rows_seen"] == 1 and out["written"] == 0
    assert list(tmp_path.glob("closed_history.csv.bak-*")) == []  # tidak ada kandidat -> tanpa backup


def test_reconcile_history_menolak_memanggil_jalur_order(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    client = FakeClient(_closed_payload(1))
    with pytest.raises(AssertionError, match="READ-ONLY"):
        client.preview_order(1, "long", 5, 2.5, 100.0)  # guard fake client: tidak pernah dipakai


# ------------------------------------------------------------------------------
# 4) Pengukuran kelengkapan (Item 1 langkah 1) — tidak boleh mengarang "LENGKAP"
# ------------------------------------------------------------------------------
def test_describe_closed_history_pernah_terpotong_saat_tanpetunjuk_kelengkapan():
    client = FakeClient(_closed_payload(3))
    m = walt_bot.describe_closed_history(client)
    assert m["closed_positions_count"] == 3
    assert m["response_wrapper_keys"] == ["positions"]
    assert m["pagination_hints_in_response"] == {}
    assert m["client_list_positions_params"] == ["statuses"], "klien tidak punya parameter paginasi"
    assert m["tool_pagination_params"] == []
    assert m["created_at_min"] == "2026-11-10T07:00:00Z"
    assert m["created_at_max"] == "2026-11-12T07:00:00Z"
    assert "TIDAK BISA DIVERIFIKASI" in m["completeness_verdict"]


def test_describe_closed_history_lengkap_saat_total_cocok():
    class TotalClient(FakeClient):
        def list_positions(self, statuses=None):
            return {"positions": _closed_payload(3), "total": 3}

    m = walt_bot.describe_closed_history(TotalClient(_closed_payload(3)))
    assert m["completeness_verdict"].startswith("LENGKAP")


def test_describe_closed_history_menunjuk_paginasi_endpoint():
    tools = [{"name": "perps_list_positions", "inputSchema": {"properties": {"statuses": {}, "limit": {}, "cursor": {}}}}]
    client = FakeClient(_closed_payload(3), tools=tools)
    m = walt_bot.describe_closed_history(client)
    assert m["tool_pagination_params"] == ["limit", "cursor"]
    assert "endpoint mendukung paginasi" in m["completeness_verdict"]


def test_describe_closed_history_menghitung_field_tidak_valid():
    payload = _closed_payload(2)
    payload[0]["avg_entry_price"] = "abc"
    payload[1]["value_base"] = None
    client = FakeClient(payload)
    m = walt_bot.describe_closed_history(client)
    assert m["invalid_number_field_counts"]["avg_entry_price"] == 1
    assert m["invalid_number_field_counts"]["value_base"] == 1


# ------------------------------------------------------------------------------
# 5) Kegagalan baca ledger BERBUNYI: 1 warning teragregasi + stats jujur
# ------------------------------------------------------------------------------
def _count_ledger_warnings(caplog) -> int:
    return sum(
        1 for rec in caplog.records
        if "LEDGER TIDAK TERTBACA" in rec.getMessage() and rec.levelname == "WARNING"
    )


def test_http_error_menghasilkan_tepat_satu_warning_teragregasi(monkeypatch, caplog):
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("connection reset")

    monkeypatch.setattr(sheet_logger.httpx, "get", boom)
    monkeypatch.setenv("GOOGLE_SHEET_ID", "dummy-id-untuk-test")
    sheet_logger.reset_ledger_warning_state()
    caplog.clear()

    rows, status = sheet_logger.fetch_sheet_csv_rows_ex(
        "Closed_Positions", expected_col_name="Exit Price ($)"
    )
    assert rows == [] and status == "http_error"
    assert calls["n"] == 1
    # Panggilan berulang dalam jendela 15 menit tidak boleh spam log.
    for _ in range(5):
        sheet_logger.fetch_sheet_csv_rows_ex("Closed_Positions", expected_col_name="Exit Price ($)")
    assert calls["n"] == 6, "percobaan HTTP tetap terjadi; yang diagregasi hanya log-nya"
    assert _count_ledger_warnings(caplog) == 1


def test_stats_memuat_kalimat_ledger_tidak_terbaca_saat_http_error(tmp_path, monkeypatch, capsys, caplog):
    def boom(*a, **k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(sheet_logger.httpx, "get", boom)
    monkeypatch.setenv("GOOGLE_SHEET_ID", "dummy-id-untuk-test")
    monkeypatch.setattr(walt_bot, "send_telegram", lambda *a, **k: False)
    sheet_logger.reset_ledger_warning_state()
    caplog.clear()

    stats = walt_bot.compute_closed_stats(
        csv_path=str(tmp_path / "kosong.csv"),
        pnl_tracker_path=str(tmp_path / "kosong_pnl.csv"),
        send_tg=False,
    )
    out = capsys.readouterr().out
    assert "LEDGER TIDAK TERTBACA (http_error)" in out
    assert stats["ledger_source"]["unreadable"] is True
    assert stats["ledger_source"]["sheet_status"] == "http_error"
    assert stats["total_trades"] == 0 and stats["pnl_terukur"] == 0
    assert "BUKAN berarti tidak ada trade" in out


def test_stats_punya_kolom_belum_terukur_pnl_untuk_baris_backfill(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    csv_path = tmp_path / "closed_history.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(sheet_logger.CLOSED_POSITIONS_HEADERS)
        # 1 baris terukur (TP) + 1 baris backfill bursa (exit & pnl kosong, sumber menandai jujur)
        w.writerow([
            "2026-11-19T10:00:00+00:00", "NVDA", "LONG", 10, 100.0, 102.0,
            2.5, 0.10, 0.01, "TP breached", "pos_ukur_1",
            "2026-11-19T12:00:00+00:00", "vtest", "2026-11-19T10:00:00+00:00", "exchange_field",
        ])
        w.writerow([
            "2026-11-18T10:00:00+00:00", "BTC", "LONG", 11, 90000.0, "",
            2.5, "", 0.02, walt_bot.BACKFILL_CLOSE_REASON, "501:90000.0:2026-11-18T07:00:00Z",
            "2026-11-18T12:00:00+00:00", "vtest", "2026-11-18T07:00:00+00:00",
            walt_bot.EXCHANGE_HISTORY_EXIT_SOURCE,
        ])

    stats = walt_bot.compute_closed_stats(
        csv_path=str(csv_path),
        pnl_tracker_path=str(tmp_path / "kosong_pnl.csv"),
        send_tg=False,
    )
    assert stats["ledger_source"]["rows_from"] == "local_csv"
    assert stats["ledger_source"]["spreadsheet_configured"] is False
    assert stats["total_trades"] == 1 and stats["pnl_terukur"] == 1
    assert stats["belum_terukur_pnl"] == 1
    assert stats["unknown_count"] == 1


def test_no_config_tidak_melakukan_percobaan_http_sama_sekali(monkeypatch, tmp_path):
    calls = {"n": 0}

    def forbidden(*a, **k):
        calls["n"] += 1
        raise AssertionError("httpx.get tidak boleh dipanggil saat GOOGLE_SHEET_ID kosong")

    monkeypatch.setattr(sheet_logger.httpx, "get", forbidden)
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    sheet_logger.reset_ledger_warning_state()

    local = tmp_path / "closed_history.csv"
    rows, status = sheet_logger.fetch_sheet_csv_rows_ex(
        "Closed_Positions", expected_col_name="Exit Price ($)", local_csv_fallback=str(local)
    )
    assert calls["n"] == 0
    assert rows == [] and status == "no_config"

    local.write_text("a,b\n1,2\n", encoding="utf-8")
    rows, status = sheet_logger.fetch_sheet_csv_rows_ex(
        "Closed_Positions", expected_col_name="Exit Price ($)", local_csv_fallback=str(local)
    )
    assert calls["n"] == 0
    assert status == "local_csv" and rows == [["a", "b"], ["1", "2"]]


def test_default_sheet_id_hilang_dari_kode():
    """R6-1/V3: repo publik tidak boleh memuat ID spreadsheet sebagai default di kode."""
    src = io.open("sheet_logger.py", encoding="utf-8").read()
    assert "DEFAULT_SPREADSHEET_ID" not in src
    assert not any(line.strip().startswith("DEFAULT_SPREADSHEET_ID") for line in src.splitlines())
