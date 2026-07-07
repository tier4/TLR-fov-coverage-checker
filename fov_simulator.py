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
LANE_DIRECTION_THRESHOLD_DEG = 120.0
AHEAD_THRESHOLD_DEG = 90.0
MAX_INHERITANCE_HOPS = 15
STOP_LINE_PROXIMITY_M = 30.0


def _polyline_length(points: list[Point3D]) -> float:
    return sum(calc_distance_3d(a, b) for a, b in zip(points, points[1:]))


def _nearby_group_by_lane_end(
    lanes: list[LanePath],
    group_positions: dict[str, Point3D],
    threshold: float,
) -> dict[str, str]:
    """lane_id -> group_id of the nearest stop line to that lane's own end
    point, for lanes ending within `threshold` of one. Vectorized: with
    ~5,000 lanes and ~900 groups this is a ~4.5M-cell distance matrix,
    fast in numpy but a lot of wasted work as nested Python loops.
    """
    if not group_positions:
        return {}
    group_ids = list(group_positions)
    group_xyz = np.array([[group_positions[g].x, group_positions[g].y, group_positions[g].z] for g in group_ids])
    endpoints = np.array([[lane.center_line[-1].x, lane.center_line[-1].y, lane.center_line[-1].z] for lane in lanes])

    diff = endpoints[:, None, :] - group_xyz[None, :, :]
    dist = np.sqrt(np.sum(diff**2, axis=-1))
    nearest_idx = np.argmin(dist, axis=1)
    nearest_dist = dist[np.arange(len(lanes)), nearest_idx]

    return {
        lane.id: group_ids[nearest_idx[i]]
        for i, lane in enumerate(lanes)
        if nearest_dist[i] <= threshold
    }


