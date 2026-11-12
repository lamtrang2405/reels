"""Playwright crawler for Facebook page posts / reels / keyword search.

Metrics come from the GraphQL responses the page loads while scrolling (more stable than the DOM);
the DOM is only read when no GraphQL post was captured for a source.
"""

import base64
import json
import random
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import quote

from trending import store

FB = "https://www.facebook.com"
DEFAULT_MAX_POSTS = 20
DEFAULT_MAX_AGE_H = 72
MAX_SCROLLS = 15
SOURCE_DELAY_S = (3.0, 8.0)
SCROLL_WAIT_MS = (1500, 3000)

# Shared/attached stories and the inline comment list carry other objects' metrics; never read them as the post's.
_SKIP_KEYS = frozenset({
    "attached_story", "attached_story_layout", "attached_story_renderer",
    "comment_list_renderer", "comment_rendering_instance_for_feed_location", "top_reactions",
})
_VIDEO_KEYS = ("play_count", "video_view_count", "playable_url", "short_form_video_context")


class FBBlocked(RuntimeError):
    """Facebook asked for login / checkpoint: stop the whole run instead of retrying."""


# ---------------------------------------------------------------- config + cookies

def config_path() -> Path:
    return store.data_dir() / "pages.json"


def load_config() -> Dict[str, Any]:
    path = config_path()
    if not path.is_file():
        example = Path(__file__).resolve().parent / "pages.example.json"
        raise FileNotFoundError(f"Chưa có {path}. Copy từ {example} rồi điền danh sách page.")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("pages", [])
    cfg.setdefault("keywords", [])
    return cfg


_SAMESITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}


def load_cookies(path: str) -> List[Dict[str, Any]]:
    """Accepts Playwright JSON, browser-extension export (Cookie-Editor...), Netscape txt or a header string."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"Không thấy file cookie FB_COOKIES_FILE={path}")
    text = p.read_text(encoding="utf-8").strip()
    raw: List[Dict[str, Any]] = []
    try:
        data = json.loads(text)
        raw = data.get("cookies", []) if isinstance(data, dict) else data
    except json.JSONDecodeError:
        if "\t" in text:
            for line in text.splitlines():
                parts = line.split("\t")
                if len(parts) >= 7 and not line.startswith("#"):
                    raw.append({"domain": parts[0], "path": parts[2], "secure": parts[3].upper() == "TRUE",
                                "expires": float(parts[4] or 0), "name": parts[5], "value": parts[6]})
        else:
            for pair in text.split(";"):
                if "=" in pair:
                    name, _, value = pair.strip().partition("=")
                    raw.append({"name": name, "value": value})

    cookies = []
    for c in raw:
        if not c.get("name"):
            continue
        cookie: Dict[str, Any] = {
            "name": c["name"],
            "value": str(c.get("value", "")),
            "domain": c.get("domain") or ".facebook.com",
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
        }
        expires = c.get("expires", c.get("expirationDate"))
        if expires and float(expires) > 0:
            cookie["expires"] = float(expires)
        same = _SAMESITE.get(str(c.get("sameSite", "")).lower())
        if same:
            cookie["sameSite"] = same
            if same == "None":
                cookie["secure"] = True
        cookies.append(cookie)
    if not any(c["name"] == "c_user" for c in cookies):
        raise ValueError("Cookie không có c_user — cần cookie của tài khoản đã đăng nhập.")
    return cookies


# ---------------------------------------------------------------- parsing helpers

_COUNT_RE = re.compile(r"(\d+(?:[.,\s]\d+)*)\s*(k|n|nghìn|ngàn|tr|triệu|m|b|tỷ)?\b", re.I)
_MULT = {"k": 1e3, "n": 1e3, "nghìn": 1e3, "ngàn": 1e3, "tr": 1e6, "triệu": 1e6, "m": 1e6, "b": 1e9, "tỷ": 1e9}


def parse_count(value: Any) -> Optional[int]:
    """'1,2K' / '3,4 N' / '1.234' / {'count': 5} / 7 -> int."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        for key in ("count", "total_count", "count_reduced"):
            if key in value:
                return parse_count(value[key])
        return None
    m = _COUNT_RE.search(str(value))
    if not m:
        return None
    num, suffix = m.group(1), (m.group(2) or "").lower()
    if suffix:
        return int(float(num.replace(" ", "").replace(",", ".")) * _MULT[suffix])
    return int(re.sub(r"[.,\s]", "", num))


