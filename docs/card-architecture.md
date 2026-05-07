# Bosch Camera Card — Architecture & Developer Notes

This document contains the architectural notes, section markers, and design rationale extracted from `src/bosch-camera-card.js`. The deployed `www/bosch-camera-card.js` is stripped of comments to reduce the wire payload.

**Source file:** [`src/bosch-camera-card.js`](../src/bosch-camera-card.js)

> **Note:** Section markers and line numbers below were last extracted at v2.8.1. The card structure is unchanged but line numbers have drifted. Use `grep` on `src/bosch-camera-card.js` for exact positions. Current card version: **v2.11.7**.

---

## Header Banner

```
/**
 * Bosch Camera Card — Custom Lovelace Card
 * ==========================================
 * Displays a Bosch Smart Home camera with live streaming state,
 * status indicator, event info, and stream controls.
 *
 * Installation:
 *   1. Copy bosch-camera-card.js to /config/www/bosch-camera-card.js
 *   2. In HA → Settings → Dashboards → ⋮ → Resources → Add resource:
 *        URL:  /local/bosch-camera-card.js
 *        Type: JavaScript module
 *   3. Hard-reload browser (Ctrl+Shift+R)
 *
 * Card YAML:
 *   type: custom:bosch-camera-card
 *   camera_entity: camera.bosch_garten        # required
 *   title: Garten                             # optional
 *   # idle refresh: 60 s visible / 1800 s background (Page Visibility API)
 *
 * Version: 2.11.7  (changelog entries below are from v2.8.1 extraction)
 *
 * Changes vs 2.7.0:
 *   - Gen2 polygon zone overlay: renders polygon zones (from GET /zones) on camera image
 *   - Privacy mask overlay: separate toggle + black polygon/rect overlay for privacy masks
 *   - Updated diagnostics to show Gen2 zones + privacy masks from separate sensor
 *   - Ambient light schedule sensor support in diagnostics
 *
 * Changes vs 2.6.0:
 *   - Separate light controls: Front Light, Wallwasher toggle + Intensity slider
 *     appear below the main Light toggle when entities exist (Outdoor camera).
 *   - Siren button in Services accordion — triggers acoustic alarm on the camera.
 *
 * Changes vs 2.4.0:
 *   - New "Services" accordion: grid of quick-action buttons for
 *     Snapshot, Zonen lesen, Privacy-Masken, Freunde, Regel erstellen, Verbindung.
 *     Regel erstellen uses prompt() for name/start/end.
 *   - Motion zone overlay now uses cloud API zones (normalized x/y/w/h 0-1)
 *     instead of broken RCP coordinates.
 *
 * Changes vs 2.3.1:
 *   - New "Zeitpläne & Zonen" accordion section:
 *     - Schedule rules list with AN/AUS toggle per rule (calls update_rule service)
 *     - Delete button per rule (calls delete_rule service)
 *     - Motion zones count display (from RCP sensor)
 *   - New entity: rules_entity (sensor.bosch_{cam}_schedule_rules)
 *   - Optimistic UI for rule toggle and delete
 *
 * Changes vs 2.1.0:
 *   - Removed dead _streamingImageLoad() method (snapshot-streaming mode removed in v2.0.0)
 *   - Cleaned up outdated snapshot-polling changelog entries
 *
 * Changes vs 1.9.4:
 *   - "connecting" badge state: while HLS is negotiating (startingLiveVideo=true),
 *     badge shows amber "connecting" instead of misleading "idle". CSS: orange dot
 *     with faster pulse (0.8 s). Clears to "streaming" once video plays.
 *   - Frame Δt in debug line: shows actual ms since last frame load
 *     (e.g. "fresh 14:23:05 Δ2003ms | 1920×1080") — live proof that 2 s intervals
 *     are now consistent. Only tracked for fresh frames (not cache restores).
 *   - Stream uptime counter: badge label updates to "00:47" / "1:23" while streaming,
 *     refreshing every frame (2 s). Proves session renewal is working — stream stays
 *     alive past 60 s. Resets when stream stops.
 *   - Retry on image error during streaming: transient snap.jpg failures (network
 *     glitch, proxy hiccup) now trigger one immediate retry after 500 ms instead of
 *     silently showing the previous frame forever.
 *
 * Changes vs 1.9.4:
 *   - "connecting" badge state while HLS negotiates (faster pulse 0.8s) → clears to "streaming"
 *   - Frame Δt in debug line (e.g. "Δ2003ms") — proof of consistent 2s intervals
 *   - Stream uptime counter in badge ("00:47") — proves session renewal working
 *   - Retry on snap.jpg error during streaming: immediate 500ms retry
 *
 * Changes vs 1.8.0:
 *   - Added 3 collapsible accordion sections below the quality dropdown:
 *     1. Benachrichtigungs-Typen: movement/person/audio/trouble/alarm notification toggles
 *     2. Erweitert: timestamp overlay, auto-follow, motion detection, record sound, privacy sound
 *     3. Diagnose: WiFi signal, firmware, ambient light, movement/audio events today
 *   - Accordion sections auto-hide when none of their entities exist
 *   - All new toggle rows use existing _updateToggleBtn pattern
 *
 * Changes vs 1.7.6:
 *   - Fix: stale image shown for up to 60 s on page load. When localStorage cache
 *     restored an old image, _imageLoaded=true blocked the immediate fresh fetch on
 *     first hass assignment. Now always fetches fresh on first hass, with a subtle
 *     "Aktualisiere…" spinner overlay on the cached image while loading.
 *
 * Changes vs 1.7.4:
 *   - Pan row buttons now use SVG icons (double-chevron left/right, chevron left/right,
 *     crosshair center) matching the style of all other card buttons.
 *     Previously used Unicode text characters which rendered inconsistently.
 *
 * Changes vs 1.7.3:
 *   - Fix: initial image load was silently skipped because _hass is null when
 *     _render() fires _scheduleImageLoad(0). HA assigns hass only after setConfig.
 *     Without localStorage cache the spinner was visible for up to 60 s (first timer
 *     tick). Fixed: re-trigger _scheduleImageLoad(0) on first hass assignment when
 *     image hasn't loaded yet.
 *   - Smaller image requests: pass ?width=<display_width> on every camera proxy URL
 *     so HA forwards the hint to async_camera_image(). Backend already returns the
 *     320×180 RCP thumbnail (~3 KB) via proxy cache, so mobile downloads 3 KB
 *     instead of a 150 KB 1080p snap.jpg.
 *   - Snapshot button first poll: 1000 ms → 500 ms (RCP refresh is ~100 ms).
 *
 * Changes vs 1.6.0:
 *   - Event-driven snapshot refresh: when sensor.last_event changes (new motion/audio
 *     event detected), the card automatically refreshes the image after 2.5 s,
 *     without waiting for the 60 s timer. Works alongside the HA integration's own
 *     event-driven refresh (v2.8.0) for double-redundant coverage.
 *
 * Changes vs 1.5.11:
 *   - Page Visibility API for smart refresh intervals:
 *     Snapshot refreshes every 60 s when the HA dashboard is visible.
 *     Drops to every 1800 s (30 min) when the browser tab goes to background.
 *     Immediately refreshes when the tab returns to foreground.
 *     Replaces the old configurable refresh_interval_idle (now removed).
 *   - HA integration (v2.7.0): async_fetch_live_snapshot tries RCP 0x099e
 *     (320×180 JPEG via cloud proxy) before falling back to snap.jpg.
 *     Faster and lower bandwidth for idle thumbnail updates.
 *
 * Changes vs 1.5.10:
 *   - Added video quality dropdown inside card (select entity):
 *     Qualität: Auto / Hoch (30 Mbps) / Niedrig (1.9 Mbps)
 *     Hidden automatically when the select entity doesn't exist or is unavailable.
 *     Configure with quality_entity: select.bosch_xxx_video_quality in card YAML.
 *
 * Changes vs 1.5.9:
 *   - After panning, automatically refresh snapshot after 2s (camera needs time to move)
 *
 * Changes vs 1.5.8:
 *   - Added pan controls for 360 cameras (number.bosch_{cam}_pan_position entity)
 *     ◀◀ ◀ ■ ▶ ▶▶ buttons with current position display; hidden for cameras without pan support
 *
 * Changes vs 1.5.7:
 *   - Added Notifications toggle (mdi:bell / mdi:bell-off) using switch.bosch_{cam}_notifications
 *
 * Changes vs 1.4.8:
 *   - localStorage (not sessionStorage) → image survives iOS app restart
 *   - Live stream switches to HLS <video> with audio (via HA camera/stream WS)
 *   - Audio (Ton) toggle mutes/unmutes the live video in real-time
 *   - Optimistic UI: toggles (Ton/Licht/Privat/Stream) flip instantly on tap
 *   - Controls always visible — no collapsible Steuerung section
 *
 * Changes vs 1.5.2:
 *   - Retry on image error: if the first load fails (backend not ready yet on startup),
 *     automatically retry every 3 seconds up to 5 times before giving up.
 *
 * Changes vs 1.5.2 (continued):
 *   - hls.js support for Chrome/Firefox: HLS is not natively supported in Chrome;
 *     hls.js is loaded on demand from CDN. Safari/iOS continue to use native HLS.
 */
```

