"""Typed building blocks for runtime model profiles."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Prop:
    siid: int
    piid: int


@dataclass(frozen=True)
class Action:
    siid: int
    aiid: int
    in_piid: int | None = None
    # Some actions take several inputs (e.g. set-room-clean in[24,25,26]) or
    # return outputs. in_piid stays for the common single-input case.
    in_piids: tuple[int, ...] = ()
    out_piids: tuple[int, ...] = ()


@dataclass(frozen=True)
class PointZoneCapability:
    service: int
    zone_points: Prop
    target_point: Prop
    set_zone_point: Action
    start_zone_clean: Action
    start_point_clean: Action | None = None
    legacy_start_point_clean: Action | None = None
    pause_point_clean: Action | None = None
    pause_zone_clean: Action | None = None
    restrict_points: Prop | None = None
    set_virtual_wall: Action | None = None
    beauty_point: Prop | None = None
    set_beauty_wall: Action | None = None
    map_type: Prop | None = None


@dataclass(frozen=True)
class MapCapability:
    service: int
    map_num: Prop | None = None
    current_map_id: Prop | None = None
    get_map_list: Action | None = None
    upload_by_mapid: Action | None = None
    upload_by_mapid_ii: Action | None = None
    set_current_map: Action | None = None
    point_zone: PointZoneCapability | None = None
    # multi-map management
    map_list: Prop | None = None
    remember_state: Prop | None = None
    has_new_map: Prop | None = None
    del_map: Action | None = None
    rename_map: Action | None = None
    build_new_map: Action | None = None
    reset_map: Action | None = None
    # live trajectory
    current_path: Prop | None = None
    start_cleaning_point: Prop | None = None
    end_cleaning_point: Prop | None = None
    get_current_path: Action | None = None
    # rooms
    room_id_name_list: Prop | None = None
    split_points: Prop | None = None
    arrange_room_ids: Prop | None = None
    get_map_room_list: Action | None = None
    rename_room: Action | None = None
    arrange_room: Action | None = None
    split_room: Action | None = None
    mijia_room_list: Prop | None = None
    set_mijia_room_list: Action | None = None


@dataclass(frozen=True)
class RoomCleanCapability:
    room_ids: Prop | None = None
    start: Action | None = None
    # richer variant: explicit room ids + mode (global/edge) + operation
    clean_room_ids: Prop | None = None
    clean_room_mode: Prop | None = None
    clean_room_oper: Prop | None = None
    set_room_clean: Action | None = None


@dataclass(frozen=True)
class XtlRoomCleanCapability(RoomCleanCapability):
    """xtl.vacuum.xm2216 room clean: the SAME start-clean action as `start`,
    re-invoked with a task-type code as its first in-param and a JSON room-id
    list as its second (in = [clean-type(8), clean-values(34)]). So ids never
    go through a property — the type codes select the mode."""
    type_full: int = 1
    type_room: int = 3
    type_zone: int = 4
    type_spot: int = 6


@dataclass(frozen=True)
class ScheduleCapability:
    """Timed/scheduled cleans (siid order)."""
    service: int
    # ijai adds via an `add` action; viomi's order service has only del/get and
    # schedules are written through the `orderdata` prop -> these stay None there.
    add: Action | None = None
    delete: Action | None = None
    get: Action | None = None
    add_iii: Action | None = None
    order_id: Prop | None = None
    enable: Prop | None = None
    day: Prop | None = None
    hour: Prop | None = None
    minute: Prop | None = None
    repeat: Prop | None = None
    clean_way: Prop | None = None
    suction: Prop | None = None
    water: Prop | None = None
    twice_clean: Prop | None = None
    mapid: Prop | None = None
    room_count: Prop | None = None
    room_data: Prop | None = None
    orderdata: Prop | None = None


@dataclass(frozen=True)
class SettingsCapability:
    """Read/write feature toggles and mop/drive controls."""
    mop_route: Prop | None = None
    shake_shift: Prop | None = None
    tank_shake: Prop | None = None
    direction: Prop | None = None
    dirt_recognize: Prop | None = None
    pet_recognize: Prop | None = None
    ai_recognize: Prop | None = None
    carpet_booster: Prop | None = None
    carpet_avoid: Prop | None = None
    map_encrypt: Prop | None = None
    multi_prop_vacuum: Prop | None = None


@dataclass(frozen=True)
class ConsumablesCapability:
    """Lifetime hours and accessory presence (siid sweep)."""
    side_brush_hours: Prop | None = None
    main_brush_hours: Prop | None = None
    hypa_hours: Prop | None = None
    mop_hours: Prop | None = None
    side_brush_life: Prop | None = None
    main_brush_life: Prop | None = None
    hypa_life: Prop | None = None
    mop_life: Prop | None = None
    door_state: Prop | None = None
    cloth_state: Prop | None = None
    reset_consumable: Action | None = None


@dataclass(frozen=True)
class CleanHistoryCapability:
    """Last-clean record fields, carried by the clean-end event."""
    start_time: Prop | None = None
    use_time: Prop | None = None
    clean_area: Prop | None = None
    map_url: Prop | None = None
    clean_mode: Prop | None = None
    clean_way: Prop | None = None
    current_map: Prop | None = None
    task_status: Prop | None = None


@dataclass(frozen=True)
class DndCapability:
    """Do-not-disturb / quiet hours (siid disturb)."""
    service: int
    # ijai exposes a set-notdisturb action; viomi folds dnd props into its order
    # service with no dedicated action -> stays None there.
    set_notdisturb: Action | None = None
    enable: Prop | None = None
    start_hour: Prop | None = None
    start_minute: Prop | None = None
    end_hour: Prop | None = None
    end_minute: Prop | None = None
    timezone: Prop | None = None


@dataclass(frozen=True)
class VoiceCapability:
    """Voice-pack download/switch (siid language)."""
    service: int
    download_voice: Action
    get_download_status: Action | None = None
    target_voice: Prop | None = None
    cur_voice: Prop | None = None
    download_status: Prop | None = None
    download_progress: Prop | None = None
    voice_url: Prop | None = None


# --- dreame-native rich caps -------------------------------------------------
# dreame exposes its feature families on different services/types than ijai, so
# these mirror the ijai-shaped caps above but match dreame's MIoT vocabulary.
# All fields Optional; an absent member stays None. Nothing consumes these yet
# (only map/room_clean are read at runtime) — they carry the spec data forward
# for the per-brand map decode and card-parity baseline.


@dataclass(frozen=True)
class DreameMapCapability:
    """Blob-model map service (siid map): a single map-data blob fetched via
    map-req/update-map, not ijai's map-list catalogue."""
    service: int
    map_data: Prop | None = None
    frame_info: Prop | None = None
    object_name: Prop | None = None
    map_extend_data: Prop | None = None
    robot_time: Prop | None = None
    result_code: Prop | None = None
    mult_map_state: Prop | None = None
    mult_map_info: Prop | None = None
    map_req: Action | None = None
    update_map: Action | None = None


