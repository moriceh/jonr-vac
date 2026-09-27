"""Model to runtime profile registry.

"""

from __future__ import annotations

from .profiles.xtl import XTL_XM2216
from .types import ModelProfile, XtlRoomCleanCapability


MODEL_PROFILES: dict[str, ModelProfile] = {
    # --- xtl (JONR P20 Pro) ------------------------------------------------
    "xtl.vacuum.xm2216": XTL_XM2216,

}


def get_profile(model: str) -> ModelProfile | None:
    return MODEL_PROFILES.get(model)


def card_baseline_gaps(profile: ModelProfile | None) -> tuple[str, ...]:
    """Return missing card-baseline features for a profile.

    Map rendering is best-effort and intentionally not gated here.
    """
    if profile is None:
        return ("profile",)
    core = profile.core
    if core is None:
        return ("core",)

    gaps: list[str] = []
    if core.status is None or not core.status_map:
        gaps.append("state")
    if core.start is None:
        gaps.append("start")
    if core.stop is None:
        gaps.append("stop")
    if core.charge is None:
        gaps.append("return_home")
    if core.battery is None:
        gaps.append("battery")
    if core.fan_speed is None or not core.fan_speeds:
        gaps.append("fan_speed")
    if core.water_level is None or not core.water_levels:
        gaps.append("water_level")
    if core.locate is None and core.alarm is None:
        gaps.append("locate")
    room = profile.room_clean
    has_simple_room_clean = (
        room is not None and room.start is not None and room.room_ids is not None
    )
    has_set_room_clean = (
        room is not None
        and room.set_room_clean is not None
        and room.clean_room_ids is not None
        and room.clean_room_mode is not None
        and room.clean_room_oper is not None
    )
    has_typed_room_clean = (
        isinstance(room, XtlRoomCleanCapability) and room.start is not None
    )
    if not (has_simple_room_clean or has_set_room_clean or has_typed_room_clean):
        gaps.append("room_clean")
    return tuple(gaps)


def supports_card_baseline(profile: ModelProfile | None) -> bool:
    return not card_baseline_gaps(profile)


def is_supported(model: str) -> bool:
    """A model is onboardable iff it satisfies the bundled-card baseline.

    This keeps models that only partially resolve from creating an entry that
    renders a broken control surface.
    """
    return supports_card_baseline(get_profile(model))
