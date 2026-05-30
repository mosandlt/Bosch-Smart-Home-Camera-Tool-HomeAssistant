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
 * Version: 2.8.1
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

const CARD_VERSION = "13.4.1";

// Card auto-play modes. Primary source = integration option
// `auto_play_default` exposed on the camera entity attribute. Per-card
// YAML `auto_play` overrides. Garbage (including the dropped v2.15.x
// "confirm" value) collapses to "lan".
//   always — auto-reveal the live video in every session.
//   lan    — auto-reveal on LAN; on remote pre-init the stream in the
//            background and show a tap-to-reveal overlay (default).
//   never  — pre-init + overlay in every session; user always taps to reveal.
const AUTO_PLAY_MODES = ["lan", "always", "never"];

// Minimal card-side i18n. Picks DE for hass.language starting with "de",
// EN otherwise. Keep keys stable — referenced by _t() lookups.
const CARD_I18N = {
  en: {
    play_gate_label: "Start stream",
    play_gate_hint_remote: "You're on a remote connection — tap to start",
    play_gate_hint_default: "Tap to start the live stream",
  },
  de: {
    play_gate_label: "Stream starten",
    play_gate_hint_remote: "Du bist remote — antippen zum Starten",
    play_gate_hint_default: "Antippen, um den Live-Stream zu starten",
  },
};

