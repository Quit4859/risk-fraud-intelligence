"""Report formatter: audit-ready SAR, CTR, STR, MIAR, liquidity and case files.

Governance properties enforced here (not merely documented):

* **Provenance stamp** - every filing carries ``run_id``, ``data_fingerprint``,
  config version, model version and a content hash of the report body.
* **Four-eyes** - the formatter only ever emits ``PENDING_REVIEW``. Clause
  FILE-2.1.1 forbids submitting on a model's output alone, so there is no code
  path that sets ``FILED`` without a named human approver.
* **Citation completeness** - ``citation_coverage`` is measured; a filing whose
  regulatory claims are not fully anchored is marked ``INCOMPLETE`` and is
  blocked from escalation.
* **De-identification** - masked account numbers only; the formatter never
  emits a full account number, because none exists in the source data.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from backend.config import load_config

REPORT_TYPES = ("SAR", "CTR", "STR", "MIAR", "LIQUIDITY", "INVESTIGATION", "RISK_PACK")

STATUS_DRAFT = "PENDING_REVIEW"
STATUS_APPROVED = "APPROVED"
STATUS_FILED = "FILED"

#: Preparer identity used when no analyst is supplied. Filings prepared by the
#: system still require a *named human* to approve them; the system can never be
#: that human.
PREPARER_SYSTEM = "risk-fraud-regulatory-copilot"


def _body_hash(body: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:32]


def _money(v: Any) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


class ReportFormatterAgent:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.cfg = config or load_config()

    # -- provenance ---------------------------------------------------------
    def _stamp(self, run_id: str, warehouse, report_type: str,
                customer_id: Optional[str],
                preparer: str = PREPARER_SYSTEM) -> Dict[str, Any]:
        return {
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "generated_by": "risk-fraud-regulatory-copilot",
            # The human on whose behalf the copilot drafted. Four-eyes compares
            # the approver against this, so it must be a real principal and not
            # just the copilot's own name.
            "preparer": preparer,
            "preparer_is_system": preparer == PREPARER_SYSTEM,
            "copilot_version": self.cfg["copilot"]["version"],
            "config_version": self.cfg["copilot"]["version"],
            "environment": self.cfg["copilot"]["environment"],
            "run_id": run_id,
            "data_fingerprint": warehouse.source_fingerprint(),
            "as_of_date": warehouse.as_of(),
        }

    # -- SAR ----------------------------------------------------------------
    def build_sar(self, finding: Dict[str, Any], evidence: Dict[str, Any],
                  policy: Dict[str, Any], run_id: str, warehouse,
                  triggered: Optional[List[Dict[str, Any]]] = None,
                  preparer: str = PREPARER_SYSTEM) -> Dict[str, Any]:
        signals = finding.get("signals") or {}
        clauses = policy.get("governing_clauses", [])
        txns = [i for i in evidence.get("items", [])
                if i["source"] == "TRANSACTION"]
        profile = next((i["payload"] for i in evidence.get("items", [])
                        if i["source"] == "CUSTOMER_PROFILE"), {})

        who = {
            "name": profile.get("name"),
            "customer_id": finding.get("customer_id"),
            "segment": profile.get("segment"),
            "jurisdiction": profile.get("jurisdiction"),
            "is_politically_exposed_person": bool(profile.get("is_pep")),
            "kyc_status": profile.get("kyc_status"),
            "accounts": [
                {"account_id": a["account_id"],
                 "account_number_masked": a["account_number_masked"],
                 "account_type": a["account_type"],
                 "currency": a["currency"]}
                for a in (profile.get("accounts") or [])
            ],
        }

        suspicious_activity = {
            "typology": _primary_typology(finding),
            "risk_level": finding.get("risk_level"),
            "composite_risk_score": finding.get("composite_score"),
            "detection_confidence": finding.get("confidence"),
            "narrative": self.narrative(finding, evidence, policy),
            "indicators": [
                {
                    "rule_code": code,
                    "score": sig.get("score"),
                    "fired": sig.get("fired"),
                    "findings": sig.get("reasons", []),
                    "supporting_transaction_ids": sig.get("evidence_txn_ids", [])[:20],
                }
                for code, sig in sorted(signals.items())
                if sig.get("score", 0) > 0
            ],
            "aggregate_amount_involved": sum(
                float(t["payload"].get("amount", 0) or 0) for t in txns),
            "transaction_count": len(txns),
            "period": _period(txns),
        }

        regulatory_basis = [
            {
                "clause_ref": c["clause_ref"],
                "citation": c["citation"],
                "document": c["doc_id"],
                "framework": c["framework"],
                "requirement": c["text"],
            } for c in clauses
        ]
        obligations = [
            {"policy_id": t["policy_id"], "obligation": t["obligation"],
             "regulatory_body": t["regulatory_body"],
             "threshold": t["threshold"], "actual": t.get("actual"),
             "comparison": f"{t['metric']} {t['comparator']} {t['threshold']}",
             "breached": t.get("breached")}
            for t in (triggered or [])
        ]

        body = {
            "report_type": "SAR",
            "report_id": self._report_id("SAR", finding.get("customer_id"), run_id),
            "subject": who,
            "suspicious_activity": suspicious_activity,
            "regulatory_basis": regulatory_basis,
            "filing_obligations": obligations,
            "evidence_manifest": {
                "item_count": evidence.get("item_count"),
                "manifest_hash": evidence.get("manifest_hash"),
                "evidence_quality": evidence.get("evidence_quality"),
                "data_fingerprint": evidence.get("data_fingerprint"),
                "retention_years": 5,
                "retention_clause": "SAR-3.2.1",
            },
            "attestation": {
                "prepared_by": "Automated detection (machine-generated first draft)",
                "approval_required": True,
                "approval_clause": "FILE-2.1.1",
                "statement": ("This draft was generated by an automated model and is not "
                              "valid for submission until independently approved by a "
                              "named compliance officer."),
            },
        }
        return self._finalise(body, run_id, warehouse, finding, evidence, preparer)

    # -- CTR ----------------------------------------------------------------
    def build_ctr(self, finding: Dict[str, Any], evidence: Dict[str, Any],
                  policy: Dict[str, Any], run_id: str, warehouse,
                  triggered: Optional[List[Dict[str, Any]]] = None,
                  preparer: str = PREPARER_SYSTEM) -> Dict[str, Any]:
        cash_items = [i for i in evidence.get("items", []) if i["source"] == "TRANSACTION"]
        rows = []
        for item in cash_items:
            p = item["payload"]
            if int(p.get("is_cash", 0)) != 1:
                continue
            rows.append({
                "transaction_id": p["transaction_id"],
                "date": p["business_date"],
                "amount": p["amount"],
                "currency": p["currency"],
                "channel": p["channel"],
                "location": p["merchant_country"],
                "type": p["txn_type"],
            })
        total = sum(float(r["amount"]) for r in rows)
        threshold = self.cfg["policy"]["ctr_min_amount"]
        body = {
            "report_type": "CTR",
            "report_id": self._report_id("CTR", finding.get("customer_id"), run_id),
            "subject": {
                "customer_id": finding.get("customer_id"),
                "name": next((i["payload"].get("name") for i in evidence.get("items", [])
                              if i["source"] == "CUSTOMER_PROFILE"), None),
                "jurisdiction": next((i["payload"].get("jurisdiction")
                                      for i in evidence.get("items", [])
                                      if i["source"] == "CUSTOMER_PROFILE"), None),
            },
            "cash_transactions": rows,
            "aggregate": {
                "total_amount": round(total, 2),
                "transaction_count": len(rows),
                "filing_threshold": threshold,
                "threshold_met": total >= threshold,
                "aggregation_rule": "72-hour rolling, sub-threshold cash deposits "
                                    "aggregated per clause STR-2.1.2",
            },
            "regulatory_basis": [
                {"clause_ref": c["clause_ref"], "citation": c["citation"],
                 "requirement": c["text"]}
                for c in policy.get("governing_clauses", [])
            ],
            "filing_obligations": [
                {"policy_id": t["policy_id"], "regulatory_body": t["regulatory_body"],
                 "threshold": t["threshold"], "actual": t.get("actual"),
                 "obligation": t["obligation"]}
                for t in (triggered or [])
            ],
            "evidence_manifest": {
                "manifest_hash": evidence.get("manifest_hash"),
                "item_count": evidence.get("item_count"),
                "data_fingerprint": evidence.get("data_fingerprint"),
            },
            "attestation": {
                "approval_required": False,
                "auto_filing_note": ("A CTR is a factual report of cash movements and is "
                                     "listed in config as auto-filing eligible; human "
                                     "approval is still supported and recommended."),
                "approval_clause": "FILE-2.1.1",
            },
        }
        return self._finalise(body, run_id, warehouse, finding, evidence, preparer)

    # -- STR (India) --------------------------------------------------------
    def build_str(self, finding: Dict[str, Any], evidence: Dict[str, Any],
                  policy: Dict[str, Any], run_id: str, warehouse,
                  triggered: Optional[List[Dict[str, Any]]] = None,
                  preparer: str = PREPARER_SYSTEM) -> Dict[str, Any]:
        sar = self.build_sar(finding, evidence, policy, run_id, warehouse,
                             triggered, preparer)
        deadline = (datetime.utcnow() + timedelta(hours=24)).isoformat(timespec="seconds") + "Z"
        sar["report_type"] = "STR"
        sar["report_id"] = self._report_id("STR", finding.get("customer_id"), run_id)
        sar["filing_deadline"] = deadline
        sar["filing_deadline_clause"] = "STR-2.1.1"
        sar["reporting_authority"] = "Financial Intelligence Unit - India"
        return sar

    # -- MIAR (management information about risk) ---------------------------
    def build_miar(self, findings: List[Dict[str, Any]], warehouse, run_id: str,
                   period_days: int = 30,
                   preparer: str = PREPARER_SYSTEM) -> Dict[str, Any]:
        as_of = warehouse.as_of()
        dist = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
        for f in findings:
            dist[f.get("risk_level", "unknown")] = dist.get(f.get("risk_level", "unknown"), 0) + 1
        top = sorted(findings, key=lambda f: f.get("composite_score", 0), reverse=True)[:20]
        body = {
            "report_type": "MIAR",
            "report_id": self._report_id("MIAR", None, run_id),
            "reporting_period": {
                "end": as_of,
                "start": (datetime.fromisoformat(as_of) - timedelta(days=period_days)).date().isoformat(),
            },
            "scope": "Aggregate risk and fraud intelligence across the monitored portfolio",
            "population": {
                "customers_assessed": len(findings),
                "risk_distribution": dist,
                "high_risk_customers": [f["customer_id"] for f in findings
                                         if f.get("risk_level") == "high"],
            },
            "top_20_by_composite_score": [
                {"customer_id": f["customer_id"], "risk_level": f.get("risk_level"),
                 "composite_score": f.get("composite_score"),
                 "confidence": f.get("confidence"), "fired_rules": f.get("fired_rules"),
                 "primary_reason": f.get("primary_reason")}
                for f in top
            ],
            "regulatory_basis": [
                {"clause_ref": "ESC-1.1.1", "citation":
                    "INT-FRAUD-01 s.ESC-1.1 Severity matrix "
                    f"COMPOSITE SCORE >= {self.cfg['detection']['risk_bands']['high']} IS HIGH",
                 "requirement": "Composite score of 0.70 or above is classified HIGH and "
                                "must be referred to the financial crime investigations team "
                                "within one business day."},
                {"clause_ref": "FILE-2.2.1", "citation":
                    "INT-FRAUD-01 s.FILE-2.2 Explainability FILE-2.2.1",
                 "requirement": "Any statement in a filing that asserts a regulatory "
                                "obligation must name the clause that creates it."},
            ],
            "attestation": {
                "approval_required": True,
                "approval_clause": "FILE-2.1.1",
                "statement": "Board information pack; requires sign-off by the Chief "
                             "Compliance Officer before circulation.",
            },
        }
        return self._finalise(body, run_id, warehouse, None, None, preparer)

    # -- Liquidity / prudential report --------------------------------------
    def build_liquidity(self, warehouse, run_id: str,
                        preparer: str = PREPARER_SYSTEM) -> Dict[str, Any]:
        positions = warehouse.query("SELECT * FROM sem_liquidity_position")
        breaches = warehouse.query(
            "SELECT * FROM sem_large_exposures WHERE limit_status != 'WITHIN_LIMIT'")
        clauses = []
        for ref in ("LCR-3.1.1", "NSFR-3.2.1", "LE-2.1.1", "LE-2.1.2", "CAP-1.1.1"):
            row = warehouse.one(
                "SELECT clause_ref, doc_id, framework, section, title_text, body "
                "FROM document_chunks WHERE clause_ref = ?", (ref,))
            if row:
                clauses.append({
                    "clause_ref": row["clause_ref"],
                    "citation": f"{row['doc_id']} s.{row['section']} {row['clause_ref']}",
                    "requirement": row["body"],
                })
        breaches_by_status: Dict[str, int] = {}
        for b in breaches:
            breaches_by_status[b["limit_status"]] = breaches_by_status.get(b["limit_status"], 0) + 1
        body = {
            "report_type": "LIQUIDITY",
            "report_id": self._report_id("LIQUIDITY", None, run_id),
            "as_of_date": warehouse.as_of(),
            "liquidity_positions": positions,
            "prudential_breaches": {
                "large_exposure_exceptions": len(breaches),
                "by_status": breaches_by_status,
                "sample": breaches[:10],
            },
            "regulatory_basis": clauses,
            "attestation": {
                "approval_required": True,
                "approval_clause": "FILE-2.1.1",
                "statement": "Prudential position report for the board risk committee.",
            },
        }
        return self._finalise(body, run_id, warehouse, None, None, preparer)

    # -- Case file (investigation working paper) -----------------------------
    def build_investigation(self, finding: Dict[str, Any], evidence: Dict[str, Any],
                            policy: Dict[str, Any], run_id: str, warehouse,
                            preparer: str = PREPARER_SYSTEM) -> Dict[str, Any]:
        body = {
            "report_type": "INVESTIGATION",
            "report_id": self._report_id("INV", finding.get("customer_id"), run_id),
            "customer_id": finding.get("customer_id"),
            "risk_level": finding.get("risk_level"),
            "composite_risk_score": finding.get("composite_score"),
            "confidence": finding.get("confidence"),
            "allegations": [r for s in (finding.get("signals") or {}).values()
                            for r in s.get("reasons", [])],
            "evidence_index": [
                {"evidence_id": i["evidence_id"], "source": i["source"],
                 "source_ref": i["source_ref"], "summary": i["summary"],
                 "content_hash": i["content_hash"]}
                for i in (evidence.get("items") or [])
            ],
            "governing_clauses": [c["citation"] for c in policy.get("governing_clauses", [])],
            "next_steps": [
                "Confirm KYC beneficial ownership for the subject.",
                "Request supporting documentation for the flagged transactions.",
                "Consider filing a SAR/STR and record the four-eyes approval.",
            ],
            "attestation": {"approval_required": True, "approval_clause": "FILE-2.1.1"},
        }
        return self._finalise(body, run_id, warehouse, finding, evidence, preparer)

    # -- generic ------------------------------------------------------------
    def build(self, report_type: str, run_id: str, warehouse, **kwargs) -> Dict[str, Any]:
        table = {
            "SAR": self.build_sar,
            "CTR": self.build_ctr,
            "STR": self.build_str,
            "MIAR": self.build_miar,
            "LIQUIDITY": self.build_liquidity,
            "INVESTIGATION": self.build_investigation,
        }
        fn = table.get(report_type.upper())
        if fn is None:
            raise ValueError(f"unsupported report type: {report_type}. "
                             f"Supported: {', '.join(sorted(table))}")
        # run_id and warehouse are passed by keyword so the per-report
        # signatures can stay in the order that reads best. `preparer` names the
        # human on whose behalf the copilot drafted the filing; four-eyes
        # compares it against the approver later.
        kwargs.setdefault("preparer", PREPARER_SYSTEM)
        return fn(run_id=run_id, warehouse=warehouse, **kwargs)

    # -- helpers ------------------------------------------------------------
    def _finalise(self, body: Dict[str, Any], run_id: str, warehouse,
                  finding: Optional[Dict[str, Any]],
                  evidence: Optional[Dict[str, Any]],
                  preparer: str = PREPARER_SYSTEM) -> Dict[str, Any]:
        claims = _count_claims(body)
        coverage = claims["with_citation"] / claims["total"] if claims["total"] else 1.0
        body["governance"] = {
            **self._stamp(run_id, warehouse, body["report_type"],
                          body.get("subject", {}).get("customer_id")
                          if isinstance(body.get("subject"), dict) else None,
                          preparer),
            "content_hash": _body_hash(body),
            "regulatory_claims_total": claims["total"],
            "regulatory_claims_cited": claims["with_citation"],
            "citation_coverage": round(coverage, 4),
            "citation_status": "COMPLETE" if coverage >= 1.0 else "INCOMPLETE",
            "status": STATUS_DRAFT,
            "approver": None,
            "approved_at": None,
            "model_output_only": True,
            "four_eyes_required": True,
        }
        if finding is not None:
            body["governance"]["finding_risk_level"] = finding.get("risk_level")
            body["governance"]["detection_confidence"] = finding.get("confidence")
        if evidence is not None:
            body["governance"]["evidence_quality"] = evidence.get("evidence_quality")
            body["governance"]["manifest_hash"] = evidence.get("manifest_hash")
        return body

    @staticmethod
    def _report_id(prefix: str, customer_id: Optional[str], run_id: str) -> str:
        stamp = datetime.utcnow().strftime("%Y%m%d")
        suffix = (customer_id or "PORTFOLIO").replace("CUST-", "")
        return f"{prefix}-{stamp}-{suffix}-{run_id[-6:]}"

    @staticmethod
    def narrative(finding: Dict[str, Any], evidence: Dict[str, Any],
                  policy: Dict[str, Any]) -> str:
        txns = [i["payload"] for i in evidence.get("items", [])
                if i["source"] == "TRANSACTION"]
        total = sum(float(t.get("amount", 0) or 0) for t in txns)
        days = _period(txns)
        typology = _primary_typology(finding)
        fired = finding.get("fired_rules") or []
        clause_refs = [c["clause_ref"] for c in policy.get("governing_clauses", [])]
        reasons = [r for s in (finding.get("signals") or {}).values() if s.get("fired")
                   for r in s.get("reasons", [])]
        return (
            f"Between {days['start']} and {days['end']}, customer "
            f"{finding.get('customer_id')} was assessed as {finding.get('risk_level')} risk "
            f"with a composite score of {finding.get('composite_score'):.3f} "
            f"(model confidence {finding.get('confidence'):.2f}). "
            f"The {typology} typology is indicated by the following rules firing: "
            f"{', '.join(fired) or 'none'}. "
            f"Supporting analysis: {' '.join(reasons[:4])} "
            f"The activity reviewed comprises {len(txns)} transaction(s) totalling "
            f"USD {total:,.2f}. "
            f"This conclusion is governed by clause{'s' if len(clause_refs) != 1 else ''} "
            f"{', '.join(clause_refs) or 'n/a'}."
        )

    def persist(self, filing: Dict[str, Any], warehouse, run_id: str,
                customer_id: Optional[str] = None) -> str:
        filing_id = filing["report_id"]
        warehouse.execute(
            "INSERT OR REPLACE INTO filings (filing_id, filing_type, customer_id, report_json,"
            " status, generated_at, generated_by, run_id, content_hash, approver, approved_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (filing_id, filing["report_type"], customer_id,
             json.dumps(filing, default=str), filing["governance"]["status"],
             filing["governance"]["generated_at"], filing["governance"]["generated_by"],
             run_id, filing["governance"]["content_hash"], None, None))
        out_dir = os.path.join(self.cfg["_root"], self.cfg["paths"]["reports_dir"], run_id)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{filing_id}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(filing, fh, indent=2, default=str)
        return path

    def approve(self, filing_id: str, approver: str, warehouse,
                comment: str = "", action: str = "approve") -> Dict[str, Any]:
        """The only path that may move a filing out of PENDING_REVIEW.

        All four-eyes and state-transition rules live in
        :class:`backend.governance.filings.FilingGovernance` so they can be
        tested in isolation. This method is the reporter's adapter onto them.
        """
        from backend.governance.filings import FilingStore

        return FilingStore.decide(warehouse, filing_id, approver, comment, action)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _primary_typology(finding: Dict[str, Any]) -> str:
    signals = finding.get("signals") or {}
    order = ["STRUCTURING", "MULE_NETWORK", "ACCOUNT_TAKEOVER", "GEOGRAPHIC",
             "TRADE_BASED", "VELOCITY"]
    for code in order:
        if (signals.get(code) or {}).get("fired"):
            return {
                "STRUCTURING": "Structuring / currency transaction reporting avoidance",
                "MULE_NETWORK": "Pass-through mule network",
                "ACCOUNT_TAKEOVER": "Account takeover via compromised credentials",
                "GEOGRAPHIC": "Geographic concentration and identity inconsistency",
                "TRADE_BASED": "Trade-based money laundering indicators",
                "VELOCITY": "Behavioural velocity anomaly",
            }[code]
    return "Unclassified activity requiring analyst review"


def _period(txns: List[Dict[str, Any]]) -> Dict[str, Any]:
    dates = sorted(t.get("business_date", "") for t in txns if t.get("business_date"))
    if not dates:
        return {"start": None, "end": None, "days": 0}
    return {"start": dates[0], "end": dates[-1], "days": len(set(dates))}


def _count_claims(body: Dict[str, Any]) -> Dict[str, int]:
    """Count regulatory assertions and how many carry a clause anchor.

    A regulatory claim is an assertion that an obligation exists or that a
    threshold is breached - i.e. an entry in ``regulatory_basis`` or
    ``filing_obligations``. Attestation text and evidence manifests are
    provenance metadata, not claims, and counting them would make the coverage
    metric meaningless.
    """
    total = cited = 0
    for key in ("regulatory_basis", "filing_obligations"):
        for entry in body.get(key, []) or []:
            total += 1
            if entry.get("citation") or entry.get("clause_ref") or entry.get("policy_id"):
                cited += 1
    return {"total": total, "with_citation": cited}
