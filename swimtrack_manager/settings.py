from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, TypeVar

from .models import BatchRenameSettings, CueSettings

APP_DIR = Path.home() / ".swimtrack_manager"
SETTINGS_FILE = APP_DIR / "settings.json"

T = TypeVar("T")


def load_settings() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data: dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _filtered_dataclass(cls, data: dict[str, Any]):
    allowed = {field.name for field in cls.__dataclass_fields__.values()}
    filtered = {k: v for k, v in data.items() if k in allowed}
    return cls(**filtered)


def cue_to_dict(settings: CueSettings) -> dict[str, Any]:
    return asdict(settings)


def cue_from_dict(data: dict[str, Any]) -> CueSettings:
    return _filtered_dataclass(CueSettings, data)


def rename_to_dict(settings: BatchRenameSettings) -> dict[str, Any]:
    return asdict(settings)


def rename_from_dict(data: dict[str, Any]) -> BatchRenameSettings:
    return _filtered_dataclass(BatchRenameSettings, data)
