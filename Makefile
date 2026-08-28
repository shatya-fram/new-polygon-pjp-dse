PY := ./.venv/bin/python

.PHONY: setup migrate start stop serve pull-overture pull-osm pull-osm-dry explain reconcile import-local pull-google-dry enrich coverage export map test clean

setup:          ; ./setup.sh
migrate:        ; $(PY) migrate.py
start:          ; ./start.sh
stop:           ; ./stop.sh
serve:          ; ./start.sh --no-open
pull-overture:  ; $(PY) pull_overture.py --all
explain:        ; $(PY) pull_overture.py --all --explain
pull-osm:       ; $(PY) pull_osm.py --all
pull-osm-dry:   ; $(PY) pull_osm.py --all --dry-run
reconcile:      ; $(PY) reconcile.py
import-local:   ; $(PY) import_local.py --all
pull-google-dry:; $(PY) pull_google.py --category bus_stop --tile-aoi --dry-run
enrich:         ; $(PY) enrich_admin.py --boundaries $(BOUNDARIES)
coverage:       ; $(PY) coverage.py --by-category
export:         ; $(PY) export.py --all --format xlsx
map:            ; $(PY) export_map.py --all-formats
test:           ; $(PY) tests/test_pulls.py
clean:          ; rm -rf __pycache__ providers/__pycache__ tests/__pycache__
