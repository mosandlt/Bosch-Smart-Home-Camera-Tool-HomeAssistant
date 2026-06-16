# Cold-start "3-second blip" — narrowed to the mobile/HLS-over-tunnel path

Status: OPEN. Investigated 2026-06-03. NOT shipped in v13.5.6.

## Symptom (user report)
Starting a livestream on the **mobile Companion app** (remote, over the Cloudflare
tunnel): video briefly appears, then dies (~3 s), then a long wait (~25 s) until it
plays for real.

## What we proved (live, 2026-06-03)
Instrumented a **real cold start** in desktop **Safari** on the LAN (Innenbereich,
Gen2 indoor) via AppleScript JS injection + shadow-DOM pierce + a `window.__bliplog`
video-event observer on `#cam-video`. Timeline:

```
0.0s   snapshot shown, video display:none
7.0s   _waitingForStream=true  (connecting overlay, pre-warm)
31.1s  _startingLiveVideo=true (pre-warm done ~31s — INHERENT Bosch latency)
31.7s  play/waiting/suspend, video display:block BUT snapshot (img:block) still covering
32.9s  loadeddata + playing, readyState=4  (first real frame)
33.1s  currentTime advancing (0.2s), snapshot hidden (img:none)  → clean handover
```

**→ The desktop LAN / WebRTC cold start is CLEAN — no blip.** The snapshot stays on
top until the video actually plays (`activateVideo`/`clearOverlay` gate works). This
rules out the WebRTC reveal path as the cause.

## Conclusion
The blip is specific to the **mobile Companion-app HLS-over-tunnel path**, which is a
DIFFERENT code path:
- `_remoteSkipWebRTC` skips WebRTC entirely (Companion/mobile + external host).
- HLS is served through HA + cloudflared (the path `cf_unbuffer` already patches for
  buffering).
Desktop Safari on the LAN takes the WebRTC path, so it cannot reproduce the mobile blip.

The ~25 s wait itself is **inherent Bosch pre-warm** (PUT /connection + TLS proxy +
encoder warm-up) — no client fix shortens it. Only a possible visible *blip* (a
premature HLS connect that paints a frame then stalls) would be fixable.

## To fix it (next session)
Need ONE of:
1. **Phone-side observation**: a 10 s screen recording / precise description of the
   mobile cold start — does video flash then freeze/black/spinner, and at what second?
2. Instrument the HLS-remote path directly (harder without the phone).
Then target the reveal/retry logic in `_startLiveVideo` / `_waitForStreamReady` on the
`_remoteSkipWebRTC` (HLS) branch, NOT the WebRTC branch.

## Tooling that worked (reuse next time)
- **Chrome AppleScript injection was OFF** ("Allow JavaScript from Apple Events" never
  took) — use **Safari** instead: enable Safari → Settings → Advanced → show Develop
  menu, then Develop → "Allow JavaScript from Apple Events". `do JavaScript ... in
  document 1` works.
- HA buries the card deep in shadow roots; a recursive `deepAll('bosch-camera-card')`
  that walks `.shadowRoot` of every element finds all cards. `el.shadowRoot` is
  readable from injected JS (the isolated-world limit only hits `customElements`).
- Observer: attach video events + a 1 s poll capturing
  `{display, readyState, currentTime, _liveVideoActive, _startingLiveVideo,
  _waitingForStream, img.display}` to `window.__bliplog`; trigger the stream via
  `switch.<cam>_live_stream` turn_on; dump the log after ~45 s.
- Pick a **privacy-OFF** camera — privacy ON makes the live-stream switch
  `unavailable` (and the card greys the play button, the v13.5.6 feature).
