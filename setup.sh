#!/usr/bin/env bash
# One-time setup. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
PYVER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "› using $($PY --version)"

# duckdb is a compiled extension. If pip can't find a wheel for this
# interpreter it silently starts a source build that takes ~an hour and
# usually fails. --only-binary makes that fail in seconds instead.
"$PY" - <<'PYCHK'
import sys
maj, min = sys.version_info[:2]
if (maj, min) < (3, 10):
    sys.exit("FATAL: Python 3.10+ required (duckdb needs it). "
             "Re-run as:  PYTHON=python3.12 ./setup.sh")
PYCHK

if [ ! -d .venv ]; then
  echo "› creating .venv"
  "$PY" -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip

echo "› installing dependencies (wheels only — no source builds)"
if ! ./.venv/bin/pip install --quiet --only-binary=duckdb -r requirements.txt; then
  cat <<MSG

──────────────────────────────────────────────────────────────────────
  Dependency install failed on Python ${PYVER}.

  Almost always this means duckdb has no prebuilt wheel for ${PYVER} yet.
  duckdb currently ships wheels for CPython 3.10 – 3.14.

  Fix: build the venv on a version that has one, e.g.

      rm -rf .venv
      PYTHON=python3.12 ./setup.sh

  Install another Python with:  brew install python@3.12
──────────────────────────────────────────────────────────────────────
MSG
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "› created .env  (add GOOGLE_API_KEY there when you want connection 2)"
fi

echo "› installing DuckDB extensions (needs internet, safe to re-run)"
./.venv/bin/python - <<'PYEXT'
import duckdb
print(f"    duckdb {duckdb.__version__}")
c = duckdb.connect()
for ext in ("httpfs", "spatial"):
    try:
        c.execute(f"INSTALL {ext}"); c.execute(f"LOAD {ext}")
        print(f"    {ext}: ok")
    except Exception as e:
        print(f"    {ext}: FAILED — {str(e).splitlines()[0]}")
PYEXT

echo "› initialising database"
./.venv/bin/python migrate.py

cat <<'MSG'

Setup complete. Next:

  ./run.sh                          start the web UI  (http://127.0.0.1:5000)
  ./.venv/bin/python pull_overture.py --list
  ./.venv/bin/python pull_overture.py --all
  ./.venv/bin/python coverage.py
  ./.venv/bin/python export_map.py --all-formats

MSG
