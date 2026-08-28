#!/usr/bin/env bash
# Kept so `./run.sh` still works. start.sh is the real launcher.
cd "$(dirname "$0")"
exec ./start.sh "$@"
