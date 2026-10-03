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
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from api._runtime import get_copilot, health, runtime_status  # noqa: E402
from backend.governance import rbac  # noqa: E402

MAX_ROWS = 100
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

#: Example questions offered in the UI. `{subject}` is substituted at runtime
#: with a customer that is genuinely flagged, so the demo never points at a
#: benign customer and then abstains. See :func:`example_questions`.
EXAMPLE_QUESTIONS = [
    "which customers are structuring cash to avoid the reporting threshold?",
    "show the top 10 highest risk customers",
    "assess the risk profile of customer {subject}",
    "why was {subject} flagged?",
    "do I need to file a SAR for {subject}?",
    "which mule network clusters are active?",
    "what does the regulation say about politically exposed persons?",
    "what is our liquidity position and are there large exposure breaches?",
    "explain the structuring rule",
    "show vulnerable obligors above the 10% PD threshold",
    "where are we geographically concentrated in elevated-risk jurisdictions?",
    "what is the current portfolio risk position?",
]


def example_questions(copilot=None) -> list:
    """Resolve ``{subject}`` against the live data.

    A hardcoded customer id was the cause of two abstaining examples: on the
    7%-prevalence dataset the fixed id was a benign control, so "do I need to
    file a SAR for CUST-000123?" correctly concluded "no", which the guardrails
    then refused to state without a citation. Pointing the demo at a customer
    the detector actually flagged is both more useful and more honest.
    """
    subject = "CUST-000123"
    try:
        if copilot is not None:
            copilot.ensure_populated()
            row = copilot.wh.one(
                "SELECT customer_id FROM findings WHERE risk_level = 'high'"
                " ORDER BY composite_risk_score DESC LIMIT 1")
            if row:
                subject = row["customer_id"]
    except Exception:
        # Never let the suggestion list break the console.
        pass
    return [q.format(subject=subject) for q in EXAMPLE_QUESTIONS]


def _json(payload: Any, status: int = 200,
          extra_headers: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, Any], int]:
    """Serialise a response.

    The body is never truncated. The previous implementation sliced the
    serialised string at 200,000 characters, which produced *invalid JSON* for
    any large response - a client parsing it got "Unterminated string" rather
    than a partial result. Oversized responses are now made small at the source
    by :func:`_paginate`, which reports ``total`` and ``truncated`` instead.
    """
    body = json.dumps(payload, default=str)
    headers = {"Content-Type": "application/json",
               "Cache-Control": "no-store",
               # Same-origin only. A wildcard here let any website in the
               # browser call the write actions.
               "Vary": "Origin",
               "Access-Control-Allow-Headers": "Content-Type",
               "Access-Control-Allow-Methods": "GET, POST, OPTIONS"}
    headers.update(extra_headers or {})
    return {"statusCode": status, "headers": headers, "body": body}, status


def _paginate(rows, offset: int = 0, limit: int = DEFAULT_PAGE_SIZE) -> Dict[str, Any]:
    """Slice rows and report what was dropped.

    Returns ``rows``/``total``/``offset``/``limit``/``truncated`` so a client
    can page deliberately rather than discovering a silent cut-off.
    """
    rows = list(rows or [])
    try:
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
    except (TypeError, ValueError):
        offset, limit = 0, DEFAULT_PAGE_SIZE
    page = rows[offset:offset + limit]
    return {
        "rows": page,
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "returned": len(page),
        "truncated": offset + len(page) < len(rows),
    }


def _allowed_origin() -> str:
    """Echo the request Origin only if it matches the deploy host.

    Falls back to same-origin (no CORS header) rather than ``*``. With a signed
    session cookie, a wildcard plus credentials is rejected by browsers anyway,
    and a wildcard without credentials lets any site drive the write actions.
    """
    return os.environ.get("RISK_ALLOWED_ORIGIN", "").strip()