// HLS player buffer profiles. Selected via the integration option
// "live_buffer_mode" and exposed on camera entity attributes. Mapped to
// hls.js parameters here. With LL-HLS (part_duration 0.75s in HA config),
// liveSyncDurationCount refers to parts, not segments, so lag in seconds =
// liveSyncDurationCount * 0.75. lowLatencyMode: true in all profiles so
// hls.js uses EXT-X-PART sub-segments (LL-HLS Blocking Playlist Reload).
// maxBufferLength MUST stay below HA's 30 s OUTPUT_IDLE_TIMEOUT.
const BOSCH_BUFFER_PROFILES = {
  latency:  { liveSyncDurationCount: 2, liveMaxLatencyDurationCount:  4, maxBufferLength:  8, maxMaxBufferLength: 14, lowLatencyMode: true },
  balanced: { liveSyncDurationCount: 4, liveMaxLatencyDurationCount:  8, maxBufferLength: 14, maxMaxBufferLength: 22, lowLatencyMode: true },
  stable:   { liveSyncDurationCount: 6, liveMaxLatencyDurationCount: 12, maxBufferLength: 22, maxMaxBufferLength: 28, lowLatencyMode: true },
};

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
    this._lastPrivacyMaskKey = null; // memoization key for privacy mask SVG
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
    // Skip WebRTC + show HLS banner when:
    //   (a) HA Companion App reaches us through an external endpoint
    //       (Cloudflare Tunnel / Nabu Casa) — UDP can't ride the tunnel,
    //       ICE always times out after ~5 s.
    //   (b) Mobile browser (iOS Safari, Android Chrome) over an external
    //       endpoint — cellular networks deploy carrier-grade NAT and often
    //       proxy/strip UDP, so STUN returns unusable candidates and ICE
    //       fails. Same 5 s wait + visible "stream failed" toast.
    // Desktop browsers external and any client on LAN/.local continue to
    // attempt WebRTC normally.
    this._remoteSkipWebRTC = (() => {
      const ua = navigator.userAgent || "";
      const isCompanion = /Home\s?Assistant/i.test(ua);
      // iPhone/iPod always identify; iPadOS 13+ Safari masquerades as Mac
      // but exposes touch via maxTouchPoints>1. Android phones/tablets
      // carry "Android" in the UA reliably.
      const isIOS = /iPhone|iPod/i.test(ua) ||
                    (/Macintosh/i.test(ua) && (navigator.maxTouchPoints || 0) > 1);
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
    // On Android WebView the autoplay policy (mediaPlaybackRequiresUserGesture)
    // can block even muted play. Start with audio muted regardless of the HA
    // audio entity state — user can enable via the Ton toggle after the stream
    // is active. Cleared on first explicit Ton toggle so the entity state takes
    // over normally from that point on.
    this._androidAudioMuted = /Android/i.test(navigator.userAgent || "");
    this._timerStreaming     = false; // whether refresh timer is running at streaming interval
    this._optimistic        = {};    // optimistic entity states { entityId: "on"/"off"/"pending" }
    this._optimisticTimers  = {};    // timers to auto-clear optimistic states
    this._errorFeedbackTimers = {};  // timers clearing transient error class on toggle buttons
    this._entityToBtnId     = {};    // map entityId → DOM id of its sw-row/btn (populated in _render)
    this._visibilityHandler = null;  // bound visibilitychange listener
    this._lastEventState    = null;  // last known last_event sensor value — for event detection
    this._lastFrameTime     = 0;    // monotonic ms of last fresh frame — for Δt debug display
    this._streamStartTime   = 0;    // ms when current stream session started — for uptime counter
    this._awaitingFresh     = false; // true while waiting for a fresh (non-cache) image
    this._showMotionZones   = false; // runtime toggle for motion zone overlay
    this._showPrivacyMasks  = false; // runtime toggle for privacy mask overlay
    this._lastRulesKey      = null;  // memoization key for rules list HTML
    // Bind the theme + mode broadcast handlers once so add/removeEventListener
    // use the same reference. Done in the constructor (not as class fields) so
    // older WebKit / iOS WKWebView builds without public-class-field support
    // don't ReferenceError during setConfig.
    this._onThemeBroadcast = this._onThemeBroadcast.bind(this);
    this._onModeBroadcast  = this._onModeBroadcast.bind(this);
    this._activeTheme       = "ios";
    this._activeMode        = "night";  // resolved by _applyMode after setConfig
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────────
  connectedCallback() {
    this._visibilityHandler = () => this._onVisibilityChange();
    document.addEventListener("visibilitychange", this._visibilityHandler);
    // Mobile reload fix: iOS Safari + HA Companion App (WKWebView) do NOT
    // reliably fire `disconnectedCallback` on tab reload / app close. Without
    // an explicit close, the WebRTC RTCPeerConnection lingers on go2rtc's
    // side as a stale consumer until its internal timeout (~10–15 s) frees
    // the slot. The next mount's `camera/webrtc/offer` then queues behind
    // the stale consumer, so the user sees "stream appears magically after
    // many seconds". `pagehide` fires reliably on iOS / WKWebView right
    // before unload — calling _stopLiveVideo() flushes pc.close() and the
    // WS-unsubscribe so go2rtc frees the consumer immediately.
    this._pagehideHandler = () => this._stopLiveVideo();
    window.addEventListener("pagehide", this._pagehideHandler);
    // Listen to theme + mode broadcast so all bosch-camera-cards on the page
    // sync when the user toggles either switcher on any one of them.
    window.addEventListener("bosch-card-theme-change", this._onThemeBroadcast);
    window.addEventListener("bosch-card-mode-change",  this._onModeBroadcast);
    // Native browser fullscreen state — fired on Esc-exit, F-key, browser-UI
    // exit etc. Bound here (not as a class field) for older WKWebView compat.
    this._onFullscreenChange = () => this._updateFullscreenButtonState();
    document.addEventListener("fullscreenchange",       this._onFullscreenChange);
    document.addEventListener("webkitfullscreenchange", this._onFullscreenChange);
    document.addEventListener("mozfullscreenchange",    this._onFullscreenChange);
    document.addEventListener("MSFullscreenChange",     this._onFullscreenChange);
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
      // During warming_up/connecting: show last snapshot as background under the
      // loading overlay instead of a black screen. Looks like a live preview but
      // the image is actually the last cached snapshot. Set to false to revert to
      // the classic dark overlay (better on low-end devices to avoid the extra
      // camera_proxy request while the stream is already starting).
      snapshot_during_warmup:    config.snapshot_during_warmup !== false,
      // Minimal layout: image + info-row + [Snapshot, Live Stream, ⋮, Vollbild] +
      // Privacy toggle. Everything else (audio/light/notifications, accordions,
      // automations, pan controls) is hidden by default and revealed when the
      // user clicks the ⋮ overflow button. Opt-in via YAML `minimal: true`.
      minimal:                    config.minimal === true,
      // Card auto-play override. When set, takes precedence over the
      // integration option `auto_play_default`. null = fall back to integration.
      auto_play:                  AUTO_PLAY_MODES.includes(config.auto_play) ? config.auto_play : null,
      // Apple-style redesign (v2.17.0): glass header pill + pill-bar overlay on
      // the video, larger radii, softer shadows. Defaults to true; set
      // `apple_style: false` to keep the legacy chrome (header bar + button
      // row below video) untouched.
      apple_style:                config.apple_style !== false,
      // Theme inside the Apple-style redesign (v2.18.0): "ios" (default,
      // glass blur + iOS systemColors), "android" (Material You: M3 surface
      // tones, solid backgrounds, tonal buttons), or "auto" (resolve from
      // user-agent — Android UA → android, anything else → ios). Config-only
      // since 2026-05-30 (the in-card theme switcher was removed, issue #15).
      theme:                      ["ios", "android", "auto"].includes(config.theme) ? config.theme : "ios",
      // Day/Night card-chrome mode (v13.0.1): "auto" follows the system
      // prefers-color-scheme, "day" forces a light card (white ha-card,
      // dark text, light M3 surface on Android), "night" forces dark. The
      // glass overlays on the video stay constant — they need to be
      // legible regardless of background. Config-only since 2026-05-30 (the
      // in-card day/night switcher was removed, issue #15).
      mode:                       ["auto", "day", "night"].includes(config.mode) ? config.mode : "auto",
      // Per-card geometry overrides (issue #21): optional CSS strings applied
      // via the card-specific --bosch-card-radius / --bosch-card-shadow vars
      // (see the setConfig tail). null = apple/legacy default. Deliberately
      // NOT the global --ha-card-* theme tokens, so a dashboard theme that
      // zeroes those never strips the card's own rounding/shadow.
      border_radius:              typeof config.border_radius === "string" ? config.border_radius : null,
      box_shadow:                 typeof config.box_shadow === "string" ? config.box_shadow : null,
      // Compact tile mode (v13 follow-up): hide the pill-bar + status badge
      // so the card is just video + title-pill. Used by the overview card's
      // `compact: true` for Apple-Home-style grid tiles. Click the video to
      // expand (existing fullscreen handler).
      compact:                    config.compact === true,
      // Element-hiding toggles (issue #15): let users strip the title-pill and
      // the last-event badge for a clean video-only tile. Both default to true
      // so existing cards are unchanged. Independent of `compact` (which only
      // drops the pill-bar + status badge).
      show_title:                 config.show_title !== false,
      show_last_event:            config.show_last_event !== false,
      // idle refresh is handled by Page Visibility API: 60 s visible, 1800 s background
    };

    this._storageKey = `bosch_cam_${config.camera_entity}`;

    const base = config.camera_entity.replace(/^camera\./, "");
    this._base = base;
    this._entities = {
      camera:       config.camera_entity,
      switch:       config.switch_entity        || `switch.${base}_live_stream`,
      audio:        config.audio_entity         || `switch.${base}_audio`,
      light:        config.light_entity         || `switch.${base}_camera_light`,
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
      privacyMasks:  config.privacy_masks_entity  || `sensor.${base}_privacy_masks`,
      streamStatus:  config.stream_status_entity  || `sensor.${base}_stream_status`,
      ambientSchedule: config.ambient_schedule_entity || `sensor.${base}_dauerlicht_zeitplan`,
      scheduleRules: config.rules_entity          || `sensor.${base}_schedule_rules`,
      frontLight:    config.front_light_entity   || `switch.${base}_front_light`,
      wallwasher:    config.wallwasher_entity    || `switch.${base}_wallwasher`,
      frontLightIntensity: config.front_light_intensity_entity || `number.${base}_front_light_intensity`,
      siren:         config.siren_entity         || `button.${base}_siren`,
      // Gen2-only entities
      statusLed:     config.status_led_entity    || `switch.${base}_status_led`,
      lensElevation: config.lens_elevation_entity || `number.${base}_lens_elevation`,
      micLevel:      config.mic_level_entity     || `number.${base}_microphone_level`,
      colorTemp:     config.color_temp_entity    || `number.${base}_color_temperature`,
      motionLight:   config.motion_light_entity  || `switch.${base}_licht_bei_bewegung`,
      ambientLight:  config.ambient_light_entity || `switch.${base}_dauerlicht`,
      intrusionDetection: config.intrusion_entity || `switch.${base}_einbrucherkennung`,
      motionSensitivity: config.motion_sensitivity_entity || `number.${base}_bewegungslicht_empfindlichkeit`,
      // Automations — manual array or auto-discover from device
      automations: config.automations || [],
      _autoDiscoverAutomations: !config.automations || config.automations.length === 0,
      topLedLight:   config.top_led_light_entity || `light.${base}_oberes_licht`,
      bottomLedLight: config.bottom_led_light_entity || `light.${base}_unteres_licht`,
      frontLightEntity: config.front_light_color_entity || `light.${base}_frontlicht`,
      topBrightness: config.top_brightness_entity || `number.${base}_helligkeit_oberes_licht`,
      bottomBrightness: config.bottom_brightness_entity || `number.${base}_helligkeit_unteres_licht`,
      // Gen2 Indoor II — alarm system (75 dB siren) + Audio+ + Power LED
      alarmSystemArm:  config.alarm_system_arm_entity  || `switch.${base}_alarmanlage`,
      alarmMode:       config.alarm_mode_entity        || `switch.${base}_sirene`,
      preAlarm:        config.pre_alarm_entity         || `switch.${base}_pre_alarm`,
      alarmState:      config.alarm_state_entity       || `sensor.${base}_alarm_status`,
      sirenDuration:   config.siren_duration_entity    || `number.${base}_sirenen_dauer`,
      alarmActivationDelay: config.alarm_activation_delay_entity || `number.${base}_alarm_verzogerung`,
      preAlarmDelay:   config.prealarm_delay_entity    || `number.${base}_pre_alarm_dauer`,
      powerLedBrightness: config.power_led_entity      || `number.${base}_power_led`,
      // Image rotation 180° (indoor cameras only — Gen1 360 + Gen2 Indoor II).
      // HA slugifies "180°" to "180deg" — this default matches the actual entity slug.
      imageRotation180: config.image_rotation_180_entity || `switch.${base}_bild_180deg_drehen`,
    };

    this._showMotionZones = this._config.show_motion_zones;
    // Apply layout flag on the custom-element host so CSS `:host(.minimal)`
    // selectors can hide/show the advanced control rows.
    this.classList.toggle("minimal", this._config.minimal);
    // Apple-style class on host gates the new glass overlay CSS.
    this.classList.toggle("apple-style", this._config.apple_style);
    this.classList.toggle("compact", this._config.compact);
    // Element-hiding flags (issue #15): :host(.no-title)/:host(.no-last-event).
    this.classList.toggle("no-title", !this._config.show_title);
    this.classList.toggle("no-last-event", !this._config.show_last_event);
    // OS host class (:host(.os-windows) etc.) for OS-targeted CSS. Development
    // happens on macOS only, so this gives a hook to correct platform-specific
    // rendering (Segoe-UI metrics, ClearType weight, scrollbar width) reported
    // from Windows/Linux without guessing. 2026-05-30 (issue #15, Edge/Win11).
    this._applyOsClass();
    // Resolve effective theme: localStorage override > config > UA auto-detect.
    this._applyTheme(this._resolveTheme());
    // Resolve effective day/night mode the same way.
    this._applyMode(this._resolveMode());
    // "minimal meaningful" (2026-05-29): a non-minimal apple-style card shows
    // the full control stack (switches + accordions) expanded by default — the
    // ⋮ menu starts open. minimal:true keeps it collapsed behind ⋮; compact
    // tiles stay clean (no pill-bar, always collapsed). Legacy mode is
    // unaffected (its non-minimal layout shows everything regardless).
    this.classList.toggle(
      "overflow-open",
      this._config.apple_style && !this._config.minimal && !this._config.compact,
    );
    // Optional per-card geometry overrides (issue #21) via card-specific CSS
    // vars — applied here so they take effect WITHOUT the card inheriting the
    // global --ha-card-* theme tokens (a theme that zeroes those must not strip
    // the apple-style rounding; opt-in only).
    if (this._config.border_radius) this.style.setProperty("--bosch-card-radius", this._config.border_radius);
    else this.style.removeProperty("--bosch-card-radius");
    if (this._config.box_shadow) this.style.setProperty("--bosch-card-shadow", this._config.box_shadow);
    else this.style.removeProperty("--bosch-card-shadow");
    this._render();
    this._restoreCachedImage();
    this._startRefreshTimer();
    // Pre-load hls.js in the background so it's cached when the user starts the stream
    this._loadHlsJs().catch(() => {});
  }

  // ── Theme (iOS / Android) ─────────────────────────────────────────────────
  // The Apple-style redesign supports two looks: iOS (glass blur, SF-Pro,
  // system colors) and Android (Material You / M3: solid surface tones,
  // tonal buttons, larger container radius). Resolution order:
  //   1. window-level localStorage override (set by the in-card switcher)
  //   2. YAML `theme: ios | android | auto`
  //   3. user-agent auto-detect when theme === "auto"
  //   4. fallback "ios"
  // The localStorage key is bosch-card-theme; once set, it sticks across
  // reloads for all Bosch cards on the page until the user picks Auto.

  _resolveTheme() {
    // Config-only since 2026-05-30 (in-card switcher removed, issue #15): YAML
    // `theme:` → UA auto-detect when "auto" → "ios" default. No localStorage
    // override (a stale value would otherwise outlive the now-removed button).
    const cfg = this._config?.theme || "ios";
    if (cfg === "ios" || cfg === "android") return cfg;
    return this._detectTheme();
  }

  _detectTheme() {
    const ua = (navigator.userAgent || "").toLowerCase();
    // Android phones/tablets always carry "android" in the UA. iPad masquerades
    // as Mac since iPadOS 13 — detect via touch capability too. Everything else
    // (desktop, Windows, Linux, macOS, Chrome OS) falls back to iOS look.
    if (/android/.test(ua)) return "android";
    return "ios";
  }

  _applyTheme(theme) {
    this.classList.toggle("theme-ios", theme === "ios");
    this.classList.toggle("theme-android", theme === "android");
    this._activeTheme = theme;
    // Refresh the switcher chips inside the Mehr menu if the card has
    // already rendered. Safe to call repeatedly — it just toggles `.on`.
    this._refreshThemeSwitcher();
  }

  _setUserTheme(theme) {
    // Persist + broadcast so all bosch-camera-cards on the page (overview
    // grid) update in lockstep. "auto" means "remove the user override and
    // fall back to config + UA".
    try {
      if (theme === "auto") window.localStorage?.removeItem("bosch-card-theme");
      else if (theme === "ios" || theme === "android") window.localStorage?.setItem("bosch-card-theme", theme);
    } catch { /* private mode / quota — apply non-persistently */ }
    window.dispatchEvent(new CustomEvent("bosch-card-theme-change", { detail: { theme } }));
  }

  // Detect the OS and stamp a `os-<name>` class on the host so CSS can target
  // platform-specific rendering quirks. UA-string based (works in every browser
  // incl. the HA Companion WKWebView/Android-WebView, unlike navigator.userAgentData
  // which is Chromium-only and async). navigator.platform is deprecated. iOS is
  // detected incl. iPadOS (reports as Macintosh + touch). 2026-05-30.
  _applyOsClass() {
    const ua = navigator.userAgent || "";
    const touch = (navigator.maxTouchPoints || 0) > 1;
    let os = "other";
    if (/Windows|Win32|Win64/i.test(ua)) os = "windows";
    else if (/iPhone|iPad|iPod/i.test(ua) || (/Macintosh/i.test(ua) && touch)) os = "ios";
    else if (/Macintosh|Mac OS X/i.test(ua)) os = "macos";
    else if (/Android/i.test(ua)) os = "android";
    else if (/Linux|X11|CrOS/i.test(ua)) os = "linux";
    for (const c of ["windows", "macos", "ios", "android", "linux", "other"]) {
      this.classList.toggle("os-" + c, c === os);
    }
  }

  _onThemeBroadcast(_ev) {
    // Bound in constructor (see this._onThemeBroadcast = ...) so the same
    // reference is used for addEventListener + removeEventListener.
    this._applyTheme(this._resolveTheme());
  }

  _refreshThemeSwitcher() {
    const sw = this.shadowRoot?.getElementById("ap-theme-switcher");
    if (!sw) return;
    let stored = null;
    try { stored = window.localStorage?.getItem("bosch-card-theme"); } catch { /* ignore */ }
    // Highlight the chip that matches the EFFECTIVE choice, mirroring
    // _resolveTheme's precedence: localStorage override → YAML config → "auto".
    // Previously this only looked at localStorage, so a card configured with
    // `theme: ios` (and no in-card override) wrongly showed "Auto" selected
    // even though it rendered iOS. (issue #15, 2026-05-30)
    const cfgTheme = this._config?.theme;
    const selected = (stored === "ios" || stored === "android") ? stored
                   : (cfgTheme === "ios" || cfgTheme === "android") ? cfgTheme
                   : "auto";
    sw.querySelectorAll("[data-theme]").forEach((b) => {
      b.classList.toggle("on", b.getAttribute("data-theme") === selected);
      b.setAttribute("aria-pressed", b.getAttribute("data-theme") === selected ? "true" : "false");
    });
  }

  // ── Day/Night mode (Tag / Nacht) ──────────────────────────────────────────
  // Parallel architecture to theme. Resolution:
  //   1. localStorage `bosch-card-mode` (set by the in-card switcher)
  //   2. YAML `mode: auto | day | night`
  //   3. `(prefers-color-scheme: dark)` when mode === "auto"
  //   4. fallback "night"
  // Only the card-chrome (ha-card background, text colors, dividers, switch
  // rows) responds. Glass overlays on the video stay dark for legibility.

  _resolveMode() {
    // Config-only since 2026-05-30 (in-card switcher removed, issue #15): YAML
    // `mode:` → prefers-color-scheme when "auto" (default). No localStorage
    // override (a stale value would otherwise outlive the now-removed button).
    const cfg = this._config?.mode || "auto";
    if (cfg === "day" || cfg === "night") return cfg;
    return this._detectMode();
  }

  _detectMode() {
    try {
      if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) return "night";
    } catch { /* ignore */ }
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
      if (mode === "auto") window.localStorage?.removeItem("bosch-card-mode");
      else if (mode === "day" || mode === "night") window.localStorage?.setItem("bosch-card-mode", mode);
    } catch { /* private mode / quota */ }
    window.dispatchEvent(new CustomEvent("bosch-card-mode-change", { detail: { mode } }));
  }

  _onModeBroadcast(_ev) {
    this._applyMode(this._resolveMode());
  }

  // Force the stream badge to "Live" the instant the <video> actually plays,
  // without waiting for the next hass push. The shared stream_status sensor can
  // trail the first frame by 10s+, which left the top-right badge stuck on
  // "Verbinde" while the stream already ran (Thomas, Innenbereich, 2026-05-30).
  // The hass-driven badge update keeps it Live afterwards (_liveVideoActive wins).
  _markLiveBadge() {
    const badge = this.shadowRoot?.getElementById("stream-badge");
    if (badge) badge.className = "stream-badge streaming";
    const apBadge = this.shadowRoot?.getElementById("ap-badge");
    if (apBadge) { apBadge.className = "ap-badge live"; apBadge.textContent = "Live"; }
    const apBtnStream = this.shadowRoot?.getElementById("ap-btn-stream");
    if (apBtnStream) apBtnStream.classList.remove("connecting");
  }

  _refreshModeSwitcher() {
    const sw = this.shadowRoot?.getElementById("ap-mode-switcher");
    if (!sw) return;
    let stored = null;
    try { stored = window.localStorage?.getItem("bosch-card-mode"); } catch { /* ignore */ }
    // Mirror _resolveMode precedence: localStorage override → YAML config →
    // "auto", so a card configured with `mode: day|night` shows that chip
    // selected instead of always "Auto". (issue #15, 2026-05-30)
    const cfgMode = this._config?.mode;
    const selected = (stored === "day" || stored === "night") ? stored
                   : (cfgMode === "day" || cfgMode === "night") ? cfgMode
                   : "auto";
    sw.querySelectorAll("[data-mode]").forEach((b) => {
      b.classList.toggle("on", b.getAttribute("data-mode") === selected);
      b.setAttribute("aria-pressed", b.getAttribute("data-mode") === selected ? "true" : "false");
    });
  }

  // ── HA state updates ──────────────────────────────────────────────────────
  // Fingerprint of every entity this card tracks (all id strings in
  // this._entities + the maintenance entity). `state + last_updated` captures
  // both state and attribute changes (last_updated bumps on any state-object
  // rewrite). Used by the set-hass diff-guard to skip redundant _update()s.
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
    if (me) { const s = hass.states[me]; fp += me + "=" + (s ? s.state + "@" + s.last_updated : "_") + ";"; }
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
    // Diff-guard: HA pushes the whole `hass` object to EVERY card on ANY entity
    // change anywhere in the system. With several cameras, an unrelated event
    // (a motion tick, a light elsewhere) would otherwise run a full _update()
    // pass on every card. Skip _update() when none of THIS card's tracked
    // entities (everything in this._entities + the maintenance entity) changed
    // since the last push. Always run on the first push. Correctness is
    // unaffected — the card still updates whenever one of its own entities
    // changes; this only drops redundant work. CSS theme vars + first-push
    // bootstrap (below) are unaffected.
    if (firstHass) {
      this._lastHassFp = this._hassFingerprint(hass);
    } else {
      const fp = this._hassFingerprint(hass);
      if (fp === this._lastHassFp) return;
      this._lastHassFp = fp;
    }
    this._applyImageRotation180();
    // Auto-play gate decision MUST run synchronously BEFORE _update() —
    // otherwise _update() would see shouldVideo=true (if the backend
    // stream is already on) and kick off the HLS connect before
    // _playGateActive is set, leaking bandwidth behind the gate.
    // hass.config + states are all immediately available on first push;
    // no defer needed for the decision itself.
    if (firstHass) this._maybeAutoPlay();
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
      // CARD_STALE_APP fix: pull authoritative entity states on mount so the
      // stream badge / switches reflect the backend immediately instead of
      // waiting for the WS push (which can lag a few seconds when HA's
      // Companion App resumes from background).
      this._pullFreshSwitchStates();
      // 800 ms re-check: covers the edge case where switch.state was
      // 'unavailable' on the very first push (camera in privacy/offline
      // at mount time) but flips to a real state right after
      // _pullFreshSwitchStates. _maybeAutoPlay is a no-op if already
      // decided, so this is safe to call unconditionally.
      setTimeout(() => this._maybeAutoPlay(), 800);
    }
  }

  // ── Auto-play gate ────────────────────────────────────────────────────────
  // Thomas, 2026-05-21 (final spec): the overlay ONLY appears when the
  // backend stream is actually running. Cold-open with stream=off shows
  // the regular snapshot card (no gate, no auto-start). When the stream
  // transitions OFF→ON (turned on here, on another device, or via an
  // automation), the gate appears in overlay-required modes so the user
  // can decide whether to display the live video.
  //
  // Modes:
  //   always — never gate; auto-display when stream goes on
  //   lan    — gate when remote; auto-display when on LAN
  //   never  — always gate when stream goes on
  //
  // Inputs: integration option `auto_play_default` on the camera entity
  // attribute, per-card YAML override `auto_play`, browser-side LAN
  // detection (hass.config.internal_url comparison + RFC-1918 fallback).
  _maybeAutoPlay() {
    // Kept as a no-op alias so older call sites (set hass firstHass + the
    // 800 ms re-check) still work. The real work is done by
    // _evaluateGateForStreamTransition() called from _update() on every
    // switch-state change.
    this._evaluateGateForStreamTransition();
  }

  // Called on every _update() pass after switch state is read. Tracks
  // OFF→ON transitions to show the gate, and ON→OFF transitions to hide
  // it. No-op while stream state is unchanged.
  _evaluateGateForStreamTransition() {
    if (!this._hass || !this._entities.switch) return;
    const switchEnt = this._hass.states[this._entities.switch];
    if (!switchEnt) return;
    const curr = switchEnt.state;  // "on" | "off" | "unavailable"
    const prev = this._lastEvaluatedSwitchState;
    this._lastEvaluatedSwitchState = curr;

    if (curr !== "on") {
      // Stream off or unavailable → no reason to gate; hide if showing.
      if (this._playGateActive) this._hidePlayGate();
      return;
    }
    // Stream is ON. Re-decide gate ONLY on transition into "on" — if we
    // already decided in a previous _update() pass (prev was "on"), don't
    // reshow the gate; the user's earlier tap stands until stream cycles.
    if (prev === "on") return;
    const camEnt = this._hass.states[this._entities.camera];
    if (camEnt && camEnt.state === "unavailable") return;
    const mode = this._getAutoPlayMode();
    if (mode === "always") return;                              // auto-display
    if (mode === "lan" && this._isLanSession()) return;         // auto-display
    // mode === "never", or mode === "lan" + remote → gate
    this._showPlayGate();
  }

  // Show the tap-to-reveal gate. Keeps the snapshot visible underneath so
  // the user still sees which camera they're about to start. Also hides
  // the loading overlay so the spinner doesn't bleed through the gate.
  _showPlayGate() {
    this._playGateActive = true;
    this._setLoadingOverlay(false);
    const el = this.shadowRoot?.getElementById("auto-play-gate");
    if (!el) return;
    const isLan = this._isLanSession();
    const hint = el.querySelector(".apg-hint");
    if (hint) {
      hint.textContent = this._t(
        isLan ? "play_gate_hint_default" : "play_gate_hint_remote",
      );
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

  // User said "go". Gate hidden, _update() then connects HLS to the
  // already-running stream. (New spec: the gate is only shown WHEN the
  // stream is running, so we never need to turn the switch on here —
  // it's already on by definition.)
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
    // Primary: exact origin match against HA-configured internal_url.
    // Covers the common case (HA Companion App + browsers using internal URL)
    // and is exact (port-aware). See knowledge-base/auto-play-lan-detect-mobile-compat.md.
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
    // Fallback: RFC-1918 / link-local hostname check for setups without
    // internal_url configured, or where the user navigated via an alias.
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

  // Apply 180° CSS rotation to the image+video wrapper based on the
  // switch.<base>_bild_180_drehen entity state (indoor cameras only).
  // Cheap — runs on every hass set, just toggles a class. Browser ignores
  // no-op class toggles so there's no re-layout cost when state didn't change.
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
    // Defensive cleanup: if card gets removed while CSS-fullscreen is active,
    // these document-level listeners would leak via `this`-closure.
    if (this._fsClickOut) { document.removeEventListener("pointerup", this._fsClickOut); this._fsClickOut = null; }
    if (this._fsKeyDown)  { document.removeEventListener("keydown", this._fsKeyDown);  this._fsKeyDown  = null; }
    if (this._loadingTimeout)    clearTimeout(this._loadingTimeout);
    if (this._snapshotPollTimer) clearTimeout(this._snapshotPollTimer);
    Object.values(this._optimisticTimers).forEach(t => clearTimeout(t));
    // Clear any lingering error-feedback timers set by _callServiceWithRollback
    if (this._errorFeedbackTimers) {
      Object.values(this._errorFeedbackTimers).forEach(t => clearTimeout(t));
      this._errorFeedbackTimers = {};
    }
    this._stopLiveVideo();
    window.removeEventListener("bosch-card-theme-change", this._onThemeBroadcast);
    window.removeEventListener("bosch-card-mode-change",  this._onModeBroadcast);
    if (this._onFullscreenChange) {
      document.removeEventListener("fullscreenchange",       this._onFullscreenChange);
      document.removeEventListener("webkitfullscreenchange", this._onFullscreenChange);
      document.removeEventListener("mozfullscreenchange",    this._onFullscreenChange);
      document.removeEventListener("MSFullscreenChange",     this._onFullscreenChange);
      this._onFullscreenChange = null;
    }
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
      // Page just came to foreground — trigger fresh snapshot like on page load,
      // but defer ~500ms so the Companion App's WebSocket has a chance to finish
      // reconnecting. Calling trigger_snapshot before WS is ready makes Android
      // show a native error popup (the `hass.connected` flag can lag the actual
      // disconnect by a few hundred ms after resume from background).
      setTimeout(() => {
        if (document.visibilityState === "visible" && !this._liveVideoActive) {
          this._triggerFreshSnapshot();
        }
      }, 500);
      // Also pull authoritative state for the toggleable switches via REST.
      // The HA-Companion-App suspends its WebSocket on backgrounding, and
      // when it resumes the local `hass.states` cache may briefly disagree
      // with the server until the next WS push arrives. A user tap during
      // that window can fire a wrong-direction toggle (observed 2026-04-28:
      // stream silently turned off because the card was seeing a stale state).
      // Best-effort, fire-and-forget; the next WS push would correct it
      // anyway, this just makes it instant.
      this._pullFreshSwitchStates();
    }
    // Restart timer with the correct interval (60 s or 1800 s)
    this._startRefreshTimer();
  }

  async _pullFreshSwitchStates() {
    if (!this._hass) return;
    // Include the camera entity so the stream badge reflects the
    // authoritative backend state. CARD_STALE_APP (2026-04-27): when the
    // card mounts after the HA-Companion-App resumes from background,
    // hass.states[camera.<base>] can lag the WS push by a few seconds.
    // The badge then shows yellow ("connecting") despite the backend
    // already streaming. Pulling the camera entity via REST closes the
    // gap inside ~200 ms instead of waiting on the next WS frame.
    const ids = [
      this._entities.camera,
      this._entities.switch,
      this._entities.privacy,
      this._entities.audio,
      this._entities.light,
    ].filter((id) => id && this._hass.states[id]);
    // .filter on hass.states[id]: only pull entities HA actually has. The
    // light entity id is derived for every camera, but cameras without a light
    // (e.g. Indoor II, the 360 indoor) have no switch.<cam>_camera_light — a
    // REST GET on it 404s and logs a console/network error on every mount.
    // A laggy-but-present entity (the CARD_STALE_APP case this method targets)
    // is still in hass.states, so it is NOT filtered out. 2026-05-29.
    let changed = false;
    for (const id of ids) {
      try {
        const fresh = await this._hass.callApi("GET", `states/${id}`);
        if (fresh && fresh.state && this._hass.states[id]?.state !== fresh.state) {
          // Don't mutate the shared hass.states cache (HA-core owns it) —
          // just clear any optimistic override we held so the next render
          // picks up the WS-pushed state. WS push for this delta is
          // typically <500 ms behind the REST result.
          delete this._optimistic[id];
          changed = true;
        }
      } catch (e) {
        // REST call failed (offline, auth issue) — silently skip; the
        // cached state is still the best we have.
      }
    }
    if (changed) this._update();
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
    // Guard 1: skip if service not yet registered (Android companion app startup race).
    if (!this._hass?.services?.bosch_shc_camera?.trigger_snapshot) return;
    // Guard 2: skip if WS is mid-reconnect. Companion App resume → cached services
    // map still shows the service, but the WS call fails before reconnect completes
    // → Android shows a native error popup even when JS catches the rejection.
    if (this._hass.connected === false) return;
    if (this._hass.connection && this._hass.connection.connected === false) return;
    this._callService("bosch_shc_camera", "trigger_snapshot", {});
    this._scheduleImageLoad(1500);
    this._scheduleImageLoad(4000);
  }

  // ── Full DOM render (once on setConfig) ───────────────────────────────────
  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; font-family: var(--primary-font-family, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif); }
        ha-card {
          overflow: hidden;
          /* Own --bosch-card-* vars (issue #21), not the global --ha-card-*
             radius/shadow tokens — a dashboard theme that zeroes those must not
             strip our card geometry. Background DOES follow the theme (intended). */
          border-radius: var(--bosch-card-radius, 12px);
          background: var(--ha-card-background, var(--card-background-color, #1c1c1e));
          box-shadow: var(--bosch-card-shadow, 0 2px 8px rgba(0,0,0,.3));
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
        .stream-badge.offline    { background: rgba(255,69,58,.15); color: #ff453a; }
        .stream-badge.offline .dot { background: #ff453a; }
        .stream-badge .dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
        .stream-badge.idle .dot       { background: #636366; }
        .stream-badge.streaming .dot  { background: #0a84ff; animation: pulse 1.5s infinite; }
        .stream-badge.connecting .dot { background: #ff9f0a; animation: pulse 0.8s infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

        /* iOS HLS info banner — sits absolutely at the top of the camera
           area so it stays readable over the video frame, never on the
           letterbox bars (which are pure black and gave 0 contrast). */
        .ios-hls-banner {
          display: none;
          position: absolute; top: 8px; left: 8px; right: 8px;
          z-index: 5;
          align-items: center; justify-content: center;
          gap: 6px; padding: 5px 10px;
          background: rgba(0,0,0,.6); backdrop-filter: blur(6px);
          -webkit-backdrop-filter: blur(6px);
          border: 1px solid rgba(255,255,255,.15);
          border-radius: 8px;
          font-size: 12px; font-weight: 500; color: #fff;
          pointer-events: none;
          text-shadow: 0 1px 2px rgba(0,0,0,.5);
        }
        .ios-hls-banner.visible { display: flex; }
        .ios-hls-banner span { white-space: nowrap; }

        /* Tap-to-play overlay — shown when Android WebView blocks autoplay
           (HA app "Autoplay videos" setting is off). z-index 9 = above video,
           below loading-overlay (10). */
        .tap-to-play-overlay {
          display: none;
          position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 9;
          flex-direction: column; align-items: center; justify-content: center;
          gap: 10px;
          background: rgba(0,0,0,.55);
          backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);
          cursor: pointer;
        }
        .tap-to-play-overlay.visible { display: flex; }
        .tap-to-play-overlay svg {
          width: 56px; height: 56px; fill: rgba(255,255,255,.9);
          filter: drop-shadow(0 2px 8px rgba(0,0,0,.5));
        }
        .tap-to-play-overlay .ttp-label {
          font-size: 13px; font-weight: 500; color: rgba(255,255,255,.85);
          text-shadow: 0 1px 3px rgba(0,0,0,.6);
        }
        .tap-to-play-overlay .ttp-hint {
          font-size: 11px; color: rgba(255,255,255,.5);
          text-align: center; max-width: 200px; line-height: 1.4;
        }

        /* Auto-play gate — shown when auto_play_default decides the user
           should explicitly tap to reveal the live video. z-index 11 sits
           above the video (1) and the tap-to-play overlay (9) but BELOW
           loading-overlay (10) — except loading is hidden while the gate
           is active so this is a non-issue. The snapshot remains visible
           through a translucent backdrop so the user sees which camera. */
        .auto-play-gate {
          display: none;
          position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 11;
          flex-direction: column; align-items: center; justify-content: center;
          gap: 8px;
          /* No backdrop-filter — Thomas wants to see the sharp snapshot
             behind the play button so he can decide based on the current
             camera image. Dimming only via low-opacity black overlay. */
          background: rgba(0,0,0,.25);
          cursor: pointer;
          transition: background 0.15s;
        }
        .auto-play-gate.visible { display: flex; }
        .auto-play-gate:hover { background: rgba(0,0,0,.4); }
        /* Hide the HLS-fallback banner while the play gate is up — the
           transport hint is irrelevant until the user actually starts
           the stream, just clutters the view. */
        .img-wrapper:has(.auto-play-gate.visible) .ios-hls-banner {
          display: none !important;
        }
        .auto-play-gate svg {
          width: 64px; height: 64px; fill: rgba(255,255,255,.95);
          filter: drop-shadow(0 2px 12px rgba(0,0,0,.6));
          transition: transform 0.12s;
        }
        .auto-play-gate:active svg { transform: scale(0.92); }
        .auto-play-gate .apg-label {
          font-size: 15px; font-weight: 600; color: rgba(255,255,255,.95);
          text-shadow: 0 1px 4px rgba(0,0,0,.7);
        }
        .auto-play-gate .apg-hint {
          font-size: 11px; color: rgba(255,255,255,.7);
          text-align: center; max-width: 240px; line-height: 1.4;
          text-shadow: 0 1px 3px rgba(0,0,0,.6);
        }

        /* Push status badge */
        .push-badge {
          display: inline-flex; align-items: center; gap: 4px;
          font-size: 10px; font-weight: 600; letter-spacing: .3px;
          text-transform: uppercase; padding: 2px 6px; border-radius: 12px;
          white-space: nowrap;
        }
        .push-badge.push  { background: rgba(48,209,88,.15); color: #30d158; }
        .push-badge.poll { background: rgba(99,99,102,.2); color: #8e8e93; }
        .push-badge .pdot { width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0; }
        .push-badge.push .pdot  { background: #30d158; }
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

        /* Live video element — absolute so it overlays the snapshot image
           without layout shift. Image stays visible underneath until video
           fires "playing" event, avoiding the black gap. */
        .cam-video {
          position: absolute; top: 0; right: 0; bottom: 0; left: 0;
          width: 100%; height: 100%; display: block; object-fit: cover;
          min-height: 160px; background: transparent;
        }

        /* Image rotation 180° (ceiling-mounted indoor cameras).
           Pure CSS transform — zero CPU, zero latency, GPU-composited.
           Toggled by the integration's switch.<base>_bild_180_drehen entity.
           Only the <video> is rotated here: the <img> is loaded from
           /api/camera_proxy/, which is already rotated server-side by
           camera.async_camera_image() (PIL) — rotating it again would
           cancel out and the dashboard snapshot would look upright. */
        .img-wrapper.rotated-180 .cam-video {
          transform: rotate(180deg);
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
          position: fixed !important; top: 0 !important; right: 0 !important; bottom: 0 !important; left: 0 !important;
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
        /* Keep Apple-style overlays on top of everything in fullscreen so
           they remain reachable for tap-to-exit and toggle clicks. Browser
           chromes (especially iOS) push the video layer aggressively to the
           foreground; the explicit high z-index ensures the glass pill +
           pill-bar stay above without changing layout. */
        :host(.fs-active) .ap-top,
        :host(.fs-active) .ap-pill-bar,
        .img-wrapper:fullscreen .ap-top,
        .img-wrapper:fullscreen .ap-pill-bar,
        .img-wrapper:-webkit-full-screen .ap-top,
        .img-wrapper:-webkit-full-screen .ap-pill-bar { z-index: 10000; }

        /* Motion zones SVG overlay */
        .motion-zones-overlay {
          position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 5;
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
        /* Gen2 polygon zones use per-zone colors from API */
        .motion-zones-overlay polygon { fill-opacity: 0.15; stroke-width: 2; stroke-opacity: 0.6; }
        /* Privacy mask SVG overlay */
        .privacy-mask-overlay {
          position: absolute; top: 0; left: 0; width: 100%; height: 100%;
          pointer-events: none; z-index: 5;
          opacity: 0; transition: opacity 0.3s;
        }
        .privacy-mask-overlay.visible { opacity: 1; }
        .privacy-mask-overlay rect, .privacy-mask-overlay polygon {
          fill: rgba(0, 0, 0, 0.5); stroke: rgba(0, 0, 0, 0.8); stroke-width: 1.5;
        }

        /* Loading overlay — must be above both cam-img and cam-video */
        .loading-overlay {
          position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 10;
          display: flex; flex-direction: column; align-items: center; justify-content: center;
          background: rgba(0,0,0,.85);
          gap: 12px;
          opacity: 0; transition: opacity 0.3s; pointer-events: none;
        }
        .loading-overlay.visible { opacity: 1; pointer-events: auto; }
        /* Semi-transparent overlay when refreshing an existing image — old image stays visible, spinner on top */
        .loading-overlay.refreshing { background: rgba(0,0,0,.4); }
        /* SVG spinner with SMIL <animateTransform> — replaces the CSS @keyframes
           div-spinner because iOS Safari + HA mobile WebView were rendering the
           CSS-animated rotation as static (animation paused on opacity:0→1
           parent transition inside shadow DOM). SMIL animations run independently
           of CSS animation scheduling and work reliably across all WebKit versions. */
        .spinner {
          width: 36px; height: 36px;
          flex: 0 0 auto;
          display: block;
        }
        .loading-text {
          font-size: 13px; color: rgba(255,255,255,.75); font-weight: 500;
        }
        .loading-hint {
          font-size: 11px; color: rgba(255,255,255,.5); font-weight: 400;
          margin-top: 4px; display: block; text-align: center; max-width: 220px;
        }
        .loading-hint:empty { display: none; }

        /* Offline overlay — shown when status sensor is OFFLINE */
        .offline-overlay {
          position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 8;
          display: none;
          flex-direction: column; align-items: center; justify-content: center;
          background: rgba(20, 20, 20, 0.82);
          backdrop-filter: grayscale(100%) blur(3px);
          -webkit-backdrop-filter: grayscale(100%) blur(3px);
          gap: 10px;
          pointer-events: none;
          animation: offline-pulse 3s ease-in-out infinite;
        }
        .offline-overlay.visible { display: flex; }
        @keyframes offline-pulse {
          0%, 100% { background: rgba(20, 20, 20, 0.78); }
          50%      { background: rgba(40, 20, 20, 0.88); }
        }
        .offline-overlay svg {
          width: 48px; height: 48px;
          stroke: #ff453a; stroke-width: 2; fill: none;
          filter: drop-shadow(0 0 8px rgba(255, 69, 58, 0.5));
        }
        .offline-overlay .offline-title {
          font-size: 18px; font-weight: 700; color: #ff453a;
          letter-spacing: 1px; text-transform: uppercase;
          text-shadow: 0 0 10px rgba(255, 69, 58, 0.4);
        }
        .offline-overlay .offline-subtitle {
          font-size: 12px; color: rgba(255,255,255,.7);
          font-weight: 400; max-width: 80%; text-align: center; line-height: 1.4;
        }

        /* Auth/integration overlay — shown when camera entity is unavailable
           (coordinator failed, e.g. Bosch Cloud refresh token rejected) */
        .auth-overlay {
          position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 9;
          display: none;
          flex-direction: column; align-items: center; justify-content: center;
          background: rgba(20, 20, 20, 0.88);
          backdrop-filter: blur(4px);
          -webkit-backdrop-filter: blur(4px);
          gap: 14px;
          pointer-events: auto;
        }
        .auth-overlay.visible { display: flex; }
        .auth-overlay svg {
          width: 48px; height: 48px;
          stroke: #ff9f0a; stroke-width: 2; fill: none;
          filter: drop-shadow(0 0 8px rgba(255, 159, 10, 0.5));
        }
        .auth-overlay .auth-title {
          font-size: 16px; font-weight: 700; color: #ff9f0a;
          letter-spacing: 0.5px; text-align: center;
          text-shadow: 0 0 10px rgba(255, 159, 10, 0.35);
        }
        .auth-overlay .auth-subtitle {
          font-size: 12px; color: rgba(255,255,255,.75);
          font-weight: 400; max-width: 85%; text-align: center; line-height: 1.45;
        }
        .auth-overlay .auth-btn {
          margin-top: 4px;
          padding: 8px 18px;
          background: #ff9f0a; color: #1a1a1a;
          border: none; border-radius: 8px;
          font-size: 13px; font-weight: 600;
          cursor: pointer;
          text-decoration: none;
          transition: filter .15s;
        }
        .auth-overlay .auth-btn:hover { filter: brightness(1.1); }
        .auth-overlay .auth-btn:active { filter: brightness(0.9); }

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
        .btn-row { display: flex; gap: 8px; padding: 8px 12px 12px; }
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
        .btn-privacy-inline { background: rgba(99,99,102,.15); color: var(--secondary-text-color, #8e8e93); flex: 0 0 auto; padding: 9px 12px; display: none; }
        .btn-privacy-inline.on { background: rgba(255,69,58,.18); color: #ff453a; }
        :host(.minimal) .btn-privacy-inline { display: inline-flex; }
        :host(.minimal) .switch-rows > .privacy-row { display: none; }
        .btn-overflow { background: rgba(99,99,102,.15); color: var(--secondary-text-color, #8e8e93); flex: 0 0 auto; padding: 9px 12px; display: none; }
        :host(.minimal) .btn-overflow { display: inline-flex; }
        :host(.minimal.overflow-open) .btn-overflow { background: rgba(10,132,255,.18); color: #0a84ff; }

        /* Minimal layout: hide everything non-essential until user taps ⋮.
         * Visible baseline: image, btn-row (Snapshot/Stream/⋮/Vollbild),
         * Privacy toggle. The overflow-open class (toggled by the ⋮ button) re-
         * reveals the hidden sections as a single flat panel — no separate popup
         * needed, just a progressive disclosure of existing controls. */
        :host(.minimal) .info-row { display: none; }
        :host(.minimal) .switch-rows { display: none; }
        :host(.minimal) .btn-row { padding-bottom: 8px; }
        :host(.minimal) .accordion,
        :host(.minimal) .pan-row,
        :host(.minimal) .pan-slider-row,
        :host(.minimal) .automation-row { display: none; }
        :host(.minimal.overflow-open) .info-row { display: flex; }
        :host(.minimal.overflow-open) .switch-rows { display: flex; padding: 0 12px 12px; }
        :host(.minimal.overflow-open) .switch-rows > .sw-row { display: flex; }
        :host(.minimal.overflow-open) .accordion,
        :host(.minimal.overflow-open) .pan-row,
        :host(.minimal.overflow-open) .pan-slider-row,
        :host(.minimal.overflow-open) .automation-row { display: block; }
        :host(.minimal.overflow-open) .pan-row { display: flex; }
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

        /* Pending: request in flight — subtle fade while waiting for HA/Bosch confirm */
        .sw-row.pending,
        .btn.pending { opacity: 0.7; }
        .sw-row.pending .sw-toggle,
        .btn.pending { animation: pendingPulse 1.2s ease-in-out infinite; }
        @keyframes pendingPulse { 0%,100%{filter:brightness(1)} 50%{filter:brightness(0.75)} }
        /* Error: 2s red outline + short shake to signal failed service call */
        .sw-row.error,
        .btn.error { animation: errorFlash 0.6s ease-in-out 0s 3; box-shadow: 0 0 0 2px rgba(255,69,58,.55); }
        @keyframes errorFlash {
          0%,100% { box-shadow: 0 0 0 2px rgba(255,69,58,.55); }
          50%     { box-shadow: 0 0 0 3px rgba(255,69,58,.15); }
        }

        /* Privacy placeholder — shown when no image + privacy mode is ON.
           Was rgba(0,0,0,.82) which read as a hard black wall over the
           camera. Mid-tone .55 + a subtle backdrop blur lets a hint of the
           dimmed camera image show through, signalling "privacy on but the
           camera is fine" rather than "this view is dead". */
        .privacy-placeholder {
          position: absolute; top: 0; right: 0; bottom: 0; left: 0;
          display: flex; flex-direction: column; align-items: center; justify-content: center;
          background: rgba(20,20,22,.55);
          backdrop-filter: blur(8px);
          -webkit-backdrop-filter: blur(8px);
          gap: 10px;
          opacity: 0; transition: opacity 0.3s; pointer-events: none;
        }
        .privacy-placeholder.visible { opacity: 1; }
        .privacy-placeholder svg { width: 44px; height: 44px; color: rgba(255,255,255,.5); }
        .privacy-placeholder span { font-size: 13px; color: rgba(255,255,255,.6); font-weight: 500; }
        /* Day mode: lighter overlay with darker glyph for legibility */
        :host(.apple-style.mode-day) .privacy-placeholder {
          background: rgba(240,240,242,.6);
        }
        :host(.apple-style.mode-day) .privacy-placeholder svg { color: rgba(28,28,30,.55); }
        :host(.apple-style.mode-day) .privacy-placeholder span { color: rgba(28,28,30,.65); }

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

        /* Service grid inside accordion */
        .svc-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; padding: 4px 0; }
        .svc-btn { display: flex; align-items: center; gap: 6px; padding: 8px 10px; border-radius: 8px; border: 1px solid rgba(255,255,255,.1); background: rgba(255,255,255,.03); color: var(--primary-text-color, #e1e1e1); font-size: 11px; cursor: pointer; transition: background .15s; }
        .svc-btn:hover { background: rgba(255,255,255,.08); }
        .svc-btn:active { background: rgba(255,255,255,.12); }
        .svc-btn svg { width: 16px; height: 16px; flex-shrink: 0; }
        .svc-btn.running { opacity: 0.5; pointer-events: none; }
        /* Rule row inside accordion */
        .rule-row { display: flex; align-items: center; justify-content: space-between; padding: 5px 4px; font-size: 12px; border-bottom: 1px solid rgba(255,255,255,.04); }
        .rule-row .rule-info { flex: 1; min-width: 0; }
        .rule-row .rule-name { font-weight: 500; color: var(--primary-text-color, #e1e1e1); }
        .rule-row .rule-time { color: #999; font-size: 11px; }
        .rule-row .rule-days { color: #888; font-size: 10px; }
        .rule-row .rule-toggle { cursor: pointer; padding: 2px 8px; border-radius: 4px; border: 1px solid rgba(255,255,255,.15); background: transparent; color: #999; font-size: 11px; margin-left: 6px; }
        .rule-row .rule-toggle.active { background: rgba(52,199,89,.15); color: #34c759; border-color: rgba(52,199,89,.3); }
        .rule-row .rule-delete { cursor: pointer; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(255,59,48,.2); background: transparent; color: #666; font-size: 11px; margin-left: 4px; }
        .rule-row .rule-delete:hover { background: rgba(255,59,48,.15); color: #ff3b30; }
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

      <style>
        /* ========================================================
         * Apple-style overlay layer (v13.0.0)
         * Active only when host has .apple-style class. Adds:
         *   - glass title-pill + status badge overlaying top of video
         *   - glass pill-bar with circular buttons overlaying bottom of video
         *   - hides legacy .header / .info-row / .btn-row
         * ====================================================== */
        :host(.apple-style) ha-card {
          /* Card-specific vars (issue #21) — NOT the global --ha-card-* theme
             tokens. A user whose dashboard theme zeroes --ha-card-border-radius
             must still get the apple-style rounding by default; the optional
             border_radius / box_shadow card config sets --bosch-card-* to
             opt into a custom look without us inheriting the global theme value. */
          border-radius: var(--bosch-card-radius, 22px);
          box-shadow: var(--bosch-card-shadow, 0 4px 24px rgba(0,0,0,.08), 0 1px 3px rgba(0,0,0,.06));
        }
        @media (prefers-color-scheme: dark) {
          :host(.apple-style) ha-card {
            box-shadow: var(--bosch-card-shadow, 0 6px 28px rgba(0,0,0,.55), 0 1px 3px rgba(0,0,0,.4));
          }
        }
        /* Hover affordance parity with the overview tiles (issue #15.1): a
           subtle box-shadow lift on pointer devices only. No transform, so the
           full-width single card never shifts position; the resting state still
           honors the user's --ha-card-box-shadow theme var (issue #21). */
        :host(.apple-style) ha-card { transition: box-shadow .18s ease; }
        @media (hover: hover) and (pointer: fine) {
          :host(.apple-style) ha-card:hover {
            box-shadow: 0 8px 30px rgba(0,0,0,.16), 0 2px 6px rgba(0,0,0,.10);
          }
        }
        :host(.apple-style) .header,
        :host(.apple-style) .info-row,
        :host(.apple-style) .btn-row { display: none !important; }
        /* Legacy on-video text overlays ("Letztes: ..." / "30 Events heute")
           clash with the glass title-pill + status badge. The same info now
           lives in the Apple-style overlays, so suppress the old layer. */
        :host(.apple-style) .img-overlay { display: none !important; }

        /* In Apple mode, switch-rows + accordions collapse via max-height
           transition (smooth slide) instead of hard display:none → block.
           display:none breaks the transition; max-height:0 + overflow:hidden
           achieves the same visual hiding while remaining animatable. */
        :host(.apple-style) .switch-rows,
        :host(.apple-style) .accordion,
        :host(.apple-style) .pan-row {
          max-height: 0;
          overflow: hidden;
          opacity: 0;
          /* max-height:0 does NOT clip padding/borders (content-box), so the
             switch-rows' 12px bottom padding + each accordion's 1px divider
             rendered as a white strip below the video when collapsed (issue:
             white gap, 2026-05-29). Zero them while collapsed; restore on open. */
          padding-top: 0;
          padding-bottom: 0;
          border-top-width: 0;
          border-bottom-width: 0;
          transition: max-height .35s cubic-bezier(.4,0,.2,1),
                      opacity .25s ease;
        }
        :host(.apple-style.overflow-open) .switch-rows,
        :host(.apple-style.overflow-open) .accordion,
        :host(.apple-style.overflow-open) .pan-row {
          max-height: 2000px;
          opacity: 1;
        }
        :host(.apple-style.overflow-open) .switch-rows { padding: 0 12px 12px; }
        :host(.apple-style.overflow-open) .accordion { border-top-width: 1px; }
        /* Default .pan-section { padding: 0 12px 12px } produces a 12 px
           white bar below the image when pan-row is hidden (apple-style,
           overflow closed). Drop padding to zero in that state; bring the
           breathing room back only when the section actually shows content. */
        :host(.apple-style) .pan-section { padding: 0; }
        :host(.apple-style.overflow-open) .pan-section { padding: 0 12px 12px; }

        /* Suppress redundant top-right "connecting" badge while the central
           loading overlay is up — both convey the same state, and the overlay
           carries the timer/hint ("ca. 25–35 s bis erstes Bild"). Once the
           overlay hides, the badge re-appears as LIVE / OFFLINE / etc. */
        :host(.apple-style) .img-wrapper:has(.loading-overlay.visible) .ap-badge.connecting {
          display: none;
        }

        /* Glass material primitive ------------------------------- */
        /* Near-opaque night glass (.92) — earlier mid-tone .42/.55 left the
           backdrop bleeding through, making text + icons washed out during
           snapshot-loading (bright loading-overlay backdrop) and on bright
           daylight scenes. Sacrifice some glass-transparency for guaranteed
           contrast on every backdrop. The blur still gives the soft Material
           edges where the pill meets the video. Border bumped to 1px so it
           renders cleanly on high-DPI mobile (.5px collapsed to 0 on some
           devices, leaving the pill rim invisible). */
        .ap-glass {
          background: rgba(22,22,24,.92);
          backdrop-filter: blur(20px) saturate(1.4);
          -webkit-backdrop-filter: blur(20px) saturate(1.4);
          border: 1px solid rgba(255,255,255,.12);
          color: #fff;
          box-shadow: 0 2px 8px rgba(0,0,0,.22);
          /* GPU composite layer — prevents scroll-flicker on iOS WKWebView */
          transform: translateZ(0);
          will-change: transform;
        }
        /* Mobile WebKit (HA Companion / iOS Safari) doesn't always honour
           backdrop-filter — fall back to a slightly denser solid tint so the
           glass pill stays legible without the blur. */
        @supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
          .ap-glass { background: rgba(20,20,22,.72); }
        }

        /* Top overlay (title pill + status badge) ---------------- */
        .ap-top {
          position: absolute; top: 12px; left: 12px; right: 12px;
          display: flex; align-items: center; justify-content: space-between;
          gap: 8px; z-index: 6; pointer-events: none;
        }
        .ap-top > * { pointer-events: auto; }
        .ap-title-pill {
          display: inline-flex; align-items: center; gap: 8px;
          padding: 8px 14px 8px 11px; border-radius: 999px;
          font-size: 14px; font-weight: 600;
          letter-spacing: .005em;
          max-width: 70%;
          line-height: 1;
        }
        .ap-title-pill .ap-title-text {
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
          /* No text-shadow — relying on solid pill bg for contrast. Earlier
             multi-layer shadows + halos washed the glyph on certain mobile
             renderers. Plain glyph on near-opaque pill is the safe bet. */
          text-shadow: none;
          /* Force-visible against fragile mobile renderers — color inherits
             from .ap-glass / mode-day override but pinning it here means
             no parent class can accidentally null it out via shorthand. */
          color: inherit;
        }
        .ap-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: #8e8e93; }
        .ap-dot.online   { background: #30d158; box-shadow: 0 0 0 3px rgba(48,209,88,.22); }
        /* Privacy = "shielded" via iOS systemPurple — Apple convention for
           locked / private states. Old .warn (orange) reflexively read as
           "caution / warning" which mismatches a deliberate privacy state. */
        .ap-dot.privacy  { background: #af52de; box-shadow: 0 0 0 3px rgba(175,82,222,.22); }
        .ap-dot.warn     { background: #ff9f0a; box-shadow: 0 0 0 3px rgba(255,159,10,.22); }
        .ap-dot.offline  { background: #ff453a; }

        .ap-top-right { display: inline-flex; align-items: center; gap: 6px; }
        .ap-badge {
          display: inline-flex; align-items: center; gap: 6px;
          padding: 6px 10px; border-radius: 999px;
          font-size: 11px; font-weight: 700; letter-spacing: .04em;
          text-transform: uppercase;
        }
        .ap-badge.live {
          background: rgba(255,59,48,.88); color: #fff;
          border: 0.5px solid rgba(255,255,255,.22);
        }
        .ap-badge.live::before {
          content: ""; width: 5px; height: 5px; border-radius: 50%;
          background: #fff; animation: ap-pulse 1.4s ease-in-out infinite;
        }
        @keyframes ap-pulse { 0%,100% { opacity: 1 } 50% { opacity: .4 } }
        .ap-badge.connecting {
          /* WCAG fix: dark text on amber (was white-on-amber = 2.7:1, fails AA).
             Dark on amber yields ~11:1, well above threshold. */
          background: rgba(255,159,10,.95); color: #1a1a1a;
          border: 0.5px solid rgba(255,255,255,.2);
        }
        .ap-badge.offline  { background: rgba(120,120,128,.55); color: #fff; border: 0.5px solid rgba(255,255,255,.18); }
        .ap-badge.hidden   { display: none; }

        /* Bottom pill-bar overlay -------------------------------- */
        .ap-pill-bar {
          position: absolute; left: 50%; bottom: 12px;
          transform: translateX(-50%);
          display: inline-flex; align-items: center;
          gap: 6px; padding: 6px;
          border-radius: 999px; z-index: 6;
          max-width: calc(100% - 24px);
        }
        .ap-pill-btn {
          width: 42px; height: 42px; border-radius: 50%;
          display: inline-flex; align-items: center; justify-content: center;
          background: rgba(255,255,255,.12);
          border: 0.5px solid rgba(255,255,255,.18);
          color: #fff; cursor: pointer;
          padding: 0; flex-shrink: 0;
          transition: background .15s ease, transform .12s ease;
        }
        .ap-pill-btn:hover { background: rgba(255,255,255,.22); }
        .ap-pill-btn:active { transform: scale(.92); }
        .ap-pill-btn svg { width: 19px; height: 19px; fill: #fff; pointer-events: none; }
        .ap-pill-btn.on { background: rgba(255,255,255,.93); }
        .ap-pill-btn.on svg { fill: #1c1c1e; }
        .ap-pill-btn.danger { background: rgba(255,59,48,.85); border-color: rgba(255,255,255,.22); }
        .ap-pill-btn.danger:hover { background: rgba(255,59,48,1); }
        .ap-pill-btn.connecting { background: rgba(255,159,10,.85); border-color: rgba(255,255,255,.22); }
        .ap-pill-btn[hidden] { display: none !important; }

        /* Phone-narrow: keep all buttons visible, shrink slightly */
        @media (max-width: 380px) {
          .ap-pill-btn { width: 38px; height: 38px; }
          .ap-pill-btn svg { width: 17px; height: 17px; }
          .ap-pill-bar { gap: 4px; padding: 4px; }
        }


        /* Img-wrapper needs relative + own stacking context so the absolute
           overlays cannot escape upward over the HA tab bar / sidebar when
           the card is rendered tall in a panel:true view. isolation:isolate
           creates a new stacking context; contain:paint clips rendering to
           the wrapper box so partially-scrolled overlays do not bleed past
           the visible region. (No backticks inside CSS comments — this CSS
           is itself inside a JS template literal.) */
        :host(.apple-style) .img-wrapper {
          border-radius: 0;
          position: relative;
          isolation: isolate;
          contain: paint;
          overflow: hidden;
        }
        /* Belt-and-braces: keep overlay z-index low — the wrapper's new
           stacking context confines them anyway, but a low value protects
           against future ancestors that might break isolation. */
        :host(.apple-style) .ap-top,
        :host(.apple-style) .ap-pill-bar { z-index: 2; }

        /* ========================================================
         * Material You (Android / M3) theme overrides
         * Active when host has .theme-android. Swaps the glass blur for
         * solid M3 surface tones, bumps the card to the M3 large container
         * radius (28px), and recolors button states with M3 tonal tokens.
         * Default theme (.theme-ios) keeps the iOS look above untouched.
         * ====================================================== */
        :host(.apple-style.theme-android) ha-card {
          /* M3 large radius (28px) as the Android default; the optional
             border_radius card config (--bosch-card-radius) overrides it
             (issue #21). !important still beats ha-card's base rule. */
          border-radius: var(--bosch-card-radius, 28px) !important;
        }
        :host(.apple-style.theme-android) .ap-glass {
          background: rgba(73, 69, 79, .92);   /* M3 surface-variant dark */
          backdrop-filter: none;
          -webkit-backdrop-filter: none;
          border: 0;
          color: #E6E0E9;                       /* M3 on-surface dark */
          box-shadow: 0 1px 3px rgba(0,0,0,.3);
        }
        :host(.apple-style.theme-android) .ap-title-pill {
          border-radius: 8px;                   /* M3 chip shape */
          font-family: var(--primary-font-family, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif);
          font-weight: 500;
        }
        :host(.apple-style.theme-android) .ap-title-pill .ap-title-text {
          text-shadow: none;                    /* Solid surface needs no shadow */
        }
        :host(.apple-style.theme-android) .ap-badge {
          border-radius: 8px;
          font-family: var(--primary-font-family, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif);
          letter-spacing: 0;
          font-weight: 500;
        }
        :host(.apple-style.theme-android) .ap-badge.live {
          background: rgba(242, 184, 181, .95); /* M3 error dark tonal */
          color: #601410;                       /* M3 on-error-container dark */
          border: 0;
        }
        :host(.apple-style.theme-android) .ap-badge.connecting {
          background: rgba(232, 222, 248, .95); /* M3 secondary-container dark */
          color: #1D192B;
          border: 0;
        }
        :host(.apple-style.theme-android) .ap-badge.privacy {
          background: rgba(208, 188, 255, .95); /* M3 primary-container dark */
          color: #381E72;                       /* M3 on-primary-container dark */
          border: 0;
        }
        :host(.apple-style.theme-android) .ap-badge.offline {
          background: rgba(73, 69, 79, .92);
          /* WCAG fix: was #CAC4D0 on surface-variant = 4.1:1 (borderline fail
             for 11px font). #E6E0E9 = M3 on-surface = ~6.5:1. */
          color: #E6E0E9;
          border: 0;
        }
        :host(.apple-style.theme-android) .ap-pill-bar {
          background: rgba(73, 69, 79, .92);
          backdrop-filter: none;
          -webkit-backdrop-filter: none;
          border: 0;
          border-radius: 28px;                  /* M3 large radius for the bar */
        }
        :host(.apple-style.theme-android) .ap-pill-btn {
          background: transparent;
          border: 0;
          color: #E6E0E9;
        }
        :host(.apple-style.theme-android) .ap-pill-btn svg { fill: #E6E0E9; }
        :host(.apple-style.theme-android) .ap-pill-btn:hover {
          /* M3 state layer: 8% opacity overlay of on-surface */
          background: rgba(230, 224, 233, .08);
        }
        :host(.apple-style.theme-android) .ap-pill-btn:active {
          /* M3 pressed state: 12% opacity overlay */
          background: rgba(230, 224, 233, .12);
        }
        :host(.apple-style.theme-android) .ap-pill-btn.on {
          background: #D0BCFF;                  /* M3 primary dark */
        }
        :host(.apple-style.theme-android) .ap-pill-btn.on svg { fill: #381E72; }
        :host(.apple-style.theme-android) .ap-pill-btn.danger {
          background: #F2B8B5;                  /* M3 error dark */
        }
        :host(.apple-style.theme-android) .ap-pill-btn.danger svg { fill: #601410; }
        :host(.apple-style.theme-android) .ap-pill-btn.connecting {
          background: #E8DEF8;                  /* M3 secondary-container dark */
        }
        :host(.apple-style.theme-android) .ap-pill-btn.connecting svg { fill: #1D192B; }
        :host(.apple-style.theme-android) .ap-dot.online   { background: #6FE899; box-shadow: 0 0 0 3px rgba(111,232,153,.18); }
        :host(.apple-style.theme-android) .ap-dot.warn     { background: #FFB68A; box-shadow: 0 0 0 3px rgba(255,182,138,.18); }
        :host(.apple-style.theme-android) .ap-dot.offline  { background: #F2B8B5; }

        /* Theme switcher row inside the Mehr menu (visible when overflow-open) */
        .ap-theme-switcher {
          align-items: center; justify-content: space-between;
          padding: 12px 14px;
          font-size: 14px;
          border-top: 0.5px solid rgba(120,120,128,.18);
        }
        .ap-theme-toggle {
          display: inline-flex; align-items: center; gap: 4px;
          padding: 3px; border-radius: 999px;
          background: rgba(120,120,128,.16);
        }
        .ap-theme-toggle button {
          font: inherit; font-size: 13px; font-weight: 500;
          padding: 6px 14px; border-radius: 999px;
          background: transparent; border: 0;
          /* WCAG fix: #8e8e93 on white = 2.85:1 (fails AA). #6c6c70 = ~4.6:1. */
          color: var(--secondary-text-color, #6c6c70);
          cursor: pointer;
          transition: background .15s ease, color .15s ease;
        }
        /* currentColor fallback works in both light + dark mode without
           requiring the user to explicitly set mode-night class. */
        .ap-theme-toggle button:hover { color: var(--primary-text-color, currentColor); }
        .ap-theme-toggle button.on {
          background: var(--card-background-color, #fff);
          color: var(--primary-text-color, #1c1c1e);
          box-shadow: 0 1px 2px rgba(0,0,0,.12);
        }
        :host(.apple-style.theme-android) .ap-theme-toggle { border-radius: 8px; padding: 2px; }
        :host(.apple-style.theme-android) .ap-theme-toggle button { border-radius: 8px; }
        :host(.apple-style.theme-android) .ap-theme-toggle button.on {
          background: #D0BCFF; color: #381E72;
        }

        /* ========================================================
         * Day/Night card-chrome mode (v13.0.1)
         * .mode-day  -> force light card (white bg, dark text)
         * .mode-night -> force dark card (M3 dark / iOS systemBackground dark)
         * No class -> auto, inherit from HA theme CSS vars
         * Glass overlays on the video are unaffected — they stay dark for
         * legibility regardless of the chrome mode.
         * ====================================================== */
        :host(.apple-style.mode-day) ha-card {
          background: #ffffff;
          color: #1c1c1e;
        }
        :host(.apple-style.mode-night) ha-card {
          background: #1c1c1e;
          color: #ffffff;
        }
        /* Android M3 light surface tones when both apple+android+day are on */
        :host(.apple-style.theme-android.mode-day) ha-card {
          background: #FEF7FF !important;
          color: #1D1B20 !important;
        }
        :host(.apple-style.theme-android.mode-night) ha-card {
          background: #211F26 !important;
          color: #E6E0E9 !important;
        }
        /* Force text + secondary-text + divider variables under day mode so
           switch-row labels, accordion chevrons, slider track edges follow.
           Night mode also pins the variables explicitly so the user gets a
           consistent dark card even when HA's active theme is light. */
        :host(.apple-style.mode-day) {
          --primary-text-color: #1c1c1e;
          --secondary-text-color: rgba(60,60,67,.6);
          --divider-color: rgba(60,60,67,.12);
          --card-background-color: #ffffff;
        }
        :host(.apple-style.mode-night) {
          --primary-text-color: #ffffff;
          --secondary-text-color: rgba(235,235,245,.6);
          --divider-color: rgba(84,84,88,.5);
          --card-background-color: #1c1c1e;
        }
        :host(.apple-style.theme-android.mode-day) {
          --primary-text-color: #1D1B20;
          --secondary-text-color: #49454F;
          --divider-color: rgba(73,69,79,.2);
          --card-background-color: #FEF7FF;
        }
        :host(.apple-style.theme-android.mode-night) {
          --primary-text-color: #E6E0E9;
          --secondary-text-color: #CAC4D0;
          --divider-color: rgba(202,196,208,.2);
          --card-background-color: #211F26;
        }

        /* === Day mode lightens the video-overlay glass but keeps the text/
         *     icons white ===
         * Earlier attempt at a white-pill in day mode broke text visibility
         * because dark text on a glass-blended-with-bright-backdrop dropped
         * below the contrast threshold. Solution: keep text + icons white
         * (always works on dark glass) but make the glass itself lighter +
         * more transparent in day so the video shows through and the
         * overall card feels brighter. Night stays denser/darker. The blur
         * radius is also higher in day so the lighter glass still feels
         * like a Material, not a tint film. iOS-day only — :not(.theme-android)
         * prevents this rule from poaching the Android M3 surface-variant
         * treatment when both mode-day + theme-android are active. */
        :host(.apple-style.mode-day:not(.theme-android)) .ap-glass {
          background: rgba(55,55,60,.42);
          backdrop-filter: blur(28px) saturate(1.6) brightness(1.05);
          -webkit-backdrop-filter: blur(28px) saturate(1.6) brightness(1.05);
          border-color: rgba(255,255,255,.22);
        }
        /* The pill-bar's inactive buttons get a brighter inner tint in day
           so they read clearly as tappable surfaces inside the lighter pill,
           and the icon stroke gets a touch more weight against the brighter
           backdrop. Active (.on) buttons stay solid white-tile to read as
           the primary "selected" state. Danger stays systemRed. */
        :host(.apple-style.mode-day:not(.theme-android)) .ap-pill-btn {
          background: rgba(255,255,255,.22);
          border-color: rgba(255,255,255,.28);
        }
        :host(.apple-style.mode-day:not(.theme-android)) .ap-pill-btn:hover { background: rgba(255,255,255,.32); }
        /* Active "on" button in day mode reads as a raised solid-white tile:
           full-opacity background, soft drop shadow + thin bright rim, dark
           icon at full contrast. The combination pops cleanly against the
           transparent grey pill backdrop without needing a saturated accent
           colour — matches Apple Home's "selected control" treatment. */
        :host(.apple-style.mode-day) .ap-pill-btn.on {
          background: #ffffff;
          border-color: rgba(255,255,255,.85);
          box-shadow:
            0 3px 10px rgba(0,0,0,.32),
            0 0 0 1px rgba(255,255,255,.5) inset;
        }
        :host(.apple-style.mode-day) .ap-pill-btn.on svg { fill: #1c1c1e; }
        :host(.apple-style.mode-day) .ap-pill-btn.on:hover { background: #ffffff; }

        /* Camera-state ACTIVE buttons: Stream + Privacy get systemRed (a
           non-neutral hardware state). Light gets amber — a lamp/bulb is
           conventionally yellow/amber when on (think of every smart-bulb
           UI ever shipped). Splitting these avoids the audit's "everything
           red" collision when stream + offline + light are all active at
           once. Fullscreen.on falls through to the generic white-tile
           rule above (viewing-mode, not hardware-state). */
        :host(.apple-style) .ap-pill-btn#ap-btn-stream.on,
        :host(.apple-style) .ap-pill-btn#ap-btn-privacy.on {
          background: rgba(255,59,48,.92);
          border-color: rgba(255,255,255,.22);
          box-shadow: none;
        }
        :host(.apple-style) .ap-pill-btn#ap-btn-stream.on svg,
        :host(.apple-style) .ap-pill-btn#ap-btn-privacy.on svg { fill: #fff; }
        :host(.apple-style.mode-day) .ap-pill-btn#ap-btn-stream.on,
        :host(.apple-style.mode-day) .ap-pill-btn#ap-btn-privacy.on {
          background: rgba(255,59,48,.95);
          box-shadow:
            0 3px 10px rgba(255,59,48,.35),
            0 0 0 1px rgba(255,255,255,.3) inset;
        }
        /* Light = amber (iOS systemYellow / M3 tertiary tonal) — lamp metaphor */
        :host(.apple-style) .ap-pill-btn#ap-btn-light.on {
          background: rgba(255,179,0,.92);
          border-color: rgba(255,255,255,.22);
          box-shadow: none;
        }
        :host(.apple-style) .ap-pill-btn#ap-btn-light.on svg { fill: #1c1c1e; }
        :host(.apple-style.mode-day) .ap-pill-btn#ap-btn-light.on {
          background: rgba(255,179,0,.95);
          box-shadow:
            0 3px 10px rgba(255,179,0,.4),
            0 0 0 1px rgba(255,255,255,.4) inset;
        }
        /* Android-theme: M3 error tonal for Stream + Privacy, tertiary for Light */
        :host(.apple-style.theme-android) .ap-pill-btn#ap-btn-stream.on,
        :host(.apple-style.theme-android) .ap-pill-btn#ap-btn-privacy.on {
          background: #F2B8B5;
        }
        :host(.apple-style.theme-android) .ap-pill-btn#ap-btn-stream.on svg,
        :host(.apple-style.theme-android) .ap-pill-btn#ap-btn-privacy.on svg { fill: #601410; }
        :host(.apple-style.theme-android) .ap-pill-btn#ap-btn-light.on {
          background: #FFD8A8;                  /* M3 tertiary-container dark */
        }
        :host(.apple-style.theme-android) .ap-pill-btn#ap-btn-light.on svg { fill: #4F2500; }
        :host(.apple-style.theme-android.mode-day) .ap-pill-btn#ap-btn-stream.on,
        :host(.apple-style.theme-android.mode-day) .ap-pill-btn#ap-btn-privacy.on {
          background: #B3261E;
        }
        :host(.apple-style.theme-android.mode-day) .ap-pill-btn#ap-btn-stream.on svg,
        :host(.apple-style.theme-android.mode-day) .ap-pill-btn#ap-btn-privacy.on svg { fill: #fff; }
        :host(.apple-style.theme-android.mode-day) .ap-pill-btn#ap-btn-light.on {
          background: #7D5260;                  /* M3 tertiary light */
        }
        :host(.apple-style.theme-android.mode-day) .ap-pill-btn#ap-btn-light.on svg { fill: #fff; }
        /* Day-mode badge gets the same lighter treatment so it doesn't pop
           as a saturated solid color against the airy overlay. */
        :host(.apple-style.mode-day) .ap-badge.live {
          background: rgba(255,59,48,.85); border-color: rgba(255,255,255,.22);
        }

        /* Mode switcher row inside the Mehr menu */
        .ap-mode-switcher {
          align-items: center; justify-content: space-between;
          padding: 12px 14px;
          font-size: 14px;
          border-top: 0.5px solid var(--divider-color, rgba(120,120,128,.18));
        }
        .ap-mode-toggle {
          display: inline-flex; align-items: center; gap: 4px;
          padding: 3px; border-radius: 999px;
          background: rgba(120,120,128,.16);
        }
        .ap-mode-toggle button {
          font: inherit; font-size: 13px; font-weight: 500;
          padding: 6px 14px; border-radius: 999px;
          background: transparent; border: 0;
          /* WCAG fix: matches theme-toggle (was #8e8e93 = 2.85:1, fails AA). */
          color: var(--secondary-text-color, #6c6c70);
          cursor: pointer;
          transition: background .15s ease, color .15s ease;
        }
        .ap-mode-toggle button:hover { color: var(--primary-text-color, #1c1c1e); }
        .ap-mode-toggle button.on {
          background: var(--card-background-color, #fff);
          color: var(--primary-text-color, #1c1c1e);
          box-shadow: 0 1px 2px rgba(0,0,0,.12);
        }
        :host(.apple-style.theme-android) .ap-mode-toggle { border-radius: 8px; padding: 2px; }
        :host(.apple-style.theme-android) .ap-mode-toggle button { border-radius: 8px; }
        :host(.apple-style.theme-android) .ap-mode-toggle button.on {
          background: #D0BCFF; color: #381E72;
        }

        /* === Accessibility + animation polish ============================ */
        /* Focus-visible: keyboard navigation feedback. systemBlue ring with
           2px offset on the pill-bar; tighter 1px on the toggle chips so
           it fits inside the toggle track. */
        :host(.apple-style) .ap-pill-btn:focus-visible {
          outline: 2px solid #0a84ff;
          outline-offset: 2px;
        }
        :host(.apple-style) .ap-theme-toggle button:focus-visible,
        :host(.apple-style) .ap-mode-toggle button:focus-visible {
          outline: 2px solid #0a84ff;
          outline-offset: 1px;
        }

        /* prefers-reduced-motion: suppress all animations + transitions for
           users with vestibular sensitivity or who set "Reduce Motion" in
           iOS / macOS Accessibility. WCAG 2.1 SC 2.3.3. */
        @media (prefers-reduced-motion: reduce) {
          :host(.apple-style) *,
          :host(.apple-style) *::before,
          :host(.apple-style) *::after {
            animation-duration: 0.01ms !important;
            animation-iteration-count: 1 !important;
            transition-duration: 0.01ms !important;
          }
        }

        /* prefers-contrast: more (high-contrast OS preference, e.g. macOS
           "Increase Contrast"). Bumps glass to near-opaque + adds visible
           hairline borders so the design degrades gracefully. */
        @media (prefers-contrast: more) {
          :host(.apple-style) .ap-glass {
            background: rgba(0,0,0,.95) !important;
            border: 1.5px solid #fff !important;
          }
          :host(.apple-style.mode-day) .ap-glass {
            background: #fff !important;
            color: #000 !important;
            border: 1.5px solid #000 !important;
          }
          :host(.apple-style) .ap-pill-btn { border-width: 1.5px !important; }
        }

        /* Android × Day combined override (higher specificity than the
           iOS-Day rule) — M3 spec for light mode: solid surface-variant
           light tint instead of glass blur. */
        :host(.apple-style.theme-android.mode-day) .ap-glass {
          background: rgba(231,224,236,.96);    /* M3 surface-variant light */
          backdrop-filter: none;
          -webkit-backdrop-filter: none;
          border: 0;
          color: #1D1B20;
          box-shadow: 0 1px 3px rgba(0,0,0,.15);
        }
        :host(.apple-style.theme-android.mode-day) .ap-pill-bar {
          background: rgba(231,224,236,.96);
        }
        :host(.apple-style.theme-android.mode-day) .ap-pill-btn { color: #1D1B20; }
        :host(.apple-style.theme-android.mode-day) .ap-pill-btn svg { fill: #1D1B20; }
        :host(.apple-style.theme-android.mode-day) .ap-title-pill .ap-title-text { color: #1D1B20; }

        /* Android theme: drop the iOS-style press-scale because M3 design
           uses a state-layer overlay (radial ripple in spec, opacity-tint
           in our implementation) rather than the iOS bounce. */
        :host(.apple-style.theme-android) .ap-pill-btn:active { transform: none; }

        /* Offline cameras (apple-style): minimalist treatment per user
           preference — just the camera name (already in the top-left glass
           pill) and a centered "OFFLINE" label + last-seen subtitle. No
           icons, no pill-bar — there's nothing meaningful to tap when the
           camera is unreachable. */
        /* Compact tile mode: hide pill-bar + status badge so the card reduces
           to just video + title-pill — used by overview grid for Apple-Home-
           style tile rows. Click on the video opens fullscreen. */
        :host(.apple-style.compact) .ap-pill-bar,
        :host(.apple-style.compact) .ap-badge { display: none; }

        /* Last-event indicator: small glass pill bottom-right of the video
           showing "🕐 14:23" when the camera fired a motion/audio/person
           event recently. Hidden while streaming live (the LIVE badge takes
           that real estate). Wired in JS via _updateLastEventBadge(). */
        .ap-last-event {
          position: absolute;
          right: 12px; bottom: 12px;
          z-index: 4;
          display: none;
          align-items: center; gap: 6px;
          padding: 5px 10px 5px 8px;
          border-radius: 999px;
          font-size: 11px; font-weight: 600;
          background: rgba(22,22,24,.78);
          backdrop-filter: blur(14px) saturate(1.3);
          -webkit-backdrop-filter: blur(14px) saturate(1.3);
          color: #fff;
          border: .5px solid rgba(255,255,255,.14);
          pointer-events: none;
        }
        .ap-last-event.visible { display: inline-flex; }
        .ap-last-event svg { width: 12px; height: 12px; fill: currentColor; opacity: .8; }
        :host(.apple-style.compact) .ap-last-event { right: 8px; bottom: 8px; padding: 4px 8px; font-size: 10px; }
        /* Hide when streaming is active — the LIVE badge already occupies
           the visual attention budget; the last-event indicator only adds
           value during idle/snapshot mode. */
        :host(.apple-style) .ap-last-event.hide-during-stream { display: none; }

        /* Element-hiding toggles (issue #15): show_title:false / show_last_event:false. */
        :host(.no-title) .ap-top { display: none; }
        :host(.no-last-event) .ap-last-event { display: none !important; }

        :host(.apple-style.cam-offline) .ap-pill-bar { display: none; }
        :host(.apple-style.cam-offline) .offline-overlay svg { display: none; }
        /* When the camera is offline, the offline-overlay is the single
           source of truth. Suppress every other overlay that would otherwise
           stack on top: the privacy-placeholder (last-known privacy state)
           bleeds through with its own lock icon and "Privat-Modus aktiv"
           label, and the last-event pill at bottom-right adds another
           competing piece of chrome. Both hidden to leave only the title
           pill + OFFLINE label visible. */
        :host(.apple-style.cam-offline) .privacy-placeholder,
        :host(.apple-style.cam-offline) .ap-last-event { display: none !important; }
        /* The offline-overlay already shows the camera name on its own line
           (.offline-cam-name), so the top-left title pill is redundant when
           offline. On short/compact tiles the centered "Kamera Offline" pill
           landed on top of the title pill, superimposing two texts into glyph
           soup (issue: garbled offline label, 2026-05-29). Hide the top pill
           when offline — the overlay is the single source of truth. */
        :host(.apple-style.cam-offline) .ap-top { display: none !important; }
        /* Offline cameras can't be operated, so in the default EXPANDED layout
           (minimal NOT enabled) the control stack — switches, light/pan/
           diagnostics accordions, theme/mode switchers — is just noise. Hide it
           all, keeping ONLY the Automations accordion (those run HA-side and
           still work while the camera is down). When minimal IS enabled the
           whole stack is collapsed behind the ⋮ anyway, so this is scoped to
           :not(.minimal). (2026-05-29 user feedback: offline shows too much.) */
        :host(.apple-style.cam-offline:not(.minimal)) .switch-rows,
        :host(.apple-style.cam-offline:not(.minimal)) .pan-row,
        :host(.apple-style.cam-offline:not(.minimal)) .pan-section,
        :host(.apple-style.cam-offline:not(.minimal)) .ap-theme-switcher,
        :host(.apple-style.cam-offline:not(.minimal)) .ap-mode-switcher,
        :host(.apple-style.cam-offline:not(.minimal)) .accordion:not(#acc-automations) {
          display: none !important;
        }
        /* Offline overlay: drop the dim red full-cover backdrop so the last
           cached snapshot stays visible behind. The OFFLINE label + last-seen
           text sit in a single glass pill centered on the video — same
           material as the title-pill so the layer reads as a coherent
           "system overlay" instead of a separate widget. */
        :host(.apple-style.cam-offline) .offline-overlay {
          background: transparent;
          gap: 0;
          align-items: center; justify-content: center;
        }
        :host(.apple-style.cam-offline) .offline-overlay .offline-title,
        :host(.apple-style.cam-offline) .offline-overlay .offline-subtitle {
          color: #fff;
        }
        :host(.apple-style.cam-offline) .offline-overlay .offline-title {
          background: rgba(22,22,24,.92);
          backdrop-filter: blur(20px) saturate(1.4);
          -webkit-backdrop-filter: blur(20px) saturate(1.4);
          border: 1px solid rgba(255,255,255,.12);
          box-shadow: 0 2px 8px rgba(0,0,0,.22);
          padding: 9px 18px;
          border-radius: 999px;
          font-size: 14px;
          font-weight: 700;
          letter-spacing: .14em;
        }
        :host(.apple-style.cam-offline) .offline-overlay .offline-subtitle {
          font-size: 11px;
          margin-top: 8px;
          opacity: .75;
          text-shadow: 0 1px 2px rgba(0,0,0,.6);
        }
        /* Camera friendly_name on its own line between the OFFLINE pill and
           the last-seen subtitle. Visible only in apple-style cam-offline
           state; legacy / non-offline render path stays untouched. */
        .offline-cam-name { display: none; }
        :host(.apple-style.cam-offline) .offline-overlay .offline-cam-name {
          display: block;
          margin-top: 10px;
          font-size: 17px;
          font-weight: 600;
          letter-spacing: .005em;
          color: #fff;
          text-shadow: 0 1px 2px rgba(0,0,0,.6);
        }

        /* Theme + Mode switcher rows: animate via max-height too so they
           slide in/out alongside the switch-rows when Mehr is toggled. */
        :host(.apple-style) .ap-theme-switcher,
        :host(.apple-style) .ap-mode-switcher {
          display: flex;
          max-height: 0;
          overflow: hidden;
          opacity: 0;
          padding-top: 0;
          padding-bottom: 0;
          /* 0.5px border-top renders even at max-height:0 → contributes to the
             white gap below the video. Zero it while collapsed (issue: white
             gap, 2026-05-29); restore on open. */
          border-top-width: 0;
          transition: max-height .35s cubic-bezier(.4,0,.2,1),
                      opacity .25s ease, padding .25s ease;
        }
        :host(.apple-style.overflow-open) .ap-theme-switcher,
        :host(.apple-style.overflow-open) .ap-mode-switcher {
          max-height: 80px;
          opacity: 1;
          padding-top: 12px;
          padding-bottom: 12px;
          border-top-width: 0.5px;
        }

        /* Snapshot success flash: 280ms green pulse on the snapshot button
           after a service call returns. Triggered by JS adding .ok-flash. */
        @keyframes ap-snapshot-flash {
          0%   { background: rgba(48,209,88,.85); transform: scale(1); }
          50%  { background: rgba(48,209,88,.95); transform: scale(1.04); }
          100% { background: rgba(255,255,255,.12); transform: scale(1); }
        }
        :host(.apple-style) .ap-pill-btn#ap-btn-snapshot.ok-flash {
          animation: ap-snapshot-flash .42s ease-out;
        }
      </style>

      <ha-card>
        <div class="header">
          <div class="header-left">
            <div class="status-dot unknown" id="status-dot"></div>
            <span class="title" id="title">Bosch Camera</span>
          </div>
          <div style="display:flex;align-items:center;gap:6px;flex-shrink:0;margin-left:auto">
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
          <video class="cam-video" id="cam-video" autoplay muted playsinline webkit-playsinline preload="auto" disableremoteplayback style="display:none; cursor:pointer"></video>
          <div class="ios-hls-banner" id="ios-hls-banner">
            <span>ℹ HLS-Modus (kein WebRTC über Tunnel)</span>
          </div>
          <div class="tap-to-play-overlay" id="tap-to-play-overlay">
            <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
            <span class="ttp-label">Zum Abspielen tippen</span>
            <span class="ttp-hint">Oder in den HA-App-Einstellungen „Videos automatisch abspielen" aktivieren</span>
          </div>
          <div class="auto-play-gate" id="auto-play-gate">
            <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
            <span class="apg-label">Stream starten</span>
            <span class="apg-hint">Antippen, um den Live-Stream zu starten</span>
          </div>
          <div class="loading-overlay visible" id="loading-overlay">
            <svg class="spinner" width="36" height="36" viewBox="0 0 40 40" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
              <circle cx="20" cy="20" r="16" fill="none" stroke="rgba(255,255,255,.2)" stroke-width="3"/>
              <circle cx="20" cy="20" r="16" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-dasharray="25 75">
                <animateTransform attributeName="transform" attributeType="XML" type="rotate" from="0 20 20" to="360 20 20" dur="0.8s" repeatCount="indefinite"/>
              </circle>
            </svg>
            <span class="loading-text" id="loading-text">Bild wird geladen…</span>
            <span class="loading-hint" id="loading-hint"></span>
          </div>
          <div class="offline-overlay" id="offline-overlay">
            <svg viewBox="0 0 24 24">
              <path d="M1 1l22 22"/>
              <path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55"/>
              <path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39"/>
              <path d="M10.71 5.05A16 16 0 0 1 22.58 9"/>
              <path d="M1.42 9a15.91 15.91 0 0 1 4.7-2.88"/>
              <path d="M8.53 16.11a6 6 0 0 1 6.95 0"/>
              <line x1="12" y1="20" x2="12.01" y2="20"/>
            </svg>
            <div class="offline-title">Kamera Offline</div>
            <div class="offline-cam-name" id="offline-cam-name"></div>
            <div class="offline-subtitle" id="offline-subtitle">Keine Verbindung zur Bosch Cloud</div>
          </div>
          <div class="auth-overlay" id="auth-overlay">
            <svg viewBox="0 0 24 24">
              <path d="M12 2L3 7v6c0 5 3.5 9.4 9 11 5.5-1.6 9-6 9-11V7l-9-5z"/>
              <line x1="12" y1="9" x2="12" y2="13"/>
              <line x1="12" y1="17" x2="12.01" y2="17"/>
            </svg>
            <div class="auth-title">Anmeldung abgelaufen</div>
            <div class="auth-subtitle">Bosch Cloud Token ungültig — erneut anmelden um die Kamera wieder zu nutzen.</div>
            <a class="auth-btn" id="auth-reauth-btn" href="/config/integrations/integration/bosch_shc_camera" target="_top">Erneut anmelden</a>
          </div>
          <div class="privacy-placeholder" id="privacy-placeholder">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
              <path d="M7 11V7a5 5 0 0110 0v4"/>
            </svg>
            <span>Privat-Modus aktiv</span>
          </div>
          <svg class="motion-zones-overlay" id="motion-zones-overlay" viewBox="0 0 100 100" preserveAspectRatio="none"></svg>
          <svg class="privacy-mask-overlay" id="privacy-mask-overlay" viewBox="0 0 100 100" preserveAspectRatio="none"></svg>
          <div class="img-overlay">
            <span class="last-event-overlay" id="last-event-overlay"></span>
            <span class="events-overlay" id="events-overlay"></span>
          </div>

          <!-- Apple-style "letzte Bewegung" indicator — small glass pill
               in the bottom-right of the video that surfaces the camera's
               most recent motion/audio/person event timestamp when idle. -->
          <span class="ap-last-event" id="ap-last-event">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 6v6l4 2"/><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/></svg>
            <span id="ap-last-event-text"></span>
          </span>

          <!-- Apple-style overlays (v2.17.0) — rendered always, gated via CSS :host(.apple-style) -->
          <div class="ap-top">
            <div class="ap-title-pill ap-glass">
              <span class="ap-dot" id="ap-dot"></span>
              <span class="ap-title-text" id="ap-title-text">Bosch Camera</span>
            </div>
            <div class="ap-top-right">
              <span class="ap-badge hidden" id="ap-badge"></span>
            </div>
          </div>

          <div class="ap-pill-bar ap-glass">
            <button class="ap-pill-btn" id="ap-btn-snapshot" title="Snapshot" aria-label="Snapshot aufnehmen">
              <svg viewBox="0 0 24 24"><path d="M9 2 7.17 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2h-3.17L15 2H9zm3 15a5 5 0 1 1 0-10 5 5 0 0 1 0 10z"/></svg>
            </button>
            <button class="ap-pill-btn" id="ap-btn-stream" title="Live-Stream" aria-label="Live-Stream starten oder stoppen" aria-pressed="false">
              <svg viewBox="0 0 24 24" id="ap-stream-icon"><path d="M8 5v14l11-7L8 5z"/></svg>
            </button>
            <button class="ap-pill-btn" id="ap-btn-privacy" title="Privat-Modus" aria-label="Privat-Modus umschalten" aria-pressed="false">
              <svg viewBox="0 0 24 24"><path d="M12 1 4 5v6c0 5.5 3.8 10.7 8 12 4.2-1.3 8-6.5 8-12V5l-8-4z"/></svg>
            </button>
            <button class="ap-pill-btn" id="ap-btn-light" title="Licht" aria-label="Licht umschalten" aria-pressed="false">
              <svg viewBox="0 0 24 24"><path d="M9 21h6v-1H9v1zm3-19a7 7 0 0 0-4 12.74V17a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1v-2.26A7 7 0 0 0 12 2z"/></svg>
            </button>
            <button class="ap-pill-btn" id="ap-btn-fullscreen" title="Vollbild" aria-label="Vollbild">
              <svg viewBox="0 0 24 24"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>
            </button>
            <button class="ap-pill-btn" id="ap-btn-more" title="Mehr Optionen" aria-label="Mehr Optionen" aria-haspopup="true" aria-expanded="false">
              <svg viewBox="0 0 24 24"><circle cx="6" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="18" cy="12" r="2"/></svg>
            </button>
          </div>
        </div>

        <div class="info-row">
          <div class="info-item">
            <span class="info-label">Status</span>
            <span class="info-value" id="info-status">—</span>
          </div>
          <div class="info-item">
            <span class="info-label">Verbindung</span>
            <span class="info-value" id="info-connection">—</span>
          </div>
          <div class="info-item" style="text-align:right" title="Bosch-API Reaktionszeit (LOCAL=500 ms, REMOTE=1000 ms). Nicht der Player-Puffer — den stellt 'Puffer-Verhalten' in den Integrations-Einstellungen ein.">
            <span class="info-label">Reaktion</span>
            <span class="info-value" id="info-buffering">—</span>
          </div>
        </div>

        <div class="btn-row">
            <button class="btn btn-snapshot" id="btn-snapshot" aria-label="Snapshot aufnehmen">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">
                <path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/>
                <circle cx="12" cy="13" r="4"/>
              </svg>
              <span id="btn-snapshot-label">Snapshot</span>
            </button>
            <button class="btn btn-privacy-inline" id="btn-privacy-inline" title="Privat-Modus" aria-label="Privat-Modus umschalten" aria-pressed="false">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">
                <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
                <path d="M7 11V7a5 5 0 0110 0v4"/>
              </svg>
            </button>
            <button class="btn btn-stream" id="btn-stream" aria-label="Live-Stream starten oder stoppen" aria-pressed="false">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">
                <polygon points="23 7 16 12 23 17 23 7"/>
                <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
              </svg>
              <span id="btn-stream-label">Live Stream</span>
            </button>
            <button class="btn btn-overflow" id="btn-overflow" title="Weitere Optionen" aria-label="Weitere Optionen" aria-haspopup="true" aria-expanded="false">
              <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false">
                <circle cx="12" cy="5" r="2"/>
                <circle cx="12" cy="12" r="2"/>
                <circle cx="12" cy="19" r="2"/>
              </svg>
            </button>
            <button class="btn btn-fullscreen" id="btn-fullscreen" title="Vollbild" aria-label="Vollbild-Ansicht">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">
                <path d="M8 3H5a2 2 0 00-2 2v3m18 0V5a2 2 0 00-2-2h-3m0 18h3a2 2 0 002-2v-3M3 16v3a2 2 0 002 2h3"/>
              </svg>
            </button>
          </div>

          <!-- Theme (iOS/Android) + day/night Mode are config-only (YAML theme: / mode:);
               the in-card switcher buttons were removed 2026-05-30 (Thomas / issue #15).
               Defaults: theme=ios, mode=auto. -->

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
            <!-- Light sub-controls: toggles + expandable details -->
            <div class="light-sub-controls" id="light-sub-controls" style="display:none;padding:0 0 0 28px;border-left:2px solid rgba(255,204,0,.3);margin:0 0 0 16px">
              <div class="sw-row" id="btn-front-light" style="padding:3px 4px"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10"/></svg><span style="font-size:13px">Frontlicht</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>
              <div class="sw-row" id="btn-top-led" style="display:none;padding:3px 4px"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><path d="M12 2v8l6-4M12 2v8l-6-4"/></svg><span style="font-size:13px">Oberes Licht</span></div><div id="top-led-color-mini" style="width:14px;height:14px;border-radius:50%;border:1px solid #666;margin-right:4px"></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>
              <div class="sw-row" id="btn-bottom-led" style="display:none;padding:3px 4px"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><path d="M12 22v-8l6 4M12 22v-8l-6 4"/></svg><span style="font-size:13px">Unteres Licht</span></div><div id="bottom-led-color-mini" style="width:14px;height:14px;border-radius:50%;border:1px solid #666;margin-right:4px"></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>
              <div class="sw-row" id="btn-wallwasher" style="display:none;padding:3px 4px"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><path d="M9 18h6M10 22h4M12 2v1"/><path d="M18 12a6 6 0 10-12 0c0 2.21 1.34 4.1 3 5h6c1.66-.9 3-2.79 3-5z"/></svg><span style="font-size:13px">Oben + Unten</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>
              <div id="light-details-toggle" style="padding:4px;cursor:pointer;display:flex;align-items:center;gap:6px;color:#888;font-size:12px;user-select:none"><svg id="light-details-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width:12px;height:12px;transition:transform .2s"><polyline points="6 9 12 15 18 9"/></svg><span>Helligkeit & Farben</span></div>
              <div id="light-details-body" style="display:none">
                <div id="intensity-row" style="display:flex;align-items:center;gap:8px;padding:2px 4px;font-size:12px"><span style="white-space:nowrap;min-width:36px">Front</span><input type="range" id="intensity-slider" min="0" max="100" step="5" style="flex:1;accent-color:#fc0;height:4px"><span id="intensity-value" style="min-width:28px;text-align:right;color:#999">—</span></div>
                <div id="top-bri-row" style="display:none;align-items:center;gap:8px;padding:2px 4px;font-size:12px"><span style="white-space:nowrap;min-width:36px">Oben</span><input type="range" id="top-bri-slider" min="0" max="100" step="5" style="flex:1;accent-color:#4DFF7D;height:4px"><span id="top-bri-value" style="min-width:28px;text-align:right;color:#999">—</span></div>
                <div id="bottom-bri-row" style="display:none;align-items:center;gap:8px;padding:2px 4px;font-size:12px"><span style="white-space:nowrap;min-width:36px">Unten</span><input type="range" id="bottom-bri-slider" min="0" max="100" step="5" style="flex:1;accent-color:#FF453A;height:4px"><span id="bottom-bri-value" style="min-width:28px;text-align:right;color:#999">—</span></div>
                <div id="colortemp-row" style="display:none;align-items:center;gap:8px;padding:2px 4px;font-size:12px"><span style="white-space:nowrap;min-width:36px">Farbt.</span><input type="range" id="colortemp-slider" min="-100" max="100" step="5" style="flex:1;accent-color:#f90;height:4px;background:linear-gradient(to right,#69f,#fff,#f90)"><span id="colortemp-value" style="min-width:28px;text-align:right;color:#999">—</span></div>
              </div>
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
              <button class="pan-btn" id="pan-full-left"  title="Ganz links" aria-label="Kamera ganz nach links schwenken">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true" focusable="false">
                  <polyline points="11 18 5 12 11 6"/><polyline points="18 18 12 12 18 6"/>
                </svg>
              </button>
              <button class="pan-btn" id="pan-left"       title="Links" aria-label="Kamera nach links schwenken">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true" focusable="false">
                  <polyline points="15 18 9 12 15 6"/>
                </svg>
              </button>
              <button class="pan-btn" id="pan-center"     title="Mitte" aria-label="Kamera zentrieren">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false">
                  <circle cx="12" cy="12" r="3"/>
                  <line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/>
                  <line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/>
                </svg>
              </button>
              <button class="pan-btn" id="pan-right"      title="Rechts" aria-label="Kamera nach rechts schwenken">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true" focusable="false">
                  <polyline points="9 18 15 12 9 6"/>
                </svg>
              </button>
              <button class="pan-btn" id="pan-full-right" title="Ganz rechts" aria-label="Kamera ganz nach rechts schwenken">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true" focusable="false">
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

          <!-- Gen2 Accordion: Automatik & Sicherheit -->
          <div class="accordion" id="acc-gen2-auto" style="display:none">
            <div class="accordion-header" id="acc-gen2-auto-header">
              <span class="accordion-title">Automatik & Sicherheit</span>
              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
            </div>
            <div class="accordion-body">
              <div class="accordion-content">
                <div class="sw-row" id="btn-motion-light" style="padding:4px 0"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg><span>Licht bei Bewegung</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>
                <div class="sw-row" id="btn-ambient-light" style="padding:4px 0"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/></svg><span>Dauerlicht</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>
                <div class="sw-row" id="btn-intrusion" style="padding:4px 0"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg><span>Einbrucherkennung</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>
                <div id="motion-sens-row" style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px"><span style="white-space:nowrap">Empfindlichkeit</span><input type="range" id="motion-sens-slider" min="1" max="5" step="1" style="flex:1;accent-color:#ff9500;height:4px"><span id="motion-sens-value" style="min-width:16px;text-align:right;color:#999">—</span></div>
                <!-- Gen2 Indoor II — Alarm system (75 dB siren) -->
                <div class="sw-row" id="btn-alarm-arm" style="padding:4px 0;display:none"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg><span>Alarmanlage scharf</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>
                <div class="sw-row" id="btn-alarm-mode" style="padding:4px 0;display:none"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="13" r="7"/><path d="M12 9v4l2 2M5 3L2 6M19 3l3 3"/></svg><span>Sirene (75 dB)</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>
                <div class="sw-row" id="btn-prealarm" style="padding:4px 0;display:none"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2"/></svg><span>Pre-Alarm (rote LED)</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>
                <div id="power-led-row" style="display:none;align-items:center;gap:8px;padding:4px 0;font-size:13px"><span style="white-space:nowrap">Power-LED</span><input type="range" id="power-led-slider" min="0" max="100" step="5" style="flex:1;accent-color:#ff9500;height:4px"><span id="power-led-value" style="min-width:34px;text-align:right;color:#999">—</span></div>
              </div>
            </div>
          </div>

          <!-- Automations Accordion (alle Kameras, konfigurierbar) -->
          <div class="accordion" id="acc-automations" style="display:none">
            <div class="accordion-header" id="acc-automations-header">
              <span class="accordion-title">Automationen</span>
              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
            </div>
            <div class="accordion-body">
              <div class="accordion-content">
                <div id="automations-container"></div>
              </div>
            </div>
          </div>

          <!-- Gen2 Accordion: Licht & Kamera -->
          <div class="accordion" id="acc-gen2-light" style="display:none">
            <div class="accordion-header" id="acc-gen2-light-header">
              <span class="accordion-title">Licht & Kamera</span>
              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
            </div>
            <div class="accordion-body">
              <div class="accordion-content">
                <div id="colortemp-row" style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px"><span style="white-space:nowrap">Farbtemperatur</span><input type="range" id="colortemp-slider" min="-100" max="100" step="5" style="flex:1;accent-color:#f90;height:4px;background:linear-gradient(to right,#69f,#fff,#f90)"><span id="colortemp-value" style="min-width:32px;text-align:right;color:#999">—</span></div>
                <div id="rgb-lights-row" style="padding:4px 0;font-size:13px">
                  <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px"><span style="flex:1">Farbe Oben</span><div id="top-led-color" style="width:24px;height:24px;border-radius:50%;border:2px solid #444;cursor:pointer" title="Farbe wählen"></div><input type="color" id="top-led-picker" style="display:none"></div>
                  <div style="display:flex;align-items:center;gap:10px"><span style="flex:1">Farbe Unten</span><div id="bottom-led-color" style="width:24px;height:24px;border-radius:50%;border:2px solid #444;cursor:pointer" title="Farbe wählen"></div><input type="color" id="bottom-led-picker" style="display:none"></div>
                </div>
                <div class="sw-row" id="btn-status-led" style="padding:4px 0"><div class="sw-left"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/></svg><span>Status-LED</span></div><button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button></div>
                <div id="mic-level-row" style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:18px;height:18px;flex-shrink:0"><path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/><path d="M19 10v2a7 7 0 01-14 0v-2"/></svg><span style="white-space:nowrap">Mikrofon</span><input type="range" id="mic-slider" min="0" max="100" step="5" style="flex:1;accent-color:#0a84ff;height:4px"><span id="mic-value" style="min-width:28px;text-align:right;color:#999">—</span></div>
                <div id="lens-elev-row" style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:18px;height:18px;flex-shrink:0"><path d="M12 22V2M5 12l7-10 7 10"/></svg><span style="white-space:nowrap">Höhe</span><input type="range" id="lens-slider" min="50" max="500" step="5" style="flex:1;accent-color:#30d158;height:4px"><span id="lens-value" style="min-width:36px;text-align:right;color:#999">—</span></div>
              </div>
            </div>
          </div>

          <!-- Accordion: Diagnostics & Services -->
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
              </div>
            </div>
          </div>

          <!-- Accordion: Schedules & Zones -->
          <div class="accordion" id="acc-schedules">
            <div class="accordion-header" id="acc-schedules-header">
              <span class="accordion-title">Zeitpläne & Zonen</span>
              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
            </div>
            <div class="accordion-body">
              <div class="accordion-content">
                <div class="diag-row">
                  <span class="diag-label">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
                    Zeitpläne
                  </span>
                  <span class="diag-value" id="diag-rules-count">—</span>
                </div>
                <div id="rules-list" style="padding:0 4px"></div>
                <div class="sw-row" id="btn-show-zones">
                  <div class="sw-left">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18"/></svg>
                    <span>Motion-Zonen anzeigen</span>
                  </div>
                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
                </div>
                <div class="sw-row" id="btn-show-masks">
                  <div class="sw-left">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
                    <span>Privacy-Masken anzeigen</span>
                  </div>
                  <button class="sw-toggle" tabindex="-1"><div class="sw-thumb"></div></button>
                </div>
                <div class="diag-row">
                  <span class="diag-label">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18"/></svg>
                    Motion-Zonen
                  </span>
                  <span class="diag-value" id="diag-zones-count">—</span>
                </div>
                <div class="diag-row">
                  <span class="diag-label">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>
                    Privacy-Masken
                  </span>
                  <span class="diag-value" id="diag-masks-count">—</span>
                </div>
              </div>
            </div>
          </div>

          <!-- Accordion: Services -->
          <div class="accordion" id="acc-services">
            <div class="accordion-header" id="acc-services-header">
              <span class="accordion-title">Services</span>
              <svg class="accordion-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
            </div>
            <div class="accordion-body">
              <div class="accordion-content">
                <div class="svc-grid" id="svc-grid"></div>
                <div id="svc-result" style="font-size:11px;color:#999;padding:4px 0;display:none"></div>
              </div>
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
    // Auto-play gate (tap-to-reveal) — visible only when auto_play_default
    // resolves to overlay-required (mode=never, or mode=lan + remote).
    const apg = this.shadowRoot.getElementById("auto-play-gate");
    // pointerup (not click): the gate is a plain <div>, and in the HA Companion
    // App's mobile WebView (iOS WKWebView / Android WebView) a synthesized
    // `click` on a non-button div is unreliable — taps didn't reveal the
    // stream. pointerup fires for mouse, touch and pen alike. 2026-05-29.
    if (apg) apg.addEventListener("pointerup", () => this._onPlayGateTap());
    this.shadowRoot.getElementById("btn-fullscreen").addEventListener("click", () =>
      this._requestFullscreen()
    );
    // Overflow ⋮ toggles the `.overflow-open` class on the host. CSS does the
    // rest — no separate popup element, just progressive disclosure of the
    // already-rendered control rows/accordions.
    this.shadowRoot.getElementById("btn-overflow").addEventListener("click", () => {
      this.classList.toggle("overflow-open");
    });
    this.shadowRoot.getElementById("btn-privacy-inline").addEventListener("click", () =>
      this._toggleSwitchWithRollback(this._entities.privacy)
    );

    // Apple-style pill-bar wiring — each maps to the same callback as the
    // legacy button, so feature behaviour is identical regardless of layout.
    const apBindClick = (id, fn) => {
      const el = this.shadowRoot.getElementById(id);
      if (el) el.addEventListener("click", fn);
    };
    apBindClick("ap-btn-snapshot",   () => this._onSnapshotClick());
    apBindClick("ap-btn-stream",     () => this._toggleStream());
    apBindClick("ap-btn-privacy",    () => this._toggleSwitchWithRollback(this._entities.privacy));
    apBindClick("ap-btn-light",      () => this._toggleSwitchWithRollback(this._entities.light));
    apBindClick("ap-btn-fullscreen", () => this._requestFullscreen());
    apBindClick("ap-btn-more",       () => {
      this.classList.toggle("overflow-open");
      this._syncMoreButton();
      // Sync the switcher's selected chip whenever the menu opens — handles
      // the case where another bosch card on the page changed the theme.
      this._refreshThemeSwitcher();
    });
    // Reflect the initial overflow state on the ⋮ button — non-minimal cards
    // start expanded (issue 2026-05-29 "minimal meaningful"), so the button
    // must render pressed (.on) from the first paint, not only after a click.
    this._syncMoreButton();

    // Theme switcher buttons (Auto / iOS / Android) — bind once after render.
    const themeSwitcher = this.shadowRoot.getElementById("ap-theme-switcher");
    if (themeSwitcher) {
      themeSwitcher.querySelectorAll("[data-theme]").forEach((b) => {
        b.addEventListener("click", () => {
          const t = b.getAttribute("data-theme");
          this._setUserTheme(t);
          // _setUserTheme broadcasts; our own listener applies + refreshes.
          // Apply locally too in case dispatchEvent is throttled by the
          // browser (some Android WebViews coalesce same-frame events).
          this._applyTheme(this._resolveTheme());
        });
      });
      this._refreshThemeSwitcher();
    }

    // Day/Night mode switcher (Auto / Tag / Nacht) — same pattern.
    const modeSwitcher = this.shadowRoot.getElementById("ap-mode-switcher");
    if (modeSwitcher) {
      modeSwitcher.querySelectorAll("[data-mode]").forEach((b) => {
        b.addEventListener("click", () => {
          const m = b.getAttribute("data-mode");
          this._setUserMode(m);
          this._applyMode(this._resolveMode());
        });
      });
      this._refreshModeSwitcher();
    }

    // Toggle buttons
    this.shadowRoot.getElementById("btn-audio").addEventListener("click", () =>
      this._toggleAudio()
    );
    this.shadowRoot.getElementById("btn-light").addEventListener("click", () =>
      this._toggleSwitchWithRollback(this._entities.light)
    );
    this.shadowRoot.getElementById("btn-privacy").addEventListener("click", () =>
      this._toggleSwitchWithRollback(this._entities.privacy)
    );
    this.shadowRoot.getElementById("btn-notifications").addEventListener("click", () =>
      this._toggleSwitch(this._entities.notifications)
    );
    this.shadowRoot.getElementById("btn-intercom")?.addEventListener("click", () =>
      this._toggleSwitch(this._entities.intercom)
    );

    // Light sub-controls
    this.shadowRoot.getElementById("btn-front-light")?.addEventListener("click", () =>
      this._toggleSwitch(this._entities.frontLight)
    );
    this.shadowRoot.getElementById("btn-wallwasher")?.addEventListener("click", () =>
      this._toggleSwitch(this._entities.wallwasher)
    );
    // Light details toggle (Helligkeit & Farben expandable)
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

    // Gen2: Top/Bottom brightness sliders.
    // Route through light.turn_on so that changes while the light is OFF are
    // preconfigured locally by the integration (don't physically turn the
    // light on). When the light is ON the same call updates brightness live.
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
            brightness: Math.max(1, Math.round(pct * 255 / 100)),
          }).catch(e => console.warn("bosch-camera-card: top-bri", e));
        } else if (this._entities.topBrightness) {
          this._hass.callService("number", "set_value", {
            entity_id: this._entities.topBrightness, value: pct,
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
            brightness: Math.max(1, Math.round(pct * 255 / 100)),
          }).catch(e => console.warn("bosch-camera-card: bot-bri", e));
        } else if (this._entities.bottomBrightness) {
          this._hass.callService("number", "set_value", {
            entity_id: this._entities.bottomBrightness, value: pct,
          }).catch(e => console.warn("bosch-camera-card: bot-bri", e));
        }
      });
    }
    // Gen2: separate top/bottom LED toggles via light.turn_on/turn_off
    this.shadowRoot.getElementById("btn-top-led")?.querySelector(".sw-toggle")?.addEventListener("click", () => {
      if (!this._hass || !this._entities.topLedLight) return;
      const st = this._hass.states[this._entities.topLedLight]?.state;
      this._callService("light", st === "on" ? "turn_off" : "turn_on", {entity_id: this._entities.topLedLight});
    });
    this.shadowRoot.getElementById("btn-bottom-led")?.querySelector(".sw-toggle")?.addEventListener("click", () => {
      if (!this._hass || !this._entities.bottomLedLight) return;
      const st = this._hass.states[this._entities.bottomLedLight]?.state;
      this._callService("light", st === "on" ? "turn_off" : "turn_on", {entity_id: this._entities.bottomLedLight});
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
            value: parseInt(intensitySlider.value),
          }).catch(err => console.warn("bosch-camera-card: intensity", err));
        }, 200);
      });
    }

    // Gen2: Status LED toggle
    const statusLedBtn = this.shadowRoot.getElementById("btn-status-led");
    if (statusLedBtn) statusLedBtn.querySelector(".sw-toggle")?.addEventListener("click", () =>
      this._toggleSwitch(this._entities.statusLed)
    );

    // Gen2: Intrusion Detection toggle
    const intrusionBtn = this.shadowRoot.getElementById("btn-intrusion");
    if (intrusionBtn) intrusionBtn.querySelector(".sw-toggle")?.addEventListener("click", () =>
      this._toggleSwitch(this._entities.intrusionDetection)
    );

    // Gen2 Indoor II: Alarm system controls
    const alarmArmBtn = this.shadowRoot.getElementById("btn-alarm-arm");
    if (alarmArmBtn) alarmArmBtn.querySelector(".sw-toggle")?.addEventListener("click", () =>
      this._toggleSwitch(this._entities.alarmSystemArm)
    );
    const alarmModeBtn = this.shadowRoot.getElementById("btn-alarm-mode");
    if (alarmModeBtn) alarmModeBtn.querySelector(".sw-toggle")?.addEventListener("click", () =>
      this._toggleSwitch(this._entities.alarmMode)
    );
    const preAlarmBtn = this.shadowRoot.getElementById("btn-prealarm");
    if (preAlarmBtn) preAlarmBtn.querySelector(".sw-toggle")?.addEventListener("click", () =>
      this._toggleSwitch(this._entities.preAlarm)
    );
    // Gen2 Indoor II: Power-LED brightness slider
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
            value: parseInt(powerLedSlider.value),
          }).catch(err => console.warn("bosch-camera-card: power-led", err));
        }, 200);
      });
    }

    // Automation toggles — dynamically generated from config.automations array
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
          this._callService("automation", st === "on" ? "turn_off" : "turn_on", {entity_id: eid});
        });
        autoContainer.appendChild(row);
      });
    }

    // Gen2: Motion Light Sensitivity slider
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
          value: parseInt(motSensSlider.value),
        }).catch(err => console.warn("bosch-camera-card: motion-sensitivity", err));
      });
    }

    // Gen2: Motion Light toggle
    const motionLightBtn = this.shadowRoot.getElementById("btn-motion-light");
    if (motionLightBtn) motionLightBtn.querySelector(".sw-toggle")?.addEventListener("click", () =>
      this._toggleSwitch(this._entities.motionLight)
    );

    // Gen2: Ambient Light toggle
    const ambientLightBtn = this.shadowRoot.getElementById("btn-ambient-light");
    if (ambientLightBtn) ambientLightBtn.querySelector(".sw-toggle")?.addEventListener("click", () =>
      this._toggleSwitch(this._entities.ambientLight)
    );

    // Gen2: RGB color pickers for top/bottom LEDs
    const topColorCircle = this.shadowRoot.getElementById("top-led-color");
    const topPicker = this.shadowRoot.getElementById("top-led-picker");
    if (topColorCircle && topPicker) {
      topColorCircle.addEventListener("click", () => topPicker.click());
      topPicker.addEventListener("change", () => {
        if (!this._hass || !this._entities.topLedLight) return;
        const hex = topPicker.value;
        const r = parseInt(hex.slice(1,3), 16), g = parseInt(hex.slice(3,5), 16), b = parseInt(hex.slice(5,7), 16);
        this._hass.callService("light", "turn_on", {
          entity_id: this._entities.topLedLight, rgb_color: [r, g, b], brightness: 200
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
        const r = parseInt(hex.slice(1,3), 16), g = parseInt(hex.slice(3,5), 16), b = parseInt(hex.slice(5,7), 16);
        this._hass.callService("light", "turn_on", {
          entity_id: this._entities.bottomLedLight, rgb_color: [r, g, b], brightness: 200
        }).catch(e => console.warn("bosch-camera-card: bottom-led-color", e));
        botColorCircle.style.background = hex;
      });
    }

    // Gen2: Color temperature slider
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
            value: parseFloat((parseInt(ctSlider.value) / 100).toFixed(2)),
          }).catch(err => console.warn("bosch-camera-card: colortemp", err));
        }, 200);
      });
    }

    // Gen2: Microphone level slider
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
            value: parseInt(micSlider.value),
          }).catch(err => console.warn("bosch-camera-card: mic-level", err));
        }, 200);
      });
    }

    // Gen2: Lens elevation slider
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
            value: parseFloat((parseInt(lensSlider.value) / 100).toFixed(2)),
          }).catch(err => console.warn("bosch-camera-card: lens-elevation", err));
        }, 200);
      });
    }

    // Pan buttons
    const PAN_STEP = 30;
    const setPan = (pos) => {
      if (!this._hass || !this._entities.pan) return;
      this._hass.callService("number", "set_value", {
        entity_id: this._entities.pan,
        value: Math.max(-120, Math.min(120, pos)),
      }).then(() => {
        // Trigger backend image refresh so _cached_image is warm before card requests it
        if (this._hass?.services?.bosch_shc_camera?.trigger_snapshot)
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
    ["acc-notif-types", "acc-advanced", "acc-diagnostics", "acc-schedules", "acc-services", "acc-gen2-auto", "acc-gen2-light", "acc-automations"].forEach(id => {
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
    // Service buttons grid
    this._renderServiceButtons();

    this.shadowRoot.getElementById("btn-show-zones")?.addEventListener("click", () => {
      this._showMotionZones = !this._showMotionZones;
      const btn = this.shadowRoot.getElementById("btn-show-zones");
      if (btn) btn.classList.toggle("on", this._showMotionZones);
      // Force motion zones re-render
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

    // (Debug line removed 2026-05-30 — no longer rendered.)
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
    // Suppress flicker during stream startup: if a connecting/waiting state is
    // active, snapshot-load callbacks must not hide the overlay (the spinner
    // would reappear a moment later when stream-startup advances to its next
    // step), and external paths must not overwrite the progressive timeline
    // text from _toggleStream — let that timeline own the messaging.
    const streamStarting = this._streamConnecting || this._waitingForStream || this._startingLiveVideo;
    if (!visible && streamStarting) return;
    if (visible && streamStarting && this._streamConnecting && text === "Bild wird geladen…") return;
    const overlay  = this.shadowRoot.getElementById("loading-overlay");
    const loadText = this.shadowRoot.getElementById("loading-text");
    const hintEl   = this.shadowRoot.getElementById("loading-hint");
    const img      = this.shadowRoot.getElementById("cam-img");
    this._loadingOverlay = visible;
    if (overlay) {
      overlay.classList.toggle("visible", visible);
      // Use transparent overlay when we already have an image — old image stays visible underneath
      overlay.classList.toggle("refreshing", visible && this._imageLoaded);
    }
    if (loadText) loadText.textContent = text;
    // Secondary hint: during stream start, reassure user the current wait is normal
    // by showing expected total time based on connection type (read live from HA state).
    if (hintEl) {
      if (visible && (this._streamConnecting || this._startingLiveVideo || this._waitingForStream)) {
        const ct = this._hass?.states?.[this._entities?.switch]?.attributes?.connection_type;
        if (ct === "REMOTE")       hintEl.textContent = "Cloud-Stream — ca. 30–45 s bis erstes Bild, danach stabil";
        else if (ct === "LOCAL")   hintEl.textContent = "LAN-Stream — ca. 25–35 s bis erstes Bild";
        else                        hintEl.textContent = "Verbindung zur Kamera wird aufgebaut…";
      } else {
        hintEl.textContent = "";
      }
    }
    // Only hide image on first load when there's nothing to show yet
    if (img) img.classList.toggle("hidden", visible && !this._imageLoaded);

    if (visible) {
      // Safety timeout — shorter for snapshot refreshes, longer during stream start.
      // During stream start (_startingLiveVideo or _waitingForStream), the overlay
      // should stay visible until the video actually plays (outdoor cam takes 80s+).
      if (this._loadingTimeout) clearTimeout(this._loadingTimeout);
      const isStreamStart = this._startingLiveVideo || this._waitingForStream || this._liveVideoActive;
      const safetyMs = isStreamStart ? 120000 : 15000;
      this._loadingTimeout = setTimeout(() => this._setLoadingOverlay(false), safetyMs);
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
      // Pinned to an exact version so the SRI hash below stays valid.
      // The previous floating "@1" range broke whenever jsdelivr shipped a
      // new hls.js@1.x.y — the body hash drifted and the browser blocked the
      // script, collapsing the card with "hls.js load failed". Bump both the
      // version and the integrity together when updating.
      s.src = "https://cdn.jsdelivr.net/npm/hls.js@1.6.16/dist/hls.min.js";
      s.integrity = "sha384-5E8B0pTlZZJMabWpC0fyYf6OUpe15jJij34BqBAh4NXoHAlLNOjCPRrwtOXOQFAn";
      s.crossOrigin = "anonymous";
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
      // Keep snapshot image visible until video actually plays — avoids
      // black screen gap between image hide and first video frame.
      this._liveVideoActive    = true;
      this._startingLiveVideo  = false;
      // Show HLS-fallback banner when streaming starts on a remote-skip path
      // (Companion+external or mobile-browser+external — see _remoteSkipWebRTC).
      if (this._remoteSkipWebRTC) {
        const banner = this.shadowRoot?.getElementById("ios-hls-banner");
        if (banner) banner.classList.add("visible");
      }
      const clearOverlay = () => {
        // NOW hide the snapshot — video is playing, no black gap
        if (img) img.style.display = "none";
        this._setLoadingOverlay(false);
        if (this._streamConnecting) {
          this._streamConnecting = false;
          if (this._connectSteps) { this._connectSteps.forEach(t => clearTimeout(t)); this._connectSteps = null; }
        }
        // Flip the badge to Live now — the first frame is on screen. Don't wait
        // for the next hass push (stream_status sensor can lag 10s+). 2026-05-30.
        this._markLiveBadge();
        video.removeEventListener("playing", clearOverlay);
      };
      video.addEventListener("playing", clearOverlay);
      // Safety timeout: if video never plays after 120s, hide overlay but
      // keep snapshot visible (don't call clearOverlay which hides the image).
      // Outdoor camera can take 80s+ for first HLS frame.
      if (this._activateSafetyTimer) clearTimeout(this._activateSafetyTimer);
      this._activateSafetyTimer = setTimeout(() => {
        if (!video.paused && video.currentTime > 0) {
          // Video is actually playing — full cleanup
          clearOverlay();
        } else {
          // Video still not playing — hide overlay spinner only,
          // keep snapshot image visible underneath
          this._setLoadingOverlay(false);
        }
      }, 120000);

      // Stall detector: if video.currentTime stops advancing for 15s, recover
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
          if (stallCount >= 3) { // 15s stall (3 × 5s)
            console.warn("bosch-camera-card: video stalled for 15s, recovering");
            stallCount = 0;
            if (this._hls && this._hls.liveSyncPosition) {
              video.currentTime = this._hls.liveSyncPosition;
            } else {
              // Full restart
              this._stopLiveVideo();
              if (this._isStreaming && this._isStreaming()) {
                setTimeout(() => this._startLiveVideo(), 2000);
              }
            }
          }
        } else {
          stallCount = 0;
        }
        lastTime = video.currentTime;
      }, 5000);
    };

    // ── WebRTC (always attempt; HLS is the fallback) ──────────────────
    // go2rtc provides WebRTC (~2s latency vs ~12s HLS). Don't gate on the
    // `camera/capabilities` query: HA's `cam._webrtc_provider` is set by
    // `async_refresh_providers` which races with stream-state-flip and
    // typically takes ~4s after switch goes ON to reflect WEB_RTC in the
    // capability list. The card's caps query at stream-start often hits
    // that race window and sees only `["hls"]` — which would lock us into
    // HLS for the whole session. Instead, just send the offer; if HA hasn't
    // wired the provider yet (or the cam genuinely doesn't support WebRTC),
    // the offer rejects fast (`webrtc_offer_failed: Camera does not support
    // WebRTC` from `require_webrtc_support` decorator), the catch block
    // takes over within ~100 ms, and HLS startup is unaffected.
    //
    // Remote-no-WebRTC exception: WebRTC media needs UDP, which neither
    // Cloudflare-Tunnel/Nabu-Casa (Companion App) nor cellular carrier-grade
    // NAT (mobile browser) can carry reliably — ICE times out after ~5 s,
    // wasting startup time and triggering a visible "stream failed" toast
    // before HLS would have taken over. When the client is the HA Companion
    // App OR a mobile browser (iOS/Android) AND reaches us through an
    // external host (not RFC1918/.local), skip WebRTC entirely and go
    // straight to HLS. Browser-on-LAN and desktop-browser-external still
    // try WebRTC.
    const _skipWebRTC = this._remoteSkipWebRTC;
    if (_skipWebRTC) {
      console.debug("bosch-camera-card: remote endpoint + Companion/mobile-browser — skipping WebRTC, using HLS");
    }
    if (!_skipWebRTC) try {
      try {
        await this._startWebRTC(video, activateVideo);
        return; // WebRTC up
      } catch (webrtcErr) {
        // The most common WebRTC rejection is "Camera does not support
        // WebRTC, frontend_stream_types={HLS}" which happens during the
        // ~3 s race window between stream-feature-flip and HA's auto-fired
        // async_refresh_providers wiring up the WebRTC provider. Log this
        // expected case at debug level only — HLS fallback handles it
        // transparently and the card's stream-retry loop tries WebRTC again
        // a few seconds later when caps have propagated. Real WebRTC
        // failures (timeout, ICE fail, transport error) still surface as
        // warnings so they're visible during diagnosis.
        const m = String(webrtcErr?.message || webrtcErr);
        const expectedRace = m.includes("does not support WebRTC")
                          || m.includes("frontend_stream_types");
        if (expectedRace) {
          console.debug("bosch-camera-card: WebRTC race miss, falling back to HLS:", m);
        } else {
          console.warn("bosch-camera-card: WebRTC failed, falling back to HLS:", m);
        }
        if (this._webrtcPc) { try { this._webrtcPc.close(); } catch {}; this._webrtcPc = null; }
        if (this._webrtcUnsub) { try { this._webrtcUnsub(); } catch {}; this._webrtcUnsub = null; }
      }
    } catch (outer) { /* paranoia */ }

    // ── HLS via camera/stream (fallback) ────────────────────────────────
    try {
      const result = await this._hass.callWS({
        type:      "camera/stream",
        entity_id: this._entities.camera,
      });
      if (!result?.url) throw new Error("no url");

      // Always start muted to comply with Chrome autoplay policy.
      // Chrome blocks unmuted autoplay without prior user interaction.
      // Audio is controlled by the user via the audio toggle in the card.
      video.muted = true;
      const startPlay = () => {
        video.muted = true;
        video.play()
          .then(() => {
            // Video is playing muted. User can unmute via audio toggle.
            // Do NOT auto-unmute — Chrome will pause the video.
          })
          .catch((err) => {
            if (err.name === "NotAllowedError") {
              // Android System WebView blocks autoplay when "Autoplay videos"
              // is disabled in HA app settings (mediaPlaybackRequiresUserGesture).
              // One tap satisfies the user-gesture requirement — show overlay.
              const overlay = this.shadowRoot?.getElementById("tap-to-play-overlay");
              if (overlay) {
                overlay.classList.add("visible");
                const resume = () => {
                  overlay.classList.remove("visible");
                  overlay.removeEventListener("pointerup", resume);
                  video.muted = true;
                  video.play().catch(() => {});
                };
                // pointerup, not click — reliable on mobile-WebView touch (the
                // tap-to-play overlay is a <div>; click can be dropped). 2026-05-29.
                overlay.addEventListener("pointerup", resume);
              }
              return;
            }
            // Any other error: retry after a short delay
            console.warn("bosch-camera-card: muted play failed:", err.message);
            setTimeout(() => {
              video.muted = true;
              video.play().catch(() => {});
            }, 2000);
          });
      };

      // hls.js CDN-Load kann in der iOS Companion App (WKWebView) blockieren
      // (strikte ATS/CSP). Ohne diesen guard wirft `await` durch in den catch
      // und der native-HLS-Fallback unten wird nie erreicht — Spinner hängt
      // dauerhaft. Daher: Load-Fehler abfangen und auf native HLS durchfallen.
      let Hls = null;
      try {
        Hls = await this._loadHlsJs();
      } catch (e) {
        console.warn("bosch-camera-card: hls.js load failed, will try native HLS:", e?.message);
      }
      if (Hls && Hls.isSupported()) {
        if (this._hls) { this._hls.destroy(); this._hls = null; }
        // Apply the buffer profile selected via integration options.
        // CRITICAL: maxBufferLength MUST stay < HA's OUTPUT_IDLE_TIMEOUT (30s).
        // If hls.js buffers ≥30s, it stops requesting segments → HA thinks
        // nobody is watching → kills FFmpeg → video freezes on last frame.
        const camAttrsForBuf = this._hass?.states?.[this._entities.camera]?.attributes || {};
        const bufModeKey = camAttrsForBuf.live_buffer_mode || "balanced";
        const bufProfile = BOSCH_BUFFER_PROFILES[bufModeKey] || BOSCH_BUFFER_PROFILES.balanced;
        console.debug("bosch-camera-card: HLS buffer profile", bufModeKey, bufProfile);
        const hls = new Hls({
          enableWorker: true,
          ...bufProfile,
          // Aggressive recovery: reload manifest on stall
          manifestLoadingMaxRetry: 10,
          levelLoadingMaxRetry: 10,
          fragLoadingMaxRetry: 10,
        });
        this._hls = hls;
        hls.on(Hls.Events.MANIFEST_PARSED, startPlay);
        // Reset stall counter on successful fragment delivery
        this._stallCount = 0;
        hls.on(Hls.Events.FRAG_LOADED, () => { this._stallCount = 0; });
        // One-shot seek to the live edge once the first fragment is buffered.
        // After a backend stream-worker restart (e.g. an integration reload),
        // the HLS playlist can carry a multi-segment backlog and hls.js starts
        // playback at the buffer start — leaving the user 20-40 s behind real
        // time (the >30 s glitch reported 2026-05-29 on the HLS fallback).
        // Seek forward ONCE, and only when we're meaningfully behind (>6 s), so
        // normal low-latency starts are untouched and we never seek into an
        // unbuffered gap (which would itself stall).
        let _didLiveSeek = false;
        hls.on(Hls.Events.FRAG_BUFFERED, () => {
          if (_didLiveSeek) return;
          const lsp = hls.liveSyncPosition;
          if (lsp != null && video && (lsp - video.currentTime) > 6) {
            _didLiveSeek = true;
            console.debug("bosch-camera-card: seeking HLS to live edge", lsp, "from", video.currentTime);
            video.currentTime = lsp;
          }
        });

        // Auto-recovery on buffer stall: seek to live edge, then reconnect
        hls.on(Hls.Events.ERROR, (_ev, data) => {
          if (data.details === "bufferStalledError") {
            this._stallCount = (this._stallCount || 0) + 1;
            if (video && hls.liveSyncPosition) {
              video.currentTime = hls.liveSyncPosition;
            }
            // After 3 consecutive stalls, FFmpeg is likely dead — full reconnect
            if (this._stallCount >= 3) {
              console.warn("bosch-camera-card: 3 buffer stalls, reconnecting HLS");
              this._stallCount = 0;
              this._stopLiveVideo();
              if (this._isStreaming && this._isStreaming()) {
                setTimeout(() => this._reconnectAfterStreamDrop(), 1000);
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
              setTimeout(() => this._reconnectAfterStreamDrop(), 2000);
            }
          }
        });
        hls.loadSource(result.url);
        hls.attachMedia(video);
        // HLS keepalive: prevent HA's 30s idle timeout from killing FFmpeg.
        // Even with maxBufferLength=10, belt-and-suspenders measure.
        if (this._hlsKeepaliveTimer) clearInterval(this._hlsKeepaliveTimer);
        this._hlsKeepaliveTimer = setInterval(() => {
          if (this._hls && this._liveVideoActive) {
            this._hls.startLoad(-1); // restart loading from current position
          }
        }, 20000); // every 20s, well within 30s timeout
      } else if (video.canPlayType("application/vnd.apple.mpegurl") !== "") {
        video.src = result.url;
        startPlay();
      } else {
        throw new Error("HLS not supported");
      }
      activateVideo();

    } catch (e) {
      if (attempt < 5) {
        // Re-check camera state before retrying: if the backend connection died,
        // the camera entity goes to "idle" (supported_features loses STREAM) and
        // calling camera/stream WS immediately produces "does not support play
        // stream service" errors. Use _waitForStreamReady() instead so we wait
        // until the backend re-establishes the connection.
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
        // After 5 attempts, back off but DON'T give up permanently.
        // First 2 extra retries: 5s (catches go2rtc registration window ~5-8s after switch on).
        // Further retries: 10s to avoid hammering HA.
        const retryDelay = attempt <= 6 ? 5000 : 10000;
        console.warn(`bosch-camera-card: stream not available (attempt ${attempt}), retrying in ${retryDelay/1000}s`, e);
        this._liveVideoActive   = false;
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
    /**
     * Start WebRTC stream via go2rtc (HA's camera/webrtc/offer WS API).
     * Provides ~2s latency vs ~12s for HLS.
     *
     * STUN/TURN: without ICE servers, RTCPeerConnection only collects host
     * candidates (LAN IPs). On the same subnet that's fine, but a client on
     * cellular reaching HA via Cloudflare Tunnel cannot route to host
     * candidates like 192.168.x.x — ICE stays in `checking` forever, no
     * track is delivered, and the card hangs until the 5s timeout below
     * fires. We pull HA's configured ICE servers via the same WS API HA's
     * own ha-web-rtc-player uses (`camera/webrtc/get_client_config`) so
     * cellular clients have a public relay path. Failure is non-fatal —
     * fall back to a default STUN so LAN clients still work.
     */
    const entityId = this._entities.camera;
    let rtcConfig = { iceServers: [{ urls: "stun:stun.home-assistant.io:80" }] };
    try {
      const settings = await this._hass.callWS({
        type: "camera/webrtc/get_client_config",
        entity_id: entityId,
      });
      if (settings?.configuration) rtcConfig = settings.configuration;
    } catch (e) {
      console.debug("bosch-camera-card: get_client_config unavailable, using default STUN:", e?.message);
    }
    const pc = new RTCPeerConnection(rtcConfig);
    this._webrtcPc = pc;

    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });

    const remoteStream = new MediaStream();
    pc.ontrack = (ev) => {
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

    // Outer-scope reject + timeout so the subscribeMessage error handler
    // can short-circuit the Promise below. Set by the Promise constructor.
    let webrtcReject = null;
    let webrtcTimeout = null;

    // Subscribe to answer/candidates via HA WS
    const unsub = await this._hass.connection.subscribeMessage(
      (event) => {
        // Guard the reload/teardown race: _stopLiveVideo() (called from
        // disconnectedCallback / pagehide / stream-stop) may close `pc` while
        // answer/candidate messages are still in flight on the HA WS. Applying
        // them to a closed connection throws InvalidStateError — this flooded
        // the console (~12 uncaught rejections) during the 2026-05-29
        // privacy-toggle reload. Drop late messages, and `.catch()` the
        // micro-race where signalingState flips between this check and the call.
        if (!pc || pc.signalingState === "closed" || pc.signalingState === "failed") return;
        if (event.type === "answer") {
          pc.setRemoteDescription({ type: "answer", sdp: event.answer })
            .catch((e) => console.debug("bosch-camera-card: setRemoteDescription skipped (pc closing):", e?.message));
        } else if (event.type === "candidate") {
          pc.addIceCandidate(event.candidate)
            .catch((e) => console.debug("bosch-camera-card: addIceCandidate skipped (pc closing):", e?.message));
        } else if (event.type === "error") {
          // 2026-05-25 fix: previously this branch only logged and let the
          // 5s setTimeout fall through — visible to the user as a 5-second
          // delay before "HLS wird geladen…" appears. Bosch cameras report
          // "Camera does not support WebRTC" on first offer when HA's
          // frontend_stream_types race hasn't propagated yet; the existing
          // _check_and_recover_webrtc watchdog repairs it for the next try.
          // Fast-fail here so HLS kicks in immediately (~100 ms vs 5 s).
          const msg = event.message || "webrtc_offer_error";
          const isRace = typeof msg === "string" && (
            msg.includes("does not support WebRTC") ||
            msg.includes("frontend_stream_types")
          );
          if (isRace) {
            console.debug("bosch-camera-card: WebRTC offer rejected (HA stream-type race), fast-falling to HLS:", msg);
          } else {
            console.warn("bosch-camera-card: WebRTC error:", msg);
          }
          if (webrtcTimeout) clearTimeout(webrtcTimeout);
          if (webrtcReject) webrtcReject(new Error(typeof msg === "string" ? msg : "webrtc_offer_error"));
        }
      },
      { type: "camera/webrtc/offer", entity_id: entityId, offer: offer.sdp }
    );
    this._webrtcUnsub = unsub;

    // Wait for first track with timeout. Also surface ICE failure
    // immediately — when ICE never finds a working pair (most common cause:
    // cellular client + LAN-only host candidates from go2rtc) the underlying
    // pc fires `iceconnectionstatechange` with `failed` long before 5s, but
    // we'd otherwise stall the full timeout. Reject early so HLS fallback
    // kicks in fast.
    await new Promise((resolve, reject) => {
      webrtcReject = reject;
      const timeout = setTimeout(() => reject(new Error("WebRTC: no track within 5s")), 5000);
      webrtcTimeout = timeout;
      pc.addEventListener("iceconnectionstatechange", () => {
        if (pc.iceConnectionState === "failed" || pc.iceConnectionState === "disconnected") {
          clearTimeout(timeout);
          reject(new Error("WebRTC: ICE " + pc.iceConnectionState));
        }
      });
      pc.ontrack = (ev) => {
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
    // Called after HLS stall/fatal error to restart the stream. Re-checks
    // camera state first: if the backend connection dropped, the camera entity
    // goes to "idle" (CameraEntityFeature.STREAM is cleared), and calling
    // camera/stream WS immediately produces "does not support play stream
    // service" errors. Use _waitForStreamReady() to wait for the backend.
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
    if (this._hls) { this._hls.destroy(); this._hls = null; }
    if (this._stallChecker) { clearInterval(this._stallChecker); this._stallChecker = null; }
    if (this._hlsKeepaliveTimer) { clearInterval(this._hlsKeepaliveTimer); this._hlsKeepaliveTimer = null; }
    if (this._activateSafetyTimer) { clearTimeout(this._activateSafetyTimer); this._activateSafetyTimer = null; }
    if (this._webrtcPc) { this._webrtcPc.close(); this._webrtcPc = null; }
    // try/catch: on page reload / WS close the subscription may already be
    // gone, so the unsubscribe rejects with "Subscription not found" — an
    // unhandled promise rejection in the console on every reload with an active
    // WebRTC stream. Swallow it (matches the guarded call at the offer site).
    if (this._webrtcUnsub) { try { this._webrtcUnsub(); } catch {}; this._webrtcUnsub = null; }
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
    // Hide tap-to-play overlay if stream stops before user tapped
    const tapOverlay = this.shadowRoot?.getElementById("tap-to-play-overlay");
    if (tapOverlay) tapOverlay.classList.remove("visible");
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

    // Capture current image byte count BEFORE firing the service — the service
    // refreshes all cameras on the coordinator, so another card's earlier click
    // may already have refreshed this camera's bytes. Fetching prevBytes first
    // avoids baselining against an already-fresh image (which would make every
    // subsequent poll see no byte change and spin until the 15 s timeout).
    const token   = this._hass?.states[this._entities.camera]?.attributes?.access_token || "";
    const dispW   = Math.round(this.offsetWidth || 640);
    const currUrl = `/api/camera_proxy/${this._entities.camera}?token=${token}&t=${Date.now()}&width=${dispW}`;

    const startPoll = (prevBytes) => {
      // Fire backend refresh AFTER prevBytes capture — see above.
      if (this._hass?.services?.bosch_shc_camera?.trigger_snapshot)
        this._callService("bosch_shc_camera", "trigger_snapshot", {});
      // First poll after 500ms — RCP refresh completes in ~100ms, so 500ms is plenty
      const startTime = Date.now();
      this._snapshotPollTimer = setTimeout(
        () => this._pollSnapshotImage(prevBytes, startTime), 500
      );
    };

    // Get current byte count (best-effort), then fire service + start polling
    fetch(currUrl)
      .then(r => r.ok ? r.blob() : null)
      .then(blob => startPoll(blob ? blob.size : 0))
      .catch(() => startPoll(0));
  }

  _pollSnapshotImage(prevBytes, startTime) {
    // 6 s total: REMOTE fetch completes in ~3 s, LOCAL snap.jpg in ~1.4 s; the
    // previous 15 s value was only needed when byte-comparison accidentally
    // baselined against an already-fresh image (fixed above). Shorter timeout
    // means the spinner resolves faster when a change isn't detected.
    const TIMEOUT  = 6000;
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
    // Apple-style success flash on the pill-bar snapshot button — 420ms
    // green pulse confirms the snapshot completed. Add then remove the
    // class so the animation can re-trigger on the next snapshot.
    const apSnap = this.shadowRoot.getElementById("ap-btn-snapshot");
    if (apSnap) {
      apSnap.classList.remove("ok-flash");
      // Force reflow so the re-add restarts the animation cleanly.
      void apSnap.offsetWidth;
      apSnap.classList.add("ok-flash");
      setTimeout(() => apSnap.classList.remove("ok-flash"), 450);
    }
  }

  // ── State update ──────────────────────────────────────────────────────────
  _update() {
    if (!this._hass || !this._config) return;
    const hass = this._hass;
    const ents = this._entities;

    // Sync HLS-fallback banner visibility with live video state — only on
    // the remote-skip-WebRTC paths (Companion+ext or mobile-browser+ext).
    if (this._remoteSkipWebRTC) {
      const banner = this.shadowRoot?.getElementById("ios-hls-banner");
      if (banner) banner.classList.toggle("visible", !!this._liveVideoActive);
    }

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
    // Apple-style title pill mirror
    const apTitleEl = this.shadowRoot.getElementById("ap-title-text");
    if (apTitleEl) apTitleEl.textContent = titleEl?.textContent || "Bosch Camera";

    // Push status badge
    const pushState  = hass.states[ents.push_status];
    const pushBadge  = this.shadowRoot.getElementById("push-badge");
    const pushLabel  = this.shadowRoot.getElementById("push-label");
    if (pushBadge && pushLabel) {
      const isFcm  = pushState?.state === "fcm_push";
      pushBadge.className = "push-badge " + (isFcm ? "push" : "poll");
      pushLabel.textContent = isFcm ? "push" : "poll";
    }

    // Status dot. The sensor's native value is lowercase
    // (sensor.py uses ENUM options ["online","offline","unknown"]) while
    // the camera-entity `.attributes.status` is uppercase ("ONLINE"/"OFFLINE").
    // Normalise to uppercase here so the dot turns red on offline regardless
    // of which entity feeds the badge. Live bug 2026-05-21: cards showed a
    // green-ish/unknown dot for Eingang + Kamera (both OFFLINE because the
    // Gen1 hardware is unplugged) — the sensor returns "offline" lower-case,
    // the case-sensitive mapping fell through to "unknown", and the
    // `unknown` class kept the default neutral grey rather than the red
    // offline tint.
    const statusState = String(hass.states[ents.status]?.state || "UNKNOWN").toUpperCase();
    const statusDot   = this.shadowRoot.getElementById("status-dot");
    const infoStatus  = this.shadowRoot.getElementById("info-status");
    if (statusDot) statusDot.className = "status-dot " + ({ ONLINE: "online", OFFLINE: "offline" }[statusState] || "unknown");
    if (infoStatus) infoStatus.textContent = statusState;
    // Apple-style status dot mirror (in title pill). Dot color encodes the
    // primary state: green=online+normal, orange=online+privacy, red=offline.
    // Privacy precedes online so users see the "shielded" colour at a glance
    // without a separate top-right badge.
    const apDot = this.shadowRoot.getElementById("ap-dot");
    if (apDot) {
      let dotCls = "ap-dot ";
      if (statusState === "OFFLINE") dotCls += "offline";
      else if (hass.states[ents.privacy]?.state === "on") dotCls += "privacy";
      else if (statusState === "ONLINE") dotCls += "online";
      apDot.className = dotCls;
    }

    // Info row: connection type (LAN/Cloud) + buffering time (API reaction).
    // connection_type and buffering_time_ms are attributes of camera.bosch_<cam>
    // (set from the Bosch cloud's PUT /connection response — LOCAL=500ms,
    // REMOTE=1000ms typically). While stream is idle both rows show "—".
    const camAttrs = hass.states[ents.camera]?.attributes || {};
    const camConnType = camAttrs.connection_type || "";
    const bufMs      = camAttrs.buffering_time_ms;
    const infoConn   = this.shadowRoot.getElementById("info-connection");
    const infoBuf    = this.shadowRoot.getElementById("info-buffering");
    if (infoConn) {
      infoConn.textContent = camConnType === "LOCAL" ? "LAN" : camConnType === "REMOTE" ? "Cloud" : "—";
    }
    if (infoBuf) {
      infoBuf.textContent = (typeof bufMs === "number" && bufMs > 0) ? `${bufMs} ms` : "—";
    }

    // Auth/integration overlay — camera entity is `unavailable` when the
    // Bosch integration's coordinator fails (typically: refresh token rejected
    // by Keycloak after long sessions or server-side rotation). The overlay
    // covers offline-overlay (z-index 9 vs 8) so the user sees the actionable
    // re-login banner instead of a generic "offline" state.
    const camState = hass.states[ents.camera]?.state;
    const isIntegrationDown = camState === "unavailable" || camState === undefined;
    const authOverlay = this.shadowRoot.getElementById("auth-overlay");
    if (authOverlay) authOverlay.classList.toggle("visible", isIntegrationDown);

    // Offline overlay (suppressed while auth-overlay is up to avoid double overlay)
    const offlineOverlay = this.shadowRoot.getElementById("offline-overlay");
    const isOffline = !isIntegrationDown && statusState === "OFFLINE";
    if (offlineOverlay) {
      offlineOverlay.classList.toggle("visible", isOffline);
      if (isOffline) {
        const lastChanged = hass.states[ents.status]?.last_changed;
        // Populate the offline-overlay's camera name line (apple-style only —
        // legacy mode hides .offline-cam-name via the base display:none rule).
        const camNameEl = this.shadowRoot.getElementById("offline-cam-name");
        if (camNameEl) {
          camNameEl.textContent = this._config?.title
            || hass.states[ents.camera]?.attributes?.friendly_name
            || ents.camera;
        }
        const sub = this.shadowRoot.getElementById("offline-subtitle");
        if (sub && lastChanged) {
          try {
            const d = new Date(lastChanged);
            sub.textContent = `Zuletzt gesehen: ${d.toLocaleString("de-DE", {day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit"})}`;
          } catch { /* fall back to default text */ }
        }
      }
    }
    this._isOffline = isOffline;
    // Host class for CSS-side dimming of camera-state buttons (Stream / Light
    // / Privacy) when the hardware is unreachable. Snapshot / Fullscreen /
    // More stay fully bright since they operate on cached state.
    this.classList.toggle("cam-offline", isOffline);

    // Streaming state
    const isStreaming  = this._isStreaming();
    const badge        = this.shadowRoot.getElementById("stream-badge");
    const streamLabel  = this.shadowRoot.getElementById("stream-label");
    const btnStream    = this.shadowRoot.getElementById("btn-stream");
    const btnStreamLbl = this.shadowRoot.getElementById("btn-stream-label");

    // Shared stream lifecycle — single source of truth for ALL browser
    // sessions (2026-05-30). The backend coordinator publishes the live
    // phase on the stream_status sensor (idle / connecting / warming_up /
    // streaming / streaming_remote), pushed by HA to every client. We derive
    // the badge / button / overlay from this shared value (plus the shared
    // switch for on/off intent) instead of the per-session local
    // `_startingLiveVideo` flag — so a second browser opened mid-connect shows
    // the SAME "connecting / waking up" state as the first, never a false
    // "idle". The false-idle was the root of the cross-session bug: it made the
    // user tap again, which re-fired turn_on and tore down the other session.
    const switchOn = hass.states[ents.switch]?.state === "on";
    const backendStreamStatus = hass.states[ents.streamStatus]?.state || camAttrs.stream_status || "";
    const sharedConnecting = switchOn
      && (backendStreamStatus === "connecting" || backendStreamStatus === "warming_up");

    // Badge = video reality, not switch intent:
    //   • "Live" (green) ONLY when THIS session's video is actually playing
    //     (_liveVideoActive). Never show Live just because the switch is on.
    //   • "connecting" (orange "Verbinde") whenever the stream is wanted/being
    //     established but no live frame yet — switch on, or local HLS negotiating,
    //     or the shared backend warming_up/connecting.
    //   • "idle" (hidden) when off.
    // `_liveVideoActive` wins so the badge flips to Live the moment the video
    // plays even if the backend stream_status sensor still lags; and there is no
    // premature "Live" from switch-on-without-video. 2026-05-30.
    const streamBadgeState = isOffline ? "offline"
                           : (this._liveVideoActive ? "streaming"
                           : ((isStreaming || this._startingLiveVideo || sharedConnecting) ? "connecting"
                           : "idle"));
    if (badge)        badge.className = "stream-badge " + streamBadgeState;
    if (streamLabel && !isStreaming) streamLabel.textContent = streamBadgeState;

    // Video is playing → the connecting phase is over. Force-tear-down any
    // lingering connecting overlay + keepalive timers that a lagging backend
    // stream_status (it can trail the first frame by seconds) would otherwise
    // keep re-asserting — the "Kamera wird aufgeweckt…" overlay was sticking
    // while the stream already ran in the background. 2026-05-30.
    if (this._liveVideoActive && (this._streamConnecting || this._waitingForStream)) {
      this._streamConnecting = false;
      this._waitingForStream = false;
      if (this._connectSteps) { this._connectSteps.forEach((t) => clearTimeout(t)); this._connectSteps = null; }
      this._setLoadingOverlay(false);
    }

    // Apple-style status badge (top-right glass pill) + stream pill-button state.
    // Privacy state is intentionally NOT shown in the badge — it lives in the
    // title-pill dot color (warn/orange) per Apple Home's single-indicator
    // convention. Badge stays reserved for transient stream state.
    const apBadge = this.shadowRoot.getElementById("ap-badge");
    const apBtnStream = this.shadowRoot.getElementById("ap-btn-stream");
    const apBtnPrivacy = this.shadowRoot.getElementById("ap-btn-privacy");
    const apStreamIcon = this.shadowRoot.getElementById("ap-stream-icon");
    const privActive = hass.states[ents.privacy]?.state === "on";
    if (apBadge) {
      // Priority: offline > live > connecting > hidden. Privacy hides badge.
      if (streamBadgeState === "offline") {
        apBadge.className = "ap-badge offline"; apBadge.textContent = "Offline";
      } else if (streamBadgeState === "streaming") {
        apBadge.className = "ap-badge live"; apBadge.textContent = "Live";
      } else if (streamBadgeState === "connecting") {
        apBadge.className = "ap-badge connecting"; apBadge.textContent = "Verbinde";
      } else {
        apBadge.className = "ap-badge hidden"; apBadge.textContent = "";
      }
    }
    if (apBtnStream) {
      apBtnStream.classList.toggle("on", isStreaming);
      apBtnStream.classList.toggle("connecting", streamBadgeState === "connecting");
      apBtnStream.setAttribute("aria-pressed", isStreaming ? "true" : "false");
      apBtnStream.setAttribute("title", isStreaming ? "Live-Stream stoppen" : "Live-Stream starten");
      // Swap the SVG path between play (▶) and stop (■) icons
      if (apStreamIcon) {
        apStreamIcon.innerHTML = isStreaming
          ? '<rect x="6" y="6" width="12" height="12" rx="2"/>'
          : '<path d="M8 5v14l11-7L8 5z"/>';
      }
    }
    if (apBtnPrivacy) {
      // Treat privacy as a regular on/off control (white tile when active),
      // not a "danger" state — privacy is intentional, not an emergency.
      apBtnPrivacy.classList.toggle("on", privActive);
      apBtnPrivacy.classList.remove("danger");
      apBtnPrivacy.setAttribute("aria-pressed", privActive ? "true" : "false");
    }
    // Light mirror (Audio toggle lives in the Mehr menu, not the pill-bar)
    const apBtnLight = this.shadowRoot.getElementById("ap-btn-light");
    const lightActive = hass.states[ents.light]?.state === "on";
    if (apBtnLight) {
      apBtnLight.classList.toggle("on", lightActive);
      apBtnLight.setAttribute("aria-pressed", lightActive ? "true" : "false");
    }
    // Hide entities that don't exist on this model (e.g. light on indoor cam)
    if (apBtnLight) apBtnLight.toggleAttribute("hidden", !hass.states[ents.light]);
    if (apBtnPrivacy) apBtnPrivacy.toggleAttribute("hidden", !hass.states[ents.privacy]);
    // "streaming" label text is updated by _onImageLoaded() with uptime counter
    if (btnStream) {
      // Toggle active + pending classes (pending = service call in flight)
      const streamOpt = this._optimistic[ents.switch];
      const streamPending = streamOpt === "pending";
      btnStream.className = "btn btn-stream"
        + (isStreaming ? " active" : "")
        + (streamPending ? " pending" : "");
      // Register DOM-id so _flashEntityError can find this element on failure
      this._entityToBtnId[ents.switch] = "btn-stream";
    }
    if (btnStreamLbl) btnStreamLbl.textContent = isStreaming ? "Stop Stream" : "Live Stream";

    // Connection-type badge — show ONLY when actually streaming via the
    // Bosch cloud relay. LAN is the configured-default for this setup, so
    // a LAN badge is just noise on every card every time. A "Cloud" badge
    // is informative because it signals an unexpected fallback (LAN-first
    // tried, cloud took over).
    const connType  = hass.states[ents.switch]?.attributes?.connection_type || "";
    const connBadge = this.shadowRoot.getElementById("conn-badge");
    if (connBadge) {
      if (isStreaming && connType === "REMOTE") {
        connBadge.className = "conn-badge remote";
        connBadge.textContent = "Cloud";
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
      if (this._hass?.services?.bosch_shc_camera?.trigger_snapshot)
        this._callService("bosch_shc_camera", "trigger_snapshot", {});
      this._scheduleImageLoad(3500);
      this._startRefreshTimer();
    }
    this._lastStreaming = isStreaming;

    // Start HLS video when stream turns ON.
    // Wait until camera entity actually reports streaming (stream_source set)
    // to avoid "does not support play stream" errors from premature WS calls.
    // Show loading overlay during the wait (outdoor pre-warm takes ~35s).
    // Also re-triggers if card got stuck (e.g. WS failed during page load).
    // "Cold open": stream_status is warming_up/connecting (read above from the
    // shared sensor — persistent across sessions, no toggle-click needed).
    // `backendWaiting` reflects warming_up/connecting on the backend, but
    // those states can fire WITHOUT user intent — the snapshot-refresh path
    // opens a live session, which HA's stream component then prepares,
    // which sets stream_status=connecting. The card must NOT enter the
    // visual connecting/loading state for that "phantom" backend warmup:
    // it would show a CONNECTING badge + 25-35 s loading overlay on every
    // dashboard open even though the user never asked for video. Gated on the
    // switch (switchOn) — same condition as `sharedConnecting` above.
    const backendWaiting = sharedConnecting;
    // Auto-play gate transitions: track switch.state changes on every
    // _update() pass. OFF→ON in overlay-required modes shows the gate;
    // ON→OFF hides it. Runs synchronously here so the early-return below
    // sees the freshest _playGateActive value.
    this._evaluateGateForStreamTransition();
    // Gate is active → suppress HLS connect + loading spinner. The user
    // taps the gate to reveal (and tap handler clears _playGateActive,
    // then triggers another _update() which proceeds to start HLS).
    if (this._playGateActive) {
      this._setLoadingOverlay(false);
      return;
    }
    if ((shouldVideo || backendWaiting) && !this._liveVideoActive && !this._startingLiveVideo && !this._waitingForStream) {
      this._waitingForStream = true;
      // snapshot_during_warmup: fetch current snapshot so the last known image
      // shows as background under the semi-transparent overlay instead of black.
      // Guard with _awaitingFresh to avoid a double fetch when firstHass already
      // triggered one in set hass().
      if (this._config.snapshot_during_warmup && !this._imageLoaded && !this._awaitingFresh) {
        this._triggerFreshSnapshot();
      }
      // Overlay text from the SHARED stream_status (synced across sessions).
      this._setLoadingOverlay(true, this._streamPhaseText());
      this._waitForStreamReady();
    }
    if (!shouldVideo && !backendWaiting) {
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
    // Last-event text: now only shown as overlay on the camera image
    // (info-row slots repurposed to Status/Verbindung/Reaktion). The event-
    // driven snapshot refresh below still uses lastEventState.
    const lastEventState = hass.states[ents.last_event];
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
    if (lastEventOverlay) lastEventOverlay.textContent = lastEventStr !== "—" ? `Letztes: ${lastEventStr}` : "";
    // Apple-style "last event" glass pill (bottom-right of video). Show only
    // when we have a timestamp AND the stream isn't actively playing — the
    // LIVE badge already owns the user's attention while a stream is live,
    // and stacking both would be visual noise.
    const apLastEvent = this.shadowRoot.getElementById("ap-last-event");
    const apLastEventText = this.shadowRoot.getElementById("ap-last-event-text");
    if (apLastEvent && apLastEventText) {
      const hasEvent = lastEventStr !== "—";
      // Pretty short time: today → "14:23", earlier → "Mo 14:23" or "23.05."
      let pretty = lastEventStr;
      if (hasEvent && lastEventState?.state) {
        try {
          const d = new Date(lastEventState.state);
          if (!isNaN(d)) {
            const sameDay = d.toDateString() === new Date().toDateString();
            pretty = sameDay
              ? d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })
              : d.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" });
          }
        } catch { /* fall back to lastEventStr */ }
      }
      apLastEventText.textContent = pretty;
      // When the camera itself burns the current date/time into the video
      // (camera attribute camera_timestamp_overlay = true), our last-event
      // pill becomes visually redundant — same info, two places. Hide the
      // pill entirely in that case. User report 2026-05-24.
      const camTimestampOverlay = !!(hass.states[ents.camera]?.attributes?.camera_timestamp_overlay);
      apLastEvent.classList.toggle("visible", hasEvent && !camTimestampOverlay);
      // Hide-during-stream class hides via CSS while video is playing.
      apLastEvent.classList.toggle("hide-during-stream", isStreaming);
    }

    // Events today — overlay only now (info-row no longer carries it)
    const evTodayState = hass.states[ents.events_today];
    const evOverlay    = this.shadowRoot.getElementById("events-overlay");
    const evCount      = evTodayState?.state ?? "—";
    if (evOverlay)   evOverlay.textContent   = evCount !== "—" ? `${evCount} Events heute` : "";

    // Toggle buttons — Ton / Licht / Privat / Benachrichtigungen / Gegensprech.
    this._updateToggleBtn("btn-audio",         ents.audio,         hass.states[ents.audio]);
    this._updateToggleBtn("btn-light",         ents.light,         hass.states[ents.light]);
    this._updateToggleBtn("btn-privacy",       ents.privacy,       hass.states[ents.privacy]);
    const privInline = this.shadowRoot.getElementById("btn-privacy-inline");
    if (privInline) {
      const ps = hass.states[ents.privacy]?.state;
      const optVal = this._optimistic[ents.privacy];
      const isPending = optVal === "pending";
      const ds = (ents.privacy in this._optimistic && !isPending) ? optVal : ps;
      privInline.classList.toggle("on", ds === "on");
    }
    this._updateToggleBtn("btn-notifications", ents.notifications, hass.states[ents.notifications]);
    this._updateToggleBtn("btn-intercom",      ents.intercom,      hass.states[ents.intercom]);

    // Light sub-controls — show only when entities exist
    const lightSubControls = this.shadowRoot.getElementById("light-sub-controls");
    if (lightSubControls) {
      const hasFront = ents.frontLight && hass.states[ents.frontLight];
      const hasWall = ents.wallwasher && hass.states[ents.wallwasher];
      const hasIntensity = ents.frontLightIntensity && hass.states[ents.frontLightIntensity];
      lightSubControls.style.display = (hasFront || hasWall || hasIntensity) ? "" : "none";
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

    // Gen2: Accordion visibility + toggle updates
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

    // Gen2 Indoor II — alarm system controls (only shown when the alarm switch entity exists)
    const hasAlarmSystem = ents.alarmSystemArm && hass.states[ents.alarmSystemArm];
    for (const [rowId, entId] of [
      ["btn-alarm-arm",   ents.alarmSystemArm],
      ["btn-alarm-mode",  ents.alarmMode],
      ["btn-prealarm",    ents.preAlarm],
    ]) {
      const row = this.shadowRoot.getElementById(rowId);
      if (row) row.style.display = (hasAlarmSystem && entId && hass.states[entId]) ? "flex" : "none";
    }
    this._updateToggleBtn("btn-alarm-arm",   ents.alarmSystemArm, hass.states[ents.alarmSystemArm]);
    this._updateToggleBtn("btn-alarm-mode",  ents.alarmMode,      hass.states[ents.alarmMode]);
    this._updateToggleBtn("btn-prealarm",    ents.preAlarm,       hass.states[ents.preAlarm]);

    // Gen2 Indoor II — Power-LED brightness slider
    const powerLedRow = this.shadowRoot.getElementById("power-led-row");
    const powerLedEnt = hass.states[ents.powerLedBrightness];
    if (powerLedRow) powerLedRow.style.display = powerLedEnt ? "flex" : "none";
    if (powerLedEnt) {
      const slider = this.shadowRoot.getElementById("power-led-slider");
      const valEl  = this.shadowRoot.getElementById("power-led-value");
      const val    = parseInt(powerLedEnt.state) || 0;
      if (slider && document.activeElement !== slider) slider.value = val;
      if (valEl) valEl.textContent = val + "%";
    }

    // Automation toggles — update state + name from HA
    if (ents.automations?.length) {
      ents.automations.forEach((eid, i) => {
        const btn = this.shadowRoot.getElementById(`btn-auto-${i}`);
        if (!btn) return;
        const state = hass.states[eid];
        if (!state) { btn.style.display = "none"; return; }
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

    // Gen2: Top/Bottom brightness sliders sync.
    // When the light is OFF the API returns brightness=0, but the integration
    // remembers the last non-zero value in the light entity's
    // `last_brightness_pct` attribute — prefer that so the slider shows what
    // will be applied on next turn-on.
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

    // Gen2: Top/Bottom LED toggles + color dots
    //
    // Color resolution order:
    //   1. Light entity's rgb_color attribute (only populated when state=="on")
    //   2. Light entity's last_rgb_color extra attribute (populated regardless
    //      of on/off — backed by RestoreEntity in light.py, survives HA restart)
    //   3. In-memory _lastTopColor / _lastBotColor (last color this card instance saw)
    //   4. Fallback grey
    // HA's core light platform blanks `rgb_color` when state=="off", which is
    // why the card used to fall back to grey when the wallwasher was off.
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

    // Gen2: RGB color circles (picker row)
    const rgbRow = this.shadowRoot.getElementById("rgb-lights-row");
    if (rgbRow) rgbRow.style.display = (hasTopLed || hasBotLed) ? "" : "none";
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
      const el = parseFloat(hass.states[ents.lensElevation]?.state) || 2.0;
      lensSliderEl.value = Math.round(el * 100);
      lensValue.textContent = el.toFixed(2) + " m";
    }

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

    // Accordion: Schedules & Zones
    this._updateSchedulesSection(hass, ents);

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
    _hideAccIf("acc-advanced", [ents.timestamp, ents.autofollow, ents.motion, ents.recordSound, ents.privacySound]);
    _hideAccIf("acc-diagnostics", [ents.wifi, ents.firmware, ents.ambient, ents.movementToday, ents.audioToday]);
    _hideAccIf("acc-schedules", [ents.scheduleRules, ents.motionZones]);

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
    // On Android WebView: _androidAudioMuted starts true so the video is always
    // muted at startup. Cleared on the first explicit Ton toggle so the entity
    // state takes over normally from that point on.
    if (this._liveVideoActive) {
      const video   = this.shadowRoot.getElementById("cam-video");
      const audioOn = this._getEffectiveState(ents.audio) === "on";
      if (video) {
        // Only ever MUTE here. Unmuting requires a genuine user gesture
        // (Chrome autoplay policy) — doing it in this programmatic, hass-driven
        // update makes Chrome pause the element ("Unmuting failed and the
        // element was paused instead"). The unmute lives in _toggleAudio(), the
        // Ton-tap handler, which runs inside a real gesture. "!video.paused" is
        // NOT a sufficient gate — a muted autoplaying video still needs a
        // gesture to unmute. See docs/card-architecture.md.
        if (!audioOn || this._androidAudioMuted) {
          video.muted = true;
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

    // Privacy just turned OFF → reload the <img> after the backend's own
    // post-privacy refresh has had time to land.
    //
    // The integration already schedules a fresh snapshot when privacy
    // turns off (shc._schedule_privacy_off_snapshot): 0.5 s for outdoor,
    // 5 s for indoor cameras (the indoor shutter motor needs the time to
    // open before snap.jpg returns a real frame). Triggering a second
    // refresh from the card before that delay completes is harmful — it
    // races the Bosch cooldown, async_camera_image returns the 1×1
    // placeholder JPEG, and HA's proxy caches that placeholder, which
    // the user then sees as a black frame for 1–2 s.
    //
    // So: no trigger_snapshot service call, no early reloads. Just reload
    // the <img> at 6 s (covers indoor 5 s delay + 1 s buffer) and 9 s
    // (safety net for slow cameras). Until then the last cached pre-
    // privacy frame stays visible — better than a black flash.
    if (this._lastPrivacy === true && !privacyOn) {
      this._scheduleImageLoad(6000);
      this._scheduleImageLoad(9000);
    }
    this._lastPrivacy = privacyOn;

    // Motion zones overlay — SVG polygons from RCP 0x0c00/0x0c0a sensor data
    this._updateMotionZones(hass, ents);
    // Privacy mask overlay — from privacy masks sensor
    this._updatePrivacyMasks(hass, ents);

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

    // Offline: hide ALL controls below the image (runs last to override accordion logic)
    if (this._isOffline) {
      for (const sel of [".info-row", ".btn-row", ".switch-rows"]) {
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
        entity_id: this._entities.camera,
      });
      const deviceId = reg?.device_id;
      if (!deviceId) return;
      const result = await hass.callWS({
        type: "search/related",
        item_type: "device",
        item_id: deviceId,
      });
      const autoIds = (result.automation || [])
        .filter(eid => hass.states[eid])
        .sort();
      if (autoIds.length) {
        this._entities.automations = autoIds;
        this._rebuildAutomationRows();
      }
    } catch (e) {
      const prefix = `automation.${this._base}_`;
      const fallback = Object.keys(hass.states)
        .filter(eid => eid.startsWith(prefix))
        .sort();
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
        this._callService("automation", st === "on" ? "turn_off" : "turn_on", {entity_id: eid});
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
    // Remember mapping so _flashEntityError() can find the DOM element
    if (entityId) this._entityToBtnId[entityId] = id;
    // Hide when entity doesn't exist or is unavailable/unknown
    // (e.g. camera light on a camera that has no physical light)
    const state = entityState?.state;
    if (!entityState || !state || state === "unavailable" || state === "unknown") {
      btn.style.display = "none";
      return;
    }
    btn.style.display = "";
    // Use optimistic state for immediate visual feedback, fall back to HA state.
    // "pending" is a transient state during an in-flight service call: keep the
    // button visually on whatever HA currently reports (no flip yet) but add the
    // "pending" class for the subtle fade/pulse animation.
    const optVal = this._optimistic[entityId];
    const isPending = optVal === "pending";
    const displayState = (entityId in this._optimistic && !isPending) ? optVal : state;
    btn.classList.toggle("on", displayState === "on");
    btn.classList.toggle("pending", isPending);
    btn.classList.remove("unavailable");
    btn.disabled = false;
  }

  // ── Schedules & Zones ──────────────────────────────────────────────────────
  _updateSchedulesSection(hass, ents) {
    const WEEKDAY_NAMES = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];

    // Rules count
    const rulesState = hass.states[ents.scheduleRules];
    const rulesCountEl = this.shadowRoot.getElementById("diag-rules-count");
    if (rulesCountEl) {
      rulesCountEl.textContent = (rulesState?.state != null && rulesState.state !== "unavailable") ? rulesState.state : "—";
    }

    // Rules list
    const rulesListEl = this.shadowRoot.getElementById("rules-list");
    if (rulesListEl && rulesState) {
      const rules = rulesState.attributes?.rules || [];
      const camId = hass.states[ents.status]?.attributes?.camera_id || "";
      if (rules.length === 0) {
        rulesListEl.innerHTML = '<div style="font-size:11px;color:#666;padding:4px 0">Keine Zeitpläne</div>';
      } else {
        // Build rules HTML — only re-render when data changes (compare JSON)
        const rulesKey = JSON.stringify(rules);
        if (this._lastRulesKey !== rulesKey) {
          this._lastRulesKey = rulesKey;
          rulesListEl.innerHTML = rules.map((r, i) => {
            const days = (r.weekdays || []).map(d => WEEKDAY_NAMES[d] || d).join(", ");
            // Sensor uses "active"/"start"/"end", API uses "isActive"/"startTime"/"endTime"
            const isActive = r.active ?? r.isActive ?? false;
            const startT = r.start || r.startTime || "?";
            const endT = r.end || r.endTime || "?";
            const activeClass = isActive ? " active" : "";
            const activeLabel = isActive ? "AN" : "AUS";
            return `<div class="rule-row" data-rule-idx="${i}">
              <div class="rule-info">
                <div class="rule-name">${this._escHtml(r.name || "Regel " + (i+1))}</div>
                <div class="rule-time">${startT} – ${endT}</div>
                <div class="rule-days">${days}</div>
              </div>
              <button class="rule-toggle${activeClass}" data-rule-id="${r.id}" data-cam-id="${camId}" data-active="${isActive ? "true" : "false"}">${activeLabel}</button>
              <button class="rule-delete" data-rule-id="${r.id}" data-cam-id="${camId}" title="Löschen">✕</button>
            </div>`;
          }).join("");

          // Wire toggle buttons
          rulesListEl.querySelectorAll(".rule-toggle").forEach(btn => {
            btn.addEventListener("click", (e) => {
              e.stopPropagation();
              const ruleId = btn.dataset.ruleId;
              const cId = btn.dataset.camId;
              const newActive = btn.dataset.active !== "true";
              this._callService("bosch_shc_camera", "update_rule", {
                camera_id: cId, rule_id: ruleId, is_active: newActive,
              });
              // Optimistic UI
              btn.dataset.active = newActive ? "true" : "false";
              btn.textContent = newActive ? "AN" : "AUS";
              btn.classList.toggle("active", newActive);
            });
          });

          // Wire delete buttons
          rulesListEl.querySelectorAll(".rule-delete").forEach(btn => {
            btn.addEventListener("click", (e) => {
              e.stopPropagation();
              const ruleId = btn.dataset.ruleId;
              const cId = btn.dataset.camId;
              this._callService("bosch_shc_camera", "delete_rule", {
                camera_id: cId, rule_id: ruleId,
              });
              // Remove row optimistically
              btn.closest(".rule-row")?.remove();
            });
          });
        }
      }
    }

    // Motion zones toggle visual state
    const zonesToggle = this.shadowRoot.getElementById("btn-show-zones");
    if (zonesToggle) {
      zonesToggle.classList.toggle("on", this._showMotionZones);
      // Only show toggle when motion zones sensor exists
      const mzExists = hass.states[ents.motionZones];
      zonesToggle.style.display = mzExists ? "" : "none";
    }

    // Motion zones count — prefer Gen2 zones, then cloud zones, then RCP
    const zonesCountEl = this.shadowRoot.getElementById("diag-zones-count");
    const mzState = hass.states[ents.motionZones];
    const gen2Zones = mzState?.attributes?.gen2_zones || [];
    const cloudZones = mzState?.attributes?.cloud_zones || [];
    if (zonesCountEl) {
      if (gen2Zones.length > 0) zonesCountEl.textContent = `${gen2Zones.length} (Gen2)`;
      else if (cloudZones.length > 0) zonesCountEl.textContent = String(cloudZones.length);
      else if (mzState?.state != null && mzState.state !== "unavailable") zonesCountEl.textContent = `${mzState.state} (RCP)`;
      else zonesCountEl.textContent = "—";
    }

    // Privacy masks count — from dedicated privacy masks sensor
    const masksCountEl = this.shadowRoot.getElementById("diag-masks-count");
    const pmState = hass.states[ents.privacyMasks];
    const gen2Areas = pmState?.attributes?.gen2_private_areas || [];
    const cloudMasks = pmState?.attributes?.cloud_privacy_masks || [];
    if (masksCountEl) {
      const total = gen2Areas.length || cloudMasks.length;
      masksCountEl.textContent = total > 0 ? String(total) : (pmState?.state != null && pmState.state !== "unavailable") ? pmState.state : "0";
    }

    // Privacy mask toggle visual state
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

    const services = [
      { icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/><circle cx="12" cy="13" r="4"/></svg>',
        label: "Snapshot", svc: "trigger_snapshot", data: {} },
      { icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18"/></svg>',
        label: "Zonen lesen", svc: "get_motion_zones", data: () => ({camera_id: camId()}) },
      { icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>',
        label: "Privacy-Masken", svc: "get_privacy_masks", data: () => ({camera_id: camId()}) },
      { icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>',
        label: "Freunde", svc: "list_friends", data: {} },
      { icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>',
        label: "Regel erstellen", svc: "_prompt_create_rule", data: null },
      { icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/></svg>',
        label: "Licht-Zeitplan", svc: "get_lighting_schedule", data: () => ({camera_id: camId()}) },
      { icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
        label: "Verbindung", svc: "open_live_connection", data: () => ({camera_id: camId()}) },
      { icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 01-3.46 0"/><line x1="1" y1="1" x2="23" y2="23"/></svg>',
        label: "Sirene", svc: "_trigger_siren", data: null },
    ];

    grid.innerHTML = services.map((s, i) => `<button class="svc-btn" data-svc-idx="${i}">${s.icon}<span>${s.label}</span></button>`).join("");

    const resultEl = this.shadowRoot.getElementById("svc-result");

    grid.querySelectorAll(".svc-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        const idx = parseInt(btn.dataset.svcIdx);
        const svc = services[idx];
        if (!svc || !this._hass) return;

        // Special: trigger siren (button entity)
        if (svc.svc === "_trigger_siren") {
          if (!confirm("Sirene wirklich auslösen?")) return;
          btn.classList.add("running");
          const sirenEntity = this._entities.siren;
          if (sirenEntity && this._hass.states[sirenEntity]) {
            this._hass.callService("button", "press", { entity_id: sirenEntity });
            if (resultEl) { resultEl.style.display = ""; resultEl.textContent = "Sirene wird ausgelöst..."; }
          } else {
            if (resultEl) { resultEl.style.display = ""; resultEl.textContent = "Sirene nicht verfügbar für diese Kamera."; }
          }
          setTimeout(() => { btn.classList.remove("running"); }, 3000);
          return;
        }

        // Special: prompt for create_rule
        if (svc.svc === "_prompt_create_rule") {
          const name = prompt("Regel-Name:", "Neue Regel");
          if (!name) return;
          const start = prompt("Startzeit (HH:MM):", "08:00");
          if (!start) return;
          const end = prompt("Endzeit (HH:MM):", "20:00");
          if (!end) return;
          btn.classList.add("running");
          this._callService("bosch_shc_camera", "create_rule", {
            camera_id: camId(), name: name,
            start_time: start + ":00", end_time: end + ":00",
            weekdays: [0,1,2,3,4,5,6], is_active: true,
          });
          if (resultEl) { resultEl.style.display = ""; resultEl.textContent = `Regel "${name}" wird erstellt...`; }
          setTimeout(() => { btn.classList.remove("running"); }, 3000);
          return;
        }

        btn.classList.add("running");
        const data = typeof svc.data === "function" ? svc.data() : svc.data;
        this._callService("bosch_shc_camera", svc.svc, data);
        if (resultEl) { resultEl.style.display = ""; resultEl.textContent = `${svc.label} wird ausgeführt...`; }
        setTimeout(() => {
          btn.classList.remove("running");
          if (resultEl) { resultEl.textContent = `${svc.label} abgeschlossen.`; setTimeout(() => { resultEl.style.display = "none"; }, 5000); }
        }, 3000);
      });
    });
  }

  // ── Helpers ───────────────────────────────────────────────────────────────
  _getEffectiveState(entityId) {
    if (entityId in this._optimistic) return this._optimistic[entityId];
    return this._hass?.states[entityId]?.state;
  }

  // Connecting-overlay text derived PURELY from the shared backend stream_status
  // (sensor, pushed to every client) — so every browser session shows the SAME
  // message at the same time during connect/warm-up, instead of each running its
  // own local time-based progression (which desynced the text). 2026-05-30.
  _streamPhaseText() {
    const st = this._hass?.states[this._entities.streamStatus]?.state
            || this._hass?.states[this._entities.camera]?.attributes?.stream_status
            || "";
    if (st === "warming_up") return "Kamera wird aufgeweckt…";
    if (st === "connecting")  return "Verbindung wird aufgebaut…";
    if (st === "streaming" || st === "streaming_remote") return "HLS wird geladen…";
    return "Stream wird gestartet…";
  }

  _waitForStreamReady(attempt = 0) {
    // Poll until camera entity reports "streaming" (stream_source is set).
    // Backend needs 25-35s for PUT /connection + TLS proxy + pre-warm
    // (outdoor camera is slower). Only then call camera/stream WS to avoid
    // "does not support play stream" errors from premature WS calls.
    if (!this._waitingForStream || !this._hass) return;

    const cam = this._hass.states[this._entities.camera];
    const camReady = cam?.state === "streaming";

    // Re-assert the loading overlay every 5s to keep the spinner alive — text
    // comes from the SHARED stream_status so it stays in sync across sessions
    // (no local time-based progression). 2026-05-30.
    if (attempt > 0 && attempt % 5 === 0) {
      this._setLoadingOverlay(true, this._streamPhaseText());
    }

    if (camReady) {
      // Camera entity reports streaming — stream_source is ready, start HLS
      this._waitingForStream = false;
      this._setLoadingOverlay(true, "HLS wird geladen…");
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

    const zoneState = hass.states[ents.motionZones];
    const gen2Zones = zoneState?.attributes?.gen2_zones || [];
    const cloudZones = zoneState?.attributes?.cloud_zones || [];
    const hasZones = gen2Zones.length > 0 || cloudZones.length > 0;

    const showZones = this._showMotionZones && hasZones;
    svg.classList.toggle("visible", showZones);
    if (!showZones) return;

    // Only re-render if coordinates changed (avoid DOM thrashing)
    const coordKey = JSON.stringify(gen2Zones.length > 0 ? gen2Zones : cloudZones);
    if (this._lastMotionCoordKey === coordKey) return;
    this._lastMotionCoordKey = coordKey;

    svg.innerHTML = "";

    if (gen2Zones.length > 0) {
      // Gen2 polygon zones: each zone has points array [{x,y}...], color, trigger
      const defaultColors = ["#0A84FF", "#34C759", "#FF9F0A", "#FF453A", "#AF52DE"];
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
      // Gen1 cloud zones: {x, y, w, h} normalized 0.0–1.0
      // ViewBox is 0-100, so multiply by 100.
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
      // Gen2 polygon private areas: each has points array [{x,y}...]
      for (const a of gen2Areas) {
        const points = a.points || a.polygon || a.vertices || [];
        if (points.length < 3) continue;
        const poly = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
        const pts = points.map(p => `${(p.x || 0) * 100},${(p.y || 0) * 100}`).join(" ");
        poly.setAttribute("points", pts);
        svg.appendChild(poly);
      }
    } else {
      // Gen1 cloud privacy masks: {x, y, w, h} normalized 0.0–1.0
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
    // Defensive pre-check: pull authoritative state from the server before
    // sending turn_on/turn_off. The HA-Companion-App's WebSocket subscription
    // can go stale after backgrounding or a Wi-Fi/Mobile-data switch, in
    // which case `_isStreaming()` returns the *old* state and a tap fires
    // the wrong direction (e.g. a "Stop" call when the user actually
    // wanted to start). Observed 2026-04-28: stream silently turned off
    // because the card was reading a stale `on` while the server already
    // had `off` — the toggle then re-flipped to off again.
    let serverIsOn = null;
    if (this._hass && this._entities.switch) {
      try {
        const fresh = await this._hass.callApi("GET", `states/${this._entities.switch}`);
        if (fresh?.state === "unavailable") return; // camera offline — abort silently
        if (fresh && fresh.state) serverIsOn = fresh.state === "on";
      } catch (e) {
        // REST failed — fall through to cached state, no worse than before
      }
    }
    const cachedIsOn = this._isStreaming();
    if (serverIsOn !== null && serverIsOn !== cachedIsOn) {
      console.warn(
        "bosch-camera-card: stale state detected — card thought " +
        (cachedIsOn ? "streaming" : "idle") +
        ", server says " + (serverIsOn ? "streaming" : "idle") +
        ". Refreshing the view; tap again to toggle.",
      );
      // Drop any optimistic override and re-render so the user sees the
      // real state. They tap again if they still want to toggle.
      delete this._optimistic[this._entities.switch];
      this._update();
      return;
    }
    const isOn = serverIsOn !== null ? serverIsOn : cachedIsOn;
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
      this._setLoadingOverlay(true, this._streamPhaseText());
      // Keepalive ticks — each _setLoadingOverlay resets the 15s safety timeout,
      // so ticks must be <15s apart to keep the spinner alive (LOCAL streams can
      // take up to 60s on first connect). Text comes from the SHARED stream_status
      // (_streamPhaseText) so the clicking browser shows the SAME message as every
      // other session, instead of its own local progression. 2026-05-30.
      this._connectSteps = [3000, 7000, 12000, 20000, 28000, 40000, 52000, 65000, 78000].map(
        (ms) => setTimeout(() => {
          if (this._streamConnecting) this._setLoadingOverlay(true, this._streamPhaseText());
        }, ms),
      );
    }
    // Stream toggle: keep instant optimistic flip (don't use "pending" state —
    // would confuse _isStreaming() and break the snapshot/video-polling decision).
    // But DO wire in error rollback + red-flash feedback so a failed call visibly
    // reverts instead of silently timing out after 8s.
    const prevState = isOn ? "on" : "off";
    // Register DOM-id so _flashEntityError finds it even before first _update()
    this._entityToBtnId[this._entities.switch] = "btn-stream";
    this._hass?.callService("switch", isOn ? "turn_off" : "turn_on", { entity_id: this._entities.switch })
      .catch((err) => {
        console.warn("bosch-camera-card: stream toggle failed:", err);
        this._setOptimistic(this._entities.switch, prevState);
        // Also cancel any connect-overlay/timers if we were starting the stream
        if (!isOn) {
          this._streamConnecting = false;
          this._waitingForStream = false;
          if (this._connectSteps) { this._connectSteps.forEach(t => clearTimeout(t)); this._connectSteps = null; }
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
    // First explicit tap on Android clears the startup mute override.
    this._androidAudioMuted = false;
    // Apply mute/unmute to the live <video> HERE — synchronously, inside the
    // click's user-gesture context. This is the ONLY place Chrome permits
    // video.muted=false without pausing the element. _update() must not unmute
    // (it runs on every hass push, with no gesture → "Unmuting failed").
    if (this._liveVideoActive) {
      const video = this.shadowRoot.getElementById("cam-video");
      if (video) {
        video.muted = !turningOn;
        if (turningOn && video.paused) video.play().catch(() => {});
      }
    }
    // Optimistic update drives the Ton icon; _update() only ever re-mutes.
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

  /**
   * Like _toggleSwitch but routes through _callServiceWithRollback so the UI
   * shows a "pending" state during the call and reverts + flashes red on error.
   * Used for the main user-facing toggles (Privacy, Licht).
   */
  _toggleSwitchWithRollback(entityId) {
    if (!this._hass || !entityId) return;
    const state = this._hass.states[entityId]?.state;
    if (!state || state === "unavailable" || state === "unknown") return;
    const turningOn = state !== "on";
    const prev = turningOn ? "off" : "on";
    const target = turningOn ? "on" : "off";
    this._callServiceWithRollback(
      entityId, prev, target,
      "switch", turningOn ? "turn_on" : "turn_off",
      { entity_id: entityId }
    );
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
    // iOS Safari / HA Companion: native Fullscreen API drags the <video>
    // into the system AVPlayerViewController which (a) auto-hides any HTML
    // overlay after ~10s of inactivity (the user's pill-bar vanishes) and
    // (b) clamps controls visibility outside our DOM. Use the CSS-fallback
    // path on iOS so the apple-style overlays stay visible the whole time.
    const ua = navigator.userAgent || "";
    const isIOS = /iPhone|iPod|iPad/i.test(ua)
                  || (/Macintosh/i.test(ua) && (navigator.maxTouchPoints || 0) > 1);
    if (isIOS) {
      this._enterCssFullscreen();
      return;
    }
    // Desktop / Android: native API works without the auto-hide issue.
    const wrapper = this.shadowRoot.getElementById("img-wrapper");
    // Toggle: if we are already in native fullscreen, the button exits it
    // instead of re-requesting (issue #16 — clicking the button again exits).
    if (this._isNativeFullscreen()) {
      if (document.exitFullscreen)       return document.exitFullscreen();
      if (document.webkitExitFullscreen) return document.webkitExitFullscreen();
      if (document.mozCancelFullScreen)  return document.mozCancelFullScreen();
      if (document.msExitFullscreen)     return document.msExitFullscreen();
      return;
    }
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
    this._updateFullscreenButtonState();
    document.body.style.overflow = "hidden";
    // Tap anywhere outside the image to exit
    this._fsClickOut = (e) => { if (!this.contains(e.target)) this._exitCssFullscreen(); };
    // Press Escape to exit
    this._fsKeyDown = (e) => { if (e.key === "Escape") this._exitCssFullscreen(); };
    setTimeout(() => {
      // pointerup, not click: in the mobile WebView a tap outside the card
      // (to exit CSS-fullscreen) may not reach `document` as a click. 2026-05-29.
      document.addEventListener("pointerup", this._fsClickOut);
      document.addEventListener("keydown", this._fsKeyDown);
    }, 100);
  }

  _exitCssFullscreen() {
    this.classList.remove("fs-active");
    this._updateFullscreenButtonState();
    document.body.style.overflow = "";
    if (this._fsClickOut) { document.removeEventListener("pointerup", this._fsClickOut); this._fsClickOut = null; }
    if (this._fsKeyDown)  { document.removeEventListener("keydown", this._fsKeyDown);  this._fsKeyDown  = null; }
  }

  _syncMoreButton() {
    // Keep the ⋮ (Mehr) button's pressed state + aria in sync with whether the
    // control stack is expanded (.overflow-open on host).
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
    // We call requestFullscreen() on #img-wrapper, which lives inside this
    // card's shadow root. Per the Fullscreen spec, document.fullscreenElement
    // is RETARGETED to the shadow host (=== this), NOT the inner wrapper — so
    // a `document.fullscreenElement === wrapper` check is always false and the
    // exit branch never fired (issue #16: button opened fullscreen but could
    // not close it). The reliable signals are shadowRoot.fullscreenElement
    // (the real inner element) and document.fullscreenElement === this (host).
    const sr = this.shadowRoot;
    const shadowFs = sr && sr.fullscreenElement;
    const docFs = document.fullscreenElement
               || document.webkitFullscreenElement
               || document.mozFullScreenElement
               || document.msFullscreenElement;
    return !!shadowFs || docFs === this;
  }

  _updateFullscreenButtonState() {
    const btn = this.shadowRoot?.getElementById("ap-btn-fullscreen");
    if (!btn) return;
    const wrapper = this.shadowRoot?.getElementById("img-wrapper");
    // Native fullscreen API: shadow-DOM-aware detection (see _isNativeFullscreen
    // — document.fullscreenElement retargets to the host, not the wrapper).
    // CSS-fullscreen fallback: host carries the .fs-active class. Either signal
    // flips the button to active.
    const nativeFs = this._isNativeFullscreen() || !!(document.fullscreenElement === wrapper
                       || document.webkitFullscreenElement === wrapper
                       || document.mozFullScreenElement === wrapper
                       || document.msFullscreenElement === wrapper);
    const cssFs = this.classList.contains("fs-active");
    const active = nativeFs || cssFs;
    btn.classList.toggle("on", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
    btn.setAttribute("title", active ? "Vollbild verlassen" : "Vollbild");
  }

  _callService(domain, service, data) {
    if (!this._hass) return;
    this._hass.callService(domain, service, data).catch((err) =>
      console.warn("bosch-camera-card:", domain, service, err)
    );
  }

  /**
   * Service call with optimistic "pending" state + error rollback.
   *
   * Flow:
   *   1. Mark entity as "pending" (visually faded+pulsing, see CSS).
   *   2. Fire hass.callService.
   *   3. On success → set optimistic state to targetState (on/off). HA sync in
   *      _update() then clears it once the real state catches up.
   *   4. On failure → revert optimistic state to prevState + flash error class
   *      for ~2s on the DOM element registered in _entityToBtnId.
   *
   * This path is used for the main toggles (Privacy, Light, Stream). Other
   * switches continue to use the original _toggleSwitch → _setOptimistic path
   * which has been good enough in practice.
   */
  _callServiceWithRollback(entityId, prevState, targetState, domain, service, data) {
    if (!this._hass) return;
    // Mark pending but remember the target so CSS pulse and UI know what's coming
    this._setOptimistic(entityId, "pending");
    this._hass.callService(domain, service, data).then(() => {
      // Success: flip optimistic to target; _update() will clear once HA confirms
      this._setOptimistic(entityId, targetState);
    }).catch((err) => {
      console.warn("bosch-camera-card:", domain, service, err);
      // Failure: revert to prev state and show short error feedback
      this._setOptimistic(entityId, prevState);
      this._flashEntityError(entityId);
    });
  }

  _flashEntityError(entityId) {
    const domId = this._entityToBtnId[entityId];
    if (!domId) { this._update(); return; }
    const el = this.shadowRoot.getElementById(domId);
    if (!el) return;
    el.classList.add("error");
    if (this._errorFeedbackTimers[entityId]) clearTimeout(this._errorFeedbackTimers[entityId]);
    this._errorFeedbackTimers[entityId] = setTimeout(() => {
      el.classList.remove("error");
      delete this._errorFeedbackTimers[entityId];
    }, 2000);
  }

  _formatDatetime(d) {
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  static getStubConfig(hass) {
    // Pick the first real Bosch camera from THIS install (issue #17 — never
    // hardcode a specific entity; "camera.bosch_garten" only exists in the
    // author's setup, so every freshly-added card was stuck on it). Falls back
    // to the first camera.* entity, then to an empty string the editor flags.
    const states = (hass && hass.states) || {};
    const ids = Object.keys(states).filter((id) => id.startsWith("camera."));
    const bosch = ids.find(
      (id) => id.includes("bosch")
        || (states[id]?.attributes?.brand || "").toLowerCase().includes("bosch")
    );
    return { camera_entity: bosch || ids[0] || "" };
  }
  static getConfigElement() { return document.createElement("bosch-camera-card-editor"); }
  getCardSize() { return 4; }
}

customElements.define("bosch-camera-card", BoschCameraCard);

/* ──────────────────────────────────────────────────────────────────────────
 * Visual editor for `custom:bosch-camera-card`. Shipped in v13 to match the
 * baseline community expectation set by Mushroom — most popular HA cards
 * expose a visual editor in the dashboard card-picker. Covers the highest-
 * impact fields (camera_entity, title, apple_style, theme, mode, minimal,
 * compact, auto_play); rarer per-entity overrides stay YAML-only. Native
 * <input>/<select> elements with HA CSS variables for theming — no ha-form
 * dependency so the editor works in HA 2024.8+ without any extra imports.
 * ────────────────────────────────────────────────────────────────────────── */
class BoschCameraCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    if (this.shadowRoot) this._render();
  }
  set hass(hass) { this._hass = hass; if (this.shadowRoot) this._render(); }
  get hass() { return this._hass; }
  connectedCallback() { this._render(); }

  _bosch_cameras() {
    const out = [];
    const states = this._hass?.states || {};
    for (const id of Object.keys(states)) {
      if (id.startsWith("camera.")
          && (id.includes("bosch") || (states[id]?.attributes?.brand || "").toLowerCase().includes("bosch"))) {
        out.push(id);
      }
    }
    // Fallback: if no Bosch-tagged camera is detected, list every camera.*
    // entity so the picker is never empty/single-option (issue #17 — entity
    // ids without "bosch" and no brand attribute left the dropdown stuck).
    if (out.length === 0) {
      for (const id of Object.keys(states)) {
        if (id.startsWith("camera.")) out.push(id);
      }
    }
    return out.sort();
  }

  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const cfg = this._config || {};
    const cams = this._bosch_cameras();
    // Keep the configured entity in the option list even if discovery missed
    // it, so it stays the selected value rather than silently snapping to the
    // first option without firing a change (issue #17).
    if (cfg.camera_entity && !cams.includes(cfg.camera_entity)) cams.push(cfg.camera_entity);
    cams.sort();
    const sel = (name, val, opts) => `
      <label>${name}
        <select name="${name.toLowerCase().replace(/\W/g, "")}">
          ${opts.map(([v,l]) => `<option value="${v}" ${val === v ? "selected" : ""}>${l}</option>`).join("")}
        </select>
      </label>`;
    const chk = (key, label, def) => `
      <label class="inline">
        <input type="checkbox" name="${key}" ${(cfg[key] ?? def) ? "checked" : ""} />
        <span>${label}</span>
      </label>`;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        .row { display: flex; flex-direction: column; gap: 14px; padding: 18px; }
        label {
          font-size: 14px; color: var(--primary-text-color);
          display: flex; flex-direction: column; gap: 4px;
        }
        label.inline { flex-direction: row; align-items: center; gap: 10px; }
        select, input[type="text"] {
          padding: 9px 10px; border-radius: 6px;
          border: 1px solid var(--divider-color, rgba(120,120,128,.2));
          background: var(--card-background-color, #fff);
          color: var(--primary-text-color, #1c1c1e);
          font: inherit; font-size: 14px;
        }
        select:focus, input:focus { outline: 2px solid #0a84ff; outline-offset: 1px; }
        input[type="checkbox"] { width: 18px; height: 18px; accent-color: #0a84ff; }
        .hint {
          font-size: 12px; color: var(--secondary-text-color, #6c6c70);
          margin-top: 2px;
        }
        h4 {
          margin: 12px 0 0; font-size: 11px; font-weight: 700;
          letter-spacing: .08em; text-transform: uppercase;
          color: var(--secondary-text-color, #6c6c70);
        }
        .help {
          font-size: 12px;
          color: var(--secondary-text-color, #6c6c70);
          background: var(--secondary-background-color, rgba(120,120,128,.08));
          padding: 8px 10px; border-radius: 6px;
        }
      </style>
      <div class="row">
        ${cams.length === 0 ? `
          <div class="help">Keine Bosch-Kameras erkannt. Trage <code>camera.bosch_xxx</code> manuell ein, oder schließe das Bosch-Integration-Setup zuerst ab.</div>
        ` : ""}
        <label>Kamera-Entity *
          <select name="camera_entity">
            ${cams.length ? cams.map(id => `<option value="${id}" ${cfg.camera_entity === id ? "selected" : ""}>${id}</option>`).join("") : `<option value="${cfg.camera_entity || ""}" selected>${cfg.camera_entity || "(noch nicht gesetzt)"}</option>`}
          </select>
          <span class="hint">Pflichtfeld — alle anderen Entities werden automatisch aus dem Camera-Namen abgeleitet.</span>
        </label>
        <label>Titel <small style="color:var(--secondary-text-color)">(optional, überschreibt Friendly-Name)</small>
          <input type="text" name="title" value="${(cfg.title || "").replace(/"/g, "&quot;")}" placeholder="z.B. Garten" />
        </label>

        <h4>Design</h4>
        ${chk("apple_style", "Apple-Style Glass-Overlay aktiv (Default an)", true)}
        ${sel("Theme", cfg.theme || "ios", [["auto","Auto (Auto-Detect via User-Agent)"],["ios","iOS (Apple Home)"],["android","Android (Material You)"]])}
        ${sel("Modus", cfg.mode || "auto", [["auto","Auto (System Light/Dark)"],["day","Tag"],["night","Nacht"]])}
        ${chk("minimal", "Minimal-Layout (Mehr-Menü versteckt zunächst alle Switches)", false)}
        ${chk("compact", "Compact-Tile (für Overview-Grid: nur Video + Title-Pill, keine Pill-Bar)", false)}
        ${chk("show_title", "Titel-Pill anzeigen (aus = nur Video, ohne Namens-Overlay)", true)}
        ${chk("show_last_event", "Letztes-Ereignis-Badge anzeigen", true)}

        <h4>Auto-Play</h4>
        ${sel("Auto-Play", cfg.auto_play || "lan", [["lan","LAN (Auto-Start nur im Heimnetz)"],["always","Immer"],["never","Nie (Tap-to-Play Gate)"]])}
        <span class="hint">Steuert wann der Live-Stream automatisch loslegt. Überschreibt die Integration-weite Voreinstellung.</span>
      </div>`;
    const root = this.shadowRoot;
    const fire = (patch) => {
      this._config = { ...this._config, ...patch };
      this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: this._config }, bubbles: true, composed: true }));
    };
    root.querySelector('select[name="camera_entity"]').addEventListener("change", e => fire({ camera_entity: e.target.value }));
    root.querySelector('input[name="title"]').addEventListener("change", e => fire({ title: e.target.value || undefined }));
    root.querySelector('input[name="apple_style"]').addEventListener("change", e => fire({ apple_style: e.target.checked }));
    root.querySelector('select[name="theme"]').addEventListener("change", e => fire({ theme: e.target.value }));
    root.querySelector('select[name="modus"]').addEventListener("change", e => fire({ mode: e.target.value }));
    root.querySelector('input[name="minimal"]').addEventListener("change", e => fire({ minimal: e.target.checked }));
    root.querySelector('input[name="compact"]').addEventListener("change", e => fire({ compact: e.target.checked }));
    root.querySelector('input[name="show_title"]').addEventListener("change", e => fire({ show_title: e.target.checked }));
    root.querySelector('input[name="show_last_event"]').addEventListener("change", e => fire({ show_last_event: e.target.checked }));
    root.querySelector('select[name="autoplay"]').addEventListener("change", e => fire({ auto_play: e.target.value }));
  }
}
customElements.define("bosch-camera-card-editor", BoschCameraCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type:        "bosch-camera-card",
  name:        "Bosch Camera Card",
  description: "Bosch Smart Home cameras with streaming state, loading indicator and controls",
  preview:     false,
});


// ─────────────────────────────────────────────────────────────────────────────
// Bosch Camera Overview Card — single wrapper that auto-discovers all Bosch
// cameras via attributes.brand === "Bosch" and renders one <bosch-camera-card>
// per camera in a responsive CSS grid. Online cameras first, offline after.
// Narrow viewports collapse to a single column; wide viewports fit multiple.
//
// YAML (minimal — this is the whole config):
//   type: custom:bosch-camera-overview-card
//   online_offline_view: true     # show offline cameras too (default true)
//   min_width: "360px"            # optional — grid cell min width
//   title: "Meine Kameras"        # optional — header above the grid
//   exclude: []                   # optional — entity_ids to skip
//   include: []                   # optional — override auto-discovery
//   use_bosch_sort: true          # optional — order each tier (live/privacy/
//                                 #   offline) by the Bosch-app priority
//                                 #   (`bosch_priority` attribute on each cam,
//                                 #   mirror of GET /v11/video_inputs.priority).
//                                 #   Default false: alphabetic ordering.
// ─────────────────────────────────────────────────────────────────────────────
const OVERVIEW_VERSION = "1.3.0";

class BoschCameraOverviewCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._cards     = new Map();   // entity_id -> <bosch-camera-card>
    this._lastSig   = "";          // discovery signature, avoids needless re-sort
    this._config    = null;
    this._hass      = null;
    this._rendered  = false;
    this._emptyNode = null;        // the empty-state <div>, tracked so re-sort removes it without touching cells
  }

  setConfig(config) {
    this._config = {
      online_offline_view: config.online_offline_view !== false,
      title:     config.title     || "",
      min_width: config.min_width || "650px",
      gap:       config.gap       || "12px",
      columns:   config.columns   ?? "auto",  // "auto" | 1 | 2 | 3 | 4
      exclude:   Array.isArray(config.exclude) ? config.exclude : [],
      include:   Array.isArray(config.include) ? config.include : [],
      // When true, sort cameras inside each tier (live → privacy → offline)
      // by the Bosch-app priority instead of alphabetically. Priority is
      // read from the `bosch_priority` attribute that the camera entity
      // exposes (mirror of GET /v11/video_inputs.priority). Cameras
      // without a priority value fall back to alphabetic at the end of
      // their tier so foreign / non-Bosch include entries don't disappear.
      use_bosch_sort: config.use_bosch_sort === true,
      // Grid tiles default to the minimal layout (switches behind the ⋮ menu)
      // so the overview stays glanceable — standalone single cards expand by
      // default, but a grid of fully-expanded cards is unusable (2026-05-29).
      // Default ON; set `minimal: false` to expand every tile. Per-camera
      // overrides still win via `overrides.<entity>.minimal`. Folded into
      // card_defaults so the child-card setConfig receives it via the same
      // merge path as any other default.
      minimal:   config.minimal !== false,
      // v13.0.0: top-level apple_style toggle (default true) + theme
      // selector ("ios" | "android" | "auto", default "ios"). Both flow
      // into card_defaults so every child card picks them up. Set
      // apple_style: false to opt back to legacy chrome on the whole
      // overview. Theme "auto" delegates to user-agent detection inside
      // each child card; the in-card theme switcher overrides this via
      // localStorage globally.
      apple_style:          config.apple_style !== false,
      theme:                ["ios", "android", "auto"].includes(config.theme) ? config.theme : "ios",
      mode:                 ["auto", "day", "night"].includes(config.mode) ? config.mode : "auto",
      // v13.1.0: compact tile mode propagates to children — hides pill-bar
      // + status badge so each tile is just video + title-pill (Apple-Home
      // grid style). Pair with `columns: 2` or `columns: 3` for true 2x2/3x3.
      compact:              config.compact === true,
      // Element-hiding toggles propagated to every tile (issue #15 parity):
      // show_title:false / show_last_event:false strip the title pill / badge.
      show_title:           config.show_title !== false,
      show_last_event:      config.show_last_event !== false,
      overrides: (config.overrides && typeof config.overrides === "object") ? config.overrides : {},
      card_defaults: (config.card_defaults && typeof config.card_defaults === "object") ? config.card_defaults : {},
    };
    if (this._config.minimal) {
      this._config.card_defaults = { ...this._config.card_defaults, minimal: true };
    }
    // Propagate apple_style + theme + mode + compact + hide-toggles to every
    // child card unless an explicit per-key override already exists in
    // card_defaults.
    this._config.card_defaults = {
      apple_style: this._config.apple_style,
      theme: this._config.theme,
      mode: this._config.mode,
      compact: this._config.compact,
      show_title: this._config.show_title,
      show_last_event: this._config.show_last_event,
      ...this._config.card_defaults,
    };
    // Apple-style class on overview host gates the CSS that drops the
    // saturated tier borders (green/orange/grey) — when on, the tier info is
    // already conveyed via the inner card's glass status dot + badge.
    this.classList.toggle("apple-style", this._config.apple_style);
    this._rendered = false;
    this._lastSig  = "";
    this._cards.clear();
    if (this.shadowRoot) this.shadowRoot.innerHTML = "";
    if (this._hass) this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }
  get hass() { return this._hass; }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        .bco-wrap { display: block; padding: 4px; overflow: visible; }
        .bco-header {
          display: flex; align-items: center; justify-content: space-between;
          padding: 0 4px 8px; font-size: 14px; font-weight: 500;
          color: var(--primary-text-color);
        }
        .bco-count {
          font-size: 12px; font-weight: 400;
          color: var(--secondary-text-color);
        }
        .bco-grid {
          display: grid;
          gap: ${this._config.gap};
          grid-template-columns: ${
            this._config.columns === "auto" || !this._config.columns
              ? `repeat(auto-fill, minmax(min(${this._config.min_width}, 100%), 1fr))`
              : `repeat(${Number(this._config.columns)}, minmax(0, 1fr))`
          };
        }
        @media (max-width: 640px) {
          .bco-grid { grid-template-columns: 1fr !important; }
        }
        /* Phones in landscape (e.g. iPhone Pro Max ≈ 932 × 430) are wider
           than 640px but the viewport height collapses below ~500px — at
           that aspect a 2-column tile grid leaves each tile ~12 lines tall
           which is unusable. Force single column when any of:
             - touch device up to small-tablet width (1024px), or
             - landscape with very short viewport (any device).
           Desktop browsers resized narrow keep their multi-column layout. */
        @media (pointer: coarse) and (max-width: 1024px) {
          .bco-grid { grid-template-columns: 1fr !important; }
        }
        @media (orientation: landscape) and (max-height: 500px) {
          .bco-grid { grid-template-columns: 1fr !important; }
        }
        .bco-cell {
          min-width: 0;
          position: relative;
          border-radius: 14px;
          border: 2px solid transparent;
          overflow: hidden;
          transition: border-color 0.2s ease;
        }
        .bco-cell[data-tier="0"] { border-color: rgba(76, 175, 80, 0.55); }
        .bco-cell[data-tier="1"] { border-color: rgba(255, 152, 0, 0.55); }
        .bco-cell[data-tier="2"] { border-color: rgba(120, 120, 120, 0.35); opacity: 0.92; }
        /* Apple-style: drop the saturated tier borders + opacity dim. Tier
           info already shows in the inner card's glass status dot + badge,
           so the wrapping border just adds visual noise that clashes with
           the soft Apple aesthetic. The cell still gets a generous border
           radius so corner cropping matches the inner card. */
        :host(.apple-style) .bco-cell,
        :host(.apple-style) .bco-cell[data-tier="0"],
        :host(.apple-style) .bco-cell[data-tier="1"],
        :host(.apple-style) .bco-cell[data-tier="2"] {
          border: 0;
          border-radius: var(--bosch-card-radius, 22px);
          opacity: 1;
          /* Smooth scale + shadow on hover so desktop users get a clear
             "this tile is tappable" affordance. Touch devices ignore :hover
             so the static state stays unchanged on mobile. */
          transition: transform .18s ease, box-shadow .18s ease;
        }
        @media (hover: hover) and (pointer: fine) {
          :host(.apple-style) .bco-cell:hover {
            /* translateY only — no scale(). Scaling a variable-height tile
               shifts its top edge when the inner card expands via ⋮, which
               reads as the tile "jumping" (issue #15.3). translateY is
               height-independent, so the lift affordance stays jump-free. */
            transform: translateY(-2px);
            box-shadow: 0 12px 32px rgba(0,0,0,.18);
            z-index: 1;
          }
        }
        .bco-cell bosch-camera-card { display: block; min-width: 0; }
        .bco-section {
          grid-column: 1 / -1;
          font-size: 11px;
          font-weight: 600;
          letter-spacing: 0.08em;
          text-transform: uppercase;
          color: var(--secondary-text-color);
          padding: 8px 4px 2px;
          border-top: 1px solid var(--divider-color, rgba(255,255,255,0.1));
          margin-top: 4px;
        }
        .bco-section.first { border-top: none; margin-top: 0; padding-top: 2px; }
        .bco-empty {
          grid-column: 1 / -1;
          padding: 24px 12px;
          text-align: center;
          color: var(--secondary-text-color);
          font-size: 14px;
        }
        .bco-empty.bco-empty-outage {
          padding: 24px 16px;
          color: var(--primary-text-color);
        }
        .bco-empty-title {
          font-size: 15px;
          font-weight: 500;
          margin-bottom: 6px;
        }
        .bco-empty-sub {
          font-size: 13px;
          color: var(--secondary-text-color);
          margin-top: 4px;
        }
        .bco-empty-link {
          display: inline-block;
          margin-top: 10px;
          color: var(--primary-color);
          text-decoration: none;
          font-size: 13px;
        }
        .bco-empty-link:hover { text-decoration: underline; }
        .bco-banner {
          display: flex;
          flex-direction: column;
          gap: 4px;
          padding: 10px 12px;
          margin-bottom: 8px;
          border-radius: 8px;
          background: var(--warning-color, #ffc107);
          color: #000;
          font-size: 13px;
          line-height: 1.35;
        }
        .bco-banner.bco-banner-info {
          background: var(--info-color, var(--primary-color));
          color: var(--text-primary-color, #fff);
        }
        .bco-banner-title { font-weight: 600; }
        .bco-banner a {
          color: inherit;
          text-decoration: underline;
          font-size: 12px;
        }
        .bco-lan-tiles {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
          gap: 8px;
          margin-bottom: 10px;
        }
        .bco-lan-tile {
          display: flex;
          flex-direction: column;
          gap: 6px;
          padding: 10px 12px;
          border-radius: 8px;
          background: var(--card-background-color, #1c1c1c);
          border: 1px solid var(--divider-color, rgba(255,255,255,0.1));
          font-size: 13px;
        }
        .bco-lan-tile-header {
          display: flex;
          align-items: center;
          gap: 8px;
          font-weight: 600;
        }
        .bco-lan-dot {
          width: 10px;
          height: 10px;
          border-radius: 50%;
          background: var(--state-inactive-color, #888);
          flex-shrink: 0;
        }
        .bco-lan-dot.bco-lan-on { background: var(--success-color, #4caf50); }
        .bco-lan-dot.bco-lan-off { background: var(--error-color, #f44336); }
        .bco-lan-controls {
          display: flex;
          gap: 6px;
          flex-wrap: wrap;
        }
        .bco-lan-btn {
          flex: 1 1 auto;
          padding: 6px 10px;
          border-radius: 6px;
          border: 1px solid var(--divider-color, rgba(255,255,255,0.1));
          background: var(--secondary-background-color, #2c2c2c);
          color: var(--primary-text-color, #fff);
          font-size: 12px;
          cursor: pointer;
          white-space: nowrap;
        }
        .bco-lan-btn:hover:not(:disabled) {
          background: var(--primary-color);
          color: var(--text-primary-color, #fff);
        }
        .bco-lan-btn:disabled {
          opacity: 0.4;
          cursor: not-allowed;
        }
        .bco-lan-btn.bco-lan-btn-on {
          background: var(--state-active-color, var(--primary-color));
          color: var(--text-primary-color, #fff);
        }
        bosch-camera-card { display: block; }
        @media (max-width: 480px) {
          .bco-grid { gap: 8px; }
        }
      </style>
      <div class="bco-wrap">
        ${this._config.title ? `
          <div class="bco-header">
            <span>${this._escape(this._config.title)}</span>
            <span class="bco-count" id="bco-count"></span>
          </div>` : ""}
        <div id="bco-banner-slot"></div>
        <div id="bco-lan-tiles-slot"></div>
        <div class="bco-grid" id="bco-grid"></div>
      </div>
    `;
    this._grid = this.shadowRoot.getElementById("bco-grid");
    this._countEl = this.shadowRoot.getElementById("bco-count");
    this._bannerSlot = this.shadowRoot.getElementById("bco-banner-slot");
    this._lanTilesSlot = this.shadowRoot.getElementById("bco-lan-tiles-slot");
    this._rendered = true;
  }

  _escape(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  _renderLanTiles() {
    // Per-camera "what still works locally" panel — shown when one or more
    // Bosch cameras are unavailable (typical during a cloud 5xx outage) AND
    // at least one of them is reachable on LAN (or has an unknown LAN state
    // we still want to surface). Tiles disappear automatically once the
    // cloud is back and cards render normally.
    if (!this._lanTilesSlot) return;
    const states = this._hass?.states || {};
    const unavailableBosch = Object.keys(states).filter(
      (eid) => eid.startsWith("camera.bosch_") && states[eid]?.state === "unavailable",
    );
    if (unavailableBosch.length === 0) {
      if (this._lanTilesSlot.firstChild) this._lanTilesSlot.innerHTML = "";
      this._lanTilesSlot.dataset.sig = "";
      return;
    }
    // Build a fallback friendly-name → entity index for the case where
    // entity_id slugs do not match the camera entity_id (cloud-degraded
    // first-setup registers LAN sensors with the cam UUID instead of the
    // friendly slug, see v12.4.10 setup_entry fallback). Match by
    // friendly_name prefix so a renamed camera still finds its sensors.
    const tiles = [];
    for (const camEid of unavailableBosch) {
      const slug = camEid.replace(/^camera\.bosch_/, "");
      const camFriendly = (states[camEid]?.attributes?.friendly_name) || `Bosch ${slug}`;
      const findByFriendlyPrefix = (domain, suffix) => {
        // Try entity_id slug match first (cheapest, works on healthy setup).
        const direct = states[`${domain}.bosch_${slug}${suffix.entityId}`];
        if (direct) return direct;
        // Fall back to friendly-name prefix match.
        const target = `${camFriendly} ${suffix.friendly}`.toLowerCase();
        return Object.values(states).find((s) => {
          if (!s.entity_id.startsWith(`${domain}.`)) return false;
          const fn = (s.attributes?.friendly_name || "").toLowerCase();
          return fn === target || fn.startsWith(target);
        });
      };
      const lan = findByFriendlyPrefix("binary_sensor", { entityId: "_lan_reachable", friendly: "LAN" });
      const privacy = findByFriendlyPrefix("switch", { entityId: "_privacy_mode", friendly: "Privacy Mode" });
      const light = findByFriendlyPrefix("light", { entityId: "_front_light", friendly: "Front Light" });
      tiles.push({ camEid, slug, friendly: camFriendly, lan, privacy, light });
    }
    const sig = tiles.map((t) =>
      `${t.slug}|${t.lan?.state}|${t.privacy?.state}|${t.privacy?.attributes?.icon}|${t.light?.state}`,
    ).join("#");
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
      if (lanState === "on") dot.classList.add("bco-lan-on");
      else if (lanState === "off") dot.classList.add("bco-lan-off");
      header.appendChild(dot);
      const nameEl = document.createElement("span");
      nameEl.textContent = t.friendly.replace(/^Bosch\s+/, "");
      header.appendChild(nameEl);
      tile.appendChild(header);
      const status = document.createElement("div");
      status.style.cssText = "font-size:11px;color:var(--secondary-text-color);";
      status.textContent = lanState === "on" ? "LAN erreichbar"
        : lanState === "off" ? "LAN nicht erreichbar"
        : "LAN-Status unbekannt";
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
        btn.title = !reachable ? "Kamera lokal nicht erreichbar"
                  : !entOk ? "Status unbekannt — Cloud-Daten fehlen"
                  : `${label} ${isOn ? "AUS" : "AN"} schalten`;
        btn.textContent = `${label}${isOn ? " AN" : ""}`;
        if (entity) {
          btn.addEventListener("click", () => {
            this._hass.callService(domain, "toggle", { entity_id: entity.entity_id });
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
    const maint = Object.values(states).find((s) => {
      const src = s?.attributes?.source;
      return typeof src === "string" && /^(rss|html):/.test(src);
    });
    const mState = maint?.state || "";
    const mAttr  = maint?.attributes || {};
    const show = (mState === "active" || mState === "scheduled") && mAttr.camera_relevant;
    if (!show) {
      if (this._bannerSlot.firstChild) this._bannerSlot.innerHTML = "";
      this._bannerSlot.dataset.sig = "";
      return;
    }
    const isActive = mState === "active";
    const win = this._formatWindow(mAttr.scheduled_start, mAttr.scheduled_end);
    // Avoid re-rendering identical banner each tick.
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
    sub.textContent = win
      ? `${mAttr.title || "Wartungsmeldung"} · ${win}`
      : (mAttr.title || "Wartungsmeldung");
    banner.appendChild(t);
    banner.appendChild(sub);
    if (isActive) {
      const note = document.createElement("div");
      note.textContent = "Live-Bild und Snapshots können in diesem Zeitfenster eingeschränkt sein.";
      banner.appendChild(note);
    }
    // Validate the URL scheme before assigning it to `href`. A compromised
    // Bosch RSS feed or any sensor-state-write path could otherwise inject
    // `javascript:` / `data:` URIs that execute in the dashboard context.
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
    // Render the maintenance window in Berlin local time for the empty-state
    // banner. Returns "" if either bound is missing or unparseable so the
    // caller can fall back to a title-only line.
    if (!startIso || !endIso) return "";
    try {
      const s = new Date(startIso);
      const e = new Date(endIso);
      if (isNaN(s) || isNaN(e)) return "";
      const date = s.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" });
      const fmt = (d) => d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
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

    const candidates = explicit
      ? this._config.include
      : Object.keys(states).filter((eid) => eid.startsWith("camera."));

    for (const eid of candidates) {
      if (this._config.exclude.includes(eid)) continue;
      const s = states[eid];
      if (!s) continue;
      const a = s.attributes || {};
      if (!explicit && a.brand !== "Bosch") continue;
      const status = String(a.status || "").toUpperCase();
      const online = status === "ONLINE";
      const base   = eid.replace(/^camera\./, "");
      const privState = states[`switch.${base}_privacy_mode`];
      const privacyOn = !!(privState && String(privState.state).toLowerCase() === "on");
      // Active live stream → push to position 1 within its tier. Read the
      // switch state directly so we react instantly when the user flips
      // it (camera entity state.streaming attribute lags ~1 coordinator tick).
      const swState = states[`switch.${base}_live_stream`];
      const streamingOn = !!(swState && String(swState.state).toLowerCase() === "on");
      // tier: 0 = online active (privacy off), 1 = online but privacy on, 2 = offline
      const tier = !online ? 2 : (privacyOn ? 1 : 0);
      const rawPrio = a.bosch_priority;
      const priority = (typeof rawPrio === "number" && isFinite(rawPrio)) ? rawPrio : null;
      list.push({
        entity_id: eid,
        name:   a.friendly_name || eid,
        online,
        privacyOn,
        streamingOn,
        tier,
        priority,
        status: status || "UNKNOWN",
        model:  a.model_name || "",
      });
    }

    const useBosch = this._config.use_bosch_sort;
    list.sort((a, b) => {
      if (a.tier !== b.tier) return a.tier - b.tier;
      // Within the same tier: a camera whose live stream is currently ON
      // jumps to position 1. Common case: while watching one camera live,
      // its tile stays at the top of the grid even if the other tier-0
      // cams sort earlier alphabetically / by Bosch priority.
      if (a.streamingOn !== b.streamingOn) return a.streamingOn ? -1 : 1;
      if (useBosch) {
        // Bosch-app order. Cams without a priority value sort after
        // those with one inside the same tier; alphabetic fallback below.
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

    // Cloud-maintenance banner above the grid — sourced from
    // BoschCloudMaintenanceSensor (RSS feed). Independent of grid rendering so
    // it stays visible whether the user runs online_offline_view=true (offline
    // tiles shown) or =false (filtered down to an empty grid).
    this._renderMaintenanceBanner();
    // Local-fallback tiles — shown when one or more Bosch cameras are
    // unavailable AND we have a LAN reachability signal. Lets the user
    // toggle privacy / light directly via the LAN even though the cloud is
    // down. Self-clears once cards render normally again.
    this._renderLanTiles();

    let cams = this._discover();
    if (!this._config.online_offline_view) cams = cams.filter((c) => c.online);

    const sig = cams.map((c) => `${c.entity_id}:${c.tier}:${c.streamingOn ? "S" : ""}`).join("|");
    // Re-render also when the grid is empty (no cards, no empty-state) — this
    // covers the post-outage edge case where the prune loop emptied the grid
    // on a previous tick but the empty-state was never appended because the
    // sig hadn't changed. Without this, the user sees a blank panel forever
    // until they reload the page.
    const gridEmpty = this._grid && this._grid.children.length === 0;
    const needsReorder = sig !== this._lastSig || gridEmpty;
    this._lastSig = sig;

    // prune stale inner cards
    const keep = new Set(cams.map((c) => c.entity_id));
    for (const [eid, el] of [...this._cards.entries()]) {
      if (!keep.has(eid)) {
        el.remove();
        this._cards.delete(eid);
      }
    }

    // re-sort / insert
    if (needsReorder) {
      // Do NOT wipe the grid (innerHTML="") and re-append cells. Detaching an
      // inner <bosch-camera-card> fires its disconnectedCallback → _stopLiveVideo,
      // which tears down the WebRTC/HLS session of a camera whose OWN state never
      // changed. That was the privacy-toggle blip (2026-05-29): toggling one
      // camera's privacy flips its tier → `sig` changes → the grid re-sorted →
      // every other card's live stream dropped for ~1-2 s ("HLS wird geladen…").
      // Cells now stay in stable DOM order and are re-sorted via CSS `order`
      // below; only the (detached) empty-state node is removed here.
      if (this._emptyNode) { this._emptyNode.remove(); this._emptyNode = null; }
      if (cams.length === 0) {
        const empty = document.createElement("div");
        // Distinguish "no Bosch entities at all" vs. "all Bosch cameras are
        // unavailable" (cloud outage / maintenance — HA strips attributes from
        // unavailable entities so the brand!='Bosch' filter drops them).
        const states = this._hass?.states || {};
        const unavailableBosch = Object.keys(states).filter(
          (eid) => eid.startsWith("camera.bosch_") && states[eid]?.state === "unavailable",
        );
        if (unavailableBosch.length > 0) {
          empty.className = "bco-empty bco-empty-outage";
          // Look up the integration's maintenance sensor — surfaces the Bosch
          // community RSS announcement (title, link, scheduled_start/end).
          // Identified by attribute `source` matching /^(rss|html):/ which our
          // BoschCloudMaintenanceSensor sets exclusively.
          const maint = Object.values(states).find((s) => {
            const src = s?.attributes?.source;
            return typeof src === "string" && /^(rss|html):/.test(src);
          });
          const mState = maint?.state || "";
          const mAttr  = maint?.attributes || {};
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
            // Best case: Bosch announced exactly this window.
            const verb = mState === "active" ? "läuft" : (mState === "scheduled" ? "geplant" : "angekündigt");
            titleEl.textContent = `Bosch-Cloud-Wartung ${verb}`;
            const win = this._formatWindow(mAttr.scheduled_start, mAttr.scheduled_end);
            sub.textContent = win
              ? `${mAttr.title || "Wartungsmeldung"} · ${win}`
              : (mAttr.title || "Wartungsmeldung");
            // Only accept https:// — fall back to Bosch's service page if
            // the sensor's link attribute is missing or has a non-https scheme.
            link.href = (mAttr.link && /^https:\/\//i.test(mAttr.link))
              ? mAttr.link
              : "https://www.bosch-smarthome.com/service";
            link.textContent = "Details in der Bosch Community";
            sub2.textContent =
              `${unavailableBosch.length} ${unavailableBosch.length === 1 ? "Kamera" : "Kameras"} ` +
              "kommen automatisch zurück, sobald die Cloud antwortet.";
          } else {
            // Fallback: cameras unavailable but no matching RSS announcement —
            // either Bosch hasn't posted yet, RSS is unreachable, or this is
            // an unannounced outage. Stay honest about the uncertainty.
            titleEl.textContent = "Bosch-Cloud nicht erreichbar";
            sub.textContent =
              `${unavailableBosch.length} ${unavailableBosch.length === 1 ? "Kamera" : "Kameras"} ` +
              "warten auf die Bosch-Server.";
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
        // Cameras are already sorted by tier (0=live → 1=privat → 2=offline).
        // No full-width section dividers: they'd collapse the grid to 1 column
        // whenever a tier has an odd count. The per-card tier badge makes the
        // group visually obvious without breaking density.
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
                title:         c.name.replace(/^Bosch\s+/i, ""),
                ...override,
                // camera_entity set AFTER ...override so a per-camera override
                // can never repoint the tile to a different camera.
                camera_entity: c.entity_id,
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
          // Re-sort via CSS `order` only — NEVER re-append an existing cell.
          // A DOM move (remove+insert) fires the inner card's
          // disconnectedCallback → _stopLiveVideo, dropping the live stream of
          // a camera whose own state never changed (privacy-toggle blip,
          // 2026-05-29). Append a cell to the grid exactly once, on creation.
          cell.style.order = String(_ord++);
          if (cell.parentNode !== this._grid) this._grid.appendChild(cell);
        }
      }
    }

    // forward hass to every live inner card
    for (const cell of this._cards.values()) {
      const inner = cell._innerCard || cell.querySelector?.("bosch-camera-card");
      if (inner) inner.hass = this._hass;
    }

    // update count in header
    if (this._countEl) {
      const live = cams.filter((c) => c.tier === 0).length;
      const priv = cams.filter((c) => c.tier === 1).length;
      const off  = cams.filter((c) => c.tier === 2).length;
      const parts = [];
      if (live) parts.push(`${live} live`);
      if (priv) parts.push(`${priv} privat`);
      if (off)  parts.push(`${off} offline`);
      this._countEl.textContent = parts.join(" · ");
    }
  }

  static getStubConfig() {
    return { online_offline_view: true, title: "Bosch Kameras" };
  }
  static getConfigElement() {
    return document.createElement("bosch-camera-overview-card-editor");
  }
  getCardSize() { return Math.max(4, this._cards ? this._cards.size * 3 : 4); }
}

class BoschCameraOverviewCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    if (this.shadowRoot) this._render();
  }
  connectedCallback() { this._render(); }
  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const cfg = this._config || {};
    // eslint-disable-next-line eqeqeq
    const sel = v => (cfg.columns == v || (v === "auto" && (cfg.columns === "auto" || cfg.columns == null))) ? "selected" : "";
    const isAuto = cfg.columns === "auto" || cfg.columns == null;
    const minW = cfg.min_width || "650px";
    const minWpx = parseInt(minW) || 650;
    // Design/behaviour/hide sub-features mirror the single-card editor so both
    // cards expose the same options in the GUI (feature parity). These flow
    // into every tile via the overview card's card_defaults propagation.
    const seldd = (name, val, opts) => `
      <label>${name}
        <select name="${name.toLowerCase().replace(/\W/g, "")}">
          ${opts.map(([v,l]) => `<option value="${v}" ${val === v ? "selected" : ""}>${l}</option>`).join("")}
        </select>
      </label>`;
    const chk = (key, label, def) => `
      <label class="inline">
        <input type="checkbox" name="${key}" ${(cfg[key] ?? def) ? "checked" : ""} />
        <span>${label}</span>
      </label>`;
    this.shadowRoot.innerHTML = `
      <style>
        .row{display:flex;flex-direction:column;gap:12px;padding:16px}
        label{font-size:14px;color:var(--primary-text-color);display:flex;flex-direction:column;gap:4px}
        label.inline{flex-direction:row;align-items:center;gap:10px}
        select,input[type="text"],input[type="number"]{padding:8px;border-radius:4px;border:1px solid var(--divider-color);
          background:var(--card-background-color);color:var(--primary-text-color);font-size:14px}
        input[type="checkbox"]{width:18px;height:18px;accent-color:#0a84ff}
        .hint{font-size:12px;color:var(--secondary-text-color)}
        h4{margin:12px 0 0;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--secondary-text-color)}
        [hidden]{display:none}
      </style>
      <div class="row">
        <label>Spalten
          <select name="columns">
            <option value="auto" ${sel("auto")}>Auto (Breakpoint)</option>
            <option value="1" ${sel(1)}>1 – volle Breite</option>
            <option value="2" ${sel(2)}>2</option>
            <option value="3" ${sel(3)}>3</option>
            <option value="4" ${sel(4)}>4</option>
          </select>
        </label>
        <label id="minw-row" ${isAuto ? "" : "hidden"}>Breakpoint – Mindestbreite pro Kachel (px)
          <input type="number" name="min_width" value="${minWpx}" min="200" max="900" step="10" />
          <span class="hint">Bei Auto: 1 Spalte unter, 2+ Spalten über diesem Wert. Standard: 650 px</span>
        </label>
        <label>Titel <small style="color:var(--secondary-text-color)">(optional)</small>
          <input type="text" name="title" value="${(cfg.title || "").replace(/"/g, "&quot;")}" placeholder="Bosch Kameras" />
        </label>

        <h4>Anzeige</h4>
        ${chk("online_offline_view", "Offline-Kameras anzeigen", true)}
        ${chk("use_bosch_sort", "Nach Bosch-App-Reihenfolge sortieren", false)}

        <h4>Design (für alle Kacheln)</h4>
        ${chk("apple_style", "Apple-Style Glass-Overlay aktiv (Default an)", true)}
        ${seldd("Theme", cfg.theme || "ios", [["auto","Auto (User-Agent)"],["ios","iOS (Apple Home)"],["android","Android (Material You)"]])}
        ${seldd("Modus", cfg.mode || "auto", [["auto","Auto (System Light/Dark)"],["day","Tag"],["night","Nacht"]])}
        ${chk("compact", "Compact-Tile (nur Video + Title-Pill, keine Pill-Bar)", false)}
        ${chk("minimal", "Minimal-Layout (Switches hinter dem Mehr-Menü) — empfohlen fürs Grid", true)}
        ${chk("show_title", "Titel-Pill anzeigen (aus = nur Video, ohne Namens-Overlay)", true)}
        ${chk("show_last_event", "Letztes-Ereignis-Badge anzeigen", true)}
      </div>`;
    const colSel = this.shadowRoot.querySelector('select[name="columns"]');
    const minwRow = this.shadowRoot.getElementById("minw-row");
    colSel.addEventListener("change", e => {
      const v = e.target.value;
      minwRow.hidden = v !== "auto";
      this._fire({ ...this._config, columns: v === "auto" ? "auto" : Number(v) });
    });
    this.shadowRoot.querySelector('input[name="min_width"]').addEventListener("change", e => {
      const px = Math.max(200, Math.min(900, Number(e.target.value) || 360));
      this._fire({ ...this._config, min_width: `${px}px` });
    });
    this.shadowRoot.querySelector('input[name="title"]').addEventListener("change", e => {
      this._fire({ ...this._config, title: e.target.value });
    });
    const onChk = (name, key) => this.shadowRoot.querySelector(`input[name="${name}"]`)
      .addEventListener("change", e => this._fire({ ...this._config, [key]: e.target.checked }));
    onChk("online_offline_view", "online_offline_view");
    onChk("use_bosch_sort", "use_bosch_sort");
    onChk("apple_style", "apple_style");
    onChk("compact", "compact");
    onChk("minimal", "minimal");
    onChk("show_title", "show_title");
    onChk("show_last_event", "show_last_event");
    this.shadowRoot.querySelector('select[name="theme"]').addEventListener("change", e => this._fire({ ...this._config, theme: e.target.value }));
    this.shadowRoot.querySelector('select[name="modus"]').addEventListener("change", e => this._fire({ ...this._config, mode: e.target.value }));
  }
  _fire(config) {
    this._config = config;
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
  }
}
customElements.define("bosch-camera-overview-card-editor", BoschCameraOverviewCardEditor);

customElements.define("bosch-camera-overview-card", BoschCameraOverviewCard);

window.customCards.push({
  type:        "bosch-camera-overview-card",
  name:        "Bosch Camera Overview",
  description: "Auto-discovers all Bosch Smart Home cameras and renders them in a responsive grid (online first, offline after).",
  preview:     false,
});

// ── Phase 5: NVR Timeline Card ────────────────────────────────────────────────
// config: { camera_entity, nvr_source_id, motion_entity, show_date }
// nvr_source_id: media_source identifier for this camera's NVR folder,
//   e.g. "media-source://bosch_shc_camera/N/11111111.../2026-05-08"
// Renders a 24-hour canvas timeline + <video> player + date navigation.

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
    this._currentDate = new Date().toISOString().slice(0, 10);
    this._render();
    this._loadDay(this._currentDate);
    if (this._config.motion_entity) this._loadMotion(this._currentDate);
  }

  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;background:var(--card-background-color);border-radius:12px;overflow:hidden}
        .header{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;
          background:var(--primary-color);color:var(--text-primary-color);font-size:14px;font-weight:500}
        .nav-btn{background:none;border:none;color:inherit;cursor:pointer;font-size:18px;padding:4px 8px}
        .date-label{flex:1;text-align:center}
        canvas{width:100%;height:48px;display:block;cursor:pointer;background:#111}
        video{width:100%;max-height:340px;display:block;background:#000}
        .status{padding:8px 16px;font-size:12px;color:var(--secondary-text-color)}
        .no-data{padding:16px;text-align:center;color:var(--secondary-text-color)}
      </style>
      <div class="header">
        <button class="nav-btn" id="prev">&#8249;</button>
        <span class="date-label" id="date-lbl">${this._currentDate}</span>
        <button class="nav-btn" id="next">&#8250;</button>
      </div>
      <canvas id="timeline" height="48"></canvas>
      <video id="player" controls preload="none" playsinline></video>
      <div class="status" id="status">Lade Aufnahmen…</div>`;

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
    // Browse the per-camera per-date media source folder
    const camPart = this._config.nvr_source_id;
    const mediaId = camPart.replace(/\/\d{4}-\d{2}-\d{2}$/, "") + "/" + dateStr;
    try {
      const result = await this._hass.callWS({
        type: "media_source/browse_media",
        media_content_id: mediaId,
      });
      this._segments = (result.children || []).filter(c => c.media_class === "video");
      this._drawTimeline();
      const status = this.shadowRoot.getElementById("status");
      status.textContent = this._segments.length
        ? `${this._segments.length} Segment(e) — klicken zum Abspielen`
        : "Keine Aufnahmen für diesen Tag";
    } catch (err) {
      const status = this.shadowRoot.getElementById("status");
      status.textContent = "Fehler beim Laden der Segmente";
    }
  }

  async _loadMotion(dateStr) {
    if (!this._hass || !this._config.motion_entity) return;
    const start = dateStr + "T00:00:00+00:00";
    const end   = dateStr + "T23:59:59+00:00";
    try {
      const result = await this._hass.callApi(
        "GET",
        `history/period/${start}?end_time=${end}&filter_entity_id=${this._config.motion_entity}`,
      );
      const states = (result || [])[0] || [];
      this._motionEvents = states
        .filter(s => s.state === "on")
        .map(s => {
          const t = new Date(s.last_changed);
          return (t.getHours() * 3600 + t.getMinutes() * 60 + t.getSeconds()) / 86400;
        });
      this._drawTimeline();
    } catch (_) {
      // Motion overlay is best-effort; don't surface errors
    }
  }

  _drawTimeline() {
    const canvas = this.shadowRoot && this.shadowRoot.getElementById("timeline");
    if (!canvas) return;
    const W = canvas.offsetWidth || canvas.width || 600;
    canvas.width = W;
    const H = canvas.height;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, W, H);

    // Draw segments as green bars
    for (const seg of this._segments) {
      const pos = this._segmentTimeOffset(seg);
      if (pos === null) continue;
      const x = Math.floor(pos.start * W);
      const w = Math.max(2, Math.floor(pos.duration * W));
      ctx.fillStyle = "rgba(76,175,80,0.7)";
      ctx.fillRect(x, 2, w, H - 4);
    }

    // Motion ticks — red vertical marks
    ctx.fillStyle = "rgba(244,67,54,0.85)";
    for (const frac of this._motionEvents) {
      const x = Math.floor(frac * W);
      ctx.fillRect(x - 1, 0, 2, H);
    }

    // Hour grid
    ctx.strokeStyle = "rgba(255,255,255,0.15)";
    ctx.lineWidth = 1;
    for (let h = 1; h < 24; h++) {
      const x = Math.floor((h / 24) * W);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    }

    // Current time cursor from video
    const video = this.shadowRoot && this.shadowRoot.getElementById("player");
    if (video && !isNaN(video.duration) && video.currentTime > 0) {
      // Map video position onto today's timeline via segment start offset
      const activeSeg = this._activeSegment;
      if (activeSeg) {
        const pos = this._segmentTimeOffset(activeSeg);
        if (pos) {
          const frac = pos.start + (video.currentTime / 86400);
          const x = Math.floor(frac * W);
          ctx.strokeStyle = "#fff";
          ctx.lineWidth = 2;
          ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
        }
      }
    }
  }

  _segmentTimeOffset(seg) {
    // seg.title is expected to be "HH-MM.mp4" or similar; derive fractional day offset
    if (!seg.title) return null;
    const m = seg.title.match(/(\d{2})[:-](\d{2})/);
    if (!m) return null;
    const start = (parseInt(m[1]) * 60 + parseInt(m[2])) * 60 / 86400;
    const duration = 300 / 86400; // 5-min segments
    return { start, duration };
  }

  async _onCanvasClick(e) {
    const canvas = e.currentTarget;
    const frac = e.offsetX / canvas.offsetWidth;
    const offsetSeconds = Math.floor(frac * 86400);

    // Find the segment that contains this time offset
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
      if (delta < bestDelta) { bestDelta = delta; best = seg; }
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
        media_content_id: mediaContentId,
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
    if (this._cursorRaf) { cancelAnimationFrame(this._cursorRaf); this._cursorRaf = null; }
  }

  getCardSize() { return 4; }
}
customElements.define("bosch-nvr-timeline-card", BoschNvrTimelineCard);
window.customCards.push({
  type:        "bosch-nvr-timeline-card",
  name:        "Bosch NVR Timeline",
  description: "24-hour timeline scrubber for Mini-NVR recordings. Click a segment to play it.",
  preview:     false,
});

// ── Phase 6: Multi-Cam Stacked Card ──────────────────────────────────────────
// config: { cameras: [{camera_entity, nvr_source_id, motion_entity, label}], show_date }
// Each camera row shows an independent timeline + video player.
// Shared seek: clicking any row's timeline scrubs ALL cameras to the same time.
// rAF drift correction keeps follower videos in sync with the master (first) video.

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
    // Push hass down to child timeline cards
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
    this._currentDate = new Date().toISOString().slice(0, 10);
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });

    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;background:var(--card-background-color);border-radius:12px;overflow:hidden}
        .multi-header{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;
          background:var(--primary-color);color:var(--text-primary-color);font-size:14px;font-weight:500}
        .nav-btn{background:none;border:none;color:inherit;cursor:pointer;font-size:18px;padding:4px 8px}
        .date-label{flex:1;text-align:center}
        .cam-row{border-bottom:1px solid var(--divider-color);padding:8px 0}
        .cam-label{padding:4px 16px;font-size:12px;font-weight:500;color:var(--secondary-text-color);
          text-transform:uppercase;letter-spacing:0.05em}
      </style>
      <div class="multi-header">
        <button class="nav-btn" id="prev">&#8249;</button>
        <span class="date-label" id="date-lbl">${this._currentDate}</span>
        <button class="nav-btn" id="next">&#8250;</button>
      </div>
      <div id="rows"></div>`;

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
        show_date: false,
      });
      timelineCard.hass = this._hass;
      rowEl.appendChild(timelineCard);
      rowsEl.appendChild(rowEl);
      this._rowCards.push(timelineCard);
    }

    // Override each card's canvas click to implement shared seek
    this._patchSharedSeek();
    this._startDriftCorrection();
  }

  _patchSharedSeek() {
    // Replace each row card's _onCanvasClick so a click seeks ALL cameras
    for (const card of this._rowCards) {
      card._onCanvasClick = async (e) => {
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
      for (const seg of (card._segments || [])) {
        const pos = card._segmentTimeOffset(seg);
        if (!pos) continue;
        const segStart = pos.start * 86400;
        const segEnd = segStart + pos.duration * 86400;
        if (offsetSeconds >= segStart && offsetSeconds <= segEnd) {
          best = seg; break;
        }
        const delta = Math.abs(segStart - offsetSeconds);
        if (delta < bestDelta) { bestDelta = delta; best = seg; }
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
    const TOLERANCE_S = 0.1;
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
    if (this._driftRaf) { cancelAnimationFrame(this._driftRaf); this._driftRaf = null; }
  }

  getCardSize() {
    return (this._config && this._config.cameras ? this._config.cameras.length : 1) * 4;
  }
}
customElements.define("bosch-nvr-multi-cam-card", BoschNvrMultiCamCard);
window.customCards.push({
  type:        "bosch-nvr-multi-cam-card",
  name:        "Bosch NVR Multi-Cam",
  description: "Stacked NVR timeline view for multiple cameras with shared seek and drift correction.",
  preview:     false,
});


// ─────────────────────────────────────────────────────────────────────────────
// Bosch Notifications Card
// ─────────────────────────────────────────────────────────────────────────────
// Aggregates Bosch-cloud-side events that the integration knows about into
// a single dashboard pane:
//
//   • Active / scheduled / recently ended cloud maintenance (RSS-driven,
//     sensor.bosch_<cam>_bosch_cloud_wartung)
//   • Cloud reachability state (info.connection_status — derived from the
//     cloud-state-alert pipeline)
//   • Cameras currently offline + LAN-reachability mismatches (cloud says
//     offline but the camera answers a LAN ping)
//
// Auto-discovers the relevant entities — pass no config to use the first
// maintenance sensor + every camera in the integration. Or pin a specific
// maintenance entity + camera list.
//
// Card YAML:
//   type: custom:bosch-notifications-card
//   # all options optional — sensible defaults
//   title: Bosch Cloud
//   maintenance_entity: sensor.bosch_terrasse_bosch_cloud_wartung
//   camera_status_entities:
//     - sensor.bosch_terrasse_status
//     - sensor.bosch_innenbereich_status
//   show_camera_grid: true   # default true
//   show_when_clear: true    # default true — show "alles ruhig" when idle
//
// Bug 2026-05-20: surfaced from a user-reported Bosch maintenance window
// with no in-dashboard signal — only the Signal notifier fired (and that
// fired 20× before the dedup-persistence fix in this same release).
// ─────────────────────────────────────────────────────────────────────────────
class BoschNotificationsCard extends HTMLElement {
  setConfig(config) {
    this._config = {
      title:                  config.title ?? "Bosch Cloud",
      maintenance_entity:     config.maintenance_entity ?? null,
      camera_status_entities: Array.isArray(config.camera_status_entities)
        ? config.camera_status_entities
        : null,
      show_camera_grid:       config.show_camera_grid !== false,
      show_when_clear:        config.show_when_clear !== false,
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
    // Auto-discover: first sensor whose entity_id ends in `_bosch_cloud_wartung`
    for (const eid in this._hass.states) {
      if (/^sensor\..*_bosch_cloud_wartung$/.test(eid)) {
        return this._hass.states[eid];
      }
    }
    return null;
  }

  _cameraStatusEntities() {
    if (this._config.camera_status_entities) {
      return this._config.camera_status_entities
        .map(eid => this._hass.states[eid])
        .filter(Boolean);
    }
    // Auto-discover: every sensor matching `sensor.bosch_*_status` whose
    // friendly_name doesn't include "Mini" / "Stream" (those are different
    // status sensors on the same cam).
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
      weekday: "short", day: "2-digit", month: "2-digit",
      hour: "2-digit", minute: "2-digit",
    }) : "?";
    // Only render the link when the URL has an https:// scheme — prevents
    // a compromised RSS feed (or any sensor-state-write path) from injecting
    // `javascript:` / `data:` URIs that execute in the dashboard context.
    const safeLink = (a.link && /^https:\/\//i.test(a.link)) ? a.link : null;
    const link = safeLink ? `<a href="${this._esc(safeLink)}" target="_blank" rel="noopener">Details bei Bosch →</a>` : "";

    if (state === "active") {
      return `
        <div class="banner banner-active">
          <div class="banner-icon">⚠️</div>
          <div class="banner-body">
            <div class="banner-title">Cloud-Wartung läuft</div>
            <div class="banner-sub">${this._esc(a.title || "Wartungsmeldung")}</div>
            <div class="banner-window">${fmtTime(a.scheduled_start)} – ${fmtTime(a.scheduled_end).split(", ").slice(-1)[0]}</div>
            <div class="banner-note">Live-Bild und Snapshots ggf. eingeschränkt.</div>
            ${link ? `<div class="banner-link">${link}</div>` : ""}
          </div>
        </div>`;
    }
    if (state === "scheduled") {
      return `
        <div class="banner banner-scheduled">
          <div class="banner-icon">📅</div>
          <div class="banner-body">
            <div class="banner-title">Cloud-Wartung geplant</div>
            <div class="banner-sub">${this._esc(a.title || "Wartungsmeldung")}</div>
            <div class="banner-window">Beginn: ${fmtTime(a.scheduled_start)}<br>Ende: ${fmtTime(a.scheduled_end)}</div>
            ${link ? `<div class="banner-link">${link}</div>` : ""}
          </div>
        </div>`;
    }
    if (state === "recent" || state === "past") {
      return `
        <div class="banner banner-past">
          <div class="banner-icon">✅</div>
          <div class="banner-body">
            <div class="banner-title">Cloud-Wartung beendet</div>
            <div class="banner-sub">${this._esc(a.title || "Wartungsmeldung")}</div>
            <div class="banner-window">Beendet ${fmtTime(a.scheduled_end)}</div>
            <div class="banner-note">Cloud-Dienste sollten wieder normal funktionieren.</div>
          </div>
        </div>`;
    }
    return "";
  }

  _cameraGrid(cams) {
    if (!cams.length || !this._config.show_camera_grid) return "";
    const rows = cams.map(c => {
      const name = (c.attributes && c.attributes.friendly_name) || c.entity_id;
      const cleanName = name.replace(/^Bosch\s+/, "").replace(/\s+Status$/, "");
      const status = c.state || "unknown";
      const cls = (status === "ONLINE" || status === "online") ? "ok"
                : (status === "OFFLINE" || status === "offline") ? "warn"
                : "muted";
      return `
        <div class="cam-row">
          <span class="cam-dot ${cls}"></span>
          <span class="cam-name">${this._esc(cleanName)}</span>
          <span class="cam-state ${cls}">${this._esc(status)}</span>
        </div>`;
    }).join("");
    return `<div class="cam-grid"><div class="cam-header">Kamera-Status</div>${rows}</div>`;
  }

  _clearMessage(maint, cams) {
    if (!this._config.show_when_clear) return "";
    const hasMaint = maint && ["active", "scheduled", "recent"].includes(maint.state);
    if (hasMaint) return "";
    const anyOffline = cams.some(c => /OFFLINE|offline/i.test(c.state));
    if (anyOffline) return "";
    return `<div class="clear">✓ Keine Bosch-Cloud-Wartung geplant. Alle Kameras erreichbar.</div>`;
  }

  _esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });

    const maint = this._maintenanceEntity();
    const cams = this._cameraStatusEntities();

    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;background:var(--card-background-color,#1c1c1c);border-radius:12px;
          padding:16px;color:var(--primary-text-color,#fff);font-family:var(--paper-font-body1_-_font-family,Roboto)}
        h2{margin:0 0 12px 0;font-size:16px;font-weight:500;color:var(--primary-text-color,#fff)}
        .banner{display:flex;gap:12px;padding:12px;border-radius:8px;margin-bottom:12px;align-items:flex-start}
        .banner-active{background:rgba(255,152,0,0.12);border-left:4px solid #ff9800}
        .banner-scheduled{background:rgba(33,150,243,0.12);border-left:4px solid #2196f3}
        .banner-past{background:rgba(76,175,80,0.12);border-left:4px solid #4caf50}
        .banner-icon{font-size:24px;line-height:1}
        .banner-body{flex:1;font-size:13px}
        .banner-title{font-weight:600;margin-bottom:4px}
        .banner-sub{color:var(--secondary-text-color,#aaa);margin-bottom:4px}
        .banner-window{font-family:var(--paper-font-code1_-_font-family,monospace);font-size:12px;margin-bottom:4px}
        .banner-note{color:var(--secondary-text-color,#aaa);font-size:12px;margin-bottom:4px}
        .banner-link a{color:var(--primary-color,#03a9f4);text-decoration:none}
        .banner-link a:hover{text-decoration:underline}
        .cam-grid{margin-top:8px}
        .cam-header{font-size:12px;color:var(--secondary-text-color,#aaa);
          text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;font-weight:500}
        .cam-row{display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--divider-color,#333);font-size:13px}
        .cam-row:last-child{border-bottom:none}
        .cam-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
        .cam-dot.ok{background:#4caf50}
        .cam-dot.warn{background:#ff9800}
        .cam-dot.muted{background:#666}
        .cam-name{flex:1}
        .cam-state{font-size:11px;color:var(--secondary-text-color,#aaa);text-transform:uppercase;letter-spacing:0.5px}
        .cam-state.ok{color:#4caf50}
        .cam-state.warn{color:#ff9800}
        .clear{padding:12px;text-align:center;color:var(--secondary-text-color,#aaa);font-size:13px}
      </style>
      <h2>${this._esc(this._config.title)}</h2>
      ${this._maintenanceBanner(maint)}
      ${this._cameraGrid(cams)}
      ${this._clearMessage(maint, cams)}`;
  }

  getCardSize() {
    return 3;
  }
}
customElements.define("bosch-notifications-card", BoschNotificationsCard);
window.customCards.push({
  type:        "bosch-notifications-card",
  name:        "Bosch Notifications",
  description: "Bosch cloud maintenance + camera status banner. Aggregates active/scheduled/past maintenance windows from the RSS feed and shows online/offline state per camera.",
  preview:     false,
});