def parse_bodies(text: str) -> Iterable[Any]:
    """GraphQL responses are newline-delimited JSON, sometimes prefixed with for (;;);"""
    text = text.strip()
    if text.startswith("for (;;);"):
        text = text[9:]
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _find(node: Any, key: str) -> Any:
    """First non-null value of key in a depth-first walk, skipping attached stories."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if cur.get(key) is not None:
                return cur[key]
            stack.extend(v for k, v in reversed(list(cur.items())) if k not in _SKIP_KEYS and isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(v for v in reversed(cur) if isinstance(v, (dict, list)))
    return None


def _text(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("text")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _metrics(node: Any) -> Dict[str, Optional[int]]:
    """Prefer the post's own UFI summary block; fall back to the whole node for missing fields."""
    ufi = _find(node, "comet_ufi_summary_and_actions_renderer")
    summary = ufi.get("feedback") if isinstance(ufi, dict) else None
    if not isinstance(summary, dict):
        return _metrics_in(node)
    found = _metrics_in(summary)
    if any(v is None for v in found.values()):
        rest = _metrics_in(node)
        found = {k: v if v is not None else rest[k] for k, v in found.items()}
    return found


def _metrics_in(node: Any) -> Dict[str, Optional[int]]:
    reactions = parse_count(_find(node, "reaction_count"))
    if reactions is None:
        reactions = parse_count(_find(node, "i18n_reaction_count"))
    comments = None
    cri = _find(node, "comment_rendering_instance")
    if isinstance(cri, dict):
        comments = parse_count((cri.get("comments") or {}).get("total_count"))
    if comments is None:
        comments = parse_count(_find(node, "total_comment_count"))
    if comments is None:
        comments = parse_count(_find(node, "i18n_comment_count"))
    shares = parse_count(_find(node, "share_count"))
    if shares is None:
        shares = parse_count(_find(node, "i18n_share_count"))
    views = None
    for key in ("play_count", "video_view_count", "i18n_play_count", "i18n_video_view_count"):
        views = parse_count(_find(node, key))
        if views is not None:
            break
    return {"reactions": reactions, "comments": comments, "shares": shares, "views": views}


def _merge(items: Dict[str, Dict[str, Any]], item: Dict[str, Any]) -> None:
    pid = item["post_id"]
    cur = items.get(pid)
    if cur is None:
        items[pid] = item
        return
    for k, v in item.items():
        if v is None:
            continue
        if k in store.METRICS:
            cur[k] = max(cur.get(k) or 0, v)
        elif k == "kind":
            cur[k] = "reel" if "reel" in (cur.get(k), v) else v
        elif k == "caption":
            if len(v) > len(cur.get(k) or ""):
                cur[k] = v
        elif cur.get(k) is None:
            cur[k] = v


def _feedback_target(fid: Any) -> Optional[str]:
    if not isinstance(fid, str) or len(fid) < 8:
        return None
    try:
        decoded = base64.b64decode(fid + "=" * (-len(fid) % 4)).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None
    if not decoded.startswith("feedback:"):
        return None
    target = decoded.split(":", 1)[1]
    # "feedback:<post>_<comment>" belongs to a comment, not the post.
    return target if target and "_" not in target else None


def _actor(node: Any) -> Dict[str, Optional[str]]:
    actors = _find(node, "actors")
    actor = actors[0] if isinstance(actors, list) and actors and isinstance(actors[0], dict) else None
    if actor is None:
        owner = _find(node, "owner") or _find(node, "video_owner")
        actor = owner if isinstance(owner, dict) else {}
    return {"page_id": actor.get("id"), "page_name": actor.get("name"), "page_url": actor.get("url")}


def _post_url(node: Any, post_id: str, kind: str) -> str:
    for key in ("wwwURL", "url", "permalink_url"):
        u = _find(node, key)
        if isinstance(u, str) and "facebook.com" in u and re.search(r"/(posts|permalink|videos|reel|photos?)\b|story_fbid|fbid=", u):
            return u
    return f"{FB}/reel/{post_id}" if kind == "reel" else f"{FB}/{post_id}"


