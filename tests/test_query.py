import pytest

from evidencedesk.query.ast import Near, Term, Vocab, walk
from evidencedesk.query.compilers import COMPILERS, compile_query
from evidencedesk.query.evaluate import evaluate
from evidencedesk.query.parser import QuerySyntaxError, parse, validate

EXAMPLE = '''SOURCES ALL

TITLE_ABSTRACT(
  (ANGPTL3 OR "angiopoietin-like protein 3")
  AND
  (CRISPR OR "genome editing" OR "base editing" OR "prime editing")
)
OR TITLE_ABSTRACT(CTX310 OR "CTX-310")'''


def test_example_parses():
    q = parse(EXAMPLE)
    assert q.sources is None
    leaves = [n for n in walk(q.expr) if isinstance(n, Term)]
    assert {t.text for t in leaves} == {"ANGPTL3", "angiopoietin-like protein 3", "CRISPR", "genome editing",
                                         "base editing", "prime editing", "CTX310", "CTX-310"}
    assert all(t.field == "title_abstract" for t in leaves)


@pytest.mark.parametrize("bad,msg", [
    ("TITLE(a b)", "Missing operator"),
    ("a AND", "ends after an operator"),
    ("(a OR b", "Unclosed parenthesis"),
    ("a OR b)", "Unmatched closing"),
    ("NOT a", "NOT must follow"),
    ("title(x)", "upper case"),
    ("NEAR/3(a)", "two operands"),
    ("SOURCES pubmd\na", "Unknown source"),
    ("TITLE(ABSTRACT(x))", "nested"),
    ("ed*t", "trailing truncation"),
    ('"abc', "Unterminated"),
    ("ab*", "at least 3"),
    ("a?c", "'?'"),
    ("TITLE_ABSTRACT(MESH(\"Liver\"))", "Controlled-vocabulary"),
    ("SOURCES ALL", "no search expression"),
    ("a\nSOURCES ALL", "before the search expression"),
    ("PUBDATE 2025 TO 2015\na", "start is after end"),
])
def test_syntax_errors(bad, msg):
    with pytest.raises(QuerySyntaxError) as e:
        parse(bad)
    assert msg in e.value.message or msg in str(e.value)
    v = validate(bad)
    assert not v["valid"] and v["errors"][0]["line"] >= 1


def test_warnings_and_directives():
    q = parse("SOURCES ALL EXCEPT openalex, scopus\nPUBDATE 2015 TO 2020-06\nliver AND kidney")
    with pytest.raises(QuerySyntaxError, match="not an operator"):
        parse("liver and kidney")
    assert q.sources_except == ["openalex", "scopus"]
    assert q.date.start == "2015-01-01" and q.date.end == "2020-06-30"
    # 'and' lowercase is a word -> missing operator error would fire; check warning path separately
    v = validate("liver AND and")
    assert v["valid"] and any("upper case" in w["message"] for w in v["warnings"])


def test_precedence_not_and_or():
    q = parse("a OR b AND c NOT d")
    from evidencedesk.query.ast import canonical
    assert canonical(q.expr) == "a OR (b AND c NOT d)"


def test_no_clause_silently_dropped():
    """Every leaf term must appear in every source's native query (dropping is only allowed with a note)."""
    q = parse(EXAMPLE)
    for s in COMPILERS:
        c = compile_query(q, s)
        for t in (n for n in walk(q.expr) if isinstance(n, Term)):
            assert t.text.lower() in c.native.lower(), (s, t.text)


def test_fidelity_classification():
    q = parse(EXAMPLE)
    st = {s: compile_query(q, s).status for s in COMPILERS}
    assert st["pubmed"] == "approximate"        # [tiab] includes author keywords
    assert st["europepmc"] in ("exact", "semantic")
    assert st["openalex"] in ("exact", "semantic")
    assert st["ctgov"] == "approximate"         # registry fields stand in for abstract
    assert st["wos"] == "approximate"           # TS widens to keywords
    assert st["scopus"] in ("exact", "semantic")  # TITLE OR ABS is faithful


