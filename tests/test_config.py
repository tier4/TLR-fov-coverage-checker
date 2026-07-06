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


def test_load_config_full_yaml():
    yaml_text = """
    map: some_map.osm
    output: result.png
    signal_type: vehicle
    blind_only: true
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
