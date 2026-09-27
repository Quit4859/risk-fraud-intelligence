"""Answer composer: turns an execution result into a governed, cited answer.

An answer is only useful to a compliance officer if it carries five things:
the conclusion, the numbers behind it, the clauses that make it an obligation,
the exact queries that were run, and an honest statement of what it does not
know. The composer assembles all five and then hands the result to the
guardrail engine, which can refuse to let it stand.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.config import load_config

HEADINGS = {
    "portfolio_summary": "Portfolio risk position",
    "top_risks": "Highest-risk customers",
    "customer_risk_profile": "Customer risk profile",
    "explain_signal": "Why this customer was flagged",
    "rule_explanation": "How the rule works and what it obliges",
    "policy_question": "What the regulation says",
    "filing_obligation": "Filing obligations",
    "draft_filing": "Filing generation",
    "structuring_exposure": "Cash structuring exposure",
    "geographic_concentration": "Elevated-risk geography",
    "account_takeover": "Account takeover indicators",
    "mule_network": "Mule network clusters",
    "trade_exposure": "Trade-based money laundering indicators",
    "velocity_check": "Transaction velocity",
    "credit_watchlist": "Credit watchlist",
    "liquidity_position": "Liquidity and funding position",
    "large_exposures": "Large exposures",
    "transaction_lookup": "Transaction detail",
}


class AnswerComposer:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.cfg = config or load_config()

    def compose(self, question: str, result, guardrail_result,
                run_id: str = "", duration_ms: int = 0) -> Dict[str, Any]:
        intent = result.intent
        sections: List[Dict[str, Any]] = []
        for block in result.blocks:
            sections.append(block)

        lead = self._lead(intent, result, guardrail_result)
        text_parts = [lead]
        for block in sections:
            if block.get("type") == "text":
                text_parts.append(f"{block['title']}: {block['text']}")
            elif block.get("type") == "bullets":
                items = "; ".join(block.get("items", []))
                text_parts.append(f"{block['title']}: {items}")
        answer_text = "\n\n".join(p for p in text_parts if p)

        if guardrail_result.status == "FAIL":
            answer_text = self._refusal(guardrail_result, result)

        citations = result.citations or []
        answer = {
            "question": question,
            "answer_text": answer_text,
            "intent": intent.to_dict(),
            "sql": result.sql,
            "metrics": result.metrics,
            "sections": sections,
            "citations": citations,
            "row_count": result.row_count if result.row_count else len(result.rows),
            "rows": result.rows[:50],
            "is_regulatory_claim": result.is_regulatory_claim,
            "confidence": self._confidence(intent, result, guardrail_result),
            "guardrail": guardrail_result.to_dict(),
            "limitations": self._limitations(result, guardrail_result),
            "next_actions": guardrail_result.actions or self._next_actions(intent),
            "provenance": {
                "run_id": run_id,
                "generated_at": result.rows and None or None,
                "duration_ms": duration_ms,
                "engine": "risk-fraud-regulatory-copilot",
                "version": self.cfg["copilot"]["version"],
                "environment": self.cfg["copilot"]["environment"],
                "data_as_of": None,
            },
        }
        return answer

    # -- pieces -------------------------------------------------------------
    def _lead(self, intent, result, guardrail) -> str:
        name = HEADINGS.get(intent.name, intent.name.replace("_", " ").title())
        m = result.metrics or {}
        if intent.name == "customer_risk_profile":
            return (f"{name} for {m.get('customer_id')} ({m.get('name') or 'name withheld'}). "
                    f"The copilot assesses this customer as "
                    f"{m.get('risk_level', 'not assessed')} risk with a composite score of "
                    f"{_f(m.get('composite_score'))} and model confidence "
                    f"{_f(m.get('confidence'))}.")
        if intent.name == "top_risks":
            return (f"{m.get('returned', 0)} customer(s) are at medium or high risk, "
                    f"of which {m.get('high', 0)} are high risk. The table below is ranked "
                    "by composite score.")
        if intent.name == "structuring_exposure":
            return (f"{m.get('customers_with_aggregation', 0)} customer(s) aggregated "
                    f"sub-threshold cash above the "
                    f"USD {_f(m.get('reporting_threshold'), 0):,.0f} reporting threshold "
                    f"in the period from {m.get('window_start')}, totalling "
                    f"USD {_f(m.get('total_aggregate')):,.2f}.")
        if intent.name == "liquidity_position":
            return (f"{m.get('entities', 0)} reporting entit(ies) assessed: "
                    f"{m.get('lcr_breaches', 0)} LCR breach(es), "
                    f"{m.get('nsfr_breaches', 0)} NSFR breach(es) and "
                    f"{m.get('large_exposure_exceptions', 0)} large-exposure exception(s).")
        if intent.name == "credit_watchlist":
            return (f"{m.get('vulnerable_obligors', 0)} vulnerable obligor(s) above the "
                    f"10% PD threshold and {m.get('watch', 0)} on watch, carrying "
                    f"USD {_f(m.get('total_exposure')):,.2f} of exposure.")
        if intent.name == "mule_network":
            return (f"{m.get('clusters', 0)} shared-device cluster(s) identified. The largest "
                    f"links {m.get('largest_cluster', 0)} customers and "
                    f"USD {_f(m.get('largest_cluster_value')):,.2f} of flow.")
        if intent.name == "policy_question":
            return (f"{name}. The copilot retrieved {m.get('retrieved_clauses', 0)} clause(s) "
                    "from the policy corpus; the most relevant are cited below.")
        if intent.name == "portfolio_summary":
            return (f"{name}: {m.get('customers', 0):,} customers, "
                    f"{m.get('transactions', 0):,} transactions, data as of "
                    f"{m.get('as_of_date')}. Risk distribution: "
                    f"{m.get('risk_distribution')}.")
        return f"{name}."

    def _refusal(self, guardrail, result) -> str:
        reasons = "; ".join(guardrail.violations) or "a guardrail check failed"
        lines = [
            "The copilot is not able to give you a confident answer to that question.",
            f"Reason: {reasons}.",
        ]
        for lim in result.limitations[:4]:
            lines.append(f"- {lim}")
        if not result.limitations:
            lines.append("- The copilot will not assert a regulatory position without "
                         "supporting evidence (clause FILE-2.2.1).")
        lines.append("Required next step: "
                     + ("; ".join(guardrail.actions) or "route to a human analyst") + ".")
        return "\n".join(lines)

    def _confidence(self, intent, result, guardrail) -> float:
        base = float(intent.confidence or 0.4)
        if result.error:
            base *= 0.5
        if result.row_count == 0 and not result.citations:
            base *= 0.5
        elif result.row_count > 0:
            base += 0.05
        if result.citations:
            base += 0.05
        g = {"PASS": 1.0, "WARN": 0.85, "FAIL": 0.35}.get(guardrail.status, 0.5)
        return round(max(0.0, min(0.99, base * g)), 3)

    def _limitations(self, result, guardrail) -> List[str]:
        lims = list(result.limitations or [])
        if guardrail.warnings:
            lims.extend(f"Guardrail warning: {w}" for w in guardrail.warnings)
        if result.error:
            lims.append(f"Execution error: {result.error}")
        if not lims:
            lims.append("This answer is limited to the synthetic dataset as of the "
                        "stated as-of date and is not a regulatory determination.")
        return lims

    def _next_actions(self, intent) -> List[str]:
        return {
            "customer_risk_profile": [
                "Run `copilot explain` to see which rules fired and why.",
                "Open the investigation case file for the full evidence index.",
                "Check `copilot obligations` for the filing duties that attach."],
            "top_risks": [
                "Escalate the high-risk rows to the financial crime investigations team.",
                "Review the highest composite score first: policy INT-FRAUD-01 clause "
                "ESC-1.1.1 requires referral within one business day."],
            "structuring_exposure": [
                "File a CTR for each qualifying customer and screen for pass-through cash "
                "withdrawal within 72 hours.",
                "Check whether the customer has a documented expected cash profile."],
            "credit_watchlist": [
                "Reclassify vulnerable obligors and re-run ECL staging.",
                "Escalate to credit risk review per clause ECL-4.1.2."],
            "liquidity_position": [
                "Prepare an LCR remediation plan for any breaching entity.",
                "Board-report large exposures above 80% of the 25% limit."],
            "mule_network": [
                "Consider transaction interdiction on the shared device cluster.",
                "Trace the sender accounts and open cases on each ring member."],
        }.get(intent.name, ["Ask a follow-up question naming a specific customer, "
                            "rule or framework for a more specific answer."])


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
