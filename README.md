# Risk, Fraud and Regulatory Intelligence Copilot

A copilot for bank and NBFC risk teams. It reads transaction and account data
alongside policy and filing text, and answers questions in a way a compliance
officer can defend: every answer carries the clauses it relies on, the queries
that produced it, an evidence manifest hash, and a guardrail verdict.

It covers the whole path from signal to evidence to a documented finding or
filing. It stops at a draft. Submission is a human step and the code enforces
that rather than merely documenting it.

---

## The problem

A fraud analyst today exports transactions, pivots them in a spreadsheet, looks
up a threshold in a PDF, and writes a narrative into a SAR form. The numbers, the
rule and the wording live in three different places, so the narrative cannot be
reproduced and the reviewer cannot check it.

The copilot joins them. A finding is a customer, a composite risk score, the
transactions that support it, the clauses that make it an obligation, and a hash
over all of it.

---

## How a question becomes a filing

```
   "which customers are structuring cash to avoid the reporting threshold?"
                              |
                              v
                 +--------------------------+
                 |   Intent router          |   named intent + typed slots
                 |   backend/agents/        |   never an invented query
                 |     intent_router.py     |
                 +--------------------------+
                              |
              +---------------+----------------+
              |                                |
   rule fires? no                    rule fires? yes
              |                                |
              v                                v
     "no clause matched"        +-----------------------------+
     abstain, state the gap     |  1  FraudDetectorAgent       |
                                |     6 detectors over SQL    |
                                +-----------------------------+
                                             |
                                             v
                                +-----------------------------+
                                |  2  PolicyMatcherAgent      |
                                |     obligations -> clauses  |
                                +-----------------------------+
                                             |
                                             v
                                +-----------------------------+
                                |  3  EvidenceGathererAgent   |
                                |     hash-addressed record   |
                                |     set + manifest hash     |
                                +-----------------------------+
                                             |
                                             v
                                +-----------------------------+
                                |  4  GuardrailEngine         |
                                |     evidence, confidence,   |
                                |     citation, PII, health   |
                                +-----------------------------+
                                     |                  |
                              blocked              passed
                                     |                  |
                                     v                  v
                              human review     +-------------------+
                                                | 5  ReportFormatter |
                                                |   SAR / CTR / STR  |
                                                |   / MIAR / LIQ     |
                                                +-------------------+
                                                          |
                                                          v
                                                +-------------------+
                                                |  PENDING_REVIEW   |
                                                |  human approves   |
                                                +-------------------+
```

The same four agents run for a single-customer question, a full portfolio scan,
and the scheduled Snowflake task. There is one implementation, not three.

---

## Layout

```
.
  config/settings.yaml        every threshold that can change a decision
  ontology/ontology.yaml      entities, metrics, gates between stages
  docs/corpus/*.md            six policy documents, 56 citable clauses
  sql/
    01_schema.sql             Snowflake DDL
    02_dynamic_tables.sql     five incremental dynamic tables
    03_streams_tasks.sql      streams, tasks, the unattended DAG
    04_semantic_views.sql     10 portable views (Snowflake + sqlite)
  backend/
    warehouse.py              SQL layer, dialect translation, provenance
    cli.py                    one CLI behind every surface
    agents/
      fraud_detector.py       six detectors
      policy_matcher.py       customer -> obligations -> clauses
      evidence_gatherer.py    reproducible evidence sets
      report_formatter.py     audit-ready output
      retrieval.py            markdown -> clauses, BM25
      intent_router.py        NL -> named plan
      executors.py            one executor per intent
      answer_composer.py      cited, confidence-scored answer
    orchestration/
      copilot.py              the wired-up flow
      multi_agent_workflow.py explicit handoffs
      guardrails.py           six checks, fail-safe
      audit_log.py            append-only trail
      mcp_integrations.py     connectors + MCP tool manifest
  scripts/
    generate_synthetic_data.py
    generate_corpus.py
    run_pipeline.py
    evaluate.py
  api/                        WSGI request handling
  backend/governance/
    rbac.py                   demo roles, signed sessions, permission matrix
  public/index.html           static UI
```

