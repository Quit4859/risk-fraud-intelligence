"""External actions via MCP-style connectors.

The copilot's read path (ask / detect) is side-effect free. Everything that
*acts* - filing a Jira ticket, posting to Slack, writing an evidence bundle to
shared storage - goes through a connector here, and every call is audited with
its payload hash.

Connectors fail soft: a failed escalation returns ``success: False`` and the
case stays open for a human. It never raises into the workflow, and it never
fabricates a success. In ``simulation`` mode (the default for the hackathon)
they write a deterministic receipt instead of calling a live endpoint, so the
demo is reproducible offline and no secret is ever needed.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.config import ROOT, load_config


def _receipt(connector: str, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "connector": connector,
        "action": action,
        "mode": "simulated",
        "payload_hash": hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16],
        "payload_keys": sorted(payload),
        "receipt_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


class BaseConnector:
    name = "base"

    def __init__(self, config: Optional[Dict[str, Any]] = None, dry_run: bool = True):
        self.cfg = config or load_config()
        settings = self.cfg.get("mcp", {}).get(name := self.name, {})
        self.settings = settings
        self.enabled = bool(settings.get("enabled"))
        self.dry_run = dry_run or not self.enabled

    def status(self) -> Dict[str, Any]:
        return {"connector": self.name, "enabled": self.enabled,
                "dry_run": self.dry_run, "settings": {
                    k: v for k, v in self.settings.items() if "url" not in k.lower()
                    and "token" not in k.lower()}}


class FilesystemEvidenceConnector(BaseConnector):
    """Writes the evidence bundle for a case to a shared location.

    This is the local stand-in for an MCP filesystem/Google Drive connector and
    is the connector used by the CLI and the app, because an evidence bundle
    really does have to land somewhere durable.
    """

    name = "filesystem_evidence"

    def write_bundle(self, case: Dict[str, Any], run_id: str) -> Dict[str, Any]:
        cid = case["customer_id"]
        payload = {
            "customer_id": cid,
            "run_id": run_id,
            "risk_level": case["finding"]["risk_level"],
            "composite_score": case["finding"]["composite_score"],
            "confidence": case["finding"]["confidence"],
            "fired_rules": case["finding"]["fired_rules"],
            "evidence": case["evidence"],
            "governing_clauses": case["policy"]["governing_clauses"],
            "triggered_obligations": case["triggered_obligations"],
            "guardrail": case["guardrail"],
            "exported_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        out_dir = os.path.join(ROOT, self.cfg["paths"]["evidence_dir"], run_id)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{cid}_case.json")
        if self.dry_run:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
        receipt = _receipt(self.name, "write_bundle", {"path": path, "customer_id": cid})
        return {"success": True, "path": path, "receipt": receipt}


class JiraConnector(BaseConnector):
    """Escalates a high-risk finding to the investigations tracker."""

    name = "jira"

    def create_issue(self, case: Dict[str, Any], run_id: str) -> Dict[str, Any]:
        f = case["finding"]
        body = {
            "customer_id": f["customer_id"],
            "risk_level": f["risk_level"],
            "composite_score": f["composite_score"],
            "confidence": f["confidence"],
            "fired_rules": f["fired_rules"],
            "primary_reason": f["primary_reason"],
            "evidence_items": case["evidence"]["item_count"],
            "manifest_hash": case["evidence"]["manifest_hash"],
            "governing_clauses": [c["citation"] for c in case["policy"]["governing_clauses"]],
            "obligations": [o["filing_type"] for o in case["triggered_obligations"]],
            "guardrail_status": case["guardrail"]["status"],
            "run_id": run_id,
        }
        summary = (f"[{f['risk_level'].upper()}] {f['customer_id']} - "
                   f"{f['composite_score']:.3f} ({', '.join(f['fired_rules']) or 'no rule'})")
        payload = {"project": self.settings.get("project_key", "RISK"),
                   "summary": summary, "description": body,
                   "labels": ["risk-copilot", f["risk_level"], *f["fired_rules"]],
                   "issuetype": "Task"}
        if self.dry_run:
            return {"success": True, "simulated": True,
                    "key": f"SIM-{hashlib.sha256(summary.encode()).hexdigest()[:6].upper()}",
                    "receipt": _receipt(self.name, "create_issue", payload),
                    "payload": payload}
        return self._post(payload)

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            import requests
            base = self.settings.get("base_url", "").rstrip("/")
            resp = requests.post(
                f"{base}/rest/api/3/issue", json=payload, timeout=20,
                headers={"Content-Type": "application/json"},
                auth=(os.environ.get("JIRA_USER", ""), os.environ.get("JIRA_TOKEN", "")))
            resp.raise_for_status()
            data = resp.json()
            return {"success": True, "simulated": False, "key": data.get("key"),
                    "url": f"{base}/browse/{data.get('key')}"}
        except Exception as exc:
            return {"success": False, "error": str(exc),
                    "note": "case remains open for human escalation"}


class SlackConnector(BaseConnector):
    """Posts a governed alert to the financial crime channel."""

    name = "slack"

    def post_alert(self, case: Dict[str, Any], run_id: str) -> Dict[str, Any]:
        f = case["finding"]
        colour = {"high": "#d7263d", "medium": "#f46036", "low": "#2e7d32"}.get(
            f["risk_level"], "#4a4a4a")
        text = (f"*RISK ALERT - {f['risk_level'].upper()}*\n"
                f"Customer: `{f['customer_id']}`\n"
                f"Composite score: {f['composite_score']:.3f} "
                f"(confidence {f['confidence']:.2f})\n"
                f"Rules: {', '.join(f['fired_rules']) or 'none'}\n"
                f"Driver: {f['primary_reason']}\n"
                f"Clauses: {', '.join(c['clause_ref'] for c in case['policy']['governing_clauses']) or 'n/a'}\n"
                f"Run: `{run_id}`")
        payload = {"channel": self.settings.get("channel", "#risk-alerts"),
                   "text": text, "attachments": [{"color": colour}]}
        if self.dry_run:
            return {"success": True, "simulated": True,
                    "receipt": _receipt(self.name, "post_alert", payload),
                    "payload": payload}
        try:
            import requests
            resp = requests.post(self.settings.get("webhook_url", ""), json=payload,
                                 timeout=20, headers={"Content-Type": "application/json"})
            resp.raise_for_status()
            return {"success": True, "simulated": False,
                    "channel": payload["channel"]}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class RegulatedFilingConnector(BaseConnector):
    """Hands an approved filing to the regulator submission gateway.

    Refuses to transmit anything that is not in APPROVED state: this is the
    machine-side enforcement of the four-eyes control (clause FILE-2.1.1).
    """

    name = "filing_gateway"

    def submit(self, filing: Dict[str, Any], approver: str) -> Dict[str, Any]:
        gov = filing.get("governance", {})
        if gov.get("status") != "APPROVED":
            return {"success": False, "error": "four_eyes_not_satisfied",
                    "detail": f"filing status is {gov.get('status')}; clause FILE-2.1.1 "
                              "requires a named human approver before submission"}
        if not approver:
            return {"success": False, "error": "approver_required"}
        if gov.get("citation_coverage", 0) < 1.0:
            return {"success": False, "error": "citation_coverage_incomplete",
                    "detail": "clause FILE-2.2.1: unanchored regulatory claims cannot be filed"}
        payload = {"filing_id": filing.get("report_id"), "approver": approver,
                   "content_hash": gov.get("content_hash")}
        return {"success": True, "simulated": True,
                "receipt": _receipt(self.name, "submit", payload), "payload": payload}


class ConnectorRegistry:
    def __init__(self, config: Optional[Dict[str, Any]] = None, dry_run: bool = True):
        self.cfg = config or load_config()
        self.dry_run = dry_run
        self.connectors = {
            c.name: c for c in [
                FilesystemEvidenceConnector(self.cfg, dry_run),
                JiraConnector(self.cfg, dry_run),
                SlackConnector(self.cfg, dry_run),
                RegulatedFilingConnector(self.cfg, dry_run),
            ]
        }

    def get(self, name: str) -> Optional[BaseConnector]:
        return self.connectors.get(name)

    def escalate(self, case: Dict[str, Any], run_id: str) -> Dict[str, Any]:
        """Run the escalation ladder: bundle -> tracker -> chat."""
        out = {}
        fs = self.connectors["filesystem_evidence"]
        out["filesystem_evidence"] = fs.write_bundle(case, run_id)
        if case["finding"]["risk_level"] == "high":
            out["jira"] = self.connectors["jira"].create_issue(case, run_id)
            out["slack"] = self.connectors["slack"].post_alert(case, run_id)
        return out

    def status(self) -> List[Dict[str, Any]]:
        return [c.status() for c in self.connectors.values()]


# --------------------------------------------------------------------------
# MCP server descriptor
# --------------------------------------------------------------------------
# Shipped so the copilot can be registered as an MCP server and driven from any
# MCP client (CoCo Desktop, another agent, a Slack bot). Tool names are stable
# and every tool is read-only except `escalate_case` and `generate_filing`,
# which are explicitly marked as write actions.
MCP_SERVER_MANIFEST = {
    "name": "risk-fraud-regulatory-copilot",
    "version": "1.0.0",
    "description": "Governed risk, fraud and regulatory intelligence over "
                   "transaction, account and policy data.",
    "tools": [
        {"name": "ask", "write": False,
         "description": "Answer a natural-language risk, fraud or regulatory question "
                        "with citations, SQL provenance and a guardrail verdict."},
        {"name": "detect_customer", "write": False,
         "description": "Run the detector portfolio for one customer and return an "
                        "explainable composite score."},
        {"name": "portfolio_risks", "write": False,
         "description": "Ranked medium/high risk customers for the portfolio."},
        {"name": "policy_lookup", "write": False,
         "description": "Retrieve citable clauses from the policy corpus."},
        {"name": "run_population_scan", "write": True,
         "description": "Assess the whole portfolio and persist findings."},
        {"name": "generate_filing", "write": True,
         "description": "Produce a draft SAR/CTR/STR. Always DRAFT; requires human "
                        "approval before submission (clause FILE-2.1.1)."},
        {"name": "escalate_case", "write": True,
         "description": "Push a case bundle to the investigations tracker and alert channel."},
        {"name": "audit_trace", "write": False,
         "description": "Return the audit trail for a run."},
    ],
}


def handle_tool_call(name: str, arguments: Dict[str, Any],
                     copilot=None, dry_run: bool = True) -> Dict[str, Any]:
    """MCP tool dispatcher used by the server entrypoint and the Vercel API."""
    from backend.orchestration.copilot import RiskCopilot
    own = copilot is None
    copilot = copilot or RiskCopilot()
    try:
        if name == "ask":
            return copilot.ask(arguments.get("question", ""))
        if name == "detect_customer":
            cid = arguments.get("customer_id")
            if not cid:
                return {"error": "customer_id is required"}
            return copilot.analyze_customer(cid, persist=False)
        if name == "portfolio_risks":
            limit = int(arguments.get("limit") or 10)
            return copilot.wh.query(
                "SELECT customer_id, composite_risk_score, risk_level, confidence, summary"
                " FROM findings WHERE risk_level IN ('high','medium')"
                " ORDER BY composite_risk_score DESC LIMIT ?", (limit,))
        if name == "policy_lookup":
            hits = copilot.retrieval.search(arguments.get("query", ""),
                                            top_k=int(arguments.get("top_k") or 5))
            return {"clauses": hits, "count": len(hits)}
        if name == "run_population_scan":
            return {k: v for k, v in copilot.run_population(
                limit=arguments.get("limit")).items() if k != "cases"}
        if name == "generate_filing":
            return copilot.generate_filing(arguments.get("customer_id", ""),
                                           arguments.get("filing_type", "SAR"))
        if name == "escalate_case":
            case = copilot.analyze_customer(arguments.get("customer_id", ""), persist=True)
            return ConnectorRegistry(copilot.cfg, dry_run).escalate(case, copilot.run_id)
        if name == "audit_trace":
            return {"run_id": copilot.run_id, "events": copilot.audit.trace(
                arguments.get("run_id"), int(arguments.get("limit") or 200))}
        return {"error": f"unknown tool: {name}"}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        if own:
            copilot.close()
