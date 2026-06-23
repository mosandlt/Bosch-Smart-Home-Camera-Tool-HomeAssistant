# Version History — Bosch Smart Home Camera HA Integration

Recent releases. For the full changelog see [`CHANGELOG.md`](../CHANGELOG.md) at the repo root or the [GitHub Releases page](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/releases).

## v13.7.9 — 2026-06-23

Patch — fixes the live stream getting stuck on "connecting" (and the related connect↔live flip) in the mobile app and on cellular networks. The cause: a WebRTC connection could come up and report itself "connected" while never actually delivering a decoded video frame — over carrier-grade NAT / 5G the media path forms and then goes silent with no error or state change, so the existing "no track" timeout never fired and the card kept retrying WebRTC instead of switching to the HLS fallback (which is why the HLS-mode banner never appeared). The card now watches the WebRTC statistics after a track arrives: if no media bytes are flowing, or bytes flow but no frame ever decodes, it switches to HLS for the rest of the session and shows the HLS-mode banner — while a connection that does deliver frames keeps the low-latency WebRTC path untouched. This is guarded so a healthy stream is never pushed onto HLS by mistake (a browser that reports no statistics, or a stream that rendered fine and then briefly dropped, is left alone). The native HLS player on iOS also gets an 8-second load watchdog that hard-reloads if it stalls at startup and does not recover on its own, so the fallback itself is reliable. The HLS-mode banner now appears for any mobile client running on HLS, not only the remote-tunnel case. No change to any camera, sensor or backend behavior; card-only. 5414 pytest, 201 card e2e (Chromium + Firefox + WebKit), mypy --strict + ruff/eslint/css/codespell clean.

## v13.7.8 — 2026-06-22

Patch — a reliability round for live video, hardening the cases where a stream could freeze or stop and not recover on its own (both Picture-in-Picture and normal tab use). After a deep code review plus web research, several recovery gaps were closed. A frozen WebRTC picture is now detected directly at the decoder — a new check counts decoded frames via the WebRTC stats API, in addition to the existing presented-frame and track-event signals — which catches a silent stall the older signals miss, including a recent browser behavior that pauses a muted video in a background tab. A stale-connection guard prevents a just-closed peer connection from tearing down the freshly reconnected stream (a self-reinforcing reconnect loop that read as "it keeps breaking"). A dead HLS source now escalates to a clean rebuild instead of retrying the same URL indefinitely; the stream-ready wait no longer gives up silently after 90 seconds; and a failed play() is retried and then recovered. On the backend, the go2rtc stream is re-registered with fresh credentials after the camera rotates them, even for viewers who only ever use WebRTC, so the picture no longer freezes once the previous credentials expire. The Picture-in-Picture reconnect gap was also shortened. No change to any camera, sensor or other backend behavior. 5416 pytest, 165 card e2e (Chromium + Firefox + WebKit), mypy --strict + ruff/eslint/css/codespell clean.

## v13.7.5 — 2026-06-21

Patch — follow-up to v13.7.4's Picture-in-Picture fix for a remaining background-tab case (Chrome on macOS). With the live stream floating in Picture-in-Picture and the browser tab left in the background, both the in-page tile and the floating window could freeze; returning to the tab recovered them after a few seconds, but the floating window you were actually watching stayed frozen for up to a minute in the meantime. The cause: the card's freeze detector runs on a periodic timer, and Chrome throttles background-tab timers to roughly once a minute — so even though v13.7.4 added the right freeze signal (presented-frame liveness), the code reading it only woke up about once a minute while hidden. The card now drives that check from a Web Worker, which runs on a separate thread Chrome does not throttle, so a frozen Picture-in-Picture stream is detected and reconnected in about ten seconds instead of up to a minute, with no interaction needed. The worker only acts on a Picture-in-Picture window you are actively watching — a plain hidden tab is left alone to conserve the limited camera session — and falls back cleanly to the existing timer where Web Workers are unavailable. Also hardens the freeze check so a slow reconnect's first frame can't briefly look "frozen". No change to any camera, sensor or backend behavior; card-only. 5368 pytest, 150 card e2e (Chromium + Firefox + WebKit), mypy --strict + ruff/eslint/css/codespell clean.

## v13.7.4 — 2026-06-19

