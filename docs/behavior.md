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

## Couldn't tell which travel direction a covered/blind point belonged to

**Symptom:** opposite-direction lanelets on the same physical road (e.g. a
northbound and southbound lanelet a few meters apart) both get scanned, and
both could have a given traffic light within `[min_range, max_range]`. The
plot had no way to show *which direction* a given colored point was scored
for, so overlapping outbound/inbound results looked ambiguous.

**Root cause, once looked into:** it wasn't only a rendering ambiguity.
Before a lane-direction check existed, `run_simulation` evaluated a
traffic light against *every* lane within range regardless of which
direction that light actually faces. A light meant for the northbound
lane (facing south, back at northbound traffic) was also being evaluated
against the adjacent southbound lane, where it geometrically sits within
the narrow FOV cone too (dead ahead, just facing the wrong way) -- scored
as a blind spot (`in_fov=True, facing_camera=False`) for a lane it was
never meant to regulate. This inflated the "facing away" bucket with
noise unrelated to the camera spec, on top of making direction
indistinguishable.

**Fix:** `check_light_relevant_to_lane(tl_facing_yaw, lane_heading,
threshold_deg=90.0)` (`geometry_calculator.py`) filters candidates
*before* the FOV/facing checks in `run_simulation`: a light facing within
90 degrees of the lane's own travel direction (i.e. shining the same way
the lane travels) belongs to the opposing lane and is skipped entirely,
not scored as a blind spot. Lights with `facing_yaw = None` (pedestrian
signals, see above) skip this filter too, since relevance can't be
evaluated without a facing direction.

Measured impact on the bundled Odaiba map: evaluated candidates dropped
from 3,049,868 to 1,849,440 (-39%), and the "in-FOV-but-facing-away" count
dropped from 454,416 to 137,219 -- the `covered` count (377,200) didn't
change at all, confirming the removed candidates were noise, not signal.

This also resolved the direction-ambiguity concern without needing a
separate offset/"two ribbons" rendering mode: Lanelet2 already models
each direction of travel as its own physically-offset lanelet (own left/
right boundary), so once irrelevant-direction lights stopped being
evaluated, zooming into both a divided road and a dense narrow-street grid
showed the two directions as clearly distinguishable parallel green/red
lines rather than a blended overlap. If a future map turns out to have
directions close enough to still be ambiguous at a glance, revisit the
perpendicular-offset rendering idea then.
