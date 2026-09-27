# JONR Vacuum (jonr_vac)

Unofficial Home Assistant integration for the **JONR P20 Pro** robot vacuum
(MIoT model `xtl.vacuum.xm2216`). Local MIoT control over LAN, plus the live
floor-plan map through the Xiaomi/Mi Home cloud.

**Unofficial — not affiliated with JONR or Xiaomi.** Use at your own risk;
signing in with your Mi account credentials is required for the map features.

[![Open your Home Assistant instance and open a repository inside the Home
Assistant Community store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=moriceh&repository=jonr-vac&category=integration)

## What it does

- **Local control** (device IP + token): start / pause / stop / return,
  locate, fan speed, water level, cleaning mode, mop route, passes, DND,
  child lock, carpet strategy, consumable life + reset buttons, station
  actions (wash mop / dry mop / dust collection), zone & room cleans, manual
  remote control.
- **Live cloud map**: a floor-plan camera (rendered PNG) plus a vector
  endpoint (`/api/jonr_vac/map/<target>`) for the companion Lovelace card,
  room names, saved-map switching with room/zone presets, MQTT-pushed
  refreshes while Mi Home uploads.
- **Errors & notifications**: robot and dock error codes surface as sensors,
  always-fired [bus events](docs/EVENTS.md) and optional
  [persistent notifications](docs/NOTIFICATIONS.md) with the fix-it text.
- **Schedules**: a cleaning-timers sensor that names the floor and the rooms
  each timer targets, read from the live map.
- **Voice packs**: install any HTTP(S) tar voice pack — including the
  built-in language packs, served straight from the vendor CDN — with md5,
  tar and `audio.conf` validation before the robot is asked to download.

## Documentation

| Doc | Content |
|---|---|
| [Installation](docs/INSTALLATION.md) | HACS and manual install, the two setup paths, OAuth linking, repairs |
| [Entities](docs/ENTITIES.md) | Every entity the integration creates, per platform |
| [Services](docs/SERVICES.md) | The ten actions, their fields and YAML examples |
| [Events](docs/EVENTS.md) | `jonr_vac_error` / `jonr_vac_station_error`, payloads, automation recipes |
| [Notifications](docs/NOTIFICATIONS.md) | Persistent error bubbles, the toggle, phone push |
| [Map card](docs/MAP_CARD.md) | The bundled card, the vector endpoint, map-card predefines |
| [Translations](docs/TRANSLATIONS.md) | Which UI languages are fully translated |

## Supported models

| Model | Status |
|---|---|
| `xtl.vacuum.xm2216` (JONR P20 Pro) | ✅ shipped profile |

Every other brand profile (ijai / dreame / viomi / roidmi / xiaomi) was pruned
from this fork on purpose — if you need them, use the upstream
[xiaomi-vac](https://github.com/letitbe-dull/xiaomi-vac) integration instead.

## Installation

See **[docs/INSTALLATION.md](docs/INSTALLATION.md)**. Short version: install
**JONR Vacuum** from HACS (or copy `custom_components/jonr_vac/` into your
`config/` directory), restart Home Assistant, then add the integration and
pick either **Email & password** (Mi cloud account — required for the live
map) or **Device IP & token** (local-only control, no map).

## Requirements

- Home Assistant **2024.12.0** or newer.
- The vacuum on the same LAN as Home Assistant (local MIoT control).
- A Mi Home account, and the robot already paired in the Mi Home app, for
  the map, map switching and the cloud command channel.
- Runtime dependency [python-miio](https://github.com/rytilahti/python-miio),
  declared in `manifest.json` and pip-installed by Home Assistant — nothing
  to install by hand.

## Services

Ten entity services cover what the Mi Home app exposes beyond plain
start/pause/stop — room cleans, zone cleans, per-room settings, the custom
cleaning order, room renaming, the DND window, manual drive, map refresh and
voice-pack install. Full field reference and examples:
**[docs/SERVICES.md](docs/SERVICES.md)**.

## Automations

Robot and dock errors fire `jonr_vac_error` and `jonr_vac_station_error` on
the Home Assistant event bus regardless of any setting, so automations never
depend on the notification toggle:

```yaml
trigger:
  - trigger: event
    event_type: jonr_vac_error
action:
  - action: notify.mobile_app_your_phone
    data:
      title: "Aspirateur"
      message: "{{ trigger.event.data.error }} ({{ trigger.event.data.code }})"
```

Payload, code table and more recipes: **[docs/EVENTS.md](docs/EVENTS.md)**.

## Credits & license

MIT — see [`LICENSE`](LICENSE) and [`CREDITS.md`](CREDITS.md).

Based on [xiaomi-vac](https://github.com/letitbe-dull/xiaomi-vac) by
letitbe-dull (MIT — [`LICENSE.xiaomi-vac`](LICENSE.xiaomi-vac) carries the
verbatim upstream notice). Map display model studied from
[dreame-vacuum](https://github.com/Tasshack/dreame-vacuum); the vendor's
Mi Home plugin bundle for the command, map and voice-pack formats; runtime
dependency [python-miio](https://github.com/rytilahti/python-miio) (GPL-3.0,
pip-installed only, never vendored).
