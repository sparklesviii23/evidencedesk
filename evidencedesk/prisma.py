"""PRISMA 2020 flow + PRISMA-S appendix, both generated from one ledger so totals reconcile by construction.
Reconciliation identities are re-checked and reported in every export."""
from __future__ import annotations

import html
import json
from collections import Counter

from .adapters import ADAPTERS
from .db import DB, loads
from .screening import get_protocol, resolve
from .studies import study_map


def _category(source: str, db: DB, import_id=None) -> str:
    if source.startswith("citation:") or source == "manual":
        return "other"
    if source.startswith("import:"):
        imp = db.one("SELECT notes FROM imports WHERE id=?", (import_id,)) if import_id else None
        if imp and (loads(imp["notes"], {}) or {}).get("register"):
            return "register"
        return "database"
    a = ADAPTERS.get(source)
    return "register" if a and a.register else "database"


def ledger(db: DB, review_id: int) -> dict:
    proto = get_protocol(db, review_id)
    req = (proto["picos"].get("reviewers_required", {}) if proto else {})
    recs = db.q("SELECT id, source, import_id FROM records WHERE review_id=?", (review_id,))
    by_source = Counter(r["source"] for r in recs)
    cat = {"database": Counter(), "register": Counter(), "other": Counter()}
    for r in recs:
        cat[_category(r["source"], db, r["import_id"])][r["source"]] += 1
    active = db.q("""SELECT DISTINCT rp.* FROM reports rp JOIN record_report rr ON rr.report_id=rp.id
                     WHERE rp.review_id=? AND rp.merged_into IS NULL ORDER BY rp.id""", (review_id,))
    smap = study_map(db, review_id)
    rows = []
    for rp in active:
        ta = resolve(db, review_id, rp["id"], "title_abstract", req.get("title_abstract", 2))
        row = {"report_id": rp["id"], "title": rp["title"], "report_kind": rp["report_kind"], "ta": ta["final"],
               "ta_basis": ta["basis"], "sought": ta["final"] in ("include", "unclear"), "ft_status": rp["ft_status"],
               "ft": None, "ft_reason": None, "study": smap.get(rp["id"])}
        if row["sought"]:
            ft = resolve(db, review_id, rp["id"], "full_text", req.get("full_text", 2))
            row["ft"], row["ft_reason"] = ft["final"], ft.get("exclusion_reason")
            retrieved = rp["ft_status"] in ("retrieved", "retrieved_extraction_failed") or (
                rp["report_kind"] == "conference_abstract" and rp["abstract"])
            if ft["final"] == "exclude" and ft.get("exclusion_reason") == "full text unavailable":
                row["retrieval"] = "not_retrieved"
            elif retrieved or ft["final"] in ("include", "exclude"):
                row["retrieval"] = "assessed"
            elif rp["ft_status"] == "unavailable":
                row["retrieval"] = "not_retrieved"
            else:
                row["retrieval"] = "pending"
        rows.append(row)
    total = len(recs)
    screened = len(rows)
    ta_excl = sum(r["ta"] == "exclude" for r in rows)
    ta_wait = sum(r["ta"] in ("pending", "conflict") for r in rows)
    sought = [r for r in rows if r["sought"]]
    not_ret = [r for r in sought if r["retrieval"] == "not_retrieved"]
    ret_pend = [r for r in sought if r["retrieval"] == "pending"]
    assessed = [r for r in sought if r["retrieval"] == "assessed"]
    ft_ex = [r for r in assessed if r["ft"] == "exclude"]
    incl = [r for r in assessed if r["ft"] == "include"]
    ft_wait = [r for r in assessed if r["ft"] in ("pending", "conflict", "unclear")]
    studies = {r["study"] for r in incl}
    reasons = Counter(r["ft_reason"] or "reason not recorded" for r in ft_ex)
    merges = db.q("SELECT method, COUNT(*) c FROM merge_events WHERE review_id=? AND kind='merge' AND undone_at IS NULL "
                  "GROUP BY method", (review_id,))
    link_methods = Counter(r["method"].split(":")[0] if r["method"] else "?" for r in db.q(
        "SELECT rr.method FROM record_report rr JOIN records r ON r.id=rr.record_id WHERE r.review_id=?", (review_id,)))
    pending_cands = db.one("SELECT COUNT(*) c FROM dedup_candidates WHERE review_id=? AND status='pending'",
                           (review_id,))["c"]
    L = {
        "identification": {"records_total": total, "by_source": dict(by_source),
                           "databases": dict(cat["database"]), "registers": dict(cat["register"]),
                           "other_methods": dict(cat["other"]),
                           "databases_total": sum(cat["database"].values()),
                           "registers_total": sum(cat["register"].values()),
                           "other_total": sum(cat["other"].values()),
                           "duplicates_removed": total - screened, "removed_other_reasons": 0},
        "dedup": {"record_link_methods": dict(link_methods), "merge_events": {m["method"]: m["c"] for m in merges},
                  "pending_uncertain_matches": pending_cands},
        "screening": {"records_screened": screened, "records_excluded": ta_excl, "awaiting_screening": ta_wait},
        "retrieval": {"reports_sought": len(sought), "reports_not_retrieved": len(not_ret),
                      "retrieval_pending": len(ret_pend)},
        "eligibility": {"reports_assessed": len(assessed), "reports_excluded": len(ft_ex),
                        "exclusion_reasons": dict(reasons), "awaiting_classification": len(ft_wait)},
        "included": {"reports": len(incl), "studies": len(studies)},
        "rows": rows,
    }
    L["checks"] = reconcile(L)
    return L


