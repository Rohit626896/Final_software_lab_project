"""Open-access discovery, metadata normalization, and a small provider cache."""

import hashlib
import re
import threading
import time
from urllib.parse import urlsplit

import requests
from flask import current_app
from src.paper.arxiv_client import search_arxiv

_cache = {}
_cache_lock = threading.Lock()


def identity(paper):
    if paper.get("upload_hash"):
        return "upload:" + paper["upload_hash"]
    if paper.get("arxiv_id"):
        return "arxiv:" + re.sub(r"v\d+$", "", paper["arxiv_id"])
    url = paper.get("paper_url") or ""
    if "arxiv.org/abs/" in url:
        return "arxiv:" + re.sub(r"v\d+$", "", url.split("/abs/")[-1])
    # Cross-provider deduplication fallback when identifiers are absent.
    title = re.sub(r"[^\w]", "", paper["title"].casefold())
    return "title:" + hashlib.sha256(title.encode()).hexdigest()


def public_url(value):
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ("http", "https") and parsed.hostname else None
    except ValueError:
        return None


def search_semantic(query, limit=10):
    key = current_app.config.get("SEMANTIC_SCHOLAR_API_KEY")
    headers = {"x-api-key": key} if key else {}
    response = requests.get(
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params={
            "query": query,
            "limit": limit,
            "openAccessPdf": "",
            "fields": "title,authors,abstract,year,url,openAccessPdf,externalIds,fieldsOfStudy",
        },
        headers=headers,
        timeout=(8, 25),
    )
    response.raise_for_status()
    papers = []
    for entry in response.json().get("data", []):
        pdf = public_url((entry.get("openAccessPdf") or {}).get("url"))
        if not pdf or not entry.get("title"):
            continue
        papers.append(
            {
                "paper_id": entry["paperId"],
                "title": entry["title"],
                "authors": [author.get("name", "") for author in entry.get("authors", [])],
                "abstract": entry.get("abstract") or "",
                "year": entry.get("year"),
                "area": next(iter(entry.get("fieldsOfStudy") or []), "Uncategorized"),
                "paper_url": public_url(entry.get("url")),
                "pdf_url": pdf,
                "arxiv_id": (entry.get("externalIds") or {}).get("ArXiv"),
                "source": "Semantic Scholar",
            }
        )
    return papers


def discover(query, source="arXiv"):
    providers = {"arXiv": search_arxiv, "Semantic Scholar": search_semantic}
    selected = list(providers) if source == "both" else [source]
    papers, errors, seen, titles = [], [], set(), set()
    for name in selected:
        cache_key = (name, query.casefold())
        with _cache_lock:
            cached = _cache.get(cache_key)
        try:
            if cached and time.monotonic() - cached[0] < 600:
                results = cached[1]
            else:
                results = providers[name](query, 10)
                with _cache_lock:
                    if len(_cache) >= 100:
                        _cache.pop(next(iter(_cache)))
                    _cache[cache_key] = (time.monotonic(), results)
            for paper in results:
                key = identity(paper)
                title = re.sub(r"\W", "", paper["title"].casefold())
                if key not in seen and title not in titles:
                    seen.add(key)
                    titles.add(title)
                    papers.append(paper)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            current_app.logger.exception("Discovery failed for %s", name)
            errors.append(
                name + " is unavailable or rate-limited. Try again later or use another source."
            )
    return papers, errors
