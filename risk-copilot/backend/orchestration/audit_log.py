"""Append-only audit trail.

Every governed action - detection, policy matching, evidence assembly, filing
generation, guardrail decision, external escalation, human approval - is
recorded with its actor, decision, rationale and payload hash. This is the
artefact a supervisor or examiner reads to reconstruct *why* the copilot did
what it did, and it is what makes the four-eyes control (clause FILE-2.1.1)
evidenced rather than assumed.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.config import load_config

ACTIONS = (
    "pipeline.build",
    "detect.customer",
    "detect.population",
    "policy.match",
    "evidence.assemble",
    "guardrail.evaluate",
    "filing.generate",
    "filing.approve",
    "question.ask",
    "answer.compose",
    "escalation.jira",
    "escalation.slack",
    "fallback.trigger",
    "session.start",
)


def new_run_id(prefix: str = "run") -> str:
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


class AuditLog:
    def __init__(self, warehouse, run_id: Optional[str] = None,
                 config: Optional[Dict[str, Any]] = None, actor: str = "copilot"):
        self.wh = warehouse
        self.cfg = config or load_config()
        self.run_id = run_id or new_run_id()
        self.actor = actor

    def record(self, action: str, target: str, decision: str, rationale: str,
               payload: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.wh.execute(
                "INSERT INTO audit_log (run_id, ts, actor, action, target, decision,"
                " rationale, payload_json) VALUES (?,?,?,?,?,?,?,?)",
                (self.run_id, datetime.utcnow().isoformat(timespec="seconds") + "Z",
                 self.actor, action, str(target)[:256], decision, rationale[:8192],
                 json.dumps(payload or {}, default=str)[:16000]))
        except Exception:
            # An audit failure must never take down an analysis, but it must be
            # visible: fall back to the JSONL mirror.
            self._mirror(action, target, decision, rationale, payload)

    def _mirror(self, action: str, target: str, decision: str, rationale: str,
                payload: Optional[Dict[str, Any]]) -> None:
        path = os.path.join(self.cfg["_root"], self.cfg["paths"]["audit_log"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "run_id": self.run_id, "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "actor": self.actor, "action": action, "target": target,
                "decision": decision, "rationale": rationale, "payload": payload or {},
            }, default=str) + "\n")

    # -- read models --------------------------------------------------------
    def trace(self, run_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        run_id = run_id or self.run_id
        rows = self.wh.query(
            "SELECT audit_id, run_id, ts, actor, action, target, decision, rationale"
            " FROM audit_log WHERE run_id = ? ORDER BY audit_id LIMIT ?",
            (run_id, limit))
        return rows

    def recent(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.wh.query(
            "SELECT audit_id, run_id, ts, actor, action, target, decision, rationale"
            " FROM audit_log ORDER BY audit_id DESC LIMIT ?", (limit,))

    def decision_counts(self, run_id: Optional[str] = None) -> Dict[str, int]:
        if run_id:
            rows = self.wh.query(
                "SELECT decision, COUNT(*) AS n FROM audit_log WHERE run_id = ?"
                " GROUP BY decision", (run_id,))
        else:
            rows = self.wh.query(
                "SELECT decision, COUNT(*) AS n FROM audit_log GROUP BY decision")
        return {r["decision"]: r["n"] for r in rows}

    def export(self, path: str) -> str:
        rows = self.wh.query("SELECT * FROM audit_log ORDER BY audit_id")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, default=str)
        return path