def reconcile(L: dict) -> list[dict]:
    i, s, r, e, inc = L["identification"], L["screening"], L["retrieval"], L["eligibility"], L["included"]
    checks = [
        ("records = databases + registers + other", i["records_total"],
         i["databases_total"] + i["registers_total"] + i["other_total"]),
        ("records = duplicates removed + records screened", i["records_total"],
         i["duplicates_removed"] + s["records_screened"]),
        ("screened = excluded + sought + awaiting screening", s["records_screened"],
         s["records_excluded"] + r["reports_sought"] + s["awaiting_screening"]),
        ("sought = not retrieved + retrieval pending + assessed", r["reports_sought"],
         r["reports_not_retrieved"] + r["retrieval_pending"] + e["reports_assessed"]),
        ("assessed = excluded + included + awaiting classification", e["reports_assessed"],
         e["reports_excluded"] + inc["reports"] + e["awaiting_classification"]),
        ("excluded = sum of exclusion reasons", e["reports_excluded"], sum(e["exclusion_reasons"].values())),
        ("included studies <= included reports", inc["studies"], min(inc["studies"], inc["reports"])),
    ]
    out = [{"check": c, "left": a, "right": b, "ok": a == b} for c, a, b in checks]
    status = []
    if s["awaiting_screening"]:
        status.append(f"{s['awaiting_screening']} records still awaiting title/abstract decisions")
    if r["retrieval_pending"]:
        status.append(f"{r['retrieval_pending']} reports not yet sought for full text")
    if e["awaiting_classification"]:
        status.append(f"{e['awaiting_classification']} reports awaiting full-text classification")
    if L["dedup"]["pending_uncertain_matches"]:
        status.append(f"{L['dedup']['pending_uncertain_matches']} uncertain duplicate matches unresolved")
    out.append({"check": "review complete (no pending items)", "left": len(status), "right": 0, "ok": not status,
                "detail": status})
    return out


def _label(source: str) -> str:
    a = ADAPTERS.get(source)
    if a:
        return a.platform.split(" (")[0].replace(" REST", "").replace(" Works API", "").replace(" API v2", "")
    return source.replace("import:", "").replace("_", " ").title() + (" (import)" if source.startswith("import:") else "")