Roughly 7,800 lines of Python, 820 of SQL.

---

## Data

Everything is generated. No production data, no personal data, no real PANs.

`scripts/generate_synthetic_data.py` produces 600 customers, ~880 accounts and
~260,000 transactions over 400 days, plus a credit portfolio, a liquidity
position, counterparty exposures and a pre-existing alert backlog. It is seeded,
so the same seed gives the same bytes.

The generator plants five fraud typologies and labels them in
`data/gold/ground_truth_labels.csv`:

| Typology | Planted | What is planted |
|---|---:|---|
| structuring | 14 | 3 to 6 cash deposits a day just under the filing line, then a large branch withdrawal |
| account takeover | 16 | dormant account wakes on an unseen device from an elevated-risk IP and immediately spends |
| geographic | 18 | three jurisdictions inside 24 hours, one of them elevated risk |
| mule network | 23 | fan-in from 22 senders on one shared device, 88 percent passed on within a day |
| trade based | 10 | repeated round-value cross-border wires with no matching inbound settlement |
| control | 519 | ordinary behaviour, no planted scenario |

Two generator constraints matter more than they look. Each customer operates in
a small set of jurisdictions and relocates every five to thirty days, rather than
appearing in a random country per transaction, because uniform sampling
manufactures impossible travel that no rule should have to ignore. And baseline
cash activity stays under the reporting threshold, because a customer who
routinely crosses the CTR line is not a clean control group. Without these the
ground truth is wrong and every number below is wrong with it.

---

## The policy corpus

Six markdown documents, 56 clause-level chunks:

| Document | Framework | Covers |
|---|---|---|
| GLOBAL-AML-001 | AML | CTR aggregation, structuring prohibition, SAR, record retention, PEP |
| SAN-1 | Sanctions | screening, geographic concentration, impossible location, behavioural baselines |
| BASEL-3 | Basel3 | minimum capital, large exposure limit, LCR, NSFR, expected credit loss |
| IN-NBFC | LocalReg | PMLA due diligence, FIU reporting, NBFC prudential directions |
| UK-PRA | Prudential | systems and controls, SMCR, credit risk categories |
| INT-FRAUD-01 | Internal | escalation matrix, four-eyes filing, explainability, graceful degradation |

The clause numbers are the join key. The table above is human-readable, but
`data/gold/policies.json` binds each detection rule to the clause family that
governs it:

```
STRUCTURING     -> STR-2.*      file a CTR, escalate
VELOCITY        -> MON-3.*      monitor
GEOGRAPHIC      -> GEO-2.*      escalate
ACCOUNT_TAKEOVER-> GEO-2.2.*    monitor under enhanced
MULE_NETWORK    -> STR-2.*      escalate, consider interdiction
TRADE_BASED     -> TBM-*        enhanced due diligence
```

That is what makes a citation a citation. The copilot does not decide that a
$10,000 aggregation is reportable; it reads the threshold out of a policy binding
and the sentence out of the clause, and cites both.

---

## Detection

Six detectors, each returning a score in [0,1], a confidence, the reasons that
produced the score, and the identifiers of the rows that justify it. A score that
cannot name its evidence is treated as a bug.

**Structuring** aggregates sub-threshold cash over a rolling 72 hours, not
same-day, because that is what the clause says. A large cash withdrawal within
72 hours of the run adds a pass-through factor.

**Velocity** compares the anchor day against a 90-day baseline using median and
MAD rather than mean and standard deviation, so one atypical day does not
distort its own detector.

**Geographic** has two tests. Concentration excludes the customer's home
jurisdiction, because a customer domiciled in an elevated-risk jurisdiction is
not geographically concentrated, they are a high-risk-geometry relationship
handled under CDD-1.2.1. Impossible travel is measured as implied speed between
consecutive jurisdiction changes against a 900 km/h cruise ceiling, not as a raw
country count. The share test also requires at least 20 cross-border
transactions, because a percentage over two transactions is a division, not a
signal.

**Account takeover** treats "new device" as a first-seen test against 90 days of
prior history, not a row count inside the detection window.

