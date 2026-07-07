"""Module E (webapp.py) smoke tests: exercise the Flask routes against a
tiny mock map (written to a temp file, since _load_data takes a path) with
a wide-open CameraSpec so the single lane/light pair is always a candidate.
"""

import gzip
import json

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
    assert data == [
        {
            "id": "900",
            "x": pytest.approx(150.25),
            "y": pytest.approx(202.0),
            "facing_yaw": pytest.approx(180.0),
            "signal_type": "vehicle",
        }
    ]


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


def test_point_candidates_endpoint_includes_group_status(client):
    # single-light group in this mock map, so group semantics collapse to
    # exactly this candidate's own is_covered.
    res = client.get("/api/points/0/candidates")
    data = res.get_json()
    c = data["candidates"][0]
    assert c["group_id"] == "refline:150"
    assert c["group_covered"] == c["is_covered"]
    if c["is_covered"]:
        assert data["status"] == "covered"
    elif c["in_fov"]:
        assert data["status"] == "facing_away"
    else:
        assert data["status"] == "out_of_fov"


def test_point_candidates_404_for_unknown_id(client):
    res = client.get("/api/points/999999/candidates")
    assert res.status_code == 404


def test_export_endpoint_returns_gzip_json_snapshot(client):
    res = client.get("/api/export")
    assert res.status_code == 200
    assert res.headers["Content-Disposition"] == "attachment; filename=fov_results.json.gz"
    data = json.loads(gzip.decompress(res.data))
    assert data["format_version"] == webapp._SNAPSHOT_FORMAT_VERSION
    assert "points" in data and "results_by_point" in data and "tl_positions" in data and "tl_facing_yaw" in data


def test_serialize_then_deserialize_round_trip_preserves_candidates(client):
    # exercises the exact --save/--load path without touching the filesystem:
    # a point's detail response must be identical before and after the map
    # and simulation are discarded in favor of the snapshot alone.
    before = client.get("/api/points/0/candidates").get_json()
    lights_before = client.get("/api/traffic_lights").get_json()

    snapshot = webapp._serialize_state()
    webapp._deserialize_state(snapshot)

    after = client.get("/api/points/0/candidates").get_json()
    assert after == before
    assert client.get("/api/traffic_lights").get_json() == lights_before


def test_deserialize_rejects_unknown_format_version(client):
    snapshot = webapp._serialize_state()
    snapshot["format_version"] = 999
    with pytest.raises(ValueError, match="unsupported snapshot format_version"):
        webapp._deserialize_state(snapshot)


def test_write_snapshot_then_read_snapshot_round_trip(tmp_path, client):
    snapshot_path = tmp_path / "snapshot.json.gz"
    original = webapp._serialize_state()

    webapp._write_snapshot(snapshot_path, original)
    loaded = webapp._read_snapshot(snapshot_path)

    assert loaded == original
    # compressed, not a plain-text JSON file
    assert snapshot_path.read_bytes()[:2] == b"\x1f\x8b"


def test_read_snapshot_also_accepts_uncompressed_json(tmp_path, client):
    snapshot_path = tmp_path / "snapshot.json"
    original = webapp._serialize_state()
    snapshot_path.write_text(json.dumps(original), encoding="utf-8")

    assert webapp._read_snapshot(snapshot_path) == original


def test_point_candidates_include_panel_size_from_map(client):
    data = client.get("/api/points/0/candidates").get_json()
    c = data["candidates"][0]
    # panel way 201: nodes (149.9, 202) -> (151.15, 202), height tag 0.45
    assert c["panel_width"] == pytest.approx(1.25)
    assert c["panel_height"] == pytest.approx(0.45)


def test_point_candidates_include_projected_lamps(client):
    data = client.get("/api/points/0/candidates").get_json()
    c = data["candidates"][0]
    # bulb nodes 10 (red) and 11 (yellow), each projected individually
    assert [lamp["color"] for lamp in c["lamps"]] == ["red", "yellow"]
    assert all(lamp["arrow"] is None for lamp in c["lamps"])
    # the mock lane looks due east and both bulbs sit exactly on the view
    # axis (y=202), so each projects to dead-center horizontally...
    assert all(lamp["yaw_diff"] == pytest.approx(0.0) for lamp in c["lamps"])
    # ...but the second bulb is 0.5m farther away, so its elevation angle
    # (same z, longer distance) must be slightly shallower -- individual
    # projection, not one shared offset for the whole light
    pitches = [lamp["pitch_diff"] for lamp in c["lamps"]]
    assert pitches[0] > pitches[1]


def test_meta_includes_latlon_transform(client):
    data = client.get("/api/meta").get_json()
    # MOCK_XML's nodes all carry lat/lon attributes, so a fit must exist
    assert data["latlon_transform"] is not None
    assert set(data["latlon_transform"].keys()) == {"lat", "lon"}
    assert len(data["latlon_transform"]["lat"]) == 3


def test_load_snapshot_route_replaces_state(client):
    snapshot = webapp._serialize_state()
    before = client.get("/api/points/0/candidates").get_json()

    raw = gzip.compress(json.dumps(snapshot).encode("utf-8"))
    res = client.post("/api/load_snapshot", data=raw)
    assert res.status_code == 200
    assert res.get_json()["ok"] is True

    assert client.get("/api/points/0/candidates").get_json() == before


def test_load_snapshot_route_rejects_garbage(client):
    res = client.post("/api/load_snapshot", data=b"not json at all")
    assert res.status_code == 400


def test_load_snapshot_route_rejects_wrong_format_version(client):
    snapshot = webapp._serialize_state()
    snapshot["format_version"] = 999
    res = client.post("/api/load_snapshot", data=json.dumps(snapshot).encode("utf-8"))
    assert res.status_code == 400


def test_load_map_route_reruns_simulation(client):
    before = client.get("/api/meta").get_json()
    res = client.post("/api/load_map", data=MOCK_XML.encode("utf-8"))
    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    # same map + same camera spec -> identical result set
    assert client.get("/api/meta").get_json() == before


def test_load_map_route_rejects_non_xml(client):
    res = client.post("/api/load_map", data=b"definitely not xml")
    assert res.status_code == 400
