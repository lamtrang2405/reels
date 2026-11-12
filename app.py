"""Web UI: library (+ Gemini AI images) + rows table → batch Reels MP4 and clean post images."""

from __future__ import annotations  # required for FastAPI on Python 3.9

import json
import os
import re
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from PIL import Image

import ai_image
from ai_image import AIImageError
from reel_core import (
    POST_SIZES,
    CAPTION_FONT_SIZE_PX,
    CAPTION_FRAME_PAD_H,
    CAPTION_FRAME_PAD_V,
    CAPTION_INNER_PAD_BOTTOM,
    CAPTION_INNER_PAD_TOP,
    CAPTION_LINE_PAD_H,
    LIBRARY_STORE_H,
    LIBRARY_STORE_W,
    OVERLAY_MAX_WIDTH_FRAC,
    OVERLAY_TEXT_GUIDELINE_CHARS,
    OVERLAY_TEXT_SAFETY_MAX_CHARS,
    aspect_slug,
    estimate_dense_caption_char_budget,
    ffmpeg_binary,
    fit_cover,
    load_image_from_bytes,
    normalize_to_916_rgb,
    normalize_text_bg,
    render_post_image,
    render_reel_mp4,
)

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
LIBRARY_DIR = DATA / "library"
MASTERS_DIR = DATA / "masters"
OUTPUT_DIR = DATA / "outputs"
INDEX_PATH = DATA / "library_index.json"

ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000
MASTER_MAX_SIDE = 2560
ASPECT_LABELS = {
    "9:16": "9:16 · Reels / TikTok / Story",
    "4:5": "4:5 · Facebook post",
    "1:1": "1:1 · Vuông",
}
OUTPUT_NAME_RE = re.compile(
    r"^(reel_\d{4,5}\.mp4|post_\d{4,5}_\d+x\d+\.jpg|batch_[0-9A-Za-z_-]+\.zip)$"
)
_MEDIA_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".zip": "application/zip"}

_index_lock = Lock()
# Gemini calls are network-bound; keep few in flight to respect rate limits.
_ai_workers = int(os.environ.get("REELS_AI_WORKERS") or 3)
# Each FFmpeg job needs ~300MB; Render free (512MB) can only afford one at a time.
_executor_workers = int(
    os.environ.get("REELS_WORKERS")
    or (1 if os.environ.get("RENDER") else min(4, os.cpu_count() or 2))
)

app = FastAPI(title="Reels Batch")

_cors_origins = [
    "https://lamtrang2405.github.io",
    "http://127.0.0.1:8000",
    "http://localhost:8000",
]
_extra = os.environ.get("CORS_ORIGINS", "").strip()
if _extra:
    _cors_origins.extend(o.strip() for o in _extra.split(",") if o.strip())

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))

_LANDING_CTX = {"base": "/", "api_base": ""}


def _collect_upload_files(form) -> list[UploadFile]:
    """Gather UploadFile instances from multipart form (any field name)."""
    found: list[UploadFile] = []
    seen: set[int] = set()

    def add(uf) -> None:
        if uf is None:
            return
        oid = id(uf)
        if oid in seen:
            return
        if hasattr(uf, "read") and hasattr(uf, "filename"):
            seen.add(oid)
            found.append(uf)  # type: ignore[arg-type]

    for key in ("files", "file"):
        try:
            for entry in form.getlist(key):
                add(entry)
        except Exception:  # noqa: BLE001
            add(form.get(key))

    if not found:
        for entry in form.values():
            add(entry)
    return found


def _ensure_dirs() -> None:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    MASTERS_DIR.mkdir(parents=True, exist_ok=True)
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
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ImportError:
        pass
    try:
        ffmpeg_binary()
    except RuntimeError:
        pass


@app.get("/", response_class=HTMLResponse)
def landing_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "landing.html", {"request": request, **_LANDING_CTX}
    )


