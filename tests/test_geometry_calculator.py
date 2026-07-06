import math

import pytest

from geometry_calculator import (
    calc_center_line,
    calc_centroid,
    calc_distance_3d,
    calc_heading_yaw,
    calc_resample_by_distance,
    check_fov_inclusion,
    check_light_facing_camera,
    check_light_relevant_to_lane,
)
from models import Point3D


def test_calc_distance_3d_basic():
    assert calc_distance_3d(Point3D(0, 0, 0), Point3D(3, 4, 0)) == pytest.approx(5.0)


def test_calc_distance_3d_with_elevation():
    assert calc_distance_3d(Point3D(0, 0, 0), Point3D(0, 0, 10)) == pytest.approx(10.0)


def test_calc_center_line_equal_length():
    left = [Point3D(0, 1, 0), Point3D(10, 1, 0)]
    right = [Point3D(0, -1, 0), Point3D(10, -1, 0)]
    center = calc_center_line(left, right)
    assert center == [Point3D(0, 0, 0), Point3D(10, 0, 0)]


def test_calc_center_line_mismatched_length_resamples_by_arc_length():
    # left has 3 points, right has 2 -- mirrors real Lanelet2 data where
    # boundary ways rarely share the same node count.
    left = [Point3D(0, 1, 0), Point3D(5, 1, 0), Point3D(10, 1, 0)]
    right = [Point3D(0, -1, 0), Point3D(10, -1, 0)]
    center = calc_center_line(left, right)
    assert len(center) == 3
    for pt in center:
        assert pt.y == pytest.approx(0.0)
    assert center[0].x == pytest.approx(0.0)
    assert center[-1].x == pytest.approx(10.0)


def test_calc_center_line_empty_input_returns_empty():
    assert calc_center_line([], []) == []


def test_calc_heading_yaw_east_is_zero():
    assert calc_heading_yaw(Point3D(0, 0, 0), Point3D(10, 0, 0)) == pytest.approx(0.0)


def test_calc_heading_yaw_north_is_ninety():
    assert calc_heading_yaw(Point3D(0, 0, 0), Point3D(0, 10, 0)) == pytest.approx(90.0)


def test_calc_heading_yaw_west_is_180():
    assert abs(calc_heading_yaw(Point3D(0, 0, 0), Point3D(-10, 0, 0))) == pytest.approx(180.0)


def test_calc_resample_by_distance_spacing_and_endpoints():
    points = [Point3D(0, 0, 0), Point3D(10, 0, 0)]
    resampled = calc_resample_by_distance(points, 2.5)
    assert len(resampled) == 5
    assert resampled[0] == Point3D(0, 0, 0)
    assert resampled[-1] == Point3D(10, 0, 0)


# --- check_fov_inclusion: boundary-value tests -----------------------------


def test_fov_directly_ahead_is_covered():
    cam = Point3D(0, 0, 0)
    target = Point3D(100, 0, 0)
    assert check_fov_inclusion(cam, cam_yaw=0.0, cam_pitch=0.0, target_pos=target, fov_h=30.0) is True


def test_fov_exactly_on_horizontal_edge_is_covered():
    # bearing = 15deg, fov_h/2 = 15deg -> exactly on the boundary, should count as inside
    cam = Point3D(0, 0, 0)
    target = Point3D(100 * math.cos(math.radians(15)), 100 * math.sin(math.radians(15)), 0)
    assert check_fov_inclusion(cam, cam_yaw=0.0, cam_pitch=0.0, target_pos=target, fov_h=30.0) is True


def test_fov_just_outside_horizontal_edge_is_blind():
    cam = Point3D(0, 0, 0)
    target = Point3D(100 * math.cos(math.radians(15.1)), 100 * math.sin(math.radians(15.1)), 0)
    assert check_fov_inclusion(cam, cam_yaw=0.0, cam_pitch=0.0, target_pos=target, fov_h=30.0) is False


def test_fov_target_behind_camera_is_blind():
    cam = Point3D(0, 0, 0)
    target = Point3D(-100, 0, 0)
    assert check_fov_inclusion(cam, cam_yaw=0.0, cam_pitch=0.0, target_pos=target, fov_h=30.0) is False


def test_fov_exactly_on_vertical_edge_is_covered():
    horizontal_dist = 100.0
    dz = horizontal_dist * math.tan(math.radians(8.5))  # fov_v/2 = 8.5deg
    cam = Point3D(0, 0, 0)
    target = Point3D(horizontal_dist, 0, dz)
    assert check_fov_inclusion(cam, cam_yaw=0.0, cam_pitch=0.0, target_pos=target, fov_v=17.0) is True