def test_proximity_and_vocab():
    q = parse('NEAR/3(ANGPTL3, editing) AND MESH("Gene Editing", NOEXP) OR TITLE(NEAR/5("base editing", liver))')
    pm = compile_query(q, "pubmed")
    assert '"ANGPTL3 editing"[all:~3]' in pm.native or "~3" in pm.native
    assert '"Gene Editing"[Mesh:NoExp]' in pm.native
    assert pm.status == "unsupported"  # proximity in [all] not supported by PubMed
    ep = compile_query(q, "europepmc")
    assert '"ANGPTL3 editing"~3' in ep.native and '"editing ANGPTL3"~3' in ep.native
    oa = compile_query(q, "openalex")
    assert oa.status == "unsupported"
    em = compile_query(parse('EMTREE("genome editing") AND TITLE_ABSTRACT(ANGPTL3)'), "embase")
    assert "'genome editing'/exp" in em.native and "'ANGPTL3':ti,ab" in em.native
    assert compile_query(parse('EMTREE("genome editing")'), "pubmed").status == "unsupported"


def test_openalex_union_for_cross_field_or():
    c = compile_query(parse("TITLE(ANGPTL3) OR ABSTRACT(CTX310)"), "openalex")
    assert len(c.requests) == 2
    assert c.requests[0]["filter"][0][0] == "title.search.exact"
    assert c.requests[1]["filter"][0][0] == "abstract.search.exact"
    c2 = compile_query(parse("TITLE(ANGPTL3) AND ABSTRACT(CRISPR)"), "openalex")
    assert len(c2.requests) == 1 and len(c2.requests[0]["filter"]) == 2


def test_wildcards():
    q = parse("TITLE_ABSTRACT(edit* AND ANGPTL3)")
    assert compile_query(q, "ctgov").status == "unsupported"
    assert "edit*[tiab]" in compile_query(q, "pubmed").native
    assert compile_query(q, "openalex").status != "unsupported"
    assert compile_query(q, "sciencedirect").status == "unsupported"


def test_date_granularity():
    q = parse("PUBDATE 2015-03-01 TO 2024\nANGPTL3")
    assert compile_query(q, "scopus").status == "approximate"
    assert '"2015/03/01"[dp] : "2024/12/31"[dp]' in compile_query(q, "pubmed").native
    assert "FIRST_PDATE:[2015-03-01 TO 2024-12-31]" in compile_query(q, "europepmc").native


def test_evaluate():
    rec = {"title": "In vivo base editing of ANGPTL3 in mice", "abstract": "We edited the Angptl3 gene in the liver.",
           "mesh": ["Gene Editing"]}
    assert evaluate(parse('TITLE_ABSTRACT("base editing" AND ANGPTL3)').expr, rec) is True
    assert evaluate(parse("TITLE(edit*)").expr, rec) is True
    assert evaluate(parse("TITLE(liver)").expr, rec) is False
    assert evaluate(parse("NEAR/2(editing, ANGPTL3)").expr, rec) is True
    assert evaluate(parse("NEAR/1(editing, mice)").expr, rec) is False
    assert evaluate(parse("ANGPTL3 NOT mice").expr, rec) is False
    assert evaluate(parse('MESH("Gene Editing")').expr, rec) is True
    assert evaluate(parse('MESH("Liver", NOEXP)').expr, rec) is False
    assert evaluate(parse('ABSTRACT(x)').expr, {"title": "x"}) is None


def test_autocomplete():
    from evidencedesk.query.complete import complete
    r = complete("a AND TIT", 9)
    assert {i["label"] for i in r["items"]} == {"TITLE(", "TITLE_ABSTRACT("}
    r = complete("SOURCES pub", 11)
    assert r["items"][0]["label"] == "pubmed"
    r = complete('MESH("Gene', 10, vocab_lookup=lambda t: ["Gene Editing"])
    assert r["items"][0]["label"] == "Gene Editing"
