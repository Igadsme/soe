#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
ruff check backend/app
PYTHONPATH=backend pytest -q backend/tests tests --cov=backend/app --cov-report=term-missing --cov-report=xml
pip-audit -r backend/requirements.txt
npm --prefix frontend ci
npm --prefix frontend audit --omit=dev
npm --prefix frontend run typecheck
npm --prefix frontend run build
