from pathlib import Path
import hmac
import os
import secrets

import click
from flask import Flask, abort, request, session

from app.config import Config


def create_app(test_config=None):
    project_root = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        template_folder=str(project_root / "templates"),
        static_folder=str(project_root / "static"),
    )
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    if not app.config.get("SECRET_KEY"):
        secret_path = Path(app.config["DATABASE"]).parent / ".secret_key"
        try:
            with secret_path.open("x") as file:
                file.write(secrets.token_hex(32))
            secret_path.chmod(0o600)
        except FileExistsError:
            pass
        app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secret_path.read_text().strip()

    from src.database import connection, init_db, now

    with app.app_context():
        init_db()
        # A second app/CLI process must not interrupt the live server's jobs.
        # Job IDs include the worker PID so only abandoned jobs are recovered.
        with connection() as db:
            active = db.execute(
                "SELECT id,paper_id FROM jobs WHERE status IN ('queued','running')"
            ).fetchall()
            for job in active:
                try:
                    owner = int(job["id"].split("-", 1)[0])
                    os.kill(owner, 0)
                    alive = True
                except (ValueError, ProcessLookupError):
                    alive = False
                except PermissionError:
                    alive = True
                if not alive:
                    db.execute(
                        "UPDATE papers SET analysis_status='failed' WHERE id=?", (job["paper_id"],)
                    )
                    db.execute(
                        """UPDATE jobs SET status='failed',stage='Interrupted',error=?,
                                  finished_at=? WHERE id=?""",
                        (
                            "Analysis was interrupted by an application restart. Please retry.",
                            now(),
                            job["id"],
                        ),
                    )

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(32)
        return session["csrf_token"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def protect_forms():
        if request.method == "POST":
            expected = session.get("csrf_token", "")
            supplied = request.form.get("csrf_token", "")
            if not expected or not hmac.compare_digest(expected, supplied):
                abort(400, description="Your form session expired. Reload the page and try again.")

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    from app.routes import main

    app.register_blueprint(main)

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(500)
    def error_page(error):
        code = error.code
        message = (
            "PDF uploads must be 20 MB or smaller."
            if code == 413
            else "Something went wrong. Please retry." if code == 500 else error.description
        )
        return app.jinja_env.get_template("error.html").render(code=code, message=message), code

    @app.cli.command("refresh-checks")
    @click.option("--limit", default=20, type=click.IntRange(1, 100))
    def refresh_checks(limit):
        """Refresh saved reports, suitable for a local cron job."""
        from src.analysis import analyze
        from src.database import list_papers

        papers = [p for p in list_papers() if p["report"] and p["analysis_status"] != "analyzing"]
        papers.sort(key=lambda paper: paper["report"]["analyzed_at"])
        papers = papers[:limit]
        failures = 0
        for paper in papers:
            try:
                analyze(paper["id"], paper["report"]["applicable"])
                click.echo("Updated: " + paper["title"])
            except Exception:
                failures += 1
                app.logger.exception("Scheduled refresh failed for %s", paper["id"])
        if failures:
            raise click.ClickException(f"{failures} reports could not be refreshed.")

    return app
