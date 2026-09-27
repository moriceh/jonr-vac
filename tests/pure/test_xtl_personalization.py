"""Pure tests for the xtl personalisation writes (17.53 / 17.15 / zones).

Payload shapes are the plugin's verbatim envelopes (CustomizedParameters
type 1, CustomizedOrder type 2, roomInfoChange, zone start-clean values);
the send layer is checked against the MIoT strict in-param frame."""
from __future__ import annotations

import dataclasses
import importlib
import json

import pytest

from .helpers import FakeMiotDevice, load_device_module


def _load(monkeypatch):
    """device module, an xtl device, the profile builders, and the list of
    sleeps the send layer asked for (patched out, never actually slept)."""
    device_mod = load_device_module(monkeypatch)
    slept: list = []
    monkeypatch.setattr(device_mod.time, "sleep", slept.append)
    device = device_mod.XtlVacuumDevice("host", "token", "xtl.vacuum.xm2216")
    xtl = importlib.import_module("jonr_vac.spec.profiles.xtl")
    return device, xtl, slept


def _last_calls():
    return FakeMiotDevice.instances[-1].calls


def _in(piid: int, value):
    return {"did": "-", "piid": piid, "value": value}


# --- payload builders --------------------------------------------------------

def _prefs(n):
    return [{"R": i, "K": 0, "F": 1, "W": 1, "C": 1, "D": 1} for i in range(1, n + 1)]


def test_preference_payloads_chunk_five_rooms(monkeypatch):
    _, xtl, _ = _load(monkeypatch)

    payloads = xtl.xtl_preference_payloads(123, _prefs(7))

    parsed = [json.loads(p) for p in payloads]
    assert [p["total"] for p in parsed] == [2, 2]
    assert [p["index"] for p in parsed] == [0, 1]
    assert [len(p["customdata"]["data"]) for p in parsed] == [5, 2]
    assert all(p["customdata"]["type"] == 1 for p in parsed)
    assert parsed[1]["customdata"]["data"][0] == {
        "I": 123, "R": 6, "K": 0, "F": 1, "W": 1, "C": 1, "D": 1}
    # compact form — the plugin JSON.stringifies without spaces
    assert ", " not in payloads[0]


def test_preference_payloads_empty_room_list_sends_nothing(monkeypatch):
    _, xtl, _ = _load(monkeypatch)

    assert xtl.xtl_preference_payloads(1, []) == []


def test_sequence_payload_matches_customized_order(monkeypatch):
    _, xtl, _ = _load(monkeypatch)

    assert json.loads(xtl.xtl_sequence_payload(123, [16, 11])) == {
        "total": 1, "index": 0,
        "customdata": {"type": 2, "mapid": 123, "data": [16, 11]}}
    # clear = empty data, ids coerced like the plugin's parseInt map
    assert json.loads(xtl.xtl_sequence_payload(7, ["16"]))[
        "customdata"]["data"] == [16]
    assert json.loads(xtl.xtl_sequence_payload(7, []))["customdata"]["data"] == []


def test_room_info_payload_carries_mapid_and_rooms(monkeypatch):
    _, xtl, _ = _load(monkeypatch)

    payload = xtl.xtl_room_info_payload(
        9, [{"roomId": 11, "name": "Salon", "category": 1}])

    assert json.loads(payload) == {
        "mapId": 9, "rooms": [{"roomId": 11, "name": "Salon", "category": 1}]}


def test_zone_flat_orders_corners_tl_tr_br_bl(monkeypatch):
    _, xtl, _ = _load(monkeypatch)

    flat = xtl.xtl_zone_flat([[320, 410, 420, 510], [0, 0, 10, 10]])

    assert flat == [320, 410, 420, 410, 420, 510, 320, 510,
                    0, 0, 10, 0, 10, 10, 0, 10]
    # schema floats truncate to grid ints
    assert xtl.xtl_zone_flat([[0.9, 0.1, 2.9, 2.1]]) == [0, 0, 2, 0, 2, 2, 0, 2]


@pytest.mark.parametrize("rect", [[420, 410, 320, 510], [320, 510, 420, 410]])
def test_zone_flat_rejects_inverted_rects(monkeypatch, rect):
    _, xtl, _ = _load(monkeypatch)

    with pytest.raises(ValueError):
        xtl.xtl_zone_flat([rect])


def test_zone_flat_requires_at_least_one_rect(monkeypatch):
    _, xtl, _ = _load(monkeypatch)

    with pytest.raises(ValueError):
        xtl.xtl_zone_flat([])


# --- device send layer -------------------------------------------------------

def test_send_customization_wraps_piid70_and_staggers_chunks(monkeypatch):
    device, _, slept = _load(monkeypatch)

    device.xtl_send_customization(['{"a":1}', '{"b":2}'])

    assert _last_calls() == [
        ("action", 17, 53, [_in(70, '{"a":1}')]),
        ("action", 17, 53, [_in(70, '{"b":2}')]),
    ]
    assert slept == [0.5]  # 500 ms between chunks, none before the first


def test_send_room_info_wraps_piid60_without_stagger(monkeypatch):
    device, _, slept = _load(monkeypatch)

    device.xtl_send_room_info(['{"mapId":9,"rooms":[]}'])

    assert _last_calls() == [
        ("action", 17, 15, [_in(60, '{"mapId":9,"rooms":[]}')])
    ]
    assert slept == []


def test_zone_clean_stops_then_sends_zone_type(monkeypatch):
    device, _, _ = _load(monkeypatch)

    device.start_zone_clean([[320, 410, 420, 510]])

    assert _last_calls() == [
        ("action", 17, 3, []),
        ("action", 17, 1, [_in(8, 4),
                           _in(34, "[320, 410, 420, 410, 420, 510, 320, 510]")]),
    ]


def test_personalisation_writes_need_the_matching_capabilities(monkeypatch):
    device, _, _ = _load(monkeypatch)
    # Strip the xtl capabilities these three writes dispatch on: the guards
    # must refuse (ValueError) instead of firing an action, which is what the
    # retired brand-profile tests used to check before the brand profiles
    # (and with them ijai.vacuum.v17) were pruned from the fork.
    device.core = object()
    device.profile = dataclasses.replace(device.profile, room_clean=None)

    with pytest.raises(ValueError):
        device.xtl_send_customization(["{}"])
    with pytest.raises(ValueError):
        device.xtl_send_room_info(["{}"])
    with pytest.raises(ValueError):
        device.start_zone_clean([[0, 0, 10, 10]])
