"""Module D: Visualizer.

The only module allowed to know about matplotlib. Takes already-computed
results and draws them; it never recomputes geometry or touches XML.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")  # headless-safe: never opens a GUI window
import matplotlib.pyplot as plt

from models import TrafficLight, ValidationResult


def plot_results(
    results: list[ValidationResult],
    traffic_lights: list[TrafficLight],
    save_path: str | None = "fov_coverage_result.png",
    show: bool = False,
) -> None:
    """Plot waypoints and traffic lights (gold stars) in 2D, color-coded by why a
    waypoint is or isn't covered:
      - green:  covered (in the camera's FOV and the signal faces the camera)
      - orange: light is in FOV geometrically, but its face points away
                (camera would only see the housing, not a lit lamp)
      - red:    light isn't in the camera's FOV at all
    """
    fig, ax = plt.subplots(figsize=(12, 10))

    covered = [r for r in results if r.is_covered]
    facing_away = [r for r in results if r.in_fov and not r.facing_camera]
    out_of_fov = [r for r in results if not r.in_fov]

    for zorder, (pts, color, label) in enumerate(
        (
            (out_of_fov, "red", "Out of FOV"),
            (facing_away, "orange", "In FOV, light facing away"),
            (covered, "green", "Covered"),
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
            zorder=4,
        )

    ax.set_xlabel("Local X [m]")
    ax.set_ylabel("Local Y [m]")
    ax.set_title("Traffic Light FOV Coverage")
    ax.set_aspect("equal", adjustable="datalim")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
    if show:
        plt.show()
    plt.close(fig)
