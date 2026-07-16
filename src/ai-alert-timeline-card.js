/**
 * AI Alert Timeline Card — Custom Lovelace Card
 * ==============================================
 * Day-grouped timeline of AI-scored motion alerts across one or more Bosch
 * cameras. Reads the native "AI Camera Analysis" entities:
 *   - sensor.<cam>_ai_alert_score      (state=score 1-10, attrs: short/detail/
 *                                        direction/carrying/activity/gate_state/
 *                                        gate_risk/known_person/image_path/
 *                                        generated_at)
 *   - image.<cam>_ai_latest_alert      (latest alert snapshot, standard HA
 *                                        `image` entity — thumbnail source)
 *
 * This card is intentionally a SEPARATE, self-contained custom element from
 * bosch-camera-card.js (own shadow DOM, own bundle, own Lovelace resource).
 * It does not import from or depend on the big card in any way.
 *
 * Installation:
 *   1. Copy ai-alert-timeline-card.js to /config/www/ai-alert-timeline-card.js
 *      (the bosch_shc_camera integration ships + auto-registers this for you,
 *      same as bosch-camera-card.js — no manual step normally required).
 *   2. Card YAML:
 *        type: custom:ai-alert-timeline-card
 *        cameras:                      # optional — omit to auto-discover
 *          - sensor.bosch_terrasse_ai_alert_score
 *        days: 7                       # optional, default 7 (history window)
 *        title: AI Camera Alerts       # optional
 *
 * KNOWN v1 LIMITATION: the `image.<cam>_ai_latest_alert` entity only ever
 * holds the camera's SINGLE most recent alert snapshot — there is no
 * per-history-entry image available client-side (the alert JSONL image
 * store is server-side only, out of scope for this card per the Phase 6
 * plan). So a thumbnail is only ever shown on the newest row per camera;
 * older rows in the same camera's history show a placeholder icon instead.
 * Solving this properly would need a new backend "list alert images" API,
 * which is a separate future pass, not this one.
 */

// ---------------------------------------------------------------------------
// Minimal i18n — this file is a standalone bundle (no shared module scope
// with bosch-camera-card.js), so it carries its own small string table
// rather than importing the big card's cardT(). English fallback always
// applies for any key/language not covered here.
// ---------------------------------------------------------------------------
const AI_TIMELINE_I18N = {
  en: {
    title: "AI Camera Alerts",
    loading: "Loading alerts…",
    load_error: "Failed to load alert history.",
    no_alerts: "No AI alerts in this window.",
    no_cameras: "No AI alert sensors found (sensor.*_ai_alert_score).",
    refresh: "Refresh",
    today: "Today",
    yesterday: "Yesterday",
    all_cameras: "All",
    unknown_camera: "Camera",
    known_person: "Known person",
    direction: "Direction",
    carrying: "Carrying",
    activity: "Activity",
    gate_state: "Gate state",
    gate_risk: "Gate risk",
    close: "Close",
  },
  de: {
    title: "KI-Kamera-Alarme",
    loading: "Alarme werden geladen…",
    load_error: "Alarmverlauf konnte nicht geladen werden.",
    no_alerts: "Keine KI-Alarme in diesem Zeitraum.",
    no_cameras: "Keine KI-Alarm-Sensoren gefunden (sensor.*_ai_alert_score).",
    refresh: "Aktualisieren",
    today: "Heute",
    yesterday: "Gestern",
    all_cameras: "Alle",
    unknown_camera: "Kamera",
    known_person: "Bekannte Person",
    direction: "Richtung",
    carrying: "Trägt",
    activity: "Aktivität",
    gate_state: "Torzustand",
    gate_risk: "Torrisiko",
    close: "Schließen",
  },
};
function aiLang(hass) {
  const l = ((hass && hass.language) || "en").toLowerCase();
  return l.startsWith("de") ? "de" : "en";
}
function aiT(hass, key) {
  const lang = aiLang(hass);
  return (AI_TIMELINE_I18N[lang] && AI_TIMELINE_I18N[lang][key]) || AI_TIMELINE_I18N.en[key] || key;
}

