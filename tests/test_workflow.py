"""Offline workflow tests: dedup, studies, imports, screening, PRISMA reconciliation, analysis, redaction,
pagination/retries/resume (mocked HTTP)."""
import base64
import json
import shutil

import httpx
import pytest
import respx

from conftest import rec


def _review():
    from evidencedesk import service as S
    rid = S.create_review("T", "mixed")["review_id"]
    p = S.protocol_template("mixed")
    for c in p["criteria"]:
        c["include"] = c["include"] or f"rule {c['id']}"
    p["criteria"][1]["search_terms"] = ["ANGPTL3"]
    S.save_protocol(rid, p)
    return rid


def _store(rid, r):
    from evidencedesk import service as S
    from evidencedesk.dedup import Deduper
    from evidencedesk.engine import store_record
    i, new = store_record(S.db(), rid, r)
    return Deduper(S.db(), rid).attach(i)


def test_dedup_rules(home):
    from evidencedesk import service as S
    rid = _review()
    t = "In vivo base editing of ANGPTL3 lowers lipids in mice"
    a = _store(rid, rec("pubmed", "1", title=t, doi="10.1/a", pmid="1", year=2020, authors=["Smith J"]))
    b = _store(rid, rec("europepmc", "MED:1", title=t + ".", doi="10.1/a", pmid="1", year=2020, authors=["Smith J"]))
    assert b["report_id"] == a["report_id"] and b["method"] == "id"
    # bibliographic auto-merge: same title/year/author, no IDs
    c = _store(rid, rec("openalex", "W1", title=t, year=2020, authors=["John Smith"]))
    assert c["report_id"] == a["report_id"] and c["method"] == "bibliographic"
    # preprint with different DOI -> queued, not merged
    d = _store(rid, rec("europepmc", "PPR:9", title=t, doi="10.21203/rs.3", year=2020, authors=["Smith J"],
                        report_kind="preprint"))
    assert d["method"] == "queued" and d["report_id"] != a["report_id"]
    # supplement DOI shared by unrelated abstracts -> queued
    e = _store(rid, rec("crossref", "x", title="Completely different conference abstract on statins", doi="10.1/a"))
    assert e["method"] == "queued"
    # registry never merges with article even with shared NCT
    f = _store(rid, rec("ctgov", "NCT01234567", title=t, nct_ids=["NCT01234567"], report_kind="registry"))
    g = _store(rid, rec("pubmed", "2", title="Other paper", pmid="2", nct_ids=["NCT01234567"]))
    assert f["report_id"] != g["report_id"]
    cands = S.dedup_candidates(rid)
    assert len(cands) == 2
    # merge + undo restores records
    pre = S.get_report(a["report_id"])["records"]
    ev = S.merge_reports(rid, a["report_id"], d["report_id"])["event_id"]
    assert len(S.get_report(a["report_id"])["records"]) == len(pre) + 1
    S.undo_merge(rid, ev)
    assert len(S.get_report(a["report_id"])["records"]) == len(pre)
    assert S.get_report(d["report_id"])["merged_into"] is None
    # same-study decision links instead of merging
    cid = [c for c in S.dedup_candidates(rid) if d["report_id"] in (c["report_a"], c["report_b"])][0]["id"]
    S.decide_candidate(rid, cid, "same_study")
    st = S.list_studies(rid)
    assert len(st) == 1 and {r["report_id"] for r in st[0]["reports"]} == {a["report_id"], d["report_id"]}
    # study link suggestion via NCT
    sug = S.suggest_study_links(rid)
    assert any("NCT01234567" in s["reason"] for s in sug)


