"""Local evaluation of an EDQL expression against a normalised record.

Used to (a) diagnose benchmark misses ("would the query match this paper's title/abstract?"),
(b) audit source results for silent widening (records returned that do not match locally), and
(c) test compiler fidelity. Three-valued: True / False / None (cannot tell, e.g. MeSH absent).
"""
from __future__ import annotations

import re
import unicodedata

from .ast import And, Near, Node, Not, Or, Term, Vocab

_WORD = re.compile(r"[a-z0-9]+")


def norm_tokens(text: str | None) -> list[str]:
    if not text:
        return []
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return _WORD.findall(t)


def _field_tokens(rec: dict, field: str) -> list[list[str]] | None:
    def tk(key):
        v = rec.get(key)
        if isinstance(v, list):
            return [norm_tokens(x) for x in v]
        return [norm_tokens(v)]

    if field == "title":
        return tk("title")
    if field == "abstract":
        return tk("abstract") if rec.get("abstract") else None
    if field == "title_abstract":
        return tk("title") + (tk("abstract") if rec.get("abstract") else [])
    if field == "author":
        return tk("authors")
    if field == "journal":
        return tk("journal")
    if field == "keywords":
        return tk("keywords") if rec.get("keywords") else None
    # all: everything we hold locally
    out = []
    for k in ("title", "abstract", "keywords", "mesh", "journal", "authors"):
        if rec.get(k):
            out += tk(k)
    return out


def _positions(seq: list[str], term: Term) -> list[tuple[int, int]]:
    words = norm_tokens(term.text)
    if not words:
        return []
    hits = []
    n = len(words)
    for i in range(len(seq) - n + 1):
        ok = True
        for j, w in enumerate(words):
            tok = seq[i + j]
            last = j == n - 1
            if last and term.wildcard:
                if not tok.startswith(w):
                    ok = False
                    break
            elif tok != w:
                ok = False
                break
        if ok:
            hits.append((i, i + n - 1))
    return hits


def evaluate(node: Node, rec: dict) -> bool | None:
    if isinstance(node, Term):
        segs = _field_tokens(rec, node.field)
        if segs is None:
            return None
        found = any(_positions(s, node) for s in segs)
        return True if found else False
    if isinstance(node, Near):
        segs = _field_tokens(rec, node.field)
        if segs is None:
            return None
        for s in segs:
            a = _positions(s, node.left)
            b = _positions(s, node.right)
            for (a0, a1) in a:
                for (b0, b1) in b:
                    gap = max(b0 - a1, a0 - b1) - 1
                    if gap <= node.distance:
                        return True
        return False
    if isinstance(node, Vocab):
        if node.vocab != "mesh" or not rec.get("mesh"):
            return None
        heads = [h.lower() for h in rec["mesh"]]
        if node.heading.lower() in heads:
            return True
        return None if node.explode else False  # exploded children unknown locally
    if isinstance(node, Not):
        v = evaluate(node.child, rec)
        return None if v is None else not v
    if isinstance(node, And):
        vals = [evaluate(c, rec) for c in node.children]
        if any(v is False for v in vals):
            return False
        return None if any(v is None for v in vals) else True
    if isinstance(node, Or):
        vals = [evaluate(c, rec) for c in node.children]
        if any(v is True for v in vals):
            return True
        return None if any(v is None for v in vals) else False
    raise TypeError(node)


def matching_clauses(node: Node, rec: dict) -> list[str]:
    """Which leaf clauses match (for explaining hits/misses)."""
    from .ast import canonical, walk
    out = []
    for n in walk(node):
        if isinstance(n, (Term, Near, Vocab)) and evaluate(n, rec):
            label = canonical(n)
            if isinstance(n, (Term, Near)):
                label += f" [{n.field}]"
            out.append(label)
    return out
