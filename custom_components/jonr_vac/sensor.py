"""Sensors: battery + consumables + clean stats (control coordinator)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfArea, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import JonrConfigEntry
from .const import DOMAIN
from .coordinator import JonrVacuumCoordinator
from .device import VacuumStatus
from .spec.profiles.xtl import (
    XTL_ERROR_CODE_KEYS,
    XTL_MESSAGE_CODE_KEYS,
    XTL_STATION_STATUSES,
    xtl_error_solutions,
    xtl_parse_timers,
    xtl_station_status_option,
    xtl_timer_labels,
    xtl_timers_summary,
)
from .spec.types import (
    ModelProfile,
    XtlConsumablesCapability,
    XtlCoreCapability,
    consumable_life_props,
)

# Read-only platform fed by the coordinator; no device writes to serialise.
PARALLEL_UPDATES = 0

# xtl station/status enum raw -> option id (spec v4 value-lists, verbatim).
XTL_WATER_TANKS = {0: "normal", 1: "uninstalled", 2: "unusable", 3: "fluid_low"}
XTL_DRAIN_TANKS = {0: "normal", 1: "uninstalled", 2: "unusable"}
XTL_INSTALLED = {0: "normal", 1: "uninstalled"}


@dataclass(frozen=True, kw_only=True)
class JonrSensorDescription(SensorEntityDescription):
    value_fn: Callable[[VacuumStatus], int | str | None]
    # Returns True when the profile actually exposes this sensor's data source.
    # None means always include (no capability gate).
    supported_fn: Callable[[ModelProfile], bool] | None = None
    # Extra state attributes (e.g. the parsed clean-records list).
    attrs_fn: Callable[[VacuumStatus], dict] | None = None
    # value_fn variant that additionally receives the map coordinator's room
    # names ({mapId: {roomId: name}}, None when no map is cached), the
    # map titles ({mapId: name}, the Active Map select's rows) and the
    # user's HA language (for translated summaries). Wins over value_fn when
    # set; the entity falls back to ids on its own.
    rooms_value_fn: Callable[
        [VacuumStatus, dict | None, dict | None, str], int | str | None
    ] | None = None
    # attrs_fn variant for translated attribute texts (error solutions).
    attrs_lang_fn: Callable[[VacuumStatus, str], dict] | None = None


_XTL_LIFE_SENSORS = frozenset({
    "main_brush_life", "side_brush_life", "filter_life", "mop_life", "dust_bag_life",
})

# xtl life-sensor key -> consumable type string inside the 17.32 report.
# Devices that never report a type gray the sensor out instead of pinning
# it to `unknown` forever; matches the availability gate the reset buttons
# already use. (The waste-water filter screen entity was retired 2026-09-25
# — the P20 Pro never reports a filterScreen, so it sat permanently grey.)
_XTL_KEY_TO_CTYPE = {
    "main_brush_life": "rollBrush",
    "side_brush_life": "sideBrush",
    "filter_life": "filter",
    "mop_life": "mop",
    "dust_bag_life": "dustbag",
    "mop_trough_life": "mopCleaningTrough",
    "unit_sensor_life": "engineSensor",
}


def _has_consumable(attr: str) -> Callable[[ModelProfile], bool]:
    """Return a Model Profile consumable predicate.

    xtl reports every item inside one 17.32 JSON prop; device.status()
    unpacks it into the same *_life fields, so the per-type predicates map
    onto the capability shape (detergent is dreame-only, hence the set)."""
    return lambda p: (
        consumable_life_props(p.consumables).get(attr) is not None
        or (
            attr in _XTL_LIFE_SENSORS
            and isinstance(p.consumables, XtlConsumablesCapability)
        )
    )


def _has_xtl_prop(attr: str) -> Callable[[ModelProfile], bool]:
    """Gate on an XtlCoreCapability prop (entity-parity sensors are xtl-only)."""
    return lambda p: (
        isinstance(p.core, XtlCoreCapability)
        and getattr(p.core, attr, None) is not None
    )


def _error_attrs(code_fn: Callable[[VacuumStatus], int | None],
                 keys: dict) -> Callable[[VacuumStatus, str], dict | None]:
    """attrs_lang_fn builder for the ENUM error sensors: code + the plugin's
    fix-it text (EN/FR picked by language). None at 0 so the entity carries
    no stale attributes while healthy."""
    def attrs(status: VacuumStatus, lang: str) -> dict | None:
        code = code_fn(status)
        if not code:
            return None
        sol = xtl_error_solutions(lang).get(code, "")
        out = {"code": code}
        if sol:
            out["solution"] = sol
        return out
    return attrs


# Canonical sensor catalogue.  Clean area/time still have no populating
# coordinator support and stay omitted; consumable-life (2026-08-01) is now
# wired end to end (device.py polls it, gated the same way below).
_ALL_SENSORS: tuple[JonrSensorDescription, ...] = (
    JonrSensorDescription(
        key="status", translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=["cleaning", "paused", "idle", "returning", "docked", "error",
                 "localizing"],
        value_fn=lambda s: s.activity,
        # status is always populated (required prop, raises on failure)
    ),
    JonrSensorDescription(
        key="battery", translation_key="battery",
        device_class=SensorDeviceClass.BATTERY, native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT, value_fn=lambda s: s.battery,
        supported_fn=lambda p: p.core is not None and p.core.battery is not None,
    ),
    JonrSensorDescription(
        key="main_brush_life", translation_key="main_brush_life", icon="mdi:broom",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC, value_fn=lambda s: s.main_brush_life,
        supported_fn=_has_consumable("main_brush_life"),
    ),
    JonrSensorDescription(
        key="side_brush_life", translation_key="side_brush_life", icon="mdi:broom",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC, value_fn=lambda s: s.side_brush_life,
        supported_fn=_has_consumable("side_brush_life"),
    ),
    JonrSensorDescription(
        key="filter_life", translation_key="filter_life", icon="mdi:air-filter",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC, value_fn=lambda s: s.filter_life,
        supported_fn=_has_consumable("filter_life"),
    ),
    JonrSensorDescription(
        key="mop_life", translation_key="mop_life", icon="mdi:layers-triple-outline",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC, value_fn=lambda s: s.mop_life,
        supported_fn=_has_consumable("mop_life"),
    ),
    JonrSensorDescription(
        key="dust_bag_life", translation_key="dust_bag_life", icon="mdi:trash-can-outline",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC, value_fn=lambda s: s.dust_bag_life,
        supported_fn=_has_consumable("dust_bag_life"),
    ),
    JonrSensorDescription(
        key="detergent_life", translation_key="detergent_life", icon="mdi:cup-water",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC, value_fn=lambda s: s.detergent_life,
        supported_fn=_has_consumable("detergent_life"),
    ),
    # --- xtl station consumables (extras dict unpacked from the 17.32 JSON) ---
    JonrSensorDescription(
        key="mop_trough_life", translation_key="mop_trough_life", icon="mdi:pail",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: (s.extra_consumable_lives or {}).get("mop_trough_life"),
        supported_fn=lambda p: isinstance(p.consumables, XtlConsumablesCapability),
    ),
    JonrSensorDescription(
        key="unit_sensor_life", translation_key="unit_sensor_life", icon="mdi:leak",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: (s.extra_consumable_lives or {}).get("unit_sensor_life"),
        supported_fn=lambda p: isinstance(p.consumables, XtlConsumablesCapability),
    ),
    # --- xtl entity parity: session/lifetime stats, DND, records, station ----
    JonrSensorDescription(
        key="session_clean_time", translation_key="session_clean_time",
        icon="mdi:timer-outline", device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.clean_time,
        supported_fn=_has_xtl_prop("session_time"),
    ),
    JonrSensorDescription(
        key="session_clean_area", translation_key="session_clean_area",
        icon="mdi:floor-plan", device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.clean_area,
        supported_fn=_has_xtl_prop("session_area"),
    ),
    JonrSensorDescription(
        key="total_clean_time", translation_key="total_clean_time",
        icon="mdi:clock-fast", native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.total_clean_time,
        supported_fn=_has_xtl_prop("total_time"),
    ),
    JonrSensorDescription(
        key="total_clean_area", translation_key="total_clean_area",
        icon="mdi:vector-square", device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.total_clean_area,
        supported_fn=_has_xtl_prop("total_area"),
    ),
    JonrSensorDescription(
        key="total_clean_count", translation_key="total_clean_count",
        icon="mdi:counter", state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.total_clean_count,
        supported_fn=_has_xtl_prop("total_count"),
    ),
    # dnd_schedule removed 2026-09-25: the read-only label is superseded by the
    # editable text.dnd_time_range entity (which also writes 17.30).
    # cleaning_records removed 2026-09-25 (user call, "pas utile"): the P20
    # Pro never answers 17.46 (null on every local get, no push — live), so
    # the counter could only ever sit unknown. Mi Home shows its history from
    # the Xiaomi cloud, outside the MIoT spec. Spec plumbing stays (prop,
    # status field, cadence read + push mirror) per the erp precedent.
    JonrSensorDescription(
        key="clean_timers", translation_key="clean_timers",
        icon="mdi:calendar-clock",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: xtl_timers_summary(s.clean_timers_raw),
        rooms_value_fn=lambda s, rooms, maps, lang: xtl_timers_summary(
            s.clean_timers_raw, rooms, xtl_timer_labels(lang), maps),
        attrs_fn=lambda s: {"timers": xtl_parse_timers(s.clean_timers_raw)},
        supported_fn=_has_xtl_prop("clean_timers"),
    ),
    # --- error / message / station-status ENUM read-outs (17.35/17.79/17.36/
    # 17.49). Option ids are the stable snake_case keys from xtl.py; every
    # label (incl. the "none" = no-error state) resolves through
    # translations/*.json entity.sensor.<key>.state. The code itself and the
    # fix-it text (plugin errorCodes table, EN/FR by HA language) ride on the
    # attributes so the UI can show what to DO, like the Mi Home toast. ------
    JonrSensorDescription(
        key="robot_error", translation_key="robot_error",
        icon="mdi:alert-circle-outline", device_class=SensorDeviceClass.ENUM,
        options=["none"] + list(XTL_ERROR_CODE_KEYS.values()),
        value_fn=lambda s: (
            "none" if not s.fault
            else XTL_ERROR_CODE_KEYS.get(s.fault)),
        attrs_lang_fn=_error_attrs(lambda s: s.fault, XTL_ERROR_CODE_KEYS),
        supported_fn=lambda p: p.core is not None and p.core.fault is not None,
    ),
    JonrSensorDescription(
        key="station_error", translation_key="station_error",
        icon="mdi:robot-industrial", device_class=SensorDeviceClass.ENUM,
        options=["none"] + list(XTL_ERROR_CODE_KEYS.values()),
        value_fn=lambda s: (
            "none" if not s.station_error_raw
            else XTL_ERROR_CODE_KEYS.get(s.station_error_raw)),
        attrs_lang_fn=_error_attrs(lambda s: s.station_error_raw,
                                   XTL_ERROR_CODE_KEYS),
        supported_fn=_has_xtl_prop("station_error"),
    ),
    JonrSensorDescription(
        key="robot_message", translation_key="robot_message",
        icon="mdi:message-alert-outline", device_class=SensorDeviceClass.ENUM,
        options=["none"] + list(XTL_MESSAGE_CODE_KEYS.values()),
        value_fn=lambda s: (
            "none" if not s.message_raw
            else XTL_MESSAGE_CODE_KEYS.get(s.message_raw)),
        attrs_fn=lambda s: {"code": s.message_raw}
        if s.message_raw else None,
        supported_fn=_has_xtl_prop("message"),
    ),
    JonrSensorDescription(
        key="station_status", translation_key="station_status",
        icon="mdi:home-lightning-bolt", device_class=SensorDeviceClass.ENUM,
        options=list(XTL_STATION_STATUSES.values()),
        value_fn=lambda s: xtl_station_status_option(s.station_status_raw),
        supported_fn=_has_xtl_prop("robot_status"),
    ),
    JonrSensorDescription(
        key="clean_water_tank", translation_key="clean_water_tank",
        icon="mdi:water", device_class=SensorDeviceClass.ENUM,
        options=list(XTL_WATER_TANKS.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: XTL_WATER_TANKS.get(s.clean_water_cistern_raw),
        supported_fn=_has_xtl_prop("clean_water_cistern"),
    ),
    JonrSensorDescription(
        key="drain_water_tank", translation_key="drain_water_tank",
        icon="mdi:water-off-outline", device_class=SensorDeviceClass.ENUM,
        options=list(XTL_DRAIN_TANKS.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: XTL_DRAIN_TANKS.get(s.drain_cistern_raw),
        supported_fn=_has_xtl_prop("drain_cistern"),
    ),
    JonrSensorDescription(
        key="dust_bag_state", translation_key="dust_bag_state",
        icon="mdi:trash-can", device_class=SensorDeviceClass.ENUM,
        options=list(XTL_INSTALLED.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: XTL_INSTALLED.get(s.dust_bag_state_raw),
        supported_fn=_has_xtl_prop("dust_bag_state"),
    ),
    JonrSensorDescription(
        key="mop_tank_state", translation_key="mop_tank_state",
        icon="mdi:bucket-outline", device_class=SensorDeviceClass.ENUM,
        options=list(XTL_INSTALLED.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: XTL_INSTALLED.get(s.mop_tank_state_raw),
        supported_fn=_has_xtl_prop("mop_tank_state"),
    ),
)


def build_sensors(profile: ModelProfile) -> tuple[JonrSensorDescription, ...]:
    """Return only the sensor descriptions supported by *profile*.

    Each descriptor with a ``supported_fn`` is tested against the profile;
    descriptors without one are always included.
    """
    return tuple(
        d for d in _ALL_SENSORS
        if d.supported_fn is None or d.supported_fn(profile)
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: JonrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.control
    sensors = build_sensors(coordinator.device.profile)
    async_add_entities(JonrVacuumSensor(coordinator, entry, d) for d in sensors)


class JonrVacuumSensor(CoordinatorEntity[JonrVacuumCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: JonrSensorDescription

    def __init__(self, coordinator, entry, description: JonrSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    @property
    def native_value(self) -> int | str | None:
        rooms_fn = self.entity_description.rooms_value_fn
        if rooms_fn is not None:
            return rooms_fn(self.coordinator.data, self._room_names(),
                            self._map_names(), self._language())
        return self.entity_description.value_fn(self.coordinator.data)

    def _language(self) -> str:
        """The user's HA UI language (fr -> the French label tables)."""
        return getattr(self.hass.config, "language", "en") or "en"

    def _room_names(self) -> dict | None:
        """{mapId: {roomId: Mi Home name}} from the last rendered map.

        Reads the map result's XVMC 'rooms' attribute — the f3-JSON room
        table whose keys are exactly the ids the timers' cleanValues use
        (same source the clean_segment service and vacuum.py's room fills
        read). None when the entry has no map coordinator (local-only
        setup) or nothing is cached yet — the summary then shows room ids."""
        map_coord = getattr(self._entry.runtime_data, "map", None)
        result = getattr(map_coord, "data", None) if map_coord is not None else None
        if result is None:
            return None
        rooms = (getattr(result, "attributes", None) or {}).get("rooms") or {}
        bucket = {
            str(rid): str(r["name"])
            for rid, r in rooms.items()
            if isinstance(r, dict) and r.get("name")
        }
        return {str(result.map_id): bucket} if bucket else None

    def _map_names(self) -> dict | None:
        """{mapId: title} from the Active Map select's catalogue rows —
        lets the timer summary name the floor a room timer runs on
        (Mi Home's timer editor shows it; an untitled floor keeps its
        "Map N" placeholder, exactly what the select lists)."""
        map_coord = getattr(self._entry.runtime_data, "map", None)
        if map_coord is None:
            return None
        return {
            int(m["id"]): str(m.get("name") or f"Map {m['id']}")
            for m in (map_coord.map_list_meta or [])
            if isinstance(m, dict) and isinstance(m.get("id"), int)
        } or None

    @property
    def extra_state_attributes(self) -> dict | None:
        lang_fn = self.entity_description.attrs_lang_fn
        if lang_fn is not None:
            return lang_fn(self.coordinator.data, self._language())
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn is not None else None

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        ctype = _XTL_KEY_TO_CTYPE.get(self.entity_description.key)
        if ctype is None:
            return True
        # Empty consumable_types: non-xtl models never fill it, and an xtl
        # device may simply not have reported 17.32 yet — stay visible then.
        types = self.coordinator.data.consumable_types if self.coordinator.data else ()
        return not types or ctype in types
