"""Reproducible browser smoke test using a temporary DB and simulated providers.

Run: PLAYWRIGHT_BROWSERS_PATH=.cache/ms-playwright .venv/bin/python -m scripts.browser_check
Screenshots: .cache/verification/. No fixture data enters your real library.
"""

import logging
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pymupdf
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from app import create_app


def fixture_pdf():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((48, 60), "A reproducible vision benchmark", fontsize=18)
    for y, line in enumerate(
        [
            "Source code: https://github.com/paperlens/vision-example",
            "Dataset: https://kaggle.com/datasets/paperlens/vision",
            "Documentation: https://docs.example.org/vision",
            "Supplementary: https://example.org/supplementary.pdf",
        ],
        1,
    ):
        page.insert_text((48, 80 + y * 28), line, fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


def fixture_check(resource):
    status = {
        "code": "accessible",
        "dataset": "accessible",
        "documentation": "restricted",
        "supplementary": "missing",
    }.get(resource["category"], "unresolved")
    code = {"accessible": 200, "restricted": 403, "missing": 404, "unresolved": 503}[status]
    return {
        **resource,
        "status": status,
        "http_status": code,
        "final_url": resource["url"],
        "checked_at": "2026-11-02T10:00:00+00:00",
        "note": "Simulated response for browser verification.",
    }


def fixture_discover(query, source):
    return [
        {
            "title": "Vision benchmark: methods and evidence",
            "authors": ["Ada Example", "Sam Researcher"],
            "abstract": "A test-only discovery result for browser verification.",
            "source": "arXiv",
            "arxiv_id": "2401.00001",
            "year": 2024,
            "area": "cs.CV",
            "paper_url": "https://arxiv.org/abs/2401.00001",
            "pdf_url": None,
        }
    ], []


def main():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    screenshots = Path(".cache/verification")
    screenshots.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory() as temp,
        patch("src.analysis.check_resource", side_effect=fixture_check),
        patch("app.routes.discover", side_effect=fixture_discover),
    ):
        root = Path(temp)
        app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(root / "browser.db"),
                "UPLOAD_FOLDER": str(root / "uploads"),
                "SECRET_KEY": "browser-test",
            }
        )
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1000}, device_scale_factor=1
                )
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base)
                expect(page.get_by_role("heading", name="Great research deserves")).to_be_visible()
                page.screenshot(path=str(screenshots / "home-desktop.png"), full_page=True)
                page.locator("#pdf").set_input_files(
                    {
                        "name": "benchmark.pdf",
                        "mimeType": "application/pdf",
                        "buffer": fixture_pdf(),
                    }
                )
                page.locator("#upload-title").fill("A reproducible vision benchmark")
                page.locator("#upload-area").fill("Computer vision")
                page.locator("#upload-year").fill("2024")
                page.get_by_role("button", name="Add PDF to library").click()
                expect(
                    page.get_by_role("heading", name="A reproducible vision benchmark", exact=True)
                ).to_be_visible()
                paper_url = page.url
                home_tab = page.context.new_page()
                home_tab.on("pageerror", lambda error: errors.append(str(error)))
                home_tab.goto(base)
                expect(home_tab.locator('[data-stat="total"]')).to_have_text("1")
                expect(home_tab.locator('[data-stat="analyzed"]')).to_have_text("0")
                page.bring_to_front()
                page.get_by_role("button", name="Start analysis").click()
                expect(page.locator(".score-ring strong")).to_have_text("70", timeout=20000)
                expect(page.locator(".resource-card")).to_have_count(4)
                home_tab.bring_to_front()
                expect(home_tab.locator('[data-stat="analyzed"]')).to_have_text("1", timeout=10000)
                home_tab.close()
                page.bring_to_front()
                page.screenshot(path=str(screenshots / "report-desktop.png"), full_page=True)
                page.locator("#resource-filter").select_option("code")
                expect(page.locator(".resource-card:visible")).to_have_count(1)
                page.locator("#resource-filter").select_option("")
                with page.expect_download() as download:
                    page.get_by_role("link", name="Download report").click()
                assert download.value.suggested_filename.endswith(".json")
                for width in (320, 390, 768, 1024, 1440):
                    page.set_viewport_size({"width": width, "height": 900})
                    for path in (
                        "/",
                        paper_url,
                        "/library",
                        "/analytics",
                        "/methodology",
                        "/search",
                    ):
                        page.goto(path if path.startswith("http") else base + path)
                        page.wait_for_load_state("networkidle")
                        overflow = page.evaluate(
                            "document.documentElement.scrollWidth > window.innerWidth"
                        )
                        assert not overflow, f"Horizontal overflow: {width}px {path}"
                    if width == 390:
                        page.goto(base)
                        page.screenshot(path=str(screenshots / "home-mobile.png"), full_page=True)
                        page.goto(paper_url)
                        page.screenshot(path=str(screenshots / "report-mobile.png"), full_page=True)
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.goto(base + "/analytics")
                expect(page.locator(".js-plotly-plot")).to_have_count(2)
                page.screenshot(path=str(screenshots / "analytics-desktop.png"), full_page=True)
                page.goto(base + "/search")
                page.locator("#search-query").fill("computer vision")
                page.get_by_role("button", name="Search papers").click()
                expect(page.locator(".paper-card")).to_have_count(1)
                page.goto(base + "/library?min_score=71")
                expect(page.locator(".paper-card")).to_have_count(0)
                page.goto(base + "/library?min_score=70")
                expect(page.locator(".paper-card")).to_have_count(1)
                assert not errors, errors
                browser.close()
                print(
                    "PASS: upload → background analysis → score → resource filter → report download"
                )
                print("PASS: provider search, library filtering, interactive analytics")
                print("PASS: six pages without horizontal overflow at 320, 390, 768, 1024, 1440px")
                print("PASS: report count updates in an already-open homepage tab")
                print("PASS: no browser JavaScript errors")
                print("Screenshots saved to .cache/verification/")
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
