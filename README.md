# Reels Batch

**Ứng dụng web:** [https://lamtrang2405.github.io/reels/](https://lamtrang2405.github.io/reels/)

Tạo hàng loạt Facebook Reels / TikTok (9:16, 1080×1920, 60fps, MP4) từ ảnh + chữ, và xuất ảnh post sạch (không chữ) cho Facebook/TikTok.

## Định dạng xuất

| Loại | Tỉ lệ | Kích thước | Dùng cho |
|------|-------|------------|----------|
| Video MP4 | 9:16 | 1080×1920, 60fps | Reels, TikTok |
| Ảnh JPG | 9:16 | 1080×1920 | TikTok photo, Story |
| Ảnh JPG | 4:5 | 1080×1350 | Facebook post |
| Ảnh JPG | 1:1 | 1080×1080 | Post vuông |

Nút **Tải tất cả (ZIP)** gom video + ảnh của phiên làm việc thành một file.

## Ảnh AI (Gemini)

- **Regenerate** (nút ↻ trên thumbnail thư viện): Gemini tạo lại ảnh import thành ảnh mới, giữ chủ thể, bỏ chữ/watermark.
- **Tạo ảnh AI** (cột *Tóm tắt* mỗi dòng): Gemini đọc phần tóm tắt (trống thì dùng cột *Nội dung*), viết prompt rồi tạo ảnh và tự gán vào dòng.
- Mỗi tỉ lệ được chọn có ảnh AI riêng (cùng cảnh, khung khác); ảnh 9:16 dùng làm khung video.

Cấu hình (biến môi trường, local để trong file `.env` ở thư mục gốc — đã có trong `.gitignore`):

```bash
GEMINI_API_KEY=...                      # https://aistudio.google.com/apikey
GEMINI_IMAGE_MODEL=gemini-2.5-flash-image  # tuỳ chọn
GEMINI_TEXT_MODEL=gemini-2.5-flash         # tuỳ chọn
REELS_AI_WORKERS=3                         # số lệnh Gemini song song
```

Không có key thì Studio vẫn tạo video và xuất ảnh từ ảnh import; các nút AI bị tắt.

## Chạy local

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

Mở http://127.0.0.1:8000

## GitHub Pages (landing + Studio UI)

- **Trang:** https://lamtrang2405.github.io/reels/
- Workflow `.github/workflows/deploy-pages.yml` build từ `templates/landing.html` (giống local).
- Studio trên Pages gọi API qua biến `REELS_API_BASE`.

### Bật Pages (lần đầu)

1. Repo **Settings → Pages → Build and deployment**: **GitHub Actions**.
2. Push `main` — workflow tự deploy.

### API backend (Render, miễn phí)

1. [Render](https://render.com) → **New → Blueprint** → chọn repo `reels` → dùng `render.yaml`.
2. URL service (vd. `https://reels-api-8fj1.onrender.com`) — kiểm tra `/health` → `{"ok":true,...}`.
   Vào **Environment** của service, nhập `GEMINI_API_KEY` để bật AI (kiểm tra `/api/config` → `"ai_enabled": true`).
   Lưu ý: gói free dùng ổ đĩa tạm và ngủ khi không dùng — thư viện/ảnh/video mất sau khi restart, nên tải ZIP ngay sau khi tạo.
3. (Tuỳ chọn) GitHub **Variables** → `REELS_API_BASE` = URL Render (không `/` cuối). Mặc định build đã dùng URL trên.
4. Chạy lại workflow **Deploy GitHub Pages** (hoặc push commit nhỏ).

## CLI (Excel)

```bash
python generate_reels.py --excel sheet.xlsx --output-dir reels_out
python generate_reels.py --excel sheet.xlsx --text-bg white
```

## Cấu trúc

| File | Mô tả |
|------|--------|
| `app.py` | FastAPI: thư viện ảnh, generate video, AI ảnh, xuất ảnh/ZIP, landing |
| `ai_image.py` | Gemini: tóm tắt → prompt → ảnh, regenerate ảnh import |
| `reel_core.py` | Render MP4, overlay chữ, xuất ảnh post theo tỉ lệ |
| `templates/landing.html` | Landing + Studio |
| `scripts/build_github_pages.py` | Build static cho Pages |
