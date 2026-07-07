# Lanelet2 (.osm) map schema, as observed

Everything this project knows about the map format was reverse-engineered
from one real Autoware-style Lanelet2 export (the Odaiba map: 198,252
nodes, 6,683 lanelets, 903 traffic-light regulatory elements) and
verified by scanning that file -- none of it comes from a formal spec.
Counts below are from that map; another exporter may differ. Fields the
tool actually consumes are marked **[used]**.

## Nodes

```xml
<node id="..." lat="35.61..." lon="139.74...">
  <tag k="local_x" v="86391.77"/>
  <tag k="local_y" v="42492.61"/>
  <tag k="ele" v="1.72"/>
</node>
```

- Every node carries both geographic (`lat`/`lon` XML attributes) and
  local planar coordinates (`local_x`/`local_y` tags, meters) plus
  elevation (`ele`). **[used: local for all geometry; lat/lon for the
  affine georeferencing fit (`parse_latlon_transform`), which is what
  powers the Google Maps links and the aerial-photo underlay]**
- The local frame is rotated ~0.7 degrees from true north on this map --
  don't assume local +y is north.
- Nodes belonging to a `light_bulbs` way additionally carry lamp tags --
  see "Light bulbs" below.

## Ways

A way is an ordered node list with a `type` tag. Types observed:
`line_thin` (5,364), `virtual` (4,579), `pedestrian_marking` (3,344),
`road_border` (2,725), `traffic_light` (949), `light_bulbs` (940),
`guard_rail` (889), `fence` (877), `stop_line` (734), `traffic_sign`
(448), `area` (447), plus assorted markings/areas.

### Traffic light panel (`type=traffic_light`) **[used]**

```xml
<way id="201">
  <nd ref="30"/>  <nd ref="31"/>
  <tag k="type" v="traffic_light"/>
  <tag k="subtype" v="red_yellow_green"/>
  <tag k="height" v="0.45"/>
</way>
```

- A 2-node linestring spanning the **physical width of the housing**:
  endpoint-to-endpoint distance is the real width (1.04-1.34m for
  horizontal vehicle housings, ~0.4m for pedestrian, 0.34-0.45m for the
  six vertical snow-region vehicle signals on this map).
- `height` tag = vertical size of the housing in meters (0.45 / 0.5 /
  0.9 / 1.2 observed; 0.9+ means an arrow section below the lamps).
  Every signal on this map has both width and height.
- `subtype` classifies the signal: `red_yellow_green` = vehicle,
  `red_green` = pedestrian. **[used for signal_type]** Verified
  empirically: `red_yellow_green` panels line up with 3+-bulb heads,
  `red_green` with 2-bulb heads.

### Stop line (`type=stop_line`) **[used]**

Short linestring across the lane. Its midpoint is used as the signal's
`stop_line_pos` (proximity fallback in lane-relevance resolution) and,
with the bulb centroid, to derive the signal's facing direction.

### Light bulbs (`type=light_bulbs`) **[position used; colors not yet]**

```xml
<way id="200">
  <nd ref="10"/>  <nd ref="11"/>  <nd ref="12"/>
  <tag k="type" v="light_bulbs"/>
  <tag k="traffic_light_id" v="201"/>
  <tag k="subtype" v="solid"/>
</way>
```

- One way per physical head; its nodes are the individual lamps.
- **Each bulb node carries a `color` tag** (`red` / `yellow` / `green`;
  6,735 tagged bulbs on this map) **and arrow lamps additionally carry
  an `arrow` tag** (`right` 604, `up` 310, `left` 281, `straight` 12).
  So the full lamp pattern of every head -- including which arrow
  directions it can show -- is recoverable from the map. **[used: the
  viewer's camera view projects each bulb individually and draws it as
  a colored lens (arrow glyph for arrow lamps), simulating the light's
  actual appearance; only the lens diameter (~0.3m) is assumed]**
- `traffic_light_id` points at the panel way (`type=traffic_light`)
  this head belongs to -- consistent on all 1,974 tagged heads here.
  This matters because one regulatory element can bundle several
  physical heads (see below).
- `subtype`: `solid` (843) observed; largely redundant with the node
  tags.

