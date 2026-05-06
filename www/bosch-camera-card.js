/**
 * Bosch Camera Card — Custom Lovelace Card
 * Repo:    https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant
 * Docs:    https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/blob/main/docs/card-architecture.md
 * License: MIT
 *
 * This file is auto-generated from src/bosch-camera-card.js by
 * scripts/build-card.mjs. Do not edit directly — edit the src file and
 * rebuild. Comments are stripped to reduce the gzipped payload size.
 */
const CARD_VERSION = "2.11.4";

const BOSCH_BUFFER_PROFILES = {
  latency: {
    liveSyncDurationCount: 2,
    liveMaxLatencyDurationCount: 4,
    maxBufferLength: 8,
    maxMaxBufferLength: 14,
    lowLatencyMode: true
  },
  balanced: {
    liveSyncDurationCount: 4,
    liveMaxLatencyDurationCount: 8,
    maxBufferLength: 14,
    maxMaxBufferLength: 22,
    lowLatencyMode: false
  },
  stable: {
    liveSyncDurationCount: 6,
    liveMaxLatencyDurationCount: 12,
    maxBufferLength: 22,
    maxMaxBufferLength: 28,
    lowLatencyMode: false
  }
};

class BoschCameraCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({
      mode: "open"
    });
    this._hass = null;
    this._config = null;
    this._refreshTimer = null;
    this._imgTimestamp = Date.now();
    this._lastStreaming = null;
    this._streamConnecting = false;
    this._connectSteps = null;
    this._waitingForStream = false;
    this._lastMotionCoordKey = null;
    this._lastPrivacyMaskKey = null;
    this._lastPrivacy = null;
    this._imageLoaded = false;
    this._loadingOverlay = false;
    this._loadingTimeout = null;
    this._storageKey = null;
    this._loadRetries = 0;
    this._snapshotPollTimer = null;
    this._liveVideoActive = false;
    this._startingLiveVideo = false;
    this._hls = null;
    this._extCompanion = (() => {
      const isCompanion = /Home\s?Assistant/i.test(navigator.userAgent || "");
      if (!isCompanion) return false;
      const h = (location.hostname || "").toLowerCase();
      if (!h) return false;
      if (h === "localhost" || h === "127.0.0.1" || h === "::1") return false;
      if (h.endsWith(".local")) return false;
      if (/^10\./.test(h)) return false;
      if (/^192\.168\./.test(h)) return false;
      if (/^172\.(1[6-9]|2\d|3[01])\./.test(h)) return false;
      if (/^fe80:/i.test(h)) return false;
      return true;
    })();
    this._timerStreaming = false;
    this._optimistic = {};
    this._optimisticTimers = {};
    this._errorFeedbackTimers = {};
    this._entityToBtnId = {};
    this._visibilityHandler = null;
    this._lastEventState = null;
    this._lastFrameTime = 0;
    this._streamStartTime = 0;
    this._awaitingFresh = false;
    this._showMotionZones = false;
    this._showPrivacyMasks = false;
    this._lastRulesKey = null;
  }
  connectedCallback() {
    this._visibilityHandler = () => this._onVisibilityChange();
    document.addEventListener("visibilitychange", this._visibilityHandler);
    this._pagehideHandler = () => this._stopLiveVideo();
    window.addEventListener("pagehide", this._pagehideHandler);
  }
  setConfig(config) {
    if (!config.camera_entity) {
      throw new Error("bosch-camera-card: camera_entity is required");
    }
    this._config = {
      camera_entity: config.camera_entity,
      title: config.title || null,
      refresh_interval_streaming: config.refresh_interval_streaming ?? 2,
      show_motion_zones: config.show_motion_zones ?? false,
      snapshot_during_warmup: config.snapshot_during_warmup !== false,
      minimal: config.minimal === true
    };
    this._storageKey = `bosch_cam_${config.camera_entity}`;
    const base = config.camera_entity.replace(/^camera\./, "");
    this._base = base;
    this._entities = {
      camera: config.camera_entity,
      switch: config.switch_entity || `switch.${base}_live_stream`,
      audio: config.audio_entity || `switch.${base}_audio`,
      light: config.light_entity || `switch.${base}_camera_light`,
      privacy: config.privacy_entity || `switch.${base}_privacy_mode`,
      notifications: config.notifications_entity || `switch.${base}_notifications`,
      intercom: config.intercom_entity || `switch.${base}_intercom`,
      speaker: config.speaker_entity || `number.${base}_speaker_level`,
      pan: config.pan_entity || `number.${base}_pan_position`,
      quality: config.quality_entity || null,
      push_status: config.push_status_entity || "sensor.bosch_camera_event_detection",
      status: config.status_entity || `sensor.${base}_status`,
      events_today: config.events_today_entity || `sensor.${base}_events_today`,
      last_event: config.last_event_entity || `sensor.${base}_last_event`,
      timestamp: config.timestamp_entity || `switch.${base}_timestamp_overlay`,
      autofollow: config.autofollow_entity || `switch.${base}_auto_follow`,
      motion: config.motion_entity || `switch.${base}_motion_detection`,
      recordSound: config.record_sound_entity || `switch.${base}_record_sound`,
      privacySound: config.privacy_sound_entity || `switch.${base}_privacy_sound`,
      notifMovement: config.notif_movement_entity || `switch.${base}_movement_notifications`,
      notifPerson: config.notif_person_entity || `switch.${base}_person_notifications`,
      notifAudio: config.notif_audio_entity || `switch.${base}_audio_notifications`,
      notifTrouble: config.notif_trouble_entity || `switch.${base}_trouble_notifications`,
      notifAlarm: config.notif_alarm_entity || `switch.${base}_camera_alarm_notifications`,
      wifi: config.wifi_entity || `sensor.${base}_wifi_signal`,
      firmware: config.firmware_entity || `sensor.${base}_firmware_version`,
      ambient: config.ambient_entity || `sensor.${base}_ambient_light`,
      movementToday: config.movement_today_entity || `sensor.${base}_movement_events_today`,
      audioToday: config.audio_today_entity || `sensor.${base}_audio_events_today`,
      motionZones: config.motion_zones_entity || `sensor.${base}_motion_zones`,
      privacyMasks: config.privacy_masks_entity || `sensor.${base}_privacy_masks`,
      streamStatus: config.stream_status_entity || `sensor.${base}_stream_status`,
      ambientSchedule: config.ambient_schedule_entity || `sensor.${base}_dauerlicht_zeitplan`,
      scheduleRules: config.rules_entity || `sensor.${base}_schedule_rules`,
      frontLight: config.front_light_entity || `switch.${base}_front_light`,
      wallwasher: config.wallwasher_entity || `switch.${base}_wallwasher`,
      frontLightIntensity: config.front_light_intensity_entity || `number.${base}_front_light_intensity`,
      siren: config.siren_entity || `button.${base}_siren`,
      statusLed: config.status_led_entity || `switch.${base}_status_led`,
      lensElevation: config.lens_elevation_entity || `number.${base}_lens_elevation`,
      micLevel: config.mic_level_entity || `number.${base}_microphone_level`,
      colorTemp: config.color_temp_entity || `number.${base}_color_temperature`,
      motionLight: config.motion_light_entity || `switch.${base}_licht_bei_bewegung`,
      ambientLight: config.ambient_light_entity || `switch.${base}_dauerlicht`,
      intrusionDetection: config.intrusion_entity || `switch.${base}_einbrucherkennung`,
      motionSensitivity: config.motion_sensitivity_entity || `number.${base}_bewegungslicht_empfindlichkeit`,
      automations: config.automations || [],
      _autoDiscoverAutomations: !config.automations || config.automations.length === 0,
      topLedLight: config.top_led_light_entity || `light.${base}_oberes_licht`,
      bottomLedLight: config.bottom_led_light_entity || `light.${base}_unteres_licht`,
      frontLightEntity: config.front_light_color_entity || `light.${base}_frontlicht`,
      topBrightness: config.top_brightness_entity || `number.${base}_helligkeit_oberes_licht`,
      bottomBrightness: config.bottom_brightness_entity || `number.${base}_helligkeit_unteres_licht`,
      alarmSystemArm: config.alarm_system_arm_entity || `switch.${base}_alarmanlage`,
      alarmMode: config.alarm_mode_entity || `switch.${base}_sirene`,
      preAlarm: config.pre_alarm_entity || `switch.${base}_pre_alarm`,
      audioAlarm: config.audio_alarm_entity || `switch.${base}_audio_plus`,
      alarmState: config.alarm_state_entity || `sensor.${base}_alarm_status`,
      sirenDuration: config.siren_duration_entity || `number.${base}_sirenen_dauer`,
      alarmActivationDelay: config.alarm_activation_delay_entity || `number.${base}_alarm_verzogerung`,
      preAlarmDelay: config.prealarm_delay_entity || `number.${base}_pre_alarm_dauer`,
      powerLedBrightness: config.power_led_entity || `number.${base}_power_led`,
      audioAlarmSensitivity: config.audio_alarm_sensitivity_entity || `number.${base}_audio_plus_empfindlichkeit`,
      imageRotation180: config.image_rotation_180_entity || `switch.${base}_bild_180deg_drehen`
    };
    this._showMotionZones = this._config.show_motion_zones;
    this.classList.toggle("minimal", this._config.minimal);
    this.classList.remove("overflow-open");
    this._render();
    this._restoreCachedImage();
    this._startRefreshTimer();
    this._loadHlsJs().catch(() => {});
  }
  set hass(hass) {
    const firstHass = !this._hass;
    this._hass = hass;
    if (this._entities._autoDiscoverAutomations && hass) {
      if (!this._autoDiscoveryDone) {
        this._autoDiscoveryDone = true;
        this._discoverAutomationsViaWs(hass);
      }
    }
    this._applyImageRotation180();
    this._update();
    if (firstHass) {
      this._awaitingFresh = true;
      if (this._imageLoaded) {
        this._setLoadingOverlay(true, "Aktualisiere…");
      }
      this._triggerFreshSnapshot();
    }
  }
  _applyImageRotation180() {
    if (!this._hass || !this.shadowRoot) return;
    const wrap = this.shadowRoot.querySelector(".img-wrapper");
    if (!wrap) return;
    const ent = this._hass.states[this._entities.imageRotation180];
    const on = ent && ent.state === "on";
    wrap.classList.toggle("rotated-180", !!on);
  }
  disconnectedCallback() {
    this._stopRefreshTimer();
    if (this._visibilityHandler) {
      document.removeEventListener("visibilitychange", this._visibilityHandler);
      this._visibilityHandler = null;
    }
    if (this._pagehideHandler) {
      window.removeEventListener("pagehide", this._pagehideHandler);
      this._pagehideHandler = null;
    }
    if (this._fsClickOut) {
      document.removeEventListener("click", this._fsClickOut);
      this._fsClickOut = null;
    }
    if (this._fsKeyDown) {
      document.removeEventListener("keydown", this._fsKeyDown);
      this._fsKeyDown = null;
    }
    if (this._loadingTimeout) clearTimeout(this._loadingTimeout);
    if (this._snapshotPollTimer) clearTimeout(this._snapshotPollTimer);
    Object.values(this._optimisticTimers).forEach(t => clearTimeout(t));
    if (this._errorFeedbackTimers) {
      Object.values(this._errorFeedbackTimers).forEach(t => clearTimeout(t));
      this._errorFeedbackTimers = {};
    }
    this._stopLiveVideo();
  }
  _startRefreshTimer() {
    this._stopRefreshTimer();
    if (this._liveVideoActive || this._startingLiveVideo) return;
    if (this._isStreaming()) return;
    let interval;
    if (document.visibilityState === "hidden") {
      interval = 1800;
    } else {
      interval = 60;
    }
    this._refreshTimer = setInterval(() => {
      this._triggerFreshSnapshot();
    }, interval * 1e3);
  }
  _onVisibilityChange() {
    if (document.visibilityState === "visible" && !this._liveVideoActive) {
      this._triggerFreshSnapshot();
      this._pullFreshSwitchStates();
    }
    this._startRefreshTimer();
  }
  async _pullFreshSwitchStates() {
    if (!this._hass) return;
    const ids = [ this._entities.switch, this._entities.privacy, this._entities.audio, this._entities.light ].filter(Boolean);
    let changed = false;
    for (const id of ids) {
      try {
        const fresh = await this._hass.callApi("GET", `states/${id}`);
        if (fresh && fresh.state && this._hass.states[id]?.state !== fresh.state) {
          delete this._optimistic[id];
          changed = true;
        }
      } catch (e) {}
    }
    if (changed) this._update();
  }
  _stopRefreshTimer() {
    if (this._refreshTimer) {
      clearInterval(this._refreshTimer);
      this._refreshTimer = null;
    }
  }
  _isStreaming() {
    if (!this._hass) return false;
    const switchId = this._entities.switch;
    if (switchId in this._optimistic) return this._optimistic[switchId] === "on";
    const sw = this._hass.states[switchId];
    if (sw) return sw.state === "on";
    const cam = this._hass.states[this._entities.camera];
    if (cam?.attributes?.streaming_state) return cam.attributes.streaming_state === "active";
    return cam?.state === "streaming";
  }
  _triggerFreshSnapshot() {
    this._callService("bosch_shc_camera", "trigger_snapshot", {});
    this._scheduleImageLoad(1500);
    this._scheduleImageLoad(4e3);
  }
  _render() {
    this.shadowRoot.innerHTML = `\n      <style>\n        :host { display: block; font-family: var(--primary-font-family, Roboto, sans-serif); }\n        ha-card {\n          overflow: hidden;\n          border-radius: var(--ha-card-border-radius, 12px);\n          background: var(--ha-card-background, var(--card-background-color, #1c1c1e));\n          box-shadow: var(--ha-card-box-shadow, 0 2px 8px rgba(0,0,0,.3));\n        }\n\n        /* Header */\n        .header {\n          display: flex; align-items: center; justify-content: space-between;\n          padding: 12px 14px 8px;\n        }\n        .header-left { display: flex; align-items: center; gap: 8px; }\n        .title {\n          font-size: 15px; font-weight: 600;\n          color: var(--primary-text-color, #e5e5ea);\n          white-space: nowrap; overflow: hidden; text-overflow: ellipsis;\n        }\n        .status-dot {\n          width: 8px; height: 8px; border-radius: 50%;\n          background: #636366; flex-shrink: 0; transition: background 0.3s;\n        }\n        .status-dot.online  { background: #30d158; }\n        .status-dot.offline { background: #ff453a; }\n\n        /* Stream badge */\n        .stream-badge {\n          display: inline-flex; align-items: center; gap: 5px;\n          font-size: 11px; font-weight: 600; letter-spacing: .4px;\n          text-transform: uppercase; padding: 3px 8px; border-radius: 20px;\n          transition: all 0.3s; white-space: nowrap;\n        }\n        .stream-badge.idle       { background: rgba(99,99,102,.25); color: #8e8e93; }\n        .stream-badge.streaming  { background: rgba(0,122,255,.2); color: #0a84ff; box-shadow: 0 0 0 1px rgba(0,122,255,.3); }\n        .stream-badge.connecting { background: rgba(255,159,10,.2); color: #ff9f0a; box-shadow: 0 0 0 1px rgba(255,159,10,.3); }\n        .stream-badge.offline    { background: rgba(255,69,58,.15); color: #ff453a; }\n        .stream-badge.offline .dot { background: #ff453a; }\n        .stream-badge .dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }\n        .stream-badge.idle .dot       { background: #636366; }\n        .stream-badge.streaming .dot  { background: #0a84ff; animation: pulse 1.5s infinite; }\n        .stream-badge.connecting .dot { background: #ff9f0a; animation: pulse 0.8s infinite; }\n        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }\n\n        /* iOS HLS info banner */\n        .ios-hls-banner {\n          display: none;\n          align-items: center; justify-content: space-between;\n          gap: 8px; padding: 6px 10px;\n          background: rgba(0,122,255,.1); border-top: 1px solid rgba(0,122,255,.2);\n          font-size: 11px; color: #0a84ff;\n        }\n        .ios-hls-banner.visible { display: flex; }\n        .ios-hls-banner span { flex: 1; }\n        .ios-hls-banner button {\n          background: rgba(0,122,255,.15); border: 1px solid rgba(0,122,255,.3);\n          color: #0a84ff; border-radius: 6px; padding: 3px 8px;\n          font-size: 11px; cursor: pointer; white-space: nowrap;\n        }\n        .ios-hls-banner button:active { background: rgba(0,122,255,.3); }\n\n        /* Push status badge */\n        .push-badge {\n          display: inline-flex; align-items: center; gap: 4px;\n          font-size: 10px; font-weight: 600; letter-spacing: .3px;\n          text-transform: uppercase; padding: 2px 6px; border-radius: 12px;\n          white-space: nowrap;\n        }\n        .push-badge.fcm  { background: rgba(48,209,88,.15); color: #30d158; }\n        .push-badge.poll { background: rgba(99,99,102,.2); color: #8e8e93; }\n        .push-badge .pdot { width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0; }\n        .push-badge.fcm .pdot  { background: #30d158; }\n        .push-badge.poll .pdot { background: #636366; }\n\n        /* Connection type badge (LAN / Cloud) */\n        .conn-badge {\n          display: inline-flex; align-items: center; gap: 4px;\n          font-size: 10px; font-weight: 600; letter-spacing: .3px;\n          padding: 2px 7px; border-radius: 12px; white-space: nowrap;\n        }\n        .conn-badge.local  { background: rgba(48,209,88,.15); color: #30d158; }\n        .conn-badge.remote { background: rgba(99,99,102,.2); color: #8e8e93; }\n        .conn-badge.hidden { display: none; }\n\n        /* Camera image area */\n        .img-wrapper { position: relative; width: 100%; background: #000; line-height: 0; aspect-ratio: 16/9; }\n        .cam-img {\n          width: 100%; height: 100%; display: block; object-fit: cover;\n          min-height: 160px; transition: opacity 0.3s;\n        }\n        .cam-img.hidden { opacity: 0; }\n\n        /* Live video element — absolute so it overlays the snapshot image\n           without layout shift. Image stays visible underneath until video\n           fires "playing" event, avoiding the black gap. */\n        .cam-video {\n          position: absolute; inset: 0;\n          width: 100%; height: 100%; display: block; object-fit: cover;\n          min-height: 160px; background: transparent;\n        }\n\n        /* Image rotation 180° (ceiling-mounted indoor cameras).\n           Pure CSS transform — zero CPU, zero latency, GPU-composited.\n           Toggled by the integration's switch.<base>_bild_180_drehen entity.\n           Only the <video> is rotated here: the <img> is loaded from\n           /api/camera_proxy/, which is already rotated server-side by\n           camera.async_camera_image() (PIL) — rotating it again would\n           cancel out and the dashboard snapshot would look upright. */\n        .img-wrapper.rotated-180 .cam-video {\n          transform: rotate(180deg);\n        }\n\n        /* Fullscreen — native API (desktop/Android) */\n        .img-wrapper:fullscreen,\n        .img-wrapper:-webkit-full-screen {\n          background: #000;\n          display: flex; align-items: center; justify-content: center;\n          width: 100vw; height: 100vh;\n        }\n        .img-wrapper:fullscreen .cam-img,\n        .img-wrapper:-webkit-full-screen .cam-img,\n        .img-wrapper:fullscreen .cam-video,\n        .img-wrapper:-webkit-full-screen .cam-video {\n          width: 100vw; height: 100vh;\n          object-fit: contain; min-height: unset;\n        }\n        /* Fullscreen — CSS fallback for iOS Safari (position:fixed overlay) */\n        :host(.fs-active) {\n          position: fixed !important; inset: 0 !important;\n          z-index: 9999 !important; background: #000 !important;\n          display: flex !important; align-items: center !important; justify-content: center !important;\n        }\n        /* Hide header, controls and other elements in fullscreen */\n        :host(.fs-active) .header,\n        :host(.fs-active) .info-row,\n        :host(.fs-active) .btn-row,\n        :host(.fs-active) .switch-rows,\n        :host(.fs-active) .quality-section,\n        :host(.fs-active) .accordion { display: none !important; }\n        :host(.fs-active) .img-wrapper { aspect-ratio: unset; width: 100vw; height: 100vh; }\n        :host(.fs-active) .cam-img,\n        :host(.fs-active) .cam-video { object-fit: contain; min-height: unset; }\n        :host(.fs-active) ha-card { width: 100vw; height: 100vh; border-radius: 0 !important; overflow: hidden; }\n        :host(.fs-active) .cam-img,\n        :host(.fs-active) .cam-video { width: 100vw; height: 100vh; object-fit: contain; min-height: unset; }\n\n        /* Motion zones SVG overlay */\n        .motion-zones-overlay {\n          position: absolute; inset: 0; z-index: 5;\n          width: 100%; height: 100%;\n          pointer-events: none; opacity: 0;\n          transition: opacity 0.3s;\n        }\n        .motion-zones-overlay.visible { opacity: 1; }\n        .motion-zones-overlay rect {\n          fill: rgba(0, 122, 255, 0.15);\n          stroke: rgba(0, 122, 255, 0.6);\n          stroke-width: 0.5;\n        }\n        .motion-zones-overlay rect:nth-child(2) { fill: rgba(52, 199, 89, 0.15); stroke: rgba(52, 199, 89, 0.6); }\n        .motion-zones-overlay rect:nth-child(3) { fill: rgba(255, 159, 10, 0.15); stroke: rgba(255, 159, 10, 0.6); }\n        .motion-zones-overlay rect:nth-child(4) { fill: rgba(255, 69, 58, 0.15); stroke: rgba(255, 69, 58, 0.6); }\n        .motion-zones-overlay rect:nth-child(5) { fill: rgba(175, 82, 222, 0.15); stroke: rgba(175, 82, 222, 0.6); }\n        /* Gen2 polygon zones use per-zone colors from API */\n        .motion-zones-overlay polygon { fill-opacity: 0.15; stroke-width: 2; stroke-opacity: 0.6; }\n        /* Privacy mask SVG overlay */\n        .privacy-mask-overlay {\n          position: absolute; top: 0; left: 0; width: 100%; height: 100%;\n          pointer-events: none; z-index: 5;\n          opacity: 0; transition: opacity 0.3s;\n        }\n        .privacy-mask-overlay.visible { opacity: 1; }\n        .privacy-mask-overlay rect, .privacy-mask-overlay polygon {\n          fill: rgba(0, 0, 0, 0.5); stroke: rgba(0, 0, 0, 0.8); stroke-width: 1.5;\n        }\n\n        /* Loading overlay — must be above both cam-img and cam-video */\n        .loading-overlay {\n          position: absolute; inset: 0; z-index: 10;\n          display: flex; flex-direction: column; align-items: center; justify-content: center;\n          background: rgba(0,0,0,.85);\n          gap: 12px;\n          opacity: 0; transition: opacity 0.3s; pointer-events: none;\n        }\n        .loading-overlay.visible { opacity: 1; pointer-events: auto; }\n        /* Semi-transparent overlay when refreshing an existing image — old image stays visible, spinner on top */\n        .loading-overlay.refreshing { background: rgba(0,0,0,.4); }\n        .spinner {\n          width: 36px; height: 36px;\n          border: 3px solid rgba(255,255,255,.2);\n          border-top-color: #fff;\n          border-radius: 50%;\n          animation: spin 0.8s linear infinite;\n        }\n        @keyframes spin { to { transform: rotate(360deg); } }\n        .loading-text {\n          font-size: 13px; color: rgba(255,255,255,.75); font-weight: 500;\n        }\n        .loading-hint {\n          font-size: 11px; color: rgba(255,255,255,.5); font-weight: 400;\n          margin-top: 4px; display: block; text-align: center; max-width: 220px;\n        }\n        .loading-hint:empty { display: none; }\n\n        /* Offline overlay — shown when status sensor is OFFLINE */\n        .offline-overlay {\n          position: absolute; inset: 0; z-index: 8;\n          display: none;\n          flex-direction: column; align-items: center; justify-content: center;\n          background: rgba(20, 20, 20, 0.82);\n          backdrop-filter: grayscale(100%) blur(3px);\n          -webkit-backdrop-filter: grayscale(100%) blur(3px);\n          gap: 10px;\n          pointer-events: none;\n          animation: offline-pulse 3s ease-in-out infinite;\n        }\n        .offline-overlay.visible { display: flex; }\n        @keyframes offline-pulse {\n          0%, 100% { background: rgba(20, 20, 20, 0.78); }\n          50%      { background: rgba(40, 20, 20, 0.88); }\n        }\n        .offline-overlay svg {\n          width: 48px; height: 48px;\n          stroke: #ff453a; stroke-width: 2; fill: none;\n          filter: drop-shadow(0 0 8px rgba(255, 69, 58, 0.5));\n        }\n        .offline-overlay .offline-title {\n          font-size: 18px; font-weight: 700; color: #ff453a;\n          letter-spacing: 1px; text-transform: uppercase;\n          text-shadow: 0 0 10px rgba(255, 69, 58, 0.4);\n        }\n        .offline-overlay .offline-subtitle {\n          font-size: 12px; color: rgba(255,255,255,.7);\n          font-weight: 400; max-width: 80%; text-align: center; line-height: 1.4;\n        }\n\n        /* Image overlay (last event / events today) */\n        .img-overlay {\n          position: absolute; bottom: 0; left: 0; right: 0;\n          padding: 20px 12px 8px;\n          background: linear-gradient(transparent, rgba(0,0,0,.55));\n          display: flex; align-items: flex-end; justify-content: space-between;\n          pointer-events: none;\n        }\n        .last-event-overlay, .events-overlay { font-size: 11px; color: rgba(255,255,255,.8); }\n\n        /* Info row */\n        .info-row {\n          display: flex; align-items: center; justify-content: space-between;\n          padding: 8px 14px; gap: 10px;\n        }\n        .info-item { display: flex; flex-direction: column; gap: 1px; min-width: 0; }\n        .info-label {\n          font-size: 10px; text-transform: uppercase; letter-spacing: .5px;\n          color: var(--secondary-text-color, #8e8e93);\n        }\n        .info-value {\n          font-size: 13px; color: var(--primary-text-color, #e5e5ea);\n          white-space: nowrap; overflow: hidden; text-overflow: ellipsis;\n        }\n\n        /* Buttons */\n        .btn-row { display: flex; gap: 8px; padding: 8px 12px 12px; }\n        .btn {\n          flex: 1; display: flex; align-items: center; justify-content: center;\n          gap: 6px; padding: 9px 10px; border-radius: 10px; border: none;\n          cursor: pointer; font-size: 13px; font-weight: 500; font-family: inherit;\n          transition: opacity 0.15s, transform 0.1s;\n          -webkit-tap-highlight-color: transparent;\n        }\n        .btn:active { transform: scale(.97); opacity: .8; }\n        .btn:disabled { opacity: .5; cursor: default; }\n        .btn-snapshot { background: rgba(99,99,102,.2); color: var(--primary-text-color, #e5e5ea); }\n        .btn-snapshot.loading { background: rgba(99,99,102,.35); }\n        .btn-stream    { background: rgba(10,132,255,.18); color: #0a84ff; }\n        .btn-stream.active { background: rgba(255,69,58,.18); color: #ff453a; }\n        .btn-fullscreen { background: rgba(99,99,102,.15); color: var(--secondary-text-color, #8e8e93); flex: 0 0 auto; padding: 9px 12px; }\n        .btn-privacy-inline { background: rgba(99,99,102,.15); color: var(--secondary-text-color, #8e8e93); flex: 0 0 auto; padding: 9px 12px; display: none; }\n        .btn-privacy-inline.on { background: rgba(255,69,58,.18); color: #ff453a; }\n        :host(.minimal) .btn-privacy-inline { display: inline-flex; }\n        :host(.minimal) .switch-rows > .privacy-row { display: none; }\n        .btn-overflow { background: rgba(99,99,102,.15); color: var(--secondary-text-color, #8e8e93); flex: 0 0 auto; padding: 9px 12px; display: none; }\n        :host(.minimal) .btn-overflow { display: inline-flex; }\n        :host(.minimal.overflow-open) .btn-overflow { background: rgba(10,132,255,.18); color: #0a84ff; }\n\n        /* Minimal layout: hide everything non-essential until user taps ⋮.\n         * Visible baseline: image, btn-row (Snapshot/Stream/⋮/Vollbild),\n         * Privacy toggle. The overflow-open class (toggled by the ⋮ button) re-\n         * reveals the hidden sections as a single flat panel — no separate popup\n         * needed, just a progressive disclosure of existing controls. */\n        :host(.minimal) .info-row { display: none; }\n        :host(.minimal) .switch-rows { display: none; }\n        :host(.minimal) .btn-row { padding-bottom: 8px; }\n        :host(.minimal) .accordion,\n        :host(.minimal) .pan-row,\n        :host(.minimal) .pan-slider-row,\n        :host(.minimal) .automation-row { display: none; }\n        :host(.minimal.overflow-open) .info-row { display: flex; }\n        :host(.minimal.overflow-open) .switch-rows { display: flex; padding: 0 12px 12px; }\n        :host(.minimal.overflow-open) .switch-rows > .sw-row { display: flex; }\n        :host(.minimal.overflow-open) .accordion,\n        :host(.minimal.overflow-open) .pan-row,\n        :host(.minimal.overflow-open) .pan-slider-row,\n        :host(.minimal.overflow-open) .automation-row { display: block; }\n        :host(.minimal.overflow-open) .pan-row { display: flex; }\n        .btn svg { width: 16px; height: 16px; flex-shrink: 0; }\n        .btn-spinner {\n          width: 14px; height: 14px;\n          border: 2px solid rgba(255,255,255,.3);\n          border-top-color: currentColor;\n          border-radius: 50%;\n          animation: spin 0.8s linear infinite;\n          flex-shrink: 0;\n        }\n\n        /* Switch rows — Ton / Licht / Privat */\n        .switch-rows { display: flex; flex-direction: column; padding: 0 12px 12px; gap: 2px; }\n        .sw-row {\n          display: flex; align-items: center; justify-content: space-between;\n          padding: 9px 4px; cursor: pointer; border-radius: 8px;\n          -webkit-tap-highlight-color: transparent;\n          transition: background 0.15s;\n        }\n        .sw-row:active { background: rgba(99,99,102,.12); }\n        .sw-left {\n          display: flex; align-items: center; gap: 10px;\n          color: var(--primary-text-color, #e5e5ea); font-size: 13px; font-weight: 500;\n        }\n        .sw-left svg { width: 18px; height: 18px; flex-shrink: 0; color: var(--secondary-text-color, #8e8e93); }\n        .sw-row.on .sw-left svg { color: #0a84ff; }\n        .sw-row.privacy-row.on .sw-left svg { color: #ff453a; }\n        /* iOS-style toggle */\n        .sw-toggle {\n          width: 44px; height: 26px; border-radius: 13px;\n          background: rgba(99,99,102,.4); border: none; padding: 0;\n          position: relative; flex-shrink: 0; cursor: pointer;\n          transition: background 0.25s;\n        }\n        .sw-row.on    .sw-toggle { background: #30d158; }\n        .sw-row.privacy-row.on .sw-toggle { background: #ff453a; }\n        .sw-thumb {\n          width: 22px; height: 22px; border-radius: 50%; background: #fff;\n          position: absolute; top: 2px; left: 2px;\n          box-shadow: 0 1px 4px rgba(0,0,0,.4);\n          transition: transform 0.25s cubic-bezier(.4,0,.2,1);\n        }\n        .sw-row.on .sw-thumb { transform: translateX(18px); }\n\n        /* Pending: request in flight — subtle fade while waiting for HA/Bosch confirm */\n        .sw-row.pending,\n        .btn.pending { opacity: 0.7; }\n        .sw-row.pending .sw-toggle,\n        .btn.pending { animation: pendingPulse 1.2s ease-in-out infinite; }\n        @keyframes pendingPulse { 0%,100%{filter:brightness(1)} 50%{filter:brightness(0.75)} }\n        /* Error: 2s red outline + short shake to signal failed service call */\n        .sw-row.error,\n        .btn.error { animation: errorFlash 0.6s ease-in-out 0s 3; box-shadow: 0 0 0 2px rgba(255,69,58,.55); }\n        @keyframes errorFlash {\n          0%,100% { box-shadow: 0 0 0 2px rgba(255,69,58,.55); }\n          50%     { box-shadow: 0 0 0 3px rgba(255,69,58,.15); }\n        }\n\n        /* Privacy placeholder — shown when no image + privacy mode is ON */\n        .privacy-placeholder {\n          position: absolute; inset: 0;\n          display: flex; flex-direction: column; align-items: center; justify-content: center;\n          background: rgba(0,0,0,.82); gap: 10px;\n          opacity: 0; transition: opacity 0.3s; pointer-events: none;\n        }\n        .privacy-placeholder.visible { opacity: 1; }\n        .privacy-placeholder svg { width: 44px; height: 44px; color: rgba(255,255,255,.35); }\n        .privacy-placeholder span { font-size: 13px; color: rgba(255,255,255,.45); font-weight: 500; }\n\n        /* Quality select */\n        .quality-section { padding: 0 12px 12px; }\n        .quality-row { display: flex; align-items: center; gap: 10px; }\n        .quality-label { font-size: 13px; color: var(--secondary-text-color, #8e8e93); flex-shrink: 0; }\n        .quality-select {\n          flex: 1; background: rgba(255,255,255,.1); border: 1px solid rgba(255,255,255,.12);\n          border-radius: 8px; color: var(--primary-text-color, #e5e5ea); font-size: 13px;\n          padding: 6px 10px; cursor: pointer; font-family: inherit;\n          -webkit-appearance: none; appearance: none;\n        }\n        .quality-select:focus { outline: none; background: rgba(255,255,255,.15); }\n        .quality-select option { background: #2c2c2e; color: #e5e5ea; }\n\n        /* Pan controls */\n        .pan-section { padding: 0 12px 12px; }\n        .pan-row { display: flex; align-items: center; gap: 6px; }\n        .pan-btn {\n          background: rgba(128,128,128,.15); border: none; border-radius: 6px;\n          color: var(--primary-text-color, #333); cursor: pointer; padding: 6px 10px; flex: 1;\n          font-family: inherit; -webkit-tap-highlight-color: transparent;\n          transition: background 0.15s;\n          display: flex; align-items: center; justify-content: center;\n        }\n        .pan-btn svg { width: 18px; height: 18px; flex-shrink: 0; }\n        .pan-btn:hover  { background: rgba(128,128,128,.25); }\n        .pan-btn:active { background: rgba(128,128,128,.35); }\n        .pan-pos { margin-left: auto; font-size: 12px; opacity: .7; color: var(--primary-text-color, #e5e5ea); white-space: nowrap; }\n\n        /* Accordion sections */\n        .accordion { border-top: 1px solid rgba(255,255,255,.06); }\n        .accordion-header {\n          display: flex; align-items: center; justify-content: space-between;\n          padding: 10px 14px; cursor: pointer;\n          -webkit-tap-highlight-color: transparent;\n          transition: background 0.15s;\n        }\n        .accordion-header:active { background: rgba(99,99,102,.08); }\n        .accordion-title {\n          font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .5px;\n          color: var(--secondary-text-color, #8e8e93);\n        }\n        .accordion-chevron {\n          width: 16px; height: 16px; color: var(--secondary-text-color, #8e8e93);\n          transition: transform 0.25s ease;\n          flex-shrink: 0;\n        }\n        .accordion.open .accordion-chevron { transform: rotate(180deg); }\n        .accordion-body {\n          max-height: 0; overflow: hidden;\n          transition: max-height 0.3s ease;\n        }\n        .accordion.open .accordion-body { max-height: 600px; }\n        .accordion-content { padding: 0 12px 12px; }\n        .accordion-content .sw-row { padding: 7px 4px; }\n\n        /* Service grid inside accordion */\n        .svc-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; padding: 4px 0; }\n        .svc-btn { display: flex; align-items: center; gap: 6px; padding: 8px 10px; border-radius: 8px; border: 1px solid rgba(255,255,255,.1); background: rgba(255,255,255,.03); color: var(--primary-text-color, #e1e1e1); font-size: 11px; cursor: pointer; transition: background .15s; }\n        .svc-btn:hover { background: rgba(255,255,255,.08); }\n        .svc-btn:active { background: rgba(255,255,255,.12); }\n        .svc-btn svg { width: 16px; height: 16px; flex-shrink: 0; }\n        .svc-btn.running { opacity: 0.5; pointer-events: none; }\n        /* Rule row inside accordion */\n        .rule-row { display: flex; align-items: center; justify-content: space-between; padding: 5px 4px; font-size: 12px; border-bottom: 1px solid rgba(255,255,255,.04); }\n        .rule-row .rule-info { flex: 1; min-width: 0; }\n        .rule-row .rule-name { font-weight: 500; color: var(--primary-text-color, #e1e1e1); }\n        .rule-row .rule-time { color: #999; font-size: 11px; }\n        .rule-row .rule-days { color: #888; font-size: 10px; }\n        .rule-row .rule-toggle { cursor: pointer; padding: 2px 8px; border-radius: 4px; border: 1px solid rgba(255,255,255,.15); background: transparent; color: #999; font-size: 11px; margin-left: 6px; }\n        .rule-row .rule-toggle.active { background: rgba(52,199,89,.15); color: #34c759; border-color: rgba(52,199,89,.3); }\n        .rule-row .rule-delete { cursor: pointer; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(255,59,48,.2); background: transparent; color: #666; font-size: 11px; margin-left: 4px; }\n        .rule-row .rule-delete:hover { background: rgba(255,59,48,.15); color: #ff3b30; }\n        /* Diagnostic row inside accordion */\n        .diag-row {\n          display: flex; align-items: center; justify-content: space-between;\n          padding: 6px 4px;\n        }\n        .diag-label {\n          font-size: 13px; color: var(--secondary-text-color, #8e8e93);\n          display: flex; align-items: center; gap: 8px;\n        }\n        .diag-label svg { width: 16px; height: 16px; flex-shrink: 0; }\n        .diag-value {\n          font-size: 13px; color: var(--primary-text-color, #e5e5ea); font-weight: 500;\n        }\n      </style>\n\n      <ha-card>\n        <div class="header">\n          <div class="header-left">\n            <div class="status-dot unknown" id="status-dot"></div>\n            <span class="title" id="title">Bosch Camera</span>\n          </div>\n          <span id="debug-line" style="font-size:9px;color:#999;opacity:0.5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;text-align:right;padding:0 8px">v${CARD_VERSION}</span>\n          <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">\n            <div class="push-badge poll" id="push-badge">\n              <div class="pdot"></div>\n              <span id="push-label">poll</span>\n            </div>\n            <div class="conn-badge hidden" id="conn-badge"></div>\n            <div class="stream-badge idle" id="stream-badge">\n              <div class="dot"></div>\n              <span id="stream-label">idle</span>\n            </div>\n          </div>\n        </div>\n\n        <div class="img-wrapper" id="img-wrapper">\n          <img class="cam-img hidden" id="cam-img" alt="Camera" style="cursor:pointer" />\n          <video class="cam-video" id="cam-video" autoplay muted playsinline webkit-playsinline preload="auto" disableremoteplayback style="display:none; cursor:pointer"></video>\n          <div class="ios-hls-banner" id="ios-hls-banner">\n            <span>ℹ Externer Zugriff – HLS-Stream aktiv</span>\n            <span style="opacity:0.7">WebRTC über Tunnel nicht möglich</span>\n          </div>\n          <div class="loading-overlay visible" id="loading-overlay">\n            <div class="spinner"></div>\n            <span class="loading-text" id="loading-text">Bild wird geladen…</span>\n            <span class="loading-hint" id="loading-hint"></span>\n          </div>\n          <div class="offline-overlay" id="offline-overlay">\n            <svg viewBox="0 0 24 24">\n              <path d="M1 1l22 22"/>\n              <path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55"/>\n              <path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39"/>\n              <path d="M10.71 5.05A16 16 0 0 1 22.58 9"/>\n              <path d="M1.42 9a15.91 15.91 0 0 1 4.7-2.88"/>\n              <path d="M8.53 16.11a6 6 0 0 1 6.95 0"/>\n              <line x1="12" y1="20" x2="12.01" y2="20"/>\n            </svg>\n            <div class="offline-title">Kamera Offline</div>\n            <div class="offline-subtitle" id="offline-subtitle">Keine Verbindung zur Bosch Cloud</div>\n          </div>\n          <div class="privacy-placeholder" id="privacy-placeholder">\n            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">\n              <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>\n              <path d="M7 11V7a5 5 0 0110 0v4"/>\n            </svg>\n            <span>Privat-Modus aktiv</span>\n          </div>\n          <svg class="motion-zones-overlay" id="motion-zones-overlay" viewBox="0 0 100 100" preserveAspectRatio="none"></svg>\n          <svg class="privacy-mask-overlay" id="privacy-mask-overlay" viewBox="0 0 100 100" preserveAspectRatio="none"></svg>\n          <div class="img-overlay">\n            <span class="last-event-overlay" id="last-event-overlay"></span>\n            <span class="events-overlay" id="events-overlay"></span>\n          </div>\n        </div>\n\n        <div class="info-row">\n          <div class="info-item">\n            <span class="info-label">Status</span>\n            <span class="info-value" id="info-status">—</span>\n          </div>\n          <div class="info-item">\n            <span class="info-label">Verbindung</span>\n            <span class="info-value" id="info-connection">—</span>\n          </div>\n          <div class="info-item" style="text-align:right" title="Bosch-API Reaktionszeit (LOCAL=500 ms, REMOTE=1000 ms). Nicht der Player-Puffer — den stellt 'Puffer-Verhalten' in den Integrations-Einstellungen ein.">\n            <span class="info-label">Reaktion</span>\n            <span class="info-value" id="info-buffering">—</span>\n          </div>\n        </div>\n\n        <div class="btn-row">\n            <button class="btn btn-snapshot" id="btn-snapshot" aria-label="Snapshot aufnehmen">\n              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">\n                <path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/>\n                <circle cx="12" cy="13" r="4"/>\n              </svg>\n              <span id="btn-snapshot-label">Snapshot</span>\n            </button>\n            <button class="btn btn-privacy-inline" id="btn-privacy-inline" title="Privat-Modus" aria-label="Privat-Modus umschalten" aria-pressed="false">\n              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">\n                <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>\n                <path d="M7 11V7a5 5 0 0110 0v4"/>\n              </svg>\n            </button>\n            <button class="btn btn-stream" id="btn-stream" aria-label="Live-Stream starten oder stoppen" aria-pressed="false">\n              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">\n                <polygon points="23 7 16 12 23 17 23 7"/>\n                <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>\n              </svg>\n              <span id="btn-stream-label">Live Stream</span>\n            </button>\n            <button class="btn btn-overflow" id="btn-overflow" title="Weitere Optionen" aria-label="Weitere Optionen" aria-haspopup="true" aria-expanded="false">\n              <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false">\n                <circle cx="12" cy="5" r="2"/>\n                <circle cx="12" cy="12" r="2"/>\n                <circle cx="12" cy="19" r="2"/>\n              </svg>\n            </button>\n            <button class="btn btn-fullscreen" id="btn-fullscreen" title="Vollbild" aria-label="Vollbild-Ansicht">\n              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">\n                <path d="M8 3H5a2 2 0 00-2 2v3m18 0V5a2 2 0 00-2-2h-3m0 18h3a2 2 0 002-2v-3M3 16v3a2 2 0 002 2h3"/>\n              </svg>\n            </button>\n          </div>\n\n          <div class="switch-rows">\n            <div class="sw-row" id="btn-audio">\n              <div class="sw-left">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">\n                  <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>\n                  <path d="M19.07 4.93a10 10 0 010 14.14M15.54 8.46a5 5 0 010 7.07"/>\n                </svg>\n                <span>Ton / Video</span>\n              </div>\n              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n            </div>\n            <div class="sw-row" id="btn-light">\n              <div class="sw-left">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">\n                  <circle cx="12" cy="12" r="5"/>\n                  <line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/>\n                  <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>\n                  <line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/>\n                  <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>\n                </svg>\n                <span>Licht</span>\n              </div>\n              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n            </div>\n            \x3c!-- Light sub-controls: toggles + expandable details --\x3e\n            <div class="light-sub-controls" id="light-sub-controls" style="display:none;padding:0 0 0 28px;border-left:2px solid rgba(255,204,0,.3);margin:0 0 0 16px">\n              <div class="sw-row" id="btn-front-light" style="padding:3px 4px"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10"/></svg><span style="font-size:13px">Frontlicht</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n              <div class="sw-row" id="btn-top-led" style="display:none;padding:3px 4px"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><path d="M12 2v8l6-4M12 2v8l-6-4"/></svg><span style="font-size:13px">Oberes Licht</span></div><div id="top-led-color-mini" style="width:14px;height:14px;border-radius:50%;border:1px solid #666;margin-right:4px"></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n              <div class="sw-row" id="btn-bottom-led" style="display:none;padding:3px 4px"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><path d="M12 22v-8l6 4M12 22v-8l-6 4"/></svg><span style="font-size:13px">Unteres Licht</span></div><div id="bottom-led-color-mini" style="width:14px;height:14px;border-radius:50%;border:1px solid #666;margin-right:4px"></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n              <div class="sw-row" id="btn-wallwasher" style="display:none;padding:3px 4px"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><path d="M9 18h6M10 22h4M12 2v1"/><path d="M18 12a6 6 0 10-12 0c0 2.21 1.34 4.1 3 5h6c1.66-.9 3-2.79 3-5z"/></svg><span style="font-size:13px">Oben + Unten</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n              <div id="light-details-toggle" style="padding:4px;cursor:pointer;display:flex;align-items:center;gap:6px;color:#888;font-size:12px;user-select:none"><svg id="light-details-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width:12px;height:12px;transition:transform .2s"><polyline points="6 9 12 15 18 9"/></svg><span>Helligkeit & Farben</span></div>\n              <div id="light-details-body" style="display:none">\n                <div id="intensity-row" style="display:flex;align-items:center;gap:8px;padding:2px 4px;font-size:12px"><span style="white-space:nowrap;min-width:36px">Front</span><input type="range" id="intensity-slider" min="0" max="100" step="5" style="flex:1;accent-color:#fc0;height:4px"><span id="intensity-value" style="min-width:28px;text-align:right;color:#999">—</span></div>\n                <div id="top-bri-row" style="display:none;align-items:center;gap:8px;padding:2px 4px;font-size:12px"><span style="white-space:nowrap;min-width:36px">Oben</span><input type="range" id="top-bri-slider" min="0" max="100" step="5" style="flex:1;accent-color:#4DFF7D;height:4px"><span id="top-bri-value" style="min-width:28px;text-align:right;color:#999">—</span></div>\n                <div id="bottom-bri-row" style="display:none;align-items:center;gap:8px;padding:2px 4px;font-size:12px"><span style="white-space:nowrap;min-width:36px">Unten</span><input type="range" id="bottom-bri-slider" min="0" max="100" step="5" style="flex:1;accent-color:#FF453A;height:4px"><span id="bottom-bri-value" style="min-width:28px;text-align:right;color:#999">—</span></div>\n                <div id="colortemp-row" style="display:none;align-items:center;gap:8px;padding:2px 4px;font-size:12px"><span style="white-space:nowrap;min-width:36px">Farbt.</span><input type="range" id="colortemp-slider" min="-100" max="100" step="5" style="flex:1;accent-color:#f90;height:4px;background:linear-gradient(to right,#69f,#fff,#f90)"><span id="colortemp-value" style="min-width:28px;text-align:right;color:#999">—</span></div>\n              </div>\n            </div>\n            <div class="sw-row privacy-row" id="btn-privacy">\n              <div class="sw-left">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">\n                  <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>\n                  <path d="M7 11V7a5 5 0 0110 0v4"/>\n                </svg>\n                <span>Privat</span>\n              </div>\n              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n            </div>\n            <div class="sw-row" id="btn-notifications">\n              <div class="sw-left">\n                <svg id="notif-icon-on" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">\n                  <path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9"/>\n                  <path d="M13.73 21a2 2 0 01-3.46 0"/>\n                </svg>\n                <svg id="notif-icon-off" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display:none">\n                  <path d="M13.73 21a2 2 0 01-3.46 0"/>\n                  <path d="M18.63 13A17.89 17.89 0 0118 8"/>\n                  <path d="M6.26 6.26A5.86 5.86 0 006 8c0 7-3 9-3 9h14"/>\n                  <path d="M18 8a6 6 0 00-9.33-5"/>\n                  <line x1="1" y1="1" x2="23" y2="23"/>\n                </svg>\n                <span>Benachrichtigungen</span>\n              </div>\n              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n            </div>\n            <div class="sw-row" id="btn-intercom" style="display:none">\n              <div class="sw-left">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">\n                  <path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/>\n                  <path d="M19 10v2a7 7 0 01-14 0v-2"/>\n                  <line x1="12" y1="19" x2="12" y2="23"/>\n                  <line x1="8" y1="23" x2="16" y2="23"/>\n                </svg>\n                <span>Gegensprech.</span>\n              </div>\n              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n            </div>\n          </div>\n\n          <div class="pan-section" id="pan-section" style="display:none">\n            <div class="pan-row">\n              <button class="pan-btn" id="pan-full-left"  title="Ganz links" aria-label="Kamera ganz nach links schwenken">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true" focusable="false">\n                  <polyline points="11 18 5 12 11 6"/><polyline points="18 18 12 12 18 6"/>\n                </svg>\n              </button>\n              <button class="pan-btn" id="pan-left"       title="Links" aria-label="Kamera nach links schwenken">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true" focusable="false">\n                  <polyline points="15 18 9 12 15 6"/>\n                </svg>\n              </button>\n              <button class="pan-btn" id="pan-center"     title="Mitte" aria-label="Kamera zentrieren">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">\n                  <circle cx="12" cy="12" r="3"/>\n                  <line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/>\n                  <line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/>\n                </svg>\n              </button>\n              <button class="pan-btn" id="pan-right"      title="Rechts" aria-label="Kamera nach rechts schwenken">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true" focusable="false">\n                  <polyline points="9 18 15 12 9 6"/>\n                </svg>\n              </button>\n              <button class="pan-btn" id="pan-full-right" title="Ganz rechts" aria-label="Kamera ganz nach rechts schwenken">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true" focusable="false">\n                  <polyline points="13 18 19 12 13 6"/><polyline points="6 18 12 12 6 6"/>\n                </svg>\n              </button>\n              <span   class="pan-pos" id="pan-position">0°</span>\n            </div>\n          </div>\n\n          <div class="quality-section" id="quality-section" style="display:none">\n            <div class="quality-row">\n              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"\n                   style="width:16px;height:16px;flex-shrink:0;color:var(--secondary-text-color,#8e8e93)">\n                <rect x="2" y="7" width="20" height="15" rx="2"/>\n                <polyline points="17 2 12 7 7 2"/>\n              </svg>\n              <span class="quality-label">Qualität</span>\n              <select class="quality-select" id="quality-select">\n                <option value="Auto">Auto</option>\n                <option value="Hoch (30 Mbps)">Hoch (30 Mbps)</option>\n                <option value="Niedrig (1.9 Mbps)">Niedrig (1.9 Mbps)</option>\n              </select>\n            </div>\n          </div>\n\n          \x3c!-- Accordion: Notification Types --\x3e\n          <div class="accordion" id="acc-notif-types">\n            <div class="accordion-header" id="acc-notif-types-header">\n              <span class="accordion-title">Benachrichtigungs-Typen</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div class="sw-row" id="btn-notif-movement">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>\n                    <span>Bewegung</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-notif-person">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>\n                    <span>Person</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-notif-audio">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 010 14.14"/></svg>\n                    <span>Audio</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-notif-trouble">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>\n                    <span>Störung</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-notif-alarm">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>\n                    <span>Kamera-Alarm</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n              </div>\n            </div>\n          </div>\n\n          \x3c!-- Accordion: Advanced Controls --\x3e\n          <div class="accordion" id="acc-advanced">\n            <div class="accordion-header" id="acc-advanced-header">\n              <span class="accordion-title">Erweitert</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div class="sw-row" id="btn-timestamp">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>\n                    <span>Zeitstempel</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-autofollow">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="8"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/></svg>\n                    <span>Auto-Follow</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-motion">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>\n                    <span>Bewegungserkennung</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-record-sound">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/></svg>\n                    <span>Ton aufnehmen</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-privacy-sound">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 010 7.07"/></svg>\n                    <span>Privat-Ton</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n              </div>\n            </div>\n          </div>\n\n          \x3c!-- Gen2 Accordion: Automatik & Sicherheit --\x3e\n          <div class="accordion" id="acc-gen2-auto" style="display:none">\n            <div class="accordion-header" id="acc-gen2-auto-header">\n              <span class="accordion-title">Automatik & Sicherheit</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div class="sw-row" id="btn-motion-light" style="padding:4px 0"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg><span>Licht bei Bewegung</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n                <div class="sw-row" id="btn-ambient-light" style="padding:4px 0"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/></svg><span>Dauerlicht</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n                <div class="sw-row" id="btn-intrusion" style="padding:4px 0"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg><span>Einbrucherkennung</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n                <div id="motion-sens-row" style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px"><span style="white-space:nowrap">Empfindlichkeit</span><input type="range" id="motion-sens-slider" min="1" max="5" step="1" style="flex:1;accent-color:#ff9500;height:4px"><span id="motion-sens-value" style="min-width:16px;text-align:right;color:#999">—</span></div>\n                \x3c!-- Gen2 Indoor II — Alarm system (75 dB siren) --\x3e\n                <div class="sw-row" id="btn-alarm-arm" style="padding:4px 0;display:none"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg><span>Alarmanlage scharf</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n                <div class="sw-row" id="btn-alarm-mode" style="padding:4px 0;display:none"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="13" r="7"/><path d="M12 9v4l2 2M5 3L2 6M19 3l3 3"/></svg><span>Sirene (75 dB)</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n                <div class="sw-row" id="btn-prealarm" style="padding:4px 0;display:none"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2"/></svg><span>Pre-Alarm (rote LED)</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n                <div class="sw-row" id="btn-audio-alarm" style="padding:4px 0;display:none"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 013 3v8a3 3 0 01-6 0V4a3 3 0 013-3z"/><path d="M19 10v2a7 7 0 01-14 0v-2M12 19v4"/></svg><span>Geräusch-Erkennung</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n                <div id="power-led-row" style="display:none;align-items:center;gap:8px;padding:4px 0;font-size:13px"><span style="white-space:nowrap">Power-LED</span><input type="range" id="power-led-slider" min="0" max="100" step="5" style="flex:1;accent-color:#ff9500;height:4px"><span id="power-led-value" style="min-width:34px;text-align:right;color:#999">—</span></div>\n              </div>\n            </div>\n          </div>\n\n          \x3c!-- Automations Accordion (alle Kameras, konfigurierbar) --\x3e\n          <div class="accordion" id="acc-automations" style="display:none">\n            <div class="accordion-header" id="acc-automations-header">\n              <span class="accordion-title">Automationen</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div id="automations-container"></div>\n              </div>\n            </div>\n          </div>\n\n          \x3c!-- Gen2 Accordion: Licht & Kamera --\x3e\n          <div class="accordion" id="acc-gen2-light" style="display:none">\n            <div class="accordion-header" id="acc-gen2-light-header">\n              <span class="accordion-title">Licht & Kamera</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div id="colortemp-row" style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px"><span style="white-space:nowrap">Farbtemperatur</span><input type="range" id="colortemp-slider" min="-100" max="100" step="5" style="flex:1;accent-color:#f90;height:4px;background:linear-gradient(to right,#69f,#fff,#f90)"><span id="colortemp-value" style="min-width:32px;text-align:right;color:#999">—</span></div>\n                <div id="rgb-lights-row" style="padding:4px 0;font-size:13px">\n                  <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px"><span style="flex:1">Farbe Oben</span><div id="top-led-color" style="width:24px;height:24px;border-radius:50%;border:2px solid #444;cursor:pointer" title="Farbe wählen"></div><input type="color" id="top-led-picker" style="display:none"></div>\n                  <div style="display:flex;align-items:center;gap:10px"><span style="flex:1">Farbe Unten</span><div id="bottom-led-color" style="width:24px;height:24px;border-radius:50%;border:2px solid #444;cursor:pointer" title="Farbe wählen"></div><input type="color" id="bottom-led-picker" style="display:none"></div>\n                </div>\n                <div class="sw-row" id="btn-status-led" style="padding:4px 0"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/></svg><span>Status-LED</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n                <div id="mic-level-row" style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:18px;height:18px;flex-shrink:0"><path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/><path d="M19 10v2a7 7 0 01-14 0v-2"/></svg><span style="white-space:nowrap">Mikrofon</span><input type="range" id="mic-slider" min="0" max="100" step="5" style="flex:1;accent-color:#0a84ff;height:4px"><span id="mic-value" style="min-width:28px;text-align:right;color:#999">—</span></div>\n                <div id="lens-elev-row" style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:18px;height:18px;flex-shrink:0"><path d="M12 22V2M5 12l7-10 7 10"/></svg><span style="white-space:nowrap">Höhe</span><input type="range" id="lens-slider" min="50" max="500" step="5" style="flex:1;accent-color:#30d158;height:4px"><span id="lens-value" style="min-width:36px;text-align:right;color:#999">—</span></div>\n              </div>\n            </div>\n          </div>\n\n          \x3c!-- Accordion: Diagnostics & Services --\x3e\n          <div class="accordion" id="acc-diagnostics">\n            <div class="accordion-header" id="acc-diagnostics-header">\n              <span class="accordion-title">Diagnose</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div class="diag-row" id="diag-wifi">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12.55a11 11 0 0114.08 0"/><path d="M1.42 9a16 16 0 0121.16 0"/><path d="M8.53 16.11a6 6 0 016.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/></svg>\n                    WiFi\n                  </span>\n                  <span class="diag-value" id="diag-wifi-val">—</span>\n                </div>\n                <div class="diag-row" id="diag-firmware">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/></svg>\n                    Firmware\n                  </span>\n                  <span class="diag-value" id="diag-firmware-val">—</span>\n                </div>\n                <div class="diag-row" id="diag-ambient">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/></svg>\n                    Umgebungslicht\n                  </span>\n                  <span class="diag-value" id="diag-ambient-val">—</span>\n                </div>\n                <div class="diag-row" id="diag-movement-today">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>\n                    Bewegung heute\n                  </span>\n                  <span class="diag-value" id="diag-movement-today-val">—</span>\n                </div>\n                <div class="diag-row" id="diag-audio-today">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 010 14.14"/></svg>\n                    Audio heute\n                  </span>\n                  <span class="diag-value" id="diag-audio-today-val">—</span>\n                </div>\n              </div>\n            </div>\n          </div>\n\n          \x3c!-- Accordion: Schedules & Zones --\x3e\n          <div class="accordion" id="acc-schedules">\n            <div class="accordion-header" id="acc-schedules-header">\n              <span class="accordion-title">Zeitpläne & Zonen</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div class="diag-row">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>\n                    Zeitpläne\n                  </span>\n                  <span class="diag-value" id="diag-rules-count">—</span>\n                </div>\n                <div id="rules-list" style="padding:0 4px"></div>\n                <div class="sw-row" id="btn-show-zones">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18"/></svg>\n                    <span>Motion-Zonen anzeigen</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-show-masks">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>\n                    <span>Privacy-Masken anzeigen</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="diag-row">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18"/></svg>\n                    Motion-Zonen\n                  </span>\n                  <span class="diag-value" id="diag-zones-count">—</span>\n                </div>\n                <div class="diag-row">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>\n                    Privacy-Masken\n                  </span>\n                  <span class="diag-value" id="diag-masks-count">—</span>\n                </div>\n              </div>\n            </div>\n          </div>\n\n          \x3c!-- Accordion: Services --\x3e\n          <div class="accordion" id="acc-services">\n            <div class="accordion-header" id="acc-services-header">\n              <span class="accordion-title">Services</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div class="svc-grid" id="svc-grid"></div>\n                <div id="svc-result" style="font-size:11px;color:#999;padding:4px 0;display:none"></div>\n              </div>\n            </div>\n          </div>\n\n      </ha-card>\n    `;
    const img = this.shadowRoot.getElementById("cam-img");
    img.addEventListener("load", () => this._onImageLoaded());
    img.addEventListener("error", () => this._onImageError());
    img.addEventListener("click", () => this._requestFullscreen());
    const vid = this.shadowRoot.getElementById("cam-video");
    vid.addEventListener("click", () => this._requestFullscreen());
    this.shadowRoot.getElementById("btn-snapshot").addEventListener("click", () => this._onSnapshotClick());
    this.shadowRoot.getElementById("btn-stream").addEventListener("click", () => this._toggleStream());
    this.shadowRoot.getElementById("btn-fullscreen").addEventListener("click", () => this._requestFullscreen());
    this.shadowRoot.getElementById("btn-overflow").addEventListener("click", () => {
      this.classList.toggle("overflow-open");
    });
    this.shadowRoot.getElementById("btn-privacy-inline").addEventListener("click", () => this._toggleSwitchWithRollback(this._entities.privacy));
    this.shadowRoot.getElementById("btn-audio").addEventListener("click", () => this._toggleAudio());
    this.shadowRoot.getElementById("btn-light").addEventListener("click", () => this._toggleSwitchWithRollback(this._entities.light));
    this.shadowRoot.getElementById("btn-privacy").addEventListener("click", () => this._toggleSwitchWithRollback(this._entities.privacy));
    this.shadowRoot.getElementById("btn-notifications").addEventListener("click", () => this._toggleSwitch(this._entities.notifications));
    this.shadowRoot.getElementById("btn-intercom")?.addEventListener("click", () => this._toggleSwitch(this._entities.intercom));
    this.shadowRoot.getElementById("btn-front-light")?.addEventListener("click", () => this._toggleSwitch(this._entities.frontLight));
    this.shadowRoot.getElementById("btn-wallwasher")?.addEventListener("click", () => this._toggleSwitch(this._entities.wallwasher));
    const lightDetailsToggle = this.shadowRoot.getElementById("light-details-toggle");
    if (lightDetailsToggle) {
      lightDetailsToggle.addEventListener("click", () => {
        const body = this.shadowRoot.getElementById("light-details-body");
        const chevron = this.shadowRoot.getElementById("light-details-chevron");
        if (body) {
          const open = body.style.display !== "none";
          body.style.display = open ? "none" : "";
          if (chevron) chevron.style.transform = open ? "" : "rotate(180deg)";
        }
      });
    }
    const topBriSlider = this.shadowRoot.getElementById("top-bri-slider");
    if (topBriSlider) {
      topBriSlider.addEventListener("input", () => {
        const v = this.shadowRoot.getElementById("top-bri-value");
        if (v) v.textContent = topBriSlider.value + "%";
      });
      topBriSlider.addEventListener("change", () => {
        if (!this._hass) return;
        const pct = parseInt(topBriSlider.value);
        if (this._entities.topLedLight && this._hass.states[this._entities.topLedLight]) {
          this._hass.callService("light", "turn_on", {
            entity_id: this._entities.topLedLight,
            brightness: Math.max(1, Math.round(pct * 255 / 100))
          }).catch(e => console.warn("bosch-camera-card: top-bri", e));
        } else if (this._entities.topBrightness) {
          this._hass.callService("number", "set_value", {
            entity_id: this._entities.topBrightness,
            value: pct
          }).catch(e => console.warn("bosch-camera-card: top-bri", e));
        }
      });
    }
    const botBriSlider = this.shadowRoot.getElementById("bottom-bri-slider");
    if (botBriSlider) {
      botBriSlider.addEventListener("input", () => {
        const v = this.shadowRoot.getElementById("bottom-bri-value");
        if (v) v.textContent = botBriSlider.value + "%";
      });
      botBriSlider.addEventListener("change", () => {
        if (!this._hass) return;
        const pct = parseInt(botBriSlider.value);
        if (this._entities.bottomLedLight && this._hass.states[this._entities.bottomLedLight]) {
          this._hass.callService("light", "turn_on", {
            entity_id: this._entities.bottomLedLight,
            brightness: Math.max(1, Math.round(pct * 255 / 100))
          }).catch(e => console.warn("bosch-camera-card: bot-bri", e));
        } else if (this._entities.bottomBrightness) {
          this._hass.callService("number", "set_value", {
            entity_id: this._entities.bottomBrightness,
            value: pct
          }).catch(e => console.warn("bosch-camera-card: bot-bri", e));
        }
      });
    }
    this.shadowRoot.getElementById("btn-top-led")?.querySelector(".sw-toggle")?.addEventListener("click", () => {
      if (!this._hass || !this._entities.topLedLight) return;
      const st = this._hass.states[this._entities.topLedLight]?.state;
      this._callService("light", st === "on" ? "turn_off" : "turn_on", {
        entity_id: this._entities.topLedLight
      });
    });
    this.shadowRoot.getElementById("btn-bottom-led")?.querySelector(".sw-toggle")?.addEventListener("click", () => {
      if (!this._hass || !this._entities.bottomLedLight) return;
      const st = this._hass.states[this._entities.bottomLedLight]?.state;
      this._callService("light", st === "on" ? "turn_off" : "turn_on", {
        entity_id: this._entities.bottomLedLight
      });
    });
    const intensitySlider = this.shadowRoot.getElementById("intensity-slider");
    if (intensitySlider) {
      let debounce = null;
      intensitySlider.addEventListener("input", () => {
        const valEl = this.shadowRoot.getElementById("intensity-value");
        if (valEl) valEl.textContent = intensitySlider.value + "%";
      });
      intensitySlider.addEventListener("change", () => {
        if (!this._hass || !this._entities.frontLightIntensity) return;
        clearTimeout(debounce);
        debounce = setTimeout(() => {
          this._hass.callService("number", "set_value", {
            entity_id: this._entities.frontLightIntensity,
            value: parseInt(intensitySlider.value)
          }).catch(err => console.warn("bosch-camera-card: intensity", err));
        }, 200);
      });
    }
    const statusLedBtn = this.shadowRoot.getElementById("btn-status-led");
    if (statusLedBtn) statusLedBtn.querySelector(".sw-toggle")?.addEventListener("click", () => this._toggleSwitch(this._entities.statusLed));
    const intrusionBtn = this.shadowRoot.getElementById("btn-intrusion");
    if (intrusionBtn) intrusionBtn.querySelector(".sw-toggle")?.addEventListener("click", () => this._toggleSwitch(this._entities.intrusionDetection));
    const alarmArmBtn = this.shadowRoot.getElementById("btn-alarm-arm");
    if (alarmArmBtn) alarmArmBtn.querySelector(".sw-toggle")?.addEventListener("click", () => this._toggleSwitch(this._entities.alarmSystemArm));
    const alarmModeBtn = this.shadowRoot.getElementById("btn-alarm-mode");
    if (alarmModeBtn) alarmModeBtn.querySelector(".sw-toggle")?.addEventListener("click", () => this._toggleSwitch(this._entities.alarmMode));
    const preAlarmBtn = this.shadowRoot.getElementById("btn-prealarm");
    if (preAlarmBtn) preAlarmBtn.querySelector(".sw-toggle")?.addEventListener("click", () => this._toggleSwitch(this._entities.preAlarm));
    const audioAlarmBtn = this.shadowRoot.getElementById("btn-audio-alarm");
    if (audioAlarmBtn) audioAlarmBtn.querySelector(".sw-toggle")?.addEventListener("click", () => this._toggleSwitch(this._entities.audioAlarm));
    const powerLedSlider = this.shadowRoot.getElementById("power-led-slider");
    if (powerLedSlider) {
      let powerLedDebounce = null;
      powerLedSlider.addEventListener("input", () => {
        const valEl = this.shadowRoot.getElementById("power-led-value");
        if (valEl) valEl.textContent = powerLedSlider.value + "%";
      });
      powerLedSlider.addEventListener("change", () => {
        if (!this._hass || !this._entities.powerLedBrightness) return;
        clearTimeout(powerLedDebounce);
        powerLedDebounce = setTimeout(() => {
          this._hass.callService("number", "set_value", {
            entity_id: this._entities.powerLedBrightness,
            value: parseInt(powerLedSlider.value)
          }).catch(err => console.warn("bosch-camera-card: power-led", err));
        }, 200);
      });
    }
    const autoContainer = this.shadowRoot.getElementById("automations-container");
    if (autoContainer && this._entities.automations?.length) {
      autoContainer.innerHTML = "";
      this._entities.automations.forEach((eid, i) => {
        const row = document.createElement("div");
        row.className = "sw-row";
        row.id = `btn-auto-${i}`;
        row.style.padding = "4px 0";
        row.innerHTML = `<div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/></svg><span class="auto-label">${eid.split(".").pop().replace(/_/g, " ")}</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>`;
        row.querySelector(".sw-toggle").addEventListener("click", () => {
          if (!this._hass) return;
          const st = this._hass.states[eid]?.state;
          this._callService("automation", st === "on" ? "turn_off" : "turn_on", {
            entity_id: eid
          });
        });
        autoContainer.appendChild(row);
      });
    }
    const motSensSlider = this.shadowRoot.getElementById("motion-sens-slider");
    if (motSensSlider) {
      motSensSlider.addEventListener("input", () => {
        const v = this.shadowRoot.getElementById("motion-sens-value");
        if (v) v.textContent = motSensSlider.value;
      });
      motSensSlider.addEventListener("change", () => {
        if (!this._hass || !this._entities.motionSensitivity) return;
        this._hass.callService("number", "set_value", {
          entity_id: this._entities.motionSensitivity,
          value: parseInt(motSensSlider.value)
        }).catch(err => console.warn("bosch-camera-card: motion-sensitivity", err));
      });
    }
    const motionLightBtn = this.shadowRoot.getElementById("btn-motion-light");
    if (motionLightBtn) motionLightBtn.querySelector(".sw-toggle")?.addEventListener("click", () => this._toggleSwitch(this._entities.motionLight));
    const ambientLightBtn = this.shadowRoot.getElementById("btn-ambient-light");
    if (ambientLightBtn) ambientLightBtn.querySelector(".sw-toggle")?.addEventListener("click", () => this._toggleSwitch(this._entities.ambientLight));
    const topColorCircle = this.shadowRoot.getElementById("top-led-color");
    const topPicker = this.shadowRoot.getElementById("top-led-picker");
    if (topColorCircle && topPicker) {
      topColorCircle.addEventListener("click", () => topPicker.click());
      topPicker.addEventListener("change", () => {
        if (!this._hass || !this._entities.topLedLight) return;
        const hex = topPicker.value;
        const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
        this._hass.callService("light", "turn_on", {
          entity_id: this._entities.topLedLight,
          rgb_color: [ r, g, b ],
          brightness: 200
        }).catch(e => console.warn("bosch-camera-card: top-led-color", e));
        topColorCircle.style.background = hex;
      });
    }
    const botColorCircle = this.shadowRoot.getElementById("bottom-led-color");
    const botPicker = this.shadowRoot.getElementById("bottom-led-picker");
    if (botColorCircle && botPicker) {
      botColorCircle.addEventListener("click", () => botPicker.click());
      botPicker.addEventListener("change", () => {
        if (!this._hass || !this._entities.bottomLedLight) return;
        const hex = botPicker.value;
        const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
        this._hass.callService("light", "turn_on", {
          entity_id: this._entities.bottomLedLight,
          rgb_color: [ r, g, b ],
          brightness: 200
        }).catch(e => console.warn("bosch-camera-card: bottom-led-color", e));
        botColorCircle.style.background = hex;
      });
    }
    const ctSlider = this.shadowRoot.getElementById("colortemp-slider");
    if (ctSlider) {
      let ctDebounce = null;
      ctSlider.addEventListener("input", () => {
        const v = this.shadowRoot.getElementById("colortemp-value");
        const val = parseInt(ctSlider.value);
        if (v) v.textContent = val === 0 ? "neutral" : val < 0 ? "kalt" : "warm";
      });
      ctSlider.addEventListener("change", () => {
        if (!this._hass || !this._entities.colorTemp) return;
        clearTimeout(ctDebounce);
        ctDebounce = setTimeout(() => {
          this._hass.callService("number", "set_value", {
            entity_id: this._entities.colorTemp,
            value: parseFloat((parseInt(ctSlider.value) / 100).toFixed(2))
          }).catch(err => console.warn("bosch-camera-card: colortemp", err));
        }, 200);
      });
    }
    const micSlider = this.shadowRoot.getElementById("mic-slider");
    if (micSlider) {
      let micDebounce = null;
      micSlider.addEventListener("input", () => {
        const v = this.shadowRoot.getElementById("mic-value");
        if (v) v.textContent = micSlider.value + "%";
      });
      micSlider.addEventListener("change", () => {
        if (!this._hass || !this._entities.micLevel) return;
        clearTimeout(micDebounce);
        micDebounce = setTimeout(() => {
          this._hass.callService("number", "set_value", {
            entity_id: this._entities.micLevel,
            value: parseInt(micSlider.value)
          }).catch(err => console.warn("bosch-camera-card: mic-level", err));
        }, 200);
      });
    }
    const lensSlider = this.shadowRoot.getElementById("lens-slider");
    if (lensSlider) {
      let lensDebounce = null;
      lensSlider.addEventListener("input", () => {
        const v = this.shadowRoot.getElementById("lens-value");
        if (v) v.textContent = (parseInt(lensSlider.value) / 100).toFixed(2) + " m";
      });
      lensSlider.addEventListener("change", () => {
        if (!this._hass || !this._entities.lensElevation) return;
        clearTimeout(lensDebounce);
        lensDebounce = setTimeout(() => {
          this._hass.callService("number", "set_value", {
            entity_id: this._entities.lensElevation,
            value: parseFloat((parseInt(lensSlider.value) / 100).toFixed(2))
          }).catch(err => console.warn("bosch-camera-card: lens-elevation", err));
        }, 200);
      });
    }
    const PAN_STEP = 30;
    const setPan = pos => {
      if (!this._hass || !this._entities.pan) return;
      this._hass.callService("number", "set_value", {
        entity_id: this._entities.pan,
        value: Math.max(-120, Math.min(120, pos))
      }).then(() => {
        this._callService("bosch_shc_camera", "trigger_snapshot", {});
        this._scheduleImageLoad(2e3);
      }).catch(err => console.warn("bosch-camera-card: pan set_value", err));
    };
    const getCurPan = () => parseFloat(this._hass?.states[this._entities.pan]?.state || 0);
    this.shadowRoot.getElementById("pan-full-left")?.addEventListener("click", () => setPan(-120));
    this.shadowRoot.getElementById("pan-left")?.addEventListener("click", () => setPan(getCurPan() - PAN_STEP));
    this.shadowRoot.getElementById("pan-center")?.addEventListener("click", () => setPan(0));
    this.shadowRoot.getElementById("pan-right")?.addEventListener("click", () => setPan(getCurPan() + PAN_STEP));
    this.shadowRoot.getElementById("pan-full-right")?.addEventListener("click", () => setPan(120));
    const qualitySel = this.shadowRoot.getElementById("quality-select");
    if (qualitySel) {
      qualitySel.addEventListener("change", () => this._onQualityChange(qualitySel.value));
    }
    [ "acc-notif-types", "acc-advanced", "acc-diagnostics", "acc-schedules", "acc-services", "acc-gen2-auto", "acc-gen2-light", "acc-automations" ].forEach(id => {
      this.shadowRoot.getElementById(`${id}-header`)?.addEventListener("click", () => {
        const acc = this.shadowRoot.getElementById(id);
        if (acc) acc.classList.toggle("open");
      });
    });
    this.shadowRoot.getElementById("btn-notif-movement")?.addEventListener("click", () => this._toggleSwitch(this._entities.notifMovement));
    this.shadowRoot.getElementById("btn-notif-person")?.addEventListener("click", () => this._toggleSwitch(this._entities.notifPerson));
    this.shadowRoot.getElementById("btn-notif-audio")?.addEventListener("click", () => this._toggleSwitch(this._entities.notifAudio));
    this.shadowRoot.getElementById("btn-notif-trouble")?.addEventListener("click", () => this._toggleSwitch(this._entities.notifTrouble));
    this.shadowRoot.getElementById("btn-notif-alarm")?.addEventListener("click", () => this._toggleSwitch(this._entities.notifAlarm));
    this._renderServiceButtons();
    this.shadowRoot.getElementById("btn-show-zones")?.addEventListener("click", () => {
      this._showMotionZones = !this._showMotionZones;
      const btn = this.shadowRoot.getElementById("btn-show-zones");
      if (btn) btn.classList.toggle("on", this._showMotionZones);
      this._lastMotionCoordKey = null;
      if (this._hass) this._updateMotionZones(this._hass, this._entities);
    });
    this.shadowRoot.getElementById("btn-show-masks")?.addEventListener("click", () => {
      this._showPrivacyMasks = !this._showPrivacyMasks;
      const btn = this.shadowRoot.getElementById("btn-show-masks");
      if (btn) btn.classList.toggle("on", this._showPrivacyMasks);
      this._lastPrivacyMaskKey = null;
      if (this._hass) this._updatePrivacyMasks(this._hass, this._entities);
    });
    this.shadowRoot.getElementById("btn-timestamp")?.addEventListener("click", () => this._toggleSwitch(this._entities.timestamp));
    this.shadowRoot.getElementById("btn-autofollow")?.addEventListener("click", () => this._toggleSwitch(this._entities.autofollow));
    this.shadowRoot.getElementById("btn-motion")?.addEventListener("click", () => this._toggleSwitch(this._entities.motion));
    this.shadowRoot.getElementById("btn-record-sound")?.addEventListener("click", () => this._toggleSwitch(this._entities.recordSound));
    this.shadowRoot.getElementById("btn-privacy-sound")?.addEventListener("click", () => this._toggleSwitch(this._entities.privacySound));
    this._imgTimestamp = Date.now();
    this._scheduleImageLoad(0);
  }
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
    const dispW = Math.round(this.offsetWidth || 640);
    const url = `/api/camera_proxy/${camEntity}?token=${token}&time=${this._imgTimestamp}&width=${dispW}`;
    if (this._imageLoaded) {
      const preload = new window.Image;
      preload.onload = () => {
        img.src = url;
      };
      preload.onerror = () => {
        this._setLoadingOverlay(false);
      };
      preload.src = url;
    } else {
      img.src = url;
    }
  }
  _onImageLoaded() {
    const img = this.shadowRoot.getElementById("cam-img");
    const src = img?.src || "";
    const isCache = src.startsWith("data:");
    this._imageLoaded = true;
    this._loadRetries = 0;
    if (img) img.classList.remove("hidden");
    if (!isCache && this._streamConnecting) {
      this._streamConnecting = false;
      if (this._connectSteps) {
        this._connectSteps.forEach(t => clearTimeout(t));
        this._connectSteps = null;
      }
    }
    if (isCache && this._awaitingFresh) {
      const overlay = this.shadowRoot.getElementById("loading-overlay");
      if (overlay) {
        overlay.classList.add("visible");
        overlay.classList.add("refreshing");
      }
    } else {
      this._awaitingFresh = false;
      this._setLoadingOverlay(false);
    }
    const dbg = this.shadowRoot.getElementById("debug-line");
    if (dbg) {
      const now = (new Date).toLocaleTimeString("de-DE");
      const w = img?.naturalWidth || "?", h = img?.naturalHeight || "?";
      const nowMs = Date.now();
      const dt = !isCache && this._lastFrameTime ? ` Δ${((nowMs - this._lastFrameTime) / 1e3).toFixed(1)}s` : "";
      if (!isCache) this._lastFrameTime = nowMs;
      dbg.textContent = `Card v${CARD_VERSION} | ${isCache ? "cache" : "fresh"} ${now}${dt} | ${w}×${h}`;
    }
    if (!isCache && !this._isStreaming()) this._cacheImage(src);
  }
  _onImageError() {
    if (!this._imageLoaded) {
      const MAX_RETRIES = 5;
      if (this._loadRetries < MAX_RETRIES) {
        this._loadRetries++;
        setTimeout(() => {
          this._imgTimestamp = Date.now();
          this._updateImage();
        }, 3e3);
      } else {
        this._setLoadingOverlay(false);
      }
      return;
    }
    this._setLoadingOverlay(false);
  }
  _setLoadingOverlay(visible, text = "Bild wird geladen…") {
    const streamStarting = this._streamConnecting || this._waitingForStream || this._startingLiveVideo;
    if (!visible && streamStarting) return;
    if (visible && streamStarting && this._streamConnecting && text === "Bild wird geladen…") return;
    const overlay = this.shadowRoot.getElementById("loading-overlay");
    const loadText = this.shadowRoot.getElementById("loading-text");
    const hintEl = this.shadowRoot.getElementById("loading-hint");
    const img = this.shadowRoot.getElementById("cam-img");
    this._loadingOverlay = visible;
    if (overlay) {
      overlay.classList.toggle("visible", visible);
      overlay.classList.toggle("refreshing", visible && this._imageLoaded);
    }
    if (loadText) loadText.textContent = text;
    if (hintEl) {
      if (visible && (this._streamConnecting || this._startingLiveVideo || this._waitingForStream)) {
        const ct = this._hass?.states?.[this._entities?.switch]?.attributes?.connection_type;
        if (ct === "REMOTE") hintEl.textContent = "Cloud-Stream — ca. 30–45 s bis erstes Bild, danach stabil"; else if (ct === "LOCAL") hintEl.textContent = "LAN-Stream — ca. 25–35 s bis erstes Bild"; else hintEl.textContent = "Verbindung zur Kamera wird aufgebaut…";
      } else {
        hintEl.textContent = "";
      }
    }
    if (img) img.classList.toggle("hidden", visible && !this._imageLoaded);
    if (visible) {
      if (this._loadingTimeout) clearTimeout(this._loadingTimeout);
      const isStreamStart = this._startingLiveVideo || this._waitingForStream || this._liveVideoActive;
      const safetyMs = isStreamStart ? 12e4 : 15e3;
      this._loadingTimeout = setTimeout(() => this._setLoadingOverlay(false), safetyMs);
    } else {
      if (this._loadingTimeout) {
        clearTimeout(this._loadingTimeout);
        this._loadingTimeout = null;
      }
    }
  }
  _restoreCachedImage() {
    if (!this._storageKey) return;
    try {
      const cached = localStorage.getItem(this._storageKey);
      if (!cached) return;
      const img = this.shadowRoot.getElementById("cam-img");
      if (img) {
        img.src = cached;
        img.classList.remove("hidden");
      }
      this._imageLoaded = true;
      this._awaitingFresh = true;
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
    if (!this._storageKey || !proxyUrl) return;
    fetch(proxyUrl).then(r => r.ok ? r.blob() : Promise.reject(r.status)).then(blob => new Promise((resolve, reject) => {
      const reader = new FileReader;
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    })).then(dataUrl => {
      try {
        localStorage.setItem(this._storageKey, dataUrl);
      } catch (_) {}
    }).catch(() => {});
  }
  _loadHlsJs() {
    if (window.Hls) return Promise.resolve(window.Hls);
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "https://cdn.jsdelivr.net/npm/hls.js@1.6.16/dist/hls.min.js";
      s.integrity = "sha384-5E8B0pTlZZJMabWpC0fyYf6OUpe15jJij34BqBAh4NXoHAlLNOjCPRrwtOXOQFAn";
      s.crossOrigin = "anonymous";
      s.onload = () => resolve(window.Hls);
      s.onerror = () => reject(new Error("hls.js load failed"));
      document.head.appendChild(s);
    });
  }
  async _startLiveVideo(attempt = 1) {
    if (!this._hass) return;
    const video = this.shadowRoot.getElementById("cam-video");
    const img = this.shadowRoot.getElementById("cam-img");
    if (!video) return;
    this._stopRefreshTimer();
    this._startingLiveVideo = true;
    const audioOn = this._getEffectiveState(this._entities.audio) === "on";
    const activateVideo = () => {
      video.style.display = "block";
      this._liveVideoActive = true;
      this._startingLiveVideo = false;
      if (this._extCompanion) {
        const banner = this.shadowRoot?.getElementById("ios-hls-banner");
        if (banner) banner.classList.add("visible");
      }
      const clearOverlay = () => {
        if (img) img.style.display = "none";
        this._setLoadingOverlay(false);
        if (this._streamConnecting) {
          this._streamConnecting = false;
          if (this._connectSteps) {
            this._connectSteps.forEach(t => clearTimeout(t));
            this._connectSteps = null;
          }
        }
        video.removeEventListener("playing", clearOverlay);
      };
      video.addEventListener("playing", clearOverlay);
      if (this._activateSafetyTimer) clearTimeout(this._activateSafetyTimer);
      this._activateSafetyTimer = setTimeout(() => {
        if (!video.paused && video.currentTime > 0) {
          clearOverlay();
        } else {
          this._setLoadingOverlay(false);
        }
      }, 12e4);
      if (this._stallChecker) clearInterval(this._stallChecker);
      let lastTime = 0;
      let stallCount = 0;
      this._stallChecker = setInterval(() => {
        if (!this._liveVideoActive || !video) {
          clearInterval(this._stallChecker);
          return;
        }
        if (video.currentTime === lastTime && !video.paused) {
          stallCount++;
          if (stallCount >= 3) {
            console.warn("bosch-camera-card: video stalled for 15s, recovering");
            stallCount = 0;
            if (this._hls && this._hls.liveSyncPosition) {
              video.currentTime = this._hls.liveSyncPosition;
            } else {
              this._stopLiveVideo();
              if (this._isStreaming && this._isStreaming()) {
                setTimeout(() => this._startLiveVideo(), 2e3);
              }
            }
          }
        } else {
          stallCount = 0;
        }
        lastTime = video.currentTime;
      }, 5e3);
    };
    const _skipWebRTC = this._extCompanion;
    if (_skipWebRTC) {
      console.debug("bosch-camera-card: Companion App + external endpoint — skipping WebRTC, using HLS");
    }
    if (!_skipWebRTC) try {
      try {
        await this._startWebRTC(video, activateVideo);
        return;
      } catch (webrtcErr) {
        const m = String(webrtcErr?.message || webrtcErr);
        const expectedRace = m.includes("does not support WebRTC") || m.includes("frontend_stream_types");
        if (expectedRace) {
          console.debug("bosch-camera-card: WebRTC race miss, falling back to HLS:", m);
        } else {
          console.warn("bosch-camera-card: WebRTC failed, falling back to HLS:", m);
        }
        if (this._webrtcPc) {
          try {
            this._webrtcPc.close();
          } catch {}
          this._webrtcPc = null;
        }
        if (this._webrtcUnsub) {
          try {
            this._webrtcUnsub();
          } catch {}
          this._webrtcUnsub = null;
        }
      }
    } catch (outer) {}
    try {
      const result = await this._hass.callWS({
        type: "camera/stream",
        entity_id: this._entities.camera
      });
      if (!result?.url) throw new Error("no url");
      video.muted = true;
      const startPlay = () => {
        video.muted = true;
        video.play().then(() => {}).catch(err => {
          console.warn("bosch-camera-card: muted play failed:", err.message);
          setTimeout(() => {
            video.muted = true;
            video.play().catch(() => {});
          }, 2e3);
        });
      };
      let Hls = null;
      try {
        Hls = await this._loadHlsJs();
      } catch (e) {
        console.warn("bosch-camera-card: hls.js load failed, will try native HLS:", e?.message);
      }
      if (Hls && Hls.isSupported()) {
        if (this._hls) {
          this._hls.destroy();
          this._hls = null;
        }
        const camAttrsForBuf = this._hass?.states?.[this._entities.camera]?.attributes || {};
        const bufModeKey = camAttrsForBuf.live_buffer_mode || "balanced";
        const bufProfile = BOSCH_BUFFER_PROFILES[bufModeKey] || BOSCH_BUFFER_PROFILES.balanced;
        console.debug("bosch-camera-card: HLS buffer profile", bufModeKey, bufProfile);
        const hls = new Hls({
          enableWorker: true,
          ...bufProfile,
          manifestLoadingMaxRetry: 10,
          levelLoadingMaxRetry: 10,
          fragLoadingMaxRetry: 10
        });
        this._hls = hls;
        hls.on(Hls.Events.MANIFEST_PARSED, startPlay);
        this._stallCount = 0;
        hls.on(Hls.Events.FRAG_LOADED, () => {
          this._stallCount = 0;
        });
        hls.on(Hls.Events.ERROR, (_ev, data) => {
          if (data.details === "bufferStalledError") {
            this._stallCount = (this._stallCount || 0) + 1;
            if (video && hls.liveSyncPosition) {
              video.currentTime = hls.liveSyncPosition;
            }
            if (this._stallCount >= 3) {
              console.warn("bosch-camera-card: 3 buffer stalls, reconnecting HLS");
              this._stallCount = 0;
              this._stopLiveVideo();
              if (this._isStreaming && this._isStreaming()) {
                setTimeout(() => this._reconnectAfterStreamDrop(), 1e3);
              }
            }
            return;
          }
          if (!data.fatal) return;
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
            hls.startLoad();
          } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
            hls.recoverMediaError();
          } else {
            console.warn("bosch-camera-card: hls.js fatal error, reconnecting", data);
            this._stopLiveVideo();
            if (this._isStreaming()) {
              setTimeout(() => this._reconnectAfterStreamDrop(), 2e3);
            }
          }
        });
        hls.loadSource(result.url);
        hls.attachMedia(video);
        if (this._hlsKeepaliveTimer) clearInterval(this._hlsKeepaliveTimer);
        this._hlsKeepaliveTimer = setInterval(() => {
          if (this._hls && this._liveVideoActive) {
            this._hls.startLoad(-1);
          }
        }, 2e4);
      } else if (video.canPlayType("application/vnd.apple.mpegurl") !== "") {
        video.src = result.url;
        startPlay();
      } else {
        throw new Error("HLS not supported");
      }
      activateVideo();
    } catch (e) {
      if (attempt < 5) {
        setTimeout(() => {
          const cam = this._hass?.states[this._entities.camera];
          if (cam?.state === "streaming") {
            this._startLiveVideo(attempt + 1);
          } else if (this._isStreaming() && !this._waitingForStream) {
            this._waitingForStream = true;
            this._setLoadingOverlay(true, "Verbindung wird neu aufgebaut…");
            this._waitForStreamReady();
          }
        }, 1500);
      } else {
        console.warn("bosch-camera-card: stream not available (attempt " + attempt + "), retrying in 10s", e);
        this._liveVideoActive = false;
        this._startingLiveVideo = false;
        this._startRefreshTimer();
        setTimeout(() => {
          if (this._isStreaming && this._isStreaming() && !this._liveVideoActive && !this._startingLiveVideo) {
            this._waitingForStream = true;
            this._setLoadingOverlay(true, "Stream wird erneut versucht…");
            this._waitForStreamReady();
          }
        }, 1e4);
      }
    }
  }
  async _startWebRTC(video, activateVideo) {
    const entityId = this._entities.camera;
    let rtcConfig = {
      iceServers: [ {
        urls: "stun:stun.home-assistant.io:80"
      } ]
    };
    try {
      const settings = await this._hass.callWS({
        type: "camera/webrtc/get_client_config",
        entity_id: entityId
      });
      if (settings?.configuration) rtcConfig = settings.configuration;
    } catch (e) {
      console.debug("bosch-camera-card: get_client_config unavailable, using default STUN:", e?.message);
    }
    const pc = new RTCPeerConnection(rtcConfig);
    this._webrtcPc = pc;
    pc.addTransceiver("video", {
      direction: "recvonly"
    });
    pc.addTransceiver("audio", {
      direction: "recvonly"
    });
    const remoteStream = new MediaStream;
    pc.ontrack = ev => {
      remoteStream.addTrack(ev.track);
      if (video.srcObject !== remoteStream) {
        video.srcObject = remoteStream;
        video.muted = true;
        video.play().catch(() => {});
        activateVideo();
      }
    };
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const unsub = await this._hass.connection.subscribeMessage(event => {
      if (event.type === "answer") {
        pc.setRemoteDescription({
          type: "answer",
          sdp: event.answer
        });
      } else if (event.type === "candidate") {
        pc.addIceCandidate(event.candidate);
      } else if (event.type === "error") {
        console.warn("bosch-camera-card: WebRTC error:", event.message);
      }
    }, {
      type: "camera/webrtc/offer",
      entity_id: entityId,
      offer: offer.sdp
    });
    this._webrtcUnsub = unsub;
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("WebRTC: no track within 5s")), 5e3);
      pc.addEventListener("iceconnectionstatechange", () => {
        if (pc.iceConnectionState === "failed" || pc.iceConnectionState === "disconnected") {
          clearTimeout(timeout);
          reject(new Error("WebRTC: ICE " + pc.iceConnectionState));
        }
      });
      pc.ontrack = ev => {
        clearTimeout(timeout);
        remoteStream.addTrack(ev.track);
        if (video.srcObject !== remoteStream) {
          video.srcObject = remoteStream;
          video.muted = true;
          video.play().catch(() => {});
          activateVideo();
        }
        resolve();
      };
    });
  }
  _reconnectAfterStreamDrop() {
    if (!this._isStreaming()) return;
    const cam = this._hass?.states[this._entities.camera];
    if (cam?.state === "streaming") {
      this._startLiveVideo();
    } else if (!this._waitingForStream) {
      this._waitingForStream = true;
      this._setLoadingOverlay(true, "Verbindung wird neu aufgebaut…");
      this._waitForStreamReady();
    }
  }
  _stopLiveVideo() {
    if (this._hls) {
      this._hls.destroy();
      this._hls = null;
    }
    if (this._stallChecker) {
      clearInterval(this._stallChecker);
      this._stallChecker = null;
    }
    if (this._hlsKeepaliveTimer) {
      clearInterval(this._hlsKeepaliveTimer);
      this._hlsKeepaliveTimer = null;
    }
    if (this._activateSafetyTimer) {
      clearTimeout(this._activateSafetyTimer);
      this._activateSafetyTimer = null;
    }
    if (this._webrtcPc) {
      this._webrtcPc.close();
      this._webrtcPc = null;
    }
    if (this._webrtcUnsub) {
      this._webrtcUnsub();
      this._webrtcUnsub = null;
    }
    const video = this.shadowRoot.getElementById("cam-video");
    const img = this.shadowRoot.getElementById("cam-img");
    if (video) {
      video.pause();
      video.srcObject = null;
      video.removeAttribute("src");
      video.load();
      video.style.display = "none";
    }
    if (img) img.style.display = "block";
    this._liveVideoActive = false;
    this._startingLiveVideo = false;
    this._streamConnecting = false;
    if (this._connectSteps) {
      this._connectSteps.forEach(t => clearTimeout(t));
      this._connectSteps = null;
    }
  }
  _onSnapshotClick() {
    const btn = this.shadowRoot.getElementById("btn-snapshot");
    const label = this.shadowRoot.getElementById("btn-snapshot-label");
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
    const privStates = this._hass?.states;
    const privacyOn = privStates && this._entities.privacy in privStates && privStates[this._entities.privacy]?.state === "on";
    if (privacyOn) {
      if (label) label.textContent = "Snapshot";
      if (btn) {
        btn.disabled = false;
        btn.classList.remove("loading");
        const sp = btn.querySelector("#snapshot-spinner");
        if (sp) sp.remove();
      }
      this._setLoadingOverlay(false);
      return;
    }
    const token = this._hass?.states[this._entities.camera]?.attributes?.access_token || "";
    const dispW = Math.round(this.offsetWidth || 640);
    const currUrl = `/api/camera_proxy/${this._entities.camera}?token=${token}&t=${Date.now()}&width=${dispW}`;
    const startPoll = prevBytes => {
      this._callService("bosch_shc_camera", "trigger_snapshot", {});
      const startTime = Date.now();
      this._snapshotPollTimer = setTimeout(() => this._pollSnapshotImage(prevBytes, startTime), 500);
    };
    fetch(currUrl).then(r => r.ok ? r.blob() : null).then(blob => startPoll(blob ? blob.size : 0)).catch(() => startPoll(0));
  }
  _pollSnapshotImage(prevBytes, startTime) {
    const TIMEOUT = 6e3;
    const INTERVAL = 1e3;
    const elapsed = Date.now() - startTime;
    if (!this._hass) {
      this._finishSnapshot();
      return;
    }
    const token = this._hass.states[this._entities.camera]?.attributes?.access_token || "";
    const dispW2 = Math.round(this.offsetWidth || 640);
    const url = `/api/camera_proxy/${this._entities.camera}?token=${token}&t=${Date.now()}&width=${dispW2}`;
    fetch(url).then(r => r.ok ? r.blob() : Promise.reject(r.status)).then(blob => {
      const changed = prevBytes === 0 || Math.abs(blob.size - prevBytes) > 200;
      if (changed || elapsed >= TIMEOUT) {
        this._showSnapshotBlob(blob);
      } else {
        this._snapshotPollTimer = setTimeout(() => this._pollSnapshotImage(prevBytes, startTime), INTERVAL);
      }
    }).catch(() => {
      if (elapsed < TIMEOUT) {
        this._snapshotPollTimer = setTimeout(() => this._pollSnapshotImage(prevBytes, startTime), INTERVAL);
      } else {
        this._finishSnapshot();
      }
    });
  }
  _showSnapshotBlob(blob) {
    if (!blob || blob.size < 500) {
      this._finishSnapshot();
      return;
    }
    const reader = new FileReader;
    reader.onload = e => {
      const dataUrl = e.target.result;
      const img = this.shadowRoot.getElementById("cam-img");
      if (img) {
        img.src = dataUrl;
        img.classList.remove("hidden");
        this._imageLoaded = true;
      }
      this._setLoadingOverlay(false);
      try {
        if (this._storageKey) localStorage.setItem(this._storageKey, dataUrl);
      } catch (_) {}
      this._finishSnapshot();
    };
    reader.onerror = () => this._finishSnapshot();
    reader.readAsDataURL(blob);
  }
  _finishSnapshot() {
    if (this._snapshotPollTimer) {
      clearTimeout(this._snapshotPollTimer);
      this._snapshotPollTimer = null;
    }
    const btn = this.shadowRoot.getElementById("btn-snapshot");
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
  _update() {
    if (!this._hass || !this._config) return;
    const hass = this._hass;
    const ents = this._entities;
    if (this._extCompanion) {
      const banner = this.shadowRoot?.getElementById("ios-hls-banner");
      if (banner) banner.classList.toggle("visible", !!this._liveVideoActive);
    }
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
    const titleEl = this.shadowRoot.getElementById("title");
    if (titleEl) {
      titleEl.textContent = this._config.title || hass.states[ents.camera]?.attributes?.friendly_name || ents.camera;
    }
    const pushState = hass.states[ents.push_status];
    const pushBadge = this.shadowRoot.getElementById("push-badge");
    const pushLabel = this.shadowRoot.getElementById("push-label");
    if (pushBadge && pushLabel) {
      const isFcm = pushState?.state === "fcm_push";
      const mode = pushState?.attributes?.fcm_push_mode || "";
      pushBadge.className = "push-badge " + (isFcm ? "fcm" : "poll");
      pushLabel.textContent = isFcm ? `fcm${mode ? " " + mode : ""}` : "poll";
    }
    const statusState = hass.states[ents.status]?.state || "UNKNOWN";
    const statusDot = this.shadowRoot.getElementById("status-dot");
    const infoStatus = this.shadowRoot.getElementById("info-status");
    if (statusDot) statusDot.className = "status-dot " + ({
      ONLINE: "online",
      OFFLINE: "offline"
    }[statusState] || "unknown");
    if (infoStatus) infoStatus.textContent = statusState;
    const camAttrs = hass.states[ents.camera]?.attributes || {};
    const camConnType = camAttrs.connection_type || "";
    const bufMs = camAttrs.buffering_time_ms;
    const infoConn = this.shadowRoot.getElementById("info-connection");
    const infoBuf = this.shadowRoot.getElementById("info-buffering");
    if (infoConn) {
      infoConn.textContent = camConnType === "LOCAL" ? "LAN" : camConnType === "REMOTE" ? "Cloud" : "—";
    }
    if (infoBuf) {
      infoBuf.textContent = typeof bufMs === "number" && bufMs > 0 ? `${bufMs} ms` : "—";
    }
    const offlineOverlay = this.shadowRoot.getElementById("offline-overlay");
    const isOffline = statusState === "OFFLINE";
    if (offlineOverlay) {
      offlineOverlay.classList.toggle("visible", isOffline);
      if (isOffline) {
        const lastChanged = hass.states[ents.status]?.last_changed;
        const sub = this.shadowRoot.getElementById("offline-subtitle");
        if (sub && lastChanged) {
          try {
            const d = new Date(lastChanged);
            sub.textContent = `Zuletzt gesehen: ${d.toLocaleString("de-DE", {
              day: "2-digit",
              month: "2-digit",
              hour: "2-digit",
              minute: "2-digit"
            })}`;
          } catch {}
        }
      }
    }
    this._isOffline = isOffline;
    const isStreaming = this._isStreaming();
    const badge = this.shadowRoot.getElementById("stream-badge");
    const streamLabel = this.shadowRoot.getElementById("stream-label");
    const btnStream = this.shadowRoot.getElementById("btn-stream");
    const btnStreamLbl = this.shadowRoot.getElementById("btn-stream-label");
    const streamBadgeState = isOffline ? "offline" : this._startingLiveVideo ? "connecting" : isStreaming ? "streaming" : "idle";
    if (badge) badge.className = "stream-badge " + streamBadgeState;
    if (streamLabel && !isStreaming) streamLabel.textContent = streamBadgeState;
    if (btnStream) {
      const streamOpt = this._optimistic[ents.switch];
      const streamPending = streamOpt === "pending";
      btnStream.className = "btn btn-stream" + (isStreaming ? " active" : "") + (streamPending ? " pending" : "");
      this._entityToBtnId[ents.switch] = "btn-stream";
    }
    if (btnStreamLbl) btnStreamLbl.textContent = isStreaming ? "Stop Stream" : "Live Stream";
    const connType = hass.states[ents.switch]?.attributes?.connection_type || "";
    const connBadge = this.shadowRoot.getElementById("conn-badge");
    if (connBadge) {
      if (isStreaming && connType) {
        connBadge.className = "conn-badge " + (connType === "LOCAL" ? "local" : "remote");
        connBadge.textContent = connType === "LOCAL" ? "LAN" : "Cloud";
      } else {
        connBadge.className = "conn-badge hidden";
      }
    }
    if (isStreaming && !this._lastStreaming) {
      this._streamStartTime = Date.now();
      if (this._uptimeTimer) clearInterval(this._uptimeTimer);
      this._uptimeTimer = setInterval(() => {
        if (!this._streamStartTime) return;
        const s = Math.floor((Date.now() - this._streamStartTime) / 1e3);
        const mm = String(Math.floor(s / 60)).padStart(2, "0");
        const ss = String(s % 60).padStart(2, "0");
        const label = this.shadowRoot?.getElementById("stream-label");
        if (label) label.textContent = `${mm}:${ss}`;
      }, 1e3);
    }
    if (!isStreaming) {
      this._streamStartTime = 0;
      if (this._uptimeTimer) {
        clearInterval(this._uptimeTimer);
        this._uptimeTimer = null;
      }
    }
    const isAudioOn = this._getEffectiveState(ents.audio) === "on";
    const shouldVideo = isStreaming;
    if (!isStreaming && this._lastStreaming !== null && this._lastStreaming !== isStreaming) {
      this._stopLiveVideo();
      this._setLoadingOverlay(true, "Aktualisiere Bild…");
      this._callService("bosch_shc_camera", "trigger_snapshot", {});
      this._scheduleImageLoad(3500);
      this._startRefreshTimer();
    }
    this._lastStreaming = isStreaming;
    const backendStreamStatus = hass.states[ents.streamStatus]?.state || camAttrs.stream_status || "";
    const backendWaiting = backendStreamStatus === "warming_up" || backendStreamStatus === "connecting";
    if ((shouldVideo || backendWaiting) && !this._liveVideoActive && !this._startingLiveVideo && !this._waitingForStream) {
      this._waitingForStream = true;
      const overlayText = backendStreamStatus === "warming_up" ? "Kamera wird aufgeweckt…" : backendStreamStatus === "connecting" ? "Verbindung wird aufgebaut…" : "Stream wird gestartet…";
      if (this._config.snapshot_during_warmup && !this._imageLoaded && !this._awaitingFresh) {
        this._triggerFreshSnapshot();
      }
      this._setLoadingOverlay(true, overlayText);
      this._waitForStreamReady();
    }
    if (!shouldVideo && !backendWaiting) {
      this._waitingForStream = false;
    }
    if (!shouldVideo && this._liveVideoActive) {
      this._stopLiveVideo();
    }
    if (!this._liveVideoActive && !this._startingLiveVideo && !isStreaming) {
      if (this._timerStreaming !== false) {
        this._timerStreaming = false;
        this._startRefreshTimer();
      }
    }
    const lastEventState = hass.states[ents.last_event];
    const lastEventOverlay = this.shadowRoot.getElementById("last-event-overlay");
    const curEventVal = lastEventState?.state;
    if (curEventVal && curEventVal !== "unavailable" && curEventVal !== "unknown" && this._lastEventState !== null && curEventVal !== this._lastEventState && !this._liveVideoActive) {
      this._scheduleImageLoad(1500);
    }
    this._lastEventState = curEventVal || this._lastEventState;
    let lastEventStr = "—";
    if (lastEventState?.state && lastEventState.state !== "unavailable") {
      try {
        const d = new Date(lastEventState.state);
        lastEventStr = isNaN(d) ? lastEventState.state : this._formatDatetime(d);
      } catch (_) {
        lastEventStr = lastEventState.state;
      }
    }
    if (lastEventStr === "—") {
      const a = hass.states[ents.camera]?.attributes?.last_event;
      if (a) lastEventStr = a.slice(0, 16).replace("T", " ");
    }
    if (lastEventOverlay) lastEventOverlay.textContent = lastEventStr !== "—" ? `Letztes: ${lastEventStr}` : "";
    const evTodayState = hass.states[ents.events_today];
    const evOverlay = this.shadowRoot.getElementById("events-overlay");
    const evCount = evTodayState?.state ?? "—";
    if (evOverlay) evOverlay.textContent = evCount !== "—" ? `${evCount} Events heute` : "";
    this._updateToggleBtn("btn-audio", ents.audio, hass.states[ents.audio]);
    this._updateToggleBtn("btn-light", ents.light, hass.states[ents.light]);
    this._updateToggleBtn("btn-privacy", ents.privacy, hass.states[ents.privacy]);
    const privInline = this.shadowRoot.getElementById("btn-privacy-inline");
    if (privInline) {
      const ps = hass.states[ents.privacy]?.state;
      const optVal = this._optimistic[ents.privacy];
      const isPending = optVal === "pending";
      const ds = ents.privacy in this._optimistic && !isPending ? optVal : ps;
      privInline.classList.toggle("on", ds === "on");
    }
    this._updateToggleBtn("btn-notifications", ents.notifications, hass.states[ents.notifications]);
    this._updateToggleBtn("btn-intercom", ents.intercom, hass.states[ents.intercom]);
    const lightSubControls = this.shadowRoot.getElementById("light-sub-controls");
    if (lightSubControls) {
      const hasFront = ents.frontLight && hass.states[ents.frontLight];
      const hasWall = ents.wallwasher && hass.states[ents.wallwasher];
      const hasIntensity = ents.frontLightIntensity && hass.states[ents.frontLightIntensity];
      lightSubControls.style.display = hasFront || hasWall || hasIntensity ? "" : "none";
      this._updateToggleBtn("btn-front-light", ents.frontLight, hass.states[ents.frontLight]);
      this._updateToggleBtn("btn-wallwasher", ents.wallwasher, hass.states[ents.wallwasher]);
      const intensityRow = this.shadowRoot.getElementById("intensity-row");
      const intensitySlider = this.shadowRoot.getElementById("intensity-slider");
      const intensityValue = this.shadowRoot.getElementById("intensity-value");
      if (intensityRow) intensityRow.style.display = hasIntensity ? "flex" : "none";
      if (hasIntensity && intensitySlider && intensityValue) {
        const v = parseFloat(hass.states[ents.frontLightIntensity]?.state) || 0;
        if (!intensitySlider.matches(":active")) {
          intensitySlider.value = v;
          intensityValue.textContent = Math.round(v) + "%";
        }
      }
    }
    const hasGen2 = ents.statusLed && hass.states[ents.statusLed];
    const hasAutomations = ents.automations?.length > 0;
    const accAuto = this.shadowRoot.getElementById("acc-gen2-auto");
    const accLight = this.shadowRoot.getElementById("acc-gen2-light");
    const accAutomations = this.shadowRoot.getElementById("acc-automations");
    if (accAuto) accAuto.style.display = hasGen2 ? "" : "none";
    if (accLight) accLight.style.display = hasGen2 ? "" : "none";
    if (accAutomations) accAutomations.style.display = hasAutomations ? "" : "none";
    this._updateToggleBtn("btn-status-led", ents.statusLed, hass.states[ents.statusLed]);
    this._updateToggleBtn("btn-motion-light", ents.motionLight, hass.states[ents.motionLight]);
    this._updateToggleBtn("btn-ambient-light", ents.ambientLight, hass.states[ents.ambientLight]);
    this._updateToggleBtn("btn-intrusion", ents.intrusionDetection, hass.states[ents.intrusionDetection]);
    const hasAlarmSystem = ents.alarmSystemArm && hass.states[ents.alarmSystemArm];
    for (const [rowId, entId] of [ [ "btn-alarm-arm", ents.alarmSystemArm ], [ "btn-alarm-mode", ents.alarmMode ], [ "btn-prealarm", ents.preAlarm ], [ "btn-audio-alarm", ents.audioAlarm ] ]) {
      const row = this.shadowRoot.getElementById(rowId);
      if (row) row.style.display = hasAlarmSystem && entId && hass.states[entId] ? "flex" : "none";
    }
    this._updateToggleBtn("btn-alarm-arm", ents.alarmSystemArm, hass.states[ents.alarmSystemArm]);
    this._updateToggleBtn("btn-alarm-mode", ents.alarmMode, hass.states[ents.alarmMode]);
    this._updateToggleBtn("btn-prealarm", ents.preAlarm, hass.states[ents.preAlarm]);
    this._updateToggleBtn("btn-audio-alarm", ents.audioAlarm, hass.states[ents.audioAlarm]);
    const powerLedRow = this.shadowRoot.getElementById("power-led-row");
    const powerLedEnt = hass.states[ents.powerLedBrightness];
    if (powerLedRow) powerLedRow.style.display = powerLedEnt ? "flex" : "none";
    if (powerLedEnt) {
      const slider = this.shadowRoot.getElementById("power-led-slider");
      const valEl = this.shadowRoot.getElementById("power-led-value");
      const val = parseInt(powerLedEnt.state) || 0;
      if (slider && document.activeElement !== slider) slider.value = val;
      if (valEl) valEl.textContent = val + "%";
    }
    if (ents.automations?.length) {
      ents.automations.forEach((eid, i) => {
        const btn = this.shadowRoot.getElementById(`btn-auto-${i}`);
        if (!btn) return;
        const state = hass.states[eid];
        if (!state) {
          btn.style.display = "none";
          return;
        }
        btn.style.display = "";
        btn.classList.toggle("on", state.state === "on");
        const label = btn.querySelector(".auto-label");
        if (label) label.textContent = state.attributes?.friendly_name || eid.split(".").pop().replace(/_/g, " ");
      });
    }
    const motSensRow = this.shadowRoot.getElementById("motion-sens-row");
    const motSensEl = this.shadowRoot.getElementById("motion-sens-slider");
    const motSensVal = this.shadowRoot.getElementById("motion-sens-value");
    const hasMotSens = ents.motionSensitivity && hass.states[ents.motionSensitivity] && hass.states[ents.motionSensitivity].state !== "unavailable";
    if (motSensRow) motSensRow.style.display = hasMotSens ? "flex" : "none";
    if (hasMotSens && motSensEl && motSensVal && !motSensEl.matches(":active")) {
      const sv = parseFloat(hass.states[ents.motionSensitivity]?.state) || 3;
      motSensEl.value = Math.round(sv);
      motSensVal.textContent = Math.round(sv);
    }
    const pickBriPct = (lightEnt, numberEnt) => {
      const lightSt = lightEnt ? hass.states[lightEnt] : null;
      if (lightSt && lightSt.state === "off") {
        const lbp = lightSt.attributes?.last_brightness_pct;
        if (typeof lbp === "number") return lbp;
      }
      return parseFloat(hass.states[numberEnt]?.state) || 0;
    };
    const topBriRow = this.shadowRoot.getElementById("top-bri-row");
    const topBriEl = this.shadowRoot.getElementById("top-bri-slider");
    const topBriVal = this.shadowRoot.getElementById("top-bri-value");
    const hasTopBri = ents.topBrightness && hass.states[ents.topBrightness] && hass.states[ents.topBrightness].state !== "unavailable" && hass.states[ents.topBrightness].state !== "unknown";
    if (topBriRow) topBriRow.style.display = hasTopBri ? "flex" : "none";
    if (hasTopBri && topBriEl && topBriVal && !topBriEl.matches(":active")) {
      const v = pickBriPct(ents.topLedLight, ents.topBrightness);
      topBriEl.value = Math.round(v);
      topBriVal.textContent = Math.round(v) + "%";
    }
    const botBriRow = this.shadowRoot.getElementById("bottom-bri-row");
    const botBriEl = this.shadowRoot.getElementById("bottom-bri-slider");
    const botBriVal = this.shadowRoot.getElementById("bottom-bri-value");
    const hasBotBri = ents.bottomBrightness && hass.states[ents.bottomBrightness] && hass.states[ents.bottomBrightness].state !== "unavailable" && hass.states[ents.bottomBrightness].state !== "unknown";
    if (botBriRow) botBriRow.style.display = hasBotBri ? "flex" : "none";
    if (hasBotBri && botBriEl && botBriVal && !botBriEl.matches(":active")) {
      const v = pickBriPct(ents.bottomLedLight, ents.bottomBrightness);
      botBriEl.value = Math.round(v);
      botBriVal.textContent = Math.round(v) + "%";
    }
    const hasTopLed = ents.topLedLight && hass.states[ents.topLedLight];
    const hasBotLed = ents.bottomLedLight && hass.states[ents.bottomLedLight];
    const topLedBtn = this.shadowRoot.getElementById("btn-top-led");
    const botLedBtn = this.shadowRoot.getElementById("btn-bottom-led");
    const pickColor = (entId, fallback) => {
      const attrs = hass.states[entId]?.attributes;
      if (!attrs) return fallback;
      const rgb = attrs.rgb_color;
      if (rgb && Array.isArray(rgb) && rgb.length === 3) return `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
      const lrc = attrs.last_rgb_color;
      if (lrc && Array.isArray(lrc) && lrc.length === 3) return `rgb(${lrc[0]},${lrc[1]},${lrc[2]})`;
      return fallback;
    };
    if (topLedBtn) {
      topLedBtn.style.display = hasTopLed ? "" : "none";
      if (hasTopLed) {
        const isOn = hass.states[ents.topLedLight]?.state === "on";
        topLedBtn.classList.toggle("on", isOn);
        const color = pickColor(ents.topLedLight, this._lastTopColor || "rgb(255,180,100)");
        this._lastTopColor = color;
        const dot = this.shadowRoot.getElementById("top-led-color-mini");
        if (dot) dot.style.background = color;
      }
    }
    if (botLedBtn) {
      botLedBtn.style.display = hasBotLed ? "" : "none";
      if (hasBotLed) {
        const isOn = hass.states[ents.bottomLedLight]?.state === "on";
        botLedBtn.classList.toggle("on", isOn);
        const color = pickColor(ents.bottomLedLight, this._lastBotColor || "rgb(255,180,100)");
        this._lastBotColor = color;
        const dot = this.shadowRoot.getElementById("bottom-led-color-mini");
        if (dot) dot.style.background = color;
      }
    }
    const rgbRow = this.shadowRoot.getElementById("rgb-lights-row");
    if (rgbRow) rgbRow.style.display = hasTopLed || hasBotLed ? "" : "none";
    const topCircle = this.shadowRoot.getElementById("top-led-color");
    if (topCircle && hasTopLed) {
      const color = pickColor(ents.topLedLight, this._lastTopColor || "rgb(255,180,100)");
      this._lastTopColor = color;
      topCircle.style.background = color;
    }
    const botCircle = this.shadowRoot.getElementById("bottom-led-color");
    if (botCircle && hasBotLed) {
      const color = pickColor(ents.bottomLedLight, this._lastBotColor || "rgb(255,180,100)");
      this._lastBotColor = color;
      botCircle.style.background = color;
    }
    const ctRow = this.shadowRoot.getElementById("colortemp-row");
    const ctSliderEl = this.shadowRoot.getElementById("colortemp-slider");
    const ctValue = this.shadowRoot.getElementById("colortemp-value");
    const hasColorTemp = ents.colorTemp && hass.states[ents.colorTemp] && hass.states[ents.colorTemp].state !== "unavailable" && hass.states[ents.colorTemp].state !== "unknown";
    if (ctRow) ctRow.style.display = hasColorTemp ? "flex" : "none";
    if (hasColorTemp && ctSliderEl && ctValue && !ctSliderEl.matches(":active")) {
      const wb = parseFloat(hass.states[ents.colorTemp]?.state) || 0;
      ctSliderEl.value = Math.round(wb * 100);
      ctValue.textContent = wb === 0 ? "neutral" : wb < 0 ? "kalt" : "warm";
    }
    const micRow = this.shadowRoot.getElementById("mic-level-row");
    const micSliderEl = this.shadowRoot.getElementById("mic-slider");
    const micValue = this.shadowRoot.getElementById("mic-value");
    const hasMic = ents.micLevel && hass.states[ents.micLevel] && hass.states[ents.micLevel].state !== "unavailable";
    if (micRow) micRow.style.display = hasMic ? "flex" : "none";
    if (hasMic && micSliderEl && micValue && !micSliderEl.matches(":active")) {
      const ml = parseFloat(hass.states[ents.micLevel]?.state) || 0;
      micSliderEl.value = Math.round(ml);
      micValue.textContent = Math.round(ml) + "%";
    }
    const lensRow = this.shadowRoot.getElementById("lens-elev-row");
    const lensSliderEl = this.shadowRoot.getElementById("lens-slider");
    const lensValue = this.shadowRoot.getElementById("lens-value");
    const hasLens = ents.lensElevation && hass.states[ents.lensElevation] && hass.states[ents.lensElevation].state !== "unavailable";
    if (lensRow) lensRow.style.display = hasLens ? "flex" : "none";
    if (hasLens && lensSliderEl && lensValue && !lensSliderEl.matches(":active")) {
      const el = parseFloat(hass.states[ents.lensElevation]?.state) || 2;
      lensSliderEl.value = Math.round(el * 100);
      lensValue.textContent = el.toFixed(2) + " m";
    }
    this._updateToggleBtn("btn-notif-movement", ents.notifMovement, hass.states[ents.notifMovement]);
    this._updateToggleBtn("btn-notif-person", ents.notifPerson, hass.states[ents.notifPerson]);
    this._updateToggleBtn("btn-notif-audio", ents.notifAudio, hass.states[ents.notifAudio]);
    this._updateToggleBtn("btn-notif-trouble", ents.notifTrouble, hass.states[ents.notifTrouble]);
    this._updateToggleBtn("btn-notif-alarm", ents.notifAlarm, hass.states[ents.notifAlarm]);
    this._updateToggleBtn("btn-timestamp", ents.timestamp, hass.states[ents.timestamp]);
    this._updateToggleBtn("btn-autofollow", ents.autofollow, hass.states[ents.autofollow]);
    this._updateToggleBtn("btn-motion", ents.motion, hass.states[ents.motion]);
    this._updateToggleBtn("btn-record-sound", ents.recordSound, hass.states[ents.recordSound]);
    this._updateToggleBtn("btn-privacy-sound", ents.privacySound, hass.states[ents.privacySound]);
    const wifiVal = hass.states[ents.wifi];
    const fwVal = hass.states[ents.firmware];
    const ambVal = hass.states[ents.ambient];
    const movVal = hass.states[ents.movementToday];
    const audVal = hass.states[ents.audioToday];
    const _dv = (id, st) => {
      const el = this.shadowRoot.getElementById(id);
      if (el) el.textContent = st?.state && st.state !== "unavailable" && st.state !== "unknown" ? st.state : "—";
    };
    _dv("diag-wifi-val", wifiVal);
    _dv("diag-firmware-val", fwVal);
    _dv("diag-ambient-val", ambVal);
    _dv("diag-movement-today-val", movVal);
    _dv("diag-audio-today-val", audVal);
    if (wifiVal?.state && wifiVal.state !== "unavailable") {
      const el = this.shadowRoot.getElementById("diag-wifi-val");
      if (el) el.textContent = wifiVal.state + " %";
    }
    if (ambVal?.state && ambVal.state !== "unavailable") {
      const el = this.shadowRoot.getElementById("diag-ambient-val");
      if (el) el.textContent = ambVal.state + " %";
    }
    this._updateSchedulesSection(hass, ents);
    const _hideAccIf = (accId, entityIds) => {
      const acc = this.shadowRoot.getElementById(accId);
      if (!acc) return;
      const anyExists = entityIds.some(eid => {
        const st = hass.states[eid];
        return st && st.state && st.state !== "unavailable" && st.state !== "unknown";
      });
      acc.style.display = anyExists ? "" : "none";
    };
    _hideAccIf("acc-notif-types", [ ents.notifMovement, ents.notifPerson, ents.notifAudio, ents.notifTrouble, ents.notifAlarm ]);
    _hideAccIf("acc-advanced", [ ents.timestamp, ents.autofollow, ents.motion, ents.recordSound, ents.privacySound ]);
    _hideAccIf("acc-diagnostics", [ ents.wifi, ents.firmware, ents.ambient, ents.movementToday, ents.audioToday ]);
    _hideAccIf("acc-schedules", [ ents.scheduleRules, ents.motionZones ]);
    const notifState = this._getEffectiveState(ents.notifications);
    const notifIconOn = this.shadowRoot.getElementById("notif-icon-on");
    const notifIconOff = this.shadowRoot.getElementById("notif-icon-off");
    if (notifIconOn && notifIconOff) {
      notifIconOn.style.display = notifState === "off" ? "none" : "";
      notifIconOff.style.display = notifState === "off" ? "" : "none";
    }
    if (this._liveVideoActive) {
      const video = this.shadowRoot.getElementById("cam-video");
      const audioOn = this._getEffectiveState(ents.audio) === "on";
      if (video) {
        if (!audioOn) {
          video.muted = true;
        } else if (!video.paused) {
          video.muted = false;
        }
      }
    }
    const privacyOptimistic = this._optimistic[ents.privacy];
    const privacyOn = privacyOptimistic !== undefined ? privacyOptimistic === "on" : ents.privacy in hass.states && hass.states[ents.privacy]?.state === "on";
    const placeholder = this.shadowRoot.getElementById("privacy-placeholder");
    if (placeholder) placeholder.classList.toggle("visible", privacyOn);
    if (privacyOn) this._setLoadingOverlay(false);
    if (this._lastPrivacy === true && !privacyOn) {
      this._scheduleImageLoad(6e3);
      this._scheduleImageLoad(9e3);
    }
    this._lastPrivacy = privacyOn;
    this._updateMotionZones(hass, ents);
    this._updatePrivacyMasks(hass, ents);
    const panState = hass.states[ents.pan];
    const panSection = this.shadowRoot.getElementById("pan-section");
    if (panSection) {
      const hasPan = panState && panState.state && panState.state !== "unavailable" && panState.state !== "unknown";
      panSection.style.display = hasPan ? "" : "none";
      if (hasPan) {
        const posEl = this.shadowRoot.getElementById("pan-position");
        if (posEl) posEl.textContent = `${panState.state}°`;
      }
    }
    const qualitySection = this.shadowRoot.getElementById("quality-section");
    const qualitySel = this.shadowRoot.getElementById("quality-select");
    if (qualitySection && qualitySel) {
      const qualityEntityId = ents.quality;
      const qualityState = qualityEntityId ? hass.states[qualityEntityId] : null;
      const hasQuality = qualityState && qualityState.state && qualityState.state !== "unavailable" && qualityState.state !== "unknown";
      qualitySection.style.display = hasQuality ? "" : "none";
      if (hasQuality && qualitySel.value !== qualityState.state) {
        qualitySel.value = qualityState.state;
      }
    }
    if (this._isOffline) {
      for (const sel of [ ".info-row", ".btn-row", ".switch-rows" ]) {
        const el = this.shadowRoot.querySelector(sel);
        if (el) el.style.display = "none";
      }
      for (const acc of this.shadowRoot.querySelectorAll(".accordion")) {
        acc.style.display = "none";
      }
      const panSec = this.shadowRoot.getElementById("pan-section");
      if (panSec) panSec.style.display = "none";
      const qualSec = this.shadowRoot.getElementById("quality-section");
      if (qualSec) qualSec.style.display = "none";
      const dbgLine = this.shadowRoot.getElementById("debug-line");
      if (dbgLine) dbgLine.style.display = "none";
    }
  }
  async _discoverAutomationsViaWs(hass) {
    try {
      const reg = await hass.callWS({
        type: "config/entity_registry/get",
        entity_id: this._entities.camera
      });
      const deviceId = reg?.device_id;
      if (!deviceId) return;
      const result = await hass.callWS({
        type: "search/related",
        item_type: "device",
        item_id: deviceId
      });
      const autoIds = (result.automation || []).filter(eid => hass.states[eid]).sort();
      if (autoIds.length) {
        this._entities.automations = autoIds;
        this._rebuildAutomationRows();
      }
    } catch (e) {
      const prefix = `automation.${this._base}_`;
      const fallback = Object.keys(hass.states).filter(eid => eid.startsWith(prefix)).sort();
      if (fallback.length) {
        this._entities.automations = fallback;
        this._rebuildAutomationRows();
      }
    }
  }
  _rebuildAutomationRows() {
    const autoContainer = this.shadowRoot?.getElementById("automations-container");
    const accAutomations = this.shadowRoot?.getElementById("acc-automations");
    if (!autoContainer) return;
    const autos = this._entities.automations || [];
    autoContainer.innerHTML = "";
    if (!autos.length) {
      if (accAutomations) accAutomations.style.display = "none";
      return;
    }
    if (accAutomations) accAutomations.style.display = "";
    autoContainer.innerHTML = "";
    autos.forEach((eid, i) => {
      const row = document.createElement("div");
      row.className = "sw-row";
      row.id = `btn-auto-${i}`;
      row.style.padding = "4px 0";
      row.style.cursor = "pointer";
      row.innerHTML = `<div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/></svg><span class="auto-label">${eid.split(".").pop().replace(/_/g, " ")}</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>`;
      row.addEventListener("click", () => {
        if (!this._hass) return;
        const st = this._hass.states[eid]?.state;
        this._callService("automation", st === "on" ? "turn_off" : "turn_on", {
          entity_id: eid
        });
      });
      const state = this._hass?.states[eid];
      if (state) {
        row.classList.toggle("on", state.state === "on");
        const label = row.querySelector(".auto-label");
        if (label) label.textContent = state.attributes?.friendly_name || eid.split(".").pop().replace(/_/g, " ");
      }
      autoContainer.appendChild(row);
    });
  }
  _updateToggleBtn(id, entityId, entityState) {
    const btn = this.shadowRoot.getElementById(id);
    if (!btn) return;
    if (entityId) this._entityToBtnId[entityId] = id;
    const state = entityState?.state;
    if (!entityState || !state || state === "unavailable" || state === "unknown") {
      btn.style.display = "none";
      return;
    }
    btn.style.display = "";
    const optVal = this._optimistic[entityId];
    const isPending = optVal === "pending";
    const displayState = entityId in this._optimistic && !isPending ? optVal : state;
    btn.classList.toggle("on", displayState === "on");
    btn.classList.toggle("pending", isPending);
    btn.classList.remove("unavailable");
    btn.disabled = false;
  }
  _updateSchedulesSection(hass, ents) {
    const WEEKDAY_NAMES = [ "So", "Mo", "Di", "Mi", "Do", "Fr", "Sa" ];
    const rulesState = hass.states[ents.scheduleRules];
    const rulesCountEl = this.shadowRoot.getElementById("diag-rules-count");
    if (rulesCountEl) {
      rulesCountEl.textContent = rulesState?.state != null && rulesState.state !== "unavailable" ? rulesState.state : "—";
    }
    const rulesListEl = this.shadowRoot.getElementById("rules-list");
    if (rulesListEl && rulesState) {
      const rules = rulesState.attributes?.rules || [];
      const camId = hass.states[ents.status]?.attributes?.camera_id || "";
      if (rules.length === 0) {
        rulesListEl.innerHTML = '<div style="font-size:11px;color:#666;padding:4px 0">Keine Zeitpläne</div>';
      } else {
        const rulesKey = JSON.stringify(rules);
        if (this._lastRulesKey !== rulesKey) {
          this._lastRulesKey = rulesKey;
          rulesListEl.innerHTML = rules.map((r, i) => {
            const days = (r.weekdays || []).map(d => WEEKDAY_NAMES[d] || d).join(", ");
            const isActive = r.active ?? r.isActive ?? false;
            const startT = r.start || r.startTime || "?";
            const endT = r.end || r.endTime || "?";
            const activeClass = isActive ? " active" : "";
            const activeLabel = isActive ? "AN" : "AUS";
            return `<div class="rule-row" data-rule-idx="${i}">\n              <div class="rule-info">\n                <div class="rule-name">${this._escHtml(r.name || "Regel " + (i + 1))}</div>\n                <div class="rule-time">${startT} – ${endT}</div>\n                <div class="rule-days">${days}</div>\n              </div>\n              <button class="rule-toggle${activeClass}" data-rule-id="${r.id}" data-cam-id="${camId}" data-active="${isActive ? "true" : "false"}">${activeLabel}</button>\n              <button class="rule-delete" data-rule-id="${r.id}" data-cam-id="${camId}" title="Löschen">✕</button>\n            </div>`;
          }).join("");
          rulesListEl.querySelectorAll(".rule-toggle").forEach(btn => {
            btn.addEventListener("click", e => {
              e.stopPropagation();
              const ruleId = btn.dataset.ruleId;
              const cId = btn.dataset.camId;
              const newActive = btn.dataset.active !== "true";
              this._callService("bosch_shc_camera", "update_rule", {
                camera_id: cId,
                rule_id: ruleId,
                is_active: newActive
              });
              btn.dataset.active = newActive ? "true" : "false";
              btn.textContent = newActive ? "AN" : "AUS";
              btn.classList.toggle("active", newActive);
            });
          });
          rulesListEl.querySelectorAll(".rule-delete").forEach(btn => {
            btn.addEventListener("click", e => {
              e.stopPropagation();
              const ruleId = btn.dataset.ruleId;
              const cId = btn.dataset.camId;
              this._callService("bosch_shc_camera", "delete_rule", {
                camera_id: cId,
                rule_id: ruleId
              });
              btn.closest(".rule-row")?.remove();
            });
          });
        }
      }
    }
    const zonesToggle = this.shadowRoot.getElementById("btn-show-zones");
    if (zonesToggle) {
      zonesToggle.classList.toggle("on", this._showMotionZones);
      const mzExists = hass.states[ents.motionZones];
      zonesToggle.style.display = mzExists ? "" : "none";
    }
    const zonesCountEl = this.shadowRoot.getElementById("diag-zones-count");
    const mzState = hass.states[ents.motionZones];
    const gen2Zones = mzState?.attributes?.gen2_zones || [];
    const cloudZones = mzState?.attributes?.cloud_zones || [];
    if (zonesCountEl) {
      if (gen2Zones.length > 0) zonesCountEl.textContent = `${gen2Zones.length} (Gen2)`; else if (cloudZones.length > 0) zonesCountEl.textContent = String(cloudZones.length); else if (mzState?.state != null && mzState.state !== "unavailable") zonesCountEl.textContent = `${mzState.state} (RCP)`; else zonesCountEl.textContent = "—";
    }
    const masksCountEl = this.shadowRoot.getElementById("diag-masks-count");
    const pmState = hass.states[ents.privacyMasks];
    const gen2Areas = pmState?.attributes?.gen2_private_areas || [];
    const cloudMasks = pmState?.attributes?.cloud_privacy_masks || [];
    if (masksCountEl) {
      const total = gen2Areas.length || cloudMasks.length;
      masksCountEl.textContent = total > 0 ? String(total) : pmState?.state != null && pmState.state !== "unavailable" ? pmState.state : "0";
    }
    const masksToggle = this.shadowRoot.getElementById("btn-show-masks");
    if (masksToggle) {
      masksToggle.classList.toggle("on", this._showPrivacyMasks);
      const hasMasks = gen2Areas.length > 0 || cloudMasks.length > 0;
      masksToggle.style.display = hasMasks ? "" : "none";
    }
  }
  _escHtml(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
  }
  _renderServiceButtons() {
    const grid = this.shadowRoot.getElementById("svc-grid");
    if (!grid) return;
    const camId = () => this._hass?.states[this._entities.status]?.attributes?.camera_id || "";
    const services = [ {
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/><circle cx="12" cy="13" r="4"/></svg>',
      label: "Snapshot",
      svc: "trigger_snapshot",
      data: {}
    }, {
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18"/></svg>',
      label: "Zonen lesen",
      svc: "get_motion_zones",
      data: () => ({
        camera_id: camId()
      })
    }, {
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>',
      label: "Privacy-Masken",
      svc: "get_privacy_masks",
      data: () => ({
        camera_id: camId()
      })
    }, {
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>',
      label: "Freunde",
      svc: "list_friends",
      data: {}
    }, {
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>',
      label: "Regel erstellen",
      svc: "_prompt_create_rule",
      data: null
    }, {
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/></svg>',
      label: "Licht-Zeitplan",
      svc: "get_lighting_schedule",
      data: () => ({
        camera_id: camId()
      })
    }, {
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
      label: "Verbindung",
      svc: "open_live_connection",
      data: () => ({
        camera_id: camId()
      })
    }, {
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 01-3.46 0"/><line x1="1" y1="1" x2="23" y2="23"/></svg>',
      label: "Sirene",
      svc: "_trigger_siren",
      data: null
    } ];
    grid.innerHTML = services.map((s, i) => `<button class="svc-btn" data-svc-idx="${i}">${s.icon}<span>${s.label}</span></button>`).join("");
    const resultEl = this.shadowRoot.getElementById("svc-result");
    grid.querySelectorAll(".svc-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        const idx = parseInt(btn.dataset.svcIdx);
        const svc = services[idx];
        if (!svc || !this._hass) return;
        if (svc.svc === "_trigger_siren") {
          if (!confirm("Sirene wirklich auslösen?")) return;
          btn.classList.add("running");
          const sirenEntity = this._entities.siren;
          if (sirenEntity && this._hass.states[sirenEntity]) {
            this._hass.callService("button", "press", {
              entity_id: sirenEntity
            });
            if (resultEl) {
              resultEl.style.display = "";
              resultEl.textContent = "Sirene wird ausgelöst...";
            }
          } else {
            if (resultEl) {
              resultEl.style.display = "";
              resultEl.textContent = "Sirene nicht verfügbar für diese Kamera.";
            }
          }
          setTimeout(() => {
            btn.classList.remove("running");
          }, 3e3);
          return;
        }
        if (svc.svc === "_prompt_create_rule") {
          const name = prompt("Regel-Name:", "Neue Regel");
          if (!name) return;
          const start = prompt("Startzeit (HH:MM):", "08:00");
          if (!start) return;
          const end = prompt("Endzeit (HH:MM):", "20:00");
          if (!end) return;
          btn.classList.add("running");
          this._callService("bosch_shc_camera", "create_rule", {
            camera_id: camId(),
            name: name,
            start_time: start + ":00",
            end_time: end + ":00",
            weekdays: [ 0, 1, 2, 3, 4, 5, 6 ],
            is_active: true
          });
          if (resultEl) {
            resultEl.style.display = "";
            resultEl.textContent = `Regel "${name}" wird erstellt...`;
          }
          setTimeout(() => {
            btn.classList.remove("running");
          }, 3e3);
          return;
        }
        btn.classList.add("running");
        const data = typeof svc.data === "function" ? svc.data() : svc.data;
        this._callService("bosch_shc_camera", svc.svc, data);
        if (resultEl) {
          resultEl.style.display = "";
          resultEl.textContent = `${svc.label} wird ausgeführt...`;
        }
        setTimeout(() => {
          btn.classList.remove("running");
          if (resultEl) {
            resultEl.textContent = `${svc.label} abgeschlossen.`;
            setTimeout(() => {
              resultEl.style.display = "none";
            }, 5e3);
          }
        }, 3e3);
      });
    });
  }
  _getEffectiveState(entityId) {
    if (entityId in this._optimistic) return this._optimistic[entityId];
    return this._hass?.states[entityId]?.state;
  }
  _waitForStreamReady(attempt = 0) {
    if (!this._waitingForStream || !this._hass) return;
    const cam = this._hass.states[this._entities.camera];
    const camReady = cam?.state === "streaming";
    if (attempt > 0 && attempt % 10 === 0) {
      const sec = attempt;
      if (sec < 20) this._setLoadingOverlay(true, "Encoder wird aufgewärmt…"); else if (sec < 40) this._setLoadingOverlay(true, "Stream wird vorbereitet…"); else this._setLoadingOverlay(true, "Verbindung wird aufgebaut…");
    }
    if (camReady) {
      this._waitingForStream = false;
      this._setLoadingOverlay(true, "HLS wird geladen…");
      this._startLiveVideo();
      return;
    }
    if (attempt > 90) {
      this._waitingForStream = false;
      this._streamConnecting = false;
      if (this._connectSteps) {
        this._connectSteps.forEach(t => clearTimeout(t));
        this._connectSteps = null;
      }
      this._setLoadingOverlay(false);
      return;
    }
    setTimeout(() => this._waitForStreamReady(attempt + 1), 1e3);
  }
  _updateMotionZones(hass, ents) {
    const svg = this.shadowRoot.getElementById("motion-zones-overlay");
    if (!svg) return;
    const zoneState = hass.states[ents.motionZones];
    const gen2Zones = zoneState?.attributes?.gen2_zones || [];
    const cloudZones = zoneState?.attributes?.cloud_zones || [];
    const hasZones = gen2Zones.length > 0 || cloudZones.length > 0;
    const showZones = this._showMotionZones && hasZones;
    svg.classList.toggle("visible", showZones);
    if (!showZones) return;
    const coordKey = JSON.stringify(gen2Zones.length > 0 ? gen2Zones : cloudZones);
    if (this._lastMotionCoordKey === coordKey) return;
    this._lastMotionCoordKey = coordKey;
    svg.innerHTML = "";
    if (gen2Zones.length > 0) {
      const defaultColors = [ "#0A84FF", "#34C759", "#FF9F0A", "#FF453A", "#AF52DE" ];
      gen2Zones.forEach((z, i) => {
        const points = z.points || z.polygon || z.vertices || [];
        if (points.length < 3) return;
        const color = z.color || defaultColors[i % defaultColors.length];
        const poly = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
        const pts = points.map(p => `${(p.x || 0) * 100},${(p.y || 0) * 100}`).join(" ");
        poly.setAttribute("points", pts);
        poly.setAttribute("fill", color);
        poly.setAttribute("stroke", color);
        svg.appendChild(poly);
      });
    } else {
      for (const z of cloudZones) {
        if (z.x == null || z.y == null || z.w == null || z.h == null) continue;
        const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        rect.setAttribute("x", z.x * 100);
        rect.setAttribute("y", z.y * 100);
        rect.setAttribute("width", z.w * 100);
        rect.setAttribute("height", z.h * 100);
        svg.appendChild(rect);
      }
    }
  }
  _updatePrivacyMasks(hass, ents) {
    const svg = this.shadowRoot.getElementById("privacy-mask-overlay");
    if (!svg) return;
    const pmState = hass.states[ents.privacyMasks];
    const gen2Areas = pmState?.attributes?.gen2_private_areas || [];
    const cloudMasks = pmState?.attributes?.cloud_privacy_masks || [];
    const hasMasks = gen2Areas.length > 0 || cloudMasks.length > 0;
    const showMasks = this._showPrivacyMasks && hasMasks;
    svg.classList.toggle("visible", showMasks);
    if (!showMasks) return;
    const coordKey = JSON.stringify(gen2Areas.length > 0 ? gen2Areas : cloudMasks);
    if (this._lastPrivacyMaskKey === coordKey) return;
    this._lastPrivacyMaskKey = coordKey;
    svg.innerHTML = "";
    if (gen2Areas.length > 0) {
      for (const a of gen2Areas) {
        const points = a.points || a.polygon || a.vertices || [];
        if (points.length < 3) continue;
        const poly = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
        const pts = points.map(p => `${(p.x || 0) * 100},${(p.y || 0) * 100}`).join(" ");
        poly.setAttribute("points", pts);
        svg.appendChild(poly);
      }
    } else {
      for (const m of cloudMasks) {
        if (m.x == null || m.y == null || m.w == null || m.h == null) continue;
        const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        rect.setAttribute("x", m.x * 100);
        rect.setAttribute("y", m.y * 100);
        rect.setAttribute("width", m.w * 100);
        rect.setAttribute("height", m.h * 100);
        svg.appendChild(rect);
      }
    }
  }
  async _toggleStream() {
    let serverIsOn = null;
    if (this._hass && this._entities.switch) {
      try {
        const fresh = await this._hass.callApi("GET", `states/${this._entities.switch}`);
        if (fresh && fresh.state) serverIsOn = fresh.state === "on";
      } catch (e) {}
    }
    const cachedIsOn = this._isStreaming();
    if (serverIsOn !== null && serverIsOn !== cachedIsOn) {
      console.warn("bosch-camera-card: stale state detected — card thought " + (cachedIsOn ? "streaming" : "idle") + ", server says " + (serverIsOn ? "streaming" : "idle") + ". Refreshing the view; tap again to toggle.");
      delete this._optimistic[this._entities.switch];
      this._update();
      return;
    }
    const isOn = serverIsOn !== null ? serverIsOn : cachedIsOn;
    this._setOptimistic(this._entities.switch, isOn ? "off" : "on");
    if (isOn) {
      this._streamConnecting = false;
      this._waitingForStream = false;
      if (this._connectSteps) {
        this._connectSteps.forEach(t => clearTimeout(t));
        this._connectSteps = null;
      }
    } else if (!this._streamConnecting) {
      this._streamConnecting = true;
      this._setLoadingOverlay(true, "Verbindung wird aufgebaut…");
      this._connectSteps = [ setTimeout(() => {
        if (this._streamConnecting) this._setLoadingOverlay(true, "Kamera wird aufgeweckt…");
      }, 3e3), setTimeout(() => {
        if (this._streamConnecting) this._setLoadingOverlay(true, "Stream wird vorbereitet…");
      }, 7e3), setTimeout(() => {
        if (this._streamConnecting) this._setLoadingOverlay(true, "HLS wird gestartet…");
      }, 12e3), setTimeout(() => {
        if (this._streamConnecting) this._setLoadingOverlay(true, "Warte auf erstes Bild…");
      }, 2e4), setTimeout(() => {
        if (this._streamConnecting) this._setLoadingOverlay(true, "Gleich geschafft…");
      }, 28e3), setTimeout(() => {
        if (this._streamConnecting) this._setLoadingOverlay(true, "Kamera braucht etwas…");
      }, 4e4), setTimeout(() => {
        if (this._streamConnecting) this._setLoadingOverlay(true, "Verbindung wird aufgebaut…");
      }, 52e3), setTimeout(() => {
        if (this._streamConnecting) this._setLoadingOverlay(true, "Fast fertig…");
      }, 65e3), setTimeout(() => {
        if (this._streamConnecting) this._setLoadingOverlay(true, "Noch einen Moment…");
      }, 78e3) ];
    }
    const prevState = isOn ? "on" : "off";
    this._entityToBtnId[this._entities.switch] = "btn-stream";
    this._hass?.callService("switch", isOn ? "turn_off" : "turn_on", {
      entity_id: this._entities.switch
    }).catch(err => {
      console.warn("bosch-camera-card: stream toggle failed:", err);
      this._setOptimistic(this._entities.switch, prevState);
      if (!isOn) {
        this._streamConnecting = false;
        this._waitingForStream = false;
        if (this._connectSteps) {
          this._connectSteps.forEach(t => clearTimeout(t));
          this._connectSteps = null;
        }
        this._setLoadingOverlay(false);
      }
      this._flashEntityError(this._entities.switch);
    });
  }
  _toggleAudio() {
    const entityId = this._entities.audio;
    if (!this._hass || !entityId) return;
    const state = this._hass.states[entityId]?.state;
    if (!state || state === "unavailable" || state === "unknown") return;
    const turningOn = state !== "on";
    this._setOptimistic(entityId, turningOn ? "on" : "off");
    this._callService("switch", turningOn ? "turn_on" : "turn_off", {
      entity_id: entityId
    });
  }
  _toggleSwitch(entityId) {
    if (!this._hass || !entityId) return;
    const state = this._hass.states[entityId]?.state;
    if (!state || state === "unavailable" || state === "unknown") return;
    const turningOn = state !== "on";
    this._setOptimistic(entityId, turningOn ? "on" : "off");
    this._callService("switch", turningOn ? "turn_on" : "turn_off", {
      entity_id: entityId
    });
  }
  _toggleSwitchWithRollback(entityId) {
    if (!this._hass || !entityId) return;
    const state = this._hass.states[entityId]?.state;
    if (!state || state === "unavailable" || state === "unknown") return;
    const turningOn = state !== "on";
    const prev = turningOn ? "off" : "on";
    const target = turningOn ? "on" : "off";
    this._callServiceWithRollback(entityId, prev, target, "switch", turningOn ? "turn_on" : "turn_off", {
      entity_id: entityId
    });
  }
  _onQualityChange(option) {
    const entityId = this._entities.quality;
    if (!entityId || !this._hass) return;
    this._callService("select", "select_option", {
      entity_id: entityId,
      option: option
    });
  }
  _setOptimistic(entityId, state) {
    this._optimistic[entityId] = state;
    if (this._optimisticTimers[entityId]) clearTimeout(this._optimisticTimers[entityId]);
    this._optimisticTimers[entityId] = setTimeout(() => {
      delete this._optimistic[entityId];
      delete this._optimisticTimers[entityId];
    }, 8e3);
    this._update();
  }
  _requestFullscreen() {
    if (this.classList.contains("fs-active")) {
      this._exitCssFullscreen();
      return;
    }
    const wrapper = this.shadowRoot.getElementById("img-wrapper");
    const el = wrapper || this;
    const tryNative = () => {
      if (el.requestFullscreen) return el.requestFullscreen();
      if (el.webkitRequestFullscreen) return Promise.resolve(el.webkitRequestFullscreen());
      if (el.mozRequestFullScreen) return Promise.resolve(el.mozRequestFullScreen());
      if (el.msRequestFullscreen) return Promise.resolve(el.msRequestFullscreen());
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
    this._fsClickOut = e => {
      if (!this.contains(e.target)) this._exitCssFullscreen();
    };
    this._fsKeyDown = e => {
      if (e.key === "Escape") this._exitCssFullscreen();
    };
    setTimeout(() => {
      document.addEventListener("click", this._fsClickOut);
      document.addEventListener("keydown", this._fsKeyDown);
    }, 100);
  }
  _exitCssFullscreen() {
    this.classList.remove("fs-active");
    document.body.style.overflow = "";
    if (this._fsClickOut) {
      document.removeEventListener("click", this._fsClickOut);
      this._fsClickOut = null;
    }
    if (this._fsKeyDown) {
      document.removeEventListener("keydown", this._fsKeyDown);
      this._fsKeyDown = null;
    }
  }
  _callService(domain, service, data) {
    if (!this._hass) return;
    this._hass.callService(domain, service, data).catch(err => console.warn("bosch-camera-card:", domain, service, err));
  }
  _callServiceWithRollback(entityId, prevState, targetState, domain, service, data) {
    if (!this._hass) return;
    this._setOptimistic(entityId, "pending");
    this._hass.callService(domain, service, data).then(() => {
      this._setOptimistic(entityId, targetState);
    }).catch(err => {
      console.warn("bosch-camera-card:", domain, service, err);
      this._setOptimistic(entityId, prevState);
      this._flashEntityError(entityId);
    });
  }
  _flashEntityError(entityId) {
    const domId = this._entityToBtnId[entityId];
    if (!domId) {
      this._update();
      return;
    }
    const el = this.shadowRoot.getElementById(domId);
    if (!el) return;
    el.classList.add("error");
    if (this._errorFeedbackTimers[entityId]) clearTimeout(this._errorFeedbackTimers[entityId]);
    this._errorFeedbackTimers[entityId] = setTimeout(() => {
      el.classList.remove("error");
      delete this._errorFeedbackTimers[entityId];
    }, 2e3);
  }
  _formatDatetime(d) {
    const p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  static getStubConfig() {
    return {
      camera_entity: "camera.bosch_garten"
    };
  }
  getCardSize() {
    return 4;
  }
}

customElements.define("bosch-camera-card", BoschCameraCard);

window.customCards = window.customCards || [];

window.customCards.push({
  type: "bosch-camera-card",
  name: "Bosch Camera Card",
  description: "Bosch Smart Home cameras with streaming state, loading indicator and controls",
  preview: false
});

const OVERVIEW_VERSION = "1.1.0";

class BoschCameraOverviewCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({
      mode: "open"
    });
    this._cards = new Map;
    this._lastSig = "";
    this._config = null;
    this._hass = null;
    this._rendered = false;
  }
  setConfig(config) {
    this._config = {
      online_offline_view: config.online_offline_view !== false,
      title: config.title || "",
      min_width: config.min_width || "360px",
      gap: config.gap || "12px",
      columns: config.columns ?? "auto",
      exclude: Array.isArray(config.exclude) ? config.exclude : [],
      include: Array.isArray(config.include) ? config.include : [],
      compact: !!config.compact,
      use_bosch_sort: config.use_bosch_sort === true,
      minimal: config.minimal === true,
      overrides: config.overrides && typeof config.overrides === "object" ? config.overrides : {},
      card_defaults: config.card_defaults && typeof config.card_defaults === "object" ? config.card_defaults : {}
    };
    if (this._config.minimal) {
      this._config.card_defaults = {
        ...this._config.card_defaults,
        minimal: true
      };
    }
    this._rendered = false;
    this._lastSig = "";
    this._cards.clear();
    if (this.shadowRoot) this.shadowRoot.innerHTML = "";
    if (this._hass) this._update();
  }
  set hass(hass) {
    this._hass = hass;
    this._update();
  }
  get hass() {
    return this._hass;
  }
  _renderShell() {
    this.shadowRoot.innerHTML = `\n      <style>\n        :host { display: block; }\n        .bco-wrap { display: block; padding: 4px; overflow: visible; }\n        .bco-header {\n          display: flex; align-items: center; justify-content: space-between;\n          padding: 0 4px 8px; font-size: 14px; font-weight: 500;\n          color: var(--primary-text-color);\n        }\n        .bco-count {\n          font-size: 12px; font-weight: 400;\n          color: var(--secondary-text-color);\n        }\n        .bco-grid {\n          display: grid;\n          gap: ${this._config.gap};\n          grid-template-columns: ${this._config.columns === "auto" || !this._config.columns ? `repeat(auto-fill, minmax(${this._config.min_width}, 1fr))` : `repeat(${Number(this._config.columns)}, minmax(0, 1fr))`};\n        }\n        @media (max-width: 640px) {\n          .bco-grid { grid-template-columns: 1fr !important; }\n        }\n        /* Phones in landscape (e.g. iPhone Pro Max ≈ 932 × 430) are wider\n           than 640px but the viewport height collapses below ~500px — at\n           that aspect a 2-column tile grid leaves each tile ~12 lines tall\n           which is unusable. Force single column when any of:\n             - touch device up to small-tablet width (1024px), or\n             - landscape with very short viewport (any device).\n           Desktop browsers resized narrow keep their multi-column layout. */\n        @media (pointer: coarse) and (max-width: 1024px) {\n          .bco-grid { grid-template-columns: 1fr !important; }\n        }\n        @media (orientation: landscape) and (max-height: 500px) {\n          .bco-grid { grid-template-columns: 1fr !important; }\n        }\n        .bco-cell {\n          min-width: 0;\n          position: relative;\n          border-radius: 14px;\n          border: 2px solid transparent;\n          overflow: hidden;\n          transition: border-color 0.2s ease;\n        }\n        .bco-cell[data-tier="0"] { border-color: rgba(76, 175, 80, 0.55); }\n        .bco-cell[data-tier="1"] { border-color: rgba(255, 152, 0, 0.55); }\n        .bco-cell[data-tier="2"] { border-color: rgba(120, 120, 120, 0.35); opacity: 0.92; }\n        .bco-cell bosch-camera-card { display: block; min-width: 0; }\n        .bco-section {\n          grid-column: 1 / -1;\n          font-size: 11px;\n          font-weight: 600;\n          letter-spacing: 0.08em;\n          text-transform: uppercase;\n          color: var(--secondary-text-color);\n          padding: 8px 4px 2px;\n          border-top: 1px solid var(--divider-color, rgba(255,255,255,0.1));\n          margin-top: 4px;\n        }\n        .bco-section.first { border-top: none; margin-top: 0; padding-top: 2px; }\n        .bco-empty {\n          grid-column: 1 / -1;\n          padding: 24px 12px;\n          text-align: center;\n          color: var(--secondary-text-color);\n          font-size: 14px;\n        }\n        bosch-camera-card { display: block; }\n        @media (max-width: 480px) {\n          .bco-grid { gap: 8px; }\n        }\n      </style>\n      <div class="bco-wrap">\n        ${this._config.title ? `\n          <div class="bco-header">\n            <span>${this._escape(this._config.title)}</span>\n            <span class="bco-count" id="bco-count"></span>\n          </div>` : ""}\n        <div class="bco-grid" id="bco-grid"></div>\n      </div>\n    `;
    this._grid = this.shadowRoot.getElementById("bco-grid");
    this._countEl = this.shadowRoot.getElementById("bco-count");
    this._rendered = true;
  }
  _escape(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }[c]));
  }
  _discover() {
    if (!this._hass) return [];
    const states = this._hass.states || {};
    const explicit = this._config.include.length > 0;
    const list = [];
    const candidates = explicit ? this._config.include : Object.keys(states).filter(eid => eid.startsWith("camera."));
    for (const eid of candidates) {
      if (this._config.exclude.includes(eid)) continue;
      const s = states[eid];
      if (!s) continue;
      const a = s.attributes || {};
      if (!explicit && a.brand !== "Bosch") continue;
      const status = String(a.status || "").toUpperCase();
      const online = status === "ONLINE";
      const base = eid.replace(/^camera\./, "");
      const privState = states[`switch.${base}_privacy_mode`];
      const privacyOn = !!(privState && String(privState.state).toLowerCase() === "on");
      const swState = states[`switch.${base}_live_stream`];
      const streamingOn = !!(swState && String(swState.state).toLowerCase() === "on");
      const tier = !online ? 2 : privacyOn ? 1 : 0;
      const rawPrio = a.bosch_priority;
      const priority = typeof rawPrio === "number" && isFinite(rawPrio) ? rawPrio : null;
      list.push({
        entity_id: eid,
        name: a.friendly_name || eid,
        online: online,
        privacyOn: privacyOn,
        streamingOn: streamingOn,
        tier: tier,
        priority: priority,
        status: status || "UNKNOWN",
        model: a.model_name || ""
      });
    }
    const useBosch = this._config.use_bosch_sort;
    list.sort((a, b) => {
      if (a.tier !== b.tier) return a.tier - b.tier;
      if (a.streamingOn !== b.streamingOn) return a.streamingOn ? -1 : 1;
      if (useBosch) {
        const aHas = a.priority !== null;
        const bHas = b.priority !== null;
        if (aHas && bHas && a.priority !== b.priority) return a.priority - b.priority;
        if (aHas !== bHas) return aHas ? -1 : 1;
      }
      return a.name.localeCompare(b.name, "de");
    });
    return list;
  }
  _update() {
    if (!this._hass || !this._config) return;
    if (!this._rendered) this._renderShell();
    let cams = this._discover();
    if (!this._config.online_offline_view) cams = cams.filter(c => c.online);
    const sig = cams.map(c => `${c.entity_id}:${c.tier}:${c.streamingOn ? "S" : ""}`).join("|");
    const needsReorder = sig !== this._lastSig;
    this._lastSig = sig;
    const keep = new Set(cams.map(c => c.entity_id));
    for (const [eid, el] of [ ...this._cards.entries() ]) {
      if (!keep.has(eid)) {
        el.remove();
        this._cards.delete(eid);
      }
    }
    if (needsReorder) {
      this._grid.innerHTML = "";
      if (cams.length === 0) {
        const empty = document.createElement("div");
        empty.className = "bco-empty";
        empty.textContent = "Keine Bosch-Kameras gefunden.";
        this._grid.appendChild(empty);
      } else {
        for (const c of cams) {
          let cell = this._cards.get(c.entity_id);
          if (!cell) {
            cell = document.createElement("div");
            cell.className = "bco-cell";
            const card = document.createElement("bosch-camera-card");
            const override = this._config.overrides[c.entity_id] || {};
            try {
              card.setConfig({
                ...this._config.card_defaults,
                camera_entity: c.entity_id,
                title: c.name.replace(/^Bosch\s+/i, ""),
                ...override,
                camera_entity: c.entity_id
              });
            } catch (e) {
              console.error(`bosch-camera-overview-card: setConfig failed for ${c.entity_id}`, e);
              continue;
            }
            cell.appendChild(card);
            cell._innerCard = card;
            this._cards.set(c.entity_id, cell);
          }
          cell.dataset.tier = String(c.tier);
          this._grid.appendChild(cell);
        }
      }
    }
    for (const cell of this._cards.values()) {
      const inner = cell._innerCard || cell.querySelector?.("bosch-camera-card");
      if (inner) inner.hass = this._hass;
    }
    if (this._countEl) {
      const live = cams.filter(c => c.tier === 0).length;
      const priv = cams.filter(c => c.tier === 1).length;
      const off = cams.filter(c => c.tier === 2).length;
      const parts = [];
      if (live) parts.push(`${live} live`);
      if (priv) parts.push(`${priv} privat`);
      if (off) parts.push(`${off} offline`);
      this._countEl.textContent = parts.join(" · ");
    }
  }
  static getStubConfig() {
    return {
      online_offline_view: true,
      title: "Bosch Kameras"
    };
  }
  getCardSize() {
    return Math.max(4, this._cards ? this._cards.size * 3 : 4);
  }
}

customElements.define("bosch-camera-overview-card", BoschCameraOverviewCard);

window.customCards.push({
  type: "bosch-camera-overview-card",
  name: "Bosch Camera Overview",
  description: "Auto-discovers all Bosch Smart Home cameras and renders them in a responsive grid (online first, offline after).",
  preview: false
});