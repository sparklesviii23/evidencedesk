"""Adapters for licensed databases: Scopus, ScienceDirect, Embase (Elsevier) and Web of Science (Clarivate).

Implemented from the vendors' documented REST interfaces. They are NOT verified live in this build because no
institutional credentials were available; `verified_live` stays False until a connection test and a search
succeed with real credentials (the connection test records this in the settings page). Without credentials
each source is shown as DISCONNECTED and its compiled native query is offered for manual execution + import.
"""
from __future__ import annotations

from ..http import SourceError, request
from .base import Adapter, Page, empty_record, kind_from_types, norm_doi, norm_pmid

ELS = "https://api.elsevier.com/content/"


class _Elsevier(Adapter):
    credentials = ["api_key"]
    optional_credentials = ["insttoken"]

    def _headers(self):
        h = {"X-ELS-APIKey": self.cred("api_key") or "", "Accept": "application/json"}
        if self.cred("insttoken"):
            h["X-ELS-Insttoken"] = self.cred("insttoken")
        return h

    def _require(self):
        if not self.configured():
            raise SourceError(f"{self.platform}: no API key configured", "auth")


class ScopusAdapter(_Elsevier):
    name = "scopus"
    platform = "Scopus Search API"
    docs_url = "https://dev.elsevier.com/documentation/ScopusSearchAPI.wadl"
    access = ("Elsevier API key from dev.elsevier.com AND an institutional Scopus subscription (requests must "
              "come from a subscriber IP range or carry an institutional token). Abstracts need view=COMPLETE.")
    page_size = 25

    def test_connection(self) -> dict:
        self._require()
        r = request("GET", ELS + "search/scopus", source=self.name, per_sec=2, headers=self._headers(),
                    params={"query": "TITLE(ANGPTL3)", "count": 1}, expect_json=True)
        tot = r.json()["search-results"]["opensearch:totalResults"]
        return {"ok": True, "detail": f"Scopus reachable; TITLE(ANGPTL3) = {tot}"}

    def search(self, compiled, cursor, on_retry=None) -> Page:
        self._require()
        view = (cursor or {}).get("view", "COMPLETE")
        params = {"query": compiled.requests[0]["query"], "count": self.page_size, "cursor": (cursor or {}).get(
            "cursor", "*"), "view": view}
        try:
            r = request("GET", ELS + "search/scopus", source=self.name, per_sec=2, headers=self._headers(),
                        params=params, on_retry=on_retry, expect_json=True)
        except SourceError as e:
            if e.kind == "access" and view == "COMPLETE":  # not entitled to COMPLETE -> STANDARD (no abstracts)
                cursor = {**(cursor or {}), "view": "STANDARD"}
                return self.search(compiled, cursor, on_retry)
            raise
        sr = r.json()["search-results"]
        total = int(sr.get("opensearch:totalResults", 0))
        entries = [e for e in sr.get("entry", []) if "error" not in e]
        recs = [parse_scopus(e) for e in entries]
        nxt = (sr.get("cursor") or {}).get("@next")
        seen = (cursor or {}).get("seen", 0) + len(recs)
        notes = ["Scopus STANDARD view: abstracts not returned (no COMPLETE entitlement)."] if view == "STANDARD" else []
        done = not recs or not nxt or seen >= total
        return Page(total, recs, None if done else {"cursor": nxt, "seen": seen, "view": view}, notes)


def parse_scopus(e: dict) -> dict:
    sid = (e.get("dc:identifier") or "").replace("SCOPUS_ID:", "")
    rec = empty_record("scopus", sid)
    rec["raw"] = e
    rec["title"] = e.get("dc:title")
    rec["abstract"] = e.get("dc:description")
    rec["authors"] = [a.get("authname") for a in e.get("author", [])] or ([e["dc:creator"]] if e.get("dc:creator") else [])
    cd = e.get("prism:coverDate")
    rec["pub_date"] = cd
    rec["year"] = int(cd[:4]) if cd else None
    rec["journal"] = e.get("prism:publicationName")
    rec["volume"], rec["issue"], rec["pages"] = e.get("prism:volume"), e.get("prism:issueIdentifier"), e.get(
        "prism:pageRange")
    rec["doi"] = norm_doi(e.get("prism:doi"))
    rec["pmid"] = norm_pmid(e.get("pubmed-id"))
    rec["pub_types"] = [e.get("subtypeDescription") or "", e.get("prism:aggregationType") or ""]
    rec["keywords"] = [k.strip() for k in (e.get("authkeywords") or "").split("|") if k.strip()]
    rec["report_kind"] = kind_from_types(rec["pub_types"])
    rec["other_ids"] = {"eid": e.get("eid")}
    rec["url"] = next((l.get("@href") for l in e.get("link", []) if l.get("@ref") == "scopus"), None)
    return rec


