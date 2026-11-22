"""Bounded PDF text, annotation, and plain-text URL extraction."""

from pathlib import Path
import pymupdf as fitz

from src.parser import classify, links_from_text, normalize_url


def extract_paper(pdf_path, max_pages=300):
    """Read PDF text and links, keeping page numbers and nearby evidence."""
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError("Please provide a PDF file.")
    with fitz.open(pdf_path) as document:
        if document.needs_pass:
            raise ValueError("Password-protected PDFs are not supported.")
        if not document.is_pdf:
            raise ValueError("The file is not a PDF.")
        if len(document) > max_pages:
            raise ValueError(f"PDF exceeds the {max_pages}-page analysis limit.")
        if len(document) == 0:
            raise ValueError("The PDF has no pages.")
        text_parts, resources, text_size = [], {}, 0
        for number, page in enumerate(document, 1):
            text = page.get_text("text")
            text_size += len(text)
            if text_size > 3_000_000:
                raise ValueError("PDF text exceeds the analysis limit.")
            text_parts.append(text)
            for resource in links_from_text(text, number):
                resources.setdefault(resource["url"], resource)
            for annotation in page.get_links():
                url = normalize_url(annotation.get("uri") or "")
                if not url or url in resources:
                    continue
                nearby = " ".join(
                    page.get_text("text", clip=annotation["from"] + (-60, -20, 60, 20)).split()
                )
                resources[url] = {
                    "url": url,
                    "category": classify(url, nearby),
                    "evidence": nearby[:500] or "Embedded PDF hyperlink",
                    "page": number,
                }
            if len(resources) > 2000:
                raise ValueError("PDF contains too many links to analyze.")
        text = "\n".join(text_parts)
        return {
            "filename": pdf_path.name,
            "pages": len(document),
            "text": text,
            "links": sorted(resources),
            "resources": list(resources.values()),
            "warnings": (
                ["Little readable text was found. Scanned PDFs need OCR, which is not included."]
                if len(text.strip()) < 100
                else []
            ),
        }
