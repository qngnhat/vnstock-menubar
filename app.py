#!/usr/bin/env python3
"""VN Stock menu bar app — icon trên topbar, click xem giá watchlist."""

import json
import os
from datetime import datetime

import requests
import rumps
from AppKit import (
    NSAttributedString,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
API_URL = "https://api-finfo.vndirect.com.vn/v4/stock_prices"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Màu theo trạng thái giá (đúng convention bảng giá VN).
STATUS_COLORS = {
    "ceiling": NSColor.systemPurpleColor,  # trần: tím
    "floor": NSColor.systemTealColor,      # sàn: cyan
    "up": NSColor.systemGreenColor,        # tăng: xanh
    "down": NSColor.systemRedColor,        # giảm: đỏ
    "ref": NSColor.systemYellowColor,      # tham chiếu: vàng
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
    """1 mã -> 1 dòng menu, VD: 'HPG   23.45  ▲+2.99%   13.9M'. ▲/▼ + màu."""
    code = stock["code"]
    price = stock.get("price")
    if price is None:
        return f"{code:<5} —"
    change = stock.get("change") or 0.0
    pct = stock.get("changePct") or 0.0
    arrow = "▲" if change > 0 else ("▼" if change < 0 else "—")
    sign = "+" if change > 0 else ""
    vol = format_volume(stock.get("volume"))
    return f"{code:<5} {price:>7.2f}  {arrow}{sign}{pct:.2f}%   {vol:>6}"


def set_colored_title(menu_item, text, status):
    """Set title cho menu item với màu theo status (NSAttributedString + font menu).

    NSMenuItem thường không tô màu chữ; phải đi qua attributedTitle của AppKit.
    """
    color = STATUS_COLORS[status]()
    font = NSFont.menuFontOfSize_(0)  # 0 = size mặc định của menu
    attrs = {
        NSForegroundColorAttributeName: color,
        NSFontAttributeName: font,
    }
    astr = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
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
                set_colored_title(self.menu[s["code"]], format_row(s), price_status(s))
            self.menu["status"].title = f"Cập nhật: {now}"
            self.title = "📈"
        except Exception:
            # Giữ giá cũ, chỉ báo lỗi ở dòng status + đổi icon.
            self.menu["status"].title = f"⚠️ Không cập nhật được ({now})"
            self.title = "⚠️"


if __name__ == "__main__":
    StockBarApp().run()
