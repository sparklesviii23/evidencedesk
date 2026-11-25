# Credential checklist

Configure in the web UI (**Sources & keys**) or with `evidencedesk set-credential <source> <key>` (prompted,
not echoed). Secrets go to the OS keychain (macOS Keychain) or, without a keychain, to
`~/.evidencedesk/credentials.json` (mode 0600). They are redacted from logs, stored error messages and every
export, and are never available to MCP tools. Environment variables `EVIDENCEDESK_<SOURCE>_<KEY>` override
stored values (useful for CI).

| # | Source | What you need | Where to get it | Required? | After adding |
|---|---|---|---|---|---|
| 1 | Contact e-mail (`contact.email`) | Your e-mail address | — | **Yes for Unpaywall**; recommended for NCBI, OpenAlex and Crossref polite pools | Test Unpaywall |
| 2 | PubMed (`pubmed.api_key`) | NCBI API key | NCBI account → Settings → API Key Management | Optional (3 → 10 requests/s) | Test PubMed |
| 3 | OpenAlex (`openalex.api_key`) | Free API key | openalex.org/settings/api | Strongly recommended: anonymous use shares a small daily budget per IP and is rate-limited under load | Test OpenAlex |
| 4 | Crossref (`crossref.plus_token`) | Metadata Plus token | Crossref (paid) | Optional | — |
| 5 | Scopus (`scopus.api_key`, `scopus.insttoken`) | Elsevier API key **and** institutional Scopus subscription; requests must come from a subscriber IP or carry an institutional token (`insttoken`, issued by Elsevier to the institution). Abstracts require COMPLETE view entitlement | dev.elsevier.com → My API Key; insttoken via your library/Elsevier | Needed for automated Scopus | Test Scopus, then run a search; if COMPLETE is refused the adapter falls back to STANDARD (no abstracts) and says so |
| 6 | ScienceDirect (`sciencedirect.api_key`, `.insttoken`) | Elsevier API key + ScienceDirect entitlement | dev.elsevier.com | Needed for automated ScienceDirect | Test ScienceDirect |
| 7 | Embase (`embase.api_key`, `.insttoken`) | Institutional **Embase API** entitlement (separate contract; most institutions only have embase.com or Ovid) | Elsevier account manager | Rarely available | Without it: run the compiled Embase.com query on embase.com/Ovid and import the RIS export |
| 8 | Web of Science (`wos.api_key`) | Clarivate Web of Science **Starter API** key (free trial or institutional plan) | developer.clarivate.com | Needed for automated WoS | Test WoS |
| 9 | Ovid (MEDLINE/Embase/PsycINFO), Cochrane CENTRAL, CINAHL (EBSCO), conference portals, ICTRP | Your institutional login in the vendor UI | — | Import route | Run the native query shown in Preview, export RIS/CSV/XML, then **Search › Import** with platform, exact query and search date |

## Verification status of this build

* Tested live without credentials: PubMed, Europe PMC (search + OA full-text XML + supplementary files),
  ClinicalTrials.gov (search + posted results), OpenAlex (anonymous: search, DOI lookup, citations),
  Crossref (DOI metadata, licences, links), Unpaywall (with a generic contact address), NLM MeSH lookup.
* Implemented from vendor documentation, **not verified live** (no credentials available): Scopus,
  ScienceDirect, Web of Science Starter. They show `DISCONNECTED` until keys are added; the first successful
  connection test and search will confirm them.
* Embase: no publicly documented general search API; the adapter reports `access` and the workflow is
  compiled native query → manual run → import.
* Aggregators (e.g. AnyAPIKey): not used. If you add one, verify the underlying database, query syntax,
  pagination and provenance first; EvidenceDesk records the platform for every run.
