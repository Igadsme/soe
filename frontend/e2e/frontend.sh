#!/usr/bin/env bash
# Serves the production standalone build (same artifact as the container image).
set -euo pipefail
cd "$(dirname "$0")/.."
test -f .next/standalone/server.js || { echo "run 'npm run build' first" >&2; exit 1; }
rm -rf .next/standalone/.next/static && cp -r .next/static .next/standalone/.next/static
[ -d public ] && cp -r public .next/standalone/ || true
PORT=3000 HOSTNAME=localhost exec node .next/standalone/server.js
