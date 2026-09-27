"""Cloud map coordinator: periodically fetch + parse the active map."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .cloud.connector import XiaomiCloud
from .cloud.mqtt import MqttMessage
from .const import (
    CONF_DEVICE_ID,
    CONF_MODEL,
    CONF_PASS_TOKEN,
    CONF_SERVER,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    CONF_USERNAME,
    DOMAIN,
    MAP_IDLE_INTERVAL,
    MAP_SCAN_INTERVAL,
)
from .coordinator import JonrVacuumCoordinator
from .device import XtlVacuumDevice
from .map import MapFetcher, MapResult, SessionExpired
from .map_cache import MapCache
from .spec.profiles.xtl import xtl_add_known_maps, xtl_apply_map_names
from .spec.types import XtlMapCapability

# activities where the map is actually changing ("localizing" = xtl
# Relocation: the robot is out sweeping its surroundings to re-localize)
_ACTIVE = {"cleaning", "returning", "localizing"}

# xtl pushes map updates as MIoT events on siid 17: eiid 1 map-data-report
# (payload carries the fresh KS3 file name), eiid 2 map-infos-report. The
# official plugin is push-only for maps (300 ms throttle, no timer) — here
# the event likewise just kicks this coordinator's debounced refresh.
_XTL_SIID_MAP = 17
_XTL_EIID_MAP_DATA = 1
_XTL_EIID_MAP_INFOS = 2

# Config-entry key where the last map-infos titles are persisted (JSON
# {"<mapId>": "<name>"}) so the Active Map select keeps its real names
# across restarts, until the next map-infos-report push refreshes them.
_XTL_NAMES_KEY = "xtl_map_names"


def _xtl_event_file_name(msg: MqttMessage) -> str | None:
    """First non-empty string an xtl map-report event carries — the fresh
    KS3 file name. Real shape (live 2026-09-25): [{"piid": 38, "value":
    "uid/did/slot"}] — the out-param dict carries it; a bare string or a
    plain list of strings is tolerated."""
    args = [msg.arguments] if isinstance(msg.arguments, str) else (
        msg.arguments or []
    )
    for arg in args:
        if isinstance(arg, dict):
            arg = arg.get("value")
        if isinstance(arg, str) and arg.strip():
            return arg.strip()
    return None


def _xtl_names_from_entry(entry: ConfigEntry) -> dict[int, str]:
    """mapId -> user name persisted by a previous run (see _XTL_NAMES_KEY)."""
    raw = entry.data.get(_XTL_NAMES_KEY)
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[int, str] = {}
    for key, value in parsed.items():
        try:
            out[int(key)] = str(value)
        except (TypeError, ValueError):
            continue
    return out

# Burst window: collapse multiple upload events within this window into one fetch
_DEBOUNCE_SECONDS = 2.0
_MAP_REFRESH_WAIT_SECONDS = 120.0
_MAP_REFRESH_POLL_SECONDS = 3.0
_REFRESH_READY_ACTIVITIES = {"docked", "idle"}

# Cache key for devices whose map capability has no map-list catalogue at all
# (single physical map). There is exactly one map, so a fixed key round-trips
# the cache correctly.
_SINGLE_MAP_ID = 0

# Vector keys that only make sense for the CURRENTLY active map; stripped from
# any other cached map shown in `.maps` so its stale position doesn't render on
# the wrong floor plan (map-reliability Phase 2: live-overlay scoping).
_LIVE_ONLY_VECTOR_KEYS = ("path", "path_segments", "vacuum", "goto",
                          "vacuum_room", "vacuum_room_name")

_LOGGER = logging.getLogger(__name__)


def _static_only(vector: dict) -> dict:
    return {k: v for k, v in vector.items() if k not in _LIVE_ONLY_VECTOR_KEYS}


class JonrMapCoordinator(DataUpdateCoordinator[MapResult]):
    """Holds a logged-in cloud session and refreshes the map."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: XtlVacuumDevice,
        control: JonrVacuumCoordinator,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:map:{entry.data[CONF_MODEL]}",
            update_interval=timedelta(seconds=MAP_IDLE_INTERVAL),
        )
        self.entry = entry
        self._device = device
        self._control = control
        self._cloud: XiaomiCloud | None = None
        self._fetcher: MapFetcher | None = None
        self._cache: MapCache | None = None
        # Last successful get-map-list read ([{name,id,cur}...]), kept up to date
        # every cycle EVEN WHEN no map decrypts, so the "Active Map" select can
        # list/switch maps independent of whether the current upload is readable.
        self._map_list_meta: list[dict] = []
        # Whether this profile has a map-list catalogue at all (set in _build).
        # A device without one has exactly one physical map -> _SINGLE_MAP_ID.
        self._has_map_list = False
        self._mqtt_debounce_task: asyncio.Task | None = None
        self._mqtt_upload_waiters: set[asyncio.Future[None]] = set()
        self._last_live_at: float | None = None
        self._refresh_map_lock = asyncio.Lock()
        self._pending_entry_updates: dict[str, str] = {}
        # xtl: file name stashed by the last map-data-report MQTT event; the
        # next _fetch_xtl cycle consumes it instead of re-running the local
        # get-map-data action. None = no pending hint.
        self._xtl_obj_hint: str | None = None
        # xtl: file name stashed by the last map-infos-report (eiid 2) event.
        # That KS3 file is the MAP CATALOGUE whose per-map `name` is the
        # title the user typed in the app (the local 17.13 answer carries
        # none — decompiled plugin: setMapInfos titles its list from it).
        self._xtl_infos_hint: str | None = None
        # mapId -> user name, from the last map-infos file read; seeded from
        # the config entry so the select's names survive restarts.
        self._xtl_names: dict[int, str] = _xtl_names_from_entry(entry)
        self._xtl_names_dirty = False
        # The map-infos file's FULL catalogue ([{name,id,cur}...], saved
        # maps only) — the plugin's map manager lists its floors from this
        # same view (bug 2026-09-26: the Active Map select showed only the
        # current map). Last good read wins; [] (never read / flaked
        # download) keeps the synthesised single-map list in charge.
        self._xtl_catalog: list[dict] = []

    @property
    def device(self) -> XtlVacuumDevice:
        return self._device

    @property
    def control(self) -> JonrVacuumCoordinator:
        return self._control

    @property
    def map_list_meta(self) -> list[dict]:
        """Latest [{name,id,cur}...] from get-map-list; available even when no
        map decrypts, so the Active Map select isn't gated on decrypt success."""
        return self._map_list_meta

    def _tune_interval(self) -> None:
        """Poll fast while the vacuum is moving, slowly when docked/idle."""
        data = self._control.data
        active = bool(data and data.activity in _ACTIVE)
        secs = MAP_SCAN_INTERVAL if active else MAP_IDLE_INTERVAL
        new = timedelta(seconds=secs)
        if self.update_interval != new:
            self.update_interval = new

    async def async_on_mqtt_message(self, msg: MqttMessage) -> None:
        """Handle an MQTT message routed from the integration setup.

        Triggered by: xtl map-data-report (siid 17 / eiid 1) or
        map-infos-report (siid 17 / eiid 2).
        """
        if (
            msg.kind == "event"
            and msg.siid == _XTL_SIID_MAP
            and msg.eiid in (_XTL_EIID_MAP_DATA, _XTL_EIID_MAP_INFOS)
        ):
            # xtl map reports carry the fresh KS3 file name in their
            # arguments — stash it so the cycle skips the local round-trip
            # (eiid 1 = the map blob, eiid 2 = the map-infos catalogue).
            # No upload request applies (the push means the upload landed).
            file_name = _xtl_event_file_name(msg)
            if file_name is not None:
                if msg.eiid == _XTL_EIID_MAP_DATA:
                    self._xtl_obj_hint = file_name
                else:
                    self._xtl_infos_hint = file_name
            _LOGGER.debug(
                "MQTT xtl map report (eiid %s) — scheduling map refresh", msg.eiid
            )
            self._notify_mqtt_upload_waiters()
            self._schedule_mqtt_refresh()

    def _new_mqtt_upload_waiter(self) -> asyncio.Future[None]:
        waiter: asyncio.Future[None] = self.hass.loop.create_future()
        self._mqtt_upload_waiters.add(waiter)
        waiter.add_done_callback(self._mqtt_upload_waiters.discard)
        return waiter

    def _notify_mqtt_upload_waiters(self) -> None:
        for waiter in tuple(self._mqtt_upload_waiters):
            if not waiter.done():
                waiter.set_result(None)

    def _fresh_live_after(self, since: float) -> bool:
        return self._last_live_at is not None and self._last_live_at > since

    async def _async_wait_for_mqtt_upload(
        self, waiter: asyncio.Future[None], since: float,
    ) -> None:
        deadline = time.monotonic() + _MAP_REFRESH_WAIT_SECONDS
        while time.monotonic() < deadline:
            if self._fresh_live_after(since):
                return
            if waiter.done():
                await self.async_request_refresh()
                return
            await asyncio.sleep(1)
        if self._fresh_live_after(since):
            return
        raise HomeAssistantError("Timed out waiting for a fresh map upload event")

    async def _async_poll_for_live_map(self, since: float) -> None:
        deadline = time.monotonic() + _MAP_REFRESH_WAIT_SECONDS
        while time.monotonic() < deadline:
            await self.async_request_refresh()
            if self._fresh_live_after(since):
                return
            remaining = deadline - time.monotonic()
            await asyncio.sleep(min(_MAP_REFRESH_POLL_SECONDS, max(0.0, remaining)))
        raise HomeAssistantError("Timed out waiting for a readable fresh map")

    async def async_refresh_map_with_movement(
        self, *, confirm_movement: bool, use_mqtt: bool,
    ) -> None:
        """Briefly start the vacuum to force a fresh map upload, then dock."""
        if confirm_movement is not True:
            raise HomeAssistantError("Refresh map requires confirm_movement: true")
        if self._refresh_map_lock.locked():
            raise HomeAssistantError("A map refresh is already running")

        async with self._refresh_map_lock:
            if self._device.profile.map is None:
                raise HomeAssistantError("This vacuum has no supported map capability")
            await self._control.async_request_refresh()
            status = self._control.data
            if status is None or status.activity not in _REFRESH_READY_ACTIVITIES:
                raise HomeAssistantError("Refresh map is only available while docked or idle")
            if self._device.core.start is None or self._device.core.charge is None:
                raise HomeAssistantError("This vacuum cannot start and return to dock")

            since = self._last_live_at or 0.0
            waiter = self._new_mqtt_upload_waiter() if use_mqtt else None
            dock_on_exit = False
            try:
                dock_on_exit = True
                await self.hass.async_add_executor_job(self._device.start)
                await self._control.async_request_refresh()
                if waiter is not None:
                    await self._async_wait_for_mqtt_upload(waiter, since)
                else:
                    await self._async_poll_for_live_map(since)
            finally:
                if waiter is not None and not waiter.done():
                    waiter.cancel()
                if dock_on_exit:
                    with contextlib.suppress(Exception):
                        await self.hass.async_add_executor_job(self._device.return_home)
                    with contextlib.suppress(Exception):
                        await self._control.async_request_refresh()

    def async_notify_status_push(self) -> None:
        """Cloud status push: re-run a map cycle soon so live overlays that
        key off the status (the relocation pin) flip promptly even when the
        robot uploads no fresh blob around the transition."""
        self._schedule_mqtt_refresh()

    def _schedule_mqtt_refresh(self) -> None:
        """Cancel any pending debounce task and start a fresh one."""
        if self._mqtt_debounce_task is not None:
            self._mqtt_debounce_task.cancel()
        self._mqtt_debounce_task = self.hass.async_create_task(
            self._mqtt_debounced_refresh()
        )

    async def _mqtt_debounced_refresh(self) -> None:
        try:
            await asyncio.sleep(_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        self._mqtt_debounce_task = None
        await self.async_refresh()

    def _build(self) -> MapFetcher:
        """Blocking: restore the saved cloud session and construct a fetcher.

        xtl maps need no key material (plain-JSON KS3 blobs), so unlike the
        upstream brand parsers there is no wifi_sn/mac to read live here.
        """
        d = self.entry.data
        self._pending_entry_updates = {}
        # No password: the saved session is restored and renewed via passToken.
        cloud = XiaomiCloud(d[CONF_USERNAME])
        cloud.restore_session(d[CONF_USER_ID], d[CONF_SSECURITY], d[CONF_SERVICE_TOKEN],
                              d.get(CONF_PASS_TOKEN))
        self._cloud = cloud

        cap = self._device.profile.map
        self._has_map_list = (
            isinstance(cap, XtlMapCapability)
            and getattr(cap, "get_map_list", None) is not None
        )

        return MapFetcher(
            cloud,
            server=d[CONF_SERVER],
            user_id=d[CONF_USER_ID],
            device_id=d[CONF_DEVICE_ID],
            model=d[CONF_MODEL],
        )

    def _persist_pending_entry_updates(self) -> None:
        if not self._pending_entry_updates:
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, **self._pending_entry_updates},
        )
        self._pending_entry_updates = {}

    async def _ensure_cache(self) -> MapCache:
        if self._cache is None:
            cache = MapCache(self.hass, self.entry.entry_id)
            await cache.async_load()
            self._cache = cache
        return self._cache

    def _fetch_slots(self) -> list[MapResult | None]:
        """Blocking: one fetch cycle for the active map.

        xtl has no cloud slots at all — one device-named KS3 object per
        cycle, see _fetch_xtl (kept the upstream method name so the
        executor call sites read the same).
        """
        return self._fetch_xtl()

    def _fetch_xtl(self) -> list[MapResult | None]:
        """One xtl cycle: the device-named KS3 object, no slot pair.

        The robot mints a fresh file name per upload; take it from the last
        map-data-report MQTT event when one is pending (parity with the
        plugin's push-only cadence) and otherwise run get-map-data (17.12).
        No name anywhere = "nothing uploaded yet", which is NOT a dead
        session: return an empty cycle and let the cache serve. SessionExpired
        from a URL-less cloud answer propagates like for the slot brands
        (outer handler renews the token and retries once). Blocking; runs in
        the executor via _fetch_slots.
        """
        obj, self._xtl_obj_hint = self._xtl_obj_hint, None
        infos_obj, self._xtl_infos_hint = self._xtl_infos_hint, None
        if not infos_obj and self._device.last_map_infos_file:
            # No eiid-2 push this cycle: the map_list() call at the top of
            # this very cycle stashed the 17.13 answer — that IS the
            # catalogue file name (the plugin's getMapInfos -> download
            # chain; probe 2026-09-25). One action feeds both readers; the
            # cloud 17.13 names the very same slot (probe 2026-09-26), so
            # the local answer is never stale.
            infos_obj = self._device.last_map_infos_file
        if infos_obj and self._fetcher is not None:
            # One download, two views: the app's map titles ({mapId: name})
            # and the saved-map catalogue ([{name,id,cur}...] — the Active
            # Map select's multi-map source, bug 2026-09-26).
            try:
                names, catalog = self._fetcher.fetch_map_infos(infos_obj)
            except Exception as err:  # noqa: BLE001 — names are enhancement-only
                _LOGGER.debug("xtl map-infos fetch failed (%s): %s", infos_obj, err)
                names, catalog = {}, []
            if names and not names.items() <= self._xtl_names.items():
                self._xtl_names = {**self._xtl_names, **names}
                self._xtl_names_dirty = True
            if catalog:
                # A read that yields rows replaces the list; an empty one
                # (junk shape) must not blank the last good catalogue.
                self._xtl_catalog = catalog
        if not obj:
            try:
                obj = self._device.map_obj_name()
            except Exception as err:  # noqa: BLE001 — local UDP action can time out
                _LOGGER.debug("xtl get-map-data action failed: %s", err)
                return [None]
        if not obj:
            _LOGGER.debug("xtl map cycle: no file name from event or device yet")
            return [None]
        # Tip modes need the robot's live state; the control coordinator
        # already polls the raws (17.9/34/10/13/12/65/14) — no extra reads.
        # Before its first refresh there is nothing to show: legacy render.
        status = self._control.data
        live = None
        if status is not None:
            live = {"robot_status": status.robot_status_raw,
                    "clean_values": status.clean_values_raw,
                    "work_mode": status.mode_raw, "fan": status.fan_speed_raw,
                    "water": status.water_level_raw,
                    "route": status.fine_drag_raw, "count": status.count_raw,
                    # siid2/piid2 code — drives the relocation pin render.
                    "status_code": status.raw_status,
                    # 17.49 token — drives the station glyph (base_*_svg
                    # variant per activity, mirror of the plugin StationIcon).
                    "station_status": status.station_status_raw}
        return [self._fetcher.fetch_obj(obj, live=live)]

    def _resolve_active_id(
        self, active_meta: dict | None, decoded: list[MapResult], maps_meta: list[dict],
    ) -> int | None:
        """Which map this cycle's data belongs to, in order of trust:

        1. A live blob's own embedded id (ground truth from the cloud blob
           itself — ijai only; other brands never carry one).
        2. The local map-list's "cur" entry (independent of the cloud fetch).
        3. A fixed single-map key, but ONLY when this profile has no map-list
           capability at all — an empty `maps_meta` from a transient read
           failure on a multi-map device must NOT be mistaken for that.
        """
        for r in decoded:
            if r.map_id is not None:
                return r.map_id
        if active_meta and active_meta.get("id") is not None:
            try:
                return int(active_meta["id"])
            except (TypeError, ValueError):
                pass
        if not self._has_map_list and not maps_meta:
            return _SINGLE_MAP_ID
        return None

    def _serve(
        self, cache: MapCache, active_id: int | None, maps_meta: list[dict],
    ) -> MapResult | None:
        """Build the result to serve from whatever the cache now holds for
        `active_id` (a live decode this cycle was already upserted before this
        runs, so cache and live are never out of sync — serve-parity holds by
        construction). None only when nothing has ever been cached for it.
        """
        if active_id is None:
            return None
        entry = cache.get(active_id)
        if entry is None:
            return None
        name_by_id = {
            int(m["id"]): m.get("name") for m in maps_meta if m.get("id") is not None
        }
        # xtl: cached maps absent from maps_meta (only the active one is
        # synthesised there) still carry their app-given title.
        for map_id, name in self._xtl_names.items():
            name_by_id.setdefault(map_id, name)
        served = MapResult(
            image_png=entry.png,
            attributes=entry.attributes,
            vector=entry.vector,
            map_id=active_id,
            content_hash=entry.content_hash,
        )
        served.maps = [
            {
                **(cached.vector if map_id == active_id else _static_only(cached.vector)),
                "map_id": map_id,
                "map_name": name_by_id.get(map_id),
                "active": map_id == active_id,
            }
            for map_id, cached in cache.all().items()
        ]
        return served

    async def _async_update_data(self) -> MapResult:
        self._tune_interval()
        try:
            if self._fetcher is None:
                self._fetcher = await self.hass.async_add_executor_job(self._build)
                self._persist_pending_entry_updates()
            cache = await self._ensure_cache()

            # The map list (distinct physical maps) drives multi-map; best-effort
            # so a flaky local read never blocks the active-map fetch.
            try:
                maps_meta = await self.hass.async_add_executor_job(self._device.map_list)
            except Exception:  # noqa: BLE001
                maps_meta = []
            # Keep the switchable map list current even if the rest of this
            # cycle fails to produce a readable map. Never clobber a good list
            # with a transient empty read (same guard as cache prune) — a UDP
            # flake on the list read must not drop active-id resolution (it
            # would flicker the camera to "unavailable" for one cycle).
            # xtl is special-cased: get-map-infos answers with the current
            # map's obj string ("uid/did/slot", probe 2026-09-25), not a
            # catalogue, so its synthesised [{id: uid}] guess must never even
            # reach resolution or prune (it would orphan the blob-keyed
            # entries, whose ground-truth id is the blob's embedded
            # mapHeadId). The device call above still runs so the shape probe
            # keeps recording; xtl's list is (re)synthesised below from each
            # successful decode and its last good value serves meanwhile.
            if isinstance(self._device.profile.map, XtlMapCapability):
                maps_meta = self._map_list_meta
            elif maps_meta:
                self._map_list_meta = maps_meta
            else:
                maps_meta = self._map_list_meta

            try:
                slot_results = await self.hass.async_add_executor_job(self._fetch_slots)
            except SessionExpired:
                # Token likely expired — renew it with the passToken and retry.
                # If renewal fails, the passToken is dead too: ask the user to
                # re-auth (raises a reauth flow) rather than dead-end.
                if not await self._refresh_and_persist():
                    raise ConfigEntryAuthFailed("Xiaomi cloud map session expired") from None
                try:
                    slot_results = await self.hass.async_add_executor_job(self._fetch_slots)
                except SessionExpired:
                    # The passToken renewal just above proved the account
                    # credentials are valid, so a second empty map URL right
                    # after isn't an auth problem — the cloud simply has
                    # nothing to serve yet (no map uploaded under this
                    # obj_name, region mismatch, transient hiccup). Reporting
                    # this as ConfigEntryAuthFailed would force a reauth the
                    # user can never satisfy (login keeps succeeding, then
                    # immediately bounces back to reauth every cycle).
                    raise UpdateFailed(
                        "Xiaomi cloud has no map available (session is valid)"
                    ) from None
            if self._xtl_names_dirty:
                # Fresh map titles landed with this cycle's map-infos file:
                # persist them (same pattern as the wifi_sn entry update).
                self._xtl_names_dirty = False
                self._pending_entry_updates[_XTL_NAMES_KEY] = json.dumps(
                    {str(k): v for k, v in self._xtl_names.items()},
                    ensure_ascii=False,
                )
                self._persist_pending_entry_updates()

            decoded = [r for r in slot_results if r is not None]
            active_meta = next((m for m in maps_meta if m.get("cur")), None)
            active_id = self._resolve_active_id(active_meta, decoded, maps_meta)
            _LOGGER.debug(
                "Map cycle: slot keys=%s active_id=%s maps_listed=%d",
                ["A" if r is not None else "B" for r in slot_results],
                active_id, len(maps_meta),
            )
            # xtl: the ground truth for "which maps exist" is the mapHeadId
            # embedded in decoded blobs (get-map-infos has no catalogue).
            # Synthesise the honest single-map list from each successful
            # decode so the Active Map select, cache prune and serve all
            # agree on that id (and switching targets it on 17.9 piid 58).
            if (isinstance(self._device.profile.map, XtlMapCapability)
                    and decoded and active_id is not None):
                # saved=0 until the catalogue vouches for it (xtl_add_known_maps
                # below): the blob id is the robot's LIVE working copy (the
                # plugin's unsaved "New Map" draft), which must never be a
                # switch target — switching to it blanks the robot's map
                # (live 2026-09-26: a tap on the "Map 1" ghost option did
                # exactly that; Mi Home's "restore" replays switch-map on the
                # saved floor to undo it).
                maps_meta = [{"name": None, "id": int(active_id), "cur": 1,
                              "saved": 0}]
                self._map_list_meta = maps_meta
            elif (active_id is None
                    and isinstance(self._device.profile.map, XtlMapCapability)):
                # Cold Key-B stretch (no decode yet, catalogue still empty):
                # a sole cached render IS the single-map truth — serve it
                # instead of blacking the camera out until the next upload.
                cached_ids = list(cache.all())
                if len(cached_ids) == 1:
                    active_id = cached_ids[0]

            # Overlay the user-given titles from the last map-infos file AND
            # append the saved maps it lists but the blob-synthesised list
            # never carries (bug 2026-09-26: the select only ever showed the
            # current map — Mi Home lists them all from this same catalogue).
            # The selector label (select._map_label) and the vector API's
            # `.maps[].map_name` (http_api → card) both read `name` here.
            if isinstance(self._device.profile.map, XtlMapCapability):
                maps_meta = xtl_add_known_maps(maps_meta, self._xtl_catalog)
                if self._xtl_names:
                    maps_meta = xtl_apply_map_names(maps_meta, self._xtl_names)
                self._map_list_meta = maps_meta

            # Whichever slot decrypted (Key A) wins and refreshes the cache for
            # this map id; both slots being None just means both were Key B this
            # cycle — normal, not an error, and handled by serving from cache.
            live = decoded[0] if decoded else None
            if live is not None and active_id is not None:
                live_at = time.time()
                self._last_live_at = live_at
                await cache.async_upsert(
                    active_id,
                    png=live.image_png,
                    attributes=live.attributes,
                    vector=live.vector,
                    content_hash=live.content_hash,
                    timestamp=live_at,
                )

            # Prune maps the device no longer lists, but never off a transient
            # empty read (map-reliability Phase 0 eviction decision).
            if maps_meta:
                keep_ids = {int(m["id"]) for m in maps_meta if m.get("id") is not None}
                if keep_ids:
                    await cache.async_prune(keep_ids)

            result = self._serve(cache, active_id, maps_meta)
            if result is None:
                # Nothing live AND nothing cached for this map id yet. This is
                # the normal cold-start state: the vacuum's current upload is a
                # bad ("Key B") blob that Mi Home can't read either, and we have
                # no prior good copy to fall back on. Not an error and not
                # actionable by the user — the cache fills the moment the vacuum
                # next uploads a good blob (any normal clean/dock does it), and
                # we serve silently from then on. So: no repair notice, no
                # warning-level log; just stay unavailable and keep polling.
                #
                # It's also what a fetcher built with stale key inputs produces
                # (wifi_sn/mac read while the device was briefly unreachable at
                # startup), so drop the fetcher to re-read live values next cycle.
                self._fetcher = None
                _LOGGER.debug(
                    "No readable map yet for active_id=%s: current upload is a "
                    "bad blob and nothing cached — waiting for a good upload",
                    active_id,
                )
                raise UpdateFailed("Waiting for a readable map upload from the vacuum")
            _LOGGER.debug(
                "Serving map active_id=%s (%s this cycle), %d map(s) cached",
                active_id, "live+cached" if decoded else "from cache",
                len(cache.all()),
            )
            return result
        except (UpdateFailed, ConfigEntryAuthFailed):
            raise
        except SessionExpired:
            raise ConfigEntryAuthFailed("Xiaomi cloud map session expired") from None
        except Exception as err:  # noqa: BLE001
            self._fetcher = None
            raise UpdateFailed(f"Map update error: {err}") from err

    async def _refresh_and_persist(self) -> bool:
        """Renew the cloud session via passToken and save the new tokens."""
        if self._cloud is None:
            return False
        if not await self.hass.async_add_executor_job(self._cloud.refresh):
            return False
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                CONF_SERVICE_TOKEN: self._cloud.service_token,
                CONF_SSECURITY: self._cloud.ssecurity,
                CONF_PASS_TOKEN: self._cloud.pass_token or "",
            },
        )
        _LOGGER.info("Renewed Xiaomi cloud session via passToken")
        return True
