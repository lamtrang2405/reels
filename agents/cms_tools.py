"""Tools for the web truyện agent: read/update the story CMS (Supabase REST, service role)."""

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from agents.runtime import add_artifact

_TIMEOUT = 20.0


def _read_env_file(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    p = Path(path).expanduser()
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _config() -> Tuple[str, str]:
    url = os.environ.get("CMS_SUPABASE_URL", "").strip()
    key = os.environ.get("CMS_SUPABASE_SERVICE_ROLE_KEY", "").strip()
    env_file = os.environ.get("CMS_ENV_FILE", "").strip()
    if (not url or not key) and env_file:
        env = _read_env_file(env_file)
        url = url or env.get("NEXT_PUBLIC_SUPABASE_URL", "")
        key = key or env.get("SUPABASE_SERVICE_ROLE_KEY", "")
    return url.rstrip("/"), key


def site_url() -> str:
    return os.environ.get("CMS_SITE_URL", "http://localhost:3000").rstrip("/")


def cms_status() -> Dict[str, Any]:
    url, key = _config()
    if not url or not key:
        return {"ok": False, "detail": "Chưa cấu hình CMS (CMS_ENV_FILE hoặc CMS_SUPABASE_URL + CMS_SUPABASE_SERVICE_ROLE_KEY)"}
    return {"ok": True, "detail": site_url()}


def _request(method: str, table: str, params: Optional[Dict[str, str]] = None, json_body: Any = None,
             prefer: str = "") -> httpx.Response:
    url, key = _config()
    if not url or not key:
        raise RuntimeError(cms_status()["detail"])
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    resp = httpx.request(method, f"{url}/rest/v1/{table}", params=params, json=json_body,
                         headers=headers, timeout=_TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"Supabase {resp.status_code}: {resp.text[:300]}")
    return resp


def _count(table: str, filters: Optional[Dict[str, str]] = None) -> int:
    params = {"select": "id", **(filters or {})}
    resp = _request("HEAD", table, params=params, prefer="count=exact")
    rng = resp.headers.get("content-range", "*/0")
    try:
        return int(rng.split("/")[-1])
    except ValueError:
        return 0


def _admin_link(story_id: str) -> str:
    return f"{site_url()}/admin/stories/{story_id}/edit"


def _find_story(story: str) -> Optional[dict]:
    story = (story or "").strip()
    if not story:
        return None
    cols = "id,title,slug,status,is_featured,published_at"
    for field in ("id", "slug"):
        if field == "id" and len(story) != 36:
            continue
        rows = _request("GET", "stories", {"select": cols, field: f"eq.{story}", "limit": "1"}).json()
        if rows:
            return rows[0]
    rows = _request("GET", "stories", {"select": cols, "title": f"ilike.*{story}*", "limit": "2"}).json()
    return rows[0] if len(rows) == 1 else None


def cms_overview() -> Dict[str, Any]:
    """Tổng quan CMS web truyện: số truyện (xuất bản / nháp / đề cử), số chương, số danh mục."""
    try:
        return {
            "stories_total": _count("stories"),
            "stories_published": _count("stories", {"status": "eq.published"}),
            "stories_draft": _count("stories", {"status": "eq.draft"}),
            "stories_featured": _count("stories", {"is_featured": "eq.true"}),
            "chapters_total": _count("chapters"),
            "categories": _count("categories"),
            "admin_url": f"{site_url()}/admin?tab=stories",
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def list_stories(search: str = "", status: str = "all", featured_only: bool = False, limit: int = 20) -> Dict[str, Any]:
    """Liệt kê / tìm truyện trong CMS, mới cập nhật trước.

    Args:
        search: từ khóa trong tiêu đề (để trống = tất cả).
        status: "all", "published" (đã xuất bản) hoặc "draft" (nháp).
        featured_only: chỉ lấy truyện đang được đề cử.
        limit: số truyện tối đa (1-50).
    """
    params = {
        "select": "id,title,slug,status,is_featured,category,completion_status,author,updated_at,chapters(count)",
        "order": "updated_at.desc",
        "limit": str(max(1, min(int(limit), 50))),
    }
    if search.strip():
        params["title"] = f"ilike.*{search.strip()}*"
    if status in ("published", "draft"):
        params["status"] = f"eq.{status}"
    if featured_only:
        params["is_featured"] = "eq.true"
    try:
        rows = _request("GET", "stories", params).json()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {
        "count": len(rows),
        "stories": [
            {
                "id": r["id"],
                "title": r["title"],
                "slug": r["slug"],
                "status": r["status"],
                "featured": r["is_featured"],
                "category": r.get("category"),
                "completion": r.get("completion_status"),
                "author": r.get("author"),
                "chapters": (r.get("chapters") or [{}])[0].get("count", 0),
                "updated_at": r.get("updated_at"),
            }
            for r in rows
        ],
    }


def get_story(story: str) -> Dict[str, Any]:
    """Xem chi tiết một truyện: thông tin, tóm tắt, thể loại, danh sách chương.

    Args:
        story: id, slug hoặc một phần tiêu đề truyện.
    """
    try:
        found = _find_story(story)
        if not found:
            return {"error": f"Không tìm thấy truyện '{story}' (hoặc có nhiều truyện trùng — dùng id/slug)."}
        params = {
            "select": "id,title,slug,summary,author,status,is_featured,category,completion_status,"
                      "total_chapters_planned,cover_url,published_at,updated_at,"
                      "chapters(chapter_number,title,is_published),story_tags(tags(name))",
            "id": f"eq.{found['id']}",
        }
        row = _request("GET", "stories", params).json()[0]
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    chapters = sorted(row.get("chapters") or [], key=lambda c: c["chapter_number"])
    add_artifact("link", _admin_link(row["id"]), f"Sửa truyện: {row['title']}")
    return {
        "id": row["id"],
        "title": row["title"],
        "slug": row["slug"],
        "summary": row["summary"],
        "author": row.get("author"),
        "status": row["status"],
        "featured": row["is_featured"],
        "category": row.get("category"),
        "completion": row.get("completion_status"),
        "tags": [t["tags"]["name"] for t in row.get("story_tags") or [] if t.get("tags")],
        "cover_url": row.get("cover_url"),
        "chapters_total": len(chapters),
        "chapters_published": sum(1 for c in chapters if c["is_published"]),
        "first_chapters": [{"no": c["chapter_number"], "title": c["title"]} for c in chapters[:5]],
        "last_chapter": ({"no": chapters[-1]["chapter_number"], "title": chapters[-1]["title"]} if chapters else None),
        "public_url": f"{site_url()}/stories/{row['slug']}",
        "admin_url": _admin_link(row["id"]),
    }


def _update_story(story: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    found = _find_story(story)
    if not found:
        return {"ok": False, "error": f"Không tìm thấy truyện '{story}'."}
    patch = {**patch, "updated_at": datetime.now(timezone.utc).isoformat()}
    _request("PATCH", "stories", {"id": f"eq.{found['id']}"}, json_body=patch, prefer="return=minimal")
    add_artifact("link", _admin_link(found["id"]), f"Sửa truyện: {found['title']}")
    return {"ok": True, "id": found["id"], "title": found["title"],
            "note": "Trang public cập nhật sau tối đa ~60 giây (ISR)."}


def set_story_published(story: str, published: bool) -> Dict[str, Any]:
    """Bật/tắt xuất bản một truyện (chỉ dùng khi người dùng yêu cầu rõ ràng).

    Args:
        story: id, slug hoặc tiêu đề truyện.
        published: true = xuất bản, false = chuyển về nháp.
    """
    try:
        found = _find_story(story)
        if not found:
            return {"ok": False, "error": f"Không tìm thấy truyện '{story}'."}
        now = datetime.now(timezone.utc).isoformat()
        patch = {
            "status": "published" if published else "draft",
            "published_at": (found.get("published_at") or now) if published else None,
        }
        return _update_story(found["id"], patch)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def set_story_featured(story: str, featured: bool) -> Dict[str, Any]:
    """Bật/tắt đề cử một truyện trên trang chủ (chỉ dùng khi người dùng yêu cầu rõ ràng).

    Args:
        story: id, slug hoặc tiêu đề truyện.
        featured: true = đề cử, false = bỏ đề cử.
    """
    try:
        return _update_story(story, {"is_featured": bool(featured)})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def update_story_summary(story: str, summary: str) -> Dict[str, Any]:
    """Sửa phần tóm tắt (giới thiệu) của truyện (chỉ dùng khi người dùng yêu cầu / đã duyệt nội dung).

    Args:
        story: id, slug hoặc tiêu đề truyện.
        summary: nội dung tóm tắt mới.
    """
    summary = (summary or "").strip()
    if not summary:
        return {"ok": False, "error": "Tóm tắt mới đang trống."}
    try:
        return _update_story(story, {"summary": summary})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def story_stats(days: int = 7, limit: int = 10) -> Dict[str, Any]:
    """Top truyện theo lượt xem trong N ngày gần nhất (bảng story_metrics_daily).

    Args:
        days: số ngày gần nhất (1-90).
        limit: số truyện trả về.
    """
    days = max(1, min(int(days), 90))
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    try:
        rows = _request("GET", "story_metrics_daily", {
            "select": "story_id,views,unique_readers,stories(title,slug)",
            "metric_date": f"gte.{since}",
            "limit": "5000",
        }).json()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    agg: Dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["story_id"], {
            "story_id": r["story_id"],
            "title": (r.get("stories") or {}).get("title"),
            "slug": (r.get("stories") or {}).get("slug"),
            "views": 0,
            "unique_readers": 0,
        })
        a["views"] += r.get("views") or 0
        a["unique_readers"] += r.get("unique_readers") or 0
    top: List[dict] = sorted(agg.values(), key=lambda x: x["views"], reverse=True)[: max(1, int(limit))]
    return {"since": since, "days": days, "stories_with_views": len(agg), "top": top}


CMS_READ_TOOLS = [cms_overview, list_stories, get_story, story_stats]
CMS_WRITE_TOOLS = [set_story_published, set_story_featured, update_story_summary]
