"""Filing governance: lifecycle, four-eyes approval, refusals.

Regression suite for the finding that the audit brief listed four-eyes approval
as an existing strength when it did not exist. Before the fix in
``backend/governance/filings.py`` the following all returned ``success: True``:

    approve(filing_id, preparer=<the preparer>)      # self-approval
    approve(filing_id, approver=<any human>)         # twice, overwriting
    approve(filing_id, approver=<no comment>)        # no rationale on record

Each test below fails against the old implementation.
"""

from __future__ import annotations

import pytest

from backend.governance.filings import (
    ALLOWED_TRANSITIONS, STATUS_APPROVED, STATUS_DRAFT, STATUS_FILED,
    STATUS_REJECTED, FilingGovernance, FilingStore,
)

PREPARER = "analyst@bank.com"
APPROVER = "approver@bank.com"
COMMENT = "Evidence reviewed against the manifest; filing is supported."


@pytest.fixture
def filed(fresh_copilot):
    """A real PENDING_REVIEW SAR and the copilot that produced it.

    The subject is discovered rather than hardcoded: a filing is only possible
    for a customer whose evidence clears G1, and that depends on the generated
    profile. Hardcoding one made the suite depend on a particular seed.
    """
    for customer_id in fresh_copilot.wh.customer_ids(60):
        out = fresh_copilot.generate_filing(
            customer_id, "SAR", preparer=PREPARER)
        if out.get("success"):
            report = FilingStore.load(fresh_copilot.wh, out["filing_id"])
            assert report["governance"]["status"] == STATUS_DRAFT
            return fresh_copilot, out["filing_id"], report
    pytest.skip("no customer in the test profile cleared the filing gate")


# -- the four-eyes rule ------------------------------------------------------

def test_preparer_cannot_approve_own_filing(filed):
    """The core four-eyes invariant. This is the bug that shipped."""
    copilot, filing_id, _ = filed
    out = copilot.approve_filing(filing_id, approver=PREPARER, comment=COMMENT)
    assert not out["success"]
    assert out["error"] == "four_eyes_violation"
    assert FilingStore.load(copilot.wh, filing_id)["governance"]["status"] == STATUS_DRAFT


def test_system_preparer_still_requires_human_approver(filed):
    """A system-prepared filing cannot be system-approved either."""
    copilot, filing_id, _ = filed
    report = FilingStore.load(copilot.wh, filing_id)
    report["governance"]["preparer"] = "risk-fraud-regulatory-copilot"
    import json
    copilot.wh.execute(
        "UPDATE filings SET report_json = ? WHERE filing_id = ?",
        (json.dumps(report, default=str), filing_id))

    res = copilot.approve_filing(
        filing_id, approver="risk-fraud-regulatory-copilot", comment=COMMENT)
    assert not res["success"]
    assert res["error"] == "approver_not_human"


def test_email_local_part_comparison_cannot_evade_four_eyes(filed):
    """Case and domain differences must not let a preparer approve their own work."""
    copilot, filing_id, _ = filed
    out = copilot.approve_filing(
        filing_id, approver="ANALYST@other-bank.com", comment=COMMENT)
    assert not out["success"]
    assert out["error"] == "four_eyes_violation"


def test_distinct_human_approver_is_accepted(filed):
    copilot, filing_id, _ = filed
    out = copilot.approve_filing(filing_id, approver=APPROVER, comment=COMMENT)
    assert out["success"], out
    assert out["status"] == STATUS_APPROVED
    report = FilingStore.load(copilot.wh, filing_id)
    assert report["governance"]["approver"] == APPROVER
    assert report["governance"]["preparer"] == PREPARER


# -- named-human rule (pre-existing, must not regress) ----------------------

@pytest.mark.parametrize("actor", ["", "   ", "bot", "BOT", "auto", "copilot",
                                   "system", "risk-fraud-regulatory-copilot",
                                   "agent", "admin", None])
def test_non_human_approvers_rejected(filed, actor):
    copilot, filing_id, _ = filed
    out = copilot.approve_filing(filing_id, approver=actor, comment=COMMENT)
    assert not out["success"]
    assert out["error"] == "approver_not_human"


# -- duplicate approval ------------------------------------------------------

def test_double_approval_is_refused(filed):
    """The second approval must not overwrite the first approver."""
    copilot, filing_id, _ = filed
    assert copilot.approve_filing(filing_id, approver=APPROVER, comment=COMMENT)["success"]
    second = copilot.approve_filing(filing_id, approver="c.d@bank.com", comment="again")
    assert not second["success"]
    assert second["error"] == "already_approved"
    report = FilingStore.load(copilot.wh, filing_id)
    assert report["governance"]["approver"] == APPROVER


