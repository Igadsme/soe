#!/bin/sh
set -eu
umask 077
interval=${SOE_BACKUP_INTERVAL_SECONDS:-86400}
retention=${SOE_BACKUP_RETENTION_DAYS:-14}
case "$interval" in ''|*[!0-9]*|0) echo 'Invalid backup interval' >&2; exit 2;; esac
[ "$interval" -gt 0 ] || { echo 'Backup interval must be positive' >&2; exit 2; }
case "$retention" in ''|*[!0-9]*) echo 'Invalid backup retention' >&2; exit 2;; esac
mkdir -p /backups
partial=
plain=
input=
trap 'rm -f "$partial" "$plain" "$input"' EXIT
trap 'exit 143' TERM INT
fail() { echo 'ERROR: backup failed; inspect database, disk and off-site configuration (credentials omitted).' >&2; exit 1; }
while :; do
  plain="/backups/soe-$(date -u +%Y%m%dT%H%M%SZ)-$$.dump"
  partial="$plain.partial"
  pg_dump --format=custom --file="$partial" 2>/dev/null || fail
  pg_restore --list "$partial" >/dev/null 2>&1 || fail
  if [ -n "${SOE_BACKUP_ENCRYPTION_PASSPHRASE:-}" ]; then
    input="$partial"
    partial="$plain.gpg.partial"
    printf '%s\n' "$SOE_BACKUP_ENCRYPTION_PASSPHRASE" | gpg --batch --yes --pinentry-mode loopback --passphrase-fd 0 --symmetric --cipher-algo AES256 --output "$partial" "$input" 2>/dev/null || fail
    rm -f "$input"
    final="$plain.gpg"
  else
    final="$plain"
  fi
  mv "$partial" "$final"
  # Never delete the completed local recovery copy if upload fails.
  plain=
  input=
  if [ -n "${SOE_BACKUP_S3_BUCKET:-}" ]; then
    export RCLONE_CONFIG_OFFSITE_TYPE=s3
    export RCLONE_CONFIG_OFFSITE_PROVIDER=Other
    export RCLONE_CONFIG_OFFSITE_ENDPOINT="${SOE_BACKUP_S3_ENDPOINT:-}"
    export RCLONE_CONFIG_OFFSITE_REGION="${SOE_BACKUP_S3_REGION:-us-east-1}"
    export RCLONE_CONFIG_OFFSITE_ACCESS_KEY_ID="${SOE_BACKUP_S3_ACCESS_KEY_ID:-}"
    export RCLONE_CONFIG_OFFSITE_SECRET_ACCESS_KEY="${SOE_BACKUP_S3_SECRET_ACCESS_KEY:-}"
    destination="offsite:${SOE_BACKUP_S3_BUCKET}/${SOE_BACKUP_S3_PREFIX:-soe}/$(basename "$final")"
    rclone copyto "$final" "$destination" --s3-no-check-bucket >/dev/null 2>&1 || fail
  fi
  if [ "$retention" -gt 0 ]; then
    find /backups -maxdepth 1 -type f \( -name 'soe-*.dump' -o -name 'soe-*.dump.gpg' \) -mtime "+$retention" -delete
  fi
  echo "Backup completed at $(date -u +%FT%TZ)"
  sleep "$interval" & wait $!
done
