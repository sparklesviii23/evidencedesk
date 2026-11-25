"""PICOS protocols (versioned) and two-stage screening with agent proposals, independent reviewers and
adjudication. Agent proposals and priorities are stored separately from confirmed eligibility decisions and
never count toward a final decision."""
from __future__ import annotations

import json
import re

from .db import DB, loads, now
from .query.evaluate import norm_tokens

ELEMENTS = ["population", "intervention", "comparator", "outcomes", "study_design"]
STATUSES = ["required", "optional", "not_applicable"]
DECISIONS = ["include", "exclude", "unclear"]
STAGES = ["title_abstract", "full_text"]

DEFAULT_EXCLUSION_REASONS = [
    "wrong population/model", "wrong intervention", "wrong comparator", "wrong outcomes", "wrong study design",
    "duplicate publication / overlapping cohort", "not primary research", "full text unavailable",
    "insufficient data reported", "language", "other"]


def protocol_template(evidence_type="mixed") -> dict:
    return {
        "question": "",
        "evidence_type": evidence_type,  # human | animal | mixed
        "reviewers_required": {"title_abstract": 2, "full_text": 2},
        "criteria": [
            {"id": "P", "element": "population", "status": "required", "label": "Population / model",
             "include": "", "exclude": "", "applies_to": ["human", "animal"], "in_search": True,
             "search_terms": []},
            {"id": "I", "element": "intervention", "status": "required", "label": "Intervention",
             "include": "", "exclude": "", "in_search": True, "search_terms": []},
            {"id": "C", "element": "comparator", "status": "optional", "label": "Comparator",
             "include": "", "exclude": "", "in_search": False, "search_terms": []},
            {"id": "O", "element": "outcomes", "status": "optional", "label": "Outcomes",
             "include": "", "exclude": "", "in_search": False, "search_terms": []},
            {"id": "S", "element": "study_design", "status": "required", "label": "Study design",
             "include": "", "exclude": "", "in_search": False, "search_terms": []},
        ],
        "exclusion_reasons": DEFAULT_EXCLUSION_REASONS,
        "notes": "",
    }


def validate_protocol(p: dict) -> list[str]:
    errs = []
    if p.get("evidence_type") not in ("human", "animal", "mixed"):
        errs.append("evidence_type must be human | animal | mixed")
    ids = set()
    for c in p.get("criteria", []):
        if not c.get("id"):
            errs.append("every criterion needs an id")
        if c.get("id") in ids:
            errs.append(f"duplicate criterion id {c.get('id')}")
        ids.add(c.get("id"))
        if c.get("element") not in ELEMENTS:
            errs.append(f"{c.get('id')}: element must be one of {ELEMENTS}")
        if c.get("status") not in STATUSES:
            errs.append(f"{c.get('id')}: status must be one of {STATUSES}")
        if c.get("status") == "required" and not (c.get("include") or c.get("exclude")):
            errs.append(f"{c.get('id')}: required criterion needs an inclusion or exclusion rule")
    rr = p.get("reviewers_required", {})
    for st in STAGES:
        if rr.get(st) not in (1, 2):
            errs.append(f"reviewers_required.{st} must be 1 or 2")
    return errs


def save_protocol(db: DB, review_id: int, picos: dict, actor: str, note: str | None = None) -> dict:
    errs = validate_protocol(picos)
    if errs:
        raise ValueError("; ".join(errs))
    cur = db.one("SELECT MAX(version) v FROM protocols WHERE review_id=?", (review_id,))["v"] or 0
    db.insert("protocols", {"review_id": review_id, "version": cur + 1, "picos_json": json.dumps(picos),
                            "change_note": note, "created_at": now(), "created_by": actor})
    db.audit(review_id, actor, "save_protocol", {"version": cur + 1, "note": note})
    return {"version": cur + 1}