def _cors_for(event: Dict[str, Any]) -> Dict[str, str]:
    """CORS headers for this request, only when the origin is explicitly allowed."""
    origin = ""
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == "origin":
            origin = value
            break
    allowed = _allowed_origin()
    if allowed and origin and origin.rstrip("/") == allowed.rstrip("/"):
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
                "Access-Control-Allow-Credentials": "true"}
    return {"Vary": "Origin"}


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
    cors = _cors_for(event)
    if event.get("httpMethod") == "OPTIONS":
        return _json({"ok": True}, extra_headers=cors)

    method = (event.get("httpMethod") or "GET").upper()
    if method not in ("GET", "POST"):
        return _json({"error": f"method {method} not allowed"}, 405, cors)

    params = _query(event)
    payload = _body(event) if method == "POST" else {}
    action = (payload.get("action") or (params.get("action") or [""])[0] or
              ("ask" if method == "POST" else "health")).lower()

    try:
        copilot = get_copilot()
    except Exception as exc:
        return _json({"ready": False, "error": str(exc),
                      "trace": traceback.format_exc()[-1500:]}, 503, cors)
    if copilot is None:
        return _json(health(), 503, cors)

    try:
        return _dispatch(copilot, action, payload, params, cors)
    except Exception as exc:
        return _json({"error": str(exc), "action": action,
                      "trace": traceback.format_exc()[-1500:]}, 500, cors)