## Section Markers

Originally embedded as `// ── Section ──` comments in the JS file.

**Line 190:**
```
// ── Lifecycle ─────────────────────────────────────────────────────────────
```

**Line 196:**
```
// ── Config ────────────────────────────────────────────────────────────────
```

**Line 276:**
```
// ── HA state updates ──────────────────────────────────────────────────────
```

**Line 310:**
```
// ── Timer ─────────────────────────────────────────────────────────────────
```

**Line 363:**
```
// ── Full DOM render (once on setConfig) ───────────────────────────────────
```

**Line 1516:**
```
// ── Image lifecycle ───────────────────────────────────────────────────────
```

**Line 1652:**
```
// ── Image caching (localStorage — persists across iOS app restarts) ────────
```

**Line 1696:**
```
// ── Live HLS video ────────────────────────────────────────────────────────
```

**Line 1791:**
```
// ── WebRTC (try first if go2rtc is available) ─────────────────────
// go2rtc provides WebRTC (~2s latency vs ~12s HLS) when stream is active.
// Falls back to HLS if WebRTC is not available or fails.
```

**Line 1813:**
```
// ── HLS via camera/stream (fallback) ────────────────────────────────
```

**Line 2020:**
```
// ── Snapshot button ───────────────────────────────────────────────────────
```