def get_protocol(db: DB, review_id: int, version: int | None = None) -> dict | None:
    if version:
        r = db.one("SELECT * FROM protocols WHERE review_id=? AND version=?", (review_id, version))
    else:
        r = db.one("SELECT * FROM protocols WHERE review_id=? ORDER BY version DESC LIMIT 1", (review_id,))
    if not r:
        return None
    return {"version": r["version"], "created_at": r["created_at"], "created_by": r["created_by"],
            "change_note": r["change_note"], "picos": json.loads(r["picos_json"])}


def draft_query(picos: dict, include_elements: list[str] | None = None, field="TITLE_ABSTRACT") -> dict:
    """Sensitive draft: AND across the elements marked in_search (default P and I), OR within each element.
    Comparator / outcome / design are left out unless explicitly requested, because they are often only
    reported in the full text; they are still applied at screening."""
    blocks, used, left_out = [], [], []
    for c in picos.get("criteria", []):
        el = c["element"]
        wanted = (el in include_elements) if include_elements else c.get("in_search")
        terms = [t for t in c.get("search_terms", []) if t.strip()]
        if not wanted or c.get("status") == "not_applicable" or not terms:
            left_out.append({"criterion": c["id"], "element": el,
                             "why": "not in search (applied at screening)" if not wanted else
                             ("not applicable" if c.get("status") == "not_applicable" else "no search terms")})
            continue
        parts = []
        for t in terms:
            t = t.strip()
            if t.upper().startswith(("MESH(", "EMTREE(")):
                parts.append(("vocab", t))
            elif re.search(r"\s", t) and not t.startswith('"'):
                parts.append(("text", f'"{t}"'))
            else:
                parts.append(("text", t))
        text = [p for k, p in parts if k == "text"]
        vocab = [p for k, p in parts if k == "vocab"]
        seg = []
        if text:
            seg.append(f"{field}({' OR '.join(text)})")
        seg += vocab
        blocks.append("(" + " OR ".join(seg) + ")" if len(seg) > 1 else seg[0])
        used.append(c["id"])
    q = "SOURCES ALL\n\n" + "\nAND\n".join(blocks) if blocks else ""
    return {"query": q, "criteria_in_search": used, "left_out": left_out}


# ------------------------------------------------------------------------- decisions
def _passage_verified(db: DB, report_id: int, passage: str | None) -> bool | None:
    if not passage:
        return None
    from .fulltext import segments
    rep = db.one("SELECT title, abstract FROM reports WHERE id=?", (report_id,))
    hay = " ".join(norm_tokens(" ".join([rep["title"] or "", rep["abstract"] or ""] +
                                        [s["text"] for s in segments(db, report_id)])))
    needle = " ".join(norm_tokens(passage.replace("…", " ").replace("...", " ")))
    if not needle:
        return None
    if needle in hay:
        return True
    # tolerate ellipsis-joined quotes: every fragment must be present
    frags = [" ".join(norm_tokens(f)) for f in re.split(r"\.\.\.|…", passage) if len(norm_tokens(f)) >= 3]
    return bool(frags) and all(f in hay for f in frags)


