# VN Stock Menu Bar App — Design

Ngày: 2026-07-01

## Mục tiêu

App macOS menu bar cá nhân: hiện icon nhỏ trên topbar, click vào xem giá gần
realtime của các mã cổ phiếu VN trong watchlist. Chạy cá nhân, tự bật bằng
terminal. Ưu tiên đơn giản.

## Quyết định đã chốt

| Vấn đề | Lựa chọn |
|--------|----------|
| Theo dõi gì | Watchlist các mã user chọn (không phải index) |
| Nguồn data | VNDirect finfo API (không chính thức, không cần key) — đã verify sống |
| Tech stack | Python + rumps (đơn giản nhất cho user, không cần Xcode) |
| Mức dùng | Cá nhân, tự bật bằng terminal (không đóng gói .app, không auto-start) |
| Topbar hiển thị | Chỉ icon 📈, click mới xem list |
| Refresh | Timer nền 30s + nút "Refresh now" |

## Kiến trúc

Một file `app.py` (~100 dòng), 3 phần tách bạch:

```
vnstock-menubar/
├── app.py          # rumps app + fetch + format
├── config.json     # watchlist + refresh_seconds
├── requirements.txt
└── run.sh          # helper chạy
```

1. **`fetch_prices(codes) -> list[dict]`** — phần duy nhất chạm mạng. Gọi
   VNDirect API, trả list `{code, name, price, change, changePct}`. Test được độc lập.
2. **`format_row(stock) -> str`** — biến 1 mã thành 1 dòng menu, VD
   `HPG   23.45   ▲ +2.99%`. Dùng ▲/▼ thay màu (menu item text thuần).
3. **`StockBarApp(rumps.App)`** — vỏ menu bar: icon topbar, dropdown watchlist,
   timer refresh, item "Refresh now" + "Quit".

## Data flow

```
Timer (30s)      ─┐
Click "Refresh"  ─┼─▶ fetch_prices() ─▶ format_row() ─▶ cập nhật menu items
App khởi động    ─┘
```

- Topbar chỉ hiện icon 📈. Không hiện số.
- Click icon → dropdown liệt kê từng mã watchlist, mỗi mã 1 dòng.
- Dòng cuối: "Cập nhật: HH:MM" + separator + "Refresh now" + "Quit".

## API (đã verify 2026-07-01)

```
GET https://api-finfo.vndirect.com.vn/v4/change_prices/latest?filter=code:HPG,FPT,VCB
Header: User-Agent: Mozilla/5.0
```

Response `data[]` mỗi phần tử:
```json
{ "code":"HPG", "name":"Hòa Phát", "price":23.45,
  "change":0.15, "changePct":0.64378, "lastUpdated":"2026-07-01 14:07" }
```

Nhận nhiều mã 1 request (`code:A,B,C`), không cần key. Trong giờ giao dịch
`lastUpdated` bám sát giờ hiện tại → gần realtime.

## Error handling

- Lỗi mạng / API đổi / rỗng → KHÔNG crash. Giữ giá cũ, đổi icon ⚠️, dòng cuối
  ghi "Không cập nhật được (HH:MM)".
- Ngoài giờ giao dịch / cuối tuần → API trả giá đóng cửa gần nhất, app hiện
  bình thường, không cần logic riêng.

## Config

```json
{ "watchlist": ["HPG", "FPT", "VCB"], "refresh_seconds": 30 }
```

Thêm/bớt mã bằng cách sửa file này, không đụng code.

## Cách chạy

```bash
pip install -r requirements.txt
python app.py       # hoặc ./run.sh
```

## YAGNI — cố tình KHÔNG làm

- Không đóng gói `.app` / auto-start.
- Không hiện số/xoay vòng mã trên topbar.
- Không settings UI (sửa config bằng file).
- Không lưu lịch sử, không biểu đồ.

## Độ khó

Dễ. ~100 dòng. Mắt xích rủi ro nhất (API realtime) đã verify sống trước khi thiết kế.
