"""Analysis jobs and atomic report snapshots."""

import json
import http.client
import logging
import os
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import current_app
from src.checker import check_resource
from src.checker.network import download_pdf
from src.database import connection, get_paper, get_resources, now
from src.paper.extractor import extract_paper
from src.parser import links_from_text
from src.scoring import calculate_score

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="paperlens")
_capacity = threading.BoundedSemaphore(4)


def stage(job_id, message):
    """Save a progress message so the browser can display the current step."""
    if job_id:
        with connection() as db:
            db.execute("UPDATE jobs SET stage=? WHERE id=?", (message, job_id))


def analyze(paper_id, applicable, job_id=None):
    """Extract resource links, check their availability, and save one report."""
    paper = get_paper(paper_id)
    if not paper:
        raise ValueError("Paper not found.")
    warnings = []
    resources = {}
    pages = None
    scope = "Abstract only"
    # 1. Read the PDF when available; otherwise use abstract links.
    stage(job_id, "Reading paper and extracting resource links")
    extracted = None
    if paper["local_path"]:
        extracted = extract_paper(paper["local_path"], current_app.config["MAX_PDF_PAGES"])
    elif paper["pdf_url"]:
        try:
            content = download_pdf(paper["pdf_url"])
            with tempfile.NamedTemporaryFile(suffix=".pdf") as temp:
                temp.write(content)
                temp.flush()
                extracted = extract_paper(temp.name, current_app.config["MAX_PDF_PAGES"])
        except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
            warnings.append(
                "PDF could not be analyzed: "
                + str(exc)[:250]
                + " Only abstract links were examined. Upload the PDF for fuller coverage."
            )
    else:
        warnings.append("No open PDF is available. Only links in the abstract were examined.")
    if extracted:
        pages, scope = extracted["pages"], "PDF and abstract"
        warnings.extend(extracted["warnings"])
        for resource in extracted["resources"]:
            resources[resource["url"]] = resource
    for resource in links_from_text(paper["abstract"]):
        resource["evidence"] = "Abstract: " + resource["evidence"]
        resources.setdefault(resource["url"], resource)
    # 2. Prioritize recognized resource types and enforce the check limit.
    candidates = sorted(
        resources.values(),
        key=lambda resource: (resource["category"] == "other", resource["url"]),
    )
    maximum = current_app.config["MAX_RESOURCES"]
    if len(candidates) > maximum:
        warnings.append(
            f"Found {len(candidates)} links; checked the first {maximum}, prioritizing resource categories."
        )
    candidates = candidates[:maximum]
    # Preserve categories corrected by the reviewer on earlier runs.
    previous = {resource["url"]: resource["category"] for resource in get_resources(paper_id)}
    for resource in candidates:
        if resource["url"] in previous:
            resource["category"] = previous[resource["url"]]
    if not candidates:
        warnings.append(
            "No HTTP resource links were detected. This does not establish that resources do not exist."
        )
    # 3. Check the links concurrently, then calculate the score.
    stage(job_id, f"Checking {len(candidates)} public resource links")
    with ThreadPoolExecutor(max_workers=4) as checks:
        checked = list(checks.map(check_resource, candidates))
    score = calculate_score(checked, applicable)
    report = {
        **score,
        "analyzed_at": now(),
        "pages": pages,
        "scope": scope,
        "warnings": warnings,
        "resource_count": len(checked),
        "candidate_count": len(resources),
        "text_characters": len(extracted["text"]) if extracted else 0,
        "limitations": [
            "Links may belong to cited work rather than this paper; review the evidence and category.",
            "Classification uses URL and nearby-text heuristics. Categories can be corrected below.",
            "An HTTP success does not verify files, licensing, completeness, or experiment results.",
            "No OCR, code execution, or full dataset downloads are performed.",
        ],
    }
    # 4. Save resources and report together so a failed refresh cannot erase
    # the previous successful report.
    stage(job_id, "Saving report")
    with connection() as db:
        db.execute("DELETE FROM resources WHERE paper_id=?", (paper_id,))
        for resource in checked:
            db.execute(
                """INSERT INTO resources
                (paper_id,url,category,evidence,page,status,http_status,final_url,checked_at,note)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    paper_id,
                    resource["url"],
                    resource["category"],
                    resource.get("evidence", ""),
                    resource.get("page"),
                    resource["status"],
                    resource["http_status"],
                    resource["final_url"],
                    resource["checked_at"],
                    resource["note"],
                ),
            )
        db.execute(
            "UPDATE papers SET report=?,score=?,analysis_status=? WHERE id=?",
            (json.dumps(report), report["score"], "complete" if extracted else "partial", paper_id),
        )
    return report


def _run(app, paper_id, applicable, job_id):
    """Run a queued analysis inside Flask and record success or failure."""
    with app.app_context():
        try:
            with connection() as db:
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
            analyze(paper_id, applicable, job_id)
            with connection() as db:
                db.execute(
                    "UPDATE jobs SET status='completed',stage='Report ready',finished_at=? WHERE id=?",
                    (now(), job_id),
                )
        except Exception:
            logging.exception("Analysis failed for paper %s", paper_id)
            with connection() as db:
                db.execute("UPDATE papers SET analysis_status='failed' WHERE id=?", (paper_id,))
                db.execute(
                    "UPDATE jobs SET status='failed',stage='Analysis failed',error=?,finished_at=? WHERE id=?",
                    (
                        "The paper could not be analyzed. Check the PDF and retry; any earlier report is preserved.",
                        now(),
                        job_id,
                    ),
                )
        finally:
            _capacity.release()


def start_analysis(paper_id, applicable):
    """Queue an analysis unless this paper already has an active job."""
    if not _capacity.acquire(blocking=False):
        raise ValueError(
            "The analysis queue is full. Please wait for a current analysis to finish."
        )
    job_id = str(os.getpid()) + "-" + uuid.uuid4().hex
    try:
        with connection() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT id FROM jobs WHERE paper_id=? AND status IN ('queued','running')",
                (paper_id,),
            ).fetchone()
            if active:
                _capacity.release()
                return active["id"]
            db.execute(
                "INSERT INTO jobs (id,paper_id,status,stage,created_at) VALUES (?,?,?,?,?)",
                (job_id, paper_id, "queued", "Waiting for analysis", now()),
            )
            db.execute("UPDATE papers SET analysis_status='analyzing' WHERE id=?", (paper_id,))
    except Exception:
        _capacity.release()
        raise
    app = current_app._get_current_object()
    if app.config["ANALYSIS_SYNC"]:
        _run(app, paper_id, applicable, job_id)
    else:
        try:
            _executor.submit(_run, app, paper_id, applicable, job_id)
        except RuntimeError:
            _capacity.release()
            with connection() as db:
                db.execute(
                    "UPDATE jobs SET status='failed',error=?,finished_at=? WHERE id=?",
                    ("Worker unavailable. Restart the app and retry.", now(), job_id),
                )
                db.execute("UPDATE papers SET analysis_status='failed' WHERE id=?", (paper_id,))
    return job_id


def latest_job(paper_id):
    """Return the most recent analysis job for a paper."""
    with connection() as db:
        row = db.execute(
            "SELECT * FROM jobs WHERE paper_id=? ORDER BY rowid DESC LIMIT 1", (paper_id,)
        ).fetchone()
    return dict(row) if row else None


def rescore(paper_id):
    """Recalculate a saved score after the reviewer changes a category."""
    paper = get_paper(paper_id)
    if not paper["report"]:
        return
    report = paper["report"]
    report.update(calculate_score(get_resources(paper_id), report["applicable"]))
    report["reviewed_at"] = now()
    with connection() as db:
        db.execute(
            "UPDATE papers SET report=?,score=? WHERE id=?",
            (json.dumps(report), report["score"], paper_id),
        )
