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
        chiral across the dataset).
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
    `refers`-role panel way's subtype, and facing_yaw is derived from the
    bulb centroid and the `ref_line`-role stop line way (both None/"unknown"
    if those members are absent, which the simulator treats permissively).
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
        stop_line_points = [nodes[n] for n in ways.get(ref_line_ref, []) if n in nodes] if ref_line_ref else []
        if stop_line_points:
            bulb_centroid = calc_centroid(bulbs)
            stop_line_mid = calc_centroid(stop_line_points)
            facing_yaw = calc_heading_yaw(bulb_centroid, stop_line_mid)

        traffic_lights.append(
            TrafficLight(id=rel_id, bulbs=bulbs, signal_type=signal_type, facing_yaw=facing_yaw)
        )
    return traffic_lights


def parse_lanes(xml_string: str, nodes: dict[str, Point3D]) -> list[LanePath]:
    """Build one LanePath per `type=lanelet subtype=road` relation, center-lined via Module A."""
    root = ET.fromstring(xml_string)
    ways = _parse_ways(root)
    lanes: list[LanePath] = []
    for rel_elem in root.findall("relation"):
        if _get_tag(rel_elem, "type") != "lanelet" or _get_tag(rel_elem, "subtype") != "road":
            continue
        rel_id = rel_elem.get("id")
        if rel_id is None:
            continue

        left_ref = right_ref = None
        for member in rel_elem.findall("member"):
            role = member.get("role")
            if role == "left":
                left_ref = member.get("ref")
            elif role == "right":
                right_ref = member.get("ref")
        if left_ref is None or right_ref is None:
            continue

        left_points = [nodes[n] for n in ways.get(left_ref, []) if n in nodes]
        right_points = [nodes[n] for n in ways.get(right_ref, []) if n in nodes]
        center = calc_center_line(left_points, right_points)
        if center:
            lanes.append(LanePath(id=rel_id, center_line=center))
    return lanes
