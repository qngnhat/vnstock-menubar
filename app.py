#!/usr/bin/env python3
"""VN Stock menu bar app — icon topbar, click bung panel nền trắng xem giá."""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import objc
import requests
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBezierPath,
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
    NSTextView,
    NSUserInterfaceLayoutOrientationHorizontal,
    NSUserInterfaceLayoutOrientationVertical,
    NSVariableStatusItemLength,
    NSView,
    NSViewController,
)
from Cocoa import CAKeyframeAnimation
from Foundation import NSObject, NSTimer

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
API_URL = "https://api-finfo.vndirect.com.vn/v4/stock_prices"
INDEX_API_URL = "https://api-finfo.vndirect.com.vn/v4/vnmarket_prices"
# Bảng giá VPS = giá khớp realtime; VNDirect stock_prices là snapshot ~1 phút/lần
# nên trễ 1-2 phút. VNDirect giữ làm fallback + nguồn avgVolume lịch sử.
LIVE_API_URL = "https://bgapidatafeed.vps.com.vn/getliststockdata/"
LIVE_INDEX_URL = "https://bgapidatafeed.vps.com.vn/getlistindexdetail/10"  # 10 = VNINDEX
# DNS VPS trả 2 IP, IP đầu (13.250.167.163) chết: connect timeout ngắn để urllib3
# bỏ nhanh sang IP sau, không thì mỗi lần fetch treo 10-15s.
LIVE_TIMEOUT = (1.0, 5)
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Phiên HOSE, mốc tính bằng phút trong ngày: (từ, đến, nhãn, có giá chạy).
# Nghỉ trưa giá đứng yên -> poll trong khoảng đó chỉ tốn request.
# Mốc cuối kéo tới 15:05 thay vì 15:00 vì giá ATC đôi khi về chậm vài phút.
PHASES = (
    (0, 9 * 60, "Chưa mở", False),
    (9 * 60, 9 * 60 + 15, "ATO", True),
    (9 * 60 + 15, 11 * 60 + 30, "Liên tục", True),
    (11 * 60 + 30, 13 * 60, "Nghỉ trưa", False),
    (13 * 60, 14 * 60 + 30, "Liên tục", True),
    (14 * 60 + 30, 14 * 60 + 45, "ATC", True),
    (14 * 60 + 45, 15 * 60 + 5, "Thỏa thuận", True),
    (15 * 60 + 5, 24 * 60, "Đóng cửa", False),
)
# Stop chỉ đổi khi đỉnh đổi (bot cập nhật lúc EOD) -> không cần kéo mỗi nhịp.
BOT_STATUS_TTL = 300


def market_phase(now=None):
    """Đang ở phiên nào -> (nhãn, giá có chạy không). Ngày lễ xem trading_day."""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return "Nghỉ cuối tuần", False
    mins = now.hour * 60 + now.minute
    for start, end, label, live in PHASES:
        if start <= mins < end:
            return label, live
    return "Đóng cửa", False


def market_open_now(now=None):
    return market_phase(now)[1]


def fetch_bot_status(sync_cfg):
    """Stop + hạn giữ + ngày bán được, từ worker. Lỗi -> None, panel hiện như cũ."""
    url = (sync_cfg or {}).get("url")
    key = (sync_cfg or {}).get("key")
    if not url or not key:
        return None
    try:
        resp = requests.get(
            url.replace("/portfolio/sync", "/portfolio/status"),
            params={"key": key}, timeout=10,
        )
        if resp.status_code != 200:
            print(f"[status] HTTP {resp.status_code}", flush=True)
            return None
        data = resp.json()
        return {
            "by_code": {r["symbol"]: r for r in data.get("positions", [])},
            "near_pct": data.get("near_pct") or 2,
            "trading_day": data.get("trading_day", True),
        }
    except Exception as e:  # payload lạ cũng không được làm chết luôn phần giá
        print(f"[status] lỗi: {e}", flush=True)
        return None


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