// Score badge color grading: <4 green (low), 4-6 yellow (mid), 7-10 red (high).
function aiScoreColor(score) {
  const n = Number(score);
  if (!Number.isFinite(n)) return "#888";
  if (n >= 7) return "#f44336";
  if (n >= 4) return "#ff9800";
  return "#4caf50";
}

function aiEsc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

// Derive a friendly camera name from the ai_alert_score sensor entity_id /
// its own friendly_name attribute, stripping the trailing suffix HA adds.
function aiCameraLabel(hass, entityId) {
  const st = hass.states[entityId];
  const friendly = st && st.attributes && st.attributes.friendly_name;
  if (friendly) {
    return friendly.replace(/\s*AI\s*Alert\s*Score\s*$/i, "").trim() || friendly;
  }
  // sensor.bosch_terrasse_ai_alert_score -> "terrasse"
  const m = entityId.match(/^sensor\.(?:bosch_)?(.+?)_ai_alert_score$/);
  return m ? m[1].replace(/_/g, " ") : entityId;
}

// Derive the matching image.<cam>_ai_latest_alert entity_id for a given
// sensor.<cam>_ai_alert_score entity_id. Both platforms are expected to
// share the same "<cam>" slug per the Phase 6 entity-naming convention.
function aiImageEntityFor(scoreEntityId) {
  const m = scoreEntityId.match(/^sensor\.(.+)_ai_alert_score$/);
  if (!m) return null;
  return `image.${m[1]}_ai_latest_alert`;
}

