"""
Phase 3 — Model Manager Tests

Tests CatBoost model loading, versioning, and activation. The model
manager ensures only one model is active, supports rollback, and
prevents model swaps while trades are open.

Business context: The trader can upload new model versions as they
retrain on accumulating data. Bad model versions can be rolled back
immediately. Model activation is deferred if positions are open to
avoid mid-trade model changes.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import numpy as np

from alpha_lab.dashboard.model.model_manager import ModelManager

# ── Tests ────────────────────────────────────────────────────────


def test_upload_model(model_dir: Path, catboost_model_path: Path):
    """Uploading a model file creates a version record and copies the file."""
    mgr = ModelManager(model_dir)

    version = mgr.upload_model(catboost_model_path, metrics={"accuracy": 0.534})

    assert version["id"] == 1
    assert version["is_active"] is False
    assert version["metrics"]["accuracy"] == 0.534
    assert isinstance(version["uploaded_at"], datetime)


def test_activate_model(model_dir: Path, catboost_model_path: Path):
    """Activating a model sets is_active=True and loads the model."""
    mgr = ModelManager(model_dir)

    version = mgr.upload_model(catboost_model_path)
    mgr.activate_model(version["id"])

    active = mgr.get_active_version()
    assert active is not None
    assert active["id"] == version["id"]
    assert active["is_active"] is True
    assert mgr.model is not None


def test_load_active_model(model_dir: Path, catboost_model_path: Path):
    """load_active_model() returns a CatBoostClassifier that can predict."""
    from catboost import CatBoostClassifier

    mgr = ModelManager(model_dir)
    version = mgr.upload_model(catboost_model_path)
    mgr.activate_model(version["id"])

    model = mgr.load_active_model()

    assert model is not None
    assert isinstance(model, CatBoostClassifier)

    # Should be able to predict on 3 features
    result = model.predict(np.array([[10.0, 200.0, 0.8]]))
    assert result is not None


def test_no_active_model_returns_none(model_dir: Path):
    """Before any activation, load_active_model() returns None."""
    mgr = ModelManager(model_dir)

    assert mgr.load_active_model() is None
    assert mgr.model is None
    assert mgr.get_active_version() is None


def test_rollback(model_dir: Path, catboost_model_path: Path):
    """Rolling back activates a previous version."""
    mgr = ModelManager(model_dir)

    # Upload two versions (create a copy for the second)
    v1 = mgr.upload_model(catboost_model_path, metrics={"version": "v1"})

    copy_path = catboost_model_path.parent / "copy.cbm"
    shutil.copy2(catboost_model_path, copy_path)
    v2 = mgr.upload_model(copy_path, metrics={"version": "v2"})

    mgr.activate_model(v2["id"])
    assert mgr.get_active_version()["id"] == v2["id"]

    # Rollback to v1
    mgr.rollback(v1["id"])
    assert mgr.get_active_version()["id"] == v1["id"]


def test_only_one_active(model_dir: Path, catboost_model_path: Path):
    """After activation, exactly one model version has is_active=True."""
    mgr = ModelManager(model_dir)

    v1 = mgr.upload_model(catboost_model_path)
    copy_path = catboost_model_path.parent / "copy.cbm"
    shutil.copy2(catboost_model_path, copy_path)
    v2 = mgr.upload_model(copy_path)

    mgr.activate_model(v1["id"])
    mgr.activate_model(v2["id"])

    active_count = sum(1 for v in mgr.get_all_versions() if v["is_active"])
    assert active_count == 1
    assert mgr.get_active_version()["id"] == v2["id"]


def test_get_all_versions(model_dir: Path, catboost_model_path: Path):
    """Returns all uploaded versions with metadata."""
    mgr = ModelManager(model_dir)

    for i in range(3):
        copy_path = catboost_model_path.parent / f"copy_{i}.cbm"
        shutil.copy2(catboost_model_path, copy_path)
        mgr.upload_model(copy_path, metrics={"index": i})

    versions = mgr.get_all_versions()
    assert len(versions) == 3
    assert [v["id"] for v in versions] == [1, 2, 3]


def test_model_metrics_stored(model_dir: Path, catboost_model_path: Path):
    """Metrics dict is stored and retrievable."""
    mgr = ModelManager(model_dir)

    metrics = {"accuracy": 0.534, "folds": [0.51, 0.55, 0.54], "precision": 0.861}
    version = mgr.upload_model(catboost_model_path, metrics=metrics)

    assert version["metrics"]["accuracy"] == 0.534
    assert version["metrics"]["folds"] == [0.51, 0.55, 0.54]
    assert version["metrics"]["precision"] == 0.861


def test_model_file_persisted(model_dir: Path, catboost_model_path: Path):
    """The .cbm file exists at the expected path after upload."""
    mgr = ModelManager(model_dir)

    version = mgr.upload_model(catboost_model_path)

    persisted_path = Path(version["file_path"])
    assert persisted_path.exists()
    assert persisted_path.suffix == ".cbm"
    # File should be in model_dir
    assert persisted_path.parent == model_dir
