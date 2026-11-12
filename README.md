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

Studio chạy theo 3 bước: **1. Nội dung tóm tắt** (cũng là chữ trên video) → **2. Import ảnh / Gen ảnh AI** → **3. Generate Reels**.

- **Gen ảnh AI**: Gemini đọc tóm tắt của dòng đang chọn, viết prompt rồi tạo ảnh và tự gán vào dòng.
- **Regenerate** (nút ↻ trên thumbnail thư viện): Gemini tạo lại ảnh import thành ảnh mới, giữ chủ thể, bỏ chữ/watermark.
- Mỗi tỉ lệ được chọn có ảnh AI riêng (cùng cảnh, khung khác); ảnh 9:16 dùng làm khung video.
- **Tạo ảnh cần bật billing** cho project Gemini: gói Free không có quota cho model tạo ảnh (lỗi 429).

Cấu hình (biến môi trường, local để trong file `.env` ở thư mục gốc — đã có trong `.gitignore`):

```bash
GEMINI_API_KEY=...   # https://aistudio.google.com/apikey
# Chuỗi model, cách nhau dấu phẩy: model đầu quá tải / hết quota thì tự thử model sau.
GEMINI_IMAGE_MODEL=gemini-3.1-flash-image,gemini-2.5-flash-image
GEMINI_TEXT_MODEL=gemini-flash-latest,gemini-3.5-flash,gemini-3.1-flash-lite,gemini-3.5-flash-lite
REELS_AI_WORKERS=3   # số lệnh Gemini song song
```

Không có key thì Studio vẫn tạo video và xuất ảnh từ ảnh import; các nút AI bị tắt.

## Agent Workspace (`/agents`)

Trang chat để trao đổi và điều phối 3 agent (Gemini, gọi tool thật):

| Agent | Việc làm | Tool |
|-------|----------|------|
| **Web truyện · Điều phối** | Nơi bạn giao việc. Quản lý CMS truyện, chuyển việc cho 2 agent kia | Tổng quan CMS, tìm / xem truyện, thống kê lượt xem, bật/tắt xuất bản & đề cử, sửa tóm tắt, xem thư viện ảnh, giao việc |
| **Tạo content** | Ảnh AI, video Reels, ảnh post, ZIP | Dùng chung thư viện + pipeline với Studio |
| **Research content** | Ý tưởng, hook, tóm tắt cho content (logic chi tiết bổ sung sau) | Đọc CMS + thư viện ảnh |

- Mỗi agent có hội thoại riêng (lưu ở `data/agents/`). Việc Điều phối giao xuất hiện trong hội thoại của agent nhận, gắn nhãn "Giao từ…".
- Agent không có quyền xóa truyện/chương; thay đổi CMS chỉ khi bạn yêu cầu rõ. Trang public cập nhật sau ~60 giây (ISR).
- CMS: agent đọc `NEXT_PUBLIC_SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` từ `.env.local` của CMS qua `CMS_ENV_FILE`, hoặc đặt trực tiếp `CMS_SUPABASE_URL` + `CMS_SUPABASE_SERVICE_ROLE_KEY`.

```bash
CMS_ENV_FILE=/đường/dẫn/Web truyện/app/.env.local
CMS_SITE_URL=http://localhost:3000   # để tạo link tới trang admin
AGENTS_TOKEN=...                      # bắt buộc khi deploy công khai (Render): API agent yêu cầu token
AGENT_TEXT_MODEL=...                  # tuỳ chọn, mặc định dùng GEMINI_TEXT_MODEL
```

Thêm agent / tool mới: khai báo trong `agents/registry.py`; mỗi tool là một hàm Python có docstring (`agents/*_tools.py`).

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

## Phân tích page Facebook (hiệu quả post + trending 24h)

