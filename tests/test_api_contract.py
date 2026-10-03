"""API contract, response validity and static-file safety.

Covers three defects that a functional test would never surface:

* B6 - ``_json`` sliced the serialised body at 200,000 characters, so any large
  response was *invalid JSON* and no client could parse it.
* B7 - the static server joined a user path onto a base with no realpath
  containment and never applied its own extension allowlist, so
  ``/../config/settings.yaml`` served every detection threshold.
* B8 - responses carried ``Access-Control-Allow-Origin: *`` on state-changing
  actions, letting any website drive the copilot from a browser.
"""

from __future__ import annotations

import json
import os

import pytest

import wsgi
from api.index import MAX_PAGE_SIZE, _json, _paginate, example_questions, handler


# -- B6: response validity ---------------------------------------------------

def test_large_response_is_valid_json():
    """The regression: string truncation produced unparseable JSON."""
    payload = {"rows": [{"x": "y" * 1000} for _ in range(500)]}
    resp, status = _json(payload)
    assert status == 200
    assert len(resp["body"]) > 200_000, "payload should exceed the old 200k cap"
    parsed = json.loads(resp["body"])  # must not raise
    assert len(parsed["rows"]) == 500


@pytest.mark.parametrize("n_rows,row_bytes", [(0, 0), (1, 10), (100, 1000),
                                              (500, 2000), (2000, 900)])
def test_response_is_always_parseable(n_rows, row_bytes):
    payload = {"rows": [{"blob": "z" * row_bytes} for _ in range(n_rows)]}
    body = _json(payload)[0]["body"]
    assert json.loads(body) == payload


def test_paginate_reports_truncation_instead_of_hiding_it():
    rows = [{"i": i} for i in range(120)]
    page = _paginate(rows, offset=0, limit=50)
    assert page["returned"] == 50
    assert page["total"] == 120
    assert page["truncated"] is True

    last = _paginate(rows, offset=100, limit=50)
    assert last["returned"] == 20
    assert last["truncated"] is False


def test_paginate_caps_page_size():
    rows = [{"i": i} for i in range(MAX_PAGE_SIZE + 100)]
    page = _paginate(rows, 0, limit=10_000_000)
    assert page["limit"] == MAX_PAGE_SIZE


def test_paginate_survives_junk_paging_parameters():
    page = _paginate([{"a": 1}], offset="abc", limit=None)
    assert page["rows"] == [{"a": 1}]
    assert page["offset"] == 0


# -- B8: CORS ----------------------------------------------------------------

def test_no_wildcard_cors_header():
    """A wildcard origin let any site call the write actions from a browser."""
    _, health_payload = _json({"ok": True})
    resp, _ = _json({"ok": True})
    assert resp["headers"].get("Access-Control-Allow-Origin") != "*"
    assert "Vary" in resp["headers"]


def test_cors_only_echoes_an_explicitly_allowed_origin(monkeypatch):
    from api.index import _cors_for

    monkeypatch.setenv("RISK_ALLOWED_ORIGIN", "https://risk.example.com")
    same = _cors_for({"headers": {"Origin": "https://risk.example.com"}})
    assert same["Access-Control-Allow-Origin"] == "https://risk.example.com"

    other = _cors_for({"headers": {"Origin": "https://evil.example"}})
    assert "Access-Control-Allow-Origin" not in other


def test_cors_absent_when_no_origin_configured(monkeypatch):
    from api.index import _cors_for

    monkeypatch.delenv("RISK_ALLOWED_ORIGIN", raising=False)
    assert "Access-Control-Allow-Origin" not in _cors_for({"headers": {}})


# -- response envelope -------------------------------------------------------

def test_every_action_returns_an_integer_status_code():
    """The regression: ``_json(payload, cors)`` passed the CORS header dict into
    ``_json``'s *status* parameter, so the envelope carried
    ``statusCode: {"Vary": "Origin"}`` and a WSGI server or API Gateway rejected
    the response. Exercised per action because one correct call site hides six
    broken ones.
    """
    for action in ("health", "status", "examples", "corpus", "mcp", "audit",
                   "policy", "findings", "evaluate", "whoami", "not_an_action"):
        resp, status = handler({"httpMethod": "GET", "path": f"/api?action={action}",
                                "headers": {}}, None)
        assert isinstance(resp.get("statusCode"), int), (
            f"action {action!r} produced statusCode={resp.get('statusCode')!r}")
        assert 100 <= resp["statusCode"] < 600
        json.loads(resp["body"])  # body must still be valid JSON


