/**
 * Bosch Camera Card — Stream Watchdog v3.0
 * Monitors bosch-camera-card instances and fixes:
 * 1. Chrome autoplay: keeps video muted and playing
 * 2. Dead HLS (readyState=0): restarts HLS via camera/stream WS
 * 3. Hidden video with stream ON: starts HLS
 * Works even when the card's JS properties are inaccessible (cache issue).
 */
(function() {
  let stallCounts = {};

  function findCards(root, depth) {
    if (depth > 20) return [];
    let result = [];
    try {
      const cards = root.querySelectorAll ? root.querySelectorAll("bosch-camera-card") : [];
      result = Array.from(cards);
    } catch (e) {}
    const all = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (const el of all) {
      if (el.shadowRoot) result = result.concat(findCards(el.shadowRoot, depth + 1));
    }
    return result;
  }

  function getHass() {
    const ha = document.querySelector("home-assistant");
    return ha ? ha.hass : null;
  }

  function getCameraEntities(hass) {
    if (!hass || !hass.states) return [];
    return Object.keys(hass.states).filter(e => e.startsWith("camera.bosch_"));
  }

  function startHLS(video, hlsUrl) {
    if (window.Hls && Hls.isSupported()) {
      const hls = new Hls({
        enableWorker: true,
        lowLatencyMode: true,
        liveSyncDurationCount: 3,
        maxBufferLength: 10,
        maxMaxBufferLength: 20,
      });
      hls.loadSource(hlsUrl);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        video.muted = true;
        video.play().catch(() => {});
      });
      hls.on(Hls.Events.ERROR, (_ev, data) => {
        if (data.fatal) {
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
          else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
        }
      });
      // Store on video element for later cleanup
      video._watchdogHls = hls;
    } else if (video.canPlayType && video.canPlayType("application/vnd.apple.mpegurl") !== "") {
      video.src = hlsUrl;
      video.muted = true;
      video.play().catch(() => {});
    }
  }

  setInterval(() => {
    const hass = getHass();
    if (!hass) return;
    const camEntities = getCameraEntities(hass);
    const cards = findCards(document, 0);

    for (let i = 0; i < cards.length && i < camEntities.length; i++) {
      const card = cards[i];
      const sr = card.shadowRoot;
      if (!sr) continue;
      const video = sr.getElementById("cam-video");
      const timer = sr.getElementById("stream-label");
      const timerText = timer ? timer.textContent : "";
      const camEntity = camEntities[i];

      // Only act when stream is supposed to be ON
      if (!timerText || timerText === "idle" || timerText === "none") continue;
      if (!video) continue;

      const key = camEntity || ("card" + i);
      const camState = hass.states[camEntity];
      const isStreaming = camState && camState.state === "streaming";

      // Case 1: Video visible but paused → play muted
      if (video.style.display === "block" && video.paused && video.readyState >= 2) {
        video.muted = true;
        video.play().catch(() => {});
        stallCounts[key] = 0;
      }

      // Case 2: Video visible but readyState=0 (HLS dead) → restart HLS
      if (video.style.display === "block" && video.readyState === 0 && isStreaming) {
        stallCounts[key] = (stallCounts[key] || 0) + 1;
        if (stallCounts[key] >= 4) { // 20s stall
          console.warn("bosch-watchdog: HLS dead for", camEntity, "— restarting");
          stallCounts[key] = 0;
          // Cleanup old HLS
          if (video._watchdogHls) { video._watchdogHls.destroy(); video._watchdogHls = null; }
          // Get fresh HLS URL
          hass.callWS({ type: "camera/stream", entity_id: camEntity })
            .then(result => {
              if (result && result.url) {
                startHLS(video, result.url);
              }
            })
            .catch(e => console.warn("bosch-watchdog: stream WS failed:", e));
        }
      } else if (video.readyState > 0) {
        stallCounts[key] = 0;
      }

      // Case 3: Video hidden but stream ON → show video and start HLS
      if (video.style.display === "none" && isStreaming) {
        stallCounts[key + "_h"] = (stallCounts[key + "_h"] || 0) + 1;
        if (stallCounts[key + "_h"] >= 6) { // 30s hidden
          console.warn("bosch-watchdog: video hidden for", camEntity, "— starting HLS");
          stallCounts[key + "_h"] = 0;
          const img = sr.getElementById("cam-img");
          video.style.display = "block";
          if (img) img.style.display = "none";
          if (video._watchdogHls) { video._watchdogHls.destroy(); video._watchdogHls = null; }
          hass.callWS({ type: "camera/stream", entity_id: camEntity })
            .then(result => {
              if (result && result.url) startHLS(video, result.url);
            })
            .catch(() => {});
        }
      } else {
        stallCounts[key + "_h"] = 0;
      }
    }
  }, 5000);

  console.log("bosch-camera-watchdog v3.0 loaded");
})();
