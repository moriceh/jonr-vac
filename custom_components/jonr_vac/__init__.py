"""The JONR Vacuum integration (local MIoT control + cloud map)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from .cloud.mqtt import MiotMqttClient, MqttMessage
from .cloud.oauth import async_refresh_oauth_entry
from .const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_MODEL,
    CONF_OAUTH_ACCESS_TOKEN,
    CONF_OAUTH_DEVICE_ID,
    CONF_OAUTH_REGION,
    CONF_PASSWORD,
    CONF_SERVICE_TOKEN,
    CONF_TOKEN,
    DOMAIN,
)
from .coordinator import JonrVacuumCoordinator
from .device import XtlVacuumDevice
from .error_events import JonrErrorNotifier
from .map_coordinator import JonrMapCoordinator
from .spec.profiles.xtl import xtl_dnd_from_push

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.VACUUM,
    Platform.CAMERA,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.BUTTON,
    Platform.TIME,
]


@dataclass
class JonrVacuumData:
    """Runtime data stored on the config entry (see runtime-data rule)."""

    control: JonrVacuumCoordinator
    map: JonrMapCoordinator | None
    mqtt: MiotMqttClient | None = None
    notifier: JonrErrorNotifier | None = None


type JonrConfigEntry = ConfigEntry[JonrVacuumData]


_CARD_BASE = "/jonr-vac-card"
_CARD_URL = f"{_CARD_BASE}/xiaomi-vac-card.js"

# Profile field names folded from cloud MQTT pushes (see
# coordinator._PUSH_RAW_ATTRS, which must carry an entry for every name in
# these tuples — harness tests enforce both directions).
_WATCH_GEARS = ("fan_speed", "water_level", "mode", "count", "route", "erp")
_WATCH_EXT_SETTINGS = (
    "volume", "message", "station_error", "clean_type_status",
    "child_lock", "disturb", "break_point", "carpet_boost",
    "auto_drying", "auto_solution", "carpet_twice", "carpet_first",
    "mop_augment", "carpet_prefer", "dust_collection", "drying_time",
    "mop_wash_freq", "mop_wash_temp", "voice_language",
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve and auto-register the Lovelace card (no manual resource setup)."""
    hass.data.setdefault(DOMAIN, {})

    from .http_api import VectorMapView

    hass.http.register_view(VectorMapView())

    www = os.path.join(os.path.dirname(__file__), "www")
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(_CARD_BASE, www, False)]
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("jonr_vac: failed to serve card files (%s)", err)
        return True

    # Register the card as a Lovelace RESOURCE (proven reliable), not via
    # add_extra_js_url (which intermittently fails to load the module). Done once
    # HA has started so the Lovelace resource collection exists.
    from homeassistant.helpers import start as ha_start

    async def _register_card_resource(_event=None) -> None:
        from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE

        ll = hass.data.get(LOVELACE_DATA)
        if ll is None:
            _LOGGER.warning("jonr_vac: Lovelace not ready; add %s manually", _CARD_URL)
            return
        if ll.resource_mode != MODE_STORAGE:
            _LOGGER.warning(
                "jonr_vac: Lovelace is in YAML mode; add this resource yourself:"
                " url: %s, type: module", _CARD_URL)
            return

        # Cache-bust by file mtime so card updates are fetched fresh (HACS does
        # the same with ?hacstag=). Keep a single resource, update it in place.
        try:
            ver = int(os.path.getmtime(os.path.join(www, "xiaomi-vac-card.js")))
        except OSError:
            ver = 0
        url = f"{_CARD_URL}?v={ver}"

        resources = ll.resources
        await resources.async_get_info()  # ensure loaded
        existing = [r for r in resources.async_items()
                    if r.get("url", "").split("?")[0] == _CARD_URL]
        if existing:
            if existing[0].get("url") != url:
                await resources.async_update_item(existing[0]["id"], {"url": url})
                _LOGGER.info("jonr_vac: updated card resource -> %s", url)
        else:
            await resources.async_create_item({"res_type": "module", "url": url})
            _LOGGER.info("jonr_vac: registered card resource -> %s", url)

    ha_start.async_at_started(hass, _register_card_resource)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: JonrConfigEntry) -> bool:
    """Set up JONR Vacuum from a config entry."""
    data = entry.data

    device = await hass.async_add_executor_job(
        XtlVacuumDevice, data[CONF_HOST], data[CONF_TOKEN], data[CONF_MODEL]
    )
    control = JonrVacuumCoordinator(hass, entry, device)
    await control.async_config_entry_first_refresh()

    map_coordinator: JonrMapCoordinator | None = None
    # Map is optional: only if a cloud session was captured at setup time.
    if data.get(CONF_SERVICE_TOKEN):
        map_coordinator = JonrMapCoordinator(hass, entry, device, control)
        # don't fail the whole entry if the first map fetch hiccups
        await map_coordinator.async_refresh()

    mqtt_client = await _async_start_mqtt(hass, entry, map_coordinator, control)
    _async_manage_oauth_issue(hass, entry, mqtt_active=mqtt_client is not None)

    notifier = JonrErrorNotifier(hass, entry, control)
    entry.runtime_data = JonrVacuumData(
        control=control, map=map_coordinator, mqtt=mqtt_client,
        notifier=notifier,
    )
    # After the platforms (the vacuum entity exists -> the bus payload's
    # entity_id resolves) and after runtime_data (the notifier reads
    # entry.options live); unlisten() runs first on unload, before the
    # coordinator teardown.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Warm the translation label tables off the event loop (the first _fire
    # would otherwise open() the translations files from the loop; a warm
    # cache makes every later _labels() call disk-free).
    await hass.async_add_executor_job(notifier.preload)
    notifier.listen()
    return True


