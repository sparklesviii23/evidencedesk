"""ClinicalTrials.gov API v2: registry records incl. posted results (resultsSection)."""
from __future__ import annotations

from ..http import SourceError, request
from .base import Adapter, Page, empty_record

BASE = "https://clinicaltrials.gov/api/v2/"


class CTGovAdapter(Adapter):
    name = "ctgov"
    platform = "ClinicalTrials.gov API v2"
    register = True
    docs_url = "https://clinicaltrials.gov/data-api/api"
    access = "Public, no credentials."
    verified_live = True
    page_size = 200

    def connection_status(self) -> dict:
        return {"status": "public", "detail": "No credentials required"}

    def test_connection(self) -> dict:
        r = request("GET", BASE + "version", source=self.name, per_sec=5, expect_json=True)
        return {"ok": True, "detail": f"ClinicalTrials.gov API {r.json().get('apiVersion')}, data "
                                      f"{r.json().get('dataTimestamp')}"}

    def search(self, compiled, cursor, on_retry=None) -> Page:
        req = compiled.requests[0]
        params = {**req, "pageSize": self.page_size, "countTotal": "true", "format": "json"}
        if cursor and cursor.get("pageToken"):
            params["pageToken"] = cursor["pageToken"]
        r = request("GET", BASE + "studies", source=self.name, per_sec=5, params=params, on_retry=on_retry,
                    expect_json=True)
        d = r.json()
        if "studies" not in d:
            raise SourceError(f"ClinicalTrials.gov error: {str(d)[:300]}", "bad_query")
        total = d.get("totalCount", (cursor or {}).get("total"))
        recs = [parse_study(s) for s in d["studies"]]
        tok = d.get("nextPageToken")
        return Page(total, recs, {"pageToken": tok, "total": total} if tok else None)

    def study(self, nct: str) -> dict | None:
        try:
            r = request("GET", BASE + f"studies/{nct}", source=self.name, per_sec=5, expect_json=True)
        except SourceError as e:
            if e.kind == "not_found":
                return None
            raise
        return parse_study(r.json())


def parse_study(s: dict) -> dict:
    ps = s.get("protocolSection", {})
    ident = ps.get("identificationModule", {})
    nct = ident.get("nctId")
    rec = empty_record("ctgov", nct)
    rec["raw"] = s
    rec["title"] = ident.get("officialTitle") or ident.get("briefTitle")
    desc = ps.get("descriptionModule", {})
    rec["abstract"] = desc.get("briefSummary")
    status = ps.get("statusModule", {})
    first = (status.get("studyFirstPostDateStruct") or {}).get("date")
    rec["pub_date"] = first
    rec["year"] = int(first[:4]) if first else None
    spons = (ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor") or {}).get("name")
    rec["authors"] = [spons] if spons else []
    rec["journal"] = "ClinicalTrials.gov"
    rec["nct_ids"] = [nct] + [x.get("id") for x in ident.get("secondaryIdInfos", []) if
                              (x.get("id") or "").upper().startswith("NCT")]
    design = ps.get("designModule", {})
    rec["pub_types"] = ["Registry record", design.get("studyType") or ""] + (design.get("phases") or [])
    rec["keywords"] = ps.get("conditionsModule", {}).get("keywords", [])
    cond = ps.get("conditionsModule", {}).get("conditions", [])
    interventions = [i.get("name") for i in ps.get("armsInterventionsModule", {}).get("interventions", [])]
    refs = ps.get("referencesModule", {}).get("references", [])
    rec["other_ids"] = {"org_study_id": (ident.get("orgStudyIdInfo") or {}).get("id"),
                        "secondary_ids": [x.get("id") for x in ident.get("secondaryIdInfos", [])],
                        "overall_status": status.get("overallStatus"), "has_results": s.get("hasResults", False),
                        "conditions": cond, "interventions": interventions,
                        "enrollment": (design.get("enrollmentInfo") or {}).get("count"),
                        "reference_pmids": [r.get("pmid") for r in refs if r.get("pmid")],
                        "result_reference_pmids": [r.get("pmid") for r in refs
                                                   if r.get("pmid") and r.get("type") == "RESULT"]}
    rec["mesh"] = [m.get("term") for m in (s.get("derivedSection", {}).get("conditionBrowseModule", {})
                                           .get("meshes", []))] + \
                  [m.get("term") for m in (s.get("derivedSection", {}).get("interventionBrowseModule", {})
                                           .get("meshes", []))]
    rec["report_kind"] = "registry"
    rec["is_oa"] = 1
    rec["url"] = f"https://clinicaltrials.gov/study/{nct}"
    return rec
