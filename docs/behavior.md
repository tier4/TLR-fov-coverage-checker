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

## A point could show red/orange next to a highlighted green light

**Symptom:** after the viewer started highlighting a selected point's
candidate lights, a point could show a green (covered) highlighted star
right next to it while the point's own dot was red or orange -- reported
as "that's got to be a bug."

**Investigation:** the worst-case-per-point aggregation (see the "covered
point wasn't always fully covered" entry above) was working as designed
-- a point is only "covered" if *every* candidate light is -- but that
didn't match reality for one specific case: a stop line can have more
than one physical signal head (e.g. a through light and a separate
turn-arrow light) recorded as **separate** `TrafficLight`s in the map
data (separate `regulatory_element` relations), each an independent
candidate. Seeing just one of them is enough to know the signal state, so
requiring every redundant head to be independently visible overstated how
many points were actually blind. Confirmed empirically: 67 of the 501
stop lines (`ref_line` ways) in the bundled Odaiba map are referenced by
more than one regulatory_element.

**Fix:** `TrafficLight.group_id` (`map_parser.py`) is the `ref_line` way
id shared by every regulatory_element that references it (or `solo:<own
id>` if it has none, e.g. pedestrian signals). `compute_point_status`
(`fov_simulator.py`) uses it to aggregate per *group* instead of per
individual light: a point is covered only if every distinct group present
has at least one covered member. `webapp.py`, `visualizer.py`, and
`main.py`'s printed stats all switched to this group-aware aggregation
(previously each computed a "worst case across all candidates" inline,
duplicating and slightly diverging in each place).

**But:** re-running the full bundled map through both the default camera
spec and the reported `camera_spec.yaml` (`min_range=20, max_range=250,
signal_type=vehicle`) found **zero** waypoints where this fix actually
changes the covered/not-covered verdict, and zero cases anywhere in the
map where two lights sharing a `group_id` disagree on `is_covered` at the
same waypoint. Redundant heads for the same stop line are apparently
always mounted close enough together that they're geometrically
indistinguishable from any waypoint far enough away to be in range at all
-- confirmed by clicking through the viewer's new "group covered" column:
sibling heads consistently pass or fail together.

That means the original "green star, red point" report was very likely a
*different* case: two lights from two genuinely different stop lines both
candidates for the same waypoint (e.g. this intersection's signal and the
next one down the road), where one is visible and the other isn't. Per
the grouping rule as specified (group by stop line, not more broadly),
that point is correctly still not covered -- the other intersection being
visible doesn't tell a driver/camera anything about the signal state at
*this* one. If a specific point still looks wrong after this fix, it's
worth clicking it in the viewer and checking the `group_id` column: same
group with different outcomes would be a bug; different groups is
expected and, per this feature's own definition, correct.

## Investigated: could some lanelets be digitized backwards?

**Concern raised:** the FOV frustum drawn on a selected point sometimes
looked like it was pointing the wrong way relative to the visible lane
geometry, raising the question of whether `run_simulation`'s heading
(`calc_heading_yaw` between consecutive `LanePath.center_line` points,
which follows the `left`/`right` way node order straight from the source
XML) could be backwards for some lanelets -- i.e. whether the map's own
digitization sometimes violates the Lanelet2 convention that boundary-way
node order follows the direction of travel.

**First pass (misleading):** compared each stop-line-adjacent lanelet's
computed heading against the opposite of its traffic light's `facing_yaw`
(the same check used to validate `facing_yaw` itself, see above). Using a
crude "nearest lanelet endpoint within 5m of the stop line, searched
across every lanelet in the map" match: 571 pairs, median error 7.7
degrees, but a distinctly separate cluster of 41 pairs (7.2%) with error
>150 degrees -- a bimodal split that looked exactly like what a reversed
lanelet would produce (correct heading + opposite facing_yaw = ~180
degrees off, not a smooth spread of noise).

**Second pass (the actual answer):** picked the cleanest of those 41
outliers (lane `2294073`, `turn_direction=left`, endpoint only 2.43m from
its stop line) and checked its topology directly: does its boundary way's
first/last node line up with neighboring lanelets' endpoints to form a
coherent route? It does -- `2293840` ends exactly where `2294073` begins,
and `2294073` ends exactly where `2293842` begins. A three-lanelet chain
digitized start-to-end consistently, no reversal.

That means the 41-pair cluster is much more likely an artifact of the
*matching* heuristic (a left-turn lanelet's endpoint happening to sit
within 5m of a stop line that isn't actually the one controlling it, e.g.
the through-lane's or the opposing direction's), not evidence that
`run_simulation`'s own heading computation is wrong. Since the production
pipeline never uses this "nearest endpoint" matching at all (it only
takes each `LanePath`'s own consecutive points, whatever `parse_lanes`
produced from that lanelet's own `left`/`right` ways), this investigation
didn't turn up a concrete bug to fix.

**Still open:** this checked one example out of 41 flagged pairs, not all
of them, and it can't rule out a genuinely reversed lanelet existing
elsewhere in a 5234-lanelet map. If you see the frustum pointing the wrong
way again, click the point in the viewer, read off its `lane_id` from the
point-info line, and report that specific id -- checking one concrete
lanelet's topology (as above) is fast and conclusive; searching blindly
for "some lanelet somewhere" is not.

## A point could fail even with its own signal clearly visible

**Symptom:** a waypoint would show facing_away/out_of_fov (via
`compute_point_status`'s worst-case-across-groups rule) even when the
signal directly ahead was plainly covered -- suspected to be an unrelated
cross-street signal at a nearby, differently-angled intersection getting
evaluated against a lane it had nothing to do with.

**Root cause:** `check_light_relevant_to_lane`'s only defense against
this was a single 90-degree threshold on the angle between a light's
`facing_yaw` and the lane's heading. Real intersections are frequently
not square; a cross-street signal at a 20-30 degree skew can end up "more
than 90 degrees" off a lane's heading by coincidence and pass the
threshold as if it faced this lane, even though it's a different street
entirely. Once it's a candidate, its own group almost always fails
outright (it's not oriented for this lane, and often out of the narrow
FOV cone too) -- and per the grouping rule above, one failed group is
enough to fail the whole waypoint, regardless of how well-covered the
lane's actual own signal is.

**Fix:** `parse_lanes` (`map_parser.py`) now also extracts each
lanelet's own `<member type="relation" role="regulatory_element">` refs,
keeping the ones pointing at `subtype=traffic_light` relations as
`LanePath.direct_tl_ids` -- the map author's own, authoritative statement
of which signal(s) control this specific lane, sidestepping the angle
threshold's ambiguity entirely. It also records `next_lane_ids` (lanelets
whose left way starts where this one's ends, via raw node id -- not
resampled coordinates, which can drift by floating-point rounding).

Only ~20% of lanelets carry a direct reference themselves (usually just
the segment immediately approaching the stop line), so
`_build_lane_relevant_tl_ids` (`fov_simulator.py`) walks `next_lane_ids`
forward from lanelets without one, inheriting the reference from the
nearest downstream lanelet that has one, bounded by `camera.max_range`
(no point inheriting a light already out of detection range) and 15 hops.
That recovers an authoritative answer for 51.7% of all lanelets on the
bundled map. `run_simulation` uses this set as-is for vehicle-signal
candidates when available (bypassing `check_light_relevant_to_lane`
entirely), falling back to the old angle-threshold heuristic only when
neither a direct nor inherited reference exists. Pedestrian signals are
untouched -- they normally have no controlling-lanelet reference at all,
so they always use the geometric fallback, gated explicitly by
`signal_type == "vehicle"` in the new check rather than relying on the
map data happening to be empty for them.

**Measured impact** on the bundled map (default camera spec): evaluated
candidates dropped from 951,725 to 540,373 (removing exactly the kind of
spurious cross-street candidate this was meant to catch), and vehicle
coverage rose from 10.7% to **54.0%** -- pedestrian coverage stayed
exactly the same (34.2%, byte-for-byte), confirming the fix is scoped to
vehicle signals only and didn't disturb anything else. Verified in the
viewer: a waypoint that previously had 21 mostly-irrelevant candidates and
read "facing_away" overall now has exactly 1 candidate (its own signal)
and reads "covered".

## Sharing a specific finding without the map file

Two related asks came up once the fix above meant most remaining red/
orange points were genuine: how to hand a specific one off for discussion
(to a teammate, or back to a future session) without re-explaining "click
here, zoom there," and how to avoid recomputing a ~20-30s run every time.

**Point identity for a shareable link:** the obvious choice was the
point's `id` in `/api/points`, but that's just this run's insertion order
-- an array index, not a stable identity. A different signal_type filter,
a different `camera.max_range` (which changes how far
`_build_lane_relevant_tl_ids` looks), or any future change to candidate
filtering can shift which index a given physical waypoint lands on. Used
`(lane_id, x, y)` instead -- the actual identity `results_by_point` is
already keyed by internally -- so a link stays correct across reruns of
the same map/camera as long as the simulation is deterministic, which it
is (fixed `SAMPLE_INTERVAL_M`, no randomness).

**Save/load format:** `_serialize_state()`/`_deserialize_state()`
round-trip the entire in-memory `_state` (points, per-point candidate
results, traffic light positions, per-waypoint heading lookup, camera
spec) through plain JSON, reconstructing real `Point3D`/`ValidationResult`
instances on load so every other function in the module keeps working
unchanged -- it doesn't know or care whether `_state` came from
`_load_data` (a fresh map+simulation) or `_deserialize_state` (a loaded
snapshot). Gzip-compressed by default (`_write_snapshot`/`_read_snapshot`):
the uncompressed JSON ran 118MB on the bundled map (one entry per
waypoint/light candidate, which is extremely repetitive and compresses
to about 13MB, an ~9x reduction) -- large enough that shipping it
uncompressed would have undercut the whole point of making a run easy to
hand off. `_read_snapshot` still accepts a plain uncompressed JSON file
too (falls back if gzip decompression fails), in case someone hand-edits
one.

## Two specific points, diagnosed via their shareable link

Shareable point links turned out to be useful for exactly what they were
built for: two reported cases (`?lane=2294556&x=...` and
`?lane=2223446&x=...`) pointed at exact waypoints to investigate, instead
of "somewhere near this intersection." Querying the running instance's
own `/api/points/<id>/candidates` for both (rather than guessing) showed
the same root cause as before, one level deeper.

**Diagnosis:** both lanes have empty `direct_tl_ids`, and
`_build_lane_relevant_tl_ids`'s successor-chain search failed to reach any
lanelet that had one -- not because there wasn't one nearby, but because
the lanelet connectivity graph (built from shared `left`-way endpoints)
has a gap right at the intersection each lane approaches:

- Lane `2294556`'s chain (`2294556` -> `2294825` -> `2294551`) dead-ends
  at a lanelet with zero `next_lane_ids` -- no lanelet's left way starts
  anywhere near that endpoint, even though the road obviously continues
  physically. The nearest stop line is only 10.5m away and turned out to
  be exactly the right one (its regulatory_element group is the same one
  already shown correctly covered at 55m in the candidate list) -- the
  graph gap, not a wrong "relevant" light, was hiding it.
- Lane `2223446`'s chain runs past `camera.max_range` before reaching a
  tagged lanelet. Both root causes converge on the same failure mode:
  once neither `direct_tl_ids` nor the chain search finds anything, the
  candidate set falls back to the geometric heuristic
  (`check_light_relevant_to_lane`, a single 90 degree threshold), which
  isn't precise enough to keep out a signal meant for a different lane at
  the same complex intersection.

**Fix:** `_build_lane_relevant_tl_ids` gained a second, independent
resolution level between the direct reference and the geometric fallback:
for a lane with no direct/inherited reference, check whether *any*
traffic light's stop line (`TrafficLight.stop_line_pos`, the `ref_line`
way's midpoint) sits within `STOP_LINE_PROXIMITY_M` (30m) of that lane's
own mapped end point -- vectorized across all lanes x all stop lines at
once (`_nearby_group_by_lane_end`), since a nested Python loop over
~5,000 lanes x ~900 groups would be needlessly slow. Also tightened
`LANE_DIRECTION_THRESHOLD_DEG` from 90 to 120 degrees for whatever still
falls all the way through to the geometric heuristic -- a smaller
reduction in false positives, but a real one, and cheap to apply.

**Measured impact:**
- Lane `2294556`'s reported point: `facing_away` -> **`covered`**. Its
  candidate set went from 3 groups (1 genuinely covered nearby, 2
  incidentally-covered and irrelevant far-away groups that the proximity
  fix now correctly excludes) down to exactly the 1 real one.
- Lane `2223446`'s reported point: **still `out_of_fov`**, not fully
  fixed. One of its three previously-included irrelevant candidates
  (`2225071`, 96.77 degrees off lane heading) is now excluded by the
  tightened threshold, but two others (`2225072`/`2225073`, both a
  well-aligned ~164-172 degrees off -- looking exactly like genuine
  matches on facing_yaw alone) remain. Their stop lines sit 91.6m and
  91.7m from where the successor search gives up, in a near-exact 3-way
  tie with the correct one (91.7m) -- close enough together that
  `STOP_LINE_PROXIMITY_M` (30m) correctly declines to guess rather than
  risk picking the wrong one, but also close enough that simple distance
  can't disambiguate them at all. This is a real, open limitation: fixing
  it would need either better lanelet routing data than this map provides
  at that intersection, or a geometry heuristic well beyond "which stop
  line is closest."
- Whole-map default-camera-spec run: vehicle coverage 54.0% -> **75.4%**;
  pedestrian coverage unchanged (34.2%, exact) as expected, since none of
  this touches pedestrian-signal handling.

## A third point, and a pivot from threshold-tuning to visualizing facing_yaw

A third reported point (`?lane=2293233&x=...`) turned out to be the same
family of issue again, but at a different edge: lane `2293233` has no
direct/inherited reference, and its two candidate stop lines sit 70.5m
and 85.3m from the lane's own end -- both past `STOP_LINE_PROXIMITY_M`
(30m), so the proximity fix (rightly) doesn't fire, and it falls all the
way through to the geometric heuristic. The wrongly-included signal
(`2296340`) sits at 130.52 degrees off the lane's heading -- comfortably
past the 120 degree threshold, not a near-miss like the earlier 96.77
degree case, so tightening the threshold further would risk cutting
genuine matches elsewhere for one more fix here. Diminishing, whack-a-mole
returns from threshold-only tuning going forward.

Given that, the next step was a pivot the user asked for directly:
instead of chasing one misclassified light at a time via a Python script
each time, make `facing_yaw` visible on the map itself so it's possible to
eyeball an intersection and see immediately which lights make geometric
sense for a given approach.

**Implementation:** `/api/traffic_lights` now includes each light's
`facing_yaw` (`webapp.py`'s `_state["tl_facing_yaw"]`, threaded through
`_serialize_state`/`_deserialize_state` too -- bumped
`_SNAPSHOT_FORMAT_VERSION` to 2 since old snapshots lack the field and
should fail loudly on `--load` rather than KeyError confusingly).
`static/app.js`'s `drawFacingArrow` draws a short arrow from each star in
its facing_yaw direction (computed directly in screen space -- a world
direction only needs scaling by `view.scale`, not a full
`worldToScreen` round-trip, since there's no rotation between the two).
Lights with no `ref_line` (facing_yaw is `null`, mostly pedestrian
signals) simply get no arrow, which is itself informative: it shows at a
glance which lights the tool has no orientation data for at all.

**Verified** on the `2293233` point specifically (headless browser,
directly setting `view.scale`/`view.offsetX`/`offsetY` and calling
`renderMap()` to center tightly on the two candidate lights rather than
fumbling scroll-zoom): the correctly-covered light's arrow points back
down the approach frustum as expected; the wrongly-included light's arrow
points at a visibly different angle, not aligned with the frustum axis --
confirming by eye what the geometry check computes, and giving the user a
way to spot the next one of these without asking for a script each time.
