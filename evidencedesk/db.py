"""SQLite storage. One database per workspace (default ~/.evidencedesk/evidencedesk.db).

Entity model
  record   – one hit as returned by one source (raw payload preserved verbatim)
  report   – one publication / registry entry / abstract; deduplicated from >=1 records
  study    – the underlying investigation; linked to >=1 reports (never merged into them)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, evidence_type TEXT NOT NULL DEFAULT 'human',
  description TEXT, created_at TEXT NOT NULL, settings_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS protocols (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL REFERENCES reviews(id), version INTEGER NOT NULL,
  picos_json TEXT NOT NULL, change_note TEXT, created_at TEXT NOT NULL, created_by TEXT,
  UNIQUE(review_id, version));
CREATE TABLE IF NOT EXISTS saved_queries (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL REFERENCES reviews(id), name TEXT NOT NULL,
  query_text TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(review_id, name));
CREATE TABLE IF NOT EXISTS search_runs (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL REFERENCES reviews(id), saved_query_id INTEGER,
  query_text TEXT NOT NULL, ast_json TEXT NOT NULL, filters_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, created_by TEXT, max_records INTEGER);
CREATE TABLE IF NOT EXISTS source_runs (
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES search_runs(id), source TEXT NOT NULL,
  platform TEXT, native_query TEXT, compiled_json TEXT, fidelity TEXT, approved INTEGER NOT NULL DEFAULT 0,
  approved_by TEXT, connector_status TEXT, status TEXT NOT NULL, reported_hits INTEGER, retrieved INTEGER DEFAULT 0,
  new_records INTEGER DEFAULT 0, pages INTEGER DEFAULT 0, cursor_json TEXT, complete INTEGER DEFAULT 0,
  incomplete_reason TEXT, platform_notes TEXT, error TEXT, retries INTEGER DEFAULT 0, started_at TEXT, finished_at TEXT,
  executed_at TEXT, heartbeat_at TEXT, worker TEXT, UNIQUE(run_id, source));
CREATE TABLE IF NOT EXISTS imports (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL REFERENCES reviews(id), source TEXT NOT NULL,
  platform TEXT, file_name TEXT, file_format TEXT, file_sha256 TEXT, native_query TEXT, search_date TEXT,
  record_count INTEGER, notes TEXT, run_id INTEGER, imported_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL REFERENCES reviews(id), source TEXT NOT NULL,
  source_record_id TEXT NOT NULL, first_run_id INTEGER, first_source_run_id INTEGER, import_id INTEGER,
  title TEXT, abstract TEXT, authors_json TEXT, year INTEGER, pub_date TEXT, journal TEXT, volume TEXT,
  issue TEXT, pages TEXT, doi TEXT, pmid TEXT, pmcid TEXT, nct_ids_json TEXT, other_ids_json TEXT,
  pub_types_json TEXT, keywords_json TEXT, mesh_json TEXT, language TEXT, url TEXT, is_oa INTEGER,
  report_kind TEXT, raw_format TEXT, raw TEXT, retrieved_at TEXT NOT NULL,
  UNIQUE(review_id, source, source_record_id));
CREATE INDEX IF NOT EXISTS ix_records_doi ON records(review_id, doi);
CREATE INDEX IF NOT EXISTS ix_records_pmid ON records(review_id, pmid);
CREATE TABLE IF NOT EXISTS record_hits (
  record_id INTEGER NOT NULL REFERENCES records(id), run_id INTEGER, source_run_id INTEGER, import_id INTEGER,
  rank INTEGER, hit_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_hits_run ON record_hits(run_id);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL REFERENCES reviews(id), title TEXT, abstract TEXT,
  authors_json TEXT, year INTEGER, journal TEXT, doi TEXT, pmid TEXT, pmcid TEXT, nct_ids_json TEXT,
  report_kind TEXT, merged_into INTEGER REFERENCES reports(id), created_at TEXT NOT NULL,
  ft_status TEXT NOT NULL DEFAULT 'not_sought', ft_unavailable_reason TEXT);
CREATE TABLE IF NOT EXISTS record_report (
  record_id INTEGER PRIMARY KEY REFERENCES records(id), report_id INTEGER NOT NULL REFERENCES reports(id),
  method TEXT NOT NULL, score REAL, linked_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_rr_report ON record_report(report_id);
CREATE TABLE IF NOT EXISTS dedup_candidates (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, report_a INTEGER NOT NULL, report_b INTEGER NOT NULL,
  score REAL, reason TEXT, status TEXT NOT NULL DEFAULT 'pending', decided_by TEXT, decided_at TEXT,
  created_at TEXT NOT NULL, UNIQUE(report_a, report_b));
CREATE TABLE IF NOT EXISTS merge_events (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, kind TEXT NOT NULL, target_report INTEGER,
  source_report INTEGER, record_ids_json TEXT, method TEXT, actor TEXT, reason TEXT, created_at TEXT NOT NULL,
  undone_at TEXT, undone_by TEXT);
CREATE TABLE IF NOT EXISTS studies (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, label TEXT NOT NULL, notes TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS report_study (
  report_id INTEGER NOT NULL REFERENCES reports(id), study_id INTEGER NOT NULL REFERENCES studies(id),
  role TEXT, actor TEXT, evidence TEXT, linked_at TEXT NOT NULL, PRIMARY KEY(report_id, study_id));
CREATE TABLE IF NOT EXISTS study_link_suggestions (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, report_a INTEGER NOT NULL, report_b INTEGER NOT NULL,
  reason TEXT, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, UNIQUE(report_a, report_b));
CREATE TABLE IF NOT EXISTS cohort_links (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, study_a INTEGER NOT NULL, study_b INTEGER NOT NULL,
  kind TEXT NOT NULL, notes TEXT, actor TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, report_id INTEGER REFERENCES reports(id), kind TEXT NOT NULL,
  path TEXT NOT NULL, sha256 TEXT, mime TEXT, size INTEGER, origin TEXT, origin_url TEXT, license TEXT,
  original_name TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fulltext_attempts (
  id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL REFERENCES reports(id), route TEXT NOT NULL, url TEXT,
  status TEXT NOT NULL, http_status INTEGER, detail TEXT, file_id INTEGER, attempted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS text_extractions (
  id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL, file_id INTEGER NOT NULL, method TEXT NOT NULL,
  status TEXT NOT NULL, text_path TEXT, segments_path TEXT, pages INTEGER, chars INTEGER, ocr_pages INTEGER,
  error TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS screening_decisions (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, report_id INTEGER NOT NULL REFERENCES reports(id),
  stage TEXT NOT NULL, decision TEXT NOT NULL, criterion_id TEXT, exclusion_reason TEXT, passage TEXT,
  location TEXT, missing_info TEXT, confidence REAL, rationale TEXT, protocol_version INTEGER NOT NULL,
  actor_type TEXT NOT NULL, actor TEXT NOT NULL, status TEXT NOT NULL, based_on INTEGER, passage_verified INTEGER,
  superseded_by INTEGER, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_sd_report ON screening_decisions(report_id, stage);
CREATE TABLE IF NOT EXISTS agent_priorities (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, report_id INTEGER NOT NULL, stage TEXT NOT NULL,
  score REAL NOT NULL, rationale TEXT, actor TEXT, protocol_version INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS extraction_rows (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, report_id INTEGER NOT NULL, study_id INTEGER,
  experiment TEXT, model TEXT, outcome TEXT NOT NULL, outcome_type TEXT NOT NULL DEFAULT 'continuous',
  timepoint TEXT, timepoint_value REAL, timepoint_unit TEXT, arm TEXT NOT NULL, arm_role TEXT,
  control_group_id TEXT, n REAL, mean REAL, sd REAL, se REAL, ci_low REAL, ci_high REAL, ci_level REAL,
  median REAL, iqr_low REAL, iqr_high REAL, events REAL, unit TEXT, direction TEXT, value_origin TEXT NOT NULL,
  derivation TEXT, source_location TEXT, source_passage TEXT, actor_type TEXT NOT NULL, actor TEXT,
  status TEXT NOT NULL, verified_by TEXT, verified_at TEXT, notes TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rob_assessments (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, report_id INTEGER, study_id INTEGER, tool TEXT NOT NULL,
  domain TEXT NOT NULL, judgement TEXT NOT NULL, support TEXT, location TEXT, actor_type TEXT NOT NULL,
  actor TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS analysis_plans (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, version INTEGER NOT NULL, plan_json TEXT NOT NULL,
  created_at TEXT NOT NULL, UNIQUE(review_id, version));
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY, review_id INTEGER, actor TEXT, action TEXT NOT NULL, payload_json TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS benchmark_sets (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, name TEXT NOT NULL, items_json TEXT NOT NULL,
  created_at TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def data_dir() -> Path:
    d = Path(os.environ.get("EVIDENCEDESK_HOME", Path.home() / ".evidencedesk"))
    d.mkdir(parents=True, exist_ok=True)
    return d


class DB:
    def __init__(self, path: str | Path | None = None):
        self.path = str(path or data_dir() / "evidencedesk.db")
        self._local = threading.local()
        self._lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    @contextmanager
    def conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            c = self._local.c = self._connect()
        if getattr(self._local, "depth", 0):
            self._local.depth += 1
            try:
                yield c
            finally:
                self._local.depth -= 1
            return
        self._local.depth = 1
        try:
            with self._lock:
                yield c
                c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            self._local.depth = 0

    def q(self, sql: str, params=()) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        r = self.q(sql, params)
        return r[0] if r else None

    def x(self, sql: str, params=()) -> int:
        with self.conn() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid

    def insert(self, table: str, row: dict) -> int:
        cols = list(row)
        vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in row.values()]
        return self.x(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)

    def update(self, table: str, id_: int, row: dict) -> None:
        if not row:
            return
        vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in row.values()]
        self.x(f"UPDATE {table} SET {','.join(f'{k}=?' for k in row)} WHERE id=?", [*vals, id_])

    def audit(self, review_id, actor, action, payload=None):
        self.insert("audit_log", {"review_id": review_id, "actor": actor, "action": action,
                                  "payload_json": json.dumps(payload or {}, default=str), "at": now()})


def loads(s, default=None):
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default
