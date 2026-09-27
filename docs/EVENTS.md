[← Back to the README](../README.md)

# Events

Robot and dock errors are fired on the Home Assistant event bus the moment
they appear, **always** — independent of the notification toggle, so
automations never silently stop working. (The bubbles on top live in
[Notifications](NOTIFICATIONS.md).)

| Event type | Fired for |
|---|---|
| `jonr_vac_error` | Robot-side faults (prop 1.3-class `fault`) |
| `jonr_vac_station_error` | Dock faults (`station_error`, the 45xx/49xx codes) |

Payload, for both:

```json
{
  "entity_id": "vacuum.your_vacuum",
  "error": "robot_stuck",
  "code": 4012
}
```

`error` is the stable snake_case slug (the same key the error sensors use,
so sensor states and event data match); `code` is the raw MIoT code. A code
the spec doesn't list degrades to the slug `error_<code>`. Events fire on
every **new or changed** code and on an error already present at startup;
returning to healthy clears the bubble but fires no event.

## Listening

Automations editor: **Trigger type → Event**, event type `jonr_vac_error`.
YAML:

```yaml
automation:
  - trigger:
      - trigger: event
        event_type: jonr_vac_error
    action:
      - action: notify.mobile_app_your_phone
        data:
          title: "Vacuum stuck"
          message: >-
            {{ trigger.event.data.error }} (code {{ trigger.event.data.code }})
```

Or watch it live from the CLI:

```yaml
action: script.log_events
# or Developer Tools → Actions → fire an event listener via
# the event stream / logbook
```

## Code table

Robot codes (`jonr_vac_error`):

| Code | Slug | Meaning |
|---|---|---|
| 4001 | `dust_box_missing` | Dust bin not installed |
| 4003 | `roller_brush_entangled` | Roller brush tangled |
| 4004 | `side_brush_entangled` | Side brush tangled |
| 4005 | `drive_wheel_blocked` | Drive wheel blocked |
| 4006 | `drop_sensor_dirty` | Drop sensor dirty |
| 4007 | `laser_obstructed` | LDS laser obstructed |
| 4008 | `laser_radar_blocked` | LDS radar blocked |
| 4009 | `drive_wheel_slipping` | Drive wheel slipping |
| 4011 | `laser_cover_jammed` | LDS cover jammed |
| 4012 | `robot_stuck` | Robot stuck |
| 4013 | `collision_sensor_jammed` | Bumper jammed |
| 4014 | `robot_suspended` | Robot lifted off the floor |
| 4016 | `robot_tilted` | Robot tilted |
| 4017 | `restricted_zone_start` | Started inside a restricted zone |
| 4018 | `carpet_start` | Started on carpet |
| 4020 | `mop_holder_entangled` | Mop holder tangled |
| 4021 | `mop_holder_missing` | Mop holder not installed |
| 4501 | `dust_bag_missing` | Dock dust bag missing |
| 4502 | `water_low` | Clean water low / tank not installed |
| 4503 | `dirty_water_full` | Dirty water tank full |
| 4506 | `mop_slot_full` | Mop slot full |
| 4507 | `mop_slot_full_refill_kit` | Mop slot full (needs refill kit) |
| 4901 | `refill_failed` | Water refill failed |
| 4902 | `drain_failed` | Drain failed |
| 4903 | `solution_low` | Cleaning solution low |

The dock codes (45xx, 49xx) arrive on `jonr_vac_station_error`; robot codes
(40xx) on `jonr_vac_error`.

## Related sensors

The same codes are readable at any time (not just on the edge) from the
`sensor` entities `robot_error`, `station_error`, `robot_message` and
`station_status` — with the raw code and the fix-it text as attributes.
See [Entities](ENTITIES.md).
