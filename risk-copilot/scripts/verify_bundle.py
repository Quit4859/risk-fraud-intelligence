#!/usr/bin/env python3
"""Build the exact Vercel upload bundle locally and verify the runtime in it.

This catches the failure mode where a file is accidentally ``.vercelignore``d
but is still imported at cold start - which only shows up in production.

    python scripts/verify_bundle.py
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IGNORE = os.path.join(ROOT, ".vercelignore")


def load_patterns() -> list:
    patterns = []
    with open(IGNORE, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return patterns


def ignored(rel: str, patterns: list) -> bool:
    parts = rel.split("/")
    for pattern in patterns:
        p = pattern.rstrip("/")
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p + "/*"):
            return True
        if any(fnmatch.fnmatch(part, p) for part in parts):
            return True
        if p.endswith("/*") and fnmatch.fnmatch(rel, p[:-2]):
            return True
    return False


def build_bundle(dest: str) -> list:
    patterns = load_patterns()
    included = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "node_modules")]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, ROOT)
            if ignored(rel, patterns):
                continue
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(full, target)
            included.append(rel)
    return included


REQUIRED_AT_RUNTIME = [
    "api/index.py", "api/_runtime.py", "api/__init__.py",
    "backend/config.py", "backend/warehouse.py", "backend/cli.py",
    "backend/agents/fraud_detector.py", "backend/agents/retrieval.py",
    "backend/agents/intent_router.py", "backend/agents/executors.py",
    "backend/agents/answer_composer.py", "backend/agents/policy_matcher.py",
    "backend/agents/evidence_gatherer.py", "backend/agents/report_formatter.py",
    "backend/orchestration/copilot.py", "backend/orchestration/guardrails.py",
    "backend/orchestration/audit_log.py", "backend/orchestration/mcp_integrations.py",
    "backend/orchestration/multi_agent_workflow.py",
    "scripts/__init__.py", "scripts/generate_synthetic_data.py",
    "scripts/generate_corpus.py",
    "config/settings.yaml", "sql/04_semantic_views.sql",
    "data/gold/policies.json", "public/index.html",
    "requirements.txt", "vercel.json",
]

CORPUS_DOCS = ["GLOBAL-AML-001", "BASEL-3", "IN-NBFC", "SAN-1", "UK-PRA", "INT-FRAUD-01"]


def main() -> int:
    dest = tempfile.mkdtemp(prefix="vercel-bundle-")
    included = build_bundle(dest)
    print(f"[bundle] {len(included)} files, "
          f"{sum(os.path.getsize(os.path.join(dest, f)) for f in included) / 1e6:.1f} MB "
          f"-> {dest}")

    failures = []

    def check(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
              f"{(' - ' + str(detail)) if detail and not cond else ''}")
        if not cond:
            failures.append(name)

    print("[bundle] required runtime files present")
    for rel in REQUIRED_AT_RUNTIME:
        check(rel, os.path.exists(os.path.join(dest, rel)), "missing from bundle")
    for doc in CORPUS_DOCS:
        check(f"corpus {doc}.md", os.path.exists(os.path.join(dest, "docs/corpus", f"{doc}.md")))

    print("[bundle] heavy files excluded")
    for rel in ("data/raw/transactions.csv", "artifacts/warehouse.db"):
        check(f"excluded {rel}", not os.path.exists(os.path.join(dest, rel)))
    check("no Next.js scaffold", not os.path.exists(os.path.join(dest, "package.json")))
    check("static UI present", os.path.exists(os.path.join(dest, "public/index.html")))

    print("[bundle] imports resolve")
    res = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '.');"
         "import api.index, api._runtime;"
         "from backend.orchestration.copilot import RiskCopilot;"
         "from backend.orchestration.mcp_integrations import handle_tool_call;"
         "print('imports ok')"],
        cwd=dest, capture_output=True, text=True)
    check("all runtime imports succeed", res.returncode == 0,
          (res.stderr or "")[-600:])

    print("[bundle] handler works from the bundle alone")
    # Run the verifier *from inside the bundle* so its sys.path points at the
    # bundle, not the source tree - otherwise this proves nothing.
    verifier = os.path.join(dest, "scripts", "verify_vercel.py")
    os.makedirs(os.path.dirname(verifier), exist_ok=True)
    shutil.copy2(os.path.join(ROOT, "scripts", "verify_vercel.py"), verifier)
    res = subprocess.run([sys.executable, "scripts/verify_vercel.py"],
                         cwd=dest, capture_output=True, text=True)
    tail = (res.stdout or "").strip().splitlines()[-3:]
    for line in tail:
        print("       " + line)
    check("handler verification passes in the bundle", res.returncode == 0,
          (res.stdout or "")[-1500:] + (res.stderr or "")[-600:])
    check("verifier ran against the bundle, not the source tree",
          str(ROOT) not in (res.stdout or ""))

    shutil.rmtree(dest, ignore_errors=True)
    print()
    if failures:
        print(f"BUNDLE VERIFICATION FAILED: {failures}")
        return 1
    print("Deploy bundle is complete and self-contained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
