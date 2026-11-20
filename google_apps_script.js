/**
 * Google Apps Script Webhook untuk Walt Perps Trading & Funding Log  (v2)
 *
 * PERUBAHAN v2 (dibanding versi lama yang ternyata TIDAK sama dengan yang ter-deploy):
 *  1. REKONSILIASI HEADER berbasis NAMA kolom. Kolom baru (mis. mg_* diagnostik
 *     Market Gravity) di-append di ujung tanpa memindahkan atau menimpa data lama.
 *  2. TULIS BERDASARKAN NAMA. Klien boleh mengirim "records" (array of object).
 *     Klien lama yang mengirim "rows" + "header" (posisional) tetap didukung:
 *     nilainya dipetakan ke nama lebih dulu, jadi tidak pernah salah kolom.
 *  3. BUKTI TULIS (read-back). Setelah append, baris yang baru ditulis dibaca ulang
 *     dan dibandingkan kolom per kolom; respons memuat verified + mismatch_columns.
 *     HTTP 200 bukan lagi bukti keberhasilan tulis.
 *  4. IDEMPOTEN. Opsi "dedupe" mencegah baris ganda ketika workflow di-retry.
 *  5. VERSI TERLIHAT. doGet(?probe=1) membuktikan deployment aktif = kode repo,
 *     bukan stub lama.
 *
 * CARA PASANG: README bagian "Redeploy Apps Script (v2)".
 * ID sheet tidak pernah ditulis di sumber publik (aturan Ronde 6 [R6-1]).
 */

var WALT_WEBHOOK_VERSION = "2026-11-21.1";

function doPost(e) {
  var lock = LockService.getScriptLock();
  try {
    // Satu writer pada satu waktu: mencegah baris selisih/duplikat saat CI retry.
    lock.waitLock(20000);
  } catch (lockErr) {
    return json_({ status: "error", code: "lock_timeout", message: String(lockErr) });
  }

  try {
    var data = JSON.parse(e.postData.contents);
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheetName = data.sheet || "Daily_Scans";
    var sheet = ss.getSheetByName(sheetName);
    var created = false;
    if (!sheet) {
      sheet = ss.insertSheet(sheetName);
      created = true;
    }

    var incomingHeader = arrayOrNull_(data.header);
    var resolved = ensureHeader_(sheet, incomingHeader, arrayOrNull_(data.records));
    var header = resolved.header;
    var records = recordsFrom_(data, header);

    var result = {
      status: "success",
      version: WALT_WEBHOOK_VERSION,
      sheet: sheetName,
      sheet_created: created,
      header_written: resolved.wroteHeader,
      columns_added: resolved.added,
      resolved_header: header.filter(function (cell) { return String(cell || "").trim() !== ""; }),
      header_warning: resolved.warning || "",
      rows_received: records.length,
      rows_added: 0,
      rows_skipped: [],
      dropped_keys: [],
      verified: false
    };

    if (!records.length) {
      // Bukan error: ada panggilan yang hanya ingin memastikan header.
      result.verified = true;
      return json_(result);
    }

    collectUnknownKeys_(records, header, result);

    var dedupe = dedupeInfo_(data, header);
    var lastRow = sheet.getLastRow();
    var existingKeys = {};
    if (dedupe.field && lastRow > 1) {
      existingKeys = readKeySet_(sheet, dedupe.col, Math.min(lastRow - 1, dedupe.scanRows));
    }

    var toAppend = [];
    for (var i = 0; i < records.length; i++) {
      var record = records[i];
      var rowArray = new Array(header.length);
      for (var c = 0; c < header.length; c++) {
        var value = record[header[c]];
        rowArray[c] = (value === undefined || value === null) ? "" : value;
      }
      if (dedupe.field) {
        var key = String(record[dedupe.field] || "").trim();
        if (key && existingKeys[key]) {
          result.rows_skipped.push(key);
          continue;
        }
        if (key) existingKeys[key] = true;
      }
      toAppend.push(rowArray);
    }

    if (toAppend.length) {
      var startRow = sheet.getLastRow() + 1;
      growSheet_(sheet, startRow + toAppend.length - 1, header.length);
      sheet.getRange(startRow, 1, toAppend.length, header.length).setValues(toAppend);
      result.rows_added = toAppend.length;
      result.first_written_row = startRow;
      verifyWrite_(sheet, startRow, toAppend, result);
    } else {
      result.verified = true;  // tidak ada yang ditulis, tidak ada yang perlu dibuktikan
    }

    return json_(result);
  } catch (err) {
    return json_({
      status: "error",
      code: "doPost_exception",
      message: String(err && err.stack ? err.stack : err)
    });
  } finally {
    try { lock.releaseLock(); } catch (ignored) {}
  }
}

