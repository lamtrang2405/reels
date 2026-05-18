"""Web UI: library + 3-column table → batch Reels MP4."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from reel_core import (
    CAPTION_FONT_SIZE_PX,
    CAPTION_INNER_PAD_BOTTOM,
    CAPTION_INNER_PAD_TOP,
    LIBRARY_STORE_H,
    LIBRARY_STORE_W,
    OVERLAY_MAX_WIDTH_FRAC,
    OVERLAY_TEXT_GUIDELINE_CHARS,
    OVERLAY_TEXT_SAFETY_MAX_CHARS,
    estimate_dense_caption_char_budget,
    ffmpeg_binary,
    load_image_from_bytes,
    normalize_to_916_rgb,
    render_reel_mp4,
)

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
LIBRARY_DIR = DATA / "library"
OUTPUT_DIR = DATA / "outputs"
INDEX_PATH = DATA / "library_index.json"

ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000

_index_lock = Lock()
_executor_workers = min(8, __import__("os").cpu_count() or 4)

app = FastAPI(title="Reels Batch")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _ensure_dirs() -> None:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _load_index() -> list[dict]:
    _ensure_dirs()
    if not INDEX_PATH.is_file():
        return []
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _save_index(entries: list[dict]) -> None:
    INDEX_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")


@app.on_event("startup")
def _startup() -> None:
    _ensure_dirs()
    try:
        ffmpeg_binary()
    except RuntimeError:
        pass


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/config")
def api_config() -> JSONResponse:
    est = estimate_dense_caption_char_budget()
    return JSONResponse(
        {
            "text_guideline_chars": OVERLAY_TEXT_GUIDELINE_CHARS,
            "estimated_chars_dense_layout": est,
            "caption_inner_pad_vertical_px": CAPTION_INNER_PAD_TOP,
            "overlay_width_frac": OVERLAY_MAX_WIDTH_FRAC,
            "text_safety_max_chars": OVERLAY_TEXT_SAFETY_MAX_CHARS,
            "text_hard_max_chars": OVERLAY_TEXT_SAFETY_MAX_CHARS,
            "caption_font_size_px": CAPTION_FONT_SIZE_PX,
            "text_note": (
                "Nội dung trong ô được giữ nguyên (xuống dòng và đoạn văn). "
                "Một dòng trống giữa các đoạn (nhấn Enter hai lần liên tiếp) = đoạn mới; "
                "một lần Enter = xuống dòng trong cùng đoạn. "
                f"Trên video: {CAPTION_FONT_SIZE_PX}px, chữ trắng — mỗi dòng có nền đen riêng (bôi nền), "
                f"căn giữa khung; rộng tối đa ~{int(OVERLAY_MAX_WIDTH_FRAC * 100)}% khung. "
                f"Khoảng cách cột chữ: {CAPTION_INNER_PAD_TOP}px trên / {CAPTION_INNER_PAD_BOTTOM}px dưới. "
                f"Nếu quá dài theo chiều dọc, phần cuối có thể bị cắt và thêm dấu … "
                f"Ước lượng ~{est} ký tự để lấp gần hết chiều cao ở {CAPTION_FONT_SIZE_PX}px. "
                f"Giới hạn an toàn ~{OVERLAY_TEXT_SAFETY_MAX_CHARS} ký tự."
            ),
        }
    )


@app.get("/api/library")
def list_library() -> JSONResponse:
    with _index_lock:
        entries = _load_index()
    out = []
    for e in entries:
        fid = e.get("id")
        fn = e.get("filename")
        if not fid or not fn:
            continue
        p = LIBRARY_DIR / fn
        if not p.is_file():
            continue
        out.append(
            {
                "id": fid,
                "original_name": e.get("original_name", fn),
                "url": f"/api/library/file/{fid}",
            }
        )
    return JSONResponse(out)


@app.get("/api/library/file/{image_id}")
def library_file(image_id: str) -> FileResponse:
    entry = _find_entry(image_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Unknown image")
    path = LIBRARY_DIR / entry["filename"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Missing file")
    return FileResponse(path)


def _find_entry(image_id: str) -> dict | None:
    with _index_lock:
        for e in _load_index():
            if e.get("id") == image_id:
                return e
    return None


def _upload_error(name: str, code: str, detail: str) -> dict:
    return {"name": name, "code": code, "detail": detail}


def _save_library_upload(raw_name: str, body: bytes) -> dict:
    label = Path(raw_name or "upload").name
    if len(body) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"Tệp quá lớn ({len(body) // (1024 * 1024)}MB). Tối đa {MAX_UPLOAD_BYTES // (1024 * 1024)}MB."
        )

    suf = Path(raw_name).suffix.lower()
    if suf and suf not in ALLOWED_SUFFIXES and suf not in {".heic", ".heif", ".avif"}:
        raise ValueError(
            f"Định dạng {suf} không hỗ trợ. Dùng JPG, PNG, WEBP hoặc GIF."
        )

    try:
        im = load_image_from_bytes(body)
    except ValueError:
        raise
    except Exception as err:
        raise ValueError(f"Không đọc được ảnh: {err}") from err

    pixels = im.width * im.height
    if pixels > MAX_IMAGE_PIXELS:
        raise ValueError(
            f"Ảnh quá lớn ({im.width}×{im.height}). Giảm kích thước hoặc nén trước khi tải lên."
        )

    src_size = f"{im.width}×{im.height}"
    try:
        normalized, was_cropped = normalize_to_916_rgb(im, LIBRARY_STORE_W, LIBRARY_STORE_H)
    except Exception as err:
        raise ValueError(f"Không xử lý được ảnh (scale/crop 9:16): {err}") from err

    image_id = uuid.uuid4().hex
    stored = f"{image_id}.jpg"
    path = LIBRARY_DIR / stored
    _ensure_dirs()
    normalized.save(path, format="JPEG", quality=92, optimize=True)

    item = {
        "id": image_id,
        "filename": stored,
        "original_name": label,
        "created": datetime.now(timezone.utc).isoformat(),
        "source_size": src_size,
        "stored_size": f"{LIBRARY_STORE_W}×{LIBRARY_STORE_H}",
        "normalized_916": was_cropped,
    }
    with _index_lock:
        entries = _load_index()
        entries.insert(0, item)
        _save_index(entries)
    return item


@app.post("/api/library/upload")
async def library_upload(
    files: list[UploadFile] | None = File(None),
    file: UploadFile | None = File(None),
) -> JSONResponse:
    uploads: list[UploadFile] = []
    if files:
        uploads.extend(files)
    if file:
        uploads.append(file)
    if not uploads:
        return JSONResponse(
            {
                "ok": False,
                "items": [],
                "errors": [
                    _upload_error(
                        "",
                        "no_files",
                        "Không có tệp nào. Chọn ảnh JPG/PNG/WEBP/GIF (tối đa 40MB).",
                    )
                ],
            },
            status_code=400,
        )

    items: list[dict] = []
    errors: list[dict] = []

    for uf in uploads:
        label = Path(uf.filename or "upload").name
        try:
            body = await uf.read()
            if not body:
                errors.append(
                    _upload_error(label, "empty_file", "Tệp rỗng (0 byte).")
                )
                continue
            item = _save_library_upload(uf.filename or "upload", body)
            items.append(
                {
                    "id": item["id"],
                    "original_name": item["original_name"],
                    "url": f"/api/library/file/{item['id']}",
                    "source_size": item.get("source_size"),
                    "stored_size": item.get("stored_size"),
                    "normalized_916": item.get("normalized_916", False),
                }
            )
        except ValueError as e:
            errors.append(_upload_error(label, "invalid_image", str(e)))
        except Exception as e:  # noqa: BLE001
            errors.append(
                _upload_error(label, "processing_failed", f"Lỗi xử lý: {e}")
            )

    ok = len(items) > 0 and len(errors) == 0
    status = 200 if items else 400
    return JSONResponse(
        {
            "ok": ok,
            "items": items,
            "errors": errors,
            "message": _upload_summary_message(items, errors),
        },
        status_code=status if not items else 200,
    )


def _upload_summary_message(items: list[dict], errors: list[dict]) -> str:
    if items and not errors:
        cropped = sum(1 for x in items if x.get("normalized_916"))
        if cropped:
            return (
                f"Đã lưu {len(items)} ảnh. {cropped} ảnh đã được scale + crop về 9:16 (1080×1920)."
            )
        return f"Đã lưu {len(items)} ảnh (đã đúng tỉ lệ 9:16)."
    if items and errors:
        return f"Lưu được {len(items)} ảnh; {len(errors)} ảnh lỗi (xem chi tiết bên dưới)."
    if errors:
        return f"Không tải được ảnh nào ({len(errors)} lỗi). Xem chi tiết bên dưới."
    return "Không có ảnh được tải lên."


@app.delete("/api/library/{image_id}")
def library_delete(image_id: str) -> JSONResponse:
    with _index_lock:
        entries = _load_index()
        kept: list[dict] = []
        removed: dict | None = None
        for e in entries:
            if e.get("id") == image_id:
                removed = e
                continue
            kept.append(e)
        if removed is None:
            raise HTTPException(status_code=404, detail="Unknown image")
        _save_index(kept)

    fn = removed.get("filename")
    if fn:
        path = LIBRARY_DIR / fn
        path.unlink(missing_ok=True)

    return JSONResponse({"ok": True, "id": image_id})


class RowModel(BaseModel):
    no: int = Field(..., ge=1, le=99999)
    text: str = Field("", max_length=OVERLAY_TEXT_SAFETY_MAX_CHARS)
    image_id: str = ""


class GenerateBody(BaseModel):
    rows: list[RowModel]
    duration: float = Field(5.0, ge=0.5, le=600.0)


def _one_render(no: int, image_id: str, text: str, duration: float) -> tuple[int, str | None, str | None]:
    entry = _find_entry(image_id)
    if not entry:
        return no, None, "unknown image_id"
    img_path = LIBRARY_DIR / entry["filename"]
    if not img_path.is_file():
        return no, None, "missing library file"
    name = f"reel_{no:04d}.mp4"
    out = OUTPUT_DIR / name
    try:
        render_reel_mp4(img_path, text, out, duration=duration, font_path=None)
        return no, name, None
    except Exception as e:  # noqa: BLE001
        return no, None, str(e)


@app.post("/api/generate")
def generate(body: GenerateBody) -> JSONResponse:
    try:
        ffmpeg_binary()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    rows = sorted(body.rows, key=lambda r: r.no)
    items: list[dict] = []
    errors: list[dict] = []

    valid_jobs: list[tuple[int, str, str]] = []
    for r in rows:
        if not r.image_id.strip():
            errors.append({"no": r.no, "error": "missing image"})
            continue
        valid_jobs.append((r.no, r.image_id.strip(), r.text))

    if not valid_jobs:
        return JSONResponse({"ok": False, "items": [], "errors": errors})

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=_executor_workers) as ex:
        futs = {
            ex.submit(_one_render, no, iid, txt, body.duration): no
            for no, iid, txt in valid_jobs
        }
        for fut in as_completed(futs):
            no, fname, err = fut.result()
            if err:
                errors.append({"no": no, "error": err})
            elif fname:
                items.append({"no": no, "filename": fname, "url": f"/api/outputs/{fname}"})

    items.sort(key=lambda x: x["no"])
    errors.sort(key=lambda x: x["no"])
    return JSONResponse({"ok": len(errors) == 0, "items": items, "errors": errors})


@app.get("/api/outputs/{filename}")
def download_output(filename: str) -> FileResponse:
    safe = Path(filename).name
    if not safe.endswith(".mp4") or not safe.startswith("reel_"):
        raise HTTPException(status_code=404)
    path = OUTPUT_DIR / safe
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, filename=safe, media_type="video/mp4")
