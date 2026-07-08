"""Module C tests use hand-built LanePath/TrafficLight fixtures (no XML
parsing involved) to keep the simulator's own logic -- range filtering,
signal_type filtering, and combining in_fov/facing_camera into
is_covered -- independently testable from Module B.
"""

from fov_simulator import (
    _build_lane_relevant_tl_ids,
    compute_point_head_counts,
    compute_point_min_visible,
    compute_point_status,
    run_simulation,
)
from models import CameraSpec, LanePath, Point3D, SignalHead, TrafficLight, ValidationResult

STRAIGHT_LANE = LanePath(
    id="lane-1",
    center_line=[Point3D(x, 0.0, 0.0) for x in range(0, 201, 20)],
    direct_tl_ids=[],
    next_lane_ids=[],
)

# entirely east of x=100 -- a light at x=100 is behind every waypoint here,
# since this lane (like STRAIGHT_LANE) still travels east (cam_yaw=0).
TRAILING_LANE = LanePath(
    id="lane-2",
    center_line=[Point3D(x, 0.0, 0.0) for x in range(150, 201, 10)],
    direct_tl_ids=[],
    next_lane_ids=[],
)


def test_run_simulation_empty_inputs_return_empty():
    assert run_simulation([], []) == []
    assert run_simulation([STRAIGHT_LANE], []) == []
    assert run_simulation([], [TrafficLight(id="tl-1", bulbs=[Point3D(100, 0, 5)])]) == []


