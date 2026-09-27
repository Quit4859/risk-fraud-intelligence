"""The copilot: wires the agents into one governed workflow.

Flow:  question -> intent -> SQL/retrieval -> signals -> evidence -> policy
       -> finding -> guardrail -> (answer | draft filing) -> human approval

Every step is recorded in the audit log with its run id, so any answer or draft
can be replayed and defended.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from backend.agents.answer_composer import AnswerComposer
from backend.agents.evidence_gatherer import EvidenceGathererAgent
from backend.agents.executors import IntentExecutor
from backend.agents.fraud_detector import FraudDetectorAgent
from backend.agents.intent_router import IntentRouter
from backend.agents.policy_matcher import PolicyMatcherAgent
from backend.agents.report_formatter import ReportFormatterAgent
from backend.agents.retrieval import RetrievalService
from backend.config import load_config
from backend.orchestration.audit_log import AuditLog, new_run_id
from backend.orchestration.guardrails import GuardrailEngine, apply_output_masking
from backend.warehouse import Warehouse


class RiskCopilot:
    def __init__(self, warehouse: Optional[Warehouse] = None,
                 config: Optional[Dict[str, Any]] = None, run_id: Optional[str] = None,
                 actor: str = "copilot"):
        self.cfg = config or load_config()
        self.wh = warehouse or Warehouse(config=self.cfg)
        self.run_id = run_id or new_run_id()
        self.audit = AuditLog(self.wh, run_id=self.run_id, config=self.cfg, actor=actor)

        self.retrieval = RetrievalService(self.wh, config=self.cfg).load()
        self.detector = FraudDetectorAgent(self.wh, self.cfg)
        self.policy = PolicyMatcherAgent(self.wh, self.retrieval, self.cfg)
        self.evidence = EvidenceGathererAgent(self.wh, self.retrieval, self.cfg)
        self.reporter = ReportFormatterAgent(self.cfg)
        self.guardrails = GuardrailEngine(self.cfg)
        self.composer = AnswerComposer(self.cfg)
        self.router = IntentRouter(self.wh, self.cfg)
        self.executor = IntentExecutor(
            self.wh, self.retrieval,
            {"fraud_detector": self.detector, "policy_matcher": self.policy,
             "evidence": self.evidence, "reporter": self.reporter},
            self.cfg)
        self._findings_cache: List[Dict[str, Any]] = []
        self.audit.record("session.start", self.run_id, "OK",
                          "Copilot session initialised",
                          {"data_fingerprint": self.wh.source_fingerprint(),
                           "as_of": self.wh.as_of(),
                           "corpus_clauses": self.retrieval.clause_count,
                           "corpus_error": self.retrieval.last_error})

    # ------------------------------------------------------------------ ask
    def ask(self, question: str, mask_output: bool = False) -> Dict[str, Any]:
        t0 = time.time()
        problems = self.guardrails.validate_question(question)
        if problems:
            return {
                "question": question, "answer_text": "I need a question to work from.",
                "intent": {"intent": "invalid", "confidence": 0.0},
                "guardrail": {"status": "FAIL", "violations": problems},
                "citations": [], "metrics": {}, "rows": [], "sections": [],
                "confidence": 0.0, "limitations": problems,
                "next_actions": ["Rephrase the question and try again."],
                "provenance": {"run_id": self.run_id,
                               "duration_ms": int((time.time() - t0) * 1000)},
            }

        intent = self.router.route(question)
        intent.slots["_question"] = question
        result = self.executor.execute(intent)
        guard = self.guardrails.check_answer({
            "confidence": 1.0, "citations": result.citations,
            "is_regulatory_claim": result.is_regulatory_claim,
            "row_count": result.row_count or len(result.rows),
            "answer_text": "",  # re-evaluated after composition
        })
        answer = self.composer.compose(question, result, guard, self.run_id,
                                        int((time.time() - t0) * 1000))
        answer["provenance"]["data_as_of"] = self.wh.as_of()
        answer["provenance"]["data_fingerprint"] = self.wh.source_fingerprint()
        # Second guardrail pass over the final text: PII scan on real output.
        final_guard = self.guardrails.check_answer({
            "confidence": answer["confidence"], "citations": answer["citations"],
            "is_regulatory_claim": answer["is_regulatory_claim"],
            "row_count": answer["row_count"], "answer_text": answer["answer_text"],
        })
        answer["guardrail"] = final_guard.to_dict()
        if final_guard.status == "FAIL" and answer["answer_text"]:
            answer["answer_text"] = self.composer._refusal(final_guard, result)
            answer["confidence"] = min(answer["confidence"], 0.3)
        if mask_output and self.cfg["guardrails"].get("mask_pii_in_logs"):
            answer = apply_output_masking(answer, self.guardrails)

        self.audit.record(
            "question.ask", intent.name, final_guard.status,
            f"intent={intent.name} confidence={intent.confidence:.2f} "
            f"rows={answer['row_count']} citations={len(answer['citations'])}",
            {"question": question, "intent": intent.to_dict(),
             "sql": result.sql[:5], "violations": final_guard.violations})
        return answer

    # ------------------------------------------------------------- analysis
    def analyze_customer(self, customer_id: str, persist: bool = True) -> Dict[str, Any]:
        t0 = time.time()
        self.audit.record("detect.customer", customer_id, "START",
                          "Running detector portfolio")
        finding = self.detector.detect_customer(customer_id)
        rules = finding["fired_rules"]

        match = self.policy.match(customer_id, rules)
        metrics = {
            "composite_risk_score": finding["composite_score"],
            "cash_aggregate_72h": _cash_aggregate(self.wh, customer_id),
        }
        triggered = self.policy.resolve_obligations(match, metrics)

        evidence = self.evidence.build(customer_id, finding)
        guard = self.guardrails.check_finding(finding, evidence, match)

        case = {
            "customer_id": customer_id,
            "run_id": self.run_id,
            "finding": finding,
            "policy": match,
            "triggered_obligations": triggered,
            "evidence": evidence,
            "guardrail": guard.to_dict(),
            "duration_ms": int((time.time() - t0) * 1000),
            "status": "OPEN",
        }
        if guard.violations and self.cfg["guardrails"].get("block_on_evidence_failure"):
            case["status"] = "BLOCKED"
            self.audit.record("guardrail.evaluate", customer_id, "FAIL",
                              "; ".join(guard.violations),
                              {"checks": [c["id"] for c in guard.checks]})
        else:
            self.audit.record("guardrail.evaluate", customer_id, guard.status,
                              "; ".join(guard.warnings + guard.violations) or "all checks passed",
                              {"checks": [c["id"] for c in guard.checks]})

        if persist:
            self._persist_finding(case)
        self.audit.record("detect.customer", customer_id, case["status"],
                          f"score={finding['composite_score']} "
                          f"level={finding['risk_level']} rules={','.join(rules) or 'none'}",
                          {"evidence_items": evidence["item_count"],
                           "obligations": [o["filing_type"] for o in triggered]})
        return case

    def run_population(self, limit: Optional[int] = None,
                       min_level: str = "medium") -> Dict[str, Any]:
        ids = self.wh.customer_ids(limit)
        order = {"high": 0, "medium": 1, "low": 2}
        cases = []
        for cid in ids:
            case = self.analyze_customer(cid, persist=True)
            if order.get(case["finding"]["risk_level"], 3) <= order[min_level]:
                cases.append(case)
        self._findings_cache = [c["finding"] for c in cases]
        self.executor.agents["findings_cache"] = self._findings_cache
        dist = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
        for c in cases:
            dist[c["finding"]["risk_level"]] += 1
        blocked = [c["customer_id"] for c in cases if c["status"] == "BLOCKED"]
        self.audit.record("detect.population", self.run_id, "OK",
                          f"assessed {len(ids)} customers, {len(cases)} at or above {min_level}",
                          {"risk_distribution": dist, "blocked": len(blocked)})
        return {
            "run_id": self.run_id,
            "assessed": len(ids),
            "escalated": len(cases),
            "risk_distribution": dist,
            "blocked": blocked,
            "cases": cases,
        }

    # -------------------------------------------------------------- filings
    def generate_filing(self, customer_id: str, filing_type: str = "SAR") -> Dict[str, Any]:
        case = self.analyze_customer(customer_id, persist=True)
        finding, match, evidence = case["finding"], case["policy"], case["evidence"]
        if case["status"] == "BLOCKED":
            return {"success": False, "error": "guardrail_blocked",
                    "violations": case["guardrail"]["violations"],
                    "customer_id": customer_id}
        pcfg = self.cfg["policy"]
        # The filing gate must agree with the escalation decision. A case the
        # copilot classifies as HIGH because a clause-bound typology fired
        # (clause STR-2.1.2, GEO-2.2.2) is exactly the case that must be
        # capable of producing a SAR, even when the weighted composite is
        # diluted by the single-signal case. Gating on the composite alone
        # would let the model label a case HIGH and then refuse to file it.
        override = finding.get("typology_override")
        gate_basis = []
        if filing_type == "SAR":
            if finding["composite_score"] >= pcfg["sar_score_threshold"]:
                gate_basis.append(
                    f"composite score {finding['composite_score']} >= "
                    f"{pcfg['sar_score_threshold']}")
            if override:
                gate_basis.append(
                    f"{override['rule_code']} scored {override['rule_score']} >= "
                    f"{override['min_score']}, imposing a {override['raised_level']} floor")
            if not gate_basis:
                return {"success": False,
                        "error": "below_sar_threshold",
                        "detail": f"composite score {finding['composite_score']} is below the "
                                  f"SAR threshold {pcfg['sar_score_threshold']} in "
                                  "config/settings.yaml and no typology override applied",
                        "risk_level": finding["risk_level"]}
        if filing_type in ("CTR", "STR") and \
                _cash_aggregate(self.wh, customer_id) < pcfg["ctr_min_amount"]:
            return {"success": False, "error": "below_ctr_threshold",
                    "detail": "72-hour cash aggregation is below the reporting threshold"}

        report = self.reporter.build(
            filing_type, self.run_id, self.wh,
            finding=finding, evidence=evidence, policy=match,
            triggered=case["triggered_obligations"])
        report["governance"]["filing_gate"] = {
            "filing_type": filing_type,
            "satisfied_by": gate_basis or ["cash aggregation threshold met"],
            "policy": "config/settings.yaml -> policy.sar_score_threshold "
                      "+ detection.typology_override",
        }
        path = self.reporter.persist(report, self.wh, self.run_id, customer_id)
        self.audit.record("filing.generate", report["report_id"], report["governance"]["status"],
                          f"{filing_type} draft generated for {customer_id}; "
                          f"citation coverage "
                          f"{report['governance']['citation_coverage']:.0%}; "
                          f"requires human approval (FILE-2.1.1)",
                          {"path": path, "content_hash": report["governance"]["content_hash"],
                           "manifest_hash": report["governance"].get("manifest_hash")})
        return {"success": True, "filing_id": report["report_id"], "path": path,
                "report": report}

    def approve_filing(self, filing_id: str, approver: str) -> Dict[str, Any]:
        out = self.reporter.approve(filing_id, approver, self.wh)
        self.audit.record("filing.approve", filing_id,
                          "APPROVED" if out.get("success") else "REJECTED",
                          out.get("error", f"approved by {approver}"),
                          {"approver": approver})
        return out

    def build_board_pack(self) -> Dict[str, Any]:
        findings = self.wh.query(
            "SELECT customer_id, risk_level, composite_risk_score, confidence, guardrail_status,"
            " summary FROM findings")
        report = self.reporter.build_miar(findings, self.wh, self.run_id)
        path = self.reporter.persist(report, self.wh, self.run_id)
        self.audit.record("filing.generate", report["report_id"], report["governance"]["status"],
                          "Board MIAR pack generated", {"path": path})
        return {"report_id": report["report_id"], "path": path, "report": report}

    def build_liquidity_report(self) -> Dict[str, Any]:
        report = self.reporter.build_liquidity(self.wh, self.run_id)
        path = self.reporter.persist(report, self.wh, self.run_id)
        self.audit.record("filing.generate", report["report_id"], report["governance"]["status"],
                          "Liquidity and large exposure pack generated", {"path": path})
        return {"report_id": report["report_id"], "path": path, "report": report}

    # ------------------------------------------------------------- persist
    def _persist_finding(self, case: Dict[str, Any]) -> str:
        finding = case["finding"]
        cid = case["customer_id"]
        summary = finding["primary_reason"]
        payload = {
            "signals": {k: {kk: vv for kk, vv in v.items() if kk != "metrics"}
                        for k, v in finding["signals"].items()},
            "rule_contributions": finding["rule_contributions"],
        }
        ev = {
            "item_count": case["evidence"]["item_count"],
            "manifest_hash": case["evidence"]["manifest_hash"],
            "quality": case["evidence"]["evidence_quality"],
            "transaction_ids": finding["evidence_txn_ids"],
        }
        clauses = [{"clause_ref": c["clause_ref"], "citation": c["citation"]}
                   for c in case["policy"]["governing_clauses"]]
        content = hashlib.sha256(
            json.dumps([summary, ev, clauses], sort_keys=True, default=str).encode()
        ).hexdigest()[:32]
        finding_id = f"FND-{cid}-{self.run_id[-6:]}"
        self.wh.execute(
            "INSERT OR REPLACE INTO findings (finding_id, customer_id, run_id, created_at,"
            " composite_risk_score, risk_level, confidence, signals_json, evidence_json,"
            " clauses_json, summary, guardrail_status, content_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (finding_id, cid, self.run_id,
             datetime.utcnow().isoformat(timespec="seconds") + "Z",
             finding["composite_score"], finding["risk_level"], finding["confidence"],
             json.dumps(payload, default=str), json.dumps(ev, default=str),
             json.dumps(clauses, default=str), summary,
             case["guardrail"]["status"], content))
        return finding_id

    # ---------------------------------------------------------------- admin
    def corpus_status(self) -> Dict[str, Any]:
        return {
            "documents": len(self.retrieval.docs),
            "clauses": self.retrieval.clause_count,
            "policies": self.wh.table_count("policies"),
            "error": self.retrieval.last_error,
            "corpus_dir": self.retrieval.corpus_dir,
        }

    def status(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "as_of_date": self.wh.as_of(),
            "data_fingerprint": self.wh.source_fingerprint(),
            "tables": {t: self.wh.table_count(t) for t in
                       ("customers", "accounts", "transactions", "findings", "filings",
                        "policies", "document_chunks", "audit_log")},
            "corpus": self.corpus_status(),
            "config_version": self.cfg["copilot"]["version"],
            "environment": self.cfg["copilot"]["environment"],
            "thresholds": self.cfg["detection"]["risk_bands"],
        }

    def close(self) -> None:
        self.wh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _cash_aggregate(wh, customer_id: str) -> float:
    row = wh.one("SELECT aggregate_amount FROM sem_cash_72h_aggregation"
                 " WHERE customer_id = ? AND rn = 1", (customer_id,))
    try:
        return float(row["aggregate_amount"]) if row else 0.0
    except (TypeError, ValueError):
        return 0.0
