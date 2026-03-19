# Bosch Smart Home Camera — Home Assistant Custom Integration

Adds your Bosch Smart Home cameras (CAMERA_EYES, CAMERA_360) as fully featured entities in Home Assistant.

> **No official API support exists for camera images.** This integration uses the reverse-engineered Bosch Cloud API, discovered via mitmproxy traffic analysis of the official Bosch Smart Home Camera iOS/Android app.

---

## Features

| Feature | Entity type | Default |
|---------|-------------|---------|
| 📸 Latest snapshot per camera | `camera` | ✅ enabled |
| 🟢 Camera status (ONLINE/OFFLINE) | `sensor` | ✅ enabled |
| 🕐 Last event timestamp | `sensor` | ✅ enabled |
| 📊 Events today count | `sensor` | ✅ enabled |
| 🔄 Refresh Snapshot button | `button` | ✅ enabled |
| 📡 Open Live Stream button | `button` | ✅ enabled |
| 💾 Auto-download events to folder | background | ❌ optional |
| 🎥 Live RTSP stream | `camera` stream_source | ⚠️ pending |

All features are individually toggleable in **Settings → Integrations → Bosch Smart Home Camera → Configure**.

---

## Installation

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

### 4. Enter your Bearer Token

The integration will ask for a Bearer Token. See the next section on how to get it.

---

## Getting the Bearer Token

The Bosch cloud API uses a JWT Bearer token. You need to capture it from the official Bosch Smart Home Camera app using a proxy tool.

### Requirements
- A Mac or PC on the same WiFi network as your phone
- [mitmproxy](https://mitmproxy.org/) installed (`pip3 install mitmproxy`)

### Steps

**On your Mac/PC:**
```bash
# Find your computer's IP address (e.g. 192.168.1.100)
ifconfig | grep "inet "   # Mac
ipconfig                  # Windows

# Start mitmproxy
mitmdump --listen-host YOUR_IP --listen-port 8890 2>&1 | tee /tmp/mitm_log.txt
```

**On your iPhone (Settings → WiFi → your network → Configure Proxy):**
1. Set Proxy to **Manual**
2. Server: `YOUR_IP`, Port: `8890`
3. Open Safari and go to `http://mitm.it`
4. Tap **Apple** → install the profile
5. Go to **Settings → General → VPN & Device Management** → trust the mitmproxy certificate
6. **Settings → General → About → Certificate Trust Settings** → enable full trust for mitmproxy

**Capture the token:**
1. Force-close the **Bosch Smart Home Camera** app on your phone
2. Re-open it (this triggers re-authentication)
3. Watch the terminal — look for a line like:
   ```
   Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCIgOiAiSldUIiwi...
   ```
4. Copy everything after `Bearer ` — that's your token

**The token lasts about 1 hour.** When cameras stop updating in HA, go to **Settings → Integrations → Bosch Smart Home Camera → Configure** to enter a fresh token.

---

## Options

Go to **Settings → Integrations → Bosch Smart Home Camera → Configure**:

| Option | Description | Default |
|--------|-------------|---------|
| Bearer Token | Paste a fresh token here (leave blank to keep current) | — |
| Scan interval | How often to refresh snapshots (seconds) | 30 |
| Enable snapshots | Show camera entities with latest JPEG | ✅ |
| Enable sensors | Show status / last event / events-today sensors | ✅ |
| Enable buttons | Show Refresh Snapshot + Open Live Stream buttons | ✅ |
| Auto-download events | Download all event JPEGs and MP4 clips to a local folder | ❌ |
| Download path | Local path for auto-downloaded events (e.g. `/config/bosch_events`) | — |

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
On success, the camera entity's `stream_source` attribute is set to the RTSP URL
and HA renders a live video feed in the Lovelace camera card.

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
| `button.bosch_outdoor_open_live_stream` | button | Try live RTSP connection |

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
| **Live view / real-time** | ⚠️ Pending (see below) |
| Local network access | ⚠️ Fragile (breaks camera connection) |
| RTSP stream | 🔬 Under investigation |

### Live View Status

The app uses a cloud proxy for live streaming (`proxy-NN.live.cbs.boschsecurity.com:42090`).
The proxy is opened via `PUT /v11/video_inputs/{id}/connection` with a `type` enum parameter.
The exact value of this enum has not yet been discovered.

The "Open Live Stream" button tries 20+ candidate values automatically.
Once the correct value is found (via mitmproxy request body capture), add it to the top
of `LIVE_TYPE_CANDIDATES` in `__init__.py` and it will work.

When live view is working, the RTSP stream URL is:
```
rtsp://proxy-NN.live.cbs.boschsecurity.com:42090/{hash}/rtsp_tunnel?inst=2&enableaudio=1
```

---

## API Reference (Reverse Engineered)

```
Base: https://residential.cbs.boschsecurity.com
Auth: Authorization: Bearer {token}

GET  /v11/video_inputs                         → list all cameras
GET  /v11/video_inputs/{id}                    → camera details
GET  /v11/video_inputs/{id}/ping               → "ONLINE"/"OFFLINE"
GET  /v11/events                               → all 400 events
GET  /v11/events?videoInputId={id}             → camera-specific events
GET  /v11/events/{event_id}/snap.jpg           → event JPEG snapshot ✅
GET  /v11/events/{event_id}/clip.mp4           → event MP4 video ✅
PUT  /v11/video_inputs/{id}/connection         → open live stream proxy (type TBD)
```

---

## Related Projects

- [Bosch SHC API Docs Issue #63](https://github.com/BoschSmartHome/bosch-shc-api-docs/issues/63) — camera API discussion
- [boschshcpy](https://github.com/tschamm/boschshcpy) — Python library for local SHC API
- [homeassistant-bosch-shc](https://github.com/tschamm/homeassistant-bosch-shc) — existing HA integration (no camera images)

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
