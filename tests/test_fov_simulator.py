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

    # same position, but facing the other way (0deg) -- geometrically in FOV,
    # but its face points away from the approaching camera.
    facing_away = TrafficLight(
        id="facing-away", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=0.0
    )
    results_away = run_simulation([STRAIGHT_LANE], [facing_away])
    away = [r for r in results_away if r.target_tl_id == "facing-away"]
    assert away
    assert all(not r.is_covered for r in away)
    assert any(r.in_fov and not r.facing_camera for r in away)


def test_run_simulation_unknown_facing_yaw_never_blocks_coverage():
    tl = TrafficLight(id="unknown-facing", bulbs=[Point3D(100.0, 0.0, 5.0)], signal_type="vehicle", facing_yaw=None)
    results = run_simulation([STRAIGHT_LANE], [tl])
    assert results
    assert all(r.facing_camera for r in results)
