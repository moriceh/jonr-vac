[← Back to the README](../README.md)

# Notifications

When the robot or the dock reports an error, the integration can raise a
**persistent notification** (the Home Assistant bell), in addition to the
always-fired [bus events](EVENTS.md):

- **one bubble per error code** — a new code gets its own bubble;
- the bubble carries the error **label and the code**, plus the official
  fix-it text — the Mi Home plugin's guidance, in the language your Home
  Assistant UI runs in (see [Languages](#languages) below);
- and the **official fault picture**, so you see what to check without
  opening Mi Home (see [Error images](#error-images) below);
- it **auto-dismisses** when the error clears — a robot you freed from the
  couch takes its notification with it;
- an error already present when Home Assistant starts is treated as real and
  raises its bubble.

## Error images

Every fault code the official Mi Home plugin illustrates gets its picture in
the bubble — the vendor's own troubleshooting artwork, extracted from the
official Mi Home JONR plugin (`data/1152219162/files/config.json`, its
`faultImages` table served from JONR's CDN) and shipped with the integration
under `www/images/faults/`. The code → picture table lives in
`spec/profiles/xtl.py` (`XTL_ERROR_IMAGES`), transcribed from that config
verbatim so it stays diffable against the plugin; the 4902/4903 pictures are
crossed there, filenames included, exactly as the vendor ships them.

The bubbles reference the files as plain same-origin markdown URLs on the
integration's own static path (`/jonr-vac-card/images/faults/…`) — no base64
in the message, no extra HTTP surface, and the images work whether or not
you are signed in. Codes the plugin table does not cover simply get no
picture; the label and fix-it text still arrive.

## Languages

Both halves of the bubble — the label and the fix-it text — come from the
official Mi Home plugin's own multilingual tables (`main.bundle`'s `errorCodes`
map plus its 11 `keywordN` tables), so they arrive in the plugin's language set:

| Home Assistant language | Label + fix-it text |
|---|---|
| English, French | our wording (polished, same key shape as the vendor's) |
| German, Spanish, Italian, Polish, Russian, Simplified/Traditional Chinese, Korean, Vietnamese | the plugin's vendor wording verbatim |
| anything else (Japanese, Dutch, Portuguese, …) | English — the plugin's own fallback |

The `zh` split follows the tag: `zh-Hant`/`zh-HK`/`zh-TW` get Traditional,
any other `zh` gets Simplified. The tables live in
`spec/profiles/xtl.py` (`XTL_ERROR_SOLUTIONS`, one dict per language, 25
codes each) and the labels mirror `translations/<lang>.json`'s
`robot_error.state` table — see [Translations](TRANSLATIONS.md).

The bubble's **title** is integration chrome the plugin never had, so it
stays French on a French install and English everywhere else; the content
below it is localized.

## The toggle

**Devices & Services → JONR Vacuum → Configure**:

> **Notifications when the robot or the dock report an error**

On by default. Turning it off stops the bubbles — the
[`jonr_vac_error` / `jonr_vac_station_error` events](EVENTS.md) keep firing,
so automations are unaffected. (Same split as dreame-vacuum's notification
option.)

## Push to your phone

Persistent notifications are a UI surface — for the phone, automate on the
events, which are the stable contract. With the
[Home Assistant Companion app](https://www.home-assistant.io/integrations/mobile_app/)
installed and notify enabled:

```yaml
automation:
  - trigger:
      - trigger: event
        event_type: jonr_vac_error
      - trigger: event
        event_type: jonr_vac_station_error
    vars:
      sensor_id: >-
        {{ 'sensor.YOUR_STATION_ERROR_SENSOR'
           if trigger.event.event_type == 'jonr_vac_station_error'
           else 'sensor.YOUR_ROBOT_ERROR_SENSOR' }}
      label: "{{ state_attr(sensor_id, 'label') or '' }}"
      solution: "{{ state_attr(sensor_id, 'solution') or '' }}"
      image: "{{ state_attr(sensor_id, 'image') or '' }}"
    action:
      - action: notify.mobile_app_YOUR_PHONE
        data:
          title: "Vacuum: {{ trigger.event.data.error }}"
          # label is the plugin's human fault text in your UI language —
          # the same string the bubble shows. The event slug (water_low)
          # and the numeric code stay out of the push body.
          message: >-
            {{ label if label else
               (trigger.event.data.error | replace('_', ' ')) }}
            {% if solution %}{{ '\n' }}{{ solution }}{% endif %}
          data:
            tag: "jonr_{{ trigger.event.data.code }}"
            ttl: 0
            # The sensors' image attribute carries the official fault picture
            # file name (see Error images above). The companion app resolves
            # a RELATIVE image URL against the HA instance that sent the
            # notification (local and over remote UI alike) — which is why
            # the sensors publish a bare file name rather than a baked-in
            # host. Empty string = no picture on codes the plugin has no
            # drawing for.
            image: >-
              {{ '/jonr-vac-card/images/faults/' ~ image if image else '' }}
```

A stable `tag` plus `ttl: 0` makes a repeated error replace its own
notification instead of stacking. Reading the fault text, fix-it line and
picture from the sensors' attributes (`label`, `solution`, `image`) rather
than the event payload keeps the push in step with the bubble: same tables,
same languages, one source of truth.

The bubble label tables and this payload come from the same translation
keys, so the automation and the UI never disagree — see
[Translations](TRANSLATIONS.md).
