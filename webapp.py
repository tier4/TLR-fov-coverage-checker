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
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request

from config import AppConfig, load_config
from fov_simulator import (
    SAMPLE_INTERVAL_M,
    compute_point_head_counts,
    compute_point_min_visible,
    compute_point_status,
    run_simulation,
)
from geometry_calculator import check_fov_inclusion, check_light_facing_camera
from geometry_calculator import calc_camera_frame_offset, calc_centroid, calc_heading_yaw, calc_resample_by_distance
from map_parser import parse_lanes, parse_latlon_transform, parse_nodes, parse_signal_heads, parse_traffic_lights
from models import CameraSpec, LanePath, Point3D, ValidationResult

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")

# Populated once by _load_data() or _deserialize_state() before the server
# starts serving requests.
_state: dict = {}
_SNAPSHOT_FORMAT_VERSION = 10  # bumped for multi-camera rigs (cameras list, per-result camera_name)

_CAMERA_FIELDS = (
    "height",
    "fov_h",
    "fov_v",
    "min_range",
    "max_range",
    "facing_tolerance_deg",
    "name",
    "yaw_offset",
    "pitch_offset",
)


def _camera_dict(cam: CameraSpec) -> dict:
    return {f: getattr(cam, f) for f in _CAMERA_FIELDS}


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


def _load_data(map_path: Path, cameras: list[CameraSpec], signal_types: set[str] | None) -> None:
    _load_from_xml(map_path.read_text(encoding="utf-8"), cameras, signal_types)


def _load_from_xml(xml_string: str, cameras: list[CameraSpec], signal_types: set[str] | None) -> None:
    nodes = parse_nodes(xml_string)
    lanes = parse_lanes(xml_string, nodes)
    traffic_lights = parse_traffic_lights(xml_string, nodes)
    results = run_simulation(lanes, traffic_lights, cameras=cameras, signal_types=signal_types)

    tl_positions = {tl.id: calc_centroid(tl.bulbs) for tl in traffic_lights if tl.bulbs}
    tl_facing_yaw = {tl.id: tl.facing_yaw for tl in traffic_lights if tl.bulbs}
    tl_signal_type = {tl.id: tl.signal_type for tl in traffic_lights if tl.bulbs}
    tl_panel_size = {tl.id: [tl.panel_width, tl.panel_height] for tl in traffic_lights if tl.bulbs}
    tl_lamps = {
        tl.id: [
            {"x": lamp.pos.x, "y": lamp.pos.y, "z": lamp.pos.z, "color": lamp.color, "arrow": lamp.arrow}
            for lamp in tl.lamps
        ]
        for tl in traffic_lights
        if tl.bulbs
    }
    tl_heads = {
        tl.id: [
            {"x": h.pos.x, "y": h.pos.y, "z": h.pos.z, "panel_width": h.panel_width, "panel_height": h.panel_height}
            for h in tl.heads
        ]
        for tl in traffic_lights
        if tl.bulbs
    }
    yaw_lookup = _build_lane_yaw_lookup(lanes)
    latlon_transform = parse_latlon_transform(xml_string)
    signal_heads = parse_signal_heads(xml_string, nodes)

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
        p["heads_visible"], p["heads_total"] = compute_point_head_counts(results_by_point[p["id"]])
        p["min_heads_visible"] = compute_point_min_visible(results_by_point[p["id"]])

    _state.clear()
    _state.update(
        cameras=list(cameras),
        signal_types=signal_types,
        points=points,
        results_by_point=results_by_point,
        tl_positions=tl_positions,
        tl_facing_yaw=tl_facing_yaw,
        tl_signal_type=tl_signal_type,
        tl_panel_size=tl_panel_size,
        tl_lamps=tl_lamps,
        tl_heads=tl_heads,
        signal_heads=signal_heads,
        yaw_lookup=yaw_lookup,
        latlon_transform=latlon_transform,
        lane_count=len(lanes),
        traffic_light_count=len(traffic_lights),
    )


