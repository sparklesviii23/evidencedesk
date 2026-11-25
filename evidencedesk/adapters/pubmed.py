"""PubMed via NCBI E-utilities (esearch + efetch, history server).

E-utilities returns at most 10,000 records per search; larger result sets are split into publication-date
slices automatically so pagination stays complete.
"""
from __future__ import annotations

from lxml import etree

from ..credentials import get
from ..http import SourceError, request
from .base import Adapter, Page, empty_record, find_ncts, kind_from_types, norm_doi, norm_pmcid

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
MAX_PER_SEARCH = 9999


class PubMedAdapter(Adapter):
    name = "pubmed"
    platform = "PubMed (NCBI E-utilities)"
    optional_credentials = ["api_key"]
    docs_url = "https://www.ncbi.nlm.nih.gov/books/NBK25499/"
    access = "Public. Optional free NCBI API key raises the rate limit to 10 requests/s."
    verified_live = True
    page_size = 200

    def _params(self, **kw) -> dict:
        p = {"db": "pubmed", "tool": "evidencedesk", **kw}
        email = get("contact", "email")
        if email:
            p["email"] = email
        key = self.cred("api_key")
        if key:
            p["api_key"] = key
        return p

    @property
    def rate(self) -> float:
        return 9.0 if self.cred("api_key") else 2.8

    def connection_status(self) -> dict:
        return {"status": "public", "detail": "No credentials required" +
                ("; NCBI API key configured" if self.cred("api_key") else "; no API key (3 req/s)")}

    def test_connection(self) -> dict:
        r = request("GET", BASE + "einfo.fcgi", source=self.name, per_sec=self.rate,
                    params=self._params(retmode="json"))
        d = r.json()
        return {"ok": True, "detail": f"PubMed reachable; last update {d['einforesult']['dbinfo'][0].get('lastupdate')}"}

    def esearch(self, term: str, retmax=0, history=True, on_retry=None) -> dict:
        data = self._params(term=term, retmode="json", retmax=retmax, usehistory="y" if history else "n")
        r = request("POST", BASE + "esearch.fcgi", source=self.name, per_sec=self.rate, data=data,
                    on_retry=on_retry, expect_json=True)
        res = r.json().get("esearchresult", {})
        if "ERROR" in res:
            raise SourceError(f"PubMed rejected the query: {res.get('ERROR') or res.get('errorlist')}", "bad_query")
        warnings = res.get("warninglist") or {}
        return {"count": int(res.get("count", 0)), "webenv": res.get("webenv"), "query_key": res.get("querykey"),
                "ids": res.get("idlist", []), "querytranslation": res.get("querytranslation"),
                "warnings": warnings, "errors": res.get("errorlist") or {}}

    def _slice(self, term: str, lo: int, hi: int, on_retry) -> list[dict]:
        t = f'({term}) AND ("{lo}/01/01"[dp] : "{hi}/12/31"[dp])'
        res = self.esearch(t, on_retry=on_retry)
        if res["count"] <= MAX_PER_SEARCH or lo == hi:
            return [{"term": t, "count": res["count"], "webenv": res["webenv"], "query_key": res["query_key"],
                     "retstart": 0, "capped": res["count"] > MAX_PER_SEARCH}]
        mid = (lo + hi) // 2
        return self._slice(term, lo, mid, on_retry) + self._slice(term, mid + 1, hi, on_retry)

    def search(self, compiled, cursor, on_retry=None) -> Page:
        notes = []
        term = compiled.requests[0]["term"]
        if cursor is None:
            res = self.esearch(term, on_retry=on_retry)
            errs = res["errors"]
            if errs.get("phrasesnotfound") or errs.get("fieldsnotfound"):
                notes.append(f"ALERT: PubMed reported these terms as not found and ignored them, so it executed "
                             f"a modified query (inside AND this widens results): {errs}")
            w = res["warnings"] or {}
            if w.get("phrasesignored"):
                notes.append(f"ALERT: PubMed ignored these terms (stopwords/unsearchable), so it executed a "
                             f"modified query: {w['phrasesignored']}")
            if w.get("quotedphrasesnotfound"):
                notes.append(f"Not in PubMed's phrase index (contributes 0 records; PubMed did not widen the "
                             f"query): {w['quotedphrasesnotfound']}")
            if res["count"] > MAX_PER_SEARCH:
                import datetime
                slices = self._slice(term, 1800, datetime.date.today().year + 1, on_retry)
                notes.append(f"{res['count']} hits exceed the E-utilities 10,000 cap; split into "
                             f"{len(slices)} publication-date slices.")
            else:
                slices = [{"term": term, "count": res["count"], "webenv": res["webenv"],
                           "query_key": res["query_key"], "retstart": 0, "capped": False}]
            cursor = {"total": res["count"], "slices": slices, "i": 0,
                      "querytranslation": res["querytranslation"]}
            if res["querytranslation"]:
                notes.append(f"PubMed query translation: {res['querytranslation']}")
        total = cursor["total"]
        while cursor["i"] < len(cursor["slices"]) and \
                cursor["slices"][cursor["i"]]["retstart"] >= min(cursor["slices"][cursor["i"]]["count"],
                                                                 MAX_PER_SEARCH):
            cursor["i"] += 1
        if cursor["i"] >= len(cursor["slices"]):
            return Page(total, [], None, notes)
        sl = cursor["slices"][cursor["i"]]
        try:
            xml = self._efetch(sl, on_retry)
        except SourceError as e:
            if e.kind in ("bad_query", "not_found", "error"):
                # history server session expired (common on resume) -> re-establish and continue
                res = self.esearch(sl["term"], on_retry=on_retry)
                if res["count"] != sl["count"]:
                    notes.append(f"Result set re-established on resume; slice count changed "
                                 f"{sl['count']} -> {res['count']}.")
                sl.update(webenv=res["webenv"], query_key=res["query_key"], count=res["count"])
                xml = self._efetch(sl, on_retry)
            else:
                raise
        records = parse_pubmed_xml(xml)
        sl["retstart"] += self.page_size
        if not records and sl["retstart"] < sl["count"]:
            raise SourceError(f"PubMed efetch returned no records at retstart {sl['retstart'] - self.page_size}",
                              "error", retryable=True)
        if sl.get("capped"):
            notes.append("A single-year slice still exceeds 10,000 records; pagination incomplete.")
        done = cursor["i"] == len(cursor["slices"]) - 1 and sl["retstart"] >= min(sl["count"], MAX_PER_SEARCH)
        return Page(total, records, None if done else cursor, notes)

    def _efetch(self, sl: dict, on_retry) -> bytes:
        data = self._params(WebEnv=sl["webenv"], query_key=sl["query_key"], retstart=sl["retstart"],
                            retmax=self.page_size, retmode="xml")
        r = request("POST", BASE + "efetch.fcgi", source=self.name, per_sec=self.rate, data=data,
                    on_retry=on_retry)
        if b"<ERROR>" in r.content[:500]:
            raise SourceError(f"efetch error: {r.text[:300]}", "error")
        return r.content

    def fetch_pmids(self, pmids: list[str], on_retry=None) -> list[dict]:
        out = []
        for i in range(0, len(pmids), 200):
            data = self._params(id=",".join(pmids[i:i + 200]), retmode="xml")
            r = request("POST", BASE + "efetch.fcgi", source=self.name, per_sec=self.rate, data=data,
                        on_retry=on_retry)
            out += parse_pubmed_xml(r.content)
        return out


