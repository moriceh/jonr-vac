"""Polling coordinator for a JONR vacuum (local MIoT)."""
from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta
from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from miio.exceptions import DeviceException

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .device import (
    DeviceCommunicationError,
    XtlVacuumDevice,
    VacuumStatus,
    merge_preserved_ext,
)

_LOGGER = logging.getLogger(__name__)


class JonrVacuumCoordinator(DataUpdateCoordinator[VacuumStatus]):
    """Fetches status from the vacuum over local MIoT on an interval.

    The very first poll reads only the core property group (one UDP
    round-trip on xtl, worst case one 15 s timeout) so entry setup stays
    inside HA's 60 s setup budget even when the robot is asleep; every
    later poll reads the full set — extended chunks that time out are
    dropped softly and only blank the slow settings/stats fields.
    """

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device: XtlVacuumDevice
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{device.model}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.device = device
        self.entry = entry
        self._first_done = False
        self._next_full = True

    async def _async_update_data(self) -> VacuumStatus:
        # First cycle core-only; later polls full unless a command/push just
        # scheduled a core-only confirmation poll (see async_schedule_confirm).
        full = self._first_done and self._next_full
        self._next_full = True
        try:
            status = await self.hass.async_add_executor_job(
                partial(self.device.status, full=full)
            )
        except (DeviceException, DeviceCommunicationError) as err:
            raise UpdateFailed(f"Error polling vacuum: {err}") from err
        # A poll is never allowed to blank a field it did not read: the
        # core-only confirm polls (and soft-group chunk timeouts inside full
        # polls) would otherwise drop every ext entity to unknown for the
        # ~10-70 s until the next full read (live regression 2026-09-25).
        status = merge_preserved_ext(
            status, self.data, brand=self.device.profile.brand
        )
        self._first_done = True
        return status

    def async_schedule_confirm(self, *, full: bool = False) -> None:
        """Confirm a just-issued write with a BACKGROUND poll, and return.

        Awaiting the poll used to pin every service call for the full poll
        duration: 6-8 sequential UDP round-trips, and a silent extended
        chunk costs ~45-75 s on this firmware (4 recv retries x 15 s +
        handshakes). The write itself is still awaited by the caller (real
        errors must surface), the visible flip is already the optimistic
        one (async_apply_raw) or the MQTT push, so this poll is only the
        reconciliation echo — nothing justifies blocking the user on it.
        Core-only by default: the status/gears a command touches live in the
        core group and get re-read; whatever the poll does not read (ext
        settings, count/route, consumable JSON) survives through
        merge_preserved_ext instead of blanking to unknown. Writes whose
        readback is an ext field pass full=True so the next poll re-reads
        it for real."""
        # Last scheduling wins (timer polls stay full: the flag re-arms after
        # every poll in _async_update_data).
        self._next_full = full
        self.hass.async_create_task(self.async_request_refresh())

    # Profile field -> VacuumStatus raw attribute folded straight from a
    # cloud MQTT push (and its echo poll). The ext props (read via
    # _batch_get_soft, live 2026-09-25 probe: the robot pushes them within
    # seconds of an app-side change) join the gear read-backs — the push is
    # folded AND mirrored into device._ext_cache by the __init__ hook, so
    # the core-only confirm poll cannot resurrect the stale value.
    # String/JSON props (clean_values, consumables, timers, records) stay
    # out of THIS int-fold table; they have their own list-fold branch in
    # __init__ (_on_message json_watch / json_ext).
    # disturb_time (17.19) is the exception: its LOCAL read serves stale
    # after local writes, so the __init__ push hook folds it as truth.
    _PUSH_RAW_ATTRS = {
        "fan_speed": "fan_speed_raw",
        "water_level": "water_level_raw",
        "mode": "mode_raw",
        "count": "count_raw",
        "route": "fine_drag_raw",
        "erp": "erp_raw",
        "volume": "volume_raw",
        "child_lock": "child_lock_raw",
        "disturb": "disturb_raw",
        "break_point": "break_point_raw",
        "carpet_boost": "carpet_boost_raw",
        "auto_drying": "auto_drying_raw",
        "auto_solution": "auto_solution_raw",
        "carpet_twice": "carpet_twice_raw",
        "carpet_first": "carpet_first_raw",
        "mop_augment": "mop_augment_raw",
        "carpet_prefer": "carpet_prefer_raw",
        "dust_collection": "dust_collection_raw",
        "drying_time": "drying_time_raw",
        "mop_wash_freq": "mop_wash_freq_raw",
        "mop_wash_temp": "mop_wash_temp_raw",
        "voice_language": "voice_language_raw",
        "message": "message_raw",
        "station_error": "station_error_raw",
        "clean_type_status": "robot_status_raw",
    }

    # String-valued push fields (same fold law, str normalisation):
    # robot-status 17.49 carries the plugin's curRobotStatus tokens
    # (WashMop/HotDry/WindDry/ClctDust/Charging/...) — the station switches'
    # live state source. Kept out of the int table: int("WashMop") would
    # drop every push, and the fold's equality guard must compare strings.
    _PUSH_STR_ATTRS = {
        "robot_status": "station_status_raw",
    }

    def async_apply_raw(self, status_attr: str, raw: int) -> None:
        """Fold one raw readback value into the cached snapshot now."""
        data = self.data
        if data is None or getattr(data, status_attr, "x") == raw:
            return
        self.async_set_updated_data(dataclasses.replace(data, **{status_attr: raw}))

    def async_apply_push(self, field: str, raw: int) -> None:
        """Fold one cloud MQTT property push into the cached snapshot now.

        The UDP poll is 10 s + per-chunk timeouts, so state lags by design;
        the MIoT cloud push knows the truth instantly. Only the pushed field
        is rewritten (activity recomputed through the profile's status_map
        for "status"); the debounced poll that follows re-reads the full
        snapshot, so a lost/garbled push is self-healing. No-op before the
        first successful poll (no snapshot to fold into)."""
        data = self.data
        if data is None:
            return
        core = self.device.core
        if field in self._PUSH_RAW_ATTRS:
            self.async_apply_raw(self._PUSH_RAW_ATTRS[field], raw)
            return
        if field in self._PUSH_STR_ATTRS:
            self.async_apply_raw(self._PUSH_STR_ATTRS[field], str(raw))
            return
        if field == "status" and raw != data.raw_status:
            data = dataclasses.replace(
                data, raw_status=raw,
                activity=core.status_map.get(raw, data.activity),
            )
        elif field == "fault" and raw != data.fault:
            data = dataclasses.replace(data, fault=raw)
        elif field == "battery" and raw != data.battery:
            data = dataclasses.replace(data, battery=raw)
        else:
            return
        self.async_set_updated_data(data)
