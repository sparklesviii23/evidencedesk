"""Crossref (DOI metadata, publisher full-text links, licences) and Unpaywall (OA locations). Enrichment only:
neither supports Boolean search, so neither is a search source for SOURCES ALL."""
from __future__ import annotations

from urllib.parse import quote

from ..credentials import get
from ..http import SourceError, request
from .base import Adapter, empty_record, kind_from_types, norm_doi


class CrossrefAdapter(Adapter):
    name = "crossref"
    platform = "Crossref REST API"
    role = "enrichment"
    optional_credentials = ["plus_token"]
    docs_url = "https://api.crossref.org/swagger-ui/index.html"
    access = "Public. Contact e-mail recommended (polite pool)."
    verified_live = True

    def connection_status(self) -> dict:
        return {"status": "public", "detail": "Enrichment only (no Boolean search)"}

    def _headers(self):
        h = {}
        tok = self.cred("plus_token")
        if tok:
            h["Crossref-Plus-API-Token"] = f"Bearer {tok}"
        return h

    def _params(self):
        email = get("contact", "email")
        return {"mailto": email} if email else {}

    def test_connection(self) -> dict:
        self.work("10.1161/CIRCULATIONAHA.117.031335")
        return {"ok": True, "detail": "Crossref reachable"}

    def work(self, doi: str) -> dict | None:
        try:
            r = request("GET", f"https://api.crossref.org/works/{quote(doi, safe='/')}", source=self.name,
                        per_sec=5, params=self._params(), headers=self._headers(), expect_json=True)
        except SourceError as e:
            if e.kind == "not_found":
                return None
            raise
        return parse_crossref(r.json()["message"])

    def search_bibliographic(self, text: str, rows=5) -> list[dict]:
        r = request("GET", "https://api.crossref.org/works", source=self.name, per_sec=5,
                    params={**self._params(), "query.bibliographic": text, "rows": rows}, headers=self._headers(),
                    expect_json=True)
        return [parse_crossref(m) for m in r.json()["message"]["items"]]


def parse_crossref(m: dict) -> dict:
    doi = norm_doi(m.get("DOI"))
    rec = empty_record("crossref", doi)
    rec["raw"] = m
    rec["doi"] = doi
    rec["title"] = (m.get("title") or [None])[0]
    import re
    if m.get("abstract"):
        rec["abstract"] = " ".join(re.sub(r"<[^>]+>", " ", m["abstract"]).split())
    rec["authors"] = [" ".join(x for x in [a.get("family"), (a.get("given") or "")[:1]] if x) or a.get("name")
                      for a in m.get("author", [])]
    dp = (m.get("issued") or {}).get("date-parts") or [[None]]
    rec["year"] = dp[0][0] if dp and dp[0] else None
    rec["journal"] = (m.get("container-title") or [None])[0]
    rec["volume"], rec["issue"], rec["pages"] = m.get("volume"), m.get("issue"), m.get("page")
    rec["pub_types"] = [m.get("type") or ""] + ([m["subtype"]] if m.get("subtype") else [])
    rec["report_kind"] = kind_from_types(rec["pub_types"])
    rec["other_ids"] = {
        "licenses": [{"url": l.get("URL"), "content_version": l.get("content-version")} for l in m.get("license", [])],
        "links": [{"url": l.get("URL"), "content_type": l.get("content-type"),
                   "intended": l.get("intended-application")} for l in m.get("link", [])],
        "relation": m.get("relation"), "publisher": m.get("publisher"),
        "update_to": m.get("update-to"), "is_preprint_of": (m.get("relation") or {}).get("is-preprint-of"),
        "has_preprint": (m.get("relation") or {}).get("has-preprint"),
    }
    rec["url"] = f"https://doi.org/{doi}" if doi else None
    return rec


class UnpaywallAdapter(Adapter):
    name = "unpaywall"
    platform = "Unpaywall API"
    role = "enrichment"
    docs_url = "https://unpaywall.org/products/api"
    access = "Public; requires a contact e-mail on every request (Settings > contact e-mail)."
    verified_live = True

    def connection_status(self) -> dict:
        if not get("contact", "email"):
            return {"status": "disconnected", "detail": "Unpaywall requires a contact e-mail (Settings > Contact)."}
        return {"status": "public", "detail": "Contact e-mail configured"}

    def test_connection(self) -> dict:
        d = self.lookup("10.1161/CIRCULATIONAHA.117.031335")
        return {"ok": d is not None, "detail": f"Unpaywall reachable; is_oa={d and d.get('is_oa')}"}

    def lookup(self, doi: str) -> dict | None:
        email = get("contact", "email")
        if not email:
            raise SourceError("Unpaywall requires a contact e-mail (configure it in Settings).", "auth")
        try:
            r = request("GET", f"https://api.unpaywall.org/v2/{quote(doi, safe='/')}", source=self.name,
                        per_sec=5, params={"email": email}, expect_json=True)
        except SourceError as e:
            if e.kind == "not_found":
                return None
            raise
        return r.json()
