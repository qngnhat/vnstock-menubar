#!/usr/bin/env python3
"""VN Stock cho Windows — icon ở system tray, click bung panel nền trắng xem giá.

Bản port của ../app.py (macOS, PyObjC). Logic lấy giá / danh mục / sync bot giữ
nguyên; UI viết lại bằng PySide6: QSystemTrayIcon + cửa sổ popup không viền.
Config riêng: windows/config.json (cùng schema với bản macOS, copy qua được).
"""

import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import escape

import requests
from PySide6.QtCore import (
    QDir,
    QEasingCurve,
    QLockFile,
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
API_URL = "https://api-finfo.vndirect.com.vn/v4/stock_prices"
INDEX_API_URL = "https://api-finfo.vndirect.com.vn/v4/vnmarket_prices"
# Bảng giá VPS = giá khớp realtime; VNDirect stock_prices là snapshot ~1 phút/lần
# nên trễ 1-2 phút. VNDirect giữ làm fallback + nguồn avgVolume lịch sử.
LIVE_API_URL = "https://bgapidatafeed.vps.com.vn/getliststockdata/"
LIVE_INDEX_URL = "https://bgapidatafeed.vps.com.vn/getlistindexdetail/10"  # 10 = VNINDEX
# DNS VPS trả 2 IP, IP đầu chết: connect timeout ngắn để urllib3 bỏ nhanh sang IP sau.
LIVE_TIMEOUT = (1.0, 5)
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Phiên HOSE, mốc tính bằng phút trong ngày: (từ, đến, nhãn, có giá chạy).
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
BOT_STATUS_TTL = 300


# ============================================================================
# Logic (giống bản macOS)
# ============================================================================

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
    except Exception as e:
        print(f"[status] lỗi: {e}", flush=True)
        return None


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    cfg.setdefault("watchlist", ["HPG"])
    cfg.setdefault("refresh_seconds", 30)
    cfg.setdefault("targets", {})
    cfg.setdefault("quantities", {})
    cfg.setdefault("dates", {})
    cfg.setdefault("sync", {})
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def normalize_code(raw):
    return (raw or "").strip().upper()


def fetch_one(code, sessions=21):
    """Giá mã + volume TB các phiên trước (VNDirect). Raise nếu network/API lỗi."""
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
    """Giá realtime nhiều mã trong 1 request từ bảng giá VPS -> {code: dict}."""
    resp = requests.get(
        LIVE_API_URL + ",".join(codes), headers=HEADERS, timeout=LIVE_TIMEOUT
    )
    resp.raise_for_status()
    out = {}
    for r in resp.json():
        ref = r.get("r")
        if not ref:
            continue
        price = r.get("lastPrice") or ref
        change = round(price - ref, 2)
        out[r["sym"]] = {
            "code": r["sym"],
            "price": price,
            "change": change,
            "changePct": change / ref * 100,
            "ceiling": r.get("c"),
            "floor": r.get("f"),
            "ref": ref,
            "volume": (r.get("lot") or 0) * 10,
        }
    return out


_daily_cache = {}


def daily_bars(code):
    """TB volume + giá đóng cửa 20 phiên trước. 1 request/mã/ngày."""
    key = (code, datetime.now().date())
    if key not in _daily_cache:
        d = fetch_one(code)
        _daily_cache[key] = (d.get("avgVolume"), d.get("history"))
    return _daily_cache[key]


def fetch_index(code="VNINDEX"):
    """Điểm VN-Index: VPS (realtime), lỗi thì fallback VNDirect. Raise nếu cả 2 lỗi."""
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
    """Giá các mã, giữ thứ tự. VPS 1 request; mã thiếu / VPS lỗi -> fallback VNDirect."""
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
            stock["avgVolume"], stock["history"] = None, None
        return stock

    with ThreadPoolExecutor(max_workers=min(len(codes), 8)) as ex:
        return list(ex.map(one, codes))


def price_status(stock):
    """trần/sàn trước, rồi tăng/giảm/tham chiếu -> key trong STATUS_COLORS."""
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
    if not vol:
        return "0"
    if vol >= 1_000_000:
        return f"{vol / 1_000_000:.1f}M"
    if vol >= 1_000:
        return f"{vol / 1_000:.0f}K"
    return f"{int(vol)}"


def format_row(stock):
    """1 mã -> (text, chg_start, chg_len, vol_start, vol_len)."""
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
    if price is None or not target:
        return None
    pct = (price - target) / target * 100
    sign = "+" if pct > 0 else ""
    key = "up" if pct > 0 else ("down" if pct < 0 else "ref")
    return f"{sign}{pct:.1f}%", key


def format_money(v):
    """Đơn vị NGHÌN đồng -> 1.52 tỷ / 152.3tr / 850k."""
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.2f} tỷ"
    if a >= 1_000:
        return f"{v / 1_000:.1f}tr"
    return f"{v:.0f}k"