@app.get("/app", response_class=HTMLResponse)
def app_workspace(request: Request) -> HTMLResponse:
    """Direct link to studio (same UI as landing #studio)."""
    return templates.TemplateResponse(
        "landing.html", {"request": request, **_LANDING_CTX}
    )


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "reels-batch",
            "commit": os.environ.get("RENDER_GIT_COMMIT", "")[:7],
            "workers": _executor_workers,
        }
    )


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
            "ai_enabled": ai_image.ai_enabled(),
            "ai_image_model": ai_image.image_model(),
            "aspects": [
                {"id": a, "label": ASPECT_LABELS.get(a, a), "size": f"{w}×{h}"}
                for a, (w, h) in POST_SIZES.items()
            ],
            "text_bg_default": "black",
            "text_bg_options": [
                {"id": "black", "label": "Nền đen · chữ trắng"},
                {"id": "white", "label": "Nền trắng · chữ đen"},
            ],
            "text_note": (
                "Nội dung trong ô được giữ nguyên (xuống dòng và đoạn văn). "
                "Một dòng trống giữa các đoạn (nhấn Enter hai lần liên tiếp) = đoạn mới; "
                "một lần Enter = xuống dòng trong cùng đoạn. "
                f"Trên video: {CAPTION_FONT_SIZE_PX}px — mỗi dòng có nền riêng (đen hoặc trắng, chọn ở Studio), "
                f"căn trái; lề khung {CAPTION_FRAME_PAD_H}px ngang / {CAPTION_FRAME_PAD_V}px dọc; "
                f"padding trong nền đen {CAPTION_LINE_PAD_H}px ngang. "
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
        out.append(_entry_public(e))
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


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    msgs = []
    for err in exc.errors():
        loc = " → ".join(str(x) for x in err.get("loc", ()))
        msgs.append(f"{loc}: {err.get('msg', 'invalid')}")
    detail = "; ".join(msgs) if msgs else "Dữ liệu gửi lên không hợp lệ."
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "items": [],
            "errors": [_upload_error("", "invalid_request", detail)],
            "message": "Upload thất bại: kiểm tra đã chọn file ảnh (JPG/PNG/WEBP/GIF).",
        },
    )


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

    return _store_library_image(im, label, source="upload")


def _to_rgb(im: Image.Image) -> Image.Image:
    if im.mode == "RGB":
        return im
    rgba = im.convert("RGBA")
    bg = Image.new("RGB", rgba.size, (0, 0, 0))
    bg.paste(rgba, mask=rgba.split()[3])
    return bg


def _store_library_image(
    master: Image.Image,
    label: str,
    *,
    source: str,
    prompt: str | None = None,
    parent_id: str | None = None,
    variants: dict[str, Image.Image] | None = None,
) -> dict:
    """
    Save one library item: master (uncropped, capped size), 9:16 frame used by video,
    and optional native-aspect variants (AI output) used for post image export.
    """
    src_size = f"{master.width}×{master.height}"
    variants = variants or {}
    try:
        if "9:16" in variants:
            normalized = fit_cover(variants["9:16"], LIBRARY_STORE_W, LIBRARY_STORE_H)
            was_cropped = False
        else:
            normalized, was_cropped = normalize_to_916_rgb(master, LIBRARY_STORE_W, LIBRARY_STORE_H)
    except Exception as err:
        raise ValueError(f"Không xử lý được ảnh (scale/crop 9:16): {err}") from err

    image_id = uuid.uuid4().hex
    stored = f"{image_id}.jpg"
    _ensure_dirs()
    normalized.save(LIBRARY_DIR / stored, format="JPEG", quality=92, optimize=True)

    master_rgb = _to_rgb(master)
    if max(master_rgb.size) > MASTER_MAX_SIDE:
        master_rgb = master_rgb.copy()
        master_rgb.thumbnail((MASTER_MAX_SIDE, MASTER_MAX_SIDE), Image.Resampling.LANCZOS)
    master_name = f"{image_id}.jpg"
    master_rgb.save(MASTERS_DIR / master_name, format="JPEG", quality=92, optimize=True)

    variant_files: dict[str, str] = {}
    for aspect, vim in variants.items():
        if aspect not in POST_SIZES:
            continue
        vname = f"{image_id}_{aspect_slug(aspect)}.jpg"
        _to_rgb(vim).save(MASTERS_DIR / vname, format="JPEG", quality=92, optimize=True)
        variant_files[aspect] = vname

    item = {
        "id": image_id,
        "filename": stored,
        "original_name": label,
        "created": datetime.now(timezone.utc).isoformat(),
        "source_size": src_size,
        "stored_size": f"{LIBRARY_STORE_W}×{LIBRARY_STORE_H}",
        "normalized_916": was_cropped,
        "source": source,
        "master": master_name,
        "variants": variant_files,
    }
    if prompt:
        item["prompt"] = prompt
    if parent_id:
        item["parent_id"] = parent_id
    with _index_lock:
        entries = _load_index()
        entries.insert(0, item)
        _save_index(entries)
    return item


