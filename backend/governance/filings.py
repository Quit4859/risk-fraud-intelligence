"""Filing lifecycle and four-eyes approval.

This is the only place in the system permitted to move a filing out of
``PENDING_REVIEW``. The invariants it enforces are the difference between a
compliance artefact and a rubber stamp, so they are stated as a single explicit
state machine rather than scattered through the reporter.

Lifecycle
---------
::

    PENDING_REVIEW --approve--> APPROVED
    PENDING_REVIEW --reject---> REJECTED   (terminal)
    APPROVED        --submit---> FILED     (never automatic)

``REJECTED`` is terminal. ``FILED`` is never reached without an explicit
submission, because filing with a regulator is an external, irreversible act.

Four-eyes
---------
A filing is drafted by the copilot *on behalf of* a named preparer, and approved
by a different named human. The audit brief listed four-eyes as an existing
strength; it was declared in ``governance.four_eyes_required`` but never
enforced. These are the checks that were missing:

1. the approver must be a named human - never the copilot, a bot, or blank;
2. **the approver must not be the preparer** (clause FILE-2.1.1);
3. the filing must currently be ``PENDING_REVIEW`` - no re-approval, so a second
   approval cannot silently overwrite the first approver and timestamp;
4. a written comment is mandatory on both approve and reject;
5. a filing blocked by a guardrail can never be approved at all.

Every rejection returns a machine-readable ``error`` code alongside a
human-readable reason, so the UI can explain the block rather than showing a
generic failure.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

STATUS_DRAFT = "PENDING_REVIEW"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_FILED = "FILED"

#: Identities that may never appear in the approver field, however they are
#: spelled. The system is approving its own output otherwise.
NON_HUMAN_APPROVERS = frozenset({
    "", "bot", "auto", "auto-approve", "copilot", "system", "agent", "ai",
    "risk-fraud-regulatory-copilot", "self", "me", "test", "none", "null",
    "unknown", "anonymous", "admin", "root",
})

#: The legal transitions. Anything absent from this map is refused.
ALLOWED_TRANSITIONS: Dict[str, frozenset] = {
    STATUS_DRAFT: frozenset({STATUS_APPROVED, STATUS_REJECTED}),
    STATUS_APPROVED: frozenset({STATUS_FILED}),
    STATUS_REJECTED: frozenset(),
    STATUS_FILED: frozenset(),
}


class FilingGovernance:
    """Stateless checks over a filing's governance block."""

    @staticmethod
    def is_human(approver: Optional[str]) -> bool:
        """Whether an approver string names a human rather than a system."""
        if not approver or not isinstance(approver, str):
            return False
        return approver.strip().lower() not in NON_HUMAN_APPROVERS

    @classmethod
    def same_person(cls, approver: Optional[str], preparer: Optional[str]) -> bool:
        """Whether the approver and preparer are the same principal.

        Compares on a normalised form (case, surrounding whitespace) and also on
        the local part of an email, so ``A.B@bank.com`` cannot approve a filing
        prepared by ``a.b@bank.com``.
        """
        if not approver or not preparer:
            return False
        norm = lambda v: str(v).strip().lower().split("@", 1)[0]  # noqa: E731
        return norm(approver) == norm(preparer)

    @classmethod
    def check(cls, governance: Dict[str, Any], approver: Optional[str],
              comment: Optional[str], action: str = "approve") -> Dict[str, Any]:
        """Validate a transition. Returns a structured verdict, never raises."""
        status = governance.get("status") or STATUS_DRAFT
        preparer = governance.get("preparer") or governance.get("generated_by")
        comment = (comment or "").strip()
        verb = "approve" if action == "approve" else "reject"

        def ok() -> Dict[str, Any]:
            return {"allowed": True, "status": status, "preparer": preparer,
                    "actor": approver}

        def no(code: str, reason: str) -> Dict[str, Any]:
            return {"allowed": False, "error": code, "reason": reason,
                    "status": status, "preparer": preparer, "actor": approver}

        # A blocked filing is not approvable by anyone, including an admin. The
        # guardrail verdict is upstream of the approval decision and cannot be
        # overridden at this layer.
        if governance.get("guardrail_status") == "FAIL" or governance.get("blocked"):
            return no("filing_blocked",
                      "Filing is blocked by a guardrail failure and cannot be "
                      "approved. Re-run detection to clear the block first.")

        if not cls.is_human(approver):
            return no("approver_not_human",
                      "A named human approver is required (clause FILE-2.1.1). "
                      "The copilot, a bot or a system account may not approve.")

        if action == "approve" and cls.same_person(approver, preparer):
            return no("four_eyes_violation",
                      f"Preparer and approver are the same person "
                      f"({approver}). A second pair of eyes is required "
                      f"(clause FILE-2.1.1).")

        if not comment:
            return no("comment_required",
                      f"A written {verb} comment is required and is recorded in "
                      f"the audit trail (clause FILE-2.1.1).")

        target = STATUS_APPROVED if action == "approve" else STATUS_REJECTED
        if target not in ALLOWED_TRANSITIONS.get(status, frozenset()):
            if status == STATUS_APPROVED:
                return no("already_approved",
                          f"Filing is already {STATUS_APPROVED} by "
                          f"{governance.get('approver')}. A second approval would "
                          f"overwrite the existing approval record and is refused.")
            if status == STATUS_REJECTED:
                return no("already_rejected", "Filing was already rejected; that is final.")
            if status == STATUS_FILED:
                return no("already_filed", "Filing has already been submitted.")
            return no("illegal_transition",
                      f"Cannot {verb} a filing in state {status}.")

        return ok()