def _story_item(d: Dict[str, Any]) -> Dict[str, Any]:
    pid = str(d["post_id"])
    url = _post_url(d, pid, "post")
    is_reel = "/reel/" in url or _find(d, "short_form_video_context") is not None
    created = _find(d, "creation_time") or _find(d, "publish_time")
    return {
        "post_id": pid,
        "kind": "reel" if is_reel else "post",
        "url": url,
        "created_at": float(created) if isinstance(created, (int, float)) else None,
        "caption": _text(_find(d, "message")),
        **_actor(d),
        **_metrics(d),
    }


def _video_item(d: Dict[str, Any]) -> Dict[str, Any]:
    vid = str(d["id"])
    created = _find(d, "creation_time") or _find(d, "publish_time")
    caption = _text(_find(d, "message")) or _text(_find(d, "description")) or _text(d.get("title"))
    return {
        "post_id": vid,
        "kind": "reel",
        "url": f"{FB}/reel/{vid}",
        "created_at": float(created) if isinstance(created, (int, float)) else None,
        "caption": caption,
        **_actor(d),
        **_metrics(d),
    }


def extract(obj: Any, items: Dict[str, Dict[str, Any]], feedback: Dict[str, Dict[str, int]], reel_mode: bool = False) -> None:
    """Collect post / reel items and standalone feedback metrics from one GraphQL payload."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(v for v in cur if isinstance(v, (dict, list)))
            continue
        if not isinstance(cur, dict):
            continue
        pid = cur.get("post_id")
        if isinstance(pid, (str, int)) and str(pid) and (
            "comet_sections" in cur or cur.get("__typename") == "Story" or "creation_time" in cur or "feedback" in cur
        ):
            _merge(items, _story_item(cur))
            continue
        vid = cur.get("id")
        if (reel_mode and isinstance(vid, str) and vid.isdigit()
                and (cur.get("__typename") == "Video" or any(k in cur for k in _VIDEO_KEYS))):
            _merge(items, _video_item(cur))
            continue
        if any(k in cur for k in ("reaction_count", "share_count", "comment_rendering_instance", "i18n_reaction_count")):
            target = _feedback_target(cur.get("id"))
            if target:
                m = {k: v for k, v in _metrics(cur).items() if v is not None}
                slot = feedback.setdefault(target, {})
                for k, v in m.items():
                    slot[k] = max(slot.get(k, 0), v)
        stack.extend(v for k, v in cur.items() if k not in _SKIP_KEYS and isinstance(v, (dict, list)))


def apply_feedback(items: Dict[str, Dict[str, Any]], feedback: Dict[str, Dict[str, int]], reel_mode: bool) -> None:
    for target, m in feedback.items():
        if target in items:
            _merge(items, {"post_id": target, **m})
        elif reel_mode and target.isdigit():
            _merge(items, {"post_id": target, "kind": "reel", "url": f"{FB}/reel/{target}", **m})


# ---------------------------------------------------------------- DOM fallback

_DOM_JS = """() => [...document.querySelectorAll('[role="article"]')]
  .filter(a => !a.parentElement || !a.parentElement.closest('[role="article"]'))
  .map(a => ({
    links: [...a.querySelectorAll('a[href]')].map(x => x.href),
    labels: [...a.querySelectorAll('[aria-label]')].map(x => x.getAttribute('aria-label')),
    text: (a.innerText || '').slice(0, 4000),
  }))"""
_ID_RES = (
    (re.compile(r"/reel/(\d+)"), "reel"),
    (re.compile(r"/posts/([\w]+)"), "post"),
    (re.compile(r"story_fbid=([\w]+)"), "post"),
    (re.compile(r"/videos/(?:[\w.-]+/)?(\d+)"), "post"),
)
_NUM = r"(\d+(?:[.,\s]\d+)*\s*(?:K|N|Tr|M)?)"
_DOM_COUNTS = {
    "reactions": re.compile(r"(?:Tất cả cảm xúc|All reactions)\s*:?\s*" + _NUM, re.I),
    "comments": re.compile(_NUM + r"\s*(?:bình luận|comments?)\b", re.I),
    "shares": re.compile(_NUM + r"\s*(?:lượt chia sẻ|chia sẻ|shares?)\b", re.I),
    "views": re.compile(_NUM + r"\s*(?:lượt xem|views?|plays?)\b", re.I),
}


def dom_fallback(page: Any) -> Dict[str, Dict[str, Any]]:
    items: Dict[str, Dict[str, Any]] = {}
    try:
        articles = page.evaluate(_DOM_JS)
    except Exception:  # noqa: BLE001
        return items
    for art in articles:
        pid, kind, url = None, "post", None
        for link in art.get("links") or []:
            for rx, k in _ID_RES:
                m = rx.search(link)
                if m:
                    pid, kind, url = m.group(1), k, link.split("?")[0]
                    break
            if pid:
                break
        if not pid:
            continue
        blob = "\n".join([art.get("text") or "", *[l for l in art.get("labels") or [] if l]])
        item: Dict[str, Any] = {"post_id": pid, "kind": kind, "url": url, "created_at": None,
                                "caption": (art.get("text") or "")[:1500] or None}
        for key, rx in _DOM_COUNTS.items():
            m = rx.search(blob)
            item[key] = parse_count(m.group(1)) if m else None
        _merge(items, item)
    return items


_FOLLOWERS_RE = re.compile(r"(\d+(?:[.,\s]\d+)*\s*(?:K|N|Tr|M|triệu|nghìn|ngàn)?)\s*(?:người theo dõi|followers)", re.I)


def read_followers(page: Any) -> Optional[int]:
    try:
        m = _FOLLOWERS_RE.search(page.inner_text("body", timeout=5000))
    except Exception:  # noqa: BLE001
        return None
    return parse_count(m.group(1)) if m else None


# ---------------------------------------------------------------- crawl

_NOT_PAGE_PATHS = {"posts", "reels", "reel", "videos", "photos", "about", "mentions", "followers", "community"}


def normalize_page_url(url: str) -> Optional[str]:
    """Any pasted Facebook page / profile link -> canonical https://www.facebook.com/<page> (None if not a page)."""
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url):
        url = "https://" + url
    m = re.match(r"^https?://(?:[\w-]+\.)?(?:facebook\.com|fb\.com)(/[^#]*)?$", url, re.I)
    if not m:
        return None
    path = m.group(1) or "/"
    pid = re.search(r"profile\.php\?(?:.*&)?id=(\d+)", path)
    if pid:
        return f"{FB}/profile.php?id={pid.group(1)}"
    parts = [p for p in path.split("?")[0].split("/") if p]
    if not parts or parts[0] in ("watch", "groups", "share", "story.php", "permalink.php", "login", "search"):
        return None
    if parts[0] == "people" and len(parts) >= 3:
        return f"{FB}/people/{parts[1]}/{parts[2]}"
    if parts[0] == "pg" and len(parts) >= 2:
        parts = parts[1:]
    return f"{FB}/{parts[0]}" if parts[0].lower() not in _NOT_PAGE_PATHS else None


