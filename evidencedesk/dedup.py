"""Deduplication: records -> reports.

Order of evidence
  1. Identifiers (DOI, PMID, PMCID; NCT for registry records). Guarded: a shared DOI with dissimilar titles
     (e.g. a supplement-issue DOI shared by many conference abstracts) goes to the review queue instead.
  2. Cautious bibliographic matching: auto-merge only when normalised titles are near-identical (>=97),
     publication years equal, first-author surnames agree, report kinds compatible and no identifier
     conflicts. 85-97 or any conflict -> review queue.
Records are never modified or deleted. Merges are events that can be undone.
Registry records only merge with other registry records (same NCT); articles that cite an NCT become study-link
suggestions, not duplicates.
"""
from __future__ import annotations

import json
import re
import unicodedata

from rapidfuzz import fuzz

from .db import DB, loads, now

SOURCE_PRIORITY = ["pubmed", "europepmc", "crossref", "scopus", "wos", "embase", "sciencedirect", "openalex",
                   "import", "ctgov"]
AUTO_TITLE = 97
REVIEW_TITLE = 85


def norm_title(t: str | None) -> str:
    if not t:
        return ""
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(t.split())


def surname(authors) -> str:
    if not authors:
        return ""
    a = authors[0] if isinstance(authors, list) else str(authors)
    a = a or ""
    if "," in a:
        a = a.split(",")[0]
    else:
        parts = a.split()
        # "Chadwick AC" -> Chadwick ; "Alexandra C Chadwick" -> Chadwick
        if len(parts) > 1 and (parts[-1].isupper() and len(parts[-1]) <= 3):
            a = parts[0]
        elif parts:
            a = parts[-1]
    return norm_title(a)


def _src_rank(src: str) -> int:
    base = src.split(":")[0]
    return SOURCE_PRIORITY.index(base) if base in SOURCE_PRIORITY else len(SOURCE_PRIORITY)


def _kind_family(k: str | None) -> str:
    return "registry" if k == "registry" else "publication"


