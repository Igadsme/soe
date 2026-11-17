"""Exercise host script boundaries without contacting Docker or a database."""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('script,args', [
    ('backup_db.sh', ['unexpected']), ('restore_db.sh', []),
    ('restore_db.sh', ['one', 'two']), ('deploy.sh', []), ('rollback.sh', []),
])
def test_script_arguments(script, args):
    result = subprocess.run(['bash', str(ROOT / 'scripts' / script), *args], capture_output=True)
    assert result.returncode == 2
    assert b'Usage:' in result.stderr


def test_local_backup_and_relative_restore(tmp_path):
    docker = tmp_path / 'docker'
    docker.write_text('#!/bin/sh\ncase "$*" in *pg_dump*) printf "archive-placeholder";; *pg_restore*) cat > "$SOE_TEST_RESTORED";; *) exit 99;; esac\n')
    docker.chmod(0o700)
    env = {**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH'],
           'SOE_BACKUP_DIR': str(tmp_path / 'archives'),
           'SOE_TEST_RESTORED': str(tmp_path / 'restored')}
    subprocess.run(['bash', str(ROOT / 'scripts/backup_db.sh')], env=env, check=True, capture_output=True)
    dumps = list((tmp_path / 'archives').glob('*.dump'))
    assert len(dumps) == 1
    assert dumps[0].stat().st_mode & 0o077 == 0
    result = subprocess.run(['bash', str(ROOT / 'scripts/restore_db.sh'), str(dumps[0].relative_to(tmp_path))],
                            cwd=tmp_path, env=env, input=b'RESTORE\n', capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'restored').read_text() == 'archive-placeholder'


def test_metrics_multiprocess(tmp_path):
    metrics_dir = tmp_path / 'metrics'
    metrics_dir.mkdir()
    env = {**os.environ, 'PROMETHEUS_MULTIPROC_DIR': str(metrics_dir)}
    produce = '''from app.observability import REQUESTS, IN_PROGRESS
REQUESTS.labels("GET", "/health", "200").inc()
IN_PROGRESS.inc()
'''
    pids = []
    for _ in range(2):
        proc = subprocess.Popen(['python', '-c', produce], env=env)
        pids.append(proc.pid)
        assert proc.wait(timeout=20) == 0
    collect = '''from app.observability import IN_PROGRESS
IN_PROGRESS.set(0)
from prometheus_client import CollectorRegistry, generate_latest, multiprocess
import sys
for pid in sys.argv[1:]:
    multiprocess.mark_process_dead(int(pid))
registry = CollectorRegistry()
multiprocess.MultiProcessCollector(registry)
print(generate_latest(registry).decode())
'''
    result = subprocess.run(['python', '-c', collect, *map(str, pids)], env=env, capture_output=True, text=True, check=True)
    assert 'soe_http_requests_total{method="GET",route="/health",status="200"} 2.0' in result.stdout
    assert 'soe_http_requests_in_progress 0.0' in result.stdout


def test_offsite_backup_requires_encryption_before_dump(tmp_path):
    marker = tmp_path / 'called'
    for command in ('pg_dump', 'pg_restore', 'aws', 'rclone'):
        stub = tmp_path / command
        stub.write_text('#!/bin/sh\ntouch "$SOE_TEST_CALLED"\nexit 99\n')
        stub.chmod(0o700)
    env = {**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH'],
           'SOE_BACKUP_S3_BUCKET': 'test-bucket', 'SOE_BACKUP_ENCRYPTION_PASSPHRASE': '',
           'SOE_TEST_CALLED': str(marker)}
    result = subprocess.run(['bash', str(ROOT / 'infra/backup/backup.sh')], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert 'SOE_BACKUP_ENCRYPTION_PASSPHRASE is required' in result.stderr
    assert not marker.exists()
