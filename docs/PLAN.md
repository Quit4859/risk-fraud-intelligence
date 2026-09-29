# PLAN — Risk, Fraud & Regulatory Intelligence Copilot

Status: **awaiting approval** (Phase 0 deliverable)
Repo: `/workspaces/risk-fraud-intelligence` · Branch: `main` · Head: `f3a4a90`
Date: 2026-09-29

---

## 0. Executive summary

The repo today is a **strong, honest, deterministic compliance engine** with a weak
surface layer and **zero real Snowflake execution**. Every number it publishes is
computed locally in sqlite. The judged artefact is currently "dialect-compatible SQL
plus a rules engine"; the brief asks for "actually runs on Snowflake with Cortex".

Three things dominate the score, and they are not the UI:

1. **Nothing has ever executed against Snowflake.** `sql/01`–`sql/04` have never been
   run. They are plausible, but unverified. Any dialect error in them is a live
   credibility risk in front of judges who know Snowflake.
2. **There is no visible CoCo usage anywhere in the repo.** Gaps G1–G6 in the brief.
   This is scored explicitly and is currently zero.
3. **The headline answers are wrong.** 4 of 12 homepage example questions abstain on a
   cold start, and structuring detection has 47% precision. A judge who clicks the
   first example chip and reads "I need a question to work from" ends the demo there.

The audit brief understated B1 (4 of 12 examples fail, not 1) and **wrongly listed
four-eyes approval as an existing strength — it does not exist**. See §2.4.

**One decision is required before Phase 1: see §9.** There are no Snowflake
credentials in this environment, and Phase 1 as written is unexecutable without them.

---

## 1. Current-state audit

### 1.1 What is real and worth protecting

These are the assets. Every phase must leave them intact and regression-tested.

| Asset | Location | Notes |
|---|---|---|
| 6 deterministic SQL detectors | `backend/agents/fraud_detector.py` (956 L) | Score + confidence + reasons + **evidence txn IDs**. No LLM. This is the right design. |
| Clause-cited answers | `policy_matcher.py`, `answer_composer.py` | Every regulatory sentence carries a `clause_ref`. |
| 6 guardrails | `backend/orchestration/guardrails.py` | G1 evidence, G2 quality, G3 confidence, G4 citation, G5 PII, G6 rule health. |
| Abstention | `guardrails.py::check_answer` | Below threshold → abstain and state the gap. |
| Evidence manifest hash | `evidence_gatherer.py` | SHA-256 over the evidence set, stamped on every finding. |
| Append-only audit log | `orchestration/audit_log.py` | Per-run, per-action, with rationale. |
| Corpus chunker + BM25 | `agents/retrieval.py` | 333 lines of 6 frameworks, dependency-free. |
| Config-driven thresholds | `config/settings.yaml` | Every decision threshold is config, not literal. |
| `as_of` anchoring | `warehouse.py::as_of()` | All windows anchor to dataset date, never `now()`. Replayable. |

**Design principle to keep: the LLM must never be the thing that decides.** Cortex
generates prose and SQL; the detectors and guardrails decide. Adding Cortex must
*not* weaken any existing guarantee — it only adds reach (open-ended questions) and
draft quality, both of which must pass the same guardrails.

### 1.2 Runtime shape today

```
wsgi.py  ──▶  api/index.py::handler  ──▶  api/_runtime.py::get_copilot
                                                │
                                                ▼
                    backend.orchestration.copilot::RiskCopilot
                                                │
        ┌───────────────┬───────────────┬───────┴────────┬──────────────┐
   intent_router   executors      fraud_detector   policy_matcher   guardrails
   (19 regex       (17 intents)   (6 detectors)    (BM25+bindings)  (G1..G6)
    intents)             │                │                 │
                        └────────► warehouse.py ◄──────────┘
                                    (sqlite3)
                                        │
                              data/raw/*.csv (generated)
```

Cold start: 1.95 s on the demo profile (200 customers / 34,269 txns).

