"""Module E (webapp.py) smoke tests: exercise the Flask routes against a
tiny mock map (written to a temp file, since _load_data takes a path) with
a wide-open CameraSpec so the single lane/light pair is always a candidate.
"""

import pytest

import webapp
from models import CameraSpec

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
    <nd ref="172463"/>
    <nd ref="172464"/>
    <tag k="type" v="traffic_light"/>
    <tag k="subtype" v="red_yellow_green"/>
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


@pytest.fixture
def client(tmp_path):
    map_file = tmp_path / "mock.osm"
    map_file.write_text(MOCK_XML, encoding="utf-8")
    # wide-open spec: the mock lane/light pair should always be a candidate
    webapp._load_data(map_file, CameraSpec(min_range=0.0, max_range=1000.0), None)
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


def test_index_serves_html(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"Traffic Light FOV Coverage Viewer" in res.data


def test_meta_endpoint(client):
    res = client.get("/api/meta")
    assert res.status_code == 200
    data = res.get_json()
    assert data["lane_count"] == 1
    assert data["traffic_light_count"] == 1
    assert data["point_count"] > 0


def test_points_endpoint_returns_expected_shape(client):
    res = client.get("/api/points")
    data = res.get_json()
    assert isinstance(data, list) and len(data) > 0
    assert set(data[0].keys()) == {"id", "lane_id", "x", "y", "z", "status"}
    assert data[0]["status"] in {"covered", "facing_away", "out_of_fov"}


def test_traffic_lights_endpoint(client):
    res = client.get("/api/traffic_lights")
    data = res.get_json()
    assert data == [{"id": "900", "x": pytest.approx(150.25), "y": pytest.approx(202.0)}]


def test_point_candidates_endpoint_offsets_agree_with_normalization(client):
    res = client.get("/api/points/0/candidates")
    assert res.status_code == 200
    data = res.get_json()
    assert len(data["candidates"]) == 1
    c = data["candidates"][0]
    assert c["target_tl_id"] == "900"
    assert c["norm_x"] == pytest.approx(c["yaw_diff"] / (data["fov_h"] / 2.0))
    assert c["norm_y"] == pytest.approx(c["pitch_diff"] / (data["fov_v"] / 2.0))
    assert c["is_covered"] == (c["in_fov"] and c["facing_camera"])


def test_point_candidates_404_for_unknown_id(client):
    res = client.get("/api/points/999999/candidates")
    assert res.status_code == 404
