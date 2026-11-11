"""The documentation space. Pages are Markdown files in app/docs, readable by anyone signed in."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .deps import User

router = APIRouter(prefix="/api/docs")
DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"

# The order of the navigation. Each page is app/docs/<slug>.md, and its first line is "# Title".
SECTIONS: list[tuple[str, list[str]]] = [
    ("Get started", ["introduction", "whats-new", "quick-start", "customer-setup", "concepts"]),
    ("Use the platform", ["overview-page", "investigations", "projects-and-reports", "runbooks", "dependency-map", "alerts-and-webhooks", "logs-and-audit"]),
    ("Connections", ["connections", "prometheus", "loki", "kubernetes", "cloud-sources", "finding-connection-details"]),
    ("AI models", ["ai-models", "accuracy-benchmark"]),
    ("Deploy", ["deploy-localhost", "deploy-azure-vm", "deploy-aks", "certificates", "configuration", "operations"]),
    ("Access and security", ["roles-and-permissions", "single-sign-on", "security", "legal-and-privacy"]),
    ("Help", ["troubleshooting", "about"]),
]
SLUG_RE = re.compile(r"^[a-z0-9-]{1,60}$")


@lru_cache(maxsize=1)
def _pages() -> dict[str, dict[str, Any]]:
    """Reads every page once. The files ship inside the image and never change while it runs."""
    pages: dict[str, dict[str, Any]] = {}
    order = [(section, slug) for section, slugs in SECTIONS for slug in slugs]
    for index, (section, slug) in enumerate(order):
        text = (DOCS_DIR / f"{slug}.md").read_text(encoding="utf-8")
        first, _, body = text.partition("\n")
        pages[slug] = {
            "slug": slug, "section": section, "title": first.removeprefix("# ").strip(), "markdown": body.lstrip("\n"),
            "prev": order[index - 1][1] if index else None, "next": order[index + 1][1] if index + 1 < len(order) else None,
        }
    return pages


def _plain(markdown: str) -> str:
    """Text to search. Code blocks stay, because people search for commands and error messages."""
    text = re.sub(r"```[a-z]*", " ", markdown)
    text = re.sub(r"\]\([^)]*\)", " ", text)
    text = re.sub(r"[`*#>|\[\]]+|(?<!\w)_|_(?!\w)|-{3,}", " ", text)
    return re.sub(r"\s+", " ", text)


@router.get("")
async def list_docs(_: User) -> dict[str, Any]:
    pages = _pages()
    return {"sections": [{"title": section, "pages": [{"slug": slug, "title": pages[slug]["title"]} for slug in slugs]} for section, slugs in SECTIONS]}


@router.get("/search")
async def search_docs(_: User, q: str = Query("", max_length=80)) -> dict[str, Any]:
    words = [word for word in re.findall(r"[\w.=~!-]+", q.lower()) if len(word) > 1][:6]
    if not words:
        return {"results": []}
    results = []
    for page in _pages().values():
        title, plain = page["title"].lower(), _plain(page["markdown"])
        lowered = plain.lower()
        if not all(word in title or word in lowered for word in words):
            continue
        score = sum(5 for word in words if word in title) + sum(lowered.count(word) for word in words)
        # Show the whole phrase when the page has it, otherwise the first word.
        at = lowered.find(q.lower().strip())
        at = at if at >= 0 else lowered.find(words[0])
        start = plain.rfind(" ", 0, max(0, at - 50)) + 1 if at > 50 else 0
        end = plain.find(" ", at + 140)
        snippet = plain[start: end if end > 0 else None].strip() if at >= 0 else plain[:200]
        results.append({"slug": page["slug"], "title": page["title"], "section": page["section"], "snippet": snippet, "score": score})
    results.sort(key=lambda item: -item["score"])
    return {"results": [{key: value for key, value in item.items() if key != "score"} for item in results[:12]]}


@router.get("/{slug}")
async def get_doc(slug: str, _: User) -> dict[str, Any]:
    page = _pages().get(slug) if SLUG_RE.match(slug) else None
    if not page:
        raise HTTPException(404, "There's no documentation page with that name.")
    pages = _pages()
    neighbour = lambda key: {"slug": page[key], "title": pages[page[key]]["title"]} if page[key] else None  # noqa: E731
    return {"slug": slug, "title": page["title"], "section": page["section"], "markdown": page["markdown"], "prev": neighbour("prev"), "next": neighbour("next")}
