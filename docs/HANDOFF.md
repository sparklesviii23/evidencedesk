# Handoff — EvidenceDesk v0.1 (2026-11-23)

## What was built

A runnable local app (`evidencedesk serve`): FastAPI + SQLite + vanilla-JS UI, a stdio MCP server
(`evidencedesk-mcp`, 73 tools) and a REST API (`/api/op/*`) — all three call one service layer
(`evidencedesk/service.py`). Docs: [README](../README.md), [query language](QUERY_LANGUAGE.md),
[credential checklist](CREDENTIALS.md), [agent guide](AGENT_GUIDE.md).

## Connector status

| Source | Status | How verified |
|---|---|---|
| PubMed (E-utilities) | **Tested live** | search, efetch paging, query translation + phrase warnings captured; >10k date-slicing unit-tested with mocked counts (not live) |
| Europe PMC | **Tested live** | search (cursorMark), abstracts, OA JATS XML, supplementary-files zip |
| ClinicalTrials.gov v2 | **Tested live** | Essie syntax verified field by field; registry JSON incl. results/adverse events extracted |
| OpenAlex | **Tested live (anonymous)** | `.search.exact` filters, multi-request union, DOI lookup, citation chasing code path; anonymous budget is tiny — add a free key |
| Crossref | **Tested live** | DOI metadata, licences, TDM links (only CC-licensed links downloaded) |
| Unpaywall | **Tested live** | with Unpaywall's documented example address; set your own contact e-mail |
| PMC Cloud Service (AWS Open Data) | **Tested live** | JATS XML / PDF / media for OA articles and author manuscripts, licence recorded |
| NLM MeSH lookup | **Tested live** | editor autocomplete inside `MESH("…` |
| Scopus | Needs institutional credentials | implemented from Elsevier docs; shows DISCONNECTED; not verified live |
| ScienceDirect | Needs institutional credentials | same |
| Web of Science Starter | Needs Clarivate API key | same |
| Embase | Import route | no public general search API; compiled Embase.com query → manual run → RIS import |
| Ovid, CENTRAL, CINAHL, conference portals, ICTRP | Import route | RIS, MEDLINE/nbib, BibTeX, CSV, PubMed XML, EndNote XML importers (unit-tested with sample files, not with real vendor exports) |

Found during live testing and fixed: Essie's default synonym expansion widens results (BriefTitle "heart attack"
66 → 1,365), so EDQL emits `EXPANSION[Term]`; OpenAlex stems `.search` filters, so `.search.exact` is used;
Europe PMC's rendered-PDF route sits behind an anti-bot challenge (removed; bot challenges are logged as
`bot_blocked`); Europe PMC returns HTTP 500 for non-OA `fullTextXML` (now recorded as not available without long
retries); the NCBI PMC OA web service is gone (replaced by the PMC Cloud Service route).

## Vertical slice on the ANGPTL3 query (live, 2026-11-23)

* Sources: PubMed 66, Europe PMC 69, OpenAlex 119, ClinicalTrials.gov 2 → 256 records → 131 reports
  (125 duplicates removed by identifier). Scopus/Embase/WoS/ScienceDirect: DISCONNECTED (no keys).
* 10 uncertain matches queued, not merged: preprint vs article, conference abstract vs article, Zenodo data
  deposit vs article, NEJM letter vs reply sharing one DOI. All resolved as same-study links or not-duplicates.
* OpenAlex returned off-topic "CTX310" machine-tool papers. That is real noise the query picks up; screening
  excluded them.
* Full text for 71 reports at the full-text stage: 66 assessed, 5 not retrieved; 185 s with 4 workers. Every
  attempt is logged per route.
* PRISMA: all 7 reconciliation identities pass. The evidence packet has 71 sections, a 701-page searchable PDF
  with TOC and bookmarks, a Markdown copy, per-report text files, a manifest, originals and CSVs. A sample is
  in `examples/angptl3_live_run/`.
* **Caveat:** the screening decisions in this run came from `tests/e2e_workflow.py`, which plays the agent
  (rule-based proposals with verbatim passages) and two scripted reviewers with planted disagreements. They
  exercise the workflow; they are **not** real eligibility judgements. The "55 included reports / 53 studies"
  figure is not a review result.

## Recall

**No real recall figure is available yet.** It needs your manually screened ANGPTL3 records:
`benchmark_import` then `benchmark_recall`. A pilot set I built (8 items: 6 relevant, 2 deliberate decoys)
exercised the tooling. It found 6/8 and diagnosed both misses as `strategy_gap`: the query lacks the synonym
"angiopoietin-like 3". The diagnosis names the failing concept block and quotes the record wording. Consider
adding that synonym to the real strategy.

## Tests

`pytest tests -q` → **40 passed**. Covered:
* parser errors and warnings; no-dropped-clause property; fidelity classes; proximity and vocabulary
* dedup rules incl. undo and same-study linking; importers
* screening resolution, adjudication and passage verification; PRISMA reconciliation
* evidence PDF/TOC/manifest
* analysis with shared-control splitting, double-counting block, R metafor REML + Hartung–Knapp, forest and
  leave-one-out
* OCR of a scanned PDF and visible extraction failure; secret redaction (incl. log args)
* retries/pagination/resume after failure; zero results vs access failure; approximation hold

Also run: `tests/mcp_smoke.py` (real stdio MCP client, live search) and `tests/e2e_workflow.py` (REST, live).

## Known limits / next steps

* Licensed connectors are unverified until real keys are tested.
* The meta-analysis path is verified on a synthetic fixture only. No real verified extraction rows exist yet,
  because extraction needs a human to verify the rows.
* Risk-of-bias data is stored and exported, but there are no tool-specific forms (RoB 2, SYRCLE) yet.
* Auto-located passages in the packet are keyword pointers, labelled unverified.
* Overlapping cohorts and shared controls are recorded manually (cohort links). They are not detected
  automatically.
* PRISMA diagram is SVG only (no PDF/DOCX). Packets with supplements can be large (240 MB here).
* A search runs in the process that started it (UI server or MCP server). It resumes from its cursor after a
  restart.
