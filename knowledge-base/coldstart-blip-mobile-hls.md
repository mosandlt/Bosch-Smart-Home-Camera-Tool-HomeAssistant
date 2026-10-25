# Cold-start "3-second blip" — narrowed to mobile/HLS-over-tunnel path
STATUS: OPEN. Investigated 2026-06-03. NOT shipped in v13.5.6.

## Symptom (user report)
Livestream start on **mobile Companion app** (remote, via Cloudflare tunnel): video briefly appears, dies (~3 s), long wait (~25 s) until real playback.

## Proven (live, 2026-06-03)
Instrumented **real cold start** in desktop **Safari** on LAN (Innenbereich, Gen2 indoor) via AppleScript JS injection + shadow-DOM pierce + `window.__bliplog` video-event observer on `#cam-video`:
```
0.0s   snapshot shown, video display:none
7.0s   _waitingForStream=true  (connecting overlay, pre-warm)
31.1s  _startingLiveVideo=true (pre-warm done ~31s — INHERENT Bosch latency)
31.7s  play/waiting/suspend, video display:block BUT snapshot (img:block) still covering
32.9s  loadeddata + playing, readyState=4  (first real frame)
33.1s  currentTime advancing (0.2s), snapshot hidden (img:none)  → clean handover
```
→ Desktop LAN / WebRTC cold start CLEAN, no blip. Snapshot stays on top until video plays (`activateVideo`/`clearOverlay` gate works). Rules out WebRTC reveal path.

## Conclusion
Blip specific to **mobile Companion-app HLS-over-tunnel path** (different code path):
- `_remoteSkipWebRTC` skips WebRTC entirely (Companion/mobile + external host)
- HLS served via HA + cloudflared (path `cf_unbuffer` already patches for buffering)
Desktop Safari on LAN takes WebRTC path → cannot reproduce. The ~25 s wait is **inherent Bosch pre-warm** (PUT /connection + TLS proxy + encoder warm-up) — no client fix; only a possible visible *blip* (premature HLS connect paints a frame then stalls) is fixable.

## To fix (next session)
Need ONE of:
1. Phone-side observation: 10 s screen recording / precise description of mobile cold start — flash then freeze/black/spinner, at what second?
2. Instrument HLS-remote path directly (harder without phone).
Then target reveal/retry logic in `_startLiveVideo` / `_waitForStreamReady` on `_remoteSkipWebRTC` (HLS) branch, NOT WebRTC branch.

## Tooling that worked (reuse)
- Chrome AppleScript injection OFF ("Allow JavaScript from Apple Events" never took) → use **Safari**: Settings → Advanced → show Develop menu, then Develop → "Allow JavaScript from Apple Events". `do JavaScript ... in document 1` works.
- HA buries card in shadow roots; recursive `deepAll('bosch-camera-card')` walking `.shadowRoot` of every element finds all cards. `el.shadowRoot` readable from injected JS (isolated-world limit only hits `customElements`).
- Observer: attach video events + 1 s poll capturing `{display, readyState, currentTime, _liveVideoActive, _startingLiveVideo, _waitingForStream, img.display}` to `window.__bliplog`; trigger via `switch.<cam>_live_stream` turn_on; dump log after ~45 s.
- Pick a **privacy-OFF** camera — privacy ON makes live-stream switch `unavailable` (card greys play button, v13.5.6 feature).
