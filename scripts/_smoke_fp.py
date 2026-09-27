#!/usr/bin/env python3
"""Ad-hoc: break down which rule drives benign-customer false positives."""
import csv
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.orchestration.copilot import RiskCopilot  # noqa: E402

copilot = RiskCopilot()
labels = {r["customer_id"]: r for r in
          csv.DictReader(open("data/gold/ground_truth_labels.csv"))}
only = sys.argv[1] if len(sys.argv) > 1 else None
c = Counter()
detail = []
for cid, lab in labels.items():
    if lab["scenario"] == "benign_control" and (not only or lab["customer_id"] == only):
        f = copilot.detector.detect_customer(cid)
        if f["risk_level"] in ("high", "medium"):
            c[tuple(sorted(f["fired_rules"]))] += 1
            if len(detail) < 8:
                detail.append((cid, f["risk_level"], round(f["composite_score"], 3),
                               f["fired_rules"],
                               {k: v["score"] for k, v in f["signals"].items() if v["score"] > 0.3},
                               f["primary_reason"][:150]))
print("benign flagged:", sum(c.values()), "of", sum(1 for l in labels.values()
                                                     if l["scenario"] == "benign_control"))
for k, v in c.most_common(10):
    print(f"  {v:4d}  {k}")
print()
for d in detail:
    print(d)
copilot.close()
