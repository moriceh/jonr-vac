"""Pure tests for the robot/station error notifier (error_events.py) and the
Active Map select's saved-only switch rule (xtl_selectable_maps).

No homeassistant: the helpers stub the notifier's two lazy HA touchpoints
(persistent_notification, entity_registry), so the event / bubble / dedup
contract runs on native Windows.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from .helpers import (
    FakeCoordinator,
    FakePersistentNotification,
    load_error_events_module,
    load_xtl_module,
)

PKG = Path(__file__).resolve().parents[2] / "custom_components" / "jonr_vac"
ENTRY_ID = "entry-1"


class FakeBus:
    def __init__(self) -> None:
        self.fired: list[tuple[str, dict]] = []

    def async_fire(self, event_type: str, payload: dict) -> None:
        self.fired.append((event_type, payload))


def _notifier(monkeypatch, options=None, language="fr"):
    mod = load_error_events_module(monkeypatch)
    hass = SimpleNamespace(bus=FakeBus(), config=SimpleNamespace(language=language))
    entry = SimpleNamespace(
        entry_id=ENTRY_ID, unique_id="44:87:63:ce:c2:07", options=options or {}
    )
    control = FakeCoordinator()
    return mod, mod.JonrErrorNotifier(hass, entry, control), control


# --- decision table ----------------------------------------------------------

def test_decide_fires_once_per_new_code_and_clears_on_zero(monkeypatch):
    decide = load_error_events_module(monkeypatch).JonrErrorNotifier.decide

    prev: dict[str, int | None] = {"robot": None, "station": None}
    assert decide(prev, {"robot": 4012, "station": None}) == (
        {"robot": 4012}, {})
    prev = {"robot": 4012, "station": None}
    # firmware echo / repeated poll of the same code -> nothing
    assert decide(prev, {"robot": 4012, "station": None}) == ({}, {})
    # back to no-error -> exactly one clear naming the code it came from
    assert decide(prev, {"robot": 0, "station": None}) == ({}, {"robot": 4012})
    assert decide({"robot": None, "station": None},
                  {"robot": 0, "station": None}) == ({}, {})
    # a different code while one is active re-fires (new error)
    assert decide(prev, {"robot": 4001, "station": 4503}) == (
        {"robot": 4001, "station": 4503}, {})


# --- events always fire; bubbles are the toggle-gated part -------------------

def test_error_transition_fires_event_and_notification_once(monkeypatch):
    _, notifier, _ = _notifier(monkeypatch)

    notifier.async_process({"robot": 4012, "station": None})
    notifier.async_process({"robot": 4012, "station": None})  # echo, no spam

    assert notifier._hass.bus.fired == [
        ("jonr_vac_error",
         {"entity_id": None, "error": "robot_stuck", "code": 4012}),
    ]
    assert len(FakePersistentNotification.created) == 1
    created = FakePersistentNotification.created[0]
    assert created["title"] == "Aspirateur JONR : erreur robot"
    # FR label (HA language) + the plugin's fix-it text for that code
    assert "Robot bloqué" in created["message"]
    assert "4012" in created["message"]
    assert "Retirez les obstacles autour du robot" in created["message"]
    # The official fault picture (extracted from the Mi Home plugin), as a
    # same-origin markdown URL on the integration's own static path.
    assert ("![Robot bloqué](/jonr-vac-card/images/faults/RobotTrapped.png)"
            in created["message"])
    assert created["notification_id"] == f"jonr_vac_{ENTRY_ID}_robot_4012"

    # clearing the error dismisses the bubble once
    notifier.async_process({"robot": 0, "station": None})
    assert FakePersistentNotification.dismissed == [
        f"jonr_vac_{ENTRY_ID}_robot_4012"
    ]
    notifier.async_process({"robot": 0, "station": None})
    assert len(FakePersistentNotification.dismissed) == 1


def test_station_error_uses_its_own_event_and_title(monkeypatch):
    _, notifier, _ = _notifier(monkeypatch, language="en")

    notifier.async_process({"robot": None, "station": 4503})

    event_type, payload = notifier._hass.bus.fired[0]
    assert event_type == "jonr_vac_station_error"
    assert payload == {"entity_id": None, "error": "dirty_water_full",
                       "code": 4503}
    created = FakePersistentNotification.created[0]
    assert created["title"] == "JONR Vacuum: station error"
    assert "Dirty water full" in created["message"]
    assert "dirty water tank" in created["message"]  # EN fix-it text


def test_notifications_off_still_fires_events(monkeypatch):
    _, notifier, _ = _notifier(
        monkeypatch, options={"error_notifications": False}
    )

    notifier.async_process({"robot": 4012, "station": 4501})

    assert [t for t, _ in notifier._hass.bus.fired] == [
        "jonr_vac_error", "jonr_vac_station_error",
    ]
    assert FakePersistentNotification.created == []

    # and a later clear has nothing to dismiss
    notifier.async_process({"robot": 0, "station": 0})
    assert FakePersistentNotification.dismissed == []


def test_startup_with_a_live_error_notifies(monkeypatch):
    """listen() takes stock: an error already present at setup is real."""
    _, notifier, control = _notifier(monkeypatch)
    control.data = SimpleNamespace(fault=4001, station_error_raw=None)

    notifier.listen()

    assert notifier._hass.bus.fired[0][0] == "jonr_vac_error"
    assert FakePersistentNotification.created[0]["notification_id"].endswith(
        "_robot_4001"
    )
    # re-listening (a reload of the same object) must not re-notify
    notifier.listen()
    assert len(notifier._hass.bus.fired) == 1


def test_listen_unlisten_wires_the_coordinator(monkeypatch):
    _, notifier, control = _notifier(monkeypatch)

    notifier.listen()
    notifier.listen()  # idempotent
    assert len(control.subs) == 1

    control.data = SimpleNamespace(fault=None, station_error_raw=None)
    control.push()  # clean snapshot -> nothing fires
    assert notifier._hass.bus.fired == []

    control.data = SimpleNamespace(fault=4014, station_error_raw=None)
    control.push()
    assert notifier._hass.bus.fired[0][1]["error"] == "robot_suspended"

    notifier.unlisten()
    assert control.subs == []
    control.data = SimpleNamespace(fault=4016, station_error_raw=None)
    control.push()
    assert len(notifier._hass.bus.fired) == 1  # detached


def test_unknown_code_gets_a_fallback_slug(monkeypatch):
    _, notifier, _ = _notifier(monkeypatch)
    notifier.async_process({"robot": 9999, "station": None})
    assert notifier._hass.bus.fired[0][1]["error"] == "error_9999"
    # no picture for a code the plugin table does not know
    assert "images/faults" not in FakePersistentNotification.created[-1]["message"]


# --- fault pictures come from the official Mi Home plugin's table ------------

def test_error_images_mirror_the_plugin_table(monkeypatch):
    """XTL_ERROR_IMAGES is the official Mi Home plugin's faultImages table
    (data/1152219162/files/config.json) transcribed verbatim: same codes,
    same filenames the CDN URLs end with."""
    xtl = load_xtl_module(monkeypatch)
    plugin_config = (Path(__file__).resolve().parents[3]
                     / "jonr_xiaomihome_plugin" / "data" / "1152219162"
                     / "files" / "config.json")
    if not plugin_config.is_file():
        pytest.skip("vendor plugin data not on this machine")
    rows = json.loads(
        plugin_config.read_text(encoding="utf-8"))["faultImages"]
    assert xtl.XTL_ERROR_IMAGES == {
        int(row["code"]): row["imgUrl"][0].rsplit("/", 1)[-1] for row in rows
    }


def test_every_referenced_picture_ships(monkeypatch):
    xtl = load_xtl_module(monkeypatch)
    folder = PKG / "www" / "images" / "faults"
    assert len(xtl.XTL_ERROR_IMAGES) == 26  # config.json faultImages size
    for code, png in xtl.XTL_ERROR_IMAGES.items():
        path = folder / png
        assert path.is_file(), f"{code} -> {png} missing from www/"
        # real PNGs, not HTML error pages off the CDN
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", png


# --- bubble labels are the strings the error sensors show --------------------

def test_bubble_labels_match_the_shipped_translations(monkeypatch):
    mod = load_error_events_module(monkeypatch)
    xtl = load_xtl_module(monkeypatch)

    for lang in ("fr", "en"):
        states = json.loads(
            (PKG / "translations" / f"{lang}.json")
            .read_text(encoding="utf-8")
        )["entity"]["sensor"]["robot_error"]["state"]
        labels = mod._labels(lang)
        assert set(labels) == set(xtl.XTL_ERROR_CODE_KEYS)
        for code, slug in xtl.XTL_ERROR_CODE_KEYS.items():
            assert labels[code] == states[slug], slug


# --- Active Map select: only saved floors are switch targets -----------------

def test_selectable_maps_drops_the_live_draft(monkeypatch):
    xtl = load_xtl_module(monkeypatch)
    rows = [
        {"name": None, "id": 1, "cur": 0, "saved": 0},  # robot working copy
        {"name": "Appart Brest", "id": 3, "cur": 1, "saved": 1},
        {"name": "Étage", "id": 9, "cur": 0, "saved": 1},
    ]
    assert xtl.xtl_selectable_maps(rows) == rows[1:]


def test_untitled_saved_map_is_switchable(monkeypatch):
    # Live 2026-09-26 15:41 + user correction the same evening: mapId 1
    # (saved:1, blank name, status:0) is the user's REAL second card — Mi
    # Home titles it "Map 1" by default and switchMap accepts it (plugin
    # filter is saved===1 alone). It must be switchable; the blank name is
    # not disqualifying. The merge still lets the status:1 row own cur even
    # over the claimed blob row.
    xtl = load_xtl_module(monkeypatch)
    blob = [{"name": None, "id": 1, "cur": 1, "saved": 0}]
    catalog = [{"name": None, "id": 1, "cur": 0, "saved": 1},
               {"name": "Appart Brest", "id": 3, "cur": 1, "saved": 1}]
    merged = xtl.xtl_add_known_maps(blob, catalog)
    by_id = {m["id"]: m for m in merged}
    assert by_id[3]["cur"] == 1          # status:1 floor owns "in use"
    assert by_id[1]["cur"] == 0
    # select view: both saved floors (the draft's saved=0 row was promoted
    # to saved:1 by the catalogue claim — that IS the user's Map 1)
    assert xtl.xtl_selectable_maps(merged) == [by_id[1], by_id[3]]


def test_selectable_maps_keeps_the_draft_only_on_cold_start(monkeypatch):
    """No catalogue read yet (no saved row at all) -> the draft is the honest
    single-map truth; once the file has spoken it never re-enters the list."""
    xtl = load_xtl_module(monkeypatch)
    draft = [{"name": None, "id": 1, "cur": 1, "saved": 0}]
    assert xtl.xtl_selectable_maps(draft) == draft
    assert xtl.xtl_selectable_maps([]) == []

    # a lone draft beside any saved floor loses (live 2026-09-26: tapping it
    # left the robot with no map until Mi Home's restore)
    assert xtl.xtl_selectable_maps(
        draft + [{"name": "Bas", "id": 3, "cur": 0, "saved": 1}]
    ) == [{"name": "Bas", "id": 3, "cur": 0, "saved": 1}]
