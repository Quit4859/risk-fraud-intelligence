#!/usr/bin/env python3
"""Generate the regulatory document corpus and the rule-to-clause binding table.

The corpus is unstructured on purpose: markdown files with headings and
clause-numbered requirements, exactly like the PDF-turned-markdown a bank keeps
in its policy library. The pipeline then *parses* these files into citable
clause chunks, so the copilot's regulatory statements are anchored to text a
compliance officer can open and read.

No copyrighted regulator text is reproduced; the clauses below are original
synthetic paraphrases written for this demo, and are marked as such in the
document front matter.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

CORPUS_DIR = os.path.join(ROOT, "docs", "corpus")

DOCUMENTS = {
    "GLOBAL-AML-001": """---
doc_id: GLOBAL-AML-001
title: Global Anti-Money Laundering Standard - Cash Reporting, Structuring and Escalation
framework: AML
jurisdiction: International
version: "4.2"
effective_date: "2025-01-01"
regulatory_body: FATF / Global Policy Committee
source: SYNTHETIC-PARAPHRASE (not a reproduction of regulator text)
---

## 1. Purpose and Scope

### CTR-1.1 Scope of cash reporting
CTR-1.1.1 This standard applies to every deposit-taking business unit and every
non-banking financial company (NBFC) within the group, without exception.

CTR-1.1.2 A cash transaction includes currency, banknotes, coins, travellers
cheques and any monetary instrument denominated in a currency that a reporting
entity accepts for conversion or deposit.

### CTR-1.2 Aggregation and the reporting threshold
CTR-1.2.1 Reporting entities must aggregate **all** cash transactions conducted
by or on behalf of a single customer within a **single business day**.

CTR-1.2.2 Where the aggregated cash total reaches or exceeds
**USD 10,000 (or the local-currency equivalent)** a Currency Transaction Report
must be filed with the Financial Intelligence Unit.

CTR-1.2.3 Aggregation must also be applied across related accounts, related
parties and known beneficial owners where the relationship is documented.

## 2. Structuring Prohibitions

### STR-2.1 Avoidance of the reporting threshold
STR-2.1.1 It is prohibited to break a cash transaction into multiple smaller
transactions, whether through multiple branches, multiple channels, multiple
accounts or multiple related parties, where the purpose or effect is to avoid
an obligation imposed by CTR-1.2.

STR-2.1.2 Multiple sub-threshold cash deposits from a single customer that
aggregate at or above the reporting threshold **within any rolling
72-hour period** must be treated as a single reportable transaction.

STR-2.1.3 Where a customer has a documented expected cash profile, deposits
that individually fall below the threshold but exceed the documented expected
profile by a factor of 3 or more must be escalated to the financial crime
review team.

### STR-2.2 Downstream indicators of structuring
STR-2.2.1 A large cash withdrawal or transfer within 72 hours of a run of
sub-threshold cash deposits is a strong indicator of pass-through and must be
recorded as a supporting factor on the resulting case.

## 3. Suspicious Activity

### SAR-3.1 When to file
SAR-3.1.1 A Suspicious Activity Report must be prepared when there are grounds
to suspect that a transaction involves criminal activity, and the aggregated
amount concerned is USD 5,000 or more, or is otherwise subject to the local
filing floor.

SAR-3.1.2 Filing a SAR is mandatory once the suspicion is formed; it is not
conditional on the customer being contacted, and it is not conditional on the
suspicion being proven.

SAR-3.1.3 Where multiple transactions are related, the narrative must state the
relationship between them and must reference the transaction identifiers
relied upon.

### SAR-3.2 Record keeping
SAR-3.2.1 All records supporting a Suspicious Activity Report must be retained
for a minimum of **five years** from the date of filing and must be producible
on request without re-derivation.

SAR-3.2.2 Each supporting record must be addressable by a stable identifier so
that a filing can be reproduced exactly from the record set.

## 4. Sanctions and PEP
### PEP-4.1 Politically exposed persons
PEP-4.1.1 Politically exposed persons, and their immediate family members and
close associates, require enhanced due diligence, senior management approval
and a documented source-of-wealth statement before account opening and at
least every 12 months thereafter.
""",

    "SAN-1": """---
doc_id: SAN-1
title: Sanctions Screening and Geographic Risk
framework: Sanctions
jurisdiction: International
version: "2.7"
effective_date: "2025-03-01"
regulatory_body: OFAC / UN Security Council
source: SYNTHETIC-PARAPHRASE
---

