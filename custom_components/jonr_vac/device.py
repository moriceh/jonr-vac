"""Local MIoT client for JONR (xtl) vacuums (synchronous; wrap in executor under HA).

Based on xiaomi-vac by letitbe-dull (MIT) — see CREDITS.md. Upstream this is the
generic device class for every brand profile; this fork ships the xtl profile
only, so the brand-specific capability branches it used to dispatch to are
pruned and the class carries the brand name instead of the generic one.
"""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import logging
import tarfile
import time
import urllib.request
from dataclasses import dataclass

from miio import MiotDevice

from .spec.profiles.xtl import (
    xtl_dnd_same,
    xtl_parse_map_infos,
    xtl_zone_flat,
)
from .spec.registry import card_baseline_gaps, get_profile
from .spec.types import (
    CoreCapability,
    ModelProfile,
    XtlConsumablesCapability,
    XtlCoreCapability,
    XtlMapCapability,
    XtlRoomCleanCapability,
    consumable_life_props,
)

_LOGGER = logging.getLogger(__name__)


class DeviceCommunicationError(Exception):
    """Raised when a required device property cannot be read."""


# Length range for the wifi serial that seeds the map AES key. Firmware isn't
# consistent here — v3 reports 19, the old code only allowed 18 or 20 — so this
# is a range rather than a whitelist (issue #4).
_WIFI_SN_MIN_LEN = 16
_WIFI_SN_MAX_LEN = 24


def _is_wifi_sn(value: str) -> bool:
    return _WIFI_SN_MIN_LEN <= len(value) <= _WIFI_SN_MAX_LEN and value.isupper()


@dataclass
class VacuumStatus:
    activity: str
    raw_status: int
    battery: int | None
    fault: int | None
    fan_speed_raw: int | None
    water_level_raw: int | None
    mode_raw: int | None
    sweep_type_raw: int | None
    repeat_raw: int | None
    alarm_raw: int | None
    volume_raw: int | None
    main_brush_life: int | None
    side_brush_life: int | None
    filter_life: int | None
    mop_life: int | None
    dust_bag_life: int | None
    detergent_life: int | None
    clean_area: int | None
    clean_time: int | None
    # xtl-only telemetry; other models leave these defaults.
    count_raw: int | None = None
    fine_drag_raw: int | None = None
    message_raw: int | None = None
    station_error_raw: int | None = None
    robot_status_raw: int | None = None
    # 17.49 robot-status (string, read+notify): the plugin's curRobotStatus
    # spellings — WashMop / HotDry / WindDry / ClctDust / Charging / ... This
    # is the only readable view of a running dock cycle (the wash/dry/empty
    # props themselves are write-only bools), so the station switches read it.
    station_status_raw: str | None = None
    clean_values_raw: str | list | None = None
    consumable_types: tuple[str, ...] = ()
    extra_consumable_lives: dict[str, int] | None = None
    # xtl entity parity (siid 17) — raw 0/1 per toggled setting.
    child_lock_raw: int | None = None
    disturb_raw: int | None = None
    break_point_raw: int | None = None
    carpet_boost_raw: int | None = None
    auto_drying_raw: int | None = None
    auto_solution_raw: int | None = None
    carpet_twice_raw: int | None = None
    carpet_first_raw: int | None = None
    erp_raw: int | None = None
    mop_augment_raw: int | None = None
    carpet_prefer_raw: int | None = None
    dust_collection_raw: int | None = None
    drying_time_raw: int | None = None
    mop_wash_freq_raw: int | None = None
    mop_wash_temp_raw: int | None = None
    disturb_time_raw: str | None = None       # 17.19 JSON schedule
    voice_language_raw: int | None = None     # 17.25 language-voice code
    language_ver_raw: str | None = None       # 17.78 language-ver (pack Ver)
    total_clean_time: int | None = None
    total_clean_area: int | None = None
    total_clean_count: int | None = None
    clean_records_raw: str | list | None = None
    clean_timers_raw: str | list | None = None    # 17.17 schedule-list JSON
    clean_water_cistern_raw: int | None = None
    drain_cistern_raw: int | None = None
    dust_bag_state_raw: int | None = None
    mop_tank_state_raw: int | None = None
    return_status_raw: int | None = None


def merge_preserved_ext(
    status: VacuumStatus, prev: VacuumStatus | None, *, brand: str
) -> VacuumStatus:
    """Keep the previous snapshot's values wherever the new read came back
    empty, so the background confirm poll (coordinator.async_schedule_confirm,
    core-only) and soft-group chunk timeouts never blank the ext entities to
    unknown (live regression 2026-09-25: tens of seconds of unknown after
    every option change). `prev` is the coordinator's live snapshot — the
    freshest truth known (optimistic flips and MQTT folds included), so it
    wins over the replayed cache for fields this poll did not read. On xtl the
    consumable JSON (17.32) is an ext read whose empty parse ((), {}) is a
    default, not a None: replay it as a unit when this poll did not read it."""
    if prev is None:
        return status
    repl = {
        f.name: getattr(prev, f.name)
        for f in dataclasses.fields(VacuumStatus)
        if getattr(status, f.name) is None and getattr(prev, f.name) is not None
    }
    if (
        brand == "xtl" and not status.consumable_types
        and prev.consumable_types
    ):
        repl["consumable_types"] = prev.consumable_types
        repl["extra_consumable_lives"] = prev.extra_consumable_lives
    return dataclasses.replace(status, **repl) if repl else status


