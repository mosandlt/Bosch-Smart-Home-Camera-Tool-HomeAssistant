# Bosch Smart Home Camera — Home Assistant Integration

Adds your Bosch Smart Home cameras (Eyes Outdoor, 360 Indoor) as fully featured entities in Home Assistant. Ships with two custom **Lovelace cards** — one detailed view per camera and a multi-camera grid — plus live streaming, controls, snapshot retrieval, and event-driven notifications.

**Supported models:** Eyes Outdoor (Gen1), Eyes Outdoor II (Gen2), 360 Indoor (Gen1), Eyes Indoor II (Gen2) — model-specific timing and configuration is automatic.

> **No official API.** This integration uses the reverse-engineered Bosch Cloud API, discovered via mitmproxy traffic analysis of the official Bosch Smart Camera app.

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

[![hacs][hacsbadge]][hacs]
[![Project Maintenance][maintenance-shield]][user_profile]
[![BuyMeCoffee][buymecoffeebadge]][buymecoffee]

[![Community Forum][forum-shield]][forum]
[![GitHub Stars][stars-shield]][stars]
![AI-Assisted](https://img.shields.io/badge/AI--Assisted-blue?style=for-the-badge)

[releases-shield]: https://img.shields.io/github/release/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant.svg?style=for-the-badge
[releases]: https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/releases
[commits-shield]: https://img.shields.io/github/commit-activity/y/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant.svg?style=for-the-badge
[commits]: https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/commits/main
[license-shield]: https://img.shields.io/github/license/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant.svg?style=for-the-badge
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-blue.svg?style=for-the-badge
[hacs]: https://hacs.xyz
[maintenance-shield]: https://img.shields.io/badge/maintainer-%40mosandlt-blue.svg?style=for-the-badge
[user_profile]: https://github.com/mosandlt
[buymecoffeebadge]: https://img.shields.io/badge/buy%20me%20a%20coffee-donate-yellow.svg?style=for-the-badge
[buymecoffee]: https://buymeacoffee.com/mosandlts
[forum-shield]: https://img.shields.io/badge/community-forum-brightgreen.svg?style=for-the-badge
[forum]: https://community.home-assistant.io/
[stars-shield]: https://img.shields.io/github/stars/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant.svg?style=for-the-badge&color=yellow
[stars]: https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/stargazers

---

## Table of Contents

- [Supported Cameras](#supported-cameras)
- [Disclaimer](#disclaimer)
- [Prerequisites — Setting Up a New Camera](#prerequisites--setting-up-a-new-camera)
- [Installation](#installation)
- [Setup](#setup)
- [Architecture](#architecture)
  - [Network Connectivity](#network-connectivity) — required ports, VLAN/subnet pitfalls
- [Streaming & Reliability](#streaming--reliability)
- [Quality Scale: Platinum](#quality-scale-platinum)
- [Features](#features)
  - [Entities](#entities)
  - [Built-in 3-Step Alert System](#built-in-3-step-alert-system)
  - [AI Snapshot Descriptions](#ai-snapshot-descriptions-opt-in-v1370) — AI Task describes what a camera sees + prompt tips
  - [FCM Push vs Polling](#fcm-push-vs-polling)
  - [SMB/NAS Upload](#smbnas-upload)
  - [Developer Tools — Services](#developer-tools--services)
- [Lovelace Cards](#lovelace-cards)
  - [`bosch-camera-card` — single camera](#bosch-camera-card--single-camera)
  - [`bosch-camera-overview-card` — multi-camera grid](#bosch-camera-overview-card--multi-camera-grid)
  - [Dashboard examples](#dashboard-examples)
  - [Admin / Health Dashboard Example](#admin--health-dashboard-example)
- [Requirements](#requirements)
- [Alarm System / Automation Setup](#alarm-system--automation-setup)
- [Apple HomeKit / Apple Home Integration](#apple-homekit--apple-home-integration)
- [Known Limitations](#known-limitations) — Cloudflare Tunnel tips
- [Roadmap](#roadmap) — parked features and what's under consideration
- [Releases](#releases) · [Full changelog](CHANGELOG.md) · [GitHub Releases](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/releases)
- [Integration Comparison](#integration-comparison) — pick the right project for your platform
- [Related Projects](#related-projects)
- [License](#license)

---

## Supported Cameras

All four current Bosch Smart Home cameras are supported. Click any camera name for the official product page.

| Camera | Generation | Type | Codec / FW seen | Highlights |
|---|---|---|---|---|
| [**360° Indoor**](https://www.bosch-smarthome.com/de/de/produkte/sicherheitsprodukte/360-grad-innenkamera/) | Gen1 | Indoor | H.264 + AAC · FW 7.91.x | Pan/tilt motor, autofollow, IR night vision, mechanical privacy shutter |
| [**Eyes Indoor II**](https://www.bosch-smarthome.com/de/de/produkte/sicherheitsprodukte/eyes-innenkamera-2/) | Gen2 | Indoor | H.264 + AAC · FW 9.40.x | Built-in 75 dB siren, Audio+ glass-break / smoke / CO, ZONES detection mode, RGB LEDs, retractable head (Privacy hardware button) |
| [**Eyes Outdoor**](https://www.bosch-smarthome.com/de/de/produkte/sicherheitsprodukte/eyes-aussenkamera/) | Gen1 | Outdoor (IP66) | H.264 + AAC · FW 7.91.x | Front spotlight, motion-triggered light, ambient-light sensor, schedule-driven illumination |
| [**Eyes Outdoor II**](https://www.bosch-smarthome.com/de/de/produkte/sicherheitsprodukte/eyes-aussenkamera-2/) | Gen2 | Outdoor (IP66) | H.264 + AAC · FW 9.40.x | Front + Top + Bottom RGB LED groups, DualRadar (motion + intrusion), wallwasher mode, mounting-elevation parameter |

> Camera images: see the linked product pages above.

Model-specific timing (pre-warm, heartbeat, retries) is configured automatically — see [Model-Specific Configuration](#model-specific-configuration) for the exact values per generation.

---

## Disclaimer

**This project is an independent, community-developed integration. It is not affiliated with, endorsed by, or connected to Robert Bosch GmbH. "Bosch" and "Bosch Smart Home" are registered trademarks of Robert Bosch GmbH.**

This integration communicates with a reverse-engineered, undocumented API. Provided **"as is"**, without warranty. Use at your own risk. The API may change or be shut down by Bosch at any time. Reverse engineering was performed solely for interoperability under **§ 69e UrhG** and **EU Directive 2009/24/EC**.

---

## Prerequisites — Setting Up a New Camera

Before adding a camera to this integration, it **must** be fully set up in the official **Bosch Smart Camera** app first.

### Step-by-step

1. **Unbox and power on** the camera
2. **Open the Bosch Smart Camera app** and follow the pairing wizard to add the camera to your account
3. **Wait for the firmware update** — new cameras typically receive a Zero-Day update during first setup. This can take **up to 1 hour**. The camera's LED blinks yellow/green during the update.
   - **Do not unplug or restart** the camera during the update
   - If the LED blink pattern doesn't change after 1 hour, leave the camera alone for up to 24 hours ([Bosch Support](https://www.bosch-smarthome.com/de/de/support/hilfe/hilfe-zum-produkt/hilfe-zur-eyes-aussenkamera-2/))
   - The app shows the update status — wait until it reports the camera as ready
4. **Verify the camera works** in the Bosch app — check live stream, settings, and notifications
5. **Then add it to Home Assistant** using this integration (see Installation below)

> **Tip:** If you're replacing an existing camera (e.g. upgrading from Gen1 to Gen2), rename the new camera in the Bosch app to match the old name before setting up the integration. This way Home Assistant creates entities with the expected names.

For more help with camera setup, see:
- [Eyes Outdoor II — Bosch Support](https://www.bosch-smarthome.com/de/de/support/hilfe/hilfe-zum-produkt/hilfe-zur-eyes-aussenkamera-2/)
- [Eyes Indoor II — Bosch Support](https://www.bosch-smarthome.com/de/de/support/hilfe/hilfe-zum-produkt/hilfe-zur-eyes-innenkamera-2/)
- [Firmware update takes a long time — Bosch Community](https://community.bosch-smarthome.com/t5/technische-probleme/wie-lange-dauert-das-update-der-software-bei-mir-l%C3%A4uft-es-seit-%C3%BCber-20-minuten/td-p/71764)

---

## Installation

### HACS (Recommended)

[![Open HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mosandlt&repository=Bosch-Smart-Home-Camera-Tool-HomeAssistant&category=integration)

1. Click the button above (it opens HACS pre-filled with this repo), or in HACS go to **Integrations → ⋮ → Custom repositories** and add `mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant` as type `Integration`.
2. Click **Download** on the Bosch Smart Home Camera entry.
3. Restart Home Assistant.
4. Continue with [Setup](#setup) below.

> **HACS Default listing.** A PR adding this integration to the HACS Default index is pending ([`hacs/default#8181`](https://github.com/hacs/default/pull/8181)). Once merged, the "Custom repositories" step disappears — the integration appears directly under HACS → Integrations → + Explore.

### Manual Installation

If you don't use HACS, copy the integration folder into your HA config:

1. Download the latest release tarball from [Releases](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/releases/latest) (or `git clone` this repo).
2. Copy `custom_components/bosch_shc_camera/` into your HA config directory so the final path is:
   ```
   /config/custom_components/bosch_shc_camera/
   ```
3. Restart Home Assistant.
4. Continue with [Setup](#setup) below.

> **Lovelace card** is auto-registered since **v10.3.19** — the integration serves it from its own bundled `www/` folder. No need to copy `bosch-camera-card.js` to `/config/www/` separately. If you have an old copy from a pre-v10.3.19 install, you can leave it (harmless) or delete it manually: `rm /config/www/bosch-camera-card.js`.

### Removal

To uninstall the integration cleanly:

1. **Settings → Devices & Services → Bosch Smart Home Camera → ⋮ → Delete** — removes the config entry, all camera entities, the device registry, and the auto-registered Lovelace resource.
2. **HACS → Integrations → Bosch Smart Home Camera → ⋮ → Remove** — deletes `custom_components/bosch_shc_camera/`. (For manual installs: `rm -rf /config/custom_components/bosch_shc_camera/`.)
3. **Restart Home Assistant.**

The integration leaves no orphan files in `/config/www/`, no leftover entries in `configuration.yaml`, and no system services running. Downloaded event snapshots/videos in `/config/bosch_events/` (or your configured `download_path`) are **not** auto-deleted — remove them manually if no longer needed.

---

## Setup

### Step 1 — Add the Integration

1. Go to **Settings → Integrations → + Add Integration**
2. Search for **"Bosch Smart Home Camera"**
3. Your browser opens the **Bosch SingleKey ID** login page automatically
4. Log in with your Bosch account (same credentials as the Bosch Smart Camera app)
5. After login, the browser redirects back to Home Assistant automatically — **no manual URL copying needed**
6. The integration discovers all your cameras automatically

> **Token renewal is automatic.** The integration uses a refresh token to silently renew the Bearer token in the background — no manual action needed after initial setup.
>
> **Note:** The automatic redirect uses [my.home-assistant.io](https://my.home-assistant.io). If your HA instance URL is not configured there, you'll be prompted to set it up on first use.

### Step 2 — Configure Settings

Go to **Settings → Integrations → Bosch Smart Home Camera → Configure**. Every setting has a description in the UI; the table below groups them by topic so you can decide which sections to fill in.

#### Event detection

| Setting | Description | Default |
|---|---|---|
| **FCM Push** | Near-instant (~2 s) event detection via Firebase Cloud Messaging. With FCM enabled the polling interval is automatically tightened to 60 s as a backup. | OFF |
| **FCM Push Mode** | `Auto` (FCM with automatic polling fallback on failure) or `Polling` (skip FCM entirely). | Auto |
| **Event check interval** | Polling fallback interval (seconds). Used only when FCM Push is OFF or as a backup. | 300 (5 min) |

#### Alerts (per-step routing)

| Setting | Description | Default |
|---|---|---|
| **Alert services — default fallback** | Notify services used when a per-step service isn't set. Comma-separated list (e.g. `notify.signal_messenger, notify.mobile_app_xxx`). | empty (alerts disabled) |
| **Step 1 — text notification** | Notify services for the instant text alert. Falls back to default. | (default) |
| **Step 2 — snapshot image** | Notify services for the JPG attachment ~3-5 s after the event. | (default) |
| **Step 3 — video clip** | Notify services for the MP4 attachment 30-90 s after the event. | (default) |
| **System alerts** | Notify services for token-expiry / disk-warning system messages. | (default) |
| **Save alert snapshots** | Keep downloaded event images / videos in `www/bosch_alerts/` (inside HA config dir, served at `/local/bosch_alerts/`) after sending. If OFF, files are deleted within seconds. | OFF |

#### SMB / NAS upload

| Setting | Description | Default |
|---|---|---|
| **SMB Upload** | Upload event snapshots + video clips to a SMB/CIFS share (FRITZ!Box NAS, Synology, …). | OFF |
| **SMB Server** | IP or hostname of the share (e.g. `192.168.1.1`). | empty |
| **SMB Share** | Share name (e.g. `FRITZ.NAS`, `cameras`). | empty |
| **SMB Username** / **SMB Password** | SMB authentication credentials. | empty |
| **SMB Base Path** | Base folder on the share (e.g. `Bosch-Cameras`). | empty |
| **SMB Folder Pattern** | Subfolder pattern with `{camera}` / `{year}` / `{month}` / `{day}`. | `{camera}/{year}/{month}/{day}` |
| **SMB File Pattern** | File-naming pattern with `{camera}` / `{date}` / `{time}` / `{type}` / `{id}`. | `{camera}_{date}_{time}_{type}_{id}` |
| **Retention period (days)** | Auto-delete files older than N days. `0` = keep forever. | 180 |
| **Low disk warning (MB)** | Notify when free space on the share drops below this. | 500 |

#### Stream + UI

| Setting | Description | Default |
|---|---|---|
| **Stream connection type** | `Auto` (try LOCAL → fall back to REMOTE), `Local` (LAN only), `Remote` (cloud only). Can also be changed at runtime via the per-camera **Stream Mode** select entity. | Auto |
| **HLS player buffer profile** (`live_buffer_mode`) | Three hls.js buffer presets that trade latency for smoothness: **Latency** (~4-6 s), **Balanced** (~8-10 s, default), **Stable** (~12-15 s). See [HLS Buffer Tuning](#hls-buffer-tuning). | Balanced |
| **Stream quality** | Per-camera quality preset for cloud/REMOTE streams: `Auto` (~7.5 Mbps), `High` (30 Mbps), `Low` (1.9 Mbps). LAN streams always use maximum quality regardless of this setting. Configurable at runtime via the **Video Quality** select entity (persists across restarts). | Auto |
| **Audio default ON** | Whether the per-camera audio switch starts ON (stream with sound) or OFF (muted). | ON |
| **Binary sensors** | Expose Motion / Audio / Person alarm binary sensors (ON for 30 s after each event). | ON |
| **Enable debug logging** | Open Settings → Devices & Services → Bosch Smart Home Camera → "⋮" menu → "Enable debug logging". HA Core auto-restores normal logging on next restart. To make it permanent, use the `logger:` integration in `configuration.yaml`. | — |

### Step 3 — Add a Lovelace Card

Both cards are auto-registered since **v10.3.19** — no manual Lovelace resource entry needed. Pick one of the two cards based on whether you want **one camera per card** or **all cameras in a grid**:

#### One camera (`bosch-camera-card`)

1. Edit dashboard → **+ Add card → Custom: Bosch Camera Card**
2. Pick the camera entity in the visual editor, or paste:

> **Since HA 2026.6** — when you tap **+ Add card** and pick a Bosch `camera.*` entity, both Bosch cards are offered automatically in the Community section, so you no longer need to type `custom:` by hand.

```yaml
type: custom:bosch-camera-card
camera_entity: camera.bosch_garten
title: Garden
```

See the [single-camera card reference](#bosch-camera-card--single-camera) for all options.

#### All cameras at once (`bosch-camera-overview-card`)

1. Edit dashboard → **+ Add card → Custom: Bosch Camera Overview Card**
2. The minimal config has zero options — every Bosch camera is auto-discovered:

```yaml
type: custom:bosch-camera-overview-card
title: Cameras
use_bosch_sort: true
```

See the [overview card reference](#bosch-camera-overview-card--multi-camera-grid) for the grid layout, sort options, and per-camera overrides.

> **Upgrading from pre-v10.3.19?** The integration auto-removes any stale `/local/bosch-camera-card.js` resource entry from Lovelace storage. The physical file in `/config/www/` is intentionally left in place (an integration must not modify user files there) and is harmless either way — the integration loads its own bundled copy.

---

## Architecture

### Components

```mermaid
graph LR
    subgraph User["User"]
        B["Browser / HA Companion"]
    end
    subgraph HA["Home Assistant"]
        Card["Lovelace cards<br/>bosch-camera-card +<br/>bosch-camera-overview-card"]
        Int["bosch_shc_camera<br/>(integration)"]
        Stream["stream component<br/>+ FFmpeg"]
        G2["bundled go2rtc"]
        TLS["TLS proxy<br/>(per camera)"]
    end
    subgraph LAN["Local Network"]
        Cam["Bosch Camera<br/>RTSPS :443"]
    end
    subgraph Cloud["Bosch Cloud"]
        OAuth["SingleKey ID OAuth"]
        API["residential.cbs.<br/>boschsecurity.com"]
        CRP["RTSPS Proxy<br/>proxy-NN.live..."]
    end

    B --> Card
    Card -->|"WS: camera/stream<br/>or webrtc/offer"| Int
    Card -->|"HLS m3u8"| Stream
    Card -->|"WebRTC SDP"| G2
    Int -->|"OAuth"| OAuth
    Int -->|"REST: video_inputs,<br/>connection, lighting…"| API
    Int -->|"spawn"| TLS
    TLS -->|"RTSPS<br/>(plain TCP→TLS bridge)"| Cam
    TLS -.->|"RTSPS<br/>(plain TCP→TLS bridge)"| CRP
    Stream -->|"rtsp://127.0.0.1"| TLS
    G2 -->|"rtsp://127.0.0.1"| TLS
```

Since **v10.3.24** the same Python TLS proxy carries both LOCAL and REMOTE — FFmpeg and go2rtc always connect to `rtsp://127.0.0.1:N`, the proxy decides whether to terminate TLS to the camera (LOCAL) or to the Bosch cloud proxy (REMOTE). Symmetric path means there's no scheme-switching trick (`rtspx://` etc.) on the consumer side, and the cert/hostname mismatch on `proxy-NN.live.cbs.boschsecurity.com` is handled in one place.

### Network Connectivity

Home Assistant must be able to reach each camera's IP on the LAN. The integration auto-discovers the camera IP via the Bosch cloud, but the actual stream/snapshot/RCP traffic flows directly from the HA host to the camera. If a firewall, VLAN boundary, or guest network blocks that path, snapshots stay stale and live streams silently fall back to cloud HLS (slower, internet-routed).

#### Required ports

| Direction | Protocol / Port | Purpose | Required |
|---|---|---|---|
| HA host → camera IP | **TCP/443** | Snapshots, camera REST API, RTSPS live stream (everything tunnels through one TLS connection) | **Yes** |
| HA host → `*.boschsecurity.com` | TCP/443 | OAuth token refresh, REMOTE/cloud fallback stream, FCM push registration | Yes |
| HA host → `fcm.googleapis.com` / `mtalk.google.com` | TCP/5228 | FCM push notifications (auto-falls-back to 30 s polling if blocked) | Optional |
| Browser → HA host | TCP/8123 | Lovelace card, HLS playlist, WebRTC signaling | Yes |
| Browser ↔ HA host | TCP+UDP **8555** | go2rtc WebRTC signaling + media (UDP preferred, falls back to TCP) | Optional, only for WebRTC |

The integration itself **only needs TCP/443 from HA to the camera**. No UDP, no extra ports, no inbound port-forward on your router.

#### Topology

```mermaid
flowchart LR
    Browser["Browser /<br/>HA Companion"]
    HA["Home Assistant<br/>:8123"]
    Cam["Bosch Camera<br/>192.168.x.y"]
    Cloud["Bosch Cloud<br/>*.boschsecurity.com"]
    FCM["Google FCM<br/>:5228"]

    Browser <-->|"HTTPS / WSS<br/>TCP/8123"| HA
    Browser <-.->|"WebRTC<br/>TCP+UDP/8555"| HA
    HA ==>|"REQUIRED<br/>TCP/443<br/>RTSPS + REST"| Cam
    HA -->|"TCP/443<br/>OAuth, cloud fallback"| Cloud
    HA -.->|"TCP/5228<br/>push (optional)"| FCM

    classDef req stroke:#d33,stroke-width:3px;
    class Cam req;
```

#### Common pitfalls

- **Camera in a different subnet/VLAN than HA** — e.g. cameras on `192.168.168.x` and HA on `192.168.1.x`. The router/firewall must allow HA's IP outbound to the camera's IP on TCP/443.
- **IoT/guest network isolation** — many routers (FRITZ!Box "Gastzugang", Unifi guest network) block all LAN-to-LAN traffic by default. Move the camera off the guest network or add an explicit allow-rule.
- **Camera reachable from the Bosch app but not from HA** — the app talks to the camera through the Bosch cloud, so this proves nothing about LAN reachability. The cloud path always works; the LAN path is what matters here.
- **Privacy mode is ON** — the camera shutter is closed and snapshots are deliberately not refreshed. The last frame stays cached. This is by design, not a network issue.

#### Quick check from the HA host

SSH into HA (or use the "Advanced SSH & Web Terminal" add-on) and probe TCP/443 to the camera:

```bash
nc -vz 192.168.x.y 443
# or
curl -k -v --connect-timeout 5 https://192.168.x.y/
```

If both time out or return "connection refused", the issue is between HA and the camera (network/firewall), not the integration. Typical log lines you will see in that case:

```
WARNING ... TLS proxy <ID>: failed to connect to 192.168.x.y:443
            _ssl.c:1063: The handshake operation timed out
WARNING ... LOCAL pre-warm failed for <ID> without REMOTE fallback
DEBUG   ... TLS proxy [C->CAM] pipe error: [Errno 104] Connection reset by peer
```

The integration handles this gracefully — it falls back to cloud HLS so you still get a stream — but for LAN-quality latency and offline operation, fix the network path.

### Stream connection state machine

In **AUTO** mode the integration tries LOCAL first and falls back to the cloud relay only when LAN attempts fail. Since **v10.5.4** the fallback self-heals — once LAN reachability returns, the live stream is migrated back to LOCAL via `Stream.update_source()` without waiting for the user to re-toggle. Both LOCAL and REMOTE sessions are kept inside their respective lifetime bounds by background watchdogs (LOCAL is renewed before relay-side rotation, REMOTE is torn down cleanly ~60 s before the relay's `maxSessionDuration` cap so the consumer never sees a hard reset on the wire).

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Idle

    state "Cloud streaming" as Cloud {
        REMOTE
        REMOTE_fallback
    }

    Idle --> LOCAL: ON · auto + LAN ok
    Idle --> REMOTE: ON · mode = remote
    Idle --> REMOTE_fallback: ON · auto + LAN down

    LOCAL --> REMOTE_fallback: 5 (indoor) / 10 (outdoor)<br/>consecutive stream errors
    REMOTE_fallback --> LOCAL: LAN ok · active promotion

    LOCAL --> Idle: OFF
    Cloud --> Idle: OFF · or lifetime watchdog

    note right of REMOTE_fallback
        Error counter time-decays automatically:
        5 min if LAN reachable, 30 min otherwise.
        Cloud-side errors do not increment the counter.
    end note
```

### REMOTE / Cloud differences

* **`/connection {type:"REMOTE"}`** returns `rtsps://proxy-NN.live.cbs.boschsecurity.com:42090/<hash>` — the Bosch cloud proxy serves the camera over the public internet.
* **TLS proxy is symmetric** (since v10.3.24): the same Python TLS proxy that handles LOCAL also terminates TLS to the cloud proxy for REMOTE. FFmpeg (HLS) and go2rtc (WebRTC) always connect to `rtsp://127.0.0.1:N` — no scheme tricks (`rtspx://`), no per-consumer special-cases. The cert/hostname mismatch on `proxy-NN.live.cbs.boschsecurity.com` (cert SAN only covers `*.residential.connect.boschsecurity.com`) is handled in one place (`verify_mode=CERT_NONE, check_hostname=False`).
* **Snapshots** (`/snap.jpg`) use the cloud-proxy URL directly with HTTP — no TLS proxy needed since they're single-shot HTTP requests, not long-lived RTSP streams.
* **bufferingTime hint** from `PUT /connection` is `1000 ms` for REMOTE (vs `500 ms` for LOCAL) — Bosch's server-side hint about expected latency.

---

## Streaming & Reliability

How the integration brings a live stream up and keeps it alive — connection types, startup timing, watchdogs, the privacy interlock, and the buffer settings that trade latency for smoothness. These are integration / stream internals (configured in the integration's own options), not Lovelace card options.

### Reliability

- **Consistent snapshot refresh** — backend frame interval is shorter than the card's poll interval, so every card request always returns a fresh frame (no jitter).
- **HLS auto-recovery** — hls.js soft errors recover automatically; fatal errors trigger a full reconnect after 2 s. Buffer-stall detection seeks to the live edge on the first two stalls and does a full reconnect on the third (`bosch-camera-card: 3 buffer stalls, reconnecting HLS`).
- **hls.js CDN load hardening** — the card loads hls.js from jsdelivr with a pinned version + subresource-integrity hash (`hls.js@1.6.16` + matching `sha384`). The previous floating `@1` range broke silently whenever jsdelivr shipped a new patch release; updates now require an explicit version + hash bump.
- **Cred-rotation refresh** — Bosch rotates the per-session digest creds on every `PUT /connection LOCAL`. The heartbeat parses each response, caches the new `user`/`password`, rebuilds the cached `rtspsUrl`, and calls `Stream.update_source()` so the next reconnect uses fresh creds. A reactive 401 rescue (max 1 per 5 min per cam) covers the rare cases where the proactive refresh missed a tick. Together they keep AUTO-mode streams on LAN even after long idle gaps (HLS consumer disconnect → reconnect would otherwise hit HTTP 401).
- **Session renewal** — REMOTE proxy hashes expire after ~60 s; the backend opens a new connection before expiry and hands the card a fresh URL via `Stream.update_source()`. LOCAL streams survive the Gen2 Outdoor firmware's ~65 s RTSP TCP reset via a transparent FFmpeg reconnect on the same TLS proxy port with the same Digest credentials (~2 s gap, HLS output continues).
- **TLS-proxy circuit breaker** — when the camera goes physically offline (privacy hardware button, power cut, Wi-Fi drop), the proxy stops retrying after 5 consecutive connect failures within 30 s instead of looping forever. The coordinator decides whether to rebuild via `try_live_connection()` once the camera is reachable again.
- **Hardware-privacy auto-teardown** — when the camera's physical privacy button is pressed (or the Bosch app toggles privacy), the coordinator detects the OFF→ON transition and tears down the live session, the same path as a user-toggle. No more stuck `state: streaming` or endless reconnect loop.
- **"Connecting" badge** — amber badge with fast pulse while HLS is negotiating. Clears to blue "streaming" once video plays. Safety timeout hides the overlay after 120 s if the video never produces a frame, keeping the snapshot visible underneath.
- **Stream uptime counter** — badge shows `00:47` / `1:23` while streaming, updating every 2 s. Proves session renewal keeps the stream alive past 60 s.
- **Frame Δt in debug line** — shows actual ms between frames (`Δ2003ms`) — live verification that 2 s intervals are consistent.
- **Snap error retry** — a failed snap.jpg during streaming triggers one immediate 500 ms retry instead of waiting for the next 2 s timer tick.
- **Connection type badge** — shows "LAN" (green) or "Cloud" (gray) in the header while streaming.

### Stream Connection Types

The integration supports three connection modes, configurable in **Settings → Configure → Stream connection type** or at runtime via the **Stream Mode** select entity:

| Mode | Description |
|------|-------------|
| **Auto** (recommended) | Try local LAN first, automatically fall back to Bosch cloud proxy on failure. |
| **Local** | Direct LAN only — no internet required. Uses a TLS proxy (TCP→TLS + RTSP transport rewrite) since FFmpeg can't handle RTSPS + Digest auth + self-signed cert natively. TCP keep-alive on all proxy sockets. |
| **Remote** | Always via Bosch cloud proxy. Faster snapshots (~0.4–1.9 s). Sessions run for up to 60 minutes; restart with one tap from the live-stream switch. |

### Stream Status Sensor

Every camera gets a `sensor.bosch_{name}_stream_status` entity that exposes the current live stream state as a persistent HA sensor:

| State | Meaning |
|---|---|
| `idle` | Stream is off |
| `warming_up` | LOCAL pre-warm running — waiting for camera encoder to initialise |
| `connecting` | RTSP URL obtained, FFmpeg connecting |
| `streaming` | Active, via local LAN |
| `streaming_remote` | Active, via Bosch cloud proxy |

The card reads this sensor on every `hass` update — so opening a dashboard while the stream is already warming up correctly shows the overlay and snapshot background without needing a toggle click (cold-open fix). You can also use the sensor in automations to react to stream state changes.

### Stream Startup Timing

The card badge progresses `idle` → `warming_up` / `connecting` (yellow) → `streaming` (blue) when you flip the live-stream switch on. How long that first transition takes depends on the connection mode and the camera model — the LOCAL path has a deliberate pre-warm to wake the camera's H.264 encoder before exposing the RTSP URL to FFmpeg, while REMOTE is just a cloud-proxy handshake.

| Camera / mode | Typical time to first frame | Why |
|---|---|---|
| Any camera · **Remote (Cloud)** | **~5–10 s** | `PUT /connection REMOTE` → cloud proxy URL exposed immediately → FFmpeg opens `rtsps://proxy-NN.live.cbs.boschsecurity.com:443/...` → first HLS segment in 3–5 s. No pre-warm. |
| **Gen1 360 Indoor** · Local | ~30–35 s | `min_total_wait = 25 s` from `PUT /connection LOCAL` before the RTSP URL is exposed (`models.py` `INDOOR`), then ~5–10 s for FFmpeg pre-buffer. |
| **Gen2 Eyes Indoor II** · Local | ~30–35 s | Same indoor timing profile (`HOME_Eyes_Indoor`, `min_total_wait = 25 s`). |
| **Gen1 Eyes Outdoor** · Local | ~40–45 s | Outdoor encoder is slower; `min_total_wait = 35 s` + `pre_warm_retries = 8 × 5 s` retry window (`models.py` `OUTDOOR`) + ~5–10 s FFmpeg buffer. |
| **Gen2 Eyes Outdoor II** · Local | ~40–45 s | Same outdoor profile (`HOME_Eyes_Outdoor`). |
| Any camera · **Auto** with working LAN | same as Local | Auto picks LOCAL when LAN is reachable. |
| Any camera · **Auto**, LAN **un**reachable | **~100 s outdoor**, **~40 s indoor**, then + ~5 s for REMOTE | `pre_warm_rtsp()` tries each retry with a ~10 s TLS-handshake timeout plus `pre_warm_retry_wait` between attempts, so the worst case is `pre_warm_retries × (~10 s TLS timeout + pre_warm_retry_wait)`: outdoor `8 × (10 + 5) = ~120 s`, indoor `3 × (10 + 3) = ~39 s`. On exhaustion `_try_live_connection_inner` tears LOCAL down, sets `_stream_fell_back[cam_id]`, and `continue`s to REMOTE (v10.3.2+). Measured end-to-end on a live HA 2026.4.3: patched Gen2 Outdoor target IP to `192.0.2.1` (RFC 5737 TEST-NET) → user-visible fallback after 101 s with `WARNING: LOCAL pre-warm failed … Falling back to REMOTE.`. |
| Any camera · Any mode, **after 2 failed 60-s watchdog ticks** | ~2 min recovery | If FFmpeg opens LOCAL cleanly but the stream goes half-dead later, `_stream_health_watchdog` saturates the error counter on the second failing tick and forces the next `try_live_connection` to REMOTE. Worst-case end-to-end recovery ~2 min (v10.3.2+). |

Renewals after the initial startup take **roughly 2/3** of the `min_total_wait` (camera encoder already warm), so ~17 s indoor, ~23 s outdoor. The TLS proxy can service a re-opened session during that window without user-visible interruption (`Stream.update_source()` hot-swap).

Values are configurable per model in `custom_components/bosch_shc_camera/models.py` if you need to tune them for a slower network or a specific firmware; the defaults above are empirically measured and known-good.

### WebRTC / go2rtc

When [go2rtc](https://github.com/AlexxIT/go2rtc) is available, the card uses **WebRTC** (~2 s latency) instead of HLS (~12 s latency).

**Setup (HA 2024.11+):**
Since Home Assistant 2024.11, go2rtc is **built-in** — no separate add-on or installation needed. Just make sure `go2rtc:` is in your `configuration.yaml` (added by `default_config`). **Do NOT install go2rtc as a separate add-on** — this can cause conflicts.

On stream start, the integration automatically registers the RTSP URL with go2rtc. The card detects WebRTC support and uses it. If WebRTC fails, it falls back to HLS automatically.

**How it works:**
- On stream start, the integration registers the RTSP URL with go2rtc's API (port 1984 inside HA container)
- The card checks `camera/capabilities` — if `web_rtc` is available, it creates an `RTCPeerConnection`
- Full ICE candidate exchange via HA's `camera/webrtc/offer` websocket
- On stream stop, the registration is removed from go2rtc
- If WebRTC fails (go2rtc not running, network issue), falls back to HLS automatically

**Stream looks delayed? You are on HLS, not WebRTC.**
A few seconds of lag is the HLS fallback buffering (~8–12 s) rather than WebRTC (~2 s). The card may also show a small `HLS mode` banner over the video when it deliberately skips WebRTC (see below). Work through this checklist:

1. **Check the building blocks.** WebRTC here needs three core integrations active: `go2rtc`, `webrtc`, and `stream`. On Home Assistant OS / Supervised they are part of `default_config`; on a **Container or Core** install you may have to add them. Settings → Devices & Services — confirm all three are present and not in an error state.
2. **Reload go2rtc after adding the camera.** Settings → Devices & Services → go2rtc → Reload, then hard-refresh the dashboard (`Ctrl`/`⌘`+`Shift`+`R`).
3. **Are you local or remote?** WebRTC needs a path the browser can reach directly. On the **same LAN** it should connect peer-to-peer. **Over the internet** (Nabu Casa / reverse proxy / Cloudflare tunnel) a direct WebRTC path often is not available, so the card intentionally uses HLS and shows the `HLS mode` banner — this is expected and not a bug. Note: opening HA via its *external* URL while sitting at home still counts as remote; use the internal `http://<ha-ip>:8123` URL to get the LAN WebRTC path.
4. **Confirm what is actually happening.** Turn on the integration's debug logging (the integration → Configure → *Debug logging*), open the stream for ~30 s, and look for the `go2rtc` / `TLS proxy` lines. They show whether the WebRTC handshake is attempted and whether it succeeds or falls back.

If WebRTC is loaded, you are on the LAN, and it still falls back, please open a [GitHub issue](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/new) with that debug excerpt, your HA version + install type, and your browser.

### Stream Watchdog

A separate JavaScript resource (`bosch-camera-autoplay-fix.js`) monitors all camera cards and auto-recovers from common issues:

| Issue | Detection | Recovery |
|-------|-----------|----------|
| Chrome autoplay block | Video paused with readyState ≥ 2 | Play muted |
| Dead HLS stream | readyState = 0 for 20 s | Request new HLS URL via `camera/stream` WS |
| Hidden video element | display:none while stream ON | Show video, start HLS |
| Buffer stall | 3 consecutive `bufferStalledError` | Full HLS reconnect |
| Video freeze | `currentTime` unchanged for 15 s | Seek to live edge or restart |

The watchdog gets entity IDs directly from HA states, so it works even when the card's JavaScript is cached.

### Privacy Guard

The **Live Stream switch cannot be turned ON while Privacy Mode is active** (camera shutter is closed). Since v10.4.6 this is enforced at four levels so there's no bypass path:
- `BoschLiveStreamSwitch.available` returns `False` while privacy is on → the entity greys out in the UI.
- An attempted service call raises a `ServiceValidationError` → HA shows a clean toast in the UI, no persistent notification clutter.
- `BoschAudioSwitch._apply_audio_change` and `coordinator.try_live_connection()` both early-exit with a logged warning if privacy is active.
- When privacy gets enabled while a stream is already running — including via the camera's hardware privacy button or the Bosch app — the coordinator detects the OFF→ON transition and tears down the live session automatically (v10.4.10).

### Fast Startup

The first coordinator tick after HA restart **skips events and slow-tier API calls** (WiFi, ambient light, RCP, motion, etc.). This reduces startup from ~2 minutes to ~15 seconds. Full data loads on the second tick (60 s later).

### Model-Specific Configuration

Camera timing and behavior is configured per model via `CameraModelConfig`. Indoor cams keep an active 30 s heartbeat (the cred-refresh path doubles as a session keepalive), while Gen1/Gen2 outdoor cams have heartbeat disabled (`= renewal_interval`) because the Outdoor firmware rotates digest creds on every PUT and would invalidate the running RTSP session.

| Parameter | 360 Indoor (Gen1) | Eyes Indoor II (Gen2) | Eyes Outdoor (Gen1) | Eyes Outdoor II (Gen2) | Purpose |
|---|---|---|---|---|---|
| Heartbeat interval | 30 s | 30 s | 3600 s (≈ off) | 3600 s (≈ off) | PUT /connection keepalive + cred refresh |
| Pre-warm delay | 1 s | 1 s | 2 s | 2 s | Wait before first RTSP DESCRIBE |
| Pre-warm retries | 3 | 3 | 8 | 8 | Max DESCRIBE attempts |
| Min total wait | 25 s | 25 s | 35 s | 35 s | Minimum time before exposing RTSP URL |
| Renewal interval | 3500 s | 3500 s | 3600 s | 3600 s | Proactive session renewal (safety net) |
| Max session duration | 3600 s | 3600 s | 3600 s | 3600 s | Sent in RTSP URL `maxSessionDuration=` (Bosch default hint is 60 s but cams accept 3600) |

### HLS Buffer Tuning

The card's HLS.js configuration is tuned to prevent HA's stream component from killing FFmpeg, and since v10.4.7 it's selectable via the **HLS player buffer profile** (`live_buffer_mode`) option in the integration settings:

| Profile | `liveSync` / `maxLatency` / `maxBuffer` / `maxMaxBuffer` / `lowLatencyMode` | Lag | Trade-off |
|---|---|---|---|
| **Latency** | `3 / 6 / 10 / 20 / true` | ~4–6 s | Lowest delay, may stutter on flaky Wi-Fi |
| **Balanced** *(default)* | `4 / 8 / 14 / 22 / false` | ~8–10 s | Robust against typical Wi-Fi hiccups |
| **Stable** | `6 / 12 / 22 / 30 / false` | ~12–15 s | Smooth even on weak links |

- **`maxBufferLength` cap** — All three modes stay below HA's `OUTPUT_IDLE_TIMEOUT` (30 s). If hls.js buffered ≥ 30 s it would stop requesting segments → HA thinks nobody's watching → kills FFmpeg → freeze.
- **HLS keepalive timer (20 s)** — Periodically calls `hls.startLoad()` as a safety net.
- **SRI integrity hash** — hls.js is loaded from jsdelivr with a pinned `hls.js@1.6.16` + matching `sha384`. Any drift (jsdelivr patch release) blocks the load instead of running an unverified bundle.

The player buffer profile is independent of the **Response** info field on the card, which shows the Bosch-API server-side `bufferingTime` hint (~500 ms LOCAL, ~1000 ms REMOTE) and is unrelated to the client-side hls.js buffer.

## Quality Scale: Platinum

Verified rule-by-rule against the [Home Assistant Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/) — all 52 rules across Bronze, Silver, Gold, and Platinum tiers satisfied. The full per-rule breakdown lives in [`custom_components/bosch_shc_camera/quality_scale.yaml`](custom_components/bosch_shc_camera/quality_scale.yaml).

```mermaid
graph LR
    Bronze["🥉 Bronze<br/>foundation<br/>(18 rules)"] --> Silver["🥈 Silver<br/>stability<br/>(10 rules)"]
    Silver --> Gold["🥇 Gold<br/>comprehensiveness<br/>(21 rules)"]
    Gold --> Plat["💎 Platinum<br/>production-grade<br/>(3 rules)"]

    style Bronze fill:#cd7f32,color:#fff
    style Silver fill:#a0a0a0,color:#fff
    style Gold fill:#ffd700,color:#000
    style Plat fill:#a0c4ff,color:#000,stroke-width:3px
```

**What the Gold tier delivers (visible to users):**

- **Diagnostics download** — Settings → Devices & Services → ⋮ → *Download diagnostics* returns a redacted JSON snapshot (config + coordinator + per-camera summary). Tokens, FCM credentials, private keys, MAC addresses are auto-redacted.
- **Repair-issues UI** — Token-expired and Bosch-auth-server-outage states surface under Settings → System → Repairs with full description and severity, instead of silent persistent notifications. Auto-clear when the issue resolves.
- **Reconfigure flow** — Integration card menu → ⋮ → *Reconfigure* runs the OAuth login again and updates the same config entry in place. Entities, automations, FCM/SMB options preserved.
- **Translatable service-action exceptions** — All 50+ `HomeAssistantError` / `ServiceValidationError` raises route through `translation_key` + placeholders. German-locale users see localized messages.
- **Auto-cleanup of stale devices** — Cameras removed from your Bosch account (via the Bosch Smart Camera app) are now automatically removed from the HA device registry on the next coordinator tick.
- **Icon translations** — All entity icons (including conditional state-based icons like privacy on/off, stream-status, FCM push status) live in `icons.json` instead of hardcoded `_attr_icon` values.

**Architectural improvements (invisible but real):**

- **`ConfigEntry.runtime_data`** instead of `hass.data[DOMAIN][entry_id]` — auto-cleanup on unload, no half-state race window.
- **Service-action exceptions instead of silent log warnings** — clicking a button that hits HTTP 500 now shows a red error notification with cause; previously failures looked like nothing happened.
- **Coordinator raises `UpdateFailed`** consistently → HA framework provides `log-when-unavailable` automatically, no manual log-spam guards.

**Platinum tier (v12.0.1):**

- `strict-typing` — mypy --strict green across all 24 source files (was 593 errors). `pyproject.toml` strict config added; ~80 targeted `# type: ignore` for unavoidable HA-stub gaps.
- `async-dependency` — all `requests` imports removed. HTTP Digest auth via `auth_utils.async_digest_request` (aiohttp); sync cloud downloads via stdlib `urllib.request`. `requests>=2.28.0,<3` removed from runtime requirements.

### Operational reliability — token recovery flow

The Gold-tier `repair-issues` + `reconfiguration-flow` rules combine into a self-service token recovery path that no longer requires deleting and re-adding the integration:

```mermaid
sequenceDiagram
    participant Coord as Coordinator
    participant IR as Issue Registry
    participant UI as Settings → Repairs
    participant User
    participant CF as Config Flow
    participant Bosch as Bosch SingleKey ID

    Note over Coord: token refresh fails<br/>3× consecutive
    Coord->>Coord: raise ConfigEntryAuthFailed
    Coord->>IR: ir.async_create_issue("token_expired", severity=CRITICAL)
    IR-->>UI: visible in repairs panel
    User->>UI: click "Reconfigure"
    UI->>CF: async_step_reauth_confirm
    CF->>Bosch: open OAuth in browser
    Bosch-->>CF: redirect with authorization code
    CF->>CF: async_oauth_create_entry → async_update_reload_and_abort
    CF-->>Coord: entry reloaded, tokens persisted
    Coord->>IR: ir.async_delete_issue("token_expired")
    IR-->>UI: issue cleared
```

Same flow handles the Bosch-side `auth_server_outage` issue: the coordinator surfaces a separate non-fixable repair issue (severity ERROR) with the Bosch HTTP status, then auto-clears it when the next refresh succeeds.

### Service-action error visibility

```mermaid
flowchart LR
    Click[User clicks button<br/>e.g. set_motion_zones]
    Click --> Validate{Input valid?}
    Validate -- "no" --> SVE["raise ServiceValidationError<br/>translation_key: argument_required<br/>placeholders: {argument: zones}"]
    Validate -- "yes" --> API[Bosch cloud API PUT]
    API --> Resp{HTTP status}
    Resp -- "200/204" --> OK[refresh coordinator]
    Resp -- "443 privacy" --> Privacy["raise HomeAssistantError<br/>translation_key: privacy_blocked"]
    Resp -- "5xx / network" --> HAE["raise HomeAssistantError<br/>translation_key: http_error_with_body"]
    SVE --> Notif[HA shows red error<br/>localized to user's language]
    Privacy --> Notif
    HAE --> Notif

    style Notif fill:#ff6961,color:#fff
    style OK fill:#77dd77,color:#000
```

Before v11.0.0 these failure paths logged a WARNING and silently returned — clicking a button that hit HTTP 500 looked like nothing happened. Now every failure surfaces a translatable red notification with the cause.

---

## Features

- **Cloud-maintenance banner + lifecycle alerts (v12.4.7/v12.4.8):** detects Bosch's announced maintenance windows from the community RSS feed; fires notifications on scheduled → active → past transitions; cameras send online/offline transition alerts.
- **LAN-fallback during cloud outage (v12.4.10):** privacy and front-light switches stay available as long as the camera is Gen2 and reachable on the LAN. A persistent LAN-IP store and outage-ping sweep keep `binary_sensor.*_lan_reachable` current. The overview-card renders per-camera LAN-status tiles with clickable privacy/light controls while the cloud is down.
- **Cloud up/down transition alerts (v12.4.11):** the coordinator announces cloud-health transitions via the alert pipeline. Requires ≥60 s of continuous failure before announcing; suppressed during RSS-announced maintenance windows.

```mermaid
sequenceDiagram
    participant Card as Lovelace Card
    participant HA as Home Assistant
    participant Coord as Coordinator
    participant RSS as Bosch Community RSS
    participant MP as maintenance.py
    participant Sensor as BoschCloudMaintenanceSensor

    HA->>Coord: hourly tick (+ reactive on 5xx)
    Coord->>RSS: fetch Wartungsarbeiten + Statusmeldungen
    RSS-->>MP: raw feed entries (+ HTML fallback)
    MP-->>MP: parse title / time window / link
    MP-->>Sensor: state: active / scheduled / past / recent / unknown / idle
    Sensor-->>Card: hass update
    Card-->>Card: show banner (state in {active,scheduled} AND camera_relevant=true)
```

```mermaid
sequenceDiagram
    participant User
    participant Switch as switch.privacy_mode<br/>switch.camera_light
    participant Coord as Coordinator
    participant Cloud as Bosch CBS API
    participant RCP as Camera LAN RCP
    participant Cam as Camera 192.168.x.y:443

    User->>Switch: toggle
    Switch->>Coord: async_turn_on/off
    Coord->>Cloud: PUT /v11/.../privacy or lighting
    Cloud-->>Coord: 5xx (cloud outage)
    Coord->>Coord: lan_reachable? + Gen2?
    Coord->>RCP: RCP write (0x0d00 privacy / 0x0c22 light)
    RCP->>Cam: HTTPS Digest + RCP payload
    Cam-->>RCP: 200 OK
    RCP-->>Coord: success (local fallback)
```

```mermaid
sequenceDiagram
    participant Coord as Coordinator
    participant Ping as outage-ping sweep
    participant Cam as Cameras (LAN)
    participant Alert as alert pipeline
    participant Notify as notify.alert_notify_system

    Coord->>Coord: tick — cloud GET fails
    Coord->>Ping: fan-out TCP-connect :443 (all cams)
    Ping-->>Coord: lan_reachable per camera
    Coord->>Coord: consecutive_failures ≥ 60 s?
    Coord->>Coord: maintenance window active? → suppress
    Coord->>Alert: cloud_down event
    Alert->>Notify: "Cloud unreachable — LAN active"
    Note over Coord,Notify: recovery: same path, cloud_up event
```

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
| Stream quality | `select` | Auto (~7.5 Mbps) / High (30 Mbps) / Low (1.9 Mbps). LAN always uses max quality; setting only affects REMOTE/cloud streams. Persists across restarts. |
| Stream mode | `select` | Auto (Local → Cloud) / Local only / Cloud only |
| Motion sensitivity | `select` | SUPER_HIGH / HIGH / MEDIUM_HIGH / MEDIUM_LOW / LOW / OFF |
| FCM Push mode | `select` | Auto / Polling |
| Motion detected | `binary_sensor` | disabled by default |
| Audio alarm detected | `binary_sensor` | disabled by default |
| Person detected | `binary_sensor` | disabled by default |
| Unread events count | `sensor` | disabled by default |
| Privacy sound (360 only) | `switch` | enabled (config category) |
| Commissioned status | `sensor` | diagnostic, disabled by default |
| Trigger siren (Gen2 only) | `switch` | disabled by default — stateful ON/OFF via `PUT /panic_alarm`. Gen1 hardware has no integrated siren and gets no entity. |
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
| Front light with color temperature | `light` | Gen2 only |
| Top LED light with RGB color picker | `light` | Gen2 only |
| Bottom LED light with RGB color picker | `light` | Gen2 only |
| Status LED on/off | `switch` | Gen2 only |
| Motion-triggered lighting on/off | `switch` | Gen2 only |
| Ambient/permanent lighting on/off | `switch` | Gen2 only |
| DualRadar intrusion detection on/off | `switch` | Gen2 only |
| Mounting height (meters) | `number` | Gen2 only |
| Microphone recording level (0–100%) | `number` | Gen2 only |
| Front light color temperature | `number` | Gen2 only |
| Top LED brightness (0–100%) | `number` | Gen2 only |
| Bottom LED brightness (0–100%) | `number` | Gen2 only |
| Motion light sensitivity (1–5) | `number` | Gen2 only |

> **RCP diagnostic sensors** are disabled by default. Enable them in entity settings to inspect camera firmware capabilities. Gen2 cameras will automatically expose new alarm types and analytics modules.

> **SHC local API is not needed.** All features work with just the Bosch cloud API.

### Built-in 3-Step Alert System

No automations needed — the integration sends alerts directly:

1. **Instant text:** `📷 Camera: Motion (10:31:56)` — sent immediately
2. **Snapshot image:** `📸 Camera Snapshot` + JPEG — sent ~5s later
3. **Video clip:** `🎬 Camera Video (245 KB)` + MP4 — sent ~30-90s later (polls until Bosch uploads the clip)

```mermaid
flowchart TD
    Cam[Camera<br/>motion / person / audio] -->|FCM push or polling| Int[Integration<br/>event handler]
    Int --> Gate{notification<br/>switches}
    Gate -->|master OFF or<br/>type-specific OFF| Skip[suppress alerts<br/>event still tracked]
    Gate -->|enabled| S1
    S1["Step 1: instant text<br/>📷 Motion HH:MM:SS"] -->|~5 s| S2
    S2["Step 2: snapshot JPEG<br/>📸 inline image"] -->|poll until clip ready<br/>~30–90 s| S3
    S3["Step 3: video clip MP4<br/>🎬 attachment"]
    S1 -.->|alert_notify_information<br/>or alert_notify_service| Notify1["notify.signal_messenger<br/>notify.mobile_app_xxx<br/>..."]
    S2 -.->|alert_notify_screenshot<br/>opt-in only| Notify2["notify.signal_messenger<br/>notify.mobile_app_xxx"]
    S3 -.->|alert_notify_video<br/>opt-in only| Notify3[notify.signal_messenger]
```

Steps 2 and 3 also include a clickable Media Browser link to that day's event folder (`📂 https://…`). Uses the configured external HA URL when set so the link works from outside the LAN; points to the SMB backend when SMB upload is enabled, otherwise the local backend.

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

**iOS + Android Companion App** (`mobile_app_*`): snapshot appears directly inside the push notification as an inline image. Files are temporarily saved to `www/bosch_alerts/` (served as `/local/bosch_alerts/`) and auto-deleted within seconds after sending. Signal and others receive a file path attachment instead.

**Notification switch guard (v7.9.1+):** Alerts respect the notification switches — if `switch.bosch_{name}_notifications` (master) is OFF, no alerts are sent. Type-specific switches (`movement_notifications`, `person_notifications`, `audio_notifications`) are also checked. The FCM push is still received (for event tracking), but the HA notification is suppressed.

### AI Snapshot Descriptions (opt-in, v13.7.0+)

Let Home Assistant's **AI Task** describe what a camera sees — in plain language, in your language. A description can be generated automatically on a motion/person event, or on demand via the `describe_snapshot` service. The result is exposed as a per-camera sensor and can optionally be appended to your event notifications ("Person at the front door — *a delivery courier is placing a parcel on the doormat*").

It is **off by default**. Nothing is sent to any model until you enable it, and **privacy mode always blocks analysis** (a blanked frame is never analysed).

> Requires an [AI Task](https://www.home-assistant.io/integrations/ai_task/) entity in Home Assistant — e.g. Google Generative AI, OpenAI, Ollama, or any provider that exposes `ai_task.generate_data` with image support.

#### How it works

```mermaid
flowchart TD
    EV[Motion / person event] -->|ai_describe_on_motion = on| P
    SVC["describe_snapshot service<br/>(manual / automation)"] -->|force: bypasses window + cooldown| AI

    P{Privacy mode?} -->|ON| STOP[Skip — never analyse a blanked frame]
    P -->|OFF| W{Active-time window?}
    W -->|inside / not set| C{Presence / condition entity matches?}
    C -->|yes / not set| R{Cooldown elapsed AND daily budget left?}
    R -->|no| CACHE[Skip — reuse last description if fresh]
    R -->|yes| AI

    AI["ai_task.generate_data<br/>current snapshot + your prompt + language"] --> SENS["sensor.bosch_CAM_ai_description"]
    AI --> NOTIF[Optional: appended to the event notification]
    AI --> BUS["Event: bosch_shc_camera_ai_description"]
```

The gates exist to keep the feature **cheap and quiet**: a per-camera **cooldown**, a **daily budget**, an optional **active-time window** (e.g. only at night), and an optional **presence/condition gate** (e.g. only when `person.you` is `not_home`). Automatic calls respect all gates; a manual `describe_snapshot` call bypasses the window and cooldown but still counts toward the daily budget and is still privacy-guarded.

#### Setup

**Settings → Devices & Services → Bosch Smart Home Camera → Configure → AI descriptions:**

| Option | Default | What it does |
|---|---|---|
| `enable_ai_description` | off | Master switch — creates the sensor + service |
| `ai_task_entity` | *(empty)* | Which AI Task entity to use; empty = HA's preferred one |
| `ai_describe_prompt` | *(security prompt)* | The instructions sent with every snapshot (see below) |
| `ai_describe_language` | `Deutsch` | Reply language (free text, e.g. `English`, `Français`) |
| `ai_describe_on_motion` | off | Auto-describe on each motion/person event |
| `ai_notify_include_description` | off | Append the description to the event notification |
| `ai_cooldown_seconds` | `60` | Minimum seconds between auto-descriptions per camera |
| `ai_max_per_day` | `100` | Daily cap across all cameras (`0` = unlimited) |
| `ai_active_time_start` / `_end` | *(empty)* | Only auto-describe within this `HH:MM` window (overnight ranges OK) |
| `ai_active_condition_entity` / `_state` | *(empty)* / `not_home` | Only auto-describe while this entity is in this state |

#### The `describe_snapshot` service

Returns the text in the response, so you can use it anywhere (a dashboard, a TTS announcement, your own automation):

```yaml
action: bosch_shc_camera.describe_snapshot
data:
  camera_id: bosch_terrasse        # or: entity_id: camera.bosch_terrasse
  # optional per-call overrides:
  instructions: "Is a parcel visible? Answer yes or no and where."
  language: English
response_variable: result
# result.description now holds the text
```

#### Optimising the prompt

The prompt is the single biggest lever on quality and cost. The shipped default is tuned for **security relevance only** — it reports people/vehicles/animals/parcels and is told to ignore scenery and not to guess. Tips when writing your own:

- **Say what to report AND what to ignore.** Vision models love to narrate furniture, weather and architecture. An explicit "describe ONLY …; do NOT describe the room/scenery/image quality" keeps answers short, useful and cheap (fewer output tokens).
- **Pin the negatives.** Models guess "parcel" from doormats, floor tiles and shadows. List those explicitly as *not* a parcel.
- **Define the empty case.** Tell it exactly what to say when nothing relevant is visible (e.g. *"Keine sicherheitsrelevanten Beobachtungen"*) so downstream automations can match on it.
- **Keep it short.** A shorter prompt + a short-answer instruction = lower latency and lower per-call cost; auto-mode runs on a 20 s timeout.
- **Tailor per camera via the service.** Use `instructions:` on a `describe_snapshot` call for camera-specific framing ("ignore the public street, report only the driveway and porch").
- **Set the language explicitly** with `ai_describe_language` — the integration also appends a bilingual "answer only in <language>" directive so the model doesn't drift back to its default.

<details>
<summary>Shipped default prompt (German, security-focused)</summary>

> Du bist eine Überwachungskamera-Assistenz. Melde NUR sicherheitsrelevante Beobachtungen: Personen (auch nur teilweise sichtbar: Beine, Arme, Silhouette, Schatten), Fahrzeuge, Tiere, Pakete oder ungewöhnliche Aktivität. Beschreibe NICHT die Umgebung, Räume, Möbel, Architektur oder Bildqualität und benenne KEINE Orte. Rate nicht: Fußmatten, Teppiche, Bodenfliesen und Schatten sind kein Paket. Wenn nichts Sicherheitsrelevantes erkennbar ist, sage das kurz, z. B.: Keine sicherheitsrelevanten Beobachtungen.

</details>

### Mark-as-Read & Last Event Fast-Path

Events are automatically **marked as read** after alert processing or download. This uses `PUT /v11/events/bulk` for batch updates and `PUT /v11/events` (with `{"id": ..., "isRead": true}`) for individual events, keeping the unread count in sync with the Bosch Smart Camera app.

On **startup**, the integration marks all currently unread events as read — clearing any backlog that accumulated while HA was offline.

The integration uses `GET /v11/video_inputs/{id}/last_event` as a **fast-path** to check for new events before fetching the full event list. This reduces unnecessary API calls — the full event list is only fetched when the last event has actually changed.

### FCM Push vs Polling

| | FCM Push (recommended) | Polling (default) |
|---|---|---|
| **Event latency** | ~2-3 seconds | 5 minutes (configurable) |
| **How it works** | Firebase Cloud Messaging push from Bosch cloud | Periodic `/v11/events` API polling |
| **Fallback** | Automatic — if FCM goes down, polling continues | Always active |
| **Status sensor** | `sensor.bosch_camera_event_detection` = `fcm_push` | `polling` |

Enable FCM Push in **Settings → Configure → FCM Push**. Two modes: `Auto` (FCM with automatic polling fallback on failure) or `Polling` (skip FCM entirely). Can be changed at runtime via the **FCM Push Mode** select entity.

#### State machine — registration, push delivery, self-heal, polling fallback

The integration always runs the 60-second coordinator poll for camera status (online/offline, privacy mode, firmware) — that part is independent of FCM. FCM is only for **event** detection (motion / audio / person), which is the latency-critical path. When FCM is healthy the watchdog stretches event polling to 5 min; when FCM is unhealthy it tightens back to 60 s.

```mermaid
stateDiagram-v2
    [*] --> Init
    Init --> Registering : asyncio.Lock acquired<br/>fresh checkin_or_register()
    Registering --> Active : token registered with Bosch CBS
    Registering --> Polling : PHONE_REGISTRATION_ERROR<br/>or library failure
    Active --> EventReceived : push from Bosch CBS
    EventReceived --> Active : event fetched + alert pipeline
    Active --> Polling : is_started=False<br/>(listener silently terminated)
    Polling --> SoftHeal : watchdog trigger<br/>(a) is_started=False<br/>(b) >=3 failures in 5 min<br/>(c) not _fcm_running
    SoftHeal --> Active : socket restart succeeds<br/>real push received → streak reset
    SoftHeal --> SoftHeal : consecutive soft-heals<br/>with no real push (streak++)
    SoftHeal --> HardHeal : streak >= 3<br/>(escalate)
    HardHeal --> Cooldown : purge ALL fcm_* keys<br/>fresh FCM registration<br/>new token · streak = 0
    Cooldown --> Registering : ladder elapsed<br/>30 min / 1 h / 3 h / 6 h / 24 h<br/>+- 20% jitter
    Cooldown --> Paused : 5 consecutive failures
    Paused --> Polling : polling fallback continues
    Paused --> [*] : HA restart resets counter
    Polling --> PollCycle : every 60 s scan_interval
    PollCycle --> EventReceived : new event in /v11/events
    PollCycle --> Polling : no new event
    Active --> CounterReset : healthy >=10 min<br/>after self-heal
    CounterReset --> Active : failures = 0
```

**Recovery behaviour (since v13.5.12):**

- **Two-stage self-heal:** a **soft-heal** first restarts the FCM socket (preserving credentials). After **3 consecutive soft-heals with no real push received**, the watchdog escalates to a **hard-heal**: all `fcm_*` keys are purged, a fresh FCM registration is performed, and a new device token is obtained. The soft-heal streak counter resets whenever a real incoming push is received or a hard-heal completes.
- **Exponential cool-down ladder** with ±20 % jitter: 30 min → 1 h → 3 h → 6 h → 24 h. After 5 consecutive failures self-heal pauses until the next HA restart; polling fallback keeps event detection alive.
- **Storm trigger watches registration failures, not just connectivity** — `PHONE_REGISTRATION_ERROR`, "Unable to complete gcm auth request", and "Unable to establish subscription" all count toward the `≥ 3 in 5 min` threshold.
- **`asyncio.Lock` serialises start + self-heal** so the setup-time registration cannot race the first watchdog tick (the race used to register two device tokens with Bosch CBS in 2 s and orphan the first listener).
- **Failure counter auto-resets** after FCM stays healthy for ≥ 10 min following a heal — so a one-off blip doesn't burn through the ladder.

Full diagnostic dump available via integration **Settings → Configure → Debug logging** when troubleshooting; lessons-learned write-up: `knowledge-base/fcm-self-heal-lessons.md`.

### SMB/NAS Upload

Upload event snapshots and video clips directly to a SMB/CIFS network share (FRITZ!Box NAS, Synology, any Windows share, etc.). Disabled by default.

**How it works:**
- When an event is detected (via FCM push or polling), the integration downloads the snapshot and video clip
- Files are uploaded to the configured SMB share using the folder and file naming patterns
- Supports any SMB-compatible NAS or router with USB storage (FRITZ!Box, Synology, QNAP, Windows shares)

**Configuration:** Go to **Settings → Integrations → Bosch Smart Home Camera → Configure** and enable **SMB Upload**. Then fill in the server, share, and credentials.

**Folder pattern variables:** `{camera}`, `{year}`, `{month}`, `{day}`
**File pattern variables:** `{camera}`, `{date}`, `{time}`, `{type}`, `{id}`

Example file path on NAS (default camera-first layout):
```
\\192.168.1.1\FRITZ.NAS\Bosch-Cameras\Garden\2026\03\25\Garden_2026-03-25_14-32-05_MOVEMENT_abc123.jpg
\\192.168.1.1\FRITZ.NAS\Bosch-Cameras\Garden\2026\03\25\Garden_2026-03-25_14-32-05_MOVEMENT_abc123.mp4
```

> Requires the `smbprotocol` Python package, which is auto-installed via `manifest.json`.

#### FRITZ!Box NAS Setup

To use your FRITZ!Box as a NAS for camera event storage:

1. **Enable NAS on FRITZ!Box:**
   - Open `http://fritz.box` → **Home Network → USB / Storage → USB Storage**
   - Enable **Storage (NAS) active**
   - Note the share name (default: `FRITZ.NAS`)

2. **Create a FRITZ!Box user with NAS access:**
   - **System → FRITZ!Box Users → Add user**
   - Give the user a username and password
   - Under **Permissions**, enable **Access to NAS contents**

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
   | SMB Base Path | Folder on NAS | `Bosch-Cameras` |
   | SMB Folder Pattern | Subfolder structure | `{camera}/{year}/{month}/{day}` |
   | SMB File Pattern | File naming | `{camera}_{date}_{time}_{type}_{id}` |
   | Retention (days) | Delete files older than N days | `180` (6 months) |
   | Low disk warning (MB) | Alert below this free space | `5120` (5 GB) |

4. **Verify:** After the next camera event, check your NAS at `FRITZ.NAS/Bosch-Cameras/` — snapshots (.jpg) and video clips (.mp4) should appear automatically.

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

### Media Browser

Once events are being saved — either to the local save folder or to a NAS via SMB/FTP upload — they appear under **Media → Bosch SHC Camera** in HA's built-in media browser. No extra setup needed; all configured backends are shown automatically.

**To enable the local backend:**

1. Settings → Devices & Services → **Bosch Smart Home Camera** → ⚙ **Configure** (NOT *Reconfigure* — that's for re-OAuth only).
2. Scroll to the **Events & Storage** section.
3. Enable **"Save events locally"**.
4. Set the **"Local save folder"** path, e.g. `/config/bosch_events` (pre-filled by default).
5. **Submit**. New events are saved to that folder automatically. The directory is created on first use.

> **Note:** Local saving is **disabled by default** and must be explicitly enabled. Only events that arrive *after* enabling are saved — there is no bulk download of historical clips.

**What the Media Browser shows**

All configured backends appear side by side — no filter option needed:

| Backend | When shown |
|---------|-----------|
| **Local** | `Local save folder` is set (even if local saving is currently off — already-saved files remain visible) |
| **NAS** | `Upload to NAS` is enabled and server + share are configured (works with both SMB and FTP upload protocols) |
| **Recordings** (NVR) | `Mini-NVR` is enabled |

**Tree shape**
- *Camera-first (default):* `Camera → Year → Month → Day → Event`
- *Year-first (legacy):* `2026 → Month → Day → Event` — folders named with a 4-digit year at the NAS/local root are automatically detected and fully browseable without any restructuring
- *NVR:* `Camera → Date → Event`

Each event shows `HH:MM:SS — TYPE (Camera)`, e.g. `09:15:23 — MOVEMENT (Garden)`. MP4 clips play inline with HTTP `Range` support so the player can seek; the matching JPEG snapshot doubles as a thumbnail. If only a JPEG exists for an event (no video clip), the image is shown directly. macOS resource-fork files (`._*`) are filtered out.

Files are served by an authenticated `/api/bosch_shc_camera/event/…` view; path-traversal is blocked, only `image/jpeg` and `video/mp4` are returned. NAS files are streamed on demand via `smbprotocol` — no local cache, no HA disk usage.

### Mini-NVR — Local Continuous Recording (BETA)

Continuously records the LOCAL stream to disk in 5-minute wall-aligned segments. No cloud involved. Recording stops automatically when the camera falls back to REMOTE (relay).

```mermaid
flowchart LR
    Cam[Camera RTSP<br/>local TLS] --> Proxy["TLS proxy<br/>127.0.0.1:N"]
    Proxy --> FFmain[FFmpeg main<br/>5 min segments]
    FFmain -->|writes| Stage["Staging tree<br/>_staging/cam/day/HHMMSS.mp4"]
    Proxy -.->|optional preroll| FFpre[FFmpeg preroll<br/>10 s segments]
    FFpre -.->|tmpfs| Cache["/dev/shm/bosch_nvr_cache/<br/>cam/HHMMSS.mp4"]
    Stage --> Drain[Drain watcher<br/>tick every 30 s]
    Drain -->|target = local| Local["base/cam/day/<br/>finalized segments"]
    Drain -->|target = smb / ftp| SMB[SMB or FTP share]
    Cache -.->|FCM motion event| Concat[concat preroll + main<br/>→ motion clip]
    Concat --> Local
    Local --> MB[HA Media Browser]
    SMB --> MB
    Drain -.->|state| Sensor[sensor.bosch_x_mini_nvr_status<br/>idle / recording / error]
```


> **BETA:** The NVR switch becomes unavailable whenever the camera is on a REMOTE connection. A live stream must be active (via `switch.bosch_{name}_live_stream`) for the TLS proxy to start before recording can begin.

#### Enable

1. Settings → Devices & Services → **Bosch Smart Home Camera** → Configure
2. Scroll to the **NVR** section and enable **Mini-NVR**
3. Set **Base path** (default: `/config/bosch_nvr`)
4. Set **Retention** in days (default: 3 days)
5. Choose **Storage target**: `local`, `smb`, or `ftp`

#### NVR Entities

These entities are **disabled by default** — enable them individually in the entity settings:

| Entity | Description |
|--------|-------------|
| `switch.bosch_{name}_mini_nvr` | Turn recording ON/OFF. Unavailable when camera is on REMOTE connection. |
| `sensor.bosch_{name}_mini_nvr_status` | State: `idle` / `recording` / `error` |

`sensor.bosch_{name}_mini_nvr_status` attributes: `target`, `pending_uploads`, `failed_uploads`, `last_segment_age_s`, `preroll_segments`, `preroll_running`.

#### Recording Quality

Set `nvr_quality` in the configure dialog:

| Value | Bitrate | Notes |
|-------|---------|-------|
| `auto` | ~30 Mbps | Default. Full-resolution stream. LOCAL only. |
| `low` | ~1.9 Mbps | Sub-stream. Reduces disk usage significantly. |

#### Pre-Roll Buffer

Set `nvr_preroll_seconds` to a value between 1 and 60 to keep a RAM-based ring buffer of the last N seconds before a motion event.

- Uses 10-second segments stored at `nvr_preroll_cache_dir`
- Default path: `/dev/shm/bosch_nvr_cache` — inside the HA container namespace, not visible via SSH
- Change to `/config/bosch_nvr_preroll` if you want the cache on shared storage
- On FCM motion push, the cached segments are prepended to the recorded clip automatically

Set to `0` (default) to disable pre-roll.

#### File Layout

```
/config/bosch_nvr/
├── Terrace/               ← finalized segments
│   └── 2026-05-08/
│       ├── 21-07.mp4      ← wall-aligned 5-min segments
│       └── 21-10.mp4
└── _staging/              ← FFmpeg writes here first (incomplete segments)
    └── Terrace/2026-05-08/
```

Segments are moved from `_staging/` to the final tree only when complete. Incomplete segments from a crash or restart are cleaned up on next startup.

#### Lovelace Cards (BETA)

Two new card types ship with the integration:

**Timeline card** — 24-hour canvas timeline with motion events, click to seek:

```yaml
type: custom:bosch-nvr-timeline-card
camera_entity: camera.bosch_terrasse
```

Optional: add `date: "2026-05-08"` to show a specific day (default: today).

**Multi-camera card** — stacked view with a shared seek bar and drift correction:

```yaml
type: custom:bosch-nvr-multicam-card
cameras:
  - camera.bosch_terrasse
  - camera.bosch_innenbereich
```

#### Event-Buffer-Only Mode

Set `nvr_event_only: true` in the options to run **only** the pre-roll ring buffer without 24/7 continuous recording. FFmpeg writes 10-second segments to the cache; on FCM motion the cached segments are assembled into a clip. Disk usage is negligible (a few MB per camera).

Requires `nvr_preroll_seconds > 0`. The main continuous recorder is skipped — no 5-minute segments are written to `nvr_base_path`.

#### Automation Examples

**Away-mode: start recording when leaving home**

```yaml
alias: "NVR when away"
trigger:
  - platform: state
    entity_id: person.thomas
    to: "not_home"
action:
  - service: switch.turn_on
    target:
      entity_id: switch.bosch_terrasse_mini_nvr
```

**Alarm mode: arm all NVR switches with the alarm panel**

```yaml
alias: "NVR with Alarm System"
trigger:
  - platform: state
    entity_id: alarm_control_panel.home_alarm
    to:
      - armed_away
      - armed_night
action:
  - service: switch.turn_on
    target:
      entity_id:
        - switch.bosch_terrasse_mini_nvr
        - switch.bosch_innenbereich_mini_nvr
```

**Disarm: stop recording when coming home**

```yaml
alias: "NVR off when home"
trigger:
  - platform: state
    entity_id: alarm_control_panel.home_alarm
    to: disarmed
action:
  - service: switch.turn_off
    target:
      entity_id:
        - switch.bosch_terrasse_mini_nvr
        - switch.bosch_innenbereich_mini_nvr
```

**Motion-only clip via sensor attribute** (verify pre-roll is running before sending alert)

```yaml
alias: "Check Pre-Roll before Alarm"
trigger:
  - platform: event
    event_type: bosch_shc_camera_motion
    event_data:
      device_id: "YOUR_DEVICE_ID"
condition:
  - condition: template
    value_template: >
      {{ state_attr('sensor.bosch_terrasse_mini_nvr_status', 'preroll_running') == true }}
action:
  - service: notify.signal_kamera
    data:
      message: "Motion detected – Pre-Roll active, clip being created."
```

→ More examples: [`examples/automations/`](examples/automations/) — bilingual EN+DE, covers presence, alarm, privacy, NVR, and notification patterns.

#### Limitations

- LOCAL connection only — no cloud fallback recording
- NVR switch shows `unavailable` when camera is on REMOTE
- Pre-roll cache at `/dev/shm/…` is inside the HA container and not visible via SSH; change to `/config/…` if direct access is needed
- Live stream switch must be ON for the TLS proxy (and thus FFmpeg) to start

---

### HA Events

The integration fires events on the HA event bus for custom automations:
- `bosch_shc_camera_motion` — movement detected
- `bosch_shc_camera_audio_alarm` — audio alarm triggered
- `bosch_shc_camera_person` — person detected (Gen2 DualRadar only)

Event data: `camera_id`, `camera_name`, `timestamp`, `image_url`, `event_id`, `source` (`fcm_push` / `polling`).

To inspect events live, open **Developer Tools → Events** (HA ≥ 2026.4: **Developer Tools → Events → Subscribe to events**), type `bosch_shc_camera_motion` (the custom event names do not appear in the dropdown — you have to enter them by hand) and click **Start listening**.

```yaml
# Example: notify on every motion at one specific camera
# HA ≥ 2026.4: use `trigger: event`; older HA: use `platform: event`
trigger:
  - trigger: event
    event_type: bosch_shc_camera_motion
    event_data:
      camera_id: 00000000-0000-0000-0000-000000000000  # from camera attributes
action:
  - service: notify.mobile_app_xxx
    data:
      title: "Motion detected"
      message: "{{ trigger.event.data.camera_name }} – {{ trigger.event.data.timestamp }}"
```

Drop `event_data:` to react to all cameras and filter inside `condition:` via `trigger.event.data.camera_id`.

#### When to use the bus event vs. the `events_today` sensor

`bosch_shc_camera_motion` and `sensor.bosch_<name>_movement_events_today` are fed from two different paths and are not interchangeable as automation triggers:

| | `bosch_shc_camera_motion` (event bus) | `sensor.*_movement_events_today` (state) |
|---|---|---|
| Source | FCM push (~2 s) **or** poll tick | Poll tick of `/v11/events` |
| Fires per | New event ID, max once per 60 s dedup window | Every poll tick where the daily counter increased |
| Burst handling | Polling tick with N new events fires the bus event **once** for the newest ID — older IDs in the same tick are not re-fired | Counter advances by N regardless of burst size |
| Best for | Live reaction with FCM Push enabled | Robust fallback when FCM is unreliable, or "any motion happened" automations |

If you primarily care about reacting to *every* motion and FCM occasionally drops on your network/phone, prefer a state trigger on the sensor:

```yaml
trigger:
  - trigger: state
    entity_id: sensor.bosch_terrasse_movement_events_today
```

The two can be combined: `bosch_shc_camera_motion` for fast reaction (~2 s via FCM) plus a state trigger on the sensor as a safety net for missed pushes.

To check FCM health: `select.bosch_camera_fcm_push_mode` should be `auto`, and `sensor.bosch_<name>_event_detection` should report `fcm_push`. If it sits on `polling`, FCM is not delivering and you'll see the burst-merge effect described above.

### Developer Tools — Services

All services are available in **Developer Tools → Services** (or via automations/scripts):

| Service | Description | Fields |
|---------|-------------|--------|
| `bosch_shc_camera.trigger_snapshot` | Force immediate snapshot refresh for all cameras | — |
| `bosch_shc_camera.open_live_connection` | Open live stream for a specific camera | `camera_id` |
| `bosch_shc_camera.rename_camera` | Rename a camera (appears in Bosch app + HA) | `camera_id`, `new_name` |
| `bosch_shc_camera.invite_friend` | Send camera sharing invitation by email | `email` |
| `bosch_shc_camera.list_friends` | List all friends and camera shares (persistent notification) | — |
| `bosch_shc_camera.remove_friend` | Remove a friend and revoke all camera shares | `friend_id` |
| `bosch_shc_camera.get_lighting_schedule` | Read full lighting schedule (persistent notification) | `camera_id` |
| `bosch_shc_camera.delete_motion_zone` | Delete a single motion zone by index | `camera_id`, `zone_index` |
| `bosch_shc_camera.get_privacy_masks` | Read privacy mask zones (persistent notification) | `camera_id` |
| `bosch_shc_camera.set_privacy_masks` | Set/clear privacy mask zones (0.0–1.0 coordinates) | `camera_id`, `masks` |
| `bosch_shc_camera.create_rule` | Create a cloud-side schedule rule | `camera_id`, `name`, `start_time`, `end_time`, `weekdays`, `is_active` |
| `bosch_shc_camera.update_rule` | Update a schedule rule (change name, times, activate/deactivate) | `camera_id`, `rule_id`, `name`?, `start_time`?, `end_time`?, `weekdays`?, `is_active`? |
| `bosch_shc_camera.delete_rule` | Delete a schedule rule | `camera_id`, `rule_id` |
| `bosch_shc_camera.set_motion_zones` | Set motion detection zones (normalized 0.0–1.0 coordinates) | `camera_id`, `zones` |
| `bosch_shc_camera.get_motion_zones` | Read motion zones from cloud API (persistent notification) | `camera_id` |
| `bosch_shc_camera.share_camera` | Share cameras with a friend (time-limited) | `friend_id`, `camera_ids`, `days`? |

**Examples:**

```yaml
# Rename a camera
service: bosch_shc_camera.rename_camera
data:
  camera_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  new_name: "Garden Camera"

# Invite a friend to share cameras
service: bosch_shc_camera.invite_friend
data:
  email: "friend@example.com"

# List all camera shares
service: bosch_shc_camera.list_friends

# Remove a friend (get friend_id from list_friends)
service: bosch_shc_camera.remove_friend
data:
  friend_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

# Create a schedule rule (notifications active 8am-8pm weekdays)
service: bosch_shc_camera.create_rule
data:
  camera_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  name: "Weekday Schedule"
  start_time: "08:00:00"
  end_time: "20:00:00"
  weekdays: [1, 2, 3, 4, 5]
  is_active: true

# Update a rule (deactivate it)
service: bosch_shc_camera.update_rule
data:
  camera_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  rule_id: "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy"
  is_active: false

# Set motion detection zones (list of normalized rectangles)
service: bosch_shc_camera.set_motion_zones
data:
  camera_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  zones:
    - { x: 0.0, y: 0.3, w: 0.67, h: 0.7 }
    - { x: 0.63, y: 0.42, w: 0.28, h: 0.58 }

# Share cameras with a friend for 30 days
service: bosch_shc_camera.share_camera
data:
  friend_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  camera_ids:
    - "cam-id-1"
    - "cam-id-2"
  days: 30
```

> **Tip:** Find the `camera_id` in the camera entity's attributes (Developer Tools → States → `camera.bosch_*` → `camera_id` attribute).

### Ready-to-Use Automations

- [`examples/automation_ios_push_alert.yaml`](examples/automation_ios_push_alert.yaml) — iPhone push (time-sensitive)
- [`examples/automation_signal_alert.yaml`](examples/automation_signal_alert.yaml) — Signal text message
- [`blueprints/bosch_camera_signal_alert.yaml`](blueprints/bosch_camera_signal_alert.yaml) — configurable blueprint

### Example Library — `examples/automations/`

A growing collection of bilingual (EN + DE) automation snippets that cover common Bosch-camera scenarios. Every file is self-contained and explains its placeholders inline, so you can copy → adapt entity IDs → drop into your `automations.yaml`.

| Category | What's in there |
|---|---|
| **Motion-light control** (silence the porch spotlight when you don't want it flaring) | manual "dinner mode" helper · door / window sensor trigger · presence sensor (mmWave / PIR / BLE) · time + sunset-driven schedule · Gen1 instant-off fallback · Gen1 hardware power-cut · **production-grade door + privacy + light coordination** with `mode: restart`, day/night-aware delays, HA-restart recovery |
| **Privacy & away mode** | indoor privacy auto-toggle by who's home · arm/disarm all cameras when the house empties out · per-person precise cleanup (only turn off what they had on) |
| **Smart notifications** | snapshot + push with image · weather-aware (skip during storms) · doorbell-style auto-display on a wall tablet · sleep mode (quiet at night, but real intruder pattern still wakes you) · vacation deterrent with random light flashes · **escalating offline alert** (silent → info → critical based on outage duration) |
| **Garage & vehicles** | combine the driveway camera with the garage-door cover entity to detect "vehicle arriving" / "vehicle leaving" — optional AI vehicle classification (own car / delivery / unknown) |
| **AI vision** | classify motion via Gemini / GPT-4o / Claude / local Ollama → push only for person/vehicle/package, ignore pets · package-delivery detection · daily AI summary of camera events · TTS visitor greeting |
| **Snapshot scheduler / time-lapse** | [`snapshot-time-lapse.yaml`](examples/automations/snapshot-time-lapse.yaml) — four ready-to-use variants: hourly daytime (workday-aware), motion-triggered 15-min throttle, daily midnight reference, weekly summary push. Snapshots land in `/media/bosch-timelapse/<cam>/`; assemble into mp4 with the included ffmpeg one-liner. |

→ **[Browse the full example library](examples/automations/README.md)** — index, generation matrix (Gen1 vs Gen2), placeholder reference, and combination patterns.

---

## Lovelace Cards

The integration ships **two custom cards**, both auto-registered (since v10.3.19 — no manual Lovelace resource entry needed). They share the same code bundle (`bosch-camera-card.js`) and version, so a single resource URL serves both.

| Card | Use case | Versioning |
|---|---|---|
| `custom:bosch-camera-card` | **One Bosch camera per card.** The full feature surface — live HLS / WebRTC video, snapshot, stream/audio/light/privacy/notifications switches, pan controls (360 only), notification-type accordion, motion-zone overlay, schedule editor, alarm controls (Gen2 Indoor II only). | Shared bundle — tracks the integration version |
| `custom:bosch-camera-overview-card` | **All Bosch cameras at once.** Auto-discovers every camera via `attributes.brand === "Bosch"` and renders a responsive tile grid. Sort order is **Live → Private → Offline**. Each tile is a full `bosch-camera-card` underneath, so per-camera overrides work the same way. | Shared bundle — tracks the integration version |

> **Since v13.0.0 — Apple-style redesign + theme switcher (iOS / Material You).** Glass title pill with camera name + green/orange/grey status dot overlays the top of the video; semantic status badge (LIVE / PRIVATE / Connecting / Offline) sits in the top-right. Glass pill-bar with circular buttons (Snapshot, Live, Privacy, Light, Fullscreen, Picture-in-Picture, More) overlays the bottom — the More button reveals the Audio toggle and every other switch / accordion. The card auto-detects iOS vs Android user-agent and renders in the matching design language; a three-state switcher (Auto / iOS / Android) inside the More menu lets the user override, with the choice persisted in `localStorage` and broadcast to every Bosch card on the page. Opt out of the redesign entirely with `apple_style: false`; pin a theme via YAML with `theme: ios | android | auto` (default `ios`).

The detailed reference for each card follows below — start with `bosch-camera-card` (the building block) and jump to [`bosch-camera-overview-card`](#bosch-camera-overview-card--multi-camera-grid) at the bottom. Dashboards combining several cards are under [Dashboard examples](#dashboard-examples).

> Two further BETA cards — `bosch-nvr-timeline-card` and `bosch-nvr-multicam-card` — are part of the Mini-NVR feature and documented under [Mini-NVR → Lovelace Cards](#lovelace-cards-beta), not here.

---

### `bosch-camera-card` — single camera

#### What the card shows

```
┌──────────────────────────────────┐
│ ● Garden              [streaming]│
│  ┌────────────────────────────┐  │
│  │   Live video / snapshot    │  │
│  │ Last: 2026-03-19 09:32     │  │
│  └────────────────────────────┘  │
│  [ 📸 Snapshot ] [ 📹 Stream ] [ ⛶ ] │
│  [ 🔊 Sound / video ] [ 💡 Light ] [ 🔒 Private ] │
│  [ 🔔 Notifications ]            │
│  [ 🎙 Intercom ]             │
│  [ ◀ ] [     ■     ] [ ▶ ]  ← pan    │
│  Quality: [Auto ▼]                   │
│  ▼ Notification Types            │
│  ▼ Advanced                          │
│  ▼ Diagnostics                           │
│  ▼ Schedules & Zones                  │
└──────────────────────────────────┘
```

#### Card modes

| Mode | Description |
|------|-------------|
| **Stream OFF** | Snapshot image, auto-refreshed every **60 s** (visible) / **30 min** (background tab). Immediate refresh on tab focus. |
| **Stream ON** | Live **HLS video** (30fps H.264 + AAC-LC). Uses go2rtc and HA's camera stream WS. Audio toggle controls mute/unmute. Loading overlay with status updates during connection. Auto-recovers from stream disconnects. **Audio quality is higher than the official Bosch app** — the Bosch mobile app downsamples audio for cellular bandwidth, while this integration delivers the unmodified AAC-LC stream straight from the camera. |

#### Controls

| Button | Function |
|--------|----------|
| 📸 Snapshot | Force-fetch a fresh image immediately |
| 📹 Live Stream | Toggle stream ON/OFF |
| 🔊 Sound | Mute / unmute live audio. Hover (desktop) or long-press (touch) reveals a volume slider. Greyed out until a live stream is playing. Both on/off and volume are backed by automatable entities and sync across all cards (see [Audio: who controls the sound](#audio-who-controls-the-sound)). |
| 💡 Light | Toggle camera LED light (outdoor camera) |
| 🔒 Private | Toggle privacy mode (covers lens) |
| 🔔 Notifications | Toggle push notifications |
| 🎙 Intercom | Toggle intercom / two-way audio |
| ◀ ▶ Pan | Pan left/right (CAMERA_360 only) |

**Collapsible accordion sections** (auto-hidden when entities not available):
- **Notification Types** — per-type notification toggles: movement, person, audio, trouble, camera alarm
- **Advanced** — timestamp overlay, auto-follow, motion detection, record sound, privacy sound
- **Diagnostics** — WiFi signal %, firmware version, ambient light %, movement/audio events today
- **Schedules & Zones** — schedule rules list with ON/OFF toggle per rule + delete button, motion zone overlay toggle, motion zone count (RCP)

#### Card YAML

```yaml
# Minimal config — everything else defaults from camera_entity
type: custom:bosch-camera-card
camera_entity: camera.bosch_garten

# With optional title
type: custom:bosch-camera-card
camera_entity: camera.bosch_garten
title: Garden

# Compact "minimal layout" — hides all advanced controls behind the ⋮ button.
# Visible: image + info row (Status / Connection / Response) + primary buttons
# (Snapshot, Live Stream, ⋮ Overflow, Fullscreen) + Privacy toggle.
# Tap ⋮ to progressively reveal audio/light/notifications/accordions/pan/etc.
type: custom:bosch-camera-card
camera_entity: camera.bosch_garten
title: Garden
minimal: true

# Clean Apple-Home-style tile — same look the overview-card grid uses, on a
# single card. Just the video + title pill, no pill-bar / status badge.
type: custom:bosch-camera-card
camera_entity: camera.bosch_garten
apple_style: true     # default true
compact: true         # drop the bottom pill-bar + top-right status badge
show_last_event: false  # hide the last-event badge too

# Bare video, nothing overlaid
type: custom:bosch-camera-card
camera_entity: camera.bosch_garten
compact: true
show_title: false
show_last_event: false

```

#### Options

| Key | Default | Effect |
|-----|---------|--------|
| `camera_entity` | *(required)* | The Bosch camera entity. Every other entity (switches, sensors, number) is auto-derived from this id. |
| `title` | friendly_name | Overrides the displayed name. |
| `apple_style` | `true` | Glass overlay redesign. `false` = legacy header-bar + button-row chrome. |
| `theme` | `ios` | `ios` / `android` / `auto` (resolve from user-agent). |
| `mode` | `auto` | Card chrome day/night: `auto` / `day` / `night`. |
| `compact` | `false` | Apple-Home tile: hides the bottom pill-bar + top-right status badge (video + title pill only). Always collapsed (no controls) — for overview grids. |
| `minimal` | `false` | `false` (default) shows the full control stack (switches + accordions) expanded with the ⋮ pre-opened; `true` collapses everything behind the ⋮ "More" button until tapped. (Overview-grid tiles default to `minimal: true`.) |
| `show_title` | `true` | Set `false` to hide the title pill entirely (clean video-only tile). |
| `show_last_event` | `true` | Set `false` to hide the last-event badge in the bottom-right of the video. |
| `show_audio` | `true` | Set `false` to hide the audio button (sound + volume) entirely. |
| `use_card_audio_settings` | `false` | Decouple this card's audio from the global backend entities. `true` = mute + volume are per-browser only (`localStorage`), never touching `switch.<cam>_audio` / `number.<cam>_audio_volume` or other devices. See [Audio: who controls the sound](#audio-who-controls-the-sound). |
| `border_radius` | *(theme)* | CSS length, e.g. `"22px"`. Overrides the card corner radius for this card only. By default the card follows your dashboard theme's `ha-card-border-radius`; set this to pin a value regardless of the theme. |
| `box_shadow` | *(theme)* | CSS shadow, e.g. `"0 0 4px 1.5px rgba(255,255,255,.5)"`. Overrides the card shadow for this card only. By default the card follows your theme's `ha-card-box-shadow`. |
| `auto_play` | *(integration default)* | `lan` / `always` / `never` — per-card override of the integration's auto-play behaviour. |
| `show_motion_zones` | `false` | Overlay the configured motion-zone SVG on the video. |
| `snapshot_during_warmup` | `true` | Show the last snapshot under the loading overlay while the stream warms up. |
| `hide_redundant_privacy` | `true` | Hides the standalone privacy switch row when the apple-style pill bar already shows a privacy button — avoids showing the same control twice. Since v13.5.5. |
| `pan_overlay` | `auto` | On-video edge pan chevrons for pan/tilt cameras: `auto` (visible on hover, touch, and fullscreen), `always`, or `never` (use the menu row instead). Shown only for cameras that expose `number.<cam>_pan_position`. Since v13.5.5. |

> **Fullscreen toggle** — the ⛶ fullscreen button now toggles: tap it once to enter fullscreen, tap it again (or the camera name button when shown) to exit. `Esc` and a tap outside the video also exit, as before.

> **Fullscreen zoom** — while in fullscreen you can zoom into the picture: pinch with two fingers (touch), scroll the mouse wheel (desktop), or double-tap / double-click to toggle 2× at that spot. Drag to pan when zoomed in. Zoom resets automatically when you leave fullscreen. Outside fullscreen the gestures are left untouched so normal page scrolling/pinch still works.

> **Picture-in-Picture** — the ⧉ PiP button pops the live stream out into the browser's floating, always-on-top window so you can keep watching while you work in other apps. On **macOS Safari** the window floats over every app; on Chrome it stays above the browser's own windows. The floating window's title shows the camera name (instead of the page address). Start the live stream first, then tap PiP. The browser allows only **one** PiP window at a time, so while one camera is floating the PiP button on every other camera greys out automatically; tap it again to close. The window keeps playing through a stream reconnect, and the button is hidden on browsers without PiP support (most iOS/Android in-app WebViews). Since v13.5.x.

> **Live subtitles & translation (browser feature, no setup)** — if your camera has audio, **Chrome → Settings → Accessibility → Live Caption** transcribes spoken audio in the stream on-device, in real time, and **Live Translate** can translate those captions into your language. This works on the live view (and alongside Picture-in-Picture) without any integration changes — it captions whatever audio is playing in the tab.

> **Sound & volume** — the 🔊 button mutes/unmutes live audio; hovering it (desktop) or long-pressing it (touch) shows a volume slider. The control is greyed out until a stream is actually playing. Both the on/off and the volume are driven by **backend entities** (see below), so they are automatable and stay in sync across every open card. On iOS the volume slider is hidden because Safari does not allow scripts to change media volume.

#### Audio: who controls the sound

Two backend entities are the single source of truth for audio, so everything is automatable and shared across all sessions and the mobile app:

| Entity | What it controls |
|---|---|
| `switch.bosch_<camera>_audio` | **On/off** — whether the live stream carries an audio track at all. Off = video-only, nothing to hear anywhere. Power-on default comes from the integration option **Audio on by default**. |
| `number.bosch_<camera>_audio_volume` | **Volume 0–100** — the playback level the card applies. A virtual preference (Bosch has no volume API; loudness is a browser property), so it is automatable and shared but has no effect on iOS, where Safari makes volume read-only. Default 50. |

The card 🔊 pill is just the front-end for these:
- It **reflects** both entities and **writes** them back — dragging the volume slider calls `number.set_value`, tapping mute/unmute toggles the audio switch when needed. HA then pushes the change to every other open card.
- **Two-way sync is live.** An automation that flips `switch.bosch_<camera>_audio` or sets `number.bosch_<camera>_audio_volume` is applied to the playing video immediately on every card. (One browser caveat: a programmatic *unmute* of a playing video may be refused by the autoplay policy until you've interacted with the page once — muting and volume changes are always honoured.)

So: **automate the entities**, and the cards follow. There is deliberately no card-YAML volume or "sound on by default" option — that would be a second, conflicting source of truth for the same decision.

**Opt out of the global behaviour.** If you'd rather a card *not* participate in the shared/automatable audio — e.g. one dashboard whose volume should be purely local to that browser — set `use_card_audio_settings: true` on the card (single or overview). The pill then toggles only that browser's mute and remembers its own volume in `localStorage`, and never reads or writes `switch.<cam>_audio` / `number.<cam>_audio_volume`. The backend entities keep working for every other (default) card and for automations.

> **Visual editor** — all the options above except the rarely-used ones are also editable in the dashboard's visual card editor (no YAML needed). The camera picker lists every `camera.*` entity on your instance, so any Bosch camera can be selected regardless of how its entity is named. The editor and the card chrome follow your Home Assistant language: German for a `de…` UI language, English for everything else.

All entity IDs are auto-derived from `camera_entity`. Buttons and sections are hidden automatically when entities don't exist. The **Response** slot in the info row reads the `buffering_time_ms` attribute exposed by the camera entity (Bosch cloud-issued, ~500 ms on LOCAL and ~1000 ms on REMOTE); it stays `—` while the stream is idle. The **Connection** slot reads `connection_type` and shows `LAN`, `Cloud`, or `—`.

> Dashboards mixing several cards (two-up, full grids) are documented under [Dashboard examples](#dashboard-examples) below, after the overview card.

---

### `bosch-camera-overview-card` — multi-camera grid

Introduced in v10.3.0. Auto-discovers every Bosch camera on the HA instance (`attributes.brand === "Bosch"`) and renders a responsive tile grid — no manual entity-list maintenance needed when you add or remove a camera. Each tile is itself a full `bosch-camera-card`, so all single-camera features (live HLS / WebRTC, snapshot, privacy/audio/light switches, etc.) work inside the grid.

#### What the overview shows

```
┌──────────────────────────────────────────────────────────────┐
│  Cameras                                                4/4  │
│  ┌─────────────────────┐  ┌─────────────────────┐            │
│  │ ● Terrace           │  │ ● Indoor            │  ← green   │
│  │ [live tile]         │  │ [live tile]         │  outline   │
│  └─────────────────────┘  └─────────────────────┘            │
│  ┌─────────────────────┐  ┌─────────────────────┐            │
│  │ 🔒 Entrance         │  │ ○ Garden            │  ← orange  │
│  │ [privacy on]        │  │ [offline]           │    & grey  │
│  └─────────────────────┘  └─────────────────────┘            │
└──────────────────────────────────────────────────────────────┘
```

* **Header** — optional `title` plus a `4/4` style count showing how many cameras are visible.
* **Tile** — full single-camera card per cell. Each tile carries a colored outline based on the camera's tier:
  * **Green** — online, privacy off (live).
  * **Orange** — online, privacy on (lens covered).
  * **Grey** — offline / unreachable.

#### Tile sort order

Two-level sort: tiers come first (always live → private → offline), then within each tier either alphabetic (default) or Bosch-app-priority (since v10.4.10).

| Mode | YAML | Behavior |
|---|---|---|
| Alphabetic *(default)* | `use_bosch_sort: false` (or omit) | Inside each tier, sort by `friendly_name` using German collation. |
| Bosch-app priority | `use_bosch_sort: true` | Inside each tier, sort by the `bosch_priority` attribute (`GET /v11/video_inputs` `priority` field — that's also what `PUT /v11/video_inputs/order` from the Bosch app sets). Cameras without a numeric priority fall back to alphabetic at the end of their tier, so foreign / non-Bosch `include:` entries don't disappear. |

The `online_offline_view: false` option hides the offline tier entirely — only online cameras (green and orange tiles) are rendered.

#### Per-camera overrides

The grid creates one `bosch-camera-card` per discovered camera using sensible defaults. To customise a specific tile (different title, attached automations, faster refresh, etc.) pass it through `overrides`:

```yaml
overrides:
  camera.bosch_terrasse:
    automations:
      - automation.alarmanlage
  camera.bosch_garten:
    refresh_interval_streaming: 3
    title: Entrance (Gen1)
    minimal: true                    # compact layout for this tile only
```

Each override is deep-merged into the child card's `setConfig()`. The wrapping `camera_entity` is always set automatically — no need to repeat it. The top-level `card_defaults` map applies the same options to every tile (e.g. `card_defaults: { minimal: true }` makes the whole grid compact).

#### Card YAML

```yaml
# Minimal — auto-discovers every Bosch camera, alphabetic order
type: custom:bosch-camera-overview-card

# Full options
type: custom:bosch-camera-overview-card
title: Cameras                  # optional header
online_offline_view: true       # default true; false = hide offline tier
columns: auto                   # "auto" | 1 | 2 | 3 | 4   (default "auto")
min_width: 650px                # cell min-width when columns: auto (default 650px)
gap: 12px                       # grid gap (default 12px)
use_bosch_sort: true            # default false; true = follow Bosch-app priority
minimal: false                  # default false; true = compact tiles for the whole grid
exclude:                        # skip these entity_ids
  - camera.bosch_test
include:                        # bypass auto-discovery and use exactly these
  - camera.bosch_terrasse
  - camera.bosch_innenbereich
overrides:                      # per-camera setConfig() overrides
  camera.bosch_terrasse:
    automations:
      - automation.alarmanlage
card_defaults:                  # base options applied to every tile (overrides win over these)
  refresh_interval_streaming: 5
```

#### Options

| Key | Default | Effect |
|-----|---------|--------|
| `title` | *(none)* | Optional header rendered above the grid. |
| `online_offline_view` | `true` | `false` hides the offline tier — only online (green/orange) tiles render. |
| `columns` | `auto` | `auto` packs as many cells as fit `min_width`; `1`/`2`/`3`/`4` forces that count. |
| `min_width` | `650px` | Cell min-width when `columns: auto`. |
| `gap` | `12px` | Grid gap between tiles (shrinks to 8 px on phones). |
| `use_bosch_sort` | `false` | `true` orders each tier by the `bosch_priority` attribute (Bosch-app order); `false` = German-collated alphabetic. |
| `exclude` | `[]` | List of entity_ids to skip from auto-discovery. |
| `include` | `[]` | Bypass auto-discovery and render exactly these entity_ids. |
| `apple_style` | `true` | Propagated to every tile. `false` = legacy chrome on the whole grid. |
| `theme` | `ios` | Propagated to every tile: `ios` / `android` / `auto`. |
| `mode` | `auto` | Propagated to every tile: `auto` / `day` / `night`. |
| `compact` | `false` | `true` makes every tile an Apple-Home tile (no pill-bar / status badge). Pair with `columns: 2`/`3` for a clean grid. |
| `minimal` | `true` | Default on for the grid — every tile starts collapsed (controls behind its ⋮). Set `false` to expand every tile's control stack. |
| `border_radius` / `box_shadow` | *(theme)* | CSS overrides applied to the whole grid (every tile). By default the cards follow your dashboard theme's `ha-card-border-radius` / `ha-card-box-shadow`. |
| `overrides` | `{}` | Per-camera `setConfig()` overrides, keyed by entity_id. Deep-merged into that tile (`camera_entity` is set automatically). |
| `card_defaults` | `{}` | Base options applied to every tile; an explicit `overrides.<entity>` key wins over these. Accepts any single-card option (see table above). |

> Because each tile is a full `bosch-camera-card`, every single-card option above (including `show_title` and `show_last_event`) can be set globally via `card_defaults` or per-camera via `overrides`.

#### Responsive behavior

* **Viewports > 640 px** — `columns: auto` packs as many cells as fit `min_width` per row; explicit `columns: 1|2|3|4` forces that count.
* **Viewports ≤ 640 px** — always one column regardless of `columns`, so cards stay legible on phones. The grid `gap` shrinks to 8 px in this mode.
* **Full-width dashboards** — to escape the sections-view max-width clamp (which limits standard dashboards to ~870 px), put the overview card in a view with `panel: true`. It then uses the full browser width.

#### Behavior notes

* **Discovery is live.** When a camera is added or removed, the overview re-discovers on the next `hass` update — no card config edit required.
* **Tier transitions are smooth.** When a camera's privacy switch flips or it goes offline, only the changed tile re-mounts; the others stay (no full grid rebuild).
* **`bosch_priority` attribute.** Exposed on every Bosch camera entity (`camera.bosch_*`). Visible in Developer Tools → States. You can also use it in templates / sensors outside the card, e. g. to drive a different layout.

---

### Dashboard examples

Ready-to-paste layouts that combine the two cards above.

#### Two-camera dashboard

```yaml
type: grid
columns: 2
cards:
  - type: custom:bosch-camera-card
    camera_entity: camera.bosch_garten
    title: Garden
  - type: custom:bosch-camera-card
    camera_entity: camera.bosch_kamera
    title: Camera
```

#### A full camera dashboard view

A complete dashboard view that mixes both cards in different configurations — a handy reference / test board you can paste into a new view (the **sections** view type, HA 2024.4+). Each entry is a custom card placed in a grid section; `grid_options: {columns: 12}` makes each one span the full section width.

```yaml
title: Cameras
path: cameras
type: sections
max_columns: 2
sections:
  # ── Single cards, one per configuration ───────────────────────────
  - type: grid
    cards:
      - type: heading
        heading: Single camera
      - type: custom:bosch-camera-card          # full glass chrome, controls expanded
        camera_entity: camera.bosch_terrasse
        title: Terrace
        grid_options: {columns: 12}
      - type: custom:bosch-camera-card          # clean Apple-Home tile
        camera_entity: camera.bosch_innenbereich
        compact: true
        grid_options: {columns: 12}
      - type: custom:bosch-camera-card          # bare video, nothing overlaid
        camera_entity: camera.bosch_kamera
        compact: true
        show_title: false
        show_last_event: false
        grid_options: {columns: 12}
  # ── Overview grid: every camera at once ───────────────────────────
  - type: grid
    cards:
      - type: heading
        heading: All cameras
      - type: custom:bosch-camera-overview-card
        title: All cameras
        columns: 2
        grid_options: {columns: 12}
```

> The view above is exactly how the bundled “Card Test” reference board is built. Standalone single cards show their controls expanded by default; overview-grid tiles stay collapsed (tap the ⋮ on a tile to reveal its controls) so the grid stays glanceable. Set `minimal: false` on a single card to collapse it behind the ⋮, or `minimal: false` on the overview to expand every tile.

---

### Admin / Health Dashboard Example

The integration exposes ~40 entities per camera covering status, firmware, WiFi, motion zones, alarm state, stream health, and more. A dedicated **Bosch view** in your admin dashboard lets you see every camera's health at a glance — useful for power-users monitoring Cloud reachability + FCM push + Gen1/Gen2 mix.

#### Entities surfaced

| Group | Entity pattern | Use |
|---|---|---|
| Online state | `sensor.bosch_<name>_status` | `online` / `offline` / `privacy_mode` |
| Stream | `sensor.bosch_<name>_stream_status` (Diagnostic) | `idle` / `streaming` / `error` |
| Firmware | `sensor.bosch_<name>_firmware_version` | matches the camera FW string |
| WiFi | `sensor.bosch_<name>_wifi_signal` | 0-100 % (Gen2 only) |
| Activity | `sensor.bosch_<name>_events_today` (Measurement) | daily-resetting motion counter |
| Last event | `sensor.bosch_<name>_last_event` + `_last_event_type` | timestamp + type |
| Ambient light | `sensor.bosch_<name>_ambient_light` | 0-100 % (Gen2 only) |
| Alarm | `sensor.bosch_<name>_alarm_status` | Indoor II only |
| Zones | `sensor.bosch_<name>_motion_zones` + `_privacy_masks` | configured count |
| FCM push health | `sensor.bosch_camera_event_detection` | `fcm_push` / `polling` / `degraded` (integration-wide) |
| Integration update | `update.bosch_smart_home_camera_update` | shows when a new release is available |

#### Drop-in dashboard view YAML

Add this as a new view to any existing dashboard (Settings → Dashboards → pick one → "Take control" → Raw config editor → paste at the end of the `views:` list). Rename `terrasse`/`innenbereich`/`kamera`/`garten` to match your own camera entity names:

```yaml
title: Bosch
path: bosch
icon: mdi:cctv
type: sections
max_columns: 3
sections:
  - type: grid
    cards:
      - type: heading
        heading: Integration
        heading_style: title
        icon: mdi:cctv
      - type: markdown
        content: |
          ### 🏅 Quality Scale: **Platinum**
          - System Health · Logbook · Diagnostics · Repair Issues · Update Entities
          - mypy --strict green · 100% test coverage · 0 `requests` deps
        grid_options: {columns: 12, rows: auto}
      - type: tile
        entity: update.bosch_smart_home_camera_update
        name: Integration Update
        icon: mdi:cloud-download
        color: blue
        grid_options: {columns: 6, rows: 1}
      - type: tile
        entity: sensor.bosch_camera_event_detection
        name: FCM Push
        icon: mdi:bell-ring
        color: green
        grid_options: {columns: 6, rows: 1}

  - type: grid
    cards:
      - type: heading
        heading: Terrace (Gen2 Outdoor)
        heading_style: title
        icon: mdi:weather-sunny
      - type: tile
        entity: sensor.bosch_terrasse_status
        name: Status
        color: green
        grid_options: {columns: 6, rows: 1}
      - type: tile
        entity: sensor.bosch_terrasse_stream_status
        name: Stream
        icon: mdi:video
        color: cyan
        grid_options: {columns: 6, rows: 1}
      - type: tile
        entity: sensor.bosch_terrasse_firmware_version
        name: Firmware
        icon: mdi:chip
        grid_options: {columns: 6, rows: 1}
      - type: tile
        entity: sensor.bosch_terrasse_wifi_signal
        name: WiFi
        icon: mdi:wifi
        color: blue
        grid_options: {columns: 6, rows: 1}
      - type: tile
        entity: sensor.bosch_terrasse_events_today
        name: Events today
        icon: mdi:motion-sensor
        color: amber
        grid_options: {columns: 6, rows: 1}
      - type: tile
        entity: sensor.bosch_terrasse_ambient_light
        name: Ambient light
        icon: mdi:weather-sunny
        color: yellow
        grid_options: {columns: 6, rows: 1}

  - type: grid
    cards:
      - type: heading
        heading: Live Streams
        heading_style: title
        icon: mdi:cctv
      - type: picture-entity
        entity: camera.bosch_terrasse
        camera_view: auto
        show_state: false
        grid_options: {columns: 6, rows: 4}
      - type: picture-entity
        entity: camera.bosch_innenbereich
        camera_view: auto
        show_state: false
        grid_options: {columns: 6, rows: 4}
```

Repeat the per-camera grid for each camera you have. The view uses the standard `sections` layout — no custom cards required.

#### Tips for own variants

- **Picture-entity vs. picture-glance**: `picture-entity` shows a single live thumbnail (refreshes ~every 30 s per `image_refresh_interval`). `picture-glance` overlays state badges. Use the integration's own **`bosch-camera-card`** for full stream + controls (see [Lovelace Cards](#lovelace-cards)).
- **Filter by area**: if you assign cameras to HA Areas, an `area` card with `navigation_path` gives a hierarchical entry point.
- **Mobile-first**: set `column_span: 1` on grids to force single-column on phones; use `panel: true` on the view for full-width tablet layouts.
- **Recorder DB**: if you don't need long-term history for the diagnostic sensors, see the *Recorder DB size* hint in **Known Limitations**.

---

## Requirements

- Home Assistant 2024.1+
- Python packages: `firebase-messaging`, `smbprotocol` (auto-installed via manifest)
- For live video: go2rtc (built into HA) or ffplay/mpv

---

## Alarm System / Automation Setup

The Eyes Indoor II (Gen2) adds a built-in alarm system with integrated 75 dB siren. Here's how to wire it into a typical HA alarm automation alongside your existing cameras:

### Entities for the alarm system (v9.1.10+, Gen2 Indoor II only)

| Entity | Purpose |
|---|---|
| `switch.bosch_{name}_alarmanlage` | Arm / disarm the built-in intrusion system (`PUT /intrusionSystem/arming`). Derived state from `alarmStatus.intrusionSystem` (INACTIVE / ACTIVE). |
| `switch.bosch_{name}_sirene` | Main 75 dB siren *configuration* on/off (`alarm_settings.alarmMode`). When OFF, alarm triggers don't fire the siren. Does **not** trigger the siren itself — see the next entry. |
| `switch.bosch_{name}_sirene_auslosen` | **Trigger** the 75 dB siren on demand (`PUT /panic_alarm` with `{"status":"ON|OFF"}`). Stateful — the siren keeps blaring until you flip the switch back to OFF. Disabled by default. |
| `switch.bosch_{name}_pre_alarm` | Pre-alarm red-LED warning before the siren fires (`alarm_settings.preAlarmMode`). |
| `sensor.bosch_{name}_alarm_status` | `INACTIVE` / `ACTIVE` / `UNKNOWN` — state machine for the alarm. Attributes include `alarm_type` (`NONE` when idle), `siren_duration_s`, `activation_delay_s`, `pre_alarm_duration_s`. |
| `number.bosch_{name}_sirenen_dauer` | Siren duration in seconds (`alarm_settings.alarmDelayInSeconds`, 10–300). |
| `number.bosch_{name}_alarm_verzogerung` | Activation delay in seconds (`alarmActivationDelaySeconds`, 0–600). |
| `number.bosch_{name}_pre_alarm_dauer` | Pre-alarm LED-warning duration (`preAlarmDelayInSeconds`, 0–300). |
| `number.bosch_{name}_power_led` | White Power-LED brightness 0–4 (*not* 0–100% — the iOS slider is misleading, the API only accepts 5 discrete steps). |

### Privacy mode — important!

Several settings only work when the camera is **actively recording** (privacy OFF):
- `switch.bosch_{name}_einbrucherkennung` (intrusion detection)
- `select.bosch_{name}_erkennungsmodus` (detection mode: `ALL_MOTIONS` / `ONLY_HUMANS` / `ZONES`)
- `number.bosch_{name}_microphone_level`

When privacy is ON, the Bosch cloud API returns HTTP 443 `"sh:camera.in.privacy.mode"` on reads/writes to these endpoints, so the entities show as `unavailable`. The integration caches the **last-known-good** values — so if you've ever had privacy OFF since HA started, the cached settings remain visible. If the camera has been in privacy mode since the HA restart, the entities stay unavailable until you turn off privacy once.

**Note on event clips:** Bosch records clips only when the camera is actively monitoring. If all cameras are in privacy mode, `videoClipUploadStatus=Unavailable` is returned for every event — you'll get the text + snapshot alert but no video attachment. This is not a bug in the integration.

### Example: Integrate the Gen2 Indoor II into an existing Alarm System automation

If you already have an alarm automation that toggles `privacy_mode` on your other cameras based on presence / schedule / garage door, just add the new camera's privacy switch alongside:

```yaml
- alias: "Everyone away → arm Alarm System + release cameras"
  sequence:
    - action: switch.turn_off
      target:
        entity_id:
          - switch.bosch_terrasse_privacy_mode      # Gen2 Outdoor
          - switch.bosch_innenbereich_privacy_mode  # Gen2 Indoor II  (new in v9.1.10)
          - switch.bosch_kamera_privacy_mode        # Gen1 360
    - action: switch.turn_on
      target:
        entity_id: switch.bosch_innenbereich_alarmanlage  # arm the built-in siren
```

### Example: Intrusion event → notify + optional siren

```yaml
- alias: "Indoor camera → Person detected"
  triggers:
    - platform: state
      entity_id: binary_sensor.bosch_innen_person
      to: "on"
  conditions:
    - condition: state
      entity_id: switch.bosch_innen_alarmanlage
      state: "on"   # only when armed
  actions:
    - action: notify.mobile_app_xxx   # replace with your notify service
      data:
        message: "🚨 Person detected indoors"
    # Optional: fire the siren (remove this line to silent-alarm)
    # - action: switch.turn_on
    #   target:
    #     entity_id: switch.bosch_innen_sirene
```

### Card config for the alarm system

The custom Lovelace card automatically shows the new alarm rows (Alarm System, Siren, Pre-Alarm, Power-LED) when the alarm entities exist and the alarm system is gated behind the presence of `switch.{base}_alarmanlage`. No card config changes needed:

```yaml
type: custom:bosch-camera-card
camera_entity: camera.bosch_innenbereich
title: "Eyes Indoor II"
```

Everything renders automatically when the integration detects a Gen2 Indoor II.

---

## Apple HomeKit / Apple Home Integration

Bosch Smart Home cameras can be surfaced in Apple Home via HA's built-in **HomeKit Bridge** integration. No special code or extra add-on is needed from this integration — HomeKit Bridge re-exposes any HA camera entity to Apple Home over the local network.

For a detailed setup guide, troubleshooting steps, and YAML configuration options see **[docs/homekit-bridge.md](docs/homekit-bridge.md)**.

### Requirements

- Home Assistant 2024.1 or later
- HomeKit Bridge integration (built into HA Core — no separate install)
- iOS 16+ / macOS 13+ / tvOS 16+ with Home app

### Setup

**Step 1 — Add the HomeKit Bridge integration**

Go to **Settings → Devices & services → Add Integration**, search for **HomeKit Bridge**, and select it. When asked which domains to expose, tick **Cameras**.

**Step 2 — Select camera entities**

In the HomeKit Bridge entity filter, include the Bosch camera entities you want to expose:

```
camera.bosch_terrasse
camera.bosch_innenbereich
camera.bosch_kamera
camera.bosch_eingang
```

Only entities with `camera` domain are needed here — motion sensors are included automatically if you also tick `binary_sensor`.

**Step 3 — Pair with Apple Home**

Open the **Home** app on iOS, tap **+** → **Add Accessory**, and scan the QR code that HA displays in the HomeKit Bridge integration card (or enter the eight-digit code manually).

**Step 4 — Verify privacy mode mapping**

Each camera's `switch.bosch_*_privacy_mode` entity maps automatically to Apple Home's **Camera Streaming** toggle. When privacy mode is ON in HA, the camera appears as streaming-disabled in Apple Home, and vice versa. No additional configuration is required.

### HomeKit Secure Video (HSV)

To record motion clips to iCloud (viewable in the Home app's timeline) you need an Apple **Home hub** (HomePod, Apple TV 4K, or a resident iPad) and an **iCloud+** plan. HSV is enabled per camera in the Home app once a **motion sensor is linked** — HomeKit Bridge does this automatically when you also expose the camera's `binary_sensor.bosch_*_motion` (already included by the `binary_sensor` filter / globs above). Live view works without any of this.

### Video resolution & the Apple 4K update

Bosch Smart Home cameras stream at **1080p Full HD (1920×1080)** — the whole SHC line (Eyes Outdoor / Indoor I + II, 360° Indoor) uses a 1080p sensor; there is no 2K/4K mode.

Apple is lifting HomeKit Secure Video's long-standing **1080p cap to up to 4K** (for both live streaming and HSV recording). That change only benefits cameras with a 2K/4K sensor — **for Bosch cameras it makes no difference**: they already deliver their full native 1080p to Apple Home, and 1080p was never the bottleneck. Nothing needs to change in this integration to "support" it; there simply is no higher-resolution Bosch stream to hand over.

### HomeKit Known Limitations

| Feature | Status |
|---|---|
| Live video stream | ✅ HLS via HomeKit Secure Video (HSV) or standard HomeKit camera stream |
| Motion sensor events | ✅ Forwarded as Apple Home motion-sensor events automatically |
| Privacy mode toggle | ✅ Mapped via `switch.bosch_*_privacy_mode` |
| Two-way audio | ❌ Not exposed — HA's HomeKit Bridge does not forward two-way audio for any camera |
| Activity zones | ❌ Not exposed — requires polygon editor, currently parked |
| Pan controls (360° Indoor) | ❌ Not forwarded via HomeKit Bridge |

Two-way audio is a HA Core HomeKit Bridge constraint that applies to all cameras, not specific to Bosch Smart Home cameras.

### Optional: YAML configuration

If you prefer managing HomeKit Bridge in `configuration.yaml` instead of the UI:

```yaml
homekit:
  name: HA Bridge
  port: 21063
  filter:
    include_domains:
      - camera
      - binary_sensor
    include_entity_globs:
      - camera.bosch_*
      - binary_sensor.bosch_*_motion
  entity_config:
    camera.bosch_terrasse:
      name: "Terrace"
    camera.bosch_innenbereich:
      name: "Indoor"
    camera.bosch_kamera:
      name: "360 Camera"
    camera.bosch_eingang:
      name: "Entrance"
```

After adding this, restart HA and pair using the QR code or PIN shown in the HomeKit Bridge integration card.

---

## Known Limitations

### Cloudflare Tunnel + live stream

The integration ships `cf_unbuffer.py` — a runtime patch that rewrites HA's HLS view classes to send `Transfer-Encoding: chunked` (no `Content-Length`) for `.m4s` segments and `text/event-stream` content-type for `.m3u8` playlists. This ensures cloudflared's `shouldFlush()` triggers immediately instead of buffering the full segment before forwarding.

On **iOS** (Companion App and Mobile Safari), the card detects the platform automatically and skips the WebRTC attempt — going straight to native HLS via `video.src`. This avoids the 5 s ICE timeout that WebRTC would incur over a Cloudflare Tunnel (UDP cannot traverse the HTTP tunnel). The card shows an info banner while streaming on iOS.

Verify the unbuffer patch is active:

```bash
curl -sI https://your-ha.example.com/api/hls/<token>/segment/0.m4s
# Expected: Transfer-Encoding: chunked  AND  no Content-Length
```

### Optional cloudflared improvement

Force cloudflared off QUIC onto HTTP/2 — QUIC over cellular is fragile (regular `failed to accept QUIC stream: timeout` errors). HA → *Settings → Add-ons → Cloudflared → Configuration*: add `--protocol=http2` to `run_parameters`, restart the add-on. Verify in the add-on log: `Initial protocol http2`. Costs nothing, helps WebSocket and large-response stability.

### Recorder DB size — exclude high-frequency sensors (optional)

Several diagnostic sensors change state whenever the camera reconnects (e.g. `sensor.bosch_*_stream_status`, `sensor.bosch_*_last_image_fetch`). On busy homes these can write tens of thousands of state-change rows per day to the HA recorder database. If you don't need long-term history for them, add to `configuration.yaml`:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.bosch_*_stream_status
      - sensor.bosch_*_last_image_fetch
      - sensor.bosch_*_fcm_last_push
```

Existing automations and the camera card continue to work — HA still reads the current state from the in-memory state machine, only the persisted history is skipped.

## Roadmap

Features investigated or intentionally parked — listed here so the direction is visible. Not planned for active development; **open an issue if any item matters to you** and we'll pick it up based on demand.

### Parked

- **Visual motion-zone editor** — cloud read + write via `set_motion_zones` / `get_motion_zones` services is already available; a visual in-card zone editor (draggable polygon overlay) is parked pending at least one user request.
- **Gen2 subscription sensor** — the `/v11/purchases` endpoint could expose active cloud subscription state as a sensor; parked pending community interest.
- **Live thumbnail via local RCP+** — opcode `0x099e` is reachable, but the local XML endpoint returns `<err>0x60</err>` for the `F_DATA` reads we tried (the cloud proxy uses binary TLV on the same path). Use case is narrow anyway: the card already shows live HLS as soon as the LOCAL session is up.

## Releases

Latest: **v13.7.2** — see the GitHub release page for full notes:
[**v13.7.2 release notes →**](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/releases/tag/v13.7.2)

| Version | Highlights |
|---|---|
| **v13.6.0** | **Cross-platform reliability round — iOS Picture-in-Picture, smarter stream recovery, and a credential-exposure fix.** Picture-in-Picture now works on iPhone and iPad (Safari); live-stream sound unmutes on the first tap; a page restored from the browser's back/forward cache reliably offers tap-to-play on iOS instead of failing silently; and returning to the app on Android no longer opens a duplicate stream. The live-stream RTSP URL — which embeds local camera credentials — is no longer exposed through the camera entity's attributes (recorder history, REST API, logbook); update recommended. Diagnostics no longer freeze on a camera left on a 24/7 live view, plus a round of reconnect/teardown and audio robustness fixes across Chrome, Safari, Firefox and Edge on macOS, Windows, iOS, Android and Linux. |
| **v13.5.17** | **Live-stream sound, tab-switch resilience, and quieter controls.** A reliability-focused card release hardened across Chrome, Safari, Firefox and Edge on macOS, Windows, iOS, Android and Linux. Sound no longer comes back muted after an automatic reconnect, a stray Tab/arrow keypress can't silently drop it, and a brief buffering pause re-arms one-click sound recovery. The stream now recovers when you return to a backgrounded tab or restore the page from the browser's back/forward cache (it used to come back frozen). The audio and Picture-in-Picture buttons now appear when the stream starts and hide when it stops instead of sitting greyed over a snapshot, and the card corners no longer flicker. The Bosch cloud maintenance banner can be dismissed with an ×, and privacy mode now shows the last live snapshot time (`privacy_stale_source: event` restores the old behaviour). Card-only — no config changes. |
| **v13.5.16** | **Picture-in-Picture — keep the live stream floating over your other apps.** A new ⧉ button in the card's control bar pops the live stream into the browser's floating, always-on-top window so you can keep an eye on a camera while you work elsewhere — over every app on macOS Safari, over the browser on Chrome. The floating window shows the camera name. Because browsers allow only one PiP window at a time, the PiP button on every other camera greys out while one is floating, and it keeps playing across a stream reconnect. Works on both the single card and the overview tiles (live WebRTC view); hidden where the browser has no PiP support. Bonus tip: if a camera has audio, Chrome's *Live Caption* (Settings → Accessibility) transcribes — and *Live Translate* can translate — spoken audio in the stream, no setup needed. Also bundles two accumulated reliability fixes (bounded diagnostic deferral for 24/7 streams, a de-flaked test). |
| **v13.5.15** | **No more brief live-stream freeze during a motion-event burst (Gen2 outdoor cameras).** Gen2 cameras share a single secure control channel between the live-stream keepalive and every cloud/diagnostic read. During a flurry of motion events that channel could saturate, the stream timed out and reconnected, and the video froze for 5–10 seconds. Diagnostic cloud reads are now deferred while a stream is running (picked up again the moment the stream is idle, so sensors never go permanently stale), and motion-event snapshots are reused instead of fetched twice. A new *Defer diagnostic reads while streaming* toggle (Options → Stream, on by default) lets you restore the old behaviour. Also: the card now prints its version to the browser console on load, and an internal log line that CodeQL flagged was removed. |
| **v13.5.14** | **Live snapshot, privacy badge, and audio for multi-card setups.** A transient cloud hiccup during the periodic refresh could silently replace the current snapshot with a days-old motion-event image; the camera now keeps the last known live frame and only falls back to an event image on a true cold start. In privacy mode, the card now labels the shown frame with the exact date and time of the last event (*Letztes Ereignis: …*), so a held snapshot is clearly dated. And if you place the same camera on a dashboard more than once, only the first card plays audio — the others auto-mute — so the same microphone no longer echoes in every instance. Card-only change. |
| **v13.5.13** | **Security patch — TLS certificate validation for Bosch cloud and login ([CWE-295](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/security/advisories/GHSA-6qh5-x5m5-vj6v)).** Outbound connections to Bosch's cloud API, login (OAuth) and live video proxy now verify the server certificate instead of accepting any certificate — closing a vulnerability where an attacker on your local network could intercept the connection and capture your Bosch login tokens. Because Bosch's cloud uses its own private certificate authority (not in the public trust store), the integration bundles and pins that CA alongside the system trust store, so everything keeps working while impostor certificates are refused. Local camera connections over your LAN are unchanged. **Update recommended for all users.** Reported by [EQSTLab](https://github.com/EQSTLab). |
| **v13.5.12** | **The live stream starts with sound again when the audio switch is on.** Browsers force every video to begin muted, so the card now treats your *start-stream* tap as the unlock: when you start the stream and the audio switch is on, sound comes on by itself as the first frame appears — no second tap on the audio button needed. After a reload the stream still starts muted (no page may play sound before you interact with it), but your **first click anywhere** now reliably restores it. For sound *immediately* after a reload, install the dashboard as an app (Chrome ⋮ → *Install app*) or use the Companion App — installed apps are allowed to play sound on load. The card still never unmutes without a real user gesture, so the stream cannot freeze. Card-only change. |
| **v13.5.11** | **Cards show up in the new card picker (HA 2026.6), plus audio fixes.** Picking a Bosch camera entity in the *Add card* dialog now suggests the *Bosch Camera Card* and *Bosch Camera Overview* directly under the Community section — no more typing `custom:` by hand (uses the new `getEntitySuggestion` hook; offered only for Bosch camera entities). And audio no longer mutes itself after a few minutes — a transient pause (buffering, tab throttle, brief network gap) used to re-mute the stream; it now resumes without muting when sound is on. Because browsers force every video to start muted, the card now also restores sound on your **first click anywhere** whenever the audio switch is on, so after a load/reload/restart you no longer have to find the audio button. The card still never unmutes without a real gesture. Card-only change. |
| **v13.5.10** | **Card: the redundant "Private" row no longer reappears in the ⋮ overflow menu.** With `hide_redundant_privacy` on (default), the standalone Private switch row is correctly dropped because the pill bar already carries a privacy button — but on a minimal overview tile it came back the moment you opened the ⋮ (More) tray. A CSS specificity fix keeps it hidden in the tray too, while every other switch row still appears. Card-only change. |
| **v13.5.9** | **Stop button reachable over overlays, plus a short remote WebRTC attempt.** The control pill bar now sits above the start/loading overlays, so Stop stays tappable while the "tap to start" gate or the warming-up spinner is showing. On an external address the card now briefly attempts WebRTC (~2.5 s) before falling back to HLS — a client that can reach the stream directly (VPN or LAN) gets low-latency WebRTC, while a true-remote client falls back quickly. The HLS banner shows only on an actual fallback. |
| **v13.5.8** | **Live stream no longer drops/freezes, fullscreen exit + volume slider fixed.** Sound is now enabled only by a real tap on the audio pill (browser autoplay rules pause a gesture-lessly unmuted video, which froze the stream); a pause-guard re-mutes and resumes if anything else pauses it. The "Green IT" idle reaper is now opt-in and OFF by default (experimental). The fullscreen exit button works again, double-tap no longer triggers zoom ([#16](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/16)), and the volume slider reflects the real level instead of snapping to 0 on mute. |
| **v13.5.7** | **Maintenance patch — no user-facing change.** CI hardened ahead of GitHub's 2026-06-16 Node-24 runner cutover (`codeql-action` v3→v4, `checkout` v4→v5), test-only dependencies pinned away from known advisories, and two unused variables plus a stale lint directive removed from the card bundle. |
| **v13.5.6** | **Green IT idle-stream reaper, mobile/HLS idle fix, privacy button greying.** A new power-saving option ends a camera's live session once nobody has watched it for about three minutes (an active HLS/WebRTC viewer or a Mini-NVR recording always counts as a consumer). Streams opened in the mobile app are now correctly recognised as idle via real HLS fetch recency. While privacy mode is on, the stream and snapshot buttons are disabled and greyed instead of failing the tap. |
| **v13.5.5** | **On-video pan arrows + cleaner privacy control.** 360° cameras get large left/right pan arrows on the picture that work in fullscreen ([#33](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/33)), via a new `pan_overlay: auto \| always \| never` option; they appear only on cameras that expose a pan position. New `hide_redundant_privacy` option (on by default) drops the duplicate "Private" row when the pill bar already shows a privacy button ([#15](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/15) / [#27](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/27)). The "HLS mode" banner is now localised across all 11 card languages. |
| **v13.5.4** | **Maintenance: Home Assistant 2026.6 readiness.** No user-facing changes. Internally, the reauth/reconfigure flow no longer uses a config-flow reload method that Home Assistant 2026.6 deprecates when combined with an options update listener (it would become an error in 2026.12) — credentials are still applied via an explicit reload, and the careful "reload only when options actually change" behaviour is unchanged. Also drops the now-unnecessary `ignore: brands` from the HACS validation workflow, since HACS recognises the integration's bundled `brand/` icons (HA 2026.3 brands proxy). |
| **v13.5.3** | **Reliability & security hardening across the integration.** The live stream now recovers from an expired token (refresh + retry) and snapshot renewal is no longer cut short on slow cameras; light, white-balance and audio writes survive a token rotation. Settings you change — detection mode, motion sensitivity, alarm delays, lighting and audio — no longer briefly snap back before the cloud catches up, and changing two sibling controls at once no longer lets one overwrite the other. Hardening: the schedule-rule list is fully HTML/attribute-escaped, alert filenames are sanitised, event alerts never attach the wrong camera's image or fetch a rejected URL, the webhook is restricted to `http(s)`, and Digest credentials no longer appear in debug logs. No new entities or breaking changes. |
| **v13.5.2** | **Mute now sticks across restarts, and a card can pin its own audio default.** The audio mute is now backend-owned and persistent: your mute/unmute choice is restored after a Home Assistant restart instead of every stream coming back with sound. A brand-new camera now starts **muted**, and your first toggle sticks for good (the old forced "default on" is gone). New per-card `audio_default: backend \| on \| off` option — by default the card follows the `switch.<cam>_audio` mute, or you can pin a single card to always start muted (or always unmuted) regardless of the backend switch. |
| **v13.5.1** | **Loading overlay no longer sticks after the stream starts, plus a visible stream cooldown.** Starting the live stream sometimes left the "loading…" spinner on screen even though the picture was already playing — only an app reload cleared it. The overlay now hides reliably the moment the first frame arrives. And the stream start/stop button now shows the same short **cooldown countdown** as the privacy button: after you stop a stream the backend needs a few seconds before it will start it again, so the button briefly shows the remaining seconds instead of letting a too-early tap bounce back. |
| **v13.5.0** | **Card in 11 languages, audio you can automate, smoother overlays.** The card editors and all in-card text now follow your Home Assistant language — English, German, Spanish, French, Italian, Dutch, Polish, Portuguese, Russian, Ukrainian and Simplified Chinese — matching the integration's own translations. **Audio is now backend-owned and synced across devices:** a `switch.<cam>_audio` mute that toggles on every tap (so muting/unmuting syncs to every browser and is automatable) plus a new `number.<cam>_audio_volume` slider; an optional `use_card_audio_settings:` card option decouples a single card from the global audio entities if you prefer. **Fullscreen digital zoom** — pinch, mouse-wheel, double-tap and pan. **Overlay polish:** the privacy placeholder no longer stacks under a "refreshing" spinner, and a card pointed at a non-existent camera entity now says **"camera not found"** instead of a misleading re-login prompt. The hover lift is now **shadow-only** — no geometry shimmer and it never clips the fullscreen/zoom view ([#15](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/15)). **Backend stream-recovery hardening:** the 401 rescue / proxy-died rebuild now tears the old local proxy down and rebuilds it **atomically under one lock**, so a concurrent session renewal can no longer leave go2rtc/HA-Stream pinned to a dead port; and a cloud **444** (session quota / a freshly re-paired camera) now falls back to the local API for privacy writes. Test coverage stays at **100%**. |
| **v13.4.5** | **Stream resilience — self-healing local sessions.** When the camera rotates its local streaming credentials the integration rebuilds the local proxy on a fresh port; the rescue now **retries with backoff (up to three attempts)** instead of giving up after a single transient refuse, so go2rtc and the HA stream are no longer left pinned to a dead port (which showed as a frozen image until a manual reload). The **card now recognises a dead go2rtc source** (connection refused, wrong user/pass, exec/rtsp, DESCRIBE 404) and forces one backend stream rebuild via the live-stream switch, with a cooldown so it stops hammering a stale source. Also folds the card-state fixes from the re-pointed v13.4.4: the **overview tile hover keeps your themed `ha-card-box-shadow`** ([#15](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/15)) and the **privacy/light buttons clear their marked state the instant you toggle them off** ([#27](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/27)) instead of waiting for the next status push. |
| **v13.4.4** | **Card UX polish — hover, overview shadows, instant audio.** The single card's hover now **lifts and scales** like the overview tiles ([#15](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/15)). Overview tiles now show a themed **`ha-card-box-shadow`** ([#21](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/21)) — the shadow moved onto the tile itself, which the corner-cropping `overflow:hidden` had been clipping. The **Sound toggle reflects audibility the instant playback starts** ([#22](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/22)) — it reads off (muted) at stream start and a single tap unmutes, without waiting for a state update. **Internal:** mock-`hass` Playwright interaction tests (hover / privacy / audio / fullscreen), a full-E2E `hass-taste-test` smoke against a real Home Assistant, and a `docs/ci-cd.md` pipeline doc with diagrams. |
| **v13.4.3** | **Privacy stops the live stream + sound immediately** ([#22](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/22)). Switching a camera to Privacy while the stream was playing tore the stream down on the backend, but the card's HLS buffer kept playing video and sound for a few seconds and the controls felt stuck. The card now stops its video the instant privacy turns on. |
| **v13.4.2** | **Audio toggle that makes sense, mobile fullscreen, theme-aware geometry.** The **Sound toggle now reflects whether you actually hear sound**: a live stream starts muted (browser autoplay policy), so the toggle reads off with a "tap for sound" hint and a **single tap unmutes instantly** — no more "on but silent", and no off/on workaround. **Mobile fullscreen** — closing one camera's fullscreen no longer immediately opens another camera's (single-owner rule + a short exit guard). **Theme-aware card geometry** ([#21](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/21)) — the cards now follow your dashboard's standard `ha-card-border-radius` / `ha-card-box-shadow` / `ha-card-border-width` theme variables by default (one theme change styles every card), with optional per-card `border_radius:` / `box_shadow:` overrides; applies to both the single card and the overview grid. Hover lifts now use a transform so a themed card shadow stays visible on hover. |
| **v13.4.1** | **Card controls simplified + theme-friendly.** The in-card **Design (iOS/Android) and Mode (Day/Night) switcher buttons were removed** — both are now set purely in YAML (`theme: ios \| android \| auto`, default iOS; `mode: auto \| day \| night`, default auto), keeping the control menu clean. **Overview card respects its column width** — tiles no longer overflow a narrow dashboard column (the grid track is capped to the container width). **No jump on expand** — an overview tile no longer shifts upward when you open its ⋮ menu, and the single card now gets the same subtle hover lift for consistency. **Theme variable support** ([#21](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/21)) — new `border_radius:` and `box_shadow:` card options let the cards match a themed dashboard; they default to the signature Apple-style look, and a dashboard theme that zeroes `ha-card-border-radius` no longer strips the card's rounding. **"Live" badge is instant** — it flips to green "Live" the moment the video actually plays instead of lingering on orange "Connecting" for several seconds while the backend status caught up. **Audio toggle fix on the 360° Indoor** ([#22](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/22)) — switching Audio off then on again now takes effect immediately; previously the toggle stayed visually stuck until the camera was panned. **Internal:** added CodeQL + gitleaks CI, ruff/mypy/ESLint/codespell quality gates, cross-OS Playwright smoke, and format-robust regression tests. |
| **v13.4.0** | **Live-stream state syncs across browser sessions.** The stream lifecycle is now driven by the shared backend `stream_status` (idle → connecting → warming_up → streaming), pushed by HA to every client — so a card opened in a second browser/device shows the *same* "connecting / waking up / Live" state as the one that started the stream, instead of a stale "idle". This removes a bug where the second session looked idle, the user tapped start again, and that re-toggle tore down the first session's stream. The connecting overlay text comes from the shared status too, so all sessions show the same message. The **"Live" badge now reflects the actual video** — green only when *your* video is really playing, "Connecting" (orange) while connecting, never a premature "Live" just because the switch is on; and the "Camera waking up…" overlay clears the moment the video plays even if the backend status lags. **Design/Mode switcher now mirrors the config** — a card with `theme: ios` / `mode: night` shows that option selected in the menu instead of always "Auto". **Cross-OS robustness** — added an `os-<name>` host class (Windows/macOS/iOS/Android/Linux) for OS-targeted CSS and a cross-platform `system-ui` / Segoe UI font fallback (the card is developed on macOS; this hardens rendering on Windows/Edge and Linux). |
| **v13.3.3** | **Removed the on-card debug line** — the small `Card vX | fresh … | WxH` diagnostics line at the top of the card is gone (it lived inside the otherwise-hidden header). No functional change. If the card looks unchanged after updating, your browser is serving a cached copy — force a refresh (Safari: ⌘+⌥+R "Reload From Origin"; HA Companion app: Settings → Companion App → Debug → Reset frontend cache; Chrome/Edge/Firefox: Ctrl/⌘+Shift+R). |
| **v13.3.2** | **Fullscreen button now closes fullscreen again** — on desktop/Android the ⛶ button entered fullscreen but a second tap did nothing (only `Esc` exited). The card requests fullscreen on an element inside its shadow root, where `document.fullscreenElement` retargets to the shadow host, so the old "already fullscreen?" check never matched; detection now reads `shadowRoot.fullscreenElement` and the button toggles in and out. **New card no longer stuck on one camera** — a freshly added `bosch-camera-card` defaulted to a hard-coded entity (`camera.bosch_garten`) that only exists on the author's instance, and the visual editor's camera picker stayed empty when entity names didn't contain "bosch". The picker now derives its default from your own cameras and lists every `camera.*` entity, and keeps the configured value selected. **Two new hide options** — `show_title: false` and `show_last_event: false` strip the title pill and last-event badge for a clean video-only tile (pairs with `compact: true` to match the overview-card grid look); both default to on, available in YAML and the visual editor. Applies to single cards and to overview-grid tiles via `card_defaults`. **`minimal` is now meaningful in the default (Apple-style) layout** — a standalone card shows its full control stack (switches + accordions) expanded by default with the ⋮ "More" button pre-opened; `minimal: true` keeps everything collapsed behind ⋮. Overview-grid tiles default to `minimal: true` so the grid stays glanceable (tap a tile's ⋮ to reveal its controls). The overview editor now exposes the same Design / hide sub-features as the single card. **Offline cameras show less chrome** — the garbled overlapping label on offline tiles is fixed (the redundant top title-pill is hidden; the centered "Camera Offline" pill is the single source of truth), and in the expanded layout an offline camera now hides the unusable control stack (keeping only attached automations). **White strip below the video removed** — collapsed control rows left ~20 px of `padding`/`border` showing under `max-height:0`; now zeroed so the card sizes exactly to the video. |
| **v13.3.1** | **Privacy/light toggle no longer interrupts streams on other cameras** — toggling privacy mode or the camera light on one camera triggered a coordinator-wide state broadcast that briefly flashed a reconnecting/HLS overlay on every other camera on the same dashboard; each card now skips the re-render path for unrelated entities. **Loading spinner centered in HA mobile app** — `inset` CSS shorthand unsupported on older iOS WebViews caused the spinner to anchor bottom-right instead of center; replaced with explicit four-side properties. **Tap reliability improved** — tap-to-play and fullscreen overlay touch handling improved for mobile. **`bosch-camera-autoplay-fix.js` deprecated** — the separate autoplay-watchdog resource is now a no-op; the card self-heals on its own; the resource is auto-removed on next HA restart, no action needed. **Internal:** intrusion-detection `distance` maximum aligned to API limit (8 m); test coverage expanded for switch on/off modes. |
| **v13.2.5** | **WebRTC race on first stream-start fixed** — `BoschCamera.is_streaming` returned True the moment `try_live_connection` populated `_live_connections`, BEFORE the 25-35 s pre-warm wrote `rtspsUrl`. Card's `_waitForStreamReady` saw `cam.state === "streaming"`, immediately fired `camera/webrtc/offer`, HA's go2rtc rejected with `Camera has no stream source`, card 5-s-timed-out and fell back to HLS — making WebRTC look like "only works after a browser reload." `is_streaming` now mirrors `stream_source`'s gate (entry present AND has `rtspsUrl`/`rtspUrl`). WebRTC succeeds on first stream-start; TLS-proxy logs show `User-Agent: go2rtc/1.9.14` confirming the actual transport. **Loading overlay leaks during privacy on/off closed.** Five layered fixes for paths where "Refreshing…" / "Loading image…" stuck while the shutter was closed or during the 12 s post-off re-snapshot grace: `_setLoadingOverlay` entry-guard, `_update` privacy-on force-clear, gated direct-DOM paths in `_restoreCachedImage` and `_onImageLoaded`, gated `set hass` firstHass block, and a pre-pass at the TOP of `_update` that sets `_privacyOffSuppressUntil` BEFORE the same-tick stream-stopped + backend-waiting overlay shows could fire. **Stuck "Connecting" badge** when the card mounted on an already-active stream now self-heals: video-element `!paused && currentTime > 0 && readyState >= 2` force-clears the stale `_startingLiveVideo` flag + dismisses the overlay (the `playing`-event listener that would normally do it is no longer in scope). **Loading overlay stacked on OFFLINE** for ~15 s of HTTP retries — stream-OFF transition's snapshot-fetch + overlay-show now gated on `!_isOffline`. **Card 404 noise eliminated** — `_pullFreshSwitchStates` filters by `id in hass.states` before the REST call (Gen2 LED rings live under `light.*`, but the default fallback fabricated `switch.X_camera_light`). |
| **v13.2.4** | **Live-stream stutter fix (Lovelace card).** Card was re-asserting `video.muted = false` on every Home Assistant state update, even when the element was already unmuted. Chrome's autoplay policy treats every such assignment as a fresh unmute attempt and, without a user gesture in scope, pauses the video — producing visible 1-2 s stutters every time *any* tracked entity changed (observed ~11×/refresh in real logs). Now sets `muted` only on actual state transitions and gates unmute on a user-gesture flag (`pointerdown`/`keydown`/`touchstart` once-listener registered at card mount). **Stream-worker auth-rescue lowered to 1 error for 401.** HTTP 401 from the LOCAL TLS proxy is an unambiguous "Bosch rotated session creds" signal; the existing rescue path waited for `max_stream_errors` (5) errors before re-issuing `PUT /connection`, but HA's stream component coalesces identical worker errors so the counter could plateau at 4 indefinitely and the rescue never fired (frozen image until manual restart). Auth errors now bypass the threshold and trigger the rescue on the first occurrence; non-auth errors keep the original 5-error gate. **FCM diagnostic visibility.** `_LOGGER.warning("FCM registration failed: %s", err)` was being swallowed by `_FCMNoiseFilter` whenever `str(err)` contained one of the noise markers (`PHONE_REGISTRATION_ERROR`, `Unable to complete gcm auth request`, `Unable to establish subscription`), starving operators of insight into why the heal ladder kept tripping. Marker substrings are now masked in the diagnostic line so it passes the filter. **Card 404 noise eliminated.** `_pullFreshSwitchStates` no longer issues REST `GET /api/states/<id>` for entities that aren't tracked in `hass.states` (typical case: Gen2 LED ring lives under `light.*`, but card default fell back to `switch.X_camera_light` and 404'd every refresh cycle). |
| **v13.1.2** | Patch — **FCM push during setup no longer crashes** (`async_handle_fcm_push` was reading `coordinator.data.keys()` before the first refresh populated it, raising `AttributeError: NoneType.keys()` four times per boot in live logs); guard added matching the existing `__init__.py:1576` pattern. **HA startup no longer waits five minutes on the LOCAL session keepalive** — `_replace_renewal_task` was scheduling `_auto_renew_local_session` (a `while True` loop) through `hass.async_create_task`, which made HA's startup-wrap-up phase block until the 5 min timeout and emit *"Something is blocking Home Assistant from wrapping up the start up phase"*. Switched to `hass.async_create_background_task` (the documented HA API for permanent loops). +4 regression tests pinning both fixes; full suite stays green. |
| **v13.1.1** | **FCM two-stage self-heal** — soft (preserve credentials, library uses lightweight `gcm_check_in()` refresh) on listener death, hard (purge + fresh register) only when credentials are proven stale by recent `PHONE_REGISTRATION_ERROR`-class markers or actually missing. Previously every watchdog trigger purged credentials, forcing fresh `gcm_register()` and hitting Google's transient PHONE_REGISTRATION_ERROR cascade that paused the heal ladder for 24 h. Now soft escalates to hard only if the restart did not bring the listener back. **UPDATING UX** — `sensor.bosch_<cam>_status` gets new enum value `updating`, and `camera`, `light`, `privacy switch`, `live-stream switch` entities flip to `unavailable` during a firmware install so dashboards do not show stale data and automations do not try to control a rebooting endpoint. New attribute `camera_timestamp_overlay` on the camera entity; **card hides the redundant last-event glass pill** when the camera burns its own date/time into the video. **FW-update Signal alert template** (three automations — available / install-started / install-complete) shipped at `automation_fw_update_alert.yaml`. +14 regression tests, full suite 4459. |
| **v13.1.0** | Day/Night card-chrome mode (Auto / Day / Night switcher in the More menu, parallel to the iOS / Android theme); visual editor for the single card (`bosch-camera-card-editor` — covers camera_entity, title, apple_style, theme, mode, minimal, compact, auto_play); compact tile mode (`compact: true` hides pill-bar + status badge for Apple-Home-style grid tiles); last-event glass pill in the bottom-right of every video with smart time format (`HH:MM` today, weekday+date for older events); hover scale + drop shadow on overview-grid tiles (desktop only); privacy dot recolored from amber to systemPurple (Apple "shielded" convention); light active state recolored from systemRed to amber (lamp metaphor, breaks the "everything red" collision); offline state redesign with the camera name on its own line below the OFFLINE pill; mobile scroll-flicker fix via `transform: translateZ(0)` GPU layer hint on glass elements; Android theme specificity fix (`:host(.X.mode-day:not(.theme-android))` syntax); focus-visible rings on all interactive elements; `prefers-reduced-motion: reduce` + `prefers-contrast: more` rules; WCAG AA contrast fixes on three places (connecting badge text, toggle chip text, Android offline badge). |
| **v13.0.0** | **Major: Apple-style card redesign + theme switcher.** Glass title pill + semantic status badge overlay the top of the video; glass six-button pill-bar (Snapshot, Live, Privacy, Light, Fullscreen, More) overlays the bottom; the More menu reveals every other switch and the Auto / iOS / Android theme selector. iOS theme uses the SF-Pro / glass-blur look; Android theme switches to Material You / M3 (solid surface-variant containers, 28 px container radius, tonal buttons, M3 dark color tokens). Theme is detected from the user-agent on first load, overridable via the in-card switcher (persisted in `localStorage` and broadcast to every Bosch card on the page), or pinned via YAML `theme: ios | android | auto`. `apple_style: false` keeps the legacy chrome. Integration and card now share version 13.0.0 so feature parity is visible at a glance. This release also bundles every reliability, connectivity, and feature improvement delivered across the 40+ v12.x patches — see `docs/version-history.md` for the full breakdown. |
| **v12.8.4** | FCM self-heal hardening: (a) purges every `fcm_*` key from entry data (was only two), recovering from `PHONE_REGISTRATION_ERROR` loops where stale `fcm_config` / `fcm_registered_device_type` markers blocked a fresh checkin; (b) exponential cool-down ladder (30 min → 1 h → 3 h → 6 h → 24 h) with ±20 % jitter and a 6-step ceiling, replacing the fixed 30 min cadence; (c) registration storms (`PHONE_REGISTRATION_ERROR`, "Unable to complete gcm auth request", "Unable to establish subscription") now count toward the watchdog's storm trigger, not just connectivity drops; (d) `asyncio.Lock` collapses the setup-time start vs first self-heal race that registered two device tokens in 2 s. Card v2.16.10: status dot + offline overlay now case-insensitive so offline cameras render red instead of grey. +6 regression tests pinning purge-scope, race, marker set, and case-fix. |
| **v12.8.3** | FCM push watchdog recovery gap closed: when a previous self-heal failed (e.g. Google returns `PHONE_REGISTRATION_ERROR` during a transient IP rate-limit), event detection used to stay on polling until the next HA restart. The watchdog now retries the self-heal once the 30 min cool-down expires, so push detection recovers automatically. +4 regression tests pinning the retry / cool-down / opt-out / happy-path branches. |
| **v12.8.2** | Card patch (v2.16.9): suppress the phantom CONNECTING badge + loading overlay that appeared on dashboard open without user intent (snapshot-refresh side-effect was opening live sessions backend-side); LAN badge hidden by default, only "Cloud" shows when stream actually falls back to the Bosch relay. |
| **v12.8.1** | Tap-to-reveal auto-play gate. New `auto_play_default` option (Features section): `lan` (default — LAN auto-reveals, mobile/tunnel shows a Play overlay), `always`, `never`. Overlay only appears once the backend stream is on; zero HLS bytes to the phone until tap. Browser-side LAN detection via `hass.config.internal_url` origin match + RFC-1918 fallback — works in HA Companion App iOS / Android + browsers. Per-card YAML override `auto_play:`. Card v2.16.7. +17 pin tests; 4408 suite total. |
| **v12.7.2** | Bosch-app-parity sprint: Mic/Speaker level sliders (Gen2), Intrusion-detection sensitivity + distance numbers, configurable motion-sensor active window (10-300s, default 90), WiFi-RSSI + Firmware diagnostic sensors. +141 regression tests, 4314 suite total. Same-day cross-port to Python CLI v10.7.4, ioBroker v0.7.7, MCP v1.3.3. |
| **v12.5.1** | Hotfix: revert the v12.5.0 Indoor II light entity — Eyes Indoor II has no controllable light hardware. Includes registry cleanup migration that removes the orphan `light.bosch_*_frontlicht` plus three stale Indoor II number orphans on next startup. |
| **v12.5.0** | LAN-fallback hardening (HTTPS + Digest on RCP writes, hw_version + Digest creds persisted across HA restarts), new `bosch-notifications-card` for the Lovelace dashboard (active / scheduled / recent maintenance + cam status), maintenance + cloud-state notification dedup persisted across restarts (kills 20× duplicate alerts during a single outage), switch / light availability gate now allows hw-unknown + LAN-reachable. Card v2.14.0 with `https://`-scheme guard for sensor-attribute URLs. |
| **v12.4.12** | Live-stream switch now tracks user intent, not raw session state — auto-opens (Cast / dashboard preload / `play_stream` / WebRTC watchdog refresh) no longer flip the visible switch; teardown clears `_live_connections` before NVR-stop so the switch correctly flips OFF even when Mini-NVR cleanup raises; webrtc-watchdog refresh now scoped to cameras with active sessions; health-watchdog race after user-OFF closed |
| **v12.4.11** | Cloud up/down transition alerts; suppressed during maintenance windows; 11 pin tests |
| **v12.4.10** | LAN-fallback: persistent LAN-IP store, outage-ping sweep, `binary_sensor.*_lan_reachable`, privacy/light switches stay available on LAN, cloud-degraded startup, front-light Gen2 LOCAL RCP, card v2.13.0 |
| **v12.4.9** | Card empty-state regression fix — blank panel after all cameras go unavailable; card v2.12.13 |

| | |
|---|---|
| **All releases** | [GitHub Releases page](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/releases) — every tagged version with notes + downloadable assets |
| **Full history** | [`CHANGELOG.md`](CHANGELOG.md) — same notes, browseable inside the repo |
| **Card version** | tracked in `src/bosch-camera-card.js` (`CARD_VERSION`) and mirrored in `custom_components/bosch_shc_camera/const.py` (`CARD_VERSION`) |
| **Integration version** | `custom_components/bosch_shc_camera/manifest.json` (`version`) |

## Integration Comparison

The Bosch Smart Home Camera reverse-engineered API is exposed via four sibling projects. Pick the one that fits your platform.

| Feature | [Home Assistant Integration](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant) | [Python CLI Tool](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-Python) | [ioBroker Adapter](https://github.com/mosandlt/ioBroker.bosch-smart-home-camera) | [MCP Server](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-MCP) | [Frontend (NiceGUI)](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-Python-frontend) | [Node-RED](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-NodeRED) |
|---|---|---|---|---|---|---|
| **Maturity** | v13.5+ — HA Quality Scale **Platinum** | v10.10+ stable (Mini-NVR BETA) | v1.5+ stable · npm | v1.5+ stable · PyPI | v0.1.2 **alpha** · PyPI | v0.2.3 **alpha** · npm |
| **Platform** | Home Assistant (HACS) | Standalone Python 3.10+ CLI | ioBroker (npm) | Python 3.10+ · pipx / uvx · stdio + streamable-HTTP for MCP clients (Claude Desktop, Claude Code, custom) | NiceGUI web app · Python 3.10+ | Node-RED palette · npm |
| **Login** | OAuth2 PKCE (browser) | OAuth2 PKCE (browser) | OAuth2 PKCE (browser) | OAuth2 PKCE (browser, one-time) | ◑ shares CLI `bosch_config.json` | ◑ refresh-token from CLI |
| **Snapshots** | ✅ Native `Camera.image` | ✅ `snapshot` command | ✅ File-store + base64 DP | ✅ `bosch_camera_snapshot` (LAN-only) | ✅ live + event fallback | ✅ `snapshot` node |
| **Live RTSP stream (LAN)** | ✅ via HA Stream component | ✅ ffmpeg/RTSPS output | ✅ TLS proxy → local RTSP | ✅ `bosch_camera_stream_url` (LAN-only, no cloud relay) | ◑ internal (go2rtc) | ◑ `stream-url` node (URL only) |
| **WebRTC (sub-second latency)** | ✅ via integrated go2rtc | ✅ *(v10.6.0)* `live --webrtc` | ❌ | ❌ | ✅ via go2rtc (else snapshot) | ❌ |
| **Dual-stream URL (main + sub)** | ✅ `sensor.bosch_<n>_stream_url` + `_sub` *(v12.4.0, opt-in per cam)* | ✅ `info` shows both · `live --sub` *(v10.5.0)* | ✅ `stream_url` + `stream_url_sub` *(v0.5.3 experimental)* | ◑ `bosch_camera_stream_url` — main stream only | ❌ *(sub-stream only)* | ◑ URL only — no sub option |
| **External recorder (BlueIris, Frigate)** | ✅ via go2rtc | ✅ stdout pipe | ✅ Digest-creds URL + LAN bind option | ✅ URL returned, hand off to ffmpeg / go2rtc downstream | ❌ | ◑ `stream-url` → wire downstream |
| **Privacy mode** | ✅ switch entity | ✅ command | ✅ DP | ✅ `bosch_camera_privacy_set` (LAN-fallback via `prefer_local`) | ✅ toggle | ✅ `privacy` node |
| **Front spotlight (Gen1/Gen2)** | ✅ light entity | ✅ command | ✅ DP | ✅ `bosch_camera_light_set` (LAN-fallback) | ❌ *(Phase 2 stub)* | ❌ |
| **RGB wallwasher (Gen2 Outdoor II)** | ✅ light w/ RGB | ◑ on/off only — no RGB | ✅ color + brightness DPs | ❌ *(on/off only — RGB not exposed)* | ❌ | ❌ |
| **Panic-alarm siren** | ✅ button entity *(Gen2 Indoor II)* | ✅ command *(Gen2 Indoor II only)* | ✅ DP | ✅ `bosch_camera_siren_trigger` *(Gen2 Indoor II only)* | ❌ | ❌ |
| **Image rotation 180°** | ✅ switch | ❌ | ✅ DP | ❌ | ❌ | ❌ |
| **Motion / person / audio events** | ✅ FCM push + polling fallback | ◑ `watch` command only (events cmd removed) | ✅ FCM push + polling fallback | ✅ `bosch_camera_events` (on-demand pull) | ◑ pull-only events table | ✅ `event` node (poll) |
| **Motion edge-trigger state** | ✅ `binary_sensor.motion` | n/a | ✅ `motion_active` DP *(v0.5.3)* | n/a *(request-response, no subscription)* | ❌ | ❌ |
| **Auto-snapshot on motion** | ✅ refreshes Camera entity | n/a | ✅ writes `last_event_image` base64 *(v0.5.3)* | n/a *(no background loop)* | ❌ | ❌ |
| **Synthetic motion trigger (external sensor)** | ✅ service | n/a | ✅ DP | ❌ | ❌ | ❌ |
| **Motion zones / privacy masks (read)** | ✅ | ✅ | ✅ read-only *(v1.2.0)* | ❌ | ❌ | ❌ |
| **Automation rules / schedules (read)** | ✅ | ✅ | ◑ read-only count + JSON *(v1.2.0)* | ❌ | ❌ | ❌ |
| **Lighting schedule (read)** | ✅ | ✅ | ✅ read *(Gen1-only, v1.2.0)* | ❌ | ❌ | ❌ |
| **Cloud clip download (history ~30 d)** | ✅ via Media Browser | ❌ | ❌ *(parked — no community request yet)* | ❌ *(intentionally not exposed — large payloads)* | ❌ *(use CLI)* | ◑ `clip_url` in event payload |
| **Mini-NVR (motion-triggered local recording)** | ✅ *(v11.2.0 BETA)* | ✅ *(v10.7.0 BETA)* | ❌ | ❌ | ❌ | ❌ |
| **SMB / NAS clip upload** | ✅ | ✅ *(v10.7.0 BETA)* | ❌ | ❌ | ❌ | ❌ |
| **Camera sharing (friends)** | ✅ services (share / invite / list) | ✅ command | ◑ read-only list *(v1.2.0)* | ❌ *(intentionally not exposed — needs user-driven flow)* | ❌ | ❌ |
| **Pan / tilt (360° Gen1)** | ✅ services | ✅ command | ✅ `pan_position` DP | ✅ `bosch_camera_pan` | ❌ *(Phase 2 stub)* | ❌ |
| **Named pan presets (home / left / right / back-left / back-right)** | ✅ opt-in select entity | ✅ `pan --preset` flag | ✅ `pan_preset` DP | ✅ `bosch_camera_pan preset=` | ❌ | ❌ |
| **Two-way audio / intercom** | ❌ | ✅ command | ❌ | ❌ *(intentionally not exposed — timing-sensitive)* | ❌ | ❌ |
| **Webhook delivery on events** | ✅ service + opt-in options | ✅ `watch --webhook URL` | ✅ via MQTT bridge | ❌ *(request-response model)* | ❌ | ❌ |
| **MQTT event bridge (motion / audio / person)** | n/a *(HA event bus native)* | n/a *(single-run)* | ✅ admin-config | n/a | ❌ | ❌ |
| **Apple HomeKit (via HA Core bridge)** | ✅ documented | n/a | n/a | n/a | n/a | n/a |
| **Snapshot scheduler / time-lapse** | ✅ examples/ YAML | ✅ cron + ffmpeg examples | ✅ Blockly example | n/a | ❌ | ❌ |
| **Native dashboard card / widget** | ✅ 2 Lovelace cards (single + grid) | n/a | ✅ 2 vis-2 widgets — BoschCamera + BoschOverview multi-cam | n/a | ✅ *(is itself a web dashboard)* | ❌ |
| **Cloud-relay REMOTE fallback** | ✅ auto-switch when LAN unreachable | ✅ remote mode | ❌ *(LOCAL-only by design)* | ❌ *(media LAN-only; status/events via cloud)* | ◑ inherits CLI | ◑ REMOTE opt (manual) |
| **Browser-based admin / config UI** | ✅ HA Config Flow | n/a (CLI) | ✅ JSON-config tabs | n/a (LLM-mediated; config via CLI / MCP client) | ✅ Settings page | ◑ editor config node |
| **UI languages** | EN · DE · FR · ES · IT · NL · PL · PT · RU · UK · ZH-Hans *(v12.4.0)* | EN · DE · FR · ES · IT · NL · PL · PT · RU · UK · ZH-Hans *(v10.3.0)* | EN · DE · FR · ES · IT · NL · PL · PT · RU · UK · ZH-CN | n/a *(no UI — LLM is the front-end)* | ◑ backend i18n · UI mostly EN | n/a *(English only)* |

**Legend:** ✅ supported · ❌ not supported / not planned · n/a not applicable for this platform.

> All four projects share the same reverse-engineered Cloud API + RCP protocol research, but evolve independently. The Home Assistant integration is the most feature-complete reference implementation; the Python CLI is the lowest-level / scriptable surface; the ioBroker adapter targets VIS dashboards and Blockly automations; the MCP server exposes a curated, LAN-first tool surface to MCP clients (Claude Desktop, Claude Code, custom) for natural-language camera control.

---

## Related Projects

Part of a five-implementation family for Bosch Smart Home Cameras (plus an alpha frontend):

| Implementation | Repo | Status |
|---|---|---|
| 🏆 **Home Assistant Integration** (this repo) | [Bosch-Smart-Home-Camera-Tool-HomeAssistant](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant) | **v13.5.17** · HA Quality Scale **Platinum** · production-ready |
| 🐍 Python CLI | [Bosch-Smart-Home-Camera-Tool-Python](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-Python) | **v10.10.2** · Mini-NVR + SMB upload (BETA) · LAN-fallback (ping / --local) · PTZ presets · webhook delivery · capture / research / standalone |
| 🟢 ioBroker Adapter | [ioBroker.bosch-smart-home-camera](https://github.com/mosandlt/ioBroker.bosch-smart-home-camera) | **v1.5.2** · stable · npm · privacy-toggle Digest rotation · MQTT bridge · PTZ presets · VIS-2 widgets (BoschCamera single-cam + BoschOverview multi-cam) |
| 🤖 MCP Server | [Bosch-Smart-Home-Camera-Tool-MCP](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-MCP) | **v1.5.3** · cred-rotation · PTZ presets · TOFU cert pinning · LAN-ping + prefer_local · Claude Code / Claude Desktop integration |
| 🔴 Node-RED nodes (alpha) | [Bosch-Smart-Home-Camera-Tool-NodeRED](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-NodeRED) | **v0.2.2-alpha** · stream-url node · 4 nodes (event / snapshot / privacy / config) |

Also: [Bosch Smart Home Camera — Python Frontend (NiceGUI)](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-Python-frontend) — **v0.1.1-alpha** (dashboard + camera detail + settings) — community interest welcome

HA stays the **reference implementation** — features land here first; the Python CLI, ioBroker Adapter and MCP Server catch up over time.

---

## License

MIT — see source files.
