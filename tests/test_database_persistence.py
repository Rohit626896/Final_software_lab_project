"""Disk persistence, transaction rollback, and compatibility migrations."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app import create_app
from src.database.db import (
    DATABASE_PATH,
    PROJECT_ROOT,
    connection,
    database_path,
    get_connection,
    get_paper,
    get_paper_by_external_id,
    get_resources,
    init_db,
    save_analysis,
    save_paper,
    save_resources,
    update_analysis_status,
    update_resources,
)
from src.scoring import calculate_score

SUPPLIED_SCHEMA = """
CREATE TABLE papers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, external_id TEXT NOT NULL,
 title TEXT NOT NULL, authors TEXT, year INTEGER, abstract TEXT, paper_url TEXT, pdf_url TEXT,
 last_analyzed_at TEXT, analysis_status TEXT NOT NULL DEFAULT 'pending'
 CHECK(analysis_status IN ('pending','completed','partial','failed')), UNIQUE(source,external_id)
);
CREATE TABLE resources (
 id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id INTEGER NOT NULL, url TEXT NOT NULL,
 resource_type TEXT NOT NULL CHECK(resource_type IN ('Code','Dataset','Documentation','Supplementary material','Unknown')),
 accessibility_status TEXT NOT NULL CHECK(accessibility_status IN ('Accessible','Inaccessible','Inconclusive')),
 http_status INTEGER, error_message TEXT, checked_at TEXT,
 FOREIGN KEY(paper_id) REFERENCES papers(id) ON DELETE CASCADE, UNIQUE(paper_id,url)
);
"""
LEGACY_SCHEMA = """
CREATE TABLE papers (
 id INTEGER PRIMARY KEY, identity TEXT UNIQUE NOT NULL, title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '[]',
 abstract TEXT NOT NULL DEFAULT '', year INTEGER, area TEXT NOT NULL DEFAULT 'Uncategorized', source TEXT NOT NULL,
 paper_url TEXT, pdf_url TEXT, local_path TEXT, created_at TEXT NOT NULL,
 analysis_status TEXT NOT NULL DEFAULT 'not_analyzed', report TEXT, score INTEGER
);
CREATE TABLE resources (
 id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 url TEXT NOT NULL, category TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '', page INTEGER,
 status TEXT NOT NULL DEFAULT 'unchecked', http_status INTEGER, final_url TEXT, checked_at TEXT,
 note TEXT NOT NULL DEFAULT '', UNIQUE(paper_id,url)
);
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, paper_id INTEGER NOT NULL REFERENCES papers(id), status TEXT NOT NULL,
 stage TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE discovery_cache (
 provider TEXT NOT NULL, query TEXT NOT NULL, fetched_at REAL NOT NULL, results TEXT NOT NULL,
 PRIMARY KEY(provider,query)
);
"""
STAMP = "2026-11-20T10:00:00+00:00"


def resource(url="https://example.org/code", status="accessible"):
    return {
        "url": url,
        "category": "code",
        "status": status,
        "http_status": 200,
        "checked_at": STAMP,
        "final_url": url,
        "note": "Saved check",
        "page": 3,
    }


def report(resources):
    return {
        **calculate_score(resources),
        "analyzed_at": STAMP,
        "scope": "PDF and abstract",
        "pages": 4,
        "warnings": [],
        "limitations": [],
        "resource_count": len(resources),
        "candidate_count": len(resources),
        "text_characters": 100,
    }


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "papers.db"
        self.config = {
            "TESTING": True,
            "DATABASE": str(self.path),
            "UPLOAD_FOLDER": str(self.root / "uploads"),
            "SECRET_KEY": "test",
        }

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, schema):
        db = sqlite3.connect(self.path)
        db.executescript(schema)
        return db

    def test_supplied_schema_is_migrated_and_checks_become_a_saved_report(self):
        db = self.seed(SUPPLIED_SCHEMA)
        db.execute(
            "INSERT INTO papers(id,source,external_id,title,authors,last_analyzed_at,analysis_status) VALUES (42,'Semantic Scholar','P123','Imported paper','Ada; Alan',?,'completed')",
            (STAMP,),
        )
        db.execute(
            "INSERT INTO resources VALUES (7,42,'https://example.org/data','Dataset','Accessible',200,NULL,?)",
            (STAMP,),
        )
        db.commit()
        db.close()
        app = create_app(self.config)
        with app.app_context():
            saved = get_paper(42)
            self.assertEqual(saved["authors"], ["Ada", "Alan"])
            self.assertEqual(saved["last_analyzed_at"], STAMP)
            self.assertEqual(saved["external_id"], "P123")
            self.assertEqual(saved["analysis_status"], "complete")
            self.assertEqual(saved["report"]["scope"], "Imported resource checks")
            self.assertEqual(saved["score"], 33)
            self.assertEqual(get_resources(42)[0]["id"], 7)
            self.assertEqual(get_resources(42)[0]["accessibility_status"], "Accessible")
            self.assertEqual(get_paper_by_external_id("Semantic Scholar", "P123")["id"], 42)
            first = get_paper(42)
            init_db()
            self.assertEqual(get_paper(42), first)
        backups = list((self.root / "backups").glob("*.db"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(backup.execute("SELECT external_id FROM papers").fetchone()[0], "P123")
            self.assertNotIn(
                "report", {row[1] for row in backup.execute("PRAGMA table_info(papers)")}
            )
        self.assertEqual(app.test_client().get("/papers/42").status_code, 200)
        self.assertEqual(app.test_client().get("/papers/42/report.json").status_code, 200)

    def seed_legacy(self, state="complete", orphan=False):
        db = self.seed(LEGACY_SCHEMA)
        snapshot = report([resource()])
        db.execute(
            "INSERT INTO papers(id,identity,title,authors,source,local_path,created_at,analysis_status,report,score) VALUES (8,'arxiv:2401.12345','Legacy paper','[\"Ada\"]','arXiv',?,?,?, ?,?)",
            (str(self.root / "paper.pdf"), STAMP, state, json.dumps(snapshot), snapshot["score"]),
        )
        db.execute(
            "INSERT INTO resources(id,paper_id,url,category,page,status,http_status,checked_at,note) VALUES (11,?,'https://example.org/code','code',3,'restricted',403,?,'Login required')",
            (999 if orphan else 8, STAMP),
        )
        db.execute(
            "INSERT INTO jobs VALUES ('old-job',8,'completed','Report ready',NULL,?,?)",
            (STAMP, STAMP),
        )
        db.execute("INSERT INTO discovery_cache VALUES ('arXiv','legacy',1,'[]')")
        db.commit()
        db.close()
        return snapshot

    def test_project_database_migration_preserves_report_ids_jobs_cache_and_pdf_path(self):
        snapshot = self.seed_legacy()
        app = create_app(self.config)
        with app.app_context():
            saved = get_paper(8)
            self.assertEqual(saved["report"], snapshot)
            self.assertEqual(saved["score"], snapshot["score"])
            self.assertEqual(saved["last_analyzed_at"], STAMP)
            self.assertEqual(saved["local_path"], str(self.root / "paper.pdf"))
            self.assertEqual(saved["external_id"], "2401.12345")
            checked = get_resources(8)[0]
            self.assertEqual(
                (checked["id"], checked["page"], checked["status"]), (11, 3, "restricted")
            )
            self.assertEqual(checked["accessibility_status"], "Inconclusive")
            self.assertEqual(checked["error_message"], "Login required")
            with connection() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM discovery_cache").fetchone()[0], 1
                )
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(
                    db.execute("SELECT analysis_status FROM papers").fetchone()[0], "completed"
                )
                self.assertFalse(db.execute("PRAGMA foreign_key_check").fetchall())

    def test_invalid_legacy_status_rolls_back_migration_and_keeps_backup(self):
        self.seed_legacy(state="invalid")
        with self.assertRaises(sqlite3.IntegrityError):
            init_db(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(
                db.execute("SELECT analysis_status FROM papers").fetchone()[0], "invalid"
            )
            self.assertIn(
                "category", {row[1] for row in db.execute("PRAGMA table_info(resources)")}
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM resources").fetchone()[0], 1)
            self.assertFalse(
                db.execute("SELECT name FROM sqlite_master WHERE name='_papers_v2'").fetchone()
            )
        self.assertEqual(len(list((self.root / "backups").glob("*.db"))), 1)

    def test_orphaned_rows_stop_migration_without_deleting_data(self):
        self.seed_legacy(orphan=True)
        with self.assertRaisesRegex(ValueError, "foreign-key"):
            init_db(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT paper_id FROM resources").fetchone()[0], 999)

    def test_resource_upsert_keeps_ids_and_replacement_removes_old_urls(self):
        app = create_app(self.config)
        with app.app_context():
            paper_id = save_paper({"source": "Manual", "external_id": "manual-1", "title": "One"})
            save_resources(paper_id, [resource()])
            first_id = get_resources(paper_id)[0]["id"]
            update_resources(paper_id, [{**resource(), "status": "missing", "note": "HTTP 404"}])
            updated = get_resources(paper_id)[0]
            self.assertEqual(updated["id"], first_id)
            self.assertEqual(updated["accessibility_status"], "Inaccessible")
            self.assertEqual(updated["error_message"], "HTTP 404")
            update_resources(paper_id, [])
            self.assertFalse(get_resources(paper_id))

    def test_snapshot_write_failure_rolls_back_resources_and_report_together(self):
        app = create_app(self.config)
        with app.app_context():
            paper_id = save_paper({"source": "Manual", "title": "Atomic"})
            snapshot = report([resource()])
            save_analysis(paper_id, [resource()], snapshot, "completed")
            before = get_resources(paper_id)
            with self.assertRaises(sqlite3.IntegrityError):
                save_analysis(
                    paper_id,
                    [
                        {**resource(), "note": "Changed"},
                        resource("https://example.org/bad", "bad-status"),
                    ],
                    snapshot,
                    "completed",
                )
            self.assertEqual(get_resources(paper_id), before)
            self.assertEqual(get_paper(paper_id)["report"], snapshot)
            self.assertEqual(get_paper(paper_id)["last_analyzed_at"], STAMP)

    def test_failed_refresh_does_not_reset_last_successful_analysis(self):
        app = create_app(self.config)
        with app.app_context():
            paper_id = save_paper({"source": "Manual", "title": "Keep report"})
            save_analysis(paper_id, [resource()], report([resource()]), "partial")
            update_analysis_status(paper_id, "failed")
        restarted = create_app(self.config)
        with restarted.app_context():
            saved = get_paper(paper_id)
            self.assertEqual(saved["analysis_status"], "failed")
            self.assertEqual(saved["last_analyzed_at"], STAMP)
            self.assertIsNotNone(saved["report"])

    def test_pasted_resource_field_names_and_external_id_uniqueness_are_supported(self):
        app = create_app(self.config)
        with app.app_context():
            paper_id = save_paper(
                {
                    "source": "Manual",
                    "external_id": "local-123",
                    "title": "Original",
                    "authors": "Ada; Alan",
                }
            )
            repeated = save_paper(
                {"source": "Manual", "external_id": "local-123", "title": "Different title"}
            )
            self.assertEqual(paper_id, repeated)
            save_resources(
                paper_id,
                [
                    {
                        "url": "https://example.org/data",
                        "resource_type": "Dataset",
                        "accessibility_status": "Inconclusive",
                        "http_status": 429,
                        "error_message": "Throttled",
                    }
                ],
            )
            saved = get_resources(paper_id)[0]
            self.assertEqual(saved["category"], "dataset")
            self.assertEqual(saved["status"], "unresolved")
            self.assertEqual(saved["error_message"], "Throttled")
            self.assertIsNotNone(saved["checked_at"])

    def test_distinct_provider_ids_can_have_the_same_title(self):
        app = create_app(self.config)
        with app.app_context():
            first = save_paper(
                {"source": "Semantic Scholar", "external_id": "first", "title": "Same title"}
            )
            second = save_paper(
                {"source": "Semantic Scholar", "external_id": "second", "title": "Same title"}
            )
            self.assertNotEqual(first, second)
            self.assertEqual(get_paper_by_external_id("Semantic Scholar", "first")["id"], first)
            self.assertEqual(get_paper_by_external_id("Semantic Scholar", "second")["id"], second)

    def test_foreign_keys_cascade_and_invalid_types_are_enforced(self):
        app = create_app(self.config)
        with app.app_context():
            paper_id = save_paper({"source": "Manual", "title": "Cascade"})
            save_resources(paper_id, [resource()])
            with connection() as db:
                self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)
                db.execute("DELETE FROM papers WHERE id=?", (paper_id,))
            self.assertFalse(get_resources(paper_id))
            with self.assertRaises(sqlite3.IntegrityError):
                save_resources(paper_id, [resource()])

    def test_path_is_stable_and_temporary_sqlite_paths_are_rejected(self):
        self.assertEqual(database_path(""), DATABASE_PATH)
        self.assertEqual(database_path("data/test.db"), PROJECT_ROOT / "data/test.db")
        for value in (":memory:", "file:temporary?mode=memory"):
            with self.assertRaises(ValueError):
                database_path(value)
        db = get_connection(self.path)
        db.close()
        self.assertTrue(self.path.is_file())

    def test_analysis_survives_two_separate_python_processes(self):
        writer = """
