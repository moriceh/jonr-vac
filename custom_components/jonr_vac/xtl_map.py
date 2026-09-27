"""xtl (JONR) map pipeline: decode the KS3 map file and render the PNG.

Unlike every other brand, an xtl robot has no cloud-upload slot convention:
the local actions get-map-data (17.12) / get-map-infos (17.13) answer with a
KS3 *file name*, and the file behind it — downloaded through
``get_interim_file_url_pro`` exactly like the official Mi Home plugin does —
is plain JSON:

    {"mapId": 7, "fields": [f0, ..., f8]}
    f0 base64 -> JSON  mapData      {map: b64(LZ4 raster), lz4Len, width,
                                     height, xMin, yMax, resolution=5cm/px,
                                     chargePos(str|obj), ...}
    f1 base64 -> JSON  mapTraceData {trace: b64(LZ4), lz4Len, ...}
    f2 JSON string     robot pos    {"x","y","a"}  (a = heading, 0.01 rad)
    f3 JSON string     rooms        [{id, room_id?, name, type, centerX,
                                      centerY, neibs: "1,4,...", prefer}...]
    f4..f7 base64 ->   wall strings "id,type,x,y,x,y;id,type,..."  (f6 carpet
           string     carries a 3rd preference field: "id,type,pref,x,y,...")
    f8  as-is          carpet preferences; index 9 is spliced out (ignored).

Raster geometry (index formula verified on plugin call sites; 1 map pixel =
1 scene unit — `resolution` only feeds metric labels):

    A[(worldX - xMin) * width + (worldY - yMax)]   row = worldX; row 0 = bottom

so the drawn image is the raster flipped vertically (PNG row 0 = top = max
worldX). pos/chargePos/trace/wall coordinates already live in the plugin's
screen frame (screenX = worldY, screenY = 800 - worldX), which lands on the
raster image as: col = screenX - yMax, row_top = (height - 1) - (worldX - xMin).

The LZ4 payloads are *raw blocks* (no frame magic, no length prefix);
``lz4Len`` is the exact output size. They are decoded by the pure-Python
block decoder below — a direct port of the plugin's own JS codec, so the
integration gains no native dependency.

Rendering mirrors the plugin (main.bundle refs in comments): native palette
0 transparent / 1 wall grey / 2 unassigned floor / >=3 room fill (room_id =
id + 2 fallback; 4 tints, >4 rooms greedy graph-coloured on `neibs`; rooms
with a non-zero ``prefer.order`` cleaning slot take the highlight palette);
whitened cleaning trace per work mode (0 thin line, 1 half core,
2 full core, >=3 not drawn — MapTraceView cases; the halo layer is kept
below the core but transparent, user ask 2026-09-25); dashed zone/wall
strokes in the AreaWallType theme colours; each map cell drawn as a 2x2
pixel block (the native rasteriser's scale arg). Room names, icons and
sequence badges are deliberately NOT baked in: the XVMC card draws them in
screen space from the `rooms` attribute / `predefined_selections`, where
pan/zoom keeps them crisp and aligned (baked labels scale and drift with the
image). The live room-clean highlight is the only task-state the PNG shows.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
import re
import struct
import time

from PIL import Image, ImageChops, ImageDraw, ImageMath

_LOGGER = logging.getLogger(__name__)

# Raster scale. The plugin's native rasteriser draws each map cell as a 2x2
# block (robotCleanerPointsScaleToImageBase64(..., scale=2)) but that 2x is
# only its internal canvas: the on-screen quality comes from the SVG vector
# layer (trace, walls, robot, dock) re-rasterised at display size over it.
# Our single PNG must bake both at once, so we pick a scale that puts the
# longest side near _TARGET_PX -- the plugin's fixed SVG canvas height --
# clamped so small test maps stay cheap and huge maps stay servable.
SCALE = 2                       # nominal scale the pixel-exact tests assume
_TARGET_PX = 800.0
_SCALE_MIN = 2
_SCALE_MAX = 8


def render_scale(width: int, height: int) -> int:
    """Pixels-per-map-cell that lands the longest side near _TARGET_PX."""
    long_side = max(width, height, 1)
    return max(_SCALE_MIN, min(_SCALE_MAX, round(_TARGET_PX / long_side)))

# Fixed plugin SVG canvas height: screenX = worldY, screenY = _CANVAS - worldX.
_CANVAS = 800.0

# mapData.resolution: centimetres per map pixel (plugin default 5). It only
# feeds metric labels, never the raster geometry (1 px = 1 scene unit).
_DEFAULT_RESOLUTION_CM = 5

# Plugin mapColor table (#AARRGGBB!): 0 unknown, 1 wall, 2 floor, rooms cycle
# through originMapColors; prefer.order != 0 rooms take highlightMapColors.
_COLOR_UNKNOWN = (0, 0, 0, 0)
_COLOR_WALL = (0x6D, 0x7D, 0x7D, 0xCC)
_COLOR_FLOOR = (0xD6, 0xE4, 0xE4, 0xFF)
_ROOM_COLORS = [
    (0xBF, 0xE8, 0xE4, 0xFF),  # #FFBFE8E4
    (0xF1, 0xE5, 0xB6, 0xFF),  # #FFF1E5B6
    (0xC5, 0xDA, 0xF6, 0xFF),  # #FFC5DAF6
    (0xF4, 0xCD, 0xBD, 0xFF),  # #FFF4CDBD
]
_HIGHLIGHT_COLORS = [
    (0x2C, 0xD5, 0xAE, 0xFF),  # #FF2CD5AE
    (0xED, 0xC3, 0x57, 0xFF),  # #FFEDC357
    (0x7A, 0xAF, 0xF5, 0xFF),  # #FF7AAFF5
    (0xEA, 0x60, 0x25, 0xFF),  # #FFEA6025
]

# MapTraceView strokes (all white, per work mode attr & 7). The halo layer
# stays BELOW the core (Mi Home's SVG stacking), but the user asked for it
# transparent (2026-09-25): at the baked k (~7 px/cell) the .2 halo read as a
# 35-px room-colour wash that merged adjacent back-and-forth rows and hid the
# neighbouring traces — alpha 0, core untouched (1 unit = the plugin width).
_COLOR_TRACE_HALO = (255, 255, 255, 0)      # was strokeOpacity .2, width 5
_COLOR_TRACE_CORE_HALF = (255, 255, 255, 128)  # mode 1 core: opacity .5 w 1
_COLOR_TRACE_CORE_FULL = (255, 255, 255, 255)  # mode 2 core: opacity 1 w 1
_TRACE_HALO_W = 5      # scene units; px = units * scale at draw time
_TRACE_CORE_W = 1

# AreaWallType theme colours (VirtualView/MopVirtualView/CarpetView).
_COLOR_SWEEP = (0xE3, 0x37, 0x25, 255)  # virtual wall / no-go zone (red)
_COLOR_MOP = (0xFF, 0xAB, 0x47, 255)    # no-mop zone (amber)
_COLOR_CARPET = (0x2C, 0xD5, 0xAE, 255)  # carpet zone (green)
_ZONE_FILL_ALPHA = 76                    # fillOpacity .3
_WALL_W = 1                              # scene units
_DASH = (4, 2)                           # strokeDasharray [4, 2] scene units

# Plugin-identical map glyphs, geometry lifted verbatim from main.bundle
# (v1730143): robot_svg = stacked discs + heading dot on the local +Y lobe,
# the whole 20x20 symbol rotated by the display heading; base_charge_svg =
# white halo (disc + downward tent + ground dot) under a teal disc with two
# 180-degree-symmetric white arrows, anchored at symbol point (17, 23) on
# the charge position and never rotated. Both share one px-per-symbol-unit
# factor k = robot radius px / 9 (robot_svg's outer circle is r=9), so the
# dock keeps the ~34-vs-20 unit width ratio the app draws. The robot disc is
# _ROBOT_R units wide, so k tracks the render scale (see _icon_k).
_COLOR_ROBOT_BODY = (0x12, 0x1C, 0x18, 255)
_COLOR_ROBOT_FACE = (0xEA, 0xF2, 0xF4, 255)
_COLOR_ROBOT_RING = (0xF0, 0xFF, 0xF9, 255)  # thin rim on the heading dot
_COLOR_WHITE = (0xFF, 0xFF, 0xFF, 255)       # dock halo + arrows + dot
_COLOR_DOCK_DISC = (0x2C, 0xD5, 0xAE, 255)   # base_charge_svg disc
_ROBOT_R = 3.5  # outer disc radius, scene units

# siid 2 / piid 2 code 17 (Relocation): the robot is out but doesn't know
# where it is yet. Like the official app, the PNG then hides the robot
# marker and centers the app's relocation badge instead (see _draw_pin).
XTL_STATUS_RELOCATION = 17


def _icon_k(scale: int) -> float:
    """px per glyph symbol unit at the given render scale (robot r=9 units)."""
    return _ROBOT_R * scale / 9.0


# --- LZ4 raw block codec (port of the plugin's JS, module 10079) ----------

def _read_length_run(src: bytes, i: int, total: int) -> tuple[int, int]:
    """LZ4 length extension: keep adding bytes while they hit 255."""
    while True:
        if i >= len(src):
            raise ValueError("lz4: truncated length extension")
        b = src[i]
        i += 1
        total += b
        if b != 255:
            return total, i


def lz4_block_decompress(src: bytes, expected: int) -> bytes:
    """Decode one raw LZ4 block; `expected` is the declared output size.

    Token byte = literal run length (high nibble) + match run length (low
    nibble, MINMATCH 4), separated by a 2-byte little-endian back offset.
    """
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            lit, i = _read_length_run(src, i, lit)
        if i + lit > n:
            raise ValueError("lz4: truncated literal run")
        out += src[i:i + lit]
        i += lit
        if i == n:  # a block legally ends on its literal run
            break
        if i + 2 > n:
            raise ValueError("lz4: truncated match offset")
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(out):
            raise ValueError("lz4: out-of-range back offset")
        ml = token & 0x0F
        if ml == 15:
            ml, i = _read_length_run(src, i, ml)
        pos = len(out) - offset
        end = len(out) + ml + 4  # MINMATCH = 4
        while len(out) < end:
            out.append(out[pos])
            pos += 1
    if len(out) < expected:
        raise ValueError(f"lz4: decoded {len(out)} bytes, expected {expected}")
    return bytes(out[:expected])


# --- tolerant field readers ------------------------------------------------

def _b64_bytes(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4))


def _b64_text(value) -> str | None:
    """base64 -> UTF-8 text, None on anything that isn't (or empty)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return _b64_bytes(value).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return None


def _json_loads(value, default=None):
    """JSON-parse a string field; pass real objects through (some firmware
    revisions hand dicts where the plugin expects strings)."""
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return default


def _point(value) -> dict | None:
    """A {x,y,...} dict with numeric x/y, else None (handles the chargePos
    string-or-object ambiguity the plugin normalises at load time)."""
    obj = _json_loads(value, {})
    if isinstance(obj, dict) and obj.get("x") is not None and obj.get("y") is not None:
        return obj
    return None


def _robot_pose(value) -> dict | None:
    """Robot pose (fields[2]); None when absent OR parked at the scene
    origin. A docked robot uploads "x":0,"y":0,"a":0,"i":0 verbatim (live
    capture 2026-09-23) — the real pose only appears once it leaves the
    dock. Drawing (0,0) literally puts the robot far outside the crop, so
    the zero signal is dropped here and render_xtl_map parks the glyph on
    the dock instead, like the app's isInBaseStation branch."""
    pose = _point(value)
    if pose and float(pose["x"]) == 0.0 and float(pose["y"]) == 0.0:
        return None
    return pose


# --- file decoding -----------------------------------------------------------