@dataclass(frozen=True)
class DreameConsumablesCapability:
    """Per-accessory life as percent + remaining time, each on its own service
    with a reset action (brush-cleaner x2 = main+side, filter, mop, detergent).
    Unlike ijai's lifetime-hours model. main/side brush split by service order
    (first brush-cleaner instance = main); the two carry identical labels so the
    split is by siid, not hardware-verified."""
    main_brush_life: Prop | None = None
    main_brush_left_time: Prop | None = None
    reset_main_brush: Action | None = None
    side_brush_life: Prop | None = None
    side_brush_left_time: Prop | None = None
    reset_side_brush: Action | None = None
    filter_life: Prop | None = None
    filter_left_time: Prop | None = None
    reset_filter: Action | None = None
    mop_life: Prop | None = None
    mop_left_time: Prop | None = None
    reset_mop: Action | None = None
    detergent_life: Prop | None = None
    detergent_left_time: Prop | None = None
    reset_detergent: Action | None = None
    # dust-bag service (its own siid, e.g. xiaomi.ov42gl's siid 19): added
    # 2026-08-01 for a xiaomi-brand profile that reuses this dataclass's
    # percent+hours shape (not a dreame-only concept, just modeled here
    # first) — every other field stays untouched, so existing dreame
    # profiles are unaffected.
    dust_bag_life: Prop | None = None
    dust_bag_left_time: Prop | None = None
    reset_dust_bag: Action | None = None


_CONSUMABLE_LIFE_KEYS = (
    "main_brush_life",
    "side_brush_life",
    "filter_life",
    "mop_life",
    "dust_bag_life",
    "detergent_life",
)


def consumable_life_props(
    consumables: ConsumablesCapability | DreameConsumablesCapability | None,
) -> dict[str, Prop | None]:
    """Return consumable percentage props keyed by sensor name."""
    if isinstance(consumables, DreameConsumablesCapability):
        return {key: getattr(consumables, key) for key in _CONSUMABLE_LIFE_KEYS}
    if isinstance(consumables, ConsumablesCapability):
        return {
            "main_brush_life": consumables.main_brush_life,
            "side_brush_life": consumables.side_brush_life,
            "filter_life": consumables.hypa_life,
            "mop_life": consumables.mop_life,
        }
    return {}


