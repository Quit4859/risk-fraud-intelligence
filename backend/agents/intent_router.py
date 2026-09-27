"""Intent router: natural language -> a governed, auditable execution plan.

Why rule-based rather than a free-form LLM call
-----------------------------------------------
A regulatory copilot must be reproducible and inspectable. Every question is
resolved to a named intent with explicit slots, a named SQL statement and a
named retrieval call, all of which are recorded in the audit log. The router
never invents a query. This is the same discipline as a verified-query semantic
layer: the set of questions the system answers is finite, declared, and tested
(ontology/verified_queries.yaml).

If an LLM is configured (``llm.enabled``), it is used only to *rephrase* the
plan into prose - never to choose the query, the thresholds or the citations.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from backend.config import load_config

# --------------------------------------------------------------------------
# Slot extraction
# --------------------------------------------------------------------------

CUSTOMER_RE = re.compile(r"\bCUST-(\d{1,8})\b", re.I)
TXN_RE = re.compile(r"\bTXN-(\d{1,10})\b", re.I)
ACCOUNT_RE = re.compile(r"\bACCT-(\d{1,8})-(\d)\b", re.I)
AMOUNT_RE = re.compile(
    r"(?:(?:usd|inr|gbp|eur|aed|sgd|ngn|\$|£|€)\s*)?"
    r"([\d][\d,]*(?:\.\d+)?)\s*"
    r"(k|m|million|thousand|lakh|crore)?\s*"
    r"(usd|inr|gbp|eur|aed|sgd|ngn|dollars?|rupees?|pounds?)?", re.I)
DAYS_RE = re.compile(r"\b(?:last|past|previous)\s+(\d{1,4})\s*(day|week|month|year)s?\b", re.I)
DAYS_RE2 = re.compile(r"\b(\d{1,4})\s*(day|week|month|year)s?\b", re.I)
LIMIT_RE = re.compile(r"\btop\s+(\d{1,3})\b", re.I)

RULE_ALIASES = {
    "structuring": "STRUCTURING", "structure": "STRUCTURING", "ctr": "STRUCTURING",
    "smurfing": "STRUCTURING", "threshold avoidance": "STRUCTURING",
    "velocity": "VELOCITY", "speed": "VELOCITY", "spike": "VELOCITY",
    "geographic": "GEOGRAPHIC", "geo": "GEOGRAPHIC", "jurisdiction": "GEOGRAPHIC",
    "travel": "GEOGRAPHIC", "impossible travel": "GEOGRAPHIC",
    "account takeover": "ACCOUNT_TAKEOVER", "ato": "ACCOUNT_TAKEOVER",
    "takeover": "ACCOUNT_TAKEOVER", "compromise": "ACCOUNT_TAKEOVER",
    "mule": "MULE_NETWORK", "mule network": "MULE_NETWORK", "pass-through": "MULE_NETWORK",
    "passthrough": "MULE_NETWORK", "ring": "MULE_NETWORK",
    "trade": "TRADE_BASED", "tbml": "TRADE_BASED", "invoice mismatch": "TRADE_BASED",
    "trade based": "TRADE_BASED",
}

FRAMEWORK_ALIASES = {
    "aml": "AML", "anti-money laundering": "AML", "fincen": "AML", "pmla": "AML",
    "fiu": "AML", "fatf": "AML",
    "basel": "Basel3", "basel iii": "Basel3", "basel3": "Basel3", "capital": "Basel3",
    "lcr": "Basel3", "nsfr": "Basel3", "large exposure": "Basel3",
    "kyc": "KYC", "cdd": "KYC", "due diligence": "KYC", "pmla": "KYC",
    "sanctions": "Sanctions", "pep": "Sanctions", "politically exposed": "Sanctions",
    "prudential": "Prudential", "pra": "Prudential", "fca": "Prudential",
    "smcr": "Prudential", "local regulation": "LocalReg",
    "internal": "Internal", "internal policy": "Internal",
}

FILING_ALIASES = {
    "sar": "SAR", "suspicious activity report": "SAR",
    "ctr": "CTR", "currency transaction report": "CTR",
    "str": "STR", "suspicious transaction report": "STR",
    "miar": "MIAR", "board pack": "MIAR", "board report": "MIAR",
    "liquidity": "LIQUIDITY",
}

JURISDICTION_ALIASES = {
    "india": "IN", "indian": "IN", "in": "IN",
    "uk": "GB", "united kingdom": "GB", "britain": "GB", "gb": "GB",
    "us": "US", "usa": "US", "united states": "US", "america": "US",
    "uae": "AE", "emirates": "AE", "dubai": "AE",
    "singapore": "SG", "germany": "DE", "nigeria": "NG",
}

UNIT_SCALE = {
    None: 1.0, "": 1.0, "k": 1_000.0, "thousand": 1_000.0,
    "m": 1_000_000.0, "million": 1_000_000.0,
    "lakh": 100_000.0, "crore": 10_000_000.0,
}


@dataclass
class Intent:
    name: str
    slots: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    matched_keywords: List[str] = field(default_factory=list)
    alternatives: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.name,
            "slots": self.slots,
            "confidence": round(self.confidence, 3),
            "matched_keywords": self.matched_keywords,
            "alternatives": self.alternatives,
        }


@dataclass
class ExecutionResult:
    intent: Intent
    sql: List[str] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)
    blocks: List[Dict[str, Any]] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    citations: List[Dict[str, Any]] = field(default_factory=list)
    is_regulatory_claim: bool = False
    limitations: List[str] = field(default_factory=list)
    row_count: int = 0
    error: Optional[str] = None


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------

# Each pattern is (intent_name, [keyword sets that must all be present in some group])
# Matched in priority order; the first intent with the highest keyword hit wins.
INTENT_PATTERNS: List[tuple] = [
    ("rule_explanation", [("explain",), ("rule", "signal", "detector", "how does")]),
    ("policy_question", [("policy", "regulation", "clause", "rule", "requirement",
                          "obligation", "threshold", "what does")]),
    ("customer_risk_profile", [("risk", "profile", "exposure", "tell me about",
                                "assess", "case"), ("customer", "account", "who is")]),
    ("explain_signal", [("why", "explain", "what triggered", "reason"),
                        ("flag", "signal", "alert", "flagged", "score")]),
    ("filing_obligation", [("filing", "file", "report to", "regulator", "sar", "ctr",
                            "str", "obligation", "must i")]),
    ("draft_filing", [("draft", "generate", "produce", "create", "prepare"),
                      ("sar", "ctr", "str", "filing", "report")]),
    ("mule_network", [("mule", "mule network", "ring", "pass-through", "passthrough")]),
    ("structuring_exposure", [("structuring", "structuring exposure", "ctr avoidance",
                               "smurfing", "sub-threshold", "cash aggregation")]),
    ("geographic_concentration", [("geographic", "geography", "jurisdiction", "country",
                                  "countries", "high risk country", "travel")]),
    ("account_takeover", [("account takeover", "takeover", "device", "credential",
                           "compromise", "unauthorised", "unauthorized")]),
    ("trade_exposure", [("trade-based", "trade based", "tbml", "invoice mismatch",
                         "trade finance")]),
    ("velocity_check", [("velocity", "spike", "unusual activity", "behaviour",
                         "behavior", "surge")]),
    ("credit_watchlist", [("credit", "pd", "probability of default", "ecl", "default",
                           "exposure at default", "vulnerable obligor", "rating")]),
    ("liquidity_position", [("liquidity", "lcr", "nsfr", "hqla", "funding")]),
    ("large_exposures", [("large exposure", "counterparty", "concentration limit",
                          "25%", "le ratio")]),
    ("transaction_lookup", [("transaction", "transactions", "txn", "payment", "wire")]),
    ("top_risks", [("top", "highest", "biggest", "worst", "leading"),
                   ("risk", "risky", "riskiest", "fraud", "alerts", "flagged")]),
    ("portfolio_summary", [("portfolio", "overall", "enterprise", "bank-wide",
                            "bank wide", "summary", "overview", "how many")]),
]


def extract_slots(question: str, warehouse=None) -> Dict[str, Any]:
    q = question.lower()
    slots: Dict[str, Any] = {}

    m = CUSTOMER_RE.search(question)
    if m:
        slots["customer_id"] = f"CUST-{m.group(1).zfill(6)}"
    m = TXN_RE.search(question)
    if m:
        slots["transaction_id"] = f"TXN-{m.group(1).zfill(8)}"
    m = ACCOUNT_RE.search(question)
    if m:
        slots["account_id"] = f"ACCT-{m.group(1).zfill(6)}-{m.group(2)}"

    rule = None
    for alias, code in RULE_ALIASES.items():
        if alias in q:
            rule = code
            break
    if rule:
        slots["rule_code"] = rule

    framework = None
    for alias, fw in FRAMEWORK_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", q):
            framework = fw
            break
    if framework:
        slots["framework"] = framework

    filing = None
    for alias, ft in FILING_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", q):
            filing = ft
            break
    if filing:
        slots["filing_type"] = filing

    jurisdiction = None
    for alias, code in JURISDICTION_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", q):
            jurisdiction = code
            break
    if jurisdiction:
        slots["jurisdiction"] = jurisdiction

    m = DAYS_RE.search(q) or DAYS_RE2.search(q)
    if m:
        if m.re is DAYS_RE:
            n, unit = int(m.group(1)), m.group(2)
        else:
            n, unit = int(m.group(1)), m.group(2)
        slots["lookback_days"] = n * {"day": 1, "week": 7, "month": 30, "year": 365}[unit]
        slots["lookback_label"] = f"{n} {unit}{'s' if n != 1 else ''}"

    m = LIMIT_RE.search(q)
    if m:
        slots["limit"] = max(1, min(100, int(m.group(1))))

    amounts = []
    for m in AMOUNT_RE.finditer(question):
        raw = m.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        if len(raw.replace(".", "")) < 3:  # ignore bare small numbers
            continue
        scale = UNIT_SCALE.get((m.group(2) or "").lower(), 1.0)
        amounts.append(round(value * scale, 2))
    if amounts:
        slots["amounts"] = amounts
        slots["amount"] = max(amounts)

    if re.search(r"\b(must|should|required?|obligation|mandatory|required by)\b", q):
        slots["obligation_question"] = True
    if re.search(r"\b(file|filing|submit|report)\b", q):
        slots["filing_question"] = True
    return slots


class IntentRouter:
    def __init__(self, warehouse=None, config: Optional[Dict[str, Any]] = None):
        self.wh = warehouse
        self.cfg = config or load_config()

    def route(self, question: str) -> Intent:
        q = question.lower().strip()
        slots = extract_slots(question, self.wh)
        scored: List[tuple] = []
        for name, groups in INTENT_PATTERNS:
            hits = []
            total = 0
            for group in groups:
                group_hits = [k for k in group if re.search(rf"\b{re.escape(k)}\b", q)]
                if group_hits:
                    hits.extend(group_hits)
                    total += len(group_hits)
                else:
                    total = -1
                    break
            if total > 0:
                scored.append((total + len(hits) * 0.1, name, hits))
        if not scored:
            return Intent("policy_question", slots, confidence=0.35,
                          matched_keywords=[], alternatives=[])
        scored.sort(reverse=True)
        top = scored[0]
        others = [s[1] for s in scored[1:3]]
        # A question naming a customer id and a risk word is a profile question
        # even if another pattern also matched.
        if "customer_id" in slots and top[1] in ("top_risks", "portfolio_summary"):
            top = (top[0] + 2, "customer_risk_profile", top[2] + ["customer_id"])
        confidence = min(0.95, 0.45 + 0.1 * top[0] + (0.15 if slots else 0))
        return Intent(top[1], slots, confidence, top[2], others)
