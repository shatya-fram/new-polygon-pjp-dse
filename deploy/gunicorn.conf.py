"""Gunicorn settings for the shared instance.

WHY GUNICORN AND NOT app.run
    app.run is Flask's development server: one request at a time, no
    hardening, and until this deployment it started with debug=True, which
    is an interactive Python console for anyone who can provoke an
    exception. It is the right tool on a laptop and the wrong one on a host.

WHY SYNC WORKERS AND SO FEW
    Every request here is CPU work against SQLite -- simplifying polygons,
    building a territory -- not waiting on a network. Async workers buy
    nothing for that and SQLite does not want many writers. Two workers per
    core, and a long timeout because Force fit on a real file legitimately
    takes half a minute.
"""
import multiprocessing
import os

bind = os.getenv("BIND", "127.0.0.1:5002")
workers = int(os.getenv("WEB_WORKERS",
                        str(min(4, multiprocessing.cpu_count() * 2 + 1))))
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
