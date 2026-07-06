# Known behavior notes

Things that are easy to misread as bugs when looking at the tool's output,
along with why they happen and what (if anything) was done about them.

## The plotted road network used to look broken

**Symptom:** the saved plot showed the road network as disconnected
clusters of colored dots with blank gaps between them, even though the
underlying map has no actual gaps.

**Root cause:** `run_simulation` only produces a `ValidationResult` for
waypoints within `[camera.min_range, camera.max_range]` (50-200m by
default) of at least one traffic light -- this is intentional, since a
waypoint far from every light has nothing meaningful to evaluate. But the
old `plot_results` only ever drew points taken from `results`, so any
mid-block stretch of road farther than `max_range` from every light (e.g.
the ~1.5km link between the two intersection clusters in the bundled
Odaiba map) was silently never drawn at all -- it wasn't a parsing failure,
just an invisible absence.

Confirmed empirically before changing anything: parsing the full map
(`lanelet2_map.osm`) produces exactly 5234 `LanePath`s for the 5234
`lanelet subtype=road` relations in the source data (see `main.py`'s
"Parsed ... lanes" line vs. `grep -c 'subtype" v="road"'` on the map) --
i.e. no lanelet is silently dropped during parsing. Plotting every lane's
full `center_line` directly (bypassing the range filter) reproduced a
single continuous road network with no gaps, which is what proved the
missing links were a visualization omission rather than a data or geometry
bug.

**Fix:** `plot_results` (in `visualizer.py`) now accepts an optional
`lanes: list[LanePath]` argument. When provided, it draws every lane's
`center_line` as a thin grey line *underneath* the coverage-colored
scatter points, so un-evaluated stretches still render as road instead of
as a break in the map. `main.py` always passes the full parsed `lanes`
list. This is purely additive to `ValidationResult`/scoring -- the
underlying coverage numbers are unaffected, only what's visible in the
plot changed.

## Pedestrian signals never show as "facing away"

Their `facing_yaw` is `None` for effectively all pedestrian signals in the
bundled Odaiba map, because pedestrian-signal regulatory elements have no
`ref_line` (stop line) member to derive a facing direction from -- vehicle
stop lines don't apply to pedestrian crossings. `check_light_facing_camera`
treats an unknown `facing_yaw` as "always faces the camera" rather than
guessing, so pedestrian coverage numbers reflect FOV geometry only, not an
orientation check. See `map_parser.py`'s `parse_traffic_lights` docstring.
