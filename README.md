# Bosch Smart Home Camera — Home Assistant Integration

Adds your Bosch Smart Home cameras (CAMERA_EYES outdoor, CAMERA_360 indoor) as fully featured entities in Home Assistant. Includes a custom **Lovelace card** with live streaming, controls, and event info.

> **No official API.** This integration uses the reverse-engineered Bosch Cloud API, discovered via mitmproxy traffic analysis of the official Bosch Smart Camera app.

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

[![hacs][hacsbadge]][hacs]
[![Project Maintenance][maintenance-shield]][user_profile]
[![BuyMeCoffee][buymecoffeebadge]][buymecoffee]

[![Community Forum][forum-shield]][forum]

[releases-shield]: https://img.shields.io/github/release/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant.svg?style=for-the-badge
[releases]: https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/releases
[commits-shield]: https://img.shields.io/github/commit-activity/y/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant.svg?style=for-the-badge
[commits]: https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/commits/main
[license-shield]: https://img.shields.io/github/license/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant.svg?style=for-the-badge
[hacsbadge]: https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge
[hacs]: https://hacs.xyz
[maintenance-shield]: https://img.shields.io/badge/maintainer-%40mosandlt-blue.svg?style=for-the-badge
[user_profile]: https://github.com/mosandlt
[buymecoffeebadge]: https://img.shields.io/badge/buy%20me%20a%20coffee-donate-yellow.svg?style=for-the-badge
[buymecoffee]: https://buymeacoffee.com/mosandlts
[forum-shield]: https://img.shields.io/badge/community-forum-brightgreen.svg?style=for-the-badge
[forum]: https://community.home-assistant.io/

---

## Known Issues

| Issue | Status | Workaround |
|-------|--------|------------|
| **LOCAL stream: first ~30s may show loading** | Under investigation | The camera's H.264 encoder needs ~30s after connection setup. FFmpeg retries automatically — the stream starts once the encoder is ready. If you need instant playback, set `Stream connection type` to `remote` in the integration settings. |
| **Motion sensitivity changes revert after ~1s** | Firmware limitation | The camera's IVA rules engine overwrites cloud-set motion sensitivity via RCP. Not fixable via the API. ([#1](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/1)) |

---

## Disclaimer

**This project is an independent, community-developed integration. It is not affiliated with, endorsed by, or connected to Robert Bosch GmbH. "Bosch" and "Bosch Smart Home" are registered trademarks of Robert Bosch GmbH.**

This integration communicates with a reverse-engineered, undocumented API. Provided **"as is"**, without warranty. Use at your own risk. The API may change or be shut down by Bosch at any time. Reverse engineering was performed solely for interoperability under **§ 69e UrhG** and **EU Directive 2009/24/EC**.

---

## Installation

### HACS (Recommended)

