"""Page Analyzer UI: paste Facebook page links -> crawl (background job) -> post performance per page."""

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from agents.hub import _require_token
from trending import store
from trending.crawler import config_path, crawl, normalize_page_url
from trending.scoring import page_performance

MAX_PAGES = 30
LOG_LINES = 200

router = APIRouter(dependencies=[Depends(_require_token)])

_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_running: Dict[str, Optional[str]] = {"id": None}


class AnalyzeBody(BaseModel):
    links: str = Field(..., max_length=20000)
    period_hours: int = Field(72, ge=6, le=24 * 30)
    reels: bool = True


class ResultsBody(BaseModel):
    links: str = Field(..., max_length=20000)
    period_hours: int = Field(72, ge=6, le=24 * 30)


def _parse_links(text: str) -> Dict[str, List[str]]:
    valid: List[str] = []
    invalid: List[str] = []
    for raw in re.split(r"[\s,;]+", text or ""):
        if not raw.strip():
            continue
        url = normalize_page_url(raw)
        if url is None:
            invalid.append(raw)
        elif url not in valid:
            valid.append(url)
    return {"valid": valid, "invalid": invalid}


def _load_cfg() -> Dict[str, Any]:
    path = config_path()
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    example = Path(__file__).resolve().parent / "pages.example.json"
    cfg = json.loads(example.read_text(encoding="utf-8"))
    cfg["pages"] = []
    return cfg


def _save_pages(urls: List[str], reels: bool) -> None:
    """The analyzed list becomes the tracked list (pages.json), keeping names of pages already there."""
    cfg = _load_cfg()
    known = {}
    for entry in cfg.get("pages") or []:
        entry = {"url": entry} if isinstance(entry, str) else entry
        url = normalize_page_url(entry.get("url", ""))
        if url:
            known[url] = entry
    cfg["pages"] = [{**known.get(u, {}), "url": u, "reels": reels} for u in urls]
    config_path().write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _tracked_urls() -> List[str]:
    try:
        cfg = _load_cfg()
    except (OSError, json.JSONDecodeError):
        return []
    urls = []
    for entry in cfg.get("pages") or []:
        url = normalize_page_url(entry if isinstance(entry, str) else entry.get("url", ""))
        if url and url not in urls:
            urls.append(url)
    return urls


def _results(urls: List[str], period_hours: int) -> Dict[str, Any]:
    conn = store.connect()
    try:
        return page_performance(conn, urls, max_age_hours=period_hours)
    finally:
        conn.close()


def _run(job: Dict[str, Any], urls: List[str], period_hours: int, reels: bool) -> None:
    def log(msg: str) -> None:
        job["log"].append(msg)
        del job["log"][:-LOG_LINES]

    try:
        crawl_result = crawl(pages=urls, reels=reels, max_pages=len(urls), max_posts_per_page=30,
                             max_age_hours=period_hours, search=False, log=log)
        job["crawl"] = crawl_result
        job["result"] = _results(urls, period_hours)
        job["status"] = "done" if crawl_result.get("ok") else "blocked"
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e)[:500]
        log(f"Lỗi: {job['error']}")
    finally:
        job["finished_at"] = time.time()
        with _jobs_lock:
            _running["id"] = None


def register(app, templates, landing_ctx: dict) -> None:
    @app.get("/trending", response_class=HTMLResponse)
    def trending_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse("trending.html", {"request": request, **landing_ctx})

    app.include_router(router)


@router.get("/api/trending/pages")
def tracked_pages() -> JSONResponse:
    return JSONResponse({
        "pages": _tracked_urls(),
        "mode": "cookie" if os.environ.get("FB_COOKIES_FILE", "").strip() else "guest",
        "running": _running["id"],
    })


@router.post("/api/trending/analyze")
def analyze(body: AnalyzeBody) -> JSONResponse:
    links = _parse_links(body.links)
    urls = links["valid"]
    if not urls:
        raise HTTPException(status_code=400, detail="Không có link page Facebook hợp lệ nào.")
    if len(urls) > MAX_PAGES:
        raise HTTPException(status_code=400, detail=f"Tối đa {MAX_PAGES} page mỗi lần phân tích.")
    with _jobs_lock:
        if _running["id"]:
            return JSONResponse({"job_id": _running["id"], "already_running": True, "invalid": links["invalid"]})
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "status": "running", "started_at": time.time(), "finished_at": None,
               "pages": urls, "period_hours": body.period_hours, "log": [], "crawl": None, "result": None,
               "error": None}
        _jobs[job_id] = job
        _running["id"] = job_id
    _save_pages(urls, body.reels)
    threading.Thread(target=_run, args=(job, urls, body.period_hours, body.reels), daemon=True).start()
    return JSONResponse({"job_id": job_id, "pages": urls, "invalid": links["invalid"]})


@router.get("/api/trending/jobs/{job_id}")
def job_status(job_id: str) -> JSONResponse:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Không có job này (server có thể đã khởi động lại).")
    end = job["finished_at"] or time.time()
    return JSONResponse({**job, "log": job["log"][-40:], "elapsed_s": round(end - job["started_at"])})


@router.post("/api/trending/results")
def stored_results(body: ResultsBody) -> JSONResponse:
    """Re-score stored data (no crawl) for the given pages."""
    urls = _parse_links(body.links)["valid"]
    if not urls:
        raise HTTPException(status_code=400, detail="Không có link page Facebook hợp lệ nào.")
    return JSONResponse(_results(urls, body.period_hours))
