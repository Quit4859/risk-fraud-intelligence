"""Copilot agent implementations.

Agent roles
-----------
``fraud_detector``    signal generation from SQL over transactions
``policy_matcher``    customer -> applicable obligations -> governing clauses
``evidence_gatherer`` hash-addressed, reproducible evidence set
``report_formatter``  audit-ready SAR / CTR / STR / MIAR / liquidity output
``retrieval``         parse unstructured policy text, serve citable clauses
``intent_router``     natural language -> governed execution plan
``answer_composer``   evidence-backed, cited, confidence-scored answer
"""

__all__ = [
    "fraud_detector",
    "policy_matcher",
    "evidence_gatherer",
    "report_formatter",
    "retrieval",
    "intent_router",
    "answer_composer",
]
