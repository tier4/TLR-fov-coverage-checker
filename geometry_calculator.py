"""Module A: Geometry Calculator.

Pure functions only: every function's output depends solely on its
arguments, with no I/O, no globals, no hidden state. This is what makes
the module trivially unit-testable and safe to reuse from the simulator.
"""

from __future__ import annotations

import math

from models import Point3D


def calc_distance_3d(p1: Point3D, p2: Point3D) -> float:
    """Euclidean distance between two 3D points."""
    return math.sqrt((p1.x - p2.x) ** 2 + (p1.y - p2.y) ** 2 + (p1.z - p2.z) ** 2)


def _cumulative_lengths(points: list[Point3D]) -> list[float]:
    lengths = [0.0]
    for a, b in zip(points, points[1:]):
        lengths.append(lengths[-1] + calc_distance_3d(a, b))
    return lengths


def _interpolate_at_arc_lengths(points: list[Point3D], targets: list[float]) -> list[Point3D]:
    """Sample `points` (a polyline) at the given arc-length distances from its start."""
    cum = _cumulative_lengths(points)
    result: list[Point3D] = []
    seg = 0
    last_seg = len(points) - 2
    for t in targets:
        while seg < last_seg and cum[seg + 1] < t:
            seg += 1
        seg_len = cum[seg + 1] - cum[seg]
        ratio = 0.0 if seg_len == 0.0 else (t - cum[seg]) / seg_len
        a, b = points[seg], points[seg + 1]
        result.append(
            Point3D(
                x=a.x + (b.x - a.x) * ratio,
                y=a.y + (b.y - a.y) * ratio,
                z=a.z + (b.z - a.z) * ratio,
            )
        )
    return result


def _resample_by_count(points: list[Point3D], count: int) -> list[Point3D]:
    """Resample a polyline to exactly `count` evenly (by arc-length) spaced points."""
    if not points or count <= 0:
        return []
    if len(points) == 1:
        return [points[0]] * count
    if count == 1:
        return [points[0]]
    total = _cumulative_lengths(points)[-1]
    if total == 0.0:
        return [points[0]] * count
    targets = [total * i / (count - 1) for i in range(count)]
    return _interpolate_at_arc_lengths(points, targets)


def calc_center_line(left_nodes: list[Point3D], right_nodes: list[Point3D]) -> list[Point3D]:
    """Compute the center line of a lane from its left and right boundary polylines.

    Real Lanelet2 maps rarely have the same node count on both boundaries
    (e.g. 125 vs 120 points for the same lanelet), so both sides are
    resampled by arc length to a common point count before averaging.
    """
    if not left_nodes or not right_nodes:
        return []
    count = max(len(left_nodes), len(right_nodes))
    left_r = _resample_by_count(left_nodes, count)
    right_r = _resample_by_count(right_nodes, count)
    return [
        Point3D(x=(l.x + r.x) / 2.0, y=(l.y + r.y) / 2.0, z=(l.z + r.z) / 2.0)
        for l, r in zip(left_r, right_r)
    ]


def calc_resample_by_distance(points: list[Point3D], interval: float) -> list[Point3D]:
    """Resample a polyline at a fixed arc-length interval (e.g. every 1m)."""
    if len(points) < 2 or interval <= 0:
        return list(points)
    total = _cumulative_lengths(points)[-1]
    if total == 0.0:
        return [points[0]]
    n_steps = int(total // interval)
    targets = [i * interval for i in range(n_steps + 1)]
    if targets[-1] < total:
        targets.append(total)
    return _interpolate_at_arc_lengths(points, targets)


def calc_heading_yaw(p1: Point3D, p2: Point3D) -> float:
    """Bearing from p1 to p2 in degrees, in the range (-180, 180]."""
    return math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))


def calc_centroid(points: list[Point3D]) -> Point3D:
    """Arithmetic mean position of a non-empty list of points."""
    n = len(points)
    return Point3D(
        x=sum(p.x for p in points) / n,
        y=sum(p.y for p in points) / n,
        z=sum(p.z for p in points) / n,
    )


def _normalize_angle_deg(angle: float) -> float:
    """Wrap an angle in degrees into (-180, 180]."""
    a = angle % 360.0
    if a > 180.0:
        a -= 360.0
    return a


_BOUNDARY_EPSILON_DEG = 1e-9  # absorbs float round-trip noise (~1e-14) at exact FOV edges