[![Open HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mosandlt&repository=Bosch-Smart-Home-Camera-Tool-HomeAssistant&category=integration)

1. Click the button above, or in HACS: **Integrations → + Explore → search "Bosch Smart Home Camera"**
2. Download the integration
3. Restart Home Assistant
4. Continue with **Setup** below

### Manual Installation

1. Copy `custom_components/bosch_shc_camera/` to your HA config directory:
   ```
   /config/custom_components/bosch_shc_camera/
   ```
2. Copy `bosch-camera-card.js` to `/config/www/bosch-camera-card.js`
3. Restart Home Assistant
4. Continue with **Setup** below

---

## Setup

### Step 1 — Add the Integration

1. Go to **Settings → Integrations → + Add Integration**
2. Search for **"Bosch Smart Home Camera"**
3. The setup wizard shows a **Bosch Login URL** — copy it and open in your browser
4. Log in with your **Bosch SingleKey ID** (same account as the Bosch Smart Camera app)
5. After login, the browser shows a **404 error page** — this is normal and expected
6. Copy the **full URL** from the browser address bar (starts with `https://www.bosch.com/boschcam?code=...`)
7. Go back to HA, click **Submit**, and paste the URL in the next step
8. The integration discovers all your cameras automatically

> **Token renewal is automatic.** The integration uses a refresh token to silently renew the Bearer token in the background — no manual action needed after initial setup.

### Step 2 — Configure Settings

Go to **Settings → Integrations → Bosch Smart Home Camera → Configure**

All settings have descriptions in the UI. Key options:

| Setting | Description | Default |
|---|---|---|
| **FCM Push** | Near-instant (~2s) event detection via Firebase Cloud Messaging | OFF |
| **FCM Push Mode** | `Auto` (iOS → Android → polling), `iOS`, `Android`, or `Polling` | Auto |
| **Alert services (default)** | Fallback notify services; per-step overrides available (text/screenshot/video/system) | empty (disabled) |
| **Save alert snapshots** | Keep event images/videos locally in `/www/bosch_alerts/` | OFF |
| **Event check interval** | How often to poll for events (FCM Push makes this a fallback only) | 300s (5 min) |
| **SMB Upload** | Upload event snapshots + video clips to SMB/CIFS share | OFF |
| **SMB Server** | IP/hostname of SMB share (e.g. `192.168.1.1`) | empty |
| **SMB Share** | Share name (e.g. `cameras`) | empty |
| **SMB Username** | SMB authentication username | empty |
| **SMB Password** | SMB authentication password | empty |
| **SMB Base Path** | Base path on the share (e.g. `bosch_cameras`) | empty |
| **SMB Folder Pattern** | Subfolder pattern: `{year}/{month}` | `{year}/{month}` |
| **SMB File Pattern** | File naming: `{camera}_{date}_{time}_{type}_{id}` | `{camera}_{date}_{time}_{type}_{id}` |
| **Audio default ON** | Audio switch starts ON (stream with sound) or OFF (muted) | ON |
| **Binary sensors** | Motion / Audio alarm binary sensors (ON for 30s after event) | ON |

### Step 3 — Add the Lovelace Card

1. Go to **Settings → Dashboards → ⋮ → Resources → + Add resource**
2. URL: `/local/bosch-camera-card.js` — Type: **JavaScript module**
3. Hard-reload browser (`Ctrl+Shift+R`)
4. Edit dashboard → **+ Add card → Custom: Bosch Camera Card**

```yaml
type: custom:bosch-camera-card
camera_entity: camera.bosch_garten
title: Garten
```

---

## Features

### Entities

| Feature | Entity type | Default |
|---------|-------------|---------|
| Camera snapshot (latest event JPEG) | `camera` | enabled |
| Camera status (ONLINE/OFFLINE) | `sensor` | enabled |
| Last event timestamp | `sensor` | enabled |
| Events today count | `sensor` | enabled |
| WiFi signal strength (%) | `sensor` | enabled |
| Firmware version | `sensor` | enabled |
| Ambient light level (%) | `sensor` | enabled |
| LED dimmer (%) | `sensor` | enabled (cameras with LED) |
| Motion sensitivity | `sensor` | diagnostic |
| Audio alarm state | `sensor` | diagnostic |
| Last event type | `sensor` | enabled |
| Movement events today | `sensor` | enabled |
| Audio events today | `sensor` | enabled |
| Event detection method | `sensor` | diagnostic — `fcm_push` / `polling` / `disabled` |
| Refresh Snapshot | `button` | enabled |
| Live Stream (ON/OFF) | `switch` | enabled |
| Audio (mute/unmute stream) | `switch` | enabled |
| Camera LED light | `switch` | enabled (cameras with LED) |
| Privacy mode | `switch` | enabled |
| Notifications | `switch` | enabled |
| Motion detection | `switch` | disabled by default |
| Record sound | `switch` | disabled by default |
| Auto-follow (360 camera) | `switch` | disabled by default |
| Intercom (two-way audio) | `switch` | disabled by default |
| Pan position (360 camera) | `number` | enabled (±120°) |
| Audio alarm threshold | `number` | disabled by default |
| Speaker level (intercom volume) | `number` | disabled by default (0–100) |
| Stream quality | `select` | Auto / Hoch 30 Mbps / Niedrig 1.9 Mbps (persists across restarts) |
| Stream mode | `select` | Auto (Lokal → Cloud) / Nur Lokal / Nur Cloud |
| Motion sensitivity | `select` | SUPER_HIGH / HIGH / MEDIUM_HIGH / MEDIUM_LOW / LOW / OFF |
| FCM Push mode | `select` | Auto / iOS / Android / Polling |
| Motion detected | `binary_sensor` | disabled by default |
| Audio alarm detected | `binary_sensor` | disabled by default |
| Person detected | `binary_sensor` | disabled by default |
| Unread events count | `sensor` | disabled by default |
| Privacy sound (360 only) | `switch` | enabled (config category) |
| Commissioned status | `sensor` | diagnostic, disabled by default |
| Acoustic alarm (siren, 360 only) | `button` | disabled by default |
| Live stream (30fps H.264 + AAC) | `camera` | via Live Stream switch |
| Timestamp overlay (clock on video) | `switch` | disabled by default |
| Movement notifications | `switch` | disabled by default |
| Person notifications | `switch` | disabled by default |
| Audio notifications | `switch` | disabled by default |
| Trouble notifications | `switch` | disabled by default |
| Camera alarm notifications | `switch` | disabled by default |
| Firmware update status | `update` | enabled — native HA update card |
| Schedule rules count | `sensor` | diagnostic, disabled by default |
| **Alarm Catalog** (RCP 0x0c38) | `sensor` | diagnostic — all alarm types supported by camera firmware (virtual, flame, smoke, glass break, audio, motion, storage) |
| **Motion Zones** (RCP 0x0c00/0x0c0a) | `sensor` | diagnostic — motion detection zone coordinates (normalized x/y for overlay) |
| **TLS Certificate** (RCP 0x0b91) | `sensor` | diagnostic — camera cert expiry date, issuer, key size |
| **Network Services** (RCP 0x0c62) | `sensor` | diagnostic — active services (HTTP, HTTPS, RTSP, SNMP, UPnP, NTP, ONVIF) |
| **IVA Analytics** (RCP 0x0b60) | `sensor` | diagnostic — analytics module inventory (detectors, versions, active state) |

> **RCP diagnostic sensors** are disabled by default. Enable them in entity settings to inspect camera firmware capabilities. Gen2 cameras will automatically expose new alarm types and analytics modules.

> **SHC local API is not needed.** All features work with just the Bosch cloud API.

### Built-in 3-Step Alert System

No automations needed — the integration sends alerts directly:

1. **Instant text:** `📷 Kamera: Bewegung (10:31:56)` — sent immediately
2. **Snapshot image:** `📸 Kamera Snapshot` + JPEG — sent ~5s later
3. **Video clip:** `🎬 Kamera Video (245 KB)` + MP4 — sent ~30-90s later (polls until Bosch uploads the clip)

**Per-step routing** (v6.5.0+): each step can go to different services, multiple recipients at once. Supports Signal, Telegram, iOS/Android Companion App, or any HA notify service.

| Setting | Description | Example |
|---|---|---|
| `Alert services — default fallback` | Used for all steps unless overridden below | `notify.signal_messenger` |
| `System alerts` | Token expiry, disk warnings | `notify.signal_messenger` |
| `Step 1 — text notification` | Instant text on event | `notify.signal_messenger, notify.mobile_app_xxx, notify.mobile_app_pixel9` |
| `Step 2 — snapshot image` | JPEG inline in notification | `notify.signal_messenger, notify.mobile_app_xxx` |
| `Step 3 — video clip` | MP4 attachment | `notify.signal_messenger` |
| `Save alert snapshots` | Keep files locally or delete after sending | OFF |
| `Delete after send` | Cleanup local files after notification sent | ON |

**iOS + Android Companion App** (`mobile_app_*`): snapshot appears directly inside the push notification as an inline image. Files are saved to `/www/bosch_alerts/` (served as `/local/bosch_alerts/`) and auto-deleted within seconds after sending. Signal and others receive a file path attachment instead.

### Mark-as-Read & Last Event Fast-Path

Events are automatically **marked as read** after alert processing or download. This uses `PUT /v11/events/bulk` for batch updates and `PUT /v11/events` (with `{"id": ..., "isRead": true}`) for individual events, keeping the unread count in sync with the Bosch Smart Camera app.

On **startup**, the integration marks all currently unread events as read — clearing any backlog that accumulated while HA was offline.

The integration uses `GET /v11/video_inputs/{id}/last_event` as a **fast-path** to check for new events before fetching the full event list. This reduces unnecessary API calls — the full event list is only fetched when the last event has actually changed.

### FCM Push vs Polling

| | FCM Push (recommended) | Polling (default) |
|---|---|---|
| **Event latency** | ~2-3 seconds | 5 minutes (configurable) |
| **How it works** | Firebase Cloud Messaging push from Bosch cloud | Periodic API polling |
| **Fallback** | Automatic — if FCM goes down, polling continues | Always active |
| **Status sensor** | `sensor.bosch_camera_event_detection` = `fcm_push` | `polling` |

Enable FCM Push in **Settings → Configure → FCM Push**. You can also select the push mode (`Auto`, `iOS`, `Android`, or `Polling`) — `Auto` tries iOS first, then Android, then falls back to polling. The mode can also be changed at runtime via the **FCM Push Mode** select entity.

### SMB/NAS Upload

Upload event snapshots and video clips directly to a SMB/CIFS network share (FRITZ!Box NAS, Synology, any Windows share, etc.). Disabled by default.

**How it works:**
- When an event is detected (via FCM push or polling), the integration downloads the snapshot and video clip
- Files are uploaded to the configured SMB share using the folder and file naming patterns
- Supports any SMB-compatible NAS or router with USB storage (FRITZ!Box, Synology, QNAP, Windows shares)

**Configuration:** Go to **Settings → Integrations → Bosch Smart Home Camera → Configure** and enable **SMB Upload**. Then fill in the server, share, and credentials.

**Folder pattern variables:** `{year}`, `{month}`, `{day}`
**File pattern variables:** `{camera}`, `{date}`, `{time}`, `{type}`, `{id}`

Example file path on NAS:
```
\\192.168.1.1\FRITZ.NAS\Bosch-Kameras\2026\03\Garten_2026-03-25_14-32-05_MOVEMENT_abc123.jpg
\\192.168.1.1\FRITZ.NAS\Bosch-Kameras\2026\03\Garten_2026-03-25_14-32-05_MOVEMENT_abc123.mp4
```

> Requires the `smbprotocol` Python package, which is auto-installed via `manifest.json`.

#### FRITZ!Box NAS Setup

To use your FRITZ!Box as a NAS for camera event storage:

1. **Enable NAS on FRITZ!Box:**
   - Open `http://fritz.box` → **Heimnetz → USB / Speicher → USB-Speicher**
   - Enable **Speicher (NAS) aktiv**
   - Note the share name (default: `FRITZ.NAS`)

2. **Create a FRITZ!Box user with NAS access:**
   - **System → FRITZ!Box-Benutzer → Benutzer hinzufügen**
   - Give the user a username and password
   - Under **Berechtigungen**, enable **Zugang zu NAS-Inhalten**

3. **Configure in Home Assistant:**
   - Go to **Settings → Integrations → Bosch Smart Home Camera → Configure**
   - Enable **SMB Upload**
   - Fill in:

   | Field | Value | Example |
   |-------|-------|---------|
   | SMB Server | FRITZ!Box IP | `192.168.1.1` |
   | SMB Share | NAS share name | `FRITZ.NAS` |
   | SMB Username | FRITZ!Box NAS user | `nas_user` |
   | SMB Password | User password | `your_password` |
   | SMB Base Path | Folder on NAS | `Bosch-Kameras` |
   | SMB Folder Pattern | Subfolder structure | `{year}/{month}` |
   | SMB File Pattern | File naming | `{camera}_{date}_{time}_{type}_{id}` |
   | Retention (days) | Delete files older than N days | `180` (6 months) |
   | Low disk warning (MB) | Alert below this free space | `5120` (5 GB) |

4. **Verify:** After the next camera event, check your NAS at `FRITZ.NAS/Bosch-Kameras/` — snapshots (.jpg) and video clips (.mp4) should appear automatically.

> **Tip:** Works with any SMB-compatible device. For Synology, use the share name from **Control Panel → Shared Folder**. For Windows, use the shared folder name (e.g. `\\PC-NAME\SharedFolder`).

#### Automatic Cleanup (Retention)

Set **Retention period (days)** to automatically delete old files from the NAS. Default: **180 days (6 months)**. Set to `0` to keep files forever.

- Cleanup runs **once per day** in the background
- Deletes `.jpg` and `.mp4` files older than the configured retention period
- Only runs when SMB upload is enabled and configured

#### Low Disk Space Warning

Set **Low disk warning threshold (MB)** to receive an alert when the NAS runs low on storage. Default: **500 MB**.

- Checked **once per hour**
- If free space drops below the threshold, an alert is sent via:
  1. The configured **notify service** (e.g. Signal, mobile app) if set
  2. **HA persistent notification** as fallback (always shown in the sidebar)

### HA Events

The integration fires events for custom automations:
- `bosch_shc_camera_motion` — movement detected
- `bosch_shc_camera_audio_alarm` — audio alarm triggered
- `bosch_shc_camera_person` — person detected

Event data: `camera_name`, `timestamp`, `image_url`, `event_id`, `source` (`fcm_push` / `polling`)

### Ready-to-Use Automations

- [`examples/automation_ios_push_alert.yaml`](examples/automation_ios_push_alert.yaml) — iPhone push (time-sensitive)
- [`examples/automation_signal_alert.yaml`](examples/automation_signal_alert.yaml) — Signal text message
- [`blueprints/bosch_camera_signal_alert.yaml`](blueprints/bosch_camera_signal_alert.yaml) — configurable blueprint

---

## Lovelace Card

> **Card version: v2.2.0**

![Bosch Camera Card Screenshot](card-screenshot.png)

### What the card shows

```
┌──────────────────────────────────┐
│ ● Garten              [streaming]│
│  ┌────────────────────────────┐  │
│  │   Live video / snapshot    │  │
│  │ Last: 2026-03-19 09:32     │  │
│  └────────────────────────────┘  │
│  [ 📸 Snapshot ] [ 📹 Stream ] [ ⛶ ] │
│  [ 🔊 ton / video ] [ 💡 Licht ] [ 🔒 Privat ] │
│  [ 🔔 Benachrichtigungen ]            │
│  [ 🎙 Gegensprechanlage ]             │
│  [ ◀ ] [     ■     ] [ ▶ ]  ← pan    │
│  Qualität: [Auto ▼]                   │
│  ▼ Benachrichtigungs-Typen            │
│  ▼ Erweitert                          │
│  ▼ Diagnose                           │
└──────────────────────────────────┘
```

### Card modes

| Mode | Description |
|------|-------------|
| **Stream OFF** | Snapshot image, auto-refreshed every **60 s** (visible) / **30 min** (background tab). Immediate refresh on tab focus. |
| **Stream ON** | Live **HLS video** (30fps H.264 + AAC-LC). Uses go2rtc and HA's camera stream WS. Audio toggle controls mute/unmute. Loading overlay with status updates during connection. Auto-recovers from stream disconnects. |

### Controls

| Button | Function |
|--------|----------|
| 📸 Snapshot | Force-fetch a fresh image immediately |
| 📹 Live Stream | Toggle stream ON/OFF |
| 🔊 Ton | Toggle audio mute/unmute during live stream |
| 💡 Licht | Toggle camera LED light (outdoor camera) |
| 🔒 Privat | Toggle privacy mode (covers lens) |
| 🔔 Benachrichtigungen | Toggle push notifications |
| 🎙 Gegensprechanlage | Toggle intercom / two-way audio |
| ◀ ▶ Pan | Pan left/right (CAMERA_360 only) |

**Collapsible accordion sections** (auto-hidden when entities not available):
- **Benachrichtigungs-Typen** — per-type notification toggles: movement, person, audio, trouble, camera alarm
- **Erweitert** — timestamp overlay, auto-follow, motion detection, record sound, privacy sound
- **Diagnose** — WiFi signal %, firmware version, ambient light %, movement/audio events today

### Reliability (v1.9.4)

- **Consistent 2 s snapshot intervals** — backend `frame_interval` is 1 s (shorter than the 2 s poll), so every card request always gets a fresh frame. Eliminates the 1 s / 3 s jitter from v1.9.3 and earlier.
- **HLS auto-recovery** — if the live stream drops (e.g. Bosch proxy hash expiry after ~60 s), hls.js errors are handled: soft errors recover automatically, fatal errors trigger a full reconnect after 2 s.
- **Session renewal** — when the proxy hash expires, the backend automatically opens a new connection and the stream continues uninterrupted.
- **"Connecting" badge** (v1.9.5) — amber badge with fast pulse while HLS is negotiating. Clears to blue "streaming" once video plays.
- **Stream uptime counter** (v1.9.5) — badge shows `00:47` / `1:23` while streaming, updating every 2 s. Proves session renewal keeps the stream alive past 60 s.
- **Frame Δt in debug line** (v1.9.5) — shows actual ms between frames (`Δ2003ms`) — live verification that 2 s intervals are consistent.
- **Snap error retry** (v1.9.5) — a failed snap.jpg during streaming triggers one immediate 500 ms retry instead of waiting for the next 2 s timer tick.
- **Connection type badge** (v1.9.6) — shows "LAN" (green) or "Cloud" (gray) in the header while streaming.

### Stream Connection Types

The integration supports three connection modes, configurable in **Settings → Configure → Stream connection type** or at runtime via the **Stream Modus** select entity:

| Mode | Description |
|------|-------------|
| **Auto** (recommended) | Try local LAN first, automatically fall back to Bosch cloud proxy on failure. |
| **Local** | Direct LAN only — no internet required. Faster RTSP, but slower snapshots (~6–10 s). Session auto-renewed every 50 s. |
| **Remote** | Always via Bosch cloud proxy. Faster snapshots (~0.4–1.9 s). Sessions run for up to 60 minutes. Best default when HA is not on the same LAN. |

### Card YAML

```yaml
# Minimal
type: custom:bosch-camera-card
camera_entity: camera.bosch_garten

# Full config
type: custom:bosch-camera-card
camera_entity: camera.bosch_garten
title: Garten
```

All entity IDs are auto-derived from `camera_entity`. Buttons and sections are hidden automatically when entities don't exist.

### Two-camera dashboard

```yaml
type: grid
columns: 2
cards:
  - type: custom:bosch-camera-card
    camera_entity: camera.bosch_garten
    title: Garten
  - type: custom:bosch-camera-card
    camera_entity: camera.bosch_kamera
    title: Kamera
```

---

## Requirements

- Home Assistant 2024.1+
- Python packages: `requests`, `firebase-messaging`, `smbprotocol` (auto-installed via manifest)
- For live video: go2rtc (built into HA) or ffplay/mpv

---

## Version History

| Version | Changes |
|---------|---------|
| **v7.8.0** | **Stream management rewrite:** Uses `Stream.update_source()` (official HA API) to hot-swap RTSP URLs without destroying HLS providers or frontend state. **Pre-warm before FFmpeg:** 5 s wait + RTSP DESCRIBE after PUT /connection, runs before `update_source()` so FFmpeg connects to a ready encoder — eliminates "Invalid data" errors. **Auto-renewal for LOCAL sessions:** Every 50 s: new PUT /connection → new TLS proxy → `update_source()`. The 60 s `maxSessionDuration` for LOCAL is an absolute limit (not inactivity timeout), replacing the old RTSP keepalive which was ineffective. Auto-renew guard prevents duplicate loops per camera. **Timeout fix:** 10 s timeout only covers the PUT /connection HTTP call, not the pre-warm phase. **inst parameter fix:** REMOTE proxy rejects `inst=4` (400 Bad Request); auto-corrected to `inst=2`. LOCAL quality override no longer leaks into REMOTE fallback. **TLS proxy revert:** Persistent proxy (v7.6.0) caused 401 auth errors; proxy now restarts fresh per session. `async_turn_off` properly stops the proxy. **Startup optimization:** SMB upload removed from coordinator tick (was blocking ~90 s checking hundreds of existing files). Events uploaded via FCM push. **Card v2.2.0:** Fixed loading spinner race condition with multiple camera cards. `_awaitingFresh` initialized in constructor. Stream startup overlay extended to 78 s (was 28 s). `_waitForStreamReady` timeout 90 s (was 30 s). |
| **v7.6.0** | **Persistent TLS proxy:** The LOCAL RTSPS proxy server socket now stays alive across session renewals — reuses the same port on stream stop/start cycles, preventing HA's stream worker from losing its cached RTSP URL (which previously caused black screens). **Autoplay fix:** Camera card handles Chrome's autoplay policy correctly — plays muted first when audio is enabled, then attempts unmute with fallback to muted playback instead of leaving the video paused. **Integration unload cleanup:** All TLS proxy server sockets are properly closed on integration unload/reload, preventing leaked threads. **Keepalive tolerance:** RTSP keepalive loop now tolerates up to 3 consecutive failures before stopping, reducing unnecessary reconnections. |
| **v7.5.1** | **Stream stale URL fix:** After ON/OFF cycles or audio toggles, HA's internal stream object was restarting with the old cached proxy port (e.g. "Connection refused" errors). Fixed by clearing `_stream` on the camera entity after each new connection — forces HA to create a fresh Stream that reads the correct new URL from `stream_source()`. |
| **v7.5.0** | **RTSP keepalive — streams now run indefinitely:** Bosch cameras enforce a hard 60-second session timeout at the TCP level, ignoring `maxSessionDuration` in the RTSP URL. Fixed by sending authenticated RTSP `OPTIONS` keepalives every 30 s through the TLS proxy to reset the camera's inactivity timer. Streams now run continuously without the "Invalid data" black screen at ~60 s. Keepalive loop auto-stops cleanly when the stream is turned off or a new session starts (audio toggle, etc.). |
| **v7.4.0** | **TLS proxy hardening:** Proxy is now properly stopped and restarted on stream recycle — eliminates zombie proxy threads that caused "Invalid data" / 401 errors on LOCAL streams. Server socket is explicitly closed on stop (was leaking before). Port reuse via `SO_REUSEADDR` keeps HA's cached stream URL valid across restarts. Debug logging for TLS connections and pre-warm RTSP results. **Privacy mode stops live stream:** Enabling privacy mode now automatically stops any active live stream and cleans up the TLS proxy — previously the stream stayed active with a black screen. |
| **v7.3.1** | **Stream startup fix:** Card now waits for backend to confirm stream is ready before requesting HLS — eliminates "does not support play stream" errors and removes 10-20s of wasted retries. Loading overlay with progressive status messages stays visible until first frame (up to 45s safety timeout). **RCP payload fix:** RCP responses now correctly extract hex payload from XML instead of parsing raw HTTP body — fixes empty/garbled sensor data. Invalid RCP sessions (0x00000000) are rejected immediately. **Motion zone overlay** (opt-in via `show_motion_zones: true`): SVG polygon overlay on camera image from RCP zone coordinates. |
| **v7.3.0** | **RCP Deep Dive — 6 new diagnostic sensors** from camera firmware via RCP protocol: Alarm Catalog (all supported alarm types incl. flame, smoke, glass break), Motion Detection Zones (zone coordinates for overlay), TLS Certificate (expiry date, issuer, key size), Network Services (HTTP, RTSP, ONVIF, etc.), IVA Analytics (analytics module inventory). All diagnostic, disabled by default — enable in entity settings. Gen2 cameras will automatically expose new alarm types and analytics modules. |
| **v7.2.0** | **Parallel camera processing:** Status checks and event fetches for all cameras now run in parallel via `asyncio.gather` — significantly faster coordinator ticks with 2+ cameras. **Local TCP health check:** Quick TCP ping to camera port 443 on LAN (~5 ms) skips the cloud `/commissioned` API call (~200 ms) when the camera is locally reachable. **Smart offline intervals:** Cameras offline for >15 min are checked every 15 min instead of 5 min, reducing unnecessary cloud API calls. **Pre-warm RTSP improved:** Authenticated DESCRIBE with proper Digest auth helper; pre-warm and go2rtc registration now run in parallel. **Audio default configurable:** New `audio_default_on` option in integration settings — controls whether audio starts ON or OFF when a stream begins. Default: ON. **Card v2.0.0:** Stream always uses HLS video (no more snapshot-polling mode). Loading overlay with status updates during stream startup ("Verbindung wird aufgebaut…" → "Kamera wird aufgeweckt…" → "Stream wird gestartet…"). Overlay stays visible until first video frame renders (no more black screen). Audio toggle only controls mute/unmute. Stream uptime counter runs independently via own interval. |
| **v7.1.0** | **10× faster startup:** Slow-tier API calls (WiFi, motion, firmware, etc.) now run in parallel via `asyncio.gather()` instead of sequentially — startup reduced from ~120s to ~20s. Stream source reads from real-time connection data to prevent stale URLs after session renewal. |
| **v7.0.0** | **Local LAN streaming:** Stream mode select entity (Auto / Lokal / Cloud) — direct RTSP on LAN without cloud proxy. Auto-renewal every 50 s for uninterrupted local streams. Connection type badge (LAN/Cloud) in Lovelace card header. **Privacy mode write-lock** (8 s) — privacy, camera light, and notifications switches no longer flip back after toggling. **Video quality persistence** — quality selection survives HA restarts via RestoreEntity. **RCP guard** — skip cloud proxy connection when a local stream is active (prevents the coordinator from killing the LAN session). Card v1.9.6: "ton / video" label, LAN/Cloud badge. Stream connection type configurable in Settings with detailed description (auto/local/remote with speed comparison). |
| **v6.5.3** | Live stream session management: Bosch proxy sessions run for up to 60 minutes (`maxSessionDuration=3600`). The integration now handles session expiry cleanly — the stream stops gracefully and can be restarted with one tap. Stream uptime is shown in the card badge so you always know where you are in the session. Offline camera guard extended: all slow-tier API calls are skipped for offline cameras (consistent with v6.5.2 for remaining endpoints). |
| **v6.5.2** | Skip all slow-tier API calls (WiFi signal, ambient light, motion, audio alarm, firmware, recording options, unread events, privacy sound, commissioned, autofollow, timestamp overlay, notifications, pan position) for offline cameras — endpoints return HTTP 444 anyway, saving ~50–60 wasted API calls per hour per offline camera. The RCP proxy block already had this guard; now the entire slow tier is consistent. |
| **v6.5.1** | Fix camera light and notifications switches flipping back to their previous state immediately after being toggled while the live stream is active. Root cause: the coordinator's next refresh fetched stale cloud data (Bosch API propagation delay ~1–3 s) and overwrote the optimistic state. Fix: 5-second write-lock (`_light_set_at`, `_notif_set_at`) prevents the coordinator from overwriting recently written values. |
| **v6.5.0** | Per-service notification type routing: each alert step (text / screenshot / video) can be sent to different notify services. Dedicated **System alerts** field for token/disk warnings. **iOS + Android Companion App** support: `mobile_app_*` services receive snapshot inline in push notification (image in `/local/bosch_alerts/`); Signal/Telegram/others receive file attachment. Multiple services per step supported simultaneously (e.g. Signal + iPhone + Android at once). Alert files moved to `www/bosch_alerts/` (auto-deleted within seconds if `alert_delete_after_send=True`). Backward compatible: leave type-specific fields empty to use existing default field. |
| **v6.4.6** | Card v1.9.5: "connecting" amber badge while HLS negotiates (was misleading "idle"); frame Δt in debug line shows actual ms between frames (e.g. `Δ2003ms`) — live proof of consistent 2 s intervals; stream uptime counter in badge (`00:47`) proves session renewal keeps stream alive past 60 s; one immediate 500 ms retry on snap.jpg error during streaming instead of waiting for next 2 s timer tick. |
| **v6.4.5** | Fix irregular snapshot intervals (1 s / 3 s gaps): `frame_interval` reduced from 2.0 → 1.0 s when streaming — browser setInterval jitter caused HA to return cached frames on ~50% of polls. Fix live stream ending unexpectedly ("disabled livestream") after ~55 s: proxy hash expiry now triggers automatic connection renewal instead of clearing the session. Card v1.9.4: hls.js error handler added — `NETWORK_ERROR` → `startLoad()`, `MEDIA_ERROR` → `recoverMediaError()`, unrecoverable → auto-reconnect after 2 s. |
| **v6.4.4** | Card v1.9.3: Fix irregular snapshot intervals in streaming mode. Root cause: `_updateImage()` preload + img.src + `_cacheImage` = 3 HTTP requests/tick causing variable frame timing. New `_streamingImageLoad()` uses direct img.src (1 request/tick). `_cacheImage` skipped during streaming (I/O optimization). |
| **v6.4.3** | Card v1.9.2: Fix snapshot streaming stopping after ~30 s. Root cause: timer called `trigger_snapshot` every 2 s → `async_request_refresh()` every 2 s → Bosch API rate limit → entities unavailable → stream switch read as off. Fix: use `_scheduleImageLoad()` during streaming instead of `_triggerFreshSnapshot()`. |
| **v6.4.2** | Proactive background token refresh 5 min before JWT expiry. Always persist bearer token to config entry. 401 retry in privacy/put methods — fixes automation failures after token expiry. |
| **v6.4.1** | Lovelace card v1.9.1: visible refresh spinner on page load (cached image no longer hides loading state), anonymized screenshot for README. |
| **v6.4.0** | **New entities:** Timestamp overlay switch, per-type notification toggles (movement, person, audio, trouble, cameraAlarm), firmware update entity (native HA update card), schedule rules sensor. **New services:** `create_rule` and `delete_rule` for cloud-side schedule rules (CRUD). **Token resilience:** 3x retry with 2 s delay on refresh failure, new refresh token persisted to config entry, alert only after 3 consecutive failures. Config updates that change only data (not credentials) no longer trigger a full HA reload. |
| **v6.3.3** | Fix duplicate alerts: update `_last_event_ids` before scheduling alert to prevent FCM push + polling race condition. |
| **v6.3.2** | Auto-reload after re-auth + improved SMB upload logging. |
| **v6.3.1** | Token refresh resilience: services (`trigger_snapshot`, `open_live_connection`) now register at domain level — available even when token is expired and integration is retrying. One-time token failure alert via configured notify services (Signal, etc.) + HA persistent notification. Auto-resets on successful refresh. |
| **v6.3.0** | SMB retention: auto-delete files older than N days (default 180 / 6 months), runs daily. SMB disk-free check: HA alert when NAS free space falls below threshold (default 500 MB, configurable), falls back to HA persistent notification if no notify service configured. Both settings in Configure → SMB. |
| **v6.2.3** | Fix mark-as-read: wrong field name (`isSeen` → `isRead`) and wrong individual fallback endpoint (`PUT /v11/events/{id}` → `PUT /v11/events`). On startup, all currently unread events are now marked as read (clears backlog in the Bosch app). |
| **v6.2.2** | Replace deprecated `async_timeout` with `asyncio.timeout`, HACS v2, `loggers` field in manifest |
| **v6.2.1** | Status sensor shows ONLINE immediately on startup (force first-tick fetch), `/commissioned` as primary health check with `/ping` fallback, commissioned + firmware attributes on status sensor, WiFi signal unit fix (no invalid device_class) |
| **v6.2.0** | Privacy sound switch (CAMERA_360 only), commissioned diagnostic sensor, direct clip.mp4 download for faster alerts, HTTP 444 error handling (camera offline/unavailable) |
| v6.1.0 | SHC local API as offline fallback for privacy + light (cloud primary ~150ms, SHC ~1100ms), SHC health tracking, motion revert documentation |
| **v6.0.0** | FCM Push mode selection (Auto/iOS/Android/Polling) with iOS-first fallback order, intercom switch (two-way audio, disabled by default), speaker level number entity (disabled by default), SMB/NAS upload for event snapshots + video clips (FRITZ!Box, Synology, etc.), configurable folder/file patterns, person detection binary sensor (disabled by default), acoustic alarm / siren button (CAMERA_360 only, disabled by default), unread events count sensor (disabled by default), `bosch_shc_camera_person` HA event, mark-as-read (events auto-marked after alerts/downloads), last_event fast-path to reduce API calls, bug fixes (FCM auto-start default corrected, events --limit fixed), Lovelace card v1.8.0 with intercom toggle |
| v5.1.3 | Skip video clip polling when status is Unavailable |
| v5.1.2 | Alert files saved to `/media/bosch_alerts/` |
| v5.1.0 | German + English translations, multi-service alerts, video clip poll retry |
| v5.0.0 | FCM push notifications, 3-step alert system, auto-follow switch |
| v4.0.0 | Code cleanup: switch base class, shared aiohttp session, TZ-aware counters |
| v3.1.0 | Snapshot refresh fix (frame_interval decoupled) |
| v3.0.0 | Motion/audio binary sensors, sensitivity select, audio threshold |
| v2.9.0 | Proxy URL caching (50s TTL), background snapshot refresh |
| v2.8.0 | Event-driven snapshot refresh, RCP session caching |
| v2.7.0 | RCP snapshot primary, snap.jpg fallback |
| v2.6.0 | Video quality select entity |
| v2.0.0 | WiFi/firmware/ambient sensors, 3-state notifications, diagnostics |
| v1.8.0 | Live HLS video in Lovelace card |
| v1.7.1 | Cloud proxy snapshots, iOS-style toggles |

---

## Related Projects

- [Bosch Smart Home Camera — Python CLI Tool](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-Python) — standalone CLI with full API access, live stream, RCP protocol, FCM push
- [Bosch Smart Home Camera — Python Frontend (concept)](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-Python-frontend) — planned NiceGUI web dashboard — community interest welcome

---

## License

MIT — see source files.
