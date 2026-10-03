"""Access control and four-eyes integration.

B8 in the audit was "no authn/authz, CORS * on every response, and the web
frontend proxying localhost:8000 so the app is not actually deployable". CORS
and proxying are fixed elsewhere. These tests pin the part that makes the
four-eyes control meaningful: **the approver identity comes from a signed
session, never from the request body.**

A caller who could POST ``{"action":"approve","approver":"chief.compliance@bank.com"}``
would satisfy the preparer != approver check while being the preparer. That is
the attack these tests exist to make impossible.
"""

import json

import pytest

from api.index import handler
from backend.governance import rbac


# ------------------------------------------------------------------ unit
def test_role_matrix_matches_the_governance_model():
    """An analyst may draft but not approve; that split is the whole point."""
    assert rbac.can("analyst", "file")
    assert not rbac.can("analyst", "approve")
    assert rbac.can("approver", "approve")
    assert not rbac.can("approver", "scan")


def test_unknown_action_is_refused_not_allowed_by_default():
    allowed, detail = rbac.authorize("delete_everything", {"user": "a", "role": "admin"})
    assert not allowed
    assert detail["error"] == "unknown_action"


def test_anonymous_cannot_write_but_can_see_the_matrix():
    allowed, detail = rbac.authorize("file", None)
    assert not allowed
    assert detail["error"] == "authentication_required"
    assert "admin" in detail["required_role"]


def test_analyst_is_forbidden_from_approving_with_403():
    allowed, detail = rbac.authorize("approve", {"user": "a@bank.com", "role": "analyst"})
    assert not allowed
    assert detail["error"] == "insufficient_role"
    assert detail["required_permission"] == "approve"


def test_token_round_trip_carries_the_role():
    token = rbac.issue_token("k.approver@bank.com", "approver")
    claims, err = rbac.verify_token(token)
    assert err is None
    assert claims["role"] == "approver"
    assert claims["user"] == "k.approver@bank.com"


def test_tampered_payload_cannot_promote_analyst_to_admin():
    """The attack this whole module exists to stop.

    Flip the role claim in the token body and re-sign nothing: the signature
    covers the payload, so the forgery is rejected.
    """
    token = rbac.issue_token("a.analyst@bank.com", "analyst")
    body, _, _sig = token.partition(".")
    raw = rbac._unb64(body)
    claims = json.loads(raw)
    claims["role"] = "admin"
    forged = rbac._b64(json.dumps(claims).encode()) + "." + _sig.partition(".")[2]
    got, err = rbac.verify_token(forged)
    assert got is None
    assert err == "invalid_signature"


def test_expired_token_is_rejected():
    token = rbac.issue_token("a@bank.com", "analyst", ttl=-1)
    claims, err = rbac.verify_token(token)
    assert claims is None
    assert err == "session_expired"


def test_garbage_token_is_rejected_without_raising():
    for bad in ("", "no-dot", "a.b.c", "!!!.???"):
        claims, err = rbac.verify_token(bad)
        assert claims is None
        assert err


def test_unknown_role_cannot_be_issued():
    with pytest.raises(ValueError):
        rbac.issue_token("x", "superuser")


# ------------------------------------------------------------------ HTTP
def _post(app, body):
    """Drive the real API entrypoint with a synthetic API Gateway event.

    The handler is called directly rather than over a socket: it is the same
    function Render and Lambda invoke, so this exercises the deployed code path
    without the flakiness and the ten-second boot cost of a live server.
    """
    event = {"httpMethod": "POST", "path": "/api", "headers": {},
             "body": json.dumps(body)}
    resp, status = handler(event, None)
    return status, json.loads(resp["body"]), resp["headers"]


def _cookie_header(headers):
    return headers.get("Set-Cookie", "")


def test_health_stays_open_so_the_platform_can_probe_it():
    status, body, _ = _post(None, {"action": "health"})
    assert status == 200
    assert "status" in body


def test_file_without_a_session_is_401():
    status, body, _ = _post(None, {"action": "file", "customer_id": "CUST-000001"})
    assert status == 401
    assert body["error"] == "authentication_required"


def test_login_sets_an_httponly_session_cookie():
    """HttpOnly so page scripts cannot read the token; the CSRF surface that
    a JavaScript-readable session cookie would open is not worth having."""
    _, body, headers = _post(None, {"action": "login", "role": "analyst"})
    assert body["role"] == "analyst"
    cookie = _cookie_header(headers)
    assert "risk_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_analyst_cannot_reach_approve_through_http():
    _, analyst, _ = _post(None, {"action": "login", "role": "analyst"})
    status, body, _ = _post(None, {"action": "approve", "filing_id": "RPT-X",
                                   "approver": "chief.compliance@bank.com",
                                   "token": analyst["token"]})
    assert status == 403
    assert body["error"] == "insufficient_role"


def test_login_with_a_bad_role_is_400():
    status, body, _ = _post(None, {"action": "login", "role": "root"})
    assert status == 400
    assert body["error"] == "unknown_role"


def test_preparer_cannot_approve_their_own_filing_end_to_end():
    """The full four-eyes path, through the real handler, with real sessions."""
    _, analyst, _ = _post(None, {"action": "login", "role": "analyst",
                                 "user": "a.analyst@bank.com"})
    status, drafted, _ = _post(None, {"action": "file", "customer_id": "CUST-000001",
                                      "filing_type": "SAR", "token": analyst["token"]})
    if not drafted.get("success"):
        pytest.skip(f"customer not fileable in this fixture: {drafted.get('error')}")
    filing_id = drafted["filing_id"]

    # Same human, second session, now holding an approver role. Four-eyes must
    # still refuse: the rule is about the *person*, not the token.
    _, as_approver, _ = _post(None, {"action": "login", "role": "approver",
                                     "user": "a.analyst@bank.com"})
    status, body, _ = _post(None, {"action": "approve", "filing_id": filing_id,
                                   "comment": "looks right to me",
                                   "token": as_approver["token"]})
    assert body.get("success") is False
    assert "preparer" in json.dumps(body).lower()

    # A genuinely different human clears it.
    _, other, _ = _post(None, {"action": "login", "role": "approver",
                               "user": "k.approver@bank.com"})
    _, body, _ = _post(None, {"action": "approve", "filing_id": filing_id,
                              "comment": "independently reviewed",
                              "token": other["token"]})
    assert body.get("success") is True
    assert body["report"]["status"] == "APPROVED"


def test_whoami_reports_the_current_role_and_matrix():
    _, analyst, _ = _post(None, {"action": "login", "role": "analyst"})
    status, body, _ = _post(None, {"action": "whoami", "token": analyst["token"]})
    assert status == 200
    assert body["role"] == "analyst"
    assert "approve" in body["permissions"]["approver"]
    assert "approve" not in body["permissions"]["analyst"]
