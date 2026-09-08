#!/bin/bash
# Start app menubar ở nền — đóng terminal vẫn sống. Không tự chạy lại khi reboot.
cd "$(dirname "$0")"

# Đã chạy rồi thì thôi, tránh mở 2 icon. Dùng pidfile chứ không pgrep theo
# command line: PyObjC exec lại qua Python.app nên cmdline thành đường dẫn
# framework, pattern nào khớp .venv cũng trượt -> guard không bao giờ nổ.
PIDFILE=.app.pid
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "App đang chạy sẵn rồi (PID $(cat "$PIDFILE"))."
  exit 0
fi

# Tạo/sửa venv khi cần (giống run.sh).
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import objc, requests" 2>/dev/null; then
  rm -rf .venv
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

nohup .venv/bin/python app.py >> vnstock.log 2>&1 &
APP_PID=$!
echo "$APP_PID" > "$PIDFILE"
disown
echo "Đã chạy nền (PID $APP_PID). Đóng terminal thoải mái. Xem log: tail -f vnstock.log"
