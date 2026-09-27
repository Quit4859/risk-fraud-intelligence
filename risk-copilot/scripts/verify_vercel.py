#!/usr/bin/env python3
"""Local verification of the Vercel serverless handler.

Exercises the same code path Vercel runs, including a cold start into a clean
tmp directory, so deployment failures surface here rather than in production.

    python scripts/verify_vercel.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def call(event):
    from api.index import handler
    response, status = handler(event)
    body = json.loads(response["body"])
    return status, body


def post(action, **payload):
    return call({"httpMethod": "POST", "rawQueryString": "",
                 "body": json.dumps({"action": action, **payload})})


def main() -> int:
    # Force a genuine cold start in a writable tmp location.
    tmp = tempfile.mkdtemp(prefix="risk-copilot-verify-")
    os.environ["VERCEL_TMP"] = tmp
    os.environ.pop("RISK_PROFILE", None)

    failures = []

    def check(name, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {name}{(' - ' + str(detail)) if detail and not cond else ''}")
        if not cond:
            failures.append(name)

    print("[1] cold start (clean tmp, generates + builds the warehouse)")
    t0 = time.time()
    status, health = call({"httpMethod": "GET", "rawQueryString": "action=health"})
    build = time.time() - t0
    check("health returns 200", status == 200, status)
    check("copilot is ready", health.get("ready"), health.get("error"))
    check("cold start under 30s", build < 30, f"{build:.1f}s")
    print(f"       cold start {build:.1f}s, fingerprint "
          f"{health.get('status', {}).get('data_fingerprint')}")

    print("[2] static metadata")
    for action, key in (("status", "as_of_date"), ("corpus", "clauses"),
                        ("examples", "questions")):
        status, body = call({"httpMethod": "GET", "rawQueryString": f"action={action}"})
        check(f"GET {action}", status == 200 and body.get(key), status)

    print("[3] natural language ask")
    status, answer = post("ask", question="which customers are structuring cash "
                                         "to avoid the reporting threshold?")
    check("ask returns 200", status == 200, status)
    check("answer text present", bool(answer.get("answer_text")))
    check("citations present", len(answer.get("citations") or []) > 0,
          answer.get("citations"))
    check("sql provenance present", len(answer.get("sql") or []) > 0)
    check("guardrail verdict present", answer.get("guardrail", {}).get("status") in
          ("PASS", "WARN", "FAIL"), answer.get("guardrail"))
    check("data fingerprint stamped", bool(answer.get("provenance", {})
                                           .get("data_fingerprint")))
    print(f"       intent={answer.get('intent', {}).get('intent')} "
          f"confidence={answer.get('confidence')} "
          f"rows={answer.get('row_count')} "
          f"citations={len(answer.get('citations') or [])}")

    print("[4] customer detection")
    cid = health.get("status", {}).get("tables", {}).get("customers") and \
        (health.get("status", {}).get("top_customer") or "CUST-000001")
    status, case = post("detect", customer_id=cid)
    check("detect returns 200", status == 200, status)
    check("finding has score", case.get("finding", {}).get("composite_score") is not None)
    check("evidence manifest", bool(case.get("evidence", {}).get("manifest_hash")))
    check("guardrail verdict", case.get("guardrail", {}).get("status") in
          ("PASS", "WARN", "FAIL"))

    print("[5] portfolio scan (writes findings)")
    status, scan = post("scan", limit=60)
    check("scan returns 200", status == 200, status)
    check("scan assessed customers", scan.get("assessed", 0) > 0)
    check("scan returns a ranked top list", len(scan.get("top") or []) > 0)
    print(f"       assessed={scan.get('assessed')} escalated={scan.get('escalated')} "
          f"distribution={scan.get('risk_distribution')}")

    status, findings = call({"httpMethod": "GET", "rawQueryString": "action=findings&limit=5"})
    check("findings persisted", len(findings.get("findings") or []) > 0)

    print("[6] policy retrieval")
    status, pol = call({"httpMethod": "GET",
                        "rawQueryString": "action=policy&q=structuring+threshold"})
    check("policy returns clauses", len(pol.get("clauses") or []) > 0, pol)

    print("[7] board and liquidity packs")
    for action in ("board_pack", "liquidity"):
        status, rep = post(action)
        check(f"{action} returns 200", status == 200, status)
        gov = rep.get("report", {}).get("governance", {})
        check(f"{action} is PENDING_REVIEW", gov.get("status") == "PENDING_REVIEW", gov)
        check(f"{action} cites clauses", len(rep.get("report", {})
                                              .get("regulatory_basis") or []) > 0)

    print("[8] four-eyes control (a draft must not be submittable unapproved)")
    status, findings = call({"httpMethod": "GET", "rawQueryString": "action=findings&limit=50"})
    ranked = [f for f in (findings.get("findings") or [])
              if f.get("risk_level") == "high"]
    target = ranked[0]["customer_id"] if ranked else \
        ((scan.get("top") or [{}])[0].get("customer_id"))
    print(f"       drafting SAR for {target} "
          f"(top high-risk score "
          f"{(ranked[0]['composite_risk_score'] if ranked else 0):.3f})")
    if target:
        status, filing = post("file", customer_id=target, filing_type="SAR")
        if status == 200 and filing.get("filing_id"):
            check("draft is PENDING_REVIEW",
                  filing["governance"]["status"] == "PENDING_REVIEW")
            check("four-eyes required", filing["governance"]["four_eyes_required"] is True)
            check("citation coverage complete",
                  filing["governance"]["citation_coverage"] >= 1.0,
                  filing["governance"]["citation_coverage"])
            status, bad = post("approve", filing_id=filing["filing_id"], approver="bot")
            check("machine approver rejected", status == 400, bad)
            status, good = post("approve", filing_id=filing["filing_id"],
                               approver="a.compliance.officer@bank.example")
            check("named human approver accepted", good.get("success") is True, good)
        else:
            check("SAR draft produced", False, filing)

    print("[9] audit trail")
    status, audit = call({"httpMethod": "GET", "rawQueryString": "action=audit&limit=20"})
    check("audit returns events", len(audit.get("events") or []) > 0)
    actions = {e["action"] for e in audit.get("events") or []}
    check("audit records detections", "detect.customer" in actions or
          "detect.population" in actions, sorted(actions))

    print("[10] error handling")
    status, body = call({"httpMethod": "GET", "rawQueryString": "action=nope"})
    check("unknown action -> 400 with a menu", status == 400 and "available" in body)
    status, body = call({"httpMethod": "PUT", "rawQueryString": ""})
    check("bad method -> 405", status == 405, status)
    status, body = call({"httpMethod": "POST", "rawQueryString": "",
                         "body": json.dumps({"action": "ask"})})
    check("missing question -> 400", status == 400, status)
    status, body = call({"httpMethod": "POST", "rawQueryString": "",
                         "body": "{not json"})
    check("malformed body handled", status in (200, 400), status)

    print("[11] warm start is fast")
    t0 = time.time()
    post("ask", question="show the top 5 highest risk customers")
    warm = time.time() - t0
    check("warm ask under 5s", warm < 5, f"{warm:.2f}s")
    print(f"       warm ask {warm:.2f}s")

    shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"VERIFICATION FAILED: {len(failures)} check(s) - {failures}")
        return 1
    print("All Vercel runtime checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