def portfolio_totals(codes, targets, quantities, by_code):
    """Tổng danh mục từ các mã có ĐỦ giá mua + vol + giá hiện tại. None nếu chưa có."""
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
    for code in list(dates):
        if not targets.get(code) or not quantities.get(code):
            del dates[code]
    for code, t in targets.items():
        if t and quantities.get(code) and code not in dates:
            dates[code] = today
    return dates


def push_holdings(sync_cfg, holdings):
    """POST snapshot lên worker. Nuốt mọi lỗi."""
    url = (sync_cfg or {}).get("url")
    key = (sync_cfg or {}).get("key")
    if not url or not key:
        return
    try:
        resp = requests.post(
            url, params={"key": key}, json={"holdings": holdings}, timeout=10
        )
        if resp.status_code == 200:
            print(f"[sync] ok {len(holdings)} mã", flush=True)
        else:
            print(f"[sync] HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
    except requests.RequestException as e:
        print(f"[sync] lỗi mạng: {e}", flush=True)


def bot_stop(info):
    return ((info or {}).get("bot") or (info or {}).get("manual") or {}).get("stop")


def _pnl_text(v, pct):
    arrow = "▲" if v > 0 else ("▼" if v < 0 else "—")
    sign = "+" if v > 0 else ""
    return f"{arrow}{sign}{format_money(v)} {sign}{pct:.1f}%"


SPARK_MIN_POINTS = 5


def spark_points(stock, today=None):
    """Giá đóng cửa các phiên đã chốt + giá hiện tại làm điểm cuối."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    pts = [c for d, c in (stock.get("history") or []) if c and (d or "") != today]
    price = stock.get("price")
    if price:
        pts.append(price)
    return pts if len(pts) >= SPARK_MIN_POINTS else []


# ============================================================================
# Màu + label (Qt)
# ============================================================================

STATUS_COLORS = {
    "ceiling": "#cc66ff",  # trần: tím
    "floor": "#00ccd9",    # sàn: cyan
    "up": "#26d959",       # tăng: xanh lá
    "down": "#ff4d4d",     # giảm: đỏ
    "ref": "#ffbf1a",      # tham chiếu: vàng
}
# Màu nền/chữ theo theme sáng/tối. Màu trạng thái (tăng/giảm/trần/sàn) giữ nguyên
# vì đủ tươi để nổi trên cả 2 nền. THEME là dict sống: đổi theme = cập nhật tại chỗ
# rồi rebuild panel, mọi hàm make_* đọc lại màu mới.
THEMES = {
    "light": {
        "bg": "#ffffff", "fg": "#000000", "muted": "#737373", "border": "#d0d0d0",
        "input_bg": "#ffffff", "input_border": "#c8c8c8", "focus": "#4a90e2",
        "btn_bg": "#f3f3f3", "btn_hover": "#e6e6e6", "btn_disabled": "#999999",
    },
    "dark": {
        "bg": "#1f1f1f", "fg": "#ededed", "muted": "#9a9a9a", "border": "#3a3a3a",
        "input_bg": "#2b2b2b", "input_border": "#4a4a4a", "focus": "#4a90e2",
        "btn_bg": "#2f2f2f", "btn_hover": "#3a3a3a", "btn_disabled": "#666666",
    },
}
THEME = dict(THEMES["light"])


def C(key):
    return THEME[key]


def set_theme(name):
    THEME.clear()
    THEME.update(THEMES.get(name, THEMES["light"]))
VOL_HIGH = "#ff800d"
VOL_LOW = "#9eb3c7"
VOL_HIGH_RATIO = 1.5
VOL_LOW_RATIO = 0.5
SPARK_STOP = "#8c8c99"
PHASE_COLORS = {"ATO": VOL_HIGH, "ATC": VOL_HIGH, "Liên tục": STATUS_COLORS["up"]}

MONO_FAMILY = "Consolas"
ROW_FONT_PX = 14


def volume_color(stock):
    vol, avg = stock.get("volume"), stock.get("avgVolume")
    if not vol or not avg:
        return C("muted")
    ratio = vol / avg
    if ratio >= VOL_HIGH_RATIO:
        return VOL_HIGH
    if ratio <= VOL_LOW_RATIO:
        return VOL_LOW
    return C("muted")


def _pnl_color(v):
    return STATUS_COLORS["up"] if v > 0 else (
        STATUS_COLORS["down"] if v < 0 else STATUS_COLORS["ref"]
    )


def spark_color(pts):
    if len(pts) < 2:
        return C("muted")
    return STATUS_COLORS["up" if pts[-1] >= pts[0] else "down"]


def _span(text, color):
    return f'<span style="color:{color}">{escape(text)}</span>'


def rich_label(segments, weight=QFont.Normal, size=ROW_FONT_PX):
    """[(text, color), ...] -> QLabel rich text, font mono, giữ nguyên khoảng trắng."""
    html = "".join(_span(t, c) for t, c in segments if t)
    lbl = QLabel(f'<span style="white-space:pre">{html}</span>')
    lbl.setTextFormat(Qt.RichText)
    f = QFont(MONO_FAMILY)
    f.setPixelSize(size)
    f.setWeight(weight)
    lbl.setFont(f)
    return lbl


def make_row_label(stock):
    text, cs, cl, vs, vl = format_row(stock)
    if not cl:
        return rich_label([(text, C("fg"))])
    return rich_label([
        (text[:cs], C("fg")),
        (text[cs:cs + cl], STATUS_COLORS[price_status(stock)]),
        (text[cs + cl:vs], C("fg")),
        (text[vs:vs + vl], volume_color(stock)),
    ])


def make_index_label(index):
    price = index.get("price")
    if price is None:
        return rich_label([("VN-Index —", C("fg"))], weight=QFont.DemiBold)
    change = index.get("change") or 0.0
    pct = index.get("changePct") or 0.0
    arrow = "▲" if change > 0 else ("▼" if change < 0 else "—")
    sign = "+" if change > 0 else ""
    prefix = f"VN-Index {price:>8.2f}  "
    chg = f"{arrow}{sign}{change:.2f} {sign}{pct:.2f}%"
    return rich_label([(prefix, C("fg")), (chg, _pnl_color(change))], weight=QFont.DemiBold)


def make_total_label(totals):
    prefix = f"TỔNG  {format_money(totals['nav'])}   "
    return rich_label(
        [(prefix, C("fg")), (_pnl_text(totals["pnl"], totals["pct"]), _pnl_color(totals["pnl"]))],
        weight=QFont.DemiBold,
    )


def make_day_label(totals):
    text = f"HÔM NAY  {_pnl_text(totals['day_pnl'], totals['day_pct'])}"
    return rich_label([(text, _pnl_color(totals["day_pnl"]))], weight=QFont.DemiBold)


def make_phase_label(phase, status_text):
    sep = "  ·  " if status_text else ""
    return rich_label(
        [(phase, PHASE_COLORS.get(phase, C("muted"))), (f"{sep}{status_text}", C("muted"))],
        weight=QFont.Light,
    )


def make_fixed_label(text, color, width, weight=QFont.Medium):
    lbl = rich_label([(text, color)], weight=weight)
    lbl.setFixedWidth(width)
    lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return lbl


def format_bot_hint(price, info, near_pct):
    """Một ô cho cả stop lẫn T+ -> (text, color). Ưu tiên cái gấp hơn."""
    if not info:
        return "", C("muted")
    stop = bot_stop(info)
    if stop and price is not None:
        if price <= stop:
            return f"⛔ thủng {stop:.2f}", STATUS_COLORS["down"]
        gap = (price - stop) / price * 100
        if gap <= near_pct:
            return f"⚠ stop {stop:.2f} {gap:.1f}%", VOL_HIGH
    if info.get("sellable") is False and info.get("sellable_date"):
        d = info["sellable_date"][8:10] + "/" + info["sellable_date"][5:7]
        return f"bán {d}", C("muted")
    if stop:
        return f"stop {stop:.2f}", C("muted")
    return "", C("muted")


def tray_tooltip(totals, phase, live, status_text):
    """Thay title menubar bản mac: tray icon không hiện chữ -> hover xem."""
    lines = ["VN Stock"]
    if live and totals:
        lines.append(f"Tổng {_pnl_text(totals['pnl'], totals['pct'])}")
        lines.append(f"Hôm nay {_pnl_text(totals['day_pnl'], totals['day_pct'])}")
    lines.append(phase)
    if status_text:
        lines.append(status_text)
    return "\n".join(lines)


class SparkView(QWidget):
    """Sparkline 20 phiên + vạch stop (đứt nét)."""

    W, H, PAD = 60, 18, 2.0

    def __init__(self, stock, stop):
        super().__init__()
        self.setFixedSize(self.W, self.H)
        self.points = spark_points(stock)
        self.stop = stop
        self.color = QColor(spark_color(self.points))

    def paintEvent(self, _event):
        pts = self.points
        if len(pts) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h, pad = self.width(), self.height(), self.PAD
        lo, hi = min(pts), max(pts)
        if self.stop:
            lo, hi = min(lo, self.stop), max(hi, self.stop)
        span = (hi - lo) or 1.0
        uh, uw = h - 2 * pad, w - 2 * pad
        # Qt: y=0 ở trên -> lật trục so với AppKit.
        ypos = lambda v: h - pad - (v - lo) / span * uh
        xpos = lambda i: pad + i * uw / (len(pts) - 1)

        line = QPainterPath(QPointF(xpos(0), ypos(pts[0])))
        for i, v in enumerate(pts[1:], 1):
            line.lineTo(xpos(i), ypos(v))
        area = QPainterPath(line)
        area.lineTo(xpos(len(pts) - 1), h)
        area.lineTo(xpos(0), h)
        area.closeSubpath()
        fill = QColor(self.color)
        fill.setAlphaF(0.15)
        p.fillPath(area, fill)

        if self.stop:
            pen = QPen(QColor(SPARK_STOP), 1.0, Qt.CustomDashLine)
            pen.setDashPattern([2.0, 2.0])
            p.setPen(pen)
            y = ypos(self.stop)
            p.drawLine(QPointF(0, y), QPointF(w, y))

        p.setPen(QPen(self.color, 1.3))
        p.drawPath(line)
        p.setPen(Qt.NoPen)
        p.setBrush(self.color)
        p.drawEllipse(QPointF(xpos(len(pts) - 1), ypos(pts[-1])), 1.8, 1.8)
        p.end()


def make_tray_icon(color="#26a65b"):
    """Icon tự vẽ: ô bo góc + đường xu hướng trắng, không cần file ảnh."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(QRect(2, 2, 60, 60), 14, 14)
    pen = QPen(QColor("white"), 7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    path = QPainterPath(QPointF(12, 46))
    for x, y in ((26, 32), (36, 40), (52, 18)):
        path.lineTo(x, y)
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    p.end()
    return QIcon(pm)


def input_style():
    return (
        f"QLineEdit{{background:{C('input_bg')};color:{C('fg')};"
        f"border:1px solid {C('input_border')};border-radius:4px;padding:2px 4px;}}"
        f"QLineEdit:focus{{border:1px solid {C('focus')};}}"
    )


def input_err_style():
    return (
        f"QLineEdit{{background:{C('input_bg')};color:{C('fg')};"
        "border:2px solid #e53935;border-radius:4px;padding:1px 3px;}"
    )


def panel_style():
    return (
        f"#panel{{background:{C('bg')};border:1px solid {C('border')};}}"
        f"QLabel{{color:{C('fg')};background:transparent;}}"
        f"QPushButton{{background:{C('btn_bg')};color:{C('fg')};"
        f"border:1px solid {C('input_border')};border-radius:4px;padding:3px 10px;}}"
        f"QPushButton:hover{{background:{C('btn_hover')};}}"
        f"QPushButton:disabled{{color:{C('btn_disabled')};}}"
    )


class ThemeToggle(QWidget):
    """Công tắc sáng/tối: rãnh bo tròn + núm có icon mặt trời (sáng) / trăng (tối).

    Vẽ tay để icon không phụ thuộc font emoji. Click -> on_toggle(theme mới).
    """

    W, H = 46, 24

    def __init__(self, dark, on_toggle):
        super().__init__()
        self.dark = dark
        self.on_toggle = on_toggle
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Chuyển sang giao diện " + ("sáng" if dark else "tối"))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.on_toggle("light" if self.dark else "dark")

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#3b4a6b" if self.dark else "#cfe3f7"))
        p.drawRoundedRect(QRect(0, 0, w, h), h / 2, h / 2)

        r = h / 2 - 2
        cx = w - h / 2 if self.dark else h / 2
        cy = h / 2
        p.setBrush(QColor("#ffffff" if not self.dark else "#1a2236"))
        p.drawEllipse(QPointF(cx, cy), r, r)

        if self.dark:  # trăng khuyết: tròn vàng nhạt, khoét bằng tròn màu núm
            p.setBrush(QColor("#f5d76e"))
            p.drawEllipse(QPointF(cx, cy), r - 3.5, r - 3.5)
            p.setBrush(QColor("#1a2236"))
            p.drawEllipse(QPointF(cx + 3.2, cy - 2.6), r - 4.2, r - 4.2)
        else:          # mặt trời: tròn cam + 8 tia
            sun = QColor("#f5a623")
            p.setBrush(sun)
            p.drawEllipse(QPointF(cx, cy), 3.6, 3.6)
            pen = QPen(sun, 1.6, Qt.SolidLine, Qt.RoundCap)
            p.setPen(pen)
            for i in range(8):
                a = i * math.pi / 4
                p.drawLine(
                    QPointF(cx + math.cos(a) * 5.6, cy + math.sin(a) * 5.6),
                    QPointF(cx + math.cos(a) * 7.8, cy + math.sin(a) * 7.8),
                )
        p.end()


def shake(widget):
    """Viền đỏ + rung báo input sai."""
    widget.setStyleSheet(input_err_style())
    start = widget.pos()
    anim = QPropertyAnimation(widget, b"pos", widget)
    anim.setDuration(300)
    anim.setEasingCurve(QEasingCurve.Linear)
    for t, dx in ((0.0, 0), (0.2, -6), (0.4, 6), (0.6, -4), (0.8, 4), (1.0, 0)):
        anim.setKeyValueAt(t, start + QPoint(dx, 0))
    anim.start(QPropertyAnimation.DeleteWhenStopped)


# ============================================================================
# App
# ============================================================================

class Bridge(QObject):
    """Thread nền -> main thread qua signal (Qt tự queue)."""
    fetched = Signal(dict)
    validated = Signal(dict)


class Panel(QWidget):
    """Cửa sổ popup không viền: click ra ngoài tự đóng (Qt.Popup)."""

    def __init__(self, on_hide):
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint)
        self.on_hide = on_hide
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("panel")
        self.setStyleSheet(panel_style())
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(14, 14, 14, 14)
        self.content = None

    def set_content(self, widget):
        if self.content is not None:
            self.outer.removeWidget(self.content)
            self.content.hide()
            self.content.deleteLater()
        self.content = widget
        self.outer.addWidget(widget)
        # Thêm con vào window đang hiện thì Qt chỉ show nó ở nhịp event sau ->
        # adjustSize lúc này đo ra 28x28 (chỉ có margin), góc neo tính sai và
        # panel bung ra tràn màn. Show + activate layout ngay để đo đúng.
        widget.show()
        self.outer.activate()
        self.adjustSize()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.on_hide()


class StockTrayApp(QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.cfg = load_config()
        self.codes = self.cfg["watchlist"]
        self.targets = self.cfg["targets"]
        self.quantities = self.cfg["quantities"]
        self.dates = self.cfg["dates"]
        set_theme(self.cfg.get("theme", "light"))
        self.last_stocks = None
        self.last_index = None
        self.bot_status = None
        self.bot_status_at = 0.0
        self.status_text = ""
        self.hidden_at = 0.0

        self.bridge = Bridge()
        self.bridge.fetched.connect(self._apply_fetch_result)
        self.bridge.validated.connect(self._after_validate)

        self.icon_ok = make_tray_icon()
        self.icon_err = make_tray_icon("#e53935")
        self.tray = QSystemTrayIcon(self.icon_ok)
        self.tray.setToolTip("VN Stock")
        menu = QMenu()
        act_refresh = QAction("Refresh now", menu)
        act_refresh.triggered.connect(lambda: self.refresh(manual=True))
        act_quit = QAction("Quit", menu)
        act_quit.triggered.connect(self.quit)
        menu.addAction(act_refresh)
        menu.addAction(act_quit)
        self.menu = menu
        self.tray.setContextMenu(menu)  # chuột phải
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        self.panel = Panel(on_hide=self._on_panel_hidden)

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.refresh(manual=False))
        self.timer.start(int(float(self.cfg["refresh_seconds"]) * 1000))

        self.refresh(manual=True)
        self._stamp_dates()
        save_config(self.cfg)
        self._sync_bg()

    # --- data ---
    def refresh(self, manual):
        """Timer chỉ fetch trong phiên; gọi tay thì luôn fetch. Fetch ở thread nền."""
        if not manual and not self._phase()[1]:
            return
        codes = list(self.codes)
        threading.Thread(target=self._fetch_bg, args=(codes,), daemon=True).start()

    def _fetch_bg(self, codes):
        now = datetime.now().strftime("%H:%M:%S")
        want_status = time.time() - self.bot_status_at > BOT_STATUS_TTL
        try:
            with ThreadPoolExecutor(max_workers=3) as ex:
                fut_index = ex.submit(fetch_index)
                fut_stocks = ex.submit(fetch_prices, codes)
                fut_bot = ex.submit(fetch_bot_status, self.cfg.get("sync")) if want_status else None
                index = fut_index.result()
                stocks = fut_stocks.result()
                bot = fut_bot.result() if fut_bot else None
            result = {
                "stocks": stocks, "index": index, "bot": bot,
                "status_tried": want_status,
                "status": f"Cập nhật: {now}", "ok": True,
            }
        except Exception as e:
            print(f"[fetch] lỗi: {e}", flush=True)
            result = {"status": f"⚠ Không cập nhật được ({now})", "ok": False}
        self.bridge.fetched.emit(result)

    def _trading_day(self):
        return (self.bot_status or {}).get("trading_day", True)

    def _phase(self):
        phase, live = market_phase()
        if live and not self._trading_day():
            return "Nghỉ lễ", False
        return phase, live

    def _totals(self):
        by_code = {s["code"]: s for s in (self.last_stocks or [])}
        return portfolio_totals(self.codes, self.targets, self.quantities, by_code)

    def _apply_fetch_result(self, result):
        if result["ok"]:
            self.last_stocks = result["stocks"]
            self.last_index = result["index"]
            if result.get("bot") is not None:
                self.bot_status = result["bot"]
            if result.get("status_tried"):
                self.bot_status_at = time.time()
            self.tray.setIcon(self.icon_ok)
        else:
            self.tray.setIcon(self.icon_err)
        self.status_text = result["status"]
        self._update_tooltip()
        if self.panel.isVisible() and not self._is_editing():
            self._rebuild_panel()

    def _update_tooltip(self):
        self.tray.setToolTip(tray_tooltip(self._totals(), *self._phase(), self.status_text))

    # --- UI ---
    def _is_editing(self):
        """Đang gõ dở ô nào thì rebuild sẽ xoá mất chữ -> để lần sau."""
        w = QApplication.focusWidget()
        return isinstance(w, QLineEdit) and self.panel.isAncestorOf(w)

    def _row_input(self, value, placeholder, width, on_done):
        f = QLineEdit(value)
        f.setPlaceholderText(placeholder)
        f.setAlignment(Qt.AlignRight)
        f.setFixedWidth(width)
        font = QFont(MONO_FAMILY)
        font.setPixelSize(13)
        f.setFont(font)
        f.setStyleSheet(input_style())
        f.editingFinished.connect(lambda: on_done(f))  # Enter / rời ô = lưu
        return f

    def _rebuild_panel(self):
        root = QWidget()
        col = QVBoxLayout(root)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(5)

        # Hàng đầu: VN-Index bên trái, công tắc sáng/tối bên phải.
        head = QHBoxLayout()
        head.addWidget(make_index_label(self.last_index or {"code": "VNINDEX", "price": None}))
        head.addStretch(1)
        head.addWidget(ThemeToggle(self.cfg.get("theme") == "dark", self._on_theme))
        col.addLayout(head)

        by_code = {s["code"]: s for s in (self.last_stocks or [])}
        near_pct = (self.bot_status or {}).get("near_pct", 2)
        bot_by_code = (self.bot_status or {}).get("by_code", {})
        # Cột % chênh / stop: chỉ dựng khi có ít nhất 1 mã có nội dung, kẻo cột
        # rỗng cố định width đẩy nút ✕ ra xa. Có thì mọi row đều có để thẳng hàng.
        show_diff = any(self.targets.get(c) for c in self.codes)
        show_hint = any(bot_by_code.get(c) for c in self.codes)
        # Ghim cùng width cho label giá mọi row: ký tự ▲▼— lấy từ font fallback nên
        # rộng khác nhau dù Consolas là mono -> các cột sau bị lệch.
        row_labels = [make_row_label(by_code.get(c, {"code": c, "price": None}))
                      for c in self.codes]
        label_w = max((lbl.sizeHint().width() for lbl in row_labels), default=0)
        for code, row_label in zip(self.codes, row_labels):
            s = by_code.get(code, {"code": code, "price": None})
            row = QHBoxLayout()
            row.setSpacing(8)
            row_label.setFixedWidth(label_w)
            row.addWidget(row_label)

            info = (self.bot_status or {}).get("by_code", {}).get(code)
            row.addWidget(SparkView(s, bot_stop(info)))

            target = self.targets.get(code)
            qty = self.quantities.get(code)
            row.addWidget(self._row_input(
                f"{target:g}" if target is not None else "", "giá", 60,
                lambda f, c=code: self._on_target_changed(c, f),
            ))
            row.addWidget(self._row_input(
                f"{qty}" if qty else "", "vol", 72,
                lambda f, c=code: self._on_qty_changed(c, f),
            ))

            if show_diff:
                diff = format_target_diff(s.get("price"), target)
                row.addWidget(make_fixed_label(
                    diff[0] if diff else "", STATUS_COLORS[diff[1]] if diff else C("muted"), 60,
                ))
            if show_hint:
                hint_text, hint_color = format_bot_hint(s.get("price"), info, near_pct)
                row.addWidget(make_fixed_label(hint_text, hint_color, 140))

            row.addStretch(1)
            x_btn = QPushButton("✕")
            x_btn.setFixedWidth(28)
            x_btn.clicked.connect(lambda _=False, c=code: self._on_remove(c))
            row.addWidget(x_btn)
            col.addLayout(row)

        totals = self._totals()
        if totals:
            col.addWidget(make_total_label(totals))
            col.addWidget(make_day_label(totals))

        col.addWidget(make_phase_label(self._phase()[0], self.status_text))

        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Mã CK…")
        self.input_field.setFixedWidth(100)
        self.input_field.setStyleSheet(input_style())
        self.input_field.returnPressed.connect(self._on_add)
        add_row.addWidget(self.input_field)
        self.add_btn = QPushButton("Add")
        self.add_btn.clicked.connect(self._on_add)
        add_row.addWidget(self.add_btn)
        add_row.addStretch(1)
        col.addLayout(add_row)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh now")
        refresh_btn.clicked.connect(lambda: self.refresh(manual=True))
        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(self.quit)
        btn_row.addWidget(refresh_btn)
        btn_row.addWidget(quit_btn)
        btn_row.addStretch(1)
        col.addLayout(btn_row)

        self.panel.set_content(root)
        if self.panel.isVisible():
            self._position_panel()
            self.panel.setFocus()  # không tự focus ô nhập nào

    def _on_theme(self, name):
        self.cfg["theme"] = name
        save_config(self.cfg)
        set_theme(name)
        self.panel.setStyleSheet(panel_style())
        # Đang trong mousePressEvent của chính công tắc sắp bị xoá -> nhịp sau.
        QTimer.singleShot(0, self._rebuild_panel)

    def _position_panel(self):
        """Neo panel ở góc vùng làm việc sát taskbar, như flyout âm lượng/mạng của Windows.

        Không bám tray.geometry(): icon nằm trong khay ẩn (^) hoặc nhiều màn hình
        thì Win11 trả toạ độ sai, panel rơi lung tung giữa màn hình.
        Màn hình chốt lúc click icon (self.panel_screen), KHÔNG lấy lại theo con
        trỏ mỗi lần rebuild: fetch xong vài giây sau thì chuột có thể đã sang màn
        khác -> panel nhảy màn (và lệch DPI nên đè taskbar).
        """
        self.panel.adjustSize()
        size = self.panel.size()
        screen = getattr(self, "panel_screen", None)
        if screen not in QApplication.screens():  # màn đã rút ra
            screen = QApplication.primaryScreen()
        full, avail = screen.geometry(), screen.availableGeometry()
        m = 12
        # Taskbar nằm ở cạnh nào = cạnh mà vùng làm việc bị hụt so với màn hình.
        if avail.top() > full.top():          # taskbar trên
            x, y = avail.right() - size.width() - m, avail.top() + m
        elif avail.left() > full.left():      # taskbar trái
            x, y = avail.left() + m, avail.bottom() - size.height() - m
        else:                                  # dưới (mặc định) hoặc phải
            x, y = avail.right() - size.width() - m, avail.bottom() - size.height() - m
        self.panel.move(max(avail.left(), x), max(avail.top(), y))

    def _on_tray_activated(self, reason):
        if reason not in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            return
        if self.panel.isVisible():
            self.panel.hide()
            return
        # Click lên icon khi panel đang mở: Qt.Popup đã tự đóng vì click ngoài,
        # rồi activated tới ngay sau -> đừng mở lại.
        if time.time() - self.hidden_at < 0.3:
            return
        # Màn vừa click icon = màn có con trỏ ngay lúc này; giữ cố định tới lần mở sau.
        self.panel_screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        self.refresh(manual=True)
        self._rebuild_panel()
        self.panel.setScreen(self.panel_screen)
        self._position_panel()
        self.panel.show()
        self.panel.activateWindow()
        self.panel.setFocus()

    def _on_panel_hidden(self):
        self.hidden_at = time.time()

    def quit(self):
        self.tray.hide()
        self.app.quit()

    # --- edit watchlist ---
    def _on_add(self):
        self.input_field.setStyleSheet(input_style())
        code = normalize_code(self.input_field.text())
        if not code or code in self.codes:
            shake(self.input_field)
            return
        self.add_btn.setEnabled(False)
        self.add_btn.setText("Checking…")
        threading.Thread(target=self._validate_bg, args=(code,), daemon=True).start()

    def _validate_bg(self, code):
        try:
            valid = fetch_one(code, sessions=1).get("price") is not None
        except Exception:
            valid = False
        self.bridge.validated.emit({"code": code, "valid": valid})

    def _after_validate(self, result):
        try:
            self.add_btn.setEnabled(True)
            self.add_btn.setText("Add")
        except RuntimeError:  # panel đã rebuild, nút cũ bị xoá
            pass
        if not result["valid"]:
            try:
                shake(self.input_field)
            except RuntimeError:
                pass
            return
        if result["code"] not in self.codes:
            self.codes.append(result["code"])
        self._apply_watchlist_change()

    def _on_remove(self, code):
        if code in self.codes:
            self.codes.remove(code)
            self.targets.pop(code, None)
            self.quantities.pop(code, None)
            self._apply_watchlist_change()

    # --- giá mua / vol đã mua ---
    def _on_target_changed(self, code, field):
        if code not in self.codes:
            return
        raw = field.text().strip().replace(",", ".")
        if not raw:
            if self.targets.pop(code, None) is not None:
                self._apply_portfolio_change()
            return
        try:
            val = float(raw)
        except ValueError:
            shake(field)
            return
        if val <= 0:
            shake(field)
            return
        if self.targets.get(code) == val:  # rời ô mà không sửa -> khỏi lưu/sync
            return
        self.targets[code] = val
        field.setStyleSheet(input_style())
        self._apply_portfolio_change()

    def _on_qty_changed(self, code, field):
        if code not in self.codes:
            return
        raw = field.text().strip().replace(",", "").replace(".", "")
        if not raw:
            if self.quantities.pop(code, None) is not None:
                self._apply_portfolio_change()
            return
        try:
            qty = int(raw)
        except ValueError:
            shake(field)
            return
        if qty <= 0:
            shake(field)
            return
        if self.quantities.get(code) == qty:
            return
        self.quantities[code] = qty
        field.setStyleSheet(input_style())
        self._apply_portfolio_change()

    def _stamp_dates(self):
        today = datetime.now().strftime("%Y-%m-%d")
        stamp_dates(self.targets, self.quantities, self.dates, today)
        self.cfg["dates"] = self.dates

    def _sync_bg(self):
        holdings = build_holdings(self.targets, self.quantities, self.dates)
        threading.Thread(
            target=push_holdings, args=(self.cfg.get("sync"), holdings), daemon=True
        ).start()

    def _apply_portfolio_change(self):
        self.cfg["targets"] = self.targets
        self.cfg["quantities"] = self.quantities
        self._stamp_dates()
        save_config(self.cfg)
        # editingFinished đang chạy trong chính ô sắp bị xoá -> rebuild ở nhịp sau.
        QTimer.singleShot(0, self._rebuild_panel)
        self._update_tooltip()
        self._sync_bg()
        self.bot_status_at = 0.0

    def _apply_watchlist_change(self):
        self.cfg["watchlist"] = self.codes
        self.cfg["targets"] = self.targets
        self.cfg["quantities"] = self.quantities
        self._stamp_dates()
        save_config(self.cfg)
        QTimer.singleShot(0, self._rebuild_panel)
        self.bot_status_at = 0.0
        self.refresh(manual=True)
        self._sync_bg()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # chỉ sống ở tray
    # Guard trùng instance: 2 icon tray thì sync bot đẩy 2 lần, khó hiểu.
    lock = QLockFile(os.path.join(QDir.tempPath(), "vnstock-tray.lock"))
    if not lock.tryLock(100):
        print("App đang chạy sẵn rồi.", flush=True)
        return 0
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("Không có system tray.", flush=True)
        return 1
    _keep = StockTrayApp(app)  # noqa: F841 — giữ reference
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
