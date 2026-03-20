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
 *   refresh_interval_idle: 30                 # seconds (default 30)
 *   refresh_interval_streaming: 3             # seconds (default 3)
 *
 * Version: 1.4.6
 */

class BoschCameraCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass          = null;
    this._config        = null;
    this._refreshTimer  = null;
    this._imgTimestamp  = Date.now();
    this._lastStreaming  = null;   // last known streaming state (true/false/null)
    this._lastPrivacy   = null;   // last known privacy state (true/false/null)
    this._imageLoaded   = false;  // did we ever successfully load an image?
    this._loadingOverlay = false; // is the "Wird geladen" overlay active?
    this._loadingTimeout = null;  // safety timeout to hide overlay
    this._sessionKey    = null;   // sessionStorage key for cached image dataURL
    this._snapshotPollTimer = null; // polling timer during snapshot refresh
  }

  // ── Config ────────────────────────────────────────────────────────────────
  setConfig(config) {
    if (!config.camera_entity) {
      throw new Error("bosch-camera-card: camera_entity is required");
    }
    this._config = {
      camera_entity:              config.camera_entity,
      title:                      config.title || null,
      refresh_interval_idle:      config.refresh_interval_idle      ?? 30,
      refresh_interval_streaming: config.refresh_interval_streaming ?? 3,
    };

    this._sessionKey = `bosch_cam_${config.camera_entity}`;

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
  }

  // ── Timer ─────────────────────────────────────────────────────────────────
  _startRefreshTimer() {
    this._stopRefreshTimer();
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
    const sw = this._hass.states[this._entities.switch];
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

        /* Fullscreen — native API (desktop/Android) */
        .img-wrapper:fullscreen,
        .img-wrapper:-webkit-full-screen {
          background: #000;
          display: flex; align-items: center; justify-content: center;
          width: 100vw; height: 100vh;
        }
        .img-wrapper:fullscreen .cam-img,
        .img-wrapper:-webkit-full-screen .cam-img {
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
        :host(.fs-active) .cam-img { width: 100vw; height: 100vh; object-fit: contain; min-height: unset; }

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

        /* Toggle row — Ton / Licht / Privat */
        .toggle-row { display: flex; gap: 8px; padding: 0 12px 12px; }
        .btn-toggle {
          flex: 1; display: flex; flex-direction: column; align-items: center;
          gap: 4px; padding: 8px 6px; border-radius: 10px; border: none;
          cursor: pointer; font-size: 11px; font-weight: 500; font-family: inherit;
          transition: background 0.2s, color 0.2s;
          background: rgba(99,99,102,.15); color: var(--secondary-text-color, #8e8e93);
          -webkit-tap-highlight-color: transparent;
        }
        .btn-toggle:active { opacity: .7; }
        .btn-toggle.on { background: rgba(10,132,255,.2); color: #0a84ff; }
        .btn-toggle.on.privacy-btn { background: rgba(255,69,58,.15); color: #ff453a; }
        .btn-toggle.unavailable { opacity: .35; cursor: default; }
        .btn-toggle svg { width: 17px; height: 17px; flex-shrink: 0; }

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

        <div class="toggle-row">
          <button class="btn-toggle" id="btn-audio" title="Ton">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
              <path d="M19.07 4.93a10 10 0 010 14.14M15.54 8.46a5 5 0 010 7.07"/>
            </svg>
            <span>Ton</span>
          </button>
          <button class="btn-toggle" id="btn-light" title="Kamera-Licht">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="5"/>
              <line x1="12" y1="1" x2="12" y2="3"/>
              <line x1="12" y1="21" x2="12" y2="23"/>
              <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>
              <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
              <line x1="1" y1="12" x2="3" y2="12"/>
              <line x1="21" y1="12" x2="23" y2="12"/>
              <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>
              <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
            </svg>
            <span>Licht</span>
          </button>
          <button class="btn-toggle privacy-btn" id="btn-privacy" title="Privat-Modus">
            <svg id="privacy-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
              <path d="M7 11V7a5 5 0 0110 0v4"/>
            </svg>
            <span>Privat</span>
          </button>
        </div>
      </ha-card>
    `;

    // Wire up image load/error events
    const img = this.shadowRoot.getElementById("cam-img");
    img.addEventListener("load", () => this._onImageLoaded());
    img.addEventListener("error", () => this._onImageError());

    // Click on image → fullscreen
    img.addEventListener("click", () => this._requestFullscreen());

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
      this._toggleSwitch(this._entities.audio)
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
    this._loadingOverlay = false;
    if (img)     img.classList.remove("hidden");
    if (overlay) { overlay.classList.remove("visible"); overlay.classList.remove("refreshing"); }
    if (this._loadingTimeout) { clearTimeout(this._loadingTimeout); this._loadingTimeout = null; }
    // Store image to sessionStorage so next page load shows it instantly
    if (img?.src && !img.src.startsWith("data:")) this._cacheImage(img.src);
  }

  _onImageError() {
    // Image fetch failed — keep overlay visible if we've never loaded one
    if (!this._imageLoaded) return;
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

  // ── Image caching (sessionStorage) ────────────────────────────────────────
  _restoreCachedImage() {
    // Immediately show last known image from sessionStorage — no wait for proxy
    if (!this._sessionKey) return;
    try {
      const cached = sessionStorage.getItem(this._sessionKey);
      if (!cached) return;
      const img     = this.shadowRoot.getElementById("cam-img");
      const overlay = this.shadowRoot.getElementById("loading-overlay");
      if (img) { img.src = cached; img.classList.remove("hidden"); }
      if (overlay) overlay.classList.remove("visible");
      this._imageLoaded = true;
    } catch (_) {}
  }

  _cacheImage(proxyUrl) {
    // Fetch image bytes and store as dataURL in sessionStorage for instant restore
    if (!this._sessionKey || !proxyUrl) return;
    fetch(proxyUrl)
      .then(r => r.ok ? r.blob() : Promise.reject(r.status))
      .then(blob => new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload  = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(blob);
      }))
      .then(dataUrl => {
        try { sessionStorage.setItem(this._sessionKey, dataUrl); } catch (_) {}
      })
      .catch(() => {});
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
      try { if (this._sessionKey) sessionStorage.setItem(this._sessionKey, dataUrl); } catch (_) {}
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

    // Detect streaming → idle transition
    if (this._lastStreaming !== null && this._lastStreaming !== isStreaming) {
      if (!isStreaming) {
        // Stream just stopped — show loading overlay, then fetch fresh snapshot
        this._setLoadingOverlay(true, "Aktualisiere Bild…");
        // HA integration fetches new snapshot ~5s after stream stops
        this._scheduleImageLoad(6000);
      } else {
        // Stream just started — start fetching live frames immediately
        this._scheduleImageLoad(500);
      }
      this._startRefreshTimer();
    }
    this._lastStreaming = isStreaming;

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
    this._updateToggleBtn("btn-audio",   hass.states[ents.audio]);
    this._updateToggleBtn("btn-light",   hass.states[ents.light]);
    this._updateToggleBtn("btn-privacy", hass.states[ents.privacy]);

    // Privacy placeholder — show whenever privacy is ON (only if entity exists)
    const privacyOn  = ents.privacy in hass.states && hass.states[ents.privacy]?.state === "on";
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

  _updateToggleBtn(id, entityState) {
    const btn = this.shadowRoot.getElementById(id);
    if (!btn) return;
    // Hide entirely when entity doesn't exist in HA (e.g. SHC not configured)
    if (entityState === undefined || entityState === null) {
      btn.style.display = "none";
      return;
    }
    btn.style.display = "";
    const state = entityState.state;
    const unavailable = !state || state === "unavailable" || state === "unknown";
    btn.classList.toggle("on",          !unavailable && state === "on");
    btn.classList.toggle("unavailable", unavailable);
    btn.disabled = unavailable;
  }

  // ── Helpers ───────────────────────────────────────────────────────────────
  _toggleStream() {
    const isOn  = this._isStreaming();
    this._callService("switch", isOn ? "turn_off" : "turn_on", { entity_id: this._entities.switch });
  }

  _toggleSwitch(entityId) {
    if (!this._hass || !entityId) return;
    const state = this._hass.states[entityId]?.state;
    if (!state || state === "unavailable" || state === "unknown") return;
    this._callService("switch", state === "on" ? "turn_off" : "turn_on", { entity_id: entityId });
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