class XtlVacuumDevice:
    """Thin wrapper over python-miio MiotDevice driven by a ModelProfile.

    Refuses to build for a model that has no profile or no runnable ``core``.
    """

    def __init__(self, host: str, token: str, model: str, timeout: int = 5):
        self.host = host
        self.token = token
        self.model = model
        profile = get_profile(model)
        if profile is None or profile.core is None:
            raise ValueError(f"{model} is not a runnable vacuum profile (no core)")
        gaps = card_baseline_gaps(profile)
        if gaps:
            raise ValueError(
                f"{model} does not satisfy the card baseline: {', '.join(gaps)}"
            )
        self.profile: ModelProfile = profile
        self.core: CoreCapability = profile.core
        if profile.brand == "xtl":
            # xtl firmware acks MIoT actions well past the 5 s default; local
            # action writes time out as -9999 otherwise (live, 2026-09-21).
            timeout = max(timeout, 15)
        self._dev = MiotDevice(host, token, mapping={}, timeout=timeout)
        # The extended chunks that go silent (~1 every 2 min live) ride the
        # 15 s action timeout above through python-miio's 4 recv retries +
        # handshakes = ~45-75 s per mute chunk, serialised inside the poll.
        # Soft-group reads tolerate failure by design, so they run on their
        # own short-timeout instance: a mute chunk costs ~20 s, not 75.
        self._dev_soft = MiotDevice(host, token, mapping={}, timeout=5)
        # Cadence + last value for the slow JSON props that the firmware only
        # answers (if) at all when read alone; skipping a read must not blank
        # the sensor, and firmware persists both values.
        self._slow_read_turn = 0
        self._slow_read_cache: dict[str, object] = {}
        # Last value the extended group ever answered with, per (siid, piid).
        # A core-only confirm poll (coordinator.async_schedule_confirm) and a
        # soft chunk that times out inside a full poll must NOT blank every
        # ext entity to unknown — the values are firmware-persistent, so
        # replay the last known one until the next successful read (live
        # regression 2026-09-25: every entity went unknown for tens of
        # seconds after each option change).
        self._ext_cache: dict[tuple[int, int], object] = {}
        # Raw out value of the last get-map-infos (17.13) call: live firmware
        # answers with the map-infos KS3 FILE NAME ("uid/did/slot", probe
        # 2026-09-25) — the very file the plugin downloads to list its maps
        # (getMapInfos -> res[0] -> get_interim_file_url_pro). The map
        # coordinator folds it (one action, two readers, no second round-trip).
        self.last_map_infos_file: str | None = None

    # --- helpers ---------------------------------------------------------
    def _batch_get(self, props: list) -> dict:
        """Batch-read MIoT props in one (or chunked) get_properties call.

        Returns {Prop: value|None}; properties whose device code is non-zero map
        to None.  Raises DeviceCommunicationError on network/protocol failure.
        The chunk size is capped at self.profile.max_properties when set — use
        this for devices that reject large batches (e.g. IJAI_CORE_LEGACY).
        """
        if not props:
            return {}
        batch_size = self.profile.max_properties
        miio_props = [
            {"did": f"{p.siid}-{p.piid}", "siid": p.siid, "piid": p.piid}
            for p in props
        ]
        try:
            raw = self._dev.get_properties(
                miio_props, property_getter="get_properties", max_properties=batch_size
            )
        except Exception as ex:  # noqa: BLE001
            raise DeviceCommunicationError(
                f"Property batch read failed ({len(props)} props): {ex}"
            ) from ex
        value_map: dict[tuple[int, int], object] = {
            (r["siid"], r["piid"]): r.get("value")
            for r in raw
            if isinstance(r, dict) and r.get("code", -1) == 0
        }
        return {p: value_map.get((p.siid, p.piid)) for p in props}

    def _batch_get_soft(self, props: list, batch_size: int | None = None) -> dict:
        """Extended-group read that tolerates per-chunk UDP timeouts.

        The settings/stats/station group is polled once its own chunks fail
        softly: a robot that misses one chunk only loses that chunk's fields
        (they surface as None), while core telemetry must still survive —
        and HA's 60 s config-entry setup budget cannot absorb 5 × 15 s of
        serial xtl timeouts (live: 2026-09-23 20:11 setup cancellation).
        Callers may shrink ``batch_size`` below the profile cap: live
        2026-09-23 showed this firmware never answers a 10-prop batch that
        mixes the stats/JSON props (one ``timed out`` ERROR per cycle while
        everything else stayed fresh).
        """
        if not props:
            return {}
        batch_size = batch_size or self.profile.max_properties
        chunks = (
            [props[i:i + batch_size] for i in range(0, len(props), batch_size)]
            if batch_size else [props]
        )
        value_map: dict[tuple[int, int], object] = {}
        for chunk in chunks:
            miio_props = [
                {"did": f"{p.siid}-{p.piid}", "siid": p.siid, "piid": p.piid}
                for p in chunk
            ]
            try:
                raw = self._dev_soft.get_properties(
                    miio_props, property_getter="get_properties",
                    max_properties=min(batch_size, len(chunk)),
                )
            except Exception as ex:  # noqa: BLE001 - any miio/UDP failure is soft here
                _LOGGER.debug("Extended chunk dropped (%d props): %s", len(chunk), ex)
                continue
            for r in raw:
                if not isinstance(r, dict):
                    continue
                if r.get("code", 0) == 0:
                    value_map[(r["siid"], r["piid"])] = r.get("value")
                else:
                    # A batch the device answered but refused per-prop: name
                    # the culprit (otherwise these reads fail in silence).
                    _LOGGER.warning(
                        "Device refused read of prop %s/%s: code %s",
                        r.get("siid"), r.get("piid"), r.get("code"),
                    )
        out: dict = {}
        for p in props:
            key = (p.siid, p.piid)
            v = value_map.get(key)
            if v is None:
                # Chunk went mute this cycle: replay the last known value
                # instead of blanking the entity (see _ext_cache).
                v = self._ext_cache.get(key)
            else:
                self._ext_cache[key] = v
            out[p] = v
        return out

    def _set(self, prop, value) -> None:
        if prop is None:
            raise ValueError(f"{self.model} does not support this property")
        self._dev.set_property_by(prop.siid, prop.piid, value)

    def _action(self, action, params=None) -> dict:
        if action is None:
            raise ValueError(f"{self.model} does not support this action")
        return self._dev.call_action_by(
            action.siid, action.aiid, self._in_params(action, params or [])
        )

    def _in_params(self, action, params: list) -> list:
        """Wrap raw values as MIoT {"did","piid","value"} dicts, xtl only.

        xtl firmware silently drops `"in": [<raw>]` frames — the UDP socket
        never answers and python-miio surfaces that as "Unable to recover
        failed command" (live, 2026-09-21). Other brands are field-proven
        against the positional form, so only xtl gets spec-strict dicts,
        zipped positionally over the action's declared input piids.
        """
        if not isinstance(self.core, XtlCoreCapability):
            return params
        piids = (action.in_piid,) if action.in_piid is not None else action.in_piids
        if not piids or len(params) != len(piids):
            return params
        return [
            {"did": "-", "piid": piid, "value": value}
            for piid, value in zip(piids, params)
        ]

    # --- telemetry -------------------------------------------------------
    def status(self, full: bool = True) -> VacuumStatus:
        """Read device telemetry in two groups.

        ``full=False`` polls only the core group — a single UDP round-trip on
        xtl — so the coordinator's first poll fits HA's 60 s setup budget even
        when the robot answers slowly; the extended (settings/stats) fields
        just stay None until the first full poll.  The extended group is read
        chunk-soft: one timed-out chunk there never takes core telemetry down
        with it (live: 2026-09-23, 41-prop poll cancelled entry setup).
        """
        c = self.core
        cons = self.profile.consumables
        life_props = consumable_life_props(cons)
        # Core group: what the vacuum platform itself needs, read strictly
        # (a failed core chunk means the device is genuinely unreachable).
        poll = [p for p in (
            c.status, c.battery, c.fault, c.fan_speed, c.water_level,
            c.mode, c.sweep_type, c.repeat, c.alarm, c.volume,
            *life_props.values(),
        ) if p is not None]
        # Extended group (xtl only): 2A telemetry + entity-parity settings /
        # stats / station states — slow-moving, tolerated on failure. The
        # stats/JSON props get their own narrow batches: live 2026-09-23 this
        # firmware never replied to the 10-prop batch mixing them (the whole
        # group read died on UDP timeout every cycle while neighbouring
        # batches stayed fresh), and the two JSON props are read alone on a
        # cadence so a mute one cannot sink the numeric reads with it.
        ext: list = []
        ext_narrow: list = []
        dnd_prop = records_prop = timers_prop = None
        if isinstance(c, XtlCoreCapability):
            ext += [p for p in (c.count, c.route, c.message, c.station_error,
                                 c.clean_type_status, c.robot_status,
                                 c.clean_values,
                                 # entity parity: toggles / selects
                                 # (all read+notify props)
                                 c.child_lock, c.disturb, c.break_point,
                                 c.carpet_boost, c.auto_drying, c.auto_solution,
                                 c.carpet_twice, c.carpet_first, c.erp,
                                 c.mop_augment, c.carpet_prefer,
                                 c.dust_collection, c.drying_time,
                                 c.voice_language, c.language_ver,
                                 c.mop_wash_freq, c.mop_wash_temp,
                                 c.clean_water_cistern, c.drain_cistern,
                                 c.dust_bag_state, c.mop_tank_state,
                                 c.return_status)
                     if p is not None]
            ext_narrow += [p for p in (c.session_time, c.session_area,
                                       c.total_time, c.total_area,
                                       c.total_count)
                           if p is not None]
            dnd_prop, records_prop = c.disturb_time, c.clean_records
            timers_prop = c.clean_timers
            if isinstance(cons, XtlConsumablesCapability) and cons.consumables is not None:
                ext.append(cons.consumables)
        vals = self._batch_get(poll)
        if not full and (ext or ext_narrow):
            # Core-only confirm poll: replay the last value each ext prop
            # ever answered with, so the fresh VacuumStatus does not blank
            # every settings/stats/station entity to unknown until the next
            # full poll (values are firmware-persistent).
            for p in ext + ext_narrow:
                vals[p] = self._ext_cache.get((p.siid, p.piid))
            for prop, key in ((dnd_prop, "dnd"), (records_prop, "records"),
                              (timers_prop, "timers")):
                if prop is not None:
                    vals[prop] = self._slow_read_cache.get(key)
        elif full and (ext or ext_narrow):
            vals.update(self._batch_get_soft(ext))
            vals.update(self._batch_get_soft(ext_narrow, batch_size=3))
            self._slow_read_turn += 1
            for prop, key, every in ((dnd_prop, "dnd", 3),
                                     (records_prop, "records", 6),
                                     (timers_prop, "timers", 6)):
                if prop is None:
                    continue
                if self._slow_read_turn % every == 1:
                    vals.update(self._batch_get_soft([prop]))
                    fresh = vals.get(prop)
                    if fresh is not None and (
                            key != "dnd"
                            # xtl 17.19 quirk (live 2026-09-25): after a local
                            # 17.30 write the robot pushes the new schedule to
                            # the cloud yet its LOCAL get keeps answering the
                            # pre-write JSON — once a write/push installed a
                            # truth, a cadence read may only CONFIRM it
                            # (xtl_dnd_same), never overwrite it. App-side
                            # changes re-install the truth through the push
                            # hook, so nothing real is lost.
                            or self._slow_read_cache.get("dnd") is None
                            or xtl_dnd_same(fresh, self._slow_read_cache["dnd"])):
                        self._slow_read_cache[key] = fresh
                if self._slow_read_cache.get(key) is not None:
                    vals[prop] = self._slow_read_cache[key]
        _raw = vals.get(c.status)
        try:
            raw = int(_raw)
        except (TypeError, ValueError) as ex:
            raise DeviceCommunicationError(
                f"Required property {c.status.siid}/{c.status.piid} read failed: "
                f"returned {_raw!r}"
            ) from ex
        status = VacuumStatus(
            activity=c.status_map.get(raw, "idle"),
            raw_status=raw,
            battery=_as_int(vals.get(c.battery)),
            fault=_as_int(vals.get(c.fault)),
            fan_speed_raw=_as_int(vals.get(c.fan_speed)),
            water_level_raw=_as_int(vals.get(c.water_level)),
            mode_raw=_as_int(vals.get(c.mode)),
            sweep_type_raw=_as_int(vals.get(c.sweep_type)),
            repeat_raw=_as_int(vals.get(c.repeat)),
            alarm_raw=_as_int(vals.get(c.alarm)),
            volume_raw=_as_int(vals.get(c.volume)),
            main_brush_life=_as_int(vals.get(life_props.get("main_brush_life"))),
            side_brush_life=_as_int(vals.get(life_props.get("side_brush_life"))),
            filter_life=_as_int(vals.get(life_props.get("filter_life"))),
            mop_life=_as_int(vals.get(life_props.get("mop_life"))),
            dust_bag_life=_as_int(vals.get(life_props.get("dust_bag_life"))),
            detergent_life=_as_int(vals.get(life_props.get("detergent_life"))),
            clean_area=None,
            clean_time=None,
        )
        if isinstance(c, XtlCoreCapability):
            status.count_raw = _as_int(vals.get(c.count))
            status.fine_drag_raw = _as_int(vals.get(c.route))
            status.message_raw = _as_int(vals.get(c.message))
            status.station_error_raw = _as_int(vals.get(c.station_error))
            status.robot_status_raw = _as_int(vals.get(c.clean_type_status))
            if c.robot_status is not None:
                # 17.49: normalise to str|None (same tolerance as 17.78); an
                # empty read is None so merge_preserved_ext replays the last
                # known status instead of blanking the station switches.
                status.station_status_raw = _as_str(vals.get(c.robot_status)) or None
            status.clean_values_raw = vals.get(c.clean_values)
            # toggles + selects (raw enum/bool values; platforms map labels)
            status.child_lock_raw = _as_int(vals.get(c.child_lock))
            status.disturb_raw = _as_int(vals.get(c.disturb))
            status.break_point_raw = _as_int(vals.get(c.break_point))
            status.carpet_boost_raw = _as_int(vals.get(c.carpet_boost))
            status.auto_drying_raw = _as_int(vals.get(c.auto_drying))
            status.auto_solution_raw = _as_int(vals.get(c.auto_solution))
            status.carpet_twice_raw = _as_int(vals.get(c.carpet_twice))
            status.carpet_first_raw = _as_int(vals.get(c.carpet_first))
            status.erp_raw = _as_int(vals.get(c.erp))
            status.mop_augment_raw = _as_int(vals.get(c.mop_augment))
            status.carpet_prefer_raw = _as_int(vals.get(c.carpet_prefer))
            status.dust_collection_raw = _as_int(vals.get(c.dust_collection))
            status.drying_time_raw = _as_int(vals.get(c.drying_time))
            status.mop_wash_freq_raw = _as_int(vals.get(c.mop_wash_freq))
            status.mop_wash_temp_raw = _as_int(vals.get(c.mop_wash_temp))
            status.disturb_time_raw = vals.get(c.disturb_time)
            status.voice_language_raw = _as_int(vals.get(c.voice_language)) \
                if c.voice_language is not None else None
            status.language_ver_raw = _as_str(vals.get(c.language_ver)) \
                if c.language_ver is not None else None
            # session + lifetime stats (fill the long-standing placeholders)
            status.clean_time = _as_int(vals.get(c.session_time))
            status.clean_area = _as_int(vals.get(c.session_area))
            status.total_clean_time = _as_int(vals.get(c.total_time))
            status.total_clean_area = _as_int(vals.get(c.total_area))
            status.total_clean_count = _as_int(vals.get(c.total_count))
            status.clean_records_raw = vals.get(c.clean_records)
            status.clean_timers_raw = vals.get(c.clean_timers)
            # station presence / dock-task enums
            status.clean_water_cistern_raw = _as_int(vals.get(c.clean_water_cistern))
            status.drain_cistern_raw = _as_int(vals.get(c.drain_cistern))
            status.dust_bag_state_raw = _as_int(vals.get(c.dust_bag_state))
            status.mop_tank_state_raw = _as_int(vals.get(c.mop_tank_state))
            status.return_status_raw = _as_int(vals.get(c.return_status))
            if isinstance(cons, XtlConsumablesCapability):
                types, lives, extras = _parse_xtl_consumables(vals.get(cons.consumables))
                status.consumable_types = types
                for attr, value in lives.items():
                    setattr(status, attr, value)
                status.extra_consumable_lives = extras
        return status

    # --- control ---------------------------------------------------------
    def _stop_and_settle(self) -> None:
        """Close a still-open task before starting a new one. xtl ignores
        start-clean entirely while a previous task is open, so a stop (whose
        ack may itself error when idle) must precede it, then a settle."""
        try:
            self._action(self.core.stop)
        except Exception:  # noqa: BLE001 - a no-op stop may still ack an error
            pass
        time.sleep(2)

    def start(self, activity: str | None = None) -> None:
        if isinstance(self.core, XtlCoreCapability):
            if activity == "paused" and self.core.resume_in:
                # Task stays open across a pause -> continue, don't restart.
                self._action(self.core.pause, list(self.core.resume_in))
                return
            if self.core.start_in:
                if self.core.needs_stop_before_start:
                    self._stop_and_settle()
                self._action(self.core.start, list(self.core.start_in))
                return
        self._action(self.core.start)

    def stop(self) -> None:
        self._action(self.core.stop)

    def pause(self) -> None:
        if isinstance(self.core, XtlCoreCapability) and self.core.pause_in:
            # pause-continue-work is one action driven by robot-set-status 7.
            self._action(self.core.pause, list(self.core.pause_in))
            return
        self._action(self.core.pause if self.core.pause is not None else self.core.stop)

    def return_home(self) -> None:
        self._action(self.core.charge)

    def locate(self) -> None:
        if self.core.locate is not None:
            self._action(self.core.locate)
        elif self.core.alarm is not None:
            self.set_alarm(True)
        else:
            raise ValueError(f"{self.model} has no locate capability")

    def _gear(self, action, prop, value) -> None:
        """Gear change: through a dedicated action where the model's gear
        property is read-only (xtl), plain property write everywhere else."""
        if action is not None:
            self._action(action, [value])
        else:
            self._set(prop, value)

    def set_fan_speed(self, preset: str) -> None:
        self._gear(getattr(self.core, "fan_action", None),
                   self.core.fan_speed, self.core.fan_speeds[preset])

    def set_water_level(self, preset: str) -> None:
        self._gear(getattr(self.core, "water_action", None),
                   self.core.water_level, self.core.water_levels[preset])

    def set_mode(self, preset: str) -> None:
        self._gear(getattr(self.core, "mode_action", None),
                   self.core.mode, self.core.modes[preset])

    def set_sweep_type(self, preset: str) -> None:
        self._set(self.core.sweep_type, self.core.sweep_types[preset])

    def set_repeat(self, on: bool) -> None:
        self._set(self.core.repeat, 1 if on else 0)

    def set_alarm(self, on: bool) -> None:
        self._set(self.core.alarm, on)

    def set_volume(self, value: int) -> None:
        # xtl: volume 17.15 is read+notify; the write channel is set-volume
        # (17.31). Other brands keep the plain property write (via _gear).
        self._gear(getattr(self.core, "volume_action", None),
                   self.core.volume, int(value))

    def set_route(self, preset: str) -> None:
        """Mop route preference — action-only on xtl (fine-drag-switch 17.50)."""
        raw = self.core.routes[preset]
        self._action(self.core.route_action, [raw])
        self._ext_cache[(self.core.route.siid, self.core.route.piid)] = raw

    def set_count(self, preset: str) -> None:
        """Cleaning passes — action-only on xtl (set-clean-count 17.28)."""
        raw = self.core.counts[preset]
        self._action(self.core.count_action, [raw])
        self._ext_cache[(self.core.count.siid, self.core.count.piid)] = raw

    def apply_ext_push(self, siid: int, piid: int, value: object) -> None:
        """Record a cloud-pushed value in the ext replay cache (see _ext_cache).

        Without this the next core-only confirm poll replays the pre-change
        cached value and cancels the coordinator's optimistic fold (live
        lesson from the 2026-09-25 write-path fix, applied to the push path)."""
        self._ext_cache[(siid, piid)] = value

    def xtl_note_json_push(self, key: str, value: object) -> list | None:
        """Install a pushed JSON list-prop (timers/records) into the
        slow-read cache — the push-side twin of xtl_note_dnd for the props
        the cadence solo-reads only re-check every 6 full turns.

        *value* is the raw push payload (already-parsed list from the
        broker, or a JSON string); anything that does not yield a list is
        dropped so a truncated push can never blank a sensor — the cadence
        read stays the reconciler. Returns the validated list (what the
        cache now holds), or None when the payload was unusable."""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return None
        if not isinstance(value, list):
            return None
        self._slow_read_cache[key] = value
        return value

    # --- xtl settings & station actions (entity parity) --------------------
    # Spec law: every setting writes a dedicated action (17.24-56); the props
    # themselves are read+notify. Values travel as MIoT in-param dicts via
    # _in_params (xtl silently drops raw lists).
    def _xtl_core(self) -> XtlCoreCapability:
        if not isinstance(self.core, XtlCoreCapability):
            raise ValueError(f"{self.model} does not support xtl setting actions")
        return self.core

    def _xtl_flag(self, name: str, on: bool) -> None:
        """Toggle one boolean setting: <name> carries the read prop,
        <name>_action the write channel."""
        c = self._xtl_core()
        prop = getattr(c, name, None)
        if prop is None or getattr(c, f"{name}_action", None) is None:
            raise ValueError(f"{self.model} does not support the {name} switch")
        self._action(getattr(c, f"{name}_action"), [bool(on)])
        # Firmware persists the write: the ext replay cache must agree, or a
        # core-only confirm poll would replay the pre-write value and cancel
        # the optimistic flip (see _ext_cache).
        self._ext_cache[(prop.siid, prop.piid)] = int(bool(on))

    def set_child_lock(self, on: bool) -> None:
        self._xtl_flag("child_lock", on)

    def set_disturb(self, on: bool) -> None:
        self._xtl_flag("disturb", on)

    def set_break_point(self, on: bool) -> None:
        self._xtl_flag("break_point", on)

    def set_carpet_boost(self, on: bool) -> None:
        self._xtl_flag("carpet_boost", on)

    def set_auto_drying(self, on: bool) -> None:
        self._xtl_flag("auto_drying", on)

    def set_auto_solution(self, on: bool) -> None:
        self._xtl_flag("auto_solution", on)

    def set_carpet_twice(self, on: bool) -> None:
        self._xtl_flag("carpet_twice", on)

    def set_carpet_first(self, on: bool) -> None:
        self._xtl_flag("carpet_first", on)

    def set_erp(self, on: bool) -> None:
        self._xtl_flag("erp", on)

    def set_mop_augment(self, on: bool) -> None:
        self._xtl_flag("mop_augment", on)

    def _xtl_select(self, name: str, preset: str) -> None:
        """Pick a labelled option: <name> the read prop, <name>_action the
        write channel, <name>s the label->raw table on the core."""
        c = self._xtl_core()
        prop = getattr(c, name, None)
        table = getattr(c, f"{name}s", None)
        action = getattr(c, f"{name}_action", None)
        if prop is None or action is None or not table:
            raise ValueError(f"{self.model} does not support the {name} setting")
        raw = table[preset]
        self._action(action, [raw])
        self._ext_cache[(prop.siid, prop.piid)] = raw   # see _ext_cache

    def set_carpet_prefer(self, preset: str) -> None:
        self._xtl_select("carpet_prefer", preset)

    def set_dust_collection(self, preset: str) -> None:
        self._xtl_select("dust_collection", preset)

    def set_drying_time(self, preset: str) -> None:
        self._xtl_select("drying_time", preset)

    def set_mop_wash_freq(self, preset: str) -> None:
        self._xtl_select("mop_wash_freq", preset)

    def set_mop_wash_temp(self, preset: str) -> None:
        self._xtl_select("mop_wash_temp", preset)

    # Station one-shot services: the write-only bool props 17.5/17.4/17.3
    # trigger a full dock cycle, the plugin sends `true` (spec type bool).
    def xtl_wash_mop(self) -> None:
        self._action(self._xtl_core().wash_mop_action, [True])

    def xtl_dry_mop(self) -> None:
        self._action(self._xtl_core().dry_mop_action, [True])

    def xtl_collect_dust(self) -> None:
        self._action(self._xtl_core().collect_dust_action, [True])

    # The station has no stop actions of its own: the plugin bundle shows the
    # same action fired with `false` aborts a running cycle — the station
    # buttons call setWashMop(false) / _changeDryMop(false) / setCollectDust
    # (false) while the status reads washing / HotDry+WindDry / emptying
    # (labels kw310 "Stop washing", kw311 "Stop drying", kw309 "Stop emptying").
    def xtl_stop_wash_mop(self) -> None:
        self._action(self._xtl_core().wash_mop_action, [False])

    def xtl_stop_dry_mop(self) -> None:
        self._action(self._xtl_core().dry_mop_action, [False])

    def xtl_stop_collect_dust(self) -> None:
        self._action(self._xtl_core().collect_dust_action, [False])

    # Mapping aids (no in-params).
    def xtl_fast_building(self) -> None:
        self._action(self._xtl_core().fast_building_action)

    def xtl_clean_building(self) -> None:
        self._action(self._xtl_core().clean_building_action)

    # Manual drive: control-order 0 stop, 1 forward, 2 turn left, 3 turn
    # right, 4 backward. Like the plugin's remote pad, motion persists until
    # 0 is sent.
    def xtl_manual_control(self, command: int) -> None:
        command = int(command)
        if not 0 <= command <= 4:
            raise ValueError("control-order is 0 (stop) to 4 (backward)")
        self._action(self._xtl_core().manual_control, [command])

    def xtl_note_dnd(self, payload: str) -> None:
        """Install the known-true 17.19 schedule (local write echo or cloud
        push). The robot's LOCAL get of 17.19 keeps serving the pre-write
        snapshot after a local 17.30 write sticks (live 2026-09-25: it pushed
        startHour 1 to the cloud while every read answered 23), so both the
        ext replay cache and the slow-read cache hold this truth — cadence
        reads only confirm it (see the status() cadence guard)."""
        c = self._xtl_core()
        if c.disturb_time is not None:
            self._ext_cache[(c.disturb_time.siid, c.disturb_time.piid)] = payload
            self._slow_read_cache["dnd"] = payload

    def xtl_send_dnd_time(self, payloads: list[str]) -> None:
        """17.30 set-disturb-time: compact JSON strings (xtl_dnd_payload);
        list-shaped like the other _xtl_write senders (one schedule per call)."""
        c = self._xtl_core()
        action = c.disturb_time_action
        for payload in payloads:
            self._action(action, [payload])
            # Same persist-the-write rule as _xtl_flag — and the 17.19 read
            # side goes stale after this write, so this is also the truth
            # installation (xtl_note_dnd).
            self.xtl_note_dnd(payload)

    def set_voice_language(self, preset: str) -> None:
        """17.21 switch-voice-lang: piid 61 (voice-lang-info, string) takes
        the language code as a plain numeric string — the same token the
        firmware echoes into 17.78 language-ver (live: after the app picked
        English, 78 pushed "2"). An un-installed pack can read back unchanged
        (the app installs packs server-side); the select then rolls back on
        the next poll, which is the honest outcome. Mirror the int code into
        _ext_cache for the replay (see _ext_cache)."""
        c = self._xtl_core()
        if (c.voice_language is None or c.voice_language_action is None
                or not c.voice_languages):
            raise ValueError(f"{self.model} does not support the voice language setting")
        code = c.voice_languages[preset]
        self._action(c.voice_language_action, [str(code)])
        self._ext_cache[(c.voice_language.siid, c.voice_language.piid)] = code

    # Hex alphabet for the pack-md5 guard (no re import in this module).
    _HEX_CHARS = set("0123456789abcdefABCDEF")

    def install_voice_pack(self, url: str, md5: str, language_type: int) -> None:
        """17.21 switch-voice-lang in the descriptor shape the official
        plugin's voice store uses: piid 61 receives a JSON string and the
        firmware downloads the pack itself — any reachable HTTP(S) tar works,
        the 11 built-in packs are just CDN urls (format recipe: services.yaml).
        No _ext_cache mirror: unlike the select's built-in shortcut, a custom
        pack reports back under the Id its audio.conf declares, so the next
        full poll reads the truth as-is."""
        c = self._xtl_core()
        if c.voice_language_action is None:
            raise ValueError(f"{self.model} does not support voice packs")
        if not str(url).startswith(("http://", "https://")):
            raise ValueError("url must be an http(s) address the robot can reach")
        if len(md5) != 32 or not set(md5) <= self._HEX_CHARS:
            raise ValueError("md5 must be the pack's 32-hex-char checksum")
        code = int(language_type)
        if not 1 <= code <= 11:
            raise ValueError("lang is a 1..11 language code (1 zh … 11 vi)")
        self._action(c.voice_language_action, [json.dumps(
            {"md5": md5.lower(), "url": str(url), "LanguageType": code})])

    def read_voice_state(self) -> tuple[int | None, str | None]:
        """Fresh read of the (17.25 language-voice, 17.78 language-ver)
        pair — the very pair the plugin's VoiceList reacts on to decide a
        voice-pack install's outcome (bundle audit 2026-09-25). Rides
        _batch_get_soft, so a mute chunk replays _ext_cache (which also
        receives the robot's 17.78 cloud pushes — see the __init__ hook);
        compare against a pre-write snapshot, not against empty."""
        c = self._xtl_core()
        vals = self._batch_get_soft(
            [p for p in (c.voice_language, c.language_ver) if p is not None])
        return (_as_int(vals.get(c.voice_language)),
                _as_str(vals.get(c.language_ver)))

    def reset_consumable(self, ctype: str) -> None:
        """Reset one consumable; xtl takes the TYPE STRING as sole in-param."""
        cons = self.profile.consumables
        if not isinstance(cons, XtlConsumablesCapability) or cons.reset_action is None:
            raise ValueError(f"{self.model} has no consumable-reset capability")
        self._action(cons.reset_action, [ctype])

    def clean_segments(self, room_ids: list[int | str]) -> None:
        cap = self.profile.room_clean
        if cap is None:
            raise ValueError(f"{self.model} has no room-clean capability")
        # Prefer sweep.set-room-clean: it takes map/device room ids. The
        # vacuum.start-room-sweep action wants Mijia room ids (prop 2/10), so
        # map ids sent through it fail at device level (verified on v17
        # hardware, issue #7). Room-ids must stay a CSV string — the device
        # reads an integer as empty ids, which means a full global clean.
        preferred = self.room_clean_set_params(room_ids)
        if preferred is not None:
            action, params = preferred
            self._action(action, params)
            return
        fallback = self.room_clean_start_params(room_ids)
        if fallback is not None:
            action, params = fallback
            if isinstance(self.core, XtlCoreCapability) and self.core.needs_stop_before_start:
                self._stop_and_settle()
            self._action(action, params)
            return
        raise ValueError(f"{self.model} has no usable room-clean action")

    def room_clean_start_params(self, room_ids: list[int | str]) -> tuple[object, list] | None:
        """Return the direct room-clean action and params, when available."""
        cap = self.profile.room_clean
        if cap is None or cap.start is None:
            return None
        if isinstance(cap, XtlRoomCleanCapability):
            # Same start-clean action, typed for rooms: [clean-type, ids JSON].
            return cap.start, [cap.type_room, json.dumps([int(r) for r in room_ids])]
        return cap.start, [",".join(str(r) for r in room_ids)]

    def room_clean_set_params(self, room_ids: list[int | str]) -> tuple[object, list] | None:
        """Return the set-room-clean action and params, when available."""
        cap = self.profile.room_clean
        if not (
            cap is not None
            and cap.set_room_clean is not None
            and cap.clean_room_ids is not None
            and cap.clean_room_mode is not None
            and cap.clean_room_oper is not None
        ):
            return None
        values = {
            cap.clean_room_mode.piid: 0,  # global/all rooms mode
            cap.clean_room_oper.piid: 1,  # start
            cap.clean_room_ids.piid: ",".join(str(r) for r in room_ids),
        }
        return cap.set_room_clean, [values[piid] for piid in cap.set_room_clean.in_piids]

    # --- xtl personalisation (17.53 / 17.15 / typed zone start) ------------
    def xtl_send_customization(self, payloads: list[str]) -> None:
        """17.53 customization-rooms: per-room Custom params (type 1) and the
        saved whole-home order (type 2) each travel as one compact JSON
        string; multi-chunk saves are staggered like the plugin (500 ms)."""
        cap = getattr(self.core, "customization_rooms", None)
        if cap is None:
            raise ValueError(f"{self.model} does not support room customization")
        for i, payload in enumerate(payloads):
            if i:
                time.sleep(0.5)
            self._action(cap, [payload])

    def xtl_send_room_info(self, payloads: list[str]) -> None:
        """17.15 edite-area-info: rename / re-type rooms (one JSON payload
        per call, the plugin batches rooms inside the string, not calls)."""
        cap = getattr(self.core, "edite_area_info", None)
        if cap is None:
            raise ValueError(f"{self.model} does not support room-info writes")
        for payload in payloads:
            self._action(cap, [payload])

    def start_zone_clean(self, rects: list) -> None:
        """Clean rectangles (x_min, y_min, x_max, y_max) in scene units
        (5 cm grid, the room-centre frame): typed start-clean [Zone, flat
        TL,TR,BR,BL ints]. Like room cleans, xtl wants the task closed
        first."""
        cap = self.profile.room_clean
        if not isinstance(cap, XtlRoomCleanCapability) or cap.start is None:
            raise ValueError(f"{self.model} does not support zone cleaning")
        flat = xtl_zone_flat(rects)
        if self.core.needs_stop_before_start:
            self._stop_and_settle()
        self._action(cap.start, [cap.type_zone, json.dumps(flat)])

    # --- maps ------------------------------------------------------------
    def map_list(self) -> list[dict]:
        """Return [{'name', 'id', 'cur'}...] via get-map-infos (17.13)."""
        cap = self.profile.map
        if isinstance(cap, XtlMapCapability):
            return self._xtl_map_list(cap)
        return []

    def _xtl_map_list(self, cap) -> list[dict]:
        """xtl get-map-infos (17.13) -> the shared [{name,id,cur}...] shape.

        Live firmware answers with the map-infos KS3 file name
        "uid/did/slot" (probe 2026-09-25: "1628932731/1152219162/1") — not
        a catalogue. The raw value is stashed on ``last_map_infos_file``
        for the map coordinator, which downloads that file (the plugin's
        own getMapInfos -> getMapInfosFileContent chain) to list every
        saved map; meanwhile the honest local list is that one file's map:
        uid keys the cache and the switch-map (17.9 piid 58) target, the
        slot labels it. A JSON catalogue (xtl_parse_map_infos) still wins
        if a firmware ever returns one. Positional out slots tolerated
        (map_obj_name's quirk)."""
        if cap.get_map_list is None:
            return []
        out_piid = cap.get_map_list.out_piids[0] if cap.get_map_list.out_piids else 37
        res = self._action(cap.get_map_list)
        for out in res.get("out", []) if isinstance(res, dict) else []:
            if isinstance(out, dict):
                value = out.get("value") if out.get("piid") == out_piid else None
            else:
                value = out
            if isinstance(value, str) and value.strip():
                parts = value.strip().split("/")
                if len(parts) == 3 and all(p.isdigit() for p in parts):
                    # The KS3 file name the plugin downloads for its map
                    # list — hand it to the coordinator, don't re-mint.
                    self.last_map_infos_file = value.strip()
                    return [{"name": f"Map {parts[2]}", "id": int(parts[0]),
                             "cur": 1}]
                maps = xtl_parse_map_infos(value)
                if maps:
                    return maps
                return []
        return []

    def map_obj_name(self) -> str | None:
        """xtl: run get-map-data (17.12) and read the KS3 file name off the
        out slot (piid 38). None when the answer carries no usable name.

        The name changes with every upload; a MQTT map-data-report event can
        supply it without this round-trip (see map_coordinator._xtl_obj_hint).
        """
        cap = self.profile.map
        if not isinstance(cap, XtlMapCapability) or cap.get_obj_name is None:
            raise ValueError(f"{self.model} has no xtl map-object capability")
        res = self._action(cap.get_obj_name)
        out_piid = cap.get_obj_name.out_piids[0] if cap.get_obj_name.out_piids else 38
        outs = res.get("out", []) if isinstance(res, dict) else []
        for out in outs:
            # Standard MIoT out slots are {"piid","value"} dicts; some firmware
            # revisions answer positionally (raw values in out order).
            if isinstance(out, dict):
                value = out.get("value") if out.get("piid") == out_piid else None
            else:
                value = out
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def set_current_map(self, map_id: int) -> None:
        """Switch the vacuum's active map (multi-map devices)."""
        cap = self.profile.map
        if isinstance(cap, XtlMapCapability):
            if cap.set_current_map is None:
                raise ValueError(f"{self.model} has no map-switch capability")
            self._action(cap.set_current_map, [int(map_id)])
            return
        raise ValueError(f"{self.model} has no map-switch capability")

    def get_mac(self) -> str | None:
        """Device MAC (used in the map AES key). From local miIO info()."""
        try:
            return self._dev.info().mac_address
        except Exception:  # noqa: BLE001
            return None

    def get_wifi_sn(self, user_id: str | None = None) -> str | None:
        """Serial used to seed the map AES key (siid 1, piid 5 on 2022+ models)."""
        for piid in (5, 3):
            try:
                val = self._dev.get_property_by(1, piid)[0].get("value")
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("wifi_sn: siid 1/piid %s read failed: %s", piid, err)
                continue
            if isinstance(val, str) and _is_wifi_sn(val):
                return val
            _LOGGER.debug("wifi_sn: siid 1/piid %s value %r did not match expected shape", piid, val)
        try:
            raw = self._dev.get_property_by(7, 45)[0].get("value", "")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("wifi_sn: siid 7/piid 45 fallback read failed: %s", err)
            return None
        # The raw value is a bracketed list rendered as a string (e.g.
        # '[0,0,...,"XXXX"]'), not real JSON. Strip the outer brackets first
        # so the first and last elements do not retain a stray '[' / ']'
        # that would otherwise fail the isalnum() check below (serial in
        # last list position never matched, e.g. ijai.vacuum.v10).
        for part in str(raw).strip("[]").split(","):
            # The serial sits before an optional ";<uid>" suffix on siid 7/piid 45.
            p = part.replace('"', "").split(";")[0].strip()
            if _is_wifi_sn(p) and p.isalnum():
                return p
        _LOGGER.debug("wifi_sn: siid 7/piid 45 value %r had no matching serial part", raw)
        return None


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_str(value) -> str | None:
    """Normalise a string-ish readback (17.78 answers "2", int 2 or None
    depending on the path it took) to str|None, so comparisons are stable."""
    if value is None:
        return None
    return str(value).strip()


