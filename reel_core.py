"""Shared Reels encoding: 9:16 (1080×1920), 60fps, H.264 MP4."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    import imageio_ffmpeg

    _FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # noqa: BLE001
    _FFMPEG_EXE = None

TARGET_W = 1080
TARGET_H = 1920
FPS = 60
CRF = 23

# Rare safety ceiling (anti-abuse / memory); normal paste is never cut before this.
OVERLAY_TEXT_SAFETY_MAX_CHARS = 50000
CAPTION_MIN_FONT_SIZE = 12
# Caption font size on 1080×1920 frame (preset dễ đọc).
CAPTION_FONT_SIZE_PX = 38
# Inset from video frame edges to caption block (tránh sát lề màn hình).
CAPTION_FRAME_PAD_H = 52
CAPTION_FRAME_PAD_V = 96
# Legacy aliases used in height budget / API copy.
CAPTION_INNER_PAD_TOP = CAPTION_FRAME_PAD_V
CAPTION_INNER_PAD_BOTTOM = CAPTION_FRAME_PAD_V
# Max fraction of frame width (fallback; wrapping uses pixel pads below).
OVERLAY_MAX_WIDTH_FRAC = 0.88
# Extra vertical space between paragraphs (after \\n\\n).
CAPTION_PARAGRAPH_GAP_PX = 24
# Padding inside each line's black highlight (chữ ↔ viền nền đen).
CAPTION_LINE_PAD_H = 22
CAPTION_LINE_PAD_V = 16
CAPTION_LINE_BLOCK_GAP = 10
CAPTION_LINE_SHADOW_OFFSET = (2, 2)
CAPTION_LINE_SHADOW_ALPHA = 140

TEXT_BG_BLACK = "black"
TEXT_BG_WHITE = "white"
TEXT_BG_CHOICES = (TEXT_BG_BLACK, TEXT_BG_WHITE)

# (fill RGBA, text RGBA, shadow RGBA)
_TEXT_BG_STYLES: dict[str, tuple[tuple[int, int, int, int], tuple[int, int, int, int], tuple[int, int, int, int]]] = {
    TEXT_BG_BLACK: ((0, 0, 0, 255), (255, 255, 255, 255), (0, 0, 0, 140)),
    TEXT_BG_WHITE: ((255, 255, 255, 242), (0, 0, 0, 255), (0, 0, 0, 70)),
}


def normalize_text_bg(value: str | None) -> str:
    v = (value or TEXT_BG_BLACK).strip().lower()
    if v in ("white", "trang", "light", "sang"):
        return TEXT_BG_WHITE
    return TEXT_BG_BLACK


def estimate_dense_caption_char_budget(
    reference_font_px: int | None = None,
    width_frac: float | None = None,
) -> int:
    """
    Rough character budget if text used most of the vertical band between inner margins,
    at the caption font size (renderer uses fixed pixel size). UI guidance only.
    """
    ref = reference_font_px if reference_font_px is not None else CAPTION_FONT_SIZE_PX
    wf = width_frac if width_frac is not None else OVERLAY_MAX_WIDTH_FRAC
    max_line_px = min(
        int(TARGET_W * wf),
        TARGET_W - 2 * CAPTION_FRAME_PAD_H - 2 * CAPTION_LINE_PAD_H,
    )
    inner_h = TARGET_H - 2 * CAPTION_FRAME_PAD_V
    line_h = max(ref + 2 * CAPTION_LINE_PAD_V + CAPTION_LINE_BLOCK_GAP, 12)
    lines = max(1, inner_h // line_h)
    avg_char_px = ref * 0.53
    cpl = max(10, int(max_line_px / avg_char_px))
    return lines * cpl


# UI guideline ≈ dense caption at caption font size.
OVERLAY_TEXT_GUIDELINE_CHARS = estimate_dense_caption_char_budget()


def ffmpeg_binary() -> str:
    if _FFMPEG_EXE:
        return _FFMPEG_EXE
    w = shutil.which("ffmpeg")
    if not w:
        raise RuntimeError(
            "ffmpeg not found. Install ffmpeg or pip install imageio-ffmpeg."
        )
    return w


def _load_font(size: int, font_path: Path | None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    paths_try = []
    if font_path and font_path.is_file():
        paths_try.append(font_path)
    if sys.platform == "darwin":
        paths_try.extend(
            [
                Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
                Path("/System/Library/Fonts/PingFang.ttc"),
                Path("/Library/Fonts/Arial Unicode.ttf"),
            ]
        )
    paths_try.append(Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"))
    for fp in paths_try:
        if fp.is_file():
            try:
                return ImageFont.truetype(str(fp), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def truncate_overlay_text(text: str, max_chars: int = OVERLAY_TEXT_SAFETY_MAX_CHARS) -> str:
    """Only trims beyond SAFETY_MAX (server protection). Does not shorten normal captions."""
    t = text.strip()
    if len(t) <= max_chars:
        return t
    if max_chars <= 1:
        return "…"
    return t[: max_chars - 1].rstrip() + "…"


REEL_ASPECT = 9 / 16
LIBRARY_STORE_W = TARGET_W
LIBRARY_STORE_H = TARGET_H


def aspect_ratio_is_916(img: Image.Image, tolerance: float = 0.02) -> bool:
    if img.height <= 0:
        return False
    ratio = img.width / img.height
    return abs(ratio - REEL_ASPECT) / REEL_ASPECT <= tolerance


# Clean post images (no caption overlay): aspect id → output size.
POST_SIZES: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "4:5": (1080, 1350),
    "1:1": (1080, 1080),
}
POST_JPEG_QUALITY = 92


def aspect_slug(aspect: str) -> str:
    return aspect.replace(":", "x")


def aspect_matches(img: Image.Image, aspect: str, tolerance: float = 0.02) -> bool:
    w, h = POST_SIZES[aspect]
    if img.height <= 0:
        return False
    target = w / h
    return abs(img.width / img.height - target) / target <= tolerance


def fit_cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale to cover w×h, center-crop — for any source aspect ratio (returns RGB)."""
    img = img.convert("RGBA")
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    nw, nh = int(round(src_w * scale)), int(round(src_h * scale))
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    framed = img.crop((left, top, left + w, top + h))
    bg = Image.new("RGB", framed.size, (0, 0, 0))
    bg.paste(framed, mask=framed.split()[3])
    return bg


