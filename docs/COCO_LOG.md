# CoCo usage log

Evidence that the Copilot was planned, built, executed and tested with CoCo,
kept as a running record. Each entry states what was asked, what CoCo did, and
what the verification showed.

Judging categories map to sections:
**Planning** · **Development** · **Execution** · **Testing**.

Evidence artefacts live in `docs/evidence/`. Screenshots referenced below are
captured during the demo pass; where a section has none yet, the command output
is committed instead of a screenshot so the claim is still checkable.

---

## How to read this

Each entry uses the same shape:

| Field | Meaning |
|---|---|
| **Prompt** | what was asked of CoCo, in substance |
| **Action** | what CoCo changed or created |
| **Verification** | the command run, and what it printed |
| **Result** | PASS / FAIL, with the number that proves it |

A FAIL is recorded when one occurred. An entry that only ever says PASS is not
evidence.

---

## Planning

### P1 — Audit and reproduction

- **Prompt:** *Audit this repo. Reproduce every reported bug on the deployed
  profile. Do not trust the bug list — verify each one.*
- **Action:** read the full source, then wrote and ran a reproduction script
  rather than reading code and asserting a defect.
- **Findings that changed the brief:**

  | Reported | Found |
  |---|---|
  | B1: 1 example abstains | **4 of 12** abstain |
  | B7: path traversal | traversal also serves `config/settings.yaml` — every detection threshold |
  | four-eyes listed as a strength | **does not exist**; self-approval succeeds |
  | B13 | confirmed, Streamlit referenced but absent |

- **Verification:** `docs/evidence/audit/phase0_reproduction.txt`
- **Result:** PASS — 13 bugs reproduced, 1 critical bug found beyond the list.

### P2 — Syntax verification before writing SQL

- **Prompt:** *Verify current Snowflake and Cortex syntax against the official
  docs. Do not guess.*
- **Action:** read the documentation for `CREATE SEMANTIC VIEW`, the semantic
  view YAML spec, `CREATE AGENT`, `CREATE CORTEX SEARCH SERVICE`, `AI_COMPLETE`
  and `AI_PARSE_DOCUMENT` *before* writing any SQL.
- **Corrections to the brief that came out of this:**

  1. `AI_EXTRACT` is not the right primitive for extracting clauses from free-form
     policy PDFs. The documented path is `AI_PARSE_DOCUMENT` (extraction) plus
     `AI_COMPLETE` (structuring into rows).
  2. `AI_COMPLETE` supersedes `COMPLETE`.
  3. `CREATE SEMANTIC VIEW` requires `FACTS` **before** `DIMENSIONS`.
  4. Cortex features have no sqlite equivalent, so those paths must target
     Snowflake specifically rather than pass through `to_sqlite()`.

- **Result:** PASS — no guessed syntax written into the repo.

- **Plan produced:** `docs/PLAN.md` (585 lines).

---

## Development

### D1 — Four-eyes approval (brief said it existed; it did not)

- **Prompt:** *The brief lists four-eyes approval as a strength to preserve.
  Verify it. If it is not there, say so rather than preserving it.*
- **Action:** reproduced the self-approval, then built the lifecycle as an
  explicit state machine in `backend/governance/filings.py` and routed the
  reporter's `approve()` through it.
- **Verification:** `docs/evidence/audit/four_eyes_before_after.txt`

  | | before | after |
  |---|---|---|
  | preparer approves own filing | `APPROVED` | `four_eyes_violation` |
  | second approver overwrites first | `SUCCESS` | `already_approved` |
  | no comment recorded | `SUCCESS` | `comment_required` |

- **Result:** PASS — 30 tests in `tests/test_filing_governance.py`.

### D2 — Structuring precision (B2)

- **Prompt:** *Precision is 0.467. Root-cause it with a number before changing a
  threshold.*
- **Action:** measured the candidate band against the control group instead of
  guessing a better threshold.
- **Finding:** **101 of 119 benign controls (85%)** had three or more deposits
  inside the 2,000–10,000 band. The rule was satisfied by arithmetic.
- **Verification:** `docs/evidence/audit/b2_before_after.md`, both runs on an
  identical dataset.

  | | precision | recall | FPR |
  |---|---|---|---|
  | High only | 0.475 → **1.000** | 0.667 (unchanged) | 0.056 → **0.000** |
  | Medium+ | 0.500 → **0.830** | 0.929 (unchanged) | 0.070 → **0.014** |

- **Result:** PASS — precision improved at identical recall, meeting the
  Definition of Done without touching a planted case.

### D3 — Bugs found by measuring rather than assuming

Three defects only surfaced because the fix was measured:

1. **`sem_cash_72h_aggregation` was never updated.** The copilot answered "30
   customers are structuring" from the view while the detector used new logic.
   Detector and view had silently diverged.
2. **My own `_corroboration` called `min()` over dicts instead of values**, which
   threw and left every structuring signal erroring at score 0. The rule looked
   "clean" in aggregate metrics until a single case was inspected.
3. **`scripts/evaluate.py` scored an empty database.** It called
   `RiskCopilot()` with no warehouse, which constructs an empty sqlite file. It
   had been reporting `P=0 R=0` and looking healthy. The numbers in the previous
   `artifacts/evaluation_report.json` were meaningless.

- **Result:** PASS — each now has a test or a direct assertion.

### D4 — Concurrency bug in the boot scan