function aiRelativeTime(hass, iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diffSec = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (diffSec < 60) return `${Math.floor(diffSec)}s`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h`;
  return `${Math.floor(diffSec / 86400)}d`;
}

function aiDayLabel(hass, dateStr) {
  const today = new Date();
  const todayStr = today.toISOString().slice(0, 10);
  const y = new Date(today);
  y.setDate(y.getDate() - 1);
  const yStr = y.toISOString().slice(0, 10);
  if (dateStr === todayStr) return aiT(hass, "today");
  if (dateStr === yStr) return aiT(hass, "yesterday");
  return dateStr;
}

class AiAlertTimelineCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = null;
    this._initialized = false;
    this._alerts = []; // flattened, newest first
    this._loading = false;
    this._loadError = false;
    this._hiddenCameras = new Set(); // camera entity_ids toggled off via chips
    this._expanded = new Set(); // alert keys currently expanded
    this._refreshTimer = null;
  }

  setConfig(config) {
    this._config = {
      cameras: Array.isArray(config.cameras) ? config.cameras : [],
      days: Number.isFinite(config.days) ? config.days : 7,
      title: config.title || undefined,
      ...config,
    };
    if (this.isConnected) this._init();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._initialized) this._init();
    else this._renderShell(); // cheap re-render for live camera-name/i18n updates
  }

  connectedCallback() {
    if (this._hass && !this._initialized) this._init();
    if (!this._refreshTimer) {
      // Alerts are event-driven, not continuously polled elsewhere, so a
      // background refresh keeps the timeline current without requiring a
      // manual reload. 5 min matches the general dashboard-refresh cadence
      // used elsewhere in this integration's cards.
      this._refreshTimer = setInterval(() => this._loadHistory(), 5 * 60 * 1000);
    }
  }

  disconnectedCallback() {
    if (this._refreshTimer) {
      clearInterval(this._refreshTimer);
      this._refreshTimer = null;
    }
  }

  _init() {
    if (!this._hass || !this._config) return;
    this._initialized = true;
    this._renderShell();
    this._loadHistory();
  }

  // Resolve which sensor.*_ai_alert_score entities this card should track.
  _resolveCameraEntities() {
    if (!this._hass) return [];
    if (this._config.cameras && this._config.cameras.length) {
      // Config may list either the score sensor itself or a bare camera slug.
      return this._config.cameras.map((c) =>
        c.startsWith("sensor.") ? c : `sensor.${c.replace(/^bosch_/, "")}_ai_alert_score`
      ).filter((id) => id in this._hass.states);
    }
    return Object.keys(this._hass.states)
      .filter((id) => id.startsWith("sensor.") && id.endsWith("_ai_alert_score"))
      .sort();
  }

  async _loadHistory() {
    if (!this._hass) return;
    const entities = this._resolveCameraEntities();
    if (!entities.length) {
      this._alerts = [];
      this._loading = false;
      this._loadError = false;
      this._renderShell();
      return;
    }
    this._loading = true;
    this._loadError = false;
    this._renderShell();

    const days = Math.max(1, Number(this._config.days) || 7);
    const start = new Date(Date.now() - days * 86400 * 1000).toISOString();

    try {
      // Standard HA history WS call — every Lovelace history-based card uses
      // this same command, nothing bespoke. Full (non-minimal) response so
      // every state change carries its attributes, since the ai_alert_score
      // sensor's attributes (short/detail/...) change on every single update.
      const result = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: start,
        entity_ids: entities,
        minimal_response: false,
        no_attributes: false,
        significant_changes_only: false,
      });

      const flat = [];
      for (const entityId of entities) {
        const rows = (result && result[entityId]) || [];
        for (const row of rows) {
          // Normalize both the abbreviated ("s"/"a"/"lc"/"lu") and full
          // ("state"/"attributes"/"last_changed"/"last_updated") shapes HA's
          // history WS command can return, defensively.
          const state = row.state !== undefined ? row.state : row.s;
          const attrs = row.attributes !== undefined ? row.attributes : row.a || {};
          const lastChanged = row.last_changed || row.lc || row.last_updated || row.lu;
          if (state === undefined || state === null) continue;
          if (state === "unavailable" || state === "unknown") continue;
          const score = Number(state);
          if (!Number.isFinite(score) || score <= 0) continue; // drop no-alert states
          flat.push({
            key: `${entityId}|${lastChanged}`,
            entityId,
            score,
            short: attrs.short || "",
            detail: attrs.detail || "",
            direction: attrs.direction || "",
            carrying: attrs.carrying || "",
            activity: attrs.activity || "",
            gate_state: attrs.gate_state || "",
            gate_risk: !!attrs.gate_risk,
            known_person: !!attrs.known_person,
            generated_at: attrs.generated_at || lastChanged,
            timestamp: lastChanged,
          });
        }
      }
      flat.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
      this._alerts = flat;
      this._loading = false;
    } catch {
      this._loading = false;
      this._loadError = true;
    }
    this._renderShell();
  }

  _toggleCamera(entityId) {
    if (this._hiddenCameras.has(entityId)) this._hiddenCameras.delete(entityId);
    else this._hiddenCameras.add(entityId);
    this._renderShell();
  }

  _toggleExpanded(key) {
    if (this._expanded.has(key)) this._expanded.delete(key);
    else this._expanded.add(key);
    this._renderShell();
  }

  _visibleAlerts() {
    if (!this._hiddenCameras.size) return this._alerts;
    return this._alerts.filter((a) => !this._hiddenCameras.has(a.entityId));
  }

  _groupedByDay(alerts) {
    const groups = new Map(); // dateStr -> alerts[]
    for (const a of alerts) {
      const d = new Date(a.timestamp);
      const dateStr = Number.isNaN(d.getTime())
        ? "unknown"
        : `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      if (!groups.has(dateStr)) groups.set(dateStr, []);
      groups.get(dateStr).push(a);
    }
    return groups; // Map preserves insertion order; alerts already sorted desc
  }

  _newestPerCamera() {
    const newest = new Map();
    for (const a of this._alerts) {
      if (!newest.has(a.entityId)) newest.set(a.entityId, a.key);
    }
    return newest;
  }

  _renderChips(entities) {
    if (entities.length < 2) return "";
    const chips = entities.map((id) => {
      const active = !this._hiddenCameras.has(id);
      return `<button class="chip ${active ? "active" : ""}" data-cam="${aiEsc(id)}">
        ${aiEsc(aiCameraLabel(this._hass, id))}
      </button>`;
    }).join("");
    return `<div class="chips">${chips}</div>`;
  }

  _renderAlertRow(alert, newestPerCamera) {
    const expanded = this._expanded.has(alert.key);
    const color = aiScoreColor(alert.score);
    const imgEntity = aiImageEntityFor(alert.entityId);
    const isNewestForCam = newestPerCamera.get(alert.entityId) === alert.key;
    const imgState = imgEntity && this._hass.states[imgEntity];
    const thumbUrl = isNewestForCam && imgState && imgState.attributes && imgState.attributes.entity_picture
      ? imgState.attributes.entity_picture
      : null;

    const thumb = thumbUrl
      ? `<img class="thumb" src="${aiEsc(thumbUrl)}" alt="">`
      : `<div class="thumb thumb-placeholder">📷</div>`;

    const detailBlock = expanded ? `
      <div class="detail-block">
        ${alert.detail ? `<p class="detail-text">${aiEsc(alert.detail)}</p>` : ""}
        <div class="detail-grid">
          ${alert.direction ? `<div><b>${aiEsc(aiT(this._hass, "direction"))}:</b> ${aiEsc(alert.direction)}</div>` : ""}
          ${alert.carrying ? `<div><b>${aiEsc(aiT(this._hass, "carrying"))}:</b> ${aiEsc(alert.carrying)}</div>` : ""}
          ${alert.activity ? `<div><b>${aiEsc(aiT(this._hass, "activity"))}:</b> ${aiEsc(alert.activity)}</div>` : ""}
          ${alert.gate_state ? `<div><b>${aiEsc(aiT(this._hass, "gate_state"))}:</b> ${aiEsc(alert.gate_state)}</div>` : ""}
          ${alert.gate_risk ? `<div class="risk"><b>${aiEsc(aiT(this._hass, "gate_risk"))}</b></div>` : ""}
          ${alert.known_person ? `<div class="known"><b>${aiEsc(aiT(this._hass, "known_person"))}</b></div>` : ""}
        </div>
        ${thumbUrl ? `<img class="thumb-large" src="${aiEsc(thumbUrl)}" alt="">` : ""}
      </div>` : "";

    return `
      <div class="row ${expanded ? "expanded" : ""}" data-key="${aiEsc(alert.key)}">
        <div class="row-main">
          ${thumb}
          <div class="badge" style="background:${color}">${aiEsc(alert.score)}</div>
          <div class="row-body">
            <div class="row-top">
              <span class="row-cam">${aiEsc(aiCameraLabel(this._hass, alert.entityId))}</span>
              <span class="row-time">${aiEsc(aiRelativeTime(this._hass, alert.timestamp))}</span>
            </div>
            <div class="row-short">${aiEsc(alert.short || alert.detail || "")}</div>
          </div>
          ${alert.known_person ? '<span class="tag known-tag">👤</span>' : ""}
          ${alert.gate_risk ? '<span class="tag risk-tag">⚠</span>' : ""}
        </div>
        ${detailBlock}
      </div>`;
  }

  _renderShell() {
    if (!this._hass || !this._config) return;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });

    const entities = this._resolveCameraEntities();
    const title = this._config.title || aiT(this._hass, "title");
    const visible = this._visibleAlerts();
    const groups = this._groupedByDay(visible);
    const newestPerCamera = this._newestPerCamera();

    let body;
    if (!entities.length) {
      body = `<div class="empty">${aiEsc(aiT(this._hass, "no_cameras"))}</div>`;
    } else if (this._loading && !this._alerts.length) {
      body = `<div class="empty">${aiEsc(aiT(this._hass, "loading"))}</div>`;
    } else if (this._loadError) {
      body = `<div class="empty error">${aiEsc(aiT(this._hass, "load_error"))}</div>`;
    } else if (!visible.length) {
      body = `<div class="empty">${aiEsc(aiT(this._hass, "no_alerts"))}</div>`;
    } else {
      const dayBlocks = [];
      for (const [dateStr, alerts] of groups.entries()) {
        dayBlocks.push(`
          <div class="day-group">
            <div class="day-label">${aiEsc(aiDayLabel(this._hass, dateStr))}</div>
            ${alerts.map((a) => this._renderAlertRow(a, newestPerCamera)).join("")}
          </div>`);
      }
      body = dayBlocks.join("");
    }

    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;background:var(--card-background-color,#1c1c1c);border-radius:12px;
          padding:16px;color:var(--primary-text-color,#fff);font-family:var(--paper-font-body1_-_font-family,Roboto)}
        .header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
        h2{margin:0;font-size:16px;font-weight:500}
        .refresh-btn{background:none;border:none;color:var(--secondary-text-color,#aaa);cursor:pointer;
          font-size:13px;padding:4px 8px;border-radius:6px}
        .refresh-btn:hover{background:rgba(255,255,255,0.08)}
        .chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
        .chip{border:1px solid var(--divider-color,#444);background:transparent;color:var(--primary-text-color,#fff);
          border-radius:16px;padding:4px 12px;font-size:12px;cursor:pointer;opacity:0.5}
        .chip.active{opacity:1;background:rgba(3,169,244,0.15);border-color:var(--primary-color,#03a9f4)}
        .day-group{margin-bottom:14px}
        .day-label{font-size:12px;color:var(--secondary-text-color,#aaa);text-transform:uppercase;
          letter-spacing:0.5px;margin-bottom:6px;font-weight:500}
        .row{border-bottom:1px solid var(--divider-color,#333);padding:8px 0;cursor:pointer}
        .row:last-child{border-bottom:none}
        .row-main{display:flex;align-items:center;gap:10px}
        .thumb{width:44px;height:44px;border-radius:8px;object-fit:cover;flex-shrink:0;background:#111}
        .thumb-placeholder{display:flex;align-items:center;justify-content:center;font-size:20px;opacity:0.5}
        .badge{width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;
          font-size:13px;font-weight:700;color:#fff;flex-shrink:0}
        .row-body{flex:1;min-width:0}
        .row-top{display:flex;justify-content:space-between;font-size:12px;color:var(--secondary-text-color,#aaa)}
        .row-cam{font-weight:500;color:var(--primary-text-color,#fff)}
        .row-short{font-size:13px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .row.expanded .row-short{white-space:normal}
        .tag{font-size:14px;flex-shrink:0}
        .detail-block{margin-top:10px;padding:10px;background:rgba(255,255,255,0.04);border-radius:8px;font-size:12px}
        .detail-text{margin:0 0 8px 0;white-space:pre-wrap}
        .detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px}
        .detail-grid .risk,.detail-grid .known{color:#ff9800}
        .thumb-large{margin-top:8px;max-width:100%;border-radius:8px;display:block}
        .empty{padding:24px 12px;text-align:center;color:var(--secondary-text-color,#aaa);font-size:13px}
        .empty.error{color:#f44336}
      </style>
      <div class="header">
        <h2>${aiEsc(title)}</h2>
        <button class="refresh-btn" id="refresh">${aiEsc(aiT(this._hass, "refresh"))}</button>
      </div>
      ${this._renderChips(entities)}
      ${body}`;

    const refreshBtn = this.shadowRoot.getElementById("refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", () => this._loadHistory());

    this.shadowRoot.querySelectorAll(".chip").forEach((chip) => {
      chip.addEventListener("click", () => this._toggleCamera(chip.dataset.cam));
    });
    this.shadowRoot.querySelectorAll(".row").forEach((row) => {
      row.addEventListener("click", () => this._toggleExpanded(row.dataset.key));
    });
  }

  static getStubConfig(hass) {
    const states = (hass && hass.states) || {};
    const ids = Object.keys(states).filter((id) => id.startsWith("sensor.") && id.endsWith("_ai_alert_score"));
    return { cameras: ids, days: 7 };
  }

  getCardSize() {
    return 4;
  }
}

customElements.define("ai-alert-timeline-card", AiAlertTimelineCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type:        "ai-alert-timeline-card",
  name:        "AI Camera Alert Timeline",
  description: "Day-grouped timeline of AI-scored motion alerts (score, summary, detail, thumbnail) across one or more Bosch cameras, with per-camera filter chips and tap-to-expand rows.",
  preview:     false,
});
