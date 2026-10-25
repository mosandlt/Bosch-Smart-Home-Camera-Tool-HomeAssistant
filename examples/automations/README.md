# Example Automations
Drop-in YAML snippets for common Bosch camera scenarios. Copy into `automations.yaml` (or UI editor -> three-dot menu -> Edit in YAML), replace placeholder entity IDs. Each file has EN: and DE: explanations side by side. Code portable; only entity IDs need adapting.

## Index
### Motion-light control
| File | Use case | Cam gen |
|---|---|---|
| [`motion-light-helper-switch.yaml`](motion-light-helper-switch.yaml) | Manual "Dinner Mode" toggle in dashboard | Gen2 native, Gen1 via fallback |
| [`motion-light-door-sensor.yaml`](motion-light-door-sensor.yaml) | Door/window opens -> motion light off until 2 min after closing | Gen2 native, Gen1 via fallback |
| [`motion-light-presence.yaml`](motion-light-presence.yaml) | Any presence sensor (mmWave, PIR, BLE, ...) on porch suppresses light | Gen2 native, Gen1 via fallback |
| [`motion-light-time-schedule.yaml`](motion-light-time-schedule.yaml) | Fixed time window OR sunset/sunrise-driven | Gen2 |
| [`motion-light-gen1-instant-off.yaml`](motion-light-gen1-instant-off.yaml) | Gen1 fallback: react to motion event, instantly send light=off | **Gen1 only** |
| [`motion-light-gen1-power-cycle.yaml`](motion-light-gen1-power-cycle.yaml) | Gen1 hardware cut: power camera off via smart plug | **Gen1 only** |
| [`door-privacy-light-production.yaml`](door-privacy-light-production.yaml) | **Production-grade**: door/garage opens -> privacy + (if dark) light. Close -> staged cleanup with day/night-aware delays, defensive re-checks, HA-restart recovery, `mode: restart` | Both |

### Privacy & away mode
| File | Use case |
|---|---|
| [`privacy-when-home.yaml`](privacy-when-home.yaml) | Indoor cams auto privacy-on when anyone is home |
| [`away-mode-arm-cameras.yaml`](away-mode-arm-cameras.yaml) | Arm all cams when last person leaves; disarm when first returns |
| [`person-leaves-precise-cleanup.yaml`](person-leaves-precise-cleanup.yaml) | Single person leaves -> turn off only lights/cams THEY had on (templated `select('is_state','on')` filter; no log spam, no no-op writes) |

### Notifications & integrations
| File | Use case |
|---|---|
| [`snapshot-and-notify-on-motion.yaml`](snapshot-and-notify-on-motion.yaml) | On motion: snapshot + push with image + "View live" action |
| [`weather-suppress-alerts.yaml`](weather-suppress-alerts.yaml) | Skip push during high wind/heavy rain (fewer false positives) |
| [`doorbell-stream-on-tablet.yaml`](doorbell-stream-on-tablet.yaml) | Wall-mounted tablet auto-displays live stream on motion |
| [`sleep-mode-mute-cameras.yaml`](sleep-mode-mute-cameras.yaml) | Quiet at night, but real intruder pattern (3+ events in 5 min) wakes you |
| [`vacation-deterrent-light.yaml`](vacation-deterrent-light.yaml) | Random camera light flashes during vacation (looks lived-in) |
| [`garage-vehicle-coordination.yaml`](garage-vehicle-coordination.yaml) | Garage door + driveway cam = "vehicle arriving/leaving" events, plus optional AI vehicle ID (our car / delivery van / unknown) |
| [`camera-offline-escalating-alert.yaml`](camera-offline-escalating-alert.yaml) | Escalating alert if cam disappears: silent <2 min (Wi-Fi blip), info push 2 min-24 h, critical (DND-bypass) push >24 h. Auto-confirm when cam returns |

### AI & vision
| File | Use case |
|---|---|
| [`ai-vision-smart-alerts.yaml`](ai-vision-smart-alerts.yaml) | Vision LLM (Gemini, GPT-4o, Claude, Ollama) classifies motion: "person", "vehicle", "package", "pet". 4 sub-examples: smart push, package detection, daily summary, visitor greeting via TTS |

### Snapshot scheduler & time-lapse
| File | Use case | Cam gen |
|---|---|---|
| [`snapshot-time-lapse.yaml`](snapshot-time-lapse.yaml) | 4 variants: hourly daytime (workday-aware), motion-triggered 15-min throttle, daily midnight reference, weekly summary push. Saves to `/media/bosch-timelapse/<cam>/`; includes ffmpeg one-liner to assemble mp4 | Both |

## Camera-generation matrix
| Switch | Gen1 (Eyes v1, 360 v1) | Gen2 (Eyes II, Innen II) |
|---|---|---|
| `switch.bosch_<cam>_privacy_mode` | ✅ Persistent | ✅ Persistent |
| `switch.bosch_<cam>_camera_light` | ✅ Manual on/off | ✅ Manual on/off |
| `switch.bosch_<cam>_front_light` | ✅ Front spotlight | (use camera_light) |
| `switch.bosch_<cam>_wallwasher` | ✅ Top+bottom LEDs | ✅ Wallwasher LEDs |
| `switch.bosch_<cam>_licht_bei_bewegung` | ❌ use Gen1 fallbacks | ✅ Native motion-light gate |
| `switch.bosch_<cam>_motion_detection` | ⚠️ State reverts (firmware) | ⚠️ State reverts (firmware) |

"Motion detection revert" = firmware limitation on both gens: on-device IVA engine overrides Cloud-API writes within ~1 s. Use the workaround automations above (privacy mode, downstream filtering, presence sensors) instead of fighting it.

## Placeholders to replace (in every file)
| Placeholder | Replace with |
|---|---|
| `<cam>` | your camera slug (e.g. `terrasse`, `porch`, `eingang`) |
| `<outdoor_cam>` / `<indoor_cam>` | outdoor / indoor camera slug |
| `<doorbell_cam>` | front-door camera slug |
| `binary_sensor.porch_door` | your door/window sensor entity |
| `binary_sensor.porch_presence` | your presence sensor entity |
| `switch.porch_camera_plug` | your smart plug entity |
| `notify.mobile_app_<your_phone>` | HA Companion notify service |
| `person.thomas`, `person.partner`, ... | your household member entities |
| `your_provider_id` (AI files only) | LLM Vision provider ID |

## Combining patterns
Several can run at once. Typical combo: helper switch + door sensor + presence sensor all feeding the same motion-light suppression. Turning an already-off switch off is a no-op.

## Contributing
Bosch-camera-specific automation idea: open a PR.
