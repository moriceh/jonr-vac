"""Selects: fan speed, water level, cleaning mode, sweep type, active map."""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import JonrConfigEntry
from .const import DOMAIN
from .coordinator import JonrVacuumCoordinator
from .map_coordinator import JonrMapCoordinator
from .spec.profiles.xtl import xtl_selectable_maps
from .spec.types import XtlMapCapability
from .vacuum import _cloud_set_current_map, _has_cloud_session

# Serialise commands to the device (one MIoT write at a time).
PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)

# (key, core attr holding {name: raw}, VacuumStatus attr with current raw, device setter)
SELECTS = (
    ("fan_speed", "fan_speeds", "fan_speed_raw", "set_fan_speed"),
    ("water_level", "water_levels", "water_level_raw", "set_water_level"),
    ("mode", "modes", "mode_raw", "set_mode"),
    ("sweep_type", "sweep_types", "sweep_type_raw", "set_sweep_type"),
    ("route", "routes", "fine_drag_raw", "set_route"),
    ("count", "counts", "count_raw", "set_count"),
)

# xtl station/cleaning selects — same shape (the value tables live on the
# XtlCoreCapability too); setters go through the dedicated 17.x actions.
XTL_SELECTS = (
    ("carpet_prefer", "carpet_prefers", "carpet_prefer_raw", "set_carpet_prefer"),
    ("dust_collection", "dust_collections", "dust_collection_raw",
     "set_dust_collection"),
    ("drying_time", "drying_times", "drying_time_raw", "set_drying_time"),
    ("mop_wash_frequency", "mop_wash_freqs", "mop_wash_freq_raw",
     "set_mop_wash_freq"),
    # Voice announcements (17.25 language-voice, written through the
    # switch-voice-lang 17.21 action): the option table is the 11 codes the
    # xtl spec enumerates, whatever the robot has actually installed.
    ("voice_language", "voice_languages", "voice_language_raw",
     "set_voice_language"),
    # mop_wash_temp (17.45) is intentionally absent: the xm2216 reports the
    # wash temperature but the firmware never exposes it as an adjustable
    # setting, so no select entity is offered (the raw status stays polled).
)

