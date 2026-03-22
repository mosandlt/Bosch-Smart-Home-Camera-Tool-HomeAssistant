# Bosch Smart Home Camera — Home Assistant Custom Integration

Adds your Bosch Smart Home cameras (CAMERA_EYES, CAMERA_360) as fully featured entities in Home Assistant.
Includes a custom **Lovelace card** with live streaming state, controls, and event info.

> **No official API support exists.** This integration uses the reverse-engineered Bosch Cloud API,
> discovered via mitmproxy traffic analysis of the official Bosch Smart Home Camera iOS/Android app.

---

## Disclaimer

**This project is an independent, community-developed integration. It is not affiliated
with, endorsed by, sponsored by, or in any way officially connected to Robert Bosch
GmbH, Bosch Smart Home GmbH, or any of their subsidiaries or affiliates.
"Bosch", "Bosch Smart Home", and related names and logos are registered trademarks
of Robert Bosch GmbH.**

This integration communicates with a reverse-engineered, undocumented, and unofficial
API. The author(s) provide this software **"as is", without warranty of any kind**,
express or implied.

**By using this software, you agree that:**
- You use it entirely **at your own risk**.
- The API may be changed, restricted, or shut down by Bosch at any time without notice.
- Reverse engineering was performed solely for interoperability under **§ 69e UrhG** and **EU Directive 2009/24/EC**.

---

## Features

| Feature | Entity type | Default |
|---------|-------------|---------|
| Latest snapshot per camera | `camera` | enabled |
| Camera status (ONLINE/OFFLINE) | `sensor` | enabled |
| Last event timestamp | `sensor` | enabled |
| Events today count | `sensor` | enabled |
| WiFi signal strength (%) | `sensor` | enabled |
| Firmware version | `sensor` | enabled |
| Ambient light level (%) | `sensor` | enabled |
| LED dimmer value (%) | `sensor` | enabled (cameras with LED only, via RCP) |
| Motion sensitivity | `sensor` | enabled (diagnostic) |
| Audio alarm state | `sensor` | enabled (diagnostic) |
| Last event type | `sensor` | enabled |
| Movement events today | `sensor` | enabled |
| Audio events today | `sensor` | enabled |
| Refresh Snapshot button | `button` | enabled |
| Live Stream switch (ON/OFF) | `switch` | enabled |
| Audio switch (muted by default) | `switch` | enabled |
| Camera LED light switch | `switch` | enabled (cloud API — no SHC needed) |
| Privacy mode switch | `switch` | enabled (cloud API — no SHC needed) |
| Notifications switch | `switch` | enabled (ON = FOLLOW_CAMERA_SCHEDULE or ON_CAMERA_SCHEDULE, OFF = ALWAYS_OFF) |
| Motion detection switch | `switch` | enabled (cloud API — no SHC needed) |
| Record sound switch | `switch` | enabled (cloud API — no SHC needed) |
| Pan position (360 camera) | `number` | enabled (−120° to +120°, auto-detected for CAMERA_360) |
| Stream quality | `select` | enabled (Auto / Hoch 30 Mbps / Niedrig 1.9 Mbps) |
| Auto-download events to folder | background | optional (disabled by default) |
| **Live stream — 30fps H.264 + optional AAC audio** | `camera` | via Live Stream switch |
| Live snapshot (current image, ~1.5s) | `camera` | via snap.jpg proxy |
| **Custom Lovelace card** | `bosch-camera-card` | separate JS file |

**Camera state** — `camera.bosch_garten` shows:
- `idle` — no live stream active
- `streaming` — live proxy connection is open (switch ON)

All features are individually toggleable in **Settings → Integrations → Bosch Smart Home Camera → Configure**.

> **SHC local API is not needed.** All features — camera snapshots, live stream, privacy mode, camera LED light, notifications, and pan control — work with just a Bosch Bearer token via the cloud API. Privacy mode uses `PUT /v11/video_inputs/{id}/privacy`, light uses `PUT /v11/video_inputs/{id}/lighting_override`, notifications use `PUT /v11/video_inputs/{id}/enable_notifications`, and pan uses `PUT /v11/video_inputs/{id}/pan`.

---

## Installation

### Integration (custom component)

#### HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mosandlt&repository=Bosch-Smart-Home-Camera-Tool-HomeAssistant&category=integration)

1. Click the button above, or in HACS go to **Integrations → + Explore & Download Repositories** and search for **"Bosch Smart Home Camera"**
2. Download the integration
3. Restart Home Assistant
4. Go to **Settings → Integrations → + Add Integration** and search for **"Bosch Smart Home Camera"**

#### Manual

1. Copy the `bosch_shc_camera/` folder into `/config/custom_components/`:

```
/config/
  custom_components/
    bosch_shc_camera/
      __init__.py
      camera.py
      sensor.py
      button.py
      switch.py
      config_flow.py
      manifest.json
      strings.json
      services.yaml
      brand/
        icon.png
        icon@2x.png
        dark_icon.png
        dark_icon@2x.png
```

2. Restart Home Assistant
3. Go to **Settings → Integrations → + Add Integration → "Bosch Smart Home Camera"**

---

## Custom Lovelace Card — Bosch Camera Card (v1.5.9)

A dedicated Lovelace card showing the camera feed with streaming state, status, event info, and controls.

**v1.5.9 additions:** pan ◀■▶ controls for the 360 camera (Kamera), and a **Benachrichtigungen** (notifications) toggle button.

> **Integration version:** v2.11.0 — instant toggle feedback (privacy/light/notifications no longer revert), fresh snapshot after disabling privacy mode. Card v1.7.4: spinner gone on first load, smaller images on mobile.

## What's New in v2.11.0

- **Privacy/light/notifications toggle no longer reverts:** After toggling any switch via the cloud API, the new state is now pushed to HA immediately via `async_update_listeners()` — no more waiting for the next 22 s coordinator tick. The card's optimistic state (10 s) was expiring before the coordinator updated, causing the toggle to visually snap back.
- **Fresh snapshot after disabling privacy:** When privacy mode is turned OFF, a snapshot refresh is automatically triggered after 1.5 s, so the card shows the live image immediately instead of the privacy placeholder.
- **Same fix applied to:** camera LED light and notifications switches (all cloud API paths).

## What's New in v2.10.0 + Card v1.7.4

