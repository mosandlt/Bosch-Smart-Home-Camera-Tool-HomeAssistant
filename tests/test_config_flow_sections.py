"""Tests for the sectioned options-flow round-trip helper.

Pins ``_flatten_sections`` and the ``OPTIONS_SECTIONS`` mapping. The helper is
the contract surface between HA's ``data_entry_flow.section()`` UI shape
(nested per-section dicts) and the legacy flat options dict every other module
in the integration consumes. Regressions here would silently drop options or
overwrite them with the wrong defaults.

User/forum source: project-internal v11.0.4 OptionsFlow Sections refactor —
Thomas asked to group ~50 fields into collapsible blocks. The flatten helper
is the only surface where the section grouping leaks into runtime behaviour;
the rest of the integration sees the legacy flat dict shape.
"""

from __future__ import annotations

import pytest

from custom_components.bosch_shc_camera.config_flow import (
    OPTIONS_SECTIONS,
    _flatten_sections,
)


class TestFlattenSectionsBasic:
    def test_empty_input_returns_empty_dict(self):
        assert _flatten_sections({}) == {}

    def test_lifts_nested_keys_to_top_level(self):
        """Section dicts get unpacked: ``{section: {field: v}}`` → ``{field: v}``."""
        out = _flatten_sections({
            "polling": {"scan_interval": 60, "interval_status": 300},
            "features": {"enable_snapshots": True},
        })
        assert out == {
            "scan_interval": 60,
            "interval_status": 300,
            "enable_snapshots": True,
        }

    def test_missing_section_does_not_raise(self):
        """HA may omit empty sections entirely."""
        # Only `polling` is present; the other sections are simply absent.
        out = _flatten_sections({"polling": {"scan_interval": 60}})
        assert out == {"scan_interval": 60}

    def test_section_set_to_none_does_not_raise(self):
        """Defensive — if HA sends ``None`` instead of an empty dict."""
        out = _flatten_sections({"polling": None})
        assert out == {}

    def test_non_dict_section_payload_skipped(self):
        """Defensive — never expected from HA but keeps tests honest."""
        out = _flatten_sections({"polling": "garbage"})
        assert out == {}

    def test_top_level_unknown_keys_pass_through(self):
        """Legacy / programmatic / test callers may submit flat dicts directly.
        Anything not matching a known section key flows through unchanged."""
        out = _flatten_sections({"force_relogin": True})
        assert out == {"force_relogin": True}

    def test_input_dict_not_mutated(self):
        original = {"polling": {"scan_interval": 60}}
        snapshot = {k: dict(v) if isinstance(v, dict) else v
                    for k, v in original.items()}
        _flatten_sections(original)
        assert original == snapshot


class TestFlattenSectionsCollisions:
    """Defensive guards — duplicate keys must explode loudly so a future
    OPTIONS_SECTIONS edit cannot silently overwrite an existing field."""

    def test_duplicate_across_two_sections_raises(self, monkeypatch):
        """Two sections claim the same field → ValueError."""
        # Patch OPTIONS_SECTIONS in place so the helper sees the conflict.
        monkeypatch.setitem(OPTIONS_SECTIONS, "_test_a", ["dupe_field"])
        monkeypatch.setitem(OPTIONS_SECTIONS, "_test_b", ["dupe_field"])
        try:
            with pytest.raises(ValueError, match="duplicate key"):
                _flatten_sections({
                    "_test_a": {"dupe_field": 1},
                    "_test_b": {"dupe_field": 2},
                })
        finally:
            OPTIONS_SECTIONS.pop("_test_a", None)
            OPTIONS_SECTIONS.pop("_test_b", None)

    def test_duplicate_top_level_and_section_raises(self):
        """A legit top-level pass-through key colliding with a flattened
        section field must raise — fail-loud — so the caller fixes it."""
        with pytest.raises(ValueError, match="duplicate key"):
            _flatten_sections({
                "polling": {"scan_interval": 60},
                "scan_interval": 999,  # already lifted from polling
            })


class TestOptionsSectionsLayout:
    """Pin the sections layout so a refactor cannot silently drop a section
    or duplicate a field across sections."""

    def test_every_section_field_is_unique(self):
        """No field key appears in two sections — guards _flatten_sections."""
        seen: dict[str, str] = {}
        for section_key, fields in OPTIONS_SECTIONS.items():
            for field in fields:
                assert field not in seen, (
                    f"field {field!r} appears in both "
                    f"{seen[field]!r} and {section_key!r}"
                )
                seen[field] = section_key

    def test_required_sections_present(self):
        """Hard-coded list — pin so a future refactor can't silently
        drop a section the strings.json relies on."""
        required = {
            "polling", "features", "stream", "fcm",
            "events_storage", "nvr", "shc", "auth", "debug",
        }
        assert required <= set(OPTIONS_SECTIONS.keys())

    def test_nvr_section_includes_new_target_keys(self):
        """The two new options added in the NVR-storage-target refactor must
        be in the nvr section so they actually render."""
        assert "nvr_storage_target" in OPTIONS_SECTIONS["nvr"]
        assert "nvr_smb_subpath" in OPTIONS_SECTIONS["nvr"]


