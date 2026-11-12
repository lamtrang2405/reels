"""SQLite store: one row per post, one row per metrics snapshot (velocity needs history)."""

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
METRICS = ("reactions", "comments", "shares", "views")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id      TEXT PRIMARY KEY,
    page_id      TEXT,
    page_name    TEXT,
    page_url     TEXT,
    url          TEXT,
    kind         TEXT NOT NULL DEFAULT 'post',
    created_at   REAL,
    caption      TEXT,
    followers    INTEGER,
    source       TEXT,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    is_story     INTEGER,
    story_title  TEXT,
    topic_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_page ON posts(page_id);

CREATE TABLE IF NOT EXISTS snapshots (
    post_id   TEXT NOT NULL,
    ts        REAL NOT NULL,
    reactions INTEGER NOT NULL DEFAULT 0,
    comments  INTEGER NOT NULL DEFAULT 0,
    shares    INTEGER NOT NULL DEFAULT 0,
    views     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (post_id, ts)
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    ts       REAL PRIMARY KEY,
    duration REAL,
    sources  INTEGER,
    posts    INTEGER,
    error    TEXT,
    mode     TEXT
);
"""


def data_dir() -> Path:
    raw = os.environ.get("FB_TRENDING_DIR", "").strip()
    path = Path(raw).expanduser() if raw else ROOT / "data" / "trending"
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "trending.db"


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def upsert_post(conn: sqlite3.Connection, post: Dict[str, Any], seen_at: Optional[float] = None) -> None:
    """Insert or refresh a post; never overwrites known values with empty ones or the topic cache."""
    now = seen_at or time.time()
    conn.execute(
        """
        INSERT INTO posts (post_id, page_id, page_name, page_url, url, kind, created_at, caption,
                           followers, source, first_seen, last_seen)
        VALUES (:post_id, :page_id, :page_name, :page_url, :url, :kind, :created_at, :caption,
                :followers, :source, :now, :now)
        ON CONFLICT(post_id) DO UPDATE SET
            page_id    = COALESCE(excluded.page_id, posts.page_id),
            page_name  = COALESCE(excluded.page_name, posts.page_name),
            page_url   = COALESCE(excluded.page_url, posts.page_url),
            url        = COALESCE(excluded.url, posts.url),
            kind       = excluded.kind,
            created_at = COALESCE(posts.created_at, excluded.created_at),
            caption    = CASE WHEN length(COALESCE(excluded.caption, '')) > length(COALESCE(posts.caption, ''))
                              THEN excluded.caption ELSE posts.caption END,
            followers  = COALESCE(excluded.followers, posts.followers),
            source     = COALESCE(posts.source, excluded.source),
            last_seen  = excluded.last_seen
        """,
        {
            "post_id": str(post["post_id"]),
            "page_id": post.get("page_id"),
            "page_name": post.get("page_name"),
            "page_url": post.get("page_url"),
            "url": post.get("url"),
            "kind": post.get("kind") or "post",
            "created_at": post.get("created_at"),
            "caption": post.get("caption"),
            "followers": post.get("followers"),
            "source": post.get("source"),
            "now": now,
        },
    )


def add_snapshot(conn: sqlite3.Connection, post_id: str, metrics: Dict[str, Any], ts: Optional[float] = None) -> None:
    row = {m: int(metrics.get(m) or 0) for m in METRICS}
    conn.execute(
        "INSERT OR REPLACE INTO snapshots (post_id, ts, reactions, comments, shares, views) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(post_id), ts or time.time(), row["reactions"], row["comments"], row["shares"], row["views"]),
    )


def posts_since(conn: sqlite3.Connection, since_ts: float,
                page_urls: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Posts created (or, if creation time is unknown, first seen) after since_ts, optionally from given pages."""
    sql = "SELECT * FROM posts WHERE COALESCE(created_at, first_seen) >= ?"
    params: List[Any] = [since_ts]
    if page_urls is not None:
        sql += f" AND source IN ({','.join('?' * len(page_urls))})"
        params.extend(page_urls)
    rows = conn.execute(sql + " ORDER BY created_at DESC", params).fetchall()
    return [dict(r) for r in rows]


def tracked_posts(conn: sqlite3.Connection, since_ts: float,
                  page_urls: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Recent posts still worth re-measuring, newest first (optionally only from the given page URLs)."""
    sql = "SELECT post_id, url, kind FROM posts WHERE COALESCE(created_at, first_seen) >= ?"
    params: List[Any] = [since_ts]
    if page_urls is not None:
        sql += f" AND source IN ({','.join('?' * len(page_urls))})"
        params.extend(page_urls)
    rows = conn.execute(sql + " ORDER BY COALESCE(created_at, first_seen) DESC", params).fetchall()
    return [dict(r) for r in rows]


def snapshots_for(conn: sqlite3.Connection, post_ids: Iterable[str]) -> Dict[str, List[Dict[str, Any]]]:
    ids = list(post_ids)
    out: Dict[str, List[Dict[str, Any]]] = {i: [] for i in ids}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT * FROM snapshots WHERE post_id IN ({marks}) ORDER BY post_id, ts", chunk
        ).fetchall()
        for r in rows:
            out[r["post_id"]].append(dict(r))
    return out


def page_activity(conn: sqlite3.Connection, page_urls: List[str]) -> Dict[str, Dict[str, Any]]:
    """Per page: how many posts were ever crawled, the newest post time and the page name."""
    if not page_urls:
        return {}
    marks = ",".join("?" * len(page_urls))
    rows = conn.execute(
        f"SELECT source, COUNT(*) AS count, MAX(created_at) AS latest, MAX(page_name) AS name "
        f"FROM posts WHERE source IN ({marks}) GROUP BY source",
        page_urls,
    ).fetchall()
    return {r["source"]: {"count": r["count"], "latest": r["latest"], "name": r["name"]} for r in rows}


def set_topic(conn: sqlite3.Connection, post_id: str, is_story: bool, story_title: Optional[str], source: str) -> None:
    conn.execute(
        "UPDATE posts SET is_story = ?, story_title = COALESCE(?, story_title), topic_source = ? WHERE post_id = ?",
        (1 if is_story else 0, story_title or None, source, str(post_id)),
    )


def record_run(conn: sqlite3.Connection, started: float, sources: int, posts: int, error: str = "",
               mode: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO crawl_runs (ts, duration, sources, posts, error, mode) VALUES (?, ?, ?, ?, ?, ?)",
        (started, time.time() - started, sources, posts, error or None, mode or None),
    )


def last_runs(conn: sqlite3.Connection, limit: int = 5) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM crawl_runs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