def svg(L: dict, title="PRISMA 2020 flow diagram") -> str:
    i, s, r, e, inc = L["identification"], L["screening"], L["retrieval"], L["eligibility"], L["included"]
    W, H = 900, 1000
    boxes = []

    def box(x, y, w, h, lines, fill="#ffffff", bold_first=False):
        boxes.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="4" fill="{fill}" stroke="#333" '
                     f'stroke-width="1.2"/>')
        for k, ln in enumerate(lines):
            wt = ' font-weight="700"' if bold_first and k == 0 else ""
            boxes.append(f'<text x="{x + 10}" y="{y + 20 + k * 16}" font-size="12.5"{wt}>{html.escape(ln)}</text>')

    def arrow(x1, y1, x2, y2):
        boxes.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#333" stroke-width="1.3" '
                     f'marker-end="url(#a)"/>')

    db_lines = ["Records identified from:"] + [f"  {_label(k)} (n = {v})" for k, v in i["databases"].items()]
    db_lines.append(f"Databases (n = {i['databases_total']})")
    reg_lines = [f"Registers (n = {i['registers_total']})"] + [f"  {_label(k)} (n = {v})" for k, v in
                                                               i["registers"].items()]
    if i["other_total"]:
        reg_lines += [f"Other methods (n = {i['other_total']})"] + [f"  {_label(k)} (n = {v})" for k, v in
                                                                    i["other_methods"].items()]
    idh = max(120, 22 + 16 * (len(db_lines) + len(reg_lines)))
    box(120, 50, 420, idh, db_lines + reg_lines)
    box(600, 50, 270, 90, ["Records removed before screening:", f"  Duplicate records (n = {i['duplicates_removed']})",
                           f"  Removed for other reasons (n = {i['removed_other_reasons']})"])
    arrow(540, 95, 600, 95)
    y = 50 + idh + 40
    arrow(330, 50 + idh, 330, y)
    sl = [f"Records screened (n = {s['records_screened']})"]
    if s["awaiting_screening"]:
        sl.append(f"  awaiting decision (n = {s['awaiting_screening']})")
    box(120, y, 420, 50 if len(sl) == 1 else 66, sl)
    box(600, y, 270, 50, [f"Records excluded (n = {s['records_excluded']})"])
    arrow(540, y + 25, 600, y + 25)
    y2 = y + 110
    arrow(330, y + (50 if len(sl) == 1 else 66), 330, y2)
    rl = [f"Reports sought for retrieval (n = {r['reports_sought']})"]
    if r["retrieval_pending"]:
        rl.append(f"  retrieval not yet attempted (n = {r['retrieval_pending']})")
    box(120, y2, 420, 50 if len(rl) == 1 else 66, rl)
    box(600, y2, 270, 50, [f"Reports not retrieved (n = {r['reports_not_retrieved']})"])
    arrow(540, y2 + 25, 600, y2 + 25)
    y3 = y2 + 110
    arrow(330, y2 + (50 if len(rl) == 1 else 66), 330, y3)
    el = [f"Reports assessed for eligibility (n = {e['reports_assessed']})"]
    if e["awaiting_classification"]:
        el.append(f"  awaiting classification (n = {e['awaiting_classification']})")
    box(120, y3, 420, 50 if len(el) == 1 else 66, el)
    exl = ["Reports excluded:"] + [f"  {k} (n = {v})" for k, v in e["exclusion_reasons"].items()] + \
          [f"Total (n = {e['reports_excluded']})"]
    exh = 20 + 16 * len(exl)
    box(600, y3, 270, exh, exl)
    arrow(540, y3 + 25, 600, y3 + 25)
    y4 = y3 + max(110, exh + 30)
    arrow(330, y3 + (50 if len(el) == 1 else 66), 330, y4)
    box(120, y4, 420, 66, [f"Studies included in review (n = {inc['studies']})",
                           f"Reports of included studies (n = {inc['reports']})"], fill="#eef6ee")
    H = y4 + 120
    for lab, yy in (("Identification", 50), ("Screening", y), ("Included", y4)):
        boxes.append(f'<text x="40" y="{yy + 40}" font-size="13" font-weight="700" transform="rotate(-90 40 {yy + 40})"'
                     f' text-anchor="end">{lab}</text>')
    bad = [c for c in L["checks"][:-1] if not c["ok"]]
    foot = "All PRISMA counts reconcile with the review ledger." if not bad else \
        "RECONCILIATION FAILED: " + "; ".join(c["check"] for c in bad)
    pend = L["checks"][-1].get("detail") or []
    boxes.append(f'<text x="120" y="{H - 40}" font-size="11" fill="{"#225522" if not bad else "#aa0000"}">'
                 f'{html.escape(foot)}</text>')
    if pend:
        boxes.append(f'<text x="120" y="{H - 24}" font-size="11" fill="#aa5500">In progress: '
                     f'{html.escape("; ".join(pend))}</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
            f'font-family="Helvetica, Arial, sans-serif"><defs><marker id="a" markerWidth="10" markerHeight="8" '
            f'refX="9" refY="4" orient="auto"><path d="M0,0 L10,4 L0,8 z" fill="#333"/></marker></defs>'
            f'<rect width="100%" height="100%" fill="white"/><text x="120" y="30" font-size="16" font-weight="700">'
            f'{html.escape(title)}</text>' + "".join(boxes) + "</svg>")


