"""Studies are the unit of synthesis. Reports (journal article, preprint, conference abstract, registry entry,
erratum ...) are LINKED to a study, never merged into it, so each report keeps its own content.
Cohort links record overlapping populations and shared control groups between studies."""
from __future__ import annotations

from .db import DB, loads, now


def _study_of(db: DB, report_id: int) -> int | None:
    r = db.one("SELECT study_id FROM report_study WHERE report_id=? ORDER BY linked_at LIMIT 1", (report_id,))
    return r["study_id"] if r else None


def create_study(db: DB, review_id: int, label: str, actor: str, notes=None) -> int:
    sid = db.insert("studies", {"review_id": review_id, "label": label, "notes": notes, "created_at": now()})
    db.audit(review_id, actor, "create_study", {"study_id": sid, "label": label})
    return sid


def link_report(db: DB, review_id: int, report_id: int, study_id: int, actor: str, role=None, evidence=None):
    db.x("INSERT OR REPLACE INTO report_study (report_id, study_id, role, actor, evidence, linked_at) "
         "VALUES (?,?,?,?,?,?)", (report_id, study_id, role or _role(db, report_id), actor, evidence, now()))
    db.audit(review_id, actor, "link_report_study", {"report_id": report_id, "study_id": study_id})


def unlink_report(db: DB, review_id: int, report_id: int, study_id: int, actor: str):
    db.x("DELETE FROM report_study WHERE report_id=? AND study_id=?", (report_id, study_id))
    db.audit(review_id, actor, "unlink_report_study", {"report_id": report_id, "study_id": study_id})


def _role(db: DB, report_id: int) -> str:
    r = db.one("SELECT report_kind FROM reports WHERE id=?", (report_id,))
    return {"registry": "registry_record", "preprint": "preprint", "conference_abstract": "conference_abstract",
            "erratum": "erratum"}.get((r or {}).get("report_kind"), "journal_article")


def link_same_study(db: DB, review_id: int, report_ids: list[int], actor: str, evidence: str) -> int:
    existing = [s for s in (_study_of(db, r) for r in report_ids) if s]
    if len(set(existing)) > 1:
        raise ValueError(f"reports already belong to different studies {sorted(set(existing))}; merge studies first")
    if existing:
        sid = existing[0]
    else:
        first = db.one("SELECT title, year, authors_json FROM reports WHERE id=?", (report_ids[0],))
        au = (loads(first["authors_json"], []) or ["Unknown"])[0]
        label = f"{au.split()[0] if au else 'Study'} {first['year'] or ''}".strip()
        sid = create_study(db, review_id, label, actor)
    for r in report_ids:
        if _study_of(db, r) != sid:
            link_report(db, review_id, r, sid, actor, evidence=evidence)
    return sid


def merge_studies(db: DB, review_id: int, keep: int, drop: int, actor: str):
    with db.conn():
        for rs in db.q("SELECT * FROM report_study WHERE study_id=?", (drop,)):
            db.x("INSERT OR IGNORE INTO report_study (report_id, study_id, role, actor, evidence, linked_at) VALUES "
                 "(?,?,?,?,?,?)", (rs["report_id"], keep, rs["role"], actor, f"from merged STU-{drop}", now()))
        db.x("DELETE FROM report_study WHERE study_id=?", (drop,))
        db.x("UPDATE extraction_rows SET study_id=? WHERE study_id=?", (keep, drop))
        db.x("UPDATE studies SET notes=COALESCE(notes,'') || ? WHERE id=?", (f" [STU-{drop} merged here]", keep))
    db.audit(review_id, actor, "merge_studies", {"keep": keep, "drop": drop})


def add_cohort_link(db: DB, review_id: int, a: int, b: int, kind: str, actor: str, notes=None) -> int:
    if kind not in ("overlapping_cohort", "shared_control", "same_animals", "extension"):
        raise ValueError("kind must be overlapping_cohort | shared_control | same_animals | extension")
    return db.insert("cohort_links", {"review_id": review_id, "study_a": a, "study_b": b, "kind": kind,
                                      "notes": notes, "actor": actor, "created_at": now()})