def calc_camera_frame_offset(
    cam_pos: Point3D,
    cam_yaw: float,
    cam_pitch: float,
    target_pos: Point3D,
) -> tuple[float, float]:
    """How far `target_pos` sits from dead-center of the camera's view.

    Returns (yaw_diff, pitch_diff) in degrees: the horizontal and vertical
    angle between where the camera looks (cam_yaw/cam_pitch) and the
    bearing/elevation to the target. (0.0, 0.0) means dead center; a target
    at the edge of a `fov_h`-degree-wide FOV has |yaw_diff| == fov_h / 2.
    This is the same geometry `check_fov_inclusion` tests against a
    threshold, exposed as a raw offset instead of a boolean so a caller
    (e.g. a "what does the camera see" viewer) can place the target within
    the frame instead of just asking in/out.
    """
    dx = target_pos.x - cam_pos.x
    dy = target_pos.y - cam_pos.y
    dz = target_pos.z - cam_pos.z
    horizontal_dist = math.hypot(dx, dy)

    if horizontal_dist == 0.0 and dz == 0.0:
        return 0.0, 0.0

    target_yaw = math.degrees(math.atan2(dy, dx))
    yaw_diff = _normalize_angle_deg(target_yaw - cam_yaw)

    if horizontal_dist == 0.0:
        target_pitch = 90.0 if dz > 0 else -90.0
    else:
        target_pitch = math.degrees(math.atan2(dz, horizontal_dist))
    pitch_diff = _normalize_angle_deg(target_pitch - cam_pitch)

    return yaw_diff, pitch_diff


def check_fov_inclusion(
    cam_pos: Point3D,
    cam_yaw: float,
    cam_pitch: float,
    target_pos: Point3D,
    fov_h: float = 30.0,
    fov_v: float = 17.0,
) -> bool:
    """Whether `target_pos` falls inside the camera's horizontal/vertical FOV cone.

    cam_yaw / cam_pitch / fov_h / fov_v are all in degrees. The camera looks
    along cam_yaw in the XY plane and cam_pitch above/below the horizon.
    Boundary angles (exactly fov/2 away) count as included.
    """
    yaw_diff, pitch_diff = calc_camera_frame_offset(cam_pos, cam_yaw, cam_pitch, target_pos)
    if abs(yaw_diff) > fov_h / 2.0 + _BOUNDARY_EPSILON_DEG:
        return False
    return abs(pitch_diff) <= fov_v / 2.0 + _BOUNDARY_EPSILON_DEG


def check_light_facing_camera(
    tl_pos: Point3D,
    tl_facing_yaw: float,
    cam_pos: Point3D,
    max_angle_diff: float = 45.0,
) -> bool:
    """Whether the camera sits within `max_angle_diff` degrees of straight ahead
    of the traffic light's face.

    A signal head is only legible from roughly in front of it; a camera far
    off to the side (or behind it) sees the housing's edge/back, not the
    lit lamp. `tl_facing_yaw` is the direction (degrees) the light points,
    e.g. as produced by `calc_heading_yaw(bulb_centroid, stop_line_midpoint)`.
    """
    bearing_to_cam = calc_heading_yaw(tl_pos, cam_pos)
    diff = _normalize_angle_deg(bearing_to_cam - tl_facing_yaw)
    return abs(diff) <= max_angle_diff + _BOUNDARY_EPSILON_DEG


def check_light_relevant_to_lane(
    tl_facing_yaw: float,
    lane_heading: float,
    threshold_deg: float = 90.0,
) -> bool:
    """Whether a signal facing `tl_facing_yaw` is plausibly meant to be seen by
    traffic travelling along `lane_heading`, as opposed to belonging to an
    opposing-direction lane at the same physical location.

    A signal that faces roughly opposite the lane's direction of travel
    (angular difference near 180 degrees) shines back at approaching
    traffic on this lane -- relevant. One that faces roughly the same way
    this lane travels (angular difference near 0) shines the other way,
    down the parallel lane going the opposite direction -- not relevant to
    this lane at all, regardless of distance or camera FOV. `threshold_deg`
    is the angular midpoint (90 degrees) that separates the two cases.
    """
    diff = _normalize_angle_deg(tl_facing_yaw - lane_heading)
    return abs(diff) > threshold_deg + _BOUNDARY_EPSILON_DEG


def check_target_ahead(
    cam_pos: Point3D,
    cam_yaw: float,
    target_pos: Point3D,
    max_angle_diff: float = 90.0,
) -> bool:
    """Whether `target_pos` lies within `max_angle_diff` degrees of straight
    ahead of `cam_yaw` (horizontal bearing only) -- i.e. hasn't already been
    passed, rather than actually being within the camera's FOV.

    Deliberately much wider than the camera's real FOV cone
    (`check_fov_inclusion`'s `fov_h`): a target more than 90 degrees off
    the direction of travel is behind the vehicle. A forward-facing camera
    not seeing something behind it isn't a camera-spec gap -- it's not
    meaningful to report as a blind spot at all, so this is a route-
    position pre-filter run before the real FOV check, not a substitute
    for it.
    """
    dx = target_pos.x - cam_pos.x
    dy = target_pos.y - cam_pos.y
    if dx == 0.0 and dy == 0.0:
        return True
    bearing = math.degrees(math.atan2(dy, dx))
    diff = _normalize_angle_deg(bearing - cam_yaw)
    return abs(diff) <= max_angle_diff + _BOUNDARY_EPSILON_DEG