def render_post_image(src_path: Path, out_path: Path, aspect: str) -> None:
    """Export a clean JPEG post image at the given aspect (9:16, 4:5, 1:1)."""
    if aspect not in POST_SIZES:
        raise ValueError(f"unsupported aspect {aspect}")
    if not src_path.is_file():
        raise FileNotFoundError(str(src_path))
    w, h = POST_SIZES[aspect]
    with Image.open(src_path) as im:
        frame = fit_cover(im, w, h)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frame.save(out_path, format="JPEG", quality=POST_JPEG_QUALITY, optimize=True)


def fit_cover_916(img: Image.Image, w: int = TARGET_W, h: int = TARGET_H) -> Image.Image:
    """Scale to cover w×h (9:16), center-crop — for any source aspect ratio."""
    img = img.convert("RGBA")
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    nw, nh = int(round(src_w * scale)), int(round(src_h * scale))
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def normalize_to_916_rgb(
    img: Image.Image,
    w: int = LIBRARY_STORE_W,
    h: int = LIBRARY_STORE_H,
) -> tuple[Image.Image, bool]:
    """Return 9:16 RGB image; bool = source was not already ~9:16 (scale+crop applied)."""
    adjusted = not aspect_ratio_is_916(img)
    framed = fit_cover_916(img, w, h)
    if framed.mode == "RGBA":
        bg = Image.new("RGB", framed.size, (0, 0, 0))
        bg.paste(framed, mask=framed.split()[3])
        return bg, adjusted
    return framed.convert("RGB"), adjusted


def load_image_from_bytes(body: bytes) -> Image.Image:
    if not body:
        raise ValueError("Tệp rỗng (0 byte).")
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ImportError:
        pass
    try:
        im = Image.open(BytesIO(body))
        im.load()
    except Exception as err:
        hint = ""
        if body[:12].find(b"ftypheic") >= 0 or body[:4] == b"\x00\x00\x00":
            hint = " (HEIC: cài pillow-heif hoặc xuất JPG/PNG trước khi tải.)"
        raise ValueError(f"Không đọc được ảnh: {err}{hint}") from err
    try:
        from PIL.ImageOps import exif_transpose

        im = exif_transpose(im)
    except Exception:
        pass
    return im


def _line_gap_px(font_size: int) -> int:
    return max(2, int(font_size * 0.2))


def _wrap_long_unbroken(draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont, piece: str, max_width: int) -> list[str]:
    """Break a string with no spaces into lines that fit max_width (CJK / long tokens)."""
    lines: list[str] = []
    rest = piece
    while rest:
        if draw.textlength(rest, font=font) <= max_width:
            lines.append(rest)
            break
        low, high = 0, len(rest)
        while low < high:
            mid = (low + high + 1) // 2
            if draw.textlength(rest[:mid], font=font) <= max_width:
                low = mid
            else:
                high = mid - 1
        if low == 0:
            lines.append("…")
            rest = rest[1:] if len(rest) > 1 else ""
            continue
        lines.append(rest[:low])
        rest = rest[low:].lstrip()
    return lines


