"""Tools for the content agent: wrap the Reels Studio endpoints (same library + outputs)."""

import json
import re
from typing import Any, Callable, Dict, List

from fastapi import HTTPException

from agents.runtime import add_artifact


def _reels():
    import app as reels  # the FastAPI module, already loaded by the server

    return reels


def _call(endpoint: Callable, body: Any) -> Dict[str, Any]:
    try:
        resp = endpoint(body)
    except HTTPException as e:
        return {"ok": False, "error": str(e.detail)}
    return json.loads(resp.body)


def _aspects(value: str) -> List[str]:
    parts = [p.strip().replace("x", ":") for p in re.split(r"[,\s;]+", value or "") if p.strip()]
    return parts or ["9:16"]


def _image_artifacts(items: List[dict]) -> None:
    for it in items:
        add_artifact(
            "image",
            it["url"],
            it.get("original_name", ""),
            image_id=it["id"],
            prompt=it.get("prompt", ""),
        )


def _next_reel_no() -> int:
    reels = _reels()
    used = [
        int(m.group(1))
        for p in reels.OUTPUT_DIR.glob("reel_*.mp4")
        if (m := re.match(r"reel_(\d+)\.mp4$", p.name))
    ]
    return max(used, default=0) + 1


def list_library_images(limit: int = 12) -> Dict[str, Any]:
    """Liệt kê ảnh mới nhất trong thư viện Reels Studio (ảnh import và ảnh AI).

    Args:
        limit: số ảnh tối đa trả về (mặc định 12).
    """
    reels = _reels()
    all_items = json.loads(reels.list_library().body)
    items = all_items[: max(1, min(int(limit), 50))]
    return {
        "total_in_library": len(all_items),
        "returned": len(items),
        "images": [
            {
                "image_id": it["id"],
                "name": it.get("original_name", ""),
                "source": it.get("source", "upload"),
                "ai_aspects": it.get("variants", []),
                "prompt": (it.get("prompt") or "")[:160],
            }
            for it in items
        ],
    }


def generate_images_from_summary(
    summary: str, style: str = "", aspects: str = "9:16,4:5", count: int = 1
) -> Dict[str, Any]:
    """Gemini đọc nội dung tóm tắt, viết prompt và tạo ảnh sạch (không chữ), lưu vào thư viện.

    Args:
        summary: nội dung tóm tắt của bài đăng / reel.
        style: phong cách ảnh mong muốn, ví dụ "ảnh chụp thật, tông ấm" (có thể để trống).
        aspects: các tỉ lệ cần tạo, cách nhau dấu phẩy: 9:16 (Reels/TikTok), 4:5 (Facebook post), 1:1.
        count: số ảnh khác nhau cần tạo (1-4).
    """
    reels = _reels()
    body = reels.SummaryBody(
        summary=summary,
        style=style,
        aspects=_aspects(aspects),
        count=max(1, min(int(count), 4)),
        label=f"AI · {summary.strip()[:40]}",
    )
    data = _call(reels.ai_from_summary, body)
    if data.get("items"):
        _image_artifacts(data["items"])
    return {
        "ok": bool(data.get("items")),
        "error": data.get("error", ""),
        "prompt": data.get("prompt", ""),
        "image_ids": [it["id"] for it in data.get("items", [])],
        "warnings": data.get("warnings", []) + data.get("errors", []),
    }


def regenerate_image(image_id: str, instruction: str = "", aspects: str = "9:16,4:5") -> Dict[str, Any]:
    """Gemini tạo lại một ảnh trong thư viện thành ảnh mới đẹp hơn (giữ chủ thể, bỏ chữ/watermark).

    Args:
        image_id: id ảnh nguồn trong thư viện.
        instruction: hướng dẫn thêm cho AI, ví dụ "nền sáng hơn, phong cách điện ảnh".
        aspects: các tỉ lệ cần tạo, cách nhau dấu phẩy (9:16, 4:5, 1:1).
    """
    reels = _reels()
    body = reels.RegenerateBody(image_id=image_id, instruction=instruction, aspects=_aspects(aspects))
    data = _call(reels.ai_regenerate, body)
    if data.get("item"):
        _image_artifacts([data["item"]])
    return {
        "ok": bool(data.get("item")),
        "error": data.get("error", ""),
        "image_id": (data.get("item") or {}).get("id", ""),
        "warnings": data.get("warnings", []),
    }


