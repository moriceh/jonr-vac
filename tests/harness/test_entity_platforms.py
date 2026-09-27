"""Harness tests: entity construction, feature flags, command dispatch."""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from homeassistant.components.vacuum import VacuumEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.jonr_vac.coordinator import JonrVacuumCoordinator
from custom_components.jonr_vac.device import VacuumStatus
from custom_components.jonr_vac.number import VolumeNumber, async_setup_entry as number_setup
from custom_components.jonr_vac.const import (
    CONF_DEVICE_ID,
    CONF_PASS_TOKEN,
    CONF_SERVER,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    CONF_USERNAME,
)
from custom_components.jonr_vac.select import (
    JonrActiveMapSelect,
    JonrVacuumSelect,
    async_setup_entry as select_setup,
)
from custom_components.jonr_vac.spec.types import Action, MapCapability
from custom_components.jonr_vac.switch import (
    AlarmSwitch,
    RepeatSwitch,
    async_setup_entry as switch_setup,
)
from custom_components.jonr_vac.vacuum import JonrVacuum

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATUS = VacuumStatus(
    activity="docked",
    raw_status=0,
    battery=80,
    fault=0,
    fan_speed_raw=1,
    water_level_raw=2,
    mode_raw=None,
    sweep_type_raw=None,
    repeat_raw=1,
    alarm_raw=0,
    volume_raw=5,
    door_state_raw=None,
    main_brush_life=90,
    side_brush_life=90,
    filter_life=90,
    mop_life=None,
    dust_bag_life=None,
    detergent_life=None,
    clean_area=0,
    clean_time=0,
)


def _make_coordinator(core_overrides: dict | None = None) -> MagicMock:
    """Return a minimal coordinator mock with configurable core attributes."""
    core = MagicMock()
    core.charge = MagicMock()      # return_home supported
    core.locate = MagicMock()      # locate supported
    core.alarm = MagicMock()       # alarm supported
    core.repeat = MagicMock()      # repeat supported
    core.volume = MagicMock()      # volume supported
    core.fan_speeds = {"quiet": 1, "normal": 2}
    core.water_levels = {"off": 0, "low": 1}
    core.modes = None              # no mode select
    core.sweep_types = None        # no sweep_type select

    if core_overrides:
        for k, v in core_overrides.items():
            setattr(core, k, v)

    device = MagicMock()
    device.model = "dreame.vacuum.p2008"
    device.core = core

    coordinator = MagicMock()
    coordinator.device = device
    coordinator.data = _STATUS
    coordinator.hass = MagicMock()
    return coordinator


def _make_entry(unique_id: str = "AA:BB:CC:DD:EE:FF") -> MagicMock:
    entry = MagicMock()
    entry.unique_id = unique_id
    entry.entry_id = "test_entry_id"
    entry.title = "Test Vacuum"
    entry.runtime_data = MagicMock()
    return entry


# ---------------------------------------------------------------------------
# Vacuum feature flags
# ---------------------------------------------------------------------------


def test_vacuum_base_features_always_present() -> None:
    """START, PAUSE, STOP, STATE are always set regardless of core."""
    core_overrides = {"charge": None, "locate": None, "alarm": None}
    coord = _make_coordinator(core_overrides)
    entry = _make_entry()
    vac = JonrVacuum(coord, entry)
    flags = vac.supported_features
    assert flags & VacuumEntityFeature.START
    assert flags & VacuumEntityFeature.PAUSE
    assert flags & VacuumEntityFeature.STOP
    assert flags & VacuumEntityFeature.STATE


def test_vacuum_return_home_added_when_core_has_charge() -> None:
    coord = _make_coordinator()
    entry = _make_entry()
    vac = JonrVacuum(coord, entry)
    assert vac.supported_features & VacuumEntityFeature.RETURN_HOME


def test_vacuum_return_home_absent_when_no_charge() -> None:
    coord = _make_coordinator({"charge": None})
    entry = _make_entry()
    vac = JonrVacuum(coord, entry)
    assert not (vac.supported_features & VacuumEntityFeature.RETURN_HOME)


