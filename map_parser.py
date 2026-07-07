"""Module B: Lanelet2/OSM Map Parser.

Pure(-ish) parsing functions: each takes the raw XML text (and, where
needed, an already-parsed node table) and returns plain dataclass
instances. No file I/O happens here, which is what lets the unit tests
feed in small hand-written XML strings instead of touching disk.

Schema notes (confirmed against a real Lanelet2 export):
  - <node> coordinates live in <tag k="local_x"/"local_y"/"ele">.
  - A lane is a <relation type="lanelet" subtype="road"> with
    <member role="left"/"right"> pointing at boundary <way>s.
  - A traffic light is a <relation type="regulatory_element"
    subtype="traffic_light"> with:
      - <member role="light_bulbs"> way(s) referencing the actual bulb
        <node>s (used for position and, via bulb count and colors, as
        the visible lamp set).
      - <member role="refers"> way: the physical signal panel. Its
        `subtype` tag is "red_yellow_green" for vehicle signals or
        "red_green" for pedestrian signals (confirmed empirically: the
        former line up with 3+ bulb light_bulbs ways, incl. arrow
        heads with 4-6; the latter line up with 2-bulb ways).
      - <member role="ref_line"> way: the stop line the signal
        controls. The signal's facing direction is derived as the
        bearing from the bulb centroid to the stop line midpoint --
        verified against ~570 lanelet approach headings in the real
        Odaiba map (median angular error ~7.7 degrees), which is far
        more reliable than trying to infer facing from the two
        endpoints of the panel way (that ordering is not consistently
        chiral across the dataset). The same ref_line way is also used
        to group TrafficLights: 67 of the 501 stop lines in the bundled
        Odaiba map are referenced by more than one regulatory_element
        (redundant signal heads for the same stop event, e.g. a through
        light and a turn-arrow light) -- seeing just one of them is
        enough for a driver/camera to know the signal state there, so
        they share a `group_id` and `compute_point_status`
        (fov_simulator.py) treats the group as covered if any member is.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from geometry_calculator import calc_center_line, calc_centroid, calc_heading_yaw
from models import LanePath, Point3D, TrafficLight

_VEHICLE_SUBTYPES = {"red_yellow_green"}
_PEDESTRIAN_SUBTYPES = {"red_green"}


def _get_tag(elem: ET.Element, key: str) -> str | None:
    for tag in elem.findall("tag"):
        if tag.get("k") == key:
            return tag.get("v")
    return None


def _parse_ways(root: ET.Element) -> dict[str, list[str]]:
    """way id -> ordered list of referenced node ids."""
    ways: dict[str, list[str]] = {}
    for way_elem in root.findall("way"):
        way_id = way_elem.get("id")
        if way_id is None:
            continue
        ways[way_id] = [nd.get("ref") for nd in way_elem.findall("nd") if nd.get("ref") is not None]
    return ways


def _index_way_elements(root: ET.Element) -> dict[str, ET.Element]:
    """way id -> its XML element, so tags (e.g. subtype) can be looked up on demand."""
    return {w.get("id"): w for w in root.findall("way") if w.get("id") is not None}


def _classify_signal_type(subtype: str | None) -> str:
    if subtype in _VEHICLE_SUBTYPES:
        return "vehicle"
    if subtype in _PEDESTRIAN_SUBTYPES:
        return "pedestrian"
    return "unknown"


def _index_regulatory_element_subtypes(root: ET.Element) -> dict[str, str | None]:
    """regulatory_element relation id -> its own `subtype` tag (e.g. "traffic_light")."""
    return {
        r.get("id"): _get_tag(r, "subtype")
        for r in root.findall("relation")
        if _get_tag(r, "type") == "regulatory_element" and r.get("id") is not None
    }


def parse_nodes(xml_string: str) -> dict[str, Point3D]:
    """Extract every <node>'s (local_x, local_y, ele) into a Point3D table keyed by node id."""
    root = ET.fromstring(xml_string)
    nodes: dict[str, Point3D] = {}
    for node_elem in root.findall("node"):
        node_id = node_elem.get("id")
        x = _get_tag(node_elem, "local_x")
        y = _get_tag(node_elem, "local_y")
        z = _get_tag(node_elem, "ele")
        if node_id is None or x is None or y is None:
            continue
        nodes[node_id] = Point3D(x=float(x), y=float(y), z=float(z) if z is not None else 0.0)
    return nodes


