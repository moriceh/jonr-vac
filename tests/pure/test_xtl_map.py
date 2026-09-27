"""Pure tests for the xtl (JONR P20 Pro) map pipeline (xtl_map.py).

Fixtures are synthetic KS3 files: the 9-field JSON envelope the get-map-data /
map-data-report flow points at, with LZ4-compressed raster/trace payloads.
Rasters are built literal-only with the encoder below (valid LZ4 blocks for
the decoder under test); the codec itself also gets hand-assembled match
blocks. Standalone import per conftest — xtl_map has no HA or relative
imports.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import struct

import pytest
from PIL import Image, ImageDraw

import xtl_map
from xvac.map import MapFetcher, SessionExpired


# --- fixture helpers --------------------------------------------------------

def _lz4_compress(data: bytes) -> bytes:
    """Literal-only LZ4 block: token + 255-extension + verbatim literals."""
    n = len(data)
    out = bytearray([min(n, 15) << 4])
    if n >= 15:
        rest = n - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    out += data
    return bytes(out)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _b64s(text: str) -> str:
    return _b64(text.encode())


def _trace_field(records) -> str:
    blob = b"".join(struct.pack("<hhHB", x, y, ang, attr)
                    for x, y, ang, attr in records)
    return _b64s(json.dumps({"trace": _b64(_lz4_compress(blob)), "lz4Len": len(blob)}))


def _build_file(raster, *, width, height, x_min=0, y_max=0, trace=None,
                pos=None, areas=None, walls=None, mop_walls=None, carpet=None,
                charge_pos=None, map_id=7, lz4_len=None) -> bytes:
    map_data = {
        "map": _b64(_lz4_compress(raster)),
        "lz4Len": lz4_len if lz4_len is not None else len(raster),
        "width": width, "height": height, "xMin": x_min, "yMax": y_max,
        "resolution": 5,
    }
    if charge_pos is not None:
        map_data["chargePos"] = charge_pos
    fields = [
        _b64s(json.dumps(map_data)),
        trace if trace is not None else "",
        json.dumps(pos) if pos is not None else "",
        json.dumps(areas) if areas is not None else "[]",
        _b64s(walls) if walls else "",
        _b64s(mop_walls) if mop_walls else "",
        _b64s(carpet) if carpet else "",
        "",
        "[]",
    ]
    return json.dumps({"mapId": map_id, "fields": fields}).encode()


def _area(id_, name="", neibs="", room_id=None, prefer=None, cx=10, cy=790) -> dict:
    a = {"id": id_, "name": name, "neibs": neibs, "centerX": cx, "centerY": cy}
    if room_id is not None:
        a["room_id"] = room_id
    if prefer is not None:
        a["prefer"] = prefer
    return a


# --- LZ4 raw block codec ----------------------------------------------------

def test_literal_only_roundtrip_with_length_extension():
    data = bytes(range(256)) * 2  # 512 > 15+255: exercises the 255-run loop
    assert xtl_map.lz4_block_decompress(_lz4_compress(data), len(data)) == data


def test_match_block_copies_back():
    # token 0x40: 4 literals "ABCD", then a MINMATCH-4 match at offset 4.
    block = bytes([0x40]) + b"ABCD" + struct.pack("<H", 4)
    assert xtl_map.lz4_block_decompress(block, 8) == b"ABCDABCD"


def test_overlapping_match_feeds_on_itself():
    # offset 1 + extra 8 -> 12 copied bytes, each read from the previous one.
    block = bytes([0x48]) + b"AAAA" + struct.pack("<H", 1)
    assert xtl_map.lz4_block_decompress(block, 16) == b"A" * 16


def test_truncated_length_extension_raises():
    with pytest.raises(ValueError):
        xtl_map.lz4_block_decompress(bytes([0xF0, 0xFF]), 100)


def test_out_of_range_offset_raises():
    block = bytes([0x40]) + b"ABCD" + struct.pack("<H", 99)
    with pytest.raises(ValueError):
        xtl_map.lz4_block_decompress(block, 8)


# --- KS3 file decode ---------------------------------------------------------

def test_decode_reads_all_fields():
    raw = _build_file(
        bytes([1, 2, 3, 3]), width=2, height=2,
        trace=_trace_field([
            (4, 4, 0, 1),        # work mode 1
            (6, 6, 0, 1),
            (8, 8, 0, 0x81),     # isBreak -> closes the previous polyline
            (9, 9, 0, 3),        # work mode >= 3: never rendered, dropped
        ]),
        pos={"x": 10, "y": 10, "a": 157},
        areas=[_area(1, "Salon", room_id=3), _area(2, "Chambre")],
        walls="1,1,10,10,20,10",
        mop_walls="2,2,30,30,40,30,40,40,30,40",
        carpet="3,1,2,15,15,25,15",
        charge_pos={"x": 5, "y": 2, "a": 0},
    )
    data = xtl_map.decode_map_file(raw)
    assert data is not None
    assert data["map_id"] == 7 and data["raster"] == bytes([1, 2, 3, 3])
    assert data["pos"] == {"x": 10, "y": 10, "a": 157}
    assert data["charge_pos"] == {"x": 5, "y": 2, "a": 0}
    # room_id defaults to id + 2 (plugin convention); an explicit one wins.
    assert [(a["id"], a["room_id"]) for a in data["areas"]] == [(1, 3), (2, 4)]
    assert data["walls"] == [{"id": 1, "type": 1, "points": [[10, 10], [20, 10]]}]
    assert data["mop_walls"][0]["type"] == 2
    assert len(data["mop_walls"][0]["points"]) == 4
    # Carpet records carry a preference field at position 2 — not a coordinate.
    assert data["carpet"] == [{"id": 3, "type": 1, "points": [[15, 15], [25, 15]]}]
    # Display-parser port (parseTestMapTraceToSvgPaths): the isBreak record
    # is a flush marker, never drawn, and closes the first polyline (typed by
    # the last work mode before it). Two-point runs pass DP untouched. The
    # work-mode-3 tail is dropped (MapTraceView draws nothing >= 3).
    assert data["trace"] == [
        (1, [(4.0, 4.0), (6.0, 6.0)]),
    ]


def test_decode_charge_pos_string_variant():
    raw = _build_file(bytes([2, 2, 2, 2]), width=2, height=2,
                      charge_pos='{"x": 5, "y": 2, "a": 0}')
    assert xtl_map.decode_map_file(raw)["charge_pos"] == {"x": 5, "y": 2, "a": 0}


def test_decode_drops_the_parked_zero_pose():
    # A docked robot uploads "x":0,"y":0,"a":0,"i":0 verbatim (live capture
    # 2026-09-23). Kept raw, it would be drawn far outside the crop.
    raw = _build_file(bytes([1, 2, 3, 3]), width=2, height=2,
                      pos={"x": 0, "y": 0, "a": 0, "i": 0},
                      charge_pos={"x": 5, "y": 2, "a": 0})
    data = xtl_map.decode_map_file(raw)
    assert data["pos"] == {}
    assert data["charge_pos"] == {"x": 5, "y": 2, "a": 0}
    # One grid unit off the origin is a real pose, kept untouched.
    near = _build_file(bytes([1, 2, 3, 3]), width=2, height=2,
                       pos={"x": 1, "y": 0, "a": 90})
    assert xtl_map.decode_map_file(near)["pos"] == {"x": 1, "y": 0, "a": 90}


def test_decode_rejects_garbage_and_mismatch():
    assert xtl_map.decode_map_file(b"not json") is None
    assert xtl_map.decode_map_file(b'{"fields": []}') is None
    # raster size != width*height ("map generation data error" in the plugin)
    assert xtl_map.decode_map_file(
        _build_file(bytes([1, 2, 3]), width=2, height=2)) is None
    # lz4Len <= 1 is the plugin's hasMapData flag: empty map, not an error
    assert xtl_map.decode_map_file(
        _build_file(b"\x01", width=1, height=1, lz4_len=1)) is None


# --- geometry / palette -------------------------------------------------------

def test_screen_to_pixel_maps_raster_flipped():
    data = {"width": 2, "height": 4, "x_min": 0, "y_max": -10}
    # col = sx - yMax; worldX = 800 - sy = 0 -> raster row 0 = drawn BOTTOM.
    assert xtl_map._screen_to_pixel(0, 800, data) == (21.0, 7.0)
    # worldX 3 -> last raster row -> first PNG row.
    assert xtl_map._screen_to_pixel(-10, 797, data) == (1.0, 1.0)


def test_room_colors_highlight_palette_for_sequence_order():
    areas = [
        {"id": 1, "room_id": 3, "neibs": "", "prefer": {"order": 0}},
        {"id": 2, "room_id": 4, "neibs": "", "prefer": {"order": 2}},
    ]
    colors = xtl_map._room_colors(areas)
    assert colors[3] == xtl_map._ROOM_COLORS[0]       # plain room, index 0
    assert colors[4] == xtl_map._HIGHLIGHT_COLORS[1]  # cleaning-order slot


def test_room_colors_greedy_conflicts_on_index_across_palettes():
    # 5-room chain: greedy colouring over >4 rooms must never reuse an INDEX
    # on neighbours, even when the two neighbours use different palettes.
    areas = []
    for i in range(1, 6):
        neibs = ",".join(str(n) for n in (i - 1, i + 1) if 1 <= n <= 5)
        areas.append({"id": i, "room_id": i + 2, "neibs": neibs,
                      "prefer": {"order": 3 if i == 1 else 0}})
    colors = xtl_map._room_colors(areas)

    def index_of(rid):
        for table in (xtl_map._ROOM_COLORS, xtl_map._HIGHLIGHT_COLORS):
            if colors[rid] in table:
                return table.index(colors[rid])
        raise AssertionError(f"{colors[rid]!r} from neither palette")

    assert colors[3] == xtl_map._HIGHLIGHT_COLORS[0]
    assert [index_of(i + 2) for i in range(1, 6)] == [0, 1, 0, 1, 0]


# --- trace simplification (plugin parseTestMapTraceToSvgPaths port) -----------

def test_simplify_ap_collapses_collinear_and_spaced():
    line = [[float(i), float(i)] for i in range(20)]  # 1-unit steps, sq dist 1
    assert xtl_map._simplify_ap(line, 0.8) == [line[0], line[-1]]
    corner = [[0.0, 0.0], [50.0, 50.0], [100.0, 0.0]]
    assert xtl_map._simplify_ap(corner, 0.8) == corner  # apex deviates by 50


def test_simplify_ap_keeps_points_over_tolerance():
    pts = [[0.0, 0.0], [10.0, 3.0], [20.0, 0.0]]  # 3-unit off-segment bulge
    assert xtl_map._simplify_ap(pts, 0.8) == pts


def test_simplify_ap_unit_sawtooth_needs_the_display_tolerance():
    # Why 0.8 and not the other parser's 1.0: a 1-unit-amplitude zigzag
    # (deviation sq dist 1) is erased at tolerance 1 and survives at 0.8 —
    # the skeleton trace the app never shows.
    saw = [[float(i), float(i % 2)] for i in range(11)]
    assert xtl_map._simplify_ap(saw, 1.0) == [saw[0], saw[-1]]
    kept = xtl_map._simplify_ap(saw, 0.8)
    assert len(kept) > 2  # the zigzag survives, first and last included
    assert kept[0] is saw[0] and kept[-1] is saw[-1]


def test_decode_trace_dp_flattens_straight_run_to_endpoints():
    # 50 points of 1-unit straight steps: the radial pass keeps them all at
    # 0.8, Douglas-Peucker then reduces the collinear run to its endpoints.
    recs = [(100 + i, 400, 0, 1) for i in range(50)]
    data = xtl_map.decode_map_file(_build_file(
        bytes([2] * 4), width=2, height=2, trace=_trace_field(recs)))
    assert data["trace"] == [(1, [(100.0, 400.0), (149.0, 400.0)])]


def test_decode_trace_dp_keeps_curve_interior_points():
    # Parabola run: DP at 0.8 keeps every point that deviates enough from
    # its chord — a curve never degenerates to the chord. And DP returns
    # input points by identity: no interpolation anywhere on this path.
    recs = [(100 + i, 400 + i * i // 20, 0, 1) for i in range(50)]
    data = xtl_map.decode_map_file(_build_file(
        bytes([2] * 4), width=2, height=2, trace=_trace_field(recs)))
    [(mode, pts)] = data["trace"]
    assert mode == 1
    assert pts[0] == (100.0, 400.0)
    assert pts[-1] == (149.0, 520.0)
    assert len(pts) > 2
    raw_pts = {(float(x), float(y)) for x, y, _, _ in recs}
    assert all(p in raw_pts for p in pts)


def test_decode_trace_break_starts_a_new_polyline():
    # parseTest flushes AND clears its buffers at a break, so no polyline
    # ever spans one (the sibling parser buffers did — porting it is what
    # drew zipper lines across rooms). One segment per break, each typed by
    # the last work mode before it.
    recs = [(10, 10, 0, 1), (20, 20, 0, 1), (30, 30, 0, 0x81),
            (40, 40, 0, 1), (50, 50, 0, 1)]
    data = xtl_map.decode_map_file(_build_file(
        bytes([2] * 4), width=2, height=2, trace=_trace_field(recs)))
    assert data["trace"] == [
        (1, [(10.0, 10.0), (20.0, 20.0)]),
        (1, [(40.0, 40.0), (50.0, 50.0)]),
    ]


def test_decode_trace_action_type_one_passes_raw():
    # actionType 1 (attr bits 3-6 set, e.g. attr 8|mode) is the spot-clean
    # "gongzi" run: buffered raw, no DP — even exact duplicates survive.
    recs = [(5, 5, 0, 8 | 2), (5, 5, 0, 8 | 2), (5, 5, 0, 8 | 2),
            (6, 6, 0, 2), (7, 7, 0, 2)]
    data = xtl_map.decode_map_file(_build_file(
        bytes([2] * 4), width=2, height=2, trace=_trace_field(recs)))
    assert data["trace"] == [
        (2, [(5.0, 5.0), (5.0, 5.0), (5.0, 5.0), (6.0, 6.0), (7.0, 7.0)]),
    ]


# --- render scale --------------------------------------------------------------

def test_render_scale_targets_the_plugin_800_unit_scene():
    assert xtl_map.render_scale(113, 105) == 7   # the live P20 Pro map
    assert xtl_map.render_scale(22, 22) == xtl_map._SCALE_MAX
    assert xtl_map.render_scale(0, 0) == xtl_map._SCALE_MAX   # div guard
    assert xtl_map.render_scale(400, 400) == xtl_map._SCALE_MIN
    assert xtl_map.render_scale(800, 600) == xtl_map._SCALE_MIN


def test_render_adaptive_scale_bakes_hd_output():
    # Same fixture as the pixel-exact tests, left on the automatic scale:
    # the 22x22 cell map must come back at scale 8, with proportionally
    # bigger (and equally crisp) glyphs.
    out = xtl_map.render_xtl_map(_build_file(
        bytes([2] * 484), width=22, height=22,
        pos={"x": _X, "y": _Y, "a": 0}))
    img = Image.open(io.BytesIO(out["image_png"]))
    assert img.size == (22 * 8, 22 * 8)
    assert out["attributes"]["scale"] == 8
    assert out["attributes"]["image_width"] == img.width
    cx, cy = xtl_map._screen_to_pixel(_X, _Y, {
        "width": 22, "height": 22, "x_min": 0, "y_max": 0, "scale": 8})
    # Body-disc centroid lands on the pose to well under a cell.
    dx, dy = _centroid(img, _is_dark)
    assert abs(dx - cx) < 1.5 and abs(dy - cy) < 1.5


# --- rendering ----------------------------------------------------------------

def test_render_flips_raster_and_applies_palette():
    raw = _build_file(bytes([1, 2, 3, 3]), width=2, height=2,
                      areas=[_area(1, room_id=3)])
    out = xtl_map.render_xtl_map(raw, scale=xtl_map.SCALE)
    img = Image.open(io.BytesIO(out["image_png"]))
    assert img.size == (2 * xtl_map.SCALE, 2 * xtl_map.SCALE)
    px = img.load()
    # Raster row 0 (worldX = xMin) draws at the PNG bottom: wall + floor.
    assert px[0, 3] == xtl_map._COLOR_WALL
    assert px[2, 2] == xtl_map._COLOR_FLOOR
    # Room byte 3 resolves through room_id 3 -> areas index 0 -> tint #1.
    assert px[0, 0] == xtl_map._ROOM_COLORS[0]
    assert out["map_id"] == 7
    assert out["attributes"]["image_width"] == img.width


def test_render_unmapped_room_byte_stays_transparent():
    raw = _build_file(bytes([5, 5, 5, 5]), width=2, height=2, areas=[])
    out = xtl_map.render_xtl_map(raw, scale=xtl_map.SCALE)
    assert Image.open(io.BytesIO(out["image_png"])).load()[0, 0] == (0, 0, 0, 0)


def test_render_trace_white_over_room():
    raw = _build_file(bytes([3, 3, 3, 3]), width=2, height=2,
                      areas=[_area(1, room_id=3)],
                      trace=_trace_field([(0, 800, 0, 1), (1, 800, 0, 1)]))
    out = xtl_map.render_xtl_map(raw, scale=xtl_map.SCALE)
    px = Image.open(io.BytesIO(out["image_png"])).load()
    base = xtl_map._ROOM_COLORS[0]
    # height=2: scene (0,800)/(1,800) -> cell centres (1,3)/(3,3); midpoint hit.
    lit = px[2, 3]
    assert lit != base
    assert lit[0] > 200  # whitish halo/core blend over the room tint


def test_render_trace_is_antialiased():
    # The trace layer is supersampled + LANCZOS-resampled (like the glyphs),
    # so halo edges carry partial alpha — PIL's raw draw.line only ever emits
    # the binary source alphas (halo 51 / cores 128 and 255).
    raw = _build_file(bytes([2, 2, 2, 2]), width=2, height=2,
                      trace=_trace_field([(0, 800, 0, 2), (1, 800, 0, 2)]))
    data = xtl_map.decode_map_file(raw)
    data["scale"] = xtl_map.SCALE
    layer = xtl_map._antialiased_trace_layer(
        data["trace"], data, (2 * xtl_map.SCALE, 2 * xtl_map.SCALE))
    soft = [a for a in layer.getchannel("A").getdata() if 0 < a < 51]
    assert soft, "trace edges are binary again — AA lost"


def test_render_trace_aa_keeps_geometry():
    # Downscaling must not drift the stroke: the core's pixel footprint stays
    # within 1 px of the 1x direct-draw reference (calibration contract).
    raw = _build_file(bytes([2, 2, 2, 2]), width=2, height=2,
                      trace=_trace_field([(0, 800, 0, 2), (1, 800, 0, 2)]))
    data = xtl_map.decode_map_file(raw)
    k = data["scale"] = xtl_map.SCALE
    size = (2 * k, 2 * k)
    aa = xtl_map._antialiased_trace_layer(data["trace"], data, size)
    ref = Image.new("RGBA", size, (0, 0, 0, 0))
    ref_draw = ImageDraw.Draw(ref)
    for mode, pts in data["trace"]:
        xtl_map._trace_strokes(
            ref_draw, [xtl_map._screen_to_pixel(x, y, data) for x, y in pts],
            mode, k)

    def footprint(layer):
        px = layer.load()
        pts = [(x, y) for y in range(size[1]) for x in range(size[0])
               if px[x, y][3] >= 128]  # the alpha-255 core, not the .2 halo
        assert pts, "no core pixels"
        return (min(p[0] for p in pts), max(p[0] for p in pts),
                min(p[1] for p in pts), max(p[1] for p in pts))

    for aa_v, ref_v in zip(footprint(aa), footprint(ref)):
        assert abs(aa_v - ref_v) <= 1


def test_render_trace_halo_transparent_keeps_gap_clean():
    # Regression 2026-09-25 (user: "halo transparent, toujours sous la trace,
    # comme Mi Home"): adjacent back-and-forth rows sit 3.4 scene units apart
    # — under the old .2-alpha 5-unit halo they merged into a room-colour
    # wash (gap alphas up to ~90 with per-polyline stacking) and hid the
    # neighbouring cores. The halo layer is now transparent: the gap must be
    # fully clean, while the cores stay white on top (halo under, cores over
    # — Mi Home's SVG stacking order).
    k = 3
    # _screen_to_pixel: col = sx - y_max, row_top = (h-1) - ((800 - sy) - x_min)
    # → rows 8.0 and 11.4, centres py 25.5 / 35.7 (width 3), gap rows 29..33
    data = {"width": 20, "height": 16, "x_min": 693, "y_max": 5, "scale": k}
    # one continuous polyline like a real record: rows sy=100 / sy=103.4
    segs = [(2, [(10.0, 100.0), (20.0, 100.0),
                 (20.0, 103.4), (10.0, 103.4)])]
    layer = xtl_map._antialiased_trace_layer(segs, data, (20 * k, 16 * k))
    px = layer.load()
    # x stays clear of the px-45..48 connector column (legitimate white there)
    for y in range(29, 34):
        for x in range(18, 43):
            assert px[x, y][3] == 0, f"halo wash back: alpha {px[x, y][3]} at {(x, y)}"
    assert px[31, 25][3] >= 200 and px[31, 36][3] >= 200  # cores on top, white


# The glyphs arrive supersampled + LANCZOS-resampled (PIL cannot antialias
# directly), so only shape interiors keep pure source pixels: locate them
# through tolerant predicates instead of exact colours.
def _is_white(p):  # heading dot (+ mint rim) and dock halo/arrows
    return p[3] >= 200 and min(p[:3]) >= 235


def _is_dark(p):  # robot_svg body stack (#121C18); nothing else is this dark
    return p[3] >= 200 and max(p[:3]) <= 90


def _is_face(p):  # #EAF2F4 rim; b-g >= 1 rejects floor->white antialias
                  # blends (floor #D6E4E4 has b == g, face has b - g == 2)
    return (p[3] >= 200 and 225 <= p[0] <= 246
            and 3 <= p[2] - p[0] <= 20 and p[2] - p[1] >= 1)


def _is_dock_teal(p):  # #2CD5AE; the window covers LANCZOS ringing around
                       # the white arrows (#2CD5AE is exact in few pixels)
    return (p[3] >= 200 and abs(p[0] - 0x2C) <= 15
            and abs(p[1] - 0xD5) <= 15 and abs(p[2] - 0xAE) <= 15)


def _centroid(img, pred, box=None):
    """Mean (x, y) of the pixels passing `pred` (inside an optional crop)."""
    x0, x1 = (0, img.width) if box is None else (box[0], box[1])
    y0, y1 = (0, img.height) if box is None else (box[2], box[3])
    pts = [(x, y) for y in range(y0, y1) for x in range(x0, x1)
           if pred(img.getpixel((x, y)))]
    assert pts, "no pixel matched the predicate"
    return sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts)


_X, _Y = 11, 788  # mid-canvas pose on the 22x22 fixtures below (a 14x14
                  # canvas clipped the dock glyph, biasing centroid checks)


def _render(live=None, **kw):
    raw = _build_file(bytes([2] * 484), width=22, height=22, **kw)
    return Image.open(io.BytesIO(
        xtl_map.render_xtl_map(raw, scale=xtl_map.SCALE, live=live)["image_png"]))


_CHARGING = {"station_status": "Charging"}


def _centre_fixture():
    return xtl_map._screen_to_pixel(_X, _Y, {
        "width": 22, "height": 22, "x_min": 0, "y_max": 0})


def test_render_robot_stack_is_plugin_robot_svg():
    img = _render(pos={"x": _X, "y": _Y, "a": 0})
    data = set(img.getdata())
    # Solid-interior layers survive antialiasing as pure pixels...
    assert xtl_map._COLOR_ROBOT_BODY in data
    assert xtl_map._COLOR_DOCK_DISC not in data  # no dock in this fixture
    # ...the FACE ring does not: 0.8 px wide between two dark discs, it is
    # only a grey blend at PNG scale (exactly as the app renders it zoomed
    # out), so geometry is asserted through the dot instead.
    cx, _ = _centre_fixture()
    # At PNG scale the heading dot nearly touches the centre (as in the app
    # zoomed out); its mass still sits on the +X lobe, not on the ring.
    dx, _ = _centroid(img, _is_white)
    assert dx - cx > 1


def test_render_robot_dot_flips_with_heading():
    def dot_side(a):
        img = _render(pos={"x": _X, "y": _Y, "a": a})
        cx, _ = _centre_fixture()
        dx, _ = _centroid(img, _is_white)  # no dock -> dot (+ rim) only
        return dx - cx
    # a=0 -> deg=-90 -> local +Y (the dot) lands on +X of centre; a half
    # turn (pi rad = 314 in 0.01-rad units) mirrors it.
    assert dot_side(0) > 0.5
    assert dot_side(314) < -0.5


def test_render_dock_is_plugin_base_charge_svg():
    out = xtl_map.render_xtl_map(_build_file(
        bytes([2] * 484), width=22, height=22,
        charge_pos={"x": _X, "y": _Y, "a": 0}), scale=xtl_map.SCALE,
        live=_CHARGING)
    img = Image.open(io.BytesIO(out["image_png"]))
    data = set(img.getdata())
    # Teal disc + white halo/arrows (pre-parity builds drew a dark blob or
    # a home icon instead).
    assert xtl_map._COLOR_DOCK_DISC in data
    assert xtl_map._COLOR_WHITE in data
    assert not any(_is_face(p) for p in data)  # no robot in this fixture
    assert out["attributes"]["charger"] == {"x": _X, "y": _Y, "a": 0}
    assert out["attributes"]["station_icon"] == "charge"
    # Placement: symbol point (17, 23) lands on chargePos, so both glyph
    # columns sit 1 symbol unit right of it (halo centre x=18, disc x=18).
    # Painting in absolute symbol coords instead shifts everything by
    # 17*_ICON_K ~ 13 px — caught here, invisible to mere presence checks.
    cx, _ = _centre_fixture()
    want_x = cx + 1.0 * xtl_map._ICON_K
    for pred in (_is_white, _is_dock_teal):
        dx, _ = _centroid(img, pred)
        assert abs(dx - want_x) < 1.5, f"anchor slip: dx={dx:.1f} want {want_x:.1f}"


def test_render_dock_at_rest_uses_plugin_base_normal_svg():
    # No station token (or an inactive one): the plugin's StationIcon
    # default branch — dark #121C18 disc + lightnings, NOT the teal
    # charging disc (teal means active charging, like Mi Home).
    raw = _build_file(bytes([2] * 484), width=22, height=22,
                      charge_pos={"x": _X, "y": _Y, "a": 0})
    out = xtl_map.render_xtl_map(raw, scale=xtl_map.SCALE)
    img = Image.open(io.BytesIO(out["image_png"]))
    data = set(img.getdata())
    assert xtl_map._COLOR_DOCK_DISC_REST in data
    assert xtl_map._COLOR_DOCK_DISC not in data
    assert xtl_map._COLOR_WHITE in data
    assert out["attributes"]["station_icon"] == "normal"
    # The resting icon is what ordinary renders always drew: hash untouched.
    assert out["content_hash"] == hashlib.sha256(raw).hexdigest()
    # Inactive tokens (ChargeAsleep = full/charged, returning, paused) are
    # None on the plugin getter too -> same resting glyph.
    for token in ("ChargeAsleep", "BackClctDust", "BPPauseReturn", "Relocate"):
        rest = xtl_map.render_xtl_map(raw, scale=xtl_map.SCALE,
                                      live={"station_status": token})
        assert rest["attributes"]["station_icon"] == "normal", token


def test_render_dock_activity_icons_are_plugin_base_svgs():
    raw = _build_file(bytes([2] * 484), width=22, height=22,
                      charge_pos={"x": _X, "y": _Y, "a": 0})
    plain = xtl_map.render_xtl_map(raw, scale=xtl_map.SCALE)
    cx, _ = _centre_fixture()
    want_x = cx + 1.0 * xtl_map._ICON_K
    seen = {}
    # The plugin getter maps WashMop->wash, ClctDust->collect,
    # HotDry|WindDry->dry, Charging->charge, everything else->normal —
    # and the map token is matched case-tolerantly, like the ENUM sensor.
    for token, want in (("WashMop", "wash"), ("ClctDust", "collect"),
                        ("HotDry", "dry"), ("WindDry", "dry"),
                        ("charging", "charge")):
        out = xtl_map.render_xtl_map(raw, scale=xtl_map.SCALE,
                                     live={"station_status": token})
        assert out["attributes"]["station_icon"] == want, token
        img = Image.open(io.BytesIO(out["image_png"]))
        data = set(img.getdata())
        # Activity glyphs all keep the teal disc and the shared skeleton...
        assert xtl_map._COLOR_DOCK_DISC in data
        assert xtl_map._COLOR_WHITE in data
        # ...and the same <use x=-17 y=-23> anchor law.
        for pred in (_is_white, _is_dock_teal):
            dx, _ = _centroid(img, pred)
            assert abs(dx - want_x) < 1.5, f"{token}: {pred} anchor {dx:.1f}"
        # A status flip on an *unchanged* blob must repaint through the
        # MapCache no-op guard: variant folded into the digest.
        assert out["content_hash"] != plain["content_hash"], token
        seen[token] = list(img.getdata())
    # Distinct activities draw distinct motifs (HotDry/WindDry share the
    # plugin's wind glyph, so compare across the other axes).
    assert seen["WashMop"] != seen["ClctDust"]
    assert seen["WashMop"] != seen["HotDry"]
    assert seen["ClctDust"] != seen["Charging".lower()]
    assert seen["HotDry"] == seen["WindDry"]
    assert seen["HotDry"] != seen["charging"]


def test_parked_robot_shifts_to_dock_like_the_app():
    pos = {"x": _X, "y": _Y, "a": 0}
    charge = {"x": _X, "y": _Y, "a": 0}
    # Body disc centroid: symmetric glyph, so it marks the pose exactly even
    # with the dock under it — rendered charging, since the resting dock
    # glyph itself draws #121C18 pixels (the teal disc does not).
    free = _centroid(_render(pos=pos), _is_dark)
    parked = _centroid(_render(live=_CHARGING, pos=pos, charge_pos=charge),
                       _is_dark)
    # chargePos + 5 grid units down (y-down), x snapped onto the dock.
    assert abs(parked[0] - free[0]) < 1
    assert 9.0 < parked[1] - free[1] < 11.0


def test_zero_pose_upload_parks_the_robot_on_the_dock():
    # Regression (2026-09-23): the docked zero pose used to be drawn at
    # scene (0,0) — off-canvas, so the robot icon vanished from the camera
    # while only the station stayed. It now lands on chargePos + 5, exactly
    # where the app's isInBaseStation branch draws it.
    charge = {"x": _X, "y": _Y, "a": 0}
    img = _render(live=_CHARGING, pos={"x": 0, "y": 0, "a": 0, "i": 0},
                  charge_pos=charge)
    want = xtl_map._screen_to_pixel(_X, _Y + 5, {
        "width": 22, "height": 22, "x_min": 0, "y_max": 0})
    dark = _centroid(img, _is_dark)
    assert abs(dark[0] - want[0]) < 1
    assert abs(dark[1] - want[1]) < 1
    # The attributes keep the raw truth: pose unknown while parked.
    out = xtl_map.render_xtl_map(_build_file(
        bytes([2] * 484), width=22, height=22,
        pos={"x": 0, "y": 0, "a": 0, "i": 0}, charge_pos=charge),
        scale=xtl_map.SCALE)
    assert out["attributes"]["vacuum_position"] is None
    assert out["attributes"]["charger"] == charge


# --- XVMC contract (calibration_points + rooms record) ----------------------

_FIX = {"width": 22, "height": 22, "x_min": 0, "y_max": 0}


def _fixture(scale=None):
    return {**_FIX, "scale": scale or xtl_map.SCALE}


def test_pixel_to_scene_is_exact_inverse_of_screen_to_pixel():
    data = _fixture(scale=4)
    for px, py in ((0, 0), (46.0, 22.0), (90.5, 88.0), (11, 3)):
        sx, sy = xtl_map._pixel_to_scene(px, py, data)
        rx, ry = xtl_map._screen_to_pixel(sx, sy, data)
        assert abs(rx - px) < 1e-9
        assert abs(ry - py) < 1e-9


def test_calibration_points_affine_matches_the_renderer():
    # The card solves a 3-point affine from these pairs (CoordinatesConverter
    # AFFINE branch), so pinning each pair against the renderer's own
    # scene->pixel transform is the whole contract.
    data = _fixture()
    pts = xtl_map._calibration_points(data, 22 * data["scale"],
                                      22 * data["scale"])
    assert len(pts) == 3
    assert [p["map"] for p in pts] == [{"x": 0.0, "y": 0.0},
                                       {"x": 44.0, "y": 0.0},
                                       {"x": 0.0, "y": 44.0}]
    for p in pts:
        px, py = xtl_map._screen_to_pixel(p["vacuum"]["x"], p["vacuum"]["y"],
                                          data)
        assert abs(px - p["map"]["x"]) < 0.02
        assert abs(py - p["map"]["y"]) < 0.02
    # vacuum side = scene units: TL of a 22x22 crop pinned at y_max=0,
    # x_min=0 is scene (22*... ) — pin one exact corner (cell corner grid,
    # k=2): pixel (0,0) sits half a cell above/left of cell (0,0) centre.
    assert pts[0]["vacuum"] == {"x": -0.5, "y": 778.5}


def _raster_with_room(cells, rid=3, floor=2):
    raster = bytearray([floor] * (22 * 22))
    for c, r in cells:  # image coords, row 0 = top
        raster[(22 - 1 - r) * 22 + c] = rid
    return bytes(raster)


def test_rooms_attr_is_id_keyed_record_with_icons():
    raw = _build_file(
        _raster_with_room([(7, 3), (8, 3), (7, 4)]), width=22, height=22,
        areas=[_area(1, "Salon", room_id=3, cx=9, cy=790),
               _area(2, "Chambre", room_id=4, cx=15, cy=785)])
    out = xtl_map.render_xtl_map(raw, scale=xtl_map.SCALE)
    rooms = out["attributes"]["rooms"]
    assert set(rooms) == {"3", "4"}  # record keyed by room_id (XVMC iterates it)
    salons = rooms["3"]
    assert salons["name"] == "Salon" and salons["room_id"] == 3
    # raw plugin fields stay raw; the card anchor (x/y) is the transposed
    # center — untransposed synthetic fixtures like this one land outside the
    # traced bbox and take the bbox-centre fallback instead
    assert salons["center_x"] == 9 and salons["center_y"] == 790
    assert salons["x"] == (salons["x0"] + salons["x1"]) / 2.0
    assert salons["y"] == (salons["y0"] + salons["y1"]) / 2.0
    assert salons["icon"] == "mdi:floor-plan"  # no type in the file -> "0"
    assert "outline" in salons and "x0" in salons
    assert "outline" not in rooms["4"]  # no raster cells -> no geometry
    assert rooms["4"]["icon"] == "mdi:floor-plan"
    # typed areas get the app-equivalent icon (type only lives in raw JSON)
    typed = _build_file(
        _raster_with_room([(7, 3)]), width=22, height=22,
        areas=[{**_area(1, "Chambre", room_id=3), "type": 3}])
    t_out = xtl_map.render_xtl_map(typed, scale=xtl_map.SCALE)
    assert t_out["attributes"]["rooms"]["3"]["icon"] == "mdi:bed"
    assert xtl_map._room_icon("12") == "mdi:broom"   # code gap
    assert xtl_map._room_icon("nope") == "mdi:broom"  # non-numeric
    assert out["attributes"]["map_id"] == 7
    assert out["attributes"]["updated_at"] > 0


def test_room_center_is_transposed_scene_frame():
    # The KS3 areas JSON stores room centres TRANSPOSED against the raster/
    # calibration frame (live map 2026-09-23: room 4 centre (376,459) — 376
    # is below the crop's own x_min of 379.5 — while the swapped reading
    # (459,376) sits inside the traced outline; same for rooms 5 and 6).
    # x/y carry the corrected pair the card anchors on; centre_x/centre_y
    # stay raw; a centre outside the bbox takes the bbox-centre fallback.
    cells = [(c, r) for r in range(2, 8) for c in range(12, 18)]  # solid 6x6

    def render(cx, cy):
        raw = _build_file(_raster_with_room(cells), width=22, height=22,
                          areas=[_area(1, "Chambre", room_id=3, cx=cx, cy=cy)])
        return xtl_map.render_xtl_map(
            raw, scale=xtl_map.SCALE)["attributes"]["rooms"]["3"]

    garbage = render(1, 1)  # matches neither frame -> fallback
    mid_x = (garbage["x0"] + garbage["x1"]) / 2.0
    mid_y = (garbage["y0"] + garbage["y1"]) / 2.0
    assert (garbage["x"], garbage["y"]) == (mid_x, mid_y)

    real = render(mid_y, mid_x)  # file's (centerX, centerY) == scene (y, x)
    assert (real["x"], real["y"]) == (mid_x, mid_y)
    assert (real["center_x"], real["center_y"]) == (mid_y, mid_x)


@pytest.mark.parametrize("cells, corners", [
    # rect: 5x3 cells cols 7..11 rows 3..5 -> four cell corners
    ([(c, r) for r in range(3, 6) for c in range(7, 12)], 4),
    # L-pentacell: exactly six turning corners
    ([(6, 4), (7, 4), (8, 4), (6, 5), (6, 6)], 6),
])
def test_room_outline_traces_turning_corners(cells, corners):
    raw = _build_file(_raster_with_room(cells), width=22, height=22,
                      areas=[_area(1, "R", room_id=3)])
    room = xtl_map.render_xtl_map(
        raw, scale=xtl_map.SCALE)["attributes"]["rooms"]["3"]
    assert len(room["outline"]) == corners
    # self-consistency: bbox == outline extremes, and every outline point
    # round-trips back to a cell-corner pixel of the rendered image
    xs = [p[0] for p in room["outline"]]
    ys = [p[1] for p in room["outline"]]
    assert (room["x0"], room["x1"]) == (min(xs), max(xs))
    assert (room["y0"], room["y1"]) == (min(ys), max(ys))
    data = _fixture()
    for x, y in room["outline"]:
        px, py = xtl_map._screen_to_pixel(x, y, data)
        assert abs(px % data["scale"]) < 1e-6
        assert abs(py % data["scale"]) < 1e-6


def test_dash_helper_leaves_gaps():
    img = Image.new("RGBA", (24, 4), (0, 0, 0, 0))
    xtl_map._dash_polyline(ImageDraw.Draw(img), [(0, 2), (20, 2)],
                           (255, 0, 0, 255), 1, dash=(4, 2))
    px = img.load()
    assert px[1, 2][3] > 0    # first on-run
    assert px[5, 2][3] == 0   # inside the off-run
    assert px[6, 2][3] > 0    # second on-run starts
    assert px[19, 2][3] > 0   # tail run up to the segment end


# --- live task state (cleaning-highlight parity) -----------------------------------------------

def test_clean_view_maps_robot_status_like_the_plugin():
    def mode(code):
        return xtl_map._clean_view(_live(robot_status=code))["mode"]

    assert mode(2) == "smart" and mode(4) == "smart"
    assert mode(6) == "building" and mode(15) == "building"  # QMAP/Carpet fold
    assert mode(8) == "room" and mode(11) == "zoning"
    assert mode(17) == "spot"
    assert mode(1) == "idle" and mode(99) == "idle"          # unknown -> Idle


def test_clean_view_plan_b_reads_the_clean_values_shape():
    # 17/9 unreadable locally (None): room ids vs zone corner runs decide it.
    v = xtl_map._clean_view(_live(robot_status=None, clean_values="[16, 12, 9]"))
    assert v["mode"] == "room" and v["selected"] == [16, 12, 9]
    v = xtl_map._clean_view(_live(
        robot_status=None, clean_values="[320,410,420,410,420,510,320,510]"))
    assert v["mode"] == "zoning" and v["selected"] == []
    v = xtl_map._clean_view(_live(robot_status=None, clean_values=None))
    assert v["mode"] == "idle"
    # 17/9 wins over the shape when readable, and gates the selection:
    v = xtl_map._clean_view(_live(robot_status=11, clean_values="[7, 8]"))
    assert v["mode"] == "zoning" and v["selected"] == []
    # list-valued (unparsed) clean_values works like the JSON string:
    v = xtl_map._clean_view(_live(robot_status=8, clean_values=[3, 4]))
    assert v["mode"] == "room" and v["selected"] == [3, 4]


def test_room_colors_highlight_includes_the_selection():
    # colourMapping highlights highlighAreaIds UNION selectedAreas: tapping a
    # room in Room mode repaints it like claiming a sequence slot.
    areas = [_area(1, room_id=3, neibs=""), _area(2, room_id=4, neibs="")]
    base = xtl_map._room_colors(areas)
    assert base[3] == xtl_map._ROOM_COLORS[0]
    assert base[4] == xtl_map._ROOM_COLORS[1]
    sel = xtl_map._room_colors(areas, selected=[4])
    assert sel[3] == base[3]
    assert sel[4] == xtl_map._HIGHLIGHT_COLORS[1]


def test_render_bakes_no_room_labels():
    # Room names/icons are the CARD's job (it draws predefined_selections in
    # screen space); the old AreaTipView port baked chips + text into the PNG
    # and they scaled and drifted under pan/zoom. A named area must not move a
    # single pixel, and a full room tint must stay the only colour in frame.
    def render(name):
        raw = _build_file(bytes([3] * (22 * 22)), width=22, height=22,
                          areas=[_area(1, name, room_id=3, cx=10, cy=790)])
        return Image.open(io.BytesIO(xtl_map.render_xtl_map(
            raw, scale=xtl_map.SCALE)["image_png"]))

    assert list(render("Séjour").getdata()) == list(render("").getdata())
    assert set(render("Séjour").getdata()) == {xtl_map._ROOM_COLORS[0]}


def _live(**over):
    """A coordinator-shaped live dict; default: a whole-home task cleaning."""
    base = {"robot_status": 2, "clean_values": "[]", "work_mode": 0,
            "fan": 1, "water": 1, "route": 1, "count": 1}
    base.update(over)
    return base


def test_render_relocation_hides_robot_and_shows_centre_pin():
    raw = _build_file(bytes([2] * 484), width=22, height=22,
                      pos={"x": _X, "y": _Y, "a": 0})
    plain = xtl_map.render_xtl_map(raw, scale=xtl_map.SCALE, live=_live())
    reloc = xtl_map.render_xtl_map(raw, scale=xtl_map.SCALE,
                                   live=_live(status_code=17))
    assert plain["attributes"]["relocating"] is False
    assert reloc["attributes"]["relocating"] is True

    img = Image.open(io.BytesIO(reloc["image_png"]))
    data = set(img.getdata())
    assert xtl_map._COLOR_ROBOT_BODY in set(  # robot there normally...
        Image.open(io.BytesIO(plain["image_png"])).getdata())
    assert xtl_map._COLOR_ROBOT_BODY not in data  # ...gone under the badge

    # The badge is the official lottie (frame 0), NOT the old red hand-drawn
    # marker: teal gradient body + white eye, and not one reddish pixel.
    def near(c, ref, tol):
        return all(abs(c[i] - ref[i]) <= tol for i in range(3))

    px = list(img.getdata())
    s, e, stops = xtl_map._PIN_FRONT_GRAD
    teal = [p for p in px if near(p[:3], stops[1][1], 40)]
    eye = [p for p in px if p == xtl_map._PIN_EYE_COLOR + (255,)]
    assert len(teal) > 150                      # body in the lottie's teal
    # The eye is only ~9 unit-wide at nominal scale — a dozen exact pixels
    # (plus AA-blended rim), enough to prove the white contour paints.
    assert len(eye) > 8                          # the white eye reads through
    assert not any(p[0] > 0xC0 and p[0] > p[1] + 40 and p[0] > p[2] + 40
                   for p in px)                  # zero red: no old pin left

    # Centred: the teardrop head's mass sits on the image centre, tip down.
    xs = [i % img.width for i, p in enumerate(px)
          if near(p[:3], stops[1][1], 40)]
    assert abs(sum(xs) / len(xs) - img.width / 2.0) <= 2.0

    # Same blob both times: the ordinary render keeps hashing exactly
    # sha256(raw); the pin render folds a marker in so MapCache.async_upsert
    # can't no-op the pin/robot toggle on an unchanged upload.
    assert plain["content_hash"] == hashlib.sha256(raw).hexdigest()
    assert reloc["content_hash"] != plain["content_hash"]


# --- MapFetcher wiring ---------------------------------------------------------

class FakeCloud:
    user_id = "u1"

    def __init__(self, url="https://ks3.example/map", blob=b""):
        self._url, self._blob = url, blob
        self.calls = []

    def map_url(self, server, did, map_name, endpoint, raw_obj=False):
        self.calls.append((server, did, map_name, endpoint, raw_obj))
        return self._url

    def download(self, url):  # noqa: ARG002
        return self._blob


def _xtl_fetcher(cloud):
    return MapFetcher(cloud, server="de", user_id="1", device_id="2",
                      model="xtl.vacuum.xm2216")


def test_map_fetcher_xtl_needs_no_third_party_parser():
    f = _xtl_fetcher(FakeCloud())
    # xtl-only fork: no parser object, no key material, plain-JSON KS3 path.
    assert not hasattr(f, "_parser") and not hasattr(f, "_unpack_kw")
    assert f._endpoint == "get_interim_file_url_pro"


def test_fetch_obj_returns_mapresult():
    raw = _build_file(bytes([2, 2, 2, 2]), width=2, height=2)
    cloud = FakeCloud(blob=raw)
    # Live get-map-data value shape: fully qualified uid/did/slot — must
    # reach the cloud verbatim (raw_obj=True), never re-prefixed.
    res = _xtl_fetcher(cloud).fetch_obj("1628932731/1152219162/0")
    assert res is not None and res.map_id == 7
    assert res.content_hash == hashlib.sha256(raw).hexdigest()
    assert cloud.calls[-1] == (
        "de", "2", "1628932731/1152219162/0", "get_interim_file_url_pro", True)


def test_fetch_obj_bare_slot_still_gets_prefixed():
    # A firmware variant answering with a plain slot number keeps the
    # standard uid/did/ prefix assembly.
    raw = _build_file(bytes([2, 2, 2, 2]), width=2, height=2)
    cloud = FakeCloud(blob=raw)
    _xtl_fetcher(cloud).fetch_obj("0")
    assert cloud.calls[-1] == ("de", "2", "0", "get_interim_file_url_pro", False)


def test_fetch_obj_no_url_is_session_expired():
    with pytest.raises(SessionExpired):
        _xtl_fetcher(FakeCloud(url=None)).fetch_obj("x")


def test_fetch_obj_forwards_live_to_the_renderer(monkeypatch):
    seen = {}

    def fake_render(raw, scale=None, *, live=None):
        seen["live"] = live
        return None

    monkeypatch.setattr("xvac.map.render_xtl_map", fake_render)
    # Non-empty download: an empty body short-circuits to None before render.
    assert _xtl_fetcher(FakeCloud(blob=b"ks3")).fetch_obj(
        "1628932731/1152219162/0", live={"work_mode": 4}) is None
    assert seen["live"] == {"work_mode": 4}


def test_fetch_obj_garbage_returns_none():
    assert _xtl_fetcher(FakeCloud(blob=b"garbage")).fetch_obj("x") is None


# --- map-infos catalogue (user-given map titles) ---------------------------

def test_fetch_map_infos_reads_titles_verbatim_obj():
    # Plugin shape (setMapInfos): JSON array of {mapId, name, saved, ...}.
    catalog = json.dumps([
        {"mapId": 3, "name": "Appart brest", "saved": 1, "status": 1},
        {"mapId": 4, "name": None, "saved": 1},
    ]).encode()
    cloud = FakeCloud(blob=catalog)
    names, rows = _xtl_fetcher(cloud).fetch_map_infos(
        "1628932731/1152219162/infos")
    assert names == {3: "Appart brest"}
    # The same bytes feed the switchable list now (bug 2026-09-26: the
    # Active Map select only ever had the current map): saved rows keep
    # their id, blank name falls to the caller's placeholder, cur=1 follows
    # status===1 (the plugin's in-use marker).
    assert rows == [{"name": "Appart brest", "id": 3, "cur": 1},
                    {"name": None, "id": 4, "cur": 0}]
    assert cloud.calls[-1] == (
        "de", "2", "1628932731/1152219162/infos",
        "get_interim_file_url_pro", True)


def test_fetch_map_infos_junk_is_empty_not_fatal():
    assert _xtl_fetcher(FakeCloud(blob=b"garbage")).fetch_map_infos("x") \
        == ({}, [])
    assert _xtl_fetcher(FakeCloud(blob=b"")).fetch_map_infos("x") == ({}, [])
    # A cloud that mints no URL: names degrade to {}, never SessionExpired —
    # titles are display polish and must never black out the camera.
    assert _xtl_fetcher(FakeCloud(url=None)).fetch_map_infos("x") == ({}, [])