def test_imports(home):
    from evidencedesk import service as S
    rid = _review()
    ris = """TY  - JOUR
TI  - Base editing of ANGPTL3 in primates
AU  - Doe, A
PY  - 2022
JO  - Nature
DO  - https://doi.org/10.1000/XYZ
AB  - We edited ANGPTL3 in cynomolgus monkeys.
ER  -
TY  - ABST
TI  - CTX310 durable reduction
AU  - Roe, B
PY  - 2023
ER  -
"""
    r = S.import_records(rid, file_name="embase.ris", content_b64=base64.b64encode(ris.encode()).decode(),
                         platform="Ovid Embase", native_query="exp genome editing/ and ANGPTL3.ti,ab.",
                         search_date="2026-11-20")
    assert r["records"] == 2 and r["format"] == "ris"
    reps = S.list_reports(rid)
    assert {x["report_kind"] for x in reps} == {"journal_article", "conference_abstract"}
    assert any(x["doi"] == "10.1000/xyz" for x in reps)
    nbib = "PMID- 123\nTI  - A title about ANGPTL3\nAB  - Abstract.\nAU  - Smith J\nDP  - 2021 Jan\nAID - 10.1/b [doi]\nPT  - Journal Article\n"
    r2 = S.import_records(rid, file_name="pubmed.nbib", content_b64=base64.b64encode(nbib.encode()).decode(),
                          platform="pubmed_manual", native_query="ANGPTL3")
    assert r2["format"] == "medline" and r2["records"] == 1
    bib = '@article{k1, title={{Editing ANGPTL3}}, author={Lee, K and Kim, H}, year={2020}, doi={10.1/c}}'
    r3 = S.import_records(rid, file_name="x.bib", content_b64=base64.b64encode(bib.encode()).decode(), platform="wos")
    assert r3["format"] == "bibtex"
    csvt = "Title,Authors,Year,DOI,Source title\nCSV paper on ANGPTL3,A B; C D,2019,10.1/d,J X\n"
    r4 = S.import_records(rid, file_name="scopus.csv", content_b64=base64.b64encode(csvt.encode()).decode(),
                          platform="scopus_web")
    assert r4["format"] == "csv" and r4["records"] == 1
    s = S.prisma_s(rid)
    assert "exp genome editing/ and ANGPTL3.ti,ab." in s and "Ovid Embase".lower().replace(" ", "_") in s


def test_screening_resolution_and_prisma(home):
    from evidencedesk import service as S
    rid = _review()
    ids = []
    for i in range(6):
        ids.append(_store(rid, rec("pubmed", str(i), title=f"Paper number {i} about ANGPTL3 base editing in mice",
                                   pmid=str(i), abstract=f"Mice were treated. Value {i}.", year=2020,
                                   authors=[f"Author{i} A"]))["report_id"])
    # duplicates: one extra record of paper 0
    _store(rid, rec("europepmc", "MED:0", title="Paper number 0 about ANGPTL3 base editing in mice", pmid="0"))
    ta = "title_abstract"
    p = S.propose_screening(rid, ids[0], ta, "include", criterion_id="I", passage="Mice were treated.",
                            location="Abstract")
    assert p["passage_verified"] is True and p["resolution"]["final"] == "pending"
    bad = S.propose_screening(rid, ids[1], ta, "include", criterion_id="I", passage="Rats were fed sugar.")
    assert bad["passage_verified"] is False
    with pytest.raises(ValueError):
        S.propose_screening(rid, ids[1], ta, "exclude")  # agent exclude needs criterion
    for r in ids:
        S.reviewer_decision(rid, r, ta, "include" if r != ids[5] else "exclude", "alice", criterion_id="I")
    assert S.screening_queue(rid, ta, "pending")  # second reviewer still needed
    for r in ids:
        S.reviewer_decision(rid, r, ta, "include" if r not in (ids[4], ids[5]) else "exclude", "bob", criterion_id="I")
    conf = S.conflicts(rid, ta)
    assert [c["report_id"] for c in conf] == [ids[4]]
    S.adjudicate(rid, ids[4], ta, "include", "carol", criterion_id="I")
    with pytest.raises(ValueError):
        S.reviewer_decision(rid, ids[5], "full_text", "include", "alice")  # excluded at TA
    with pytest.raises(ValueError):
        S.reviewer_decision(rid, ids[0], "full_text", "exclude", "alice")  # FT exclude needs reason
    S.mark_fulltext_unavailable(rid, ids[3], "requested from authors, no reply", "alice")
    for who in ("alice", "bob"):
        S.reviewer_decision(rid, ids[0], "full_text", "include", who, criterion_id="I")
        S.reviewer_decision(rid, ids[1], "full_text", "include", who, criterion_id="I")
        S.reviewer_decision(rid, ids[2], "full_text", "exclude", who, criterion_id="O", exclusion_reason="wrong outcomes")
    # ids[4]: no FT decision, not sought -> retrieval pending
    S.create_study(rid, "Same study", [ids[0], ids[1]])
    c = S.prisma_counts(rid)
    i, s, rr, e, inc = c["identification"], c["screening"], c["retrieval"], c["eligibility"], c["included"]
    assert i["records_total"] == 7 and i["duplicates_removed"] == 1 and s["records_screened"] == 6
    assert s["records_excluded"] == 1 and rr["reports_sought"] == 5
    assert rr["reports_not_retrieved"] == 1 and rr["retrieval_pending"] == 1
    assert e["reports_assessed"] == 3 and e["reports_excluded"] == 1 and e["exclusion_reasons"] == {"wrong outcomes": 1}
    assert inc["reports"] == 2 and inc["studies"] == 1
    assert all(ch["ok"] for ch in c["checks"][:-1])
    assert not c["checks"][-1]["ok"]  # pending items reported
    svg = S.prisma_svg(rid)
    assert "Studies included in review (n = 1)" in svg and "reconcile" in svg


