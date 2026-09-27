"""Fetch one xtl (JONR) cloud map and build the card contract.

xtl maps are plain-JSON KS3 objects named by the device itself — no third-
party parser, no key material, no slot fallback chain. This module is the
thin cloud side of that pipeline: mint a URL, download, hand the blob to
xtl_map.render_xtl_map. Synchronous; run in an executor.

Based on xiaomi-vac by letitbe-dull (MIT) — see CREDITS.md. The upstream
brand-parser dispatch (ijai/xiaomi/dreame/viomi/roidmi slots) was pruned
with this fork; what remains is the xtl path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .cloud.connector import XiaomiCloud
from .map_parsers import map_url_endpoint
from .xtl_map import render_xtl_map

_LOGGER = logging.getLogger(__name__)


class SessionExpired(Exception):
    """The cloud session no longer returns a map URL (token likely expired)."""


@dataclass
class MapResult:
    image_png: bytes
    attributes: dict
    vector: dict  # ACTIVE map's grid + vector overlays
    # Physical map id (mapHeadId) this render belongs to; None when the blob
    # carries no id of its own.
    map_id: int | None = None
    # sha256 of the raw blob (plus the relocating flag — see xtl_map); lets
    # the coordinator's cache skip rewriting storage when a poll yields
    # byte-identical content.
    content_hash: str | None = None
    # All maps the device lists, each a vector dict tagged with map_id/map_name/
    # active. Always contains at least the active map; extra entries appear only
    # when the device actually has more than one map.
    maps: list = field(default_factory=list)


class MapFetcher:
    """Fetches device-named KS3 map objects for one xtl vacuum."""

    def __init__(self, cloud: XiaomiCloud, *, server: str, user_id: str,
                 device_id: str, model: str):
        self._cloud = cloud
        self._server = server
        self._user_id = str(user_id)
        self._device_id = str(device_id)
        self._model = model
        self._endpoint = map_url_endpoint("xtl")

    def fetch_obj(self, obj_name: str,
                  live: dict | None = None) -> MapResult | None:
        """Fetch one device-named KS3 object and render it (xtl_map).

        `live` is the coordinator's raw property-read snapshot (see
        xtl_map._clean_view); it makes the room tips follow the robot's
        current whole/room/zone task. None renders without live state.

        get-map-data (17.12) answers with a fully-qualified obj
        ("uid/did/slot", live-verified) which the cloud call must receive
        verbatim — re-prefixing it (the slot-brand behaviour) mints a dead
        path and every cycle dead-ends in "no map available". A bare slot
        name still gets the standard prefix. None for unreadable content,
        SessionExpired when the cloud won't mint a URL at all.
        """
        url = self._cloud.map_url(self._server, self._device_id, obj_name,
                                  self._endpoint, raw_obj="/" in obj_name)
        if not url:
            raise SessionExpired()
        raw = self._cloud.download(url)
        if not raw:
            _LOGGER.debug("xtl map download failed (%s)", obj_name)
            return None
        try:
            rendered = render_xtl_map(raw, live=live)
        except Exception as ex:  # noqa: BLE001 — never crash the coordinator
            _LOGGER.debug("xtl map render failed (%s): %s", obj_name, ex)
            return None
        if rendered is None:
            _LOGGER.debug("xtl map carries no readable raster (%s)", obj_name)
            return None
        return MapResult(**rendered)

    def fetch_map_infos(self, obj_name: str) -> tuple[dict, list]:
        """Fetch one map-infos KS3 object -> ({mapId: name}, [{name,id,cur}...]).

        The map-infos-report (siid 17 eiid 2) push names this file; the
        official plugin titles its map list from exactly this source, so
        the user-typed titles ("Appart brest") only exist here — never in
        the local 17.13 answer. The same file is ALSO the map catalogue
        the plugin's map manager lists (bug 2026-09-26: the Active Map
        select showed only the current map because this second view was
        never parsed). Same mint-a-URL contract as fetch_obj; ({}, []) on
        anything unreadable (the list is display polish, never fatal).
        """
        from .spec.profiles.xtl import (
            xtl_parse_map_infos_catalog, xtl_parse_map_infos_file,
        )

        url = self._cloud.map_url(self._server, self._device_id, obj_name,
                                  self._endpoint, raw_obj="/" in obj_name)
        if not url:
            _LOGGER.debug("xtl map-infos: no cloud URL for %s", obj_name)
            return {}, []
        raw = self._cloud.download(url)
        if not raw:
            _LOGGER.debug("xtl map-infos download failed (%s)", obj_name)
            return {}, []
        return xtl_parse_map_infos_file(raw), xtl_parse_map_infos_catalog(raw)
