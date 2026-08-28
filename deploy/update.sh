#!/usr/bin/env bash
# Update the running instance from GitHub. Runs ON THE SERVER.
#
#   sudo -u pjp /srv/pjp-dse/deploy/update.sh
#
# WHAT THIS DOES AND DOES NOT TOUCH
#   Code, templates, static files    <- pulled from GitHub
#   .env                             <- never touched, it is not in the repo
#   data/                            <- never touched, it does not come from git
#
# The database and the map cache travel separately, by rsync from the Mac
# that built them. That separation is the point: a code deploy can never
# damage the data, and a data refresh can never ship half-finished code.
set -euo pipefail
cd "$(dirname "$0")/.."

BRANCH="${BRANCH:-main}"
echo "  fetching $BRANCH …"
git fetch --quiet origin "$BRANCH"

BEFORE=$(git rev-parse --short HEAD)
git reset --hard --quiet "origin/$BRANCH"
AFTER=$(git rev-parse --short HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
  echo "  already at $AFTER — nothing to do"
else
  echo "  $BEFORE -> $AFTER"
  git log --oneline "$BEFORE..$AFTER" | sed 's/^/    /'
fi

# Only reinstall when the dependency list actually moved. pip is slow and a
# restart that waits on it for no reason is a restart people stop doing.
if ! git diff --quiet "$BEFORE" "$AFTER" -- requirements.txt 2>/dev/null; then
  echo "  requirements changed — installing …"
  .venv/bin/pip install -q -r requirements.txt
fi

# Schema migrations are idempotent and safe to run on every deploy; a new
# column arriving with new code is the normal case.
.venv/bin/python migrate.py | sed 's/^/  /'

echo "  restarting …"
sudo systemctl restart pjp-dse
sleep 2
systemctl is-active --quiet pjp-dse && echo "  up" || {
  echo "  FAILED — rolling back to $BEFORE" >&2
  git reset --hard --quiet "$BEFORE"
  sudo systemctl restart pjp-dse
  exit 1
}
curl -fsS localhost:5002/healthz >/dev/null && echo "  healthy"
