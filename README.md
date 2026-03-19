# Bosch Smart Home Camera — Home Assistant Custom Integration

Adds your Bosch Smart Home cameras (CAMERA_EYES, CAMERA_360) as fully featured entities in Home Assistant.

> **No official API support exists for camera images.** This integration uses the reverse-engineered Bosch Cloud API, discovered via mitmproxy traffic analysis of the official Bosch Smart Home Camera iOS/Android app.

---

## Disclaimer

**This project is an independent, community-developed integration. It is not affiliated
with, endorsed by, sponsored by, or in any way officially connected to Robert Bosch
GmbH, Bosch Smart Home GmbH, or any of their subsidiaries or affiliates.
"Bosch", "Bosch Smart Home", and related names and logos are registered trademarks
of Robert Bosch GmbH.**

This integration communicates with a reverse-engineered, undocumented, and unofficial
API. The author(s) provide this software **"as is", without warranty of any kind**,
express or implied, including but not limited to warranties of merchantability,
fitness for a particular purpose, or non-infringement.

**By using this software, you agree that:**

- You use it entirely **at your own risk**.
- The author(s) shall not be held liable for any direct, indirect, incidental,
  special, or consequential damages arising from the use of, or inability to use,
  this software — including but not limited to data loss, service disruption,
  account suspension, or device damage.
- The API may be changed, restricted, or shut down by Bosch at any time without
  notice, which may render this integration non-functional.
- You are solely responsible for ensuring your use complies with Bosch's Terms of
  Service and any applicable laws in your jurisdiction.
- All rights and any legal recourse are expressly disclaimed by the author(s).
  Any use of this software is entirely your own responsibility.

**Reverse engineering notice:** The API was discovered solely for the purpose of
achieving interoperability with the user's own devices and data, which is explicitly
permitted under **§ 69e of the German Copyright Act (UrhG)** and **Article 6 of
EU Directive 2009/24/EC** on the legal protection of computer programs. No copy
of Bosch's software was distributed. Only network protocol observations were used.

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
| 💾 Auto-download events to folder | background | ❌ optional |
| 🎥 **Live stream — 30fps H.264 + AAC audio** | `camera` | ✅ via Live Stream switch |
| 📷 Live snapshot (current image, ~1.5s) | `camera` | ✅ via snap.jpg proxy |

All features are individually toggleable in **Settings → Integrations → Bosch Smart Home Camera → Configure**.

---

## Installation

### HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mosandlt&repository=Bosch-Smart-Home-Camera-Tool-HomeAssistant&category=integration)

1. Click the button above, or in HACS go to **Integrations → + Explore & Download Repositories** and search for **"Bosch Smart Home Camera"**
2. Download the integration
3. Restart Home Assistant
4. Go to **Settings → Integrations → + Add Integration** and search for **"Bosch Smart Home Camera"**

### Manual

### 1. Copy the integration

Copy the `bosch_shc_camera` folder into your Home Assistant `custom_components` directory:

```
/config/
  custom_components/
    bosch_shc_camera/
      __init__.py
      camera.py
      sensor.py
      button.py
      config_flow.py
      manifest.json
      strings.json
      services.yaml
```

### 2. Restart Home Assistant

Restart HA so it picks up the new custom component.

### 3. Add the integration

Go to **Settings → Integrations → + Add Integration** and search for **"Bosch Smart Home Camera"**.

### 4. Log in with your Bosch account

The integration uses OAuth2 (Bosch SingleKey ID). During setup, you will see a login URL — open it in your browser, log in, then paste the redirect URL back into the integration.

The integration saves your refresh token and renews it silently in the background. No manual token copy-paste needed.

---

## Authentication

The integration uses **OAuth2 PKCE** with your Bosch SingleKey ID (the same account you use for the Bosch Smart Home Camera app).

**Setup flow:**
1. During configuration, a login URL is shown — copy it and open it in your browser
2. Log in with your Bosch SingleKey ID
3. Your browser will show a **404 page** — this is expected and normal
4. Copy the full URL from the browser address bar (starts with `https://www.bosch.com/boschcam?code=...`)
5. Paste that URL back into the integration dialog

After first login, the integration saves a long-lived **refresh token** and renews the access token silently in the background. No manual action needed day-to-day.

If the refresh token expires (rare, after months of inactivity), go to **Settings → Integrations → Bosch Smart Home Camera → Configure → Force new browser login**.

---

## Options

Go to **Settings → Integrations → Bosch Smart Home Camera → Configure**:

| Option | Description | Default |
|--------|-------------|---------|
| Coordinator tick interval | How often the integration wakes up (seconds) | 60 |
| Camera status check interval | How often to ping ONLINE/OFFLINE (seconds) | 300 |
| Events fetch interval | How often to check for new motion events (seconds) | 300 |
| Enable snapshots | Show camera entities with latest JPEG | ✅ |
| Enable sensors | Show status / last event / events-today sensors | ✅ |
| Enable buttons | Show Refresh Snapshot button + Live Stream switch | ✅ |
| Auto-download events | Download all event JPEGs and MP4 clips to a local folder | ❌ |
| Download path | Local path for auto-downloaded events (e.g. `/config/bosch_events`) | — |
| Force new browser login | Re-run OAuth2 login if refresh token expired | — |

---

## Services

### `bosch_shc_camera.trigger_snapshot`
Force an immediate snapshot refresh for all cameras (same as pressing the Refresh button).

### `bosch_shc_camera.open_live_connection`
Try to establish a live proxy stream connection for a specific camera.
```yaml
service: bosch_shc_camera.open_live_connection
data:
  camera_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"   # your camera UUID
```
On success, the camera entity's `stream_source` attribute is set to the `rtsps://` URL
and HA renders a live video feed (30fps H.264 + AAC audio) in the Lovelace camera card.

