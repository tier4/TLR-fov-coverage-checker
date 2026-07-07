"""Parser tests use a hand-written mock XML string (not the real 46MB map)
so the parsing logic can be exercised in isolation, per the spec's
"mock XML instead of real files" requirement.
"""

import pytest

from map_parser import parse_lanes, parse_latlon_transform, parse_nodes, parse_signal_heads, parse_traffic_lights
from models import Point3D

MOCK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm generator="test">
  <node id="1" lat="35.0" lon="139.0">
    <tag k="local_x" v="100.0"/>
    <tag k="local_y" v="200.0"/>
    <tag k="ele" v="5.0"/>
  </node>
  <node id="2" lat="35.0" lon="139.0">
    <tag k="local_x" v="110.0"/>
    <tag k="local_y" v="200.0"/>
    <tag k="ele" v="5.0"/>
  </node>
  <node id="3" lat="35.0" lon="139.0">
    <tag k="local_x" v="100.0"/>
    <tag k="local_y" v="204.0"/>
    <tag k="ele" v="5.0"/>
  </node>
  <node id="4" lat="35.0" lon="139.0">
    <tag k="local_x" v="110.0"/>
    <tag k="local_y" v="204.0"/>
    <tag k="ele" v="5.0"/>
  </node>
  <node id="10" lat="35.0" lon="139.0">
    <tag k="local_x" v="150.0"/>
    <tag k="local_y" v="202.0"/>
    <tag k="ele" v="10.0"/>
    <tag k="color" v="red"/>
  </node>
  <node id="11" lat="35.0" lon="139.0">
    <tag k="local_x" v="150.5"/>
    <tag k="local_y" v="202.0"/>
    <tag k="ele" v="10.0"/>
    <tag k="color" v="yellow"/>
  </node>
  <node id="20" lat="35.0" lon="139.0">
    <tag k="local_x" v="110.0"/>
    <tag k="local_y" v="201.5"/>
    <tag k="ele" v="0.0"/>
  </node>
  <node id="21" lat="35.0" lon="139.0">
    <tag k="local_x" v="110.0"/>
    <tag k="local_y" v="202.5"/>
    <tag k="ele" v="0.0"/>
  </node>
  <node id="30" lat="35.0" lon="139.0">
    <tag k="local_x" v="149.9"/>
    <tag k="local_y" v="202.0"/>
    <tag k="ele" v="9.8"/>
  </node>
  <node id="31" lat="35.0" lon="139.0">
    <tag k="local_x" v="151.15"/>
    <tag k="local_y" v="202.0"/>
    <tag k="ele" v="9.8"/>
  </node>
  <way id="100">
    <nd ref="1"/>
    <nd ref="2"/>
  </way>
  <way id="101">
    <nd ref="3"/>
    <nd ref="4"/>
  </way>
  <way id="150">
    <nd ref="20"/>
    <nd ref="21"/>
    <tag k="type" v="stop_line"/>
  </way>
  <way id="200">
    <nd ref="10"/>
    <nd ref="11"/>
    <tag k="type" v="light_bulbs"/>
    <tag k="traffic_light_id" v="201"/>
  </way>
  <way id="201">
    <nd ref="30"/>
    <nd ref="31"/>
    <tag k="type" v="traffic_light"/>
    <tag k="subtype" v="red_yellow_green"/>
    <tag k="height" v="0.45"/>
  </way>
  <relation id="50">
    <member type="way" role="left" ref="100"/>
    <member type="way" role="right" ref="101"/>
    <tag k="type" v="lanelet"/>
    <tag k="subtype" v="road"/>
  </relation>
  <relation id="900">
    <member type="way" role="refers" ref="201"/>
    <member type="way" role="ref_line" ref="150"/>
    <member type="way" role="light_bulbs" ref="200"/>
    <tag k="type" v="regulatory_element"/>
    <tag k="subtype" v="traffic_light"/>
  </relation>
