"""Analysis-ready dataset + reproducible R (metafor) script from VERIFIED extraction rows and a prespecified plan.

First supported workflow: continuous outcomes, two-group comparisons, random-effects REML with Hartung–Knapp
(test="knha"), forest plot, leave-one-out. Pre-pooling checks flag incompatible outcomes/units/time points and
possible double counting (same study via several reports, shared control groups, overlapping cohorts).
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from .db import DB, loads, now
from .evidence import write_csv
from .studies import study_map

PLAN_TEMPLATE = {
    "analyses": [{
        "id": "A1", "outcome": "LDL-C", "outcome_aliases": [], "timepoint": None,
        "effect_measure": "SMD",  # SMD (Hedges g) | MD | ROM (log ratio of means)
        "method": "REML", "inference": "knha", "min_studies": 2,
        "shared_control": "split",  # split control n across comparisons | flag (block)
        "direction": "lower_is_better",
    }]}

T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
       12: 2.179, 15: 2.131, 20: 2.086, 25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980}


def _t95(df: float) -> float:
    if df >= 120:
        return 1.96
    keys = sorted(T95)
    for k in keys:
        if df <= k:
            return T95[k]
    return 1.96


def save_plan(db: DB, review_id: int, plan: dict) -> int:
    for a in plan.get("analyses", []):
        if a.get("effect_measure") not in ("SMD", "MD", "ROM"):
            raise ValueError("effect_measure must be SMD | MD | ROM")
        if a.get("method", "REML") != "REML" or a.get("inference", "knha") != "knha":
            raise ValueError("this version supports method=REML with inference=knha")
    v = (db.one("SELECT MAX(version) v FROM analysis_plans WHERE review_id=?", (review_id,))["v"] or 0) + 1
    db.insert("analysis_plans", {"review_id": review_id, "version": v, "plan_json": json.dumps(plan), "created_at": now()})
    return v


def get_plan(db: DB, review_id: int) -> dict | None:
    r = db.one("SELECT * FROM analysis_plans WHERE review_id=? ORDER BY version DESC LIMIT 1", (review_id,))
    return {"version": r["version"], "plan": json.loads(r["plan_json"])} if r else None


def _sd(row: dict) -> tuple[float | None, str | None]:
    if row["sd"] is not None:
        return row["sd"], None
    n = row["n"]
    if row["se"] is not None and n:
        return row["se"] * math.sqrt(n), f"SD = SE×√n ({row['se']}×√{n:g})"
    if row["ci_low"] is not None and row["ci_high"] is not None and n and n > 1:
        lvl = row["ci_level"] or 0.95
        if abs(lvl - 0.95) > 1e-6:
            return None, f"CI level {lvl} not supported for SD derivation"
        t = _t95(n - 1)
        return math.sqrt(n) * (row["ci_high"] - row["ci_low"]) / (2 * t), \
            f"SD = √n×(upper−lower)/(2×t[{n - 1:g}]={t})"
    return None, None


def build_dataset(db: DB, review_id: int, plan: dict | None = None) -> dict:
    plan = plan or (get_plan(db, review_id) or {}).get("plan")
    if not plan:
        raise ValueError("no analysis plan saved")
    smap = study_map(db, review_id)
    rows = db.q("SELECT * FROM extraction_rows WHERE review_id=? AND status='verified' AND outcome_type='continuous'",
                (review_id,))
    unverified = db.one("SELECT COUNT(*) c FROM extraction_rows WHERE review_id=? AND status!='verified'",
                        (review_id,))["c"]
    cohort = db.q("SELECT * FROM cohort_links WHERE review_id=?", (review_id,))
    results = []
    for a in plan["analyses"]:
        names = {a["outcome"].lower()} | {x.lower() for x in a.get("outcome_aliases", [])}
        sel = [r for r in rows if r["outcome"].lower() in names and
               (a.get("timepoint") is None or (r["timepoint"] or "").lower() == str(a["timepoint"]).lower())]
        flags, excluded, comps = [], [], []
        # study key: linked study, else report as own study; rows tied to a study_id override
        for r in sel:
            r["_study"] = r["study_id"] or smap.get(r["report_id"]) or -r["report_id"]
        # double counting: same study + experiment + arm reported by >1 report
        seen = defaultdict(set)
        for r in sel:
            seen[(r["_study"], r["experiment"] or "", r["arm"].lower(), r["timepoint"] or "")].add(r["report_id"])
        dup_keys = {k: v for k, v in seen.items() if len(v) > 1}
        for k, reps in dup_keys.items():
            flags.append({"type": "double_counting", "severity": "block",
                          "message": f"study {k[0]} experiment '{k[1]}' arm '{k[2]}' at '{k[3]}' has rows from reports "
                                     f"{sorted(reps)}; keep one report's rows (prefer the most complete)"})
        groups = defaultdict(list)
        for r in sel:
            groups[(r["_study"], r["experiment"] or "", r["timepoint"] or "")].append(r)
        tps = {r["timepoint"] for r in sel}
        if a.get("timepoint") is None and len(tps) > 1:
            by_study = defaultdict(set)
            for r in sel:
                by_study[r["_study"]].add(r["timepoint"])
            multi = [s for s, t in by_study.items() if len(t) > 1]
            if multi:
                flags.append({"type": "multiple_timepoints", "severity": "block",
                              "message": f"studies {multi} contribute several time points {sorted(map(str, tps))}; set "
                                         "plan.timepoint to pool one time point"})
        units = {(r["unit"] or "").strip() for r in sel}
        if a["effect_measure"] == "MD" and len(units) > 1:
            flags.append({"type": "incompatible_units", "severity": "block",
                          "message": f"MD requires a common unit; found {sorted(units)} (use SMD or ROM, or convert)"})
        for (study, exp, tp), grp in groups.items():
            ctrls = [g for g in grp if g["arm_role"] == "control"]
            ints = [g for g in grp if g["arm_role"] == "intervention"]
            if not ctrls or not ints:
                excluded.append({"study": study, "experiment": exp, "reason": "needs one control and >=1 intervention arm"})
                continue
            for it in ints:
                ctrl = next((c for c in ctrls if it["control_group_id"] and c["control_group_id"] == it["control_group_id"]),
                            ctrls[0] if len(ctrls) == 1 else None)
                if ctrl is None:
                    excluded.append({"study": study, "experiment": exp, "arm": it["arm"],
                                     "reason": "several control arms; set control_group_id to pair arms"})
                    continue
                comps.append((study, exp, tp, it, ctrl))
        use_count = defaultdict(int)
        for c in comps:
            use_count[c[4]["id"]] += 1
        data = []
        for study, exp, tp, it, ctrl in comps:
            sd1, d1 = _sd(it)
            sd2, d2 = _sd(ctrl)
            if sd1 is None or sd2 is None or not it["n"] or not ctrl["n"] or it["mean"] is None or ctrl["mean"] is None:
                excluded.append({"study": study, "experiment": exp, "arm": it["arm"],
                                 "reason": "missing mean, n or dispersion (SD/SE/95% CI)"})
                continue
            n2 = ctrl["n"]
            k = use_count[ctrl["id"]]
            note = []
            if k > 1:
                if a.get("shared_control", "split") == "split":
                    n2 = ctrl["n"] / k
                    note.append(f"shared control '{ctrl['arm']}' used by {k} comparisons: control n split {ctrl['n']:g}/{k}")
                    flags.append({"type": "shared_control", "severity": "handled",
                                  "message": f"study {study} {exp}: " + note[-1]})
                else:
                    flags.append({"type": "shared_control", "severity": "block",
                                  "message": f"study {study} {exp}: control '{ctrl['arm']}' shared by {k} comparisons"})
            if a["effect_measure"] == "ROM" and (it["mean"] <= 0 or ctrl["mean"] <= 0):
                excluded.append({"study": study, "experiment": exp, "arm": it["arm"],
                                 "reason": "ROM requires positive means"})
                continue
            origins = {it["value_origin"], ctrl["value_origin"]}
            data.append({
                "analysis": a["id"], "study": f"STU-{study}" if study > 0 else f"RPT-{-study}",
                "report_ids": f"{it['report_id']}" + (f";{ctrl['report_id']}" if ctrl["report_id"] != it["report_id"] else ""),
                "experiment": exp, "timepoint": tp, "comparison": f"{it['arm']} vs {ctrl['arm']}",
                "label": f"{'STU-%d' % study if study > 0 else 'RPT-%d' % -study} {exp} {it['arm']}".strip(),
                "m1i": it["mean"], "sd1i": round(sd1, 6), "n1i": it["n"], "m2i": ctrl["mean"], "sd2i": round(sd2, 6),
                "n2i": round(n2, 4), "unit": it["unit"], "value_origin": "+".join(sorted(origins)),
                "derivations": "; ".join(x for x in (d1 and f"int: {d1}", d2 and f"ctrl: {d2}", *note) if x),
                "source_locations": f"{it['source_location']} | {ctrl['source_location']}",
                "row_ids": f"{it['id']};{ctrl['id']}"})
        studies = {d["study"] for d in data}
        for cl in cohort:
            a_, b_ = f"STU-{cl['study_a']}", f"STU-{cl['study_b']}"
            if a_ in studies and b_ in studies:
                flags.append({"type": "overlapping_cohort", "severity": "block",
                              "message": f"{a_} and {b_} linked as {cl['kind']}: {cl['notes'] or ''}"})
        if len(studies) < a.get("min_studies", 2):
            flags.append({"type": "too_few_studies", "severity": "block",
                          "message": f"{len(studies)} studies < min_studies {a.get('min_studies', 2)}"})
        results.append({"analysis": a, "data": data, "excluded": excluded, "flags": flags,
                        "blocking": [f for f in flags if f["severity"] == "block"]})
    return {"analyses": results, "unverified_rows_ignored": unverified}


R_TEMPLATE = r'''# EvidenceDesk reproducible meta-analysis script
# Generated: {generated}
# Plan version: {plan_version}; analysis {aid}: outcome "{outcome}", measure {measure}, REML + Hartung-Knapp
# Input: {csv} (verified extraction rows only; one row per comparison)
suppressPackageStartupMessages(library(metafor))
args <- commandArgs(trailingOnly = TRUE)
base <- if (length(args)) args[1] else "."
dat <- read.csv(file.path(base, "{csv}"), stringsAsFactors = FALSE)
cat("Comparisons:", nrow(dat), " Studies:", length(unique(dat$study)), "\n")
dat <- escalc(measure = "{measure}", m1i = m1i, sd1i = sd1i, n1i = n1i,
              m2i = m2i, sd2i = sd2i, n2i = n2i, data = dat, slab = label)
# Comparisons from the same study are treated as independent only after shared-control splitting;
# blocking double-counting flags must be resolved before this script is generated.
res <- rma(yi, vi, data = dat, method = "REML", test = "knha")
sink(file.path(base, "{aid}_results.txt"))
cat("Analysis {aid}: {outcome} ({measure})\n\n"); print(res)
cat("\nPrediction interval:\n"); print(predict(res))
l1o <- leave1out(res)
cat("\nLeave-one-out:\n"); print(l1o)
sink()
write.csv(as.data.frame(l1o), file.path(base, "{aid}_leave1out.csv"), row.names = TRUE)
write.csv(data.frame(estimate = res$b[1], ci.lb = res$ci.lb, ci.ub = res$ci.ub, pval = res$pval, tau2 = res$tau2,
                     I2 = res$I2, k = res$k, test = "knha", method = "REML"),
          file.path(base, "{aid}_summary.csv"), row.names = FALSE)
png(file.path(base, "{aid}_forest.png"), width = 1800, height = 300 + 90 * nrow(dat), res = 200)
forest(res, header = c("Study / comparison", "{measure} [95% CI]"), xlab = "{measure}")
invisible(dev.off())
pdf(file.path(base, "{aid}_forest.pdf"), width = 9, height = 2 + 0.35 * nrow(dat))
forest(res, header = c("Study / comparison", "{measure} [95% CI]"), xlab = "{measure}")
invisible(dev.off())
writeLines(capture.output(sessionInfo()), file.path(base, "sessionInfo.txt"))
cat("done\n")
'''


def export_analysis(db: DB, review_id: int, out: Path, run_r=True, allow_blocking=False) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    p = get_plan(db, review_id)
    if not p:
        raise ValueError("no analysis plan saved")
    ds = build_dataset(db, review_id, p["plan"])
    summary = {"plan_version": p["version"], "analyses": []}
    for res in ds["analyses"]:
        a = res["analysis"]
        csvname = f"{a['id']}_dataset.csv"
        write_csv(out / csvname, res["data"])
        (out / f"{a['id']}_checks.json").write_text(json.dumps({"flags": res["flags"], "excluded": res["excluded"]},
                                                               indent=1, default=str))
        entry = {"id": a["id"], "comparisons": len(res["data"]), "flags": res["flags"], "excluded": res["excluded"],
                 "script": None, "r": None}
        if res["blocking"] and not allow_blocking:
            entry["status"] = "blocked"
        else:
            script = R_TEMPLATE.format(generated=now(), plan_version=p["version"], aid=a["id"], outcome=a["outcome"],
                                       measure=a["effect_measure"], csv=csvname)
            sp = out / f"{a['id']}_analysis.R"
            sp.write_text(script)
            entry["script"] = str(sp)
            entry["status"] = "script_ready"
            if run_r and shutil.which("Rscript"):
                pr = subprocess.run(["Rscript", str(sp), str(out)], capture_output=True, text=True, timeout=300)
                entry["r"] = {"returncode": pr.returncode, "stdout": pr.stdout[-3000:], "stderr": pr.stderr[-3000:]}
                entry["status"] = "ran" if pr.returncode == 0 else "r_failed"
                sfile = out / f"{a['id']}_summary.csv"
                if sfile.exists():
                    import csv
                    entry["summary"] = list(csv.DictReader(open(sfile)))[0]
        summary["analyses"].append(entry)
    summary["unverified_rows_ignored"] = ds["unverified_rows_ignored"]
    (out / "analysis_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary


# ---------------------------------------------------------------- extraction rows
ROW_FIELDS = ["experiment", "model", "outcome", "outcome_type", "timepoint", "timepoint_value", "timepoint_unit", "arm",
              "arm_role", "control_group_id", "n", "mean", "sd", "se", "ci_low", "ci_high", "ci_level", "median",
              "iqr_low", "iqr_high", "events", "unit", "direction", "value_origin", "derivation", "source_location",
              "source_passage", "notes", "study_id"]


def propose_row(db: DB, review_id: int, report_id: int, row: dict, actor: str, actor_type="agent") -> int:
    if row.get("value_origin") not in ("reported", "calculated", "figure_derived", "author_provided"):
        raise ValueError("value_origin must be reported | calculated | figure_derived | author_provided")
    if not row.get("outcome") or not row.get("arm"):
        raise ValueError("outcome and arm are required")
    if not row.get("source_location"):
        raise ValueError("source_location is required (page/table/figure/section)")
    if row.get("arm_role") not in (None, "intervention", "control"):
        raise ValueError("arm_role must be intervention | control")
    if row.get("value_origin") == "calculated" and not row.get("derivation"):
        raise ValueError("calculated values need a derivation")
    clean = {k: row.get(k) for k in ROW_FIELDS if k in row}
    clean.setdefault("outcome_type", "continuous")
    status = "proposed" if actor_type == "agent" else "entered"
    return db.insert("extraction_rows", {"review_id": review_id, "report_id": report_id, **clean,
                                         "actor_type": actor_type, "actor": actor, "status": status,
                                         "created_at": now()})


def verify_row(db: DB, row_id: int, reviewer: str, edits: dict | None = None) -> None:
    upd = {k: v for k, v in (edits or {}).items() if k in ROW_FIELDS}
    upd.update({"status": "verified", "verified_by": reviewer, "verified_at": now()})
    db.update("extraction_rows", row_id, upd)


def reject_row(db: DB, row_id: int, reviewer: str, note: str) -> None:
    db.update("extraction_rows", row_id, {"status": "rejected", "verified_by": reviewer, "verified_at": now(),
                                          "notes": note})
