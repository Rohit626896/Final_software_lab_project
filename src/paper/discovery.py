"""Open-access discovery with persistent caching, cooldowns, and labeled fallbacks."""

import hashlib
import html
import json
import re
import time
import uuid
from urllib.parse import urlsplit

import requests
from flask import current_app
from src.paper.arxiv_client import search_arxiv
from src.paper.provider_http import ProviderRateLimit, provider_get
from src.database.db import connection, list_papers


def identity(paper):
    if paper.get("upload_hash"):
        return "upload:" + paper["upload_hash"]
    if paper.get("arxiv_id"):
        return "arxiv:" + re.sub(r"v\d+$", "", paper["arxiv_id"])
    url = paper.get("paper_url") or ""
    if "arxiv.org/abs/" in url:
        return "arxiv:" + re.sub(r"v\d+$", "", url.split("/abs/")[-1])
    external_id = paper.get("external_id") or paper.get("paper_id")
    if external_id:
        provider_key = paper.get("source", "").casefold() + ":" + str(external_id)
        return "provider:" + hashlib.sha256(provider_key.encode()).hexdigest()
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
    response = provider_get(
        "Semantic Scholar",
        "https://api.semanticscholar.org/graph/v1/paper/search",
        interval=1.1 if key else 3,
        params={
            "query": query,
            "limit": limit,
            "openAccessPdf": "",
            "fields": "title,authors,abstract,year,url,openAccessPdf,externalIds,fieldsOfStudy",
        },
        headers=headers,
        timeout=(5, 15),
    )
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


def search_europe_pmc(query, limit=10):
    """Key-free fallback for open-access life-science research with an explicit PDF link."""
    terms = re.findall(r"[\w.-]+", query, flags=re.UNICODE)
    if not terms:
        return []
    response = provider_get(
        "Europe PMC",
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        interval=3,
        params={
            "query": "(" + " AND ".join('"' + term + '"' for term in terms) + ") AND OPEN_ACCESS:Y",
            "format": "json",
            "resultType": "core",
            "pageSize": limit,
        },
        timeout=(5, 15),
    )
    papers = []
    for entry in response.json().get("resultList", {}).get("result", []):
        if entry.get("isOpenAccess") != "Y" or not entry.get("title"):
            continue
        links = (entry.get("fullTextUrlList") or {}).get("fullTextUrl", [])
        pdf = next(
            (
                public_url(link.get("url"))
                for link in links
                if link.get("documentStyle") == "pdf"
                and link.get("availabilityCode") == "OA"
                and public_url(link.get("url"))
            ),
            None,
        )
        if not pdf:
            continue
        landing = next(
            (
                public_url(link.get("url"))
                for link in links
                if link.get("documentStyle") == "html"
                and link.get("availabilityCode") == "OA"
                and public_url(link.get("url"))
            ),
            pdf,
        )
        year = str(entry.get("pubYear") or "")
        papers.append(
            {
                "paper_id": entry.get("id"),
                "title": html.unescape(entry["title"]),
                "authors": [
                    author.get("fullName", "")
                    for author in (entry.get("authorList") or {}).get("author", [])
                ],
                "abstract": html.unescape(re.sub(r"<[^>]+>", " ", entry.get("abstractText") or "")),
                "year": int(year) if year.isdigit() else None,
                "area": "Life sciences",
                "paper_url": landing,
                "pdf_url": pdf,
                "source": "Europe PMC",
            }
        )
    return papers


def _cached(provider, query):
    with connection() as db:
        row = db.execute(
            "SELECT fetched_at,results FROM discovery_cache WHERE provider=? AND query=?",
            (provider, query),
        ).fetchone()
    if row is None:
        return None
    try:
        results = json.loads(row["results"])
        if not isinstance(results, list):
            return None
        return row["fetched_at"], results
    except (ValueError, TypeError):
        return None


def _claim(provider):
    """Coordinate workers without holding a database transaction during HTTP requests."""
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO discovery_provider_state(provider) VALUES (?)", (provider,)
        )
        state = db.execute(
            "SELECT * FROM discovery_provider_state WHERE provider=?", (provider,)
        ).fetchone()
        now = time.time()
        if state["retry_at"] > now:
            return None, state["retry_at"] - now
        if state["lease_until"] > now:
            return None, None
        token = uuid.uuid4().hex
        db.execute(
            "UPDATE discovery_provider_state SET lease_until=?,lease_token=? WHERE provider=?",
            (now + 120, token, provider),
        )
        return token, 0


