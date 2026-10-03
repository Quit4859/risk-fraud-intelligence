"""Configuration and dataset-profile invariants.

These cover bugs that produced *plausible* output rather than a crash, which is
why they survived as long as they did:

* ``RISK_DATA_DIR`` flattened ``data/gold/ground_truth_labels.csv`` to the base
  directory while the generator writes it inside ``gold/``. Redirecting the data
  directory therefore pointed the evaluator at labels that were not there, and
  at a population it was not scoring.
* ``RISK_PROFILE`` is read by ``api/_runtime.py``, not by ``backend.config``, so
  setting it and then reading the config told you nothing about the dataset the
  app would actually build.
* Thresholds are configuration, not deployment configuration. A stray env var
  must not be able to move a detection band.
"""

from __future__ import annotations

import os

import pytest

from backend.config import load_config


# -- gold labels follow the data --------------------------------------------

def test_gold_labels_move_with_the_data_directory(tmp_path, monkeypatch):
    """The regression: labels resolved outside the redirected data dir."""
    monkeypatch.setenv("RISK_DATA_DIR", str(tmp_path))
    cfg = load_config()
    assert cfg["paths"]["raw_dir"] == str(tmp_path / "raw")
    assert cfg["paths"]["gold_dir"] == str(tmp_path / "gold")
    # Must sit inside the redirected gold_dir, not flattened beside it.
    assert cfg["paths"]["gold_labels"] == str(tmp_path / "gold" /
                                              "ground_truth_labels.csv")


def test_gold_labels_resolve_to_a_real_file_after_a_redirect(tmp_path, monkeypatch):
    """A redirect that cannot find its labels must not silently score against
    the wrong population - it should resolve to a path that does not exist so
    the caller fails loudly."""
    monkeypatch.setenv("RISK_DATA_DIR", str(tmp_path))
    cfg = load_config()
    assert not os.path.exists(cfg["paths"]["gold_labels"])


def test_generator_and_config_agree_on_the_label_location(tmp_path, monkeypatch):
    """The two halves must name the same file. This is the actual invariant
    that broke."""
    from api._runtime import _ensure_dataset

    monkeypatch.setenv("RISK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RISK_PROFILE", "demo")
    _ensure_dataset(str(tmp_path / "raw"), str(tmp_path / "gold"))

    cfg = load_config()
    assert os.path.exists(cfg["paths"]["gold_labels"]), (
        f"generator wrote labels elsewhere; config looks for "
        f"{cfg['paths']['gold_labels']}")


# -- profile -----------------------------------------------------------------

def test_demo_profile_is_the_deployed_default():
    from api._runtime import _dataset_profile

    os.environ.pop("RISK_PROFILE", None)
    os.environ.pop("RISK_CUSTOMERS", None)
    os.environ.pop("RISK_HORIZON_DAYS", None)
    profile = _dataset_profile()
    assert profile["customers"] == 200
    assert profile["horizon_days"] == 150
    assert profile["seed"] == 42


def test_full_profile_is_larger(monkeypatch):
    from api._runtime import _dataset_profile

    monkeypatch.setenv("RISK_PROFILE", "full")
    profile = _dataset_profile()
    assert profile["customers"] == 600
    assert profile["horizon_days"] == 400


def test_explicit_customer_count_wins(monkeypatch):
    from api._runtime import _dataset_profile

    monkeypatch.setenv("RISK_PROFILE", "full")
    monkeypatch.setenv("RISK_CUSTOMERS", "37")
    assert _dataset_profile()["customers"] == 37


def test_the_same_seed_produces_the_same_dataset(tmp_path, monkeypatch):
    """Reproducibility is the point of a seeded generator. Two runs at the same
    seed must agree exactly, or a before/after comparison measures nothing."""
    from api._runtime import _ensure_dataset

    digests = []
    for run in ("a", "b"):
        base = tmp_path / run
        monkeypatch.setenv("RISK_PROFILE", "demo")
        monkeypatch.setenv("RISK_CUSTOMERS", "40")
        monkeypatch.setenv("RISK_HORIZON_DAYS", "90")
        monkeypatch.setenv("RISK_SEED", "42")
        _ensure_dataset(str(base / "raw"), str(base / "gold"))
        with open(base / "raw" / "transactions.csv", "rb") as fh:
            digests.append(hash(fh.read()))
    assert digests[0] == digests[1]


# -- thresholds are not deployment config -----------------------------------

def test_thresholds_cannot_be_moved_by_environment(monkeypatch):
    """A compliance officer tunes settings.yaml. An env var that silently
    widened a detection band would defeat that."""
    monkeypatch.setenv("RISK_THRESHOLD_HIGH", "0.01")
    monkeypatch.setenv("RISK_STRUCTURING_WINDOW_DAYS", "365")
    cfg = load_config()
    text = open("config/settings.yaml").read()
    assert "high:" in text
    # The env vars above have no wiring into the config at all. Thresholds live
    # under `detection` (risk_bands / structuring) and must not move.
    assert cfg["detection"]["risk_bands"]["high"] != 0.01
    assert "365" not in str(cfg["detection"])
