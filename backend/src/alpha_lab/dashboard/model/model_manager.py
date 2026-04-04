"""
Model Manager — CatBoost model lifecycle management.

Loads .cbm model files, manages versions (active, historical), handles
uploads and rollbacks. Only one model is active at a time.

Model files are stored on disk in the configured model directory.
Version metadata is maintained in-memory (DB persistence is wired
at the integration level).
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path


class ModelManager:
    """Manages CatBoost model lifecycle — loading, versioning, activation.

    Stores model metadata in-memory and model files on disk.
    Only one model is active at a time.
    """

    def __init__(self, model_dir: Path) -> None:
        self._model_dir = model_dir
        self._model_dir.mkdir(parents=True, exist_ok=True)
        self._versions: list[dict] = []
        self._active_id: int | None = None
        self._model: object | None = None
        self._next_id: int = 1

    def upload_model(
        self,
        file_path: Path,
        metrics: dict | None = None,
    ) -> dict:
        """Upload a model file. Copies to model_dir, creates version record.

        Does NOT activate the model — call activate_model() separately.
        """
        version_id = self._next_id
        self._next_id += 1

        now = datetime.now(UTC)
        dest_name = f"v{version_id}_{now.strftime('%Y%m%d_%H%M%S')}.cbm"
        dest_path = self._model_dir / dest_name
        shutil.copy2(file_path, dest_path)

        version = {
            "id": version_id,
            "version": f"v{version_id}",
            "file_path": str(dest_path),
            "is_active": False,
            "metrics": metrics,
            "uploaded_at": now,
            "activated_at": None,
        }
        self._versions.append(version)
        return version

    def activate_model(self, version_id: int) -> None:
        """Activate a model version. Deactivates any previously active model."""
        target = self._find_version(version_id)

        # Deactivate all
        for v in self._versions:
            v["is_active"] = False
            if v["id"] != version_id:
                v["activated_at"] = v.get("activated_at")

        # Activate target
        target["is_active"] = True
        target["activated_at"] = datetime.now(UTC)
        self._active_id = version_id

        # Load and validate the model
        self._model = self._load_from_file(target["file_path"])
        self.validate_model_contract(self._model)

    def load_active_model(self) -> object | None:
        """Load and return the active model. Returns None if none active."""
        active = self.get_active_version()
        if active is None:
            return None

        self._model = self._load_from_file(active["file_path"])
        return self._model

    def rollback(self, version_id: int) -> None:
        """Rollback to a specific previous model version."""
        self.activate_model(version_id)

    def get_active_version(self) -> dict | None:
        """Return the currently active version record, or None."""
        for v in self._versions:
            if v["is_active"]:
                return v
        return None

    def get_all_versions(self) -> list[dict]:
        """Return all uploaded version records."""
        return list(self._versions)

    @property
    def model(self) -> object | None:
        """The currently loaded model instance."""
        return self._model

    def _find_version(self, version_id: int) -> dict:
        """Find a version by ID. Raises ValueError if not found."""
        for v in self._versions:
            if v["id"] == version_id:
                return v
        raise ValueError(f"Model version {version_id} not found")

    @staticmethod
    def _load_from_file(file_path: str) -> object:
        """Load a CatBoost model from a .cbm file."""
        from catboost import CatBoostClassifier

        model = CatBoostClassifier()
        model.load_model(file_path)
        return model

    @staticmethod
    def validate_model_contract(model: object) -> None:
        """Verify the model matches the expected 3-feature / 3-class contract.

        Raises ValueError if the model's feature count or class count
        doesn't match the dashboard contract.
        """
        import numpy as np

        from alpha_lab.dashboard.model import CLASS_NAMES, FEATURE_COLUMNS

        expected_features = len(FEATURE_COLUMNS)
        expected_classes = len(CLASS_NAMES)

        # Check prediction shape with a dummy input
        dummy = np.zeros((1, expected_features))
        try:
            proba = model.predict_proba(dummy)
        except Exception as exc:
            raise ValueError(
                f"Model failed prediction with {expected_features} features: {exc}"
            ) from exc

        actual_classes = proba.shape[1]
        if actual_classes != expected_classes:
            raise ValueError(
                f"Model outputs {actual_classes} classes, "
                f"expected {expected_classes} ({list(CLASS_NAMES.values())})"
            )
