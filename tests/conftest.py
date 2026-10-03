"""Shared pytest fixtures.

The suite runs against the *real* detector, guardrail and filing code on a real
generated dataset. Nothing is mocked, because the properties under test are
exactly the ones a mock would hide: that a detector can name its evidence, that
a guardrail blocks, that four-eyes refuses.

Speed comes from generating one small dataset per session and reusing the
warehouse. The dataset is written to a temporary directory so a test run can
never clobber the deployed artefacts.
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: The deployed demo profile. The suite deliberately runs the *same* dataset the
#: app serves, because several contracts only hold at realistic prevalence: at
#: 120 customers the generator plants so few mule victims that no shared-device
#: cluster forms, and the "which mule clusters are active" example correctly
#: abstains for want of data. Testing a smaller population would test a
#: configuration nobody deploys.
TEST_CUSTOMERS = 200
TEST_HORIZON_DAYS = 150


@pytest.fixture(scope="session")
def tmp_root():
    path = tempfile.mkdtemp(prefix="risk-copilot-tests-")
    yield path
    import shutil
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session")
def dataset(tmp_root):
    """Generate the synthetic dataset once and return its paths."""
    from scripts import generate_synthetic_data as gen

    raw_dir = os.path.join(tmp_root, "raw")
    gold_dir = os.path.join(tmp_root, "gold")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(gold_dir, exist_ok=True)
    gen.main([
        "--customers", str(TEST_CUSTOMERS),
        "--horizon-days", str(TEST_HORIZON_DAYS),
        "--seed", "42",
        "--out", raw_dir,
        "--gold-dir", gold_dir,
    ])
    return {"raw_dir": raw_dir, "gold_dir": gold_dir, "root": tmp_root}


@pytest.fixture(scope="session")
def labels(dataset):
    """customer_id -> ground truth row."""
    import csv
    path = os.path.join(dataset["gold_dir"], "ground_truth_labels.csv")
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return {r["customer_id"]: r for r in csv.DictReader(fh)}


@pytest.fixture(scope="session")
def config(tmp_root, dataset):
    """Base config with artefact paths redirected into the temp directory."""
    from backend.config import load_config

    cfg = copy.deepcopy(load_config())
    cfg["paths"] = dict(cfg["paths"])
    for key in ("warehouse_db", "audit_log", "evidence_dir", "reports_dir"):
        cfg["paths"][key] = os.path.join(tmp_root, cfg["paths"][key])
    cfg["paths"]["raw_dir"] = dataset["raw_dir"]
    cfg["paths"]["gold_dir"] = dataset["gold_dir"]
    return cfg


@pytest.fixture(scope="session")
def warehouse(dataset, config):
    """Warehouse with raw data, corpus and semantic views loaded."""
    from backend.agents.retrieval import RetrievalService
    from backend.warehouse import Warehouse

    wh = Warehouse(db_path=os.path.join(dataset["root"], "test.db"), config=config)
    wh.load_raw_dir(dataset["raw_dir"])
    RetrievalService(wh, config=config).load()
    wh.load_sql_file(os.path.join(ROOT, "sql", "04_semantic_views.sql"))
    wh.execute("DELETE FROM mule_clusters")
    wh.execute("INSERT INTO mule_clusters SELECT * FROM sem_mule_network_clusters")
    yield wh
    wh.close()


@pytest.fixture(scope="session")
def copilot(warehouse, config):
    """A fully wired copilot, shared across tests that only read."""
    from backend.orchestration.copilot import RiskCopilot

    cp = RiskCopilot(warehouse=warehouse, config=config)
    yield cp


@pytest.fixture
def fresh_copilot(warehouse, config):
    """A copilot with a private, fully-loaded copy of the warehouse.

    Copying the session database (rather than regenerating or re-running DDL)
    gives mutating tests real data, real views and real isolation, and keeps the
    per-test cost to one file copy.
    """
    import shutil

    from backend.orchestration.copilot import RiskCopilot
    from backend.warehouse import Warehouse

    path = warehouse.db_path + ".mutate"
    shutil.copyfile(warehouse.db_path, path)
    wh = Warehouse(db_path=path, config=config)
    cp = RiskCopilot(warehouse=wh, config=config)
    yield cp
    wh.close()
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


@pytest.fixture(scope="session")
def detector(warehouse, config):
    """The detector portfolio on its own, for per-typology assertions."""
    from backend.agents.fraud_detector import FraudDetectorAgent

    return FraudDetectorAgent(warehouse, config)


@pytest.fixture(scope="session")
def guardrails(config):
    from backend.orchestration.guardrails import GuardrailEngine

    return GuardrailEngine(config)


@pytest.fixture(scope="session")
def retrieval(warehouse, config):
    from backend.agents.retrieval import RetrievalService

    return RetrievalService(warehouse, config=config).load()
