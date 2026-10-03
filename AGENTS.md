# AGENTS.md — conventions for CoCo on this repository

How to work in this repo without breaking the properties that make it
trustworthy. Read this before editing detection logic, guardrails or filings.

## What must never break

These are the reason the system is defensible. Each has tests; if a change
requires weakening one, that is a finding, not a refactor.

1. **A detector that fires must name its evidence.** `evidence_txn_ids` is not
   optional. A score that cannot point at the rows that produced it is a bug.
2. **Thresholds live in `config/settings.yaml`, never as literals in code.** A
   compliance officer tunes this file and diffs it in a pull request.
3. **Every regulatory sentence carries a clause citation.** An uncited
   regulatory claim is suppressed, not softened.
4. **Abstention is a feature.** Below the confidence threshold, state the gap.
   Do not raise a threshold to make a demo look better.
5. **No filing leaves `PENDING_REVIEW` without four-eyes.** Preparer ≠ approver,
   named human, mandatory comment, single decision. See
   `backend/governance/filings.py`.
6. **The audit log is append-only.** Refused actions are recorded too — an
   attempted self-approval is exactly what a reviewer needs to see.
7. **The evidence manifest hash** is stamped on every finding. Never recompute
   it from mutated data.
8. **The LLM never decides.** Cortex drafts prose; the detectors and guardrails
   decide. Removing every Cortex call must still leave a working copilot.

## Things that have already gone wrong

Read these before repeating them.

- **The detector and the SQL view can silently diverge.** `sem_cash_72h_aggregation`
  answered "30 customers are structuring" while the detector used corrected
  logic. When a detector's semantics change, the matching view changes in the
  same commit. `tests/test_ddl_parity.py` guards the metric definitions.
- **"Looks healthy in aggregate" hid three bugs.** An evaluation harness scoring
  an empty database reported `P=0 R=0`. Always assert that the data is loaded
  before trusting a metric.
- **A row-count check is not a readiness check.** The boot scan persists
  findings per customer, so `findings is non-empty` was true while the
  population was half-written. Use explicit completion signalling.
- **Ship the profile you test.** A 120-customer test fixture planted too few
  mule victims for any cluster to form, so an example question abstained. The
  suite now runs the 200-customer deployed profile.

## Data

- **Synthetic only.** No real customer data, ever.
- **No secrets in git.** `.env` is ignored; `.env.example` is committed. CI
  fails the build if a `.env` is tracked or a credential-shaped string appears.
- **Credentials are optional.** An unconfigured environment is a supported
  deployment: the engine factory selects sqlite and the UI says
  `Fallback mode: local engine`. Never make a Snowflake connection required.

## Before you commit

```bash
python3 -m pytest tests/ -q          # must be green
python3 scripts/evaluate.py          # metrics must not silently regress
```

- Run tests on the **deployed dataset profile**, not a smaller fixture.
- If you changed a threshold, publish before/after P/R/F1/FPR in
  `docs/evidence/audit/`. A threshold change with no measurement is a
  regression waiting to be discovered by a judge.
- Add a regression test for every bug fixed. Name it after the behaviour that
  was wrong, not the function you edited.
- Append a `docs/COCO_LOG.md` entry: prompt, action, verification command,
  result. Record FAILs — a log that only says PASS is not evidence.

## Style

- Comments explain *why*, not *what*. The existing code carries a lot of
  hard-won reasoning; keep that voice.
- Do not weaken a guardrail to make a metric improve. Report the trade-off and
  let the reviewer decide.
- Prefer a small explicit state machine over scattered conditionals when a rule
  is a compliance invariant (see `backend/governance/filings.py`).
