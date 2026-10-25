# Apple HomeKit / Apple Home — Setup Guide
Bosch SHC cameras surface in Apple Home via HA's built-in HomeKit Bridge integration. Back to README: [Apple HomeKit / Apple Home Integration](../README.md#apple-homekit--apple-home-integration)

## How it works
HomeKit Bridge = HA Core component acting as HomeKit hub; translates HA entities to HomeKit accessories over LAN via HAP. No cloud relay, no extra server, no changes to this integration. Bosch cameras become standard HomeKit camera accessories; Apple Home polls HA camera proxy for snapshots + HLS segments. Privacy sync via existing `switch.bosch_*_privacy_mode`: HomeKit Bridge maps switch state to HomeKit "Camera Streaming" toggle automatically.

## Requirements
- HA 2024.1+
- HomeKit Bridge integration (HA Core built-in; Settings → Devices & services → Add Integration)
- iOS 16+ / iPadOS 16+ / macOS 13+ / tvOS 16+ with Home app
- HA + Apple devices on same LAN segment (mDNS/Bonjour must reach both)

## Setup
### Step 1 — Add HomeKit Bridge
Settings → Devices & services → Add Integration → "HomeKit Bridge".
![Step 1 — Add HomeKit Bridge](screenshots/homekit-step1.png)
When asked for entity domains, tick at minimum: `camera` (live video + snapshot); `binary_sensor` (forwards motion as Apple Home motion sensors; optional, recommended).

### Step 2 — Filter to Bosch camera entities
Restrict exposure to wanted Bosch cameras (all cameras by domain works; explicit list keeps bridge clean):
```
camera.bosch_terrasse
camera.bosch_innenbereich
camera.bosch_kamera
camera.bosch_eingang
```
Motion sensors:
```
binary_sensor.bosch_terrasse_motion
binary_sensor.bosch_innenbereich_motion
binary_sensor.bosch_kamera_motion
binary_sensor.bosch_eingang_motion
```
![Step 2 — Entity filter](screenshots/homekit-step2.png)

### Step 3 — Pair with Home app
HA shows QR code + eight-digit pairing PIN in HomeKit Bridge integration card. iOS Home app → "+" (top right) → Add Accessory → scan QR (or enter PIN manually).
![Step 3 — Scan QR code](screenshots/homekit-step3.png)
Confirm uncertified accessory: "Add Anyway" (expected for all software HomeKit bridges). Assign each camera to a room to finish.

### Step 4 — Verify privacy mode mapping
Home app → Bosch camera tile → detail view. "Camera Streaming" toggle (camera icon) reflects privacy state:
- ON (streaming enabled) → `switch.bosch_*_privacy_mode` = `off` (camera active)
- OFF (streaming disabled) → `switch.bosch_*_privacy_mode` = `on` (shutter closed)
No extra automation/config; handled by HomeKit Bridge's `CameraActivity` characteristic binding to the HA switch.
![Step 4 — Privacy toggle in Home app](screenshots/homekit-step4.png)

