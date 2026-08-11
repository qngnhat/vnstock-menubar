#!/bin/bash
# Start app menubar ở nền — đóng terminal vẫn sống. Không tự chạy lại khi reboot.
cd "$(dirname "$0")"

# Đã chạy rồi thì thôi, tránh mở 2 icon.
if pgrep -f "$(pwd)/.venv/bin/python app.py" >/dev/null 2>&1; then
  echo "App đang chạy sẵn rồi."
  exit 0
fi

# Tạo/sửa venv khi cần (giống run.sh).
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import objc, requests" 2>/dev/null; then
  rm -rf .venv
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

nohup .venv/bin/python app.py >> vnstock.log 2>&1 &
disown
echo "Đã chạy nền (PID $!). Đóng terminal thoải mái. Xem log: tail -f vnstock.log"
