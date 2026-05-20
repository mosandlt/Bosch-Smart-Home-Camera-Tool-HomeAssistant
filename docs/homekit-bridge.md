# Apple HomeKit / Apple Home — Setup Guide

Bosch Smart Home cameras can be surfaced in Apple Home via HA's built-in **HomeKit Bridge** integration. This guide covers full setup, YAML configuration, and troubleshooting.

Back to main README: [Apple HomeKit / Apple Home Integration](../README.md#apple-homekit--apple-home-integration)

---

## How it works

HomeKit Bridge is a HA Core component that acts as a HomeKit hub. It translates HA entities into HomeKit accessories over the local network using the HomeKit Accessory Protocol (HAP). No cloud relay, no additional server, and no changes to this integration are required.

The Bosch Smart Home cameras become standard HomeKit camera accessories. Apple Home polls HA's camera proxy for snapshots and HLS segments. Privacy mode synchronisation works via the existing `switch.bosch_*_privacy_mode` entity — HomeKit Bridge maps the switch state to the HomeKit "Camera Streaming" toggle automatically.

---

## Requirements

- Home Assistant 2024.1 or later
- HomeKit Bridge integration (built into HA Core — enable via **Settings → Devices & services → Add Integration**)
- iOS 16+ / iPadOS 16+ / macOS 13+ / tvOS 16+ with the Home app
- HA and Apple devices must be on the same local network segment (mDNS / Bonjour must reach both)

---

## Step-by-step Setup

### Step 1 — Add the HomeKit Bridge integration

Navigate to **Settings → Devices & services → Add Integration** and search for **HomeKit Bridge**.

![Step 1 — Add HomeKit Bridge](screenshots/homekit-step1.png)

Select the integration. HA will open a configuration dialog. When asked which entity domains to expose, tick at minimum:

- `camera` — for the live video stream and snapshot
- `binary_sensor` — to forward motion events as Apple Home motion sensors (optional but recommended)

### Step 2 — Filter to Bosch camera entities

In the entity filter step, restrict exposure to the Bosch cameras you want in Apple Home. Including all cameras by domain works, but an explicit glob keeps the bridge clean:

```
camera.bosch_terrasse
camera.bosch_innenbereich
camera.bosch_kamera
camera.bosch_eingang
```

For motion sensors, add the corresponding binary sensors:

```
binary_sensor.bosch_terrasse_motion
binary_sensor.bosch_innenbereich_motion
binary_sensor.bosch_kamera_motion
binary_sensor.bosch_eingang_motion
```

![Step 2 — Entity filter](screenshots/homekit-step2.png)

### Step 3 — Pair with the Home app

After the integration is added, HA displays a **QR code** and an eight-digit pairing PIN in the HomeKit Bridge integration card.

Open the **Home** app on iOS, tap **+** in the top-right corner, then **Add Accessory**. Point the camera at the QR code. If the QR code is not accessible, enter the PIN manually when prompted.

![Step 3 — Scan QR code](screenshots/homekit-step3.png)

HA will ask you to confirm adding an uncertified accessory — tap **Add Anyway**. This is expected for all software HomeKit bridges.

Assign each camera to a room in Apple Home to finish pairing.

### Step 4 — Verify privacy mode mapping

Open the **Home** app and navigate to one of the Bosch cameras. Tap the camera tile to open its detail view. The **Camera Streaming** toggle (a camera icon) reflects the privacy state:

- Toggle **ON** (streaming enabled) → HA sets `switch.bosch_*_privacy_mode` to `off` (camera active)
- Toggle **OFF** (streaming disabled) → HA sets `switch.bosch_*_privacy_mode` to `on` (shutter closed)

No additional automation or configuration is needed. The mapping is handled automatically by HomeKit Bridge's `CameraActivity` characteristic binding to the HA switch entity.

![Step 4 — Privacy toggle in Home app](screenshots/homekit-step4.png)

---

## Optional: YAML Configuration

If you manage HA configuration via YAML, add the following to `configuration.yaml` instead of using the UI flow. Remove (or comment out) any existing UI-managed HomeKit Bridge entry first to avoid duplicate bridges.

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

After saving, restart HA. A new QR code and PIN are generated. If the bridge was previously paired, you must re-pair (see [Re-pairing](#re-pairing-the-homekit-bridge) below).

---

## Known Limitations

| Feature | Status | Notes |
|---|---|---|
| Live video stream | ✅ | HLS via HomeKit camera stream protocol |
| Snapshot | ✅ | Polled by Apple Home |
| Motion sensor events | ✅ | Forwarded as HomeKit motion-sensor events |
| Privacy mode toggle | ✅ | Maps to `switch.bosch_*_privacy_mode` automatically |
| Two-way audio | ❌ | HA Core HomeKit Bridge does not forward two-way audio for any camera — not specific to Bosch Smart Home cameras |
| Activity / detection zones | ❌ | Requires polygon editor in the card; currently parked |
| Pan controls (360° Indoor) | ❌ | HomeKit camera accessory type has no pan API |
| HomeKit Secure Video (HSV) | ⚠️ | Requires an Apple Home hub (HomePod, Apple TV 4K, iPad) with iCloud+ subscription; HA's bridge streams standard HomeKit video without HSV cloud recording |

---

## Troubleshooting

### Camera does not appear in Apple Home after pairing

1. Verify the HomeKit Bridge integration card shows **Running** in HA.
2. Confirm the camera entities were included in the entity filter (Settings → Devices & services → HomeKit Bridge → Configure).
3. Check that mDNS / Bonjour is not blocked between HA and the Apple device. On VLANs, mDNS forwarding must be enabled on the router.
4. Try removing and re-adding the bridge (see [Re-pairing](#re-pairing-the-homekit-bridge)).

### Stream does not load in Apple Home

1. Open a Bosch camera card in HA and confirm the live stream loads there first — HomeKit Bridge proxies the same HLS stream.
2. If the stream works in HA but not Apple Home, check HA logs for `homekit` errors: **Settings → System → Logs**, filter by `homekit`.
3. Apple Home requires the HA instance to be reachable from the Apple device over the local network. External (remote) access via Cloudflare Tunnel is not used by HomeKit Bridge — HAP runs over the LAN.
4. If the camera is in privacy mode, the stream is intentionally blocked. Toggle privacy off in HA first.

### Motion events not appearing in Apple Home

1. Verify the corresponding `binary_sensor.bosch_*_motion` entity was included in the HomeKit Bridge entity filter.
2. The binary sensor must transition from `off` to `on` to trigger an Apple Home notification. If the sensor is stuck in `on`, Apple Home will not re-fire the notification until the next `off` → `on` edge.
3. Check that notification permissions for the Home app are enabled on the iOS device: **Settings → Home → Notifications**.

### Privacy toggle out of sync

If the Apple Home "Camera Streaming" toggle does not reflect the HA `switch.bosch_*_privacy_mode` state:

1. Force-close and reopen the Home app on iOS.
2. In HA, go to **Developer Tools → States**, find the `switch.bosch_*_privacy_mode` entity, and verify its state updates correctly when you toggle privacy from the Bosch camera card.
3. If still out of sync, reload the HomeKit Bridge integration: **Settings → Devices & services → HomeKit Bridge → Reload**.

### Re-pairing the HomeKit Bridge

HomeKit pairing data is stored in `.storage/homekit.json`. When the bridge configuration changes significantly (new port, new name, YAML config added or removed), you must re-pair:

1. In the **Home** app, long-press the HA Bridge accessory, tap **Remove Accessory**, and confirm.
2. In HA, go to **Settings → Devices & services → HomeKit Bridge → Delete**, then re-add the integration (or restart HA if using YAML config).
3. A new QR code is generated. Pair as described in Step 3.

> If `.storage/homekit.json` still exists after deletion, remove it manually via the HA file editor or SSH before re-adding, or the bridge will attempt to re-use stale pairing data.

---

## Automation Example: Privacy sync across platforms

If you manage privacy mode from both HA and Apple Home, the existing `switch.bosch_*_privacy_mode` entity is the single source of truth. No extra automation is needed — HomeKit Bridge reads and writes the switch state directly. Any change from Apple Home triggers the same HA service call as a manual toggle.

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

This automation works regardless of whether the privacy toggle was last touched in HA, the Bosch app, or Apple Home.
