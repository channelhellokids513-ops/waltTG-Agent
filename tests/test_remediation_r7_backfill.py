"""(d) Backfill riwayat bursa ke ledger: dedupe WAJIB membaca sheet, bukan cuma cache.

Kasus yang dicegah: di CI, `logs/closed_history.csv` berasal dari Actions cache yang bisa ter-eviksi.
Sebelum ronde 7, `reconcile_history` hanya dedupe terhadap CSV + closed_ids.json — sementara
`--mirror-sheet` justru MENULIS ke Google Sheet. Kalau cache tertinggal dari sheet, sekali jalan
berarti baris duplikat permanen di ledger yang dipakai mengukur performansi strategi.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml

import sheet_logger
import walt_bot

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeClient:
    def __init__(self, closed):
        self._closed = closed
        self.calls: list[str] = []

    def list_positions(self, statuses=None):
        self.calls.append(f"list_positions:{tuple(statuses or [])}")
        return {"positions": self._closed if list(statuses or []) == ["closed"] else []}

    def list_tools(self):
        self.calls.append("list_tools")
        return []

    def __getattr__(self, name):
        raise AssertionError(f"reconcile_history READ-ONLY terhadap bursa, ditanya {name!r}")


def _payload(n: int, prefix: str = "POS"):
    """Bentuk payload memakai NAMA KUNCI bursa sebenarnya (dibaca dari build_backfill_rows),
    bukan nama internal — supaya test menguji jalur nyata."""
    out = []
    for i in range(n):
        out.append({
            "position_id": f"{prefix}{i}",
            "contract_id": 100 + i,
            "contract_name": f"AS{i}-USDT-PERPETUAL",
            "contract_base_asset": f"AS{i}",
            "side": "SHORT" if i % 2 else "LONG",
            "avg_entry_price": f"{100 + i}.5",
            "margin_quote": "2.50",
            "leverage": 2,
            "created_at": f"2026-11-1{i}T07:00:00Z",
            "updated_at": f"2026-11-1{i}T09:00:00Z",
            "funding_paid_quote": f"{0.5 + i:.6f}",
            "status": "closed",
            "realized_pnl": None,
            "exit_price": None,
        })
    return out


@pytest.fixture(autouse=True)
def _tanpa_sheet(monkeypatch):
    """Default: sheet tidak terkonfigurasi -> jalur baca asli mengembalikan `no_config` tanpa HTTP.

    Sengaja TIDAK men-stub `_recorded_closed_ids_from_sheet`: dengan begitu test yang mau mengisi
    ulang stub sheet tetap memanggil fungsi aslinya kalau ia membutuhkannya.
    """
    monkeypatch.setenv("GOOGLE_SHEET_ID", "")
    yield


def _csv_rows(path: Path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def test_mirror_tanpa_write_ditolak():
    with pytest.raises(ValueError, match="butuh write=True"):
        walt_bot.reconcile_history(FakeClient(_payload(1)), mirror_sheet=True)


def test_mirror_dibatalkan_ketika_sheet_gagal_dibaca(tmp_path):
    """Inti (d): tanpa dedupe yang bisa dipercaya, menulis = berjudi duplikat. Harus menolak."""
    csv_path, ids_path = tmp_path / "c.csv", tmp_path / "i.json"
    out = walt_bot.reconcile_history(
        FakeClient(_payload(3)), write=True, mirror_sheet=True,
        csv_path=str(csv_path), closed_ids_path=str(ids_path),
    )
    assert out["written"] == 0 and out["mirrored_ke_sheet"] == 0
    assert "aborted" in out and "no_config" in out["aborted"]
    assert not csv_path.exists(), "penolakan harus terjadi SEBELUM ada file yang disentuh"


def test_id_sheet_dipakai_menimbang_kandidat(tmp_path, monkeypatch):
    """Satu posisi sudah ada di sheet tapi tidak ada di cache CSV -> tidak boleh ditulis ulang."""
    payload = _payload(3)
    kunci = [r["position_id"] for r in walt_bot.build_backfill_rows(payload)]
    sudah_di_sheet = {kunci[1]}
    monkeypatch.setattr(
        walt_bot, "_recorded_closed_ids_from_sheet", lambda: (sudah_di_sheet, set(), "ok")
    )
    csv_path, ids_path = tmp_path / "c.csv", tmp_path / "i.json"
    out = walt_bot.reconcile_history(
        FakeClient(payload), write=True,
        csv_path=str(csv_path), closed_ids_path=str(ids_path),
    )
    assert out["candidates_to_add"] == 2 and out["written"] == 2
    assert out["sheet_dedupe"] == {"status": "ok", "punya_position_id": 1, "kunci_cadangan": 0, "dicocokkan_kunci_cadangan": 1, "sumber": "gviz Closed_Positions"}
    ids_di_csv = [r[10] for r in _csv_rows(csv_path)[1:]]
    assert kunci[1] not in ids_di_csv and kunci[0] in ids_di_csv


def test_webhook_hanya_untuk_mirror(tmp_path, monkeypatch):
    """write biasa = ledger lokal; hanya --mirror-sheet yang boleh mengirim webhook."""
    monkeypatch.setattr(walt_bot, "_recorded_closed_ids_from_sheet", lambda: (set(), set(), "ok"))
    dipanggil: list[bool] = []

    def fake_log_close(**kw):
        dipanggil.append(bool(kw.get("webhook")))
        return {}

    monkeypatch.setattr(sheet_logger, "log_close", fake_log_close)

    walt_bot.reconcile_history(FakeClient(_payload(2)), write=True,
                               csv_path=str(tmp_path / "a.csv"), closed_ids_path=str(tmp_path / "a.json"))
    assert dipanggil == [False, False], "write tanpa mirror tidak boleh menyentuh sheet"

    out = walt_bot.reconcile_history(FakeClient(_payload(2)), write=True, mirror_sheet=True,
                                     csv_path=str(tmp_path / "b.csv"), closed_ids_path=str(tmp_path / "b.json"))
    assert dipanggil[2:] == [True, True] and out["mirrored_ke_sheet"] == 2


def test_dedupe_sheet_membaca_posisi_kolom_position_id(monkeypatch):
    """Pencocokan nama kolom (bukan indeks buta): kolom bisa bergeser tanpa menjebak duplikat."""
    header = list(sheet_logger.CLOSED_POSITIONS_HEADERS)
    baris = [["", "", "", "", "", "", "", "", "", "", "LAMA-1"],
             ["", "", "", "", "", "", "", "", "", "", "LAMA-2"]]
    monkeypatch.setattr(sheet_logger, "fetch_sheet_csv_rows_ex",
                        lambda name, **kw: ([header] + baris, "ok"))
    monkeypatch.setenv("GOOGLE_SHEET_ID", "id-uji-bukan-nyata")
    ids, keys, status = walt_bot._recorded_closed_ids_from_sheet()
    assert status == "ok" and ids == {"LAMA-1", "LAMA-2"}


def test_gagal_baca_sheet_dikembalikan_sebagai_status_bukan_himpunan_kosong_saja(monkeypatch):
    monkeypatch.setattr(sheet_logger, "fetch_sheet_csv_rows_ex", lambda name, **kw: ([], "http_error"))
    ids, keys, status = walt_bot._recorded_closed_ids_from_sheet()
    assert ids == set() and keys == set() and status == "http_error", "panggilan harus bisa BEDAKAN kosong vs gagal"


def test_idempoten_saat_sheet_terbaca(tmp_path, monkeypatch):
    """Jalankan kedua setelah mirror harus nol delta — bukti guard dedupe benar-benar bekerja."""
    monkeypatch.setattr(walt_bot, "_recorded_closed_ids_from_sheet", lambda: (set(), set(), "ok"))
    csv_path, ids_path = tmp_path / "c.csv", tmp_path / "i.json"
    client = FakeClient(_payload(3))
    pertama = walt_bot.reconcile_history(client, write=True, mirror_sheet=True,
                                         csv_path=str(csv_path), closed_ids_path=str(ids_path))
    assert pertama["written"] == 3
    kedua = walt_bot.reconcile_history(client, write=True, mirror_sheet=True,
                                       csv_path=str(csv_path), closed_ids_path=str(ids_path))
    assert kedua["written"] == 0 and kedua["already_recorded"] == 3
    assert len(_csv_rows(csv_path)) == 4


def test_mode_ci_write_terpisah_rapat_dari_dry_run():
    """Mode yang menulis ledger harus bisa dibedakan SATU baris `if:` saja, dan bukan default."""
    wf = yaml.safe_load((REPO_ROOT / ".github/workflows" / "diagnostics.yml").read_text(encoding="utf-8"))
    steps = next(iter(wf["jobs"].values()))["steps"]
    tulis = [s for s in steps if "reconcile_history --write" in (s.get("run") or "")]
    kering = [s for s in steps
              if "python walt_bot.py reconcile_history" in (s.get("run") or "")
              and "--write" not in (s.get("run") or "")]
    assert len(tulis) == 1 and len(kering) == 1, (f"tulis={len(tulis)} kering={len(kering)}")
    assert tulis[0]["if"].endswith("oauth_step == 'reconcile_write'")
    assert "always()" in tulis[0]["if"], "langkah tulis harus tetap jalan walau sensor merah"
    assert kering[0]["if"].endswith("oauth_step == 'reconcile_history'")
    assert "--mirror-sheet" in tulis[0]["run"], "mode write harus mirror, kalau tidak ledger sheet diam"
    assert "GOOGLE_SHEET_WEBHOOK_URL" in tulis[0]["env"], "mirror tanpa URL webhook = tulis lokal doang"
    options = wf[True]["workflow_dispatch"]["inputs"]["oauth_step"]
    assert options["default"] != "reconcile_write", "operasi tak terbatal tidak boleh bisa salah pilih default"


def test_baris_warisan_tanpa_id_tanpa_sisi_ditahan_kunci_identitas(tmp_path, monkeypatch):
    """Kasus nyata 17:58Z: 11 baris warisan tanpa Position ID, tanpa Side, tanpa waktu bursa.

    Dedupe by id buta terhadap mereka. Kunci (aset|harga) adalah satu-satunya penahan yang masih
    tersedia, dan ARAH galatnya harus "tidak menulis" — baris ledger ganda merusak pengukuran
    performansi secara senyap, sedangkan baris yang terlewat membuat angka tetap tak lengkap
    dan itu dilaporkan.
    """
    payload = _payload(2)
    baris = walt_bot.build_backfill_rows(payload)
    legacy = f"{str(baris[0]['asset']).upper()}|{float(baris[0]['entry_price']):.4f}"
    monkeypatch.setattr(
        walt_bot, "_recorded_closed_ids_from_sheet", lambda: (set(), {legacy}, "ok")
    )
    out = walt_bot.reconcile_history(
        FakeClient(payload), write=True,
        csv_path=str(tmp_path / "c.csv"), closed_ids_path=str(tmp_path / "i.json"),
    )
    assert out["candidates_to_add"] == 1, "baris yang sama di sheet (tanpa id) tidak boleh ditulis ulang"
    assert out["written"] == 1
    assert out["sheet_dedupe"]["dicocokkan_kunci_cadangan"] == 1
    assert out["sheet_dedupe"]["kunci_cadangan"] == 1


def test_kunci_identitas_toleran_terhadap_format_harga():
    """Sheet menyimpan '812.97' sementara bursa mengirim '812.970000' -> harus tetap cocok."""
    dari_sheet = sheet_logger.close_identity_keys("SNDK", "", "812.97")
    dari_bursa = sheet_logger.close_identity_keys("SNDK", "SHORT", "812.970000")
    assert set(dari_sheet) & set(dari_bursa), f"{dari_sheet} vs {dari_bursa}"
    assert sheet_logger.close_identity_keys("", "SHORT", "1.0") == []
    assert sheet_logger.close_identity_keys("NVDA", "SHORT", "bukan angka") == []


def test_closed_ids_json_tersimpan_untuk_dedupe_lintas_proses(tmp_path, monkeypatch):

    monkeypatch.setattr(walt_bot, "_recorded_closed_ids_from_sheet", lambda: (set(), set(), "ok"))
    ids_path = tmp_path / "i.json"
    payload = _payload(2)
    kunci = {r["position_id"] for r in walt_bot.build_backfill_rows(payload)}
    walt_bot.reconcile_history(FakeClient(payload), write=True,
                               csv_path=str(tmp_path / "c.csv"), closed_ids_path=str(ids_path))
    tersimpan = json.loads(ids_path.read_text(encoding="utf-8"))
    tercatat = set(tersimpan) if isinstance(tersimpan, list) else set(tersimpan)
    assert kunci <= tercatat, f"closed_ids.json harus menyimpan {kunci}, dapat {tercatat}"
