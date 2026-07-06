"""Module C tests use hand-built LanePath/TrafficLight fixtures (no XML
parsing involved) to keep the simulator's own logic -- range filtering,
signal_type filtering, and combining in_fov/facing_camera into
is_covered -- independently testable from Module B.
"""

from fov_simulator import run_simulation
from models import CameraSpec, LanePath, Point3D, TrafficLight

STRAIGHT_LANE = LanePath(
    id="lane-1",
    center_line=[Point3D(x, 0.0, 0.0) for x in range(0, 201, 20)],
)

# entirely east of x=100 -- a light at x=100 is behind every waypoint here,
# since this lane (like STRAIGHT_LANE) still travels east (cam_yaw=0).
TRAILING_LANE = LanePath(
    id="lane-2",
    center_line=[Point3D(x, 0.0, 0.0) for x in range(150, 201, 10)],
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

    # same position, facing_yaw=120deg: still >90deg from the lane's 0deg
    # heading (i.e. still plausibly meant for this lane, not the opposing
    # one) but past the default 45deg facing_tolerance_deg of straight-on
    # -> geometrically in FOV, but too far off-axis to read.
    facing_off_tolerance = TrafficLight(
        id="facing-off-tolerance", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=120.0
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