def _async_manage_oauth_issue(
    hass: HomeAssistant, entry: JonrConfigEntry, *, mqtt_active: bool
) -> None:
    """Raise a Repairs nudge when a map-capable cloud entry has no OAuth yet.

    Map-capable = a cloud session was captured (service token). Without OAuth the
    live map stays on slow poll+cache, so we surface a fixable Repairs issue that
    walks the user through linking OAuth. Cleared once OAuth is active.
    """
    from .repairs import oauth_missing_issue_id

    issue_id = oauth_missing_issue_id(entry.entry_id)
    map_capable = bool(entry.data.get(CONF_SERVICE_TOKEN))
    if map_capable and not mqtt_active:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="oauth_missing",
            data={"entry_id": entry.entry_id},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


async def async_unload_entry(hass: HomeAssistant, entry: JonrConfigEntry) -> bool:
    """Unload a config entry."""
    # runtime_data is only assigned at the end of setup; guard never-loaded entries
    data = getattr(entry, "runtime_data", None)
    if data is not None:
        if data.notifier is not None:
            data.notifier.unlisten()
        if data.mqtt is not None:
            await data.mqtt.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_start_mqtt(
    hass: HomeAssistant,
    entry: JonrConfigEntry,
    map_coord: JonrMapCoordinator | None,
    control_coord: JonrVacuumCoordinator,
) -> MiotMqttClient | None:
    """Bring up the MIoT cloud MQTT client for this entry when OAuth is set.

    Additive & optional per the plan: any missing OAuth field → no client, no
    error, existing behaviour intact.
    """
    await _async_refresh_oauth_for_mqtt(hass, entry, force=False)
    data = entry.data
    required = (
        data.get(CONF_OAUTH_ACCESS_TOKEN),
        data.get(CONF_OAUTH_REGION),
        data.get(CONF_OAUTH_DEVICE_ID),
        data.get(CONF_DEVICE_ID),
    )
    if not all(required):
        _LOGGER.debug(
            "MIoT MQTT not started: OAuth not configured on this entry "
            "(access_token/region/oauth_device_id/device_id present=%s)",
            [bool(v) for v in required],
        )
        return None

    async def _token_provider(force: bool) -> str:
        await _async_refresh_oauth_for_mqtt(hass, entry, force=force)
        return str(entry.data.get(CONF_OAUTH_ACCESS_TOKEN, ""))

    # Which pushed properties mean "the cached snapshot is stale": the
    # profile's live props (status/fault/battery + the gear read-backs). The
    # old hook watched a hardcoded siid 2/piid 1, which never matched xtl
    # (its status is siid 2/piid 2), so control state only ever moved on the
    # 10 s UDP poll. Derive the watch set from the profile instead.
    core = control_coord.device.core
    optimistic: dict[tuple[int, int], str] = {}
    watch: set[tuple[int, int]] = set()
    for field in ("status", "fault", "battery"):
        prop = getattr(core, field, None)
        if prop is not None:
            optimistic[(prop.siid, prop.piid)] = field
            watch.add((prop.siid, prop.piid))
    # Gear read-backs and the ext long tail. The robot uploads every one of
    # these to the cloud within seconds of an app-side change (live probe
    # 2026-09-25: 17/26, 17/27, 17/34, 17/49, 17/64, 17/9, 17/36 all pushed),
    # so each folds straight into the snapshot via coordinator._PUSH_RAW_ATTRS
    # — no reason to wait for the (possibly slow) UDP poll. Fields a brand
    # does not declare simply drop out of getattr.
    for field in (*_WATCH_GEARS, *_WATCH_EXT_SETTINGS):
        prop = getattr(core, field, None)
        if prop is not None:
            watch.add((prop.siid, prop.piid))
            # A setting changed from the Mi Home app flips the HA entity
            # instantly; the debounced poll that follows only reconciles.
            optimistic[(prop.siid, prop.piid)] = field

    # The DND schedule is the one JSON prop on the watch list: its LOCAL UDP
    # read serves the pre-write snapshot after a local 17.30 write (live
    # 2026-09-25), so the cloud push is its truth source — folded straight
    # into the snapshot and the device caches below, int-fold never applies.
    dnd_prop = getattr(core, "disturb_time", None)
    dnd_key = (dnd_prop.siid, dnd_prop.piid) if dnd_prop is not None else None
    if dnd_key is not None:
        watch.add(dnd_key)

    # Schedules & history (17.17 / 17.46): the robot pushes the full JSON
    # lists to the cloud on every app-side edit, but these props used to be
    # dropped at the gate — the only path to the sensors was the cadence
    # solo-read (every 6 full turns), so a Mi Home timer change showed up
    # minutes late (live 2026-09-25). Fold validated pushes into the
    # device's slow-read cache AND the snapshot now (the dnd pattern); the
    # debounced poll below reconciles, cadence reads stay the fallback.
    json_watch: dict[tuple[int, int], tuple[str, str]] = {}
    for field, cache_key, attr in (
            ("clean_timers", "timers", "clean_timers_raw"),
            ("clean_records", "records", "clean_records_raw")):
        prop = getattr(core, field, None)
        if prop is not None:
            watch.add((prop.siid, prop.piid))
            json_watch[(prop.siid, prop.piid)] = (cache_key, attr)

    # The two JSON props riding the ext group instead of the cadence reads:
    # clean_values (17.34, task targets) and the 17.32 consumables list.
    # They never go through the int-fold (int() on JSON would drop them) —
    # validated list pushes mirror into _ext_cache instead, and the core-only
    # confirm poll replays ext from there while rebuilding the status
    # (device.status parses consumables every poll), so the sensors flip in
    # ~1 s without waiting on the full poll.
    json_ext: set[tuple[int, int]] = set()
    for prop in (getattr(core, "clean_values", None),
                 getattr(getattr(control_coord.device.profile, "consumables",
                                 None), "consumables", None)):
        if prop is not None:
            watch.add((prop.siid, prop.piid))
            json_ext.add((prop.siid, prop.piid))

    # language-ver (17.78): the installed voice pack's version. String
    # valued, so it stays OUT of the int-fold table (int("2") would fold
    # fine but snapshots hold str via _as_str — mixing spellings breaks the
    # fold's equality guard). The voice-pack install confirm loop reads it
    # fresh instead; mirroring the push into _ext_cache lets that loop see
    # the robot's report between its own polls.
    ver_prop = getattr(core, "language_ver", None)
    ver_key = (ver_prop.siid, ver_prop.piid) if ver_prop is not None else None
    if ver_key is not None:
        watch.add(ver_key)

    # robot-status (17.49): the plugin's curRobotStatus string — WashMop /
    # HotDry / WindDry / ClctDust / Charging / ... It is the ONLY readable
    # view of a running dock cycle (wash/dry/empty are write-only bools), so
    # the station switches live off this push. String-valued: stays out of
    # the int-fold (int("WashMop") drops everything); folded through the
    # coordinator's string branch AND mirrored into the ext replay cache
    # (same two-step as ver_key, plus the snapshot fold the switches need).
    rstat_prop = getattr(core, "robot_status", None)
    rstat_key = (rstat_prop.siid, rstat_prop.piid) if rstat_prop is not None else None
    if rstat_key is not None:
        watch.add(rstat_key)

    pending: dict[str, asyncio.Task | None] = {"task": None}

    async def _debounced_poll() -> None:
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            return
        pending["task"] = None
        # The pushed fields are already folded (async_apply_push); this is
        # the reconciliation echo only — core group, never the slow extended
        # chunks (a push burst must never queue behind a 45-75 s silent
        # chunk). The periodic timer poll stays full.
        control_coord.async_schedule_confirm()

    def _kick_poll() -> None:
        # Collapse a burst (one task start pushes status+fan+water+mode) into
        # a single poll; each push also lands optimistically, so the poll is
        # confirmation, not the source of the visible flip.
        if pending["task"] is not None:
            pending["task"].cancel()
        pending["task"] = hass.async_create_task(_debounced_poll())

    def _as_push_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _as_push_list(value: Any) -> list | None:
        """Push payload -> list (the broker decodes properties_changed
        frames, so a real list is the norm; a JSON string is tolerated);
        None for anything else, so a scalar/garbled push never poisons the
        replay caches — the poll-side reads keep the truth."""
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                return None
            if isinstance(parsed, list):
                return parsed
        return None

    async def _on_message(message: MqttMessage) -> None:
        if map_coord is not None:
            await map_coord.async_on_mqtt_message(message)
        if message.kind != "property" or message.siid is None or message.piid is None:
            return
        key = (message.siid, message.piid)
        if key not in watch:
            return
        if dnd_key is not None and key == dnd_key:
            # The 17.19 schedule's local read serves stale after writes —
            # the cloud push IS the truth: normalise it, install it in the
            # device caches (xtl_note_dnd) and the snapshot, skip int-fold.
            payload = xtl_dnd_from_push(message.value)
            if payload is not None:
                control_coord.device.xtl_note_dnd(payload)
                control_coord.async_apply_raw("disturb_time_raw", payload)
            _kick_poll()
            return
        json_slot = json_watch.get(key)
        if json_slot is not None:
            # xtl_note_json_push re-validates and returns what it installed
            # (None drops the push without clobbering the cache); the same
            # validated list folds into the snapshot for the instant sensor
            # flip — the core-only confirm poll replaying _slow_read_cache
            # would otherwise still take the debounce + poll round-trip.
            fresh = control_coord.device.xtl_note_json_push(
                json_slot[0], message.value)
            if fresh is not None:
                control_coord.async_apply_raw(json_slot[1], fresh)
            _kick_poll()
            return
        if key in json_ext:
            parsed = _as_push_list(message.value)
            if parsed is not None:
                control_coord.device.apply_ext_push(key[0], key[1], parsed)
                _kick_poll()
            return
        if ver_key is not None and key == ver_key:
            # 17.78 string push: mirror the text into the ext replay cache
            # (see the ver_key comment) — no int-fold, no snapshot rewrite;
            # the install-confirm loop picks it up on its next fresh read.
            text = str(message.value).strip() if message.value is not None else ""
            if text:
                control_coord.device.apply_ext_push(key[0], key[1], text)
                _kick_poll()
            return
        if rstat_key is not None and key == rstat_key:
            # 17.49 station status string: fold the token into the snapshot
            # (station switches) AND mirror into _ext_cache so the core-only
            # confirm poll cannot resurrect the previous cycle. An empty
            # push is dropped — the replay cache keeps the last truth.
            token = str(message.value).strip() if message.value is not None else ""
            if token:
                control_coord.device.apply_ext_push(key[0], key[1], token)
                control_coord.async_apply_push("robot_status", token)
                _kick_poll()
            return
        field = optimistic.get(key)
        if field is not None:
            raw = _as_push_int(message.value)
            if raw is not None:
                control_coord.async_apply_push(field, raw)
                # Mirror into the ext replay cache: the debounced poll is
                # core-only and replays ext props from _ext_cache — without
                # this it resurrects the pre-change value and cancels the
                # fold (live lesson from the write path, 2026-09-25). Harmless
                # for core-read props: replay only covers the ext lists.
                control_coord.device.apply_ext_push(key[0], key[1], raw)
            if field == "status" and map_coord is not None:
                # Relocation in/out changes the live overlay (the pin) even
                # when no fresh blob uploads — nudge the map cycle too.
                map_coord.async_notify_status_push()
        _kick_poll()

    client = MiotMqttClient(
        hass,
        region=str(data[CONF_OAUTH_REGION]),
        did=str(data[CONF_DEVICE_ID]),
        client_device_id=str(data[CONF_OAUTH_DEVICE_ID]),
        access_token=str(data[CONF_OAUTH_ACCESS_TOKEN]),
        token_provider=_token_provider,
        on_message=_on_message,
    )
    try:
        await client.async_start()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Failed to start MIoT MQTT client — continuing without it")
        return None
    return client


async def _async_refresh_oauth_for_mqtt(
    hass: HomeAssistant, entry: JonrConfigEntry, *, force: bool
) -> bool:
    """Refresh OAuth for MQTT; keep stale tokens when a due-check refresh fails."""
    try:
        return await async_refresh_oauth_entry(hass, entry, force=force)
    except Exception:  # noqa: BLE001
        if force:
            raise
        _LOGGER.exception("MIoT OAuth proactive refresh failed; using stored token")
        return False


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries forward.

    Bump ConfigFlow.VERSION and add a branch here when entry.data changes shape,
    so existing users never have to log in again. New optional keys that the code
    reads with .get() don't need a migration at all.
    """
    if entry.version == 1:
        # v1 -> v2: stop persisting the Mi account password. The session is kept
        # alive with the long-lived passToken, so the stored password is dead
        # weight; drop it. (Local-only entries never had one.)
        data = dict(entry.data)
        data.pop(CONF_PASSWORD, None)
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        return True
    return False
