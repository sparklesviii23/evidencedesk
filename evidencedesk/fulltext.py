"""Full-text retrieval and text extraction.

Routes tried per report (every attempt is logged in fulltext_attempts, success or not):
  registry_record        ClinicalTrials.gov record incl. posted results (structured JSON)
  pmc_cloud              PMC Cloud Service (AWS Open Data, pmc-oa-opendata): JATS XML / PDF / media of PMC
                         open-access articles and author manuscripts, with their licence code
  europepmc_xml          Europe PMC OA full-text JATS XML (fallback)
  europepmc_supplements  Europe PMC supplementary-files bundle (zip, preserved as-is + unpacked)
  unpaywall_pdf          Unpaywall best OA location (repository/publisher OA copy)
  openalex_pdf           OpenAlex best_oa_location.pdf_url
  crossref_tdm           Crossref text-mining links, only when the work carries an open (CC) licence
  manual_upload          user-supplied files
Only openly licensed / publicly permitted routes are automated. Paywalled publisher routes are never scraped.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import zipfile
from pathlib import Path

from lxml import etree

from .adapters import ADAPTERS
from .credentials import redact
from .db import DB, data_dir, loads, now
from .http import RetryPolicy, SourceError, download, request

FT_RETRY = RetryPolicy(attempts=2, base=1.5, cap=10)
PMC_S3 = "https://pmc-oa-opendata.s3.amazonaws.com/"


def review_dir(review_id: int) -> Path:
    d = data_dir() / "reviews" / str(review_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def report_dir(review_id: int, report_id: int) -> Path:
    d = review_dir(review_id) / "files" / f"RPT-{report_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _attempt(db, report_id, route, url, status, http_status=None, detail=None, file_id=None):
    db.insert("fulltext_attempts", {"report_id": report_id, "route": route, "url": redact(url),
                                    "status": status, "http_status": http_status,
                                    "detail": redact(detail)[:1000] if detail else None, "file_id": file_id,
                                    "attempted_at": now()})


def save_file(db: DB, review_id: int, report_id: int, data: bytes, name: str, kind: str, mime: str, origin: str,
              origin_url=None, license=None, original_name=None) -> int:
    sha = hashlib.sha256(data).hexdigest()
    dup = db.one("SELECT id FROM files WHERE report_id=? AND sha256=?", (report_id, sha))
    if dup:
        return dup["id"]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]
    p = report_dir(review_id, report_id) / safe
    i = 1
    while p.exists():
        p = p.with_name(f"{p.stem}_{i}{p.suffix}")
        i += 1
    p.write_bytes(data)
    return db.insert("files", {"review_id": review_id, "report_id": report_id, "kind": kind, "path": str(p),
                               "sha256": sha, "mime": mime, "size": len(data), "origin": origin,
                               "origin_url": redact(origin_url), "license": license,
                               "original_name": original_name or name, "created_at": now()})


def _records(db, report_id):
    ids = [report_id] + [r["id"] for r in db.q("SELECT id FROM reports WHERE merged_into=?", (report_id,))]
    return db.q(f"""SELECT r.* FROM records r JOIN record_report rr ON rr.record_id=r.id
                    WHERE rr.report_id IN ({','.join('?' * len(ids))})""", ids)


def _download_pdf(db, review_id, report_id, url, route, license=None) -> int | None:
    try:
        code, hdrs, data = download(url, source=f"ft:{route}", headers={"Accept": "application/pdf,*/*"})
    except SourceError as e:
        bot = "Just a moment" in str(e) or "cf-" in str(e).lower()
        _attempt(db, report_id, route, url, "bot_blocked" if bot else (
            "access_denied" if e.kind in ("auth", "access") else "error"), e.http_status,
            "host serves an anti-bot challenge; download manually and upload" if bot else str(e))
        return None
    if not data.startswith(b"%PDF"):
        ctype = hdrs.get("content-type", "")
        _attempt(db, report_id, route, url, "not_pdf", code,
                 f"response is {ctype or 'unknown type'} ({len(data)} bytes), probably a landing/paywall page")
        return None
    fid = save_file(db, review_id, report_id, data, f"RPT-{report_id}_{route}.pdf", "pdf", "application/pdf", route,
                    url, license)
    _attempt(db, report_id, route, url, "success", code, f"{len(data)} bytes", fid)
    return fid


def retrieve(db: DB, review_id: int, report_id: int, routes: list[str] | None = None, stop_after_xml=False) -> dict:
    rep = db.one("SELECT * FROM reports WHERE id=?", (report_id,))
    recs = _records(db, report_id)
    pmcid = rep["pmcid"] or next((r["pmcid"] for r in recs if r["pmcid"]), None)
    doi = rep["doi"]
    got = {"xml": None, "pdf": None, "registry": None, "supplements": None}
    want = set(routes or ["registry_record", "pmc_cloud", "europepmc_xml", "europepmc_supplements",
                          "unpaywall_pdf", "openalex_pdf", "crossref_tdm"])

    # registry record
    if "registry_record" in want and rep["report_kind"] == "registry":
        nct = (loads(rep["nct_ids_json"], []) or [None])[0]
        raw = next((r["raw"] for r in recs if r["source"] == "ctgov"), None)
        try:
            data = None
            if nct:
                rr = request("GET", f"https://clinicaltrials.gov/api/v2/studies/{nct}", source="ctgov", per_sec=5,
                             expect_json=True)
                data = rr.content
            elif raw:
                data = raw.encode()
            if data:
                fid = save_file(db, review_id, report_id, data, f"{nct or 'registry'}.json", "registry_json",
                                "application/json", "registry_record", f"https://clinicaltrials.gov/study/{nct}")
                _attempt(db, report_id, "registry_record", f"https://clinicaltrials.gov/api/v2/studies/{nct}",
                         "success", 200, "has posted results" if b'"resultsSection"' in data else
                         "no posted results", fid)
                got["registry"] = fid
        except SourceError as e:
            _attempt(db, report_id, "registry_record", nct, "error", e.http_status, str(e))

    if rep["report_kind"] != "registry":
        if "pmc_cloud" in want:
            if pmcid:
                _pmc_cloud(db, review_id, report_id, pmcid, got)
            else:
                _attempt(db, report_id, "pmc_cloud", None, "skipped", None, "no PMCID")
        if "europepmc_xml" in want and not got["xml"]:
            if pmcid:
                url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
                try:
                    r = request("GET", url, source="europepmc", per_sec=8, policy=FT_RETRY)
                    if b"<article" in r.content[:5000]:
                        fid = save_file(db, review_id, report_id, r.content, f"{pmcid}.xml", "xml",
                                        "application/xml", "europepmc_xml", url)
                        _attempt(db, report_id, "europepmc_xml", url, "success", r.status_code, "JATS XML", fid)
                        got["xml"] = fid
                    else:
                        _attempt(db, report_id, "europepmc_xml", url, "not_available", r.status_code,
                                 "no JATS article in response")
                except SourceError as e:
                    na = e.kind == "not_found" or e.http_status == 500
                    _attempt(db, report_id, "europepmc_xml", url, "not_available" if na else "error", e.http_status,
                             "not in the Europe PMC open-access subset (HTTP %s)" % e.http_status if na else str(e))
            else:
                _attempt(db, report_id, "europepmc_xml", None, "skipped", None, "no PMCID")
        if "europepmc_supplements" in want and pmcid:
            url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles"
            try:
                r = request("GET", url, source="europepmc", per_sec=8, policy=FT_RETRY)
                if r.content[:2] == b"PK":
                    fid = save_file(db, review_id, report_id, r.content, f"{pmcid}_supplementary.zip",
                                    "supplement_bundle", "application/zip", "europepmc_supplements", url)
                    names = _unpack_supplements(db, review_id, report_id, r.content, url)
                    _attempt(db, report_id, "europepmc_supplements", url, "success", r.status_code,
                             f"{len(names)} files: {', '.join(names[:10])}", fid)
                    got["supplements"] = fid
                else:
                    _attempt(db, report_id, "europepmc_supplements", url, "not_available", r.status_code,
                             "no supplementary bundle")
            except SourceError as e:
                _attempt(db, report_id, "europepmc_supplements", url, "not_available" if e.kind == "not_found"
                         else "error", e.http_status, str(e))
        pdf_sources = []
        if got.get("pmc_pdf_url"):
            pdf_sources.append(("pmc_cloud_pdf", got["pmc_pdf_url"], got.get("pmc_license")))
        if "unpaywall_pdf" in want and doi:
            try:
                up = ADAPTERS["unpaywall"].lookup(doi)
                if up and up.get("best_oa_location"):
                    loc = up["best_oa_location"]
                    if loc.get("url_for_pdf"):
                        pdf_sources.append(("unpaywall_pdf", loc["url_for_pdf"], loc.get("license")))
                    else:
                        _attempt(db, report_id, "unpaywall_pdf", loc.get("url"), "not_available", None,
                                 f"OA location without direct PDF ({loc.get('host_type')}, {loc.get('version')})")
                else:
                    _attempt(db, report_id, "unpaywall_pdf", None, "not_available", None,
                             "Unpaywall: no open-access copy" if up else "DOI unknown to Unpaywall")
            except SourceError as e:
                _attempt(db, report_id, "unpaywall_pdf", None, "skipped" if e.kind == "auth" else "error",
                         e.http_status, str(e))
        if "openalex_pdf" in want:
            oa = next((loads(r["other_ids_json"], {}) for r in recs if r["source"] == "openalex"), None)
            if oa is None and doi:
                try:
                    w = ADAPTERS["openalex"].work(f"doi:{doi}")
                    oa = w["other_ids"] if w else None
                except SourceError as e:
                    _attempt(db, report_id, "openalex_pdf", None, "error", e.http_status, str(e))
            if oa and oa.get("pdf_url"):
                pdf_sources.append(("openalex_pdf", oa["pdf_url"], oa.get("license")))
            elif oa is not None:
                _attempt(db, report_id, "openalex_pdf", None, "not_available", None, "no OA PDF location in OpenAlex")
        if "crossref_tdm" in want and doi:
            try:
                cr = ADAPTERS["crossref"].work(doi)
                if cr:
                    lic = [l.get("url") or "" for l in cr["other_ids"].get("licenses", [])]
                    open_lic = any("creativecommons.org" in l for l in lic)
                    links = [l for l in cr["other_ids"].get("links", []) if l.get("content_type") in
                             ("application/pdf", "unspecified") and l.get("intended") in ("text-mining", "similarity-checking", None)]
                    if open_lic and links:
                        pdf_sources.append(("crossref_tdm", links[0]["url"], next(l for l in lic if "creativecommons" in l)))
                    else:
                        _attempt(db, report_id, "crossref_tdm", None, "not_permitted" if links else "not_available",
                                 None, "publisher link present but licence is not open; not retrieved" if links
                                 else "no full-text link in Crossref")
            except SourceError as e:
                _attempt(db, report_id, "crossref_tdm", None, "error", e.http_status, str(e))
        seen = set()
        for route, url, lic in pdf_sources:
            if got["pdf"] or url in seen:
                if url not in seen:
                    _attempt(db, report_id, route, url, "skipped", None, "PDF already retrieved via another route")
                continue
            seen.add(url)
            fid = _download_pdf(db, review_id, report_id, url, route, lic)
            if fid:
                got["pdf"] = fid

    got.pop("pmc_pdf_url", None)
    got.pop("pmc_license", None)
    for k in ("xml", "pdf", "registry"):
        if got[k]:
            extract(db, review_id, report_id, got[k])
    for f in db.q("SELECT id FROM files WHERE report_id=? AND kind='supplement' AND id NOT IN "
                  "(SELECT file_id FROM text_extractions)", (report_id,)):
        extract(db, review_id, report_id, f["id"])
    status = _ft_status(db, report_id)
    return {"report_id": report_id, "ft_status": status, "files": {k: v for k, v in got.items() if v},
            "attempts": db.q("SELECT route,status,detail,url FROM fulltext_attempts WHERE report_id=? ORDER BY id DESC "
                             "LIMIT 12", (report_id,))}


MIME = {"pdf": "application/pdf", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "csv": "text/csv",
        "txt": "text/plain", "xml": "application/xml", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "gif": "image/gif", "tif": "image/tiff", "tiff": "image/tiff", "zip": "application/zip",
        "xls": "application/vnd.ms-excel", "doc": "application/msword", "pptx": "application/vnd.ms-powerpoint"}
EXTRACTABLE = {"pdf", "xlsx", "docx", "csv", "txt", "xml"}


def _media_kind(name: str) -> tuple[str, str]:
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    mime = MIME.get(ext, "application/octet-stream")
    if mime.startswith("image/"):
        return "figure_image", mime  # preserved, not text-extracted
    return ("supplement" if ext in EXTRACTABLE else "supplement_other"), mime


def _pmc_cloud(db, review_id, report_id, pmcid, got) -> None:
    """PMC Cloud Service on AWS Open Data: list versions, read metadata JSON, fetch JATS XML (+ PDF url)."""
    listing = f"{PMC_S3}?list-type=2&prefix={pmcid}."
    try:
        r = request("GET", listing, source="pmc_cloud", per_sec=5, policy=FT_RETRY)
    except SourceError as e:
        _attempt(db, report_id, "pmc_cloud", listing, "error", e.http_status, str(e))
        return
    keys = re.findall(r"<Key>([^<]+\.json)</Key>", r.text)
    if not keys:
        _attempt(db, report_id, "pmc_cloud", listing, "not_available", r.status_code,
                 "not in the PMC open-access / author-manuscript datasets")
        return
    key = sorted(keys, key=lambda k: int(re.search(r"\.(\d+)/", k).group(1)) if re.search(r"\.(\d+)/", k) else 0)[-1]
    meta = request("GET", PMC_S3 + key, source="pmc_cloud", per_sec=5, policy=FT_RETRY).json()
    lic = meta.get("license_code")
    if meta.get("is_retracted"):
        _attempt(db, report_id, "pmc_cloud", PMC_S3 + key, "warning", 200, "PMC marks this article as RETRACTED")
    xml_url = (meta.get("xml_url") or "").split("?")[0].replace("s3://pmc-oa-opendata/", PMC_S3)
    if xml_url:
        x = request("GET", xml_url, source="pmc_cloud", per_sec=5, policy=FT_RETRY)
        fid = save_file(db, review_id, report_id, x.content, f"{pmcid}_pmc.xml", "xml", "application/xml", "pmc_cloud",
                        xml_url, lic)
        kind = "author manuscript" if meta.get("is_manuscript") else ("OA article" if meta.get("is_pmc_openaccess")
                                                                     else "PMC article")
        _attempt(db, report_id, "pmc_cloud", xml_url, "success", 200, f"JATS XML ({kind}, licence {lic})", fid)
        got["xml"] = fid
    pdf_url = (meta.get("pdf_url") or "").split("?")[0].replace("s3://pmc-oa-opendata/", PMC_S3)
    if pdf_url:
        got["pmc_pdf_url"], got["pmc_license"] = pdf_url, lic
    for m in meta.get("media_urls") or []:
        mu = m.split("?")[0].replace("s3://pmc-oa-opendata/", PMC_S3)
        try:
            mr = request("GET", mu, source="pmc_cloud", per_sec=5, policy=FT_RETRY)
            nm = mu.rsplit("/", 1)[-1]
            kind, mime = _media_kind(nm)
            save_file(db, review_id, report_id, mr.content, ("fig_" if kind == "figure_image" else "suppl_") + nm, kind,
                      mime, "pmc_cloud", mu, lic, original_name=nm)
        except SourceError as e:
            _attempt(db, report_id, "pmc_cloud_media", mu, "error", e.http_status, str(e))


def _unpack_supplements(db, review_id, report_id, data: bytes, url) -> list[str]:
    names = []
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return names
    for info in z.infolist():
        if info.is_dir() or info.file_size > 200_000_000:
            continue
        nm = Path(info.filename).name
        if not nm or nm.startswith("."):
            continue
        content = z.read(info)
        kind, mime = _media_kind(nm)
        save_file(db, review_id, report_id, content, ("fig_" if kind == "figure_image" else "suppl_") + nm, kind, mime,
                  "europepmc_supplements", url, original_name=nm)
        names.append(nm)
    return names


def _ft_status(db, report_id) -> str:
    files = db.q("SELECT kind FROM files WHERE report_id=?", (report_id,))
    kinds = {f["kind"] for f in files}
    ex_ok = db.one("SELECT COUNT(*) c FROM text_extractions WHERE report_id=? AND status IN ('ok','partial')",
                   (report_id,))["c"]
    rep = db.one("SELECT * FROM reports WHERE id=?", (report_id,))
    if kinds & {"xml", "pdf", "registry_json", "upload"}:
        st = "retrieved" if ex_ok else "retrieved_extraction_failed"
    else:
        st = "unavailable" if db.one("SELECT 1 FROM fulltext_attempts WHERE report_id=?", (report_id,)) else "not_sought"
    reason = None
    if st == "unavailable":
        reason = "; ".join(f"{a['route']}: {a['status']}" for a in db.q(
            "SELECT route, status FROM fulltext_attempts WHERE report_id=? GROUP BY route ORDER BY MAX(id)", (report_id,)))
        if rep["abstract"]:
            reason = "abstract only — " + reason
    db.update("reports", report_id, {"ft_status": st, "ft_unavailable_reason": reason})
    return st


def mark_unavailable(db: DB, report_id: int, reason: str, actor: str) -> None:
    _attempt(db, report_id, "manual_search", None, "not_available", None, f"{actor}: {reason}")
    db.update("reports", report_id, {"ft_status": "unavailable", "ft_unavailable_reason": reason})


def upload(db: DB, review_id: int, report_id: int, name: str, data: bytes, kind: str, actor: str) -> dict:
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if data.startswith(b"%PDF"):
        mime = "application/pdf"
    elif data.lstrip()[:1] == b"<":
        mime = "application/xml"
    elif ext in MIME:
        mime = MIME[ext]
    else:
        try:  # plain text / Markdown pasted or saved by a reviewer
            data[:20000].decode("utf-8")
            mime = "text/plain"
        except UnicodeDecodeError:
            mime = "application/octet-stream"
    k = "upload" if kind in ("fulltext", "upload") else "supplement"
    fid = save_file(db, review_id, report_id, data, f"upload_{name}", k, mime, "manual_upload", None,
                    original_name=name)
    _attempt(db, report_id, "manual_upload", None, "success", None, f"uploaded by {actor}: {name}", fid)
    ex = extract(db, review_id, report_id, fid)
    _ft_status(db, report_id)
    return {"file_id": fid, "extraction": ex}


# ----------------------------------------------------------------------------- extraction
def extract(db: DB, review_id: int, report_id: int, file_id: int) -> dict:
    f = db.one("SELECT * FROM files WHERE id=?", (file_id,))
    data = Path(f["path"]).read_bytes()
    method, segments, pages, ocr_pages, err, status = None, [], None, 0, None, "ok"
    try:
        if f["mime"] == "application/pdf" or data.startswith(b"%PDF"):
            method = "pdf_text"
            segments, pages, ocr_pages, err = _pdf_segments(data)
            if ocr_pages:
                method = "pdf_text+ocr"
            if err:
                status = "partial" if segments else "failed"
        elif f["kind"] == "registry_json":
            method = "registry_json"
            segments = _registry_segments(json.loads(data))
        elif data.lstrip()[:1] == b"<":
            method = "jats_xml"
            segments = _jats_segments(data)
        elif f["mime"] in ("text/plain", "text/csv"):
            method = "plain_text"
            paras = [p.strip() for p in re.split(r"\n\s*\n", data.decode("utf-8", "replace")) if p.strip()]
            segments = [{"section": f["original_name"], "kind": "p", "text": " ".join(p.split()),
                         "location": f"{f['original_name']} ¶{i + 1}"} for i, p in enumerate(paras)]
        elif f["original_name"] and f["original_name"].lower().endswith((".xlsx", ".docx")):
            method = "office_text"
            segments = _office_segments(data, f["original_name"])
        else:
            status, err = "unsupported", f"no extractor for {f['mime']}"
    except Exception as e:  # keep failures visible
        status, err = "failed", f"{type(e).__name__}: {e}"
    if not segments and status == "ok":
        status, err = "failed", "no text extracted"
    text = "\n\n".join(_seg_line(s) for s in segments)
    out_dir = report_dir(review_id, report_id)
    tp = out_dir / f"extract_{file_id}.txt"
    sp = out_dir / f"extract_{file_id}.segments.json"
    tp.write_text(text)
    sp.write_text(json.dumps(segments))
    db.x("DELETE FROM text_extractions WHERE file_id=?", (file_id,))
    db.insert("text_extractions", {"report_id": report_id, "file_id": file_id, "method": method or "none",
                                   "status": status, "text_path": str(tp), "segments_path": str(sp), "pages": pages,
                                   "chars": len(text), "ocr_pages": ocr_pages, "error": err, "created_at": now()})
    return {"status": status, "method": method, "chars": len(text), "pages": pages, "ocr_pages": ocr_pages,
            "error": err}


def _seg_line(s):
    return f"[{s['location']}] {s['text']}"


def ocr_available() -> bool:
    return bool(shutil.which("tesseract"))


def _pdf_segments(data: bytes):
    import pymupdf
    doc = pymupdf.open(stream=data, filetype="pdf")
    segs, ocr_pages, errs = [], 0, []
    tess = ocr_available()
    for i, page in enumerate(doc):
        txt = page.get_text("text") or ""
        if len(txt.strip()) < 40 and page.get_images():
            if tess:
                try:
                    tp = page.get_textpage_ocr(language="eng", dpi=300, full=True)
                    txt = page.get_text("text", textpage=tp)
                    ocr_pages += 1
                except Exception as e:
                    errs.append(f"OCR failed on p.{i + 1}: {e}")
            else:
                errs.append(f"p.{i + 1} is an image without a text layer and tesseract is not installed")
        for j, para in enumerate(re.split(r"\n\s*\n", txt)):
            p = " ".join(para.split())
            if p:
                segs.append({"section": f"page {i + 1}", "kind": "p", "text": p, "location": f"p. {i + 1}",
                             "page": i + 1})
    n = len(doc)
    doc.close()
    return segs, n, ocr_pages, "; ".join(errs) or None


def _t(el) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _jats_segments(data: bytes) -> list[dict]:
    root = etree.fromstring(data, etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False,
                                                  no_network=True))
    segs = []
    tg = root.find(".//article-meta/title-group/article-title")
    if tg is not None:
        segs.append({"section": "Title", "kind": "title", "text": _t(tg), "location": "Title"})
    for ab in root.findall(".//article-meta/abstract"):
        for k, p in enumerate(ab.iter("p")):
            secs = [_t(s.find("title")) for s in p.iterancestors("sec") if s.find("title") is not None]
            label = "Abstract" + (" > " + " > ".join(reversed(secs)) if secs else "")
            segs.append({"section": label, "kind": "abstract", "text": _t(p), "location": f"{label} ¶{k + 1}"})
    body = root.find(".//body")
    if body is not None:
        counters: dict[str, int] = {}
        for el in body.iter("p", "table-wrap", "fig"):
            if el.tag == "p" and any(a.tag in ("table-wrap", "fig", "caption") for a in el.iterancestors()):
                continue
            secs = [_t(s.find("title")) for s in el.iterancestors("sec") if s.find("title") is not None]
            path = " > ".join(reversed(secs)) or "Body"
            if el.tag == "p":
                counters[path] = counters.get(path, 0) + 1
                segs.append({"section": path, "kind": "p", "text": _t(el), "location": f"{path} ¶{counters[path]}"})
            elif el.tag == "table-wrap":
                label = _t(el.find("label")) or "Table"
                cap = _t(el.find("caption"))
                rows = []
                for tr in el.iter("tr"):
                    rows.append(" | ".join(_t(c) for c in tr if c.tag in ("td", "th")))
                foot = _t(el.find("table-wrap-foot"))
                segs.append({"section": path, "kind": "table", "text": f"{label}. {cap}\n" + "\n".join(rows) +
                             (f"\n{foot}" if foot else ""), "location": label})
            else:
                label = _t(el.find("label")) or "Figure"
                segs.append({"section": path, "kind": "figure_caption", "text": f"{label}. {_t(el.find('caption'))}",
                             "location": f"{label} legend"})
    for sm in root.findall(".//supplementary-material"):
        segs.append({"section": "Supplementary material", "kind": "supplement_ref",
                     "text": _t(sm) or (sm.find("media").get("{http://www.w3.org/1999/xlink}href")
                                        if sm.find("media") is not None else ""),
                     "location": _t(sm.find("label")) or "Supplementary material"})
    return [s for s in segs if s["text"]]


def _registry_segments(d: dict) -> list[dict]:
    segs = []
    ps = d.get("protocolSection", {})

    def add(section, text, loc=None):
        if text:
            segs.append({"section": section, "kind": "registry", "text": text, "location": loc or section})
    ident = ps.get("identificationModule", {})
    add("Identification", f"{ident.get('nctId')} — {ident.get('officialTitle') or ident.get('briefTitle')}")
    st = ps.get("statusModule", {})
    add("Status", f"Overall status: {st.get('overallStatus')}; start {st.get('startDateStruct', {}).get('date')}; "
                  f"primary completion {st.get('primaryCompletionDateStruct', {}).get('date')}; first posted "
                  f"{st.get('studyFirstPostDateStruct', {}).get('date')}; results first posted "
                  f"{st.get('resultsFirstPostDateStruct', {}).get('date')}")
    add("Brief summary", ps.get("descriptionModule", {}).get("briefSummary"))
    add("Detailed description", ps.get("descriptionModule", {}).get("detailedDescription"))
    dm = ps.get("designModule", {})
    add("Design", f"Type {dm.get('studyType')}; phases {dm.get('phases')}; allocation "
                  f"{dm.get('designInfo', {}).get('allocation')}; model {dm.get('designInfo', {}).get('interventionModel')}; "
                  f"masking {dm.get('designInfo', {}).get('maskingInfo', {}).get('masking')}; enrollment "
                  f"{dm.get('enrollmentInfo', {}).get('count')} ({dm.get('enrollmentInfo', {}).get('type')})")
    for i, a in enumerate(ps.get("armsInterventionsModule", {}).get("armGroups", [])):
        add("Arms", f"{a.get('label')} [{a.get('type')}]: {a.get('description')} Interventions: "
                    f"{a.get('interventionNames')}", f"Arm {i + 1}")
    for i, o in enumerate(ps.get("outcomesModule", {}).get("primaryOutcomes", [])):
        add("Primary outcomes", f"{o.get('measure')} — {o.get('description')} (time frame: {o.get('timeFrame')})",
            f"Primary outcome {i + 1}")
    for i, o in enumerate(ps.get("outcomesModule", {}).get("secondaryOutcomes", [])):
        add("Secondary outcomes", f"{o.get('measure')} — {o.get('description')} (time frame: {o.get('timeFrame')})",
            f"Secondary outcome {i + 1}")
    el = ps.get("eligibilityModule", {})
    add("Eligibility", f"{el.get('eligibilityCriteria')} Sex: {el.get('sex')}; ages {el.get('minimumAge')}–"
                       f"{el.get('maximumAge')}")
    rs = d.get("resultsSection")
    if rs:
        pf = rs.get("participantFlowModule", {})
        for g in pf.get("groups", []):
            add("Results: participant flow", f"{g.get('id')} {g.get('title')}: {g.get('description')}", "Participant flow")
        for i, om in enumerate(rs.get("outcomeMeasuresModule", {}).get("outcomeMeasures", [])):
            groups = {g["id"]: g.get("title") for g in om.get("groups", [])}
            lines = [f"{om.get('type')} outcome: {om.get('title')} — {om.get('description', '')} "
                     f"(time frame {om.get('timeFrame')}; param {om.get('paramType')}; dispersion "
                     f"{om.get('dispersionType')}; unit {om.get('unitOfMeasure')})"]
            for den in om.get("denoms", []):
                lines.append("N analysed: " + ", ".join(f"{groups.get(c.get('groupId'))}={c.get('value')}"
                                                         for c in den.get("counts", [])))
            for cl in om.get("classes", []):
                for cat in cl.get("categories", []):
                    for m in cat.get("measurements", []):
                        lines.append(f"{cl.get('title') or ''} {cat.get('title') or ''} {groups.get(m.get('groupId'))}: "
                                     f"value {m.get('value')} spread {m.get('spread')} "
                                     f"[{m.get('lowerLimit')}, {m.get('upperLimit')}]".strip())
            add("Results: outcome measures", "\n".join(lines), f"Results outcome measure {i + 1}")
        ae = rs.get("adverseEventsModule", {})
        if ae:
            lines = [f"Adverse events time frame: {ae.get('timeFrame')}; {ae.get('description', '')}"]
            for g in ae.get("eventGroups", []):
                lines.append(f"{g.get('title')}: serious {g.get('seriousNumAffected')}/{g.get('seriousNumAtRisk')}, "
                             f"other {g.get('otherNumAffected')}/{g.get('otherNumAtRisk')}, deaths "
                             f"{g.get('deathsNumAffected')}/{g.get('deathsNumAtRisk')}")
            for e in ae.get("seriousEvents", [])[:50]:
                lines.append(f"SAE {e.get('term')}: " + ", ".join(f"{s.get('groupId')}={s.get('numAffected')}/"
                                                                   f"{s.get('numAtRisk')}" for s in e.get("stats", [])))
            add("Results: adverse events", "\n".join(lines), "Adverse events")
    else:
        add("Results", "No results posted on ClinicalTrials.gov.", "Results")
    refs = ps.get("referencesModule", {}).get("references", [])
    for r in refs:
        add("References", f"[{r.get('type')}] PMID {r.get('pmid')}: {r.get('citation')}", "References")
    return segs


def _office_segments(data: bytes, name: str) -> list[dict]:
    segs = []
    z = zipfile.ZipFile(io.BytesIO(data))
    if name.lower().endswith(".docx") and "word/document.xml" in z.namelist():
        root = etree.fromstring(z.read("word/document.xml"))
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        for i, p in enumerate(root.iter(f"{ns}p")):
            t = "".join(x.text or "" for x in p.iter(f"{ns}t")).strip()
            if t:
                segs.append({"section": name, "kind": "p", "text": t, "location": f"{name} ¶{i + 1}"})
    elif name.lower().endswith(".xlsx"):
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            sroot = etree.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(si.itertext()) for si in sroot]
        for sheet in sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet")):
            root = etree.fromstring(z.read(sheet))
            ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            for row in root.iter(f"{ns}row"):
                vals = []
                for c in row.iter(f"{ns}c"):
                    v = c.find(f"{ns}v")
                    if v is None:
                        continue
                    vals.append(shared[int(v.text)] if c.get("t") == "s" and v.text.isdigit() and int(v.text) < len(shared)
                                else v.text)
                if vals:
                    segs.append({"section": f"{name} {Path(sheet).stem}", "kind": "table_row", "text": " | ".join(
                        str(x) for x in vals), "location": f"{name} {Path(sheet).stem} row {row.get('r')}"})
    return segs


def segments(db: DB, report_id: int) -> list[dict]:
    """All extracted segments for a report, best source first (XML > registry > PDF > uploads > supplements)."""
    order = {"jats_xml": 0, "registry_json": 1, "pdf_text": 2, "pdf_text+ocr": 2, "office_text": 4, "plain_text": 4}
    exs = db.q("""SELECT te.*, f.kind fkind, f.original_name, f.path fpath FROM text_extractions te
                  JOIN files f ON f.id=te.file_id WHERE te.report_id=? AND te.status IN ('ok','partial')""", (report_id,))
    exs.sort(key=lambda e: (e["fkind"] == "supplement", order.get(e["method"], 5)))
    out = []
    primary_done = False
    for e in exs:
        if e["fkind"] != "supplement":
            if primary_done and e["method"].startswith("pdf"):
                continue  # PDF duplicate of XML text: keep file, don't double the text
            primary_done = True
        for s in json.loads(Path(e["segments_path"]).read_text()):
            s["file_id"] = e["file_id"]
            s["file"] = Path(e["fpath"]).name
            s["method"] = e["method"]
            if e["fkind"] == "supplement":
                s["location"] = f"Supplement {e['original_name']}: {s['location']}"
            out.append(s)
    return out


def reclassify_media(db: DB) -> int:
    """Idempotent migration: older builds stored figure images/opaque supplements as 'supplement' and logged a
    pointless 'unsupported' extraction. Reclassify by extension and drop those extraction rows."""
    n = 0
    for f in db.q("SELECT id, original_name, path FROM files WHERE kind='supplement'"):
        kind, mime = _media_kind(f["original_name"] or Path(f["path"]).name)
        if kind != "supplement":
            db.update("files", f["id"], {"kind": kind, "mime": mime})
            db.x("DELETE FROM text_extractions WHERE file_id=? AND status='unsupported'", (f["id"],))
            n += 1
    return n
