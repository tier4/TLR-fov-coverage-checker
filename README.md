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

### Interactive viewer

`main.py` produces a static plot; `webapp.py` is for drilling into *why* a
specific point is red/orange -- it runs the same simulation once, then
serves a small local page where you click a waypoint and see, literally,
where each candidate light falls inside (or outside) the camera's FOV
rectangle, next to a numeric table of the same values:

```bash
python3 webapp.py --config camera_spec.yaml
# open http://127.0.0.1:8000
```

Takes the same `--map`/`--config`/camera flags as `main.py`, plus `--port`.
It's a fixed-camera-spec viewer (no live FOV sliders) -- restart with
different flags to inspect a different camera spec.

Both panes support scroll-to-zoom and drag-to-pan (double-click the
camera-view panel to reset it back to auto-fit). Selecting a point also
draws its FOV frustum on the map itself -- a blue wedge from `min_range`
to `max_range` spanning `fov_h`, with a dashed line down the middle showing
`cam_yaw` (the lane's direction of travel at that point) -- and enlarges
its candidate traffic lights' markers, colored by that candidate's
status, so it's clear at a glance which one(s) on the map the camera-view
panel is showing. The "Point size" slider above the map scales the
waypoint dots if they're too small to make out at the zoom level you're at.

Each traffic light is drawn as a triangle pointing in the direction that
signal actually faces (`facing_yaw`, from `/api/traffic_lights`) -- a
single solid shape rather than a separate arrow overlay, since a thin
line degrades badly at small marker sizes/low zoom while a filled wedge
stays legible. Zoom into an intersection and you can see directly whether
a light's own orientation actually points back down a given lane's
approach, instead of having to infer it from a
`check_light_relevant_to_lane` pass/fail. Color distinguishes signal
type when a marker isn't currently highlighted: gold for vehicle, cyan
for pedestrian. A plain circle (no triangle) means facing_yaw is unknown
-- no `ref_line` in the map, normally a pedestrian signal, since those
usually lack a stop line reference entirely.

CLI flags always take precedence over a `--config` YAML, which takes
precedence over built-in defaults (`CameraSpec` in `models.py`).

**Sharing a specific point:** selecting a point updates the browser's own
URL with `?lane=...&x=...&y=...` -- just copy the address bar, or click
"Copy link to this point" next to the candidate table. Pasting that URL
(to yourself later, or to someone else running the same snapshot/map)
re-selects and centers on the exact same waypoint automatically. It's
keyed by lane id + coordinates rather than the point's internal array
index, so it still resolves correctly even if a rerun's filtering changes
which index that waypoint happens to land on.

**Saving/loading a run:** computing a run takes ~20-30s and needs the
(large, not redistributable) `.osm` file -- `--save PATH` freezes the
computed result set to a gzip-compressed JSON snapshot, and `--load PATH`
reloads one later (or on another machine) in about a second, skipping map
parsing and the simulation entirely:

```bash
# compute once, freeze the result, keep serving
python3 webapp.py --config camera_spec.yaml --save run1.json.gz

# later (or from the .json.gz file alone, no .osm needed)
python3 webapp.py --load run1.json.gz
```

A running instance can also be exported on demand via the "Download
results" link in the header (`GET /api/export`), without needing a
`--save` flag or a server restart. The "Data:" row under the candidate
table does the same jobs without touching the terminal at all: save the
current results, load a previously saved `.json.gz`/`.json` snapshot
(`POST /api/load_snapshot`), or pick a different `.osm` file and re-run
the simulation on it with the current camera spec (`POST /api/load_map`,
takes ~30s).

**Google Maps / Street View:** the selected-point row links straight to
the same location in Google Maps (pin at the waypoint) and Street View
(panorama looking in the direction of travel). Coordinates come from a
per-map affine fit between the nodes' `local_x`/`local_y` tags and their
`lat`/`lon` attributes (`parse_latlon_transform`) -- accurate to well
under a meter at city scale.

The camera-view panel renders through the windshield: L is the vehicle's
left, the dashed FOV rectangle keeps its true `fov_h x fov_v` aspect
ratio, and the blue line is the horizon (the camera is modeled level,
pitch 0).

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
| `--point-size` | `point_size` (top-level) | matplotlib marker area (`s=`) for each waypoint dot -- bump it up if points are too small to see | 6.0 |
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

Before that per-waypoint check runs, two pre-filters decide whether a
light is even a candidate for this lane at all.

**Which lane a light belongs to** is decided by the map itself wherever
possible, not by geometry, tried in order by `_build_lane_relevant_tl_ids`
(`fov_simulator.py`):

1. `parse_lanes` reads each lanelet's own `regulatory_element` reference
   (`LanePath.direct_tl_ids`) -- the map author's explicit statement of
   which signal(s) control that lane. Only ~20% of lanelets carry this
   directly (typically just the one immediately approaching a stop line).
2. Walk forward through `next_lane_ids` (lanelets connected via shared
   boundary endpoints) to inherit a reference from a downstream lanelet
   within `camera.max_range`.
3. Lanelet connectivity graphs are frequently incomplete right at
   intersections -- confirmed on the bundled map, a lane can dead-end (or
   run past `camera.max_range`) before the walk in (2) ever reaches
   anywhere tagged, well short of where the road actually continues. As a
   second resort, check whether any traffic light's stop line sits within
   30m of this lane's own mapped end point (`STOP_LINE_PROXIMITY_M`) --
   skipped when multiple stop lines are similarly close (a common
   situation at complex intersections) rather than guess wrong.

Together these recover an authoritative answer for the large majority of
lanelets on the bundled map. Only when none of the three applies does it
fall back to a geometric heuristic (facing_yaw vs. lane heading), which a
skewed (non-square) intersection -- or, per `docs/behavior.md`, a handful
of very close stop lines at once -- can fool: a cross-street signal can
end up "more than `LANE_DIRECTION_THRESHOLD_DEG` (120) degrees" off a
lane's heading by coincidence and get treated as relevant when it has
nothing to do with that lane:

```
Plan view: a light only belongs to the direction it faces (fallback only --
see docs/behavior.md for why the map's own regulatory_element reference is
used first)
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

  relevant = |angle(facing_yaw, lane_heading)| > 120deg   (else skip this
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
`check_light_relevant_to_lane` (fallback only), `check_target_ahead` and
`calc_heading_yaw` in `geometry_calculator.py` implement exactly this, and
are pure functions -- see their docstrings and
`tests/test_geometry_calculator.py` for the boundary cases (e.g. a target
exactly on the fov_h/2 edge, or a light exactly on the 90-degree
relevance/ahead threshold). `calc_camera_frame_offset` exposes the same
yaw/pitch-diff geometry as a raw offset instead of a boolean, which is
what the interactive viewer (`webapp.py`) uses to place each candidate
light inside its FOV-rectangle rendering. `_build_lane_relevant_tl_ids`
(`fov_simulator.py`) is the map-topology-based filter that runs before the
fallback ever gets a chance -- see the section below and
`tests/test_fov_simulator.py`'s `test_build_lane_relevant_tl_ids_*` and
`test_run_simulation_map_authoritative_reference_*` tests.

### From per-light candidates to a per-waypoint verdict

`run_simulation` returns one `ValidationResult` per (waypoint, candidate
light) pair, not per waypoint -- a single waypoint often has several
candidates, including redundant signal heads for the very same stop line
(a through light and a separate turn-arrow light are frequently two
distinct `TrafficLight`s in the source map, 67 of 501 stop lines in the
bundled Odaiba map). `compute_point_status` (`fov_simulator.py`) is the
aggregation step every consumer (`main.py`'s printed stats,
`visualizer.py`'s plot, `webapp.py`'s API) shares: it groups a waypoint's
candidates by `TrafficLight.group_id` (the shared `ref_line` stop-line
way, so redundant heads only need one of them visible) and calls the
waypoint covered only if *every distinct group present* has at least one
covered member. See `docs/behavior.md` for why this made zero difference
to the bundled map's numbers (redundant heads turned out to always be
close enough together to pass or fail as a unit) and what a remaining
red-point-next-to-a-green-star case actually means instead.

## Architecture

Each module is independently testable and has a single responsibility:

- `models.py` -- shared dataclasses (`Point3D`, `TrafficLight`, `LanePath`, `CameraSpec`, `ValidationResult`)
- `geometry_calculator.py` -- pure math only (distance, center line, FOV/facing checks); no I/O
- `map_parser.py` -- Lanelet2 XML -> dataclasses; takes XML/YAML text, not file paths, so tests don't touch disk
- `config.py` -- YAML text -> `AppConfig`
- `fov_simulator.py` -- combines Modules A + B's output to run the simulation
- `visualizer.py` -- the only module that imports matplotlib
- `webapp.py` -- the only module that imports Flask; serves the interactive point-inspection viewer (`static/`) over a small JSON API
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
