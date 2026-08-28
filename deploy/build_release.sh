#!/usr/bin/env bash
# Assemble exactly what goes to the server, and nothing else.
#
#   ./deploy/build_release.sh          -> dist/pjp-dse-<date>.tar.gz
#
# WHAT IS DELIBERATELY LEFT OUT
#   Data Upload/     1.3 GB of source KML. The server reads the database,
#                    not the exports it was built from.
#   Data Samples/    82 MB of somebody's working files.
#   .env             the local one: it holds a Google key and a secret that
#                    must not exist on a public host.
#   data/*.db        the working database; the scrubbed copy goes instead.
#   .venv, __pycache__, _to_delete
set -euo pipefail
cd "$(dirname "$0")/.."

# macOS writes an AppleDouble sidecar (._name) beside every file it copies to
# a non-native filesystem, and tar then packs both. Half the last release was
# ._ junk: harmless on the server, but it doubles the archive's file count
# and hides what is actually in it.
export COPYFILE_DISABLE=1

# The project's own interpreter, the way start.sh does it. A bare `python3`
# is whatever macOS ships, which has none of this application's dependencies
# -- so the first thing this script did was fail on `import dotenv`.
if [ -x .venv/bin/python ]; then
  PY=./.venv/bin/python
else
  PY=python3
  echo "  note: no .venv — falling back to system python3" >&2
fi
STAMP=$(date +%Y%m%d-%H%M)
OUT="dist/pjp-dse-$STAMP"
PUB="data/public/poi_pulldown.db"

if [ ! -f "$PUB" ]; then
  echo "No scrubbed database. Run this first:" >&2
  echo "    ./.venv/bin/python make_public_db.py" >&2
  exit 1
fi
# Refuse to ship a database that still has phone numbers in it.
"$PY" make_public_db.py --check "$PUB"

rm -rf "$OUT" 2>/dev/null || true
mkdir -p "$OUT/data"
echo "  code…"
for f in *.py; do cp "$f" "$OUT/"; done
cp -r static templates deploy "$OUT/"
cp deploy/requirements-public.txt "$OUT/requirements.txt"
[ -d "Data Templates" ] && cp -r "Data Templates" "$OUT/"

echo "  database…"
cp "$PUB" "$OUT/data/poi_pulldown.db"
if [ -d data/mapcache ]; then
  # The prebuilt map cache. Shipping it means the first visitor gets a warm
  # map instead of waiting for every layer to be simplified from scratch.
  #
  # Chosen file by file rather than copied and then pruned: the *.points.*
  # caches are per-upload and mean nothing on a fresh host, and copying
  # something in order to delete it is one failed `rm` away from shipping it.
  mkdir -p "$OUT/data/mapcache"
  for f in data/mapcache/*.geojson.gz data/mapcache/*.stamp; do
    [ -e "$f" ] || continue
    case "$(basename "$f")" in
      # The per-upload point caches mean nothing on a fresh host.
      *.points.geojson.gz|*.points.geojson.gz.stamp) continue ;;
      # AND THE TWO LAYERS THAT MAY NOT TRAVEL. Excluding *.points.* alone
      # was not enough: outlet_dse.geojson.gz is 4.0 MB and is the whole
      # DSE-to-outlet mapping, scrubbed of phone numbers and otherwise
      # complete. Matched on the layer name so a new cache file for the same
      # layer is excluded the day it appears, rather than the day somebody
      # notices.
      outlet_dse*|site_locations*) continue ;;
    esac
    cp "$f" "$OUT/data/mapcache/"
  done
fi
cp deploy/env.public.example "$OUT/.env.example"
# Whoever installs this needs the instructions on the machine they are
# installing it on, not in a folder on somebody else's laptop.
[ -f DEPLOY.md ] && cp DEPLOY.md "$OUT/"

echo "  checking nothing private slipped in…"
if find "$OUT" -name ".env" -o -name "*.kml" -o -name "*.kmz" | grep -q .; then
  echo "  REFUSING: source exports or a .env are in the release" >&2
  find "$OUT" -name ".env" -o -name "*.kml" -o -name "*.kmz" >&2
  exit 1
fi
# The outlet mapping and the mast locations reach the server two ways -- the
# database and the map cache -- so both are checked here by name. A refusal
# is cheap; noticing on a public host is not.
if find "$OUT" \( -name "outlet_dse*" -o -name "site_locations*" \) | grep -q .; then
  echo "  REFUSING: an outlet or site cache is in the release" >&2
  find "$OUT" \( -name "outlet_dse*" -o -name "site_locations*" \) >&2
  exit 1
fi

mkdir -p dist
tar czf "$OUT.tar.gz" --exclude "._*" --exclude ".DS_Store" \
    -C dist "$(basename "$OUT")"
rm -rf "$OUT"
echo
echo "  $OUT.tar.gz  ($(du -h "$OUT.tar.gz" | cut -f1))"
echo
echo "  Send it up:"
echo "    scp $OUT.tar.gz pjp@<server>:/tmp/"