def _wrap_paragraph_segment(
    draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont, segment: str, max_width: int
) -> list[str]:
    segment = segment.strip()
    if not segment:
        return []
    if draw.textlength(segment, font=font) <= max_width:
        return [segment]
    words = segment.split()
    if len(words) == 1:
        return _wrap_long_unbroken(draw, font, words[0], max_width)
    out: list[str] = []
    cur: list[str] = []
    for w in words:
        trial = " ".join(cur + [w]) if cur else w
        if draw.textlength(trial, font=font) <= max_width:
            cur.append(w)
            continue
        if cur:
            out.append(" ".join(cur))
        if draw.textlength(w, font=font) <= max_width:
            cur = [w]
        else:
            out.extend(_wrap_long_unbroken(draw, font, w, max_width))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def _build_caption_layout(
    draw: ImageDraw.ImageDraw,
    font: ImageFont.ImageFont,
    text: str,
    max_width: int,
) -> list[str | None]:
    """
    Preserve structure: blank line between paragraphs (\\n\\n), single \\n = line break before wrap.
    None entries add CAPTION_PARAGRAPH_GAP_PX between paragraphs.
    """
    raw = text.replace("\r\n", "\n")
    paragraphs = raw.split("\n\n")
    layout: list[str | None] = []
    for pi, para in enumerate(paragraphs):
        if pi > 0:
            layout.append(None)
        sublines = para.split("\n")
        for si, sub in enumerate(sublines):
            if sub.strip() == "":
                if si > 0 or layout:
                    layout.append("")
                continue
            layout.extend(_wrap_paragraph_segment(draw, font, sub, max_width))
    return layout


def _line_bbox(font: ImageFont.ImageFont, line: str) -> tuple[int, int, int, int]:
    tmp = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    return tmp.textbbox((0, 0), line if line else " ", font=font)


def _line_block_height(bbox: tuple[int, int, int, int]) -> int:
    return (bbox[3] - bbox[1]) + 2 * CAPTION_LINE_PAD_V


def _layout_height(
    draw: ImageDraw.ImageDraw,
    font: ImageFont.ImageFont,
    layout: list[str | None],
    font_size: int,
) -> int:
    h = 0
    prev_was_text = False
    for item in layout:
        if item is None:
            h += CAPTION_PARAGRAPH_GAP_PX
            prev_was_text = False
            continue
        if item == "":
            h += _line_block_height(_line_bbox(font, " "))
            prev_was_text = True
            continue
        if prev_was_text:
            h += CAPTION_LINE_BLOCK_GAP
        bbox = _line_bbox(font, item)
        h += _line_block_height(bbox)
        prev_was_text = True
    return h


def _draw_line_highlight(
    draw: ImageDraw.ImageDraw,
    line: str,
    font: ImageFont.ImageFont,
    x: int,
    y: int,
    text_bg: str = TEXT_BG_BLACK,
) -> tuple[int, int, int, int]:
    """Draw one line with solid bg + soft shadow; returns pixel bbox used."""
    bg_fill, text_fill, shadow_fill = _TEXT_BG_STYLES[normalize_text_bg(text_bg)]
    bbox = draw.textbbox((x, y), line, font=font)
    x0 = bbox[0] - CAPTION_LINE_PAD_H
    y0 = bbox[1] - CAPTION_LINE_PAD_V
    x1 = bbox[2] + CAPTION_LINE_PAD_H
    y1 = bbox[3] + CAPTION_LINE_PAD_V
    sx, sy = CAPTION_LINE_SHADOW_OFFSET
    draw.rectangle(
        [x0 + sx, y0 + sy, x1 + sx, y1 + sy],
        fill=shadow_fill,
    )
    draw.rectangle([x0, y0, x1, y1], fill=bg_fill)
    draw.text((x, y), line, font=font, fill=text_fill)
    return (x0, y0, x1, y1)


def _ellipsis_fit_line(
    draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont, line: str, max_width: int
) -> str:
    if draw.textlength(line, font=font) <= max_width:
        return line
    suf = "…"
    low, high = 0, len(line)
    while low < high:
        mid = (low + high + 1) // 2
        trial = line[:mid].rstrip() + suf
        if draw.textlength(trial, font=font) <= max_width:
            low = mid
        else:
            high = mid - 1
    base = line[:low].rstrip()
    return (base + suf) if base else suf


