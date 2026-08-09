# Changelog

Full release history for the Bosch Smart Home Camera HA integration.

Newest first. The README only highlights the most recent release — for older
versions see this file or the [GitHub Releases page](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/releases) (each release page mirrors the same notes plus downloadable assets).

## [Unreleased]

## [v16.1.8] - 2026-08-08

- **Live stream endlessly flapped "Live"/"Connecting" on a remote connection without VPN, while the same camera stayed rock-solid on LAN.** Root cause: LL-HLS's blocking-reload playlist requests (`_HLS_msn`/`_HLS_part` query parameters, sent whenever a manifest advertises `CAN-BLOCK-RELOAD`) don't survive some reverse proxies/tunnels — they 404/400'd repeatedly through the reporter's Cloudflare Tunnel. An initial fix (disabling hls.js's `lowLatencyMode` for external connections) turned out not to work — confirmed live, hls.js kept sending the same requests regardless of that config flag, a known cross-version hls.js quirk (`lowLatencyMode` only changes which part index is requested, it doesn't gate whether the request is built at all). The real fix strips the LL-HLS advertisement out of every fetched manifest, via a custom playlist loader, before hls.js's parser ever sees it — verified against the actual hls.js 1.6.16 source across three independent adversarial reviews. LAN/`.local`/VPN clients are unaffected and keep real low-latency HLS. Bundled with two related hardening fixes found investigating the same flapping: the stream now backs off its automatic-reconnect delay (capped at 16s) instead of retrying every ~1s when a source genuinely never renders a frame on either transport, and a race that could otherwise start a second, untracked session (or start a stream in a backgrounded tab) during that longer reconnect window is now guarded against.
- **FCM push event handling silently errored on every event for any camera in Event-Buffered NVR mode** (`'CameraSessionState' object has no attribute 'nvr_motion_clip_blocked_warned'`, caught and logged at DEBUG level rather than surfaced). Two fields (`nvr_motion_clip_blocked_warned`, `nvr_preroll_first_segment_logged`) were wired up via the coordinator's field-view facade and read/written by `fcm.py`/`recorder.py`, but never actually added to the underlying `CameraSessionState` dataclass — every existing test for these fields used a stub coordinator that bypassed the real dataclass entirely, so nothing caught the gap. Both fields added, plus a new systemic test that reads the coordinator's actual field-view wiring and checks every field name against the real dataclass, to catch this whole class of bug going forward.
- **Mini-NVR pre-roll ring wasn't recognized as an active session consumer, so motion events could produce zero clips even with the ring writing real segments** (GitHub [#64](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/64) follow-up). Every "is a recorder using this session" check across the integration keyed on the CONTINUOUS Mini-NVR mode's process dict only — a camera in Event Buffered (Preroll) mode only ever populates a separate dict for the ring, so it was invisible to: the idle-session reaper (could tear down a healthy ring's LOCAL session, and the TLS proxy it depends on, after ~3 minutes of nobody watching the live view — only reachable via the opt-in "Green IT" idle-reap option, so this may not be every reporter's exact cause), the same-file teardown step that's supposed to cleanly stop the ring on any other teardown (privacy on, user pressed stop), and the LOCAL→REMOTE fallback transition (never stopped the ring at all, and made worse by the idle-reaper fix alone — the orphaned ring would now read as "active" and block cleanup there too). All three now recognize the ring. Also broadened the "clip not scheduled" diagnostic to WARNING (was DEBUG) and to fire even when the camera's mode silently isn't Event Buffered, not just the sub-conditions past that gate — found via a 3-agent adversarial bug-hunt that also caught the diagnostic's own coverage gap and short-circuit-evaluation bug before release.
- **Live-stream self-heal could leave a stream permanently off instead of recovering it** (GitHub multi-camera "was streaming, then stopped" report). The card's stale-source recovery (cycling the live-stream switch off then back on ~1.5s later) was always rejected by the backend's 5-second turn-on cooldown — silently, with only a WARNING logged server-side — so the very mechanism meant to recover a stale stream instead left it off for good, in the foreground or backgrounded alike. A second, compounding bug: a tab/app backgrounding right after the turn-off could throttle or drop the re-arm timer entirely, and the tab-return recovery path treated the resulting OFF switch as intentional and never revived it. Fixed by re-arming past the backend's cooldown and completing an interrupted rewarm as soon as the tab is confirmed visible again, whichever fires first. Found via a 3-agent adversarial bug-hunt after the first fix attempt only addressed the backgrounding half.
- **Mini-NVR pre-roll ring now logs a one-time confirmation once segments are actually on disk** (GitHub [#64](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/64) follow-up). A reporter's debug log kept stopping dead right after "NVR pre-roll starting" even with every prior diagnostic gap closed — likely because `/dev/shm` isn't shared between the Home Assistant Core container and the SSH & Web Terminal add-on on Supervised installs, so checking from the add-on's shell shows an empty cache dir and no ffmpeg process even when the ring is working correctly. The new log states explicitly that the path is as seen by HA Core itself, closing this class of ambiguity from the log alone.
- **Panning the Gen1 360° Indoor camera never refreshed the still image/thumbnail** — after moving the camera, the snapshot kept showing the pre-pan frame until an unrelated ~30s poll caught up, requiring a manual refresh. `async_cloud_set_pan` updated the pan-position cache and fired entity-state listeners but never touched the cached snapshot bytes. Fixed with a new `_schedule_pan_snapshot` helper (mirroring the existing privacy-mode pattern) that schedules a snapshot refresh once the pan motor has settled, using Bosch's own `estimatedTimeToCompletion` for the camera as the delay instead of a hardcoded guess. Adversarially bug-hunted (3 agents): closed a crash risk where an untrusted/non-numeric ETA from Bosch could have raised out of an already-successful pan write, and switched the scheduled task to the repo's tracked-background-task convention so it's cancelled on integration unload instead of leaking past a reload.
- **Found the real root cause of GitHub [#64](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/64)'s "pre-roll ring vanishes with zero log trace"** — even v16.1.7's new diagnostic logging (threaded through every known intentional-stop call site) still showed nothing after "starting" for one reporter. Root cause: on integration unload/reload, the teardown sequence hard-cancelled every tracked background task — including the pre-roll ring's own crash/health watcher — *before* the cooperative NVR-recorder stop path ran. That race meant neither the watcher's own "exited unexpectedly" log nor the stop path's "stopping (reason=...)" log ever fired for a ring caught mid-teardown — two independently-correct diagnostic guards, mutually exclusive in this exact window. Fixed by running the cooperative recorder stop *before* the generic background-task sweep, so the ring's ffmpeg child is always cleanly signaled while still tracked. Adversarially bug-hunted (3 agents, 2 further rounds): found and fixed two related cancellation-handling gaps the reordering itself exposed — the NVR drain-watcher's cancellation-swallowing could have masked a genuine outer shutdown cancellation, and the recorder stop call itself wasn't explicitly guarding against one — both now correctly distinguish a self-inflicted cancellation from a real one and preserve the rest of the teardown sequence either way. Also added a defense-in-depth debug log to the ring's health watcher for any future untraced cancellation path.

## [v16.1.7] - 2026-08-07

- **Mini-NVR pre-roll ring can still vanish silently after spawning, with zero further log trace** (GitHub [#64](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/64) follow-up, Lawyer82 — the `rc=234` codec-probe issue from v16.1.6 is confirmed fixed, but the ring can still disappear within ~2 minutes with no process, no cache dir, and no further log line). Traced the code: the only place that ever signals the ring's ffmpeg child is `stop_preroll_recorder()`, which was completely silent about being invoked — and the crash-respawn watcher is silent-by-design when it detects an intentional stop, so a ring killed moments after spawning left no trace of why. Added a `reason` parameter threaded through every recorder start/stop path (switch toggle, mode-select restore-race correction, LOCAL session open/renewal, LAN teardown, crash/auth-retry respawn, integration unload) with a debug log right before the ffmpeg child is signaled. Diagnostic-only change, no behavior change — next debug capture from an affected user will show exactly which caller killed the ring and why it didn't respawn.
- **`hacs.json`'s minimum Home Assistant version was pinned to whatever the maintainer's test instance happened to be running, not an actual requirement.** Re-derived the true floor from the newest HA-core APIs the integration genuinely uses (config subentries, `ai_task` structured data with attachments, native async WebRTC) and lowered the pin to `2026.7.0`.

## [v16.1.6] - 2026-08-05

- **Mini-NVR pre-roll ring's ffmpeg exited immediately with `rc=234` ("Could not find codec parameters for stream 0 (Video: h264, none): unspecified size"), producing zero cache segments** (GitHub [#64](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/64) follow-up, Lawyer82 — found after the previous stderr-drain fix in this same release let the real failure surface for the first time). ffmpeg's default 5s/5MB probe window was too small for the ring's own RTSP session to reliably capture a decodable SPS/PPS. Bumped `-analyzeduration`/`-probesize` to 10M for both the pre-roll ring and the continuous recorder (`bosch-shc-camera-client` v0.5.9). Also fixed a related gap found during the fix: the pre-roll ring always requested the full `inst=1` stream regardless of the `nvr_quality` option, unlike the continuous recorder — now wired the same way.
- **Toggling the camera light switch left the Front/Top/Bottom Light entities "frozen" in the wrong on/off state** (GitHub [#66](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/66), seti1337), self-correcting only when a motion-event push forced a fresh refetch. Root cause: `switch.*_kameralicht`'s state cache and the Front/Top/Bottom Light entities' brightness cache were two independent caches that never synced with each other on write, and neither had full protection against the coordinator's own background polls immediately reverting a fresh optimistic write with stale cloud data. Fixed across both directions (switch → light entities and light entity → switch), including the individual `switch.*_front_light` write path, with the background poll now correctly skipping only the specific write it would otherwise undo. Root-caused, fixed, and independently re-verified across two rounds of adversarial bug-hunting; every fix point has a dedicated regression test.
- **Browsing the SMB media source could hang silently for a full minute with no error if the NAS was unreachable.** `register_session`'s own 60s default connection timeout is fine for a background upload but a bad fit for an interactive browse click. Cut to 8s for browsing, with a clear "cannot reach the NAS" error instead of an opaque failure.
- **Mini-NVR pre-roll ring (and the continuous recorder) could hang completely silently, producing zero output forever** (GitHub [#64](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/64), Lawyer82). Both ffmpeg spawns piped `stderr` but nothing read it while the process ran — only after it exited. On a flaky RTSP source, enough `-loglevel warning` output fills the OS pipe buffer and ffmpeg's own `write()` to its stderr blocks forever: the process never exits, never crashes, never logs anything, and produces no output — exactly the reported symptom (debug logs showed "NVR pre-roll starting", the cache directory stayed empty forever). Fixed with a live background stderr-drain task for the lifetime of each ffmpeg process, keeping a small rolling tail for crash diagnostics instead of the old post-exit-only read. Proven against a real OS pipe deadlock (not just theorized) with a dedicated regression test, plus a 3-agent adversarial review confirming the wiring itself is covered (an earlier draft of the fix had zero test coverage on whether the drain was actually attached at the real spawn call sites — closed before merge).
- Bumped the CI test harness to Home Assistant 2026.8.0b5 (was 2026.6.0b2) — no code changes required, full suite green.

Internal — re-evaluated `smb.py` for further client-library extraction (an
earlier session had rejected it wholesale as too coordinator-coupled; this
pass went function-by-function instead). Moved the genuinely pure/stateless
pieces to `bosch-shc-camera-client` v0.5.7's new `media_transfer` module:
`is_safe_bosch_url` (was a byte-identical copy duplicated three times across
this repo — `smb.py`, `fcm.py`, `coordinator.py`; only the `smb.py` copy was
replaced this round, the other two are unchanged, flagged as a follow-up),
`sanitize_filename` (camera-name/path sanitizer), the synchronous
urllib-based `sync_http_get`/`sync_http_get_to_file`/`sync_http_get_chunked`
(Bearer-token cloud media download, the sync GET-side sibling of `cloud.py`'s
async `cloud_put_json`), and plain-FTP mechanics `ftp_connect`/`ftp_exists`/
`ftp_makedirs`. `smb.py` keeps identical public names/signatures for every
symbol other modules import (`recorder.py`/`sensor.py`/`ai_alert_store.py`
still do `from .smb import _safe_name`, etc.) — these are now thin
module-level re-exports/wrappers, not a call-site change. Deliberately left
in place: `smb_makedirs` (uses the optional `smbclient` dependency, kept out
of the library to avoid adding that dependency surface for every library
consumer) and every coordinator-coupled function (`sync_smb_upload`,
`sync_smb_cleanup`, `_fire_cleanup_alert`, `_async_cleanup_alert`,
`smb_available`, `smb_dependent_features`, `sync_local_save`, and the
`_sync_smb_upload_events`/`_sync_ftp_upload`/`_sync_ftp_cleanup`
orchestration loops), all of which read `coordinator.hass`/`.options` or
dispatch HA events/notifications directly. Zero behavior change — verified
via the full pre-existing `tests/test_smb*.py` suite passing unchanged
(6420 passed, 1 skipped, same as the pre-change baseline), plus 21 new tests
in the library repo. `ruff format --check` / `ruff check` / `mypy --strict` /
`codespell` / `pylint` clean, 100% coverage maintained in both repos.

Follow-up: closed the `is_safe_bosch_url` dedup gap for the other two copies
— `fcm.py` and `coordinator.py` now alias the same shared
`bosch_shc_camera_client.media_transfer.is_safe_bosch_url` instead of
carrying their own byte-identical copies. No version bump needed (the
function already shipped in 0.5.7). Zero behavior change.

Also re-evaluated `recorder.py` (Mini-NVR ffmpeg subprocess manager) for
the same kind of extraction. Moved a scoped, well-understood first slice to
`bosch-shc-camera-client` v0.5.8's new `mini_nvr` module: the pure ffmpeg
argv builders (`apply_quality`, `build_ffmpeg_args`,
`build_preroll_ffmpeg_args`, `create_motion_clip_args`), the pure pre-roll
ring cache filesystem mechanics (`list_preroll_segments`,
`newest_preroll_path`, `prune_preroll_cache`), and
`newest_segment_is_finalized` (one `ffprobe` subprocess call proving
moov-atom finalization). The bulk of `recorder.py` — per-camera subprocess
lifecycle, coordinator locks/caches, crash-respawn bookkeeping, and the
staging/concat orchestration around these helpers — stays in the
integration, genuinely coupled. `recorder.py` keeps identical function
names/signatures as thin wrappers (`_prune_and_count` deliberately keeps
its pruning call routed through this module's own `prune_preroll_cache`
rather than the library's internal one, since several tests patch it at
that exact point). Zero behavior change — verified via the full test suite
(6421 passed, 100% coverage, same as the pre-change baseline) plus 31 new
tests in the library repo.

Internal — round 2 of the `switch.py` `EntityDescription` pilot (round 1,
shipped in v16.1.6, collapsed 4 switches sharing the direct
PUT-endpoint/body write pattern). This round targeted `BoschCameraLightSwitch`
/`BoschFrontLightSwitch`/`BoschWallwasherSwitch` — described up front as
"structurally similar but not mechanically identical" to round 1's 4, and
that held up: these 3 (plus `BoschNotificationsSwitch`, found during the
same read-through) share a different, second pattern — their entire write
path is already delegated to a `shc.py` cloud-setter coroutine
(`async_cloud_set_camera_light`/`async_cloud_set_light_component`/
`async_cloud_set_notifications`) that owns all the real complexity
(Gen1-vs-Gen2 branching, wallwasher's dual front+topdown PUT with
brightness save/restore, the cloud→local-RCP→SHC fallback cascade); the
switch class itself was only ever a thin "read one field out of
`shc_state_cache`, call the coordinator method, warn on failure" wrapper.
That thin wrapper shape — not the underlying write logic, which is
untouched — is what collapses into a new `BoschDelegatedSwitchEntity` /
`BoschDelegatedSwitchEntityDescription` pair (distinct from round 1's
`BoschSwitchEntity`, which owns the PUT itself). `BoschNotificationsSwitch`
needed two extra description fields to fit without forcing anything: an
`is_on_transform` for its three-state (FOLLOW_CAMERA_SCHEDULE/
ON_CAMERA_SCHEDULE/ALWAYS_OFF) → bool mapping, and `require_online=False`
+ `require_field_present=True` for its cloud-only availability (the other
3 gate on camera-online instead). Every other switch touched during this
read-through (`BoschMotionLightSwitch`, `BoschAmbientLightSwitch`,
`BoschSoftLightFadingSwitch`, `BoschIntrusionDetectionSwitch`,
`BoschMotionEnabledSwitch`, `BoschRecordSoundSwitch`,
`BoschAutoFollowSwitch`, the alarm-settings/notification-type switches, …)
does its own direct read-modify-write HTTP call, refresh scheduling, or
Gen2-privacy guard and was deliberately left alone — same judgment call as
round 1. Zero behavior change: unique_id, translation_key, entity_category,
and every write call's exact (coordinator-method, args) tuple are
byte-identical before/after for all 4 switches, verified against the
pre-refactor call sites and the existing pinned tests (which patch the
coordinator methods directly, so the added indirection doesn't change what
they intercept). 6421 pytest / 100% coverage / mypy --strict / ruff /
codespell clean.

Internal — two small, mechanical style cleanups from the Platinum-structural
comparison research, no user-facing behavior change. (1) Deduplicated
`services.py`'s 15 near-identical `try/except HomeAssistantError: raise/
except Exception as err: raise HomeAssistantError(...)` error-translation
blocks (one per HTTP-call service handler — `create_rule`, `delete_rule`,
`update_rule`, `set_motion_zones`, `get_motion_zones`, `share_camera`,
`get_privacy_masks`, `set_privacy_masks`, `delete_motion_zone`,
`get_lighting_schedule`, `set_lighting_schedule`, `rename_camera`,
`invite_friend`, `list_friends`, `remove_friend`) into one shared
`guards.wrap_service_errors(action)` context manager, used as
`with wrap_service_errors("<action>"):` in place of the repeated try/except.
Same exception type, same `unexpected_error` translation key, same
placeholders — `services.py` shrank from 1994 to 1829 lines. (2) Extracted
the ~110-line inline 12-language "thanks for updating" persistent-
notification dict literal out of `__init__.py::async_setup_entry`'s body
into a new `announcements.maybe_announce_feedback_hint()` function
(matching the existing `tick_bootstrap`/`tick_failure`/`tick_housekeeping`/
`announcements` free-function pattern already used for coordinator-adjacent
notification logic in this repo) — same trigger condition, same message
content/language keys, same `entry.options["feedback_hint_version"]`
persistence. Zero behavior change — verified via the full test suite
(6420 passed, 1 skipped, 100% coverage, matching the pre-change baseline;
no tests were added, removed, or modified) plus `ruff format --check` /
`ruff check` / `mypy --strict` / `codespell` all clean.

Internal — 6-round structural cleanup of `coordinator.py` (4,585 → 3,899
lines, -15%), each round implemented in an isolated worktree, independently
re-verified (full suite + gates re-run directly, never trusting a self-report),
adversarially peer-reviewed, findings fixed, then merged. All new modules
follow the established pattern: module-level free functions taking
`coordinator` as first arg, thin delegating stubs kept on
`BoschCameraCoordinator` for API/test-pattern compatibility, cross-calls
routed through `coordinator.method_name(...)` rather than the raw module
function (preserves virtual dispatch for instance-level overrides — the
round-1 peer review's real finding, and the thing every later round had to
explicitly guard against). New modules: `quality_prefs.py` (video-quality +
Mini-NVR-mode preference getters/setters), `rcp_client.py` +
`rcp_diagnostics.py` (RCP session/read protocol + LAN diagnostic sensors),
`ai_analysis_runtime.py` (AI-analysis budget/rate/window gating),
`status_compute.py` + `maintenance_announcements.py` (pure status-derivation
split from side-effecting Repairs/Store/notification logic),
`snapshot_fetchers.py` (one deliberately small leaf — the terminal
Digest-authenticated `snap.jpg` GET — pulled out of the live/event snapshot
fetch cascade; the tiered fallback orchestration itself reads/writes
coordinator state at nearly every step and correctly stayed inline rather
than risk this integration's most business-critical path). Landed above the
original ~3,300-3,600-line floor estimate — not a shortfall: one round found
6 of its 11 planned methods were already extracted in an earlier session,
another deliberately extracted only 40 of ~700 possible lines to avoid risk
on the snapshot path. Zero behavior change anywhere — every extraction
diffed statement-by-statement against the removed inline code. 6629 pytest,
100.00% coverage across all 15,249 statements, `ruff format --check` /
`ruff check` / `mypy --strict` / `codespell` all clean. Also fixed in
passing: `quality_scale.yaml`/its test's `BRONZE_RULES` constant was
missing `docs-triggers`/`docs-conditions` (2 genuine official Bronze rules
per `home-assistant/core`'s own `hassfest` source, verified directly against
it) — both now marked `exempt` with justification.

Patch — a Home Assistant integration-quality audit (Bronze through Platinum
tiers, verified against the actual code rather than self-reported) found
and fixed 6 real gaps, plus an internal architecture cleanup that shrinks
the integration's own tree by roughly 3,600 lines with no behavior change
outside the items listed below.

### Added

- **A camera added to the Bosch account after Home Assistant is already
  running now gets its entities automatically** instead of requiring a
  manual integration reload. Applies across all 11 entity platforms
  (camera, sensor, binary_sensor, switch, light, number, select, button,
  text, image, update).
- **A camera removed from the Bosch account is now automatically cleaned
  up** — its device and all entities are removed from the registry on the
  next coordinator tick, instead of lingering indefinitely.

### Fixed

- Three services (`describe_snapshot`, `analyze_camera_ai`,
  `send_event_webhook`) were registered too late in Home Assistant's setup
  sequence to be schema-validated when the integration wasn't loaded; moved
  to the correct setup phase.
- A handful of entity icons were hardcoded in Python instead of coming from
  `icons.json`, and some internal error messages weren't fully translatable.
  Both now follow the standard Home Assistant pattern.
- Three internal HTTP call sites (a local-camera mutual-TLS request, the
  Bosch cloud-proxy RCP handshake, and the go2rtc registration client) were
  opening a fresh network connection on every single call instead of
  reusing a managed session — fixed, with connection pooling/lifecycle now
  matching how Home Assistant expects integrations to manage HTTP sessions.
- Mini-NVR "Event Buffered (Preroll)" mode's pre-roll ring writer had two
  completely silent early-returns (wrong connection type / no valid RTSP
  URL yet) with zero log trace, making a never-spawning ring
  undiagnosable from the logs alone (GitHub #64). Both now log a clear
  DEBUG reason. Also closed a real test-coverage gap: every existing
  "ring starts for event_buffered" test mocked out the spawn function
  itself, so none of them ever exercised its real body — added a
  genuine end-to-end test that drives the real code path down to a real
  `ffmpeg` argv and directory creation, which passes against current
  code and did not reveal a defect in the direct switch-toggle path.
  Investigation confirmed a genuine (but narrower than first suspected)
  race window: `coordinator.live_connections` can be mutated outside the
  per-camera NVR lock by the LOCAL/REMOTE session layer, so the crash-
  respawn and post-event-restart callers of the ring spawn (which have a
  real await gap before re-reading the connection state) can legitimately
  hit either silent branch — the direct switch-toggle path itself runs
  under one uninterrupted, lock-held stretch and is not subject to it.
  No definitive single root cause was found for the reporter's fully
  empty cache directory; the new logging is intended to pinpoint the
  exact branch on the next occurrence.

### Changed

- Internal-only: the TLS-proxy module used to bridge camera RTSP/TLS
  connections for local streaming was moved out of the integration into
  the companion `bosch-shc-camera-client` library (bumped to v0.5.5), and
  several coordinator-only responsibilities (Repairs-issue lifecycle,
  firmware-install/reset device actions, status/outage notifications) were
  split into their own modules instead of living directly on the data
  coordinator. Both changes are pure internal restructuring — no behavior
  difference for any camera, stream, or entity. Verified via full test
  suite (100% coverage, 0 regressions) plus independent adversarial review
  of every change before merge.
- Internal-only: the cloud-write request-building for privacy mode, camera
  light on/off, notifications, and pan (the exact endpoint URL + JSON body
  for each) moved into the companion `bosch-shc-camera-client` library
  (bumped to v0.5.6) as pure, stateless functions. The 401-detect +
  refresh-and-retry-once orchestration, the local-RCP/SHC fallback cascade,
  coordinator cache writes, and write-failure notifications all stay in
  `shc.py` unchanged. No behavior difference — same endpoint URLs, same
  JSON body shapes, same retry semantics, verified via the existing test
  suite (100% coverage, 0 regressions).
- Internal-only: `binary_sensor.py`, `sensor.py`, `switch.py`, and
  `number.py` were restructured to use Home Assistant's standard
  `EntityDescription` pattern (one generic entity class driven by a table
  of per-entity descriptions, instead of a hand-written subclass per
  entity) for the entities structurally simple enough to benefit —
  5 of 5 binary sensors, 24 of ~30 sensors, 4 of ~32 switches, 16 of 17
  numbers. Entities with genuinely distinct write-paths or multi-source
  logic (privacy mode, intercom, glass-break/fire-alarm, Frigate, NVR
  recording, the live-stream watchdog, motion-zone aggregation, and
  others) were deliberately left as their own classes rather than forced
  into the shared pattern. Every collapsed entity's `unique_id`,
  `translation_key`, `device_class`, `entity_category`, and (for
  switches/numbers) exact write endpoint/request body were individually
  diffed against the pre-refactor code and independently
  adversarially re-verified — no entity_ids changed, no write behavior
  changed. 100% coverage maintained throughout.

## [v16.1.5] - 2026-08-02

Patch — Mini-NVR "Event Buffered (Preroll)" mode-mismatch fixes, an FCM
push-listener crash fix for HA 2026.8 / Python 3.14, an RCP proxy SSRF
allowlist hardening, and a privacy-mode fail-closed hardening across the
snapshot fallback cascade.

### Fixed

- **A camera set to "Event Buffered (Preroll)" Mini-NVR mode could silently never start recording, with zero log output** (GitHub [#64](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/64), Lawyer82). Mini-NVR mode is chosen per camera, but the pre-roll ring's duration (`nvr_preroll_seconds`) is a single global integration option, defaulting to 0 — a user can pick "Event Buffered (Preroll)" for a camera without ever touching that separate option. `_start_recorder_locked` correctly no-ops in that case (there is nothing useful a 0-second ring could record), but did so completely silently: no log line, `/dev/shm/bosch_nvr_cache` never created, motion events still detected and shown in the activity log as normal — indistinguishable from a real bug. This is a configuration gap, not a functional defect (continuous mode is unaffected and works as designed), but the integration now logs a one-time WARNING per camera pointing at the exact option to change, instead of staying silent.
- **A camera correctly configured for "Event Buffered (Preroll)" mode (including `nvr_preroll_seconds` > 0) could still record continuously forever instead, with zero indication anything was wrong** (GitHub [#64](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/64) follow-up, Lawyer82 — reporter confirmed the preroll-seconds option was correctly set, yet the pre-roll ring never started and `/dev/shm/bosch_nvr_cache` stayed empty). Root cause: a startup platform-load race. HA forwards all entity platforms concurrently, and `switch`'s `async_added_to_hass` can restore Mini-NVR "on" intent and start the recorder before `select`'s `async_added_to_hass` has restored the per-camera mode override — at that instant `get_nvr_mode()` falls back to the global `nvr_event_only` option (default off → "continuous"), so the recorder silently starts in the wrong mode with no gate rejecting anything and nothing logged. The mode select and HA diagnostics still correctly showed "event_buffered" the whole time — only the actually-running recorder was wrong. The per-camera mode-select restore now detects this exact mismatch (serialized on the same per-camera recorder lock the spawn path itself holds, so the check can't land in the brief window between the recorder reading its mode and registering its process) and restarts the recorder into the correct mode in the background, self-correcting a bad platform-load order instead of getting stuck on it.
- **The FCM push listener crashed repeatedly with `binascii.Error: Incorrect padding` and shut itself down on HA 2026.8.0b4 / Python 3.14** (GitHub [#65](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/65), Matze89x). Root cause is a known, unfixed bug in the upstream `firebase-messaging` library ([sdb9696/firebase-messaging#40](https://github.com/sdb9696/firebase-messaging/issues/40) / [#37](https://github.com/sdb9696/firebase-messaging/pull/37), unmerged): a push message's crypto-key/salt headers can legitimately arrive without base64 padding (RFC 8291-valid), and the library's `_decrypt_raw_data()` decodes them raw, raising `binascii.Error` — not a Python 3.14 regression, just more likely to surface now. A single undecryptable message is now skipped and logged (rate-limited, since the message is redelivered on every reconnect) instead of terminating the whole FCM push client; a genuine credential-corruption error still terminates and triggers the existing self-heal recovery as before.

### Security

- **The RCP proxy host/port SSRF allowlist could be bypassed via URL-authority userinfo smuggling.** `_is_safe_bosch_host()` validated Bosch's cloud-issued RCP proxy host (used to build a request URL for live-stream session negotiation) with a naive `rsplit(":", 1)`, which for a value like `proxy.boschsecurity.com:443@attacker.example` extracts the allowlisted-looking `proxy.boschsecurity.com` — but the actual URL built from it connects to whatever follows the last `@`, per standard URL-authority syntax. Exploiting this requires the Bosch cloud API response itself to be compromised or malicious (this integration only ever authenticates and calls the real, TLS-verified Bosch API — there's no way for an external attacker to inject this value from outside), so it's a defense-in-depth hardening rather than a directly externally-exploitable bug in normal use. Fixed by validating via `urlparse()` (matching the existing `_is_safe_bosch_url` sibling function) and rejecting any `@` outright, since a legitimate Bosch value never contains one and aiohttp would otherwise turn userinfo into a Basic-Auth header sent to Bosch's real proxy. Also hardened all three copies of the URL-scheme SSRF guard (`__init__.py`/`coordinator.py`, `fcm.py`, `smb.py`) against `urlparse()` itself raising `ValueError` on malformed input (e.g. unmatched IPv6 brackets), which could otherwise propagate past a caller's narrower exception handler instead of failing closed. Backported from the same fix on the upstream Home Assistant Core submission PR (Copilot review round 18).
- **An unknown camera privacy state (e.g. a cloud-degraded restart, or a camera whose cloud payload never carries `privacyMode`) got zero protection in the snapshot fallback cascade — only a confirmed-ON state was ever gated.** The old guard was a truthy-only check; an unknown state let the full 5-tier snapshot cascade (MJPEG, live-stream proxy, cloud on-demand, LOCAL outage, cached-image, event-snapshot) run normally, and several "blind cache serve, no fetch attempt" return points scattered across every tier could then serve a stale, possibly-pre-privacy `cached_image`. Now an unknown state forces a throttled live-verification attempt, and every blind cache-serve point across all tiers fails closed (serves the placeholder) instead — including the event-snapshot last resort, which fetches a stored *historical* Bosch-cloud JPEG independent of the camera's live privacy state and so needed its own explicit gate. Also closes a timing bug where a failed verification attempt could unlock the withheld frame on the very next request within the retry window, and hardens the outer exception handler against privacy flipping ON mid-request. Backported from the same multi-round fix on the upstream Core PR, verified by 3 independent rounds of 3-agent bug-hunts across both repos.
- **The LOCAL camera host validator could accept loopback/unspecified addresses.** `_is_safe_local_camera_host()` checked `addr.is_private and not addr.is_link_local`, but Python's `ipaddress.is_private` is also true for loopback (`127.0.0.0/8`) and unspecified (`0.0.0.0`) addresses — a poisoned persisted cache or malicious cloud response could make Home Assistant connect back to itself. Same defense-in-depth framing as above: both known data sources feeding this validator (a malicious/MITM'd Bosch cloud PUT /connection response, or a locally-persisted credential cache) require a prior compromise to be attacker-controlled. Fixed by also excluding `is_loopback` and `is_unspecified`. Backported from the upstream Core PR's Copilot review round 19.

## [v16.1.4] - 2026-08-01

Patch — diagnostic logging only, no behavior change.

### Added

- **DEBUG-level diagnostic logging for movement/person FCM event timing.** A user reported that movement and person Signal push notifications arrive at nearly the same wall-clock time, even though motion detection should be much faster than person detection. Investigation found no batching/debounce bug in the integration's own dispatch code (`async_handle_fcm_push`/`build_data_and_dispatch` fire each event independently, with no artificial delay) — the working theory is that the simultaneity originates at Bosch's cloud side (person-detection AI analysis runs on the movement-triggered clip, and both FCM pushes go out once that analysis finishes), but this couldn't be confirmed without real payload timestamps. New DEBUG log lines record Bosch's own event timestamp alongside the local wall-clock receipt time, plus the previous event's own timestamp when Bosch batches two events into a single poll response, so the theory can be confirmed or refuted from real logs on the next event. No dispatch/notification behavior changed.

## [v16.1.3] - 2026-07-31

Patch — bundles fixes shipped across beta iterations (beta-1 through beta-16) before going stable. No breaking changes.

### Security

- **The LOCAL Digest-credential store and persisted snapshot JPEGs are now written with owner-only permissions.** The `.storage` file holding every camera's LOCAL Digest username/password was written with `Store`'s default mode (world-readable); it now passes `private=True`, matching HA's own credential-store convention. Persisted camera snapshot JPEGs are now `chmod 0600` after write for the same reason — `Path.write_bytes()` honors the process umask and could otherwise leave a camera image world-readable.
- **The RCP proxy host Bosch's cloud PUT /connection response hands back is now validated against the Bosch domain allowlist before being used to build any request URL**, at all three call sites that consume it (including the snap.jpg 404-retry path) — an unvalidated host there was a server-side request forgery (SSRF) path. Both event-snapshot fetch paths (`camera.py` and, in a follow-up round, the coordinator's own equivalent used by AI analysis) now disable HTTP redirects (`allow_redirects=False`): a validated `imageUrl` could otherwise still redirect to an arbitrary internal host, since `aiohttp` follows redirects by default and the existing allowlist check only validated the URL itself.

### Fixed

- **A camera's status could silently flip from a genuine `OFFLINE` back to `UNKNOWN` (and read as available again) after two inconclusive status probes in a row** — `camera_status.py` seeded each tick's working status from a bare `"UNKNOWN"` literal instead of the last known cached value, so a transient network blip on both the `/ping` and `/commissioned` fallback checks cleared `offline_since` tracking and made an actually-offline camera look reachable until the next successful poll.
- **The account's last camera being removed could leave its cached LAN IP and stale device/state behind indefinitely** — the housekeeping pass's LAN-IP snapshot persistence had the same last-camera-removed truthiness-guard gap already fixed for LOCAL Digest credentials in an earlier release; a definitively empty LAN-IP cache now persists correctly instead of silently skipping the write.
- **Token-refresh failures caused by network/DNS/timeout errors could still count toward the reauth-escalation threshold**, occasionally sending users into an unnecessary re-authentication prompt after a run of transient connectivity blips rather than a genuine invalid/expired refresh token. Network-layer failures (`aiohttp.ClientError`, not just outright timeouts) are now tracked in their own dedicated counter, completely separate from the one that drives the reauthentication flow.
- **Removing the integration entirely (not just reloading or unloading it) left every persisted `.storage` file and camera snapshot on disk indefinitely.** A new cleanup hook now deletes all four integration-owned store files (cloud-outage flag, LAN IPs, hardware versions, LOCAL Digest credentials) plus the persisted-snapshot directory when the config entry is removed.
- **Card snapshots could freeze permanently on a Gen1 camera with an active LOCAL session** (GitHub [#55](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/55), realKim-dotcom — measured on the same constrained-link Gen1 testbed as #54). Two independent causes, both in the snapshot path:
  - **`snap.jpg` was always requested at full resolution.** Every call site hardcoded `JpegSize=1206` (~500 KB measured) even though the Lovelace card asks HA for `width=315`. On a constrained link the body alone can outlast HA's 10 s `CAMERA_IMAGE_TIMEOUT`, so the preview fetch fails and the card keeps showing whatever was cached. The requested size is now derived from the width HA passes to `async_camera_image()` (new `jpeg_size_for_width()`/`with_jpeg_size()` in `const.py`): ~65 KB at `JpegSize=320` for a card-sized request, ~220 KB at 640. Callers that persist or analyse the frame — the background image refresh, the `image.*` entity, AI analysis — pass no width and keep full resolution, byte-for-byte the same URL as before.
  - **The inline LOCAL snap budget sat below what these cameras cost from cold.** `_async_camera_image_impl`'s tier-1 Digest fetch ran under `asyncio.timeout(6)`, chosen to avoid a `12 s + 10 s` cascade — but the LOCAL branch returns immediately after that attempt and deliberately skips the aiohttp fallback beneath it, so no cascade exists there and the only ceiling is HA's 10 s. Measured cold cost on Gen1: TLS handshake alone 2.5–6.9 s, ~7.05 s end to end; warm on a pooled connection, 0.05–0.79 s. A budget above the warm cost but below the cold cost can never succeed from cold, and since a handshake killed by the timeout is never pooled, cold is the only state such a camera reaches — so it failed 100 % of the time rather than occasionally. Now `LOCAL_SNAP_TIMEOUT` (8.5 s), leaving ~1.5 s of margin under HA's ceiling.
  - Reported symptom before the fix, on a camera whose Mini-NVR was in `continuous` mode (no pre-roll ring to cover for the snapshot path): 6.0 s on every request and a byte-identical JPEG each time — a permanently frozen preview. After: 7.05 s cold, then 0.61–0.79 s pooled, with different content each request; a frame fetched at 18:23:38 carried a burnt-in OSD of 18:23:37.
  - The `LOCAL_SNAP_TIMEOUT` increase alone only fixes the *first* request per camera: measured on the same hardware, even with the raised budget most successful requests still paid the full 5.2–6.0 s cold TLS-handshake cost, because a killed handshake never gets pooled — a background warm-up (real `TimeoutError` on the inline attempt schedules one Digest handshake on the same shared session with a generous 25 s budget, rate-limited to once per 30 s per camera) now banks a pooled connection so every request *after* the first lands in ~0.05–0.79 s instead.
- **The RCP `0x099e` thumbnail probe in `async_fetch_live_snapshot` could starve the `snap.jpg` fallback's share of HA's `CAMERA_IMAGE_TIMEOUT`** (GitHub [#56](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/56), realKim-dotcom). The probe had no timeout of its own and measured 2-8 s on a camera where it always failed. Now capped at 2.5 s (`TIMEOUT_RCP_099E_PROBE`) and, on a genuine bad read or timeout, memoized per camera for 1 h (`RCP_099E_PROBE_FAILURE_MEMO_SEC`) so a camera that can never satisfy the probe stops paying its cost on every fetch — cleared immediately on the next success.
- **The account's last camera being removed could leave its cached hardware version behind indefinitely** — the housekeeping pass's hardware-version snapshot persistence had the same last-camera-removed truthiness-guard gap already fixed for LAN IPs and LOCAL Digest credentials; a definitively empty hardware-version cache now persists correctly instead of silently skipping the write, which previously made `_is_gen2()` default back to Gen1 after a cold restart during a cloud outage.
- **The `auth_server_outage` Repairs issue has been removed.** Its own description told the user "retrying automatically, no action needed" — but a Repairs issue exists to prompt user action, and this one never had any, violating that design constraint. The existing WARNING log already covers the same information without the misleading "check Settings → Repairs" implication.
- **A brand-new setup or re-login could be silently accepted even when Bosch's camera API was only temporarily unreachable (rate-limited or a server-side outage) during the post-login verification check**, rather than the reachable-check being retried — `_async_verify_camera_access` previously treated a 429/5xx response the same as success (`True`), so an unverified config entry could be created and immediately fail its first coordinator refresh. It now returns `None` for that inconclusive case, distinct from `True` (verified) and `False` (definitive account-access denial), and the setup flow aborts with a new "temporarily unavailable, try again" message instead of proceeding. The equivalent 429 case in the OAuth token-refresh path (`_do_refresh`) is now treated as the same transient/backoff condition as a 5xx outage, not as an ambiguous response. A network-layer timeout/connection error during the same check is now treated identically (also `None`, not `True`) for the same consistency reason.
- **A malicious or compromised Bosch cloud response could redirect the LOCAL-session snapshot fetch to an arbitrary host.** The LOCAL camera host:port returned by `PUT /connection` was used to build a TLS-verification-disabled request URL, and cached for later outage fallback, without any validation — unlike the sibling RCP proxy host, which already had an allowlist check. It's now required to be a private LAN address (link-local addresses like the well-known 169.254.169.254 cloud-metadata SSRF target are explicitly excluded too).
- **The background image-refresh task tracked by the camera entity could be silently replaced by a harmless duplicate while a real fetch was still in flight**, on both the streaming→idle transition and the proactive-refresh-interval tick. Since entity removal cancels whatever task is currently tracked, this meant removing the integration mid-fetch could cancel the wrong (already-finished) task and leave the real network fetch running uncancelled. Both spawn sites now skip creating a new task while one is already in flight.
- **A failed thumbnail (`width=N`) snapshot fetch could suppress a following full-resolution request for up to `CLOUD_SNAP_CACHE_TTL`**, even though the shared cache was never actually refreshed — the shared `last_image_fetch` timestamp was being advanced on failure regardless of whether the request was a thumbnail or a full-resolution one, unlike the success path which already gated the write correctly.
- **A camera write that succeeded with HTTP 201 could still be reported as failed if it happened to land right after a token refresh.** `async_put_camera`'s retry-after-401 path only accepted HTTP 200/204 as success, while the initial attempt (before any refresh was needed) already accepted 200/201/204 — the retry is the identical write, not a different operation, so a definitively-successful 201 response there was incorrectly treated as a failure and surfaced as a service-call error even though Bosch had accepted the change.
- **Two background writes could keep running (and, for one of them, recreate a just-deleted file) after the integration was removed or reloaded.** The cloud-degraded startup's immediate LAN reachability ping and the cloud-outage-notified flag's disk persistence both used an untracked fire-and-forget task instead of the coordinator's own tracked-task mechanism, so a removal/reload racing either of them could leave it running against an already-torn-down coordinator — for the persistence write, that meant it could still complete after config-entry removal deleted its `.storage` file, silently recreating it. Both now go through the same tracked-task path everything else in this class already uses.
- **A third background task had the same lifecycle problem**: the LAN-reachability ping kicked when the camera list fetch returns a non-200 response also used an untracked fire-and-forget task, unlike its sibling outage-ping call sites. Now goes through the same tracked-task mechanism.
- **The advanced diagnostic `cloud_api_override` field is now re-validated at coordinator startup, not just when first typed.** The config/options flow already required it to start with `https://`, but nothing re-checked the persisted value at the point every request's Bosch bearer token actually gets attached to it — a stale or tampered value there could send the real access token to an arbitrary host. It's now required to be a recognized Bosch domain (same allowlist already used for image/video URLs), with a clear warning logged if it's ignored.
- **Two background refreshes fired after a successful motion-detection toggle or a detected privacy-mode state drift could keep running against an already-torn-down coordinator after config-entry unload**, since both used a bare fire-and-forget task instead of the coordinator's own tracked-task mechanism. Both now go through the same tracked-task path everything else in this class already uses.
- **Enabling or disabling motion detection before the coordinator had ever fetched the camera's current sensitivity (e.g. right after startup while the camera was offline) silently reset it to `HIGH`** instead of preserving whatever the user had actually configured — the write now fails with a clear error instead of guessing, so a real `LOW`/`MEDIUM` setting can never be silently overwritten.
- **A stale or previously-poisoned persisted LOCAL Digest-credential entry could bypass host validation.** The fresh-credentials path only ever caches a host already checked against the private-LAN-address allowlist, but the persisted-store restore path read `host`/`port` straight out of `.storage` with no such check — letting the outage-fallback snapshot fetch send authenticated Digest credentials to an arbitrary host. It's now validated the same way at restore time.
- **A corrupted or legacy persisted-credential `port` value could fail the entire config-entry setup**, leaving every camera unloaded, instead of discarding just that one bad record.
- **A cached LAN IP used for the fast TCP-reachability pre-check could point at an arbitrary, non-LAN address.** That cache is populated from cloud-proxied RCP data and restored unvalidated from storage — unlike the LOCAL-credentials cache, which is validated at every write site — so it had no equivalent guard before a raw TCP connection attempt. It's now required to be a private LAN address too.
- **The LOCAL-session-bootstrap snapshot fetch opened a fresh connector and TLS handshake on every call** instead of reusing the pooled Bosch cloud session the rest of the integration already shares, adding avoidable latency to every fallback attempt for cameras (e.g. `CAMERA_360`) whose REMOTE snapshot is rejected.
- **The overview card's LAN-fallback tiles (shown for a camera that's unavailable/cloud-degraded but still reachable on LAN) rendered in their own narrower grid above the main camera grid**, so an offline camera always appeared first and at a different width than the online ones — breaking the card's own tier-based sort order (live → privacy → offline). LAN-fallback tiles now share the main grid's column width and render after it. CARD_VERSION 14.1.16 → 14.1.17.
- **A camera going genuinely unavailable (not just Bosch-side "offline", but HA's own entity state) was silently dropped out of the overview card's main grid entirely**, without an explicit `include:` list — HA strips all attributes (including `brand`) on an unavailable entity, and the card's Bosch-camera filter only ever matched on `attributes.brand === "Bosch"`. The camera's real card (with its own cached-last-frame + offline-overlay treatment) was destroyed and only the bare, image-less LAN-fallback tile remained. Every entity this integration creates is named `camera.bosch_*`, matching the same convention the LAN-fallback tile logic already relies on — now used as a fallback so the camera keeps its real card instead of losing it. The last-known friendly name is also now remembered, so a camera that goes offline mid-session doesn't fall back to showing its raw entity ID. CARD_VERSION 14.1.17 → 14.1.18.
- **Follow-up to the fix above: an offline camera could now show up twice** — once as its real card (the fix above) and again as the bare LAN-fallback tile, since that panel had no way to know the main grid already covered it. The LAN-fallback panel now skips any camera already shown as a real card, and only ever kicks in for a camera the main grid genuinely doesn't cover (e.g. one left out of a restrictive `include:` list). CARD_VERSION 14.1.18 → 14.1.19.
- **Second follow-up: an unavailable camera's real card showed a misleading "Bosch Cloud token invalid — sign in again" banner**, as if the whole integration's login had failed — even when only that one camera was affected and every other camera kept working fine on the same login. A camera entity going `unavailable` is not necessarily an auth failure; it's now treated as a plain offline state (which also correctly hides the redundant privacy indicator underneath it), and the auth/re-login banner is reserved for the one case it can actually fix: a wrong or removed `camera_entity`. CARD_VERSION 14.1.19 → 14.1.20.
- **The "permanent/ambient light" switch could get stuck showing "unknown" forever with zero explanation why** — unlike every sibling camera-control switch in this file, a failed GET or PUT to the lighting/ambient endpoint (e.g. a camera model that doesn't support this feature) returned completely silently, with no log line anywhere. Now logs the HTTP status on a GET failure and reports a write failure the same way every other switch here already does.

## [v16.1.2] - 2026-07-17

Patch — bundles fixes shipped across beta iterations (beta-1/beta-2/beta-3/beta-4/beta-5/beta-6/beta-7/beta-8/beta-9/beta-10/beta-11/beta-12) before going stable. No breaking changes.

### Changed

- **`GET /v11/video_inputs` — the first, gating call of every coordinator tick — now retries once on a bare timeout before failing the whole tick** (beta-12, community report on the [simon42 forum](https://community.simon42.com/t/bosch-smart-home-kameras-vollstaendig-in-home-assistant-custom-integration-mit-live-stream-bewegungssensoren-cloud-api-kein-shc-noetig/81743/44), AndreasSchn, 2026-07-23): a brief Bosch-cloud blip (2 back-to-back failed ticks, 60s apart, self-recovered on the third) previously logged the generic "Timeout fetching camera data from Bosch cloud" and skipped an entire tick's worth of camera data over what a few seconds' grace would have absorbed — `fetch_camera_list` now retries the request once after a 3s delay (new `VIDEO_INPUTS_RETRY_DELAY_SEC` constant) on a bare `TimeoutError` specifically; a definitive HTTP error status (401/5xx) is unaffected and still fails immediately, as does a second consecutive timeout (a genuine outage still fails promptly, not masked). New `TIMEOUT_VIDEO_INPUTS` constant (15.0s, same value as before, now named). 3 new pytest regression tests pin the retry-then-recover, retry-then-fail, and non-timeout-not-retried contracts; 2 existing chaos-fault-injection tests updated to mock the new retry delay (kept them fast — no real sleep). 6352 pytest / mypy --strict / ruff / codespell clean, 100.00% coverage, deploy-verified on test HA (clean restart, 0 `bosch_shc_camera` exceptions).

- **Mini-NVR post-roll is now derived from the pre-roll ring itself instead of a second cold RTSP capture** (beta-11, GitHub #54 follow-up, realKim-dotcom, same constrained-link testbed as beta-10). `assemble_and_ship_motion_clip` used to spawn a brand-new live RTSP session (`_capture_postroll`) right after a motion event to record the post-roll tail — the reporter measured that this inherited every Gen1 transport pathology (rc=183 mid-capture, jittery-DTS slowdowns, the GitHub #52 timeout guillotine) at exactly the worst moment, and cost a session Gen1 hardware often can't spare. Post-roll now waits for the still-running pre-roll ring to record past the event and takes the newly-written ring segments as the tail — zero extra sessions, immune to connection failure since the ring is already flowing. If `nvr_finalize_ring_on_event` stopped the ring for the freshest-segment recovery, it's now restarted *before* the post-roll wait (previously only after the whole clip was built) so it's actually recording through the window. `create_motion_clip`'s hardlink-staging (GitHub #51) now covers the post-roll segments too, since they're read straight out of the live ring directory and exposed to the same prune race the pre-roll segments already were. Requires `nvr_preroll_seconds > 0` — with no ring running there's nothing to derive a tail from; that combination now logs a warning and ships pre-roll-only instead of silently doing nothing. `nvr_postroll_seconds`'s description (strings.json/en/de) updated to note the dependency; other locales carry the prior wording pending a full 12-locale translation pass. 12 pytest regression tests added/rewritten, obsolete cold-capture tests removed, `TIMEOUT_RECORDER_POSTROLL_GRACE`/`_MULTIPLIER` retired (no longer applicable). 6349 pytest / mypy --strict / ruff / codespell clean, 100.00% coverage on `recorder.py`, deploy-verified on test HA (clean restart, 0 `bosch_shc_camera` exceptions).

- **Pre-roll ring no longer runs while a camera's Mini-NVR mode is `continuous`** (beta-10, GitHub #54, realKim-dotcom — reported and measured on a bandwidth-constrained WiFi link). Previously, `_start_recorder_locked` spawned the pre-roll ring buffer unconditionally alongside the continuous recorder — but the ring's output is only ever consumed by motion-clip assembly, which is gated to `event_buffered` mode, so on a `continuous`-mode camera the ring was a second full-bandwidth ffmpeg consumer whose output nothing ever read. The reporter measured this actively degrading footage during motion events specifically (ffprobe timestamp gaps, `RTP: PT=23: bad cseq`, occasional full session collapse) — the burst of concurrent consumer spawns at event time overloads a constrained link, damaging exactly the footage the pipeline exists to capture. The ring now only runs in `event_buffered` mode; switching a camera's Mini-NVR mode back to `event_buffered` re-spawns it fresh (an accepted pre-roll-refill gap the reporter explicitly asked to trade for undamaged footage). THREE_PER_ISSUE_PER_CHANGE bug-hunt (3 agents) found one real race the initial fix missed: the ring's own crash-respawn watcher (`_watch_preroll_health`) and `restart_preroll_recorder_after_finalize` (used by motion-clip assembly) didn't re-check the camera's mode before respawning, so a mode switch to `continuous` racing a ring crash/finalize could still resurrect the ring — fixed by moving the mode gate into `_spawn_preroll_recorder_locked` itself, the single choke point all three callers share. Also closed a doc gap the bug-hunt surfaced: `nvr_preroll_seconds`'s description (strings.json, all 12 locales, README) didn't mention the `event_buffered`-only gating, unlike its sibling `nvr_postroll_seconds`. 2 new/updated pytest regression tests, 6365 pytest / mypy --strict / ruff / codespell clean, deploy-verified on test HA (clean restart, 0 `bosch_shc_camera` exceptions).

### Fixed

- **Multi-area bug-hunt sweep** (beta-9, 5 broad agents across config_flow/media_source+smb/fcm/recorder/diagnostics+repairs, none previously audited this cycle) — 6 real bugs found and fixed, each with a regression test:
  - **FCM supervisor's backoff counter never reset on a successful hard-heal** — only `soft_streak` was reset after a credential purge+re-registration, not `failures` (the backoff-delay counter), contradicting the module's own docstring ("resets to 0 after a successful push arrived"). If the freshly re-registered listener then died again for an unrelated reason (a WAN blip, not credentials) before any push arrived, the supervisor computed its retry delay off the stale, still-elevated `failures` value — up to a 30-minute wait, even though the actual root cause had just been fixed.
  - **SMB retention cleanup could delete Mini-NVR recordings early** — the generic `sync_smb_cleanup` walk covers the entire `smb_base_path` tree, which includes the NVR subtree (`{smb_base_path}/{nvr_smb_subpath}`) — that subtree has its own independent daily job with its own `nvr_retention_days` setting. A user with `smb_retention_days` shorter than `nvr_retention_days` (a reasonable, UI-exposed combination) got their NVR recordings silently deleted by the wrong retention policy. The NVR subdirectory is now excluded entirely from this walk.
  - **SMB retention cleanup was missing the socket-timeout lock** other SMB code already uses — `socket.setdefaulttimeout()` is process-global; without the same `_SOCKET_TIMEOUT_LOCK` `sync_smb_upload` holds for its identical pattern, a concurrent upload could strip this call's timeout protection mid-walk, leaving the (many blocking) directory traversal unbounded — a network blip or unresponsive share could then hang the executor thread indefinitely.
  - **Options flow discarded every in-progress edit on a single validation error** — `frigate_bind_host`/`frigate_ip_allowlist`/`webhook_url` errors caused the redisplayed form to rebuild its schema from the persisted (saved) options instead of what the user just typed, silently reverting every OTHER field across all ~50 fields/9 sections in the same submission, not just the invalid one.
  - **Diagnostics export didn't redact `webhook_url`** — the opt-in event-webhook-delivery URL (Slack/Discord incoming-webhooks, ntfy topics, HA long-lived-token webhooks) embeds a secret token in the URL path itself, leaked verbatim into every "Download diagnostics" export, the integration's own recommended path for filing a bug report.
  - **Firmware install had a check-then-act race** — the Update entity's Install button and the Repairs "Fix" action both call the same coordinator method; without a per-camera lock, a double-click or a race between the two could both read `updating=False` before either finished, sending two overlapping install PUTs to Bosch's cloud for the same camera.
  - **Daily Mini-NVR cleanup could delete the pre-created "tomorrow" staging directory**, reintroducing the exact midnight-rollover bug (`rc=254 "Failed to open segment"`) it was built to prevent — `start_recorder` deliberately pre-creates today's and tomorrow's empty staging date-dir because ffmpeg's `-strftime_mkdir` is unreliable on some bundled builds; the daily empty-directory prune pass ran on essentially the same cadence and almost always found tomorrow's dir still empty, deleting the workaround before it could ever be used.
  - Also, per an explicit due-diligence check this session: re-confirmed (live re-probe against firmware 9.40.104, decompiled-APK re-check, and an independent second research pass) that Bosch has NOT yet delivered anything from the "permanent local user"/ONVIF/RCP-write promise ("Sommer 2026") — no code changes from this, purely a status re-verification.
  - 4 new pytest regression tests + 2 extended existing ones. 6364 pytest / mypy --strict / ruff / codespell clean, 100.00% coverage.

- **Quality selector: reconnect could silently no-op or corrupt live-session state, and the section broke the overview's minimal/glanceable layout** (beta-8, live-reported by Thomas within minutes of beta-7 going out — "quality switch changes nothing on my livestream", then "my livestream is broken, I only see a picture every 2 seconds"). Root cause: `BoschVideoQualitySelect.async_select_option` calls `coordinator.try_live_connection()` to reconnect with the new quality, but didn't guard against the `STREAM_START_SKIPPED` sentinel that every OTHER call site (`camera.py`, `switch.py`) already checks for — returned when a concurrent start for the same camera is already in flight (e.g. a heartbeat renewal racing the user's quality change). A bare `if new_live:` treats the sentinel as a valid result (it's a real object, not falsy) and overwrites `coordinator.data[cam_id]["live"]` with the sentinel itself instead of a URL dict, corrupting live-session state for every other consumer that calls `.get()` on it — matching both symptoms (no visible change, then a broken stream once HA's own Stream worker hit the resulting bad state and logged "Stream ended; no additional packets"). Fixed to match the established pattern. Separately, even on a clean reconnect, go2rtc dedups WebRTC stream registrations by exact URL string, so an already-connected WebRTC viewer's PeerConnection kept decoding the OLD-quality stream — `_onQualityChange` now awaits the service call, then forces a stop+restart of the card's own live view so a fresh WebRTC offer picks up the new quality. Also: the Quality section (auto-appearing since beta-7) showed as its own always-visible row even on minimal/overview-tile cards, breaking the "controls behind ⋮, glanceable grid" design every other secondary control follows (Thomas, live screenshot) — now hidden in minimal mode and revealed in the overflow tray alongside everything else. 3 new e2e tests + 1 new pytest regression test, full suite green (6358 pytest, 100.00% coverage, 324/324 e2e across chromium/firefox/webkit).

### Added

- **Quality selector REMOTE-fallback caveat surfaced + auto-appears on every card (single-camera and overview)** (beta-7, same forum thread as beta-6's WebRTC-timeout work). While investigating whether to recommend the existing per-camera Quality selector (auto/high/low) as a further bandwidth mitigation for the WireGuard reporter, found the "Low" (~1.9 Mbps) option was silently getting clamped to ~7.5 Mbps for REMOTE/VPN sessions the whole time — Bosch's remote proxy rejects the underlying `inst=4` parameter with a 400, and `live_connection.py` has always transparently substituted `inst=2` (the same bitrate as "auto") to keep the connection working, but nothing ever told the user their "Low" pick wasn't actually taking effect. New `coordinator.get_quality_remote_fallback_active()` + a `remote_fallback_active`/`effective_bitrate_mbps` attribute on the quality select entity, surfaced in the card as a hint under the dropdown. Separately, the Quality dropdown itself was opt-in-only via an explicit `quality_entity:` YAML key with no default — unlike every sibling entity (switch/audio/light/etc.), which meant `BoschCameraOverviewCard`'s tiles never showed a Quality control at all. Now auto-derived as `select.<camera>_video_quality`, same pattern as its siblings; `quality_entity: false` opts back out. Bug-hunt (3 agents: backend correctness, card UI, real-world UX) found two more real issues fixed in the same round: `_quality_effective_inst` (the bookkeeping backing the new attribute) wasn't cleaned up on a failed connection attempt in two separate code paths, risking a stale value describing a torn-down attempt rather than any real session; and the auto-appearing dropdown initially had no working opt-out at all (`quality_entity: ""` is falsy, so it fell straight through to the new default). 5 new e2e tests + 3 new pytest regression tests, full suite green (6357 pytest, 100.00% coverage, 318/318 e2e across chromium/firefox/webkit).

- **Card: user-configurable WebRTC connect timeout + a "disable WebRTC" opt-out** (beta-6, prompted by a [forum report](https://community.home-assistant.io/t/bosch-smart-home-camera-full-ha-integration-hacs/998974/42) of slow/inconsistent stream startup over WireGuard with "WebRTC: no track within 5000ms" errors). Two new options, exposed in both the single-camera and overview card editors: `webrtc_connect_timeout_ms` (how long to wait for WebRTC before falling back to HLS, clamped 1000-30000ms) and `disable_webrtc` (an explicit, permanent opt-out that skips the WebRTC attempt entirely and always uses HLS, for setups like WireGuard where WebRTC never has a usable path at all — no timeout tuning fixes that case). The overview card's existing `card_defaults` propagation means setting either option once at the overview level applies it to every camera tile automatically. Three real bugs were found and fixed during implementation and a follow-up bug-hunt round: both cards' `setConfig` reconstruct their config object as an explicit key allowlist, and the two new keys were initially missing from both, silently dropping any configured value before it ever reached the code that reads it (caught by new e2e tests that actually exercise the config flow end-to-end rather than just pinning the source); and both editors initially displayed the raw, unclamped stored value in the timeout number input instead of the clamped effective runtime value (e.g. a hand-edited `webrtc_connect_timeout_ms: 500` in YAML showed "500" in the box even though the runtime actually used the 1000ms floor) — now both share the same clamping helper the runtime uses. A dedicated bug-hunt round also flagged that lowering the *default* timeout from the existing flat 5000ms to 4000ms for every user would shave real margin off the already-documented Cloudflare-tunnel/Nabu-Casa case (2-3s round-trip floor) without actually helping the WireGuard reporter's case (ICE either finds a path or it doesn't — a shorter timeout only fails faster, not more reliably) — the default stays 5000ms; the two new opt-in options are the actual fix for setups like theirs. 8 new e2e tests, 309/309 e2e green across chromium/firefox/webkit.

### Fixed

- **Five more real bugs found in a dedicated multi-flow bug-hunt round** (beta-5, same review as beta-4 — privacy toggle, FCM/motion-alert, Mini-NVR clip assembly, card audio registry, and token refresh flows each independently audited), plus 2 follow-up fixes to beta-4's own changes caught by re-verification:
  - **LOCAL stream health watchdog** (`switch.py`) now tracks the session generation it's watching and re-baselines instead of misattributing state across a mid-window renewal (heartbeat cred rotation, a 401-rescue reconnect); and dedups its own error recording against `handle_stream_worker_error`'s near-instant reaction to the same FFmpeg crash, which could otherwise double-count one real incident and saturate `max_stream_errors` in roughly half the configured number of genuine failures.
  - **Mini-NVR pre-roll ring double-spawn race**: `_spawn_preroll_recorder_locked` now refuses to spawn a second ffmpeg ring writer while one is already alive for that camera. `assemble_and_ship_motion_clip` releases and re-acquires the per-camera recorder lock three times during finalize (with a live, unlocked postroll capture in between), and an unrelated concurrent trigger (heartbeat renewal, a rapid switch re-toggle) could spawn its own ring in one of those gaps — the finalize/restart bracket would then spawn a *second* writer on top, leaking the first and interleaving colliding segments. Same class of bug as the earlier #44 fix, a different trigger pair not covered by it.
  - **FCM push vs. cloud poll race could re-fire an already-delivered alert**: the events poll's "nothing changed" fast path snapshots `cached_events` early, before its multi-camera fetch completes — if a concurrent FCM push advanced `cached_events`/`last_event_ids` to something newer during that window, the poll's own bookkeeping used to write the stale snapshot back, clobbering FCM's fresher data while `last_event_ids` stayed advanced. The next dispatch pass then saw an "older" event than expected and could re-fire a motion/person alert that was already delivered. The poll no longer writes back in this fast-path case.
  - **A malformed token-refresh response could mask a persistent auth failure forever**: a Keycloak HTTP 200 with a missing/empty `access_token` was being treated as a full success — persisting an empty bearer token and resetting the failure counter — instead of the failure it actually is, silently preventing the `>=3 failures → re-authenticate` escalation from ever triggering. Now matches the equivalent check that already existed at two other token-exchange call sites.
  - **Card: a secondary camera-card instance for the same camera_entity (e.g. shown on both an overview tile and a detail view) could still become audible**, defeating the multi-instance anti-echo audio registry — `_toggleAudio()`'s own anti-echo guard didn't check the secondary-instance flag on either its decoupled or shared-switch branch, and a third path, the desktop volume slider, had no guard at all.
  - Follow-up fixes to beta-4: `_showPlayGate()` now also clears `_startingLiveVideo` (matching its own header comment's promise to hide the loading spinner — a stream-stop landing mid-`_startLiveVideo()` could otherwise leave the spinner bleeding through the play gate), and `_resumeLiveStreamIfNeeded()` now checks tab visibility at every call site and re-checks it inside its own deferred-restart timers (a call arriving via `leavepictureinpicture`/`webkitpresentationmodechanged` while the tab is genuinely hidden — or a hide→show→hide race during the 500ms defer — could otherwise start/restart a stream with no hidden-teardown timer ever armed for it, running unwatched in the background indefinitely).
  - Every fix independently reviewed by dedicated bug-hunt passes (2 rounds total on the watchdog change, including a redesign after the first round found a real false-positive-escalation risk). 6345 pytest / mypy --strict / ruff / codespell clean, 291/291 e2e green across chromium/firefox/webkit, deploy-verified on real hardware (clean restart, zero exceptions, live-confirmed the new watchdog classifier against an active LOCAL session on Terrasse).

- **LOCAL stream health watchdog gave zero backend monitoring to WebRTC-only (mobile/LAN) viewers** (beta-4, prompted by a broader failover/overlay/mobile-connection review — web research + code audit across the card's WebRTC/HLS fallback and this backend LOCAL/REMOTE failover). WebRTC never touches HA's Stream/HLS component, so a WebRTC-only session left `cam_entity.stream` at `None` for its entire life — the health watchdog's "no HLS Stream object" bucket wrongly treated this as "nobody watching" and gave up after the very first check, leaving that session with no health monitoring at all. `_stream_health_state` now falls back to go2rtc's own reported consumer count when no HLS Stream object exists: a confirmed (or unconfirmed-but-not-ruled-out) consumer keeps the watchdog watching through the second check; two checks of confirmed presence clears the error counter like a healthy HLS session would. A consumer present at the first check but gone by the second, with HLS never established, is inherently ambiguous — an ordinary tab-close looks identical to a genuinely dead session — so only a single soft error is recorded, never a forced-REMOTE escalation; a first design pass over-aggressively forced REMOTE here and would have punished an ordinary tab-close with up to ~30 min of needless degraded/cloud-routed streaming, caught by an independent bug-hunt round before release. 4 new regression tests, live-verified end to end on real hardware (debug logs confirm the new consumer-based classification fires correctly against an active LOCAL session).

- **Card: legacy stream badge showed raw internal state text, and the 90s connection-timeout gave zero feedback** (beta-4, same review). The legacy (non-apple-style) stream badge dumped the raw internal state token ("offline"/"connecting"/"idle"/"streaming") straight into the DOM — not just unlocalized but not even friendly text, unlike the apple-style badge a few lines below it which already mapped these to proper text. Now uses the same mapping (all 11 locales). Separately, `_waitForStreamReady`'s 90-second connection-attempt timeout used to silently hide the loading spinner with no explanation after the expected ~10-35s wait was blown 2-3x over — it now briefly shows a "taking longer than expected, still trying…" message (new translated string, all 11 locales) before reverting to idle. Both changes independently reviewed by 3 bug-hunt passes; no issues found. 3 new e2e tests, 282/282 e2e green across chromium/firefox/webkit.

- **LOCAL camera snapshot polls contended with an in-progress live-stream pre-warm for the camera's own limited onboard webserver/TLS capacity** (beta-3, [forum report](https://community.home-assistant.io/t/bosch-smart-home-camera-full-ha-integration-hacs/998974/40), RkcCorian). Reported as the Lovelace card's stream taking far longer to reach WebRTC/MSE than viewing the same stream directly through go2rtc's own debug UI. Root cause: the ~25-35s LOCAL pre-warm window is an intentional conservative floor (a single successful RTSP DESCRIBE isn't proof the encoder is actually producing frames yet) — but during that window, the card's routine snapshot polls (`_async_camera_image_impl`'s inline LOCAL Digest fetch, and `async_trigger_image_refresh`'s LOCAL fallback, which opens its own fresh `PUT /connection`) kept firing anyway, contending with pre-warm for the camera's limited concurrent-session capacity. This produced the repeated "LOCAL snap via proxy failed" warnings visible in the reporter's logs and could add jitter to the pre-warm retries themselves. Both snapshot code paths now defer to the cached frame while `coordinator.is_stream_warming(cam_id)` is True instead of racing the live session. Three independent bug-hunt agents reviewed the fix (correctness, concurrency/test-fixtures, UX/root-cause-validity); one real gap was found and fixed in the same round (`async_trigger_image_refresh`'s LOCAL fallback lacked the same guard as the inline fetch). 4 new regression tests, all verified to fail against the pre-fix code and pass against the fix. 6329 pytest / mypy --strict / ruff / codespell clean, live-verified on test HA end to end (debug logs confirm the skip fires throughout the real pre-warm window, zero "LOCAL snap via proxy failed" warnings, zero exceptions after restart).

- **Alert snapshot/clip files could accumulate indefinitely in `www/bosch_alerts/` despite `alert_save_snapshots` being OFF** ([#53](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/53), Matze89x — reported ~2GB accumulated over a few weeks). `alert_save_snapshots`'s own description promises "if OFF, files are deleted within seconds after sending", but the actual deletion was gated on a second, independent toggle, `alert_delete_after_send`, whose own description promises the opposite when OFF ("files are kept for reference"). Turning both OFF — a combination that reads as internally consistent from each option's own text — silently defeated `alert_save_snapshots`'s promise: files were queued for cleanup but the `os.remove()` call itself never ran, so every alert's JPEG/MP4 piled up forever. `alert_save_snapshots` is now the sole authority over deletion; `alert_delete_after_send` is deprecated (all 12 locale translations updated to say so) and no longer has any effect. Confirmed unrelated to SMB upload or the folder/label sorting settings, which write to a separate `download_path`/SMB share, not `www/bosch_alerts/`. New regression test reproduces the exact reported combination and confirms deletion now happens. 6326 pytest / mypy --strict / ruff / codespell clean.

- **Card: HLS-fallback banner overlapped the camera-name pill / VERBINDE-LIVE status badge on mobile** (beta-2, Thomas — 3 live iOS Companion App screenshots over a remote/cloud tunnel connection). `.ios-hls-banner` and `.ap-top` (title pill + status badge) were both `position:absolute` at nearly the same top offset, with the banner's lower z-index rendering it directly behind the pill/badge — the "HLS-Modus, höhere Latenz" text appeared squeezed and partially illegible between "Innenbereich" and the VERBINDE/LIVE badge instead of stacking cleanly below them. Fixed by moving the banner below the title-pill row (`top:54px`, clearing the row's ~30px height + top:12px offset + gap), with the original `top:8px` restored via `:host(.no-title)` when the row itself is hidden (`show_title:false`). New e2e regression test renders the real shadow DOM and asserts the banner's bounding rect never overlaps the title-pill row's — confirmed failing pre-fix (16px) and passing post-fix (52px+) across chromium/firefox/webkit. Three independent bug-hunt agents verified layout-variant coverage (compact mode, long/translated camera names, fullscreen, offline state, legacy non-apple-style layout), CSS-cascade correctness, and user-facing concerns (accessibility, readability, locale-independence) — no blockers found. 276/276 e2e green, live-verified on test HA (clean restart, no errors).

## [v16.1.1] - 2026-07-16

Patch — bundles fixes shipped across beta iterations (beta-1/beta-2/beta-3/beta-4/beta-5/beta-6) before going stable. No breaking changes.

### Fixed

- **`event_buffered` post-roll tail could time out and never attach** (beta-1, [#52](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/52), realKim-dotcom). `nvr_postroll_seconds` opens a fresh cold RTSP capture right after a motion event, competing with the pre-roll ring/continuous recorder for the same camera — the old budget (`duration + 10s` flat) was too thin for that cold handshake plus `-c copy` output on a jittery or slow-warming stream, so the capture was killed and the clip shipped pre-roll-only (graceful, but the configured tail never landed). The timeout now scales with the configured duration (`duration × 1.5 + 10s`) instead of a flat `+10s`, giving longer post-roll windows proportionally more slack.
  - This is a first, lower-risk fix targeting the timeout-budget mechanism the reporter diagnosed. It does not address the reporter's alternate root-cause suggestion (deriving the tail from the already-warm recorder instead of a second cold RTSP session) or the separate `rc=183 … End of file` connection-drop case, which no timeout tuning can fix — shipped as a **beta** first so the reporter can confirm on their hardware whether this closes the gap before it goes stable.

- **Loading overlay could vanish and reappear with a different message mid-connect** (beta-2, Thomas, live report). Starting a live stream would show "Stream wird gestartet…", then the overlay would briefly disappear entirely, then "Bild wird geladen…" would flash, before the stream finally appeared. Root cause: `_onImageLoaded()` — fired by the periodic/background snapshot image loader, not just by the actual stream-connect handshake — unconditionally cleared the connect-in-progress state and hid the overlay whenever any fresh snapshot frame arrived, including incidental background fetches unrelated to the connect sequence. This silently killed the progressive connect-status text timeline and let the overlay hide/reappear with stale or default text. Fixed across 4 rounds (each independently bug-hunted): the snapshot loader no longer touches the connect-in-progress state or hides the overlay while any part of the connect sequence is still active (that responsibility now belongs exclusively to the real stream-started/stop/failure signals); a text-preserving guard was broadened to cover the whole connect sequence, not just its first phase; two remaining places that bypassed the overlay's own text/visibility logic were routed through it properly; and a narrow single-tick race right after tapping "start" (found during bug-hunting) was closed. 3 new regression tests, each verified to fail against the pre-fix code and pass against the fix. Tested across Chromium, Firefox, and WebKit (270 tests passed).

- **FCM alert notifications could silently fail to deliver while the log claimed "sent"** (beta-2, Thomas, live report — "Benachrichtigungen kommen nicht/verspätet aufs Handy"). `get_alert_services()` deliberately does not fall back to `alert_notify_service` for the "screenshot"/"video" alert steps — an unset `alert_notify_video` or `alert_notify_screenshot` correctly means "skip that step", by design. The bug: the code that logs each step's outcome said "sent" unconditionally regardless of whether any notify service was actually configured/called, so a genuinely-skipped video or screenshot alert looked identical in the logs to a delivered one — the exact scenario found on Thomas's own instance, where `alert_notify_video` was unset while every other `alert_notify_*` option was configured. `_notify_type()` now reports whether it actually dispatched anything, and every call site logs "sent" vs "skipped" accurately. 3 new regression tests (skip case for both screenshot and video, plus a control case confirming the working "sent" path still logs correctly).

- **Mini-NVR recorder could get permanently stuck after a mid-run crash, only fixable by a full config-entry reload** (beta-3, [#51](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/51) follow-up, realKim-dotcom — "cold-start warm-up loop ... `TLS-proxy URL not ready after 35s` repeating for hours"). Root cause: `try_live_connection_inner()` publishes a `live_connections[cam_id]` entry with `_connection_type` set but no `rtspsUrl` yet, before the ~250-line LOCAL warm-up sequence (TLS-proxy start, RTSP pre-warm, viewing-front-door bind) runs — and that sequence's exception handling only covered `TimeoutError`/`aiohttp.ClientError`. Any other exception (e.g. an `OSError` from a port-bind failure) propagated straight out, skipping every cleanup path and leaving the half-published entry stuck forever: every gate that decides whether to (re-)establish a session saw a LOCAL entry that "already exists" and never retried — matching the reporter's exact symptom (snapshot path unaffected since it doesn't touch this state, camera power-cycle didn't help since the stuck state was in HA's own memory, only a full reload cleared it). Fixed with a catch-all cleanup handler that pops the half-published entry, resets warm-up bookkeeping, and wakes any waiter immediately instead of the full timeout — with a second fix, found in the same round's mandatory bug-hunt, so this cleanup only fires when warm-up genuinely never completed: an exception from LATER bookkeeping (after a session already succeeded) now correctly returns the working session instead of tearing it down. 2 new regression tests, both verified to fail against the pre-fix code and pass against the fix; 100% coverage maintained on the touched module.

- **Proactive maintenance round** (beta-4, Thomas: "mache eine schöne Maintenance Runde") — a live stream start/stop cycle on a Gen2 Eyes Outdoor II camera plus a broad multi-agent bug-hunt sweep across the card frontend and the FCM/NVR backend surfaced five further real, previously-unreported bugs, all fixed and independently bug-hunted before shipping:
  - **Card: `_startingLiveVideo` could get stuck `true` forever**, permanently defeating every auto-recovery gate in the file (`_update()`'s auto-start gate, the 90s dead-end's own 30s retry timer, `_resumeLiveStreamIfNeeded()`). `_startLiveVideo`'s catch-block can delegate to `_waitForStreamReady()` while leaving the flag set from its own top; the 90s dead-end reset only cleared `_waitingForStream`/`_streamConnecting`, not this one. Now cleared there too.
  - **FCM: `_notify_type()` still returned "delivered" even when every configured notify service's call actually failed** (only the "zero services configured" case was fixed earlier this cycle) — the same "attempted vs. delivered" misreport class, just triggered by a live service-call failure (e.g. a briefly-unavailable mobile_app device, a Signal/Telegram outage) instead of an unset option. Now tracks real per-call success; all three step logs read "NOT delivered" (covering both causes) instead of a false "sent".
  - **Recorder: the Mini-NVR crash-respawn watchdogs (`_watch_recorder`, `_watch_preroll_health`) had no catch-all exception guard around their own respawn calls** — the same "external trigger never fires again" shape as the beta-3 fix above, just in the sibling watchdogs (ironic for `_watch_preroll_health`, which exists specifically to close that class of gap). An unexpected exception there used to kill the background watcher task silently, with no error state and no UI signal. Now caught, logged, and surfaced via `nvr_error_state` + a listener push — `_watch_preroll_health`'s existing give-up path gained the same UI-visibility fix (previously log-only).
  - **`live_connection.py`'s beta-3 fix had its own follow-up gap**: the "warm-up already succeeded, keep the session" branch never re-scheduled LOCAL auto-renewal, so a session that hit that exact path would silently never renew and go stale at the next Bosch cred rotation. Fixed to defensively re-schedule renewal — and a second bug-hunt round caught a flaw in *that* fix too: the defensive re-call minted a second, independent session-generation bump, which could desync the (opt-in, `enable_green_it`) idle-session reaper's already-captured generation and silently disable it. Fixed to reuse the current generation instead of bumping again.
  - All 6 new/updated regression tests independently verified to fail against each pre-fix code path and pass against the fix (including the two-round self-correction on the last item). 273 Playwright tests (chromium/firefox/webkit) and the full 6311-test Python suite green, 100% coverage maintained.

### Changed

- **Stream starts propagate to entities faster** (beta-5, Thomas: "can we somehow speed up the stream starting within the card?"). A fresh (non-renewal) stream toggle used to trigger a full Bosch-cloud re-poll of every camera (`coordinator.async_request_refresh()`) purely to notify entities that the just-opened session's state had changed — a real, avoidable network round-trip sitting on the stream-start critical path. The camera entity's `is_streaming`/`stream_source()` already deliberately read the in-memory session state directly (bypassing the coordinator's poll cache, per their own existing docstrings) specifically to avoid this exact dependency, so the cloud re-poll was never actually needed to propagate a fresh session — only a lighter, synchronous listener push was. Confirmed against Home Assistant core's own source that the removed call really does trigger real network I/O, not something already cheap/debounced. Independently bug-hunted by 3 parallel agents (checked for any other entity/sensor that might have depended on the removed cloud fetch, verified the change doesn't reorder anything relative to the other work scheduled right after it, and confirmed the swap is consistent with how every other call site in this repo already calls this method). 3 new regression tests, verified to fail against the pre-fix code and pass against the fix.

- **LOCAL stream start is much faster on cameras where the encoder proves itself ready early** (beta-6, Thomas: "can we faster that the camera is going to streaming?" / "min_total_wait also please i want to speed the full process up"). After the camera accepts the LOCAL connection, the integration always waited a blind, model-specific floor (`min_total_wait`: 25s Indoor / 35s Outdoor, both generations) before exposing the stream URL — because a single successful RTSP DESCRIBE (the pre-warm handshake) doesn't prove the H.264 encoder is actually producing valid frames yet, only that the RTSP/TLS session stack answered. New: a second, independent confirmation DESCRIBE a few seconds after the first success — if it also succeeds, the wait is cut to a short fixed buffer (2-3s) instead of the full floor; on any failure/timeout it falls back to the exact original blind wait, unchanged. Verified live against all 4 camera model variants on real hardware (privacy mode toggled off for testing, restored after): **Gen2 Eyes Outdoor II 35s → ~10.2s, Gen2 Eyes Indoor II 25s → ~7.4s, Gen1 Eyes Outdoor 35s → ~17.7s** confirmed-ready and shortened; **Gen1 360° Indoor** did not confirm on this run and correctly fell back to its full 25s floor with no added latency (proving the fallback path is genuinely safe, not just faster on paper) — real-world confirmation isn't guaranteed every time, but when it lands the saving is substantial. Independently bug-hunted by 3 parallel agents, which converged on the same real bug: `elapsed`/`remaining` were computed *before* the confirmation DESCRIBE, so a failed/slow confirmation attempt's own duration (up to `describe_timeout`, 5-8s) was never subtracted — silently adding extra latency on top of the floor instead of the promised "identical to pre-change behavior" fallback. Fixed by re-measuring elapsed after the confirmation call; a dedicated fake-clock regression test reproduces the exact bug (35s observed vs. the expected ≤31s) and confirms the fix. 4 new/updated regression tests, full 6324-test suite green, mypy --strict / ruff / codespell clean.

## [v16.1.0] - 2026-07-16

Minor — two bundled changes: a Mini-NVR clip-assembly race fix (GitHub #51) and a new opt-in AI Camera Analysis feature. No breaking changes.

### Fixed

- **Mini-NVR motion-clip assembly could abort entirely (`ffmpeg rc=254 … Impossible to open`)** ([#51](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/51), realKim-dotcom). A pre-roll ring segment selected for a motion clip could get pruned or rotated by a concurrent periodic watcher tick or ring respawn before ffmpeg's concat demuxer opened it, losing the whole clip. Fixed by hardlinking every selected segment into a private per-event stage directory while holding the same per-camera recorder lock the prune watcher now also acquires, so nothing can touch a segment between "selected" and "safely staged".
- **The pre-roll ring had no crash-respawn at all**, unlike the main recorder — a ring that died mid-idle (e.g. a non-monotonic-DTS abort on a flaky camera) stayed dead indefinitely until something unrelated happened to respawn it, matching the "stalled … until the recording switch was toggled" symptom from #51. Added a dedicated crash-detect/respawn watchdog mirroring the main recorder's own crash-window/backoff discipline.
- Smaller fixes found in the same pass: a `.concat.txt` temp-file leak on ffmpeg spawn-failure/timeout, a stale process handle left behind after a ring crash (misleading the `preroll_running` sensor attribute), orphaned staging directories from a hard-killed process now swept on ring spawn, and an inaccurate log message that told operators to wait on recovery paths that don't actually revive a dead ring.

### Added

- **AI Camera Analysis** (opt-in, off by default): motion-triggered structured suspicion scoring (1-10) via Home Assistant's `ai_task` integration, using its native schema-enforced `structure` output instead of free-text parsing. Sibling to the existing free-text AI Snapshot Description feature, with its own separate cooldown and daily budget so the two never compete for the same allowance. Design inspired by concepts from [HomeAssistantAICameraCentre](https://github.com/simpleaddins/HomeAssistantAICameraCentre) (MIT) — independently reimplemented from scratch against this integration's own architecture, no code copied.
  - New per-camera entities: `switch.<camera>_ai_analysis` (enable/disable), `text.<camera>_ai_scene_context` (prompt context), `sensor.<camera>_ai_alert_score`, `sensor.<camera>_ai_alerts_24h`, `binary_sensor.<camera>_ai_recent_alert`, `image.<camera>_ai_latest_alert`.
  - New `analyze_camera_ai` service for on-demand triggering.
  - New Settings section with global options (AI Task entity, snapshot count, cooldown, daily budget, retention, repeat-context window, Alarmo/alarm-panel integration) plus two new repeatable configuration types: named alert targets (each with its own score threshold, camera filter, and armed/away condition) and known visitors (free-text descriptions fed into the AI prompt to reduce false positives on people you recognize).
  - Optional alarm/siren trigger: fires a configurable Home Assistant service call when a target's score threshold is met while an alarm panel (Alarmo or any other) is armed.
  - New dedicated timeline card for browsing AI alert history with images, score badges, and per-camera filtering (added to your dashboard's resources manually — not auto-registered).
  - **Known limitation**: the configured snapshot-interval setting doesn't yet space out the frames in a burst capture — every frame in a single analysis currently resolves back-to-back. A future release may address this with a larger capture-pipeline change.

## [v16.0.1] - 2026-07-15

Patch — three real bug fixes reported by realKim-dotcom against v16.0.0, plus a small `cloud_ssl.py` session-lifecycle improvement found during HA-Core-submission-prep work. No breaking changes.

### Fixed

- **Mini-NVR never started on slower-encoder/weaker-WiFi cameras** ([#49](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/49)). The recorder's readiness wait for a fresh stream URL was a flat 12s window tuned only for Gen2 cameras, while Gen1 Outdoor's own documented pre-warm ceiling is 35s on a weak link — the recorder gave up on every coordinator tick. Redesigned as an event-driven wait (a per-camera signal set the instant the stream is genuinely ready) with the camera's own model-specific timing used only as a safety-net ceiling, not the primary mechanism — removes the underlying class of bug (two independently-drifting timing constants) rather than just widening the old one.
- **Two concurrent recorder-start attempts for the same camera could both spawn ffmpeg**, leaving two processes writing the same segment file and mutually truncating it (same report, secondary finding — pre-existing on v15.0.2 too, not a v16 regression). The recorder's stop-then-spawn sequence is now fully serialized under one lock acquisition, the same pattern already used for the pre-roll ring buffer.
- **`event_buffered` motion clips dropped 15-31 seconds of footage right over the motion event** when `nvr_finalize_ring_on_event` is enabled ([#50](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/50), verified against the camera's own on-screen clock). The pre-roll ring's "always drop the newest segment" safety rule was being applied even after the ring had already been stopped for finalization, discarding a real, complete segment for no reason — restructured the finalize step so the ring is confirmed stopped before the clip is assembled from a stable, already-correct segment list, with the ring restarted only afterward.
- **Motion-clip filenames used UTC while continuous-mode segments use local time** (cosmetic, same report), sorting a single event's files hours apart in the same dated folder. Motion-clip naming now uses local time to match.
- Shared Bosch cloud session now closes on `EVENT_HOMEASSISTANT_CLOSE` instead of the earlier `EVENT_HOMEASSISTANT_STOP`, matching Home Assistant's own `aiohttp_client` session-teardown timing — avoids tearing the session down while other integrations' stop-phase cleanup may still be running.

## [v16.0.0] - 2026-07-14

Major — HA-Core-submission preparation: TLS-proxy rewritten to native asyncio, go2rtc streams register through Home Assistant's own mechanism instead of a manual API call, OAuth ported to Core's `application_credentials` platform, `smbprotocol` made an optional dependency, and a further round of protocol/session logic extracted into the standalone `bosch-shc-camera-client` PyPI library. No breaking changes to the config schema or public entities — existing installs upgrade with zero action needed. Bumped as a major version because of the scale of the internal architecture change, matching the v15.0.0 precedent.

### Changed

- **TLS-proxy rewritten from a thread-based listener to a native `asyncio.start_server()`.** The old implementation ran each camera's TLS↔RTSP proxy on its own OS thread with raw sockets; Core reviewers push back on custom threading where the event loop already provides the primitive. The rewrite is behaviorally equivalent (same Digest-auth injection, same keepalive/pre-warm logic) but stop-while-active teardown now uses `asyncio.Server.close_clients()` before `wait_closed()` — a real hang the old code could hit under a stop-during-active-session race is now fixed, not just ported.
- **Live-stream URLs are now stable across a session instead of changing on every credential/hash rotation.** New `viewing_front_door.py` (LOCAL — reuses the existing Digest-injecting relay pattern from Frigate-endpoint support) and `remote_viewing_front_door.py` (REMOTE — a path-rewriting relay, since REMOTE has no Digest pair, only an opaque hash baked into the URL) each publish one fixed, credential-free URL per camera for the lifetime of a viewing session. `stream_source()` now returns this stable URL instead of the raw, frequently-rotating Bosch URL.
- **go2rtc streams now register via Home Assistant's own native auto-registration** instead of a manual `PUT /api/streams` call — safe now that the URL above no longer changes underneath it. The manual `DELETE` on session teardown is kept: go2rtc's own client library has no removal API at all, so this remains the only way to keep its registry from accumulating stale entries.
- **OAuth login now goes through Home Assistant Core's `application_credentials` platform** (`async_import_client_credential`) instead of the integration's own `async_register_implementation` call — the standard mechanism Core expects an OAuth2-based integration to use.
- **`smbprotocol` is now an optional dependency.** SMB-dependent media-source browsing degrades gracefully (surfaced via a Repairs issue) instead of failing integration setup if the package is unavailable.
- **Further protocol/session logic extracted to the `bosch-shc-camera-client` PyPI library**: HTTP Digest auth, local RCP reads, the full RCP protocol/session layer (including all 6 wire-format parsers), the `async_update_rcp_data` cache-fetch orchestration (now a pure `fetch_rcp_camera_data()` function returning a dataclass), and the shared HTTP-PUT mechanics (headers/timeout/status-classification/JSON-parsing boilerplate) used by all 5 cloud setters (`privacy_mode`/`camera_light`/`light_component`/`notifications`/`pan`). The fallback-tier selection, coordinator cache writes, notification side effects, and privacy mode's 401-retry-with-token-refresh orchestration stay in the integration — only the mechanical, HA-independent wire logic moved. Net result across this extraction round: the HACS/Core integration tree is roughly 6500 lines smaller.

### Fixed

- **`_connection_type` was never actually set to `"REMOTE"` anywhere in the codebase** — only `"LOCAL"` was ever assigned. Three pre-existing call sites that gate on `_connection_type == "REMOTE"` (`session_renewal.promote_to_local` — the REMOTE→LOCAL live-recovery path, part of `remote_session_terminator`'s own check, and an RCP-read REMOTE branch) were silently unreachable in every prior release. Found and fixed while wiring the new REMOTE front-door, which needed this field for its own resolve logic. This may be directly related to the still-open #47 report (AUTO stream mode never recovering LOCAL after a camera's DHCP-leased LAN IP changes) — not claimed as a fix for that issue, since the originally-suspected stale-TCP-pre-check-IP root cause may be a separate, additional layer; flagged for a dedicated follow-up investigation.
- **REMOTE session setup could silently discard an already-working proxied stream.** The new REMOTE front-door's own start call originally shared an exception handler with the surrounding TLS-proxy setup, so an unrelated exception from the front-door could wrongly downgrade a working stream to the raw, cert-failing cloud URL. Split into its own narrow exception scope.

Every change went through an independent 3-agent bug-hunt before being committed (THREE_PER_ISSUE_PER_CHANGE) — real issues found and fixed along the way, beyond the two above: a heartbeat cred-refresh path that was rebuilding the published front-door URL with raw credentials on every rotation (leaking the exact churn the front-door exists to prevent); a fresh-install gap in the `application_credentials` flow. Live-verified end to end on real hardware (Terrassenkamera): full LOCAL RTSP session through the new front-door (TLS handshake, Digest auth, SETUP video+audio, PLAY, GET_PARAMETER keepalives), native go2rtc registration working without the removed manual PUT call, and clean teardown (proxy server closed promptly, go2rtc stream unregistered).

5939 pytest, 100.00% coverage, mypy --strict / ruff / pylint / codespell clean.

## [v15.0.2] - 2026-07-14

Patch — iOS PiP-after-native-fullscreen fix, LAN-IP staleness recovery (#47), i18n completion (#45)

### Fixed

- **iOS Picture-in-Picture hangs permanently after native fullscreen.** The `ownsPip` guard that protects live-recovery teardown from tearing PiP's compositor link never checked `video.webkitPresentationMode === "fullscreen"` — a third, separate WebKit presentation state iOS can enter on its own. Factored into a shared `_ownsNativePresentation()` helper covering both cases, applied at all 5 call sites.
- **AUTO-mode LOCAL stream never recovers after a camera's LAN IP changes** (#47, realKim-dotcom). Once a camera's cached LAN IP goes stale (DHCP re-lease after a mesh flap/reboot), the TCP pre-check failed against the dead address forever. Now, at most once every 10 minutes per camera, a failing pre-check is ignored and LOCAL is attempted for real — the existing pre-warm fallback still demotes to REMOTE gracefully if the camera really is unreachable.
- **Orphaned Mini-NVR ffmpeg processes on integration unload** (same #47 report). A recorder spawn still in flight when HA stops/reloads could leak an untracked ffmpeg process. Fixed by sweeping every configured camera under the existing per-camera lock, plus a shutdown flag both spawn paths check.
- **Completed the card i18n pass from #45.** A follow-up scan found more hardcoded German: the tap-to-play overlay, LAN-fallback tile buttons, three date/time formatters hardcoded to `de-DE` regardless of the configured language, the rules list, and the entire "Create rule" dialog. All now respect `hass.language` across all 11 supported locales.

CARD_VERSION 14.1.7 → 14.1.8. 6136 pytest, 261 e2e (chromium/firefox/webkit), mypy --strict / ruff / codespell clean.

## [v15.0.1] - 2026-07-13

Patch — internal cleanup: Session-State-Facade migration complete (Slice 3 + Slice 4), no user-facing change

### Changed

- **Internal only.** Finished consolidating the coordinator's per-camera state (started in v15.0.0) into the single `CameraSessionState` facade. Slice 3 folded the live-connection session dict and the "user explicitly wants this stream" flag onto the facade; Slice 4 — the plan's highest-risk step, since it moves the coordinator's per-camera `asyncio.Lock` instances themselves (stream, Mini-NVR recorder, snapshot fetch, go2rtc re-registration, NVR clip assembly, and a sixth found during a systematic re-audit: fresh-event-snapshot coalescing) — moved all six onto the facade too, reusing the existing view abstraction rather than adding a new one.
- Lock identity (two different lock objects are never interchangeable even both unlocked) was verified before any production call site was switched over, including a real two-coroutine mutual-exclusion test performed while a lock is held.
- Every slice went through an independent 3-agent bug-hunt (call-site completeness across the whole package, TOCTOU + purge-safety analysis, semantic equivalence + test-fixture correctness) before being committed; the only real finding was a test-coverage gap in the new lock-identity test class, fixed in the same commit.

6124 pytest (1 pre-existing skip), 100.00% coverage, mypy --strict / ruff / codespell clean.

## [v15.0.0] - 2026-07-13

Major — a large internal performance/stability refactoring round, one new card feature, and the start of a deeper structural cleanup of the coordinator. No breaking changes to the config schema or public entities; existing installs upgrade with zero action needed. Bumped as a major version because of the scale of the internal architecture change (coordinator split into 4 new modules, first slice of a per-camera state consolidation), not because anything public changed.

### Added

- **Fullscreen auto-hide controls**: the bottom pill-bar (live/audio/PiP/etc. buttons) now fades out after 10s of no mouse movement/touch while in fullscreen, and reappears instantly on activity — standard video-player UX. New card option `fullscreen_auto_hide_controls` (default `true`) lets you opt back out to the always-visible behavior. Desktop uses throttled pointer-move tracking; touch devices show-on-tap. Outside fullscreen, nothing changes.

### Performance

- Pooled `aiohttp` sessions instead of a fresh `ClientSession` (+ TCP/TLS handshake) per call for the live-connection setup path, the three go2rtc API calls, and the per-camera RCP slow-tier fetch.
- Capped the snapshot fallback cascade's final timeout from 20s to 10s.
- Removed a per-tick task-spawn for the (almost-always-no-op) cloud-state announce check.
- CI test suite parallelized (`pytest-xdist`) — 3.3x faster.
- Card: dead-track watchdog and stall checker now share a cached `getStats()` snapshot instead of polling independently.

### Stability

- Bounded the token-refresh lock's total hold time to 15s — a hanging Keycloak response could previously block every 401-recovery path for much longer.
- Coalesced go2rtc stream re-registration on LOCAL credential-rotation heartbeats — concurrent heartbeats no longer risk two overlapping registrations for the same stream.
- Background tasks spawned from event/tick/status handlers are now tracked and properly cancelled on integration unload/HA stop instead of leaking.
- Fixed a `socket.setdefaulttimeout()` race in the SMB upload path (this call is process-global, not thread-local — concurrent camera uploads could silently strip each other's timeout protection).
- Bounded deadline added to the SMB/FTP NVR-cleanup directory walk.
- Frigate RTSP front-door sockets now properly drain on close instead of just disconnecting.
- Stale live-stream teardown timeouts are now tracked per-camera with diagnostic logging.
- Media Source SMB browsing now reuses one session per browse step instead of reconnecting repeatedly; clip-streaming chunk size increased.
- Per-camera coordinator state is now purged when a camera is removed/replaced, instead of growing unbounded across the integration's lifetime.
- Config-entry migration across multiple versions now writes once instead of once per version step.
- Webhook URL and Frigate IP-allowlist config fields now validate their input and report specifically what's wrong.
- **Found by a new chaos-engineering fault-injection test suite** (20 tests simulating cloud-API errors, connection resets, credential races, and more): a malformed-but-200 cloud API response on one of three slow-tier diagnostic endpoints could have crashed the entire coordinator update cycle with an unhandled exception. Fixed with the same defensive check every neighboring endpoint already had.

### Structure

- Extracted the stream/session lifecycle logic out of the ~6700-line main module into four focused files (`stream_lifecycle.py`, `session_renewal.py`, `go2rtc_client.py`, `tls_proxy_wiring.py`) — pure reorganization, no behavior change.
- Began consolidating the coordinator's ~90 scattered per-camera data structures into a single per-camera state object — done in reviewable slices rather than one large rewrite; roughly half are migrated so far, the rest is tracked as an ongoing internal cleanup.

Every change above went through an independent adversarial review pass before being committed, which is how the SMB timeout race, a go2rtc-session teardown race, two fullscreen-feature edge cases, and the chaos-test-suite bug above were all caught and fixed pre-release rather than post-release.

6056 automated tests (1 pre-existing skip) / mypy --strict / ruff / codespell clean, 255+ browser (e2e) tests green across Chromium/Firefox/WebKit. Deploy-verified twice on a live test instance with zero integration-related errors after restart, plus a live stream start/stop cycle on real hardware confirming the new module structure and pooled sessions work end-to-end.

## [v14.8.0] - 2026-07-12

Minor — two feature requests from realKim-dotcom's `#43` follow-up feedback on v14.7.1, both opt-in and backward compatible. Also bundles the card i18n fix already sitting on `main` (v14.1.6, see below).

### Added

- **Opt-in "recover freshest pre-roll segment" mode** (`nvr_finalize_ring_on_event` option, default off). Normally the ring's actively-written newest segment is always dropped before assembling an FCM-triggered clip, since it may not have a finalized moov atom yet — safe, but it can cost the ~0-10s of footage closest to the actual trigger moment, which is often the most valuable part of the clip. When enabled, the ring is briefly stop-finalized (SIGTERM, confirmed clean exit) and immediately restarted before the newest-segment cutoff is applied, so that segment can be safely re-attached instead of discarded — at the cost of a small (~1s) gap in ring coverage on every event. Mirrors the approach realKim-dotcom's own fork already uses.
- **Per-camera opt-out of the native FCM-triggered clip** — new `Mini-NVR event clip` switch (default ON, hidden by default like the existing recording switch). Installs that orchestrate their own clip-saving externally (e.g. HA automations flipping the camera to `continuous` for the whole motion window) can turn this off per camera so the integration doesn't also produce its own shorter native clip on every event. Turning it off only skips the native clip assembly — the underlying pre-roll ring buffer keeps running unaffected for other consumers.

A 3-agent bug-hunt (THREE_PER_ISSUE_PER_CHANGE) on the new code found and fixed two real issues before release: the finalized segment was initially returned from inside the ring's own cache directory, which let `list_preroll_files()`'s normal scan pick it back up and concatenate it into the clip a second time once newer segments made it no longer "the newest" (now relocated to a dedicated directory before being handed back, mirroring the fix already applied to the post-roll capture file); and the new opt-out switch's restore-on-restart logic wrote `False` for any non-"on" persisted state, including HA's own "unavailable" placeholder — silently disabling a feature that defaults to enabled after a coordinator hiccup at shutdown (now only acts on an explicit "on"/"off").

5964+ pytest / mypy --strict / ruff / codespell clean.

## Card v14.1.6 — 2026-07-12

Patch — card i18n regression fixed (GitHub #45, realKim-dotcom): ~90 static UI labels (accordion headers, light/notification/diagnostics controls, service-row buttons, tooltips/aria-labels) were hardcoded German and ignored `hass.language`, even though the card ships a working `_t()`/`CARD_I18N` i18n system with 11 locale blocks. Re-keyed every string the reporter listed (plus several more found in the same investigation) through `_t()`, adding 81 new keys translated across all 11 locales (de/en/es/fr/it/nl/pl/pt/ru/uk/zh-Hans).

Found and fixed two adjacent bugs in the same code while there:
- The quality-select dropdown's `<option value="...">` attributes were the German display text (`"Hoch (30 Mbps)"` etc.), but the real backend HA select entity (`select.py`, `BoschVideoQualitySelect`) has canonical options `["auto","high","low"]` — a bug-hunt agent confirmed this made the dropdown non-functional end-to-end (HA core's `select.select_option` validates against the entity's declared options before the integration ever sees the call, so the old German-text value would have been rejected outright). Fixed by using the canonical values while keeping the visible text localized.
- The Apple-style stream pill's `title` attribute was re-set to hardcoded German on every stream state change (`isStreaming ? "Live-Stream stoppen" : "Live-Stream starten"`), silently overwriting the already-localized title `_render()` set on mount — meaning any non-German user saw German the moment the stream state first changed after page load. New regression test pins this across three browsers.

A 3-agent bug-hunt (THREE_PER_ISSUE_PER_CHANGE) on the diff found no hard bugs in the key-wiring itself (zero missing/typo'd keys across 93 call sites × 11 locales, zero duplicate keys, all placeholder tokens intact) but caught two real completeness gaps against the reporter's own list: the dynamic stream-pill title (fixed, see above) and the "Regel erstellen" create-rule dialog itself, which is deliberately scoped OUT as a documented follow-up (see TODO) since it's a separate dynamic-modal surface, not a `_render()` template label.

234/234 Playwright e2e tests green (chromium/firefox/webkit), 0 new eslint/stylelint errors, 5964 pytest / mypy --strict / ruff / codespell clean.

## [v14.7.1] - 2026-07-12

Patch — `event_buffered` Mini-NVR mode now actually assembles and ships motion clips, plus two correctness fixes for the pre-roll ring the feature reads from.

### Fixed

- **`event_buffered` mode now produces recordings** (#43 follow-up, realKim-dotcom). v14.7.0 added the per-camera mode select, but `event_buffered` only ever ran the pre-roll ring buffer — `create_motion_clip()` existed but had zero call sites anywhere in the integration, contradicting the README's own description. A movement/person FCM event for a camera in `event_buffered` mode with the NVR switch on and LOCAL now assembles the pre-roll ring (plus an optional new `nvr_postroll_seconds` live-captured window) into a clip and drops it into the existing NVR staging tree, where it ships exactly like a continuous-mode segment (local/SMB/FTP).
- `mini_nvr_state`'s `preroll_running`/`preroll_segments` attributes now refresh immediately when the pre-roll ring spawns, instead of lagging until the next coordinator tick.
- Leftover pre-roll ring segments are cleaned up on a genuine stop — previously they lingered in tmpfs until the next start happened to overwrite them. A new internal distinction between a genuine stop and an internal respawn (LOCAL-session/cred-rotation renewal) means this cleanup does NOT fire on every renewal, which would otherwise wipe the ring's accumulated context far more often than intended.
- The ring writer's ffmpeg process keeps exactly one file open at a time, so the newest segment on disk can still be mid-write with no finalized moov atom — concatenating it could produce a corrupt/failing clip. The pre-roll segment list now always excludes the newest segment before handing it to the concat step (costs at most ~10s of the freshest footage, never risks a corrupt clip). This is the exact race realKim-dotcom's own local patch for #43 independently discovered and had to work around.
- **#44** (realKim-dotcom): `start_preroll_recorder()` was unserialized, unlike the main recorder spawn — concurrent callers (switch turn-on, the stream-up hook, the NVR mode select) could each pass the leading stop-then-spawn sequence and leak a second untracked ffmpeg ring writer that interleaves segments with the first. Now serialized on the same per-camera lock the main recorder spawn already uses.

### Added

- New `nvr_postroll_seconds` option (0-60 seconds, default 0) — records this many extra seconds live after a motion event and appends them to the pre-roll clip. 12-locale translated.

A 3-agent bug-hunt pass (THREE_PER_ISSUE_PER_CHANGE) on the new clip-assembly code found and fixed three further issues before release: the post-roll capture was written into the same directory the pre-roll ring scans (duplicating it in the assembled clip), a failed post-roll capture leaked its partial file, and same-second motion events could collide on the output filename.

5964 pytest (1 pre-existing skip) / mypy --strict / ruff / codespell clean, 100% coverage.

## [v14.7.0] - 2026-07-11

Minor — new per-camera Mini-NVR mode select entity: a mixed camera fleet can now run different recording strategies per camera instead of one global setting.

### Added

- **Per-camera Mini-NVR mode override** (closes the core of #43, realKim-dotcom) — new `select.*_nvr_mode` entity per camera (`continuous` / `event_buffered`), falling back to the existing global `nvr_event_only` option when left untouched (zero change for existing installs). Use case: a glass-facing camera whose PIR can't fire through glass can stay on always-on recording while other cameras run the lightweight event-buffered pre-roll ring, in the same install. Gated behind the existing "Enable NVR" option, disabled by default (enable it in Settings → Devices → your camera's disabled entities).
- **Scope, stated plainly**: "Continuous" is today's existing always-on recording, not alarm-armed-gated recording — this integration doesn't have an alarm-aware recording gate yet. A per-camera override for the pre-roll buffer *size* (`nvr_preroll_seconds`) is not included either; all event-buffered cameras still share one global value. Both are reasonable candidates for a follow-up, tracked against #43 rather than closing it outright.
- Changing the mode now restarts an already-running recorder for that camera immediately, so the new setting doesn't sit unused until some unrelated event happens to respawn it.

24 new tests (coordinator methods, entity incl. restore-on-restart, 3 integration tests proving two cameras on one coordinator resolve independently — the actual mixed-fleet scenario the feature exists for). 5916 pytest (1 pre-existing skip) / mypy --strict / ruff / pylint / codespell clean, 100% coverage.

## [v14.6.0] - 2026-07-11

Minor — new `set_lighting_schedule` service: LED lighting schedules (on/off time, motion trigger, darkness threshold) can now be written from Home Assistant, not just read.

### Added

- **`set_lighting_schedule` service** — sets the LED lighting schedule for outdoor cameras with LED light (on time, off time, light-on-motion, darkness threshold; any field left out keeps its current value). The integration only ever had a read-only `get_lighting_schedule` service, even though Bosch's cloud API supports `PUT /v11/video_inputs/{id}/lighting_options` — the sibling Python CLI tool already proves this endpoint is writable. Flagged as a gap during the 2026-07-11 cross-product feature-parity resync. Implementation mirrors the CLI's GET-merge-PUT pattern and reuses the same conventions as the existing `set_privacy_masks`/`set_motion_zones` write handlers.
- The new handler's write also optimistically updates `_lighting_options_cache` and stamps a write-lock timestamp (`_lighting_options_set_at`), matching the pattern already used for `_privacy_sound_cache`/`_ledlights_cache` — found and fixed via a 3-agent bug-hunt pass on the new code before release (all 3 independently found the same gap: without it, `get_lighting_schedule` could serve stale pre-write data for up to a full slow-tier poll interval right after a successful write).

5890 pytest (1 pre-existing skip) / mypy --strict / ruff / codespell / pylint clean, deploy-verified on test HA: the endpoint is Gen1-outdoor-only (confirmed live — the existing pre-write `lighting_options` endpoint also `sh:hardware.not.supported`s on both cameras currently on this account, Gen2 Eyes Outdoor II and Gen1 360° Indoor, neither eligible), so the real 442 rejection was live-confirmed against Bosch's cloud API and the new handler surfaces it as a clean `HomeAssistantError` (no crash) — but the write-success (PUT 200) path could not be live-verified end to end for lack of an eligible Gen1 Eyes Outdoor camera on this account.

## [v14.5.13] - 2026-07-11

Patch — internal cleanup: coordinator split into mixins (continuing v14.5.7-v14.5.10's rewrite), no user-facing change

### Changed

- **Internal only.** `__init__.py`'s `BoschCameraCoordinator` class was 9385 lines before this release's work started — it's now 6636 (-29%). Four cohesive method groups were split off into mixin classes, each co-located with the module it already delegated to where one existed: FCM push glue → `fcm.py`, Frigate/external-recorder front-door management → `frigate_endpoint.py`, SHC local API/cloud-setter glue → `shc.py`, and — new this release — the full bearer-token/OAuth lifecycle (read/refresh/proactive-renewal, Keycloak retry + outage backoff, reauth-flow triggering) → a new `token_auth.py`. Two more chunks were moved out as standalone functions rather than mixins: the ~20 HA service-call handlers (trigger_snapshot, rules CRUD, motion zones, privacy masks, camera sharing, …) → `services.py`, and the single largest, most complex method in the coordinator — opening a live RTSP/WebRTC session — → `live_connection.py`. A small duplicated write-pattern in `switch.py` (4 near-identical boolean toggles) was also consolidated into one shared helper.
- Every extraction was independently verified: full pytest suite green after each step, 100% coverage maintained throughout, and — for the four mixins plus the two larger extractions — 3 independent bug-hunt passes per change (self→coordinator substitution correctness, dangling-reference checks across the whole repo, cross-mixin collision checks, and for the token/auth move specifically, line-by-line confirmation that none of the retry counts/backoff formulas/failure thresholds drifted). Also fixed 8 pre-existing tests that were passing but wouldn't actually have caught a regression in the branch they claimed to guard (found during the same review pass).
- Live-verified end to end on test HA after deploying all of it together: real WebRTC session open, live snapshot fetch, a full privacy-mode on/off round trip, and the `trigger_snapshot` service call — all against the real Terrasse camera, zero exceptions.

5883 tests (1 pre-existing skip) / mypy --strict / ruff / codespell green, 100% coverage.

## [v14.5.12] - 2026-07-11

Patch — first-time setup: the native camera view no longer stays black until the "Live Stream" switch is toggled by hand; README gets a Quick Start guide

### Fixed

- **A camera that never had its "Live Stream" switch manually turned on showed no video at all in Home Assistant's native camera view (more-info dialog, Companion app)** — reported by a new user on the community forum right after finishing setup. Opening the camera via Cast or the built-in HLS card already auto-opened a live connection on demand; the native WebRTC path (used by HA's own more-info dialog and the Companion app) did not, so `go2rtc` had nothing to stream and raised "Camera does not support WebRTC" with no obvious error surfaced to the user. The native WebRTC path now auto-opens a live connection the same way the Cast/HLS path already did, so video starts automatically the first time a camera is opened, without ever needing to find and flip a switch.

### Documentation

- **README now leads with a short "Quick Start" section** covering only the handful of steps actually required for a working camera — install, log in, add a card. The ~50 options under "Configure" (notifications, SMB upload, AI descriptions, external recorders, …) are clearly optional and can be skipped entirely on first setup. A new "I don't see any image / video" troubleshooting checklist was also added under Setup.

5882 tests (1 pre-existing skip) / mypy --strict / ruff / codespell green, 100% coverage on the touched file, deploy-verified on test HA.

## [v14.5.11] - 2026-07-10

Patch — Mini-NVR "recording"/"idle" sensor no longer lags behind reality

### Fixed

- **The Mini-NVR state sensor (`mini_nvr_state`) could show "idle" for up to ~20 seconds after recording had actually started, or "recording" for 1-2 minutes after it had actually stopped.** Found by realKim-dotcom while re-verifying the issue #42 fix. The sensor already read the correct underlying state directly (no caching), but nothing told Home Assistant to refresh it the moment recording actually started or stopped — it only updated on the next routine ~60-second background check. It now refreshes immediately at every real state change: start, stop, an unexpected crash, and each of the three "giving up" cases (disk full, repeated authentication failures, crashed twice in a row).

5877 tests / mypy --strict / ruff / codespell green, 100% coverage, deploy-verified on test HA.

## [v14.5.10] - 2026-07-10

Patch — internal cleanup continued (coordinator rewrite, Phase 2), no user-facing change

### Changed

- **Internal only**: the coordinator's ~10,000-line `__init__.py` had its per-tick polling method (`_async_update_data`, the single largest piece of the file) split into 8 focused modules — camera-list fetch, tick bootstrap, status polling, event polling, event dispatch, post-tick housekeeping, and the per-camera slow-tier diagnostic pass (itself further split into 4 sub-pieces: per-camera context, info-cache updates, pan/lighting control, and the ~20-endpoint slow-tier fetch). Each extraction step preserved behavior exactly — no functional change — and was independently verified by a dedicated review pass plus a full regression-test run before merging; the highest-risk piece (the slow-tier endpoint fetch) additionally got a thorough endpoint-by-endpoint review and a live health check on test hardware spanning a full slow-tier poll cycle. Continuation of the internal cleanup started in v14.5.7–v14.5.9.

5870 tests / mypy --strict / ruff / codespell green, 100% coverage, deploy-verified on test HA across all 8 extraction steps.

## [v14.5.9] - 2026-07-10

Patch — "Download Diagnostics" no longer crashes; internal cleanup continued

### Fixed

- **The "Download Diagnostics" button in Settings → Devices & Services could fail** due to an internal refactor in the previous two releases leaving one piece of bookkeeping unable to report its count. Found and fixed during this release's own internal review, before it shipped to most users — see the version history for detail if curious.

### Changed

- **Internal only**: two more per-camera bookkeeping structures used for the live-stream warm-up/session-timestamp logic are now part of the same consolidated object from v14.5.7/v14.5.8, with no change in behavior for anything else.

5662 tests / mypy --strict / ruff / codespell green, 100% coverage, deploy-verified on test HA.

## [v14.5.8] - 2026-07-10

Patch — internal cleanup, no user-facing change

### Changed

- **Internal only**: three separate per-camera bookkeeping dicts used for the live-stream session renewal/idle-timeout logic are now one consolidated object. No behavior change — continuation of the internal cleanup started in v14.5.7.

5642 tests / mypy --strict / ruff / codespell green, 100% coverage, deploy-verified on test HA.

## [v14.5.7] - 2026-07-10

Patch — internal cleanup, no user-facing change

### Changed

- **Internal only**: five near-identical copies of the same "get-or-create a per-camera lock" code, scattered across the integration and accumulated one at a time over many releases, are now a single shared helper. No behavior change — this is groundwork for further internal cleanup planned for upcoming releases.

5639 tests / mypy --strict / ruff / codespell green, 100% coverage, deploy-verified on test HA.

## [v14.5.6] - 2026-07-10

Patch — Mini-NVR credential-rotation race fixed at its root, not just tolerated

### Fixed

- **Follow-up to v14.5.5**: that release stopped the recorder from permanently giving up when a background credential rotation raced its very first connection attempt, but the underlying 401 rejection was still happening every single time — just being absorbed instead of prevented, as a user's live verification confirmed. The recorder now re-checks for a credential rotation immediately before connecting instead of relying on a value that could already be a few seconds stale, closing the window where this could happen at all.
- **A genuinely broken camera credential (as opposed to a passing rotation) could retry silently forever** without ever surfacing an error. Repeated authentication failures are now capped at 5 attempts before the recorder reports a clear error state, instead of retrying indefinitely.

5634 tests / mypy --strict / ruff / codespell green, 100% coverage, deploy-verified on test HA.

## [v14.5.5] - 2026-07-09

Patch — Mini-NVR no longer gives up permanently on a credential-rotation race

### Fixed

- **Mini-NVR could permanently stop recording (until a manual switch toggle) if it was turned on shortly after a live view was opened** (issue #42, found by a user verifying the previous v14.5.4 fix). A very quick, back-to-back pair of authentication failures — caused by the recorder's very first connection attempt racing the camera's background credential rotation — was being counted the same as two genuine crashes, which triggers a permanent give-up requiring the switch to be manually toggled off and back on. This specific kind of authentication failure is now recognized as transient and no longer counts toward that give-up limit; the recorder just keeps retrying until it lands after the rotation settles. Separately, the "error" state is now also properly cleared the next time recording successfully starts, instead of potentially lingering forever after a give-up.

5625 tests / mypy --strict / ruff / codespell green, 100% coverage, deploy-verified on test HA.

## [v14.5.4] - 2026-07-09

Patch — Mini-NVR recordings no longer truncated to a few seconds per minute

### Fixed

- **Mini-NVR recordings were being cut into tiny few-second clips instead of continuous 5-minute segments** (issue #41). The recorder was being unnecessarily restarted every time Bosch rotated the camera's local access credentials in the background — which for some cameras happens as often as every 15 seconds — interrupting the in-progress recording each time. The credential rotation itself doesn't require a restart, since the already-open recording connection survives it; only a brand-new connection would need the refreshed credentials, and that path already existed separately for genuine recorder failures. Recording now continues uninterrupted through routine credential rotation.

5796 tests / mypy --strict / ruff / codespell green, 100% coverage, deploy-verified on test HA.

## [v14.5.3] - 2026-07-08

Patch — fixed several entity icons stuck showing the wrong state

### Fixed

- **Several entities' icons never actually reflected their real state** — most noticeably the camera light and intercom switches, which always showed their "on" icon even while off, and the stream status sensor, which always showed the same icon regardless of whether the stream was idle, connecting, or actively streaming. Root cause: a hardcoded icon in the integration's code was silently overriding the correct, state-aware icon already defined internally. All entity icons are now defined in one place, so state-based icon switching works everywhere it's supposed to.

5679 tests / mypy --strict / ruff / codespell / pylint green.

## [v14.5.2] - 2026-07-08

Patch — entity names now use sentence case

### Changed

- **Entity display names now follow sentence case** ("Live stream" instead of "Live Stream", "Camera light" instead of "Camera Light") to match the convention used across Home Assistant Core itself, instead of this integration's prior ad-hoc Title Case. Acronyms and proper nouns (LED, WiFi, NVR, RCP, TLS, IVA, ONVIF, FCM, RTSP, LAN, Bosch, Frigate) keep their capitalization. English only — the 11 translated languages are unaffected, since e.g. German capitalizes every noun regardless of position. Purely a display-text change: entity IDs, unique IDs, and automations are unaffected.
- **Internal only:** added four new CI checks (enum comparisons, docstring hygiene, entity-name sentence case, PARALLEL_UPDATES presence) sourced from real review findings across `home-assistant/core` pull requests for the separate `bosch_shc` integration, to catch the same classes of issues here before they reach a reviewer.

5679 tests / mypy --strict / ruff / codespell / pylint green.

## [v14.5.1] - 2026-07-08

Patch — firmware-install progress indicator + clearer entity labels

### Fixed

- **Pressing the firmware Install button showed no progress indicator at all**, even though the on-camera flash takes several minutes. Home Assistant's own install-progress tracking only follows the entity for as long as the Install button's own call is awaiting — a single quick network request — not the multi-minute install this integration already tracks separately via the camera's own status. The entity now declares that it supports progress tracking, so Home Assistant uses that existing tracking instead of its own, much shorter, one.
- **The "Audio" switch and "Audio Volume" slider were unclearly labeled** — they control whether the Home Assistant card plays back a camera's stream audio and how loud, not a real microphone/speaker on the camera. Renamed to "Stream Audio" / "Stream Volume" to avoid confusion with the camera's actual audio hardware controls (e.g. intercom, Audio-Plus sound detection).

5679 tests / mypy --strict / ruff / codespell green.

## [v14.5.0] - 2026-07-08

Minor — camera restart button (reverse-engineered from the official app)

### Added

- **A "Restart Camera" button**, reverse-engineered from the official Bosch app (same cloud endpoint the app's own restart action uses). **Disabled by default**: live-testing against a real, online, owned camera showed Bosch's cloud currently rejects the request with "entity not found" on this account, even though the request matches the official app byte-for-byte — most likely the endpoint isn't enabled for every account/camera/firmware combination yet. Shipped disabled rather than left out entirely, since the implementation is correct and may simply start working as Bosch rolls it out further; enable it manually in the entity's settings to try it.

5679 tests / mypy --strict / ruff / codespell green, deploy-verified on test HA.

## [v14.4.13] - 2026-07-08

Patch — RCP session-open race fix

### Fixed

- **A camera could intermittently fail to fetch its RCP-based data (live thumbnail, ONVIF scopes, RCP version, LED dimmer state, and similar supplementary values) right after a privacy-mode toggle**, most noticeable on cameras that had just come back online. Two internal requests could each try to open their own session with Bosch's cloud RCP proxy at the same moment; the proxy only allows one live session per camera, so the second request was silently rejected and that camera's RCP-derived data stayed unavailable until the next retry. Found and fixed while debugging on real Gen1 hardware.

5669 tests / mypy --strict / ruff / codespell green, live-verified on test HA under repeated concurrent-toggle stress against real cameras — no further rejections.

## [v14.4.12] - 2026-07-08

Patch — WebRTC hard-fail regression fix

### Fixed

- **Opening a camera's live view from Home Assistant's native device/more-info view immediately showed "Failed to start WebRTC stream: Camera does not support WebRTC"**, for every camera (Gen1 and Gen2 alike), introduced by v14.4.11's WebRTC pre-warm wait. Overriding the WebRTC offer handler to add that wait unintentionally made Home Assistant's own camera framework skip its normal provider detection, so every offer was rejected before reaching the actual streaming backend. The custom card itself silently fell back to its regular video player and kept working, which is why this only showed up in the native app view. Reported in [#40](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues/40).

5668 tests / mypy --strict / ruff / codespell green, deploy-verified on test HA — a real WebRTC offer against a live camera now succeeds instead of failing.

## [v14.4.11] - 2026-07-08

Patch — native WebRTC pre-warm wait + in-card rule dialog

### Fixed

- **A live view opened from the native Home Assistant Companion app (not the card) could show up to ~25-35 seconds of black video with no retry** while a camera was still warming up its LOCAL stream. `async_create_stream()` (the HLS/Cast path) already waited for pre-warm to clear before reading the stream source, but `async_handle_async_webrtc_offer()` (the native app / go2rtc path) delegated straight to the base handler and read a not-yet-ready stream source immediately. Both paths now share the same pre-warm wait.
- **The "Regel erstellen" quick-action button used a native browser prompt for the rule name/start/end time**, which iOS's Companion app (WKWebView) silently ignores — the button looked broken there. Replaced with an in-card dialog.

### Changed

- Raised the minimum required Home Assistant version (`hacs.json`) to 2026.7.1, matching the version this integration is actually tested against.

1457 tests / mypy --strict / ruff / codespell / eslint / stylelint green, 231 e2e green.

## [v14.4.10] - 2026-07-08

Patch — firmware-update Repairs issue with a one-click Fix to install

### Added

- **A camera firmware update becoming available now raises a Repairs issue (Settings → Repairs)** naming the camera and the version jump (e.g. `9.40.102 → 9.40.104`), instead of the only signal being the generic core Settings → Updates panel — easy to miss. The issue clears itself automatically once the update has installed.
- **Clicking "Fix" on that issue installs the update immediately**, reverse-engineered from the official Bosch app's own "Update now" button (same cloud endpoint, same request), instead of only waiting for Bosch's automatic rollout schedule.
- **The camera's Firmware `update` entity also gets a working Install button.** Both entry points call the same underlying method, so a double-press guard and a short write-lock protect both identically instead of duplicating that logic. Installing reboots the camera; it's unreachable for roughly 3–7 minutes.

5660 tests (1 skipped) / mypy --strict / ruff / codespell green.

## [v14.4.9] - 2026-07-07

Patch — camera control switches now tell you when a command wasn't delivered, instead of silently reverting

### Fixed

- **Privacy mode, camera light, and notification switches now surface a notification when a command can't be delivered on any path.** When Bosch's cloud API is briefly unreachable, the integration already falls back to writing directly to the camera over your LAN (Gen2) or via a paired Smart Home Controller — but if every one of those paths failed too, the switch used to just silently revert to its previous state with zero explanation, looking like the button had done nothing. A persistent notification now appears whenever a command is not delivered on any path, so you know to try again once the connection recovers instead of wondering whether the button is broken.
- Fixed a related gap where the notification could itself be skipped if the local Smart Home Controller fallback was reachable but its own write also failed — that case previously went unreported too.

5620 tests (1 skipped) / mypy --strict / ruff / codespell green, 100% coverage.

## [v14.4.8] - 2026-07-06

Patch — advanced diagnostic field for a custom camera-API URL on manual login/re-login

### Added

- **An optional, empty-by-default "Advanced" field on the manual-login and re-login screens** lets you type in a different camera-API base URL to test against, for the rare case where Bosch support has confirmed your account should authorize against a non-default server. Never pre-filled with any specific value — it only changes which server this integration talks to, it doesn't unlock a beta program or anything else on its own. Most people will never need this; it exists purely as a diagnostic tool for a specific account issue, used only with Bosch's explicit guidance.

5611 tests / mypy --strict / ruff / codespell green, 100% coverage.

## [v14.4.7] - 2026-07-06

Patch — clearer guidance for the Bosch account/permission error added in v14.4.6

### Changed

- **The `sh:authorization.failed` error message now points at the actual fix.** Research into the official Bosch Camera App showed it performs a separate, one-time account-registration step against the camera backend after login (collecting name and terms-of-service acceptance) — an account that never went through that screen ends up with a permanently valid login but a permanently rejected camera-API token, and re-authenticating cannot fix it since that only repeats the login step. The message now says to open the official Bosch Smart Camera App and complete any registration or terms-of-service screen it shows, instead of the more generic "check camera sharing/access" wording from v14.4.6.

5603 tests (1 skipped) / mypy --strict / ruff / codespell green, 100% coverage.

## [v14.4.6] - 2026-07-06

Patch — Bosch account/permission errors reported correctly, plus a manual-login instruction fix

### Fix

- **Bosch account/permission errors are now reported correctly instead of as a token problem.** The v14.4.5 diagnostic logging paid off immediately: a community follow-up showed the camera API rejecting a freshly-renewed, valid token with `sh:authorization.failed` — Bosch's way of saying the account is missing camera-API access (e.g. an incomplete shared-user registration), not that the token expired. The integration used to respond by telling you to re-authenticate, which cannot fix an account-side permission problem; it now reports it as an account/permission issue and suggests checking camera sharing/access in the Bosch Smart Home app instead.
- Fixed step 6 of the manual-login instructions ("Click Submit to continue") — the actual button on that step is labelled "OK", not "Submit"/its localized equivalent. Fixed in all 12 locales.

5603 tests (1 skipped) / mypy --strict / ruff / codespell green, 100% coverage.

## [v14.4.5] - 2026-07-06

Patch — diagnostic logging for the "Token expired and renewal failed" case

### Changed

- **Enabling debug logging now captures why Bosch's cloud API rejects a token, not just that it did.** A follow-up community report (via the Bosch Smart Home Community) showed the v14.4.4 fix doesn't resolve every case: on some setups, a freshly-renewed token can still be rejected immediately by Bosch's API, which points away from the integration's retry logic and toward something on Bosch's side we previously had no visibility into. Both the initial 401 and the post-renewal-retry 401 now log the response body from Bosch at debug level (truncated, no token material), so a future report can be diagnosed with actual evidence instead of guesswork.
- No behavior change — this is diagnostics only.

5602 tests / mypy --strict / ruff / codespell green, 100% coverage.

## [v14.4.4] - 2026-07-06

Patch — token-refresh reliability fix, plus a manual-login wording tweak

### Fix

- **The integration could get stuck permanently reporting "setup error, retrying" after logging in.** If Bosch rejected a bearer token for any reason other than plain expiry — for example, right after a fresh manual login — the integration kept resending that same token forever instead of refreshing it. The token still looked valid by its own expiry timestamp, so a refresh was never attempted, and the built-in "please log in again" prompt was never reached either. A 401 from Bosch is now treated as authoritative: the integration correctly refreshes the token or asks you to re-authenticate instead of looping silently. As a side effect, the proactive refresh that runs a few minutes before a token's expiry now actually fires (it had been silently skipping itself every time).
- Generalized the manual-login instructions to say "an error page" instead of a specific "404 page", since Bosch's redirect page can return other error codes too (reported via the Bosch Smart Home Community).

### Internal

- Release workflow hardening: fixed a `gh release edit` flag mismatch, an awk command-injection vector via the tag name, and a silent changelog-extraction fallback that could ship a release without notes.

5601 tests / mypy --strict / ruff / codespell green, 100% coverage.

## [v14.4.3] - 2026-07-05

Patch — manual login option for SingleKey ID setup

### Fix

- **Setup now offers automatic or manual SingleKey ID login**, for the rare case where the automatic browser redirect gets confused. The one-click automatic login relies on a redirect relay (`my.home-assistant.io`) that tracks "the last Home Assistant instance you visited" rather than the specific setup flow you're in. With more than one browser tab open, or an in-app webview (as in the HA Companion App — also reported on desktop Safari), the redirect can land back in the wrong place, and the setup screen ends up flapping between blank, briefly successful, and an error, with login never completing (reported via the Bosch Smart Home Community).
- Setup now shows a choice up front: the existing **automatic login** (unchanged, still the default recommendation — most people won't notice a difference), or a **manual login** that sidesteps the redirect entirely — copy a link, log in, paste the result back. The manual option reuses the same mechanism already available for re-login under *Configure → Auth*.
- Also fixed: a leftover setup-dialog description (all 11 languages) that promised an immediate browser redirect after clicking Submit on re-authentication — now accurately describes the new choice.

5599 tests / mypy --strict / ruff / codespell green, 100% coverage.

## [v14.4.2] - 2026-07-04

Patch — live-stream teardown reliability fix, found and hardened via a live production incident.

### Fixes

- **Teardown/rebuild race.** `_tear_down_live_stream` (idle reaper, external-privacy-detection, frigate-idle-timeout, REMOTE-lifetime terminator) previously ran without the per-camera stream lock that `try_live_connection` already holds across a whole session rebuild. An unlocked teardown could interleave mid-rebuild: the rebuild publishes a brand-new local-proxy port, then the racing teardown closes that same port and clears the session state — leaving the new session dead with no error-counting and no automatic LOCAL→REMOTE recovery, stuck until a manual restart. Live incident (2026-07-04, Eyes Indoor II): the stream worker looped on "Connection refused" against a rotated session for 4+ minutes. Fixed by running the whole teardown under the same per-cam lock.
- **Stale-intent teardown.** Locking the teardown closed the race above but opened a narrower one: watchdogs that decide to tear down based on a stale read (idle reaper, frigate-idle-timeout, REMOTE-lifetime terminator) could now block on the lock for the whole duration of a concurrent rebuild, then run unconditionally against whatever session exists afterward — even a fresh, healthy, unrelated one. Fixed with a generation check: teardown re-validates the session generation it was told to expect, immediately after acquiring the lock, and no-ops if a newer rebuild has since superseded the stale trigger.
- **REMOTE-terminator self-cancellation.** The REMOTE session lifetime terminator is itself tracked as a renewal task and directly awaited the teardown, whose first action cancels that same tracked task — i.e. it could cancel itself mid-cleanup, aborting after the TLS proxy stopped but before go2rtc was unregistered / the HA `Stream` object was stopped. Fixed by scheduling teardown as its own task, matching the idle reaper's existing pattern.

## [v14.4.1] - 2026-07-03

Patch — bug-hunt round 2: ten hardening fixes across the cloud session, Digest auth, RCP, FCM, and the card, found by a further round of adversarial review.

### Fixes

- **Cloud-session race.** A lock now serializes cloud session (re)creation so two concurrent callers can no longer open duplicate sessions against the same camera.
- **Digest auth `-SESS` cnonce.** The `MD5-sess`/`SHA-256-sess` Digest variants now reuse the same client nonce across the algorithm's two hash rounds instead of generating a fresh one, matching RFC 7616.
- **RCP error-path hardening.** Malformed or unexpected RCP responses are now handled without raising an unguarded exception into the coordinator.
- **FCM supervisor race guards.** Additional guards around the supervisor's soft/hard-heal transitions close a window where two heals could overlap.
- **Per-camera write locks (intercom / audio / light / switch).** Concurrent writes to these entities now serialize per camera instead of racing.
- **Sensor None-vs-empty semantics.** Diagnostic sensors now distinguish "no data yet" (`None`) from "empty result" instead of collapsing both to the same displayed state.
- **`media_source` path-traversal guard.** Requests for saved event media are validated against path traversal before the file is served.
- **Snapshot-store save lock.** Concurrent snapshot writes to the same camera's cache file are now serialized.
- **Diagnostics redaction.** The downloadable diagnostics export closes a small additional gap in secret redaction.
- **Card `setConfig` re-registration guard.** Calling `setConfig` again on an already-mounted card no longer double-registers its internal listeners.

CARD_VERSION bumped 14.1.3 → 14.1.4.

Full per-round detail: [`docs/version-history.md`](docs/version-history.md).

5536 pytest / mypy --strict / ruff / codespell clean, card e2e green.

## [v14.4.0] - 2026-07-01

Minor — bug-hunt round with 5 backend/card reliability fixes plus one new working feature: `frigate_idle_timeout` (previously a documented no-op) now actually lingers and tears down idle external-recorder sessions.

### New feature

- **`frigate_idle_timeout` now works.** The option has been documented in 12 languages ("set 0 to close immediately") since it was introduced, but nothing ever read it and the on-demand external-recorder front door had no idle signalling wired up at all. `_CameraServer` (`frigate_endpoint.py`) now arms a cancellable idle-linger task when the last recorder client disconnects; it waits `idle_timeout` seconds of continuous zero clients before tearing the front-door's on-demand LOCAL session down (immediately if `idle_timeout<=0`). A reconnecting client (e.g. a recorder briefly dropping at a segment boundary) cancels the pending linger so the session isn't thrashed.

### Reliability fixes

- **REMOTE-snapshot renewal could kill a healthy stream (C1).** `_async_camera_image_impl`'s 401/403 renewal path treated a coalesced `STREAM_START_SKIPPED` result the same as a real failure and popped `_live_connections`/`_live_opened_at`, deleting a concurrent renewal's fresh session — killing the stream and the Frigate front-door with it. Now guarded the same way the `play_stream`/switch-turn-on paths already were.
- **FCM supervisor could miss a forced hard-heal (C2).** The inner poll loop only checked `is_started()` and never re-read `_fcm_force_hard_heal`, so in the "socket says started but pushes are silently dead" case the flag could sit unacted-on. The loop now breaks on the flag and fast-restarts so the top-of-loop credential purge runs promptly.
- **Concurrent light-group writes clobbered each other (C3).** `_put_lighting_switch` overwrote the whole `_lighting_switch_cache` entry on success instead of merging only the changed group — a scene toggling Top+Bottom LEDs could revert each other both in cache and on the camera. Fixed with a per-camera lock + merge-only-own-key, matching the fix `number.py` already had.
- **Same clobber on the glass-break/fire-alarm switches (C4).** `_BoschAudioDetectionSwitchBase._set_detection` had the identical whole-entry overwrite on `_audio_detection_cache`; same per-camera-lock fix applied.
- **TLS-proxy keepalive could silently kill the proxy thread (P3).** `setsockopt(SO_KEEPALIVE)` sat outside its try/except in `tls_proxy.py`; an `OSError` (FFmpeg closing its end) propagated out of the accept loop and killed the daemon proxy thread while `port_cache` still reported it alive, bypassing `on_proxy_died`. Both `SO_KEEPALIVE` calls moved inside their existing guard.
- **Card: dead-track watchdog could false-fire after a quick tab switch (C6).** The 9s dead-track deadline was measured against wall-clock time; a poll re-armed while the tab was hidden but the deadline kept counting in the background, so a quick alt-tab right after opening a live view could land back past the deadline on a single 0-frames sample and force sticky HLS on a perfectly healthy WebRTC stream. Now only *visible* elapsed time accrues toward the deadline.
- **Card: unbounded HLS MEDIA_ERROR recovery loop (P1).** hls.js's fatal `MEDIA_ERROR` path called `recoverMediaError()` without a bound (unlike the `NETWORK_ERROR` path, which already capped at 3). A persistently corrupt segment could loop forever, silently frozen. Now bounded to 3 attempts, then falls through to the same full-reconnect the other fatal error paths use.

Also: the tag-triggered Release workflow now gates on `tests.yml`/`quality.yml`/`validate.yml`/`secret-scan.yml` concluding green on the same commit before creating a release, and release titles are `vX.Y.Z — <summary>` instead of a bare version number.

CARD_VERSION bumped 14.1.2 → 14.1.3 (dead-track deadline + bounded MEDIA_ERROR fixes).

5530 pytest / mypy --strict / ruff / codespell / pylint clean. 225/225 card e2e (Firefox + WebKit) green.

## [v14.3.1] - 2026-06-29

Patch — **stops the diagnostic entities from bloating the recorder database** (reported in #39).

### Database optimization

- **Volatile and large diagnostic attributes are no longer recorded.** Several diagnostic entities carried attributes that either changed on every coordinator/drain tick (`last_push_seconds_ago`, `last_fetched_seconds_ago`, `last_check_seconds_ago`, `write_grace_seconds_left`, the NVR drain counters) or held large card-only data (motion-zone / privacy-mask coordinate lists, rule and analytics-module lists, rotating stream/proxy URLs). Home Assistant's recorder hashes each state's attributes into the shared `state_attributes` table, so a value that changes every tick minted a brand-new row every tick — ballooning the database. The most visible offender was the *Event Detection* (`fcm_push_status`) sensor.
- These attributes are now marked `_unrecorded_attributes`, so they **stay fully visible live in the UI** but are excluded from history. Each affected entity's `state_attributes` footprint collapses from thousands of unique rows to a single shared row. Existing oversized history is reclaimed by the recorder's normal purge.
- No change to any entity's state, availability, streaming, or card behavior.

5524 pytest / mypy --strict / ruff / codespell clean.

## [v14.3.0] - 2026-06-26

A reliability rewrite of the FCM push logic, replacing the old watchdog + self-heal state machine with a single, self-restarting supervisor task.

### Reliability improvements

- **FCM supervisor replaces the watchdog + self-heal ladder.** The previous design used a cool-down ladder (`SELF_HEAL_COOLDOWNS_SEC`) with jitter, a soft-heal streak counter, and a separate `async_self_heal_fcm_push` function called from the coordinator watchdog. It was hard to reason about and could get stuck in the "ladder exhausted" state requiring an HA restart. Replaced by `_async_run_fcm_supervisor` — a single asyncio Task that loops forever, polls `is_started()` every 10 s, and restarts with clean exponential backoff `(5, 30, 60, 120, 300, 600, 1800 s)`.
- **Soft vs. hard heal without a ladder.** Soft heal (stop + restart the listener, preserve credentials) is tried first. After 3 consecutive soft failures, or when Google signals a credential rejection (`PHONE_REGISTRATION_ERROR`), the supervisor escalates automatically to a hard heal: purge all `fcm_*` entry-data keys and run a fresh `checkin_or_register()`.
- **Delivery-death detection still works.** The `_fcm_force_hard_heal` flag set by the push-delivery detector is now read directly by the supervisor on its next poll tick and triggers an immediate hard heal; the flag is cleared after acting.
- **Coordinator watchdog simplified.** The watchdog in `_async_update_data` now has one job: if the supervisor task is `None` or done, spawn it. No more ladder checks, no more cool-down state.

### Internal / tests

- Deleted `test_fcm_self_heal.py` (22 tests for the removed `async_self_heal_fcm_push` function).
- `_SHARED_ERROR_TIMESTAMPS` removed from `_FCMNoiseFilter`; replaced by `_SHARED_STALENESS_TIMESTAMPS` which tracks only credential-rejection markers (not general connectivity errors).
- `reset_fcm_error_counter()` and `async_start_fcm_push()` kept as backward-compat shims so external callers don't break.

Addresses #36 (motion stuck on "Clear" when FCM push silently dies).

## [v13.7.1] - 2026-06-17

A patch focused on snapshot/image display, plus one security fix.

### Security

- **SMB upload now verifies the cloud-media download over TLS.** When uploading event media to an SMB share, the integration fetched the clip/snapshot from the Bosch cloud without verifying the TLS certificate (`CERT_NONE`). It now verifies against the pinned Bosch CA, the same way every other cloud call does (CWE-295). Update recommended if you use the SMB upload feature.

### Bug fixes

- **Black image on mobile (last snapshot not shown).** On the Home Assistant Companion app a camera — especially an offline one — could show a black frame instead of its last snapshot, while the desktop browser showed it fine. Two causes, both fixed: (1) the card refused to load the image for an offline camera, so it relied on a browser-only cache the app's webview doesn't have — it now fetches the last good frame the backend still serves; (2) the backend treated its internal 1×1 black placeholder as a valid cached image on a cold start, so a request arriving before the on-disk snapshot was restored got the placeholder — it now fetches a real frame, with a back-off so an offline camera isn't polled on every request.
- **Fewer redundant snapshot requests.** A dashboard with several cameras no longer makes every camera refresh on every tile's timer (the refresh now targets only the relevant camera, and the per-camera timers are staggered), and the card no longer downloads each snapshot twice to cache it. This is easier on the camera's limited cloud session budget.
- **Offline cameras showed a stuck loading spinner.** A camera reported as offline displayed the "loading image…" spinner on top of the "Camera Offline" overlay (the two messages overlapped). The spinner is now reliably suppressed while a camera is offline, no matter which path triggered it.
- **A "refreshing" overlay could get stuck.** After a failed image refresh the semi-transparent "refreshing" overlay could remain on screen on every subsequent refresh. The pending-refresh state is now cleared on error.
- **Live view didn't recover cleanly after a camera dropped offline.** If a camera went offline while its live stream was playing, a frozen frame lingered and the stream didn't restart cleanly once the camera came back. The live view is now torn down on the offline transition so it resumes correctly on recovery.
- **Offline overlay is now localized.** The offline message (title, "last seen" label and the date format) was hardcoded in German for everyone — it now follows your Home Assistant language across all 11 supported languages.

## [v13.7.0] - 2026-06-16

A big bug-fixing round across the backend and the card, plus one new opt-in feature.

### New

- **AI snapshot descriptions (opt-in).** A new option lets Home Assistant's AI Task describe what a camera sees — automatically on motion/person events, or on demand through the new `describe_snapshot` service (which returns the text). You choose the AI Task entity, the prompt, and the reply language. Guardrails keep it economical: a per-camera cooldown, a daily call budget, an optional active-time window, and an optional presence/condition gate (for example, only analyse when nobody is home). The latest description is exposed as a per-camera sensor and can optionally be appended to your event notifications. **Off by default** — no AI calls happen until you enable it, and privacy mode always blocks analysis.

### Bug fixes

- **"Events today" counts drifted around midnight.** The events-today / movement / audio sensors compared a local date against the cameras' UTC event timestamps, so counts could under-count or land in the wrong day for an hour or two around midnight. Now bucketed by UTC date.
- **A cloud blip blanked the events list and delayed detection.** A transient cloud failure during an event poll wiped a camera's recent-events list (and its events-today count) and pushed the next poll out by a full interval (up to 5 min). The poll now retries promptly and keeps the cached events until the cloud answers again.
- **Camera flipped to *unavailable* during cloud maintenance.** While a camera was streaming locally during a known Bosch cloud maintenance window, every brief cloud dip flipped it (and its entities) to unavailable. It now stays available as long as the LAN datapath keeps serving frames (firmware-update guard still takes priority).
- **Redundant FCM retries during a cloud outage.** After a push, the follow-up cloud fetch was retried even when every fetch had failed. It now retries only when a fetch actually succeeded.
- **Double snapshot fetch.** Overlapping image refreshes could each open a camera session; a synchronous in-flight guard now skips the duplicate, saving a redundant `PUT /connection`.
- **AI caption never came back after auto-hiding.** The on-image AI caption now re-appears correctly after it has auto-hidden.
- **Volume-slider listener leak.** The card now removes the volume-slider listener when it is torn down.
- **AI settings couldn't always be saved.** You can now clear an AI entity gate to disable it, the daily-budget field no longer has a hidden upper limit (`0` = unlimited), and malformed active-time values are rejected instead of silently disabling the window.
- **Internal:** the daily AI-budget "limit reached" notice now re-arms in lockstep with the local-midnight reset, and the manual `describe_snapshot` call is counted toward the in-flight budget.

Existing installations behave exactly as before until you turn the AI option on.

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

