"""Post performance vs. the page's own baseline, trending score (growth in the window), dedupe, top N.

Every post of a tracked page is content to evaluate (no topic filter).
"""

import json
import math
import re
import sqlite3
import statistics
import time
from bisect import bisect_right
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from trending import store

W_REACTION, W_COMMENT, W_SHARE, W_VIEW = 1.0, 3.0, 5.0, 0.02
W_DELTA, W_VELOCITY, W_OUTPERFORM = 0.40, 0.30, 0.30
DEFAULT_MAX_AGE_H = 72
BASELINE_DAYS = 14
BASELINE_SMOOTHING = 50.0
PERF_SMOOTHING = 20.0
MATURE_H = 48.0
RISING_ACCEL = 1.2
MAX_PER_PAGE = 2
DEDUPE_RATIO = 0.8
AI_BATCH = 25
PERF_LEVELS = ((2.0, "Rất tốt"), (1.0, "Tốt"), (0.5, "Trung bình"), (0.0, "Thấp"))

# ---------------------------------------------------------------- titles

_TITLE_RES = (
    re.compile(r"(?:tên truyện|truyện)\s*[:：\-–]\s*([^\n|#]{3,90})", re.I),
    re.compile(r"[【《\[「]([^】》\]」\n]{3,90})[】》\]」]"),
    re.compile(r"^\s*[\"“]([^\"”\n]{3,90})[\"”]"),
)


def guess_title(caption: str) -> Optional[str]:
    head = (caption or "")[:300]
    for rx in _TITLE_RES:
        m = rx.search(head)
        if m:
            title = m.group(1).strip(" .-–:")
            if len(title) >= 3:
                return title
    return None


