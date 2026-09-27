"""Configuration loading and repo paths.

The copilot keeps every decision-bearing constant in ``config/settings.yaml`` so
a compliance officer can diff it in a pull request. Code reads thresholds from
here and never hardcodes a regulatory number.
"""

from __future__ import annotations

import functools
import os
from typing import Any, Dict

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "settings.yaml")


@functools.lru_cache(maxsize=4)
def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["_root"] = ROOT
    _apply_env_overrides(cfg)
    return cfg


#: Keys under ``paths`` that may be redirected with an environment variable.
#: Vercel and other read-only serverless runtimes need artefacts in /tmp;
#: Streamlit deployments may want them elsewhere. Detection thresholds are
#: never overridable this way - they are configuration, not deployment config.
ENV_OVERRIDABLE_PATHS = {
    "RISK_ARTIFACT_DIR": ("warehouse_db", "audit_log", "evidence_dir", "reports_dir"),
    "RISK_DATA_DIR": ("raw_dir", "gold_dir"),
    "RISK_CORPUS_DIR": ("corpus_dir",),
}


def _apply_env_overrides(cfg: Dict[str, Any]) -> Dict[str, Any]:
    for env_var, keys in ENV_OVERRIDABLE_PATHS.items():
        base = os.environ.get(env_var)
        if not base:
            continue
        for key in keys:
            rel = cfg["paths"][key]
            if key == "warehouse_db" or key == "audit_log":
                cfg["paths"][key] = os.path.join(base, os.path.basename(rel))
            else:
                cfg["paths"][key] = os.path.join(base, os.path.basename(rel))
    return cfg


def resolve(path_key: str, path: str = CONFIG_PATH) -> str:
    """Resolve a ``paths.*`` entry to an absolute path."""
    cfg = load_config(path)
    rel = cfg["paths"][path_key]
    return rel if os.path.isabs(rel) else os.path.join(ROOT, rel)


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def cfg(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return config if config is not None else load_config()
