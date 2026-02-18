"""Persistent per-model generation profile storage."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ModelProfileStore:
    """Thread-safe JSON-backed store for per-model profiles."""

    def __init__(self, path: str):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._loaded = False
        self._data: dict[str, Any] = {"version": 1, "models": {}}

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
            models = raw.get("models")
            if isinstance(models, dict):
                self._data = {"version": 1, "models": models}
        self._loaded = True

    def _persist(self) -> None:
        temp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
        payload = json.dumps(self._data, ensure_ascii=True, indent=2, sort_keys=True)
        temp_path.write_text(payload, encoding="utf-8")
        temp_path.replace(self._path)

    def get_profile(self, model_id: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            record = self._data["models"].get(model_id, {})
            values = record.get("values", {})
            if isinstance(values, dict):
                return dict(values)
            return {}

    def set_profile(self, model_id: str, values: dict[str, Any]) -> dict[str, Any]:
        clean_values = {k: v for k, v in values.items() if v is not None}
        with self._lock:
            self._ensure_loaded()
            if clean_values:
                self._data["models"][model_id] = {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "values": clean_values,
                }
            else:
                self._data["models"].pop(model_id, None)
            self._persist()
            return dict(clean_values)

    def patch_profile(self, model_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            current_record = self._data["models"].get(model_id, {})
            current_values = current_record.get("values", {})
            if not isinstance(current_values, dict):
                current_values = {}
            merged = dict(current_values)
            for key, value in updates.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            if merged:
                self._data["models"][model_id] = {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "values": merged,
                }
            else:
                self._data["models"].pop(model_id, None)
            self._persist()
            return dict(merged)
