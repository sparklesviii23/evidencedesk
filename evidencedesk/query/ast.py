"""AST for the EvidenceDesk query language (EDQL).

Every node carries a source span so errors, previews and per-source notes can
point back at the exact clause the user wrote.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FIELDS = {
    "TITLE": "title",
    "ABSTRACT": "abstract",
    "TITLE_ABSTRACT": "title_abstract",
    "ALL_FIELDS": "all",
    "AUTHOR": "author",
    "JOURNAL": "journal",
    "KEYWORDS": "keywords",
}
FIELD_LABELS = {v: k for k, v in FIELDS.items()}
DEFAULT_FIELD = "all"

VOCABS = {"MESH": "mesh", "EMTREE": "emtree"}


@dataclass
class Span:
    start: int
    end: int

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end}


@dataclass
class Node:
    span: Span

    def to_dict(self) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError


@dataclass
class Term(Node):
    text: str
    phrase: bool = False
    wildcard: bool = False  # trailing * truncation
    field: str = DEFAULT_FIELD  # filled in by scope resolution

    def to_dict(self) -> dict:
        return {"type": "term", "text": self.text, "phrase": self.phrase,
                "wildcard": self.wildcard, "field": self.field, "span": self.span.to_dict()}

    def display(self) -> str:
        t = f'"{self.text}"' if self.phrase else self.text
        return t + ("*" if self.wildcard and not t.endswith("*") else "")


@dataclass
class And(Node):
    children: list[Node]

    def to_dict(self) -> dict:
        return {"type": "and", "children": [c.to_dict() for c in self.children], "span": self.span.to_dict()}


@dataclass
class Or(Node):
    children: list[Node]

    def to_dict(self) -> dict:
        return {"type": "or", "children": [c.to_dict() for c in self.children], "span": self.span.to_dict()}


@dataclass
class Not(Node):
    """Negated clause. Only valid as a non-first child of And ("A NOT B")."""
    child: Node

    def to_dict(self) -> dict:
        return {"type": "not", "child": self.child.to_dict(), "span": self.span.to_dict()}


@dataclass
class Field(Node):
    name: str  # internal field id, e.g. "title_abstract"
    child: Node

    def to_dict(self) -> dict:
        return {"type": "field", "field": self.name, "child": self.child.to_dict(), "span": self.span.to_dict()}


@dataclass
class Near(Node):
    """Unordered proximity: both operands within `distance` words of each other."""
    distance: int
    left: Term
    right: Term
    field: str = DEFAULT_FIELD

    def to_dict(self) -> dict:
        return {"type": "near", "distance": self.distance, "left": self.left.to_dict(),
                "right": self.right.to_dict(), "field": self.field, "span": self.span.to_dict()}


@dataclass
class Vocab(Node):
    """Controlled-vocabulary heading, e.g. MESH("Gene Editing", NOEXP)."""
    vocab: str  # "mesh" | "emtree"
    heading: str
    explode: bool = True
    major: bool = False

    def to_dict(self) -> dict:
        return {"type": "vocab", "vocab": self.vocab, "heading": self.heading, "explode": self.explode,
                "major": self.major, "span": self.span.to_dict()}


@dataclass
class DateRange:
    start: str | None  # ISO yyyy-mm-dd
    end: str | None
    start_precision: str = "day"  # "year" | "day"
    end_precision: str = "day"

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end,
                "start_precision": self.start_precision, "end_precision": self.end_precision}


@dataclass
class Query:
    text: str
    expr: Node | None
    sources: list[str] | None = None  # None == ALL
    sources_except: list[str] = field(default_factory=list)
    date: DateRange | None = None
    warnings: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "expr": self.expr.to_dict() if self.expr else None,
            "sources": self.sources if self.sources is not None else "ALL",
            "sources_except": self.sources_except,
            "date": self.date.to_dict() if self.date else None,
            "warnings": self.warnings,
        }


def walk(node: Node):
    yield node
    if isinstance(node, (And, Or)):
        for c in node.children:
            yield from walk(c)
    elif isinstance(node, (Not, Field)):
        yield from walk(node.child)
    elif isinstance(node, Near):
        yield node.left
        yield node.right


def canonical(node: Node | None) -> str:
    """Fully parenthesised EDQL rendering of a node (used for previews and diffs)."""
    if node is None:
        return ""
    if isinstance(node, Term):
        return node.display()
    if isinstance(node, And):
        parts = []
        for i, c in enumerate(node.children):
            if isinstance(c, Not):
                parts.append("NOT " + _wrap(c.child))
            else:
                parts.append(("AND " if i else "") + _wrap(c))
        return " ".join(parts)
    if isinstance(node, Or):
        return " OR ".join(_wrap(c) for c in node.children)
    if isinstance(node, Not):
        return "NOT " + _wrap(node.child)
    if isinstance(node, Field):
        return f"{FIELD_LABELS[node.name]}({canonical(node.child)})"
    if isinstance(node, Near):
        return f"NEAR/{node.distance}({node.left.display()}, {node.right.display()})"
    if isinstance(node, Vocab):
        flags = []
        if not node.explode:
            flags.append("NOEXP")
        if node.major:
            flags.append("MAJOR")
        name = "MESH" if node.vocab == "mesh" else "EMTREE"
        return f'{name}("{node.heading}"' + "".join(", " + f for f in flags) + ")"
    raise TypeError(node)


def _wrap(node: Node) -> str:
    s = canonical(node)
    return f"({s})" if isinstance(node, (And, Or)) and len(getattr(node, "children", [])) > 1 else s
