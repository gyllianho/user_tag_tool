#!/usr/bin/env python3
"""
User Group Uploader — single-file version
Chạy: python3 tool.py  →  http://localhost:5001
"""

import re, uuid, time, json, queue, threading, os, webbrowser
from pathlib import Path
import requests, pandas as pd
from datetime import datetime
from io import StringIO
from flask import Flask, Response, request, stream_with_context, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

SCHEDULE_FILE = Path(__file__).parent / "schedule_local.json"
HISTORY_FILE  = Path(__file__).parent / "history_local.json"

# ── Core logic ─────────────────────────────────────────────────────────────────

BASE_URL = "https://food-admin.shopee.vn"

def parse_sheet_id(url):
    m = re.search(r"/spreadsheets/(?:u/\d+/)?d/([a-zA-Z0-9_-]+)", url)
    if not m: raise ValueError("URL Google Sheet không hợp lệ")
    return m.group(1)

def get_sheet_tabs(sheet_id):
    res = requests.get(f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit", timeout=15)
    if res.status_code != 200:
        raise RuntimeError(f"Không đọc được sheet (status {res.status_code}). Sheet đã public chưa?")
    tabs = re.findall(r'class="[^"]*sheet[^"]*"[^>]*>([^<]+)<', res.text)
    if not tabs: raise RuntimeError("Không lấy được tên tab. Sheet đã public chưa?")
    return list(dict.fromkeys(tabs))

def read_tab_csv(sheet_id, tab_name):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={requests.utils.quote(tab_name)}"
    res = requests.get(url, timeout=15)
    res.raise_for_status()
    return pd.read_csv(StringIO(res.text), dtype=str)

def normalize_phone(p):
    p = re.sub(r"\D", "", p)
    if not p: return None
    if p.startswith("0"): p = "84" + p[1:]
    elif not p.startswith("84"): p = "84" + p
    return p if len(p) >= 9 else None

def extract_phones(sheet_id, tab_name, log=print):
    tabs = get_sheet_tabs(sheet_id)
    def norm(s): return s.replace("/","").replace("-","").replace(".","").replace(" ","")
    matched = next((t for t in tabs if norm(t)[:4] == norm(tab_name)[:4]), None)
    if not matched: raise RuntimeError(f"Không tìm thấy tab '{tab_name}'. Các tab: {tabs}")
    log(f"  Đọc tab: '{matched}'")
    df = read_tab_csv(sheet_id, matched)
    phone_col = next(
        (c for c in df.columns if isinstance(c, str) and len(c) < 30 and
         any(kw in c.lower() for kw in ["điện thoại","phone","sdt","mobile"])), None
    ) or next(
        (c for c in df.columns if isinstance(c, str) and
         any(kw in c.lower() for kw in ["điện thoại","phone","sdt","mobile"])), None
    )
    if phone_col is None:
        raise RuntimeError(f"Không tìm thấy cột SĐT. Columns: {list(df.columns)}")
    note_col = next((c for c in df.columns if isinstance(c, str) and "note" in c.lower()), None)
    tnv, user = [], []
    for _, row in df.iterrows():
        phone = normalize_phone(str(row.get(phone_col, "")))
        if not phone: continue
        note = str(row.get(note_col, "")).strip().upper() if note_col else ""
        (tnv if note == "TNV" else user).append(phone)
    log(f"  TNV={len(tnv)}, User={len(user)}")
    return tnv, user

def make_session(cookie_token):
    cookies = {}
    for part in cookie_token.split(";"):
        part = part.strip()
        if "=" not in part: continue
        k, _, v = part.partition("=")
        cookies[k.strip()] = v.strip()
    if not cookies: raise RuntimeError("Thiếu cookie token")
    csrf = cookies.get("csrfToken", cookies.get("csrftoken", ""))
    s = requests.Session()
    s.cookies.update(cookies)
    s.headers.update({
        "Content-Type": "application/json",
        "x-sf-csrf-token": csrf,
        "origin": BASE_URL, "referer": BASE_URL,
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    })
    return s

def get_group_detail(group_name, session, log=print):
    log(f"  Lấy group '{group_name}'...")
    res = session.post(f"{BASE_URL}/tag-sys/api/v1/group/get_group_detail",
        json={"region":"VN","group_name":group_name,"business_type":1}, timeout=15)
    res.raise_for_status()
    data = res.json()
    if data.get("code") != 0: raise RuntimeError(f"get_group_detail lỗi: {data.get('msg')}")
    return data["data"]["group_detail"]

def call_upload(group_detail, session, op_type, user_list, file_name=""):
    payload = {
        "region":"VN","business_type":1,
        "entity_type": group_detail["entity_type"],
        "group_id":    group_detail["group_id"],
        "group_type":  group_detail["group_type"],
        "creator":     group_detail["creator"],
        "file_name": file_name, "operation_type": op_type,
        "user_list": user_list, "execute_type": 0, "expected_execute_time": 0,
    }
    res = session.post(f"{BASE_URL}/tag-sys/api/v1/group/upload_list", json=payload, timeout=30)
    res.raise_for_status()
    data = res.json()
    if data.get("code") != 0: raise RuntimeError(f"upload_list lỗi: {data.get('msg')}")
    return data

def run_upload(cookie_token, sections, clear_before=True, log=print):
    """sections = [{sheet_url, tab_name, group_name}, ...]"""
    if not sections: raise RuntimeError("Không có section nào")
    session = make_session(cookie_token)
    results = []
    for sec in sections:
        sheet_url  = sec.get("sheet_url","").strip()
        tab_name   = sec.get("tab_name","").strip()
        group_name = sec.get("group_name","").strip()
        if not sheet_url or not tab_name or not group_name:
            log(f"[SKIP] Section thiếu thông tin"); continue
        log(f"\n── {tab_name} → {group_name} ──")
        sheet_id = parse_sheet_id(sheet_url)
        tnv, user = extract_phones(sheet_id, tab_name, log=log)
        phones = tnv + user
        if not phones: log(f"  Không có SĐT — bỏ qua"); continue
        gd = get_group_detail(group_name, session, log=log)
        if clear_before:
            log(f"  Xóa danh sách cũ...")
            call_upload(gd, session, 3, [], "")
            log(f"  Chờ 8s..."); time.sleep(8)
            log(f"  Đã xóa ✓")
        fname = f"upl_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.csv"
        user_list = [int(p) for p in phones]
        log(f"  Upload {len(user_list)} số...")
        call_upload(gd, session, 1, user_list, fname)
        log(f"  Upload thành công ✓")
        results.append({"tab":tab_name,"group_name":group_name,
                        "sheet_url":sheet_url,"total":len(phones),
                        "tnv":len(tnv),"user":len(user)})
    log("\n✅ Hoàn tất!")
    return results

# ── Persistence ────────────────────────────────────────────────────────────────

def load_sched():
    if SCHEDULE_FILE.exists():
        try: return json.loads(SCHEDULE_FILE.read_text())
        except: pass
    return {"enabled":False,"cookie_token":"","sections":[],"clear_before":True}

def save_sched(d): SCHEDULE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2))

