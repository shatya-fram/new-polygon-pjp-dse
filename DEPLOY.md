# Putting NEW POLYGON PJP DSE on a Hetzner box

The shape of this deployment, decided 27 Aug:

| | |
|---|---|
| **Who gets in** | one shared password, checked by nginx |
| **What they can do** | read everything; preview their own workbook in the browser; nothing else |
| **Where uploads happen** | on your Mac. The server never accepts a data layer |
| **Personal data** | phone numbers stripped from the copy that goes up; partner names kept |
| **Transport** | plain HTTP for now — **read the TLS warning below** |

---

## Read this before you start

**The password crosses the network in clear text.** Basic auth over HTTP
sends `user:password` base64-encoded — which is encoding, not encryption —
on *every single request*. Anyone between a visitor and the server (a café
network, a hotel wifi, an ISP, anyone on the same office LAN) can read it,
and then they have the password and the data.

For a short private trial that is a considered risk. Before this carries
anything that matters, spend ten minutes on §7. There is a free path that
works on an IP-only box.

**The server is a copy, not a second home.** Nothing on it can be edited.
All loading, importing and deriving stays on your Mac; when the data
changes you build a new release and push it. That is what makes the box
safe to expose at all — there is no write path to attack.

---

## 1. The server

A Hetzner **CX22** (2 vCPU, 4 GB, 40 GB) is comfortable. CPX11 works if the
budget is tight, but Force fit on a full file will be slow.

Ubuntu 24.04. When it is built, from your Mac:

```bash
ssh root@<server-ip>

adduser --disabled-password --gecos "" pjp
apt update && apt install -y python3-venv python3-pip nginx apache2-utils ufw

ufw allow OpenSSH
ufw allow "Nginx Full"
ufw --force enable
```

Hetzner also has a **cloud firewall** in its console, outside the machine.
Use it as well as `ufw` — allow only 22, 80, 443 inbound. Two locks, and
the outer one survives anything that goes wrong inside the box.

## 2. Build the release, on your Mac

```bash
cd "/Users/shatyaframudia/Documents/Python Project/Python Project/API Pull Apps /API Location Pulldown/new-polygon-pjp-dse"

# The project's own interpreter — a bare `python3` on macOS has none of
# this application's dependencies.
./.venv/bin/python make_public_db.py    # strips the phone numbers, vacuums
./deploy/build_release.sh               # -> dist/pjp-dse-<stamp>.tar.gz
```

`build_release.sh` refuses to produce an archive that still contains a
stripped field, a `.env`, or any `.kml`. It leaves out `Data Upload/`
(1.3 GB of source exports) and `Data Samples/` — the server reads the
database, not the files it was built from.

```bash
scp dist/pjp-dse-<stamp>.tar.gz pjp@<server-ip>:/tmp/
```

## 2a. Code through GitHub, data around it

**Two things travel, by two routes, on purpose.**

| | route | why |
|---|---|---|
| application | GitHub → `git pull` on the server | small, versioned, worth a history |
| database + map cache | `rsync` Mac → server | 227 MB, rebuilt whole every refresh |

Git is the wrong tool for the second. It keeps every version of every file
forever, so a 210 MB database refreshed weekly would add 210 MB to the
repository *each time* and never give it back. GitHub also refuses any file
over 100 MB outright. And putting Indosat outlet data on GitHub is a
decision about where commercial data lives that nothing here requires you
to make — so the split is not only cheaper, it is smaller in every sense
that matters.

### One-time: create the repository

**Make it private.** The code alone reveals the territory model, the field
names and the internal hierarchy.

```bash
# on the Mac, in the app folder
git init                       # if it is not a repo yet
git add -A
git commit -m "NEW POLYGON PJP DSE — territory application"

# create an EMPTY private repo on github.com first, then:
git remote add origin git@github.com:<you>/pjp-dse.git
git branch -M main
git push -u origin main
```

Check what you are about to publish before the first push:

```bash
git ls-files | wc -l                    # ~105 files
du -ch $(git ls-files) | tail -1        # ~1.4 MB
git ls-files | grep -Ei "\.env$|\.db$|\.kml|\.kmz|tar\.gz"   # must be empty
```

`.gitignore` already excludes `.env`, `.venv/`, `data/*.db`,
`data/mapcache/`, `data/public/`, `dist/`, `Data Upload/` (1.3 GB of source
exports) and `Data Samples/`. If any of those appear in `git ls-files`,
stop and fix it — a secret pushed once stays in the history even after you
delete the file.