**Line 2140:**
```
// ── State update ──────────────────────────────────────────────────────────
```

**Line 2614:**
```
// ── Schedules & Zones ──────────────────────────────────────────────────────
```

**Line 2817:**
```
// ── Helpers ───────────────────────────────────────────────────────────────
```

## Design Notes & Explanations

Multi-line `//` comment blocks that explain non-obvious logic, workarounds, or design decisions.

**Line 281:**
```
// _render() calls _scheduleImageLoad(0) before _hass is assigned (HA sets hass
// AFTER setConfig), so the first image load silently returns early.
// Always fetch fresh on first hass — even when localStorage cache is showing an
// old image. Show a "refreshing" overlay so the user knows it's updating.
```

**Line 286:**
```
// _awaitingFresh is already true if _restoreCachedImage found a cache.
// For the no-cache case, set it now before triggering any image loads.
```

**Line 289:**
```
// If cache already showed the "refreshing" overlay, this is a no-op.
// If no cache, this shows the full spinner.
```

**Line 354:**
```
// Tell backend to fetch a fresh image and bypass HA's 60s frame_interval cache.
// _force_image_refresh makes frame_interval=0.1s so the next proxy request
// actually calls async_camera_image instead of returning HA's internal cache.
// Cloud API response varies (1.5–5s), so fetch at 1.5s and 4s.
```

**Line 1535:**
```
// Request at display width — HA passes this to async_camera_image(width=).
// Our backend already prefers the 320×180 RCP thumbnail (~3 KB) which is
// well within 640 px. This avoids serving 1080p (~150 KB) to mobile.
```

**Line 1567:**
```
// Overlay management:
// - Cache image + awaitingFresh → keep "refreshing" overlay visible
// - Fresh image (non-cache) → always clear overlay
// - Cache image + NOT awaitingFresh → clear overlay (normal idle refresh)
```

**Line 1572:**
```
// Cache loaded — keep spinner visible, fresh image will clear it.
// But ensure the overlay is in "refreshing" mode (semi-transparent)
// so the cached image is visible underneath.
```

**Line 1596:**
```
// Uptime counter is handled by its own setInterval (_uptimeTimer) — no update needed here.
// Store image to localStorage so next app launch shows it instantly.
// Skip during streaming — live frames change every 2s so per-frame I/O is wasteful.
// After stream stops, _isStreaming() returns false → the post-stop refresh image
// IS saved, keeping localStorage as fresh as possible without excess writes.
```

**Line 1640:**
```
// Safety timeout — shorter for snapshot refreshes, longer during stream start.
// During stream start (_startingLiveVideo or _waitingForStream), the overlay
// should stay visible until the video actually plays (outdoor cam takes 80s+).
```

