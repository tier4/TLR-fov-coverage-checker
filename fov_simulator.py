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

`run_simulation` returns one `ValidationResult` per (waypoint, candidate
light) pair -- still per-light, not per-waypoint -- because a single
waypoint often has several candidate lights, including redundant heads
for the very same stop line. `compute_point_status` is the second half:
it aggregates a waypoint's candidates into one covered/facing_away/
out_of_fov verdict, using `TrafficLight.group_id` so redundant heads don't
each have to be independently visible.
"""

from __future__ import annotations

import numpy as np

from geometry_calculator import (
    calc_centroid,
    calc_distance_3d,
    calc_heading_yaw,
    calc_resample_by_distance,
    check_fov_inclusion,
    check_light_facing_camera,
    check_light_relevant_to_lane,
    check_target_ahead,
)
from models import CameraSpec, LanePath, Point3D, TrafficLight, ValidationResult

SAMPLE_INTERVAL_M = 1.0
LANE_DIRECTION_THRESHOLD_DEG = 90.0
AHEAD_THRESHOLD_DEG = 90.0
MAX_INHERITANCE_HOPS = 15


def _polyline_length(points: list[Point3D]) -> float:
    return sum(calc_distance_3d(a, b) for a, b in zip(points, points[1:]))


def _build_lane_relevant_tl_ids(lanes: list[LanePath], max_range: float) -> dict[str, set[str] | None]:
    """For each lane, the set of vehicle-signal ids the map's own topology says
    control it -- or None if neither the lane nor any nearby successor has an
    authoritative answer, signaling the caller to fall back to the geometric
    `check_light_relevant_to_lane` heuristic instead.

    A lanelet whose own XML lists `direct_tl_ids` (a `regulatory_element`
    member pointing at a `subtype=traffic_light` relation -- the map
    author's own statement of which signal controls this specific lane) is
    unambiguous: only 90 degrees is a poor test for "is this the same
    intersection" at a skewed intersection, where a genuinely unrelated
    cross-street signal can still end up facing "more than 90 degrees" off
    this lane's heading and slip through as a false candidate. Most
    lanelets don't carry this reference directly though (only ~20% do on
    the bundled Odaiba map, typically just the segment immediately
    approaching the stop line) -- for the rest, this walks forward through
    `next_lane_ids` (the route graph built from shared lanelet endpoints)
    to inherit the reference from the nearest downstream lanelet that has
    one, stopping once accumulated lane length exceeds `max_range` (no
    point inheriting a light so far down the route it's already out of
    detection range) or `MAX_INHERITANCE_HOPS` lanelets deep.
    """
    lanes_by_id = {lane.id: lane for lane in lanes}
    lane_length = {lane.id: _polyline_length(lane.center_line) for lane in lanes}

    result: dict[str, set[str] | None] = {}
    for lane in lanes:
        if lane.direct_tl_ids:
            result[lane.id] = set(lane.direct_tl_ids)
            continue

        visited = {lane.id}
        frontier = [(lane.id, 0.0)]
        found: set[str] | None = None
        for _ in range(MAX_INHERITANCE_HOPS):
            if found is not None or not frontier:
                break
            next_frontier: list[tuple[str, float]] = []
            for cur_id, dist_so_far in frontier:
                cur = lanes_by_id.get(cur_id)
                if cur is None:
                    continue
                for nxt_id in cur.next_lane_ids:
                    if nxt_id in visited:
                        continue
                    visited.add(nxt_id)
                    nxt = lanes_by_id.get(nxt_id)
                    if nxt is None:
                        continue
                    nxt_dist = dist_so_far + lane_length[cur_id]
                    if nxt_dist > max_range:
                        continue
                    if nxt.direct_tl_ids:
                        found = set(nxt.direct_tl_ids)
                        break
                    next_frontier.append((nxt_id, nxt_dist))
                if found is not None:
                    break
            frontier = next_frontier
        result[lane.id] = found

    return result


def run_simulation(
    lanes: list[LanePath],
    traffic_lights: list[TrafficLight],
    camera: CameraSpec = CameraSpec(),
    signal_types: set[str] | None = None,
) -> list[ValidationResult]:
    """Scan every lane's center line at 1m intervals and check whether `camera`
    can detect nearby traffic lights.

    For each waypoint: the camera sits `camera.height` above it, looks
    toward the next waypoint (pitch fixed at 0.0). It is checked against
    every traffic light (optionally restricted to `signal_types`, e.g.
    {"vehicle"}) whose representative position (bulb centroid) is within
    [camera.min_range, camera.max_range] AND that hasn't already been
    passed along the route (see `check_target_ahead`; a light more than 90
    degrees off the direction of travel is behind the vehicle, which isn't
    a meaningful camera-spec gap).

    On top of that range/ahead filter, a vehicle-signal candidate must
    also be relevant to this specific lane. Where the map itself says so
    (`_build_lane_relevant_tl_ids`, using each lanelet's own
    `regulatory_element` reference or one inherited from a nearby
    downstream lanelet), that's authoritative and used as-is -- this is
    what keeps an unrelated cross-street signal at a skewed intersection
    from being evaluated against a lane it was never meant to regulate.
    Lanes with no such map data (and all pedestrian-signal candidates,
    which normally lack a controlling lanelet reference entirely) fall
    back to the geometric heuristic (`check_light_relevant_to_lane`: a
    light facing the same way this lane travels belongs to a parallel
    opposing-direction lane at the same location, not this one).
    Irrelevant candidates are skipped entirely rather than counted as a
    blind spot. A candidate is `is_covered` only if it is both inside the
    camera's FOV cone AND the signal face is oriented toward the camera
    within `camera.facing_tolerance_deg` (lights with unknown facing_yaw
    are never excluded by the facing check).
    """
    if not lanes or not traffic_lights:
        return []

    if signal_types is not None:
        traffic_lights = [tl for tl in traffic_lights if tl.signal_type in signal_types]
    tl_targets = [
        (tl.id, tl.signal_type, tl.group_id, tl.facing_yaw, calc_centroid(tl.bulbs))
        for tl in traffic_lights
        if tl.bulbs
    ]
    if not tl_targets:
        return []
    tl_xyz = np.array([[p.x, p.y, p.z] for _, _, _, _, p in tl_targets], dtype=float)

    lane_relevant_tl_ids = _build_lane_relevant_tl_ids(lanes, camera.max_range)

    results: list[ValidationResult] = []

    for lane in lanes:
        sampled = calc_resample_by_distance(lane.center_line, SAMPLE_INTERVAL_M)
        if len(sampled) < 2:
            continue

        lane_tl_ids = lane_relevant_tl_ids.get(lane.id)
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

            tl_id, signal_type, group_id, facing_yaw, tl_pos = tl_targets[j]
            cam_pos = cam_positions[i]

            if signal_type == "vehicle" and lane_tl_ids is not None:
                if tl_id not in lane_tl_ids:
                    continue
            elif facing_yaw is not None and not check_light_relevant_to_lane(
                tl_facing_yaw=facing_yaw,
                lane_heading=yaw_cache[i],
                threshold_deg=LANE_DIRECTION_THRESHOLD_DEG,
            ):
                continue

            if not check_target_ahead(
                cam_pos=cam_pos,
                cam_yaw=yaw_cache[i],
                target_pos=tl_pos,
                max_angle_diff=AHEAD_THRESHOLD_DEG,
            ):
                continue

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
                    group_id=group_id,
                    distance_m=float(dist[i, j]),
                    in_fov=in_fov,
                    facing_camera=facing_camera,
                    is_covered=in_fov and facing_camera,
                )
            )

    return results


def compute_point_status(results_for_point: list[ValidationResult]) -> str:
    """Aggregate every candidate light at one waypoint (i.e. every
    ValidationResult sharing the same lane_id and point) into a single
    "covered" / "facing_away" / "out_of_fov" status, using per-signal-group
    semantics: redundant traffic light heads sharing the same stop line
    (`TrafficLight.group_id`, e.g. a through light and a turn-arrow light)
    only need one of them visible to count as covered -- seeing one head
    is enough to know the signal state, so requiring every individual head
    to be independently covered overstates how many real gaps there are.

    The point is "covered" only if every distinct group present has at
    least one covered member. Otherwise it reflects the least-bad reason
    among the groups that failed entirely: "facing_away" if any failed
    group at least had a light in FOV (just poorly oriented), else
    "out_of_fov".
    """
    by_group: dict[str, list[ValidationResult]] = {}
    for r in results_for_point:
        by_group.setdefault(r.group_id, []).append(r)

    all_groups_covered = True
    failed_group_has_facing_away = False
    for group_results in by_group.values():
        if any(r.is_covered for r in group_results):
            continue
        all_groups_covered = False
        if any(r.in_fov and not r.facing_camera for r in group_results):
            failed_group_has_facing_away = True

    if all_groups_covered:
        return "covered"
    if failed_group_has_facing_away:
        return "facing_away"
    return "out_of_fov"
