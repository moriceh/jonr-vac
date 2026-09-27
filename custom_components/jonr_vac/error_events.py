"""Robot/station error events + persistent notifications (dreame-vacuum style).

Two mechanisms, on purpose (docs/notifications.md + docs/events.md):

* bus events ``jonr_vac_error`` / ``jonr_vac_station_error`` with payload
  ``{entity_id, error: <slug>, code: <int>}`` — fired ALWAYS, automations
  must keep working when the user turns notification bubbles off;
* persistent_notification bubbles — gated by the integration option
  ``error_notifications`` (default on), one bubble per code, auto-dismissed
  when the error clears.

Dedup is per source (robot fault = core 1.3, station = 17.79): an action
fires only when the code changes to a new non-zero value, so a flapping
firmware echo of the same code can't spam. A snapshot taken while an error
is already active (integration startup) does notify — the error is real.

No homeassistant import at module load (``hass`` arrives as ``Any``, the
few HA helpers used are imported lazily inside methods): the pure test tier
drives ``async_process`` with a fake hass and never needs the package.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

from .const import (
    DOMAIN,
    EVENT_ERROR,
    EVENT_STATION_ERROR,
    FAULT_IMAGE_URL,
    OPT_ERROR_NOTIFICATIONS,
)
from .spec.profiles.xtl import (
    XTL_ERROR_CODE_KEYS,
    XTL_ERROR_IMAGES,
    xtl_error_solutions,
)

_LOGGER = logging.getLogger(__name__)

# source key -> bus event + bubble titles per UI language. The plugin ships
# no chrome strings for our own bubble (only the error labels/solutions),
# so FR keeps the hand-written title and every other HA language rides the
# EN one — the bubble CONTENT (label, solution, image) is still localized.
_KINDS = {
    "robot": {
        "event": EVENT_ERROR,
        "titles": {
            "fr": "Aspirateur JONR : erreur robot",
            "en": "JONR Vacuum: robot error",
        },
    },
    "station": {
        "event": EVENT_STATION_ERROR,
        "titles": {
            "fr": "Aspirateur JONR : erreur station",
            "en": "JONR Vacuum: station error",
        },
    },
}

# Fallback label tables, same content as translations/<bucket>.json
# entity.sensor.robot_error.state (that file is tried first — see
# _labels(); these only cover a missing/unreadable translations dir).
_FALLBACK_LABELS: dict[str, dict[int, str]] = {}


def _translations_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "translations")


def _available_buckets() -> dict[str, str]:
    """lowercase stem -> real stem of every translations/<stem>.json,
    discovered once (ko/vi/zh-Hant joined the FR/EN pair with the plugin's
    keyword tables; ja/nl/pt/pt-BR ship labels too, EN-shaped chrome)."""
    cached = _AVAILABLE_BUCKETS.get("m")
    if cached is None:
        try:
            names = os.listdir(_translations_dir())
        except OSError:
            names = []
        cached = {n[:-5].lower(): n[:-5] for n in names if n.endswith(".json")}
        _AVAILABLE_BUCKETS["m"] = cached
    return cached


_AVAILABLE_BUCKETS: dict[str, dict[str, str]] = {}


def _label_bucket(language) -> str:
    """HA language tag -> translations/*.json stem. Exact tag first (pt-BR
    ships as its own file), then the base tag (de-DE -> de, zh-Hant -> the
    zh-Hant file), finally EN like every HA translation fallback."""
    tag = str(language or "").strip().lower().replace("_", "-")
    avail = _available_buckets()
    return avail.get(tag) or avail.get(tag.split("-")[0]) or "en"


def _load_labels(bucket: str) -> dict[int, str]:
    """code -> human label for one translations bucket, from
    translations/<bucket>.json (robot_error states, keyed by the
    XTL_ERROR_CODE_KEYS slugs). Cached per bucket. Reads from disk on the
    first call per bucket — call it via preload() during setup (executor),
    not from the event loop (HA flags blocking open() there)."""
    cached = _FALLBACK_LABELS.get(bucket)
    if cached is not None:
        return cached
    out: dict[int, str] = {}
    path = os.path.join(_translations_dir(), f"{bucket}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            states = (
                json.load(fh).get("entity", {}).get("sensor", {})
                .get("robot_error", {}).get("state", {})
            )
    except (OSError, ValueError):
        states = {}
    for code, slug in XTL_ERROR_CODE_KEYS.items():
        out[code] = states.get(slug) or slug.replace("_", " ").capitalize()
    _FALLBACK_LABELS[bucket] = out
    return out


def _labels(language) -> dict[int, str]:
    return _load_labels(_label_bucket(language))


class JonrErrorNotifier:
    """Watches the control coordinator's fault/station_error_raw values."""

    def __init__(self, hass: Any, entry: Any, control: Any) -> None:
        self._hass = hass
        self._entry = entry
        self._control = control
        self._last: dict[str, int | None] = {"robot": None, "station": None}
        self._notified: set[tuple[str, int]] = set()
        self._unsub: Callable[[], None] | None = None

    # -- wiring -------------------------------------------------------------

    def preload(self) -> None:
        """Warm every shipped label table off the event loop (HA flags
        open() inside it; async_setup_entry runs in executor context, see
        _load_labels). Driven by the files that actually exist, not a
        hard-coded list, so a new locale file needs no change here."""
        for stem in sorted(_available_buckets().values()):
            _load_labels(stem)

    def listen(self) -> None:
        """Start watching coordinator snapshots (idempotent)."""
        if self._unsub is not None:
            return
        self._unsub = self._control.async_add_listener(self._on_coordinator_update)
        # Take stock now: an error already present at startup is real.
        self._on_coordinator_update()

    def unlisten(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    # -- pure decision --------------------------------------------------------

    @staticmethod
    def decide(
        previous: dict[str, int | None], codes: dict[str, int | None]
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Compare two snapshots; (fires, clears) — pure, unit-testable.

        fires: source -> new non-zero code (different from last seen);
        clears: source -> the code it just went back from (to 0/None), once.
        """
        fires: dict[str, int] = {}
        clears: dict[str, int] = {}
        for source, code in codes.items():
            last = previous.get(source)
            normalised = code if code else None
            if normalised:
                if normalised != last:
                    fires[source] = code
            elif last:
                clears[source] = last
        return fires, clears

    # -- reacting -------------------------------------------------------------

    def _on_coordinator_update(self) -> None:
        status = getattr(self._control, "data", None)
        if status is None:
            return
        self.async_process(
            {
                "robot": getattr(status, "fault", None),
                "station": getattr(status, "station_error_raw", None),
            }
        )

    def async_process(self, codes: dict[str, int | None]) -> None:
        """Apply one snapshot (public so tests can drive it directly)."""
        fires, clears = self.decide(self._last, codes)
        for source, code in fires.items():
            self._fire(source, code)
        for source, code in clears.items():
            self._dismiss(source, code)
        self._last = {s: (c if c else None) for s, c in codes.items()}

    def _language(self) -> str:
        """The user's HA UI language as a lowercase tag (the bubble title
        keeps the FR chrome only for fr; everything else rides EN, see
        _KINDS)."""
        return str(getattr(self._hass.config, "language", "en") or "en").lower()

    def _fire(self, source: str, code: int) -> None:
        kind = _KINDS[source]
        slug = XTL_ERROR_CODE_KEYS.get(code) or f"error_{code}"
        # Events always fire, toggle-proof (automation contract).
        _LOGGER.debug("jonr_vac: %s code %s (%s)", kind["event"], code, slug)
        self._hass.bus.async_fire(
            kind["event"],
            {"entity_id": self._vacuum_entity_id(), "error": slug, "code": code},
        )
        if not self._entry.options.get(OPT_ERROR_NOTIFICATIONS, True):
            return
        if (source, code) in self._notified:
            return
        lang = self._language()
        label = _labels(lang).get(code, slug.replace("_", " ").capitalize())
        solution = xtl_error_solutions(lang).get(code, "")
        message = f"**{label}** ({code})"
        if solution:
            message += f"\n\n{solution}"
        # Official fault picture (extracted from the Mi Home plugin — see
        # XTL_ERROR_IMAGES), served same-origin from the integration's own
        # static path, so the bubble carries a URL, not base64 (the way
        # dreame-vacuum inlines it).
        if img := XTL_ERROR_IMAGES.get(code):
            message += f"\n\n![{label}]({FAULT_IMAGE_URL}/{img})"
        # chrome title: the FR wording for fr, EN for every other language
        # (the plugin ships no bubble chrome to translate, see _KINDS).
        chrome = "fr" if lang.split("-")[0] == "fr" else "en"
        self._pn().async_create(
            self._hass,
            message,
            title=kind["titles"][chrome],
            notification_id=self._nid(source, code),
        )
        self._notified.add((source, code))

    def _dismiss(self, source: str, code: int) -> None:
        if (source, code) in self._notified:
            self._notified.discard((source, code))
            self._pn().async_dismiss(self._hass, self._nid(source, code))

    def _nid(self, source: str, code: int) -> str:
        return f"{DOMAIN}_{self._entry.entry_id}_{source}_{code}"

    def _pn(self) -> Any:
        from homeassistant.components import persistent_notification

        return persistent_notification

    def _vacuum_entity_id(self) -> str | None:
        base = getattr(self._entry, "unique_id", None) or self._entry.entry_id
        try:
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(self._hass)
        except Exception:  # noqa: BLE001 - registry unavailable (pure tests)
            return None
        if registry is None:
            return None
        return registry.async_get_entity_id("vacuum", DOMAIN, f"{base}_vacuum")
