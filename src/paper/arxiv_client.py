import requests
import feedparser

ARXIV_API = "https://export.arxiv.org/api/query"


def search_arxiv(query, max_results=10):
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }

    response = requests.get(
        ARXIV_API,
        params=params,
        timeout=30
    )
    response.raise_for_status()

    feed = feedparser.parse(response.text)
    papers = []

    for entry in feed.entries:
        paper = {
            "paper_id": entry.id.split("/abs/")[-1],
            "title": " ".join(entry.title.split()),
            "authors": [
                author.name for author in entry.authors
            ],
            "abstract": " ".join(entry.summary.split()),
            "published": entry.get("published", ""),
            "year": (
                int(entry.published[:4])
                if entry.get("published")
                else None
            ),
            "paper_url": entry.link,
            "pdf_url": None,
            "source": "arXiv",
        }

        for link in entry.get("links", []):
            if link.get("title") == "pdf":
                paper["pdf_url"] = link.href

        papers.append(paper)

    return papers
query = "computer vision"

papers = search_arxiv(query, max_results=3)

print(f"Found {len(papers)} papers\n")

for paper in papers:
       print("Title:", paper["title"])
       print("Authors:", ", ".join(paper["authors"]))
       print("Year:", paper["year"])
       print("PDF URL:", paper["pdf_url"])
        