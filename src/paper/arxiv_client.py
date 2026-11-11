"""arXiv Atom client; importing this module never starts a request."""

import re
import threading
import time

import feedparser
import requests

ARXIV_API = "https://export.arxiv.org/api/query"
_lock = threading.Lock()
_last_request = 0.0


def search_arxiv(query, max_results=10):
    global _last_request
    # Treat the UI query as terms, rather than letting it alter the API grammar.
    terms = re.findall(r"[\w.-]+", query, flags=re.UNICODE)
    if not terms:
        return []
    params = {
        "search_query": " AND ".join('all:"' + term + '"' for term in terms),
        "start": 0,
        "max_results": min(max(int(max_results), 1), 20),
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    with _lock:
        time.sleep(max(0, 3 - (time.monotonic() - _last_request)))
        try:
            response = requests.get(
                ARXIV_API,
                params=params,
                timeout=(8, 25),
                headers={"User-Agent": "PaperLens/1.0 (academic resource explorer)"},
            )
            response.raise_for_status()
        finally:
            _last_request = time.monotonic()
    feed = feedparser.parse(response.content)
    if feed.bozo and not feed.entries:
        raise ValueError("arXiv returned an unreadable response. Please try again later.")
    papers = []
    for entry in feed.entries:
        if "/api/errors" in entry.get("id", ""):
            raise ValueError("arXiv could not process this search.")
        if not entry.get("title") or not entry.get("id"):
            continue
        paper_id = entry.id.split("/abs/")[-1]
        published = entry.get("published", "")
        pdf_url = next(
            (
                link.get("href")
                for link in entry.get("links", [])
                if link.get("type") == "application/pdf" or link.get("title") == "pdf"
            ),
            None,
        )
        papers.append(
            {
                "paper_id": paper_id,
                "title": " ".join(entry.title.split()),
                "authors": [a.get("name", "") for a in entry.get("authors", [])],
                "abstract": " ".join(entry.get("summary", "").split()),
                "published": published,
                "year": int(published[:4]) if published[:4].isdigit() else None,
                "area": (entry.get("arxiv_primary_category") or {}).get("term", "Uncategorized"),
                "paper_url": "https://arxiv.org/abs/" + paper_id,
                "pdf_url": pdf_url.replace("http://", "https://", 1) if pdf_url else None,
                "arxiv_id": re.sub(r"v\d+$", "", paper_id),
                "source": "arXiv",
            }
        )
    return papers


if __name__ == "__main__":
    for paper in search_arxiv("computer vision", 3):
        print(paper["title"], paper["pdf_url"])