def fetch_one(code, sessions=21):
    """Lấy giá mã + volume trung bình để so vol hôm nay cao/thấp.

    sessions phiên gần nhất (date:desc): phiên [0] = mới nhất (giá hiện tại),
    [1:] = lịch sử -> tính avgVolume so với vol hôm nay. Dùng sessions=1 khi
    chỉ cần kiểm tra mã tồn tại (validate). Raise nếu network/API lỗi.
    """
    resp = requests.get(
        API_URL,
        params={"sort": "date:desc", "q": f"code:{code}", "size": sessions},
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
        "history": [(r.get("date"), r.get("close")) for r in reversed(data)],
    }


def fetch_live(codes):
    """Giá realtime nhiều mã trong 1 request từ bảng giá VPS -> {code: dict}.

    Mã VPS không biết thì không có trong kết quả. Raise nếu network/API lỗi.
    """
    resp = requests.get(
        LIVE_API_URL + ",".join(codes), headers=HEADERS, timeout=LIVE_TIMEOUT
    )
    resp.raise_for_status()
    out = {}
    for r in resp.json():
        ref = r.get("r")
        if not ref:
            continue
        # lastPrice = 0 khi chưa khớp lệnh nào trong ngày (trước phiên / ATO).
        price = r.get("lastPrice") or ref
        # changePc của VPS không có dấu -> tự tính change từ ref.
        change = round(price - ref, 2)
        out[r["sym"]] = {
            "code": r["sym"],
            "price": price,
            "change": change,
            "changePct": change / ref * 100,
            "ceiling": r.get("c"),
            "floor": r.get("f"),
            "ref": ref,
            "volume": (r.get("lot") or 0) * 10,  # lot = đơn vị 10 cp
        }
    return out


# {(code, ngày): (avgVolume, history)} — lịch sử phiên đã chốt, trong ngày không đổi.
_daily_cache = {}


def daily_bars(code):
    """TB volume + giá đóng cửa 20 phiên trước. 1 request/mã/ngày (đủ cho cả sparkline)."""
    key = (code, datetime.now().date())
    if key not in _daily_cache:
        d = fetch_one(code)
        _daily_cache[key] = (d.get("avgVolume"), d.get("history"))
    return _daily_cache[key]


def fetch_index(code="VNINDEX"):
    """Lấy điểm VN-Index: bảng giá VPS (realtime), lỗi thì fallback vnmarket_prices.

    Raise nếu cả 2 nguồn lỗi — caller tự bắt.
    """
    try:
        resp = requests.get(LIVE_INDEX_URL, headers=HEADERS, timeout=LIVE_TIMEOUT)
        resp.raise_for_status()
        row = (resp.json() or [None])[0]
        if row and row.get("cIndex") and row.get("oIndex"):
            price, ref = row["cIndex"], row["oIndex"]
            change = round(price - ref, 2)
            return {
                "code": code,
                "price": price,
                "change": change,
                "changePct": change / ref * 100,
            }
    except (requests.RequestException, ValueError) as e:
        print(f"[live] index lỗi, fallback VNDirect: {e}", flush=True)
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
    """Lấy giá các mã, giữ đúng thứ tự. Trả list dict.

    Giá từ VPS (1 request cho cả list); mã VPS thiếu hoặc VPS lỗi thì fallback
    fetch_one VNDirect từng mã. Song song vì fallback/avgVolume là 1 HTTP mỗi mã.
    """
    if not codes:
        return []
    try:
        live = fetch_live(codes)
    except (requests.RequestException, ValueError) as e:
        print(f"[live] lỗi, fallback VNDirect: {e}", flush=True)
        live = {}

    def one(code):
        stock = live.get(code)
        if stock is None:
            return fetch_one(code)
        try:
            stock["avgVolume"], stock["history"] = daily_bars(code)
        except requests.RequestException:
            # Thiếu lịch sử chỉ mất màu volume + sparkline, giá vẫn hiện.
            stock["avgVolume"], stock["history"] = None, None
        return stock

    with ThreadPoolExecutor(max_workers=min(len(codes), 8)) as ex:
        return list(ex.map(one, codes))


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


