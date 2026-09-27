"""Command line interface for the copilot.

This is the surface the CoCo CLI, the Streamlit app, the scheduled automation
and the Vercel API all call, so there is exactly one implementation of every
governed behaviour.

    python -m backend.cli status
    python -m backend.cli ask "which customers are structuring cash?"
    python -m backend.cli scan --limit 200
    python -m backend.cli file CUST-000123 --type SAR
    python -m backend.cli evaluate
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import load_config  # noqa: E402
from backend.orchestration.copilot import RiskCopilot  # noqa: E402


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _render_answer(answer: Dict[str, Any]) -> None:
    print("=" * 78)
    print(answer["answer_text"])
    print("=" * 78)
    for section in answer.get("sections", []):
        title = section.get("title", "")
        if section.get("type") == "table":
            rows = section.get("rows") or []
            cols = section.get("columns") or []
            if not rows:
                print(f"\n{title}\n  (no rows)")
                continue
            print(f"\n{title}")
            headers = [c[1] for c in cols]
            widths = [min(38, max(len(h), 12)) for h in headers]
            print("  " + " | ".join(h[:w].ljust(w) for h, w in zip(headers, widths)))
            for r in rows[:20]:
                print("  " + " | ".join(
                    str(r.get(c[0], ""))[:w].ljust(w) for c, w in zip(cols, widths)))
            if len(rows) > 20:
                print(f"  ... {len(rows) - 20} more row(s)")
        elif section.get("type") == "bullets":
            print(f"\n{title}")
            for item in section.get("items", []):
                print(f"  - {item}")
        elif section.get("type") == "kv":
            print(f"\n{title}")
            items = section.get("items", [])
            width = max((len(i["label"]) for i in items), default=10)
            for i in items:
                print(f"  {i['label']:<{width}}  {i['value']}")
        elif section.get("type") == "clause":
            print(f"\n{title}\n  {section.get('text', '')}")
    if answer.get("citations"):
        print("\nCited clauses")
        for c in answer["citations"]:
            print(f"  [{c['clause_ref']}] {c['citation']}")
    if answer.get("sql"):
        print("\nQueries executed")
        for s in answer["sql"]:
            print(f"  {s.strip()[:160]}")
    g = answer.get("guardrail", {})
    print(f"\nGuardrail: {g.get('status')} | confidence: {answer.get('confidence')} | "
          f"rows: {answer.get('row_count')} | "
          f"intent: {answer.get('intent', {}).get('intent')}")
    if g.get("required_actions"):
        print("Required actions: " + "; ".join(g["required_actions"]))
    if answer.get("limitations"):
        print("Limitations:")
        for lim in answer["limitations"][:5]:
            print(f"  - {lim}")
    if answer.get("next_actions"):
        print("Next actions:")
        for na in answer["next_actions"][:5]:
            print(f"  - {na}")


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="copilot",
                                 description="Risk, Fraud and Regulatory Intelligence Copilot")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show warehouse, corpus and configuration status")
    p_ask = sub.add_parser("ask", help="ask a natural-language question")
    p_ask.add_argument("question", nargs="+")
    p_ask.add_argument("--json", action="store_true")

    p_scan = sub.add_parser("scan", help="assess the population and persist findings")
    p_scan.add_argument("--limit", type=int, default=None)
    p_scan.add_argument("--min-level", default="medium", choices=["high", "medium", "low"])

    p_det = sub.add_parser("detect", help="run detectors for one customer")
    p_det.add_argument("customer_id")

    p_expl = sub.add_parser("explain", help="explain why a customer was flagged")
    p_expl.add_argument("customer_id")

    p_file = sub.add_parser("file", help="generate a draft regulatory filing")
    p_file.add_argument("customer_id")
    p_file.add_argument("--type", default="SAR", choices=["SAR", "CTR", "STR"])

    p_appr = sub.add_parser("approve", help="record human approval for a draft filing")
    p_appr.add_argument("filing_id")
    p_appr.add_argument("--approver", required=True)

    sub.add_parser("pack", help="generate the board MIAR pack")
    sub.add_parser("liquidity", help="generate the liquidity and large exposure pack")

    p_esc = sub.add_parser("escalate", help="escalate a case to the tracker and channel")
    p_esc.add_argument("customer_id")

    p_pol = sub.add_parser("policy", help="retrieve citable clauses")
    p_pol.add_argument("query", nargs="+")
    p_pol.add_argument("--framework", default=None)
    p_pol.add_argument("--top-k", type=int, default=5)

    p_aud = sub.add_parser("audit", help="show the audit trail")
    p_aud.add_argument("--run-id", default=None)
    p_aud.add_argument("--limit", type=int, default=50)

    p_mcp = sub.add_parser("mcp", help="list MCP tools and connector status")
    p_mcp.add_argument("action", nargs="?", default="list", choices=["list", "call"])
    p_mcp.add_argument("tool", nargs="?")
    p_mcp.add_argument("--args", default="{}")

    p_eval = sub.add_parser("evaluate", help="score detection against ground truth")
    p_eval.add_argument("--out", default=None)

    args = ap.parse_args(argv)
    copilot = RiskCopilot()

    try:
        if args.command == "status":
            _print(copilot.status())
            return 0

        if args.command == "ask":
            answer = copilot.ask(" ".join(args.question))
            _print(answer) if args.json else _render_answer(answer)
            return 0

        if args.command == "scan":
            out = copilot.run_population(limit=args.limit, min_level=args.min_level)
            print(f"run {out['run_id']}: assessed {out['assessed']} customers, "
                  f"{out['escalated']} at or above {args.min_level} risk")
            print("distribution:", json.dumps(out["risk_distribution"]))
            if out["blocked"]:
                print(f"blocked by guardrail: {len(out['blocked'])}")
            for case in sorted(out["cases"],
                                key=lambda c: -c["finding"]["composite_score"])[:10]:
                f = case["finding"]
                print(f"  {f['customer_id']}  {f['composite_score']:.3f}  "
                      f"{f['risk_level']:6s} conf={f['confidence']:.2f}  "
                      f"{','.join(f['fired_rules']) or '-'}")
            return 0

        if args.command == "detect":
            case = copilot.analyze_customer(args.customer_id, persist=True)
            _print({"customer_id": case["customer_id"], "status": case["status"],
                    "finding": {k: v for k, v in case["finding"].items()
                                if k != "signals"},
                    "rule_contributions": case["finding"]["rule_contributions"],
                    "triggered_obligations": case["triggered_obligations"],
                    "guardrail": {k: v for k, v in case["guardrail"].items()
                                  if k != "checks"},
                    "evidence": {"item_count": case["evidence"]["item_count"],
                                 "quality": case["evidence"]["evidence_quality"],
                                 "manifest_hash": case["evidence"]["manifest_hash"]},
                    "workflow_ms": case["duration_ms"]})
            return 0

        if args.command == "explain":
            answer = copilot.ask(f"why was {args.customer_id} flagged?")
            _render_answer(answer)
            return 0

        if args.command == "file":
            out = copilot.generate_filing(args.customer_id, args.type)
            if out.get("success"):
                print(f"{out['report']['report_type']} draft {out['filing_id']} written to "
                      f"{out['path']}")
                gov = out["report"]["governance"]
                print(f"  status: {gov['status']} (human approval required, clause FILE-2.1.1)")
                print(f"  citation coverage: {gov['citation_coverage']:.0%}")
                print(f"  content hash: {gov['content_hash']}")
                print(f"  evidence manifest: {gov.get('manifest_hash')}")
            else:
                print(f"not filed: {out.get('error')} - {out.get('detail', '')}")
                return 1
            return 0

        if args.command == "approve":
            out = copilot.approve_filing(args.filing_id, args.approver)
            print(json.dumps(out, indent=2))
            return 0 if out.get("success") else 1

        if args.command == "pack":
            out = copilot.build_board_pack()
            print(f"MIAR {out['report_id']} -> {out['path']}")
            return 0

        if args.command == "liquidity":
            out = copilot.build_liquidity_report()
            print(f"LIQUIDITY {out['report_id']} -> {out['path']}")
            return 0

        if args.command == "escalate":
            case = copilot.analyze_customer(args.customer_id, persist=True)
            from backend.orchestration.multi_agent_workflow import MultiAgentWorkflow
            wf = MultiAgentWorkflow(copilot.wh, copilot.cfg, run_id=copilot.run_id)
            out = wf.escalate(case)
            _print(out)
            return 0

        if args.command == "policy":
            hits = copilot.retrieval.search(" ".join(args.query), top_k=args.top_k,
                                            frameworks=[args.framework] if args.framework else None)
            for h in hits:
                print(f"[{h['clause_ref']}] {h['citation']}  (score {h['score']})")
                print(f"   {h['title_text']}")
                print(f"   {h['body']}\n")
            if not hits:
                print("no clause matched")
            return 0

        if args.command == "audit":
            events = copilot.audit.trace(args.run_id, args.limit) if args.run_id \
                else copilot.audit.recent(args.limit)
            for e in events:
                print(f"{e['ts']}  {e['action']:22s} {e['target'][:36]:36s} "
                      f"{e['decision']:8s} {e['rationale'][:90]}")
            return 0

        if args.command == "mcp":
            from backend.orchestration.mcp_integrations import (
                MCP_SERVER_MANIFEST, ConnectorRegistry, handle_tool_call)
            if args.action == "list":
                _print({"server": MCP_SERVER_MANIFEST,
                        "connectors": ConnectorRegistry(copilot.cfg).status()})
            else:
                _print(handle_tool_call(args.tool, json.loads(args.args), copilot))
            return 0

        if args.command == "evaluate":
            from scripts.evaluate import evaluate_and_report
            _print(evaluate_and_report(copilot, out_path=args.out))
            return 0

        ap.error(f"unknown command {args.command}")
        return 2
    finally:
        copilot.close()


if __name__ == "__main__":
    raise SystemExit(main())
