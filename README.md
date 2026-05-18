# Reels Batch

**Ứng dụng web:** [https://lamtrang2405.github.io/reels/](https://lamtrang2405.github.io/reels/)

Tạo hàng loạt Facebook Reels (9:16, 1080×1920, 60fps, MP4) từ ảnh + chữ.

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
| `app.py` | FastAPI: thư viện ảnh, generate, landing |
| `reel_core.py` | Render MP4, overlay chữ |
| `templates/landing.html` | Landing + Studio |
| `scripts/build_github_pages.py` | Build static cho Pages |
