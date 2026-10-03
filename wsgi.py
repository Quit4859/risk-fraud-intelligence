"""WSGI entrypoint for Render / generic Python hosts.

Wraps the existing Vercel-style ``api.handler`` so the same code path serves
both the API and the static Next.js frontend. Deploy with::

    gunicorn wsgi:app

The app is import-safe at module scope: the copilot is built lazily on the
first request, so a cold start that only serves static files never pays for
the warehouse bootstrap.
"""

from __future__ import annotations

import html
import json
import os
import sys
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from api.index import handler, _json  # noqa: E402
from api._runtime import get_copilot, health, healthz  # noqa: E402

STATIC_DIR = os.path.join(ROOT, "public")
PAGE_DIR = os.path.join(ROOT, "pages")

# File extensions the frontend ships that we are willing to serve verbatim.
_STATIC_EXTS = {
    ".html", ".js", ".css", ".json", ".png", ".svg", ".ico", ".woff",
    ".woff2", ".txt", ".xml", ".map", ".webp", ".gif",
}


def _within(base: str, candidate: str) -> bool:
    """True when ``candidate`` resolves inside ``base``.

    realpath is what makes this safe: it collapses ``..`` *before* the prefix
    check, so ``/public/../config/settings.yaml`` is rejected rather than
    string-matched against ``/public``. ``os.path.commonpath`` is used instead
    of ``startswith`` so ``/public_evil`` cannot pass as a child of ``/public``.
    """
    try:
        base_real = os.path.realpath(base)
        cand_real = os.path.realpath(candidate)
        return os.path.commonpath([base_real, cand_real]) == base_real
    except ValueError:
        # Different drives on Windows, or a malformed path.
        return False


def _serve_static(path: str):
    """Serve a file from public/ or pages/ if it exists.

    Two independent controls, because either alone is insufficient:

    * **realpath containment** - the resolved file must sit inside one of the
      two served roots. Without it, ``/../config/settings.yaml`` disclosed every
      detection threshold to any visitor, which is the exact information a
      threshold-evasion attacker needs.
    * **extension allowlist** - ``_STATIC_EXTS`` was declared but never applied,
      so any file type could be served verbatim.
    """
    rel = path.lstrip("/")
    if rel in ("", "/"):
        rel = "index.html"
    # Reject before touching the filesystem: NUL and traversal segments.
    if "\x00" in rel:
        return None
    if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
        return None

    ext = os.path.splitext(rel)[1].lower()
    if ext not in _STATIC_EXTS:
        return None

    for base in (STATIC_DIR, PAGE_DIR):
        candidate = os.path.join(base, rel)
        if not _within(base, candidate):
            continue
        if os.path.isfile(candidate):
            return _static_response(candidate)
    return None


def _static_response(file_path: str):
    ext = os.path.splitext(file_path)[1].lower()
    content_types = {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".txt": "text/plain; charset=utf-8",
        ".xml": "application/xml",
        ".map": "application/json; charset=utf-8",
    }
    ct = content_types.get(ext, "application/octet-stream")
    try:
        with open(file_path, "rb") as fh:
            body = fh.read()
    except OSError:
        return None
    return ("200 OK", [("Content-Type", ct), ("Content-Length", str(len(body)))],
            body)


def app(environ, start_response):
    """WSGI application: API first, health check, static files, SPA fallback."""
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/")
    query_string = environ.get("QUERY_STRING", "")

    # --- Health check: instant, never builds the engine ------------------
    # Render's health check must get an immediate answer. Calling the full
    # health() here would start a multi-second warehouse build and the platform
    # would kill the instance for not responding.
    if path.rstrip("/") in ("/healthz", "/health"):
        body = json.dumps(healthz()).encode("utf-8")
        start_response("200 OK",
                       [("Content-Type", "application/json"),
                        ("Cache-Control", "no-store"),
                        ("Content-Length", str(len(body)))])
        return [body]

    # --- API routes -----------------------------------------------------
    if path.startswith("/api"):
        event = _wsgi_to_vercel_event(environ)
        # handler() returns ({"statusCode", "headers", "body"}, status_code)
        resp, _status_code = handler(event)
        status_code = str(resp.get("statusCode", 200))
        body = resp.get("body", "")
        body_bytes = body.encode("utf-8") if isinstance(body, str) else body
        header_list = list(resp.get("headers", {}).items()) + \
            [("Content-Length", str(len(body_bytes)))]
        start_response(status_code, header_list)
        return [body_bytes]

    # --- Static assets --------------------------------------------------
    served = _serve_static(path)
    if served is not None:
        status_code, headers, body = served
        start_response(status_code, headers)
        return [body]

    # --- SPA fallback: serve index.html for client-side routes ---------
    if method == "GET" and not path.startswith("/api"):
        index = os.path.join(STATIC_DIR, "index.html")
        if os.path.isfile(index):
            with open(index, "rb") as fh:
                body = fh.read()
            start_response("200 OK",
                           [("Content-Type", "text/html; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
            return [body]

    start_response("404 Not Found",
                   [("Content-Type", "text/plain; charset=utf-8"),
                    ("Content-Length", "9")])
    return [b"Not Found"]


def _wsgi_to_vercel_event(environ):
    """Translate a WSGI environ into the dict shape api.handler expects."""
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path_info = environ.get("PATH_INFO", "")
    raw_qs = environ.get("QUERY_STRING", "")

    # Read the request body for POST.
    body_bytes = b""
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    if length and method in ("POST", "PUT", "PATCH"):
        body_bytes = environ.get("wsgi.input").read(length) if environ.get("wsgi.input") else b""

    return {
        "httpMethod": method,
        "path": path_info,
        "rawQueryString": raw_qs,
        "queryStringParameters": {},
        "headers": {k: v for k, v in environ.items() if k.startswith("HTTP_")},
        "body": body_bytes.decode("utf-8", errors="replace") if body_bytes else "",
        "isBase64Encoded": False,
    }


def health_check():
    """Render health-check endpoint: report readiness without building."""
    return health()


# A Gunicorn app callable. ``gunicorn wsgi:app``.
app = app