## Lanelet relations (`type=lanelet`) **[subtype=road used]**

```xml
<relation id="50">
  <member type="way" role="left" ref="100"/>
  <member type="way" role="right" ref="101"/>
  <member type="relation" role="regulatory_element" ref="900"/>
  <tag k="type" v="lanelet"/>
  <tag k="subtype" v="road"/>
  <tag k="one_way" v="yes"/>
  <tag k="speed_limit" v="50"/>
  <tag k="location" v="urban"/>
  <tag k="turn_direction" v="right"/>
</relation>
```

- Subtypes on this map: `road` (5,234) **[used]**, `road_shoulder`
  (550), `pedestrian_lane` (429), `crosswalk` (370), `walkway` (51),
  `bicycle_lane` (49).
- `left`/`right` members are the boundary ways; the drivable direction
  follows the node order of the boundaries (verified: boundary
  digitization order matches travel direction; `one_way=yes` on every
  lanelet here). **[used: center line = resampled midpoints of the
  boundaries; successor lanelets are found by matching the raw node id
  at which one left boundary ends and another begins]**
- `role=regulatory_element` members point at the regulatory relations
  governing this specific lanelet -- the map author's authoritative
  statement of which traffic light controls which lane. Only ~20% of
  road lanelets carry one (typically the segment right before the stop
  line). **[used: primary source for lane<->signal relevance]**
- `turn_direction` (`left`/`right`/`straight`, 1,807 lanelets) marks
  intersection turn lanes. *Not used yet -- but combined with the bulb
  `arrow` tags above, it would let a future version match arrow-only
  signal heads to exactly the turn lanes they apply to.*
- Rare/nonstandard tags seen once or a handful of times:
  `related_traffic_light` (1), `stopline_id` (4), `lane_change` (1),
  `turn_signal_distance` (5), `fms_lane_passable` (276),
  `intersection_area` (1,606), `participant:pedestrian` (413).

## Regulatory element relations (`type=regulatory_element`)

Subtypes on this map: `traffic_light` (903) **[used]**, `right_of_way`
(874), `crosswalk` (680), `traffic_sign` (584), `road_marking` (128),
`no_parking_area` (113), `no_stopping_area` (16).

### `subtype=traffic_light` **[used]**

```xml
<relation id="900">
  <member type="way" role="refers" ref="201"/>
  <member type="way" role="refers" ref="203"/>
  <member type="way" role="ref_line" ref="150"/>
  <member type="way" role="light_bulbs" ref="200"/>
  <member type="way" role="light_bulbs" ref="202"/>
  <tag k="type" v="regulatory_element"/>
  <tag k="subtype" v="traffic_light"/>
</relation>
```

- `refers` = panel way(s). **Usually more than one**: 2 panels on 569 of
  the 903 relations, 3 on 219, 4 on 26, just 1 on only 89. A single
  regulatory element models one *logical* signal displayed by several
  physical housings (e.g. a near/far pair over the same stop line).
  This tool currently reads the first `refers` for subtype/size and
  pools all bulbs together; the `traffic_light_id` back-references
  would allow full per-housing separation if ever needed.
- `ref_line` = the stop line way this signal controls. **[used: facing
  direction, signal grouping (`group_id`), and the stop-line-proximity
  relevance fallback]** Redundant heads for the same stop event live in
  *different* regulatory elements sharing the same `ref_line` (67 of
  501 stop lines here are shared by 2+ relations) -- that sharing is
  what `group_id` is built from.
- `light_bulbs` = the lamp ways, one per physical head.

## What is *not* in the map

- **No signal phase/timing data** -- which lamp is lit when, cycle
  times, offsets. The lamp *hardware* (colors, arrows) is fully
  described, but nothing about its temporal behavior. Phase/timing is
  runtime information (V2I / detection), out of scope for a map.
- No housing outline polygons -- the panel way + `height` tag is the
  entire 3D extent (a width segment extruded upward).
- No explicit lane-successor pointers: lanelet connectivity must be
  inferred from shared boundary endpoints, and (as documented in
  behavior.md) that graph has real gaps at some intersections.