def _entry_public(e: dict) -> dict:
    fid = e["id"]
    return {
        "id": fid,
        "original_name": e.get("original_name", e.get("filename")),
        "url": f"/api/library/file/{fid}",
        "source": e.get("source", "upload"),
        "prompt": e.get("prompt", ""),
        "parent_id": e.get("parent_id", ""),
        "variants": sorted((e.get("variants") or {}).keys()),
        "source_size": e.get("source_size"),
        "stored_size": e.get("stored_size"),
        "normalized_916": e.get("normalized_916", False),
    }


def _entry_files(e: dict) -> list[Path]:
    files: list[Path] = []
    if e.get("filename"):
        files.append(LIBRARY_DIR / e["filename"])
    if e.get("master"):
        files.append(MASTERS_DIR / e["master"])
    for vname in (e.get("variants") or {}).values():
        files.append(MASTERS_DIR / vname)
    return files


def _source_for_aspect(e: dict, aspect: str) -> Path:
    """Native AI variant → uncropped master → 9:16 library frame."""
    vname = (e.get("variants") or {}).get(aspect)
    if vname and (MASTERS_DIR / vname).is_file():
        return MASTERS_DIR / vname
    if e.get("master") and (MASTERS_DIR / e["master"]).is_file():
        return MASTERS_DIR / e["master"]
    return LIBRARY_DIR / e["filename"]


def _best_source_bytes(e: dict) -> bytes:
    """Bytes of the highest-fidelity copy of an item (for AI regenerate input)."""
    for p in (
        MASTERS_DIR / e["master"] if e.get("master") else None,
        LIBRARY_DIR / e["filename"] if e.get("filename") else None,
    ):
        if p is not None and p.is_file():
            return p.read_bytes()
    raise FileNotFoundError("missing library file")


@app.post("/api/library/upload")
async def library_upload(request: Request) -> JSONResponse:
    try:
        form = await request.form()
    except Exception as err:  # noqa: BLE001
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "items": [],
                "errors": [
                    _upload_error(
                        "",
                        "bad_form",
                        f"Không đọc được form upload: {err}",
                    )
                ],
                "message": "Upload thất bại.",
            },
        )

    uploads = _collect_upload_files(form)
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
            items.append(_entry_public(item))
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

    for path in _entry_files(removed):
        path.unlink(missing_ok=True)

    return JSONResponse({"ok": True, "id": image_id})


class RowModel(BaseModel):
    no: int = Field(..., ge=1, le=99999)
    text: str = Field("", max_length=OVERLAY_TEXT_SAFETY_MAX_CHARS)
    image_id: str = ""


class GenerateBody(BaseModel):
    rows: list[RowModel]
    duration: float = Field(5.0, ge=0.5, le=600.0)
    text_bg: str = Field("black", description="black | white")


def _one_render(
    no: int,
    image_id: str,
    text: str,
    duration: float,
    text_bg: str,
) -> tuple[int, str | None, str | None]:
    entry = _find_entry(image_id)
    if not entry:
        return no, None, "unknown image_id"
    img_path = LIBRARY_DIR / entry["filename"]
    if not img_path.is_file():
        return no, None, "missing library file"
    name = f"reel_{no:04d}.mp4"
    out = OUTPUT_DIR / name
    try:
        render_reel_mp4(
            img_path, text, out, duration=duration, font_path=None, text_bg=text_bg
        )
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
    text_bg = normalize_text_bg(body.text_bg)
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
            ex.submit(_one_render, no, iid, txt, body.duration, text_bg): no
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


