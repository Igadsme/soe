#!/usr/bin/env bash
# Host-agnostic release from a remote tag or SHA; never run from a dirty checkout.
set -euo pipefail
[[ $# == 1 && "$1" != -* ]] || { echo 'Usage: scripts/deploy.sh TAG_OR_SHA' >&2; exit 2; }
cd "$(dirname "$0")/.."
[[ -z $(git status --porcelain --untracked-files=normal) ]] || { echo 'Deploy requires a clean checkout.' >&2; exit 1; }
previous=$(git rev-parse HEAD)
git fetch origin "$1"
git checkout --detach FETCH_HEAD
export SOE_IMAGE_TAG
SOE_IMAGE_TAG=$(git rev-parse HEAD)
compose=(docker compose -f docker-compose.prod.yml)
echo "Rollback: scripts/rollback.sh $previous"
"${compose[@]}" build
"${compose[@]}" run --rm migrate
"${compose[@]}" up -d --no-deps api worker frontend caddy
# Read the effective domain without sourcing .env as shell code.
domain=$("${compose[@]}" config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["caddy"]["environment"]["SOE_DOMAIN"])')
curl_args=(--fail --silent --show-error --max-time 10)
if [[ -n ${SOE_DEPLOY_CA_CERT:-} ]]; then curl_args+=(--cacert "$SOE_DEPLOY_CA_CERT"); fi
for ((attempt=0; attempt<60; attempt++)); do
  if curl "${curl_args[@]}" "https://$domain/health" >/dev/null 2>&1 && curl "${curl_args[@]}" "https://$domain/ready" >/dev/null 2>&1; then
    "${compose[@]}" ps
    echo "Deployment ready: $SOE_IMAGE_TAG"
    exit 0
  fi
  sleep 5
done
"${compose[@]}" ps
echo 'Deployment did not become ready. Inspect logs and use the rollback hint above.' >&2
exit 1