### One-time: give the server read access

A **deploy key** is right here: read-only, and scoped to this one
repository rather than your whole GitHub account.

```bash
# on the server
sudo -u pjp ssh-keygen -t ed25519 -C "pjp-dse deploy" -f /home/pjp/.ssh/id_ed25519 -N ""
sudo cat /home/pjp/.ssh/id_ed25519.pub
```

Paste that into GitHub → the repo → Settings → Deploy keys → Add,
**leaving "Allow write access" unticked**. Then:

```bash
sudo -u pjp ssh -T git@github.com          # accept the fingerprint once
```

### One-time: clone in place

The server needs the code from git and the data from rsync, in one folder.

**The two `--exclude` lines below are the data boundary, not tidiness.**
`data/mapcache/` is gitignored, so it travels by rsync and bypasses every
check that reads the repository. It contains `outlet_dse.geojson.gz` — 4.0 MB
and the whole DSE-to-outlet mapping — and `site_locations.geojson.gz`. Neither
may reach a shared server: they are read from the user's own desktop in the
browser and are never uploaded. `--delete` is there so that a layer excluded
today is also removed from a server an earlier build already put it on.

```bash
# on the server
sudo -u pjp git clone git@github.com:<you>/pjp-dse.git /opt/pjp-dse
cd /opt/pjp-dse
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp deploy/env.public.example .env && nano .env     # §4
```

```bash
# from the Mac — the data, which git never carries
./.venv/bin/python make_public_db.py
rsync -avz --progress data/public/poi_pulldown.db pjp@<server>:/opt/pjp-dse/data/
rsync -avz --delete \
  --exclude '*.points.*' \
  --exclude 'outlet_dse*' --exclude 'site_locations*' \
  data/mapcache/ pjp@<server>:/opt/pjp-dse/data/mapcache/
```

Then §5 and §6 as written — systemd and nginx.

### From then on

**Code change** — push, then one command on the server:

```bash
# Mac
git add -A && git commit -m "…" && git push

# server
sudo -u pjp /opt/pjp-dse/deploy/update.sh
```

`update.sh` pulls, reinstalls only if `requirements.txt` moved, runs
`migrate.py`, restarts, checks `/healthz` — and **rolls back to the previous
commit if the service does not come up**. It never touches `.env` or
`data/`.

**Data change** — no git at all:

```bash
# Mac
./.venv/bin/python make_public_db.py
rsync -avz --progress data/public/poi_pulldown.db pjp@<server>:/opt/pjp-dse/data/
rsync -avz --delete \
  --exclude '*.points.*' \
  --exclude 'outlet_dse*' --exclude 'site_locations*' \
  data/mapcache/ pjp@<server>:/opt/pjp-dse/data/mapcache/
ssh pjp@<server> "sudo systemctl restart pjp-dse"
```

rsync sends only what differs, so a refresh where most polygons are
unchanged is far less than 210 MB on the wire.

### Where this leaves `build_release.sh`

The tarball still has a job: it is the way in with **no GitHub at all** —
one file, `scp`, unpack. Use it for the very first install if you would
rather not set up keys yet, or as an offline fallback. Once the server has
a clone, `update.sh` is the faster path and the one with a rollback.

## 3. Install it

```bash
ssh pjp@<server-ip>
sudo mkdir -p /opt/pjp-dse && sudo chown pjp:pjp /opt/pjp-dse
tar xzf /tmp/pjp-dse-*.tar.gz -C /tmp
mv /tmp/pjp-dse-*/* /opt/pjp-dse/
cd /opt/pjp-dse

python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` in the release is the short one — no duckdb. It is a
60 MB dependency used only by the Overture pull, which this instance never
runs.

## 4. Configure it

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into SECRET_KEY
nano .env
chmod 600 .env
```

Three lines decide whether this is safe:

```
PUBLIC_MODE=1          # refuses every write
SECRET_KEY=<the new one>   # NOT the one from your Mac
BIND=127.0.0.1:5002    # nginx is the only thing that may reach the app
```

The application **will not start** if `PUBLIC_MODE=1` and `SECRET_KEY` is
still the placeholder. Leave `GOOGLE_API_KEY` blank — a key on a reachable
box is a bill waiting to happen.

Check it by hand before wiring up systemd:

```bash
set -a; . ./.env; set +a
.venv/bin/gunicorn -c deploy/gunicorn.conf.py app:app
# another terminal:
curl -s localhost:5002/healthz
curl -s -o /dev/null -w "%{http_code}\n" -X POST localhost:5002/api/configuration/upload   # must be 403
```

## 5. Run it under systemd

```bash
sudo cp deploy/pjp-dse.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pjp-dse
sudo systemctl status pjp-dse
sudo journalctl -u pjp-dse -f
```

## 6. The password and nginx

```bash
sudo htpasswd -c /etc/nginx/.htpasswd-pjp jaya      # it will prompt twice
sudo chown root:www-data /etc/nginx/.htpasswd-pjp
sudo chmod 640 /etc/nginx/.htpasswd-pjp

