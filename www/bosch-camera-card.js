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
const CARD_VERSION = "13.4.4.2";

let _boschFsExitAt = 0;

let _boschFsOwner = null;

const AUTO_PLAY_MODES = [ "lan", "always", "never" ];

const CARD_I18N = {
  en: {
    play_gate_label: "Start stream",
    play_gate_hint_remote: "You're on a remote connection — tap to start",
    play_gate_hint_default: "Tap to start the live stream"
  },
  de: {
    play_gate_label: "Stream starten",
    play_gate_hint_remote: "Du bist remote — antippen zum Starten",
    play_gate_hint_default: "Antippen, um den Live-Stream zu starten"
  }
};

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
    lowLatencyMode: true
  },
  stable: {
    liveSyncDurationCount: 6,
    liveMaxLatencyDurationCount: 12,
    maxBufferLength: 22,
    maxMaxBufferLength: 28,
    lowLatencyMode: true
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
    this._remoteSkipWebRTC = (() => {
      const ua = navigator.userAgent || "";
      const isCompanion = /Home\s?Assistant/i.test(ua);
      const isIOS = /iPhone|iPod/i.test(ua) || /Macintosh/i.test(ua) && (navigator.maxTouchPoints || 0) > 1;
      const isAndroid = /Android/i.test(ua);
      const isMobileBrowser = !isCompanion && (isIOS || isAndroid);
      if (!isCompanion && !isMobileBrowser) return false;
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
    this._androidAudioMuted = /Android/i.test(navigator.userAgent || "");
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
    this._onThemeBroadcast = this._onThemeBroadcast.bind(this);
    this._onModeBroadcast = this._onModeBroadcast.bind(this);
    this._activeTheme = "ios";
    this._activeMode = "night";
  }
  connectedCallback() {
    this._visibilityHandler = () => this._onVisibilityChange();
    document.addEventListener("visibilitychange", this._visibilityHandler);
    this._pagehideHandler = () => this._stopLiveVideo();
    window.addEventListener("pagehide", this._pagehideHandler);
    window.addEventListener("bosch-card-theme-change", this._onThemeBroadcast);
    window.addEventListener("bosch-card-mode-change", this._onModeBroadcast);
    this._onFullscreenChange = () => {
      if (!this._isNativeFullscreen()) _boschFsExitAt = Date.now();
      this._updateFullscreenButtonState();
    };
    document.addEventListener("fullscreenchange", this._onFullscreenChange);
    document.addEventListener("webkitfullscreenchange", this._onFullscreenChange);
    document.addEventListener("mozfullscreenchange", this._onFullscreenChange);
    document.addEventListener("MSFullscreenChange", this._onFullscreenChange);
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
      minimal: config.minimal === true,
      auto_play: AUTO_PLAY_MODES.includes(config.auto_play) ? config.auto_play : null,
      apple_style: config.apple_style !== false,
      theme: [ "ios", "android", "auto" ].includes(config.theme) ? config.theme : "ios",
      mode: [ "auto", "day", "night" ].includes(config.mode) ? config.mode : "auto",
      border_radius: typeof config.border_radius === "string" ? config.border_radius : null,
      box_shadow: typeof config.box_shadow === "string" ? config.box_shadow : null,
      compact: config.compact === true,
      show_title: config.show_title !== false,
      show_last_event: config.show_last_event !== false
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
      alarmState: config.alarm_state_entity || `sensor.${base}_alarm_status`,
      sirenDuration: config.siren_duration_entity || `number.${base}_sirenen_dauer`,
      alarmActivationDelay: config.alarm_activation_delay_entity || `number.${base}_alarm_verzogerung`,
      preAlarmDelay: config.prealarm_delay_entity || `number.${base}_pre_alarm_dauer`,
      powerLedBrightness: config.power_led_entity || `number.${base}_power_led`,
      imageRotation180: config.image_rotation_180_entity || `switch.${base}_bild_180deg_drehen`
    };
    this._showMotionZones = this._config.show_motion_zones;
    this.classList.toggle("minimal", this._config.minimal);
    this.classList.toggle("apple-style", this._config.apple_style);
    this.classList.toggle("compact", this._config.compact);
    this.classList.toggle("no-title", !this._config.show_title);
    this.classList.toggle("no-last-event", !this._config.show_last_event);
    this._applyOsClass();
    this._applyTheme(this._resolveTheme());
    this._applyMode(this._resolveMode());
    this.classList.toggle("overflow-open", this._config.apple_style && !this._config.minimal && !this._config.compact);
    if (this._config.border_radius) this.style.setProperty("--bosch-card-radius", this._config.border_radius); else this.style.removeProperty("--bosch-card-radius");
    if (this._config.box_shadow) this.style.setProperty("--bosch-card-shadow", this._config.box_shadow); else this.style.removeProperty("--bosch-card-shadow");
    this._render();
    this._restoreCachedImage();
    this._startRefreshTimer();
    this._loadHlsJs().catch(() => {});
  }
  _resolveTheme() {
    const cfg = this._config?.theme || "ios";
    if (cfg === "ios" || cfg === "android") return cfg;
    return this._detectTheme();
  }
  _detectTheme() {
    const ua = (navigator.userAgent || "").toLowerCase();
    if (/android/.test(ua)) return "android";
    return "ios";
  }
  _applyTheme(theme) {
    this.classList.toggle("theme-ios", theme === "ios");
    this.classList.toggle("theme-android", theme === "android");
    this._activeTheme = theme;
    this._refreshThemeSwitcher();
  }
  _setUserTheme(theme) {
    try {
      if (theme === "auto") window.localStorage?.removeItem("bosch-card-theme"); else if (theme === "ios" || theme === "android") window.localStorage?.setItem("bosch-card-theme", theme);
    } catch {}
    window.dispatchEvent(new CustomEvent("bosch-card-theme-change", {
      detail: {
        theme: theme
      }
    }));
  }
  _applyOsClass() {
    const ua = navigator.userAgent || "";
    const touch = (navigator.maxTouchPoints || 0) > 1;
    let os = "other";
    if (/Windows|Win32|Win64/i.test(ua)) os = "windows"; else if (/iPhone|iPad|iPod/i.test(ua) || /Macintosh/i.test(ua) && touch) os = "ios"; else if (/Macintosh|Mac OS X/i.test(ua)) os = "macos"; else if (/Android/i.test(ua)) os = "android"; else if (/Linux|X11|CrOS/i.test(ua)) os = "linux";
    for (const c of [ "windows", "macos", "ios", "android", "linux", "other" ]) {
      this.classList.toggle("os-" + c, c === os);
    }
  }
  _onThemeBroadcast(_ev) {
    this._applyTheme(this._resolveTheme());
  }
  _refreshThemeSwitcher() {
    const sw = this.shadowRoot?.getElementById("ap-theme-switcher");
    if (!sw) return;
    let stored = null;
    try {
      stored = window.localStorage?.getItem("bosch-card-theme");
    } catch {}
    const cfgTheme = this._config?.theme;
    const selected = stored === "ios" || stored === "android" ? stored : cfgTheme === "ios" || cfgTheme === "android" ? cfgTheme : "auto";
    sw.querySelectorAll("[data-theme]").forEach(b => {
      b.classList.toggle("on", b.getAttribute("data-theme") === selected);
      b.setAttribute("aria-pressed", b.getAttribute("data-theme") === selected ? "true" : "false");
    });
  }
  _resolveMode() {
    const cfg = this._config?.mode || "auto";
    if (cfg === "day" || cfg === "night") return cfg;
    return this._detectMode();
  }
  _detectMode() {
    try {
      if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) return "night";
    } catch {}
    return "day";
  }
  _applyMode(mode) {
    this.classList.toggle("mode-day", mode === "day");
    this.classList.toggle("mode-night", mode === "night");
    this._activeMode = mode;
    this._refreshModeSwitcher();
  }
  _setUserMode(mode) {
    try {
      if (mode === "auto") window.localStorage?.removeItem("bosch-card-mode"); else if (mode === "day" || mode === "night") window.localStorage?.setItem("bosch-card-mode", mode);
    } catch {}
    window.dispatchEvent(new CustomEvent("bosch-card-mode-change", {
      detail: {
        mode: mode
      }
    }));
  }
  _onModeBroadcast(_ev) {
    this._applyMode(this._resolveMode());
  }
  _markLiveBadge() {
    const badge = this.shadowRoot?.getElementById("stream-badge");
    if (badge) badge.className = "stream-badge streaming";
    const apBadge = this.shadowRoot?.getElementById("ap-badge");
    if (apBadge) {
      apBadge.className = "ap-badge live";
      apBadge.textContent = "Live";
    }
    const apBtnStream = this.shadowRoot?.getElementById("ap-btn-stream");
    if (apBtnStream) apBtnStream.classList.remove("connecting");
    this._refreshAudioToggle();
  }
  _refreshAudioToggle() {
    if (!this._liveVideoActive) return;
    const video = this.shadowRoot?.getElementById("cam-video");
    const b = this.shadowRoot?.getElementById("btn-audio");
    if (!video || !b || b.style.display === "none") return;
    b.classList.toggle("on", !video.muted);
    b.classList.toggle("tap-hint", !!video.muted);
    const lbl = b.querySelector(".sw-left span");
    if (lbl) lbl.textContent = video.muted ? "Ton einschalten" : "Ton";
    b.setAttribute("title", video.muted ? "Tippen für Ton" : "Ton stummschalten");
  }
  _refreshModeSwitcher() {
    const sw = this.shadowRoot?.getElementById("ap-mode-switcher");
    if (!sw) return;
    let stored = null;
    try {
      stored = window.localStorage?.getItem("bosch-card-mode");
    } catch {}
    const cfgMode = this._config?.mode;
    const selected = stored === "day" || stored === "night" ? stored : cfgMode === "day" || cfgMode === "night" ? cfgMode : "auto";
    sw.querySelectorAll("[data-mode]").forEach(b => {
      b.classList.toggle("on", b.getAttribute("data-mode") === selected);
      b.setAttribute("aria-pressed", b.getAttribute("data-mode") === selected ? "true" : "false");
    });
  }
  _hassFingerprint(hass) {
    if (!hass || !hass.states) return "";
    let fp = "";
    const ents = this._entities || {};
    for (const k in ents) {
      const v = ents[k];
      if (typeof v === "string" && v.indexOf(".") > 0) {
        const s = hass.states[v];
        fp += v + "=" + (s ? s.state + "@" + s.last_updated : "_") + ";";
      }
    }
    const me = this._config && this._config.maintenance_entity;
    if (me) {
      const s = hass.states[me];
      fp += me + "=" + (s ? s.state + "@" + s.last_updated : "_") + ";";
    }
    return fp;
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
    if (firstHass) {
      this._lastHassFp = this._hassFingerprint(hass);
    } else {
      const fp = this._hassFingerprint(hass);
      if (fp === this._lastHassFp) return;
      this._lastHassFp = fp;
    }
    this._applyImageRotation180();
    if (firstHass) this._maybeAutoPlay();
    this._update();
    if (firstHass) {
      this._awaitingFresh = true;
      if (this._imageLoaded) {
        this._setLoadingOverlay(true, "Aktualisiere…");
      }
      this._triggerFreshSnapshot();
      this._pullFreshSwitchStates();
      setTimeout(() => this._maybeAutoPlay(), 800);
    }
  }
  _maybeAutoPlay() {
    this._evaluateGateForStreamTransition();
  }
  _evaluateGateForStreamTransition() {
    if (!this._hass || !this._entities.switch) return;
    const switchEnt = this._hass.states[this._entities.switch];
    if (!switchEnt) return;
    const curr = switchEnt.state;
    const prev = this._lastEvaluatedSwitchState;
    this._lastEvaluatedSwitchState = curr;
    if (curr !== "on") {
      if (this._playGateActive) this._hidePlayGate();
      return;
    }
    if (prev === "on") return;
    const camEnt = this._hass.states[this._entities.camera];
    if (camEnt && camEnt.state === "unavailable") return;
    const mode = this._getAutoPlayMode();
    if (mode === "always") return;
    if (mode === "lan" && this._isLanSession()) return;
    this._showPlayGate();
  }
  _showPlayGate() {
    this._playGateActive = true;
    this._setLoadingOverlay(false);
    const el = this.shadowRoot?.getElementById("auto-play-gate");
    if (!el) return;
    const isLan = this._isLanSession();
    const hint = el.querySelector(".apg-hint");
    if (hint) {
      hint.textContent = this._t(isLan ? "play_gate_hint_default" : "play_gate_hint_remote");
    }
    const lbl = el.querySelector(".apg-label");
    if (lbl) lbl.textContent = this._t("play_gate_label");
    el.classList.add("visible");
  }
  _hidePlayGate() {
    this._playGateActive = false;
    const el = this.shadowRoot?.getElementById("auto-play-gate");
    if (el) el.classList.remove("visible");
  }
  _onPlayGateTap() {
    this._hidePlayGate();
    this._update();
  }
  _getAutoPlayMode() {
    const cfgMode = this._config?.auto_play;
    if (cfgMode && AUTO_PLAY_MODES.includes(cfgMode)) return cfgMode;
    const camAttrs = this._hass?.states[this._entities.camera]?.attributes || {};
    const attrMode = camAttrs.auto_play_default;
    if (attrMode && AUTO_PLAY_MODES.includes(attrMode)) return attrMode;
    return "lan";
  }
  _isLanSession() {
    if (!this._hass) return false;
    const cfg = this._hass.config || {};
    if (cfg.internal_url) {
      try {
        if (window.location.origin === new URL(cfg.internal_url).origin) return true;
      } catch (_) {}
    }
    if (cfg.external_url) {
      try {
        if (window.location.origin === new URL(cfg.external_url).origin) return false;
      } catch (_) {}
    }
    const h = (window.location.hostname || "").toLowerCase();
    if (!h) return false;
    if (h === "localhost" || h === "127.0.0.1" || h === "::1") return true;
    if (h.endsWith(".local") || h.endsWith(".fritz.box") || h.endsWith(".lan")) return true;
    if (/^192\.168\./.test(h)) return true;
    if (/^10\./.test(h)) return true;
    if (/^172\.(1[6-9]|2\d|3[01])\./.test(h)) return true;
    if (/^fe80:/i.test(h)) return true;
    return false;
  }
  _t(key) {
    const lang = (this._hass?.language || "en").toLowerCase();
    const dict = lang.startsWith("de") ? CARD_I18N.de : CARD_I18N.en;
    return dict[key] || CARD_I18N.en[key] || key;
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
      document.removeEventListener("pointerup", this._fsClickOut);
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
    window.removeEventListener("bosch-card-theme-change", this._onThemeBroadcast);
    window.removeEventListener("bosch-card-mode-change", this._onModeBroadcast);
    if (this._onFullscreenChange) {
      document.removeEventListener("fullscreenchange", this._onFullscreenChange);
      document.removeEventListener("webkitfullscreenchange", this._onFullscreenChange);
      document.removeEventListener("mozfullscreenchange", this._onFullscreenChange);
      document.removeEventListener("MSFullscreenChange", this._onFullscreenChange);
      this._onFullscreenChange = null;
    }
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
      setTimeout(() => {
        if (document.visibilityState === "visible" && !this._liveVideoActive) {
          this._triggerFreshSnapshot();
        }
      }, 500);
      this._pullFreshSwitchStates();
    }
    this._startRefreshTimer();
  }
  async _pullFreshSwitchStates() {
    if (!this._hass) return;
    const ids = [ this._entities.camera, this._entities.switch, this._entities.privacy, this._entities.audio, this._entities.light ].filter(id => id && this._hass.states[id]);
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
    if (!this._hass?.services?.bosch_shc_camera?.trigger_snapshot) return;
    if (this._hass.connected === false) return;
    if (this._hass.connection && this._hass.connection.connected === false) return;
    this._callService("bosch_shc_camera", "trigger_snapshot", {});
    this._scheduleImageLoad(1500);
    this._scheduleImageLoad(4e3);
  }
  _render() {
    this.shadowRoot.innerHTML = `\n      <style>\n        :host { display: block; font-family: var(--primary-font-family, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif); }\n        ha-card {\n          overflow: hidden;\n          /* Own --bosch-card-* vars (issue #21), not the global --ha-card-*\n             radius/shadow tokens — a dashboard theme that zeroes those must not\n             strip our card geometry. Background DOES follow the theme (intended). */\n          border-radius: var(--bosch-card-radius, var(--ha-card-border-radius, 12px));\n          background: var(--ha-card-background, var(--card-background-color, #1c1c1e));\n          box-shadow: var(--bosch-card-shadow, var(--ha-card-box-shadow, 0 2px 8px rgba(0,0,0,.3)));\n        }\n\n        /* Header */\n        .header {\n          display: flex; align-items: center; justify-content: space-between;\n          padding: 12px 14px 8px;\n        }\n        .header-left { display: flex; align-items: center; gap: 8px; }\n        .title {\n          font-size: 15px; font-weight: 600;\n          color: var(--primary-text-color, #e5e5ea);\n          white-space: nowrap; overflow: hidden; text-overflow: ellipsis;\n        }\n        .status-dot {\n          width: 8px; height: 8px; border-radius: 50%;\n          background: #636366; flex-shrink: 0; transition: background 0.3s;\n        }\n        .status-dot.online  { background: #30d158; }\n        .status-dot.offline { background: #ff453a; }\n\n        /* Stream badge */\n        .stream-badge {\n          display: inline-flex; align-items: center; gap: 5px;\n          font-size: 11px; font-weight: 600; letter-spacing: .4px;\n          text-transform: uppercase; padding: 3px 8px; border-radius: 20px;\n          transition: all 0.3s; white-space: nowrap;\n        }\n        .stream-badge.idle       { background: rgba(99,99,102,.25); color: #8e8e93; }\n        .stream-badge.streaming  { background: rgba(0,122,255,.2); color: #0a84ff; box-shadow: 0 0 0 1px rgba(0,122,255,.3); }\n        .stream-badge.connecting { background: rgba(255,159,10,.2); color: #ff9f0a; box-shadow: 0 0 0 1px rgba(255,159,10,.3); }\n        .stream-badge.offline    { background: rgba(255,69,58,.15); color: #ff453a; }\n        .stream-badge.offline .dot { background: #ff453a; }\n        .stream-badge .dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }\n        .stream-badge.idle .dot       { background: #636366; }\n        .stream-badge.streaming .dot  { background: #0a84ff; animation: pulse 1.5s infinite; }\n        .stream-badge.connecting .dot { background: #ff9f0a; animation: pulse 0.8s infinite; }\n        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }\n\n        /* iOS HLS info banner — sits absolutely at the top of the camera\n           area so it stays readable over the video frame, never on the\n           letterbox bars (which are pure black and gave 0 contrast). */\n        .ios-hls-banner {\n          display: none;\n          position: absolute; top: 8px; left: 8px; right: 8px;\n          z-index: 5;\n          align-items: center; justify-content: center;\n          gap: 6px; padding: 5px 10px;\n          background: rgba(0,0,0,.6); backdrop-filter: blur(6px);\n          -webkit-backdrop-filter: blur(6px);\n          border: 1px solid rgba(255,255,255,.15);\n          border-radius: 8px;\n          font-size: 12px; font-weight: 500; color: #fff;\n          pointer-events: none;\n          text-shadow: 0 1px 2px rgba(0,0,0,.5);\n        }\n        .ios-hls-banner.visible { display: flex; }\n        .ios-hls-banner span { white-space: nowrap; }\n\n        /* Tap-to-play overlay — shown when Android WebView blocks autoplay\n           (HA app "Autoplay videos" setting is off). z-index 9 = above video,\n           below loading-overlay (10). */\n        .tap-to-play-overlay {\n          display: none;\n          position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 9;\n          flex-direction: column; align-items: center; justify-content: center;\n          gap: 10px;\n          background: rgba(0,0,0,.55);\n          backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);\n          cursor: pointer;\n        }\n        .tap-to-play-overlay.visible { display: flex; }\n        .tap-to-play-overlay svg {\n          width: 56px; height: 56px; fill: rgba(255,255,255,.9);\n          filter: drop-shadow(0 2px 8px rgba(0,0,0,.5));\n        }\n        .tap-to-play-overlay .ttp-label {\n          font-size: 13px; font-weight: 500; color: rgba(255,255,255,.85);\n          text-shadow: 0 1px 3px rgba(0,0,0,.6);\n        }\n        .tap-to-play-overlay .ttp-hint {\n          font-size: 11px; color: rgba(255,255,255,.5);\n          text-align: center; max-width: 200px; line-height: 1.4;\n        }\n\n        /* Auto-play gate — shown when auto_play_default decides the user\n           should explicitly tap to reveal the live video. z-index 11 sits\n           above the video (1) and the tap-to-play overlay (9) but BELOW\n           loading-overlay (10) — except loading is hidden while the gate\n           is active so this is a non-issue. The snapshot remains visible\n           through a translucent backdrop so the user sees which camera. */\n        .auto-play-gate {\n          display: none;\n          position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 11;\n          flex-direction: column; align-items: center; justify-content: center;\n          gap: 8px;\n          /* No backdrop-filter — Thomas wants to see the sharp snapshot\n             behind the play button so he can decide based on the current\n             camera image. Dimming only via low-opacity black overlay. */\n          background: rgba(0,0,0,.25);\n          cursor: pointer;\n          transition: background 0.15s;\n        }\n        .auto-play-gate.visible { display: flex; }\n        .auto-play-gate:hover { background: rgba(0,0,0,.4); }\n        /* Hide the HLS-fallback banner while the play gate is up — the\n           transport hint is irrelevant until the user actually starts\n           the stream, just clutters the view. */\n        .img-wrapper:has(.auto-play-gate.visible) .ios-hls-banner {\n          display: none !important;\n        }\n        .auto-play-gate svg {\n          width: 64px; height: 64px; fill: rgba(255,255,255,.95);\n          filter: drop-shadow(0 2px 12px rgba(0,0,0,.6));\n          transition: transform 0.12s;\n        }\n        .auto-play-gate:active svg { transform: scale(0.92); }\n        .auto-play-gate .apg-label {\n          font-size: 15px; font-weight: 600; color: rgba(255,255,255,.95);\n          text-shadow: 0 1px 4px rgba(0,0,0,.7);\n        }\n        .auto-play-gate .apg-hint {\n          font-size: 11px; color: rgba(255,255,255,.7);\n          text-align: center; max-width: 240px; line-height: 1.4;\n          text-shadow: 0 1px 3px rgba(0,0,0,.6);\n        }\n\n        /* Push status badge */\n        .push-badge {\n          display: inline-flex; align-items: center; gap: 4px;\n          font-size: 10px; font-weight: 600; letter-spacing: .3px;\n          text-transform: uppercase; padding: 2px 6px; border-radius: 12px;\n          white-space: nowrap;\n        }\n        .push-badge.push  { background: rgba(48,209,88,.15); color: #30d158; }\n        .push-badge.poll { background: rgba(99,99,102,.2); color: #8e8e93; }\n        .push-badge .pdot { width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0; }\n        .push-badge.push .pdot  { background: #30d158; }\n        .push-badge.poll .pdot { background: #636366; }\n\n        /* Connection type badge (LAN / Cloud) */\n        .conn-badge {\n          display: inline-flex; align-items: center; gap: 4px;\n          font-size: 10px; font-weight: 600; letter-spacing: .3px;\n          padding: 2px 7px; border-radius: 12px; white-space: nowrap;\n        }\n        .conn-badge.local  { background: rgba(48,209,88,.15); color: #30d158; }\n        .conn-badge.remote { background: rgba(99,99,102,.2); color: #8e8e93; }\n        .conn-badge.hidden { display: none; }\n\n        /* Camera image area */\n        .img-wrapper { position: relative; width: 100%; background: #000; line-height: 0; aspect-ratio: 16/9; }\n        .cam-img {\n          width: 100%; height: 100%; display: block; object-fit: cover;\n          min-height: 160px; transition: opacity 0.3s;\n        }\n        .cam-img.hidden { opacity: 0; }\n\n        /* Live video element — absolute so it overlays the snapshot image\n           without layout shift. Image stays visible underneath until video\n           fires "playing" event, avoiding the black gap. */\n        .cam-video {\n          position: absolute; top: 0; right: 0; bottom: 0; left: 0;\n          width: 100%; height: 100%; display: block; object-fit: cover;\n          min-height: 160px; background: transparent;\n        }\n\n        /* Image rotation 180° (ceiling-mounted indoor cameras).\n           Pure CSS transform — zero CPU, zero latency, GPU-composited.\n           Toggled by the integration's switch.<base>_bild_180_drehen entity.\n           Only the <video> is rotated here: the <img> is loaded from\n           /api/camera_proxy/, which is already rotated server-side by\n           camera.async_camera_image() (PIL) — rotating it again would\n           cancel out and the dashboard snapshot would look upright. */\n        .img-wrapper.rotated-180 .cam-video {\n          transform: rotate(180deg);\n        }\n\n        /* Fullscreen — native API (desktop/Android) */\n        .img-wrapper:fullscreen,\n        .img-wrapper:-webkit-full-screen {\n          background: #000;\n          display: flex; align-items: center; justify-content: center;\n          width: 100vw; height: 100vh;\n        }\n        .img-wrapper:fullscreen .cam-img,\n        .img-wrapper:-webkit-full-screen .cam-img,\n        .img-wrapper:fullscreen .cam-video,\n        .img-wrapper:-webkit-full-screen .cam-video {\n          width: 100vw; height: 100vh;\n          object-fit: contain; min-height: unset;\n        }\n        /* Fullscreen — CSS fallback for iOS Safari (position:fixed overlay) */\n        :host(.fs-active) {\n          position: fixed !important; top: 0 !important; right: 0 !important; bottom: 0 !important; left: 0 !important;\n          z-index: 9999 !important; background: #000 !important;\n          display: flex !important; align-items: center !important; justify-content: center !important;\n        }\n        /* Hide header, controls and other elements in fullscreen */\n        :host(.fs-active) .header,\n        :host(.fs-active) .info-row,\n        :host(.fs-active) .btn-row,\n        :host(.fs-active) .switch-rows,\n        :host(.fs-active) .quality-section,\n        :host(.fs-active) .accordion { display: none !important; }\n        :host(.fs-active) .img-wrapper { aspect-ratio: unset; width: 100vw; height: 100vh; }\n        :host(.fs-active) .cam-img,\n        :host(.fs-active) .cam-video { object-fit: contain; min-height: unset; }\n        :host(.fs-active) ha-card { width: 100vw; height: 100vh; border-radius: 0 !important; overflow: hidden; }\n        :host(.fs-active) .cam-img,\n        :host(.fs-active) .cam-video { width: 100vw; height: 100vh; object-fit: contain; min-height: unset; }\n        /* Keep Apple-style overlays on top of everything in fullscreen so\n           they remain reachable for tap-to-exit and toggle clicks. Browser\n           chromes (especially iOS) push the video layer aggressively to the\n           foreground; the explicit high z-index ensures the glass pill +\n           pill-bar stay above without changing layout. */\n        :host(.fs-active) .ap-top,\n        :host(.fs-active) .ap-pill-bar,\n        .img-wrapper:fullscreen .ap-top,\n        .img-wrapper:fullscreen .ap-pill-bar,\n        .img-wrapper:-webkit-full-screen .ap-top,\n        .img-wrapper:-webkit-full-screen .ap-pill-bar { z-index: 10000; }\n\n        /* Motion zones SVG overlay */\n        .motion-zones-overlay {\n          position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 5;\n          width: 100%; height: 100%;\n          pointer-events: none; opacity: 0;\n          transition: opacity 0.3s;\n        }\n        .motion-zones-overlay.visible { opacity: 1; }\n        .motion-zones-overlay rect {\n          fill: rgba(0, 122, 255, 0.15);\n          stroke: rgba(0, 122, 255, 0.6);\n          stroke-width: 0.5;\n        }\n        .motion-zones-overlay rect:nth-child(2) { fill: rgba(52, 199, 89, 0.15); stroke: rgba(52, 199, 89, 0.6); }\n        .motion-zones-overlay rect:nth-child(3) { fill: rgba(255, 159, 10, 0.15); stroke: rgba(255, 159, 10, 0.6); }\n        .motion-zones-overlay rect:nth-child(4) { fill: rgba(255, 69, 58, 0.15); stroke: rgba(255, 69, 58, 0.6); }\n        .motion-zones-overlay rect:nth-child(5) { fill: rgba(175, 82, 222, 0.15); stroke: rgba(175, 82, 222, 0.6); }\n        /* Gen2 polygon zones use per-zone colors from API */\n        .motion-zones-overlay polygon { fill-opacity: 0.15; stroke-width: 2; stroke-opacity: 0.6; }\n        /* Privacy mask SVG overlay */\n        .privacy-mask-overlay {\n          position: absolute; top: 0; left: 0; width: 100%; height: 100%;\n          pointer-events: none; z-index: 5;\n          opacity: 0; transition: opacity 0.3s;\n        }\n        .privacy-mask-overlay.visible { opacity: 1; }\n        .privacy-mask-overlay rect, .privacy-mask-overlay polygon {\n          fill: rgba(0, 0, 0, 0.5); stroke: rgba(0, 0, 0, 0.8); stroke-width: 1.5;\n        }\n\n        /* Loading overlay — must be above both cam-img and cam-video */\n        .loading-overlay {\n          position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 10;\n          display: flex; flex-direction: column; align-items: center; justify-content: center;\n          background: rgba(0,0,0,.85);\n          gap: 12px;\n          opacity: 0; transition: opacity 0.3s; pointer-events: none;\n        }\n        .loading-overlay.visible { opacity: 1; pointer-events: auto; }\n        /* Semi-transparent overlay when refreshing an existing image — old image stays visible, spinner on top */\n        .loading-overlay.refreshing { background: rgba(0,0,0,.4); }\n        /* SVG spinner with SMIL <animateTransform> — replaces the CSS @keyframes\n           div-spinner because iOS Safari + HA mobile WebView were rendering the\n           CSS-animated rotation as static (animation paused on opacity:0→1\n           parent transition inside shadow DOM). SMIL animations run independently\n           of CSS animation scheduling and work reliably across all WebKit versions. */\n        .spinner {\n          width: 36px; height: 36px;\n          flex: 0 0 auto;\n          display: block;\n        }\n        .loading-text {\n          font-size: 13px; color: rgba(255,255,255,.75); font-weight: 500;\n        }\n        .loading-hint {\n          font-size: 11px; color: rgba(255,255,255,.5); font-weight: 400;\n          margin-top: 4px; display: block; text-align: center; max-width: 220px;\n        }\n        .loading-hint:empty { display: none; }\n\n        /* Offline overlay — shown when status sensor is OFFLINE */\n        .offline-overlay {\n          position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 8;\n          display: none;\n          flex-direction: column; align-items: center; justify-content: center;\n          background: rgba(20, 20, 20, 0.82);\n          backdrop-filter: grayscale(100%) blur(3px);\n          -webkit-backdrop-filter: grayscale(100%) blur(3px);\n          gap: 10px;\n          pointer-events: none;\n          animation: offline-pulse 3s ease-in-out infinite;\n        }\n        .offline-overlay.visible { display: flex; }\n        @keyframes offline-pulse {\n          0%, 100% { background: rgba(20, 20, 20, 0.78); }\n          50%      { background: rgba(40, 20, 20, 0.88); }\n        }\n        .offline-overlay svg {\n          width: 48px; height: 48px;\n          stroke: #ff453a; stroke-width: 2; fill: none;\n          filter: drop-shadow(0 0 8px rgba(255, 69, 58, 0.5));\n        }\n        .offline-overlay .offline-title {\n          font-size: 18px; font-weight: 700; color: #ff453a;\n          letter-spacing: 1px; text-transform: uppercase;\n          text-shadow: 0 0 10px rgba(255, 69, 58, 0.4);\n        }\n        .offline-overlay .offline-subtitle {\n          font-size: 12px; color: rgba(255,255,255,.7);\n          font-weight: 400; max-width: 80%; text-align: center; line-height: 1.4;\n        }\n\n        /* Auth/integration overlay — shown when camera entity is unavailable\n           (coordinator failed, e.g. Bosch Cloud refresh token rejected) */\n        .auth-overlay {\n          position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 9;\n          display: none;\n          flex-direction: column; align-items: center; justify-content: center;\n          background: rgba(20, 20, 20, 0.88);\n          backdrop-filter: blur(4px);\n          -webkit-backdrop-filter: blur(4px);\n          gap: 14px;\n          pointer-events: auto;\n        }\n        .auth-overlay.visible { display: flex; }\n        .auth-overlay svg {\n          width: 48px; height: 48px;\n          stroke: #ff9f0a; stroke-width: 2; fill: none;\n          filter: drop-shadow(0 0 8px rgba(255, 159, 10, 0.5));\n        }\n        .auth-overlay .auth-title {\n          font-size: 16px; font-weight: 700; color: #ff9f0a;\n          letter-spacing: 0.5px; text-align: center;\n          text-shadow: 0 0 10px rgba(255, 159, 10, 0.35);\n        }\n        .auth-overlay .auth-subtitle {\n          font-size: 12px; color: rgba(255,255,255,.75);\n          font-weight: 400; max-width: 85%; text-align: center; line-height: 1.45;\n        }\n        .auth-overlay .auth-btn {\n          margin-top: 4px;\n          padding: 8px 18px;\n          background: #ff9f0a; color: #1a1a1a;\n          border: none; border-radius: 8px;\n          font-size: 13px; font-weight: 600;\n          cursor: pointer;\n          text-decoration: none;\n          transition: filter .15s;\n        }\n        .auth-overlay .auth-btn:hover { filter: brightness(1.1); }\n        .auth-overlay .auth-btn:active { filter: brightness(0.9); }\n\n        /* Image overlay (last event / events today) */\n        .img-overlay {\n          position: absolute; bottom: 0; left: 0; right: 0;\n          padding: 20px 12px 8px;\n          background: linear-gradient(transparent, rgba(0,0,0,.55));\n          display: flex; align-items: flex-end; justify-content: space-between;\n          pointer-events: none;\n        }\n        .last-event-overlay, .events-overlay { font-size: 11px; color: rgba(255,255,255,.8); }\n\n        /* Info row */\n        .info-row {\n          display: flex; align-items: center; justify-content: space-between;\n          padding: 8px 14px; gap: 10px;\n        }\n        .info-item { display: flex; flex-direction: column; gap: 1px; min-width: 0; }\n        .info-label {\n          font-size: 10px; text-transform: uppercase; letter-spacing: .5px;\n          color: var(--secondary-text-color, #8e8e93);\n        }\n        .info-value {\n          font-size: 13px; color: var(--primary-text-color, #e5e5ea);\n          white-space: nowrap; overflow: hidden; text-overflow: ellipsis;\n        }\n\n        /* Buttons */\n        .btn-row { display: flex; gap: 8px; padding: 8px 12px 12px; }\n        .btn {\n          flex: 1; display: flex; align-items: center; justify-content: center;\n          gap: 6px; padding: 9px 10px; border-radius: 10px; border: none;\n          cursor: pointer; font-size: 13px; font-weight: 500; font-family: inherit;\n          transition: opacity 0.15s, transform 0.1s;\n          -webkit-tap-highlight-color: transparent;\n        }\n        .btn:active { transform: scale(.97); opacity: .8; }\n        .btn:disabled { opacity: .5; cursor: default; }\n        .btn-snapshot { background: rgba(99,99,102,.2); color: var(--primary-text-color, #e5e5ea); }\n        .btn-snapshot.loading { background: rgba(99,99,102,.35); }\n        .btn-stream    { background: rgba(10,132,255,.18); color: #0a84ff; }\n        .btn-stream.active { background: rgba(255,69,58,.18); color: #ff453a; }\n        .btn-fullscreen { background: rgba(99,99,102,.15); color: var(--secondary-text-color, #8e8e93); flex: 0 0 auto; padding: 9px 12px; }\n        .btn-privacy-inline { background: rgba(99,99,102,.15); color: var(--secondary-text-color, #8e8e93); flex: 0 0 auto; padding: 9px 12px; display: none; }\n        .btn-privacy-inline.on { background: rgba(255,69,58,.18); color: #ff453a; }\n        :host(.minimal) .btn-privacy-inline { display: inline-flex; }\n        :host(.minimal) .switch-rows > .privacy-row { display: none; }\n        .btn-overflow { background: rgba(99,99,102,.15); color: var(--secondary-text-color, #8e8e93); flex: 0 0 auto; padding: 9px 12px; display: none; }\n        :host(.minimal) .btn-overflow { display: inline-flex; }\n        :host(.minimal.overflow-open) .btn-overflow { background: rgba(10,132,255,.18); color: #0a84ff; }\n\n        /* Minimal layout: hide everything non-essential until user taps ⋮.\n         * Visible baseline: image, btn-row (Snapshot/Stream/⋮/Vollbild),\n         * Privacy toggle. The overflow-open class (toggled by the ⋮ button) re-\n         * reveals the hidden sections as a single flat panel — no separate popup\n         * needed, just a progressive disclosure of existing controls. */\n        :host(.minimal) .info-row { display: none; }\n        :host(.minimal) .switch-rows { display: none; }\n        :host(.minimal) .btn-row { padding-bottom: 8px; }\n        :host(.minimal) .accordion,\n        :host(.minimal) .pan-row,\n        :host(.minimal) .pan-slider-row,\n        :host(.minimal) .automation-row { display: none; }\n        :host(.minimal.overflow-open) .info-row { display: flex; }\n        :host(.minimal.overflow-open) .switch-rows { display: flex; padding: 0 12px 12px; }\n        :host(.minimal.overflow-open) .switch-rows > .sw-row { display: flex; }\n        :host(.minimal.overflow-open) .accordion,\n        :host(.minimal.overflow-open) .pan-row,\n        :host(.minimal.overflow-open) .pan-slider-row,\n        :host(.minimal.overflow-open) .automation-row { display: block; }\n        :host(.minimal.overflow-open) .pan-row { display: flex; }\n        .btn svg { width: 16px; height: 16px; flex-shrink: 0; }\n        .btn-spinner {\n          width: 14px; height: 14px;\n          border: 2px solid rgba(255,255,255,.3);\n          border-top-color: currentColor;\n          border-radius: 50%;\n          animation: spin 0.8s linear infinite;\n          flex-shrink: 0;\n        }\n\n        /* Switch rows — Ton / Licht / Privat */\n        .switch-rows { display: flex; flex-direction: column; padding: 0 12px 12px; gap: 2px; }\n        .sw-row {\n          display: flex; align-items: center; justify-content: space-between;\n          padding: 9px 4px; cursor: pointer; border-radius: 8px;\n          -webkit-tap-highlight-color: transparent;\n          transition: background 0.15s;\n        }\n        .sw-row:active { background: rgba(99,99,102,.12); }\n        .sw-left {\n          display: flex; align-items: center; gap: 10px;\n          color: var(--primary-text-color, #e5e5ea); font-size: 13px; font-weight: 500;\n        }\n        .sw-left svg { width: 18px; height: 18px; flex-shrink: 0; color: var(--secondary-text-color, #8e8e93); }\n        .sw-row.on .sw-left svg { color: #0a84ff; }\n        .sw-row.privacy-row.on .sw-left svg { color: #ff453a; }\n        /* iOS-style toggle */\n        .sw-toggle {\n          width: 44px; height: 26px; border-radius: 13px;\n          background: rgba(99,99,102,.4); border: none; padding: 0;\n          position: relative; flex-shrink: 0; cursor: pointer;\n          transition: background 0.25s;\n        }\n        .sw-row.on    .sw-toggle { background: #30d158; }\n        /* Audio "tap for sound" hint (issue #22): the stream starts muted by the\n           browser autoplay policy, so the Ton row reads off until a tap unmutes.\n           A gentle pulse on the toggle draws the eye to it. */\n        .sw-row.tap-hint .sw-toggle { animation: bosch-audio-hint 1.6s ease-in-out infinite; }\n        @keyframes bosch-audio-hint {\n          0%, 100% { box-shadow: 0 0 0 0 rgba(255,159,10,0); }\n          50%      { box-shadow: 0 0 0 3px rgba(255,159,10,.4); }\n        }\n        .sw-row.privacy-row.on .sw-toggle { background: #ff453a; }\n        .sw-thumb {\n          width: 22px; height: 22px; border-radius: 50%; background: #fff;\n          position: absolute; top: 2px; left: 2px;\n          box-shadow: 0 1px 4px rgba(0,0,0,.4);\n          transition: transform 0.25s cubic-bezier(.4,0,.2,1);\n        }\n        .sw-row.on .sw-thumb { transform: translateX(18px); }\n\n        /* Pending: request in flight — subtle fade while waiting for HA/Bosch confirm */\n        .sw-row.pending,\n        .btn.pending { opacity: 0.7; }\n        .sw-row.pending .sw-toggle,\n        .btn.pending { animation: pendingPulse 1.2s ease-in-out infinite; }\n        @keyframes pendingPulse { 0%,100%{filter:brightness(1)} 50%{filter:brightness(0.75)} }\n        /* Error: 2s red outline + short shake to signal failed service call */\n        .sw-row.error,\n        .btn.error { animation: errorFlash 0.6s ease-in-out 0s 3; box-shadow: 0 0 0 2px rgba(255,69,58,.55); }\n        @keyframes errorFlash {\n          0%,100% { box-shadow: 0 0 0 2px rgba(255,69,58,.55); }\n          50%     { box-shadow: 0 0 0 3px rgba(255,69,58,.15); }\n        }\n\n        /* Privacy placeholder — shown when no image + privacy mode is ON.\n           Was rgba(0,0,0,.82) which read as a hard black wall over the\n           camera. Mid-tone .55 + a subtle backdrop blur lets a hint of the\n           dimmed camera image show through, signalling "privacy on but the\n           camera is fine" rather than "this view is dead". */\n        .privacy-placeholder {\n          position: absolute; top: 0; right: 0; bottom: 0; left: 0;\n          display: flex; flex-direction: column; align-items: center; justify-content: center;\n          background: rgba(20,20,22,.55);\n          backdrop-filter: blur(8px);\n          -webkit-backdrop-filter: blur(8px);\n          gap: 10px;\n          opacity: 0; transition: opacity 0.3s; pointer-events: none;\n        }\n        .privacy-placeholder.visible { opacity: 1; }\n        .privacy-placeholder svg { width: 44px; height: 44px; color: rgba(255,255,255,.5); }\n        .privacy-placeholder span { font-size: 13px; color: rgba(255,255,255,.6); font-weight: 500; }\n        /* Day mode: lighter overlay with darker glyph for legibility */\n        :host(.apple-style.mode-day) .privacy-placeholder {\n          background: rgba(240,240,242,.6);\n        }\n        :host(.apple-style.mode-day) .privacy-placeholder svg { color: rgba(28,28,30,.55); }\n        :host(.apple-style.mode-day) .privacy-placeholder span { color: rgba(28,28,30,.65); }\n\n        /* Quality select */\n        .quality-section { padding: 0 12px 12px; }\n        .quality-row { display: flex; align-items: center; gap: 10px; }\n        .quality-label { font-size: 13px; color: var(--secondary-text-color, #8e8e93); flex-shrink: 0; }\n        .quality-select {\n          flex: 1; background: rgba(255,255,255,.1); border: 1px solid rgba(255,255,255,.12);\n          border-radius: 8px; color: var(--primary-text-color, #e5e5ea); font-size: 13px;\n          padding: 6px 10px; cursor: pointer; font-family: inherit;\n          -webkit-appearance: none; appearance: none;\n        }\n        .quality-select:focus { outline: none; background: rgba(255,255,255,.15); }\n        .quality-select option { background: #2c2c2e; color: #e5e5ea; }\n\n        /* Pan controls */\n        .pan-section { padding: 0 12px 12px; }\n        .pan-row { display: flex; align-items: center; gap: 6px; }\n        .pan-btn {\n          background: rgba(128,128,128,.15); border: none; border-radius: 6px;\n          color: var(--primary-text-color, #333); cursor: pointer; padding: 6px 10px; flex: 1;\n          font-family: inherit; -webkit-tap-highlight-color: transparent;\n          transition: background 0.15s;\n          display: flex; align-items: center; justify-content: center;\n        }\n        .pan-btn svg { width: 18px; height: 18px; flex-shrink: 0; }\n        .pan-btn:hover  { background: rgba(128,128,128,.25); }\n        .pan-btn:active { background: rgba(128,128,128,.35); }\n        .pan-pos { margin-left: auto; font-size: 12px; opacity: .7; color: var(--primary-text-color, #e5e5ea); white-space: nowrap; }\n\n        /* Accordion sections */\n        .accordion { border-top: 1px solid rgba(255,255,255,.06); }\n        .accordion-header {\n          display: flex; align-items: center; justify-content: space-between;\n          padding: 10px 14px; cursor: pointer;\n          -webkit-tap-highlight-color: transparent;\n          transition: background 0.15s;\n        }\n        .accordion-header:active { background: rgba(99,99,102,.08); }\n        .accordion-title {\n          font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .5px;\n          color: var(--secondary-text-color, #8e8e93);\n        }\n        .accordion-chevron {\n          width: 16px; height: 16px; color: var(--secondary-text-color, #8e8e93);\n          transition: transform 0.25s ease;\n          flex-shrink: 0;\n        }\n        .accordion.open .accordion-chevron { transform: rotate(180deg); }\n        .accordion-body {\n          max-height: 0; overflow: hidden;\n          transition: max-height 0.3s ease;\n        }\n        .accordion.open .accordion-body { max-height: 600px; }\n        .accordion-content { padding: 0 12px 12px; }\n        .accordion-content .sw-row { padding: 7px 4px; }\n\n        /* Service grid inside accordion */\n        .svc-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; padding: 4px 0; }\n        .svc-btn { display: flex; align-items: center; gap: 6px; padding: 8px 10px; border-radius: 8px; border: 1px solid rgba(255,255,255,.1); background: rgba(255,255,255,.03); color: var(--primary-text-color, #e1e1e1); font-size: 11px; cursor: pointer; transition: background .15s; }\n        .svc-btn:hover { background: rgba(255,255,255,.08); }\n        .svc-btn:active { background: rgba(255,255,255,.12); }\n        .svc-btn svg { width: 16px; height: 16px; flex-shrink: 0; }\n        .svc-btn.running { opacity: 0.5; pointer-events: none; }\n        /* Rule row inside accordion */\n        .rule-row { display: flex; align-items: center; justify-content: space-between; padding: 5px 4px; font-size: 12px; border-bottom: 1px solid rgba(255,255,255,.04); }\n        .rule-row .rule-info { flex: 1; min-width: 0; }\n        .rule-row .rule-name { font-weight: 500; color: var(--primary-text-color, #e1e1e1); }\n        .rule-row .rule-time { color: #999; font-size: 11px; }\n        .rule-row .rule-days { color: #888; font-size: 10px; }\n        .rule-row .rule-toggle { cursor: pointer; padding: 2px 8px; border-radius: 4px; border: 1px solid rgba(255,255,255,.15); background: transparent; color: #999; font-size: 11px; margin-left: 6px; }\n        .rule-row .rule-toggle.active { background: rgba(52,199,89,.15); color: #34c759; border-color: rgba(52,199,89,.3); }\n        .rule-row .rule-delete { cursor: pointer; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(255,59,48,.2); background: transparent; color: #666; font-size: 11px; margin-left: 4px; }\n        .rule-row .rule-delete:hover { background: rgba(255,59,48,.15); color: #ff3b30; }\n        /* Diagnostic row inside accordion */\n        .diag-row {\n          display: flex; align-items: center; justify-content: space-between;\n          padding: 6px 4px;\n        }\n        .diag-label {\n          font-size: 13px; color: var(--secondary-text-color, #8e8e93);\n          display: flex; align-items: center; gap: 8px;\n        }\n        .diag-label svg { width: 16px; height: 16px; flex-shrink: 0; }\n        .diag-value {\n          font-size: 13px; color: var(--primary-text-color, #e5e5ea); font-weight: 500;\n        }\n      </style>\n\n      <style>\n        /* ========================================================\n         * Apple-style overlay layer (v13.0.0)\n         * Active only when host has .apple-style class. Adds:\n         *   - glass title-pill + status badge overlaying top of video\n         *   - glass pill-bar with circular buttons overlaying bottom of video\n         *   - hides legacy .header / .info-row / .btn-row\n         * ====================================================== */\n        :host(.apple-style) ha-card {\n          /* Card-specific vars (issue #21) — NOT the global --ha-card-* theme\n             tokens. A user whose dashboard theme zeroes --ha-card-border-radius\n             must still get the apple-style rounding by default; the optional\n             border_radius / box_shadow card config sets --bosch-card-* to\n             opt into a custom look without us inheriting the global theme value. */\n          border-radius: var(--bosch-card-radius, var(--ha-card-border-radius, 22px));\n          box-shadow: var(--bosch-card-shadow, var(--ha-card-box-shadow, 0 4px 24px rgba(0,0,0,.08), 0 1px 3px rgba(0,0,0,.06)));\n          border-width: var(--ha-card-border-width, 0);\n        }\n        @media (prefers-color-scheme: dark) {\n          :host(.apple-style) ha-card {\n            box-shadow: var(--bosch-card-shadow, var(--ha-card-box-shadow, 0 6px 28px rgba(0,0,0,.55), 0 1px 3px rgba(0,0,0,.4)));\n          }\n        }\n        /* Hover affordance parity with the overview tiles (issue #15.1): lift +\n           a subtle scale on pointer devices, like the grid tiles. transform-origin\n           anchors the top edge so the card grows downward (no jump). Uses transform\n           only — NOT box-shadow — so a themed --ha-card-box-shadow stays visible on\n           hover (RkcCorian, issue #15/#21). */\n        :host(.apple-style) ha-card { transition: transform .18s ease; transform-origin: top center; }\n        @media (hover: hover) and (pointer: fine) {\n          :host(.apple-style) ha-card:hover { transform: translateY(-2px) scale(1.01); }\n        }\n        :host(.apple-style) .header,\n        :host(.apple-style) .info-row,\n        :host(.apple-style) .btn-row { display: none !important; }\n        /* Legacy on-video text overlays ("Letztes: ..." / "30 Events heute")\n           clash with the glass title-pill + status badge. The same info now\n           lives in the Apple-style overlays, so suppress the old layer. */\n        :host(.apple-style) .img-overlay { display: none !important; }\n\n        /* In Apple mode, switch-rows + accordions collapse via max-height\n           transition (smooth slide) instead of hard display:none → block.\n           display:none breaks the transition; max-height:0 + overflow:hidden\n           achieves the same visual hiding while remaining animatable. */\n        :host(.apple-style) .switch-rows,\n        :host(.apple-style) .accordion,\n        :host(.apple-style) .pan-row {\n          max-height: 0;\n          overflow: hidden;\n          opacity: 0;\n          /* max-height:0 does NOT clip padding/borders (content-box), so the\n             switch-rows' 12px bottom padding + each accordion's 1px divider\n             rendered as a white strip below the video when collapsed (issue:\n             white gap, 2026-05-29). Zero them while collapsed; restore on open. */\n          padding-top: 0;\n          padding-bottom: 0;\n          border-top-width: 0;\n          border-bottom-width: 0;\n          transition: max-height .35s cubic-bezier(.4,0,.2,1),\n                      opacity .25s ease;\n        }\n        :host(.apple-style.overflow-open) .switch-rows,\n        :host(.apple-style.overflow-open) .accordion,\n        :host(.apple-style.overflow-open) .pan-row {\n          max-height: 2000px;\n          opacity: 1;\n        }\n        :host(.apple-style.overflow-open) .switch-rows { padding: 0 12px 12px; }\n        :host(.apple-style.overflow-open) .accordion { border-top-width: 1px; }\n        /* Default .pan-section { padding: 0 12px 12px } produces a 12 px\n           white bar below the image when pan-row is hidden (apple-style,\n           overflow closed). Drop padding to zero in that state; bring the\n           breathing room back only when the section actually shows content. */\n        :host(.apple-style) .pan-section { padding: 0; }\n        :host(.apple-style.overflow-open) .pan-section { padding: 0 12px 12px; }\n\n        /* Suppress redundant top-right "connecting" badge while the central\n           loading overlay is up — both convey the same state, and the overlay\n           carries the timer/hint ("ca. 25–35 s bis erstes Bild"). Once the\n           overlay hides, the badge re-appears as LIVE / OFFLINE / etc. */\n        :host(.apple-style) .img-wrapper:has(.loading-overlay.visible) .ap-badge.connecting {\n          display: none;\n        }\n\n        /* Glass material primitive ------------------------------- */\n        /* Near-opaque night glass (.92) — earlier mid-tone .42/.55 left the\n           backdrop bleeding through, making text + icons washed out during\n           snapshot-loading (bright loading-overlay backdrop) and on bright\n           daylight scenes. Sacrifice some glass-transparency for guaranteed\n           contrast on every backdrop. The blur still gives the soft Material\n           edges where the pill meets the video. Border bumped to 1px so it\n           renders cleanly on high-DPI mobile (.5px collapsed to 0 on some\n           devices, leaving the pill rim invisible). */\n        .ap-glass {\n          background: rgba(22,22,24,.92);\n          backdrop-filter: blur(20px) saturate(1.4);\n          -webkit-backdrop-filter: blur(20px) saturate(1.4);\n          border: 1px solid rgba(255,255,255,.12);\n          color: #fff;\n          box-shadow: 0 2px 8px rgba(0,0,0,.22);\n          /* GPU composite layer — prevents scroll-flicker on iOS WKWebView */\n          transform: translateZ(0);\n          will-change: transform;\n        }\n        /* Mobile WebKit (HA Companion / iOS Safari) doesn't always honour\n           backdrop-filter — fall back to a slightly denser solid tint so the\n           glass pill stays legible without the blur. */\n        @supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {\n          .ap-glass { background: rgba(20,20,22,.72); }\n        }\n\n        /* Top overlay (title pill + status badge) ---------------- */\n        .ap-top {\n          position: absolute; top: 12px; left: 12px; right: 12px;\n          display: flex; align-items: center; justify-content: space-between;\n          gap: 8px; z-index: 6; pointer-events: none;\n        }\n        .ap-top > * { pointer-events: auto; }\n        .ap-title-pill {\n          display: inline-flex; align-items: center; gap: 8px;\n          padding: 8px 14px 8px 11px; border-radius: 999px;\n          font-size: 14px; font-weight: 600;\n          letter-spacing: .005em;\n          max-width: 70%;\n          line-height: 1;\n        }\n        .ap-title-pill .ap-title-text {\n          overflow: hidden; text-overflow: ellipsis; white-space: nowrap;\n          /* No text-shadow — relying on solid pill bg for contrast. Earlier\n             multi-layer shadows + halos washed the glyph on certain mobile\n             renderers. Plain glyph on near-opaque pill is the safe bet. */\n          text-shadow: none;\n          /* Force-visible against fragile mobile renderers — color inherits\n             from .ap-glass / mode-day override but pinning it here means\n             no parent class can accidentally null it out via shorthand. */\n          color: inherit;\n        }\n        .ap-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: #8e8e93; }\n        .ap-dot.online   { background: #30d158; box-shadow: 0 0 0 3px rgba(48,209,88,.22); }\n        /* Privacy = "shielded" via iOS systemPurple — Apple convention for\n           locked / private states. Old .warn (orange) reflexively read as\n           "caution / warning" which mismatches a deliberate privacy state. */\n        .ap-dot.privacy  { background: #af52de; box-shadow: 0 0 0 3px rgba(175,82,222,.22); }\n        .ap-dot.warn     { background: #ff9f0a; box-shadow: 0 0 0 3px rgba(255,159,10,.22); }\n        .ap-dot.offline  { background: #ff453a; }\n\n        .ap-top-right { display: inline-flex; align-items: center; gap: 6px; }\n        .ap-badge {\n          display: inline-flex; align-items: center; gap: 6px;\n          padding: 6px 10px; border-radius: 999px;\n          font-size: 11px; font-weight: 700; letter-spacing: .04em;\n          text-transform: uppercase;\n        }\n        .ap-badge.live {\n          background: rgba(255,59,48,.88); color: #fff;\n          border: 0.5px solid rgba(255,255,255,.22);\n        }\n        .ap-badge.live::before {\n          content: ""; width: 5px; height: 5px; border-radius: 50%;\n          background: #fff; animation: ap-pulse 1.4s ease-in-out infinite;\n        }\n        @keyframes ap-pulse { 0%,100% { opacity: 1 } 50% { opacity: .4 } }\n        .ap-badge.connecting {\n          /* WCAG fix: dark text on amber (was white-on-amber = 2.7:1, fails AA).\n             Dark on amber yields ~11:1, well above threshold. */\n          background: rgba(255,159,10,.95); color: #1a1a1a;\n          border: 0.5px solid rgba(255,255,255,.2);\n        }\n        .ap-badge.offline  { background: rgba(120,120,128,.55); color: #fff; border: 0.5px solid rgba(255,255,255,.18); }\n        .ap-badge.hidden   { display: none; }\n\n        /* Bottom pill-bar overlay -------------------------------- */\n        .ap-pill-bar {\n          position: absolute; left: 50%; bottom: 12px;\n          transform: translateX(-50%);\n          display: inline-flex; align-items: center;\n          gap: 6px; padding: 6px;\n          border-radius: 999px; z-index: 6;\n          max-width: calc(100% - 24px);\n        }\n        .ap-pill-btn {\n          width: 42px; height: 42px; border-radius: 50%;\n          display: inline-flex; align-items: center; justify-content: center;\n          background: rgba(255,255,255,.12);\n          border: 0.5px solid rgba(255,255,255,.18);\n          color: #fff; cursor: pointer;\n          padding: 0; flex-shrink: 0;\n          transition: background .15s ease, transform .12s ease;\n        }\n        .ap-pill-btn:hover { background: rgba(255,255,255,.22); }\n        .ap-pill-btn:active { transform: scale(.92); }\n        .ap-pill-btn svg { width: 19px; height: 19px; fill: #fff; pointer-events: none; }\n        .ap-pill-btn.on { background: rgba(255,255,255,.93); }\n        .ap-pill-btn.on svg { fill: #1c1c1e; }\n        .ap-pill-btn.danger { background: rgba(255,59,48,.85); border-color: rgba(255,255,255,.22); }\n        .ap-pill-btn.danger:hover { background: rgba(255,59,48,1); }\n        .ap-pill-btn.connecting { background: rgba(255,159,10,.85); border-color: rgba(255,255,255,.22); }\n        .ap-pill-btn[hidden] { display: none !important; }\n\n        /* Phone-narrow: keep all buttons visible, shrink slightly */\n        @media (max-width: 380px) {\n          .ap-pill-btn { width: 38px; height: 38px; }\n          .ap-pill-btn svg { width: 17px; height: 17px; }\n          .ap-pill-bar { gap: 4px; padding: 4px; }\n        }\n\n\n        /* Img-wrapper needs relative + own stacking context so the absolute\n           overlays cannot escape upward over the HA tab bar / sidebar when\n           the card is rendered tall in a panel:true view. isolation:isolate\n           creates a new stacking context; contain:paint clips rendering to\n           the wrapper box so partially-scrolled overlays do not bleed past\n           the visible region. (No backticks inside CSS comments — this CSS\n           is itself inside a JS template literal.) */\n        :host(.apple-style) .img-wrapper {\n          border-radius: 0;\n          position: relative;\n          isolation: isolate;\n          contain: paint;\n          overflow: hidden;\n        }\n        /* Belt-and-braces: keep overlay z-index low — the wrapper's new\n           stacking context confines them anyway, but a low value protects\n           against future ancestors that might break isolation. */\n        :host(.apple-style) .ap-top,\n        :host(.apple-style) .ap-pill-bar { z-index: 2; }\n\n        /* ========================================================\n         * Material You (Android / M3) theme overrides\n         * Active when host has .theme-android. Swaps the glass blur for\n         * solid M3 surface tones, bumps the card to the M3 large container\n         * radius (28px), and recolors button states with M3 tonal tokens.\n         * Default theme (.theme-ios) keeps the iOS look above untouched.\n         * ====================================================== */\n        :host(.apple-style.theme-android) ha-card {\n          /* M3 large radius (28px) as the Android default; the optional\n             border_radius card config (--bosch-card-radius) overrides it\n             (issue #21). !important still beats ha-card's base rule. */\n          border-radius: var(--bosch-card-radius, var(--ha-card-border-radius, 28px)) !important;\n        }\n        :host(.apple-style.theme-android) .ap-glass {\n          background: rgba(73, 69, 79, .92);   /* M3 surface-variant dark */\n          backdrop-filter: none;\n          -webkit-backdrop-filter: none;\n          border: 0;\n          color: #E6E0E9;                       /* M3 on-surface dark */\n          box-shadow: 0 1px 3px rgba(0,0,0,.3);\n        }\n        :host(.apple-style.theme-android) .ap-title-pill {\n          border-radius: 8px;                   /* M3 chip shape */\n          font-family: var(--primary-font-family, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif);\n          font-weight: 500;\n        }\n        :host(.apple-style.theme-android) .ap-title-pill .ap-title-text {\n          text-shadow: none;                    /* Solid surface needs no shadow */\n        }\n        :host(.apple-style.theme-android) .ap-badge {\n          border-radius: 8px;\n          font-family: var(--primary-font-family, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif);\n          letter-spacing: 0;\n          font-weight: 500;\n        }\n        :host(.apple-style.theme-android) .ap-badge.live {\n          background: rgba(242, 184, 181, .95); /* M3 error dark tonal */\n          color: #601410;                       /* M3 on-error-container dark */\n          border: 0;\n        }\n        :host(.apple-style.theme-android) .ap-badge.connecting {\n          background: rgba(232, 222, 248, .95); /* M3 secondary-container dark */\n          color: #1D192B;\n          border: 0;\n        }\n        :host(.apple-style.theme-android) .ap-badge.privacy {\n          background: rgba(208, 188, 255, .95); /* M3 primary-container dark */\n          color: #381E72;                       /* M3 on-primary-container dark */\n          border: 0;\n        }\n        :host(.apple-style.theme-android) .ap-badge.offline {\n          background: rgba(73, 69, 79, .92);\n          /* WCAG fix: was #CAC4D0 on surface-variant = 4.1:1 (borderline fail\n             for 11px font). #E6E0E9 = M3 on-surface = ~6.5:1. */\n          color: #E6E0E9;\n          border: 0;\n        }\n        :host(.apple-style.theme-android) .ap-pill-bar {\n          background: rgba(73, 69, 79, .92);\n          backdrop-filter: none;\n          -webkit-backdrop-filter: none;\n          border: 0;\n          border-radius: 28px;                  /* M3 large radius for the bar */\n        }\n        :host(.apple-style.theme-android) .ap-pill-btn {\n          background: transparent;\n          border: 0;\n          color: #E6E0E9;\n        }\n        :host(.apple-style.theme-android) .ap-pill-btn svg { fill: #E6E0E9; }\n        :host(.apple-style.theme-android) .ap-pill-btn:hover {\n          /* M3 state layer: 8% opacity overlay of on-surface */\n          background: rgba(230, 224, 233, .08);\n        }\n        :host(.apple-style.theme-android) .ap-pill-btn:active {\n          /* M3 pressed state: 12% opacity overlay */\n          background: rgba(230, 224, 233, .12);\n        }\n        :host(.apple-style.theme-android) .ap-pill-btn.on {\n          background: #D0BCFF;                  /* M3 primary dark */\n        }\n        :host(.apple-style.theme-android) .ap-pill-btn.on svg { fill: #381E72; }\n        :host(.apple-style.theme-android) .ap-pill-btn.danger {\n          background: #F2B8B5;                  /* M3 error dark */\n        }\n        :host(.apple-style.theme-android) .ap-pill-btn.danger svg { fill: #601410; }\n        :host(.apple-style.theme-android) .ap-pill-btn.connecting {\n          background: #E8DEF8;                  /* M3 secondary-container dark */\n        }\n        :host(.apple-style.theme-android) .ap-pill-btn.connecting svg { fill: #1D192B; }\n        :host(.apple-style.theme-android) .ap-dot.online   { background: #6FE899; box-shadow: 0 0 0 3px rgba(111,232,153,.18); }\n        :host(.apple-style.theme-android) .ap-dot.warn     { background: #FFB68A; box-shadow: 0 0 0 3px rgba(255,182,138,.18); }\n        :host(.apple-style.theme-android) .ap-dot.offline  { background: #F2B8B5; }\n\n        /* Theme switcher row inside the Mehr menu (visible when overflow-open) */\n        .ap-theme-switcher {\n          align-items: center; justify-content: space-between;\n          padding: 12px 14px;\n          font-size: 14px;\n          border-top: 0.5px solid rgba(120,120,128,.18);\n        }\n        .ap-theme-toggle {\n          display: inline-flex; align-items: center; gap: 4px;\n          padding: 3px; border-radius: 999px;\n          background: rgba(120,120,128,.16);\n        }\n        .ap-theme-toggle button {\n          font: inherit; font-size: 13px; font-weight: 500;\n          padding: 6px 14px; border-radius: 999px;\n          background: transparent; border: 0;\n          /* WCAG fix: #8e8e93 on white = 2.85:1 (fails AA). #6c6c70 = ~4.6:1. */\n          color: var(--secondary-text-color, #6c6c70);\n          cursor: pointer;\n          transition: background .15s ease, color .15s ease;\n        }\n        /* currentColor fallback works in both light + dark mode without\n           requiring the user to explicitly set mode-night class. */\n        .ap-theme-toggle button:hover { color: var(--primary-text-color, currentColor); }\n        .ap-theme-toggle button.on {\n          background: var(--card-background-color, #fff);\n          color: var(--primary-text-color, #1c1c1e);\n          box-shadow: 0 1px 2px rgba(0,0,0,.12);\n        }\n        :host(.apple-style.theme-android) .ap-theme-toggle { border-radius: 8px; padding: 2px; }\n        :host(.apple-style.theme-android) .ap-theme-toggle button { border-radius: 8px; }\n        :host(.apple-style.theme-android) .ap-theme-toggle button.on {\n          background: #D0BCFF; color: #381E72;\n        }\n\n        /* ========================================================\n         * Day/Night card-chrome mode (v13.0.1)\n         * .mode-day  -> force light card (white bg, dark text)\n         * .mode-night -> force dark card (M3 dark / iOS systemBackground dark)\n         * No class -> auto, inherit from HA theme CSS vars\n         * Glass overlays on the video are unaffected — they stay dark for\n         * legibility regardless of the chrome mode.\n         * ====================================================== */\n        :host(.apple-style.mode-day) ha-card {\n          background: #ffffff;\n          color: #1c1c1e;\n        }\n        :host(.apple-style.mode-night) ha-card {\n          background: #1c1c1e;\n          color: #ffffff;\n        }\n        /* Android M3 light surface tones when both apple+android+day are on */\n        :host(.apple-style.theme-android.mode-day) ha-card {\n          background: #FEF7FF !important;\n          color: #1D1B20 !important;\n        }\n        :host(.apple-style.theme-android.mode-night) ha-card {\n          background: #211F26 !important;\n          color: #E6E0E9 !important;\n        }\n        /* Force text + secondary-text + divider variables under day mode so\n           switch-row labels, accordion chevrons, slider track edges follow.\n           Night mode also pins the variables explicitly so the user gets a\n           consistent dark card even when HA's active theme is light. */\n        :host(.apple-style.mode-day) {\n          --primary-text-color: #1c1c1e;\n          --secondary-text-color: rgba(60,60,67,.6);\n          --divider-color: rgba(60,60,67,.12);\n          --card-background-color: #ffffff;\n        }\n        :host(.apple-style.mode-night) {\n          --primary-text-color: #ffffff;\n          --secondary-text-color: rgba(235,235,245,.6);\n          --divider-color: rgba(84,84,88,.5);\n          --card-background-color: #1c1c1e;\n        }\n        :host(.apple-style.theme-android.mode-day) {\n          --primary-text-color: #1D1B20;\n          --secondary-text-color: #49454F;\n          --divider-color: rgba(73,69,79,.2);\n          --card-background-color: #FEF7FF;\n        }\n        :host(.apple-style.theme-android.mode-night) {\n          --primary-text-color: #E6E0E9;\n          --secondary-text-color: #CAC4D0;\n          --divider-color: rgba(202,196,208,.2);\n          --card-background-color: #211F26;\n        }\n\n        /* === Day mode lightens the video-overlay glass but keeps the text/\n         *     icons white ===\n         * Earlier attempt at a white-pill in day mode broke text visibility\n         * because dark text on a glass-blended-with-bright-backdrop dropped\n         * below the contrast threshold. Solution: keep text + icons white\n         * (always works on dark glass) but make the glass itself lighter +\n         * more transparent in day so the video shows through and the\n         * overall card feels brighter. Night stays denser/darker. The blur\n         * radius is also higher in day so the lighter glass still feels\n         * like a Material, not a tint film. iOS-day only — :not(.theme-android)\n         * prevents this rule from poaching the Android M3 surface-variant\n         * treatment when both mode-day + theme-android are active. */\n        :host(.apple-style.mode-day:not(.theme-android)) .ap-glass {\n          background: rgba(55,55,60,.42);\n          backdrop-filter: blur(28px) saturate(1.6) brightness(1.05);\n          -webkit-backdrop-filter: blur(28px) saturate(1.6) brightness(1.05);\n          border-color: rgba(255,255,255,.22);\n        }\n        /* The pill-bar's inactive buttons get a brighter inner tint in day\n           so they read clearly as tappable surfaces inside the lighter pill,\n           and the icon stroke gets a touch more weight against the brighter\n           backdrop. Active (.on) buttons stay solid white-tile to read as\n           the primary "selected" state. Danger stays systemRed. */\n        :host(.apple-style.mode-day:not(.theme-android)) .ap-pill-btn {\n          background: rgba(255,255,255,.22);\n          border-color: rgba(255,255,255,.28);\n        }\n        :host(.apple-style.mode-day:not(.theme-android)) .ap-pill-btn:hover { background: rgba(255,255,255,.32); }\n        /* Active "on" button in day mode reads as a raised solid-white tile:\n           full-opacity background, soft drop shadow + thin bright rim, dark\n           icon at full contrast. The combination pops cleanly against the\n           transparent grey pill backdrop without needing a saturated accent\n           colour — matches Apple Home's "selected control" treatment. */\n        :host(.apple-style.mode-day) .ap-pill-btn.on {\n          background: #ffffff;\n          border-color: rgba(255,255,255,.85);\n          box-shadow:\n            0 3px 10px rgba(0,0,0,.32),\n            0 0 0 1px rgba(255,255,255,.5) inset;\n        }\n        :host(.apple-style.mode-day) .ap-pill-btn.on svg { fill: #1c1c1e; }\n        :host(.apple-style.mode-day) .ap-pill-btn.on:hover { background: #ffffff; }\n\n        /* Camera-state ACTIVE buttons: Stream + Privacy get systemRed (a\n           non-neutral hardware state). Light gets amber — a lamp/bulb is\n           conventionally yellow/amber when on (think of every smart-bulb\n           UI ever shipped). Splitting these avoids the audit's "everything\n           red" collision when stream + offline + light are all active at\n           once. Fullscreen.on falls through to the generic white-tile\n           rule above (viewing-mode, not hardware-state). */\n        :host(.apple-style) .ap-pill-btn#ap-btn-stream.on,\n        :host(.apple-style) .ap-pill-btn#ap-btn-privacy.on {\n          background: rgba(255,59,48,.92);\n          border-color: rgba(255,255,255,.22);\n          box-shadow: none;\n        }\n        :host(.apple-style) .ap-pill-btn#ap-btn-stream.on svg,\n        :host(.apple-style) .ap-pill-btn#ap-btn-privacy.on svg { fill: #fff; }\n        :host(.apple-style.mode-day) .ap-pill-btn#ap-btn-stream.on,\n        :host(.apple-style.mode-day) .ap-pill-btn#ap-btn-privacy.on {\n          background: rgba(255,59,48,.95);\n          box-shadow:\n            0 3px 10px rgba(255,59,48,.35),\n            0 0 0 1px rgba(255,255,255,.3) inset;\n        }\n        /* Light = amber (iOS systemYellow / M3 tertiary tonal) — lamp metaphor */\n        :host(.apple-style) .ap-pill-btn#ap-btn-light.on {\n          background: rgba(255,179,0,.92);\n          border-color: rgba(255,255,255,.22);\n          box-shadow: none;\n        }\n        :host(.apple-style) .ap-pill-btn#ap-btn-light.on svg { fill: #1c1c1e; }\n        :host(.apple-style.mode-day) .ap-pill-btn#ap-btn-light.on {\n          background: rgba(255,179,0,.95);\n          box-shadow:\n            0 3px 10px rgba(255,179,0,.4),\n            0 0 0 1px rgba(255,255,255,.4) inset;\n        }\n        /* Android-theme: M3 error tonal for Stream + Privacy, tertiary for Light */\n        :host(.apple-style.theme-android) .ap-pill-btn#ap-btn-stream.on,\n        :host(.apple-style.theme-android) .ap-pill-btn#ap-btn-privacy.on {\n          background: #F2B8B5;\n        }\n        :host(.apple-style.theme-android) .ap-pill-btn#ap-btn-stream.on svg,\n        :host(.apple-style.theme-android) .ap-pill-btn#ap-btn-privacy.on svg { fill: #601410; }\n        :host(.apple-style.theme-android) .ap-pill-btn#ap-btn-light.on {\n          background: #FFD8A8;                  /* M3 tertiary-container dark */\n        }\n        :host(.apple-style.theme-android) .ap-pill-btn#ap-btn-light.on svg { fill: #4F2500; }\n        :host(.apple-style.theme-android.mode-day) .ap-pill-btn#ap-btn-stream.on,\n        :host(.apple-style.theme-android.mode-day) .ap-pill-btn#ap-btn-privacy.on {\n          background: #B3261E;\n        }\n        :host(.apple-style.theme-android.mode-day) .ap-pill-btn#ap-btn-stream.on svg,\n        :host(.apple-style.theme-android.mode-day) .ap-pill-btn#ap-btn-privacy.on svg { fill: #fff; }\n        :host(.apple-style.theme-android.mode-day) .ap-pill-btn#ap-btn-light.on {\n          background: #7D5260;                  /* M3 tertiary light */\n        }\n        :host(.apple-style.theme-android.mode-day) .ap-pill-btn#ap-btn-light.on svg { fill: #fff; }\n        /* Day-mode badge gets the same lighter treatment so it doesn't pop\n           as a saturated solid color against the airy overlay. */\n        :host(.apple-style.mode-day) .ap-badge.live {\n          background: rgba(255,59,48,.85); border-color: rgba(255,255,255,.22);\n        }\n\n        /* Mode switcher row inside the Mehr menu */\n        .ap-mode-switcher {\n          align-items: center; justify-content: space-between;\n          padding: 12px 14px;\n          font-size: 14px;\n          border-top: 0.5px solid var(--divider-color, rgba(120,120,128,.18));\n        }\n        .ap-mode-toggle {\n          display: inline-flex; align-items: center; gap: 4px;\n          padding: 3px; border-radius: 999px;\n          background: rgba(120,120,128,.16);\n        }\n        .ap-mode-toggle button {\n          font: inherit; font-size: 13px; font-weight: 500;\n          padding: 6px 14px; border-radius: 999px;\n          background: transparent; border: 0;\n          /* WCAG fix: matches theme-toggle (was #8e8e93 = 2.85:1, fails AA). */\n          color: var(--secondary-text-color, #6c6c70);\n          cursor: pointer;\n          transition: background .15s ease, color .15s ease;\n        }\n        .ap-mode-toggle button:hover { color: var(--primary-text-color, #1c1c1e); }\n        .ap-mode-toggle button.on {\n          background: var(--card-background-color, #fff);\n          color: var(--primary-text-color, #1c1c1e);\n          box-shadow: 0 1px 2px rgba(0,0,0,.12);\n        }\n        :host(.apple-style.theme-android) .ap-mode-toggle { border-radius: 8px; padding: 2px; }\n        :host(.apple-style.theme-android) .ap-mode-toggle button { border-radius: 8px; }\n        :host(.apple-style.theme-android) .ap-mode-toggle button.on {\n          background: #D0BCFF; color: #381E72;\n        }\n\n        /* === Accessibility + animation polish ============================ */\n        /* Focus-visible: keyboard navigation feedback. systemBlue ring with\n           2px offset on the pill-bar; tighter 1px on the toggle chips so\n           it fits inside the toggle track. */\n        :host(.apple-style) .ap-pill-btn:focus-visible {\n          outline: 2px solid #0a84ff;\n          outline-offset: 2px;\n        }\n        :host(.apple-style) .ap-theme-toggle button:focus-visible,\n        :host(.apple-style) .ap-mode-toggle button:focus-visible {\n          outline: 2px solid #0a84ff;\n          outline-offset: 1px;\n        }\n\n        /* prefers-reduced-motion: suppress all animations + transitions for\n           users with vestibular sensitivity or who set "Reduce Motion" in\n           iOS / macOS Accessibility. WCAG 2.1 SC 2.3.3. */\n        @media (prefers-reduced-motion: reduce) {\n          :host(.apple-style) *,\n          :host(.apple-style) *::before,\n          :host(.apple-style) *::after {\n            animation-duration: 0.01ms !important;\n            animation-iteration-count: 1 !important;\n            transition-duration: 0.01ms !important;\n          }\n        }\n\n        /* prefers-contrast: more (high-contrast OS preference, e.g. macOS\n           "Increase Contrast"). Bumps glass to near-opaque + adds visible\n           hairline borders so the design degrades gracefully. */\n        @media (prefers-contrast: more) {\n          :host(.apple-style) .ap-glass {\n            background: rgba(0,0,0,.95) !important;\n            border: 1.5px solid #fff !important;\n          }\n          :host(.apple-style.mode-day) .ap-glass {\n            background: #fff !important;\n            color: #000 !important;\n            border: 1.5px solid #000 !important;\n          }\n          :host(.apple-style) .ap-pill-btn { border-width: 1.5px !important; }\n        }\n\n        /* Android × Day combined override (higher specificity than the\n           iOS-Day rule) — M3 spec for light mode: solid surface-variant\n           light tint instead of glass blur. */\n        :host(.apple-style.theme-android.mode-day) .ap-glass {\n          background: rgba(231,224,236,.96);    /* M3 surface-variant light */\n          backdrop-filter: none;\n          -webkit-backdrop-filter: none;\n          border: 0;\n          color: #1D1B20;\n          box-shadow: 0 1px 3px rgba(0,0,0,.15);\n        }\n        :host(.apple-style.theme-android.mode-day) .ap-pill-bar {\n          background: rgba(231,224,236,.96);\n        }\n        :host(.apple-style.theme-android.mode-day) .ap-pill-btn { color: #1D1B20; }\n        :host(.apple-style.theme-android.mode-day) .ap-pill-btn svg { fill: #1D1B20; }\n        :host(.apple-style.theme-android.mode-day) .ap-title-pill .ap-title-text { color: #1D1B20; }\n\n        /* Android theme: drop the iOS-style press-scale because M3 design\n           uses a state-layer overlay (radial ripple in spec, opacity-tint\n           in our implementation) rather than the iOS bounce. */\n        :host(.apple-style.theme-android) .ap-pill-btn:active { transform: none; }\n\n        /* Offline cameras (apple-style): minimalist treatment per user\n           preference — just the camera name (already in the top-left glass\n           pill) and a centered "OFFLINE" label + last-seen subtitle. No\n           icons, no pill-bar — there's nothing meaningful to tap when the\n           camera is unreachable. */\n        /* Compact tile mode: hide pill-bar + status badge so the card reduces\n           to just video + title-pill — used by overview grid for Apple-Home-\n           style tile rows. Click on the video opens fullscreen. */\n        :host(.apple-style.compact) .ap-pill-bar,\n        :host(.apple-style.compact) .ap-badge { display: none; }\n\n        /* Last-event indicator: small glass pill bottom-right of the video\n           showing "🕐 14:23" when the camera fired a motion/audio/person\n           event recently. Hidden while streaming live (the LIVE badge takes\n           that real estate). Wired in JS via _updateLastEventBadge(). */\n        .ap-last-event {\n          position: absolute;\n          right: 12px; bottom: 12px;\n          z-index: 4;\n          display: none;\n          align-items: center; gap: 6px;\n          padding: 5px 10px 5px 8px;\n          border-radius: 999px;\n          font-size: 11px; font-weight: 600;\n          background: rgba(22,22,24,.78);\n          backdrop-filter: blur(14px) saturate(1.3);\n          -webkit-backdrop-filter: blur(14px) saturate(1.3);\n          color: #fff;\n          border: .5px solid rgba(255,255,255,.14);\n          pointer-events: none;\n        }\n        .ap-last-event.visible { display: inline-flex; }\n        .ap-last-event svg { width: 12px; height: 12px; fill: currentColor; opacity: .8; }\n        :host(.apple-style.compact) .ap-last-event { right: 8px; bottom: 8px; padding: 4px 8px; font-size: 10px; }\n        /* Hide when streaming is active — the LIVE badge already occupies\n           the visual attention budget; the last-event indicator only adds\n           value during idle/snapshot mode. */\n        :host(.apple-style) .ap-last-event.hide-during-stream { display: none; }\n\n        /* Element-hiding toggles (issue #15): show_title:false / show_last_event:false. */\n        :host(.no-title) .ap-top { display: none; }\n        :host(.no-last-event) .ap-last-event { display: none !important; }\n\n        :host(.apple-style.cam-offline) .ap-pill-bar { display: none; }\n        :host(.apple-style.cam-offline) .offline-overlay svg { display: none; }\n        /* When the camera is offline, the offline-overlay is the single\n           source of truth. Suppress every other overlay that would otherwise\n           stack on top: the privacy-placeholder (last-known privacy state)\n           bleeds through with its own lock icon and "Privat-Modus aktiv"\n           label, and the last-event pill at bottom-right adds another\n           competing piece of chrome. Both hidden to leave only the title\n           pill + OFFLINE label visible. */\n        :host(.apple-style.cam-offline) .privacy-placeholder,\n        :host(.apple-style.cam-offline) .ap-last-event { display: none !important; }\n        /* The offline-overlay already shows the camera name on its own line\n           (.offline-cam-name), so the top-left title pill is redundant when\n           offline. On short/compact tiles the centered "Kamera Offline" pill\n           landed on top of the title pill, superimposing two texts into glyph\n           soup (issue: garbled offline label, 2026-05-29). Hide the top pill\n           when offline — the overlay is the single source of truth. */\n        :host(.apple-style.cam-offline) .ap-top { display: none !important; }\n        /* Offline cameras can't be operated, so in the default EXPANDED layout\n           (minimal NOT enabled) the control stack — switches, light/pan/\n           diagnostics accordions, theme/mode switchers — is just noise. Hide it\n           all, keeping ONLY the Automations accordion (those run HA-side and\n           still work while the camera is down). When minimal IS enabled the\n           whole stack is collapsed behind the ⋮ anyway, so this is scoped to\n           :not(.minimal). (2026-05-29 user feedback: offline shows too much.) */\n        :host(.apple-style.cam-offline:not(.minimal)) .switch-rows,\n        :host(.apple-style.cam-offline:not(.minimal)) .pan-row,\n        :host(.apple-style.cam-offline:not(.minimal)) .pan-section,\n        :host(.apple-style.cam-offline:not(.minimal)) .ap-theme-switcher,\n        :host(.apple-style.cam-offline:not(.minimal)) .ap-mode-switcher,\n        :host(.apple-style.cam-offline:not(.minimal)) .accordion:not(#acc-automations) {\n          display: none !important;\n        }\n        /* Offline overlay: drop the dim red full-cover backdrop so the last\n           cached snapshot stays visible behind. The OFFLINE label + last-seen\n           text sit in a single glass pill centered on the video — same\n           material as the title-pill so the layer reads as a coherent\n           "system overlay" instead of a separate widget. */\n        :host(.apple-style.cam-offline) .offline-overlay {\n          background: transparent;\n          gap: 0;\n          align-items: center; justify-content: center;\n        }\n        :host(.apple-style.cam-offline) .offline-overlay .offline-title,\n        :host(.apple-style.cam-offline) .offline-overlay .offline-subtitle {\n          color: #fff;\n        }\n        :host(.apple-style.cam-offline) .offline-overlay .offline-title {\n          background: rgba(22,22,24,.92);\n          backdrop-filter: blur(20px) saturate(1.4);\n          -webkit-backdrop-filter: blur(20px) saturate(1.4);\n          border: 1px solid rgba(255,255,255,.12);\n          box-shadow: 0 2px 8px rgba(0,0,0,.22);\n          padding: 9px 18px;\n          border-radius: 999px;\n          font-size: 14px;\n          font-weight: 700;\n          letter-spacing: .14em;\n        }\n        :host(.apple-style.cam-offline) .offline-overlay .offline-subtitle {\n          font-size: 11px;\n          margin-top: 8px;\n          opacity: .75;\n          text-shadow: 0 1px 2px rgba(0,0,0,.6);\n        }\n        /* Camera friendly_name on its own line between the OFFLINE pill and\n           the last-seen subtitle. Visible only in apple-style cam-offline\n           state; legacy / non-offline render path stays untouched. */\n        .offline-cam-name { display: none; }\n        :host(.apple-style.cam-offline) .offline-overlay .offline-cam-name {\n          display: block;\n          margin-top: 10px;\n          font-size: 17px;\n          font-weight: 600;\n          letter-spacing: .005em;\n          color: #fff;\n          text-shadow: 0 1px 2px rgba(0,0,0,.6);\n        }\n\n        /* Theme + Mode switcher rows: animate via max-height too so they\n           slide in/out alongside the switch-rows when Mehr is toggled. */\n        :host(.apple-style) .ap-theme-switcher,\n        :host(.apple-style) .ap-mode-switcher {\n          display: flex;\n          max-height: 0;\n          overflow: hidden;\n          opacity: 0;\n          padding-top: 0;\n          padding-bottom: 0;\n          /* 0.5px border-top renders even at max-height:0 → contributes to the\n             white gap below the video. Zero it while collapsed (issue: white\n             gap, 2026-05-29); restore on open. */\n          border-top-width: 0;\n          transition: max-height .35s cubic-bezier(.4,0,.2,1),\n                      opacity .25s ease, padding .25s ease;\n        }\n        :host(.apple-style.overflow-open) .ap-theme-switcher,\n        :host(.apple-style.overflow-open) .ap-mode-switcher {\n          max-height: 80px;\n          opacity: 1;\n          padding-top: 12px;\n          padding-bottom: 12px;\n          border-top-width: 0.5px;\n        }\n\n        /* Snapshot success flash: 280ms green pulse on the snapshot button\n           after a service call returns. Triggered by JS adding .ok-flash. */\n        @keyframes ap-snapshot-flash {\n          0%   { background: rgba(48,209,88,.85); transform: scale(1); }\n          50%  { background: rgba(48,209,88,.95); transform: scale(1.04); }\n          100% { background: rgba(255,255,255,.12); transform: scale(1); }\n        }\n        :host(.apple-style) .ap-pill-btn#ap-btn-snapshot.ok-flash {\n          animation: ap-snapshot-flash .42s ease-out;\n        }\n      </style>\n\n      <ha-card>\n        <div class="header">\n          <div class="header-left">\n            <div class="status-dot unknown" id="status-dot"></div>\n            <span class="title" id="title">Bosch Camera</span>\n          </div>\n          <div style="display:flex;align-items:center;gap:6px;flex-shrink:0;margin-left:auto">\n            <div class="push-badge poll" id="push-badge">\n              <div class="pdot"></div>\n              <span id="push-label">poll</span>\n            </div>\n            <div class="conn-badge hidden" id="conn-badge"></div>\n            <div class="stream-badge idle" id="stream-badge">\n              <div class="dot"></div>\n              <span id="stream-label">idle</span>\n            </div>\n          </div>\n        </div>\n\n        <div class="img-wrapper" id="img-wrapper">\n          <img class="cam-img hidden" id="cam-img" alt="Camera" style="cursor:pointer" />\n          <video class="cam-video" id="cam-video" autoplay muted playsinline webkit-playsinline preload="auto" disableremoteplayback style="display:none; cursor:pointer"></video>\n          <div class="ios-hls-banner" id="ios-hls-banner">\n            <span>ℹ HLS-Modus (kein WebRTC über Tunnel)</span>\n          </div>\n          <div class="tap-to-play-overlay" id="tap-to-play-overlay">\n            <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>\n            <span class="ttp-label">Zum Abspielen tippen</span>\n            <span class="ttp-hint">Oder in den HA-App-Einstellungen „Videos automatisch abspielen" aktivieren</span>\n          </div>\n          <div class="auto-play-gate" id="auto-play-gate">\n            <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>\n            <span class="apg-label">Stream starten</span>\n            <span class="apg-hint">Antippen, um den Live-Stream zu starten</span>\n          </div>\n          <div class="loading-overlay visible" id="loading-overlay">\n            <svg class="spinner" width="36" height="36" viewBox="0 0 40 40" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">\n              <circle cx="20" cy="20" r="16" fill="none" stroke="rgba(255,255,255,.2)" stroke-width="3"/>\n              <circle cx="20" cy="20" r="16" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-dasharray="25 75">\n                <animateTransform attributeName="transform" attributeType="XML" type="rotate" from="0 20 20" to="360 20 20" dur="0.8s" repeatCount="indefinite"/>\n              </circle>\n            </svg>\n            <span class="loading-text" id="loading-text">Bild wird geladen…</span>\n            <span class="loading-hint" id="loading-hint"></span>\n          </div>\n          <div class="offline-overlay" id="offline-overlay">\n            <svg viewBox="0 0 24 24">\n              <path d="M1 1l22 22"/>\n              <path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55"/>\n              <path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39"/>\n              <path d="M10.71 5.05A16 16 0 0 1 22.58 9"/>\n              <path d="M1.42 9a15.91 15.91 0 0 1 4.7-2.88"/>\n              <path d="M8.53 16.11a6 6 0 0 1 6.95 0"/>\n              <line x1="12" y1="20" x2="12.01" y2="20"/>\n            </svg>\n            <div class="offline-title">Kamera Offline</div>\n            <div class="offline-cam-name" id="offline-cam-name"></div>\n            <div class="offline-subtitle" id="offline-subtitle">Keine Verbindung zur Bosch Cloud</div>\n          </div>\n          <div class="auth-overlay" id="auth-overlay">\n            <svg viewBox="0 0 24 24">\n              <path d="M12 2L3 7v6c0 5 3.5 9.4 9 11 5.5-1.6 9-6 9-11V7l-9-5z"/>\n              <line x1="12" y1="9" x2="12" y2="13"/>\n              <line x1="12" y1="17" x2="12.01" y2="17"/>\n            </svg>\n            <div class="auth-title">Anmeldung abgelaufen</div>\n            <div class="auth-subtitle">Bosch Cloud Token ungültig — erneut anmelden um die Kamera wieder zu nutzen.</div>\n            <a class="auth-btn" id="auth-reauth-btn" href="/config/integrations/integration/bosch_shc_camera" target="_top">Erneut anmelden</a>\n          </div>\n          <div class="privacy-placeholder" id="privacy-placeholder">\n            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">\n              <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>\n              <path d="M7 11V7a5 5 0 0110 0v4"/>\n            </svg>\n            <span>Privat-Modus aktiv</span>\n          </div>\n          <svg class="motion-zones-overlay" id="motion-zones-overlay" viewBox="0 0 100 100" preserveAspectRatio="none"></svg>\n          <svg class="privacy-mask-overlay" id="privacy-mask-overlay" viewBox="0 0 100 100" preserveAspectRatio="none"></svg>\n          <div class="img-overlay">\n            <span class="last-event-overlay" id="last-event-overlay"></span>\n            <span class="events-overlay" id="events-overlay"></span>\n          </div>\n\n          \x3c!-- Apple-style "letzte Bewegung" indicator — small glass pill\n               in the bottom-right of the video that surfaces the camera's\n               most recent motion/audio/person event timestamp when idle. --\x3e\n          <span class="ap-last-event" id="ap-last-event">\n            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 6v6l4 2"/><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/></svg>\n            <span id="ap-last-event-text"></span>\n          </span>\n\n          \x3c!-- Apple-style overlays (v2.17.0) — rendered always, gated via CSS :host(.apple-style) --\x3e\n          <div class="ap-top">\n            <div class="ap-title-pill ap-glass">\n              <span class="ap-dot" id="ap-dot"></span>\n              <span class="ap-title-text" id="ap-title-text">Bosch Camera</span>\n            </div>\n            <div class="ap-top-right">\n              <span class="ap-badge hidden" id="ap-badge"></span>\n            </div>\n          </div>\n\n          <div class="ap-pill-bar ap-glass">\n            <button class="ap-pill-btn" id="ap-btn-snapshot" title="Snapshot" aria-label="Snapshot aufnehmen">\n              <svg viewBox="0 0 24 24"><path d="M9 2 7.17 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2h-3.17L15 2H9zm3 15a5 5 0 1 1 0-10 5 5 0 0 1 0 10z"/></svg>\n            </button>\n            <button class="ap-pill-btn" id="ap-btn-stream" title="Live-Stream" aria-label="Live-Stream starten oder stoppen" aria-pressed="false">\n              <svg viewBox="0 0 24 24" id="ap-stream-icon"><path d="M8 5v14l11-7L8 5z"/></svg>\n            </button>\n            <button class="ap-pill-btn" id="ap-btn-privacy" title="Privat-Modus" aria-label="Privat-Modus umschalten" aria-pressed="false">\n              <svg viewBox="0 0 24 24"><path d="M12 1 4 5v6c0 5.5 3.8 10.7 8 12 4.2-1.3 8-6.5 8-12V5l-8-4z"/></svg>\n            </button>\n            <button class="ap-pill-btn" id="ap-btn-light" title="Licht" aria-label="Licht umschalten" aria-pressed="false">\n              <svg viewBox="0 0 24 24"><path d="M9 21h6v-1H9v1zm3-19a7 7 0 0 0-4 12.74V17a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1v-2.26A7 7 0 0 0 12 2z"/></svg>\n            </button>\n            <button class="ap-pill-btn" id="ap-btn-fullscreen" title="Vollbild" aria-label="Vollbild">\n              <svg viewBox="0 0 24 24"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>\n            </button>\n            <button class="ap-pill-btn" id="ap-btn-more" title="Mehr Optionen" aria-label="Mehr Optionen" aria-haspopup="true" aria-expanded="false">\n              <svg viewBox="0 0 24 24"><circle cx="6" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="18" cy="12" r="2"/></svg>\n            </button>\n          </div>\n        </div>\n\n        <div class="info-row">\n          <div class="info-item">\n            <span class="info-label">Status</span>\n            <span class="info-value" id="info-status">—</span>\n          </div>\n          <div class="info-item">\n            <span class="info-label">Verbindung</span>\n            <span class="info-value" id="info-connection">—</span>\n          </div>\n          <div class="info-item" style="text-align:right" title="Bosch-API Reaktionszeit (LOCAL=500 ms, REMOTE=1000 ms). Nicht der Player-Puffer — den stellt 'Puffer-Verhalten' in den Integrations-Einstellungen ein.">\n            <span class="info-label">Reaktion</span>\n            <span class="info-value" id="info-buffering">—</span>\n          </div>\n        </div>\n\n        <div class="btn-row">\n            <button class="btn btn-snapshot" id="btn-snapshot" aria-label="Snapshot aufnehmen">\n              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">\n                <path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/>\n                <circle cx="12" cy="13" r="4"/>\n              </svg>\n              <span id="btn-snapshot-label">Snapshot</span>\n            </button>\n            <button class="btn btn-privacy-inline" id="btn-privacy-inline" title="Privat-Modus" aria-label="Privat-Modus umschalten" aria-pressed="false">\n              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">\n                <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>\n                <path d="M7 11V7a5 5 0 0110 0v4"/>\n              </svg>\n            </button>\n            <button class="btn btn-stream" id="btn-stream" aria-label="Live-Stream starten oder stoppen" aria-pressed="false">\n              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">\n                <polygon points="23 7 16 12 23 17 23 7"/>\n                <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>\n              </svg>\n              <span id="btn-stream-label">Live Stream</span>\n            </button>\n            <button class="btn btn-overflow" id="btn-overflow" title="Weitere Optionen" aria-label="Weitere Optionen" aria-haspopup="true" aria-expanded="false">\n              <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false">\n                <circle cx="12" cy="5" r="2"/>\n                <circle cx="12" cy="12" r="2"/>\n                <circle cx="12" cy="19" r="2"/>\n              </svg>\n            </button>\n            <button class="btn btn-fullscreen" id="btn-fullscreen" title="Vollbild" aria-label="Vollbild-Ansicht">\n              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">\n                <path d="M8 3H5a2 2 0 00-2 2v3m18 0V5a2 2 0 00-2-2h-3m0 18h3a2 2 0 002-2v-3M3 16v3a2 2 0 002 2h3"/>\n              </svg>\n            </button>\n          </div>\n\n          \x3c!-- Theme (iOS/Android) + day/night Mode are config-only (YAML theme: / mode:);\n               the in-card switcher buttons were removed 2026-05-30 (Thomas / issue #15).\n               Defaults: theme=ios, mode=auto. --\x3e\n\n          <div class="switch-rows">\n            <div class="sw-row" id="btn-audio">\n              <div class="sw-left">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">\n                  <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>\n                  <path d="M19.07 4.93a10 10 0 010 14.14M15.54 8.46a5 5 0 010 7.07"/>\n                </svg>\n                <span>Ton</span>\n              </div>\n              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n            </div>\n            <div class="sw-row" id="btn-light">\n              <div class="sw-left">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">\n                  <circle cx="12" cy="12" r="5"/>\n                  <line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/>\n                  <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>\n                  <line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/>\n                  <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>\n                </svg>\n                <span>Licht</span>\n              </div>\n              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n            </div>\n            \x3c!-- Light sub-controls: toggles + expandable details --\x3e\n            <div class="light-sub-controls" id="light-sub-controls" style="display:none;padding:0 0 0 28px;border-left:2px solid rgba(255,204,0,.3);margin:0 0 0 16px">\n              <div class="sw-row" id="btn-front-light" style="padding:3px 4px"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10"/></svg><span style="font-size:13px">Frontlicht</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n              <div class="sw-row" id="btn-top-led" style="display:none;padding:3px 4px"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><path d="M12 2v8l6-4M12 2v8l-6-4"/></svg><span style="font-size:13px">Oberes Licht</span></div><div id="top-led-color-mini" style="width:14px;height:14px;border-radius:50%;border:1px solid #666;margin-right:4px"></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n              <div class="sw-row" id="btn-bottom-led" style="display:none;padding:3px 4px"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><path d="M12 22v-8l6 4M12 22v-8l-6 4"/></svg><span style="font-size:13px">Unteres Licht</span></div><div id="bottom-led-color-mini" style="width:14px;height:14px;border-radius:50%;border:1px solid #666;margin-right:4px"></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n              <div class="sw-row" id="btn-wallwasher" style="display:none;padding:3px 4px"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><path d="M9 18h6M10 22h4M12 2v1"/><path d="M18 12a6 6 0 10-12 0c0 2.21 1.34 4.1 3 5h6c1.66-.9 3-2.79 3-5z"/></svg><span style="font-size:13px">Oben + Unten</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n              <div id="light-details-toggle" style="padding:4px;cursor:pointer;display:flex;align-items:center;gap:6px;color:#888;font-size:12px;user-select:none"><svg id="light-details-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width:12px;height:12px;transition:transform .2s"><polyline points="6 9 12 15 18 9"/></svg><span>Helligkeit & Farben</span></div>\n              <div id="light-details-body" style="display:none">\n                <div id="intensity-row" style="display:flex;align-items:center;gap:8px;padding:2px 4px;font-size:12px"><span style="white-space:nowrap;min-width:36px">Front</span><input type="range" id="intensity-slider" min="0" max="100" step="5" style="flex:1;accent-color:#fc0;height:4px"><span id="intensity-value" style="min-width:28px;text-align:right;color:#999">—</span></div>\n                <div id="top-bri-row" style="display:none;align-items:center;gap:8px;padding:2px 4px;font-size:12px"><span style="white-space:nowrap;min-width:36px">Oben</span><input type="range" id="top-bri-slider" min="0" max="100" step="5" style="flex:1;accent-color:#4DFF7D;height:4px"><span id="top-bri-value" style="min-width:28px;text-align:right;color:#999">—</span></div>\n                <div id="bottom-bri-row" style="display:none;align-items:center;gap:8px;padding:2px 4px;font-size:12px"><span style="white-space:nowrap;min-width:36px">Unten</span><input type="range" id="bottom-bri-slider" min="0" max="100" step="5" style="flex:1;accent-color:#FF453A;height:4px"><span id="bottom-bri-value" style="min-width:28px;text-align:right;color:#999">—</span></div>\n                <div id="colortemp-row" style="display:none;align-items:center;gap:8px;padding:2px 4px;font-size:12px"><span style="white-space:nowrap;min-width:36px">Farbt.</span><input type="range" id="colortemp-slider" min="-100" max="100" step="5" style="flex:1;accent-color:#f90;height:4px;background:linear-gradient(to right,#69f,#fff,#f90)"><span id="colortemp-value" style="min-width:28px;text-align:right;color:#999">—</span></div>\n              </div>\n            </div>\n            <div class="sw-row privacy-row" id="btn-privacy">\n              <div class="sw-left">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">\n                  <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>\n                  <path d="M7 11V7a5 5 0 0110 0v4"/>\n                </svg>\n                <span>Privat</span>\n              </div>\n              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n            </div>\n            <div class="sw-row" id="btn-notifications">\n              <div class="sw-left">\n                <svg id="notif-icon-on" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">\n                  <path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9"/>\n                  <path d="M13.73 21a2 2 0 01-3.46 0"/>\n                </svg>\n                <svg id="notif-icon-off" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display:none">\n                  <path d="M13.73 21a2 2 0 01-3.46 0"/>\n                  <path d="M18.63 13A17.89 17.89 0 0118 8"/>\n                  <path d="M6.26 6.26A5.86 5.86 0 006 8c0 7-3 9-3 9h14"/>\n                  <path d="M18 8a6 6 0 00-9.33-5"/>\n                  <line x1="1" y1="1" x2="23" y2="23"/>\n                </svg>\n                <span>Benachrichtigungen</span>\n              </div>\n              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n            </div>\n            <div class="sw-row" id="btn-intercom" style="display:none">\n              <div class="sw-left">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">\n                  <path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/>\n                  <path d="M19 10v2a7 7 0 01-14 0v-2"/>\n                  <line x1="12" y1="19" x2="12" y2="23"/>\n                  <line x1="8" y1="23" x2="16" y2="23"/>\n                </svg>\n                <span>Gegensprech.</span>\n              </div>\n              <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n            </div>\n          </div>\n\n          <div class="pan-section" id="pan-section" style="display:none">\n            <div class="pan-row">\n              <button class="pan-btn" id="pan-full-left"  title="Ganz links" aria-label="Kamera ganz nach links schwenken">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true" focusable="false">\n                  <polyline points="11 18 5 12 11 6"/><polyline points="18 18 12 12 18 6"/>\n                </svg>\n              </button>\n              <button class="pan-btn" id="pan-left"       title="Links" aria-label="Kamera nach links schwenken">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true" focusable="false">\n                  <polyline points="15 18 9 12 15 6"/>\n                </svg>\n              </button>\n              <button class="pan-btn" id="pan-center"     title="Mitte" aria-label="Kamera zentrieren">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">\n                  <circle cx="12" cy="12" r="3"/>\n                  <line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/>\n                  <line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/>\n                </svg>\n              </button>\n              <button class="pan-btn" id="pan-right"      title="Rechts" aria-label="Kamera nach rechts schwenken">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true" focusable="false">\n                  <polyline points="9 18 15 12 9 6"/>\n                </svg>\n              </button>\n              <button class="pan-btn" id="pan-full-right" title="Ganz rechts" aria-label="Kamera ganz nach rechts schwenken">\n                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true" focusable="false">\n                  <polyline points="13 18 19 12 13 6"/><polyline points="6 18 12 12 6 6"/>\n                </svg>\n              </button>\n              <span   class="pan-pos" id="pan-position">0°</span>\n            </div>\n          </div>\n\n          <div class="quality-section" id="quality-section" style="display:none">\n            <div class="quality-row">\n              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"\n                   style="width:16px;height:16px;flex-shrink:0;color:var(--secondary-text-color,#8e8e93)">\n                <rect x="2" y="7" width="20" height="15" rx="2"/>\n                <polyline points="17 2 12 7 7 2"/>\n              </svg>\n              <span class="quality-label">Qualität</span>\n              <select class="quality-select" id="quality-select">\n                <option value="Auto">Auto</option>\n                <option value="Hoch (30 Mbps)">Hoch (30 Mbps)</option>\n                <option value="Niedrig (1.9 Mbps)">Niedrig (1.9 Mbps)</option>\n              </select>\n            </div>\n          </div>\n\n          \x3c!-- Accordion: Notification Types --\x3e\n          <div class="accordion" id="acc-notif-types">\n            <div class="accordion-header" id="acc-notif-types-header">\n              <span class="accordion-title">Benachrichtigungs-Typen</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div class="sw-row" id="btn-notif-movement">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>\n                    <span>Bewegung</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-notif-person">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>\n                    <span>Person</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-notif-audio">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 010 14.14"/></svg>\n                    <span>Audio</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-notif-trouble">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>\n                    <span>Störung</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-notif-alarm">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>\n                    <span>Kamera-Alarm</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n              </div>\n            </div>\n          </div>\n\n          \x3c!-- Accordion: Advanced Controls --\x3e\n          <div class="accordion" id="acc-advanced">\n            <div class="accordion-header" id="acc-advanced-header">\n              <span class="accordion-title">Erweitert</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div class="sw-row" id="btn-timestamp">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>\n                    <span>Zeitstempel</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-autofollow">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="8"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/></svg>\n                    <span>Auto-Follow</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-motion">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>\n                    <span>Bewegungserkennung</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-record-sound">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/></svg>\n                    <span>Ton aufnehmen</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-privacy-sound">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 010 7.07"/></svg>\n                    <span>Privat-Ton</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n              </div>\n            </div>\n          </div>\n\n          \x3c!-- Gen2 Accordion: Automatik & Sicherheit --\x3e\n          <div class="accordion" id="acc-gen2-auto" style="display:none">\n            <div class="accordion-header" id="acc-gen2-auto-header">\n              <span class="accordion-title">Automatik & Sicherheit</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div class="sw-row" id="btn-motion-light" style="padding:4px 0"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg><span>Licht bei Bewegung</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n                <div class="sw-row" id="btn-ambient-light" style="padding:4px 0"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/></svg><span>Dauerlicht</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n                <div class="sw-row" id="btn-intrusion" style="padding:4px 0"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg><span>Einbrucherkennung</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n                <div id="motion-sens-row" style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px"><span style="white-space:nowrap">Empfindlichkeit</span><input type="range" id="motion-sens-slider" min="1" max="5" step="1" style="flex:1;accent-color:#ff9500;height:4px"><span id="motion-sens-value" style="min-width:16px;text-align:right;color:#999">—</span></div>\n                \x3c!-- Gen2 Indoor II — Alarm system (75 dB siren) --\x3e\n                <div class="sw-row" id="btn-alarm-arm" style="padding:4px 0;display:none"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg><span>Alarmanlage scharf</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n                <div class="sw-row" id="btn-alarm-mode" style="padding:4px 0;display:none"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="13" r="7"/><path d="M12 9v4l2 2M5 3L2 6M19 3l3 3"/></svg><span>Sirene (75 dB)</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n                <div class="sw-row" id="btn-prealarm" style="padding:4px 0;display:none"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2"/></svg><span>Pre-Alarm (rote LED)</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n                <div id="power-led-row" style="display:none;align-items:center;gap:8px;padding:4px 0;font-size:13px"><span style="white-space:nowrap">Power-LED</span><input type="range" id="power-led-slider" min="0" max="100" step="5" style="flex:1;accent-color:#ff9500;height:4px"><span id="power-led-value" style="min-width:34px;text-align:right;color:#999">—</span></div>\n              </div>\n            </div>\n          </div>\n\n          \x3c!-- Automations Accordion (alle Kameras, konfigurierbar) --\x3e\n          <div class="accordion" id="acc-automations" style="display:none">\n            <div class="accordion-header" id="acc-automations-header">\n              <span class="accordion-title">Automationen</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div id="automations-container"></div>\n              </div>\n            </div>\n          </div>\n\n          \x3c!-- Gen2 Accordion: Licht & Kamera --\x3e\n          <div class="accordion" id="acc-gen2-light" style="display:none">\n            <div class="accordion-header" id="acc-gen2-light-header">\n              <span class="accordion-title">Licht & Kamera</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div id="colortemp-row" style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px"><span style="white-space:nowrap">Farbtemperatur</span><input type="range" id="colortemp-slider" min="-100" max="100" step="5" style="flex:1;accent-color:#f90;height:4px;background:linear-gradient(to right,#69f,#fff,#f90)"><span id="colortemp-value" style="min-width:32px;text-align:right;color:#999">—</span></div>\n                <div id="rgb-lights-row" style="padding:4px 0;font-size:13px">\n                  <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px"><span style="flex:1">Farbe Oben</span><div id="top-led-color" style="width:24px;height:24px;border-radius:50%;border:2px solid #444;cursor:pointer" title="Farbe wählen"></div><input type="color" id="top-led-picker" style="display:none"></div>\n                  <div style="display:flex;align-items:center;gap:10px"><span style="flex:1">Farbe Unten</span><div id="bottom-led-color" style="width:24px;height:24px;border-radius:50%;border:2px solid #444;cursor:pointer" title="Farbe wählen"></div><input type="color" id="bottom-led-picker" style="display:none"></div>\n                </div>\n                <div class="sw-row" id="btn-status-led" style="padding:4px 0"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/></svg><span>Status-LED</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>\n                <div id="mic-level-row" style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:18px;height:18px;flex-shrink:0"><path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/><path d="M19 10v2a7 7 0 01-14 0v-2"/></svg><span style="white-space:nowrap">Mikrofon</span><input type="range" id="mic-slider" min="0" max="100" step="5" style="flex:1;accent-color:#0a84ff;height:4px"><span id="mic-value" style="min-width:28px;text-align:right;color:#999">—</span></div>\n                <div id="lens-elev-row" style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:18px;height:18px;flex-shrink:0"><path d="M12 22V2M5 12l7-10 7 10"/></svg><span style="white-space:nowrap">Höhe</span><input type="range" id="lens-slider" min="50" max="500" step="5" style="flex:1;accent-color:#30d158;height:4px"><span id="lens-value" style="min-width:36px;text-align:right;color:#999">—</span></div>\n              </div>\n            </div>\n          </div>\n\n          \x3c!-- Accordion: Diagnostics & Services --\x3e\n          <div class="accordion" id="acc-diagnostics">\n            <div class="accordion-header" id="acc-diagnostics-header">\n              <span class="accordion-title">Diagnose</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div class="diag-row" id="diag-wifi">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12.55a11 11 0 0114.08 0"/><path d="M1.42 9a16 16 0 0121.16 0"/><path d="M8.53 16.11a6 6 0 016.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/></svg>\n                    WiFi\n                  </span>\n                  <span class="diag-value" id="diag-wifi-val">—</span>\n                </div>\n                <div class="diag-row" id="diag-firmware">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/></svg>\n                    Firmware\n                  </span>\n                  <span class="diag-value" id="diag-firmware-val">—</span>\n                </div>\n                <div class="diag-row" id="diag-ambient">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/></svg>\n                    Umgebungslicht\n                  </span>\n                  <span class="diag-value" id="diag-ambient-val">—</span>\n                </div>\n                <div class="diag-row" id="diag-movement-today">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>\n                    Bewegung heute\n                  </span>\n                  <span class="diag-value" id="diag-movement-today-val">—</span>\n                </div>\n                <div class="diag-row" id="diag-audio-today">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 010 14.14"/></svg>\n                    Audio heute\n                  </span>\n                  <span class="diag-value" id="diag-audio-today-val">—</span>\n                </div>\n              </div>\n            </div>\n          </div>\n\n          \x3c!-- Accordion: Schedules & Zones --\x3e\n          <div class="accordion" id="acc-schedules">\n            <div class="accordion-header" id="acc-schedules-header">\n              <span class="accordion-title">Zeitpläne & Zonen</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div class="diag-row">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>\n                    Zeitpläne\n                  </span>\n                  <span class="diag-value" id="diag-rules-count">—</span>\n                </div>\n                <div id="rules-list" style="padding:0 4px"></div>\n                <div class="sw-row" id="btn-show-zones">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18"/></svg>\n                    <span>Motion-Zonen anzeigen</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="sw-row" id="btn-show-masks">\n                  <div class="sw-left">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>\n                    <span>Privacy-Masken anzeigen</span>\n                  </div>\n                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>\n                </div>\n                <div class="diag-row">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18"/></svg>\n                    Motion-Zonen\n                  </span>\n                  <span class="diag-value" id="diag-zones-count">—</span>\n                </div>\n                <div class="diag-row">\n                  <span class="diag-label">\n                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>\n                    Privacy-Masken\n                  </span>\n                  <span class="diag-value" id="diag-masks-count">—</span>\n                </div>\n              </div>\n            </div>\n          </div>\n\n          \x3c!-- Accordion: Services --\x3e\n          <div class="accordion" id="acc-services">\n            <div class="accordion-header" id="acc-services-header">\n              <span class="accordion-title">Services</span>\n              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>\n            </div>\n            <div class="accordion-body">\n              <div class="accordion-content">\n                <div class="svc-grid" id="svc-grid"></div>\n                <div id="svc-result" style="font-size:11px;color:#999;padding:4px 0;display:none"></div>\n              </div>\n            </div>\n          </div>\n\n      </ha-card>\n    `;
    const img = this.shadowRoot.getElementById("cam-img");
    img.addEventListener("load", () => this._onImageLoaded());
    img.addEventListener("error", () => this._onImageError());
    img.addEventListener("click", () => this._requestFullscreen());
    const vid = this.shadowRoot.getElementById("cam-video");
    vid.addEventListener("click", () => this._requestFullscreen());
    this.shadowRoot.getElementById("btn-snapshot").addEventListener("click", () => this._onSnapshotClick());
    this.shadowRoot.getElementById("btn-stream").addEventListener("click", () => this._toggleStream());
    const apg = this.shadowRoot.getElementById("auto-play-gate");
    if (apg) apg.addEventListener("pointerup", () => this._onPlayGateTap());
    this.shadowRoot.getElementById("btn-fullscreen").addEventListener("click", () => this._requestFullscreen());
    this.shadowRoot.getElementById("btn-overflow").addEventListener("click", () => {
      this.classList.toggle("overflow-open");
    });
    this.shadowRoot.getElementById("btn-privacy-inline").addEventListener("click", () => this._toggleSwitchWithRollback(this._entities.privacy));
    const apBindClick = (id, fn) => {
      const el = this.shadowRoot.getElementById(id);
      if (el) el.addEventListener("click", fn);
    };
    apBindClick("ap-btn-snapshot", () => this._onSnapshotClick());
    apBindClick("ap-btn-stream", () => this._toggleStream());
    apBindClick("ap-btn-privacy", () => this._toggleSwitchWithRollback(this._entities.privacy));
    apBindClick("ap-btn-light", () => this._toggleSwitchWithRollback(this._entities.light));
    apBindClick("ap-btn-fullscreen", () => this._requestFullscreen());
    apBindClick("ap-btn-more", () => {
      this.classList.toggle("overflow-open");
      this._syncMoreButton();
      this._refreshThemeSwitcher();
    });
    this._syncMoreButton();
    const themeSwitcher = this.shadowRoot.getElementById("ap-theme-switcher");
    if (themeSwitcher) {
      themeSwitcher.querySelectorAll("[data-theme]").forEach(b => {
        b.addEventListener("click", () => {
          const t = b.getAttribute("data-theme");
          this._setUserTheme(t);
          this._applyTheme(this._resolveTheme());
        });
      });
      this._refreshThemeSwitcher();
    }
    const modeSwitcher = this.shadowRoot.getElementById("ap-mode-switcher");
    if (modeSwitcher) {
      modeSwitcher.querySelectorAll("[data-mode]").forEach(b => {
        b.addEventListener("click", () => {
          const m = b.getAttribute("data-mode");
          this._setUserMode(m);
          this._applyMode(this._resolveMode());
        });
      });
      this._refreshModeSwitcher();
    }
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
        if (this._hass?.services?.bosch_shc_camera?.trigger_snapshot) this._callService("bosch_shc_camera", "trigger_snapshot", {});
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
      if (this._remoteSkipWebRTC) {
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
        this._markLiveBadge();
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
    const _skipWebRTC = this._remoteSkipWebRTC;
    if (_skipWebRTC) {
      console.debug("bosch-camera-card: remote endpoint + Companion/mobile-browser — skipping WebRTC, using HLS");
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
          if (err.name === "NotAllowedError") {
            const overlay = this.shadowRoot?.getElementById("tap-to-play-overlay");
            if (overlay) {
              overlay.classList.add("visible");
              const resume = () => {
                overlay.classList.remove("visible");
                overlay.removeEventListener("pointerup", resume);
                video.muted = true;
                video.play().catch(() => {});
              };
              overlay.addEventListener("pointerup", resume);
            }
            return;
          }
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
        let _didLiveSeek = false;
        hls.on(Hls.Events.FRAG_BUFFERED, () => {
          if (_didLiveSeek) return;
          const lsp = hls.liveSyncPosition;
          if (lsp != null && video && lsp - video.currentTime > 6) {
            _didLiveSeek = true;
            console.debug("bosch-camera-card: seeking HLS to live edge", lsp, "from", video.currentTime);
            video.currentTime = lsp;
          }
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
        const retryDelay = attempt <= 6 ? 5e3 : 1e4;
        console.warn(`bosch-camera-card: stream not available (attempt ${attempt}), retrying in ${retryDelay / 1e3}s`, e);
        this._liveVideoActive = false;
        this._startingLiveVideo = false;
        this._startRefreshTimer();
        setTimeout(() => {
          if (this._isStreaming && this._isStreaming() && !this._liveVideoActive && !this._startingLiveVideo) {
            this._waitingForStream = true;
            this._setLoadingOverlay(true, "Stream wird erneut versucht…");
            this._waitForStreamReady();
          }
        }, retryDelay);
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
    let webrtcReject = null;
    let webrtcTimeout = null;
    const unsub = await this._hass.connection.subscribeMessage(event => {
      if (!pc || pc.signalingState === "closed" || pc.signalingState === "failed") return;
      if (event.type === "answer") {
        pc.setRemoteDescription({
          type: "answer",
          sdp: event.answer
        }).catch(e => console.debug("bosch-camera-card: setRemoteDescription skipped (pc closing):", e?.message));
      } else if (event.type === "candidate") {
        pc.addIceCandidate(event.candidate).catch(e => console.debug("bosch-camera-card: addIceCandidate skipped (pc closing):", e?.message));
      } else if (event.type === "error") {
        const msg = event.message || "webrtc_offer_error";
        const isRace = typeof msg === "string" && (msg.includes("does not support WebRTC") || msg.includes("frontend_stream_types"));
        if (isRace) {
          console.debug("bosch-camera-card: WebRTC offer rejected (HA stream-type race), fast-falling to HLS:", msg);
        } else {
          console.warn("bosch-camera-card: WebRTC error:", msg);
        }
        if (webrtcTimeout) clearTimeout(webrtcTimeout);
        if (webrtcReject) webrtcReject(new Error(typeof msg === "string" ? msg : "webrtc_offer_error"));
      }
    }, {
      type: "camera/webrtc/offer",
      entity_id: entityId,
      offer: offer.sdp
    });
    this._webrtcUnsub = unsub;
    await new Promise((resolve, reject) => {
      webrtcReject = reject;
      const timeout = setTimeout(() => reject(new Error("WebRTC: no track within 5s")), 5e3);
      webrtcTimeout = timeout;
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
      try {
        this._webrtcUnsub();
      } catch {}
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
    const tapOverlay = this.shadowRoot?.getElementById("tap-to-play-overlay");
    if (tapOverlay) tapOverlay.classList.remove("visible");
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
      if (this._hass?.services?.bosch_shc_camera?.trigger_snapshot) this._callService("bosch_shc_camera", "trigger_snapshot", {});
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
    const apSnap = this.shadowRoot.getElementById("ap-btn-snapshot");
    if (apSnap) {
      apSnap.classList.remove("ok-flash");
      void apSnap.offsetWidth;
      apSnap.classList.add("ok-flash");
      setTimeout(() => apSnap.classList.remove("ok-flash"), 450);
    }
  }
  _update() {
    if (!this._hass || !this._config) return;
    const hass = this._hass;
    const ents = this._entities;
    if (this._remoteSkipWebRTC) {
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
    const apTitleEl = this.shadowRoot.getElementById("ap-title-text");
    if (apTitleEl) apTitleEl.textContent = titleEl?.textContent || "Bosch Camera";
    const pushState = hass.states[ents.push_status];
    const pushBadge = this.shadowRoot.getElementById("push-badge");
    const pushLabel = this.shadowRoot.getElementById("push-label");
    if (pushBadge && pushLabel) {
      const isFcm = pushState?.state === "fcm_push";
      pushBadge.className = "push-badge " + (isFcm ? "push" : "poll");
      pushLabel.textContent = isFcm ? "push" : "poll";
    }
    const statusState = String(hass.states[ents.status]?.state || "UNKNOWN").toUpperCase();
    const statusDot = this.shadowRoot.getElementById("status-dot");
    const infoStatus = this.shadowRoot.getElementById("info-status");
    if (statusDot) statusDot.className = "status-dot " + ({
      ONLINE: "online",
      OFFLINE: "offline"
    }[statusState] || "unknown");
    if (infoStatus) infoStatus.textContent = statusState;
    const apDot = this.shadowRoot.getElementById("ap-dot");
    if (apDot) {
      let dotCls = "ap-dot ";
      if (statusState === "OFFLINE") dotCls += "offline"; else if (hass.states[ents.privacy]?.state === "on") dotCls += "privacy"; else if (statusState === "ONLINE") dotCls += "online";
      apDot.className = dotCls;
    }
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
    const camState = hass.states[ents.camera]?.state;
    const isIntegrationDown = camState === "unavailable" || camState === undefined;
    const authOverlay = this.shadowRoot.getElementById("auth-overlay");
    if (authOverlay) authOverlay.classList.toggle("visible", isIntegrationDown);
    const offlineOverlay = this.shadowRoot.getElementById("offline-overlay");
    const isOffline = !isIntegrationDown && statusState === "OFFLINE";
    if (offlineOverlay) {
      offlineOverlay.classList.toggle("visible", isOffline);
      if (isOffline) {
        const lastChanged = hass.states[ents.status]?.last_changed;
        const camNameEl = this.shadowRoot.getElementById("offline-cam-name");
        if (camNameEl) {
          camNameEl.textContent = this._config?.title || hass.states[ents.camera]?.attributes?.friendly_name || ents.camera;
        }
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
    this.classList.toggle("cam-offline", isOffline);
    const isStreaming = this._isStreaming();
    const badge = this.shadowRoot.getElementById("stream-badge");
    const streamLabel = this.shadowRoot.getElementById("stream-label");
    const btnStream = this.shadowRoot.getElementById("btn-stream");
    const btnStreamLbl = this.shadowRoot.getElementById("btn-stream-label");
    const switchOn = hass.states[ents.switch]?.state === "on";
    const backendStreamStatus = hass.states[ents.streamStatus]?.state || camAttrs.stream_status || "";
    const sharedConnecting = switchOn && (backendStreamStatus === "connecting" || backendStreamStatus === "warming_up");
    const streamBadgeState = isOffline ? "offline" : this._liveVideoActive ? "streaming" : isStreaming || this._startingLiveVideo || sharedConnecting ? "connecting" : "idle";
    if (badge) badge.className = "stream-badge " + streamBadgeState;
    if (streamLabel && !isStreaming) streamLabel.textContent = streamBadgeState;
    if (this._liveVideoActive && (this._streamConnecting || this._waitingForStream)) {
      this._streamConnecting = false;
      this._waitingForStream = false;
      if (this._connectSteps) {
        this._connectSteps.forEach(t => clearTimeout(t));
        this._connectSteps = null;
      }
      this._setLoadingOverlay(false);
    }
    const apBadge = this.shadowRoot.getElementById("ap-badge");
    const apBtnStream = this.shadowRoot.getElementById("ap-btn-stream");
    const apBtnPrivacy = this.shadowRoot.getElementById("ap-btn-privacy");
    const apStreamIcon = this.shadowRoot.getElementById("ap-stream-icon");
    const privActive = this._optimisticActive(ents.privacy, hass);
    if (apBadge) {
      if (streamBadgeState === "offline") {
        apBadge.className = "ap-badge offline";
        apBadge.textContent = "Offline";
      } else if (streamBadgeState === "streaming") {
        apBadge.className = "ap-badge live";
        apBadge.textContent = "Live";
      } else if (streamBadgeState === "connecting") {
        apBadge.className = "ap-badge connecting";
        apBadge.textContent = "Verbinde";
      } else {
        apBadge.className = "ap-badge hidden";
        apBadge.textContent = "";
      }
    }
    if (apBtnStream) {
      apBtnStream.classList.toggle("on", isStreaming);
      apBtnStream.classList.toggle("connecting", streamBadgeState === "connecting");
      apBtnStream.setAttribute("aria-pressed", isStreaming ? "true" : "false");
      apBtnStream.setAttribute("title", isStreaming ? "Live-Stream stoppen" : "Live-Stream starten");
      if (apStreamIcon) {
        apStreamIcon.innerHTML = isStreaming ? '<rect x="6" y="6" width="12" height="12" rx="2"/>' : '<path d="M8 5v14l11-7L8 5z"/>';
      }
    }
    if (apBtnPrivacy) {
      apBtnPrivacy.classList.toggle("on", privActive);
      apBtnPrivacy.classList.remove("danger");
      apBtnPrivacy.setAttribute("aria-pressed", privActive ? "true" : "false");
    }
    const apBtnLight = this.shadowRoot.getElementById("ap-btn-light");
    const lightActive = this._optimisticActive(ents.light, hass);
    if (apBtnLight) {
      apBtnLight.classList.toggle("on", lightActive);
      apBtnLight.setAttribute("aria-pressed", lightActive ? "true" : "false");
    }
    if (apBtnLight) apBtnLight.toggleAttribute("hidden", !hass.states[ents.light]);
    if (apBtnPrivacy) apBtnPrivacy.toggleAttribute("hidden", !hass.states[ents.privacy]);
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
      if (isStreaming && connType === "REMOTE") {
        connBadge.className = "conn-badge remote";
        connBadge.textContent = "Cloud";
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
      if (this._hass?.services?.bosch_shc_camera?.trigger_snapshot) this._callService("bosch_shc_camera", "trigger_snapshot", {});
      this._scheduleImageLoad(3500);
      this._startRefreshTimer();
    }
    this._lastStreaming = isStreaming;
    const backendWaiting = sharedConnecting;
    this._evaluateGateForStreamTransition();
    if (this._playGateActive) {
      this._setLoadingOverlay(false);
      return;
    }
    if ((shouldVideo || backendWaiting) && !this._liveVideoActive && !this._startingLiveVideo && !this._waitingForStream) {
      this._waitingForStream = true;
      if (this._config.snapshot_during_warmup && !this._imageLoaded && !this._awaitingFresh) {
        this._triggerFreshSnapshot();
      }
      this._setLoadingOverlay(true, this._streamPhaseText());
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
    const apLastEvent = this.shadowRoot.getElementById("ap-last-event");
    const apLastEventText = this.shadowRoot.getElementById("ap-last-event-text");
    if (apLastEvent && apLastEventText) {
      const hasEvent = lastEventStr !== "—";
      let pretty = lastEventStr;
      if (hasEvent && lastEventState?.state) {
        try {
          const d = new Date(lastEventState.state);
          if (!isNaN(d)) {
            const sameDay = d.toDateString() === (new Date).toDateString();
            pretty = sameDay ? d.toLocaleTimeString("de-DE", {
              hour: "2-digit",
              minute: "2-digit"
            }) : d.toLocaleDateString("de-DE", {
              weekday: "short",
              day: "2-digit",
              month: "2-digit"
            });
          }
        } catch {}
      }
      apLastEventText.textContent = pretty;
      const camTimestampOverlay = !!hass.states[ents.camera]?.attributes?.camera_timestamp_overlay;
      apLastEvent.classList.toggle("visible", hasEvent && !camTimestampOverlay);
      apLastEvent.classList.toggle("hide-during-stream", isStreaming);
    }
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
    for (const [rowId, entId] of [ [ "btn-alarm-arm", ents.alarmSystemArm ], [ "btn-alarm-mode", ents.alarmMode ], [ "btn-prealarm", ents.preAlarm ] ]) {
      const row = this.shadowRoot.getElementById(rowId);
      if (row) row.style.display = hasAlarmSystem && entId && hass.states[entId] ? "flex" : "none";
    }
    this._updateToggleBtn("btn-alarm-arm", ents.alarmSystemArm, hass.states[ents.alarmSystemArm]);
    this._updateToggleBtn("btn-alarm-mode", ents.alarmMode, hass.states[ents.alarmMode]);
    this._updateToggleBtn("btn-prealarm", ents.preAlarm, hass.states[ents.preAlarm]);
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
        if (!audioOn || this._androidAudioMuted) {
          video.muted = true;
        }
        this._refreshAudioToggle();
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
    if (this._lastPrivacy !== true && privacyOn && this._liveVideoActive) {
      this._stopLiveVideo();
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
  _optimisticActive(entityId, hass) {
    const opt = this._optimistic[entityId];
    const isPending = opt === "pending";
    const displayState = entityId in this._optimistic && !isPending ? opt : hass?.states[entityId]?.state;
    return displayState === "on";
  }
  _streamPhaseText() {
    const st = this._hass?.states[this._entities.streamStatus]?.state || this._hass?.states[this._entities.camera]?.attributes?.stream_status || "";
    if (st === "warming_up") return "Kamera wird aufgeweckt…";
    if (st === "connecting") return "Verbindung wird aufgebaut…";
    if (st === "streaming" || st === "streaming_remote") return "HLS wird geladen…";
    return "Stream wird gestartet…";
  }
  _waitForStreamReady(attempt = 0) {
    if (!this._waitingForStream || !this._hass) return;
    const cam = this._hass.states[this._entities.camera];
    const camReady = cam?.state === "streaming";
    if (attempt > 0 && attempt % 5 === 0) {
      this._setLoadingOverlay(true, this._streamPhaseText());
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
        if (fresh?.state === "unavailable") return;
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
      this._setLoadingOverlay(true, this._streamPhaseText());
      this._connectSteps = [ 3e3, 7e3, 12e3, 2e4, 28e3, 4e4, 52e3, 65e3, 78e3 ].map(ms => setTimeout(() => {
        if (this._streamConnecting) this._setLoadingOverlay(true, this._streamPhaseText());
      }, ms));
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
    const video = this._liveVideoActive ? this.shadowRoot.getElementById("cam-video") : null;
    if (video) {
      this._androidAudioMuted = false;
      const unmuting = video.muted;
      video.muted = !unmuting;
      if (unmuting && video.paused) video.play().catch(() => {});
      const b = this.shadowRoot.getElementById("btn-audio");
      if (b) b.classList.toggle("on", !video.muted);
      if (unmuting && this._hass.states[entityId]?.state === "off") {
        this._setOptimistic(entityId, "on");
        this._callService("switch", "turn_on", {
          entity_id: entityId
        });
      }
      return;
    }
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
    if (Date.now() - _boschFsExitAt < 400) return;
    const ua = navigator.userAgent || "";
    const isIOS = /iPhone|iPod|iPad/i.test(ua) || /Macintosh/i.test(ua) && (navigator.maxTouchPoints || 0) > 1;
    if (isIOS) {
      this._enterCssFullscreen();
      return;
    }
    const wrapper = this.shadowRoot.getElementById("img-wrapper");
    if (this._isNativeFullscreen()) {
      _boschFsExitAt = Date.now();
      if (document.exitFullscreen) return document.exitFullscreen();
      if (document.webkitExitFullscreen) return document.webkitExitFullscreen();
      if (document.mozCancelFullScreen) return document.mozCancelFullScreen();
      if (document.msExitFullscreen) return document.msExitFullscreen();
      return;
    }
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
    if (_boschFsOwner && _boschFsOwner !== this && _boschFsOwner.classList?.contains("fs-active")) {
      _boschFsOwner._exitCssFullscreen();
    }
    _boschFsOwner = this;
    this.classList.add("fs-active");
    this._updateFullscreenButtonState();
    document.body.style.overflow = "hidden";
    this._fsClickOut = e => {
      if (!this.contains(e.target)) this._exitCssFullscreen();
    };
    this._fsKeyDown = e => {
      if (e.key === "Escape") this._exitCssFullscreen();
    };
    setTimeout(() => {
      document.addEventListener("pointerup", this._fsClickOut);
      document.addEventListener("keydown", this._fsKeyDown);
    }, 100);
  }
  _exitCssFullscreen() {
    this.classList.remove("fs-active");
    if (_boschFsOwner === this) _boschFsOwner = null;
    _boschFsExitAt = Date.now();
    this._updateFullscreenButtonState();
    document.body.style.overflow = "";
    if (this._fsClickOut) {
      document.removeEventListener("pointerup", this._fsClickOut);
      this._fsClickOut = null;
    }
    if (this._fsKeyDown) {
      document.removeEventListener("keydown", this._fsKeyDown);
      this._fsKeyDown = null;
    }
  }
  _syncMoreButton() {
    const open = this.classList.contains("overflow-open");
    const more = this.shadowRoot?.getElementById("ap-btn-more");
    if (more) {
      more.classList.toggle("on", open);
      more.setAttribute("aria-expanded", open ? "true" : "false");
    }
    const legacy = this.shadowRoot?.getElementById("btn-overflow");
    if (legacy) legacy.setAttribute("aria-expanded", open ? "true" : "false");
  }
  _isNativeFullscreen() {
    const sr = this.shadowRoot;
    const shadowFs = sr && sr.fullscreenElement;
    const docFs = document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement || document.msFullscreenElement;
    return !!shadowFs || docFs === this;
  }
  _updateFullscreenButtonState() {
    const btn = this.shadowRoot?.getElementById("ap-btn-fullscreen");
    if (!btn) return;
    const wrapper = this.shadowRoot?.getElementById("img-wrapper");
    const nativeFs = this._isNativeFullscreen() || !!(document.fullscreenElement === wrapper || document.webkitFullscreenElement === wrapper || document.mozFullScreenElement === wrapper || document.msFullscreenElement === wrapper);
    const cssFs = this.classList.contains("fs-active");
    const active = nativeFs || cssFs;
    btn.classList.toggle("on", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
    btn.setAttribute("title", active ? "Vollbild verlassen" : "Vollbild");
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
  static getStubConfig(hass) {
    const states = hass && hass.states || {};
    const ids = Object.keys(states).filter(id => id.startsWith("camera."));
    const bosch = ids.find(id => id.includes("bosch") || (states[id]?.attributes?.brand || "").toLowerCase().includes("bosch"));
    return {
      camera_entity: bosch || ids[0] || ""
    };
  }
  static getConfigElement() {
    return document.createElement("bosch-camera-card-editor");
  }
  getCardSize() {
    return 4;
  }
}

customElements.define("bosch-camera-card", BoschCameraCard);

class BoschCameraCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    if (this.shadowRoot) this._render();
  }
  set hass(hass) {
    this._hass = hass;
    if (this.shadowRoot) this._render();
  }
  get hass() {
    return this._hass;
  }
  connectedCallback() {
    this._render();
  }
  _bosch_cameras() {
    const out = [];
    const states = this._hass?.states || {};
    for (const id of Object.keys(states)) {
      if (id.startsWith("camera.") && (id.includes("bosch") || (states[id]?.attributes?.brand || "").toLowerCase().includes("bosch"))) {
        out.push(id);
      }
    }
    if (out.length === 0) {
      for (const id of Object.keys(states)) {
        if (id.startsWith("camera.")) out.push(id);
      }
    }
    return out.sort();
  }
  _render() {
    if (!this.shadowRoot) this.attachShadow({
      mode: "open"
    });
    const cfg = this._config || {};
    const cams = this._bosch_cameras();
    if (cfg.camera_entity && !cams.includes(cfg.camera_entity)) cams.push(cfg.camera_entity);
    cams.sort();
    const sel = (name, val, opts) => `\n      <label>${name}\n        <select name="${name.toLowerCase().replace(/\W/g, "")}">\n          ${opts.map(([v, l]) => `<option value="${v}" ${val === v ? "selected" : ""}>${l}</option>`).join("")}\n        </select>\n      </label>`;
    const chk = (key, label, def) => `\n      <label class="inline">\n        <input type="checkbox" name="${key}" ${cfg[key] ?? def ? "checked" : ""} />\n        <span>${label}</span>\n      </label>`;
    this.shadowRoot.innerHTML = `\n      <style>\n        :host { display: block; }\n        .row { display: flex; flex-direction: column; gap: 14px; padding: 18px; }\n        label {\n          font-size: 14px; color: var(--primary-text-color);\n          display: flex; flex-direction: column; gap: 4px;\n        }\n        label.inline { flex-direction: row; align-items: center; gap: 10px; }\n        select, input[type="text"] {\n          padding: 9px 10px; border-radius: 6px;\n          border: 1px solid var(--divider-color, rgba(120,120,128,.2));\n          background: var(--card-background-color, #fff);\n          color: var(--primary-text-color, #1c1c1e);\n          font: inherit; font-size: 14px;\n        }\n        select:focus, input:focus { outline: 2px solid #0a84ff; outline-offset: 1px; }\n        input[type="checkbox"] { width: 18px; height: 18px; accent-color: #0a84ff; }\n        .hint {\n          font-size: 12px; color: var(--secondary-text-color, #6c6c70);\n          margin-top: 2px;\n        }\n        h4 {\n          margin: 12px 0 0; font-size: 11px; font-weight: 700;\n          letter-spacing: .08em; text-transform: uppercase;\n          color: var(--secondary-text-color, #6c6c70);\n        }\n        .help {\n          font-size: 12px;\n          color: var(--secondary-text-color, #6c6c70);\n          background: var(--secondary-background-color, rgba(120,120,128,.08));\n          padding: 8px 10px; border-radius: 6px;\n        }\n      </style>\n      <div class="row">\n        ${cams.length === 0 ? `\n          <div class="help">Keine Bosch-Kameras erkannt. Trage <code>camera.bosch_xxx</code> manuell ein, oder schließe das Bosch-Integration-Setup zuerst ab.</div>\n        ` : ""}\n        <label>Kamera-Entity *\n          <select name="camera_entity">\n            ${cams.length ? cams.map(id => `<option value="${id}" ${cfg.camera_entity === id ? "selected" : ""}>${id}</option>`).join("") : `<option value="${cfg.camera_entity || ""}" selected>${cfg.camera_entity || "(noch nicht gesetzt)"}</option>`}\n          </select>\n          <span class="hint">Pflichtfeld — alle anderen Entities werden automatisch aus dem Camera-Namen abgeleitet.</span>\n        </label>\n        <label>Titel <small style="color:var(--secondary-text-color)">(optional, überschreibt Friendly-Name)</small>\n          <input type="text" name="title" value="${(cfg.title || "").replace(/"/g, "&quot;")}" placeholder="z.B. Garten" />\n        </label>\n\n        <h4>Design</h4>\n        ${chk("apple_style", "Apple-Style Glass-Overlay aktiv (Default an)", true)}\n        ${sel("Theme", cfg.theme || "ios", [ [ "auto", "Auto (Auto-Detect via User-Agent)" ], [ "ios", "iOS (Apple Home)" ], [ "android", "Android (Material You)" ] ])}\n        ${sel("Modus", cfg.mode || "auto", [ [ "auto", "Auto (System Light/Dark)" ], [ "day", "Tag" ], [ "night", "Nacht" ] ])}\n        ${chk("minimal", "Minimal-Layout (Mehr-Menü versteckt zunächst alle Switches)", false)}\n        ${chk("compact", "Compact-Tile (für Overview-Grid: nur Video + Title-Pill, keine Pill-Bar)", false)}\n        ${chk("show_title", "Titel-Pill anzeigen (aus = nur Video, ohne Namens-Overlay)", true)}\n        ${chk("show_last_event", "Letztes-Ereignis-Badge anzeigen", true)}\n\n        <h4>Auto-Play</h4>\n        ${sel("Auto-Play", cfg.auto_play || "lan", [ [ "lan", "LAN (Auto-Start nur im Heimnetz)" ], [ "always", "Immer" ], [ "never", "Nie (Tap-to-Play Gate)" ] ])}\n        <span class="hint">Steuert wann der Live-Stream automatisch loslegt. Überschreibt die Integration-weite Voreinstellung.</span>\n      </div>`;
    const root = this.shadowRoot;
    const fire = patch => {
      this._config = {
        ...this._config,
        ...patch
      };
      this.dispatchEvent(new CustomEvent("config-changed", {
        detail: {
          config: this._config
        },
        bubbles: true,
        composed: true
      }));
    };
    root.querySelector('select[name="camera_entity"]').addEventListener("change", e => fire({
      camera_entity: e.target.value
    }));
    root.querySelector('input[name="title"]').addEventListener("change", e => fire({
      title: e.target.value || undefined
    }));
    root.querySelector('input[name="apple_style"]').addEventListener("change", e => fire({
      apple_style: e.target.checked
    }));
    root.querySelector('select[name="theme"]').addEventListener("change", e => fire({
      theme: e.target.value
    }));
    root.querySelector('select[name="modus"]').addEventListener("change", e => fire({
      mode: e.target.value
    }));
    root.querySelector('input[name="minimal"]').addEventListener("change", e => fire({
      minimal: e.target.checked
    }));
    root.querySelector('input[name="compact"]').addEventListener("change", e => fire({
      compact: e.target.checked
    }));
    root.querySelector('input[name="show_title"]').addEventListener("change", e => fire({
      show_title: e.target.checked
    }));
    root.querySelector('input[name="show_last_event"]').addEventListener("change", e => fire({
      show_last_event: e.target.checked
    }));
    root.querySelector('select[name="autoplay"]').addEventListener("change", e => fire({
      auto_play: e.target.value
    }));
  }
}

customElements.define("bosch-camera-card-editor", BoschCameraCardEditor);

window.customCards = window.customCards || [];

window.customCards.push({
  type: "bosch-camera-card",
  name: "Bosch Camera Card",
  description: "Bosch Smart Home cameras with streaming state, loading indicator and controls",
  preview: false
});

const OVERVIEW_VERSION = "1.3.0";

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
    this._emptyNode = null;
  }
  setConfig(config) {
    this._config = {
      online_offline_view: config.online_offline_view !== false,
      title: config.title || "",
      min_width: config.min_width || "650px",
      gap: config.gap || "12px",
      columns: config.columns ?? "auto",
      exclude: Array.isArray(config.exclude) ? config.exclude : [],
      include: Array.isArray(config.include) ? config.include : [],
      use_bosch_sort: config.use_bosch_sort === true,
      minimal: config.minimal !== false,
      apple_style: config.apple_style !== false,
      theme: [ "ios", "android", "auto" ].includes(config.theme) ? config.theme : "ios",
      mode: [ "auto", "day", "night" ].includes(config.mode) ? config.mode : "auto",
      compact: config.compact === true,
      show_title: config.show_title !== false,
      show_last_event: config.show_last_event !== false,
      border_radius: typeof config.border_radius === "string" ? config.border_radius : null,
      box_shadow: typeof config.box_shadow === "string" ? config.box_shadow : null,
      overrides: config.overrides && typeof config.overrides === "object" ? config.overrides : {},
      card_defaults: config.card_defaults && typeof config.card_defaults === "object" ? config.card_defaults : {}
    };
    if (this._config.minimal) {
      this._config.card_defaults = {
        ...this._config.card_defaults,
        minimal: true
      };
    }
    if (this._config.border_radius) this.style.setProperty("--bosch-card-radius", this._config.border_radius); else this.style.removeProperty("--bosch-card-radius");
    if (this._config.box_shadow) this.style.setProperty("--bosch-card-shadow", this._config.box_shadow); else this.style.removeProperty("--bosch-card-shadow");
    this._config.card_defaults = {
      apple_style: this._config.apple_style,
      theme: this._config.theme,
      mode: this._config.mode,
      compact: this._config.compact,
      show_title: this._config.show_title,
      show_last_event: this._config.show_last_event,
      ...this._config.card_defaults
    };
    this.classList.toggle("apple-style", this._config.apple_style);
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
    this.shadowRoot.innerHTML = `\n      <style>\n        :host { display: block; }\n        .bco-wrap { display: block; padding: 4px; overflow: visible; }\n        .bco-header {\n          display: flex; align-items: center; justify-content: space-between;\n          padding: 0 4px 8px; font-size: 14px; font-weight: 500;\n          color: var(--primary-text-color);\n        }\n        .bco-count {\n          font-size: 12px; font-weight: 400;\n          color: var(--secondary-text-color);\n        }\n        .bco-grid {\n          display: grid;\n          gap: ${this._config.gap};\n          grid-template-columns: ${this._config.columns === "auto" || !this._config.columns ? `repeat(auto-fill, minmax(min(${this._config.min_width}, 100%), 1fr))` : `repeat(${Number(this._config.columns)}, minmax(0, 1fr))`};\n        }\n        @media (max-width: 640px) {\n          .bco-grid { grid-template-columns: 1fr !important; }\n        }\n        /* Phones in landscape (e.g. iPhone Pro Max ≈ 932 × 430) are wider\n           than 640px but the viewport height collapses below ~500px — at\n           that aspect a 2-column tile grid leaves each tile ~12 lines tall\n           which is unusable. Force single column when any of:\n             - touch device up to small-tablet width (1024px), or\n             - landscape with very short viewport (any device).\n           Desktop browsers resized narrow keep their multi-column layout. */\n        @media (pointer: coarse) and (max-width: 1024px) {\n          .bco-grid { grid-template-columns: 1fr !important; }\n        }\n        @media (orientation: landscape) and (max-height: 500px) {\n          .bco-grid { grid-template-columns: 1fr !important; }\n        }\n        .bco-cell {\n          min-width: 0;\n          position: relative;\n          border-radius: 14px;\n          border: 2px solid transparent;\n          overflow: hidden;\n          transition: border-color 0.2s ease;\n        }\n        .bco-cell[data-tier="0"] { border-color: rgba(76, 175, 80, 0.55); }\n        .bco-cell[data-tier="1"] { border-color: rgba(255, 152, 0, 0.55); }\n        .bco-cell[data-tier="2"] { border-color: rgba(120, 120, 120, 0.35); opacity: 0.92; }\n        /* Apple-style: drop the saturated tier borders + opacity dim. Tier\n           info already shows in the inner card's glass status dot + badge,\n           so the wrapping border just adds visual noise that clashes with\n           the soft Apple aesthetic. The cell still gets a generous border\n           radius so corner cropping matches the inner card. */\n        :host(.apple-style) .bco-cell,\n        :host(.apple-style) .bco-cell[data-tier="0"],\n        :host(.apple-style) .bco-cell[data-tier="1"],\n        :host(.apple-style) .bco-cell[data-tier="2"] {\n          border: 0;\n          border-radius: var(--bosch-card-radius, var(--ha-card-border-radius, 22px));\n          /* Shadow lives on the CELL, not the inner card: the cell's\n             overflow:hidden (for corner-cropping) would clip the inner card's\n             box-shadow, so a themed ha-card-box-shadow never showed on overview\n             tiles (issue #21, RkcCorian). An element's own outset shadow is not\n             clipped by its own overflow:hidden. Default none = unchanged look. */\n          box-shadow: var(--bosch-card-shadow, var(--ha-card-box-shadow, none));\n          opacity: 1;\n          /* Smooth scale on hover so desktop users get a clear\n             "this tile is tappable" affordance. Touch devices ignore :hover\n             so the static state stays unchanged on mobile. transform-origin:top\n             anchors the TOP edge, so the scale grows downward — the tile no\n             longer "jumps" up when the inner card expands via ⋮ (issue #15.3)\n             while keeping the scale effect RkcCorian liked. Uses transform\n             ONLY — NOT box-shadow — so a themed --ha-card-box-shadow stays\n             visible during the lift, exactly like the single card (issue #15,\n             RkcCorian: theme variables were ignored during the overview lift). */\n          transform-origin: top center;\n          transition: transform .18s ease;\n        }\n        @media (hover: hover) and (pointer: fine) {\n          :host(.apple-style) .bco-cell:hover {\n            transform: translateY(-2px) scale(1.012);\n            z-index: 1;\n          }\n        }\n        .bco-cell bosch-camera-card { display: block; min-width: 0; }\n        .bco-section {\n          grid-column: 1 / -1;\n          font-size: 11px;\n          font-weight: 600;\n          letter-spacing: 0.08em;\n          text-transform: uppercase;\n          color: var(--secondary-text-color);\n          padding: 8px 4px 2px;\n          border-top: 1px solid var(--divider-color, rgba(255,255,255,0.1));\n          margin-top: 4px;\n        }\n        .bco-section.first { border-top: none; margin-top: 0; padding-top: 2px; }\n        .bco-empty {\n          grid-column: 1 / -1;\n          padding: 24px 12px;\n          text-align: center;\n          color: var(--secondary-text-color);\n          font-size: 14px;\n        }\n        .bco-empty.bco-empty-outage {\n          padding: 24px 16px;\n          color: var(--primary-text-color);\n        }\n        .bco-empty-title {\n          font-size: 15px;\n          font-weight: 500;\n          margin-bottom: 6px;\n        }\n        .bco-empty-sub {\n          font-size: 13px;\n          color: var(--secondary-text-color);\n          margin-top: 4px;\n        }\n        .bco-empty-link {\n          display: inline-block;\n          margin-top: 10px;\n          color: var(--primary-color);\n          text-decoration: none;\n          font-size: 13px;\n        }\n        .bco-empty-link:hover { text-decoration: underline; }\n        .bco-banner {\n          display: flex;\n          flex-direction: column;\n          gap: 4px;\n          padding: 10px 12px;\n          margin-bottom: 8px;\n          border-radius: 8px;\n          background: var(--warning-color, #ffc107);\n          color: #000;\n          font-size: 13px;\n          line-height: 1.35;\n        }\n        .bco-banner.bco-banner-info {\n          background: var(--info-color, var(--primary-color));\n          color: var(--text-primary-color, #fff);\n        }\n        .bco-banner-title { font-weight: 600; }\n        .bco-banner a {\n          color: inherit;\n          text-decoration: underline;\n          font-size: 12px;\n        }\n        .bco-lan-tiles {\n          display: grid;\n          grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));\n          gap: 8px;\n          margin-bottom: 10px;\n        }\n        .bco-lan-tile {\n          display: flex;\n          flex-direction: column;\n          gap: 6px;\n          padding: 10px 12px;\n          border-radius: 8px;\n          background: var(--card-background-color, #1c1c1c);\n          border: 1px solid var(--divider-color, rgba(255,255,255,0.1));\n          font-size: 13px;\n        }\n        .bco-lan-tile-header {\n          display: flex;\n          align-items: center;\n          gap: 8px;\n          font-weight: 600;\n        }\n        .bco-lan-dot {\n          width: 10px;\n          height: 10px;\n          border-radius: 50%;\n          background: var(--state-inactive-color, #888);\n          flex-shrink: 0;\n        }\n        .bco-lan-dot.bco-lan-on { background: var(--success-color, #4caf50); }\n        .bco-lan-dot.bco-lan-off { background: var(--error-color, #f44336); }\n        .bco-lan-controls {\n          display: flex;\n          gap: 6px;\n          flex-wrap: wrap;\n        }\n        .bco-lan-btn {\n          flex: 1 1 auto;\n          padding: 6px 10px;\n          border-radius: 6px;\n          border: 1px solid var(--divider-color, rgba(255,255,255,0.1));\n          background: var(--secondary-background-color, #2c2c2c);\n          color: var(--primary-text-color, #fff);\n          font-size: 12px;\n          cursor: pointer;\n          white-space: nowrap;\n        }\n        .bco-lan-btn:hover:not(:disabled) {\n          background: var(--primary-color);\n          color: var(--text-primary-color, #fff);\n        }\n        .bco-lan-btn:disabled {\n          opacity: 0.4;\n          cursor: not-allowed;\n        }\n        .bco-lan-btn.bco-lan-btn-on {\n          background: var(--state-active-color, var(--primary-color));\n          color: var(--text-primary-color, #fff);\n        }\n        bosch-camera-card { display: block; }\n        @media (max-width: 480px) {\n          .bco-grid { gap: 8px; }\n        }\n      </style>\n      <div class="bco-wrap">\n        ${this._config.title ? `\n          <div class="bco-header">\n            <span>${this._escape(this._config.title)}</span>\n            <span class="bco-count" id="bco-count"></span>\n          </div>` : ""}\n        <div id="bco-banner-slot"></div>\n        <div id="bco-lan-tiles-slot"></div>\n        <div class="bco-grid" id="bco-grid"></div>\n      </div>\n    `;
    this._grid = this.shadowRoot.getElementById("bco-grid");
    this._countEl = this.shadowRoot.getElementById("bco-count");
    this._bannerSlot = this.shadowRoot.getElementById("bco-banner-slot");
    this._lanTilesSlot = this.shadowRoot.getElementById("bco-lan-tiles-slot");
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
  _renderLanTiles() {
    if (!this._lanTilesSlot) return;
    const states = this._hass?.states || {};
    const unavailableBosch = Object.keys(states).filter(eid => eid.startsWith("camera.bosch_") && states[eid]?.state === "unavailable");
    if (unavailableBosch.length === 0) {
      if (this._lanTilesSlot.firstChild) this._lanTilesSlot.innerHTML = "";
      this._lanTilesSlot.dataset.sig = "";
      return;
    }
    const tiles = [];
    for (const camEid of unavailableBosch) {
      const slug = camEid.replace(/^camera\.bosch_/, "");
      const camFriendly = states[camEid]?.attributes?.friendly_name || `Bosch ${slug}`;
      const findByFriendlyPrefix = (domain, suffix) => {
        const direct = states[`${domain}.bosch_${slug}${suffix.entityId}`];
        if (direct) return direct;
        const target = `${camFriendly} ${suffix.friendly}`.toLowerCase();
        return Object.values(states).find(s => {
          if (!s.entity_id.startsWith(`${domain}.`)) return false;
          const fn = (s.attributes?.friendly_name || "").toLowerCase();
          return fn === target || fn.startsWith(target);
        });
      };
      const lan = findByFriendlyPrefix("binary_sensor", {
        entityId: "_lan_reachable",
        friendly: "LAN"
      });
      const privacy = findByFriendlyPrefix("switch", {
        entityId: "_privacy_mode",
        friendly: "Privacy Mode"
      });
      const light = findByFriendlyPrefix("light", {
        entityId: "_front_light",
        friendly: "Front Light"
      });
      tiles.push({
        camEid: camEid,
        slug: slug,
        friendly: camFriendly,
        lan: lan,
        privacy: privacy,
        light: light
      });
    }
    const sig = tiles.map(t => `${t.slug}|${t.lan?.state}|${t.privacy?.state}|${t.privacy?.attributes?.icon}|${t.light?.state}`).join("#");
    if (this._lanTilesSlot.dataset.sig === sig) return;
    this._lanTilesSlot.dataset.sig = sig;
    this._lanTilesSlot.innerHTML = "";
    const grid = document.createElement("div");
    grid.className = "bco-lan-tiles";
    for (const t of tiles) {
      const tile = document.createElement("div");
      tile.className = "bco-lan-tile";
      const header = document.createElement("div");
      header.className = "bco-lan-tile-header";
      const dot = document.createElement("span");
      dot.className = "bco-lan-dot";
      const lanState = t.lan?.state;
      if (lanState === "on") dot.classList.add("bco-lan-on"); else if (lanState === "off") dot.classList.add("bco-lan-off");
      header.appendChild(dot);
      const nameEl = document.createElement("span");
      nameEl.textContent = t.friendly.replace(/^Bosch\s+/, "");
      header.appendChild(nameEl);
      tile.appendChild(header);
      const status = document.createElement("div");
      status.style.cssText = "font-size:11px;color:var(--secondary-text-color);";
      status.textContent = lanState === "on" ? "LAN erreichbar" : lanState === "off" ? "LAN nicht erreichbar" : "LAN-Status unbekannt";
      tile.appendChild(status);
      const controls = document.createElement("div");
      controls.className = "bco-lan-controls";
      const addBtn = (label, entity, domain) => {
        const btn = document.createElement("button");
        btn.className = "bco-lan-btn";
        btn.type = "button";
        const reachable = lanState === "on";
        const entOk = entity && entity.state !== "unavailable" && entity.state !== "unknown";
        const isOn = entity && entity.state === "on";
        if (isOn) btn.classList.add("bco-lan-btn-on");
        btn.disabled = !entOk || !reachable;
        btn.title = !reachable ? "Kamera lokal nicht erreichbar" : !entOk ? "Status unbekannt — Cloud-Daten fehlen" : `${label} ${isOn ? "AUS" : "AN"} schalten`;
        btn.textContent = `${label}${isOn ? " AN" : ""}`;
        if (entity) {
          btn.addEventListener("click", () => {
            this._hass.callService(domain, "toggle", {
              entity_id: entity.entity_id
            });
          });
        }
        controls.appendChild(btn);
      };
      addBtn("Privacy", t.privacy, "switch");
      if (t.light) addBtn("Licht", t.light, "light");
      tile.appendChild(controls);
      grid.appendChild(tile);
    }
    this._lanTilesSlot.appendChild(grid);
  }
  _renderMaintenanceBanner() {
    if (!this._bannerSlot) return;
    const states = this._hass?.states || {};
    const maint = Object.values(states).find(s => {
      const src = s?.attributes?.source;
      return typeof src === "string" && /^(rss|html):/.test(src);
    });
    const mState = maint?.state || "";
    const mAttr = maint?.attributes || {};
    const show = (mState === "active" || mState === "scheduled") && mAttr.camera_relevant;
    if (!show) {
      if (this._bannerSlot.firstChild) this._bannerSlot.innerHTML = "";
      this._bannerSlot.dataset.sig = "";
      return;
    }
    const isActive = mState === "active";
    const win = this._formatWindow(mAttr.scheduled_start, mAttr.scheduled_end);
    const sig = `${mState}|${mAttr.title}|${win}`;
    if (this._bannerSlot.dataset.sig === sig) return;
    this._bannerSlot.dataset.sig = sig;
    this._bannerSlot.innerHTML = "";
    const banner = document.createElement("div");
    banner.className = isActive ? "bco-banner" : "bco-banner bco-banner-info";
    const t = document.createElement("div");
    t.className = "bco-banner-title";
    t.textContent = isActive ? "Bosch-Cloud-Wartung läuft" : "Bosch-Cloud-Wartung geplant";
    const sub = document.createElement("div");
    sub.textContent = win ? `${mAttr.title || "Wartungsmeldung"} · ${win}` : mAttr.title || "Wartungsmeldung";
    banner.appendChild(t);
    banner.appendChild(sub);
    if (isActive) {
      const note = document.createElement("div");
      note.textContent = "Live-Bild und Snapshots können in diesem Zeitfenster eingeschränkt sein.";
      banner.appendChild(note);
    }
    if (mAttr.link && /^https:\/\//i.test(mAttr.link)) {
      const a = document.createElement("a");
      a.href = mAttr.link;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = "Details in der Bosch Community";
      banner.appendChild(a);
    }
    this._bannerSlot.appendChild(banner);
  }
  _formatWindow(startIso, endIso) {
    if (!startIso || !endIso) return "";
    try {
      const s = new Date(startIso);
      const e = new Date(endIso);
      if (isNaN(s) || isNaN(e)) return "";
      const date = s.toLocaleDateString("de-DE", {
        weekday: "short",
        day: "2-digit",
        month: "2-digit",
        year: "numeric"
      });
      const fmt = d => d.toLocaleTimeString("de-DE", {
        hour: "2-digit",
        minute: "2-digit"
      });
      return `${date} ${fmt(s)}–${fmt(e)}`;
    } catch (_) {
      return "";
    }
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
    this._renderMaintenanceBanner();
    this._renderLanTiles();
    let cams = this._discover();
    if (!this._config.online_offline_view) cams = cams.filter(c => c.online);
    const sig = cams.map(c => `${c.entity_id}:${c.tier}:${c.streamingOn ? "S" : ""}`).join("|");
    const gridEmpty = this._grid && this._grid.children.length === 0;
    const needsReorder = sig !== this._lastSig || gridEmpty;
    this._lastSig = sig;
    const keep = new Set(cams.map(c => c.entity_id));
    for (const [eid, el] of [ ...this._cards.entries() ]) {
      if (!keep.has(eid)) {
        el.remove();
        this._cards.delete(eid);
      }
    }
    if (needsReorder) {
      if (this._emptyNode) {
        this._emptyNode.remove();
        this._emptyNode = null;
      }
      if (cams.length === 0) {
        const empty = document.createElement("div");
        const states = this._hass?.states || {};
        const unavailableBosch = Object.keys(states).filter(eid => eid.startsWith("camera.bosch_") && states[eid]?.state === "unavailable");
        if (unavailableBosch.length > 0) {
          empty.className = "bco-empty bco-empty-outage";
          const maint = Object.values(states).find(s => {
            const src = s?.attributes?.source;
            return typeof src === "string" && /^(rss|html):/.test(src);
          });
          const mState = maint?.state || "";
          const mAttr = maint?.attributes || {};
          const titleEl = document.createElement("div");
          titleEl.className = "bco-empty-title";
          const sub = document.createElement("div");
          sub.className = "bco-empty-sub";
          const link = document.createElement("a");
          link.className = "bco-empty-link";
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          const sub2 = document.createElement("div");
          sub2.className = "bco-empty-sub";
          if ((mState === "active" || mState === "scheduled" || mState === "recent") && mAttr.camera_relevant) {
            const verb = mState === "active" ? "läuft" : mState === "scheduled" ? "geplant" : "angekündigt";
            titleEl.textContent = `Bosch-Cloud-Wartung ${verb}`;
            const win = this._formatWindow(mAttr.scheduled_start, mAttr.scheduled_end);
            sub.textContent = win ? `${mAttr.title || "Wartungsmeldung"} · ${win}` : mAttr.title || "Wartungsmeldung";
            link.href = mAttr.link && /^https:\/\//i.test(mAttr.link) ? mAttr.link : "https://www.bosch-smarthome.com/service";
            link.textContent = "Details in der Bosch Community";
            sub2.textContent = `${unavailableBosch.length} ${unavailableBosch.length === 1 ? "Kamera" : "Kameras"} ` + "kommen automatisch zurück, sobald die Cloud antwortet.";
          } else {
            titleEl.textContent = "Bosch-Cloud nicht erreichbar";
            sub.textContent = `${unavailableBosch.length} ${unavailableBosch.length === 1 ? "Kamera" : "Kameras"} ` + "warten auf die Bosch-Server.";
            link.href = "https://community.bosch-smarthome.com/t5/wartungsarbeiten/bg-p/Wartungsarbeiten";
            link.textContent = "Status prüfen: Bosch Community";
            sub2.textContent = "Die Kameras kommen automatisch zurück, sobald die Cloud antwortet.";
          }
          empty.appendChild(titleEl);
          empty.appendChild(sub);
          empty.appendChild(link);
          empty.appendChild(sub2);
        } else {
          empty.className = "bco-empty";
          empty.textContent = "Keine Bosch-Kameras gefunden.";
        }
        this._grid.appendChild(empty);
        this._emptyNode = empty;
      } else {
        let _ord = 0;
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
          cell.style.order = String(_ord++);
          if (cell.parentNode !== this._grid) this._grid.appendChild(cell);
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
  static getConfigElement() {
    return document.createElement("bosch-camera-overview-card-editor");
  }
  getCardSize() {
    return Math.max(4, this._cards ? this._cards.size * 3 : 4);
  }
}

class BoschCameraOverviewCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    if (this.shadowRoot) this._render();
  }
  connectedCallback() {
    this._render();
  }
  _render() {
    if (!this.shadowRoot) this.attachShadow({
      mode: "open"
    });
    const cfg = this._config || {};
    const sel = v => cfg.columns == v || v === "auto" && (cfg.columns === "auto" || cfg.columns == null) ? "selected" : "";
    const isAuto = cfg.columns === "auto" || cfg.columns == null;
    const minW = cfg.min_width || "650px";
    const minWpx = parseInt(minW) || 650;
    const seldd = (name, val, opts) => `\n      <label>${name}\n        <select name="${name.toLowerCase().replace(/\W/g, "")}">\n          ${opts.map(([v, l]) => `<option value="${v}" ${val === v ? "selected" : ""}>${l}</option>`).join("")}\n        </select>\n      </label>`;
    const chk = (key, label, def) => `\n      <label class="inline">\n        <input type="checkbox" name="${key}" ${cfg[key] ?? def ? "checked" : ""} />\n        <span>${label}</span>\n      </label>`;
    this.shadowRoot.innerHTML = `\n      <style>\n        .row{display:flex;flex-direction:column;gap:12px;padding:16px}\n        label{font-size:14px;color:var(--primary-text-color);display:flex;flex-direction:column;gap:4px}\n        label.inline{flex-direction:row;align-items:center;gap:10px}\n        select,input[type="text"],input[type="number"]{padding:8px;border-radius:4px;border:1px solid var(--divider-color);\n          background:var(--card-background-color);color:var(--primary-text-color);font-size:14px}\n        input[type="checkbox"]{width:18px;height:18px;accent-color:#0a84ff}\n        .hint{font-size:12px;color:var(--secondary-text-color)}\n        h4{margin:12px 0 0;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--secondary-text-color)}\n        [hidden]{display:none}\n      </style>\n      <div class="row">\n        <label>Spalten\n          <select name="columns">\n            <option value="auto" ${sel("auto")}>Auto (Breakpoint)</option>\n            <option value="1" ${sel(1)}>1 – volle Breite</option>\n            <option value="2" ${sel(2)}>2</option>\n            <option value="3" ${sel(3)}>3</option>\n            <option value="4" ${sel(4)}>4</option>\n          </select>\n        </label>\n        <label id="minw-row" ${isAuto ? "" : "hidden"}>Breakpoint – Mindestbreite pro Kachel (px)\n          <input type="number" name="min_width" value="${minWpx}" min="200" max="900" step="10" />\n          <span class="hint">Bei Auto: 1 Spalte unter, 2+ Spalten über diesem Wert. Standard: 650 px</span>\n        </label>\n        <label>Titel <small style="color:var(--secondary-text-color)">(optional)</small>\n          <input type="text" name="title" value="${(cfg.title || "").replace(/"/g, "&quot;")}" placeholder="Bosch Kameras" />\n        </label>\n\n        <h4>Anzeige</h4>\n        ${chk("online_offline_view", "Offline-Kameras anzeigen", true)}\n        ${chk("use_bosch_sort", "Nach Bosch-App-Reihenfolge sortieren", false)}\n\n        <h4>Design (für alle Kacheln)</h4>\n        ${chk("apple_style", "Apple-Style Glass-Overlay aktiv (Default an)", true)}\n        ${seldd("Theme", cfg.theme || "ios", [ [ "auto", "Auto (User-Agent)" ], [ "ios", "iOS (Apple Home)" ], [ "android", "Android (Material You)" ] ])}\n        ${seldd("Modus", cfg.mode || "auto", [ [ "auto", "Auto (System Light/Dark)" ], [ "day", "Tag" ], [ "night", "Nacht" ] ])}\n        ${chk("compact", "Compact-Tile (nur Video + Title-Pill, keine Pill-Bar)", false)}\n        ${chk("minimal", "Minimal-Layout (Switches hinter dem Mehr-Menü) — empfohlen fürs Grid", true)}\n        ${chk("show_title", "Titel-Pill anzeigen (aus = nur Video, ohne Namens-Overlay)", true)}\n        ${chk("show_last_event", "Letztes-Ereignis-Badge anzeigen", true)}\n      </div>`;
    const colSel = this.shadowRoot.querySelector('select[name="columns"]');
    const minwRow = this.shadowRoot.getElementById("minw-row");
    colSel.addEventListener("change", e => {
      const v = e.target.value;
      minwRow.hidden = v !== "auto";
      this._fire({
        ...this._config,
        columns: v === "auto" ? "auto" : Number(v)
      });
    });
    this.shadowRoot.querySelector('input[name="min_width"]').addEventListener("change", e => {
      const px = Math.max(200, Math.min(900, Number(e.target.value) || 360));
      this._fire({
        ...this._config,
        min_width: `${px}px`
      });
    });
    this.shadowRoot.querySelector('input[name="title"]').addEventListener("change", e => {
      this._fire({
        ...this._config,
        title: e.target.value
      });
    });
    const onChk = (name, key) => this.shadowRoot.querySelector(`input[name="${name}"]`).addEventListener("change", e => this._fire({
      ...this._config,
      [key]: e.target.checked
    }));
    onChk("online_offline_view", "online_offline_view");
    onChk("use_bosch_sort", "use_bosch_sort");
    onChk("apple_style", "apple_style");
    onChk("compact", "compact");
    onChk("minimal", "minimal");
    onChk("show_title", "show_title");
    onChk("show_last_event", "show_last_event");
    this.shadowRoot.querySelector('select[name="theme"]').addEventListener("change", e => this._fire({
      ...this._config,
      theme: e.target.value
    }));
    this.shadowRoot.querySelector('select[name="modus"]').addEventListener("change", e => this._fire({
      ...this._config,
      mode: e.target.value
    }));
  }
  _fire(config) {
    this._config = config;
    this.dispatchEvent(new CustomEvent("config-changed", {
      detail: {
        config: config
      },
      bubbles: true,
      composed: true
    }));
  }
}

customElements.define("bosch-camera-overview-card-editor", BoschCameraOverviewCardEditor);

customElements.define("bosch-camera-overview-card", BoschCameraOverviewCard);

window.customCards.push({
  type: "bosch-camera-overview-card",
  name: "Bosch Camera Overview",
  description: "Auto-discovers all Bosch Smart Home cameras and renders them in a responsive grid (online first, offline after).",
  preview: false
});

class BoschNvrTimelineCard extends HTMLElement {
  setConfig(config) {
    if (!config.nvr_source_id) throw new Error("nvr_source_id is required");
    this._config = config;
    if (this.isConnected) this._init();
  }
  set hass(hass) {
    this._hass = hass;
    if (!this._initialized) this._init();
  }
  connectedCallback() {
    if (this._hass && !this._initialized) this._init();
  }
  disconnectedCallback() {
    this._stopCursor();
  }
  _init() {
    if (!this._hass || !this._config) return;
    this._initialized = true;
    this._segments = [];
    this._motionEvents = [];
    this._currentDate = (new Date).toISOString().slice(0, 10);
    this._render();
    this._loadDay(this._currentDate);
    if (this._config.motion_entity) this._loadMotion(this._currentDate);
  }
  _render() {
    if (!this.shadowRoot) this.attachShadow({
      mode: "open"
    });
    this.shadowRoot.innerHTML = `\n      <style>\n        :host{display:block;background:var(--card-background-color);border-radius:12px;overflow:hidden}\n        .header{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;\n          background:var(--primary-color);color:var(--text-primary-color);font-size:14px;font-weight:500}\n        .nav-btn{background:none;border:none;color:inherit;cursor:pointer;font-size:18px;padding:4px 8px}\n        .date-label{flex:1;text-align:center}\n        canvas{width:100%;height:48px;display:block;cursor:pointer;background:#111}\n        video{width:100%;max-height:340px;display:block;background:#000}\n        .status{padding:8px 16px;font-size:12px;color:var(--secondary-text-color)}\n        .no-data{padding:16px;text-align:center;color:var(--secondary-text-color)}\n      </style>\n      <div class="header">\n        <button class="nav-btn" id="prev">&#8249;</button>\n        <span class="date-label" id="date-lbl">${this._currentDate}</span>\n        <button class="nav-btn" id="next">&#8250;</button>\n      </div>\n      <canvas id="timeline" height="48"></canvas>\n      <video id="player" controls preload="none" playsinline></video>\n      <div class="status" id="status">Lade Aufnahmen…</div>`;
    const canvas = this.shadowRoot.getElementById("timeline");
    canvas.addEventListener("click", e => this._onCanvasClick(e));
    this.shadowRoot.getElementById("prev").addEventListener("click", () => this._changeDay(-1));
    this.shadowRoot.getElementById("next").addEventListener("click", () => this._changeDay(1));
    const video = this.shadowRoot.getElementById("player");
    video.addEventListener("timeupdate", () => this._drawTimeline());
  }
  _changeDay(delta) {
    const d = new Date(this._currentDate + "T00:00:00");
    d.setDate(d.getDate() + delta);
    this._currentDate = d.toISOString().slice(0, 10);
    this.shadowRoot.getElementById("date-lbl").textContent = this._currentDate;
    this._segments = [];
    this._motionEvents = [];
    this._loadDay(this._currentDate);
    if (this._config.motion_entity) this._loadMotion(this._currentDate);
  }
  async _loadDay(dateStr) {
    if (!this._hass) return;
    const camPart = this._config.nvr_source_id;
    const mediaId = camPart.replace(/\/\d{4}-\d{2}-\d{2}$/, "") + "/" + dateStr;
    try {
      const result = await this._hass.callWS({
        type: "media_source/browse_media",
        media_content_id: mediaId
      });
      this._segments = (result.children || []).filter(c => c.media_class === "video");
      this._drawTimeline();
      const status = this.shadowRoot.getElementById("status");
      status.textContent = this._segments.length ? `${this._segments.length} Segment(e) — klicken zum Abspielen` : "Keine Aufnahmen für diesen Tag";
    } catch (err) {
      const status = this.shadowRoot.getElementById("status");
      status.textContent = "Fehler beim Laden der Segmente";
    }
  }
  async _loadMotion(dateStr) {
    if (!this._hass || !this._config.motion_entity) return;
    const start = dateStr + "T00:00:00+00:00";
    const end = dateStr + "T23:59:59+00:00";
    try {
      const result = await this._hass.callApi("GET", `history/period/${start}?end_time=${end}&filter_entity_id=${this._config.motion_entity}`);
      const states = (result || [])[0] || [];
      this._motionEvents = states.filter(s => s.state === "on").map(s => {
        const t = new Date(s.last_changed);
        return (t.getHours() * 3600 + t.getMinutes() * 60 + t.getSeconds()) / 86400;
      });
      this._drawTimeline();
    } catch (_) {}
  }
  _drawTimeline() {
    const canvas = this.shadowRoot && this.shadowRoot.getElementById("timeline");
    if (!canvas) return;
    const W = canvas.offsetWidth || canvas.width || 600;
    canvas.width = W;
    const H = canvas.height;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, W, H);
    for (const seg of this._segments) {
      const pos = this._segmentTimeOffset(seg);
      if (pos === null) continue;
      const x = Math.floor(pos.start * W);
      const w = Math.max(2, Math.floor(pos.duration * W));
      ctx.fillStyle = "rgba(76,175,80,0.7)";
      ctx.fillRect(x, 2, w, H - 4);
    }
    ctx.fillStyle = "rgba(244,67,54,0.85)";
    for (const frac of this._motionEvents) {
      const x = Math.floor(frac * W);
      ctx.fillRect(x - 1, 0, 2, H);
    }
    ctx.strokeStyle = "rgba(255,255,255,0.15)";
    ctx.lineWidth = 1;
    for (let h = 1; h < 24; h++) {
      const x = Math.floor(h / 24 * W);
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, H);
      ctx.stroke();
    }
    const video = this.shadowRoot && this.shadowRoot.getElementById("player");
    if (video && !isNaN(video.duration) && video.currentTime > 0) {
      const activeSeg = this._activeSegment;
      if (activeSeg) {
        const pos = this._segmentTimeOffset(activeSeg);
        if (pos) {
          const frac = pos.start + video.currentTime / 86400;
          const x = Math.floor(frac * W);
          ctx.strokeStyle = "#fff";
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.moveTo(x, 0);
          ctx.lineTo(x, H);
          ctx.stroke();
        }
      }
    }
  }
  _segmentTimeOffset(seg) {
    if (!seg.title) return null;
    const m = seg.title.match(/(\d{2})[:-](\d{2})/);
    if (!m) return null;
    const start = (parseInt(m[1]) * 60 + parseInt(m[2])) * 60 / 86400;
    const duration = 300 / 86400;
    return {
      start: start,
      duration: duration
    };
  }
  async _onCanvasClick(e) {
    const canvas = e.currentTarget;
    const frac = e.offsetX / canvas.offsetWidth;
    const offsetSeconds = Math.floor(frac * 86400);
    let best = null;
    let bestDelta = Infinity;
    for (const seg of this._segments) {
      const pos = this._segmentTimeOffset(seg);
      if (!pos) continue;
      const segStart = pos.start * 86400;
      const segEnd = segStart + pos.duration * 86400;
      if (offsetSeconds >= segStart && offsetSeconds <= segEnd) {
        best = seg;
        break;
      }
      const delta = Math.abs(segStart - offsetSeconds);
      if (delta < bestDelta) {
        bestDelta = delta;
        best = seg;
      }
    }
    if (best) {
      this._activeSegment = best;
      await this._playSegment(best.media_content_id);
    }
  }
  async _playSegment(mediaContentId) {
    if (!this._hass) return;
    const status = this.shadowRoot.getElementById("status");
    status.textContent = "Lade Stream…";
    try {
      const result = await this._hass.callWS({
        type: "media_source/resolve_media",
        media_content_id: mediaContentId
      });
      const video = this.shadowRoot.getElementById("player");
      video.src = result.url;
      video.load();
      video.play().catch(() => {});
      status.textContent = "Wiedergabe";
    } catch (err) {
      status.textContent = "Fehler beim Laden des Segments";
    }
  }
  _stopCursor() {
    if (this._cursorRaf) {
      cancelAnimationFrame(this._cursorRaf);
      this._cursorRaf = null;
    }
  }
  getCardSize() {
    return 4;
  }
}

customElements.define("bosch-nvr-timeline-card", BoschNvrTimelineCard);

window.customCards.push({
  type: "bosch-nvr-timeline-card",
  name: "Bosch NVR Timeline",
  description: "24-hour timeline scrubber for Mini-NVR recordings. Click a segment to play it.",
  preview: false
});

class BoschNvrMultiCamCard extends HTMLElement {
  setConfig(config) {
    if (!Array.isArray(config.cameras) || !config.cameras.length) {
      throw new Error("cameras array is required");
    }
    this._config = config;
    if (this.isConnected) this._initRows();
  }
  set hass(hass) {
    this._hass = hass;
    if (!this._initialized) this._initRows();
    if (this._rowCards) {
      for (const card of this._rowCards) card.hass = hass;
    }
  }
  connectedCallback() {
    if (this._hass && !this._initialized) this._initRows();
  }
  disconnectedCallback() {
    this._stopDriftCorrection();
  }
  _initRows() {
    if (!this._hass || !this._config) return;
    this._initialized = true;
    this._rowCards = [];
    this._currentDate = (new Date).toISOString().slice(0, 10);
    if (!this.shadowRoot) this.attachShadow({
      mode: "open"
    });
    this.shadowRoot.innerHTML = `\n      <style>\n        :host{display:block;background:var(--card-background-color);border-radius:12px;overflow:hidden}\n        .multi-header{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;\n          background:var(--primary-color);color:var(--text-primary-color);font-size:14px;font-weight:500}\n        .nav-btn{background:none;border:none;color:inherit;cursor:pointer;font-size:18px;padding:4px 8px}\n        .date-label{flex:1;text-align:center}\n        .cam-row{border-bottom:1px solid var(--divider-color);padding:8px 0}\n        .cam-label{padding:4px 16px;font-size:12px;font-weight:500;color:var(--secondary-text-color);\n          text-transform:uppercase;letter-spacing:0.05em}\n      </style>\n      <div class="multi-header">\n        <button class="nav-btn" id="prev">&#8249;</button>\n        <span class="date-label" id="date-lbl">${this._currentDate}</span>\n        <button class="nav-btn" id="next">&#8250;</button>\n      </div>\n      <div id="rows"></div>`;
    this.shadowRoot.getElementById("prev").addEventListener("click", () => this._changeDay(-1));
    this.shadowRoot.getElementById("next").addEventListener("click", () => this._changeDay(1));
    const rowsEl = this.shadowRoot.getElementById("rows");
    for (const camCfg of this._config.cameras) {
      const rowEl = document.createElement("div");
      rowEl.className = "cam-row";
      if (camCfg.label) {
        const lbl = document.createElement("div");
        lbl.className = "cam-label";
        lbl.textContent = camCfg.label;
        rowEl.appendChild(lbl);
      }
      const timelineCard = document.createElement("bosch-nvr-timeline-card");
      timelineCard.setConfig({
        camera_entity: camCfg.camera_entity,
        nvr_source_id: camCfg.nvr_source_id + "/" + this._currentDate,
        motion_entity: camCfg.motion_entity,
        show_date: false
      });
      timelineCard.hass = this._hass;
      rowEl.appendChild(timelineCard);
      rowsEl.appendChild(rowEl);
      this._rowCards.push(timelineCard);
    }
    this._patchSharedSeek();
    this._startDriftCorrection();
  }
  _patchSharedSeek() {
    for (const card of this._rowCards) {
      card._onCanvasClick = async e => {
        const canvas = e.currentTarget;
        const frac = e.offsetX / canvas.offsetWidth;
        const offsetSeconds = Math.floor(frac * 86400);
        await this._seekAll(this._currentDate, offsetSeconds);
      };
    }
  }
  async _seekAll(dateStr, offsetSeconds) {
    const promises = this._rowCards.map(card => {
      let best = null;
      let bestDelta = Infinity;
      for (const seg of card._segments || []) {
        const pos = card._segmentTimeOffset(seg);
        if (!pos) continue;
        const segStart = pos.start * 86400;
        const segEnd = segStart + pos.duration * 86400;
        if (offsetSeconds >= segStart && offsetSeconds <= segEnd) {
          best = seg;
          break;
        }
        const delta = Math.abs(segStart - offsetSeconds);
        if (delta < bestDelta) {
          bestDelta = delta;
          best = seg;
        }
      }
      if (best) {
        card._activeSegment = best;
        return card._playSegment(best.media_content_id);
      }
      return Promise.resolve();
    });
    await Promise.all(promises);
  }
  _changeDay(delta) {
    const d = new Date(this._currentDate + "T00:00:00");
    d.setDate(d.getDate() + delta);
    this._currentDate = d.toISOString().slice(0, 10);
    this.shadowRoot.getElementById("date-lbl").textContent = this._currentDate;
    for (const card of this._rowCards) {
      card._segments = [];
      card._motionEvents = [];
      card._currentDate = this._currentDate;
      card._loadDay(this._currentDate);
      if (card._config && card._config.motion_entity) card._loadMotion(this._currentDate);
    }
  }
  _startDriftCorrection() {
    this._stopDriftCorrection();
    const TOLERANCE_S = .1;
    const tick = () => {
      if (!this._rowCards || this._rowCards.length < 2) {
        this._driftRaf = requestAnimationFrame(tick);
        return;
      }
      const masterShadow = this._rowCards[0].shadowRoot;
      const master = masterShadow && masterShadow.getElementById("player");
      if (!master || master.paused || isNaN(master.currentTime)) {
        this._driftRaf = requestAnimationFrame(tick);
        return;
      }
      for (let i = 1; i < this._rowCards.length; i++) {
        const followShadow = this._rowCards[i].shadowRoot;
        const follower = followShadow && followShadow.getElementById("player");
        if (!follower || isNaN(follower.currentTime)) continue;
        const drift = master.currentTime - follower.currentTime;
        if (Math.abs(drift) > TOLERANCE_S) {
          follower.currentTime = master.currentTime;
        }
        if (master.paused && !follower.paused) follower.pause();
        if (!master.paused && follower.paused && !follower.ended) {
          follower.play().catch(() => {});
        }
      }
      this._driftRaf = requestAnimationFrame(tick);
    };
    this._driftRaf = requestAnimationFrame(tick);
  }
  _stopDriftCorrection() {
    if (this._driftRaf) {
      cancelAnimationFrame(this._driftRaf);
      this._driftRaf = null;
    }
  }
  getCardSize() {
    return (this._config && this._config.cameras ? this._config.cameras.length : 1) * 4;
  }
}

customElements.define("bosch-nvr-multi-cam-card", BoschNvrMultiCamCard);

window.customCards.push({
  type: "bosch-nvr-multi-cam-card",
  name: "Bosch NVR Multi-Cam",
  description: "Stacked NVR timeline view for multiple cameras with shared seek and drift correction.",
  preview: false
});

class BoschNotificationsCard extends HTMLElement {
  setConfig(config) {
    this._config = {
      title: config.title ?? "Bosch Cloud",
      maintenance_entity: config.maintenance_entity ?? null,
      camera_status_entities: Array.isArray(config.camera_status_entities) ? config.camera_status_entities : null,
      show_camera_grid: config.show_camera_grid !== false,
      show_when_clear: config.show_when_clear !== false
    };
    if (this.isConnected) this._render();
  }
  set hass(hass) {
    this._hass = hass;
    this._render();
  }
  connectedCallback() {
    if (this._hass) this._render();
  }
  _maintenanceEntity() {
    if (this._config.maintenance_entity) {
      return this._hass.states[this._config.maintenance_entity] || null;
    }
    for (const eid in this._hass.states) {
      if (/^sensor\..*_bosch_cloud_wartung$/.test(eid)) {
        return this._hass.states[eid];
      }
    }
    return null;
  }
  _cameraStatusEntities() {
    if (this._config.camera_status_entities) {
      return this._config.camera_status_entities.map(eid => this._hass.states[eid]).filter(Boolean);
    }
    const out = [];
    for (const eid in this._hass.states) {
      if (!/^sensor\.bosch_[a-z0-9_]+_status$/.test(eid)) continue;
      if (eid.endsWith("_mini_nvr_status")) continue;
      if (eid.endsWith("_stream_status")) continue;
      if (eid.endsWith("_alarm_status")) continue;
      out.push(this._hass.states[eid]);
    }
    return out;
  }
  _maintenanceBanner(maint) {
    if (!maint) return "";
    const state = maint.state;
    const a = maint.attributes || {};
    const fmtTime = ts => ts ? new Date(ts).toLocaleString("de-DE", {
      weekday: "short",
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit"
    }) : "?";
    const safeLink = a.link && /^https:\/\//i.test(a.link) ? a.link : null;
    const link = safeLink ? `<a href="${this._esc(safeLink)}" target="_blank" rel="noopener">Details bei Bosch →</a>` : "";
    if (state === "active") {
      return `\n        <div class="banner banner-active">\n          <div class="banner-icon">⚠️</div>\n          <div class="banner-body">\n            <div class="banner-title">Cloud-Wartung läuft</div>\n            <div class="banner-sub">${this._esc(a.title || "Wartungsmeldung")}</div>\n            <div class="banner-window">${fmtTime(a.scheduled_start)} – ${fmtTime(a.scheduled_end).split(", ").slice(-1)[0]}</div>\n            <div class="banner-note">Live-Bild und Snapshots ggf. eingeschränkt.</div>\n            ${link ? `<div class="banner-link">${link}</div>` : ""}\n          </div>\n        </div>`;
    }
    if (state === "scheduled") {
      return `\n        <div class="banner banner-scheduled">\n          <div class="banner-icon">📅</div>\n          <div class="banner-body">\n            <div class="banner-title">Cloud-Wartung geplant</div>\n            <div class="banner-sub">${this._esc(a.title || "Wartungsmeldung")}</div>\n            <div class="banner-window">Beginn: ${fmtTime(a.scheduled_start)}<br>Ende: ${fmtTime(a.scheduled_end)}</div>\n            ${link ? `<div class="banner-link">${link}</div>` : ""}\n          </div>\n        </div>`;
    }
    if (state === "recent" || state === "past") {
      return `\n        <div class="banner banner-past">\n          <div class="banner-icon">✅</div>\n          <div class="banner-body">\n            <div class="banner-title">Cloud-Wartung beendet</div>\n            <div class="banner-sub">${this._esc(a.title || "Wartungsmeldung")}</div>\n            <div class="banner-window">Beendet ${fmtTime(a.scheduled_end)}</div>\n            <div class="banner-note">Cloud-Dienste sollten wieder normal funktionieren.</div>\n          </div>\n        </div>`;
    }
    return "";
  }
  _cameraGrid(cams) {
    if (!cams.length || !this._config.show_camera_grid) return "";
    const rows = cams.map(c => {
      const name = c.attributes && c.attributes.friendly_name || c.entity_id;
      const cleanName = name.replace(/^Bosch\s+/, "").replace(/\s+Status$/, "");
      const status = c.state || "unknown";
      const cls = status === "ONLINE" || status === "online" ? "ok" : status === "OFFLINE" || status === "offline" ? "warn" : "muted";
      return `\n        <div class="cam-row">\n          <span class="cam-dot ${cls}"></span>\n          <span class="cam-name">${this._esc(cleanName)}</span>\n          <span class="cam-state ${cls}">${this._esc(status)}</span>\n        </div>`;
    }).join("");
    return `<div class="cam-grid"><div class="cam-header">Kamera-Status</div>${rows}</div>`;
  }
  _clearMessage(maint, cams) {
    if (!this._config.show_when_clear) return "";
    const hasMaint = maint && [ "active", "scheduled", "recent" ].includes(maint.state);
    if (hasMaint) return "";
    const anyOffline = cams.some(c => /OFFLINE|offline/i.test(c.state));
    if (anyOffline) return "";
    return `<div class="clear">✓ Keine Bosch-Cloud-Wartung geplant. Alle Kameras erreichbar.</div>`;
  }
  _esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }[c]));
  }
  _render() {
    if (!this._hass || !this._config) return;
    if (!this.shadowRoot) this.attachShadow({
      mode: "open"
    });
    const maint = this._maintenanceEntity();
    const cams = this._cameraStatusEntities();
    this.shadowRoot.innerHTML = `\n      <style>\n        :host{display:block;background:var(--card-background-color,#1c1c1c);border-radius:12px;\n          padding:16px;color:var(--primary-text-color,#fff);font-family:var(--paper-font-body1_-_font-family,Roboto)}\n        h2{margin:0 0 12px 0;font-size:16px;font-weight:500;color:var(--primary-text-color,#fff)}\n        .banner{display:flex;gap:12px;padding:12px;border-radius:8px;margin-bottom:12px;align-items:flex-start}\n        .banner-active{background:rgba(255,152,0,0.12);border-left:4px solid #ff9800}\n        .banner-scheduled{background:rgba(33,150,243,0.12);border-left:4px solid #2196f3}\n        .banner-past{background:rgba(76,175,80,0.12);border-left:4px solid #4caf50}\n        .banner-icon{font-size:24px;line-height:1}\n        .banner-body{flex:1;font-size:13px}\n        .banner-title{font-weight:600;margin-bottom:4px}\n        .banner-sub{color:var(--secondary-text-color,#aaa);margin-bottom:4px}\n        .banner-window{font-family:var(--paper-font-code1_-_font-family,monospace);font-size:12px;margin-bottom:4px}\n        .banner-note{color:var(--secondary-text-color,#aaa);font-size:12px;margin-bottom:4px}\n        .banner-link a{color:var(--primary-color,#03a9f4);text-decoration:none}\n        .banner-link a:hover{text-decoration:underline}\n        .cam-grid{margin-top:8px}\n        .cam-header{font-size:12px;color:var(--secondary-text-color,#aaa);\n          text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;font-weight:500}\n        .cam-row{display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--divider-color,#333);font-size:13px}\n        .cam-row:last-child{border-bottom:none}\n        .cam-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}\n        .cam-dot.ok{background:#4caf50}\n        .cam-dot.warn{background:#ff9800}\n        .cam-dot.muted{background:#666}\n        .cam-name{flex:1}\n        .cam-state{font-size:11px;color:var(--secondary-text-color,#aaa);text-transform:uppercase;letter-spacing:0.5px}\n        .cam-state.ok{color:#4caf50}\n        .cam-state.warn{color:#ff9800}\n        .clear{padding:12px;text-align:center;color:var(--secondary-text-color,#aaa);font-size:13px}\n      </style>\n      <h2>${this._esc(this._config.title)}</h2>\n      ${this._maintenanceBanner(maint)}\n      ${this._cameraGrid(cams)}\n      ${this._clearMessage(maint, cams)}`;
  }
  getCardSize() {
    return 3;
  }
}

customElements.define("bosch-notifications-card", BoschNotificationsCard);

window.customCards.push({
  type: "bosch-notifications-card",
  name: "Bosch Notifications",
  description: "Bosch cloud maintenance + camera status banner. Aggregates active/scheduled/past maintenance windows from the RSS feed and shows online/offline state per camera.",
  preview: false
});