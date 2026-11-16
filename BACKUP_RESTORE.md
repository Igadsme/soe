# PostgreSQL backup and restore

These scripts use `docker compose -f docker-compose.prod.yml exec -T db` and the container's PostgreSQL environment. They target the bundled Compose database, not an external managed PostgreSQL URL. Run from a trusted host with Docker access and a configured `.env`; never commit credentials or dumps.

## Backup

1. Start the database and verify available disk space. Run `scripts/backup_db.sh`.
2. Dumps use PostgreSQL custom format and UTC timestamped names under `./backups`, with restrictive permissions. Incomplete dumps are removed and never rotated as successful backups.
3. Optionally set `SOE_BACKUP_DIR=/secure/backup/path` and `SOE_BACKUP_RETENTION_DAYS=30`. Default retention is 14 days; `0` disables deletion. Rotation only touches `soe-*.dump` files in that directory.
4. Schedule backups with your scheduler, alert on nonzero exit or missing/stale dumps, and record recovery point/time objectives. Check archives with `pg_restore --list` and test restoration regularly; a successful dump alone is not proof of recovery.

## Restore (destructive to existing database objects)

1. Restore first into an isolated Compose project/database to verify recovery. Use a PostgreSQL version compatible with the dump. Ensure `.env` and the Compose project identify the intended target.
2. Stop writers: `docker compose -f docker-compose.prod.yml stop api worker`. Quiesce external writers too.
3. Take a fresh backup of the target with `scripts/backup_db.sh` and retain it separately.
4. Run `scripts/restore_db.sh /secure/path/soe-TIMESTAMP.dump`. Review the target and type `RESTORE` at the safety prompt. The script uses `pg_restore --clean --if-exists --single-transaction --exit-on-error`; it restores into the configured existing database, without restoring ownership or ACLs. Reapply least-privilege grants separately where needed.
5. Apply current migrations with `docker compose -f docker-compose.prod.yml run --rm migrate`, restart API/worker, check `/ready`, log in and verify source/rights/clip records and representative media. Record timing and results.
6. After rollback to an older backup, rotate `SOE_SESSION_SECRET` to invalidate restored sessions (including tokens revoked after the backup). Decide whether restored credentials and downstream publication jobs require reconciliation before restarting writers.

## Owner actions

- Encrypt archives using an approved backup service, KMS or encryption tool; keep recovery keys separately and test key recovery. These scripts do **not** encrypt files.
- Copy encrypted backups to off-site storage in a separate failure domain/account, with private access, retention and deletion protection. Verify transfer and recovery; the local retention setting does not manage remote copies.
- Back up the media volume/object store independently and coordinate database/media recovery points. Document retention, data deletion and legal requirements.
- For managed PostgreSQL, enable provider snapshots/PITR and run provider-supported exports/restores; the Compose scripts will not back up an external database.