---

## Entities Created

For each discovered camera (example: camera named "Outdoor"):

| Entity ID | Type | Description |
|-----------|------|-------------|
| `camera.bosch_outdoor` | camera | Latest snapshot (JPEG) |
| `sensor.bosch_outdoor_status` | sensor | ONLINE / OFFLINE |
| `sensor.bosch_outdoor_last_event` | sensor | Timestamp of latest motion event |
| `sensor.bosch_outdoor_events_today` | sensor | Number of events today |
| `button.bosch_outdoor_refresh_snapshot` | button | Force immediate refresh |
| `switch.bosch_outdoor_live_stream` | switch | Live stream ON/OFF |

All entities share a single HA device (grouped in the device view).

---

## Auto-Download

When enabled, the integration downloads all event files (JPEG snapshots + MP4 clips)
to `download_path/{camera_name}/` in the background after each refresh cycle.

Files are named: `2026-03-19_09-32-08_MOVEMENT_49C3521E.jpg`

Already-downloaded files are skipped — it's a smart incremental sync.

Suggested path: `/config/bosch_events` (accessible via HA file editor / Samba share)

---

## Limitations

| Feature | Status |
|---------|--------|
| Latest event snapshot | ✅ Working |
| Motion detection events | ✅ Via cloud API |
| Video clips (MP4) | ✅ Via cloud API |
| Status (ONLINE/OFFLINE) | ✅ Working |
| **Live snapshot (current image)** | ✅ Via snap.jpg proxy (port 42090) |
| **Live stream 30fps H.264 + AAC** | ✅ Via rtsps:// port 443 |
| Local network access | ⚠️ Fragile (breaks camera connection) |

### Live Stream — How It Works

Turn ON the **Live Stream switch** for a camera to open a live proxy connection.

The connection is opened via `PUT /v11/video_inputs/{id}/connection` with `{"type": "REMOTE"}`.

Response includes `urls[0]` = `proxy-NN.live.cbs.boschsecurity.com:42090/{hash}`.

**Two ports are available on the proxy:**

| Port | Protocol | What it serves |
|------|----------|---------------|
| `42090` | HTTP | `snap.jpg` — current JPEG snapshot, no auth needed |
| `443` | RTSP/1.0 over TLS (`rtsps://`) | Full 30fps H.264 1920×1080 + AAC-LC 16kHz audio |

When the switch is ON, the camera entity exposes:
- `rtsps_url` attribute: `rtsps://proxy-NN:443/{hash}/rtsp_tunnel?inst=1&enableaudio=1&...` — full stream
- `proxy_snap_url` attribute: `https://proxy-NN:42090/{hash}/snap.jpg` — current image

The `stream_source` is set to the `rtsps://` URL. HA's stream component (ffmpeg backend)
can open this if TLS verification can be disabled for Bosch's private CA.

**The live stream stays active** as long as the switch is ON. Turn it OFF to close the session.

> **Note:** If HA's stream component cannot open `rtsps://` (TLS verify issues),
> use the Python CLI tool's `live` command which uses `ffplay -tls_verify 0`.

> **Tip:** Add a **Camera card** in the Lovelace dashboard, select the camera entity,
> and click the live button in the card to start streaming.

---

## API Reference (Reverse Engineered)

```
Base: https://residential.cbs.boschsecurity.com
Auth: Authorization: Bearer {token}
SSL:  verify=False (Bosch private CA)

GET  /v11/video_inputs                         → list all cameras (id, title, model, firmware, mac)
GET  /v11/video_inputs/{id}                    → camera details
GET  /v11/video_inputs/{id}/ping               → "ONLINE"/"OFFLINE"
GET  /v11/video_inputs/{id}/firmware           → firmware version info
GET  /v11/events?videoInputId={id}             → camera-specific events
GET  /v11/events?videoInputId={id}&limit=N     → limited event list
GET  {event.imageUrl}                          → event JPEG snapshot ✅
GET  {event.videoClipUrl}                      → event MP4 clip ✅
PUT  /v11/video_inputs/{id}/connection         → open live proxy ({"type": "REMOTE"})
GET  /v11/feature_flags                        → feature flags for the account
GET  /v11/purchases                            → subscription / purchase info
GET  /v11/contracts?locale=de_DE               → contract info
```

### Live Proxy Endpoints (after PUT /connection)

```
# Port 42090 — HTTP only
https://proxy-NN.live.cbs.boschsecurity.com:42090/{hash}/snap.jpg
  → Current camera image (1920×1080 JPEG, no auth needed — hash = credential)

# Port 443 — RTSP/1.0 over TLS  ✅ WORKING
rtsps://proxy-NN.live.cbs.boschsecurity.com:443/{hash}/rtsp_tunnel
  ?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=60
  → Full 30fps H.264 1920×1080 + AAC-LC 16kHz mono audio
  → Open with: ffplay -rtsp_transport tcp -tls_verify 0 -i "rtsps://..."
```

Proxy sessions expire after ~60 seconds on the Bosch side. The integration automatically
re-opens the connection when the switch is toggled ON again.

---

## Related Projects

- [Bosch SHC API Docs Issue #63](https://github.com/BoschSmartHome/bosch-shc-api-docs/issues/63) — camera API discussion
- [boschshcpy](https://github.com/tschamm/boschshcpy) — Python library for local SHC API
- [homeassistant-bosch-shc](https://github.com/tschamm/homeassistant-bosch-shc) — existing HA integration (no camera images)
