# EDQL — the EvidenceDesk query language

One query, written once, compiled to every enabled source. The web editor and the agent API use the same
parser (`evidencedesk/query/parser.py`), compiler (`evidencedesk/query/compilers.py`) and execution engine
(`evidencedesk/engine.py`).

## Example

```
SOURCES ALL
PUBDATE 2015 TO *

TITLE_ABSTRACT(
  (ANGPTL3 OR "angiopoietin-like protein 3")
  AND
  (CRISPR OR "genome editing" OR "base editing" OR "prime editing")
)
OR TITLE_ABSTRACT(CTX310 OR "CTX-310")
```

## Grammar (EBNF)

```
query       = { directive NEWLINE } expr ;
directive   = "SOURCES" ( "ALL" [ "EXCEPT" source { "," source } ] | source { "," source } )
            | "PUBDATE" [ date ] ( "TO" | ".." ) [ date | "*" ] ;
date        = YYYY | YYYY-MM | YYYY-MM-DD ;
source      = "pubmed" | "europepmc" | "ctgov" | "openalex" | "scopus" | "embase" | "wos" | "sciencedirect" ;

expr        = and_expr { "OR" and_expr } ;
and_expr    = primary { ( "AND" | "NOT" ) primary } ;        (* "A NOT B" means A AND NOT B *)
primary     = "(" expr ")" | field "(" expr ")" | near | vocab | term ;
field       = "TITLE" | "ABSTRACT" | "TITLE_ABSTRACT" | "ALL_FIELDS" | "AUTHOR" | "JOURNAL" | "KEYWORDS" ;
near        = "NEAR/" INT "(" term "," term ")" ;             (* INT 1..50; any order *)
vocab       = ( "MESH" | "EMTREE" ) "(" heading { "," flag } ")" ;
flag        = "NOEXP" | "EXP" | "MAJOR" ;
term        = WORD [ "*" ] | PHRASE ;                          (* PHRASE = "double quoted" *)
comment     = "#" to end of line ;
```

Rules

* Operators `AND`, `OR`, `NOT` and all field/function names are **upper case**. A lower-case `and` between
  terms is a syntax error ("not an operator"); a lower-case operator word elsewhere produces a warning.
* Precedence: `NOT` > `AND` > `OR`. Mixing `AND`/`OR` without parentheses produces a warning; every compiled
  native query is fully parenthesised exactly as EDQL interpreted it, so platform precedence quirks
  (PubMed left-to-right, Scopus OR-before-AND) cannot change the meaning.
* Adjacent terms without an operator are an error (`TITLE(gene editing)` → quote it or add `AND`).
* Pure negation (`NOT a`) is an error.
* Field scopes apply to everything inside them; a different field nested inside another field is an error.
  Unscoped terms use `ALL_FIELDS`, which by definition means *the platform's own all-fields index* (shown as a
  semantic difference for every source).
* Truncation: trailing `*` only, at least 3 leading characters. `?` is not supported.
* `MESH(...)` / `EMTREE(...)` cannot be inside a field scope. Default is exploded; `NOEXP` disables explosion,
  `MAJOR` restricts to major topic.
* `NEAR/n(a, b)`: both operands within n intervening words, either order. Operands are single words or phrases.

## Translation fidelity

For every source the preview shows the exact native query, the platform, the field mapping and a list of
notes with a severity:

| severity | meaning | effect |
|---|---|---|
| info | tokenisation/cosmetic | none |
| semantic | documented platform behaviour that cannot be switched off (stemming, plural lemmatisation, what "all fields" covers, which date field is used) | shown; does not block |
| approximation | the native query changes field scope or structure (e.g. PubMed `[tiab]` also searches author keywords; registry fields stand in for an abstract; year-granular dates) | the source is held in `awaiting_approval` until approved for that run (`approve=["pubmed"]` or the checkbox in the UI) |
| unsupported | no native construct exists (e.g. OpenAlex proximity, ClinicalTrials.gov truncation, Emtree in PubMed) | not executed automatically; the native query is offered for manual execution + import |