**Mule network** has two paths, because a sender into a pass-through hub has no
fan-in of its own. The hub is found by inbound fan-in followed by onward
pass-through. The member is found by a shared device fingerprint and described
using the hub's fan-in, so the ring is reported rather than guessed.

**Trade based** looks for repeated round-value cross-border wires with no
matching inbound settlement.

The composite is a configured weighted sum. A single high-confidence typology
match is not left to be diluted by the weighting, so a rule floor in
`settings.yaml` can hold a confirmed structuring case at HIGH, and the finding
records which rule did it.

---

## Results

**These are the numbers for the profile the app actually deploys** — 200
customers, 150 days, seed 42 (`RISK_PROFILE=demo`). The previous figures in
this README came from a 600-customer run that no deployment uses, which is the
whole of bug B4. Both were measured on the same code; see
`docs/evidence/audit/b4_deployed_profile_metrics.txt`.

```bash
RISK_DATA_DIR=/tmp/deployed-profile python scripts/evaluate.py
```

| Operating point | Precision | Recall | F1 | False positive rate |
|---|---:|---:|---:|---:|
| High only | 1.000 | 0.357 | 0.526 | 0.000 |
| Medium or high | 0.917 | 0.786 | 0.846 | 0.005 |

Population 200 (14 planted fraud cases, 186 benign controls). Throughput is
61 ms per customer on the local runtime.

Recall by typology on the deployed profile:

| Typology | Planted | Recall at medium+ | Correct rule fired |
|---|---:|---:|---:|
| structuring | 2 | 1.00 | 1.00 |
| account takeover | 3 | 1.00 | 1.00 |
| mule network | 4 | **0.25** | 1.00 |
| trade based | 2 | 1.00 | 1.00 |
| geographic | 3 | 1.00 | 1.00 |

**Read the mule-network number honestly.** The demo profile plants only four
mule cases and the detector catches one. On the 600-customer profile, where it
plants thirteen, recall is 1.00. At n=4 this is a sample-size artefact rather
than a detector defect, and the right response is to say so — not to quietly
enlarge the demo profile until the number looks good. Use `RISK_PROFILE=full`
for a statistically meaningful read; the live numbers behind the badge in the
console come from the `evaluate` API action and describe the running dataset.

Comparing like for like with the 600-customer profile, the B2 structuring fix
raised precision from 0.475 to 1.000 at high-only and 0.500 to 0.830 at
medium+ **at identical recall**. The deployed profile is a different population,
so the two tables are not directly comparable; the before/after pair that
isolates the fix is in `docs/evidence/audit/b2_before_after.md`.

These numbers are against synthetic data with known labels, which is a lower bar
than a live deployment. They show the detectors work and the plumbing is honest.
They do not show the thresholds are tuned for a real portfolio.

---

## Guardrails

Six checks run on every finding. A violation does not produce a clean-looking
result; it routes to human review and records why.

| Check | What it enforces |
|---|---|
| G1 evidence present | at least 3 evidence items |
| G2 evidence quality | below medium, a human looks before filing |
| G3 confidence gate | below 0.70 no auto-escalation |
| G4 citation completeness | no uncited regulatory claim survives |
| G5 PII scan | matched patterns are masked before anything leaves |
| G6 rule health | an errored rule degrades the case rather than passing it |

On the answer path there is an abstention threshold below which the copilot
states it cannot answer and why, rather than asserting something. That is a
deliberate choice: a copilot that always has an answer is worse than useless to
a compliance officer, because it teaches them to stop checking.

Filing governance is enforced in code, not in a runbook:

- The formatter has no path that emits a filed status. Everything is
  `PENDING_REVIEW`.
- `approve` rejects an empty approver, or one named `bot`, `copilot` or `auto`.
- The submission gateway refuses any filing whose citation coverage is below
  1.0, and refuses any filing that is not approved.

---

## Interfaces

### CLI

