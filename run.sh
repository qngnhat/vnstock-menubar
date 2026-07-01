#!/bin/bash
# Chạy app trong venv riêng; tự tạo venv + cài deps lần đầu.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

exec .venv/bin/python app.py
