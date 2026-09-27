"""Buttons: xtl consumable resets (the device takes the type STRING as param)."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import JonrConfigEntry
from .const import DOMAIN
from .coordinator import JonrVacuumCoordinator
from .spec.types import XtlConsumablesCapability, XtlCoreCapability

# Serialise commands to the device (one MIoT write at a time).
PARALLEL_UPDATES = 1

# (translation key, icon, 17.32 consumable type string)
XTL_RESET_BUTTONS = (
    ("reset_side_brush", "mdi:broom", "sideBrush"),
    ("reset_main_brush", "mdi:broom", "rollBrush"),
    ("reset_filter", "mdi:air-filter", "filter"),
    ("reset_mop", "mdi:layers-triple-outline", "mop"),
    ("reset_dust_bag", "mdi:trash-can-outline", "dustbag"),
    ("reset_mop_trough", "mdi:pail", "mopCleaningTrough"),
    ("reset_unit_sensor", "mdi:leak", "engineSensor"),
)

# One-shot building actions (translation key, icon, device method,
# enabled-by-default). clean_building starts a map rebuild — a rare, mostly
# destructive op, so its entity ships disabled until the user opts in.
# The wash/dry/collect-dust sextet retired 2026-09-26: the same six writes
# live on as the live-state toggles in switch.py (XTL_STATION_SWITCHES) now
# that 17.49 robot-status reads the running cycle back.
XTL_ACTION_BUTTONS = (
    ("fast_building", "mdi:map-search", "xtl_fast_building", True),
    ("clean_building", "mdi:broom", "xtl_clean_building", False),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: JonrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.control
    core = coordinator.device.core
    entities = []
    if isinstance(coordinator.device.profile.consumables, XtlConsumablesCapability):
        entities += [
            XtlConsumableResetButton(coordinator, entry, key, icon, ctype)
            for key, icon, ctype in XTL_RESET_BUTTONS
        ]
    if isinstance(core, XtlCoreCapability):
        entities += [
            XtlActionButton(coordinator, entry, key, icon, method, enabled)
            for key, icon, method, enabled in XTL_ACTION_BUTTONS
            if getattr(core, _ACTION_PROP_ATTR[method], None) is not None
        ]
    async_add_entities(entities)


# Which XtlCoreCapability attr backs each action button (build-time gate).
# The station methods (xtl_wash_mop / xtl_stop_*) still exist on the device
# but are driven by switch.py's toggles now, so they leave this table.
_ACTION_PROP_ATTR = {
    "xtl_fast_building": "fast_building_action",
    "xtl_clean_building": "clean_building_action",
}


class XtlConsumableResetButton(CoordinatorEntity[JonrVacuumCoordinator], ButtonEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, key: str, icon: str, ctype: str) -> None:
        super().__init__(coordinator)
        self._ctype = ctype
        self._attr_translation_key = key
        self._attr_icon = icon
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    @property
    def available(self) -> bool:
        # Offer the reset only once the device has reported that consumable.
        return self._ctype in self.coordinator.data.consumable_types

    async def async_press(self) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.device.reset_consumable, self._ctype
        )
        # On xtl the lives come from the ext consumable JSON (17.32), not the
        # core group — only a full confirm poll re-reads them; the press
        # itself must not wait.
        self.coordinator.async_schedule_confirm(full=True)


class XtlActionButton(CoordinatorEntity[JonrVacuumCoordinator], ButtonEntity):
    """One-shot xtl mapping action (fast mapping, rebuild map).

    No entity category: like the vacuum controls themselves these are
    everyday buttons. `clean_building` ships registry-disabled.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, key, icon, method, enabled_default):
        super().__init__(coordinator)
        self._method = method
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_entity_registry_enabled_default = enabled_default
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    async def async_press(self) -> None:
        await self.hass.async_add_executor_job(
            getattr(self.coordinator.device, self._method)
        )
        # The visible effect (return_status, station_error) is an ext read:
        # only a full confirm poll re-reads it.
        self.coordinator.async_schedule_confirm(full=True)
