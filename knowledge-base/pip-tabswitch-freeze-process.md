# PiP + tab-switch freeze — the actual process (2026-06-24)

STATUS: ROOT-CAUSE WALKTHROUGH — decision A below shipped as **v14.0.0** (2026-06-24, "Picture-in-Picture now keeps playing when you switch browser tabs"), superseding the internal `v13.7.9.2` label used in this doc. See `docs/version-history.md`. Written to stop blind patching.
SCOPE: Desktop Chrome (Mac), WebRTC transport, PiP window open, user switches browser tabs.
SYMPTOM (Thomas, v13.7.9.1, 2026-06-24): "PiP breaks again after some minutes, and I see a loading banner on the tile when I switch back."

## Lifecycle (current code)

T0: PiP open, WebRTC live. `_boschPipActive === this`; same `<video id=cam-video>` shown in floating PiP. `_webrtcPc` live; remote `MediaStream` bound to `video.srcObject`.

T1: User switches to another tab → this tab HIDDEN.
- `_onVisibilityChange` hidden branch → `_scheduleHiddenTeardown()`. PiP-exempt: 60s-grace callback bails because `ownsPip`. No teardown. GOOD.
- setInterval stall-checker throttled by Chrome to ~1×/min. Web-Worker heartbeat keeps ticking (not throttled).
- WebRTC media (SRTP/UDP) keeps flowing in Chrome's network process (not throttled).

T2: After a few minutes hidden: go2rtc signaling path times out (AlexxIT/WebRTC#121, WS i/o timeout). Chrome fires `mute` on remote video track.
- `ev.track.onmute` → 6s debounce → `_scheduleLiveRecovery("webrtc video track muted >6s")`.
- Fires EVEN while hidden because event-driven; Lever-1 guard does NOT suppress: guard only bails for `hidden && !ownsPip`. Here `ownsPip` is TRUE → recovery RUNS.

T3: `_scheduleLiveRecovery` → `_stopLiveVideo()`:
- `_webrtcPc.close()` (old tracks END) → `video.srcObject = null` → `video.removeAttribute("src")` → `video.load()`.
- PiP exit SKIPPED (`_reconnectingLiveVideo===true` guard ~7251) → floating window STAYS open but bound to torn-down compositor layer.
- THE FREEZE: `srcObject=null` + `video.load()` tears Chrome's PiP WebMediaPlayer/compositor link (crbug 894317, W3C picture-in-picture#97).

T4: 1s gap → `_startLiveVideo()` → `_startWebRTC()`:
- Builds NEW `RTCPeerConnection` + brand-NEW `MediaStream` → `video.srcObject = remoteStream`.
- Assigning NEW MediaStream to PiP'd element does NOT reliably re-wire PiP surface (crbug 894317) → PiP stays FROZEN.

T5: User switches back → tab VISIBLE.
- In-page tile mid-rebuild → shows "connecting"/"Verbinde" = LOADING BANNER Thomas sees.
- In-page `<video>` recovers; PiP window does NOT.

## Why prior levers did not fix the PiP case
- Lever 1 (extend grace 8s→60s + suppress recovery while `hidden && !ownsPip`): keeps stream alive for short non-PiP switches, but PiP-owned recovery at T2 is NOT suppressed → PiP still tears down + freezes.
- `_reconnectingLiveVideo` PiP-keep guard (keeps window OPEN during reconnect) does not help: keeping the window open over a `null`+`load()` compositor teardown is exactly what freezes it.

ROOT_CAUSE (one sentence): recovery for a backgrounded WebRTC stream does destructive `srcObject=null` + `video.load()` + new-PeerConnection/new-MediaStream rebuild → destroys PiP compositor binding (crbug 894317). Trigger: go2rtc background WS timeout (AlexxIT/WebRTC#121) fires `track.onmute`.

## Candidate Fixes

A. PiP-suppress recovery (Thomas' idea): while PiP window open, do NOT auto-recover at all (6h cap, re-eval on mount). Pro: trivial, kills self-inflicted freeze. Con: genuinely dead PiP stays frozen until user closes/reopens.
B. Non-destructive recovery (build-then-swap): build new pc + stream, WAIT for live tracks, assign `video.srcObject = newStream` with NO `null`/`load()` while PiP active; close old pc after first frame. Bigger rework; crbug 894317 may still not re-wire PiP on NEW MediaStream.
C. Transport-level recovery: never reassign srcObject; recover via `pc.restartIce()` / receiver-side track refresh so SAME bound MediaStream keeps compositing → PiP never detaches.
D. Keep-alive: Web-Worker WS keepalive ping so go2rtc signaling never i/o-times-out → no `track.onmute` → no recovery → no freeze.

## VERDICT (3 web-research agents + code check, 2026-06-24)
DECISION: A — suppress all recovery while a PiP window is open (= exactly what HA core `ha-web-rtc-player.ts` does: `if (document.pictureInPictureElement) return;` at the top of its visibility/recovery handler). Recover on PiP-exit / tab-return instead.

RULED_OUT:
- C `pc.restartIce()`: go2rtc has NO re-offer/renegotiation support — `camera/webrtc/offer` HA WS is one-shot (go2rtc source `pkg/webrtc/conn.go`; #1851 ICE-restart panic). NOT VIABLE.
- D Worker WS keepalive: targets AlexxIT STANDALONE card's DIRECT browser→go2rtc WS (5s hardcoded write-deadline, `internal/api/ws/ws.go`). OUR card holds NO direct go2rtc WS — signaling rides HA's `hass.connection.subscribeMessage` (verified: `grep "new WebSocket" src/bosch-camera-card.js` → none). NOT APPLICABLE.
- B build-then-swap: not needed once we simply don't recover during PiP; crbug 894317 may not re-wire a NEW MediaStream into PiP surface anyway. Reserve as future fallback.

## Implementation (HA v13.7.9.2, card-only)
1. `_scheduleLiveRecovery` unified guard → recovery runs ONLY when `visible AND !ownsPip`.
2. `ownsPip` suppression has 6h hard cap (`PIP_RECOVERY_SUPPRESS_MS`), lazily self-initialising.
3. `enterpictureinpicture` sets 6h deadline; `leavepictureinpicture` clears it AND calls `_resumeLiveStreamIfNeeded()`; `connectedCallback` re-evaluates.
4. NEVER `srcObject=null`/`video.load()` while `document.pictureInPictureElement === video`.

VERIFY: source pins + behavioural test (recovery no-ops while PiP-owned, runs when visible+non-PiP) + 3 verify agents + Thomas Desktop-Chrome PiP verification (NOT self-verifiable headless).
SOURCES: home-assistant/frontend `src/components/ha-web-rtc-player.ts` (PiP guard lines 71-81, restartIce 252-260); AlexxIT/go2rtc `www/video-rtc.js` + `internal/api/ws/ws.go`; AlexxIT/WebRTC#121; crbug 894317; W3C mediacapture-main#453.