def test_run_simulation_respects_min_and_max_range():
    # target sits 100m ahead of the lane -- well inside [50, 200]
    tl = TrafficLight(id="tl-near", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle")
    camera = CameraSpec(min_range=50.0, max_range=200.0)
    results = run_simulation([STRAIGHT_LANE], [tl], camera=camera)
    assert results
    assert all(49.0 <= r.distance_m <= 201.0 for r in results)

    # tighten the window to exclude every waypoint
    camera_tight = CameraSpec(min_range=500.0, max_range=600.0)
    assert run_simulation([STRAIGHT_LANE], [tl], camera=camera_tight) == []


def test_run_simulation_filters_by_signal_type():
    vehicle_tl = TrafficLight(id="veh", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle")
    pedestrian_tl = TrafficLight(id="ped", bulbs=[Point3D(100.0, 5.0, 5.0)], signal_type="pedestrian")

    only_vehicle = run_simulation([STRAIGHT_LANE], [vehicle_tl, pedestrian_tl], signal_types={"vehicle"})
    assert only_vehicle
    assert all(r.target_tl_id == "veh" for r in only_vehicle)

    only_pedestrian = run_simulation([STRAIGHT_LANE], [vehicle_tl, pedestrian_tl], signal_types={"pedestrian"})
    assert only_pedestrian
    assert all(r.target_tl_id == "ped" for r in only_pedestrian)


def test_run_simulation_marks_covered_only_when_in_fov_and_facing_camera():
    # light sits dead ahead of the lane and faces back down it (west, 180deg)
    # -> a car driving east toward it should find it covered.
    facing_toward_lane = TrafficLight(
        id="facing-lane", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=180.0
    )
    results = run_simulation([STRAIGHT_LANE], [facing_toward_lane])
    covered = [r for r in results if r.target_tl_id == "facing-lane"]
    assert covered
    assert any(r.is_covered for r in covered)

    # same position, facing_yaw=130deg: still >120deg from the lane's 0deg
    # heading (i.e. still plausibly meant for this lane, not the opposing
    # one) but past the default 45deg facing_tolerance_deg of straight-on
    # -> geometrically in FOV, but too far off-axis to read.
    facing_off_tolerance = TrafficLight(
        id="facing-off-tolerance", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=130.0
    )
    results_off = run_simulation([STRAIGHT_LANE], [facing_off_tolerance])
    off = [r for r in results_off if r.target_tl_id == "facing-off-tolerance"]
    assert off
    assert all(not r.is_covered for r in off)
    assert any(r.in_fov and not r.facing_camera for r in off)


def test_run_simulation_excludes_lights_facing_same_direction_as_lane():
    # facing_yaw=0deg matches the lane's own 0deg heading exactly -- this
    # light shines the same way the lane travels, i.e. it belongs to a
    # parallel lane going the opposite direction at the same location, not
    # this one. It should be skipped entirely, not merely marked uncovered.
    facing_same_way = TrafficLight(
        id="belongs-to-other-direction", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=0.0
    )
    results = run_simulation([STRAIGHT_LANE], [facing_same_way])
    assert results == []


def test_run_simulation_unknown_facing_yaw_never_blocks_coverage_or_relevance():
    tl = TrafficLight(id="unknown-facing", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=None)
    results = run_simulation([STRAIGHT_LANE], [tl])
    assert results
    assert all(r.facing_camera for r in results)


def test_run_simulation_excludes_lights_already_behind_the_camera():
    # facing_yaw=180 makes this light relevant to an eastbound lane (it
    # passes check_light_relevant_to_lane), but every waypoint on
    # TRAILING_LANE has already driven past it -- the light sits behind,
    # not ahead. It shouldn't be scored as a blind spot just because a
    # forward-facing camera can't see behind itself.
    tl = TrafficLight(id="already-passed", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=180.0)
    results = run_simulation([TRAILING_LANE], [tl])
    assert results == []


def test_run_simulation_ahead_filter_applies_even_without_facing_yaw():
    # pedestrian-style lights with no facing_yaw still shouldn't be scored
    # against a lane that has already passed them.
    tl = TrafficLight(id="ped-already-passed", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="pedestrian")
    results = run_simulation([TRAILING_LANE], [tl])
    assert results == []


def test_build_lane_relevant_tl_ids_uses_direct_reference():
    lane = LanePath(id="A", center_line=[Point3D(0, 0, 0), Point3D(50, 0, 0)], direct_tl_ids=["light-1"], next_lane_ids=[])
    result = _build_lane_relevant_tl_ids([lane], [], max_range=200.0)
    assert result["A"] == {"light-1"}


def test_build_lane_relevant_tl_ids_inherits_from_successor_within_range():
    lane_a = LanePath(id="A", center_line=[Point3D(0, 0, 0), Point3D(50, 0, 0)], direct_tl_ids=[], next_lane_ids=["B"])
    lane_b = LanePath(id="B", center_line=[Point3D(50, 0, 0), Point3D(60, 0, 0)], direct_tl_ids=["light-1"], next_lane_ids=[])
    result = _build_lane_relevant_tl_ids([lane_a, lane_b], [], max_range=200.0)
    assert result["A"] == {"light-1"}
    assert result["B"] == {"light-1"}


def test_build_lane_relevant_tl_ids_propagates_through_multiple_hops():
    lane_a = LanePath(id="A", center_line=[Point3D(0, 0, 0), Point3D(10, 0, 0)], direct_tl_ids=[], next_lane_ids=["B"])
    lane_b = LanePath(id="B", center_line=[Point3D(10, 0, 0), Point3D(20, 0, 0)], direct_tl_ids=[], next_lane_ids=["C"])
    lane_c = LanePath(id="C", center_line=[Point3D(20, 0, 0), Point3D(30, 0, 0)], direct_tl_ids=["light-1"], next_lane_ids=[])
    result = _build_lane_relevant_tl_ids([lane_a, lane_b, lane_c], [], max_range=200.0)
    assert result["A"] == {"light-1"}
    assert result["B"] == {"light-1"}


def test_build_lane_relevant_tl_ids_none_when_successor_reference_is_out_of_range():
    lane_a = LanePath(id="A", center_line=[Point3D(0, 0, 0), Point3D(250, 0, 0)], direct_tl_ids=[], next_lane_ids=["B"])
    lane_b = LanePath(id="B", center_line=[Point3D(250, 0, 0), Point3D(260, 0, 0)], direct_tl_ids=["light-1"], next_lane_ids=[])
    result = _build_lane_relevant_tl_ids([lane_a, lane_b], [], max_range=200.0)
    assert result["A"] is None


def test_build_lane_relevant_tl_ids_none_with_no_reachable_reference():
    lane = LanePath(id="A", center_line=[Point3D(0, 0, 0), Point3D(50, 0, 0)], direct_tl_ids=[], next_lane_ids=[])
    result = _build_lane_relevant_tl_ids([lane], [], max_range=200.0)
    assert result["A"] is None


def _tl_with_stop_line(tl_id, group_id, stop_line_pos):
    return TrafficLight(id=tl_id, bulbs=[Point3D(0, 0, 0)], signal_type="vehicle", group_id=group_id, stop_line_pos=stop_line_pos)


def test_build_lane_relevant_tl_ids_uses_nearby_stop_line_when_graph_has_no_successor():
    # lane's own connectivity dead-ends (no next_lane_ids at all), but a
    # stop line sits right where the lane's mapped path runs out -- exactly
    # the real "graph gap at an intersection" case found on the bundled map.
    lane = LanePath(id="A", center_line=[Point3D(0, 0, 0), Point3D(50, 0, 0)], direct_tl_ids=[], next_lane_ids=[])
    tls = [_tl_with_stop_line("light-1", "refline:1", Point3D(55, 0, 0))]  # 5m from lane's end
    result = _build_lane_relevant_tl_ids([lane], tls, max_range=200.0)
    assert result["A"] == {"light-1"}


def test_build_lane_relevant_tl_ids_ignores_stop_line_beyond_proximity_threshold():
    lane = LanePath(id="A", center_line=[Point3D(0, 0, 0), Point3D(50, 0, 0)], direct_tl_ids=[], next_lane_ids=[])
    tls = [_tl_with_stop_line("light-1", "refline:1", Point3D(500, 0, 0))]  # far beyond STOP_LINE_PROXIMITY_M
    result = _build_lane_relevant_tl_ids([lane], tls, max_range=200.0)
    assert result["A"] is None


def test_build_lane_relevant_tl_ids_proximity_match_includes_whole_group():
    # a stop line's group can have several redundant heads (through +
    # turn-arrow) -- the nearby-stop-line match should return every member
    # of that group, not just the one TrafficLight instance that carries
    # the position.
    lane = LanePath(id="A", center_line=[Point3D(0, 0, 0), Point3D(50, 0, 0)], direct_tl_ids=[], next_lane_ids=[])
    tls = [
        _tl_with_stop_line("light-1", "refline:1", Point3D(55, 0, 0)),
        _tl_with_stop_line("light-2", "refline:1", Point3D(55, 0, 0)),
    ]
    result = _build_lane_relevant_tl_ids([lane], tls, max_range=200.0)
    assert result["A"] == {"light-1", "light-2"}


def test_build_lane_relevant_tl_ids_direct_reference_takes_priority_over_proximity():
    lane = LanePath(id="A", center_line=[Point3D(0, 0, 0), Point3D(50, 0, 0)], direct_tl_ids=["own-light"], next_lane_ids=[])
    tls = [_tl_with_stop_line("nearby-but-not-mine", "refline:1", Point3D(55, 0, 0))]
    result = _build_lane_relevant_tl_ids([lane], tls, max_range=200.0)
    assert result["A"] == {"own-light"}


def test_run_simulation_map_authoritative_reference_excludes_unlisted_cross_signal():
    # this lane's own map data says only "ahead-light" controls it -- a
    # cross-street signal that happens to satisfy the geometric
    # relevant-to-lane heuristic too (facing_yaw=100deg, >90deg off this
    # lane's 0deg heading) must still be excluded, since it isn't listed.
    lane = LanePath(
        id="lane-authoritative",
        center_line=[Point3D(x, 0.0, 0.0) for x in range(0, 201, 20)],
        direct_tl_ids=["ahead-light"],
        next_lane_ids=[],
    )
    ahead_light = TrafficLight(id="ahead-light", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=180.0)
    cross_light = TrafficLight(id="cross-light", bulbs=[Point3D(100.0, 1.0, 5.0)], signal_type="vehicle", facing_yaw=100.0)

    results = run_simulation([lane], [ahead_light, cross_light])

    assert results
    assert all(r.target_tl_id == "ahead-light" for r in results)
    assert any(r.is_covered for r in results)


def test_run_simulation_map_authoritative_reference_does_not_block_pedestrian_signals():
    # direct_tl_ids only ever lists vehicle-signal ids in practice (pedestrian
    # regulatory elements aren't referenced by road lanelets), but the filter
    # should not accidentally exclude a pedestrian candidate just because
    # it's absent from that list.
    lane = LanePath(
        id="lane-authoritative",
        center_line=[Point3D(x, 0.0, 0.0) for x in range(0, 201, 20)],
        direct_tl_ids=["ahead-light"],
        next_lane_ids=[],
    )
    ahead_light = TrafficLight(id="ahead-light", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=180.0)
    ped_light = TrafficLight(id="ped-light", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="pedestrian")

    results = run_simulation([lane], [ahead_light, ped_light])

    assert any(r.target_tl_id == "ped-light" for r in results)


def _result(target_tl_id, group_id, in_fov, facing_camera):
    return ValidationResult(
        lane_id="lane-1",
        point=Point3D(0.0, 0.0, 0.0),
        target_tl_id=target_tl_id,
        signal_type="vehicle",
        group_id=group_id,
        distance_m=100.0,
        in_fov=in_fov,
        facing_camera=facing_camera,
        is_covered=in_fov and facing_camera,
    )


def test_compute_point_status_single_covered_light():
    assert compute_point_status([_result("a", "g1", True, True)]) == "covered"


def test_compute_point_status_single_out_of_fov_light():
    assert compute_point_status([_result("a", "g1", False, True)]) == "out_of_fov"


def test_compute_point_status_single_facing_away_light():
    assert compute_point_status([_result("a", "g1", True, False)]) == "facing_away"


def test_compute_point_status_redundant_head_in_same_group_counts_as_covered():
    # two heads for the same stop line (same group_id, e.g. a through light
    # and a turn-arrow light) -- only one is visible, but that's enough to
    # know the signal state, so the point should read as covered.
    results = [_result("head-1", "g1", False, True), _result("head-2", "g1", True, True)]
    assert compute_point_status(results) == "covered"


def test_compute_point_status_different_groups_both_must_be_covered():
    # two genuinely different intersections/groups at this waypoint; only
    # one is covered -> unlike the redundant-head case, this point is NOT
    # fully covered, since the second group has no visible member at all.
    results = [_result("light-A", "g1", True, True), _result("light-B", "g2", False, True)]
    assert compute_point_status(results) == "out_of_fov"


def test_compute_point_status_prefers_facing_away_reason_when_present():
    # group g1 fails entirely out-of-fov; group g2 fails entirely
    # facing-away -- facing_away wins as the reported reason, since at
    # least one failed group had a light that was geometrically visible.
    results = [_result("light-A", "g1", False, True), _result("light-B", "g2", True, False)]
    assert compute_point_status(results) == "facing_away"


def test_run_simulation_judges_visibility_per_head_not_pooled_centroid():
    # Head A dead ahead (bearing 0, inside the +-15deg FOV); head B at
    # (70, 60), bearing ~41deg (well outside). Their pooled centroid sits
    # at (85, 30), bearing ~19.4deg -- ALSO outside the FOV -- so the old
    # centroid-based judgment called this light not covered even though a
    # real camera plainly sees head A. Per-head judgment must cover it,
    # with 1 of 2 heads visible.
    two_heads = TrafficLight(
        id="two-heads",
        bulbs=[Point3D(100.0, 0.0, 5.0), Point3D(70.0, 60.0, 5.0)],
        signal_type="vehicle",
        facing_yaw=180.0,
        heads=(
            SignalHead(pos=Point3D(100.0, 0.0, 5.0)),
            SignalHead(pos=Point3D(70.0, 60.0, 5.0)),
        ),
    )
    lane = LanePath(id="lane-1", center_line=[Point3D(0, 0, 0), Point3D(2, 0, 0)], direct_tl_ids=[], next_lane_ids=[])
    results = run_simulation([lane], [two_heads], camera=CameraSpec(min_range=10.0, max_range=200.0))
    assert results
    r = results[0]
    assert r.is_covered
    assert r.heads_total == 2
    assert r.heads_visible == 1
    assert r.in_fov  # at least one head is in FOV


def test_run_simulation_without_heads_falls_back_to_centroid_as_single_head():
    light = TrafficLight(id="plain", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=180.0)
    results = run_simulation([STRAIGHT_LANE], [light])
    assert results
    assert all(r.heads_total == 1 for r in results)
    assert any(r.is_covered and r.heads_visible == 1 for r in results)


def _head_result(group_id, heads_visible, heads_total):
    return ValidationResult(
        lane_id="lane-1",
        point=Point3D(0.0, 0.0, 0.0),
        target_tl_id=f"{group_id}-{heads_visible}",
        signal_type="vehicle",
        group_id=group_id,
        distance_m=100.0,
        in_fov=heads_visible > 0,
        facing_camera=heads_visible > 0,
        is_covered=heads_visible > 0,
        heads_total=heads_total,
        heads_visible=heads_visible,
    )


def test_compute_point_head_counts_sums_within_group():
    # one group, two lights: 2/3 + 1/2 -> 3/5 for the group
    results = [_head_result("g1", 2, 3), _head_result("g1", 1, 2)]
    assert compute_point_head_counts(results) == (3, 5)


def test_compute_point_head_counts_returns_worst_group():
    # g1 fully visible (2/2), g2 barely visible (1/4) -> weakest link is g2
    results = [_head_result("g1", 2, 2), _head_result("g2", 1, 4)]
    assert compute_point_head_counts(results) == (1, 4)


def test_compute_point_head_counts_empty():
    assert compute_point_head_counts([]) == (0, 0)


def test_compute_point_min_visible_is_absolute_count_not_ratio():
    # g1: 1 of 1 heads visible (100% but zero redundancy); g2: 2 of 6
    # visible (33%, but two independent heads). The worst *ratio* group is
    # g2, yet the redundancy bottleneck is g1's single head.
    results = [_head_result("g1", 1, 1), _head_result("g2", 2, 6)]
    assert compute_point_head_counts(results) == (2, 6)  # ratio view
    assert compute_point_min_visible(results) == 1  # redundancy view


def test_compute_point_min_visible_sums_within_group_before_taking_min():
    # g1's two lights contribute 1+2=3 visible heads; g2 has 2
    results = [_head_result("g1", 1, 2), _head_result("g1", 2, 3), _head_result("g2", 2, 2)]
    assert compute_point_min_visible(results) == 2


def test_compute_point_min_visible_zero_when_any_group_blind():
    results = [_head_result("g1", 3, 3), _head_result("g2", 0, 2)]
    assert compute_point_min_visible(results) == 0


def test_compute_point_min_visible_empty():
    assert compute_point_min_visible([]) == 0


def test_run_simulation_multi_camera_emits_per_camera_results():
    light = TrafficLight(id="ahead", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=180.0)
    tele = CameraSpec(name="tele", fov_h=30.0, min_range=50.0, max_range=200.0)
    wide = CameraSpec(name="wide", fov_h=90.0, min_range=5.0, max_range=80.0)
    results = run_simulation([STRAIGHT_LANE], [light], cameras=[tele, wide])

    names = {r.camera_name for r in results}
    assert names == {"tele", "wide"}
    # range prefilter is per camera: only tele reaches the light from x=0
    # (100m > wide's 80m max), only wide sees it from x=60 (~40m, inside
    # tele's 50m min_range)
    at_origin = {r.camera_name for r in results if r.point.x == 0.0}
    assert at_origin == {"tele"}
    at_60 = {r.camera_name for r in results if r.point.x == 60.0}
    assert at_60 == {"wide"}


def test_run_simulation_multi_camera_sums_into_redundancy():
    # both cameras cover the light at x=60 (40m away: within tele's
    # [30,200] and wide's [5,80]) -> two independent observations of the
    # same single head
    light = TrafficLight(id="ahead", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=180.0)
    tele = CameraSpec(name="tele", fov_h=30.0, min_range=30.0, max_range=200.0)
    wide = CameraSpec(name="wide", fov_h=90.0, min_range=5.0, max_range=80.0)
    results = run_simulation([STRAIGHT_LANE], [light], cameras=[tele, wide])
    at_60 = [r for r in results if r.point.x == 60.0]
    assert len(at_60) == 2
    assert all(r.is_covered for r in at_60)
    assert compute_point_min_visible(at_60) == 2  # camera redundancy counted


def test_fallback_excludes_owned_signal_serving_a_different_approach():
    # The candidate signal is owned (direct_tl_ids) by a lanelet whose
    # approach direction is 40deg off ours. Our own lane has no
    # authoritative set (no direct refs, no successors), so the candidate
    # reaches the fallback -- where the owner-approach test must reject
    # it, even though its facing_yaw (144deg off our heading) passes the
    # old 120deg facing test.
    import math
    owner = LanePath(
        id="side-approach",
        center_line=[Point3D(100.0 - 30 * math.cos(math.radians(40)), -30 * math.sin(math.radians(40)), 0.0), Point3D(100.0, 0.0, 0.0)],
        direct_tl_ids=["skewed-light"],
        next_lane_ids=[],
    )
    light = TrafficLight(
        id="skewed-light", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=-144.0
    )
    results = run_simulation([STRAIGHT_LANE, owner], [light])
    ours = [r for r in results if r.lane_id == "lane-1"]
    assert ours == []  # excluded as a candidate entirely, not just uncovered


def test_fallback_keeps_owned_signal_serving_our_approach_direction():
    # owner lanelet runs the same direction as our lane (0 deg) -> kept
    owner = LanePath(
        id="parallel-approach",
        center_line=[Point3D(70.0, 5.0, 0.0), Point3D(100.0, 5.0, 0.0)],
        direct_tl_ids=["own-light"],
        next_lane_ids=[],
    )
    light = TrafficLight(id="own-light", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=180.0)
    results = run_simulation([STRAIGHT_LANE, owner], [light])
    ours = [r for r in results if r.lane_id == "lane-1"]
    assert ours
    assert any(r.is_covered for r in ours)


def test_fallback_unowned_signal_still_uses_facing_test():
    # no lanelet anywhere claims this light -> the old 120deg facing test
    # is still what decides (facing_yaw=-144 passes it)
    light = TrafficLight(id="unowned", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=-144.0)
    results = run_simulation([STRAIGHT_LANE], [light])
    assert [r for r in results if r.lane_id == "lane-1"]


def test_run_simulation_yaw_offset_rotates_the_fov():
    # light dead ahead; a camera mounted 90deg to the left can't see it,
    # even though it's in range
    light = TrafficLight(id="ahead", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=180.0)
    side = CameraSpec(name="side", fov_h=30.0, min_range=50.0, max_range=200.0, yaw_offset=90.0)
    results = run_simulation([STRAIGHT_LANE], [light], cameras=[side])
    assert results
    assert all(not r.is_covered and not r.in_fov for r in results)
