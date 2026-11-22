"""Offline tests for quota handling, persistent cache, concurrency, and honest fallbacks."""

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from app import create_app
from src.database.db import connection, save_paper
from src.paper.discovery import discover, search_semantic, search_europe_pmc
from src.paper import provider_http


def paper(source="arXiv", title="Vision methods"):
    return {
        "title": title,
        "authors": ["Example Author"],
        "abstract": "Vision research",
        "source": source,
        "paper_url": "https://example.org/" + title.replace(" ", "-"),
        "pdf_url": "https://example.org/paper.pdf",
    }


class DiscoveryReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.fallback_patch = patch("src.paper.discovery.search_europe_pmc", return_value=[])
        self.fallback = self.fallback_patch.start()
        self.addCleanup(self.fallback_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.config = {
            "TESTING": True,
            "DATABASE": str(Path(self.temp.name) / "test.db"),
            "UPLOAD_FOLDER": str(Path(self.temp.name) / "uploads"),
            "SECRET_KEY": "test",
        }
        self.app = create_app(self.config)
        self.context = self.app.app_context()
        self.context.push()

    def tearDown(self):
        self.context.pop()
        self.temp.cleanup()

    def age_cache(self, seconds):
        with connection() as db:
            db.execute("UPDATE discovery_cache SET fetched_at=?", (time.time() - seconds,))
            db.execute("UPDATE discovery_provider_state SET lease_until=0")

    def test_normalized_queries_survive_app_restart(self):
        with patch("src.paper.discovery.search_arxiv", return_value=[paper()]) as fetch:
            first = discover("  VISION   methods ")
            other_app = create_app(self.config)
            with other_app.app_context():
                second = discover("vision methods")
            self.assertEqual(first, second)
            self.assertEqual(fetch.call_count, 1)

    def test_sources_have_separate_cache_entries_and_deduplicate(self):
        with (
            patch("src.paper.discovery.search_arxiv", return_value=[paper()]) as arxiv,
            patch(
                "src.paper.discovery.search_semantic", return_value=[paper("Semantic Scholar")]
            ) as semantic,
        ):
            results, notices = discover("vision", "both")
            self.assertEqual(len(results), 1)
            self.assertFalse(notices)
            discover("VISION", "both")
            self.assertEqual(arxiv.call_count, 1)
            self.assertEqual(semantic.call_count, 1)

    def test_stale_results_are_labeled_and_preserved_on_throttle(self):
        with patch("src.paper.discovery.search_arxiv", return_value=[paper()]):
            discover("vision")
        self.age_cache(90000)
        with (
            patch(
                "src.paper.discovery.search_arxiv", side_effect=provider_http.ProviderRateLimit(90)
            ) as fetch,
            patch("src.paper.discovery.search_semantic") as alternate,
        ):
            results, notices = discover("vision")
            self.assertEqual(results[0]["title"], "Vision methods")
            self.assertIn("previously cached", " ".join(notices))
            discover("vision")
            self.assertEqual(fetch.call_count, 1)
            alternate.assert_not_called()
        with connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM discovery_cache").fetchone()[0], 1)

    def test_provider_cooldown_survives_restart_and_new_queries(self):
        with (
            patch(
                "src.paper.discovery.search_semantic",
                side_effect=provider_http.ProviderRateLimit(120),
            ) as semantic,
            patch("src.paper.discovery.search_arxiv", return_value=[paper()]) as arxiv,
        ):
            results, notices = discover("vision", "Semantic Scholar")
            self.assertEqual(results[0]["source"], "arXiv")
            self.assertIn("arXiv instead", " ".join(notices))
            second_app = create_app(self.config)
            with second_app.app_context():
                with connection() as db:
                    db.execute(
                        "UPDATE discovery_provider_state SET lease_until=0 WHERE provider='arXiv'"
                    )
                discover("different topic", "Semantic Scholar")
            self.assertEqual(semantic.call_count, 1)
            self.assertEqual(arxiv.call_count, 2)

    def test_both_providers_down_uses_local_library_with_notice(self):
        save_paper(paper())
        with (
            patch("src.paper.discovery.search_arxiv", side_effect=requests.Timeout),
            patch("src.paper.discovery.search_semantic", side_effect=requests.Timeout),
            self.assertLogs(level="ERROR"),
        ):
            results, notices = discover("Vision", "both")
        self.assertEqual(len(results), 1)
        self.assertIn("saved in your library", " ".join(notices))

    def test_old_cache_outside_stale_window_does_not_masquerade_as_fresh(self):
        with patch("src.paper.discovery.search_arxiv", return_value=[paper()]):
            discover("vision")
        self.age_cache(8 * 86400)
        with (
            patch(
                "src.paper.discovery.search_arxiv", side_effect=provider_http.ProviderRateLimit(60)
            ),
            patch(
                "src.paper.discovery.search_semantic",
                return_value=[paper("Semantic Scholar", "Alternate")],
            ),
        ):
            results, notices = discover("vision")
        self.assertEqual(results[0]["title"], "Alternate")
        self.assertNotIn("previously cached", " ".join(notices))

    def test_empty_results_have_shorter_cache_lifetime(self):
        with patch("src.paper.discovery.search_arxiv", return_value=[]) as fetch:
            discover("empty")
            discover("empty")
            self.assertEqual(fetch.call_count, 1)
            self.age_cache(301)
            discover("empty")
            self.assertEqual(fetch.call_count, 2)

    def test_concurrent_identical_queries_share_one_request(self):
        started = threading.Event()
        finish = threading.Event()

        def fetch(*args):
            started.set()
            if not finish.wait(5):
                raise RuntimeError("Test timed out waiting for concurrent search")
            return [paper()]

        def search():
            with self.app.app_context():
                return discover("concurrent vision")

        with patch("src.paper.discovery.search_arxiv", side_effect=fetch) as request:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(search)
                self.assertTrue(started.wait(5))
                second = executor.submit(search)
                finish.set()
                self.assertEqual(first.result(timeout=5), second.result(timeout=5))
            self.assertEqual(request.call_count, 1)

    def test_europe_pmc_fallback_is_labeled_and_cached(self):
        self.fallback.return_value = [paper("Europe PMC", "Vision biology")]
        with (
            patch(
                "src.paper.discovery.search_arxiv", side_effect=provider_http.ProviderRateLimit(120)
            ),
            patch(
                "src.paper.discovery.search_semantic",
                side_effect=provider_http.ProviderRateLimit(120),
            ),
        ):
            results, notices = discover("vision", "both")
            repeat, _ = discover("vision", "both")
        self.assertEqual(results, repeat)
        self.assertEqual(results[0]["source"], "Europe PMC")
        self.assertIn("life-science research source", " ".join(notices))
        self.assertEqual(self.fallback.call_count, 1)

    def test_europe_pmc_only_uses_explicit_open_access_pdf_links(self):
        response = Mock()
        response.json.return_value = {
            "resultList": {
                "result": [
                    {
                        "title": "OA paper",
                        "isOpenAccess": "Y",
                        "pubYear": "2024",
                        "authorList": {"author": [{"fullName": "Ada"}]},
                        "fullTextUrlList": {
                            "fullTextUrl": [
                                {
                                    "documentStyle": "pdf",
                                    "availabilityCode": "S",
                                    "url": "https://example.org/paid.pdf",
                                },
                                {
                                    "documentStyle": "pdf",
                                    "availabilityCode": "OA",
                                    "url": "https://example.org/open.pdf",
                                },
                            ]
                        },
                    },
                    {"title": "No PDF", "isOpenAccess": "Y"},
                    {"title": "Closed", "isOpenAccess": "N"},
                ]
            }
        }
        with patch("src.paper.discovery.provider_get", return_value=response) as fetch:
            results = search_europe_pmc("computer vision")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["pdf_url"], "https://example.org/open.pdf")
        self.assertEqual(results[0]["source"], "Europe PMC")
        self.assertEqual(results[0]["authors"], ["Ada"])
        self.assertEqual(results[0]["year"], 2024)
        self.assertIn("OPEN_ACCESS:Y", fetch.call_args.kwargs["params"]["query"])

    def test_provider_lease_is_released_after_malformed_response(self):
        with (
            patch("src.paper.discovery.search_arxiv", side_effect=ValueError("bad response")),
            patch("src.paper.discovery.search_semantic", return_value=[]),
            self.assertLogs(level="ERROR"),
        ):
            discover("malformed")
        with connection() as db:
            row = db.execute(
                "SELECT * FROM discovery_provider_state WHERE provider='arXiv'"
            ).fetchone()
        self.assertEqual(row["lease_token"], "")
        self.assertGreater(row["retry_at"], time.time())

    def test_cache_is_rechecked_after_claiming_provider(self):
        with (
            patch("src.paper.discovery._cached", side_effect=[None, (time.time(), [paper()])]),
            patch("src.paper.discovery.search_arxiv") as fetch,
        ):
            results, notices = discover("vision")
        self.assertEqual(results[0]["title"], "Vision methods")
        self.assertFalse(notices)
        fetch.assert_not_called()

    def test_database_lease_enforces_spacing_for_other_workers(self):
        from src.paper.discovery import _claim, _release

        token, _ = _claim("arXiv")
        self.assertIsNotNone(token)
        self.assertEqual(_claim("arXiv"), (None, None))
        _release("arXiv", token)
        self.assertEqual(_claim("arXiv"), (None, None))
        with connection() as db:
            state = db.execute(
                "SELECT * FROM discovery_provider_state WHERE provider='arXiv'"
            ).fetchone()
        self.assertGreater(state["lease_until"], time.time() + 2.5)
        self.assertEqual(state["lease_token"], "")

    def test_semantic_api_key_and_result_normalization(self):
        response = Mock()
        response.json.return_value = {
            "data": [
                {
                    "paperId": "123",
                    "title": "Example",
                    "authors": [{"name": "Ada"}],
                    "openAccessPdf": {"url": "https://example.org/paper.pdf"},
                    "externalIds": {},
                }
            ]
        }
        self.app.config["SEMANTIC_SCHOLAR_API_KEY"] = "test-key"
        with patch("src.paper.discovery.provider_get", return_value=response) as fetch:
            results = search_semantic("example")
        self.assertEqual(results[0]["authors"], ["Ada"])
        self.assertEqual(fetch.call_args.kwargs["headers"], {"x-api-key": "test-key"})
        self.assertEqual(fetch.call_args.kwargs["interval"], 1.1)


