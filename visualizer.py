"""Module D: Visualizer.

The only module allowed to know about matplotlib. Takes already-computed
results and draws them; it never recomputes geometry or touches XML.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")  # headless-safe: never opens a GUI window
import matplotlib.pyplot as plt

from fov_simulator import compute_point_status
from models import LanePath, TrafficLight, ValidationResult


def plot_results(
    results: list[ValidationResult],
    traffic_lights: list[TrafficLight],
    lanes: list[LanePath] | None = None,
    blind_only: bool = False,
    save_path: str | None = "fov_coverage_result.png",
    show: bool = False,
) -> None:
    """Plot waypoints and traffic lights (gold stars) in 2D, color-coded by why a
    waypoint is or isn't covered:
      - green:  covered (in the camera's FOV and the signal faces the camera)
      - orange: light is in FOV geometrically, but its face points away
                (camera would only see the housing, not a lit lamp)
      - red:    light isn't in the camera's FOV at all

    A single waypoint often has more than one candidate light (e.g. this
    intersection's signal and the next one down the road both within
    range) -- 94.7% of waypoints do, on the bundled Odaiba map. Each
    waypoint is plotted once, using `compute_point_status`
    (fov_simulator.py) to combine all of its candidates into a single
    verdict: covered only if every distinct signal *group* present (see
    `TrafficLight.group_id` -- redundant heads for the same stop line only
    need one of them visible) has at least one covered member, otherwise
    facing_away/out_of_fov for whichever reason applies. See
    docs/behavior.md for the two mistakes this replaced: plotting every
    candidate as its own point (letting an unrelated covered light mask a
    real gap at the same pixel), and requiring every individual light --
    including redundant ones -- to be independently covered.

    `results` only contains candidates that are within [camera.min_range,
    camera.max_range], plausibly meant for the lane's direction of travel,
    and not already behind the camera along the route (see
    `run_simulation`'s docstring and docs/behavior.md) -- so every red/
    orange point here reflects an actual camera-FOV/orientation limitation,
    not routing noise. Mid-block stretches farther than max_range from
    every light are never evaluated at all, by design. Passing `lanes`
    draws the full road network as a thin grey base layer underneath, so
    those un-evaluated stretches still show up as road rather than reading
    as a break in the map.

    `blind_only=True` skips the "Covered" (green) layer entirely, showing
    only where the camera spec falls short -- useful once a full plot gets
    too dense to spot the gaps in.
    """
    fig, ax = plt.subplots(figsize=(12, 10))

    if lanes:
        for lane in lanes:
            ax.plot(
                [p.x for p in lane.center_line],
                [p.y for p in lane.center_line],
                color="lightgrey",
                linewidth=0.8,
                zorder=1,
            )
        ax.plot([], [], color="lightgrey", linewidth=2, label="Road (not evaluated: out of range of any light)")

    by_point: dict[tuple[str, float, float], list[ValidationResult]] = {}
    for r in results:
        by_point.setdefault((r.lane_id, r.point.x, r.point.y), []).append(r)

    points_by_status: dict[str, list[tuple[float, float]]] = {"covered": [], "facing_away": [], "out_of_fov": []}
    for (_, x, y), rs in by_point.items():
        points_by_status[compute_point_status(rs)].append((x, y))
    if blind_only:
        points_by_status["covered"] = []

    for zorder, (status, color, label) in enumerate(
        (
            ("covered", "green", "Covered"),
            ("facing_away", "orange", "In FOV, light facing away"),
            ("out_of_fov", "red", "Out of FOV"),
        ),
        start=2,
    ):
        pts = points_by_status[status]
        if pts:
            ax.scatter(
                [p[0] for p in pts],
                [p[1] for p in pts],
                c=color,
                s=6,
                label=f"{label} ({len(pts)})",
                zorder=zorder,
            )

    tl_x = [bulb.x for tl in traffic_lights for bulb in tl.bulbs]
    tl_y = [bulb.y for tl in traffic_lights for bulb in tl.bulbs]
    if tl_x:
        ax.scatter(
            tl_x,
            tl_y,
            c="gold",
            marker="*",
            s=120,
            edgecolors="black",
            linewidths=0.5,
            label="Traffic light",
            zorder=5,
        )

    ax.set_xlabel("Local X [m]")
    ax.set_ylabel("Local Y [m]")
    ax.set_title("Traffic Light FOV Coverage -- Blind Spots Only" if blind_only else "Traffic Light FOV Coverage")
    ax.set_aspect("equal", adjustable="datalim")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
    if show:
        plt.show()
    plt.close(fig)
