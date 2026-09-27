#!/usr/bin/env python3
"""Ad-hoc detector smoke check. Not part of the deliverable."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.agents.fraud_detector import FraudDetectorAgent  # noqa: E402
from backend.warehouse import Warehouse  # noqa: E402

wh = Warehouse()
det = FraudDetectorAgent(wh)

lab = {r["customer_id"]: r for r in wh.query(
    "SELECT * FROM read_csv_auto('data/gold/ground_truth_labels.csv')"
) } if False else None

ids = wh.customer_ids(limit=8)
t0 = time.time()
for cid in ids:
    r = det.detect_customer(cid)
    print(f"{cid} score={r['composite_score']:.3f} {r['risk_level']:6s} "
          f"conf={r['confidence']:.2f} fired={r['fired_rules']} ev={r['evidence_count']}")
    print(f"   {r['primary_reason'][:150]}")
print(f"{len(ids)} customers in {time.time()-t0:.1f}s "
      f"({(time.time()-t0)/len(ids)*1000:.0f} ms/customer)")
wh.close()
