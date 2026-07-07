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

Computing a run takes ~20-30s on the bundled map and needs the (large,
not redistributable) .osm file. `--save`/`--load` and `/api/export` let a
computed run be frozen to a JSON snapshot and handed to someone else (or
reloaded later) without either of those -- see docs/behavior.md.
"""

from __future__ import annotations

import argparse
import gzip
import json
from dataclasses import replace
from pathlib import Path

from flask import Flask, Response, abort, jsonify

from config import AppConfig, load_config
from fov_simulator import SAMPLE_INTERVAL_M, compute_point_status, run_simulation
from geometry_calculator import calc_camera_frame_offset, calc_centroid, calc_heading_yaw, calc_resample_by_distance
from map_parser import parse_lanes, parse_nodes, parse_traffic_lights
from models import CameraSpec, LanePath, Point3D, ValidationResult

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")

# Populated once by _load_data() or _deserialize_state() before the server
# starts serving requests.
_state: dict = {}
_SNAPSHOT_FORMAT_VERSION = 1


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
        p["status"] = compute_point_status(results_by_point[p["id"]])

    _state.update(
        camera=camera,
        points=points,
        results_by_point=results_by_point,
        tl_positions=tl_positions,
        yaw_lookup=yaw_lookup,
        lane_count=len(lanes),
        traffic_light_count=len(traffic_lights),
    )


def _serialize_state() -> dict:
    """The current `_state` as a JSON-safe dict, compact enough to hand to
    someone else or reload later without the (large, not redistributable)
    source .osm file. `_deserialize_state` is the exact inverse.
    """
    camera: CameraSpec = _state["camera"]
    return {
        "format_version": _SNAPSHOT_FORMAT_VERSION,
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
        "points": _state["points"],
        "results_by_point": {
            str(point_id): [
                {
                    "target_tl_id": r.target_tl_id,
                    "signal_type": r.signal_type,
                    "group_id": r.group_id,
                    "distance_m": r.distance_m,
                    "in_fov": r.in_fov,
                    "facing_camera": r.facing_camera,
                    "is_covered": r.is_covered,
                }
                for r in candidates
            ]
            for point_id, candidates in _state["results_by_point"].items()
        },
        "tl_positions": {tl_id: {"x": p.x, "y": p.y, "z": p.z} for tl_id, p in _state["tl_positions"].items()},
        "yaw_lookup": {
            lane_id: [[x, y, yaw] for (x, y), yaw in per_lane.items()]
            for lane_id, per_lane in _state["yaw_lookup"].items()
        },
    }


def _deserialize_state(data: dict) -> None:
    """Populate `_state` from a dict produced by `_serialize_state`
    (typically loaded from a `--save`d JSON file), reconstructing the
    `Point3D`/`ValidationResult` instances the rest of this module expects
    -- no map file or simulation run needed.
    """
    if data.get("format_version") != _SNAPSHOT_FORMAT_VERSION:
        raise ValueError(
            f"unsupported snapshot format_version {data.get('format_version')!r}, expected {_SNAPSHOT_FORMAT_VERSION}"
        )

    camera = CameraSpec(**data["camera"])
    points = data["points"]
    tl_positions = {tl_id: Point3D(**pos) for tl_id, pos in data["tl_positions"].items()}
    yaw_lookup = {
        lane_id: {(x, y): yaw for x, y, yaw in entries} for lane_id, entries in data["yaw_lookup"].items()
    }

    results_by_point: dict[int, list[ValidationResult]] = {}
    for point_id_str, candidates in data["results_by_point"].items():
        point_id = int(point_id_str)
        p = points[point_id]
        results_by_point[point_id] = [
            ValidationResult(
                lane_id=p["lane_id"],
                point=Point3D(p["x"], p["y"], p["z"]),
                target_tl_id=c["target_tl_id"],
                signal_type=c["signal_type"],
                group_id=c["group_id"],
                distance_m=c["distance_m"],
                in_fov=c["in_fov"],
                facing_camera=c["facing_camera"],
                is_covered=c["is_covered"],
            )
            for c in candidates
        ]

    _state.clear()
    _state.update(
        camera=camera,
        points=points,
        results_by_point=results_by_point,
        tl_positions=tl_positions,
        yaw_lookup=yaw_lookup,
        lane_count=data["lane_count"],
        traffic_light_count=data["traffic_light_count"],
    )


def _write_snapshot(path: Path, data: dict) -> None:
    """gzip-compressed JSON -- the uncompressed snapshot runs ~100+MB on the
    bundled map (one entry per waypoint/light candidate), which compresses
    very well since it's so repetitive; too big to casually share otherwise.
    """
    path.write_bytes(gzip.compress(json.dumps(data).encode("utf-8")))


def _read_snapshot(path: Path) -> dict:
    """Inverse of `_write_snapshot`, but also accepts a plain (uncompressed)
    JSON file -- e.g. one a user hand-edited -- by falling back if gzip
    decompression fails.
    """
    raw = path.read_bytes()
    try:
        text = gzip.decompress(raw).decode("utf-8")
    except OSError:
        text = raw.decode("utf-8")
    return json.loads(text)


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/export")
def export_results():
    """The current results as a downloadable gzip-compressed JSON snapshot --
    the same format `--save` writes, and what `--load` reads back.
    """
    return Response(
        gzip.compress(json.dumps(_serialize_state()).encode("utf-8")),
        mimetype="application/gzip",
        headers={"Content-Disposition": "attachment; filename=fov_results.json.gz"},
    )


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

    point_results = _state["results_by_point"][point_id]
    group_covered = {r.group_id for r in point_results if r.is_covered}

    candidates = []
    for r in point_results:
        target_pos = _state["tl_positions"][r.target_tl_id]
        yaw_diff, pitch_diff = calc_camera_frame_offset(cam_pos, cam_yaw, 0.0, target_pos)
        candidates.append(
            {
                "target_tl_id": r.target_tl_id,
                "signal_type": r.signal_type,
                "group_id": r.group_id,
                "group_covered": r.group_id in group_covered,
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
            "status": compute_point_status(point_results),
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
    parser.add_argument(
        "--load", type=Path, default=None, help="load previously --save'd results, skipping map parsing and simulation entirely"
    )
    parser.add_argument(
        "--save", type=Path, default=None, help="write the computed (or loaded) results to this gzip-compressed JSON file"
    )
    args = parser.parse_args()

    if args.load:
        print(f"Loading saved results from {args.load} ...")
        _deserialize_state(_read_snapshot(args.load))
    else:
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
            parser.error(
                "no map specified: pass --map <file.osm>, set `map:` in a --config YAML file, "
                "or use --load to load previously --save'd results"
            )

        print(f"Loading {map_path} ...")
        _load_data(map_path, camera, signal_types)

    if args.save:
        _write_snapshot(args.save, _serialize_state())
        print(f"Saved results to {args.save}")

    print(f"Loaded {len(_state['points'])} waypoints, {_state['traffic_light_count']} traffic lights.")
    print(f"Open http://127.0.0.1:{args.port} in a browser.")
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