</osm>
"""


def test_parse_nodes_extracts_coordinates():
    nodes = parse_nodes(MOCK_XML)
    assert len(nodes) == 10
    assert nodes["1"] == Point3D(100.0, 200.0, 5.0)
    assert nodes["10"] == Point3D(150.0, 202.0, 10.0)


def test_parse_nodes_defaults_missing_elevation_to_zero():
    xml = MOCK_XML.replace('<tag k="ele" v="5.0"/>', "", 1)
    nodes = parse_nodes(xml)
    assert nodes["1"].z == 0.0


def test_parse_traffic_lights_reads_light_bulbs_way():
    nodes = parse_nodes(MOCK_XML)
    lights = parse_traffic_lights(MOCK_XML, nodes)
    assert len(lights) == 1
    tl = lights[0]
    assert tl.id == "900"
    assert len(tl.bulbs) == 2
    assert Point3D(150.0, 202.0, 10.0) in tl.bulbs
    assert Point3D(150.5, 202.0, 10.0) in tl.bulbs


def test_parse_traffic_lights_classifies_signal_type_from_refers_way():
    nodes = parse_nodes(MOCK_XML)
    tl = parse_traffic_lights(MOCK_XML, nodes)[0]
    assert tl.signal_type == "vehicle"  # refers-way subtype is red_yellow_green


def test_parse_traffic_lights_classifies_pedestrian_from_red_green_subtype():
    xml = MOCK_XML.replace('<tag k="subtype" v="red_yellow_green"/>', '<tag k="subtype" v="red_green"/>')
    nodes = parse_nodes(xml)
    tl = parse_traffic_lights(xml, nodes)[0]
    assert tl.signal_type == "pedestrian"


def test_parse_traffic_lights_unknown_type_when_refers_missing():
    xml = MOCK_XML.replace('<member type="way" role="refers" ref="201"/>', "")
    nodes = parse_nodes(xml)
    tl = parse_traffic_lights(xml, nodes)[0]
    assert tl.signal_type == "unknown"


def test_parse_traffic_lights_computes_facing_yaw_toward_stop_line():
    # bulb centroid is (150.25, 202.0); stop line midpoint is (110.0, 202.0)
    # -> facing points due west (180 degrees), back at the approaching lane.
    nodes = parse_nodes(MOCK_XML)
    tl = parse_traffic_lights(MOCK_XML, nodes)[0]
    assert tl.facing_yaw == pytest.approx(180.0)


def test_parse_traffic_lights_group_id_uses_ref_line_way():
    nodes = parse_nodes(MOCK_XML)
    tl = parse_traffic_lights(MOCK_XML, nodes)[0]
    assert tl.group_id == "refline:150"


def test_parse_traffic_lights_group_id_falls_back_to_own_id_without_ref_line():
    xml = MOCK_XML.replace('<member type="way" role="ref_line" ref="150"/>', "")
    nodes = parse_nodes(xml)
    tl = parse_traffic_lights(xml, nodes)[0]
    assert tl.group_id == "solo:900"


def test_parse_traffic_lights_shares_group_id_across_regulatory_elements_on_same_stop_line():
    # a second regulatory_element (id=901) referencing the SAME ref_line way
    # (150) as relation 900 -- e.g. a redundant turn-arrow head controlling
    # the same stop event -- should end up in the same group.
    xml = MOCK_XML.replace(
        "</osm>",
        """  <relation id="901">
    <member type="way" role="refers" ref="201"/>
    <member type="way" role="ref_line" ref="150"/>
    <member type="way" role="light_bulbs" ref="200"/>
    <tag k="type" v="regulatory_element"/>
    <tag k="subtype" v="traffic_light"/>
  </relation>