- **Prompt:** *Run a scan at boot in a background thread; make the executor
  rescan lazily if findings are empty.*
- **Action:** first attempt used a non-blocking lock and a row-count readiness
  check. Both were wrong, and tests caught it:
  - the boot thread and a question-triggered scan raced on one sqlite
    connection, corrupting the findings table;
  - `table_count("findings") > 0` reported "ready" *while the population was
    still being written*, because findings are persisted per customer.
- **Fix:** a single `threading.Event` for completion, a **blocking** bounded
  acquire so a mid-boot question waits for results rather than abstaining, and a
  readiness check of *completed **and** findings present*.
- **Result:** PASS — 12/12 example questions answer.

---

## Execution

### E1 — Environment is configured, not assumed

Credentials are read from the environment with a gitignored `.env`, and an
unconfigured environment is a **supported** configuration, not a crash.
`.env.example` is committed; CI fails if a `.env` is ever tracked.

- **Result:** PASS — `is_configured() <=> connection_parameters() is not None`
  verified across all six partial-configuration permutations.

### E2 — Deployment is valid

`render.yaml` rewritten as a real Blueprint (`services:` with `type`, `runtime`,
`buildCommand`, `startCommand` including `--preload`, `healthCheckPath: /healthz`,
`envVars`). The invalid top-level `serviceType` / `envFiles` / `runCommand`
schema is gone, as are the Next.js scaffold, `vercel.json`, `.vercelignore` and
`Procfile`.

- **Verification:** parsed and asserted for required keys, absence of the invalid
  keys, `--preload` present, and 6 secrets declared as `sync: false` (never
  literals).
- **Result:** PASS.

### E3 — `/healthz` answers without building

The health check deliberately does **not** call the engine bootstrap. A health
check that built the engine would be killed by the platform for being slow.

- **Result:** PASS — returns `warming_up` with zero build cost.

---

## Testing

### T1 — The suite exists and is real

`tests/` previously contained `__init__.py` and `test_probe.txt`. There was no
test of any kind.

| Suite | Covers |
|---|---|
| `test_filing_governance.py` | four-eyes, lifecycle, refusals (30 tests) |
| `test_examples.py` | all 12 example questions, lazy scan, `row_count` |
| `test_api_contract.py` | JSON validity, CORS, path traversal, `/healthz` |

No mocks. The properties under test are precisely the ones a mock would hide:
that a detector can name its evidence, that a guardrail blocks, that four-eyes
refuses.

### T2 — The suite runs the deployed dataset

The fixture profile is 200 customers — the same one the app serves. An earlier
120-customer fixture was rejected because it planted so few mule victims that no
shared-device cluster formed, and the "which mule clusters are active" example
correctly abstained for want of data. Testing a smaller population would test a
configuration nobody deploys.

### T3 — CI

`.github/workflows/ci.yml` runs pytest on Python 3.11 and 3.12 with **no
Snowflake credentials**: the fallback path is the one under test, and a suite
that only passes with a warehouse would be testing the wrong thing. A second job
fails the build if a `.env` is tracked or a credential-shaped string is
committed.

### T4 — Configuration cache returned stale env redirects

- **Prompt:** *Check all ok.* Verify the suite and the evaluate harness.
- **Action:** two bugs surfaced while verifying:

  1. `backend.config.load_config` was `@lru_cache`d, so its one-shot
     `_apply_env_overrides` ran only on the first call. After a later
     `RISK_DATA_DIR` / `RISK_ARTIFACT_DIR` redirect the cached result kept the old
     location, so `data/gold/ground_truth_labels.csv` resolved outside the
     redirected gold directory. The loader now caches only the file read
     (`_read_settings`) and re-applies env overrides per call, returning a fresh
     dict each time. `test_config_profiles.py::test_generator_and_config_agree_on_the_label_location`
     is the regression.
  2. `test_thresholds_cannot_be_moved_by_environment` read `cfg["thresholds"]`,
     a key no code defines; thresholds live under `cfg["detection"]`
     (`risk_bands` / `structuring`). Corrected the assertion to the real schema.

  No thresholds were changed — both fixes preserve the values in
  `config/settings.yaml`.

- **Verification:** `python3 -m pytest tests/ -q` → 93 passed, 1 skipped;
  `python3 scripts/evaluate.py` → high-only P=1.000 R=0.667 FPR=0.000,
  medium+ P=0.830 R=0.929 FPR=0.014 (identical to the before/after in D2).

- **Result:** PASS — was 2 failing in `test_config_profiles.py`, now 8/8 pass.

---

## Verdict against the judging categories

| Category | Evidence | State |
|---|---|---|
| Planning | P1, P2 — audit, reproduction, verified syntax, `docs/PLAN.md` | complete |
| Development | D1–D4 — four-eyes, precision, measured-bug fixes, concurrency | complete |
| Execution | E1–E3 — env, deploy blueprint, health check | complete |
| Testing | T1–T4 — suites, fixture fidelity, CI, config/env-override cache | complete |

**Not yet evidenced, and not claimed:** Snowflake provisioning, Cortex Analyst /
Search / Agent / `AI_COMPLETE` / `AI_PARSE_DOCUMENT`, and live Jira and Slack.
These require credentials that are not present in this environment. The code
paths exist and degrade to the local engine; no output from them has been
observed, and none is presented as if it had. See `docs/PLAN.md` §9.
