"""SQLite persistence with short-lived connections and explicit transactions."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from flask import current_app, has_app_context

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATABASE_PATH = PROJECT_ROOT / "data" / "paperlens.db"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
APP_SCHEMA_PATH = Path(__file__).with_name("app_schema.sql")
SCHEMA = (
    SCHEMA_PATH.read_text(encoding="utf-8") + "\n" + APP_SCHEMA_PATH.read_text(encoding="utf-8")
)
RESOURCE_TYPES = {
    "code": "Code",
    "dataset": "Dataset",
    "documentation": "Documentation",
    "supplementary": "Supplementary material",
    "other": "Unknown",
}
DB_STATUSES = {"not_analyzed": "pending", "complete": "completed"}
UI_STATUSES = {value: key for key, value in DB_STATUSES.items()}


def get_connection(db_path=None):
    """Open a persistent database, including outside a Flask request or app context."""
    path = database_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA synchronous=FULL")
    return db


def init_db(db_path=None):
    from src.database.migrations import initialize_database

    initialize_database(database_path(db_path), SCHEMA)


def save_paper(paper):
    """Save new metadata or return the ID of an existing matching paper."""
    from src.paper.discovery import identity

    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        key = identity(paper)
        external_id = str(
            paper.get("external_id") or paper.get("paper_id") or paper.get("arxiv_id") or key
        )
        existing_id = _find_existing_paper(db, paper, key, external_id)
        if existing_id is not None:
            return existing_id
        cursor = db.execute(
            """INSERT INTO papers
            (source,external_id,title,authors,year,abstract,paper_url,pdf_url,identity,area,local_path,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                paper["source"],
                external_id,
                paper["title"],
                json.dumps(_authors(paper.get("authors"))),
                paper.get("year"),
                paper.get("abstract") or "",
                paper.get("paper_url"),
                paper.get("pdf_url"),
                key,
                paper.get("area") or "Uncategorized",
                paper.get("local_path"),
                now(),
            ),
        )
        return cursor.lastrowid


def get_paper_by_id(paper_id):
    with connection() as db:
        return db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()


def get_paper_by_external_id(source, external_id):
    with connection() as db:
        return db.execute(
            "SELECT * FROM papers WHERE source=? AND external_id=?",
            (source, str(external_id)),
        ).fetchone()


def update_analysis_status(paper_id, status):
    status = DB_STATUSES.get(status, status)
    if status not in ("pending", "analyzing", "completed", "partial", "failed"):
        raise ValueError("Invalid analysis status.")
    with connection() as db:
        cursor = db.execute("UPDATE papers SET analysis_status=? WHERE id=?", (status, paper_id))
        return cursor.rowcount > 0


def save_resources(paper_id, resources, db_path=None):
    """Merge resource checks using either the supplied schema or checker field names."""
    with connection(db_path) as db:
        _write_resources(db, paper_id, resources)


def get_resources_by_paper_id(paper_id):
    with connection() as db:
        return db.execute(
            "SELECT * FROM resources WHERE paper_id=? ORDER BY id", (paper_id,)
        ).fetchall()


def update_resources(paper_id, resources, db_path=None):
    """Replace a resource snapshot, retaining IDs for URLs that still exist."""
    with connection(db_path) as db:
        _write_resources(db, paper_id, resources, replace=True)


