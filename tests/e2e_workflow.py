"""Live end-to-end workflow through the REST API, acting as an AI agent + two reviewers + adjudicator.

Usage: python tests/e2e_workflow.py http://127.0.0.1:8765 <review_id>
Requires a running server (evidencedesk serve) whose review already has a completed search.
The agent's title/abstract proposals use transparent rules with verbatim supporting passages; reviewers
confirm or disagree; a deliberate disagreement set exercises adjudication.
"""
from __future__ import annotations

import functools
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

print = functools.partial(print, flush=True)

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
RID = int(sys.argv[2]) if len(sys.argv) > 2 else 1
HOME = Path(os.environ.get("EVIDENCEDESK_HOME", Path.home() / ".evidencedesk"))
TOK = (HOME / "api_token").read_text().strip()
C = httpx.Client(base_url=BASE, headers={"X-EvidenceDesk-Token": TOK}, timeout=1800)


def op(name, **kw):
    r = C.post(f"/api/op/{name}", json=kw)
    j = r.json()
    if not j.get("ok", True) and "markdown" not in j:
        raise RuntimeError(f"{name}: {j}")
    return j.get("result", j.get("markdown"))


TARGET = re.compile(r"ANGPTL3|angiopoietin[- ]like (protein )?3|Angptl3|CTX[- ]?310|VERVE-201", re.I)
EDIT = re.compile(r"CRISPR|genome edit|gene edit|base edit|prime edit|Cas9|Cas12|editing", re.I)
INVIVO = re.compile(r"in vivo|mice|mouse|murine|rats?\b|monkeys?|primates?|macaques?|patients|participants|"
                    r"trial|first-in-human|hamsters?", re.I)


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


def best_passage(title, abstract, *patterns):
    for s in sentences(abstract):
        if all(p.search(s) for p in patterns):
            return s, "Abstract"
    if title and all(p.search(title) for p in patterns):
        return title, "Title"
    for s in sentences(abstract):
        if patterns[0].search(s):
            return s, "Abstract"
    return None, None


def agent_ta(rep):
    t, a, kind = rep["title"] or "", rep["abstract"] or "", rep["report_kind"]
    text = f"{t} {a}"
    if not TARGET.search(text):
        return dict(decision="exclude", criterion_id="I", passage=t, location="Title",
                    rationale="No ANGPTL3 target in title/abstract (off-topic hit, e.g. CTX310 machine tool).",
                    confidence=0.95)
    if kind in ("review", "letter_editorial", "book"):
        p, loc = best_passage(t, a, TARGET)
        return dict(decision="exclude", criterion_id="S", passage=p or t, location=loc or "Title",
                    rationale=f"Report type '{kind}': not primary research.", confidence=0.85)
    if not EDIT.search(text):
        p, loc = best_passage(t, a, TARGET)
        return dict(decision="exclude", criterion_id="I2", passage=p or t, location=loc or "Title",
                    rationale="ANGPTL3 studied without genome editing.", confidence=0.8)
    p, loc = best_passage(t, a, TARGET, EDIT)
    if not a:
        return dict(decision="unclear", criterion_id="P", passage=p or t, location=loc or "Title",
                    missing_info="No abstract: model/population and design unknown.", confidence=0.5)
    if INVIVO.search(text):
        p2, loc2 = best_passage(t, a, INVIVO, TARGET)
        return dict(decision="include", criterion_id="I", passage=p2 or p, location=loc2 or loc,
                    rationale="ANGPTL3-targeted genome editing with an in vivo model/population.", confidence=0.8)
    return dict(decision="unclear", criterion_id="P", passage=p, location=loc,
                missing_info="In vivo model not stated in abstract.", confidence=0.5)


PHASE = sys.argv[3] if len(sys.argv) > 3 else "all"


