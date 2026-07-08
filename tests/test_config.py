import pytest

from config import load_config
from models import CameraSpec


def test_load_config_empty_yaml_uses_all_defaults():
    config = load_config("")
    assert config.camera == CameraSpec()
    assert config.signal_type == "both"
    assert config.map_path is None
    assert config.output_path is None
    assert config.blind_only is False
    assert config.point_size == 6.0


def test_load_config_full_yaml():
    yaml_text = """
    map: some_map.osm
    output: result.png
    signal_type: vehicle
    blind_only: true
    point_size: 12.0
    camera:
      height: 2.5
      fov_h: 40.0
      fov_v: 20.0
      min_range: 30.0
      max_range: 150.0
      facing_tolerance_deg: 60.0
    """
    config = load_config(yaml_text)
    assert config.map_path == "some_map.osm"
    assert config.output_path == "result.png"
    assert config.signal_type == "vehicle"
    assert config.blind_only is True
    assert config.point_size == 12.0
    assert config.camera == CameraSpec(
        height=2.5, fov_h=40.0, fov_v=20.0, min_range=30.0, max_range=150.0, facing_tolerance_deg=60.0
    )


def test_load_config_partial_camera_keys_fall_back_to_defaults():
    config = load_config("camera:\n  fov_h: 45.0\n")
    default = CameraSpec()
    assert config.camera.fov_h == 45.0
    assert config.camera.fov_v == default.fov_v
    assert config.camera.max_range == default.max_range


def test_load_config_unknown_camera_key_raises():
    with pytest.raises(ValueError, match="Unknown camera config key"):
        load_config("camera:\n  fvo_h: 45.0\n")  # typo


def test_load_config_invalid_signal_type_raises():
    with pytest.raises(ValueError, match="Invalid signal_type"):
        load_config("signal_type: bicycle\n")


def test_load_config_cameras_list():
    yaml_text = """
    cameras:
      - name: front_tele
        fov_h: 30.0
        max_range: 250.0
      - name: front_wide
        fov_h: 90.0
        max_range: 80.0
        yaw_offset: -10.0
    """
    config = load_config(yaml_text)
    assert len(config.cameras) == 2
    assert config.cameras[0].name == "front_tele"
    assert config.cameras[0].fov_h == 30.0
    assert config.cameras[1].name == "front_wide"
    assert config.cameras[1].yaw_offset == -10.0
    # `camera` stays as the first-camera shorthand
    assert config.camera is config.cameras[0]


def test_load_config_cameras_get_default_names():
    config = load_config("cameras:\n  - fov_h: 30.0\n  - fov_h: 90.0\n")
    assert [c.name for c in config.cameras] == ["camera1", "camera2"]


def test_load_config_rejects_both_camera_and_cameras():
    with pytest.raises(ValueError, match="both"):
        load_config("camera:\n  fov_h: 30.0\ncameras:\n  - fov_h: 90.0\n")


def test_load_config_rejects_duplicate_camera_names():
    with pytest.raises(ValueError, match="Duplicate camera name"):
        load_config("cameras:\n  - name: cam\n  - name: cam\n")
