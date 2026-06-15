# Changelog

Full release history for the Bosch Smart Home Camera HA integration.

Newest first. The README only highlights the most recent release — for older
versions see this file or the [GitHub Releases page](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/releases) (each release page mirrors the same notes plus downloadable assets).

## [v13.6.0] - 2026-06-15

Cross-platform reliability round driven by a structured bug-hunt across the card and the Python backend (Chrome, Safari, Firefox, Edge on macOS, Windows, iOS, Android, Linux).

- **iOS Picture-in-Picture**: the PiP button now works on iPhone and iPad (Safari), falling back to the WebKit presentation-mode API where the standard one is unavailable.
- **iOS audio & playback**: sound now unmutes on the very first tap when you start a stream; a page restored from the browser's back/forward cache reliably offers tap-to-play instead of silently failing; iPad is now recognised when choosing the remote-stream transport.
- **Android**: returning to the app no longer opens a second, duplicate stream, and the autoplay latch is cleared correctly.
- **Security**: the live-stream RTSP URL — which embeds local camera credentials — is no longer exposed through the camera entity's attributes (recorder history, REST API, logbook). Credentials were already redacted in the logs; this closes the matching attribute path. Update recommended.
- **Diagnostics**: sensors no longer freeze on a camera left on a 24/7 live view (a deferred diagnostic read is now force-completed past its time bound).
- **Robustness**: a lingering snapshot helper process is now reaped on timeout, reconnect/teardown guards were hardened, and several internal "never-run" time sentinels were aligned.

No configuration changes.

## [v13.5.17] - 2026-06-15

Reliability-focused card release, hardened by a cross-browser / cross-OS pass (Chrome, Safari, Firefox, Edge on macOS, Windows, iOS, Android, Linux).

