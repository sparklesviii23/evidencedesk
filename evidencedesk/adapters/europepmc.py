"""Europe PMC REST API: search (cursorMark pagination), abstracts, OA full-text XML and supplementary files."""
from __future__ import annotations

from ..http import SourceError, request
from .base import Adapter, Page, empty_record, find_ncts, kind_from_types, norm_doi, norm_pmcid

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/"


class EuropePMCAdapter(Adapter):
    name = "europepmc"
    platform = "Europe PMC REST"
    docs_url = "https://europepmc.org/RestfulWebService"
    access = "Public, no credentials."
    verified_live = True
    page_size = 1000

    def connection_status(self) -> dict:
        return {"status": "public", "detail": "No credentials required"}

    def test_connection(self) -> dict:
        r = request("GET", BASE + "search", source=self.name, per_sec=8,
                    params={"query": "ANGPTL3", "format": "json", "pageSize": 1}, expect_json=True)
        return {"ok": True, "detail": f"Europe PMC reachable; ANGPTL3 hits={r.json().get('hitCount')}"}

    def search(self, compiled, cursor, on_retry=None) -> Page:
        mark = (cursor or {}).get("cursorMark", "*")
        params = {"query": compiled.requests[0]["query"], "format": "json", "resultType": "core",
                  "pageSize": self.page_size, "cursorMark": mark}
        r = request("GET", BASE + "search", source=self.name, per_sec=8, params=params, on_retry=on_retry,
                    expect_json=True)
        d = r.json()
        if "hitCount" not in d:
            raise SourceError(f"Europe PMC error: {str(d)[:300]}", "bad_query")
        total = int(d["hitCount"])
        results = d.get("resultList", {}).get("result", [])
        recs = [parse_epmc(x) for x in results]
        nxt = d.get("nextCursorMark")
        seen = (cursor or {}).get("seen", 0) + len(recs)
        done = not results or not nxt or nxt == mark or seen >= total
        return Page(total, recs, None if done else {"cursorMark": nxt, "seen": seen})

    def fulltext_xml(self, pmcid: str) -> bytes:
        r = request("GET", f"{BASE}{pmcid}/fullTextXML", source=self.name, per_sec=8)
        return r.content

    def supplementary_zip(self, pmcid: str) -> bytes:
        r = request("GET", f"{BASE}{pmcid}/supplementaryFiles", source=self.name, per_sec=8)
        return r.content

    def lookup(self, query: str) -> list[dict]:
        r = request("GET", BASE + "search", source=self.name, per_sec=8,
                    params={"query": query, "format": "json", "resultType": "core", "pageSize": 25},
                    expect_json=True)
        return [parse_epmc(x) for x in r.json().get("resultList", {}).get("result", [])]


def parse_epmc(x: dict) -> dict:
    src = x.get("source", "")
    rec = empty_record("europepmc", f"{src}:{x.get('id')}")
    rec["raw"] = x
    rec["title"] = (x.get("title") or "").strip().rstrip(".") or None
    rec["abstract"] = x.get("abstractText")
    if rec["abstract"]:
        import re
        rec["abstract"] = re.sub(r"<[^>]+>", " ", rec["abstract"])
        rec["abstract"] = " ".join(rec["abstract"].split())
    al = (x.get("authorList") or {}).get("author") or []
    rec["authors"] = [a.get("fullName") or a.get("collectiveName") for a in al if a.get("fullName") or
                      a.get("collectiveName")] or ([s.strip() for s in x["authorString"].split(",")]
                                                    if x.get("authorString") else [])
    y = x.get("pubYear")
    rec["year"] = int(y) if y and str(y).isdigit() else None
    rec["pub_date"] = x.get("firstPublicationDate")
    ji = x.get("journalInfo") or {}
    rec["journal"] = (ji.get("journal") or {}).get("title") or (x.get("bookOrReportDetails") or {}).get("publisher")
    rec["volume"], rec["issue"] = ji.get("volume"), ji.get("issue")
    rec["pages"] = x.get("pageInfo")
    rec["doi"] = norm_doi(x.get("doi"))
    rec["pmid"] = x.get("pmid")
    rec["pmcid"] = norm_pmcid(x.get("pmcid"))
    rec["pub_types"] = (x.get("pubTypeList") or {}).get("pubType") or []
    rec["keywords"] = (x.get("keywordList") or {}).get("keyword") or []
    rec["mesh"] = [m.get("descriptorName") for m in (x.get("meshHeadingList") or {}).get("meshHeading", [])
                   if m.get("descriptorName")]
    rec["language"] = x.get("language")
    rec["is_oa"] = 1 if x.get("isOpenAccess") == "Y" else 0
    rec["other_ids"] = {k: x[k] for k in ("id", "source") if x.get(k)}
    if x.get("fullTextIdList"):
        rec["other_ids"]["fullTextIds"] = x["fullTextIdList"].get("fullTextId")
    rec["other_ids"]["inEPMC"] = x.get("inEPMC")
    rec["other_ids"]["hasPDF"] = x.get("hasPDF")
    rec["other_ids"]["hasSuppl"] = x.get("hasSuppl")
    urls = (x.get("fullTextUrlList") or {}).get("fullTextUrl") or []
    rec["other_ids"]["fullTextUrls"] = [{"url": u.get("url"), "style": u.get("documentStyle"),
                                         "availability": u.get("availabilityCode"), "site": u.get("site")}
                                        for u in urls]
    rec["nct_ids"] = find_ncts(rec["abstract"], rec["title"])
    rec["report_kind"] = "preprint" if src == "PPR" else kind_from_types(rec["pub_types"])
    if src == "PAT":
        rec["report_kind"] = "other"
    rec["url"] = f"https://europepmc.org/article/{src}/{x.get('id')}"
    return rec
