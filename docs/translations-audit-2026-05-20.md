# Translations Audit — 2026-05-20

## Scope
11 language files in `custom_components/bosch_shc_camera/translations/`:
de, en, es, fr, it, nl, pl, pt, ru, uk, zh-Hans

Source of truth: `strings.json` (427 lines)

## Method
Leaf-key diff (dot-notation flattening) comparing `strings.json` options.step.init.sections and services subtrees against each translation file.

## Findings Before Edits

### de.json + en.json (2 files, 776 lines each)
Had: webhook, ptz, motion_active_window, intrusion_sensitivity/distance, lan_reachable
Missing: `services.send_event_webhook` (7 leaf keys)

### es, fr, it, nl, pl, pt, ru, uk, zh-Hans (9 files, 732 lines each)
Missing across all 9:
- `options.step.init.sections.webhook` (entire section: 6 leaf keys)
- `options.step.init.sections.ptz` (entire section: 2 leaf keys)
- `options.step.init.sections.features.data.motion_active_window`
- `options.step.init.sections.features.data_description.motion_active_window`
- `entity.number.intrusion_sensitivity`
- `entity.number.intrusion_distance`
- `entity.binary_sensor.lan_reachable`
- `services.send_event_webhook` (7 leaf keys)

## Edits Applied

### All 11 files
- Added `services.send_event_webhook` (name, description, fields.entity_id.{name,description}, fields.event_type.{name,description})
- Machine-translated from English source in strings.json
- Proper nouns kept untranslated: Bosch, HTTPS, URL, JSON, PTZ, MOVEMENT, AUDIO_ALARM, PERSON, INTRUSION

### 9 files (es, fr, it, nl, pl, pt, ru, uk, zh-Hans)
- Added `options.step.init.sections.webhook` (name, description, data.enable_webhook_delivery, data.webhook_url, data_description.enable_webhook_delivery, data_description.webhook_url)
- Added `options.step.init.sections.ptz` (name, description, data.enable_ptz_controls, data_description.enable_ptz_controls)
- Added `features.data.motion_active_window` + `features.data_description.motion_active_window`
- Added `entity.number.intrusion_sensitivity` (name only)
- Added `entity.number.intrusion_distance` (name only)
- Added `entity.binary_sensor.lan_reachable` (name only)
- webhook + ptz sections inserted before `auth` section (preserving section order)

## Post-Edit Verification
- All 11 files: `python3 -c "import json; json.load(open('translations/<lang>.json'))"` — all parse clean
- Leaf-key diff: 0 gaps remaining across all 11 files
- options.step.init.sections key order (all 11): polling, features, stream, fcm, events_storage, nvr, webhook, ptz, auth
- strings.json options.sections leaf keys: 116; services leaf keys: 38

## No Changes To
- Existing translation values (only additions)
- Version numbers
- Commits (none created)
- config.step.user / config.step.auth keys in translation files (custom flow, not in strings.json — intentional divergence)
