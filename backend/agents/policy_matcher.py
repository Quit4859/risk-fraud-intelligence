"""Policy matcher: customer + fired rules -> applicable obligations -> clauses.

This is the join between *what the model detected* and *what the regulation
requires*. Every obligation the copilot acts on is returned with:

* the policy that creates it (``policy_id``)
* the threshold that triggers it, taken from the binding, not invented
* the citable clause text from the parsed document
* the clause citation string that goes into the filing
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from backend.config import load_config

# Which filing type an obligation maps to, per jurisdiction.
OBLIGATION_TO_FILING = {
    "FILE_CTR": "CTR",
    "FILE_SAR": "SAR",
    "FILE_STR": "STR",
    "ESCALATE": "ESCALATE",
    "MONITOR": "MONITOR",
    "MONITOR_ENHANCED": "MONITOR",
    "EDD": "EDD_REVIEW",
    "REVIEW": "EDD_REVIEW",
    "REPORT": "REGULATORY_REPORT",
    "ATTEST": "REGULATORY_REPORT",
    "APPROVAL": "CONTROL",
    "FALLBACK_REVIEW": "CONTROL",
}


class PolicyMatcherAgent:
    def __init__(self, warehouse, retrieval=None, config: Optional[Dict[str, Any]] = None):
        self.wh = warehouse
        self.retrieval = retrieval
        self.cfg = config or load_config()

    # -- customer-scoped obligations ---------------------------------------
    def obligations_for_customer(self, customer_id: str) -> List[Dict[str, Any]]:
        rows = self.wh.query(
            "SELECT * FROM sem_filing_obligation_queue WHERE customer_id = ? "
            "ORDER BY framework, policy_id", (customer_id,))
        out = []
        for r in rows:
            thresholds = _loads(r.get("key_thresholds"), {})
            out.append({
                "policy_id": r["policy_id"],
                "framework": r["framework"],
                "policy_name": r["policy_name"],
                "regulatory_body": r["regulatory_body"],
                "jurisdiction": r["jurisdiction"],
                "obligation": r["obligation"],
                "resulting_filing_type": OBLIGATION_TO_FILING.get(r["obligation"], "REVIEW"),
                "binding_rule_code": r["binding_rule_code"],
                "thresholds": thresholds,
                "source_doc_id": r["source_doc_id"],
                "source_clause_refs": [],
            })
        return out

    # -- rules -> governing clauses ----------------------------------------
    def match(self, customer_id: str, fired_rules: Iterable[str],
              obligations: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        fired = [r for r in fired_rules if r]
        obligations = obligations if obligations is not None else \
            self.obligations_for_customer(customer_id)

        matched_policies = [o for o in obligations if o["binding_rule_code"] in fired]
        if fired:
            fired_set = set(fired)
            matched_policies.extend(
                {**o, "binding_rule_code": None} for o in obligations
                if not o["binding_rule_code"]
                and o["obligation"] in ("APPROVAL", "FALLBACK_REVIEW", "FILE_CTR"))

        clauses: List[Dict[str, Any]] = []
        if self.retrieval is not None:
            clauses = self.retrieval.clauses_for_rules(fired)
        by_ref = {c["clause_ref"]: c for c in clauses}
        for pol in matched_policies:
            pol["source_clause_refs"] = [
                c["clause_ref"] for c in clauses
                if c.get("policy_id") in (None, pol["policy_id"])
            ][:4] or pol["source_clause_refs"]

        # Thresholds that are actually breached, evaluated against the finding.
        triggered = self._evaluate_triggers(matched_policies)

        return {
            "customer_id": customer_id,
            "fired_rules": fired,
            "obligations_total": len(obligations),
            "obligations_matched": len(matched_policies),
            "matched_policies": matched_policies,
            "governing_clauses": [
                {
                    "clause_ref": c["clause_ref"],
                    "citation": c["citation"],
                    "doc_id": c["doc_id"],
                    "framework": c["framework"],
                    "title": c["title_text"],
                    "text": c["body"],
                    "obligation": c.get("obligation"),
                } for c in clauses
            ],
            "thresholds_triggered": triggered,
            "filings_indicated": sorted({t["filing_type"] for t in triggered}),
        }

    def _evaluate_triggers(self, policies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Apply binding thresholds to the live finding metrics.

        Kept explicit (rather than data-driven) so a compliance reviewer can
        read exactly which comparison produces which obligation.
        """
        pcfg = self.cfg["policy"]
        triggered: List[Dict[str, Any]] = []

        def add(policy, filing, metric, actual, threshold, comparator, note):
            triggered.append({
                "policy_id": policy["policy_id"],
                "framework": policy["framework"],
                "regulatory_body": policy["regulatory_body"],
                "filing_type": filing,
                "metric": metric,
                "actual": actual,
                "threshold": threshold,
                "comparator": comparator,
                "obligation": policy["obligation"],
                "note": note,
            })

        for p in policies:
            t = p.get("thresholds") or {}
            if p["obligation"] == "FILE_CTR" and t.get("reporting_amount"):
                add(p, "CTR", "cash_aggregate_72h", None, t["reporting_amount"], ">=",
                    "A 72-hour cash aggregation at or above the reporting amount requires "
                    "a currency transaction report.")
            elif p["obligation"] == "FILE_STR" and t.get("cash_threshold_inr"):
                add(p, "STR", "cash_aggregate_72h", None, t["cash_threshold_inr"], ">=",
                    "Local cash aggregation threshold reached; a suspicious transaction "
                    "report is due to the FIU within 24 hours.")
            elif p["obligation"] == "FILE_SAR":
                add(p, "SAR", "composite_risk_score", None, pcfg["sar_score_threshold"], ">=",
                    "Suspicion is formed and the aggregate amount is at or above the "
                    "local filing floor; the filing duty is mandatory.")
        return triggered

    def resolve_obligations(self, match_result: Dict[str, Any],
                            metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fill in the actual measured values so a filing can cite numbers."""
        cash_agg = metrics.get("cash_aggregate_72h")
        score = metrics.get("composite_risk_score")
        for trig in match_result.get("thresholds_triggered", []):
            if trig["metric"] == "cash_aggregate_72h":
                trig["actual"] = cash_agg
                trig["breached"] = bool(cash_agg and cash_agg >= trig["threshold"])
            elif trig["metric"] == "composite_risk_score":
                trig["actual"] = score
                trig["breached"] = bool(score is not None and score >= trig["threshold"])
            else:
                trig["breached"] = None
        return [t for t in match_result.get("thresholds_triggered", []) if t.get("breached")]

    # -- portfolio-level compliance ----------------------------------------
    def jurisdiction_frameworks(self, jurisdiction: str) -> List[Dict[str, Any]]:
        return self.wh.query(
            "SELECT framework, COUNT(*) AS policies FROM policies "
            "WHERE jurisdiction IN ('International', ?, 'Internal') "
            "GROUP BY framework ORDER BY framework", (jurisdiction,))

    def obligation_matrix(self) -> List[Dict[str, Any]]:
        """Board-level view: which obligations are live across the bank."""
        return self.wh.query("""
            SELECT p.policy_id, p.framework, p.obligation, p.jurisdiction,
                   p.name, p.binding_rule_code, p.key_thresholds, p.source_doc_id,
                   (SELECT COUNT(*) FROM sem_filing_obligation_queue q
                     WHERE q.policy_id = p.policy_id) AS customers_in_scope
            FROM policies p
            ORDER BY p.framework, p.obligation, p.policy_id
        """)


def _loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default