def create_reel(text: str, image_id: str, number: int = 0, duration: float = 5.0, text_bg: str = "black") -> Dict[str, Any]:
    """Tạo một video Reels MP4 9:16 (1080x1920, 60fps): ảnh nền + chữ overlay.

    Args:
        text: chữ hiển thị trên video (thường là nội dung tóm tắt). Enter 2 lần = đoạn mới.
        image_id: id ảnh nền trong thư viện.
        number: số thứ tự file (reel_000N.mp4); 0 = tự lấy số tiếp theo.
        duration: thời lượng video (giây).
        text_bg: nền chữ: "black" (chữ trắng) hoặc "white" (chữ đen).
    """
    reels = _reels()
    no = int(number) if int(number) > 0 else _next_reel_no()
    body = reels.GenerateBody(
        rows=[reels.RowModel(no=no, text=text, image_id=image_id)],
        duration=float(duration),
        text_bg=text_bg,
    )
    data = _call(reels.generate, body)
    for it in data.get("items", []):
        add_artifact("video", it["url"], it["filename"], filename=it["filename"])
    return {
        "ok": bool(data.get("items")),
        "files": [it["filename"] for it in data.get("items", [])],
        "errors": data.get("errors", []) or ([data["error"]] if data.get("error") else []),
    }


def export_post_images(image_id: str, number: int = 1, aspects: str = "9:16,4:5,1:1") -> Dict[str, Any]:
    """Xuất ảnh JPG sạch (không chữ) để đăng post Facebook/TikTok theo các tỉ lệ.

    Args:
        image_id: id ảnh trong thư viện.
        number: số thứ tự dùng trong tên file post_000N_<tỉ lệ>.jpg.
        aspects: các tỉ lệ, cách nhau dấu phẩy (9:16, 4:5, 1:1).
    """
    reels = _reels()
    body = reels.ExportImagesBody(
        rows=[reels.ExportRow(no=max(1, int(number)), image_id=image_id)],
        aspects=_aspects(aspects),
    )
    data = _call(reels.export_images, body)
    for it in data.get("items", []):
        add_artifact("image", it["url"], it["filename"], filename=it["filename"], aspect=it["aspect"])
    return {
        "ok": bool(data.get("items")),
        "files": [it["filename"] for it in data.get("items", [])],
        "errors": data.get("errors", []),
    }


def zip_outputs(filenames: str) -> Dict[str, Any]:
    """Nén các file đã tạo (video reel_*.mp4, ảnh post_*.jpg) thành một file ZIP để tải về.

    Args:
        filenames: tên các file cần nén, cách nhau dấu phẩy.
    """
    reels = _reels()
    names = [n.strip() for n in re.split(r"[,\s]+", filenames or "") if n.strip()]
    if not names:
        return {"ok": False, "error": "Chưa có tên file."}
    data = _call(reels.export_zip, reels.ZipBody(filenames=names))
    if data.get("url"):
        add_artifact("file", data["url"], data["filename"], filename=data["filename"])
    return {
        "ok": bool(data.get("url")),
        "zip": data.get("filename", ""),
        "count": data.get("count", 0),
        "missing": data.get("missing", []),
        "error": data.get("error", ""),
    }


CONTENT_TOOLS = [
    list_library_images,
    generate_images_from_summary,
    regenerate_image,
    create_reel,
    export_post_images,
    zip_outputs,
]