def _trim_layout_to_height(
    draw: ImageDraw.ImageDraw,
    font: ImageFont.ImageFont,
    layout: list[str | None],
    max_inner_h: int,
    font_size: int,
    max_width: int,
) -> list[str | None]:
    """Drop lines from the end until height fits; ellipsize last kept text line if trimmed."""
    layout = list(layout)
    if not layout:
        return layout
    h0 = _layout_height(draw, font, layout, font_size)
    if h0 <= max_inner_h:
        return layout
    while layout and _layout_height(draw, font, layout, font_size) > max_inner_h:
        layout.pop()
    while layout and layout[-1] is None:
        layout.pop()
    for i in range(len(layout) - 1, -1, -1):
        if layout[i] is not None and layout[i] != "":
            layout[i] = _ellipsis_fit_line(draw, font, layout[i], max_width)
            break
    if not layout:
        return ["…"]
    return layout


def draw_text_overlay(
    base: Image.Image,
    text: str,
    font_path: Path | None,
    margin: int | None = None,
    max_width_frac: float | None = None,
    text_bg: str = TEXT_BG_BLACK,
) -> Image.Image:
    """Per-line highlights (black or white bg); fixed font size; left align; preserves paragraphs."""
    text_bg = normalize_text_bg(text_bg)
    text = truncate_overlay_text(text)
    if not text.strip():
        return base

    margin_top = margin_bottom = (0 if margin is None else margin)
    available_h = max(48, TARGET_H - margin_top - margin_bottom)
    content_h = max(48, available_h - 2 * CAPTION_FRAME_PAD_V)
    max_inner_text_h = content_h

    img = base.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    wf = OVERLAY_MAX_WIDTH_FRAC if max_width_frac is None else max_width_frac
    max_width = min(
        int(TARGET_W * wf),
        TARGET_W - 2 * CAPTION_FRAME_PAD_H - 2 * CAPTION_LINE_PAD_H,
    )
    max_width = max(120, max_width)
    font_size = CAPTION_FONT_SIZE_PX
    font = _load_font(font_size, font_path)

    layout = _build_caption_layout(draw, font, text, max_width)
    layout = _trim_layout_to_height(draw, font, layout, max_inner_text_h, font_size, max_width)

    th = _layout_height(draw, font, layout, font_size)
    column_top = margin_top + CAPTION_FRAME_PAD_V + max(0, (content_h - th) // 2)
    cy = column_top
    prev_was_text = False

    for item in layout:
        if item is None:
            cy += CAPTION_PARAGRAPH_GAP_PX
            prev_was_text = False
            continue

        line = item if item else " "
        bbox0 = _line_bbox(font, line)
        # Căn trái: mép trái khung an toàn + padding trong nền đen từng dòng.
        x = CAPTION_FRAME_PAD_H + CAPTION_LINE_PAD_H - bbox0[0]

        if prev_was_text and item:
            cy += CAPTION_LINE_BLOCK_GAP

        _draw_line_highlight(draw, line if item else " ", font, x, cy, text_bg=text_bg)
        cy += _line_block_height(bbox0)
        prev_was_text = bool(item)

    return Image.alpha_composite(img, overlay).convert("RGB")


def ffmpeg_image_to_mp4(png_path: Path, out_mp4: Path, duration: float) -> None:
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    # x264 "medium" with auto threads peaks ~700MB at 1080×1920; hosts like Render free cap at 512MB.
    threads = os.environ.get("REELS_FFMPEG_THREADS", "2")
    cmd = [
        ffmpeg_binary(),
        "-y",
        "-loop",
        "1",
        "-framerate",
        str(FPS),
        "-i",
        str(png_path),
        "-t",
        str(duration),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(FPS),
        "-crf",
        str(CRF),
        "-preset",
        "veryfast",
        "-tune",
        "stillimage",
        "-threads",
        threads,
        "-x264-params",
        "rc-lookahead=10:sync-lookahead=0",
        str(out_mp4),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-2000:]}")


def render_reel_mp4(
    image_path: Path,
    text: str,
    out_mp4: Path,
    duration: float = 5.0,
    font_path: Path | None = None,
    text_bg: str = TEXT_BG_BLACK,
) -> None:
    duration = max(0.5, min(float(duration), 600.0))
    if not image_path.is_file():
        raise FileNotFoundError(str(image_path))
    with Image.open(image_path) as im:
        frame = fit_cover_916(im)
    frame = draw_text_overlay(frame, text, font_path, text_bg=text_bg)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        frame.save(tmp_path, format="PNG")
        ffmpeg_image_to_mp4(tmp_path, out_mp4, duration)
    finally:
        tmp_path.unlink(missing_ok=True)
