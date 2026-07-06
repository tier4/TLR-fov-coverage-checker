"""Module C: FOV Simulator.

Integrates Module A (geometry) and Module B's output data (LanePath /
TrafficLight instances) to run the coverage check. Knows nothing about
XML parsing or plotting.

This is the "given a camera spec, can it detect the signals I care
about" validation harness: a CameraSpec bundles mount height, FOV,
detection range and facing-angle tolerance, and `run_simulation` can be
restricted to a subset of signal_types (e.g. only "vehicle" or only
"pedestrian") so different camera configurations can be compared for
the signal population that actually matters for a given use case.

Performance note: a real Lanelet2 map easily has thousands of lanes and
hundreds of traffic lights. Checking every 1m waypoint against every
traffic light one-by-one in pure Python does not finish in a reasonable
time, so the expensive min/max-range distance pre-filter is vectorized
with numpy per lane; the pure `check_fov_inclusion` /
`check_light_facing_camera` functions are then only called for the
(much smaller) set of candidates that survive it.
"""

from __future__ import annotations

import numpy as np

from geometry_calculator import (
    calc_centroid,
    calc_heading_yaw,
    calc_resample_by_distance,
    check_fov_inclusion,
    check_light_facing_camera,
)
from models import CameraSpec, LanePath, Point3D, TrafficLight, ValidationResult

SAMPLE_INTERVAL_M = 1.0


def run_simulation(
    lanes: list[LanePath],
    traffic_lights: list[TrafficLight],
    camera: CameraSpec = CameraSpec(),
    signal_types: set[str] | None = None,
) -> list[ValidationResult]:
    """Scan every lane's center line at 1m intervals and check whether `camera`
    can detect nearby traffic lights.

    For each waypoint: the camera sits `camera.height` above it, looks
    toward the next waypoint (pitch fixed at 0.0), and is checked against
    every traffic light (optionally restricted to `signal_types`, e.g.
    {"vehicle"}) whose representative position (bulb centroid) is within
    [camera.min_range, camera.max_range]. A candidate is `is_covered` only
    if it is both inside the camera's FOV cone AND the signal face is
    oriented toward the camera within `camera.facing_tolerance_deg`
    (lights with unknown facing_yaw are never excluded by the facing check).
    """
    if not lanes or not traffic_lights:
        return []

    if signal_types is not None:
        traffic_lights = [tl for tl in traffic_lights if tl.signal_type in signal_types]
    tl_targets = [(tl.id, tl.signal_type, tl.facing_yaw, calc_centroid(tl.bulbs)) for tl in traffic_lights if tl.bulbs]
    if not tl_targets:
        return []
    tl_xyz = np.array([[p.x, p.y, p.z] for _, _, _, p in tl_targets], dtype=float)

    results: list[ValidationResult] = []

    for lane in lanes:
        sampled = calc_resample_by_distance(lane.center_line, SAMPLE_INTERVAL_M)
        if len(sampled) < 2:
            continue

        cam_positions = [Point3D(p.x, p.y, p.z + camera.height) for p in sampled]
        cam_xyz = np.array([[p.x, p.y, p.z] for p in cam_positions], dtype=float)

        diff = cam_xyz[:, None, :] - tl_xyz[None, :, :]
        dist = np.sqrt(np.sum(diff**2, axis=-1))
        candidate_i, candidate_j = np.where((dist >= camera.min_range) & (dist <= camera.max_range))
        if candidate_i.size == 0:
            continue

        yaw_cache: dict[int, float] = {}
        last_idx = len(sampled) - 1
        for i, j in zip(candidate_i.tolist(), candidate_j.tolist()):
            if i not in yaw_cache:
                if i < last_idx:
                    yaw_cache[i] = calc_heading_yaw(sampled[i], sampled[i + 1])
                else:
                    yaw_cache[i] = calc_heading_yaw(sampled[i - 1], sampled[i])

            tl_id, signal_type, facing_yaw, tl_pos = tl_targets[j]
            cam_pos = cam_positions[i]

            in_fov = check_fov_inclusion(
                cam_pos=cam_pos,
                cam_yaw=yaw_cache[i],
                cam_pitch=0.0,
                target_pos=tl_pos,
                fov_h=camera.fov_h,
                fov_v=camera.fov_v,
            )
            facing_camera = (
                True
                if facing_yaw is None
                else check_light_facing_camera(
                    tl_pos=tl_pos,
                    tl_facing_yaw=facing_yaw,
                    cam_pos=cam_pos,
                    max_angle_diff=camera.facing_tolerance_deg,
                )
            )

            results.append(
                ValidationResult(
                    lane_id=lane.id,
                    point=sampled[i],
                    target_tl_id=tl_id,
                    signal_type=signal_type,
                    distance_m=float(dist[i, j]),
                    in_fov=in_fov,
                    facing_camera=facing_camera,
                    is_covered=in_fov and facing_camera,
                )
            )

    return results
