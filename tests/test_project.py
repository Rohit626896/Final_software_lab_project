"""Offline regression tests: no provider calls or external resource checks."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pymupdf as fitz

from app import create_app
from src.checker import check_resource
from src.checker.network import UnsafeURL, fetch, validate_url
from src.database import connection, get_paper, get_resources, list_papers, save_paper
from src.paper.discovery import discover, identity
from src.paper.extractor import extract_paper
from src.parser import classify, links_from_text, normalize_url
from src.scoring import calculate_score


def pdf_bytes(text="PaperLens test paper", with_link=True):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 60), text)
    if with_link:
        page.insert_text((60, 90), "Source code: https://github.com/paperlens/example")
        page.insert_link(
            {
                "kind": fitz.LINK_URI,
                "from": fitz.Rect(60, 100, 250, 120),
                "uri": "https://example.org/supplement.pdf",
            }
        )
    data = doc.tobytes()
    doc.close()
    return data


def metadata(**overrides):
    paper = {
        "title": "A research paper",
        "authors": ["Test Author"],
        "abstract": "",
        "year": 2024,
        "area": "cs.CV",
        "source": "arXiv",
        "paper_url": "https://arxiv.org/abs/2401.12345",
        "pdf_url": "https://arxiv.org/pdf/2401.12345",
        "arxiv_id": "2401.12345",
    }
    paper.update(overrides)
    return paper


def accessible(resource):
    return {
        **resource,
        "status": "accessible",
        "http_status": 200,
        "final_url": resource["url"],
        "checked_at": "2026-01-01T00:00:00+00:00",
        "note": "Test response",
    }


class ParsingTests(unittest.TestCase):
    def test_plain_text_urls_and_balanced_punctuation(self):
        links = links_from_text(
            "Code https://github.com/org/repo. Dataset https://example.org/data_(v1)."
        )
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0]["url"], "https://github.com/org/repo")
        self.assertEqual(links[1]["url"], "https://example.org/data_(v1)")

    def test_url_schemes_and_credentials(self):
        for value in (
            "javascript:alert(1)",
            "file:///etc/passwd",
            "https://u:p@example.org",
            "not-a-url",
        ):
            self.assertIsNone(normalize_url(value))
        self.assertEqual(
            normalize_url("HTTPS://Example.org/path#section"), "https://example.org/path"
        )

    def test_categories(self):
        cases = {
            "https://github.com/a/b": "code",
            "https://huggingface.co/datasets/a/b": "dataset",
            "https://x.readthedocs.io/": "documentation",
            "https://example.org/supplementary.pdf": "supplementary",
            "https://arxiv.org/abs/1234": "other",
        }
        for url, category in cases.items():
            self.assertEqual(classify(url), category)

    def test_real_pdf_text_and_annotations(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "paper.pdf"
            path.write_bytes(pdf_bytes())
            result = extract_paper(path)
        self.assertEqual(result["pages"], 1)
        self.assertEqual(len(result["links"]), 2)
        self.assertTrue(all(r["page"] == 1 for r in result["resources"]))
        self.assertIn("code", [r["category"] for r in result["resources"]])

    def test_scanned_pdf_warns(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "empty.pdf"
            path.write_bytes(pdf_bytes("", False))
            self.assertTrue(extract_paper(path)["warnings"])

    def test_pdf_page_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "paper.pdf"
            path.write_bytes(pdf_bytes())
            with self.assertRaises(ValueError):
                extract_paper(path, max_pages=0)


class ScoringTests(unittest.TestCase):
    def test_weights_and_exclusions(self):
        resources = [
            {"category": "code", "status": "accessible"},
            {"category": "dataset", "status": "accessible"},
            {"category": "documentation", "status": "restricted"},
        ]
        self.assertEqual(calculate_score(resources)["score"], 70)
        self.assertEqual(calculate_score(resources, ["code", "dataset"])["score"], 100)

    def test_other_and_unresolved_earn_no_points(self):
        resources = [
            {"category": "other", "status": "accessible"},
            {"category": "code", "status": "unresolved"},
        ]
        self.assertEqual(calculate_score(resources)["score"], 0)

    def test_duplicates_do_not_inflate_score(self):
        resources = [{"category": "code", "status": "accessible"}] * 10
        self.assertEqual(calculate_score(resources)["score"], 35)

    def test_requires_applicable_category(self):
        with self.assertRaises(ValueError):
            calculate_score([], [])


class NetworkTests(unittest.TestCase):
    def test_blocks_non_public_destinations(self):
        for address in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "192.168.1.1"):
            with (
                self.subTest(address=address),
                patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", (address, 80))]),
            ):
                with self.assertRaises(UnsafeURL):
                    validate_url("http://example.org/")

    def test_blocks_credentials_ports_and_protocols(self):
        for url in (
            "http://u:p@example.org",
            "ftp://example.org/",
            "https://example.org:8443",
            "http://example.org/\n",
        ):
            with self.assertRaises(UnsafeURL):
                validate_url(url)

    def test_public_host_is_pinned(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            host, address, path = validate_url("https://example.org/a b?q=a b")
        self.assertEqual((host, address, path), ("example.org", "93.184.216.34", "/a%20b?q=a%20b"))

    def test_redirect_target_is_revalidated(self):
        response = Mock(status=302)
        response.getheader.return_value = "http://127.0.0.1/"
        conn = Mock()
        conn.getresponse.return_value = response
        with (
            patch(
                "src.checker.network.validate_url",
                side_effect=[("example.org", "93.184.216.34", "/"), UnsafeURL("private")],
            ) as validate,
            patch("http.client.HTTPConnection", return_value=conn),
        ):
            with self.assertRaises(UnsafeURL):
                fetch("http://example.org/")
        self.assertEqual(validate.call_count, 2)
        conn.close.assert_called_once()

    def test_status_semantics(self):
        for code, expected in (
            (200, "accessible"),
            (404, "missing"),
            (410, "missing"),
            (403, "restricted"),
            (429, "restricted"),
            (503, "unresolved"),
        ):
            with patch(
                "src.checker.fetch", return_value={"status": code, "url": "https://example.org"}
            ):
                self.assertEqual(check_resource({"url": "https://example.org"})["status"], expected)

    def test_head_fallback_and_timeout(self):
        with patch(
            "src.checker.fetch",
            side_effect=[
                {"status": 405, "url": "https://example.org"},
                {"status": 206, "url": "https://example.org"},
            ],
        ) as fetch_mock:
            self.assertEqual(check_resource({"url": "https://example.org"})["status"], "accessible")
            self.assertEqual(fetch_mock.call_count, 2)
        with patch("src.checker.fetch", side_effect=TimeoutError):
            self.assertEqual(check_resource({"url": "https://example.org"})["status"], "unresolved")

    def test_mixed_public_and_private_dns_is_blocked(self):
        with patch(
            "socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("127.0.0.1", 443))],
        ):
            with self.assertRaises(UnsafeURL):
                validate_url("https://example.org")


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(root / "test.db"),
                "UPLOAD_FOLDER": str(root / "uploads"),
                "SECRET_KEY": "test-key",
                "ANALYSIS_SYNC": True,
            }
        )
        self.context = self.app.app_context()
        self.context.push()
        self.pdf = pdf_bytes()
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["csrf_token"]

    def tearDown(self):
        self.context.pop()
        self.temp.cleanup()

    def post(self, url, **data):
        return self.client.post(url, data={"csrf_token": self.csrf, **data}, follow_redirects=True)

    def upload(self, **kwargs):
        return self.post("/upload", pdf=(io.BytesIO(self.pdf), "paper.pdf"), **kwargs)

    def test_pages_and_assets(self):
        for url in (
            "/",
            "/search",
            "/library",
            "/analytics",
            "/methodology",
            "/static/css/style.css",
            "/static/js/app.js",
        ):
            with self.client.get(url) as response:
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/missing").status_code, 404)

    def test_report_counters_follow_saved_reports(self):
        def counts():
            response = self.client.get("/library/stats")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return response.json

        self.assertEqual(counts(), {"total": 0, "analyzed": 0})
        self.pdf = pdf_bytes("No resource links", with_link=False)
        self.upload()
        paper_id = list_papers()[0]["id"]
        self.assertEqual(counts(), {"total": 1, "analyzed": 0})
        self.post(f"/papers/{paper_id}/analyze", applicable=["code"])
        self.assertEqual(get_paper(paper_id)["score"], 0)
        self.assertEqual(counts(), {"total": 1, "analyzed": 1})
        self.post(f"/papers/{paper_id}/analyze", applicable=["code"])
        self.assertEqual(counts()["analyzed"], 1)

        partial_id = save_paper(metadata(pdf_url=None))
        self.post(f"/papers/{partial_id}/analyze", applicable=["code"])
        self.assertEqual(counts(), {"total": 2, "analyzed": 2})
        failed_id = save_paper(
            metadata(arxiv_id="2401.99999", local_path=get_paper(paper_id)["local_path"])
        )
        with patch("src.analysis.extract_paper", side_effect=ValueError("broken")):
            with self.assertLogs(level="ERROR"):
                self.post(f"/papers/{failed_id}/analyze", applicable=["code"])
            with self.assertLogs(level="ERROR"):
                self.post(f"/papers/{paper_id}/analyze", applicable=["code"])
        self.assertEqual(counts(), {"total": 3, "analyzed": 2})
        home = self.client.get("/")
        self.assertIn(b'data-stat="total">3</strong>', home.data)
        self.assertIn(b'data-stat="analyzed">2</strong>', home.data)
        self.assertIn(b'data-library-stats="/library/stats"', home.data)

    def test_csrf_required(self):
        self.assertEqual(self.client.post("/upload", data={}).status_code, 400)

    def test_invalid_pdf_rejected(self):
        response = self.post("/upload", pdf=(io.BytesIO(b"not a PDF"), "paper.pdf"))
        self.assertIn(b"Could not read", response.data)
        self.assertEqual(len(list_papers()), 0)

    def test_upload_and_duplicate(self):
        self.assertEqual(self.upload(title="My paper").status_code, 200)
        self.assertEqual(len(list_papers()), 1)
        self.upload(title="Another title")
        self.assertEqual(len(list_papers()), 1)

    def test_complete_upload_report_and_category_correction(self):
        self.upload(title="An empirical study")
        paper = list_papers()[0]
        with patch("src.analysis.check_resource", side_effect=accessible):
            response = self.post(
                f"/papers/{paper['id']}/analyze",
                applicable=["code", "dataset", "documentation", "supplementary"],
            )
        self.assertEqual(response.status_code, 200)
        paper = get_paper(paper["id"])
        self.assertEqual(paper["analysis_status"], "complete")
        self.assertEqual(paper["score"], 45)
        self.assertEqual(paper["report"]["pages"], 1)
        resource = next(r for r in get_resources(paper["id"]) if r["category"] == "code")
        self.post(f"/resources/{resource['id']}/category", category="other")
        self.assertEqual(get_paper(paper["id"])["score"], 10)
        exported = self.client.get(f"/papers/{paper['id']}/report.json")
        self.assertEqual(exported.status_code, 200)
        self.assertNotIn("local_path", exported.json["paper"])
        self.assertEqual(exported.json["paper"]["report"]["score"], 10)
        self.assertEqual(self.client.get("/analytics").status_code, 200)
        self.assertEqual(self.client.get("/assets/plotly.js").status_code, 200)

    def test_abstract_only_is_labelled_partial(self):
        paper_id = save_paper(metadata(pdf_url=None, abstract="Source code https://github.com/a/b"))
        with patch("src.analysis.check_resource", side_effect=accessible):
            self.post(f"/papers/{paper_id}/analyze", applicable=["code"])
        paper = get_paper(paper_id)
        self.assertEqual(paper["analysis_status"], "partial")
        self.assertEqual(paper["report"]["scope"], "Abstract only")

    def test_pdf_download_failure_falls_back_to_abstract(self):
        paper_id = save_paper(metadata(abstract="Data: https://kaggle.com/datasets/a"))
        with (
            patch("src.analysis.download_pdf", side_effect=ValueError("HTTP 403")),
            patch("src.analysis.check_resource", side_effect=accessible),
        ):
            self.post(f"/papers/{paper_id}/analyze", applicable=["dataset"])
        report = get_paper(paper_id)["report"]
        self.assertEqual(report["score"], 100)
        self.assertIn("HTTP 403", report["warnings"][0])

    def test_existing_report_preserved_on_failure(self):
        self.upload()
        paper_id = list_papers()[0]["id"]
        with patch("src.analysis.check_resource", side_effect=accessible):
            self.post(f"/papers/{paper_id}/analyze", applicable=["code"])
        previous = get_paper(paper_id)["report"]
        with patch("src.analysis.extract_paper", side_effect=ValueError("broken")):
            with self.assertLogs(level="ERROR"):
                self.post(f"/papers/{paper_id}/analyze", applicable=["code"])
        self.assertEqual(get_paper(paper_id)["report"], previous)
        self.assertEqual(get_paper(paper_id)["analysis_status"], "failed")
        status = self.client.get(f"/papers/{paper_id}/status").json
        self.assertEqual(status["job"]["status"], "failed")

    def test_no_categories_rejected(self):
        self.upload()
        paper_id = list_papers()[0]["id"]
        self.post(f"/papers/{paper_id}/analyze")
        self.assertIsNone(get_paper(paper_id)["report"])

    def test_filters_csv_and_injection(self):
        save_paper(metadata(title="=unsafe spreadsheet formula"))
        response = self.client.get("/library.csv")
        self.assertIn(b"'=unsafe", response.data)
        self.assertEqual(self.client.get("/library?year=invalid").status_code, 400)
        self.assertEqual(self.client.get("/library?min_score=101").status_code, 400)
        self.assertNotIn(b"unsafe", self.client.get("/library?q=unmatched").data)
        self.assertEqual(self.client.get("/library?source=arXiv&sort=score").status_code, 200)

    def test_search_saves_and_deduplicates(self):
        with patch("app.routes.discover", return_value=([metadata()], [])):
            self.assertEqual(self.client.get("/search?q=test").status_code, 200)
            self.client.get("/search?q=test")
        self.assertEqual(len(list_papers()), 1)
        self.assertEqual(self.client.get("/search?source=unknown&q=test").status_code, 400)

    def test_cross_search_provider_deduplication(self):
        semantic = metadata(
            source="Semantic Scholar",
            arxiv_id=None,
            paper_url="https://www.semanticscholar.org/paper/example",
        )
        first = save_paper(semantic)
        second = save_paper(metadata())
        self.assertEqual(first, second)
        self.assertEqual(len(list_papers()), 1)

    def test_xss_escaped(self):
        paper_id = save_paper(metadata(title="<script>alert(1)</script>"))
        response = self.client.get(f"/papers/{paper_id}")
        self.assertNotIn(b"<script>alert(1)</script>", response.data)
        self.assertIn(b"&lt;script&gt;", response.data)

    def test_report_not_found_before_analysis(self):
        paper_id = save_paper(metadata())
        self.assertEqual(self.client.get(f"/papers/{paper_id}/report.json").status_code, 404)

    def test_refresh_cli(self):
        self.upload()
        paper_id = list_papers()[0]["id"]
        with patch("src.analysis.check_resource", side_effect=accessible):
            self.post(f"/papers/{paper_id}/analyze", applicable=["code"])
            result = self.app.test_cli_runner().invoke(args=["refresh-checks", "--limit", "1"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Updated:", result.output)

    def test_live_jobs_survive_other_app_instances(self):
        import os
        from src.database import now

        paper_id = save_paper(metadata())
        job_id = str(os.getpid()) + "-alive"
        with connection() as db:
            db.execute("UPDATE papers SET analysis_status='analyzing' WHERE id=?", (paper_id,))
            db.execute(
                "INSERT INTO jobs (id,paper_id,status,stage,created_at) VALUES (?,?,?,?,?)",
                (job_id, paper_id, "running", "Checking links", now()),
            )
        create_app(dict(self.app.config))
        self.assertEqual(get_paper(paper_id)["analysis_status"], "analyzing")

    def test_interrupted_jobs_are_recoverable(self):
        from src.database import now

        paper_id = save_paper(metadata())
        with connection() as db:
            db.execute("UPDATE papers SET analysis_status='analyzing' WHERE id=?", (paper_id,))
            db.execute(
                "INSERT INTO jobs (id,paper_id,status,stage,created_at) VALUES (?,?,?,?,?)",
                ("99999999-dead", paper_id, "running", "Checking links", now()),
            )
        with patch("app.os.kill", side_effect=ProcessLookupError):
            create_app(dict(self.app.config))
        self.assertEqual(get_paper(paper_id)["analysis_status"], "failed")
        self.assertEqual(
            self.client.get(f"/papers/{paper_id}/status").json["job"]["stage"], "Interrupted"
        )

    def test_oversized_upload_is_rejected(self):
        self.app.config["MAX_CONTENT_LENGTH"] = 100
        response = self.client.post(
            "/upload", data={"csrf_token": self.csrf, "pdf": (io.BytesIO(b"x" * 200), "paper.pdf")}
        )
        self.assertEqual(response.status_code, 413)

    def test_invalid_upload_year_and_category(self):
        response = self.upload(year="wrong")
        self.assertIn(b"four-digit", response.data)
        self.assertEqual(len(list_papers()), 0)
        self.assertEqual(self.post("/resources/1/category", category="invented").status_code, 400)

    def test_upload_year_is_saved(self):
        self.upload(year="2023")
        self.assertEqual(list_papers()[0]["year"], 2023)


class ProviderTests(unittest.TestCase):
    def test_arxiv_normalization(self):
        from src.paper.arxiv_client import search_arxiv

        atom = b"""<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
          <entry><id>http://arxiv.org/abs/2401.12345v2</id><title> A test\n paper </title>
          <summary>Example abstract</summary><published>2024-01-01T00:00:00Z</published>
          <author><name>Ada Example</name></author><arxiv:primary_category term="cs.CV"/>
          <link title="pdf" type="application/pdf" href="http://arxiv.org/pdf/2401.12345v2"/>
          </entry></feed>"""
        response = Mock(content=atom)
        with (
            patch("src.paper.arxiv_client.requests.get", return_value=response),
            patch("time.sleep"),
        ):
            paper = search_arxiv("test paper")[0]
        self.assertEqual(paper["title"], "A test paper")
        self.assertEqual(paper["year"], 2024)
        self.assertEqual(paper["area"], "cs.CV")
        self.assertTrue(paper["pdf_url"].startswith("https://"))
        self.assertEqual(identity(paper), "arxiv:2401.12345")

    def test_multi_provider_deduplication_and_partial_failure(self):
        import requests
        import src.paper.discovery as discovery

        with tempfile.TemporaryDirectory() as temp:
            app = create_app(
                {
                    "TESTING": True,
                    "DATABASE": str(Path(temp) / "db"),
                    "UPLOAD_FOLDER": str(Path(temp) / "uploads"),
                    "SECRET_KEY": "test",
                }
            )
            with app.app_context():
                discovery._cache.clear()
                with (
                    patch("src.paper.discovery.search_arxiv", return_value=[metadata()]),
                    patch(
                        "src.paper.discovery.search_semantic",
                        return_value=[metadata(source="Semantic Scholar")],
                    ),
                ):
                    papers, errors = discover("duplicate test", "both")
                self.assertEqual(len(papers), 1)
                self.assertFalse(errors)
                discovery._cache.clear()
                with (
                    patch("src.paper.discovery.search_arxiv", return_value=[metadata()]),
                    patch("src.paper.discovery.search_semantic", side_effect=requests.Timeout),
                    self.assertLogs(level="ERROR"),
                ):
                    papers, errors = discover("outage test", "both")
                self.assertEqual(len(papers), 1)
                self.assertEqual(len(errors), 1)
                discovery._cache.clear()


if __name__ == "__main__":
    unittest.main()