class FilingStore:
    """Persistence for filing status transitions.

    Reads the full report, applies one transition, and writes it back. The
    approver, timestamp and comment are appended to a ``decision_history`` list
    so the trail is append-only and a refusal is as visible as an approval.
    """

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().isoformat(timespec="seconds") + "Z"

    @staticmethod
    def load(warehouse, filing_id: str) -> Optional[Dict[str, Any]]:
        row = warehouse.one(
            "SELECT report_json, status FROM filings WHERE filing_id = ?", (filing_id,))
        if not row:
            return None
        report = json.loads(row["report_json"])
        report.setdefault("governance", {})["status"] = row["status"]
        return report

    @classmethod
    def decide(cls, warehouse, filing_id: str, approver: str, comment: str,
               action: str = "approve") -> Dict[str, Any]:
        """Run the four-eyes check and, only if it passes, persist the decision."""
        report = cls.load(warehouse, filing_id)
        if report is None:
            return {"success": False, "error": "unknown_filing",
                    "reason": f"No filing {filing_id}.", "filing_id": filing_id}

        governance = report.get("governance") or {}
        verdict = FilingGovernance.check(governance, approver, comment, action)
        if not verdict["allowed"]:
            # The refusal is recorded too: an attempted self-approval is exactly
            # the event a compliance reviewer needs to see.
            cls._record_attempt(warehouse, filing_id, approver, verdict, action)
            return {"success": False, "filing_id": filing_id,
                    **verdict, "status": verdict.get("status")}

        target = STATUS_APPROVED if action == "approve" else STATUS_REJECTED
        now = cls._now()
        governance.update({
            "status": target,
            "approver": approver if action == "approve" else None,
            "rejected_by": approver if action == "reject" else None,
            "approved_at": now if action == "approve" else None,
            "rejected_at": now if action == "reject" else None,
            "decision_comment": comment,
            "decided_at": now,
        })
        governance.setdefault("decision_history", []).append({
            "action": action, "actor": approver, "at": now,
            "comment": comment, "status_before": verdict.get("status"),
            "status_after": target,
        })
        report["governance"] = governance

        warehouse.execute(
            "UPDATE filings SET report_json = ?, status = ?, approver = ?, approved_at = ?"
            " WHERE filing_id = ?",
            (json.dumps(report, default=str), target,
             approver if action == "approve" else None,
             now if action == "approve" else None, filing_id))
        return {"success": True, "filing_id": filing_id, "status": target,
                "approver": approver if action == "approve" else None,
                "decided_by": approver, "comment": comment,
                "preparer": verdict.get("preparer"), "decided_at": now}

    @staticmethod
    def _record_attempt(warehouse, filing_id: str, actor: str,
                        verdict: Dict[str, Any], action: str) -> None:
        """Append a refused decision to the filing's history."""
        report = FilingStore.load(warehouse, filing_id)
        if report is None:
            return
        governance = report.setdefault("governance", {})
        governance.setdefault("decision_history", []).append({
            "action": f"{action}_refused",
            "actor": actor,
            "at": FilingStore._now(),
            "comment": verdict.get("reason"),
            "refused_because": verdict.get("error"),
            "status_after": governance.get("status"),
        })
        warehouse.execute(
            "UPDATE filings SET report_json = ? WHERE filing_id = ?",
            (json.dumps(report, default=str), filing_id))
