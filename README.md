# Research Paper Reproducibility Explorer — PaperLens

A local Flask workspace for finding open-access research, extracting linked resources, checking accessibility, and generating explainable reproducibility-readiness reports.

## Run it (Ubuntu / WSL)

Python 3.10 or newer is required. From the project root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
python run.py
```

Open **http://127.0.0.1:5000/**.

The installed environment is already available in `.venv` in this workspace. On subsequent runs:

```bash
source .venv/bin/activate
python run.py
```

For development with automatic Python reload:

```bash
python -m flask --app run run --debug
```

Refresh the browser after HTML/CSS edits. Debug mode is for local development.

## What you can do

1. **Discover:** search arXiv, Semantic Scholar, or both. Up to 10 results per source are normalized, deduplicated, saved, and cached in memory for 10 minutes.
2. **Upload:** add your own PDF (20 MB / 300 pages maximum), with optional title, area, and year.
3. **Analyze:** choose applicable resource categories, then start a background analysis. The page shows progress and refreshes when the report is ready.
4. **Inspect:** review resource URLs, categories, source pages, extraction evidence, HTTP responses, and UTC check timestamps. Correct a category to recalculate the score.
5. **Report:** download the full JSON report or use Print / save PDF for a printable report.
6. **Library:** search saved metadata and filter by source, research area, year, minimum score, and analysis status. Export the filtered results to CSV.
7. **Analytics:** compare mean readiness by area and year with interactive, locally served Plotly charts and accessible tables. See resource status totals.
8. **Refresh:** recheck a report manually or schedule report refreshes through cron.

Searching saves paper metadata, not PDF files. Uploaded PDFs stay on disk. Remote PDFs are downloaded only during analysis and removed after extraction.

## Readiness score

Default weights:

| Category | Weight |
| --- | ---: |
| Source code | 35 |
| Datasets | 35 |
| Documentation | 20 |
| Supplementary material | 10 |

`score = round(100 × earned applicable weights / total applicable weights)`

A category earns its weight when at least one matching resource returns HTTP 2xx. A reviewer can exclude categories that do not apply; exclusions remove their weight from the denominator. “Other” links never contribute points. Multiple links in one category cannot inflate the score.

All categories are initially selected for review; the application does not infer what a paper requires. Compare the selections and report scope alongside the score. A high score does **not** establish that experiments can be reproduced.

Accessibility states:

- **Accessible:** HTTP 2xx; resource content, completeness, and licensing are not audited.
- **Missing:** HTTP 404/410.
- **Restricted:** HTTP 401/403/429; login, bot protection, or rate limiting may apply.
- **Unresolved:** timeouts, DNS/TLS problems, unexpected responses, and other uncertain results.
- **Blocked:** a private, local, reserved, credential-bearing, or unsupported destination.

Restricted and unresolved resources are uncertain, not proven absent. Their categories earn no verified points.

## Configuration

`.env.example` lists supported settings. Environment variables must be exported in your shell; the file is not loaded automatically.

```bash
export SEMANTIC_SCHOLAR_API_KEY="your-key"
# Optional:
export SECRET_KEY="a-long-random-secret"
export PAPERLENS_DATABASE="/absolute/path/to/paperlens.db"
python run.py
```

Without `SECRET_KEY`, a random local key is persisted next to the database. Do not commit it.

Both providers can rate-limit requests. The app preserves partial results when one provider fails and never substitutes fabricated results. arXiv calls are spaced at least 3 seconds apart per process; successful searches are cached. Semantic Scholar may require an API key or time before retrying. Official references: [arXiv API manual](https://info.arxiv.org/help/api/user-manual.html), [Semantic Scholar API](https://api.semanticscholar.org/api-docs/graph).

Live verification during implementation found both search providers returning HTTP 429 from this environment. Provider parsing and failure behavior are also covered by offline tests. Live public resource checks succeeded. Provider availability is external to this application.

## Storage and background work

- SQLite database: `data/paperlens.db`
- Uploaded PDFs: `data/uploads/`
- Generated signing key: `data/.secret_key`
- Browser screenshots and tooling: `.cache/`

These generated files are ignored by Git. Back up the database and uploaded PDFs together while the application is stopped.

Two analyses can run concurrently, with up to four active/queued jobs. Each analysis checks up to 60 links with four bounded network workers. Jobs persist their progress in SQLite; jobs whose owning process has exited are marked interrupted on startup and can be retried. A failed refresh preserves the last saved report.

This is a personal **single-server-process** application, without authentication or multi-user isolation. Keep it on localhost. A public multi-user service would need authentication, a separate durable task queue, worker isolation, and operational rate limiting.

## Scheduled refreshes

Run manually:

```bash
bash scripts/refresh_checks.sh 20
# Equivalent:
.venv/bin/python -m flask --app run refresh-checks --limit 20
```

This refreshes up to 20 reports, ordered by the oldest check first. Failures are logged, existing reports are preserved, and the command exits nonzero if any refresh fails.

Example daily cron entry; replace the path with your checkout:

```cron
0 3 * * * /bin/bash /absolute/path/to/project/scripts/refresh_checks.sh 20 >> /absolute/path/to/project/data/refresh.log 2>&1
```

Cron is not installed automatically. WSL must be running and its cron service enabled for scheduled jobs to execute.

## Tests

The core suite uses only the installed runtime dependencies and Python's standard library:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

It uses temporary databases, generated real PDFs, and mocked external HTTP results. It covers uploads, extraction, scoring, category corrections, API normalization, filtering, CSV/JSON exports, network safeguards, errors, and scheduled refreshes.

Browser verification:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
PLAYWRIGHT_BROWSERS_PATH=.cache/ms-playwright .venv/bin/python -m playwright install chromium
PLAYWRIGHT_BROWSERS_PATH=.cache/ms-playwright .venv/bin/python -m scripts.browser_check
```

