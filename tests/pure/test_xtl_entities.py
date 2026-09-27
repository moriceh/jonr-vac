"""Pure tests for the xtl entity-parity layer (spec v4 siid 17).

Locks the prop<->action wiring against the official MIoT spec, the plugin
DND JSON envelope, the enlarged poll and the device setters — plus an
AST-level consistency check of the platform tables (switch/select/button)
against the device API, so a rename cannot ship silently.
"""
from __future__ import annotations

import ast
import dataclasses
import gzip
import hashlib
import importlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from .helpers import FakeMiotDevice, load_device_module

PKG = Path(__file__).resolve().parents[2] / "custom_components" / "jonr_vac"


def _load(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.XtlVacuumDevice("host", "token", "xtl.vacuum.xm2216")
    xtl = importlib.import_module("jonr_vac.spec.profiles.xtl")
    return device_mod, device, xtl


def _calls():
    return FakeMiotDevice.instances[-1].calls


def _in(piid: int, value):
    return {"did": "-", "piid": piid, "value": value}


def _table(module: str, name: str):
    """Literal value of a module-level constant, read via AST (no HA needed)."""
    tree = ast.parse((PKG / module).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") == name:
                    return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {module}")


# --- spec wiring -------------------------------------------------------------

# (core attr, prop siid/piid, action aiid, action in_piid) — spec v4 table.
EXPECTED = [
    ("volume", 15, 31, 15),
    ("child_lock", 6, 36, 6),
    ("disturb", 1, 24, 1),
    ("break_point", 2, 25, 2),
    ("carpet_boost", 7, 34, 7),
    ("auto_drying", 21, 48, 21),
    ("auto_solution", 44, 39, 44),
    ("carpet_twice", 74, 55, 74),
    ("carpet_first", 75, 56, 75),
    ("erp", 42, 38, 42),
    ("mop_augment", 43, 35, 43),
    ("carpet_prefer", 11, 33, 11),
    ("dust_collection", 20, 32, 20),
    ("drying_time", 22, 43, 22),
    ("mop_wash_freq", 23, 44, 23),
    ("mop_wash_temp", 45, 45, 45),
]


@pytest.mark.parametrize("attr,piid,aiid,in_piid", EXPECTED)
def test_setting_prop_and_action_match_spec(monkeypatch, attr, piid, aiid, in_piid):
    _, device, _ = _load(monkeypatch)
    c = device.core

    prop = getattr(c, attr)
    action = getattr(c, f"{attr}_action")
    assert (prop.siid, prop.piid) == (17, piid)
    assert (action.siid, action.aiid, action.in_piid) == (17, aiid, in_piid)


def test_disturb_time_reads_19_writes_18(monkeypatch):
    """The DND asymmetry: prop 17.19 (read+notify) vs in-piid 18 on 17.30."""
    _, device, _ = _load(monkeypatch)

    assert (device.core.disturb_time.siid, device.core.disturb_time.piid) == (17, 19)
    act = device.core.disturb_time_action
    assert (act.siid, act.aiid, act.in_piid) == (17, 30, 18)


def test_voice_language_wires_prop_25_action_21(monkeypatch):
    """17.25 language-voice (uint8 enum) is written through 17.21
    switch-voice-lang — in_piid 61 (voice-lang-info, a string)."""
    _, device, _ = _load(monkeypatch)

    assert (device.core.voice_language.siid,
            device.core.voice_language.piid) == (17, 25)
    act = device.core.voice_language_action
    assert (act.siid, act.aiid, act.in_piid) == (17, 21, 61)


@pytest.mark.parametrize("attr,piid", [
    ("session_time", 26), ("session_area", 27), ("total_time", 31),
    ("total_area", 29), ("total_count", 30), ("clean_records", 46),
    ("clean_water_cistern", 51), ("drain_cistern", 52),
    ("dust_bag_state", 53), ("mop_tank_state", 54), ("return_status", 48),
    ("language_ver", 78),
])
def test_readonly_stat_props_match_spec(monkeypatch, attr, piid):
    _, device, _ = _load(monkeypatch)

    assert (getattr(device.core, attr).siid, getattr(device.core, attr).piid) == (17, piid)


@pytest.mark.parametrize("attr,aiid,in_piid", [
    ("wash_mop_action", 40, 5), ("dry_mop_action", 41, 4),
    ("collect_dust_action", 42, 3), ("manual_control", 49, 62),
])
def test_station_action_frames_match_spec(monkeypatch, attr, aiid, in_piid):
    _, device, _ = _load(monkeypatch)
    act = getattr(device.core, attr)

    assert (act.siid, act.aiid, act.in_piid) == (17, aiid, in_piid)


def test_building_actions_take_no_params(monkeypatch):
    _, device, _ = _load(monkeypatch)

    for attr, aiid in (("fast_building_action", 10), ("clean_building_action", 11)):
        act = getattr(device.core, attr)
        assert (act.siid, act.aiid) == (17, aiid)
        assert act.in_piid is None and not act.in_piids


@pytest.mark.parametrize("table,expected", [
    ("carpet_prefers", {"adaptive": 0, "evade": 1, "carpet_only": 2, "ignore": 3}),
    ("dust_collections", {"none": 0, "every_clean": 1, "medium": 3}),
    ("drying_times", {"120": 120, "180": 180, "240": 240}),
    ("mop_wash_freqs", {"5": 5, "10": 10, "15": 15}),
    # The spec's full language-voice enum (17.25 codes); packs not yet
    # installed read back unchanged, which the select shows honestly.
    ("voice_languages", {"chinese": 1, "english": 2, "russian": 3, "german": 4,
                         "italian": 5, "french": 6, "polish": 7, "spanish": 8,
                         "korean": 9, "chinese_tw": 10, "vietnamese": 11}),
])
def test_select_tables_match_spec_value_lists(monkeypatch, table, expected):
    _, device, _ = _load(monkeypatch)

    assert getattr(device.core, table) == expected


def test_poll_is_chunked_for_the_enlarged_prop_set(monkeypatch):
    _, device, _ = _load(monkeypatch)

    assert device.profile.max_properties == 10


# --- DND payload / parsing ----------------------------------------------------

def test_dnd_payload_exact_live_string(monkeypatch):
    _, _, xtl = _load(monkeypatch)

    assert xtl.xtl_dnd_payload(22, 0, 8, 0) == (
        '{"startHour":22,"startMin":0,"endHour":8,"endMin":0,"notDust":0,"notDry":0}'
    )
    assert xtl.xtl_dnd_payload(13, 30, 14, 45, no_dust=True, no_dry=True) == (
        '{"startHour":13,"startMin":30,"endHour":14,"endMin":45,'
        '"notDust":1,"notDry":1}'
    )


def test_dnd_label_formats_live_readback(monkeypatch):
    _, _, xtl = _load(monkeypatch)

    live = '{"startHour":22,"startMin":0,"endHour":8,"endMin":0,"notDust":0,"notDry":0}'
    assert xtl.xtl_dnd_label(live) == "22:00 - 08:00"
    assert xtl.xtl_dnd_label({"startHour": 7, "startMin": 5,
                              "endHour": 9, "endMin": 0}) == "07:05 - 09:00"
    assert xtl.xtl_dnd_label("nope") is None
    assert xtl.xtl_dnd_label(None) is None
    assert xtl.xtl_dnd_label('{"startHour":22}') is None


def test_parse_records_tolerates_malformed_json(monkeypatch):
    _, _, xtl = _load(monkeypatch)

    assert xtl.xtl_parse_records('[{"a": 1}]') == [{"a": 1}]
    assert xtl.xtl_parse_records([1, 2]) == [1, 2]
    assert xtl.xtl_parse_records("{broken") == []
    assert xtl.xtl_parse_records(None) == []


def test_records_count_separates_never_reported_from_empty(monkeypatch):
    """`unknown` (None) when the robot never answered 17.46 — the P20 Pro's
    live behaviour — vs an honest 0 when it reported an empty list. len() on
    the parse alone (the pre-fix sensor) could not tell the two apart."""
    _, _, xtl = _load(monkeypatch)

    assert xtl.xtl_records_count(None) is None            # never reported
    assert xtl.xtl_records_count("") == 0                 # reported "nothing"
    assert xtl.xtl_records_count("[]") == 0
    assert xtl.xtl_records_count([]) == 0
    assert xtl.xtl_records_count('[{"d": "2026-09-25"}]') == 1
    assert xtl.xtl_records_count([{"d": "x"}, {"d": "y"}]) == 2
    assert xtl.xtl_records_count("{broken") == 0          # malformed -> counted empty


# --- scheduled-cleanings list (17.17) ------------------------------------------

# Live read of the user robot's 17.17 (probe 2026-09-25): whole-map weekday
# timer + a rooms timer on map 3.
TIMERS_LIVE = (
    '[{"id":"1","on":1,"state":0,"type":"1","repeat":"11111100","hour":10,'
    '"min":0,"workMode":1,"fanMode":3,"waterMode":1,"cleanCount":1,'
    '"routePrefer":1},'
    '{"id":"2","on":1,"state":0,"type":"2","repeat":"11010100","hour":11,'
    '"min":0,"workMode":0,"fanMode":1,"waterMode":1,"cleanCount":1,"mapId":3,'
    '"cleanValues":[3,5,6],"routePrefer":1}]'
)


def test_parse_timers_tolerates_malformed_json(monkeypatch):
    _, _, xtl = _load(monkeypatch)

    assert len(xtl.xtl_parse_timers(TIMERS_LIVE)) == 2
    assert xtl.xtl_parse_timers(TIMERS_LIVE)[1]["cleanValues"] == [3, 5, 6]
    assert xtl.xtl_parse_timers("{broken") == []
    assert xtl.xtl_parse_timers(None) == []


def test_timers_summary_live_string(monkeypatch):
    _, _, xtl = _load(monkeypatch)

    # default vocabulary is the English table; the sensor injects the user's
    # HA language table (the FR calibration lives in
    # test_timers_summary_french_labels below)
    assert xtl.xtl_timers_summary(TIMERS_LIVE) == (
        "mon→fri 10:00 (whole home, vacuum only, turbo)"
        " · mon/wed/fri 11:00 (rooms 3/5/6, vacuum+mop, standard,"
        " medium wetness)"
    )
    # dict list (push shape) formats identically to the JSON string
    assert xtl.xtl_timers_summary(xtl.xtl_parse_timers(TIMERS_LIVE)) == (
        xtl.xtl_timers_summary(TIMERS_LIVE)
    )


def test_timers_summary_french_labels(monkeypatch):
    """The 2026-09-25 calibration against the user's two live Mi Home
    plannings, pinned through the injected French label table (the path the
    sensor takes when hass.config.language starts with 'fr')."""
    _, _, xtl = _load(monkeypatch)

    fr = xtl.XTL_TIMER_LABELS_FR
    assert xtl.xtl_timers_summary(TIMERS_LIVE, None, fr) == (
        "lun→ven 10:00 (toute la maison, aspiration seule, turbo)"
        " · lun/mer/ven 11:00 (pièces 3/5/6, asp.+lavage, standard,"
        " humidité moyenne)"
    )
    assert xtl.xtl_timer_labels("fr") is fr
    assert xtl.xtl_timer_labels("fr-FR") is fr
    assert xtl.xtl_timer_labels("en") is xtl.XTL_TIMER_LABELS_EN
    assert xtl.xtl_timer_labels(None) is xtl.XTL_TIMER_LABELS_EN


def test_timers_summary_room_names(monkeypatch):
    _, _, xtl = _load(monkeypatch)

    rooms = {"3": {"3": "Salon", "5": "Cuisine", "6": "Bureau"}}
    assert xtl.xtl_timers_summary(TIMERS_LIVE, rooms) == (
        "mon→fri 10:00 (whole home, vacuum only, turbo)"
        " · mon/wed/fri 11:00 (rooms Salon/Cuisine/Bureau, vacuum+mop,"
        " standard, medium wetness)"
    )
    # other maps' buckets still resolve, unknown ids keep their number
    assert xtl.xtl_timers_summary(TIMERS_LIVE, {"9": {"3": "Salon"}}) == (
        "mon→fri 10:00 (whole home, vacuum only, turbo)"
        " · mon/wed/fri 11:00 (rooms Salon/5/6, vacuum+mop, standard,"
        " medium wetness)"
    )
    # the timer's own mapId bucket wins over other maps on id collisions
    assert xtl.xtl_timers_summary(TIMERS_LIVE, {
        "3": {"3": "Salon"}, "9": {"3": "Cuisine"},
    }) == (
        "mon→fri 10:00 (whole home, vacuum only, turbo)"
        " · mon/wed/fri 11:00 (rooms Salon/5/6, vacuum+mop, standard,"
        " medium wetness)"
    )
    # empty/garbage rooms behave like None (numeric fallback)
    assert xtl.xtl_timers_summary(TIMERS_LIVE, {}) == (
        xtl.xtl_timers_summary(TIMERS_LIVE, None)
    )


def test_timers_summary_map_name(monkeypatch):
    """The user-requested clause (2026-09-26): Mi Home's timer editor shows
    which floor a room timer cleans — the summary must name it. Whole-home
    timers (no mapId) stay unnamed."""
    _, _, xtl = _load(monkeypatch)

    maps = {3: "Appart Brest", 1: "Map 1"}
    assert xtl.xtl_timers_summary(TIMERS_LIVE, None, None, maps) == (
        "mon→fri 10:00 (whole home, vacuum only, turbo)"
        " · mon/wed/fri 11:00 (map Appart Brest, rooms 3/5/6, vacuum+mop,"
        " standard, medium wetness)"
    )
    fr = xtl.XTL_TIMER_LABELS_FR
    assert xtl.xtl_timers_summary(TIMERS_LIVE, None, fr, maps) == (
        "lun→ven 10:00 (toute la maison, aspiration seule, turbo)"
        " · lun/mer/ven 11:00 (carte Appart Brest, pièces 3/5/6,"
        " asp.+lavage, standard, humidité moyenne)"
    )
    # unknown map (no name yet) -> no map clause, rooms clause alone
    assert xtl.xtl_timers_summary(TIMERS_LIVE, None, None, {}) == (
        xtl.xtl_timers_summary(TIMERS_LIVE, None, None, None)
    )


def test_timers_summary_edge_cases(monkeypatch):
    _, _, xtl = _load(monkeypatch)

    assert xtl.xtl_timers_summary(None) is None
    assert xtl.xtl_timers_summary("garbage") is None
    assert xtl.xtl_timers_summary("[]") is None
    # live layout (Mi Home cross-check): char 0 recurring flag ('0' = the
    # run is unique/one-shot), chars 1..7 = mon..sun
    assert xtl.xtl_timers_summary('[{"hour": 8, "min": 30, "repeat": "1"}]') == (
        "? 08:30"
    )
    assert xtl.xtl_timers_summary(
        '[{"hour": 8, "min": 30, "repeat": "11000000"}]'
    ) == "mon 08:30"
    # last char = sunday; the recurring flag is respected (char 0 = '1')
    assert xtl.xtl_timers_summary(
        '[{"hour": 22, "min": 30, "repeat": "10000001"}]'
    ) == "sun 22:30"
    assert xtl.xtl_timers_summary(
        '[{"hour": 8, "min": 0, "repeat": "11111111", "on": 1}]'
    ) == "every day 08:00"
    assert xtl.xtl_timers_summary(
        '[{"hour": 8, "min": 0, "repeat": "11000000", "on": 0}]'
    ) == "mon 08:00 (disabled)"
    # live one-shot timer 2026-09-25: flag '0' + the run's weekday (friday)
    assert xtl.xtl_timers_summary(
        '[{"hour": 8, "min": 30, "repeat": "01000000"}]'
    ) == "one-time mon 08:30"
    assert xtl.xtl_timers_summary(
        '[{"hour": 22, "min": 8, "repeat": "00000100", "type": "1",'
        ' "workMode": 0, "fanMode": 1, "waterMode": 1, "cleanCount": 1,'
        ' "routePrefer": 1}]'
    ) == ("one-time fri 22:08 (whole home, vacuum+mop, standard,"
         " medium wetness)")
    # type 2 without usable cleanValues -> no rooms clause
    assert xtl.xtl_timers_summary(
        '[{"hour": 9, "min": 15, "repeat": "10000001", "type": "2"}]'
    ) == "sun 09:15"
    # timer without hour/min is skipped, not crashed on
    assert xtl.xtl_timers_summary('[{"repeat": "1111111"}]') is None
    # multi-pass clause + FR vocabulary round-trip
    assert xtl.xtl_timers_summary(
        '[{"hour": 7, "min": 0, "repeat": "11010100", "cleanCount": 3}]',
        None, xtl.XTL_TIMER_LABELS_FR,
    ) == "lun/mer/ven 07:00 (3 passes)"


DND_FLAGS_ON = ('{"startHour":22,"startMin":0,"endHour":8,"endMin":0,'
                '"notDust":1,"notDry":0}')


def test_parse_dnd_accepts_str_and_dict(monkeypatch):
    _, _, xtl = _load(monkeypatch)

    assert xtl.xtl_parse_dnd(DND_LIVE) == {
        "startHour": 22, "startMin": 0, "endHour": 8,
        "endMin": 0, "notDust": 0, "notDry": 0}
    assert xtl.xtl_parse_dnd({"notDust": 1}) == {"notDust": 1}
    assert xtl.xtl_parse_dnd("nope") is None
    assert xtl.xtl_parse_dnd(None) is None
    assert xtl.xtl_parse_dnd("[1,2]") is None


def test_dnd_merged_window_edit_keeps_flags(monkeypatch):
    """The text entity's rule: touching the window must not silently clear
    the app's NPD check-boxes (and vice versa)."""
    _, _, xtl = _load(monkeypatch)

    assert xtl.xtl_dnd_merged(DND_FLAGS_ON, start_hour=23, start_min=0,
                              end_hour=7, end_min=30) == (
        '{"startHour":23,"startMin":0,"endHour":7,"endMin":30,'
        '"notDust":1,"notDry":0}'
    )
    assert xtl.xtl_dnd_merged(DND_LIVE, no_dust=True) == (
        '{"startHour":22,"startMin":0,"endHour":8,"endMin":0,'
        '"notDust":1,"notDry":0}'
    )
    assert xtl.xtl_dnd_merged(DND_FLAGS_ON, no_dust=False) == (
        '{"startHour":22,"startMin":0,"endHour":8,"endMin":0,'
        '"notDust":0,"notDry":0}'
    )


def test_dnd_merged_defaults_without_readback(monkeypatch):
    """No 17.19 read-back yet (or a broken one) -> the app's shipped defaults."""
    _, _, xtl = _load(monkeypatch)

    for broken in (None, "nope", 42):
        assert xtl.xtl_dnd_merged(broken, no_dry=True) == (
            '{"startHour":22,"startMin":0,"endHour":8,"endMin":0,'
            '"notDust":0,"notDry":1}'
        )


def test_dnd_same_compares_parsed(monkeypatch):
    """Key order and spacing never look like a change; an unusable side
    never compares equal (the caller decides what unknown means)."""
    _, _, xtl = _load(monkeypatch)

    assert xtl.xtl_dnd_same(
        DND_LIVE,
        '{"notDry":0,"notDust":0,"endMin":0,"endHour":8,"startMin":0,"startHour":22}')
    assert not xtl.xtl_dnd_same(DND_LIVE, DND_FLAGS_ON)
    assert not xtl.xtl_dnd_same(DND_LIVE, "nope")
    assert not xtl.xtl_dnd_same(None, DND_LIVE)


def test_dnd_from_push_normalises_broker_dict(monkeypatch):
    """The broker hands properties_changed(17/19) over already parsed (live
    probe 2026-09-25); the fold needs the canonical compact write shape."""
    _, _, xtl = _load(monkeypatch)

    assert xtl.xtl_dnd_from_push(
        {"startHour": 1, "startMin": 0, "endHour": 2, "endMin": 0,
         "notDust": 1, "notDry": 1}) == (
        '{"startHour":1,"startMin":0,"endHour":2,"endMin":0,'
        '"notDust":1,"notDry":1}')
    assert xtl.xtl_dnd_from_push(DND_LIVE) == DND_LIVE   # raw string tolerated
    assert xtl.xtl_dnd_from_push("nope") is None
    assert xtl.xtl_dnd_from_push(42) is None


# --- map-infos catalogue parsing (map titles) ----------------------------------

def test_parse_map_infos_file_reads_user_titles(monkeypatch):
    """The map-infos-report (eiid 2) KS3 file is the ONLY source of the
    app-typed map names (decompiled plugin: setMapInfos titles its list
    with mapInfo.name); the local 17.13 answer carries none."""
    _, _, xtl = _load(monkeypatch)

    live = [
        {"mapId": 3, "name": "Appart brest", "saved": 1, "status": 1},
        {"mapId": 4, "name": "", "saved": 1},          # blank drops out
        {"mapId": 5, "saved": 0},                       # absent drops out
        {"name": "sans-id"},                            # no mapId drops out
        "junk",
    ]
    assert xtl.xtl_parse_map_infos_file(json.dumps(live)) == {3: "Appart brest"}
    # bytes, bare list and dict-wrapped forms all land on the same answer.
    assert xtl.xtl_parse_map_infos_file(
        json.dumps({"data": live}).encode("utf-8")) == {3: "Appart brest"}
    assert xtl.xtl_parse_map_infos_file(live) == {3: "Appart brest"}
    for junk in (None, 42, "nope", b"\xff\xfe", "{}", "[]"):
        assert xtl.xtl_parse_map_infos_file(junk) == {}


def test_parse_map_infos_catalog_lists_saved_maps(monkeypatch):
    """The Active Map select bug (2026-09-26): the map-infos file is also
    the switchable-map catalogue (plugin mapStore.mapInfos, saved===1,
    cur from status===1, mapId 0 = the unsaved live map dropped)."""
    _, _, xtl = _load(monkeypatch)

    live = [
        {"mapId": 0, "name": "live", "saved": 0, "status": 0},   # drop: unsaved
        {"mapId": 3, "name": "Appart brest", "saved": 1, "status": 1},
        {"mapId": 4, "name": "  ", "saved": 1},                  # blank -> None
        {"mapId": 5, "name": "Local", "saved": 0},               # drop: not saved
        {"mapId": 6, "name": "Garage"},                          # saved-absent ok
        {"name": "sans-id"}, "junk", 42,
    ]
    assert xtl.xtl_parse_map_infos_catalog(json.dumps(live)) == [
        {"name": "Appart brest", "id": 3, "cur": 1},
        {"name": None, "id": 4, "cur": 0},
        {"name": "Garage", "id": 6, "cur": 0},
    ]
    # bytes / dict-wrapped land the same; junk is [] (caller keeps last good).
    assert xtl.xtl_parse_map_infos_catalog(
        json.dumps({"data": live}).encode("utf-8")) == \
        xtl.xtl_parse_map_infos_catalog(json.dumps(live))
    for junk in (None, 42, "nope", b"\xff\xfe", "{}", "[]"):
        assert xtl.xtl_parse_map_infos_catalog(junk) == []


def test_add_known_maps_appends_catalogue_behind_active(monkeypatch):
    """The blob-synthesised active entry keeps cur=1 and leads; the
    catalogue's other floors join cur=0 (a lagging status===1 must never
    produce a second current row); ids already listed are never doubled.
    Catalogue rows are switchable (saved=1); the blob row stays saved=0
    until the file vouches for its id (then it is promoted)."""
    _, _, xtl = _load(monkeypatch)

    blob = [{"name": None, "id": 3, "cur": 1, "saved": 0}]
    catalog = [
        {"name": "Appart brest", "id": 3, "cur": 1},   # claims blob id -> saved
        {"name": "Local atelier", "id": 4, "cur": 0},
        {"name": "Étage", "id": 5, "cur": 1},           # cur forced to 0
    ]
    assert xtl.xtl_add_known_maps(blob, catalog) == [
        {"name": None, "id": 3, "cur": 1, "saved": 1},
        {"name": "Local atelier", "id": 4, "cur": 0, "saved": 1},
        {"name": "Étage", "id": 5, "cur": 0, "saved": 1},
    ]
    # empty catalogue = the old single-map behaviour, untouched
    assert xtl.xtl_add_known_maps(blob, []) == blob
    assert xtl.xtl_add_known_maps([], []) == []
    # a blob row the file does NOT claim stays unsaved (never a switch target)
    blob1 = [{"name": None, "id": 1, "cur": 1, "saved": 0}]
    assert xtl.xtl_add_known_maps(blob1, [
        {"name": "Bas", "id": 7, "cur": 0},
        {"name": "Étage", "id": 9, "cur": 0},
    ]) == [
        {"name": None, "id": 1, "cur": 1, "saved": 0},
        {"name": "Bas", "id": 7, "cur": 0, "saved": 1},
        {"name": "Étage", "id": 9, "cur": 0, "saved": 1},
    ]


def test_add_known_maps_demotes_the_unsaved_live_copy(monkeypatch):
    """Live 2026-09-26: the robot keeps uploading its working copy under
    mapHeadId 0/1 (the plugin's unsaved draft — never in the app's switch
    list) even while a SAVED map is in use. The catalogue's status===1 row
    must then own cur, or the select highlights "Map 0" where Mi Home
    highlights the saved floor."""
    _, _, xtl = _load(monkeypatch)

    blob = [{"name": None, "id": 0, "cur": 1, "saved": 0}]
    catalog = [{"name": "Appart Brest", "id": 3, "cur": 1}]
    assert xtl.xtl_add_known_maps(blob, catalog) == [
        {"name": None, "id": 0, "cur": 0, "saved": 0},
        {"name": "Appart Brest", "id": 3, "cur": 1, "saved": 1},
    ]
    # Live 2026-09-26: this firmware NEVER writes status==1 (the map-infos
    # file's only saved row was status 0 while the robot ran on it). With
    # exactly one saved map that map owns the draft — the plugin's own
    # isMapSaved (saved===1 || curMapInfo.mapId != 0) says the same.
    assert xtl.xtl_add_known_maps(blob, [{"name": "Appart Brest", "id": 3,
                                          "cur": 0}]) == [
        {"name": None, "id": 0, "cur": 0, "saved": 0},
        {"name": "Appart Brest", "id": 3, "cur": 1, "saved": 1},
    ]
    # Two saved maps and no status flag: nothing identifies the floor, so
    # the draft row keeps cur — the honest "still the live copy" answer
    # (the select then lists the two saved floors without a current).
    assert xtl.xtl_add_known_maps(blob, [{"name": "Bas", "id": 7, "cur": 0},
                                         {"name": "Étage", "id": 9,
                                          "cur": 0}]) == [
        {"name": None, "id": 0, "cur": 1, "saved": 0},
        {"name": "Bas", "id": 7, "cur": 0, "saved": 1},
        {"name": "Étage", "id": 9, "cur": 0, "saved": 1},
    ]


def test_apply_map_names_overlays_placeholders_only(monkeypatch):
    _, _, xtl = _load(monkeypatch)
    names = {3: "Appart brest", 4: "Local atelier"}

    # xtl's synthesised list carries name=None; "Map N" placeholders upgrade.
    meta = [{"name": None, "id": 3, "cur": 1}, {"name": "Map 4", "id": 4, "cur": 0}]
    assert xtl.xtl_apply_map_names(meta, names) == [
        {"name": "Appart brest", "id": 3, "cur": 1},
        {"name": "Local atelier", "id": 4, "cur": 0},
    ]
    # A real (non-placeholder) name from another source is never clobbered;
    # unknown ids and an empty name table pass through untouched.
    other = [{"name": "Donnée par le firmware", "id": 3, "cur": 1},
             {"name": None, "id": 9, "cur": 0}]
    assert xtl.xtl_apply_map_names(other, names) == other
    assert xtl.xtl_apply_map_names(other, {}) == other


# --- status() parsing ----------------------------------------------------------

DND_LIVE = '{"startHour":22,"startMin":0,"endHour":8,"endMin":0,"notDust":0,"notDry":0}'


def test_status_parses_entity_parity_props(monkeypatch):
    device_mod, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = {
        (2, 2): 9,                      # status: Charging
        (17, 15): 79,                   # volume (live-verified scale)
        (17, 6): 1, (17, 1): 0, (17, 2): 1, (17, 7): 1, (17, 21): 0,
        (17, 44): 1, (17, 74): 1, (17, 75): 0, (17, 42): 0, (17, 43): 0,
        (17, 11): 2, (17, 20): 1, (17, 22): 180, (17, 23): 10, (17, 45): 1,
        (17, 19): DND_LIVE,
        (17, 26): 1200, (17, 27): 45,   # session time s / area m2
        (17, 31): 36000, (17, 29): 210, (17, 30): 88,   # lifetime totals
        (17, 46): '[{"room":"salon"}]',
        (17, 17): TIMERS_LIVE,
        (17, 51): 3, (17, 52): 1, (17, 53): 0, (17, 54): 1, (17, 48): 2,
    }

    status = device.status()

    assert status.volume_raw == 79
    assert status.child_lock_raw == 1 and status.disturb_raw == 0
    assert status.break_point_raw == 1 and status.carpet_boost_raw == 1
    assert status.auto_drying_raw == 0 and status.auto_solution_raw == 1
    assert status.carpet_twice_raw == 1 and status.carpet_first_raw == 0
    assert status.erp_raw == 0 and status.mop_augment_raw == 0
    assert status.carpet_prefer_raw == 2 and status.dust_collection_raw == 1
    assert status.drying_time_raw == 180 and status.mop_wash_freq_raw == 10
    assert status.mop_wash_temp_raw == 1
    assert status.disturb_time_raw == DND_LIVE
    assert status.clean_time == 1200 and status.clean_area == 45
    assert status.total_clean_time == 36000
    assert status.total_clean_area == 210 and status.total_clean_count == 88
    assert status.clean_records_raw == '[{"room":"salon"}]'
    assert status.clean_timers_raw == TIMERS_LIVE
    assert status.clean_water_cistern_raw == 3
    assert status.drain_cistern_raw == 1 and status.dust_bag_state_raw == 0
    assert status.mop_tank_state_raw == 1 and status.return_status_raw == 2
    # core read strictly (one 10-prop chunk) + extended in broad chunks
    # (the toggles/selects layout that verifiably answers live), stats in
    # 3-prop batches, the three JSON props read alone on turn 1
    bm = FakeMiotDevice.instances[-1].batch_max_properties
    assert bm[0] == 10 and max(bm) == 10      # core + the proven broad shape
    assert 3 in bm and 2 in bm                # stats group sliced to 3/2
    assert bm.count(1) == 3                   # DND + records + timers each solo


def test_quick_poll_reads_only_the_core_chunk(monkeypatch):
    """First-refresh fast path: one 10-prop batch, extended fields left None."""
    device_mod, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = {(2, 2): 9, (17, 15): 79, (17, 6): 1}

    status = device.status(full=False)

    gets = [c for c in FakeMiotDevice.instances[-1].calls if c[0] == "get"]
    assert 0 < len(gets) <= 10                  # one single core chunk
    assert (17, 6) not in [(s, p) for _k, s, p in gets]  # child_lock not read
    assert status.raw_status == 9
    assert status.volume_raw == 79              # volume stays in the core group
    assert status.child_lock_raw is None


CORE_VALUES = {(2, 2): 9, (17, 15): 79, (17, 6): 1}


def test_core_only_confirm_poll_replays_last_known_ext(monkeypatch):
    """Regression 2026-09-25: the background confirm poll (core-only) used
    to build a fresh VacuumStatus whose ext fields were all None, dropping
    every settings/stats/station entity to unknown until the next full poll.
    The device now replays the ext values it last read."""
    device_mod, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = {
        **CORE_VALUES,
        (17, 14): 2,      # count (the ×N passes select)
        (17, 22): 180,    # drying time
        (17, 42): 1,      # erp
    }
    assert device.status().erp_raw == 1

    # Next poll is the core-only confirm: the ext props are not even asked.
    FakeMiotDevice.property_values = {**CORE_VALUES, (2, 2): 8}
    status = device.status(full=False)
    assert status.raw_status == 8                  # core stays freshly read
    assert status.count_raw == 2                   # ext replayed from cache
    assert status.drying_time_raw == 180
    assert status.erp_raw == 1


def test_mute_ext_chunk_keeps_last_known_values(monkeypatch):
    """A soft chunk that times out inside a FULL poll replays the last
    value it ever read instead of blanking the entity."""
    device_mod, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = {**CORE_VALUES, (17, 22): 180}
    assert device.status().drying_time_raw == 180

    FakeMiotDevice.property_values = {**CORE_VALUES,
                                       (17, 22): RuntimeError("timed out")}
    status = device.status()
    assert status.raw_status == 9
    assert status.drying_time_raw == 180            # replayed, not blanked


def test_action_write_updates_ext_cache_for_confirm_replay(monkeypatch):
    """xtl settings write through ACTIONS (17.24-56), never property sets:
    the ext replay cache must follow the write, or the confirm poll would
    replay the pre-write value and cancel the optimistic flip (e.g. the
    once/twice select reverting right after the click)."""
    device_mod, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = {**CORE_VALUES, (17, 14): 1}
    assert device.status().count_raw == 1           # "once" cached

    device.set_count("twice")                       # action 17.28 in_param 14
    status = device.status(full=False)
    assert status.count_raw == 2                    # the WRITTEN value survives
    calls = [c for c in _calls() if c[0] == "action"]
    assert calls and calls[-1][:3] == ("action", 17, 28)  # via set-clean-count
    assert calls[-1][3] == [_in(14, 2)]                    # in_piid 14, value 2


def test_apply_ext_push_updates_cache_for_confirm_replay(monkeypatch):
    """MQTT push path mirror: __init__ folds a pushed value into the snapshot
    AND calls device.apply_ext_push, so the debounced core-only confirm poll
    replays the PUSHED value from _ext_cache instead of the pre-change one
    (same cancel-the-fold trap as the action-write case above, 2026-09-25)."""
    device_mod, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = {**CORE_VALUES, (17, 21): 0}
    assert device.status().auto_drying_raw == 0      # auto-drying off, cached

    device.apply_ext_push(17, 21, 1)                 # Mi Home toggled it on
    status = device.status(full=False)
    assert status.auto_drying_raw == 1               # pushed value survives


def test_merge_preserved_ext_keeps_prev_only_where_new_is_empty(monkeypatch):
    """The coordinator-level guard: a poll must never blank a field it did
    not read; fields it DID read (freshly polled core) win over the previous
    snapshot, and the xtl consumable JSON default ((), {}) replays as a unit
    only for xtl."""
    device_mod, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = {**CORE_VALUES, (17, 14): 2, (17, 42): 0}
    base = device.status()

    prev = dataclasses.replace(
        base, battery=80, erp_raw=1, main_brush_life=90,
        consumable_types=("main", "filter"), extra_consumable_lives={"main": 80},
    )
    core_only = dataclasses.replace(  # what a core-only poll yields
        base, battery=55, erp_raw=None, main_brush_life=None,
        consumable_types=(), extra_consumable_lives=None, count_raw=None,
    )

    merged = device_mod.merge_preserved_ext(core_only, prev, brand="xtl")
    assert merged.battery == 55                     # freshly polled core wins
    assert merged.erp_raw == 1                      # ext kept from prev
    assert merged.main_brush_life == 90             # xtl-only field preserved
    assert merged.count_raw == 2                    # count read came from base
    assert merged.consumable_types == ("main", "filter")
    assert merged.extra_consumable_lives == {"main": 80}

    # Non-xtl brands genuinely have no consumable JSON: the empty tuple
    # default must survive the merge (the replay branch is xtl-only).
    prev_plain = dataclasses.replace(base, consumable_types=("main", "filter"))
    merged2 = device_mod.merge_preserved_ext(core_only, prev_plain, brand="dreame")
    assert merged2.consumable_types == ()           # brand gate holds
    assert merged2.erp_raw == 0                     # None-fields rule still holds
    assert device_mod.merge_preserved_ext(core_only, None, brand="xtl") is core_only


def test_extended_chunk_timeout_is_tolerated(monkeypatch):
    """A timing-out settings chunk must not take core telemetry down."""
    device_mod, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = {
        (2, 2): 9,
        (17, 19): RuntimeError("timed out"),   # poisons its whole soft chunk
        (17, 48): 2,                            # later chunk, must survive
    }

    status = device.status()

    assert status.raw_status == 9
    assert status.disturb_time_raw is None
    assert status.return_status_raw == 2


def test_mute_json_prop_does_not_sink_the_stats_reads(monkeypatch):
    """17.46 timing out must not cost us the numeric totals next to it."""
    device_mod, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = {
        (2, 2): 9,
        (17, 46): RuntimeError("timed out"),   # records, read alone
        (17, 19): DND_LIVE,
        (17, 26): 1200, (17, 31): 36000,
    }

    status = device.status()

    assert status.disturb_time_raw == DND_LIVE
    assert status.clean_time == 1200 and status.total_clean_time == 36000
    assert status.clean_records_raw is None    # the mute prop, dropped alone


def test_slow_json_props_persist_across_skipped_turns(monkeypatch):
    """Cadenced reads keep the last firmware value when not (re)read."""
    device_mod, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = {
        (2, 2): 9, (17, 19): DND_LIVE,
        (17, 46): '[{"room":"salon"}]',
    }

    status = device.status()                      # turn 1: both read fresh
    assert status.disturb_time_raw == DND_LIVE
    assert status.clean_records_raw == '[{"room":"salon"}]'

    # Firmware goes mute on both JSON props afterwards; later turns skip or
    # fail the read, and the cached values must survive (no sensor flicker).
    FakeMiotDevice.property_values = {
        (2, 2): 9, (17, 19): RuntimeError("timed out"),
        (17, 46): RuntimeError("timed out"),
    }
    for _ in range(3):
        status = device.status()
        assert status.disturb_time_raw == DND_LIVE
        assert status.clean_records_raw == '[{"room":"salon"}]'


def test_core_chunk_failure_still_raises(monkeypatch):
    """The strict core read keeps the old hard-failure contract."""
    device_mod, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = {(2, 2): RuntimeError("timed out")}

    with pytest.raises(device_mod.DeviceCommunicationError):
        device.status()


def test_new_status_fields_default_to_none(monkeypatch):
    device_mod, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = {(2, 2): 1}

    status = device.status()

    for attr in ("child_lock_raw", "disturb_time_raw", "total_clean_time",
                 "clean_records_raw", "clean_water_cistern_raw",
                 "return_status_raw", "carpet_prefer_raw"):
        assert getattr(status, attr) is None


# --- setters: action frames ----------------------------------------------------

@pytest.mark.parametrize("method,arg,aiid,piid,value", [
    ("set_child_lock", True, 36, 6, True),
    ("set_child_lock", False, 36, 6, False),
    ("set_disturb", True, 24, 1, True),
    ("set_break_point", True, 25, 2, True),
    ("set_carpet_boost", True, 34, 7, True),
    ("set_auto_drying", False, 48, 21, False),
    ("set_auto_solution", True, 39, 44, True),
    ("set_carpet_twice", True, 55, 74, True),
    ("set_carpet_first", False, 56, 75, False),
    ("set_erp", True, 38, 42, True),
    ("set_mop_augment", True, 35, 43, True),
    ("set_carpet_prefer", "carpet_only", 33, 11, 2),
    ("set_dust_collection", "every_clean", 32, 20, 1),
    ("set_drying_time", "180", 43, 22, 180),
    ("set_mop_wash_freq", "15", 44, 23, 15),
    ("set_volume", 79, 31, 15, 79),
])
def test_setting_setter_frames(monkeypatch, method, arg, aiid, piid, value):
    _, device, _ = _load(monkeypatch)

    getattr(device, method)(arg)

    assert _calls() == [("action", 17, aiid, [_in(piid, value)])]


@pytest.mark.parametrize("method,aiid,piid,value", [
    ("xtl_wash_mop", 40, 5, True),
    ("xtl_dry_mop", 41, 4, True),
    ("xtl_collect_dust", 42, 3, True),
    # Stops are the same action with false, exactly like the plugin's
    # "Stop washing/drying/emptying" buttons.
    ("xtl_stop_wash_mop", 40, 5, False),
    ("xtl_stop_dry_mop", 41, 4, False),
    ("xtl_stop_collect_dust", 42, 3, False),
])
def test_station_one_shot_frames(monkeypatch, method, aiid, piid, value):
    _, device, _ = _load(monkeypatch)

    getattr(device, method)()

    assert _calls() == [("action", 17, aiid, [_in(piid, value)])]


def test_building_actions_send_empty_params(monkeypatch):
    _, device, _ = _load(monkeypatch)

    device.xtl_fast_building()
    device.xtl_clean_building()

    assert _calls() == [("action", 17, 10, []), ("action", 17, 11, [])]


@pytest.mark.parametrize("command", [0, 1, 2, 3, 4])
def test_manual_control_frames(monkeypatch, command):
    _, device, _ = _load(monkeypatch)

    device.xtl_manual_control(command)

    assert _calls() == [("action", 17, 49, [_in(62, command)])]


@pytest.mark.parametrize("command", [-1, 5, 99])
def test_manual_control_rejects_out_of_range(monkeypatch, command):
    _, device, _ = _load(monkeypatch)

    with pytest.raises(ValueError):
        device.xtl_manual_control(command)
    assert _calls() == []


def test_dnd_send_wraps_json_in_piid18(monkeypatch):
    _, device, xtl = _load(monkeypatch)

    device.xtl_send_dnd_time([xtl.xtl_dnd_payload(22, 0, 8, 0)])

    assert _calls() == [("action", 17, 30, [_in(
        18, '{"startHour":22,"startMin":0,"endHour":8,"endMin":0,'
            '"notDust":0,"notDry":0}')])]


def test_dnd_send_mirrors_into_ext_cache(monkeypatch):
    """disturb_time (17.19) is an ext-group prop: the core-only confirm poll
    replays it from _ext_cache, so the write must be mirrored there or the
    poll resurrects the pre-edit window/flags (the _ext_cache rule)."""
    _, device, xtl = _load(monkeypatch)
    payload = xtl.xtl_dnd_merged(DND_LIVE, no_dust=True)

    device.xtl_send_dnd_time([payload])

    assert device._ext_cache[(17, 19)] == payload


def test_xtl_note_dnd_installs_truth_in_both_caches(monkeypatch):
    """The push/write entry point behind the 17.19 stale-read fix: BOTH the
    ext replay cache and the slow-read cache must hold the known-true
    schedule (the cadence guard compares against the slow-read cache)."""
    _, device, xtl = _load(monkeypatch)
    truth = xtl.xtl_dnd_merged(DND_LIVE, start_hour=1, end_hour=2)

    device.xtl_note_dnd(truth)

    assert device._ext_cache[(17, 19)] == truth
    assert device._slow_read_cache["dnd"] == truth


def test_json_push_from_broker_list_is_the_common_case(monkeypatch):
    """The broker decodes properties_changed frames, so the timer push
    arrives as a real list — install it and return it (folded into the
    snapshot for the instant flip)."""
    _, device, _ = _load(monkeypatch)
    timers = [{"id": "1", "hour": 10, "min": 0}]

    assert device.xtl_note_json_push("timers", timers) == timers
    assert device._slow_read_cache["timers"] is timers


def test_json_push_tolerates_string_json(monkeypatch):
    """A JSON-string form is tolerated (same latitude as the record
    cadence read) and normalised to the parsed list before caching."""
    _, device, _ = _load(monkeypatch)

    fresh = device.xtl_note_json_push("records", '[{"room":"salon"}]')

    assert fresh == [{"room": "salon"}]
    assert device._slow_read_cache["records"] == [{"room": "salon"}]


def test_json_push_drops_garbage_without_clobbering(monkeypatch):
    """A truncated/garbled push must NOT blank the cache — the cadence read
    stays the reconciler, so the last good list keeps the sensor alive."""
    _, device, _ = _load(monkeypatch)
    good = [{"id": "1"}]
    device.xtl_note_json_push("timers", good)

    for junk in (None, 3, "not json", '{"not": "a list"}', '{"hour":10}'):
        assert device.xtl_note_json_push("timers", junk) is None

    assert device._slow_read_cache["timers"] == good


def test_json_push_fold_survives_core_only_confirm_poll(monkeypatch):
    """The whole point of the fold: right after the push installs a new
    timer list, the core-only confirm poll must still show it (replayed
    from the slow-read cache), not blank it until the next cadence read."""
    _, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = dict(CORE_VALUES)
    timers = [{"id": "9", "hour": 7, "min": 30}]
    device.xtl_note_json_push("timers", timers)

    status = device.status(full=False)

    assert status.clean_timers_raw == timers


def test_ext_json_push_folds_into_confirm_replay(monkeypatch):
    """clean_values/consumables take the _ext_cache path instead of the
    slow-read cache — same requirement: a core-only confirm poll right
    after the push must replay the pushed list, not the stale value."""
    _, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = dict(CORE_VALUES)

    device.apply_ext_push(17, 34, [3, 5, 6])       # the __init__ json_ext fold
    status = device.status(full=False)

    assert status.clean_values_raw == [3, 5, 6]


def test_stale_local_read_cannot_overwrite_installed_dnd_truth(monkeypatch):
    """The live 2026-09-25 failure: after a local 17.30 write sticks, the
    LOCAL get of 17.19 keeps answering the PRE-write JSON (the robot pushed
    startHour 1 to the cloud while every read answered 23). Once a
    write/push installed the truth, a cadence read may only CONFIRM it."""
    device_mod, device, xtl = _load(monkeypatch)
    FakeMiotDevice.property_values = {**CORE_VALUES, (17, 19): DND_LIVE}
    assert device.status().disturb_time_raw == DND_LIVE   # turn 1 seeds

    truth = xtl.xtl_dnd_merged(DND_LIVE, start_hour=1, start_min=0,
                               end_hour=2, end_min=0)
    device.xtl_send_dnd_time([truth])           # the write installs the truth

    # Turns 2-4 of the cadence: only turn 4 (every 3rd) reads 17.19 — and
    # the firmware still answers the stale window there.
    for _ in range(3):
        status = device.status()
    assert ("get", 17, 19) in FakeMiotDevice.instances[-1].calls
    assert status.disturb_time_raw == truth     # confirmed, never overwritten


def test_dnd_cadence_read_confirms_an_equal_value(monkeypatch):
    """When the cadence read AGREES (xtl_dnd_same on parsed dicts), the
    cache just refreshes — no permanent lock-in of the written spelling."""
    device_mod, device, xtl = _load(monkeypatch)
    FakeMiotDevice.property_values = {**CORE_VALUES, (17, 19): DND_LIVE}
    assert device.status().disturb_time_raw == DND_LIVE   # turn 1 seeds

    spaced = ('{ "startHour": 22, "startMin": 0, "endHour": 8, "endMin": 0, '
              '"notDust": 0, "notDry": 0 }')          # same schedule, wider gaps
    FakeMiotDevice.property_values = {**CORE_VALUES, (17, 19): spaced}
    device.xtl_note_dnd(DND_LIVE)                       # truth, compact spelling

    for _ in range(3):
        status = device.status()                        # turn 4 re-reads 17.19
    assert xtl.xtl_dnd_same(status.disturb_time_raw, spaced)


def test_records_cadence_read_still_refreshes(monkeypatch):
    """The confirm-only rule is scoped to "dnd": the records JSON keeps
    flowing through cadence reads, or a robot-side clean would never show."""
    device_mod, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = {**CORE_VALUES, (17, 46): '[{"room":"salon"}]'}
    assert device.status().clean_records_raw == '[{"room":"salon"}]'  # turn 1

    FakeMiotDevice.property_values = {**CORE_VALUES, (17, 46): '[{"room":"cuisine"}]'}
    for _ in range(6):
        status = device.status()                        # turn 7 re-reads 17.46
    assert status.clean_records_raw == '[{"room":"cuisine"}]'


def test_set_voice_language_frames_switch_voice_lang(monkeypatch):
    """17.21 in piid 61 takes the language code as a PLAIN numeric string
    (same token 17.78 language-ver echoes), and the int code is mirrored
    into _ext_cache for the core-only confirm replay."""
    device_mod, device, _ = _load(monkeypatch)

    device.set_voice_language("french")

    assert _calls() == [("action", 17, 21, [_in(61, "6")])]
    assert device._ext_cache[(17, 25)] == 6
    assert device._ext_cache[(17, 25)] != "6"           # int, like every read


def test_voice_language_rejects_unknown_preset(monkeypatch):
    device_mod, device, _ = _load(monkeypatch)

    with pytest.raises(KeyError):
        device.set_voice_language("klingon")
    assert _calls() == []


def test_install_voice_pack_sends_store_descriptor(monkeypatch):
    """The custom-pack door: 17.21 piid 61 takes the official store's
    descriptor as a JSON string (plugin VoiceList sends exactly
    {md5, url, LanguageType}) and the firmware downloads the pack itself.
    md5 is lower-cased on the way out; no _ext_cache mirror — a custom
    pack reports the code its audio.conf declares, so the select must
    wait for the read-back instead of flipping optimistically."""
    device_mod, device, _ = _load(monkeypatch)

    device.install_voice_pack(
        "http://192.168.1.10:8000/pack.tar",
        "76650EB08655D4019C1E3A37F9D2D723", 6)

    expected = json.dumps({"md5": "76650eb08655d4019c1e3a37f9d2d723",
                           "url": "http://192.168.1.10:8000/pack.tar",
                           "LanguageType": 6})
    assert _calls() == [("action", 17, 21, [_in(61, expected)])]
    assert (17, 25) not in device._ext_cache


@pytest.mark.parametrize("url,md5,lang", [
    ("ftp://host/pack.tar", "0" * 32, 6),      # wrong scheme
    ("pack.tar", "0" * 32, 6),                 # not a url
    ("http://host/pack.tar", "0" * 31, 6),     # md5 too short
    ("http://host/pack.tar", "z" * 32, 6),     # md5 not hex
    ("http://host/pack.tar", "0" * 32, 0),     # code below the table
    ("http://host/pack.tar", "0" * 32, 12),    # code above it
])
def test_install_voice_pack_rejects_bad_descriptor(monkeypatch, url, md5, lang):
    """Nothing reaches the robot unless the descriptor is well-formed."""
    device_mod, device, _ = _load(monkeypatch)

    with pytest.raises(ValueError):
        device.install_voice_pack(url, md5, lang)
    assert _calls() == []


# --- voice-pack install confirmation (the plugin's (17.25, 17.78) pair) -------

def test_read_voice_state_normalises_the_pair(monkeypatch):
    """The install-confirm pair is (17.25 language, 17.78 pack Ver); the
    firmware answers Ver as int or str depending on the path taken — the
    read normalises to (int, str) so the loop's comparisons hold either
    way (plugin VoiceList reacts on exactly this pair, bundle audit)."""
    device_mod, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = {**CORE_VALUES, (17, 25): 6, (17, 78): 2}
    assert device.read_voice_state() == (6, "2")

    FakeMiotDevice.property_values = {**CORE_VALUES, (17, 25): "6", (17, 78): "7"}
    assert device.read_voice_state() == (6, "7")


def test_status_populates_language_ver(monkeypatch):
    """17.78 rides the ext poll like the other toggles/selects — the
    install loop AND the core-only replay both see it."""
    device_mod, device, _ = _load(monkeypatch)
    FakeMiotDevice.property_values = {**CORE_VALUES, (17, 78): 7}
    for _ in range(2):
        status = device.status()                     # full poll seeds cache
    assert status.language_ver_raw == "7"
    assert device._ext_cache[(17, 78)] == 7           # replay keeps the raw value


@pytest.mark.parametrize("pre,cur,code,expected,want", [
    # Ver known -> the exact pair decides (a reaction with a spec).
    ((2, "1"), (6, "7"), 6, "7", True),
    ((6, "7"), (6, "7"), 6, "7", True),
    ((2, "1"), (6, "2"), 6, "7", False),              # still the old pack
    ((2, "1"), (3, "7"), 6, "7", False),              # wrong language
    # Ver unknown -> ANY change onto the requested language counts,
    # exactly like the plugin's reaction (silence stays a failure).
    ((2, "1"), (6, "1"), 6, None, True),
    ((6, "2"), (6, "2"), 6, None, False),
])
def test_voice_pack_confirmed_matrix(monkeypatch, pre, cur, code, expected, want):
    device_mod, _, _ = _load(monkeypatch)

    assert device_mod.voice_pack_confirmed(pre, cur, code, expected) is want


def _voice_pack_bytes(conf):
    """A pack in the vendor's shape: PLAIN tar, media/music/<N>.ogg +
    media/music/audio.conf (conf=None leaves the conf out)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        payload = b"OggS\x00"
        info = tarfile.TarInfo("media/music/1.ogg")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        if conf is not None:
            raw = json.dumps(conf).encode()
            info = tarfile.TarInfo("media/music/audio.conf")
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


def _serve_pack(monkeypatch, device_mod, data=None, error=None):
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def read(self, n=-1): return data[:n] if n and n > 0 else data

    def fake(url, timeout=None):
        if error is not None:
            raise error
        return _Resp()

    monkeypatch.setattr(device_mod.urllib.request, "urlopen", fake)


def test_fetch_voice_pack_proves_bytes_and_returns_ver(monkeypatch):
    """The pre-download authenticates the bytes the robot will fetch and
    reports the Ver the robot must echo into 17.78 once installed."""
    device_mod, _, _ = _load(monkeypatch)
    data = _voice_pack_bytes({"Path": "/data/media/music", "Language": "FR",
                              "Format": "ogg", "Id": 6, "Ver": 20260925})
    _serve_pack(monkeypatch, device_mod, data)

    got = device_mod.fetch_voice_pack(
        "http://h/p.tar", hashlib.md5(data).hexdigest(), 6)
    assert got == "20260925"


def test_fetch_voice_pack_unreachable_is_not_a_defect(monkeypatch):
    """The robot's network is the judge — HA not reaching a LAN share
    must not veto the install (robot-side md5 guard still closes it)."""
    device_mod, _, _ = _load(monkeypatch)
    _serve_pack(monkeypatch, device_mod, error=OSError("no route"))

    assert device_mod.fetch_voice_pack("http://lan-only/p.tar", "0" * 32, 6) is None


@pytest.mark.parametrize("build,want_md5,expect", [
    # (pack builder, md5 handed to the check, substring the error must name)
    (lambda: _voice_pack_bytes({"Id": 6, "Ver": 3}), "0" * 32, "hash"),
    (lambda: gzip.compress(_voice_pack_bytes({"Id": 6, "Ver": 3})), None,
     "gzipped"),
    (lambda: b"x" * 300, None, "tar"),
    (lambda: _voice_pack_bytes(None), None, "audio.conf"),
    (lambda: _voice_pack_bytes({"Id": 5, "Ver": 3}), None, "Id 5"),
])
def test_fetch_voice_pack_refuses_defective_packs(monkeypatch, build, want_md5, expect):
    device_mod, _, _ = _load(monkeypatch)
    data = build()
    _serve_pack(monkeypatch, device_mod, data)

    with pytest.raises(ValueError) as exc:
        device_mod.fetch_voice_pack(
            "http://h/p.tar", want_md5 or hashlib.md5(data).hexdigest(), 6)
    assert expect in str(exc.value)


def test_fetch_voice_pack_missing_ver_is_not_a_defect(monkeypatch):
    """No Ver in the conf: the pack is fine, the change-based rule confirms."""
    device_mod, _, _ = _load(monkeypatch)
    data = _voice_pack_bytes({"Path": "/data/media/music", "Id": 6})
    _serve_pack(monkeypatch, device_mod, data)

    assert device_mod.fetch_voice_pack(
        "http://h/p.tar", hashlib.md5(data).hexdigest(), 6) is None


# --- map list / map switch (get-map-infos 17.13, switch-map 17.9) -------------

MAP_INFOS_JSON = json.dumps([
    {"id": 1, "name": "Salon", "cur": 1},
    {"mapId": 2, "mapName": "Cuisine", "current": 0},
])


def test_map_capability_wires_get_infos_and_switch(monkeypatch):
    _, device, _ = _load(monkeypatch)
    spec_types = importlib.import_module("jonr_vac.spec.types")
    cap = device.profile.map

    assert isinstance(cap, spec_types.XtlMapCapability)
    assert (cap.get_map_list.siid, cap.get_map_list.aiid,
            cap.get_map_list.out_piids) == (17, 13, (37,))
    assert (cap.set_current_map.siid, cap.set_current_map.aiid,
            cap.set_current_map.in_piid) == (17, 9, 58)


def test_map_list_normalises_get_map_infos(monkeypatch):
    _, device, _ = _load(monkeypatch)
    FakeMiotDevice.action_results[(17, 13)] = {
        "out": [{"piid": 37, "value": MAP_INFOS_JSON}]}

    assert device.map_list() == [
        {"name": "Salon", "id": 1, "cur": 1},
        {"name": "Cuisine", "id": 2, "cur": 0},
    ]
    # Declared-input-free action: params pass through empty, xtl wrap inert.
    assert _calls() == [("action", 17, 13, [])]


def test_map_list_tolerates_positional_out_slot(monkeypatch):
    """Same untagged-out firmware quirk as map_obj_name — the payload can
    come back as a bare string slot instead of {"piid","value"}."""
    _, device, _ = _load(monkeypatch)
    FakeMiotDevice.action_results[(17, 13)] = {"out": [MAP_INFOS_JSON]}

    assert [m["id"] for m in device.map_list()] == [1, 2]


def test_map_list_empty_without_payload(monkeypatch):
    _, device, _ = _load(monkeypatch)

    assert device.map_list() == []
    assert _calls() == [("action", 17, 13, [])]


def test_map_list_parses_live_obj_shape(monkeypatch):
    """Probe 2026-09-25: the firmware answers 17.13 with the CURRENT map's
    obj "uid/did/slot" — not a catalogue. One honest entry: uid keys cache
    and the switch-map target, the slot labels it."""
    _, device, _ = _load(monkeypatch)
    FakeMiotDevice.action_results[(17, 13)] = {
        "out": [{"piid": 37, "value": "1628932731/1152219162/1"}]}

    assert device.map_list() == [{"name": "Map 1", "id": 1628932731, "cur": 1}]
    # The raw file name rides out for the map coordinator, which downloads
    # it (the plugin's getMapInfosFileContent) to list every saved map.
    assert device.last_map_infos_file == "1628932731/1152219162/1"


def test_map_list_rejects_garbage_payload(monkeypatch):
    """Neither a JSON catalogue nor uid/did/slot — nothing honest to list."""
    _, device, _ = _load(monkeypatch)
    FakeMiotDevice.action_results[(17, 13)] = {
        "out": [{"piid": 37, "value": "ok"}]}

    assert device.map_list() == []


def test_set_current_map_frames_switch_map(monkeypatch):
    _, device, _ = _load(monkeypatch)

    device.set_current_map(2)

    assert _calls() == [("action", 17, 9, [_in(58, 2)])]


@pytest.mark.parametrize("payload,expected", [
    ('{"maps":[{"map_id":"3","title":"RDC","isCurrent":true}]}',
     [{"name": "RDC", "id": 3, "cur": 1}]),
    ('[{"id": 4}]', [{"name": None, "id": 4, "cur": 0}]),
    ('{"noListHere": 1}', []),
    ('[{"name": "sans id"}]', []),
    ("{broken", []),
    (None, []),
    (42, []),
])
def test_parse_map_infos_tolerates_shapes(monkeypatch, payload, expected):
    _, _, xtl = _load(monkeypatch)

    assert xtl.xtl_parse_map_infos(payload) == expected


@pytest.mark.parametrize("method,args", [
    ("set_child_lock", (True,)),
    ("set_carpet_prefer", ("evade",)),
    ("xtl_wash_mop", ()),
    ("xtl_fast_building", ()),
    ("xtl_manual_control", (1,)),
    ("xtl_send_dnd_time", (["{}"],)),
    ("set_voice_language", ("french",)),
    ("install_voice_pack", ("http://h/p.tar", "0" * 32, 2)),
])
def test_xtl_setting_methods_need_an_xtl_core(monkeypatch, method, args):
    # The retired ijai.vacuum.v17 construction can no longer stand in for a
    # non-xtl core (pruned brands raise at __init__ now), so strip the xtl
    # core off the shipped device: every setter must refuse via _xtl_core().
    _, device, _ = _load(monkeypatch)
    device.core = object()

    with pytest.raises(ValueError):
        getattr(device, method)(*args)


# --- platform-table consistency (AST level, no HA import needed) -----------------

def test_switch_table_matches_core_props_and_device_api(monkeypatch):
    _, device, _ = _load(monkeypatch)
    device_mod = importlib.import_module("jonr_vac.device")

    for key, _icon, status_attr, setter, prop_attr, *needs in _table(
            "switch.py", "XTL_SWITCHES"):
        core = device.core
        assert getattr(core, prop_attr, "missing") != "missing", key
        assert getattr(core, f"{prop_attr}_action", None) is not None, key
        assert callable(getattr(device, setter, None)), key
        assert any(f.name == status_attr for f in
                   dataclasses.fields(device_mod.VacuumStatus)), key
        if needs:  # optional availability gate (attr, raw)
            gate_attr, gate_want = needs[0]
            assert any(f.name == gate_attr for f in
                       dataclasses.fields(device_mod.VacuumStatus)), key
            assert isinstance(gate_want, int), key


def test_switch_table_no_phantom_features_and_boost_gate():
    """The xm2216 app exposes neither auto-detergent nor ERP — those rows
    are phantoms (removed 2026-09-25). Carpet boost is app-gated on the
    adaptive carpet strategy, mirrored as the optional (attr, raw) gate."""
    rows = _table("switch.py", "XTL_SWITCHES")
    keys = [r[0] for r in rows]

    assert "auto_detergent" not in keys and "erp_mode" not in keys
    boost = next(r for r in rows if r[0] == "carpet_boost")
    assert len(boost) == 6 and boost[5] == ("carpet_prefer_raw", 0)
    assert all(len(r) in (5, 6) for r in rows)


def test_select_table_matches_core_tables_and_status(monkeypatch):
    _, device, _ = _load(monkeypatch)
    device_mod = importlib.import_module("jonr_vac.device")

    for key, spec_attr, status_attr, setter in _table("select.py", "XTL_SELECTS"):
        table = getattr(device.core, spec_attr, None)
        assert table, key
        action = getattr(device.core, spec_attr[:-1] + "_action", None)
        assert action is not None and action.in_piid is not None, key
        assert callable(getattr(device, setter, None)), key
        assert any(f.name == status_attr for f in
                   dataclasses.fields(device_mod.VacuumStatus)), key


def test_button_action_table_exists(monkeypatch):
    _, device, _ = _load(monkeypatch)

    rows = _table("button.py", "XTL_ACTION_BUTTONS")
    prop_attrs = _table("button.py", "_ACTION_PROP_ATTR")
    # the wash/dry/collect sextet retired 2026-09-26 (live toggles in
    # switch.py read 17.49 now); the two mapping buttons stay
    assert len(rows) == 2
    for key, _icon, method, _enabled in rows:
        assert getattr(device.core, prop_attrs[method], None) is not None, key
        assert callable(getattr(device, method, None)), key


def test_station_switch_table_matches_device_and_strings(monkeypatch):
    """The live station toggles replaced the six one-shot buttons: each row
    must name real device start/stop methods and gate on a real action prop,
    and the running-token sets must be real 17.49 curRobotStatus spellings."""
    _, device, xtl = _load(monkeypatch)

    rows = _table("switch.py", "XTL_STATION_SWITCHES")
    assert [r[0] for r in rows] == ["wash_mop", "dry_mop", "collect_dust"]
    for key, _icon, prop_attr, setter, stop_setter, running in rows:
        assert getattr(device.core, prop_attr, None) is not None, key
        assert callable(getattr(device, setter, None)), key
        assert callable(getattr(device, stop_setter, None)), key
        for token in running:
            assert token in xtl.XTL_STATION_STATUSES, (key, token)
    # the wash/dry/empty tokens mirror the plugin's stationStatus getter
    # (plain tuples in the table — ast.literal_eval cannot build a frozenset)
    assert set(rows[0][5]) == {"WashMop"}
    assert set(rows[1][5]) == {"HotDry", "WindDry"}
    assert set(rows[2][5]) == {"ClctDust"}


def test_volume_number_max_covers_xtl():
    # xtl-only fork: one volume range table, the xtl.xm2216 one (0-100).
    assert _table("number.py", "_VOLUME_MAX") == 100


def test_no_sensor_ships_the_forbidden_config_category():
    """HA core refuses to ADD any sensor-platform entity whose entity_category
    is CONFIG (SensorEntity.async_internal_added_to_hass raises "entity
    category is set to config"), which silently left dnd_schedule `unavailable`
    forever. Mirror the rule here — the harness builds entities through a bare
    async_add_entities callback and never exercises that guard, so CI was green.
    """
    tree = ast.parse((PKG / "sensor.py").read_text(encoding="utf-8"))
    offenders = [
        call.lineno
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and getattr(call.func, "id", "") == "JonrSensorDescription"
        and any(
            kw.arg == "entity_category"
            and isinstance(kw.value, ast.Attribute)
            and kw.value.attr == "CONFIG"
            for kw in call.keywords
        )
    ]
    assert not offenders, f"sensor.py lines {offenders}: CONFIG is forbidden on sensor platform"


def test_translation_files_cover_every_new_key():
    """strings.json (runtime mirror en.json) must name every new entity,
    and enum-ish selects/sensors carry state labels."""
    strings = json.loads((PKG / "strings.json").read_text(encoding="utf-8"))
    en = json.loads((PKG / "translations" / "en.json").read_text(encoding="utf-8"))
    ent, ent_en = strings["entity"], en["entity"]
    assert ent == ent_en  # the byte-mirror law

    switches = [k for k, *_rest in _table("switch.py", "XTL_SWITCHES")]
    selects = [k for k, *_rest in _table("select.py", "XTL_SELECTS")]
    buttons = [k for k, *_rest in _table("button.py", "XTL_ACTION_BUTTONS")]
    for key in switches:
        assert "name" in ent["switch"][key], key
    for key in selects:
        assert "state" in ent["select"][key], key
    for key in buttons:
        assert "name" in ent["button"][key], key
    # NPD check-boxes live outside XTL_SWITCHES (XtlDndFlagSwitch instances)
    # and the window is the two time pickers of time.py's _SIDES — name them
    # explicitly here, the AST table walk cannot see their translation keys.
    for key in ("dnd_no_dust", "dnd_no_dry"):
        assert "name" in ent["switch"][key], key
    for key in _table("time.py", "_SIDES"):
        assert "name" in ent["time"][key], key
    # The old free-text single window is gone for good — a lingering name is
    # how a re-added platform slips back in unnoticed.
    assert "text" not in ent
    for key in ("session_clean_time", "session_clean_area", "total_clean_time",
                "total_clean_area", "total_clean_count",
                "clean_water_tank", "drain_water_tank",
                "dust_bag_state", "mop_tank_state"):
        assert "name" in ent["sensor"][key], key
    # Removed entities must leave their names behind too — a lingering
    # translation is how a re-added phantom slips through unnoticed.
    assert "auto_detergent" not in ent["switch"]
    assert "erp_mode" not in ent["switch"]
    assert "dnd_schedule" not in ent["sensor"]
    # Retired 2026-09-25 (user call): the return-status sensor duplicates the
    # vacuum status text (17.48 stays a polled raw prop), and the waste-water
    # filter screen / its reset button are phantoms on the P20 Pro (17.32
    # never reports a filterScreen).
    assert "return_status" not in ent["sensor"]
    assert "waste_filter_life" not in ent["sensor"]
    assert "reset_waste_filter" not in ent["button"]
    # Retired 2026-09-25 (user call): the plugin renders no togglable switch
    # for either carpet toggle — "2 fois" only as a read-only row in the
    # per-carpet editor, "prioriser" never rendered outside the i18n tables
    # (bundle audit). Their 17.74/17.75 props stay polled (erp precedent:
    # phantom entities retire, spec plumbing lives on).
    assert "carpet_twice" not in ent["switch"]
    assert "carpet_first" not in ent["switch"]
    # Retired 2026-09-25 (user call, "pas utile"): the P20 Pro never answers
    # 17.46 (null on every local get, no push), so the records counter sat
    # unknown permanently; Mi Home serves its history from the Xiaomi cloud
    # instead. The 17.46 spec plumbing stays polled (erp precedent).
    assert "cleaning_records" not in ent["sensor"]
    # 2026-09-26: the six station buttons retired in favour of the live
    # toggles — their keys must leave button.* and arrive on switch.*, and
    # the new ENUM sensors carry a state label per option.
    for key in ("wash_mop", "dry_mop", "collect_dust"):
        assert key not in ent["button"], key
        assert "name" in ent["switch"][key], key
    for key in ("stop_wash_mop", "stop_dry_mop", "stop_collect_dust"):
        assert key not in ent["button"], key
    for key in ("robot_error", "station_error", "robot_message",
                "station_status"):
        assert "name" in ent["sensor"][key] and ent["sensor"][key]["state"], key


def test_error_sensor_state_maps_cover_every_option(monkeypatch):
    """HA renders an ENUM sensor's option id raw when the state map misses
    it: every option id of the four new sensors must have a state label in
    strings.json AND in fr.json (the language the user runs), and the
    descriptions must advertise exactly those option lists."""
    from .helpers import load_sensor_module

    strings = json.loads((PKG / "strings.json").read_text(encoding="utf-8"))
    fr = json.loads((PKG / "translations" / "fr.json").read_text(encoding="utf-8"))
    _, _, xtl = _load(monkeypatch)

    expected = {
        "robot_error": ["none"] + list(xtl.XTL_ERROR_CODE_KEYS.values()),
        "station_error": ["none"] + list(xtl.XTL_ERROR_CODE_KEYS.values()),
        "robot_message": ["none"] + list(xtl.XTL_MESSAGE_CODE_KEYS.values()),
        "station_status": list(xtl.XTL_STATION_STATUSES.values()),
    }
    for key, options in expected.items():
        for book, tag in ((strings, "strings"), (fr, "fr")):
            state = book["entity"]["sensor"][key]["state"]
            assert set(state) == set(options), f"{tag}:{key} state map mismatch"
    # the option ids are the value lists the sensor descriptions advertise
    sensor_mod = load_sensor_module(monkeypatch)
    by_key = {d.key: d for d in sensor_mod._ALL_SENSORS}
    for key, options in expected.items():
        assert by_key[key].options == options, key
    # the fix-it attribute tables key exactly the error codes the options
    # render, in both languages
    assert (set(xtl.XTL_ERROR_SOLUTIONS_EN)
            == set(xtl.XTL_ERROR_SOLUTIONS_FR)
            == set(xtl.XTL_ERROR_CODE_KEYS))


def test_station_status_option_normalizes_case(monkeypatch):
    _, _, xtl = _load(monkeypatch)

    assert xtl.xtl_station_status_option("WashMop") == "washing_mop"
    # firmware may answer any casing — the map is case-insensitive
    assert xtl.xtl_station_status_option("HOTDRY") == "hot_drying"
    assert xtl.xtl_station_status_option("  clctdust ") == "emptying"
    assert xtl.xtl_station_status_option("Charging") == "charging"
    assert xtl.xtl_station_status_option("NoSuchToken") is None
    assert xtl.xtl_station_status_option(None) is None
    assert xtl.xtl_station_status_option("") is None
    assert xtl.xtl_station_status_option(42) is None
