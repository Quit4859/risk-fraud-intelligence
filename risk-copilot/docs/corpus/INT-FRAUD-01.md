---
doc_id: INT-FRAUD-01
title: Internal Policy - Fraud Escalation, Four-Eyes Filing and Record Retention
framework: Internal
jurisdiction: International
version: "9.1"
effective_date: "2025-06-01"
regulatory_body: Group Financial Crime Committee
source: INTERNAL-POLICY (synthetic)
---

## 1. Detection and triage
### ESC-1.1 Severity matrix
ESC-1.1.1 A composite risk score of **0.70 or above** is classified HIGH and
must be referred to the financial crime investigations team within one
business day.

ESC-1.1.2 A composite risk score between **0.42 and 0.70** is classified MEDIUM
and must be reviewed by a Level 1 analyst within three business days.

ESC-1.1.3 A composite risk score below **0.42** is classified LOW and is
handled through continuous monitoring only.

### ESC-1.2 Confluence of indicators
ESC-1.2.1 Two or more independent indicators triggered for the same customer in
a 30-day window shall be treated as a single enhanced case rather than as
independent alerts.

## 2. Filing controls
### FILE-2.1 Four-eyes principle
FILE-2.1.1 Every regulatory filing shall be produced by a machine-generated
first draft and independently validated by a named human approver before
submission. A filing shall never be submitted on the basis of a model's output
alone.

FILE-2.1.2 Every automated filing draft shall carry a traceable derivation:
the run identifier, the data fingerprint, the detection rules that fired, the
regulatory clauses relied upon, and the identifiers of the supporting records.

### FILE-2.2 Explainability
FILE-2.2.1 Any statement in a filing that asserts a regulatory obligation must
name the clause that creates it. A statement that cannot be anchored to a
clause shall not be included.

## 3. Automation and change control
### AUTO-3.1 Model risk management
AUTO-3.1.1 Detection thresholds are configuration, not code. Changes to
thresholds require independent review and a recorded approval, and the previous
configuration must remain retrievable for the life of any filing produced under
it.

AUTO-3.1.2 Where an automated decision is made on data of insufficient quality,
or where evidence cannot be assembled, the system shall fall back to human
review and shall record the reason. It shall not silently return a low-risk
result.