def test_status_and_headers_are_not_swapped():
    resp, _ = handler({"httpMethod": "GET", "path": "/api?action=health",
                       "headers": {}}, None)
    assert isinstance(resp["statusCode"], int)
    assert isinstance(resp["headers"], dict)
    assert resp["headers"]["Content-Type"] == "application/json"


# -- B7: static file safety --------------------------------------------------

@pytest.mark.parametrize("path", [
    "/../wsgi.py",
    "/../config/settings.yaml",
    "/../.env",
    "/../requirements.txt",
    "/../backend/config.py",
    "/../../etc/passwd",
    "/..%2fwsgi.py",
    "/..\\wsgi.py",
    "/public/../../config/settings.yaml",
    "//../config/settings.yaml",
])
def test_path_traversal_is_blocked(path):
    assert wsgi._serve_static(path) is None, f"{path} escaped the web root"


def test_extension_allowlist_is_enforced():
    """_STATIC_EXTS was declared but never applied."""
    forbidden = "/../config/settings.yaml"     # .yaml not in the allowlist
    assert wsgi._serve_static(forbidden) is None
    # A .py file inside the served root would also be refused.
    assert wsgi._serve_static("/sneaky.py") is None


def test_legitimate_static_files_still_serve():
    served = wsgi._serve_static("/index.html")
    assert served is not None
    status, headers, body = served
    assert status == "200 OK"
    assert any(k == "Content-Type" for k, _ in headers)
    assert b"<html" in body.lower()


def test_root_serves_index():
    assert wsgi._serve_static("/") is not None


def test_status_lines_have_reason_phrases():
    """WSGI requires '200 OK', not a bare '200'."""
    status, _, _ = wsgi._serve_static("/index.html")
    assert status == "200 OK"
    assert len(status.split(" ", 1)) == 2


# -- B12: health check -------------------------------------------------------

def test_healthz_does_not_build_the_engine():
    """The health check must answer instantly, even mid-build."""
    payload = wsgi.healthz()
    assert payload["status"] in ("ok", "warming_up")
    assert "uptime_seconds" in payload
    assert "engine" in payload


def test_healthz_route_served():
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(wsgi.app({
        "REQUEST_METHOD": "GET", "PATH_INFO": "/healthz", "QUERY_STRING": "",
    }, start_response))
    assert captured["status"] == "200 OK"
    assert json.loads(body)["status"] in ("ok", "warming_up")


# -- contract ----------------------------------------------------------------

# Read actions are gated behind a session since B8. These tests exercise the
# *contract* of the response, not the access control, so they authenticate
# first; tests/test_rbac.py covers who is allowed to do what.
_TOKEN = json.loads(
    handler({"httpMethod": "POST", "rawQueryString": "", "headers": {},
             "body": '{"action":"login","role":"analyst"}'}, None)[0]["body"]
)["token"]


def _get(action, **extra):
    return handler({"httpMethod": "GET", "headers": {},
                    "rawQueryString": f"action={action}&token={_TOKEN}"}, None)


def _post(body):
    return handler({"httpMethod": "POST", "headers": {},
                    "body": json.dumps({**body, "token": _TOKEN})}, None)


def test_unknown_action_reports_available_actions():
    resp, status = handler({"httpMethod": "GET", "rawQueryString": "action=bogus",
                            "headers": {}}, None)
    assert status == 400
    body = json.loads(resp["body"])
    assert body["error"] == "unknown_action"
    assert "ask" in body["available"] and "scan" in body["available"]


def test_ask_requires_a_question():
    resp, status = _post({"action": "ask"})
    assert status == 400
    assert json.loads(resp["body"])["error"] == "question is required"


def test_examples_action_returns_resolved_subjects():
    resp, _ = _get("examples")
    questions = json.loads(resp["body"])["questions"]
    assert len(questions) == 12
    assert all("{" not in q for q in questions)
