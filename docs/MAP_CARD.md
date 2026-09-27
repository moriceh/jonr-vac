[← Back to the README](../README.md)

# Map card

## The bundled card

The integration ships its own Lovelace card and **registers it
automatically** (as a storage-mode Lovelace resource, cache-busted by file
mtime) — no manual resource needed as long as your Lovelace runs in storage
mode. In YAML mode the startup log prints the resource snippet to add
yourself. The card keeps its upstream filename/element tag so existing
dashboards keep working:

```yaml
type: custom:xiaomi-vac-card
vacuum: vacuum.your_vacuum
```

Map-forward dashboard: swipe between the vacuum image and the map, status
and battery chrome, room tinting from the live vector, and control tray
(water/fan/active-map selects). Map data comes from the integration's vector
endpoint; the card needs a **cloud** entry (the map pipeline is cloud-fed —
see [Installation](INSTALLATION.md)).

## The vector endpoint

```text
GET /api/jonr_vac/map/<entry_id>
```

The latest vector map JSON. `<entry_id>` is the config-entry id (the card
resolves it from the vacuum entity; the map camera's attributes carry
`rooms` and calibration for manual setups).

### The vector contract

The map page of the bundled card is driven entirely by the `vector` of the
active map entry — it builds one page per entry carrying a non-empty
`rooms` **list**, and draws rooms from traced contours rather than
bounding boxes when they are available. The xtl renderer feeds every part
of that contract from the same decoded blob that produces the camera PNG
(the two never drift, and `content_hash` stays exactly `sha256(blob)`):

| vector key | content | frame |
|---|---|---|
| `map_id` | Mi Home map head id | — |
| `size` / `grid_rle` | raster cell values (`[value, run]` pairs, row 0 = south), only when every room label falls in the card's paint band | grid cells |
| `bounds` / `resolution` | crop origin (half-cell corner anchors) and cell size (0.05 m) | metres |
| `rooms` | `{id, name, cx, cy, bbox}` per room with traced geometry; `id` is the room id the [services](SERVICES.md) consume | metres |
| `room_chains` | exact cell-corner contours per room (list of rings) — the card fills these; the `bbox` rectangle is the fallback | grid-corner coords |
| `charger` / `vacuum` | dock and robot dots; `vacuum` mirrors the PNG glyph (docked poses parked, relocation omits the dot for the badge) | metres |
| `path_segments` | the cleaning route polylines, break-split like the PNG trace | metres |
| `walls` / `carpets` | dashed virtual walls, carpet zones | metres |

Room raster cells only ship when every room label lies inside the card's
10–59 paint band (JONR room ids are 3–6, so JONR maps take the
chains-only path — the traced contours carry the tint, and the `size` /
`grid_rle` keys stay absent, which is exactly how the card decides to
skip the raster layer). Room centres land on the same pixel the PNG drew:
the card's grid-corner law and the renderer's scene→pixel transform are
algebraically pinned against each other by the test suite, including the
robot's transposed area-centre frame.

Live keys (`vacuum`, `path_segments`, …) are stripped from inactive
entries served under `.maps`, so a stale robot never ghosts onto the wrong
floor plan.

## The map camera

`camera.<device>_map` renders the floor plan as a PNG for any image tile,
and its attributes expose the machine-readable map:

- `rooms` — one record per room: `id`, Mi Home `name`, centre, bbox. The
  `id` values are exactly what the room services consume
  ([Services](SERVICES.md)).
- `station_icon` — which dock glyph the render drew (see below).

## The station icon

The dock is drawn as a **vector glyph that changes with what the station is
doing**, mirroring the official Mi Home plugin's map (same `base_*_svg`
symbols, same anchor): washing mops, emptying the dust bag, drying the
mops, charging — and a dark resting icon whenever none of those is active
(a robot away, full or merely "returning" shows the resting glyph, exactly
like Mi Home). The activity comes from the dock's 17.49 status token, the
same readout as the `station_status` sensor, so the glyph follows the
station within a refresh cycle; `station_icon` on the camera exposes which
variant is on screen (`wash` / `collect` / `dry` / `charge` / `normal`).

## The relocation pin

While the robot is localizing (`status_code` 17 — "Localisation en cours"),
the render hides the robot and drops the **official Mi Home pin** at the
map's centre, reproducing the plugin's `RelocateTipView`. The vendor asset
is a Lottie animation
(`raw/projects_comxtlrobottest01_src_assets_lottie_relocation.json` in the
official JONR plugin bundle — a 66×66 vector comp, not a PNG); we ship the
**static frame 0** of that same composition — the bounce and the ground-
ellipse pulses are animation-only, so a still loses nothing — drawn by the
integration's own vector rasteriser: the same SVG-path fill + supersampled
anti-aliasing pipeline as the dock glyph, including the two linear-gradient
teardrops, the halo, the white eye and the three ground ellipses, all with
the plugin's colors and transforms transcribed from the Lottie JSON. No
rasterized asset is involved, so the pin stays crisp at any scale.

The pin is drawn from `spec/profiles/xtl.py`'s companion renderer
(`xtl_map.py`'s `_PIN_*` constants — path `d` strings verbatim from the
comp, anchor at the teardrop's tip). Relocating renders fold a `reloc` salt
into the content hash, so a cached ordinary map never gets reused as a
relocation frame or vice versa.

## Xiaomi Vacuum Map Card (XVMC) presets

The camera's attributes implement the calibration contract of Piotr
Machowski's [lovelace-xiaomi-vacuum-map-card](https://github.com/PiotrMachowski/lovelace-xiaomi-vacuum-map-card),
so its room/zone/service presets work against this integration:
`calibration_source: {camera: true}` plus the `rooms` attribute, and service
presets call this integration's [services](SERVICES.md) (`clean_segment`,
`clean_zone`, `set_room_clean_mode`, …). That's the tap-a-room-and-go setup;
the bundled card covers the everyday controls without extra YAML.

## Map switching

The **Active Map** select lists the floors saved in Mi Home — exactly the
cards the Mi Home app can restore, and the same contract: switching is a
`switch-map` (17.9) replayed on the saved floor, with the cloud channel
first and a local-UDP confirmation retry. Unsaved robot drafts are never
offered (tapping one is how maps get "lost" — Mi Home's *restore* is the
same switch-map on the saved floor). If a floor you expect is missing, tap
it in Mi Home once so it is in the published catalogue, then refresh the
map (`refresh_map` [service](SERVICES.md)).
