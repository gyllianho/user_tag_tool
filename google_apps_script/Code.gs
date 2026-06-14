// ── Config ────────────────────────────────────────────────────────────────────
var BASE_URL = "https://food-admin.shopee.vn";

// ── Web App entry points ───────────────────────────────────────────────────────
function doGet(e) {
  return HtmlService.createHtmlOutputFromFile('Index')
    .setTitle('User Group Uploader')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function doPost(e) {
  return doGet(e);
}

// ── Called from client HTML via google.script.run ─────────────────────────────

function getTabs(sheetUrl) {
  var match = sheetUrl.match(/\/spreadsheets\/(?:u\/\d+\/)?d\/([a-zA-Z0-9_-]+)/);
  if (!match) throw new Error("URL Google Sheet không hợp lệ");
  var sheetId = match[1];

  var url = "https://docs.google.com/spreadsheets/d/" + sheetId + "/edit";
  var res = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
  var html = res.getContentText();
  var tabs = [];
  var re = /class="[^"]*sheet[^"]*"[^>]*>([^<]+)</g;
  var m;
  while ((m = re.exec(html)) !== null) {
    if (tabs.indexOf(m[1]) === -1) tabs.push(m[1]);
  }
  if (!tabs.length) throw new Error("Không lấy được tab. Sheet đã public chưa?");
  return tabs;
}

function extractPhones(sheetUrl, tabName) {
  var match = sheetUrl.match(/\/spreadsheets\/(?:u\/\d+\/)?d\/([a-zA-Z0-9_-]+)/);
  var sheetId = match[1];

  var csvUrl = "https://docs.google.com/spreadsheets/d/" + sheetId +
               "/gviz/tq?tqx=out:csv&sheet=" + encodeURIComponent(tabName);
  var res = UrlFetchApp.fetch(csvUrl, { muteHttpExceptions: true });
  var csv = res.getContentText();

  var rows = Utilities.parseCsv(csv);
  if (!rows.length) throw new Error("Tab trống");

  var headers = rows[0];
  var phoneCol = -1, noteCol = -1;
  var phoneKw = ["điện thoại", "phone", "sdt", "mobile"];
  // Ưu tiên cột có header ngắn (< 30 ký tự) chứa keyword
  for (var i = 0; i < headers.length; i++) {
    var h = headers[i].toLowerCase();
    if (h.indexOf("note") !== -1 && noteCol === -1) noteCol = i;
    if (phoneCol === -1 && headers[i].length < 30) {
      for (var k = 0; k < phoneKw.length; k++) {
        if (h.indexOf(phoneKw[k]) !== -1) { phoneCol = i; break; }
      }
    }
  }
  // Fallback: cột có header dài cũng chứa keyword
  if (phoneCol === -1) {
    for (var i = 0; i < headers.length; i++) {
      var h = headers[i].toLowerCase();
      for (var k = 0; k < phoneKw.length; k++) {
        if (h.indexOf(phoneKw[k]) !== -1) { phoneCol = i; break; }
      }
      if (phoneCol !== -1) break;
    }
  }
  // Fallback cuối: tìm cột có dữ liệu dạng số điện thoại (9-12 chữ số)
  if (phoneCol === -1) {
    for (var i = 0; i < headers.length; i++) {
      var count = 0;
      for (var r2 = 1; r2 < Math.min(rows.length, 6); r2++) {
        var val = (rows[r2][i] || "").replace(/\D/g, "");
        if (val.length >= 9 && val.length <= 12) count++;
      }
      if (count >= 2) { phoneCol = i; break; }
    }
  }
  if (phoneCol === -1) throw new Error("Không tìm thấy cột SĐT. Headers: " + headers.join(" | "));

  var tnv = [], user = [];
  for (var r = 1; r < rows.length; r++) {
    var raw = rows[r][phoneCol] || "";
    var phone = raw.replace(/\D/g, "");
    if (phone.length < 9) continue;
    if (phone.charAt(0) === "0") phone = "84" + phone.slice(1);
    else if (phone.slice(0,2) !== "84") phone = "84" + phone;
    var note = noteCol >= 0 ? (rows[r][noteCol] || "").trim().toUpperCase() : "";
    if (note === "TNV") tnv.push(phone);
    else user.push(phone);
  }
  return { tnv: tnv, user: user, phones: tnv.concat(user) };
}

function debugTab(sheetUrl, tabName) {
  var match = sheetUrl.match(/\/spreadsheets\/(?:u\/\d+\/)?d\/([a-zA-Z0-9_-]+)/);
  var sheetId = match[1];
  var csvUrl = "https://docs.google.com/spreadsheets/d/" + sheetId +
               "/gviz/tq?tqx=out:csv&sheet=" + encodeURIComponent(tabName);
  var res = UrlFetchApp.fetch(csvUrl, { muteHttpExceptions: true });
  var csv = res.getContentText();
  var rows = Utilities.parseCsv(csv);
  return {
    status: res.getResponseCode(),
    totalRows: rows.length,
    headers: rows.length > 0 ? rows[0] : [],
    row1: rows.length > 1 ? rows[1] : [],
    row2: rows.length > 2 ? rows[2] : [],
    csvSnippet: csv.slice(0, 300)
  };
}

function getGroupDetail(groupName, cookieStr) {
  var cookies = parseCookies(cookieStr);
  var csrf = cookies["csrfToken"] || cookies["csrftoken"] || "";
  var res = UrlFetchApp.fetch(BASE_URL + "/tag-sys/api/v1/group/get_group_detail", {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify({ region: "VN", group_name: groupName, business_type: 1 }),
    headers: {
      "Cookie": cookieStr,
      "x-sf-csrf-token": csrf,
      "origin": BASE_URL,
      "referer": BASE_URL,
      "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"
    },
    muteHttpExceptions: true
  });
  if (res.getResponseCode() !== 200) {
    throw new Error("HTTP " + res.getResponseCode() + " khi gọi get_group_detail");
  }
  var data = JSON.parse(res.getContentText());
  if (data.code !== 0) throw new Error("API lỗi: " + data.msg);
  return data.data.group_detail;
}

function callUploadList(groupDetail, cookieStr, operationType, userList, fileName) {
  var cookies = parseCookies(cookieStr);
  var csrf = cookies["csrfToken"] || cookies["csrftoken"] || "";
  var payload = {
    region: "VN", business_type: 1,
    entity_type: groupDetail.entity_type,
    group_id: groupDetail.group_id,
    group_type: groupDetail.group_type,
    creator: groupDetail.creator,
    file_name: fileName || "",
    operation_type: operationType,
    user_list: userList,
    execute_type: 0,
    expected_execute_time: 0
  };
  var res = UrlFetchApp.fetch(BASE_URL + "/tag-sys/api/v1/group/upload_list", {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify(payload),
    headers: {
      "Cookie": cookieStr,
      "x-sf-csrf-token": csrf,
      "origin": BASE_URL,
      "referer": BASE_URL,
      "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"
    },
    muteHttpExceptions: true
  });
  if (res.getResponseCode() !== 200) {
    throw new Error("HTTP " + res.getResponseCode() + " khi gọi upload_list");
  }
  var data = JSON.parse(res.getContentText());
  if (data.code !== 0) throw new Error("Upload lỗi: " + data.msg);
  return data;
}

function runUpload(params) {
  // params: { sheetUrl, cookieStr, sections:[{tabName, groupName}], clearBefore }
  var logs = [];
  var results = [];

  function log(msg) { logs.push(msg); }

  try {
    var sheetUrl  = params.sheetUrl;
    var cookieStr = params.cookieStr;
    var sections  = params.sections;
    var clearBefore = params.clearBefore;

    for (var i = 0; i < sections.length; i++) {
      var tabName   = sections[i].tabName;
      var groupName = sections[i].groupName;
      log("── Tab '" + tabName + "' → Group '" + groupName + "' ──");

      var phones = extractPhones(sheetUrl, tabName);
      log("TNV=" + phones.tnv.length + ", User=" + phones.user.length);

      var all = phones.tnv.concat(phones.user);
      if (!all.length) { log("[SKIP] Không có SĐT"); continue; }

      log("Lấy thông tin group...");
      var groupDetail = getGroupDetail(groupName, cookieStr);
      log("group_id=" + groupDetail.group_id + " ✓");

      if (clearBefore) {
        log("Xóa danh sách cũ...");
        callUploadList(groupDetail, cookieStr, 3, [], "");
        log("Chờ 8s...");
        Utilities.sleep(8000);
        log("Đã xóa ✓");
      }

      var fileName = "upl_" + Utilities.formatDate(new Date(), "Asia/Ho_Chi_Minh", "yyyyMMdd_HHmmss") +
                     "_" + Math.random().toString(36).slice(2,10) + ".csv";
      var userList = all.map(function(p){ return parseInt(p, 10); });
      log("Upload " + userList.length + " số...");
      callUploadList(groupDetail, cookieStr, 1, userList, fileName);
      log("Upload thành công ✓");

      results.push({ tab: tabName, groupName: groupName, total: all.length,
                     tnv: phones.tnv.length, user: phones.user.length });
    }
    return { ok: true, logs: logs, results: results };
  } catch(e) {
    logs.push("❌ " + e.message);
    return { ok: false, logs: logs, error: e.message };
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function parseCookies(cookieStr) {
  var obj = {};
  cookieStr.split(";").forEach(function(part) {
    part = part.trim();
    var idx = part.indexOf("=");
    if (idx < 0) return;
    obj[part.slice(0, idx).trim()] = part.slice(idx + 1).trim();
  });
  return obj;
}
