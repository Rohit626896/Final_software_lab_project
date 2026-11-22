"""Heuristic resource classification and text URL extraction."""

import re
from urllib.parse import urlsplit, urlunsplit

CATEGORIES = ("code", "dataset", "documentation", "supplementary", "other")
URL_PATTERN = re.compile(r"https?://[^\s<>\x00-\x1f\"\u201c\u201d]+", re.IGNORECASE)


def normalize_url(url):
    """Clean a detected web URL and discard unsupported or unsafe formats."""
    url = url.strip().rstrip(".,;:!?\u2019\u201d")
    while url.endswith((")", "]", "}")):
        opening = {")": "(", "]": "[", "}": "{"}[url[-1]]
        if url.count(url[-1]) > url.count(opening):
            url = url[:-1]
        else:
            break
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None or len(url) > 2048:
            return None
        return urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
        )
    except ValueError:
        return None


def classify(url, context=""):
    """Suggest a resource category using the URL and surrounding text."""
    parts = urlsplit(url)
    host, path = (parts.hostname or "").lower(), parts.path.lower()
    words = (path + " " + context).lower()
    if (
        (host == "huggingface.co" and path.startswith("/datasets/"))
        or any(
            token in host
            for token in ("kaggle.com", "openml.org", "dataverse", "data.mendeley.com")
        )
        or re.search(r"\b(dataset|datasets|data repository)\b", path)
    ):
        return "dataset"
    if (
        host.endswith("readthedocs.io")
        or host.startswith("docs.")
        or re.search(r"\b(documentation|readme|docs)\b", path)
    ):
        return "documentation"
    if any(
        host == domain or host.endswith("." + domain)
        for domain in ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")
    ):
        return "code"
    if re.search(r"\b(supplement|supplementary|appendix|supplemental)\b", words):
        return "supplementary"
    if any(
        host == domain or host.endswith("." + domain)
        for domain in ("zenodo.org", "figshare.com", "osf.io")
    ):
        if re.search(r"\b(data|dataset|datasets)\b", context.lower()):
            return "dataset"
        return "supplementary"
    if re.search(r"\b(dataset|datasets|training data)\b", context.lower()):
        return "dataset"
    if re.search(r"\b(source code|implementation|code repository)\b", context.lower()):
        return "code"
    if re.search(r"\b(documentation|tutorial)\b", context.lower()):
        return "documentation"
    return "other"


def links_from_text(text, page=None):
    """Find web URLs in text and attach their extraction context."""
    links = []
    for match in URL_PATTERN.finditer(text):
        url = normalize_url(match.group())
        if url:
            evidence = " ".join(text[max(0, match.start() - 100) : match.end() + 100].split())
            links.append(
                {
                    "url": url,
                    "category": classify(url, evidence),
                    "evidence": evidence[:500],
                    "page": page,
                }
            )
    return links