**Card v1.7.4 — faster initial load & mobile-appropriate images**
- **Spinner on first load fixed:** The card rendered before HA assigned `hass` (standard HA lifecycle). `_scheduleImageLoad(0)` fired in `_render()` before `_hass` was set, so the image request silently returned early and the spinner showed for 60 s until the first periodic timer fired. Fixed: `set hass(hass)` now re-triggers image load on first assignment.
- **Smaller images on mobile:** All snapshot requests now pass `?width=<display_width>` to the HA camera proxy. HA passes this to `async_camera_image(width, height)` and the integration chooses the 320×180 RCP thumbnail (3 KB) instead of the 1080p snap.jpg (150 KB) when `width ≤ 640`. Desktop still gets full resolution.
- **Snapshot button first poll:** 1000 ms → 500 ms.

## What's New in v2.9.0

- **Background snapshot refresh:** `async_camera_image` now returns the cached image **instantly** when the cache is stale, then kicks off a background refresh task. All card refresh cycles (every 60 s) respond in **< 90 ms** regardless of cache age. Only the very first load (no cached image yet) waits synchronously (~3 s). This eliminates the visible 3–4 s freeze on every card refresh.
- **Proxy URL caching:** The `PUT /connection` call (~1.5 s) that opens a temporary proxy lease is now cached per camera for 50 s. Skips the PUT call entirely on warm refreshes. On cache miss (first load, expired lease, or quality change) the lease is re-fetched automatically. Combined with RCP session caching (5-min TTL from v2.8.0), background refreshes complete in ~0.5 s.
- **Quality change invalidation:** When the stream quality entity changes value, the proxy URL cache is immediately cleared so the next fetch uses a fresh `PUT /connection` with the updated `highQualityVideo` flag.

## What's New in v2.8.0 + Card v1.7.0

- **Event-driven snapshot refresh (HA integration):** When a new event is detected (newest event ID changes), the integration immediately triggers a snapshot refresh on the camera entity — no need to wait for the next coordinator tick. The card will update within a few seconds of motion being detected.
- **Card event detection:** The card now watches `sensor.last_event` state changes directly. When a new event arrives, it auto-refreshes the image after 2.5 s (giving the HA integration time to fetch a fresh snap). Works in parallel with the server-side trigger for double-redundant coverage.
- **RCP session caching (HA integration + Python CLI):** The 2-step RCP handshake (0xff0c + 0xff0d) is now cached per proxy hash for 5 minutes. All RCP calls (`_async_update_rcp_data`, `_async_rcp_thumbnail`, `async_fetch_live_snapshot`) reuse the cached session ID, reducing overhead by 2 round-trips per call.

## What's New in v2.7.0 + Card v1.6.0

- **Smarter idle snapshot (HA integration):** When fetching a background thumbnail, the integration now tries RCP command `0x099e` first — returning a 320×180 JPEG directly from the camera via the cloud proxy. This is faster and lower bandwidth than the previous snap.jpg approach. Falls back to snap.jpg if RCP is unavailable. Resolution confirmed via RCP `0x0a88` (reads `320×180`).
- **Page Visibility API (Card):** The card now uses the browser's `visibilitychange` event to adjust the snapshot refresh interval automatically:
  - **60 s** when the HA dashboard tab is **visible** (was configurable, default 300 s)
  - **1800 s (30 min)** when the tab is **in the background** — saves bandwidth and CPU
  - **Immediate refresh** when the tab comes back to the foreground
  - Removes the `refresh_interval_idle` card YAML option (no longer needed)

## What's New in v2.6.0

- **Stream Quality select entity** (`select.bosch_<cam>_video_quality`) — choose between Auto (inst=2, ~7.5 Mbps), Hoch (inst=1, ~30 Mbps), and Niedrig (inst=4, ~1.9 Mbps) at runtime.
- Quality preference is applied to all `PUT /connection` calls: live stream, background snapshot, and RCP proxy.
- When the Live Stream switch is ON and quality changes, the stream reconnects automatically.

> **Previous version:** v2.4.0 — motion detection sensor & switch, audio alarm sensor, last event type sensor, movement/audio event counters, record sound switch.

## What's New in v2.4.0

- **Motion Sensitivity sensor** (`sensor.bosch_<cam>_motion_sensitivity`): shows motion detection enabled state and sensitivity level; attributes: `enabled`, `sensitivity`.
- **Audio Alarm sensor** (`sensor.bosch_<cam>_audio_alarm`): shows audio alarm enabled/disabled state and detection threshold; attributes: `enabled`, `threshold`.
- **Last Event Type sensor** (`sensor.bosch_<cam>_last_event_type`): shows the type of the most recent event (`movement` / `audio alarm`); attributes: `event_type`, `timestamp`, `event_id`.
- **Movement Events Today sensor** (`sensor.bosch_<cam>_movement_events_today`): count of MOVEMENT events today.
- **Audio Events Today sensor** (`sensor.bosch_<cam>_audio_events_today`): count of AUDIO_ALARM events today.
- **Motion Detection switch** (`switch.bosch_<cam>_motion_detection`): toggle motion detection on/off via cloud API; preserves existing sensitivity level.
- **Record Sound switch** (`switch.bosch_<cam>_record_sound`): toggle audio recording in cloud event clips on/off via cloud API.

> **Previous version:** v2.3.0 — maxSessionDuration fix (60→3600), `high_quality_video` config option, RCP YUV422 fallback snapshot, `stream_url` camera attribute, bitrate ladder on WiFi sensor.

![Bosch Camera Card](card-screenshot.png)

### What the card shows

```
┌──────────────────────────────────┐
│ ● Garten              [streaming]│  ← status dot + stream badge
│  ┌────────────────────────────┐  │
│  │   Live video / snapshot    │  │  ← HLS video (Stream+Ton ON) or snapshot polling
│  │ Last: 2026-03-19 09:32  5 events │
│  └────────────────────────────┘  │
│  Status: ONLINE  Last event: …   │
│  [ 📸 Snapshot ] [ 📹 Live Stream ] [ ⛶ ] │
│  [  🔊 Ton  ] [  💡 Licht  ] [  🔒 Privat  ] │
│  [  🔔 Benachrichtigungen  ]                 │
│  [ ◀ ] [     ■     ] [ ▶ ]  ← pan (360 only)│
└──────────────────────────────────┘
```

