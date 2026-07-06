"""Module E: Web Viewer.

The only module allowed to know about Flask/HTTP, same role as
visualizer.py has for matplotlib. Loads a map and runs the simulation
once at startup (reusing Modules A-C unchanged), then serves that fixed
result set over a tiny JSON API for the static frontend in static/.

Point of this tool: let you click a specific waypoint on the map and see,
literally, where each candidate traffic light falls inside (or outside)
the camera's FOV rectangle -- the same geometry `check_fov_inclusion` /
`check_light_facing_camera` test against a threshold, rendered instead of
just judged, so "why is this red/orange" stops being a guessing game.

v1 is deliberately fixed-camera-spec (see the prior conversation): no live
FOV/range sliders. Restart with different --fov-h/etc. flags to inspect a
different camera spec.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from flask import Flask, abort, jsonify

from config import AppConfig, load_config
from fov_simulator import SAMPLE_INTERVAL_M, run_simulation
from geometry_calculator import calc_camera_frame_offset, calc_centroid, calc_heading_yaw, calc_resample_by_distance
from map_parser import parse_lanes, parse_nodes, parse_traffic_lights
from models import CameraSpec, LanePath, Point3D

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")

# Populated once by _load_data() before the server starts serving requests.
_state: dict = {}


def _build_lane_yaw_lookup(lanes: list[LanePath]) -> dict[str, dict[tuple[float, float], float]]:
    """lane_id -> {(x, y): cam_yaw at that resampled waypoint}.

    Mirrors run_simulation's own per-waypoint heading computation exactly
    (same SAMPLE_INTERVAL_M, same calc_resample_by_distance/calc_heading_yaw
    calls) so the (x, y) keys line up bit-for-bit with ValidationResult.point.
    """
    lookup: dict[str, dict[tuple[float, float], float]] = {}
    for lane in lanes:
        sampled = calc_resample_by_distance(lane.center_line, SAMPLE_INTERVAL_M)
        if len(sampled) < 2:
            continue
        last_idx = len(sampled) - 1
        per_lane: dict[tuple[float, float], float] = {}
        for i, p in enumerate(sampled):
            if i < last_idx:
                yaw = calc_heading_yaw(sampled[i], sampled[i + 1])
            else:
                yaw = calc_heading_yaw(sampled[i - 1], sampled[i])
            per_lane[(p.x, p.y)] = yaw
        lookup[lane.id] = per_lane
    return lookup


def _load_data(map_path: Path, camera: CameraSpec, signal_types: set[str] | None) -> None:
    xml_string = map_path.read_text(encoding="utf-8")
    nodes = parse_nodes(xml_string)
    lanes = parse_lanes(xml_string, nodes)
    traffic_lights = parse_traffic_lights(xml_string, nodes)
    results = run_simulation(lanes, traffic_lights, camera=camera, signal_types=signal_types)

    tl_positions = {tl.id: calc_centroid(tl.bulbs) for tl in traffic_lights if tl.bulbs}
    yaw_lookup = _build_lane_yaw_lookup(lanes)

    points: list[dict] = []
    point_key_to_id: dict[tuple[str, float, float], int] = {}
    results_by_point: dict[int, list] = {}

    for r in results:
        key = (r.lane_id, r.point.x, r.point.y)
        point_id = point_key_to_id.get(key)
        if point_id is None:
            point_id = len(points)
            point_key_to_id[key] = point_id
            points.append({"id": point_id, "lane_id": r.lane_id, "x": r.point.x, "y": r.point.y, "z": r.point.z})
            results_by_point[point_id] = []
        results_by_point[point_id].append(r)

    for p in points:
        rs = results_by_point[p["id"]]
        if any(not r.in_fov for r in rs):
            p["status"] = "out_of_fov"
        elif any(not r.facing_camera for r in rs):
            p["status"] = "facing_away"
        else:
            p["status"] = "covered"

    _state.update(
        camera=camera,
        points=points,
        results_by_point=results_by_point,
        tl_positions=tl_positions,
        yaw_lookup=yaw_lookup,
        lane_count=len(lanes),
        traffic_light_count=len(traffic_lights),
    )


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/meta")
def meta():
    camera: CameraSpec = _state["camera"]
    return jsonify(
        {
            "camera": {
                "height": camera.height,
                "fov_h": camera.fov_h,
                "fov_v": camera.fov_v,
                "min_range": camera.min_range,
                "max_range": camera.max_range,
                "facing_tolerance_deg": camera.facing_tolerance_deg,
            },
            "lane_count": _state["lane_count"],
            "traffic_light_count": _state["traffic_light_count"],
            "point_count": len(_state["points"]),
        }
    )


@app.route("/api/points")
def points():
    return jsonify(_state["points"])


@app.route("/api/traffic_lights")
def traffic_lights():
    return jsonify([{"id": tl_id, "x": pos.x, "y": pos.y} for tl_id, pos in _state["tl_positions"].items()])


@app.route("/api/points/<int:point_id>/candidates")
def point_candidates(point_id: int):
    points_list = _state["points"]
    if point_id < 0 or point_id >= len(points_list):
        abort(404, description="unknown point id")

    p = points_list[point_id]
    camera: CameraSpec = _state["camera"]
    cam_yaw = _state["yaw_lookup"][p["lane_id"]][(p["x"], p["y"])]
    cam_pos = Point3D(p["x"], p["y"], p["z"] + camera.height)

    candidates = []
    for r in _state["results_by_point"][point_id]:
        target_pos = _state["tl_positions"][r.target_tl_id]
        yaw_diff, pitch_diff = calc_camera_frame_offset(cam_pos, cam_yaw, 0.0, target_pos)
        candidates.append(
            {
                "target_tl_id": r.target_tl_id,
                "signal_type": r.signal_type,
                "distance_m": r.distance_m,
                "in_fov": r.in_fov,
                "facing_camera": r.facing_camera,
                "is_covered": r.is_covered,
                "yaw_diff": yaw_diff,
                "pitch_diff": pitch_diff,
                "norm_x": yaw_diff / (camera.fov_h / 2.0),
                "norm_y": pitch_diff / (camera.fov_v / 2.0),
            }
        )

    return jsonify(
        {
            "point": {"lane_id": p["lane_id"], "x": p["x"], "y": p["y"]},
            "cam_pos": {"x": cam_pos.x, "y": cam_pos.y, "z": cam_pos.z},
            "cam_yaw": cam_yaw,
            "fov_h": camera.fov_h,
            "fov_v": camera.fov_v,
            "candidates": candidates,
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive viewer: inspect FOV coverage at a specific waypoint")
    parser.add_argument("--config", type=Path, default=None, help="YAML file with camera/signal_type/map settings")
    parser.add_argument("--map", type=Path, default=None)
    parser.add_argument("--cam-height", type=float, default=None)
    parser.add_argument("--fov-h", type=float, default=None)
    parser.add_argument("--fov-v", type=float, default=None)
    parser.add_argument("--min-range", type=float, default=None)
    parser.add_argument("--max-range", type=float, default=None)
    parser.add_argument("--facing-tolerance", type=float, default=None)
    parser.add_argument("--signal-type", choices=["vehicle", "pedestrian", "both"], default=None)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    config = load_config(args.config.read_text(encoding="utf-8")) if args.config else AppConfig()

    camera_overrides = {
        "height": args.cam_height,
        "fov_h": args.fov_h,
        "fov_v": args.fov_v,
        "min_range": args.min_range,
        "max_range": args.max_range,
        "facing_tolerance_deg": args.facing_tolerance,
    }
    camera = replace(config.camera, **{k: v for k, v in camera_overrides.items() if v is not None})

    signal_type = args.signal_type or config.signal_type
    signal_types = None if signal_type == "both" else {signal_type}

    map_path = args.map or (Path(config.map_path) if config.map_path else None)
    if map_path is None:
        parser.error("no map specified: pass --map <file.osm>, or set `map:` in a --config YAML file")

    print(f"Loading {map_path} ...")
    _load_data(map_path, camera, signal_types)
    print(f"Loaded {len(_state['points'])} waypoints, {_state['traffic_light_count']} traffic lights.")
    print(f"Open http://127.0.0.1:{args.port} in a browser.")
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
