"""Module D: Visualizer.

The only module allowed to know about matplotlib. Takes already-computed
results and draws them; it never recomputes geometry or touches XML.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")  # headless-safe: never opens a GUI window
import matplotlib.pyplot as plt

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
    range) -- 94.7% of waypoints do, on the bundled Odaiba map, and 48% of
    waypoints have a *mixed* outcome (covered for one light, not for
    another) at the exact same (x, y). Since those results occupy the same
    pixel, only the last-drawn one would normally be visible; problems are
    drawn last (on top of a same-point "covered" result) so a real blind
    spot is never hidden by an unrelated light that happens to be fine at
    that spot -- see docs/behavior.md.

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

    covered = [] if blind_only else [r for r in results if r.is_covered]
    facing_away = [r for r in results if r.in_fov and not r.facing_camera]
    out_of_fov = [r for r in results if not r.in_fov]

    # Draw worst-case-per-pixel last (on top): a point with both a covered
    # result (for one candidate light) and an uncovered one (for another)
    # must still read as a problem, not get masked by the "fine" result.
    for zorder, (pts, color, label) in enumerate(
        (
            (covered, "green", "Covered"),
            (facing_away, "orange", "In FOV, light facing away"),
            (out_of_fov, "red", "Out of FOV"),
        ),
        start=2,
    ):
        if pts:
            ax.scatter(
                [r.point.x for r in pts],
                [r.point.y for r in pts],
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
