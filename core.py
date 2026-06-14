"""
Core: Google Sheet (public) → extract phones → upload to ShopeeFood User Tag
Auth bằng cookie token, gọi API trực tiếp (không cần Playwright)
"""

import re
import uuid
import time
import requests
import pandas as pd
from datetime import datetime
from io import StringIO


BASE_URL = "https://food-admin.shopee.vn"


# ── Google Sheet ───────────────────────────────────────────────────────────────

def parse_sheet_id(url: str) -> str:
    m = re.search(r"/spreadsheets/(?:u/\d+/)?d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError("URL Google Sheet không hợp lệ")
    return m.group(1)


def get_sheet_tabs(sheet_id: str) -> list[str]:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    res = requests.get(url, timeout=15)
    if res.status_code != 200:
        raise RuntimeError(f"Không đọc được sheet (status {res.status_code}). Kiểm tra sheet đã share public chưa?")
    tabs = re.findall(r'class="[^"]*sheet[^"]*"[^>]*>([^<]+)<', res.text)
    if not tabs:
        raise RuntimeError("Không lấy được tên tab. Kiểm tra sheet đã share public chưa?")
    return list(dict.fromkeys(tabs))


def read_tab_csv(sheet_id: str, tab_name: str) -> pd.DataFrame:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={requests.utils.quote(tab_name)}"
    res = requests.get(url, timeout=15)
    res.raise_for_status()
    return pd.read_csv(StringIO(res.text), dtype=str)


# ── Extract phones ─────────────────────────────────────────────────────────────

def extract_phones(sheet_id: str, tab_name: str, log=print) -> tuple[list[str], list[str]]:
    tabs = get_sheet_tabs(sheet_id)
    log(f"Tabs: {', '.join(tabs)}")

    def normalize(s): return s.replace("/","").replace("-","").replace(".","").replace(" ","")
    matched = next((t for t in tabs if normalize(t)[:4] == normalize(tab_name)[:4]), None)
    if not matched:
        raise RuntimeError(f"Không tìm thấy tab '{tab_name}'. Các tab: {tabs}")

    log(f"Đọc tab: '{matched}'")
    df = read_tab_csv(sheet_id, matched)

    phone_col = next(
        (c for c in df.columns if isinstance(c, str) and len(c) < 30 and
         any(kw in c.lower() for kw in ["điện thoại", "phone", "sdt", "mobile"])),
        None
    ) or next(
        (c for c in df.columns if isinstance(c, str) and
         any(kw in c.lower() for kw in ["điện thoại", "phone", "sdt", "mobile"])),
        None
    )
    note_col = next(
        (c for c in df.columns if isinstance(c, str) and "note" in c.lower()), None
    )

    if phone_col is None:
        raise RuntimeError(f"Không tìm thấy cột SĐT. Columns: {list(df.columns)}")

    tnv, user = [], []
    for _, row in df.iterrows():
        phone = re.sub(r"\D", "", str(row.get(phone_col, "")))
        if len(phone) < 9:
            continue
        note = str(row.get(note_col, "")).strip().upper() if note_col else ""
        if note == "TNV":
            tnv.append(phone)
        else:
            user.append(phone)

    log(f"Tab '{matched}': TNV={len(tnv)}, User={len(user)}")
    return tnv, user


# ── Cookie / session helpers ───────────────────────────────────────────────────

def parse_cookies(cookie_str: str) -> dict:
    cookies = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies[name.strip()] = value.strip()
    return cookies


def get_csrf_token(cookies: dict) -> str:
    return cookies.get("csrfToken", cookies.get("csrftoken", ""))


def make_session(cookie_token: str) -> requests.Session:
    cookies = parse_cookies(cookie_token)
    if not cookies:
        raise RuntimeError("Thiếu token đăng nhập. Vui lòng nhập cookie từ browser.")
    csrf = get_csrf_token(cookies)
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update({
        "Content-Type": "application/json",
        "x-sf-csrf-token": csrf,
        "origin": BASE_URL,
        "referer": BASE_URL,
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    })
    return session


# ── ShopeeFood API ─────────────────────────────────────────────────────────────

def get_group_detail(group_name: str, session: requests.Session, log=print) -> dict:
    log(f"Lấy thông tin group '{group_name}'...")
    res = session.post(
        f"{BASE_URL}/tag-sys/api/v1/group/get_group_detail",
        json={"region": "VN", "group_name": group_name, "business_type": 1},
        timeout=15,
    )
    res.raise_for_status()
    data = res.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lỗi get_group_detail: {data.get('msg')}")
    return data["data"]["group_detail"]


def _call_upload_list(group_detail: dict, session: requests.Session,
                      operation_type: int, user_list: list, file_name: str = "") -> dict:
    payload = {
        "region": "VN",
        "business_type": 1,
        "entity_type": group_detail["entity_type"],
        "group_id": group_detail["group_id"],
        "group_type": group_detail["group_type"],
        "creator": group_detail["creator"],
        "file_name": file_name,
        "operation_type": operation_type,
        "user_list": user_list,
        "execute_type": 0,
        "expected_execute_time": 0,
    }
    res = session.post(f"{BASE_URL}/tag-sys/api/v1/group/upload_list", json=payload, timeout=30)
    res.raise_for_status()
    return res.json()



def clear_group(group_detail: dict, session: requests.Session, label: str, log=print):
    """Xóa toàn bộ user list trong group (operation_type=3), chờ task hoàn thành."""
    log(f"[{label}] Xóa danh sách cũ...")
    data = _call_upload_list(group_detail, session, operation_type=3, user_list=[], file_name="")
    if data.get("code") != 0:
        raise RuntimeError(f"[{label}] Clear lỗi: {data.get('msg')}")
    log(f"[{label}] Chờ clear xử lý (8s)...")
    time.sleep(8)
    log(f"[{label}] Đã xóa danh sách cũ ✓")


def phones_to_84(phones: list[str]) -> list[str]:
    result = []
    for p in phones:
        p = re.sub(r"\D", "", p)
        if not p:
            continue
        if p.startswith("0"):
            p = "84" + p[1:]
        elif not p.startswith("84"):
            p = "84" + p
        result.append(p)
    return result


def phones_to_csv_bytes(phones: list[str]) -> bytes:
    lines = ["User ID"] + phones_to_84(phones)
    return "\n".join(lines).encode("utf-8")


def upload_to_group(group_detail: dict, phones: list[str], session: requests.Session,
                    label: str, log=print):
    """Upload danh sách số điện thoại lên group (operation_type=1)."""
    user_list = [int(p) for p in phones_to_84(phones)]
    file_name = f"upl_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.csv"

    log(f"[{label}] Upload {len(user_list)} số → group_id={group_detail['group_id']}...")
    data = _call_upload_list(group_detail, session, operation_type=1,
                             user_list=user_list, file_name=file_name)
    if data.get("code") != 0:
        raise RuntimeError(f"[{label}] Upload lỗi: {data.get('msg')}")
    log(f"[{label}] Upload thành công ✓")


# ── Entry point ────────────────────────────────────────────────────────────────

def run(sheet_url: str, cookie_token: str, sections: list[dict],
        clear_before_upload: bool = True, log=print):
    """
    sections = [{ "tab_name": "1106", "group_name": "Mai_Test_Hihub" }, ...]
    clear_before_upload: xóa toàn bộ list cũ trước khi upload
    """
    sheet_id = parse_sheet_id(sheet_url)
    log(f"Sheet ID: {sheet_id}")

    # Extract phones cho từng section trước
    section_data = []
    for sec in sections:
        tab_name   = sec.get("tab_name", "").strip()
        group_name = sec.get("group_name", "").strip()
        if not tab_name or not group_name:
            log(f"[SKIP] Section thiếu tab_name hoặc group_name")
            continue
        log(f"\n── Tab '{tab_name}' → Group '{group_name}' ──")
        tnv, user = extract_phones(sheet_id, tab_name, log=log)
        section_data.append({"tab_name": tab_name, "group_name": group_name, "tnv": tnv, "user": user})

    if not section_data:
        raise RuntimeError("Không có section hợp lệ nào")

    session = make_session(cookie_token)

    results = []
    for sec in section_data:
        group_name = sec["group_name"]
        tab        = sec["tab_name"]
        phones     = sec["tnv"] + sec["user"]

        if not phones:
            log(f"[{tab}] Không có SĐT — bỏ qua")
            continue

        group_detail = get_group_detail(group_name, session, log=log)

        if clear_before_upload:
            clear_group(group_detail, session, label=tab, log=log)
        upload_to_group(group_detail, phones, session, label=tab, log=log)
        results.append({
            "tab": tab, "group_name": group_name,
            "total": len(phones), "tnv": len(sec["tnv"]), "user": len(sec["user"])
        })

    log("\n✅ Hoàn tất!")
    return results