def _text(el) -> str | None:
    if el is None:
        return None
    s = "".join(el.itertext()).strip()
    return " ".join(s.split()) or None


def parse_pubmed_xml(xml: bytes) -> list[dict]:
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(xml, parser)
    if root is None:
        return []
    out = []
    for art in root.iter("PubmedArticle", "PubmedBookArticle"):
        mc = art.find("MedlineCitation")
        pmid = None
        if mc is not None:
            pmid = _text(mc.find("PMID"))
        else:
            pmid = _text(art.find(".//BookDocument/PMID"))
        rec = empty_record("pubmed", pmid)
        rec["pmid"] = pmid
        rec["raw_format"] = "pubmed_xml"
        rec["raw"] = etree.tostring(art, encoding="unicode")
        a = mc.find("Article") if mc is not None else art.find(".//BookDocument")
        if a is not None:
            rec["title"] = _text(a.find("ArticleTitle")) or _text(a.find("VernacularTitle")) or \
                _text(a.find(".//BookTitle"))
            parts = []
            for at in a.findall(".//Abstract/AbstractText"):
                lab = at.get("Label")
                t = _text(at)
                if t:
                    parts.append(f"{lab}: {t}" if lab else t)
            rec["abstract"] = "\n".join(parts) or None
            authors = []
            for au in a.findall(".//AuthorList/Author"):
                ln, fn, coll = _text(au.find("LastName")), _text(au.find("Initials")) or _text(
                    au.find("ForeName")), _text(au.find("CollectiveName"))
                if ln:
                    authors.append(f"{ln} {fn}" if fn else ln)
                elif coll:
                    authors.append(coll)
            rec["authors"] = authors
            j = a.find("Journal")
            if j is not None:
                rec["journal"] = _text(j.find("Title")) or _text(j.find("ISOAbbreviation"))
                ji = j.find("JournalIssue")
                if ji is not None:
                    rec["volume"] = _text(ji.find("Volume"))
                    rec["issue"] = _text(ji.find("Issue"))
                    pd = ji.find("PubDate")
                    y = _text(pd.find("Year")) if pd is not None else None
                    if not y and pd is not None:
                        md = _text(pd.find("MedlineDate")) or ""
                        y = md[:4] if md[:4].isdigit() else None
                    rec["year"] = int(y) if y and y.isdigit() else None
                    if pd is not None:
                        mo = _text(pd.find("Month"))
                        rec["pub_date"] = " ".join(x for x in [y, mo, _text(pd.find("Day"))] if x)
            rec["pages"] = _text(a.find(".//Pagination/MedlinePgn"))
            for eid in a.findall("ELocationID"):
                if eid.get("EIdType") == "doi":
                    rec["doi"] = norm_doi(_text(eid))
            rec["pub_types"] = [_text(p) for p in a.findall(".//PublicationTypeList/PublicationType") if _text(p)]
            rec["language"] = _text(a.find("Language"))
            for db in a.findall(".//DataBankList/DataBank"):
                if (_text(db.find("DataBankName")) or "").lower().startswith("clinicaltrials"):
                    rec["nct_ids"] += [_text(x).upper() for x in db.findall(".//AccessionNumber") if _text(x)]
        if mc is not None:
            rec["mesh"] = [_text(d) for d in mc.findall(".//MeshHeadingList/MeshHeading/DescriptorName") if _text(d)]
            rec["keywords"] = [_text(k) for k in mc.findall(".//KeywordList/Keyword") if _text(k)]
        for aid in art.findall(".//PubmedData/ArticleIdList/ArticleId"):
            t = aid.get("IdType")
            v = _text(aid)
            if t == "doi" and not rec["doi"]:
                rec["doi"] = norm_doi(v)
            elif t == "pmc":
                rec["pmcid"] = norm_pmcid(v)
            elif t and v and t not in ("pubmed", "doi"):
                rec["other_ids"][t] = v
        rec["nct_ids"] = list(dict.fromkeys(rec["nct_ids"] + find_ncts(rec["abstract"], rec["title"])))
        rec["report_kind"] = kind_from_types(rec["pub_types"])
        rec["url"] = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        out.append(rec)
    return out