def record_decision(db: DB, review_id: int, report_id: int, stage: str, decision: str, *, actor: str,
                    actor_type: str, criterion_id=None, exclusion_reason=None, passage=None, location=None,
                    missing_info=None, confidence=None, rationale=None, based_on=None,
                    protocol_version=None) -> dict:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    if actor_type not in ("agent", "reviewer", "adjudicator"):
        raise ValueError("actor_type must be agent | reviewer | adjudicator")
    proto = get_protocol(db, review_id, protocol_version)
    if not proto:
        raise ValueError("review has no protocol; save a PICOS protocol first")
    crit_ids = {c["id"] for c in proto["picos"]["criteria"]}
    if criterion_id and criterion_id not in crit_ids:
        raise ValueError(f"criterion_id must be one of {sorted(crit_ids)}")
    if decision == "exclude" and stage == "full_text" and not exclusion_reason:
        raise ValueError("full-text exclusions need an exclusion_reason (PRISMA)")
    if exclusion_reason and exclusion_reason not in proto["picos"].get("exclusion_reasons", DEFAULT_EXCLUSION_REASONS):
        raise ValueError(f"exclusion_reason must be one of {proto['picos'].get('exclusion_reasons')}")
    if decision == "exclude" and not criterion_id and actor_type == "agent":
        raise ValueError("agent exclusions must name the criterion that drove the decision")
    if stage == "full_text":
        ta = resolve(db, review_id, report_id, "title_abstract")
        if ta["final"] not in ("include", "unclear"):
            raise ValueError(f"report is not at full-text stage (title/abstract final: {ta['final']})")
    rep = db.one("SELECT merged_into FROM reports WHERE id=?", (report_id,))
    if not rep:
        raise ValueError("report not found")
    if rep["merged_into"]:
        raise ValueError(f"RPT-{report_id} was merged into RPT-{rep['merged_into']}; screen that report")
    status = {"agent": "proposed", "reviewer": "confirmed", "adjudicator": "adjudicated"}[actor_type]
    verified = _passage_verified(db, report_id, passage)
    with db.conn():
        did = db.insert("screening_decisions", {
            "review_id": review_id, "report_id": report_id, "stage": stage, "decision": decision,
            "criterion_id": criterion_id, "exclusion_reason": exclusion_reason, "passage": passage,
            "location": location, "missing_info": missing_info, "confidence": confidence,
            "rationale": rationale, "protocol_version": proto["version"], "actor_type": actor_type, "actor": actor,
            "status": status, "based_on": based_on, "created_at": now(), "passage_verified": None if verified is None
            else int(verified)})
        db.x("""UPDATE screening_decisions SET superseded_by=? WHERE report_id=? AND stage=? AND actor=? AND id<>?
                AND superseded_by IS NULL""", (did, report_id, stage, actor, did))
    db.audit(review_id, actor, f"screen_{status}", {"report_id": report_id, "stage": stage, "decision": decision})
    return {"decision_id": did, "status": status, "passage_verified": verified, "protocol_version": proto["version"],
            "resolution": resolve(db, review_id, report_id, stage)}


def _report_family(db: DB, report_id: int) -> list[int]:
    ids = [report_id]
    frontier = [report_id]
    while frontier:
        nxt = [r["id"] for r in db.q(f"SELECT id FROM reports WHERE merged_into IN ({','.join('?' * len(frontier))})",
                                     frontier)]
        ids += nxt
        frontier = nxt
    return ids


def active_decisions(db: DB, report_id: int, stage: str) -> list[dict]:
    fam = _report_family(db, report_id)
    return db.q(f"""SELECT * FROM screening_decisions WHERE report_id IN ({','.join('?' * len(fam))}) AND stage=?
                    AND superseded_by IS NULL ORDER BY id""", (*fam, stage))


def resolve(db: DB, review_id: int, report_id: int, stage: str, required: int | None = None) -> dict:
    ds = active_decisions(db, report_id, stage)
    if required is None:
        proto = get_protocol(db, review_id)
        required = (proto["picos"].get("reviewers_required", {}) if proto else {}).get(stage, 2)
    adj = [d for d in ds if d["actor_type"] == "adjudicator"]
    if adj:
        d = adj[-1]
        return {"final": d["decision"], "basis": "adjudicated", "decision_id": d["id"],
                "exclusion_reason": d["exclusion_reason"]}
    rev = [d for d in ds if d["actor_type"] == "reviewer"]
    agent = [d for d in ds if d["actor_type"] == "agent"]
    if not rev:
        return {"final": "pending", "basis": "agent_proposal_only" if agent else "unscreened",
                "agent_proposal": agent[-1]["decision"] if agent else None}
    vals = {d["decision"] for d in rev}
    if len(vals) > 1:
        return {"final": "conflict", "basis": "reviewer_disagreement",
                "reviewers": [{"actor": d["actor"], "decision": d["decision"]} for d in rev]}
    if len(rev) < required:
        return {"final": "pending", "basis": f"{len(rev)}/{required} reviewers", "tentative": rev[0]["decision"]}
    reasons = [d["exclusion_reason"] for d in rev if d["exclusion_reason"]]
    return {"final": rev[0]["decision"], "basis": f"{len(rev)} reviewers agree",
            "exclusion_reason": reasons[0] if reasons else None}