- **Live-stream sound**: no longer comes back muted after an automatic reconnect (stall / HLS-fallback / session refresh); first-click-unmute only reacts to real activation keys (a stray Tab/arrow can't drop sound); a transient buffering pause re-arms one-click sound recovery; iOS first-tap / Android / multi-card edge cases fixed.
- **Tab-switch & lifecycle**: stream recovers on return to a backgrounded tab or back/forward-cache restore (was frozen); paused-while-hidden detected and reconnected; leaked teardown timers/listeners cleaned up; a transient ICE blip no longer forces a premature HLS downgrade.
- **Quieter controls**: the audio and Picture-in-Picture buttons appear on stream start and hide on stop (no greyed controls over a snapshot); card corners no longer flicker on overlay fade / mouse-out.
- **Bosch cloud maintenance banner**: dismissible with an × (per browser, per maintenance window).
- **Privacy mode**: the placeholder shows the last live snapshot time by default (`privacy_stale_source: event` restores the old "last event" behaviour).

Card-only release — no configuration changes required.

## [v13.5.16] - 2026-06-14

Picture-in-Picture for the live stream. A new ⧉ button pops the live WebRTC stream into the browser's floating, always-on-top window (over all apps on macOS Safari, over the browser on Chrome); the floating window's title shows the camera name (via Media Session). Only one PiP window is allowed by the browser, so the PiP button on every other camera greys out while one is floating, and the window keeps playing across a stream reconnect. Available on the single card and the overview tiles (live WebRTC view); hidden where the browser lacks PiP support (most iOS/Android WebViews). README documents the feature plus the Chrome Live Caption / Live Translate subtitle tip. Also bundles two accumulated fixes: a bounded slow-tier diagnostic deferral so a 24/7 stream can't freeze diagnostics, and a de-flaked stream-cooldown e2e test.

## [v13.5.14] - 2026-06-11

Patch release — live-snapshot stale-event fix, privacy badge, and multi-instance audio mute.

- **Fix: the live snapshot no longer flips to an old event image.** With privacy off, a transient cloud hiccup on the periodic refresh could replace the current snapshot with a days-old motion-event picture. The card now keeps the last good live frame in that situation and only falls back to an event image on a true cold start (no live frame has ever been fetched for that camera).
- **New: a "last image" badge in privacy mode.** When the camera is in privacy mode the card labels the shown frame with the date and time of the last motion event (e.g. *Letztes Ereignis: …*), so a held snapshot from days ago is clearly dated and cannot be mistaken for a live view.
- **Audio: cards no longer echo when the same camera is shown twice.** If more than one card on a dashboard shows the same camera, only the first one plays audio; every additional instance auto-mutes itself. (The ~80–200 ms A/V drift on indoor cameras is a characteristic of the Bosch/go2rtc AAC transcoding path — it is not something the card can correct.)

## [v13.5.13] - 2026-06-11

Security patch — TLS certificate validation for Bosch cloud and login (CWE-295, [GHSA-6qh5-x5m5-vj6v](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/security/advisories/GHSA-6qh5-x5m5-vj6v)). Update recommended for everyone.

- **Outbound connections to Bosch's cloud, login (OAuth) and live video proxy now verify the server certificate.** Earlier versions disabled certificate validation on those calls, so an attacker on the same network (e.g. via ARP or DNS spoofing) could have intercepted the connection, captured your Bosch login tokens, or tampered with cloud responses. The integration now validates every Bosch cloud/login/proxy connection against the proper Bosch certificate authority and rejects anything that doesn't chain to it.
- **Why a simple "turn verification on" wasn't enough.** Bosch's cloud is served by Bosch's own (private) certificate authority, which isn't in the public trust store — so naively enabling validation would have broken the integration for everyone. This release bundles and pins the Bosch CA alongside the system trust store, so the cloud API, the Let's Encrypt-signed login host, and the video proxy all validate correctly while impostor certificates are refused.
- **Local camera connections are unchanged.** Snapshots and streams pulled directly from a camera's LAN IP keep using the camera's own self-signed certificate over your private network — that path is not exposed to the cloud and is unaffected.
- Reported by [EQSTLab](https://github.com/EQSTLab). Thank you for the responsible disclosure.

**Also fixed since v13.5.12** (accumulated maintenance):

- **Privacy toggle:** rapid taps during the cooldown window are now debounced/coalesced instead of raising, and the state-drift recovery path now actually forces a coordinator refresh.
- **Login/session:** a persistent `401` now falls through to the next connection candidate in Auto mode instead of getting stuck on a dead one.
- **Streaming:** the go2rtc unregister loop only ends on a real removal (HTTP 200/204); the cloud-outage LOCAL snapshot fallback is now skipped while a stream is active; and a file-descriptor leak in the TLS-proxy keepalive failure path is closed.
- **Hardening:** camera titles coming from the cloud are sanitized before they are used in the video-clip file path (path-traversal guard).

## [v13.5.12] - 2026-06-05

Patch release — the live stream starts with sound again when the audio switch is on.

- **Starting the stream now turns sound on by itself.** Browsers force every `<video>` to begin muted and only allow un-muting inside a real user gesture — but the stream warms up for ~15-35 s after you tap *start*, long after that tap's activation has expired, so the card could not simply un-mute when the picture appeared. The card now banks your start tap the way YouTube and audio-heavy web apps do: it resumes a short-lived `AudioContext` synchronously inside the tap, which keeps sound permitted while the WebRTC stream connects. The moment the first frame plays, sound comes on by itself (when the audio switch is on) — no second tap on the audio button. If the browser still refuses, the existing pause-guard resumes the stream muted, so it can never freeze (the v13.5.8 safeguard is fully preserved).
- **First click after a reload reliably restores sound.** A page reload auto-starts the stream without any user gesture, so it must begin muted — no browser (YouTube included, outside its own privileged origin) may play sound before you interact with the page. Your first click anywhere now routes through the same `AudioContext` bridge, so the un-mute sticks instead of being re-muted a few seconds later.
- **Want sound immediately after a reload?** Install the dashboard as an app — Chrome ⋮ → *Install app*, or use the Companion App. Installed apps in standalone mode are allowed to play sound on load, which a normal browser tab is not. Card-only change.

## [v13.5.11] - 2026-06-05

Patch release — card-picker entity suggestions (HA 2026.6) and an audio-mute fix.

- **The Bosch cards now show up in the new card picker (HA 2026.6).** When you add a card to a dashboard and pick a Bosch camera entity, the *Bosch Camera Card* and *Bosch Camera Overview* appear directly in the picker's Community section — no more typing `custom:` by hand. The suggestion is offered only for Bosch camera entities (a `camera.*` entity whose brand is "Bosch"), so the picker stays quiet for everything else. Built on the new `getEntitySuggestion` hook introduced in [HA 2026.6](https://www.home-assistant.io/blog/2026/06/03/release-20266/). Card-only change.
- **Audio no longer mutes itself, and comes back with a single click.** A transient pause of the live video — buffering, a background-tab throttle, a brief network gap — used to make the pause-guard re-mute the stream, silencing the sound you had switched on. When sound is on, the guard now resumes the video *without* muting first, and only falls back to a muted resume if the browser actually refuses playback. And because browsers force every `<video>` to start muted (no sound without a real gesture), the card now restores sound on your **first click anywhere on the page** whenever the audio switch is on — after a page load, a reload or a stream restart you no longer have to hunt for the audio button; any click counts (the muted-autoplay pattern used by YouTube/Instagram). The card still never unmutes without a real gesture, so the v13.5.8 stream-freeze safeguard is fully preserved.

## [v13.5.10] - 2026-06-03

Patch release — a card-only CSS fix for the redundant privacy row.

- **The redundant "Privat" row no longer reappears in the ⋮ overflow tray.** With `hide_redundant_privacy` on (the default), the standalone Privat switch row is dropped because the Apple-style pill bar already carries a privacy button. On a minimal overview tile, though, the row came back the moment the ⋮ (More) tray was opened: the tray-reveal rule out-specified the dedupe rule in CSS. A higher-specificity guard now keeps the Privat row hidden in the open tray too, while every other switch row still appears. Card-only change, no backend impact ([#15](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/15) / [#27](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/27)).

## [v13.5.9] - 2026-06-03

Patch release — Stop button reachability and a short remote WebRTC attempt.

- **Stop button reachable while the start/loading overlay is showing.** The control pill bar sat below the full-cover overlays (the remote "antippen zum Starten" gate, the warming-up spinner), which swallowed taps on the Stop button so a stream couldn't be ended from the card. The pill bar now sits above them and stays tappable.
- **Remote / VPN: try WebRTC instead of forcing HLS.** For the Home Assistant app or a mobile browser on an external address, the card used to skip WebRTC and go straight to HLS. It now attempts WebRTC with a short (~2.5 s) timeout and falls back to HLS if it doesn't connect — the attempt itself is the reachability check, so a client that can reach the stream directly (VPN or LAN) gets low-latency WebRTC while a true-remote client falls back quickly. The "HLS mode" banner now appears only on an actual HLS fallback.

## [v13.5.8] - 2026-06-03

Patch release — live-stream drop fix, fullscreen exit, volume slider.

- **Live stream no longer drops / freezes.** The browser autoplay policy pauses a `<video>` that is unmuted without a real tap. The card previously auto-unmuted on stream start and on every state sync, which froze the stream. Sound is now enabled only by tapping the audio pill (a real gesture); otherwise the stream stays muted and playing, and a new pause-guard re-mutes and resumes the video if anything else pauses it.
- **Green IT idle-stream reaper is now opt-in and OFF by default.** Marked experimental: its viewer detection could not reliably see a live WebRTC viewer on every setup, so it could tear down a stream you were watching. Idle sessions are still bounded by the 60-minute session recycle; when enabled it only reaps on a confirmed zero consumers, never on an "unknown" reading.
- **Fullscreen & controls.** The fullscreen exit button works again, and double-tapping a control no longer triggers the digital zoom ([#16](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/16)). Pointer events on the pill buttons, pan arrows and volume slider are no longer captured by the zoom handler.
- **Volume slider** reflects the actual level — muting no longer snaps it to 0.

## [v13.5.7] - 2026-06-03

Maintenance patch — no user-facing behaviour change.

- **CI on Node-24-native action majors.** `github/codeql-action` v3 → v4 (the v3 line stops running on the new runner image from 2026-06-16) and `actions/checkout` v4 → v5 across the remaining workflows. The gates check exactly the same things.
- **Test-dependency security pins.** `pytest-homeassistant-custom-component` bumped (pulls `zeroconf 0.149.16`) and `idna >= 3.15` pinned, clearing four transitive advisories in the test toolchain. These are dev/CI-only dependencies and never ship to an installation.
- **Card bundle cleanup.** Removed two unused variables and a stale lint directive; the bundle is a little smaller. Behaviour is identical.

## [v13.5.6] - 2026-06-03

Minor release — a Green IT power-saving option, an idle-stream fix, and privacy-mode button greying.

- **Green IT (new).** The integration ends a camera's live session once nobody has fetched the stream for about three minutes, so the camera stops encoding and streaming to no one — saving Wi-Fi bandwidth and camera power/heat, turning the live LED off, and leaving the session ready for the next viewer. Pressing Stop still ends a stream instantly; an active HLS/WebRTC viewer or a running Mini-NVR recording always counts as a consumer. Toggle under Options → Live stream → "Green IT".
- **Fix: streams opened in the mobile app could linger after closing.** Consumer presence is now read from real HLS playlist/segment fetch recency (plus go2rtc consumers and active recordings), so an abandoned mobile/HLS session is torn down about three minutes after the last fetch instead of never.
- **Privacy mode greys out the stream and snapshot buttons.** While privacy mode is on the shutter is closed, so both buttons are now disabled and greyed in the card (classic and Apple-style) instead of letting the tap fail.

## [v13.5.5] - 2026-06-03

Card-focused release — on-video pan arrows and a cleaner privacy control. No backend changes, nothing breaking.

- **Pan arrows on the video ([#33](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/33)).** 360° cameras get large left/right pan arrows directly on the picture, so the view can be changed without leaving the card — and crucially while in fullscreen, where the menu's `◀◀ ◀ ■ ▶ ▶▶` row is hidden by the browser. Each tap moves one step, a badge shows the new angle, and the arrows grey out at the ends of travel. New option `pan_overlay: auto | always | never` (default `auto`). The arrows appear only on cameras that expose a pan position.
- **Cleaner privacy control ([#15](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/15) / [#27](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/27)).** New option `hide_redundant_privacy` (on by default) removes the duplicate "Privat" switch row when the Apple-style pill bar already shows a privacy button. The legacy layout keeps the labelled row.
- **Localised HLS banner.** The "HLS mode" hint over the video was hardcoded in German and missed the card's 11-language localisation; it now shows in your language with clearer wording. The README gains a short WebRTC-vs-HLS troubleshooting checklist.

## [v13.5.4] - 2026-06-02

Maintenance release — Home Assistant 2026.6 readiness; no user-facing changes, no new entities, drop-in upgrade.

- **HA 2026.6 config-flow deprecation handled.** The reauth/reconfigure flow no longer uses `async_update_reload_and_abort`, which HA 2026.6 deprecates (and would make a hard error in 2026.12) when an integration also has an options update listener. Credentials are still applied via an explicit reload; the "reload only when options actually change" behaviour is unchanged.
- **HACS validation no longer needs `ignore: brands`.** Since HA 2026.3 the integration ships its own brand icons in `custom_components/bosch_shc_camera/brand/`, and the HACS validation action now recognises them, so the workflow validates cleanly without the ignore flag.

## [v13.5.3] - 2026-06-02

Reliability and security hardening release — no new entities, no breaking changes.

- **Live stream recovers from an expired token.** A `401` on the connection PUT now triggers a token refresh and one retry instead of silently failing, so a rotated token no longer leaves a dead stream.
- **Settings no longer snap back.** Changing detection mode, motion sensitivity, or alarm delays no longer briefly reverts in the UI; a short write-lock is held for each, matching the other controls. Changing two sibling controls simultaneously (e.g. speaker and microphone level) no longer lets one overwrite the other in the local cache.
- **Security hardening.** The schedule-rule list is now fully HTML- and attribute-escaped. Event-alert snapshots never attach the wrong camera's image and never fetch a URL that fails the Bosch-domain check; alert filenames are sanitised against path traversal. The optional webhook only accepts `http(s)` URLs. Camera Digest credentials no longer appear in debug logs.
- **Snapshot renewal no longer cut short on slow cameras.** The on-demand snapshot used to renew the live connection inside its own 10-second budget; it now runs outside that budget.
- **Alarm-state sensor maps unknown values correctly.** An unrecognised alarm state now maps to `unknown` instead of being discarded by Home Assistant.

## [v13.5.2] - 2026-06-02

Audio mute is now persistent and backend-owned — your mute choice survives Home Assistant restarts and is the single source of truth across every browser and automation.

- **`switch.<cam>_audio` is a `RestoreEntity`.** Mute state is no longer lost on HA restart. A brand-new camera starts muted; the first toggle sticks for good. The old `audio_default_on` integration option is removed.
- **New per-card option `audio_default`.** A card can declare its own start mute state independently of the backend switch: `backend` (default, follows `switch.<cam>_audio`), `on`, or `off`. Useful for a wall-tablet card that should always start silent regardless of the global switch.

## [v13.5.1] - 2026-06-01

Small card-only release fixing a stuck loading overlay and adding a visible stream cooldown.

- **Loading overlay no longer sticks after the stream starts.** The overlay-hide call ran while the "still connecting" flag was technically still set, so the anti-flicker guard swallowed it. The flag is now cleared first, so the overlay hides reliably the moment the first frame arrives.
- **Stream start/stop button shows a cooldown badge.** The backend needs a few seconds after stopping before it will accept a new start. The button now shows the same brief countdown badge the privacy button already uses, so you can see exactly when it is ready.

## [v13.5.0] - 2026-06-01

Major card and integration release — 11-language card, automatable audio entities, fullscreen digital zoom, and several overlay and recovery fixes.

- **Card in 11 languages.** Both config editors and all in-card text follow the Home Assistant language: English, German, Spanish, French, Italian, Dutch, Polish, Portuguese, Russian, Ukrainian, and Simplified Chinese (English fallback).
- **Automatable audio entities.** Each camera now has `switch.<cam>_audio` (shared mute across all browsers, automatable) and `number.<cam>_audio_volume` (0–100 virtual volume preference). Toggling sound no longer re-opens the stream. Set `use_card_audio_settings: false` on a card to keep its sound independent.
- **Fullscreen digital zoom.** Pinch, mouse-wheel, double-tap, and pan inside the fullscreen view.
- **Overlay and recovery fixes.** The privacy placeholder no longer stacks under a "refreshing" spinner. A card pointed at a non-existent camera entity now shows "camera not found" instead of the misleading "session expired" prompt. The 401 rescue / proxy-died rebuild now tears the old local proxy down and rebuilds it atomically under one lock, preventing go2rtc / HA-Stream from being pinned to a dead port. A cloud `444` response (session quota) now falls back to the local API for privacy writes.
- **Hover lift is now shadow-only.** No sub-pixel edge shimmer, and the lift no longer becomes a containing block that clips the fullscreen/zoom overlay ([#15](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/15)).

## [v13.4.6] - 2026-06-01

Stability and polish round — cold-start stream fix, privacy cooldown indicator, write-path hardening, and card timer cleanup.

- **Cold-start race fixed.** Opening the dashboard with a stream already active could leave the card stuck on the last event snapshot instead of starting live video. The card now self-heals: if the first stream attempt fails before the camera and switch entities have finished loading, it resets and retries on the next state update.
- **Privacy cooldown now visible ([#27](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/27)).** The camera enforces a short cooldown between privacy changes; a toggle inside that window was previously dropped silently, leaving the button looking stuck. The privacy button now shows a countdown and is disabled during the cooldown.
- **Camera-setting writes are now confirmed before updating state.** LED, timestamp overlay, lens elevation, microphone level, and light writes only update the displayed value once the camera confirms the change; a failed write no longer flips the control to the wrong state until the next poll.
- **Card visual editor auto-play fix.** The editor no longer silently pins Auto-Play to "LAN" when you open and save without touching that field; there is now an explicit "use the integration default" option.
- **Security.** The SMB snapshot-upload path now validates event image URLs against a Bosch-domain allowlist before fetching, matching the other upload paths.

## [v13.4.5] - 2026-05-31

Hardens the local LAN streaming path against mid-session credential rotation by the camera.

- **Local session rescue retries with backoff.** When a Bosch camera re-issues its local RTSP credentials, the integration rebuilds the local TLS proxy on a fresh port. A single transient `SSL UNEXPECTED_EOF` or connection reset previously made the one-shot rescue give up, leaving go2rtc and the HA stream pinned to the now-dead port (visible as a frozen image until a manual reload). The rescue now retries up to three times with backoff.
- **Card classifies dead go2rtc sources.** A persistently stale source (connection refused, wrong credentials, DESCRIBE 404) is now classified distinctly from a benign stream-type race, and the card forces one backend stream rebuild via the live-stream switch (with a cooldown) instead of hammering a source that will never recover.
- **Overview tile hover preserves themed shadow ([#15](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/15)).** The hover lift no longer overwrites `ha-card-box-shadow` on overview tiles.
- **Privacy and light buttons clear their state instantly ([#27](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/27)).** The marked state clears the moment you toggle off, instead of waiting for the next status push — most noticeable on a Gen1 camera over LAN.

## [v13.4.4] - 2026-05-31

Card polish release — hover lift on the single card, box-shadow on overview tiles, and instant audio toggle at stream start.

- **Single-card hover lift ([#15](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/15)).** The standalone card now lifts and scales on hover like the overview tiles, anchored at the top edge so it grows without jumping.
- **Overview tile shadows ([#21](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/21)).** A themed `ha-card-box-shadow` now shows on overview tiles; the inner card's shadow was previously clipped by `overflow:hidden` on the tile.
- **Instant audio toggle ([#22](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/22)).** The audio toggle now reflects audibility the moment playback starts — reads as off/muted at stream start; a single tap unmutes — instead of briefly showing a stale "on".

## [v13.4.3] - 2026-05-31

Small fix: privacy mode now immediately stops the live stream and silences audio in the card.

- **Privacy stops video and audio instantly ([#22](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/22)).** When switching a camera to privacy while the live stream was playing, the backend tore the stream down but the card's HLS buffer kept playing video and sound for several seconds, and the controls felt stuck. The card now stops its video element the moment privacy turns on.

## [v13.4.2] - 2026-05-30

Follow-up release — a sensible audio toggle, single-owner mobile fullscreen, and theme-aware card geometry.

- **Audio toggle reflects what you actually hear ([#22](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/22)).** Browsers start video muted by autoplay policy, so the toggle now reads off at stream start with a subtle pulse, and a single tap unmutes immediately (the AAC track is already in the stream — no two-tap dance, no reconnect).
- **Mobile fullscreen is single-owner.** Closing one card's fullscreen on a multi-camera dashboard could immediately open a sibling card's fullscreen because the closing tap landed on the sibling's tap-to-play video. Fullscreen is now single-owner across all cards, with a short guard after any exit.
- **Theme-aware card geometry ([#21](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/21)).** Cards now follow standard HA theme variables (`ha-card-border-radius`, `ha-card-box-shadow`, `ha-card-border-width`) by default. Override per card with `border_radius:` and `box_shadow:` options.

## [v13.4.1] - 2026-05-30

Card controls cleanup — design/mode toggles moved to YAML-only, instant "Live" badge, overview grid overflow fix, and new CI/CD quality gates.

- **In-card Design and Modus switchers removed.** The iOS/Android Design and Day/Night Mode in-card toggles are gone; both are set purely in YAML (`theme: ios | android | auto`, `mode: auto | day | night`) and stay out of the control menu.
- **"Live" badge is instant.** The badge now flips to green Live the moment the video is actually playing, instead of lingering on orange "Verbinde" while the backend status caught up.
- **Overview grid no longer overflows narrow columns.** Tiles are capped to the container width so they do not overflow when the card sits in a narrow dashboard column.
- **Theme-variable support ([#21](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/21)).** New optional card options `border_radius:` and `box_shadow:` let the card match a themed dashboard; a theme setting `ha-card-border-radius: 0` no longer strips the card's own rounding.
- **360° Indoor audio toggle fix ([#22](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/22)).** The Audio toggle now writes its new state to HA immediately instead of staying visually stuck until the next pan event.
- **CI hardened.** CodeQL (SAST), gitleaks (secret scanning), least-privilege workflow permissions, ruff/mypy/ESLint/codespell quality gates, and a cross-OS Playwright smoke matrix added.

## [v13.4.0] - 2026-05-30

Live-stream lifecycle is now driven by the shared backend `stream_status`, syncing state across all browser sessions.

- **Cross-session stream state sync.** HA pushes `stream_status` (idle → connecting → warming_up → streaming) to every connected client. A card opened in a second browser or device now shows the same connecting/waking-up/Live state as the session that started the stream, instead of a stale "idle" that caused a second-session tap to tear down the first session's stream.
- **"Live" badge reflects the real video.** Green only when your video is actually playing; orange "Verbinde" while connecting; never a premature "Live" just because the switch is on.
- **Connecting overlay clears on first frame.** The overlay clears the moment the video plays, even if the backend status sensor lags a few seconds. All sessions show the same message, taken from the shared status.
- **Design/Mode menu mirrors your config.** A card set to `theme: ios` or `mode: night` now shows that option selected in the menu instead of always showing "Auto".
- **Cross-OS robustness.** An `os-<name>` host class (Windows/macOS/iOS/Android/Linux) and a cross-platform `system-ui`/Segoe UI font fallback are added for OS-targeted styling and correct rendering on Windows/Edge and Linux.

## [v13.3.3] - 2026-05-30

Small cleanup release — removes the development diagnostics line from the top of the card.

- **On-card debug line removed.** The `Card vX | fresh … | 1920×1080` diagnostics line at the top of the card (a development aid) is removed. It lived inside the card's otherwise-hidden header, so on the default Apple-style layout this has no visible or functional effect. All other behaviour — streaming, controls, camera picker, fullscreen, and the hide options from v13.3.2 — is unchanged.

## [v13.3.2] - 2026-05-29

Card rework fixing three reported issues — camera picker, fullscreen toggle, and new hide options — plus a behaviour change making the control stack expanded by default.

- **Camera picker no longer sticks to one camera ([#17](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/17)).** The picker used to default to a hard-coded entity and the visual editor's dropdown stayed empty for cameras not named "bosch". The picker now derives its default from the cameras on your instance and lists every `camera.*` entity.
- **Fullscreen button toggles correctly ([#16](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/16)).** A second tap on the fullscreen button now exits fullscreen instead of doing nothing. The detection now reads `shadowRoot.fullscreenElement` so the old "already fullscreen?" check that never matched is fixed.
- **New `show_title` and `show_last_event` options ([#15](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/15)).** Set `show_title: false` or `show_last_event: false` to strip the title pill and last-event badge; combine with `compact: true` for a clean video-only tile. Both options are available in YAML and the visual editor.
- **Controls expanded by default.** A standalone single card now shows its full control stack expanded by default. Set `minimal: true` to keep everything collapsed behind the "Mehr" button as before. Overview-grid tiles default to `minimal: true` so the grid stays glanceable.
- **Offline camera layout cleaned up.** Offline cameras drop the redundant title pill (which overlapped the centred "Offline" label) and hide the unusable control stack, keeping only attached automations.

## [v13.3.1] - 2026-05-29

Patch release — two card-rendering fixes, two interaction improvements, a deprecated watchdog resource, and an internal test and API-limit cleanup.

- **Privacy/light toggle no longer interrupts streams on other cameras.** Toggling the privacy mode switch or the camera light on one camera triggered a coordinator-wide state broadcast that caused the Lovelace card to briefly show a reconnecting/HLS overlay on every other camera visible on the same dashboard. Each card now skips the re-render path when the changed entity is unrelated to its own camera, eliminating the cross-camera blip.
- **Loading spinner now centered in the HA mobile app.** The spinner was anchored to the bottom-right corner instead of the center of the card in older iOS WebViews (the `inset` CSS shorthand is unsupported there). Replaced with explicit `top`/`right`/`bottom`/`left` properties — centered on all platforms.
- **Tap-to-play and fullscreen overlay touch handling improved.** Tap reliability on mobile for both the tap-to-play overlay and the fullscreen toggle is improved; intermittent misses on touch-only devices are resolved.
- **`bosch-camera-autoplay-fix.js` watchdog is now a no-op (deprecated).** The separate autoplay-watchdog resource is no longer needed — the card self-heals on its own. Existing installations have the resource auto-removed on next HA restart; no manual action is required.
- **Internal:** intrusion-detection `distance` number entity maximum aligned to the API limit of 8 m (was uncapped). Test suite expanded with switch on/off mode coverage.

## [v13.3.0] - 2026-05-28

Minor release — five fixes in the same live-camera debugging session that produced the matching MCP v1.6.0 and Python CLI v10.10.0 releases. All five were reproduced against the user's prod hardware (Eyes Außenkamera II "Terrasse" + Eyes Innenkamera II "Innenbereich" + Gen1 Outdoor + Gen1 360° Indoor) and verified by HA-integration reload + live re-test.

- **`light.bosch_<cam>_frontlicht` would never turn on, every time.** The `_put_lighting_switch` PUT to `/v11/video_inputs/{id}/lighting/switch` succeeds with **HTTP 204 No Content** — but the code path tried to update `_lighting_switch_cache` from `resp.json()`, which raised on the empty body, was caught by a silent `except Exception: pass`, and left the cache untouched. The entity's `is_on` property reads through `_load_state_from_cache()` which then returned the stale `brightness=0` value, so `async_write_ha_state()` pushed `False` to HA even though the camera had already turned the spotlight on. HA's verify timeout fired with "could not verify state change" 100 % of the time. Now on JSON-parse failure of a 2xx response we write the sent body (already the merged post-write state, computed from cache + user updates) into the cache — the cache stays in sync with reality. The 200/JSON happy path is unchanged. Confirmed root cause via 45/45 round-5 test pass; pre-existing test `test_204_no_content_updates_cache_from_body` and two new regression guards added.

- **Four service handlers no longer fail silently when the camera is in privacy mode.** The Bosch cloud rejects every privacy-gated write with HTTP 443 `sh:camera.in.privacy.mode`; the integration's `async_put_camera` helper logs a warning and returns `False`, but four entity-write paths didn't check the helper's existing `_warn_if_privacy_on()` guard — so the PUT failed, the cache wasn't updated, and HA emitted "Service executed but state change could not be verified" with no path forward for the user. Added the guard to:
  - `BoschFrontLight.async_turn_on` (light.py — Eyes Außenkamera II front spotlight)
  - `_BoschRgbLedLight.async_turn_on` (light.py — top + bottom RGB LED rings)
  - `BoschPanicAlarmSwitch._set` (switch.py — Gen2 Indoor II 75 dB siren) — the live test confirmed the siren never actually fired the first time it was triggered against `privacy=ON` on 2026-05-28; after this fix + privacy off the siren works reliably
  - `_BoschAlarmDelayBase.async_set_native_value` (number.py — Gen2 Indoor only, the `Sirenen-Dauer` / `Pre-Alarm-Dauer` / `Alarm-Verzögerung` sliders)
  Each path now early-returns after posting the persistent-notification, instead of issuing a PUT that's doomed by the cloud.

- **Internal:** 3 new test modules (`tests/test_camera_coverage_gaps.py`, `tests/test_fcm_coverage_gaps.py`, `tests/test_privacy_guard_branches.py`) and updates to 3 existing test modules cover the new branches.

## [v13.2.5] - 2026-05-27

Patch release — second wave of fixes developed in the same live debugging session as v13.2.4. Six visible-state bugs in the Lovelace card and one race in the integration's `is_streaming` property, all surfaced via osascript-driven Chrome testing against the running install.

- **WebRTC race on first stream-start.** The integration's `BoschCamera.is_streaming` returned True the moment `try_live_connection` wrote its PUT-result into `coordinator._live_connections` — but BEFORE the 25-35 s pre-warm completed and `rtspsUrl` was actually populated on the entry. During that window the camera entity's `state` flipped to `"streaming"`, the card's `_waitForStreamReady` saw `camReady=true` and immediately fired `camera/webrtc/offer`, and HA's go2rtc provider rejected with `Camera has no stream source` (because `stream_source()` correctly gated on `rtspsUrl`). The card hit its 5 s no-track timeout and fell back to HLS. After a browser reload pre-warm was already complete and `rtspsUrl` was present from the first state read, so WebRTC worked first try — which made the bug look like "WebRTC only works after a reload." `is_streaming` now mirrors `stream_source`'s gate: True only when an entry exists AND has `rtspsUrl`/`rtspUrl`. WebRTC succeeds on first stream-start; the TLS-proxy log shows `User-Agent: go2rtc/1.9.14` (not `Lavf62.3.100`) confirming the WebRTC path is now the actual transport.
- **Loading overlay leaked through privacy on→off.** Multiple bypass paths kept the "Aktualisiere…" / "Bild wird geladen…" / "Stream wird gestartet…" overlays alive while the privacy shutter was closed — and again during the 12 s post-off re-snapshot grace window. Five layered fixes: (a) `_setLoadingOverlay` entry-guard returns early on `visible=true` when privacy is on or within the post-off suppress window; (b) `_update` privacy-on branch force-removes `.visible`/`.refreshing` directly on the DOM + clears `_awaitingFresh` + `_loadingOverlay` + `_loadingTimeout`; (c) `_restoreCachedImage` and `_onImageLoaded` cache-path now honour both privacy and the suppress window before doing direct `.classList.add("visible")`; (d) the `set hass` firstHass block no longer flags `_awaitingFresh` while privacy is on; (e) the privacy on→off transition pre-pass at the TOP of `_update` sets `_privacyOffSuppressUntil = now + 12 s` BEFORE any subsequent overlay-show in the same synchronous tick (the previous placement at the bottom of `_update` was hundreds of lines too late and never caught the same-tick stream-stopped + backend-waiting overlay shows at lines 1799/1820).
- **Stuck "Verbinde" badge on card mount with active stream.** When the card mounted while HA's stream was already streaming (tab navigation back, HA restart over an active session), `_startLiveVideo()` set `_startingLiveVideo=true` and the `<video>` element's `playing` event may have fired BEFORE the listener was wired — so `activateVideo()` never ran and the badge state-machine stuck on "connecting" forever while frames were flowing. Badge computation now self-heals: if HA reports streaming AND the `<video>` element is actually playing (`!paused && currentTime > 0 && readyState >= 2`), force-clear the stale flag, set `_liveVideoActive=true`, and dismiss the loading overlay directly (because the `playing`-event listener that would normally call `clearOverlay()` is no longer in scope).
- **Loading overlay stacked on top of OFFLINE state for 15 s of HTTP retries.** The stream-OFF transition fired `_setLoadingOverlay("Aktualisiere Bild…")` + scheduled a snapshot fetch unconditionally — even when the camera was offline and the fetch was guaranteed to fail. Result: double overlay (OFFLINE chrome + loading overlay) for the full HTTP retry sequence. Now gated on `!this._isOffline`.
- **Card 404 noise eliminated** (carry-over from v13.2.4 verification): `_pullFreshSwitchStates` filters by `id in this._hass.states` before issuing the API call — Gen2 LED rings live under `light.*` but the card's default fallback fabricated `switch.X_camera_light`, 404'ing every refresh.
- **Defensive `getattr` on `_hw_version`.** The Gen2-gate for RCP 0x099e (added in the local v13.2.3 hotfix that landed in the v13.2.4 commit) reads `self._hw_version.get(cam_id, "")` directly. Production coordinator always has this populated, but tests use `SimpleNamespace` stubs that don't auto-populate dicts — 14 snapshot tests broke in CI. Added `getattr(...,{})` fallback so the gate behaviour is unchanged in production and tests reach the gate's condition.

Internal: development used a new convention `+0.0.0.X` SemVer build-metadata suffix on `CARD_VERSION` during in-session iteration so Cmd+Shift+R could bypass aggressive browser cache of the Lovelace resource URL (HA serves `www/` with max-age=31 days). The suffix is stripped for release tags; see CLAUDE.md `INTERNAL_TEST_VERSION` rule.

## [v13.2.4] - 2026-05-27

Patch release — four bugs surfaced during a live debugging session against a real installation (HA 2026.5, Indoor Gen2 firmware 9.40.102, Lovelace card consuming HLS).

- **Live-stream stutter caused by the Lovelace card's auto-unmute loop.** `_update()` set `video.muted = false` on every Home Assistant state-change tick. Chrome treats every such assignment as a fresh unmute attempt and, without a user gesture in scope, pauses the video element + logs `"Unmuting failed and the element was paused instead because the user didn't interact with the document before"`. Each pause/resume produced a visible 1-2 s stutter; with ~11 hass-updates per coordinator cycle on a busy install, the card stuttered itself constantly. Fix: only mutate `video.muted` when the desired state differs from the current state (no idempotent re-assignment), AND only attempt the unmute after a global user-gesture flag has been set (`pointerdown`/`keydown`/`touchstart` once-listener registered at `connectedCallback`).
- **Stream-worker LOCAL rescue did not fire on HTTP 401.** `_handle_stream_worker_error` required `max_stream_errors` (5 for Indoor Gen2 default) accumulated worker errors before issuing the 401-rescue (fresh `PUT /connection` to swap rotated session creds). HA Core's stream component coalesces repeated identical worker errors, so a real 401 storm could plateau at 4 ticks in the listener and the rescue would never fire — frozen image until manual `Stream.stop()` + restart. Fix: 401 / `Unauthorized` / `authorization failed` substrings in the worker message bypass the threshold and trigger the rescue on the first occurrence. Non-auth errors keep the original 5-error gate so transient encoder hiccups still tolerate a few retries before fallback.
- **FCM `_try_fcm` failure diagnostics swallowed by the noise filter.** The inner warning `FCM registration failed: %s` carries the underlying error string, which on `PHONE_REGISTRATION_ERROR` (Google's GCM rate-limit response) contains the very substring `_FCMNoiseFilter._FAILURE_MARKERS` dedups. Result: the operator saw the outer "FCM registration failed — falling back to standard polling" but not the actual cause, so the heal-ladder log was opaque. Fix: mask the marker substrings before logging the diagnostic line so the noise filter doesn't suppress it (operational visibility preserved without re-opening the original 12 k-lines/min log-flood vulnerability).
- **Lovelace card 404 noise from non-existent switch entities.** `_pullFreshSwitchStates` issued REST `GET /api/states/<id>` for every entity in its watchlist regardless of presence. The default light entity fallback is `switch.{base}_camera_light`, but the integration exposes Gen2 LED rings under the `light.*` domain — so the GET 404'd on every refresh cycle, polluting the browser console (Innenbereich, Kamera observed live). Fix: filter the watchlist by `id in this._hass.states` before issuing the API call. Existing entities still get the fresh-state pull; missing ones become silent no-ops.

This release was developed against a live install via SSH/MCP — every fix was deployed to the running HA instance, reload-tested, and verified to remove the observed log signature (or, for the stutter, confirmed smooth by the user before commit). Full test suite passes (no regression touched).

## [v13.2.3] - 2026-05-26

Patch release — fixes two real production bugs surfaced during a thorough code scan, plus quality-of-life improvements in logging, options-flow correctness, and async hygiene.

- **`bosch_shc_camera_intrusion` webhook event now actually fires.** The event type was registered as a webhook target and exposed via `send_event_webhook` selector, but no code path ever fired it. Added rising-edge detection on `alarmStatus.alarmType`: a transition from `"NONE"` (or empty) to a real alarm type fires the event once with a payload `{camera_id, camera_name, alarm_type, intrusion_system, timestamp}`. Falling edges and repeats do not fire (avoids spam). Pinned with 12 regression tests covering every transition.
- **Stale go2rtc + Stream-object state on integration reload.** `_async_cancel_coordinator_tasks` only stopped TLS proxies but never per-cam tore down active streams. Result: go2rtc kept producer URLs pointing at dead proxy ports, and HA's `Stream` object on the camera entity held the dead URL — the browser polled a 404 m3u8 until the user hard-refreshed the card. Now iterates `_live_connections` and calls `_tear_down_live_stream(cam_id)` before `stop_all_proxies` (unregisters go2rtc + `stream.stop()` + `cam_entity.stream = None`). Pinned with 4 regression tests.
- **RCP-LAN HTTP 401 throttle.** CBS users lack permission for some RCP opcodes (`iconLedBrightness` etc.). The slow-tier polled them every ~5 min forever — 100+ unnecessary 401s/hour in real logs. Added a per-`(cam_id, opcode_hex)` denied cache (24 h TTL) that short-circuits the next call without a network request; cleared automatically on the next 200 so a permission change recovers within the next slow-tier cycle. Pinned with 8 regression tests.
- **Empty-message exception logs now show the type.** `_LOGGER.debug("X fetch error for %s: %s", cam, err)` produced `"X fetch error for AAAA: "` with no trailing message for `asyncio.TimeoutError()` and several `aiohttp` errors whose `str()` returns `""`. New `_err_str(err)` helper falls back to `repr(err)` when the str is empty. Applied to 6 fetch-error sites.
- **`use_mjpeg_snapshot` documentation rewritten.** All three translation files (`strings.json`, `en.json`, `de.json`) claimed "On by default — silently falls back …" while `DEFAULT_OPTIONS["use_mjpeg_snapshot"] = False`. Rewritten to "Off by default, experimental" and explains the FFmpeg-TLS-stack incompatibility with Bosch's RTSPS server (FFmpeg error 183).
- **`enable_intercom` option now actually controls visibility.** The option toggle in Settings had no effect — `BoschIntercomSwitch` was always registered (only hidden via `_attr_entity_registry_enabled_default = False`). Now gated on `opts.get("enable_intercom", False)` OR a legacy entity-registry entry (preserves existing installs that enabled the entity via the UI). The hide-by-default attribute is dropped so a fresh opt-in shows the entity immediately.
- **`threading.Event.wait(timeout=2)` removed from the asyncio event loop.** `tls_proxy.start_tls_proxy` allocated a `threading.Event` and waited on it on the asyncio thread purely to confirm the daemon thread had started. The port is already listening before the thread starts, so the wait was both blocking-on-async-loop and pointless. Removed.
- **`_SHC_MAX_FAILS` / `_SHC_RETRY_INTERVAL` migrated to `const.py`.** Were instance variables for what are truly immutable thresholds; now class-level constants mirrored from `const.py`. Test pins on `coord._SHC_MAX_FAILS` keep working.
- **Inline timeouts centralized.** Recorder (grace/stderr-drain/ffmpeg-init/stop-grace) and tls_proxy (TCP-connect, RTSP DESCRIBE-read) inline `timeout=N` literals moved to named constants in `const.py`. Same values — tunable in one place going forward.
- **Dead-code cleanup.** Removed 7 unused imports across 6 modules. `BoschAcousticAlarmButton` class deleted entirely — never instantiated since v12.0.4 (Gen1 cams have no integrated siren; Gen2 uses `BoschPanicAlarmSwitch`). 11 dead OAuth-abort translation keys + `pick_implementation` step removed from `strings.json`. `device_automation.trigger_type.*` section removed from `strings.json` + 11 translation files (no `device_automation.py` exists). Config-flow + select.py use the `CONF_ENABLE_WEBHOOK_DELIVERY` / `CONF_WEBHOOK_URL` / `CONF_ENABLE_PTZ_CONTROLS` constants instead of inline string literals.
- **Test suite.** Net new: 7 test files, +47 tests. Full suite **4514 passed / 17 skipped / 0 failed** in ~101 s.

## [v13.2.2] - 2026-05-25

- `trouble_connect` added to `last_event_type` enum options. Fixes `ValueError: provides state value trouble_connect, which is not in the list of options` log spam on Gen1 camera reconnect events.

## [v13.2.1] - 2026-05-25

- WebRTC fast-fail in card: Promise reject hoisted out of subscribeMessage scope so 5 s delay no longer applies when WebRTC negotiation rejects synchronously.
- Bosch session-quota 444 mapped to `SESSION_LIMIT` sensor state instead of `OFFLINE` in 3 call sites.

## [v13.2.0] - 2026-05-25

- audioAlarm cleanup: removed unactivatable Geräusch-Erkennung switch + threshold/sensitivity number entities + diagnostic sensor + coordinator helper.
- Bosch iOS app v2.11.2+ activates the microphone via a pinned LAN HTTPS call that cannot be replicated from HA, so the entity could never activate.
- Cross-version mirror of Python CLI v10.8.0.

## v13.1.2 — 2026-05-24

Patch release — fixes two issues surfaced by live HA logs: an FCM-push crash during integration setup, and a 5-minute startup-wait warning caused by the LOCAL session keepalive being registered as a tracked task instead of a background task.

- **FCM push during setup no longer crashes.** `async_handle_fcm_push()` accessed `coordinator.data.keys()` unconditionally. When an FCM push arrives in the narrow window between `async_setup_entry` and the first coordinator refresh, `coordinator.data` is still `None`, which raised `AttributeError: 'NoneType' object has no attribute 'keys'` and surfaced in the system log (count=4 in a single boot). Added the equivalent `or not coordinator.data` early-return guard already used elsewhere in the coordinator (`__init__.py:1576`). The caller's bookkeeping side effects (`_fcm_last_push`, `_fcm_healthy`, push logging) run before the schedule, so an early return is the correct behaviour when the coordinator data is not yet warm.
- **HA startup no longer waits 5 minutes for the LOCAL session keepalive.** `_replace_renewal_task()` scheduled `_auto_renew_local_session` — a `while True` keepalive coroutine that only exits on stream-off — through `hass.async_create_task()`. HA Core blocks the startup-wrap-up phase until every tracked task finishes; the keepalive never finished, so HA waited the full 5 min before logging "Something is blocking Home Assistant from wrapping up the start up phase" and continuing anyway. Switched to `hass.async_create_background_task(coro, "bosch_shc_camera_renewal_<short_id>")`, which is the documented HA API for permanent loops (exempt from startup-wait, still cancelled on shutdown). The same code path schedules `_remote_session_terminator`, so REMOTE sessions benefit too.
- **Regression tests.** New `tests/test_fcm_push_data_none.py` (3 tests — `coordinator.data is None`, `coordinator.data == {}`, `token == ""`) pins the FCM guard. `tests/test_init_async_methods.py::TestReplaceRenewalTask` updated to mock `async_create_background_task` and adds `test_replace_uses_background_task_api_not_tracked` which pins the task-API choice and the debuggable name prefix. Existing renewal-task tests (cancel-old / no-cancel-done / done-callback) stay green. Full suite stays green; 100 % line coverage on touched files preserved.

## v12.8.3 — 2026-05-21

Patch release — closes a recovery gap in the FCM push watchdog so a failed self-heal no longer leaves event detection stuck on polling until the next HA restart.

- **FCM watchdog now retries after a failed self-heal.** When the FCM listener silent-died and the follow-up `checkin_or_register()` failed (e.g. Google returns `PHONE_REGISTRATION_ERROR` while it rate-limits the public IP), `_fcm_running` stayed `False` and every existing self-heal trigger was gated by `_fcm_running=True`. FCM stayed dead until the user reloaded the integration manually. v12.8.3 adds a third self-heal trigger: `enable_fcm_push=True` + `_fcm_running=False` + cool-down expired → re-attempt. The same 30 min cool-down still applies, so a persistent Google rate-limit will not be hammered.
- **Regression coverage.** New `tests/test_fcm_watchdog_retry.py` (4 tests) pins the retry-after-failed-heal path, the cool-down suppression, the `enable_fcm_push=False` opt-out, and the healthy-listener no-op. Existing self-heal tests (`test_init_sprint_ka::TestFcmWatchdog`, `test_fcm_self_heal.py`) stay green.

## v12.8.2 — 2026-05-21

Patch release — card-only fixes for two UX papercuts on the auto-play gate. Ships Lovelace card v2.16.9. No Python code change beyond the `CARD_VERSION` bump.

- **Phantom CONNECTING badge fix.** The `backendWaiting` branch in `_update()` (warming_up / connecting from the `stream_status` sensor) used to trigger the loading overlay + start the HLS connect path even when the user had NOT requested video. The integration's snapshot-refresh path can open a live session backend-side — which then has HA's stream component prepare HLS, which flips `stream_status` to `connecting`. The card now requires `switch.<cam>_live_stream === "on"` (explicit user intent) before honouring `backendWaiting`. Result: opening the dashboard never shows a CONNECTING badge or "LAN-Stream — ca. 25-35 s bis erstes Bild" overlay against the user's will.
- **LAN-badge hidden by default.** The connection-type badge top-right of the card no longer displays "LAN" for the default-case stream. It only displays "Cloud" when the stream actually fell back to the Bosch cloud relay (the noteworthy case). LAN is the normal, configured-default — surfacing it on every card was pure noise. Card chrome cleanup matching the v12.8.1 "HLS-Modus banner hidden behind gate" idea.

## v12.8.1 — 2026-05-21

Feature release — opt-in tap-to-reveal gate for the live stream, with LAN/remote awareness. Ships alongside Lovelace card v2.16.7.

- **Card auto-play default (integration option).** New `auto_play_default` option in Settings → Features. Three modes: `lan` (default — auto-reveal on LAN, tap-to-reveal overlay on mobile/tunnel), `always` (auto-reveal in every session), `never` (always tap-to-reveal). Exposed as a `camera.*` entity attribute so the Lovelace card picks it up without restart. Per-card YAML override `auto_play: lan|always|never` shadows the integration default for one card; garbage and any legacy value (incl. the dropped `confirm` from earlier internal iterations) collapse to `lan`.
- **Overlay only when the stream is running.** Opening the card on a cold camera shows the regular snapshot — no gate, no auto-start. When the backend stream transitions OFF→ON (Stream switch, automation, second device), the gate appears in overlay-required modes; ON→OFF hides it. Tap reveals the live video. The decision runs synchronously on every `_update()` pass so the gate is up before the HLS path is even considered — zero HLS bytes flow to the phone until the user explicitly taps.
- **Bandwidth-gated.** While the gate is shown the card transmits nothing beyond a ~30 KB snapshot per minute (≈ 4 Kbps). Tapping starts the ~2 Mbps HLS pull. Verified in HA logs: no `HlsPlaylistView` / `HlsPartView` requests from the frontend while the gate is up.
- **LAN/remote detection (browser-side, all platforms).** Primary check compares `window.location.origin` against `hass.config.internal_url` — exact, port-aware. Fallback is an RFC-1918 / `.local` / `.fritz.box` hostname regex. Works inside HA Companion App iOS (WKWebView), HA Companion App Android (WebView), Mobile Safari, Chrome Android. Companion apps already auto-switch to `internal_url` when the device's Wi-Fi SSID matches the configured internal SSIDs, so the detection follows the same signal the app uses.
- **Card chrome cleanup.** The `HLS-Modus (kein WebRTC über Tunnel)` informational banner is suppressed while the auto-play gate is visible — the transport hint is irrelevant until the user actually starts playback.
- **Card bumped to v2.16.7.** No-op when `auto_play=always` and on LAN with `auto_play=lan`. No `backdrop-filter: blur` on the gate — the snapshot stays sharp so users can decide based on the current image.
- **Pin-tests for every mode.** New `tests/test_auto_play_default.py` (17 tests): 3 modes × options-flow round-trip + 3 modes × camera-attribute exposure + DEFAULT_OPTIONS membership + dropdown-options match + section-membership + garbage-collapse + empty-string-collapse + legacy `confirm` collapse. Full suite **4408 passed, 17 skipped, 0 failed**. 100% line coverage maintained on touched files.
- **Translations.** New `auto_play_default` field translated in all 11 languages (de, en, es, fr, it, nl, pl, pt, ru, uk, zh-Hans) plus `strings.json`. Description covers all three modes + the per-card YAML override.
- **Mobile compatibility research saved.** `knowledge-base/auto-play-lan-detect-mobile-compat.md` documents the iOS/Android Companion source paths confirming how the WebView URL is selected and what each LAN-detection approach buys. `knowledge-base/auto-play-lan-detect-best-signal.md` records why no better signal exists — Companion App's native `isOnInternalNetwork` decision is not forwarded to the WebView.

## v12.7.2 — 2026-05-20

Security pass — three findings from a pre-release static-analysis scan (Semgrep + Bandit + detect-secrets). No functional change for end users; mypy --strict stays green; coverage stays at 100% line.

- **`hashlib.md5(..., usedforsecurity=False)` in `auth_utils.py`.** MD5 is protocol-mandated by RFC 7616 (HTTP Digest auth) and not used for security here. The flag prevents `ValueError` on Python 3.9+ in FIPS mode and silences the Bandit B324 warning. Same pattern was already correct in `tls_proxy.py:359`.
- **`defusedxml.ElementTree` in `local_rcp.py` + `maintenance.py`.** Drop-in replacement for `xml.etree.ElementTree` that rejects XXE entity-expansion attacks. The camera is LAN-only and trusted, but defusedxml is the modern hygiene default for any XML parser that touches device-returned data. New runtime dep `defusedxml>=0.7.1`.
- **`send_event_webhook` service handler refactor.** The handler is now defined inside `_register_services` instead of `async_setup_entry`, and reads options from `hass.config_entries.async_loaded_entries(DOMAIN)` at call time instead of capturing a stale `entry` closure. The old closure pattern silently held the original `entry` reference across reloads. New regression test `test_service_handler_uses_current_entry_options_after_reload` proves the fix.
- 4410 tests passing (was 4409 / +1 regression test). 100% line coverage maintained.

## v12.7.1 — 2026-05-20

Hotfix: Hassfest CI rejected v12.7.0 because the new `webhook_url` field's data_description contained a literal example URL (`https://example.com/hooks/bosch`). HA Core's translation linter blocks URLs in description strings to keep them safe for end-user UIs. v12.7.1 strips the example URL from the description string across all 12 translation files (`strings.json` + 11 languages). No functional change.

## v12.7.0 — 2026-05-20

Feature release shipped alongside Python CLI v10.7.5, ioBroker v0.7.9, MCP v1.3.4 and a new Node-RED skeleton (alpha). Adds opt-in entity controls, webhook event delivery, HomeKit Bridge documentation, snapshot scheduling examples, and reaches 100% line coverage.

- **PTZ named presets (Gen1 360°).** New `select.bosch_<cam>_pan_preset` entity exposing five named pan positions: `home` (0°), `left` (-60°), `right` (+60°), `back_left` (-120°), `back_right` (+120°). Ceiling-mount sign-inversion handled automatically. Cross-module port — also available as `pan --preset` flag in the Python CLI, `pan_preset` DP in the ioBroker adapter, and `bosch_camera_pan preset=` argument in the MCP server.
- **Opt-in PTZ controls.** New options-flow toggle `enable_ptz_controls` (default off) — the pan-preset select entity is only created when the toggle is on. Users without a 360° camera see no stray entity.
- **Webhook event delivery.** New service `bosch_shc_camera.send_event_webhook` plus opt-in options-flow toggle (`enable_webhook_delivery` + `webhook_url`). When enabled, motion / audio / person / intrusion events POST a JSON payload to the configured URL. Default off. POST failures are logged but not propagated. Cross-module — Python CLI gets `watch --webhook URL` flag, ioBroker uses the MQTT bridge instead.
- **Apple HomeKit / Apple Home documentation.** New `## Apple HomeKit Integration` section in the README plus full `docs/homekit-bridge.md` guide. Camera entities and privacy switches are exposed via HA Core's built-in HomeKit Bridge — no additional code required, just configuration.
- **Snapshot scheduler / time-lapse examples.** New `examples/automations/snapshot-time-lapse.yaml` with four variants: hourly daytime, motion-triggered with throttle, daily midnight, weekly summary. Filenames templated with `{{ now().strftime('%Y%m%d_%H%M') }}`. Companion `ffmpeg` one-liner included for assembly.
- **100% line coverage milestone.** +34 new tests close every previously-uncovered line in the production codebase. Test count: **4409 passed, 0 failures** (was 4374 / 1 failure). Coverage: 100% line / 11,776 statements / 0 missed. 1 stale assertion fix (single_instance_allowed since `single_config_entry: true` is set on the manifest).
- **Cross-module Comparison Table sync.** All four sister-project READMEs (HA + Python CLI + ioBroker + MCP) now share a byte-identical 37-row Integration Comparison Table — verified by MD5 hash. New rows: Named pan presets, Webhook delivery, MQTT event bridge, Apple HomeKit, Snapshot scheduler.
- **Translation completeness.** Audit + fill across all 11 languages (`de`, `en`, `es`, `fr`, `it`, `nl`, `pl`, `pt`, `ru`, `uk`, `zh-Hans`). Zero gaps remaining — added `services.send_event_webhook`, `webhook` + `ptz` option sections, plus a handful of previously-missed entity labels.
- **Architecture diagrams.** Five new Mermaid diagrams in `docs/architecture-diagrams/`: PTZ preset path, MQTT event flow, cred-rotation retry, emergency LiveSession, stream-lifecycle-with-privacy. Source `.mmd` + rendered `.svg` for each.

## v12.6.0 — 2026-05-20

Feature release porting four Bosch-app-parity gaps surfaced by a competitive audit against Reolink/Eufy/Ring integrations. All four ship the same day as cross-platform releases (Python CLI v10.7.4, ioBroker v0.7.7, MCP v1.3.3).

- **Mic/Speaker level entities (Gen2).** Two new `number` entities per Gen2 camera: `number.bosch_<cam>_microphone_level` and `number.bosch_<cam>_speaker_level`, range 0-100, slider mode, mapped to `PUT /v11/video_inputs/{id}/audio` body fields `microphoneLevel` / `speakerLevel`. Read-back from the existing audio cache. Indoor II Intercom benefits especially — was previously only adjustable via the Bosch app.
- **Intrusion-detection sensitivity + distance entities (Gen2).** `number.bosch_<cam>_intrusion_sensitivity` (range 0-7, confirmed from FW 9.40 captures — was 0-5 in older firmware) + `number.bosch_<cam>_intrusion_distance` (range 1-10 m, inferred from iOS app slider). Both PUT `/intrusionDetectionConfig` with a read-modify-write to preserve the existing `mode` field. Write-lock + cache invalidation match the existing privacy/light pattern.
- **Configurable motion-sensor active window.** New options-flow field `motion_active_window` (range 10-300 s, default 90, NumberSelector slider). `binary_sensor.bosch_<cam>_motion` / `_audio_alarm` / `_person_detected` all read it via the new `_motion_active_window` property on `_BoschBinarySensorBase`, clamped at the boundaries with a fallback to 90 on invalid values. Replaces the hardcoded 90 s; surfaced from a long-standing HA-Forum request thread.
- **WiFi RSSI + firmware version diagnostic sensors.** Two new entities per camera: `sensor.bosch_<cam>_wifi_signal` (signal_strength device-class, dBm-derived %, diagnostic category) and `sensor.bosch_<cam>_firmware_version` (string, diagnostic category, `mdi:chip` icon). Fed from the existing `/v11/video_inputs/{id}/wifiinfo` slow-tier endpoint (already in the fetch loop) and `cam_info.firmwareVersion`. Matches Reolink/Ring/Eufy standard.
- **+141 regression tests** across `tests/test_number_audio_intrusion.py` (74), `tests/test_motion_window_option.py` (30), `tests/test_diagnostic_sensors.py` (37). Full suite **4314 passed, 17 skipped, 0 failed**. Mypy strict green on all touched files.

## v12.5.1 — 2026-05-20

Hotfix: revert the v12.5.0 Indoor II light entity. The Eyes Indoor II has no controllable light hardware (only fixed IR night-vision LEDs managed by the camera firmware itself). v12.5.0 mistakenly created a `BoschFrontLight` for it based on the presence of stale `number.*_helligkeit_*` / `*_farbtemperatur_*` entities that had been left in the registry from an older codepath. Those numbers had always been `unavailable` and were not a signal that the hardware existed. Confirmed by the user (cam owner).

- `light.py`: removed the Indoor II + `featureSupport.light=false` → `BoschFrontLight` branch. Light entities are now ONLY created for Outdoor II (the only Gen2 model with a controllable visible light surface).
- New `v12.5.1 migration` in `async_setup_entry`: removes the `light.bosch_*_frontlicht` orphan plus three stale Indoor II number orphans (`*_helligkeit_oberes_licht`, `*_helligkeit_unteres_licht`, `*_white_balance`) from the entity registry. Per-cam scoped — only entities matching a cam_id in `_hw_version` with `HOME_Eyes_Indoor` / `CAMERA_INDOOR_GEN2` are removed.
- Tests in `tests/test_light_round6.py` updated: Indoor II now asserts zero light entities; hw_version-fallback test inverted to Outdoor II (which is the case that actually needs the fallback during cloud-degraded cold start).

## v12.5.0 — 2026-05-20

LAN-fallback hardening for cloud outages, duplicate-notification dedup persistence, Indoor II light entity, and a new `bosch-notifications-card` for the Lovelace dashboard. Surfaced during the Bosch maintenance window 2026-05-20 (cloud returned HTTP 503 for 30+ minutes) — multiple silent failures came out at once. Surfaced during the Bosch maintenance window 2026-05-20 (cloud returned HTTP 503 for 30+ minutes): privacy + light switches on Innenbereich II / Terrasse showed `unavailable` for the whole outage even though both cameras were LAN-reachable, and the LAN-RCP fallback path itself was non-functional regardless.

- **HTTPS + Digest for LAN RCP writes.** `rcp_local_write` previously opened plain HTTP on port 80; Bosch SHC cameras only listen on HTTPS port 443 and require Digest auth on `rcp.xml`. Every LAN-fallback write silently failed with connection-refused. Now uses `https://` and threads the cycling Digest `cbs-XXXXXXXX` user + password through from `_local_creds_cache` via the existing `async_digest_request` helper. Anonymous-path fallback kept for back-compat (will fail with HTTP 401 on modern Gen2 firmware, surfaced as False).
- **Switch / light availability relaxed for unknown hardware.** `BoschPrivacyModeSwitch.available` and the Gen2 light `available` previously required `_is_gen2()` to return True. After a cold restart during a cloud outage, `_hw_version` is empty and the function defaults to "Gen1" — so the LAN-fallback gate denied the entity even on Gen2 cams. Now: confirmed Gen1 → deny (no LAN endpoint), confirmed Gen2 OR unknown → allow. Gen1 toggles still fail cleanly at the write layer.
- **`_hw_version` persistence + device-registry rehydrate.** New `Store(key="bosch_shc_camera_hw_versions")` snapshots every cam's hardware version on each successful coordinator refresh. On boot the cache is rehydrated from disk before the first refresh runs, so cold-degraded starts know which cams are Gen2 without waiting for the cloud. Belt-and-suspenders: if the store is empty (first start after upgrade), reverse-map `device.model` from the device registry back to canonical hardwareVersion strings.
- **LOCAL Digest creds persistence.** New `Store(key="bosch_shc_camera_local_creds")` snapshots `_local_creds_cache` entries (user, password, host, port) on every successful refresh. After cloud recovers + one fresh PUT /connection LOCAL, the cycling Digest creds are written to disk; next cold-restart-during-outage can use them immediately. Treated as same security level as the cloud bearer token — LAN-effective scope only.
- **`async_cloud_set_privacy_mode` and `async_cloud_set_light_component` LAN-fallback gate.** Both relaxed to fire for confirmed-Gen2 OR unknown-hw. Previously the cold-start-during-outage path produced HTTP 503 from the cloud + no LAN attempt at all.
- 6 new regression tests in `tests/test_lan_fallback_during_outage.py` covering: HTTPS not HTTP transport, anonymous-path fallback, switch availability for hw-unknown + Gen2, Gen1-known still denied, and source-grep guards on both shc.py fallback gates. Full suite 4170 passed.

**Notification dedup persistence.** The maintenance-announce (v12.4.8) and cloud-state-alert (v12.4.11) dedup flags lived only in `coordinator.__init__` memory. Every HA restart wiped them, and the next coordinator tick re-fired the same "Wartung läuft" / "Cloud nicht erreichbar" alert. Thomas reported receiving the same `Umfangreiche Wartung: Kameras` notification ~20 times during the 2026-05-20 outage because the integration was being restarted repeatedly while the maintenance window was active. Fix: two new `Store`s (`bosch_shc_camera_maint_notified`, `bosch_shc_camera_cloud_alert_state`) snapshot the dedup keys whenever they change and rehydrate them on boot. Restarts mid-window now stay silent.

**Indoor II front-spotlight as a light entity.** The cloud API reports `featureSupport.light=false` for Gen2 Indoor II (only Outdoor II returns true), so previous releases never created any `light.bosch_<indoor>_*` entity for it — even though the camera has a working front spotlight that uses the same `/v11/video_inputs/{id}/lighting/switch` endpoint as Outdoor II. v12.5.0 explicitly handles Indoor II + `light=false` by creating a single `BoschFrontLight` entity (color-temperature white, no RGB). Outdoor II keeps the full Top/Bottom RGB + Front trio. The light setup also falls back to the new persistent `_hw_version` store when `cam_info.hardwareVersion` is empty (cold-start during cloud outage), so the entity materialises even when the cloud is unreachable on boot.

**New `bosch-notifications-card` (card bundle v2.14.0).** Custom Lovelace card aggregating Bosch-cloud-side events into one dashboard pane: active / scheduled / recently-ended cloud maintenance windows, per-camera online status, and a fallback "alles ruhig" line when nothing is pending. Auto-discovers the maintenance sensor and every cam status sensor in the integration; all config options optional. Defense-in-depth: `href` assignments now validate the `https://` scheme of every URL pulled from sensor attributes so a compromised RSS feed (or any state-write path) cannot inject `javascript:` URIs. Also ships `examples/lovelace-bosch-notifications-card.yaml` — a pure-YAML conditional + entities + markdown variant for users who don't want to load the card bundle.

> Note: the very outage that surfaced this could not be fully recovered in-flight because no Digest creds were cached when the cloud went 503 (HA had been restarted in the middle of the outage). The fix lands for *future* outages — first cloud recovery + one stream-open persists the creds, then any subsequent outage works LAN-only.

## v12.4.12 — 2026-05-20

Live-stream switch semantics + watchdog scope corrections. Surfaced after Thomas reported the Innenbereich camera waking up streaming overnight without any user / automation / card request, and the Terrasse live-stream switch staying ON after a privacy toggle.

- **User-intent decoupling for the live-stream switch.** `BoschLiveStreamSwitch.is_on` previously read `_live_connections[cam_id]` directly. HA Core auto-opens streams via `async_create_stream` for Lovelace card preload, Cast / `camera.play_stream`, and snapshot fetches — each populated `_live_connections` and flipped the visible switch to ON even though the user never toggled it. New `coordinator._user_intent_streams: set[str]` tracks explicit user intent. Switch state derives from the set; `_live_connections` continues to drive `stream_source()` and internal routing. Auto-opens behave exactly as before from a streaming perspective, but the UI switch no longer flips.
- **WebRTC-watchdog refresh scope.** `_ensure_go2rtc_schemes_fresh` and the post-reload arm of `_check_and_recover_webrtc` previously iterated every `_camera_entities.values()` and called `cam_ent.async_refresh_providers()` on each. HA Core's `async_refresh_providers` resolves the WebRTC provider by awaiting `stream_source()`, which in our integration opens a fresh LOCAL session via `try_live_connection()`. Net effect: starting one stream silently opened a stream on every other idle camera too. Both loops now skip cams not in `_live_connections`.
- **Health-watchdog race closed.** `_stream_health_watchdog` ran as a background task scheduled by `async_turn_on`. Between scheduling and the 60 s sleep ending, the user could toggle the switch OFF; the watchdog would still wake up, tear the (already-torn-down) session down again, and call `try_live_connection()` — re-opening a stream the user did not want. New check between `_stop_tls_proxy` and the reconnect attempt: bail if `cam_id` is no longer in `_user_intent_streams`.
- **`_tear_down_live_stream` correctness.** Pops `_live_connections` (and the other per-cam dicts) BEFORE calling `stop_recorder()`; wraps the NVR-stop in try/except so a Mini-NVR BETA error cannot leave the visible switch stuck on ON. Pushes the cleared state to HA immediately via the new `_live_stream_entities` registry instead of waiting for the next coordinator refresh tick. Also discards the cam_id from `_user_intent_streams` so privacy-on / external teardowns end user intent too.
- 13 new regression tests covering the four contracts: switch reads intent (not raw session state), watchdog skips reconnect after user OFF, teardown clears intent, failed turn_on reverts intent, `_ensure_go2rtc_schemes_fresh` + recovery loop scope to streaming cams. Full suite 4164 passed.

## v12.4.11 — 2026-05-19

- **Cloud up/down transition alerts**: the coordinator now fires a user notification when the Bosch cloud transitions between healthy and unreachable. Outage path: requires ≥60 s of continuous failure before announcing, so a single transient blip never spams. Recovery path: fires immediately on the first successful tick after an announced outage. Suppressed while an RSS-announced maintenance window is `active` so users don't get duplicate alerts about a planned event. Routes via `alert_notify_system` → fallback `alert_notify_service`, same delivery path as TROUBLE_DISCONNECT and the maintenance lifecycle notifier. 11 new pin tests in `tests/test_cloud_state_alert.py` covering first-observation silence, sub-threshold blip suppression, threshold-cross fires-once, recovery, maintenance-window suppression, multi-service routing, and notify-failure containment.

## v12.4.10 — 2026-05-19

- **Cloud-degraded startup**: the integration now bootstraps with LAN-only entities when the Bosch cloud returns 5xx on HA start. Before, a 5xx on the very first coordinator refresh raised `ConfigEntryNotReady` and the integration sat in a retry loop with no usable entities — even though privacy / light / LAN-ping all work without the cloud. Now: catch `ConfigEntryNotReady`, rehydrate `coordinator.data` from the entity registry (using previously known cam_ids), look up the human-readable title from the device registry (with a one-time repair pass that fixes "Bosch <UUID>" placeholder names from earlier broken startups), and kick an immediate outage-ping sweep. The next successful coordinator refresh seamlessly takes over.
- **Persistent LAN-IP store**: a tiny JSON store at `.storage/bosch_shc_camera_lan_ips` records cam_id → LAN-IP on every successful coordinator refresh. Loaded at setup so the LAN-ping path has a working address book even on a cold cloud-degraded start. Throttled write — only re-saves when the mapping actually changes.
- **Local-fallback tiles under the cloud-outage banner**: when one or more Bosch cameras are unavailable (typical during a Bosch cloud 5xx outage), the overview-card now renders a per-camera tile row showing LAN reachability (green / red / grey dot) plus clickable Privacy and Light buttons that route to the LAN fallback. Tiles match cameras via entity_id slug first, friendly-name prefix second. Tiles disappear automatically once the cloud is back and normal cards render. Card v2.13.0.
- **Privacy & light switch LAN-fallback availability**: the privacy and front-light entities now stay `available` when the cloud is unhealthy as long as the camera is Gen2 and pingable on the LAN — `async_cloud_set_privacy_mode` / `async_cloud_set_light_component` already fall through to `rcp_local_write_privacy` / `rcp_local_write_front_light` (new). Same routing as the existing Gen2-LOCAL-RCP-after-cloud-fail chain, just no longer blocked by a grey switch in the UI.
- **Front-light Gen2 LOCAL RCP fallback**: new `rcp.rcp_local_write_front_light` writes RCP `0x0c22` (LED dimmer 0-100) directly to the camera. Wired into `async_cloud_set_light_component` for `component in {front, intensity}` — wallwasher RGB is still cloud-only (write payload too complex for the unauthenticated RCP path). `rcp_local_write` gained an optional `num` kwarg for T_WORD-typed writes.
- **Post-write grace period (~30 s)**: after a successful local RCP write the camera tears down its HTTPS endpoint briefly while Digest creds rotate. `_local_write_at` is stamped on every successful local write; `coordinator.is_lan_reachable(cam_id)` honors that window and reports the cam as still reachable so the UI does not flip to "LAN offline" for a few seconds after every privacy/light toggle.
- **LAN reachability binary sensor**: new `binary_sensor.bosch_<cam>_lan_reachable` (device_class CONNECTIVITY, always `available`) surfaces the ping cache so automations and the overview-card LAN tiles have a stable signal that survives cloud outages. Attributes carry `last_check_seconds_ago` and `write_grace_seconds_left` for diagnostics.
- **Coordinator outage-ping sweep**: when `_async_update_data` hits HTTP 5xx or a timeout, a background `asyncio.gather` pings every known camera on port 443 (throttled to once per 30 s). The ping result feeds `_lan_tcp_reachable`, which the switch / light / binary_sensor entities consult.

## v12.4.9 — 2026-05-19

- **Card empty-state regression fix**: when all Bosch cameras transitioned from rendered to `unavailable` (typical during a Bosch cloud 5xx outage), the overview-card grid was correctly pruned of stale tiles but the empty-state banner was *not* re-appended — the user saw a blank panel for the rest of the session. Root cause: `_update()` only re-rendered when the discovery signature changed; two consecutive ticks with `cams.length === 0` produced the same signature `""`, so the empty-state path never ran. Fix in `src/bosch-camera-card.js:_update()`: also re-render when the grid is currently empty (`needsReorder = sig !== this._lastSig || gridEmpty`). Card v2.12.13.

## v12.4.8 — 2026-05-19

- **Maintenance lifecycle notifications** (scheduled → active → past): when the RSS-derived `BoschCloudMaintenanceSensor` enters `scheduled`, `active`, or `past` state for a window, the coordinator fires a notification via the existing alert pipeline (`alert_notify_system` falls back to `alert_notify_service`). Three notifications per window: announcement when first seen as scheduled, "läuft" when the window opens, "beendet" when it closes. Deduped by `(RSS link, state)` so a coordinator tick during the same phase stays silent. The `past` notification only fires if we previously announced `active` for the same link — prevents spam from stale historical windows discovered after restart. Recent / unknown / idle states stay silent. Notification body carries title + Europe/Berlin time window + community link.
- **Per-camera offline / online transition notifications**: each camera now gets a notification when its computed availability flips between `online` and `offline`. The first observation after a HA restart is silent (baseline recording) so reboots inside an outage do not re-announce existing state. Transitions involving `unknown` are also silent — coordinator transient cloud-flap, not a real availability change. Uses the same `_compute_status_for` helper as `BoschCameraStatusSensor` so the announce path and the sensor never drift apart.
- **Tests**: 14 new pin tests in `tests/test_maintenance_announce.py` covering scheduled / active / past transitions, stale-past suppression, full-lifecycle, dedupe, multi-service routing, and notify-failure containment. 15 new pin tests in `tests/test_camera_status_announce.py` covering every offline ↔ online transition path plus the `unknown` flap suppression and per-camera state isolation. Full suite: passing.

## v12.4.7 — 2026-05-19

- **Cloud-maintenance banner**: when Bosch announces a maintenance window in their community forum, the dashboard card now shows a clear banner with title + time window + link. Detects via RSS feed (`community.bosch-smarthome.com` Wartungsarbeiten + Statusmeldungen) with HTML fallback. Sensor `sensor.<cam>_bosch_cloud_wartung` exposes the state (`active` / `scheduled` / `past` / `recent` / `unknown` / `idle`) for automations. Coordinator fetches once per hour; reactive re-fetch on 5xx responses with a 5 min cooldown. Card shows the banner only when `state in {active, scheduled}` and `camera_relevant=true`. Card v2.12.12. 35 new regression tests in `tests/test_maintenance.py`.
- **Status sensor offline fix**: the camera status sensor now correctly flips to `offline` when the Bosch cloud reports `ONLINE` but the latest event carries `TROUBLE_DISCONNECT` — fixes outdoor Gen1 cameras showing as online for up to 22 days after a physical disconnect. 5 new pin tests in `tests/test_sensors.py` (one per evaluation path).
- **Stream warm-up timeout reduced 300 s → 180 s**: pre-warm worst case is ~150 s (CAMERA_EYES Outdoor 8 retries × 13 s + 35 s min_total_wait + buffer). 180 s leaves a safety margin without holding the privacy toggle hostage on a stuck warm-up. Lowered after a 2026-05-19 incident where the Indoor II TLS proxy reset three times during pre-warm and the privacy switch stayed blocked for ~45 s. The TLS-proxy circuit breaker (`_on_tls_proxy_died`) also clears the warm-up flag now — when the camera is demonstrably unreachable, any pre-warm in flight cannot succeed.
- **LOCAL pre-warm failure without REMOTE fallback**: cleared warm-up flag immediately rather than holding it through the `min_total_wait` sleep. Users in LOCAL-only mode no longer get locked out of the privacy toggle for ~25 s (Indoor) / ~100 s (Outdoor) on a definitively failed warm-up.

## v12.4.6 — 2026-05-18

- **Fix silent FCM push delivery regression after v12.4.5 upgrade.** Users upgrading from v12.4.4 had their previous FCM credentials and Bosch CBS device-token registration carried over unchanged, but v12.4.5's code path posts `deviceType=ANDROID` to Bosch CBS. The `register_fcm_with_bosch` skip-guard fired on "token unchanged" and never re-registered, leaving Bosch CBS with the stale `deviceType=IOS` from before the migration. Bosch routed pushes via the wrong Firebase sub-app and they were silently dropped. Observed delivery latency on a production install: 1–5 min via polling fallback instead of <2 s via FCM push. Two layered fixes ship together:
  - **`__init__.py:async_migrate_entry` (v2 → v3)** now also pops `data.fcm_credentials` and `data.fcm_registered_token` when the legacy `fcm_push_mode` is in `(ios, android, auto)`. This forces a single clean re-registration with `deviceType=ANDROID` on first start after upgrade. Polling-mode users (`fcm_push_mode = polling`) are not affected — no FCM state to clear. Already-migrated entries (already at `version = 3`) are not re-migrated.
  - **`fcm.py:register_fcm_with_bosch`** now tracks the last-registered `deviceType` via a new `fcm_registered_device_type` field in `entry.data`. The skip-guard requires BOTH `fcm_registered_token == current_token` AND `fcm_registered_device_type == "ANDROID"`. Users who already migrated to v3 without the migration fix above detect the missing marker on next start, fire one corrective POST `/v11/devices` with `deviceType=ANDROID`, and write the marker — subsequent starts skip cleanly. The drift case logs `FCM CBS heal: registered as deviceType=ANDROID (was IOS or unknown)` at INFO level.
- **Regression coverage**: 6 new pin tests in `tests/test_fcm_mode_pin.py` cover the migration clearance contract (ios/android/auto → creds + token cleared; polling → preserved; v1→v2→v3 chain; already-v3 no-op). 7 new pin tests in `tests/test_fcm_drift_heal.py` cover the runtime drift heal (fresh install, already-healed fast-path, drift-with-None-marker, drift-with-IOS-marker, token rotation, HTTP 500 success path, HTTP 401 failure path). Full suite: **3917 passed**, 10 skipped, 0 failures.
- **Test-fixture PII cleanup**: ~36 test files previously contained operator-specific MAC addresses, LAN IPs, and `notify.*` entity ids from the development environment. All anonymised to RFC 5737 placeholder IPs, locally-administered placeholder MACs, and generic notify ids (`notify.test_user`, `notify.mobile_app_test_phone`). No code paths affected — pure string replacement across mock fixtures.
- **Card v2.12.9**: badge label changed from `fcm` to `push` (alongside existing `poll`) for non-developer clarity. Sensor enum values (`fcm_push`, `polling`, `disabled`) unchanged — automations referencing the sensor state stay valid.

Live-verified on a production entry: push latency after the drift heal dropped from 3:43 min (polling fallback) to 0.9 seconds (FCM push).

## v12.4.5 — 2026-05-18

- **FCM Push Mode simplified from four options to two.** The push-mode selector now offers only `auto` (FCM push with automatic fallback to polling on registration failure) and `polling` (skip FCM entirely). The previous `android` and `ios` options have been removed — the OSS-sanctioned Firebase configuration handles both platforms via the same registration path, so the per-platform distinction never offered the user a real choice and produced misleading error logs ("both iOS and Android failed") whenever Google's GCM check-in throttled the request. The removed iOS-specific Firebase app id and API key — never part of the OSS partnership grant — are gone from the source tree.
- **`fcm.py`**: dropped `FCM_IOS_APP_ID` constant and the iOS-only API key blob; `_build_fcm_cfg(mode)` collapsed to `_build_fcm_cfg()` (single Firebase config source); `_try_fcm_with_mode(mode)` renamed to `_try_fcm()` and the auto-fallback chain trimmed from "iOS → Android → polling" to "FCM once → polling on failure"; `register_fcm_with_bosch(coordinator)` lost its `mode` parameter and always posts `deviceType="ANDROID"` (the OSS Bosch Firebase app is registered under the Android app id). All-time effect: ~50 fewer lines, one log instead of two on registration failure, no more `FCM auto mode: iOS failed, trying Android fallback` info-spam on every restart.
- **`select.py` + 12 translation files**: dropdown options pinned to `["auto", "polling"]` via `FCM_PUSH_MODE_OPTIONS`; state label for `auto` reads "FCM Push" (localised). Description rewritten as a two-line key-value summary ("Auto = FCM push with automatic fallback to polling on registration failure. Polling = skip FCM and use standard API calls only.") across `strings.json` + `translations/{de,en,nl,ru,pt,uk,pl,fr,zh-Hans,it,es}.json`.
- **`config_flow.py`**: hardcoded `vol.In(["auto", "android", "ios", "polling"])` updated to `vol.In(["auto", "polling"])`; default-value coercion added so a legacy stored value never tries to pre-select a now-removed option. `ConfigFlow.VERSION` bumped 2 → 3.
- **Migration v2 → v3 (`__init__.py:async_migrate_entry`)**: entries whose stored `fcm_push_mode` is the legacy `"ios"` or `"android"` get rewritten to `"auto"` on first load after upgrade, with an info log per entry. Values already at `"auto"` or `"polling"` are kept verbatim; the schema version bumps to 3 unconditionally so HA stops re-running the migration step. Live-tested against a production entry holding `"ios"` — single restart, log line `Migration v2→v3: rewrote legacy fcm_push_mode to 'auto' for entry <id>`, select entity reports `auto`, dropdown reports the two-option set.
- **Regression coverage**: 9 new pin tests in `tests/test_fcm_mode_pin.py` cover the new 2-option contract (auto path calls FCM register; polling path never does; default is `"auto"`; garbage values coerce to FCM path) and the migration (rewrites `ios → auto`, `android → auto`; keeps `auto`/`polling` unchanged; v3 entries are a no-op). 5 obsolete tests removed across `test_bug_regression_v11.py`, `test_select_extra.py`, `test_fcm_round5.py`, `test_coverage_round_n.py`. Full suite: 3904 passed, 0 failures.

## v12.4.4 — 2026-05-18

- **TLS proxy auto-rebuild after circuit-breaker close.** The LOCAL TLS proxy (`tls_proxy.py`) closes its server socket after 5 consecutive upstream connect failures within 30 s — a defensive circuit breaker that fires on transient WiFi jitter, brief camera firmware glitches, or a Bosch-side TCP-reset burst. Before this release the close was final: the coordinator never received any signal, the camera entity stayed stuck on `streaming` with a stream URL pointing at the now-dead local port, and HA's stream worker / go2rtc got `Connection refused` indefinitely. Recovery required manually toggling the `switch.<cam>_live_stream` off→on. Observed 2026-05-18 around 05:59 UTC on an Indoor II Gen2 unit where the camera delivered five TLS `Connection reset by peer` events in roughly three seconds; the proxy closed cleanly, but stream errors then accumulated for over a minute until a manual toggle restored the session.
- New `on_proxy_died: Callable[[], None] | None = None` kwarg on `start_tls_proxy()` — when the circuit breaker closes the server socket, the callback is invoked from the proxy daemon thread. The kwarg defaults to `None` so all existing callers stay backward-compatible; exceptions raised inside the callback are swallowed so a broken handler can never crash the proxy thread.
- Coordinator wires a thread-safe handler via `hass.loop.call_soon_threadsafe` that schedules `_on_tls_proxy_died(cam_id)`. The handler waits 5 s (giving the camera a moment to recover), re-checks that the stream is still active and still on LOCAL, then clears stale `_live_connections` state, calls `_stop_tls_proxy(cam_id)` and runs `try_live_connection(cam_id)` — the same code path the manual switch toggle exercises. A 30 s backoff (`_tls_proxy_rebuild_last`, initialised with `float('-inf')` per the SENTINEL_RULE) prevents a rebuild storm if the new proxy also dies immediately because the camera is still flapping; the next renewal cycle will pick up where the backoff leaves off.
- Skip conditions are explicit: if the stream was turned off during the 5 s wait (`cam_id` no longer in `_live_connections`) the handler exits without action; if the active connection has already moved to REMOTE (e.g. `_handle_stream_worker_error` escalated first) the handler defers — REMOTE has no TLS proxy to rebuild, and another recovery flow owns the camera.
- **Regression coverage**: 12 new tests across `tests/test_tls_proxy_died_callback.py` (4 tests pinning the callback contract: fires after circuit breaker, exceptions swallowed, kwarg default `None` for backward-compat, signature stable) and `tests/test_coordinator_tls_proxy_rebuild.py` (8 tests covering the happy-path rebuild, both skip branches, the 30 s backoff window in both directions, the `float('-inf')` sentinel, and source-pin assertions that `_start_tls_proxy` actually wires the callback).

## v12.4.3 — 2026-05-17

- **Indoor II live stream stability**: disabled the destructive 30 s `PUT /connection` heartbeat for `HOME_Eyes_Indoor` (FW 9.40.25). The indoor variant now mirrors the outdoor profile (`heartbeat_interval=3600`, `renewal_interval=3600`). Symptom before the fix: every ~30 s the camera rotated the ephemeral Digest credentials, FFmpeg lost the live RTSP session and reconnected, the picture flickered with green YUV-garbage blocks until the next keyframe arrived. FFmpeg's native `GET_PARAMETER` every ~15 s already keeps the RTSP session alive without rotating credentials, so disabling the destructive heartbeat is safe.
- **FCM push self-heal on silent death**: when the `firebase-messaging` listener silently terminates (`FcmPushClient.is_started()` returns `False`) the coordinator watchdog now triggers `async_self_heal_fcm_push` instead of just flagging the listener unhealthy. Previously the watchdog set `_fcm_healthy=False` but never recovered — the legacy error-storm self-heal branch required `_fcm_healthy=True` so it never fired after the flag flipped, leaving the integration stuck in `fcm_running=True / fcm_healthy=False` and the Bosch FCM Push Status sensor stuck on `polling` until the user restarted Home Assistant. Both watchdog triggers now share the same 30-min cool-down so a transient WAN blip cannot keep tearing FCM down.

## v12.4.2 — 2026-05-17

**Race fix during LOCAL pre-warm + LOCAL-first default for new installs.** The 12.x series ships an essentially complete LOCAL stack — privacy, light, RGB, panic, motion/audio events with FCM, Mini-NVR, SMB/NAS upload, dual-stream URL — so the integration now defaults new installs to pure-LAN streaming. The cloud is consulted only for OAuth login and FCM subscription; the rest stays on the local network unless the user opts back into `auto` (LOCAL with cloud fallback) or `remote`. Existing installs keep their previous behaviour via a soft migration.

- **`camera.py — async_create_stream()` waits for pre-warm to complete instead of returning None.** Observed 2026-05-17 05:16:14 UTC for `camera.bosch_innenbereich`: HA logged `Error requesting stream: camera.bosch_innenbereich does not support play stream service` once during a stream activation. Root cause: `__init__.py:2621` sets `_live_connections[cam_id]` synchronously *before* the LOCAL pre-warm populates `rtspsUrl` (rtspsUrl is set at line 2749 only after pre-warm finishes). During that window — typically 5–25 s depending on camera model — `stream_source()` intentionally returns None (comment at `camera.py:519-523`), but the existing gate in `async_create_stream()` only checks whether `_live_connections.get(cam_id)` is truthy, so it skipped the auto-open path and called `super().async_create_stream()` directly. Super reads `stream_source()`, gets None, returns None, and HA's `_async_stream_endpoint_url` raises `HomeAssistantError("does not support play stream service")`. New code path: after the gate check, if `cam_id ∈ coordinator._stream_warming`, poll-wait at 500 ms cadence until warming clears (which happens at the same point `rtspsUrl` is populated) — with a deadline of `cfg.min_total_wait + 5 s` so a hung pre-warm does not pin the request forever. On timeout the method returns None with a `WARNING play_stream — pre-warm did not complete within Ns` log line, so genuine pre-warm failures still surface. This complements the existing `_StreamSupportNoiseFilter` (v12.4.1) — that filter limited the noise from the race; this fix eliminates the race so the filter rarely needs to fire.
- **Regression coverage**: 3 new tests in `tests/test_play_stream_during_prewarm.py` pin the contract — (a) pre-warm-in-progress waits for completion and then returns the stream; (b) pre-warm-timeout returns None with the warning; (c) when not warming, delegate immediately to super (backwards-compatible happy path).
- **No user-visible behaviour change for the happy path.** Stream toggle, switch behaviour, AUTO/LOCAL/REMOTE selection, privacy gate, heartbeat-by-PUT cred rotation, and `_StreamSupportNoiseFilter` are all unchanged. The only diff is what HA sees during the ~5–25 s pre-warm window: previously a misleading error, now a brief wait that resolves into a working stream.
- **`const.py — DEFAULT_OPTIONS['stream_connection_type']` flipped from `auto` to `local`.** New installs now default to pure-LAN streaming, no automatic REMOTE-fallback through the Bosch cloud proxy. This matches the integration's reverse-engineered LOCAL stack reaching feature-completeness in the v12.x series — the cloud round-trip is no longer needed for any day-to-day functionality (privacy, light, RGB wallwasher, panic siren, motion/audio events with FCM push, Mini-NVR local recording, SMB/NAS upload, dual-stream main+sub for external recorders). Users who explicitly want cloud-fallback can switch the per-integration `Stream Modus` select entity (or the `stream_connection_type` option in the Config-Flow options) back to `auto` (LOCAL with REMOTE fallback) or `remote` (cloud-only).
- **Soft migration for existing installs**: a new `async_migrate_entry` runs on entry load. For entries that never explicitly set `stream_connection_type` (silently relying on the old `auto` default), the value is now persisted as `auto` so existing users keep their REMOTE-fallback safety net untouched. Entries with any explicit choice (`auto`/`local`/`remote`) are preserved as-is. `ConfigFlow.VERSION` bumped 1 → 2 so HA invokes the migration. The defensive inline fallbacks in `__init__.py` / `select.py` / `config_flow.py` were updated from `"auto"` to `"local"` for consistency — these branches are dead code after `get_options()` merges `DEFAULT_OPTIONS`, but the change keeps the intent obvious to future readers.
- **Regression coverage (LOCAL-first)**: 7 new tests in `tests/test_local_first_default.py` pin the contract — `DEFAULT_OPTIONS` is `local`; `get_options()` returns `local` for empty option dicts; explicit `auto`/`remote` is never overwritten; migration v1→v2 sets `auto` only when the key is missing; v2 entries are no-op; `ConfigFlow.VERSION == 2`.
- **No code-path change for users with explicit Stream-Modus choice.** AUTO/LOCAL/REMOTE picker continues to work as before — the new default only affects entries that never touched the option.

## v12.4.1 — 2026-05-16

**Reliability round: blocking-call fix in Mini-NVR sensor, FCM zombie-task drain, FCM self-heal after stale creds, NVR start-recorder timing, firebase-messaging pin bump.**

- **`sensor.py` — `BoschNvrStateSensor.extra_state_attributes` no longer walks the pre-roll cache directory on every state read.** Home Assistant's blocking-call detector flagged `os.listdir('/config/bosch_nvr_preroll/<cam>')` from `recorder.py:221` (via `list_preroll_files`) running inside the event loop on every coordinator update — the property is synchronous and HA polled it tens of times per minute, stalling the loop for a few ms per call when the cache held more than a handful of segments. The sensor now reads `preroll_segments` from a coordinator-side counter (`_nvr_preroll_segment_counts`) populated by the pre-roll watcher itself, so the event loop never touches the filesystem from the sensor read path.
- **`recorder.py` — pre-roll watcher publishes the segment count.** The periodic prune tick (`_watch_preroll_recorder`, every 10 s while pre-roll is running) and the spawn-time prune (`start_preroll_recorder`) both call a new `_prune_and_count()` helper that returns the post-prune segment count in the same executor job. The counter is cleared in `stop_preroll_recorder()` so a stopped camera does not report stale numbers. `create_motion_clip()` was also moved off the event loop — its `list_preroll_files()` call now runs via `async_add_executor_job()`.
- **`fcm.py — async_stop_fcm_push()` now awaits the FCM client's pending tasks after `stop()`.** The upstream `firebase-messaging` library cancels its read loop via `task.cancel()` but returns from `stop()` before the cancelled coroutines finish their `finally: await self._do_writer_close()` SSL shutdown. Recreating the client (e.g. user toggles the FCM-push-mode select in the UI) while the old SSL session is still draining made the old read loop emit `ERROR firebase_messaging.fcmpushclient: Unexpected exception during read` once per ~63 s, with no traceback and no recovery, because the state machine sees the SSL close fire outside of `RESETTING`. The fix wraps `gather(*client.tasks, return_exceptions=True)` in `asyncio.wait_for(..., timeout=10)` after `client.stop()` so the new instance never races the old one's SSL teardown; a 10 s ceiling keeps the user-facing toggle responsive even when SSL shutdown deadlocks (upstream `sdb9696/firebase-messaging#33`). The library's missing `client.tasks` attribute on older versions is handled via `getattr(..., None) or []`.
- **New: FCM self-heal when stale persisted credentials trigger a reconnect-loop.** Even with a clean stop, the saved `fcm_credentials` in entry data can go stale (Google's mtalk endpoint rejects the saved Android-ID / security-token pair); the new client opens an SSL session that immediately fails, library auto-reconnects internally, every retry re-fails the same way and emits the `Unexpected exception during read` line again ~63 s later. The library's `is_started()` keeps returning True so the existing silent-death watchdog never fires. New code path: the noise-filter records timestamps of every `Unexpected exception during read` into a shared list; the coordinator's FCM watchdog calls `get_recent_fcm_error_count(300)` each tick and, when ≥ 3 errors landed in the last 5 min, fires `async_self_heal_fcm_push()` — which stops the client, deletes `fcm_credentials` and `fcm_registered_token` from entry data, and restarts FCM so `checkin_or_register()` issues fresh creds. A 30 min cool-down prevents thrash on a permanent WAN outage; the user does not need to remove + re-add the integration.
- **`fcm.py` — noise filter dedup window bumped from 60 s to 300 s** so a permanently-failing FCM session no longer logs an error every minute. The 60 s window let every reconnect attempt through; 300 s shows one heartbeat per 5 min and gives the watchdog enough margin to flip to polling-fallback without a noisy log.
- **`recorder.py` — `start_recorder()` polls for the TLS-proxy URL.** When `switch.bosch_<n>_mini_nvr` is toggled on within the first ~10 s of `switch.bosch_<n>_live_stream` turning on, the RTSP DESCRIBE handshake hasn't finished yet so `_live_connections[cam_id].rtspsUrl` is still empty. Previously the recorder bailed immediately with `WARNING NVR start skipped — TLS-proxy URL not ready` and waited for the next coordinator tick to retry (one stranded WARNING per tick until the URL landed). Now `start_recorder` polls the dict every 500 ms for up to 12 s; the WARNING fires only when the proxy truly fails to come up.
- **`manifest.json` — pin bumped to `firebase-messaging>=0.4.5,<1`** (was `>=0.4.0,<1`). Picks up any patch fixes since 0.4.0; the underlying library bugs (zombie task on cancel, stale-creds reconnect loop) remain open upstream and are what the new wait + self-heal logic above works around.
- **Regression coverage**: two new tests in `tests/test_nvr_state_sensor.py` (sensor must never call `list_preroll_files` from event loop; default-zero attribute when watcher hasn't populated); three new tests in `tests/test_fcm_round5.py` (pending SSL-close tasks awaited after stop; hung tasks hit the 10 s timeout but state still clears; older library versions without `client.tasks` keep working via `getattr` default).
- **`fcm.py` — new `_QuietFcmPushClient` subclass fixes the upstream state-machine ordering bug** (`github.com/sdb9696/firebase-messaging#33`). The library's `_listen()` decides whether to log loudly *before* `_reset()` advances `run_state` to `RESETTING`, so the very first connectivity error always took the loud `_logger.exception("Unexpected exception during read\n")` branch even when the connection was about to be reset. The subclass adds one line at the top of the `except (OSError, EOFError)` block — set `run_state = RESETTING` immediately so the existing quiet-path check evaluates True and routes to `_log_verbose` (INFO) instead. The ERROR record is never emitted, no traceback floods, no filter needed. `_get_fcm_push_client_class()` introspects `_listen` signature with `inspect.signature` before subclassing — if upstream ever changes the method shape, a DEBUG note logs and we fall back transparently to the vanilla `FcmPushClient`. Live verification 2026-05-16: 0 errors in 4+ min after deploy (was 14 in 14 min before this fix landed).
- **`fcm.py — _install_fcm_noise_filter()` now attaches the noise filter to BOTH loggers** (`firebase_messaging.fcmpushclient` AND `custom_components.bosch_shc_camera.fcm`). The subclass's fallback `else` branch logs via `_LOGGER`, which a single-logger install would miss. A single shared `_FCMNoiseFilter` instance is installed on both loggers so `_last_passed` and `_SHARED_ERROR_TIMESTAMPS` stay identical regardless of emission path; the 300 s dedup window now applies uniformly.
- **`camera.py — close_webrtc_session(session_id)` is now idempotent for unknown session IDs.** HA's websocket layer registers `partial(camera.close_webrtc_session, session_id)` as a cleanup callable *before* forwarding the WebRTC offer. When privacy mode blocks the offer (`stream_source()` returns None → `HomeAssistantError`), the offer handler never inserts the session into go2rtc's `_sessions` dict — but the cleanup is still registered. On client disconnect, HA invokes it, the dict `.pop(session_id)` raises `KeyError`, and HA logs `ERROR Error unsubscribing from subscription` once per disconnect with a full traceback. The override now wraps `super().close_webrtc_session()` in `try / except KeyError` that silently discards the unknown-session case and logs at DEBUG; other exception types still propagate so genuine bugs stay visible. Decorated with `@callback` to match the base contract.
- **`camera.py — async_create_stream()` short-circuits when privacy mode is ON.** Previously the privacy state was implicit — `try_live_connection` failed, the `WARNING play_stream — live connection failed` log fired, and HA's frontend got an opaque `does not support play stream service` error. Now the method consults the coordinator's privacy-mode cache (`privacy_mode is True` strict identity check; fails open on `None`/unknown to avoid blocking streams on coordinator startup) and raises `HomeAssistantError("privacy mode is ON")` immediately. The misleading WARNING is gone, frontend receives a meaningful message.
- **Regression coverage (additional)**: 4 new tests in `tests/test_fcm_round5.py` `TestFCMNoiseFilterDualLogger`; 7 new tests in `tests/test_fcm_round5.py` `TestQuietFcmPushClient`; 8 new tests in `tests/test_close_webrtc_session.py`.
- **Test-stub fix**: 46 pre-existing test stubs in `test_nvr_phases34.py`, `test_recorder_async.py`, `test_recorder_coverage_low.py`, `test_recorder_remaining_lines.py`, `test_recorder_watch_loops.py` now include `_nvr_preroll_segment_counts={}` on the SimpleNamespace coord stub so the new `start_preroll_recorder` / `stop_preroll_recorder` code paths don't hit `AttributeError`.
- **No user-visible behaviour change for the happy path.** FCM start/stop, push-mode auto/ios/android/polling, the silent-death watchdog that flips to polling, the credentials-persistence flow, WebRTC offers when privacy is OFF, and the NVR `idle / recording / error` state machine and its other attributes (`pending_uploads`, `last_segment_age_s`, etc.) are all unchanged.

## v12.4.0 — 2026-05-16

**External-stream URL exposure (Frigate / BlueIris); 5 new UI languages.**

- **New per-camera switch + 2 sensors for external recorder integration.** When the new `switch.bosch_<name>_external_stream_url_freigeben` is enabled, the integration publishes two diagnostic sensors per camera: `sensor.bosch_<name>_stream_url_haupt` (main quality, `inst=1`) and `sensor.bosch_<name>_stream_url_sub` (sub-stream, `inst=2`). Paste either URL straight into a Frigate `cameras.<id>.ffmpeg.inputs[].path` field or BlueIris's camera setup. Both URLs route through the existing per-camera TLS proxy, share the same Bosch session, and consume no extra cloud-API quota — RTSP is pull-based, so the camera only sends bytes when an external client actually opens a session. Default OFF on every camera; the switch + sensors ship `_attr_entity_registry_enabled_default = False` so a fresh install adds 12 extra registry rows that stay hidden until a user opts in. State persists across HA restarts via `RestoreEntity`. The URLs carry Digest credentials inline (the HA TLS proxy is a pure TCP-TLS tunnel — FFmpeg / Frigate / BlueIris handle Digest auth themselves), which matches the ~99 % of Frigate tutorials. A follow-up release will port the ioBroker v0.5.3 RTSP-aware Digest-injection proxy if there's demand for credential-free URLs.
- **5 new UI languages: Russian, Portuguese, Polish, Ukrainian, Simplified Chinese.** The integration now ships in **11 fully-translated locales** (EN · DE · FR · ES · IT · NL · PL · PT · RU · UK · ZH-Hans), matching the ioBroker adapter's coverage. Each of the 5 new files carries the full 329-string corpus already present in `en.json` / `de.json` — every config-flow page, options form, entity name, service description, repair-issue text, system-health label, and error message. Selection follows the user's Home Assistant frontend language — no reconfiguration. Technical terms (`Bosch`, `LAN`, `Wi-Fi`, `RTSP`, `FFmpeg`, `go2rtc`, `HACS`, `Lovelace`, `Frigate`, `BlueIris`, `Digest`, camera model names) stay in English in every locale per HA-community convention. The 14 new regression tests in `tests/test_external_stream_url.py` pin the substream feature's contracts (default-off, null when no session, inst= rewrite).
- **No code-path or runtime behaviour changes** for users not opting into the new switch. FCM push, alert routing, snapshot/video capture, motion detection, stream handling, and the Bosch cloud API surface are untouched. No migration required.

## v12.3.1 — 2026-05-16

**4 new UI languages; cleaner Signal alert captions.**

- **Full UI translations for French, Dutch, Italian, and Spanish** (`translations/fr.json`, `nl.json`, `it.json`, `es.json`). Each new locale carries the full 329-string corpus already present in `en.json` / `de.json` — every config-flow page, options form, entity name, service description, repair-issue text, system-health label, and error message. Combined with the existing German and English files, the integration now ships in **6 fully-translated locales** (DE · EN · FR · NL · IT · ES) covering ~242 k of the 626 k active Home Assistant installations per the latest [analytics.home-assistant.io](https://analytics.home-assistant.io/) snapshot. Selection follows the user's Home Assistant frontend language — no reconfiguration. Per-locale notes: ES uses HA-core-conformant *Armado/Desarmado* alarm vocabulary; NL uses *Ingeschakeld/Uitgeschakeld* (HA-NL standard, not a literal "armed/disarmed"); FR uses *Mode confidentialité*, *Armé (Présent/Absent)*; IT uses *Inserito/Disinserito*. Technical terms (`Bosch`, `LAN`, `Wi-Fi`, `RTSP`, `FFmpeg`, `go2rtc`, `HACS`, `Lovelace`, `Snapshot`, camera model names) stay in English in every locale per HA-community convention. The `folder_pattern` / `file_pattern` description text mirrors the Hassfest-validated workaround introduced in v12.2.1 — variables listed as bare backticked words, no curly braces.
- **`fcm.py` — `📂 https://…` Media Browser link removed from Signal alert step 2 (snapshot) and step 3 (video) captions.** The deep-link line was clutter alongside the inline image/clip attachment that the recipient already gets in the same message; users with mobile Signal clients reported the URL as visual noise that buried the actual filename / timestamp. The `📸 <camera> Snapshot (HH:MM:SS)` and `🎬 <camera> Video (HH:MM:SS, NN KB)` headers stay; the second line is gone. Helper function `build_browser_url()` plus its imports (`urllib.parse.quote`, `homeassistant.helpers.network.get_url` / `NoURLAvailableError`, `.const.DOMAIN`) deleted as the only callers were the two now-simplified caption builders. Net change in `fcm.py`: −40 lines, −3 imports. The Media Browser remains reachable via the HA UI as before — only the per-event push link is gone.
- **No code-path or runtime behaviour changes** beyond the caption shape; FCM push, alert step routing, snapshot/video capture, motion detection, stream URL handling, and the Bosch cloud API surface are untouched. No migration required. No automation, script, scene, or Lovelace card needs an update.

## v12.3.0 — 2026-05-15

**Fix doubled-prefix entity_ids (v11.0.0 regression); catch ValueError in snapshot path (HTTP 500 → cached image).**

- **30 entity classes had a doubled device-name prefix in their entity_id** (e.g. `button.bosch_est_bosch_est_refresh_snapshot` instead of `button.bosch_est_refresh_snapshot`). Root cause: v11.0.0 Gold-Compliance migration set `_attr_has_entity_name = True` on `button.py`, `update.py`, `select.py`, `number.py`, `binary_sensor.py` and `light.py` entities without removing the manual `f"Bosch {cam_title} "` prefix from `_attr_name`. HA then prepended the device name a second time. The bug was masked in the UI by `_attr_translation_key`, but the entity_id slug was generated from `_attr_name` and stuck in the registry forever. Reported in the HA community forum thread (post #15) on 2026-05-15.
- **Code fix**: 30 `_attr_name` declarations across 6 platform files now hold only the bare suffix (`"Refresh Snapshot"`, `"Pan Position"`, etc.); HA's `has_entity_name=True` handling prepends the device name automatically and the doubling stops.
- **Migration helper** `_migrate_doubled_prefix_entity_ids()` runs at the top of `async_setup_entry` (before platforms forward) and uses `entity_registry.async_migrate_entries` to rename every surviving buggy entity_id to its correct form. History and statistics are migrated automatically by HA core; user customisations (area, icon, friendly_name overrides, aliases) are preserved by `async_update_entity(new_entity_id=…)`. A WARNING is logged and a Repair Issue is created in Settings → Repairs with the rename count so users know to update automations, scripts, and Lovelace dashboards (HA does not rewrite YAML files when entity_ids change).
- **`Status code 500` on `/api/camera_proxy`**: `auth_utils.async_digest_request` raises `ValueError` when the camera returns a 401 without a `WWW-Authenticate: Digest` header (observed during half-rotated Digest credential states around FCM-flap windows). The two callers in the snapshot hot-path (`camera.py:_async_camera_image_impl` LOCAL Digest branch + `__init__.py:async_fetch_live_snapshot_local`) caught `(aiohttp.ClientError, asyncio.TimeoutError)` but not `ValueError`; the exception propagated up, HA core returned HTTP 500, and Telegram / Lovelace / automation proxies all rendered a brown error frame. Both `except` clauses now include `ValueError` and fall through to the cached image / placeholder.
- **Regression coverage**: five new test files — `test_doubled_prefix_button_update_select.py` (24 tests), `test_doubled_prefix_number.py` (32), `test_doubled_prefix_light_binary_sensor.py` (12), `test_camera_image_value_error.py` (6), `test_migrate_doubled_prefix.py` (21). Total 95 new regression tests; the doubled-prefix bug and the `ValueError`-escape can't reappear.
- **Breaking change for affected installs**: the entity_id rename is automatic, but any automation, script, scene or Lovelace card referencing one of the doubled-prefix names must be updated by hand. The Repair Issue lists examples; the v12.3.0 release page documents the full mapping.

## v12.2.1 — 2026-05-15

**Hassfest fix — rewrite `folder_pattern` / `file_pattern` helper text as prose to satisfy both formatjs and HA's validator.**

- v12.2.0 wrapped the literal `{camera}` placeholders in ICU single-quote escapes (`'{camera}'`) to stop the French frontend's `formatjs MISSING_VALUE` errors. That worked at render time but **Hassfest** (HA's official translation validator, runs in CI on every push) rejected the escape syntax with:

  ```
  [ERROR] [TRANSLATIONS] Invalid strings.json: the string should not
  contain placeholders inside single quotes
  ```

  → CI Validate workflow failed on v12.2.0.
- Both ``{name}`` and ``'{name}'`` are forbidden in helper-text strings. The only safe form is **prose**: list the variable names plain-text and explain in words that the user wraps them in curly braces when entering the actual pattern field.
- Updated wording in `strings.json`, `translations/en.json`, and `translations/de.json` for both `folder_pattern` and `file_pattern` descriptions. All variable names are still listed (backticks for monospace rendering).
- Regression test in `tests/test_translations_icu_escape.py` rewritten: now asserts **zero curly-brace tokens** (any form, escaped or not) in any `data_description` pattern string, plus a positive check that every variable name still appears in prose.
- Functional impact: none. The pattern field accepts the same `{camera}` syntax; only the helper text below the field is rephrased.

## v12.2.0 — 2026-05-15

**Event-driven snapshot refresh; per-camera-model settle delay; French frontend ICU-escape fix.**

- **Path A — live snap (~1-2 s after event)**: when an FCM push delivers a new motion/person/audio/vehicle/animal event, the integration immediately schedules `_async_trigger_image_refresh(...)` on the matched camera entity. This pulls a fresh frame from the camera (RCP 0x099e fast path → snap.jpg fallback) and propagates it to the image entity so the HA frontend shows an up-to-date picture within seconds of the event. Only real visual-event types trigger this — connectivity events (`TROUBLE_CONNECT`/`TROUBLE_DISCONNECT`) are excluded. Refresh tasks are reference-tracked so HA shutdown can cancel them cleanly.
- **Path B — Bosch cloud event image (~5-30 s after event)**: once the Bosch cloud has finished generating its event snapshot (often with motion-detection overlay / AI bounding boxes), the alert pipeline downloads it for the Signal/Telegram step-2 screenshot. The same bytes are now also pushed directly into the camera entity's `_cached_image`, atomically written to disk via `snapshot_store.save_snapshot`, and the image entity is notified via `async_notify_refreshed()`. The frontend receives a second update with the official Bosch-annotated image. Privacy-mode gate: update is skipped while privacy is ON. Length-based deduplication: skipped when the incoming byte-count equals the cached byte-count (avoids pointless double writes from duplicate Bosch pushes). Errors are caught per-path and logged at WARNING level without disrupting the alert pipeline.
- **Per-camera-model event-refresh delay** (`models.py`, new field `event_refresh_delay: float`). Gen2 cameras (Eyes Außenkamera II, Eyes Innenkamera II) capture immediately → set to `0 s`. Gen1 cameras (360 Innenkamera, Eyes Außenkamera) need ~`1.5 s` for the encoder to settle so the snap reflects the post-trigger frame rather than the pre-motion one. Path A reads the value via `get_model_config(hw).event_refresh_delay`; defensive fallback to default if `_hw_version` is absent. Diagnostic log line includes the chosen delay.
- **`formatjs MISSING_VALUE` fix** in the SMB-upload options dialog (HA Community forum report 2026-05-15): the helper text for `folder_pattern` and `file_pattern` contained bare `{camera}`, `{year}` etc. tokens as literal examples, which ICU MessageFormat interpreted as runtime variables → repeated `MISSING_VALUE` errors in the French frontend. All occurrences in `strings.json`, `translations/en.json`, and `translations/de.json` now wrap the braces in the ICU single-quote escape (`'{camera}'` renders as literal `{camera}`). New regression test `tests/test_translations_icu_escape.py` scans every `data_description` block for pattern keys and asserts no naked tokens remain — prevents this kind of leak from sneaking back in via copy-paste. Visual output unchanged; pure log-spam reduction.
- Streaming behaviour, alert routing, motion triggers, and sensor entities are unchanged.

## v12.1.0 — 2026-05-15

**New `image` entity per camera — fixes stale snapshot on iOS Companion App cold-open.**

- **Root cause**: HA's `CameraImageView` returns no `Cache-Control` header. WKWebView (iOS) applies heuristic disk-caching and serves yesterday's snapshot for up to 5 seconds before the fresh fetch returns. The fix: a separate `image.*` entity. Its signed URL changes on every `image_last_updated` state-push, so WKWebView treats each refresh as a distinct resource — no more stale frames on cold open.
- **New `image.bosch_<cam>_last_snapshot` entity per camera** (`image.py`): exposes the latest persisted snapshot. Use it in picture-entity cards, automations, and any consumer that needs a static JPEG reference without opening a live stream. The existing `camera.bosch_<cam>` entity is unchanged — it still streams and serves snapshots via HA's camera proxy.
- **Disk persistence** (`snapshot_store.py`): every successful background refresh now atomically writes the snapshot to `.storage/bosch_shc_camera/snapshots/{cam_id}.jpg`. On HA restart the camera entity immediately restores the last-known image from disk so the camera proxy serves a real frame from the first request — no more 1×1 black placeholder flash on cold start.
- **Atomic write**: temp file `{cam_id}.jpg.tmp` → `Path.replace` to final name. A crash mid-write leaves the previous snapshot intact.
- **Privacy mode honoured**: saves are skipped while privacy mode is ON (the privacy gate at the top of `_async_trigger_image_refresh` already prevents reaching the save path, plus an additional inline check for defence in depth).
- **Size guards**: snapshots < 100 B or > 10 MiB are rejected with a WARNING and never written to disk.
- **UUID validation**: `cam_id` is validated against the Bosch UUID format before constructing file paths — prevents path traversal.

## v12.0.5 — 2026-05-15

**Alert captions link to the Media Browser; NAS clip playback survives parallel range-requests.**

- **`fcm.py` — snapshot + video alerts now contain a `📂 https://…` line below the caption** (`build_browser_url` helper). The link uses the configured external HA URL when set so it works from outside the LAN, falling back to the internal URL. Backend kind is picked dynamically — `S` (SMB) when SMB upload is enabled, otherwise `L` (local). URL format is `media-browser/browser/<encoded-root>/<encoded-day-path>` derived from the HA frontend `ha-panel-media-browser.ts` parser, which only needs two encoded segments — no full breadcrumb chain. The link points to the day folder, not a leaf file: `media_source/browse_media` rejects leaf IDs, so the frontend errors on file-level deep links.
- **`media_source.py` — fixed two latent SMB bugs surfaced by Media Browser playback against NAS-stored clips.** (1) **SMB2 credit-pool exhaustion** — a shared `smbclient` session served every concurrent HTTP range-request from one `Connection` object, draining its ~64-credit window faster than responses replenished it and raising `Request requires 1 credits but only 0 credits are available` (production trace 2026-05-14). Fix per upstream maintainer recommendation (jborean93/smbprotocol#312): every public read builds its own `connection_cache` dict and registers a fresh session into it, giving each concurrent caller an independent credit window. (2) **`NtStatus 0xc0000043` sharing violation** — with credits isolated, parallel range-requests started colliding on FRITZ.NAS's exclusive-open default. `smbclient.open_file` now passes `share_access="r"` to allow concurrent readers. Verified with 9 parallel range-requests against a real NAS-stored clip — all 9 returned HTTP 206 Partial Content, log scan clean. Research notes: `knowledge-base/smb-credit-starvation.md`.

## v12.0.4 — 2026-05-13

**Gen2 siren trigger + Intrusion write-lock + ICU translation fix; Geräusch-Erkennung parked.**

- **`BoschPanicAlarmSwitch` — Gen2 siren trigger now actually works** (`switch.py`): the old `BoschAcousticAlarmButton` aimed at `PUT /acoustic_alarm`, which exists only on CAMERA_360 Gen1 — and Gen1 hardware has no integrated siren anyway. Gen2 (Eyes Innenkamera II / Aussenkamera II) uses `PUT /v11/video_inputs/{id}/panic_alarm` with `{"status":"ON"|"OFF"}` — confirmed via mitm capture. Implemented as a stateful switch because the endpoint is explicit ON/OFF; the siren keeps blaring until OFF is sent. Disabled by default (75 dB).
- **`BoschAcousticAlarmButton` no longer instantiated** (`button.py`): class definition kept for entity-registry backward compatibility, but removed from `async_setup_entry` — Gen1 hardware has no siren.
- **`BoschIntrusionDetectionSwitch` — slow-tier poll could revert the toggle** (`switch.py` + `__init__.py`): the `intrusionDetectionConfig` endpoint handler wrote the cloud value back into `_intrusion_config_cache` unconditionally, including in the eventual-consistency window right after a user toggle. New `_intrusion_config_set_at` write-lock dict + `_is_write_locked()` gate mirror the pattern used for `_audio_alarm_set_at`, `_arming_set_at` etc.
- **`BoschAudioAlarmSwitch` (Geräusch-Erkennung) parked — removed from setup**: mitm capture analysis showed our PUT body matched the iOS app byte-for-byte (sensitivity/threshold/enabled/audioAlarmConfiguration), Bosch returned 204, the toggle appeared correct in the Bosch app — but the camera's actual mic processing did *not* activate. Toggling the same field via the Bosch app activates it as expected. The trigger Bosch's camera uses to start audio processing is not visible at the HTTPS layer (likely an implicit RCP local call or device-subscription side-effect over the LOCAL stream session that the app maintains). Switch removed from `async_setup_entry`; class kept in code with full root-cause docstring so the parking decision survives refactors. The class-level cache+write-lock fix (`_audio_alarm_cache`/`_audio_alarm_set_at`) and `audioAlarmConfiguration` pairing fix were both implemented and tested while diagnosing — they remain in the code so the switch is correct *if* future capture analysis reveals the missing piece.
- **Translation/ICU fix — Options dialog used to render blank descriptions** (`config_flow.py`): the `events_storage.folder_pattern` and `events_storage.file_pattern` data_descriptions contained literal `{camera}`/`{year}`/etc. tokens that formatjs/ICU parsed as variables and rejected with `MISSING_VALUE`, so the entire description rendered empty. Fix: pass each token through `description_placeholders` as a self-substituting literal.
- **Docstring updates** (`number.py`, `switch.py`, `select.py`): documented the `audioAlarmConfiguration`/`enabled` pairing, corrected `BoschDetectionModeSelect` API values (`ONLY_HUMANS` / `ZONES` / `ALL_MOTIONS` — `PERSON_DETECTION` was never a valid value).
- **Tests**: +14 regression tests across `tests/test_audio_alarm_cache_revert.py`, `tests/test_intrusion_detection_write_lock.py`, `tests/test_panic_alarm_switch.py` — pin the cloud-PUT body shape, the cache + write-lock contracts, and the new switch behaviour.

## v12.0.3 — 2026-05-13

**trigger_snapshot — close residual race on Android Companion App resume.**

- **Lovelace card v2.12.7** (`src/bosch-camera-card.js`): the proactive `bosch_shc_camera.trigger_snapshot` call that refreshes the camera image after the app returns from background occasionally still triggered an Android Companion App native error popup ("unknown error") even with the service-existence guard added in v11.2.2/v11.2.5. Root cause: when the app resumes, `hass.services` still shows the cached service entry (guard passes), but the actual WebSocket call fails because the WS is mid-reconnect. Two new safeguards: (1) explicit `hass.connected` + `hass.connection.connected` check before each call so we skip while reconnecting, (2) the visibility-change-resume path now defers the trigger by 500 ms so the WS has time to finish reconnecting. Reported by @Andreas74 (simon42).
- No Python changes.

## v12.0.2 — 2026-05-13

**RCP cloud-proxy XML-leak hardening + Lovelace reauth banner.**

- **RCP read robustness** (`rcp.py`): the Bosch cloud proxy occasionally returns the outer RCP XML envelope (`<rcp>…</rcp>`) as the P_OCTET payload bytes instead of the requested binary value. Gen2 FW 9.40 additionally prefixes that envelope with whitespace (`\n\n<rcp>…`). The previous `raw.startswith(b"<")` guard missed the whitespace-prefixed form, causing the 4-byte uint32 unpack to log noisy "out-of-range values [168442994, 1668300298, …]" messages on every coordinator update for bitrate (`0x0c81`), LED dimmer (`0x0c22`), and clock (`0x0a0f`) reads. New shared `_is_xml_envelope()` helper detects both the plain and whitespace-prefixed XML cases (plus the pure-whitespace truncation that happens when the proxy clips an XML response into a 2-byte T_WORD slot). All three readers now silently `_mark_fail()` when the envelope is detected, so retries are bounded by the existing 3-strike threshold instead of looping forever. Bitrate (`0x0c81`) gained the missing `_skip`/`_mark_fail` wrapping for consistency with the other readers.
- **Lovelace card v2.12.6 — reauth banner** (`src/bosch-camera-card.js`): when the camera entity becomes `unavailable` (typically because the Bosch Cloud refresh token was rejected by Keycloak with `invalid_grant`), the card now shows an orange overlay with **"Anmeldung abgelaufen"** and a one-click **"Erneut anmelden"** button that deep-links to `/config/integrations/integration/bosch_shc_camera`. Z-index 9 places it over the existing offline overlay, and the offline overlay is suppressed while the auth banner is visible so the user sees the actionable re-login banner instead of a generic "offline" message. Covers all coordinator-down states, not just auth failures.
- **Tests** (`tests/test_rcp_extended.py` +11): pinned helper behaviour (none/empty/plain XML/whitespace-prefixed XML/pure-whitespace/binary/ASCII), plus regressions for the dimmer/clock XML cases and the bitrate `_mark_fail` retry bound.

## v12.0.1 — 2026-05-12

**Platinum polish — consolidate on HA built-in mechanisms.**

- Removed our own `Debug logging` checkbox from the Options flow — HA's built-in **Settings → Devices → Bosch Smart Home Camera → ⋮ → "Enable debug logging"** covers every sub-module via Python logger hierarchy (`tls_proxy`, `fcm`, `rcp`, etc.). Eliminates the dual-toggle UX confusion. `manifest.json` `loggers` corrected to `custom_components.bosch_shc_camera`.
- **System Health integration** (new `system_health.py`): Settings → System → System Health now shows `bosch_cloud` reachability, FCM push status, camera count, time since last FCM push, and quality-scale version — alongside Hue/Nest/etc.
- **Logbook custom events**: motion, audio-alarm, and person events from FCM push now appear in the HA Logbook with friendly messages like "Bosch Terrasse detected motion" instead of raw event dumps.
- `BoschStreamStatusSensor` now has `EntityCategory.DIAGNOSTIC` — moves out of the default dashboard into the device's diagnostic group.
- `BoschCameraEventsTodaySensor` `state_class` changed from string `"total"` to `SensorStateClass.MEASUREMENT` enum — correct semantic for a daily-resetting counter.
- README: new "Recorder DB size" section with `recorder: exclude:` example for high-frequency diagnostic sensors.
- Test suite: 3665 passing (was 3633 in v12.0.0). +24 logbook tests, +17 system_health tests.

## v12.0.0 — 2026-05-12

**Quality Scale: Gold → Platinum.**

- `strict-typing`: mypy --strict now green across all 24 source files (was 593 errors). Added `pyproject.toml` with strict mypy config, ~80 targeted `# type: ignore[misc/no-any-return]` for unavoidable HA-stub gaps.
- `async-dependency`: removed all `requests` imports from production code. HTTP Digest auth now via new `auth_utils.async_digest_request` (aiohttp); sync cloud downloads via stdlib `urllib.request`. Removes `requests>=2.28.0,<3` from runtime requirements.
- New module `auth_utils.py` (75 stmts, 100% test coverage, 29 tests).
- Test suite: 3633 passing (was 3604 in v11.2.7).

## v11.2.7

### Fixed

- **SENTINEL_RULE violations: 2 production-code fixes** (`__init__.py`, `camera.py`, `sensor.py`):
  - `_fcm_last_push` initialised to `0.0` → on hosts with `time.monotonic() < boot_threshold`, the `> 0` check in `sensor.py` reported a stale boot-time delta instead of "never received". Now initialised to `float("-inf")`; sensor guard updated to `!= float("-inf")`.
  - `_last_image_fetch` initialised to `0.0` → on CI / fresh-VM hosts with monotonic uptime < `IMAGE_REFRESH_INTERVAL` (1800 s), the proactive image-refresh tick was suppressed for the first 30 min after boot. Now initialised to `-86400.0` (finite, handles `int(cache_age)` conversion safely).

### Tested

- **Coverage sprint: 95 % → 99.79 %** (3232 → 3604 tests, 118 → 141 test files).
  All entity platforms now at 100 %: camera, switch, sensor, binary_sensor, light, number, select, button, update. Also 100 %: config_flow, diagnostics, fcm, rcp, shc, smb, media_source, recorder (-4 lines), local_rcp, cf_unbuffer, models. Remaining gap: `__init__.py` 9 module-load / setup-closure lines (tested in isolation) and `tls_proxy.py` 8 daemon-thread lines (known coverage-tool limitation on macOS for newly-spawned daemons). Bug-hunt agents confirmed: zero further production bugs.

## v11.2.6

### Fixed

- **Log noise: `cloud_set_privacy_mode: HTTP 444` for offline cams** (`shc.py`): Bosch cloud responds with HTTP 444 (silent connection close) when privacy mode is set on an offline camera. For users with a permanently offline cam (e.g. uninstalled / not yet commissioned), every privacy-mode toggle on any other cam triggered a cosmetic WARNING for the offline ones. The cloud HTTP call is now skipped when `_cached_status[cam_id] == "OFFLINE"`; local fallbacks (Gen2 LOCAL RCP / SHC) still run in case the cam recovered between status ticks.

## v11.2.5

### Fixed

- **Card: `trigger_snapshot` service guard missing in 3 call paths** (`bosch-camera-card.js` v2.12.5): v11.2.2 added a guard for the proactive load-time call, but three further call paths lacked the check — stream-stopped state update, snapshot-button `startPoll`, and pan-button `.then()` callback. On Android, the stream-stopped path fires on app-resume via WebSocket state updates before HA has finished registering services, producing the `bosch_shc_camera/trigger_snapshot konnte nicht ausgeführt werden` popup. Guard added to all three paths: `if (this._hass?.services?.bosch_shc_camera?.trigger_snapshot)` before each `callService` call.

## v11.2.4

### Changed

- **Card: LL-HLS enabled for all buffer profiles** (`bosch-camera-card.js` v2.12.4): `lowLatencyMode: true` is now set in all three hls.js buffer profiles (latency / balanced / stable). Previously only the `latency` profile used LL-HLS, so the `ll_hls: true` HA config had no effect for users on the default `balanced` mode. With `part_duration: 0.75 s`, the HLS fallback first-frame time drops from ~20 s to ~1.5–5 s depending on profile.
- **Card: `android_auto_stream` removed** (`bosch-camera-card.js` v2.12.3): The option introduced in v2.12.1 is removed — it started streams even when the user had manually stopped them. Existing configs with `android_auto_stream: true/false` load without error; the key is silently ignored.
- **Card: Android offline guard** (v2.12.2): `_toggleStream()` silently aborts when the server returns `unavailable` for the live-stream switch, preventing HA log warnings when OFFLINE cameras are referenced. Same guard in the auto-stream timer (`_isOffline` + `swState !== "unavailable"`).
- **Card: faster WebRTC retry** (v2.12.2): Retry attempts 6–7 use 5 s delay (was 10 s) to catch the go2rtc registration window (~5–8 s after stream switch turns on); further retries stay at 10 s.
- **LL-HLS** (`configuration.yaml`): `stream: ll_hls: true, part_duration: 0.75` reduces HLS fallback first-frame time from ~20 s to ~3–5 s on LAN.

> **Android tip:** enable **"Videos automatisch abspielen"** in the HA app settings to use WebRTC instead of HLS — first frame in ~1–2 s.

## v11.2.3

### Fixes

- **Card: Android WebView autoplay blocked** (`bosch-camera-card.js` v2.12.0): When Android's `mediaPlaybackRequiresUserGesture` setting blocks `video.play()` (HA app → "Autoplay videos" is disabled), the card now shows a tap-to-play overlay instead of silently freezing on a black screen. One tap satisfies the user-gesture requirement and starts the stream. The overlay also includes a hint to enable "Autoplay videos" in the HA app settings for a seamless experience.

## v11.2.2

### Fixes

- **Card: Android companion app startup error popup** (`bosch-camera-card.js` v2.11.9): Added service availability guard in `_triggerFreshSnapshot`. If `bosch_shc_camera.trigger_snapshot` is not yet registered when the card loads (Android companion app opens dashboard before HA finishes setup), the call is silently skipped instead of producing a native Android error toast.

## v11.2.1

### Fixes

- **Log noise: CF unbuffer startup message** (`cf_unbuffer.py`): "patch applied" message demoted from WARNING to INFO — no longer appears in the HA system log on every restart.
- **Log noise: TLS proxy EBADF suppression** (`tls_proxy.py`): `[Errno 9] Bad file descriptor` after credential rotation is now silently ignored. Expected during session teardown when the C→CAM pipe closes the shared TLS socket before CAM→C finishes.
- **Log noise: "Camera not found" burst** (`__init__.py`): Extended `_StreamSupportNoiseFilter` with a 60 s global rate-limit for "Error requesting stream: Camera not found". Collapses the go2rtc startup-race burst (up to 17 occurrences in 36 s) to at most 1 log line per minute.
- **Card: FCM push badge** (`bosch-camera-card.js` v2.11.8): Push status badge now shows "fcm" without appending the mode suffix (was "fcm ios" / "fcm android"). Mode detail is visible in the HA entity settings.

### Tests

- 20 new regression tests: `test_stream_noise_filter.py` (20), `test_cf_unbuffer.py` (+2), `test_tls_proxy.py` (+3). Total: 3347 / 3 skipped.

## v11.2.0

> **⚠ BETA — local NVR only, no cloud recordings.**
> This release ships the next phases of the built-in Mini-NVR (local-only continuous recording to your HA disk, NAS, or FRITZ.NAS).
> The recorder runs only while the camera is on a LOCAL stream — it stops cleanly when the connection falls back to the cloud relay.
> **Looking for testers!** If you try this, please share your experience (what works, what breaks, camera model + HA version) in the [simon42 thread](https://www.simon42.com/t/bosch-smart-home-kamera-integration-fuer-home-assistant/5221) or open a GitHub issue.

### New features

- **Phase 3 — Quality selector.** New NVR option `nvr_quality`: `auto` (default, full-resolution ~30 Mbps) or `low` (~1.9 Mbps, `inst=4` sub-stream). Useful for low-bandwidth NAS targets or Raspberry Pi setups. LOCAL-only — REMOTE sessions always use full-resolution.

- **Phase 4 — Pre-roll buffer.** When enabled (`nvr_preroll_seconds > 0`), a second ffmpeg process writes 10 s rolling segments to a tmpfs cache directory (`/dev/shm/bosch_nvr_cache` by default). On motion (FCM push), the last N seconds before the trigger are prepended to the motion clip, giving true pre-roll capture without a cloud dependency. Configurable via `nvr_preroll_seconds` (0–60) and `nvr_preroll_cache_dir` in the NVR options. A background watcher prunes the ring buffer every 10 s to keep it bounded at the configured depth.

- **Event-buffer-only mode (`nvr_event_only`).** New option: skip the 24/7 continuous recorder and run only the pre-roll ring buffer. Motion events still produce clips from the cached segments; disk usage stays in the single-digit MB range. Enable via `nvr_event_only: true` in the NVR options (requires `nvr_preroll_seconds > 0`).

- **Phase 5 — Timeline card (`BoschNvrTimelineCard`).** New Lovelace card type `custom:bosch-nvr-timeline-card`. Renders a 24-hour canvas strip of recorded segments and motion events. Clicking a segment seeks the embedded video player to that timestamp.

- **Phase 6 — Multi-camera stacked view (`BoschNvrMultiCamCard`).** New Lovelace card type `custom:bosch-nvr-multicam-card`. Stacks multiple camera streams with a shared seek bar and rAF-based drift correction that keeps playback positions within ±100 ms of each other.

### Fixes bundled in this release

- **Fix: pre-roll recorder never started — wiring omission.** `start_preroll_recorder()` was defined but never called from `start_recorder()`. The pre-roll ring buffer silently produced nothing even when `nvr_preroll_seconds > 0`. Confirmed live (2026-05-08): no cache directory was ever created. Fixed by wiring the call into `start_recorder()`, `stop_recorder()`, and `stop_all()`.

- **Fix: pre-roll ring buffer grew unbounded.** `prune_preroll_cache()` was only called once at ffmpeg spawn time. After that the cache accumulated indefinitely — confirmed live: 11 segments when max should be 4. Fixed by adding `_watch_preroll_recorder()`, a background task that prunes every `_PREROLL_SEGMENT_SECONDS` (10 s) while the pre-roll process is alive.

- **Fix: ffmpeg crashed with rc=8 on RTSP inputs due to HTTP-only `-reconnect` flags.** The `-reconnect`, `-reconnect_at_eof`, `-reconnect_streamed`, `-reconnect_delay_max` options are HTTP demuxer options only — passing them on an `rtsp://` input causes ffmpeg to exit immediately with `Option reconnect not found`. Removed from both the main segment recorder and the pre-roll recorder. The watcher (`_watch_recorder`) already handles respawn on TLS-proxy gaps.

- **Fix: ffmpeg failed to open segment file (`rc=254`) due to missing date subdirectory.** `-strftime_mkdir 1` does not create date-level subdirectories on the ffmpeg version bundled with HA (confirmed on HA 2026-05-08). `start_recorder()` now pre-creates today's and tomorrow's date dirs (`YYYY-MM-DD/`) under the staging path before spawning ffmpeg, so segment rotation across midnight also works without a restart.

### Internal

- 76 new tests: Phase 3 quality URL rewrite + ffmpeg-arg pins (8); pre-roll dir/pattern/prune/concat helpers (10); pre-roll motion-clip creation (9); pre-roll recorder lifecycle start/stop/stop-all (5); list_preroll_files (1); `_build_preroll_ffmpeg_args` wire-format pins including no-reconnect regression (6); `start_recorder` date-dir pre-creation (2); 22 Timeline-card contract tests; prune watcher loop + cancel + exit conditions (5); event-only mode (4); watcher task create/stop (4). Total: 3327 tests, 95% coverage.

---

## v11.1.1

No card changes.

### Bug fixes

- **Fix: FCM re-registration on every HA restart triggered HTTP 500 from Bosch.** Bosch's `POST /v11/devices` returns `{"status":500,"error":"sh:internal.error"}` when the same FCM device token is registered twice. FCM pushed fine (old registration still active), but the error filled the log on every restart. Fixed in two layers: (1) Bosch's `sh:internal.error` is now recognized as "already registered" — the token is saved to the config entry and the call returns success; (2) subsequent restarts skip the POST entirely when the saved token matches the current FCM token. The POST fires automatically again when the FCM token rotates (new `checkin_or_register()` result).

- **Fix: FCM registration failure body not logged.** When Bosch returned a non-2xx status on `POST /v11/devices`, only the HTTP status code was logged — not the response body. Diagnosing the `sh:internal.error` code required a separate debug session. The response body (first 200 chars) is now included in the WARNING log line.

- **Fix: SENTINEL_RULE violation in `_FCMNoiseFilter`.** `_last_passed` was initialized to `0.0` instead of `float("-inf")`. On systems with `time.monotonic() < 60 s` (container cold-starts), the very first FCM noise record was incorrectly suppressed instead of passing through.

- **Perf: concurrent post-FCM snapshot requests coalesced.** When an FCM push arrives, HA wakes all consumers simultaneously, causing up to 8 `fetch_fresh_event_snapshot` calls within ~230 ms — all fetching the same 350 KB JPEG from the Bosch cloud. A per-camera `asyncio.Lock` with an 8 s TTL cache now ensures only one network call fires per event burst; subsequent callers within the window receive the cached bytes without a round-trip.

### Internal

- 11 new regression tests: FCM skip-registration (same-token skips POST, new token fires POST, success saves token, 500 sh:internal.error treated as success); response-body logging assertion; sentinel value check for `_FCMNoiseFilter._last_passed`; fixed two test stubs missing `_entry` attribute; snapshot coalescing (second call uses cache, expired entry re-fetches, 3 concurrent calls produce 1 network request). Total: 3251 tests, 95% coverage.

## v11.1.0

No card changes.

### Bug fixes

- **Fix: enable_local_save toggle ignored — snapshots/videos saved despite feature being disabled.** Disabling the "Local Save" toggle in Options had no effect as long as a `download_path` was still configured. `fcm.py` checked only `download_path`, not `enable_local_save`. Additionally, `sync_local_save`, `sync_smb_upload`, `sync_smb_cleanup`, and `_sync_ftp_cleanup` in `smb.py` all lacked the toggle check internally (defense-in-depth fix — callers already guard, but each function now enforces it independently).

- **Fix: concurrent FCM events contaminate each other's filenames.** When two camera motions fired within seconds of each other, the FCM push pipeline for each event could use the wrong `event_id` when naming the saved file on NAS/local. Root cause: `async_send_alert` read `coordinator._last_event_ids[cam_id]` at upload time (up to 90 s after the push arrived), by which point newer pushes had already overwritten the shared dict. Fixed by snapshotting the event ID at push-arrival time and passing it as an explicit `event_id` parameter through the entire alert pipeline. The shared dict is now only a fallback for legacy call sites.

### Internal

- 20 new regression tests: toggle-off/on for all four smb.py functions + 2 fcm.py caller-path tests; concurrent-pipeline isolation test (two cameras push simultaneously — each upload receives its own event_id, not the other camera's); delegation assert for `_async_send_alert` event_id pass-through. All existing test fixtures updated with `enable_local_save`/`enable_smb_upload` defaults. Total: 3240 tests.

## v11.0.19

No card changes.

### Bug fixes

- **Fix: Media Browser — legacy year-first folders were hidden and not browseable.** Recordings stored in the old year-first layout (`2026/MM/DD/file.mp4` at the NAS or local root) were silently filtered out of the Media Browser camera list. Clicking the source showed camera-first cameras only; any `2026/` folder was invisible. Fixed by removing the filter: year-first folders now appear in the camera list and are fully navigable (`2026 → Month → Day → Event`). Works for all three backends: Local, SMB, and FTP (FRITZ.NAS). No file restructuring required — recordings stay in place. Removed the `migrate_year_first_events` service that was introduced in v11.0.18 as a workaround. Reported by Andreas74 (simon42, 2026-05-08).

### Internal

- 12 new regression tests: Local backend (year-first folders visible in `list_cameras`, `list_year_first_months/days/events`, 4-part resolver path); SMB backend (`list_cameras` includes year dirs, `list_year_first_months/days/events`); browse handler structural pins (`_browse_smb`/`_browse_local` call all three year-first methods, use `_YEAR_RE.match(camera)` for detection). Total: 3233 tests, 95% coverage.

## v11.0.18

No card changes.

### Bug fixes

- **Fix: Media Browser — event clips not playable when using local year/month/day folder layout.** When `folder_pattern` defaults to `{camera}/{year}/{month}/{day}` (the default since v11.0.13), new FCM-triggered downloads land in `Camera/2026/05/08/`. The Media Browser showed them correctly in the nested tree, but clicking play returned HTTP 404. Root cause: the file-serve view detected `year` as the second path segment and incorrectly routed the request to the SMB backend (`kind='S'`). When no SMB share is configured, `_find_source` returned `None` → 404. Fixed by checking whether an SMB source is actually configured before choosing `kind='S'`; falls back to `kind='L'` (Local) when only a local download path is set. Both old flat files (`camera/filename.jpg`) and new nested files (`camera/2026/05/08/filename.jpg`) now serve correctly from the same base directory. Reported by Georg (simon42, 2026-05-08).

### Internal

- 10 new regression tests for `_LocalBackend` camera-first tree (list_years / list_months / list_days / list_events_dated / resolve) and view routing disambiguation (legacy flat routes to Local, SMB date-first still routes to SMB, flat+nested coexist). Total: 95%, 3218 tests.

## v11.0.17

No card changes.

### Bug fixes

- **Fix: NVR crash-loop guard misfired on CI / fresh HA installs.** `_nvr_recent_crash.get(cam_id, 0.0)` violated the SENTINEL_RULE — on hosts where `time.monotonic()` was less than `_RESPAWN_WINDOW_SECONDS` (30 s), the very first NVR crash was misread as a crash-loop and the recorder never restarted. Fixed by using `float("-inf")` as the default so the check correctly evaluates "no previous crash".
- **Fix: Motion alerts suppressed at HA startup on hosts with low uptime.** `_alert_sent_ids.get(newest_id, 0.0)` and `fcm.py _sent.get(newest_id, 0.0)` violated the SENTINEL_RULE — on systems where `time.monotonic() < 60 s` (e.g. container restarts), the dedup guard fired immediately and dropped the first motion alert after startup. Fixed by using `float("-inf")` in both locations.
- **Fix: Camera status checks stopped firing when `scan_interval < interval_status`.** `_should_check_status` used the global `_last_status` timestamp (advanced on every scan tick) instead of the per-camera `_per_cam_status_at` — so whenever `scan_interval` was shorter than `interval_status`, the global timestamp was always "fresh" and per-camera status was never re-checked after the first tick. Fixed by using per-camera timestamps for all cameras.
- **Fix: `pre_warm_rtsp` leaked TCP writer on exception paths.** If `asyncio.open_connection` succeeded but the RTSP exchange raised, the writer was not closed. The no-nonce retry path also called `writer.close()` without `await writer.wait_closed()`. Both paths now properly close and await.
- **Fix: `_auto_renew_local_session` marked session stale after a successful heartbeat rescue.** The `renewal_fails` counter was not reset when a heartbeat-forced renewal succeeded — a session that recovered via heartbeat still accumulated prior failure counts and triggered `_session_stale=True` on the next failure threshold check. Fixed by resetting `renewal_fails = 0` on heartbeat-forced success.

### Improvements

- **Richer diagnostics JSON.** `Download Diagnostics` now includes: `integration_version` (top-level, from `manifest.json`), `debug_logging` flag and `stream_warming_count` in the coordinator section; per-camera `stream_error_count`, `stream_fell_back`, `session_stale`, and `offline_since_seconds` — the key signals for diagnosing stream-restart loops and session bugs without follow-up questions.
- **GitHub issue template.** New structured bug-report template (`.github/ISSUE_TEMPLATE/bug_report.yml`) guides users through: enable Debug Logging → reproduce → Download Diagnostics → copy log lines. Includes required fields for HA version, integration version, camera model and connection type.

### Internal

- Bug-hunt regression suite: 12 new tests in `test_bug_regression_v11.py` (37 total). 3 new diagnostics tests (11 total). Coverage: 95%, 3208 tests across 118 files.

## v11.0.10

No card changes.

### Bug fixes

- **Fix: camera entity friendly_name was doubled.** With `_attr_has_entity_name = True`, HA concatenates device name and entity name. Because both were set to `"Bosch {title}"`, the result was `"Bosch Terrasse Bosch Terrasse"`. Fixed by setting entity name to `None` (camera is the device's main feature), so HA uses the device name directly as the friendly_name.
- **Fix: Media Browser showed empty folders for files saved by v10.x.** Files downloaded by v10.x used the naming scheme `{date}_{time}_{type}_{id}.jpg` (no camera-name prefix). The filename parser required a camera-name prefix and silently skipped those files. Made the camera prefix optional so both old and new naming schemes are recognised. Reported by Andreas74 (simon42 forum).

### Internal

- Sprint G test coverage: `tls_proxy.py` daemon threads and `smb.py` FTP/cleanup paths (+137 tests, 2211 total).

## v11.0.9

**Card v2.11.7** — overview card visual editor + wider tile default.

### Card improvements

- **Visual card editor for bosch-camera-overview-card.** The overview card now exposes a UI editor in the Lovelace card picker: a Columns dropdown (Auto / 1 / 2 / 3 / 4) and — when Auto is selected — a Breakpoint field that controls the min-width at which the grid switches from 1 to 2 columns. Previously only configurable via YAML.
- **Default tile width increased.** `min_width` default changed from 360 px to 650 px (Auto mode). On a typical 1280 px-wide HA panel this keeps tiles full-width; set a lower value or switch to `columns: 2` to force a 2-column layout.

### Bug fixes

- **Fix: German translation placeholder mismatch on startup.** `de.json` had `{basis}/{Kamera}/{YYYY-MM-DD}` in `nvr_base_path` where HA's translation validator expected `{base}/{Camera}/{YYYY-MM-DD}` — logging `ERROR` on every HA startup. Fixed by aligning the German file. Regression test: `tests/test_translation_placeholders.py`.

### Internal

- Renamed 3 Python classes to drop the legacy `SHC` prefix: `BoschSHCCamera` → `BoschCamera`, `BoschSHCCameraConfigFlow` → `BoschCameraConfigFlow`, `BoschSHCCameraOptionsFlow` → `BoschCameraOptionsFlow`. Entity IDs and unique IDs unchanged — no user impact.

## v11.0.8

No card changes.

### Improvements

- **FCM-triggered local save replaces `enable_auto_download`.** Removes the polling-based auto-download toggle. Events are now saved to `download_path` immediately when an FCM push fires (same latency as SMB upload). The Media Browser local backend activates automatically when `download_path` is non-empty — no extra checkbox needed.

### Bug fixes

- **Fix: Media Browser showed empty dates.** `_download_one` omitted the camera-ID prefix in filenames, so `_FILE_RE` never matched and all dates appeared empty.

## v11.0.7

**Card v2.11.6** — kill the WebRTC popup on cellular.

### Card improvements

- **No more "stream konnte nicht geladen werden" toast on mobile data.** The original `_extCompanion` gate skipped WebRTC only for the HA Companion App over an external endpoint. Safari iOS / Chrome Android opened by URL over the same Cloudflare-Tunnel / Nabu-Casa endpoint fell through and tried WebRTC — which always fails on cellular networks because carrier-grade NAT (CGNAT) strips/proxies UDP. ICE timed out after ~5 s and the card surfaced the popup before HLS could take over. Reproduced by Thomas on iPhone Safari over mobile data, never on foreign WiFi-through-tunnel; web research (Kindgeek, HA community, go2rtc#554) confirmed cellular CGNAT + carrier UDP-blocking as the documented root cause. Fix: rename the gate to `_remoteSkipWebRTC` and fire it for `(Companion OR mobile browser) AND external endpoint`. iOS detection covers iPhone/iPod literally + iPadOS-13+ Safari (Mac UA + `maxTouchPoints>1`); Android via the literal `Android` UA token. Desktop browsers external still try WebRTC for the lower latency. Regression guards: `tests/test_card_lifecycle.py::test_remote_skip_webrtc_includes_mobile_browser`, `::test_remote_skip_webrtc_excludes_lan`.

## v11.0.6

**Card v2.11.5** — mobile-readability + stale-state polish.

### Card improvements

- **HLS info-banner readable on mobile.** Previously two dark-blue spans on a 10 %-blue tint sat on the black letterbox bars in fullscreen — effectively unreadable (screenshot 2026-05-06). Replaced with a single white-on-translucent-black "pille" overlay (`backdrop-filter: blur(6px)`, `border-radius: 8px`) absolutely positioned over the video, no longer sitting in the letterbox. Text consolidated to one line: "ℹ HLS-Modus (kein WebRTC über Tunnel)" — the previous redundant "WebRTC über Tunnel nicht möglich" span dropped. Regression guard: `tests/test_card_lifecycle.py::test_banner_uses_high_contrast_white`.
- **Stream badge no longer stale on mount** (CARD_STALE_APP, 2026-04-27). When the HA Companion App resumed from background, the card mounted with a stale `hass.states[camera]` snapshot — badge stuck "connecting" yellow despite backend already streaming. Fix: `_pullFreshSwitchStates()` now includes the camera entity, and the pull also fires on first-hass mount (previously only on `visibilitychange`). Closes the gap inside ~200 ms instead of waiting on the next WS frame. Regression guard: `tests/test_card_lifecycle.py::test_pull_fresh_states_includes_camera_entity`.

## v11.0.5

**Mini-NVR Phase 2** — local-only continuous recording goes opt-in production. OptionsFlow gets collapsible sections.

### Mini-NVR (LAN-only continuous recording)

- **New per-camera switch `switch.<cam>_nvr_recording`** + state sensor `sensor.<cam>_nvr_state`. Both opt-in via the new integration option `enable_nvr` (default OFF). Switches the recorder on/off; sensor exposes `target` / `pending_uploads` / `failed_uploads` / `last_segment_age_s` for diagnostics.
- **Hard prerequisite:** the camera's live-stream switch must be ON. Recording reuses the existing TLS proxy; if no live session is open, no recording. Privacy mode → live-stream blocks → no recording. LAN-only — REMOTE/cloud sessions never record.
- **Storage targets `local` / `smb` / `ftp`** — `nvr_storage_target` option. ffmpeg always writes locally to `_staging/` first (defends against partial-writes during segment rotation). A per-coordinator drain watcher promotes finalized segments (mtime > 60 s, size > 10 KB) to the chosen target every 30 s. SMB and FTP reuse the existing `smb_*` connection options + a new `nvr_smb_subpath` (default `NVR`). Failed uploads quarantined to `_failed/` after 5 retries + persistent_notification.
- **5-min wall-aligned MP4 segments** via `ffmpeg -c copy -segment_atclocktime 1`. No transcoding (0 % CPU overhead). `+faststart` so segments are browser-playable while still being written.
- **Lifecycle reactivity:** recorder restarts on Bosch credential rotation (~1 s gap, every ~333 s on Gen2 Outdoor); auto-stops on LOCAL→REMOTE fallback or privacy-on; auto-clean on integration unload / HA stop. Crash-loop guard: two crashes within 30 s → give up, surface error state.
- **Daily retention purge** (configurable `nvr_retention_days`, default 3) walks the configured target and deletes files older than cutoff. Empty date folders pruned afterwards.
- **Media Browser** — new "Recordings" branch alongside the existing event-clips path. Browse: Recordings → Camera → Date → Segment.

### OptionsFlow — collapsible sections

The integration's options dialog now groups its ~50 fields into 8 sections via HA's `data_entry_flow.section()`: Polling intervals · Features · Live stream · Push notifications · Storage (SMB/FTP) · Mini-NVR · Authentication · Debug. The first two default-open; the rest collapsed. `min_ha_version` raised to `2024.8.0` (section selectors require it). All existing options preserved — just rearranged.

### Test coverage

**+428 tests** session-total (588 → 1016). Total line coverage 34 % → **41 %**. New file `recorder.py` lands at **99 %** covered. Per-file deltas this round: `shc.py` 53 → **74 %** · `config_flow.py` 55 → **73 %** · `switch.py` 42 → **52 %** · `sensor.py` 50 → **52 %**. New test files: `test_recorder.py`, `test_recorder_async.py`, `test_recorder_drain.py`, `test_nvr_state_sensor.py`, `test_config_flow_sections.py`, `test_shc_light_component.py`.

## v11.0.4

**Card v2.11.4** — overview-card UX polish, big test-coverage round.

### Card improvements

- **Active-stream cameras jump to position 1 in the overview card.** While watching one camera live, its tile stays at the top of the grid even if other tier-0 cams sort earlier alphabetically / by Bosch priority. Detected via `switch.<base>_live_stream` state (reacts instantly when the user flips the toggle). Regression guard: `tests/test_card_lifecycle.py::test_overview_sort_promotes_active_stream`.
- **Phones in landscape now render a single column.** The previous `(max-width: 640px)` rule missed phones in landscape (iPhone Pro Max ≈ 932×430), so the grid stayed at 2 columns and each tile collapsed to ~12 lines tall. Added `(pointer: coarse) and (max-width: 1024px)` + `(orientation: landscape) and (max-height: 500px)` rules — desktops resized narrow keep their multi-column layout. Regression guard: `tests/test_card_lifecycle.py::test_overview_grid_single_column_on_mobile_landscape`.

### Test coverage

**+227 tests across 7 new files** — covers stream lifecycle, mobile-reload teardown, FTP/SMB path generation, FCM push handler, switch turn_on/off contracts, coordinator pure-state helpers (write-lock, JWT, RCP cache, quality, warming), select-entity option-key constants. Total now **815 tests / 37 % line coverage** (was 588 / 34 %). Per-file deltas: `switch.py` 42 → 51 % · `smb.py` 6 → 15 % · `__init__.py` 12 → 15 % · `select.py` 53 → 61 %.

## v11.0.2

**Card v2.11.2** — mobile reload fix.

### Bug fixed

- **Stream stalls 10–15 s on mobile after browser reload** — when the user reloaded the camera dashboard on a phone (iOS Safari or HA Companion App), the live stream would not resume immediately; instead it "appeared magically after many seconds". Desktop browsers were unaffected. Root cause: iOS Safari + WKWebView do not reliably fire the custom-element `disconnectedCallback` on tab reload, so `RTCPeerConnection.close()` and the WS subscription teardown never ran. The previous WebRTC consumer lingered on go2rtc's side as a stale slot until its internal timeout (~10–15 s) released it, blocking the next mount's `camera/webrtc/offer`. Fix: hook the `pagehide` event in `connectedCallback` to call `_stopLiveVideo()` — `pagehide` fires reliably on iOS / WKWebView right before unload, so `pc.close()` and the WS-unsubscribe message reach go2rtc / HA cleanly. Regression guards: `tests/test_card_lifecycle.py::test_pagehide_listener_wired` and `::test_pagehide_calls_stop_live_video`.

## v11.0.1

**645 tests across 38 files. 5 bugs fixed by writing the tests.**

### Bugs fixed

- **PRIVACY_REVERT race** (`shc.py:async_update_shc_states`) — first OFF-toggle of the privacy switch visibly reverted to ON for ~1-2 s, then settled. Root cause: the SHC fetcher overwrote `_shc_state_cache[cam_id]["privacy_mode"]` on every poll without honoring the `_privacy_set_at` write-lock that the cloud-fetcher path already respected. Within the cloud's eventual-consistency window (~10-30 s after a write), a stale ENABLED reading from the SHC clobbered the user's freshly-set OFF. Fixed by adding the same write-lock check the cloud path already uses. Confirmed by reproducing the bug in `tests/test_privacy_race.py::test_user_off_toggle_survives_stale_shc_poll` — the test would have failed pre-fix.
- **camera_light cache race** (same function, same shape) — discovered while writing the privacy regression test. The `entry["camera_light"] = (val.upper() == "ON")` assignment had the same unguarded write pattern. A user-toggle of the camera light was vulnerable to the same flip-back when the SHC poll hit within the eventual-consistency window. Fixed by extending the write-lock check to also cover `_light_set_at`. Regression guard: `tests/test_privacy_race.py::test_user_light_off_survives_stale_shc_poll`.

Both were already-known about in the codebase as A/B-Diag debug logs (CLAUDE.md TODO `PRIVACY_REVERT 2026-04-27`) — but the FORCE-RULE fix the comment promised was never applied. The privacy-race test reproduced the bug in <100 lines without any HA fixtures, then the camera_light bug was found by code inspection while looking at the same function.

- **RCP bitrate cache garbage** (`rcp.py:async_update_rcp_data`) — Gen1/360 cameras (e.g. `22222222`) return XML-wrapped responses for `0x0c81` via the cloud proxy. Without a guard, the bitrate parser interpreted XML character bytes as big-endian uint32 kbps values (`168442994`, `1668300298`, …), populating the bitrate cache with nonsense. Fix: skip XML-starting payloads and validate all values are in the 100–50 000 kbps range before caching. Regression guard: `tests/test_rcp_extended.py::TestAsyncUpdateRcpDataBitrate::test_garbage_bitrate_from_xml_not_cached`.
- **RCP network services cache garbage** (`rcp.py:async_update_rcp_data`) — same Gen1 proxy issue for `0x0c62`. Without a guard, the network services parser decoded the full RCP XML document as ASCII and appended it as a single service entry, logging several hundred characters of raw XML. Fix: skip XML-starting payloads. Log message now says "N services" instead of dumping the raw list.

### New tests under `tests/`

- **`test_service_validation.py`** (19 tests) — every service-handler input-validation path raises `ServiceValidationError` with the right `translation_key` + `translation_placeholders`. Covers `set_motion_zones`, `set_privacy_masks`, `share_camera`, `invite_friend`, `remove_friend`, `delete_motion_zone`, `update_rule`, `rename_camera`, etc. No aiohttp mocks — these tests fire BEFORE the network call.
- **`test_translations.py`** (8 tests) — strings.json + de.json + en.json all parse, exception keys match across all three files, every translation key matches Hassfest's `[a-z0-9_-]+` rule, no placeholders sit inside single quotes (the bug that bit the v11.0.0 squash), all `ir.async_create_issue` keys + state-based icon keys exist.
- **`test_quality_scale.py`** (5 tests) — manifest tier matches `quality_scale.yaml` rule statuses; no unknown rules (typo guard); every `exempt` and `todo` rule has a `comment`.
- **`test_models.py`** (24 tests) — every documented hardwareVersion resolves to a real `CameraModelConfig`, Gen2 Outdoor heartbeat stays at 3600 (regression guard for the rotating-Digest-cred bug), `@dataclass(frozen=True)` immutability holds, display-name inference for unknown hardware works.
- **`test_security_helpers.py`** (29 tests) — `_is_safe_bosch_url` SSRF allowlist (rejects internal IPs, localhost, AWS metadata host, homoglyph domains, non-HTTPS), `_redact_creds` redacts ephemeral Digest passwords without mutating the source dict, `get_options` merges `DEFAULT_OPTIONS` with entry options.
- **`test_coordinator_helpers.py`** (16 tests) — pure-state coordinator queries (`is_camera_online`, `is_session_stale`, `token`, `refresh_token`, `options`, `debug`) called from every entity's `available` property. ONLINE/OFFLINE/UPDATING_REGULAR/missing all handled correctly; in-memory token override prefers fresh value over stale entry.data.
- **`test_privacy_race.py`** (7 tests) — the regression guards described above + the symmetric camera_light coverage.

Plus pre-existing tests (`test_config_flow.py`, `test_diagnostics.py` extended with 5 more cases) — **120 total tests, all passing**.

### Entity-class test pass — switches / sensors / numbers / lights / etc.

Followed up the initial layer with stub-coordinator tests for every entity platform:

- `test_switches.py` (28) — `BoschLiveStreamSwitch` is_on/available/extra_attrs incl. privacy gate and stale-session gate; `BoschPrivacyModeSwitch` cloud-only-availability + cooldown logic + RCP cross-validation attribute; `BoschAudioSwitch`, `BoschTimestampSwitch`, `BoschPrivacySoundSwitch`, `BoschStatusLedSwitch`, `BoschNotificationsSwitch` (incl. all 3 cloud states).
- `test_sensors.py` (16) — `BoschCameraStatusSensor` + commissioned/firmware attrs, `BoschFcmPushStatusSensor` 3-state matrix (disabled/fcm_push/polling), `BoschCameraEventsTodaySensor`, `BoschFirmwareVersionSensor`, etc.
- `test_binary_sensors.py` (16) — motion / audio_alarm / person event sensors. Verifies the 90-second active window, malformed-timestamp handling, type-isolation (audio event must NOT trigger motion sensor).
- `test_buttons.py` (6), `test_numbers.py` (13) — incl. `BoschPanNumber` 180°-rotation sign-inversion logic for ceiling-mounted cameras.
- `test_light.py` (9) — Gen2 RGB light cache, brightness scaling 0-100 ↔ 0-255, last_color persistence even when off, invalid-hex graceful degradation.
- `test_select.py` (7) — video quality, FCM push mode, stream mode, motion sensitivity.
- `test_update.py` (12) — firmware update entity (installed/latest version, in_progress, attrs).

### Coverage (`pytest --cov`)

```
diagnostics.py    100%
const.py          100%
models.py         100%
update.py          90%
binary_sensor.py   85%
button.py          69%
sensor.py          50%
number.py          50%
select.py          47%
light.py           44%
switch.py          40%
config_flow.py     34%
shc.py             14%
__init__.py        10%
TOTAL              23% (228 tests)
```

Full 95% Silver `test-coverage` remains tracked as `todo` in `quality_scale.yaml`. The remaining 77% is the coordinator's async hot paths — every cloud-API call site, the stream lifecycle, FCM listener, SHC fetcher state machine — all of which need extensive aiohttp/firebase mocks. Filed as a separate sprint.

### `_alert_sent_ids` cache eviction starvation

Eviction was gated behind `if len(_sent) > 32` — if 4 cams burst-fired motion events all within the 120 s window, the cache grew past 32 but every entry was still < 120 s old, so the eviction loop ran but evicted nothing. Cache could grow without bound until the burst ended. Fix: drop the size gate, run plain age-based cleanup on every push (`if _sent:` truthy → loop). O(len) cost is fine; len stays small.

Regression guards in `tests/test_theoretical_bugs.py::TestAlertSentIdsEviction`. Plus pinning of FCM watchdog state-clearing invariants and stream-fallback timing constants in the same file.

### Binary sensor timezone bug — never fired in non-UTC timezones

User report (geotie, simon42 forum post #8): "Motion-Sensor wird oft nicht ausgelöst" — second component of the same complaint, found by reproducing the bug in a stub-coordinator script.

Root cause in `binary_sensor.py:_event_within_window`: Bosch `/v11/events` returns timestamps in UTC (`"2026-05-05T10:30:00.000Z"`). The function stripped the `Z` suffix and replaced the naive datetime's `tzinfo` with the user's local timezone — so a UTC event from 30 s ago appeared as `(local-offset)` hours old. In summer-time Europe/Berlin (`UTC+02:00`) that's 2 h 30 s, far outside the 90 s `EVENT_ACTIVE_WINDOW`. Result: motion / audio_alarm / person binary sensors never fired in any non-UTC user timezone — i.e. ~every German user.

Fix: parse the naive datetime as explicit UTC (`replace(tzinfo=timezone.utc)`) and compare both sides in UTC.

Combined with the polling-only `_last_event_ids` bootstrap fix below, this closes geotie's complaint completely. Regression guards in `tests/test_binary_sensors.py::TestEventWindow::test_utc_event_in_berlin_timezone_fires` (and three siblings).

### Polling-only mode never fired alerts after restart

User report (geotie, simon42 forum post #8): "Die obige Automation funktioniert, wird aber oft nicht ausgelöst." Found by mapping every forum issue to a regression test (CLAUDE.md `TEST_EVERY_BUG` rule).

Root cause: in `BoschCameraCoordinator._async_update_data`, the first-tick branch (`if prev_id is None:`) marked unread events as read but never set `_last_event_ids[cam_id]`. Without the seed, `prev_id` stayed `None` forever; the alert-chain `elif newest_id and newest_id != prev_id:` was never reached; `bosch_shc_camera_motion`, `_audio_alarm`, `_person` events never fired in polling-only mode (FCM disabled or unhealthy) after a restart.

Fix: bootstrap `self._last_event_ids[cam_id] = newest_id` at the end of the `prev_id is None` branch. Subsequent ticks now have `prev_id != None` and proceed to the alert chain on the next new event.

The FCM path already had its own bootstrap (`fcm.py:546-547`); only the polling path was broken. Most users have FCM enabled by default so the bug was masked, but anyone running with `enable_fcm_push=False` or whose FCM listener died (`_fcm_healthy=False`) hit this consistently.

Regression guard: `tests/test_forum_issues.py::TestIssue5_BinarySensorMissesEvents::test_polling_seeds_last_event_ids_on_first_tick`. Plus a new file `tests/test_forum_issues.py` with one `TestIssue<N>_…` class per forum-reported issue, enforced by `TestMeta::test_eight_forum_issues_have_test_classes`.

### Media Browser appears immediately after enabling auto-download

User report: "v10.7.1 → v11.0.0 installed, Media Browser stays empty." Root cause: `_enabled_sources` in `media_source.py` only added the Local backend if `download_path` already existed on disk. The v10.7.1 fix set a default path (`/config/bosch_events`) but didn't create the directory — so until the first event arrived and `sync_download` created it via `os.makedirs(folder)`, the Media Browser entry was hidden. Fixed: `_enabled_sources` now calls `Path(base).mkdir(parents=True, exist_ok=True)` before the `is_dir()` check, so the entry appears the moment auto-download is enabled. Regression guard: `tests/test_media_source_helpers.py::TestEnabledSources::test_auto_download_creates_missing_directory`.

### Generalized HTTP-201 acceptance across all cloud setters

After fixing the pan-setter HTTP-204 bug (below), grepped the codebase for similar patterns. Found 9 more places that accepted only `(200, 204)` instead of the standard `(200, 201, 204)` set Bosch can return:

- `BoschCameraCoordinator.async_put_camera` (the generic helper used by every switch entity)
- 6 service-action handlers in `__init__.py` (rule-update, motion-zones-set, share-camera, privacy-masks-set, delete-motion-zone, remove-friend)
- 2 intercom on/off PUTs in `switch.py`
- The number-entity audio-alarm setter

Bosch returns 201 Created on POSTs that create new resources (e.g. friend-share). Without 201 in the success set, the code logged a warning and returned False even though the camera/cloud had accepted the write. Single replace_all fix; no behavior change for the 200/204 happy paths.

### Pan setter HTTP-204 fix

`async_cloud_set_pan` only treated HTTP 200 as success — 201 and 204 (which Bosch can return on some firmware revisions or on indoor 360 cams) were misclassified as failures. Logged a misleading WARNING (`cloud_set_pan: HTTP 204`) and returned `False` even though the camera had accepted the position. Fixed to accept the same `(200, 201, 204)` set every other cloud setter uses; for 204 (no body) the requested position is used as the cache value. Surfaced by the new `tests/test_shc_setters.py::TestCloudSetPan::test_success` aiohttp-mock test.

### Generic write-lock helper — closes 4 more cache races

Initially filed as follow-up, but fixed in this release after all. Same bug shape as PRIVACY_REVERT, on four more caches: `_privacy_sound_cache`, `_timestamp_cache`, `_ledlights_cache`, `_arming_cache`. The user-toggle path wrote the cache optimistically on PUT success but didn't record a write timestamp; a coordinator slow-tier poll within the cloud's eventual-consistency window could revert the cache to the stale value.

Fix is a single `BoschCameraCoordinator._is_write_locked(cam_id, set_at_dict)` helper that all five guards (privacy + light + privacy_sound + timestamp + ledlights + arming + audioAlarm) now share. Each user-write site sets `_<field>_set_at[cam_id] = time.monotonic()` after a successful PUT; the coordinator's slow-tier handler calls the helper before overwriting. New `_set_at` dicts: `_privacy_sound_set_at`, `_timestamp_set_at`, `_ledlights_set_at`, `_arming_set_at`, `_audio_alarm_set_at`. Regression guards in `tests/test_cache_races.py`.

Net effect on real safety: the alarm-system arm state can no longer briefly flicker back to the previous value after the user toggled it. UX impact for the other three switches is more subtle (visual flicker only) but the same fix removes them.

### Tooling

- `pytest.ini` adds `asyncio_mode = auto` so the `pytest-homeassistant-custom-component` autouse fixtures work with pytest 9.
- `.venv-tests/`, `.coverage`, `.pytest_cache/`, `htmlcov/` added to `.gitignore`.

### Run the tests

```bash
python3 -m venv .venv-tests
.venv-tests/bin/pip install pytest pytest-asyncio pytest-homeassistant-custom-component home-assistant-frontend
.venv-tests/bin/pytest tests/ -v
```

## v11.0.1

**Pragmatic test-coverage layer + 2 real bugs found by writing tests.** 120 pytest cases across 7 files; the test work surfaced two race conditions that the production code never logged, both now fixed.

### Bugs fixed

- **PRIVACY_REVERT race** (`shc.py:async_update_shc_states`) — first OFF-toggle of the privacy switch visibly reverted to ON for ~1-2 s, then settled. Root cause: the SHC fetcher overwrote `_shc_state_cache[cam_id]["privacy_mode"]` on every poll without honoring the `_privacy_set_at` write-lock that the cloud-fetcher path already respected. Within the cloud's eventual-consistency window (~10-30 s after a write), a stale ENABLED reading from the SHC clobbered the user's freshly-set OFF. Fixed by adding the same write-lock check the cloud path already uses. Confirmed by reproducing the bug in `tests/test_privacy_race.py::test_user_off_toggle_survives_stale_shc_poll` — the test would have failed pre-fix.
- **camera_light cache race** (same function, same shape) — discovered while writing the privacy regression test. The `entry["camera_light"] = (val.upper() == "ON")` assignment had the same unguarded write pattern. A user-toggle of the camera light was vulnerable to the same flip-back when the SHC poll hit within the eventual-consistency window. Fixed by extending the write-lock check to also cover `_light_set_at`. Regression guard: `tests/test_privacy_race.py::test_user_light_off_survives_stale_shc_poll`.

Both were already-known about in the codebase as A/B-Diag debug logs (CLAUDE.md TODO `PRIVACY_REVERT 2026-04-27`) — but the FORCE-RULE fix the comment promised was never applied. The privacy-race test reproduced the bug in <100 lines without any HA fixtures, then the camera_light bug was found by code inspection while looking at the same function.

### New tests under `tests/`

- **`test_service_validation.py`** (19 tests) — every service-handler input-validation path raises `ServiceValidationError` with the right `translation_key` + `translation_placeholders`. Covers `set_motion_zones`, `set_privacy_masks`, `share_camera`, `invite_friend`, `remove_friend`, `delete_motion_zone`, `update_rule`, `rename_camera`, etc. No aiohttp mocks — these tests fire BEFORE the network call.
- **`test_translations.py`** (8 tests) — strings.json + de.json + en.json all parse, exception keys match across all three files, every translation key matches Hassfest's `[a-z0-9_-]+` rule, no placeholders sit inside single quotes (the bug that bit the v11.0.0 squash), all `ir.async_create_issue` keys + state-based icon keys exist.
- **`test_quality_scale.py`** (5 tests) — manifest tier matches `quality_scale.yaml` rule statuses; no unknown rules (typo guard); every `exempt` and `todo` rule has a `comment`.
- **`test_models.py`** (24 tests) — every documented hardwareVersion resolves to a real `CameraModelConfig`, Gen2 Outdoor heartbeat stays at 3600 (regression guard for the rotating-Digest-cred bug), `@dataclass(frozen=True)` immutability holds, display-name inference for unknown hardware works.
- **`test_security_helpers.py`** (29 tests) — `_is_safe_bosch_url` SSRF allowlist (rejects internal IPs, localhost, AWS metadata host, homoglyph domains, non-HTTPS), `_redact_creds` redacts ephemeral Digest passwords without mutating the source dict, `get_options` merges `DEFAULT_OPTIONS` with entry options.
- **`test_coordinator_helpers.py`** (16 tests) — pure-state coordinator queries (`is_camera_online`, `is_session_stale`, `token`, `refresh_token`, `options`, `debug`) called from every entity's `available` property. ONLINE/OFFLINE/UPDATING_REGULAR/missing all handled correctly; in-memory token override prefers fresh value over stale entry.data.
- **`test_privacy_race.py`** (7 tests) — the regression guards described above + the symmetric camera_light coverage.

Plus pre-existing tests (`test_config_flow.py`, `test_diagnostics.py` extended with 5 more cases) — **120 total tests, all passing**.

### Entity-class test pass — switches / sensors / numbers / lights / etc.

Followed up the initial layer with stub-coordinator tests for every entity platform:

- `test_switches.py` (28) — `BoschLiveStreamSwitch` is_on/available/extra_attrs incl. privacy gate and stale-session gate; `BoschPrivacyModeSwitch` cloud-only-availability + cooldown logic + RCP cross-validation attribute; `BoschAudioSwitch`, `BoschTimestampSwitch`, `BoschPrivacySoundSwitch`, `BoschStatusLedSwitch`, `BoschNotificationsSwitch` (incl. all 3 cloud states).
- `test_sensors.py` (16) — `BoschCameraStatusSensor` + commissioned/firmware attrs, `BoschFcmPushStatusSensor` 3-state matrix (disabled/fcm_push/polling), `BoschCameraEventsTodaySensor`, `BoschFirmwareVersionSensor`, etc.
- `test_binary_sensors.py` (16) — motion / audio_alarm / person event sensors. Verifies the 90-second active window, malformed-timestamp handling, type-isolation (audio event must NOT trigger motion sensor).
- `test_buttons.py` (6), `test_numbers.py` (13) — incl. `BoschPanNumber` 180°-rotation sign-inversion logic for ceiling-mounted cameras.
- `test_light.py` (9) — Gen2 RGB light cache, brightness scaling 0-100 ↔ 0-255, last_color persistence even when off, invalid-hex graceful degradation.
- `test_select.py` (7) — video quality, FCM push mode, stream mode, motion sensitivity.
- `test_update.py` (12) — firmware update entity (installed/latest version, in_progress, attrs).

### Coverage (`pytest --cov`)

```
diagnostics.py    100%
const.py          100%
models.py         100%
update.py          90%
binary_sensor.py   85%
button.py          69%
sensor.py          50%
number.py          50%
select.py          47%
light.py           44%
switch.py          40%
config_flow.py     34%
shc.py             14%
__init__.py        10%
TOTAL              23% (228 tests)
```

Full 95% Silver `test-coverage` remains tracked as `todo` in `quality_scale.yaml`. The remaining 77% is the coordinator's async hot paths — every cloud-API call site, the stream lifecycle, FCM listener, SHC fetcher state machine — all of which need extensive aiohttp/firebase mocks. Filed as a separate sprint.

### `_alert_sent_ids` cache eviction starvation

Eviction was gated behind `if len(_sent) > 32` — if 4 cams burst-fired motion events all within the 120 s window, the cache grew past 32 but every entry was still < 120 s old, so the eviction loop ran but evicted nothing. Cache could grow without bound until the burst ended. Fix: drop the size gate, run plain age-based cleanup on every push (`if _sent:` truthy → loop). O(len) cost is fine; len stays small.

Regression guards in `tests/test_theoretical_bugs.py::TestAlertSentIdsEviction`. Plus pinning of FCM watchdog state-clearing invariants and stream-fallback timing constants in the same file.

### Binary sensor timezone bug — never fired in non-UTC timezones

User report (geotie, simon42 forum post #8): "Motion-Sensor wird oft nicht ausgelöst" — second component of the same complaint, found by reproducing the bug in a stub-coordinator script.

Root cause in `binary_sensor.py:_event_within_window`: Bosch `/v11/events` returns timestamps in UTC (`"2026-05-05T10:30:00.000Z"`). The function stripped the `Z` suffix and replaced the naive datetime's `tzinfo` with the user's local timezone — so a UTC event from 30 s ago appeared as `(local-offset)` hours old. In summer-time Europe/Berlin (`UTC+02:00`) that's 2 h 30 s, far outside the 90 s `EVENT_ACTIVE_WINDOW`. Result: motion / audio_alarm / person binary sensors never fired in any non-UTC user timezone — i.e. ~every German user.

Fix: parse the naive datetime as explicit UTC (`replace(tzinfo=timezone.utc)`) and compare both sides in UTC.

Combined with the polling-only `_last_event_ids` bootstrap fix below, this closes geotie's complaint completely. Regression guards in `tests/test_binary_sensors.py::TestEventWindow::test_utc_event_in_berlin_timezone_fires` (and three siblings).

### Polling-only mode never fired alerts after restart

User report (geotie, simon42 forum post #8): "Die obige Automation funktioniert, wird aber oft nicht ausgelöst." Found by mapping every forum issue to a regression test (CLAUDE.md `TEST_EVERY_BUG` rule).

Root cause: in `BoschCameraCoordinator._async_update_data`, the first-tick branch (`if prev_id is None:`) marked unread events as read but never set `_last_event_ids[cam_id]`. Without the seed, `prev_id` stayed `None` forever; the alert-chain `elif newest_id and newest_id != prev_id:` was never reached; `bosch_shc_camera_motion`, `_audio_alarm`, `_person` events never fired in polling-only mode (FCM disabled or unhealthy) after a restart.

Fix: bootstrap `self._last_event_ids[cam_id] = newest_id` at the end of the `prev_id is None` branch. Subsequent ticks now have `prev_id != None` and proceed to the alert chain on the next new event.

The FCM path already had its own bootstrap (`fcm.py:546-547`); only the polling path was broken. Most users have FCM enabled by default so the bug was masked, but anyone running with `enable_fcm_push=False` or whose FCM listener died (`_fcm_healthy=False`) hit this consistently.

Regression guard: `tests/test_forum_issues.py::TestIssue5_BinarySensorMissesEvents::test_polling_seeds_last_event_ids_on_first_tick`. Plus a new file `tests/test_forum_issues.py` with one `TestIssue<N>_…` class per forum-reported issue, enforced by `TestMeta::test_eight_forum_issues_have_test_classes`.

### Media Browser appears immediately after enabling auto-download

User report: "v10.7.1 → v11.0.0 installed, Media Browser stays empty." Root cause: `_enabled_sources` in `media_source.py` only added the Local backend if `download_path` already existed on disk. The v10.7.1 fix set a default path (`/config/bosch_events`) but didn't create the directory — so until the first event arrived and `sync_download` created it via `os.makedirs(folder)`, the Media Browser entry was hidden. Fixed: `_enabled_sources` now calls `Path(base).mkdir(parents=True, exist_ok=True)` before the `is_dir()` check, so the entry appears the moment auto-download is enabled. Regression guard: `tests/test_media_source_helpers.py::TestEnabledSources::test_auto_download_creates_missing_directory`.

### Generalized HTTP-201 acceptance across all cloud setters

After fixing the pan-setter HTTP-204 bug (below), grepped the codebase for similar patterns. Found 9 more places that accepted only `(200, 204)` instead of the standard `(200, 201, 204)` set Bosch can return:

- `BoschCameraCoordinator.async_put_camera` (the generic helper used by every switch entity)
- 6 service-action handlers in `__init__.py` (rule-update, motion-zones-set, share-camera, privacy-masks-set, delete-motion-zone, remove-friend)
- 2 intercom on/off PUTs in `switch.py`
- The number-entity audio-alarm setter

Bosch returns 201 Created on POSTs that create new resources (e.g. friend-share). Without 201 in the success set, the code logged a warning and returned False even though the camera/cloud had accepted the write. Single replace_all fix; no behavior change for the 200/204 happy paths.

### Pan setter HTTP-204 fix

`async_cloud_set_pan` only treated HTTP 200 as success — 201 and 204 (which Bosch can return on some firmware revisions or on indoor 360 cams) were misclassified as failures. Logged a misleading WARNING (`cloud_set_pan: HTTP 204`) and returned `False` even though the camera had accepted the position. Fixed to accept the same `(200, 201, 204)` set every other cloud setter uses; for 204 (no body) the requested position is used as the cache value. Surfaced by the new `tests/test_shc_setters.py::TestCloudSetPan::test_success` aiohttp-mock test.

### Generic write-lock helper — closes 4 more cache races

Initially filed as follow-up, but fixed in this release after all. Same bug shape as PRIVACY_REVERT, on four more caches: `_privacy_sound_cache`, `_timestamp_cache`, `_ledlights_cache`, `_arming_cache`. The user-toggle path wrote the cache optimistically on PUT success but didn't record a write timestamp; a coordinator slow-tier poll within the cloud's eventual-consistency window could revert the cache to the stale value.

Fix is a single `BoschCameraCoordinator._is_write_locked(cam_id, set_at_dict)` helper that all five guards (privacy + light + privacy_sound + timestamp + ledlights + arming + audioAlarm) now share. Each user-write site sets `_<field>_set_at[cam_id] = time.monotonic()` after a successful PUT; the coordinator's slow-tier handler calls the helper before overwriting. New `_set_at` dicts: `_privacy_sound_set_at`, `_timestamp_set_at`, `_ledlights_set_at`, `_arming_set_at`, `_audio_alarm_set_at`. Regression guards in `tests/test_cache_races.py`.

Net effect on real safety: the alarm-system arm state can no longer briefly flicker back to the previous value after the user toggled it. UX impact for the other three switches is more subtle (visual flicker only) but the same fix removes them.

### Tooling

- `pytest.ini` adds `asyncio_mode = auto` so the `pytest-homeassistant-custom-component` autouse fixtures work with pytest 9.
- `.venv-tests/`, `.coverage`, `.pytest_cache/`, `htmlcov/` added to `.gitignore`.

### Run the tests

```bash
python3 -m venv .venv-tests
.venv-tests/bin/pip install pytest pytest-asyncio pytest-homeassistant-custom-component home-assistant-frontend
.venv-tests/bin/pytest tests/ -v
```

## v11.0.0

**Home Assistant Integration Quality Scale: Gold.** All Bronze foundation + all Silver stability + all Gold comprehensiveness rules verified rule-by-rule in `quality_scale.yaml`. Manifest declares `quality_scale: "gold"`. The integration is now on par with the most polished HA core integrations.

### Silver tier — UX wins

- **Service errors are visible.** All 16 service actions (`bosch_shc_camera.create_rule`, `set_motion_zones`, `share_camera`, …) now raise `ServiceValidationError` for bad input and `HomeAssistantError` for upstream failures. Previously failures were silently logged at WARNING level — clicking a button that hit HTTP 500 looked like nothing happened. Now HA shows a red error notification with the cause.
- **Runtime data on the config entry.** Coordinator state moved from `hass.data[DOMAIN][entry_id]` to `ConfigEntry.runtime_data`. Auto-cleaned on unload, no race window if `async_setup_entry` aborts halfway.

### Gold tier — UX wins

- **Diagnostics download.** Settings → Devices & Services → Bosch Smart Home Camera → ⋮ → Download diagnostics. Returns a redacted JSON snapshot with config entry data, options, coordinator state, FCM status, and per-camera summary (model, firmware, status, event count). Tokens, MAC addresses, FCM IDs, and SMB credentials are auto-redacted via `homeassistant.diagnostics.async_redact_data`. Replaces the manual log-collection workflow for bug reports.
- **Repairs UI for actionable problems.** Token-expired and Bosch-auth-server-outage notifications moved from `persistent_notification` to `ir.async_create_issue`. They now appear under Settings → System → Repairs with full description and severity, auto-clear when the issue resolves itself, and survive HA restarts.
- **Reconfigure flow.** Integration card menu → ⋮ → Reconfigure runs the OAuth login again and updates the same config entry in place. Entities, automations, FCM/SMB options — all preserved. Replaces the "delete + re-add" workaround when credentials need a manual refresh.
- **Stale-device cleanup.** Cameras removed from your Bosch account (e.g. via the Bosch Smart Camera app) are now automatically removed from the HA device registry on the next coordinator tick. Previously they stayed as ghost `unavailable` entities indefinitely.
- **Translatable exceptions.** All 50+ service-action exceptions use `translation_domain` + `translation_key` + `translation_placeholders`. German-locale users see localized error messages instead of English-only strings.

### Bronze + Silver hygiene

- README has a new **Removal** section with the clean-uninstall steps.
- `hass.data.setdefault(DOMAIN, {})` in `async_setup` removed — no longer needed after the runtime-data migration.
- Verified already-compliant rules: `has-entity-name`, `unique-id`, `PARALLEL_UPDATES = 0`, `entity-event-setup`, `config-entry-unloading`, `reauthentication-flow`, `entity-unavailable`, `log-when-unavailable` (coordinator-implicit via `UpdateFailed`).

### Icon-translations

- All 14 dynamic `def icon(self)` properties across `switch.py` + `sensor.py` removed; their state-based icon logic now lives in `icons.json` (`default` + per-`state` keys). Covers `live_stream`, `privacy_mode`, `audio`, `camera_light`, `notifications`, `intercom`, `privacy_sound`, `timestamp_overlay`, `status_led`, `intrusion_detection`, `alarm_system_arm`, `status` (online/offline), `fcm_push_status` (3 states), `stream_status` (5 states), and 6 notification-type variants.

### Test coverage

- New `tests/` directory with pytest framework using `pytest-homeassistant-custom-component`. Covers the Bronze `config-flow-test-coverage` rule (unique-config-entry, reauth, reconfigure, OAuth-create-entry) plus diagnostics-redaction tests (no FCM/JWT/private-key leaks).
- The Silver `test-coverage` rule (95% target) is partially met — full coverage of the 5000-line `__init__.py` and cloud-API paths needs extensive aiohttp mocking and is filed as a separate sprint. Tracked as `todo` in `quality_scale.yaml`.

## v10.7.0

**Event recordings now appear in HA's Media Browser — both local and NAS.** New `media_source` provider exposes downloaded events under **Media → Bosch SHC Camera**, with two backends auto-detected from existing options:

* **Local** — when *Events automatically download* is enabled with a `download_path`. Tree: *Camera → Date → Event*.
* **NAS / SMB** — when *SMB upload* is enabled (default for users who don't want to fill HA's small disk). Tree: *Year → Month → Day → Event*; matches the on-disk layout, all cameras share a day folder. Files are streamed on-demand via smbprotocol with HTTP `Range` support so MP4 seeking works.

Each event title shows time, type, and camera (e.g. `09:15:23 — MOVEMENT (Garten)`). MP4 clips play inline; JPEG snapshots double as thumbnails for the matching clip. macOS resource-fork files (`._*`) are filtered out — relevant for FRITZ.NAS / Time Machine targets.

When only one backend is configured, the source-chooser is hidden so the tree opens straight at the meaningful content. With both backends enabled the user picks *Lokal* vs *NAS* at the entry root.

**Manual filter — `Media Browser source` option.** New options-flow dropdown overrides the auto-detect when needed: *Auto* (default — show every backend with data), *Nur Lokal*, *Nur NAS*, *Deaktiviert* (hide the Media Browser entry entirely). Useful when both download_path and SMB upload are active but only one of them should appear in the browser.

Files are served by an authenticated `/api/bosch_shc_camera/event/…` view; path-traversal is blocked, only `image/jpeg` and `video/mp4` are returned. Forum thread context: [simon42 community post #14](https://community.simon42.com/t/bosch-smart-home-kameras-vollstaendig-in-home-assistant-custom-integration-mit-live-stream-bewegungssensoren-cloud-api-kein-shc-noetig/81743/14) — same UX as Reolink's `Media → Reolink` entry.

## v10.6.2

**Branding fix — switched to the right Bosch app icon.** v10.6.1 mistakenly used the blue *Bosch Smart Home* hub icon. v10.6.2 uses the red *Bosch Smart Camera* app icon (Robert Bosch GmbH, sourced from the official iOS App Store listing) — that's the camera-specific Bosch branding which matches what this integration actually does. Pure asset swap.

## v10.6.1

**Branding refresh.** The integration's icon files (`brand/icon.png`, `icon@2x.png`, `dark_icon.png`, `dark_icon@2x.png`) now use the official Bosch Smart Home brand mark — same icon HA Core's bundled `bosch_shc` integration uses (sourced from the [Home Assistant Brands](https://github.com/home-assistant/brands/tree/master/core_integrations/bosch_shc) repository, CC BY 4.0). Replaces the previous custom red camera icon for visual consistency with the rest of the Bosch Smart Home ecosystem in HA. Pure asset swap — no code change. *(Superseded by v10.6.2 — wrong icon variant.)*

## v10.6.0

**Image rotation 180° for ceiling-mounted indoor cameras.** New per-camera switch `switch.bosch_<cam>_bild_180deg_drehen` that rotates the camera image by 180° for upside-down (ceiling) mounting. Indoor-only — outdoor cameras have a fixed mounting orientation and don't get the switch. Three layers of effect, all client-side (Bosch firmware does not expose any image-rotation API):

- **Lovelace card** applies a CSS `transform: rotate(180deg)` to the `<video>` and `<img>` elements. Zero CPU, zero latency, GPU-composited — the toggle is instant with no stream restart and no re-encode.
- **Snapshot path** rotates the JPEG via PIL before serving it through `camera.async_camera_image()`, so push notifications, NAS clip uploads, and any other consumer that reads the camera entity also see the right-way-up image. ~15-30 ms per snapshot.
- **PTZ pan inversion** — for the Gen1 360 camera, `BoschPanNumber` automatically inverts the slider sign when the rotation switch is on, so "right" on the slider stays "right" on the user's screen even when the camera is upside-down.

State persists across HA restarts via `RestoreEntity`. Default OFF. Available on Gen1 360 Innenkamera and Gen2 Eyes Innenkamera II.

**Card v2.11.1** shipped alongside v11.0.1.

## v10.5.4

**Stream switch unblocked when prior session has expired upstream.** When a previous live session had its underlying URL invalidated (e.g. the relay-side lifetime cap was reached while the switch was still ON), HA's `Stream.stop()` could block waiting for a stuck FFmpeg reconnect-loop to exit. Both teardown paths (`_tear_down_live_stream` shared exit, fresh-toggle stale-Stream invalidation in `_try_live_connection_inner`) now wrap the call in `asyncio.wait_for(timeout=5)` and force-detach on timeout. Without this, a single hung `stream.stop()` held the per-camera setup lock for >5 minutes and every subsequent switch-ON returned `try_live_connection: already in progress for ... — skipping`.

**REMOTE session lifetime watchdog.** Mirror of the existing LOCAL keepalive task: when a stream opens against the cloud relay, a generation-tracked terminator is scheduled for `max_session_duration - 60 s` and tears the session down cleanly before the relay drops the RTSP TCP with a hard reset. Without this, the URL goes stale silently — switch shows "streaming" but FFmpeg is in a reconnect-loop, the next consumer sees a 2-minute HLS spinner. Generation counter shared with `_auto_renew_local_session`; OFF→ON cycles cancel the watchdog automatically.

**AUTO-mode REMOTE-fallback now self-heals.** Three independent fixes reduce the "permanently pinned to Cloud" failure mode that occurred after a transient LAN issue saturated the LOCAL error counter:

- `record_stream_error` skips the increment when the active connection is REMOTE — Cloud-side hiccups no longer count against the LAN's health budget.
- The error counter time-decays in `_try_live_connection_inner`'s AUTO branch: 5 minutes if the camera's TCP-ping cache says LAN is currently reachable, 30 minutes otherwise. Modeled after the existing `_LOCAL_RESCUE_TTL_SEC` decay for cred-rotation rescues.
- The status-loop's TCP-ping fast-path actively clears the fallback flag the moment LAN becomes reachable again — the next stream-on attempts LOCAL first instead of going straight to REMOTE. Only fires when `stream_connection_type == "auto"` and a fallback was actually in effect.
- During a *currently running* REMOTE-fallback stream, the same trigger additionally schedules a `try_live_connection(is_renewal=True)` so the live HLS session migrates Cloud → LAN via `Stream.update_source()` without waiting for a re-toggle. Brief (~2-3 s) re-buffer during the swap; LAN failure simply lands back on REMOTE. 5-minute cooldown prevents ping-pong if LAN flaps.

**`max_stream_errors` raised — per-model thresholds.** With self-heal in place a false fallback now recovers automatically, so the gradual-counter path can give LOCAL a fairer chance before giving up. Default bumped from 3 → 5 (indoor / `INDOOR`, `HOME_Eyes_Indoor`, default unknown), explicit override 10 for outdoor models (`OUTDOOR` / `CAMERA_EYES`, `HOME_Eyes_Outdoor` / `CAMERA_OUTDOOR_GEN2`) where real WLAN flap + slower encoder init produce more transient bursts. The watchdog's hard 120 s "no healthy HLS output" path is unchanged — it still forces REMOTE fallback regardless of this counter.

## v10.5.3

**`mark_events_read` default flipped to OFF.** v10.5.2 introduced the option but kept the previous behaviour as default — events were still being marked as read on the Bosch cloud after HA processed them, which silently consumed the "new event" highlight in the Bosch app for users who only use HA for live streaming. simon42 forum (Topic 81743 / Post 366006) confirmed the default-ON path was the wrong choice for the typical user. The default in `OPTIONS_DEFAULTS`, in the *Configure* dialog, and in all five gating call sites (`__init__.py` startup-poll / per-event tick / auto-download cycle, `fcm.py` push handler / clip handler) now resolves to `False` — fresh installs and existing installs that never explicitly toggled the option both stop firing `PUT /v11/events {isRead: true}`. The Bosch app keeps treating new events as unread regardless of whether HA already saw them. Users who prefer the previous behaviour (HA as primary client, no stale "new event" badges in the app) can enable it via *Integration → Configure → Mark Bosch cloud events as read*. English/German option-help text updated to describe the new default.

## v10.5.2

**New option `mark_events_read` (default ON).** The integration calls `PUT /v11/events {id, isRead: true}` after processing each motion/audio event from five different code paths (startup poll, per-event coordinator tick, auto-download cycle, FCM push handler, FCM clip handler). Side effect: motion events appear as already viewed in the Bosch app, even if the user only consumes them via HA's live stream and never opens an automation. Reported by xDraGGi on the simon42 forum (Topic 81743 / Post 364079). New option `mark_events_read` in *Integration → Configure* gates all five call sites — default `True` preserves backwards-compatible behaviour, set to `False` to keep events flagged as unread in the Bosch app while still receiving them in HA. Local dedup via `_last_event_ids` is unaffected (lives independent of the cloud `isRead` flag).

**Sensor renamed: `Event Detection` → `FCM Push Status`.** The diagnostic sensor `BoschFcmPushStatusSensor` was named "Event Detection" in entity translations, which suggested that a `disabled` state meant *no event detection at all*. In reality the sensor only reflects the FCM-push pipeline (states: `fcm_push` / `polling` / `disabled`) — normal coordinator polling continues regardless. The `unique_id` (`bosch_shc_camera_fcm_push_status`) was already correct and is unchanged, so historical state preserves cleanly across the rename.

**`FCM Push Mode` dropdown gated on master switch.** The per-integration `select.fcm_push_mode` entity is now `available=False` whenever `enable_fcm_push` is OFF in *Integration → Configure*. Previously the dropdown was fully interactive on the device page even though changing it had no effect until the master switch was enabled — discovered via simon42-forum PN where geotie reported `Event Detection: disabled` while showing `FCM Push Mode: Auto` in the same screenshot.

## v10.5.1 (patch)

**iOS native HLS direct-path (Card v2.10.20).** On iOS/WKWebView, WebRTC over Cloudflare Tunnel fails after a 5 s ICE timeout (UDP cannot traverse the HTTP tunnel) — the card then fell back to HLS, but the combined delay caused AVFoundation timeouts resulting in a ~1 minute black screen. Fix: iOS is detected via `!window.MediaSource && canPlayType("application/vnd.apple.mpegurl")` and the WebRTC attempt is skipped entirely; native HLS starts immediately via `video.src`. Desktop browsers (Chrome/Firefox) continue to use WebRTC as before. An info banner is shown while streaming on iOS: *"ℹ HLS (kein WebRTC auf iOS) — wird automatisch neu gestartet"*.

## v10.5.1

**Stream Status Sensor.** New `sensor.bosch_{name}_stream_status` entity per camera with states `idle / warming_up / connecting / streaming / streaming_remote`. `device_class: enum` with `_attr_options` so HA's more-info popup shows all possible states and a categorical state-history timeline. The card reads this sensor on every `hass` update — cold-open fix: opening a dashboard while the backend is already pre-warming shows the correct overlay and snapshot background without requiring a toggle click (`_awaitingFresh` guard prevents duplicate snapshot fetches). New `snapshot_during_warmup` card config option (default `true`).

**Full entity translation and documentation for all platforms.** Added `_attr_translation_key` + `_attr_has_entity_name = True` on `_BoschSensorBase` — entity names now render as "Bosch {Camera} {Sensor Name}" via translations instead of falling back to the device name. Removed conflicting `_attr_name` assignments from `sensor.py`. All 7 enum-like sensors now have `SensorDeviceClass.ENUM` + `_attr_options`. Full `entity.*` translation blocks in `en.json` and `de.json` covering all platforms: 26 sensors, 22 switches, 16 number entities, 5 selects (with per-state labels), 3 binary sensors, 2 buttons, 1 update, 3 lights. `_attr_entity_category` (CONFIG / DIAGNOSTIC / none) set on all entities across all platforms.

**Event type fixes.** `BoschLastEventTypeSensor.native_value` kept underscores in API values (`trouble_disconnect` not `trouble disconnect`) — options and translations expanded to cover `audio_alarm`, `trouble_disconnect`, `trouble_reconnect`. `BoschAlarmStateSensor` options expanded with `SYSTEM_MANAGED_ARMED / DISARMED`, `ARMED_AWAY`, `ARMED_STAY`, `DISARMED` after `SYSTEM_MANAGED_DISARMED` caused a `ValueError` at runtime on the Gen2 Indoor II.

**Cloudflare-Tunnel HLS-Buffering Workaround (`cf_unbuffer.py`, runtime monkey-patch).** Diagnosed 2026-04-29 from a remote-over-Cloudflare-tunnel session: cloudflared buffers HTTP responses by default per its `connection.shouldFlush(headers)` source — only `Content-Type: text/event-stream` / `application/grpc` / `application/x-ndjson`, no `Content-Length`, or `Transfer-Encoding: chunked` triggers streaming mode. HA's HLS endpoints (`/api/hls/<token>/*.m3u8` and `*.m4s` segments) hit none of those — `application/vnd.apple.mpegurl` / `video/mp4` with `Content-Length` set, no chunked. Cloudflared collected each segment in full at the edge before forwarding; iOS WKWebView on cellular gave up before the buffer flushed (visible in the cloudflared add-on log as `Incoming request ended abruptly: context canceled`). **Two-prong runtime monkey-patch** of HA's view classes (`homeassistant.components.stream.hls`): (1) `HlsMasterPlaylistView` + `HlsPlaylistView` get their Content-Type rewritten to `text/event-stream; x-actual=application/vnd.apple.mpegurl` — cloudflared `HasPrefix`-matches Branch (C) → flush. (2) `HlsInitView` + `HlsPartView` + `HlsSegmentView` get their `web.Response` re-emitted as a chunked `web.StreamResponse` (no `Content-Length`) — cloudflared `shouldFlush()` Branch (B) → flush. Verify with `curl -sI https://your-ha.example.com/api/hls/<token>/segment/0.m4s` — must show `Transfer-Encoding: chunked` and **no** `Content-Length`.

**iOS Companion App livestream fix (Card v2.10.14).** Root cause: `_startLiveVideo` called `_loadHlsJs()` unconditionally before the native-HLS fallback — WKWebView's stricter CDN policy caused hls.js load to throw, aborting the init before the `video.src` native path was tried. Fix: wrap `_loadHlsJs()` in its own try/catch; fall through to native playback when `Hls` is null or `Hls.isSupported()` is false (= iOS).

**Motion / Person / Audio binary sensor reliability fix.** Two compounding issues: (1) FCM push wrote to `_cached_events` but not to `coordinator.data[cam_id]["events"]` — sensors read stale event list on the same `async_update_listeners()` cycle. Fixed by mirroring immediately. (2) `EVENT_ACTIVE_WINDOW` raised from 30 s to 90 s to cover the full `scan_interval` in the polling-only path.

## v10.5.0

**FTP upload backend for NAS uploads + correctness fixes for the NAS settings.** **(1) FTP as alternative to SMB for event uploads.** The FRITZ!Box NAS (and several other consumer-grade NAS devices) handles SMB metadata operations very poorly — and on macOS Sequoia 15.x the smbfs client is also known to hang on cross-directory `rename()` for minutes at a time (multiple Apple-Discussions threads, plus AVM and PC-WELT documenting the FritzOS-CPU bottleneck on USB storage). Real measurement on a FRITZ!Box 7590 with ~3300 small files (JPG + MP4): SMB rename via macOS-mounted share blocked for 9+ minutes without a single completed move, while FTP `RNFR/RNTO` against the same hardware completed all 3117 moves in 42 seconds (~74 file/s). The integration now exposes a new `upload_protocol` option (SMB / FTP, default SMB for backwards compatibility). FTP reuses the existing `smb_server` / `smb_username` / `smb_password` / `smb_base_path` / `smb_folder_pattern` / `smb_file_pattern` fields — only the `smb_share` field is unused under FTP because FTP has no shares (the base path is taken relative to the FTP root, e.g. `FILES/Bosch-Kameras` instead of just `Bosch-Kameras` on a FRITZ!Box). All three sync paths are protocol-aware: the periodic event upload, the daily retention cleanup (FTP uses `MDTM` for accurate mtimes), and the disk-free check (skipped silently under FTP because there's no portable RPC for it). Implementation uses Python's stdlib `ftplib` so no new requirement is added to the manifest. **(2) NAS folder-pattern docs corrected.** The settings descriptions for `smb_folder_pattern` / `smb_file_pattern` listed placeholders as `[year]`, `[month]`, `[day]`, etc. — but the code actually uses Python `str.format()` with `{year}`, `{month}`, `{day}`, … so anyone who copy-pasted the documented pattern into the field got a `KeyError` on the next upload. All three translation files (`strings.json`, `translations/en.json`, `translations/de.json`) are now consistent with the code, plus the alert-storage path was corrected from `/media/bosch_alerts/` (wrong) to `www/bosch_alerts/` (the actual on-disk location served at `/local/bosch_alerts/`). **(3) Default folder pattern now `{year}/{month}/{day}`.** Previously `{year}/{month}` — for cameras that fire many motion events per day this produces folders with a thousand files inside them, which is hostile to both browsing and SMB performance. Existing custom patterns are untouched; only the default for new installs (and for upgraders who have not yet customised the field) changes. **(4) Translation cleanup.** German and English option screens are now 100 % key-aligned (no more cases of an option being labelled in one language but missing the description in the other). Previously-missing entries restored: `enable_go2rtc` description was missing in both languages, `debug_logging` description was missing in German. The `alert_notify_service` description in English now matches the German one and the actual code behaviour: this field is the *fallback* when per-type fields are empty, not a fan-out destination as it used to read. **(5) Helper script `migrate_smb_day_folders.sh`** ships at the repo root for users who want to migrate an existing flat `{year}/{month}/` layout into the new `{year}/{month}/{day}/` layout. Default dry-run, parses the day from filename, and runs against any mounted share.

## v10.4.10

**Three resilience fixes for stream stability + WAN-outage handling.** **(1) Stream stays on LAN after idle reconnect (Bosch session-cred rotation).** Symptom: AUTO mode pre-warms LOCAL successfully and runs cleanly for ~14 min, then — when the HLS consumer disconnects (browser tab closed) and HA's stream-worker later reconnects — the camera answers HTTP 401 on the same TLS proxy (Bosch silently rotated the per-session digest creds during the RTSP idle gap). After 3 consecutive `Error from stream worker: 401 Unauthorized` errors, AUTO fell back to REMOTE even though the LAN was perfectly reachable. **Reactive 401 rescue:** when `_handle_stream_worker_error` sees a 401 / "Unauthorized" / "authorization failed" message on a LOCAL session, issue one fresh `PUT /connection LOCAL` to obtain new creds before falling through to the REMOTE path. Gated by a per-camera `_local_rescue_attempts` counter (max 1 per failure burst) with a 5-minute time-decay so the counter doesn't stick at 1 after the first rescue: `record_stream_success` never fires when no HLS consumer is connected, so without time decay the next legitimate 401 burst (typically 8–14 min later) would skip straight to REMOTE. **Proactive cred refresh in heartbeat:** capture analysis (see `captures/api-findings.md` §1) showed the Bosch iOS app fires `PUT /connection LOCAL` at ~5 Hz during live view and consumes the fresh digest user/password from each response; the active RTSP connection is unaffected because Bosch only invalidates the rotated creds for *new* connects. Our heartbeat now mirrors this behaviour: each successful heartbeat parses the response, caches `user`/`password` into `_live_connections[cam_id]`, rebuilds the cached `rtspsUrl` with fresh creds, and calls `Stream.update_source()`. The running stream-worker is not disturbed (HA's `update_source` only changes the source for the next worker restart) — but when the worker eventually restarts after an idle gap, it picks up fresh creds and avoids the 401 in the first place. **(2) FCM noise filter for WAN outages.** Real-world finding 2026-04-28: when the home router rebooted, `firebase_messaging.fcmpushclient._listen` re-entered itself recursively on every retry, and each ERROR log line carried a ~3000-frame stack trace. With the 30 s reconnect cadence that produced ~200 log lines/s, ~12 500 lines/min, and the HA MainThread became wedged in stack-trace formatting and disk I/O — CPU rose from 30 % to 85 %, the bosch-shc-camera coordinator stopped firing entirely (no "Finished fetching" line for 4 min), and other integrations slowed too. New `_FCMNoiseFilter` (in `fcm.py`) attaches once to the `firebase_messaging.fcmpushclient` logger when FCM is set up: it strips `exc_info`/`exc_text` from "Unexpected exception during read" records (the recursive trace adds zero diagnostic value — we already know FCM disconnected) and rate-limits to one pass-through per 60 s. Reconnect behaviour is unchanged; the library still retries normally and recovers when WAN comes back, but the log volume drops from ~200 lines/s to ~1 line/min and the MainThread stays free. Library issue [sdb9696/firebase-messaging#33](https://github.com/sdb9696/firebase-messaging/issues/33) covers the abort-on-error angle but not the recursive trace itself, so a client-side filter is the right place. **(3) Same-camera stream-source race protection** (carried over from earlier work in this version): `try_live_connection: already in progress for X — skipping` is now the warning we see when two parallel start attempts collide; the first one always wins, the second exits cleanly without leaving a half-built TLS proxy or stale cache entry. **(4) Hardware-privacy auto-teardown.** When the camera's physical privacy button is pressed (or someone toggles privacy in the Bosch app), the cloud reports `privacyMode=ON` but our `BoschPrivacyModeSwitch.async_turn_on` — the only path that calls `_tear_down_live_stream` — never runs. Result before this fix: stuck `state: streaming`, the live-stream switch frozen on `on`, and the TLS proxy entering an endless reconnect loop against the now-gone camera (Errno 113 `Host unreachable`, observed in production at 06:25 on 2026-04-28 when a household member pressed the indoor cam's privacy button). New code path: in `_async_update_data`, when the privacy cache transitions OFF→ON outside the user-write lock and a live session is active, schedule the same teardown as the user-toggle path. **(5) TLS-proxy connect-failure circuit breaker.** When the camera goes physically offline (privacy button, power cut, Wi-Fi drop), HA's stream worker keeps opening new client connections every few seconds, and each one triggered a 10 s connect-timeout against the gone camera — burning CPU on a hopeless loop. After 5 consecutive connect failures within 30 s the proxy now closes its server socket; the coordinator (privacy-aware) decides whether to rebuild the session or stay torn-down. **(6) `does not support play stream service` log filter.** During the ~25 s LOCAL pre-warm window (PUT /connection → TLS proxy → encoder warm-up → rtspsUrl set) any consumer that calls the `camera/stream` WS API gets `stream_source()==None` and HA's camera component logs an ERROR. Real captures show 9 such lines in 15 s for a single stream start (multiple Lovelace tabs + Companion app + the card's own HLS-fallback path all polling around the same time). New `_StreamSupportNoiseFilter` keeps one ERROR per 30 s per `bosch_*` entity so a real "stream truly broken" issue still surfaces, but the pre-warm-window burst is collapsed to a single line. Other camera integrations are not touched. **(7) Overview card `use_bosch_sort` option.** New per-card opt-in flag for `custom:bosch-camera-overview-card` (Card v2.10.12 / Overview v1.1.0): when set, sorts cameras inside each tier (live → privacy → offline) by the Bosch-app priority instead of alphabetically. The priority is read from the new `bosch_priority` attribute on each Bosch camera entity, which mirrors the float `priority` field returned by `GET /v11/video_inputs` (settable via `PUT /v11/video_inputs/order` from the Bosch app). Default `false` preserves the old alphabetic ordering. YAML: `use_bosch_sort: true`. **(8) Card stale-state guard against accidental toggles** (Card v2.10.13). Diagnosed live 2026-04-28 14:00: a Live-Stream switch flipped to `off` from a system-admin user_id (iOS Companion App) with `parent_id: null` (= direct service call, not an automation) — but the user reported they didn't tap it. Root cause: when the HA-Companion-App suspends its WebSocket on backgrounding (Mobile/WLAN switch, app put away for a while), the local `hass.states` cache can briefly disagree with the server until the next WS push arrives. A user tap on the card's stream button during that window fires the wrong-direction toggle, because the card was reading a stale state. Fix in `bosch-camera-card.js`: (a) `_toggleStream` is now `async` and pulls the authoritative state via `GET /api/states/<switch>` immediately before `callService` — if the freshly-fetched state disagrees with what the card was showing, the toggle is aborted, the optimistic state is cleared, and the view is re-rendered (the user has to tap again with the now-correct state); (b) `_onVisibilityChange` (already wired to the Page Visibility API) now also pulls fresh REST states for the four primary toggle switches (live_stream, privacy_mode, audio, camera_light) when the page returns to the foreground, so a backgrounded card resyncs immediately rather than waiting for the next WS push. Behaviour unchanged when the card was already in sync; the REST round-trip adds <100 ms before the existing optimistic flip in the common path.

## v10.4.9

**Revert of v10.4.8 part 2 — privacy-mode RCP override was based on a wrong byte mapping.** A/B testing 2026-04-27 showed that RCP `0x0d00` byte[1] stays `1` regardless of the user-facing privacy-mode toggle (verified by toggling privacy ON↔OFF in HA and reading 0x0d00 before and after — no change). That byte therefore does **not** represent the privacy mode; rcp_findings.txt's "PRIVACY MASK state" label refers to a separate static configuration. The Bosch cloud `/v11/video_inputs.privacyMode` field was never the lie I claimed in v10.4.8 — it was the correct source of truth all along. **Removed:** the override block in `_async_update_data`, the mismatch override in `_refresh_rcp_state`, the `async_update_listeners()` trigger, the camera-entity attributes `rcp_privacy_mode` / `rcp_led_dimmer` / `rcp_state_age` / `rcp_state_source` (since the underlying cache is no longer populated for those keys), and the helper functions `parse_privacy_state` / `parse_led_dimmer_percent` from `local_rcp.py`. **Kept:** the generic `rcp_read_local_sync` / `rcp_read_remote_sync` helpers (correct), the `_rcp_state_cache` dict scaffolding, and the post-stream-start `_refresh_rcp_state` hook (now a marker, ready for future verified RCP+ reads). The lesson: never ship a feature that overrides authoritative state from one source with another, without first confirming via a controlled toggle that the new source actually reflects the toggled value.

## v10.4.8

**Local RCP+ READ via the ad-hoc `cbs-…`-user from `PUT /connection`** + **Bosch Cloud `privacyMode` correction.** Two parts: **(1) RCP+ reads.** New module `local_rcp.py` issues HTTP Digest reads against `https://<cam>:443/rcp.xml` (LOCAL session) and HTTP Basic-empty against `https://proxy-XX:42090/{hash}/rcp.xml` (REMOTE session — Cloud-Proxy fallback when HA is not on the LAN). Verified on Gen2 Outdoor FW 9.40.25: 10 reads/10 s did not rotate creds or kill the running stream — only `PUT /connection` rotates, normal RCP reads are safe. Two fields pulled opportunistically after each successful stream start: `rcp_privacy_mode` (from `0x0d00` P_OCTET, byte[1]==1 means ON) and `rcp_led_dimmer` (from `0x0c22` T_WORD, 0–100 %). Exposed as camera entity diagnostic attributes plus `rcp_state_age` (seconds since last read) and `rcp_state_source` (`local` / `remote`). **(2) Privacy-mode correction.** Diagnosed live 2026-04-27: Bosch Cloud `/v11/video_inputs.privacyMode` returned `'OFF'` for the Terrasse (Gen2 Outdoor, ONLINE, physically in privacy) while every offline camera and the camera's own RCP read correctly returned `ON`. The HA `switch.bosch_<cam>_privacy_mode` entity, the `BoschLiveStreamSwitch.available` gate, the snapshot-fetch short-circuit, and `try_live_connection`'s privacy guard all read `_shc_state_cache.privacy_mode` — so the cloud lie propagated everywhere. **Fix:** RCP+ now refines the SHC cache aggressively when (a) SHC is None (unconfigured, was already the v10.4.8-part-1 behavior), or (b) SHC and RCP disagree and no user-write lock is active — RCP wins because it reads camera hardware directly. Two override sites: `_refresh_rcp_state` corrects on each stream start, and the Cloud-Coordinator-Tick re-checks the RCP cache (≤120 s old) and re-corrects after every cloud refresh, so the cloud lie cannot resurface. `async_update_listeners()` is fired on each correction so the privacy switch flips immediately, without waiting for the next 60 s tick. The local `/rcp.xml` endpoint returns XML (not the binary TLV the Cloud-Proxy uses on the same path), so the parser is XML-based. Read-only — writes require additional credentials that aren't currently exposed via the local API.

## v10.4.7

**New option: HLS player buffer profile (`live_buffer_mode`).** Adds an integration-options dropdown to choose how aggressively the Lovelace card pre-buffers video before showing it. Three modes: **Latency** (~4-6 s lag, may stutter on flaky Wi-Fi), **Balanced** (~8-10 s lag, default — robust against typical Wi-Fi hiccups), **Stable** (~12-15 s lag, smooth even on weak links). Mapping is hardcoded client-side in the card: each mode sets `liveSyncDurationCount`, `liveMaxLatencyDurationCount`, `maxBufferLength`, `maxMaxBufferLength`, and `lowLatencyMode` on the hls.js instance. The previous values (`3 / 6 / 10 / 20 / true`) corresponded roughly to "Latency"; the new default is "Balanced" (`4 / 8 / 14 / 22 / false`), which is why existing users may see slightly more lag (~2 s) but fewer stutters out of the box. The `maxBufferLength` cap stays well below HA's 30 s `OUTPUT_IDLE_TIMEOUT` for all three modes, so FFmpeg is never killed by the idle watchdog. Audio quality is higher than the official Bosch app — the mobile app downsamples audio for cellular bandwidth, while this integration delivers the unmodified AAC-LC stream. **Also fixed a UX confusion:** the card's "Reaktion" info field now has a tooltip clarifying that the `500 ms` / `1000 ms` value shown is the Bosch-API response hint (`bufferingTime` from `PUT /connection`), not the player buffer — the latter is now controlled by the new `live_buffer_mode` option in integration settings.

## v10.4.6

**Three hardening changes. (1) Privacy enforcement — stream cannot be started when Privacy Mode is ON.** Four bypass paths existed: `BoschLiveStreamSwitch.available` returned `True` while privacy was active (entity appeared clickable); `async_turn_on` used a fragile string comparison (`str(…).upper() in ("ON", "TRUE", "1")`) and issued a `persistent_notification` on the old code path; `BoschAudioSwitch._apply_audio_change` called `try_live_connection` without checking privacy; and `coordinator.try_live_connection()` had no guard at all. Fixes: `available` now gates on `bool(_shc_state_cache.get(cam_id, {}).get("privacy_mode"))` (entity greys out); `async_turn_on` raises `ServiceValidationError` (HA toast in UI, clean exception — no more persistent notification); `_apply_audio_change` logs a warning and returns early; `try_live_connection` has an early-exit guard (fail-open when cache not yet populated at boot). **(2) Icon — no changes needed.** Legal assessment confirmed the current SVG does not reproduce the Bosch trademark (uses Bosch red as a color only, not the circular wordmark). **(3) Translation fixes (EN + DE).** DE: standardised formality to informal "du" throughout (`user.description` heading); added missing `debug_logging` label (was in EN, absent in DE); corrected `alert_save_snapshots` path `/www/bosch_alerts/` → `/media/bosch_alerts/`. EN: already consistent, no changes.

## v10.4.5

**Two fixes. (1) Fix: LOCAL snapshot was 6–10 s; now matches REMOTE speed (~1 s).** The `imageUrlScheme` field from `PUT /connection LOCAL` response defaults to `https://{url}/snap.jpg` with no resolution parameter. Without a `?JpegSize=` parameter, the camera triggers a full-resolution on-demand capture from the sensor — slow (~8 s when idle). The REMOTE path already hardcodes `?JpegSize=1206`. Fix: append `?JpegSize=1206` to the LOCAL `proxyUrl` when no `JpegSize=` is already present. One-line change in `__init__.py`. Probe-confirmed: adding any `JpegSize` parameter on the LAN path cuts snapshot latency from 8 s to ~1.4 s (7×) when the camera is idle; with an active stream the latency was already <100 ms regardless. **(2) Fix: TROUBLE_CONNECT / TROUBLE_DISCONNECT alerts now route to `alert_notify_system` instead of the information path.** Previously, camera connectivity events (camera going offline or back online) were dispatched via `"information"` — same path as motion/person events — so they landed on the video clip service (or the fallback service) instead of the configured system notification service. Fix in `fcm.py`: detect TROUBLE events at dispatch time and route the text notification through `_notify_type("system", …)`. Steps 2 (snapshot) and 3 (clip) are skipped entirely since connectivity events carry no media. Also fixes an edge case where the early-return guard blocked TROUBLE events when no `alert_notify_information` service was configured.

## v10.4.4

**Hotfix for v10.4.3:** the privacy short-circuit accessed `self._camera_status_extra` directly — but that dict isn't allocated until the first successful coordinator tick. During the boot/integration-load window (and on any HA restart), `async_camera_image` raised `AttributeError: 'BoschCameraCoordinator' object has no attribute '_camera_status_extra'`, which the v10.4.2 wrapper caught and served the placeholder JPEG — but every snapshot-refresh background task also failed with the same error in `_async_trigger_image_refresh`, so cameras showed only the placeholder until the cache had warmed up. Fix: `getattr(self, "_camera_status_extra", {}).get(cam_id, {})` — falls through to normal fetch when the cache isn't ready yet, identical pre-v10.4.3 behavior. v10.4.3 was live ~10 minutes before this regression was caught in the post-deploy log scan; rolled forward rather than reverted because v10.4.4 keeps the network-call optimization once the cache is warm.

## v10.4.3

**Optimization: skip snapshot fetches when Privacy Mode is ON.** Both `async_fetch_live_snapshot` (REMOTE Cloud-proxy path) and `async_fetch_live_snapshot_local` (LAN HTTPDigest path) now short-circuit and return `None` immediately when the cached `privacy_mode` flag is `True` for the camera. Before: every coordinator tick (~1/min) would issue a `PUT /connection` REMOTE → snap.jpg request, get HTTP 200 with 0 bytes (Bosch backend behavior when the privacy shutter is closed), and log a debug line "empty response (privacy mode ON?)". With 4 cameras and one in privacy, that's ~4-8 wasted PUT/connection cycles per minute plus the same number of debug log lines, even though we already know the answer from the cached `privacyMode` field in the same `/v11/video_inputs` response we'd just fetched. The privacy state is read from `_camera_status_extra[cam_id]["privacy_mode"]` (populated at coordinator init line 1386), so no extra request needed for the check. The camera entity `async_camera_image()` falls through to its placeholder/cached path on `None`, identical to what happened before the short-circuit. No user-visible behavior change — pure log-noise + network-call reduction.

## v10.4.2

**Two robustness fixes — Gen1 cameras only.** Diagnosed live 2026-04-27 with Innenbereich + Terrasse + Kamera (Gen1 360 Indoor) + Eingang/Garten (Gen1 Eyes Outdoor) all toggled simultaneously. **Fix 1 — `async_camera_image` no longer 500s on transient pre-warm state.** During the pre-warm window for Gen1 cams, an unhandled exception path produced HTTP 500 from HA's camera proxy. The Lovelace `<img>` element rendered the literal "500: Internal Server Error" 26-byte text body as a brown error frame on every Gen1 card — looking like cross-camera bleed even though the underlying streams were correct. Wrapped `async_camera_image` in a top-level try/except that always returns at least the placeholder 1×1 black JPEG (renamed the existing implementation to `_async_camera_image_impl`); `CancelledError` still propagates cleanly. Net effect: any future regression in the snapshot path becomes a debug log entry instead of a visible error frame. **Fix 2 — `is_stream_warming` clears stuck flags more aggressively.** Observed during the same 4-camera test: Gen1 cams stayed at `stream_status="warming_up"` with `live_rtsps=null` for >7 minutes while keepalive was already running (gen=2, 480s into session) — the existing auto-clear (added 2026-04-11) only handled the case where `_live_connections[cam_id]` was missing entirely, but not the case where the entry exists with `_connection_type` and `_bufferingTime` but no `rtspsUrl` (race in `_try_live_connection_inner` where the warming flag wasn't discarded on some exit path). Added two more clear-conditions: (a) flag set but `rtspsUrl` already populated → race, clear; (b) flag set for >300 s → hard timeout, clear. New `_stream_warming_started: dict[str, float]` tracks per-camera start time. Also unblocks privacy toggles on stuck cameras (which were previously gated on `is_stream_warming` returning False).

## v10.4.1

**Fix: stream cross-talk between two cameras streaming simultaneously.** Reproduced live 2026-04-27 with Innenbereich (Gen2 Indoor) and Terrasse (Gen2 Outdoor) both active: the dashboard would render the *same* video on both camera cards — whichever camera was toggled most recently became the source for both. The HLS playlists at HA's `/api/hls/<token>/master_playlist.m3u8` returned different tokens per camera and the `image()` snapshot endpoint returned the correct distinct frame for each — but the live HLS playback served the same content. Root cause: `_try_live_connection_inner` only invalidated the existing `cam_entity.stream` object on `is_renewal=True` (added in v10.3.10 for credential rotation). On a fresh user-toggle, a stale Stream object from a prior session could survive — `update_source(new_url)` then re-pointed it but HA's internal stream worker cache could still serve buffered segments tagged with the *previous* camera's source URL, producing the cross-camera bleed. Fix in `__init__.py`: always stop+null `cam_entity.stream` before pre-warm, regardless of `is_renewal`. Adds one cold FFmpeg start per stream-on (negligible — the pre-warm already dominates the 25–35 s activation window). User credit: hypothesis ("alte Streams nicht beendet → bei Stream-Start fixen Stream zuordnen") came directly from the live observation.

## v10.4.0

**Fix: stream health watchdog no longer triggers REMOTE fallback when no HLS consumer is connected.** Diagnosed live 2026-04-27 on Innenbereich (HOME_Eyes_Indoor, FW 9.40.25): user enabled Live Stream switch via dashboard but the Lovelace card was not actively rendering the video element (e.g. tab in background or Picture card not yet mounted), so HA's `Stream` object was never instantiated by the frontend. The v10.3.x watchdog read `cam_entity.stream` as `None` and treated that as "stream unhealthy" — it tore the LOCAL session down, restarted, hit `None` again on the next 60 s tick, and after 2 consecutive failures escalated to REMOTE. Net effect: cameras silently demoted to Cloud streaming whenever the user toggled the switch from a non-rendering context, even though LAN was perfectly reachable and the LOCAL session was up. **Root cause:** `_is_stream_healthy()` collapsed three distinct states ("no consumer yet", "healthy", "FFmpeg crashed") into a single boolean, so the absence of a consumer was indistinguishable from a real failure. **Fix in `switch.py`:** replaced with `_stream_health_state()` returning `"no_consumer" / "healthy" / "unhealthy"`. The watchdog now exits cleanly when no consumer is connected — leaves the LOCAL session up so a future browser tab gets the stream instantly. Restart-and-fallback path only triggers when a Stream object exists but isn't producing output (real FFmpeg failure). Also adds a debug log line so future false-positive cases are diagnosable. No behavior change when an HLS client is actively reading. **Knowledge base added:** `knowledge-base/` folder with `ha-stream-component.md` (HA Core Stream lifecycle + `.available` semantics), `go2rtc-races.md` (Lazy-Registration race + producer-drop), and `local-stream-failure-modes.md` (3 prioritised hypotheses for the broader class of "RTSP-OK, no frames" failures with verification tests).

## v10.3.29

**Fix: snapshot occasionally missing from motion alerts (Step 2 silently skipped).** Diagnosed live 2026-04-26 from a back-to-back pair of Innenbereich movement events: 05:13:49 received Step 1 (text) but no snapshot/clip notification, while 05:20:16 (~6 min later) sent the full text + snapshot + 4.7 MB clip sequence. Root cause in `fcm.py:614-635`: the FCM push sometimes arrives before the Bosch cloud has populated `imageUrl` on the corresponding `/v11/events` row — eventually consistent backend. The single re-fetch attempt at +5s gave up immediately when `imageUrl` was still empty, dropping Step 2 with no warning (the JPG eventually appeared ~90s later via the SMB upload path, but the Signal screenshot notification was already lost). v10.3.29 replaces the single attempt with a 3-attempt retry loop at cumulative +3 / +10 / +25 s — covers warm-cloud (succeeds on attempt 1) and slow-cloud cases (attempt 2 or 3) without delaying the common path. Adds an explicit "still empty after 3 retries" debug line so future skips are diagnosable. No behavior change when `imageUrl` was already in the FCM payload.

## v10.3.28

**Card v2.10.10 — quiet expected WebRTC race-window rejects.** Follow-up to v10.3.27. The card spammed `console.warn` on every WebRTC offer reject during the ~3 s race-window between stream-feature-flip and HA's `async_refresh_providers` wiring up the WebRTC provider. The retry loop succeeds within seconds and the user gets WebRTC anyway — but the visible warn-level noise during that window looked alarming ("Text und Logs sind komisch"). Fix: classify the rejection. The `Camera does not support WebRTC, frontend_stream_types={HLS}` message is the expected race-window response — logged at `console.debug` only. Real WebRTC failures (timeout, ICE failure, transport error) still log at `console.warn` so they're visible during diagnosis. Net effect: clean console during normal stream activation; noisy console only when something actually breaks.

## v10.3.27

**Fix: WebRTC race condition (caps stale at stream-start) + always-attempt-WebRTC card path.** Even with v10.3.24's watchdog, the card's `camera/capabilities` query at stream-start would race against HA's `async_refresh_providers` (which itself awaits `stream_source()` and runs out-of-band ~4s after `supported_features` flips to STREAM). Result: caps returned `['hls']` at the moment the card asked → card cached HLS for the whole session even though `web_rtc` would appear in caps a few seconds later. **Two-part fix:** (1) Coordinator `_ensure_go2rtc_schemes_fresh()` now does a *direct* refresh — re-fetches `_supported_schemes` on the existing `WebRTCProvider` instance via `provider._rest_client.schemes.list()` and pushes `await cam.async_refresh_providers()` to all streaming cameras. Cheaper and more reliable than a full config-entry reload, and bypasses the timing where reload happens but cam's cached `_webrtc_provider = None` from earlier doesn't get re-evaluated. Called pre-flight in `try_live_connection()` and from the post-stream watchdog as first-line recovery before falling back to the heavier reload. (2) Card v2.10.9 — drop the `frontend_stream_types.includes('web_rtc')` gate in `_startLiveVideo`. Always send the WebRTC offer; if HA's `require_webrtc_support` decorator rejects (caps haven't propagated yet, or genuine HLS-only camera), the offer fails fast in <100 ms and the existing HLS fallback kicks in unaffected. Also adds explicit pc-cleanup on WebRTC failure (was leaking a stuck-in-`have-local-offer` peer connection that confused diagnostic snippets). End-to-end verified live 2026-04-25 on Innenbereich Cloud: card v2.10.9 + `_webrtcPc.connectionState='connected'`, no HLS fallback engaged.

## v10.3.26

**Card v2.10.8 — fix: loading-overlay flicker during stream startup.** User report: "Loading erscheint 2-3 mal" — the overlay text would change rapidly between progressive messages ("Verbindung wird aufgebaut…" → "Stream wird gestartet…" → "Encoder wird aufgewärmt…" → "HLS wird geladen…") because three independent code paths (`_toggleStream` 9-message timeline, `_update()` periodic re-render, `_waitForStreamReady` polling) all called `_setLoadingOverlay()` independently — and a snapshot-load completing mid-startup would *hide* the overlay via `_onImageLoaded` only for it to reappear on the next stream-state poll, producing a visible spinner-on-off-on flicker. Fix in `_setLoadingOverlay()`: when any of `_streamConnecting` / `_waitingForStream` / `_startingLiveVideo` is active, refuse to hide the overlay (snapshot-load callbacks no longer interfere with stream-start UX), and refuse to overwrite a connecting-timeline message with the default `"Bild wird geladen…"`. Net effect: one continuous spinner with progressive text from the moment the user taps Stream until the video plays — no bounces, no message flickering.

## v10.3.25

**Fix: Bug B — Cloud (REMOTE) WebRTC cert-mismatch.** The Bosch Cloud RTSPS proxy serves session URLs on hosts like `proxy-NN.live.cbs.boschsecurity.com:443` but the TLS cert SAN list only covers `*.residential.connect.boschsecurity.com`. go2rtc's Go RTSP client (used at WebRTC offer time) refuses the mismatch with `tls: failed to verify certificate`, leaving the card stuck on HLS (~20 s Cloud delay). Until v10.3.24 the integration worked around this with a `rtspx://` rewrite at go2rtc-pre-registration time, but HA's `homeassistant/components/go2rtc:_update_stream_source` overwrites that URL with whatever `stream_source()` returns at offer time — re-introducing the cert error. v10.3.25 ports the existing LOCAL TLS-proxy approach to REMOTE: the integration starts a per-camera in-process Python TLS terminator (`verify_mode=CERT_NONE, check_hostname=False`), the cloud RTSPS bytes get unwrapped on `127.0.0.1`, and `stream_source()` returns plain `rtsp://127.0.0.1:N/<HASH>/rtsp_tunnel?...` for both LOCAL and REMOTE. Both FFmpeg (HLS path) and go2rtc (WebRTC path) consume without scheme tricks. The `rtspx://` rewrite from v10.3.21–v10.3.24 stays as fallback for the case where TLS-proxy startup fails (graceful degradation back to v10.3.24 behavior). Sub-millisecond latency penalty (in-process socket forwarding on the same host); no extra bandwidth cost (TLS tunnel terminates locally). Verified live 2026-04-25 on Innenbereich (Gen2 Indoor, REMOTE mode): WebRTC offer now returns session_id + answer + ICE candidates without cert error.

## v10.3.24

**Fix: WebRTC capability auto-recover from HA Core's stale-schemes bug.** HA's bundled go2rtc integration runs `WebRTCProvider.initialize()` exactly once at config-entry-setup, caching `_supported_schemes` from the go2rtc REST API. The bundled go2rtc binary is occasionally respawned by HA's own watchdog (`go2rtc/server.py`) when its API stops responding — the Python provider instance keeps running, but if the initial `initialize()` call ever raced and returned an empty set, the cached schemes stay empty forever. Symptom: `frontend_stream_types: ['hls']` only, no WebRTC, even though the go2rtc binary is healthy and reports rtsp/rtsps/rtspx in `/api/schemes`. Manifests as silently degraded performance — the card falls back to HLS (~8-10 s LAN, ~20 s Cloud) instead of using WebRTC (~2-3 s). Reproduced live 2026-04-25 on Innenbereich (Gen2): `attempt 1: ['hls']` → reload go2rtc entry → `attempt 2: ['web_rtc', 'hls']`. Recovery: 4 s after every successful stream activation, the integration probes `camera_capabilities.frontend_stream_types`. If `STREAM` is in `supported_features` but `WEB_RTC` is missing, the bundled go2rtc config entry is reloaded — which re-runs `provider.initialize()` and refreshes the schemes set. Throttled to one reload per hour per integration entry to avoid loops if go2rtc is actually broken. No effect on already-working installations (the check returns early when WebRTC is already advertised). Upstream HA Core issue not yet filed; reload-after-empty-init is undocumented behavior we're depending on but it works.

## v10.3.23

Three changes. **1) Fix: Gen1 Outdoor independent front-light / wallwasher control.** The Bosch Cloud `lighting_override` endpoint rejects any request that includes `frontIlluminatorIntensity` while `frontLightOn` is `false`, with HTTP 400 `frontIlluminatorIntensity must not be set if frontLightOn is false`. Our integration always sent the intensity field, so toggling **front-light off** while **wallwasher on** was silently rejected — UI showed `front=on` indefinitely until the user also turned off the wallwasher. Diagnosed live on Gen1 Outdoor (Eyes Außenkamera) on 2026-04-25 by capturing the API response body. Fix: omit `frontLightIntensity` from the PUT body when `frontLightOn` is `false`. Both directions now work independently — front-on/wall-off, front-off/wall-on, both-on, both-off all pass. Verified via 30 s observation: `after front OFF: front=off wall=on` (was `front=on wall=on` before). No behavior change on Gen2 (different endpoint structure). **2) `experimental_go2rtc_rtspx` flag removed — rtspx:// is now the unconditional default for Bosch Cloud RTSPS routing through go2rtc.** The flag was Beta in v10.3.21, default ON in v10.3.22, and after a week of testing on Gen2 Outdoor II + Gen1 Outdoor with no regressions, it graduates to permanent behavior. The option no longer appears in the integration UI. The rewrite (`rtsps://…boschsecurity.com/…` → `rtspx://…`) is required to skip TLS verification for the Bosch cert/hostname mismatch — without it go2rtc rejects the producer with `tls: failed to verify certificate`. Existing config entries with the option set are silently ignored on load. **3) README cleanup: stale OAuth migration banner removed (now ~17 months old since v8.0.5; users on the legacy client see the auto-Reconfigure flow), added an `Architecture` section with two Mermaid diagrams (component overview + LOCAL stream activation sequence + REMOTE differences) so new users can grasp the LOCAL/REMOTE/HLS/WebRTC/TLS-proxy/go2rtc topology without reading the source.**

## v10.3.22

Four bundled changes. **1) FCM push listener hardening** — the `firebase-messaging` library defaults to shutting its listener down after 3 sequential connection errors (e.g. a brief WAN blip) and does not self-restart, leaving the integration silently in "subscribed but no pushes arriving" state until the next HA restart. v10.3.22 passes `FcmPushClientConfig(abort_on_sequential_error_count=None)` so the library keeps reconnecting, and adds a watchdog in the coordinator tick that calls `FcmPushClient.is_started()` — if the listener terminates for any reason, `sensor.bosch_camera_event_detection` flips from `fcm_push` to `polling`, making silent death visible on the dashboard. Guarded by `ImportError` for older `firebase-messaging` installs. Ref: [sdb9696/firebase-messaging#33](https://github.com/sdb9696/firebase-messaging/issues/33). **2) `experimental_go2rtc_rtspx` now ON by default** (was Beta-OFF in v10.3.21). After a week of testing on Gen2 Eyes Outdoor II with no regressions, the Cloud-RTSPS → go2rtc rtspx:// path becomes the new default. Option stays available as an opt-out escape hatch; label + description updated to drop Beta wording. **3) Card v2.10.7 — loading overlay sub-hint.** The card now shows a secondary hint line under the progressive status message during stream startup: "Cloud-Stream — ca. 30–45 s bis erstes Bild, danach stabil" for REMOTE, "LAN-Stream — ca. 25–35 s bis erstes Bild" for LOCAL. Addresses user feedback that the ~30–45 s HLS initial-buffer-fill phase on Cloud streams feels broken without context — the hint sets realistic expectations. The actual stream startup time is unchanged (physics of HLS segment generation + Bosch cloud proxy first-frame latency). **4) README:** Step 3 rewritten to reflect that the Lovelace resource is auto-registered since v10.3.19 — no manual "Add resource" step needed. Added a one-line note that the old `www/bosch-camera-card.js` file in `/config/www/` is intentionally left in place on upgrade (the integration doesn't modify user files) and can be deleted manually if desired.

## v10.3.21

**Beta: route Bosch Cloud streams through go2rtc via the `rtspx://` scheme.** New Options toggle *"Beta: lower cloud stream lag (go2rtc rtspx://)"* (default OFF). **Scope:** only affects WebRTC and snapshot playback paths — HA's HLS path continues via FFmpeg-direct and is unaffected. Root cause: the Bosch cloud RTSPS proxy serves session URLs on hosts like `proxy-NN.live.cbs.boschsecurity.com` but its certificate only covers `*.residential.connect.boschsecurity.com`. When the integration registers the stream in go2rtc with `rtsps://`, go2rtc's Go RTSP client rejects the cert mismatch (`tls: failed to verify certificate`) — the registration succeeds but any WebRTC/snapshot consumer request 500s and HA silently falls back to built-in behavior. With this flag ON, the integration registers with `rtspx://` (go2rtc's documented scheme for skipping TLS verification, originally added for Ubiquiti UniFi), and the stream name is aligned with `camera.entity_id` so HA's bundled go2rtc provider (`homeassistant/components/go2rtc/`) picks up our pre-registration on WebRTC/snapshot requests. LOCAL (LAN) streams are unaffected — they go through the integration's own TLS proxy and use plain `rtsp://127.0.0.1:…`. Additional fix in the same release: `_register_go2rtc_stream` now accepts HTTP 400 with a `yaml:` body as soft-success (bundled go2rtc returns that when its in-memory stream registration succeeds but YAML persistence to `/config/go2rtc.yaml` fails — verified via `GET /api/streams?src=<name>`). Sources: [go2rtc `rtspx://` — RTSP README](https://github.com/AlexxIT/go2rtc/blob/master/internal/rtsp/README.md), [go2rtc `pkg/tcp/dial.go` — `InsecureSkipVerify` for `rtspx`](https://github.com/AlexxIT/go2rtc/blob/master/pkg/tcp/dial.go), [go2rtc #343 — insecure HTTPS client request](https://github.com/AlexxIT/go2rtc/issues/343), [go2rtc #1386 — 400 on successful POST /api/streams](https://github.com/AlexxIT/go2rtc/issues/1386).

## v10.3.20

**CI compliance:** Add `.github/workflows/validate.yml` (HACS action + Hassfest) running on push/PR/daily. `manifest.json` cleanup — drop invalid `homeassistant` key (belongs in `hacs.json`), add `http` to `dependencies` (used but undeclared), sort keys per Hassfest rule (domain, name, then alphabetical). Remove bare URLs from `data_description` fields in `strings.json` + `translations/en.json` (Hassfest disallows URLs there). No user-visible changes.

## Earlier history

For v10.3.19 and below see [GitHub Releases](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/releases).

