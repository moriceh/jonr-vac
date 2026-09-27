"""Time entities: the two editable DND window endpoints."""
from __future__ import annotations

import datetime

from homeassistant.components.time import TimeEntity
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

# translation_key / unique-id suffix / 17.19 keys written per picker side.
_SIDES = {
    "dnd_start_time": ("start", "mdi:clock-start"),
    "dnd_end_time": ("end", "mdi:clock-end"),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: JonrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.control
    core = coordinator.device.core
    entities: list[TimeEntity] = []
    if (
        isinstance(core, XtlCoreCapability)
        and core.disturb_time is not None
        and core.disturb_time_action is not None
    ):
        entities.extend(
            XtlDndTime(coordinator, entry, key, side, icon)
            for key, (side, icon) in _SIDES.items()
        )
    async_add_entities(entities)


class XtlDndTime(CoordinatorEntity[JonrVacuumCoordinator], TimeEntity):
    """One editable endpoint (start or end) of the Do-Not-Disturb window.

    The robot stores the whole schedule as one 17.19 JSON; the app exposes
    it as two time pickers, so HA does the same. Each picker writes through
    17.30 with everything the other picker and the two `dnd_no_dust` /
    `dnd_no_dry` switches own PRESERVED (xtl_dnd_merged replaces only its
    own half of the window)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, key: str, side: str, icon: str):
        super().__init__(coordinator)
        self._side = side
        self._attr_translation_key = key
        self._attr_icon = icon
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    def _schedule(self) -> dict | None:
        data = self.coordinator.data
        return xtl_parse_dnd(getattr(data, "disturb_time_raw", None)) if data else None

    @property
    def available(self) -> bool:
        return super().available and self._schedule() is not None

    @property
    def native_value(self) -> datetime.time | None:
        sched = self._schedule()
        if sched is None:
            return None

        def pick(hour_key: str, min_key: str) -> datetime.time:
            try:
                return datetime.time(int(sched.get(hour_key, 0)), int(sched.get(min_key, 0)))
            except (TypeError, ValueError):
                return datetime.time(0, 0)

        if self._side == "start":
            return pick("startHour", "startMin")
        return pick("endHour", "endMin")

    async def async_set_value(self, value: datetime.time) -> None:
        kwargs = (
            {"start_hour": value.hour, "start_min": value.minute}
            if self._side == "start"
            else {"end_hour": value.hour, "end_min": value.minute}
        )
        data = self.coordinator.data
        payload = xtl_dnd_merged(
            getattr(data, "disturb_time_raw", None) if data else None, **kwargs
        )
        await self.hass.async_add_executor_job(
            self.coordinator.device.xtl_send_dnd_time, [payload]
        )
        # Install the written truth now (xtl_send_dnd_time mirrors it into
        # the device caches too): the local 17.19 read serves the PRE-write
        # snapshot after a local 17.30 write (live 2026-09-25), so the
        # confirm poll may only confirm this value, never overwrite it.
        self.coordinator.async_apply_raw("disturb_time_raw", payload)
        # disturb_time (17.19) lives in the extended JSON group — a core-only
        # confirm would never re-read it (same rule as the dnd flag switches).
        self.coordinator.async_schedule_confirm(full=True)
