[← Back to the README](../README.md)

# Translations

Entity names, select options, sensor states (including every error label),
config-flow steps and service descriptions resolve from
`custom_components/jonr_vac/translations/`. Any label Home Assistant cannot
find falls back to English — a missing translation never leaves an entity
nameless.

## Coverage

| Language | Keys | Status |
|---|---|---|
| English (`en`) | 375 / 375 | ✅ complete (source language, mirrored by `strings.json`) |
| French (`fr`) | 375 / 375 | ✅ complete |
| German, Spanish, Italian, Dutch, Polish, Portuguese, Portuguese (BR), Japanese, Russian, Simplified Chinese | 375 / 375 | ✅ complete |
| Korean (`ko`), Vietnamese (`vi`), Traditional Chinese (`zh-Hant`) | 375 / 375 | ✅ complete — error labels/solutions from the plugin, the rest English (see below) |

Every key exists in every shipped language. Where the official Mi Home
plugin ships a label for a string (its `keywordN` multilingual tables cover
de/es/fr/it/ko/pl/ru/vi/zh-Hans/zh-Hant), that vendor wording is used; the
rest (Home Assistant plumbing the plugin never had — OAuth flows, service
descriptions, newer sensors) is translated from the English source.

The three locales the plugin's own language set adds beyond the hand-run
ones (`ko`, `vi`, `zh-Hant`) were generated from the bundle's tables, not
re-translated: the 25 `robot_error` state labels are the plugin's title
keywords verbatim and the notification **solutions** shown in bubbles are
the plugin's solution keywords verbatim (vendor typos included — they are
the vendor's wording and stay diffable against the bundle). Strings the
plugin never shipped (bubble chrome titles, the `none` state, station-only
labels, config flow, services) keep the English text in those files, same
as HA's own fallback would show. HA does not merge a partial language file
over `en.json`, so each of these files is a complete copy of `en.json`
with the plugin-sourced values written in.

Error **solutions** follow the same vendor-language set: `de`, `es`, `it`,
`pl`, `ru`, `zh-Hans`, `zh-Hant`, `ko`, `vi` (+ the hand-polished `en`/`fr`
tables) — anything outside the plugin's set (`ja`, `nl`, `pt`, …) gets
English solutions, like the plugin itself. `zh-Hant/zh-HK/zh-TW` select
Traditional, any other `zh` selects Simplified.

## What translates where

- **Entity names & states** — `entity.<platform>.<key>` including `state`
  maps (the error labels in [Events](EVENTS.md) and the `station_status`
  options come from here — the notification bubbles read the same keys).
- **Services** — the names/descriptions Dev Services shows for
  [Services](SERVICES.md) (`services.<key>.name` / `.fields`).
- **Config & options flow** — setup path labels ([Installation](INSTALLATION.md))
  and the error-notification toggle
  ([Notifications](NOTIFICATIONS.md)).

## Contributing a translation

1. Start from `translations/en.json` (kept byte-identical in key shape to
   `strings.json` — hassfest validates the pairing in CI).
2. Translate the values, keep every key — HA's English fallback makes a
   partial file safe, but a wrong key silently shows nothing to fix.
3. Open a pull request; the labels that appear in
   [Events](EVENTS.md)/[Notifications](NOTIFICATIONS.md) bubbles are covered
   by a test that pins them to the same `robot_error.state` table, so a new
   language needs no test change.