def test_packet_and_analysis(home, tmp_path):
    from evidencedesk import service as S
    rid = _review()
    ids = [_store(rid, rec("pubmed", str(i), title=f"Study {i} of ANGPTL3 editing in mice", pmid=str(i),
                           abstract="Mice (n = 8 per group) received LNP. LDL-C fell by 50% at week 4.", year=2021,
                           authors=[f"Au{i} B"]))["report_id"] for i in range(4)]
    for st in ("title_abstract", "full_text"):
        for r in ids:
            for w in ("a", "b"):
                S.reviewer_decision(rid, r, st, "include", w, criterion_id="I")
    # synthetic, clearly labelled test numbers
    data = [(ids[0], 1.2, 0.3, 8, 3.0, 0.5, 8), (ids[1], 1.5, 0.4, 6, 2.8, 0.6, 6), (ids[2], 2.0, 0.5, 10, 3.1, 0.7, 10)]
    rows = []
    for r, m1, s1, n1, m2, s2, n2 in data:
        res = S.propose_extraction(rid, r, [
            {"outcome": "LDL-C", "timepoint": "week 4", "arm": "edited", "arm_role": "intervention", "n": n1, "mean": m1,
             "sd": s1, "unit": "mmol/L", "value_origin": "reported", "source_location": "Table 1"},
            {"outcome": "LDL-C", "timepoint": "week 4", "arm": "control", "arm_role": "control", "n": n2, "mean": m2,
             "sd": s2, "unit": "mmol/L", "value_origin": "reported", "source_location": "Table 1"}], agent="t")
        rows += res["row_ids"]
    # study 4 has two edited arms sharing one control; SE instead of SD -> calculated SD
    res = S.propose_extraction(rid, ids[3], [
        {"outcome": "LDL-C", "timepoint": "week 4", "arm": "low dose", "arm_role": "intervention", "n": 5, "mean": 2.2,
         "se": 0.2, "unit": "mmol/L", "value_origin": "reported", "source_location": "Fig 2A"},
        {"outcome": "LDL-C", "timepoint": "week 4", "arm": "high dose", "arm_role": "intervention", "n": 5, "mean": 1.4,
         "se": 0.2, "unit": "mmol/L", "value_origin": "figure_derived", "source_location": "Fig 2A"},
        {"outcome": "LDL-C", "timepoint": "week 4", "arm": "control", "arm_role": "control", "n": 6, "mean": 3.0,
         "se": 0.25, "unit": "mmol/L", "value_origin": "figure_derived", "source_location": "Fig 2A"}], agent="t")
    rows += res["row_ids"]
    S.save_analysis_plan(rid, {"analyses": [{"id": "A1", "outcome": "LDL-C", "timepoint": "week 4",
                                             "effect_measure": "SMD", "method": "REML", "inference": "knha",
                                             "min_studies": 2, "shared_control": "split"}]})
    chk = S.analysis_checks(rid)
    assert chk["analyses"][0]["data"] == [] and chk["unverified_rows_ignored"] == len(rows)
    for r in rows:
        S.verify_extraction(r, "reviewer")
    chk = S.analysis_checks(rid)["analyses"][0]
    assert len(chk["data"]) == 5
    assert any(f["type"] == "shared_control" and f["severity"] == "handled" for f in chk["flags"])
    split = [d for d in chk["data"] if "low dose" in d["comparison"]][0]
    assert split["n2i"] == 3 and "SE×√n" in split["derivations"]
    # double counting: the same study reported twice (link reports 0 and 1 as one study)
    S.create_study(rid, "dup", [ids[0], ids[1]])
    chk2 = S.analysis_checks(rid)["analyses"][0]
    assert any(f["type"] == "double_counting" and f["severity"] == "block" for f in chk2["flags"])
    S.unlink_report_from_study(rid, ids[1], S.list_studies(rid)[0]["id"])
    if shutil.which("Rscript"):
        out = S.export_analysis(rid, out_dir=str(tmp_path / "an"))
        a = out["analyses"][0]
        assert a["status"] == "ran", a.get("r")
        assert float(a["summary"]["estimate"]) < 0 and a["summary"]["test"] == "knha"
        assert (tmp_path / "an" / "A1_forest.png").exists() and (tmp_path / "an" / "A1_leave1out.csv").exists()
    pk = S.export_evidence_packet(rid, out_dir=str(tmp_path / "pk"), scope="included")
    assert pk["reports"] == 4 and pk["pdf"]["pages"] >= 5
    man = json.loads((tmp_path / "pk" / "manifest.json").read_text())
    assert len(man["reports"]) == 4 and (tmp_path / "pk" / "text" / "RPT-1.txt").exists()
    md = (tmp_path / "pk" / "evidence_packet.md").read_text()
    assert "Table of contents" in md and "figure_derived" in md and "NOT FOUND in available text" in md
    import pymupdf
    doc = pymupdf.open(tmp_path / "pk" / "evidence_packet.pdf")
    assert len(doc.get_toc()) == 5 and "Study 2 of ANGPTL3" in "".join(p.get_text() for p in doc)


