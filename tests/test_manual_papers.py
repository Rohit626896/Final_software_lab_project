"""Manual paper creation works without discovery providers."""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz

from app import create_app
from src.database import get_paper, get_resources, list_papers


def pdf_bytes():
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((60, 60), "Manual paper https://github.com/example/manual")
        return document.tobytes()


class ManualPaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.uploads = root / "uploads"
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(root / "test.db"),
                "UPLOAD_FOLDER": str(self.uploads),
                "SECRET_KEY": "test",
                "ANALYSIS_SYNC": True,
            }
        )
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.client.get("/papers/add")
        with self.client.session_transaction() as session:
            self.csrf = session["csrf_token"]

    def tearDown(self):
        self.context.pop()
        self.temp.cleanup()

    def post(self, **values):
        return self.client.post("/papers/add", data={"csrf_token": self.csrf, **values})

    def test_page_is_linked_and_marks_active_navigation(self):
        response = self.client.get("/papers/add")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Add a research paper", response.data)
        self.assertIn(b'aria-current="page"', response.data)
        self.assertIn(b"/papers/add", self.client.get("/library").data)

    def test_title_only_save_never_calls_provider_api(self):
        with patch("requests.get", side_effect=AssertionError("No API calls allowed")):
            response = self.post(title="My manual paper")
        self.assertEqual(response.status_code, 302)
        saved = list_papers()[0]
        self.assertEqual(saved["source"], "Manual")
        self.assertEqual(saved["authors"], [])
        self.assertIsNone(saved["year"])
        self.assertIsNone(saved["local_path"])
        self.assertEqual(response.location, f"/papers/{saved['id']}")

    def test_full_metadata_is_saved(self):
        self.post(
            title="Metadata paper",
            authors="Lovelace, Ada; Alan Turing\nGrace Hopper",
            year="2024",
            area="Computer science",
            paper_url="https://example.org/paper",
            pdf_url="https://example.org/paper.pdf",
            abstract="Source code https://github.com/a/b",
        )
        saved = list_papers()[0]
        self.assertEqual(saved["authors"], ["Lovelace, Ada", "Alan Turing", "Grace Hopper"])
        self.assertEqual(saved["year"], 2024)
        self.assertEqual(saved["area"], "Computer science")
        self.assertEqual(saved["paper_url"], "https://example.org/paper")
        self.assertEqual(saved["pdf_url"], "https://example.org/paper.pdf")
        self.assertIn("github.com", saved["abstract"])

    def test_invalid_fields_preserve_input_without_saving(self):
        response = self.post(
            title="Keep this title", authors="Ada", year="wrong", paper_url="javascript:alert(1)"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Keep this title", response.data)
        self.assertIn(b"Ada", response.data)
        self.assertIn(b'aria-invalid="true"', response.data)
        self.assertFalse(list_papers())

    def test_required_title_and_lengths_are_validated_on_server(self):
        for data in ({"title": "  "}, {"title": "x" * 301}, {"title": "Paper", "area": "x" * 81}):
            with self.subTest(data=list(data)):
                self.assertEqual(self.post(**data).status_code, 400)
        self.assertFalse(list_papers())

    def test_invalid_url_formats_are_rejected(self):
        for url in (
            "file:///tmp/paper.pdf",
            "https://user:secret@example.org/p.pdf",
            "https://example.org:bad/p.pdf",
            "https://example.org/a b",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.post(title="Paper", pdf_url=url).status_code, 400)
        self.assertFalse(list_papers())

    def test_attached_pdf_and_metadata_are_saved_together(self):
        data = pdf_bytes()
        response = self.post(
            title="Attached paper", authors="Ada; Alan", pdf=(io.BytesIO(data), "paper.pdf")
        )
        self.assertEqual(response.status_code, 302)
        saved = list_papers()[0]
        self.assertEqual(saved["source"], "Manual")
        self.assertEqual(saved["authors"], ["Ada", "Alan"])
        self.assertEqual(Path(saved["local_path"]).read_bytes(), data)

    def test_duplicate_pdf_leaves_one_file_and_record(self):
        data = pdf_bytes()
        for _ in range(2):
            self.post(title="Duplicate", pdf=(io.BytesIO(data), "paper.pdf"))
        self.assertEqual(len(list_papers()), 1)
        self.assertEqual(len(list(self.uploads.iterdir())), 1)

    def test_existing_manual_entry_can_receive_pdf_later(self):
        self.post(title="Attach later")
        original_id = list_papers()[0]["id"]
        self.post(title="Attach later", pdf=(io.BytesIO(pdf_bytes()), "paper.pdf"))
        self.assertEqual(len(list_papers()), 1)
        self.assertTrue(Path(get_paper(original_id)["local_path"]).is_file())

    def test_invalid_upload_creates_no_files_or_records(self):
        for filename in ("paper.pdf", "paper.txt"):
            response = self.post(title="Invalid", pdf=(io.BytesIO(b"not a pdf"), filename))
            self.assertEqual(response.status_code, 400)
            self.assertIn(b"Could not read this PDF", response.data)
        self.assertFalse(list_papers())
        self.assertFalse(list(self.uploads.iterdir()))

    def test_database_failure_removes_new_upload(self):
        with patch("app.routes.save_paper", side_effect=RuntimeError("Database failure")):
            with self.assertRaises(RuntimeError):
                self.post(title="Failed", pdf=(io.BytesIO(pdf_bytes()), "paper.pdf"))
        self.assertFalse(list(self.uploads.iterdir()))

    def test_csrf_is_required(self):
        response = self.client.post("/papers/add", data={"title": "No token"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(list_papers())

    def test_manual_resource_notes_use_normal_analysis_workflow(self):
        self.post(title="Manual analysis", abstract="Source code https://github.com/example/manual")
        paper_id = list_papers()[0]["id"]

        def accessible(resource):
            return {
                **resource,
                "status": "accessible",
                "http_status": 200,
                "final_url": resource["url"],
                "checked_at": "2026-01-01T00:00:00+00:00",
                "note": "Test",
            }

        with patch("src.analysis.check_resource", side_effect=accessible):
            response = self.client.post(
                f"/papers/{paper_id}/analyze",
                data={"csrf_token": self.csrf, "applicable": ["code"]},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(get_paper(paper_id)["score"], 100)
        self.assertEqual(len(get_resources(paper_id)), 1)
