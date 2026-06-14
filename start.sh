#!/bin/bash
cd "$(dirname "$0")"

if ! command -v python3 &>/dev/null; then
  echo "Cần cài Python3. Tải tại https://python.org"
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Đang cài dependencies lần đầu..."
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt -q
fi

echo "Mở trình duyệt tại http://localhost:5001 ..."
open "http://localhost:5001" 2>/dev/null || true
.venv/bin/python app.py
