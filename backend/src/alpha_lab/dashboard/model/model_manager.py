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
        """Verify the model uses a valid subset of dashboard-computable features.

        Checks that all model features are in FEATURE_COLUMNS (subset ok),
        and that the model outputs exactly 3 classes.
        """
        import logging as _log
        import numpy as np

        from alpha_lab.dashboard.model import CLASS_NAMES, FEATURE_COLUMNS

        _logger = _log.getLogger(__name__)
        expected_classes = len(CLASS_NAMES)
        allowed_features = set(FEATURE_COLUMNS)

        # Determine model feature count from feature_names_ or try progressively
        model_feature_names = getattr(model, "feature_names_", None)

        if model_feature_names is not None:
            model_features = list(model_feature_names)
            n_features = len(model_features)

            # CatBoost assigns numeric names ('0', '1', '2') when trained
            # without explicit feature names — treat as unnamed
            is_numeric_default = all(f.isdigit() for f in model_features)

            if not is_numeric_default:
                # Verify all model features are in the allowed set
                unknown = [f for f in model_features if f not in allowed_features]
                if unknown:
                    raise ValueError(
                        f"Model uses features not computable by dashboard: {unknown}. "
                        f"Allowed: {FEATURE_COLUMNS}"
                    )
                _logger.info(
                    "Model contract: %d named features (%s), validating...",
                    n_features, ", ".join(model_features),
                )
            else:
                _logger.warning(
                    "Model has numeric feature names (%d features) — "
                    "cannot verify feature name contract. Only count validated.",
                    n_features,
                )
        else:
            # No feature names — try with max feature count
            n_features = len(FEATURE_COLUMNS)
            _logger.warning(
                "Model has no feature_names_ — trying with %d features",
                n_features,
            )

        # Check prediction shape with dummy input
        dummy = np.zeros((1, n_features))
        try:
            proba = model.predict_proba(dummy)
        except Exception as exc:
            raise ValueError(
                f"Model failed prediction with {n_features} features: {exc}"
            ) from exc

        actual_classes = proba.shape[1]
        if actual_classes != expected_classes:
            raise ValueError(
                f"Model outputs {actual_classes} classes, "
                f"expected {expected_classes} ({list(CLASS_NAMES.values())})"
            )
        else:
            _logger.warning(
                "Model has no feature_names_ attribute — cannot verify "
                "feature name/order contract. Only count validated."
            )