def build_sources(cfg: Dict[str, Any], max_pages: int, search: bool) -> List[Dict[str, Any]]:
    sources: List[Dict[str, Any]] = []
    for entry in cfg["pages"][: max(0, max_pages)]:
        entry = {"url": entry} if isinstance(entry, str) else entry
        base = normalize_page_url(entry["url"])
        if not base:
            continue
        name = entry.get("name")
        reels_url = f"{base}&sk=reels_tab" if "profile.php" in base else f"{base}/reels"
        sources.append({"url": base, "kind": "post", "page_url": base, "page_name": name, "seed": True})
        if entry.get("reels", True):
            sources.append({"url": reels_url, "kind": "reel", "page_url": base, "page_name": name, "seed": True})
    keywords = [k for k in cfg.get("keywords") or [] if k.strip()]
    per_run = int(cfg.get("max_keywords_per_run", 3))
    if search and keywords and per_run > 0:
        # Rotate keywords every 2 hours so each run searches a different slice.
        offset = int(time.time() // 7200) * per_run
        picked = [keywords[(offset + i) % len(keywords)] for i in range(min(per_run, len(keywords)))]
        for kw in picked:
            sources.append({"url": f"{FB}/search/posts?q={quote(kw)}", "kind": "search", "keyword": kw, "seed": False})
    return sources


def _check_blocked(page: Any, guest: bool) -> None:
    url = page.url.lower()
    if "checkpoint" in url or "/login" in url or "login.php" in url or "two_step" in url:
        if guest:
            raise FBBlocked(f"Facebook chặn truy cập không đăng nhập ({page.url}) — có thể IP bị giới hạn. "
                            "Dừng crawl; thử lại sau hoặc giảm số page / tần suất.")
        raise FBBlocked(f"Facebook yêu cầu đăng nhập / checkpoint ({page.url}). Dừng crawl; kiểm tra lại tài khoản và cookie.")
    if guest:
        return
    # Invalid sessions are not redirected: Facebook drops c_user and renders a login form in place.
    logged_in = any(c["name"] == "c_user" for c in page.context.cookies(FB))
    if not logged_in or page.locator('form input[name="pass"]').count() > 0:
        raise FBBlocked("Cookie Facebook hết hạn hoặc bị đăng xuất. Export lại cookie (FB_COOKIES_FILE) rồi chạy lại.")


def _save_debug(name: str, bodies: List[str]) -> Optional[str]:
    if not bodies:
        return None
    folder = store.data_dir() / "debug"
    folder.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w]+", "_", name)[-60:]
    path = folder / f"{int(time.time())}_{slug}.txt"
    path.write_text("\n\n".join(b[:200_000] for b in bodies[:3]), encoding="utf-8")
    return str(path)


