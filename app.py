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
    NSTextAlignmentRight,
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
INDEX_API_URL = "https://api-finfo.vndirect.com.vn/v4/vnmarket_prices"
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
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}  # chưa có config (clone mới) -> dựng từ default; save_config sẽ tạo file
    cfg.setdefault("watchlist", ["HPG"])
    cfg.setdefault("refresh_seconds", 30)
    cfg.setdefault("targets", {})     # {code: giá mua} user tự note
    cfg.setdefault("quantities", {})  # {code: số cp đã mua} để tính lãi/lỗ
    cfg.setdefault("dates", {})       # {code: "YYYY-MM-DD"} đóng dấu lúc vào vị thế -> bot tính T+
    cfg.setdefault("sync", {})        # {url, key} đẩy danh mục lên bot; thiếu = tắt sync
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def normalize_code(raw):
    """Chuẩn hoá mã: bỏ khoảng trắng, viết hoa. '' nếu rỗng."""
    return (raw or "").strip().upper()


def fetch_one(code, history=21):
    """Lấy giá mã + volume trung bình để so vol hôm nay cao/thấp.

    history phiên gần nhất (date:desc): phiên [0] = mới nhất (giá hiện tại),
    [1:] = lịch sử -> tính avgVolume so với vol hôm nay. Dùng history=1 khi
    chỉ cần kiểm tra mã tồn tại (validate). Raise nếu network/API lỗi.
    """
    resp = requests.get(
        API_URL,
        params={"sort": "date:desc", "q": f"code:{code}", "size": history},
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        return {"code": code, "price": None}
    row = data[0]
    # avgVolume = TB các phiên TRƯỚC hôm nay (bỏ [0] đang chạy dở trong phiên).
    hist_vols = [r.get("nmVolume") for r in data[1:] if r.get("nmVolume")]
    avg_vol = sum(hist_vols) / len(hist_vols) if hist_vols else None
    return {
        "code": code,
        "price": row.get("close"),
        "change": row.get("change"),
        "changePct": row.get("pctChange"),
        "ceiling": row.get("ceilingPrice"),
        "floor": row.get("floorPrice"),
        "ref": row.get("basicPrice"),
        "volume": row.get("nmVolume"),
        "avgVolume": avg_vol,
    }


def fetch_index(code="VNINDEX"):
    """Lấy điểm chỉ số (VN-Index) mới nhất từ endpoint vnmarket_prices.

    Index không nằm trong stock_prices; endpoint riêng trả close/change/pctChange.
    Raise nếu network/API lỗi — caller tự bắt.
    """
    resp = requests.get(
        INDEX_API_URL,
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
    """1 mã -> (text, chg_start, chg_len, vol_start, vol_len).

    chg = đoạn '▲+2.99%' tô màu status; vol = đoạn khối lượng tô màu cao/thấp.
    Phần còn lại (mã, giá) để đen cho rõ. VD: 'HPG   23.45  ▲+2.99%   13.9M'.
    """
    code = stock["code"]
    price = stock.get("price")
    if price is None:
        text = f"{code:<5} —"
        return text, 0, 0, 0, 0
    change = stock.get("change") or 0.0
    pct = stock.get("changePct") or 0.0
    arrow = "▲" if change > 0 else ("▼" if change < 0 else "—")
    sign = "+" if change > 0 else ""
    vol = f"{format_volume(stock.get('volume')):>6}"
    chg = f"{arrow}{sign}{pct:.2f}%"
    prefix = f"{code:<5} {price:>7.2f}  "
    mid = f"{prefix}{chg}   "
    text = f"{mid}{vol}"
    return text, len(prefix), len(chg), len(mid), len(vol)


def format_target_diff(price, target):
    """% chênh giá hiện tại so giá đối chiếu -> (text, status_key), hoặc None.

    price > target -> 'up' (đang cao hơn giá định vào), < -> 'down'. VD '+4.1%'.
    """
    if price is None or not target:
        return None
    pct = (price - target) / target * 100
    sign = "+" if pct > 0 else ""
    key = "up" if pct > 0 else ("down" if pct < 0 else "ref")
    return f"{sign}{pct:.1f}%", key


ROW_FONT_SIZE = 13.0
BLACK = NSColor.blackColor
GRAY = lambda: _rgb(0.45, 0.45, 0.45)  # volume bình thường: xám

# Màu volume theo tỉ lệ vol hôm nay / TB 20 phiên.
VOL_HIGH = lambda: _rgb(1.00, 0.50, 0.05)  # cao đột biến: cam đậm (có sóng)
VOL_LOW = lambda: _rgb(0.62, 0.70, 0.78)   # thấp: xám xanh nhạt (èo uột)
VOL_HIGH_RATIO = 1.5
VOL_LOW_RATIO = 0.5


def volume_color(stock):
    """Màu đoạn volume: cam nếu vol hôm nay cao đột biến so TB, xám xanh nếu thấp.

    Không có avgVolume (mã mới / chưa đủ lịch sử) -> xám thường.
    """
    vol, avg = stock.get("volume"), stock.get("avgVolume")
    if not vol or not avg:
        return GRAY()
    ratio = vol / avg
    if ratio >= VOL_HIGH_RATIO:
        return VOL_HIGH()
    if ratio <= VOL_LOW_RATIO:
        return VOL_LOW()
    return GRAY()


def _mono(size=ROW_FONT_SIZE, weight=0.4):
    return NSFont.monospacedSystemFontOfSize_weight_(size, weight)


def make_row_label(stock):
    """1 mã -> NSTextField: mã/giá ĐEN, đoạn %[chg] tô status, đoạn volume tô cao/thấp.

    Dùng monospaced font để mọi cột thẳng hàng (width cố định theo dòng dài nhất).
    """
    text, chg_start, chg_len, vol_start, vol_len = format_row(stock)
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
    if vol_len:
        astr.addAttribute_value_range_(
            NSForegroundColorAttributeName, volume_color(stock), (vol_start, vol_len)
        )
    return NSTextField.labelWithAttributedString_(astr)


def make_text_label(text, color, weight=0.4):
    astr = NSMutableAttributedString.alloc().initWithString_(text)
    astr.addAttribute_value_range_(NSFontAttributeName, _mono(weight=weight), (0, len(text)))
    astr.addAttribute_value_range_(NSForegroundColorAttributeName, color, (0, len(text)))
    return NSTextField.labelWithAttributedString_(astr)


def make_index_label(index):
    """VN-Index -> NSTextField 1 dòng nổi bật: 'VN-Index 1838.86  ▼-1.84 -0.10%'.

    Nhãn 'VN-Index' + điểm số để ĐEN đậm; đoạn change/% tô xanh(tăng)/đỏ(giảm).
    """
    price = index.get("price")
    if price is None:
        astr = NSMutableAttributedString.alloc().initWithString_("VN-Index —")
        astr.addAttribute_value_range_(NSFontAttributeName, _mono(weight=0.6), (0, 10))
        astr.addAttribute_value_range_(NSForegroundColorAttributeName, BLACK(), (0, 10))
        return NSTextField.labelWithAttributedString_(astr)

    change = index.get("change") or 0.0
    pct = index.get("changePct") or 0.0
    arrow = "▲" if change > 0 else ("▼" if change < 0 else "—")
    sign = "+" if change > 0 else ""
    prefix = f"VN-Index {price:>8.2f}  "
    chg = f"{arrow}{sign}{change:.2f} {sign}{pct:.2f}%"
    text = f"{prefix}{chg}"

    astr = NSMutableAttributedString.alloc().initWithString_(text)
    astr.addAttribute_value_range_(NSFontAttributeName, _mono(weight=0.6), (0, len(text)))
    astr.addAttribute_value_range_(NSForegroundColorAttributeName, BLACK(), (0, len(text)))
    color = STATUS_COLORS["up"]() if change > 0 else (
        STATUS_COLORS["down"]() if change < 0 else STATUS_COLORS["ref"]()
    )
    astr.addAttribute_value_range_(
        NSForegroundColorAttributeName, color, (len(prefix), len(chg))
    )
    return NSTextField.labelWithAttributedString_(astr)


def format_money(v):
    """Số tiền (đơn vị NGHÌN đồng, = giá[nghìn] × số cp) -> gọn: 1.52 tỷ / 152.3tr / 850k."""
    a = abs(v)
    if a >= 1_000_000:  # >= 1 tỷ (1e6 nghìn đồng)
        return f"{v / 1_000_000:.2f} tỷ"
    if a >= 1_000:      # >= 1 triệu
        return f"{v / 1_000:.1f}tr"
    return f"{v:.0f}k"


def portfolio_totals(codes, targets, quantities, by_code):
    """Tổng danh mục từ các mã có ĐỦ giá mua + vol + giá hiện tại. None nếu chưa có mã nào.

    Đơn vị tiền = nghìn đồng (giá VNDirect là nghìn đồng, qty là số cp).
    nav = Σ giá_hiện_tại × qty; cost = Σ giá_mua × qty; pnl = nav - cost.
    """
    cost = nav = 0.0
    counted = 0
    for code in codes:
        t, q = targets.get(code), quantities.get(code)
        price = (by_code.get(code) or {}).get("price")
        if not t or not q or price is None:
            continue
        counted += 1
        cost += t * q
        nav += price * q
    if not counted:
        return None
    pnl = nav - cost
    pct = (pnl / cost * 100) if cost else 0.0
    return {"nav": nav, "cost": cost, "pnl": pnl, "pct": pct, "count": counted}


def build_holdings(targets, quantities, dates):
    """Snapshot vị thế để đẩy lên bot. Chỉ mã có ĐỦ giá vốn + vol mới là vị thế:
    mã chỉ ghi giá mà không ghi vol là đang ngắm, không phải đang cầm."""
    out = []
    for code in sorted(targets):
        t, q = targets.get(code), quantities.get(code)
        if not t or not q:
            continue
        out.append({
            "symbol": code,
            "avg_cost": t,
            "qty": q,
            "first_buy_date": dates.get(code),
        })
    return out


def stamp_dates(targets, quantities, dates, today):
    """Đủ giá vốn + vol lần đầu = vào vị thế -> đóng dấu ngày, bot lấy tính T+.
    Bỏ giá hoặc vol = thoát -> xoá dấu, lần mua sau đếm lại từ đầu.
    Sửa `dates` tại chỗ vì cfg["dates"] và self.dates là cùng một object."""
    for code in list(dates):
        if not targets.get(code) or not quantities.get(code):
            del dates[code]
    for code, t in targets.items():
        if t and quantities.get(code) and code not in dates:
            dates[code] = today
    return dates


def push_holdings(sync_cfg, holdings):
    """POST snapshot lên worker (replace-all bên đó).

    Nuốt mọi lỗi: mất mạng không được làm kẹt app, và lần sửa sau sẽ tự đẩy lại
    nguyên snapshot nên không có chuyện lệch tích luỹ.
    """
    url = (sync_cfg or {}).get("url")
    key = (sync_cfg or {}).get("key")
    if not url or not key:
        return
    try:
        resp = requests.post(
            url, params={"key": key}, json={"holdings": holdings}, timeout=10
        )
        # flush: nohup redirect stdout vào file -> print bị buffer, log sync tới
        # lúc cần soi thì chưa ra tới nơi.
        if resp.status_code == 200:
            print(f"[sync] ok {len(holdings)} mã", flush=True)
        else:
            print(f"[sync] HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
    except requests.RequestException as e:
        print(f"[sync] lỗi mạng: {e}", flush=True)


def make_total_label(totals):
    """Hàng tổng: 'TỔNG  152.3tr  ▲+5.2tr +3.5%'. Nhãn+NAV đen, đoạn lãi/lỗ tô xanh/đỏ."""
    nav, pnl, pct = totals["nav"], totals["pnl"], totals["pct"]
    arrow = "▲" if pnl > 0 else ("▼" if pnl < 0 else "—")
    sign = "+" if pnl > 0 else ""
    prefix = f"TỔNG  {format_money(nav)}   "
    chg = f"{arrow}{sign}{format_money(pnl)} {sign}{pct:.1f}%"
    text = f"{prefix}{chg}"

    astr = NSMutableAttributedString.alloc().initWithString_(text)
    astr.addAttribute_value_range_(NSFontAttributeName, _mono(weight=0.6), (0, len(text)))
    astr.addAttribute_value_range_(NSForegroundColorAttributeName, BLACK(), (0, len(text)))
    color = STATUS_COLORS["up"]() if pnl > 0 else (
        STATUS_COLORS["down"]() if pnl < 0 else STATUS_COLORS["ref"]()
    )
    astr.addAttribute_value_range_(
        NSForegroundColorAttributeName, color, (len(prefix), len(chg))
    )
    return NSTextField.labelWithAttributedString_(astr)


class StockBarApp(NSObject):
    """App thuần PyObjC: NSStatusItem (icon) + NSPopover nền trắng. Fetch thủ công."""

    def init(self):
        self = objc.super(StockBarApp, self).init()
        if self is None:
            return None
        self.cfg = load_config()
        self.codes = self.cfg["watchlist"]
        self.targets = self.cfg["targets"]
        self.quantities = self.cfg["quantities"]
        self.dates = self.cfg["dates"]
        self.last_stocks = None
        self.last_index = None

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
        # Đẩy 1 lần lúc mở: danh mục có thể đã đổi ở lần chạy trước mà push hỏng,
        # hoặc bảng bên bot vừa được tạo lại.
        self._stamp_dates()
        save_config(self.cfg)
        self._sync_bg()
        return self

    # --- data ---
    def refresh_(self, _timer):
        """Kick off fetch trên background thread; UI không bao giờ đơ vì mạng."""
        codes = list(self.codes)  # snapshot, tránh race khi user đang edit
        threading.Thread(target=self._fetch_bg, args=(codes,), daemon=True).start()

    def _fetch_bg(self, codes):
        now = datetime.now().strftime("%H:%M:%S")
        try:
            # Index + watchlist fetch song song để không cộng dồn latency.
            with ThreadPoolExecutor(max_workers=2) as ex:
                fut_index = ex.submit(fetch_index)
                fut_stocks = ex.submit(fetch_prices, codes)
                index = fut_index.result()
                stocks = fut_stocks.result()
            result = {
                "stocks": stocks,
                "index": index,
                "status": f"Cập nhật: {now}",
                "ok": True,
            }
        except Exception:
            result = {"status": f"⚠️ Không cập nhật được ({now})", "ok": False}
        # AppKit: mọi update UI phải về main thread.
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "_applyFetchResult:", result, False
        )

    def _applyFetchResult_(self, result):
        if result["ok"]:
            self.last_stocks = result["stocks"]
            self.last_index = result["index"]
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

        # VN-Index trên cùng — '—' tới khi fetch xong lần đầu.
        stack.addArrangedSubview_(
            make_index_label(self.last_index or {"code": "VNINDEX", "price": None})
        )

        # Iterate theo self.codes (nguồn chân lý) để tag nút ✕ = index trong codes,
        # tránh lệch khi fetch nền chưa kịp cập nhật last_stocks sau Add/Remove.
        by_code = {s["code"]: s for s in (self.last_stocks or [])}
        for i, code in enumerate(self.codes):
            s = by_code.get(code, {"code": code, "price": None})  # chưa fetch -> '—'
            row = NSStackView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
            row.setOrientation_(NSUserInterfaceLayoutOrientationHorizontal)
            row.setSpacing_(8.0)
            row.addArrangedSubview_(make_row_label(s))

            # Ô nhập giá mua + vol đã mua (tag=index để handler map ra code).
            target = self.targets.get(code)
            qty = self.quantities.get(code)
            row.addArrangedSubview_(self._row_input(
                i, f"{target:g}" if target is not None else "", "giá", 56.0,
                "onTargetChanged:",
            ))
            row.addArrangedSubview_(self._row_input(
                i, f"{qty}" if qty else "", "vol", 66.0, "onQtyChanged:",
            ))

            # % chênh giá hiện tại so target. Luôn add (rỗng nếu chưa có target) +
            # ghim width để cột ✕ thẳng hàng giữa mọi row.
            diff = format_target_diff(s.get("price"), target)
            diff_text = diff[0] if diff else ""
            diff_color = STATUS_COLORS[diff[1]]() if diff else GRAY()
            diff_lbl = make_text_label(diff_text, diff_color, weight=0.5)
            diff_lbl.setAlignment_(NSTextAlignmentRight)
            diff_lbl.widthAnchor().constraintEqualToConstant_(54.0).setActive_(True)
            row.addArrangedSubview_(diff_lbl)

            x_btn = NSButton.buttonWithTitle_target_action_("✕", self, "onRemove:")
            x_btn.setTag_(i)
            x_btn.setBezelStyle_(0)  # bezel gọn
            row.addArrangedSubview_(x_btn)
            stack.addArrangedSubview_(row)

        # Hàng tổng danh mục — chỉ hiện khi có mã nhập đủ giá mua + vol.
        totals = portfolio_totals(self.codes, self.targets, self.quantities, by_code)
        if totals:
            stack.addArrangedSubview_(make_total_label(totals))

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
        # Rebuild lúc popover đang mở (fetch nền xong) sẽ set content view mới ->
        # window auto-focus lại ô nhập; clear để không nhảy con trỏ vào ô giá.
        if self.popover.isShown():
            self._clear_focus()

    def _clear_focus(self):
        """Bỏ first responder để popover mở ra không tự focus/select ô nhập nào."""
        vc = self.popover.contentViewController()
        win = vc.view().window() if vc is not None else None
        if win is not None:
            win.makeFirstResponder_(None)

    def togglePopover_(self, sender):
        if self.popover.isShown():
            self.popover.performClose_(sender)
        else:
            self._rebuild_panel()
            btn = self.status_item.button()
            self.popover.showRelativeToRect_ofView_preferredEdge_(
                btn.bounds(), btn, NSRectEdgeMaxY
            )
            self._clear_focus()  # mở panel = không focus ô nào

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
            valid = fetch_one(code, history=1).get("price") is not None
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
        """Ô thêm mã: viền đỏ + rung báo mã không hợp lệ."""
        self._shake_field(self.input_field)

    def _shake_field(self, field):
        """Tô viền đỏ + rung 1 NSTextField bất kỳ để báo input sai."""
        layer = field.layer()
        layer.setBorderColor_(NSColor.systemRedColor().CGColor())
        layer.setBorderWidth_(2.0)
        f = field.frame()
        cx = f.origin.x + f.size.width / 2
        shake = CAKeyframeAnimation.animationWithKeyPath_("position.x")
        shake.setValues_([cx, cx - 6, cx + 6, cx - 4, cx + 4, cx])
        shake.setDuration_(0.3)
        layer.addAnimation_forKey_(shake, "shake")

    def onRemove_(self, sender):
        i = sender.tag()
        if 0 <= i < len(self.codes):
            code = self.codes[i]
            del self.codes[i]
            self.targets.pop(code, None)     # xoá mã thì bỏ luôn giá mua...
            self.quantities.pop(code, None)  # ...và vol đã mua
            self._apply_watchlist_change()

    def _row_input(self, i, value, placeholder, width, action):
        """1 ô nhập nhỏ trong row (giá mua / vol). tag=index để handler map ra code."""
        f = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, width, 22))
        f.setEditable_(True)
        f.setAlignment_(NSTextAlignmentRight)
        f.setFont_(_mono(size=12.0))
        f.setPlaceholderString_(placeholder)
        if value:
            f.setStringValue_(value)
        f.setTag_(i)
        f.setTarget_(self)
        f.setAction_(action)  # Enter/end-edit = lưu
        f.setWantsLayer_(True)
        f.layer().setCornerRadius_(4.0)
        # Ghim width: NSTextField editable compression resistance thấp, cạnh label
        # khác nó bị nén về ~0 -> ẩn. Constraint ép width cố định.
        f.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
        return f

    # --- giá mua / vol đã mua ---
    def onTargetChanged_(self, sender):
        """Enter trong ô giá mua -> parse + lưu. Rỗng = xoá."""
        i = sender.tag()
        if not (0 <= i < len(self.codes)):
            return
        code = self.codes[i]
        raw = sender.stringValue().strip().replace(",", ".")
        if not raw:
            self.targets.pop(code, None)
            self._apply_portfolio_change()
            return
        try:
            val = float(raw)
        except ValueError:
            self._shake_field(sender)
            return
        if val <= 0:
            self._shake_field(sender)
            return
        self.targets[code] = val
        self._apply_portfolio_change()

    def onQtyChanged_(self, sender):
        """Enter trong ô vol đã mua -> parse số nguyên cp + lưu. Rỗng = xoá."""
        i = sender.tag()
        if not (0 <= i < len(self.codes)):
            return
        code = self.codes[i]
        raw = sender.stringValue().strip().replace(",", "").replace(".", "")  # bỏ phân cách nghìn
        if not raw:
            self.quantities.pop(code, None)
            self._apply_portfolio_change()
            return
        try:
            qty = int(raw)
        except ValueError:
            self._shake_field(sender)
            return
        if qty <= 0:
            self._shake_field(sender)
            return
        self.quantities[code] = qty
        self._apply_portfolio_change()

    def _stamp_dates(self):
        today = datetime.now().strftime("%Y-%m-%d")
        stamp_dates(self.targets, self.quantities, self.dates, today)
        self.cfg["dates"] = self.dates

    def _sync_bg(self):
        """Đẩy nền: POST mạng mà chạy trên main thread thì panel đứng hình."""
        holdings = build_holdings(self.targets, self.quantities, self.dates)
        threading.Thread(
            target=push_holdings, args=(self.cfg.get("sync"), holdings), daemon=True
        ).start()

    def _apply_portfolio_change(self):
        """Ghi giá mua + vol vào config + rebuild để cập nhật % chênh & hàng tổng."""
        self.cfg["targets"] = self.targets
        self.cfg["quantities"] = self.quantities
        self._stamp_dates()
        save_config(self.cfg)
        self._rebuild_panel()
        self._sync_bg()

    def _apply_watchlist_change(self):
        """Ghi config.json + rebuild ngay (phản hồi tức thì) + fetch mã mới ở nền."""
        self.cfg["watchlist"] = self.codes
        self.cfg["targets"] = self.targets
        self.cfg["quantities"] = self.quantities
        self._stamp_dates()
        save_config(self.cfg)
        self._rebuild_panel()  # hiện ngay list mới (mã mới = '—' tới khi fetch xong)
        self.refresh_(None)    # fetch nền, xong tự rebuild lại
        self._sync_bg()


if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # menu bar only
    delegate = StockBarApp.alloc().init()
    app.run()