def load_history():
    if HISTORY_FILE.exists():
        try: return json.loads(HISTORY_FILE.read_text())
        except: pass
    return []

def append_history(record):
    h = load_history()
    h.insert(0, record)
    HISTORY_FILE.write_text(json.dumps(h[:300], ensure_ascii=False, indent=2))

# ── Scheduler ─────────────────────────────────────────────────────────────────

def scheduled_job():
    cfg = load_sched()
    if not cfg.get("enabled"): return
    started = datetime.now().isoformat()
    logs = []
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏰ Scheduled upload...")
    try:
        results = run_upload(cfg["cookie_token"], cfg["sections"],
                             clear_before=cfg.get("clear_before",True),
                             log=lambda m: logs.append(m) or print(m))
        append_history({"id":uuid.uuid4().hex[:8],"type":"scheduled","status":"success",
                        "started":started,"finished":datetime.now().isoformat(),
                        "sections":cfg["sections"],"results":results,"error":"","log":logs})
    except Exception as e:
        append_history({"id":uuid.uuid4().hex[:8],"type":"scheduled","status":"error",
                        "started":started,"finished":datetime.now().isoformat(),
                        "sections":cfg["sections"],"results":[],"error":str(e),"log":logs})
        print(f"❌ {e}")

scheduler = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")
scheduler.add_job(scheduled_job, "cron", hour=9, minute=0, id="daily_9am")
scheduler.start()

