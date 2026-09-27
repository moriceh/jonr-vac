[← Back to the README](../README.md)

# Services

Ten services sit on the `jonr_vac` domain, all **targeted at the vacuum
entity** (they are registered as entity services, so Dev Services → Actions
and the automation editor ask for a `target: entity_id`). Call them like:

```yaml
action: jonr_vac.clean_segment
target:
  entity_id: vacuum.your_vacuum
data:
  segments: [3, 5]
  repeats: 2
```

Room ids (`segments`, `rooms`, `room_id`) come from the map camera's
`rooms` attribute — see [Entities](ENTITIES.md). The same ids feed the map
card's presets ([Map card](MAP_CARD.md)).

## Cleaning tasks

### `clean_segment` — clean specific rooms

Clean one or more rooms by map room id.

| Field | Type | Notes |
|---|---|---|
| `segments` | list of int | Room ids to clean (map camera `rooms` attribute). |
| `repeats` | int, optional | Passes for this task only (1–2 — the map card's ×N button). Writes the global pass count before the task starts; omit to keep the current `count` select value. |

### `clean_zone` — clean rectangle zones

Like the Mi Home app's zone clean.

| Field | Type | Notes |
|---|---|---|
| `zones` | zone or list of zones | Each zone is `[x_min, y_min, x_max, y_max]` in map scene units on the 5 cm grid — the same frame as the room centres in the camera's `rooms` attribute. |
| `repeats` | int, optional | Passes, as in `clean_segment`. |

### `remote_control` — manual drive

The app's manual D-pad. The robot **keeps** the last command — it keeps
moving or turning until you send stop, so always finish with `0`.

| Field | Type | Notes |
|---|---|---|
| `command` | int 0–4 | 0 stop (closes the manual session), 1 forward, 2 turn left, 3 turn right, 4 backward. |

## Per-room personalisation

### `set_room_clean_mode`

Per-room settings, like the app's room panel. Omitted fields keep the
room's saved setting, then the robot's global value, then the firmware
default. The cleaning mode switches to **custom** automatically —
per-room values only apply in that mode.

| Field | Type | Notes |
|---|---|---|
| `room_id` | int | Map room id. |
| `work_mode` | int 0–3 | 0 sweep+mop, 1 sweep only, 2 mop only, 3 sweep then mop. |
| `fan` | int 0–3 | 0 quiet, 1 auto, 2 strong, 3 max. |
| `water` | int 0–2 | 0 low, 1 mid, 2 high. |
| `passes` | int 1–2 | One or two passes. |
| `route` | int 0–2 | 0 fast, 1 daily, 2 fine (zigzag). |

### `reset_room_clean_modes`

No fields. Restores every room on the current map to the firmware defaults
(sweep+mop / auto fan / mid water / one pass / daily route) — the app's
"default" button.

### `set_clean_sequence`

The whole-home room order (the app's custom order).

| Field | Type | Notes |
|---|---|---|
| `rooms` | list of int | Ordered room ids — first entry cleaned first. Pass `[]` to clear. Some firmwares reorder an ongoing whole-home clean live. |

### `set_room_info`

The app's room editor: rename and/or re-type a room. Omitted fields keep
their current value.

| Field | Type | Notes |
|---|---|---|
| `room_id` | int | Map room id. |
| `name` | string | New room name (max 32 bytes). |
| `category` | int 0–17 | Room type: 0 custom, 1 living room, 2 bathroom, 3 bedroom, 4 kitchen, 5 balcony, 6 dining room, 7 study, 8 gym, 9 hallway, 10 sunroom, 11 kids room, 13 lounge, 14 laundry, 15 entrance, 16/17 bedroom variants. |

## Scheduling & house rules

### `set_dnd_time`

Programs the Do-Not-Disturb window (the app's DND screen). This only
**stores** the schedule — the `do_not_disturb` [switch](ENTITIES.md) turns
the window on.

| Field | Type | Notes |
|---|---|---|
| `start_time` | string | Window start, 24 h (`"22:30"`). |
| `end_time` | string | Window end, 24 h. |
| `no_dust` | bool, optional | Skip the dock's auto-empty during DND (firmware ≥ 439.681). |
| `no_dry` | bool, optional | Skip mop drying during DND (firmware ≥ 439.681). |

### `refresh_map` — force a fresh upload

Briefly starts the vacuum so it leaves the dock, uploads a fresh map, then
sends it back. The robot has to physically move to push a new map — this is
why the confirmation field exists.

| Field | Type | Notes |
|---|---|---|
| `confirm_movement` | bool | Must be `true`. Refuses otherwise, precisely because the vacuum will leave the dock and return. |

Map refreshes normally arrive by themselves (Mi Home MQTT pushes while
cleaning, timed refreshes otherwise) — reach for this after a rebuild or
when a rename hasn't surfaced.

## Voice packs

### `install_voice_pack`

Installs a voice pack through the official plugin's channel: the **robot
downloads the pack from the url itself**, so the url must be reachable from
the robot's network — a LAN share on a PC works (`python -m http.server`).
Pack format: a **plain tar** (not gzipped, despite the vendor's `.tar.gz`
names) holding `media/music/<N>.ogg` voice files and a
`media/music/audio.conf` declaring the `Language`/`Id`.

The call answers honestly: Home Assistant fetches and proves the pack first
(md5, plain tar, `audio.conf`), then waits up to 120 s — the official
plugin's own give-up — for the robot to report the new pack. The call only
succeeds on that report; anything else raises an error quoting what the
robot said.

| Field | Type | Notes |
|---|---|---|
| `url` | string | HTTP(S) url of the pack tar the robot will download. |
| `md5` | string | Pack md5 (32 hex) — the robot verifies it. |
| `lang` | int 1–11 | Language code the pack installs as (its `audio.conf` `Id`). |
| `version` | string, optional | The `Ver` the robot must report for the install to count as confirmed. Read from the pack automatically when HA can reach the url — only needed for shares HA itself cannot reach. Give a custom pack a unique `Ver` so the install can be told apart from the pack it replaces. |

Built-in language packs are just CDN urls in that same format — the vendor
bucket for the P20 Pro:

```text
https://xtl-data-sg.ks3-sgp.ksyuncs.com/Jonr/xm2216/sounds/v2/<lang>.tar.gz
```

with `<lang>` one of `en ru de it fr pl es kr tw vt` (`zh` ships only
preloaded on the robot — the bucket no longer serves it). Install English
over the top of the current pack, for example:

```yaml
action: jonr_vac.install_voice_pack
target:
  entity_id: vacuum.your_vacuum
data:
  url: https://xtl-data-sg.ks3-sgp.ksyuncs.com/Jonr/xm2216/sounds/v2/en.tar.gz
  md5: b29036e0bf84045581cba6406b7441d2
  lang: 2
```

(`lang` = the `audio.conf` Id: 1 chinese, 2 english, 3 russian, 4 german,
5 italian, 6 french, 7 polish, 8 spanish, 9 korean, 10 chinese_tw,
11 vietnamese — the same codes as the `voice_language` select.)
