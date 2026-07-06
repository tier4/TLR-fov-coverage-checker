# Lanelet2 Traffic Light FOV Coverage Checker

Given a Lanelet2 (`.osm`) map and a camera spec (mount height, FOV, detection
range, facing-angle tolerance), checks whether a vehicle driving every lane
would actually have each traffic light in view, and plots the result.

Not bundled with this repo: any `.osm` map or `.csv` file. Point the tool at
your own map via `--map` or a `map:` key in a YAML config.

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Minimal
python3 main.py --map path/to/map.osm

# Via a YAML config (see camera_spec.yaml for a template)
python3 main.py --config camera_spec.yaml

# YAML as a base profile, one-off override on top
python3 main.py --config camera_spec.yaml --fov-h 60 --signal-type pedestrian

# Only plot the gaps -- useful once a full plot gets too dense to read
python3 main.py --config camera_spec.yaml --blind-only
```

CLI flags always take precedence over a `--config` YAML, which takes
precedence over built-in defaults (`CameraSpec` in `models.py`).

Available settings (CLI flag / YAML key under `camera:`):

| CLI flag | YAML key | Meaning | Default |
|---|---|---|---|
| `--cam-height` | `height` | camera mount height [m] | 3.0 |
| `--fov-h` | `fov_h` | horizontal FOV [deg] | 30.0 |
| `--fov-v` | `fov_v` | vertical FOV [deg] | 17.0 |
| `--min-range` | `min_range` | min detection range [m] | 50.0 |
| `--max-range` | `max_range` | max detection range [m] | 200.0 |
| `--facing-tolerance` | `facing_tolerance_deg` | max angle between the signal's face and the camera for it to still be legible [deg] | 45.0 |
| `--signal-type` | `signal_type` (top-level) | `vehicle` / `pedestrian` / `both` | both |
| `--blind-only` / `--no-blind-only` | `blind_only` (top-level) | plot only uncovered waypoints, hiding the Covered/green layer | false |
| `--map` | `map` (top-level) | path to the Lanelet2 `.osm` file | *(required)* |
| `--output` | `output` (top-level) | output plot path | `fov_coverage_result.png` |

The printed report breaks coverage down by signal type and by *why* a
waypoint is uncovered (out of the camera's FOV vs. the signal facing away
from it), and the saved plot color-codes the same three cases (or, with
`--blind-only`, only the two "uncovered" cases).

## How coverage is computed

For every 1m waypoint on every lane, a camera at `point.z + camera.height`
looks toward the next waypoint (`cam_yaw`, pitch fixed at 0) and is checked
against every traffic light within range.

Before that per-waypoint check runs, a light is skipped for a lane
entirely if it doesn't plausibly belong to that lane's direction of
travel -- opposite-direction lanelets on the same physical road are only
a few meters apart, so without this a light meant for northbound traffic
would also get scored (as a blind spot) against the adjacent southbound
lane it was never meant to regulate:

```
Plan view: a light only belongs to the direction it faces
==========================================================

        northbound lane  ->  o->  o->  o->  o->  *  (light facing south,
                                                      back at northbound
                                                      traffic: relevant)
                                                  |
        southbound lane  <-  o<-  o<-  o<-  o<-  |  (same light: facing the
                                                     *same* way this lane
                                                     travels -> belongs to
                                                     the other lane, skipped
                                                     entirely, not scored)

  relevant = |angle(facing_yaw, lane_heading)| > 90deg   (else skip this
                                                            (lane, light) pair)
```

A second, independent pre-filter then drops any light the camera has
already driven past along the route -- a forward-facing camera not seeing
something behind it isn't a camera-spec gap worth reporting:

```
Plan view: a light already behind the camera is skipped too
==============================================================

  already-passed light *  --------  o ------->  o -------> next light *
                          (behind, > 90deg              (ahead, within
                           off cam_yaw: skipped,          90deg of cam_yaw:
                           not scored at all)             still a candidate)

  ahead = |angle(cam_yaw, bearing(camera -> light))| <= 90deg   (much wider
                                                                  than fov_h --
                                                                  a route-
                                                                  position
                                                                  filter, not
                                                                  the real FOV
                                                                  check below)
