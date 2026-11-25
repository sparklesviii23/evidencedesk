"""Search execution engine shared by the UI, REST API and MCP server.

A run = one EDQL query executed against a set of sources. Every source gets a source_run row holding the exact
native query, the compiler notes, approval state, connector status, reported hit count, retrieved count, the
pagination cursor (persisted after every page, so runs are resumable after a crash or restart), retries and
errors. Records are stored verbatim and deduplicated incrementally.
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from .adapters import ADAPTERS, SEARCH_SOURCES
from .credentials import redact
from .db import DB, loads, now
from .dedup import Deduper
from .http import SourceError
from .query.compilers import compile_query
from .query.parser import parse

log = logging.getLogger("evidencedesk.engine")

import os
WORKER = f"{os.uname().nodename}:{os.getpid()}"
RUNNABLE = ("queued", "running", "partial", "failed")
_active: dict[int, threading.Thread] = {}
_cancel: set[int] = set()
_lock = threading.Lock()


def preview(query_text: str, sources: list[str] | None = None) -> dict:
    """Validate + compile for every selected source. No network."""
    q = parse(query_text)
    selected = _selected_sources(q, sources)
    out = []
    for s in selected:
        a = ADAPTERS[s]
        c = compile_query(q, s)
        conn = a.connection_status()
        out.append({**c.to_dict(), "connector": conn, "verified_live": a.verified_live,
                    "execution": _execution_mode(c, conn)})
    return {"query": q.to_dict(), "sources": out}


def _selected_sources(q, sources: list[str] | None) -> list[str]:
    if sources:
        sel = sources
    elif q.sources is None:
        sel = [s for s in SEARCH_SOURCES if s not in q.sources_except]
    else:
        sel = q.sources
    bad = [s for s in sel if s not in ADAPTERS or ADAPTERS[s].role != "search"]
    if bad:
        raise ValueError(f"not search sources: {bad}")
    return sel


def _execution_mode(c, conn) -> str:
    if c.status == "unsupported":
        return "manual"  # needs manual execution + import
    if conn["status"] == "disconnected":
        return "disconnected"
    if c.status == "approximate":
        return "awaiting_approval"
    return "automatic"


def start_run(db: DB, review_id: int, query_text: str, *, sources=None, approve: list[str] | None = None,
              actor="user", max_records=5000, saved_query_id=None, background=True) -> int:
    q = parse(query_text)
    selected = _selected_sources(q, sources)
    approve = set(approve or [])
    run_id = db.insert("search_runs", {
        "review_id": review_id, "saved_query_id": saved_query_id, "query_text": query_text,
        "ast_json": json.dumps(q.to_dict()), "filters_json": json.dumps({"date": q.date.to_dict() if q.date else None}),
        "status": "running", "started_at": now(), "created_by": actor, "max_records": max_records})
    for s in selected:
        a = ADAPTERS[s]
        c = compile_query(q, s)
        conn = a.connection_status()
        mode = _execution_mode(c, conn)
        status = {"automatic": "queued", "manual": "manual_required", "disconnected": "disconnected",
                  "awaiting_approval": "queued" if s in approve or "all" in approve else "awaiting_approval"}[mode]
        db.insert("source_runs", {
            "run_id": run_id, "source": s, "platform": c.platform, "native_query": c.native,
            "compiled_json": json.dumps(c.to_dict()), "fidelity": c.status,
            "approved": 1 if (c.status != "approximate" or s in approve or "all" in approve) else 0,
            "approved_by": actor if (c.status == "approximate" and (s in approve or "all" in approve)) else None,
            "connector_status": json.dumps(conn), "status": status,
            "error": conn["detail"] if status == "disconnected" else None})
    db.audit(review_id, actor, "start_search", {"run_id": run_id, "sources": selected})
    execute(db, run_id, background=background)
    return run_id


def approve_source(db: DB, run_id: int, source: str, actor: str, background=True) -> dict:
    sr = db.one("SELECT * FROM source_runs WHERE run_id=? AND source=?", (run_id, source))
    if not sr:
        raise ValueError("no such source in run")
    if sr["status"] != "awaiting_approval":
        raise ValueError(f"source status is {sr['status']}, not awaiting_approval")
    db.update("source_runs", sr["id"], {"approved": 1, "approved_by": actor, "status": "queued"})
    db.update("search_runs", run_id, {"status": "running", "finished_at": None})
    execute(db, run_id, background=background)
    return {"approved": source}


def skip_source(db: DB, run_id: int, source: str, actor: str) -> None:
    sr = db.one("SELECT * FROM source_runs WHERE run_id=? AND source=?", (run_id, source))
    db.update("source_runs", sr["id"], {"status": "skipped", "error": f"skipped by {actor}"})
    _finalize(db, run_id)


def cancel(run_id: int) -> None:
    _cancel.add(run_id)


def resume(db: DB, run_id: int, background=True, raise_cap: int | None = None) -> None:
    if raise_cap:
        db.update("search_runs", run_id, {"max_records": raise_cap})
    db.x("UPDATE source_runs SET status='queued' WHERE run_id=? AND status IN ('running','partial','failed')",
         (run_id,))
    db.update("search_runs", run_id, {"status": "running", "finished_at": None})
    execute(db, run_id, background=background)


def resume_interrupted(db: DB, stale_seconds=180) -> list[int]:
    """Called at startup: resume runs whose worker process died mid-pagination (no heartbeat for
    `stale_seconds`). Runs still heart-beating in another process (API server vs MCP server) are left alone."""
    import datetime as dt
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=stale_seconds)).replace(microsecond=0).isoformat()
    ids = []
    for r in db.q("SELECT id FROM search_runs WHERE status='running'"):
        live = db.one("SELECT 1 FROM source_runs WHERE run_id=? AND status='running' AND heartbeat_at>?", (r["id"], cutoff))
        if not live and r["id"] not in _active:
            resume(db, r["id"])
            ids.append(r["id"])
    return ids


def execute(db: DB, run_id: int, background=True) -> None:
    def work():
        try:
            srs = db.q("SELECT * FROM source_runs WHERE run_id=? AND status='queued'", (run_id,))
            with ThreadPoolExecutor(max_workers=max(1, len(srs))) as ex:
                list(ex.map(lambda sr: _run_source(db, run_id, sr["id"]), srs))
        finally:
            _finalize(db, run_id)
            with _lock:
                _active.pop(run_id, None)
                _cancel.discard(run_id)

    with _lock:
        if run_id in _active and _active[run_id].is_alive():
            if not background:
                _active[run_id].join()
            return
        if background:
            t = threading.Thread(target=work, daemon=True, name=f"run-{run_id}")
            _active[run_id] = t
            t.start()
            return
    work()


def _run_source(db: DB, run_id: int, sr_id: int) -> None:
    sr = db.one("SELECT * FROM source_runs WHERE id=?", (sr_id,))
    run = db.one("SELECT * FROM search_runs WHERE id=?", (run_id,))
    review_id = run["review_id"]
    adapter = ADAPTERS[sr["source"]]
    comp = loads(sr["compiled_json"])
    compiled = SimpleNamespace(requests=comp["requests"], native=comp["native"])
    cursor = loads(sr["cursor_json"])
    if sr["complete"]:
        return
    retrieved = sr["retrieved"] or 0
    new_records = sr["new_records"] or 0
    pages = sr["pages"] or 0
    retries = sr["retries"] or 0
    alerts = []
    db.update("source_runs", sr_id, {"status": "running", "heartbeat_at": now(), "worker": WORKER,
                                     "started_at": sr["started_at"] or now(),
                                     "executed_at": sr["executed_at"] or now(), "error": None})
    deduper = Deduper(db, review_id)
    rank = retrieved

    def on_retry(attempt, msg):
        nonlocal retries
        retries += 1
        db.update("source_runs", sr_id, {"retries": retries, "error": redact(f"retrying (attempt {attempt}): {msg}")})

    try:
        while True:
            if run_id in _cancel:
                db.update("source_runs", sr_id, {"status": "partial", "incomplete_reason": "cancelled by user"})
                return
            if retrieved >= (run["max_records"] or 10 ** 9):
                db.update("source_runs", sr_id, {"status": "partial",
                                                 "incomplete_reason": f"max_records cap {run['max_records']} reached; "
                                                                      f"resume with a higher cap to continue"})
                return
            page = adapter.search(compiled, cursor, on_retry=on_retry)
            pages += 1
            for note in page.notes:
                if note.startswith("ALERT"):
                    alerts.append(note)
            for rec in page.records:
                rank += 1
                rid, is_new = store_record(db, review_id, rec, run_id=run_id, source_run_id=sr_id, rank=rank)
                if is_new:
                    new_records += 1
                    deduper.attach(rid)
            retrieved += len(page.records)
            cursor = page.cursor
            db.update("source_runs", sr_id, {
                "reported_hits": page.total, "retrieved": retrieved, "new_records": new_records, "pages": pages,
                "cursor_json": json.dumps(cursor) if cursor else None,
                "error": None, "heartbeat_at": now(), "worker": WORKER,
                **({"platform_notes": redact(" | ".join(page.notes))[:4000]} if page.notes else {})})
            if cursor is None:
                break
        complete_status = "complete_with_alerts" if alerts else "complete"
        total = db.one("SELECT reported_hits FROM source_runs WHERE id=?", (sr_id,))["reported_hits"]
        reason = None
        unique_retrieved = db.one("SELECT COUNT(DISTINCT record_id) c FROM record_hits WHERE source_run_id=?",
                                  (sr_id,))["c"]
        if total is not None and unique_retrieved < total and sr["source"] != "openalex":
            complete_status = "complete_with_alerts"
            reason = f"retrieved {unique_retrieved} unique records but source reported {total}"
        db.update("source_runs", sr_id, {"status": complete_status, "complete": 1, "finished_at": now(),
                                         "incomplete_reason": reason or (" ; ".join(alerts) if alerts else None)})
    except SourceError as e:
        db.update("source_runs", sr_id, {
            "status": "failed" if e.kind not in ("auth", "access") else "access_denied",
            "error": redact(f"[{e.kind}] {e}")[:4000], "finished_at": now(), "retries": retries,
            "incomplete_reason": f"stopped after {pages} pages / {retrieved} records; resumable" if pages else None})
    except Exception as e:  # pragma: no cover - unexpected bug: keep visible
        log.exception("source run failed")
        db.update("source_runs", sr_id, {"status": "failed", "error": redact(f"[internal] {type(e).__name__}: {e}"),
                                         "finished_at": now()})


def store_record(db: DB, review_id: int, rec: dict, *, run_id=None, source_run_id=None, import_id=None,
                 rank=None) -> tuple[int, bool]:
    existing = db.one("SELECT id FROM records WHERE review_id=? AND source=? AND source_record_id=?",
                      (review_id, rec["source"], rec["source_record_id"]))
    if existing:
        rid, new = existing["id"], False
    else:
        raw = rec.get("raw")
        rid = db.insert("records", {
            "review_id": review_id, "source": rec["source"], "source_record_id": rec["source_record_id"],
            "first_run_id": run_id, "first_source_run_id": source_run_id, "import_id": import_id,
            "title": rec.get("title"), "abstract": rec.get("abstract"), "authors_json": json.dumps(rec.get("authors") or []),
            "year": rec.get("year"), "pub_date": rec.get("pub_date"), "journal": rec.get("journal"),
            "volume": rec.get("volume"), "issue": rec.get("issue"), "pages": rec.get("pages"), "doi": rec.get("doi"),
            "pmid": rec.get("pmid"), "pmcid": rec.get("pmcid"), "nct_ids_json": json.dumps(rec.get("nct_ids") or []),
            "other_ids_json": json.dumps(rec.get("other_ids") or {}, default=str),
            "pub_types_json": json.dumps(rec.get("pub_types") or []), "keywords_json": json.dumps(rec.get("keywords") or []),
            "mesh_json": json.dumps(rec.get("mesh") or []), "language": rec.get("language"), "url": rec.get("url"),
            "is_oa": rec.get("is_oa"), "report_kind": rec.get("report_kind"), "raw_format": rec.get("raw_format"),
            "raw": raw if isinstance(raw, str) else json.dumps(raw, default=str), "retrieved_at": now()})
        new = True
    db.insert("record_hits", {"record_id": rid, "run_id": run_id, "source_run_id": source_run_id,
                              "import_id": import_id, "rank": rank, "hit_at": now()})
    return rid, new


def _finalize(db: DB, run_id: int) -> None:
    srs = db.q("SELECT status FROM source_runs WHERE run_id=?", (run_id,))
    st = [s["status"] for s in srs]
    if any(s in ("queued", "running") for s in st):
        return
    if any(s == "awaiting_approval" for s in st):
        status = "awaiting_approval"
    elif any(s in ("failed", "partial", "access_denied") for s in st):
        status = "incomplete"
    elif any(s in ("manual_required", "disconnected") for s in st):
        status = "complete_automatic_sources"
    else:
        status = "complete"
    db.update("search_runs", run_id, {"status": status, "finished_at": now()})


def run_status(db: DB, run_id: int) -> dict:
    run = db.one("SELECT * FROM search_runs WHERE id=?", (run_id,))
    if not run:
        raise ValueError("run not found")
    srs = db.q("SELECT * FROM source_runs WHERE run_id=? ORDER BY id", (run_id,))
    out = []
    for s in srs:
        comp = loads(s["compiled_json"], {})
        out.append({
            "source": s["source"], "platform": s["platform"], "status": s["status"], "fidelity": s["fidelity"],
            "approved": bool(s["approved"]), "approved_by": s["approved_by"], "native_query": s["native_query"],
            "notes": comp.get("notes", []), "connector": loads(s["connector_status"], {}),
            "reported_hits": s["reported_hits"], "retrieved": s["retrieved"], "new_records": s["new_records"],
            "pages": s["pages"], "complete": bool(s["complete"]), "incomplete_reason": s["incomplete_reason"],
            "error": s["error"], "platform_notes": s["platform_notes"], "retries": s["retries"], "executed_at": s["executed_at"],
            "finished_at": s["finished_at"], "resumable": bool(s["cursor_json"]) and not s["complete"],
            "zero_results": s["complete"] == 1 and (s["reported_hits"] == 0),
        })
    newly = db.one("""SELECT COUNT(DISTINCT rr.report_id) c FROM records r JOIN record_report rr ON rr.record_id=r.id
                      JOIN reports rp ON rp.id=rr.report_id
                      WHERE r.first_run_id=? AND NOT EXISTS (SELECT 1 FROM record_report rr2 JOIN records r2 ON
                      r2.id=rr2.record_id WHERE rr2.report_id=rr.report_id AND (r2.first_run_id IS NULL OR
                      r2.first_run_id<?))""", (run_id, run_id))["c"]
    return {"run_id": run_id, "review_id": run["review_id"], "status": run["status"], "query": run["query_text"],
            "started_at": run["started_at"], "finished_at": run["finished_at"], "max_records": run["max_records"],
            "created_by": run["created_by"], "sources": out, "new_reports_this_run": newly,
            "active": run_id in _active}


def wait(run_id: int, timeout: float | None = None) -> None:
    t = _active.get(run_id)
    if t:
        t.join(timeout)
