[← Back to the README](../README.md)

# Entities

One **device** is created per config entry, carrying every entity below.
Entity names resolve through the integration's own translations (English and
French are complete — see [Translations](TRANSLATIONS.md)). Entities whose MIoT
property the robot does not answer for are hidden by an availability gate
rather than sitting `unknown` forever.

## Vacuum

The main `vacuum` entity: start, pause, stop, return-to-dock and locate
(locate is the "find me" beep). Everything richer than these five lives in
the [services](SERVICES.md).

| Attribute / feature | Backed by |
|---|---|
| Activity (`cleaning` / `paused` / `idle` / `returning` / `docked` / `error`) | MIoT status (1.2-style state + `Relocate` reads as cleaning) |
| Fan speed preset, water level, mopping mode | mirrors of the selects below |
| Battery level | battery prop |

## Selects (13)

| `translation_key` | What it picks |
|---|---|
| `fan_speed` | Suction preset (quiet → max) |
| `water_level` | Mop water level |
| `mode` | Cleaning mode (sweep / mop / sweep+mop …) |
| `sweep_type` | Sweep variant the model exposes |
| `route` | Mop route pattern (fast / daily / fine zigzag) |
| `count` | Cleaning passes (×1 / ×2) |
| `carpet_prefer` | Carpet strategy (avoid / sweep once / boost …) |
| `dust_collection` | Dock auto-empty schedule |
| `drying_time` | Mop drying duration |
| `mop_wash_frequency` | Mop wash interval during cleaning |
| `voice_language` | Voice-pack language (the 11 spec codes; install packs with the `install_voice_pack` [service](SERVICES.md)) |
| `active_map` | Active saved floor — see [Map card](MAP_CARD.md) |

A `mop_wash_temp` label exists for models that expose an adjustable wash
temperature; the P20 Pro reports the temperature but the firmware never
offers it as a setting, so no select is created for it.

Only the selects whose value table the profile actually fills are created —
a model without a setting never shows an empty dropdown.

## Sensors (24)

| Group | Sensors |
|---|---|
| Status | `status` (activity enum), `battery` |
| Errors & station | `robot_error`, `station_error` (both with `code` + `solution` attributes — the solution follows your UI language like the [notification bubbles](NOTIFICATIONS.md) — see [Events](EVENTS.md)), `robot_message`, `station_status` |
| Tanks & hardware | `clean_water_tank`, `drain_water_tank`, `dust_bag_state`, `mop_tank_state` |
| Consumable life (%) | `main_brush_life`, `side_brush_life`, `filter_life`, `mop_life`, `dust_bag_life`, `detergent_life`, `mop_trough_life`, `unit_sensor_life` |
| Stats | `session_clean_time`, `session_clean_area`, `total_clean_time`, `total_clean_area`, `total_clean_count` |
| Schedules | `clean_timers` — one line per timer, naming the floor and rooms from the live map |

## Switches (13)

| Switch | Effect |
|---|---|
| `wash_mop` / `dry_mop` / `collect_dust` | Run a dock cycle (washing / drying / auto-empty); switching off stops it |
| `auto_drying` | Auto-dry the mop after cleaning |
| `do_not_disturb` | Turn the DND window on/off (`set_dnd_time` programs the window; the `dnd_no_dust` / `dnd_no_dry` flags skip dock cycles inside it) |
| `carpet_boost` | Boost suction on carpet |
| `break_point` | Resume from the interruption point after recharging |
| `child_lock` | Lock the robot's buttons |
| `mop_augment` | Increased wetting |
| `repeat` | Two-pass cleaning toggle |
| `alarm` | Trigger the locate beep |

## Buttons (9)

Seven consumable-life resets — `reset_main_brush`, `reset_side_brush`,
`reset_filter`, `reset_mop`, `reset_dust_bag`, `reset_mop_trough`,
`reset_unit_sensor` — and the two mapping aids `fast_building` and
`clean_building` (quick-map / re-map the current floor).

## Numbers, times, camera

- **`volume`** — voice announcement volume, 0–100.
- **`dnd_start_time` / `dnd_end_time`** — the do-not-disturb window pickers
  (they write the same schedule the `set_dnd_time` [service](SERVICES.md) does).
- **`map`** camera — the rendered floor plan. Its state attributes carry the
  machine-readable map: `rooms` (per-room `id` / `name` / centre — the ids
  every room-based service consumes), the `station_icon` drawn on the dock
  (`wash` / `collect` / `dry` / `charge` / `normal` — the Mi Home activity
  glyph, [Map card](MAP_CARD.md)), plus map and trim metadata. The raw
  vector JSON is served at `/api/jonr_vac/map/<target>` for the companion
  card ([Map card](MAP_CARD.md)).