def set_priority(db: DB, review_id: int, report_id: int, stage: str, score: float, rationale: str, actor: str):
    proto = get_protocol(db, review_id)
    db.x("DELETE FROM agent_priorities WHERE report_id=? AND stage=? AND actor=?", (report_id, stage, actor))
    return db.insert("agent_priorities", {"review_id": review_id, "report_id": report_id, "stage": stage,
                                          "score": score, "rationale": rationale, "actor": actor,
                                          "protocol_version": proto["version"] if proto else None,
                                          "created_at": now()})


def keyword_prioritize(db: DB, review_id: int, stage="title_abstract") -> int:
    """Baseline ranking from protocol search terms (transparent, deterministic). Stored as a priority by
    'system:keyword-ranker'; it never makes or confirms decisions."""
    proto = get_protocol(db, review_id)
    if not proto:
        return 0
    terms = []
    for c in proto["picos"]["criteria"]:
        for t in c.get("search_terms", []):
            if not t.upper().startswith(("MESH(", "EMTREE(")):
                terms.append((c["id"], norm_tokens(t.strip('"*'))))
    n = 0
    for r in db.q("SELECT id, title, abstract FROM reports WHERE review_id=? AND merged_into IS NULL", (review_id,)):
        toks = norm_tokens(f"{r['title'] or ''} {r['abstract'] or ''}")
        s = " ".join(toks)
        hit_crit = {cid for cid, t in terms if t and " ".join(t) in s}
        score = len(hit_crit) + 0.01 * sum(s.count(" ".join(t)) for _, t in terms if t)
        set_priority(db, review_id, r["id"], stage, round(score, 3),
                     f"protocol terms matched for criteria {sorted(hit_crit)}", "system:keyword-ranker")
        n += 1
    return n


def queue(db: DB, review_id: int, stage: str, filter_: str = "all", reviewer: str | None = None,
          limit=500, offset=0) -> list[dict]:
    reps = db.q("SELECT * FROM reports WHERE review_id=? AND merged_into IS NULL ORDER BY id", (review_id,))
    prio = {}
    for p in db.q("SELECT report_id, MAX(score) s, rationale, actor FROM agent_priorities WHERE review_id=? AND stage=? "
                  "GROUP BY report_id", (review_id, stage)):
        prio[p["report_id"]] = p
    proto = get_protocol(db, review_id)
    req = (proto["picos"].get("reviewers_required", {}) if proto else {}).get(stage, 2)
    out = []
    for r in reps:
        if stage == "full_text":
            ta = resolve(db, review_id, r["id"], "title_abstract", req)
            if ta["final"] not in ("include", "unclear"):
                continue
        res = resolve(db, review_id, r["id"], stage, req)
        ds = active_decisions(db, r["id"], stage)
        mine = next((d for d in ds if reviewer and d["actor"] == reviewer), None)
        if filter_ == "pending" and res["final"] != "pending":
            continue
        if filter_ == "conflict" and res["final"] != "conflict":
            continue
        if filter_ == "unclear" and res["final"] != "unclear":
            continue
        if filter_ == "mine_todo" and (mine or res["final"] not in ("pending",)):
            continue
        if filter_ == "decided" and res["final"] in ("pending", "conflict"):
            continue
        out.append({"report_id": r["id"], "title": r["title"], "year": r["year"], "report_kind": r["report_kind"],
                    "ft_status": r["ft_status"], "resolution": res,
                    "priority": prio.get(r["id"], {}).get("s"), "priority_by": prio.get(r["id"], {}).get("actor"),
                    "agent_proposals": [d["decision"] for d in ds if d["actor_type"] == "agent"],
                    "reviewer_decisions": [{"actor": d["actor"], "decision": d["decision"]} for d in ds
                                           if d["actor_type"] == "reviewer"]})
    out.sort(key=lambda x: (-(x["priority"] or 0), x["report_id"]))
    return out[offset:offset + limit]
