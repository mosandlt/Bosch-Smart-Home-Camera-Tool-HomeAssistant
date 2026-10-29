/**
 * AI Alert Timeline Card — Custom Lovelace Card
 * Repo:    https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant
 * License: MIT
 *
 * This file is auto-generated from src/ai-alert-timeline-card.js by
 * scripts/build-card.mjs. Do not edit directly — edit the src file and
 * rebuild. Comments are stripped to reduce the gzipped payload size.
 */
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
    close: "Close"
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
    close: "Schließen"
  }
};

function aiLang(hass) {
  const l = (hass && hass.language || "en").toLowerCase();
  return l.startsWith("de") ? "de" : "en";
}

function aiT(hass, key) {
  const lang = aiLang(hass);
  return AI_TIMELINE_I18N[lang] && AI_TIMELINE_I18N[lang][key] || AI_TIMELINE_I18N.en[key] || key;
}

function aiScoreColor(score) {
  const n = Number(score);
  if (!Number.isFinite(n)) return "#888";
  if (n >= 7) return "#f44336";
  if (n >= 4) return "#ff9800";
  return "#4caf50";
}

function aiEsc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }[c]));
}

function aiCameraLabel(hass, entityId) {
  const st = hass.states[entityId];
  const friendly = st && st.attributes && st.attributes.friendly_name;
  if (friendly) {
    return friendly.replace(/\s*AI\s*Alert\s*Score\s*$/i, "").trim() || friendly;
  }
  const m = entityId.match(/^sensor\.(?:bosch_)?(.+?)_ai_alert_score$/);
  return m ? m[1].replace(/_/g, " ") : entityId;
}

function aiImageEntityFor(scoreEntityId) {
  const m = scoreEntityId.match(/^sensor\.(.+)_ai_alert_score$/);
  if (!m) return null;
  return `image.${m[1]}_ai_latest_alert`;
}

