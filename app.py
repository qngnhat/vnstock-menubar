#!/usr/bin/env python3
"""VN Stock menu bar app — icon trên topbar, click xem giá watchlist."""

import json
import os
from datetime import datetime

import requests
import rumps

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
API_URL = "https://api-finfo.vndirect.com.vn/v4/stock_prices"
HEADERS = {"User-Agent": "Mozilla/5.0"}


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
        return {"code": code, "price": None, "change": None, "changePct": None}
    row = data[0]
    return {
        "code": code,
        "price": row.get("close"),
        "change": row.get("change"),
        "changePct": row.get("pctChange"),
    }


def fetch_prices(codes):
    """Lấy giá từng mã trong watchlist, giữ đúng thứ tự. Trả list dict."""
    return [fetch_one(c) for c in codes]


def format_row(stock):
    """1 mã -> 1 dòng menu: 'HPG   23.45   ▲ +2.99%'. ▲/▼ thay cho màu."""
    code = stock["code"]
    price = stock.get("price")
    if price is None:
        return f"{code:<6} —"
    change = stock.get("change") or 0.0
    pct = stock.get("changePct") or 0.0
    arrow = "▲" if change > 0 else ("▼" if change < 0 else "—")
    sign = "+" if change > 0 else ""
    return f"{code:<6} {price:>7.2f}   {arrow} {sign}{pct:.2f}%"


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
                self.menu[s["code"]].title = format_row(s)
            self.menu["status"].title = f"Cập nhật: {now}"
            self.title = "📈"
        except Exception:
            # Giữ giá cũ, chỉ báo lỗi ở dòng status + đổi icon.
            self.menu["status"].title = f"⚠️ Không cập nhật được ({now})"
            self.title = "⚠️"


if __name__ == "__main__":
    StockBarApp().run()
