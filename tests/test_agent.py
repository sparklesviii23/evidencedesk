"""Lean agent toolkit: end-to-end on stored records (offline)."""
import base64

from conftest import rec

PICOS = {"question": "q", "reviewers": {"ta": 1, "ft": 1},
         "criteria": [{"id": "P", "element": "population", "include": "mice in vivo"},
                      {"id": "I", "element": "intervention", "include": "ANGPTL3 editing", "terms": ["ANGPTL3"]},
                      {"id": "S", "element": "study_design", "include": "primary", "exclude": "reviews"}],
         "reasons": ["wrong intervention", "full text unavailable", "other"]}


def test_agent_flow(home):
    from evidencedesk import agent, service as S
    from evidencedesk.dedup import Deduper
    from evidencedesk.engine import store_record
    msg = agent.setup_review("T", "animal", PICOS)
    assert "review_id 1" in msg and "Protocol v1" in msg
    assert "ANGPTL3" in agent.plan_search(1)
    assert "SYNTAX ERROR" in agent.plan_search(1, "TITLE(a b)")
    for i in range(3):
        rid, _ = store_record(S.db(), 1, rec("pubmed", str(i), pmid=str(i), title=f"Base editing of ANGPTL3 study {i}",
                                             abstract="Mice received LNP base editors targeting ANGPTL3. LDL fell by 40%."))
        Deduper(S.db(), 1).attach(rid)
    b = agent.batch(1, "ta", n=2)
    assert "2 of 3 remaining" in b and "RPT-1" in b and "SOURCE TEXT" in b
    out = agent.decide(1, "ta", [{"id": "RPT-1", "d": "include", "c": "I", "q": "Mice received LNP base editors targeting ANGPTL3.",
                                  "loc": "Abstract"},
                                 {"id": 2, "d": "exclude", "c": "S", "q": "this sentence is invented", "loc": "Abstract"},
                                 {"id": 99, "d": "include"}])
    assert "proposed 2/3" in out and "NOT found verbatim" in out and "RPT-2" in out and "99" in out
    assert "1 of 1 remaining" in agent.batch(1, "ta")  # proposed ones are skipped
    st = agent.status(1)
    assert "no agent proposal 1" in st and "NEXT:" in st
    # human confirms -> full-text stage; upload text; read finds passages
    S.reviewer_decision(1, 1, "title_abstract", "include", "alice", criterion_id="I")
    S.upload_fulltext(1, 1, "paper.txt", base64.b64encode(b"Methods: 12 mice per group.\n\nResults: LDL-C fell by 40% at day 7.").decode())
    r = agent.read("RPT-1")
    assert "SECTIONS" in r and len(r) < 2000
    f = agent.read("RPT-1", find="LDL")
    assert "fell by 40%" in f and "12 mice" not in f
    out = agent.decide(1, "ft", [{"id": 1, "d": "exclude", "c": "I"}])
    assert "exclusion_reason" in out
    assert "proposed 1/1" in agent.decide(1, "ft", [{"id": 1, "d": "include", "c": "I", "q": "LDL-C fell by 40% at day 7.",
                                                     "loc": "paper.txt"}])
    assert "proposed rows" in agent.extract(1, "RPT-1", [{"outcome": "LDL-C", "arm": "edited", "value_origin": "reported",
                                                          "source_location": "Results"}])
    assert "prisma_counts" in agent.ops("prisma")
    assert '"records_total":3' in agent.op("prisma_counts", {"review_id": 1})
    assert "unknown operation" in agent.op("set_credential", {})
    assert agent.compact({"a": None, "b": [], "c": 1}) == '{"c":1}'