# Dashboard icons keyed by translation key (user-chosen set 2026-09-25).
# dust_collection is NOT here: its icon tracks the current option (see the
# icon property on JonrVacuumSelect).
_SELECT_ICONS = {
    "fan_speed": "mdi:fan",
    "water_level": "mdi:water-opacity",
    "mode": "mdi:creation",
    "route": "mdi:go-kart-track",
    "count": "mdi:numeric-1-box-multiple-outline",
    "carpet_prefer": "mdi:rug",
    "drying_time": "mdi:hair-dryer",
    "mop_wash_frequency": "mdi:timer-sync",
    "voice_language": "mdi:web",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: JonrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.control
    core = coordinator.device.core
    # Build only the selects whose core value table exists for this model —
    # no empty "sweep type" dropdown on a model that lacks it (e.g. dreame).
    entities: list[SelectEntity] = [
        JonrVacuumSelect(coordinator, entry, *cfg)
        for cfg in (*SELECTS, *XTL_SELECTS)
        if getattr(core, cfg[1], None)
    ]

    map_coordinator = entry.runtime_data.map
    cap = coordinator.device.profile.map
    if (
        map_coordinator is not None
        and isinstance(cap, XtlMapCapability)
        and cap.set_current_map is not None
        and cap.get_map_list is not None
    ):
        entities.append(JonrActiveMapSelect(map_coordinator, entry))

    async_add_entities(entities)


class JonrVacuumSelect(CoordinatorEntity[JonrVacuumCoordinator], SelectEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, key, spec_attr, status_attr, setter):
        super().__init__(coordinator)
        self._key = key
        self._status_attr = status_attr
        self._setter = setter
        self._options_map: dict[str, int] = getattr(coordinator.device.core, spec_attr)
        self._reverse = {v: k for k, v in self._options_map.items()}
        self._attr_translation_key = key
        self._attr_options = list(self._options_map)
        self._attr_icon = _SELECT_ICONS.get(key)
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    @property
    def current_option(self) -> str | None:
        return self._reverse.get(getattr(self.coordinator.data, self._status_attr))

    @property
    def icon(self) -> str | None:
        # Auto-vidage: the option table starts at "none" — show the
        # restore-trash glyph only while the dock actually pumps (the user
        # asked for delete-restore/delete-empty mirroring the switch pairs).
        if self._key == "dust_collection":
            return ("mdi:delete-empty"
                    if self.current_option in (None, "none")
                    else "mdi:delete-restore")
        return super().icon

    async def async_select_option(self, option: str) -> None:
        setter = getattr(self.coordinator.device, self._setter)
        await self.hass.async_add_executor_job(setter, option)
        # Flip the displayed option NOW from the value we just wrote — the
        # poll below can ride out UDP timeouts, and its echo only confirms
        # (or rolls back, if the robot quietly refused).
        raw = self._options_map.get(option)
        if raw is not None:
            self.coordinator.async_apply_raw(self._status_attr, raw)
        # Background confirmation; awaiting it used to block the whole poll
        # (6-8 sequential UDP round-trips, ~45-75 s on a silent chunk) on
        # every dropdown tap — the exact "super lent" complaint.
        self.coordinator.async_schedule_confirm()


def _map_label(m: dict) -> str:
    """Label for a get-map-list entry ({name,id,cur})."""
    return m.get("name") or f"Map {m['id']}"


class JonrActiveMapSelect(CoordinatorEntity[JonrMapCoordinator], SelectEntity):
    """Switch the vacuum's active map.

    Populated from `get-map-list` (via the coordinator's `map_list_meta`), NOT
    from decrypted map data — so the dropdown lists and switches maps even when
    the current cloud upload is undecryptable ("Key B"). Switching serves the
    new map from cache immediately when a readable copy exists.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "active_map"
    _attr_icon = "mdi:map"

    def __init__(self, coordinator: JonrMapCoordinator, entry: JonrConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_active_map"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    def _maps(self) -> list[dict]:
        # Only the catalogue's saved floors are switch targets (plugin rule —
        # see xtl_selectable_maps for why the robot's live draft must never
        # be one: tapping it blanked the map, live 2026-09-26).
        return xtl_selectable_maps(self.coordinator.map_list_meta)

    @property
    def available(self) -> bool:
        # Available whenever we know the map list, regardless of whether the
        # active map's upload currently decrypts (a Key-B active map must not
        # make the switch control disappear — that's when you need it most).
        return bool(self._maps())

    @property
    def options(self) -> list[str]:
        return [_map_label(m) for m in self._maps()]

    @property
    def current_option(self) -> str | None:
        return next((_map_label(m) for m in self._maps() if m.get("cur")), None)

    async def async_select_option(self, option: str) -> None:
        target = next((m for m in self._maps() if _map_label(m) == option), None)
        if target is None:
            return
        map_id = int(target["id"])
        device = self.coordinator.device
        data = self._entry.data
        # The plugin's restore flow (MapListView cardFunClick case 2): while
        # a task is running, stopClean() runs FIRST and switchMap rides its
        # .then(). The xtl firmware ignores switch-map with a task open (the
        # same task-open law start-clean obeys via _stop_and_settle), so a
        # mid-clean switch is a silent no-op without it. The plugin gates
        # the stop behind a confirm dialog; in HA the select tap IS the
        # confirm.
        control = getattr(self._entry.runtime_data, "control", None)
        activity = getattr(getattr(control, "data", None), "activity", None)
        if activity in ("cleaning", "paused", "returning"):
            _LOGGER.info("map-switch: stopping %s task before switch-map %s",
                         activity, map_id)
            try:
                await self.hass.async_add_executor_job(device.stop)
            except Exception:  # noqa: BLE001 - an idle stop may still ack an error
                pass
        if _has_cloud_session(data):
            # xtl switch-map (17.9) goes through the cloud miotspec/action
            # channel — the same one the official app uses; local on fallback.
            try:
                await self.hass.async_add_executor_job(
                    _cloud_set_current_map, data, device, map_id
                )
                _LOGGER.debug("%s: map-switch served via cloud", device.model)
            except Exception as cloud_err:  # noqa: BLE001
                _LOGGER.warning(
                    "%s: cloud map-switch failed, falling back to local: %s",
                    device.model, cloud_err,
                )
                await self.hass.async_add_executor_job(
                    device.set_current_map, map_id
                )
                _LOGGER.debug("%s: map-switch served via local", device.model)
        else:
            await self.hass.async_add_executor_job(
                device.set_current_map, map_id
            )
            _LOGGER.debug(
                "%s: map-switch served via local (no cloud session)",
                device.model,
            )
        # Confirm like the plugin (`res && _getMapInfos()`): the cloud can
        # accept an in-param shape it then silently no-ops on (the whole
        # reason this select "did nothing" before), so after the refresh the
        # target must own cur — otherwise retry over local UDP (the
        # piid-dict form is live-verified there) and refresh again.
        await self.coordinator.async_request_refresh()
        if not any(m.get("cur") and m.get("id") == map_id
                   for m in self._maps()):
            _LOGGER.warning(
                "%s: map-switch to %s not confirmed by the device, retrying "
                "locally", device.model, map_id,
            )
            try:
                await self.hass.async_add_executor_job(
                    device.set_current_map, map_id
                )
            except Exception as local_err:  # noqa: BLE001
                _LOGGER.warning("%s: local switch-map retry failed: %s",
                                device.model, local_err)
            await self.coordinator.async_request_refresh()
