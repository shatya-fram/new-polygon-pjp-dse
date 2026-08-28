#!/usr/bin/env bash
# Stop the web UI started by ./start.sh.
#   ./stop.sh              stop the server on port 5002
#   ./stop.sh --port=5003  stop the server on another port
set -euo pipefail
cd "$(dirname "$0")"
# Prefer the port start.sh actually used -- it may have had to move off 5000
# because something else held it, and stopping "the server" should not depend
# on the operator remembering that.
if [ -n "${PORT:-}" ]; then
  PORT="$PORT"
elif [ -r .server-port ]; then
  PORT="$(tr -dc '0-9' < .server-port)"
fi
PORT="${PORT:-5002}"
for arg in "$@"; do
  case "$arg" in --port=*) PORT="${arg#*=}" ;; esac
done

holders() { lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null || true; }

# Ours means started from THIS folder. There are two copies of this
# application on the machine now and both run a file called app.py, so
# matching on the name alone would let this script stop the other one.
here="$PWD"
cwd_of() { lsof -a -d cwd -p "$1" -Fn 2>/dev/null | sed -n 's/^n//p' | head -1; }
is_ours() { [ -n "$1" ] && [ "$(cwd_of "$1")" = "$here" ]; }

if [ -z "$(holders)" ]; then
  echo "  nothing is listening on port $PORT"
  exit 0
fi

# Several pids is normal -- the reloader runs a parent and a child. Stop the
# ones that are this app and say so about anything else, rather than judging
# the whole port by whichever pid lsof happened to list first.
mine=""; theirs=0
for pid in $(holders); do
  args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  if is_ours "$pid"; then
    mine="$mine $pid"
  else
    echo "  port $PORT is held by something started from another folder, left alone:"
    echo "    pid $pid  ${args:-<no command line>}"
    theirs=1
  fi
done

if [ -z "${mine# }" ]; then
  exit "$theirs"
fi
for pid in $mine; do kill "$pid" 2>/dev/null || true; done
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -z "$(holders)" ] && { echo "  stopped (pid${mine})"; exit 0; }
  sleep 0.3
done
for pid in $mine; do kill -9 "$pid" 2>/dev/null || true; done
echo "  stopped (pid${mine}, forced)"
