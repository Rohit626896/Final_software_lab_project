"""arXiv Atom client; importing this module never starts a request."""

import re

import feedparser

from src.paper.provider_http import provider_get

ARXIV_API = "https://export.arxiv.org/api/query"


def search_arxiv(query, max_results=10):
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
    response = provider_get(
        "arXiv",
        ARXIV_API,
        interval=3,
        params=params,
        timeout=(5, 15),
        headers={"User-Agent": "PaperLens/1.1 (academic resource explorer)"},
    )
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
