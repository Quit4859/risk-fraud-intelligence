---
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
