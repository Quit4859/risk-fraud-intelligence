"""Multi-agent orchestration: explicit handoffs and shared context.

The workflow is deliberately boring. Each stage has one owner, one input, one
output, and a named handoff artefact. If a stage fails the case degrades to
human review with a recorded reason - it does not silently continue, and it
never produces a clean-looking result out of partial data.

    DetectorAgent  --finding-->  PolicyMatcherAgent
    PolicyMatcher   --obligations+clauses-->  EvidenceGathererAgent
    EvidenceGatherer --evidence-->  GuardrailEngine
    GuardrailEngine --PASS-->  ReportFormatterAgent --> human approval

Shared context (``WorkflowContext``) carries the run id, the data fingerprint and
the accumulated audit trail so every agent stamps the same provenance.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.agents.evidence_gatherer import EvidenceGathererAgent
from backend.agents.fraud_detector import FraudDetectorAgent
from backend.agents.policy_matcher import PolicyMatcherAgent
from backend.agents.report_formatter import ReportFormatterAgent
from backend.agents.retrieval import RetrievalService
from backend.config import load_config
from backend.orchestration.audit_log import AuditLog, new_run_id
from backend.orchestration.guardrails import GuardrailEngine
from backend.orchestration.mcp_integrations import ConnectorRegistry
from backend.warehouse import Warehouse


@dataclass
class WorkflowContext:
    run_id: str
    data_fingerprint: str
    as_of_date: str
    steps: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: Dict[str, Any] = field(default_factory=dict)

    def handoff(self, stage: str, status: str, detail: str,
                artefact: Optional[Dict[str, Any]] = None,
                duration_ms: int = 0) -> None:
        self.steps.append({"stage": stage, "status": status, "detail": detail,
                           "duration_ms": duration_ms, "run_id": self.run_id})
        if artefact is not None:
            self.artifacts[stage] = artefact


class MultiAgentWorkflow:
    """Chains the agents for a single case, with per-stage failure isolation."""

    def __init__(self, warehouse: Optional[Warehouse] = None,
                 config: Optional[Dict[str, Any]] = None, run_id: Optional[str] = None,
                 dry_run_connectors: bool = True):
        self.cfg = config or load_config()
        self.wh = warehouse or Warehouse(config=self.cfg)
        self.run_id = run_id or new_run_id()
        self.audit = AuditLog(self.wh, run_id=self.run_id, config=self.cfg)
        self.retrieval = RetrievalService(self.wh, config=self.cfg).load()
        self.detector = FraudDetectorAgent(self.wh, self.cfg)
        self.policy = PolicyMatcherAgent(self.wh, self.retrieval, self.cfg)
        self.evidence = EvidenceGathererAgent(self.wh, self.retrieval, self.cfg)
        self.reporter = ReportFormatterAgent(self.cfg)
        self.guardrails = GuardrailEngine(self.cfg)
        self.connectors = ConnectorRegistry(self.cfg, dry_run=dry_run_connectors)

    def new_context(self) -> WorkflowContext:
        return WorkflowContext(run_id=self.run_id,
                               data_fingerprint=self.wh.source_fingerprint(),
                               as_of_date=self.wh.as_of())

    def analyze(self, customer_id: str, filing_type: Optional[str] = None) -> Dict[str, Any]:
        ctx = self.new_context()
        t0 = time.time()

        # 1. Signals
        stage_t = time.time()
        finding = self.detector.detect_customer(customer_id)
        ctx.handoff("fraud_detection", "completed",
                    f"rules fired: {', '.join(finding['fired_rules']) or 'none'}",
                    {"composite_score": finding["composite_score"],
                     "risk_level": finding["risk_level"]},
                    int((time.time() - stage_t) * 1000))

        # 2. Policy
        stage_t = time.time()
        try:
            match = self.policy.match(customer_id, finding["fired_rules"])
            cash = self._cash_aggregate(customer_id)
            triggered = self.policy.resolve_obligations(
                match, {"composite_risk_score": finding["composite_score"],
                        "cash_aggregate_72h": cash})
            ctx.handoff("policy_matching", "completed",
                        f"{len(match['matched_policies'])} policy/policies matched, "
                        f"{len(triggered)} obligation(s) triggered",
                        {"filings_indicated": sorted({t['filing_type'] for t in triggered})},
                        int((time.time() - stage_t) * 1000))
        except Exception as exc:
            match = {"matched_policies": [], "governing_clauses": [], "obligations_total": 0}
            triggered = []
            ctx.handoff("policy_matching", "failed", str(exc))

        # 3. Evidence
        stage_t = time.time()
        try:
            evidence = self.evidence.build(customer_id, finding)
            ctx.handoff("evidence_gathering", "completed",
                        f"{evidence['item_count']} item(s), quality "
                        f"{evidence['evidence_quality']}",
                        {"manifest_hash": evidence["manifest_hash"]},
                        int((time.time() - stage_t) * 1000))
        except Exception as exc:
            evidence = {"customer_id": customer_id, "item_count": 0, "items": [],
                        "evidence_quality": "low", "quality_reasons": [str(exc)],
                        "manifest_hash": "", "transaction_count": 0, "clause_count": 0}
            ctx.handoff("evidence_gathering", "failed", str(exc))

        case = {
            "customer_id": customer_id,
            "run_id": self.run_id,
            "finding": finding,
            "policy": match,
            "triggered_obligations": triggered,
            "evidence": evidence,
        }

        # 4. Guardrail
        stage_t = time.time()
        guard = self.guardrails.check_finding(finding, evidence, match)
        case["guardrail"] = guard.to_dict()
        case["status"] = "BLOCKED" if guard.violations else "OPEN"
        ctx.handoff("guardrail", guard.status,
                    "; ".join(guard.violations + guard.warnings) or "all checks passed",
                    {"checks": [c["id"] for c in guard.checks]},
                    int((time.time() - stage_t) * 1000))
        self.audit.record("guardrail.evaluate", customer_id, guard.status,
                          "; ".join(guard.violations + guard.warnings) or "pass")

        # 5. Optional filing draft
        filing = None
        if filing_type and not guard.violations:
            stage_t = time.time()
            try:
                filing = self.reporter.build(
                    filing_type, self.run_id, self.wh, finding=finding,
                    evidence=evidence, policy=match, triggered=triggered)
                path = self.reporter.persist(filing, self.wh, self.run_id, customer_id)
                ctx.handoff(f"{filing_type.lower()}_generation", "completed",
                            f"draft {filing['report_id']} -> {path}",
                            {"content_hash": filing["governance"]["content_hash"],
                             "citation_coverage": filing["governance"]["citation_coverage"]},
                            int((time.time() - stage_t) * 1000))
                self.audit.record("filing.generate", filing["report_id"],
                                  filing["governance"]["status"],
                                  f"draft {filing_type} for {customer_id}; "
                                  "awaiting human approval (FILE-2.1.1)",
                                  {"path": path})
            except Exception as exc:
                ctx.handoff(f"{filing_type.lower()}_generation", "failed", str(exc))
        elif filing_type:
            ctx.handoff(f"{filing_type.lower()}_generation", "skipped",
                        "blocked by guardrail: " + "; ".join(guard.violations))

        case["workflow_steps"] = ctx.steps
        case["duration_ms"] = int((time.time() - t0) * 1000)
        case["run_id"] = self.run_id
        return case

    def analyze_batch(self, customer_ids: List[str],
                      filing_type: Optional[str] = None) -> Dict[str, Any]:
        results, errors = [], []
        for cid in customer_ids:
            try:
                results.append(self.analyze(cid, filing_type))
            except Exception as exc:
                errors.append({"customer_id": cid, "error": str(exc)})
        dist = {"high": 0, "medium": 0, "low": 0}
        for r in results:
            dist[r["finding"]["risk_level"]] += 1
        self.audit.record("detect.population", self.run_id, "OK",
                          f"batch of {len(customer_ids)}", {"errors": len(errors)})
        return {
            "run_id": self.run_id,
            "batch_size": len(customer_ids),
            "successful": len(results),
            "failed": len(errors),
            "errors": errors,
            "risk_distribution": dist,
            "blocked": [r["customer_id"] for r in results if r["status"] == "BLOCKED"],
            "cases": results,
        }

    def escalate(self, case: Dict[str, Any]) -> Dict[str, Any]:
        result = self.connectors.escalate(case, self.run_id)
        for connector, outcome in result.items():
            self.audit.record(f"escalation.{connector}",
                              case["customer_id"],
                              "OK" if outcome.get("success") else "ERROR",
                              "escalated" if outcome.get("success")
                              else outcome.get("error", "failed"),
                              {"simulated": outcome.get("simulated")})
        return result

    def _cash_aggregate(self, customer_id: str) -> float:
        row = self.wh.one("SELECT aggregate_amount FROM sem_cash_72h_aggregation"
                          " WHERE customer_id = ? AND rn = 1", (customer_id,))
        try:
            return float(row["aggregate_amount"]) if row else 0.0
        except (TypeError, ValueError):
            return 0.0
