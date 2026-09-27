"""Cold-start bootstrap for the Vercel serverless runtime.

Vercel gives a function a read-only bundle and a writable ``/tmp``. So:

* the regulatory corpus ships in the bundle and is only read;
* the synthetic dataset is generated once per warm instance into ``/tmp`` and
  reused for every subsequent request;
* the warehouse is built from that dataset into ``/tmp`` and cached at module
  scope, so a warm invocation pays no build cost.

The demo dataset is deliberately smaller than the local one (200 customers over
150 days) to keep cold start inside the function timeout. Set
``RISK_PROFILE=full`` to generate the full 600-customer dataset instead.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Must be set before backend.config is imported so the artefact paths resolve
# into the writable tmp mount.
os.environ.setdefault(
    "RISK_ARTIFACT_DIR", os.environ.get("VERCEL_TMP", "/tmp") + "/risk-copilot")
os.environ.setdefault("RISK_DATA_DIR", os.environ["RISK_ARTIFACT_DIR"] + "/data")

_lock = threading.Lock()
_state: Dict[str, Any] = {"copilot": None, "error": None, "build_seconds": None,
                          "ready": False, "attempts": 0}


def _dataset_profile() -> Dict[str, Any]:
    full = os.environ.get("RISK_PROFILE", "demo").lower() == "full"
    return {
        "customers": int(os.environ.get("RISK_CUSTOMERS", 600 if full else 200)),
        "horizon_days": int(os.environ.get("RISK_HORIZON_DAYS", 400 if full else 150)),
        "seed": int(os.environ.get("RISK_SEED", 42)),
        "as_of": os.environ.get("RISK_AS_OF") or None,
    }


def _ensure_dataset(raw_dir: str, gold_dir: str) -> None:
    from scripts import generate_synthetic_data as gen

    profile = _dataset_profile()
    needed = ["customers.csv", "accounts.csv", "transactions.csv"]
    if all(os.path.exists(os.path.join(raw_dir, f)) for f in needed):
        return
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(gold_dir, exist_ok=True)
    gen.main([
        "--customers", str(profile["customers"]),
        "--horizon-days", str(profile["horizon_days"]),
        "--seed", str(profile["seed"]),
        "--out", raw_dir,
        "--gold-dir", gold_dir,
    ] + (["--as-of", profile["as_of"]] if profile["as_of"] else []))


def _build_copilot():
    from backend.config import load_config
    from backend.orchestration.copilot import RiskCopilot
    from backend.warehouse import Warehouse
    from backend.agents.retrieval import RetrievalService

    cfg = load_config()
    raw_dir = os.path.join(os.environ["RISK_DATA_DIR"], "raw")
    gold_dir = os.path.join(os.environ["RISK_DATA_DIR"], "gold")

    _ensure_dataset(raw_dir, gold_dir)

    wh = Warehouse(config=cfg)
    wh.load_raw_dir(raw_dir)
    RetrievalService(wh, config=cfg).load()
    wh.load_sql_file(os.path.join(ROOT, "sql", "04_semantic_views.sql"))
    wh.execute("DELETE FROM mule_clusters")
    wh.execute("INSERT INTO mule_clusters SELECT * FROM sem_mule_network_clusters")

    copilot = RiskCopilot(warehouse=wh, config=cfg)
    copilot.audit.record("session.start", copilot.run_id, "OK",
                         "Vercel serverless cold start", {"runtime": "vercel"})
    return copilot


def get_copilot():
    """Return a ready copilot, building it at most once per warm instance."""
    if _state["copilot"] is not None:
        return _state["copilot"]
    with _lock:
        if _state["copilot"] is not None:
            return _state["copilot"]
        _state["attempts"] += 1
        t0 = time.time()
        try:
            copilot = _build_copilot()
            # Warm the detector on one customer so the first real request does
            # not pay for SQLite page-cache population.
            try:
                first = copilot.wh.customer_ids(limit=1)
                if first:
                    copilot.detector.detect_customer(first[0])
            except Exception:
                pass
            _state["copilot"] = copilot
            _state["build_seconds"] = round(time.time() - t0, 2)
            _state["ready"] = True
        except Exception as exc:  # surfaced to the client instead of a 500 blob
            _state["error"] = f"{type(exc).__name__}: {exc}"
    return _state["copilot"]


def health() -> Dict[str, Any]:
    copilot = get_copilot()
    if copilot is None:
        return {"ready": False, "error": _state["error"],
                "build_attempts": _state["attempts"]}
    return {
        "ready": True,
        "build_seconds": _state["build_seconds"],
        "profile": _dataset_profile(),
        "status": copilot.status(),
    }
