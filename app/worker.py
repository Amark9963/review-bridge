"""Sync worker: `python -m app.worker`. Several can run at once; each claims different jobs.

`python -m app.worker --check` exits 0 only if this container's worker heartbeat is recent,
for use as a container health check.
"""
import argparse
import logging
import os
import signal
import socket
import sys
import threading
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jobs, logs, ratelimit
from app.config import settings
from app.db import engine
from app.models import WorkerHeartbeat
from app.yelp import yelp_client

log = logging.getLogger("app.worker")

POLL_SECONDS = 2
HEARTBEAT_EVERY = timedelta(seconds=5)
HEALTHY_WITHIN = timedelta(seconds=30)
SCHEDULE_EVERY = timedelta(seconds=60)


def check() -> int:
    with Session(engine()) as session:
        latest = session.scalar(select(WorkerHeartbeat.last_seen_at)
            .where(WorkerHeartbeat.worker_id.startswith(f"{socket.gethostname()}:"))
            .order_by(WorkerHeartbeat.last_seen_at.desc()).limit(1))
    return 0 if latest and jobs.utcnow() - latest < HEALTHY_WITHIN else 1


def run_forever():
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    started_at = jobs.utcnow()
    stop = threading.Event()
    # Finish the current job, then exit, when the container is stopped.
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    yelp = yelp_client()
    interval = timedelta(hours=settings().sync_interval_hours)
    last_beat = last_schedule = None
    fields = {"worker_id": worker_id}
    log.info("worker started", extra={"fields": {
        **fields, "sync_interval_hours": settings().sync_interval_hours}})
    with Session(engine()) as session:
        while not stop.is_set():
            try:
                now = jobs.utcnow()
                if last_beat is None or now - last_beat >= HEARTBEAT_EVERY:
                    jobs.heartbeat(session, worker_id, started_at, now)
                    jobs.recover_expired(session, now)
                    last_beat = now
                if last_schedule is None or now - last_schedule >= SCHEDULE_EVERY:
                    jobs.schedule_due(session, now, interval)
                    ratelimit.prune(session, now)
                    last_schedule = now
                job = jobs.claim(session, worker_id, now)
                if job is None:
                    stop.wait(POLL_SECONDS)
                    continue
                jobs.run(session, yelp, job, worker_id)
            except Exception:
                # Never let one bad job stop the worker; its lease expires and it is retried.
                log.exception("worker loop error", extra={"fields": fields})
                session.rollback()
                stop.wait(POLL_SECONDS)
    log.info("worker stopped", extra={"fields": fields})


def main():
    parser = argparse.ArgumentParser(prog="python -m app.worker", description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 0 if this worker is alive")
    args = parser.parse_args()
    if args.check:
        sys.exit(check())
    logs.configure(settings().log_level)
    run_forever()


if __name__ == "__main__":
    main()