class ScienceDirectAdapter(_Elsevier):
    name = "sciencedirect"
    platform = "ScienceDirect Search API v2"
    docs_url = "https://dev.elsevier.com/documentation/ScienceDirectSearchAPI.wadl"
    access = "Elsevier API key + institutional ScienceDirect entitlement. Offset pagination capped at 6,000."
    page_size = 100

    def test_connection(self) -> dict:
        self._require()
        r = request("PUT", ELS + "search/sciencedirect", source=self.name, per_sec=2, headers=self._headers(),
                    json={"title": "ANGPTL3", "display": {"show": 1}}, expect_json=True)
        return {"ok": True, "detail": f"ScienceDirect reachable; resultsFound={r.json().get('resultsFound')}"}

    def search(self, compiled, cursor, on_retry=None) -> Page:
        self._require()
        cur = cursor or {"branch": 0, "offset": 0, "totals": {}}
        req = dict(compiled.requests[cur["branch"]])
        body = {k: v for k, v in req.items()}
        body["display"] = {"offset": cur["offset"], "show": self.page_size, "sortBy": "date"}
        r = request("PUT", ELS + "search/sciencedirect", source=self.name, per_sec=2, headers=self._headers(),
                    json=body, on_retry=on_retry, expect_json=True)
        d = r.json()
        total = int(d.get("resultsFound", 0))
        cur["totals"][str(cur["branch"])] = total
        recs = [parse_sd(x) for x in d.get("results", [])]
        notes = []
        cur["offset"] += self.page_size
        if cur["offset"] >= min(total, 6000) or not recs:
            if total > 6000:
                notes.append("ALERT: ScienceDirect offset cap (6,000) reached; pagination incomplete.")
            if cur["branch"] + 1 < len(compiled.requests):
                cur = {"branch": cur["branch"] + 1, "offset": 0, "totals": cur["totals"]}
            else:
                cur = None
        return Page(sum((cursor or {"totals": {}})["totals"].values()) if cursor else total, recs, cur, notes)


def parse_sd(x: dict) -> dict:
    rec = empty_record("sciencedirect", x.get("pii") or x.get("doi"))
    rec["raw"] = x
    rec["title"] = x.get("title")
    rec["authors"] = [a.get("name") for a in x.get("authors") or []]
    pd = x.get("publicationDate")
    rec["pub_date"], rec["year"] = pd, int(pd[:4]) if pd else None
    rec["journal"] = x.get("sourceTitle")
    rec["doi"] = norm_doi(x.get("doi"))
    rec["other_ids"] = {"pii": x.get("pii"), "openaccess": x.get("openAccess")}
    rec["is_oa"] = 1 if x.get("openAccess") else 0
    rec["url"] = x.get("uri")
    return rec


class EmbaseAdapter(_Elsevier):
    name = "embase"
    platform = "Embase (Elsevier)"
    docs_url = "https://dev.elsevier.com/"
    access = ("Embase programmatic search requires an institutional Embase API entitlement negotiated with "
              "Elsevier; most institutions access Embase only via embase.com or Ovid. Default route: run the "
              "compiled Embase.com query manually and import the RIS export.")

    def test_connection(self) -> dict:
        self._require()
        raise SourceError("Embase API endpoint is entitlement-specific and not publicly documented; this "
                          "connector cannot be verified without institutional API documentation. Use manual "
                          "export + import.", "access")

    def search(self, compiled, cursor, on_retry=None) -> Page:
        raise SourceError("Embase automated search is not available; run the native query on embase.com/Ovid and "
                          "import the export.", "access")


class WoSAdapter(Adapter):
    name = "wos"
    platform = "Web of Science Starter API"
    credentials = ["api_key"]
    docs_url = "https://developer.clarivate.com/apis/wos-starter"
    access = "Clarivate developer-portal API key (Starter API; free trial or institutional plan)."
    page_size = 50

    def _headers(self):
        return {"X-ApiKey": self.cred("api_key") or ""}

    def test_connection(self) -> dict:
        if not self.configured():
            raise SourceError("No WoS API key configured", "auth")
        r = request("GET", "https://api.clarivate.com/apis/wos-starter/v1/documents", source=self.name, per_sec=2,
                    headers=self._headers(), params={"q": "TI=(ANGPTL3)", "db": "WOS", "limit": 1}, expect_json=True)
        return {"ok": True, "detail": f"WoS Starter reachable; total={r.json().get('metadata', {}).get('total')}"}

    def search(self, compiled, cursor, on_retry=None) -> Page:
        if not self.configured():
            raise SourceError("No WoS API key configured", "auth")
        page = (cursor or {}).get("page", 1)
        req = compiled.requests[0]
        r = request("GET", "https://api.clarivate.com/apis/wos-starter/v1/documents", source=self.name, per_sec=2,
                    headers=self._headers(), params={**req, "limit": self.page_size, "page": page},
                    on_retry=on_retry, expect_json=True)
        d = r.json()
        total = int(d.get("metadata", {}).get("total", 0))
        recs = [parse_wos(h) for h in d.get("hits", [])]
        done = not recs or page * self.page_size >= total
        return Page(total, recs, None if done else {"page": page + 1})


def parse_wos(h: dict) -> dict:
    rec = empty_record("wos", h.get("uid"))
    rec["raw"] = h
    rec["title"] = h.get("title")
    src = h.get("source") or {}
    rec["journal"] = src.get("sourceTitle")
    rec["year"] = src.get("publishYear")
    rec["volume"], rec["issue"] = src.get("volume"), src.get("issue")
    rec["authors"] = [a.get("displayName") for a in (h.get("names") or {}).get("authors", [])]
    ids = h.get("identifiers") or {}
    rec["doi"] = norm_doi(ids.get("doi"))
    rec["pmid"] = norm_pmid(ids.get("pmid"))
    rec["pub_types"] = h.get("types") or []
    rec["report_kind"] = kind_from_types(rec["pub_types"])
    rec["keywords"] = (h.get("keywords") or {}).get("authorKeywords") or []
    rec["url"] = (h.get("links") or {}).get("record")
    return rec