def test_vacuum_locate_added_when_core_has_locate() -> None:
    coord = _make_coordinator()
    entry = _make_entry()
    vac = JonrVacuum(coord, entry)
    assert vac.supported_features & VacuumEntityFeature.LOCATE


def test_vacuum_locate_added_via_alarm_when_no_locate() -> None:
    """LOCATE is also set when only alarm is present (alarm IS the locate)."""
    coord = _make_coordinator({"locate": None})  # alarm still present
    entry = _make_entry()
    vac = JonrVacuum(coord, entry)
    assert vac.supported_features & VacuumEntityFeature.LOCATE


def test_vacuum_locate_absent_when_neither_locate_nor_alarm() -> None:
    coord = _make_coordinator({"locate": None, "alarm": None})
    entry = _make_entry()
    vac = JonrVacuum(coord, entry)
    assert not (vac.supported_features & VacuumEntityFeature.LOCATE)


# ---------------------------------------------------------------------------
# Select entity conditional creation
# ---------------------------------------------------------------------------


async def test_select_setup_creates_only_backed_selects(hass: HomeAssistant) -> None:
    """Only selects whose core attr is truthy should be created."""
    coord = _make_coordinator()  # fan_speeds + water_levels; no modes/sweep_types
    entry = _make_entry()
    entry.runtime_data.control = coord

    added: list = []
    # async_add_entities receives a generator; extend consumes it.
    await select_setup(hass, entry, lambda entities: added.extend(entities))

    keys = {e._key for e in added}
    assert "fan_speed" in keys
    assert "water_level" in keys
    assert "mode" not in keys
    assert "sweep_type" not in keys


async def test_select_setup_creates_nothing_when_all_absent(hass: HomeAssistant) -> None:
    overrides = {"fan_speeds": None, "water_levels": None, "modes": None, "sweep_types": None}
    coord = _make_coordinator(overrides)
    entry = _make_entry()
    entry.runtime_data.control = coord

    added: list = []
    await select_setup(hass, entry, lambda entities: added.extend(entities))
    assert added == []


# ---------------------------------------------------------------------------
# Switch entity conditional creation
# ---------------------------------------------------------------------------


