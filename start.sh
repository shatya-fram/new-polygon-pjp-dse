#!/usr/bin/env bash
#
# Start the NEW POLYGON PJP DSE web UI.
#
#   ./start.sh                 start on http://127.0.0.1:5002
#   ./start.sh --port=5003     use another port
#   ./start.sh --no-open       do not open the browser
#   ./start.sh --no-migrate    skip the schema check (rarely wanted)
#   ./start.sh --strict-port   fail instead of stepping to the next free port
#   ./stop.sh                  stop whatever this script started
#
# It restarts rather than refuses: if a previous copy of this app is still
# holding the port, that copy is stopped first. If somebody ELSE holds it --
# on a Mac that is usually AirPlay Receiver, which owns 5000 out of the box --
# it steps onto the next free port and tells you, rather than either failing
# or killing a stranger's process because it wanted the same number.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-5002}"
OPEN=1
MIGRATE=1
STRICT_PORT=0

for arg in "$@"; do
  case "$arg" in
    --port=*)    PORT="${arg#*=}" ;;
    --no-open)   OPEN=0 ;;
    --no-migrate) MIGRATE=0 ;;
    --strict-port) STRICT_PORT=1 ;;
    -h|--help)   awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "unknown option: $arg  (try --help)" >&2; exit 2 ;;
  esac
done

say() { printf '  %s\n' "$*"; }
die() { printf '\n  %s\n\n' "$*" >&2; exit 1; }

# ── the interpreter ──────────────────────────────────────────────────────
if [ ! -x .venv/bin/python ]; then
  say "no .venv yet — running setup.sh (one time, a few minutes)"
  ./setup.sh || die "Setup failed. Fix the errors above, then run ./start.sh again."
fi
PY=./.venv/bin/python
"$PY" -c 'import flask, openpyxl' 2>/dev/null || \
  die "The .venv is broken or incomplete. Rebuild it:  rm -rf .venv && ./setup.sh"

# ── the port ─────────────────────────────────────────────────────────────
# Three different situations wear the same "address already in use" error,
# and only one of them is a problem worth stopping for:
#
#   our own copy      -> stop it and take the port back. This is what you
#                        want after editing something.
#   somebody else     -> step aside onto the next free port and say so.
#                        macOS ships AirPlay Receiver listening on 5000, so
#                        on a Mac this is the common case, not the rare one.
#   nothing at all    -> go.
#
# lsof names several pids for one server -- the reloader keeps a parent and
# a child -- so every check treats it as a list. `ps -p "1234\n5678"` prints
# nothing, and an empty command line makes our own copy look like a stranger.
holders() { lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null || true; }

# WHICH COPY IS "OURS" IS DECIDED BY FOLDER, NOT BY THE WORD app.py
#
# This used to ask whether the process command line contained "app.py" and
# call anything that matched "our own copy" -- fair enough when there was one
# of these applications on the machine. There are now two, they are copies of
# each other, and both run a file called app.py. Under the old test either one
# would happily stop the other the moment it was pointed at its port, which is
# how a running server disappears without anybody asking it to.
#
# A process is ours only if its working directory IS this folder. Where that
# cannot be read -- lsof declines for processes owned by another user -- the
# answer is no. Stepping aside from something we cannot prove is ours is the
# cheap mistake; killing it is the expensive one.
here="$PWD"
cwd_of() {
  lsof -a -d cwd -p "$1" -Fn 2>/dev/null | sed -n 's/^n//p' | head -1
}
is_ours() { [ -n "$1" ] && [ "$(cwd_of "$1")" = "$here" ]; }

# "" when the port is free or only we are on it, otherwise a description of
# whoever else is there.
occupant() {
  local pid args name
  for pid in $(holders "$1"); do
    if is_ours "$pid"; then continue; fi
    args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    case "$args" in
      *app.py*)
        # Another copy of this application, in a different folder. Not ours
        # to stop -- name it so the operator knows which one is in the way.
        printf 'another copy of this app (pid %s)' "$pid"
        return 0 ;;
    esac
    name="$(ps -p "$pid" -o comm= 2>/dev/null || echo '?')"
    case "$name" in
      *ControlCe*|*AirPlay*|*rapportd*)
        printf 'macOS AirPlay Receiver (%s)' "$name" ;;
      *) printf "'%s' (pid %s)" "$name" "$pid" ;;
    esac
    return 0
  done
  return 0
}

WANTED="$PORT"
BUSY="$(occupant "$PORT")"
if [ -n "$BUSY" ]; then
  if [ "$STRICT_PORT" = 1 ]; then
    die "Port $PORT is held by $BUSY.
  Free it, or choose another port:  ./start.sh --port=5001"
  fi
  found=""
  for try in $(seq $((PORT + 1)) $((PORT + 20))); do
    if [ -z "$(occupant "$try")" ] && [ -z "$(holders "$try")" ]; then
      found="$try"; break
    fi
  done
  [ -n "$found" ] || die "Port $PORT is held by $BUSY, and nothing between \
$((PORT + 1)) and $((PORT + 20)) is free either.
  Choose one yourself:  ./start.sh --port=8080"
  say "port $PORT is held by $BUSY — using $found instead"
  case "$BUSY" in
    *AirPlay*)
      say "  (that is a macOS feature, not a stuck process. To get $PORT back:"
      say "   System Settings → General → AirDrop & Handoff → AirPlay Receiver off)" ;;
  esac
  PORT="$found"
fi

# Our own copy, if any, on the port we are actually going to use. Same test:
# same folder, or it is not ours to stop.
mine=""
for pid in $(holders "$PORT"); do
  if is_ours "$pid"; then mine="$mine $pid"; fi
done
if [ -n "${mine# }" ]; then
  say "stopping the copy already running on port $PORT (pid${mine})"
  for pid in $mine; do kill "$pid" 2>/dev/null || true; done
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -z "$(holders "$PORT")" ] && break
    sleep 0.3
  done
  for pid in $(holders "$PORT"); do
    if is_ours "$pid"; then kill -9 "$pid" 2>/dev/null || true; fi
  done
  sleep 0.3
fi

# ── schema ───────────────────────────────────────────────────────────────
# Cheap and idempotent. Running it here means a pull of new code can never
# leave the UI querying a column the database does not have yet.
if [ "$MIGRATE" = 1 ]; then
  say "checking the database schema"
  "$PY" migrate.py | sed 's/^/    /'
fi

# ── go ───────────────────────────────────────────────────────────────────
if [ "$OPEN" = 1 ] && command -v open >/dev/null 2>&1; then
  (
    for _ in $(seq 1 40); do
      if curl -sf -o /dev/null "http://127.0.0.1:$PORT/healthz"; then
        open "http://127.0.0.1:$PORT/"
        exit 0
      fi
      sleep 0.5
    done
  ) &
fi

# So ./stop.sh finds it without being told, when the port had to move.
printf '%s\n' "$PORT" > .server-port 2>/dev/null || true
trap 'rm -f .server-port 2>/dev/null || true' EXIT

echo
if [ "$PORT" != "$WANTED" ]; then
  say "NOTE: not the usual port — $WANTED was taken"
fi
say "NEW POLYGON PJP DSE  ->  http://127.0.0.1:$PORT/"
say "menus: /configuration  ·  /distribution  ·  /preview-polygon  ·  /layer-model"
say "stop with Ctrl+C, or ./stop.sh from another terminal"
echo
PORT="$PORT" BANNER_ALREADY_PRINTED=1 exec "$PY" app.py
