#!/usr/bin/env python3
"""
User Group Uploader — single-file version
Chạy: python tool.py
Mở:  http://localhost:5001
"""

import re, uuid, time, json, queue, threading, os, webbrowser
import requests, pandas as pd
from datetime import datetime
from io import StringIO
from flask import Flask, Response, request, stream_with_context, jsonify

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
    log(f"Tabs: {', '.join(tabs)}")
    def norm(s): return s.replace("/","").replace("-","").replace(".","").replace(" ","")
    matched = next((t for t in tabs if norm(t)[:4] == norm(tab_name)[:4]), None)
    if not matched: raise RuntimeError(f"Không tìm thấy tab '{tab_name}'. Các tab: {tabs}")
    log(f"Đọc tab: '{matched}'")
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
    log(f"Tab '{matched}': TNV={len(tnv)}, User={len(user)}")
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
    log(f"Lấy thông tin group '{group_name}'...")
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

def run_upload(sheet_url, cookie_token, sections, clear_before=True, log=print):
    sheet_id = parse_sheet_id(sheet_url)
    log(f"Sheet ID: {sheet_id}")
    section_data = []
    for sec in sections:
        tab, grp = sec.get("tab_name","").strip(), sec.get("group_name","").strip()
        if not tab or not grp: continue
        log(f"\n── Tab '{tab}' → Group '{grp}' ──")
        tnv, user = extract_phones(sheet_id, tab, log=log)
        section_data.append({"tab":tab,"group":grp,"phones":tnv+user})
    if not section_data: raise RuntimeError("Không có section hợp lệ nào")
    session = make_session(cookie_token)
    results = []
    for sec in section_data:
        phones = sec["phones"]
        if not phones: log(f"[{sec['tab']}] Không có SĐT — bỏ qua"); continue
        gd = get_group_detail(sec["group"], session, log=log)
        if clear_before:
            log(f"[{sec['tab']}] Xóa danh sách cũ...")
            call_upload(gd, session, 3, [], "")
            log(f"[{sec['tab']}] Chờ 8s...")
            time.sleep(8)
            log(f"[{sec['tab']}] Đã xóa ✓")
        fname = f"upl_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.csv"
        user_list = [int(p) for p in phones]
        log(f"[{sec['tab']}] Upload {len(user_list)} số...")
        call_upload(gd, session, 1, user_list, fname)
        log(f"[{sec['tab']}] Upload thành công ✓")
        results.append({"tab":sec["tab"],"group_name":sec["group"],"total":len(phones)})
    log("\n✅ Hoàn tất!")
    return results

# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__)

HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>User Group Uploader</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f0f2f5;min-height:100vh;padding:32px 16px}
.card{background:#fff;border-radius:12px;box-shadow:0 2px 16px rgba(0,0,0,.10);width:100%;max-width:720px;margin:0 auto;overflow:hidden}
.hdr{background:#ee4d2d;padding:20px 28px}.hdr h1{color:#fff;font-size:18px;font-weight:600}.hdr p{color:rgba(255,255,255,.75);font-size:12px;margin-top:2px}
.body{padding:28px}
label{display:block;font-size:13px;font-weight:500;color:#333;margin-bottom:6px}
.req{color:#ee4d2d}
.field{margin-bottom:16px}
input,select,textarea{width:100%;padding:10px 12px;border:1px solid #d9d9d9;border-radius:8px;font-size:14px;color:#222;outline:none;transition:border-color .2s;background:#fff}
input:focus,select:focus,textarea:focus{border-color:#ee4d2d;box-shadow:0 0 0 2px rgba(238,77,45,.12)}
textarea{resize:vertical;min-height:72px;font-family:monospace;font-size:12px}
.row{display:flex;gap:8px;align-items:flex-end}.row input{flex:1}
.btn{padding:10px 18px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:.15s}
.btn-primary{background:#ee4d2d;color:#fff;width:100%;margin-top:4px;padding:12px}
.btn-primary:hover{background:#d94327}.btn-primary:disabled{opacity:.6;cursor:not-allowed}
.btn-sm{padding:8px 12px;font-size:12px;background:#fff;border:1px solid #d9d9d9;color:#555;border-radius:6px;cursor:pointer;white-space:nowrap}
.btn-sm:hover{border-color:#ee4d2d;color:#ee4d2d}
.btn-add{width:100%;padding:10px;border:1.5px dashed #d9d9d9;border-radius:8px;background:#fff;color:#888;font-size:13px;cursor:pointer;margin-bottom:16px}
.btn-add:hover{border-color:#ee4d2d;color:#ee4d2d}
.sec{border:1px solid #e8e8e8;border-radius:10px;padding:16px;margin-bottom:10px;position:relative;background:#fafafa}
.sec-num{position:absolute;top:-10px;left:14px;background:#ee4d2d;color:#fff;font-size:11px;font-weight:700;padding:2px 8px;border-radius:10px}
.sec-rm{position:absolute;top:10px;right:12px;background:none;border:none;cursor:pointer;color:#bbb;font-size:16px}.sec-rm:hover{color:#ee4d2d}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:480px){.g2{grid-template-columns:1fr}}
.toggle-row{display:flex;align-items:center;gap:8px;margin-bottom:16px;font-size:13px;color:#555}
.divider{height:1px;background:#f0f0f0;margin:20px 0}
#logBox{display:none;background:#111;border-radius:8px;padding:14px 16px;font-family:monospace;font-size:12px;max-height:280px;overflow-y:auto;margin-top:16px}
#logBox.on{display:block}
.ll{padding:1px 0;line-height:1.6;color:#ccc}
.ll.ok{color:#4ade80}.ll.er{color:#f87171}.ll.sep{color:#facc15}.ll.info{color:#60a5fa}
#resBox{margin-top:12px}
.res-card{background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:14px 16px;margin-bottom:8px}
.res-card h4{font-size:13px;font-weight:600;color:#166534;margin-bottom:4px}
.res-card p{font-size:12px;color:#15803d}
.spin{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.4);border-top-color:#fff;border-radius:50%;animation:sp .6s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes sp{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="card">
  <div class="hdr"><h1>🍊 User Group Uploader</h1><p>Upload số điện thoại từ Google Sheet → ShopeeFood User Tag</p></div>
  <div class="body">
    <div class="field">
      <label>Cookie Token <span class="req">*</span></label>
      <textarea id="cookie" placeholder="Paste cookie từ DevTools (F12 → Network → Request Headers → Cookie)"></textarea>
    </div>
    <div class="field">
      <label>Google Sheet URL <span class="req">*</span></label>
      <div class="row">
        <input type="url" id="sheet_url" placeholder="https://docs.google.com/spreadsheets/d/...">
        <button class="btn-sm" id="btnFetch" onclick="fetchTabs()">Lấy tabs</button>
      </div>
    </div>
    <div class="divider"></div>
    <div id="secList"></div>
    <button class="btn-add" onclick="addSec()">+ Thêm Section</button>
    <div class="toggle-row">
      <input type="checkbox" id="clearBefore" checked>
      <label for="clearBefore" style="margin:0">Xóa danh sách cũ trước khi upload</label>
    </div>
    <button class="btn btn-primary" id="btnGo" onclick="doUpload()">🚀 Bắt đầu Upload</button>
    <div id="logBox"></div>
    <div id="resBox"></div>
  </div>
</div>
<script>
const LS = 'ug_v2';
let tabs = [], n = 0;

function ddmm(){ const d=new Date(); return String(d.getDate()).padStart(2,'0')+String(d.getMonth()+1).padStart(2,'0'); }

function save(){
  const secs=[];
  document.querySelectorAll('.sec').forEach(c=>{
    const i=c.id.slice(2);
    secs.push({tab:(document.getElementById('t'+i)||{}).value||'',grp:(document.getElementById('g'+i)||{}).value||''});
  });
  localStorage.setItem(LS,JSON.stringify({cookie:document.getElementById('cookie').value,sheet_url:document.getElementById('sheet_url').value,clear:document.getElementById('clearBefore').checked,secs}));
}

async function restore(){
  const raw=localStorage.getItem(LS); if(!raw)return;
  const d=JSON.parse(raw);
  document.getElementById('cookie').value=d.cookie||'';
  document.getElementById('sheet_url').value=d.sheet_url||'';
  document.getElementById('clearBefore').checked=d.clear!==false;
  if(d.sheet_url){ await fetchTabs(true); }
  (d.secs||[]).forEach(s=>addSec(s.tab,s.grp));
  if(!d.secs||!d.secs.length) addSec();
}

async function fetchTabs(silent=false){
  const url=document.getElementById('sheet_url').value.trim();
  if(!url){if(!silent)alert('Nhập Google Sheet URL');return;}
  const btn=document.getElementById('btnFetch');
  btn.disabled=true;btn.textContent='⏳';
  try{
    const r=await fetch('/tabs?url='+encodeURIComponent(url));
    const d=await r.json();
    if(d.error)throw new Error(d.error);
    tabs=d.tabs;
    document.querySelectorAll('.tab-sel').forEach(fillSel);
  }catch(e){if(!silent)alert('Lỗi: '+e.message);}
  btn.disabled=false;btn.textContent='Lấy tabs';
}

function fillSel(sel){
  const cur=sel.value;
  sel.innerHTML='<option value="">-- Chọn tab --</option>';
  tabs.forEach(t=>{const o=document.createElement('option');o.value=t;o.textContent=t;if(t===cur)o.selected=true;sel.appendChild(o);});
}

function addSec(tabVal='',grpVal=''){
  n++;const i=n;
  const div=document.createElement('div');
  div.className='sec';div.id='sc'+i;
  div.innerHTML=`<span class="sec-num">#${i}</span>
    <button class="sec-rm" onclick="document.getElementById('sc${i}').remove();save()">✕</button>
    <div class="g2">
      <div class="field" style="margin:0"><label>Tên tab <span class="req">*</span></label>
        <select class="tab-sel" id="t${i}"><option value="">-- Chọn tab --</option></select></div>
      <div class="field" style="margin:0"><label>User Group Name <span class="req">*</span></label>
        <input type="text" id="g${i}" placeholder="vd: Mai_Test_Hihub" value="${grpVal}" oninput="save()"></div>
    </div>
    <div style="margin-top:10px;text-align:right">
      <button class="btn-sm" onclick="dlCSV(${i})">⬇ Download CSV</button>
    </div>`;
  document.getElementById('secList').appendChild(div);
  const sel=document.getElementById('t'+i);
  if(tabs.length){
    fillSel(sel);
    if(tabVal) sel.value=tabVal;
    else{const today=tabs.find(t=>t.replace(/\D/g,'').startsWith(ddmm()));if(today)sel.value=today;}
  }
  sel.addEventListener('change',save);
}

function collectSecs(){
  const r=[];
  document.querySelectorAll('.sec').forEach(c=>{
    const i=c.id.slice(2);
    const tab=(document.getElementById('t'+i)||{}).value||'';
    const grp=(document.getElementById('g'+i)||{}).value||'';
    if(tab&&grp) r.push({tab_name:tab,group_name:grp});
  });
  return r;
}

function addLog(msg,cls=''){
  const b=document.getElementById('logBox');
  const l=document.createElement('div');l.className='ll'+(cls?' '+cls:'');l.textContent=msg;
  b.appendChild(l);b.scrollTop=b.scrollHeight;
}

async function doUpload(){
  const cookie=document.getElementById('cookie').value.trim();
  const sheet_url=document.getElementById('sheet_url').value.trim();
  const sections=collectSecs();
  const clear_before_upload=document.getElementById('clearBefore').checked;
  if(!cookie){alert('Nhập Cookie Token');return;}
  if(!sheet_url){alert('Nhập Google Sheet URL');return;}
  if(!sections.length){alert('Điền ít nhất 1 section');return;}
  save();
  const lb=document.getElementById('logBox'),rb=document.getElementById('resBox');
  lb.innerHTML='';lb.classList.add('on');rb.innerHTML='';
  const btn=document.getElementById('btnGo');
  btn.disabled=true;btn.innerHTML='<span class="spin"></span>Đang xử lý...';
  try{
    const res=await fetch('/upload',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cookie_token:cookie,sheet_url,sections,clear_before_upload})});
    const reader=res.body.getReader();const dec=new TextDecoder();let buf='';
    while(true){
      const{done,value}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n\n');buf=lines.pop();
      for(const line of lines){
        if(!line.startsWith('data: '))continue;
        const d=JSON.parse(line.slice(6));
        if(d.type==='log'){
          const cls=d.msg.includes('✓')?'ok':d.msg.includes('❌')||d.msg.includes('lỗi')?'er':d.msg.startsWith('──')?'sep':d.msg.startsWith('[')?'info':'';
          addLog(d.msg,cls);
        }else if(d.type==='done'){
          d.results.forEach(r=>{rb.innerHTML+=`<div class="res-card"><h4>✅ ${r.group_name}</h4><p>Tab: ${r.tab} | ${r.total} số</p></div>`;});
        }else if(d.type==='error'){
          addLog('❌ '+d.msg,'er');
          rb.innerHTML=`<div style="padding:14px;background:#fff1f2;border:1px solid #fca5a5;border-radius:8px;color:#991b1b;font-size:13px">❌ ${d.msg}</div>`;
        }
      }
    }
  }catch(e){addLog('❌ '+e.message,'er');}
  btn.disabled=false;btn.innerHTML='🚀 Bắt đầu Upload';
}

async function dlCSV(i){
  const sheet_url=document.getElementById('sheet_url').value.trim();
  const tab=(document.getElementById('t'+i)||{}).value||'';
  if(!sheet_url||!tab){alert('Nhập Sheet URL và chọn tab trước');return;}
  const btn=event.target;btn.disabled=true;btn.textContent='⏳';
  try{
    const res=await fetch('/download_csv',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sheet_url,tab_name:tab})});
    if(!res.ok){const e=await res.json();alert('Lỗi: '+e.error);return;}
    const blob=await res.blob();
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');a.href=url;
    a.download=res.headers.get('Content-Disposition')?.match(/filename=(.+)/)?.[1]||tab+'.csv';
    a.click();URL.revokeObjectURL(url);
  }finally{btn.disabled=false;btn.textContent='⬇ Download CSV';}
}

document.getElementById('cookie').addEventListener('input',save);
document.getElementById('sheet_url').addEventListener('input',save);
document.getElementById('clearBefore').addEventListener('change',save);

restore().then(()=>{ if(!document.querySelectorAll('.sec').length) addSec(); });
</script>
</body>
</html>"""

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return HTML

@app.route("/tabs")
def tabs():
    url = request.args.get("url","").strip()
    if not url: return jsonify({"error":"Thiếu URL"}), 400
    try:
        return jsonify({"tabs": get_sheet_tabs(parse_sheet_id(url))})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/upload", methods=["POST"])
def upload():
    data = request.get_json()
    sheet_url    = data.get("sheet_url","").strip()
    cookie_token = data.get("cookie_token","").strip()
    sections     = data.get("sections",[])
    clear_before = data.get("clear_before_upload", True)

    log_queue = queue.Queue()

    def worker():
        def log(msg): log_queue.put(("log", msg))
        try:
            results = run_upload(sheet_url, cookie_token, sections, clear_before, log=log)
            log_queue.put(("done", results))
        except Exception as e:
            log_queue.put(("error", str(e)))

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
    data     = request.get_json()
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"🍊 User Group Uploader → http://localhost:{port}")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    app.run(port=port, debug=False, use_reloader=False)
