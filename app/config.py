"""Local application defaults. Override secrets and database paths with environment variables."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Config:
    DATABASE = os.environ.get("PAPERLENS_DATABASE", str(ROOT / "data" / "paperlens.db"))
    UPLOAD_FOLDER = str(ROOT / "data" / "uploads")
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024
    MAX_PDF_PAGES = 300
    MAX_RESOURCES = 60
    SEMANTIC_SCHOLAR_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    ANALYSIS_SYNC = False
    TESTING = False
