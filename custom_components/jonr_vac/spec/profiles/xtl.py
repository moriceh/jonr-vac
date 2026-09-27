"""xtl (JONR) runtime profiles.

Protocol source: public MIOT-SPEC urn:miot-spec-v2:device:vacuum:0000A006:
xtl-xm2216:4, cross-checked against the reverse-engineered Mi Home plugin
(propertyCodes/actionCodes) and live local-MiIO probes (2026-09-21: status
siid2/piid2 values, 17.32 consumables JSON shape, message/error codes all
verified on hardware).

Device quirks encoded here (see XtlCoreCapability docstring):
- fan/water/mode/count/route properties are read+notify ONLY; every gear
  change is a dedicated action (17.26/27/46/28/50).
- start-clean (17.1) takes [clean-type(8), clean-values(34)] and is ignored
  while a task is open -> stop-clean first + settle.
- pause/resume is one pause-continue-work action (17.2) driven by the
  robot-set-status code 7/8.
- maps: get-map-data (17.12, out piid 38) / get-map-infos (17.13, out piid
  37) return KS3 *file names* — the raster itself lives on the Mi cloud and
  is fetched + decoded by the map pipeline (xtl_map.py), never locally.
  Fresh uploads additionally push MIoT events siid 17 eiid 1
  (map-data-report) whose payload carries the same file name.
"""
from __future__ import annotations

import json

from ..types import (
    Action,
    ModelProfile,
    Prop,
    XtlConsumablesCapability,
    XtlCoreCapability,
    XtlMapCapability,
    XtlRoomCleanCapability,
)

# siid 2 / piid 2 status value-list (spec v4) -> HA activity.
XTL_STATUS_MAP = {
    1: "idle",        # Idle
    2: "idle",        # Busy
    3: "idle",        # Delay
    4: "cleaning",    # Mopping
    5: "cleaning",    # Sweeping and Mopping
    6: "paused",      # Paused
    7: "cleaning",    # Sweeping
    8: "error",       # Error
    9: "docked",      # Charging
    10: "returning",  # Go Charging
    11: "docked",     # BreakCharging
    12: "returning",  # GoWash
    13: "docked",     # Charged
    14: "idle",       # BuildingMap
    15: "idle",       # Updating
    16: "idle",       # Sleeping
    17: "localizing", # Relocation — self-localizing at task start (map pin)
    18: "docked",     # StationWorking
    19: "paused",     # MappingPause
    20: "returning",  # GoChargeBreak
    21: "returning",  # WashBreak
    22: "idle",       # Linking Device
    23: "returning",  # GoDust
}

XTL_XM2216_CORE = XtlCoreCapability(
    status=Prop(2, 2),           # status — 23-value enum, live-verified (13=Charged)
    fault=Prop(17, 35),          # error — uint16 push code
    battery=Prop(16, 1),         # battery-level
    fan_speed=Prop(17, 13),      # fan-mode (read-only; change via 17.26)
    water_level=Prop(17, 12),    # water-mode (read-only; change via 17.27)
    mode=Prop(17, 10),           # clean-mode / work mode (read-only; change via 17.46)
    start=Action(17, 1, in_piids=(8, 34)),   # start-clean [clean-type, values JSON]
    stop=Action(17, 3),          # stop-clean — real stop, closes the task
    pause=Action(17, 2, in_piid=55),  # pause-continue-work [robot-set-status]
    charge=Action(16, 1),        # battery start-charge — return to dock
    locate=Action(17, 4),        # seek-robot
    status_map=XTL_STATUS_MAP,
    modes={"both": 0, "sweep": 1, "mop": 2, "sweep_then_mop": 3, "custom": 4},
    fan_speeds={"quiet": 0, "standard": 1, "strong": 2, "turbo": 3},
    water_levels={"low": 0, "mid": 1, "high": 2},
    fan_action=Action(17, 26, in_piid=13),
    water_action=Action(17, 27, in_piid=12),
    mode_action=Action(17, 46, in_piid=10),
    count_action=Action(17, 28, in_piid=14),
    route_action=Action(17, 50, in_piid=65),
    count=Prop(17, 14),
    route=Prop(17, 65),
    message=Prop(17, 36),
    station_error=Prop(17, 79),
    clean_type_status=Prop(17, 9),   # RobotStatus — whole/room/zone task (tips)
    robot_status=Prop(17, 49),       # robot-status (string, read+notify) —
    # plugin curRobotStatus (WashMop/HotDry/WindDry/ClctDust/Charging/...).
    # NB: aiid 49 (manual-control) shares this number — Prop vs Action, no
    # collision in the spec, but don't confuse the two when wiring.
    clean_values=Prop(17, 34),       # clean-values JSON — task targets (tips)
    customization_rooms=Action(17, 53, in_piid=70),  # per-room Custom params
    # (type 1) + saved whole-home order (type 2); payload = compact JSON string
    edite_area_info=Action(17, 15, in_piid=60),      # room rename / category
    # --- entity parity (spec v4 siid 17): prop = read+notify, action = write ---
    # Live-verified prop values (2026-09 state dump): volume 79 (0-100),
    # disturb-time JSON, carpet_prefer 0, dust_collection 1, drying_time 120,
    # mop_wash_frequency 5.
    volume=Prop(17, 15),
    volume_action=Action(17, 31, in_piid=15),
    child_lock=Prop(17, 6),
    child_lock_action=Action(17, 36, in_piid=6),
    disturb=Prop(17, 1),
    disturb_action=Action(17, 24, in_piid=1),
    break_point=Prop(17, 2),
    break_point_action=Action(17, 25, in_piid=2),
    carpet_boost=Prop(17, 7),
    carpet_boost_action=Action(17, 34, in_piid=7),
    auto_drying=Prop(17, 21),
    auto_drying_action=Action(17, 48, in_piid=21),
    auto_solution=Prop(17, 44),
    auto_solution_action=Action(17, 39, in_piid=44),
    carpet_twice=Prop(17, 74),
    carpet_twice_action=Action(17, 55, in_piid=74),
    carpet_first=Prop(17, 75),
    carpet_first_action=Action(17, 56, in_piid=75),
    erp=Prop(17, 42),
    erp_action=Action(17, 38, in_piid=42),
    mop_augment=Prop(17, 43),
    mop_augment_action=Action(17, 35, in_piid=43),
    carpet_prefer=Prop(17, 11),
    carpet_prefer_action=Action(17, 33, in_piid=11),
    carpet_prefers={"adaptive": 0, "evade": 1, "carpet_only": 2, "ignore": 3},
    dust_collection=Prop(17, 20),
    dust_collection_action=Action(17, 32, in_piid=20),
    # Only three of the firmware's five enum members are reachable — the
    # plugin's AutomaticDustCollection page offers exactly
    # {None=0, MediumFrequency=3, EveryTime=1} (LowFrequency=2/
    # HighFrequency=4 are dead members no picker ever writes; live 2026-09-25
    # the app never showed them either). MediumFrequency = every 5 cleans.
    dust_collections={"none": 0, "every_clean": 1, "medium": 3},
    drying_time=Prop(17, 22),
    drying_time_action=Action(17, 43, in_piid=22),
    drying_times={"120": 120, "180": 180, "240": 240},   # minutes (spec value-list)
    mop_wash_freq=Prop(17, 23),
    mop_wash_freq_action=Action(17, 44, in_piid=23),
    mop_wash_freqs={"5": 5, "10": 10, "15": 15},         # minutes
    mop_wash_temp=Prop(17, 45),
    mop_wash_temp_action=Action(17, 45, in_piid=45),
    # No mop_wash_temps table: the xm2216 exposes 17.45 but the firmware
    # never lets the user set the wash temperature — no select entity.
    disturb_time=Prop(17, 19),
    disturb_time_action=Action(17, 30, in_piid=18),
    clean_timers=Prop(17, 17),          # timer-list JSON (read-only sensor)
    voice_language=Prop(17, 25),
    language_ver=Prop(17, 78),           # language-ver: version of the
    # installed pack — the plugin's VoiceList reacts on (language, this)
    # to decide a pack install succeeded (bundle audit 2026-09-25; live
    # probe read "2" = the built-in FR pack's audio.conf Ver).
    voice_language_action=Action(17, 21, in_piid=61),
    voice_languages={
        "chinese": 1, "english": 2, "russian": 3, "german": 4,
        "italian": 5, "french": 6, "polish": 7, "spanish": 8,
        "korean": 9, "chinese_tw": 10, "vietnamese": 11,
    },
    session_time=Prop(17, 26),
    session_area=Prop(17, 27),
    total_time=Prop(17, 31),
    total_area=Prop(17, 29),
    total_count=Prop(17, 30),
    clean_records=Prop(17, 46),
    clean_water_cistern=Prop(17, 51),
    drain_cistern=Prop(17, 52),
    dust_bag_state=Prop(17, 53),
    mop_tank_state=Prop(17, 54),
    return_status=Prop(17, 48),
    wash_mop_action=Action(17, 40, in_piid=5),
    dry_mop_action=Action(17, 41, in_piid=4),
    collect_dust_action=Action(17, 42, in_piid=3),
    fast_building_action=Action(17, 10),
    clean_building_action=Action(17, 11),
    manual_control=Action(17, 49, in_piid=62),
    counts={"once": 1, "twice": 2},
    routes={"fast": 0, "daily": 1, "fine": 2},
    start_in=(1, "[]"),          # full-area clean
    resume_in=(8,),
    pause_in=(7,),
    needs_stop_before_start=True,
)

XTL_XM2216_ROOM_CLEAN = XtlRoomCleanCapability(
    start=Action(17, 1, in_piids=(8, 34)),
)

# --- personalisation payload builders (17.53 / 17.15 / zone start) ----------
# Pure JSON-string builders for the write channels; shapes are verbatim from
# the plugin's CustomizedParameters / CustomizedOrder screens and the
# zoneClean call site (bundle v1730143, content-verified 2026-09-22).

# The plugin's "restore default" push (defaultCleaningModeParameters):
# BothWork / Auto fan / Mid water / One pass / Daily route.
XTL_ROOM_DEFAULTS = {"K": 0, "F": 1, "W": 1, "C": 1, "D": 1}