def save_analysis(paper_id, resources, report, status):
    """Commit a complete analysis snapshot atomically; failed writes retain the old report."""
    status = DB_STATUSES.get(status, status)
    if status not in ("completed", "partial"):
        raise ValueError("A saved analysis must be completed or partial.")
    with connection() as db:
        _write_resources(db, paper_id, resources, replace=True)
        cursor = db.execute(
            "UPDATE papers SET report=?,score=?,analysis_status=?,last_analyzed_at=? WHERE id=?",
            (json.dumps(report), report["score"], status, report["analyzed_at"], paper_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Paper not found.")


# Compatibility helpers used by the current UI and background analysis.


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def database_path(db_path=None):
    value = db_path
    if value is None and has_app_context():
        value = current_app.config.get("DATABASE")
    value = str(value or DATABASE_PATH)
    if value == ":memory:" or value.startswith("file:"):
        raise ValueError("Use a persistent SQLite file path for DATABASE.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


@contextmanager
def connection(db_path=None):
    """Commit successful writes, roll back failures, and always close the connection."""
    db = get_connection(db_path)
    try:
        with db:
            yield db
    finally:
        db.close()


def _authors(value):
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            decoded = None
        return (
            decoded
            if isinstance(decoded, list)
            else [name.strip() for name in value.split(";") if name.strip()]
        )
    return list(value or [])


def decode(row):
    """Convert a SQLite row and its JSON fields into a paper dictionary."""
    if row is None:
        return None
    paper = dict(row)
    paper["authors"] = _authors(paper["authors"])
    paper["analysis_status"] = UI_STATUSES.get(paper["analysis_status"], paper["analysis_status"])
    paper["report"] = json.loads(paper["report"]) if paper["report"] else None
    return paper


def get_paper(paper_id):
    """Adapt the saved row for existing templates and analysis code."""
    return decode(get_paper_by_id(paper_id))


def get_resources(paper_id):
    """Expose checker category names while keeping the supplied SQL column names."""
    resources = [decode_resource(row) for row in get_resources_by_paper_id(paper_id)]
    return sorted(resources, key=lambda resource: (resource["resource_type"], resource["url"]))


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
            values.append(
                DB_STATUSES.get(filters[field], filters[field])
                if field == "analysis_status"
                else filters[field]
            )
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


def decode_resource(row):
    resource = dict(row)
    resource["category"] = next(
        key for key, label in RESOURCE_TYPES.items() if label == resource["resource_type"]
    )
    return resource


def normalize_resource(resource):
    resource = dict(resource)
    category = resource.get("category")
    label = resource.get("resource_type") or RESOURCE_TYPES.get(category, "Unknown")
    if label not in RESOURCE_TYPES.values():
        raise ValueError("Invalid resource type.")
    status = resource.get("status") or {
        "Accessible": "accessible",
        "Inaccessible": "missing",
        "Inconclusive": "unresolved",
    }.get(resource.get("accessibility_status"), "unchecked")
    broad = (
        "Accessible"
        if status == "accessible"
        else ("Inaccessible" if status == "missing" else "Inconclusive")
    )
    note = resource.get("note") or resource.get("error_message") or ""
    return {
        **resource,
        "resource_type": label,
        "status": status,
        "accessibility_status": broad,
        "note": note,
        "error_message": resource.get("error_message")
        or (note if status != "accessible" else None),
    }


def _write_resources(db, paper_id, resources, replace=False):
    resources = [normalize_resource(resource) for resource in resources]
    if replace:
        urls = [resource["url"] for resource in resources]
        if urls:
            placeholders = ",".join("?" for _ in urls)
            db.execute(
                f"DELETE FROM resources WHERE paper_id=? AND url NOT IN ({placeholders})",
                [paper_id, *urls],
            )
        else:
            db.execute("DELETE FROM resources WHERE paper_id=?", (paper_id,))
    for resource in resources:
        db.execute(
            """INSERT INTO resources
               (paper_id,url,resource_type,accessibility_status,status,http_status,error_message,
                checked_at,evidence,page,final_url,note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(paper_id,url) DO UPDATE SET
                 resource_type=excluded.resource_type,accessibility_status=excluded.accessibility_status,
                 status=excluded.status,http_status=excluded.http_status,error_message=excluded.error_message,
                 checked_at=excluded.checked_at,evidence=excluded.evidence,page=excluded.page,
                 final_url=excluded.final_url,note=excluded.note""",
            (
                paper_id,
                resource["url"],
                resource["resource_type"],
                resource["accessibility_status"],
                resource["status"],
                resource.get("http_status"),
                resource.get("error_message"),
                resource.get("checked_at") or now(),
                resource.get("evidence", ""),
                resource.get("page"),
                resource.get("final_url"),
                resource["note"],
            ),
        )


def _find_existing_paper(db, paper, key, external_id):
    """Preserve existing cross-provider and uploaded-PDF deduplication."""
    from src.paper.discovery import identity

    existing = db.execute(
        "SELECT id FROM papers WHERE identity=? OR (source=? AND external_id=?)",
        (key, paper["source"], external_id),
    ).fetchone()
    if not existing and paper["source"] != "Upload":
        for row in db.execute(
            "SELECT id,title,identity,source FROM papers WHERE source <> 'Upload'"
        ):
            # Merge a title-only provider record with a canonical identifier.
            # Distinct canonical IDs must remain distinct even if titles match.
            if not (key.startswith("title:") or row["identity"].startswith("title:")):
                continue
            if identity({"title": row["title"]}) == identity({"title": paper["title"]}):
                existing = row
                if not key.startswith("title:"):
                    db.execute("UPDATE papers SET identity=? WHERE id=?", (key, row["id"]))
                    if row["source"] == paper["source"]:
                        db.execute(
                            "UPDATE papers SET external_id=? WHERE id=?",
                            (external_id, row["id"]),
                        )
                break
    return existing["id"] if existing else None