## 1. Screening obligations
### SCR-1.1 Customer and counterparty screening
SCR-1.1.1 All customers, beneficial owners, authorised signatories and
payment counterparties must be screened against consolidated sanctions lists at
onboarding, on every list update, and before onboarding is confirmed.

SCR-1.1.2 Payment transactions must be screened against the originator,
the beneficiary, the intermediary and any nominated beneficiary before release.

## 2. Geographic risk
### GEO-2.1 Elevated-risk jurisdictions
GEO-2.1.1 Transactions booked in, or routed through, jurisdictions subject to
comprehensive sanctions or identified by the institution as elevated risk must
receive enhanced scrutiny regardless of value.

GEO-2.1.2 Where **more than 30%** of a customer's transactions in a rolling
90-day window are booked in elevated-risk jurisdictions, the relationship must
be treated as geographically concentrated and escalated.

### GEO-2.2 Inconsistent location
GEO-2.2.1 Where a single customer shows activity in **two or more jurisdictions
within a 24-hour period** that is inconsistent with the customer profile or
with stated travel, the activity must be flagged for review as an indicator of
compromised credentials or of identity substitution.

GEO-2.2.2 A change in the device fingerprint combined with a change in the
originating network country, followed within 14 days by a transaction above the
customer's normal ticket size, is a recognised account-takeover indicator and
requires the relationship to be placed under enhanced monitoring.

## 3. Transaction monitoring
### MON-3.1 Behavioural baselines
MON-3.1.1 Behavioural baselines shall be established per customer per product
using a minimum of 90 days of history, and shall use a robust dispersion measure
so that a single atypical day does not distort the baseline.

MON-3.1.2 An alert is raised where daily activity exceeds the baseline by a
robust z-score of 3.0 or more, or where daily cash or wire aggregation exceeds
the reporting threshold.
""",

    "BASEL-3": """---
doc_id: BASEL-3
title: Basel III - Capital, Large Exposure and Liquidity Requirements
framework: Basel3
jurisdiction: International
version: "3.1"
effective_date: "2025-01-01"
regulatory_body: Basel Committee on Banking Supervision
source: SYNTHETIC-PARAPHRASE
---

## 1. Minimum capital requirements
### CAP-1.1 Risk-based capital ratios
CAP-1.1.1 Each reporting bank shall maintain, as a minimum and on a
consolidated basis, Common Equity Tier 1 capital of **4.5%** of risk-weighted
assets, Tier 1 capital of **6.0%**, and total capital of **8.0%**.

CAP-1.1.2 The capital conservation buffer adds 2.5% to each minimum, giving a
consolidated effective CET1 requirement of 7.0%.

### CAP-1.2 Capital buffers and constraints
CAP-1.2.1 Where a buffer is breached, distributions are constrained on a
graduated scale and must be disclosed in the Pillar 3 report.

## 2. Counterparty credit risk
### LE-2.1 Large exposure limit
LE-2.1.1 The **large exposure limit** is set at **25% of Tier 1 capital**.
A single counterparty or a group of connected counterparties must not exceed
this limit without prior supervisory approval.

LE-2.1.2 Exposures must be reported daily to the board risk function where
utilisation of the limit exceeds 80%.

## 3. Liquidity
### LCR-3.1 Liquidity coverage ratio
LCR-3.1.1 The liquidity coverage ratio, defined as high-quality liquid assets
divided by total net cash outflows over the following 30 calendar days, must be
maintained at a minimum of **100%**.

LCR-3.1.2 A bank that breaches the LCR must have a remediation plan approved by
its board and must not rely on central bank facilities to meet the ratio on a
structural basis.

### NSFR-3.2 Net stable funding ratio
NSFR-3.2.1 The net stable funding ratio must be maintained at a minimum of
**100%**, computed as available stable funding divided by required stable
funding.

## 4. Credit risk and expected loss
### ECL-4.1 Expected credit loss
ECL-4.1.1 Expected credit losses shall be computed on a 12-month basis for
Stage 1 exposures and on a lifetime basis for Stage 2 and Stage 3 exposures,
using a forward-looking, probability-weighted scenario set.

ECL-4.1.2 A borrower with a 12-month probability of default above **10%** must
be classified as a vulnerable obligor and escalated to credit risk review.
""",

    "IN-NBFC": """---
