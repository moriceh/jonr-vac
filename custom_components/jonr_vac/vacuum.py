"""Vacuum entity for JONR (xtl) vacuums."""
from __future__ import annotations

import asyncio
import logging
import time

import voluptuous as vol
from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import JonrConfigEntry
from .cloud.connector import XiaomiCloud
from .const import (
    CONF_DEVICE_ID,
    CONF_PASS_TOKEN,
    CONF_SERVER,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    CONF_USERNAME,
    DOMAIN,
)
from .coordinator import JonrVacuumCoordinator
from .device import (
    XtlVacuumDevice,
    fetch_voice_pack,
    voice_pack_confirmed,
)
from .spec.profiles.xtl import (
    XTL_ERROR_CODES,
    XTL_MESSAGE_CODES,
    XTL_ROOM_DEFAULTS,
    xtl_dnd_payload,
    xtl_preference_payloads,
    xtl_room_info_payload,
    xtl_sequence_payload,
)
from .spec.types import XtlMapCapability

# Serialise commands to the device (one MIoT write at a time).
PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)

_ACTIVITY = {
    "cleaning": VacuumActivity.CLEANING,
    "paused": VacuumActivity.PAUSED,
    "idle": VacuumActivity.IDLE,
    "returning": VacuumActivity.RETURNING,
    "docked": VacuumActivity.DOCKED,
    "error": VacuumActivity.ERROR,
    # xtl Relocation: the robot is out and working (it just doesn't know
    # where it is yet) — reads as cleaning, never as idle.
    "localizing": VacuumActivity.CLEANING,
}

_BASE_SUPPORT = (
    VacuumEntityFeature.START
    | VacuumEntityFeature.PAUSE
    | VacuumEntityFeature.STOP
    | VacuumEntityFeature.STATE
)

