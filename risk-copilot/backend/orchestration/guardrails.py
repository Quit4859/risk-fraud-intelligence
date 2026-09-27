"""Guardrails: validation, confidence gating, PII control, graceful fallback.

The copilot's governing principle is *fail safe, not fail silent*. Concretely:

* a decision below the configured confidence is routed to human review, never
  auto-escalated (POL-INT-FALLBACK, clause AUTO-3.1.2);
* a regulatory claim without a clause anchor is suppressed (clause FILE-2.2.1);
* a finding whose evidence cannot be assembled is downgraded, not reported clean
  (clause AUTO-3.1.2);
* PII patterns are masked in anything that leaves the process boundary;
* every guardrail decision is written to the audit log with its rationale.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from backend.config import load_config

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"


@dataclass
class GuardrailResult:
    status: str
    checks: List[Dict[str, Any]] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status != FAIL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "checks_run": len(self.checks),
            "violations": self.violations,
            "warnings": self.warnings,
            "required_actions": self.actions,
            "checks": self.checks,
        }


class GuardrailEngine:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.cfg = config or load_config()
        self.gcfg = self.cfg["guardrails"]
        self._pii = [re.compile(p) for p in self.gcfg.get("pii_patterns", [])]

    # -- PII ----------------------------------------------------------------
    def mask_pii(self, text: str) -> Dict[str, Any]:
        if not isinstance(text, str):
            return {"text": text, "redactions": 0}
        out, count = text, 0
        for pattern in self._pii:
            out, n = pattern.subn("[REDACTED]", out)
            count += n
        return {"text": out, "redactions": count}

    def scan_for_pii(self, text: str) -> List[str]:
        return [p.pattern for p in self._pii if p.search(text or "")]

    # -- findings -----------------------------------------------------------
    def check_finding(self, finding: Dict[str, Any], evidence: Dict[str, Any],
                      policy: Dict[str, Any]) -> GuardrailResult:
        res = GuardrailResult(status=PASS)
        copilot = self.cfg["copilot"]

        # G1 - minimum evidence
        n_ev = int(evidence.get("item_count") or 0)
        ok = n_ev >= 3
        res.checks.append({"id": "G1_EVIDENCE_PRESENT", "status": PASS if ok else FAIL,
                           "detail": f"{n_ev} evidence item(s); minimum 3"})
        if not ok:
            res.violations.append("insufficient_evidence")
            res.actions.append("route_to_human_review")

        # G2 - evidence quality gate
        quality = evidence.get("evidence_quality")
        ok = quality in ("medium", "high")
        res.checks.append({"id": "G2_EVIDENCE_QUALITY", "status": PASS if ok else WARN,
                           "detail": f"evidence_quality={quality}"})
        if not ok:
            res.warnings.append("low_evidence_quality")
            res.actions.append("analyst_review_before_filing")

        # G3 - confidence gate for auto-escalation
        conf = float(finding.get("confidence") or 0.0)
        need = float(copilot["min_auto_escalation_confidence"])
        ok = conf >= need
        res.checks.append({"id": "G3_CONFIDENCE_GATE", "status": PASS if ok else WARN,
                           "detail": f"confidence={conf:.2f} vs auto-escalation floor {need:.2f}"})
        if not ok:
            res.warnings.append("below_auto_escalation_confidence")
            res.actions.append("human_review_required")

        # G4 - every regulatory claim must be clause-anchored
        if self.gcfg.get("require_citation_for_regulatory_claim"):
            clauses = policy.get("governing_clauses") or []
            claims = [p for p in policy.get("matched_policies", [])
                      if p.get("obligation") in ("FILE_CTR", "FILE_SAR", "FILE_STR")]
            ok = (not claims) or bool(clauses)
            res.checks.append({"id": "G4_CITATION_COMPLETENESS",
                               "status": PASS if ok else FAIL,
                               "detail": f"{len(clauses)} governing clause(s) for "
                                         f"{len(claims)} regulatory claim(s)"})
            if not ok:
                res.violations.append("uncited_regulatory_claim")
                res.actions.append("suppress_claim_until_cited")

        # G5 - PII leakage
        blob = str(finding.get("primary_reason", "")) + str(
            [i.get("summary") for i in evidence.get("items", [])][:20])
        hits = self.scan_for_pii(blob)
        res.checks.append({"id": "G5_PII_SCAN", "status": PASS if not hits else WARN,
                           "detail": f"{len(hits)} pattern(s) matched"})
        if hits:
            res.warnings.append("pii_pattern_detected")
            res.actions.append("mask_before_external_sharing")

        # G6 - detector errors degrade, never silently pass
        errored = [c for c, s in (finding.get("signals") or {}).items() if s.get("error")]
        res.checks.append({"id": "G6_RULE_HEALTH", "status": PASS if not errored else WARN,
                           "detail": f"errored rules: {errored or 'none'}"})
        if errored:
            res.warnings.append("rule_errors_present")
            res.actions.append("recompute_before_filing")

        if res.violations:
            res.status = FAIL
        elif res.warnings:
            res.status = WARN
        return res

    # -- answers ------------------------------------------------------------
    def check_answer(self, answer: Dict[str, Any]) -> GuardrailResult:
        res = GuardrailResult(status=PASS)
        copilot = self.cfg["copilot"]

        conf = float(answer.get("confidence") or 0.0)
        floor = float(copilot["min_answer_confidence"])
        abstain = float(copilot["abstention_threshold"])

        if conf < abstain:
            res.status = FAIL
            res.violations.append("confidence_below_abstention_threshold")
            res.actions.append("abstain_and_state_gap")
            res.checks.append({"id": "A1_ABSTENTION", "status": FAIL,
                               "detail": f"confidence={conf:.2f} < {abstain:.2f}"})
        elif conf < floor:
            res.status = WARN
            res.warnings.append("answer_below_confidence_floor")
            res.actions.append("disclose_low_confidence")
            res.checks.append({"id": "A1_CONFIDENCE_FLOOR", "status": WARN,
                               "detail": f"confidence={conf:.2f} < {floor:.2f}"})
        else:
            res.checks.append({"id": "A1_CONFIDENCE_FLOOR", "status": PASS,
                               "detail": f"confidence={conf:.2f} >= {floor:.2f}"})

        is_regulatory = bool(answer.get("is_regulatory_claim"))
        citations = answer.get("citations") or []
        if is_regulatory and self.gcfg.get("require_citation_for_regulatory_claim"):
            ok = len(citations) > 0
            res.checks.append({"id": "A2_CITATION_REQUIRED", "status": PASS if ok else FAIL,
                               "detail": f"{len(citations)} citation(s) on a regulatory claim"})
            if not ok:
                res.violations.append("regulatory_claim_without_citation")
                res.actions.append("refuse_to_assert")
                res.status = FAIL
        else:
            res.checks.append({"id": "A2_CITATION_REQUIRED", "status": PASS,
                               "detail": "non-regulatory answer, citation optional"})

        row_count = int(answer.get("row_count") or 0)
        ok = row_count > 0
        res.checks.append({"id": "A3_EVIDENCE_PRESENT", "status": PASS if ok else FAIL,
                           "detail": f"{row_count} supporting row(s)"})
        if not ok and res.status == PASS:
            res.status = FAIL
            res.violations.append("answer_has_no_supporting_rows")
            res.actions.append("state_that_no_data_supports_the_question")

        pii = self.scan_for_pii(str(answer.get("answer_text", "")))
        res.checks.append({"id": "A4_PII_SCAN", "status": PASS if not pii else WARN,
                           "detail": f"{len(pii)} pattern(s) matched"})
        if pii:
            res.warnings.append("pii_pattern_detected")
            res.actions.append("mask_in_shared_output")

        if res.violations and res.status != FAIL:
            res.status = FAIL
        elif res.warnings and res.status == PASS:
            res.status = WARN
        return res

    # -- input --------------------------------------------------------------
    def validate_question(self, question: str) -> List[str]:
        problems = []
        if not question or not question.strip():
            problems.append("empty_question")
        elif len(question) > 1000:
            problems.append("question_too_long")
        return problems


def apply_output_masking(payload: Any, engine: GuardrailEngine) -> Any:
    """Recursively mask PII in a structure about to leave the process."""
    if isinstance(payload, str):
        return engine.mask_pii(payload)["text"]
    if isinstance(payload, list):
        return [apply_output_masking(v, engine) for v in payload]
    if isinstance(payload, dict):
        return {k: apply_output_masking(v, engine) for k, v in payload.items()}
    return payload
