#!/bin/bash
# Chạy app trong venv riêng; tự tạo/sửa venv + cài deps khi cần.
set -e
cd "$(dirname "$0")"

# Tạo venv nếu chưa có, hoặc nếu venv hỏng (thường do move project sang path khác).
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import rumps, requests" 2>/dev/null; then
  rm -rf .venv
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

exec .venv/bin/python app.py
