"""Entry point: wires Modules A-D together against a real Lanelet2 .osm map.

This is the "given this camera spec, can it see the signals I care about"
harness: describe a candidate camera either in a YAML file (--config, see
camera_spec.yaml) or via --fov-h/--fov-v/--min-range/--max-range/
--facing-tolerance/--signal-type flags. Precedence is CLI flags > YAML
config > built-in CameraSpec defaults, so a config file can hold a base
profile while a flag tweaks just one value for a one-off run.

The map itself is never bundled with this repo (real Lanelet2 exports are
large and often not redistributable) -- point at one with `--map`, or a
`map:` key in a --config YAML file.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import replace
from pathlib import Path

from config import AppConfig, load_config
from fov_simulator import compute_point_min_visible, compute_point_status, run_simulation
from map_parser import parse_lanes, parse_nodes, parse_traffic_lights
from models import ValidationResult
from visualizer import plot_results

HERE = Path(__file__).resolve().parent


def _group_by_point(results: list[ValidationResult]) -> dict[tuple[str, float, float], list[ValidationResult]]:
    by_point: dict[tuple[str, float, float], list[ValidationResult]] = {}
    for r in results:
        by_point.setdefault((r.lane_id, r.point.x, r.point.y), []).append(r)
    return by_point


def _print_breakdown(label: str, results: list[ValidationResult]) -> None:
    """Per-waypoint, not per-(waypoint, light): a waypoint counts as covered
    if every distinct signal group present is (see compute_point_status),
    so redundant heads for the same stop line don't each need to be
    independently visible.
    """
    by_point = _group_by_point(results)
    total = len(by_point)
    if not total:
        print(f"  {label}: no candidates")
        return
    statuses = [compute_point_status(rs) for rs in by_point.values()]
    covered = statuses.count("covered")
    facing_away = statuses.count("facing_away")
    out_of_fov = statuses.count("out_of_fov")
    print(
        f"  {label}: {covered}/{total} covered ({covered / total:.1%}) | "
        f"facing-away {facing_away} | out-of-FOV {out_of_fov}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Lanelet2 traffic light FOV coverage checker")
    parser.add_argument("--config", type=Path, default=None, help="YAML file with camera/signal_type/map/output settings")
    parser.add_argument("--map", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)

    parser.add_argument("--cam-height", type=float, default=None, help="camera mount height [m]")
    parser.add_argument("--fov-h", type=float, default=None, help="horizontal FOV [deg]")
    parser.add_argument("--fov-v", type=float, default=None, help="vertical FOV [deg]")
    parser.add_argument("--min-range", type=float, default=None, help="min detection range [m]")
    parser.add_argument("--max-range", type=float, default=None, help="max detection range [m]")
    parser.add_argument(
        "--facing-tolerance",
        type=float,
        default=None,
        help="max angle [deg] between the signal's face and the camera for it to be legible",
    )
    parser.add_argument(
        "--signal-type",
        choices=["vehicle", "pedestrian", "both"],
        default=None,
        help="restrict the check to this signal population",
    )
    parser.add_argument(
        "--blind-only",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="only plot uncovered waypoints (hide the Covered/green layer)",
    )
    parser.add_argument(
        "--point-size",
        type=float,
        default=None,
        help="matplotlib marker area for each waypoint dot in the saved plot (default 6.0)",
    )
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
    active_overrides = {k: v for k, v in camera_overrides.items() if v is not None}
    if active_overrides and len(config.cameras) > 1:
        parser.error("per-camera CLI flags don't apply to a multi-camera `cameras:` config -- edit the YAML instead")
    cameras = [replace(config.camera, **active_overrides)] if len(config.cameras) == 1 else list(config.cameras)

    signal_type = args.signal_type or config.signal_type
    signal_types = None if signal_type == "both" else {signal_type}
    blind_only = config.blind_only if args.blind_only is None else args.blind_only
    point_size = config.point_size if args.point_size is None else args.point_size

    map_path = args.map or (Path(config.map_path) if config.map_path else None)
    if map_path is None:
        parser.error("no map specified: pass --map <file.osm>, or set `map:` in a --config YAML file (see camera_spec.yaml)")
    output_path = args.output or (Path(config.output_path) if config.output_path else HERE / "fov_coverage_result.png")

    xml_string = map_path.read_text(encoding="utf-8")

    t0 = time.perf_counter()
    nodes = parse_nodes(xml_string)
    lanes = parse_lanes(xml_string, nodes)
    traffic_lights = parse_traffic_lights(xml_string, nodes)
    t1 = time.perf_counter()
    by_type = {t: sum(1 for tl in traffic_lights if tl.signal_type == t) for t in ("vehicle", "pedestrian", "unknown")}
    print(f"Parsed {len(nodes)} nodes, {len(lanes)} lanes, {len(traffic_lights)} traffic lights ({t1 - t0:.1f}s)")
    print(f"  by signal_type: {by_type}")
    for cam in cameras:
        print(
            f"Camera '{cam.name}': height={cam.height}m fov=({cam.fov_h}x{cam.fov_v})deg "
            f"range=[{cam.min_range},{cam.max_range}]m yaw_offset={cam.yaw_offset}deg "
            f"pitch_offset={cam.pitch_offset}deg facing_tolerance={cam.facing_tolerance_deg}deg "
            f"target_signal_type={signal_type}"
        )

    results = run_simulation(lanes, traffic_lights, cameras=cameras, signal_types=signal_types)
    t2 = time.perf_counter()
    print(f"Evaluated {len(results)} waypoint/light/camera candidates within range ({t2 - t1:.1f}s)")

    _print_breakdown("overall (all cameras)", results)
    for st in ("vehicle", "pedestrian"):
        _print_breakdown(st, [r for r in results if r.signal_type == st])
    if len(cameras) > 1:
        for cam in cameras:
            _print_breakdown(f"{cam.name} alone", [r for r in results if r.camera_name == cam.name])

    if results:
        by_point = _group_by_point(results)
        blind_lanes = {lane_id for (lane_id, _, _), rs in by_point.items() if compute_point_status(rs) != "covered"}
        print(f"Lanes with at least one blind waypoint: {len(blind_lanes)} / {len(lanes)}")

        # redundancy: min *absolute* visible head count across a waypoint's
        # groups. 1 = covered with zero margin (one occluded/dirty head
        # loses the signal); 2+ = genuinely redundant observation.
        redundancy_hist: dict[str, int] = {"0 (blind)": 0, "1 (no margin)": 0, "2": 0, "3+": 0}
        for rs in by_point.values():
            n = compute_point_min_visible(rs)
            key = "3+" if n >= 3 else "0 (blind)" if n == 0 else "1 (no margin)" if n == 1 else "2"
            redundancy_hist[key] += 1
        total = len(by_point)
        print(
            "  redundancy (min visible heads per waypoint): "
            + " | ".join(f"{k}: {v} ({v / total:.1%})" for k, v in redundancy_hist.items())
        )

    plotted_lights = traffic_lights if signal_types is None else [tl for tl in traffic_lights if tl.signal_type in signal_types]
    plot_results(
        results,
        plotted_lights,
        lanes=lanes,
        blind_only=blind_only,
        point_size=point_size,
        save_path=str(output_path),
        show=False,
    )
    print(f"Plot saved to {output_path}")


if __name__ == "__main__":
    main()
