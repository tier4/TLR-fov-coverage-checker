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
    blind_only: bool = False
    point_size: float = 6.0
    cameras: tuple[CameraSpec, ...] = (_DEFAULT_CAMERA,)

    @property
    def camera(self) -> CameraSpec:
        """The first (or only) camera -- single-camera call sites' shorthand."""
        return self.cameras[0]


def _parse_camera(camera_raw: dict, default_name: str) -> CameraSpec:
    unknown_keys = set(camera_raw) - set(CameraSpec.__dataclass_fields__)
    if unknown_keys:
        raise ValueError(f"Unknown camera config key(s): {sorted(unknown_keys)}")
    camera = replace(_DEFAULT_CAMERA, **camera_raw)
    if "name" not in camera_raw:
        camera = replace(camera, name=default_name)
    return camera


def load_config(yaml_string: str) -> AppConfig:
    """Parse a YAML config string into an AppConfig.

    Cameras come from either `cameras:` (a list -- the multi-camera rig
    form; each entry may carry its own name/fov/range/yaw_offset/...) or
    the original single `camera:` mapping, which stays supported as the
    one-camera shorthand. Specifying both is ambiguous and rejected.
    Any field may be omitted; missing camera keys fall back to
    CameraSpec's own defaults, and a missing `signal_type` defaults to
    "both". Unknown camera keys or an invalid signal_type raise
    ValueError up front, rather than silently ignoring a typo'd setting.
    """
    raw = yaml.safe_load(yaml_string) or {}

    if raw.get("camera") and raw.get("cameras"):
        raise ValueError("Config has both `camera:` and `cameras:` -- use one or the other")

    cameras_raw = raw.get("cameras")
    if cameras_raw:
        if not isinstance(cameras_raw, list):
            raise ValueError("`cameras:` must be a list of camera mappings")
        cameras = tuple(
            _parse_camera(entry or {}, default_name=f"camera{i + 1}") for i, entry in enumerate(cameras_raw)
        )
        names = [c.name for c in cameras]
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate camera name(s) in `cameras:`: {sorted(n for n in names if names.count(n) > 1)}")
    else:
        cameras = (_parse_camera(raw.get("camera") or {}, default_name="camera"),)

    signal_type = raw.get("signal_type", "both")
    if signal_type not in _VALID_SIGNAL_TYPES:
        raise ValueError(f"Invalid signal_type {signal_type!r}, expected one of {_VALID_SIGNAL_TYPES}")

    return AppConfig(
        map_path=raw.get("map"),
        output_path=raw.get("output"),
        signal_type=signal_type,
        blind_only=bool(raw.get("blind_only", False)),
        point_size=float(raw.get("point_size", 6.0)),
        cameras=cameras,
    )