doc_id: IN-NBFC
title: India - PMLA and NBFC prudential obligations
framework: LocalReg
jurisdiction: IN
version: "6.0"
effective_date: "2024-10-01"
regulatory_body: Financial Intelligence Unit - India / RBI
source: SYNTHETIC-PARAPHRASE
---

## 1. Customer due diligence
### CDD-1.1 Know your customer
CDD-1.1.1 A reporting entity shall complete customer due diligence,
including
identification of the beneficial owner, before establishing a relationship.

CDD-1.1.2 For non-individual customers, the beneficial owner shall be
identified up to a natural person holding 10% or more of voting rights or
economic interest, and the ownership chain shall be documented.

### CDD-1.2 Enhanced due diligence
CDD-1.2.1 Enhanced due diligence shall be applied to high-risk customers,
politically exposed persons, and to relationships with a higher than normal
risk of money laundering, and shall be reviewed at least annually.

## 2. Suspicious transaction reporting
### STR-2.1 Reporting to the FIU
STR-2.1.1 A suspicious transaction report shall be filed with the Financial
Intelligence Unit within 24 hours of reaching a reasonable belief that a
suspicious transaction has taken place.

STR-2.1.2 Where a cash transaction aggregates at or above INR 10,00,000, or the
applicable local-currency threshold, within a day, a report shall be filed.

### STR-2.2 Registration and monitoring
STR-2.2.1 Entities shall register with the Financial Intelligence Unit and
maintain a system for monitoring transactions against known risk profiles.

## 3. NBFC prudential directions
### NBFC-3.1 Overdue management
NBFC-3.1.1 An asset is classified as standard, sub-standard, doubtful or loss
asset in accordance with the days-past-due grid; an account classified as
sub-standard or worse shall attract provisioning as set out in the
capital adequacy schedule.

### NBFC-3.2 Directions to systemically important NBFCs
NBFC-3.2.1 Systemically important NBFCs shall file a returns pack on a monthly
basis covering asset quality, capital adequacy, liquidity and customer
grievances.
""",

    "UK-PRA": """---
doc_id: UK-PRA
title: United Kingdom - PRA/FCA credit risk and financial crime systems
framework: Prudential
jurisdiction: GB
version: "3.4"
effective_date: "2025-02-01"
regulatory_body: PRA / FCA
source: SYNTHETIC-PARAPHRASE
---

## 1. Systems and controls
### SYSC-1.1 Systems and controls framework
SYSC-1.1.1 A firm must maintain a risk framework that identifies the risks
relevant to its business, sets the level of tolerated risk, and includes
effective controls to mitigate those risks.

SYSC-1.1.2 The framework must be embedded in organisational arrangements,
staffing, training and remuneration policies, and must be documented.

## 2. Financial crime systems and controls
### SMCR-2.1 Financial crime systems and controls
SMCR-2.1.1 A firm must establish and maintain proportionate and effective
systems and controls to prevent and respond to money laundering, terrorist
financing, fraud and other financial crime.

SMCR-2.1.2 Customer due diligence, ongoing monitoring, transaction monitoring,
sanctions screening and the reporting of suspicions are mandatory elements of
those systems and controls.

## 3. Credit risk
### CRR-3.1 Probability of default
CRR-3.1.1 A credit institution shall measure probability of default over a
one-year horizon and shall assign exposures to credit risk categories
according to their credit quality steps.