def _clean_aspects(values: list[str]) -> list[str]:
    """Valid, de-duplicated aspects; 9:16 first when present (it feeds the video frame)."""
    seen: list[str] = []
    for v in values or []:
        a = (v or "").strip()
        if a in POST_SIZES and a not in seen:
            seen.append(a)
    if not seen:
        seen = ["9:16"]
    if "9:16" in seen:
        seen.remove("9:16")
        seen.insert(0, "9:16")
    return seen


def _require_ai() -> None:
    if not ai_image.ai_enabled():
        raise HTTPException(
            status_code=503,
            detail="Chưa bật AI: đặt biến môi trường GEMINI_API_KEY trên server rồi khởi động lại.",
        )


def _build_variants(primary: Image.Image, aspects: list[str]) -> tuple[dict[str, Image.Image], list[str]]:
    """Primary image covers aspects[0]; other aspects are recomposed from it."""
    variants = {aspects[0]: primary}
    warnings: list[str] = []
    for aspect in aspects[1:]:
        try:
            variants[aspect] = ai_image.recompose_to_aspect(primary, aspect)
        except AIImageError as e:
            warnings.append(f"{aspect}: {e} (sẽ crop từ ảnh chính khi xuất)")
    return variants, warnings


class RegenerateBody(BaseModel):
    image_id: str
    instruction: str = Field("", max_length=2000)
    aspects: list[str] = Field(default_factory=lambda: ["9:16"])


@app.post("/api/ai/regenerate")
def ai_regenerate(body: RegenerateBody) -> JSONResponse:
    _require_ai()
    entry = _find_entry(body.image_id.strip())
    if not entry:
        raise HTTPException(status_code=404, detail="Không tìm thấy ảnh trong thư viện.")
    aspects = _clean_aspects(body.aspects)
    try:
        src = _best_source_bytes(entry)
        primary = ai_image.regenerate_from_image(src, body.instruction, aspects[0])
        variants, warnings = _build_variants(primary, aspects)
        label = f"AI · {entry.get('original_name', entry['id'][:8])}"
        item = _store_library_image(
            primary,
            label,
            source="ai_regen",
            prompt=body.instruction.strip() or None,
            parent_id=entry["id"],
            variants=variants,
        )
    except AIImageError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return JSONResponse({"ok": True, "item": _entry_public(item), "warnings": warnings})


class SummaryBody(BaseModel):
    summary: str = Field("", max_length=8000)
    style: str = Field("", max_length=500)
    prompt: str = Field("", max_length=4000, description="Optional: reuse/edited prompt, skips summary step")
    aspects: list[str] = Field(default_factory=lambda: ["9:16"])
    count: int = Field(1, ge=1, le=4)
    label: str = Field("", max_length=120)


def _one_summary_image(prompt: str, aspects: list[str], label: str) -> tuple[dict | None, list[str], str | None]:
    try:
        primary = ai_image.generate_from_prompt(prompt, aspects[0])
        variants, warnings = _build_variants(primary, aspects)
        item = _store_library_image(
            primary, label, source="ai_summary", prompt=prompt, variants=variants
        )
        return _entry_public(item), warnings, None
    except (AIImageError, ValueError) as e:
        return None, [], str(e)


@app.post("/api/ai/from-summary")
def ai_from_summary(body: SummaryBody) -> JSONResponse:
    _require_ai()
    aspects = _clean_aspects(body.aspects)
    try:
        prompt = body.prompt.strip() or ai_image.summary_to_prompt(body.summary, body.style)
    except AIImageError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    label = body.label.strip() or "AI · tóm tắt"
    items: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, min(_ai_workers, body.count))) as ex:
        futs = [ex.submit(_one_summary_image, prompt, aspects, label) for _ in range(body.count)]
        for fut in as_completed(futs):
            item, warns, err = fut.result()
            if item:
                items.append(item)
            warnings.extend(warns)
            if err:
                errors.append(err)

    if not items:
        raise HTTPException(status_code=502, detail="; ".join(errors) or "Không tạo được ảnh.")
    return JSONResponse(
        {"ok": not errors, "prompt": prompt, "items": items, "warnings": warnings, "errors": errors}
    )