@dataclass(frozen=True)
class DreameDndCapability:
    """Do-not-disturb (siid do-not-disturb): enable + single HH:MM start/end
    string props, no action."""
    service: int
    enable: Prop | None = None
    start_time: Prop | None = None
    end_time: Prop | None = None


@dataclass(frozen=True)
class DreameSettingsCapability:
    """vacuum-extend service: cleaning/mop modes + feature toggles. cleaning_mode
    (suction) and mop_mode (water) also feed the dreame fan/water selects in the
    card-parity baseline.

    cleaning_mode duplicates core.fan_speed and mop_mode duplicates
    core.water_level in every dreame profile (verified 02-07-2026): the
    runtime intentionally drives fan/water via core.* only, and these fields
    stay as spec mirrors (see tests/pure/test_dreame_redundancy.py)."""
    service: int
    cleaning_mode: Prop | None = None
    mop_mode: Prop | None = None
    waterbox_status: Prop | None = None
    task_status: Prop | None = None
    break_point_restart: Prop | None = None
    carpet_press: Prop | None = None
    child_lock: Prop | None = None


@dataclass(frozen=True)
class DreameCleanHistoryCapability:
    """Lifetime totals from the clean-logs service (not ijai's per-clean record)."""
    service: int
    first_clean_time: Prop | None = None
    total_clean_time: Prop | None = None
    total_clean_times: Prop | None = None
    total_clean_area: Prop | None = None


@dataclass(frozen=True)
class DreameAudioCapability:
    """audio service: voice-pack props + locate (position) / play-sound. Carries
    the dreame locate path (ijai locates via the alarm prop).

    locate duplicates core.locate in every dreame profile that has it
    (verified 02-07-2026): the runtime drives locate via core.locate only,
    and this field stays as a spec mirror (see
    tests/pure/test_dreame_redundancy.py)."""
    service: int
    voice_packet_id: Prop | None = None
    voice_change_state: Prop | None = None
    set_voice: Prop | None = None
    locate: Action | None = None
    play_sound: Action | None = None


@dataclass(frozen=True)
class DreameScheduleCapability:
    """Scheduled cleans on the time service: timer blob + delete action."""
    service: int
    time_zone: Prop | None = None
    timer_clean: Prop | None = None
    timer_id: Prop | None = None
    delete_timer: Action | None = None