def xtl_preference_payloads(map_id: int, prefs: list[dict]) -> list[str]:
    """17.53 type-1: [{I, K work, F fan, W water, C count, D drag, R room}]
    chunked 5 rooms per action (the plugin's splitArray); total = chunk
    count, index 0-based — chunks send in order, ~500 ms apart. An empty
    room list sends nothing (the UI never splits one; callers guard)."""
    chunks = [prefs[i:i + 5] for i in range(0, len(prefs), 5)]
    total = len(chunks)
    return [
        json.dumps({"total": total, "index": idx,
                    "customdata": {"type": 1, "data": [
                        {"I": int(map_id), **p} for p in chunk]}},
                   separators=(",", ":"))
        for idx, chunk in enumerate(chunks)
    ]


def xtl_sequence_payload(map_id: int, room_ids: list) -> str:
    """17.53 type-2: the saved whole-home cleaning order — a flat id list in
    execution order, [] clears it. mapid lives INSIDE customdata here (type 1
    carries I per item); verbatim plugin asymmetry."""
    return json.dumps({"total": 1, "index": 0,
                       "customdata": {"type": 2, "mapid": int(map_id),
                                      "data": [int(r) for r in room_ids]}},
                      separators=(",", ":"))


def xtl_room_info_payload(map_id: int, rooms: list[dict]) -> str:
    """17.15 area-info: [{"roomId", "name", "category"}] — the plugin always
    sends name+category together, so pre-merge the untouched field."""
    return json.dumps({"mapId": int(map_id), "rooms": rooms},
                      separators=(",", ":"))


def xtl_dnd_payload(start_hour: int, start_min: int, end_hour: int, end_min: int,
                    no_dust: bool = False, no_dry: bool = False) -> str:
    """17.30 set-disturb-time: one compact JSON string, key order and shape
    verbatim from the plugin's paramStr (and the live 17.19 read-back:
    '{"startHour":22,"startMin":0,"endHour":8,"endMin":0,"notDust":0,"notDry":0}').
    notDust/notDry (no dock dust-collection / drying during DND) are honoured
    on firmware >= 439.681, silently ignored below it."""
    return json.dumps({"startHour": int(start_hour), "startMin": int(start_min),
                       "endHour": int(end_hour), "endMin": int(end_min),
                       "notDust": 1 if no_dust else 0,
                       "notDry": 1 if no_dry else 0},
                      separators=(",", ":"))


def xtl_dnd_label(raw) -> str | None:
    """Display text for the 17.19 disturb-time JSON, the plugin's
    `formatTime(start)+\" - \"+formatTime(end)`. None when unusable."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    try:
        sh, sm = int(raw["startHour"]), int(raw["startMin"])
        eh, em = int(raw["endHour"]), int(raw["endMin"])
    except (KeyError, TypeError, ValueError):
        return None
    return f"{sh:02d}:{sm:02d} - {eh:02d}:{em:02d}"


def xtl_parse_dnd(raw) -> dict | None:
    """17.19 disturb-time JSON -> dict (None when unusable)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def xtl_dnd_merged(raw, *, start_hour: int | None = None,
                   start_min: int | None = None, end_hour: int | None = None,
                   end_min: int | None = None,
                   no_dust: bool | None = None,
                   no_dry: bool | None = None) -> str:
    """Compact 17.18 write payload: the current 17.19 read-back with only the
    given fields replaced, so editing the window keeps the flags and vice
    versa. No (or broken) read-back yet -> the app's shipped defaults
    (22:00-08:00, both flags off). Key order/shape = xtl_dnd_payload."""
    cur = xtl_parse_dnd(raw) or {}

    def pick(key: str, new, default: int) -> int:
        if new is not None:
            return int(new)
        try:
            return int(cur.get(key, default))
        except (TypeError, ValueError):
            return default

    return json.dumps({
        "startHour": pick("startHour", start_hour, 22),
        "startMin": pick("startMin", start_min, 0),
        "endHour": pick("endHour", end_hour, 8),
        "endMin": pick("endMin", end_min, 0),
        "notDust": pick("notDust", None if no_dust is None else int(no_dust), 0),
        "notDry": pick("notDry", None if no_dry is None else int(no_dry), 0),
    }, separators=(",", ":"))


def xtl_dnd_same(a, b) -> bool:
    """True when two 17.19 schedule payloads (dict or JSON string) describe
    the same window/flags — compared as PARSED dicts, so key order and
    spacing differences never look like a change. False when either side is
    unusable (callers decide what an unknown means)."""
    pa, pb = xtl_parse_dnd(a), xtl_parse_dnd(b)
    return pa is not None and pb is not None and pa == pb


def xtl_dnd_from_push(value) -> str | None:
    """17.19 properties-changed payload -> the canonical compact write shape.
    The broker hands the schedule over as an already-parsed dict (live probe
    2026-09-25: the properties_changed frames arrive decoded), but a raw JSON
    string form is tolerated; anything unusable gives None (fold skipped)."""
    parsed = xtl_parse_dnd(value)
    if parsed is None:
        return None
    return xtl_dnd_merged(parsed)


def xtl_parse_map_infos(raw) -> list[dict]:
    """17.13 get-map-infos -> [{name,id,cur}...] (the shape the map plumbing
    eats, like the ijai/viomi get-map-list). The payload is a JSON string;
    accept a bare list, or a dict wrapping the list under any key. Key
    aliases are tolerated (mapId/id, mapName/name/title, cur/current/...)
    until a live probe pins the exact firmware spelling — a list-shaped
    payload with no id key still yields []."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if isinstance(raw, dict):
        raw = next((v for v in raw.values() if isinstance(v, list)), None)
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        mid = next((m[k] for k in ("id", "mapId", "map_id")
                    if m.get(k) is not None), None)
        if mid is None:
            continue
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            continue
        name = next((str(m[k]) for k in ("name", "mapName", "map_name", "title")
                     if m.get(k)), None)
        cur = next((m[k] for k in ("cur", "current", "isCurrent", "is_current",
                                   "curMap")
                    if m.get(k) is not None), 0)
        try:
            cur = int(bool(int(cur)))
        except (TypeError, ValueError):
            cur = int(bool(cur))
        out.append({"name": name, "id": mid, "cur": cur})
    return out


def xtl_parse_map_infos_file(raw) -> dict[int, str]:
    """map-infos KS3 file (pushed as siid 17 eiid 2) -> {mapId: user name}.

    The app's map titles live HERE, not in the local 17.13 answer (decompiled
    plugin: setMapInfos reads [{mapId, name, saved, status, …}] and titles
    the list with `name`, falling back to "Carte {mapId}"). Junk shape = {}
    — names are a display enhancement, never fatal. Empty/blank names drop
    out so the caller's placeholder survives."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", "replace")
        except (UnicodeError, ValueError):
            return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if isinstance(raw, dict):
        raw = next((v for v in raw.values() if isinstance(v, list)), None)
    if not isinstance(raw, list):
        return {}
    out: dict[int, str] = {}
    for m in raw:
        if not isinstance(m, dict):
            continue
        try:
            mid = int(m.get("mapId", m.get("map_id")))
        except (TypeError, ValueError):
            continue
        name = m.get("name")
        if isinstance(name, str) and name.strip():
            out[mid] = name.strip()
    return out


