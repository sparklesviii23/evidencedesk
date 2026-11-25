"""Lexer and recursive-descent parser for EDQL. See docs/QUERY_LANGUAGE.md for the grammar."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .ast import (DEFAULT_FIELD, FIELD_LABELS, FIELDS, VOCABS, And, DateRange, Field, Near, Node, Not, Or,
                  Query, Span, Term, Vocab, walk)

KNOWN_SOURCES = ["pubmed", "europepmc", "ctgov", "openalex", "scopus", "embase", "wos", "sciencedirect"]
SOURCE_ALIASES = {
    "pubmed": "pubmed", "medline": "pubmed", "europepmc": "europepmc", "epmc": "europepmc",
    "europe_pmc": "europepmc", "ctgov": "ctgov", "clinicaltrials": "ctgov", "clinicaltrials.gov": "ctgov",
    "openalex": "openalex", "scopus": "scopus", "embase": "embase", "wos": "wos", "webofscience": "wos",
    "web_of_science": "wos", "sciencedirect": "sciencedirect",
}
OPERATORS = {"AND", "OR", "NOT"}
FUNCS = set(FIELDS) | set(VOCABS)
NEAR_RE = re.compile(r"^NEAR/(\d+)$")
VOCAB_FLAGS = {"NOEXP", "EXP", "MAJOR"}


class QuerySyntaxError(Exception):
    def __init__(self, message: str, pos: int, text: str, hint: str | None = None, end: int | None = None):
        self.message = message
        self.pos = pos
        self.end = end if end is not None else pos + 1
        self.hint = hint
        self.line, self.col = line_col(text, pos)
        super().__init__(self.format(text))

    def format(self, text: str) -> str:
        lines = text.splitlines() or [""]
        src = lines[self.line - 1] if self.line - 1 < len(lines) else ""
        caret = " " * (self.col - 1) + "^" * max(1, min(self.end - self.pos, len(src) - self.col + 1))
        msg = f"line {self.line}, col {self.col}: {self.message}\n  {src}\n  {caret}"
        if self.hint:
            msg += f"\n  hint: {self.hint}"
        return msg

    def to_dict(self) -> dict:
        return {"message": self.message, "line": self.line, "col": self.col, "start": self.pos,
                "end": self.end, "hint": self.hint}


def line_col(text: str, pos: int) -> tuple[int, int]:
    before = text[:pos]
    line = before.count("\n") + 1
    col = pos - (before.rfind("\n") + 1) + 1
    return line, col


@dataclass
class Tok:
    kind: str  # LP RP COMMA PHRASE WORD OP FUNC NEAR FLAG EOF
    value: str
    start: int
    end: int


def _strip_comments(text: str) -> str:
    out = []
    in_q = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            in_q = not in_q
        if ch == "#" and not in_q:
            j = text.find("\n", i)
            j = len(text) if j == -1 else j
            out.append(" " * (j - i))
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def tokenize(text: str, warnings: list[dict]) -> list[Tok]:
    toks: list[Tok] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            toks.append(Tok("LP", ch, i, i + 1)); i += 1; continue
        if ch == ")":
            toks.append(Tok("RP", ch, i, i + 1)); i += 1; continue
        if ch == ",":
            toks.append(Tok("COMMA", ch, i, i + 1)); i += 1; continue
        if ch in "\"“”":
            if ch != '"':
                warnings.append({"message": "Curly quote treated as a straight double quote", "start": i, "end": i + 1})
            j = i + 1
            while j < n and text[j] not in "\"“”":
                if text[j] == "\n":
                    raise QuerySyntaxError("Unterminated phrase: closing quote missing on this line", i, text,
                                           "Close the phrase with a double quote (\").")
                j += 1
            if j >= n:
                raise QuerySyntaxError("Unterminated phrase: closing quote missing", i, text,
                                       "Close the phrase with a double quote (\").")
            inner = " ".join(text[i + 1:j].split())
            if not inner:
                raise QuerySyntaxError("Empty phrase", i, text, end=j + 1)
            toks.append(Tok("PHRASE", inner, i, j + 1))
            i = j + 1
            continue
        j = i
        while j < n and not text[j].isspace() and text[j] not in '(),"“”':
            j += 1
        word = text[i:j]
        if word in OPERATORS:
            toks.append(Tok("OP", word, i, j))
        elif NEAR_RE.match(word):
            toks.append(Tok("NEAR", word, i, j))
        elif word.upper().startswith("NEAR/") or word.upper() == "NEAR":
            raise QuerySyntaxError(f"Malformed proximity operator '{word}'", i, text,
                                   "Write NEAR/n(term, term), e.g. NEAR/5(ANGPTL3, editing).", end=j)
        elif word in FUNCS and j < n and text[j] == "(":
            toks.append(Tok("FUNC", word, i, j))
        elif word.upper() in FUNCS and j < n and text[j] == "(":
            raise QuerySyntaxError(f"Field and function names are upper case: '{word}'", i, text,
                                   f"Did you mean {word.upper()}(...)?", end=j)
        elif word in VOCAB_FLAGS:
            toks.append(Tok("FLAG", word, i, j))
        else:
            if word.lower() in {"and", "or", "not"} and word not in OPERATORS:
                warnings.append({"message": f"'{word}' is searched as a word. Operators must be upper case "
                                            f"({word.upper()}).", "start": i, "end": j})
            toks.append(Tok("WORD", word, i, j))
        i = j
    toks.append(Tok("EOF", "", n, n))
    return toks


DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def _parse_date(s: str, end: bool, text: str, pos: int) -> tuple[str, str]:
    m = DATE_RE.match(s)
    if not m:
        raise QuerySyntaxError(f"Invalid date '{s}'", pos, text, "Use YYYY, YYYY-MM or YYYY-MM-DD.", end=pos + len(s))
    y, mo, d = m.groups()
    if mo is None:
        return (f"{y}-12-31" if end else f"{y}-01-01"), "year"
    if d is None:
        import calendar
        last = calendar.monthrange(int(y), int(mo))[1]
        return (f"{y}-{mo}-{last:02d}" if end else f"{y}-{mo}-01"), "month"
    return f"{y}-{mo}-{d}", "day"


def _parse_directives(text: str, q: Query) -> str:
    """Consume SOURCES / PUBDATE lines. Returns text with those lines blanked (offsets preserved)."""
    out_lines = []
    offset = 0
    seen_expr = False
    for line in text.split("\n"):
        stripped = line.strip()
        head = stripped.split(None, 1)[0] if stripped else ""
        lead = len(line) - len(line.lstrip())
        if head in ("SOURCES", "PUBDATE"):
            if seen_expr:
                raise QuerySyntaxError(f"{head} must appear before the search expression", offset + lead, text,
                                       f"Move the {head} line to the top of the query.")
            rest = stripped[len(head):].strip()
            rest_pos = offset + lead + len(head) + (len(stripped[len(head):]) - len(stripped[len(head):].lstrip()))
            if head == "SOURCES":
                _parse_sources(rest, q, text, rest_pos)
            else:
                _parse_pubdate(rest, q, text, rest_pos)
            out_lines.append(" " * len(line))
        else:
            if stripped:
                seen_expr = True
            out_lines.append(line)
        offset += len(line) + 1
    return "\n".join(out_lines)


def _parse_sources(rest: str, q: Query, text: str, pos: int) -> None:
    if not rest:
        raise QuerySyntaxError("SOURCES needs ALL or a comma-separated list", pos, text,
                               "e.g. SOURCES ALL  or  SOURCES pubmed, europepmc")
    m = re.match(r"^ALL(?:\s+EXCEPT\s+(.+))?$", rest)
    if m:
        q.sources = None
        if m.group(1):
            q.sources_except = [_source_name(s, text, pos) for s in m.group(1).split(",")]
        return
    q.sources = [_source_name(s, text, pos) for s in rest.split(",")]


def _source_name(s: str, text: str, pos: int) -> str:
    key = s.strip().lower()
    if key not in SOURCE_ALIASES:
        import difflib
        close = difflib.get_close_matches(key, list(SOURCE_ALIASES), n=1)
        raise QuerySyntaxError(f"Unknown source '{s.strip()}'", pos + max(0, text[pos:].find(s.strip())), text,
                               f"Did you mean '{close[0]}'?" if close else "Known: " + ", ".join(KNOWN_SOURCES),
                               end=pos + max(0, text[pos:].find(s.strip())) + len(s.strip()))
    return SOURCE_ALIASES[key]


def _parse_pubdate(rest: str, q: Query, text: str, pos: int) -> None:
    m = re.match(r"^(\S+)?\s*(?:TO|\.\.)\s*(\S+)?$", rest) or re.match(r"^(\S+)\.\.(\S+)?$", rest)
    if not m:
        raise QuerySyntaxError("PUBDATE needs a range", pos, text,
                               "e.g. PUBDATE 2015 TO 2025, PUBDATE 2015-01-01 TO 2025-06-30, PUBDATE 2015 TO *")
    a, b = m.group(1), m.group(2)
    a = None if a in (None, "*") else a
    b = None if b in (None, "*") else b
    if a is None and b is None:
        raise QuerySyntaxError("PUBDATE range is open on both ends", pos, text)
    start, sp = _parse_date(a, False, text, pos) if a else (None, "day")
    end, ep = _parse_date(b, True, text, pos + rest.find(b)) if b else (None, "day")
    if start and end and start > end:
        raise QuerySyntaxError("PUBDATE start is after end", pos, text)
    q.date = DateRange(start, end, sp, ep)


class Parser:
    def __init__(self, text: str, toks: list[Tok], warnings: list[dict]):
        self.text = text
        self.toks = toks
        self.i = 0
        self.warnings = warnings

    @property
    def tok(self) -> Tok:
        return self.toks[self.i]

    def eat(self, kind: str, value: str | None = None) -> Tok:
        t = self.tok
        if t.kind != kind or (value is not None and t.value != value):
            raise self.err(f"Expected {value or kind}, found {self.describe(t)}")
        self.i += 1
        return t

    def describe(self, t: Tok) -> str:
        return "end of query" if t.kind == "EOF" else f"'{t.value}'"

    def err(self, msg: str, hint: str | None = None, t: Tok | None = None) -> QuerySyntaxError:
        t = t or self.tok
        return QuerySyntaxError(msg, t.start, self.text, hint, end=max(t.end, t.start + 1))

    def parse(self) -> Node | None:
        if self.tok.kind == "EOF":
            return None
        node = self.parse_or()
        if self.tok.kind != "EOF":
            t = self.tok
            if t.kind == "RP":
                raise self.err("Unmatched closing parenthesis", "Remove it or add a matching '('.")
            if t.kind in ("WORD", "PHRASE", "FUNC", "NEAR", "LP"):
                prev = self.toks[self.i - 1]
                if t.value.lower() in ("and", "or", "not"):
                    raise self.err(f"Lower-case '{t.value}' is not an operator",
                                   f"Operators are upper case: write {t.value.upper()}, or quote the phrase.")
                raise self.err(f"Missing operator between {self.describe(prev)} and {self.describe(t)}",
                               "Join clauses with AND / OR / NOT, or quote a multi-word phrase "
                               f'(e.g. "{prev.value} {t.value}").')
            raise self.err(f"Unexpected {self.describe(t)}")
        return node

    def parse_or(self) -> Node:
        first = self.parse_and()
        children = [first]
        while self.tok.kind == "OP" and self.tok.value == "OR":
            self.i += 1
            children.append(self.parse_and())
        if len(children) == 1:
            return first
        return Or(Span(children[0].span.start, children[-1].span.end), children)

    def parse_and(self) -> Node:
        start_i = self.i
        if self.tok.kind == "OP" and self.tok.value == "NOT":
            raise self.err("NOT must follow a positive clause", "Write A NOT B (pure negation is not searchable).")
        first = self.parse_primary()
        children: list[Node] = [first]
        ops = []
        while self.tok.kind == "OP" and self.tok.value in ("AND", "NOT"):
            op = self.tok.value
            ops.append(op)
            self.i += 1
            if self.tok.kind == "OP":
                raise self.err(f"Operator {op} followed by operator {self.tok.value}")
            nxt = self.parse_primary()
            children.append(Not(nxt.span, nxt) if op == "NOT" else nxt)
        if len(children) == 1:
            return first
        # warn when AND/OR are mixed without parentheses at this level
        if self.tok.kind == "OP" and self.tok.value == "OR" or (
                start_i > 0 and self.toks[start_i - 1].kind == "OP" and self.toks[start_i - 1].value == "OR"):
            self.warnings.append({
                "message": "AND/NOT and OR mixed without parentheses. EDQL binds NOT > AND > OR; "
                           "compiled queries are fully parenthesised as interpreted.",
                "start": children[0].span.start, "end": children[-1].span.end})
        return And(Span(children[0].span.start, children[-1].span.end), children)

    def parse_primary(self) -> Node:
        t = self.tok
        if t.kind == "LP":
            self.i += 1
            if self.tok.kind == "RP":
                raise self.err("Empty parentheses")
            node = self.parse_or()
            if self.tok.kind != "RP":
                raise QuerySyntaxError("Unclosed parenthesis", t.start, self.text,
                                       f"Add ')' — found {self.describe(self.tok)} instead.")
            self.i += 1
            node.span = Span(t.start, self.toks[self.i - 1].end)
            return node
        if t.kind == "FUNC":
            return self.parse_func()
        if t.kind == "NEAR":
            return self.parse_near()
        if t.kind in ("WORD", "PHRASE"):
            return self.parse_term()
        if t.kind == "OP":
            raise self.err(f"Operator {t.value} needs a clause before it" if self.i == 0 else
                           f"Expected a search term after the previous operator, found operator {t.value}")
        if t.kind == "RP":
            raise self.err("Unexpected ')'", "Check for an operator with nothing after it.")
        if t.kind == "EOF":
            raise self.err("Query ends after an operator", "Add a term after the last AND/OR/NOT.")
        if t.kind == "FLAG":
            raise self.err(f"{t.value} is only valid inside MESH(...) or EMTREE(...)")
        raise self.err(f"Unexpected {self.describe(t)}")

    def parse_term(self) -> Term:
        t = self.tok
        self.i += 1
        text = t.value
        phrase = t.kind == "PHRASE"
        wildcard = False
        if "?" in text:
            raise QuerySyntaxError("Single-character wildcard '?' is not supported", t.start, self.text,
                                   "Use trailing truncation (edit*) or list variants with OR.", end=t.end)
        if "*" in text:
            if text.endswith("*") and "*" not in text[:-1]:
                wildcard = True
                text = text[:-1].rstrip()
                if len(text.replace(" ", "")) < 3:
                    raise QuerySyntaxError("Truncation needs at least 3 leading characters", t.start, self.text,
                                           end=t.end)
            else:
                raise QuerySyntaxError("Only trailing truncation is supported (e.g. edit*)", t.start, self.text,
                                       end=t.end)
        return Term(Span(t.start, t.end), text=text, phrase=phrase and " " in text or phrase, wildcard=wildcard)

    def parse_func(self) -> Node:
        t = self.eat("FUNC")
        lp = self.eat("LP")
        if t.value in FIELDS:
            if self.tok.kind == "RP":
                raise self.err(f"{t.value}() is empty")
            inner = self.parse_or()
            if self.tok.kind in ("WORD", "PHRASE", "FUNC", "NEAR", "LP"):
                prev, cur = self.toks[self.i - 1], self.tok
                if cur.value.lower() in ("and", "or", "not"):
                    raise self.err(f"Lower-case '{cur.value}' is not an operator",
                                   f"Operators are upper case: write {cur.value.upper()}, or quote the phrase.")
                raise self.err(f"Missing operator between {self.describe(prev)} and {self.describe(cur)}",
                               "Join clauses with AND / OR / NOT, or quote a multi-word phrase "
                               f'(e.g. "{prev.value} {cur.value}").')
            if self.tok.kind != "RP":
                raise QuerySyntaxError(f"Unclosed {t.value}(", lp.start, self.text,
                                       f"Add ')' — found {self.describe(self.tok)} instead.")
            rp = self.eat("RP")
            return Field(Span(t.start, rp.end), FIELDS[t.value], inner)
        # vocab
        if self.tok.kind not in ("PHRASE", "WORD"):
            raise self.err(f'{t.value} needs a heading, e.g. {t.value}("Gene Editing")')
        head = self.tok
        self.i += 1
        explode, major = True, False
        while self.tok.kind == "COMMA":
            self.i += 1
            f = self.tok
            if f.kind not in ("FLAG", "WORD") or f.value.upper() not in VOCAB_FLAGS:
                raise self.err(f"Unknown {t.value} option {self.describe(f)}", "Options: NOEXP, EXP, MAJOR")
            self.i += 1
            v = f.value.upper()
            if v == "NOEXP":
                explode = False
            elif v == "EXP":
                explode = True
            else:
                major = True
        rp = self.eat("RP")
        return Vocab(Span(t.start, rp.end), VOCABS[t.value], head.value, explode, major)

    def parse_near(self) -> Near:
        t = self.eat("NEAR")
        dist = int(NEAR_RE.match(t.value).group(1))
        if dist < 1 or dist > 50:
            raise self.err("NEAR distance must be between 1 and 50", t=t)
        self.eat("LP")
        a = self._near_operand()
        if self.tok.kind != "COMMA":
            raise self.err("NEAR/n takes exactly two operands separated by a comma",
                           "e.g. NEAR/5(ANGPTL3, \"base editing\")")
        self.i += 1
        b = self._near_operand()
        if self.tok.kind != "RP":
            raise self.err("NEAR/n takes exactly two operands", "Use OR outside NEAR to combine alternatives.")
        rp = self.eat("RP")
        return Near(Span(t.start, rp.end), dist, a, b)

    def _near_operand(self) -> Term:
        if self.tok.kind not in ("WORD", "PHRASE"):
            raise self.err("NEAR/n operands must be single words or quoted phrases",
                           "Nested Boolean inside NEAR is not supported; expand with OR outside NEAR.")
        return self.parse_term()


def _resolve_scopes(node: Node, field: str | None, text: str) -> None:
    if isinstance(node, Term):
        node.field = field or DEFAULT_FIELD
    elif isinstance(node, Near):
        node.field = field or DEFAULT_FIELD
        node.left.field = node.field
        node.right.field = node.field
    elif isinstance(node, Field):
        if field is not None and field != node.name:
            raise QuerySyntaxError(
                f"{FIELD_LABELS[node.name]}(...) is nested inside {FIELD_LABELS[field]}(...)", node.span.start, text,
                "Field scopes cannot be nested with a different field; move the clause outside.",
                end=node.span.end)
        _resolve_scopes(node.child, node.name, text)
    elif isinstance(node, Vocab):
        if field is not None:
            raise QuerySyntaxError("Controlled-vocabulary terms cannot sit inside a field scope", node.span.start,
                                   text, "Move MESH(...)/EMTREE(...) outside TITLE_ABSTRACT(...).", end=node.span.end)
    elif isinstance(node, (And, Or)):
        for c in node.children:
            _resolve_scopes(c, field, text)
    elif isinstance(node, Not):
        _resolve_scopes(node.child, field, text)


def _flatten(node: Node) -> Node:
    """Merge nested same-type Boolean nodes: (a OR (b OR c)) -> OR(a,b,c). Field nodes are dropped after
    scope resolution because every leaf now carries its field."""
    if isinstance(node, Field):
        return _flatten(node.child)
    if isinstance(node, Not):
        return Not(node.span, _flatten(node.child))
    if isinstance(node, (And, Or)):
        kids = []
        for c in node.children:
            fc = _flatten(c)
            if type(fc) is type(node):
                kids.extend(fc.children)
            else:
                kids.append(fc)
        return type(node)(node.span, kids)
    return node


def parse(text: str, keep_fields: bool = False) -> Query:
    """Parse EDQL text. Raises QuerySyntaxError. Leaves carry resolved fields."""
    warnings: list[dict] = []
    q = Query(text=text, expr=None, warnings=warnings)
    body = _parse_directives(_strip_comments(text), q)
    toks = tokenize(body, warnings)
    p = Parser(text, toks, warnings)
    expr = p.parse()
    if expr is None:
        raise QuerySyntaxError("Query has no search expression", len(text), text,
                               "Add terms after the SOURCES/PUBDATE lines.")
    _resolve_scopes(expr, None, text)
    q.expr = expr if keep_fields else _flatten(expr)
    if q.sources is not None:
        q.sources = list(dict.fromkeys(q.sources))
    for w in warnings:
        w["line"], w["col"] = line_col(text, w["start"])
    return q


def validate(text: str) -> dict:
    """Validation result for UI/agent: never raises."""
    try:
        q = parse(text)
    except QuerySyntaxError as e:
        return {"valid": False, "errors": [e.to_dict()], "warnings": [], "formatted_error": e.format(text)}
    from .ast import canonical
    leaves = [n for n in walk(q.expr) if isinstance(n, (Term, Vocab))]
    return {"valid": True, "errors": [], "warnings": q.warnings, "ast": q.to_dict(),
            "canonical": canonical(q.expr), "clauses": len(leaves)}