```

```
Top-down view, one lane waypoint vs. one traffic light
=======================================================

                                    stop line midpoint
                                            :
                                            : (only used to derive facing_yaw)
                                            :
                                facing_yaw  v
                                     _______*_______   <- traffic light
                                    /       |       \     (bulb centroid)
                                   /        |        \
                          facing_tolerance_deg (each side of facing_yaw)
                                 /          |          \
                                .           |           .
                               .    "legible zone" --   .
                              .   camera must be in    .
                                  here for facing_camera
                                  to be True
                                            :
                                            : <-- distance must satisfy
                                            :     min_range <= d <= max_range
                                            :     (else never evaluated at all)
                                    fov_h/2 : fov_h/2
                                       \    :    /
                                        \   :   /
                                         \  :  /
                                     _____\ : /_____
                          cam_yaw  <-------o        <- camera (this waypoint,
                                            |             height = point.z + camera.height)
                                            |
                                      next waypoint
                                (cam_yaw = calc_heading_yaw(point, next_point))

  in_fov         = |angle(cam_yaw, bearing(camera -> light))|    <= fov_h / 2   (fov_v checked the same way, using pitch)
  facing_camera  = |angle(facing_yaw, bearing(light -> camera))| <= facing_tolerance_deg   (True if facing_yaw is unknown)
  is_covered     = in_fov AND facing_camera
```

```
Side view, same waypoint (vertical FOV / pitch check)
======================================================

                                traffic light
                                      *
                                     /:
                                    / :
                                   /  : dz = light.z - camera.z
                          fov_v/2 /   :
                                 /    :
                                /_ _ _:
                               o ---------------  horizontal_dist
                          camera (point.z + camera.height)

  target_pitch = atan2(dz, horizontal_dist)   (cam_pitch is fixed at 0.0)
```

`check_fov_inclusion`, `check_light_facing_camera`,
`check_light_relevant_to_lane`, `check_target_ahead` and `calc_heading_yaw`
in `geometry_calculator.py` implement exactly this, and are pure functions --
see their docstrings and `tests/test_geometry_calculator.py` for the
boundary cases (e.g. a target exactly on the fov_h/2 edge, or a light
exactly on the 90-degree relevance/ahead threshold).

## Architecture

Each module is independently testable and has a single responsibility:

- `models.py` -- shared dataclasses (`Point3D`, `TrafficLight`, `LanePath`, `CameraSpec`, `ValidationResult`)
- `geometry_calculator.py` -- pure math only (distance, center line, FOV/facing checks); no I/O
- `map_parser.py` -- Lanelet2 XML -> dataclasses; takes XML/YAML text, not file paths, so tests don't touch disk
- `config.py` -- YAML text -> `AppConfig`
- `fov_simulator.py` -- combines Modules A + B's output to run the simulation
- `visualizer.py` -- the only module that imports matplotlib
- `main.py` -- CLI wiring

## Testing

```bash
pytest tests/
```

Parser and config tests use small hand-written XML/YAML strings rather than
real files, so they run without any map data present.

## Notes on the underlying map data

- A traffic light's `signal_type` is classified from its panel way's
  `subtype` tag: `red_yellow_green` -> vehicle, `red_green` -> pedestrian.
- A traffic light's facing direction is derived from the bearing between its
  bulb centroid and its stop line (`ref_line`) midpoint -- this was verified
  against real lane-approach headings and is far more reliable than trying
  to infer facing from the two endpoints of the signal panel way (whose
  ordering is not consistently chiral across real-world exports). Signals
  without an associated stop line (commonly true for pedestrian signals) get
  `facing_yaw = None` and are never excluded by the facing check.

See `docs/behavior.md` for output quirks that look like bugs but aren't
(and the one that was: the plot used to omit mid-block road stretches
outside every light's range, making the map look disconnected).