def xtl_parse_map_infos_catalog(raw) -> list[dict]:
    """map-infos KS3 file -> the switchable [{name,id,cur}...] catalogue.

    The Active Map select bug (2026-09-26, "only the current map shows"):
    xtl's list was synthesised from the decoded blob alone, so the OTHER
    saved maps never appeared — while Mi Home lists them from exactly this
    file (decompiled plugin: mapStore.mapInfos, filtered to saved===1,
    labelled by `name`, with status===1 as the in-use one). Returns the
    plugin's own view: saved maps (the field's absence counts as saved),
    mapId 0 dropped (that's the unsaved live map, covered by the blob's
    own id), cur from status===1, blank names kept as None so the caller's
    "Map N" placeholder survives. Junk shape = [] — the caller keeps its
    last good list."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", "replace")
        except (UnicodeError, ValueError):
            return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if isinstance(raw, dict):
        raw = next((v for v in raw.values() if isinstance(v, list)), None)
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        try:
            mid = int(m.get("mapId", m.get("map_id")))
        except (TypeError, ValueError):
            continue
        if mid == 0:
            continue  # the plugin's unsaved live map (curMapInfo.mapId != 0)
        saved = m.get("saved")
        if saved is not None:
            try:
                if int(saved) != 1:
                    continue  # still-being-built map, not a saved floor
            except (TypeError, ValueError):
                continue
        name = m.get("name")
        status = m.get("status")
        try:
            cur = 1 if int(status) == 1 else 0
        except (TypeError, ValueError):
            cur = 0
        out.append({"name": name.strip() if isinstance(name, str) and name.strip()
                    else None, "id": mid, "cur": cur})
    return out


def xtl_add_known_maps(maps_meta: list[dict], catalog: list[dict]) -> list[dict]:
    """Append the catalogue's saved maps the blob-synthesised list misses.

    xtl's maps_meta is synthesised from the CURRENT decoded blob, so the
    other saved floors (what Mi Home's map manager lists from this very
    file) vanished from the Active Map select (bug 2026-09-26: "only the
    current map shows"). Ids already listed pass through untouched (dedup).

    The blob's embedded id is the LIVE working copy (mapHeadId 0/1 even
    while a saved map runs). The blob row joins as saved=0 — never a switch
    target (tapping its "Map N" ghost blanked the robot's live map,
    2026-09-26); every row the catalogue vouches for is marked saved=1, and
    a blob row the catalogue claims (mapHeadId == that floor, the plugin's
    isMapSaved same reading) is promoted to saved=1 too. The robot can also
    PERSIST its draft into the file (saved=1, blank name, mapId 1) — that
    row is un-selectable downstream (xtl_selectable_maps), not here.

    cur: the catalogue's status===1 row (parsed to cur=1) owns it — even
    over a claimed blob row (live 2026-09-26 15:41: the robot had
    self-persisted its draft as mapId 1 and uploaded the blob under that
    id; only mapId 3's status==1 said which floor actually ran). No status
    flag anywhere and exactly ONE saved map: that map owns cur, unless the
    blob already claims it. Everything else keeps the blob's cur=1."""
    if not catalog:
        return maps_meta
    catalog_ids = {m["id"] for m in catalog if isinstance(m.get("id"), int)}
    known = {int(m["id"]) for m in maps_meta
             if isinstance(m, dict) and m.get("id") is not None}
    # A saved map the catalogue flags in-use (status===1, parsed to cur=1),
    # read BEFORE the append below forces every joined row to cur=0. That
    # explicit flag owns cur unconditionally — even over a blob row the file
    # claims (live 2026-09-26 15:41: the robot had just self-persisted its
    # draft as mapId 1 "saved:1" and uploaded the blob under mapHeadId 1;
    # only mapId 3's status==1 says which floor actually runs). Without a
    # flag: the xm2216 sometimes writes no status==1 at all (live dump
    # 2026-09-26 noon: the sole saved row was status 0 while the robot ran
    # on it), so with exactly ONE saved map that map is the floor — but
    # never over a row the blob itself claims (that claim IS the floor).
    user_id = next((m["id"] for m in catalog
                    if m.get("cur") == 1 and isinstance(m.get("id"), int)
                    and m["id"] != 0), None)
    sole = None
    if user_id is None:
        saved_ids = [m["id"] for m in catalog
                     if isinstance(m.get("id"), int) and m["id"] != 0]
        if len(saved_ids) == 1:
            sole = saved_ids[0]
    out = [{**m, "saved": 1} if isinstance(m.get("id"), int)
           and m["id"] in catalog_ids else m for m in maps_meta]
    for m in catalog:
        mid = m.get("id")
        if isinstance(mid, int) and mid not in known:
            out.append({**m, "cur": 0, "saved": 1})
            known.add(mid)

    def _curate(rows, owner, claimed):
        if any(m.get("cur") == 1 and m.get("id") != owner
               and (claimed is None or m.get("id") not in claimed)
               for m in rows):
            rows = [{**m, "cur": 1} if m.get("id") == owner
                    else {**m, "cur": 0}
                    for m in rows]
        return rows

    # Blob rows the file CLAIMS (mapHeadId equals a catalogue floor): that
    # row IS the floor and keeps cur against the sole-saved-map guess.
    claimed = {m["id"] for m in maps_meta if isinstance(m.get("id"), int)
               and m["id"] in catalog_ids} - {sole}
    if user_id is not None:
        out = _curate(out, user_id, None)
    elif sole is not None:
        out = _curate(out, sole, claimed)
    return out


def xtl_selectable_maps(maps_meta: list[dict]) -> list[dict]:
    """The rows the Active Map select may offer — the plugin's rule.

    Saved means switchable, exactly what the official plugin's switchMap
    filter accepts (mapInfos.filter(saved===1)). A user-created map that
    was never titled shows in Mi Home under the default label ("Map 1",
    keyword235+mapId) and IS a real floor — the user's second card
    (live 2026-09-26) — so a blank name is not disqualifying. Only the
    unsaved working copy (saved=0, the blob-synthesised row) can blank
    the robot's map when switched to, and never enters the list.
    Cold-start guard: with no saved row at all (catalogue never read,
    only the saved=0 blob draft) the draft stays as the sole option — the
    honest single-map truth until the file says otherwise.
    """
    switchable = [
        m for m in maps_meta
        if isinstance(m, dict) and m.get("saved") == 1
    ]
    return switchable if switchable else maps_meta


def xtl_apply_map_names(maps_meta: list[dict], names: dict[int, str]) -> list[dict]:
    """Overlay user-given map names onto the synthesised [{name,id,cur}...].
    Matches on the physical mapId; a placeholder ("Map 3"/None) is replaced,
    any other name kept, and maps the file doesn't know pass through."""
    if not names:
        return maps_meta
    out = []
    for m in maps_meta:
        name = names.get(m.get("id")) if isinstance(m, dict) else None
        current = m.get("name") if isinstance(m, dict) else None
        if name and (not current or str(current).startswith("Map ")):
            out.append({**m, "name": name})
        else:
            out.append(m)
    return out


def xtl_parse_records(raw) -> list:
    """17.46 clean-records JSON -> list of record dicts ([] when malformed)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return raw if isinstance(raw, list) else []


def xtl_records_count(raw) -> int | None:
    """Cleaning-records sensor state: len of the reported list, None when the
    robot has never answered 17.46 at all (live 2026-09-25: the P20 Pro's
    local get returns null for it and it never rides a push — the plugin's
    own history screen feeds the same prop, so an empty store there is the
    firmware's truth). None -> sensor `unknown`, which is honest; a plain 0
    would claim the robot reported an empty history when it reported
    nothing."""
    return None if raw is None else len(xtl_parse_records(raw))


# Timer repeat bitmask layout, cross-checked against the Mi Home screen
# 2026-09-25: LAST char = Sunday (… "11111100" = lun→ven, "11010100" =
# lun/mer/ven), char 1 = Monday, and the LEADING char is the recurring
# flag — '1' weekly, '0' for one-shot timers (live "unique" timer created
# Friday at 22:08 read repeat "00000100": flag 0 + the run's weekday).

# Timer-summary labels: every human-visible word the one-line summary
# renders comes from a table (never hardcoded), so the sensor can hand
# xtl_timers_summary the translation of the user's HA language. The defaults
# below are the English table; translations/*.json carry the rest (the
# French table lives in translations/fr.json, key
# entity.sensor.clean_timers.state — see sensor._timer_labels). Setting
# codes were calibrated 2026-09-25 against the user's two live Mi Home
# plannings (workMode 1 = vacuum only / 0 = vacuum+mop; routePrefer 1 =
# daily = the plugin's default, so only 0/2 earn a clause).
XTL_TIMER_LABELS_EN = {
    "days": ("mon", "tue", "wed", "thu", "fri", "sat", "sun"),
    "every_day": "every day",
    "once_prefix": "one-time ",
    "whole_home": "whole home",
    "map_prefix": "map ",
    "rooms_prefix": "rooms ",
    "passes": "{n} passes",
    "disabled": "(disabled)",
    "work": {"0": "vacuum+mop", "1": "vacuum only", "2": "mop only"},
    "fan": {"0": "quiet", "1": "standard", "2": "strong", "3": "turbo"},
    "water": {"0": "low wetness", "1": "medium wetness", "2": "high wetness"},
    "route": {"0": "quick route", "2": "thorough route"},
}


def xtl_parse_timers(raw) -> list:
    """17.17 timer-list JSON -> list of timer dicts ([] when malformed)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return raw if isinstance(raw, list) else []


def _xtl_repeat_days(bits: str, labels=XTL_TIMER_LABELS_EN) -> str:
    """Repeat bitmask -> day label ('mon→fri', 'mon/wed/fri', ...).

    Chars 1..7 are Monday..Sunday; char 0 is the reserved flag (skipped).
    The day words come from *labels* (see XTL_TIMER_LABELS_EN / the sensor's
    i18n injection)."""
    days = [i for i, ch in enumerate(bits[1:8]) if ch == "1"]
    if not days:
        return "?"
    if days == list(range(7)):
        return labels["every_day"]
    names = labels["days"]
    if len(days) > 1 and days == list(range(days[0], days[-1] + 1)):
        return f"{names[days[0]]}→{names[days[-1]]}"
    return "/".join(names[i] for i in days)


def _xtl_map_label(map_names, map_id):
    """A timer's mapId -> the user's map title ("Appart Brest"), or None
    when the name is unknown (whole-home timers ride on no map at all).

    *map_names* is {mapId: name} as built by the timer sensor from the map
    coordinator's catalogue (the same rows the Active Map select lists, so
    "Map 1" is this map's Mi Home label when untitled)."""
    if not isinstance(map_names, dict) or map_id is None:
        return None
    name = map_names.get(map_id)
    if name is None:
        try:
            name = map_names.get(int(map_id))
        except (TypeError, ValueError):
            name = map_names.get(str(map_id))
    return str(name) if name else None


def _xtl_room_labels(rooms, map_id, values: list) -> list[str]:
    """cleanValues room ids -> Mi Home room names, numeric fallback.

    *rooms* is {mapId: {roomId: name}} as built by the timer sensor from the
    map coordinator's vector 'rooms' lists; None (no map cached yet, or the
    unit-only test path) keeps the bare ids. The timer's own mapId bucket
    wins over other maps' on id collisions."""
    if not isinstance(rooms, dict):
        return [str(v) for v in values]
    lookup: dict = {}
    for bucket in rooms.values():
        if isinstance(bucket, dict):
            lookup.update(bucket)
    own = rooms.get(str(map_id)) if map_id is not None else None
    if isinstance(own, dict):
        lookup.update(own)
    return [str(lookup.get(str(v), v)) for v in values]


def xtl_timers_summary(raw, rooms=None, labels=None, map_names=None) -> str | None:
    """One-line human summary of the 17.17 timer JSON (None when unusable).

    Example: 'mon→fri 10:00 (whole home, vacuum only, turbo) ·
    mon/wed/fri 11:00 (map Appart Brest, rooms 3/5/6, vacuum+mop, standard,
    medium wetness) (disabled)'. *rooms* ({mapId: {roomId: name}}) turns the
    type-2 room ids into Mi Home names; ids show when absent. *map_names*
    ({mapId: title}, the Active Map select's catalogue) names the map a
    room/zone timer runs on — Mi Home's timer editor shows it, so the HA
    read-out should too (user request 2026-09-26); whole-home timers ride on
    no map and stay unnamed. *labels* overrides the whole
    visible vocabulary (see XTL_TIMER_LABELS_EN; the sensor injects the
    translation of the user's HA language). Only non-default settings earn a
    clause (daily route / single pass are hidden); the raw JSON rides on the
    sensor attributes.
    """
    labels = labels or XTL_TIMER_LABELS_EN
    timers = [t for t in xtl_parse_timers(raw) if isinstance(t, dict)]
    parts = []
    for t in timers:
        try:
            hh, mm = int(t["hour"]), int(t["min"])
        except (KeyError, TypeError, ValueError):
            continue
        bits = str(t.get("repeat", ""))
        once = labels["once_prefix"] if bits[:1] == "0" else ""
        label = f"{once}{_xtl_repeat_days(bits, labels)} {hh:02d}:{mm:02d}"
        clauses = []
        values = t.get("cleanValues")
        if str(t.get("type")) == "2" and isinstance(values, list) and values:
            map_name = _xtl_map_label(map_names, t.get("mapId"))
            if map_name:
                clauses.append(labels["map_prefix"] + map_name)
            clauses.append(labels["rooms_prefix"] + "/".join(
                _xtl_room_labels(rooms, t.get("mapId"), values)))
        elif str(t.get("type")) == "1":
            clauses.append(labels["whole_home"])
        work = str(t.get("workMode", ""))
        if work in labels["work"]:
            clauses.append(labels["work"][work])
        fan = str(t.get("fanMode", ""))
        if fan in labels["fan"]:
            clauses.append(labels["fan"][fan])
        if work in ("0", "2"):
            water = str(t.get("waterMode", ""))
            if water in labels["water"]:
                clauses.append(labels["water"][water])
        try:
            count = int(t.get("cleanCount", 1))
        except (TypeError, ValueError):
            count = 1
        if count > 1:
            clauses.append(labels["passes"].format(n=count))
        route = str(t.get("routePrefer", ""))
        if route in labels["route"]:
            clauses.append(labels["route"][route])
        if clauses:
            label += " (" + ", ".join(clauses) + ")"
        if str(t.get("on", "1")) != "1":
            label += " " + labels["disabled"]
        parts.append(label)
    return " · ".join(parts) if parts else None


def xtl_zone_flat(rects: list) -> list[int]:
    """Zone-clean values (17/34 shape): 8 ints per rect — TL, TR, BR, BL
    corners in scene units (5 cm grid, the room-centre frame), matching the
    plugin's pointsArray and cleanValuesConvertZoning round-trip."""
    flat: list[int] = []
    for rect in rects:
        x1, y1, x2, y2 = (int(v) for v in rect)
        if not (x2 > x1 and y2 > y1):
            raise ValueError("zone rects are (x_min, y_min, x_max, y_max)")
        flat += [x1, y1, x2, y1, x2, y2, x1, y2]
    if not flat:
        raise ValueError("at least one zone is required")
    return flat


XTL_XM2216_CONSUMABLES = XtlConsumablesCapability(
    consumables=Prop(17, 32),
    reset_action=Action(17, 20, in_piid=33),
)

XTL_XM2216_MAP = XtlMapCapability(
    get_obj_name=Action(17, 12, out_piids=(38,)),  # get-map-data -> KS3 file name
    # Multi-map parity with the app: get-map-infos (17.13) answers the map
    # catalogue as a JSON string on out piid 37, switch-map (17.9) makes one
    # current via in piid 58. Feeds the shared "Active Map" select plumbing.
    get_map_list=Action(17, 13, out_piids=(37,)),
    set_current_map=Action(17, 9, in_piid=58),
)

# Push-code texts (17.35 robot error / 17.79 station error share the code
# space; 17.36 message). Live-verified 2026-09-21: message 2008 raised while
# engineSensor reported used=100; station_error 4502 while dock low on water.
XTL_ERROR_CODES = {
    0: "",
    4001: "Dust box not installed",
    4003: "Roller brush entangled",
    4004: "Side brush entangled",
    4005: "Drive wheel blocked",
    4006: "Drop sensor accumulated dust",
    4007: "Laser sensor obstructed",
    4008: "Laser sensor radar blocked",
    4009: "Drive wheel slipping",
    4011: "Laser sensor cover jammed",
    4012: "Robot stuck",
    4013: "Collision sensor jammed",
    4014: "Robot suspended",
    4016: "Robot tilted",
    4017: "Unable to start in restricted zone",
    4018: "Unable to start on carpet",
    4020: "Mop pad holder entangled",
    4021: "Mop pad holder not installed",
    4501: "Dust bag not installed",
    4502: "Insufficient water / clean tank not installed",
    4503: "Dirty water full / tank not installed",
    4506: "Mop cleaning slot full or not installed",
    4507: "Mop cleaning slot full or not installed",
    4901: "Unable to auto refill water",
    4902: "Unable to auto drain water",
    4903: "Insufficient cleaning solution",
}

# Option ids (snake_case) for the ENUM error sensors; the state labels ride
# translations/*.json (entity.sensor.<key>.state), the keys never change.
XTL_ERROR_CODE_KEYS = {
    4001: "dust_box_missing",
    4003: "roller_brush_entangled",
    4004: "side_brush_entangled",
    4005: "drive_wheel_blocked",
    4006: "drop_sensor_dirty",
    4007: "laser_obstructed",
    4008: "laser_radar_blocked",
    4009: "drive_wheel_slipping",
    4011: "laser_cover_jammed",
    4012: "robot_stuck",
    4013: "collision_sensor_jammed",
    4014: "robot_suspended",
    4016: "robot_tilted",
    4017: "restricted_zone_start",
    4018: "carpet_start",
    4020: "mop_holder_entangled",
    4021: "mop_holder_missing",
    4501: "dust_bag_missing",
    4502: "water_low",
    4503: "dirty_water_full",
    4506: "mop_slot_full",
    4507: "mop_slot_full_refill_kit",
    4901: "refill_failed",
    4902: "drain_failed",
    4903: "solution_low",
}
# Official-picture per fault code — the `faultImages` table of the official
# Mi Home JONR plugin's config.json (data/1152219162/files/config.json,
# CDN xtl-data-sg.ks3-sgp.ksyuncs.com/p20/images/faultImages/), downloaded
# verbatim into www/images/faults/ and served from the integration's own
# static path (const.FAULT_IMAGE_URL). Error bubbles append the matching
# picture, like dreame-vacuum does. Filenames keep the vendor's spelling
# (typos included) so they stay diffable against config.json. 4902/4903 are
# crossed on the vendor side — the plugin labels 4902 "drain" yet ships
# InsufficientCleaningSolution for it — our own label tables stay correct
# (4902 drain_failed / 4903 solution_low); the image follows the plugin's
# own code->file mapping, crossed as shipped. 4002 exists plugin-side only
# (no error-label entry above): the robot never reports it, but Mi Home
# draws it, so the picture ships too.
XTL_ERROR_IMAGES: dict[int, str] = {
    4001: "DustBoxNotInstalled.png",
    4002: "NoWaterTankInstalled.png",
    4003: "TheRollingBrushIsEntangled.png",
    4004: "EdgeBrushEntangled.png",
    4005: "DriveWheelStuck.png",
    4006: "DownwardGrayAccumulation.png",
    4007: "LDSBeingObstructed.png",
    4008: "LdsStuck.png",
    4009: "DriveWheelSlipping.png",
    4011: "RadarHoodStuck.png",
    4012: "RobotTrapped.png",
    4013: "TheCollisionBoardIsStuck.png",
    4014: "RobotsSuspendedInTheAir.png",
    4016: "RobotTilt.png",
    4017: "UnableToStartWithinTheRestrictedArea.png",
    4018: "UnableToStartLnsideTheCarpet.png",
    4020: "TheMopTrayIsEntangled.png",
    4021: "TheMopTrayIsNotInstalled.png",
    4501: "DustBagNotInstalled.png",
    4502: "InsufficientWater.png",
    4503: "FullOfSewage.png",
    4506: "TheMopCleaningTankIsFullOfWater.png",
    4507: "TheMopCleaningTankIsFullOfWater.png",
    4901: "UnableTo4901.png",
    4902: "InsufficientCleaningSolution4902.png",
    4903: "UnableDrainW4903ater.png",
}

XTL_MESSAGE_CODE_KEYS = {
    1001: "charge_complete_resume",
    1002: "low_battery_return",
    1003: "upgrade_failed",
    1004: "upgrade_success",
    1005: "upgrade_available",
    1006: "battery_low_shutdown",
    1007: "shutdown_soon",
    1008: "cleaning_done_returning",
    1009: "clean_dirty_water_tank",
    1010: "schedule_started",
    1011: "schedule_conflict",
    1012: "schedule_skipped_dnd",
    1013: "schedule_low_battery",
    1014: "area_not_found",
    1015: "lost_return_to_station",
    2001: "maintain_side_brush",
    2002: "maintain_roller_brush",
    2003: "maintain_dust_box_filter",
    2004: "maintain_mop_pads",
    2005: "maintain_dust_bag",
    2006: "maintain_mop_slot",
    2007: "maintain_dirty_water_filter",
    2008: "maintain_unit_sensor",
}

# 17.49 robot-status tokens (the plugin's curRobotStatus spellings) -> ENUM
# option ids. BackWash*/BackClct*/BP* are the mid-task "robot left the dock
# toward X / its return is paused" tokens; HotDry and WindDry share the
# plugin's Auto-drying label but stay distinct options.
XTL_STATION_STATUSES = {
    "None": "none",
    "Asleep": "asleep",
    "Relocate": "relocating",
    "WashMop": "washing_mop",
    "ClctDust": "emptying",
    "HotDry": "hot_drying",
    "WindDry": "wind_drying",
    "BackWashMop": "returning_to_wash",
    "BackWashPause": "return_to_wash_paused",
    "BackClctDust": "returning_to_empty",
    "BackClctPause": "return_to_empty_paused",
    "BPReturn": "returning",
    "BPPauseReturn": "return_paused",
    "Charging": "charging",
    "ChargeAsleep": "charged",
}


def xtl_station_status_option(raw) -> str | None:
    """17.49 token -> option id, case-insensitive (the exact firmware
    spelling stays live-verification material); unknown/None -> None."""
    if not isinstance(raw, str) or not raw:
        return None
    target = raw.strip().lower()
    for token, option in XTL_STATION_STATUSES.items():
        if token.lower() == target:
            return option
    return None


# Fix-it texts the app shows under each error (plugin errorCodes map). The
# long-form solutions are Python-side tables, not translations/*.json — the
# attribute is read synchronously and the plugin itself only ships 11
# locales — the sensor picks EN/FR off hass.config.language (the user's HA
# language), everything else on English.
XTL_ERROR_SOLUTIONS_EN = {
    4001: "Open the robot cover and insert the dust box.",
    4003: "Take off the roller brush cover, take out the roller brush, and clear tangled hair and other debris.",
    4004: "Take off the side brush and clear the tangled hair and other debris.",
    4005: "Rotate the drive wheel, clean tangled hair, or obstructing debris.",
    4006: "Wipe the drop sensor with a dry cloth, and place the robot in a safe position to restart.",
    4007: "Remove the objects obstructing the laser sensor.",
    4008: "Remove the debris jamming the rotating radar of the laser sensor and gently tap the sensor cover.",
    4009: "Wipe the drive wheel and place the robot in another position to restart.",
    4011: "Remove objects obstructing the laser sensor cover, or place the robot in another position to restart.",
    4012: "Remove the obstacles around the robot, or move it to a new area to restart.",
    4013: "Gently tap the robot's front bumper and confirm that the bumper rebounds.",
    4014: "Restart the robot after placing it back on the ground.",
    4016: "Restart the robot after placing it on a level surface.",
    4017: "Restart the robot after moving it away from the restricted area, or delete the current restricted area.",
    4018: "Restart the robot after moving it away from the carpet area, or modify the current carpet cleaning strategy.",
    4020: "Take off the mop pad holder and clear tangled hair and other debris.",
    4021: "Install both mop pad holders properly.",
    4501: "Correctly install the dust bag.",
    4502: "Take out the clean water tank, add an adequate amount of water, reinstall the clean water tank, and ensure it is properly installed.",
    4503: "Take out the dirty water tank, empty all the dirty water, reinstall the dirty water tank, and ensure it is properly installed.",
    4506: "1. Remove the mop cleaning slot filter, clean the accumulated hair and stains on the filter, and reinstall it; 2. Check for objects blocking the mop cleaning slot suction port; 3. Ensure the dirty water tank cover is tightly closed; 4. Verify the dirty water tank is properly installed.",
    4507: "1. Remove the filter from the mop cleaning slot, clean any hair and stains accumulated on the filter, and reinstall it; 2. Check for objects blocking the suction port of the mop cleaning slot; 3. Take out the dirty water tank filter from the auto water refill and drain kit, clean it thoroughly, and reinstall it; 4. Verify the cover of the automatic water supply and drain kit module is tightly closed.",
    4901: "1. Check the household water pressure; 2. Verify the water inlet pipe is correctly installed.",
    4902: "1. Check the drain pipe is correctly installed; 2. Open the dirty water tank cover, remove the filter, clean it, and reinstall it.",
    4903: "Add an adequate amount of cleaning solution to the auto water refill and drain kit.",
}

XTL_ERROR_SOLUTIONS_FR = {
    4001: "Ouvrez le couvercle du robot et insérez la boîte à poussière.",
    4003: "Retirez le couvercle de la brosse rouleau, sortez la brosse rouleau et nettoyez les cheveux emmêlés et autres débris.",
    4004: "Retirez la brosse latérale et nettoyez les cheveux emmêlés et autres débris.",
    4005: "Faites tourner la roue motrice, nettoyez les cheveux emmêlés ou les débris obstruants.",
    4006: "Essuyez le capteur de chute avec un chiffon sec et placez le robot dans une position sûre pour redémarrer.",
    4007: "Retirez les objets obstruant le capteur laser.",
    4008: "Retirez les débris qui bloquent le radar rotatif du capteur laser et tapez doucement sur le couvercle du capteur.",
    4009: "Essuyez la roue motrice et replacez le robot dans une autre position pour redémarrer.",
    4011: "Retirez les objets obstruant le couvercle du capteur laser, ou placez le robot dans une autre position pour redémarrer.",
    4012: "Retirez les obstacles autour du robot ou déplacez le robot vers une nouvelle zone pour redémarrer.",
    4013: "Tapotez doucement le pare-chocs avant du robot et confirmez que le pare-chocs rebondit.",
    4014: "Redémarrez le robot après l'avoir replacé au sol.",
    4016: "Redémarrez le robot après l'avoir placé sur une surface plane.",
    4017: "Redémarrez le robot après l'avoir déplacé hors de la zone restreinte, ou supprimez la zone restreinte actuelle.",
    4018: "Redémarrez le robot après l'avoir éloigné de la zone de tapis, ou modifiez la stratégie de nettoyage actuelle du tapis.",
    4020: "Retirez le support des serpillières et nettoyez les cheveux emmêlés et autres débris.",
    4021: "Installez correctement les deux supports de serpillières.",
    4501: "Installez correctement le sac à poussière.",
    4502: "Sortez le réservoir d'eau propre, ajoutez une quantité adéquate d'eau, réinstallez-le et assurez-vous qu'il est correctement installé.",
    4503: "Sortez le réservoir d'eau sale, videz toute l'eau sale, réinstallez le réservoir d'eau sale et assurez-vous qu'il est correctement installé.",
    4506: "1. Retirez le filtre du compartiment de nettoyage des serpillières, nettoyez les poils accumulés et les taches sur le filtre, puis réinstallez-le ; 2. Vérifiez s'il y a des objets bloquant le port d'aspiration du slot de nettoyage de la serpillière ; 3. Assurez-vous que le couvercle du réservoir d'eau usée est bien fermé ; 4. Vérifiez si le réservoir d'eau usée est correctement installé en place.",
    4507: "1. Retirez le filtre de l'emplacement de nettoyage des serpillières, nettoyez les poils et les taches accumulés sur le filtre, puis réinstallez-le ; 2. Vérifiez s'il y a des objets bloquant le port d'aspiration du slot de nettoyage de la serpillière ; 3. Retirez le filtre du réservoir d'eau usée du module de kit d'alimentation et de vidange, nettoyez-le soigneusement, puis réinstallez-le ; 4. Vérifiez si le couvercle du module de kit d'alimentation et de vidange automatique est bien fermé.",
    4901: "1. Vérifiez si la pression de l'eau domestique est normale ; 2. Vérifiez si le tuyau d'entrée d'eau est correctement installé.",
    4902: "1. Vérifiez si le tuyau de vidange est correctement installé ; 2. Ouvrez le couvercle du réservoir d'eau usée, retirez le filtre, nettoyez-le et réinstallez-le.",
    4903: "Ajoutez une quantité suffisante de solution de nettoyage dans le module de kit d'alimentation et de vidange automatique.",
}

# French summary vocabulary — the official Mi Home FR wording for every
# clause (gears kw 292-295, wetness kw 296-299, route kw 136/138, scope
# kw 171/172, weekdays lower-cased to match the sentence the way the live
# 2026-09-25 calibration rendered them).
XTL_TIMER_LABELS_FR = {
    "days": ("lun", "mar", "mer", "jeu", "ven", "sam", "dim"),
    "every_day": "tous les jours",
    "once_prefix": "unique ",
    "whole_home": "toute la maison",
    "map_prefix": "carte ",
    "rooms_prefix": "pièces ",
    "passes": "{n} passes",
    "disabled": "(désactivé)",
    "work": {"0": "asp.+lavage", "1": "aspiration seule", "2": "lavage seul"},
    "fan": {"0": "silencieux", "1": "standard", "2": "fort", "3": "turbo"},
    "water": {"0": "humidité faible", "1": "humidité moyenne", "2": "humidité élevée"},
    "route": {"0": "route rapide", "2": "route approfondie"},
}


def xtl_timer_labels(language) -> dict:
    """Timer-summary label table for an HA language (fr -> the official
    FR vocabulary, everything else English)."""
    if str(language or "").lower().startswith("fr"):
        return XTL_TIMER_LABELS_FR
    return XTL_TIMER_LABELS_EN


# The 9 extra locales the official plugin ships (keywordN tables of
# main.bundle, extracted verbatim — vendor typos included, they are the
# vendor's own wording and stay diffable against the bundle). en/fr reuse
# the polished tables above; the plugin EN/FR wording differs only in
# punctuation/style, ours stays canonical for those two.
XTL_ERROR_SOLUTIONS_DE = {
    4001: "Öffnen Sie die Roboterabdeckung und setzen Sie den Staubbehälter ein.",
    4003: "Nehmen Sie die Abdeckung der Walzenbürste ab, nehmen Sie die Walzenbürste heraus und entfernen Sie verhedderte Haare und andere Verunreinigungen.",
    4004: "Nehmen Sie die Seitenbürste ab und entfernen Sie die verhedderten Haare und andere Verschmutzungen.",
    4005: "Drehen Sie das Antriebsrad und befreien Sie es von verhedderten Haaren oder anderen Verunreinigungen.",
    4006: "Wischen Sie den Tropfensensor mit einem trockenen Tuch ab und stellen Sie den Roboter in eine sichere Position, um ihn neu zu starten.",
    4007: "Entfernen Sie die Gegenstände, die den Lasersensor behindern.",
    4008: "Entfernen Sie den Schmutz, der den rotierenden Radar des Lasersensors blockiert, und klopfen Sie vorsichtig auf die Sensorabdeckung.",
    4009: "Wischen Sie das Antriebsrad ab und bringen Sie den Roboter in eine andere Position, um ihn neu zu starten.",
    4011: "Bitte entfernen Sie Objekte, die die Abdeckung des Lasersensors blockieren, oder bringen Sie den Roboter in eine andere Position, um ihn neu zu starten.",
    4012: "Bitte entfernen Sie die Hindernisse um den Roboter herum, oder begeben Sie sich in einen anderen Bereich, um neu zu starten.",
    4013: "Bitte klopfen Sie leicht auf die vordere Stoßstange des Roboters und vergewissern Sie sich, dass die Stoßstange zurückfedert.",
    4014: "Starten Sie den Roboter neu, nachdem Sie ihn wieder auf den Boden gestellt haben.",
    4016: "Starten Sie den Roboter neu, nachdem Sie ihn auf eine ebene Fläche gestellt haben.",
    4017: "Starten Sie den Roboter neu, nachdem Sie ihn aus dem Sperrgebiet entfernt haben, oder löschen Sie das aktuelle Sperrgebiet.",
    4018: "Starten Sie den Roboter neu, nachdem Sie ihn aus dem Teppichbereich entfernt haben, oder ändern Sie die aktuelle Teppichreinigungsstrategie.",
    4020: "Nehmen Sie den Wischmopphalter ab und entfernen Sie verhedderte Haare und andere Verunreinigungen.",
    4021: "Bringen Sie beide Mop-Pad-Halterungen richtig an.",
    4501: "Setzen Sie den Staubbeutel richtig ein.",
    4502: "Nehmen Sie den Frischwassertank heraus, füllen Sie eine ausreichende Menge Wasser ein, setzen Sie den Frischwassertank wieder ein und vergewissern Sie sich, dass er ordnungsgemäß installiert ist.",
    4503: "Nehmen Sie den Schmutzwassertank heraus, leeren Sie das gesamte Schmutzwasser, setzen Sie den Schmutzwassertank wieder ein und vergewissern Sie sich, dass er ordnungsgemäß installiert ist.",
    4506: "1. Entfernen Sie den Filter aus dem Mopp-Reinigungsschlitz, reinigen Sie die angesammelten Haare und Flecken auf dem Filter und installieren Sie ihn anschließend wieder; \r\n2. Überprüfen Sie, ob Objekte den Ansauganschluss des Reinigungsbereichs des Wischmopps blockieren; \r\n3. Stellen Sie sicher, dass der Deckel des Abwassertanks fest verschlossen ist; \r\n4. Überprüfen Sie, ob der Abwassertank ordnungsgemäß installiert ist.",
    4507: "1. Entnehmen Sie den Filter aus dem Mop-Reinigungsschlitz, reinigen Sie alle Haare und Flecken, die sich auf dem Filter angesammelt haben, und installieren Sie ihn wieder; \r\n2. Überprüfen Sie, ob Objekte den Ansauganschluss des Reinigungsbereichs des Wischmopps blockieren; \r\n3. Nehmen Sie den Schmutzwassertankfilter aus dem Modul für die Wasserversorgung und den Abfluss heraus, reinigen Sie ihn gründlich und setzen Sie ihn wieder ein; \r\n4. Vergewissern Sie sich, dass die Abdeckung des automatischen Wasserzufuhr- und -ablassmoduls fest geschlossen ist.",
    4901: "1. Prüfen Sie, ob der Wasserdruck im Haushalt normal ist; \r\n2. Überprüfen Sie, ob die Wasserzuleitung richtig installiert ist.",
    4902: "1. Prüfen Sie, ob das Abflussrohr richtig installiert ist; \r\n2. 2. Öffnen Sie die Abdeckung des Schmutzwassertanks, nehmen Sie den Filter heraus, reinigen Sie ihn und setzen Sie ihn wieder ein.",
    4903: "Bitte geben Sie eine ausreichende Menge Reinigungslösung in das Modul für die automatische Wasserzufuhr und den Abfluss.",
}

XTL_ERROR_SOLUTIONS_ES = {
    4001: "Abra la cubierta del robot e inserte la caja de polvo.",
    4003: "Quitar la cubierta del cepillo principal, sacar el cepillo principal y limpiar los pelos enredados y otros residuos.",
    4004: "Quitar el cepillo lateral y limpiar los pelos enredados y otros residuos.",
    4005: "Gire la rueda motriz, limpie los pelos enredados o los residuos que obstruyen.",
    4006: "Limpie el sensor de goteo con un paño seco y coloque el robot en una posición segura para reiniciar.",
    4007: "Quite los objetos que obstruyen el sensor láser.",
    4008: "Quite los desechos que obstruyen el radar giratorio del sensor láser y golpee suavemente la cubierta del sensor.",
    4009: "Limpie la rueda motriz y coloque el robot en otra posición para reiniciar.",
    4011: "Por favor, retire los objetos que obstruyen la cubierta del sensor láser, o coloque el robot en otra posición para reiniciar.",
    4012: "Por favor, retire los obstáculos alrededor del robot, o muévalo a un área nueva para reiniciar.",
    4013: "Por favor, toque suavemente el parachoques delantero del robot y confirme que el parachoques rebota.",
    4014: "Reinicie el robot después de colocarlo de nuevo en el suelo.",
    4016: "Reinicie el robot después de colocarlo en una superficie nivelada.",
    4017: "Reinicie el robot después de moverlo lejos del área restringida, o elimine el área restringida actual.",
    4018: "Reinicie el robot después de moverlo lejos del área de la alfombra, o modifique la estrategia actual de limpieza de alfombras.",
    4020: "Quitar el soporte de la almohadilla del trapeador y limpiar los pelos enredados y otros residuos.",
    4021: "Instale correctamente ambos soportes de almohadillas de trapeador.",
    4501: "Instale correctamente la bolsa de polvo.",
    4502: "Saque el tanque de agua limpia, agregue una cantidad adecuada de agua, reinstale el tanque de agua limpia y asegúrese de que esté correctamente instalado.",
    4503: "Saque el tanque de agua sucia, vacíe toda el agua sucia, reinstale el tanque de agua sucia y asegúrese de que esté correctamente instalado.",
    4506: "1. Retira el filtro del compartimento de limpieza de la fregona, limpia los cabellos y las manchas acumuladas en el filtro y reinstálalo después.  \r\n2. Verifique si hay objetos bloqueando el puerto de succión del slot de limpieza del mop; \r\n3. Asegúrate de que la cubierta del depósito de aguas residuales esté bien cerrada.  \r\n4. Verifica si el depósito de aguas residuales está correctamente instalado.",
    4507: "1. Retira el filtro de la ranura de limpieza del trapeador, limpia cualquier cabello y manchas acumuladas en el filtro, y vuelva a instalarlo.  \r\n2. Verifique si hay objetos bloqueando el puerto de succión del slot de limpieza del mop; \r\n3. Saca el filtro del tanque de agua y drenaje, límpialo a fondo y vuelve a instalarlo.  \r\n4. Verifica si la cubierta del módulo de suministro y drenaje de agua automático está bien cerrada.",
    4901: "1. Verifique si la presión del agua del hogar es normal.  \r\n2. Verifique si la tubería de entrada de agua está instalada correctamente.",
    4902: "1. Verifique si el tubo de desagüe está instalado correctamente.  \r\n2. Abra la tapa del tanque de aguas residuales, retire el filtro, límpielo y vuelva a instalarlo.",
    4903: "Por favor, agregue una cantidad adecuada de solución de limpieza al kit de suministro y drenaje de agua automático.",
}

XTL_ERROR_SOLUTIONS_IT = {
    4001: "Apri il coperchio del robot e inserisci il contenitore della polvere.",
    4003: "Togli il coperchio del rullo spazzola, estrai il rullo spazzola e pulisci i capelli e altri detriti aggrovigliati.",
    4004: "Togli il pennello laterale e pulisci i capelli e altri detriti aggrovigliati.",
    4005: "Ruota la ruota motrice, pulisci i capelli aggrovigliati o detriti che ostruiscono.",
    4006: "Pulisci il sensore di caduta con un panno asciutto e posiziona il robot in una posizione sicura per riavviarlo.",
    4007: "Rimuovi gli oggetti che ostruiscono il sensore laser.",
    4008: "Rimuovi i detriti che bloccano il radar rotante del sensore laser e tocca delicatamente il coperchio del sensore.",
    4009: "Pulisci la ruota motrice e posiziona il robot in un'altra posizione per riavviarlo.",
    4011: "Per favore, rimuovi gli oggetti che ostruiscono il coperchio del sensore laser o posiziona il robot in un'altra posizione per riavviarlo.",
    4012: "Per favore, rimuovi gli ostacoli intorno al robot o spostati in una nuova area per riavviarlo.",
    4013: "Per favore, tocca delicatamente il paraurti anteriore del robot e conferma che il paraurti rimbalza.",
    4014: "Riavvia il robot dopo averlo riposizionato a terra.",
    4016: "Riavvia il robot dopo averlo posizionato su una superficie livellata.",
    4017: "Riavvia il robot dopo averlo spostato fuori dall'area restrittiva, oppure elimina l'area restrittiva attuale.",
    4018: "Riavvia il robot dopo averlo spostato fuori dall'area del tappeto, oppure modifica la strategia corrente di pulizia del tappeto.",
    4020: "Rimuovi il supporto per il mop e pulisci i capelli aggrovigliati e altri detriti.",
    4021: "Installare entrambi i supporti per il mop correttamente.",
    4501: "Installare correttamente il sacchetto della polvere.",
    4502: "Rimuovi il serbatoio dell'acqua pulita, aggiungi una quantità adeguata di acqua, reinstalla il serbatoio dell'acqua pulita e assicurati che sia installato correttamente.",
    4503: "Rimuovi il serbatoio dell'acqua sporca, svuota tutta l'acqua sporca, reinstalla il serbatoio dell'acqua sporca e assicurati che sia installato correttamente.",
    4506: "1. Rimuovere il filtro dello slot di pulizia del mop, pulire i capelli e le macchie accumulati sul filtro e quindi reinstallarlo; \r\n2. Controllare se ci sono oggetti che bloccano il portello di aspirazione dello slot di pulizia del mop; \r\n3. Assicurarsi che il coperchio del serbatoio dell'acqua sporca sia saldamente chiuso; \r\n4. Verificare se il serbatoio dell'acqua sporca è correttamente installato al suo posto.",
    4507: "1. Rimuovere il filtro dallo slot di pulizia del mop, pulire eventuali capelli e macchie accumulate sul filtro e reinstallarlo;  \r\n2. Controllare se ci sono oggetti che bloccano il portello di aspirazione dello slot di pulizia del mop; \r\n3. Estrarre il filtro del serbatoio dell'acqua sporca dal modulo di alimentazione e scarico dell'acqua, pulirlo accuratamente e reinstallarlo; \r\n4. Verificare se il coperchio del modulo di alimentazione e scarico automatico dell'acqua è chiuso saldamente.",
    4901: "1. Verificare se la pressione dell'acqua domestica è normale; \r\n2. Verificare se il tubo di ingresso dell'acqua è installato correttamente.",
    4902: "1. Verificare se il tubo di scarico è installato correttamente; \r\n2. Aprire il coperchio del serbatoio dell'acqua sporca, rimuovere il filtro, pulirlo e reinstallarlo.",
    4903: "Aggiungere una quantità adeguata di soluzione detergente al modulo di alimentazione e scarico automatico dell'acqua.",
}

XTL_ERROR_SOLUTIONS_PL = {
    4001: "Otwórz pokrywę robota i włóż pojemnik na kurz.",
    4003: "Zdejmij pokrywę szczotki walcowej, wyjmij szczotkę walcową i usuń splątane włosy i inne zanieczyszczenia.",
    4004: "Zdejmij boczną szczotkę i usuń splątane włosy oraz inne zanieczyszczenia.",
    4005: "Obróć koło napędowe, usuń splątane włosy lub blokujące zanieczyszczenia.",
    4006: "Przetrzyj czujnik suchą szmatką i umieść robota w bezpiecznej pozycji do ponownego uruchomienia.",
    4007: "Usuń przedmioty zasłaniające czujnik laserowy.",
    4008: "Usuń zanieczyszczenia blokujące radar obrotowy czujnika laserowego i delikatnie postukaj w pokrywę czujnika.",
    4009: "Wytrzyj koło napędowe i umieść robota w innej pozycji, aby ponownie uruchomić.",
    4011: "Proszę usunąć przedmioty blokujące pokrywę czujnika laserowego lub umieścić robota w innej pozycji, aby ponownie uruchomić.",
    4012: "Proszę usunąć przeszkody wokół robota lub przenieść go na nowe miejsce, aby ponownie uruchomić.",
    4013: "Delikatnie stuknij w przedni zderzak robota i potwierdź, że zderzak się odbija.",
    4014: "Uruchom ponownie robota po umieszczeniu go z powrotem na ziemi.",
    4016: "Uruchom ponownie robota po umieszczeniu go na płaskiej powierzchni.",
    4017: "Uruchom ponownie robota po przesunięciu go z obszaru ograniczonego, lub usuń aktualny obszar ograniczony.",
    4018: "Uruchom ponownie robota po przesunięciu go z obszaru dywanowego, lub zmodyfikuj aktualną strategię czyszczenia dywanu.",
    4020: "Zdejmij uchwyt na mop i usuń splątane włosy oraz inne zanieczyszczenia.",
    4021: "Zainstaluj oba uchwyty na mop poprawnie.",
    4501: "Prawidłowo zainstaluj worek na kurz.",
    4502: "Wyjmij pojemnik na czystą wodę, dodaj odpowiednią ilość wody, ponownie zainstaluj pojemnik na czystą wodę i upewnij się, że jest on prawidłowo zainstalowany.",
    4503: "Wyjmij pojemnik na brudną wodę, opróżnij całą brudną wodę, ponownie zainstaluj pojemnik na brudną wodę i upewnij się, że jest on prawidłowo zainstalowany.",
    4506: "1. Wyjmin filtr ze szczeliny czyszczącej mopa, wyczyść nagromadzone włosy i plamy na filtrze, a następnie zamontuj go ponownie; \r\n.\n2. Sprawdź, czy żadne przedmioty nie blokują otworu ssącego szczeliny czyszczącej mopa; \r\n3. Upewnij się, że pokrywa zbiornika brudnej wody jest szczelnie zamknięta; \r\n4. Sprawdź, czy zbiornik brudnej wody jest prawidłowo zamontowany na swoim miejscu.",
    4507: "„1. Wyjmij filtr ze szczeliny czyszczącej mopa, wyczyść wszelkie włosy i plamy nagromadzone na filtrze i zainstaluj go ponownie;  \r\n2. Sprawdzić, czy żadne przedmioty nie blokują otworu ssącego szczeliny czyszczącej mopa; \r\n3. Wyjmij filtr zbiornika brudnej wody z zestawu doprowadzania i odprowadzania wody, wyczyść go dokładnie i zainstaluj ponownie; \r\n4. Sprawdź, czy pokrywa modułu zestawu doprowadzania i odprowadzania wody jest szczelnie zamknięta.”",
    4901: "„1. sprawdzić, czy ciśnienie wody w gospodarstwie domowym jest normalne; \r\n2. Sprawdź, czy rura wlotowa wody jest prawidłowo zainstalowana.”",
    4902: "„1. Sprawdź, czy przewód odprowadzająca jest prawidłowo zainstalowana; \r\n2. Otwórz pokrywę zbiornika brudnej wody, wyjmij filtr, wyczyść go i zainstaluj ponownie.”",
    4903: "Proszę dodać odpowiednią ilość roztworu do automatycznego zasilania wodą i modułu odpływu.",
}

XTL_ERROR_SOLUTIONS_RU = {
    4001: "Откройте крышку робота и вставьте пылесборник.",
    4003: "Снимите крышку роликовой щетки, выньте роликовую щетку и очистите её от волос и мусора.",
    4004: "Снимите боковую щетку и очистите её от волос и мусора.",
    4005: "Проверните застрявшее колесо привода, очистите его от волос или мусора.",
    4006: "Протрите датчик падения сухой тряпкой и поместите робота в безопасное положение для перезапуска.",
    4007: "Удалите препятствия, мешающие лазерному датчику.",
    4008: "Удалите мусор, заблокировавший вращающийся радар лазерного датчика и аккуратно постучите по крышке датчика.",
    4009: "Протрите колесо приводы и переместите робота в другое место для перезапуска.",
    4011: "Пожалуйста, удалите объекты, мешающие лазерному датчику и переместите робота в другое место для перезапуска.",
    4012: "Пожалуйста, удалите препятствия вокруг робота или переместите его в новую область для перезапуска.",
    4013: "Пожалуйста, аккратно нажмите на передний датчик столкновения робота и подтвердите отскок датчика.",
    4014: "Перезапустите робот после установки его обратно на пол.",
    4016: "Перезапустите робот после установки его на ровную поверхность.",
    4017: "Перезапустите робот после перемещения его из ограниченной зоны или удалите текущую ограниченную зону.",
    4018: "Перезапустите робот после перемещения его из зоны с коврами или измените текущий режим уборки ковров.",
    4020: "Снимите держатель тряпки и очистите запутанные волосы и другой мусор.",
    4021: "Правильно установите обе насадки для мытья пола.",
    4501: "Правильно установите пылесборник.",
    4502: "Достаньте чистый бак для воды, добавьте достаточное количество воды, установите бак снова и убедитесь, что он установлен правильно.",
    4503: "Достаньте грязный бак для воды, вылейте всю грязную воду, установите бак снова и убедитесь, что он установлен правильно.",
    4506: "1. Снимите фильтр из модуля для чистки швабры, очистите фильтр от волос и мусора, а затем установите его обратно.  \r\n2. Проверьте, нет ли каких-либо посторонних предметов, блокирующих всасывающее отверстие модуля для чистки швабры.  \r\n3. Убедитесь, что крышка резервуара для отработанной воды плотно закрыта.  \r\n4. Проверьте, правильно ли установлен резервуар для отработанной воды.",
    4507: "1. Извлеките фильтр из отверстия для чистки швабры, очистите фильтр от волос и пятен и установите его на место; [бр]\n2. Проверьте, не блокируют ли какие-либо посторонние предметы всасывающее отверстие слота для чистки швабры; \r\n3. Выньте фильтр бака для грязной воды из модуля подачи и слива воды, тщательно очистите его и установите на место; \r\n4. Убедитесь, что крышка модуля автоматической подачи и слива воды плотно закрыта.",
    4901: "1. Проверьте, что давление воды в доме на нормальном уровне;  \r\n 2. Проверьте, что труба подачи воды правильно установлена.",
    4902: "1. Проверьте, что канализационная труба правильно установлена;  \r\n 2. Откройте крышку бака для грязной воды, извлеките фильтр, очистите его и установите обратно.",
    4903: "Пожалуйста, добавьте достаточное количество моющего средства в модуль автоматического подачи и слива воды.",
}

XTL_ERROR_SOLUTIONS_ZH_HANS = {
    4001: "打开机器人面盖，放入尘盒",
    4003: "取下滚刷盖板，拿出滚刷，清理缠绕的毛发等异物",
    4004: "取下边刷，清理缠绕的毛发等异物",
    4005: "转动驱动轮，清理缠绕的毛发、或卡住的异物",
    4006: "用干抹布擦拭跌落传感器，并将机器人放到安全位置重新启动",
    4007: "清理遮挡激光传感器的物体",
    4008: "清理卡住激光传感器旋转雷达的异物，并轻拍传感器上盖",
    4009: "擦拭驱动轮，并将机器人放到其他位置启动",
    4011: "请移除卡住激光传感器上盖的物体，或将机器人放到其他位置启动",
    4012: "请清除机器人周围的障碍物，或搬到新区域重新启动",
    4013: "请轻拍机器人前方撞板，并确认撞板回弹",
    4014: "将机器人放回地面后重新启动",
    4016: "将机器人放到水平地面后重新启动",
    4017: "将机器人搬离禁区后重新启动，或删除当前禁区",
    4018: "将机器人搬离地毯区域后重新启动，或修改当前地毯清洁策略",
    4020: "取下拖布圆盘，清理缠绕的毛发等异物",
    4021: "将2个拖布圆盘安装到位",
    4501: "正确安装集尘袋",
    4502: "拿出清水箱，添加足量的清水后，重新安装清水箱，并确认安装到位",
    4503: "拿出污水箱，清理所有的污水后，重新安装污水箱，并确认安装到位",
    4506: "1、取下拖布清洗槽滤网，清理滤网上堆积的毛发和污渍后重新安装； \r\n2、检查拖布清洗槽吸水口是否有异物堵塞； \r\n3、检查污水箱上盖是否扣紧； \r\n4、检查污水箱是否安装到位。",
    4507: "1、取下拖布清洗槽滤网，清理滤网上堆积的毛发和污渍后重新安装； \r\n2、检查拖布清洗槽吸水口是否有异物堵塞； \r\n3、取出上下水模块中的污水箱滤网，清理干净后重新安装； \r\n4、检查自动上下水模块上盖是否扣紧。",
    4901: "1、检查家庭水压是否正常； \r\n2、检查上水管是否正常安装。",
    4902: "1、检查下水管是否正常安装； \r\n2、打开污水箱上盖，取出滤网，清理后重新安装。",
    4903: "请向自动上下水模块中添加足量的清洁液",
}

XTL_ERROR_SOLUTIONS_ZH_HANT = {
    4001: "打開機器人面蓋，放入塵盒",
    4003: "取下滾刷蓋板，拿出滾刷，清理纏繞的毛髮等異物",
    4004: "取下邊刷，清理纏繞的毛髮等異物",
    4005: "轉動驅動輪，清理纏繞的毛髮、或卡住的異物",
    4006: "用幹抹布擦拭跌落感測器，並將機器人放到安全位置重新啟動",
    4007: "清理遮擋鐳射感測器的物體",
    4008: "清理卡住鐳射感測器旋轉雷達的異物，並輕拍感測器上蓋",
    4009: "擦拭驅動輪，並將機器人放到其他位置啟動",
    4011: "請移除卡住鐳射感測器上蓋的物體，或將機器人放到其他位置啟動",
    4012: "請清除機器人周圍的障礙物，或搬到新區域重新啟動",
    4013: "請輕拍機器人前方撞板，並確認撞板回彈",
    4014: "將機器人放回地面後重新啟動",
    4016: "將機器人放到水準地面後重新啟動",
    4017: "將機器人搬離禁區後重新啟動，或刪除當前禁區",
    4018: "將機器人搬離地毯區域後重新啟動，或修改當前地毯清潔策略",
    4020: "取下拖布圓盤，清理纏繞的毛髮等異物",
    4021: "將2個拖布圓盤安裝到位",
    4501: "正確安裝集塵袋",
    4502: "拿出清水箱，添加足量的清水後，重新安裝清水箱，並確認安裝到位",
    4503: "拿出污水箱，清理所有的污水後，重新安裝污水箱，並確認安裝到位",
    4506: "1、取下拖布清洗槽濾網，清理濾網上堆積的毛髮和污漬後重新安裝； \r\n2、檢查拖布清洗槽吸水口是否有異物堵塞； \r\n3、檢查污水箱上蓋是否扣緊； \r\n4、檢查污水箱是否安裝到位。",
    4507: "1、取下拖布清洗槽濾網，清理濾網上堆積的毛髮和污漬後重新安裝； \r\n2、檢查拖布清洗槽吸水口是否有異物堵塞； \r\n3、取出上下水模組中的污水箱濾網，清理乾淨後重新安裝； \r\n4、檢查自動上下水模組上蓋是否扣緊。",
    4901: "1、檢查家庭水壓是否正常； \r\n2、檢查上水管是否正常安裝。",
    4902: "1、檢查下水管是否正常安裝； \r\n2、打開污水箱上蓋，取出濾網，清理後重新安裝。",
    4903: "請向自動上下水模組中添加足量的清潔液",
}

XTL_ERROR_SOLUTIONS_KO = {
    4001: "로봇 커버를 열고 먼지통을 장착해주세요.",
    4003: "메인브러시 커버를 열고 메인브러시를 커낸 후 엉켜 있는 머리카락등의 이물질을 제거하십시오.",
    4004: "사이드 브러시를 꺼내시고 엉켜 있는 머리카락등의 이물질을 제거하십시오.",
    4005: "바퀴를 돌려서 엉킨 머리카락이나 이물질을 제거 해주세요.",
    4006: "마른 걸레로 낙하방지 센서를 닦으시고 로봇을 안전한 위치에 두시고 작동시켜주세요.",
    4007: "레이저 센서를 가리고 있는 이물질을 제거하십시오.",
    4008: "레이저 센서의 회전 레이더에 걸린 이물질을 제거하고 센서 덮개를 가볍게 두드려 주세요.",
    4009: "바퀴을 닦고 로봇을 다른 위치에서 작동시켜주세요.",
    4011: "레이저 센서 커버가 걸린 물체를 제거하거나 로봇을 다른 위치에 두시고 작동시켜주세요.",
    4012: "로봇 주변에 있는 장애물을 제거하시거나 새로운 구역으로 이동한 후 작동시켜주세요.",
    4013: "로봇 앞쪽의 범퍼를 가볍게 두드리고 범퍼가 제대로 되돌아오는지 확인해 주세요.",
    4014: "로봇을 바닥에 두신 후 작동시켜주세요.",
    4016: "로봇을 평평한 바닥에 두신 후 작동시켜주세요.",
    4017: "로봇을 금지 구역에서 이동시킨 후 시작하거나 현재 금지 구역을 삭제하세요.",
    4018: "로봇을 카펫 구역에서 이동시킨 후 시작하거나 현재 카펫 청소 설정을 수정하세요.",
    4020: "물걸레키트를 분리하시고 엉켜 있는 머리카락등의 이물질을 제거하십시오.",
    4021: "물걸레키트 2개를 잘 장착해주세요.",
    4501: "더스트백을 잘 장착해주세요.",
    4502: "청수 탱크를 꺼내 적당량의 물을 채운 후, 청수탱크를 다시 설치하고 올바르게 장착되었는지 확인하세요.",
    4503: "오수 탱크를 꺼내 모든 오수를 비운 후, 오수크를 다시 설치하고 올바르게 장착되었는지 확인하세요.",
    4506: "1. 물걸레 세척 보드를 탈착하여 보드에 쌓인 머리카락과 얼룩을 청소한 후 다시 장착해주세요. \r\n2. 세척 싱크 오수 흡입구게 막혀있지 않은지 확인해주세요. \r\n3. 오수탱크의 덮개가 단단히 닫혀 있는지 확인하세요.\r\n4. 오수탱크가 올바르게 설치되었는지 확인하세요.",
    4507: "1. 걸레 세척 보드를 꺼내 보드에 쌓여 있는 머리카락과 오염물을 청소하신 후 다시 장착하십시오. \r\n2. 걸레 세척 싱크의 오수 흡입구가 막혀 있는지를 확인하십시오.; \r\n3. 직배수 키트에 있는 오수탱크 필터를 꺼내 청소한 후 다시 장착하십시오. \r\n4. 직배수 키트 뚜껑이 제대로 닫혀 있는지를 확인 하십시오.",
    4901: "1. 가정내의 수압이 정상적인지를 확인하십시오.  \r\n2. 급수관이 올바르게 설치되어 있는지 확인하십시오.",
    4902: "1. 배수관이 올바르게 설치되어 있는지 확인하십시오. \r\n2. 오수탱크 뚜껑을 열시고 필터를 꺼내십시오. 필터를 청소하신 후 다시 장착하십시오.",
    4903: "직배수 키트에 충분한 세정제를 투입하십시오.",
}

XTL_ERROR_SOLUTIONS_VI = {
    4001: "Vui lòng lắp hộp bụi",
    4003: "Kiểm tra và vệ sinh chổi lăn (chổi chính)",
    4004: "Kiểm tra và vệ sinh chổi cạnh",
    4005: "Vui lòng kiểm tra và làm sạch bánh xe trước",
    4006: "Vệ sinh cảm biến quanh robot bằng giẻ mềm và khô",
    4007: "Kiểm tra và vệ sinh laser điều hướng",
    4008: "Vệ sinh Laser bằng giẻ mềm và khô, vỗ nhẹ nắp chụp Laser",
    4009: "Kiểm tra và vệ sinh bánh xe",
    4011: "Kiểm tra và vệ sinh laser điều hướng",
    4012: "Kiểm tra và đặt robot ở vị trí bằng phẳng để khởi động lại",
    4013: "Gõ nhẹ vào cản trước để kiểm tra độ đàn hồi",
    4014: "Kiểm tra và đặt robot ở vị trí bằng phẳng để khởi động lại",
    4016: "Kiểm tra và đặt robot ở vị trí bằng phẳng để khởi động lại",
    4017: "Đưa robot ra khỏi khu vực cấm và khởi động lại robot ở vị trí bằng phẳng",
    4018: "Đưa robot ra khỏi thảm và khởi động lại robot ở vị trí bằng phẳng",
    4020: "Kiểm tra và vệ sinh khay gắn khăn",
    4021: "Kiểm tra và lắp lại khay khăn",
    4501: "Kiểm tra và lắp lại túi rác",
    4502: "Kiểm tra và bổ sung đủ nước sạch",
    4503: "Kiểm tra và vệ sinh bình nước bẩn",
    4506: "1. Tháo và vệ sinh khay giặt giẻ;\r\n 2. Kiểm tra và vệ sinh cổng hút nước bẩn;\r\n 3. Kiểm tra bình nước bẩn đã lắp đặt đúng cách;\r\n 4. Kiểm tra nắp bình nước bẩn đã lắp khít và đóng chặt.",
    4507: "1. Tháo và vệ sinh khay giặt giẻ;\r\n 2. Kiểm tra và vệ sinh cổng hút nước bẩn;\r\n 3. Kiểm tra bình nước bẩn đã lắp đặt đúng cách, nắp bình đóng khít và chặt;\r\n 4. Kiểm tra bộ kít thoát nước lắp đặt đúng cách.",
    4901: "1. Kiểm tra áp lực nước bình thường;\r\n 2. Kiểm tra đường ống lắp đặt đúng cách.",
    4902: "1. Kiểm tra áp lực nước bình thường;\r\n 2. Kiểm tra đường ống nước bẩn lắp đặt đúng cách, tháo ra và lắp lại.",
    4903: "Vui lòng thêm nước lau sàn vào bình nước sạch",
}


XTL_ERROR_SOLUTIONS = {
    "en": XTL_ERROR_SOLUTIONS_EN,
    "fr": XTL_ERROR_SOLUTIONS_FR,
    "de": XTL_ERROR_SOLUTIONS_DE,
    "es": XTL_ERROR_SOLUTIONS_ES,
    "it": XTL_ERROR_SOLUTIONS_IT,
    "pl": XTL_ERROR_SOLUTIONS_PL,
    "ru": XTL_ERROR_SOLUTIONS_RU,
    "zh_Hans": XTL_ERROR_SOLUTIONS_ZH_HANS,
    "zh_Hant": XTL_ERROR_SOLUTIONS_ZH_HANT,
    "ko": XTL_ERROR_SOLUTIONS_KO,
    "vi": XTL_ERROR_SOLUTIONS_VI,
}


def _solution_bucket(language) -> str:
    """HA language tag -> XTL_ERROR_SOLUTIONS key. The plugin's own locale
    set is en/zh-CN/zh-TW/ko/ru/es/fr/it/de/pl/vi; anything outside it
    (ja, nl, pt, pt-BR, …) falls back to English like the plugin itself.
    zh-Hant/zh-HK/zh-TW take Traditional, any other zh takes Simplified."""
    tag = str(language or "").strip().lower().replace("_", "-")
    if tag in ("zh-hant", "zh-hk", "zh-mo", "zh-tw"):
        return "zh_Hant"
    if tag.startswith("zh"):
        return "zh_Hans"
    if tag.startswith("pt"):          # incl. pt / pt-br: plugin ships none
        return "en"
    base = tag.split("-")[0]
    return base if base in XTL_ERROR_SOLUTIONS else "en"


def xtl_error_solutions(language) -> dict:
    """Error fix-it texts for an HA language (the plugin's 11 locales;
    everything else English)."""
    return XTL_ERROR_SOLUTIONS[_solution_bucket(language)]


XTL_MESSAGE_CODES = {
    0: "",
    1001: "Charging complete, resuming cleaning",
    1002: "Low battery, returning to charge",
    1003: "Firmware upgrade failed",
    1004: "Firmware upgrade successful",
    1005: "New firmware available",
    1006: "Battery low, shutting down soon",
    1007: "Main unit about to shut down, return it to the station",
    1008: "Cleaning completed, returning to the station",
    1009: "Clean the dirty water tank to avoid odor",
    1010: "Scheduled cleaning started",
    1011: "Task conflict, schedule skipped",
    1012: "Scheduled cleaning skipped (Do Not Disturb)",
    1013: "Low battery, scheduled cleaning cannot run",
    1014: "Selected area not found, cleaning ended",
    1015: "Lost, please return the robot to the station",
    2001: "Maintain: side brush",
    2002: "Maintain: roller brush",
    2003: "Maintain: dust box filter",
    2004: "Maintain: mop pads",
    2005: "Maintain: dust bag",
    2006: "Maintain: mop cleaning slot",
    2007: "Maintain: dirty water tank filter",
    2008: "Maintain: main unit sensor",
}

XTL_XM2216 = ModelProfile(
    profile_id="xtl.xm2216",
    brand="xtl",
    core=XTL_XM2216_CORE,
    room_clean=XTL_XM2216_ROOM_CLEAN,
    consumables=XTL_XM2216_CONSUMABLES,
    map=XTL_XM2216_MAP,
    notes=("urn:miot-spec-v2:device:vacuum:0000A006:xtl-xm2216:4",),
    # Entity parity grew the poll to ~40 props; keep each get_properties
    # request well inside the firmware's UDP buffer (~1 KB observed safe).
    max_properties=10,
)
