import json
import queue
import threading
import os
import uuid
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, Response, stream_with_context, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from core import run, get_sheet_tabs, parse_sheet_id

app = Flask(__name__)

SCHEDULE_FILE = Path(__file__).parent / "schedule.json"
HISTORY_FILE  = Path(__file__).parent / "history.json"

# ── Persistence ────────────────────────────────────────────────────────────────

def load_schedule() -> dict:
    if SCHEDULE_FILE.exists():
        try:
            return json.loads(SCHEDULE_FILE.read_text())
        except Exception:
            pass
    return {"enabled": False, "run_times": ["08:00"], "dates": [], "sheet_url": "", "cookie_token": "", "sections": []}

def save_schedule(data: dict):
    SCHEDULE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def load_history() -> list:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            pass
    return []

def append_history(record: dict):
    history = load_history()
    history.insert(0, record)
    history = history[:200]  # keep last 200
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2))

# ── Scheduler ─────────────────────────────────────────────────────────────────

def do_run(sheet_url, cookie_token, sections, run_type="manual", log=print, clear_before_upload=False):
    record_id = str(uuid.uuid4())[:8]
    started   = datetime.now().isoformat()
    logs      = []

    def _log(msg):
        logs.append(msg)
        log(msg)

    try:
        results = run(sheet_url, cookie_token, sections,
                      clear_before_upload=clear_before_upload, log=_log)
        append_history({
            "id": record_id, "type": run_type, "status": "success",
            "started": started, "finished": datetime.now().isoformat(),
            "sheet_url": sheet_url, "sections": sections,
            "results": results, "error": "", "log": logs,
        })
        return results
    except Exception as e:
        append_history({
            "id": record_id, "type": run_type, "status": "error",
            "started": started, "finished": datetime.now().isoformat(),
            "sheet_url": sheet_url, "sections": sections,
            "results": [], "error": str(e), "log": logs,
        })
        raise

def scheduled_job():
    cfg = load_schedule()
    if not cfg.get("enabled"):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if today not in cfg.get("dates", []):
        return
    logs = []
    try:
        do_run(cfg["sheet_url"], cfg["cookie_token"], cfg["sections"],
               run_type="scheduled", log=lambda m: logs.append(m))
    except Exception:
        pass

scheduler = BackgroundScheduler()

def rebuild_scheduler(run_times: list):
    for job in scheduler.get_jobs():
        if job.id.startswith("sched_"):
            job.remove()
    for i, t in enumerate(run_times):
        try:
            h, m = t.split(":")
            scheduler.add_job(scheduled_job, "cron", hour=int(h), minute=int(m), id=f"sched_{i}")
        except Exception:
            pass

_cfg = load_schedule()
rebuild_scheduler(_cfg.get("run_times", ["08:00"]))
scheduler.start()

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/tabs")
def tabs():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "Thiếu URL"}), 400
    try:
        sheet_id = parse_sheet_id(url)
        tab_list = get_sheet_tabs(sheet_id)
        return jsonify({"tabs": tab_list})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/upload", methods=["POST"])
def upload():
    data         = request.get_json()
    sheet_url    = data.get("sheet_url", "").strip()
    cookie_token        = data.get("cookie_token", "").strip()
    sections            = data.get("sections", [])
    clear_before_upload = data.get("clear_before_upload", True)

    if not sheet_url:
        return Response("data: " + json.dumps({"type":"error","msg":"Thiếu Google Sheet URL"}) + "\n\n", mimetype="text/event-stream")
    if not sections:
        return Response("data: " + json.dumps({"type":"error","msg":"Chưa thêm section nào"}) + "\n\n", mimetype="text/event-stream")

    log_queue: queue.Queue = queue.Queue()

    def worker():
        def log(msg):
            log_queue.put(("log", msg))
        try:
            results = do_run(sheet_url, cookie_token, sections, run_type="manual",
                             log=log, clear_before_upload=clear_before_upload)
            log_queue.put(("done", results))
        except Exception as e:
            log_queue.put(("error", str(e)))

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        while True:
            kind, payload = log_queue.get()
            if kind == "log":
                yield "data: " + json.dumps({"type": "log", "msg": payload}) + "\n\n"
            elif kind == "done":
                yield "data: " + json.dumps({"type": "done", "results": payload}) + "\n\n"
                break
            elif kind == "error":
                yield "data: " + json.dumps({"type": "error", "msg": payload}) + "\n\n"
                break

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/schedule", methods=["GET"])
def get_schedule():
    return jsonify(load_schedule())


@app.route("/schedule", methods=["POST"])
def post_schedule():
    data = request.get_json()
    cfg  = load_schedule()
    cfg.update({
        "enabled":      data.get("enabled", cfg["enabled"]),
        "run_times":    data.get("run_times", cfg.get("run_times", ["08:00"])),
        "dates":        data.get("dates", cfg["dates"]),
        "sheet_url":    data.get("sheet_url", cfg["sheet_url"]),
        "cookie_token": data.get("cookie_token", cfg["cookie_token"]),
        "sections":     data.get("sections", cfg["sections"]),
    })
    save_schedule(cfg)
    rebuild_scheduler(cfg["run_times"])
    return jsonify({"ok": True})


@app.route("/history")
def history():
    return jsonify(load_history())


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5001)), use_reloader=False)
