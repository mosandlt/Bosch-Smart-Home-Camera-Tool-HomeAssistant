/**
 * Bosch Camera Card — Stream Watchdog
 * Comprehensive fix for HLS stream issues:
 * 1. Chrome autoplay policy: keeps video muted and playing
 * 2. Dead stream detection: restarts HLS when video stalls
 * 3. Session recovery: restarts HLS when stream_source changes
 * Version: 2.0.0
 */
(function() {
  let lastTimes = {};
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

  setInterval(() => {
    for (const card of findCards(document, 0)) {
      const sr = card.shadowRoot;
      if (!sr) continue;
      const video = sr.getElementById("cam-video");
      const timer = sr.getElementById("stream-label");
      const timerText = timer ? timer.textContent : "";

      // Only act if stream is supposed to be ON (timer shows MM:SS, not "idle")
      if (!timerText || timerText === "idle" || timerText === "none") continue;

      if (!video) continue;
      const cardId = card._config ? card._config.camera_entity : "unknown";

      // Case 1: Video element visible but paused → restart muted
      if (video.style.display === "block" && video.paused && video.readyState >= 2) {
        video.muted = true;
        video.play().catch(() => {});
      }

      // Case 2: Video element visible but readyState=0 (no data) → HLS dead
      // This happens when session restarts and FFmpeg reconnects with new URL
      if (video.style.display === "block" && video.readyState === 0 && video.src) {
        stallCounts[cardId] = (stallCounts[cardId] || 0) + 1;
        if (stallCounts[cardId] >= 4) { // 20s of no data (4 × 5s)
          console.warn("bosch-watchdog: HLS dead for", cardId, "— restarting");
          stallCounts[cardId] = 0;
          // Force card to restart HLS by calling camera/stream WS
          try {
            const ha = document.querySelector("home-assistant");
            const hass = ha ? ha.hass : null;
            if (hass && card._entities && card._entities.camera) {
              hass.callWS({ type: "camera/stream", entity_id: card._entities.camera })
                .then(result => {
                  if (result && result.url) {
                    video.src = "";
                    // Try to load hls.js if available
                    if (window.Hls && Hls.isSupported()) {
                      const hls = new Hls({ enableWorker: true, lowLatencyMode: true, liveSyncDurationCount: 3 });
                      hls.loadSource(result.url);
                      hls.attachMedia(video);
                      hls.on(Hls.Events.MANIFEST_PARSED, () => {
                        video.muted = true;
                        video.play().catch(() => {});
                      });
                    } else {
                      video.src = result.url;
                      video.muted = true;
                      video.play().catch(() => {});
                    }
                  }
                })
                .catch(e => console.warn("bosch-watchdog: stream WS failed:", e));
            }
          } catch (e) {
            console.warn("bosch-watchdog: restart error:", e);
          }
        }
      } else {
        stallCounts[cardId] = 0;
      }

      // Case 3: Video hidden but stream ON → card never started HLS
      if (video.style.display === "none" && video.readyState === 0) {
        stallCounts[cardId + "_hidden"] = (stallCounts[cardId + "_hidden"] || 0) + 1;
        if (stallCounts[cardId + "_hidden"] >= 6) { // 30s hidden (6 × 5s)
          console.warn("bosch-watchdog: video hidden for 30s with stream ON — starting HLS for", cardId);
          stallCounts[cardId + "_hidden"] = 0;
          try {
            const ha = document.querySelector("home-assistant");
            const hass = ha ? ha.hass : null;
            const camEntity = card._config ? card._config.camera_entity : null;
            if (hass && camEntity) {
              const cam = hass.states[camEntity];
              if (cam && cam.state === "streaming") {
                hass.callWS({ type: "camera/stream", entity_id: camEntity })
                  .then(result => {
                    if (result && result.url) {
                      const img = sr.getElementById("cam-img");
                      video.style.display = "block";
                      if (img) img.style.display = "none";
                      if (window.Hls && Hls.isSupported()) {
                        const hls = new Hls({ enableWorker: true, lowLatencyMode: true, liveSyncDurationCount: 3 });
                        hls.loadSource(result.url);
                        hls.attachMedia(video);
                        hls.on(Hls.Events.MANIFEST_PARSED, () => {
                          video.muted = true;
                          video.play().catch(() => {});
                        });
                      } else {
                        video.src = result.url;
                        video.muted = true;
                        video.play().catch(() => {});
                      }
                    }
                  })
                  .catch(e => console.warn("bosch-watchdog: start error:", e));
              }
            }
          } catch (e) {}
        }
      } else {
        stallCounts[cardId + "_hidden"] = 0;
      }
    }
  }, 5000);

  console.log("bosch-camera-autoplay-fix v2.0.0 loaded");
})();