def suggest_links(db: DB, review_id: int) -> int:
    """Suggest report pairs that likely describe the same study:
      - publication mentions/declares an NCT id that matches a registry report
      - registry record lists a publication PMID as a reference
      - Crossref preprint <-> published-version relations
    """
    reps = db.q("SELECT * FROM reports WHERE review_id=? AND merged_into IS NULL", (review_id,))
    by_nct, by_pmid, by_doi = {}, {}, {}
    for r in reps:
        if r["report_kind"] == "registry":
            for n in loads(r["nct_ids_json"], [])[:1]:
                by_nct[n] = r["id"]
        if r["pmid"]:
            by_pmid[r["pmid"]] = r["id"]
        if r["doi"]:
            by_doi[r["doi"]] = r["id"]
    pairs = []
    shared: dict[str, list[int]] = {}
    for r in reps:
        if r["report_kind"] != "registry":
            for n in loads(r["nct_ids_json"], []):
                shared.setdefault(n, []).append(r["id"])
                if n in by_nct:
                    pairs.append((by_nct[n], r["id"], f"publication cites registry {n}"))
    for n, ids in shared.items():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pairs.append((ids[i], ids[j], f"both reports cite trial registration {n}"))
    for rec in db.q("""SELECT r.raw, r.source, rr.report_id FROM records r JOIN record_report rr ON rr.record_id=r.id
                       WHERE r.review_id=? AND r.source IN ('ctgov','crossref','openalex','europepmc')""", (review_id,)):
        raw = loads(rec["raw"], {}) or {}
        if rec["source"] == "ctgov":
            refs = raw.get("protocolSection", {}).get("referencesModule", {}).get("references", [])
            for ref in refs:
                p = ref.get("pmid")
                if p in by_pmid:
                    pairs.append((rec["report_id"], by_pmid[p], f"registry lists PMID {p} ({ref.get('type')})"))
        if rec["source"] == "crossref":
            rel = raw.get("relation") or {}
            for k in ("is-preprint-of", "has-preprint", "is-version-of", "has-version"):
                for item in rel.get(k, []):
                    d = (item.get("id") or "").lower()
                    if d in by_doi:
                        pairs.append((rec["report_id"], by_doi[d], f"Crossref relation {k}"))
    n = 0
    for a, b, why in pairs:
        if a == b:
            continue
        sa, sb = _study_of(db, a), _study_of(db, b)
        if sa and sa == sb:
            continue
        n += db.x("INSERT OR IGNORE INTO study_link_suggestions (review_id, report_a, report_b, reason, created_at) "
                  "VALUES (?,?,?,?,?)", (review_id, min(a, b), max(a, b), why, now())) and 1 or 0
    return n


def decide_suggestion(db: DB, review_id: int, sid: int, accept: bool, actor: str) -> dict:
    s = db.one("SELECT * FROM study_link_suggestions WHERE id=?", (sid,))
    if accept:
        study = link_same_study(db, review_id, [s["report_a"], s["report_b"]], actor, s["reason"])
        db.update("study_link_suggestions", sid, {"status": "accepted"})
        return {"study_id": study}
    db.update("study_link_suggestions", sid, {"status": "rejected"})
    return {"status": "rejected"}


def study_map(db: DB, review_id: int) -> dict[int, int]:
    """report_id -> study key. Unlinked reports are their own study (negative key)."""
    out = {}
    for r in db.q("SELECT id FROM reports WHERE review_id=? AND merged_into IS NULL", (review_id,)):
        out[r["id"]] = -r["id"]
    for rs in db.q("""SELECT rs.report_id, rs.study_id FROM report_study rs JOIN studies s ON s.id=rs.study_id
                      WHERE s.review_id=?""", (review_id,)):
        out[rs["report_id"]] = rs["study_id"]
    return out
