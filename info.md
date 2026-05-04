# Bosch Smart Home Camera

Adds your Bosch Smart Home cameras (Eyes Außenkamera, 360 Innenkamera, Gen2 Eyes Outdoor II / Indoor II) as fully featured entities in Home Assistant — including a custom Lovelace card with live streaming, controls, and event info.

## ⚠️ MAJOR CHANGE: Auth provider changed (since v8.0.5)

**Bosch switched to a new OAuth client (`oss_residential_app`) starting with v8.0.5.** Existing installations must **re-authenticate once** to migrate from the legacy `residential_app` client.

### Easiest path (v9.1.5+ — in-place migration, no data loss)

1. Update to v9.1.5 or later via HACS
2. *Settings → Devices & Services → Bosch Smart Home Camera → Configure*
3. If you're still on the legacy client, a **"Migrate to new OAuth client (oss_residential_app)"** checkbox appears at the bottom — enable it and submit
4. Click the **Reconfigure** banner that appears on the integration card → browser opens Bosch SingleKey ID login → log in
5. Done! All your entities, automations, options, FCM config, and SMB settings are preserved.

### Also supported — automatic reauth banner (v9.1.4+)

If your refresh token has already expired on Bosch's side, Home Assistant will automatically show a **Reconfigure** banner on the integration card the moment Keycloak rejects the stored token. Clicking it runs the same auto-login flow. No manual action required.

### Legacy manual fallback (still works)

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
- **Alarm system** — 75 dB siren, pre-alarm LED, arming/disarming (Gen2 Indoor)
- **3-step alerts** — instant text → snapshot (5s) → video clip (30-90s) via any HA notify service
- **Media Browser** (v10.7.0+) — browse downloaded events under *Media → Bosch SHC Camera*; works for both local downloads and SMB-uploaded NAS shares (streamed on demand, no HA disk cost)

## Documentation

Full documentation, supported entities, configuration options, and troubleshooting: [README on GitHub](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant#readme)

## Disclaimer

This is a community-developed integration, **not affiliated with Robert Bosch GmbH**. It uses a reverse-engineered, undocumented API. Provided **as is**, without warranty.