import sys
from unittest.mock import patch
from app import create_app
from src.database.db import save_paper
from src.analysis import analyze
app = create_app({'TESTING': True, 'DATABASE': sys.argv[1], 'UPLOAD_FOLDER': sys.argv[2], 'SECRET_KEY': 'test'})
with app.app_context():
    paper_id = save_paper({'source': 'Manual', 'external_id': 'process-test', 'title': 'Persistent analysis', 'abstract': 'Source code https://github.com/example/repro'})
    def checked(resource):
        return {**resource, 'status': 'accessible', 'http_status': 200, 'final_url': resource['url'], 'checked_at': '2026-11-20T10:00:00+00:00', 'note': 'Offline check'}
    with patch('src.analysis.check_resource', side_effect=checked):
        analyze(paper_id, ['code'])
    print(paper_id)
"""
        reader = """
import json, sys
from unittest.mock import patch
from app import create_app
from src.database.db import get_paper, get_resources
with patch('requests.get', side_effect=AssertionError('No API calls during reload')):
    app = create_app({'TESTING': True, 'DATABASE': sys.argv[1], 'UPLOAD_FOLDER': sys.argv[2], 'SECRET_KEY': 'test'})
    with app.app_context():
        paper_id = int(sys.argv[3])
        paper = get_paper(paper_id)
        client = app.test_client()
        statuses = [client.get(path).status_code for path in ('/library', f'/papers/{paper_id}', f'/papers/{paper_id}/report.json', '/analytics')]
        print(json.dumps({'score': paper['score'], 'last_analyzed_at': paper['last_analyzed_at'], 'resources': len(get_resources(paper_id)), 'statuses': statuses}))
"""
        env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
        args = [str(self.path), str(self.root / "uploads")]
        first = subprocess.run(
            [sys.executable, "-c", writer, *args],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )
        paper_id = first.stdout.strip()
        second = subprocess.run(
            [sys.executable, "-c", reader, *args, paper_id],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )
        saved = json.loads(second.stdout)
        self.assertEqual(saved["score"], 100)
        self.assertEqual(saved["resources"], 1)
        self.assertTrue(saved["last_analyzed_at"])
        self.assertEqual(saved["statuses"], [200, 200, 200, 200])
