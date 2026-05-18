#!/usr/bin/env python3
"""Build static site for GitHub Pages (landing + studio UI)."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PAGES_SITE = ROOT / "pages_site"
ROOT_INDEX = ROOT / "index.html"
ROOT_NOJEKYLL = ROOT / ".nojekyll"


def pages_base_path() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "lamtrang2405/reels").strip()
    name = repo.split("/")[-1] if "/" in repo else repo
    return f"/{name}/"


def _debug_log(message: str, data: dict, hypothesis_id: str) -> None:
    # #region agent log
    try:
        import json
        import time

        line = json.dumps(
            {
                "sessionId": "0aa677",
                "hypothesisId": hypothesis_id,
                "location": "build_github_pages.py",
                "message": message,
                "data": data,
                "timestamp": int(time.time() * 1000),
            }
        )
        log_path = ROOT / ".cursor" / "debug-0aa677.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    # #endregion


def main() -> int:
    base = os.environ.get("PAGES_BASE", pages_base_path()).strip()
    if not base.endswith("/"):
        base += "/"

    api_base = os.environ.get("REELS_API_BASE", "").strip().rstrip("/")

    env = Environment(
        loader=FileSystemLoader(str(ROOT / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )
    html = env.get_template("landing.html").render(
        base=base,
        api_base=api_base,
    )

    def write_site(target: Path) -> None:
        if target.exists():
            shutil.rmtree(target)
        (target / "static").mkdir(parents=True)
        (target / "index.html").write_text(html, encoding="utf-8")
        (target / ".nojekyll").touch()
        shutil.copy2(ROOT / "static" / "styles.css", target / "static" / "styles.css")
        shutil.copy2(ROOT / "static" / "app.js", target / "static" / "app.js")

    write_site(DOCS)
    write_site(PAGES_SITE)

    # Branch deploy from / (root): Jekyll would render README without index + .nojekyll.
    ROOT_INDEX.write_text(html, encoding="utf-8")
    ROOT_NOJEKYLL.touch()

    print(f"Built {DOCS}/index.html")
    print(f"Built {PAGES_SITE}/ (Actions artifact)")
    print(f"Built {ROOT_INDEX} + {ROOT_NOJEKYLL}")
    api_label = api_base if api_base else "(same origin — local only)"
    print(f"  base={base!r} api_base={api_label!r}")
    _debug_log(
        "build_ok",
        {"base": base, "api_base": api_base, "pages_site": str(PAGES_SITE)},
        "A",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        _debug_log("build_failed", {"error": str(exc)}, "A")
        raise