def prisma_s(db: DB, review_id: int, L: dict | None = None) -> str:
    L = L or ledger(db, review_id)
    rv = db.one("SELECT * FROM reviews WHERE id=?", (review_id,))
    out = [f"# PRISMA-S search appendix — {rv['title']}", "",
           "Generated by EvidenceDesk from the review ledger. Counts match the PRISMA 2020 flow diagram.", ""]
    runs = db.q("SELECT * FROM search_runs WHERE review_id=? ORDER BY id", (review_id,))
    for run in runs:
        out += [f"## Search run {run['id']} — started {run['started_at']} (status: {run['status']})", "",
                "Original EDQL query:", "", "```", run["query_text"], "```", ""]
        filt = loads(run["filters_json"], {})
        if filt.get("date"):
            out.append(f"Date limit: {filt['date']['start'] or 'open'} to {filt['date']['end'] or 'open'}")
        out.append(f"Record cap per source: {run['max_records']}")
        out.append("")
        for sr in db.q("SELECT * FROM source_runs WHERE run_id=? ORDER BY id", (run["id"],)):
            comp = loads(sr["compiled_json"], {}) or {}
            out += [f"### {sr['platform'] or sr['source']} (`{sr['source']}`)", "",
                    f"- Interface/platform: {sr['platform']}",
                    f"- Executed: {sr['executed_at'] or 'not executed'}; finished {sr['finished_at'] or '—'}",
                    f"- Status: **{sr['status']}**" + (f" — {sr['incomplete_reason']}" if sr["incomplete_reason"] else ""),
                    f"- Hits reported by platform: {sr['reported_hits'] if sr['reported_hits'] is not None else '—'}; "
                    f"records retrieved: {sr['retrieved']}; new to review: {sr['new_records']}",
                    f"- Translation fidelity: {sr['fidelity']}" + (f" (approximation approved by {sr['approved_by']})"
                                                                   if sr["approved_by"] else ""),
                    ]
            if sr["error"]:
                out.append(f"- Connector message: {sr['error']}")
            if sr["platform_notes"]:
                out.append(f"- Platform messages: {sr['platform_notes']}")
            out += ["", "Exact native query:", "", "```", sr["native_query"] or "", "```", ""]
            notes = comp.get("notes") or []
            if notes:
                out.append("Semantic differences / approximations:")
                out += [f"- [{n['severity']}] {n['clause'] + ': ' if n['clause'] else ''}{n['message']}" for n in notes]
                out.append("")
    imps = db.q("SELECT * FROM imports WHERE review_id=? ORDER BY id", (review_id,))
    if imps:
        out += ["## Imported database exports", ""]
        for im in imps:
            out += [f"### {im['platform']} — {im['file_name']}", "",
                    f"- Search date: {im['search_date'] or 'not recorded'}; imported {im['imported_at']}",
                    f"- Format: {im['file_format']}; records: {im['record_count']}; SHA-256 `{im['file_sha256']}`",
                    "", "Native query (as supplied):", "", "```", im["native_query"] or "(not supplied)", "```", ""]
    cit = [k for k in L["identification"]["other_methods"]]
    if cit:
        out += ["## Other methods", ""] + [f"- {k}: {L['identification']['other_methods'][k]} records" for k in cit] + [""]
    d = L["dedup"]
    out += ["## Deduplication", "",
            "Method: EvidenceDesk identifier matching (DOI, PMID, PMCID; NCT for registry records) with a title-"
            "similarity guard, followed by cautious bibliographic matching (auto-merge only if normalised titles "
            "≥97% similar, same year, same first-author surname, compatible report types and no identifier "
            "conflicts; 85–97% or any conflict queued for manual review). Every source record is preserved.", "",
            f"- Records identified: {L['identification']['records_total']}",
            f"- Duplicates removed: {L['identification']['duplicates_removed']}",
            f"- Unique reports screened: {L['screening']['records_screened']}",
            f"- Record-to-report link methods: {json.dumps(d['record_link_methods'])}",
            f"- Report merge events: {json.dumps(d['merge_events'])}",
            f"- Uncertain matches still pending review: {d['pending_uncertain_matches']}", ""]
    out += ["## Reconciliation", ""] + [f"- {'OK' if c['ok'] else 'FAIL'}: {c['check']} ({c['left']} vs {c['right']})"
                                        + (f" — {'; '.join(c['detail'])}" if c.get("detail") else "") for c in L["checks"]]
    return "\n".join(out) + "\n"
