#!/usr/bin/env python3
"""CLI: Excel rows → 9:16 MP4 (1080×1920, 60fps, H.264)."""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from reel_core import ffmpeg_binary, normalize_text_bg, render_reel_mp4


def _pick_first_nonempty(row: pd.Series, names: list[str]) -> str | None:
    lower = {str(c).strip().lower(): c for c in row.index}
    for n in names:
        key = n.lower()
        if key in lower:
            val = row[lower[key]]
            if pd.notna(val) and str(val).strip():
                return str(val).strip()
    return None


def _resolve_image_path(raw: str, excel_dir: Path, cwd: Path) -> Path:
    p = Path(raw.strip())
    if p.is_absolute():
        return p
    for base in (cwd, excel_dir):
        cand = (base / p).resolve()
        if cand.is_file():
            return cand
    return (cwd / p).resolve()


def build_one_job(
    idx: int,
    row: pd.Series,
    excel_dir: Path,
    cwd: Path,
    out_dir: Path,
    font_path: Path | None,
    default_duration: float,
    text_bg: str,
) -> tuple[int, str]:
    img_raw = _pick_first_nonempty(row, ["image", "image_path", "filepath", "photo", "path"])
    if not img_raw:
        return idx, "skip: no image column"

    text = _pick_first_nonempty(row, ["text", "caption", "overlay", "title", "script"]) or ""

    dur_raw = _pick_first_nonempty(row, ["duration", "duration_sec", "seconds", "length"])
    duration = default_duration
    if dur_raw:
        try:
            duration = float(dur_raw)
        except ValueError:
            pass

    no_raw = _pick_first_nonempty(row, ["no", "no.", "number", "stt"])
    name_raw = _pick_first_nonempty(row, ["output", "output_filename", "filename", "file"])
    if name_raw:
        stem = Path(name_raw).stem
        out_name = f"{stem}.mp4"
    elif no_raw:
        try:
            n = int(float(no_raw.replace(",", ".")))
            out_name = f"reel_{n:04d}.mp4"
        except ValueError:
            out_name = f"reel_{idx + 1:04d}.mp4"
    else:
        out_name = f"reel_{idx + 1:04d}.mp4"

    img_path = _resolve_image_path(img_raw, excel_dir, cwd)
    if not img_path.is_file():
        return idx, f"error: missing image {img_path}"

    try:
        render_reel_mp4(
            img_path,
            text,
            out_dir / out_name,
            duration=duration,
            font_path=font_path,
            text_bg=text_bg,
        )
        return idx, f"ok: {out_name}"
    except Exception as e:  # noqa: BLE001
        return idx, f"error: {e}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Excel → batch Facebook Reels (9:16, 1080p, 60fps MP4)")
    parser.add_argument("--excel", required=True, type=Path, help="Path to .xlsx")
    parser.add_argument("--output-dir", type=Path, default=Path("reels_out"))
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 4))
    parser.add_argument("--duration", type=float, default=5.0, help="Default seconds per clip if column missing")
    parser.add_argument("--font", type=Path, default=None, help="TTF/TTC for captions")
    parser.add_argument(
        "--text-bg",
        choices=("black", "white"),
        default="black",
        help="Caption line background: black (white text) or white (black text)",
    )
    parser.add_argument("--sheet", type=str, default=0, help="Sheet name or index")
    args = parser.parse_args()

    try:
        ffmpeg_binary()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    excel_path = args.excel.resolve()
    if not excel_path.is_file():
        print(f"Excel not found: {excel_path}", file=sys.stderr)
        sys.exit(1)

    excel_dir = excel_path.parent
    cwd = Path.cwd()

    df = pd.read_excel(excel_path, sheet_name=args.sheet, engine="openpyxl")
    df = df.dropna(how="all")
    if df.empty:
        print("No data rows in sheet.", file=sys.stderr)
        sys.exit(1)

    out_dir = args.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    workers = max(1, args.workers)
    text_bg = normalize_text_bg(args.text_bg)

    rows = list(df.iterrows())
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [
            ex.submit(
                build_one_job,
                i,
                row,
                excel_dir,
                cwd,
                out_dir,
                args.font.resolve() if args.font else None,
                args.duration,
                text_bg,
            )
            for i, (_, row) in enumerate(rows)
        ]
        msgs: list[str] = []
        for fut in as_completed(futs):
            idx, msg = fut.result()
            msgs.append(msg)
            print(f"row {idx + 1}: {msg}")

    errs = sum(1 for m in msgs if m.startswith("error"))
    if errs:
        sys.exit(1)


if __name__ == "__main__":
    main()
