/**
 * Test harness untuk google_apps_script.js (dijalankan dengan: node tools/test_apps_script.js)
 *
 * Apps Script tidak punya test runner, jadi SpreadsheetApp/LockService/ContentService
 * ditiru secukupnya dan doPost() dijalankan sungguhan di atas sheet tiruan.
 * Python memanggil file ini lewat tests/test_apps_script.py (skip bila node tidak ada).
 */
"use strict";

const path = require("path");
const fs = require("fs");

// ------------------------------------------------------------------ mock --
class MockSheet {
  constructor(name, rows, opts) {
    this.name_ = name;
    this.rows = rows.map((row) => row.slice());
    this.maxRows = (opts && opts.maxRows) || 1000;
    this.maxCols = (opts && opts.maxCols) || 26;
    this.tamper = (opts && opts.tamper) || null;
    this.grew = { rows: 0, cols: 0 };
  }
  getName() { return this.name_; }
  getMaxRows() { return this.maxRows; }
  getMaxColumns() { return this.maxCols; }
  getLastRow() {
    for (let r = this.rows.length - 1; r >= 0; r--) {
      if (this.rows[r].some((c) => String(c === null || c === undefined ? "" : c).trim() !== "")) return r + 1;
    }
    return 0;
  }
  getLastColumn() {
    let last = 0;
    for (const row of this.rows) {
      for (let c = row.length - 1; c >= 0; c--) {
        if (String(row[c] === null || row[c] === undefined ? "" : row[c]).trim() !== "") { last = Math.max(last, c + 1); break; }
      }
    }
    return last;
  }
  insertRowsAfter(after, howMany) { this.maxRows += howMany; this.grew.rows += howMany; }
  insertColumnsAfter(after, howMany) { this.maxCols += howMany; this.grew.cols += howMany; }
  getRange(row, col, numRows, numCols) {
    numRows = numRows === undefined ? 1 : numRows;
    numCols = numCols === undefined ? 1 : numCols;
    const sheet = this;
    if (row + numRows - 1 > sheet.maxRows || col + numCols - 1 > sheet.maxCols) {
      throw new Error(`getRange out of bounds: r${row}c${col} ${numRows}x${numCols} (max ${sheet.maxRows}x${sheet.maxCols})`);
    }
    while (sheet.rows.length < row + numRows - 1) sheet.rows.push([]);
    return {
      getValues() {
        const out = [];
        for (let r = row - 1; r < row - 1 + numRows; r++) {
          const line = [];
          for (let c = col - 1; c < col - 1 + numCols; c++) line.push(sheet.rows[r] && sheet.rows[r][c] !== undefined ? sheet.rows[r][c] : "");
          out.push(line);
        }
        return out;
      },
      setValues(values) {
        for (let r = 0; r < values.length; r++) {
          const target = row - 1 + r;
          while (sheet.rows[target].length < col - 1 + values[r].length) sheet.rows[target].push("");
          for (let c = 0; c < values[r].length; c++) {
            let value = values[r][c];
            if (sheet.tamper && sheet.tamper(row + r, col + c)) value = "CORRUPTED";
            sheet.rows[target][col - 1 + c] = value;
          }
        }
      },
      setValue(value) {
        const target = sheet.rows[row - 1];
        while (target.length < col) target.push("");
        target[col - 1] = value;
      },
    };
  }
}

function runDoPost(sheet, payload) {
  const sheets = {};
  sheets[sheet.getName()] = sheet;
  global.SpreadsheetApp = {
    getActiveSpreadsheet: () => ({
      getSheetByName: (name) => sheets[name] || null,
      insertSheet: (name) => { sheets[name] = new MockSheet(name, [], {}); return sheets[name]; },
      getSheets: () => Object.values(sheets),
    }),
  };
  global.LockService = { getScriptLock: () => ({ waitLock: () => {}, releaseLock: () => {} }) };
  let captured = "";
  global.ContentService = {
    createTextOutput: (text) => { captured = text; return { setMimeType: () => ({ toString: () => captured, parse: () => JSON.parse(captured) }) }; },
    MimeType: { JSON: "json" },
  };
  const file = path.join(__dirname, "..", "google_apps_script.js");
  const source = fs.readFileSync(file, "utf8");
  // Dimuat ulang tiap panggilan supaya konstanta versi tidak menumpuk di global scope.
  const sandbox = new Function(source + "\nreturn { doPost: doPost, doGet: doGet, ensureHeader_: ensureHeader_, norm_: norm_, sameValue_: sameValue_, recordsFrom_: recordsFrom_ };");
  const api = sandbox();
  return api.doPost({ postData: { contents: JSON.stringify(payload) } }).parse();
}

