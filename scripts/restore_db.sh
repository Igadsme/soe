#!/usr/bin/env bash
set -euo pipefail
[[ $# == 1 ]] || { echo "Usage: scripts/restore_db.sh PATH_TO_DUMP" >&2; exit 2; }
backup_file=$(realpath -- "$1")
cd "$(dirname "$0")/.."
[[ -f "$backup_file" ]] || { echo 'Backup file not found.' >&2; exit 2; }
printf 'This replaces database objects in the configured production Compose database. Stop API/worker and take a fresh backup first.\n'
read -r -p 'Type RESTORE to continue: ' confirmation
[[ "$confirmation" == RESTORE ]] || { echo 'Restore cancelled.' >&2; exit 1; }
docker compose -f docker-compose.prod.yml exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-acl --exit-on-error --single-transaction' < "$backup_file"
echo 'Restore complete. Apply migrations and verify readiness before restarting writers.'