# ── Flask ──────────────────────────────────────────────────────────────────────

app = Flask(__name__)

HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>User Group Uploader</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f0f2f5;min-height:100vh;padding:24px 16px}
.card{background:#fff;border-radius:12px;box-shadow:0 2px 16px rgba(0,0,0,.10);width:100%;max-width:800px;margin:0 auto;overflow:hidden}
.hdr{background:#ee4d2d;padding:18px 28px;display:flex;align-items:center;justify-content:space-between}
.hdr h1{color:#fff;font-size:17px;font-weight:600}.hdr p{color:rgba(255,255,255,.75);font-size:12px;margin-top:1px}
.tab-bar{display:flex;border-bottom:2px solid #f0f0f0;padding:0 24px;background:#fff}
.tb{padding:12px 18px;font-size:13px;font-weight:500;color:#888;border:none;background:none;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-2px}
.tb.on{color:#ee4d2d;border-bottom-color:#ee4d2d;font-weight:600}
.pane{display:none;padding:24px}.pane.on{display:block}
label{display:block;font-size:13px;font-weight:500;color:#333;margin-bottom:5px}
.req{color:#ee4d2d}
.field{margin-bottom:14px}
input,select,textarea{width:100%;padding:9px 12px;border:1px solid #d9d9d9;border-radius:8px;font-size:13px;color:#222;outline:none;transition:border-color .2s;background:#fff}
input:focus,select:focus,textarea:focus{border-color:#ee4d2d;box-shadow:0 0 0 2px rgba(238,77,45,.12)}
textarea{resize:vertical;min-height:64px;font-family:monospace;font-size:11px}
.row{display:flex;gap:8px;align-items:flex-end}.row input{flex:1}
.btn{padding:9px 16px;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer}
.btn-primary{background:#ee4d2d;color:#fff;width:100%;padding:12px;font-size:14px}
.btn-primary:hover{background:#d94327}.btn-primary:disabled{opacity:.6;cursor:not-allowed}
.btn-sm{padding:7px 11px;font-size:12px;background:#fff;border:1px solid #d9d9d9;color:#555;border-radius:6px;cursor:pointer;white-space:nowrap}
.btn-sm:hover{border-color:#ee4d2d;color:#ee4d2d}
.btn-add{width:100%;padding:9px;border:1.5px dashed #d9d9d9;border-radius:8px;background:#fff;color:#888;font-size:13px;cursor:pointer;margin-bottom:14px}
.btn-add:hover{border-color:#ee4d2d;color:#ee4d2d}
.sec{border:1px solid #e8e8e8;border-radius:10px;padding:14px;margin-bottom:10px;position:relative;background:#fafafa}
.sec-num{position:absolute;top:-9px;left:12px;background:#ee4d2d;color:#fff;font-size:10px;font-weight:700;padding:2px 7px;border-radius:10px}
.sec-rm{position:absolute;top:9px;right:10px;background:none;border:none;cursor:pointer;color:#bbb;font-size:15px}.sec-rm:hover{color:#ee4d2d}
.g3{display:grid;grid-template-columns:2fr 1fr 1fr;gap:10px}
@media(max-width:560px){.g3{grid-template-columns:1fr}}
.sec-foot{display:flex;align-items:center;justify-content:space-between;margin-top:10px}
.toggle-row{display:flex;align-items:center;gap:8px;margin-bottom:14px;font-size:13px;color:#555}
.divider{height:1px;background:#f0f0f0;margin:18px 0}
#logBox{display:none;background:#111;border-radius:8px;padding:12px 14px;font-family:monospace;font-size:11px;max-height:260px;overflow-y:auto;margin-top:14px}
#logBox.on{display:block}
.ll{padding:1px 0;line-height:1.6;color:#ccc}
.ll.ok{color:#4ade80}.ll.er{color:#f87171}.ll.sep{color:#facc15}.ll.info{color:#60a5fa}
#resBox{margin-top:10px}
.res-card{background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:12px 14px;margin-bottom:8px}
.res-card h4{font-size:13px;font-weight:600;color:#166534;margin-bottom:3px}
.res-card p{font-size:12px;color:#15803d}
.sched-box{background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:14px;margin-top:16px}
.sched-box h3{font-size:13px;font-weight:600;color:#92400e;margin-bottom:6px}
.spin{display:inline-block;width:13px;height:13px;border:2px solid rgba(255,255,255,.4);border-top-color:#fff;border-radius:50%;animation:sp .6s linear infinite;vertical-align:middle;margin-right:5px}
@keyframes sp{to{transform:rotate(360deg)}}
/* History */
.hist-filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.flt{padding:5px 12px;font-size:12px;border:1px solid #d9d9d9;border-radius:20px;background:#fff;cursor:pointer;color:#666}
.flt.on{background:#ee4d2d;color:#fff;border-color:#ee4d2d}
.hcard{border:1px solid #e8e8e8;border-radius:10px;margin-bottom:10px;overflow:hidden}
.hcard-hdr{padding:11px 14px;display:flex;align-items:center;gap:10px;cursor:pointer;user-select:none}
.hcard-hdr:hover{background:#fafafa}
.hbadge{font-size:11px;font-weight:700;padding:2px 8px;border-radius:10px}
.hbadge.success{background:#dcfce7;color:#166534}
.hbadge.error{background:#fee2e2;color:#991b1b}
.hbadge.scheduled{background:#dbeafe;color:#1e40af}
.hbadge.manual{background:#f3f4f6;color:#374151}
.hcard-body{display:none;padding:12px 14px;border-top:1px solid #f0f0f0;font-size:12px}
.hcard-body.on{display:block}
.hcard-body pre{background:#111;color:#ccc;border-radius:6px;padding:10px;font-size:11px;overflow-x:auto;max-height:160px;overflow-y:auto;margin-top:8px}
.stat-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px}
@media(max-width:500px){.stat-grid{grid-template-columns:1fr 1fr}}
.stat-card{background:#fff;border:1px solid #e8e8e8;border-radius:10px;padding:14px;text-align:center}
.stat-card .num{font-size:26px;font-weight:700;color:#ee4d2d}
.stat-card .lbl{font-size:12px;color:#888;margin-top:2px}
</style>
</head>
<body>
<div class="card">
  <div class="hdr">
    <div><h1>🍊 User Group Uploader</h1><p>Upload SĐT từ Google Sheet → ShopeeFood User Tag</p></div>
  </div>
  <div class="tab-bar">
    <button class="tb on" onclick="switchTab('upload',this)">Upload</button>
    <button class="tb" onclick="switchTab('history',this)">Lịch sử</button>
  </div>

  <!-- ── TAB UPLOAD ── -->
  <div id="pane-upload" class="pane on">
    <div class="field">
      <label>Cookie Token <span class="req">*</span></label>
      <textarea id="cookie" placeholder="Paste cookie từ DevTools (F12 → Network → Request Headers → Cookie)" rows="3"></textarea>
    </div>
    <div class="toggle-row">
      <input type="checkbox" id="clearBefore" checked>
      <label for="clearBefore" style="margin:0">Xóa danh sách cũ trước khi upload</label>
    </div>
    <div class="divider"></div>

    <div id="secList"></div>
    <button class="btn-add" onclick="addSec()">+ Thêm Section</button>

    <button class="btn btn-primary" id="btnGo" onclick="doUpload()">🚀 Bắt đầu Upload</button>
    <div id="logBox"></div>
    <div id="resBox"></div>

    <div class="sched-box">
      <h3>⏰ Tự động chạy lúc 9:00 sáng hàng ngày</h3>
      <div class="toggle-row" style="margin-bottom:6px">
        <input type="checkbox" id="schedEnabled" onchange="saveSchedule()">
        <label for="schedEnabled" style="margin:0;font-size:13px">Bật lịch tự động</label>
      </div>
      <div style="font-size:12px;color:#78716c">Khi bật, dùng cookie + sections đang điền ở trên. Tool phải đang chạy lúc 9h.</div>
      <div id="schedStatus" style="margin-top:6px;font-size:12px;color:#059669;font-weight:500"></div>
    </div>
  </div>

  <!-- ── TAB HISTORY ── -->
  <div id="pane-history" class="pane">
    <div class="stat-grid" id="statsGrid"></div>
    <div class="hist-filters">
      <button class="flt on" onclick="filterHist('all',this)">Tất cả</button>
      <button class="flt" onclick="filterHist('manual',this)">Thủ công</button>
      <button class="flt" onclick="filterHist('scheduled',this)">Scheduled</button>
      <button class="flt" onclick="filterHist('success',this)">✅ Thành công</button>
      <button class="flt" onclick="filterHist('error',this)">❌ Lỗi</button>
      <button class="btn-sm" style="margin-left:auto" onclick="loadHistory()">↻ Refresh</button>
    </div>
    <div id="histList"></div>
  </div>
</div>

<script>
const LS='ug_v3';
let tabs_cache={}, secN=0, allHistory=[], histFilter='all';

function switchTab(name,btn){
  document.querySelectorAll('.pane').forEach(p=>p.classList.remove('on'));
  document.querySelectorAll('.tb').forEach(b=>b.classList.remove('on'));
  document.getElementById('pane-'+name).classList.add('on');
  btn.classList.add('on');
  if(name==='history') loadHistory();
}

// ── Save / Restore ─────────────────────────────────────────────────────────
function collectSecs(){
  const r=[];
  document.querySelectorAll('.sec').forEach(c=>{
    const i=c.id.slice(2);
    const sheet_url=(document.getElementById('u'+i)||{}).value||'';
    const tab_name=(document.getElementById('t'+i)||{}).value||'';
    const group_name=(document.getElementById('g'+i)||{}).value||'';
    if(sheet_url.trim()&&tab_name.trim()&&group_name.trim())
      r.push({sheet_url:sheet_url.trim(),tab_name:tab_name.trim(),group_name:group_name.trim()});
  });
  return r;
}

function save(){
  const secs=[];
  document.querySelectorAll('.sec').forEach(c=>{
    const i=c.id.slice(2);
    secs.push({
      sheet_url:(document.getElementById('u'+i)||{}).value||'',
      tab_name:(document.getElementById('t'+i)||{}).value||'',
      group_name:(document.getElementById('g'+i)||{}).value||''
    });
  });
  localStorage.setItem(LS,JSON.stringify({
    cookie:document.getElementById('cookie').value,
    clear:document.getElementById('clearBefore').checked,
    secs
  }));
}

async function restore(){
  const raw=localStorage.getItem(LS);if(!raw)return;
  const d=JSON.parse(raw);
  document.getElementById('cookie').value=d.cookie||'';
  document.getElementById('clearBefore').checked=d.clear!==false;
  for(const s of (d.secs||[])){
    await addSec(s.sheet_url,s.tab_name,s.group_name);
  }
}

// ── Section ────────────────────────────────────────────────────────────────
function ddmm(){const d=new Date();return String(d.getDate()).padStart(2,'0')+String(d.getMonth()+1).padStart(2,'0');}

async function addSec(sheetUrl='',tabVal='',grpVal=''){
  secN++;const i=secN;
  const div=document.createElement('div');
  div.className='sec';div.id='sc'+i;
  div.innerHTML=`
    <span class="sec-num">#${i}</span>
    <button class="sec-rm" onclick="document.getElementById('sc${i}').remove();save()">✕</button>
    <div class="g3">
      <div class="field" style="margin:0">
        <label>Google Sheet URL <span class="req">*</span></label>
        <div class="row">
          <input type="url" id="u${i}" placeholder="https://docs.google.com/spreadsheets/d/..." value="${sheetUrl}" oninput="save()">
          <button class="btn-sm" onclick="fetchTabs(${i})">Lấy tabs</button>
        </div>
      </div>
      <div class="field" style="margin:0">
        <label>Tab <span class="req">*</span></label>
        <select id="t${i}" onchange="save()"><option value="">-- Chọn tab --</option></select>
      </div>
      <div class="field" style="margin:0">
        <label>User Group Name <span class="req">*</span></label>
        <input type="text" id="g${i}" placeholder="vd: Mai_Test_Hihub" value="${grpVal}" oninput="save()">
      </div>
    </div>
    <div class="sec-foot">
      <span id="tabStatus${i}" style="font-size:11px;color:#999"></span>
      <button class="btn-sm" onclick="dlCSV(${i})">⬇ Download CSV</button>
    </div>`;
  document.getElementById('secList').appendChild(div);
  if(sheetUrl){
    await fetchTabs(i, tabVal, true);
  }
}

async function fetchTabs(i, preselect='', silent=false){
  const urlEl=document.getElementById('u'+i);
  const url=urlEl?urlEl.value.trim():'';
  if(!url){if(!silent)alert('Nhập Google Sheet URL cho section này');return;}
  const btn=urlEl.parentElement.querySelector('.btn-sm');
  if(btn){btn.disabled=true;btn.textContent='⏳';}
  const st=document.getElementById('tabStatus'+i);
  try{
    let list=tabs_cache[url];
    if(!list){
      const r=await fetch('/tabs?url='+encodeURIComponent(url));
      const d=await r.json();
      if(d.error)throw new Error(d.error);
      list=d.tabs;
      tabs_cache[url]=list;
    }
    const sel=document.getElementById('t'+i);
    const cur=preselect||sel.value;
    sel.innerHTML='<option value="">-- Chọn tab --</option>';
    list.forEach(t=>{const o=document.createElement('option');o.value=t;o.textContent=t;if(t===cur)o.selected=true;sel.appendChild(o);});
    if(!cur){const today=list.find(t=>t.replace(/\D/g,'').startsWith(ddmm()));if(today)sel.value=today;}
    if(st)st.textContent=`${list.length} tabs`;
    save();
  }catch(e){if(!silent)alert('Lỗi: '+e.message);if(st)st.textContent='❌ '+e.message;}
  if(btn){btn.disabled=false;btn.textContent='Lấy tabs';}
}

// ── Upload ─────────────────────────────────────────────────────────────────
function addLog(msg,cls=''){
  const b=document.getElementById('logBox');
  const l=document.createElement('div');l.className='ll'+(cls?' '+cls:'');l.textContent=msg;
  b.appendChild(l);b.scrollTop=b.scrollHeight;
}

async function doUpload(){
  const cookie=document.getElementById('cookie').value.trim();
  const sections=collectSecs();
  const clear=document.getElementById('clearBefore').checked;
  if(!cookie){alert('Nhập Cookie Token');return;}
  if(!sections.length){alert('Điền ít nhất 1 section đầy đủ (URL + tab + group)');return;}
  save();
  const lb=document.getElementById('logBox'),rb=document.getElementById('resBox');
  lb.innerHTML='';lb.classList.add('on');rb.innerHTML='';
  const btn=document.getElementById('btnGo');
  btn.disabled=true;btn.innerHTML='<span class="spin"></span>Đang xử lý...';
  try{
    const res=await fetch('/upload',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cookie_token:cookie,sections,clear_before_upload:clear})});
    const reader=res.body.getReader();const dec=new TextDecoder();let buf='';
    while(true){
      const{done,value}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n\n');buf=lines.pop();
      for(const line of lines){
        if(!line.startsWith('data: '))continue;
        const d=JSON.parse(line.slice(6));
        if(d.type==='log'){
          const cls=d.msg.includes('✓')?'ok':d.msg.includes('❌')||d.msg.includes('lỗi')?'er':d.msg.startsWith('──')?'sep':d.msg.startsWith('  ')?'info':'';
          addLog(d.msg,cls);
        }else if(d.type==='done'){
          d.results.forEach(r=>{
            rb.innerHTML+=`<div class="res-card"><h4>✅ ${r.group_name}</h4><p>Tab: ${r.tab} | ${r.total} số (TNV: ${r.tnv}, User: ${r.user})</p></div>`;
          });
        }else if(d.type==='error'){
          addLog('❌ '+d.msg,'er');
          rb.innerHTML=`<div style="padding:12px;background:#fff1f2;border:1px solid #fca5a5;border-radius:8px;color:#991b1b;font-size:13px">❌ ${d.msg}</div>`;
        }
      }
    }
  }catch(e){addLog('❌ '+e.message,'er');}
  btn.disabled=false;btn.innerHTML='🚀 Bắt đầu Upload';
}

// ── Download CSV ───────────────────────────────────────────────────────────
async function dlCSV(i){
  const sheet_url=(document.getElementById('u'+i)||{}).value||'';
  const tab_name=(document.getElementById('t'+i)||{}).value||'';
  if(!sheet_url||!tab_name){alert('Nhập Sheet URL và chọn tab trước');return;}
  const btn=event.target;btn.disabled=true;btn.textContent='⏳';
  try{
    const res=await fetch('/download_csv',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sheet_url,tab_name})});
    if(!res.ok){const e=await res.json();alert('Lỗi: '+e.error);return;}
    const blob=await res.blob();
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');a.href=url;
    a.download=res.headers.get('Content-Disposition')?.match(/filename=(.+)/)?.[1]||tab_name+'.csv';
    a.click();URL.revokeObjectURL(url);
  }finally{btn.disabled=false;btn.textContent='⬇ Download CSV';}
}

// ── Schedule ───────────────────────────────────────────────────────────────
async function saveSchedule(){
  const enabled=document.getElementById('schedEnabled').checked;
  const cookie=document.getElementById('cookie').value.trim();
  const sections=collectSecs();
  const clear_before=document.getElementById('clearBefore').checked;
  await fetch('/schedule',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled,cookie_token:cookie,sections,clear_before})});
  const st=document.getElementById('schedStatus');
  st.textContent=enabled?'✅ Đang bật — chạy lúc 9:00 sáng hàng ngày ('+sections.length+' sections)':'⏸ Đã tắt';
}

async function loadScheduleStatus(){
  try{
    const r=await fetch('/schedule');const d=await r.json();
    document.getElementById('schedEnabled').checked=!!d.enabled;
    const st=document.getElementById('schedStatus');
    if(d.enabled) st.textContent='✅ Đang bật — chạy lúc 9:00 sáng ('+( d.sections||[]).length+' sections)';
  }catch(e){}
}

// ── History ────────────────────────────────────────────────────────────────
function fmt(iso){
  if(!iso)return'';
  const d=new Date(iso);
  return d.toLocaleDateString('vi-VN')+' '+d.toLocaleTimeString('vi-VN',{hour:'2-digit',minute:'2-digit'});
}

function renderStats(data){
  const total=data.length;
  const success=data.filter(d=>d.status==='success').length;
  const scheduled=data.filter(d=>d.type==='scheduled').length;
  document.getElementById('statsGrid').innerHTML=`
    <div class="stat-card"><div class="num">${total}</div><div class="lbl">Tổng lượt chạy</div></div>
    <div class="stat-card"><div class="num" style="color:#16a34a">${success}</div><div class="lbl">Thành công</div></div>
    <div class="stat-card"><div class="num" style="color:#2563eb">${scheduled}</div><div class="lbl">Tự động (9h)</div></div>`;
}

function renderHistory(data){
  const list=document.getElementById('histList');
  if(!data.length){list.innerHTML='<div style="text-align:center;color:#999;padding:32px;font-size:13px">Chưa có lịch sử</div>';return;}
  list.innerHTML=data.map((h,idx)=>{
    const secs=(h.sections||[]).map(s=>`<div style="margin-bottom:3px">• <b>${s.tab_name||s.tab}</b> → ${s.group_name} ${s.total!=null?'('+s.total+' số)':''}</div>`).join('');
    const results=(h.results||[]).map(r=>`<div>✅ ${r.group_name}: ${r.total} số (TNV:${r.tnv}, User:${r.user})</div>`).join('');
    const log=(h.log||[]).join('\n');
    return `<div class="hcard">
      <div class="hcard-hdr" onclick="toggleCard('hb${idx}')">
        <span class="hbadge ${h.status}">${h.status==='success'?'✅':'❌'}</span>
        <span class="hbadge ${h.type}">${h.type==='scheduled'?'⏰ Auto':'👤 Manual'}</span>
        <span style="font-size:13px;color:#333;flex:1">${fmt(h.started)}</span>
        <span style="font-size:12px;color:#999">${(h.sections||[]).length} section(s)</span>
        <span style="color:#bbb;margin-left:8px">▾</span>
      </div>
      <div class="hcard-body" id="hb${idx}">
        ${secs}${results}
        ${h.error?'<div style="color:#dc2626;margin-top:6px">❌ '+h.error+'</div>':''}
        ${log?'<pre>'+log+'</pre>':''}
      </div>
    </div>`;
  }).join('');
}

function toggleCard(id){const el=document.getElementById(id);el.classList.toggle('on');}

function filterHist(f,btn){
  histFilter=f;
  document.querySelectorAll('.flt').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  const filtered=f==='all'?allHistory:
    f==='success'||f==='error'?allHistory.filter(h=>h.status===f):
    allHistory.filter(h=>h.type===f);
  renderHistory(filtered);
}

async function loadHistory(){
  try{
    const r=await fetch('/history');allHistory=await r.json();
    renderStats(allHistory);
    const filtered=histFilter==='all'?allHistory:
      histFilter==='success'||histFilter==='error'?allHistory.filter(h=>h.status===histFilter):
      allHistory.filter(h=>h.type===histFilter);
    renderHistory(filtered);
  }catch(e){document.getElementById('histList').innerHTML='<div style="color:#dc2626;padding:16px">Lỗi tải lịch sử</div>';}
}

// ── Init ───────────────────────────────────────────────────────────────────
document.getElementById('cookie').addEventListener('input',save);
document.getElementById('clearBefore').addEventListener('change',save);

restore().then(()=>{
  if(!document.querySelectorAll('.sec').length) addSec();
  loadScheduleStatus();
});
</script>
</body>
</html>"""

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index(): return HTML

@app.route("/tabs")
def tabs_route():
    url = request.args.get("url","").strip()
    if not url: return jsonify({"error":"Thiếu URL"}), 400
    try:
        return jsonify({"tabs": get_sheet_tabs(parse_sheet_id(url))})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/upload", methods=["POST"])
def upload():
    data         = request.get_json()
    cookie_token = data.get("cookie_token","").strip()
    sections     = data.get("sections",[])
    clear_before = data.get("clear_before_upload", True)

    log_queue = queue.Queue()

    def worker():
        logs = []
        def log(msg): logs.append(msg); log_queue.put(("log", msg))
        started = datetime.now().isoformat()
        try:
            results = run_upload(cookie_token, sections, clear_before, log=log)
            log_queue.put(("done", results))
            append_history({"id":uuid.uuid4().hex[:8],"type":"manual","status":"success",
                            "started":started,"finished":datetime.now().isoformat(),
                            "sections":sections,"results":results,"error":"","log":logs})
        except Exception as e:
            log_queue.put(("error", str(e)))
            append_history({"id":uuid.uuid4().hex[:8],"type":"manual","status":"error",
                            "started":started,"finished":datetime.now().isoformat(),
                            "sections":sections,"results":[],"error":str(e),"log":logs})

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        while True:
            kind, payload = log_queue.get()
            if kind == "log":
                yield "data: " + json.dumps({"type":"log","msg":payload}) + "\n\n"
            elif kind == "done":
                yield "data: " + json.dumps({"type":"done","results":payload}) + "\n\n"; break
            elif kind == "error":
                yield "data: " + json.dumps({"type":"error","msg":payload}) + "\n\n"; break

    return Response(stream_with_context(generate()), mimetype="text/event-stream")

@app.route("/download_csv", methods=["POST"])
def download_csv():
    data      = request.get_json()
    sheet_url = data.get("sheet_url","").strip()
    tab_name  = data.get("tab_name","").strip()
    if not sheet_url or not tab_name:
        return jsonify({"error":"Thiếu sheet_url hoặc tab_name"}), 400
    try:
        sheet_id = parse_sheet_id(sheet_url)
        tnv, user = extract_phones(sheet_id, tab_name, log=lambda m: None)
        phones = tnv + user
        csv_bytes = ("User ID\n" + "\n".join(phones)).encode("utf-8")
        fname = f"{tab_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(csv_bytes, mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={fname}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/schedule", methods=["GET"])
def get_schedule(): return jsonify(load_sched())

@app.route("/schedule", methods=["POST"])
def post_schedule():
    data = request.get_json()
    cfg  = load_sched()
    cfg.update({
        "enabled":      data.get("enabled", False),
        "cookie_token": data.get("cookie_token", cfg["cookie_token"]),
        "sections":     data.get("sections", cfg["sections"]),
        "clear_before": data.get("clear_before", cfg.get("clear_before", True)),
    })
    save_sched(cfg)
    return jsonify({"ok": True})

@app.route("/history")
def history(): return jsonify(load_history())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"🍊 User Group Uploader → http://localhost:{port}")
    threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    app.run(port=port, debug=False, use_reloader=False)
