#!/usr/bin/env python3
"""VN Stock menu bar app — icon topbar, click bung panel nền trắng xem giá."""

import json
import os
from datetime import datetime

import objc
import requests
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSLayoutAttributeLeading,
    NSMakeRect,
    NSMutableAttributedString,
    NSPopover,
    NSPopoverBehaviorTransient,
    NSRectEdgeMaxY,
    NSStackView,
    NSStatusBar,
    NSTextField,
    NSUserInterfaceLayoutOrientationVertical,
    NSVariableStatusItemLength,
    NSView,
    NSViewController,
)
from Foundation import NSObject, NSTimer

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


ROW_FONT_SIZE = 13.0
BLACK = NSColor.blackColor
GRAY = lambda: _rgb(0.45, 0.45, 0.45)  # volume xám nhạt


def _mono(size=ROW_FONT_SIZE, weight=0.4):
    return NSFont.monospacedSystemFontOfSize_weight_(size, weight)


def make_row_label(stock):
    """1 mã -> NSTextField: mã/giá/volume ĐEN đậm, chỉ đoạn %[chg] tô màu status.

    Dùng monospaced font để mọi cột thẳng hàng (width cố định theo dòng dài nhất).
    """
    text, chg_start, chg_len = format_row(stock)
    astr = NSMutableAttributedString.alloc().initWithString_(text)
    astr.addAttribute_value_range_(NSFontAttributeName, _mono(), (0, len(text)))
    astr.addAttribute_value_range_(
        NSForegroundColorAttributeName, BLACK(), (0, len(text))
    )
    if chg_len:
        astr.addAttribute_value_range_(
            NSForegroundColorAttributeName,
            STATUS_COLORS[price_status(stock)](),
            (chg_start, chg_len),
        )
    return NSTextField.labelWithAttributedString_(astr)


def make_text_label(text, color, weight=0.4):
    astr = NSMutableAttributedString.alloc().initWithString_(text)
    astr.addAttribute_value_range_(NSFontAttributeName, _mono(weight=weight), (0, len(text)))
    astr.addAttribute_value_range_(NSForegroundColorAttributeName, color, (0, len(text)))
    return NSTextField.labelWithAttributedString_(astr)


class StockBarApp(NSObject):
    """App thuần PyObjC: NSStatusItem (icon) + NSPopover nền trắng + NSTimer."""

    def init(self):
        self = objc.super(StockBarApp, self).init()
        if self is None:
            return None
        self.cfg = load_config()
        self.codes = self.cfg["watchlist"]
        self.last_stocks = None

        # Status item + icon topbar; click button -> toggle popover.
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        btn = self.status_item.button()
        btn.setTitle_("📈")
        btn.setTarget_(self)
        btn.setAction_("togglePopover:")

        self.popover = NSPopover.alloc().init()
        self.popover.setBehavior_(NSPopoverBehaviorTransient)  # click ngoài -> đóng

        self.refresh_(None)  # fetch ngay
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            float(self.cfg["refresh_seconds"]), self, "refresh:", None, True
        )
        return self

    # --- data ---
    def refresh_(self, _timer):
        now = datetime.now().strftime("%H:%M:%S")
        try:
            self.last_stocks = fetch_prices(self.codes)
            self.status_text = f"Cập nhật: {now}"
            self.status_item.button().setTitle_("📈")
        except Exception:
            self.status_text = f"⚠️ Không cập nhật được ({now})"
            self.status_item.button().setTitle_("⚠️")
        # Nếu popover đang mở, dựng lại nội dung cho tươi.
        if self.popover.isShown():
            self._rebuild_panel()

    # --- UI ---
    def _rebuild_panel(self):
        stack = NSStackView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
        stack.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
        stack.setAlignment_(NSLayoutAttributeLeading)
        stack.setSpacing_(5.0)

        for s in (self.last_stocks or []):
            stack.addArrangedSubview_(make_row_label(s))

        stack.addArrangedSubview_(
            make_text_label(getattr(self, "status_text", ""), GRAY(), weight=0.3)
        )

        refresh_btn = NSButton.buttonWithTitle_target_action_(
            "Refresh now", self, "onRefresh:"
        )
        quit_btn = NSButton.buttonWithTitle_target_action_("Quit", self, "onQuit:")
        stack.addArrangedSubview_(refresh_btn)
        stack.addArrangedSubview_(quit_btn)

        fit = stack.fittingSize()
        pad = 14.0
        view = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, fit.width + 2 * pad, fit.height + 2 * pad)
        )
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(NSColor.whiteColor().CGColor())
        # Stack có translatesAutoresizingMask=True nên phải áp frame=fittingSize;
        # nếu chỉ set origin, stack kẹt 10x10 và các row chồng nhau ở góc.
        stack.setFrame_(NSMakeRect(pad, pad, fit.width, fit.height))
        view.addSubview_(stack)

        vc = NSViewController.alloc().init()
        vc.setView_(view)
        self.popover.setContentViewController_(vc)
        self.popover.setContentSize_((fit.width + 2 * pad, fit.height + 2 * pad))

    def togglePopover_(self, sender):
        if self.popover.isShown():
            self.popover.performClose_(sender)
        else:
            self._rebuild_panel()
            btn = self.status_item.button()
            self.popover.showRelativeToRect_ofView_preferredEdge_(
                btn.bounds(), btn, NSRectEdgeMaxY
            )

    def onRefresh_(self, sender):
        self.refresh_(None)
        self._rebuild_panel()  # cập nhật panel ngay

    def onQuit_(self, sender):
        NSApplication.sharedApplication().terminate_(sender)


if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # menu bar only
    delegate = StockBarApp.alloc().init()
    app.run()