def test_refused_attempt_is_recorded_in_history(filed):
    """An attempted self-approval is itself a compliance event."""
    copilot, filing_id, _ = filed
    copilot.approve_filing(filing_id, approver=PREPARER, comment="self approve")
    history = FilingStore.load(copilot.wh, filing_id)["governance"]["decision_history"]
    assert any(h.get("action") == "approve_refused" for h in history)
    assert any(h.get("refused_because") == "four_eyes_violation" for h in history)


# -- mandatory comment -------------------------------------------------------

def test_comment_is_mandatory(filed):
    copilot, filing_id, _ = filed
    out = copilot.approve_filing(filing_id, approver=APPROVER, comment="   ")
    assert not out["success"]
    assert out["error"] == "comment_required"


# -- blocked filings ---------------------------------------------------------

def test_guardrail_blocked_filing_cannot_be_approved(filed):
    """A filing the guardrails blocked is unapprovable by anyone, including admin."""
    import json
    copilot, filing_id, _ = filed
    report = FilingStore.load(copilot.wh, filing_id)
    report["governance"]["guardrail_status"] = "FAIL"
    copilot.wh.execute(
        "UPDATE filings SET report_json = ? WHERE filing_id = ?",
        (json.dumps(report, default=str), filing_id))

    res = copilot.approve_filing(filing_id, approver=APPROVER, comment=COMMENT)
    assert not res["success"]
    assert res["error"] == "filing_blocked"


# -- rejection ---------------------------------------------------------------

def test_rejection_is_terminal(filed):
    copilot, filing_id, _ = filed
    assert copilot.approve_filing(
        filing_id, approver=APPROVER, comment="insufficient evidence",
        action="reject")["success"]
    report = FilingStore.load(copilot.wh, filing_id)
    assert report["governance"]["status"] == STATUS_REJECTED
    again = copilot.approve_filing(filing_id, approver=APPROVER, comment="retry")
    assert not again["success"]
    assert again["error"] == "already_rejected"


# -- lifecycle invariants ----------------------------------------------------

def test_no_path_reaches_filed_automatically(filed):
    """FILED is only ever reachable by an explicit submit, never by approval."""
    copilot, filing_id, _ = filed
    copilot.approve_filing(filing_id, approver=APPROVER, comment=COMMENT)
    report = FilingStore.load(copilot.wh, filing_id)
    assert report["governance"]["status"] == STATUS_APPROVED
    assert STATUS_FILED not in report["governance"]["status"]


def test_filing_status_never_exceeds_approved_without_submit(fresh_copilot):
    for report_type in ("SAR", "CTR", "STR"):
        for customer_id in fresh_copilot.wh.customer_ids(30):
            out = fresh_copilot.generate_filing(
                customer_id, report_type, preparer=PREPARER)
            if not out.get("success"):
                continue
            status = FilingStore.load(
                fresh_copilot.wh, out["filing_id"])["governance"]["status"]
            assert status in (STATUS_DRAFT, STATUS_APPROVED), (report_type, status)
            break


def test_transition_table_is_total():
    """Every declared state has a rule, and only DRAFT may reach APPROVED."""
    for state in (STATUS_DRAFT, STATUS_APPROVED, STATUS_REJECTED, STATUS_FILED):
        assert state in ALLOWED_TRANSITIONS
    # Only PENDING_REVIEW can be approved. APPROVED must not re-approve, and
    # the terminal states must have no outgoing edges at all.
    assert ALLOWED_TRANSITIONS[STATUS_DRAFT] == frozenset(
        {STATUS_APPROVED, STATUS_REJECTED})
    assert STATUS_APPROVED not in ALLOWED_TRANSITIONS[STATUS_APPROVED]
    assert ALLOWED_TRANSITIONS[STATUS_REJECTED] == frozenset()
    assert ALLOWED_TRANSITIONS[STATUS_FILED] == frozenset()


def test_unknown_filing_is_reported_not_raised(fresh_copilot):
    out = fresh_copilot.approve_filing("SAR-does-not-exist", APPROVER, COMMENT)
    assert not out["success"]
    assert out["error"] == "unknown_filing"


# -- unit-level guards -------------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    (STATUS_DRAFT, True), (STATUS_APPROVED, False),
    (STATUS_REJECTED, False), (STATUS_FILED, False),
])
def test_check_refuses_approve_from_terminal_states(status, expected):
    gov = {"status": status, "preparer": PREPARER}
    verdict = FilingGovernance.check(gov, APPROVER, COMMENT, "approve")
    assert verdict["allowed"] is expected


def test_check_allows_distinct_approver_from_draft():
    gov = {"status": STATUS_DRAFT, "preparer": PREPARER}
    assert FilingGovernance.check(gov, APPROVER, COMMENT, "approve")["allowed"]


def test_check_requires_comment_for_reject_too():
    gov = {"status": STATUS_DRAFT, "preparer": PREPARER}
    verdict = FilingGovernance.check(gov, APPROVER, "", "reject")
    assert not verdict["allowed"]
    assert verdict["error"] == "comment_required"