def test_double_counting_flag(home):
    from evidencedesk import service as S
    rid = _review()
    a = _store(rid, rec("pubmed", "1", title="Journal article on ANGPTL3 editing results", pmid="1", year=2022,
                        authors=["X A"]))["report_id"]
    b = _store(rid, rec("europepmc", "PPR:1", title="Conference abstract on ANGPTL3 editing", year=2021,
                        authors=["X A"], report_kind="conference_abstract"))["report_id"]
    S.create_study(rid, "S", [a, b])
    for r in (a, b):
        S.enter_extraction(rid, r, [
            {"outcome": "TG", "arm": "edited", "arm_role": "intervention", "n": 5, "mean": 1, "sd": 0.2,
             "value_origin": "reported", "source_location": "Abstract"},
            {"outcome": "TG", "arm": "control", "arm_role": "control", "n": 5, "mean": 2, "sd": 0.2,
             "value_origin": "reported", "source_location": "Abstract"}], "rev")
    for row in S.list_extraction(rid):
        S.verify_extraction(row["id"], "rev")
    S.save_analysis_plan(rid, {"analyses": [{"id": "A1", "outcome": "TG", "effect_measure": "MD"}]})
    flags = S.analysis_checks(rid)["analyses"][0]["flags"]
    assert any(f["type"] == "double_counting" and f["severity"] == "block" for f in flags)