The browser check uses a temporary library and explicitly simulated provider/resource responses. It verifies the upload → background analysis → report workflow, downloads, filters, charts, JavaScript errors, and overflow at 320, 390, 768, 1024, and 1440 px. Screenshots are written to `.cache/verification/`; fixture data never enters the real library.

## Architecture

```text
app/
  __init__.py           App factory, CSRF protection, CLI refresh
  config.py             Environment-backed defaults and limits
  routes.py             Discovery, upload, report, library, export, analytics
src/
  analysis.py           Background jobs and atomic report snapshots
  paper/
    arxiv_client.py     arXiv Atom metadata client
    discovery.py        Semantic Scholar, deduplication, provider cache
    extractor.py        PDF text, annotation, and plain-text URL extraction
  parser/               URL normalization and heuristic classification
  checker/              Bounded, DNS-pinned public HTTP checks
  scoring/              Versioned readiness rubric
  database/             SQLite schema, queries, and transactions
templates/              Server-rendered pages and reusable cards
static/                 Responsive CSS and progressive JavaScript
scripts/                PDF demo, cron entry point, browser verification
tests/                  Offline regression suite
```

`requirements.txt` specifies compatible runtime ranges; `requirements-lock.txt` records the versions verified in this environment. `requirements-dev.txt` adds optional browser testing.

## Practical limits

- Links may refer to cited papers; review evidence and move unrelated links to “Other.”
- Classification is heuristic. OCR, experiment execution, full dataset downloads, and license validation are not included.
- Generic web pages and repository READMEs are not crawled for additional links.
- If a remote PDF is unavailable, the report is explicitly **abstract only**; upload the PDF for fuller coverage.
- PDF text extraction is limited to 3 million characters. PDFs over the page/size limit are rejected. At most 60 links are checked, prioritizing recognized resource categories; truncation is reported.
- Network checks use HEAD, with a small ranged GET fallback for HTTP 405/501. Redirects are capped and public destination addresses are validated and pinned per hop. No private-network fetching or environment proxy is used for untrusted links.
- Analytics describe the saved sample only. Different category selections, PDF coverage, and provider-specific area labels limit comparisons. Tables show sample counts.

## Standalone demos

Run from the project root with the virtual environment active:

```bash
python -m scripts.extract_demo
python -m src.paper.arxiv_client
```
