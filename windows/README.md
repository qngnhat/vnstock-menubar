# VN Stock — bản Windows (system tray)

Bản port của `../app.py` (macOS menubar) sang Windows. Icon nằm ở system tray:

- **Click trái**: mở panel giá (VN-Index, watchlist, sparkline, giá mua/vol, tổng lãi/lỗ, stop/T+ của bot). Click ra ngoài thì panel tự đóng.
- **Hover**: tooltip hiện lãi/lỗ tổng và hôm nay (thay cho chữ trên menubar bản mac).
- **Chuột phải**: Refresh now / Quit.
- Icon đổi sang đỏ khi lần fetch gần nhất bị lỗi.

## Chạy

Cần Python 3 (launcher `py`).

| File | Việc |
|---|---|
| `run.bat` | Chạy kèm console để xem log. Lần đầu tự tạo `.venv` và cài deps |
| `start.bat` | Chạy nền, không console, log ghi vào `vnstock.log` |
| `install-startup.bat` | Tạo shortcut trong thư mục Startup để tự chạy khi đăng nhập |

Mỗi lúc chỉ chạy một instance (lock file trong %TEMP%).

## Config

`windows/config.json` (tự tạo khi chạy lần đầu). Schema giống bản mac nên copy `../config.json` sang dùng luôn được: `watchlist`, `refresh_seconds`, `targets`, `quantities`, `dates`, `sync {url, key}`.