class Deduper:
    def __init__(self, db: DB, review_id: int):
        self.db = db
        self.review_id = review_id

    # ------------------------------------------------------------------ attach
    def attach(self, record_id: int) -> dict:
        db = self.db
        rec = db.one("SELECT * FROM records WHERE id=?", (record_id,))
        if db.one("SELECT 1 FROM record_report WHERE record_id=?", (record_id,)):
            return {"report_id": db.one("SELECT report_id FROM record_report WHERE record_id=?", (record_id,))[
                "report_id"], "method": "existing"}
        fam = _kind_family(rec["report_kind"])
        cands = self._id_matches(rec, fam)
        if cands:
            # prefer the identifier match with no conflicting identifiers and the most similar title
            # (e.g. a reply sharing the letter's DOI but carrying its own PMID belongs with that PMID)
            def rank(cid):
                rp = db.one("SELECT * FROM reports WHERE id=?", (cid,))
                s = fuzz.ratio(norm_title(rec["title"]), norm_title(rp["title"])) if rec["title"] and rp["title"] else 100
                return (self._id_conflict(rec, rp) is not None, -s, cid)
            cands = sorted(cands, key=rank)
            target = cands[0]
            rep = db.one("SELECT * FROM reports WHERE id=?", (target,))
            sim = fuzz.ratio(norm_title(rec["title"]), norm_title(rep["title"])) if rec["title"] and rep["title"] \
                else 100
            conflict = self._id_conflict(rec, rep)
            if sim < 60 or conflict:
                new = self._new_report(rec, "new")
                reason = (f"shares an identifier with RPT-{target} but titles differ (similarity {sim:.0f}); "
                          "possible supplement/issue-level DOI") if sim < 60 else f"identifier conflict: {conflict}"
                self._candidate(target, new, sim, reason)
                return {"report_id": new, "method": "queued"}
            self._link(record_id, target, "id", sim)
            for other in cands[1:]:
                orep = db.one("SELECT * FROM reports WHERE id=?", (other,))
                osim = fuzz.ratio(norm_title(rep["title"]), norm_title(orep["title"])) if rep["title"] and \
                    orep["title"] else 100
                if osim >= 90 and not self._report_conflict(target, other):
                    self.merge(target, other, actor="system", method="id-bridge",
                               reason=f"record REC-{record_id} carries identifiers of both reports")
                else:
                    self._candidate(target, other, osim, "identifiers bridge both reports but titles differ")
            self.refresh(target)
            return {"report_id": target, "method": "id"}
        # bibliographic
        best = self._bib_best(rec, fam)
        if best:
            rid, score, ok, why = best
            if ok:
                self._link(record_id, rid, "bibliographic", score)
                self.refresh(rid)
                return {"report_id": rid, "method": "bibliographic"}
            new = self._new_report(rec, "new")
            self._candidate(rid, new, score, why)
            return {"report_id": new, "method": "queued"}
        return {"report_id": self._new_report(rec, "new"), "method": "new"}

    def _id_matches(self, rec: dict, fam: str) -> list[int]:
        out: list[int] = []
        conds, params = [], []
        if fam == "registry":
            ncts = loads(rec["nct_ids_json"], [])
            if rec["source"] == "ctgov" or rec["source_record_id"].upper().startswith("NCT"):
                ncts = ncts[:1] if rec["source"] == "ctgov" else ncts
            for n in ncts:
                rows = self.db.q("""SELECT DISTINCT rp.id FROM reports rp JOIN record_report rr ON rr.report_id=rp.id
                    JOIN records r ON r.id=rr.record_id WHERE rp.review_id=? AND rp.merged_into IS NULL
                    AND rp.report_kind='registry' AND (r.source_record_id=? OR r.nct_ids_json LIKE ?)""",
                                 (self.review_id, n, f'%"{n}"%'))
                out += [r["id"] for r in rows]
            return sorted(set(out))
        for col in ("doi", "pmid", "pmcid"):
            if rec[col]:
                conds.append(f"r.{col}=?")
                params.append(rec[col])
        if not conds:
            return []
        rows = self.db.q(f"""SELECT DISTINCT rp.id FROM records r JOIN record_report rr ON rr.record_id=r.id
            JOIN reports rp ON rp.id=rr.report_id WHERE r.review_id=? AND rp.merged_into IS NULL
            AND (rp.report_kind IS NULL OR rp.report_kind!='registry') AND ({' OR '.join(conds)})""",
                         (self.review_id, *params))
        return sorted(r["id"] for r in rows)

    def _id_conflict(self, rec: dict, rep: dict) -> str | None:
        for col in ("doi", "pmid"):
            if rec[col] and rep[col] and rec[col] != rep[col]:
                return f"{col} {rec[col]} vs {rep[col]}"
        return None

    def _report_conflict(self, a: int, b: int) -> str | None:
        ra = self.db.one("SELECT * FROM reports WHERE id=?", (a,))
        rb = self.db.one("SELECT * FROM reports WHERE id=?", (b,))
        return self._id_conflict(ra, rb)

    def _bib_best(self, rec: dict, fam: str):
        nt = norm_title(rec["title"])
        if len(nt) < 20:
            return None
        rows = self.db.q("""SELECT id, title, year, authors_json, doi, pmid, report_kind FROM reports
            WHERE review_id=? AND merged_into IS NULL""", (self.review_id,))
        best = None
        for r in rows:
            if _kind_family(r["report_kind"]) != fam:
                continue
            s = fuzz.ratio(nt, norm_title(r["title"]))
            if s >= REVIEW_TITLE and (best is None or s > best[1]):
                best = (r, s)
        if not best:
            return None
        r, s = best
        reasons = []
        if rec["year"] and r["year"] and rec["year"] != r["year"]:
            reasons.append(f"year {rec['year']} vs {r['year']}")
        sa, sb = surname(loads(rec["authors_json"], [])), surname(loads(r["authors_json"], []))
        if sa and sb and sa != sb:
            reasons.append(f"first author {sa} vs {sb}")
        c = self._id_conflict(rec, r)
        if c:
            reasons.append(f"identifier conflict ({c}) – possibly a preprint/version or erratum")
        if rec["report_kind"] != r["report_kind"] and {rec["report_kind"], r["report_kind"]} & {
                "preprint", "conference_abstract", "erratum"}:
            reasons.append(f"report kinds differ ({rec['report_kind']} vs {r['report_kind']})")
        ok = s >= AUTO_TITLE and not reasons and (sa and sb)
        why = f"title similarity {s:.0f}" + ("; " + "; ".join(reasons) if reasons else "") + \
            ("" if (sa and sb) else "; first author missing")
        return r["id"], s, bool(ok), why

    # ------------------------------------------------------------------ storage helpers
    def _new_report(self, rec: dict, method: str) -> int:
        rid = self.db.insert("reports", {"review_id": self.review_id, "created_at": now(),
                                         "title": rec["title"], "report_kind": rec["report_kind"]})
        self._link(rec["id"], rid, method, None)
        self.refresh(rid)
        return rid

    def _link(self, record_id: int, report_id: int, method: str, score) -> None:
        self.db.x("INSERT OR REPLACE INTO record_report (record_id, report_id, method, score, linked_at) "
                  "VALUES (?,?,?,?,?)", (record_id, report_id, method, score, now()))

    def _candidate(self, a: int, b: int, score, reason: str) -> None:
        a, b = min(a, b), max(a, b)
        self.db.x("INSERT OR IGNORE INTO dedup_candidates (review_id, report_a, report_b, score, reason, created_at)"
                  " VALUES (?,?,?,?,?,?)", (self.review_id, a, b, score, reason, now()))

    def refresh(self, report_id: int) -> None:
        recs = self.db.q("""SELECT r.* FROM records r JOIN record_report rr ON rr.record_id=r.id
                            WHERE rr.report_id=?""", (report_id,))
        if not recs:
            return
        recs.sort(key=lambda r: _src_rank(r["source"]))
        def pick(col, longest=False):
            vals = [r[col] for r in recs if r[col]]
            if not vals:
                return None
            return max(vals, key=len) if longest else vals[0]
        ncts = []
        for r in recs:
            ncts += loads(r["nct_ids_json"], [])
        kinds = [r["report_kind"] for r in recs if r["report_kind"]]
        authors = next((r["authors_json"] for r in recs if loads(r["authors_json"])), "[]")
        self.db.update("reports", report_id, {
            "title": pick("title"), "abstract": pick("abstract", longest=True), "authors_json": authors,
            "year": pick("year"), "journal": pick("journal"), "doi": pick("doi"), "pmid": pick("pmid"),
            "pmcid": pick("pmcid"), "nct_ids_json": json.dumps(list(dict.fromkeys(ncts))),
            "report_kind": kinds[0] if kinds else None})

    # ------------------------------------------------------------------ merge / undo
    def merge(self, target: int, source: int, actor: str, method="manual", reason=None) -> int:
        if target == source:
            raise ValueError("cannot merge a report into itself")
        t = self.db.one("SELECT * FROM reports WHERE id=?", (target,))
        s = self.db.one("SELECT * FROM reports WHERE id=?", (source,))
        if not t or not s or t["merged_into"] or s["merged_into"]:
            raise ValueError("both reports must exist and be active")
        rec_ids = [r["record_id"] for r in self.db.q("SELECT record_id FROM record_report WHERE report_id=?",
                                                     (source,))]
        with self.db.conn():
            self.db.x("UPDATE record_report SET report_id=?, method=? WHERE report_id=?",
                      (target, f"merge:{method}", source))
            self.db.update("reports", source, {"merged_into": target})
            ev = self.db.insert("merge_events", {"review_id": self.review_id, "kind": "merge",
                                                 "target_report": target, "source_report": source,
                                                 "record_ids_json": rec_ids, "method": method, "actor": actor,
                                                 "reason": reason, "created_at": now()})
            self.db.x("""UPDATE dedup_candidates SET status='merged', decided_by=?, decided_at=? WHERE
                         ((report_a=? AND report_b=?) OR (report_a=? AND report_b=?)) AND status='pending'""",
                      (actor, now(), min(target, source), max(target, source), min(target, source),
                       max(target, source)))
            # links to studies follow the surviving report
            for rs in self.db.q("SELECT * FROM report_study WHERE report_id=?", (source,)):
                self.db.x("INSERT OR IGNORE INTO report_study (report_id, study_id, role, actor, evidence, "
                          "linked_at) VALUES (?,?,?,?,?,?)", (target, rs["study_id"], rs["role"], "merge",
                                                             f"inherited from RPT-{source}", now()))
            self.refresh(target)
        return ev

    def undo(self, event_id: int, actor: str) -> dict:
        ev = self.db.one("SELECT * FROM merge_events WHERE id=?", (event_id,))
        if not ev or ev["undone_at"]:
            raise ValueError("event not found or already undone")
        if ev["kind"] == "merge":
            t = self.db.one("SELECT * FROM reports WHERE id=?", (ev["target_report"],))
            if t["merged_into"]:
                raise ValueError(f"RPT-{t['id']} was later merged into RPT-{t['merged_into']}; undo that first")
            with self.db.conn():
                for rid in loads(ev["record_ids_json"], []):
                    self.db.x("UPDATE record_report SET report_id=?, method='undo' WHERE record_id=? AND report_id=?",
                              (ev["source_report"], rid, ev["target_report"]))
                self.db.update("reports", ev["source_report"], {"merged_into": None})
                self.db.x("DELETE FROM report_study WHERE report_id=? AND evidence=?",
                          (ev["target_report"], f"inherited from RPT-{ev['source_report']}"))
                self.db.update("merge_events", event_id, {"undone_at": now(), "undone_by": actor})
                self.db.x("""UPDATE dedup_candidates SET status='pending', decided_by=NULL, decided_at=NULL
                             WHERE report_a=? AND report_b=?""",
                          (min(ev["target_report"], ev["source_report"]), max(ev["target_report"],
                                                                              ev["source_report"])))
                self.refresh(ev["target_report"])
                self.refresh(ev["source_report"])
        elif ev["kind"] == "split":
            with self.db.conn():
                for rid in loads(ev["record_ids_json"], []):
                    self.db.x("UPDATE record_report SET report_id=? WHERE record_id=?", (ev["target_report"], rid))
                self.db.update("reports", ev["source_report"], {"merged_into": ev["target_report"]})
                self.db.update("merge_events", event_id, {"undone_at": now(), "undone_by": actor})
                self.refresh(ev["target_report"])
        return {"undone": event_id}

    def split(self, record_id: int, actor: str, reason=None) -> dict:
        rr = self.db.one("SELECT * FROM record_report WHERE record_id=?", (record_id,))
        n = self.db.one("SELECT COUNT(*) c FROM record_report WHERE report_id=?", (rr["report_id"],))["c"]
        if n < 2:
            raise ValueError("report has a single record; nothing to split")
        rec = self.db.one("SELECT * FROM records WHERE id=?", (record_id,))
        with self.db.conn():
            new = self.db.insert("reports", {"review_id": self.review_id, "created_at": now(), "title": rec["title"],
                                             "report_kind": rec["report_kind"]})
            self._link(record_id, new, "split", None)
            ev = self.db.insert("merge_events", {"review_id": self.review_id, "kind": "split",
                                                 "target_report": rr["report_id"], "source_report": new,
                                                 "record_ids_json": [record_id], "method": "manual", "actor": actor,
                                                 "reason": reason, "created_at": now()})
            self.refresh(new)
            self.refresh(rr["report_id"])
            self._candidate(rr["report_id"], new, None, "manually split; kept for audit")
            self.db.x("UPDATE dedup_candidates SET status='not_duplicate', decided_by=? WHERE report_a=? AND report_b=?",
                      (actor, min(rr["report_id"], new), max(rr["report_id"], new)))
        return {"new_report": new, "event": ev}

    def decide(self, candidate_id: int, decision: str, actor: str) -> dict:
        c = self.db.one("SELECT * FROM dedup_candidates WHERE id=?", (candidate_id,))
        if not c:
            raise ValueError("candidate not found")
        if decision == "merge":
            ev = self.merge(c["report_a"], c["report_b"], actor, "manual", c["reason"])
            return {"merged": True, "event": ev}
        if decision in ("not_duplicate", "same_study"):
            self.db.update("dedup_candidates", candidate_id, {"status": decision, "decided_by": actor,
                                                              "decided_at": now()})
            if decision == "same_study":
                from .studies import link_same_study
                link_same_study(self.db, self.review_id, [c["report_a"], c["report_b"]], actor,
                                "dedup review: different reports of the same study")
            return {"status": decision}
        raise ValueError("decision must be merge | not_duplicate | same_study")