class ExportRow(BaseModel):
    no: int = Field(..., ge=1, le=99999)
    image_id: str = ""


class ExportImagesBody(BaseModel):
    rows: list[ExportRow]
    aspects: list[str] = Field(default_factory=lambda: list(POST_SIZES.keys()))


def _one_post_export(no: int, image_id: str, aspect: str) -> tuple[int, str, str | None, str | None]:
    entry = _find_entry(image_id)
    if not entry:
        return no, aspect, None, "unknown image_id"
    name = f"post_{no:04d}_{aspect_slug(aspect)}.jpg"
    try:
        render_post_image(_source_for_aspect(entry, aspect), OUTPUT_DIR / name, aspect)
        return no, aspect, name, None
    except Exception as e:  # noqa: BLE001
        return no, aspect, None, str(e)


@app.post("/api/export/images")
def export_images(body: ExportImagesBody) -> JSONResponse:
    aspects = _clean_aspects(body.aspects)
    jobs: list[tuple[int, str, str]] = []
    errors: list[dict] = []
    for r in sorted(body.rows, key=lambda r: r.no):
        if not r.image_id.strip():
            errors.append({"no": r.no, "error": "missing image"})
            continue
        for a in aspects:
            jobs.append((r.no, r.image_id.strip(), a))
    if not jobs:
        return JSONResponse({"ok": False, "items": [], "errors": errors})

    _ensure_dirs()
    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, _executor_workers)) as ex:
        futs = [ex.submit(_one_post_export, no, iid, a) for no, iid, a in jobs]
        for fut in as_completed(futs):
            no, aspect, fname, err = fut.result()
            if err:
                errors.append({"no": no, "aspect": aspect, "error": err})
            elif fname:
                items.append(
                    {"no": no, "aspect": aspect, "filename": fname, "url": f"/api/outputs/{fname}"}
                )

    order = {a: i for i, a in enumerate(aspects)}
    items.sort(key=lambda x: (x["no"], order.get(x["aspect"], 99)))
    errors.sort(key=lambda x: x["no"])
    return JSONResponse({"ok": not errors, "items": items, "errors": errors})


class ZipBody(BaseModel):
    filenames: list[str] = Field(..., min_length=1, max_length=2000)


@app.post("/api/export/zip")
def export_zip(body: ZipBody) -> JSONResponse:
    files: list[Path] = []
    missing: list[str] = []
    for raw in body.filenames:
        safe = Path(raw).name
        path = OUTPUT_DIR / safe
        if not OUTPUT_NAME_RE.match(safe) or safe.startswith("batch_") or not path.is_file():
            missing.append(safe)
            continue
        if path not in files:
            files.append(path)
    if not files:
        raise HTTPException(status_code=404, detail="Không có file nào để nén (có thể server đã restart).")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    name = f"batch_{stamp}_{uuid.uuid4().hex[:6]}.zip"
    with zipfile.ZipFile(OUTPUT_DIR / name, "w", compression=zipfile.ZIP_STORED) as zf:
        for p in files:
            zf.write(p, arcname=p.name)
    return JSONResponse(
        {
            "ok": not missing,
            "filename": name,
            "url": f"/api/outputs/{name}",
            "count": len(files),
            "missing": missing,
        }
    )


@app.get("/api/outputs/{filename}")
def download_output(filename: str) -> FileResponse:
    safe = Path(filename).name
    if not OUTPUT_NAME_RE.match(safe):
        raise HTTPException(status_code=404)
    path = OUTPUT_DIR / safe
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, filename=safe, media_type=_MEDIA_TYPES.get(path.suffix, "application/octet-stream"))
