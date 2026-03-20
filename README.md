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
| 📸 Latest snapshot per camera | `camera` | ✅ enabled |
| 🟢 Camera status (ONLINE/OFFLINE) | `sensor` | ✅ enabled |
| 🕐 Last event timestamp | `sensor` | ✅ enabled |
| 📊 Events today count | `sensor` | ✅ enabled |
| 🔄 Refresh Snapshot button | `button` | ✅ enabled |
| 📡 Live Stream switch (ON/OFF) | `switch` | ✅ enabled |
| 🔇 Audio switch (muted by default) | `switch` | ✅ enabled |
| 💡 Camera LED light switch | `switch` | ✅ enabled (requires SHC config) |
| 🔒 Privacy mode switch | `switch` | ✅ enabled (requires SHC config) |
| 💾 Auto-download events to folder | background | ❌ optional |
| 🎥 **Live stream — 30fps H.264 + optional AAC audio** | `camera` | ✅ via Live Stream switch |
| 📷 Live snapshot (current image, ~1.5s) | `camera` | ✅ via snap.jpg proxy |
| 🃏 **Custom Lovelace card** | `bosch-camera-card` | ✅ separate JS file |

**Camera state** — `camera.bosch_garten` shows:
- `idle` — no live stream active
- `streaming` — live proxy connection is open (switch ON)

All features are individually toggleable in **Settings → Integrations → Bosch Smart Home Camera → Configure**.

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

## Custom Lovelace Card — Bosch Camera Card

A dedicated Lovelace card showing the camera feed with streaming state, status, event info, and controls.

### What the card shows

```
┌──────────────────────────────────┐
│ ● Garten              [streaming]│  ← status dot + stream badge
│  ┌────────────────────────────┐  │
│  │        Camera image        │  │  ← auto-refreshed (30s idle / 3s streaming)
│  │ Last: 2026-03-19 09:32  5 events │
│  └────────────────────────────┘  │
│  Status: ONLINE  Last event: …   │
│  [ 📸 Snapshot ] [ 📹 Live Stream ] [ ⛶ ] │
│  [  🔊 Ton  ] [  💡 Licht  ] [  🔒 Privat  ] │
└──────────────────────────────────┘
```

- **Status dot** — green = ONLINE, red = OFFLINE, grey = unknown
- **Stream badge** — `idle` (grey) or `streaming` (blue, pulsing dot)
- **Camera image** — served via HA camera proxy, auto-refreshed; shows cached image instantly on load
- **Snapshot button** — triggers a live image refresh; polls for the new image and displays it automatically (no fixed delay)
- **Live Stream button** — toggles `switch.bosch_garten_live_stream`; starts 30fps H.264 + audio stream
- **Fullscreen button** — native fullscreen on desktop/Android; CSS overlay fallback on iOS Safari
- **Ton** — toggles `switch.bosch_garten_audio` (audio in live stream, default OFF)
- **Licht** — toggles `switch.bosch_garten_camera_light` (camera LED indicator, requires SHC local API config)
- **Privat** — toggles `switch.bosch_garten_privacy_mode` (privacy mode, requires SHC local API config); when ON and no image available, shows a "Privat-Modus aktiv" placeholder

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
refresh_interval_idle: 30              # seconds between image refreshes when idle (default: 30)
refresh_interval_streaming: 3          # seconds between image refreshes when streaming (default: 3)
```

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
| *(auto)* | `sensor.bosch_garten_status` |
| *(auto)* | `sensor.bosch_garten_events_today` |
| *(auto)* | `sensor.bosch_garten_last_event` |

Toggle button visibility rules:
- **Entity doesn't exist** (e.g. SHC not configured) → button is **hidden**
- **Entity is `unavailable` / `unknown`** → button shown but **dimmed and disabled**
- **Entity is `on` / `off`** → button shown, highlighted when ON

This means without SHC configured, only the **Ton** button is shown (audio always works). **Licht** and **Privat** buttons appear automatically once SHC is set up.

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
| `button.bosch_garten_refresh_snapshot` | button | Force immediate data refresh |
| `switch.bosch_garten_live_stream` | switch | Live stream ON/OFF |
| `switch.bosch_garten_audio` | switch | Audio ON/OFF in live stream (default: OFF) |
| `switch.bosch_garten_camera_light` | switch | Camera LED indicator ON/OFF (requires SHC) |
| `switch.bosch_garten_privacy_mode` | switch | Privacy mode ON/OFF — blocks camera view (requires SHC) |

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

**Privacy Mode:** When Privacy Mode is ON, `snap.jpg` returns HTTP 200 with an empty body (0 bytes). The integration detects this and does not update the cached image. The Lovelace card shows a 🔒 "Privat-Modus aktiv" overlay instead of the camera image. When Privacy Mode is turned OFF, the card automatically fetches a fresh image.

**Session lifetime:** The Bosch proxy session lasts ~60 seconds. If the switch stays ON, the integration maintains the session. Turn the switch OFF to close the session immediately.

> **Live video with audio in HA:** When the Live Stream switch is ON, the integration registers
> the `rtsps://` stream in HA's built-in **go2rtc** bridge with `#insecure` to bypass Bosch's
> private CA certificate. HA's camera card will then play **30fps H.264 via WebRTC**
> directly in the browser — no extra software needed.
>
> **Audio is OFF by default** — turn on the **Audio switch** (`switch.bosch_garten_audio`) to add
> AAC-LC 16kHz mono audio. If already streaming, the switch re-opens the connection automatically
> so the change takes effect immediately.
>
> If the live stream does not appear: verify that go2rtc is running (HA 2023.4+ includes it by
> default), then turn the switch OFF and ON again to re-register the stream.
> The `snap.jpg` proxy (near-real-time JPEG) always works and is used by the Bosch Camera Card
> for the thumbnail image.

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

GET  /v11/video_inputs                          → list cameras
GET  /v11/video_inputs/{id}/ping                → "ONLINE" / "OFFLINE"
GET  /v11/events?videoInputId={id}&limit=20     → motion events (imageUrl + videoClipUrl)
GET  {event.imageUrl}                           → event JPEG snapshot
GET  {event.videoClipUrl}                       → event MP4 clip
PUT  /v11/video_inputs/{id}/connection          → open live proxy {"type": "REMOTE"}
GET  /v11/feature_flags                         → account feature flags
GET  /v11/purchases                             → subscription info
GET  /v11/contracts?locale=de_DE                → contracts
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

## Related Projects

- [Bosch SHC API Docs Issue #63](https://github.com/BoschSmartHome/bosch-shc-api-docs/issues/63) — camera API discussion
- [boschshcpy](https://github.com/tschamm/boschshcpy) — Python library for local SHC API
- [homeassistant-bosch-shc](https://github.com/tschamm/homeassistant-bosch-shc) — existing HA integration (no camera images)