sudo cp deploy/nginx.conf /etc/nginx/sites-available/pjp-dse
sudo ln -sf /etc/nginx/sites-available/pjp-dse /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Use a long password — this is the only thing between the internet and
Indosat commercial data. Add people with `htpasswd` (no `-c`, or you will
overwrite the file); remove them with `htpasswd -D`.

Now `http://<server-ip>/` asks for the password.

## 7. TLS — do this soon

**If you can point a name at the box** (even a subdomain of something you
already own):

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d pjp.yourdomain.co.id
```

certbot edits the nginx file, installs the certificate and renews it by
itself. Two minutes.

**If you have no domain at all**, a free dynamic name is enough for
Let's Encrypt to issue against:

1. Register at duckdns.org (or nip.io / sslip.io) and point it at the IP.
2. `sudo certbot --nginx -d yourname.duckdns.org`

Either way, afterwards check that port 80 redirects rather than serving:

```bash
curl -sI http://<server-ip>/ | head -1     # expect 301
```

## 8. Updating the data

The whole point of the read-only posture: data changes on your Mac, then:

```bash
# on the Mac
./.venv/bin/python make_public_db.py
./deploy/build_release.sh
scp dist/pjp-dse-<stamp>.tar.gz pjp@<server-ip>:/tmp/

# on the server
sudo systemctl stop pjp-dse
cd /opt/pjp-dse
cp data/poi_pulldown.db /srv/backup-$(date +%F).db     # keep the last good one
tar xzf /tmp/pjp-dse-<stamp>.tar.gz -C /tmp
rsync -a --delete --exclude .env --exclude .venv --exclude data/ /tmp/pjp-dse-*/ /opt/pjp-dse/
cp /tmp/pjp-dse-*/data/poi_pulldown.db data/poi_pulldown.db
cp /tmp/pjp-dse-*/data/mapcache/* data/mapcache/ 2>/dev/null || true
sudo systemctl start pjp-dse
```

The map cache travels with the release, so the first visitor after an
update gets a warm map instead of waiting for every layer to be simplified
from scratch.

## 9. Checking it is actually locked down

Run these from your laptop against the live box. Each one should behave
exactly as noted, and if any does not, stop and fix it before sharing
the URL.

```bash
S=http://<server-ip>

curl -sI $S/ | head -1                                   # 401 without a password
curl -sI -u jaya:<pw> $S/ | head -1                      # 200 with one

# every write refused even when logged in
for p in /api/configuration/upload /api/configuration/import \
         /api/upload-layer /api/local-layers/import /api/samples; do
  printf "%-34s %s\n" $p "$(curl -s -o /dev/null -w '%{http_code}' \
    -u jaya:<pw> -X POST -H 'Content-Type: application/json' -d '{}' $S$p)"
done                                                     # all 403

# gunicorn must not be reachable except through nginx
curl -s --max-time 5 http://<server-ip>:5002/ || echo "5002 closed — correct"

# no phone number survives anywhere in the served data
curl -s -u jaya:<pw> "$S/api/layer/outlet_dse/points.geojson" --compressed \
  | grep -ci "MSISDN\|OUTLET_MSI" || echo "no MSISDN in the layer — correct"
```

## 10. What is still worth doing

- **TLS** (§7). The password is in clear text until then.
- **Backups.** Hetzner snapshots are a few euros a month and are the whole
  box. The database is the only irreplaceable thing on it, and it also
  exists on your Mac.
- **Fail2ban** for repeated basic-auth failures:
  `sudo apt install fail2ban` and enable the `nginx-http-auth` jail.
- **Watch the disk.** `data/mapcache/` grows when layers are rebuilt.
- If the team grows past a handful of people, one shared password stops
  being appropriate — you cannot revoke one person or tell who looked.
  That is when named accounts (the option not taken) become worth the work.
