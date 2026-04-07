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
 * Version: 2.6.0
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

class BoschCameraCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass           = null;
    this._config         = null;
    this._refreshTimer   = null;
    this._imgTimestamp   = Date.now();
    this._lastStreaming   = null;    // last known streaming state (true/false/null)
    this._streamConnecting = false;  // true while stream is connecting (overlay shown)
    this._connectSteps     = null;   // setTimeout IDs for progressive overlay text
    this._waitingForStream = false;  // true while waiting for backend stream ready
    this._lastMotionCoordKey = null; // memoization key for motion zone SVG
    this._lastPrivacy    = null;    // last known privacy state (true/false/null)
    this._imageLoaded    = false;   // did we ever successfully load an image?
    this._loadingOverlay = false;   // is the "Wird geladen" overlay active?
    this._loadingTimeout = null;    // safety timeout to hide overlay
    this._storageKey     = null;    // localStorage key for cached image dataURL
    this._loadRetries    = 0;       // retry counter for initial image load (max 5)
    this._snapshotPollTimer = null; // polling timer during snapshot refresh
    this._liveVideoActive   = false; // true when HLS <video> is playing
    this._startingLiveVideo = false; // true while _startLiveVideo() is in progress
    this._hls               = null;  // hls.js instance for Chrome (null = native or inactive)
    this._timerStreaming     = false; // whether refresh timer is running at streaming interval
    this._optimistic        = {};    // optimistic entity states { entityId: "on"/"off" }
    this._optimisticTimers  = {};    // timers to auto-clear optimistic states
    this._visibilityHandler = null;  // bound visibilitychange listener
    this._lastEventState    = null;  // last known last_event sensor value — for event detection
    this._lastFrameTime     = 0;    // monotonic ms of last fresh frame — for Δt debug display
    this._streamStartTime   = 0;    // ms when current stream session started — for uptime counter
    this._awaitingFresh     = false; // true while waiting for a fresh (non-cache) image
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────────
  connectedCallback() {
    this._visibilityHandler = () => this._onVisibilityChange();
    document.addEventListener("visibilitychange", this._visibilityHandler);
  }

  // ── Config ────────────────────────────────────────────────────────────────
  setConfig(config) {
    if (!config.camera_entity) {
      throw new Error("bosch-camera-card: camera_entity is required");
    }
    this._config = {
      camera_entity:              config.camera_entity,
      title:                      config.title || null,
      refresh_interval_streaming: config.refresh_interval_streaming ?? 2,
      show_motion_zones:         config.show_motion_zones ?? false,
      // idle refresh is handled by Page Visibility API: 60 s visible, 1800 s background
    };

    this._storageKey = `bosch_cam_${config.camera_entity}`;

    const base = config.camera_entity.replace(/^camera\./, "");
    this._entities = {
      camera:       config.camera_entity,
      switch:       config.switch_entity        || `switch.${base}_live_stream`,
      audio:        config.audio_entity         || `switch.${base}_audio`,
      light:        config.light_entity         || `switch.${base}_camera_light`,
      frontLight:   config.front_light_entity   || `switch.${base}_front_light`,
      wallwasher:   config.wallwasher_entity    || `switch.${base}_wallwasher`,
      frontIntensity: config.front_intensity_entity || `number.${base}_front_light_intensity`,
      privacy:      config.privacy_entity       || `switch.${base}_privacy_mode`,
      notifications: config.notifications_entity || `switch.${base}_notifications`,
      intercom:     config.intercom_entity      || `switch.${base}_intercom`,
      speaker:      config.speaker_entity       || `number.${base}_speaker_level`,
      pan:          config.pan_entity           || `number.${base}_pan_position`,
      quality:      config.quality_entity       || null,
      push_status:  config.push_status_entity   || "sensor.bosch_camera_event_detection",
      status:       config.status_entity        || `sensor.${base}_status`,
      events_today: config.events_today_entity  || `sensor.${base}_events_today`,
      last_event:   config.last_event_entity    || `sensor.${base}_last_event`,
      timestamp:     config.timestamp_entity     || `switch.${base}_timestamp_overlay`,
      autofollow:    config.autofollow_entity    || `switch.${base}_auto_follow`,
      motion:        config.motion_entity        || `switch.${base}_motion_detection`,
      recordSound:   config.record_sound_entity  || `switch.${base}_record_sound`,
      privacySound:  config.privacy_sound_entity || `switch.${base}_privacy_sound`,
      notifMovement: config.notif_movement_entity || `switch.${base}_movement_notifications`,
      notifPerson:   config.notif_person_entity   || `switch.${base}_person_notifications`,
      notifAudio:    config.notif_audio_entity    || `switch.${base}_audio_notifications`,
      notifTrouble:  config.notif_trouble_entity  || `switch.${base}_trouble_notifications`,
      notifAlarm:    config.notif_alarm_entity    || `switch.${base}_camera_alarm_notifications`,
      wifi:          config.wifi_entity          || `sensor.${base}_wifi_signal`,
      firmware:      config.firmware_entity      || `sensor.${base}_firmware_version`,
      ambient:       config.ambient_entity       || `sensor.${base}_ambient_light`,
      movementToday: config.movement_today_entity || `sensor.${base}_movement_events_today`,
      audioToday:    config.audio_today_entity    || `sensor.${base}_audio_events_today`,
      motionZones:   config.motion_zones_entity   || `sensor.${base}_motion_zones`,
    };

    this._render();
    this._restoreCachedImage();
    this._startRefreshTimer();
    // Pre-load hls.js in the background so it's cached when the user starts the stream
    this._loadHlsJs().catch(() => {});
  }

  // ── HA state updates ──────────────────────────────────────────────────────
  set hass(hass) {
    const firstHass = !this._hass;
    this._hass = hass;
    this._update();
    // _render() calls _scheduleImageLoad(0) before _hass is assigned (HA sets hass
    // AFTER setConfig), so the first image load silently returns early.
    // Always fetch fresh on first hass — even when localStorage cache is showing an
    // old image. Show a "refreshing" overlay so the user knows it's updating.
    if (firstHass) {
      // _awaitingFresh is already true if _restoreCachedImage found a cache.
      // For the no-cache case, set it now before triggering any image loads.
      this._awaitingFresh = true;
      // If cache already showed the "refreshing" overlay, this is a no-op.
      // If no cache, this shows the full spinner.
      if (this._imageLoaded) {
        this._setLoadingOverlay(true, "Aktualisiere…");
      }
      this._triggerFreshSnapshot();
    }
  }

  disconnectedCallback() {
    this._stopRefreshTimer();
    if (this._visibilityHandler) {
      document.removeEventListener("visibilitychange", this._visibilityHandler);
      this._visibilityHandler = null;
    }
    if (this._loadingTimeout)    clearTimeout(this._loadingTimeout);
    if (this._snapshotPollTimer) clearTimeout(this._snapshotPollTimer);
    Object.values(this._optimisticTimers).forEach(t => clearTimeout(t));
    this._stopLiveVideo();
  }

  // ── Timer ─────────────────────────────────────────────────────────────────
  _startRefreshTimer() {
    this._stopRefreshTimer();
    // No snapshot polling when live video (HLS) is playing or starting
    if (this._liveVideoActive || this._startingLiveVideo) return;
    // When streaming is active, HLS handles video — no snapshot polling needed.
    if (this._isStreaming()) return;
    let interval;
    if (document.visibilityState === "hidden") {
      interval = 1800; // 30 min — page is in background, save resources
    } else {
      interval = 60;   // 1 min — page is visible
    }
    this._refreshTimer = setInterval(() => {
      this._triggerFreshSnapshot();
    }, interval * 1000);
  }

  _onVisibilityChange() {
    if (document.visibilityState === "visible" && !this._liveVideoActive) {
      // Page just came to foreground — trigger fresh snapshot like on page load
      this._triggerFreshSnapshot();
    }
    // Restart timer with the correct interval (60 s or 1800 s)
    this._startRefreshTimer();
  }

  _stopRefreshTimer() {
    if (this._refreshTimer) { clearInterval(this._refreshTimer); this._refreshTimer = null; }
  }

  _isStreaming() {
    if (!this._hass) return false;
    const switchId = this._entities.switch;
    // Check optimistic state first (immediate feedback after button press)
    if (switchId in this._optimistic) return this._optimistic[switchId] === "on";
    const sw = this._hass.states[switchId];
    if (sw) return sw.state === "on";
    const cam = this._hass.states[this._entities.camera];
    if (cam?.attributes?.streaming_state) return cam.attributes.streaming_state === "active";
    return cam?.state === "streaming";
  }

  _triggerFreshSnapshot() {
    // Tell backend to fetch a fresh image and bypass HA's 60s frame_interval cache.
    // _force_image_refresh makes frame_interval=0.1s so the next proxy request
    // actually calls async_camera_image instead of returning HA's internal cache.
    // Cloud API response varies (1.5–5s), so fetch at 1.5s and 4s.
    this._callService("bosch_shc_camera", "trigger_snapshot", {});
    this._scheduleImageLoad(1500);
    this._scheduleImageLoad(4000);
  }

  // ── Full DOM render (once on setConfig) ───────────────────────────────────
  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; font-family: var(--primary-font-family, Roboto, sans-serif); }
        ha-card {
          overflow: hidden;
          border-radius: var(--ha-card-border-radius, 12px);
          background: var(--ha-card-background, var(--card-background-color, #1c1c1e));
          box-shadow: var(--ha-card-box-shadow, 0 2px 8px rgba(0,0,0,.3));
        }

        /* Header */
        .header {
          display: flex; align-items: center; justify-content: space-between;
          padding: 12px 14px 8px;
        }
        .header-left { display: flex; align-items: center; gap: 8px; }
        .title {
          font-size: 15px; font-weight: 600;
          color: var(--primary-text-color, #e5e5ea);
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .status-dot {
          width: 8px; height: 8px; border-radius: 50%;
          background: #636366; flex-shrink: 0; transition: background 0.3s;
        }
        .status-dot.online  { background: #30d158; }
        .status-dot.offline { background: #ff453a; }

        /* Stream badge */
        .stream-badge {
          display: inline-flex; align-items: center; gap: 5px;
          font-size: 11px; font-weight: 600; letter-spacing: .4px;
          text-transform: uppercase; padding: 3px 8px; border-radius: 20px;
          transition: all 0.3s; white-space: nowrap;
        }
        .stream-badge.idle       { background: rgba(99,99,102,.25); color: #8e8e93; }
        .stream-badge.streaming  { background: rgba(0,122,255,.2); color: #0a84ff; box-shadow: 0 0 0 1px rgba(0,122,255,.3); }
        .stream-badge.connecting { background: rgba(255,159,10,.2); color: #ff9f0a; box-shadow: 0 0 0 1px rgba(255,159,10,.3); }
        .stream-badge .dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
        .stream-badge.idle .dot       { background: #636366; }
        .stream-badge.streaming .dot  { background: #0a84ff; animation: pulse 1.5s infinite; }
        .stream-badge.connecting .dot { background: #ff9f0a; animation: pulse 0.8s infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

        /* Push status badge */
        .push-badge {
          display: inline-flex; align-items: center; gap: 4px;
          font-size: 10px; font-weight: 600; letter-spacing: .3px;
          text-transform: uppercase; padding: 2px 6px; border-radius: 12px;
          white-space: nowrap;
        }
        .push-badge.fcm  { background: rgba(48,209,88,.15); color: #30d158; }
        .push-badge.poll { background: rgba(99,99,102,.2); color: #8e8e93; }
        .push-badge .pdot { width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0; }
        .push-badge.fcm .pdot  { background: #30d158; }
        .push-badge.poll .pdot { background: #636366; }

        /* Connection type badge (LAN / Cloud) */
        .conn-badge {
          display: inline-flex; align-items: center; gap: 4px;
          font-size: 10px; font-weight: 600; letter-spacing: .3px;
          padding: 2px 7px; border-radius: 12px; white-space: nowrap;
        }
        .conn-badge.local  { background: rgba(48,209,88,.15); color: #30d158; }
        .conn-badge.remote { background: rgba(99,99,102,.2); color: #8e8e93; }
        .conn-badge.hidden { display: none; }

        /* Camera image area */
        .img-wrapper { position: relative; width: 100%; background: #000; line-height: 0; aspect-ratio: 16/9; }
        .cam-img {
          width: 100%; height: 100%; display: block; object-fit: cover;
          min-height: 160px; transition: opacity 0.3s;
        }
        .cam-img.hidden { opacity: 0; }

        /* Live video element */
        .cam-video {
          width: 100%; height: 100%; display: block; object-fit: cover;
          min-height: 160px; background: #000;
        }

        /* Fullscreen — native API (desktop/Android) */
        .img-wrapper:fullscreen,
        .img-wrapper:-webkit-full-screen {
          background: #000;
          display: flex; align-items: center; justify-content: center;
          width: 100vw; height: 100vh;
        }
        .img-wrapper:fullscreen .cam-img,
        .img-wrapper:-webkit-full-screen .cam-img,
        .img-wrapper:fullscreen .cam-video,
        .img-wrapper:-webkit-full-screen .cam-video {
          width: 100vw; height: 100vh;
          object-fit: contain; min-height: unset;
        }
        /* Fullscreen — CSS fallback for iOS Safari (position:fixed overlay) */
        :host(.fs-active) {
          position: fixed !important; inset: 0 !important;
          z-index: 9999 !important; background: #000 !important;
          display: flex !important; align-items: center !important; justify-content: center !important;
        }
        /* Hide header, controls and other elements in fullscreen */
        :host(.fs-active) .header,
        :host(.fs-active) .info-row,
        :host(.fs-active) .btn-row,
        :host(.fs-active) .switch-rows,
        :host(.fs-active) .quality-section,
        :host(.fs-active) .accordion { display: none !important; }
        :host(.fs-active) .img-wrapper { aspect-ratio: unset; width: 100vw; height: 100vh; }
        :host(.fs-active) .cam-img,
        :host(.fs-active) .cam-video { object-fit: contain; min-height: unset; }
        :host(.fs-active) ha-card { width: 100vw; height: 100vh; border-radius: 0 !important; overflow: hidden; }
        :host(.fs-active) .cam-img,
        :host(.fs-active) .cam-video { width: 100vw; height: 100vh; object-fit: contain; min-height: unset; }

        /* Motion zones SVG overlay */
        .motion-zones-overlay {
          position: absolute; inset: 0; z-index: 5;
          width: 100%; height: 100%;
          pointer-events: none; opacity: 0;
          transition: opacity 0.3s;
        }
        .motion-zones-overlay.visible { opacity: 1; }
        .motion-zones-overlay rect {
          fill: rgba(0, 122, 255, 0.15);
          stroke: rgba(0, 122, 255, 0.6);
          stroke-width: 0.5;
        }
        .motion-zones-overlay rect:nth-child(2) { fill: rgba(52, 199, 89, 0.15); stroke: rgba(52, 199, 89, 0.6); }
        .motion-zones-overlay rect:nth-child(3) { fill: rgba(255, 159, 10, 0.15); stroke: rgba(255, 159, 10, 0.6); }
        .motion-zones-overlay rect:nth-child(4) { fill: rgba(255, 69, 58, 0.15); stroke: rgba(255, 69, 58, 0.6); }
        .motion-zones-overlay rect:nth-child(5) { fill: rgba(175, 82, 222, 0.15); stroke: rgba(175, 82, 222, 0.6); }

        /* Loading overlay — must be above both cam-img and cam-video */
        .loading-overlay {
          position: absolute; inset: 0; z-index: 10;
          display: flex; flex-direction: column; align-items: center; justify-content: center;
          background: rgba(0,0,0,.85);
          gap: 12px;
          opacity: 0; transition: opacity 0.3s; pointer-events: none;
        }
        .loading-overlay.visible { opacity: 1; pointer-events: auto; }
        /* Semi-transparent overlay when refreshing an existing image — old image stays visible, spinner on top */
        .loading-overlay.refreshing { background: rgba(0,0,0,.4); }
        .spinner {
          width: 36px; height: 36px;
          border: 3px solid rgba(255,255,255,.2);
          border-top-color: #fff;
          border-radius: 50%;
          animation: spin 0.8s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .loading-text {
          font-size: 13px; color: rgba(255,255,255,.75); font-weight: 500;
        }

        /* Image overlay (last event / events today) */
        .img-overlay {
          position: absolute; bottom: 0; left: 0; right: 0;
          padding: 20px 12px 8px;
          background: linear-gradient(transparent, rgba(0,0,0,.55));
          display: flex; align-items: flex-end; justify-content: space-between;
          pointer-events: none;
        }
        .last-event-overlay, .events-overlay { font-size: 11px; color: rgba(255,255,255,.8); }

        /* Info row */
        .info-row {
          display: flex; align-items: center; justify-content: space-between;
          padding: 8px 14px; gap: 10px;
        }
        .info-item { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
        .info-label {
          font-size: 10px; text-transform: uppercase; letter-spacing: .5px;
          color: var(--secondary-text-color, #8e8e93);
        }
        .info-value {
          font-size: 13px; color: var(--primary-text-color, #e5e5ea);
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }

        /* Buttons */
        .btn-row { display: flex; gap: 8px; padding: 0 12px 12px; }
        .btn {
          flex: 1; display: flex; align-items: center; justify-content: center;
          gap: 6px; padding: 9px 10px; border-radius: 10px; border: none;
          cursor: pointer; font-size: 13px; font-weight: 500; font-family: inherit;
          transition: opacity 0.15s, transform 0.1s;
          -webkit-tap-highlight-color: transparent;
        }
        .btn:active { transform: scale(.97); opacity: .8; }
        .btn:disabled { opacity: .5; cursor: default; }
        .btn-snapshot { background: rgba(99,99,102,.2); color: var(--primary-text-color, #e5e5ea); }
        .btn-snapshot.loading { background: rgba(99,99,102,.35); }
        .btn-stream    { background: rgba(10,132,255,.18); color: #0a84ff; }
        .btn-stream.active { background: rgba(255,69,58,.18); color: #ff453a; }
        .btn-fullscreen { background: rgba(99,99,102,.15); color: var(--secondary-text-color, #8e8e93); flex: 0 0 auto; padding: 9px 12px; }
        .btn svg { width: 16px; height: 16px; flex-shrink: 0; }
        .btn-spinner {
          width: 14px; height: 14px;
          border: 2px solid rgba(255,255,255,.3);
          border-top-color: currentColor;
          border-radius: 50%;
          animation: spin 0.8s linear infinite;
          flex-shrink: 0;
        }

        /* Switch rows — Ton / Licht / Privat */
        .switch-rows { display: flex; flex-direction: column; padding: 0 12px 12px; gap: 2px; }
        .sw-row {
          display: flex; align-items: center; justify-content: space-between;
          padding: 9px 4px; cursor: pointer; border-radius: 8px;
          -webkit-tap-highlight-color: transparent;
          transition: background 0.15s;
        }
        .sw-row:active { background: rgba(99,99,102,.12); }
        .sw-left {
          display: flex; align-items: center; gap: 10px;
          color: var(--primary-text-color, #e5e5ea); font-size: 13px; font-weight: 500;
        }
        .sw-left svg { width: 18px; height: 18px; flex-shrink: 0; color: var(--secondary-text-color, #8e8e93); }
        .sw-row.on .sw-left svg { color: #0a84ff; }
        .sw-row.privacy-row.on .sw-left svg { color: #ff453a; }
        /* iOS-style toggle */
        .sw-toggle {
          width: 44px; height: 26px; border-radius: 13px;
          background: rgba(99,99,102,.4); border: none; padding: 0;
          position: relative; flex-shrink: 0; cursor: pointer;
          transition: background 0.25s;
        }
        .sw-row.on    .sw-toggle { background: #30d158; }
        .sw-row.privacy-row.on .sw-toggle { background: #ff453a; }
        .sw-thumb {
          width: 22px; height: 22px; border-radius: 50%; background: #fff;
          position: absolute; top: 2px; left: 2px;
          box-shadow: 0 1px 4px rgba(0,0,0,.4);
          transition: transform 0.25s cubic-bezier(.4,0,.2,1);
        }
        .sw-row.on .sw-thumb { transform: translateX(18px); }

        /* Privacy placeholder — shown when no image + privacy mode is ON */
        .privacy-placeholder {
          position: absolute; inset: 0;
          display: flex; flex-direction: column; align-items: center; justify-content: center;
          background: rgba(0,0,0,.82); gap: 10px;
          opacity: 0; transition: opacity 0.3s; pointer-events: none;
        }
        .privacy-placeholder.visible { opacity: 1; }
        .privacy-placeholder svg { width: 44px; height: 44px; color: rgba(255,255,255,.35); }
        .privacy-placeholder span { font-size: 13px; color: rgba(255,255,255,.45); font-weight: 500; }

        /* Quality select */
        .quality-section { padding: 0 12px 12px; }
        .quality-row { display: flex; align-items: center; gap: 10px; }
        .quality-label { font-size: 13px; color: var(--secondary-text-color, #8e8e93); flex-shrink: 0; }
        .quality-select {
          flex: 1; background: rgba(255,255,255,.1); border: 1px solid rgba(255,255,255,.12);
          border-radius: 8px; color: var(--primary-text-color, #e5e5ea); font-size: 13px;
          padding: 6px 10px; cursor: pointer; font-family: inherit;
          -webkit-appearance: none; appearance: none;
        }
        .quality-select:focus { outline: none; background: rgba(255,255,255,.15); }
        .quality-select option { background: #2c2c2e; color: #e5e5ea; }

        /* Pan controls */
        .pan-section { padding: 0 12px 12px; }
        .pan-row { display: flex; align-items: center; gap: 6px; }
        .pan-btn {
          background: rgba(128,128,128,.15); border: none; border-radius: 6px;
          color: var(--primary-text-color, #333); cursor: pointer; padding: 6px 10px; flex: 1;
          font-family: inherit; -webkit-tap-highlight-color: transparent;
          transition: background 0.15s;
          display: flex; align-items: center; justify-content: center;
        }
        .pan-btn svg { width: 18px; height: 18px; flex-shrink: 0; }
        .pan-btn:hover  { background: rgba(128,128,128,.25); }
        .pan-btn:active { background: rgba(128,128,128,.35); }
        .pan-pos { margin-left: auto; font-size: 12px; opacity: .7; color: var(--primary-text-color, #e5e5ea); white-space: nowrap; }

        /* Accordion sections */
        .accordion { border-top: 1px solid rgba(255,255,255,.06); }
        .accordion-header {
          display: flex; align-items: center; justify-content: space-between;
          padding: 10px 14px; cursor: pointer;
          -webkit-tap-highlight-color: transparent;
          transition: background 0.15s;
        }
        .accordion-header:active { background: rgba(99,99,102,.08); }
        .accordion-title {
          font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .5px;
          color: var(--secondary-text-color, #8e8e93);
        }
        .accordion-chevron {
          width: 16px; height: 16px; color: var(--secondary-text-color, #8e8e93);
          transition: transform 0.25s ease;
          flex-shrink: 0;
        }
        .accordion.open .accordion-chevron { transform: rotate(180deg); }
        .accordion-body {
          max-height: 0; overflow: hidden;
          transition: max-height 0.3s ease;
        }
        .accordion.open .accordion-body { max-height: 600px; }
        .accordion-content { padding: 0 12px 12px; }
        .accordion-content .sw-row { padding: 7px 4px; }

        /* Diagnostic row inside accordion */
        .diag-row {
          display: flex; align-items: center; justify-content: space-between;
          padding: 6px 4px;
        }
        .diag-label {
          font-size: 13px; color: var(--secondary-text-color, #8e8e93);
          display: flex; align-items: center; gap: 8px;
        }
        .diag-label svg { width: 16px; height: 16px; flex-shrink: 0; }
        .diag-value {
          font-size: 13px; color: var(--primary-text-color, #e5e5ea); font-weight: 500;
        }
      </style>

      <ha-card>
        <div class="header">
          <div class="header-left">
            <div class="status-dot unknown" id="status-dot"></div>
            <span class="title" id="title">Bosch Camera</span>
          </div>
          <div style="display:flex;align-items:center;gap:6px">
            <div class="push-badge poll" id="push-badge">
              <div class="pdot"></div>
              <span id="push-label">poll</span>
            </div>
            <div class="conn-badge hidden" id="conn-badge"></div>
            <div class="stream-badge idle" id="stream-badge">
              <div class="dot"></div>
              <span id="stream-label">idle</span>
            </div>
          </div>
        </div>

        <div class="img-wrapper" id="img-wrapper">
          <img class="cam-img hidden" id="cam-img" alt="Camera" style="cursor:pointer" />
          <video class="cam-video" id="cam-video" autoplay playsinline style="display:none; cursor:pointer"></video>
          <div class="loading-overlay visible" id="loading-overlay">
            <div class="spinner"></div>
            <span class="loading-text" id="loading-text">Bild wird geladen…</span>
          </div>
          <div class="privacy-placeholder" id="privacy-placeholder">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
              <path d="M7 11V7a5 5 0 0110 0v4"/>
            </svg>
            <span>Privat-Modus aktiv</span>
          </div>
          <svg class="motion-zones-overlay" id="motion-zones-overlay" viewBox="0 0 100 100" preserveAspectRatio="none"></svg>
          <div class="img-overlay">
            <span class="last-event-overlay" id="last-event-overlay"></span>
            <span class="events-overlay" id="events-overlay"></span>
          </div>
        </div>

        <div class="info-row">
          <div class="info-item">
            <span class="info-label">Status</span>
            <span class="info-value" id="info-status">—</span>
          </div>
          <div class="info-item">
            <span class="info-label">Letztes Event</span>
            <span class="info-value" id="info-last-event">—</span>
          </div>
          <div class="info-item" style="text-align:right">
            <span class="info-label">Heute</span>
            <span class="info-value" id="info-events-today">—</span>
          </div>
        </div>

        <div class="btn-row">
            <button class="btn btn-snapshot" id="btn-snapshot">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/>
                <circle cx="12" cy="13" r="4"/>
              </svg>
              <span id="btn-snapshot-label">Snapshot</span>
            </button>
            <button class="btn btn-stream" id="btn-stream">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polygon points="23 7 16 12 23 17 23 7"/>
                <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
              </svg>
              <span id="btn-stream-label">Live Stream</span>
            </button>
            <button class="btn btn-fullscreen" id="btn-fullscreen" title="Vollbild">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M8 3H5a2 2 0 00-2 2v3m18 0V5a2 2 0 00-2-2h-3m0 18h3a2 2 0 002-2v-3M3 16v3a2 2 0 002 2h3"/>
              </svg>
            </button>
          </div>

          <div class="switch-rows">
            <div class="sw-row" id="btn-audio">
              <div class="sw-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                  <path d="M19.07 4.93a10 10 0 010 14.14M15.54 8.46a5 5 0 010 7.07"/>
                </svg>
                <span>Ton / Video</span>
              </div>
              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
            </div>
            <div class="sw-row" id="btn-light">
              <div class="sw-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <circle cx="12" cy="12" r="5"/>
                  <line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/>
                  <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
                  <line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/>
                  <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
                </svg>
                <span>Licht</span>
              </div>
              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
            </div>
            <div class="sw-row" id="btn-front-light" style="padding-left:28px">
              <div class="sw-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
                <span>Frontlicht</span>
              </div>
              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
            </div>
            <div class="sw-row" id="btn-wallwasher" style="padding-left:28px">
              <div class="sw-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>
                <span>Wallwasher</span>
              </div>
              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
            </div>
            <div class="sw-row" id="row-front-intensity" style="padding-left:28px;padding-right:12px">
              <div class="sw-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/></svg>
                <span>Helligkeit</span>
              </div>
              <input type="range" id="slider-front-intensity" min="0" max="100" step="5" style="flex:1;margin-left:8px;accent-color:#4fc3f7">
              <span id="val-front-intensity" style="min-width:36px;text-align:right;font-size:13px;color:#aaa">—</span>
            </div>
            <div class="sw-row privacy-row" id="btn-privacy">
              <div class="sw-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
                  <path d="M7 11V7a5 5 0 0110 0v4"/>
                </svg>
                <span>Privat</span>
              </div>
              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
            </div>
            <div class="sw-row" id="btn-notifications">
              <div class="sw-left">
                <svg id="notif-icon-on" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9"/>
                  <path d="M13.73 21a2 2 0 01-3.46 0"/>
                </svg>
                <svg id="notif-icon-off" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display:none">
                  <path d="M13.73 21a2 2 0 01-3.46 0"/>
                  <path d="M18.63 13A17.89 17.89 0 0118 8"/>
                  <path d="M6.26 6.26A5.86 5.86 0 006 8c0 7-3 9-3 9h14"/>
                  <path d="M18 8a6 6 0 00-9.33-5"/>
                  <line x1="1" y1="1" x2="23" y2="23"/>
                </svg>
                <span>Benachrichtigungen</span>
              </div>
              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
            </div>
            <div class="sw-row" id="btn-intercom" style="display:none">
              <div class="sw-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/>
                  <path d="M19 10v2a7 7 0 01-14 0v-2"/>
                  <line x1="12" y1="19" x2="12" y2="23"/>
                  <line x1="8" y1="23" x2="16" y2="23"/>
                </svg>
                <span>Gegensprech.</span>
              </div>
              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
            </div>
          </div>

          <div class="pan-section" id="pan-section" style="display:none">
            <div class="pan-row">
              <button class="pan-btn" id="pan-full-left"  title="Ganz links">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                  <polyline points="11 18 5 12 11 6"/><polyline points="18 18 12 12 18 6"/>
                </svg>
              </button>
              <button class="pan-btn" id="pan-left"       title="Links">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                  <polyline points="15 18 9 12 15 6"/>
                </svg>
              </button>
              <button class="pan-btn" id="pan-center"     title="Mitte">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <circle cx="12" cy="12" r="3"/>
                  <line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/>
                  <line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/>
                </svg>
              </button>
              <button class="pan-btn" id="pan-right"      title="Rechts">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                  <polyline points="9 18 15 12 9 6"/>
                </svg>
              </button>
              <button class="pan-btn" id="pan-full-right" title="Ganz rechts">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                  <polyline points="13 18 19 12 13 6"/><polyline points="6 18 12 12 6 6"/>
                </svg>
              </button>
              <span   class="pan-pos" id="pan-position">0°</span>
            </div>
          </div>

          <div class="quality-section" id="quality-section" style="display:none">
            <div class="quality-row">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                   style="width:16px;height:16px;flex-shrink:0;color:var(--secondary-text-color,#8e8e93)">
                <rect x="2" y="7" width="20" height="15" rx="2"/>
                <polyline points="17 2 12 7 7 2"/>
              </svg>
              <span class="quality-label">Qualität</span>
              <select class="quality-select" id="quality-select">
                <option value="Auto">Auto</option>
                <option value="Hoch (30 Mbps)">Hoch (30 Mbps)</option>
                <option value="Niedrig (1.9 Mbps)">Niedrig (1.9 Mbps)</option>
              </select>
            </div>
          </div>

          <!-- Accordion: Notification Types -->
          <div class="accordion" id="acc-notif-types">
            <div class="accordion-header" id="acc-notif-types-header">
              <span class="accordion-title">Benachrichtigungs-Typen</span>
              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
            </div>
            <div class="accordion-body">
              <div class="accordion-content">
                <div class="sw-row" id="btn-notif-movement">
                  <div class="sw-left">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                    <span>Bewegung</span>
                  </div>
                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
                </div>
                <div class="sw-row" id="btn-notif-person">
                  <div class="sw-left">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
                    <span>Person</span>
                  </div>
                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
                </div>
                <div class="sw-row" id="btn-notif-audio">
                  <div class="sw-left">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 010 14.14"/></svg>
                    <span>Audio</span>
                  </div>
                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
                </div>
                <div class="sw-row" id="btn-notif-trouble">
                  <div class="sw-left">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                    <span>Störung</span>
                  </div>
                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
                </div>
                <div class="sw-row" id="btn-notif-alarm">
                  <div class="sw-left">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
                    <span>Kamera-Alarm</span>
                  </div>
                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
                </div>
              </div>
            </div>
          </div>

          <!-- Accordion: Advanced Controls -->
          <div class="accordion" id="acc-advanced">
            <div class="accordion-header" id="acc-advanced-header">
              <span class="accordion-title">Erweitert</span>
              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
            </div>
            <div class="accordion-body">
              <div class="accordion-content">
                <div class="sw-row" id="btn-timestamp">
                  <div class="sw-left">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                    <span>Zeitstempel</span>
                  </div>
                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
                </div>
                <div class="sw-row" id="btn-autofollow">
                  <div class="sw-left">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="8"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/></svg>
                    <span>Auto-Follow</span>
                  </div>
                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
                </div>
                <div class="sw-row" id="btn-motion">
                  <div class="sw-left">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                    <span>Bewegungserkennung</span>
                  </div>
                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
                </div>
                <div class="sw-row" id="btn-record-sound">
                  <div class="sw-left">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/></svg>
                    <span>Ton aufnehmen</span>
                  </div>
                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
                </div>
                <div class="sw-row" id="btn-privacy-sound">
                  <div class="sw-left">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 010 7.07"/></svg>
                    <span>Privat-Ton</span>
                  </div>
                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
                </div>
              </div>
            </div>
          </div>

          <!-- Accordion: Diagnostics -->
          <div class="accordion" id="acc-diagnostics">
            <div class="accordion-header" id="acc-diagnostics-header">
              <span class="accordion-title">Diagnose</span>
              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
            </div>
            <div class="accordion-body">
              <div class="accordion-content">
                <div class="diag-row" id="diag-wifi">
                  <span class="diag-label">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12.55a11 11 0 0114.08 0"/><path d="M1.42 9a16 16 0 0121.16 0"/><path d="M8.53 16.11a6 6 0 016.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/></svg>
                    WiFi
                  </span>
                  <span class="diag-value" id="diag-wifi-val">—</span>
                </div>
                <div class="diag-row" id="diag-firmware">
                  <span class="diag-label">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/></svg>
                    Firmware
                  </span>
                  <span class="diag-value" id="diag-firmware-val">—</span>
                </div>
                <div class="diag-row" id="diag-ambient">
                  <span class="diag-label">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/></svg>
                    Umgebungslicht
                  </span>
                  <span class="diag-value" id="diag-ambient-val">—</span>
                </div>
                <div class="diag-row" id="diag-movement-today">
                  <span class="diag-label">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>
                    Bewegung heute
                  </span>
                  <span class="diag-value" id="diag-movement-today-val">—</span>
                </div>
                <div class="diag-row" id="diag-audio-today">
                  <span class="diag-label">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 010 14.14"/></svg>
                    Audio heute
                  </span>
                  <span class="diag-value" id="diag-audio-today-val">—</span>
                </div>
                <div class="sw-row" id="btn-motion-zones" style="margin-top:4px">
                  <div class="sw-left">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><rect x="7" y="7" width="10" height="10" rx="1" stroke-dasharray="3 2"/></svg>
                    <span>Motion Zones</span>
                  </div>
                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
                </div>
              </div>
            </div>
          </div>

          <div id="debug-line" style="font-size:10px;color:#666;text-align:right;padding:2px 12px 4px;opacity:0.7">Card v2.6.0</div>
      </ha-card>
    `;

    // Wire up image load/error events
    const img = this.shadowRoot.getElementById("cam-img");
    img.addEventListener("load", () => this._onImageLoaded());
    img.addEventListener("error", () => this._onImageError());

    // Click on image or video → fullscreen
    img.addEventListener("click", () => this._requestFullscreen());
    const vid = this.shadowRoot.getElementById("cam-video");
    vid.addEventListener("click", () => this._requestFullscreen());

    // Buttons
    this.shadowRoot.getElementById("btn-snapshot").addEventListener("click", () =>
      this._onSnapshotClick()
    );
    this.shadowRoot.getElementById("btn-stream").addEventListener("click", () =>
      this._toggleStream()
    );
    this.shadowRoot.getElementById("btn-fullscreen").addEventListener("click", () =>
      this._requestFullscreen()
    );

    // Toggle buttons
    this.shadowRoot.getElementById("btn-audio").addEventListener("click", () =>
      this._toggleAudio()
    );
    this.shadowRoot.getElementById("btn-light").addEventListener("click", () =>
      this._toggleSwitch(this._entities.light)
    );
    this.shadowRoot.getElementById("btn-privacy").addEventListener("click", () =>
      this._toggleSwitch(this._entities.privacy)
    );
    this.shadowRoot.getElementById("btn-notifications").addEventListener("click", () =>
      this._toggleSwitch(this._entities.notifications)
    );
    this.shadowRoot.getElementById("btn-intercom")?.addEventListener("click", () =>
      this._toggleSwitch(this._entities.intercom)
    );

    // Pan buttons
    const PAN_STEP = 30;
    const setPan = (pos) => {
      if (!this._hass || !this._entities.pan) return;
      this._hass.callService("number", "set_value", {
        entity_id: this._entities.pan,
        value: Math.max(-120, Math.min(120, pos)),
      }).then(() => {
        // Trigger backend image refresh so _cached_image is warm before card requests it
        this._callService("bosch_shc_camera", "trigger_snapshot", {});
        // Refresh snapshot after camera has had time to move (~2s)
        this._scheduleImageLoad(2000);
      }).catch((err) => console.warn("bosch-camera-card: pan set_value", err));
    };
    const getCurPan = () => parseFloat(this._hass?.states[this._entities.pan]?.state || 0);
    this.shadowRoot.getElementById("pan-full-left") ?.addEventListener("click", () => setPan(-120));
    this.shadowRoot.getElementById("pan-left")      ?.addEventListener("click", () => setPan(getCurPan() - PAN_STEP));
    this.shadowRoot.getElementById("pan-center")    ?.addEventListener("click", () => setPan(0));
    this.shadowRoot.getElementById("pan-right")     ?.addEventListener("click", () => setPan(getCurPan() + PAN_STEP));
    this.shadowRoot.getElementById("pan-full-right")?.addEventListener("click", () => setPan(120));

    // Quality dropdown
    const qualitySel = this.shadowRoot.getElementById("quality-select");
    if (qualitySel) {
      qualitySel.addEventListener("change", () => this._onQualityChange(qualitySel.value));
    }

    // Accordion toggle handlers
    ["acc-notif-types", "acc-advanced", "acc-diagnostics"].forEach(id => {
      this.shadowRoot.getElementById(`${id}-header`)?.addEventListener("click", () => {
        const acc = this.shadowRoot.getElementById(id);
        if (acc) acc.classList.toggle("open");
      });
    });

    // New toggle switches
    this.shadowRoot.getElementById("btn-notif-movement")?.addEventListener("click", () => this._toggleSwitch(this._entities.notifMovement));
    this.shadowRoot.getElementById("btn-notif-person")?.addEventListener("click", () => this._toggleSwitch(this._entities.notifPerson));
    this.shadowRoot.getElementById("btn-notif-audio")?.addEventListener("click", () => this._toggleSwitch(this._entities.notifAudio));
    this.shadowRoot.getElementById("btn-notif-trouble")?.addEventListener("click", () => this._toggleSwitch(this._entities.notifTrouble));
    this.shadowRoot.getElementById("btn-notif-alarm")?.addEventListener("click", () => this._toggleSwitch(this._entities.notifAlarm));
    this.shadowRoot.getElementById("btn-timestamp")?.addEventListener("click", () => this._toggleSwitch(this._entities.timestamp));
    this.shadowRoot.getElementById("btn-autofollow")?.addEventListener("click", () => this._toggleSwitch(this._entities.autofollow));
    this.shadowRoot.getElementById("btn-motion")?.addEventListener("click", () => this._toggleSwitch(this._entities.motion));
    this.shadowRoot.getElementById("btn-record-sound")?.addEventListener("click", () => this._toggleSwitch(this._entities.recordSound));
    this.shadowRoot.getElementById("btn-privacy-sound")?.addEventListener("click", () => this._toggleSwitch(this._entities.privacySound));
    this.shadowRoot.getElementById("btn-front-light")?.addEventListener("click", () => this._toggleSwitch(this._entities.frontLight));
    this.shadowRoot.getElementById("btn-wallwasher")?.addEventListener("click", () => this._toggleSwitch(this._entities.wallwasher));
    // Front light intensity slider
    const intSlider = this.shadowRoot.getElementById("slider-front-intensity");
    if (intSlider) {
      intSlider.addEventListener("change", (e) => {
        const val = parseFloat(e.target.value);
        this._hass?.callService("number", "set_value", {
          entity_id: this._entities.frontIntensity,
          value: val,
        });
        const lbl = this.shadowRoot.getElementById("val-front-intensity");
        if (lbl) lbl.textContent = val + "%";
      });
    }

    // Motion zones overlay toggle
    this.shadowRoot.getElementById("btn-motion-zones")?.addEventListener("click", () => {
      this._config.show_motion_zones = !this._config.show_motion_zones;
      this._update();
    });

    // Load the first image immediately
    this._imgTimestamp = Date.now();
    this._scheduleImageLoad(0);
  }

  // ── Image lifecycle ───────────────────────────────────────────────────────

  _scheduleImageLoad(delayMs = 0) {
    if (delayMs <= 0) {
      this._imgTimestamp = Date.now();
      this._updateImage();
    } else {
      setTimeout(() => {
        this._imgTimestamp = Date.now();
        this._updateImage();
      }, delayMs);
    }
  }

  _updateImage() {
    const img = this.shadowRoot.getElementById("cam-img");
    if (!img || !this._hass) return;
    const camEntity = this._entities.camera;
    const token = this._hass.states[camEntity]?.attributes?.access_token || "";
    // Request at display width — HA passes this to async_camera_image(width=).
    // Our backend already prefers the 320×180 RCP thumbnail (~3 KB) which is
    // well within 640 px. This avoids serving 1080p (~150 KB) to mobile.
    const dispW = Math.round(this.offsetWidth || 640);
    const url = `/api/camera_proxy/${camEntity}?token=${token}&time=${this._imgTimestamp}&width=${dispW}`;

    if (this._imageLoaded) {
      // Preload so the old image stays visible until the new one is fully ready
      const preload = new window.Image();
      preload.onload = () => { img.src = url; };
      preload.onerror = () => { this._setLoadingOverlay(false); };
      preload.src = url;
    } else {
      img.src = url;
    }
  }

  _onImageLoaded() {
    const img     = this.shadowRoot.getElementById("cam-img");
    const src     = img?.src || "";
    const isCache = src.startsWith("data:");

    this._imageLoaded = true;
    this._loadRetries = 0;   // reset retry counter on success
    if (img) img.classList.remove("hidden");

    // Clear stream-connecting overlay when first real frame arrives
    if (!isCache && this._streamConnecting) {
      this._streamConnecting = false;
      if (this._connectSteps) { this._connectSteps.forEach(t => clearTimeout(t)); this._connectSteps = null; }
    }

    // Overlay management:
    // - Cache image + awaitingFresh → keep "refreshing" overlay visible
    // - Fresh image (non-cache) → always clear overlay
    // - Cache image + NOT awaitingFresh → clear overlay (normal idle refresh)
    if (isCache && this._awaitingFresh) {
      // Cache loaded — keep spinner visible, fresh image will clear it.
      // But ensure the overlay is in "refreshing" mode (semi-transparent)
      // so the cached image is visible underneath.
      const overlay = this.shadowRoot.getElementById("loading-overlay");
      if (overlay) {
        overlay.classList.add("visible");
        overlay.classList.add("refreshing");
      }
    } else {
      // Fresh image arrived (or no fresh pending) — clear spinner
      this._awaitingFresh = false;
      this._setLoadingOverlay(false);
    }

    // Debug: show load time + frame interval on card
    const dbg = this.shadowRoot.getElementById("debug-line");
    if (dbg) {
      const now = new Date().toLocaleTimeString("de-DE");
      const w = img?.naturalWidth || "?", h = img?.naturalHeight || "?";
      const nowMs = Date.now();
      const dt = (!isCache && this._lastFrameTime) ? ` Δ${nowMs - this._lastFrameTime}ms` : "";
      if (!isCache) this._lastFrameTime = nowMs;
      dbg.textContent = `Card v2.6.0 | ${isCache ? "cache" : "fresh"} ${now}${dt} | ${w}×${h}`;
    }
    // Uptime counter is handled by its own setInterval (_uptimeTimer) — no update needed here.
    // Store image to localStorage so next app launch shows it instantly.
    // Skip during streaming — live frames change every 2s so per-frame I/O is wasteful.
    // After stream stops, _isStreaming() returns false → the post-stop refresh image
    // IS saved, keeping localStorage as fresh as possible without excess writes.
    if (!isCache && !this._isStreaming()) this._cacheImage(src);
  }

  _onImageError() {
    // Image fetch failed and we've never successfully loaded an image yet
    if (!this._imageLoaded) {
      const MAX_RETRIES = 5;
      if (this._loadRetries < MAX_RETRIES) {
        this._loadRetries++;
        // Backend may still be starting up — retry after 3s
        setTimeout(() => {
          this._imgTimestamp = Date.now();
          this._updateImage();
        }, 3000);
      } else {
        // Gave up after 5 retries (~15s) — hide spinner and show whatever we have
        this._setLoadingOverlay(false);
      }
      return;
    }
    // If we already had an image, keep showing the old one (don't blank it).
    this._setLoadingOverlay(false);
  }

  _setLoadingOverlay(visible, text = "Bild wird geladen…") {
    const overlay  = this.shadowRoot.getElementById("loading-overlay");
    const loadText = this.shadowRoot.getElementById("loading-text");
    const img      = this.shadowRoot.getElementById("cam-img");
    this._loadingOverlay = visible;
    if (overlay) {
      overlay.classList.toggle("visible", visible);
      // Use transparent overlay when we already have an image — old image stays visible underneath
      overlay.classList.toggle("refreshing", visible && this._imageLoaded);
    }
    if (loadText) loadText.textContent = text;
    // Only hide image on first load when there's nothing to show yet
    if (img) img.classList.toggle("hidden", visible && !this._imageLoaded);

    if (visible) {
      // Safety timeout: always hide overlay after 15s even if image never loads
      if (this._loadingTimeout) clearTimeout(this._loadingTimeout);
      this._loadingTimeout = setTimeout(() => this._setLoadingOverlay(false), 15000);
    } else {
      if (this._loadingTimeout) { clearTimeout(this._loadingTimeout); this._loadingTimeout = null; }
    }
  }

  // ── Image caching (localStorage — persists across iOS app restarts) ────────
  _restoreCachedImage() {
    // Immediately show last known image from localStorage — no wait for proxy.
    // Shows the cached image underneath a semi-transparent "refreshing" overlay
    // so the user sees something while we fetch a fresh image.
    if (!this._storageKey) return;
    try {
      const cached = localStorage.getItem(this._storageKey);
      if (!cached) return;
      const img = this.shadowRoot.getElementById("cam-img");
      if (img) { img.src = cached; img.classList.remove("hidden"); }
      this._imageLoaded = true;
      // Mark that we'll need a fresh image — set hass() will show the
      // "refreshing" overlay and trigger a snapshot fetch.
      this._awaitingFresh = true;
      // Switch from full-black spinner to semi-transparent "refreshing" overlay
      // so the cached image is visible underneath.
      const overlay = this.shadowRoot.getElementById("loading-overlay");
      if (overlay) {
        overlay.classList.add("visible");
        overlay.classList.add("refreshing");
      }
      const loadText = this.shadowRoot.getElementById("loading-text");
      if (loadText) loadText.textContent = "Aktualisiere…";
    } catch (_) {}
  }

  _cacheImage(proxyUrl) {
    // Fetch image bytes and store as dataURL in localStorage for instant restore
    if (!this._storageKey || !proxyUrl) return;
    fetch(proxyUrl)
      .then(r => r.ok ? r.blob() : Promise.reject(r.status))
      .then(blob => new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload  = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(blob);
      }))
      .then(dataUrl => {
        try { localStorage.setItem(this._storageKey, dataUrl); } catch (_) {}
      })
      .catch(() => {});
  }

  // ── Live HLS video ────────────────────────────────────────────────────────

  /**
   * Load hls.js from CDN on demand. Returns the Hls constructor.
   * hls.js uses MSE and works in Chrome/Firefox/Edge.
   * Safari/iOS has native HLS but no MSE → Hls.isSupported() returns false there.
   */
  _loadHlsJs() {
    if (window.Hls) return Promise.resolve(window.Hls);
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js";
      s.onload  = () => resolve(window.Hls);
      s.onerror = () => reject(new Error("hls.js load failed"));
      document.head.appendChild(s);
    });
  }

  async _startLiveVideo(attempt = 1) {
    if (!this._hass) return;
    const video = this.shadowRoot.getElementById("cam-video");
    const img   = this.shadowRoot.getElementById("cam-img");
    if (!video) return;

    this._stopRefreshTimer();
    this._startingLiveVideo = true;

    const audioOn = this._getEffectiveState(this._entities.audio) === "on";

    // Helper: activate video element with overlay management
    const activateVideo = () => {
      video.style.display = "block";
      if (img) img.style.display = "none";
      this._liveVideoActive    = true;
      this._startingLiveVideo  = false;
      const clearOverlay = () => {
        this._setLoadingOverlay(false);
        if (this._streamConnecting) {
          this._streamConnecting = false;
          if (this._connectSteps) { this._connectSteps.forEach(t => clearTimeout(t)); this._connectSteps = null; }
        }
        video.removeEventListener("playing", clearOverlay);
      };
      video.addEventListener("playing", clearOverlay);
      setTimeout(() => { clearOverlay(); }, 45000);
    };

    // ── WebRTC (deferred — needs active stream_source before go2rtc can offer it) ──
    // go2rtc provides WebRTC but only after stream_source returns an RTSP URL.
    // On-demand streams (Bosch) don't have a permanent RTSP URL — it's created
    // when the live switch is turned ON. HLS starts the stream, then WebRTC
    // could be used for subsequent reconnects. TODO: implement WebRTC upgrade.

    // ── HLS via camera/stream ───────────────────────────────────────────
    try {
      const result = await this._hass.callWS({
        type:      "camera/stream",
        entity_id: this._entities.camera,
      });
      if (!result?.url) throw new Error("no url");

      video.muted = true;
      const startPlay = () => {
        video.muted = true;
        video.play()
          .then(() => {
            if (audioOn) {
              // Try unmuting — Chrome may pause the video if there was no user gesture.
              video.muted = false;
              // Check after a tick if Chrome paused us due to autoplay policy.
              setTimeout(() => {
                if (video.paused && !video.muted) {
                  // Chrome blocked unmuted autoplay — fall back to muted playback.
                  video.muted = true;
                  video.play().catch(() => {});
                }
              }, 100);
            }
          })
          .catch(() => {});
      };

      const Hls = await this._loadHlsJs();
      if (Hls.isSupported()) {
        if (this._hls) { this._hls.destroy(); this._hls = null; }
        const hls = new Hls({ enableWorker: true, lowLatencyMode: true });
        this._hls = hls;
        hls.on(Hls.Events.MANIFEST_PARSED, startPlay);
        hls.on(Hls.Events.ERROR, (_ev, data) => {
          if (!data.fatal) return;
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
            hls.startLoad();
          } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
            hls.recoverMediaError();
          } else {
            console.warn("bosch-camera-card: hls.js fatal error, reconnecting", data);
            this._stopLiveVideo();
            if (this._isStreaming()) {
              setTimeout(() => { if (this._isStreaming()) this._startLiveVideo(); }, 2000);
            }
          }
        });
        hls.loadSource(result.url);
        hls.attachMedia(video);
      } else if (video.canPlayType("application/vnd.apple.mpegurl") !== "") {
        video.src = result.url;
        startPlay();
      } else {
        throw new Error("HLS not supported");
      }
      activateVideo();

    } catch (e) {
      if (attempt < 3) {
        setTimeout(() => this._startLiveVideo(attempt + 1), 1000);
      } else {
        console.warn("bosch-camera-card: stream not available", e);
        this._liveVideoActive   = false;
        this._startingLiveVideo = false;
        this._startRefreshTimer();
      }
    }
  }

  _stopLiveVideo() {
    if (this._hls) { this._hls.destroy(); this._hls = null; }
    const video = this.shadowRoot.getElementById("cam-video");
    const img   = this.shadowRoot.getElementById("cam-img");
    if (video) {
      video.pause();
      video.srcObject = null;
      video.removeAttribute("src");
      video.load();
      video.style.display = "none";
    }
    if (img) img.style.display = "block";
    this._liveVideoActive   = false;
    this._startingLiveVideo = false;
    // Clean up stream-connecting state
    this._streamConnecting = false;
    if (this._connectSteps) { this._connectSteps.forEach(t => clearTimeout(t)); this._connectSteps = null; }
  }

  // ── Snapshot button ───────────────────────────────────────────────────────
  _onSnapshotClick() {
    const btn   = this.shadowRoot.getElementById("btn-snapshot");
    const label = this.shadowRoot.getElementById("btn-snapshot-label");

    // Visual feedback
    if (btn) {
      btn.disabled = true;
      btn.classList.add("loading");
      const spinner = document.createElement("div");
      spinner.className = "btn-spinner";
      spinner.id = "snapshot-spinner";
      btn.insertBefore(spinner, btn.firstChild);
    }
    if (label) label.textContent = "Lädt…";
    this._setLoadingOverlay(true, "Aktualisiere Bild…");

    // If privacy mode is ON — no live image is available, show placeholder immediately
    const privStates = this._hass?.states;
    const privacyOn  = privStates && this._entities.privacy in privStates
                       && privStates[this._entities.privacy]?.state === "on";
    if (privacyOn) {
      if (label) label.textContent = "Snapshot";
      if (btn) { btn.disabled = false; btn.classList.remove("loading"); const sp = btn.querySelector("#snapshot-spinner"); if (sp) sp.remove(); }
      this._setLoadingOverlay(false);
      return;
    }

    // Trigger backend image refresh
    this._callService("bosch_shc_camera", "trigger_snapshot", {});

    // Capture current image byte count, then poll until it changes (new image ready)
    // REMOTE takes ~3-5s; LOCAL Digest auth takes ~6-15s
    const token   = this._hass?.states[this._entities.camera]?.attributes?.access_token || "";
    const dispW   = Math.round(this.offsetWidth || 640);
    const currUrl = `/api/camera_proxy/${this._entities.camera}?token=${token}&t=${Date.now()}&width=${dispW}`;

    const startPoll = (prevBytes) => {
      // First poll after 500ms — RCP refresh completes in ~100ms, so 500ms is plenty
      const startTime = Date.now();
      this._snapshotPollTimer = setTimeout(
        () => this._pollSnapshotImage(prevBytes, startTime), 500
      );
    };

    // Get current byte count (best-effort), then start polling
    fetch(currUrl)
      .then(r => r.ok ? r.blob() : null)
      .then(blob => startPoll(blob ? blob.size : 0))
      .catch(() => startPoll(0));
  }

  _pollSnapshotImage(prevBytes, startTime) {
    const TIMEOUT  = 15000;
    const INTERVAL = 1000;
    const elapsed  = Date.now() - startTime;

    if (!this._hass) { this._finishSnapshot(); return; }

    // Re-read token on every poll (it may refresh)
    const token = this._hass.states[this._entities.camera]?.attributes?.access_token || "";
    const dispW2 = Math.round(this.offsetWidth || 640);
    const url   = `/api/camera_proxy/${this._entities.camera}?token=${token}&t=${Date.now()}&width=${dispW2}`;

    fetch(url)
      .then(r => r.ok ? r.blob() : Promise.reject(r.status))
      .then(blob => {
        const changed = prevBytes === 0 || Math.abs(blob.size - prevBytes) > 200;
        if (changed || elapsed >= TIMEOUT) {
          this._showSnapshotBlob(blob);
        } else {
          this._snapshotPollTimer = setTimeout(
            () => this._pollSnapshotImage(prevBytes, startTime), INTERVAL
          );
        }
      })
      .catch(() => {
        if (elapsed < TIMEOUT) {
          this._snapshotPollTimer = setTimeout(
            () => this._pollSnapshotImage(prevBytes, startTime), INTERVAL
          );
        } else {
          this._finishSnapshot();
        }
      });
  }

  _showSnapshotBlob(blob) {
    if (!blob || blob.size < 500) { this._finishSnapshot(); return; }
    const reader = new FileReader();
    reader.onload = (e) => {
      const dataUrl = e.target.result;
      const img     = this.shadowRoot.getElementById("cam-img");
      if (img) {
        img.src = dataUrl;
        img.classList.remove("hidden");
        this._imageLoaded = true;
      }
      this._setLoadingOverlay(false);
      try { if (this._storageKey) localStorage.setItem(this._storageKey, dataUrl); } catch (_) {}
      this._finishSnapshot();
    };
    reader.onerror = () => this._finishSnapshot();
    reader.readAsDataURL(blob);
  }

  _finishSnapshot() {
    if (this._snapshotPollTimer) { clearTimeout(this._snapshotPollTimer); this._snapshotPollTimer = null; }
    const btn   = this.shadowRoot.getElementById("btn-snapshot");
    const label = this.shadowRoot.getElementById("btn-snapshot-label");
    if (btn) {
      btn.disabled = false;
      btn.classList.remove("loading");
      const sp = btn.querySelector("#snapshot-spinner");
      if (sp) sp.remove();
    }
    if (label) label.textContent = "Snapshot";
    this._setLoadingOverlay(false);
  }

  // ── State update ──────────────────────────────────────────────────────────
  _update() {
    if (!this._hass || !this._config) return;
    const hass = this._hass;
    const ents = this._entities;

    // Clear optimistic states that have been confirmed by HA
    for (const [entityId, optState] of Object.entries(this._optimistic)) {
      const actual = hass.states[entityId]?.state;
      if (actual && actual === optState) {
        delete this._optimistic[entityId];
        if (this._optimisticTimers[entityId]) {
          clearTimeout(this._optimisticTimers[entityId]);
          delete this._optimisticTimers[entityId];
        }
      }
    }

    // Title
    const titleEl = this.shadowRoot.getElementById("title");
    if (titleEl) {
      titleEl.textContent = this._config.title
        || hass.states[ents.camera]?.attributes?.friendly_name
        || ents.camera;
    }

    // Push status badge
    const pushState  = hass.states[ents.push_status];
    const pushBadge  = this.shadowRoot.getElementById("push-badge");
    const pushLabel  = this.shadowRoot.getElementById("push-label");
    if (pushBadge && pushLabel) {
      const isFcm  = pushState?.state === "fcm_push";
      const mode   = pushState?.attributes?.fcm_push_mode || "";
      pushBadge.className = "push-badge " + (isFcm ? "fcm" : "poll");
      pushLabel.textContent = isFcm ? `fcm${mode ? " " + mode : ""}` : "poll";
    }

    // Status dot
    const statusState = hass.states[ents.status]?.state || "UNKNOWN";
    const statusDot   = this.shadowRoot.getElementById("status-dot");
    const infoStatus  = this.shadowRoot.getElementById("info-status");
    if (statusDot) statusDot.className = "status-dot " + ({ ONLINE: "online", OFFLINE: "offline" }[statusState] || "unknown");
    if (infoStatus) infoStatus.textContent = statusState;

    // Streaming state
    const isStreaming  = this._isStreaming();
    const badge        = this.shadowRoot.getElementById("stream-badge");
    const streamLabel  = this.shadowRoot.getElementById("stream-label");
    const btnStream    = this.shadowRoot.getElementById("btn-stream");
    const btnStreamLbl = this.shadowRoot.getElementById("btn-stream-label");

    // "connecting" while HLS is negotiating (startingLiveVideo), "streaming" once live,
    // "idle" when off. Badge label shows uptime counter once streaming (updated per frame).
    const streamBadgeState = this._startingLiveVideo ? "connecting"
                           : (isStreaming ? "streaming" : "idle");
    if (badge)        badge.className = "stream-badge " + streamBadgeState;
    if (streamLabel && !isStreaming) streamLabel.textContent = streamBadgeState; // "idle"/"connecting"
    // "streaming" label text is updated by _onImageLoaded() with uptime counter
    if (btnStream)    btnStream.className = "btn btn-stream" + (isStreaming ? " active" : "");
    if (btnStreamLbl) btnStreamLbl.textContent = isStreaming ? "Stop Stream" : "Live Stream";

    // Connection type badge (LAN / Cloud)
    const connType  = hass.states[ents.switch]?.attributes?.connection_type || "";
    const connBadge = this.shadowRoot.getElementById("conn-badge");
    if (connBadge) {
      if (isStreaming && connType) {
        connBadge.className = "conn-badge " + (connType === "LOCAL" ? "local" : "remote");
        connBadge.textContent = connType === "LOCAL" ? "LAN" : "Cloud";
      } else {
        connBadge.className = "conn-badge hidden";
      }
    }

    // Track stream session start time for uptime counter in the badge
    if (isStreaming && !this._lastStreaming) {
      this._streamStartTime = Date.now();
      // Start uptime counter interval (1s updates)
      if (this._uptimeTimer) clearInterval(this._uptimeTimer);
      this._uptimeTimer = setInterval(() => {
        if (!this._streamStartTime) return;
        const s = Math.floor((Date.now() - this._streamStartTime) / 1000);
        const mm = String(Math.floor(s / 60)).padStart(2, "0");
        const ss = String(s % 60).padStart(2, "0");
        const label = this.shadowRoot?.getElementById("stream-label");
        if (label) label.textContent = `${mm}:${ss}`;
      }, 1000);
    }
    if (!isStreaming) {
      this._streamStartTime = 0;
      if (this._uptimeTimer) { clearInterval(this._uptimeTimer); this._uptimeTimer = null; }
    }

    // shouldVideo: always use HLS video when stream is ON.
    // Audio toggle only controls mute/unmute — no more snapshot-polling mode.
    const isAudioOn   = this._getEffectiveState(ents.audio) === "on";
    const shouldVideo = isStreaming;

    // Stream just stopped → stop video, fetch fresh snapshot for current + next session.
    if (!isStreaming && this._lastStreaming !== null && this._lastStreaming !== isStreaming) {
      this._stopLiveVideo();
      this._setLoadingOverlay(true, "Aktualisiere Bild…");
      this._callService("bosch_shc_camera", "trigger_snapshot", {});
      this._scheduleImageLoad(3500);
      this._startRefreshTimer();
    }
    this._lastStreaming = isStreaming;

    // Start HLS video when stream turns ON.
    // Wait until camera entity actually reports streaming (stream_source set)
    // to avoid "does not support play stream" errors from premature WS calls.
    if (shouldVideo && !this._liveVideoActive && !this._startingLiveVideo) {
      if (!this._waitingForStream) {
        this._waitingForStream = true;
        this._waitForStreamReady();
      }
    }
    if (!shouldVideo) {
      this._waitingForStream = false;
    }
    // Stop video when stream turns OFF
    if (!shouldVideo && this._liveVideoActive) {
      this._stopLiveVideo();
    }

    // Sync refresh timer when not in live video mode (idle snapshot polling).
    if (!this._liveVideoActive && !this._startingLiveVideo && !isStreaming) {
      if (this._timerStreaming !== false) {
        this._timerStreaming = false;
        this._startRefreshTimer();
      }
    }

    // Last event — detect new events and refresh snapshot immediately
    const lastEventState = hass.states[ents.last_event];
    const infoLastEvent  = this.shadowRoot.getElementById("info-last-event");
    const lastEventOverlay = this.shadowRoot.getElementById("last-event-overlay");
    const curEventVal = lastEventState?.state;
    if (curEventVal && curEventVal !== "unavailable" && curEventVal !== "unknown"
        && this._lastEventState !== null && curEventVal !== this._lastEventState
        && !this._liveVideoActive) {
      // New event detected — refresh image after short delay (HA needs ~1s to fetch fresh snap)
      this._scheduleImageLoad(1500);
    }
    this._lastEventState = curEventVal || this._lastEventState;
    let lastEventStr = "—";
    if (lastEventState?.state && lastEventState.state !== "unavailable") {
      try {
        const d = new Date(lastEventState.state);
        lastEventStr = isNaN(d) ? lastEventState.state : this._formatDatetime(d);
      } catch (_) { lastEventStr = lastEventState.state; }
    }
    if (lastEventStr === "—") {
      const a = hass.states[ents.camera]?.attributes?.last_event;
      if (a) lastEventStr = a.slice(0, 16).replace("T", " ");
    }
    if (infoLastEvent)    infoLastEvent.textContent = lastEventStr;
    if (lastEventOverlay) lastEventOverlay.textContent = lastEventStr !== "—" ? `Letztes: ${lastEventStr}` : "";

    // Events today
    const evTodayState = hass.states[ents.events_today];
    const infoEvToday  = this.shadowRoot.getElementById("info-events-today");
    const evOverlay    = this.shadowRoot.getElementById("events-overlay");
    const evCount      = evTodayState?.state ?? "—";
    if (infoEvToday) infoEvToday.textContent = evCount !== "—" ? `${evCount} Events` : "—";
    if (evOverlay)   evOverlay.textContent   = evCount !== "—" ? `${evCount} Events heute` : "";

    // Toggle buttons — Ton / Licht / Privat / Benachrichtigungen / Gegensprech.
    this._updateToggleBtn("btn-audio",         ents.audio,         hass.states[ents.audio]);
    this._updateToggleBtn("btn-light",         ents.light,         hass.states[ents.light]);
    this._updateToggleBtn("btn-privacy",       ents.privacy,       hass.states[ents.privacy]);
    this._updateToggleBtn("btn-notifications", ents.notifications, hass.states[ents.notifications]);
    this._updateToggleBtn("btn-intercom",      ents.intercom,      hass.states[ents.intercom]);

    // Accordion: notification type toggles
    this._updateToggleBtn("btn-notif-movement", ents.notifMovement, hass.states[ents.notifMovement]);
    this._updateToggleBtn("btn-notif-person",   ents.notifPerson,   hass.states[ents.notifPerson]);
    this._updateToggleBtn("btn-notif-audio",    ents.notifAudio,    hass.states[ents.notifAudio]);
    this._updateToggleBtn("btn-notif-trouble",  ents.notifTrouble,  hass.states[ents.notifTrouble]);
    this._updateToggleBtn("btn-notif-alarm",    ents.notifAlarm,    hass.states[ents.notifAlarm]);

    // Accordion: advanced controls
    this._updateToggleBtn("btn-timestamp",     ents.timestamp,     hass.states[ents.timestamp]);
    this._updateToggleBtn("btn-autofollow",    ents.autofollow,    hass.states[ents.autofollow]);
    this._updateToggleBtn("btn-motion",        ents.motion,        hass.states[ents.motion]);
    this._updateToggleBtn("btn-record-sound",  ents.recordSound,   hass.states[ents.recordSound]);
    this._updateToggleBtn("btn-privacy-sound", ents.privacySound,  hass.states[ents.privacySound]);
    this._updateToggleBtn("btn-front-light",   ents.frontLight,    hass.states[ents.frontLight]);
    this._updateToggleBtn("btn-wallwasher",    ents.wallwasher,    hass.states[ents.wallwasher]);
    // Front light intensity slider
    const intState = hass.states[ents.frontIntensity];
    const intSlider = this.shadowRoot.getElementById("slider-front-intensity");
    const intLabel = this.shadowRoot.getElementById("val-front-intensity");
    const intRow = this.shadowRoot.getElementById("row-front-intensity");
    if (intState && intState.state !== "unavailable" && intState.state !== "unknown") {
      if (intSlider && !intSlider.matches(":active")) intSlider.value = intState.state;
      if (intLabel) intLabel.textContent = intState.state + "%";
      if (intRow) intRow.style.display = "";
    } else {
      if (intRow) intRow.style.display = "none";
    }

    // Accordion: diagnostics sensor values
    const wifiVal = hass.states[ents.wifi];
    const fwVal   = hass.states[ents.firmware];
    const ambVal  = hass.states[ents.ambient];
    const movVal  = hass.states[ents.movementToday];
    const audVal  = hass.states[ents.audioToday];
    const _dv = (id, st) => { const el = this.shadowRoot.getElementById(id); if (el) el.textContent = (st?.state && st.state !== "unavailable" && st.state !== "unknown") ? st.state : "\u2014"; };
    _dv("diag-wifi-val", wifiVal);
    _dv("diag-firmware-val", fwVal);
    _dv("diag-ambient-val", ambVal);
    _dv("diag-movement-today-val", movVal);
    _dv("diag-audio-today-val", audVal);
    // Add units
    if (wifiVal?.state && wifiVal.state !== "unavailable") { const el = this.shadowRoot.getElementById("diag-wifi-val"); if (el) el.textContent = wifiVal.state + " %"; }
    if (ambVal?.state && ambVal.state !== "unavailable") { const el = this.shadowRoot.getElementById("diag-ambient-val"); if (el) el.textContent = ambVal.state + " %"; }

    // Motion zones toggle — visual state from config flag
    const mzBtn = this.shadowRoot.getElementById("btn-motion-zones");
    if (mzBtn) {
      const mzOn = !!this._config.show_motion_zones;
      mzBtn.classList.toggle("on", mzOn);
      const mzToggle = mzBtn.querySelector(".sw-toggle");
      if (mzToggle) mzToggle.classList.toggle("on", mzOn);
    }

    // Hide entire accordion sections if ALL their toggle entities are missing
    const _hideAccIf = (accId, entityIds) => {
      const acc = this.shadowRoot.getElementById(accId);
      if (!acc) return;
      const anyExists = entityIds.some(eid => {
        const st = hass.states[eid];
        return st && st.state && st.state !== "unavailable" && st.state !== "unknown";
      });
      acc.style.display = anyExists ? "" : "none";
    };
    _hideAccIf("acc-notif-types", [ents.notifMovement, ents.notifPerson, ents.notifAudio, ents.notifTrouble, ents.notifAlarm]);
    _hideAccIf("acc-advanced", [ents.timestamp, ents.autofollow, ents.motion, ents.recordSound, ents.privacySound, ents.frontLight, ents.wallwasher, ents.frontIntensity]);
    _hideAccIf("acc-diagnostics", [ents.wifi, ents.firmware, ents.ambient, ents.movementToday, ents.audioToday]);

    // Swap bell icon: bell when ON (notifications active), bell-off when OFF
    const notifState = this._getEffectiveState(ents.notifications);
    const notifIconOn  = this.shadowRoot.getElementById("notif-icon-on");
    const notifIconOff = this.shadowRoot.getElementById("notif-icon-off");
    if (notifIconOn && notifIconOff) {
      notifIconOn.style.display  = (notifState === "off") ? "none" : "";
      notifIconOff.style.display = (notifState === "off") ? ""     : "none";
    }

    // Keep live video muted state in sync with Ton toggle (only when streaming).
    // Only unmute when the video is already playing — unmuting a paused video
    // before play() is called would cause an autoplay NotAllowedError.
    if (this._liveVideoActive) {
      const video   = this.shadowRoot.getElementById("cam-video");
      const audioOn = this._getEffectiveState(ents.audio) === "on";
      if (video) {
        if (!audioOn) {
          video.muted = true;           // mute immediately — always safe
        } else if (!video.paused) {
          video.muted = false;          // unmute only if already playing
        }
      }
    }

    // Privacy placeholder — show whenever privacy is ON (only if entity exists)
    const privacyOptimistic = this._optimistic[ents.privacy];
    const privacyOn = privacyOptimistic !== undefined
      ? privacyOptimistic === "on"
      : (ents.privacy in hass.states && hass.states[ents.privacy]?.state === "on");
    const placeholder = this.shadowRoot.getElementById("privacy-placeholder");
    if (placeholder) placeholder.classList.toggle("visible", privacyOn);
    // Hide the spinner overlay when privacy is ON (placeholder takes over)
    if (privacyOn) this._setLoadingOverlay(false);

    // Privacy just turned OFF → fetch a fresh image immediately
    if (this._lastPrivacy === true && !privacyOn) {
      this._setLoadingOverlay(true, "Aktualisiere Bild…");
      this._scheduleImageLoad(1500);
    }
    this._lastPrivacy = privacyOn;

    // Motion zones overlay — SVG polygons from RCP 0x0c00/0x0c0a sensor data
    this._updateMotionZones(hass, ents);

    // Pan section — only visible when the pan number entity exists and has a valid state
    const panState   = hass.states[ents.pan];
    const panSection = this.shadowRoot.getElementById("pan-section");
    if (panSection) {
      const hasPan = panState && panState.state && panState.state !== "unavailable" && panState.state !== "unknown";
      panSection.style.display = hasPan ? "" : "none";
      if (hasPan) {
        const posEl = this.shadowRoot.getElementById("pan-position");
        if (posEl) posEl.textContent = `${panState.state}°`;
      }
    }

    // Quality section — only visible when quality_entity is configured and available
    const qualitySection = this.shadowRoot.getElementById("quality-section");
    const qualitySel     = this.shadowRoot.getElementById("quality-select");
    if (qualitySection && qualitySel) {
      const qualityEntityId = ents.quality;
      const qualityState    = qualityEntityId ? hass.states[qualityEntityId] : null;
      const hasQuality = qualityState && qualityState.state &&
                         qualityState.state !== "unavailable" && qualityState.state !== "unknown";
      qualitySection.style.display = hasQuality ? "" : "none";
      if (hasQuality && qualitySel.value !== qualityState.state) {
        qualitySel.value = qualityState.state;
      }
    }
  }

  _updateToggleBtn(id, entityId, entityState) {
    const btn = this.shadowRoot.getElementById(id);
    if (!btn) return;
    // Hide when entity doesn't exist or is unavailable/unknown
    // (e.g. camera light on a camera that has no physical light)
    const state = entityState?.state;
    if (!entityState || !state || state === "unavailable" || state === "unknown") {
      btn.style.display = "none";
      return;
    }
    btn.style.display = "";
    // Use optimistic state for immediate visual feedback, fall back to HA state
    const displayState = (entityId in this._optimistic) ? this._optimistic[entityId] : state;
    btn.classList.toggle("on", displayState === "on");
    btn.classList.remove("unavailable");
    btn.disabled = false;
  }

  // ── Helpers ───────────────────────────────────────────────────────────────
  _getEffectiveState(entityId) {
    if (entityId in this._optimistic) return this._optimistic[entityId];
    return this._hass?.states[entityId]?.state;
  }

  _waitForStreamReady(attempt = 0) {
    // Poll until the switch entity (real, not optimistic) confirms ON.
    // Backend needs ~5s for PUT /connection + TLS proxy + pre-warm.
    // Only then call camera/stream WS to avoid "does not support play stream"
    // errors and wasted retries that add 10-20s to startup.
    if (!this._waitingForStream || !this._hass) return;

    const switchState = this._hass.states[this._entities.switch];
    const reallyOn = switchState?.state === "on";
    // Also check if camera entity has streaming attributes set
    const cam = this._hass.states[this._entities.camera];
    const camReady = cam?.state === "streaming"
                  || cam?.attributes?.streaming_state === "active"
                  || (reallyOn && switchState?.attributes?.connection_type);

    if (camReady || (reallyOn && attempt >= 5)) {
      // Stream is ready or switch confirmed ON after 5s — start HLS
      this._waitingForStream = false;
      this._startLiveVideo();
      return;
    }
    if (attempt > 90) {
      // Give up after 90s — camera likely unreachable
      this._waitingForStream = false;
      this._streamConnecting = false;
      if (this._connectSteps) { this._connectSteps.forEach(t => clearTimeout(t)); this._connectSteps = null; }
      this._setLoadingOverlay(false);
      return;
    }
    setTimeout(() => this._waitForStreamReady(attempt + 1), 1000);
  }

  _updateMotionZones(hass, ents) {
    const svg = this.shadowRoot.getElementById("motion-zones-overlay");
    if (!svg) return;

    // Read cloud zones from the motion zones sensor attributes.
    // cloud_zones use normalized 0.0–1.0 coordinates {x, y, w, h} from the Cloud API.
    // The old "coordinates" field contains raw RCP data (not usable for overlay).
    const zoneState = hass.states[ents.motionZones];
    const zones = zoneState?.attributes?.cloud_zones;

    // Only show overlay when config option is set and data is available
    const showZones = this._config.show_motion_zones && zones && zones.length > 0;
    svg.classList.toggle("visible", showZones);
    if (!showZones) return;

    // Only re-render if zones changed (avoid DOM thrashing)
    const coordKey = JSON.stringify(zones);
    if (this._lastMotionCoordKey === coordKey) return;
    this._lastMotionCoordKey = coordKey;

    // Cloud zones: {x, y, w, h} normalized 0.0–1.0. ViewBox is 0-100, so multiply by 100.
    svg.innerHTML = "";
    for (let z = 0; z < zones.length; z++) {
      const c = zones[z];
      if (c.x == null || c.y == null || c.w == null || c.h == null) continue;
      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("x", c.x * 100);
      rect.setAttribute("y", c.y * 100);
      rect.setAttribute("width", c.w * 100);
      rect.setAttribute("height", c.h * 100);
      svg.appendChild(rect);
    }
  }

  _toggleStream() {
    const isOn = this._isStreaming();
    // Optimistic update — badge and button update instantly
    this._setOptimistic(this._entities.switch, isOn ? "off" : "on");
    if (isOn) {
      // Stopping stream — clean up connecting state immediately
      this._streamConnecting = false;
      this._waitingForStream = false;
      if (this._connectSteps) { this._connectSteps.forEach(t => clearTimeout(t)); this._connectSteps = null; }
    } else if (!this._streamConnecting) {
      // Starting stream → show loading overlay with progressive status updates
      // Timeline: PUT /connection ~2s, TLS proxy ~0.5s, pre-warm ~3s,
      // go2rtc RTSP connect ~5s, HLS segment generation ~10-15s, first frame ~25-35s total.
      this._streamConnecting = true;
      this._setLoadingOverlay(true, "Verbindung wird aufgebaut…");
      // Progressive status messages — each _setLoadingOverlay resets the 15s
      // safety timeout, so messages must be spaced <15s apart to keep the
      // spinner alive. LOCAL streams can take up to 60s on first connect.
      this._connectSteps = [
        setTimeout(() => { if (this._streamConnecting) this._setLoadingOverlay(true, "Kamera wird aufgeweckt…"); }, 3000),
        setTimeout(() => { if (this._streamConnecting) this._setLoadingOverlay(true, "Stream wird vorbereitet…"); }, 7000),
        setTimeout(() => { if (this._streamConnecting) this._setLoadingOverlay(true, "HLS wird gestartet…"); }, 12000),
        setTimeout(() => { if (this._streamConnecting) this._setLoadingOverlay(true, "Warte auf erstes Bild…"); }, 20000),
        setTimeout(() => { if (this._streamConnecting) this._setLoadingOverlay(true, "Gleich geschafft…"); }, 28000),
        setTimeout(() => { if (this._streamConnecting) this._setLoadingOverlay(true, "Kamera braucht etwas…"); }, 40000),
        setTimeout(() => { if (this._streamConnecting) this._setLoadingOverlay(true, "Verbindung wird aufgebaut…"); }, 52000),
        setTimeout(() => { if (this._streamConnecting) this._setLoadingOverlay(true, "Fast fertig…"); }, 65000),
        setTimeout(() => { if (this._streamConnecting) this._setLoadingOverlay(true, "Noch einen Moment…"); }, 78000),
      ];
    }
    this._callService("switch", isOn ? "turn_off" : "turn_on", { entity_id: this._entities.switch });
  }

  _toggleAudio() {
    const entityId = this._entities.audio;
    if (!this._hass || !entityId) return;
    const state = this._hass.states[entityId]?.state;
    if (!state || state === "unavailable" || state === "unknown") return;
    const turningOn = state !== "on";
    // Optimistic update → calls _update() which syncs video.muted state.
    this._setOptimistic(entityId, turningOn ? "on" : "off");
    // Persist to HA (affects rtsps URL for next stream open)
    this._callService("switch", turningOn ? "turn_on" : "turn_off", { entity_id: entityId });
  }

  _toggleSwitch(entityId) {
    if (!this._hass || !entityId) return;
    const state = this._hass.states[entityId]?.state;
    if (!state || state === "unavailable" || state === "unknown") return;
    const turningOn = state !== "on";
    // Optimistic update — toggle flips instantly without waiting for HA confirmation
    this._setOptimistic(entityId, turningOn ? "on" : "off");
    this._callService("switch", turningOn ? "turn_on" : "turn_off", { entity_id: entityId });
  }

  _onQualityChange(option) {
    const entityId = this._entities.quality;
    if (!entityId || !this._hass) return;
    this._callService("select", "select_option", { entity_id: entityId, option });
  }

  _setOptimistic(entityId, state) {
    this._optimistic[entityId] = state;
    // Safety: clear optimistic after 8s even if HA never confirms
    if (this._optimisticTimers[entityId]) clearTimeout(this._optimisticTimers[entityId]);
    this._optimisticTimers[entityId] = setTimeout(() => {
      delete this._optimistic[entityId];
      delete this._optimisticTimers[entityId];
    }, 8000);
    // Trigger immediate re-render with optimistic state
    this._update();
  }

  _requestFullscreen() {
    // If CSS fullscreen is already active, exit it
    if (this.classList.contains("fs-active")) {
      this._exitCssFullscreen();
      return;
    }
    // Try native Fullscreen API first (desktop, Android Chrome)
    const wrapper = this.shadowRoot.getElementById("img-wrapper");
    const el = wrapper || this;
    const tryNative = () => {
      if (el.requestFullscreen)            return el.requestFullscreen();
      if (el.webkitRequestFullscreen)      return Promise.resolve(el.webkitRequestFullscreen());
      if (el.mozRequestFullScreen)         return Promise.resolve(el.mozRequestFullScreen());
      if (el.msRequestFullscreen)          return Promise.resolve(el.msRequestFullscreen());
      return Promise.reject("no API");
    };
    try {
      Promise.resolve(tryNative()).catch(() => this._enterCssFullscreen());
    } catch (_) {
      this._enterCssFullscreen();
    }
  }

  _enterCssFullscreen() {
    this.classList.add("fs-active");
    document.body.style.overflow = "hidden";
    // Tap anywhere outside the image to exit
    this._fsClickOut = (e) => { if (!this.contains(e.target)) this._exitCssFullscreen(); };
    // Press Escape to exit
    this._fsKeyDown = (e) => { if (e.key === "Escape") this._exitCssFullscreen(); };
    setTimeout(() => {
      document.addEventListener("click", this._fsClickOut);
      document.addEventListener("keydown", this._fsKeyDown);
    }, 100);
  }

  _exitCssFullscreen() {
    this.classList.remove("fs-active");
    document.body.style.overflow = "";
    if (this._fsClickOut) { document.removeEventListener("click", this._fsClickOut); this._fsClickOut = null; }
    if (this._fsKeyDown)  { document.removeEventListener("keydown", this._fsKeyDown);  this._fsKeyDown  = null; }
  }

  _callService(domain, service, data) {
    if (!this._hass) return;
    this._hass.callService(domain, service, data).catch((err) =>
      console.warn("bosch-camera-card:", domain, service, err)
    );
  }

  _formatDatetime(d) {
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  static getStubConfig() { return { camera_entity: "camera.bosch_garten" }; }
  getCardSize() { return 4; }
}

customElements.define("bosch-camera-card", BoschCameraCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type:        "bosch-camera-card",
  name:        "Bosch Camera Card",
  description: "Bosch Smart Home cameras with streaming state, loading indicator and controls",
  preview:     false,
});
