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
 *   refresh_interval_idle: 300                # seconds (default 300 = 5 min)
 *   refresh_interval_streaming: 2             # seconds (default 2)
 *
 * Version: 1.5.7
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
  }

  // ── Config ────────────────────────────────────────────────────────────────
  setConfig(config) {
    if (!config.camera_entity) {
      throw new Error("bosch-camera-card: camera_entity is required");
    }
    this._config = {
      camera_entity:              config.camera_entity,
      title:                      config.title || null,
      refresh_interval_idle:      config.refresh_interval_idle      ?? 300,
      refresh_interval_streaming: config.refresh_interval_streaming ?? 2,
    };

    this._storageKey = `bosch_cam_${config.camera_entity}`;

    const base = config.camera_entity.replace(/^camera\./, "");
    this._entities = {
      camera:       config.camera_entity,
      switch:       config.switch_entity        || `switch.${base}_live_stream`,
      audio:        config.audio_entity         || `switch.${base}_audio`,
      light:        config.light_entity         || `switch.${base}_camera_light`,
      privacy:      config.privacy_entity       || `switch.${base}_privacy_mode`,
      status:       config.status_entity        || `sensor.${base}_status`,
      events_today: config.events_today_entity  || `sensor.${base}_events_today`,
      last_event:   config.last_event_entity    || `sensor.${base}_last_event`,
    };

    this._render();
    this._restoreCachedImage();
    this._startRefreshTimer();
  }

  // ── HA state updates ──────────────────────────────────────────────────────
  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  disconnectedCallback() {
    this._stopRefreshTimer();
    if (this._loadingTimeout)    clearTimeout(this._loadingTimeout);
    if (this._snapshotPollTimer) clearTimeout(this._snapshotPollTimer);
    Object.values(this._optimisticTimers).forEach(t => clearTimeout(t));
    this._stopLiveVideo();
  }

  // ── Timer ─────────────────────────────────────────────────────────────────
  _startRefreshTimer() {
    this._stopRefreshTimer();
    // No snapshot polling when live video is playing
    if (this._liveVideoActive) return;
    const interval = this._isStreaming()
      ? this._config.refresh_interval_streaming
      : this._config.refresh_interval_idle;

    this._refreshTimer = setInterval(() => {
      this._imgTimestamp = Date.now();
      this._updateImage();
    }, interval * 1000);
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
        .stream-badge.idle      { background: rgba(99,99,102,.25); color: #8e8e93; }
        .stream-badge.streaming { background: rgba(0,122,255,.2); color: #0a84ff; box-shadow: 0 0 0 1px rgba(0,122,255,.3); }
        .stream-badge .dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
        .stream-badge.idle .dot      { background: #636366; }
        .stream-badge.streaming .dot { background: #0a84ff; animation: pulse 1.5s infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

        /* Camera image area */
        .img-wrapper { position: relative; width: 100%; background: #000; line-height: 0; }
        .cam-img {
          width: 100%; height: auto; display: block; object-fit: cover;
          min-height: 160px; transition: opacity 0.3s;
        }
        .cam-img.hidden { opacity: 0; }

        /* Live video element */
        .cam-video {
          width: 100%; height: auto; display: block; object-fit: cover;
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
        :host(.fs-active) ha-card { width: 100vw; height: 100vh; border-radius: 0 !important; overflow: hidden; }
        :host(.fs-active) .cam-img,
        :host(.fs-active) .cam-video { width: 100vw; height: 100vh; object-fit: contain; min-height: unset; }

        /* Loading overlay */
        .loading-overlay {
          position: absolute; inset: 0;
          display: flex; flex-direction: column; align-items: center; justify-content: center;
          background: rgba(0,0,0,.75);
          gap: 12px;
          opacity: 0; transition: opacity 0.3s; pointer-events: none;
        }
        .loading-overlay.visible { opacity: 1; }
        /* Transparent overlay when refreshing an existing image — old image stays visible */
        .loading-overlay.refreshing { background: rgba(0,0,0,.15); }
        .loading-overlay.refreshing .loading-text { display: none; }
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
      </style>

      <ha-card>
        <div class="header">
          <div class="header-left">
            <div class="status-dot unknown" id="status-dot"></div>
            <span class="title" id="title">Bosch Camera</span>
          </div>
          <div class="stream-badge idle" id="stream-badge">
            <div class="dot"></div>
            <span id="stream-label">idle</span>
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
                <span>Ton</span>
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
          </div>
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
    const url = `/api/camera_proxy/${camEntity}?token=${token}&time=${this._imgTimestamp}`;

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
    const overlay = this.shadowRoot.getElementById("loading-overlay");
    this._imageLoaded    = true;
    this._loadRetries    = 0;   // reset retry counter on success
    this._loadingOverlay = false;
    if (img)     img.classList.remove("hidden");
    if (overlay) { overlay.classList.remove("visible"); overlay.classList.remove("refreshing"); }
    if (this._loadingTimeout) { clearTimeout(this._loadingTimeout); this._loadingTimeout = null; }
    // Store image to localStorage so next app launch shows it instantly
    if (img?.src && !img.src.startsWith("data:")) this._cacheImage(img.src);
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
    // If we already had an image, keep showing the old one (don't blank it)
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
    // Immediately show last known image from localStorage — no wait for proxy
    if (!this._storageKey) return;
    try {
      const cached = localStorage.getItem(this._storageKey);
      if (!cached) return;
      const img     = this.shadowRoot.getElementById("cam-img");
      const overlay = this.shadowRoot.getElementById("loading-overlay");
      if (img) { img.src = cached; img.classList.remove("hidden"); }
      if (overlay) overlay.classList.remove("visible");
      this._imageLoaded = true;
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

    // Stop snapshot polling immediately — don't wait for video to be ready
    this._stopRefreshTimer();
    this._startingLiveVideo = true;

    try {
      const result = await this._hass.callWS({
        type:      "camera/stream",
        entity_id: this._entities.camera,
      });
      if (!result?.url) throw new Error("no url");

      const hlsUrl  = result.url;
      const audioOn = this._getEffectiveState(this._entities.audio) === "on";
      // Always start muted — autoplay policy blocks unmuted autoplay.
      // After play() resolves, unmute if Ton is ON (safe: changing muted on a
      // playing video does not require a user gesture).
      video.muted = true;

      // startPlay: called when the pipeline is ready.
      // For hls.js → called from MANIFEST_PARSED (source buffers are set up).
      // For native HLS → called immediately after video.src is set.
      // Re-mute before play() in case _update() already unmuted while waiting for MANIFEST_PARSED.
      const startPlay = () => {
        video.muted = true; // ensure muted before play() — autoplay policy
        video.play()
          .then(() => { video.muted = !audioOn; })
          .catch(() => {});
      };

      // Standard hls.js recommendation:
      //   1. Hls.isSupported() → MSE available (Chrome/Firefox/Edge) → use hls.js
      //   2. canPlayType → Safari/iOS native HLS
      //   3. Neither → can't play, fall back to snapshots
      const Hls = await this._loadHlsJs();
      if (Hls.isSupported()) {
        // Chrome / Firefox / Edge — use hls.js via MSE
        if (this._hls) { this._hls.destroy(); this._hls = null; }
        const hls = new Hls({ enableWorker: true, lowLatencyMode: true });
        this._hls = hls;
        // Play only after manifest is parsed and source buffers are ready
        hls.on(Hls.Events.MANIFEST_PARSED, startPlay);
        hls.loadSource(hlsUrl);
        hls.attachMedia(video);
      } else if (video.canPlayType("application/vnd.apple.mpegurl") !== "") {
        // Safari / iOS — native HLS (MSE not needed)
        video.src = hlsUrl;
        startPlay();
      } else {
        throw new Error("HLS not supported by this browser");
      }

      video.style.display = "block";
      if (img) img.style.display = "none";
      this._liveVideoActive    = true;
      this._startingLiveVideo  = false;
      this._setLoadingOverlay(false);

    } catch (e) {
      if (attempt < 3) {
        // Retry — go2rtc may still be starting the RTSP session
        setTimeout(() => this._startLiveVideo(attempt + 1), 2000);
      } else {
        // Fall back to fast snapshot polling
        console.warn("bosch-camera-card: HLS stream not available, using snapshot fallback", e);
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
      video.src = "";
      video.style.display = "none";
    }
    if (img) img.style.display = "block";
    this._liveVideoActive   = false;
    this._startingLiveVideo = false;
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
    const currUrl = `/api/camera_proxy/${this._entities.camera}?token=${token}&t=${Date.now()}`;

    const startPoll = (prevBytes) => {
      // First poll after 3s — REMOTE cameras usually have a fresh image by then
      const startTime = Date.now();
      this._snapshotPollTimer = setTimeout(
        () => this._pollSnapshotImage(prevBytes, startTime), 3000
      );
    };

    // Get current byte count (best-effort), then start polling
    fetch(currUrl)
      .then(r => r.ok ? r.blob() : null)
      .then(blob => startPoll(blob ? blob.size : 0))
      .catch(() => startPoll(0));
  }

  _pollSnapshotImage(prevBytes, startTime) {
    const TIMEOUT  = 26000;
    const INTERVAL = 3000;
    const elapsed  = Date.now() - startTime;

    if (!this._hass) { this._finishSnapshot(); return; }

    // Re-read token on every poll (it may refresh)
    const token = this._hass.states[this._entities.camera]?.attributes?.access_token || "";
    const url   = `/api/camera_proxy/${this._entities.camera}?token=${token}&t=${Date.now()}`;

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

    if (badge)        badge.className = "stream-badge " + (isStreaming ? "streaming" : "idle");
    if (streamLabel)  streamLabel.textContent = isStreaming ? "streaming" : "idle";
    if (btnStream)    btnStream.className = "btn btn-stream" + (isStreaming ? " active" : "");
    if (btnStreamLbl) btnStreamLbl.textContent = isStreaming ? "Stop Stream" : "Live Stream";

    // shouldVideo: only show HLS live video when stream is ON AND Ton (audio) is ON.
    // Stream ON + Ton OFF → snapshot polling (img). Stream ON + Ton ON → HLS video.
    const isAudioOn  = this._getEffectiveState(ents.audio) === "on";
    const shouldVideo = isStreaming && isAudioOn;

    // Stream just stopped → stop video, refresh snapshot
    if (!isStreaming && this._lastStreaming !== null && this._lastStreaming !== isStreaming) {
      this._stopLiveVideo();
      this._setLoadingOverlay(true, "Aktualisiere Bild…");
      this._scheduleImageLoad(6000);
      this._startRefreshTimer();
    }
    this._lastStreaming = isStreaming;

    // Start video when shouldVideo becomes true (stream ON + audio ON)
    if (shouldVideo && !this._liveVideoActive && !this._startingLiveVideo) {
      this._startLiveVideo();
    }
    // Stop video when shouldVideo becomes false (audio turned OFF while streaming)
    if (!shouldVideo && this._liveVideoActive) {
      this._stopLiveVideo();
      if (isStreaming) this._startRefreshTimer(); // stream still on → use snapshot polling
    }

    // Sync refresh timer interval (idle vs streaming) when not in live video mode.
    // This ensures the timer runs at 3s when stream is ON+Ton OFF, 30s when stream is OFF.
    if (!this._liveVideoActive && !this._startingLiveVideo) {
      if (this._timerStreaming !== isStreaming) {
        this._timerStreaming = isStreaming;
        this._startRefreshTimer();
      }
    }

    // Last event
    const lastEventState = hass.states[ents.last_event];
    const infoLastEvent  = this.shadowRoot.getElementById("info-last-event");
    const lastEventOverlay = this.shadowRoot.getElementById("last-event-overlay");
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

    // Toggle buttons — Ton / Licht / Privat
    this._updateToggleBtn("btn-audio",   ents.audio,   hass.states[ents.audio]);
    this._updateToggleBtn("btn-light",   ents.light,   hass.states[ents.light]);
    this._updateToggleBtn("btn-privacy", ents.privacy, hass.states[ents.privacy]);

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
      this._scheduleImageLoad(3000);
    }
    this._lastPrivacy = privacyOn;
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

  _toggleStream() {
    const isOn = this._isStreaming();
    // Optimistic update — badge and button update instantly
    this._setOptimistic(this._entities.switch, isOn ? "off" : "on");
    this._callService("switch", isOn ? "turn_off" : "turn_on", { entity_id: this._entities.switch });
  }

  _toggleAudio() {
    const entityId = this._entities.audio;
    if (!this._hass || !entityId) return;
    const state = this._hass.states[entityId]?.state;
    if (!state || state === "unavailable" || state === "unknown") return;
    const turningOn = state !== "on";
    // Optimistic update → calls _update() which starts/stops video based on
    // shouldVideo = isStreaming && isAudioOn, and syncs video.muted accordingly.
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
