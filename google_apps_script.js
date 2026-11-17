/**
 * Google Apps Script Webhook untuk Walt Perps Trading & Funding Log
 * 
 * CARA PASANG:
 * 1. Buka Google Sheet: https://docs.google.com/spreadsheets/d/1ag6MqYnOLdZgkFwk_cs3W-22EOX5e5DGQTVvPWENHTg/edit
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
    if (!sheet) {
      sheet = ss.insertSheet(sheetName);
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
