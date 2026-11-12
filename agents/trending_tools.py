"""Tools for the research agent: Facebook trending posts / reels about stories (last N hours)."""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from agents.cms_tools import cms_status, list_stories
from agents.runtime import add_artifact
from trending import store
from trending.scoring import top_trending

ROOT = Path(__file__).resolve().parent.parent
CRAWL_SCRIPT = ROOT / "scripts" / "fb_trending_crawl.py"
CRAWL_TIMEOUT_S = 40 * 60


def _when(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else None


def _cms_match(title: str) -> Optional[Dict[str, Any]]:
    words = title.split()
    for query in dict.fromkeys([title, " ".join(words[:4])]):
        if len(query) < 4:
            continue
        found = list_stories(search=query, limit=3)
        stories = found.get("stories") or []
        if stories:
            s = stories[0]
            return {"title": s["title"], "slug": s["slug"], "id": s["id"]}
    return None


def fb_trending_top(hours: int = 24, limit: int = 10, kind: str = "all") -> Dict[str, Any]:
    """Top post / reels hot nhất của các page Facebook đang theo dõi trong N giờ qua (từ dữ liệu crawl đã lưu).

    Điểm = tương tác tăng thêm trong N giờ + tốc độ tăng gần nhất + mức vượt trội so với mặt bằng của chính page.
    Tương tác = cảm xúc + 3×bình luận + 5×chia sẻ + 0.02×lượt xem. perf_label / perf_ratio = hiệu quả so với
    post điển hình của page (1.0 = bằng trung vị).

    Args:
        hours: khung thời gian tính tăng trưởng (mặc định 24).
        limit: số bài trả về (mặc định 10).
        kind: "all", "post" hoặc "reel".
    """
    kind = kind if kind in ("all", "post", "reel") else "all"
    conn = store.connect()
    try:
        result = top_trending(conn, hours=max(1, int(hours)), limit=max(1, min(int(limit), 30)), kind=kind)
    finally:
        conn.close()

    cms_ok = cms_status().get("ok")
    for rank, item in enumerate(result["items"], 1):
        item["rank"] = rank
        if cms_ok and item.get("story_title"):
            item["cms_story"] = _cms_match(item["story_title"])
        label = f"#{rank} {item.get('page') or ''} · {item.get('story_title') or item.get('hook') or ''}"[:120]
        if item.get("url"):
            add_artifact("link", item["url"], label)

    result["generated_at"] = _when(result["generated_at"])
    result["last_crawl_at"] = _when(result["last_crawl_at"])
    return result


def fb_crawl_now(max_pages: int = 30) -> Dict[str, Any]:
    """Crawl ngay các page truyện trong pages.json (Playwright) để cập nhật số liệu.

    Không có cookie Facebook thì chạy chế độ khách: ~6 post / reels mới nhất trên feed mỗi page,
    không đọc tab Reels, không tìm theo từ khoá.

    Chạy lâu (vài phút đến ~30 phút). Thường đã có lịch crawl tự động mỗi 2 giờ — chỉ gọi khi người dùng yêu cầu
    hoặc khi lần crawl gần nhất đã quá cũ.

    Args:
        max_pages: số page tối đa crawl trong lần này (1-60).
    """
    cmd = [sys.executable, str(CRAWL_SCRIPT), "--json", "--max-pages", str(max(1, min(int(max_pages), 60)))]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=CRAWL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Crawl quá {CRAWL_TIMEOUT_S // 60} phút, đã dừng."}
    lines = [l for l in proc.stdout.strip().splitlines() if l.strip()]
    try:
        return json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        return {"ok": False, "error": (proc.stderr or proc.stdout or "Crawl lỗi không rõ").strip()[-800:]}


TRENDING_TOOLS = [fb_trending_top, fb_crawl_now]