def _serialize_state() -> dict:
    """The current `_state` as a JSON-safe dict, compact enough to hand to
    someone else or reload later without the (large, not redistributable)
    source .osm file. `_deserialize_state` is the exact inverse.
    """
    signal_types: set[str] | None = _state["signal_types"]
    return {
        "format_version": _SNAPSHOT_FORMAT_VERSION,
        "cameras": [_camera_dict(cam) for cam in _state["cameras"]],
        "signal_types": sorted(signal_types) if signal_types is not None else None,
        "latlon_transform": _state["latlon_transform"],
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
                    "heads_total": r.heads_total,
                    "heads_visible": r.heads_visible,
                    "camera_name": r.camera_name,
                }
                for r in candidates
            ]
            for point_id, candidates in _state["results_by_point"].items()
        },
        "tl_positions": {tl_id: {"x": p.x, "y": p.y, "z": p.z} for tl_id, p in _state["tl_positions"].items()},
        "tl_facing_yaw": _state["tl_facing_yaw"],
        "tl_signal_type": _state["tl_signal_type"],
        "tl_panel_size": _state["tl_panel_size"],
        "tl_lamps": _state["tl_lamps"],
        "tl_heads": _state["tl_heads"],
        "signal_heads": _state["signal_heads"],
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

    cameras = [CameraSpec(**cam) for cam in data["cameras"]]
    points = data["points"]
    tl_positions = {tl_id: Point3D(**pos) for tl_id, pos in data["tl_positions"].items()}
    tl_facing_yaw = data["tl_facing_yaw"]
    tl_signal_type = data["tl_signal_type"]
    tl_panel_size = data["tl_panel_size"]
    tl_lamps = data["tl_lamps"]
    tl_heads = data["tl_heads"]
    signal_heads = data["signal_heads"]
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
                heads_total=c["heads_total"],
                heads_visible=c["heads_visible"],
                camera_name=c["camera_name"],
            )
            for c in candidates
        ]

    signal_types_list = data["signal_types"]
    _state.clear()
    _state.update(
        cameras=cameras,
        signal_types=set(signal_types_list) if signal_types_list is not None else None,
        points=points,
        results_by_point=results_by_point,
        tl_positions=tl_positions,
        tl_facing_yaw=tl_facing_yaw,
        tl_signal_type=tl_signal_type,
        tl_panel_size=tl_panel_size,
        tl_lamps=tl_lamps,
        tl_heads=tl_heads,
        signal_heads=signal_heads,
        yaw_lookup=yaw_lookup,
        latlon_transform=data["latlon_transform"],
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


@app.route("/api/load_snapshot", methods=["POST"])
def load_snapshot():
    """Replace the served results with an uploaded snapshot (the request body
    is the raw .json.gz -- or plain .json -- file, same format as
    /api/export), so a shared run can be inspected without restarting the
    server with --load.
    """
    raw = request.get_data()
    try:
        text = gzip.decompress(raw).decode("utf-8")
    except OSError:
        text = raw.decode("utf-8")
    try:
        _deserialize_state(json.loads(text))
    except (ValueError, KeyError, TypeError) as exc:
        abort(400, description=f"not a valid results snapshot: {exc}")
    return jsonify({"ok": True, "point_count": len(_state["points"])})


@app.route("/api/load_map", methods=["POST"])
def load_map():
    """Replace the served results by parsing an uploaded .osm file (raw XML
    request body) and re-running the simulation with the current camera
    spec and signal-type filter. Synchronous on purpose -- the run takes
    ~20-30s on a city-scale map and the frontend just shows a "computing"
    notice until this returns.
    """
    xml_string = request.get_data(as_text=True)
    cameras: list[CameraSpec] = _state["cameras"]
    signal_types: set[str] | None = _state["signal_types"]
    try:
        _load_from_xml(xml_string, cameras, signal_types)
    except ET.ParseError as exc:
        abort(400, description=f"not parseable as OSM XML: {exc}")
    return jsonify({"ok": True, "point_count": len(_state["points"])})


@app.route("/api/meta")
def meta():
    return jsonify(
        {
            "cameras": [_camera_dict(cam) for cam in _state["cameras"]],
            "lane_count": _state["lane_count"],
            "traffic_light_count": _state["traffic_light_count"],
            "point_count": len(_state["points"]),
            "latlon_transform": _state["latlon_transform"],
        }
    )


@app.route("/api/points")
def points():
    return jsonify(_state["points"])


_COLOR_ORDER = {"green": 0, "yellow": 1, "red": 2}
_ARROW_ORDER = {"left": 0, "up": 1, "straight": 2, "right": 3}


def _head_signature(head: dict) -> str:
    """Canonical, human-readable pattern signature for one physical head.

    Built from what a recognition model has to distinguish: signal type,
    housing orientation (from the panel's aspect ratio), plain lens colors,
    and arrow lamps with their directions. Bulb *order within the way* is
    deliberately ignored (canonical G/Y/R and left/up/straight/right
    ordering instead), so two identical housings digitized in opposite
    directions still count as the same pattern. Panel size is left out of
    the signature -- real widths vary by a few cm per installation, which
    would fragment the counts into near-duplicates.
    """
    plain = sorted(
        (lamp["color"] or "?") for lamp in head["lamps"] if not lamp["arrow"]
    )
    plain.sort(key=lambda c: _COLOR_ORDER.get(c, 9))
    arrows = sorted(
        (lamp["arrow"] for lamp in head["lamps"] if lamp["arrow"]),
        key=lambda a: _ARROW_ORDER.get(a, 9),
    )
    w, h = head["panel_width"], head["panel_height"]
    if w is not None and h is not None:
        orientation = "vertical" if h > w else "horizontal"
    else:
        orientation = "unknown-orientation"
    parts = [head["signal_type"], orientation, "+".join(plain) if plain else "no-plain-lens"]
    if arrows:
        parts.append("arrows:" + "+".join(arrows))
    return " | ".join(parts)


@app.route("/api/patterns")
def patterns():
    """The signal-hardware population, aggregated by per-head pattern
    signature -- 'what must the recognizer be able to see, and how many of
    each are there'. Heads, not regulatory elements: a relation bundling
    two identical housings contributes two counts of one pattern.
    """
    by_signature: dict[str, dict] = {}
    for head in _state["signal_heads"]:
        sig = _head_signature(head)
        entry = by_signature.setdefault(
            sig,
            {"signature": sig, "count": 0, "signal_type": head["signal_type"], "heads": []},
        )
        entry["count"] += 1
        entry["heads"].append(
            {
                "way_id": head["way_id"],
                "relation_id": head["relation_id"],
                "x": head["x"],
                "y": head["y"],
                "panel_width": head["panel_width"],
                "panel_height": head["panel_height"],
                "lamps": head["lamps"],
            }
        )
    ordered = sorted(by_signature.values(), key=lambda e: -e["count"])
    return jsonify({"total_heads": len(_state["signal_heads"]), "patterns": ordered})


@app.route("/api/traffic_lights")
def traffic_lights():
    tl_facing_yaw = _state["tl_facing_yaw"]
    tl_signal_type = _state["tl_signal_type"]
    return jsonify(
        [
            {
                "id": tl_id,
                "x": pos.x,
                "y": pos.y,
                "facing_yaw": tl_facing_yaw.get(tl_id),
                "signal_type": tl_signal_type.get(tl_id, "unknown"),
            }
            for tl_id, pos in _state["tl_positions"].items()
        ]
    )


@app.route("/api/points/<int:point_id>/candidates")
def point_candidates(point_id: int):
    points_list = _state["points"]
    if point_id < 0 or point_id >= len(points_list):
        abort(404, description="unknown point id")

    p = points_list[point_id]
    cameras: list[CameraSpec] = _state["cameras"]
    cam_by_name = {cam.name: cam for cam in cameras}
    ref_cam = cameras[0]
    cam_yaw = _state["yaw_lookup"][p["lane_id"]][(p["x"], p["y"])]
    # All drawing projections use one shared frame -- the vehicle-heading
    # angle space at the first camera's height -- so every camera's FOV
    # rectangle and every light plot into the same panel. Per-camera
    # *visibility* checks below use each camera's own height/axis/FOV.
    cam_pos = Point3D(p["x"], p["y"], p["z"] + ref_cam.height)

    point_results = _state["results_by_point"][point_id]
    group_covered = {r.group_id for r in point_results if r.is_covered}

    tl_panel_size = _state["tl_panel_size"]
    tl_lamps = _state["tl_lamps"]
    tl_heads = _state["tl_heads"]
    tl_facing_yaw = _state["tl_facing_yaw"]
    candidates = []
    for r in point_results:
        cam = cam_by_name.get(r.camera_name, ref_cam)
        r_cam_pos = Point3D(p["x"], p["y"], p["z"] + cam.height)
        target_pos = _state["tl_positions"][r.target_tl_id]
        yaw_diff, pitch_diff = calc_camera_frame_offset(cam_pos, cam_yaw, 0.0, target_pos)
        panel_width, panel_height = tl_panel_size.get(r.target_tl_id) or (None, None)

        # each physical bulb projected individually, so the frontend can
        # draw the true lamp arrangement (incl. foreshortening when the
        # housing is seen at an angle) instead of a symbolic box alone
        lamps = []
        for lamp in tl_lamps.get(r.target_tl_id, []):
            lamp_pos = Point3D(lamp["x"], lamp["y"], lamp["z"])
            lamp_yaw, lamp_pitch = calc_camera_frame_offset(cam_pos, cam_yaw, 0.0, lamp_pos)
            lamps.append(
                {"yaw_diff": lamp_yaw, "pitch_diff": lamp_pitch, "color": lamp["color"], "arrow": lamp["arrow"]}
            )

        # per-head projection (shared frame) + the same visibility check
        # the simulator ran for THIS result's camera (identical geometry
        # functions and inputs), so the frame view can draw each physical
        # housing at its own position with its own per-camera state
        facing_yaw = tl_facing_yaw.get(r.target_tl_id)
        heads = []
        for h in tl_heads.get(r.target_tl_id, []):
            head_pos = Point3D(h["x"], h["y"], h["z"])
            head_yaw, head_pitch = calc_camera_frame_offset(cam_pos, cam_yaw, 0.0, head_pos)
            head_in_fov = check_fov_inclusion(
                cam_pos=r_cam_pos, cam_yaw=cam_yaw + cam.yaw_offset, cam_pitch=cam.pitch_offset,
                target_pos=head_pos, fov_h=cam.fov_h, fov_v=cam.fov_v,
            )
            head_facing = True if facing_yaw is None else check_light_facing_camera(
                tl_pos=head_pos, tl_facing_yaw=facing_yaw, cam_pos=r_cam_pos,
                max_angle_diff=cam.facing_tolerance_deg,
            )
            heads.append(
                {
                    "yaw_diff": head_yaw,
                    "pitch_diff": head_pitch,
                    "panel_width": h["panel_width"],
                    "panel_height": h["panel_height"],
                    "visible": head_in_fov and head_facing,
                }
            )

        candidates.append(
            {
                "target_tl_id": r.target_tl_id,
                "camera_name": r.camera_name,
                "signal_type": r.signal_type,
                "group_id": r.group_id,
                "group_covered": r.group_id in group_covered,
                "distance_m": r.distance_m,
                "in_fov": r.in_fov,
                "facing_camera": r.facing_camera,
                "is_covered": r.is_covered,
                "heads_total": r.heads_total,
                "heads_visible": r.heads_visible,
                "yaw_diff": yaw_diff,
                "pitch_diff": pitch_diff,
                "norm_x": yaw_diff / (ref_cam.fov_h / 2.0),
                "norm_y": pitch_diff / (ref_cam.fov_v / 2.0),
                "panel_width": panel_width,
                "panel_height": panel_height,
                "lamps": lamps,
                "heads": heads,
            }
        )

    return jsonify(
        {
            "point": {"lane_id": p["lane_id"], "x": p["x"], "y": p["y"]},
            "status": compute_point_status(point_results),
            "cam_pos": {"x": cam_pos.x, "y": cam_pos.y, "z": cam_pos.z},
            "cam_yaw": cam_yaw,
            "fov_h": ref_cam.fov_h,
            "fov_v": ref_cam.fov_v,
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
        active_overrides = {k: v for k, v in camera_overrides.items() if v is not None}
        if active_overrides and len(config.cameras) > 1:
            parser.error("per-camera CLI flags don't apply to a multi-camera `cameras:` config -- edit the YAML instead")
        cameras = [replace(config.camera, **active_overrides)] if len(config.cameras) == 1 else list(config.cameras)

        signal_type = args.signal_type or config.signal_type
        signal_types = None if signal_type == "both" else {signal_type}

        map_path = args.map or (Path(config.map_path) if config.map_path else None)
        if map_path is None:
            parser.error(
                "no map specified: pass --map <file.osm>, set `map:` in a --config YAML file, "
                "or use --load to load previously --save'd results"
            )

        print(f"Loading {map_path} ...")
        _load_data(map_path, cameras, signal_types)

    if args.save:
        _write_snapshot(args.save, _serialize_state())
        print(f"Saved results to {args.save}")

    print(f"Loaded {len(_state['points'])} waypoints, {_state['traffic_light_count']} traffic lights.")
    print(f"Open http://127.0.0.1:{args.port} in a browser.")
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