- **Status dot** — green = ONLINE, red = OFFLINE, grey = unknown
- **Stream badge** — `idle` (grey) or `streaming` (blue, pulsing dot)
- **Camera image / live video:**
  - **Stream OFF** → snapshot image, auto-refreshed every **5 minutes** (configurable)
  - **Stream ON + Ton OFF** → snapshot polling every **2 seconds** (near-real-time, no audio)
  - **Stream ON + Ton ON** → **live HLS video with audio** — 30fps H.264 + AAC. Chrome/Firefox use hls.js; Safari/iOS use native HLS
  - First load instantly shows the **last cached image** from localStorage (persists across iOS app restarts)
  - Retries up to 5× on first load if the backend is still starting up
- **Snapshot button** — triggers a live image refresh; polls for the new image and displays it automatically
- **Live Stream button** — toggles `switch.bosch_garten_live_stream`; UI updates instantly (optimistic state)
- **Fullscreen button** — native fullscreen on desktop/Android; CSS overlay fallback on iOS Safari
- **Ton** — toggles `switch.bosch_garten_audio`; when stream is active, switches between snapshot polling (OFF) and live HLS video with audio (ON)
- **Licht** — toggles `switch.bosch_garten_camera_light` (camera LED override via **Bosch cloud API** — no SHC needed)
- **Privat** — toggles `switch.bosch_garten_privacy_mode` (privacy mode via **Bosch cloud API** — no SHC needed); when ON, shows a "Privat-Modus aktiv" placeholder; card fetches a fresh image automatically when turned OFF
- **Benachrichtigungen** — toggles `switch.bosch_garten_notifications` (push notifications ON = FOLLOW_CAMERA_SCHEDULE, OFF = ALWAYS_OFF)
- **Pan controls** (◀■▶) — shown only for CAMERA_360 (Kamera); pans left/center/right via `number.bosch_kamera_pan`

### Installation

1. **Copy the card file** to your HA `www` folder:
   ```
   /config/www/bosch-camera-card.js
   ```

2. **Register the resource** in HA:
   - Go to **Settings → Dashboards → ⋮ (three dots) → Resources**
   - Click **+ Add resource**
   - URL: `/local/bosch-camera-card.js`
   - Type: **JavaScript module**
   - Click **Create**

3. **Reload the browser** (hard refresh: `Ctrl+Shift+R` / `Cmd+Shift+R`)

4. **Add the card** to your dashboard:
   - Edit dashboard → **+ Add card** → search for **"Custom: Bosch Camera Card"**
   - Or paste the YAML directly (see below)

### Card YAML configuration

**Minimal (entity IDs auto-derived):**
```yaml
type: custom:bosch-camera-card
camera_entity: camera.bosch_garten
```

**Full config with all options:**
```yaml
type: custom:bosch-camera-card
camera_entity: camera.bosch_garten
title: Garten                          # optional — overrides entity friendly name
refresh_interval_streaming: 2          # seconds between snapshots when stream ON + Ton OFF (default: 2)
# idle refresh is automatic: 60 s when tab visible, 1800 s in background (Page Visibility API)
```

> **`refresh_interval_streaming`** only applies when the Live Stream switch is ON and **Ton is OFF** (snapshot polling mode).
> When Ton is ON, the card shows live HLS video — no snapshot polling.
> **Idle refresh** is now automatic via the Page Visibility API: 60 s when the tab is visible, 1800 s in the background. The old `refresh_interval_idle` option is removed.