async def test_switch_setup_creates_repeat_and_alarm(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    entry = _make_entry()
    entry.runtime_data.control = coord

    added: list = []
    await switch_setup(hass, entry, lambda entities: added.extend(entities))

    types = {type(e) for e in added}
    assert RepeatSwitch in types
    assert AlarmSwitch in types


async def test_switch_setup_no_entities_when_absent(hass: HomeAssistant) -> None:
    coord = _make_coordinator({"repeat": None, "alarm": None})
    entry = _make_entry()
    entry.runtime_data.control = coord

    added: list = []
    await switch_setup(hass, entry, lambda entities: added.extend(entities))
    assert added == []


# ---------------------------------------------------------------------------
# Number entity conditional creation
# ---------------------------------------------------------------------------


async def test_number_setup_creates_volume_when_supported(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    entry = _make_entry()
    entry.runtime_data.control = coord

    added: list = []
    await number_setup(hass, entry, lambda entities: added.extend(entities))
    assert len(added) == 1
    assert isinstance(added[0], VolumeNumber)


async def test_number_setup_no_entity_when_no_volume(hass: HomeAssistant) -> None:
    coord = _make_coordinator({"volume": None})
    entry = _make_entry()
    entry.runtime_data.control = coord

    added: list = []
    await number_setup(hass, entry, lambda entities: added.extend(entities))
    assert added == []


def test_c107_volume_uses_full_hardware_range() -> None:
    coord = _make_coordinator()
    coord.device.profile.profile_id = "xiaomi.c107"
    coord.device.profile.brand = "xiaomi"

    number = VolumeNumber(coord, _make_entry())

    assert number.native_max_value == 100


# ---------------------------------------------------------------------------
# Command dispatch: vacuum entity
# ---------------------------------------------------------------------------


async def test_vacuum_start_calls_device_and_refreshes(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_schedule_confirm = MagicMock()
    entry = _make_entry()
    vac = JonrVacuum(coord, entry)
    vac.hass = hass

    await vac.async_start()

    coord.device.start.assert_called_once()
    coord.async_schedule_confirm.assert_called_once()


async def test_vacuum_stop_calls_device_and_refreshes(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_schedule_confirm = MagicMock()
    entry = _make_entry()
    vac = JonrVacuum(coord, entry)
    vac.hass = hass

    await vac.async_stop()

    coord.device.stop.assert_called_once()
    coord.async_schedule_confirm.assert_called_once()


async def test_vacuum_pause_calls_device_and_refreshes(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_schedule_confirm = MagicMock()
    entry = _make_entry()
    vac = JonrVacuum(coord, entry)
    vac.hass = hass

    await vac.async_pause()

    coord.device.pause.assert_called_once()
    coord.async_schedule_confirm.assert_called_once()


async def test_vacuum_return_home_calls_device_and_refreshes(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_schedule_confirm = MagicMock()
    entry = _make_entry()
    vac = JonrVacuum(coord, entry)
    vac.hass = hass

    await vac.async_return_to_base()

    coord.device.return_home.assert_called_once()
    coord.async_schedule_confirm.assert_called_once()


async def test_vacuum_locate_calls_device(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    entry = _make_entry()
    vac = JonrVacuum(coord, entry)
    vac.hass = hass

    await vac.async_locate()

    coord.device.locate.assert_called_once()


async def test_vacuum_clean_segment_uses_local_when_no_cloud_session(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator()
    coord.async_schedule_confirm = MagicMock()
    entry = _make_entry()
    entry.data = {}
    vac = JonrVacuum(coord, entry)
    vac.hass = hass

    with patch("custom_components.jonr_vac.vacuum.XiaomiCloud") as cloud_cls:
        await vac.async_clean_segment(segments=[1, 2])

    cloud_cls.assert_not_called()
    coord.device.clean_segments.assert_called_once_with([1, 2])
    coord.async_schedule_confirm.assert_called_once()


async def test_vacuum_clean_segment_uses_cloud_first_when_session_present(
    hass: HomeAssistant,
) -> None:
    """Cloud is tried before local when a session exists; on success local is
    never touched — the reverse of the pre-v1.2.2 local-first order."""
    coord = _make_coordinator()
    coord.device.room_clean_start_params.return_value = (
        SimpleNamespace(siid=2, aiid=7),
        ["1,2"],
    )
    coord.device.room_clean_set_params.return_value = (
        SimpleNamespace(siid=7, aiid=3),
        [0, 1, "1,2"],
    )
    coord.async_schedule_confirm = MagicMock()
    entry = _make_entry()
    entry.data = {
        CONF_USERNAME: "user@example.com",
        CONF_USER_ID: "uid",
        CONF_SSECURITY: "ssec",
        CONF_SERVICE_TOKEN: "svc",
        CONF_PASS_TOKEN: "pass",
        CONF_SERVER: "sg",
        CONF_DEVICE_ID: "did123",
    }
    vac = JonrVacuum(coord, entry)
    vac.hass = hass

    cloud = MagicMock()
    # set-room-clean rejected, start-room-sweep accepted — cloud still resolves
    # on its own without ever falling back to local.
    cloud.cloud_action.side_effect = [{"code": -1}, {"code": 0}]
    with patch("custom_components.jonr_vac.vacuum.XiaomiCloud", return_value=cloud):
        await vac.async_clean_segment(segments=[1, 2])

    coord.device.clean_segments.assert_not_called()
    cloud.cloud_action.assert_has_calls(
        [
            call("sg", "did123", 7, 3, [0, 1, "1,2"]),
            call("sg", "did123", 2, 7, ["1,2"]),
        ]
    )
    coord.async_schedule_confirm.assert_called_once()


async def test_vacuum_clean_segment_falls_back_to_local_when_cloud_errors(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator()
    coord.async_schedule_confirm = MagicMock()
    entry = _make_entry()
    entry.data = {
        CONF_USERNAME: "user@example.com",
        CONF_USER_ID: "uid",
        CONF_SSECURITY: "ssec",
        CONF_SERVICE_TOKEN: "svc",
        CONF_PASS_TOKEN: "pass",
        CONF_SERVER: "sg",
        CONF_DEVICE_ID: "did123",
    }
    vac = JonrVacuum(coord, entry)
    vac.hass = hass

    cloud = MagicMock()
    cloud.restore_session.side_effect = RuntimeError("cloud session invalid")
    with patch("custom_components.jonr_vac.vacuum.XiaomiCloud", return_value=cloud):
        await vac.async_clean_segment(segments=[1, 2])

    coord.device.clean_segments.assert_called_once_with([1, 2])
    coord.async_schedule_confirm.assert_called_once()


async def test_vacuum_clean_segment_does_not_cloud_retry_other_failures(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator()
    coord.device.clean_segments.side_effect = RuntimeError("boom")
    coord.async_schedule_confirm = MagicMock()
    entry = _make_entry()
    entry.data = {}
    vac = JonrVacuum(coord, entry)
    vac.hass = hass

    with (
        patch("custom_components.jonr_vac.vacuum.XiaomiCloud") as cloud_cls,
        pytest.raises(HomeAssistantError, match="Room cleaning failed"),
    ):
        await vac.async_clean_segment(segments=[1, 2])

    cloud_cls.assert_not_called()
    coord.async_schedule_confirm.assert_not_called()


# ---------------------------------------------------------------------------
# Command dispatch: select entity
# ---------------------------------------------------------------------------


async def test_select_option_calls_setter_and_refreshes(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_schedule_confirm = MagicMock()
    entry = _make_entry()

    sel = JonrVacuumSelect(coord, entry, "fan_speed", "fan_speeds", "fan_speed_raw", "set_fan_speed")
    sel.hass = hass

    await sel.async_select_option("normal")

    coord.device.set_fan_speed.assert_called_once_with("normal")
    coord.async_schedule_confirm.assert_called_once()


async def test_select_option_flips_option_before_the_poll(hass: HomeAssistant) -> None:
    """The displayed option must flip from the written value immediately —
    the confirmation poll is fire-and-forget (async_schedule_confirm) and can
    ride out UDP timeouts."""
    coord = _make_coordinator()
    coord.async_schedule_confirm = MagicMock()
    entry = _make_entry()

    sel = JonrVacuumSelect(coord, entry, "fan_speed", "fan_speeds", "fan_speed_raw", "set_fan_speed")
    sel.hass = hass
    assert sel.current_option == "quiet"  # fan_speed_raw == 1

    def _apply(attr: str, raw: int) -> None:
        coord.data = dataclasses.replace(coord.data, **{attr: raw})

    coord.async_apply_raw = MagicMock(side_effect=_apply)

    await sel.async_select_option("normal")

    coord.async_apply_raw.assert_called_once_with("fan_speed_raw", 2)
    assert sel.current_option == "normal"
    coord.async_schedule_confirm.assert_called_once()


def _push_coordinator() -> MagicMock:
    """Coordinator mock with the real apply methods bound, over the shared
    snapshot, so folding is exercised for real."""
    coord = MagicMock()
    coord.data = _STATUS
    coord.device.core.status_map = {5: "cleaning"}
    coord.async_apply_raw = lambda attr, raw: JonrVacuumCoordinator.async_apply_raw(coord, attr, raw)
    coord.async_apply_push = lambda field, raw: JonrVacuumCoordinator.async_apply_push(coord, field, raw)
    return coord


def test_apply_push_folds_gear_readback() -> None:
    """A pushed gear value (e.g. count changed in Mi Home) must reach the
    snapshot now, not on the next poll."""
    coord = _push_coordinator()

    coord.async_apply_push("count", 2)

    new = coord.async_set_updated_data.call_args[0][0]
    assert new.count_raw == 2


def test_apply_push_still_folds_status_with_activity() -> None:
    coord = _push_coordinator()

    coord.async_apply_push("status", 5)

    new = coord.async_set_updated_data.call_args[0][0]
    assert new.raw_status == 5
    assert new.activity == "cleaning"


def test_apply_push_folds_ext_setting() -> None:
    """An ext setting toggled in Mi Home (auto-drying here, 17/21) must reach
    the snapshot instantly — folding it only for the gears left the long tail
    waiting on the slow UDP poll (2026-09-25 watch-set extension)."""
    coord = _push_coordinator()

    coord.async_apply_push("auto_drying", 1)

    new = coord.async_set_updated_data.call_args[0][0]
    assert new.auto_drying_raw == 1


def test_push_raw_attrs_table_matches_status_and_profile() -> None:
    """A typo in _PUSH_RAW_ATTRS drops a fold in silence: the target must be
    a real VacuumStatus attribute and the key a field xtl declares (else the
    __init__ getattr watch loop never arms the prop)."""
    from dataclasses import fields as dc_fields

    from custom_components.jonr_vac.spec.types import XtlCoreCapability

    attrs = {f.name for f in dc_fields(VacuumStatus)}
    core_fields = {f.name for f in dc_fields(XtlCoreCapability)}
    for field, attr in JonrVacuumCoordinator._PUSH_RAW_ATTRS.items():
        assert attr in attrs, f"{field}: unknown VacuumStatus attr {attr}"
        assert field in core_fields, f"{field}: not an XtlCoreCapability field"


def test_watch_tuples_and_push_table_stay_in_sync() -> None:
    """Every watched gear/ext field needs a fold entry (a missing one makes
    the push a silent no-op), and the table must not name fields the watch
    loops never arm (dead entries pretending to be folded)."""
    from custom_components.jonr_vac import _WATCH_EXT_SETTINGS, _WATCH_GEARS

    watched = set(_WATCH_GEARS) | set(_WATCH_EXT_SETTINGS)
    assert watched == set(JonrVacuumCoordinator._PUSH_RAW_ATTRS)


def test_apply_raw_skips_when_unchanged() -> None:
    coord = _push_coordinator()

    coord.async_apply_raw("fan_speed_raw", 1)  # snapshot already says 1

    coord.async_set_updated_data.assert_not_called()


def _make_map_coordinator() -> MagicMock:
    coord = MagicMock()
    coord.map_list_meta = [
        {"id": 1, "name": "Ground", "cur": True},
        {"id": 2, "name": "Upstairs", "cur": False},
    ]
    coord.async_request_map_upload = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    return coord


async def test_active_map_select_options_current_and_switch_uses_local_when_no_cloud_session(
    hass: HomeAssistant,
) -> None:
    coord = _make_map_coordinator()
    entry = _make_entry()
    entry.data = {}

    sel = JonrActiveMapSelect(coord, entry)
    sel.hass = hass

    assert sel.options == ["Ground", "Upstairs"]
    assert sel.current_option == "Ground"

    with patch("custom_components.jonr_vac.vacuum.XiaomiCloud") as cloud_cls:
        await sel.async_select_option("Upstairs")

    cloud_cls.assert_not_called()
    coord.device.set_current_map.assert_called_once_with(2)
    coord.async_request_map_upload.assert_awaited_once_with(2)
    coord.async_request_refresh.assert_awaited_once()


async def test_active_map_select_switch_uses_cloud_first_when_session_present(
    hass: HomeAssistant,
) -> None:
    coord = _make_map_coordinator()
    coord.device.profile.map = MapCapability(
        service=7, set_current_map=Action(siid=7, aiid=8)
    )
    entry = _make_entry()
    entry.data = {
        CONF_USERNAME: "user@example.com",
        CONF_USER_ID: "uid",
        CONF_SSECURITY: "ssec",
        CONF_SERVICE_TOKEN: "svc",
        CONF_PASS_TOKEN: "pass",
        CONF_SERVER: "sg",
        CONF_DEVICE_ID: "did123",
    }

    sel = JonrActiveMapSelect(coord, entry)
    sel.hass = hass

    cloud = MagicMock()
    cloud.cloud_action.return_value = {"code": 0}
    with patch("custom_components.jonr_vac.vacuum.XiaomiCloud", return_value=cloud):
        await sel.async_select_option("Upstairs")

    cloud.cloud_action.assert_called_once_with("sg", "did123", 7, 8, [2])
    coord.device.set_current_map.assert_not_called()
    coord.async_request_map_upload.assert_awaited_once_with(2)
    coord.async_request_refresh.assert_awaited_once()


async def test_active_map_select_switch_falls_back_to_local_when_cloud_errors(
    hass: HomeAssistant,
) -> None:
    coord = _make_map_coordinator()
    coord.device.profile.map = MapCapability(
        service=7, set_current_map=Action(siid=7, aiid=8)
    )
    entry = _make_entry()
    entry.data = {
        CONF_USERNAME: "user@example.com",
        CONF_USER_ID: "uid",
        CONF_SSECURITY: "ssec",
        CONF_SERVICE_TOKEN: "svc",
        CONF_PASS_TOKEN: "pass",
        CONF_SERVER: "sg",
        CONF_DEVICE_ID: "did123",
    }

    sel = JonrActiveMapSelect(coord, entry)
    sel.hass = hass

    cloud = MagicMock()
    cloud.restore_session.side_effect = RuntimeError("cloud session invalid")
    with patch("custom_components.jonr_vac.vacuum.XiaomiCloud", return_value=cloud):
        await sel.async_select_option("Upstairs")

    coord.device.set_current_map.assert_called_once_with(2)
    coord.async_request_map_upload.assert_awaited_once_with(2)
    coord.async_request_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# Command dispatch: switch entity (repeat + alarm)
# ---------------------------------------------------------------------------


async def test_repeat_switch_turn_on_calls_device(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_schedule_confirm = MagicMock()
    entry = _make_entry()

    sw = RepeatSwitch(coord, entry)
    sw.hass = hass

    await sw.async_turn_on()

    coord.device.set_repeat.assert_called_once_with(True)
    coord.async_schedule_confirm.assert_called_once()


async def test_repeat_switch_turn_off_calls_device(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_schedule_confirm = MagicMock()
    entry = _make_entry()

    sw = RepeatSwitch(coord, entry)
    sw.hass = hass

    await sw.async_turn_off()

    coord.device.set_repeat.assert_called_once_with(False)
    coord.async_schedule_confirm.assert_called_once()


async def test_alarm_switch_turn_on_calls_device(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_schedule_confirm = MagicMock()
    entry = _make_entry()

    sw = AlarmSwitch(coord, entry)
    sw.hass = hass

    await sw.async_turn_on()

    coord.device.set_alarm.assert_called_once_with(True)
    coord.async_schedule_confirm.assert_called_once()


# ---------------------------------------------------------------------------
# Command dispatch: volume number
# ---------------------------------------------------------------------------


async def test_volume_set_value_calls_device_and_refreshes(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_schedule_confirm = MagicMock()
    entry = _make_entry()

    num = VolumeNumber(coord, entry)
    num.hass = hass

    await num.async_set_native_value(7.0)

    coord.device.set_volume.assert_called_once_with(7)
    coord.async_schedule_confirm.assert_called_once()

async def test_vacuum_refresh_map_delegates_to_map_coordinator(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator()
    entry = _make_entry()
    entry.runtime_data.map.async_refresh_map_with_movement = AsyncMock()
    entry.runtime_data.mqtt = object()
    vac = JonrVacuum(coord, entry)
    vac.hass = hass

    await vac.async_refresh_map(confirm_movement=True)

    entry.runtime_data.map.async_refresh_map_with_movement.assert_awaited_once_with(
        confirm_movement=True,
        use_mqtt=True,
    )


async def test_vacuum_refresh_map_requires_cloud_map_session(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator()
    entry = _make_entry()
    entry.runtime_data.map = None
    vac = JonrVacuum(coord, entry)
    vac.hass = hass

    with pytest.raises(HomeAssistantError, match="cloud map session"):
        await vac.async_refresh_map(confirm_movement=True)
