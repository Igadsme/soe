#!/usr/bin/env bash
set -euo pipefail
[[ $# == 1 && "$1" != -* ]] || { echo 'Usage: scripts/rollback.sh PREVIOUS_TAG_OR_SHA' >&2; exit 2; }
echo 'Rollback rebuilds the previous Git release. Migrations are forward-only; verify schema compatibility first.'
echo 'For incompatible schemas, stop writers and restore a verified backup, or review alembic downgrade REVISION before executing manually.'
exec "$(dirname "$0")/deploy.sh" "$1"