CRR-3.1.2 Where an exposure moves between credit quality steps, the treatment
applied to the exposure shall change accordingly and shall be evidenced.
""",

    "INT-FRAUD-01": """---
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
""",
}

# --------------------------------------------------------------------------
# Rule bindings: detection rule -> clause + threshold. This is the auditable
# bridge between "what we detected" and "what the regulation requires".
# --------------------------------------------------------------------------

POLICIES = [
    {
        "policy_id": "POL-AML-CTR",
        "framework": "AML",
        "name": "Cash Transaction Reporting threshold and aggregation",
        "jurisdiction": "International",
        "regulatory_body": "FATF / Global Policy Committee",
        "description": "Cash transactions must be aggregated per customer per business day and reported at USD 10,000 or above.",
        "applies_to": ["all"],
        "risk_levels": [],
        "effective_date": "2025-01-01",
        "version": "4.2",
        "key_thresholds": {"reporting_amount": 10000, "currency": "USD", "window": "business_day"},
        "binding_rule_code": "STRUCTURING",
        "obligation": "FILE_CTR",
        "source_doc_id": "GLOBAL-AML-001",
    },
    {
        "policy_id": "POL-AML-STR",
        "framework": "AML",
        "name": "Structuring / threshold avoidance prohibition",
        "jurisdiction": "International",
        "regulatory_body": "FATF / Global Policy Committee",
        "description": "Sub-threshold cash deposits aggregating above the reporting threshold within a rolling 72-hour period are a single reportable transaction.",
        "applies_to": ["all"],
        "risk_levels": [],
        "effective_date": "2025-01-01",
        "version": "4.2",
        "key_thresholds": {"reporting_amount": 10000, "min_deposits": 3, "window_hours": 72,
                           "profile_multiple": 3.0},
        "binding_rule_code": "STRUCTURING",
        "obligation": "ESCALATE",
        "source_doc_id": "GLOBAL-AML-001",
    },
    {
        "policy_id": "POL-AML-SAR",
        "framework": "AML",
        "name": "Suspicious Activity Reporting",
        "jurisdiction": "International",
        "regulatory_body": "FATF / Global Policy Committee",
        "description": "A SAR is mandatory on formed suspicion with an aggregate amount at or above USD 5,000, supported by retained records for five years.",
        "applies_to": ["all"],
        "risk_levels": ["medium", "high"],
        "effective_date": "2025-01-01",
        "version": "4.2",
        "key_thresholds": {"reporting_amount": 5000, "retention_years": 5},
        "binding_rule_code": "COMPOSITE",
        "obligation": "FILE_SAR",
        "source_doc_id": "GLOBAL-AML-001",
    },
    {
        "policy_id": "POL-SAN-GEO",
        "framework": "Sanctions",
        "name": "Geographic concentration and inconsistent location",
        "jurisdiction": "International",
        "regulatory_body": "OFAC / UN Security Council",
        "description": "Over 30% elevated-risk jurisdiction share in 90 days is concentrated; two or more jurisdictions in 24 hours is an identity inconsistency.",
        "applies_to": ["all"],
        "risk_levels": [],
        "effective_date": "2025-03-01",
        "version": "2.7",
        "key_thresholds": {"high_risk_share": 0.30, "window_days": 90, "impossible_travel_hours": 24},
        "binding_rule_code": "GEOGRAPHIC",
        "obligation": "ESCALATE",
        "source_doc_id": "SAN-1",
    },
    {
        "policy_id": "POL-SAN-ATO",
        "framework": "Sanctions",
        "name": "Account takeover / compromised credentials monitoring",
        "jurisdiction": "International",
        "regulatory_body": "OFAC / UN Security Council",
        "description": "New device plus new network country followed by above-normal ticket within 14 days requires enhanced monitoring.",
        "applies_to": ["all"],
        "risk_levels": [],
        "effective_date": "2025-03-01",
        "version": "2.7",
        "key_thresholds": {"window_days": 14, "ticket_multiple": 2.0},
        "binding_rule_code": "ACCOUNT_TAKEOVER",
        "obligation": "MONITOR_ENHANCED",
        "source_doc_id": "SAN-1",
    },
    {
        "policy_id": "POL-MON-VEL",
        "framework": "AML",
        "name": "Behavioural velocity monitoring",
        "jurisdiction": "International",
        "regulatory_body": "Global Policy Committee",
        "description": "Baselines use 90 days of history with a robust dispersion measure; alert at robust z-score of 3.0 or more.",
        "applies_to": ["all"],
        "risk_levels": [],
        "effective_date": "2025-03-01",
        "version": "2.7",
        "key_thresholds": {"baseline_days": 90, "zscore": 3.0, "alert_zscore": 3.0},
        "binding_rule_code": "VELOCITY",
        "obligation": "MONITOR",
        "source_doc_id": "SAN-1",
    },
    {
        "policy_id": "POL-BASEL-CAP",
        "framework": "Basel3",
        "name": "Minimum capital and conservation buffer",
        "jurisdiction": "International",
        "regulatory_body": "Basel Committee",
        "description": "CET1 4.5%, Tier 1 6.0%, total capital 8.0%, plus a 2.5% conservation buffer.",
        "applies_to": ["corporate", "nbfc"],
        "risk_levels": [],
        "effective_date": "2025-01-01",
        "version": "3.1",
        "key_thresholds": {"cet1": 0.045, "tier1": 0.06, "total_capital": 0.08, "buffer": 0.025},
        "binding_rule_code": None,
        "obligation": "REPORT",
        "source_doc_id": "BASEL-3",
    },
    {
        "policy_id": "POL-BASEL-LARGE",
        "framework": "Basel3",
        "name": "Large exposure limit",
        "jurisdiction": "International",
        "regulatory_body": "Basel Committee",
        "description": "Single counterparty or connected-counterparty group capped at 25% of Tier 1 capital; board reporting above 80% utilisation.",
        "applies_to": ["corporate", "nbfc"],
        "risk_levels": [],
        "effective_date": "2025-01-01",
        "version": "3.1",
        "key_thresholds": {"limit_pct": 0.25, "board_report_pct": 0.80},
        "binding_rule_code": None,
        "obligation": "REPORT",
        "source_doc_id": "BASEL-3",
    },
    {
        "policy_id": "POL-BASEL-LCR",
        "framework": "Basel3",
        "name": "Liquidity coverage ratio and NSFR",
        "jurisdiction": "International",
        "regulatory_body": "Basel Committee",
        "description": "LCR and NSFR each maintained at a minimum of 100%.",
        "applies_to": ["corporate", "nbfc"],
        "risk_levels": [],
        "effective_date": "2025-01-01",
        "version": "3.1",
        "key_thresholds": {"lcr_min": 1.0, "nsfr_min": 1.0},
        "binding_rule_code": None,
        "obligation": "REPORT",
        "source_doc_id": "BASEL-3",
    },
    {
        "policy_id": "POL-BASEL-ECL",
        "framework": "Basel3",
        "name": "Expected credit loss and vulnerable obligor threshold",
        "jurisdiction": "International",
        "regulatory_body": "Basel Committee",
        "description": "12-month PD above 10% requires vulnerable obligor classification and escalation to credit risk review.",
        "applies_to": ["sme", "corporate", "nbfc", "retail"],
        "risk_levels": [],
        "effective_date": "2025-01-01",
        "version": "3.1",
        "key_thresholds": {"pd_high": 0.10, "stage_boundary": 0.03},
        "binding_rule_code": "CREDIT",
        "obligation": "ESCALATE",
        "source_doc_id": "BASEL-3",
    },
    {
        "policy_id": "POL-IN-CDD",
        "framework": "LocalReg",
        "name": "Customer due diligence and beneficial ownership (PMLA)",
        "jurisdiction": "IN",
        "regulatory_body": "FIU-India / RBI",
        "description": "CDD with beneficial owner identification before relationship; EDD for high-risk and PEP relationships reviewed at least annually.",
        "applies_to": ["all"],
        "risk_levels": [],
        "effective_date": "2024-10-01",
        "version": "6.0",
        "key_thresholds": {"ubo_threshold": 0.10, "edd_review_months": 12},
        "binding_rule_code": "KYC",
        "obligation": "REVIEW",
        "source_doc_id": "IN-NBFC",
    },
    {
        "policy_id": "POL-IN-STR",
        "framework": "LocalReg",
        "name": "Suspicious transaction reporting to the FIU",
        "jurisdiction": "IN",
        "regulatory_body": "FIU-India",
        "description": "STR within 24 hours of reasonable belief; cash aggregation at or above INR 10,00,000 in a day is reportable.",
        "applies_to": ["all"],
        "risk_levels": [],
        "effective_date": "2024-10-01",
        "version": "6.0",
        "key_thresholds": {"cash_threshold_inr": 1000000, "hours": 24},
        "binding_rule_code": "STRUCTURING",
        "obligation": "FILE_STR",
        "source_doc_id": "IN-NBFC",
    },
    {
        "policy_id": "POL-UK-SMCR",
        "framework": "Prudential",
        "name": "Financial crime systems and controls",
        "jurisdiction": "GB",
        "regulatory_body": "FCA",
        "description": "Proportionate and effective systems and controls covering CDD, monitoring, transaction monitoring, screening and reporting of suspicions.",
        "applies_to": ["all"],
        "risk_levels": [],
        "effective_date": "2025-02-01",
        "version": "3.4",
        "key_thresholds": {},
        "binding_rule_code": "COMPOSITE",
        "obligation": "ATTEST",
        "source_doc_id": "UK-PRA",
    },
    {
        "policy_id": "POL-INT-ESC",
        "framework": "Internal",
        "name": "Escalation severity matrix and SLA",
        "jurisdiction": "Internal",
        "regulatory_body": "Group Financial Crime Committee",
        "description": "Composite score >= 0.70 HIGH (1 business day), 0.42-0.70 MEDIUM (3 business days), below 0.42 LOW (monitoring only).",
        "applies_to": ["all"],
        "risk_levels": [],
        "effective_date": "2025-06-01",
        "version": "9.1",
        "key_thresholds": {"high": 0.70, "medium": 0.42},
        "binding_rule_code": "COMPOSITE",
        "obligation": "ESCALATE",
        "source_doc_id": "INT-FRAUD-01",
    },
    {
        "policy_id": "POL-INT-FOUR-EYES",
        "framework": "Internal",
        "name": "Four-eyes filing and explainability requirement",
        "jurisdiction": "Internal",
        "regulatory_body": "Group Financial Crime Committee",
        "description": "Every filing is a machine-generated draft plus an independent human approval; every regulatory assertion must name its clause.",
        "applies_to": ["all"],
        "risk_levels": [],
        "effective_date": "2025-06-01",
        "version": "9.1",
        "key_thresholds": {},
        "binding_rule_code": None,
        "obligation": "APPROVAL",
        "source_doc_id": "INT-FRAUD-01",
    },
    {
        "policy_id": "POL-INT-FALLBACK",
        "framework": "Internal",
        "name": "Graceful degradation and model risk management",
        "jurisdiction": "Internal",
        "regulatory_body": "Group Financial Crime Committee",
        "description": "On insufficient data quality or unassemblable evidence, fall back to human review and record the reason; never silently return low risk.",
        "applies_to": ["all"],
        "risk_levels": [],
        "effective_date": "2025-06-01",
        "version": "9.1",
        "key_thresholds": {"min_evidence_quality": "medium"},
        "binding_rule_code": None,
        "obligation": "FALLBACK_REVIEW",
        "source_doc_id": "INT-FRAUD-01",
    },
    {
        "policy_id": "POL-TBM-MULE",
        "framework": "AML",
        "name": "Pass-through mule network detection and interdiction",
        "jurisdiction": "International",
        "regulatory_body": "Global Policy Committee",
        "description": "Fan-in from multiple senders on a shared device followed by rapid onward pass-through within 24-48 hours is a pass-through mule pattern and supports an interdiction decision.",
        "applies_to": ["all"],
        "risk_levels": [],
        "effective_date": "2025-01-01",
        "version": "4.2",
        "key_thresholds": {"min_senders": 2, "pass_through_hours": 48, "inflow_outflow_ratio": 1.20},
        "binding_rule_code": "MULE_NETWORK",
        "obligation": "ESCALATE",
        "source_doc_id": "GLOBAL-AML-001",
    },
    {
        "policy_id": "POL-TBM-TRADE",
        "framework": "AML",
        "name": "Trade-based money laundering indicators",
        "jurisdiction": "International",
        "regulatory_body": "Global Policy Committee",
        "description": "Repeated round-value cross-border wires with no matching inbound settlement, or with a counterparty in an elevated-risk jurisdiction, are TBML indicators requiring enhanced due diligence.",
        "applies_to": ["sme", "corporate"],
        "risk_levels": [],
        "effective_date": "2025-01-01",
        "version": "4.2",
        "key_thresholds": {"round_amount": 5000, "min_wires": 3},
        "binding_rule_code": "TRADE_BASED",
        "obligation": "EDD",
        "source_doc_id": "GLOBAL-AML-001",
    },
]


def main(argv=None) -> int:
    gold_dir = None
    if argv:
        for i, a in enumerate(argv):
            if a == "--gold-dir" and i + 1 < len(argv):
                gold_dir = argv[i + 1]
    os.makedirs(CORPUS_DIR, exist_ok=True)
    written = []
    for doc_id, body in DOCUMENTS.items():
        path = os.path.join(CORPUS_DIR, f"{doc_id}.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        written.append(path)

    pol_dir = gold_dir or os.path.join(ROOT, "data", "gold")
    os.makedirs(pol_dir, exist_ok=True)
    pol_path = os.path.join(pol_dir, "policies.json")
    with open(pol_path, "w", encoding="utf-8") as fh:
        json.dump(POLICIES, fh, indent=2)

    print(f"[generate_corpus] {len(written)} documents -> {CORPUS_DIR}")
    print(f"[generate_corpus] {len(POLICIES)} rule bindings -> {pol_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