async def async_setup_entry(
    hass: HomeAssistant, entry: JonrConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.control
    async_add_entities([JonrVacuum(coordinator, entry)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "clean_segment",
        {
            vol.Required("segments"): vol.All(cv.ensure_list, [vol.Coerce(int)]),
            vol.Optional("repeats"): vol.All(vol.Coerce(int), vol.Range(min=1, max=2)),
        },
        "async_clean_segment",
    )
    platform.async_register_entity_service(
        "refresh_map",
        {vol.Required("confirm_movement"): vol.All(cv.boolean, vol.Equal(True))},
        "async_refresh_map",
    )
    platform.async_register_entity_service(
        "set_room_clean_mode",
        {
            vol.Required("room_id"): vol.Coerce(int),
            vol.Optional("work_mode"): vol.All(vol.Coerce(int), vol.Range(min=0, max=3)),
            vol.Optional("fan"): vol.All(vol.Coerce(int), vol.Range(min=0, max=3)),
            vol.Optional("water"): vol.All(vol.Coerce(int), vol.Range(min=0, max=2)),
            vol.Optional("passes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=2)),
            vol.Optional("route"): vol.All(vol.Coerce(int), vol.Range(min=0, max=2)),
        },
        "async_set_room_clean_mode",
    )
    platform.async_register_entity_service(
        "reset_room_clean_modes",
        {},
        "async_reset_room_clean_modes",
    )
    platform.async_register_entity_service(
        "set_clean_sequence",
        {vol.Required("rooms"): vol.All(cv.ensure_list, [vol.Coerce(int)])},
        "async_set_clean_sequence",
    )
    platform.async_register_entity_service(
        "clean_zone",
        {
            vol.Required("zones"): vol.All(cv.ensure_list, [vol.All(
                cv.ensure_list, [vol.Coerce(float)], vol.Length(min=4, max=4))]),
            vol.Optional("repeats"): vol.All(vol.Coerce(int), vol.Range(min=1, max=2)),
        },
        "async_clean_zone",
    )
    platform.async_register_entity_service(
        "set_room_info",
        {
            vol.Required("room_id"): vol.Coerce(int),
            vol.Optional("name"): cv.string,
            vol.Optional("category"): vol.All(vol.Coerce(int), vol.Range(min=0, max=17)),
        },
        "async_set_room_info",
    )
    platform.async_register_entity_service(
        "set_dnd_time",
        {
            vol.Required("start_time"): cv.string,
            vol.Required("end_time"): cv.string,
            vol.Optional("no_dust", default=False): cv.boolean,
            vol.Optional("no_dry", default=False): cv.boolean,
        },
        "async_set_dnd_time",
    )
    platform.async_register_entity_service(
        "remote_control",
        {vol.Required("command"): vol.All(vol.Coerce(int), vol.Range(min=0, max=4))},
        "async_remote_control",
    )
    platform.async_register_entity_service(
        "install_voice_pack",
        {
            vol.Required("url"): cv.string,
            vol.Required("md5"): cv.string,
            vol.Required("lang"): vol.All(vol.Coerce(int), vol.Range(min=1, max=11)),
            vol.Optional("version"): cv.string,
        },
        "async_install_voice_pack",
    )


class JonrVacuum(CoordinatorEntity[JonrVacuumCoordinator], StateVacuumEntity):
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: JonrVacuumCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device = coordinator.device
        core = self._device.core
        support = _BASE_SUPPORT
        if core.charge is not None:
            support |= VacuumEntityFeature.RETURN_HOME
        if core.locate is not None or core.alarm is not None:
            support |= VacuumEntityFeature.LOCATE
        self._attr_supported_features = support
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_vacuum"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, base)},
            manufacturer="JONR",
            model=self._device.model,
            name=entry.title,
        )

    @property
    def activity(self) -> VacuumActivity:
        return _ACTIVITY.get(self.coordinator.data.activity, VacuumActivity.IDLE)

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {"fault": self.coordinator.data.fault, "model": self._device.model}
        if self._device.profile.brand == "xtl":
            d = self.coordinator.data
            attrs.update({
                "fault_text": XTL_ERROR_CODES.get(d.fault or 0, ""),
                "message": d.message_raw,
                "message_text": XTL_MESSAGE_CODES.get(d.message_raw or 0, ""),
                "station_error": d.station_error_raw,
                "station_error_text": XTL_ERROR_CODES.get(d.station_error_raw or 0, ""),
            })
        return attrs

    async def async_start(self) -> None:
        # On models whose pause keeps the task open (xtl) start doubles as
        # resume: pass the current activity down. Others ignore the arg.
        await self.hass.async_add_executor_job(
            self._device.start, self.coordinator.data.activity
        )
        self.coordinator.async_schedule_confirm()

    async def async_stop(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self._device.stop)
        self.coordinator.async_schedule_confirm()

    async def async_return_to_base(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self._device.return_home)
        self.coordinator.async_schedule_confirm()

    async def async_refresh_map(self, confirm_movement: bool) -> None:
        data = self._entry.runtime_data
        if data.map is None:
            raise HomeAssistantError("Refresh map requires a cloud map session")
        await data.map.async_refresh_map_with_movement(
            confirm_movement=confirm_movement,
            use_mqtt=data.mqtt is not None,
        )

    async def async_pause(self) -> None:
        await self.hass.async_add_executor_job(self._device.pause)
        self.coordinator.async_schedule_confirm()

    async def async_locate(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self._device.locate)

    def _apply_repeats(self, repeats: int | None) -> None:
        """×N popup button → global cleaning passes (set-clean-count 17.28),
        written just before the typed task start — the same channel the
        count select entity uses. No-op off xtl (no pass-count concept
        exposed there)."""
        if repeats is None:
            return
        counts = self._device.core.counts
        preset = next((k for k, v in counts.items() if v == repeats), None)
        if preset is not None:
            self._device.set_count(preset)

    async def async_clean_segment(self, segments: list[int],
                                  repeats: int | None = None) -> None:
        """Clean one or more rooms by their map room id (tap-to-clean)."""
        data = self._entry.data
        await self.hass.async_add_executor_job(self._apply_repeats, repeats)
        # xtl room-clean = stop + settle + typed start-clean; a single cloud
        # action cannot sequence that (and local control is reliable here),
        # so room-clean is always served locally.
        try:
            await self.hass.async_add_executor_job(self._device.clean_segments, segments)
            _LOGGER.debug("%s: room-clean served via local", self._device.model)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Room cleaning failed: {err}") from err
        self.coordinator.async_schedule_confirm()

    # --- xtl room personalisation (17.53 / 17.15 / typed zone start) ---------
    def _xtl_map_context(self) -> tuple[int, list[dict]]:
        """(active map id, cached rooms). The 17.53/17.15 payloads must name
        the map the settings belong to, and the cached rooms drive the
        merge/reset fills — both ride on the last rendered map."""
        if self._device.profile.brand != "xtl":
            raise HomeAssistantError(
                "Room personalisation is only supported on xtl vacuums")
        map_coord = self._entry.runtime_data.map
        if map_coord is None or getattr(map_coord, "data", None) is None:
            raise HomeAssistantError(
                "No map cached yet — wait for the map camera to render once")
        return int(map_coord.data.map_id), list(
            map_coord.data.attributes.get("rooms") or [])

    def _schedule_map_refresh(self) -> None:
        """Re-pull the map after a write: the plugin reloads it the same way
        (prefer/order and area names come back inside the map JSON)."""
        map_coord = self._entry.runtime_data.map
        if map_coord is not None:
            self.hass.async_create_task(map_coord.async_request_refresh())

    async def _xtl_write(self, send, payloads: list[str], action) -> None:
        """Send built payloads to an xtl write action, locally first. On any
        local failure retry them one by one through the cloud session —
        17.53/17.15 are the plugin's cloud-side channels and local
        writability is unverified on this firmware."""
        try:
            await self.hass.async_add_executor_job(send, payloads)
            return
        except Exception as local_err:  # noqa: BLE001
            data = self._entry.data
            if not _has_cloud_session(data):
                raise HomeAssistantError(f"Write failed: {local_err}") from local_err
            _LOGGER.debug("%s: local write failed (%s), retrying via cloud",
                          self._device.model, local_err)
            local_text = str(local_err)
        for payload in payloads:
            try:
                await self.hass.async_add_executor_job(
                    _cloud_do_action, data, action, [payload])
            except Exception as cloud_err:  # noqa: BLE001
                raise HomeAssistantError(
                    f"Write failed locally ({local_text}) and via cloud ({cloud_err})"
                ) from cloud_err

    async def async_set_room_clean_mode(
        self, room_id: int, work_mode: int | None = None, fan: int | None = None,
        water: int | None = None, passes: int | None = None,
        route: int | None = None,
    ) -> None:
        """One room's entry in the plugin's CustomizedParameters screen.
        Fields left out keep the room's saved preference, then the robot's
        current global value, then the firmware default — like the screen's
        pre-filled sliders. The robot only applies per-room values while the
        work mode is Custom, so this call switches it when needed."""
        map_id, rooms = self._xtl_map_context()
        saved = next((r.get("prefer") or {} for r in rooms
                      if r.get("room_id") == room_id), {})
        d = self.coordinator.data
        pref = {
            "R": int(room_id),
            "K": _or(work_mode, _or(saved.get("mode"), _or(d.mode_raw, 0))),
            "F": _or(fan, _or(saved.get("wind"), _or(d.fan_speed_raw, 1))),
            "W": _or(water, _or(saved.get("water"), _or(d.water_level_raw, 1))),
            "C": _or(passes, _or(saved.get("count"), _or(d.count_raw, 1))),
            "D": _or(route, _or(saved.get("drag"), _or(d.fine_drag_raw, 1))),
        }
        await self._xtl_write(
            self._device.xtl_send_customization,
            xtl_preference_payloads(map_id, [pref]),
            self._device.core.customization_rooms,
        )
        if d.mode_raw != 4:   # 4 = Custom: the mode per-room values apply in
            await self.hass.async_add_executor_job(self._device.set_mode, "custom")
            self.coordinator.async_apply_raw("mode_raw", 4)
        self.coordinator.async_schedule_confirm()
        self._schedule_map_refresh()

    async def async_reset_room_clean_modes(self) -> None:
        """The plugin's "default" push: firmware defaults (Both / auto fan /
        mid water / one pass / daily route) for every cached room. Work mode
        is left alone, mirroring the screen."""
        map_id, rooms = self._xtl_map_context()
        prefs = [{"R": r["room_id"], **XTL_ROOM_DEFAULTS}
                 for r in rooms if r.get("room_id") is not None]
        if not prefs:
            raise HomeAssistantError("The cached map lists no rooms to reset")
        await self._xtl_write(
            self._device.xtl_send_customization,
            xtl_preference_payloads(map_id, prefs),
            self._device.core.customization_rooms,
        )
        self._schedule_map_refresh()

    async def async_set_clean_sequence(self, rooms: list[int]) -> None:
        """The plugin's CustomizedOrder save (type-2 channel): the ordered
        room ids are the whole-home execution order, [] clears the sequence
        and the robot falls back to its own routing."""
        map_id, _ = self._xtl_map_context()
        await self._xtl_write(
            self._device.xtl_send_customization,
            [xtl_sequence_payload(map_id, rooms)],
            self._device.core.customization_rooms,
        )
        self._schedule_map_refresh()

    async def async_clean_zone(self, zones: list[list[float]],
                               repeats: int | None = None) -> None:
        """Clean rectangles [x_min, y_min, x_max, y_max] in map scene units
        (5 cm grid — the same frame as the room centres). Local only: xtl
        needs stop+settle before the typed start-clean, which a single cloud
        action cannot sequence (same reasoning as async_clean_segment)."""
        if self._device.profile.brand != "xtl":
            raise HomeAssistantError(
                "Zone cleaning is only supported on xtl vacuums")
        await self.hass.async_add_executor_job(self._apply_repeats, repeats)
        try:
            await self.hass.async_add_executor_job(
                self._device.start_zone_clean, zones)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Zone cleaning failed: {err}") from err
        self.coordinator.async_schedule_confirm()

    async def async_set_room_info(
        self, room_id: int, name: str | None = None, category: int | None = None,
    ) -> None:
        """Rename and/or re-type a room (the plugin's room editor, 17.15).
        The action always carries name AND category, so the untouched field
        is filled from the cached map."""
        map_id, rooms = self._xtl_map_context()
        if name is None and category is None:
            raise HomeAssistantError("Pass at least one of name/category")
        cur = next((r for r in rooms if r.get("room_id") == room_id), None)
        if cur is None:
            raise HomeAssistantError(f"Room {room_id} is not in the cached map")
        if name is None:
            name = str(cur.get("name") or "")
        if category is None:
            try:
                category = int(cur.get("type"))
            except (TypeError, ValueError):
                category = 0
        if len(name.encode("utf-8")) > 32:
            raise HomeAssistantError("Room names are limited to 32 bytes")
        await self._xtl_write(
            self._device.xtl_send_room_info,
            [xtl_room_info_payload(map_id, [{
                "roomId": int(room_id), "name": name,
                "category": int(category)}])],
            self._device.core.edite_area_info,
        )
        self._schedule_map_refresh()

    async def async_set_dnd_time(
        self, start_time: str, end_time: str,
        no_dust: bool = False, no_dry: bool = False,
    ) -> None:
        """Program the Do-Not-Disturb window (17.30), the exact JSON string
        the plugin's DND screen sends. This only stores the schedule — the
        `do_not_disturb` switch turns the window on. no_dust/no_dry skip the
        dock's dust-collection / mop-drying during DND (firmware >= 439.681)."""
        if self._device.profile.brand != "xtl":
            raise HomeAssistantError(
                "Do Not Disturb scheduling is only supported on xtl vacuums")
        try:
            sh, sm = _parse_hhmm(start_time)
            eh, em = _parse_hhmm(end_time)
        except ValueError as err:
            raise HomeAssistantError(
                f"start_time/end_time must be HH:MM (got {err})") from err
        await self._xtl_write(
            self._device.xtl_send_dnd_time,
            [xtl_dnd_payload(sh, sm, eh, em, no_dust, no_dry)],
            self._device.core.disturb_time_action,
        )
        # disturb_time (17.19) lives in the extended JSON group — a core-only
        # confirm would never re-read it, so this write asks for a full poll.
        self.coordinator.async_schedule_confirm(full=True)

    async def async_remote_control(self, command: int) -> None:
        """Manual drive (17.49 control-order): 0 stop, 1 forward, 2 turn
        left, 3 turn right, 4 backward. The robot keeps the last command —
        movement persists until command 0, like holding the plugin's D-pad;
        send 0 when done."""
        if self._device.profile.brand != "xtl":
            raise HomeAssistantError(
                "Manual control is only supported on xtl vacuums")
        try:
            await self.hass.async_add_executor_job(
                self._device.xtl_manual_control, int(command))
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Manual control failed: {err}") from err
        self.coordinator.async_schedule_confirm()

    async def async_install_voice_pack(
        self, url: str, md5: str, lang: int, version: str | None = None
    ) -> None:
        """Install a voice pack through the official store's channel
        (17.21 piid 61 as a JSON descriptor): the robot downloads the pack
        from the url itself, so the url must be reachable from the robot's
        network — a LAN share on a PC works.

        The call ANSWERS: first the pack itself is fetched and proven from
        HA (md5 of the served bytes, plain tar, audio.conf parses with the
        right Id — device.fetch_voice_pack), then after the store write we
        wait for the robot to re-report its (17.25 language, 17.78 pack
        version) pair — exactly what the plugin's VoiceList reacts on,
        down to its 120 s give-up. Any failure raises HomeAssistantError
        naming what was seen; a clean return means the robot confirmed."""
        if self._device.profile.brand != "xtl":
            raise HomeAssistantError(
                "Voice-pack install is only supported on xtl vacuums")
        code = int(lang)
        expected = None if version in (None, "") else str(version).strip()
        try:
            fetched = await self.hass.async_add_executor_job(
                fetch_voice_pack, url, md5, code)
        except ValueError as err:
            raise HomeAssistantError(f"Voice-pack check failed: {err}") from err
        if fetched is not None:
            expected = fetched  # the pack's own audio.conf wins
        pre = await self.hass.async_add_executor_job(
            self._device.read_voice_state)
        if expected is not None and pre == (code, expected):
            raise HomeAssistantError(
                f"the robot already reports pack version {expected} for "
                f"language {code} — this install could never be told apart "
                "from doing nothing: give the pack a unique Ver in its "
                "audio.conf (or pass a different version)")
        try:
            await self.hass.async_add_executor_job(
                self._device.install_voice_pack, url, md5, code)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Voice-pack install failed: {err}") from err
        cur = pre
        deadline = time.monotonic() + 120.0   # the plugin's own give-up
        while time.monotonic() < deadline:
            await asyncio.sleep(4.0)
            cur = await self.hass.async_add_executor_job(
                self._device.read_voice_state)
            if voice_pack_confirmed(pre, cur, code, expected):
                self.coordinator.async_schedule_confirm(full=True)
                return
        self.coordinator.async_schedule_confirm(full=True)
        raise HomeAssistantError(
            f"voice-pack install not confirmed within 120 s — the robot "
            f"reports language={cur[0]}, pack version {cur[1]!r} (wanted "
            f"{code}/{expected!r}); the url must be reachable from the "
            "robot's network, and the robot silently rejects a pack whose "
            "md5 or format is off")


def _or(value, fallback):
    """Coalesce that keeps 0 — the plugin's `value ?? fallback` merge."""
    return fallback if value is None else value


def _parse_hhmm(text) -> tuple[int, int]:
    """'HH:MM' (or 'HH:MM:SS' — the HA time selector's format) ->
    (hour, minute), seconds dropped; raises ValueError on anything else."""
    parts = str(text).strip().split(":")
    if not 2 <= len(parts) <= 3:
        raise ValueError(text)
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(text)
    return hour, minute


def _has_cloud_session(data: dict) -> bool:
    return all(
        data.get(k)
        for k in (
            CONF_USERNAME, CONF_USER_ID, CONF_SSECURITY,
            CONF_SERVICE_TOKEN, CONF_SERVER, CONF_DEVICE_ID,
        )
    )


def _cloud_action_ok(response: object) -> bool:
    if not isinstance(response, dict):
        return False
    if response.get("code", 0) != 0:
        return False
    result = response.get("result")
    if isinstance(result, dict) and result.get("code", 0) != 0:
        return False
    if isinstance(result, list):
        return all(not isinstance(item, dict) or item.get("code", 0) == 0 for item in result)
    return True


def _cloud_do_action(data: dict, action, params: list) -> None:
    """Run one action through the cloud session — the fallback channel for
    the personalisation writes (17.53/17.15), which the plugin itself drives
    cloud-side. Tries raw positional in-params (the doSpecAction form), then
    the piid-wrapped form the local bridge needs."""
    if not _has_cloud_session(data):
        raise ValueError("This write requires a Xiaomi cloud session")
    cloud = XiaomiCloud(str(data[CONF_USERNAME]))
    cloud.restore_session(
        data[CONF_USER_ID],
        data[CONF_SSECURITY],
        data[CONF_SERVICE_TOKEN],
        data.get(CONF_PASS_TOKEN),
    )
    attempts = [params]
    if action.in_piid is not None:
        attempts.append(
            [{"did": "-", "piid": action.in_piid, "value": v} for v in params])
    last: object = None
    for candidate in attempts:
        last = cloud.cloud_action(
            str(data[CONF_SERVER]), str(data[CONF_DEVICE_ID]),
            action.siid, action.aiid, candidate,
        )
        if _cloud_action_ok(last):
            return
    raise ValueError(f"Xiaomi cloud rejected the action: {last}")


def _cloud_set_current_map(data: dict, device: XtlVacuumDevice, map_id: int) -> None:
    # Reproduce the official plugin's switchMap exactly: actions.switchMap ->
    # doSpecAction(actionCodes["switch-map"], [mapId]) posts the RAW
    # positional in-param to the cloud /miotspec/action channel (decompiled
    # main.bundle:1586213 switchMap + :1566416 doSpecAction — `in: ins || []`,
    # the mapId bare, no piid wrapper). The piid-tagged dict form (what this
    # used to send, mirroring the LOCAL bridge's _in_params quirk) is NOT what
    # the app sends cloud-side — and on the xm2216 the dict form silently no-
    # ops: the select reported success while the robot kept its old floor
    # (live 2026-09-26, "switching does nothing"). Try the plugin's raw
    # positional form first, then the dict as a fallback (parity with
    # _cloud_do_action's two attempts), so either firmware revision switches.
    cap = device.profile.map
    if not isinstance(cap, XtlMapCapability):
        raise ValueError(f"{device.model} has no cloud-mappable map capability")
    action = cap.set_current_map
    if action is None:
        raise ValueError(f"{device.model} has no set-current-map action")
    attempts = [[int(map_id)]]
    if action.in_piid is not None:
        attempts.append([{"piid": action.in_piid, "value": int(map_id)}])
    cloud = XiaomiCloud(str(data[CONF_USERNAME]))
    cloud.restore_session(
        data[CONF_USER_ID],
        data[CONF_SSECURITY],
        data[CONF_SERVICE_TOKEN],
        data.get(CONF_PASS_TOKEN),
    )
    last: object = None
    for in_params in attempts:
        last = cloud.cloud_action(
            str(data[CONF_SERVER]),
            str(data[CONF_DEVICE_ID]),
            action.siid,
            action.aiid,
            in_params,
        )
        if _cloud_action_ok(last):
            return
    raise ValueError(f"Xiaomi cloud rejected map-switch: {last}")
