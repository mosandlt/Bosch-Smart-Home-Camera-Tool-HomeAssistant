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
 *   3. Restart HA or reload browser
 *
 * Card configuration (YAML):
 *   type: custom:bosch-camera-card
 *   camera_entity: camera.bosch_garten        # required
 *   title: Garten                             # optional — derived from entity if omitted
 *   refresh_interval_idle: 30                 # seconds between image refreshes when idle (default: 30)
 *   refresh_interval_streaming: 3             # seconds between image refreshes when streaming (default: 3)
 *
 * Entity IDs are derived automatically from camera_entity:
 *   camera.bosch_garten → switch.bosch_garten_live_stream
 *                       → sensor.bosch_garten_status
 *                       → sensor.bosch_garten_events_today
 *                       → sensor.bosch_garten_last_event
 *
 * Version: 1.3.0
 */

class BoschCameraCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = null;
    this._refreshTimer = null;
    this._imgTimestamp = Date.now();
    this._lastStreamingState = null;
  }

  // ── Config ────────────────────────────────────────────────────────────────
  setConfig(config) {
    if (!config.camera_entity) {
      throw new Error("bosch-camera-card: camera_entity is required");
    }
    this._config = {
      camera_entity: config.camera_entity,
      title: config.title || null,
      refresh_interval_idle: config.refresh_interval_idle ?? 30,
      refresh_interval_streaming: config.refresh_interval_streaming ?? 3,
    };

    // Derive entity base (e.g. "camera.bosch_garten" → "bosch_garten")
    const base = config.camera_entity.replace(/^camera\./, "");
    this._entities = {
      camera:      config.camera_entity,
      switch:      config.switch_entity      || `switch.${base}_live_stream`,
      status:      config.status_entity      || `sensor.${base}_status`,
      events_today: config.events_today_entity || `sensor.${base}_events_today`,
      last_event:  config.last_event_entity  || `sensor.${base}_last_event`,
    };

    this._render();
    this._startRefreshTimer();
  }

  // ── HA state updates ──────────────────────────────────────────────────────
  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────────
  disconnectedCallback() {
    this._stopRefreshTimer();
  }

  // ── Timer ─────────────────────────────────────────────────────────────────
  _startRefreshTimer() {
    this._stopRefreshTimer();
    const isStreaming = this._isStreaming();
    const interval = isStreaming
      ? this._config.refresh_interval_streaming
      : this._config.refresh_interval_idle;

    this._refreshTimer = setInterval(() => {
      this._imgTimestamp = Date.now();
      this._updateImage();
    }, interval * 1000);
  }

  _stopRefreshTimer() {
    if (this._refreshTimer) {
      clearInterval(this._refreshTimer);
      this._refreshTimer = null;
    }
  }

  _isStreaming() {
    if (!this._hass) return false;
    const switchState = this._hass.states[this._entities.switch];
    if (switchState) return switchState.state === "on";
    // Fallback: check camera entity streaming_state attribute
    const camState = this._hass.states[this._entities.camera];
    if (camState) {
      const attr = camState.attributes.streaming_state;
      if (attr) return attr === "active";
      return camState.state === "streaming";
    }
    return false;
  }

  // ── Render (full DOM build — called once on setConfig) ────────────────────
  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          font-family: var(--primary-font-family, Roboto, sans-serif);
        }
        ha-card {
          overflow: hidden;
          border-radius: var(--ha-card-border-radius, 12px);
          background: var(--ha-card-background, var(--card-background-color, #1c1c1e));
          box-shadow: var(--ha-card-box-shadow, 0 2px 8px rgba(0,0,0,.3));
        }

        /* ── Header ── */
        .header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 12px 14px 8px;
        }
        .header-left {
          display: flex;
          align-items: center;
          gap: 8px;
        }
        .title {
          font-size: 15px;
          font-weight: 600;
          color: var(--primary-text-color, #e5e5ea);
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }
        .status-dot {
          width: 8px;
          height: 8px;
          border-radius: 50%;
          background: #636366;
          flex-shrink: 0;
          transition: background 0.3s;
        }
        .status-dot.online  { background: #30d158; }
        .status-dot.offline { background: #ff453a; }
        .status-dot.unknown { background: #636366; }

        /* ── Stream state badge ── */
        .stream-badge {
          display: inline-flex;
          align-items: center;
          gap: 5px;
          font-size: 11px;
          font-weight: 600;
          letter-spacing: .4px;
          text-transform: uppercase;
          padding: 3px 8px;
          border-radius: 20px;
          transition: all 0.3s;
          white-space: nowrap;
        }
        .stream-badge.idle {
          background: rgba(99,99,102,.25);
          color: #8e8e93;
        }
        .stream-badge.streaming {
          background: rgba(0,122,255,.2);
          color: #0a84ff;
          box-shadow: 0 0 0 1px rgba(0,122,255,.3);
        }
        .stream-badge .dot {
          width: 6px;
          height: 6px;
          border-radius: 50%;
          flex-shrink: 0;
        }
        .stream-badge.idle .dot        { background: #636366; }
        .stream-badge.streaming .dot   {
          background: #0a84ff;
          animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50%       { opacity: .4; }
        }

        /* ── Camera image ── */
        .img-wrapper {
          position: relative;
          width: 100%;
          background: #000;
          line-height: 0;
        }
        .cam-img {
          width: 100%;
          height: auto;
          display: block;
          object-fit: cover;
          min-height: 140px;
        }
        .img-overlay {
          position: absolute;
          bottom: 0; left: 0; right: 0;
          padding: 20px 12px 8px;
          background: linear-gradient(transparent, rgba(0,0,0,.55));
          display: flex;
          align-items: flex-end;
          justify-content: space-between;
          pointer-events: none;
        }
        .last-event-overlay {
          font-size: 11px;
          color: rgba(255,255,255,.8);
        }
        .events-overlay {
          font-size: 11px;
          color: rgba(255,255,255,.7);
        }

        /* ── Info row ── */
        .info-row {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 8px 14px;
          gap: 10px;
        }
        .info-item {
          display: flex;
          flex-direction: column;
          gap: 1px;
          min-width: 0;
        }
        .info-label {
          font-size: 10px;
          text-transform: uppercase;
          letter-spacing: .5px;
          color: var(--secondary-text-color, #8e8e93);
        }
        .info-value {
          font-size: 13px;
          color: var(--primary-text-color, #e5e5ea);
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }

        /* ── Button row ── */
        .btn-row {
          display: flex;
          gap: 8px;
          padding: 0 12px 12px;
        }
        .btn {
          flex: 1;
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 6px;
          padding: 9px 10px;
          border-radius: 10px;
          border: none;
          cursor: pointer;
          font-size: 13px;
          font-weight: 500;
          font-family: inherit;
          transition: opacity 0.15s, transform 0.1s;
          -webkit-tap-highlight-color: transparent;
        }
        .btn:active { transform: scale(.97); opacity: .8; }
        .btn-snapshot {
          background: rgba(99,99,102,.2);
          color: var(--primary-text-color, #e5e5ea);
        }
        .btn-stream {
          background: rgba(10,132,255,.18);
          color: #0a84ff;
        }
        .btn-stream.active {
          background: rgba(255,69,58,.18);
          color: #ff453a;
        }
        .btn svg {
          width: 16px;
          height: 16px;
          flex-shrink: 0;
        }
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

        <div class="img-wrapper">
          <img class="cam-img" id="cam-img" src="" alt="Camera"
               onerror="this.style.minHeight='160px'" />
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
            <span class="info-label">Last event</span>
            <span class="info-value" id="info-last-event">—</span>
          </div>
          <div class="info-item" style="text-align:right">
            <span class="info-label">Today</span>
            <span class="info-value" id="info-events-today">—</span>
          </div>
        </div>

        <div class="btn-row">
          <button class="btn btn-snapshot" id="btn-snapshot">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/>
              <circle cx="12" cy="13" r="4"/>
            </svg>
            Snapshot
          </button>
          <button class="btn btn-stream" id="btn-stream">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polygon points="23 7 16 12 23 17 23 7"/>
              <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
            </svg>
            <span id="btn-stream-label">Live Stream</span>
          </button>
        </div>
      </ha-card>
    `;

    this.shadowRoot.getElementById("btn-snapshot").addEventListener("click", () =>
      this._callService("bosch_shc_camera", "trigger_snapshot", {})
    );
    this.shadowRoot.getElementById("btn-stream").addEventListener("click", () =>
      this._toggleStream()
    );
  }

  // ── Update (called on every hass state change) ────────────────────────────
  _update() {
    if (!this._hass || !this._config) return;
    const hass = this._hass;
    const ents = this._entities;

    // ── Title ──────────────────────────────────────────────────────────────
    const titleEl = this.shadowRoot.getElementById("title");
    if (titleEl) {
      titleEl.textContent = this._config.title
        || hass.states[ents.camera]?.attributes?.friendly_name
        || ents.camera;
    }

    // ── Status dot ─────────────────────────────────────────────────────────
    const statusState  = hass.states[ents.status]?.state || "UNKNOWN";
    const statusDot    = this.shadowRoot.getElementById("status-dot");
    const infoStatus   = this.shadowRoot.getElementById("info-status");
    if (statusDot) {
      statusDot.className = "status-dot " + ({
        ONLINE: "online", OFFLINE: "offline",
      }[statusState] || "unknown");
    }
    if (infoStatus) infoStatus.textContent = statusState;

    // ── Streaming state ─────────────────────────────────────────────────────
    const isStreaming  = this._isStreaming();
    const badge        = this.shadowRoot.getElementById("stream-badge");
    const streamLabel  = this.shadowRoot.getElementById("stream-label");
    const btnStream    = this.shadowRoot.getElementById("btn-stream");
    const btnStreamLbl = this.shadowRoot.getElementById("btn-stream-label");

    if (badge) {
      badge.className = "stream-badge " + (isStreaming ? "streaming" : "idle");
    }
    if (streamLabel) streamLabel.textContent = isStreaming ? "streaming" : "idle";
    if (btnStream) {
      btnStream.className = "btn btn-stream" + (isStreaming ? " active" : "");
    }
    if (btnStreamLbl) {
      btnStreamLbl.textContent = isStreaming ? "Stop Stream" : "Live Stream";
    }

    // Restart timer if streaming state changed
    if (isStreaming !== this._lastStreamingState) {
      this._lastStreamingState = isStreaming;
      this._startRefreshTimer();
      // Force immediate image refresh on state change
      this._imgTimestamp = Date.now();
      this._updateImage();
    }

    // ── Last event ─────────────────────────────────────────────────────────
    const lastEventState   = hass.states[ents.last_event];
    const infoLastEvent    = this.shadowRoot.getElementById("info-last-event");
    const lastEventOverlay = this.shadowRoot.getElementById("last-event-overlay");

    let lastEventStr = "—";
    if (lastEventState && lastEventState.state && lastEventState.state !== "unavailable") {
      try {
        const d = new Date(lastEventState.state);
        lastEventStr = isNaN(d) ? lastEventState.state : this._formatDatetime(d);
      } catch (_) {
        lastEventStr = lastEventState.state;
      }
    }
    // Fallback: read from camera attributes
    if (lastEventStr === "—") {
      const camAttr = hass.states[ents.camera]?.attributes?.last_event;
      if (camAttr) lastEventStr = camAttr.slice(0, 16).replace("T", " ");
    }
    if (infoLastEvent)    infoLastEvent.textContent = lastEventStr;
    if (lastEventOverlay) lastEventOverlay.textContent = lastEventStr !== "—" ? `Last: ${lastEventStr}` : "";

    // ── Events today ───────────────────────────────────────────────────────
    const evTodayState  = hass.states[ents.events_today];
    const infoEvToday   = this.shadowRoot.getElementById("info-events-today");
    const evOverlay     = this.shadowRoot.getElementById("events-overlay");
    const evCount       = evTodayState?.state ?? "—";
    if (infoEvToday) infoEvToday.textContent = evCount !== "—" ? `${evCount} events` : "—";
    if (evOverlay)   evOverlay.textContent   = evCount !== "—" ? `${evCount} events today` : "";
  }

  // ── Image update (called by timer) ────────────────────────────────────────
  _updateImage() {
    const img = this.shadowRoot.getElementById("cam-img");
    if (!img || !this._hass) return;

    const camEntity = this._entities.camera;
    // Use HA's camera proxy endpoint — appends timestamp to bypass cache
    img.src = `/api/camera_proxy/${camEntity}?token=${this._hass.states[camEntity]?.attributes?.access_token || ""}&time=${this._imgTimestamp}`;
  }

  // ── Helpers ───────────────────────────────────────────────────────────────
  _toggleStream() {
    const isOn   = this._isStreaming();
    const domain  = "switch";
    const service = isOn ? "turn_off" : "turn_on";
    this._callService(domain, service, { entity_id: this._entities.switch });
  }

  _callService(domain, service, data) {
    if (!this._hass) return;
    this._hass.callService(domain, service, data).catch((err) =>
      console.warn("bosch-camera-card: service error", err)
    );
  }

  _formatDatetime(d) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  // ── Required HA card metadata ─────────────────────────────────────────────
  static getConfigElement() {
    return null; // No visual config editor — use YAML
  }

  static getStubConfig() {
    return { camera_entity: "camera.bosch_garten" };
  }

  getCardSize() {
    return 4;
  }
}

customElements.define("bosch-camera-card", BoschCameraCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type:        "bosch-camera-card",
  name:        "Bosch Camera Card",
  description: "Card for Bosch Smart Home cameras with live streaming state and controls",
  preview:     false,
});