class _Collector:
    def __init__(self, page: Any) -> None:
        self.responses: List[Any] = []
        self.raw: List[str] = []
        page.on("response", self._on_response)

    def _on_response(self, resp: Any) -> None:
        if "/api/graphql" in resp.url:
            self.responses.append(resp)

    def reset(self) -> None:
        self.responses.clear()
        self.raw.clear()

    def drain(self, items: Dict[str, Dict[str, Any]], reel_mode: bool) -> None:
        pending, self.responses = self.responses, []
        feedback: Dict[str, Dict[str, int]] = {}
        for resp in pending:
            try:
                body = resp.text()
            except Exception:  # noqa: BLE001
                continue
            if len(self.raw) < 3:
                self.raw.append(body)
            for obj in parse_bodies(body):
                extract(obj, items, feedback, reel_mode)
        apply_feedback(items, feedback, reel_mode)


def _wait(page: Any, low_ms: int, high_ms: int) -> None:
    page.wait_for_timeout(random.randint(low_ms, high_ms))


_EMBEDDED_JS = """() => [...document.querySelectorAll('script[type="application/json"]')]
  .map(s => s.textContent || '')
  .filter(t => t.includes('"post_id"') || t.includes('"play_count"') || t.includes('"reaction_count"'))"""


def _embedded(page: Any, items: Dict[str, Dict[str, Any]], reel_mode: bool) -> None:
    """Posts server-rendered into the initial HTML (first posts of a feed, single-post pages)."""
    try:
        blobs = page.evaluate(_EMBEDDED_JS)
    except Exception:  # noqa: BLE001
        return
    feedback: Dict[str, Dict[str, int]] = {}
    for blob in blobs:
        try:
            extract(json.loads(blob), items, feedback, reel_mode)
        except json.JSONDecodeError:
            continue
    apply_feedback(items, feedback, reel_mode)


def _dismiss_login(page: Any) -> None:
    """Logged-out visitors get a login modal; closing it keeps the feed scrollable a bit longer."""
    try:
        page.keyboard.press("Escape")
        close = page.locator('div[role="dialog"] [aria-label="Đóng"], div[role="dialog"] [aria-label="Close"]')
        if close.count():
            close.first.click(timeout=1500)
    except Exception:  # noqa: BLE001
        pass


def _open(page: Any, url: str, guest: bool) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    _check_blocked(page, guest)
    _wait(page, 2000, 4000)
    _check_blocked(page, guest)
    if guest:
        _dismiss_login(page)