@dataclass(frozen=True)
class CoreCapability:
    """Core vacuum service: live telemetry, controls, and their value tables.

    Lean by design (decision 2026-06-25): consumable life and clean-area/time
    are NOT carried here — they come from ConsumablesCapability /
    CleanHistoryCapability and are parked ("coming soon") at launch. A field is
    ``None``/empty when the model's spec lacks it; entities are built per what
    exists. ``core is None`` on a profile = rich-reference only, not runnable.
    """
    # telemetry props (None = this model lacks it)
    status: Prop | None = None
    fault: Prop | None = None
    mode: Prop | None = None
    battery: Prop | None = None
    charging_state: Prop | None = None   # dreame has it, ijai does not
    fan_speed: Prop | None = None
    water_level: Prop | None = None
    sweep_type: Prop | None = None       # dreame has none -> stays None
    repeat: Prop | None = None
    alarm: Prop | None = None
    volume: Prop | None = None
    # actions
    start: Action | None = None
    stop: Action | None = None
    pause: Action | None = None          # real pause; None -> caller falls back to stop
    charge: Action | None = None
    locate: Action | None = None
    # value tables (empty = entity not built for this model)
    status_map: dict[int, str] = field(default_factory=dict)   # raw int -> HA activity
    fan_speeds: dict[str, int] = field(default_factory=dict)    # label -> raw
    water_levels: dict[str, int] = field(default_factory=dict)
    modes: dict[str, int] = field(default_factory=dict)
    sweep_types: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class XtlCoreCapability(CoreCapability):
    """xtl.vacuum.xm2216 core: gear changes go through dedicated actions, not
    writable properties (fan 17.13 / water 17.12 / mode 17.10 / count 17.14 /
    route 17.65 are read+notify only — MIoT writes to them are rejected). The
    in-param tuples carry what the actions need: start-clean wants
    [clean-type, clean-values JSON], pause-continue-work wants a
    robot-set-status code (7 pause / 8 continue). start-clean is also silently
    ignored while a task is still open, so a stop must precede it."""
    fan_action: Action | None = None    # set-fan-mode (17.26)
    water_action: Action | None = None  # set-water-mode (17.27)
    mode_action: Action | None = None   # set-clean-mode (17.46)
    count_action: Action | None = None  # set-clean-count (17.28)
    route_action: Action | None = None  # fine-drag-switch (17.50)
    count: Prop | None = None           # clean-count read-back (17.14)
    route: Prop | None = None           # fine-drag read-back (17.65)
    message: Prop | None = None         # message push code (17.36)
    station_error: Prop | None = None   # station error code (17.79)
    clean_type_status: Prop | None = None  # RobotStatus 0-18 (17.9) — which
    # clean task is running (whole/room/zone), drives the live map tips
    robot_status: Prop | None = None      # robot-status (17.49): string,
    # read+notify — the plugin's curRobotStatus ('WashMop', 'HotDry', ...).
    # The only readable window into a running dock cycle (wash/dry/empty), so
    # the station switches read it; their actions are write-only bools.
    clean_values: Prop | None = None    # clean-values JSON (17.34) — running
    # task targets: selected room ids (tap order) or zone point runs
    customization_rooms: Action | None = None  # customization-rooms (17.53
    # via piid 70): per-room Custom settings (type 1) + saved whole-home
    # cleaning order (type 2); one compact JSON string per chunk payload
    edite_area_info: Action | None = None      # edite-area-info (17.15 via
    # piid 60): rename / re-type a room — area-info JSON, write-only
    # --- entity parity (spec v4 siid 17). Same law as the gears above: every
    # setting READS its read+notify property and WRITES a dedicated action;
    # direct property writes are rejected by firmware. ---
    volume_action: Action | None = None       # set-volume (17.31 via piid 15)
    # boolean toggles (read prop + action)
    child_lock: Prop | None = None            # 17.6  / set-child-lock 17.36
    child_lock_action: Action | None = None
    disturb: Prop | None = None               # 17.1  / set-disturb-switch 17.24
    disturb_action: Action | None = None
    break_point: Prop | None = None           # 17.2  / set-break-switch 17.25
    break_point_action: Action | None = None
    carpet_boost: Prop | None = None          # 17.7  / set-carpet-boost 17.34
    carpet_boost_action: Action | None = None
    auto_drying: Prop | None = None           # 17.21 / set-drying-switch 17.48
    auto_drying_action: Action | None = None
    auto_solution: Prop | None = None         # 17.44 / set-auto-solution 17.39
    auto_solution_action: Action | None = None
    carpet_twice: Prop | None = None          # 17.74 / setcarpetcleantwice 17.55
    carpet_twice_action: Action | None = None
    carpet_first: Prop | None = None          # 17.75 / setcarpetcleanfirst 17.56
    carpet_first_action: Action | None = None
    erp: Prop | None = None                   # 17.42 / set-erp-switch 17.38
    erp_action: Action | None = None
    mop_augment: Prop | None = None           # 17.43 / set-mop-augment 17.35
    mop_augment_action: Action | None = None
    # labelled tables (prop + action + label->raw, like counts/routes)
    carpet_prefer: Prop | None = None         # 17.11 / set-carpet-prefer 17.33
    carpet_prefer_action: Action | None = None
    carpet_prefers: dict[str, int] = field(default_factory=dict)
    dust_collection: Prop | None = None       # 17.20 / set-dust-collection 17.32
    dust_collection_action: Action | None = None
    dust_collections: dict[str, int] = field(default_factory=dict)
    drying_time: Prop | None = None           # 17.22 / set-drying-time 17.43
    drying_time_action: Action | None = None
    drying_times: dict[str, int] = field(default_factory=dict)
    mop_wash_freq: Prop | None = None         # 17.23 / set-mop-wash-freq 17.44
    mop_wash_freq_action: Action | None = None
    mop_wash_freqs: dict[str, int] = field(default_factory=dict)
    mop_wash_temp: Prop | None = None         # 17.45 / set-mop-wash-temp 17.45
    mop_wash_temp_action: Action | None = None
    mop_wash_temps: dict[str, int] = field(default_factory=dict)
    # do-not-disturb window: 17.19 reads the JSON schedule, 17.30 (via 18) writes
    disturb_time: Prop | None = None
    disturb_time_action: Action | None = None
    # scheduled-cleanings list: 17.17 reads the timer JSON array (read+notify;
    # editing stays in Mi Home — no write path wired). Hidden prop: siid 17
    # answers code 0 for any piid, so 17.17 is proven real by its non-null
    # value (probe live 2026-09-25).
    clean_timers: Prop | None = None
    # multi-language voice packs: 17.25 reads the language code, switch-voice-
    # lang 17.21 (via string prop 61) writes it. Code table = spec enum
    # (1 Chine … 11 Vietnam, home.miot-spec.com xtl.vacuum.xm2216); the app
    # installs packs server-side, so a code whose pack is absent can read back
    # unchanged (17.78 language-ver reports what is actually installed).
    voice_language: Prop | None = None          # 17.25 language-voice
    language_ver: Prop | None = None            # 17.78 language-ver: version
    # string of the pack the robot actually installed (audio.conf Ver)
    voice_language_action: Action | None = None # switch-voice-lang 17.21 (in piid 61)
    voice_languages: dict[str, int] = field(default_factory=dict)
    # session + lifetime clean stats
    session_time: Prop | None = None          # 17.26 seconds, current clean
    session_area: Prop | None = None          # 17.27 m², current clean
    total_time: Prop | None = None            # 17.31
    total_area: Prop | None = None            # 17.29
    total_count: Prop | None = None           # 17.30
    clean_records: Prop | None = None         # 17.46 JSON history records
    # station / dock presence enums
    clean_water_cistern: Prop | None = None   # 17.51
    drain_cistern: Prop | None = None         # 17.52
    dust_bag_state: Prop | None = None        # 17.53
    mop_tank_state: Prop | None = None        # 17.54
    return_status: Prop | None = None         # 17.48 (dock-task progress)
    # one-shot station service runs (write-only bool props 5/4/3)
    wash_mop_action: Action | None = None     # set-wash-mop 17.40
    dry_mop_action: Action | None = None      # set-dry-mop 17.41
    collect_dust_action: Action | None = None # set-collect-dust 17.42
    # mapping aids (no in-params)
    fast_building_action: Action | None = None    # fast-building 17.10
    clean_building_action: Action | None = None   # clean-building 17.11
    # manual drive (17.49 via control-order 62: 0 stop,1 up,2 left,3 right,4 down)
    manual_control: Action | None = None
    counts: dict[str, int] = field(default_factory=dict)   # label -> raw (17.28)
    routes: dict[str, int] = field(default_factory=dict)   # label -> raw (17.50)
    start_in: tuple = ()                # e.g. (1, "[]") — full clean
    resume_in: tuple = ()               # e.g. (8,) — continue after pause
    pause_in: tuple = ()                # e.g. (7,) — robot-set-status pause
    needs_stop_before_start: bool = False


