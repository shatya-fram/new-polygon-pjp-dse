#!/usr/bin/env bash
# Double-clickable macOS launcher.
cd "$(dirname "$0")"
./start.sh "$@"
echo
echo "Server stopped. Press any key to close."
read -n 1