SPARK_W, SPARK_H = 54.0, 16.0
SPARK_MIN_POINTS = 5
SPARK_PAD = 2.0
SPARK_STOP = lambda: _rgb(0.55, 0.55, 0.60)


def spark_points(stock, today=None):
    """Giá đóng cửa các phiên đã chốt + giá hiện tại làm điểm cuối -> list float.

    Bỏ phiên hôm nay trong history: VNDirect snapshot trễ 1-2 phút, giá live của
    VPS mới là điểm cuối đúng. Ít hơn SPARK_MIN_POINTS thì vẽ ra vô nghĩa -> [].
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    pts = [c for d, c in (stock.get("history") or []) if c and (d or "") != today]
    price = stock.get("price")
    if price:
        pts.append(price)
    return pts if len(pts) >= SPARK_MIN_POINTS else []


def spark_color(pts):
    """Màu theo hướng cả cửa sổ (không phải hôm nay — màu hôm nay đã có ở cột %)."""
    if len(pts) < 2:
        return GRAY()
    return STATUS_COLORS["up" if pts[-1] >= pts[0] else "down"]()


class SparkView(NSView):
    """Sparkline 20 phiên + vạch stop. points/stop/color gán sau alloc (make_spark_view).

    Vẽ tay vì AppKit không có control nào nhẹ hơn cho cỡ 54x16pt.
    """

    def drawRect_(self, rect):
        pts = getattr(self, "points", None) or []
        if len(pts) < 2:
            return
        stop = getattr(self, "stop", None)
        color = getattr(self, "color", None) or GRAY()
        b = self.bounds()
        lo, hi = min(pts), max(pts)
        # Stop vào thang luôn: nhìn được khoảng cách tới stop mới là điểm của hình.
        if stop:
            lo, hi = min(lo, stop), max(hi, stop)
        span = (hi - lo) or 1.0
        usable_h = b.size.height - 2 * SPARK_PAD
        usable_w = b.size.width - 2 * SPARK_PAD
        ypos = lambda v: SPARK_PAD + (v - lo) / span * usable_h
        xpos = lambda i: SPARK_PAD + i * usable_w / (len(pts) - 1)

        path = NSBezierPath.bezierPath()
        path.moveToPoint_((xpos(0), ypos(pts[0])))
        for i, v in enumerate(pts[1:], 1):
            path.lineToPoint_((xpos(i), ypos(v)))
        # Tô nhẹ dưới đường: ở 16pt thì khối màu dễ đọc hướng hơn nét 1px.
        area = path.copy()
        area.lineToPoint_((xpos(len(pts) - 1), 0.0))
        area.lineToPoint_((xpos(0), 0.0))
        area.closePath()
        color.colorWithAlphaComponent_(0.15).setFill()
        area.fill()
        if stop:
            line = NSBezierPath.bezierPath()
            line.moveToPoint_((0.0, ypos(stop)))
            line.lineToPoint_((b.size.width, ypos(stop)))
            line.setLineWidth_(1.0)
            line.setLineDash_count_phase_([2.0, 2.0], 2, 0.0)
            SPARK_STOP().setStroke()
            line.stroke()

        color.setStroke()
        path.setLineWidth_(1.2)
        path.stroke()

        dot = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(xpos(len(pts) - 1) - 1.6, ypos(pts[-1]) - 1.6, 3.2, 3.2)
        )
        color.setFill()
        dot.fill()


def make_spark_view(stock, stop):
    """Luôn trả view (rỗng khi thiếu lịch sử) để các cột sau thẳng hàng mọi row."""
    v = SparkView.alloc().initWithFrame_(NSMakeRect(0, 0, SPARK_W, SPARK_H))
    pts = spark_points(stock)
    v.points = pts
    v.stop = stop
    v.color = spark_color(pts)
    v.widthAnchor().constraintEqualToConstant_(SPARK_W).setActive_(True)
    v.heightAnchor().constraintEqualToConstant_(SPARK_H).setActive_(True)
    return v


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
    Lãi/lỗ HÔM NAY tính so giá tham chiếu, khác pnl (so giá vốn): trong phiên cái
    cần biết là hôm nay được mất bao nhiêu, không phải lãi từ hồi mua.
    """
    cost = nav = prev = 0.0
    counted = 0
    for code in codes:
        t, q = targets.get(code), quantities.get(code)
        stock = by_code.get(code) or {}
        price = stock.get("price")
        if not t or not q or price is None:
            continue
        counted += 1
        cost += t * q
        nav += price * q
        prev += (stock.get("ref") or price) * q
    if not counted:
        return None
    pnl = nav - cost
    pct = (pnl / cost * 100) if cost else 0.0
    day_pnl = nav - prev
    day_pct = (day_pnl / prev * 100) if prev else 0.0
    return {
        "nav": nav, "cost": cost, "pnl": pnl, "pct": pct, "count": counted,
        "day_pnl": day_pnl, "day_pct": day_pct,
    }


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