### 1.3 Deployment state

| File | Status |
|---|---|
| `render.yaml` | **Invalid.** Uses `serviceType`/`envFiles`/`build.command`/`runCommand` — not Render blueprint schema. Also references a non-existent `.env.render`. |
| `Procfile` | Redundant with render.yaml. |
| `vercel.json` | Redundant; also declares a cron hitting `GET /api?action=scan`, an **unauthenticated write via GET**. |
| `package.json`, `next.config.js`, `pages/` | Dead Next.js scaffold. `.next/` present on disk. |
| `/healthz` | **Absent.** Render free tier has no health check path configured. |
| `.github/` | **Absent.** No CI at all. |

### 1.4 Dependency reality check

Installed: `pyyaml`, `requests`, `gunicorn`, stdlib. **Not installed:** `pytest`,
`snowflake-connector-python`, `streamlit`, `sqlglot`. `scripts/verify_vercel.py` is a
180-line custom reimplementation of checks the platform does for free.

---

## 2. Bugs confirmed by reproduction

Every item below was **reproduced on the deployed 200-customer demo profile**, not
read off the brief. Commands and outputs are in `docs/evidence/audit/`.

### 2.1 B1 — 4 of 12 example questions abstain (brief said 1)

Boot with a clean warehouse, then loop `EXAMPLE_QUESTIONS`:

```
 0.05s conf=0.81 PASS rows= 30  which customers are structuring cash...
 0.00s conf=0.16 FAIL rows=  0  show the top 10 highest risk customers
 0.17s conf=0.99 PASS rows=  1  assess the risk profile of customer CUST-000123
 0.12s conf=0.30 FAIL rows=  0  why was CUST-000123 flagged?
 0.15s conf=0.92 PASS rows=  5  do I need to file a SAR for CUST-000123?
 0.03s conf=0.92 PASS rows=  1  which mule network clusters are active?
 0.00s conf=0.92 PASS rows=  1  what does the regulation say about PEPs?
 0.00s conf=0.81 PASS rows=  3  what is our liquidity position...
 0.00s conf=0.92 PASS rows=  3  explain the structuring rule
 0.00s conf=0.66 PASS rows=  5  show vulnerable obligors above the 10% PD threshold
 0.00s conf=0.45 WARN rows=  3  where are we geographically concentrated...
 0.00s conf=0.10 FAIL rows=  0  what is the current portfolio risk position?
```

**Root cause:** `findings` is empty on a cold start. `top_risks`,
`portfolio_summary` and `explain_signal` all read `FROM findings`
(`executors.py:105,136`). No scan has run, so there are no rows, so guardrail A3
(`answer_has_no_supporting_rows`) forces FAIL, so the answer abstains. The
abstention is **correct behaviour** — the defect is that the app ships in a state
where the primary demo path is unavailable.

Also note: `explain_signal` fails even for `CUST-000123`, which the *previous* question
in the list successfully assessed. Route order and prior-findings coupling both
contribute.

**Fix:** boot-time population scan (background thread, `gunicorn --preload`) +
lazy scan in the executor when `findings` is empty. Regression test loops all 12
examples and asserts each returns PASS or abstains **with a stated gap**.

### 2.2 B2 — Structuring precision is 0.467 (brief said "too noisy", now quantified)

```
structuring question -> 30 rows of 200 customers (15%)
TP=14  FP=16  FN=0  Precision=0.467  Recall=1.000  F1=0.636
```