def parse_traffic_lights(xml_string: str, nodes: dict[str, Point3D]) -> list[TrafficLight]:
    """Build one TrafficLight per `subtype=traffic_light` regulatory_element relation.

    Its bulbs are the union of all node positions found on that relation's
    `light_bulbs`-role member way(s). signal_type is classified from the
    `refers`-role panel way's subtype, and facing_yaw/stop_line_pos are
    derived from the bulb centroid and the `ref_line`-role stop line way's
    midpoint (all None/"unknown" if those members are absent, which the
    simulator treats permissively). stop_line_pos also anchors
    `fov_simulator._build_lane_relevant_tl_ids`'s proximity fallback: a
    lane with no `regulatory_element` reference of its own (directly or
    inherited) but whose path ends right next to this stop line is very
    likely approaching this exact signal, lanelet-graph gaps aside.
    """
    root = ET.fromstring(xml_string)
    ways = _parse_ways(root)
    way_elems = _index_way_elements(root)
    traffic_lights: list[TrafficLight] = []
    for rel_elem in root.findall("relation"):
        if _get_tag(rel_elem, "subtype") != "traffic_light":
            continue
        rel_id = rel_elem.get("id")
        if rel_id is None:
            continue

        bulbs: list[Point3D] = []
        refers_ref: str | None = None
        ref_line_ref: str | None = None
        for member in rel_elem.findall("member"):
            role = member.get("role")
            if role == "light_bulbs":
                for node_id in ways.get(member.get("ref"), []):
                    point = nodes.get(node_id)
                    if point is not None:
                        bulbs.append(point)
            elif role == "refers" and refers_ref is None:
                refers_ref = member.get("ref")
            elif role == "ref_line" and ref_line_ref is None:
                ref_line_ref = member.get("ref")

        if not bulbs:
            continue

        panel_elem = way_elems.get(refers_ref) if refers_ref else None
        signal_type = _classify_signal_type(_get_tag(panel_elem, "subtype") if panel_elem is not None else None)

        facing_yaw: float | None = None
        stop_line_pos: Point3D | None = None
        stop_line_points = [nodes[n] for n in ways.get(ref_line_ref, []) if n in nodes] if ref_line_ref else []
        if stop_line_points:
            bulb_centroid = calc_centroid(bulbs)
            stop_line_pos = calc_centroid(stop_line_points)
            facing_yaw = calc_heading_yaw(bulb_centroid, stop_line_pos)

        # prefixed so a ref_line way id can never collide with a fallback
        # relation id -- ways and relations are separate OSM id namespaces.
        group_id = f"refline:{ref_line_ref}" if ref_line_ref else f"solo:{rel_id}"

        traffic_lights.append(
            TrafficLight(
                id=rel_id,
                bulbs=bulbs,
                signal_type=signal_type,
                facing_yaw=facing_yaw,
                group_id=group_id,
                stop_line_pos=stop_line_pos,
            )
        )
    return traffic_lights


def parse_lanes(xml_string: str, nodes: dict[str, Point3D]) -> list[LanePath]:
    """Build one LanePath per `type=lanelet subtype=road` relation, center-lined via Module A.

    Each lanelet's own `<member type="relation" role="regulatory_element">`
    refs are also collected (filtered to ones whose subtype is
    "traffic_light") into `direct_tl_ids` -- this is the map author's own,
    authoritative statement of which signal(s) control this specific lane,
    as opposed to guessing from geometry. Not every lanelet carries one
    (only ~20% of lanelets in the bundled Odaiba map do, typically the
    segment immediately approaching a stop line); `next_lane_ids` (lanelet
    ids whose left way starts exactly where this one's left way ends, by
    raw node id -- not resampled coordinates, which can drift by floating-
    point rounding) lets `fov_simulator.py` walk forward through the route
    to inherit a reference from a nearby downstream lanelet that has one.
    """
    root = ET.fromstring(xml_string)
    ways = _parse_ways(root)
    reg_subtypes = _index_regulatory_element_subtypes(root)

    raw_lanes: list[tuple[str, list[Point3D], list[str], str | None, str | None]] = []
    for rel_elem in root.findall("relation"):
        if _get_tag(rel_elem, "type") != "lanelet" or _get_tag(rel_elem, "subtype") != "road":
            continue
        rel_id = rel_elem.get("id")
        if rel_id is None:
            continue

        left_ref = right_ref = None
        reg_refs: list[str] = []
        for member in rel_elem.findall("member"):
            role = member.get("role")
            if role == "left":
                left_ref = member.get("ref")
            elif role == "right":
                right_ref = member.get("ref")
            elif role == "regulatory_element" and member.get("type") == "relation":
                ref = member.get("ref")
                if ref is not None:
                    reg_refs.append(ref)
        if left_ref is None or right_ref is None:
            continue

        left_way_nodes = ways.get(left_ref, [])
        left_points = [nodes[n] for n in left_way_nodes if n in nodes]
        right_points = [nodes[n] for n in ways.get(right_ref, []) if n in nodes]
        center = calc_center_line(left_points, right_points)
        if not center:
            continue

        direct_tl_ids = [r for r in reg_refs if reg_subtypes.get(r) == "traffic_light"]
        first_node = left_way_nodes[0] if left_way_nodes else None
        last_node = left_way_nodes[-1] if left_way_nodes else None
        raw_lanes.append((rel_id, center, direct_tl_ids, first_node, last_node))

    starts_at: dict[str, list[str]] = {}
    for rel_id, _center, _direct_tl_ids, first_node, _last_node in raw_lanes:
        if first_node is not None:
            starts_at.setdefault(first_node, []).append(rel_id)

    lanes: list[LanePath] = []
    for rel_id, center, direct_tl_ids, _first_node, last_node in raw_lanes:
        next_ids = [lid for lid in starts_at.get(last_node, []) if lid != rel_id] if last_node is not None else []
        lanes.append(LanePath(id=rel_id, center_line=center, direct_tl_ids=direct_tl_ids, next_lane_ids=next_ids))
    return lanes
