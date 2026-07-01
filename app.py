#!/usr/bin/env python3
"""VN Stock menu bar app — icon topbar, click bung panel nền trắng xem giá."""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
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
    NSUserInterfaceLayoutOrientationHorizontal,
    NSUserInterfaceLayoutOrientationVertical,
    NSVariableStatusItemLength,
    NSView,
    NSViewController,
)
from Cocoa import CAKeyframeAnimation
from Foundation import NSObject

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


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def normalize_code(raw):
    """Chuẩn hoá mã: bỏ khoảng trắng, viết hoa. '' nếu rỗng."""
    return (raw or "").strip().upper()


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
    """Lấy giá các mã SONG SONG, giữ đúng thứ tự. Trả list dict.

    ThreadPoolExecutor.map giữ order theo input; song song ~3x nhanh hơn tuần tự
    (mỗi mã 1 HTTP ~300ms, 5 mã tuần tự 1.3s -> song song ~0.4s).
    """
    if not codes:
        return []
    with ThreadPoolExecutor(max_workers=min(len(codes), 8)) as ex:
        return list(ex.map(fetch_one, codes))


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
    """App thuần PyObjC: NSStatusItem (icon) + NSPopover nền trắng. Fetch thủ công."""

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

        self.refresh_(None)  # fetch 1 lần lúc mở; sau đó chỉ fetch khi bấm Refresh
        return self

    # --- data ---
    def refresh_(self, _timer):
        """Kick off fetch trên background thread; UI không bao giờ đơ vì mạng."""
        codes = list(self.codes)  # snapshot, tránh race khi user đang edit
        threading.Thread(target=self._fetch_bg, args=(codes,), daemon=True).start()

    def _fetch_bg(self, codes):
        now = datetime.now().strftime("%H:%M:%S")
        try:
            stocks = fetch_prices(codes)
            result = {"stocks": stocks, "status": f"Cập nhật: {now}", "ok": True}
        except Exception:
            result = {"status": f"⚠️ Không cập nhật được ({now})", "ok": False}
        # AppKit: mọi update UI phải về main thread.
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "_applyFetchResult:", result, False
        )

    def _applyFetchResult_(self, result):
        if result["ok"]:
            self.last_stocks = result["stocks"]
            self.status_item.button().setTitle_("📈")
        else:
            self.status_item.button().setTitle_("⚠️")
        self.status_text = result["status"]
        if self.popover.isShown():
            self._rebuild_panel()

    # --- UI ---
    def _rebuild_panel(self):
        stack = NSStackView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
        stack.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
        stack.setAlignment_(NSLayoutAttributeLeading)
        stack.setSpacing_(5.0)

        # Iterate theo self.codes (nguồn chân lý) để tag nút ✕ = index trong codes,
        # tránh lệch khi fetch nền chưa kịp cập nhật last_stocks sau Add/Remove.
        by_code = {s["code"]: s for s in (self.last_stocks or [])}
        for i, code in enumerate(self.codes):
            s = by_code.get(code, {"code": code, "price": None})  # chưa fetch -> '—'
            row = NSStackView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
            row.setOrientation_(NSUserInterfaceLayoutOrientationHorizontal)
            row.setSpacing_(8.0)
            row.addArrangedSubview_(make_row_label(s))
            x_btn = NSButton.buttonWithTitle_target_action_("✕", self, "onRemove:")
            x_btn.setTag_(i)
            x_btn.setBezelStyle_(0)  # bezel gọn
            row.addArrangedSubview_(x_btn)
            stack.addArrangedSubview_(row)

        stack.addArrangedSubview_(
            make_text_label(getattr(self, "status_text", ""), GRAY(), weight=0.3)
        )

        # Dòng thêm mã: [ô nhập] [Add].
        add_row = NSStackView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
        add_row.setOrientation_(NSUserInterfaceLayoutOrientationHorizontal)
        add_row.setSpacing_(6.0)
        self.input_field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 90, 22))
        self.input_field.setEditable_(True)
        self.input_field.setPlaceholderString_("Mã CK…")
        self.input_field.setTarget_(self)
        self.input_field.setAction_("onAdd:")  # Enter trong ô = Add
        self.input_field.setWantsLayer_(True)  # để vẽ viền đỏ khi mã sai
        self.input_field.layer().setCornerRadius_(4.0)
        add_row.addArrangedSubview_(self.input_field)
        self.add_btn = NSButton.buttonWithTitle_target_action_("Add", self, "onAdd:")
        add_row.addArrangedSubview_(self.add_btn)
        stack.addArrangedSubview_(add_row)

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
        self.refresh_(None)  # fetch nền; thread xong tự rebuild panel

    def onQuit_(self, sender):
        NSApplication.sharedApplication().terminate_(sender)

    # --- edit watchlist ---
    def onAdd_(self, sender):
        self.input_field.layer().setBorderWidth_(0.0)  # clear viền đỏ lần trước
        code = normalize_code(self.input_field.stringValue())
        if not code:
            self._warn_invalid_input()
            return
        if code in self.codes:  # trùng -> báo lỗi luôn, khỏi gọi API
            self._warn_invalid_input()
            return
        # Check mã có thật qua API (off main thread, không block UI).
        self.add_btn.setEnabled_(False)
        self.add_btn.setTitle_("Checking…")
        threading.Thread(target=self._validate_bg, args=(code,), daemon=True).start()

    def _validate_bg(self, code):
        try:
            valid = fetch_one(code).get("price") is not None
        except Exception:
            valid = False  # lỗi mạng coi như chưa xác thực được -> báo lỗi
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "_afterValidate:", {"code": code, "valid": valid}, False
        )

    def _afterValidate_(self, result):
        self.add_btn.setEnabled_(True)
        self.add_btn.setTitle_("Add")
        if not result["valid"]:
            self._warn_invalid_input()
            return
        self.codes.append(result["code"])
        self.input_field.setStringValue_("")
        self._apply_watchlist_change()

    def _warn_invalid_input(self):
        """Ô nhập viền đỏ + rung để báo mã không hợp lệ."""
        layer = self.input_field.layer()
        layer.setBorderColor_(NSColor.systemRedColor().CGColor())
        layer.setBorderWidth_(2.0)
        f = self.input_field.frame()
        cx = f.origin.x + f.size.width / 2
        shake = CAKeyframeAnimation.animationWithKeyPath_("position.x")
        shake.setValues_([cx, cx - 6, cx + 6, cx - 4, cx + 4, cx])
        shake.setDuration_(0.3)
        layer.addAnimation_forKey_(shake, "shake")

    def onRemove_(self, sender):
        i = sender.tag()
        if 0 <= i < len(self.codes):
            del self.codes[i]
            self._apply_watchlist_change()

    def _apply_watchlist_change(self):
        """Ghi config.json + rebuild ngay (phản hồi tức thì) + fetch mã mới ở nền."""
        self.cfg["watchlist"] = self.codes
        save_config(self.cfg)
        self._rebuild_panel()  # hiện ngay list mới (mã mới = '—' tới khi fetch xong)
        self.refresh_(None)    # fetch nền, xong tự rebuild lại


if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # menu bar only
    delegate = StockBarApp.alloc().init()
    app.run()
