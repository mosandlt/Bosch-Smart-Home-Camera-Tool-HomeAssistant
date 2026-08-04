"""Tests for dynamic_devices.register_dynamic_camera_listener — the shared
Quality-Scale Gold `dynamic-devices` helper used by every platform's
async_setup_entry to add entities for cameras that appear after initial
setup."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.bosch_shc_camera.dynamic_devices import (
    register_dynamic_camera_listener,
)


def _register(coordinator_data: dict[str, object], known_cam_ids: set[str]):
    coordinator = SimpleNamespace(
        data=coordinator_data, async_add_listener=MagicMock(return_value=lambda: None)
    )
    async_add_entities = MagicMock()
    build_calls: list[str] = []

    def build_entities_for_cam(cam_id: str) -> list[object]:
        build_calls.append(cam_id)
        return [object()]

    register_dynamic_camera_listener(
        coordinator, known_cam_ids, async_add_entities, build_entities_for_cam
    )
    listener = coordinator.async_add_listener.call_args[0][0]
    return listener, async_add_entities, build_calls


class TestRegisterDynamicCameraListener:
    def test_empty_coordinator_data_is_a_noop(self):
        """A tick with falsy `coordinator.data` (e.g. mid cloud-outage) must
        not raise and must not call async_add_entities."""
        listener, async_add_entities, build_calls = _register({}, set())
        listener()
        async_add_entities.assert_not_called()
        assert build_calls == []

    def test_no_new_cameras_is_a_noop(self):
        listener, async_add_entities, build_calls = _register({"cam1": {}}, {"cam1"})
        listener()
        async_add_entities.assert_not_called()
        assert build_calls == []

    def test_new_camera_triggers_entity_build_and_add(self):
        known: set[str] = {"cam1"}
        listener, async_add_entities, build_calls = _register(
            {"cam1": {}, "cam2": {}}, known
        )
        listener()
        assert build_calls == ["cam2"]
        assert "cam2" in known
        async_add_entities.assert_called_once()
        args, kwargs = async_add_entities.call_args
        assert len(args[0]) == 1
        assert kwargs == {"update_before_add": False}

    def test_multiple_new_cameras_added_in_sorted_order(self):
        known: set[str] = set()
        listener, _async_add_entities, build_calls = _register(
            {"b": {}, "a": {}}, known
        )
        listener()
        assert build_calls == ["a", "b"]
        assert known == {"a", "b"}

    def test_second_tick_does_not_rebuild_already_known_cameras(self):
        known: set[str] = set()
        listener, async_add_entities, build_calls = _register({"cam1": {}}, known)
        listener()
        listener()
        assert build_calls == ["cam1"]
        async_add_entities.assert_called_once()

    def test_returns_the_coordinators_unsubscribe_callback(self):
        coordinator = SimpleNamespace(
            data={}, async_add_listener=MagicMock(return_value="unsub-token")
        )
        result = register_dynamic_camera_listener(
            coordinator, set(), MagicMock(), lambda cam_id: []
        )
        assert result == "unsub-token"
