"""SQLite persistence with short-lived connections and explicit transactions."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from flask import current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
 id INTEGER PRIMARY KEY,
 identity TEXT UNIQUE NOT NULL,
 title TEXT NOT NULL,
 authors TEXT NOT NULL DEFAULT '[]',
 abstract TEXT NOT NULL DEFAULT '',
 year INTEGER,
 area TEXT NOT NULL DEFAULT 'Uncategorized',
 source TEXT NOT NULL,
 paper_url TEXT,
 pdf_url TEXT,
 local_path TEXT,
 created_at TEXT NOT NULL,
 analysis_status TEXT NOT NULL DEFAULT 'not_analyzed',
 report TEXT,
 score INTEGER
);
CREATE TABLE IF NOT EXISTS resources (
 id INTEGER PRIMARY KEY,
 paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 url TEXT NOT NULL,
 category TEXT NOT NULL,
 evidence TEXT NOT NULL DEFAULT '',
 page INTEGER,
 status TEXT NOT NULL DEFAULT 'unchecked',
 http_status INTEGER,
 final_url TEXT,
 checked_at TEXT,
 note TEXT NOT NULL DEFAULT '',
 UNIQUE(paper_id, url)
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY,
 paper_id INTEGER NOT NULL REFERENCES papers(id),
 status TEXT NOT NULL,
 stage TEXT NOT NULL,
 error TEXT,
 created_at TEXT NOT NULL,
 finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_resources_paper ON resources(paper_id);
CREATE INDEX IF NOT EXISTS idx_papers_score ON papers(score);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connection():
    """Open a database transaction and always close its connection."""
    db = sqlite3.connect(current_app.config["DATABASE"], timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def init_db():
    with connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)


def decode(row):
    """Convert a SQLite row and its JSON fields into a paper dictionary."""
    if row is None:
        return None
    paper = dict(row)
    paper["authors"] = json.loads(paper["authors"])
    paper["report"] = json.loads(paper["report"]) if paper["report"] else None
    return paper


def save_paper(paper):
    """Save new metadata or return the ID of an existing matching paper."""
    from src.paper.discovery import identity

    with connection() as db:
        key = identity(paper)
        existing = db.execute("SELECT id FROM papers WHERE identity=?", (key,)).fetchone()
        if not existing and paper["source"] != "Upload":
            for row in db.execute("SELECT id,title,identity FROM papers WHERE source <> 'Upload'"):
                # Merge a title-only provider record with a canonical identifier.
                # Distinct canonical IDs must remain distinct even if titles match.
                if not (key.startswith("title:") or row["identity"].startswith("title:")):
                    continue
                if identity({"title": row["title"]}) == identity({"title": paper["title"]}):
                    existing = row
                    if not key.startswith("title:"):
                        db.execute("UPDATE papers SET identity=? WHERE id=?", (key, row["id"]))
                    break
        if existing:
            return existing["id"]
        cursor = db.execute(
            """INSERT INTO papers
            (identity,title,authors,abstract,year,area,source,paper_url,pdf_url,local_path,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                paper["title"],
                json.dumps(paper.get("authors", [])),
                paper.get("abstract") or "",
                paper.get("year"),
                paper.get("area") or "Uncategorized",
                paper["source"],
                paper.get("paper_url"),
                paper.get("pdf_url"),
                paper.get("local_path"),
                now(),
            ),
        )
        return cursor.lastrowid


def get_paper(paper_id):
    with connection() as db:
        return decode(db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone())


def get_resources(paper_id):
    with connection() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM resources WHERE paper_id=? ORDER BY category,url", (paper_id,)
            )
        ]


def list_papers(filters=None):
    """Find saved papers using validated filters and a known sort order."""
    filters = filters or {}
    clauses, values = [], []
    if filters.get("q"):
        clauses.append("(title LIKE ? OR abstract LIKE ? OR authors LIKE ?)")
        values.extend(["%" + filters["q"] + "%"] * 3)
    for field in ("source", "area", "analysis_status"):
        if filters.get(field):
            clauses.append(field + "=?")
            values.append(filters[field])
    for field, operator in (("year", "="), ("min_score", ">=")):
        if filters.get(field) not in (None, ""):
            clauses.append(("score" if field == "min_score" else field) + operator + "?")
            values.append(int(filters[field]))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    order = {"newest": "id DESC", "score": "score DESC, id DESC", "year": "year DESC, id DESC"}
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM papers"
            + where
            + " ORDER BY "
            + order.get(filters.get("sort"), "id DESC"),
            values,
        ).fetchall()
    return [decode(row) for row in rows]


def library_stats():
    """Count saved papers and reports, including reports with a zero score."""
    with connection() as db:
        row = db.execute(
            "SELECT COUNT(*) AS total, COUNT(NULLIF(report, '')) AS analyzed FROM papers"
        ).fetchone()
    return dict(row)


def facets():
    with connection() as db:
        return {
            field: [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT "
                    + field
                    + " FROM papers WHERE "
                    + field
                    + " IS NOT NULL ORDER BY "
                    + field
                )
            ]
            for field in ("source", "area", "year")
        }
