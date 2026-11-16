#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
backup_dir=${SOE_BACKUP_DIR:-./backups}
retention_days=${SOE_BACKUP_RETENTION_DAYS:-14}
[[ "$retention_days" =~ ^[0-9]+$ ]] || { echo 'Retention must be a nonnegative number of days.' >&2; exit 2; }
mkdir -p "$backup_dir"
backup_file="$backup_dir/soe-$(date -u +%Y%m%dT%H%M%SZ)-$$.dump"
trap 'rm -f "$backup_file.partial"' EXIT
docker compose -f docker-compose.prod.yml exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$backup_file.partial"
mv "$backup_file.partial" "$backup_file"
# 0 disables retention deletion. Only this script's completed dumps are eligible.
if (( 10#$retention_days > 0 )); then
  find "$backup_dir" -maxdepth 1 -type f -name 'soe-*.dump' -mtime "+$retention_days" -delete
fi
printf 'Backup saved: %s\n' "$backup_file"