def decode_map_file(raw: bytes) -> dict | None:
    """Decode the KS3 current-map JSON file into render-ready parts.

    None = no usable raster (empty map, unknown layout, failed LZ4 block).
    """
    try:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    except AttributeError:
        return None
    obj = _json_loads(text)
    if not isinstance(obj, dict):
        return None
    fields = list(obj.get("fields") or [])
    if len(fields) > 9:
        fields = fields[:9]  # the plugin splices index 9 out; never rendered
    map_data = _json_loads(_b64_text(fields[0] if fields else None))
    if not isinstance(map_data, dict):
        return None
    blob = map_data.get("map")
    try:
        lz4_len = int(map_data.get("lz4Len") or 0)
        width = int(map_data["width"])
        height = int(map_data["height"])
        x_min = int(map_data.get("xMin") or 0)
        y_max = int(map_data.get("yMax") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if not blob or lz4_len <= 1:
        return None  # plugin hasMapData flag: lz4Len > 1
    if width * height != lz4_len:
        # The plugin refuses to render ("地图生成数据错误") on a mismatch.
        _LOGGER.debug("xtl map raster size %d != width*height %d", lz4_len, width * height)
        return None
    try:
        raster = lz4_block_decompress(_b64_bytes(blob), lz4_len)
    except (ValueError, TypeError) as ex:
        _LOGGER.debug("xtl map raster lz4 failed: %s", ex)
        return None
    try:
        resolution = float(map_data.get("resolution") or _DEFAULT_RESOLUTION_CM)
    except (TypeError, ValueError):
        resolution = float(_DEFAULT_RESOLUTION_CM)

    return {
        "map_id": obj.get("mapId"),
        "raster": raster,
        "width": width,
        "height": height,
        "x_min": x_min,
        "y_max": y_max,
        "resolution_cm": resolution,
        "trace": _decode_trace(fields[1] if len(fields) > 1 else None),
        "pos": _robot_pose(fields[2] if len(fields) > 2 else None) or {},
        "areas": _normalize_areas(_json_loads(fields[3] if len(fields) > 3 else None) or []),
        "walls": _decode_wall_field(fields[4] if len(fields) > 4 else None),
        "mop_walls": _decode_wall_field(fields[5] if len(fields) > 5 else None),
        "carpet": _decode_wall_field(fields[6] if len(fields) > 6 else None, leading=3),
        "thres": _decode_wall_field(fields[7] if len(fields) > 7 else None),
        "charge_pos": _point(map_data.get("chargePos")) or {},
    }


# --- trace simplification -----------------------------------------------------
# The map screen's display path is useMapTracePoints -> parseTestMapTraceToSvg-
# Paths (main.bundle module 10259 family), NOT pareseMapTraceData: the chunked
# CONUTS/DP/Bezier pipeline lives in that sibling parser, which the hook never
# calls (porting it once is what turned the trace into a sparse skeleton —
# its tolerance 1 drops 1-unit grid steps in the radial pass). parseTest
# splits the pose stream at break flags into one polyline each, typed by the
# LAST work mode seen, and simplifies the plain runs with SimplifyAP at
# tolerance 0.8 scene units; actionType-1 runs ("gongzi", the spot-clean
# spirals) pass through raw. Its theta accumulators (gCalTheta et al.) are
# computed and never read, so they are not ported.

_TRACE_TOLERANCE = 0.8  # dpUpdateTrackedPoints(buffer, 0.8) at every flush


def _sq_seg_dist(p, p1, p2) -> float:
    """getSqSegDistAP: squared distance from p to segment p1-p2."""
    x, y = p1[0], p1[1]
    dx, dy = p2[0] - x, p2[1] - y
    if dx != 0 or dy != 0:
        t = ((p[0] - x) * dx + (p[1] - y) * dy) / (dx * dx + dy * dy)
        if t > 1:
            x, y = p2[0], p2[1]
        elif t > 0:
            x, y = x + dx * t, y + dy * t
    ex, ey = p[0] - x, p[1] - y
    return ex * ex + ey * ey


def _simplify_radial(points, sq_tolerance):
    """simplifyRadialDistAP: drop points closer than the tolerance to the
    previous kept point. Point objects are reused (identity carries the
    work-mode tags through the simplification)."""
    prev = points[0]
    new_points = [prev]
    point = prev
    for point in points[1:]:
        dx, dy = point[0] - prev[0], point[1] - prev[1]
        if dx * dx + dy * dy > sq_tolerance:
            new_points.append(point)
            prev = point
    if prev is not point:
        new_points.append(point)
    return new_points


def _simplify_dp_step(points, first, last, sq_tolerance, simplified) -> None:
    """simplifyDPStepAP."""
    max_sq, index = sq_tolerance, None
    for i in range(first + 1, last):
        sq = _sq_seg_dist(points[i], points[first], points[last])
        if sq > max_sq:
            index, max_sq = i, sq
    if max_sq > sq_tolerance and index is not None:
        if index - first > 1:
            _simplify_dp_step(points, first, index, sq_tolerance, simplified)
        simplified.append(points[index])
        if last - index > 1:
            _simplify_dp_step(points, index, last, sq_tolerance, simplified)


def _simplify_ap(points, tolerance: float = 1.0, highest_quality: bool = False):
    """SimplifyAP: radial pre-pass then Douglas-Peucker."""
    if len(points) <= 2:
        return list(points)
    sq = tolerance * tolerance
    if not highest_quality:
        points = _simplify_radial(points, sq)
    last = len(points) - 1
    simplified = [points[0]]
    _simplify_dp_step(points, 0, last, sq, simplified)
    simplified.append(points[last])
    return simplified


def _trace_segments(records):
    """Port of parseTestMapTraceToSvgPaths — the pose parser useMapTracePoints
    actually feeds MapTraceView.

    records: (x, y, mode, action_type, is_break) per 7-byte pose. Returns
    [(type, [[x, y], ...])] — one polyline per break, typed by the last work
    mode seen before it. action_type 1 runs are buffered raw ("gongzi"); the
    rest go through the DP buffer, flushed AND cleared at every break and
    every action-type change (unlike pareseMapTraceData, buffers never span
    breaks here — which is why no line is ever drawn across one).
    """
    output: list[list[float]] = []      # m_AllTrackedPoseOutput
    gongzi: list[list[float]] = []      # m_gongziTrackedPose
    dp_buf: list[list[float]] = []      # m_DpPartTrackedPose
    last_type = -1
    last_work_mode = -1
    segments: list[tuple[int, list[list[float]]]] = []

    def flush_dp() -> None:
        nonlocal output
        if dp_buf:
            output = output + _simplify_ap(dp_buf, _TRACE_TOLERANCE)
            del dp_buf[:]

    def flush_gongzi() -> None:
        nonlocal output
        if gongzi:
            output = output + gongzi
            del gongzi[:]

    for x, y, mode, action_type, is_break in records:
        if is_break:
            flush_dp()
            flush_gongzi()
            if output:
                segments.append((last_work_mode, list(output)))
                output = []
            last_type = -1
            continue
        pt = [float(x), float(y)]
        if action_type == 1:
            if action_type != last_type:
                flush_dp()
            gongzi.append(pt)
        else:
            if action_type != last_type:
                flush_gongzi()
            dp_buf.append(pt)
        last_type = action_type
        last_work_mode = mode
    flush_dp()
    flush_gongzi()
    if output:
        segments.append((last_work_mode, list(output)))
    return segments


def _decode_trace(field) -> list[tuple[int, list[tuple[float, float]]]]:
    """mapTraceData {trace: b64(LZ4), lz4Len} -> [(workMode, polyline)]
    in scene coordinates (the plugin draws trace points with no transform).

    7-byte little-endian records: x/y int16 scene px, theta int16 (0.01 rad —
    the display parser reads it into dead accumulators, so it is dropped
    here), attr bit7 break / bits 3-6 action type / bits 0-2 work mode.
    Records flagged isBreak are flush markers, never drawn (plugin parity).
    MapTraceView has no styling case for work mode >= 3, so the parser's
    polylines typed with one are dropped.
    """
    trace_data = _json_loads(_b64_text(field))
    if not isinstance(trace_data, dict) or not trace_data.get("trace"):
        return []
    try:
        blob = lz4_block_decompress(
            _b64_bytes(trace_data["trace"]), int(trace_data.get("lz4Len") or 0)
        )
    except (ValueError, TypeError) as ex:
        _LOGGER.debug("xtl trace lz4 failed: %s", ex)
        return []
    records = []
    for i in range(0, len(blob) - len(blob) % 7, 7):
        x, y, _theta, attr = struct.unpack_from("<hhHB", blob, i)
        records.append(
            (x, y, attr & 7, (attr >> 3) & 15, bool(attr & 0x80))
        )
    return [
        (mode, [(p[0], p[1]) for p in pts])
        for mode, pts in _trace_segments(records)
        if mode < 3
    ]


def _normalize_areas(raw) -> list[dict]:
    """Keep the fields we render/expose, resolving the plugin's
    room_id = room_id ?? id + 2 raster-id convention."""
    areas: list[dict] = []
    if not isinstance(raw, list):
        return areas
    for a in raw:
        if not isinstance(a, dict) or a.get("id") is None:
            continue
        try:
            rid = int(a.get("room_id", int(a["id"]) + 2))
            wid = int(a["id"])
        except (TypeError, ValueError):
            continue
        areas.append({
            "id": wid,
            "room_id": rid,
            "name": a.get("name") or "",
            "type": str(a.get("type") or "0"),
            "center_x": a.get("centerX"),
            "center_y": a.get("centerY"),
            "neibs": str(a.get("neibs") or ""),
            "prefer": a.get("prefer"),
        })
    return areas


def _decode_wall_field(field, leading: int = 2) -> list[dict]:
    """Base64 wall string -> [{id, type, points: [[x,y],...]}].

    Wire format "id,type,x,y,x,y;id,type,..." (carpet records insert a
    preference field at position 2 -> leading=3). Coordinates are plugin
    scene px. type: 1 = line, otherwise rectangle/no-go zone.
    """
    text = _b64_text(field)
    if not text or not text.strip():
        return []
    out: list[dict] = []
    for seg in text.split(";"):
        parts = [p for p in seg.split(",") if p.strip() != ""]
        if len(parts) < leading + 4 or not parts[0].lstrip("-").isdigit():
            continue
        try:
            wid, wtype = int(parts[0]), int(parts[1])
            nums = [int(float(v)) for v in parts[leading:]]
        except ValueError:
            continue
        points = [[nums[i], nums[i + 1]] for i in range(0, len(nums) - 1, 2)]
        if points:
            out.append({"id": wid, "type": wtype, "points": points})
    return out


def _room_colors(areas: list[dict],
                 selected: list | None = None) -> dict[int, tuple[int, int, int, int]]:
    """roomId -> RGBA, mirroring the plugin colourMapping builder: <=4 rooms
    cycle the four tints by areas-list index; past that a greedy colouring of
    the `neibs` adjacency graph on colour INDICES (two neighbours must never
    share an index, even across palettes). The highlight palette applies to
    rooms holding a cleaning-sequence slot (prefer.order != 0) UNION the
    Room-mode selection (selectedAreas) — outside room mode `selected` is
    empty and this is the historical behaviour."""
    highlight = {
        a["room_id"] for a in areas
        if isinstance(a.get("prefer"), dict) and (a["prefer"].get("order") or 0) != 0
    } | set(selected or ())

    def palette_for(rid: int) -> list:
        return _HIGHLIGHT_COLORS if rid in highlight else _ROOM_COLORS

    colors: dict[int, tuple] = {}
    if len(areas) <= 4:
        for idx, a in enumerate(areas):
            colors[a["room_id"]] = palette_for(a["room_id"])[idx % 4]
        return colors
    rid_of: dict[str, int] = {}
    for a in areas:
        rid_of[str(a["id"])] = a["room_id"]
        rid_of.setdefault(str(a["room_id"]), a["room_id"])
    used_index: dict[int, int] = {}
    for a in areas:
        rid = a["room_id"]
        taken = {used_index[n] for n in (
            rid_of.get(tok.strip()) for tok in a["neibs"].split(",") if tok.strip()
        ) if n is not None and n in used_index}
        for cand in range(4):
            if cand not in taken:
                used_index[rid] = cand
                colors[rid] = palette_for(rid)[cand]
                break
    return colors


# --- rendering ---------------------------------------------------------------

def _screen_to_pixel(sx: float, sy: float, data: dict) -> tuple[float, float]:
    """Plugin scene coord -> scaled PNG pixel (cell centre)."""
    k = data.get("scale", SCALE)
    col = sx - data["y_max"]
    row_top = (data["height"] - 1) - ((_CANVAS - sy) - data["x_min"])
    return col * k + k / 2.0, row_top * k + k / 2.0


def _pixel_to_scene(px: float, py: float, data: dict) -> tuple[float, float]:
    """Exact inverse of _screen_to_pixel (PNG pixel -> plugin scene coord).
    Feeds the XVMC calibration contract: cells centre-align on scene ints,
    so pixel corners land on .5 scene offsets — never re-derive it."""
    k = data.get("scale", SCALE)
    sx = (px - k / 2.0) / k + data["y_max"]
    sy = _CANVAS - data["x_min"] - (data["height"] - 1) + (py - k / 2.0) / k
    return sx, sy


# Room type (plugin AreaIconType codes, services.yaml docs) -> mdi icon.
# The card's ROOM mode paints these on the room centroid; unknown/garbage
# codes fall back to mdi:broom (the card's own default).
_ROOM_ICONS = {
    0: "mdi:floor-plan", 1: "mdi:sofa", 2: "mdi:shower", 3: "mdi:bed",
    4: "mdi:food-variant", 5: "mdi:flower", 6: "mdi:silverware-fork-knife",
    7: "mdi:book-open-page-variant", 8: "mdi:dumbbell", 9: "mdi:door",
    10: "mdi:sun", 11: "mdi:teddy-bear", 13: "mdi:couch",
    14: "mdi:washing-machine", 15: "mdi:door-open", 16: "mdi:bed", 17: "mdi:bed",
}


def _room_icon(type_code) -> str:
    try:
        return _ROOM_ICONS.get(int(type_code), "mdi:broom")
    except (TypeError, ValueError):
        return "mdi:broom"


def _boundary_loop(cells: set) -> list:
    """Outer boundary of a 4-connected cell set as an ordered corner loop
    (cell-corner grid, CW). Each occupied/unoccupied cell side emits one
    directed edge with the interior on its right; edges are then chained,
    the largest |shoelace| loop wins (holes / diagonals are discarded)."""
    edges: dict[tuple, list] = {}
    for c, r in cells:
        if (c, r - 1) not in cells:
            edges.setdefault((c, r), []).append((c + 1, r))          # top ->
        if (c + 1, r) not in cells:
            edges.setdefault((c + 1, r), []).append((c + 1, r + 1))  # right v
        if (c, r + 1) not in cells:
            edges.setdefault((c + 1, r + 1), []).append((c, r + 1))  # bottom <-
        if (c - 1, r) not in cells:
            edges.setdefault((c, r + 1), []).append((c, r))          # left ^

    def area(loop) -> float:
        s = 0.0
        for i, (x0, y0) in enumerate(loop):
            x1, y1 = loop[(i + 1) % len(loop)]
            s += x0 * y1 - x1 * y0
        return abs(s) / 2.0

    loops = []
    while edges:
        start = next(iter(edges))
        loop: list = []
        cur, closed = start, False
        while True:
            outs = edges.get(cur)
            if not outs:  # open chain (defensive, well-formed masks close)
                break
            nxt = outs.pop()
            if not outs:
                del edges[cur]
            loop.append(cur)
            cur = nxt
            if cur == start:
                closed = True
                break
        if closed and len(loop) >= 4:
            loops.append(loop)
    return max(loops, key=area) if loops else []


def _simplify_orthogonal(pts: list[tuple[float, float]]) -> list[list[float]]:
    """Keep only turning points of a rectilinear loop (straight-run corners
    carry no information; the plugin's selection tint is the same shape)."""
    out = []
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (x1 - x0) * (y2 - y1) != (y1 - y0) * (x2 - x1):
            out.append([round(x1, 2), round(y1, 2)])
    return out


def _room_attribute(a: dict, geo: dict | None) -> dict:
    """One `rooms` record entry (XVMC contract): the card centres the
    room's icon/label on `x`/`y`.

    The KS3 areas JSON stores the plugin centres TRANSPOSED against the
    raster/trace/calibration scene frame. Live map 2026-09-23: room 4
    centre (376,459) — 376 does not even reach the crop's own x_min of
    379.5, and (459,376) is the reading that sits inside the traced
    outline; same for rooms 5 and 6. So x = centerY, y = centerX, while
    centre_x/centre_y stay raw for our own dashboards. A swapped centre
    outside the traced bbox is garbage: fall back to the bbox centre.
    """
    entry = {
        **a,
        "x": a.get("center_y"), "y": a.get("center_x"),
        "icon": _room_icon(a["type"]),
    }
    if geo:
        if not (entry["x"] is not None and entry["y"] is not None
                and geo["x0"] <= entry["x"] <= geo["x1"]
                and geo["y0"] <= entry["y"] <= geo["y1"]):
            entry["x"] = (geo["x0"] + geo["x1"]) / 2.0
            entry["y"] = (geo["y0"] + geo["y1"]) / 2.0
        entry.update(geo)
    return entry


def _room_geometry(raster: bytes, w: int, h: int, room_ids,
                   data: dict) -> dict[int, dict]:
    """Bbox + outline per room, in scene units — the XVMC rooms contract
    (`attributes["rooms"][id].{x0,y0,x1,y1,outline}`, true polygons, richer
    than Dreame's bbox-only map extractor). Cells of the raster carrying a
    room_id byte form the mask; an absent room yields no geometry."""
    out: dict[int, dict] = {}
    k = data.get("scale", SCALE)
    for rid in room_ids:
        cells = {(c, r) for r in range(h) for c in range(w)
                 if raster[(h - 1 - r) * w + c] == rid}
        if not cells:
            continue
        loop = _boundary_loop(cells)
        # loop nodes are cell CORNERS -> pixel coords are (c*k, r*k)
        pts = [_pixel_to_scene(c * k, r * k, data) for c, r in loop]
        outline = _simplify_orthogonal(pts)
        if not outline:
            continue
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        out[rid] = {"x0": min(xs), "y0": min(ys),
                    "x1": max(xs), "y1": max(ys), "outline": outline}
    return out


def _calibration_points(data: dict, img_w: int, img_h: int) -> list[dict]:
    """The XVMC `calibration_source: {camera: true}` contract: three pairs
    tying PNG pixels (`map`, the card's image frame) to the robot frame
    (`vacuum`, our scene units — the space room outlines and the
    clean_zone/clean_segment service coordinates live in). TL, TR, BL: three
    points select the card's affine branch of CoordinatesConverter."""
    pts = []
    for px, py in ((0.0, 0.0), (float(img_w), 0.0), (0.0, float(img_h))):
        sx, sy = _pixel_to_scene(px, py, data)
        pts.append({"vacuum": {"x": round(sx, 2), "y": round(sy, 2)},
                    "map": {"x": px, "y": py}})
    return pts


# --- embedded-card vector contract (www/xiaomi-vac-card.js) ------------------
# The bundled card's map page draws from the vector the /maps endpoint serves:
# a labelled grid (`grid_rle`), traced room contours (`room_chains`), a room
# list with metre centres, and metre overlays (charger/vacuum/path/walls/
# carpets). Upstream this contract was only ever produced by map_vector.py for
# the ijai/xiaomi-JSON brands; xtl never fed it, which is why the card showed
# no map page at all (`_maps().filter(m => m.rooms)`). The bridge below builds
# it from the parts _render_map already renders — the camera `attributes` stay
# untouched (XVMC and our dashboards read those).

# The card paints raster cells with room labels hard-coded to this band and
# tints them by index into `rooms`; outside it, a shipped grid paints a fully
# transparent layer OVER the traced fills (the upstream extract_json_grid
# guard, which is also how _roomRaster decides: it bails out when `size` is
# absent, leaving the traced chains as solid fills). JONR room_ids are the
# plugin's room_id = id + 2 (usually 3-6), so xtl takes the chains-only path
# — no `size`, no grid values — and only ships the grid when every label
# really lands in band.
_CARD_ROOM_BAND = (10, 59)


def _m(x) -> float:
    return round(x, 3)


# Metre laws: the crop anchors scene x on the grid columns and scene y on the
# rows (_screen_to_pixel: col = sx - y_max, row_top = (h-1) - ((800 - sy) -
# x_min)), and _pixel_to_scene bakes cell-corner coords on .5 scene offsets
# (pixel (0,0) -> scene (y_max-0.5, 800-x_min-(h-1)-0.5), the pair the
# calibration test pins). The card's cell(c) = [minX + c[0]*res,
# minY + c[1]*res] law then agrees with the PNG exactly — chains (entered as
# [c, h - r] over _boundary_loop's top-down corner grid) and overlays (their
# raw scene pose through the two laws below) land on the same pixel — at the
# half-cell crop anchors bounds carries. The centroid test replays both
# against the PNG gridlines; never re-derive it.
def _scene_x_to_metres(sx: float, res: float) -> float:
    return sx * res


def _scene_y_to_metres(sy: float, res: float) -> float:
    # scene y grows south (the PNG top row reads the smallest sy), so the
    # north-up metre axis mirrors it — no rotation, no transpose.
    return (_CANVAS - sy) * res


def _card_grid_rle(raster: bytes, room_ids: set) -> list:
    """[value,run] pairs the card expands row-major, row 0 at the bottom
    (south). Our raster byte row 0 is already the drawn bottom (the vertical
    flip happens at PNG compose time), so the bytes go verbatim; only room_id
    cells keep a value, everything else paints 0 = transparent. EVERY run is
    emitted (zero runs included): the card advances its cursor by the emitted
    run length, so a dropped middle run would shift every later cell."""
    present = set(raster) & room_ids
    if not present:
        return []
    if not all(_CARD_ROOM_BAND[0] <= v <= _CARD_ROOM_BAND[1] for v in present):
        return []  # upstream chains-only guard (see _CARD_ROOM_BAND)
    out: list = []
    prev, run = None, 0
    for v in raster:
        v = v if v in room_ids else 0
        if v == prev:
            run += 1
            continue
        if prev is not None:
            out += [prev, run]
        prev, run = v, 1
    if prev is not None:
        out += [prev, run]
    return out


def _card_room_chains(raster: bytes, w: int, h: int, room_ids) -> list:
    """Exact room contours as card grid-cell corner rings. _boundary_loop
    nodes are cell corners on the same cell-corner grid the card's
    cell(c) = [minX + c[0]*res, minY + c[1]*res] maps to metres; the loop is
    walked top-down (r increases south, the PNG top row) while the card's
    row index increases north, so the corner (c, r) enters as (c, h - r) —
    the same flip the PNG compose applies, and the centroid test pins it
    against those pixels."""
    chains = []
    for rid in sorted(set(room_ids)):
        cells = {(c, r) for r in range(h) for c in range(w)
                 if raster[(h - 1 - r) * w + c] == rid}
        if not cells:
            continue
        loop = _boundary_loop(cells)
        if len(loop) < 4:
            continue
        # Loop nodes are top-down PNG cell-corner lines (r increases south);
        # the card's cell(c) counts rows from the SOUTH, so the line enters as
        # h - r. Derivation (pinned by the centroid test against the PNG
        # pixels, never re-derive): _pixel_to_scene sends corner (c, r) to
        # scene (y_max + c - 0.5, _CANVAS - x_min - (h - 1) + r - 0.5), the
        # card maps [c, h - r] to (minX + c*res, minY + (h - r)*res) — both
        # laws agree exactly for the bounds below.
        ring = _simplify_orthogonal([(c, h - r) for c, r in loop])
        if ring:
            # `rings` is a LIST OF RINGS (each ring a point list) — the card
            # iterates chain.rings then maps every point through cell(c).
            chains.append({"id": rid, "rings": [[list(pt) for pt in ring]]})
    return chains


def _card_vector(data: dict, attributes: dict, pos: dict | None,
                 charge: dict | None, relocating: bool) -> dict:
    """The /maps vector for the bundled card (see the contract block above).
    Pure function of the decoded blob + the resolved robot pose, so the
    content_hash stays exactly sha256(raw) for ordinary renders."""
    w, h = data["width"], data["height"]
    res = float(data.get("resolution_cm") or _DEFAULT_RESOLUTION_CM) / 100.0
    room_ids = {a["room_id"] for a in data["areas"]}

    rooms = []
    for a in data["areas"]:
        entry = attributes["rooms"].get(str(a["room_id"]))
        if not entry or "outline" not in entry:
            continue  # no traced geometry = nothing to tap; keep it off-list
        x0, y0, x1, y1 = entry["x0"], entry["y0"], entry["x1"], entry["y1"]
        rooms.append({
            "id": a["room_id"],
            "name": entry["name"],
            # _room_attribute already un-transposed and bbox-fell-back on
            # x/y — metres straight, same law as the XVMC divider 20.
            "cx": _m(_scene_x_to_metres(entry["x"], res)),
            "cy": _m(_scene_y_to_metres(entry["y"], res)),
            # bbox fallback rectangle (card draws it only without chains);
            # y0 (the outline's min sy) is the NORTH edge, so it enters as
            # bbox[1] — min-x/min-y/max-x/max-y in the card's metre space.
            "bbox": [_m(_scene_x_to_metres(x0, res)),
                     _m(_scene_y_to_metres(y1, res)),
                     _m(_scene_x_to_metres(x1, res)),
                     _m(_scene_y_to_metres(y0, res))],
        })

    vec: dict = {
        "map_id": data["map_id"],
        "resolution": res,
        # Half-cell crop anchors (the .5 corner offset _pixel_to_scene
        # bakes): cell (c, h - r) metres must equal the PNG position of the
        # scene coord _pixel_to_scene gives that corner — both axes match at
        # exactly these bounds (derivation in the metre-laws comment).
        "bounds": {"minX": _m((data["y_max"] - 0.5) * res),
                   "minY": _m((data["x_min"] - 0.5) * res)},
        "rooms": rooms,
        "room_chains": _card_room_chains(data["raster"], w, h, room_ids),
    }
    grid = _card_grid_rle(data["raster"], room_ids)
    if grid:
        # `size`/`grid_rle` stay ABSENT off-band, never empty: the card's
        # _roomRaster treats an empty array as a present grid (![] is false
        # in JS), paints the all-zero grid as a transparent <image> and then
        # blanks every traced chain (fill = raster ? transparent : tint).
        # Upstream _empty_grid omits them for the same reason.
        vec["size"] = {"x": w, "y": h}
        vec["grid_rle"] = grid
    if charge:
        vec["charger"] = {"x": _m(_scene_x_to_metres(charge["x"], res)),
                          "y": _m(_scene_y_to_metres(charge["y"], res))}
    # Mirrors the PNG glyph law: `pos` is the pose _render_map resolved (docked
    # poses already parked on chargePos+5), and a relocating robot shows the
    # badge there instead of the dot (the card draws m.vacuum verbatim).
    if pos and not relocating:
        vec["vacuum"] = {"x": _m(_scene_x_to_metres(pos["x"], res)),
                         "y": _m(_scene_y_to_metres(pos["y"], res))}
    segments = [[[_m(_scene_x_to_metres(x, res)),
                  _m(_scene_y_to_metres(y, res))]
                 for x, y in pts]
                for _mode, pts in data["trace"] if len(pts) > 1]
    if segments:
        vec["path_segments"] = segments
    walls = []
    for wl in data["walls"]:
        if wl["type"] != 1 or len(wl["points"]) < 2:
            continue
        (x1, y1), (x2, y2) = wl["points"][0], wl["points"][1]
        walls.append([_m(_scene_x_to_metres(x1, res)),
                      _m(_scene_y_to_metres(y1, res)),
                      _m(_scene_x_to_metres(x2, res)),
                      _m(_scene_y_to_metres(y2, res))])
    if walls:
        vec["walls"] = walls
    carpets = []
    for zone in data["carpet"]:
        pts = zone["points"]
        if len(pts) < 4:
            continue
        flat = [v for (x, y) in pts[:4] for v in
                (_m(_scene_x_to_metres(x, res)),
                 _m(_scene_y_to_metres(y, res)))]
        carpets.append(flat)
    if carpets:
        vec["carpets"] = carpets
    return vec


def _dash_polyline(draw: ImageDraw.ImageDraw, pts, fill, width,
                   dash=_DASH) -> None:
    """Dashed polyline (PIL has no native dash): stroke [4, 2] scene units,
    mirroring the plugin's strokeDasharray [4/scaleRatio, 2/scaleRatio]."""
    on, off = dash
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length <= 0:
            continue
        ux, uy = dx / length, dy / length
        d = 0.0
        while d < length:
            end = min(d + on, length)
            draw.line(
                [(x1 + ux * d, y1 + uy * d), (x1 + ux * end, y1 + uy * end)],
                fill=fill, width=width,
            )
            if end >= length:
                break
            d = end + off


def _rect_from_points(points):
    """VirtualArea zonePath corners: 4 points verbatim, else the bbox."""
    if len(points) >= 4:
        return [tuple(p) for p in points[:4]]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [(min(xs), min(ys)), (max(xs), min(ys)), (max(xs), max(ys)), (min(xs), max(ys))]


def _trace_strokes(draw: ImageDraw.ImageDraw, px, mode: int, k: float) -> None:
    """One MapTraceView polyline (white, per work mode) at pixel scale k."""
    if mode == 0:
        draw.line(px, fill=(255, 255, 255, 255),
                  width=max(1, round(0.5 * _TRACE_CORE_W * k)), joint="curve")
        return
    if _COLOR_TRACE_HALO[3]:  # transparent halo (user ask 2026-09-25) → skip
        draw.line(px, fill=_COLOR_TRACE_HALO,
                  width=max(1, _TRACE_HALO_W * k), joint="curve")
    draw.line(
        px,
        fill=_COLOR_TRACE_CORE_HALF if mode == 1 else _COLOR_TRACE_CORE_FULL,
        width=max(1, _TRACE_CORE_W * k),
        joint="curve",
    )


# The card re-rasterises the plugin's SVG trace at display size, so the
# browser antialiases it for free; our baked PNG cannot (PIL has no AA), and
# a 1-unit core straight at final scale is exactly the stair-step the user
# saw. Draw the trace layer the same way the glyphs are drawn (see the _SS
# note): strokes at _TRACE_SS inside the trace bbox only (halo first — it is
# transparent today but stays UNDER the cores when its alpha returns), then
# BOX down through premultiplied alpha. Geometry is linear in k, so the
# SS stroke centres land on the very pixels _screen_to_pixel gives at 1x —
# the XVMC calibration contract is untouched, and cells outside the bbox
# stay bit-exact (empty canvas composites as a no-op).
_TRACE_SS = 4


def _antialiased_trace_layer(segments, data,
                             size: tuple[int, int]) -> Image.Image:
    """Full-size trace layer (final px), strokes supersampled by _TRACE_SS."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    k = data.get("scale", SCALE)
    traced = [(mode, [_screen_to_pixel(x, y, data) for x, y in pts])
              for mode, pts in segments]
    traced = [(mode, px) for mode, px in traced if len(px) >= 2]
    if not traced:
        return layer
    xs = [p[0] for _, px in traced for p in px]
    ys = [p[1] for _, px in traced for p in px]
    pad = math.ceil(_TRACE_HALO_W * k / 2) + 2
    x0 = max(0, int(math.floor(min(xs) - pad)))
    y0 = max(0, int(math.floor(min(ys) - pad)))
    x1 = min(size[0], int(math.ceil(max(xs) + pad)))
    y1 = min(size[1], int(math.ceil(max(ys) + pad)))
    if x1 <= x0 or y1 <= y0:
        return layer
    canvas = Image.new("RGBA", ((x1 - x0) * _TRACE_SS, (y1 - y0) * _TRACE_SS),
                       (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    for mode, px in traced:
        _trace_strokes(draw, [(_TRACE_SS * (x - x0), _TRACE_SS * (y - y0))
                              for x, y in px], mode, k * _TRACE_SS)
    # BOX downscale: the SS canvas is a 4x supersample, so the area average
    # is the right reconstruction filter; LANCZOS' negative lobes showed as
    # a faint room-colour ring hugging the strokes (user report 2026-09-25).
    layer.alpha_composite(
        _pma_down(canvas, (x1 - x0, y1 - y0), Image.BOX), (x0, y0))
    return layer


def _draw_walls(draw: ImageDraw.ImageDraw, walls, data, color, lines_as_zones=False) -> None:
    """Dashed stroke + .3-alpha rect fill, per AreaWallType theme colour."""
    k = data.get("scale", SCALE)
    width = max(1, _WALL_W * k)
    dash = (_DASH[0] * k, _DASH[1] * k)
    for wall in walls:
        px = [_screen_to_pixel(x, y, data) for x, y in wall["points"]]
        if len(px) < 2:
            continue
        if wall["type"] == 1 and not lines_as_zones:
            _dash_polyline(draw, px, color, width, dash=dash)
        else:
            corners = _rect_from_points(px)
            fill = color[:3] + (_ZONE_FILL_ALPHA,)
            draw.polygon(corners, fill=fill)
            _dash_polyline(draw, corners + [corners[0]], color, width, dash=dash)


_ICON_K = _icon_k(SCALE)  # nominal-scale reference (see _icon_k for a run)

# Station glyphs, geometry lifted from main.bundle (v1730143) verbatim —
# the base_*_svg Symbols the plugin's StationIcon <use> selects by activity
# (stationStatus getter @874883). Common skeleton: ground dot (18, 32, r2)
# + halo (18, 13, r12) + tent + activity disc (18, 12.976, r10) carrying a
# white motif. The bundle's motifs are SVG paths (fillRule evenodd), so the
# port keeps the `d` strings and flattens them through the tiny path
# rasteriser below instead of hand-triangles — same anchor law as before
# (<use x=-17 y=-23> puts symbol point (17, 23) on chargePos).
#
# Activity switch mirrored from the plugin (stationStatus getter): WashMop
# -> base_wash_svg (teal, soap bubble + three four-point sparkles), ClctDust
# -> base_collect_svg (teal, bucket + five dust dots), HotDry/WindDry ->
# base_wind_svg (teal, three wind swirls — StationIcon's '#base_dry_svg'
# href is NEVER registered by CommonSvgs, so wind is what the map actually
# draws while drying), Charging -> base_charge_svg (teal, two 180-degree
# lightnings), and everything else -> base_normal_svg (dark #121C18 disc,
# same lightnings): the dock at rest is the DARK icon — teal lightning means
# active charging. ChargeAsleep and the returning/paused tokens are None on
# the plugin side too, so they keep the resting icon.
_DOCK_HALO_CIRCLES = [(18, 32, 2, _COLOR_WHITE), (18, 13, 12, _COLOR_WHITE)]
_DOCK_HALO_TENT_D = (
    "M24.675 22.704s-3.57 2.057-4.722 3.148c-.815.773-1.479 2.03-1.796 2.698"
    "-.078.162-.327.161-.404-.001-.306-.647-.949-1.85-1.84-2.697-1.275-1.21"
    "-4.594-3.148-4.594-3.148h13.356Z")
_COLOR_DOCK_DISC_REST = (0x12, 0x1C, 0x18, 255)  # base_normal_svg disc #121C18
_DOCK_DISC = (18, 12.976, 10, _COLOR_DOCK_DISC)
_DOCK_LIGHTNING_D = (
    "M16.192 18.689a.5.5 0 0 1-.822-.486l1.497-6.337 4.289 1.304a.5.5 0 0 1"
    " .19.849l-5.154 4.67Z",
    "M18.808 7.02a.5.5 0 0 1 .822.485l-1.497 6.337-4.289-1.304a.5.5 0 0 1"
    "-.19-.849l5.154-4.67Z",
)
# collect: bucket outline (one even-odd path: rim ellipse + body + handle)
# + five dust dots.
_DOCK_BUCKET_D = (
    "M12.016 10.039a.9.9 0 1 0-1.746.436l1.746-.436Zm13.41.436a.9.9 0 0 0"
    "-1.747-.436l1.747.436Zm-2.323 5.582-.874-.219.874.219Zm-1.94.614h-6.63"
    "v1.8h6.63v-1.8Zm-7.697-.833-1.45-5.8-1.746.437 1.45 5.8 1.746-.437Z"
    "m10.213-5.8-1.45 5.8 1.747.437 1.45-5.8-1.747-.436Zm-9.146 6.633a1.1"
    " 1.1 0 0 1-1.067-.833l-1.746.437a2.9 2.9 0 0 0 2.813 2.196v-1.8Zm6.63"
    " 1.8a2.9 2.9 0 0 0 2.813-2.196l-1.747-.437a1.1 1.1 0 0 1-1.067.833v1.8Z")
_DOCK_BUCKET_DOTS = [(20.59, 13.61, 0.914), (15.714, 9.343, 0.914),
                     (14.617, 13.122, 0.427), (18.884, 11.903, 0.427),
                     (21.505, 10.867, 0.61)]
# wash: soap bubble (outer contour + even-odd counter) + one path holding
# the three four-point sparkles.
_DOCK_BUBBLE_D = (
    "M12.67 9.675c-.044-.518.175-1.066.685-1.169.991-.2 2.797-.285 5.284.737"
    " 1.988.816 3.671.688 4.85.362.797-.22 1.806.387 1.429 1.122-.239.463"
    "-.616 1.017-1.209 1.844-.875 1.222-.681 2.764-.398 3.778.164.586-.106"
    " 1.286-.69 1.456-1.144.333-2.882.553-4.907-.662s-3.983-.61-5.32-.196c"
    "-.708.219-1.242.384-1.52.196-.802-.543-.055-3.04 1.126-4.857a4.086"
    " 4.086 0 0 0 .67-2.61Zm.672 3.482q.963-1.482.945-3.167 1.65-.127 3.745"
    " .733 2.296.943 4.54.686-.078.112-.163.23-1.396 1.948-.74 4.748-1.62"
    " .291-3.132-.616-1.752-1.051-3.83-.935-.97.055-2.383.462.324-1.072"
    " 1.018-2.14Z")
_DOCK_SPARKLES_D = (
    "m19.718 15.435-.533-1.185L18 13.718l1.185-.533.533-1.185.532 1.185"
    " 1.185.533-1.185.532-.532 1.185ZM15.425 12.004l-.27-.583-.584-.271"
    " .583-.272.271-.592.272.592.592.272-.592.271-.272.583ZM17.993 8l-.36"
    "-.775-.776-.361.775-.361.361-.789.361.789.789.36-.789.362-.36.775Z")
# dry: the three wind swirls (base_wind_svg's solid paths; its second
# evenodd overlay is white-on-white, a no-op in raster).
_DOCK_SWIRLS_D = (
    "M19.916 11.223c0-1.77 1.335-2.91 1.4-2.965a.536.536 0 0 1 .752.065"
    ".528.528 0 0 1-.063.747c-.06.051-1.338 1.164-.945 2.77.166.678.566"
    " 1.212.954 1.728.518.692 1.053 1.407.98 2.402-.132 1.75-1.325 2.846"
    "-1.376 2.892a.536.536 0 0 1-.754-.036.528.528 0 0 1 .036-.75c.008-.007"
    " .93-.868 1.028-2.184.046-.602-.315-1.081-.77-1.69-.431-.573-.919"
    "-1.224-1.135-2.11a3.675 3.675 0 0 1-.107-.87Zm-3.336-.134c0-1.77 1.334"
    "-2.91 1.4-2.964a.536.536 0 0 1 .752.064.528.528 0 0 1-.063.747c-.06"
    ".051-1.338 1.164-.945 2.77.166.678.566 1.212.953 1.728.519.692 1.054"
    " 1.407.98 2.403-.131 1.75-1.324 2.846-1.375 2.891a.536.536 0 0 1-.754"
    "-.036.528.528 0 0 1 .036-.75c.007-.007.93-.867 1.028-2.184.046-.601"
    "-.316-1.08-.77-1.69-.432-.573-.92-1.224-1.136-2.11a3.675 3.675 0 0 1"
    "-.106-.869Zm-3.58 0c0-1.77 1.334-2.91 1.4-2.964a.536.536 0 0 1 .752"
    ".064.528.528 0 0 1-.063.747c-.06.051-1.338 1.164-.945 2.77.165.678"
    ".566 1.212.953 1.728.519.692 1.054 1.407.98 2.403-.131 1.75-1.324"
    " 2.846-1.376 2.891a.536.536 0 0 1-.754-.036.528.528 0 0 1 .037-.75"
    "c.007-.007.929-.867 1.028-2.184.046-.601-.316-1.08-.77-1.69-.432-.573"
    "-.92-1.224-1.136-2.11a3.675 3.675 0 0 1-.106-.869Z")

# variant -> (disc colour, motif paths, motif circles).
_DOCK_VARIANTS = {
    "normal": (_COLOR_DOCK_DISC_REST, _DOCK_LIGHTNING_D, ()),
    "charge": (_COLOR_DOCK_DISC, _DOCK_LIGHTNING_D, ()),
    "collect": (_COLOR_DOCK_DISC, (_DOCK_BUCKET_D,), _DOCK_BUCKET_DOTS),
    "wash": (_COLOR_DOCK_DISC, (_DOCK_BUBBLE_D, _DOCK_SPARKLES_D), ()),
    "dry": (_COLOR_DOCK_DISC, (_DOCK_SWIRLS_D,), ()),
}

# plugin stationStatus getter, tokens verbatim (curRobotStatus spellings).
_STATION_ICON_BY_TOKEN = {
    "WashMop": "wash",
    "ClctDust": "collect",
    "HotDry": "dry",
    "WindDry": "dry",
    "Charging": "charge",
}


def _station_variant(live: dict | None) -> str:
    """Map the 17.49 station token onto a base_*_svg variant; case
    tolerant like xtl_station_status_option (which mirrors the same table
    into ENUM options — parity pinned in the pure tests)."""
    raw = (live or {}).get("station_status")
    if isinstance(raw, str) and raw:
        key = raw.strip().lower()
        for token, variant in _STATION_ICON_BY_TOKEN.items():
            if token.lower() == key:
                return variant
    return "normal"


# --- tiny SVG path rasteriser (dock motifs only) -----------------------------
# PIL has no path fill; the four activity motifs are the bundle's own
# <path d> data. The d-grammar the bundle uses is a small closed set
# (M m L l H h V v C c Q q A a Z z) — flatten it into polygons and fill with
# even-odd parity: each subpath becomes its own L mask, and XOR-ing the
# masks reproduces the evenodd fillRule the plugin draws with (holes:
# bubble counter, bucket rim ring). Curves sample fine enough that at the
# _SS sprite scale every feature stays several pixels wide through the
# LANCZOS downscale.

_PATH_TOKEN_RE = re.compile(
    r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_PATH_POLY_CACHE: dict[str, list[list[tuple[float, float]]]] = {}


def _cubic_pts(p0, p1, p2, p3, n):
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1.0 - t
        out.append((u * u * u * p0[0] + 3 * u * u * t * p1[0]
                    + 3 * u * t * t * p2[0] + t * t * t * p3[0],
                    u * u * u * p0[1] + 3 * u * u * t * p1[1]
                    + 3 * u * t * t * p2[1] + t * t * t * p3[1]))
    return out


def _arc_to_cubics(p0, rx, ry, phi, large, sweep, p1):
    """SVG endpoint arc -> cubic control points (W3C F.6), then flatten."""
    if p0 == p1 or rx == 0.0 or ry == 0.0:
        return [p1] if p1 != p0 else []
    rx, ry = abs(rx), abs(ry)
    c, s = math.cos(phi), math.sin(phi)
    dx, dy = (p0[0] - p1[0]) / 2.0, (p0[1] - p1[1]) / 2.0
    x1p, y1p = c * dx + s * dy, -s * dx + c * dy
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1.0:  # radii too small: scale up (F.6.6)
        r = math.sqrt(lam)
        rx, ry = rx * r, ry * r
    num = max(0.0, (rx * rx * ry * ry - rx * rx * y1p * y1p
                    - ry * ry * x1p * x1p)
               / (rx * rx * y1p * y1p + ry * ry * x1p * x1p))
    co = math.sqrt(num) * (-1.0 if large == sweep else 1.0)
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx, cy = c * cxp - s * cyp + (p0[0] + p1[0]) / 2.0, \
        s * cxp + c * cyp + (p0[1] + p1[1]) / 2.0

    def ang(ux, uy, vx, vy):
        dot = ux * vx + uy * vy
        nrm = math.hypot(ux, uy) * math.hypot(vx, vy)
        a = math.acos(max(-1.0, min(1.0, dot / nrm)))
        if ux * vy - uy * vx < 0:
            a = -a
        return a

    th1 = ang(1.0, 0.0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dth = ang((x1p - cxp) / rx, (y1p - cyp) / ry,
              (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dth > 0:
        dth -= 2 * math.pi
    elif sweep and dth < 0:
        dth += 2 * math.pi
    pts = []
    segs = max(1, int(math.ceil(abs(dth) / (math.pi / 2.0))))
    dseg = dth / segs
    k = 4.0 / 3.0 * math.tan(dseg / 4.0)
    for i in range(segs):
        t1, t2 = th1 + i * dseg, th1 + (i + 1) * dseg
        c1, s1 = math.cos(t1), math.sin(t1)
        c2, s2 = math.cos(t2), math.sin(t2)

        def at(tt_cost, tt_sin):
            ex, ey = rx * tt_cost, ry * tt_sin
            return (c * ex - s * ey + cx, s * ex + c * ey + cy)

        e1, e2 = at(c1, s1), at(c2, s2)
        t1v = (-rx * s1 * k, ry * c1 * k)
        t2v = (-rx * s2 * k, ry * c2 * k)
        c1p = (e1[0] + c * t1v[0] - s * t1v[1], e1[1] + s * t1v[0] + c * t1v[1])
        c2p = (e2[0] - c * t2v[0] + s * t2v[1], e2[1] - s * t2v[0] - c * t2v[1])
        pts.extend(_cubic_pts(e1, c1p, c2p, e2, 8)[:-1])
    pts.append(p1)
    return pts


def _svg_path_polygons(d: str) -> list[list[tuple[float, float]]]:
    """Parse an SVG `d` into closed subpath polygons (symbol units)."""
    cached = _PATH_POLY_CACHE.get(d)
    if cached is not None:
        return cached
    toks = [m.group(0) for m in _PATH_TOKEN_RE.finditer(d)]
    pos, n = 0, len(toks)
    polys: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    x = y = 0.0
    subx = suby = 0.0
    ctrl = None  # last curve control point (for S/T smoothing)
    curve = None

    def num():
        nonlocal pos
        v = float(toks[pos])
        pos += 1
        return v

    def close():
        nonlocal cur
        if len(cur) > 2:
            polys.append(cur)
        cur = []

    cmd = ""
    while pos < n:
        tok = toks[pos]
        if tok.isalpha() and len(tok) == 1:
            cmd = tok
            pos += 1
        elif cmd in ("M",):
            cmd = "L"          # implicit lineto after a moveto pair
        elif cmd == "m":
            cmd = "l"
        if cmd in ("Z", "z"):
            cur.append((subx, suby))
            x, y = subx, suby
            close()
            continue
        rel = cmd.islower()
        if cmd in ("M", "m", "L", "l"):
            nx, ny = num(), num()
            if rel:
                nx, ny = nx + x, ny + y
            if cmd in ("M", "m"):
                close()
                cur = [(nx, ny)]
                subx, suby = nx, ny
                ctrl, curve = None, None
            else:
                cur.append((nx, ny))
                ctrl, curve = None, None
            x, y = nx, ny
        elif cmd in ("H", "h"):
            x = x + num() if rel else num()
            cur.append((x, y))
        elif cmd in ("V", "v"):
            y = y + num() if rel else num()
            cur.append((x, y))
        elif cmd in ("C", "c"):
            pts = [num() for _ in range(6)]
            if rel:
                pts = [pts[i] + (x if i % 2 == 0 else y) for i in range(6)]
            p1, p2 = (pts[0], pts[1]), (pts[2], pts[3])
            p3 = (pts[4], pts[5])
            cur.extend(_cubic_pts((x, y), p1, p2, p3, 16))
            ctrl, curve, (x, y) = p2, "C", p3
        elif cmd in ("S", "s"):
            pts = [num() for _ in range(4)]
            if rel:
                pts = [pts[i] + (x if i % 2 == 0 else y) for i in range(4)]
            p2, p3 = (pts[0], pts[1]), (pts[2], pts[3])
            p1 = ((2 * x - ctrl[0], 2 * y - ctrl[1])
                  if (curve == "C" and ctrl) else (x, y))
            cur.extend(_cubic_pts((x, y), p1, p2, p3, 16))
            ctrl, curve, (x, y) = p2, "C", p3
        elif cmd in ("Q", "q"):
            pts = [num() for _ in range(4)]
            if rel:
                pts = [pts[i] + (x if i % 2 == 0 else y) for i in range(4)]
            qc, qe = (pts[0], pts[1]), (pts[2], pts[3])
            # elevate the quadratic to cubic control points
            c1 = (x + 2.0 / 3.0 * (qc[0] - x), y + 2.0 / 3.0 * (qc[1] - y))
            c2 = (qe[0] + 2.0 / 3.0 * (qc[0] - qe[0]),
                  qe[1] + 2.0 / 3.0 * (qc[1] - qe[1]))
            cur.extend(_cubic_pts((x, y), c1, c2, qe, 16))
            ctrl, curve, (x, y) = qc, "Q", qe
        elif cmd in ("T", "t"):
            nx, ny = num(), num()
            if rel:
                nx, ny = nx + x, ny + y
            qc = ((2 * x - ctrl[0], 2 * y - ctrl[1])
                  if (curve == "Q" and ctrl) else (x, y))
            c1 = (x + 2.0 / 3.0 * (qc[0] - x), y + 2.0 / 3.0 * (qc[1] - y))
            c2 = (nx + 2.0 / 3.0 * (qc[0] - nx), ny + 2.0 / 3.0 * (qc[1] - ny))
            cur.extend(_cubic_pts((x, y), c1, c2, (nx, ny), 16))
            ctrl, curve, (x, y) = qc, "Q", (nx, ny)
        elif cmd in ("A", "a"):
            rx, ry, phi_deg = num(), num(), num()
            laf = int(bool(num()))
            sf = int(bool(num()))
            nx, ny = num(), num()
            if rel:
                nx, ny = nx + x, ny + y
            cur.extend(_arc_to_cubics((x, y), rx, ry, math.radians(phi_deg),
                                      laf, sf, (nx, ny)))
            ctrl, curve, (x, y) = None, None, (nx, ny)
        else:
            pos += 1  # T is the only one missing above… guard: skip token
    cur.append((subx, suby))
    close()
    _PATH_POLY_CACHE[d] = polys
    return polys


def _mask_path(canvas: Image.Image, pt, d: str) -> Image.Image | None:
    """Even-odd parity mask of one <path> on the sprite (the bundle's
    fillRule): subpath masks XOR, so nested contours cut holes.
    (ImageDraw.polygon fills per-polygon; separate calls could not punch.)
    `pt` maps symbol units to sprite px."""
    if not d.strip():
        return None
    mask: Image.Image | None = None
    for poly in _svg_path_polygons(d):
        sub = Image.new("L", canvas.size, 0)
        ImageDraw.Draw(sub).polygon([pt(*v) for v in poly], fill=255)
        mask = sub if mask is None else ImageChops.difference(mask, sub)
    return mask


def _fill_path(canvas: Image.Image, pt, d: str, color) -> None:
    """Opaque fill of one <path>: straight masked paste."""
    mask = _mask_path(canvas, pt, d)
    if mask is not None:
        canvas.paste(color, (0, 0), mask)


def _fill_layer_alpha(canvas: Image.Image, pt, d: str,
                      rgb, alpha: int) -> None:
    """Source-over fill of one <path> at a fixed layer opacity — lottie's
    layer "o": the colour blends with what the layers below already painted,
    it does not replace them (canvas.paste would punch a translucent hole)."""
    mask = _mask_path(canvas, pt, d)
    if mask is None or alpha <= 0:
        return
    layer = Image.new("RGBA", canvas.size, rgb + (255,))
    layer.putalpha(mask if alpha >= 255
                   else ImageChops.multiply(mask, Image.new("L", canvas.size,
                                                            alpha)))
    canvas.alpha_composite(layer)


def _fill_ellipse_layer(canvas: Image.Image, cx: float, cy: float,
                        rx: float, ry: float, rgb, alpha: int) -> None:
    """Source-over ellipse (centre/radii already in sprite px) at a fixed
    layer opacity — the lottie ground ellipses pulse scale + opacity; the
    frame-0 port only needs the static form."""
    if alpha <= 0:
        return
    m = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(m).ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255)
    layer = Image.new("RGBA", canvas.size, rgb + (255,))
    layer.putalpha(m if alpha >= 255
                   else ImageChops.multiply(m, Image.new("L", canvas.size,
                                                         alpha)))
    canvas.alpha_composite(layer)


def _fill_layer_alpha(canvas: Image.Image, pt, d: str,
                     rgb, alpha: int) -> None:
    """Source-over fill of one <path> at a fixed layer opacity — lottie's
    layer "o": the colour blends with what the layers below already painted,
    it does not replace them (canvas.paste would punch a translucent hole)."""
    mask = _mask_path(canvas, pt, d)
    if mask is None:
        return
    layer = Image.new("RGBA", canvas.size, rgb + (255,))
    layer.putalpha(mask if alpha >= 255
                   else ImageChops.multiply(mask, Image.new("L", canvas.size,
                                                             alpha)))
    canvas.alpha_composite(layer)


_GRAD_RAMP_CACHE: dict[tuple, Image.Image] = {}
_GRAD_RAMP_W = 1024  # px; centre 511.5, one stop-range (255 px) per unit t


def _fill_path_gradient(canvas: Image.Image, pt, d: str,
                        s: tuple[float, float], e: tuple[float, float],
                        stops: tuple[tuple[float, tuple[int, int, int]],
                                     ...]) -> None:
    """Linear-gradient fill of one <path>: the three-stop band (lottie
    GradientFill) is baked into a wide ramp whose middle sits at t=0 so the
    segment s->e (t 0..1) is flanked by two full extra unit-ranges on each
    side — projections outside the segment clamp to the end colours instead
    of sampling black. The ramp is affine-mapped so ramp x runs along sprite
    s->e, then masked onto the sprite."""
    mask = _mask_path(canvas, pt, d)
    if mask is None:
        return
    ramp = _GRAD_RAMP_CACHE.get(stops)
    if ramp is None:
        pos = [st[0] for st in stops]
        cols = [st[1] for st in stops]

        def at(t: float) -> tuple[int, int, int]:
            if t <= pos[0]:
                return cols[0]
            if t >= pos[-1]:
                return cols[-1]
            i = max(j for j in range(len(pos) - 1) if pos[j] <= t)
            f = (t - pos[i]) / (pos[i + 1] - pos[i])
            return tuple(int(round(cols[i][c] + f * (cols[i + 1][c]
                                                      - cols[i][c])))
                         for c in range(3))  # type: ignore[return-value]

        mid = (_GRAD_RAMP_W - 1) / 2.0
        ramp = Image.new("RGB", (_GRAD_RAMP_W, 1))
        ramp.putdata([at((x - mid) / 255.0) for x in range(_GRAD_RAMP_W)])
        _GRAD_RAMP_CACHE[stops] = ramp
    p0, p1 = pt(*s), pt(*e)
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    ln2 = (dx * dx + dy * dy) or 1.0
    a, b = 255.0 * dx / ln2, 255.0 * dy / ln2
    mid = (_GRAD_RAMP_W - 1) / 2.0
    grad = ramp.transform(canvas.size, Image.AFFINE,
                          (a, b, mid - (p0[0] * a + p0[1] * b), 0.0, 0.0, 0.0),
                          resample=Image.BILINEAR)
    layer = grad.convert("RGBA")
    layer.putalpha(mask)
    canvas.alpha_composite(layer)


# The app overlays these glyphs as vectors, crisp at any zoom; PIL draws
# without antialiasing, so at ~14 px they turn to mush. Mirror the app's
# structure instead: paint each glyph on a _SS-times supersampled sprite,
# shrink it back with LANCZOS *in premultiplied alpha* (transparent pixels
# carry black RGB — resampling them straight leaves dark fringes), then
# alpha-composite onto the finished raster, exactly like a high-DPI SVG.
_SS = 6


def _op(args, name):
    """ImageMath lambda_eval context access (dict on Pillow >= 10, attr on 9)."""
    return args[name] if isinstance(args, dict) else getattr(args, name)


def _unpremultiply(pm: Image.Image, alpha: Image.Image) -> Image.Image:
    """Reverse ImageChops.multiply(pm, alpha): L -> pm * 255 / max(alpha, 1)."""

    def expr(args):
        convert, maximum = _op(args, "convert"), _op(args, "max")
        return convert(_op(args, "a") * 255 / maximum(_op(args, "al"), 1), "L")

    return ImageMath.lambda_eval(expr, a=pm, al=alpha)


def _pma_down(im: Image.Image, size: tuple[int, int],
              resample=Image.LANCZOS) -> Image.Image:
    """Shrink an RGBA sprite through premultiplied alpha.

    LANCZOS for glyph sprites (photographic-ish shapes benefit from its
    sharpness); the trace passes BOX — an exact area average, which is the
    textbook downscale for supersampling AND has no negative lobes, so the
    room colour cannot peek as a dip/ring around a stroke edge.
    """
    r, g, b, a = im.split()
    chans = [ImageChops.multiply(c, a).resize(size, resample)
             for c in (r, g, b)]
    a = a.resize(size, resample)
    return Image.merge("RGBA", [_unpremultiply(c, a) for c in chans] + [a])


def _aa_paste(img: Image.Image, cx: float, cy: float, w: int, h: int,
              ax: float, ay: float, k: float, paint) -> None:
    """Run paint(draw, ox, oy, k, canvas) on a w-by-h px sprite rendered at
    _SS, anti-alias it down and composite so sprite point (ax, ay) lands on
    (cx, cy) of img. The raw canvas comes along for mask-paste fills (the
    room-tip glyphs) that need the Image, not just the pen."""
    canvas = Image.new("RGBA", (w * _SS, h * _SS), (0, 0, 0, 0))
    paint(ImageDraw.Draw(canvas), ax * _SS, ay * _SS, k * _SS, canvas)
    sprite = _pma_down(canvas, (w, h))
    ox, oy = int(round(cx - ax)), int(round(cy - ay))
    # Image.alpha_composite clamps negative offsets (a glyph near an edge
    # would shift instead of clip), so pre-apply the overflow by cropping.
    sx, sy = max(-ox, 0), max(-oy, 0)
    right, bottom = min(w, img.width - ox), min(h, img.height - oy)
    if right > sx and bottom > sy:
        img.alpha_composite(sprite.crop((sx, sy, right, bottom)), (ox + sx, oy + sy))


# base_charge_svg bounding box (6, 1)-(30, 34) symbol units -> half-extents
# 13 x 22 around the anchor (17, 23); bbox centre (18, 17.5) sits (-1, +5.5)
# units away from it, which offsets the sprite anchor from its own centre.
def _dock_geom(k: float) -> tuple[int, int, float, float]:
    w = math.ceil(2 * 13 * k) + 2
    h = math.ceil(2 * 22 * k) + 2
    return w, h, w / 2.0 - 1.0 * k, h / 2.0 + 5.5 * k


def _draw_dock(img: Image.Image, charge_pos: dict, data,
               variant: str = "normal") -> None:
    if not charge_pos:
        return
    k = _icon_k(data.get("scale", SCALE))
    cx, cy = _screen_to_pixel(float(charge_pos["x"]), float(charge_pos["y"]), data)
    disc_color, motif_paths, motif_dots = _DOCK_VARIANTS[variant]

    def paint(draw: ImageDraw.ImageDraw, ox: float, oy: float, k: float,
              canvas: Image.Image) -> None:
        # (ox, oy) is the anchor: symbol point (17, 23) lands there, so
        # symbol coords must be taken relative to it.
        def pt(sx: float, sy: float) -> tuple[float, float]:
            return ox + (sx - 17.0) * k, oy + (sy - 23.0) * k

        def circle(sx: float, sy: float, r: float, color) -> None:
            x, y = pt(sx, sy)
            rr = r * k
            draw.ellipse([x - rr, y - rr, x + rr, y + rr], fill=color)

        for sx, sy, r, color in _DOCK_HALO_CIRCLES:
            circle(sx, sy, r, color)
        _fill_path(canvas, pt, _DOCK_HALO_TENT_D, _COLOR_WHITE)
        circle(*_DOCK_DISC[:3], disc_color)
        for d in motif_paths:
            _fill_path(canvas, pt, d, _COLOR_WHITE)
        for dot in motif_dots:
            circle(*dot, _COLOR_WHITE)

    # StationIcon <use x=-17 y=-23>: symbol point (17, 23) lands on the
    # charge position. rotate(0, ...) is hardcoded -> no heading.
    dw, dh, dax, day = _dock_geom(k)
    _aa_paste(img, cx, cy, dw, dh, dax, day, k, paint)


def _draw_robot(img: Image.Image, pos: dict, data) -> None:
    if not pos:
        return
    k = _icon_k(data.get("scale", SCALE))
    cx, cy = _screen_to_pixel(float(pos["x"]), float(pos["y"]), data)
    # Plugin display heading, verbatim from main.bundle:
    # a: -(pos.a)/100/Math.PI*180 - 90 (a in 0.01 rad, y-down frame).
    deg = -float(pos.get("a") or 0) / 100.0 * 180.0 / math.pi - 90.0
    rad = math.radians(deg)
    cos_t, sin_t = math.cos(rad), math.sin(rad)

    def paint(draw: ImageDraw.ImageDraw, ox: float, oy: float, k: float,
              _canvas: Image.Image) -> None:
        def disc(lx: float, ly: float, r: float, color) -> None:
            x = ox + (lx * cos_t - ly * sin_t) * k
            y = oy + (lx * sin_t + ly * cos_t) * k
            rr = r * k
            draw.ellipse([x - rr, y - rr, x + rr, y + rr], fill=color)

        # robot_svg stack: dark r=9 -> light r=8.048 -> dark r=7, then the
        # heading dot on the +Y lobe (local (0.002, 2.928)), mint-rimmed.
        disc(0, 0, 9.0, _COLOR_ROBOT_BODY)
        disc(0, 0, 8.048, _COLOR_ROBOT_FACE)
        disc(0, 0, 7.0, _COLOR_ROBOT_BODY)
        disc(0.002, 2.928, 2.196, _COLOR_ROBOT_RING)
        disc(0.002, 2.928, 1.996, _COLOR_WHITE)

    side = math.ceil(2 * 9 * k) + 2  # robot_svg is a 20x20-unit symbol
    _aa_paste(img, cx, cy, side, side, side / 2.0, side / 2.0, k, paint)


# Relocation pin: the official app shows a Lottie (assets/lottie/relocation,
# 66x66 vector comp) while status_code == 17 — teal gradient teardrop with a
# white eye, a mint halo, a blue rear sliver and three pulsing ground
# ellipses. The animation is only a bounce + ground pulse, so the static
# port bakes frame 0: the lottie's shapes, transforms (comp null rotate
# 176.729deg + y-flip folded in), gradients and opacities, verbatim, drawn
# through the same vector pipeline as the dock/robot glyphs — no pasted PNG.
# Constants come from _i18n_work/convert_lottie_pin.py (paths baked to
# comp-0 units). _PIN_UP is the old hand-drawn pin's "badge, not robot"
# enlargement; _PIN_FIT then scales the lottie's own drop (crest top 5.51 to
# tip 46.850 = 41.34 units) so the badge keeps the ~30-unit drop height the
# old pin had at that same enlargement — same footprint as always, only the
# shape becomes the official one.
_PIN_UP = 1.6
_PIN_FIT = 30.0 / 41.34
_PIN_BOX = (17.7, 5.0, 51.0, 62.8)      # lottie content bbox in comp units
_PIN_TIP = (33.246, 46.850)             # front-drop tip (ground contact)
_PIN_GROUND_COLOR = (0x7C, 0xBF, 0xC6)
# back-to-front, as lottie paints them (ind5, ind4, ind3):
_PIN_GROUND = ((34.444, 54.758, 16.197, 7.246, 26),   # Ellipse 149, op 10 %
               (34.189, 54.331, 11.082, 5.115, 64),   # Ellipse 148, op 25 %
               (33.940, 54.198, 6.067, 2.696, 255))   # Ellipse 147, op 100 %
# back-to-front (comp ind6, ind5, ind4, ind3, ind2):
_PIN_REAR_D = ("M 44.688 26.563 C 44.555 34.223, 40.187 40.172, 36.878 43.606"
               " C 34.065 40.491, 24.046 28.704, 23.398 17.352"
               " C 23.193 13.778, 24.285 10.791, 26.273 8.784"
               " C 27.776 8.428, 29.428 8.424, 31.213 8.803"
               " C 38.727 10.426, 44.820 18.702, 44.688 26.563"
               " C 44.688 26.563, 44.688 26.563, 44.688 26.563 Z")
_PIN_REAR_COLOR = (0x78, 0xA5, 0xFF)
_PIN_FRONT_D = ("M 44.688 26.563 C 44.555 34.223, 40.187 40.172, 36.878 43.606"
                " C 35.015 45.535, 33.496 46.654, 33.266 46.827"
                " C 33.266 46.827, 33.227 46.850, 33.227 46.850"
                " C 33.227 46.850, 19.766 33.025, 19.011 19.807"
                " C 18.731 14.915, 20.897 11.105, 24.398 9.442"
                " C 24.993 9.158, 25.611 8.942, 26.283 8.784"
                " C 27.786 8.428, 29.438 8.423, 31.222 8.802"
                " C 38.727 10.426, 44.820 18.702, 44.688 26.563"
                " C 44.688 26.563, 44.688 26.563, 44.688 26.563 Z")
_PIN_FRONT_GRAD = ((36.867, 8.029), (28.078, 47.148),
                   ((0.0, (0x5F, 0xD2, 0xD1)),
                    (0.5, (0x3E, 0xC0, 0xC7)),
                    (1.0, (0x1E, 0xAE, 0xBD))))
_PIN_HALO_D = ("M 27.559 25.458 C 26.028 22.501, 26.716 19.296, 29.093 18.309"
               " C 31.481 17.321, 34.658 18.932, 36.190 21.900"
               " C 37.731 24.857, 37.043 28.061, 34.665 29.049"
               " C 32.278 30.036, 29.101 28.425, 27.559 25.458"
               " C 27.559 25.458, 27.559 25.458, 27.559 25.458 Z")
_PIN_HALO_COLOR = (0x72, 0xE2, 0xE6)
_PIN_HALO_ALPHA = 153                       # layer opacity 60 %
_PIN_EYE_D = ("M 28.864 25.283 C 27.405 22.362, 28.046 19.200, 30.305 18.240"
              " C 32.553 17.270, 35.559 18.861, 37.018 21.792"
              " C 38.477 24.714, 37.836 27.866, 35.588 28.836"
              " C 33.339 29.795, 30.333 28.204, 28.864 25.283"
              " C 28.864 25.283, 28.864 25.283, 28.864 25.283 Z")
_PIN_EYE_COLOR = (0xF4, 0xFA, 0xFB)
_PIN_CREST_D = ("M 49.082 24.108 C 48.886 36.459, 37.620 44.385, 37.620 44.385"
                " C 37.620 44.385, 33.262 46.818, 33.262 46.818"
                " C 33.492 46.644, 35.021 45.525, 36.874 43.596"
                " C 40.194 40.171, 44.561 34.212, 44.685 26.563"
                " C 44.816 18.692, 38.734 10.426, 31.199 8.793"
                " C 29.434 8.403, 27.772 8.418, 26.260 8.775"
                " C 25.608 8.933, 24.979 9.139, 24.384 9.433"
                " C 24.384 9.433, 27.984 7.394, 27.984 7.394"
                " C 27.984 7.394, 28.158 7.294, 28.158 7.294"
                " C 30.205 6.116, 32.756 5.709, 35.585 6.329"
                " C 43.122 7.982, 49.213 16.238, 49.082 24.108"
                " C 49.082 24.108, 49.082 24.108, 49.082 24.108 Z")
_PIN_CREST_GRAD = ((41.643, 5.532), (33.600, 46.802),
                   ((0.0, (0x4E, 0xB7, 0xB6)),
                    (0.5, (0x36, 0xB2, 0xB9)),
                    (1.0, (0x1E, 0xAE, 0xBD))))


def _draw_pin(img: Image.Image, data) -> None:
    """Official-app relocation state: the robot marker is hidden and the
    app's lottie pin (frame 0) sits at the centre of the map — position not
    yet known, see the _PIN_* block for the source."""
    x0, y0, x1, y1 = _PIN_BOX
    k = _icon_k(data.get("scale", SCALE)) * _PIN_UP * _PIN_FIT
    w, h = math.ceil((x1 - x0) * k) + 2, math.ceil((y1 - y0) * k) + 2
    # Sprite point (ax, ay) = the lottie tip; the sprite is _aa_pasted so it
    # lands on the image centre, the content bbox centred in the sprite.
    ax = (_PIN_TIP[0] - x0) * k + (w - (x1 - x0) * k) / 2.0
    ay = (_PIN_TIP[1] - y0) * k + (h - (y1 - y0) * k) / 2.0

    def paint(draw: ImageDraw.ImageDraw, ox: float, oy: float, kk: float,
              canvas: Image.Image) -> None:
        # (ox, oy) is the lottie tip; the box origin sits tip-u px back.
        def pt(sx: float, sy: float) -> tuple[float, float]:
            return (ox + (sx - _PIN_TIP[0]) * kk,
                    oy + (sy - _PIN_TIP[1]) * kk)

        for cx, cy, rx, ry, alpha in _PIN_GROUND:
            gx, gy = pt(cx, cy)
            _fill_ellipse_layer(canvas, gx, gy, rx * kk, ry * kk,
                                _PIN_GROUND_COLOR, alpha)
        _fill_path(canvas, pt, _PIN_REAR_D, _PIN_REAR_COLOR)
        s, e, stops = _PIN_FRONT_GRAD
        _fill_path_gradient(canvas, pt, _PIN_FRONT_D, s, e, stops)
        _fill_layer_alpha(canvas, pt, _PIN_HALO_D, _PIN_HALO_COLOR,
                          _PIN_HALO_ALPHA)
        _fill_path(canvas, pt, _PIN_EYE_D, _PIN_EYE_COLOR)
        s, e, stops = _PIN_CREST_GRAD
        _fill_path_gradient(canvas, pt, _PIN_CREST_D, s, e, stops)

    _aa_paste(img, img.width / 2.0, img.height / 2.0, w, h, ax, ay, k, paint)


# The plugin's cleanMode getter maps RobotStatus (17.9) onto the cleaning
# highlight modes: only Smart and Room tasks ever highlight selected rooms.
# Anything unmapped (None/Idle, codes outside a task) reads Idle.
# only Smart and Room ever badge, and MapView blanks cleaningModeParameters
# in Zoning. Anything unmapped (None/Idle, codes outside a task) reads Idle.
_CLEAN_MODE_BY_ROBOT_STATUS = {
    2: "smart", 3: "smart", 4: "smart",              # Full clean start/pause/resume
    5: "building", 6: "building", 7: "building",     # QMAP (build map)
    8: "room", 9: "room", 10: "room",                # Area clean
    11: "zoning", 12: "zoning", 13: "zoning",        # Zone clean
    14: "building", 15: "building", 16: "building",  # Carpet clean (into Building)
    17: "spot", 18: "spot",                          # Spot clean
}


def _as_int_list(raw) -> list | None:
    """clean-values (17/34): JSON string or list of ints, None if unparseable."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, list) or not all(isinstance(v, int) for v in raw):
        return None
    return list(raw)


def _looks_like_zones(nums: list) -> bool:
    """Zone clean-values are flat TL,TR,BR,BL runs, 8 per zone: the corners
    repeat (a==g, b==d, c==e, f==h). A room-id list never matches — this is
    the plan-B shape test when RobotStatus (17/9) is not readable locally."""
    if not nums or len(nums) % 8:
        return False
    for i in range(0, len(nums), 8):
        a, b, c, d, e, f, g, h = nums[i:i + 8]
        if not (a == g and b == d and c == e and f == h):
            return False
    return True


def _selected_rooms(raw) -> list:
    """The Room-mode selectedAreas id list as 17/34 carries it (tap order),
    [] for zone-shaped, empty or unparseable values."""
    nums = _as_int_list(raw)
    if not nums or _looks_like_zones(nums):
        return []
    return nums


def _clean_view(live: dict | None) -> dict | None:
    """Normalize the coordinator's raw reads (robot_status 17/9, clean_values
    17/34) into the live-task state: the plugin's mode + the Room-mode
    selection the highlight palette follows. None (no live) keeps the
    prefer-only render for offline callers."""
    if live is None:
        return None
    rs = live.get("robot_status")
    if isinstance(rs, int):
        mode = _CLEAN_MODE_BY_ROBOT_STATUS.get(rs, "idle")
    else:
        # Plan B: 17/9 unreadable — infer room/zoning from the 17/34 shape.
        nums = _as_int_list(live.get("clean_values"))
        mode = ("zoning" if nums and _looks_like_zones(nums)
                else "room" if nums else "idle")
    selected = _selected_rooms(live.get("clean_values")) if mode == "room" else []
    return {"mode": mode, "selected": selected}


def render_xtl_map(raw: bytes, scale: int | None = None, *,
                   live: dict | None = None) -> dict | None:
    """Decode + render one xtl KS3 map file into the MapResult contract.

    `scale` overrides the automatic px-per-cell choice (see render_scale);
    tests pin it to the nominal SCALE so pixel expectations stay small.

    `live` carries the robot's current cleaning state as raw property reads
    (see _clean_view: robot_status/clean_values, plus status_code for the
    relocation pin) so rooms selected in a live room-clean task take the
    highlight palette, like the official app. None (no state) keeps the
    prefer-only render.

    Returns the MapResult kwargs (image_png/attributes/vector/map_id/
    content_hash), or None when the file carries no readable map.
    """
    data = decode_map_file(raw)
    if data is None:
        return None
    w, h = data["width"], data["height"]
    data["scale"] = scale or render_scale(w, h)
    view = _clean_view(live)
    colors = _room_colors(data["areas"], view["selected"] if view else None)
    palette = {0: _COLOR_UNKNOWN, 1: _COLOR_WALL, 2: _COLOR_FLOOR}
    raster = data["raster"]

    base = Image.new("RGBA", (w, h))
    base_px = base.load()
    for row in range(h):
        src = (h - 1 - row) * w  # raster row 0 = worldX xMin = drawn bottom
        for col in range(w):
            v = raster[src + col]
            if v <= 2:
                base_px[col, row] = palette[v]
            else:
                # Raster ids >= 3 are rooms; one with no areas entry has no
                # palette key and the native renderer draws nothing.
                col_val = colors.get(v)
                if col_val is not None:
                    base_px[col, row] = col_val
    img = base.resize((w * data["scale"], h * data["scale"]), Image.NEAREST)

    # Overlays go on their own layer so zone fills alpha-composite over the
    # raster (matching the app's SVG stacking) instead of punching holes.
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    # MopVirtualView turns every entry into a VirtualArea rect, lines included.
    _draw_walls(draw, data["mop_walls"], data, _COLOR_MOP, lines_as_zones=True)
    _draw_walls(draw, data["carpet"], data, _COLOR_CARPET)
    _draw_walls(draw, data["walls"], data, _COLOR_SWEEP)
    img = Image.alpha_composite(img, layer)
    # The trace keeps its own layer so its (transparent-today) halo alpha,
    # when re-enabled, blends over the walls the way the browser composites
    # it — cores on top, always. Room names and icons are NOT baked
    # in any more (the old AreaTipView port was): the XVMC card draws its own
    # labels over `rooms`/`predefined_selections`, in screen space where they
    # stay crisp and correctly placed under pan/zoom.
    img = Image.alpha_composite(img, _antialiased_trace_layer(
        data["trace"], data, img.size))
    # Glyphs land on top of the finished raster (plugin SVG stacking), as
    # supersampled sprites so they stay crisp where the map itself cannot.
    station = _station_variant(live)
    _draw_dock(img, data["charge_pos"], data, station)
    # Relocation (siid 2 / piid 2 code 17): hide the robot marker entirely
    # and centre a big pin instead — the robot is out but doesn't know where
    # it is, exactly like the official app's positioning badge.
    relocating = (live is not None
                  and live.get("status_code") == XTL_STATUS_RELOCATION)
    pos = data["pos"]
    # App parity (isInBaseStation branch): while parked, the robot is drawn
    # at chargePos + 5 grid units, not at its raw pose, so robot and dock
    # glyphs stay both visible. Two parked signals reach this branch: a real
    # pose sitting on the dock (heuristic), or the zero pose a docked upload
    # carries, already reduced to "no pose" by _robot_pose — in that case
    # the dock itself is the best available position, and the next upload
    # off-dock restores the true pose.
    charge = data["charge_pos"]
    # NOT an if/elif chain on truthiness: `elif charge` used to catch every
    # valid away-from-dock pose too (the if only covers dist<10), pinning the
    # robot to the dock while its pose was fresh. The charge fallback is for
    # a MISSING pose only.
    if relocating:
        _draw_pin(img, data)
    else:
        if charge:
            if not pos:
                pos = {"x": charge["x"], "y": float(charge["y"]) + 5,
                       "a": charge.get("a", 0)}
            elif math.hypot(float(pos["x"]) - float(charge["x"]),
                            float(pos["y"]) - float(charge["y"])) < 10:
                pos = {**pos, "x": charge["x"], "y": float(charge["y"]) + 5}
        _draw_robot(img, pos, data)

    buf = io.BytesIO()
    img.save(buf, format="PNG")

    rooms_geo = _room_geometry(raster, w, h,
                               {a["room_id"] for a in data["areas"]}, data)
    attributes = {
        "image_width": img.width,
        "image_height": img.height,
        "width": w,
        "height": h,
        "x_min": data["x_min"],
        "y_max": data["y_max"],
        "scale": data["scale"],
        "resolution_cm": data["resolution_cm"],
        # XVMC rooms contract: a record keyed by room id (the card iterates
        # `for (const room_id in rooms)` and reads outline/x0..y1/name/icon/
        # x/y). outline+bbox are traced from the raster bytes (true polygons,
        # where Dreame's extractor only offers bboxes); id/room_id/name/type/
        # center/neibs/prefer keep serving our own dashboards.
        "rooms": {
            str(a["room_id"]): _room_attribute(a, rooms_geo.get(a["room_id"]))
            for a in data["areas"]
        },
        "map_id": data["map_id"],
        "updated_at": int(time.time() * 1000),
        # XVMC `calibration_source: {camera: true}` reads exactly this attr.
        "calibration_points": _calibration_points(data, img.width, img.height),
        "charger": data["charge_pos"] or None,
        "vacuum_position": data["pos"] or None,
        "walls": [w_ for w_ in data["walls"] if w_["type"] == 1],
        "no_go_areas": [w_ for w_ in data["walls"] if w_["type"] != 1],
        "no_mopping_areas": data["mop_walls"],
        "carpet_zones": data["carpet"],
        "door_sills": data["thres"],
        "relocating": relocating,
        # Which base_*_svg the dock glyph took (diagnostic; also folded into
        # content_hash while not resting, so a status flip alone repaints).
        "station_icon": station,
    }
    # Full card contract for the /maps endpoint (www/xiaomi-vac-card.js):
    # rooms list + metre bounds + traced chains + overlays. Every input is
    # blob-derived except the relocating/station flags, which the digest
    # already folds below — so the hash law is untouched.
    vector = _card_vector(data, attributes, pos, data["charge_pos"] or None,
                          relocating)
    # MapCache.async_upsert no-ops when content_hash matches the previous
    # entry, so a pin that appeared over an *unchanged* blob would never get
    # served. Fold the relocating flag into the digest (only while set, so
    # ordinary renders keep hashing exactly sha256(raw)). Same for the
    # station glyph: the resting icon is what every plain render already
    # draws, so only an activity variant (wash/dry/dust/charge) changes it.
    digest = hashlib.sha256(raw)
    if relocating:
        digest.update(b"\x00reloc")
    if station != "normal":
        digest.update(b"\x00sta:" + station.encode())
    return {
        "image_png": buf.getvalue(),
        "attributes": attributes,
        "vector": vector,
        "map_id": data["map_id"] if isinstance(data["map_id"], int) else None,
        "content_hash": digest.hexdigest(),
    }