@dataclass(frozen=True)
class XtlConsumablesCapability:
    """xtl.vacuum.xm2216: ALL consumables live in one JSON string prop
    (17.32) shaped [{"type": str, "used": 0-100, "mode": 0|1|2}...]; shown
    life = 100 - used. Reset takes the consumable TYPE STRING as the sole
    in-param (17.20 via piid 33), not an int id."""
    consumables: Prop | None = None
    reset_action: Action | None = None


@dataclass(frozen=True)
class XtlMapCapability:
    """xtl.vacuum.xm2216 maps: no upload slots, no local raster. get-map-data
    (17.12) answers on out piid 38 with a KS3 *file name*; the JSON blob lives
    on the Mi cloud (fetched via get_interim_file_url_pro, decoded in-process
    by xtl_map.py). Fresh uploads additionally push MIoT events siid 17 /
    eiid 1 (map-data-report, payload carries the same file name).

    Multi-map: get-map-infos (17.13, out piid 37 JSON string) and switch-map
    (17.9, in piid 58) feed the same [{name,id,cur}] map-list plumbing as the
    MapCapability brands, so the shared Active Map select works unchanged."""
    get_obj_name: Action | None = None
    get_map_list: Action | None = None
    set_current_map: Action | None = None


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    brand: str
    core: CoreCapability | None = None
    # Each rich slot holds whichever brand shape the spec mapped (ijai-shaped or
    # the dreame-native variant); consumers isinstance-check where they care.
    map: MapCapability | DreameMapCapability | XtlMapCapability | None = None
    room_clean: RoomCleanCapability | None = None
    schedule: ScheduleCapability | DreameScheduleCapability | None = None
    settings: SettingsCapability | DreameSettingsCapability | None = None
    consumables: ConsumablesCapability | DreameConsumablesCapability | XtlConsumablesCapability | None = None
    clean_history: CleanHistoryCapability | DreameCleanHistoryCapability | None = None
    dnd: DndCapability | DreameDndCapability | None = None
    voice: VoiceCapability | DreameAudioCapability | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    # Cap on properties per get_properties call; None = send all at once.
    # Set to a small value for devices that reject large batches.
    max_properties: int | None = None
