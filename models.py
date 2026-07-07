"""Data Models (dataclasses) shared by every module.

Kept dependency-free and immutable so they can cross module boundaries
(parser -> simulator -> visualizer) without hidden state.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Point3D:
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class TrafficLight:
    id: str
    bulbs: list[Point3D]
    signal_type: str = "unknown"  # "vehicle" | "pedestrian" | "unknown"
    facing_yaw: float | None = None  # degrees; direction the signal face points, or None if undeterminable
    group_id: str = ""  # shared by every TrafficLight regulating the same stop line; defaults to `id` if solo


@dataclass(frozen=True)
class LanePath:
    id: str
    center_line: list[Point3D]
    direct_tl_ids: list[str]  # traffic light regulatory_element ids this lanelet's own XML explicitly references
    next_lane_ids: list[str]  # lanelet ids whose left way starts where this one's left way ends


@dataclass(frozen=True)
class CameraSpec:
    """A candidate camera configuration to validate against the map."""

    height: float = 3.0
    fov_h: float = 30.0
    fov_v: float = 17.0
    min_range: float = 50.0
    max_range: float = 200.0
    facing_tolerance_deg: float = 45.0


@dataclass(frozen=True)
class ValidationResult:
    lane_id: str
    point: Point3D
    target_tl_id: str
    signal_type: str
    group_id: str
    distance_m: float
    in_fov: bool
    facing_camera: bool
    is_covered: bool  # in_fov and facing_camera