## Optional: YAML Configuration
Alternative to UI flow, in `configuration.yaml`. First remove/comment any existing UI-managed HomeKit Bridge entry (avoid duplicate bridges).
```yaml
homekit:
  name: HA Bridge
  port: 21063        # change if port is already in use
  filter:
    include_domains:
      - camera
      - binary_sensor
    include_entity_globs:
      - camera.bosch_*
      - binary_sensor.bosch_*_motion
  entity_config:
    camera.bosch_terrasse:
      name: "Terrasse"
      support_audio: false   # two-way audio is not supported
    camera.bosch_innenbereich:
      name: "Innenbereich"
      support_audio: false
    camera.bosch_kamera:
      name: "360 Kamera"
      support_audio: false
    camera.bosch_eingang:
      name: "Eingang"
      support_audio: false
```
After saving, restart HA; new QR code + PIN generated. If bridge was previously paired: re-pair (see [Re-pairing](#re-pairing-the-homekit-bridge)).

## Known Limitations
| Feature | Status | Notes |
|---|---|---|
| Live video stream | ✅ | HLS via HomeKit camera stream protocol |
| Snapshot | ✅ | Polled by Apple Home |
| Motion sensor events | ✅ | Forwarded as HomeKit motion-sensor events |
| Privacy mode toggle | ✅ | Maps to `switch.bosch_*_privacy_mode` automatically |
| Two-way audio | ❌ | HA Core HomeKit Bridge forwards no two-way audio for any camera — not Bosch-specific |
| Activity / detection zones | ❌ | Needs polygon editor in card; currently parked |
| Pan controls (360° Indoor) | ❌ | HomeKit camera accessory type has no pan API |
| HomeKit Secure Video (HSV) | ✅ | Works when motion sensor linked (`binary_sensor.bosch_*_motion`, auto-included) AND Apple Home hub (HomePod / Apple TV 4K / resident iPad) + iCloud+. Motion clips record to iCloud, appear in Home timeline. Without hub/iCloud+: live view only. |
| Video resolution | 1080p | Bosch SHC cameras native **1920×1080**; no 2K/4K stream exists. Apple's "up to 4K" HSV cap-lift only benefits 2K/4K-sensor cameras; Bosch already delivers full 1080p. |

## Troubleshooting
### Camera not in Apple Home after pairing
1. HomeKit Bridge integration card shows **Running**.
2. Camera entities included in entity filter (Settings → Devices & services → HomeKit Bridge → Configure).
3. mDNS/Bonjour not blocked between HA and Apple device; on VLANs enable mDNS forwarding on router.
4. Remove + re-add bridge (see [Re-pairing](#re-pairing-the-homekit-bridge)).

### Stream does not load in Apple Home
1. Confirm live stream loads in HA Bosch camera card first (HomeKit Bridge proxies same HLS stream).
2. If works in HA but not Apple Home: HA logs (Settings → System → Logs), filter `homekit`.
3. Apple device must reach HA over LAN; remote access via Cloudflare Tunnel not used by HomeKit Bridge (HAP runs over LAN).
4. Camera in privacy mode = stream intentionally blocked; toggle privacy off in HA first.

### Motion events not in Apple Home
1. `binary_sensor.bosch_*_motion` included in HomeKit Bridge entity filter.
2. Sensor must transition `off`→`on` to trigger notification; if stuck `on`, no re-fire until next `off`→`on`.
3. Home app notification permission enabled: iOS Settings → Home → Notifications.

### Privacy toggle out of sync
If Apple Home "Camera Streaming" toggle doesn't reflect `switch.bosch_*_privacy_mode`:
1. Force-close + reopen Home app.
2. HA Developer Tools → States: verify `switch.bosch_*_privacy_mode` updates when toggling privacy from Bosch camera card.
3. Still out of sync: Settings → Devices & services → HomeKit Bridge → Reload.

### Re-pairing the HomeKit Bridge
Pairing data in `.storage/homekit.json`. Re-pair when bridge config changes significantly (new port, new name, YAML config added/removed):
1. Home app: long-press HA Bridge accessory → Remove Accessory → confirm.
2. HA: Settings → Devices & services → HomeKit Bridge → Delete, then re-add integration (or restart HA if YAML config).
3. New QR code generated; pair as Step 3.
> If `.storage/homekit.json` still exists after deletion, remove manually via HA file editor or SSH before re-adding, else bridge reuses stale pairing data.

## Automation Example: Privacy sync across platforms
`switch.bosch_*_privacy_mode` is single source of truth when managing privacy from both HA and Apple Home; no extra automation needed (HomeKit Bridge reads/writes switch directly; Apple Home changes trigger same HA service call as manual toggle).
```yaml
# Example: turn off privacy on all cameras when you arrive home
- alias: "Arrived home — cameras active"
  triggers:
    - platform: zone
      entity_id: person.thomas
      zone: zone.home
      event: enter
  actions:
    - action: switch.turn_off
      target:
        entity_id:
          - switch.bosch_terrasse_privacy_mode
          - switch.bosch_innenbereich_privacy_mode
          - switch.bosch_kamera_privacy_mode
          - switch.bosch_eingang_privacy_mode
```
Works regardless of whether privacy was last toggled in HA, Bosch app, or Apple Home.