# Sanity cap for the install pre-check download (voice packs are a couple
# of MB; the robot downloads the pack again itself, this fetch only proves
# the bytes it serves).
_VOICE_PACK_MAX_BYTES = 64 * 1024 * 1024


def fetch_voice_pack(url: str, md5: str, language_type: int) -> str | None:
    """Prove a custom voice pack from the HA side before the robot is
    asked to download it (the store channel only answers success-or-nothing
    a full 120 s later).

    Downloads the pack once and checks: the served bytes hash to the given
    md5 (catches servers that re-encode on the fly — the vendor's own
    .tar.gz naming lies: the firmware needs a PLAIN tar, gzip magic here
    means the robot's own checksum will reject it too), that it parses as
    a tar holding an audio.conf, and that the conf's Id is the language
    code we install as. Returns the Ver the robot should echo into 17.78
    language-ver once installed (None if the conf declares none). Raises
    ValueError on a definite pack defect; returns None only when the url
    is unreachable from HA (the robot's network is the judge there)."""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = resp.read(_VOICE_PACK_MAX_BYTES + 1)
    except Exception:  # noqa: BLE001 - unreachable != broken pack
        return None
    if len(data) > _VOICE_PACK_MAX_BYTES:
        raise ValueError("pack is implausibly large (>64 MB)")
    digest = hashlib.md5(data).hexdigest()
    if digest != str(md5).lower():
        raise ValueError(
            f"served bytes hash to {digest}, not the md5 given — the robot's "
            "own checksum would reject them too")
    if data[:2] == b"\x1f\x8b":
        raise ValueError(
            "pack is gzipped — the firmware needs a PLAIN tar (the vendor's "
            ".tar.gz names are a lie)")
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    except tarfile.TarError as err:
        raise ValueError(f"pack is not a readable tar: {err}") from err
    conf_raw = None
    with tar:
        for name in tar.getnames():
            if name.endswith("audio.conf"):
                handle = tar.extractfile(name)
                if handle is not None:
                    conf_raw = handle.read()
                break
    if conf_raw is None:
        raise ValueError("pack has no audio.conf (expected media/music/audio.conf)")
    try:
        conf = json.loads(conf_raw.decode("utf-8", "replace"))
    except ValueError as err:
        raise ValueError(f"audio.conf is not valid JSON: {err}") from err
    conf_id = conf.get("Id")
    if conf_id is not None and int(conf_id) != int(language_type):
        raise ValueError(
            f"audio.conf declares Id {conf_id}, not language code "
            f"{language_type} — pass lang={conf_id} to install under it")
    ver = conf.get("Ver")
    return None if ver is None else str(ver).strip()