</osm>
""",
    )
    nodes = parse_nodes(xml)
    lights = parse_traffic_lights(xml, nodes)
    assert len(lights) == 2
    assert lights[0].group_id == lights[1].group_id == "refline:150"


def test_parse_traffic_lights_facing_yaw_none_when_ref_line_missing():
    xml = MOCK_XML.replace('<member type="way" role="ref_line" ref="150"/>', "")
    nodes = parse_nodes(xml)
    tl = parse_traffic_lights(xml, nodes)[0]
    assert tl.facing_yaw is None


def test_parse_traffic_lights_stop_line_pos_is_ref_line_midpoint():
    # ref_line way 150 spans (110, 201.5) to (110, 202.5) -> midpoint (110, 202)
    nodes = parse_nodes(MOCK_XML)
    tl = parse_traffic_lights(MOCK_XML, nodes)[0]
    assert tl.stop_line_pos == Point3D(110.0, 202.0, 0.0)


def test_parse_traffic_lights_stop_line_pos_none_when_ref_line_missing():
    xml = MOCK_XML.replace('<member type="way" role="ref_line" ref="150"/>', "")
    nodes = parse_nodes(xml)
    tl = parse_traffic_lights(xml, nodes)[0]
    assert tl.stop_line_pos is None


def test_parse_traffic_lights_panel_size_from_refers_way():
    # panel way 201 spans nodes 30 (149.9, 202) -> 31 (151.15, 202) and
    # carries height=0.45 -- the real housing size, straight from the map
    nodes = parse_nodes(MOCK_XML)
    tl = parse_traffic_lights(MOCK_XML, nodes)[0]
    assert tl.panel_width == pytest.approx(1.25)
    assert tl.panel_height == pytest.approx(0.45)


def test_parse_traffic_lights_panel_height_none_without_height_tag():
    xml = MOCK_XML.replace('<tag k="height" v="0.45"/>', "")
    nodes = parse_nodes(xml)
    tl = parse_traffic_lights(xml, nodes)[0]
    assert tl.panel_width == pytest.approx(1.25)
    assert tl.panel_height is None


def test_parse_traffic_lights_panel_width_none_when_panel_nodes_unresolvable():
    xml = MOCK_XML.replace('<nd ref="30"/>', '<nd ref="99998"/>').replace('<nd ref="31"/>', '<nd ref="99999"/>')
    nodes = parse_nodes(xml)
    tl = parse_traffic_lights(xml, nodes)[0]
    assert tl.panel_width is None
    assert tl.panel_height == pytest.approx(0.45)


def test_parse_traffic_lights_lamps_carry_color_and_position():
    # bulb nodes 10 (color=red) and 11 (color=yellow) in document order
    nodes = parse_nodes(MOCK_XML)
    tl = parse_traffic_lights(MOCK_XML, nodes)[0]
    assert len(tl.lamps) == 2
    assert tl.lamps[0].color == "red"
    assert tl.lamps[0].pos == Point3D(150.0, 202.0, 10.0)
    assert tl.lamps[1].color == "yellow"
    assert tl.lamps[0].arrow is None


def test_parse_traffic_lights_lamps_carry_arrow_tag():
    xml = MOCK_XML.replace('<tag k="color" v="yellow"/>', '<tag k="color" v="green"/><tag k="arrow" v="right"/>')
    nodes = parse_nodes(xml)
    tl = parse_traffic_lights(xml, nodes)[0]
    assert tl.lamps[1].color == "green"
    assert tl.lamps[1].arrow == "right"


def test_parse_signal_heads_one_record_per_light_bulbs_way():
    nodes = parse_nodes(MOCK_XML)
    heads = parse_signal_heads(MOCK_XML, nodes)
    assert len(heads) == 1
    head = heads[0]
    assert head["way_id"] == "200"
    assert head["relation_id"] == "900"
    assert head["signal_type"] == "vehicle"
    # panel resolved via the bulb way's traffic_light_id tag (way 201)
    assert head["panel_width"] == pytest.approx(1.25)
    assert head["panel_height"] == pytest.approx(0.45)
    # centroid of bulbs 10 (150, 202) and 11 (150.5, 202)
    assert head["x"] == pytest.approx(150.25)
    assert head["y"] == pytest.approx(202.0)
    assert [lamp["color"] for lamp in head["lamps"]] == ["red", "yellow"]


def test_parse_signal_heads_falls_back_to_first_refers_without_traffic_light_id():
    xml = MOCK_XML.replace('<tag k="traffic_light_id" v="201"/>', "")
    nodes = parse_nodes(xml)
    heads = parse_signal_heads(xml, nodes)
    assert heads[0]["panel_width"] == pytest.approx(1.25)
    assert heads[0]["signal_type"] == "vehicle"


def test_parse_traffic_lights_ignores_non_traffic_light_relations():
    xml = MOCK_XML.replace('v="traffic_light"', 'v="traffic_sign"')
    nodes = parse_nodes(xml)
    assert parse_traffic_lights(xml, nodes) == []


def test_parse_lanes_builds_center_line_from_left_right_ways():
    nodes = parse_nodes(MOCK_XML)
    lanes = parse_lanes(MOCK_XML, nodes)
    assert len(lanes) == 1
    lane = lanes[0]
    assert lane.id == "50"
    assert lane.center_line == [Point3D(100.0, 202.0, 5.0), Point3D(110.0, 202.0, 5.0)]


def test_parse_lanes_ignores_non_road_relations():
    xml = MOCK_XML.replace('v="road"', 'v="crosswalk"')
    nodes = parse_nodes(xml)
    assert parse_lanes(xml, nodes) == []


# Two lanelets in sequence (100 -> 101, sharing node "2"/"5" at the junction),
# only the second of which references a traffic_light regulatory_element --
# used to test direct_tl_ids extraction and next_lane_ids successor linking.
TOPOLOGY_MOCK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm generator="test">
  <node id="1"><tag k="local_x" v="0.0"/><tag k="local_y" v="0.0"/></node>
  <node id="2"><tag k="local_x" v="10.0"/><tag k="local_y" v="0.0"/></node>
  <node id="3"><tag k="local_x" v="20.0"/><tag k="local_y" v="0.0"/></node>
  <node id="4"><tag k="local_x" v="0.0"/><tag k="local_y" v="4.0"/></node>
  <node id="5"><tag k="local_x" v="10.0"/><tag k="local_y" v="4.0"/></node>
  <node id="6"><tag k="local_x" v="20.0"/><tag k="local_y" v="4.0"/></node>
  <way id="10"><nd ref="1"/><nd ref="2"/></way>
  <way id="11"><nd ref="4"/><nd ref="5"/></way>
  <way id="12"><nd ref="2"/><nd ref="3"/></way>
  <way id="13"><nd ref="5"/><nd ref="6"/></way>
  <relation id="900">
    <tag k="type" v="regulatory_element"/>
    <tag k="subtype" v="traffic_light"/>
  </relation>
  <relation id="100">
    <member type="way" role="left" ref="10"/>
    <member type="way" role="right" ref="11"/>
    <tag k="type" v="lanelet"/>
    <tag k="subtype" v="road"/>
  </relation>
  <relation id="101">
    <member type="way" role="left" ref="12"/>
    <member type="way" role="right" ref="13"/>
    <member type="relation" role="regulatory_element" ref="900"/>
    <tag k="type" v="lanelet"/>
    <tag k="subtype" v="road"/>
  </relation>
</osm>
"""