def test_redaction(home):
    from evidencedesk import credentials as C
    C.set("scopus", "api_key", "SECRETKEY123456")
    assert C.get("scopus", "api_key") == "SECRETKEY123456"
    assert "SECRETKEY123456" not in C.redact("GET https://x/?apiKey=SECRETKEY123456&q=1 failed")
    assert "***REDACTED***" in C.redact("header X-ELS-APIKey: abcdef0123456789")
    st = C.status("scopus")
    assert st["api_key"]["configured"] and st["api_key"]["value"] is None
    from evidencedesk import service as S
    listed = json.dumps(S.list_sources())
    assert "SECRETKEY123456" not in listed
    from evidencedesk.mcp_server import MCP_EXCLUDE
    assert "set_credential" in MCP_EXCLUDE


def _epmc_page(ids, nxt, total):
    return {"hitCount": total, "nextCursorMark": nxt, "resultList": {"result": [
        {"id": str(i), "source": "MED", "pmid": str(i), "title": f"Paper {i} on ANGPTL3 base editing in mice",
         "pubYear": "2020", "authorString": f"Auth{i} A."} for i in ids]}}


@respx.mock
def test_pagination_retry_and_resume(home):
    from evidencedesk import engine, service as S
    rid = _review()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        mark = request.url.params.get("cursorMark")
        if mark == "*":
            return httpx.Response(200, json=_epmc_page(range(1, 4), "c2", 7))
        if mark == "c2":
            if calls.get("failed_once") is None:
                calls["failed_once"] = True
                return httpx.Response(503, text="busy")
            return httpx.Response(200, json=_epmc_page(range(4, 7), "c3", 7))
        if mark == "c3":
            if calls.get("hard_fail", 0) < 5:
                calls["hard_fail"] = calls.get("hard_fail", 0) + 1
                return httpx.Response(500, text="down")
            return httpx.Response(200, json=_epmc_page([7], "c3", 7))
        return httpx.Response(400)

    respx.get(url__startswith="https://www.ebi.ac.uk/europepmc/webservices/rest/search").mock(side_effect=handler)
    import evidencedesk.http as H
    H.RetryPolicy.__init__.__defaults__ = (5, 0.01, 0.05)
    run = S.start_search(rid, "SOURCES europepmc\nTITLE(ANGPTL3)", background=False)["run_id"]
    st = S.search_status(run)["sources"][0]
    assert st["status"] == "failed" and st["retrieved"] == 6 and st["retries"] >= 5 and st["resumable"]
    S.resume_search(run, background=False)
    st = S.search_status(run)["sources"][0]
    assert st["status"] == "complete" and st["retrieved"] == 7 and st["reported_hits"] == 7
    assert S.db().one("SELECT COUNT(*) c FROM records")["c"] == 7
    H.RetryPolicy.__init__.__defaults__ = (5, 1.5, 60.0)


@respx.mock
def test_zero_results_vs_error(home):
    from evidencedesk import service as S
    rid = _review()
    respx.get(url__startswith="https://www.ebi.ac.uk/europepmc/webservices/rest/search").mock(
        return_value=httpx.Response(200, json={"hitCount": 0, "resultList": {"result": []}}))
    respx.get(url__startswith="https://clinicaltrials.gov/api/v2/studies").mock(
        return_value=httpx.Response(403, text="forbidden"))
    run = S.start_search(rid, "SOURCES europepmc, ctgov\nTITLE(zzzz)", approve=["ctgov"], background=False)["run_id"]
    d = {s["source"]: s for s in S.search_status(run)["sources"]}
    assert d["europepmc"]["zero_results"] and d["europepmc"]["status"] == "complete"
    assert d["ctgov"]["status"] == "access_denied" and not d["ctgov"]["zero_results"]
    dash = {s["source"]: s for s in S.run_dashboard(run)["sources"]}
    assert dash["europepmc"]["status_label"].startswith("zero results")


def test_approximation_held_for_approval(home):
    from evidencedesk import service as S
    rid = _review()
    run = S.start_search(rid, "SOURCES pubmed, scopus\nTITLE_ABSTRACT(ANGPTL3)", background=False)["run_id"]
    d = {s["source"]: s for s in S.search_status(run)["sources"]}
    assert d["pubmed"]["status"] == "awaiting_approval"
    assert d["scopus"]["status"] == "disconnected"
    assert S.search_status(run)["status"] == "awaiting_approval"