// ----------------------------------------------------------------- tests --
let failures = 0;
function check(label, condition, detail) {
  if (condition) { console.log("  ok   " + label); return; }
  failures++;
  console.log("  GAGAL " + label + (detail ? " -> " + JSON.stringify(detail) : ""));
}

const V2_HEADER = ["timestamp_utc", "asset", "price", "verdict"]
  .concat(["mg_status", "mg_net_gravity", "mg_regime"]);

console.log("1. sheet baru: header ditulis, baris terbukti");
{
  const sheet = new MockSheet("Daily_Scans", [], { maxCols: 5 });
  const res = runDoPost(sheet, { sheet: "Daily_Scans", header: V2_HEADER, records: [{ timestamp_utc: "T1", asset: "BTC", price: 1, verdict: "GO", mg_status: "ok", mg_net_gravity: "42.0", mg_regime: "Trend" }] });
  check("status success", res.status === "success", res);
  check("rows_added 1", res.rows_added === 1, res);
  check("verified", res.verified === true, res);
  check("header baris 1 lengkap", String(sheet.rows[0].slice(0, 7)) === V2_HEADER.join(","), sheet.rows[0]);
  check("kolom baru dilaporkan", res.columns_added.length === 7, res.columns_added);
}

console.log("2. sheet lama 4 kolom: mg_* ditambahkan di kanan TANPA menggeser data");
{
  const sheet = new MockSheet("Daily_Scans", [
    ["timestamp_utc", "asset", "price", "verdict"],
    ["T0", "ETH", "99", "SKIP"],
  ], { maxCols: 26 });
  const res = runDoPost(sheet, { sheet: "Daily_Scans", header: V2_HEADER, records: [{ timestamp_utc: "T1", asset: "BTC", price: 2, verdict: "GO", mg_status: "ok", mg_net_gravity: "10", mg_regime: "Range" }] });
  check("hanya mg_* yang ditambahkan", String(res.columns_added) === "mg_status,mg_net_gravity,mg_regime", res.columns_added);
  check("baris lama utuh", String(sheet.rows[1]) === "T0,ETH,99,SKIP", sheet.rows[1]);
  check("data baru di kolom yang benar", sheet.rows[2][1] === "BTC" && sheet.rows[2][6] === "Range", sheet.rows[2]);
  check("read-back terbukti", res.verified === true, res);
}

console.log("3. klien lama posisional (rows + header) dipetakan lewat nama kolom");
{
  const sheet = new MockSheet("Daily_Scans", [
    ["timestamp_utc", "asset", "price", "verdict", "mg_status", "mg_net_gravity", "mg_regime"],
    ["T0", "ETH", "99", "SKIP", "ok", "1", "Trend"],
  ], { maxCols: 26 });
  const res = runDoPost(sheet, {
    sheet: "Daily_Scans",
    header: ["timestamp_utc", "asset", "price", "verdict"],
    rows: [["T1", "SOL", "77", "GO"]],
  });
  check("satu baris ditulis", res.rows_added === 1, res);
  check("tidak ada kolom baru", res.columns_added.length === 0, res.columns_added);
  check("nilai mendarat di kolomnya", sheet.rows[2][1] === "SOL" && sheet.rows[2][3] === "GO", sheet.rows[2]);
  check("kolom mg kosong untuk klien lama", sheet.rows[2][4] === "" && sheet.rows[2][6] === "", sheet.rows[2]);
}

