#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
# WHAT IS ALREADY ON THIS BOX, AND WHERE IS THERE ROOM
#
# Run this on the Hetzner server BEFORE installing anything. It reads and
# prints; it writes nothing, starts nothing, stops nothing and changes no
# configuration. Safe to run on a box carrying live applications, which is
# the whole point of it.
#
#     scp deploy/server_survey.sh pjp@188.245.228.253:/tmp/
#     ssh pjp@188.245.228.253 'bash /tmp/server_survey.sh' | tee survey.txt
#
# Some sections need root to be complete. Without sudo it still runs and
# says which answers are partial rather than printing a confident blank.
# ══════════════════════════════════════════════════════════════════════════
set -uo pipefail

S() { printf '\n\033[1m── %s %s\033[0m\n' "$1" "$(printf '─%.0s' $(seq 1 $((66 - ${#1}))))"; }
note() { printf '   %s\n' "$*"; }

if [ "$(id -u)" -eq 0 ]; then SUDO=""; CAN_ROOT=1
elif sudo -n true 2>/dev/null;  then SUDO="sudo -n"; CAN_ROOT=1
else SUDO=""; CAN_ROOT=0; fi

printf '\033[1mSERVER SURVEY  %s  %s\033[0m\n' "$(hostname)" "$(date -Is)"
[ "$CAN_ROOT" -eq 0 ] && note "(no root — port owners, firewall and nginx files will be partial)"

S "The machine"
. /etc/os-release 2>/dev/null && note "$PRETTY_NAME"
note "kernel   $(uname -r)"
note "uptime  $(uptime -p 2>/dev/null || uptime)"
note "cpu      $(nproc) core(s)"
echo
free -h 2>/dev/null | sed 's/^/   /'
echo
df -h / /srv /var 2>/dev/null | sort -u | sed 's/^/   /'

S "Load right now"
note "$(cat /proc/loadavg)"
note "top 5 by memory:"
ps -eo pmem,rss,comm --sort=-rss 2>/dev/null | head -6 | sed 's/^/     /'

S "What is listening  — pick a free port from the gaps"
if [ "$CAN_ROOT" -eq 1 ]; then
  $SUDO ss -tlnp 2>/dev/null | sed 's/^/   /'
else
  ss -tln 2>/dev/null | sed 's/^/   /'
  note "(owners hidden without root)"
fi
echo
note "ports 8000-8100 already taken:"
ss -tln 2>/dev/null | awk '{print $4}' | grep -oE '[0-9]+$' | sort -un \
  | awk '$1>=8000 && $1<=8100' | tr '\n' ' ' | sed 's/^/     /'
echo

S "Running services  — check for a name that would collide with pjp-dse"
systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null \
  | awk '{print "   " $1}' | head -40
echo
note "anything already called pjp / polygon / dse:"
systemctl list-unit-files --no-pager --no-legend 2>/dev/null \
  | grep -Ei 'pjp|polygon|dse' | sed 's/^/     /' || note "     none — the unit name is free"

S "nginx"
if command -v nginx >/dev/null 2>&1; then
  note "$(nginx -v 2>&1)"
  note "config test:"; $SUDO nginx -t 2>&1 | sed 's/^/     /'
  echo
  note "enabled sites:"
  ls -1 /etc/nginx/sites-enabled/ 2>/dev/null | sed 's/^/     /'
  ls -1 /etc/nginx/conf.d/*.conf 2>/dev/null | sed 's/^/     /'
  echo
  note "server_name values in use  — a new block must not reuse one:"
  $SUDO grep -rhE '^\s*server_name' /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null \
    | sed 's/^/     /' | sort -u
  echo
  note "WHO OWNS THE BARE IP  — the default_server answers requests with no hostname:"
  $SUDO grep -rlE 'default_server' /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null \
    | sed 's/^/     /' || note "     no explicit default_server — the first block loaded wins"
  echo
  note "listen directives:"
  $SUDO grep -rhE '^\s*listen' /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null \
    | sed 's/^/     /' | sort -u
else
  note "nginx is NOT installed — installing it would be a change to a shared box."
fi

S "Certificates  — certbot --nginx rewrites OTHER apps' server blocks"
if command -v certbot >/dev/null 2>&1; then
  note "$(certbot --version 2>&1)"
  $SUDO certbot certificates 2>/dev/null | grep -E 'Certificate Name|Domains|Expiry' | sed 's/^/     /'
  note "renewal hooks that touch nginx:"
  $SUDO grep -rl nginx /etc/letsencrypt/renewal/ 2>/dev/null | sed 's/^/     /' || note "     none"
else
  note "certbot not installed"
fi

S "Other applications on disk"
for d in /srv /opt /var/www /home; do
  [ -d "$d" ] || continue
  note "$d"
  ls -1 "$d" 2>/dev/null | head -12 | sed 's/^/     /'
done

S "Databases  — none of ours; confirm we touch nothing"
ss -tln 2>/dev/null | grep -E ':(5432|3306|6379|27017)\b' | sed 's/^/   /' \
  || note "no postgres / mysql / redis / mongo listening"
for svc in postgresql mysql mariadb redis-server; do
  systemctl is-active "$svc" >/dev/null 2>&1 && note "$svc is ACTIVE — leave it alone"
done

S "Users"
awk -F: '$3>=1000 && $3<65534 {print "   " $1 "  uid=" $3 "  " $6}' /etc/passwd
note "is 'pjp' taken?"
id pjp >/dev/null 2>&1 && note "     YES — pick another name" || note "     no — free to create"

S "Firewall"
if command -v ufw >/dev/null 2>&1 && [ "$CAN_ROOT" -eq 1 ]; then
  $SUDO ufw status verbose 2>/dev/null | sed 's/^/   /'
elif command -v nft >/dev/null 2>&1 && [ "$CAN_ROOT" -eq 1 ]; then
  $SUDO nft list ruleset 2>/dev/null | head -30 | sed 's/^/   /'
else
  note "(needs root, or no ufw/nft — check the Hetzner Cloud Firewall in the console too)"
fi

S "Python"
for p in python3 python3.10 python3.11 python3.12; do
  command -v $p >/dev/null 2>&1 && note "$p  $($p -V 2>&1)"
done

S "Read this before installing"
cat <<'EOF'
   1. Take a port from the free range above and put it in the systemd unit.
      Do not assume 8000 — one of the existing apps probably has it.
   2. Add a NEW file under sites-available. Do not edit an existing one and
      do not claim default_server: on a bare IP the default_server is what
      answers, so taking it would silently redirect another app's traffic.
      Until a hostname exists, listen on a distinct port instead.
   3. Reload nginx, never restart it. A reload keeps the other apps'
      connections; a restart drops every one of them.
   4. If certbot already manages certificates here, use
      `certbot certonly --webroot` — `--nginx` edits every server block it
      finds, including the ones that are not ours.
   5. Set MemoryMax and CPUQuota on the unit. This application loads 7,761
      polygons into memory on first request; without a ceiling an overrun
      would be killed by the OOM reaper, and the reaper does not always pick
      the process that caused it.
EOF
echo