def main():
    t0 = time.time()
    if PHASE == "fulltext":
        return fulltext_phase(t0)
    reps = op("list_reports", review_id=RID, limit=1000)
    print(f"{len(reps)} reports")
    # --- dedup review queue: decide every pending candidate (as reviewer)
    for c in op("dedup_candidates", review_id=RID):
        a, b = c["a"], c["b"]
        kinds = {a["report_kind"], b["report_kind"]}
        if a["doi"] and a["doi"] == b["doi"] and {a["report_kind"], b["report_kind"]} == {"letter_editorial"}:
            dec = "not_duplicate"  # NEJM letter vs reply share a DOI: different reports
        elif kinds & {"preprint", "conference_abstract", "dataset"} or (a["doi"] and b["doi"] and a["doi"] != b["doi"]):
            dec = "same_study"     # preprint/abstract/data deposit vs article: different reports, same study
        else:
            dec = "merge"
        try:
            op("decide_candidate", review_id=RID, candidate_id=c["id"], decision=dec, actor="reviewer:alice")
            print(f"  dedup {c['id']}: {dec}  ({a['title'][:50]} | {a['report_kind']} vs {b['report_kind']})")
        except RuntimeError as e:
            print("  dedup decision failed:", e)
    # --- study links suggested from NCT ids / registry references
    for s in op("suggest_study_links", review_id=RID):
        op("decide_study_link", review_id=RID, suggestion_id=s["id"], accept=True, actor="reviewer:alice")
        print(f"  study link accepted: {s['reason']}")
    # --- prioritise, then agent proposals at title/abstract
    op("keyword_prioritize", review_id=RID)
    reps = op("list_reports", review_id=RID, limit=1000)
    proposals = {}
    for r in reps:
        full = op("get_report", report_id=r["id"])
        p = agent_ta(full)
        res = op("propose_screening", review_id=RID, report_id=r["id"], stage="title_abstract", agent="e2e", **p)
        proposals[r["id"]] = (p, res)
    unverified = [rid for rid, (p, res) in proposals.items() if res["passage_verified"] is False]
    print(f"agent proposals: {len(proposals)}; passages not verifiable: {unverified}")
    # --- two reviewers. Bob disagrees on 4 'include' proposals to exercise adjudication
    inc = [rid for rid, (p, _) in proposals.items() if p["decision"] == "include"]
    disagree = set(inc[:2] + [rid for rid, (p, _) in proposals.items() if p["decision"] == "unclear"][:2])
    for rid, (p, res) in proposals.items():
        for reviewer in ("alice", "bob"):
            d = dict(p)
            d.pop("confidence", None)
            if reviewer == "bob" and rid in disagree:
                d["decision"] = "exclude" if p["decision"] != "exclude" else "include"
                d["criterion_id"] = "P"
                d["rationale"] = "Bob: population/model not clearly in vivo from abstract."
            op("reviewer_decision", review_id=RID, report_id=rid, stage="title_abstract", reviewer=reviewer,
               based_on=res["decision_id"], **d)
    conf = op("conflicts", review_id=RID, stage="title_abstract")
    print(f"TA conflicts: {len(conf)}")
    for c in conf:
        prop = proposals[c["report_id"]][0]
        op("adjudicate", review_id=RID, report_id=c["report_id"], stage="title_abstract",
           decision="include" if prop["decision"] in ("include", "unclear") else "exclude", adjudicator="charlie",
           criterion_id=prop["criterion_id"], rationale="Adjudicated: forwarded to full text for verification.")
    fulltext_phase(t0)


def fulltext_phase(t0):
    ft_queue = op("screening_queue", review_id=RID, stage="full_text", limit=1000)
    print(f"full-text stage: {len(ft_queue)} reports")
    # --- full-text retrieval
    t1 = time.time()
    res = op("retrieve_fulltext", review_id=RID, scope="full_text_stage")
    print(f"retrieval finished in {time.time() - t1:.0f}s")
    print(json.dumps(op("fulltext_summary", review_id=RID), indent=1)[:1500])
    # --- full-text decisions (agent proposal + 2 reviewers)
    for q in ft_queue:
        rep = op("get_report", report_id=q["report_id"], include_text=True)
        segs = rep.get("segments") or []
        text = " ".join(s["text"] for s in segs) or (rep["abstract"] or "")
        has_ft = rep["ft_status"] == "retrieved" or rep["report_kind"] in ("conference_abstract", "registry")
        if not has_ft and not rep["abstract"]:
            d = dict(decision="exclude", exclusion_reason="full text unavailable", criterion_id="S",
                     missing_info="No full text and no abstract retrievable via open routes.")
        else:
            hit = None
            for s in segs or [{"text": rep["abstract"] or "", "location": "Abstract"}]:
                if TARGET.search(s["text"]) and EDIT.search(s["text"]) and INVIVO.search(s["text"]):
                    hit = s
                    break
            if hit:
                sent = next((x for x in sentences(hit["text"]) if TARGET.search(x) and (EDIT.search(x) or
                                                                                      INVIVO.search(x))), hit["text"][:400])
                d = dict(decision="include", criterion_id="I", passage=sent, location=hit["location"],
                         rationale="In vivo ANGPTL3-targeted editing reported in the source text.")
            elif TARGET.search(text) and EDIT.search(text):
                d = dict(decision="exclude", criterion_id="P", exclusion_reason="wrong population/model",
                         passage=next((x for x in sentences(text) if TARGET.search(x)), None), location="full text",
                         rationale="ANGPTL3 editing mentioned but no in vivo model/population reported.")
            else:
                d = dict(decision="exclude", criterion_id="I", exclusion_reason="wrong intervention",
                         rationale="No ANGPTL3-targeted editing in the source text.")
        pr = op("propose_screening", review_id=RID, report_id=q["report_id"], stage="full_text", agent="e2e", **d)
        for reviewer in ("alice", "bob"):
            op("reviewer_decision", review_id=RID, report_id=q["report_id"], stage="full_text", reviewer=reviewer,
               based_on=pr["decision_id"], **d)
    print("FT conflicts:", len(op("conflicts", review_id=RID, stage="full_text")))
    counts = op("prisma_counts", review_id=RID)
    print(json.dumps({k: v for k, v in counts.items() if k != "checks"}, indent=1))
    print("checks:", [(c["check"], c["ok"]) for c in counts["checks"]])
    pk = op("export_evidence_packet", review_id=RID, scope="full_text_stage")
    print("packet:", json.dumps(pk, indent=1))
    print(f"total {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
