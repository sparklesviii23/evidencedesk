"""OpenAlex Works API: discovery (cursor pagination, multi-branch unions) and citation chasing."""
from __future__ import annotations

from ..credentials import get
from ..http import SourceError, request
from .base import Adapter, Page, empty_record, kind_from_types, norm_doi, norm_pmcid, norm_pmid

BASE = "https://api.openalex.org/"


class OpenAlexAdapter(Adapter):
    name = "openalex"
    platform = "OpenAlex Works API"
    optional_credentials = ["api_key"]
    docs_url = "https://docs.openalex.org/"
    access = ("Public. A free API key (openalex.org/settings/api) is strongly recommended: anonymous requests "
              "share a small daily budget per IP and search is rate-limited under load.")
    verified_live = True
    page_size = 200

    def _params(self, **kw):
        p = dict(kw)
        key = self.cred("api_key")
        if key:
            p["api_key"] = key
        email = get("contact", "email")
        if email:
            p["mailto"] = email
        return p

    def connection_status(self) -> dict:
        if self.cred("api_key"):
            return {"status": "configured", "detail": "API key configured"}
        return {"status": "public", "detail": "No API key: anonymous shared budget, may be rate-limited"}

    def _get(self, path, params, on_retry=None):
        r = request("GET", BASE + path, source=self.name, per_sec=8, params=self._params(**params),
                    on_retry=on_retry, expect_json=True)
        d = r.json()
        if "error" in d:
            raise SourceError(f"OpenAlex: {d.get('error')} {d.get('message', '')[:300]}", "bad_query")
        return d

    def test_connection(self) -> dict:
        d = self._get("works", {"filter": "title.search:ANGPTL3", "per-page": 1, "select": "id"})
        return {"ok": True, "detail": f"OpenAlex reachable; meta={ {k: d['meta'].get(k) for k in ('count',)} }"
                                      + ("" if self.cred("api_key") else " (anonymous)")}

    def search(self, compiled, cursor, on_retry=None) -> Page:
        reqs = compiled.requests
        cur = cursor or {"branch": 0, "cursor": "*", "counts": [None] * len(reqs), "seen": 0}
        b = cur["branch"]
        flt = ",".join(f"{k}:{v}" for k, v in reqs[b]["filter"])
        d = self._get("works", {"filter": flt, "per-page": self.page_size, "cursor": cur["cursor"]}, on_retry)
        meta = d.get("meta", {})
        cur["counts"][b] = meta.get("count")
        recs = [parse_openalex(w) for w in d.get("results", [])]
        cur["seen"] += len(recs)
        nxt = meta.get("next_cursor")
        notes = []
        if not nxt or not recs:
            if b + 1 < len(reqs):
                cur = {**cur, "branch": b + 1, "cursor": "*", "seen": 0}
            else:
                cur = None
        else:
            cur["cursor"] = nxt
        counts = (cursor or {}).get("counts") or [None] * len(reqs)
        counts[b] = meta.get("count")
        known = [c for c in counts if c is not None]
        total = sum(known) if len(known) == len(reqs) else (sum(known) if known else None)
        if len(reqs) > 1:
            notes.append(f"OpenAlex branch counts (union computed after retrieval): {counts}")
        if cur is not None:
            cur["counts"] = counts
        return Page(total, recs, cur, notes)

    def works_by_ids(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            chunk = [x.rsplit("/", 1)[-1] for x in ids[i:i + 50]]
            d = self._get("works", {"filter": "openalex:" + "|".join(chunk), "per-page": 50})
            out += [parse_openalex(w) for w in d.get("results", [])]
        return out

    def work(self, key: str) -> dict | None:
        """key: OpenAlex ID, 'doi:10...', 'pmid:123'"""
        try:
            d = self._get(f"works/{key}", {})
        except SourceError as e:
            if e.kind == "not_found":
                return None
            raise
        return parse_openalex(d)

    def citing(self, work_id: str, limit=1000) -> list[dict]:
        out, cursor = [], "*"
        while cursor and len(out) < limit:
            d = self._get("works", {"filter": f"cites:{work_id}", "per-page": 200, "cursor": cursor})
            out += [parse_openalex(w) for w in d.get("results", [])]
            cursor = d.get("meta", {}).get("next_cursor") if d.get("results") else None
        return out


def _abstract(inv: dict | None) -> str | None:
    if not inv:
        return None
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos)) or None


def parse_openalex(w: dict) -> dict:
    wid = (w.get("id") or "").rsplit("/", 1)[-1]
    rec = empty_record("openalex", wid)
    raw = dict(w)
    rec["raw"] = raw
    rec["title"] = w.get("title") or w.get("display_name")
    rec["abstract"] = _abstract(w.get("abstract_inverted_index"))
    rec["authors"] = [(a.get("author") or {}).get("display_name") for a in w.get("authorships") or []
                      if (a.get("author") or {}).get("display_name")]
    rec["year"] = w.get("publication_year")
    rec["pub_date"] = w.get("publication_date")
    src = ((w.get("primary_location") or {}).get("source") or {})
    rec["journal"] = src.get("display_name")
    bib = w.get("biblio") or {}
    rec["volume"], rec["issue"] = bib.get("volume"), bib.get("issue")
    if bib.get("first_page"):
        rec["pages"] = bib["first_page"] + (f"-{bib['last_page']}" if bib.get("last_page") else "")
    ids = w.get("ids") or {}
    rec["doi"] = norm_doi(w.get("doi") or ids.get("doi"))
    rec["pmid"] = norm_pmid((ids.get("pmid") or "").rsplit("/", 1)[-1]) if ids.get("pmid") else None
    rec["pmcid"] = norm_pmcid((ids.get("pmcid") or "").rstrip("/").rsplit("/", 1)[-1]) if ids.get("pmcid") else None
    rec["other_ids"] = {"openalex": wid, "mag": ids.get("mag")}
    t = w.get("type") or ""
    rec["pub_types"] = [t, w.get("type_crossref") or ""]
    rec["keywords"] = [k.get("display_name") for k in w.get("keywords") or [] if k.get("display_name")]
    rec["mesh"] = [m.get("descriptor_name") for m in w.get("mesh") or [] if m.get("descriptor_name")]
    rec["language"] = w.get("language")
    oa = w.get("open_access") or {}
    rec["is_oa"] = 1 if oa.get("is_oa") else 0
    best = w.get("best_oa_location") or {}
    rec["other_ids"]["oa_url"] = oa.get("oa_url")
    rec["other_ids"]["pdf_url"] = best.get("pdf_url")
    rec["other_ids"]["license"] = best.get("license")
    rec["other_ids"]["referenced_works_count"] = len(w.get("referenced_works") or [])
    kind = kind_from_types(rec["pub_types"])
    if t == "preprint" or src.get("type") == "repository" and t == "preprint":
        kind = "preprint"
    rec["report_kind"] = kind
    rec["url"] = w.get("id")
    return rec
