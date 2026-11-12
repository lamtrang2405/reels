"""Crawl Facebook story pages once (one metrics snapshot per post), optionally print the trending top.

Run every ~2 hours (cron / launchd) so the 24h growth and velocity can be measured:
    python scripts/fb_trending_crawl.py
    python scripts/fb_trending_crawl.py --top 10          # crawl, then print top 10
    python scripts/fb_trending_crawl.py --top-only        # only print the top from stored data
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from trending import store  # noqa: E402
from trending.crawler import crawl  # noqa: E402
from trending.scoring import top_trending  # noqa: E402


def _print_top(limit: int, hours: int, kind: str, use_ai: bool) -> None:
    conn = store.connect()
    try:
        res = top_trending(conn, hours=hours, limit=limit, kind=kind, use_ai=use_ai)
    finally:
        conn.close()
    last = datetime.fromtimestamp(res["last_crawl_at"]).strftime("%d/%m %H:%M") if res["last_crawl_at"] else "chưa có"
    print(f"\nTop {limit} post hot {hours}h — {res['candidates']} post, crawl gần nhất: {last}")
    for note in res["notes"]:
        print(f"  ! {note}")
    for i, it in enumerate(res["items"], 1):
        flags = " ↑đang lên" if it["rising"] else ""
        flags += f" ({it['same_story_pages']} page cùng đăng)" if it["same_story_pages"] > 1 else ""
        age = f"{it['age_hours']}h" if it["age_hours"] is not None else "?"
        print(f"{i:>2}. [{it['score']:>5}] {it['kind']:<4} {age:>6}  +{it['delta_window']} tương tác"
              f"  hiệu quả x{it['perf_ratio']} ({it['perf_label']})  {it['page']}{flags}")
        print(f"      {it.get('story_title') or it.get('hook') or ''}")
        print(f"      {it['url']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-pages", type=int, default=30, help="số page tối đa mỗi lần chạy")
    ap.add_argument("--max-posts", type=int, default=20, help="số post tối đa mỗi page")
    ap.add_argument("--no-search", action="store_true", help="bỏ qua tìm theo từ khoá")
    ap.add_argument("--headful", action="store_true", help="mở cửa sổ Chromium (debug)")
    ap.add_argument("--json", action="store_true", help="log ra stderr, kết quả JSON ở dòng cuối stdout")
    ap.add_argument("--top", type=int, default=0, help="in top N sau khi crawl")
    ap.add_argument("--top-only", action="store_true", help="không crawl, chỉ in top từ dữ liệu đã lưu")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--kind", choices=("all", "post", "reel"), default="all")
    ap.add_argument("--no-ai", action="store_true", help="không dùng Gemini để đoán tên truyện")
    args = ap.parse_args()

    if args.top_only:
        _print_top(args.top or 10, args.hours, args.kind, not args.no_ai)
        return 0

    def log(msg: str) -> None:
        print(msg, file=sys.stderr if args.json else sys.stdout, flush=True)

    try:
        result = crawl(max_pages=args.max_pages, max_posts_per_page=args.max_posts, search=not args.no_search,
                       headless=False if args.headful else None, log=log)
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": str(e)}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        if args.top and result.get("posts") is not None:
            _print_top(args.top, args.hours, args.kind, not args.no_ai)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
