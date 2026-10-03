"""Cold-start bootstrap and warm-up lifecycle.

The deployed app previously only built the copilot on the first API request.
That made the very first question pay the full build cost, and left the
findings table empty, which made the headline portfolio questions abstain
(B1). This module owns two things:

* **boot scan** - a background thread that runs a portfolio scan as soon as the
  copilot exists, so a warm instance already has findings to answer from;
* **warm-up state** - :func:`healthz` answers *instantly* and never triggers a
  build, so a Render health check (and a user watching the UI) get an immediate
  "warming up" signal rather than a hung request.

Degradation is explicit rather than silent: :func:`runtime_status` reports which
engine is live, and the UI renders "Fallback mode: local engine" when the
Snowflake engine is unavailable.
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

from backend import snowflake_config as sf_config  # noqa: E402

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "copilot": None, "error": None, "build_seconds": None,
    "ready": False, "attempts": 0, "engine": "local-fallback",
    "engine_detail": {}, "scan_state": "pending", "scan_seconds": None,
    "started_at": time.time(),
}

#: Where the deployed artefacts live. Set before backend.config is imported so
#: the paths resolve into a writable mount.
os.environ.setdefault(
    "RISK_ARTIFACT_DIR",
    os.environ.get("VERCEL_TMP", os.environ.get("TMPDIR", "/tmp")) + "/risk-copilot")
os.environ.setdefault("RISK_DATA_DIR", os.environ["RISK_ARTIFACT_DIR"] + "/data")


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
        "--fraud-prevalence", str(_fraud_prevalence()),
    ] + (["--as-of", profile["as_of"]] if profile["as_of"] else []))


def _fraud_prevalence() -> float:
    try:
        return float(os.environ.get("RISK_FRAUD_PREVALENCE", "0.07"))
    except ValueError:
        return 0.07


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
                         "Runtime cold start", {"runtime": "wsgi"})
    return copilot


def start_boot_scan(copilot) -> threading.Thread:
    """Run a portfolio scan on a background thread.

    Keeps the first question fast. The executor also scans lazily if findings
    are still empty, so correctness never depends on this thread having
    finished - it is an optimisation, not a gate.
    """
    def _run():
        _state["scan_state"] = "running"
        t0 = time.time()
        try:
            # Shares the copilot's lock with the lazy path, so a question
            # arriving mid-boot does not trigger a second concurrent scan.
            copilot.ensure_populated(
                limit=int(copilot.cfg.get("copilot", {}).get("boot_scan_limit", 200)))
            rows = copilot.wh.query(
                "SELECT risk_level, COUNT(*) n FROM findings GROUP BY risk_level")
            _state["scan_state"] = "done"
            _state["scan_seconds"] = round(time.time() - t0, 2)
            _state["scan_counts"] = {r["risk_level"]: r["n"] for r in rows}
        except Exception as exc:
            # Never fatal: the lazy path in the executor covers this case.
            _state["scan_state"] = "failed"
            _state["scan_error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=_run, name="boot-scan", daemon=True)
    thread.start()
    return thread


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
            _state["copilot"] = copilot
            _state["build_seconds"] = round(time.time() - t0, 2)
            _state["ready"] = True
            _state["error"] = None
            # Warm the detector on one customer so the first real request does
            # not pay for page-cache population.
            try:
                first = copilot.wh.customer_ids(limit=1)
                if first:
                    copilot.detector.detect_customer(first[0])
            except Exception:
                pass
            if copilot.cfg.get("copilot", {}).get("boot_scan_enabled", True):
                start_boot_scan(copilot)
        except Exception as exc:  # surfaced to the client instead of a 500 blob
            _state["error"] = f"{type(exc).__name__}: {exc}"
    return _state["copilot"]


def healthz() -> Dict[str, Any]:
    """Instant, build-free liveness. Used by the Render health check.

    Deliberately does *not* call get_copilot(): a health check must answer even
    while the engine is still building, or the platform will kill the instance.
    """
    ready = bool(_state["ready"] and _state["copilot"] is not None)
    return {
        "status": "ok" if ready else "warming_up",
        "ready": ready,
        "engine": _state["engine"],
        "scan_state": _state["scan_state"],
        "uptime_seconds": round(time.time() - _state["started_at"], 1),
        "build_seconds": _state["build_seconds"],
    }


def runtime_status() -> Dict[str, Any]:
    """Engine selection and warm-up detail, safe to render in the UI."""
    return {
        "engine": _state["engine"],
        "engine_detail": _state["engine_detail"],
        "snowflake": sf_config.auth_summary(),
        "scan_state": _state["scan_state"],
        "scan_seconds": _state["scan_seconds"],
        "scan_counts": _state.get("scan_counts", {}),
        "warmup_seconds": round(time.time() - _state["started_at"], 1),
    }


def health() -> Dict[str, Any]:
    copilot = get_copilot()
    if copilot is None:
        return {"ready": False, "error": _state["error"],
                "build_attempts": _state["attempts"],
                "runtime": runtime_status()}
    return {
        "ready": True,
        "build_seconds": _state["build_seconds"],
        "profile": _dataset_profile(),
        "status": copilot.status(),
        "runtime": runtime_status(),
    }
