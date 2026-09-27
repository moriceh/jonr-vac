[← Back to the README](../README.md)

# Installation

## Requirements

- Home Assistant **2024.12.0** or newer (`hacs.json` minimum).
- The vacuum reachable on the **same LAN** as Home Assistant — local MIoT
  control talks UDP to the robot's IP. The `xtl` command channel is not
  proxied over the cloud, so a VLAN or firewall that blocks Home Assistant
  from reaching the robot's IP will leave control dead.
- For the map features: a Mi Home account with the robot already paired, and
  the correct Xiaomi cloud **server** region (`cn`, `de`, `us`, `ru`, `tw`,
  `sg`, `in`, `i2`).

## Install with HACS

1. In HACS → **Integrations** → ⋮ → **Add custom repository**, use this
   repository URL with category **Integration**.
2. Install **JONR Vacuum**, then restart Home Assistant.

## Install manually

Copy `custom_components/jonr_vac/` into your Home Assistant `config/`
directory (so the file lands at
`config/custom_components/jonr_vac/manifest.json`), then restart.

## Add the integration

Settings → **Devices & Services** → **Add Integration** → **JONR Vacuum**.
Pick a setup path:

- **Email & password** — sign in with your Mi Home account (the region and,
  if 2FA or a captcha is demanded, those steps follow). This gives local
  control **and** the live map, and is the path to choose if you want the map.
- **Device IP & token** — local-only control with no map. Supply the robot's
  LAN IP and its 32-hex MIoT **token**; get the token from
  [Xiaomi cloud token extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor).

The device is created once; both paths end up controlling the robot locally
over UDP. The cloud session only drives the map pipeline and the cloud
command fallbacks (map switching, room writes).

## Linking MIoT OAuth (live map)

The real-time map is delivered over an MIoT **OAuth** session, separate from
the account login. On a cloud entry that never linked it, Home Assistant
raises a **repairs** issue (*Link Xiaomi OAuth*); open **Fix** and either let
Xiaomi redirect back automatically, or open the shown link, approve, and
paste the code from the redirect address bar (after `CODE=`, before `&`).
Cloud entries added without it keep working for control — only the live map
and its push refreshes wait on this link. A local-only entry has no cloud
session to link, so the OAuth option reports that and stops.

## Integration options

On the integration card, **Configure** opens a single toggle:

- **Notifications when the robot or the dock report an error** — creates
  persistent Home Assistant notification bubbles for robot/station errors.
  Bus [events](EVENTS.md) fire either way; this only gates the bubbles.
  See [Notifications](NOTIFICATIONS.md).

The same Configure step continues into the OAuth link steps when the entry
has a cloud session.

## What to check after setup

- The **vacuum** entity responds to start / pause / stop / return.
- The **map camera** renders a floor plan (cloud entries only).
- The **Active Map** select lists your saved floors and switching them is
  confirmed — see [Map card](MAP_CARD.md).
- Error sensors populate and a provoked error (lift the robot) fires an
  [event](EVENTS.md) and, if enabled, a
  [notification](NOTIFICATIONS.md).
