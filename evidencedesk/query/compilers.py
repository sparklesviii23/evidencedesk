"""Compile an EDQL query into each platform's native syntax.

Fidelity policy (see docs/QUERY_LANGUAGE.md#translation-fidelity):
  info          – cosmetic / tokenisation differences only.
  semantic      – documented platform matching behaviour that cannot be switched off
                  (stemming, plural lemmatisation, what "all fields" covers). Shown, does not block.
  approximation – the native query changes field scope or structure (e.g. abstract -> title/abstract,
                  registry fields standing in for an abstract, year-granular dates). Execution is held
                  until the approximation is approved for that source.
  unsupported   – no native construct exists. Automated execution is blocked; a best-effort native
                  query is produced for manual execution and import.
No clause is ever dropped from the native query silently: every substitution produces a note.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ast import FIELD_LABELS, And, DateRange, Near, Node, Not, Or, Query, Term, Vocab, canonical

SEVERITY_ORDER = {"info": 0, "semantic": 1, "approximation": 2, "unsupported": 3}


@dataclass
class Note:
    severity: str
    clause: str
    message: str
    span: dict | None = None

    def to_dict(self) -> dict:
        return {"severity": self.severity, "clause": self.clause, "message": self.message, "span": self.span}


@dataclass
class Compiled:
    source: str
    platform: str
    native: str  # human-readable exact native query (what to paste into the platform)
    requests: list[dict] = field(default_factory=list)  # executable request parameters
    notes: list[Note] = field(default_factory=list)
    fields: dict = field(default_factory=dict)  # EDQL field -> native field used
    status: str = "exact"  # exact | semantic | approximate | unsupported
    manual_only: bool = False

    def finalize(self) -> "Compiled":
        worst = max((SEVERITY_ORDER[n.severity] for n in self.notes), default=0)
        self.status = {0: "exact", 1: "semantic", 2: "approximate", 3: "unsupported"}[worst]
        # de-duplicate identical notes
        seen, notes = set(), []
        for n in self.notes:
            k = (n.severity, n.clause, n.message)
            if k not in seen:
                seen.add(k)
                notes.append(n)
        self.notes = notes
        return self

    @property
    def needs_approval(self) -> bool:
        return self.status == "approximate"

    def to_dict(self) -> dict:
        return {"source": self.source, "platform": self.platform, "native": self.native,
                "requests": self.requests, "notes": [n.to_dict() for n in self.notes], "fields": self.fields,
                "status": self.status, "needs_approval": self.needs_approval, "manual_only": self.manual_only}


class Compiler:
    source = ""
    platform = ""
    field_map: dict[str, str | None] = {}
    field_notes: dict[str, tuple[str, str]] = {}  # field -> (severity, message)

    def __init__(self, q: Query):
        self.q = q
        self.notes: list[Note] = []
        self.used_fields: dict[str, str] = {}

    def note(self, severity: str, node: Node | None, message: str) -> None:
        self.notes.append(Note(severity, canonical(node) if node is not None else "", message,
                               node.span.to_dict() if node is not None else None))

    # --- dispatch
    def render(self, node: Node) -> str:
        if isinstance(node, Term):
            self._field_used(node, node.field)
            return self.term(node)
        if isinstance(node, Near):
            self._field_used(node, node.field)
            return self.near(node)
        if isinstance(node, Vocab):
            return self.vocab(node)
        if isinstance(node, Or):
            return "(" + " OR ".join(self.render(c) for c in node.children) + ")"
        if isinstance(node, And):
            out = []
            for i, c in enumerate(node.children):
                if isinstance(c, Not):
                    out.append(f"{self.not_op} {self.render(c.child)}")
                else:
                    out.append(("AND " if i else "") + self.render(c))
            return "(" + " ".join(out) + ")"
        raise TypeError(node)

    not_op = "NOT"

    def _field_used(self, node: Node, f: str) -> None:
        if f in self.field_notes and f not in self.used_fields:
            sev, msg = self.field_notes[f]
            self.note(sev, None, msg)
        nf = self.field_map.get(f)
        self.used_fields[FIELD_LABELS[f]] = nf if nf else "UNSUPPORTED"
        if nf is None:
            self.note("unsupported", node, f"{self.platform} has no {FIELD_LABELS[f]} field.")

    def strip_outer(self, s: str) -> str:
        if s.startswith("(") and s.endswith(")"):
            depth = 0
            for i, ch in enumerate(s):
                depth += ch == "("
                depth -= ch == ")"
                if depth == 0 and i < len(s) - 1:
                    return s
            return s[1:-1]
        return s

    def term(self, t: Term) -> str: raise NotImplementedError
    def near(self, n: Near) -> str: raise NotImplementedError
    def vocab(self, v: Vocab) -> str: raise NotImplementedError

    def compile(self) -> Compiled: raise NotImplementedError


def _needs_quotes(s: str) -> bool:
    return bool(re.search(r"[^A-Za-z0-9]", s))


# ---------------------------------------------------------------- PubMed
class PubMedCompiler(Compiler):
    source, platform = "pubmed", "PubMed (NCBI E-utilities)"
    field_map = {"title": "[ti]", "abstract": "[tiab]", "title_abstract": "[tiab]", "all": "[all]",
                 "author": "[au]", "journal": "[ta]", "keywords": "[ot]"}
    field_notes = {
        "title_abstract": ("approximation",
                           "PubMed [tiab] indexes title, collection title, abstract, other abstract AND author "
                           "keywords, so TITLE_ABSTRACT is widened to include author keywords. No narrower PubMed "
                           "field exists."),
        "abstract": ("approximation", "PubMed has no abstract-only field; ABSTRACT is widened to [tiab] "
                                      "(title + abstract + author keywords)."),
        "all": ("semantic", "PubMed [all] covers every searchable PubMed field (incl. MeSH, affiliations)."),
        "keywords": ("semantic", "KEYWORDS maps to PubMed Other Term [ot] (author/other keywords)."),
        "journal": ("semantic", "JOURNAL maps to [ta] (journal title abbreviation or full title)."),
    }

    def term(self, t: Term) -> str:
        tag = self.field_map[t.field]
        txt = t.text.replace('"', "")
        if t.wildcard:
            if t.phrase:
                self.note("semantic", t, "PubMed phrase truncation matches at most the first 600 variants.")
                return f'"{txt}*"{tag}'
            self.note("semantic", t, "PubMed truncation matches at most the first 600 variants.")
            return f"{txt}*{tag}"
        return f'"{txt}"{tag}'

    def near(self, n: Near) -> str:
        tag = self.field_map[n.field]
        if n.field not in ("title", "title_abstract", "abstract"):
            self.note("unsupported", n, "PubMed proximity works only in [ti], [tiab] and [ad].")
        if n.left.wildcard or n.right.wildcard:
            self.note("unsupported", n, "PubMed proximity search does not allow truncation.")
        words = f"{n.left.text} {n.right.text}".replace('"', "")
        if n.left.phrase or n.right.phrase:
            self.note("approximation", n, "PubMed proximity takes a bag of words: word order inside the "
                                          "phrase operands is not enforced and each word may float.")
        return f'"{words}"{tag[:-1]}:~{n.distance}]'

    def vocab(self, v: Vocab) -> str:
        if v.vocab != "mesh":
            self.note("unsupported", v, "PubMed does not index Emtree. Map to a MeSH heading instead.")
            return f'"{v.heading}"[Mesh]'
        tag = "Majr" if v.major else "Mesh"
        if not v.explode:
            tag += ":NoExp"
        self.used_fields["MESH"] = f"[{tag}]"
        return f'"{v.heading}"[{tag}]'

    def compile(self) -> Compiled:
        expr = self.strip_outer(self.render(self.q.expr))
        if self.q.date:
            d = self.q.date
            a = (d.start or "1800-01-01").replace("-", "/")
            b = (d.end or "3000/12/31").replace("-", "/")
            expr = f'({expr}) AND ("{a}"[dp] : "{b}"[dp])'
            self.note("semantic", None, "Publication date [dp] matches either the print or the electronic "
                                        "publication date.")
        self.note("info", None, "Field tags disable PubMed Automatic Term Mapping, so terms are searched "
                                "exactly as written.")
        c = Compiled(self.source, self.platform, expr, [{"term": expr}], self.notes, self.used_fields)
        return c.finalize()


# ---------------------------------------------------------------- Europe PMC
class EuropePMCCompiler(Compiler):
    source, platform = "europepmc", "Europe PMC REST"
    field_map = {"title": "TITLE", "abstract": "ABSTRACT", "title_abstract": "TITLE|ABSTRACT", "all": "(default)",
                 "author": "AUTH", "journal": "JOURNAL", "keywords": "KW"}
    field_notes = {
        "all": ("semantic", "Europe PMC default search covers all metadata and, for full-text articles, the "
                            "full text."),
    }

    def _q(self, t: Term) -> str:
        txt = t.text.replace('"', "")
        if t.wildcard:
            if t.phrase:
                self.note("unsupported", t, "Europe PMC (Lucene) cannot truncate inside a quoted phrase.")
                return f'"{txt}"'
            return f"{txt}*"
        return f'"{txt}"' if (t.phrase or _needs_quotes(txt)) else txt

    def term(self, t: Term) -> str:
        v = self._q(t)
        if t.field == "all":
            return v
        if t.field == "title_abstract":
            return f"(TITLE:{v} OR ABSTRACT:{v})"
        return f"{self.field_map[t.field]}:{v}"

    def near(self, n: Near) -> str:
        if n.left.phrase or n.right.phrase:
            self.note("approximation", n, "Lucene proximity takes single words; phrase operands are "
                                          "flattened into a bag of words.")
        if n.left.wildcard or n.right.wildcard:
            self.note("unsupported", n, "Europe PMC proximity does not allow truncation.")
        a, b = n.left.text, n.right.text
        fields = ["TITLE", "ABSTRACT"] if n.field == "title_abstract" else [self.field_map[n.field]]
        parts = []
        for f in fields:
            pre = "" if f == "(default)" else f + ":"
            # Lucene slop is order-sensitive: query both orders for unordered NEAR/n
            parts += [f'{pre}"{a} {b}"~{n.distance}', f'{pre}"{b} {a}"~{n.distance}']
        return "(" + " OR ".join(parts) + ")"

    def vocab(self, v: Vocab) -> str:
        if v.vocab != "mesh":
            self.note("unsupported", v, "Europe PMC does not index Emtree.")
        else:
            self.note("approximation", v, "Europe PMC MESH: matches MeSH headings of MEDLINE-indexed records only; "
                                          "explosion/major-topic behaviour is not equivalent to PubMed.")
        self.used_fields["MESH"] = "MESH"
        return f'MESH:"{v.heading}"'

    def compile(self) -> Compiled:
        expr = self.strip_outer(self.render(self.q.expr))
        if self.q.date:
            d = self.q.date
            expr = f"({expr}) AND FIRST_PDATE:[{d.start or '1800-01-01'} TO {d.end or '3000-12-31'}]"
            self.note("semantic", None, "Date filter uses FIRST_PDATE (earliest publication date).")
        self.note("semantic", None, "Europe PMC includes MEDLINE, PMC, preprints (PPR), Agricola, patents and "
                                    "other sources; source type is recorded per record.")
        c = Compiled(self.source, self.platform, expr, [{"query": expr}], self.notes, self.used_fields)
        return c.finalize()


# ---------------------------------------------------------------- OpenAlex
class OpenAlexCompiler(Compiler):
    source, platform = "openalex", "OpenAlex Works API"
    field_map = {"title": "title.search", "abstract": "abstract.search",
                 "title_abstract": "title_and_abstract.search", "all": "default.search",
                 "author": None, "journal": None, "keywords": None}
    field_notes = {
        "all": ("semantic", "OpenAlex default.search covers title, abstract and indexed full text."),
    }
    NO_STEM = True

    def term(self, t: Term) -> str:
        txt = t.text.replace('"', "")
        if t.wildcard:
            if t.phrase:
                self.note("unsupported", t, "OpenAlex cannot truncate inside a quoted phrase.")
                return f'"{txt}"'
            return f"{txt}*"
        return f'"{txt}"' if (t.phrase or _needs_quotes(txt)) else txt

    def near(self, n: Near) -> str:
        self.note("unsupported", n, "OpenAlex search has no proximity operator.")
        return f"({self.term(n.left)} AND {self.term(n.right)})"

    def vocab(self, v: Vocab) -> str:
        self.note("unsupported", v, "OpenAlex does not support MeSH/Emtree searching.")
        return f'"{v.heading}"'

    def _single_field(self, node: Node) -> str | None:
        from .ast import walk
        fs = {n.field for n in walk(node) if isinstance(n, (Term, Near))}
        if any(isinstance(n, Vocab) for n in walk(node)):
            return None
        return fs.pop() if len(fs) == 1 else None

    def _render_group(self, node: Node) -> str:
        if isinstance(node, Not):
            return "NOT " + self._render_group(node.child)
        return self.strip_outer(self.render(node))

    def _filters_for(self, node: Node) -> list[tuple[str, str]] | None:
        """Express node as AND of single-field search filters, or None."""
        f = self._single_field(node)
        if f is not None:
            return [(f, self._render_group(node))]
        if isinstance(node, And):
            groups: dict[str, list[Node]] = {}
            for c in node.children:
                cf = self._single_field(c.child if isinstance(c, Not) else c)
                if cf is None:
                    return None
                groups.setdefault(cf, []).append(c)
            out = []
            for gf, kids in groups.items():
                if all(isinstance(k, Not) for k in kids):
                    return None  # negation-only filter not expressible
                body = " AND ".join(("NOT " + self._paren(k.child)) if isinstance(k, Not) else self._paren(k)
                                    for k in kids).replace(" AND NOT ", " NOT ")
                out.append((gf, body))
            return out
        return None

    def _paren(self, n: Node) -> str:
        s = self.render(n)
        return s

    def compile(self) -> Compiled:
        expr = self.q.expr
        branches = expr.children if isinstance(expr, Or) and self._single_field(expr) is None else [expr]
        requests = []
        native_parts = []
        for b in branches:
            flt = self._filters_for(b)
            if flt is None:
                self.note("unsupported", b, "OpenAlex can only AND separate field filters; this clause mixes "
                                            "fields inside an OR/NOT that cannot be expressed.")
                flt = [("title_abstract", self.strip_outer(self.render(b)))]
            fl = []
            for f, v in flt:
                key = self.field_map.get(f) or "default.search"
                if self.NO_STEM:
                    key += ".exact"
                fl.append((key, v))
            if self.q.date:
                if self.q.date.start:
                    fl.append(("from_publication_date", self.q.date.start))
                if self.q.date.end:
                    fl.append(("to_publication_date", self.q.date.end))
            requests.append({"filter": fl})
            native_parts.append(",".join(f"{k}:{v}" for k, v in fl))
        if len(requests) > 1:
            self.note("info", None, f"Top-level OR across different fields is executed as {len(requests)} OpenAlex "
                                    "requests whose results are unioned by OpenAlex ID (exact set union).")
        if self.NO_STEM:
            self.note("info", None, "Using OpenAlex .search.exact filters so words are not stemmed "
                                    "(verified live: the stemmed .search variants return a superset).")
        if self.q.date:
            self.note("semantic", None, "Dates use OpenAlex publication_date (earliest known publication).")
        native = "\n  UNION\n".join(f"filter={p}" for p in native_parts)
        c = Compiled(self.source, self.platform, native, requests, self.notes, self.used_fields)
        return c.finalize()


# ---------------------------------------------------------------- ClinicalTrials.gov
class CTGovCompiler(Compiler):
    source, platform = "ctgov", "ClinicalTrials.gov API v2 (Essie)"
    field_map = {"title": "BriefTitle|OfficialTitle|Acronym", "abstract": "BriefSummary|DetailedDescription",
                 "title_abstract": "BriefTitle|OfficialTitle|Acronym|BriefSummary|DetailedDescription",
                 "all": "(default Essie search)", "author": None, "journal": None, "keywords": "Keyword"}
    field_notes = {
        "abstract": ("approximation", "Registry records have no abstract; ABSTRACT is mapped to BriefSummary and "
                                      "DetailedDescription."),
        "title_abstract": ("approximation", "Registry records have no abstract; TITLE_ABSTRACT is mapped to "
                                            "BriefTitle, OfficialTitle, Acronym, BriefSummary and "
                                            "DetailedDescription."),
        "all": ("semantic", "Default Essie search covers all registry fields and applies synonym expansion."),
    }
    AREAS = {"title": ["BriefTitle", "OfficialTitle", "Acronym"],
             "abstract": ["BriefSummary", "DetailedDescription"],
             "title_abstract": ["BriefTitle", "OfficialTitle", "Acronym", "BriefSummary", "DetailedDescription"],
             "keywords": ["Keyword"]}

    def _q(self, t: Term) -> str:
        txt = t.text.replace('"', "")
        if t.wildcard:
            self.note("unsupported", t, "ClinicalTrials.gov Essie ignores '*' (verified live: edit* == edit).")
        return f'"{txt}"' if (t.phrase or _needs_quotes(txt)) else txt

    def term(self, t: Term) -> str:
        v = self._q(t)
        if t.field == "all":
            return v
        areas = self.AREAS.get(t.field)
        if not areas:
            return v
        return self._areas(t.field, v)

    def _areas(self, f: str, body: str) -> str:
        # EXPANSION[Term]: no synonym/concept expansion and no phrase relaxation (Essie default adds synonyms,
        # e.g. "heart attack" 66 -> 1365 trials in BriefTitle). EXPANSION[None] is case-sensitive, so not used.
        self._expansion_note()
        parts = [f"AREA[{a}]EXPANSION[Term]{body}" for a in self.AREAS[f]]
        return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"

    def _expansion_note(self):
        self.note("semantic", None, "Essie EXPANSION[Term] is used: synonym expansion is off, but plurals and "
                                    "lexical variants still match.")

    def render(self, node):
        # Or of plain terms in one registry field -> AREA[x]EXPANSION[Term](a OR b) (verified equivalent live)
        if isinstance(node, Or) and all(isinstance(c, Term) for c in node.children):
            fs = {c.field for c in node.children}
            if len(fs) == 1 and (f := fs.pop()) in self.AREAS:
                for c in node.children:
                    self._field_used(c, f)
                return self._areas(f, "(" + " OR ".join(self._q(c) for c in node.children) + ")")
        return super().render(node)

    def near(self, n: Near) -> str:
        self.note("unsupported", n, "ClinicalTrials.gov has no proximity operator.")
        return f"({self.term(n.left)} AND {self.term(n.right)})"

    def vocab(self, v: Vocab) -> str:
        if v.vocab != "mesh":
            self.note("unsupported", v, "ClinicalTrials.gov does not use Emtree.")
        self.note("approximation", v, "MeSH mapped to registry ConditionMeshTerm/InterventionMeshTerm (assigned "
                                      "automatically by NLM; explosion via ancestors fields).")
        fld = ["ConditionMeshTerm", "InterventionMeshTerm"] if v.explode is False else \
            ["ConditionMeshTerm", "InterventionMeshTerm", "ConditionAncestorTerm", "InterventionAncestorTerm"]
        return "(" + " OR ".join(f'AREA[{f}]"{v.heading}"' for f in fld) + ")"

    def compile(self) -> Compiled:
        expr = self.strip_outer(self.render(self.q.expr))
        req = {"query.term": expr}
        if self.q.date:
            d = self.q.date
            req["filter.advanced"] = f"AREA[StudyFirstPostDate]RANGE[{d.start or 'MIN'},{d.end or 'MAX'}]"
            self.note("approximation", None, "Registry records have no publication date; PUBDATE is applied to "
                                             "StudyFirstPostDate (first posted).")
        native = f"query.term={expr}" + (f"\nfilter.advanced={req['filter.advanced']}" if self.q.date else "")
        c = Compiled(self.source, self.platform, native, [req], self.notes, self.used_fields)
        return c.finalize()


# ---------------------------------------------------------------- Scopus
class ScopusCompiler(Compiler):
    source, platform = "scopus", "Scopus Search API (Elsevier)"
    field_map = {"title": "TITLE", "abstract": "ABS", "title_abstract": "TITLE|ABS", "all": "ALL",
                 "author": "AUTH", "journal": "SRCTITLE", "keywords": "AUTHKEY"}
    field_notes = {"all": ("semantic", "Scopus ALL() covers all fields incl. references and affiliations.")}
    not_op = "AND NOT"

    def _q(self, t: Term) -> str:
        txt = t.text.replace('"', "")
        if t.phrase:
            if t.wildcard:
                return f'"{txt}*"'
            return "{" + txt + "}"
        if t.wildcard:
            return f"{txt}*"
        self.note("semantic", None, "Scopus single-word searches automatically include plural and "
                                    "British/American spelling variants.")
        return f'"{txt}"' if _needs_quotes(txt) else txt

    def term(self, t: Term) -> str:
        v = self._q(t)
        if t.phrase and t.wildcard:
            self.note("semantic", t, "Truncated phrase uses Scopus loose-phrase matching (\"...\").")
        if t.field == "title_abstract":
            return f"(TITLE({v}) OR ABS({v}))"
        return f"{self.field_map[t.field]}({v})"

    def near(self, n: Near) -> str:
        a, b = self._q(n.left), self._q(n.right)
        a = a.replace("{", '"').replace("}", '"')
        b = b.replace("{", '"').replace("}", '"')
        inner = f"{a} W/{n.distance} {b}"
        if n.field == "title_abstract":
            return f"(TITLE({inner}) OR ABS({inner}))"
        return f"{self.field_map[n.field]}({inner})"

    def vocab(self, v: Vocab) -> str:
        self.note("approximation", v, "Scopus has no MeSH/Emtree explosion; mapped to INDEXTERMS() "
                                      "(any controlled index term, not exploded).")
        self.used_fields["MESH" if v.vocab == "mesh" else "EMTREE"] = "INDEXTERMS"
        return f'INDEXTERMS("{v.heading}")'

    def compile(self) -> Compiled:
        expr = self.strip_outer(self.render(self.q.expr))
        if self.q.date:
            d = self.q.date
            parts = []
            if d.start:
                parts.append(f"PUBYEAR > {int(d.start[:4]) - 1}")
                if d.start_precision != "year" and not d.start.endswith("-01-01"):
                    self.note("approximation", None, "Scopus PUBYEAR is year-granular; start date widened to "
                                                     "the whole year.")
            if d.end:
                parts.append(f"PUBYEAR < {int(d.end[:4]) + 1}")
                if d.end_precision != "year" and not d.end.endswith("-12-31"):
                    self.note("approximation", None, "Scopus PUBYEAR is year-granular; end date widened to the "
                                                     "whole year.")
            expr = f"({expr}) AND " + " AND ".join(parts)
        c = Compiled(self.source, self.platform, expr, [{"query": expr}], self.notes, self.used_fields)
        return c.finalize()


# ---------------------------------------------------------------- Embase (Embase.com syntax)
class EmbaseCompiler(Compiler):
    source, platform = "embase", "Embase.com syntax (Elsevier)"
    field_map = {"title": ":ti", "abstract": ":ab", "title_abstract": ":ti,ab", "all": "(all)", "author": ":au",
                 "journal": ":jt", "keywords": ":kw"}
    field_notes = {"all": ("semantic", "Embase default search maps free text to Emtree and searches all fields.")}

    def _q(self, t: Term) -> str:
        txt = t.text.replace("'", "")
        return f"'{txt}{'*' if t.wildcard else ''}'"

    def term(self, t: Term) -> str:
        v = self._q(t)
        if t.field == "all":
            return v
        return f"{v}{self.field_map[t.field]}"

    def near(self, n: Near) -> str:
        inner = f"({self._q(n.left)} NEAR/{n.distance} {self._q(n.right)})"
        return inner if n.field == "all" else f"{inner}{self.field_map[n.field]}"

    def vocab(self, v: Vocab) -> str:
        if v.vocab == "mesh":
            self.note("approximation", v, "Embase maps MeSH headings to Emtree; the Emtree tree may differ. "
                                          "Prefer EMTREE(...) for Embase.")
        suffix = "/exp" if v.explode else "/de"
        if v.major:
            suffix = "/exp/mj" if v.explode else "/mj"
        self.used_fields["EMTREE"] = suffix
        return f"'{v.heading.lower()}'{suffix}"

    def compile(self) -> Compiled:
        expr = self.strip_outer(self.render(self.q.expr))
        if self.q.date:
            d = self.q.date
            a = d.start[:4] if d.start else "1947"
            b = d.end[:4] if d.end else "3000"
            expr = f"({expr}) AND [{a}-{b}]/py"
            if (d.start and not d.start.endswith("-01-01")) or (d.end and not d.end.endswith("-12-31")):
                self.note("approximation", None, "Embase [yyyy-yyyy]/py is year-granular; dates widened to years.")
        c = Compiled(self.source, self.platform, expr, [{"query": expr}], self.notes, self.used_fields)
        return c.finalize()


# ---------------------------------------------------------------- Web of Science
class WoSCompiler(Compiler):
    source, platform = "wos", "Web of Science Starter API (Clarivate)"
    field_map = {"title": "TI", "abstract": "TS", "title_abstract": "TS", "all": "TS", "author": "AU",
                 "journal": "SO", "keywords": "TS"}
    field_notes = {
        "title_abstract": ("approximation", "WoS Topic (TS) = title + abstract + author keywords + Keywords Plus; "
                                            "TITLE_ABSTRACT is widened."),
        "abstract": ("approximation", "WoS Starter has no abstract-only field; ABSTRACT widened to Topic (TS)."),
        "keywords": ("approximation", "KEYWORDS widened to Topic (TS)."),
        "all": ("semantic", "ALL_FIELDS mapped to Topic (TS)."),
    }

    def _q(self, t: Term) -> str:
        txt = t.text.replace('"', "")
        return (f'"{txt}"' if t.phrase or _needs_quotes(txt) else txt) + ("*" if t.wildcard else "")

    def term(self, t: Term) -> str:
        return f"{self.field_map[t.field]}=({self._q(t)})"

    def near(self, n: Near) -> str:
        return f"{self.field_map[n.field]}=({self._q(n.left)} NEAR/{n.distance} {self._q(n.right)})"

    def vocab(self, v: Vocab) -> str:
        self.note("unsupported", v, "Web of Science Core Collection has no MeSH/Emtree index.")
        return f'TS=("{v.heading}")'

    def compile(self) -> Compiled:
        expr = self.strip_outer(self.render(self.q.expr))
        if self.q.date:
            d = self.q.date
            expr = f"({expr}) AND PY=({(d.start or '1900')[:4]}-{(d.end or '3000')[:4]})"
            if (d.start and not d.start.endswith("-01-01")) or (d.end and not d.end.endswith("-12-31")):
                self.note("approximation", None, "WoS PY is year-granular; dates widened to whole years.")
        c = Compiled(self.source, self.platform, expr, [{"q": expr, "db": "WOS"}], self.notes, self.used_fields)
        return c.finalize()


# ---------------------------------------------------------------- ScienceDirect
class ScienceDirectCompiler(Compiler):
    source, platform = "sciencedirect", "ScienceDirect Search API v2 (Elsevier)"
    field_map = {"title": "title", "abstract": "tak", "title_abstract": "tak", "all": "qs", "author": "authors",
                 "journal": "pub", "keywords": "tak"}
    field_notes = {
        "title_abstract": ("approximation", "ScienceDirect 'tak' = title + abstract + keywords; TITLE_ABSTRACT "
                                            "is widened to keywords."),
        "abstract": ("approximation", "ScienceDirect has no abstract-only field; widened to 'tak'."),
        "keywords": ("approximation", "KEYWORDS widened to 'tak'."),
        "all": ("semantic", "ALL_FIELDS mapped to 'qs' (full text and metadata)."),
    }

    def term(self, t: Term) -> str:
        if t.wildcard:
            self.note("unsupported", t, "ScienceDirect does not support truncation (it stems automatically).")
        txt = t.text.replace('"', "")
        return f'"{txt}"' if t.phrase or _needs_quotes(txt) else txt

    def near(self, n: Near) -> str:
        self.note("unsupported", n, "ScienceDirect Search API has no proximity operator.")
        return f"({self.term(n.left)} AND {self.term(n.right)})"

    def vocab(self, v: Vocab) -> str:
        self.note("unsupported", v, "ScienceDirect has no controlled-vocabulary search.")
        return f'"{v.heading}"'

    def compile(self) -> Compiled:
        from .ast import walk
        expr = self.q.expr
        oa = OpenAlexCompiler(self.q)
        branches = expr.children if isinstance(expr, Or) and oa._single_field(expr) is None else [expr]
        requests, natives = [], []
        for b in branches:
            fs = {n.field for n in walk(b) if isinstance(n, (Term, Near))}
            if len(fs) != 1:
                self.note("unsupported", b, "ScienceDirect fields can only be combined with AND at top level; "
                                            "this clause mixes fields.")
            f = fs.pop() if fs else "all"
            self._field_used(b, f)
            body = self.strip_outer(self.render(b))
            ops = len(re.findall(r"\b(AND|OR|NOT)\b", body))
            if ops > 8:
                self.note("unsupported", b, f"ScienceDirect allows at most 8 Boolean connectors per field; this "
                                            f"clause has {ops}. Split it into several OR'd searches.")
            req = {self.field_map[f]: body}
            if self.q.date:
                req["date"] = f"{(self.q.date.start or '1823')[:4]}-{(self.q.date.end or '3000')[:4]}"
            requests.append(req)
            natives.append(", ".join(f"{k}={v}" for k, v in req.items()))
        if self.q.date and ((self.q.date.start and not self.q.date.start.endswith("-01-01")) or
                            (self.q.date.end and not self.q.date.end.endswith("-12-31"))):
            self.note("approximation", None, "ScienceDirect date filter is year-granular.")
        self.note("semantic", None, "ScienceDirect applies automatic stemming and plural matching.")
        c = Compiled(self.source, self.platform, "\n  UNION\n".join(natives), requests, self.notes,
                     self.used_fields)
        return c.finalize()


COMPILERS: dict[str, type[Compiler]] = {
    "pubmed": PubMedCompiler, "europepmc": EuropePMCCompiler, "openalex": OpenAlexCompiler,
    "ctgov": CTGovCompiler, "scopus": ScopusCompiler, "embase": EmbaseCompiler, "wos": WoSCompiler,
    "sciencedirect": ScienceDirectCompiler,
}


def compile_query(q: Query, source: str) -> Compiled:
    return COMPILERS[source](q).compile()
