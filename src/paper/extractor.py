
from pathlib import Path
import fitz


def extract_paper(pdf_path):
    pdf_path = Path(pdf_path)

    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError("Please provide a PDF file.")

    document = fitz.open(pdf_path)

    try:
        if document.needs_pass:
            raise ValueError("Password-protected PDF is not supported.")

        text_parts = []
        links = set()

        for page in document:
            # Extract readable text from each page
            page_text = page.get_text("text")
            text_parts.append(page_text)

            # Extract embedded hyperlinks
            for link in page.get_links():
                uri = link.get("uri")
                if uri and uri.startswith(("http://", "https://")):
                    links.add(uri)

        return {
            "filename": pdf_path.name,
            "pages": len(document),
            "text": "\n".join(text_parts),
            "links": sorted(links)
        }

    finally:
        document.close()