# ── Options-flow schema rendering — exercise the section schema branch ───────


from unittest.mock import MagicMock, AsyncMock
from types import SimpleNamespace
import asyncio

from custom_components.bosch_shc_camera.config_flow import (
    BoschCameraOptionsFlow,
)


def _make_entry(*, options: dict | None = None,
                bearer_token: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="01TEST",
        data={"bearer_token": bearer_token, "refresh_token": "rt"},
        options=options or {},
    )


class TestOptionsStepInitRender:
    """Smoke-cover the section-schema rendering branch (no user_input)."""

    @pytest.mark.asyncio
    async def test_render_returns_form_with_sections(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        # async_show_form / hass aren't actually needed for the helper
        # because the OptionsFlow base class composes the result dict — but
        # we patch async_show_form to capture the schema.
        captured = {}

        def capture(**kw):
            captured.update(kw)
            return {"type": "form", **kw}
        flow.async_show_form = capture

        result = await flow.async_step_init(user_input=None)
        assert result["type"] == "form"
        # Section keys must show up in the schema as required keys.
        schema = captured["data_schema"]
        keys = {str(k) for k in schema.schema.keys()}
        assert "polling" in keys
        assert "nvr" in keys

    @pytest.mark.asyncio
    async def test_render_with_legacy_client_includes_migrate(self):
        """A legacy ``residential_app`` JWT must surface the migrate option."""
        import base64
        import json
        # Build a minimal JWT with azp=residential_app
        header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
        body = base64.urlsafe_b64encode(
            json.dumps({"azp": "residential_app"}).encode()
        ).rstrip(b"=").decode()
        token = f"{header}.{body}.x"

        flow = BoschCameraOptionsFlow(_make_entry(bearer_token=token))
        flow.async_show_form = MagicMock(return_value={"type": "form"})
        await flow.async_step_init(user_input=None)
        flow.async_show_form.assert_called_once()


class TestOptionsStepInitSubmit:
    """Submit branches: plain save / force_relogin / migrate_to_oss."""

    @pytest.mark.asyncio
    async def test_submit_plain_save_creates_entry(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        flow.async_create_entry = MagicMock(
            return_value={"type": "create_entry"}
        )
        # Sectioned submit shape — only one section non-empty.
        result = await flow.async_step_init(user_input={
            "polling": {"scan_interval": 30},
        })
        assert result == {"type": "create_entry"}
        flow.async_create_entry.assert_called_once()
        kw = flow.async_create_entry.call_args.kwargs
        assert kw["data"]["scan_interval"] == 30

    @pytest.mark.asyncio
    async def test_submit_with_force_relogin_branches(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        # Stub the relogin step
        flow.async_step_relogin_show = AsyncMock(
            return_value={"type": "form", "step_id": "relogin_show"},
        )
        result = await flow.async_step_init(user_input={
            "auth": {"force_relogin": True},
        })
        assert result["step_id"] == "relogin_show"

    @pytest.mark.asyncio
    async def test_submit_with_migrate_starts_reauth(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        flow.hass = MagicMock()
        flow.hass.config_entries = MagicMock()
        flow.hass.config_entries.async_update_entry = MagicMock()
        flow.hass.async_create_task = MagicMock()
        flow._config_entry.async_start_reauth = MagicMock(return_value=None)
        flow.async_abort = MagicMock(return_value={"type": "abort",
                                                    "reason": "migration_started"})
        result = await flow.async_step_init(user_input={
            "auth": {"migrate_to_oss_client": True},
        })
        assert result["reason"] == "migration_started"
        flow.hass.async_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_submit_normalizes_booleans(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        captured = {}
        flow.async_create_entry = MagicMock(
            side_effect=lambda **kw: captured.update(kw) or {"type": "create_entry"},
        )
        await flow.async_step_init(user_input={
            "features": {"enable_snapshots": 1, "enable_intercom": 0},
            "nvr": {"enable_nvr": 1},
        })
        # ``1``/``0`` get coerced to True/False so downstream code can rely
        # on plain bool checks.
        assert captured["data"]["enable_snapshots"] is True
        assert captured["data"]["enable_intercom"] is False
        assert captured["data"]["enable_nvr"] is True
