"""Config Loader.

Pure parsing of a YAML config string into typed settings -- no file I/O or
CLI concerns here, so it is unit-testable from an in-memory YAML string,
the same pattern used for Module B's XML parsing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import yaml

from models import CameraSpec

_VALID_SIGNAL_TYPES = ("vehicle", "pedestrian", "both")
_DEFAULT_CAMERA = CameraSpec()


@dataclass(frozen=True)
class AppConfig:
    map_path: str | None = None
    output_path: str | None = None
    signal_type: str = "both"
    camera: CameraSpec = _DEFAULT_CAMERA


def load_config(yaml_string: str) -> AppConfig:
    """Parse a YAML config string into an AppConfig.

    Any field may be omitted; missing `camera.*` keys fall back to
    CameraSpec's own defaults, and a missing `signal_type` defaults to
    "both". Unknown camera keys or an invalid signal_type raise ValueError
    up front, rather than silently ignoring a typo'd setting.
    """
    raw = yaml.safe_load(yaml_string) or {}

    camera_raw = raw.get("camera") or {}
    unknown_keys = set(camera_raw) - set(CameraSpec.__dataclass_fields__)
    if unknown_keys:
        raise ValueError(f"Unknown camera config key(s): {sorted(unknown_keys)}")
    camera = replace(_DEFAULT_CAMERA, **camera_raw)

    signal_type = raw.get("signal_type", "both")
    if signal_type not in _VALID_SIGNAL_TYPES:
        raise ValueError(f"Invalid signal_type {signal_type!r}, expected one of {_VALID_SIGNAL_TYPES}")

    return AppConfig(
        map_path=raw.get("map"),
        output_path=raw.get("output"),
        signal_type=signal_type,
        camera=camera,
    )