def _dispatch(copilot, action: str, payload: Dict[str, Any], params,
              cors: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, Any], int]:
    cors = cors or {}
    one = lambda key, default=None: payload.get(key) or (params.get(key) or [default])[0]  # noqa: E731

    if action in ("health", ""):
        return _json(health(), 200, cors)

    if action == "login":
        role = str(one("role", "analyst") or "analyst").lower()
        user = str(one("user", "") or "")
        if role not in rbac.ROLES:
            return _json({"error": "unknown_role", "roles": list(rbac.ROLES)}, 400, cors)
        if not user:
            user = rbac.DEMO_USERS[role][0]
        token = rbac.issue_token(user, role)
        body, _ = rbac.auth_status({"user": user, "role": role}), None
        body["token"] = token
        return _json(body, 200, {**cors, "Set-Cookie": rbac.cookie_header(token)})

    if action == "logout":
        return _json({"authenticated": False, "ok": True}, 200,
                     {**cors, "Set-Cookie": rbac.clear_cookie_header()})

    if action == "whoami":
        claims, err = rbac.verify_token(
            payload.get("token") or (params.get("token") or [""])[0])
        return _json(rbac.auth_status(claims, err), 200, cors)

    # Single choke point for access control. It runs after login/logout/whoami
    # so those stay open, and before any state change so an unauthorised caller
    # cannot reach the work. An unknown action is refused here too - failing
    # closed, not open.
    claims, auth_err = rbac.verify_token(payload.get("token"))
    if not claims:
        claims, auth_err = rbac.verify_token(
            (params.get("token") or [""])[0])
    allowed, detail = rbac.authorize(action, claims)
    if not allowed:
        status = 400 if detail.get("error") == "unknown_action" else 401
        if detail.get("error") == "insufficient_role":
            status = 403
        if detail.get("error") == "unknown_action":
            # Naming the valid actions turns a 400 from a guessing game into a
            # self-documenting API.
            detail["available"] = sorted(rbac.ACTION_PERMISSION)
        return _json({**detail, "auth": rbac.auth_status(claims, auth_err)}, status, cors)
    actor = claims.get("user")

    if action == "status":
        return _json({**copilot.status(), "runtime": runtime_status(),
                      "auth": rbac.auth_status(claims)}, cors)

    if action == "examples":
        return _json({"questions": example_questions(copilot)}, cors)

    if action == "ask":
        question = one("question", "")
        if not question:
            return _json({"error": "question is required"}, 400, cors)
        answer = copilot.ask(str(question), mask_output=True)
        answer.pop("rows", None) if len(answer.get("rows") or []) > 30 else None
        return _json(answer, 200, cors)

    if action == "detect":
        cid = one("customer_id", "")
        if not cid:
            return _json({"error": "customer_id is required"}, 400, cors)
        case = copilot.analyze_customer(str(cid), persist=True)
        return _json(_slim_case(case), 200, cors)

    if action == "scan":
        limit = int(one("limit", 150) or 150)
        out = copilot.run_population(limit=limit, min_level=one("min_level", "medium"))
        copilot.audit.record("scan.request", copilot.run_id, "OK",
                             f"portfolio scan by {actor}", {"limit": limit})
        cases = out.pop("cases", [])
        ranked = sorted(cases, key=lambda c: -c["finding"]["composite_score"])
        summary = [{"customer_id": c["customer_id"],
                    "risk_level": c["finding"]["risk_level"],
                    "composite_score": c["finding"]["composite_score"],
                    "confidence": c["finding"]["confidence"],
                    "fired_rules": c["finding"]["fired_rules"],
                    "primary_reason": c["finding"]["primary_reason"],
                    "status": c["status"]} for c in ranked]
        page = _paginate(summary, int(one("offset", 0) or 0),
                         int(one("limit", 25) or 25))
        return _json({**out, "top": page["rows"], "total": page["total"],
                      "offset": page["offset"], "truncated": page["truncated"]}, 200, cors)

    if action in ("findings", "portfolio_risks"):
        offset = int(one("offset", 0) or 0)
        limit = int(one("limit", DEFAULT_PAGE_SIZE) or DEFAULT_PAGE_SIZE)
        rows = copilot.wh.query(
            "SELECT finding_id, customer_id, composite_risk_score, risk_level, confidence,"
            " guardrail_status, summary, created_at FROM findings"
            " WHERE risk_level IN ('high','medium')"
            " ORDER BY composite_risk_score DESC")
        page = _paginate(rows, offset, limit)
        return _json({"findings": page["rows"], "count": page["returned"],
                      "total": page["total"], "offset": page["offset"],
                      "truncated": page["truncated"],
                      "as_of_date": copilot.wh.as_of()}, cors)

    if action == "policy":
        query = one("query", one("q", ""))
        if not query:
            return _json({"error": "query is required"}, 400, cors)
        hits = copilot.retrieval.search(str(query), top_k=int(one("top_k", 20) or 20))
        page = _paginate(hits, int(one("offset", 0) or 0),
                         int(one("limit", DEFAULT_PAGE_SIZE) or DEFAULT_PAGE_SIZE))
        return _json({"clauses": page["rows"], "count": page["returned"],
                      "total": page["total"], "truncated": page["truncated"]}, cors)

    if action == "file":
        cid = one("customer_id", "")
        if not cid:
            return _json({"error": "customer_id is required"}, 400, cors)
        out = copilot.generate_filing(
            str(cid), str(one("filing_type", "SAR")).upper(), preparer=actor)
        if not out.get("success"):
            return _json(out, 400, cors)
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
            return _json({"error": "filing_id is required"}, 400, cors)
        out = copilot.approve_filing(str(filing_id), str(approver))
        return _json(out, 200 if out.get("success") else 400, cors)

    if action == "escalate":
        cid = one("customer_id", "")
        if not cid:
            return _json({"error": "customer_id is required"}, 400, cors)
        from backend.orchestration.multi_agent_workflow import MultiAgentWorkflow
        case = copilot.analyze_customer(str(cid), persist=True)
        wf = MultiAgentWorkflow(copilot.wh, copilot.cfg, run_id=copilot.run_id)
        return _json(wf.escalate(case), 200, cors)

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
        return _json(copilot.corpus_status(), 200, cors)

    if action == "evaluate":
        # Metrics computed on the *running* dataset (B4). The README previously
        # quoted numbers from a 600-customer profile while the app served 200.
        from scripts.evaluate import evaluate_and_report
        report = evaluate_and_report(copilot)
        report.pop("_written_to", None)
        return _json(report, 200, cors)

    if action == "mcp":
        from backend.orchestration.mcp_integrations import (
            MCP_SERVER_MANIFEST, ConnectorRegistry, handle_tool_call)
        tool = one("tool", "")
        if not tool:
            return _json({"server": MCP_SERVER_MANIFEST,
                          "connectors": ConnectorRegistry(copilot.cfg).status()})
        args = payload.get("args") or {}
        return _json(handle_tool_call(str(tool), args, copilot), 200, cors)

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