class FakeClock:
    def __init__(self):
        self.value = 1000000.0
        self.waits = []

    def time(self):
        return self.value

    monotonic = time

    def sleep(self, delay):
        self.waits.append(delay)
        self.value += delay


class ProviderHttpTests(unittest.TestCase):
    def setUp(self):
        provider_http._last_request.clear()
        provider_http._blocked_until.clear()
        self.clock = FakeClock()
        self.time_patch = patch("src.paper.provider_http.time", self.clock)
        self.time_patch.start()
        self.jitter_patch = patch("src.paper.provider_http.random.uniform", return_value=0)
        self.jitter_patch.start()

    def tearDown(self):
        self.time_patch.stop()
        self.jitter_patch.stop()
        provider_http._last_request.clear()
        provider_http._blocked_until.clear()

    def response(self, status=200, retry_after=None):
        response = Mock(status_code=status, headers={})
        if retry_after is not None:
            response.headers["Retry-After"] = retry_after
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(response=response)
        return response

    def get(self):
        return provider_http.provider_get("arXiv", "https://example.org/api", interval=3)

    def test_short_429_retries_after_requested_delay_and_respects_spacing(self):
        with patch(
            "src.paper.provider_http.requests.get",
            side_effect=[self.response(429, "4"), self.response()],
        ) as get:
            self.assertEqual(self.get().status_code, 200)
        self.assertEqual(get.call_count, 2)
        self.assertGreaterEqual(sum(self.clock.waits), 4)

    def test_long_429_fails_fast_and_blocks_further_calls(self):
        with patch(
            "src.paper.provider_http.requests.get", return_value=self.response(429, "120")
        ) as get:
            with self.assertRaises(provider_http.ProviderRateLimit) as error:
                self.get()
            self.assertEqual(error.exception.retry_after, 120)
            with self.assertRaises(provider_http.ProviderRateLimit):
                self.get()
        self.assertEqual(get.call_count, 1)
        self.assertFalse(self.clock.waits)

    def test_repeated_429_has_only_two_attempts_and_default_cooldown(self):
        with patch("src.paper.provider_http.requests.get", return_value=self.response(429)) as get:
            with self.assertRaises(provider_http.ProviderRateLimit) as error:
                self.get()
        self.assertEqual(get.call_count, 2)
        self.assertEqual(error.exception.retry_after, 60)

    def test_http_date_and_invalid_retry_after(self):
        date = datetime.fromtimestamp(self.clock.value + 90, timezone.utc)
        self.assertEqual(provider_http.retry_after_seconds(format_datetime(date)), 90)
        for value in ("not a date", "nan", "inf", None):
            self.assertIsNone(provider_http.retry_after_seconds(value))

    def test_transient_503_retry_and_long_server_wait(self):
        with patch(
            "src.paper.provider_http.requests.get",
            side_effect=[self.response(503), self.response()],
        ):
            self.assertEqual(self.get().status_code, 200)
        with patch("src.paper.provider_http.requests.get", return_value=self.response(503, "300")):
            with self.assertRaises(provider_http.ProviderRateLimit) as error:
                self.get()
            self.assertEqual(error.exception.retry_after, 300)

    def test_permanent_errors_are_not_retried(self):
        with patch("src.paper.provider_http.requests.get", return_value=self.response(401)) as get:
            with self.assertRaises(requests.HTTPError):
                self.get()
        self.assertEqual(get.call_count, 1)

    def test_successful_requests_are_paced(self):
        with patch("src.paper.provider_http.requests.get", return_value=self.response()):
            self.get()
            self.get()
        self.assertEqual(self.clock.waits, [3])
