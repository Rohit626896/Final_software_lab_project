#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
if [[ ! -x .venv/bin/python ]]; then
    echo "Create .venv and install requirements-lock.txt first. See README.md." >&2
    exit 1
fi
exec .venv/bin/python run.py