```bash
python -m backend.cli status
python -m backend.cli ask "do I need to file a SAR for CUST-000123?"
python -m backend.cli scan --limit 200
python -m backend.cli detect CUST-000123
python -m backend.cli file CUST-000123 --type SAR
python -m backend.cli approve SAR-20260926-000123-a1b2c3 --approver "name@bank.example"
python -m backend.cli policy "structuring threshold"
python -m backend.cli audit --limit 40
```

### Render

```bash
# Point Render at the repo; it reads render.yaml as a Blueprint.
# Secrets are set in the dashboard, never committed.
```

One WSGI process serving the API and the static console. `render.yaml` uses
`--preload` so the engine is built once in the master before workers fork,
and `/healthz` as the health check so probing never triggers a build. The
synthetic dataset is generated into `/tmp` on each cold start.

```bash
curl -s "$URL/healthz"
curl -s -X POST "$URL/api" -H 'Content-Type: application/json' \
  -d '{"action":"login","role":"analyst"}'
curl -s -X POST "$URL/api" -H 'Content-Type: application/json' \
  -d '{"action":"ask","question":"which customers are structuring cash?","token":"<token>"}'
```

Read actions need a session token since B8. Full route table in `DEPLOY.md`.

### Snowflake

`sql/01_schema.sql` through `sql/03_streams_tasks.sql` are dialect-specific and
run as written. The pipeline is a DAG, not a batch job:

```
tsk_ingest_transactions        every 5 min
  -> tsk_ingest_and_apply_stream
     -> tsk_run_fraud_detection  hourly
        -> tsk_generate_draft_filings
tsk_daily_risk_pack            06:07 UTC, board pack
```

`sql/04_semantic_views.sql` is the portable part. The same text runs on Snowflake
and on the local sqlite runtime, so a metric cannot drift between the demo and
the warehouse. It avoids `RANGE` window frames, Snowflake-only date functions
and `IFF`/`NVL` for that reason.

### MCP

`backend/orchestration/mcp_integrations.py` carries the tool manifest and a
dispatcher. Read tools are `ask`, `detect_customer`, `portfolio_risks`,
`policy_lookup`, `audit_trace`. Write tools are `run_population_scan`,
`generate_filing`, `escalate_case`, and they are marked as such. Jira and Slack
connectors run in simulated mode by default and return a hashed receipt, so the
demo is reproducible offline and no credential is needed.

---

## Running it

```bash
pip install -r requirements.txt          # pyyaml, requests
python scripts/run_pipeline.py           # generate, ingest, parse, create views
python -m backend.cli status
python -m backend.cli ask "show the top 10 highest risk customers"
python scripts/evaluate.py               # score against ground truth
```

Every surface needs a session now. `python -m backend.cli login --role analyst`
prints a token to export as `RISK_TOKEN`.

The Streamlit app referenced in earlier revisions of this README was never
built. It is listed under Known limits rather than linked as though it existed.

---

## Known limits

- The numbers above are against synthetic data. Real transaction data has
  messier keys, more silent failures and a long tail the generator does not
  reproduce.
- Thresholds have not been tuned against a real alert history, so the operating
  point is a reasonable default rather than a chosen one.
- Document parsing handles the markdown the corpus ships in. Scanned PDFs would
  need OCR first.
- **No Streamlit app exists.** Earlier revisions of this README linked one. The
  CLI, the WSGI API and the static console are the working surfaces; building
  the Streamlit-in-Snowflake app is open work, listed in `docs/PLAN.md`.
- **RBAC is a demonstration, not an identity provider.** Three roles and an
  HMAC-signed session token. With `RISK_SESSION_SECRET` unset the key is
  per-process, so sessions do not survive a restart. It is access control that
  makes the four-eyes rule meaningful, not a substitute for corporate SSO.
- **Snowflake and Cortex are unverified.** No credentials were available in the
  environment this was built in, so nothing in `sql/01`–`sql/03`, the semantic
  view, or any Cortex call has been executed. The app runs on the sqlite
  fallback. Syntax was checked against the official documentation; behaviour
  was not observed. `docs/COCO_LOG.md` records this rather than implying
  otherwise.
- `RISK_PROFILE=full` builds the 600-customer dataset at a cold start cost of
  about 8 s. The deployed default is `demo` (200 customers, 150 days).
