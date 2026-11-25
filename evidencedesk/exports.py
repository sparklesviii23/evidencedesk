"""CSV exports: records, reports, screening decisions, exclusions, risk of bias, extraction, studies."""
from __future__ import annotations

from pathlib import Path

from .db import DB, loads
from .evidence import write_csv
from .screening import resolve
from .studies import study_map


def write_tables(db: DB, review_id: int, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    recs = db.q("""SELECT r.id record_id, r.source, r.source_record_id, rr.report_id, rr.method link_method, rr.score,
                   r.title, r.year, r.journal, r.doi, r.pmid, r.pmcid, r.nct_ids_json, r.report_kind, r.first_run_id,
                   r.import_id, r.retrieved_at FROM records r LEFT JOIN record_report rr ON rr.record_id=r.id
                   WHERE r.review_id=? ORDER BY r.id""", (review_id,))
    write_csv(out / "records.csv", recs)
    smap = study_map(db, review_id)
    reps = []
    for r in db.q("SELECT * FROM reports WHERE review_id=? ORDER BY id", (review_id,)):
        ta = resolve(db, review_id, r["id"], "title_abstract")
        ft = resolve(db, review_id, r["id"], "full_text") if ta["final"] in ("include", "unclear") else {"final": None}
        reps.append({"report_id": r["id"], "merged_into": r["merged_into"], "study": smap.get(r["id"]),
                     "title": r["title"], "year": r["year"], "journal": r["journal"], "doi": r["doi"], "pmid": r["pmid"],
                     "pmcid": r["pmcid"], "nct_ids": ";".join(loads(r["nct_ids_json"], [])), "report_kind": r["report_kind"],
                     "ft_status": r["ft_status"], "ta_final": ta["final"], "ft_final": ft["final"],
                     "ft_exclusion_reason": ft.get("exclusion_reason")})
    write_csv(out / "reports.csv", reps)
    dec = db.q("""SELECT id decision_id, report_id, stage, decision, criterion_id, exclusion_reason, passage, location,
                  passage_verified, missing_info, confidence, rationale, protocol_version, actor_type, actor, status,
                  based_on, superseded_by, created_at FROM screening_decisions WHERE review_id=? ORDER BY id""",
                 (review_id,))
    write_csv(out / "screening_decisions.csv", dec)
    excl = [r for r in reps if r["ft_final"] == "exclude"]
    write_csv(out / "fulltext_exclusions.csv", [{"report_id": r["report_id"], "title": r["title"],
                                                 "reason": r["ft_exclusion_reason"], "study": r["study"]} for r in excl])
    write_csv(out / "risk_of_bias.csv", db.q("SELECT * FROM rob_assessments WHERE review_id=? ORDER BY id", (review_id,)))
    write_csv(out / "extraction.csv", db.q("SELECT * FROM extraction_rows WHERE review_id=? ORDER BY study_id, report_id, "
                                           "experiment, outcome, timepoint, arm", (review_id,)))
    write_csv(out / "studies.csv", db.q("""SELECT s.id study_id, s.label, s.notes, rs.report_id, rs.role, rs.evidence
                                           FROM studies s LEFT JOIN report_study rs ON rs.study_id=s.id
                                           WHERE s.review_id=? ORDER BY s.id""", (review_id,)))
    write_csv(out / "cohort_links.csv", db.q("SELECT * FROM cohort_links WHERE review_id=?", (review_id,)))
    write_csv(out / "fulltext_attempts.csv", db.q("""SELECT fa.* FROM fulltext_attempts fa JOIN reports r ON
                                                     r.id=fa.report_id WHERE r.review_id=? ORDER BY fa.id""", (review_id,)))
    return {"dir": str(out)}