function aiRelativeTime(hass, iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diffSec = Math.max(0, (Date.now() - d.getTime()) / 1e3);
  if (diffSec < 60) return `${Math.floor(diffSec)}s`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h`;
  return `${Math.floor(diffSec / 86400)}d`;
}

function aiDayLabel(hass, dateStr) {
  const today = new Date;
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
    this.attachShadow({
      mode: "open"
    });
    this._hass = null;
    this._config = null;
    this._initialized = false;
    this._alerts = [];
    this._loading = false;
    this._loadError = false;
    this._hiddenCameras = new Set;
    this._expanded = new Set;
    this._refreshTimer = null;
  }
  setConfig(config) {
    this._config = {
      cameras: Array.isArray(config.cameras) ? config.cameras : [],
      days: Number.isFinite(config.days) ? config.days : 7,
      title: config.title || undefined,
      ...config
    };
    if (this.isConnected) this._init();
  }
  set hass(hass) {
    this._hass = hass;
    if (!this._initialized) this._init(); else this._renderShell();
  }
  connectedCallback() {
    if (this._hass && !this._initialized) this._init();
    if (!this._refreshTimer) {
      this._refreshTimer = setInterval(() => this._loadHistory(), 5 * 60 * 1e3);
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
  _resolveCameraEntities() {
    if (!this._hass) return [];
    if (this._config.cameras && this._config.cameras.length) {
      return this._config.cameras.map(c => c.startsWith("sensor.") ? c : `sensor.${c.replace(/^bosch_/, "")}_ai_alert_score`).filter(id => id in this._hass.states);
    }
    return Object.keys(this._hass.states).filter(id => id.startsWith("sensor.") && id.endsWith("_ai_alert_score")).sort();
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
    const start = new Date(Date.now() - days * 86400 * 1e3).toISOString();
    try {
      const result = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: start,
        entity_ids: entities,
        minimal_response: false,
        no_attributes: false,
        significant_changes_only: false
      });
      const flat = [];
      for (const entityId of entities) {
        const rows = result && result[entityId] || [];
        for (const row of rows) {
          const state = row.state !== undefined ? row.state : row.s;
          const attrs = row.attributes !== undefined ? row.attributes : row.a || {};
          const lastChanged = row.last_changed || row.lc || row.last_updated || row.lu;
          if (state === undefined || state === null) continue;
          if (state === "unavailable" || state === "unknown") continue;
          const score = Number(state);
          if (!Number.isFinite(score) || score <= 0) continue;
          flat.push({
            key: `${entityId}|${lastChanged}`,
            entityId: entityId,
            score: score,
            short: attrs.short || "",
            detail: attrs.detail || "",
            direction: attrs.direction || "",
            carrying: attrs.carrying || "",
            activity: attrs.activity || "",
            gate_state: attrs.gate_state || "",
            gate_risk: !!attrs.gate_risk,
            known_person: !!attrs.known_person,
            generated_at: attrs.generated_at || lastChanged,
            timestamp: lastChanged
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
    if (this._hiddenCameras.has(entityId)) this._hiddenCameras.delete(entityId); else this._hiddenCameras.add(entityId);
    this._renderShell();
  }
  _toggleExpanded(key) {
    if (this._expanded.has(key)) this._expanded.delete(key); else this._expanded.add(key);
    this._renderShell();
  }
  _visibleAlerts() {
    if (!this._hiddenCameras.size) return this._alerts;
    return this._alerts.filter(a => !this._hiddenCameras.has(a.entityId));
  }
  _groupedByDay(alerts) {
    const groups = new Map;
    for (const a of alerts) {
      const d = new Date(a.timestamp);
      const dateStr = Number.isNaN(d.getTime()) ? "unknown" : `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      if (!groups.has(dateStr)) groups.set(dateStr, []);
      groups.get(dateStr).push(a);
    }
    return groups;
  }
  _newestPerCamera() {
    const newest = new Map;
    for (const a of this._alerts) {
      if (!newest.has(a.entityId)) newest.set(a.entityId, a.key);
    }
    return newest;
  }
  _renderChips(entities) {
    if (entities.length < 2) return "";
    const chips = entities.map(id => {
      const active = !this._hiddenCameras.has(id);
      return `<button class="chip ${active ? "active" : ""}" data-cam="${aiEsc(id)}">\n        ${aiEsc(aiCameraLabel(this._hass, id))}\n      </button>`;
    }).join("");
    return `<div class="chips">${chips}</div>`;
  }
  _renderAlertRow(alert, newestPerCamera) {
    const expanded = this._expanded.has(alert.key);
    const color = aiScoreColor(alert.score);
    const imgEntity = aiImageEntityFor(alert.entityId);
    const isNewestForCam = newestPerCamera.get(alert.entityId) === alert.key;
    const imgState = imgEntity && this._hass.states[imgEntity];
    const thumbUrl = isNewestForCam && imgState && imgState.attributes && imgState.attributes.entity_picture ? imgState.attributes.entity_picture : null;
    const thumb = thumbUrl ? `<img class="thumb" src="${aiEsc(thumbUrl)}" alt="">` : `<div class="thumb thumb-placeholder">📷</div>`;
    const detailBlock = expanded ? `\n      <div class="detail-block">\n        ${alert.detail ? `<p class="detail-text">${aiEsc(alert.detail)}</p>` : ""}\n        <div class="detail-grid">\n          ${alert.direction ? `<div><b>${aiEsc(aiT(this._hass, "direction"))}:</b> ${aiEsc(alert.direction)}</div>` : ""}\n          ${alert.carrying ? `<div><b>${aiEsc(aiT(this._hass, "carrying"))}:</b> ${aiEsc(alert.carrying)}</div>` : ""}\n          ${alert.activity ? `<div><b>${aiEsc(aiT(this._hass, "activity"))}:</b> ${aiEsc(alert.activity)}</div>` : ""}\n          ${alert.gate_state ? `<div><b>${aiEsc(aiT(this._hass, "gate_state"))}:</b> ${aiEsc(alert.gate_state)}</div>` : ""}\n          ${alert.gate_risk ? `<div class="risk"><b>${aiEsc(aiT(this._hass, "gate_risk"))}</b></div>` : ""}\n          ${alert.known_person ? `<div class="known"><b>${aiEsc(aiT(this._hass, "known_person"))}</b></div>` : ""}\n        </div>\n        ${thumbUrl ? `<img class="thumb-large" src="${aiEsc(thumbUrl)}" alt="">` : ""}\n      </div>` : "";
    return `\n      <div class="row ${expanded ? "expanded" : ""}" data-key="${aiEsc(alert.key)}">\n        <div class="row-main">\n          ${thumb}\n          <div class="badge" style="background:${color}">${aiEsc(alert.score)}</div>\n          <div class="row-body">\n            <div class="row-top">\n              <span class="row-cam">${aiEsc(aiCameraLabel(this._hass, alert.entityId))}</span>\n              <span class="row-time">${aiEsc(aiRelativeTime(this._hass, alert.timestamp))}</span>\n            </div>\n            <div class="row-short">${aiEsc(alert.short || alert.detail || "")}</div>\n          </div>\n          ${alert.known_person ? '<span class="tag known-tag">👤</span>' : ""}\n          ${alert.gate_risk ? '<span class="tag risk-tag">⚠</span>' : ""}\n        </div>\n        ${detailBlock}\n      </div>`;
  }
  _renderShell() {
    if (!this._hass || !this._config) return;
    if (!this.shadowRoot) this.attachShadow({
      mode: "open"
    });
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
        dayBlocks.push(`\n          <div class="day-group">\n            <div class="day-label">${aiEsc(aiDayLabel(this._hass, dateStr))}</div>\n            ${alerts.map(a => this._renderAlertRow(a, newestPerCamera)).join("")}\n          </div>`);
      }
      body = dayBlocks.join("");
    }
    this.shadowRoot.innerHTML = `\n      <style>\n        :host{display:block;background:var(--card-background-color,#1c1c1c);border-radius:12px;\n          padding:16px;color:var(--primary-text-color,#fff);font-family:var(--paper-font-body1_-_font-family,Roboto)}\n        .header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}\n        h2{margin:0;font-size:16px;font-weight:500}\n        .refresh-btn{background:none;border:none;color:var(--secondary-text-color,#aaa);cursor:pointer;\n          font-size:13px;padding:4px 8px;border-radius:6px}\n        .refresh-btn:hover{background:rgba(255,255,255,0.08)}\n        .chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}\n        .chip{border:1px solid var(--divider-color,#444);background:transparent;color:var(--primary-text-color,#fff);\n          border-radius:16px;padding:4px 12px;font-size:12px;cursor:pointer;opacity:0.5}\n        .chip.active{opacity:1;background:rgba(3,169,244,0.15);border-color:var(--primary-color,#03a9f4)}\n        .day-group{margin-bottom:14px}\n        .day-label{font-size:12px;color:var(--secondary-text-color,#aaa);text-transform:uppercase;\n          letter-spacing:0.5px;margin-bottom:6px;font-weight:500}\n        .row{border-bottom:1px solid var(--divider-color,#333);padding:8px 0;cursor:pointer}\n        .row:last-child{border-bottom:none}\n        .row-main{display:flex;align-items:center;gap:10px}\n        .thumb{width:44px;height:44px;border-radius:8px;object-fit:cover;flex-shrink:0;background:#111}\n        .thumb-placeholder{display:flex;align-items:center;justify-content:center;font-size:20px;opacity:0.5}\n        .badge{width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;\n          font-size:13px;font-weight:700;color:#fff;flex-shrink:0}\n        .row-body{flex:1;min-width:0}\n        .row-top{display:flex;justify-content:space-between;font-size:12px;color:var(--secondary-text-color,#aaa)}\n        .row-cam{font-weight:500;color:var(--primary-text-color,#fff)}\n        .row-short{font-size:13px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n        .row.expanded .row-short{white-space:normal}\n        .tag{font-size:14px;flex-shrink:0}\n        .detail-block{margin-top:10px;padding:10px;background:rgba(255,255,255,0.04);border-radius:8px;font-size:12px}\n        .detail-text{margin:0 0 8px 0;white-space:pre-wrap}\n        .detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px}\n        .detail-grid .risk,.detail-grid .known{color:#ff9800}\n        .thumb-large{margin-top:8px;max-width:100%;border-radius:8px;display:block}\n        .empty{padding:24px 12px;text-align:center;color:var(--secondary-text-color,#aaa);font-size:13px}\n        .empty.error{color:#f44336}\n      </style>\n      <div class="header">\n        <h2>${aiEsc(title)}</h2>\n        <button class="refresh-btn" id="refresh">${aiEsc(aiT(this._hass, "refresh"))}</button>\n      </div>\n      ${this._renderChips(entities)}\n      ${body}`;
    const refreshBtn = this.shadowRoot.getElementById("refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", () => this._loadHistory());
    this.shadowRoot.querySelectorAll(".chip").forEach(chip => {
      chip.addEventListener("click", () => this._toggleCamera(chip.dataset.cam));
    });
    this.shadowRoot.querySelectorAll(".row").forEach(row => {
      row.addEventListener("click", () => this._toggleExpanded(row.dataset.key));
    });
  }
  static getStubConfig(hass) {
    const states = hass && hass.states || {};
    const ids = Object.keys(states).filter(id => id.startsWith("sensor.") && id.endsWith("_ai_alert_score"));
    return {
      cameras: ids,
      days: 7
    };
  }
  getCardSize() {
    return 4;
  }
}

customElements.define("ai-alert-timeline-card", AiAlertTimelineCard);

window.customCards = window.customCards || [];

window.customCards.push({
  type: "ai-alert-timeline-card",
  name: "AI Camera Alert Timeline",
  description: "Day-grouped timeline of AI-scored motion alerts (score, summary, detail, thumbnail) across one or more Bosch cameras, with per-camera filter chips and tap-to-expand rows.",
  preview: false
});