def _pnl_color(v):
    return STATUS_COLORS["up"]() if v > 0 else (
        STATUS_COLORS["down"]() if v < 0 else STATUS_COLORS["ref"]()
    )


def _pnl_text(v, pct):
    arrow = "▲" if v > 0 else ("▼" if v < 0 else "—")
    sign = "+" if v > 0 else ""
    return f"{arrow}{sign}{format_money(v)} {sign}{pct:.1f}%"


def make_day_label(totals):
    """Hàng 'HÔM NAY ▼-1.2tr -2.8%' — lãi/lỗ trong phiên, so giá tham chiếu."""
    text = f"HÔM NAY  {_pnl_text(totals['day_pnl'], totals['day_pct'])}"
    return make_text_label(text, _pnl_color(totals["day_pnl"]), weight=0.6)


# ATO/ATC khớp lệnh định kỳ, giá nhảy một phát rồi chốt -> tô cam cho đập vào mắt.
PHASE_COLORS = {"ATO": VOL_HIGH, "ATC": VOL_HIGH, "Liên tục": lambda: STATUS_COLORS["up"]()}


def make_phase_label(phase, status_text):
    """Hàng cuối: 'ATC · Cập nhật: 14:32:10'. Nhãn phiên tô màu, phần sau xám."""
    sep = "  ·  " if status_text else ""
    text = f"{phase}{sep}{status_text}"
    astr = NSMutableAttributedString.alloc().initWithString_(text)
    astr.addAttribute_value_range_(NSFontAttributeName, _mono(weight=0.3), (0, len(text)))
    astr.addAttribute_value_range_(NSForegroundColorAttributeName, GRAY(), (0, len(text)))
    astr.addAttribute_value_range_(
        NSForegroundColorAttributeName, PHASE_COLORS.get(phase, GRAY)(), (0, len(phase))
    )
    return NSTextField.labelWithAttributedString_(astr)


def make_bar_title(totals):
    """Title trên menubar: lãi/lỗ hôm nay, tô xanh/đỏ. Chưa có vị thế -> icon."""
    if not totals:
        return None
    text = _pnl_text(totals["day_pnl"], totals["day_pct"])
    astr = NSMutableAttributedString.alloc().initWithString_(text)
    astr.addAttribute_value_range_(NSFontAttributeName, _mono(size=12.0, weight=0.5), (0, len(text)))
    astr.addAttribute_value_range_(
        NSForegroundColorAttributeName, _pnl_color(totals["day_pnl"]), (0, len(text))
    )
    return astr


def bot_stop(info):
    """Pick bot thì stop theo luật tier; mã mua tay worker tự dựng mức tham chiếu."""
    return ((info or {}).get("bot") or (info or {}).get("manual") or {}).get("stop")