Patch — fixes a frozen Picture-in-Picture window after a background tab switch (Chrome). With the live stream floating in Picture-in-Picture, switching to another browser tab for a while could freeze the floating window; returning to the tab resumed the in-page video but the floating window stayed frozen on the last frame. Two things combined: a hidden tab heavily throttles the card's periodic stall check (so recovery effectively only happened once you switched back), and the underlying WebRTC stream from go2rtc can quietly die while the tab is in the background. The card now detects a freeze without relying on that throttled timer — it watches for presented video frames (which keep flowing to a Picture-in-Picture window even while the tab is hidden) and listens for the WebRTC track going silent or the connection failing, then reconnects the stream into the same floating window automatically, with no interaction needed. The reconnect reuses the existing video element so the Picture-in-Picture window picks the stream straight back up. No change to any camera, sensor or backend behavior. 5368 pytest, 147 card e2e (Chromium + Firefox + WebKit), mypy --strict + ruff/eslint/css/codespell clean.

## v13.7.3 — 2026-06-18

Patch — fixes the integration's **Configure** dialog (#35). Opening Settings → Devices & Services → Bosch Smart Home Camera → Configure failed with `500 Internal Server Error` for every user, on every camera model. The AI options section (added in v13.7.0) declared four fields in a form Home Assistant's frontend schema serializer cannot convert (a `vol.Any` with an empty-string member), so the whole options form failed to render. The two AI entity pickers now use the serializer's supported nullable form (clearable — submits no entity when emptied) and the two AI active-time fields are plain text inputs validated at runtime, so the dialog opens again. A new regression test serializes the options schema exactly the way Home Assistant does, so an unconvertible field is caught in CI instead of in the browser. No functional change to any camera, sensor, stream or the Lovelace card (card version unchanged). 5256 pytest, mypy --strict + ruff/codespell clean.

## v13.7.2 — 2026-06-18

