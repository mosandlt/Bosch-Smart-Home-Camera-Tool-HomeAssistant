"""Regression tests: SHC class renames (2026-05-07).

BoschSHCCamera, BoschSHCCameraConfigFlow, BoschSHCCameraOptionsFlow were
renamed to drop the legacy SHC prefix. Entity IDs and unique IDs are
unchanged. These tests guard against re-introducing the old names.
"""

import importlib


def test_bosch_camera_importable():
    """BoschCamera is the current class name in camera.py."""
    mod = importlib.import_module("custom_components.bosch_shc_camera.camera")
    assert hasattr(mod, "BoschCamera"), "BoschCamera must be exported from camera.py"


def test_bosch_shc_camera_gone():
    """BoschSHCCamera must no longer exist — old name was removed."""
    mod = importlib.import_module("custom_components.bosch_shc_camera.camera")
    assert not hasattr(mod, "BoschSHCCamera"), (
        "BoschSHCCamera still exists — remove the old name from camera.py"
    )


def test_bosch_camera_config_flow_importable():
    """BoschCameraConfigFlow is the current class name in config_flow.py."""
    mod = importlib.import_module("custom_components.bosch_shc_camera.config_flow")
    assert hasattr(mod, "BoschCameraConfigFlow"), (
        "BoschCameraConfigFlow must be exported from config_flow.py"
    )


def test_bosch_camera_options_flow_importable():
    """BoschCameraOptionsFlow is the current class name in config_flow.py."""
    mod = importlib.import_module("custom_components.bosch_shc_camera.config_flow")
    assert hasattr(mod, "BoschCameraOptionsFlow"), (
        "BoschCameraOptionsFlow must be exported from config_flow.py"
    )


def test_old_config_flow_names_gone():
    """Old SHC-prefixed config flow names must no longer exist."""
    mod = importlib.import_module("custom_components.bosch_shc_camera.config_flow")
    assert not hasattr(mod, "BoschSHCCameraConfigFlow"), (
        "BoschSHCCameraConfigFlow still exists — remove the old name"
    )
    assert not hasattr(mod, "BoschSHCCameraOptionsFlow"), (
        "BoschSHCCameraOptionsFlow still exists — remove the old name"
    )


def test_bosch_camera_unique_id_unchanged():
    """Renaming the class must not change the unique_id format (no migration needed)."""
    import inspect
    import re
    mod = importlib.import_module("custom_components.bosch_shc_camera.camera")
    src = inspect.getsource(mod.BoschCamera)
    # unique_id must still use the old bosch_shc_cam_ prefix (no migration yet)
    assert "bosch_shc_cam_" in src, (
        "BoschCamera.unique_id must use the bosch_shc_cam_ prefix "
        "(SHC = Smart Home Camera — correct naming, no migration needed)."
    )
