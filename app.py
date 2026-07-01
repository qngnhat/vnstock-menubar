#!/usr/bin/env python3
"""VN Stock menu bar app — icon trên topbar, click xem giá watchlist."""

import json
import os
from datetime import datetime

import requests
import rumps
from AppKit import (
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMutableAttributedString,
)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
API_URL = "https://api-finfo.vndirect.com.vn/v4/stock_prices"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def _rgb(r, g, b):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)


# Màu theo bảng giá VN — RGB đậm/tươi tự set để nổi trên nền menu
# (màu systemXxx của macOS bị làm nhạt, lu mờ trên background).
STATUS_COLORS = {
    "ceiling": lambda: _rgb(0.80, 0.40, 1.00),  # trần: tím tươi
    "floor": lambda: _rgb(0.00, 0.80, 0.85),    # sàn: cyan đậm
    "up": lambda: _rgb(0.15, 0.85, 0.35),       # tăng: xanh lá tươi
    "down": lambda: _rgb(1.00, 0.30, 0.30),     # giảm: đỏ tươi
    "ref": lambda: _rgb(1.00, 0.75, 0.10),      # tham chiếu: vàng đậm
}


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("watchlist", ["HPG"])
    cfg.setdefault("refresh_seconds", 30)
    return cfg


def fetch_one(code):
    """Lấy dòng giá mới nhất (ngày gần nhất) của 1 mã.

    Dùng stock_prices?sort=date:desc&size=1 để có close (giá hiện tại),
    basicPrice (tham chiếu) và pctChange (% thay đổi TRONG NGÀY).
    Raise nếu network/API lỗi — caller tự bắt.
    """
    resp = requests.get(
        API_URL,
        params={"sort": "date:desc", "q": f"code:{code}", "size": 1},
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        return {"code": code, "price": None}
    row = data[0]
    return {
        "code": code,
        "price": row.get("close"),
        "change": row.get("change"),
        "changePct": row.get("pctChange"),
        "ceiling": row.get("ceilingPrice"),
        "floor": row.get("floorPrice"),
        "ref": row.get("basicPrice"),
        "volume": row.get("nmVolume"),
    }


def fetch_prices(codes):
    """Lấy giá từng mã trong watchlist, giữ đúng thứ tự. Trả list dict."""
    return [fetch_one(c) for c in codes]


def price_status(stock):
    """Phân loại trạng thái giá -> key trong STATUS_COLORS.

    Ưu tiên trần/sàn trước (so khớp giá trần/sàn), rồi mới tăng/giảm/tham chiếu.
    """
    price = stock.get("price")
    if price is None:
        return "ref"
    ceiling, floor = stock.get("ceiling"), stock.get("floor")
    if ceiling is not None and price >= ceiling:
        return "ceiling"
    if floor is not None and price <= floor:
        return "floor"
    change = stock.get("change") or 0.0
    if change > 0:
        return "up"
    if change < 0:
        return "down"
    return "ref"


def format_volume(vol):
    """Khối lượng -> gọn: 13.9M / 1.23M / 950K."""
    if not vol:
        return "0"
    if vol >= 1_000_000:
        return f"{vol / 1_000_000:.1f}M"
    if vol >= 1_000:
        return f"{vol / 1_000:.0f}K"
    return f"{int(vol)}"


def format_row(stock):
    """1 mã -> (text, chg_start, chg_len).

    chg_start/chg_len = vị trí đoạn '▲+2.99%' để tô màu riêng; phần còn lại
    (mã, giá, volume) để labelColor cho rõ. VD: 'HPG   23.45  ▲+2.99%   13.9M'.
    """
    code = stock["code"]
    price = stock.get("price")
    if price is None:
        text = f"{code:<5} —"
        return text, 0, 0
    change = stock.get("change") or 0.0
    pct = stock.get("changePct") or 0.0
    arrow = "▲" if change > 0 else ("▼" if change < 0 else "—")
    sign = "+" if change > 0 else ""
    vol = format_volume(stock.get("volume"))
    chg = f"{arrow}{sign}{pct:.2f}%"
    prefix = f"{code:<5} {price:>7.2f}  "
    text = f"{prefix}{chg}   {vol:>6}"
    return text, len(prefix), len(chg)


def set_colored_title(menu_item, text, chg_start, chg_len, status):
    """Set title menu item: cả dòng semibold + labelColor, riêng đoạn %[chg] tô màu.

    NSMenuItem không tô màu chữ mặc định; phải đi qua attributedTitle. Dùng
    mutable string để set nhiều màu theo range trên cùng 1 dòng.
    """
    astr = NSMutableAttributedString.alloc().initWithString_(text)
    full = (0, len(text))
    semibold = NSFont.systemFontOfSize_weight_(0, 0.3)  # ~semibold, dày dễ đọc
    astr.addAttribute_value_range_(NSFontAttributeName, semibold, full)
    astr.addAttribute_value_range_(
        NSForegroundColorAttributeName, NSColor.labelColor(), full
    )
    if chg_len:
        astr.addAttribute_value_range_(
            NSForegroundColorAttributeName, STATUS_COLORS[status](), (chg_start, chg_len)
        )
    menu_item._menuitem.setAttributedTitle_(astr)


class StockBarApp(rumps.App):
    def __init__(self):
        super().__init__("📈", quit_button=None)
        self.cfg = load_config()
        self.last_stocks = None  # giữ giá cũ khi fetch fail

        # Build menu shell 1 lần; nội dung mã cập nhật sau qua refresh().
        self._row_keys = self.cfg["watchlist"]
        self.menu = [
            *self._row_keys,
            rumps.separator,
            "status",
            rumps.separator,
            rumps.MenuItem("Refresh now", callback=self.on_refresh),
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]
        self.menu["status"].set_callback(None)  # dòng status không click được

        self.refresh(None)  # fetch ngay khi mở
        self.timer = rumps.Timer(self.refresh, self.cfg["refresh_seconds"])
        self.timer.start()

    def on_refresh(self, _):
        self.refresh(None)

    def refresh(self, _):
        now = datetime.now().strftime("%H:%M:%S")
        try:
            stocks = fetch_prices(self._row_keys)
            self.last_stocks = stocks
            for s in stocks:
                text, chg_start, chg_len = format_row(s)
                set_colored_title(
                    self.menu[s["code"]], text, chg_start, chg_len, price_status(s)
                )
            self.menu["status"].title = f"Cập nhật: {now}"
            self.title = "📈"
        except Exception:
            # Giữ giá cũ, chỉ báo lỗi ở dòng status + đổi icon.
            self.menu["status"].title = f"⚠️ Không cập nhật được ({now})"
            self.title = "⚠️"


if __name__ == "__main__":
    StockBarApp().run()