def titles_with_ai(posts: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Batch-ask Gemini for the story title each caption is about (None when it is not a story post)."""
    import ai_image

    if not ai_image.ai_enabled() or not posts:
        return {}
    from google.genai import types

    out: Dict[str, Optional[str]] = {}
    for start in range(0, len(posts), AI_BATCH):
        batch = posts[start:start + AI_BATCH]
        listing = "\n\n".join(f"[{p['post_id']}]\n{(p.get('caption') or '')[:600]}" for p in batch)
        prompt = (
            "Với mỗi bài đăng Facebook tiếng Việt dưới đây, đoán tên truyện mà bài đang đăng / giới thiệu "
            "(null nếu không đoán được). Trả về JSON list: "
            '[{"id": "...", "title": "..." | null}]\n\n' + listing
        )
        try:
            response = ai_image.generate_with_fallback(
                ai_image.text_models(),
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            rows = json.loads(getattr(response, "text", "") or "[]")
        except Exception:  # noqa: BLE001
            continue
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("id") is not None:
                title = row.get("title")
                out[str(row["id"])] = title.strip() if isinstance(title, str) and title.strip() else None
    return out


def fill_titles_with_ai(conn: sqlite3.Connection, items: List[Dict[str, Any]], captions: Dict[str, str]) -> None:
    need = [{"post_id": i["post_id"], "caption": captions.get(i["post_id"], "")} for i in items
            if not i.get("story_title") and captions.get(i["post_id"], "").strip()]
    answers = titles_with_ai(need)
    for item in items:
        title = answers.get(item["post_id"])
        if title:
            item["story_title"] = title
            store.set_topic(conn, item["post_id"], True, title, "ai")
    conn.commit()


# ---------------------------------------------------------------- metrics

def engagement(s: Dict[str, Any]) -> float:
    return (W_REACTION * (s.get("reactions") or 0) + W_COMMENT * (s.get("comments") or 0)
            + W_SHARE * (s.get("shares") or 0) + W_VIEW * (s.get("views") or 0))


class Curve:
    """Engagement over time from snapshots; starts at 0 at creation time when it is known."""

    def __init__(self, created_at: Optional[float], snaps: List[Dict[str, Any]]):
        pts = [(s["ts"], engagement(s)) for s in snaps]
        self.snap_ts = [t for t, _ in pts]
        if created_at and (not pts or created_at < pts[0][0]):
            pts.insert(0, (created_at, 0.0))
        self.t = [t for t, _ in pts]
        self.e = [e for _, e in pts]

    def at(self, ts: float) -> float:
        if not self.t:
            return 0.0
        if ts <= self.t[0]:
            return self.e[0]
        if ts >= self.t[-1]:
            return self.e[-1]
        i = bisect_right(self.t, ts)
        t0, t1, e0, e1 = self.t[i - 1], self.t[i], self.e[i - 1], self.e[i]
        return e0 + (e1 - e0) * (ts - t0) / (t1 - t0)

    @property
    def now(self) -> float:
        return self.e[-1] if self.e else 0.0

    def rate(self, back: int) -> Optional[float]:
        """Engagement per hour over the back-th segment from the end (1 = latest)."""
        if len(self.t) < back + 1:
            return None
        t1, t0 = self.t[-back], self.t[-back - 1]
        e1, e0 = self.e[-back], self.e[-back - 1]
        return max(0.0, (e1 - e0) / max((t1 - t0) / 3600, 0.25))


def _page_key(p: Dict[str, Any]) -> str:
    return str(p.get("source") or p.get("page_id") or p.get("page_url") or p.get("page_name") or "?")


def _kind(p: Dict[str, Any]) -> str:
    return p.get("kind") or "post"


def _median_by(pairs: List[Tuple[str, float]]) -> Dict[str, float]:
    """Median per key; keys with fewer than 3 values are dropped except the global "*|kind" ones."""
    groups: Dict[str, List[float]] = {}
    for k, v in pairs:
        groups.setdefault(k, []).append(v)
    return {k: statistics.median(vs) for k, vs in groups.items() if len(vs) >= 3 or k.startswith("*|")}


def _lookup(table: Dict[str, float], p: Dict[str, Any]) -> float:
    # Reels and posts are measured separately: they reach different audiences, and some sources
    # (reels tab) only expose comment counts.
    return table.get(f"{_page_key(p)}|{_kind(p)}", table.get(f"*|{_kind(p)}", 0.0))


def _age_factor(age_h: float) -> float:
    """Share of its final engagement a post usually has at age_h (front-loaded, ~1 after MATURE_H)."""
    return min(1.0, math.sqrt(max(age_h, 1.0) / MATURE_H))


def perf_label(ratio: float) -> str:
    return next(label for threshold, label in PERF_LEVELS if ratio >= threshold)


def _percentiles(values: List[float]) -> List[float]:
    n = len(values)
    if n == 1:
        return [1.0]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 / (n - 1)
        i = j + 1
    return ranks


def _hook(caption: Optional[str]) -> Optional[str]:
    text = (caption or "").strip()
    if not text:
        return None
    first = re.split(r"(?<=[.!?…])\s+|\n", text, maxsplit=1)[0]
    return first[:160]


def score_posts(conn: sqlite3.Connection, hours: float = 24, max_age_hours: float = DEFAULT_MAX_AGE_H,
                kind: str = "all", page_urls: Optional[List[str]] = None,
                now: Optional[float] = None) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """All posts created within max_age_hours, with performance + trending metrics, sorted by trending score.

    Returns (items, captions by post_id).
    """
    now = now or time.time()
    window_s = max(1.0, float(hours)) * 3600
    history = store.posts_since(conn, now - max(BASELINE_DAYS * 86400, max_age_hours * 3600), page_urls)
    snaps = store.snapshots_for(conn, [p["post_id"] for p in history])
    curves = {p["post_id"]: Curve(p.get("created_at"), snaps[p["post_id"]]) for p in history}

    growth_pairs: List[Tuple[str, float]] = []
    perf_pairs: List[Tuple[str, float]] = []
    for p in history:
        c, created = curves[p["post_id"]], p.get("created_at")
        if not c.snap_ts or not created:
            continue
        age_h = (c.snap_ts[-1] - created) / 3600
        norm = c.now / _age_factor(age_h)
        for key in (f"{_page_key(p)}|{_kind(p)}", f"*|{_kind(p)}"):
            perf_pairs.append((key, norm))
            if now - created >= window_s:
                growth_pairs.append((key, c.at(created + window_s)))
    perf_base = _median_by(perf_pairs)
    growth_base = _median_by(growth_pairs)

    min_created = now - max_age_hours * 3600
    rows = []
    for p in history:
        c = curves[p["post_id"]]
        created = p.get("created_at") or p["first_seen"]
        if created < min_created or not c.snap_ts:
            continue
        if kind in ("post", "reel") and _kind(p) != kind:
            continue
        start = now - window_s
        delta = max(0.0, c.now - c.at(start))
        v_recent = c.rate(1) or 0.0
        v_prev = c.rate(2)
        accel = (v_recent / v_prev) if v_prev else None
        g_base = _lookup(growth_base, p)
        age_h = (c.snap_ts[-1] - created) / 3600
        p_base = _lookup(perf_base, p)
        ratio = (c.now / _age_factor(age_h)) / (p_base + PERF_SMOOTHING)
        rows.append({
            "post": p, "delta": delta, "velocity": v_recent, "accel": accel,
            "outperform": delta / (g_base + BASELINE_SMOOTHING), "growth_base": g_base,
            "perf_ratio": ratio, "perf_base": p_base, "engagement": c.now,
            "estimated": bool(p.get("created_at")) and p["created_at"] < start and c.snap_ts[0] > start,
            "last": snaps[p["post_id"]][-1], "snapshots": len(c.snap_ts),
        })

    if rows:
        pd = _percentiles([math.log1p(r["delta"]) for r in rows])
        pv = _percentiles([math.log1p(r["velocity"]) for r in rows])
        po = _percentiles([math.log1p(r["outperform"]) for r in rows])
        for r, a, b, c_ in zip(rows, pd, pv, po):
            r["score"] = W_DELTA * a + W_VELOCITY * b + W_OUTPERFORM * c_
    rows.sort(key=lambda r: (r["score"], r["delta"]), reverse=True)

    items, captions = [], {}
    for r in rows:
        p, last = r["post"], r["last"]
        created = p.get("created_at")
        captions[p["post_id"]] = p.get("caption") or ""
        items.append({
            "post_id": p["post_id"],
            "url": p.get("url"),
            "page": p.get("page_name") or p.get("page_url") or p.get("source"),
            "page_key": _page_key(p),
            "kind": _kind(p),
            "created_at": created,
            "age_hours": round((now - created) / 3600, 1) if created else None,
            "reactions": last["reactions"],
            "comments": last["comments"],
            "shares": last["shares"],
            "views": last["views"],
            "engagement": round(r["engagement"]),
            "perf_ratio": round(r["perf_ratio"], 2),
            "perf_label": perf_label(r["perf_ratio"]),
            "page_typical": round(r["perf_base"]),
            "delta_window": round(r["delta"]),
            "velocity_per_hour": round(r["velocity"], 1),
            "rising": bool(r["accel"] and r["accel"] > RISING_ACCEL),
            "outperform": round(r["outperform"], 2),
            "page_baseline": round(r["growth_base"]),
            "score": round(r["score"] * 100, 1),
            "estimated": r["estimated"],
            "snapshots": r["snapshots"],
            "story_title": p.get("story_title") or guess_title(p.get("caption") or ""),
            "hook": _hook(p.get("caption")),
        })
    return items, captions


# ---------------------------------------------------------------- dedupe + top N

def _norm(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def _similar(a: str, b: str) -> bool:
    return bool(a and b) and SequenceMatcher(None, a, b).ratio() >= DEDUPE_RATIO


def _dedupe(ranked: List[Dict[str, Any]], captions: Dict[str, str]) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    for item in ranked:
        tkey, ckey = _norm(item.get("story_title")), _norm(captions.get(item["post_id"], "")[:120])
        for g in groups:
            if (tkey and _similar(tkey, g["tkey"])) or _similar(ckey, g["ckey"]):
                g["pages"].add(item["page"] or "?")
                g["posts"] += 1
                break
        else:
            groups.append({"tkey": tkey, "ckey": ckey, "best": item, "pages": {item["page"] or "?"}, "posts": 1})
    out = []
    for g in groups:
        best = dict(g["best"])
        best["same_story_pages"] = len(g["pages"])
        best["same_story_posts"] = g["posts"]
        out.append(best)
    return out


def _notes(conn: sqlite3.Connection, items: List[Dict[str, Any]]) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    runs = store.last_runs(conn, 1)
    notes = []
    if not runs:
        notes.append("Chưa có lần crawl nào.")
        return notes, None
    if items and max(i["snapshots"] for i in items) < 2:
        notes.append("Mới có 1 lần crawl: tốc độ tăng đang ước lượng = tương tác / tuổi post. "
                     "Crawl thêm (mỗi ~2h) để đo tăng trưởng 24h thật.")
    if runs[0].get("mode") == "guest":
        notes.append("Crawl ở chế độ khách (không đăng nhập): mỗi page chỉ thấy ~6 post / reels mới nhất trên feed "
                     "mỗi lần, không đọc tab Reels, không tìm theo từ khoá.")
    return notes, runs[0]


def top_trending(conn: sqlite3.Connection, hours: float = 24, limit: int = 10, kind: str = "all",
                 use_ai: bool = True, max_age_hours: float = DEFAULT_MAX_AGE_H,
                 now: Optional[float] = None) -> Dict[str, Any]:
    """Top N trending posts across tracked pages: one post per story, at most MAX_PER_PAGE per page."""
    now = now or time.time()
    items, captions = score_posts(conn, hours, max_age_hours, kind, None, now)
    if use_ai:
        fill_titles_with_ai(conn, items[: limit * 3], captions)

    picked: List[Dict[str, Any]] = []
    per_page: Dict[str, int] = {}
    for item in _dedupe(items, captions):
        if per_page.get(item["page_key"], 0) >= MAX_PER_PAGE:
            continue
        per_page[item["page_key"]] = per_page.get(item["page_key"], 0) + 1
        item.pop("page_key")
        picked.append(item)
        if len(picked) >= limit:
            break

    notes, run = _notes(conn, items)
    return {
        "window_hours": hours,
        "generated_at": now,
        "last_crawl_at": run["ts"] if run else None,
        "last_crawl_error": run["error"] if run else None,
        "candidates": len(items),
        "items": picked,
        "notes": notes,
    }


def page_performance(conn: sqlite3.Connection, page_urls: List[str], max_age_hours: float = DEFAULT_MAX_AGE_H,
                     hours: float = 24, now: Optional[float] = None) -> Dict[str, Any]:
    """Every post of the given pages within max_age_hours, with performance vs. its page, plus a per-page summary."""
    now = now or time.time()
    items, captions = score_posts(conn, hours, max_age_hours, "all", page_urls, now)
    pages: Dict[str, Dict[str, Any]] = {}
    for item in items:
        item["caption"] = captions.get(item["post_id"], "")[:400]
        page = pages.setdefault(item["page_key"], {"page_url": item["page_key"], "name": item["page"],
                                                   "posts": 0, "good": 0, "best_ratio": 0.0})
        page["posts"] += 1
        page["good"] += item["perf_ratio"] >= 1.0
        page["best_ratio"] = max(page["best_ratio"], item["perf_ratio"])
    notes, run = _notes(conn, items)
    seen = store.page_activity(conn, page_urls)
    for url in page_urls:
        page = pages.setdefault(url, {"page_url": url, "name": None, "posts": 0, "good": 0, "best_ratio": 0.0})
        page["crawled"] = seen.get(url, {}).get("count", 0)
        page["latest_post_at"] = seen.get(url, {}).get("latest")
        page["name"] = page["name"] or seen.get(url, {}).get("name")
    return {
        "max_age_hours": max_age_hours,
        "generated_at": now,
        "last_crawl_at": run["ts"] if run else None,
        "last_crawl_mode": run.get("mode") if run else None,
        "pages": list(pages.values()),
        "items": items,
        "notes": notes,
    }
