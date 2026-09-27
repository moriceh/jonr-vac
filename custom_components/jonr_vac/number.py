"""Number: voice volume."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import JonrConfigEntry
from .const import DOMAIN
from .coordinator import JonrVacuumCoordinator

# Serialise commands to the device (one MIoT write at a time).
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: JonrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.control
    if coordinator.device.core.volume is not None:
        async_add_entities([VolumeNumber(coordinator, entry)])


# xtl.xm2216 volume range: spec 17.15 value-range [0,100], live volume
# read-back 79 — the write channel is set-volume (17.31), see device.set_volume.
_VOLUME_MAX = 100


class VolumeNumber(CoordinatorEntity[JonrVacuumCoordinator], NumberEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "volume"
    _attr_icon = "mdi:volume-high"
    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_volume"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})
        self._attr_native_max_value = _VOLUME_MAX

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.volume_raw

    async def async_set_native_value(self, value: float) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.device.set_volume, int(value)
        )
        # Optimistic slider position; the poll is only the echo (see
        # coordinator.async_schedule_confirm).
        self.coordinator.async_apply_raw("volume_raw", int(value))
        self.coordinator.async_schedule_confirm()
