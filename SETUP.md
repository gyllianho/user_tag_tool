# Setup Google Sheets API (1 lần duy nhất)

## Bước 1 — Tạo credentials.json

1. Vào: https://console.cloud.google.com/
2. Tạo project mới (hoặc chọn project có sẵn)
3. Vào **APIs & Services → Enable APIs** → bật:
   - Google Sheets API
   - Google Drive API
4. Vào **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Đặt tên bất kỳ, bấm Create
5. Download JSON → đổi tên thành `credentials.json`
6. Đặt file vào thư mục `hihub_tool/`

## Bước 2 — Chạy app lần đầu

```bash
cd ~/hihub_tool
python3 app.py
```

- Lần đầu chạy sẽ mở browser để đăng nhập Google
- Chọn tài khoản có quyền đọc Sheet
- Sau khi auth xong, `token.json` được lưu → các lần sau không cần auth lại

## Chạy hàng ngày

```bash
cd ~/hihub_tool && python3 app.py
# Mở http://localhost:5001
```
