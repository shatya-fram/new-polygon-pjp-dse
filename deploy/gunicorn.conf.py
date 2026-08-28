"""Gunicorn settings for the shared instance.

WHY GUNICORN AND NOT app.run
    app.run is Flask's development server: one request at a time, no
    hardening, and until this deployment it started with debug=True, which
    is an interactive Python console for anyone who can provoke an
    exception. It is the right tool on a laptop and the wrong one on a host.

WHY SYNC WORKERS AND SO FEW
    Every request here is CPU work against SQLite -- simplifying polygons,
    building a territory -- not waiting on a network. Async workers buy
    nothing for that and SQLite does not want many writers.

WHY TWO, FIXED, AND NOT "two per core"
    The survey of the target box settled this. It has 2 cores and 3.7 GB of
    RAM, of which 1.5 GB was available and 509 MB of swap was already in
    use, carrying eight other applications -- Postpaid, Frontliner,
    Merchandiser, Vanguard, Opshub, Consignment, a dashboard and an
    analytics API -- plus Postgres and Docker.

    The old default was min(4, cores * 2 + 1), which on that box is FOUR.
    Each worker holds its own parsed copy of the desa layer in
    territory_api._poly_cache for the life of the process; four of them
    would have been most of the free memory, and the kernel's OOM killer
    does not reliably kill the process that caused the pressure. It might
    have taken Postpaid instead.

    So: two, stated as a number rather than derived from the hardware, with
    max_requests recycling to keep the footprint flat. The unit file caps
    the cgroup as well, so an overrun kills this service and nothing else.
"""
import os

# 8096: free on the target box. 8001, 8080, 8081, 8082, 8090, 8091, 8095
# and 8099 are taken by the applications already there, and 8180/8181 by
# nginx. Loopback only -- nginx is the only thing that may reach this.
bind = os.getenv("BIND", "127.0.0.1:8096")
workers = int(os.getenv("WEB_WORKERS", "2"))
worker_class = "sync"
# Force fit loads 7,761 desa polygons and rebalances against them. Thirty
# seconds is not enough; a worker killed mid-build looks like a broken page.
timeout = int(os.getenv("WEB_TIMEOUT", "180"))
graceful_timeout = 30
keepalive = 5
# A long-lived Python process that has parsed an 800 MB KML holds that memory
# for the rest of its life. Recycling keeps the footprint flat on a small box.
max_requests = 400
max_requests_jitter = 50
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info")
# The visitor's address, forwarded by nginx. For logs only -- nothing in this
# application decides permissions from an address.
forwarded_allow_ips = "127.0.0.1"
access_log_format = '%({x-forwarded-for}i)s %(m)s %(U)s %(s)s %(M)sms'
