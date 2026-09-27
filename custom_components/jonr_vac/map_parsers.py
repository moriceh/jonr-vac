"""Per-brand map parser dispatch — JONR (xtl) edition.

The upstream xiaomi-vac project dispatches parsers for ijai/xiaomi/dreame/
viomi/roidmi families; this fork ships the xtl (JONR) profile only, and xtl
needs no third-party parser and no key material: the KS3 blob is plain JSON
that xtl_map decodes + renders in-process (see xtl_map.py). What remains here
is the tiny part of the upstream map_parsers.py the coordinator still calls.

Based on xiaomi-vac by letitbe-dull (MIT) — see CREDITS.md.
"""
from __future__ import annotations


def map_url_endpoint(key: str) -> str:
    """Cloud endpoint path segment for fetching a map blob.

    xtl is a confirmed _pro user: the official xtl/JONR Mi Home plugin fetches
    its KS3 map blobs through get_interim_file_url_pro. The connector falls
    back to the plain variant on any failure.
    """
    return "get_interim_file_url_pro"


def parser_key(profile) -> str:
    """Parser family decoding ``profile``'s cloud map blob — xtl only."""
    return profile.brand


def required_map_key_inputs(brand: str) -> frozenset[str]:
    """Keys that MUST be non-empty before a MapFetcher can be constructed.

    xtl: none — the object name comes from the device itself (get-map-data
    17.12 / the map-data-report MQTT push) and the KS3 JSON blob is
    unencrypted.
    """
    if brand == "xtl":
        return frozenset()
    raise ValueError(f"unsupported brand {brand!r} (xtl only)")
