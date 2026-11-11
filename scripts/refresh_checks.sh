#!/usr/bin/env bash
# Works from any directory. Run after installing the project's .venv.
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
exec .venv/bin/python -m flask --app run refresh-checks --limit "${1:-20}"
