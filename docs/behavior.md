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

## A "covered" (green) point wasn't always fully covered

**Symptom:** asked to confirm that red points mean "this camera can't see
the relevant light(s) here," which is correct, but surfaced a sharper
question: does green reliably mean the opposite -- everything relevant is
seen?

**Root cause:** a single waypoint frequently has more than one candidate
traffic light (e.g. this intersection's signal and the next one down the
road, both within `[min_range, max_range]`) -- on the bundled Odaiba map,
94.7% of the 145,010 evaluated waypoints have more than one candidate
light, and 48% have a *mixed* outcome (covered for at least one light,
not for another) at the exact same `(x, y)`. `plot_results` drew `covered`
(green) last/on top, so a mixed waypoint always rendered as green,
silently hiding the fact that it was also blind to a second light. The
numeric stats in `main.py` were never affected by this (each
`ValidationResult` is counted independently there) -- only the plot could
mislead.

**Fix:** reversed the draw order in `plot_results` so problems draw last
(on top): covered (green) first, facing-away (orange) next, out-of-FOV
(red) last. A mixed waypoint now always shows as a problem rather than
"fine," which fits the tool's purpose -- finding gaps, not papering over
them. This changed nothing about the underlying computation or reported
statistics, only which color wins when multiple results share a pixel.
The visual difference on the bundled map is large: with the previous
"green wins" order the plot looked mostly green with scattered red; with
"problems win" it's now dominated by red, revealing that true full
coverage (every relevant light seen) at a given point is fairly rare with
the default 30x17 degree FOV camera.

## Most "out of FOV" results were just lights the car had already driven past

**Symptom:** asked to add a mode that plots only the uncovered waypoints,
with an explicit warning not to include "it's out of FOV because it's
behind the camera" as a manufactured blind spot.

**Root cause:** `check_light_relevant_to_lane` (see above) only checks
whether a light's facing direction is compatible with a lane's direction
of travel -- it says nothing about *where along the route* the camera
currently is. A light correctly assigned to an eastbound lane (facing
back at eastbound traffic) is relevant for that lane's entire length, but
once the camera has driven past it, that light sits behind the camera,
and a forward-facing camera not seeing something behind it isn't a
camera-spec limitation -- it's not a meaningful finding at all.

Measured before fixing it: of the 1,335,021 "out of FOV" results on the
bundled Odaiba map, 897,715 (67.2%) were for lights more than 90 degrees
off the camera's heading at that point -- i.e. behind it, not beside or
ahead of it.

**Fix:** `check_target_ahead(cam_pos, cam_yaw, target_pos,
max_angle_diff=90.0)` (`geometry_calculator.py`) is a route-position
pre-filter, applied in `run_simulation` alongside (but independently of)
`check_light_relevant_to_lane` -- deliberately much wider than the
camera's actual FOV cone (90 degrees vs. a typical 30-degree `fov_h`), so
it only excludes things genuinely behind the vehicle, not things merely
outside the narrow FOV. It applies even when `facing_yaw` is unknown
(e.g. pedestrian signals), since "behind me" doesn't depend on knowing
which way the light faces.

This is a simulation-level fix (like the lane-direction filter), not
something scoped to the new blind-spots-only view: it also cleaned up the
full coverage plot and the printed statistics. On the bundled map,
evaluated candidates dropped from 1,849,440 to 951,725, and overall
coverage went from 20.4% to 39.6% -- a truer number, since the noisy
"can't see behind myself" cases are gone from the denominator too.

**The new setting:** `plot_results(..., blind_only=True)` / `--blind-only`
/ `blind_only: true` in a `--config` YAML skips the green "Covered" layer
entirely, showing only red/orange -- meaningful now that both are
guaranteed to reflect an actual camera-FOV or signal-orientation
limitation rather than routing noise.

## Why an interactive viewer, not just a bigger/better static plot

Repeated confusion in this project traced back to the same root cause:
the static plot shows *whether* a point is covered, not *why*, so
questions like "why is this red" or "would widening the FOV actually help
here" had to be answered by re-deriving the geometry by hand each time
(see the FOV-doubling comparison earlier in this history: doubling
`fov_h`/`fov_v` mostly converted red into orange, not green, because
`facing_tolerance_deg` is an independent constraint FOV width can't fix --
a real but easy-to-miss distinction from a colored dot alone).

`webapp.py` + `static/` exists to make that geometry inspectable directly:
click a waypoint, and `calc_camera_frame_offset` places every candidate
light at its actual position inside (or outside) a rendered FOV rectangle,
next to the exact numbers (`yaw_diff`, `pitch_diff`, `in_fov`,
`facing_camera`) that produced it. It deliberately reuses Modules A-C
unchanged (parses the map and runs `run_simulation` once at startup) --
the only new logic is `calc_camera_frame_offset`, which is `check_
fov_inclusion`'s own yaw/pitch-diff math exposed as a raw offset instead
of collapsed into a boolean.

Scoped to v1 as a fixed-camera-spec viewer (confirmed with the user
before building it): no live FOV/range sliders, restart with different
flags to inspect a different spec. The full per-(point, light) result set
is too large to ship to the browser as one JSON blob (950k+ rows on the
bundled map), so the frontend only receives one row per unique waypoint
(a worst-case-wins `status`, same convention as the static plot's z-order)
for the map overview, and fetches per-light detail on click.

## The viewer's map rendered nothing, silently, on a bigger point set

**Symptom:** the viewer loaded (header/legend visible) but the map canvas
stayed blank and the meta line never left "loading...". Reproduced only
with a non-default camera spec (`min_range: 20, max_range: 250,
signal_type: vehicle`) that happened to evaluate 126,264 waypoints; a
default-camera run with 109,340 waypoints had shown no problem, which is
what let this ship initially.

**Root cause:** `fitViewToData()` in `static/app.js` computed the map's
bounding box with `Math.min(...xs)` / `Math.max(...xs)`, spreading every
point's coordinate into the call as individual arguments. V8 (and other
JS engines) cap how many arguments a single call can take -- comfortably
past that cap with 100k+ elements -- so the call threw `RangeError:
Maximum call stack size exceeded`. `main()` had no `try`/`catch`, so the
exception silently aborted everything after that point (`renderMap()`,
`setupMapInteraction()` never ran) with nothing on the page and nothing
but a console error to explain it.

**Fix:** replaced the spread-into-`Math.min`/`max` calls with a plain
`for` loop accumulating min/max (`static/app.js`'s `fitViewToData`), which
has no argument-count limit regardless of point count. Also wrapped
`main()` in `try`/`catch` so any future failure here shows up as visible
red text in the meta line instead of a silent blank page -- catchable
only via the browser console before this fix.

Verified with a headless browser against the exact reproducing config
(`camera_spec.yaml` with `min_range: 20, max_range: 250, signal_type:
vehicle`): map renders, click-to-inspect works, no console errors.