// ---------------------------------------------------------------- helpers --

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function arrayOrNull_(value) {
  return (value && Object.prototype.toString.call(value) === "[object Array]" && value.length) ? value : null;
}

function norm_(name) {
  // Samakan "P1 Polarity", "p1_polarity", "P1-Polarity" -> "p1polarity".
  return String(name || "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

/**
 * Pastikan baris 1 memuat semua kolom yang dibutuhkan klien.
 *
 * Aturan keamanan:
 *  - Kolom lama TIDAK pernah digeser, dibalik, atau dihapus.
 *  - Kolom baru menempati sel header yang kosong HANYA bila kolom itu benar-benar
 *    kosong di semua baris data; kalau tidak, ditambahkan di kanan sheet.
 *  - Lebar/tinggi sheet dinaikkan lebih dulu: Sheets baru punya 26 kolom default
 *    sedangkan header kita 30, dan getRange di luar batas akan melempar error.
 *
 * Mengembalikan header sebagai array POSISI (sel header kosong = string ""), supaya
 * array baris yang dibangun nanti selalu sejajar dengan kolom sheet.
 */
function ensureHeader_(sheet, incomingHeader, records) {
  var wanted = arrayOrNull_(incomingHeader);
  if (!wanted && records && records.length && typeof records[0] === "object" && !Array.isArray(records[0])) {
    wanted = Object.keys(records[0]);
  }
  wanted = (wanted || []).map(function (name) { return String(name || "").trim(); })
    .filter(function (name) { return name !== ""; });

  var lastRow = sheet.getLastRow();
  var lastCol = sheet.getLastColumn();
  if (lastRow < 1 || lastCol < 1) {
    if (!wanted.length) return { header: [], wroteHeader: false, added: [] };
    growSheet_(sheet, 1, wanted.length);
    sheet.getRange(1, 1, 1, wanted.length).setValues([wanted.slice()]);
    return { header: wanted.slice(), wroteHeader: true, added: wanted.slice() };
  }

  var width = Math.max(lastCol, wanted.length);
  growSheet_(sheet, lastRow, width);
  var row1 = sheet.getRange(1, 1, 1, width).getValues()[0].map(function (cell) { return String(cell || "").trim(); });
  var labeled = 0;
  var byNorm = {};
  for (var i = 0; i < row1.length; i++) {
    if (!row1[i]) continue;
    labeled++;
    if (byNorm[norm_(row1[i])] === undefined) byNorm[norm_(row1[i])] = i;
  }
  if (!labeled) {
    // Baris 1 kosong padahal ada data: JANGAN menimpa. Biarkan CI yang ribut.
    return { header: row1, wroteHeader: false, added: [], warning: "row1_has_no_labels_write_skipped" };
  }

  var dataBlock = lastRow > 1 ? sheet.getRange(2, 1, lastRow - 1, width).getValues() : [];
  function columnEmpty(col) {
    for (var r = 0; r < dataBlock.length; r++) {
      if (String(dataBlock[r][col] === null || dataBlock[r][col] === undefined ? "" : dataBlock[r][col]).trim() !== "") return false;
    }
    return true;
  }

  var added = [];
  var placements = [];
  for (var w = 0; w < wanted.length; w++) {
    var name = wanted[w];
    if (byNorm[norm_(name)] !== undefined) continue;
    var col = -1;
    for (var c = 0; c < row1.length; c++) {
      if (!row1[c] && columnEmpty(c)) { col = c; break; }
    }
    if (col < 0) { col = row1.length; row1.push(""); }
    row1[col] = name;
    byNorm[norm_(name)] = col;
    added.push(name);
    placements.push(col);
  }
  for (var p = 0; p < placements.length; p++) {
    sheet.getRange(1, placements[p] + 1).setValue(row1[placements[p]]);
  }
  return { header: row1, wroteHeader: added.length > 0, added: added };
}

/** Tambah kolom/baris bila kebutuhan melampaui ukuran maksimum sheet saat ini. */
function growSheet_(sheet, neededRows, neededCols) {
  var maxCols = sheet.getMaxColumns();
  if (neededCols > maxCols) sheet.insertColumnsAfter(maxCols, neededCols - maxCols);
  var maxRows = sheet.getMaxRows();
  if (neededRows > maxRows) sheet.insertRowsAfter(maxRows, neededRows - maxRows);
}

/**
 * Normalisasikan payload menjadi array of object.
 *  - data.records : array of object (disarankan, tahan terhadap perubahan kolom)
 *  - data.rows    : array of array; dipetakan lewat data.header, atau lewat header
 *                   sheet bila header tidak dikirim (perilaku klien lama Executions_Log)
 */
function recordsFrom_(data, header) {
  var records = arrayOrNull_(data.records);
  if (records) return records;

  var rows = arrayOrNull_(data.rows);
  if (!rows) return [];
  var names = arrayOrNull_(data.header) || header;
  if (!names) return [];

  var out = [];
  for (var r = 0; r < rows.length; r++) {
    var row = rows[r];
    if (!Array.isArray(row)) { out.push(row); continue; }
    var obj = {};
    for (var c = 0; c < row.length; c++) obj[names[c] || ("col" + (c + 1))] = row[c];
    out.push(obj);
  }
  return out;
}

function collectUnknownKeys_(records, header, result) {
  var known = {};
  for (var i = 0; i < header.length; i++) known[norm_(header[i])] = true;
  var unknown = {};
  for (var r = 0; r < records.length; r++) {
    var record = records[r];
    if (!record || typeof record !== "object" || Array.isArray(record)) continue;
    for (var key in record) {
      if (!Object.prototype.hasOwnProperty.call(record, key)) continue;
      if (!known[norm_(key)]) unknown[key] = true;
    }
  }
  var list = [];
  for (var name in unknown) if (Object.prototype.hasOwnProperty.call(unknown, name)) list.push(name);
  // Disampaikan, tidak diam-diam: kolom yang tidak ada di sheet memang dibuang.
  result.dropped_keys = list;
}

function dedupeInfo_(data, header) {
  var field = data.dedupe && data.dedupe.field ? String(data.dedupe.field) : "";
  var scanRows = data.dedupe && data.dedupe.scan_rows ? Number(data.dedupe.scan_rows) : 300;
  var col = -1;
  if (field) {
    for (var i = 0; i < header.length; i++) {
      if (norm_(header[i]) === norm_(field)) { col = i + 1; break; }
    }
  }
  return { field: col > 0 ? field : "", col: col, scanRows: scanRows };
}

function readKeySet_(sheet, col, depth) {
  var set = {};
  if (col < 1 || depth < 1) return set;
  var last = sheet.getLastRow();
  var from = Math.max(2, last - depth + 1);
  if (from > last) return set;
  var values = sheet.getRange(from, col, last - from + 1, 1).getValues();
  for (var i = 0; i < values.length; i++) {
    var key = String(values[i][0] || "").trim();
    if (key) set[key] = true;
  }
  return set;
}

/**
 * Baca ulang baris yang barusan ditulis dan bandingkan kolom per kolom.
 * Angka dibandingkan sebagai nilai numerik agar "0.10" vs 0.1 bukan menjadi differensi palsu.
 */
function verifyWrite_(sheet, startRow, toAppend, result) {
  var columns = toAppend[0].length;
  var readBack = sheet.getRange(startRow, 1, toAppend.length, columns).getValues();
  var mismatches = [];
  for (var r = 0; r < toAppend.length; r++) {
    for (var c = 0; c < columns; c++) {
      var expected = toAppend[r][c];
      var actual = readBack[r][c];
      if (sameValue_(expected, actual)) continue;
      mismatches.push({ row: startRow + r, column: result.resolved_header[c] || ("col" + (c + 1)), expected: expected, actual: String(actual) });
      if (mismatches.length >= 10) { result.verify_truncated = true; r = toAppend.length; break; }
    }
  }
  result.verified = mismatches.length === 0;
  if (mismatches.length) result.mismatch_columns = mismatches;
}

function sameValue_(expected, actual) {
  if (expected === actual) return true;
  var e = String(expected === null || expected === undefined ? "" : expected).trim();
  var a = String(actual === null || actual === undefined ? "" : actual).trim();
  if (e === a) return true;
  var ne = Number(e), na = Number(a);
  if (e !== "" && a !== "" && !isNaN(ne) && !isNaN(na)) return Math.abs(ne - na) <= 1e-9;
  if (actual instanceof Date) return true;  // Sheets menormalkan tanggal ke Date; bentuk teks tidak dibandingkan
  return e === "" && (a === "" || a === "0");
}

// ------------------------------------------------------------------ reads --

/**
 * GET /exec?probe=1     -> bukti versi deployment + header tiap tab (dipakai CI/pemilik)
 * GET /exec             -> riset terbaru per aset, dipetakan BERDASARKAN NAMA kolom
 * GET /exec?sheet=X&limit=N
 */
function doGet(e) {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var params = (e && e.parameter) || {};

    if (params.probe) {
      var sheets = ss.getSheets();
      var inventory = [];
      for (var s = 0; s < sheets.length; s++) {
        var sh = sheets[s];
        var cols = sh.getLastColumn();
        var header = cols > 0 ? sh.getRange(1, 1, 1, cols).getValues()[0] : [];
        inventory.push({
          name: sh.getName(),
          rows: Math.max(0, sh.getLastRow() - 1),
          header: header.filter(function (h) { return String(h || "").trim() !== ""; })
        });
      }
      return json_({ status: "success", version: WALT_WEBHOOK_VERSION, probe: true, sheets: inventory });
    }

    var sheetName = params.sheet || "Daily_Scans";
    var sheet = ss.getSheetByName(sheetName);
    if (!sheet || sheet.getLastRow() < 2) {
      return json_({
        status: "empty",
        version: WALT_WEBHOOK_VERSION,
        message: "Tab '" + sheetName + "' belum berisi data",
        data: {}
      });
    }

    var cols = sheet.getLastColumn();
    var headerRow = sheet.getRange(1, 1, 1, cols).getValues()[0];
    var indexOf = {};
    for (var h = 0; h < headerRow.length; h++) indexOf[norm_(headerRow[h])] = h;

    var limit = Math.min(5000, Math.max(1, Number(params.limit) || 500));
    var total = sheet.getLastRow() - 1;
    var fromRow = Math.max(2, total - limit + 2);
    var values = sheet.getRange(fromRow, 1, total - fromRow + 2, cols).getValues();

    var assetCol = indexOf["asset"];
    var timestampCol = indexOf["timestamputc"];
    var latest = {};
    for (var i = 1; i < values.length; i++) {
      var row = values[i];
      var asset = assetCol === undefined ? "" : String(row[assetCol] || "").trim().toUpperCase();
      if (!asset) continue;
      var record = {};
      for (var k = 0; k < headerRow.length; k++) {
        var label = String(headerRow[k] || "").trim();
        if (label) record[label] = row[k];
      }
      latest[asset] = record;
    }

    return json_({
      status: "success",
      version: WALT_WEBHOOK_VERSION,
      sheet: sheetName,
      count: Object.keys(latest).length,
      scanned_rows: values.length - 1,
      header: headerRow.filter(function (x) { return String(x || "").trim() !== ""; }),
      has_timestamp: timestampCol !== undefined,
      data: latest
    });
  } catch (err) {
    return json_({ status: "error", code: "doGet_exception", message: String(err && err.stack ? err.stack : err) });
  }
}

/**
 * Pembersihan MANUAL (jalankan dari editor Apps Script, tidak otomatis):
 * 1. Hapus baris header duplikat di Closed_Positions dan PnL_Tracker (sisakan baris 1).
 * 2. Hapus baris duplikat di Closed_Positions dengan kunci (Asset + Contract ID + Entry Price).
 */
function cleanupClosedPositions() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();

  var closedSheet = ss.getSheetByName("Closed_Positions");
  if (closedSheet && closedSheet.getLastRow() > 1) {
    var values = closedSheet.getDataRange().getValues();
    var seenKeys = {};
    var rowsToDelete = [];
    for (var i = 1; i < values.length; i++) {
      var row = values[i];
      var col0 = String(row[0] || "").trim().toLowerCase();
      var asset = String(row[1] || "").trim().toUpperCase();
      var cid = String(row[3] || "").trim();
      var entry = String(row[4] || "").trim();
      if (col0 === "timestamp (utc)" || asset === "ASSET") {
        rowsToDelete.push(i + 1);
        continue;
      }
      var dedupeKey = asset + "|" + cid + "|" + entry;
      if (asset && seenKeys[dedupeKey]) {
        rowsToDelete.push(i + 1);
      } else if (asset) {
        seenKeys[dedupeKey] = true;
      }
    }
    for (var d = rowsToDelete.length - 1; d >= 0; d--) closedSheet.deleteRow(rowsToDelete[d]);
    Logger.log("Closed_Positions: dihapus " + rowsToDelete.length + " baris duplikat/header ganda.");
  }

  var pnlSheet = ss.getSheetByName("PnL_Tracker");
  if (pnlSheet && pnlSheet.getLastRow() > 1) {
    var pnlValues = pnlSheet.getDataRange().getValues();
    var pnlDelete = [];
    for (var j = 1; j < pnlValues.length; j++) {
      var c0 = String(pnlValues[j][0] || "").trim().toLowerCase();
      if (c0 === "tanggal" || c0 === "date") pnlDelete.push(j + 1);
    }
    for (var k2 = pnlDelete.length - 1; k2 >= 0; k2--) pnlSheet.deleteRow(pnlDelete[k2]);
    Logger.log("PnL_Tracker: dihapus " + pnlDelete.length + " baris header ganda.");
  }
}
