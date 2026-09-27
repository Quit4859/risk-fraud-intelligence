"""Vercel serverless entrypoint for the Risk, Fraud & Regulatory Intelligence
Copilot.

One function serves every route so a warm instance reuses the same built
warehouse. Routing is explicit and read-only by default: any state-changing
action (``scan``, ``file``, ``escalate``) must be requested by POST and is
recorded in the audit log.

    GET  /api                     -> health + status
    GET  /api?action=status       -> warehouse, corpus and threshold status
    GET  /api?action=findings     -> ranked findings
    GET  /api?action=policy&q=... -> citable clauses
    GET  /api?action=example_questions
    POST /api  {"action":"ask","question":"..."}
    POST /api  {"action":"detect","customer_id":"CUST-000123"}
    POST /api  {"action":"scan","limit":200}
    POST /api  {"action":"file","customer_id":"...","filing_type":"SAR"}
    POST /api  {"action":"approve","filing_id":"...","approver":"a.b@bank.com"}
    POST /api  {"action":"escalate","customer_id":"..."}
    POST /api  {"action":"board_pack"} | {"action":"liquidity"}
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from api._runtime import get_copilot, health  # noqa: E402

MAX_ROWS = 100

EXAMPLE_QUESTIONS = [
    "which customers are structuring cash to avoid the reporting threshold?",
    "show the top 10 highest risk customers",
    "assess the risk profile of customer CUST-000123",
    "why was CUST-000123 flagged?",
    "do I need to file a SAR for CUST-000123?",
    "which mule network clusters are active?",
    "what does the regulation say about politically exposed persons?",
    "what is our liquidity position and are there large exposure breaches?",
    "explain the structuring rule",
    "show vulnerable obligors above the 10% PD threshold",
    "where are we geographically concentrated in elevated-risk jurisdictions?",
    "what is the current portfolio risk position?",
]


def _json(payload: Any, status: int = 200) -> Tuple[Dict[str, Any], int]:
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json",
                        "Cache-Control": "no-store",
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Headers": "Content-Type",
                        "Access-Control-Allow-Methods": "GET, POST, OPTIONS"},
            "body": json.dumps(payload, default=str)[:200000]}, status


def _body(event: Dict[str, Any]) -> Dict[str, Any]:
    raw = event.get("body") or "{}"
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        return {}


def _trim_rows(rows) -> list:
    rows = list(rows or [])
    return rows[:MAX_ROWS]


def _query(event: Dict[str, Any]) -> Dict[str, list]:
    """Merge every way Vercel can deliver a query string.

    The Node runtime passes ``queryStringParameters``; the Python runtime passes
    ``rawQueryString`` (and sometimes only ``path``). Handling all three keeps
    ``/api?action=x`` and ``POST {"action": "x"}`` equivalent.
    """
    merged: Dict[str, list] = {}
    raw = event.get("rawQueryString")
    if not raw:
        raw = urlparse(event.get("path") or "").query
    for k, v in parse_qs(raw or "").items():
        merged[k] = v if isinstance(v, list) else [v]
    for k, v in (event.get("queryStringParameters") or {}).items():
        merged[k] = v if isinstance(v, list) else [v]
    return merged


def handler(event: Dict[str, Any], context: Any = None):
    if event.get("httpMethod") == "OPTIONS":
        return _json({"ok": True})

    method = (event.get("httpMethod") or "GET").upper()
    if method not in ("GET", "POST"):
        return _json({"error": f"method {method} not allowed"}, 405)

    params = _query(event)
    payload = _body(event) if method == "POST" else {}
    action = (payload.get("action") or (params.get("action") or [""])[0] or
              ("ask" if method == "POST" else "health")).lower()

    try:
        copilot = get_copilot()
    except Exception as exc:
        return _json({"ready": False, "error": str(exc),
                      "trace": traceback.format_exc()[-1500:]}, 503)
    if copilot is None:
        return _json(health(), 503)

    try:
        return _dispatch(copilot, action, payload, params)
    except Exception as exc:
        return _json({"error": str(exc), "action": action,
                      "trace": traceback.format_exc()[-1500:]}, 500)


def _dispatch(copilot, action: str, payload: Dict[str, Any], params) -> Tuple[Dict[str, Any], int]:
    one = lambda key, default=None: payload.get(key) or (params.get(key) or [default])[0]  # noqa: E731

    if action in ("health", ""):
        return _json(health())

    if action == "status":
        return _json(copilot.status())

    if action == "examples":
        return _json({"questions": EXAMPLE_QUESTIONS})

    if action == "ask":
        question = one("question", "")
        if not question:
            return _json({"error": "question is required"}, 400)
        answer = copilot.ask(str(question), mask_output=True)
        answer.pop("rows", None) if len(answer.get("rows") or []) > 30 else None
        return _json(answer)

    if action == "detect":
        cid = one("customer_id", "")
        if not cid:
            return _json({"error": "customer_id is required"}, 400)
        case = copilot.analyze_customer(str(cid), persist=True)
        return _json(_slim_case(case))

    if action == "scan":
        limit = int(one("limit", 150) or 150)
        out = copilot.run_population(limit=limit, min_level=one("min_level", "medium"))
        cases = out.pop("cases", [])
        return _json({**out, "top": [
            {"customer_id": c["customer_id"],
             "risk_level": c["finding"]["risk_level"],
             "composite_score": c["finding"]["composite_score"],
             "confidence": c["finding"]["confidence"],
             "fired_rules": c["finding"]["fired_rules"],
             "primary_reason": c["finding"]["primary_reason"],
             "status": c["status"]}
            for c in sorted(cases, key=lambda c: -c["finding"]["composite_score"])[:25]]})

    if action in ("findings", "portfolio_risks"):
        limit = int(one("limit", 25) or 25)
        rows = copilot.wh.query(
            "SELECT finding_id, customer_id, composite_risk_score, risk_level, confidence,"
            " guardrail_status, summary, created_at FROM findings"
            " WHERE risk_level IN ('high','medium')"
            " ORDER BY composite_risk_score DESC LIMIT ?", (limit,))
        return _json({"findings": rows, "count": len(rows),
                      "as_of_date": copilot.wh.as_of()})

    if action == "policy":
        query = one("query", one("q", ""))
        if not query:
            return _json({"error": "query is required"}, 400)
        hits = copilot.retrieval.search(str(query), top_k=int(one("top_k", 5) or 5))
        return _json({"clauses": _trim_rows(hits), "count": len(hits)})

    if action == "file":
        cid = one("customer_id", "")
        if not cid:
            return _json({"error": "customer_id is required"}, 400)
        out = copilot.generate_filing(str(cid), str(one("filing_type", "SAR")).upper())
        if not out.get("success"):
            return _json(out, 400)
        report = out["report"]
        return _json({"filing_id": out["filing_id"], "report_type": report["report_type"],
                      "governance": report["governance"],
                      "subject": report.get("subject"),
                      "regulatory_basis": report.get("regulatory_basis"),
                      "suspicious_activity": report.get("suspicious_activity"),
                      "evidence_manifest": report.get("evidence_manifest"),
                      "attestation": report.get("attestation")})

    if action == "approve":
        filing_id = one("filing_id", "")
        approver = one("approver", "")
        if not filing_id:
            return _json({"error": "filing_id is required"}, 400)
        out = copilot.approve_filing(str(filing_id), str(approver))
        return _json(out, 200 if out.get("success") else 400)

    if action == "escalate":
        cid = one("customer_id", "")
        if not cid:
            return _json({"error": "customer_id is required"}, 400)
        from backend.orchestration.multi_agent_workflow import MultiAgentWorkflow
        case = copilot.analyze_customer(str(cid), persist=True)
        wf = MultiAgentWorkflow(copilot.wh, copilot.cfg, run_id=copilot.run_id)
        return _json(wf.escalate(case))

    if action == "board_pack":
        out = copilot.build_board_pack()
        return _json({"report_id": out["report_id"],
                      "report": out["report"]})

    if action == "liquidity":
        out = copilot.build_liquidity_report()
        return _json({"report_id": out["report_id"], "report": out["report"]})

    if action == "audit":
        return _json({"run_id": copilot.run_id,
                      "events": copilot.audit.trace(one("run_id"),
                                                    int(one("limit", 50) or 50))})

    if action == "corpus":
        return _json(copilot.corpus_status())

    if action == "mcp":
        from backend.orchestration.mcp_integrations import (
            MCP_SERVER_MANIFEST, ConnectorRegistry, handle_tool_call)
        tool = one("tool", "")
        if not tool:
            return _json({"server": MCP_SERVER_MANIFEST,
                          "connectors": ConnectorRegistry(copilot.cfg).status()})
        args = payload.get("args") or {}
        return _json(handle_tool_call(str(tool), args, copilot))

    return _json({"error": f"unknown action: {action}",
                  "available": ["health", "status", "examples", "ask", "detect", "scan",
                                "findings", "policy", "file", "approve", "escalate",
                                "board_pack", "liquidity", "audit", "corpus", "mcp"]}, 400)


def _slim_case(case: Dict[str, Any]) -> Dict[str, Any]:
    """Drop the bulky evidence payloads; keep hashes and counts."""
    evidence = case["evidence"]
    return {
        "customer_id": case["customer_id"],
        "run_id": case["run_id"],
        "status": case["status"],
        "duration_ms": case["duration_ms"],
        "finding": {k: v for k, v in case["finding"].items() if k != "signals"},
        "signals": {k: {"rule_code": v["rule_code"], "score": v["score"],
                        "fired": v["fired"], "reasons": v["reasons"],
                        "evidence_count": v.get("evidence_count", 0),
                        "metrics": v.get("metrics", {})}
                    for k, v in case["finding"]["signals"].items()},
        "matched_policies": [p["policy_id"] for p in case["policy"]["matched_policies"]],
        "governing_clauses": [c["citation"] for c in case["policy"]["governing_clauses"]],
        "triggered_obligations": case["triggered_obligations"],
        "guardrail": {k: v for k, v in case["guardrail"].items() if k != "checks"},
        "evidence": {"item_count": evidence["item_count"],
                     "items_by_source": evidence["items_by_source"],
                     "evidence_quality": evidence["evidence_quality"],
                     "manifest_hash": evidence["manifest_hash"],
                     "quality_reasons": evidence["quality_reasons"]},
    }