def voice_pack_confirmed(pre: tuple, cur: tuple, code: int,
                        expected: str | None) -> bool:
    """Did the (language, pack-version) pair prove the install?

    Mirrors the plugin's VoiceList, which decides success the moment the
    robot re-reports its (language, languageVersion) pair and gives up
    after 120 s of silence. With the pack's own Ver known (fetched, or
    passed as `version`) the match is exact; otherwise any *change* of
    the pair onto the requested language counts, like the plugin."""
    if cur[0] != code:
        return False
    if expected is not None:
        return cur[1] == str(expected).strip()
    return cur != pre


# xtl 17.32 JSON item shapes -> VacuumStatus fields. Shown life = 100 - used.
_XTL_LIFE_ATTRS = {
    "sideBrush": "side_brush_life",
    "rollBrush": "main_brush_life",
    "filter": "filter_life",
    "mop": "mop_life",
    "dustbag": "dust_bag_life",
}
_XTL_LIFE_EXTRAS = {
    "mopCleaningTrough": "mop_trough_life",
    "filterScreen": "waste_filter_life",
    "engineSensor": "unit_sensor_life",
}


def _parse_xtl_consumables(raw) -> tuple[tuple[str, ...], dict[str, int], dict[str, int]]:
    """17.32 JSON -> (reported types, standard lives, station extras).

    Item shape (live-verified 2026-09-21): {"type": str, "used": 0-100,
    "mode": 0|1|2}; the app shows 100 - used as remaining life.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    lives: dict[str, int] = {}
    extras: dict[str, int] = {}
    types: list[str] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        ctype = item.get("type")
        if not isinstance(ctype, str):
            continue
        types.append(ctype)
        try:
            life = max(0, min(100, 100 - int(item.get("used", 0))))
        except (TypeError, ValueError):
            continue
        if ctype in _XTL_LIFE_ATTRS:
            lives[_XTL_LIFE_ATTRS[ctype]] = life
        elif ctype in _XTL_LIFE_EXTRAS:
            extras[_XTL_LIFE_EXTRAS[ctype]] = life
    return tuple(types), lives, extras