def format_bot_hint(price, info, near_pct):
    """Một ô cho cả stop lẫn T+ -> (text, color). Ưu tiên cái gấp hơn.

    Chỉ hiện ràng buộc đang có hiệu lực: đã bán được rồi thì không cần nhắc T+.
    """
    if not info:
        return "", GRAY()
    stop = bot_stop(info)
    if stop and price is not None:
        if price <= stop:
            return f"⛔ thủng {stop:.2f}", STATUS_COLORS["down"]()
        gap = (price - stop) / price * 100
        if gap <= near_pct:
            return f"⚠ stop {stop:.2f} {gap:.1f}%", VOL_HIGH()
    if info.get("sellable") is False and info.get("sellable_date"):
        d = info["sellable_date"][8:10] + "/" + info["sellable_date"][5:7]
        return f"bán {d}", GRAY()
    if stop:
        return f"stop {stop:.2f}", GRAY()
    return "", GRAY()


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
        self.bot_status = None
        self.bot_status_at = 0.0

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

        # refresh_seconds vốn là config chết: không có timer nào, giá chỉ mới khi
        # mở panel. Timer chỉ fetch trong phiên (xem refresh_).
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            float(self.cfg["refresh_seconds"]), self, "refresh:", None, True
        )

        self.refresh_(None)  # fetch ngay lúc mở, khỏi chờ hết nhịp timer đầu
        # Đẩy 1 lần lúc mở: danh mục có thể đã đổi ở lần chạy trước mà push hỏng,
        # hoặc bảng bên bot vừa được tạo lại.
        self._stamp_dates()
        save_config(self.cfg)
        self._sync_bg()
        return self

    # --- data ---
    def refresh_(self, timer):
        """Kick off fetch trên background thread; UI không bao giờ đơ vì mạng.

        Timer gọi (timer != None) thì chỉ fetch trong phiên; gọi tay thì luôn fetch.
        """
        if timer is not None and not self._should_poll():
            return
        codes = list(self.codes)  # snapshot, tránh race khi user đang edit
        threading.Thread(target=self._fetch_bg, args=(codes,), daemon=True).start()

    def _fetch_bg(self, codes):
        now = datetime.now().strftime("%H:%M:%S")
        want_status = time.time() - self.bot_status_at > BOT_STATUS_TTL
        try:
            # Index + watchlist fetch song song để không cộng dồn latency.
            with ThreadPoolExecutor(max_workers=3) as ex:
                fut_index = ex.submit(fetch_index)
                fut_stocks = ex.submit(fetch_prices, codes)
                fut_bot = ex.submit(fetch_bot_status, self.cfg.get("sync")) if want_status else None
                index = fut_index.result()
                stocks = fut_stocks.result()
                bot = fut_bot.result() if fut_bot else None
            result = {
                "stocks": stocks,
                "index": index,
                "bot": bot,
                "status_tried": want_status,
                "status": f"Cập nhật: {now}",
                "ok": True,
            }
        except Exception:
            result = {"status": f"⚠️ Không cập nhật được ({now})", "ok": False}
        # AppKit: mọi update UI phải về main thread.
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "_applyFetchResult:", result, False
        )

    def _should_poll(self):
        """Ngoài phiên thì giá đứng yên. Ngày lễ app không tự biết -> hỏi bot."""
        if not market_open_now():
            return False
        return self._trading_day()

    def _trading_day(self):
        return (self.bot_status or {}).get("trading_day", True)

    def _phase_label(self):
        phase, live = market_phase()
        # Lễ thì giờ vẫn rơi vào khung phiên, chỉ bot mới biết là không có giao dịch.
        return "Nghỉ lễ" if live and not self._trading_day() else phase

    def _applyFetchResult_(self, result):
        if result["ok"]:
            self.last_stocks = result["stocks"]
            self.last_index = result["index"]
            if result.get("bot") is not None:
                self.bot_status = result["bot"]
            # Đánh dấu cả khi bot lỗi, kẻo 30 giây lại gọi lại một endpoint đang hỏng.
            if result.get("status_tried"):
                self.bot_status_at = time.time()
            self._update_bar_title()
        else:
            self.status_item.button().setTitle_("⚠️")
        self.status_text = result["status"]
        # Đang gõ dở ô nào thì rebuild sẽ xoá mất chữ -> để lần rebuild sau mới hiện giá mới.
        if self.popover.isShown() and not self._is_editing():
            self._rebuild_panel()

    # --- UI ---
    def _update_bar_title(self):
        """Lãi/lỗ hôm nay hiện thẳng trên menubar — khỏi phải mở panel mới biết."""
        by_code = {s["code"]: s for s in (self.last_stocks or [])}
        totals = portfolio_totals(self.codes, self.targets, self.quantities, by_code)
        astr = make_bar_title(totals)
        btn = self.status_item.button()
        if astr is None:
            btn.setTitle_("📈")
        else:
            btn.setAttributedTitle_(astr)

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

            info = (self.bot_status or {}).get("by_code", {}).get(code)
            near_pct = (self.bot_status or {}).get("near_pct", 2)
            row.addArrangedSubview_(make_spark_view(s, bot_stop(info)))

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

            # Stop của bot + hạn T+ — cùng một ô, cái nào gấp hơn thì hiện.
            hint_text, hint_color = format_bot_hint(s.get("price"), info, near_pct)
            hint_lbl = make_text_label(hint_text, hint_color, weight=0.5)
            hint_lbl.setAlignment_(NSTextAlignmentRight)
            hint_lbl.widthAnchor().constraintEqualToConstant_(126.0).setActive_(True)
            row.addArrangedSubview_(hint_lbl)

            x_btn = NSButton.buttonWithTitle_target_action_("✕", self, "onRemove:")
            x_btn.setTag_(i)
            x_btn.setBezelStyle_(0)  # bezel gọn
            row.addArrangedSubview_(x_btn)
            stack.addArrangedSubview_(row)

        # Hàng tổng danh mục — chỉ hiện khi có mã nhập đủ giá mua + vol.
        totals = portfolio_totals(self.codes, self.targets, self.quantities, by_code)
        if totals:
            stack.addArrangedSubview_(make_total_label(totals))
            stack.addArrangedSubview_(make_day_label(totals))

        stack.addArrangedSubview_(
            make_phase_label(self._phase_label(), getattr(self, "status_text", ""))
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

    def _is_editing(self):
        vc = self.popover.contentViewController()
        win = vc.view().window() if vc is not None else None
        # NSTextField đang edit thì first responder là field editor (NSTextView).
        return win is not None and isinstance(win.firstResponder(), NSTextView)

    def togglePopover_(self, sender):
        if self.popover.isShown():
            self.popover.performClose_(sender)
        else:
            self.refresh_(None)  # mở panel = lấy giá mới; xong tự rebuild
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
            valid = fetch_one(code, sessions=1).get("price") is not None
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
        self.bot_status_at = 0.0  # vị thế đổi -> T+ đổi, kéo lại ở nhịp tới

    def _apply_watchlist_change(self):
        """Ghi config.json + rebuild ngay (phản hồi tức thì) + fetch mã mới ở nền."""
        self.cfg["watchlist"] = self.codes
        self.cfg["targets"] = self.targets
        self.cfg["quantities"] = self.quantities
        self._stamp_dates()
        save_config(self.cfg)
        self._rebuild_panel()  # hiện ngay list mới (mã mới = '—' tới khi fetch xong)
        self.bot_status_at = 0.0  # mã mới có thể là pick bot -> kéo stop ngay
        self.refresh_(None)    # fetch nền, xong tự rebuild lại
        self._sync_bg()


if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # menu bar only
    delegate = StockBarApp.alloc().init()
    app.run()
