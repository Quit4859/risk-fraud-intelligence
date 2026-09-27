#!/usr/bin/env python3
"""Detection evaluation against planted ground truth.

Answers the question a judge will ask first: *does it actually work, and how do
you know?* The generator plants known fraud scenarios and writes
``data/gold/ground_truth_labels.csv``; this harness scores the detector portfolio
against those labels and reports precision, recall, F1, false-positive rate,
per-typology recall and latency.

    python scripts/evaluate.py
    python -m backend.cli evaluate
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from backend.config import ensure_dir, load_config  # noqa: E402

# Which detector is *expected* to catch which planted typology. Reporting the
# miss is more useful than hiding it: it is the accuracy backlog.
EXPECTED_RULE = {
    "structuring": "STRUCTURING",
    "account_takeover": "ACCOUNT_TAKEOVER",
    "geographic": "GEOGRAPHIC",
    "mule_network": "MULE_NETWORK",
    "trade_based": "TRADE_BASED",
    "benign_control": None,
}


def load_labels(cfg) -> Dict[str, Dict[str, Any]]:
    path = os.path.join(ROOT, cfg["paths"]["gold_labels"])
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return {r["customer_id"]: r for r in csv.DictReader(fh)}


def _prf(tp: int, fp: int, fn: int) -> Dict[str, float]:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 4), "recall": round(recall, 4),
            "f1": round(f1, 4)}


def evaluate_and_report(copilot, out_path: str | None = None) -> Dict[str, Any]:
    cfg = copilot.cfg
    labels = load_labels(cfg)
    detector = copilot.detector
    bands = cfg["detection"]["risk_bands"]

    predictions: Dict[str, Dict[str, Any]] = {}
    t0 = time.time()
    for cid, label in labels.items():
        f = detector.detect_customer(cid)
        predictions[cid] = {
            "composite_score": f["composite_score"],
            "risk_level": f["risk_level"],
            "confidence": f["confidence"],
            "fired_rules": f["fired_rules"],
            "primary_reason": f["primary_reason"],
            "risk_level_source": f.get("risk_level_source"),
            "evidence_count": f["evidence_count"],
        }
    elapsed = time.time() - t0
    n = max(1, len(labels))

    def evaluate_level(level: str) -> Dict[str, Any]:
        flagged = {c for c, p in predictions.items()
                   if p["risk_level"] in (level, "high", "medium")
                   and {"high": 2, "medium": 1}[p["risk_level"]] >= {"high": 2, "medium": 1}[level]}
        actual = {c for c, l in labels.items() if int(l["is_fraud"]) == 1}
        tp = len(flagged & actual)
        fp = len(flagged - actual)
        fn = len(actual - flagged)
        tn = len(labels) - tp - fp - fn
        out = _prf(tp, fp, fn)
        out["tn"] = tn
        out["false_positive_rate"] = round(fp / (fp + tn), 4) if (fp + tn) else 0.0
        out["flagged"] = len(flagged)
        return out

    at_high = evaluate_level("high")
    at_medium_plus = evaluate_level("medium")

    # Per-typology recall and rule attribution.
    by_scenario: Dict[str, Dict[str, Any]] = {}
    for cid, label in labels.items():
        scenario = label["scenario"]
        entry = by_scenario.setdefault(scenario, {
            "planted": 0, "detected_high": 0, "detected_medium_plus": 0,
            "correct_rule_fired": 0, "wrong_or_no_rule": 0, "misses": [],
        })
        if int(label["is_fraud"]) != 1:
            continue
        entry["planted"] += 1
        p = predictions[cid]
        fired = set(p["fired_rules"])
        if p["risk_level"] == "high":
            entry["detected_high"] += 1
        if p["risk_level"] in ("high", "medium"):
            entry["detected_medium_plus"] += 1
        expected = EXPECTED_RULE.get(scenario)
        if expected and expected in fired:
            entry["correct_rule_fired"] += 1
        else:
            entry["wrong_or_no_rule"] += 1
            entry["misses"].append({"customer_id": cid, "fired": sorted(fired),
                                     "score": p["composite_score"]})
    for entry in by_scenario.values():
        if entry["planted"]:
            entry["recall_high"] = round(entry["detected_high"] / entry["planted"], 4)
            entry["recall_medium_plus"] = round(
                entry["detected_medium_plus"] / entry["planted"], 4)
            entry["rule_attribution"] = round(
                entry["correct_rule_fired"] / entry["planted"], 4)
            entry["misses"] = entry["misses"][:10]

    # False positives: what does the benign control group trip on?
    fp_cases = []
    for cid, label in labels.items():
        if int(label["is_fraud"]) == 1:
            continue
        p = predictions[cid]
        if p["risk_level"] in ("high", "medium"):
            fp_cases.append({"customer_id": cid, "risk_level": p["risk_level"],
                             "score": p["composite_score"],
                             "fired_rules": p["fired_rules"],
                             "primary_reason": p["primary_reason"][:200]})
    fp_cases.sort(key=lambda r: -r["score"])

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": copilot.run_id,
        "dataset_fingerprint": copilot.wh.source_fingerprint(),
        "as_of_date": copilot.wh.as_of(),
        "population": n,
        "planted_fraud_cases": sum(1 for l in labels.values() if int(l["is_fraud"]) == 1),
        "control_cases": sum(1 for l in labels.values() if int(l["is_fraud"]) == 0),
        "thresholds": bands,
        "detect_high_only": at_high,
        "detect_medium_or_high": at_medium_plus,
        "by_scenario": by_scenario,
        "false_positive_sample": fp_cases[:15],
        "false_positive_count": len(fp_cases),
        "performance": {
            "customers_scored": n,
            "wall_clock_seconds": round(elapsed, 2),
            "ms_per_customer": round(elapsed / n * 1000, 2),
        },
        "detection_thresholds_configured": {
            "structuring_ctr_amount": cfg["detection"]["structuring"]["ctr_threshold"],
            "structuring_window_days": cfg["detection"]["structuring"]["lookback_days"],
            "velocity_zscore": cfg["detection"]["velocity"]["zscore_threshold"],
            "geographic_high_risk_share": cfg["detection"]["geographic"]["high_risk_share_threshold"],
            "impossible_travel_hours": cfg["detection"]["geographic"]["impossible_travel_hours"],
        },
    }

    out = out_path or os.path.join(ROOT, "artifacts", "evaluation_report.json")
    ensure_dir(os.path.dirname(out))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    md = out.replace(".json", ".md")
    with open(md, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report))
    report["_written_to"] = [out, md]
    return report


def render_markdown(r: Dict[str, Any]) -> str:
    hi = r["detect_high_only"]
    mp = r["detect_medium_or_high"]
    lines = [
        "# Detection evaluation",
        "",
        f"- Generated: {r['generated_at']}  ",
        f"- Dataset fingerprint: `{r['dataset_fingerprint']}` (as of {r['as_of_date']})  ",
        f"- Population: {r['population']:,} customers "
        f"({r['planted_fraud_cases']} planted fraud, {r['control_cases']} benign control)",
        f"- Throughput: {r['performance']['ms_per_customer']} ms/customer",
        "",
        "## Aggregate",
        "",
        "| Operating point | TP | FP | FN | TN | Precision | Recall | F1 | FPR |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| High only | {hi['tp']} | {hi['fp']} | {hi['fn']} | {hi['tn']} | "
        f"{hi['precision']:.3f} | {hi['recall']:.3f} | {hi['f1']:.3f} | {hi['false_positive_rate']:.3f} |",
        f"| Medium or high | {mp['tp']} | {mp['fp']} | {mp['fn']} | {mp['tn']} | "
        f"{mp['precision']:.3f} | {mp['recall']:.3f} | {mp['f1']:.3f} | {mp['false_positive_rate']:.3f} |",
        "",
        "## Recall by planted typology",
        "",
        "| Typology | Planted | High | Rec@high | Med+ | Rec@med+ | Correct rule fired |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for scenario, e in sorted(r["by_scenario"].items()):
        if not e["planted"]:
            continue
        lines.append(
            f"| {scenario} | {e['planted']} | {e['detected_high']} | {e['recall_high']:.3f} | "
            f"{e['detected_medium_plus']} | {e['recall_medium_plus']:.3f} | "
            f"{e['rule_attribution']:.3f} |")
    lines += [
        "",
        f"## False positives ({r['false_positive_count']} benign customer(s) flagged)",
        "",
        "| Customer | Level | Score | Rules | Driver |",
        "|---|---|---:|---|---|",
    ]
    for f in r["false_positive_sample"][:10]:
        lines.append(f"| {f['customer_id']} | {f['risk_level']} | {f['score']:.3f} | "
                     f"{', '.join(f['fired_rules']) or '-'} | {f['primary_reason'][:110]} |")
    lines += ["", "## Known misses", ""]
    any_miss = False
    for scenario, e in sorted(r["by_scenario"].items()):
        if e["misses"]:
            any_miss = True
            lines.append(f"- **{scenario}**: {len(e['misses'])} case(s) where the expected rule "
                         f"did not fire, e.g. "
                         + "; ".join(f"{m['customer_id']} (score {m['score']:.3f}, "
                                     f"{','.join(m['fired']) or 'no rule'})"
                                     for m in e["misses"][:3]))
    if not any_miss:
        lines.append("- None.")
    lines += ["", "## Configured thresholds used for this run", ""]
    for k, v in r["detection_thresholds_configured"].items():
        lines.append(f"- `{k}`: {v}")
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    from backend.orchestration.copilot import RiskCopilot
    copilot = RiskCopilot()
    try:
        report = evaluate_and_report(copilot)
    finally:
        copilot.close()
    hi, mp = report["detect_high_only"], report["detect_medium_or_high"]
    print(f"population            {report['population']}")
    print(f"planted / control     {report['planted_fraud_cases']} / {report['control_cases']}")
    print(f"high-only   P={hi['precision']:.3f} R={hi['recall']:.3f} F1={hi['f1']:.3f} "
          f"FPR={hi['false_positive_rate']:.3f}")
    print(f"medium+     P={mp['precision']:.3f} R={mp['recall']:.3f} F1={mp['f1']:.3f} "
          f"FPR={mp['false_positive_rate']:.3f}")
    print(f"throughput  {report['performance']['ms_per_customer']} ms/customer")
    print("recall by typology:")
    for s, e in sorted(report["by_scenario"].items()):
        if e["planted"]:
            print(f"  {s:20s} {e['recall_medium_plus']:.2f} med+  "
                  f"{e['rule_attribution']:.2f} correct-rule")
    print(f"report -> {report.get('_written_to')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