def _build_lane_relevant_tl_ids(
    lanes: list[LanePath],
    traffic_lights: list[TrafficLight],
    max_range: float,
) -> dict[str, set[str] | None]:
    """For each lane, the set of vehicle-signal ids the map's own topology (or
    geometry, as a second resort) says control it -- or None if nothing
    authoritative is available anywhere within reach, signaling the caller
    to fall back to the (least reliable) angle-based heuristic instead.

    Three levels, most authoritative first, tried at the lane itself and
    then at each successor walked through `next_lane_ids` (bounded by
    `max_range` of cumulative lane length and `MAX_INHERITANCE_HOPS`):

    1. `direct_tl_ids` -- a lanelet whose own XML lists a
       `regulatory_element` member pointing at a `subtype=traffic_light`
       relation. The map author's own statement of which signal controls
       this specific lane. Only ~20% of lanelets carry this themselves on
       the bundled Odaiba map (typically just the segment immediately
       approaching the stop line).
    2. Proximity to a stop line -- for a lane with no direct reference,
       whether *any* traffic light's `stop_line_pos` sits within
       `STOP_LINE_PROXIMITY_M` of that lane's own end point. Lanelet
       connectivity graphs are frequently incomplete right at
       intersections (a lane can dead-end, or run out of `next_lane_ids`
       before reaching anywhere tagged, well short of a real physical
       dead end) -- confirmed on the bundled map: a lane whose mapped path
       ends 10.5m from an unrelated-looking stop line turned out to be
       that stop line's own approach, with no lanelet-graph path to it at
       all. Skipped when the nearest stop line isn't close and unambiguous
       (ties between multiple similarly-close stop lines at a complex
       intersection are common and not safely resolved by distance alone)
       -- level 3 still applies in that case.
    3. The geometric heuristic (`check_light_relevant_to_lane`), applied by
       the caller when this function returns None -- least reliable, since
       a skewed (non-square) intersection can make a genuinely unrelated
       cross-street signal face "more than `LANE_DIRECTION_THRESHOLD_DEG`
       off this lane's heading by coincidence.
    """
    lanes_by_id = {lane.id: lane for lane in lanes}
    lane_length = {lane.id: _polyline_length(lane.center_line) for lane in lanes}

    group_members: dict[str, list[str]] = {}
    group_positions: dict[str, Point3D] = {}
    for tl in traffic_lights:
        group_members.setdefault(tl.group_id, []).append(tl.id)
        if tl.stop_line_pos is not None and tl.group_id not in group_positions:
            group_positions[tl.group_id] = tl.stop_line_pos
    nearby_group_by_lane = _nearby_group_by_lane_end(lanes, group_positions, STOP_LINE_PROXIMITY_M)

    def resolve(lane_id: str) -> set[str] | None:
        lane = lanes_by_id[lane_id]
        if lane.direct_tl_ids:
            return set(lane.direct_tl_ids)
        nearby_group = nearby_group_by_lane.get(lane_id)
        if nearby_group is not None:
            return set(group_members[nearby_group])
        return None

    result: dict[str, set[str] | None] = {}
    for lane in lanes:
        found = resolve(lane.id)
        if found is None:
            visited = {lane.id}
            frontier = [(lane.id, 0.0)]
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
                        nxt_found = resolve(nxt_id)
                        if nxt_found is not None:
                            found = nxt_found
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
    (`_build_lane_relevant_tl_ids`: a direct `regulatory_element`
    reference, one inherited from a nearby downstream lanelet, or -- when
    the lanelet connectivity graph itself has a gap right at an
    intersection, which happens -- a stop line sitting suspiciously close
    to where this lane's mapped path ends), that's authoritative and used
    as-is. This is what keeps an unrelated cross-street signal at a skewed
    intersection from being evaluated against a lane it was never meant to
    regulate. Lanes with no such map data at all (and all pedestrian-signal
    candidates, which normally lack a controlling lanelet reference
    entirely) fall back to the geometric heuristic
    (`check_light_relevant_to_lane`: a light facing the same way this lane
    travels belongs to a parallel opposing-direction lane at the same
    location, not this one) -- the least reliable option, since a skewed
    intersection can still let a genuinely unrelated signal slip through.
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
        # heads fall back to the pooled centroid as a single pseudo-head so
        # a TrafficLight built without per-head data behaves as before
        (
            tl.id,
            tl.signal_type,
            tl.group_id,
            tl.facing_yaw,
            calc_centroid(tl.bulbs),
            [h.pos for h in tl.heads] or [calc_centroid(tl.bulbs)],
        )
        for tl in traffic_lights
        if tl.bulbs
    ]
    if not tl_targets:
        return []
    tl_xyz = np.array([[p.x, p.y, p.z] for _, _, _, _, p, _ in tl_targets], dtype=float)

    lane_relevant_tl_ids = _build_lane_relevant_tl_ids(lanes, traffic_lights, camera.max_range)

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

            tl_id, signal_type, group_id, facing_yaw, tl_pos, head_positions = tl_targets[j]
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

            # Judged per physical head, not at the pooled centroid: a
            # regulatory element often bundles 2-4 housings meters apart,
            # and the centroid can sit where no housing exists (in-FOV
            # judged there was measurably wrong at FOV edges). Seeing any
            # one head is seeing the light; heads_visible/heads_total keep
            # the finer "how many of them" grading for display.
            heads_visible = 0
            any_head_in_fov = False
            any_head_facing = False
            for head_pos in head_positions:
                head_in_fov = check_fov_inclusion(
                    cam_pos=cam_pos,
                    cam_yaw=yaw_cache[i],
                    cam_pitch=0.0,
                    target_pos=head_pos,
                    fov_h=camera.fov_h,
                    fov_v=camera.fov_v,
                )
                head_facing = (
                    True
                    if facing_yaw is None
                    else check_light_facing_camera(
                        tl_pos=head_pos,
                        tl_facing_yaw=facing_yaw,
                        cam_pos=cam_pos,
                        max_angle_diff=camera.facing_tolerance_deg,
                    )
                )
                any_head_in_fov = any_head_in_fov or head_in_fov
                any_head_facing = any_head_facing or head_facing
                if head_in_fov and head_facing:
                    heads_visible += 1

            is_covered = heads_visible >= 1
            # facing_camera keeps its role in status classification
            # (in_fov and not facing_camera => "facing_away"): while any
            # head is in FOV it answers "was the light readable there";
            # out of FOV it stays purely informational.
            facing_camera = is_covered if any_head_in_fov else any_head_facing

            results.append(
                ValidationResult(
                    lane_id=lane.id,
                    point=sampled[i],
                    target_tl_id=tl_id,
                    signal_type=signal_type,
                    group_id=group_id,
                    distance_m=float(dist[i, j]),
                    in_fov=any_head_in_fov,
                    facing_camera=facing_camera,
                    is_covered=is_covered,
                    heads_total=len(head_positions),
                    heads_visible=heads_visible,
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


def compute_point_head_counts(results_for_point: list[ValidationResult]) -> tuple[int, int]:
    """The finer-grained companion to `compute_point_status`: of the signal
    heads the waypoint is supposed to see, how many are actually visible?

    Sums heads_visible/heads_total per group (a group's heads span every
    redundant light sharing its stop line) and returns the (visible,
    total) of the *worst-ratio* group -- the point's weakest link, same
    per-group framing as the status. A covered point can still be
    fragile: (1, 6) means one visible head out of six is doing all the
    work, while (6, 6) has full redundancy. Ties on ratio resolve to the
    larger total (more heads missing in absolute terms).
    """
    by_group: dict[str, list[ValidationResult]] = {}
    for r in results_for_point:
        by_group.setdefault(r.group_id, []).append(r)

    worst: tuple[int, int] | None = None
    for group_results in by_group.values():
        visible = sum(r.heads_visible for r in group_results)
        total = sum(r.heads_total for r in group_results)
        if total == 0:
            continue
        if worst is None or visible * worst[1] < worst[0] * total or (
            visible * worst[1] == worst[0] * total and total > worst[1]
        ):
            worst = (visible, total)
    return worst if worst is not None else (0, 0)


def compute_point_min_visible(results_for_point: list[ValidationResult]) -> int:
    """The waypoint's redundancy number: the minimum *absolute* count of
    visible heads across its signal groups.

    Deliberately not the same thing as `compute_point_head_counts`'
    worst *ratio*: a group with 1 of 1 heads visible is 100% covered but
    has zero redundancy -- lose that one head (occlusion, dirt, glare)
    and the signal state is gone. 0 means some group is entirely
    invisible (the point is not covered); 1 means covered with no
    margin; 2+ means genuinely redundant observation of every group.
    """
    by_group: dict[str, int] = {}
    for r in results_for_point:
        by_group[r.group_id] = by_group.get(r.group_id, 0) + r.heads_visible
    return min(by_group.values()) if by_group else 0
