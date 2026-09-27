#!/usr/bin/env python3
"""Ad-hoc smoke check of the semantic views. Not part of the deliverable."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.warehouse import Warehouse  # noqa: E402

VIEWS = ["sem_dataset_clock", "sem_customer_profile", "sem_daily_activity",
         "sem_cash_72h_aggregation", "sem_elevated_risk_exposure",
         "sem_mule_network_clusters", "sem_credit_watchlist", "sem_liquidity_position",
         "sem_large_exposures", "sem_filing_obligation_queue", "sem_alert_backlog"]

wh = Warehouse()
for v in VIEWS:
    print(f"{v:32s} {wh.scalar('SELECT COUNT(*) FROM ' + v)}")
print("clock:", wh.query("SELECT * FROM sem_dataset_clock"))
print("72h:", wh.query("SELECT customer_id, deposit_count, aggregate_amount "
                       "FROM sem_cash_72h_aggregation WHERE rn=1 ORDER BY aggregate_amount DESC LIMIT 3"))
print("mule:", wh.query("SELECT * FROM sem_mule_network_clusters "
                        "ORDER BY customers_on_device DESC LIMIT 2"))
print("geo:", wh.query("SELECT * FROM sem_elevated_risk_exposure "
                       "ORDER BY risk_share DESC LIMIT 3"))
wh.close()
