"""Background jobs for long agent operations (full-text retrieval, packet export, analysis).

The agent starts a job, gets an id back immediately and polls `job(id)` (cheap, a few dozen tokens) instead of
holding a tool call open for minutes. Jobs are persisted in SQLite so status survives a server restart; a job
interrupted by a restart is reported as `interrupted` and can simply be started again (all job kinds are
idempotent).
"""
from __future__ import annotations

import json
import threading
import traceback

from .credentials import redact
from .db import DB, now

SCHEMA = """CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, review_id INTEGER, kind TEXT NOT NULL, status TEXT NOT NULL, done INTEGER DEFAULT 0,
  total INTEGER DEFAULT 0, message TEXT, result_json TEXT, error TEXT, started_at TEXT NOT NULL, finished_at TEXT,
  worker TEXT)"""

_threads: dict[int, threading.Thread] = {}


def init(db: DB) -> None:
    with db.conn() as c:
        c.execute(SCHEMA)
    # anything still "running" was owned by a process that is gone unless this process is running it
    from .engine import WORKER
    for j in db.q("SELECT id, worker FROM jobs WHERE status='running'"):
        if j["worker"] == WORKER and j["id"] in _threads:
            continue
        if j["worker"] and j["worker"].split(":")[0] == WORKER.split(":")[0] and not _pid_alive(j["worker"]):
            db.update("jobs", j["id"], {"status": "interrupted", "finished_at": now(),
                                        "message": "server restarted; start the job again (it is idempotent)"})


def _pid_alive(worker: str) -> bool:
    import os
    try:
        os.kill(int(worker.split(":")[1]), 0)
        return True
    except (OSError, ValueError, IndexError):
        return False


class Progress:
    def __init__(self, db: DB, job_id: int):
        self.db, self.id = db, job_id

    def set(self, done: int | None = None, total: int | None = None, message: str | None = None) -> None:
        row = {}
        if done is not None:
            row["done"] = done
        if total is not None:
            row["total"] = total
        if message is not None:
            row["message"] = redact(message)[:500]
        self.db.update("jobs", self.id, row)


def start(db: DB, kind: str, review_id: int | None, fn, *args, **kw) -> int:
    """Run fn(progress, *args, **kw) in a thread. fn returns a JSON-serialisable result."""
    init(db)
    running = db.one("SELECT id FROM jobs WHERE kind=? AND review_id IS ? AND status='running'", (kind, review_id))
    if running and running["id"] in _threads and _threads[running["id"]].is_alive():
        return running["id"]
    from .engine import WORKER
    jid = db.insert("jobs", {"review_id": review_id, "kind": kind, "status": "running", "started_at": now(),
                             "worker": WORKER})

    def work():
        try:
            res = fn(Progress(db, jid), *args, **kw)
            db.update("jobs", jid, {"status": "done", "finished_at": now(),
                                    "result_json": json.dumps(res, default=str)})
        except Exception as e:  # job failures must be visible, never silent
            db.update("jobs", jid, {"status": "failed", "finished_at": now(),
                                    "error": redact(f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")[:3000]})
        finally:
            _threads.pop(jid, None)

    t = threading.Thread(target=work, daemon=True, name=f"job-{jid}")
    _threads[jid] = t
    t.start()
    return jid


def get(db: DB, job_id: int) -> dict:
    init(db)
    j = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not j:
        raise ValueError("job not found")
    out = {"job": j["id"], "kind": j["kind"], "status": j["status"], "progress": f"{j['done']}/{j['total']}" if j["total"] else None,
           "message": j["message"], "error": j["error"], "started": j["started_at"], "finished": j["finished_at"]}
    if j["result_json"]:
        out["result"] = json.loads(j["result_json"])
    return out


def wait(job_id: int, timeout: float) -> None:
    t = _threads.get(job_id)
    if t:
        t.join(timeout)


def active(db: DB, review_id: int) -> list[dict]:
    init(db)
    return db.q("SELECT id, kind, status, done, total, message FROM jobs WHERE review_id=? AND status='running'",
                (review_id,))