No clause is ever removed from a native query without a note; a unit test checks that every term appears in
every compiled query. Platform-side alterations reported at run time (e.g. PubMed "phrases ignored") are stored
on the source run as `ALERT` notes and set its status to `complete_with_alerts`.

## Per-source support (as implemented and verified)

| construct | PubMed | Europe PMC | OpenAlex | ClinicalTrials.gov | Scopus | Embase.com | WoS Starter | ScienceDirect |
|---|---|---|---|---|---|---|---|---|
| TITLE | `[ti]` | `TITLE:` | `title.search.exact` | BriefTitle/OfficialTitle/Acronym | `TITLE()` | `:ti` | `TI=` | `title` |
| ABSTRACT | ≈ `[tiab]` (approx.) | `ABSTRACT:` | `abstract.search.exact` | ≈ BriefSummary/DetailedDescription | `ABS()` | `:ab` | ≈ `TS=` | ≈ `tak` |
| TITLE_ABSTRACT | ≈ `[tiab]` (+author keywords) | `TITLE: OR ABSTRACT:` | `title_and_abstract.search.exact` | ≈ titles + summaries | `TITLE() OR ABS()` | `:ti,ab` | ≈ `TS=` | ≈ `tak` |
| ALL_FIELDS | `[all]` | default | `default.search` | default Essie | `ALL()` | default | `TS=` | `qs` |
| AUTHOR / JOURNAL / KEYWORDS | `[au]` `[ta]` `[ot]` | `AUTH:` `JOURNAL:` `KW:` | unsupported | –/–/`Keyword` | `AUTH` `SRCTITLE` `AUTHKEY` | `:au` `:jt` `:kw` | `AU` `SO` ≈`TS` | `authors` `pub` ≈`tak` |
| phrase | `"x y"[tag]` | `"x y"` | `"x y"` | `"x y"` with `EXPANSION[Term]` | `{x y}` exact | `'x y'` | `"x y"` | `"x y"` |
| truncation `*` | yes (600-variant cap noted) | words only | words only (exact field) | unsupported (verified: ignored) | yes | yes | yes | unsupported |
| NEAR/n | `"a b"[tiab:~n]` (ti/tiab only) | `"a b"~n OR "b a"~n` | unsupported | unsupported | `W/n` | `NEAR/n` | `NEAR/n` | unsupported |
| MESH | `[Mesh]`, `[Mesh:NoExp]`, `[Majr]` | ≈ `MESH:` | unsupported | ≈ Condition/Intervention MeSH | ≈ `INDEXTERMS` | ≈ mapped to Emtree | unsupported | unsupported |
| EMTREE | unsupported | unsupported | unsupported | unsupported | ≈ `INDEXTERMS` | `/exp`, `/de`, `/mj` | unsupported | unsupported |
| PUBDATE | `[dp]` range | `FIRST_PDATE` | `from/to_publication_date` | ≈ StudyFirstPostDate | ≈ `PUBYEAR` (years) | ≈ `/py` (years) | ≈ `PY` (years) | ≈ `date` (years) |
| cross-field OR | yes | yes | union of several requests (exact) | yes | yes | yes | yes | union of requests |

Live-verified details: ClinicalTrials.gov Essie by default adds synonyms (BriefTitle `"heart attack"` 66 →
1,365 trials), so EDQL emits `EXPANSION[Term]` (no synonyms; plurals still match). `EXPANSION[None]` was
rejected because it is case-sensitive. OpenAlex `.search` filters stem words; EDQL uses the `.search.exact`
variants (wildcards are only allowed there).

## Errors

Errors carry line, column, the offending span, and a hint, e.g.

```
line 9, col 28: Missing operator between 'ANGPTL3' and 'liver'
  AND TITLE_ABSTRACT(ANGPTL3 liver)
                             ^^^^^
  hint: Join clauses with AND / OR / NOT, or quote a multi-word phrase (e.g. "ANGPTL3 liver").
```

## Autocomplete

`complete_query(text, pos)` returns operators, fields, functions, source names after `SOURCES`, PICOS search
terms from the review protocol, and live MeSH descriptors inside `MESH("…` (NLM MeSH lookup API).
