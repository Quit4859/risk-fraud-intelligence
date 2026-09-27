#!/usr/bin/env python3
"""Ad-hoc: inspect detector output for labelled fraud cases. Debug helper."""
import csv
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.orchestration.copilot import RiskCopilot  # noqa: E402

copilot = RiskCopilot()
labels = {r["customer_id"]: r for r in
          csv.DictReader(open("data/gold/ground_truth_labels.csv"))}
want = sys.argv[1] if len(sys.argv) > 1 else "mule_network"
shown = 0
for cid, lab in labels.items():
    if lab["scenario"] != want:
        continue
    f = copilot.detector.detect_customer(cid)
    shown += 1
    print(f"--- {cid} [{lab['scenario']}] score={f['composite_score']} "
          f"level={f['risk_level']} src={f['risk_level_source']} fired={f['fired_rules']}")
    for code, sig in f["signals"].items():
        if sig.get("error"):
            print(f"    {code} ERROR: {sig['error']}")
        elif sig.get("score", 0) > 0.05:
            print(f"    {code} score={sig['score']} fired={sig['fired']} "
                  f":: {sig['reasons'][0][:130] if sig['reasons'] else ''}")
    if shown >= 3:
        break
copilot.close()