console.log("4. tulis rusak terdeteksi oleh read-back");
{
  const sheet = new MockSheet("Daily_Scans", [], { maxCols: 26, tamper: (row, col) => row > 1 && col === 2 });
  const res = runDoPost(sheet, { sheet: "Daily_Scans", header: V2_HEADER, records: [{ timestamp_utc: "T1", asset: "BTC", price: 1, verdict: "GO", mg_status: "ok", mg_net_gravity: "1", mg_regime: "Trend" }] });
  check("verified false", res.verified === false, res);
  check("mismatch menyebut kolom asset", res.mismatch_columns && res.mismatch_columns[0].column === "asset", res.mismatch_columns);
}

console.log("5. dedupe mencegah baris ganda saat CI retry");
{
  const sheet = new MockSheet("Daily_Scans", [
    ["timestamp_utc", "asset", "price", "verdict", "mg_status", "mg_net_gravity", "mg_regime"],
    ["T1", "BTC", "1", "GO", "ok", "1", "Trend"],
  ], { maxCols: 26 });
  const res = runDoPost(sheet, {
    sheet: "Daily_Scans", header: V2_HEADER,
    records: [{ timestamp_utc: "T1", asset: "BTC", price: 1, verdict: "GO", mg_status: "ok", mg_net_gravity: "1", mg_regime: "Trend" }],
    dedupe: { field: "timestamp_utc", scan_rows: 50 },
  });
  check("baris disetujui, tidak ditulis ulang", res.rows_added === 0 && res.rows_skipped.length === 1, res);
  check("sheet tidak bertambah", sheet.rows.length === 2, sheet.rows.length);
}

console.log("6. sheet 26 kolom dapat menampung header 30 kolom (pertumbuhan otomatis)");
{
  const header = [];
  for (let i = 0; i < 30; i++) header.push("c" + i);
  const record = {};
  header.forEach((name) => { record[name] = name; });
  const sheet = new MockSheet("Daily_Scans", [], { maxCols: 26 });
  const res = runDoPost(sheet, { sheet: "Daily_Scans", header: header, records: [record] });
  check("tumbuh ke 30 kolom", sheet.maxCols === 30 && res.verified === true, { maxCols: sheet.maxCols, verified: res.verified });
}

console.log("7. baris 1 tanpa label: data tidak pernah ditimpa");
{
  const sheet = new MockSheet("Daily_Scans", [["", "", ""], ["X", "Y", "Z"]], { maxCols: 26 });
  const res = runDoPost(sheet, { sheet: "Daily_Scans", header: ["a", "b"], records: [{ a: 1, b: 2 }] });
  check("warning muncul", res.header_warning === "row1_has_no_labels_write_skipped", res);
  check("isi lama utuh", String(sheet.rows[1]) === "X,Y,Z", sheet.rows[1]);
}

console.log("8. doGet(?probe=1) melaporkan versi + header");
{
  const sheet = new MockSheet("Daily_Scans", [["timestamp_utc", "asset"], ["T1", "BTC"]], { maxCols: 26 });
  const api = (() => {
    global.SpreadsheetApp = { getActiveSpreadsheet: () => ({ getSheetByName: () => sheet, getSheets: () => [sheet], insertSheet: () => sheet }) };
    let captured = "";
    global.ContentService = { createTextOutput: (t) => { captured = t; return { setMimeType: () => ({ parse: () => JSON.parse(captured) }) }; }, MimeType: { JSON: "json" } };
    global.LockService = { getScriptLock: () => ({ waitLock: () => {}, releaseLock: () => {} }) };
    const source = fs.readFileSync(path.join(__dirname, "..", "google_apps_script.js"), "utf8");
    return new Function(source + "\nreturn { doGet: doGet };")();
  })();
  const res = api.doGet({ parameter: { probe: "1" } }).parse();
  check("probe sukses", res.status === "success" && res.probe === true, res);
  check("versi terlapor", typeof res.version === "string" && res.version.length > 5, res.version);
  check("header terlapor", String(res.sheets[0].header) === "timestamp_utc,asset", res.sheets[0]);
}

console.log(failures ? "\nHARNESS GAGAL: " + failures + " pemeriksaan" : "\nsemua pemeriksaan apps script lulus");
process.exit(failures ? 1 : 0);