def test_fov_just_outside_vertical_edge_is_blind():
    horizontal_dist = 100.0
    dz = horizontal_dist * math.tan(math.radians(9.0))
    cam = Point3D(0, 0, 0)
    target = Point3D(horizontal_dist, 0, dz)
    assert check_fov_inclusion(cam, cam_yaw=0.0, cam_pitch=0.0, target_pos=target, fov_v=17.0) is False


def test_fov_respects_camera_yaw_offset():
    cam = Point3D(0, 0, 0)
    target = Point3D(100, 100, 0)  # bearing = 45deg
    assert check_fov_inclusion(cam, cam_yaw=45.0, cam_pitch=0.0, target_pos=target, fov_h=30.0) is True
    assert check_fov_inclusion(cam, cam_yaw=0.0, cam_pitch=0.0, target_pos=target, fov_h=30.0) is False


def test_fov_camera_and_target_coincide_is_covered():
    p = Point3D(5, 5, 5)
    assert check_fov_inclusion(p, cam_yaw=0.0, cam_pitch=0.0, target_pos=p) is True


def test_calc_centroid_of_three_points():
    pts = [Point3D(0, 0, 0), Point3D(3, 0, 0), Point3D(0, 3, 0)]
    assert calc_centroid(pts) == Point3D(1.0, 1.0, 0.0)


# --- check_light_facing_camera: boundary-value tests -----------------------


def test_facing_camera_directly_in_front_is_visible():
    tl_pos = Point3D(0, 0, 10)
    # light faces east (yaw=0); camera due east of it -> dead-on view
    cam_pos = Point3D(100, 0, 0)
    assert check_light_facing_camera(tl_pos, tl_facing_yaw=0.0, cam_pos=cam_pos, max_angle_diff=45.0) is True


def test_facing_camera_directly_behind_is_not_visible():
    tl_pos = Point3D(0, 0, 10)
    # camera is behind the light (west side), looking at its back
    cam_pos = Point3D(-100, 0, 0)
    assert check_light_facing_camera(tl_pos, tl_facing_yaw=0.0, cam_pos=cam_pos, max_angle_diff=45.0) is False


def test_facing_camera_exactly_on_angle_edge_is_visible():
    tl_pos = Point3D(0, 0, 0)
    bearing = math.radians(45.0)
    cam_pos = Point3D(100 * math.cos(bearing), 100 * math.sin(bearing), 0)
    assert check_light_facing_camera(tl_pos, tl_facing_yaw=0.0, cam_pos=cam_pos, max_angle_diff=45.0) is True


def test_facing_camera_just_outside_angle_edge_is_not_visible():
    tl_pos = Point3D(0, 0, 0)
    bearing = math.radians(45.1)
    cam_pos = Point3D(100 * math.cos(bearing), 100 * math.sin(bearing), 0)
    assert check_light_facing_camera(tl_pos, tl_facing_yaw=0.0, cam_pos=cam_pos, max_angle_diff=45.0) is False


# --- check_light_relevant_to_lane: boundary-value tests --------------------


def test_relevant_when_facing_directly_opposite_lane_heading():
    # lane travels east (0deg); light faces west (180deg) -- shines back at
    # this lane's approaching traffic.
    assert check_light_relevant_to_lane(tl_facing_yaw=180.0, lane_heading=0.0) is True


def test_not_relevant_when_facing_same_direction_as_lane_heading():
    # light faces the same way the lane travels -- it belongs to a parallel
    # lane going the opposite direction at the same location.
    assert check_light_relevant_to_lane(tl_facing_yaw=0.0, lane_heading=0.0) is False


def test_relevant_just_inside_the_90_degree_threshold():
    assert check_light_relevant_to_lane(tl_facing_yaw=90.1, lane_heading=0.0, threshold_deg=90.0) is True


def test_not_relevant_exactly_on_the_90_degree_threshold():
    assert check_light_relevant_to_lane(tl_facing_yaw=90.0, lane_heading=0.0, threshold_deg=90.0) is False


def test_not_relevant_just_outside_the_90_degree_threshold():
    assert check_light_relevant_to_lane(tl_facing_yaw=89.9, lane_heading=0.0, threshold_deg=90.0) is False


def test_relevant_to_lane_is_symmetric_regardless_of_absolute_heading():
    # only the angular difference matters, not the absolute compass directions
    assert check_light_relevant_to_lane(tl_facing_yaw=270.0, lane_heading=90.0) is True
    assert check_light_relevant_to_lane(tl_facing_yaw=-90.0, lane_heading=90.0) is True
