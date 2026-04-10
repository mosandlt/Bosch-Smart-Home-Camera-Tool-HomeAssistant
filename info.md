# Bosch Smart Home Camera

Adds your Bosch Smart Home cameras (Eyes Außenkamera, 360 Innenkamera, Gen2 Eyes Outdoor II / Indoor II) as fully featured entities in Home Assistant — including a custom Lovelace card with live streaming, controls, and event info.

## ⚠️ MAJOR CHANGE: Auth provider changed (since v8.0.5)

**Bosch switched to a new OAuth client (`oss_residential_app`) starting with v8.0.5.** Existing installations must **re-authenticate once**, otherwise the token will expire and the re-login will fail with a **404 error**.

### Recommended (v9.1.0+ — Auto-Login)

1. Remove the integration → *Settings → Devices & Services → Bosch Smart Home Camera → Delete*
2. Re-add the integration → browser opens automatically for Bosch SingleKey ID login
3. After login you're redirected back to HA automatically — done!

### Manual fallback

1. *Settings → Devices & Services → Bosch Smart Home Camera → Configure*
2. Enable **"Re-login"** at the bottom → Submit
3. Open the displayed URL in your browser, log in at Bosch
4. After login you'll land on a **404 page (`bosch.com/boschcam`) — this is expected!**
5. Copy the **full URL** from the address bar (contains `?code=...`) and paste it into HA

---

## Features

- **Native HA entities** — camera, sensors, switches, lights, binary sensors, buttons
- **Custom Lovelace card** — live streaming with HLS/WebRTC, snapshot, light controls, motion zones overlay
- **Gen1 + Gen2 support** — Eyes Außenkamera (Gen1), Eyes Außenkamera II (Gen2), 360 Innenkamera (Gen1), Eyes Innenkamera II (Gen2)
- **Local streaming** via TLS proxy — bypasses cloud for low-latency LAN streaming
- **OAuth2 Auto-Login** (v9.1.0+) — one-click setup via my.home-assistant.io
- **FCM push notifications** — real-time motion alerts via Bosch's Firebase backend
- **Privacy mode**, **camera light**, **wallwasher** (Gen2: top + bottom RGB LEDs with color picker)
- **Motion zones**, **privacy masks**, **detection mode** (DualRadar on Gen2)

## Documentation

Full documentation, supported entities, configuration options, and troubleshooting: [README on GitHub](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant#readme)

## Disclaimer

This is a community-developed integration, **not affiliated with Robert Bosch GmbH**. It uses a reverse-engineered, undocumented API. Provided **as is**, without warranty.
