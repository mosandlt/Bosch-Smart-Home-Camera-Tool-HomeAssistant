# PiP freezes when browser tab hidden (Chrome/Mac)

SYMPTOM: PiP active → switch browser tab → after a while PiP freezes (video stops) → return to tab → in-page `<video>` resumes but the floating PiP window stays frozen.
PLATFORM: confirmed Chrome on macOS (Thomas 2026-06-19). v13.7.2 "PiP-freeze-hidden-tab fix" did NOT resolve it.

## Root cause (two compounding factors)
1. HIDDEN_TAB_TIMER_THROTTLING: Chrome 88+ heavily throttles `setInterval`/`setTimeout` in hidden tabs (sub-1s → 1s; chained timers → max 1×/min; intensive throttling after 5 min hidden can pause/clamp to 1×/min). Our stall detector is a 5 s `setInterval` (src/bosch-camera-card.js ~L5950). In a hidden tab it barely runs → the v13.7.2 PiP escalation (`ownsPip` → full reconnect) effectively only fires once the tab is visible again, not while hidden.
   Ref: developer.chrome.com/blog/timer-throttling-in-chrome-88
2. GO2RTC_TRANSPORT_DEATH: go2rtc/WebRTC WebSocket times out when tab inactive (`websocket … i/o timeout`, "WebRTC Client Offline"). Hard refresh fixes it; resume does not. The decode track stops; PiP surface holds the last frame.
   Ref: github.com/AlexxIT/WebRTC/issues/121
PIP_SPECIFIC: reconnect rebuilds the stream into the in-page `<video>`; the PiP floating window is a separate compositor surface. If reconnect swaps `srcObject`/HLS but frames don't re-present to that element, the PiP stays frozen even after the tab recovers.

## Recommended fix direction (NOT yet implemented — patch #2 candidate)
- LIVENESS via `requestVideoFrameCallback` instead of throttled `setInterval`: rVFC fires per frame presented to the compositor. A PiP window keeps compositing while the tab is hidden, so rVFC keeps firing while frames flow and STOPS exactly when they freeze → unthrottled freeze signal that works in the background. (web.dev/articles/requestvideoframecallback-rvfc)
- REACT TO TRANSPORT, not timers: add `track.onmute`/`onunmute` (fires when the WebRTC source can't provide data) + `pc.oniceconnectionstatechange` (disconnected/failed → ICE restart / full reconnect). Catches the go2rtc WS death immediately. (MDN MediaStreamTrack mute/unmute)
- PIP REATTACH: on reconnect while `document.pictureInPictureElement === video`, re-attach the new `srcObject`/HLS to the SAME element; if frames still don't resume, programmatic exit+re-enter PiP as last resort.
- Optional: keepalive ping to stop the go2rtc WS `i/o timeout` in background.

## Sources
- developer.chrome.com/blog/timer-throttling-in-chrome-88
- github.com/AlexxIT/WebRTC/issues/121
- web.dev/articles/requestvideoframecallback-rvfc
- MDN MediaStreamTrack mute/unmute events
