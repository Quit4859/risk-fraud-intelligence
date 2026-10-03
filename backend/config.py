"""Configuration loading and repo paths.

The copilot keeps every decision-bearing constant in ``config/settings.yaml`` so
a compliance officer can diff it in a pull request. Code reads thresholds from
here and never hardcodes a regulatory number.
"""

from __future__ import annotations

import copy
import functools
import os
from typing import Any, Dict

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "settings.yaml")


@functools.lru_cache(maxsize=4)
def _read_settings(path: str = CONFIG_PATH) -> Dict[str, Any]:
    """Raw YAML, cached: the file is read at most once per path.

    This is intentionally separate from :func:`load_config` so that the
    deployment-time env overrides below are re-applied on every call.
    """
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    """Load governed configuration, applying deployment env overrides.

    The file is cached, but :func:`_apply_env_overrides` runs on every call so
    that redirecting ``RISK_DATA_DIR`` / ``RISK_ARTIFACT_DIR`` takes effect even
    after a first load. ``api._runtime`` sets those redirects at import time,
    i.e. before the copilot reads any path, so the engine never evaluates against
    a stale location.

    A fresh dict is returned per call so callers can never mutate shared state -
    the cache holds only the immutable YAML text, not the env-applied result.
    """
    cfg = copy.deepcopy(_read_settings(path))
    cfg["_root"] = ROOT
    _apply_env_overrides(cfg)
    return cfg


#: Keys under ``paths`` that may be redirected with an environment variable.
#: Vercel and other read-only serverless runtimes need artefacts in /tmp;
#: Streamlit deployments may want them elsewhere. Detection thresholds are
#: never overridable this way - they are configuration, not deployment config.
ENV_OVERRIDABLE_PATHS = {
    "RISK_ARTIFACT_DIR": ("warehouse_db", "audit_log", "evidence_dir", "reports_dir"),
    "RISK_DATA_DIR": ("raw_dir", "gold_dir", "gold_labels"),
    "RISK_CORPUS_DIR": ("corpus_dir",),
}


def _apply_env_overrides(cfg: Dict[str, Any]) -> Dict[str, Any]:
    for env_var, keys in ENV_OVERRIDABLE_PATHS.items():
        base = os.environ.get(env_var)
        if not base:
            continue
        for key in keys:
            rel = cfg["paths"][key]
            if key == "gold_labels":
                # A *file* inside gold_dir, not a sibling of it. Redirecting the
                # data directory must move the labels with the data they
                # describe: evaluating the detector against a different
                # population's labels is worse than failing outright. Deriving
                # the location from the already-redirected gold_dir keeps the
                # two in step whatever the layout.
                cfg["paths"][key] = os.path.join(
                    cfg["paths"]["gold_dir"], os.path.basename(rel))
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
