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


def pages_base_path() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "lamtrang2405/reels").strip()
    name = repo.split("/")[-1] if "/" in repo else repo
    return f"/{name}/"


def main() -> int:
    base = os.environ.get("PAGES_BASE", pages_base_path()).strip()
    if not base.endswith("/"):
        base += "/"

    default_api = "https://reels-api.onrender.com"
    api_base = os.environ.get("REELS_API_BASE", default_api).strip().rstrip("/") or default_api

    env = Environment(
        loader=FileSystemLoader(str(ROOT / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )
    html = env.get_template("landing.html").render(
        base=base,
        api_base=api_base,
    )

    if DOCS.exists():
        shutil.rmtree(DOCS)
    (DOCS / "static").mkdir(parents=True)
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    (DOCS / ".nojekyll").touch()
    shutil.copy2(ROOT / "static" / "styles.css", DOCS / "static" / "styles.css")
    shutil.copy2(ROOT / "static" / "app.js", DOCS / "static" / "app.js")

    print(f"Built {DOCS}/index.html")
    api_label = api_base if api_base else "(same origin — local only)"
    print(f"  base={base!r} api_base={api_label!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