Patch — a timezone fix plus a mobile reliability/perf batch. **Last-event timestamp (#34):** `sensor.<cam>_last_event` showed the event time exactly two hours late in CEST (correct again now). Bosch event timestamps carry an explicit timezone offset (e.g. `…+02:00[Europe/Berlin]`); the previous code truncated that offset away and re-labelled the local reading as UTC. The offset is now honored everywhere it matters — the last-event sensor, the events-today / movement / audio counters (which now bucket by the event's local date, fixing miscounts around midnight), and the motion active-window check (which no longer treated fresh events as two hours in the future). **Live stream reliability:** idle-online cameras pre-warm more gracefully (a stream toggle that races an auto-open no longer logs a false error or drops the user's intent); the card escalates an HLS reconnect loop to a backend re-warm only after genuinely repeated failures; and a Picture-in-Picture stream no longer stays frozen when its tab is hidden. **Snapshot performance:** the image entity serves an in-RAM copy of the last frame between refreshes instead of reading from disk on every `/api/image_proxy` request (the iOS app and dashboards re-fetch the same signed URL repeatedly), and a shared cloud session is reused on hot paths. **Logging:** several cold-start-expected conditions dropped from WARNING to DEBUG. 5253 pytest, 141 card e2e, mypy --strict + ruff/eslint/css/codespell clean.

## v13.7.1 — 2026-06-17

Patch — snapshot/image display, mostly mobile, plus one security fix. **Security:** SMB upload now verifies the cloud-media download over TLS (was `CERT_NONE`), pinned to the Bosch CA (CWE-295). **Mobile black/grey image:** offline cameras showed a black tile and privacy cameras a grey tile in the Companion app (browser was fine — it has a localStorage image cache the app webview lacks) — the card now loads the last good frame the backend serves as the backdrop behind the offline/privacy overlay; the backend also no longer serves its 1×1 placeholder as a real cached image on cold start (with a back-off so offline cameras aren't polled every request). **Mobile native camera view:** the entity no longer advertises STREAM while OFFLINE, so tapping an offline camera (more-info / picture-glance live) falls back to the snapshot instead of a black live-stream attempt; offline cameras hide their control icons in fullscreen; WebRTC is skipped on iOS over plain http (→ HLS). **Other:** stuck loading spinner on offline cameras suppressed; stuck "refreshing" overlay cleared on error; live view recovers cleanly after a camera drops offline mid-stream; offline overlay localized for all 11 languages; fewer redundant snapshot requests (per-camera trigger targeting, staggered timers, single-fetch caching). Note: the iOS Companion app caches custom cards and ignores `?v=` — after updating, Reset frontend cache once if a camera still looks stale. 5224 pytest, 141 card e2e, mypy --strict + ruff/eslint/css clean.

## v13.7.0 — 2026-06-16

Minor release — new opt-in **AI snapshot descriptions** (Home Assistant AI Task describes what a camera sees, on motion or via the `describe_snapshot` service; cooldown / daily-budget / time-window / presence gating; per-camera sensor; optional in notifications — off by default), plus reliability fixes: UTC-date bucketing for the events-today sensors, FCM retry only after a real fetch success, event-poll resilience (a cloud blip no longer blanks the events list or delays the next poll), the camera staying available during a known cloud maintenance window while streaming locally, a synchronous in-flight guard against double snapshot refreshes, AI-caption reappearance + volume-slider listener cleanup in the card, and AI options-flow validation (clearable entity gates, unlimited daily budget, HH:MM validation). Full suite green, mypy --strict + ruff clean, card e2e green.

## v13.5.7 — 2026-06-03

Maintenance patch — no user-facing behaviour change. CI hardening ahead of GitHub's 2026-06-16 Node-24 action cutover, test-only dependency security pins, and dead-code removal in the card bundle.

- **CI on Node-24-native action majors.** `github/codeql-action` `v3 → v4` (the v3 line stops running on the new runner image) and `actions/checkout` `v4 → v5` across the remaining workflows. No change to what the gates check — only the action runtimes.
- **Test-dependency security pins.** `pytest-homeassistant-custom-component` bumped (pulls `zeroconf 0.149.16`) and `idna >= 3.15` pinned, clearing four transitive advisories in the test toolchain. These are dev/CI-only dependencies and never ship to your installation. Four `pyjwt` advisories remain pinned to Home Assistant core's exact `PyJWT==2.12.1` and will clear when core itself bumps.
- **Card bundle cleanup.** Removed two unused variables and a stale lint directive; the bundle is a little smaller. Behaviour is identical.

## v13.5.6 — 2026-06-03

Minor release — a new **Green IT** power-saving option, a fix for streams lingering after mobile/HLS viewing, and privacy-mode button greying.

- **Green IT (new, on by default).** The integration now automatically ends a camera's live session once nobody is watching it any more — when no app, dashboard or Cast has fetched the stream for about three minutes. This stops the camera from continuously encoding and streaming video to no one: it saves Wi-Fi bandwidth and camera power/heat, turns the camera's live LED off, and leaves the session cleanly ready for the next viewer. Pressing Stop still ends a stream instantly; this only catches the "closed the app / navigated away without pressing Stop" case. An active viewer (HLS or WebRTC) or a running Mini-NVR recording always counts as a consumer and is never interrupted, and watching again resets the timer. Toggle under Options → Live stream → "Green IT"; it is an umbrella flag that future power-saving behaviours will hang off.
- **Fix: streams opened in the mobile app could linger after closing.** The idle detector relied on HA's `Stream.available`, which reports "can serve", not "is serving", and stays true for the whole session once HLS was ever used — so a stream watched on mobile and then closed was never recognised as idle. Consumer presence is now read from real HLS playlist/segment fetch recency (plus go2rtc consumers and active recordings), so an abandoned mobile/HLS session is torn down about three minutes after the last fetch.
- **Privacy mode greys out the stream and snapshot buttons.** While privacy mode is on the camera shutter is closed, so starting a stream or taking a snapshot cannot work; both buttons are now disabled and greyed in the card (classic and Apple-style layouts) instead of letting the tap fail.
- Internal: new idle-session reaper with full test coverage, HLS-access tracking added to the cf_unbuffer HLS view wrappers, e2e tests for the privacy button greying, and forward-compatibility verified against the upcoming HA 2026.6 beta.

## v13.3.1 — 2026-05-29

Patch release — two card-rendering fixes, two interaction improvements, a deprecated watchdog resource, and internal test and API-limit cleanup.

- Privacy/light toggle on one camera no longer causes a brief reconnecting/HLS overlay flash on other cameras on the same dashboard. Each card now skips the re-render path when the changed entity belongs to a different camera.
- Loading spinner is now correctly centered in the HA mobile app. The `inset` CSS shorthand is unsupported on older iOS WebViews; replaced with explicit `top`/`right`/`bottom`/`left` declarations.
- Tap reliability for the tap-to-play and fullscreen overlays improved on mobile (touch handling).
- `bosch-camera-autoplay-fix.js` deprecated — the card self-heals on its own; the watchdog resource is now a no-op and is auto-removed on next HA restart. No action needed.
- Internal: intrusion-detection `distance` number entity maximum aligned to the API limit (8 m). Test coverage expanded for switch on/off modes.

## v13.3.0 — 2026-05-28

Minor release — five fixes verified against live hardware (Eyes Außenkamera II, Eyes Innenkamera II, Gen1 Outdoor, Gen1 360° Indoor).

- `light.bosch_<cam>_frontlicht` turn-on now reliably updates HA state. `_put_lighting_switch` returns HTTP 204 No Content; the previous code tried `resp.json()` on the empty body, raised silently, left `brightness=0` in the cache, and caused HA's verify timeout to fire every time. On JSON-parse failure of a 2xx response the sent body (already the merged post-write state) is now written into the cache instead.
- Four service handlers guarded against privacy-mode rejection. The Bosch cloud returns HTTP 443 `sh:camera.in.privacy.mode` for writes while the shutter is closed; `BoschFrontLight.async_turn_on`, `_BoschRgbLedLight.async_turn_on`, `BoschPanicAlarmSwitch._set`, and `_BoschAlarmDelayBase.async_set_native_value` now early-return with a persistent notification instead of issuing a PUT that is rejected silently.
- Internal: 3 new test modules + updates to 3 existing modules covering the new branches.

## v13.2.5 — 2026-05-27

Patch release — six Lovelace card state-rendering bugs and one integration race, surfaced via osascript-driven Chrome testing against a live install.

- `BoschCamera.is_streaming` now mirrors `stream_source`'s gate (entry present AND `rtspsUrl`/`rtspUrl` populated). Previously it returned True the moment `try_live_connection` wrote to `_live_connections`, causing the card to fire `camera/webrtc/offer` before the pre-warm wrote `rtspsUrl` — go2rtc rejected with `Camera has no stream source` and the card fell back to HLS. WebRTC now succeeds on first stream-start.
- Privacy on→off loading-overlay leaks closed: five layered fixes ensure the "Aktualisiere…" / "Bild wird geladen…" / "Stream wird gestartet…" overlays are suppressed while the shutter is closed and during the 12 s post-off re-snapshot grace window.
- Stuck "Verbinde" badge on card mount with an active stream self-heals: `!paused && currentTime > 0 && readyState >= 2` force-clears `_startingLiveVideo` + dismisses the overlay.
- Loading overlay stacked on top of the OFFLINE state (15 s of HTTP retries) — snapshot-fetch and overlay-show now gated on `!_isOffline`.
- `_pullFreshSwitchStates` filters by `id in hass.states` before the REST call — eliminates 404 noise from the Gen2 LED-ring `light.*` entity fallback.
- `getattr(..., {})` guard on `_hw_version` prevents `AttributeError` in `SimpleNamespace` test stubs (Gen2-gate for RCP 0x099e).

## v13.2.4 — 2026-05-27

Patch release — four bugs surfaced in a live debugging session (HA 2026.5, Indoor Gen2 FW 9.40.102, Lovelace HLS).

- Live-stream stutter fixed. Card was re-asserting `video.muted = false` on every HA state tick; Chrome's autoplay policy pauses the element on each assignment without a user gesture — causing 1-2 s stutters ~11×/cycle. Now sets `muted` only on actual state transitions and gates unmute on a user-gesture flag set at card mount.
- Stream-worker 401 rescue lowered from 5 errors to 1. HA's stream component coalesces identical worker errors so the counter could plateau at 4 indefinitely; auth errors now bypass the threshold and trigger the rescue immediately.
- FCM diagnostic line no longer suppressed by `_FCMNoiseFilter` — marker substrings are masked before logging so the cause is visible without reopening the log-flood vulnerability.
- `_pullFreshSwitchStates` 404 noise eliminated — entities not present in `hass.states` are silently skipped.

## v13.2.3 — 2026-05-26

Patch release — production bugs from a thorough code scan plus logging, options-flow, and async hygiene improvements.

- `bosch_shc_camera_intrusion` webhook event now fires on rising edge of `alarmStatus.alarmType` (was registered but never triggered). Payload: `{camera_id, camera_name, alarm_type, intrusion_system, timestamp}`.
- Stale go2rtc + Stream state on integration reload fixed. `_async_cancel_coordinator_tasks` now tears down per-cam streams (`_tear_down_live_stream`) before stopping proxies.
- RCP-LAN HTTP 401 throttle: per-`(cam_id, opcode_hex)` denied cache (24 h TTL) eliminates repeated 401s for opcodes the account lacks permission for.
- Exception logs with empty `str(err)` now fall back to `repr(err)`.
- `use_mjpeg_snapshot` option description corrected (off by default; FFmpeg-TLS incompatibility explained).
- `enable_intercom` option now actually gates entity registration (was always registered regardless).
- Blocking `threading.Event.wait(timeout=2)` removed from the asyncio event loop in `tls_proxy.start_tls_proxy`.
- Dead-code cleanup: 7 unused imports, `BoschAcousticAlarmButton` class, 11 dead OAuth translation keys, `device_automation.trigger_type.*` section removed.
- Test suite: +47 tests, 4514 passed / 17 skipped.
