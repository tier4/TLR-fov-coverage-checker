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
| `--map` | `map` (top-level) | path to the Lanelet2 `.osm` file | *(required)* |
| `--output` | `output` (top-level) | output plot path | `fov_coverage_result.png` |

The printed report breaks coverage down by signal type and by *why* a
waypoint is uncovered (out of the camera's FOV vs. the signal facing away
from it), and the saved plot color-codes the same three cases.

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
