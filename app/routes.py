"""Web routes for discovery, reports, uploads, library, exports, and analytics."""

import csv
import hashlib
import io
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pymupdf as fitz
from flask import (
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from src.analysis import latest_job, rescore, start_analysis
from src.database import (
    connection,
    facets,
    get_paper,
    get_resources,
    library_stats,
    list_papers,
    save_paper,
)
from src.paper.discovery import discover
from src.parser import CATEGORIES
from src.scoring import WEIGHTS


def paper_or_404(paper_id):
    """Load a saved paper or return a page-not-found response."""
    paper = get_paper(paper_id)
    if not paper:
        abort(404)
    return paper


def filters():
    """Read and validate the library filters submitted in the URL."""
    result = {
        key: request.args.get(key, "").strip()
        for key in ("q", "source", "area", "year", "min_score", "sort", "analysis_status")
    }
    result["q"] = result["q"][:200]
    for key, low, high in (("year", 1000, 9999), ("min_score", 0, 100)):
        if result[key]:
            try:
                value = int(result[key])
                if not low <= value <= high:
                    raise ValueError
            except ValueError:
                abort(400, description="Year or minimum score is invalid.")
    return result


def register_routes(app):
    """Register view functions directly on each Flask application instance."""

    @app.route("/", methods=["GET"])
    def home():
        papers = list_papers()
        return render_template(
            "index.html",
            recent=papers[:3],
            **library_stats(),
        )

    @app.route("/library/stats", methods=["GET"])
    def library_statistics():
        """Return current counters for pages left open during background analysis."""
        return jsonify(library_stats())

    @app.route("/search", methods=["GET"])
    def search():
        query = request.args.get("q", "").strip()
        source = request.args.get("source", "arXiv")
        if source not in ("arXiv", "Semantic Scholar", "both") or len(query) > 200:
            abort(400, description="Choose a valid source and a query of up to 200 characters.")
        papers, errors = [], []
        if query:
            found, errors = discover(query, source)
            papers = [get_paper(save_paper(paper)) for paper in found]
        return render_template(
            "search.html", papers=papers, errors=errors, query=query, source=source
        )

    @app.route("/library", methods=["GET"])
    def library():
        selected = filters()
        return render_template(
            "library.html", papers=list_papers(selected), filters=selected, facets=facets()
        )

    @app.route("/papers/add", methods=["GET", "POST"])
    def add_paper():
        """Save user-entered metadata and an optional PDF without discovery API calls."""
        limits = {
            "title": 300,
            "authors": 2000,
            "year": 4,
            "area": 80,
            "paper_url": 2048,
            "pdf_url": 2048,
            "abstract": 20000,
        }
        values = {field: request.form.get(field, "").strip() for field in limits}
        errors = {}
        if request.method == "GET":
            return render_template("add_paper.html", values=values, errors=errors)
        for field, maximum in limits.items():
            if len(values[field]) > maximum:
                errors[field] = f"Use no more than {maximum} characters."
        if not values["title"]:
            errors["title"] = "Enter the paper title."
        year = None
        if values["year"]:
            if not re.fullmatch(r"[1-9][0-9]{3}", values["year"]):
                errors["year"] = "Enter a four-digit publication year."
            else:
                year = int(values["year"])
        for field in ("paper_url", "pdf_url"):
            value = values[field]
            if not value:
                continue
            try:
                parts = urlsplit(value)
                _ = parts.port
                if (
                    parts.scheme not in ("http", "https")
                    or not parts.hostname
                    or parts.username is not None
                    or parts.password is not None
                    or any(char.isspace() or ord(char) < 32 for char in value)
                ):
                    raise ValueError
            except ValueError:
                errors[field] = "Enter a complete HTTP or HTTPS URL without a username or password."
        file = request.files.get("pdf")
        data = None
        if not errors and file and file.filename:
            try:
                if not file.filename.lower().endswith(".pdf"):
                    raise ValueError("Choose a PDF file.")
                data = file.read(current_app.config["MAX_CONTENT_LENGTH"] + 1)
                if len(data) > current_app.config["MAX_CONTENT_LENGTH"]:
                    abort(413)
                if b"%PDF-" not in data[:1024]:
                    raise ValueError("The file is not a PDF.")
                with fitz.open(stream=data, filetype="pdf") as document:
                    if document.needs_pass:
                        raise ValueError("Password-protected PDFs are not supported.")
                    if (
                        not document.is_pdf
                        or not 1 <= len(document) <= current_app.config["MAX_PDF_PAGES"]
                    ):
                        raise ValueError("Choose a PDF with 1 to 300 pages.")
            except (ValueError, RuntimeError, fitz.FileDataError) as error:
                errors["pdf"] = "Could not read this PDF: " + str(error)[:250]
        if errors:
            return render_template("add_paper.html", values=values, errors=errors), 400
        paper = {
            "title": values["title"],
            "authors": [
                name.strip() for name in re.split(r"[;\r\n]+", values["authors"]) if name.strip()
            ],
            "year": year,
            "area": values["area"] or "Uncategorized",
            "paper_url": values["paper_url"] or None,
            "pdf_url": values["pdf_url"] or None,
            "abstract": values["abstract"],
            "source": "Manual",
        }
        path = None
        if data is not None:
            paper["upload_hash"] = hashlib.sha256(data).hexdigest()
            path = Path(current_app.config["UPLOAD_FOLDER"]) / (uuid.uuid4().hex + ".pdf")
            path.write_bytes(data)
            paper["local_path"] = str(path)
        try:
            paper_id = save_paper(paper)
            saved = get_paper(paper_id)
            if path and saved["local_path"] != str(path):
                # A title-only manual entry can receive its PDF on a later submission.
                with connection() as db:
                    attached = db.execute(
                        "UPDATE papers SET local_path=? WHERE id=? AND local_path IS NULL",
                        (str(path), paper_id),
                    ).rowcount
                if not attached:
                    path.unlink(missing_ok=True)
        except Exception:
            if path:
                path.unlink(missing_ok=True)
            raise
        flash(
            "Paper saved to your library. Choose its resource categories to start analysis.",
            "success",
        )
        return redirect(url_for("detail", paper_id=paper_id))

    @app.route("/upload", methods=["POST"])
    def upload():
        """Validate an uploaded PDF, avoid duplicates, and save its metadata."""
        file = request.files.get("pdf")
        if not file or not file.filename or Path(file.filename).suffix.lower() != ".pdf":
            flash("Choose a PDF file to upload.", "error")
            return redirect(url_for("home"))
        year = request.form.get("year", "").strip()
        if year:
            try:
                year = int(year)
                if not 1000 <= year <= 9999:
                    raise ValueError
            except ValueError:
                flash("Enter a valid four-digit publication year.", "error")
                return redirect(url_for("home", _anchor="upload"))
        else:
            year = None
        data = file.read(current_app.config["MAX_CONTENT_LENGTH"] + 1)
        if len(data) > current_app.config["MAX_CONTENT_LENGTH"]:
            abort(413)
        try:
            if b"%PDF-" not in data[:1024]:
                raise ValueError("The file is not a PDF.")
            with fitz.open(stream=data, filetype="pdf") as document:
                if document.needs_pass:
                    raise ValueError("Password-protected PDFs are not supported.")
                if (
                    not document.is_pdf
                    or not 1 <= len(document) <= current_app.config["MAX_PDF_PAGES"]
                ):
                    raise ValueError("Choose a PDF with 1 to 300 pages.")
                title = (
                    request.form.get("title", "").strip()[:300]
                    or (document.metadata.get("title") or "").strip()[:300]
                    or Path(file.filename).stem[:300]
                )
        except (ValueError, RuntimeError, fitz.FileDataError) as exc:
            flash("Could not read this PDF: " + str(exc)[:250], "error")
            return redirect(url_for("home"))
        fingerprint = hashlib.sha256(data).hexdigest()
        with connection() as db:
            existing = db.execute(
                "SELECT id FROM papers WHERE identity=?", ("upload:" + fingerprint,)
            ).fetchone()
        if existing:
            flash("This PDF is already in your library.", "info")
            return redirect(url_for("detail", paper_id=existing["id"]))
        path = Path(current_app.config["UPLOAD_FOLDER"]) / (uuid.uuid4().hex + ".pdf")
        path.write_bytes(data)
        try:
            paper_id = save_paper(
                {
                    "title": title,
                    "authors": [],
                    "source": "Upload",
                    "upload_hash": fingerprint,
                    "local_path": str(path),
                    "year": year,
                    "area": request.form.get("area", "").strip()[:80] or "Uncategorized",
                }
            )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        flash("PDF saved. Choose applicable categories and start analysis.", "success")
        return redirect(url_for("detail", paper_id=paper_id))

    @app.route("/papers/<int:paper_id>", methods=["GET"])
    def detail(paper_id):
        paper = paper_or_404(paper_id)
        return render_template(
            "paper.html",
            paper=paper,
            resources=get_resources(paper_id),
            job=latest_job(paper_id),
            categories=CATEGORIES,
            weights=WEIGHTS,
        )

    @app.route("/papers/<int:paper_id>/analyze", methods=["POST"])
    def analyze_paper(paper_id):
        """Start analysis with the resource categories selected by the reviewer."""
        paper_or_404(paper_id)
        applicable = [
            category for category in request.form.getlist("applicable") if category in WEIGHTS
        ]
        if not applicable:
            flash("Select at least one applicable category.", "error")
        else:
            try:
                start_analysis(paper_id, applicable)
            except ValueError as exc:
                flash(str(exc), "error")
        return redirect(url_for("detail", paper_id=paper_id))

    @app.route("/papers/<int:paper_id>/status", methods=["GET"])
    def analysis_status(paper_id):
        paper = paper_or_404(paper_id)
        return jsonify(status=paper["analysis_status"], job=latest_job(paper_id))

    @app.route("/resources/<int:resource_id>/category", methods=["POST"])
    def update_category(resource_id):
        """Save a reviewed resource category and refresh the paper's score."""
        category = request.form.get("category")
        if category not in CATEGORIES:
            abort(400, description="Invalid resource category.")
        with connection() as db:
            resource = db.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
            if not resource:
                abort(404)
            paper_id = resource["paper_id"]
            if (
                db.execute("SELECT analysis_status FROM papers WHERE id=?", (paper_id,)).fetchone()[
                    0
                ]
                == "analyzing"
            ):
                flash("Wait for analysis to finish before editing categories.", "info")
                return redirect(url_for("detail", paper_id=paper_id))
            db.execute("UPDATE resources SET category=? WHERE id=?", (category, resource_id))
        rescore(paper_id)
        flash("Category updated and score recalculated.", "success")
        return redirect(url_for("detail", paper_id=paper_id, _anchor="resources"))

    @app.route("/papers/<int:paper_id>/report.json", methods=["GET"])
    def report_json(paper_id):
        paper = paper_or_404(paper_id)
        if not paper["report"]:
            abort(404, description="Analyze the paper before downloading its report.")
        paper.pop("local_path", None)
        return Response(
            json.dumps({"paper": paper, "resources": get_resources(paper_id)}, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="paperlens-{paper_id}.json"'},
        )

    def csv_safe(value):
        """Prevent spreadsheet applications from interpreting exported text as formulas."""
        value = "" if value is None else str(value)
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value

    @app.route("/library.csv", methods=["GET"])
    def library_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "Title",
                "Authors",
                "Year",
                "Area",
                "Source",
                "Status",
                "Readiness score",
                "Analyzed at",
                "Paper URL",
            ]
        )
        for paper in list_papers(filters()):
            writer.writerow(
                [
                    csv_safe(value)
                    for value in (
                        paper["title"],
                        "; ".join(paper["authors"]),
                        paper["year"],
                        paper["area"],
                        paper["source"],
                        paper["analysis_status"],
                        paper["score"],
                        (paper["report"] or {}).get("analyzed_at"),
                        paper["paper_url"],
                    )
                ]
            )
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="paperlens-library.csv"'},
        )

    @app.route("/analytics", methods=["GET"])
    def analytics():
        """Summarize saved reports and prepare the comparison charts."""
        from collections import Counter, defaultdict
        import plotly.graph_objects as go
        from plotly.utils import PlotlyJSONEncoder

        papers = list_papers()
        analyzed = [paper for paper in papers if paper["report"]]
        groups = {}
        for field in ("area", "year"):
            bucket = defaultdict(list)
            for paper in analyzed:
                if paper[field] is not None:
                    bucket[str(paper[field])].append(paper["score"])
            groups[field] = {
                key: {"mean": round(sum(values) / len(values), 1), "count": len(values)}
                for key, values in sorted(bucket.items())
            }
        status_counts = Counter()
        for paper in analyzed:
            status_counts.update(resource["status"] for resource in get_resources(paper["id"]))
        charts = []
        for field, title in (
            ("area", "Readiness by research area"),
            ("year", "Readiness by publication year"),
        ):
            group = groups[field]
            fig = go.Figure(
                go.Bar(
                    x=list(group),
                    y=[row["mean"] for row in group.values()],
                    text=[f'n={row["count"]}' for row in group.values()],
                    marker_color="#2563eb",
                )
            )
            fig.update_layout(
                title=title,
                yaxis={"title": "Mean readiness score", "range": [0, 105]},
                template="plotly_white",
                margin={"l": 50, "r": 20, "t": 60, "b": 100},
                height=360,
                font={"family": "Arial"},
            )
            charts.append(json.loads(json.dumps(fig.to_plotly_json(), cls=PlotlyJSONEncoder)))
        return render_template(
            "analytics.html",
            total=len(papers),
            analyzed=len(analyzed),
            average=(
                round(sum(p["score"] for p in analyzed) / len(analyzed), 1) if analyzed else None
            ),
            charts=charts,
            groups=groups,
            statuses=status_counts,
        )

    @app.route("/assets/plotly.js", methods=["GET"])
    def plotly_asset():
        from plotly.offline import get_plotlyjs

        return Response(
            get_plotlyjs(),
            mimetype="application/javascript",
            headers={"Cache-Control": "public, max-age=86400"},
        )
