"""Switches: repeat/alarm plus the xtl setting toggles (spec siid 17)."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import JonrConfigEntry
from .const import DOMAIN
from .coordinator import JonrVacuumCoordinator
from .spec.profiles.xtl import xtl_dnd_merged, xtl_parse_dnd
from .spec.types import XtlCoreCapability

# Serialise commands to the device (one MIoT write at a time).
PARALLEL_UPDATES = 1

# xtl toggles: (translation key, icon, VacuumStatus attr, device setter,
# XtlCoreCapability prop attr, optional (VacuumStatus attr, raw value) gate).
# Writes go through the dedicated actions — the props themselves are read+notify
# (device._xtl_flag enforces the law). The optional gate hides the entity unless
# the Mi Home screen shows it too (carpet boost only exists under the adaptive
# carpet strategy). auto_detergent (17.44) and erp_mode (17.42) are deliberately
# absent: the xm2216 firmware exposes neither in the app — phantom features.
# carpet_twice (17.74) / carpet_first (17.75) were retired 2026-09-25 for the
# same reason: the plugin carries their props, actions and strings but renders
# no togglable switch for either ("2 fois" only read-only in the per-carpet
# editor; "prioriser" never rendered at all), so they are firmware features the
# app itself never lets anyone use.
XTL_SWITCHES = (
    ("child_lock", "mdi:account-lock", "child_lock_raw", "set_child_lock",
     "child_lock"),
    ("do_not_disturb", "mdi:weather-night", "disturb_raw", "set_disturb",
     "disturb"),
    ("break_point", "mdi:autorenew", "break_point_raw", "set_break_point",
     "break_point"),
    ("carpet_boost", "mdi:arrow-up-bold-box-outline", "carpet_boost_raw",
     "set_carpet_boost", "carpet_boost", ("carpet_prefer_raw", 0)),
    ("auto_drying", "mdi:hair-dryer", "auto_drying_raw", "set_auto_drying",
     "auto_drying"),
    ("mop_augment", None, "mop_augment_raw", "set_mop_augment", "mop_augment"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: JonrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.control
    core = coordinator.device.core
    entities = []
    if core.repeat is not None:
        entities.append(RepeatSwitch(coordinator, entry))
    if core.alarm is not None:
        entities.append(AlarmSwitch(coordinator, entry))
    if isinstance(core, XtlCoreCapability):
        entities += [
            XtlSettingSwitch(coordinator, entry, *cfg)
            for cfg in XTL_SWITCHES
            if getattr(core, cfg[4], None) is not None
        ]
        if core.disturb_time is not None and core.disturb_time_action is not None:
            # The NPD screen's two check-boxes (the app's "No auto-empty" /
            # "No auto-dry") — notDust/notDry inside the 17.19 JSON.
            entities.append(
                XtlDndFlagSwitch(coordinator, entry, "dnd_no_dust",
                                 "mdi:delete-off", "notDust")
            )
            entities.append(
                XtlDndFlagSwitch(coordinator, entry, "dnd_no_dry",
                                 "mdi:tumble-dryer-off", "notDry")
            )
        if core.robot_status is not None:
            # The live station toggles replace the 2026-09 start/stop button
            # sextet — they need 17.49 to read a real state from, so the
            # whole set is gated on that prop (their unique_ids carry over
            # the retired buttons' wash_mop/dry_mop/collect_dust keys).
            entities += [
                XtlStationSwitch(coordinator, entry, *cfg)
                for cfg in XTL_STATION_SWITCHES
                if getattr(core, cfg[2], None) is not None
            ]
    async_add_entities(entities)


class RepeatSwitch(CoordinatorEntity[JonrVacuumCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "repeat"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_repeat"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    @property
    def is_on(self) -> bool | None:
        raw = self.coordinator.data.repeat_raw
        return None if raw is None else bool(raw)

    async def async_turn_on(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self.coordinator.device.set_repeat, True)
        self.coordinator.async_apply_raw("repeat_raw", 1)
        self.coordinator.async_schedule_confirm()

    async def async_turn_off(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self.coordinator.device.set_repeat, False)
        self.coordinator.async_apply_raw("repeat_raw", 0)
        self.coordinator.async_schedule_confirm()


class AlarmSwitch(CoordinatorEntity[JonrVacuumCoordinator], SwitchEntity):
    """Beep the vacuum to find it (Alarm property)."""

    _attr_has_entity_name = True
    _attr_translation_key = "alarm"
    _attr_icon = "mdi:bell-ring"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_alarm"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    @property
    def is_on(self) -> bool | None:
        raw = self.coordinator.data.alarm_raw
        return None if raw is None else bool(raw)

    async def async_turn_on(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self.coordinator.device.set_alarm, True)
        self.coordinator.async_apply_raw("alarm_raw", 1)
        self.coordinator.async_schedule_confirm()

    async def async_turn_off(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self.coordinator.device.set_alarm, False)
        self.coordinator.async_apply_raw("alarm_raw", 0)
        self.coordinator.async_schedule_confirm()


class XtlSettingSwitch(CoordinatorEntity[JonrVacuumCoordinator], SwitchEntity):
    """One xtl setting toggle (spec siid 17): state reads the polled prop,
    toggling drives the dedicated set-<name> action (device._xtl_flag)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, key, icon, status_attr, setter, _prop,
                 needs=None):
        super().__init__(coordinator)
        self._status_attr = status_attr
        self._setter = setter
        self._needs = needs
        self._attr_translation_key = key
        if icon:
            self._attr_icon = icon
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    @property
    def is_on(self) -> bool | None:
        raw = getattr(self.coordinator.data, self._status_attr)
        return None if raw is None else bool(raw)

    @property
    def available(self) -> bool:
        """Hidden while the Mi Home screen would hide it too (carpet boost
        only shows under the adaptive carpet strategy)."""
        if self._needs is not None:
            attr, want = self._needs
            if getattr(self.coordinator.data, attr, None) != want:
                return False
        return super().available

    async def async_turn_on(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(
            getattr(self.coordinator.device, self._setter), True
        )
        # Same optimistic-flip + background-confirm pattern as the selects:
        # awaiting the poll made every toggle ride the full UDP round-trip
        # chain (silent extended chunk = tens of seconds).
        self.coordinator.async_apply_raw(self._status_attr, 1)
        self.coordinator.async_schedule_confirm()

    async def async_turn_off(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(
            getattr(self.coordinator.device, self._setter), False
        )
        self.coordinator.async_apply_raw(self._status_attr, 0)
        self.coordinator.async_schedule_confirm()


class XtlDndFlagSwitch(CoordinatorEntity[JonrVacuumCoordinator], SwitchEntity):
    """One NPD side-option (the app's "No auto-empty" / "No auto-dry" boxes).

    These live INSIDE the 17.19 disturb-time JSON as notDust/notDry — there is
    no standalone prop for them, so toggling merges the new flag into the
    current schedule and re-sends the whole string through 17.30 (the same
    channel the app uses; device.xtl_send_dnd_time mirrors the read-back)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, key, icon, field):
        super().__init__(coordinator)
        self._field = field  # "notDust" | "notDry"
        self._attr_translation_key = key
        self._attr_icon = icon
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    @property
    def is_on(self) -> bool | None:
        dnd = xtl_parse_dnd(self.coordinator.data.disturb_time_raw)
        if dnd is None:
            return None
        try:
            return bool(int(dnd.get(self._field, 0)))
        except (TypeError, ValueError):
            return None

    async def _async_write(self, on: bool) -> None:
        kw = {"no_dust": on} if self._field == "notDust" else {"no_dry": on}
        payload = xtl_dnd_merged(self.coordinator.data.disturb_time_raw, **kw)
        await self.hass.async_add_executor_job(
            self.coordinator.device.xtl_send_dnd_time, [payload]
        )
        self.coordinator.async_apply_raw("disturb_time_raw", payload)
        # disturb_time (17.19) lives in the extended JSON group — only a full
        # poll re-reads it (same rule as the set_dnd_time service).
        self.coordinator.async_schedule_confirm(full=True)

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_write(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_write(False)


# Station cycles (wash / dry / empty). The write side is the same action the
# old one-shot buttons used — start sends true, stop sends false on the very
# same aiid (plugin setWashMop(false) etc.), so both rides stay on
# wash_mop_action / dry_mop_action / collect_dust_action. The READ side is
# 17.49 robot-status (the plugin's curRobotStatus): WashMop answers the wash
# switch, HotDry+WindDry the dry switch (the plugin renders one "Auto-drying"
# label for both), ClctDust the empty switch; anything else (Charging, None,
# a cleaning token) is off. The token rides the polled/pushed
# station_status_raw, so the toggle tracks the real cycle the same way Mi
# Home's stop buttons light up.
XTL_STATION_SWITCHES = (
    # (translation key, icon, action-prop attr, device setter, stop setter,
    #  robot-status tokens that mean "running" — plain tuple so the AST
    #  table test can read the rows)
    ("wash_mop", "mdi:washing-machine", "wash_mop_action",
     "xtl_wash_mop", "xtl_stop_wash_mop", ("WashMop",)),
    ("dry_mop", "mdi:hair-dryer", "dry_mop_action",
     "xtl_dry_mop", "xtl_stop_dry_mop", ("HotDry", "WindDry")),
    ("collect_dust", "mdi:delete-restore", "collect_dust_action",
     "xtl_collect_dust", "xtl_stop_collect_dust", ("ClctDust",)),
)


class XtlStationSwitch(CoordinatorEntity[JonrVacuumCoordinator], SwitchEntity):
    """One live station cycle (wash-mop / dry-mop / collect-dust).

    Like the setting toggles the write goes through the dedicated action —
    but with the cycle's own bool: ON fires action[true] (start), OFF fires
    action[false] (stop, the plugin's stop-button channel). The state is
    NOT optimistic on top of the write: the robot pushes 17.49 within
    seconds and the __init__ hook folds it, so a cycle that never started
    (robot away from the dock) settles back to off on its own. The confirm
    poll after a press is a full one because the token lives in the ext
    read group."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, key, icon, _prop, setter,
                 stop_setter, running):
        super().__init__(coordinator)
        self._setter = setter
        self._stop_setter = stop_setter
        self._running = running
        self._attr_translation_key = key
        self._attr_icon = icon
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    @property
    def is_on(self) -> bool | None:
        raw = self.coordinator.data.station_status_raw
        return None if raw is None else raw in self._running

    async def _async_write(self, on: bool) -> None:
        await self.hass.async_add_executor_job(
            getattr(self.coordinator.device,
                    self._setter if on else self._stop_setter)
        )
        self.coordinator.async_schedule_confirm(full=True)

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_write(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_write(False)
