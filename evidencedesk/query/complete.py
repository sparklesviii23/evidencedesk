"""Context-aware autocomplete for the EDQL editor (shared by UI and agent)."""
from __future__ import annotations

import re

from .ast import FIELDS, VOCABS
from .parser import KNOWN_SOURCES

KEYWORDS = [
    {"label": "AND", "kind": "operator", "detail": "both clauses"},
    {"label": "OR", "kind": "operator", "detail": "either clause"},
    {"label": "NOT", "kind": "operator", "detail": "A NOT B"},
] + [{"label": f + "(", "kind": "field", "detail": f"restrict to {f.lower().replace('_', '/')}"} for f in FIELDS] + [
    {"label": "NEAR/5(", "kind": "proximity", "detail": "NEAR/n(a, b): within n words, any order"},
    {"label": 'MESH("', "kind": "vocabulary", "detail": "MeSH heading (exploded; add , NOEXP / , MAJOR)"},
    {"label": 'EMTREE("', "kind": "vocabulary", "detail": "Emtree heading (Embase)"},
    {"label": "SOURCES ALL", "kind": "directive", "detail": "run on every enabled search source"},
    {"label": "PUBDATE 2015 TO *", "kind": "directive", "detail": "publication date range"},
]


def complete(text: str, pos: int, vocab_lookup=None, protocol_terms: list[str] | None = None) -> dict:
    before = text[:pos]
    line = before.split("\n")[-1]
    m = re.search(r"([A-Za-z_/\-\d\"]*)$", before)
    prefix = m.group(1) if m else ""
    start = pos - len(prefix)
    items = []
    if re.match(r"^\s*SOURCES\s+", line):
        word = re.split(r"[\s,]+", line)[-1]
        items = [{"label": s, "kind": "source", "detail": "search source"} for s in ["ALL", "EXCEPT"] + KNOWN_SOURCES
                 if s.lower().startswith(word.lower())]
        return {"from": pos - len(word), "to": pos, "items": items}
    vm = re.search(r'(MESH|EMTREE)\(\s*"([^"]*)$', before)
    if vm:
        term = vm.group(2)
        if vocab_lookup and len(term) >= 3 and vm.group(1) == "MESH":
            try:
                items = [{"label": h, "kind": "mesh", "detail": "MeSH descriptor"} for h in vocab_lookup(term)]
            except Exception as e:  # network failure must not break the editor
                items = [{"label": term, "kind": "info", "detail": f"MeSH lookup unavailable: {e}"}]
        return {"from": pos - len(term), "to": pos, "items": items}
    if prefix.startswith('"'):
        return {"from": start, "to": pos, "items": []}
    up = prefix.upper()
    if prefix:
        items = [k for k in KEYWORDS if k["label"].startswith(up) and k["label"] != up]
        for t in protocol_terms or []:
            if t.lower().startswith(prefix.lower()) and t.lower() != prefix.lower():
                lab = f'"{t}"' if " " in t else t
                items.append({"label": lab, "kind": "protocol term", "detail": "from PICOS search terms"})
    else:
        items = [k for k in KEYWORDS if k["kind"] in ("operator", "field")]
    return {"from": start, "to": pos, "items": items[:30]}


def mesh_lookup(term: str) -> list[str]:
    from ..http import request
    r = request("GET", "https://id.nlm.nih.gov/mesh/lookup/descriptor", source="mesh", per_sec=5,
                params={"label": term, "match": "contains", "limit": 12}, expect_json=True)
    return [x["label"] for x in r.json()]
