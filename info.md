# Bosch Smart Home Camera

Adds your Bosch Smart Home cameras (Eyes Außenkamera, 360 Innenkamera, Gen2 Eyes Outdoor II / Indoor II) as fully featured entities in Home Assistant — including a custom Lovelace card with live streaming, controls, and event info.

> **Quality Scale: Platinum** (achieved in v12.0.1, current release: v16.1.0) — `strict-typing` (mypy --strict green across the codebase, 0 errors), `async-dependency` (all `requests` imports removed; HTTP via aiohttp + `auth_utils.async_digest_request`), runtime data on the config entry, raised + translatable service-action exceptions, downloadable diagnostics with secret redaction, repair issues for token-expired / Bosch-outage states, in-place reconfigure flow, automatic stale-device cleanup, full icon translations, pytest config-flow coverage. See `quality_scale.yaml` for the rule-by-rule status.

## Login

One-click OAuth2 login via `my.home-assistant.io` — log in with the same Bosch SingleKey ID account you use in the Bosch Smart Camera app. If Bosch ever rejects your stored token, Home Assistant automatically shows a **Reconfigure** banner on the integration card; click it to log in again, no manual steps needed. A manual copy-the-link fallback is also available if the automatic browser redirect ever gets confused (rare — mobile/in-app webviews mostly). Full detail in the [README](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant#readme).

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
- **Mini-NVR** (opt-in, BETA) — continuous or event-buffered local recording with a pre-roll ring buffer, no cloud storage needed
- **External recorders** (Frigate / BlueIris / go2rtc) — persistent, credential-free RTSP endpoint per camera, opt-in
- **AI Camera Analysis** (opt-in, v16.1.0+) — motion-triggered 1-10 suspicion scoring via Home Assistant's AI Task integration, with known-visitor context and a dedicated timeline card

## Documentation

Full documentation, supported entities, configuration options, and troubleshooting: [README on GitHub](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant#readme)

## Disclaimer

This is a community-developed integration, **not affiliated with Robert Bosch GmbH**. It uses a reverse-engineered, undocumented API. Provided **as is**, without warranty.