def test_parse_lanes_extracts_direct_tl_ids_from_regulatory_element_member():
    nodes = parse_nodes(TOPOLOGY_MOCK_XML)
    lanes = {lane.id: lane for lane in parse_lanes(TOPOLOGY_MOCK_XML, nodes)}
    assert lanes["101"].direct_tl_ids == ["900"]
    assert lanes["100"].direct_tl_ids == []


def test_parse_lanes_links_successor_via_shared_left_way_endpoint():
    nodes = parse_nodes(TOPOLOGY_MOCK_XML)
    lanes = {lane.id: lane for lane in parse_lanes(TOPOLOGY_MOCK_XML, nodes)}
    assert lanes["100"].next_lane_ids == ["101"]
    assert lanes["101"].next_lane_ids == []


def test_parse_lanes_ignores_non_traffic_light_regulatory_elements():
    xml = TOPOLOGY_MOCK_XML.replace('<tag k="subtype" v="traffic_light"/>', '<tag k="subtype" v="traffic_sign"/>')
    nodes = parse_nodes(xml)
    lanes = {lane.id: lane for lane in parse_lanes(xml, nodes)}
    assert lanes["101"].direct_tl_ids == []


# Exact affine relationship: lat = 35 + 9e-6 * y, lon = 139 + 1.1e-5 * x --
# the least-squares fit must recover it (and thus predict unseen points).
LATLON_MOCK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm generator="test">
  <node id="1" lat="35.0" lon="139.0"><tag k="local_x" v="0.0"/><tag k="local_y" v="0.0"/></node>
  <node id="2" lat="35.0009" lon="139.0"><tag k="local_x" v="0.0"/><tag k="local_y" v="100.0"/></node>
  <node id="3" lat="35.0" lon="139.0011"><tag k="local_x" v="100.0"/><tag k="local_y" v="0.0"/></node>
  <node id="4" lat="35.0009" lon="139.0011"><tag k="local_x" v="100.0"/><tag k="local_y" v="100.0"/></node>
</osm>
"""


def test_parse_latlon_transform_recovers_affine_mapping():
    transform = parse_latlon_transform(LATLON_MOCK_XML)
    assert transform is not None
    a, b, c = transform["lat"]
    d, e, f = transform["lon"]
    x, y = 50.0, 25.0  # not one of the fitted sample points
    assert a * x + b * y + c == pytest.approx(35.0 + 9e-6 * 25.0, abs=1e-9)
    assert d * x + e * y + f == pytest.approx(139.0 + 1.1e-5 * 50.0, abs=1e-9)


def test_parse_latlon_transform_none_without_latlon_attributes():
    # TOPOLOGY_MOCK_XML's nodes carry only local_x/local_y, no lat/lon attrs
    assert parse_latlon_transform(TOPOLOGY_MOCK_XML) is None
