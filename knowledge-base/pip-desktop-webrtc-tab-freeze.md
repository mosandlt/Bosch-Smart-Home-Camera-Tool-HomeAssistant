# Desktop Chrome — WebRTC PiP window frozen after tab-switch (RESOLVED in v14.0.0)

STATUS: RESOLVED. Diagnosed 2026-06-23 with the fix recipe below (option B, "non-destructive recovery"); the follow-up investigation in `pip-tabswitch-freeze-process.md` instead chose option A (suppress recovery entirely while PiP is open) and shipped it as part of **v14.0.0** (2026-06-24, "Picture-in-Picture now keeps playing when you switch browser tabs") — see `docs/version-history.md`. Kept here for the root-cause research trail.
SCOPE: Desktop Chrome (Mac). WebRTC transport (no HLS banner). NOT iOS, NOT mobile.

## Symptom
PiP open + switch browser tab (tab hidden) + return → in-page `<video>` recovers (fresh frames) but the floating PiP window stays frozen on the last old frame. Verified by Thomas 2026-06-23 on v13.7.9.

## Root cause (2 web-research + 1 code-trace agent, converged)
On tab-return `_resumeLiveStreamIfNeeded` finds no fresh frame in 3s → `_scheduleLiveRecovery` → `_stopLiveVideo` → 1s gap → `_startLiveVideo`/`_startWebRTC`. During that path:
- `_stopLiveVideo`: `this._webrtcPc.close()` (old tracks END) → `video.srcObject = null` → `video.load()`.
- `_startWebRTC`: `const remoteStream = new MediaStream()` (NEW object) → `video.srcObject = remoteStream`.

Chrome PiP window is bound to the old `WebMediaPlayer`/compositor layer. `srcObject = null` + `video.load()` tears that compositor link → PiP freezes on last frame; assigning a brand-NEW MediaStream afterward does not reliably re-wire the PiP surface.
- crbug.com/894317, W3C picture-in-picture #97: PiP not following srcObject replacement.
- Chromium #415501 / W3C mediacapture-main #453: `addTrack`/`removeTrack` on a MediaStream already bound to `<video>` is IGNORED by Chrome → in-place track swap does NOT work.
- Ending the old track before assigning the new one freezes the compositor stream immediately.

## Fix recipe (matches HA core `ha-web-rtc-player.ts`)
Build-then-swap, not stop-then-start:
1. Build the NEW `RTCPeerConnection` + new `MediaStream` and WAIT until its tracks are live BEFORE touching the element.
2. Swap directly: `video.srcObject = newStream` with NO `null` in between and NO `video.load()` while `document.pictureInPictureElement === video` (ha-web-rtc-player explicitly skips load()/null during PiP).
3. Close the OLD pc / stop old tracks only AFTER the new stream's first frame plays.
4. `video.play()` after the swap (no autoplay attribute).

IMPLICATION: `_scheduleLiveRecovery`'s teardown-then-1s-gap-then-start ordering must be reworked for the PiP/WebRTC case so old stream is never detached before new one is ready. Needs its own verify round + Desktop-Chrome device verification (cannot be self-verified headless).

## Out of scope / already fixed
- Mobile (iOS Companion / cellular): FIXED in v13.7.9 (dead-track → sticky HLS). HLS in PiP uses a real media element src, not a MediaStream, so this compositor issue does not apply.
- Without PiP + tab-switch on desktop: works (in-page rebuild recovers).
