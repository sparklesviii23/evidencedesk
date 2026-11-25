"""Import native database exports: RIS, BibTeX, CSV, PubMed/MEDLINE (.nbib/.txt), PubMed XML, EndNote XML.

Imported records keep the platform, native query, search date and file hash as provenance, are stored under
source 'import:<platform>' and go through the same dedup/screening workflow as API results.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re

from lxml import etree

from .adapters.base import empty_record, find_ncts, kind_from_types, norm_doi, norm_pmcid, norm_pmid
from .db import DB, now
from .dedup import Deduper
from .engine import store_record

RIS_TYPES = {"JOUR": "journal_article", "ABST": "conference_abstract", "CONF": "conference_abstract",
             "CPAPER": "conference_abstract", "THES": "thesis", "BOOK": "book", "CHAP": "book", "DATA": "dataset",
             "UNPB": "preprint", "GEN": "other", "ELEC": "other", "RPRT": "other", "CTLG": "other"}


def detect_format(name: str, text: str) -> str:
    n = name.lower()
    head = text.lstrip()[:2000]
    if n.endswith((".ris", ".txt")) and re.search(r"^TY  - ", head, re.M):
        return "ris"
    if re.search(r"^PMID- ", head, re.M):
        return "medline"
    if n.endswith(".bib") or head.startswith("@"):
        return "bibtex"
    if head.startswith("<?xml") or head.startswith("<"):
        if "<PubmedArticle" in text[:20000] or "<PubmedArticleSet" in text[:20000]:
            return "pubmed_xml"
        if "<records>" in text[:20000] or "<xml>" in text[:200]:
            return "endnote_xml"
        return "xml"
    if n.endswith((".csv", ".tsv")):
        return "csv"
    if re.search(r"^TY  - ", head, re.M):
        return "ris"
    raise ValueError("Unrecognised export format. Supported: RIS, MEDLINE/nbib, BibTeX, CSV, PubMed XML, EndNote XML")


def parse_ris(text: str, platform: str) -> list[dict]:
    out, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])  -(?: (.*))?$", line)
        if not m:
            if cur is not None and line.strip() and cur.get("_last"):
                cur[cur["_last"]][-1] += " " + line.strip()
            continue
        tag, val = m.group(1), (m.group(2) or "").strip()
        if tag == "TY":
            cur = {"TY": [val]}
            continue
        if cur is None:
            continue
        if tag == "ER":
            out.append(cur)
            cur = None
            continue
        cur.setdefault(tag, []).append(val)
        cur["_last"] = tag
    return [_ris_to_rec(r, platform, i) for i, r in enumerate(out)]


def _first(r, *tags):
    for t in tags:
        if r.get(t):
            return r[t][0]
    return None


def _ris_to_rec(r: dict, platform: str, i: int) -> dict:
    an = _first(r, "AN", "ID", "U1", "L2") or f"row{i + 1}"
    rec = empty_record(f"import:{platform}", an)
    rec["raw_format"] = "ris"
    rec["raw"] = {k: v for k, v in r.items() if k != "_last"}
    rec["title"] = _first(r, "TI", "T1", "CT", "BT")
    rec["abstract"] = "\n".join(r.get("AB", []) + r.get("N2", [])) or None
    rec["authors"] = r.get("AU", []) + r.get("A1", [])
    y = _first(r, "PY", "Y1", "DA")
    m = re.search(r"(1[89]\d\d|20\d\d)", y or "")
    rec["year"] = int(m.group(1)) if m else None
    rec["pub_date"] = _first(r, "DA", "PY", "Y1")
    rec["journal"] = _first(r, "JO", "T2", "JF", "JA", "J2")
    rec["volume"], rec["issue"] = _first(r, "VL"), _first(r, "IS")
    sp, ep = _first(r, "SP"), _first(r, "EP")
    rec["pages"] = f"{sp}-{ep}" if sp and ep else sp
    alltext = " ".join(sum((v for k, v in r.items() if k != "_last" and isinstance(v, list)), []))
    rec["doi"] = norm_doi(_first(r, "DO", "DI")) or norm_doi(" ".join(r.get("UR", []) + r.get("L3", [])))
    pm = None
    for t in ("PM", "AN", "C2"):
        for v in r.get(t, []):
            mm = re.search(r"(?:PMID[:\s]*)?(\d{6,9})\b", v)
            if mm and (t == "PM" or "pmid" in v.lower() or platform.lower() in ("ovid_medline", "medline", "pubmed")):
                pm = mm.group(1)
    rec["pmid"] = norm_pmid(pm)
    rec["pmcid"] = norm_pmcid(next((x for x in re.findall(r"PMC\d+", alltext)), None))
    rec["keywords"] = r.get("KW", [])
    rec["pub_types"] = [r["TY"][0]] + r.get("M3", [])
    rec["report_kind"] = kind_from_types(r.get("M3", []), RIS_TYPES.get(r["TY"][0], "journal_article"))
    rec["nct_ids"] = find_ncts(alltext)
    if rec["report_kind"] == "journal_article" and rec["nct_ids"] and platform.lower() in (
            "central", "cochrane_central") and "clinicaltrials.gov" in alltext.lower() and not rec["doi"]:
        rec["report_kind"] = "registry"
    rec["language"] = _first(r, "LA")
    rec["url"] = _first(r, "UR", "L1")
    rec["other_ids"] = {"accession": an, "database": _first(r, "DB", "DP")}
    return rec


def parse_medline(text: str, platform: str) -> list[dict]:
    recs = []
    for block in re.split(r"\n\s*\n(?=PMID- )", text.strip()):
        fields: dict[str, list[str]] = {}
        last = None
        for line in block.splitlines():
            m = re.match(r"^([A-Z]{2,4})\s*- (.*)$", line)
            if m:
                last = m.group(1)
                fields.setdefault(last, []).append(m.group(2).strip())
            elif last and line.startswith("      "):
                fields[last][-1] += " " + line.strip()
        if "PMID" not in fields:
            continue
        pmid = fields["PMID"][0]
        rec = empty_record(f"import:{platform}", pmid)
        rec["raw_format"], rec["raw"] = "medline", fields
        rec["pmid"] = pmid
        rec["title"] = (fields.get("TI") or [None])[0]
        rec["abstract"] = (fields.get("AB") or [None])[0]
        rec["authors"] = fields.get("AU", [])
        dp = (fields.get("DP") or [""])[0]
        rec["year"] = int(dp[:4]) if dp[:4].isdigit() else None
        rec["pub_date"] = dp
        rec["journal"] = (fields.get("JT") or fields.get("TA") or [None])[0]
        rec["volume"], rec["issue"], rec["pages"] = [(fields.get(k) or [None])[0] for k in ("VI", "IP", "PG")]
        for aid in fields.get("AID", []) + fields.get("LID", []):
            if "[doi]" in aid:
                rec["doi"] = norm_doi(aid.replace("[doi]", ""))
        pmc = (fields.get("PMC") or [None])[0]
        rec["pmcid"] = norm_pmcid(pmc)
        rec["pub_types"] = fields.get("PT", [])
        rec["mesh"] = [m.split("/")[0].lstrip("*") for m in fields.get("MH", [])]
        rec["keywords"] = fields.get("OT", [])
        rec["nct_ids"] = find_ncts(" ".join(fields.get("SI", [])), rec["abstract"])
        rec["report_kind"] = kind_from_types(rec["pub_types"])
        rec["language"] = (fields.get("LA") or [None])[0]
        recs.append(rec)
    return recs


def parse_bibtex(text: str, platform: str) -> list[dict]:
    out = []
    for i, m in enumerate(re.finditer(r"@(\w+)\s*\{\s*([^,]*),", text)):
        start = m.end()
        depth, j = 1, start
        while j < len(text) and depth:
            depth += text[j] == "{"
            depth -= text[j] == "}"
            j += 1
        body = text[start:j - 1]
        fields = {}
        for fm in re.finditer(r"(\w+)\s*=\s*", body):
            k = fm.group(1).lower()
            p = fm.end()
            if p < len(body) and body[p] == "{":
                d, q = 1, p + 1
                while q < len(body) and d:
                    d += body[q] == "{"
                    d -= body[q] == "}"
                    q += 1
                v = body[p + 1:q - 1]
            elif p < len(body) and body[p] == '"':
                q = body.find('"', p + 1)
                v = body[p + 1:q]
            else:
                q = re.search(r"[,}\n]", body[p:])
                v = body[p:p + (q.start() if q else len(body))]
            fields.setdefault(k, re.sub(r"[{}]", "", " ".join(v.split())))
        etype = m.group(1).lower()
        key = m.group(2).strip() or f"entry{i + 1}"
        rec = empty_record(f"import:{platform}", key)
        rec["raw_format"], rec["raw"] = "bibtex", {"type": etype, **fields}
        rec["title"] = fields.get("title")
        rec["abstract"] = fields.get("abstract")
        rec["authors"] = [a.strip() for a in fields.get("author", "").split(" and ") if a.strip()]
        y = re.search(r"\d{4}", fields.get("year", ""))
        rec["year"] = int(y.group(0)) if y else None
        rec["journal"] = fields.get("journal") or fields.get("booktitle")
        rec["volume"], rec["issue"], rec["pages"] = fields.get("volume"), fields.get("number"), fields.get("pages")
        rec["doi"] = norm_doi(fields.get("doi"))
        rec["pmid"] = norm_pmid(fields.get("pmid"))
        rec["pmcid"] = norm_pmcid(fields.get("pmcid"))
        rec["keywords"] = [k.strip() for k in re.split(r"[;,]", fields.get("keywords", "")) if k.strip()]
        rec["report_kind"] = {"inproceedings": "conference_abstract", "conference": "conference_abstract",
                              "phdthesis": "thesis", "mastersthesis": "thesis", "book": "book",
                              "unpublished": "preprint", "misc": "other"}.get(etype, "journal_article")
        rec["nct_ids"] = find_ncts(rec["abstract"], fields.get("note"))
        out.append(rec)
    return out


CSV_MAP = {
    "title": ["title", "article title", "ti", "document title"],
    "abstract": ["abstract", "ab"],
    "authors": ["authors", "author", "au", "author full names"],
    "year": ["year", "publication year", "py", "pubyear", "publication_year"],
    "journal": ["journal", "source title", "source", "journal/book", "so", "publication title"],
    "doi": ["doi", "di"],
    "pmid": ["pmid", "pubmed id", "pubmed_id", "pm"],
    "pmcid": ["pmcid", "pmc id"],
    "id": ["id", "accession number", "an", "eid", "ut (unique wos id)", "record id", "nct number", "nct_id"],
    "type": ["document type", "type", "publication type", "dt", "study type"],
    "url": ["url", "link"],
}


def parse_csv(text: str, platform: str) -> list[dict]:
    sample = text[:5000]
    dialect = csv.Sniffer().sniff(sample, delimiters=",\t;") if sample else csv.excel
    rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    out = []
    for i, row in enumerate(rows):
        low = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        def g(name):
            for c in CSV_MAP[name]:
                if low.get(c):
                    return low[c]
            return None
        sid = g("id") or g("doi") or g("pmid") or f"row{i + 1}"
        rec = empty_record(f"import:{platform}", sid)
        rec["raw_format"], rec["raw"] = "csv", row
        rec["title"], rec["abstract"] = g("title"), g("abstract")
        au = g("authors") or ""
        rec["authors"] = [a.strip() for a in re.split(r";|\band\b", au) if a.strip()] if ";" in au or " and " in au \
            else ([a.strip() for a in au.split(",")] if au.count(",") > 1 else ([au] if au else []))
        y = re.search(r"\d{4}", g("year") or "")
        rec["year"] = int(y.group(0)) if y else None
        rec["journal"] = g("journal")
        rec["doi"], rec["pmid"], rec["pmcid"] = norm_doi(g("doi")), norm_pmid(g("pmid")), norm_pmcid(g("pmcid"))
        rec["pub_types"] = [g("type") or ""]
        rec["report_kind"] = kind_from_types(rec["pub_types"])
        if sid.upper().startswith("NCT") or (platform.lower() in ("ctgov_csv", "ictrp") and find_ncts(sid)):
            rec["report_kind"] = "registry"
        rec["nct_ids"] = find_ncts(sid, rec["abstract"], " ".join(low.values()))
        rec["url"] = g("url")
        out.append(rec)
    return out


def parse_endnote_xml(text: str, platform: str) -> list[dict]:
    root = etree.fromstring(text.encode(), etree.XMLParser(recover=True, resolve_entities=False, no_network=True))
    out = []
    def t(el, path):
        x = el.find(path)
        return " ".join("".join(x.itertext()).split()) if x is not None else None
    for i, r in enumerate(root.iter("record")):
        sid = t(r, "rec-number") or t(r, "accession-num") or f"rec{i + 1}"
        rec = empty_record(f"import:{platform}", sid)
        rec["raw_format"], rec["raw"] = "endnote_xml", etree.tostring(r, encoding="unicode")
        rec["title"] = t(r, "titles/title")
        rec["abstract"] = t(r, "abstract")
        rec["authors"] = [" ".join("".join(a.itertext()).split()) for a in r.findall("contributors/authors/author")]
        y = t(r, "dates/year")
        rec["year"] = int(y[:4]) if y and y[:4].isdigit() else None
        rec["journal"] = t(r, "titles/secondary-title") or t(r, "periodical/full-title")
        rec["doi"] = norm_doi(t(r, "electronic-resource-num"))
        rec["pmid"] = norm_pmid(t(r, "accession-num")) if (t(r, "remote-database-name") or "").lower().find(
            "pubmed") >= 0 or (t(r, "remote-database-provider") or "").lower().find("nlm") >= 0 else None
        rec["keywords"] = [" ".join("".join(k.itertext()).split()) for k in r.findall("keywords/keyword")]
        rt = r.find("ref-type")
        rec["pub_types"] = [rt.get("name") if rt is not None else ""]
        rec["report_kind"] = kind_from_types(rec["pub_types"])
        rec["nct_ids"] = find_ncts(rec["abstract"])
        out.append(rec)
    return out


def parse_file(name: str, data: bytes, platform: str, fmt: str | None = None) -> tuple[str, list[dict]]:
    text = data.decode("utf-8-sig", errors="replace")
    fmt = fmt or detect_format(name, text)
    if fmt == "ris":
        return fmt, parse_ris(text, platform)
    if fmt == "medline":
        return fmt, parse_medline(text, platform)
    if fmt == "bibtex":
        return fmt, parse_bibtex(text, platform)
    if fmt == "csv":
        return fmt, parse_csv(text, platform)
    if fmt == "pubmed_xml":
        from .adapters.pubmed import parse_pubmed_xml
        recs = parse_pubmed_xml(data)
        for r in recs:
            r["source"] = f"import:{platform}"
        return fmt, recs
    if fmt == "endnote_xml":
        return fmt, parse_endnote_xml(text, platform)
    raise ValueError(f"Unsupported format {fmt}")


def import_file(db: DB, review_id: int, name: str, data: bytes, *, platform: str, native_query: str | None,
                search_date: str | None, fmt: str | None = None, run_id: int | None = None, notes=None,
                actor="user", register=False) -> dict:
    platform = re.sub(r"[^a-z0-9_]+", "_", platform.strip().lower()) or "unknown"
    fmt, recs = parse_file(name, data, platform, fmt)
    sha = hashlib.sha256(data).hexdigest()
    imp = db.insert("imports", {"review_id": review_id, "source": f"import:{platform}", "platform": platform,
                                "file_name": name, "file_format": fmt, "file_sha256": sha, "native_query": native_query,
                                "search_date": search_date, "record_count": len(recs),
                                "notes": json.dumps({"notes": notes, "register": register}), "run_id": run_id,
                                "imported_at": now()})
    # imported search becomes a source_run of the run (if given) so PRISMA-S has the strategy + count
    if run_id:
        db.x("""INSERT OR REPLACE INTO source_runs (run_id, source, platform, native_query, fidelity, approved,
                connector_status, status, reported_hits, retrieved, complete, executed_at, finished_at)
                VALUES (?,?,?,?, 'manual', 1, ?, 'imported', ?, ?, 1, ?, ?)""",
             (run_id, f"import:{platform}", platform, native_query, json.dumps({"status": "import"}), len(recs),
              len(recs), search_date or now(), now()))
    sr = db.one("SELECT id FROM source_runs WHERE run_id=? AND source=?", (run_id, f"import:{platform}")) if run_id \
        else None
    d = Deduper(db, review_id)
    new = 0
    for i, rec in enumerate(recs):
        rid, is_new = store_record(db, review_id, rec, run_id=run_id, source_run_id=sr["id"] if sr else None,
                                   import_id=imp, rank=i + 1)
        if is_new:
            new += 1
            d.attach(rid)
    db.audit(review_id, actor, "import", {"import_id": imp, "file": name, "records": len(recs), "new": new})
    return {"import_id": imp, "format": fmt, "records": len(recs), "new_records": new, "sha256": sha}