**Trang `/trending`** (chạy local: http://127.0.0.1:8000/trending): dán link các page Facebook cần theo dõi →
**Analyze** → crawl các post mới của từng page rồi trả về các post có hiệu quả tốt. Mọi post trên page đều được
đánh giá (không lọc chủ đề). Danh sách page vừa phân tích được lưu vào `data/trending/pages.json` để crawl định kỳ.

Agent **Research content** có tool `fb_trending_top` (top 10 post/reels hot nhất trong 24h của các page đang theo
dõi) và `fb_crawl_now`. Dữ liệu lấy bằng Playwright, chỉ chạy local (Render free không chạy được Chromium).

Hai chế độ:

| | Chế độ khách (mặc định, không cần tài khoản) | Có cookie tài khoản FB phụ |
|---|---|---|
| Post / reels trên feed page | ~6 mới nhất mỗi lần + mở lại link post cũ hơn để cập nhật | ~20 mỗi page |
| Tab Reels | Không đọc (khi không đăng nhập chỉ trả reels cũ) | Có |
| Tìm theo từ khoá | Không | Có |

Chế độ khách vẫn đủ cho top 24h nếu crawl mỗi 2 giờ: post mới được bắt khi còn trong 6 bài đầu, sau đó được theo
dõi tiếp qua link riêng (`max_refresh` trong `pages.json`). Facebook có thể giới hạn theo IP nếu crawl dày.

Cài đặt:

```bash
pip install -r requirements.txt
playwright install chromium
mkdir -p data/trending && cp trending/pages.example.json data/trending/pages.json   # điền page + từ khoá
```

Tuỳ chọn, trong `.env`: `FB_COOKIES_FILE=/đường/dẫn/fb_cookies.json` (export bằng extension Cookie-Editor khi đang
đăng nhập facebook.com; cần có `c_user` và `xs`). Để trống thì chạy chế độ khách.

Chạy:

```bash
python scripts/fb_trending_crawl.py --top 10       # crawl 1 lần rồi in top 10
python scripts/fb_trending_crawl.py --top-only     # chỉ in top từ dữ liệu đã lưu
python scripts/fb_trending_crawl.py --headful      # mở cửa sổ trình duyệt để debug
```

Cần crawl định kỳ (~2 giờ/lần) để đo được tăng trưởng 24h và tốc độ. Ví dụ crontab (`crontab -e`):

```
0 */2 * * * cd /Users/macos/Documents/crawl && .venv/bin/python scripts/fb_trending_crawl.py --json >> data/trending/cron.log 2>&1
```

Cách chấm điểm:

- Tương tác `E = cảm xúc + 3×bình luận + 5×chia sẻ + 0.02×lượt xem` (bình luận "xin link" là tín hiệu traffic).
- **Hiệu quả** (`perf_ratio`, dùng ở trang `/trending`) = `E / √(min(1, tuổi_giờ/48))` chia cho trung vị của cùng
  page (post và reels so riêng, 14 ngày gần nhất) + 20. ×1 = ngang post bình thường của page; Tốt ≥ ×1, Rất tốt ≥ ×2.
  Chỉ cần 1 lần crawl là có kết quả.
- `Δ24h` = E tăng thêm trong 24h (post chưa đủ 24h tuổi thì là toàn bộ E); `velocity` = E/giờ giữa 2 lần crawl
  cuối; "đang lên" khi velocity gần nhất lớn hơn 1.2 lần lần trước.
- `outperform = Δ24h / (median E 24h đầu của các post cũ cùng page + 50)` để page nhỏ có bài bùng nổ vẫn lên top.
- `score = 0.4·pct(Δ24h) + 0.3·pct(velocity) + 0.3·pct(outperform)` (percentile trong tập ứng viên).
- Ứng viên: mọi post của page đăng trong 72h (trang `/trending` chọn được 24h – 14 ngày).
- Top của agent: gộp các post cùng truyện (tên truyện / 120 ký tự đầu caption giống ≥ 80%), tối đa 2 post mỗi page.

Khi bị Facebook checkpoint / đăng xuất, crawler dừng ngay và ghi lỗi vào lần chạy. Nếu một page không bắt được
post nào, mẫu response được lưu ở `data/trending/debug/` để chỉnh parser.

## Cấu trúc

| File | Mô tả |
|------|--------|
| `app.py` | FastAPI: thư viện ảnh, generate video, AI ảnh, xuất ảnh/ZIP, landing |
| `ai_image.py` | Gemini: tóm tắt → prompt → ảnh, regenerate ảnh import, chuỗi model dự phòng |
| `agents/` | Agent Workspace: runtime (vòng gọi tool), 3 agent, tool CMS / content, route `/agents` |
| `reel_core.py` | Render MP4, overlay chữ, xuất ảnh post theo tỉ lệ |
| `templates/landing.html` | Landing + Studio |
| `scripts/build_github_pages.py` | Build static cho Pages |
| `trending/` | Crawl Facebook (Playwright), SQLite snapshot, chấm điểm hiệu quả / trending, route `/trending` |
| `agents/trending_tools.py` | Tool `fb_trending_top`, `fb_crawl_now` cho Agent Research |
| `scripts/fb_trending_crawl.py` | CLI crawl định kỳ + in top |