def test_pubmed_slicing(home):
    from evidencedesk.adapters.pubmed import PubMedAdapter
    a = PubMedAdapter()
    counts = {}

    def fake(term, retmax=0, history=True, on_retry=None):
        import re
        m = re.search(r'"(\d{4})/01/01"\[dp\] : "(\d{4})/12/31"', term)
        n = 15000 if not m else max(0, (int(m.group(2)) - int(m.group(1)) + 1) * 400)
        counts[term] = n
        return {"count": n, "webenv": "W", "query_key": "1", "ids": [], "querytranslation": "", "warnings": {},
                "errors": {}}
    a.esearch = fake
    slices = a._slice("x", 1990, 2026, None)
    assert all(s["count"] <= 9999 for s in slices) and sum(s["count"] for s in slices) == 37 * 400


def test_pdf_ocr_and_failure_visible(home):
    """Scanned (image-only) PDF goes through OCR; a corrupt upload yields a visible extraction failure."""
    import pymupdf
    from evidencedesk import fulltext, service as S
    rid = _review()
    rep = _store(rid, rec("pubmed", "9", title="Scanned report on ANGPTL3", pmid="9"))["report_id"]
    src = pymupdf.open()
    pg = src.new_page()
    pg.insert_text((72, 100), "ANGPTL3 base editing reduced triglycerides by 60 percent in mice", fontsize=16)
    pix = pg.get_pixmap(dpi=200)
    scan = pymupdf.open()
    sp = scan.new_page(width=pg.rect.width, height=pg.rect.height)
    sp.insert_image(sp.rect, stream=pix.tobytes("png"))
    data = scan.tobytes()
    res = S.upload_fulltext(rid, rep, "scan.pdf", base64.b64encode(data).decode())
    ex = res["extraction"]
    if fulltext.ocr_available():
        assert ex["status"] == "ok" and ex["ocr_pages"] == 1 and ex["method"] == "pdf_text+ocr"
        assert "triglycerides" in S.report_text(rep)["text"].lower()
    else:
        assert ex["status"] == "failed" and "tesseract" in ex["error"]
    bad = S.upload_fulltext(rid, rep, "broken.pdf", base64.b64encode(b"%PDF-1.4 garbage").decode())
    assert bad["extraction"]["status"] == "failed" and bad["extraction"]["error"]
    assert S.fulltext_summary(rid)["extraction_problems"]


def test_log_redaction_keeps_args(home, caplog):
    import logging
    from evidencedesk import credentials as C
    C.set("openalex", "api_key", "OAKEY987654321")
    f = C.RedactingFilter()
    r = logging.LogRecord("httpx", 20, "", 0, 'HTTP Request: %s %s "%s"', ("GET", "https://api.openalex.org/works?api_key=OAKEY987654321", "200"), None)
    f.filter(r)
    assert len(r.args) == 3 and "OAKEY987654321" not in r.getMessage()


def test_dedup_prefers_conflict_free_id_match(home):
    from evidencedesk import service as S
    rid = _review()
    letter = _store(rid, rec("pubmed", "100", title="Phase 1 Trial of Gene Editing Targeting ANGPTL3.", doi="10.1/l",
                             pmid="100", report_kind="letter_editorial"))["report_id"]
    reply = _store(rid, rec("pubmed", "101", title="Phase 1 Trial of Gene Editing Targeting ANGPTL3. Reply.", doi="10.1/l",
                            pmid="101", report_kind="letter_editorial"))["report_id"]
    again = _store(rid, rec("europepmc", "MED:101", title="Phase 1 Trial of Gene Editing Targeting ANGPTL3. Reply", doi="10.1/l",
                            pmid="101", report_kind="letter_editorial"))
    assert reply != letter
    assert again["report_id"] == reply and again["method"] == "id"
