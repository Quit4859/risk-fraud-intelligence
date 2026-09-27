#!/usr/bin/env python3
"""Build the local warehouse: ingest -> parse documents -> create views.

Mirrors the Snowflake pipeline in sql/02_dynamic_tables.sql and
sql/04_semantic_views.sql. Idempotent, so it is safe to re-run.

    python scripts/run_pipeline.py
    python scripts/run_pipeline.py --regenerate
"""

from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from backend.config import load_config  # noqa: E402
from backend.agents.retrieval import RetrievalService  # noqa: E402
from backend.warehouse import Warehouse  # noqa: E402
import scripts.generate_corpus as generate_corpus  # noqa: E402
import scripts.generate_synthetic_data as generate_data  # noqa: E402

VIEWS_SQL = os.path.join(ROOT, "sql", "04_semantic_views.sql")


def ensure_corpus(cfg) -> None:
    corpus_dir = os.path.join(ROOT, cfg["paths"]["corpus_dir"])
    if not os.path.isdir(corpus_dir) or not any(f.endswith(".md") for f in os.listdir(corpus_dir)):
        generate_corpus.main([])


def ensure_data(cfg, regenerate: bool = False) -> None:
    raw_dir = os.path.join(ROOT, cfg["paths"]["raw_dir"])
    needed = ["customers.csv", "accounts.csv", "transactions.csv"]
    if regenerate or not all(os.path.exists(os.path.join(raw_dir, f)) for f in needed):
        generate_data.main([])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the local risk warehouse")
    ap.add_argument("--regenerate", action="store_true", help="regenerate synthetic data first")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    cfg = load_config()
    t0 = time.time()

    print("[pipeline] 1/4 ensuring regulatory corpus")
    ensure_corpus(cfg)
    print("[pipeline] 2/4 ensuring synthetic data")
    ensure_data(cfg, regenerate=args.regenerate)

    print("[pipeline] 3/4 ingesting into warehouse")
    wh = Warehouse(db_path=args.db, config=cfg)
    counts = wh.load_raw_dir()
    for table, n in sorted(counts.items()):
        print(f"           {table:24s} {n:>9,}")
    print(f"           as_of_date              {wh.as_of()}")
    print(f"           data_fingerprint        {wh.source_fingerprint()}")

    print("[pipeline] 4/4 parsing policy corpus and creating semantic views")
    ret = RetrievalService(wh, config=cfg).load()
    if ret.last_error:
        print(f"           WARNING corpus parse: {ret.last_error}")
    print(f"           documents                {len(ret.docs)}")
    print(f"           clause chunks            {ret.clause_count}")
    print(f"           policy bindings          {wh.table_count('policies')}")

    wh.load_sql_file(VIEWS_SQL)
    view_names = ["sem_customer_profile", "sem_daily_activity", "sem_cash_72h_aggregation",
                  "sem_elevated_risk_exposure", "sem_mule_network_clusters",
                  "sem_credit_watchlist", "sem_liquidity_position", "sem_large_exposures",
                  "sem_filing_obligation_queue", "sem_alert_backlog"]
    for v in view_names:
        try:
            wh.execute(f"SELECT COUNT(*) FROM {v}")
        except Exception as exc:
            print(f"           WARNING view {v}: {exc}")
    # Materialise the one view that is re-scanned per customer during detection.
    # Without this the mule-network detector regroups 300k rows on every call.
    wh.execute("DELETE FROM mule_clusters")
    wh.execute("INSERT INTO mule_clusters SELECT * FROM sem_mule_network_clusters")
    print(f"           semantic views          {len(view_names)}")
    print(f"           materialised clusters   {wh.table_count('mule_clusters')}")
    print(f"[pipeline] ready in {time.time() - t0:.1f}s -> {wh.db_path}")
    wh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
