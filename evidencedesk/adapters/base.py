"""Adapter interface shared by every bibliographic source."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..credentials import get

REPORT_KINDS = ["journal_article", "preprint", "conference_abstract", "registry", "review", "letter_editorial",
                "protocol", "thesis", "book", "dataset", "erratum", "other"]


@dataclass
class Page:
    total: int | None
    records: list[dict]
    cursor: dict | None  # cursor to fetch the next page; None == finished
    notes: list[str] = field(default_factory=list)


class Adapter:
    name = ""
    platform = ""
    role = "search"  # search | enrichment
    register = False  # PRISMA: True for trial registers (counted separately from databases)
    credentials: list[str] = []  # required credential keys
    optional_credentials: list[str] = []
    docs_url = ""
    access = ""  # human-readable access requirement
    verified_live = False  # set True only for adapters exercised against the live service
    page_size = 100

    def cred(self, key: str) -> str | None:
        return get(self.name, key)

    def configured(self) -> bool:
        return all(self.cred(k) for k in self.credentials)

    def connection_status(self) -> dict:
        if not self.configured():
            missing = [k for k in self.credentials if not self.cred(k)]
            return {"status": "disconnected", "detail": f"Missing credential(s): {', '.join(missing)}. "
                                                        f"{self.access}"}
        return {"status": "configured", "detail": "Credentials present; run a connection test to verify."}

    def test_connection(self) -> dict:
        raise NotImplementedError

    def search(self, compiled, cursor: dict | None, on_retry=None) -> Page:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name, "platform": self.platform, "role": self.role, "register": self.register,
                "credentials": self.credentials, "optional_credentials": self.optional_credentials,
                "docs_url": self.docs_url, "access": self.access, "verified_live": self.verified_live,
                **self.connection_status()}


DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.I)
# trial-registry identifiers used for study linking (ClinicalTrials.gov, ANZCTR, ISRCTN, ChiCTR, EudraCT/CTIS,
# CTRI, jRCT, DRKS, IRCT, PACTR)
NCT_RE = re.compile(r"\b(NCT\d{8}|ACTRN\d{14}|ISRCTN\d{8}|ChiCTR[-A-Z0-9]{8,20}|\d{4}-\d{6}-\d{2}|"
                    r"20\d\d-5\d{5}-\d{2}-\d{2}|CTRI/\d{4}/\d{2,3}/\d{6}|jRCT[a-z]?\d{10}|DRKS\d{8}|"
                    r"IRCT\d{8,}N\d+|PACTR\d{15})\b", re.I)


def norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    m = DOI_RE.search(str(doi).strip())
    if not m:
        return None
    return m.group(0).rstrip(".,;)]").lower()


def norm_pmid(p) -> str | None:
    if p is None:
        return None
    s = re.sub(r"\D", "", str(p))
    return s or None


def norm_pmcid(p) -> str | None:
    if not p:
        return None
    s = str(p).upper().strip()
    if s.isdigit():
        s = "PMC" + s
    return s if re.match(r"^PMC\d+$", s) else None


def find_ncts(*texts) -> list[str]:
    out = []
    for t in texts:
        if t:
            out += [m.upper() if m.upper().startswith(("NCT", "ACTRN", "ISRCTN", "DRKS", "PACTR")) else m
                    for m in NCT_RE.findall(str(t))]
    return list(dict.fromkeys(out))


def empty_record(source: str, sid: str) -> dict:
    return {"source": source, "source_record_id": str(sid), "title": None, "abstract": None, "authors": [],
            "year": None, "pub_date": None, "journal": None, "volume": None, "issue": None, "pages": None,
            "doi": None, "pmid": None, "pmcid": None, "nct_ids": [], "other_ids": {}, "pub_types": [],
            "keywords": [], "mesh": [], "language": None, "url": None, "is_oa": None,
            "report_kind": "journal_article", "raw_format": "json", "raw": None}


def kind_from_types(types: list[str], default="journal_article") -> str:
    t = " ".join(types).lower()
    if "preprint" in t or "posted-content" in t:
        return "preprint"
    if "congress" in t or "conference" in t or "meeting abstract" in t or "proceedings" in t:
        return "conference_abstract"
    if "erratum" in t or "correction" in t or "retraction" in t:
        return "erratum"
    if "protocol" in t:
        return "protocol"
    if "systematic review" in t or "review" in t or "meta-analysis" in t:
        return "review"
    if "editorial" in t or "letter" in t or "comment" in t or "news" in t:
        return "letter_editorial"
    if "dissertation" in t or "thesis" in t:
        return "thesis"
    if "book" in t:
        return "book"
    if "dataset" in t:
        return "dataset"
    return default