def _crawl_source(page: Any, col: _Collector, src: Dict[str, Any], max_posts: int, cutoff: float,
                  max_reel_visits: int, guest: bool, log: Callable[[str], None]) -> List[Dict[str, Any]]:
    reel_mode = src["kind"] == "reel"
    col.reset()
    _open(page, src["url"], guest)

    followers = read_followers(page) if src["kind"] == "post" else None
    items: Dict[str, Dict[str, Any]] = {}
    _embedded(page, items, reel_mode)
    stalled = 0
    for _ in range(MAX_SCROLLS):
        before = len(items)
        col.drain(items, reel_mode)
        dated = [i["created_at"] for i in items.values() if i.get("created_at")]
        if len(items) >= max_posts or (len(dated) >= 3 and min(dated) < cutoff):
            break
        # Logged-out feeds stop loading behind the login wall; don't keep scrolling for nothing.
        stalled = stalled + 1 if len(items) == before else 0
        if stalled >= 3:
            break
        if guest:
            _dismiss_login(page)
        page.mouse.wheel(0, random.randint(1800, 2800))
        _wait(page, *SCROLL_WAIT_MS)
    col.drain(items, reel_mode)

    if not items and src["kind"] != "search":
        items = dom_fallback(page)
        if items:
            log(f"  GraphQL không bắt được post, dùng DOM fallback ({len(items)} post)")
        else:
            dump = _save_debug(src["url"], col.raw)
            log(f"  Không lấy được post nào{f' — mẫu response: {dump}' if dump else ''}")

    # Single reel pages return no data to logged-out visitors.
    if reel_mode and max_reel_visits > 0 and not guest:
        missing = [i for i in items.values() if not (i.get("reactions") or i.get("comments"))]
        for reel in missing[:max_reel_visits]:
            _wait(page, 2000, 4000)
            _open(page, reel["url"], guest)
            col.drain(items, reel_mode)

    # Posts older than the window are kept too: they form the page's performance baseline.
    out = []
    for item in list(items.values())[: max_posts * 2]:
        item.setdefault("page_name", None)
        if src.get("seed"):
            item["page_name"] = item.get("page_name") or src.get("page_name")
            item["page_url"] = item.get("page_url") or src.get("page_url")
            if followers and (not item.get("page_url") or item["page_url"].rstrip("/") == src["page_url"]):
                item["followers"] = followers
        item["source"] = f"search:{src['keyword']}" if src.get("keyword") else src.get("page_url")
        out.append(item)
    return out


def _refresh_known(page: Any, col: _Collector, tracked: List[Dict[str, Any]], seen: Iterable[str], guest: bool,
                   limit: int, log: Callable[[str], None]) -> List[Dict[str, Any]]:
    """Re-open permalinks of recent posts that fell out of the feed window, for a fresh snapshot."""
    seen = set(seen)
    todo = [p for p in tracked if p["post_id"] not in seen and p.get("url") and (p["kind"] == "post" or not guest)]
    out: List[Dict[str, Any]] = []
    if todo[:limit]:
        log(f"Cập nhật lại {min(len(todo), limit)} post đã theo dõi (mở link từng post)")
    for p in todo[:limit]:
        _wait(page, 2000, 5000)
        col.reset()
        try:
            _open(page, p["url"], guest)
        except FBBlocked:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"  {p['url']}: {str(e)[:120]}")
            continue
        items: Dict[str, Dict[str, Any]] = {}
        reel_mode = p["kind"] == "reel"
        _embedded(page, items, reel_mode)
        col.drain(items, reel_mode)
        found = items.get(p["post_id"])
        if found and any(found.get(m) is not None for m in store.METRICS):
            out.append({"post_id": p["post_id"], "kind": p["kind"], "created_at": found.get("created_at"),
                        **{m: found.get(m) for m in store.METRICS}})
    return out


