"""Backed-up, transactional migrations for the original app and supplied two-table schema."""

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = 2


def _columns(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def _needs_migration(db):
    papers = _columns(db, "papers")
    return bool(papers) and (
        not {"identity", "external_id", "last_analyzed_at", "report", "local_path"} <= papers
        or not {"resource_type", "accessibility_status", "status", "note", "page"}
        <= _columns(db, "resources")
    )


def _execute_schema(db, schema):
    for statement in schema.split(";"):
        if statement.strip():
            db.execute(statement)


def _backup(db, path):
    directory = path.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = directory / f"{path.name}.pre-v2-{stamp}-{uuid.uuid4().hex[:8]}.db"
    backup = sqlite3.connect(destination)
    try:
        db.backup(backup)
    finally:
        backup.close()
    return destination


def _migrate(db, schema):
    from src.database.db import DB_STATUSES, RESOURCE_TYPES, _authors, normalize_resource, now
    from src.scoring import calculate_score

    papers = [dict(row) for row in db.execute("SELECT * FROM papers")]
    resources = (
        [dict(row) for row in db.execute("SELECT * FROM resources")]
        if _columns(db, "resources")
        else []
    )
    statements = [statement for statement in schema.split(";") if statement.strip()]
    db.execute(statements[0].replace("IF NOT EXISTS papers", "_papers_v2", 1))
    db.execute(statements[1].replace("IF NOT EXISTS resources", "_resources_v2", 1))
    by_paper = {}
    for original in resources:
        resource = normalize_resource(original)
        resource["category"] = next(
            key for key, label in RESOURCE_TYPES.items() if label == resource["resource_type"]
        )
        by_paper.setdefault(resource["paper_id"], []).append(resource)
        db.execute(
            """INSERT INTO _resources_v2
               (id,paper_id,url,resource_type,accessibility_status,status,http_status,error_message,
                checked_at,evidence,page,final_url,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                resource["id"],
                resource["paper_id"],
                resource["url"],
                resource["resource_type"],
                resource["accessibility_status"],
                resource["status"],
                resource.get("http_status"),
                resource.get("error_message"),
                resource.get("checked_at"),
                resource.get("evidence") or "",
                resource.get("page"),
                resource.get("final_url"),
                resource["note"],
            ),
        )
    for paper in papers:
        source = paper["source"]
        external_id = str(
            paper.get("external_id") or paper.get("identity") or f"legacy:{paper['id']}"
        )
        if (
            not paper.get("external_id")
            and source.casefold() == "arxiv"
            and external_id.startswith("arxiv:")
        ):
            external_id = external_id.removeprefix("arxiv:")
        key = (
            paper.get("identity")
            or "provider:"
            + hashlib.sha256((source.casefold() + ":" + external_id).encode()).hexdigest()
        )
        status = DB_STATUSES.get(
            paper.get("analysis_status", "pending"), paper.get("analysis_status", "pending")
        )
        report = json.loads(paper["report"]) if paper.get("report") else None
        checked = by_paper.get(paper["id"], [])
        if report is None and (checked or status in ("completed", "partial")):
            imported_at = now()
            checked_at = max(
                (r["checked_at"] for r in checked if r.get("checked_at")), default=None
            )
            report = {
                **calculate_score(checked),
                "analyzed_at": paper.get("last_analyzed_at") or checked_at or imported_at,
                "imported_at": imported_at,
                "scope": "Imported resource checks",
                "pages": None,
                "warnings": [
                    "Report reconstructed from saved resource checks; original PDF coverage is unknown."
                ],
                "resource_count": len(checked),
                "candidate_count": len(checked),
                "text_characters": 0,
                "limitations": [
                    "Imported checks retain their original timestamps. No new network checks were performed."
                ],
            }
        last_analyzed = paper.get("last_analyzed_at") or (report or {}).get("analyzed_at")
        db.execute(
            """INSERT INTO _papers_v2
               (id,source,external_id,identity,title,authors,year,abstract,area,paper_url,pdf_url,
                local_path,created_at,last_analyzed_at,analysis_status,report,score)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                paper["id"],
                source,
                external_id,
                key,
                paper["title"],
                json.dumps(_authors(paper.get("authors"))),
                paper.get("year"),
                paper.get("abstract") or "",
                paper.get("area") or "Uncategorized",
                paper.get("paper_url"),
                paper.get("pdf_url"),
                paper.get("local_path"),
                paper.get("created_at") or now(),
                last_analyzed,
                status,
                json.dumps(report) if report is not None else None,
                (
                    paper.get("score")
                    if paper.get("score") is not None
                    else (report or {}).get("score")
                ),
            ),
        )
    if _columns(db, "resources"):
        db.execute("DROP TABLE resources")
    db.execute("DROP TABLE papers")
    db.execute("ALTER TABLE _papers_v2 RENAME TO papers")
    db.execute("ALTER TABLE _resources_v2 RENAME TO resources")


def initialize_database(path, schema):
    from src.database.db import get_connection

    db = get_connection(path)
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
            raise ValueError("This database was created by a newer schema version.")
        db.execute("PRAGMA journal_mode=WAL")
        if _needs_migration(db):
            _backup(db, path)
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            db.execute("BEGIN IMMEDIATE")
            if _needs_migration(db):
                _migrate(db, schema)
            _execute_schema(db, schema)
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Database migration found an invalid foreign-key reference.")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys=ON")
    finally:
        db.close()
