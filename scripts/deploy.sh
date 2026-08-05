#!/usr/bin/env bash
# Deploy this repo's current commit to a running hub in place.
#
# Encodes the manual runbook used to deploy switchboard.lucille-ai.com by
# hand on 2026-08-05: pre-flight dirty check, tag the build by commit so a
# rollback has something to roll back TO, gate the new image before it ever
# touches the running container, cut over, verify by request/response (not
# by trusting a log line).
#
# Usage: scripts/deploy.sh
# Run from the repo root, on the host that runs the hub. Expects `docker
# compose` and a `.env` already holding whichever auth-mode variable this
# deployment uses (see docker-compose.yml's comment block).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> Pre-flight: checking for local drift"
# --untracked-files=no on purpose: this checks for local MODIFICATIONS to
# TRACKED files, which is the only thing that can conflict with the
# fast-forward below. Untracked files (a deploy host's own Caddyfile,
# docker-compose.tls.yml, .env.bak.* snapshots, etc.) can never conflict
# with `git merge --ff-only` on tracked paths, so they don't belong in this
# check at all — no need to enumerate every filename a real deploy host
# happens to carry.
dirty="$(git status --porcelain --untracked-files=no)"
if [ -n "$dirty" ]; then
  echo "ERROR: tracked files have local changes not from git:" >&2
  echo "$dirty" >&2
  echo "Reconcile by hand before deploying — see docs/deployment.md." >&2
  exit 1
fi

old_commit="$(git rev-parse --short HEAD)"
echo "==> Current commit: ${old_commit}"

echo "==> Fetching and fast-forwarding to origin/main"
git fetch origin
git checkout main
git merge --ff-only origin/main

new_commit="$(git rev-parse --short HEAD)"
echo "==> New commit: ${new_commit}"

if [ "$old_commit" = "$new_commit" ]; then
  echo "==> Already up to date, nothing to deploy."
  exit 0
fi

echo "==> Building image, tagged by commit (does not touch the running container)"
docker compose build switchboard
docker tag agent-switchboard:latest "agent-switchboard:${new_commit}"

echo "==> Gate: sanity-check the new image standalone before cutover"
docker run --rm --entrypoint switchboard "agent-switchboard:${new_commit}" --version

echo "==> Cutover"
docker compose up -d switchboard
sleep 3

echo "==> Verifying"
docker ps --filter name=switchboard --format '{{.Names}}\t{{.Status}}'
docker logs switchboard --tail 20

if ! curl -sf http://127.0.0.1:8787/health; then
  echo
  echo "ERROR: health check failed after cutover." >&2
  echo "Rollback: git checkout ${old_commit} && docker compose build switchboard && docker compose up -d switchboard" >&2
  echo "(agent-switchboard:${old_commit} is also still on this host if it was tagged by a prior deploy.)" >&2
  exit 1
fi
echo
echo "==> Deployed ${old_commit} -> ${new_commit}, healthy."