def _release(provider, token, cooldown=0):
    spacing = (
        1.1
        if provider == "Semantic Scholar" and current_app.config.get("SEMANTIC_SCHOLAR_API_KEY")
        else 3
    )
    with connection() as db:
        db.execute(
            """UPDATE discovery_provider_state SET lease_until=?,lease_token='',retry_at=?
               WHERE provider=? AND lease_token=?""",
            (time.time() + spacing, time.time() + cooldown, provider, token),
        )


def _search_provider(provider, query, fetch):
    key = " ".join(query.casefold().split())
    deadline = time.monotonic() + 45
    token = None
    while token is None:
        cached = _cached(provider, key)
        fresh_for = current_app.config["DISCOVERY_CACHE_TTL"]
        if cached and not cached[1]:
            fresh_for = min(fresh_for, 300)
        if cached and time.time() - cached[0] < fresh_for:
            return cached[1], None, False
        token, cooldown = _claim(provider)
        if token:
            break
        if cooldown is not None:
            reason = f"is temporarily busy; the next live attempt is in {max(1, int(cooldown) + 1)} seconds"
            break
        if time.monotonic() >= deadline:
            reason = "already has a search in progress"
            break
        time.sleep(0.1)
    if token:
        cooldown = 0
        try:
            # Another worker can finish between our cache read and lease acquisition.
            latest = _cached(provider, key)
            ttl = current_app.config["DISCOVERY_CACHE_TTL"]
            if latest and not latest[1]:
                ttl = min(ttl, 300)
            if latest and time.time() - latest[0] < ttl:
                return latest[1], None, False
            results = fetch(query, 10)
            with connection() as db:
                db.execute(
                    "INSERT OR REPLACE INTO discovery_cache VALUES (?,?,?,?)",
                    (provider, key, time.time(), json.dumps(results)),
                )
                # Bound storage while retaining popular successful searches across restarts.
                db.execute("""DELETE FROM discovery_cache WHERE rowid NOT IN
                       (SELECT rowid FROM discovery_cache ORDER BY fetched_at DESC LIMIT 1000)""")
            return results, None, False
        except ProviderRateLimit as error:
            cooldown = error.retry_after
            reason = "is temporarily busy"
            current_app.logger.warning("Discovery throttled for %s; cooling down", provider)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            cooldown = 15
            reason = "is temporarily unavailable"
            current_app.logger.exception("Discovery failed for %s", provider)
        finally:
            _release(provider, token, cooldown)
    if (
        cached
        and cached[1]
        and time.time() - cached[0] <= current_app.config["DISCOVERY_STALE_TTL"]
    ):
        return cached[1], f"{provider} {reason}. Showing previously cached results.", False
    return [], f"{provider} {reason}.", True


def discover(query, source="arXiv"):
    providers = {"arXiv": search_arxiv, "Semantic Scholar": search_semantic}
    if source not in (*providers, "both"):
        raise ValueError("Unknown discovery source.")
    query = " ".join(query.split())
    if not query:
        return [], []
    selected = list(providers) if source == "both" else [source]
    papers, notices, seen, titles = [], [], set(), set()
    failed = False

    def collect(results):
        for paper in results:
            key = identity(paper)
            title = re.sub(r"\W", "", paper["title"].casefold())
            if key not in seen and title not in titles:
                seen.add(key)
                titles.add(title)
                papers.append(paper)

    for name in selected:
        results, notice, unavailable = _search_provider(name, query, providers[name])
        collect(results)
        failed = failed or unavailable
        if notice:
            notices.append(notice)
    if failed and len(selected) == 1 and not papers:
        alternate = next(name for name in providers if name not in selected)
        results, notice, _ = _search_provider(alternate, query, providers[alternate])
        collect(results)
        if papers:
            notices.append(f"Showing results from {alternate} instead.")
        if notice:
            notices.append(notice)
    if failed and not papers:
        results, notice, _ = _search_provider("Europe PMC", query, search_europe_pmc)
        collect(results)
        if papers:
            notices.append(
                "Showing open-access results from Europe PMC, a life-science research source."
            )
        if notice:
            notices.append(notice)
    if failed and not papers:
        collect(list_papers({"q": query})[:10])
        if papers:
            notices.append("Showing matching papers already saved in your library.")
    return papers, notices