**With explicit entity IDs** (if auto-derived names don't match):
```yaml
type: custom:bosch-camera-card
camera_entity: camera.bosch_garten
switch_entity: switch.bosch_garten_live_stream
audio_entity: switch.bosch_garten_audio
light_entity: switch.bosch_garten_camera_light
privacy_entity: switch.bosch_garten_privacy_mode
status_entity: sensor.bosch_garten_status
events_today_entity: sensor.bosch_garten_events_today
last_event_entity: sensor.bosch_garten_last_event
```

### Entity ID derivation

The card automatically derives all entity IDs from `camera_entity`:

| Config | Derived entity |
|--------|---------------|
| `camera_entity: camera.bosch_garten` | — |
| *(auto)* | `switch.bosch_garten_live_stream` |
| *(auto)* | `switch.bosch_garten_audio` |
| *(auto)* | `switch.bosch_garten_camera_light` |
| *(auto)* | `switch.bosch_garten_privacy_mode` |
| *(auto)* | `switch.bosch_garten_notifications` |
| *(auto)* | `number.bosch_garten_pan` (CAMERA_360 only) |
| *(auto)* | `sensor.bosch_garten_status` |
| *(auto)* | `sensor.bosch_garten_events_today` |
| *(auto)* | `sensor.bosch_garten_last_event` |

Toggle button visibility rules:
- **Entity doesn't exist** (e.g. SHC not configured) → button is **hidden**
- **Entity is `unavailable` / `unknown`** → button shown but **dimmed and disabled**
- **Entity is `on` / `off`** → button shown, highlighted when ON

All buttons use the cloud API — no SHC required. **Licht** is shown only if the camera reports `featureSupport.light = true`. **Pan controls** are shown only for CAMERA_360 cameras. **Benachrichtigungen** is always shown.

For camera named **Kamera**: use `camera_entity: camera.bosch_kamera`.

### Two-camera dashboard example

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

### Diagnostic cards (v2.0.0)

The Camera view (`/lovelace/camera`) includes two additional diagnostic sections showing the new sensor data for each camera.

**Garten diagnostics card:**
```yaml
type: entities
title: Garten Diagnose
show_header_toggle: false
entities:
  - entity: sensor.bosch_garten_wifi_signal
    name: WLAN-Signal
  - entity: sensor.bosch_garten_firmware_version
    name: Firmware
  - entity: sensor.bosch_garten_ambient_light
    name: Umgebungslicht
  - entity: sensor.bosch_garten_status
    name: Status
  - entity: sensor.bosch_garten_events_today
    name: Ereignisse heute
  - entity: sensor.bosch_garten_last_event
    name: Letztes Ereignis
```

**Kamera diagnostics card:**
```yaml
type: entities
title: Kamera Diagnose
show_header_toggle: false
entities:
  - entity: sensor.bosch_kamera_wifi_signal
    name: WLAN-Signal
  - entity: sensor.bosch_kamera_firmware_version
    name: Firmware
  - entity: sensor.bosch_kamera_ambient_light
    name: Umgebungslicht
  - entity: sensor.bosch_kamera_status
    name: Status
  - entity: sensor.bosch_kamera_events_today
    name: Ereignisse heute
  - entity: sensor.bosch_kamera_last_event
    name: Letztes Ereignis
```

To add these manually: edit the Camera view in the Lovelace UI → **+ Add section** → paste the YAML above into a new **Entities** card.

---

## Authentication

The integration uses **OAuth2 PKCE** with your Bosch SingleKey ID.

**Setup flow:**
1. A login URL is shown — open it in your browser
2. Log in with your Bosch SingleKey ID
3. Your browser shows a **404 page** — this is expected
4. Copy the full URL from the address bar (starts with `https://www.bosch.com/boschcam?code=...`)
5. Paste it back into the integration dialog

After first login, the integration saves a **refresh token** and renews the access token silently. No daily action needed.

If the refresh token expires: **Settings → Integrations → Bosch Smart Home Camera → Configure → Force new browser login**.

---

## Options

Go to **Settings → Integrations → Bosch Smart Home Camera → Configure**:

| Option | Description | Default |
|--------|-------------|---------|
| Coordinator tick interval | How often the integration wakes up (seconds) | 60 |
| Camera status check interval | How often to ping ONLINE/OFFLINE (seconds) | 300 |
| Events fetch interval | How often to check for new motion events (seconds) | 300 |
| Enable snapshots | Show camera entities | ✅ |
| Enable sensors | Show status / last event / events-today sensors | ✅ |
| Enable buttons | Show Refresh Snapshot button + Live Stream switch | ✅ |
| Auto-download events | Download all event JPEGs and MP4 clips to a local folder | ❌ |
| Download path | Local path for auto-downloaded events | — |
| Force new browser login | Re-run OAuth2 login if refresh token expired | — |

---

## Entities

For each discovered camera (example: camera named "Garten"):

| Entity ID | Type | Description |
|-----------|------|-------------|
| `camera.bosch_garten` | camera | Latest snapshot — state: `idle` / `streaming` |
| `sensor.bosch_garten_status` | sensor | ONLINE / OFFLINE |
| `sensor.bosch_garten_last_event` | sensor | Timestamp of latest motion event |
| `sensor.bosch_garten_events_today` | sensor | Number of events today |
| `sensor.bosch_garten_wifi_signal` | sensor | WiFi signal strength in %; attributes: ssid, ip_address, mac_address, lan_ip_rcp |
| `sensor.bosch_garten_firmware_version` | sensor | Firmware version string; attributes: up_to_date, hardware_version, product_name_rcp |
| `sensor.bosch_garten_ambient_light` | sensor | Ambient light level 0–100% (from on-camera light sensor) |
| `sensor.bosch_garten_led_dimmer` | sensor | LED dimmer value 0–100% via RCP (cameras with LED only) |
| `sensor.bosch_garten_clock_offset` | sensor | Camera clock offset vs HA server in seconds; attributes: offset_seconds, status (in_sync/minor_drift/out_of_sync) |
| `sensor.bosch_garten_motion_sensitivity` | sensor | Motion detection status and sensitivity level; attributes: enabled, sensitivity |
| `sensor.bosch_garten_audio_alarm` | sensor | Audio alarm status (enabled/disabled) and threshold; attributes: enabled, threshold |
| `sensor.bosch_garten_last_event_type` | sensor | Type of most recent event (movement / audio alarm / none); attributes: event_type, timestamp, event_id |
| `sensor.bosch_garten_movement_events_today` | sensor | Number of MOVEMENT events today |
| `sensor.bosch_garten_audio_events_today` | sensor | Number of AUDIO_ALARM events today |
| `button.bosch_garten_refresh_snapshot` | button | Force immediate data refresh |
| `switch.bosch_garten_live_stream` | switch | Live stream ON/OFF |
| `switch.bosch_garten_audio` | switch | Audio ON/OFF in live stream (default: OFF) |
| `switch.bosch_garten_camera_light` | switch | Camera LED indicator ON/OFF — cloud API, no SHC needed |
| `switch.bosch_garten_privacy_mode` | switch | Privacy mode ON/OFF — cloud API, no SHC needed |
| `switch.bosch_garten_notifications` | switch | Push notifications ON (FOLLOW_CAMERA_SCHEDULE / ON_CAMERA_SCHEDULE) / OFF (ALWAYS_OFF) |
| `switch.bosch_garten_motion_detection` | switch | Motion detection ON/OFF — cloud API, preserves sensitivity level |
| `switch.bosch_garten_record_sound` | switch | Audio in cloud event recordings ON/OFF — cloud API |
| `number.bosch_kamera_pan` | number | Pan position −120° to +120° (CAMERA_360 only, auto-detected) |

### Camera streaming state

`camera.bosch_garten` follows the standard HA camera state machine:

| State | When | `streaming_state` attribute |
|-------|------|-----------------------------|
| `idle` | Live stream switch is OFF | `idle` |
| `streaming` | Live stream switch is ON, proxy session active | `active` |

Use this in automations:
```yaml
trigger:
  - platform: state
    entity_id: camera.bosch_garten
    to: streaming
action:
  - service: notify.mobile_app
    data:
      message: "Garten camera is now streaming"
```

---

## Services

### `bosch_shc_camera.trigger_snapshot`
Force an immediate refresh for all cameras (same as the Refresh button).
```yaml
service: bosch_shc_camera.trigger_snapshot
```

### `bosch_shc_camera.open_live_connection`
Open a live proxy connection for a specific camera by camera ID (UUID).
```yaml
service: bosch_shc_camera.open_live_connection
data:
  camera_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```
> Tip: Use the **Live Stream switch** instead — it does the same and shows state in the UI.

---

## Live Stream — How It Works

Turn ON the **Live Stream switch** (`switch.bosch_garten_live_stream`) to open a live proxy connection.

The connection is opened via `PUT /v11/video_inputs/{id}/connection` with `{"type": "REMOTE"}`.

**Two streams available on the proxy:**

| Port | Protocol | Content |
|------|----------|---------|
| `42090` | HTTPS | `snap.jpg` — current JPEG, no auth, ~1.5s latency |
| `443` | `rtsps://` (RTSP over TLS) | 30fps H.264 1920×1080 + AAC-LC 16kHz audio |

**When switch is ON:**
- Camera entity state changes to `streaming`
- Camera image refreshes from live `snap.jpg` proxy (current image, near-real-time)
- `stream_source` attribute is set to the `rtsps://` URL for HA's stream component
- `live_rtsps` and `live_proxy` attributes appear on the camera entity

**Privacy Mode:** Controlled via `PUT /v11/video_inputs/{id}/privacy` (Bosch cloud API — no SHC needed). When Privacy Mode is ON, `snap.jpg` returns HTTP 200 with an empty body (0 bytes). The integration detects this and does not update the cached image. The Lovelace card shows a 🔒 "Privat-Modus aktiv" overlay instead of the camera image. When Privacy Mode is turned OFF, the card automatically fetches a fresh image. Privacy state is read directly from the `/v11/video_inputs` cloud response on every coordinator tick.

**Session lifetime:** The Bosch proxy session lasts ~60 seconds. If the switch stays ON, the integration maintains the session. Turn the switch OFF to close the session immediately.

> **Live video with audio in the Bosch Camera Card:**
> - Turn on **Live Stream** + **Ton** → the card switches from snapshot polling to a **live HLS video** with 30fps H.264 + AAC audio
> - Turn on **Live Stream** only (Ton OFF) → the card uses fast snapshot polling (every 2s) — near-real-time image without audio
> - The integration registers the `rtsps://` stream in HA's built-in **go2rtc** bridge. Chrome/Firefox use **hls.js** (loaded on demand); Safari/iOS use native HLS
>
> **Audio is OFF by default.** Turn on the **Ton** switch to enable AAC-LC 16kHz mono audio.
>
> If the live stream does not appear: verify that go2rtc is running (HA 2023.4+ includes it by
> default), then turn the switch OFF and ON again to re-register the stream.

---

## Example Automations

### 1 — Notify on motion detection

Send a mobile push notification with a camera snapshot when a new motion event is detected.

```yaml
alias: "Bosch Garten — Motion notification"
trigger:
  - platform: state
    entity_id: sensor.bosch_garten_last_event
action:
  - service: notify.mobile_app_your_phone
    data:
      title: "Motion detected — Garten"
      message: "New motion event at {{ states('sensor.bosch_garten_last_event') }}"
      data:
        image: /api/camera_proxy/camera.bosch_garten
```

---

### 2 — Auto start live stream on motion, stop after 5 minutes

Automatically opens the live proxy connection when motion is detected and closes it 5 minutes later.

```yaml
alias: "Bosch Garten — Auto live stream on motion"
trigger:
  - platform: state
    entity_id: sensor.bosch_garten_last_event
action:
  - service: switch.turn_on
    target:
      entity_id: switch.bosch_garten_live_stream
  - delay: "00:05:00"
  - service: switch.turn_off
    target:
      entity_id: switch.bosch_garten_live_stream
mode: restart   # restart timer if motion fires again before 5 min
```

---

### 3 — Turn off live stream when nobody is home

Stop live streams automatically when everyone leaves home (presence detection).

```yaml
alias: "Bosch — Stop streams when leaving"
trigger:
  - platform: state
    entity_id: zone.home
    to: "0"
action:
  - service: switch.turn_off
    target:
      entity_id:
        - switch.bosch_garten_live_stream
        - switch.bosch_kamera_live_stream
```

---

### 4 — Daily snapshot refresh (keep thumbnails fresh overnight)

Force a snapshot refresh every morning so the card always shows today's image even without motion.

```yaml
alias: "Bosch — Morning snapshot refresh"
trigger:
  - platform: time
    at: "07:00:00"
action:
  - service: bosch_shc_camera.trigger_snapshot
```

---

### 5 — Alert when camera goes OFFLINE

Get notified if a camera loses connection.

```yaml
alias: "Bosch Garten — Camera offline alert"
trigger:
  - platform: state
    entity_id: sensor.bosch_garten_status
    to: "OFFLINE"
    for: "00:05:00"   # only alert if offline for 5+ min (avoid flapping)
action:
  - service: notify.mobile_app_your_phone
    data:
      title: "Camera offline"
      message: "Bosch Garten is OFFLINE. Check your network or the Bosch Smart Home app."
```

---

### 6 — Auto-download events (via HA script)

If auto-download is not enabled in the integration options, trigger a refresh via script:

```yaml
alias: "Bosch — Refresh all cameras every 5 minutes"
trigger:
  - platform: time_pattern
    minutes: "/5"
action:
  - service: bosch_shc_camera.trigger_snapshot
```

---

## Auto-Download

When enabled, all event files (JPEG + MP4) are downloaded to `download_path/{camera_name}/` after each refresh.

Files are named: `2026-03-19_09-32-08_MOVEMENT_49C3521E.jpg`

Already-downloaded files are skipped — incremental sync.

Suggested path: `/config/bosch_events` (accessible via HA file editor / Samba share)

---

## Icon in Settings → Integrations

The integration icon is served from the `custom_components/bosch_shc_camera/brand/` folder. HA's brands API checks this directory automatically for custom integrations (HA 2023.9+).

All required variants are included:

| File | Size |
|------|------|
| `brand/icon.png` | 256 × 256 (light background) |
| `brand/icon@2x.png` | 512 × 512 (high-DPI light) |
| `brand/dark_icon.png` | 256 × 256 (dark mode) |
| `brand/dark_icon@2x.png` | 512 × 512 (high-DPI dark) |

If the icon still shows "not available" after installing: do a **full HA restart** (not just a reload) to clear the internal `has_branding` cache.

---

## API Reference (Reverse Engineered)

```
Base: https://residential.cbs.boschsecurity.com
Auth: Authorization: Bearer {token}
SSL:  verify=False (Bosch private CA)

GET  /v11/video_inputs                          → list cameras (includes privacyMode + featureStatus)
GET  /v11/video_inputs/{id}/ping                → "ONLINE" / "OFFLINE"
GET  /v11/events?videoInputId={id}&limit=20     → motion events (imageUrl + videoClipUrl)
GET  {event.imageUrl}                           → event JPEG snapshot
GET  {event.videoClipUrl}                       → event MP4 clip
PUT  /v11/video_inputs/{id}/connection          → open live proxy {"type": "REMOTE"/"LOCAL"}
GET  /v11/video_inputs/{id}/privacy             → {"privacyMode": "ON"/"OFF", "durationInSeconds": null}
PUT  /v11/video_inputs/{id}/privacy             → toggle privacy mode (HTTP 204 on success)
PUT  /v11/video_inputs/{id}/lighting_override   → camera light on/off (HTTP 204 on success)
PUT  /v11/video_inputs/{id}/enable_notifications → notifications on/off (HTTP 204 on success)
GET  /v11/video_inputs/{id}/pan                 → pan position (CAMERA_360 only)
PUT  /v11/video_inputs/{id}/pan                 → set pan position (CAMERA_360 only, HTTP 204)
GET  /v11/feature_flags                         → account feature flags
GET  /v11/purchases                             → subscription info
GET  /v11/contracts?locale=de_DE                → contracts
```

### Privacy mode
```
GET  /v11/video_inputs/{id}/privacy
  → {"privacyMode": "OFF", "durationInSeconds": null}

PUT  /v11/video_inputs/{id}/privacy
  Body: {"privacyMode": "ON", "durationInSeconds": null}
  Response: HTTP 204 (no body)

No SHC local API needed. State is also included in GET /v11/video_inputs response
(privacyMode field), so no separate polling is required.
```

### Camera light control (cloud API)
```
GET  /v11/video_inputs/{id}/lighting_override
→ {"frontLightOn": false, "wallwasherOn": false}

PUT  /v11/video_inputs/{id}/lighting_override
# Turn on:
{"frontLightOn": true, "wallwasherOn": true, "frontLightIntensity": 1.0}
# Turn off:
{"frontLightOn": false, "wallwasherOn": false}
→ HTTP 204 No Content

Light schedule state is also embedded in GET /v11/video_inputs per camera:
  "featureSupport": {"light": true/false, ...}
  "featureStatus": {
    "scheduleStatus": "ALWAYS_OFF" / "ALWAYS_ON" / "SCHEDULE",
    "frontIlluminatorInGeneralLightOn": false,
    "frontIlluminatorGeneralLightIntensity": 1.0,
    "lightOnMotion": false,
    "lightOnMotionFollowUpTimeSeconds": 60,
    "generalLightOnTime": "20:15:00",
    "generalLightOffTime": "22:35:00"
  }

No SHC local API needed — light override is fully controllable via cloud API.
```

### Live proxy endpoints (after PUT /connection)

```
https://proxy-NN.live.cbs.boschsecurity.com:42090/{hash}/snap.jpg
  → Current JPEG (no auth — hash = credential)

rtsps://proxy-NN.live.cbs.boschsecurity.com:443/{hash}/rtsp_tunnel
  ?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=60
  → 30fps H.264 1920×1080 + AAC-LC 16kHz mono
  → Open: ffplay -rtsp_transport tcp -tls_verify 0 "rtsps://..."
```

---

## Discovered API Endpoints (v2.0.0)

The following endpoints were found via mitmproxy traffic analysis of the official Bosch Smart Home Camera iOS/Android app. Not all endpoints are used by this integration — this is a complete reference for future development.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v11/registration/check` | GET | User info + exact token expiration time |
| `/protocol_support?protocol=11` | GET | Protocol support check |
| `/v11/state/pre-maintenance` | GET | Server maintenance mode check |
| `/v11/video_inputs/{id}` | GET | Fetch single camera by ID |
| `/v11/video_inputs/{id}/commissioned` | GET | Pairing/connection status |
| `/v11/video_inputs/{id}/firmware` | GET | Firmware version + update status |
| `/v11/video_inputs/{id}/lighting_override` | GET | Current light override state |
| `/v11/video_inputs/{id}/lighting_options` | GET | Full light schedule config |
| `/v11/video_inputs/{id}/ambient_light_sensor_level` | GET | Ambient light sensor reading (0.0–1.0) |
| `/v11/video_inputs/{id}/motion` | GET | Motion detection on/off + sensitivity |
| `/v11/video_inputs/{id}/motion_sensitive_areas` | GET | Motion zones (normalized rect coords) |
| `/v11/video_inputs/{id}/audioAlarm` | GET | Audio alarm threshold + config |
| `/v11/video_inputs/{id}/recording_options` | GET | Sound-in-recording setting |
| `/v11/video_inputs/{id}/timestamp` | GET | Timestamp overlay on/off |
| `/v11/video_inputs/{id}/wifiinfo` | GET | WiFi SSID, signal strength, local IP, MAC |
| `/v11/video_inputs/{id}/rules` | GET | Camera automation rules |

---

## New Sensors (v2.0.0 / v2.1.0 / v2.2.0)

### WiFi Signal Strength

`sensor.bosch_garten_wifi_signal` — WiFi signal strength in percent.

- Data source: `GET /v11/video_inputs/{id}/wifiinfo` (polled each coordinator tick)
- Unit: `%`, device class: `signal_strength`
- Attributes: `ssid`, `ip_address`, `mac_address`, `lan_ip_rcp` (LAN IP from RCP 0x0a36, if available)

### Firmware Version

`sensor.bosch_garten_firmware_version` — Firmware version string.

- Data source: `firmwareVersion` field from `GET /v11/video_inputs` (no extra API call)
- Attributes: `up_to_date` (bool), `hardware_version`, `product_name_rcp` (product name from RCP 0x0aea, if available)

### Ambient Light Level

`sensor.bosch_garten_ambient_light` — Ambient light level as percentage.

- Data source: `GET /v11/video_inputs/{id}/ambient_light_sensor_level` (polled each coordinator tick)
- API returns a float 0.0–1.0, converted to 0–100%
- Unit: `%`

### Clock Offset (v2.2.0)

`sensor.bosch_garten_clock_offset` — camera clock offset vs HA server in seconds.

- Data source: RCP command `0x0a0f` (P_OCTET 8B) via cloud proxy `rcp.xml`
- Unit: `s` (seconds), state class: `measurement`, entity category: `diagnostic`
- Attributes: `offset_seconds` (float), `status` (`in_sync` if |offset| < 5s, `minor_drift` if < 60s, `out_of_sync` otherwise)
- State is `unavailable` if RCP session fails or command not accessible
- Registered for all cameras

### LED Dimmer (v2.1.0)

`sensor.bosch_garten_led_dimmer` — LED indicator dimmer value.

- Data source: RCP command `0x0c22` (T_WORD) via cloud proxy `rcp.xml`
- Unit: `%` (0–100), state class: `measurement`
- Only created for cameras with `featureSupport.light = True`
- State is `unavailable` if RCP session fails (camera OFFLINE or proxy expired)

---

## Video Quality — Stream Quality Select (v2.6.0)

The `select.bosch_<cam>_video_quality` entity lets you choose the RTSPS stream quality at runtime without restarting the integration. The preference is applied to the next `PUT /connection` call (live stream reconnect, background snapshot refresh, or RCP proxy connection).

| Option | `highQualityVideo` | `inst` | Approx. bitrate | Use when |
|--------|-------------------|--------|----------------|----------|
| **Auto** (default) | `false` | `2` | ~7.5 Mbps | Balanced — matches iOS app default |
| **Hoch (30 Mbps)** | `true` | `1` | ~30 Mbps | Maximum quality, primary encoder stream |
| **Niedrig (1.9 Mbps)** | `false` | `4` | ~1.9 Mbps | Slow network / remote access |

When the Live Stream switch is already ON and you change the quality, the integration automatically reconnects the stream with the new parameters and re-registers the go2rtc source.

---

## Notifications Switch — 3-State Handling (v2.0.0)

The Bosch API can return three values for `notificationsEnabledStatus`:

| API value | Switch state | Description |
|-----------|-------------|-------------|
| `FOLLOW_CAMERA_SCHEDULE` | ON | Notifications follow the camera schedule |
| `ON_CAMERA_SCHEDULE` | ON | Notifications active (alternate ON state) |
| `ALWAYS_OFF` | OFF | Notifications always disabled |

Both `FOLLOW_CAMERA_SCHEDULE` and `ON_CAMERA_SCHEDULE` are treated as switch state = **ON**.
Turning the switch **ON** always sends `FOLLOW_CAMERA_SCHEDULE`.

---

## RCP Protocol Integration (v2.1.0 / v2.2.0)

### What is RCP?

RCP (Bosch Remote Configuration Protocol) is a low-level camera configuration protocol embedded in Bosch cameras. It is accessible via the cloud proxy connection at the path `rcp.xml` — the same proxy used for `snap.jpg` and the RTSP stream.

After calling `PUT /connection REMOTE`, the proxy URL `https://proxy-NN:42090/{hash}/rcp.xml` accepts RCP commands as HTTP GET requests with query parameters. Authentication is provided by the URL hash (auth=3, anonymous via hash), which grants **read-only access**.

### Session handshake

Each RCP session requires a two-step handshake:

1. `GET rcp.xml?command=0xff0c&direction=WRITE&type=P_OCTET&payload=...` → extracts `<sessionid>` from the XML response
2. `GET rcp.xml?command=0xff0d&direction=WRITE&type=P_OCTET&sessionid=...` → ACK

After the handshake, read commands can be issued:
```
GET rcp.xml?command=0xCOMMAND&direction=READ&type=TYPE&sessionid=SID
```

### Readable RCP commands

| Command | Type | Description |
|---------|------|-------------|
| `0x099e` | P_OCTET | Live JPEG thumbnail 160×90 (~2–3 KB) — very fast alternative to snap.jpg |
| `0x0a0f` | P_OCTET 8B | Camera real-time clock (bytes: YYYY×2 MM DD HH MM SS DOW) |
| `0x0c22` | T_WORD num=1 | LED dimmer value 0–100 |
| `0x0d00` | P_OCTET 4B | Privacy mask state (byte[1]=1 means ON) |
| `0x0aea` | P_OCTET | Product name as ASCII string |
| `0x0a36` | P_OCTET 16B | Camera LAN IP address (bytes 0–3 as IPv4) |

> **Note:** Write commands require a service account (auth level > 3). This integration uses auth=3 (anonymous via hash) which is read-only. No camera configuration is modified by the RCP integration.

### New entity: LED Dimmer sensor

`sensor.bosch_*_led_dimmer` — LED dimmer intensity 0–100%.

- Only registered for cameras that report `featureSupport.light = True` (i.e. cameras with a physical LED indicator)
- Data source: RCP command `0x0c22` (T_WORD) via cloud proxy on each coordinator tick
- Unit: `%`, state class: `measurement`, icon: `mdi:brightness-6`
- State is `unavailable` when the RCP session cannot be established (e.g. camera OFFLINE, proxy expired)

### Privacy mode cross-validation

The privacy mode switch (`switch.bosch_*_privacy_mode`) now exposes an additional attribute:

| Attribute | Description |
|-----------|-------------|
| `rcp_state` | Privacy mask byte[1] from RCP command `0x0d00` (1=ON, 0=OFF, None=unavailable) |

This allows cross-validating the REST API privacy state (from `/v11/video_inputs`) with a direct camera-side reading. The switch logic (`is_on`) is still driven solely by the REST API — `rcp_state` is informational only.

### RCP thumbnail fallback in camera entity

When the live proxy `snap.jpg` fetch fails (timeout or network error), the camera entity automatically tries the RCP thumbnail (command `0x099e`) as a fallback:

- Returns a 160×90 JPEG (2–3 KB, valid JPEG with `FFD8` magic)
- Much faster than snap.jpg (~instant vs ~1.5 s round-trip)
- Logged as: `Using RCP thumbnail fallback (160x90)`
- Only attempted when a live proxy connection is active (the proxy URL is required for RCP access)

---

## Example Automations

### Motion alert with camera snapshot

```yaml
automation:
  - alias: "Bosch Garten — Motion Alert"
    description: "Push notification with snapshot on motion detection"
    trigger:
      - platform: state
        entity_id: sensor.bosch_garten_last_event_type
        to: "movement"
    action:
      - service: notify.mobile_app
        data:
          title: "Bewegung erkannt — Garten"
          message: >
            {{ now().strftime('%H:%M') }} Uhr
          data:
            image: "{{ state_attr('sensor.bosch_garten_last_event', 'image_url') }}"
            url: "{{ state_attr('sensor.bosch_garten_last_event', 'video_clip_url') }}"
```

### Audio alarm alert

```yaml
automation:
  - alias: "Bosch Kamera — Audio Alarm"
    trigger:
      - platform: state
        entity_id: sensor.bosch_kamera_last_event_type
        to: "audio alarm"
    action:
      - service: notify.mobile_app
        data:
          title: "Geraeusch erkannt — Kamera"
          message: "Audio-Alarm um {{ now().strftime('%H:%M') }} Uhr"
```

### Privacy mode schedule

```yaml
automation:
  - alias: "Bosch Kamera — Privacy On at Night"
    trigger:
      - platform: time
        at: "22:00:00"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.bosch_kamera_privacy_mode

  - alias: "Bosch Kamera — Privacy Off in Morning"
    trigger:
      - platform: time
        at: "07:00:00"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.bosch_kamera_privacy_mode
```

### Camera light on motion after sunset

```yaml
automation:
  - alias: "Bosch Garten — Light on Motion After Dark"
    trigger:
      - platform: state
        entity_id: sensor.bosch_garten_last_event_type
        to: "movement"
    condition:
      - condition: sun
        after: sunset
        before: sunrise
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.bosch_garten_camera_light
      - delay:
          minutes: 5
      - service: switch.turn_off
        target:
          entity_id: switch.bosch_garten_camera_light
```

### Enable motion on workdays only

```yaml
automation:
  - alias: "Bosch — Motion On on Workdays"
    trigger:
      - platform: time
        at: "08:00:00"
    condition:
      - condition: time
        weekday: [mon, tue, wed, thu, fri]
    action:
      - service: switch.turn_on
        target:
          entity_id:
            - switch.bosch_garten_motion_detection
            - switch.bosch_kamera_motion_detection

  - alias: "Bosch — Motion Off on Weekends"
    trigger:
      - platform: time
        at: "08:00:00"
    condition:
      - condition: time
        weekday: [sat, sun]
    action:
      - service: switch.turn_off
        target:
          entity_id:
            - switch.bosch_garten_motion_detection
            - switch.bosch_kamera_motion_detection
```

---

## Changelog

### v2.11.0 — Instant toggle feedback, snapshot on privacy OFF

**Toggle state no longer reverts on mobile**
After toggling privacy mode, camera LED light, or notifications, the integration now immediately calls `async_update_listeners()` after writing the new state to the cache. Previously, the card's 10 s optimistic timeout expired before the 22 s coordinator tick updated the entity state, causing the toggle to snap back.

**Fresh snapshot after privacy OFF**
When privacy mode is disabled, a `_async_trigger_image_refresh(delay=1.5)` task is kicked off automatically. The card will show the live camera image within ~2 s of disabling privacy.

---

### v2.10.0 + Card v1.7.4 — Faster first load, mobile images

**Card v1.7.4**
- Fixed 60 s spinner on first load (HA assigns `hass` after `setConfig()` — image request now re-triggered in `set hass(hass)`)
- Snapshot URLs now include `?width=<display_width>` — mobile gets 320×180 RCP thumbnail (3 KB); desktop gets 1080p snap.jpg (150 KB)
- Snapshot button first poll: 1000 ms → 500 ms

**Integration (camera.py)**
- `async_camera_image(width, height)` prefers RCP `0x099e` thumbnail when `width ≤ 640`

---

### v2.9.0 — Performance: sub-second snapshot response, non-blocking switches & buttons

**Proxy URL caching (50 s TTL)**
`PUT /connection` (opens the Bosch proxy lease, ~1.5 s) is now cached per camera. All snapshot refreshes within 50 s reuse the cached URL and skip the PUT call entirely.
- Cold first fetch: ~3 s (unchanged — proxy must be opened)
- Warm refreshes: ~70 ms (down from ~3 s)
- Quality change (`select.bosch_*_video_quality`) invalidates the cache so the next fetch always opens a fresh proxy with the new settings.

**Background snapshot refresh in `camera.py`**
`async_camera_image` now returns the stale cached image immediately (< 1 ms) and fires a background task to refresh the snapshot. Every 60 s card cycle resolves in < 90 ms. Only the very first image load (empty cache) waits for the network.

**Non-blocking coordinator refresh — all switches and buttons**
All `await coordinator.async_request_refresh()` calls across `__init__.py`, `switch.py`, and `button.py` replaced with `hass.async_create_task(coordinator.async_request_refresh())`. Previously, toggling any switch (Live Stream, Privacy, Audio, Motion, Record Sound, Light, Notifications, Pan) or pressing the Refresh button blocked the HA event loop for up to 22 s while the full coordinator tick ran.

Affected methods:
- `BoschLiveStreamSwitch.async_turn_off`
- `BoschAudioSwitch._apply_audio_change`
- `BoschMotionEnabledSwitch.async_turn_on / async_turn_off`
- `BoschRecordSoundSwitch.async_turn_on / async_turn_off`
- `try_live_connection` in `__init__.py`
- All 5 cloud API call methods in coordinator (`set_privacy`, `set_light`, `set_notifications`, `set_pan`, `set_quality`)
- `BoschRefreshSnapshotButton.async_press`
- `handle_trigger_snapshot` service handler

**Lovelace card v1.7.3 — reduced delays**
- First snapshot poll after page load: 3 s → 1 s
- Snapshot poll interval (stream ON, Ton OFF): 3 s → 1 s
- Snapshot poll timeout: 26 s → 15 s
- Stream stop → snapshot switch delay: 6 s → 2 s
- Event-driven refresh delay (after new event detected): 2.5 s → 1.5 s
- Privacy-off auto-refresh delay: 3 s → 1.5 s
- Stream retry delay on HLS error: 2 s → 1 s
- hls.js pre-loaded at card init (`setConfig()`) instead of on demand — CDN fetch is done before the user toggles the stream
- `img-wrapper` CSS: `aspect-ratio: 16/9` added — privacy mode placeholder is now the same size as the camera image (was different aspect ratio for lower-resolution cameras)

---

## Related Projects

- [Bosch SHC API Docs Issue #63](https://github.com/BoschSmartHome/bosch-shc-api-docs/issues/63) — camera API discussion
- [boschshcpy](https://github.com/tschamm/boschshcpy) — Python library for local SHC API
- [homeassistant-bosch-shc](https://github.com/tschamm/homeassistant-bosch-shc) — existing HA integration (no camera images)
