/**
 * Google Apps Script Webhook untuk Walt Perps Trading & Funding Log
 * 
 * CARA PASANG:
 * 1. Buka Google Sheet Anda: https://docs.google.com/spreadsheets/d/<GOOGLE_SHEET_ID>/edit
 *    (ID TIDAK ditulis di sumber publik — lihat Ronde 6 [R6-1]; sumber resmi: secret/variable
 *    GOOGLE_SHEET_ID di GitHub Actions atau env lokal Anda)
 * 2. Klik menu: Extensions (Ekstensi) > Apps Script
 * 3. Hapus kode default, paste seluruh kode ini.
 * 4. Klik: Deploy > New deployment (Penerapan Baru)
 * 5. Pilih jenis: Web app (Aplikasi Web)
 *    - Description: Walt Bot Webhook
 *    - Execute as: Me (email Anda)
 *    - Who has access: Anyone (Siapa saja)
 * 6. Klik Deploy > Berikan izin (Authorize Access).
 * 7. Copy Web app URL yang didapat (contoh: https://script.google.com/macros/s/.../exec).
 * 8. Simpan URL tersebut ke:
 *    - GitHub Repository Secrets: GOOGLE_SHEET_WEBHOOK_URL
 *    - File lokal .env: GOOGLE_SHEET_WEBHOOK_URL=...
 */

function doPost(e) {
  try {
    var contents = e.postData.contents;
    var data = JSON.parse(contents);
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    
    var sheetName = data.sheet || "Daily_Scans";
    var sheet = ss.getSheetByName(sheetName);
    var isNew = false;
    if (!sheet) {
      sheet = ss.insertSheet(sheetName);
      isNew = true;
    }

    // Header dikelola tunggal oleh Apps Script (hanya ditulis jika sheet baru / masih kosong)
    if ((isNew || sheet.getLastRow() === 0) && data.header && data.header.length > 0) {
      sheet.appendRow(data.header);
    }

    var rows = data.rows || [];
    if (rows.length > 0) {
      for (var i = 0; i < rows.length; i++) {
        sheet.appendRow(rows[i]);
      }
    }
    
    return ContentService.createTextOutput(JSON.stringify({
      status: "success",
      rows_added: rows.length
    })).setMimeType(ContentService.MimeType.JSON);
    
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({
      status: "error",
      message: err.toString()
    })).setMimeType(ContentService.MimeType.JSON);
  }
}

function doGet(e) {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName("Daily_Scans");
    if (!sheet) {
      return ContentService.createTextOutput(JSON.stringify({
        status: "empty",
        message: "Sheet Daily_Scans belum dibuat",
        data: {}
      })).setMimeType(ContentService.MimeType.JSON);
    }
    
    var data = sheet.getDataRange().getValues();
    if (data.length <= 1) {
      return ContentService.createTextOutput(JSON.stringify({
        status: "empty",
        message: "Data masih kosong",
        data: {}
      })).setMimeType(ContentService.MimeType.JSON);
    }
    
    // Map riset terbaru per ticker (baris paling bawah adalah yang paling baru)
    var latest = {};
    for (var i = 1; i < data.length; i++) {
      var row = data[i];
      var asset = String(row[3] || "").trim().toUpperCase();
      if (!asset) continue;
      
      latest[asset] = {
        timestamp: row[0],
        asset: asset,
        side: String(row[4] || "").toLowerCase(),
        funding_daily_pct: row[5],
        price: row[6],
        swarm_score: row[7],
        sentiment: String(row[8] || "").toLowerCase(),
        consensus: row[9],
        vendor_alignment: row[10],
        p1_polarity: row[11],
        p4_momentum: row[12],
        p5_catalysts: row[13],
        p6_contrarian: row[14],
        verdict: String(row[15] || "").toUpperCase(),
        margin: row[16],
        reason: row[17],
        narrative: row[18]
      };
    }
    
    return ContentService.createTextOutput(JSON.stringify({
      status: "success",
      count: Object.keys(latest).length,
      data: latest
    })).setMimeType(ContentService.MimeType.JSON);
    
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({
      status: "error",
      message: err.toString()
    })).setMimeType(ContentService.MimeType.JSON);
  }
}

/**
 * Fungsi pembersihan MANUAL (jalankan langsung dari editor Apps Script, tidak otomatis):
 * 1. Hapus baris header duplikat di Closed_Positions dan PnL_Tracker (sisakan baris 1).
 * 2. Hapus baris duplikat di Closed_Positions dengan kunci (Asset + Contract ID + Entry Price), sisakan baris pertama.
 */
function cleanupClosedPositions() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();

  // 1. Bersihkan Closed_Positions (header ganda + data duplikat)
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
    for (var d = rowsToDelete.length - 1; d >= 0; d--) {
      closedSheet.deleteRow(rowsToDelete[d]);
    }
    Logger.log("Closed_Positions: dihapus " + rowsToDelete.length + " baris duplikat/header ganda.");
  }

  // 2. Bersihkan header duplikat di PnL_Tracker
  var pnlSheet = ss.getSheetByName("PnL_Tracker");
  if (pnlSheet && pnlSheet.getLastRow() > 1) {
    var pnlValues = pnlSheet.getDataRange().getValues();
    var pnlDelete = [];
    for (var j = 1; j < pnlValues.length; j++) {
      var c0 = String(pnlValues[j][0] || "").trim().toLowerCase();
      if (c0 === "tanggal" || c0 === "date") {
        pnlDelete.push(j + 1);
      }
    }
    for (var k = pnlDelete.length - 1; k >= 0; k--) {
      pnlSheet.deleteRow(pnlDelete[k]);
    }
    Logger.log("PnL_Tracker: dihapus " + pnlDelete.length + " baris header ganda.");
  }
}