def crawl(max_pages: int = 30, max_posts_per_page: int = DEFAULT_MAX_POSTS, max_age_hours: float = DEFAULT_MAX_AGE_H,
          search: bool = True, headless: Optional[bool] = None, cookies_file: str = "",
          pages: Optional[List[str]] = None, reels: bool = True,
          log: Callable[[str], None] = print) -> Dict[str, Any]:
    """Crawl pages (from `pages`, else pages.json) once and store one metrics snapshot per post.

    Without a cookie file it runs in guest mode (logged out): ~6 newest feed items per page (reels included),
    no reels tab, no keyword search; older posts are kept up to date through their permalinks.
    """
    import os

    from playwright.sync_api import sync_playwright

    started = time.time()
    cfg = load_config() if config_path().is_file() else {"pages": [], "keywords": []}
    if pages is not None:
        cfg = {**cfg, "pages": [{"url": u, "reels": reels} for u in pages], "keywords": []}
    cookies_file = cookies_file or os.environ.get("FB_COOKIES_FILE", "").strip()
    guest = not cookies_file
    cookies = [] if guest else load_cookies(cookies_file)
    sources = build_sources(cfg, max_pages, search and not guest)
    if guest:
        # Logged out, the reels tab lists old reels in no useful order; new reels show up in the feed anyway.
        sources = [s for s in sources if s["kind"] != "reel"]
    if not sources:
        raise ValueError("Chưa có link page Facebook hợp lệ nào.")
    page_urls = sorted({s["page_url"] for s in sources if s.get("page_url")})
    if headless is None:
        headless = os.environ.get("FB_HEADLESS", "1").strip() not in ("0", "false", "no")
    cutoff = started - max_age_hours * 3600
    max_reel_visits = int(cfg.get("max_reel_visits", 6))
    max_refresh = int(cfg.get("max_refresh", 40))
    log(f"Chế độ: {'khách (không đăng nhập)' if guest else 'cookie tài khoản'}")

    conn = store.connect()
    tracked = store.tracked_posts(conn, cutoff, page_urls if pages is not None else None)
    conn.close()

    collected: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    blocked = ""
    done = 0
    refreshed = 0
    launch = {"headless": headless, "args": ["--disable-blink-features=AutomationControlled"]}
    context_opts = {"locale": "vi-VN", "timezone_id": "Asia/Ho_Chi_Minh", "viewport": {"width": 1280, "height": 900}}
    with sync_playwright() as pw:
        browser = None
        if guest:
            browser = pw.chromium.launch(**launch)
            ctx = browser.new_context(**context_opts)
        else:
            ctx = pw.chromium.launch_persistent_context(str(store.data_dir() / "browser_profile"), **launch, **context_opts)
            ctx.add_cookies(cookies)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            col = _Collector(page)
            for idx, src in enumerate(sources):
                if idx:
                    time.sleep(random.uniform(*SOURCE_DELAY_S))
                log(f"[{idx + 1}/{len(sources)}] {src['url']}")
                try:
                    found = _crawl_source(page, col, src, max_posts_per_page, cutoff, max_reel_visits, guest, log)
                except FBBlocked as e:
                    blocked = str(e)
                    log(f"  DỪNG: {blocked}")
                    break
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{src['url']}: {str(e)[:200]}")
                    log(f"  lỗi: {str(e)[:200]}")
                    continue
                done += 1
                for item in found:
                    _merge(collected, item)
                log(f"  {len(found)} post")
            if not blocked and max_refresh > 0:
                try:
                    for item in _refresh_known(page, col, tracked, collected, guest, max_refresh, log):
                        _merge(collected, item)
                        refreshed += 1
                except FBBlocked as e:
                    blocked = str(e)
                    log(f"  DỪNG: {blocked}")
        finally:
            ctx.close()
            if browser is not None:
                browser.close()

    conn = store.connect()
    try:
        ts = time.time()
        snapshots = 0
        for item in collected.values():
            store.upsert_post(conn, item, ts)
            if any(item.get(m) is not None for m in store.METRICS):
                store.add_snapshot(conn, item["post_id"], item, ts)
                snapshots += 1
        store.record_run(conn, started, done, len(collected), blocked or "; ".join(errors)[:1000],
                         mode="guest" if guest else "cookie")
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": not blocked,
        "mode": "guest" if guest else "cookie",
        "pages": page_urls,
        "sources_total": len(sources),
        "sources_done": done,
        "posts": len(collected),
        "refreshed": refreshed,
        "snapshots": snapshots,
        "blocked": blocked or None,
        "errors": errors[:10],
        "duration_s": round(time.time() - started, 1),
    }