**Line 1654:**
```
// Immediately show last known image from localStorage — no wait for proxy.
// Shows the cached image underneath a semi-transparent "refreshing" overlay
// so the user sees something while we fetch a fresh image.
```

**Line 1664:**
```
// Mark that we'll need a fresh image — set hass() will show the
// "refreshing" overlay and trigger a snapshot fetch.
```

**Line 1667:**
```
// Switch from full-black spinner to semi-transparent "refreshing" overlay
// so the cached image is visible underneath.
```

**Line 1730:**
```
// Keep snapshot image visible until video actually plays — avoids
// black screen gap between image hide and first video frame.
```

**Line 1745:**
```
// Safety timeout: if video never plays after 120s, hide overlay but
// keep snapshot visible (don't call clearOverlay which hides the image).
// Outdoor camera can take 80s+ for first HLS frame.
```

**Line 1754:**
```
// Video still not playing — hide overlay spinner only,
// keep snapshot image visible underneath
```

**Line 1792:**
```
// go2rtc provides WebRTC (~2s latency vs ~12s HLS) when stream is active.
// Falls back to HLS if WebRTC is not available or fails.
```

**Line 1821:**
```
// Always start muted to comply with Chrome autoplay policy.
// Chrome blocks unmuted autoplay without prior user interaction.
// Audio is controlled by the user via the audio toggle in the card.
```

**Line 1829:**
```
// Video is playing muted. User can unmute via audio toggle.
// Do NOT auto-unmute — Chrome will pause the video.
```

**Line 1848:**
```
// CRITICAL: maxBufferLength MUST be < HA's OUTPUT_IDLE_TIMEOUT (30s).
// If hls.js buffers ≥30s, it stops requesting segments → HA thinks
// nobody is watching → kills FFmpeg → video freezes on last frame.
```

**Line 1899:**
```
// HLS keepalive: prevent HA's 30s idle timeout from killing FFmpeg.
// Even with maxBufferLength=10, belt-and-suspenders measure.
```

**Line 1920:**
```
// After 5 attempts, back off but DON'T give up permanently.
// Schedule a retry in 10s — the stream may still be starting.
```

**Line 2051:**
```
// Capture current image byte count, then poll until it changes (new image ready)
// REMOTE takes ~3-5s; LOCAL Digest auth takes ~6-15s
```

**Line 2191:**
```
// "connecting" while HLS is negotiating (startingLiveVideo), "streaming" once live,
// "idle" when off. Badge label shows uptime counter once streaming (updated per frame).
```

**Line 2232:**
```
// shouldVideo: always use HLS video when stream is ON.
// Audio toggle only controls mute/unmute — no more snapshot-polling mode.
```

**Line 2247:**
```
// Start HLS video when stream turns ON.
// Wait until camera entity actually reports streaming (stream_source set)
// to avoid "does not support play stream" errors from premature WS calls.
// Show loading overlay during the wait (outdoor pre-warm takes ~35s).
// Also re-triggers if card got stuck (e.g. WS failed during page load).
```

**Line 2532:**
```
// Keep live video muted state in sync with Ton toggle (only when streaming).
// Only unmute when the video is already playing — unmuting a paused video
// before play() is called would cause an autoplay NotAllowedError.
```

**Line 2599:**
```
// Hide when entity doesn't exist or is unavailable/unknown
// (e.g. camera light on a camera that has no physical light)
```

**Line 2824:**
```
// Poll until camera entity reports "streaming" (stream_source is set).
// Backend needs 25-35s for PUT /connection + TLS proxy + pre-warm
// (outdoor camera is slower). Only then call camera/stream WS to avoid
// "does not support play stream" errors from premature WS calls.
```

**Line 2894:**
```
// Gen1 cloud zones: {x, y, w, h} normalized 0.0–1.0
// ViewBox is 0-100, so multiply by 100.
```

**Line 2961:**
```
// Starting stream → show loading overlay with progressive status updates
// Timeline: PUT /connection ~2s, TLS proxy ~0.5s, pre-warm ~3s,
// go2rtc RTSP connect ~5s, HLS segment generation ~10-15s, first frame ~25-35s total.
```

**Line 2966:**
```
// Progressive status messages — each _setLoadingOverlay resets the 15s
// safety timeout, so messages must be spaced <15s apart to keep the
// spinner alive. LOCAL streams can take up to 60s on first connect.
```