Recall is already 1.0. **Only precision needs to improve**, so the DoD ("improved
precision at >= the same recall") is achievable without touching the planted cases.

**Root cause — quantified.** The candidate band is `amount_floor=2000` to
`amount_ceiling=10000`. Measured on the demo profile:

```
customers with >=3 cash deposits in the 2k-10k band: 170
  of which benign controls:                             101
benign cash deposits: n=10,563  min=73.8  max=825,889  mean=14,011
```

**101 of 119 benign controls (85%) have ≥3 deposits inside the candidate band.**
The detector then only requires 3 in-band deposits summing over 10,000 within 72 h.
Ordinary cash-using customers pass by arithmetic accident. The generator's baseline
`generate_baseline_txns` emits cash in the same range as the planted scenario, so
benign and malicious are not separable at the current thresholds.

The regex rule already scores well; the **threshold band** is the defect. A real AML
team would not accept 30 SARs per 200 customers.

**Fix** (config-first, detector second, generator last):
- Tighten the band to a configurable fraction of threshold (default 0.85–0.999), so
  the signal is *deliberately* just-below, which is what structuring *is*.
- Require ≥3 deposits in 72 h **and** aggregate > CTR threshold **and** a
  corroborating signal (pass-through withdrawal or new counterparty) for HIGH.
- Fix the generator so benign customers cannot accidentally produce the pattern
  (benign cash deposits must not all sit in the 85–99.9% band).
- Publish before/after P/R/F1/FPR in `artifacts/evaluation_report.md`.

### 2.3 B3 — 40.5% fraud prevalence (confirmed)

```
population 200 · fraud 81 · benign_control 119
planted: structuring 14, takeover 16, geographic 18, mule 23, trade 10 = 81  (40.5%)
```

Counts are **absolute, not proportional** (`generate_synthetic_data.py:684-689`), so
the 600-customer profile has 81/600 = 13.5% while the deployed 200-customer profile
has 40.5%. Every rate in the brief's B4 complaint traces back to this. Scale counts to
5–8% per profile.

### 2.4 NEW — Four-eyes approval does not exist (brief lists it as an existing strength)

```
generated_by: risk-fraud-regulatory-copilot   status: PENDING_REVIEW
approve(approver='risk-fraud-regulatory-copilot')  -> success: True   <-- SELF-APPROVAL
approve(approver='bot')                          -> success: False  (blocked)
approve(approver='a.b@bank.com')                 -> success: True
approve(approver='c.d@bank.com')                 -> success: True   <-- RE-APPROVAL of APPROVED
```

`report_formatter.py:451` `approve()`:
- does **not** compare `approver` to `governance.generated_by` → **preparer can
  approve their own filing**, which is the exact failure four-eyes exists to prevent;
- does **not** check current status → an already-`APPROVED` filing can be re-approved,
  silently overwriting `approver` and `approved_at`;
- has no status transition guard, so the audit trail shows two competing approvals.

The `bot` rejection works and must be preserved. I am flagging this explicitly because
the brief instructs me not to break "four-eyes approval" as an existing strength —
it is a **gap to build**, not a strength to protect. I will not weaken it further.

### 2.5 B4 — README metrics vs deployed profile (confirmed)

`README.md:139` "produces 600 customers", `README.md:241` "Scored against the planted
labels over all 600 customers". Deployed default is `RISK_PROFILE=demo` = 200
(`api/_runtime.py:40-47`). No `evaluate` API action exists. The README quotes numbers
from a profile that is **not running**.

### 2.6 B5 — structuring window: config says 7, code and docs say 72 (confirmed)

`settings.yaml:63` `lookback_days: 7`; `fraud_detector.py:104-116` uses it as the
**candidate pull** window, then `fraud_detector.py:127-129` hardcodes `if gap > 72`.
README:204 says 72 hours. So there are three values and two meanings, and the one a
compliance officer would tune is the one that does nothing.

Fix: split into `candidate_lookback_days` (data pull) and `aggregation_window_hours:
72` (the clause-bound number), make the detector read both from config, and make the
citation text derive from config so it can never drift again.

### 2.7 B6 — `_json` produces invalid JSON (confirmed)

```python
api/index.py:63   "body": json.dumps(payload, default=str)[:200000]
```

400-row payload → body length exactly 200000, `json.loads` raises
`Unterminated string starting at: line 1 column 199184`. **Any response over ~200 KB
is unparseable by the client.** Fix: paginate rows, return `total` + `truncated: true`.

### 2.8 B7 — Path traversal in static server (confirmed, worse than reported)

```python
wsgi.py:37  os.path.join(base, path)     # no realpath containment
wsgi.py:44  os.path.isfile(candidate)     # no extension allowlist
```

`_STATIC_EXTS` is **defined at line 31 and never used**.

```
'/../wsgi.py'               -> SERVED  (Content-Length 5263 — source code disclosed)
'/../config/settings.yaml'  -> SERVED  (Content-Length 7896 — every detection
                                               threshold disclosed to any visitor)
'/../../etc/passwd'         -> None   (only because /etc/passwd is absent, not by design)
'/index.html'               -> SERVED
```

Parent traversal escapes `public/` and serves arbitrary files as `application/octet-stream`.
In a compliance product this is the worst of the functional bugs: it hands an attacker
the exact threshold configuration that G3/G4 guardrails are built on, so every
detection can be trivially evaded. Fix: `os.path.realpath` containment check against
`STATIC_DIR`/`PAGE_DIR` roots plus the allowlist; proper reason-phrase status lines.

### 2.9 B8 — Unauthenticated writes + wildcard CORS (confirmed)

`api/index.py:57-62` sets `Access-Control-Allow-Origin: *` on every response, and
`scan` / `file` / `approve` / `escalate` have no authentication. `vercel.json` compounds
this by invoking `action=scan` over **GET** from a cron. Any browser on the internet can
file and approve SARs in the demo.

### 2.10 B9 / B10 / B11 / B12 / B13 — confirmed by inspection

- **B9** `render.yaml` invalid blueprint schema (see §1.3).
- **B10** Next.js scaffold committed; `.next/` build cache on disk.
- **B11** `tests/` contains `__init__.py` and `test_probe.txt`. **No test exists.** No CI.
- **B12** No `/healthz`.
- **B13** `README.md` references a Streamlit app that is not in the repo.

### 2.11 Verified syntax baseline (checked against official docs before planning)

To avoid guessing, I confirmed current syntax for the objects this project needs:

| Object | Verified source | Key detail that would have been guessed wrong |
|---|---|---|
| `CREATE SEMANTIC VIEW` | `sql-reference/sql/create-semantic-view` | `FACTS` **must precede** `DIMENSIONS`; `AI_VERIFIED_QUERIES (name AS (QUESTION '…' SQL '…'))`; `WITH SYNONYMS` on tables/facts/dims/metrics. |
| Semantic view YAML | `user-guide/views-semantic/semantic-view-yaml-spec` | Docs now recommend semantic views; YAML via `SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML`. Legacy YAML retained for compatibility. |
| `CREATE AGENT` | `sql-reference/sql/create-agent` | `FROM SPECIFICATION $$ yaml $$`; tool types `cortex_analyst_text_to_sql`, `cortex_search`, `generic`. |
| Cortex Search | `sql-reference/sql/create-cortex-search` | Multi-index `TEXT INDEXES … VECTOR INDEXES …` is the current form; a service with **no** vector index errors. |
| `AI_COMPLETE` | `sql-reference/functions/ai_complete` | `AI_COMPLETE` supersedes `COMPLETE`. Optional `return_error_details` BOOLEAN. |
| `AI_PARSE_DOCUMENT` | `sql-reference/functions/ai_parse_document` | `AI_PARSE_DOCUMENT(<file_object>[, <options>][, <return_error_details>])`. |

Two corrections to assumptions in the brief, from the docs:
- **`AI_EXTRACT` is not the right primitive** for pulling clauses out of free-form
  policy PDFs. The documented path is `AI_PARSE_DOCUMENT` for extraction (OCR/layout)
  plus `AI_COMPLETE` to structure clauses into rows. I will use that pairing.
- **Snowflake is not "just dialect-compatible" here** — `AI_PARSE_DOCUMENT`,
  `AI_COMPLETE`, Cortex Search and Semantic Views have **no sqlite equivalent**, so
  those paths must be written against the Snowflake engine specifically with an
  explicit local degradation, not ported through `to_sqlite()`.

---

## 3. Target architecture

```
                       ┌──────────────── browser ────────────────┐
                       │  public/  vanilla JS + CSS, no build,   │
                       │  no CDN, inline SVG charts, 9 views      │
                       └───────────────┬──────────────────────────┘
                                       │ same-origin, session cookie
                       ┌───────────────▼──────────────────────────┐
                       │  wsgi.py (gunicorn --preload)            │
                       │  /healthz (instant, no build)            │
                       │  static: realpath + allowlist            │
                       └───────────────┬──────────────────────────┘
                                       │
                       ┌───────────────▼──────────────────────────┐
                       │  api/index.py  (RBAC + pagination)       │
                       └───┬──────────────────────────────────┬───┘
                           │                                  │
        ┌──────────────────▼───────────┐        ┌─────────────▼─────────────┐
        │  backend.engine              │        │  backend.auth             │
        │   ├ EngineFactory            │        │   analyst│approver│admin │
        │   ├ SnowflakeWarehouse       │        │   HMAC-signed cookie      │
        │   └ SqliteWarehouse (exists) │        │   four-eyes enforced here │
        │  auto-select by env         │        └───────────────────────────┘
        └──────────┬──────────────────┘
                   │  engine: snowflake | local-fallback
     ┌─────────────┼──────────────┬───────────────┬──────────────┐
     ▼             ▼              ▼               ▼              ▼
  sql/01-03    Semantic       Cortex          Cortex         Task DAG
  DDL + tasks  Views          Search          Agent          (03_streams)
  dynamic tbls (04_semantic)  (policy         (Analyst+      + UI panel
  streams/                     clauses)       Search+4       + Run now
  tasks                        + AI_PARSE      custom tools)
                               documents
                   │
        ┌──────────▼──────────────────────────────────────────┐
        │  Existing deterministic core — UNCHANGED            │
        │  6 detectors · 6 guardrails (+G7) · clause binder   │
        │  abstention · evidence manifest · four-eyes · audit │
        └─────────────────────────────────────────────────────┘
                   │  degraded path (each stage independent)
        ┌──────────▼──────────────────────────────────────────┐
        │  Local fallback: sqlite · BM25 · template drafter   │
        │  UI shows "Fallback mode: local engine"             │
        └─────────────────────────────────────────────────────┘
```

**Degradation contract — per stage, not global.** Each Cortex capability has its own
fallback and its own health flag, so the System page can show *which* stages are live
rather than one global on/off. A Snowflake outage must not disable the deterministic
detectors, because those are the part that must never go away.

**The invariant:** Cortex is additive. Removing every Cortex call must still leave a
working, guardrail-enforcing copilot. There will be a test asserting exactly that.

---

## 4. Data model and ontology

Existing entities (Customer, Account, Transaction, CreditPortfolio, Liquidity,
Counterparty, EntityExposure, Finding, Filing, Policy, Clause, AuditEvent) are sound.
`ontology/ontology.yaml` v1.0.0 is good and will be kept, extended with:

**New entities**
- `Obligation` — a binding from extracted clause → detector rule → filing type. This
  is what makes "new obligation detected" diffable.
- `Document` / `DocumentChunk` — already exist as `document_chunks`; extended with
  `source_kind` (`prewritten` | `parsed`), `parse_confidence`, `extracted_at`.
- `TaskRun` — Snowflake task run history surfaced in the Automation view.
- `Agent` — replaces the free-text `actor` on audit rows with a real principal +
  role, so four-eyes is provable from the audit log alone.

**Snowflake layout** (fixes the current `CORE/RAW/SEMANTIC/EVIDENCE/FILINGS` sprawl
into the brief's four):
`RAW` (landing, untouched) · `CURATED` (typed, tagged, masked) · `RISK` (detection
outputs, findings, semantic views) · `GOVERNANCE` (filings, audit, obligations).

**Governance objects** (Phase 1): masking policies on name/PAN-like/account-number
fields; a row access policy by role (analyst sees own book, approver sees all, admin
sees all + PII); PII tags on tagged columns; `SHOW TASKS`/`TASK_HISTORY` surfaced in
the System view as evidence the DAG really runs.

**Semantic view** over customers, accounts, transactions, findings, exposures,
liquidity — with synonyms, relationships, and metrics including
`structuring_amount_72h`, `lcr`, `nsfr`, `large_exposure_pct`, `composite_risk_score`,
plus ≥15 verified queries. Metric definitions must match
`sql/04_semantic_views.sql` exactly; a test asserts parity so the local and Snowflake
answers cannot diverge.

---

## 5. Target workflow

```
 signal ─▶ detect (deterministic, evidence-linked)
            │
            ├─▶ policy match (Cortex Search over clause chunks; BM25 offline)
            │      └─▶ obligation diff vs bound obligations  ─▶ "new obligation" inbox
            │
            ├─▶ guardrails G1..G6 ──▶ finding (score, level, confidence, manifest hash)
            │                                  │
            │                     ┌────────────┴────────────┐
            │                     │                         │
            │              confidence >= floor      confidence < floor
            │                     │                         │
            │              draft filing               human review only
            │              (Cortex COMPLETE,          (abstain + state gap)
            │               evidence-constrained)
            │                     │
            │              guardrail G7: every number/date/ID in the
            │              narrative exists in the manifest; every
            │              regulatory sentence carries a clause citation
            │                     │
            │              fail ─▶ block to human review (never auto-file)
            │              pass ─▶ PENDING_REVIEW
            │                     │
            │              four-eyes: approver ≠ preparer (B-new)
            │                     │
            ├─▶ escalate ─▶ Jira ticket + Slack alert (LIVE | SIMULATED receipt)
            │                     │
            └─▶ audit_log ◀── every step, hashed, actor-attributed
```

**Guardrail G7** is the new one, and it is the reason Cortex output is trustworthy:
the model may only restate facts that are in the evidence manifest. A hallucinated
amount is caught mechanically, not by hoping.

---

## 6. Phase plan

Each phase ends with: tests green → commit → `docs/COCO_LOG.md` entry → CoCo evidence
in `docs/evidence/`. No phase starts before the previous is committed.

### Phase 1 — Snowflake as the primary engine
- `backend/engine/` with `Warehouse` interface; `SnowflakeWarehouse`
  (`snowflake-connector-python`, key-pair + PAT from env) and the existing sqlite one
  behind it. Factory auto-selects; `engine` surfaced in `/api?action=health` + header.
- Execute `sql/01`–`sql/03` for real via CoCo. Fix every dialect error found.
  Provision DB, schemas, warehouse, roles, stage, file formats. `PUT` + `COPY INTO`
  the synthetic CSVs. Dynamic tables, streams, tasks actually created and refreshed.
- Masking policies, row access policy, PII tags. Surface `SHOW TASKS`/`TASK_HISTORY`.
- `.env.example` + gitignored `.env`. **Blocked on §9.**

### Phase 2 — Semantic layer
- Real `CREATE SEMANTIC VIEW` + verified queries (syntax confirmed in §2.11).
- `tests/test_semantic_questions.py`: 25+ NL questions with expected result *shape*,
  run against Cortex Analyst → `artifacts/semantic_eval.md` with accuracy.

### Phase 3 — Cortex intelligence
- Cortex Search service over clause chunks (`framework`, `clause_ref`, `jurisdiction`
  metadata), hybrid retrieval, BM25 retained as fallback.
- Cortex Agent: Analyst + Search + custom tools (`detect_customer`, `run_scan`,
  `draft_filing`, `escalate_case`). Router order stays **deterministic first**, agent
  for open-ended, abstain if neither passes.
- `AI_COMPLETE` with a pinned model drafts narratives from the evidence set only.
  **Guardrail G7** verifies every number/date/ID and every clause citation.
- `docs/incoming/` sample PDFs (regulatory circular, policy memo, prior SAR) →
  `AI_PARSE_DOCUMENT` + `AI_COMPLETE` → obligations table → Cortex Search → UI inbox
  with a diff against existing bindings.

### Phase 4 — Live actions (MCP)
- Real Jira + Slack via env tokens, with simulated fallback. Receipts labelled
  `LIVE` / `SIMULATED` in UI and audit log. Escalation carries finding summary,
  evidence hash and clause refs.

### Phase 5 — Automation + Streamlit
- Task DAG live: ingest → detect → draft → board pack. Automation panel with next/last
  run, status, Run now. Slack/email digest of new HIGH findings.
- `streamlit/app.py` mirroring Alert Queue, Case View, Filing Review.

### Phase 6 — CoCo evidence + skills
- `AGENTS.md`; three documented skills with README, inputs, outputs, example run:
  `fraud-typology-tuner`, `regulatory-filing-drafter`, `semantic-view-verifier`.
  Location and format verified against CoCo docs first.
- `docs/COCO_LOG.md` across all four judged categories (Planning, Development,
  Execution, Testing) with prompts, commands, outputs, screenshots in
  `docs/evidence/`. `docs/DEMO_SCRIPT.md` — 5-minute walkthrough.

### Phase 7 — Guardrails + test polish
- Freshness check ("data as-of X, stale > N hours"), prompt-injection filter on
  questions and parsed documents, PII masking on all outputs, "Why was this blocked?"
  panel.
- Edge cases: empty, 5,000-char, unknown customer ID, SQL-injection strings,
  non-English, conflicting clauses, Snowflake down, Cortex timeout, duplicate approval,
  approving a blocked filing.

### UI rewrite (spans Phases 1–7, shipped with Phase 7)
`public/index.html` → multi-view app, vanilla JS/CSS, no build, no CDN, inline SVG.
Dark slate `#0d1117`/`#161b22`/`#30363d`/`#e6edf3`, accent `#2f81f7`, severity
`#f85149`/`#d29922`/`#3fb950`/`#8b949e`. **border-radius 0 everywhere**, 1px borders,
4px grid, no gradients/shadows/pills/emoji. 13px UI sans + tabular monospace for
IDs/amounts/hashes; 11px uppercase letter-spaced column headers.
Nine views: Copilot, Alert Queue, Case View, Filings, Regulatory Packs, Policy
Library, Automation, Audit Trail, System. Fixed top bar, collapsible nav rail,
resizable split panes, bottom status bar (run ID, evidence hash, latency, guardrail
summary). Dense tables: sticky header, sort, filters, j/k/Enter/Esc, CSV export.
Skeletons + per-panel retry, never raw JSON. Ctrl+K palette. WCAG AA, 2px focus rings,
responsive to 1024px. Every number clickable to its evidence; every clause ref opens
the clause drawer.

---

## 7. What I will not do

- **Weaken a guardrail to make a metric look better.** If tightening structuring drops
  recall, I report both numbers.
- **Fake Snowflake output.** No invented `TASK_HISTORY`, no screenshot of a console I
  did not run. If Snowflake is unavailable, the app says *Fallback mode: local
  engine* and the repo says so plainly. Judges punish fabricated evidence harder than
  absent evidence.
- **Put an LLM in the decision path.** Deterministic detectors decide; Cortex drafts
  prose; guardrails gate.
- **Commit secrets.** `.env` gitignored, `.env.example` committed.
- **Commit to the four phases before verifying** Cortex returns what the docs promise.

---

## 8. Test strategy (B11)

`pytest`, added to CI in `.github/workflows/ci.yml` (Python 3.11 + 3.12 matrix):

| Suite | Covers |
|---|---|
| `test_detectors.py` | one file per typology: fires on planted, silent on benign |
| `test_guardrails.py` | G1–G6 + new G7 individually |
| `test_router.py` | all 19 intents + slot extraction |
| `test_abstention.py` | abstain-with-gap is preserved |
| `test_filing_governance.py` | no `FILED` status, empty/bot approver rejected, **preparer ≠ approver**, duplicate approval rejected, blocked filing cannot be approved |
| `test_api_contract.py` | every action, status codes, **`_json` always yields parseable JSON** |
| `test_static_safety.py` | traversal payloads blocked; allowlist enforced |
| `test_examples.py` | loops all `EXAMPLE_QUESTIONS` (B1 regression) |
| `test_detector_precision.py` | structuring precision floor (B2 regression) |
| `test_semantic_questions.py` | 25+ NL questions, result shape (Phase 2) |
| `test_ddl_parity.py` | `sql/01` vs `BASE_DDL`; semantic metrics parity |
| `test_engine_fallback.py` | Snowflake unreachable → local, labelled correctly |

One regression test per bug in the brief. Pinned `requirements-dev.txt`; CI installs
only dev-safe deps and never needs Snowflake credentials.

---

## 9. Decision required before Phase 1

**There are no Snowflake credentials in this environment.** No `SNOWFLAKE_*` env vars,
no `~/.snowflake`, no `~/.snowsql`, no `.env`. `snowflake-connector-python` is not
installed.

Phase 1 as written — *"Use CoCo to create database, schemas, warehouse, roles, stage;
load the CSVs with PUT/COPY INTO; run sql/01–03 for real; fix any dialect errors
found"* — is **not executable** without an account. This is the single biggest
determinant of the outcome, because gaps G2/G3/G4/G5 (the biggest score risk) all
live in Phases 1–3.

**Option A (recommended) — you provide credentials now.**
Add a `.env` (gitignored) with account/user/role/warehouse, or give me a PAT plus the
account identifier. I provision for real, run `sql/01`–`sql/03`, fix the dialect errors
that surface, create the semantic view, and capture **genuine** `SHOW TASKS`,
`TASK_HISTORY`, Cortex Search and Analyst output into `docs/evidence/`. Highest score,
and it satisfies "actually runs on Snowflake" literally.

**Option B — no credentials available (e.g. none provisioned before the deadline).**
I build the full engine abstraction, execute every Snowflake object as
`sql/05_provision.sql` + `sql/06_cortex.sql` that you run with one command, and verify
them **offline** with `sqlglot` (Snowflake dialect) plus structural validation of the
agent/search/semantic YAML against the grammar in §2.11. I write a
`docs/SNOWFLAKE_RUNBOOK.md` with the exact commands and expected output.
The code is real and correct; what is missing is *executed* evidence, and I will say
so in the README and to the judges rather than imply otherwise. Materially lower on
Real-World Relevance, higher on nothing.

I recommend **A**. Every Snowflake object I write under B is unverified until someone
runs it, and unverified SQL is exactly the failure mode this rewrite is meant to fix.

**Secondary decision — scope of the four-eyes correction (§2.4).** I intend to
*implement* four-eyes (preparer ≠ approver, single approval, status-guarded), not
merely test what exists. Confirm you want that, since it changes demo behaviour: the
demo must then log in as an approver with a different identity than the preparer.

---

## 10. Phase 0 exit criteria

- [x] Repo audited; all 13 bugs reproduced with evidence, 1 additional critical bug found
- [x] Official syntax verified for Semantic Views, Cortex Agents, Cortex Search, `AI_COMPLETE`, `AI_PARSE_DOCUMENT`
- [x] Target architecture, data model, workflow and phase list written
- [ ] **Approved by user**
- [ ] Snowflake credential path decided (§9)

On approval I begin Phase 1 and open `docs/COCO_LOG.md`.
