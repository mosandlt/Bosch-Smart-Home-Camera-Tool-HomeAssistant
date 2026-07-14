# Stream Performance Baseline — 2026-07-13

Baseline measurement for the stream-performance/stability refactor (see `docs/stream-perf-stability-refactor-plan.md`). Pure measurement session — no deploy, no code change, no HA restart. Test HA instance. Debug logging for `custom_components.bosch_shc_camera` was already enabled (Options → Debug logging).

## Method

- LOCAL session establishment: called `switch.turn_on` on the camera's `live_stream` switch via REST (`/api/services/switch/turn_on`), which is a **blocking service call** that only returns once the coordinator's `try_live_connection` pipeline finished — so the call's own `time_total` is a solid proxy for "time to a usable LOCAL session." Cross-checked against `ha core logs` timestamps for the underlying pipeline (`PUT /connection` → TLS proxy start → TLS proxy connect → RTSP `DESCRIBE` handshake → go2rtc stream registration → "Live stream active" log line).
- Snapshot fetch: `curl -w` TTFB/total against `/api/camera_proxy/camera.<id>`, 3 reps per camera, 1s apart.
- `camera_proxy_stream` (continuous MJPEG proxy) was also tried as an alternative cold-start probe but **never returns a first frame** for this integration (15s timeout, no data) — it does not drive `try_live_connection` the way the docs assumed. Not used further; switch-based measurement above used instead.

## LOCAL session establishment (switch.turn_on → session ready)

| Camera | Run | Result | Notes |
|---|---|---|---|
| Innenbereich | 1 (cold, idle ≥30s before) | **25.25s** | True cold path — see pipeline breakdown below |
| Innenbereich | 2 (re-run 15–75s after previous teardown) | **0.003s** | Session/credentials reused — coordinator did not redo the full RTSP/go2rtc handshake |
| Innenbereich | 3 | invalid | Real-world automation flipped `switch.bosch_innenbereich_privacy_mode` to ON mid-session (door/night automation on the shared test property) — entity went `unavailable`, call was a silent no-op (HA logged a WARNING). Session ended here; no further toggling attempted to avoid interfering with the live automation. |
| Terrasse | — | **not measurable** | `switch.bosch_terrasse_privacy_mode` was ON for the entire session → `live_stream` switch stayed `unavailable`, and `async_camera_image` never issues a fresh fetch (log: `skipping image refresh — privacy mode is ON`, later `fresh fetch failed — returning cached (Ns old)`). No LOCAL session was ever opened for Terrasse this session. |

### Cold-path pipeline breakdown (Innenbereich, representative cycle, log timestamps)

| Step | Δ from turn_on | Absolute |
|---|---|---|
| `switch.turn_on` issued | 0.000s | 06:20:37.865 |
| `PUT /connection` type=LOCAL → HTTP 200 | +0.274s | 06:20:38.139 |
| TLS proxy thread started | +0.275s | 06:20:38.140 |
| TLS proxy TCP+TLS connect to camera | +1.846s | 06:20:39.711 |
| RTSP `DESCRIBE` → 401 (expected, digest challenge) | +2.394s | 06:20:40.259 |
| RTSP `DESCRIBE` (authed) → 200, SDP received | +2.428s | 06:20:40.293 |
| "Pre-warm RTSP complete" | +2.428s | 06:20:40.293 |
| go2rtc stream registered + "Live stream active" | ~+25.28s | 06:21:03.149 |

The RTSP/TLS/digest handshake itself completes in **~2.4s**. The remaining ~23s to go2rtc registration/"Live stream active" is the dominant cost in the cold path — worth profiling further in the refactor (not root-caused in this measurement session; candidate culprits not investigated here: go2rtc's own polling interval for the `/api/streams` PUT, or a fixed wait in the integration's post-pre-warm sequencing).

## Snapshot fetch (`/api/camera_proxy/camera.<id>`), 3 reps

| Camera | Run 1 | Run 2 | Run 3 | Notes |
|---|---|---|---|---|
| Terrasse | 2.7ms (TTFB 1.7ms) | 2.9ms | 2.9ms | Privacy mode ON → served from RAM cache every time, identical byte size (520056B) all 3 runs. Not a live fetch. |
| Innenbereich | 2.20s (TTFB 2.20s) | 213ms | 188ms | Run 1 was effectively cold (right after a LOCAL session cycle); runs 2–3 warm, served from the LOCAL live-snap cache (`fetch_live_snapshot`). |

## Caveats / not measurable this session

- Terrasse: privacy mode was ON for the whole session (real property state, not something we toggled) — zero LOCAL-session and zero live-snapshot data for this camera. Re-run when privacy is off.
- Innenbereich: only 1 clean cold sample before a real-world automation (door/night → privacy ON) took the switch `unavailable` and ended further testing. Did not attempt to override the automation or toggle privacy — out of scope for a passive baseline measurement.
- `camera_proxy_stream` is not a usable cold-start probe for this integration (never triggers `try_live_connection`, times out with no data).
- No REMOTE-path (cloud relay) measurement — both available cycles were LOCAL only.
- Sample size is small (1 cold + 1 warm LOCAL cycle) due to the above; treat the 25.25s cold number as a single data point, not a statistically solid baseline. Re-run with more reps once the privacy-mode/automation interference is controlled for (e.g. schedule the next measurement session for a window when the property automations won't flip privacy mid-test).
