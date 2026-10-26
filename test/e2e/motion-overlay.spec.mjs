import { test, expect } from "@playwright/test";

// Client-side motion overlay: feed the card's <video> a synthetic canvas
// stream (static scene, then a moving square) and assert the overlay canvas,
// the red box pixels and the bosch-card-motion event. No real HA/camera needed.
test.afterEach(async ({ page }) => {
  await page
    .evaluate(() => document.querySelectorAll("bosch-camera-card").forEach((c) => c.remove()))
    .catch(() => {});
});

async function mount(page, extra) {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  await page.evaluate((extra) => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", ...extra });
    card.hass = {
      states: { "camera.test": { state: "idle", attributes: { friendly_name: "Test" }, last_updated: "2026-01-01T00:00:00Z" } },
      config: { internal_url: "http://localhost:4321" },
      language: "en", localize: () => "",
      callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
    };
    document.body.appendChild(card);
    window.__card = card;
    window.__events = [];
    card.addEventListener("bosch-card-motion", (e) => window.__events.push(e.detail.active));
  }, extra);
  await page.waitForTimeout(500);
}

// Attach a synthetic stream to the card's video; `moving` toggles the square.
async function attachFakeStream(page) {
  await page.evaluate(async () => {
    const src = document.createElement("canvas");
    src.width = 320; src.height = 180;
    const c = src.getContext("2d");
    window.__moving = false;
    let x = 0;
    setInterval(() => {
      c.fillStyle = "#222"; c.fillRect(0, 0, 320, 180);
      if (window.__moving) { x = (x + 37) % 240; c.fillStyle = "#fff"; c.fillRect(x, 60, 60, 60); }
      // burned-in clock stand-in: flickers in the bottom-right corner
      if (window.__clock && Math.random() > 0.5) { c.fillStyle = "#fff"; c.fillRect(262, 158, 54, 14); }
    }, 40);
    const video = window.__card.shadowRoot.getElementById("cam-video");
    video.style.display = "block";
    video.srcObject = src.captureStream(25);
    await video.play();
    window.__video = video;
  });
  await page.waitForFunction(() => window.__video.readyState >= 2 && window.__video.videoWidth > 0);
}

test("motion_overlay: static scene = no motion, moving square = box + event, stop = cleanup", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await mount(page, { motion_overlay: true });
  await attachFakeStream(page);
  await page.evaluate(() => window.__card._motionStart(window.__video));

  // overlay canvas exists inside the img-wrapper
  expect(await page.evaluate(() => !!window.__card.shadowRoot.querySelector("#img-wrapper canvas"))).toBe(true);

  // static: no event
  await page.waitForTimeout(1200);
  expect(await page.evaluate(() => window.__events)).toEqual([]);

  // moving: event true + red pixels on overlay
  await page.evaluate(() => { window.__moving = true; });
  await page.waitForFunction(() => window.__events.includes(true), null, { timeout: 5000 });
  // Overlay is cleared on quiet ticks, so poll and keep the best sample.
  const red = await page.evaluate(() => new Promise((res) => {
    const ov = window.__card._motionCanvas;
    let best = 0, left = 20;
    const t = setInterval(() => {
      const d = ov.getContext("2d").getImageData(0, 0, ov.width, ov.height).data;
      let n = 0;
      for (let i = 0; i < d.length; i += 4) if (d[i] > 200 && d[i + 1] < 100 && d[i + 3] > 0) n++;
      best = Math.max(best, n);
      if (--left === 0) { clearInterval(t); res(best); }
    }, 100);
  }));
  expect(red, "red bounding box pixels drawn").toBeGreaterThan(20);

  // back to static: inactive event after debounce
  await page.evaluate(() => { window.__moving = false; });
  await page.waitForFunction(() => window.__events.at(-1) === false, null, { timeout: 6000 });

  // overlay box tracks the <video> box
  const geo = await page.evaluate(() => {
    const o = window.__card._motionCanvas, v = window.__video;
    return [o.clientWidth === v.clientWidth, o.clientHeight === v.clientHeight];
  });
  expect(geo).toEqual([true, true]);

  // stop while active must emit active:false (no stuck "motion" for listeners)
  await page.evaluate(() => { window.__moving = true; });
  await page.waitForFunction(() => window.__card._motionActive === true, null, { timeout: 5000 });
  await page.evaluate(() => { window.__events.length = 0; window.__card._motionStop(); });
  expect(await page.evaluate(() => window.__events)).toEqual([false]);

  // stop removes canvas + timer
  expect(await page.evaluate(() => !!window.__card.shadowRoot.querySelector("#img-wrapper canvas"))).toBe(false);
  expect(errors).toEqual([]);
});

test("motion_overlay off (default): _motionStart is a no-op", async ({ page }) => {
  await mount(page, {});
  await attachFakeStream(page);
  await page.evaluate(() => window.__card._motionStart(window.__video));
  expect(await page.evaluate(() => !!window.__card.shadowRoot.querySelector("#img-wrapper canvas"))).toBe(false);
  expect(await page.evaluate(() => window.__card._motionTimer || null)).toBeNull();
});

test("motion_overlay: threshold/min_area are clamped", async ({ page }) => {
  await mount(page, { motion_overlay: true, motion_overlay_threshold: 9999, motion_overlay_min_area: -5 });
  const cfg = await page.evaluate(() => [window.__card._config.motion_overlay_threshold, window.__card._config.motion_overlay_min_area]);
  expect(cfg).toEqual([100, 0.1]);
});

test("motion_overlay: off-screen card pauses analysis and resets state", async ({ page }) => {
  await mount(page, { motion_overlay: true });
  await attachFakeStream(page);
  await page.evaluate(() => { window.__moving = true; window.__card._motionStart(window.__video); });
  await page.waitForFunction(() => window.__card._motionActive === true, null, { timeout: 5000 });
  // Scroll the card far out of view -> IntersectionObserver -> inactive event + no new activity.
  await page.evaluate(() => { window.__card.style.display = "block"; window.__card.style.marginTop = "6000px"; });
  await page.waitForFunction(() => window.__card._motionActive === false, null, { timeout: 5000 });
  expect(await page.evaluate(() => window.__events.at(-1))).toBe(false);
});

test("motion_overlay: unreadable canvas stops the detector after retries", async ({ page }) => {
  await mount(page, { motion_overlay: true });
  await attachFakeStream(page);
  await page.evaluate(() => {
    CanvasRenderingContext2D.prototype.getImageData = () => { throw new Error("tainted"); };
    window.__card._motionStart(window.__video);
  });
  await page.waitForFunction(() => window.__card._motionTimer === null, null, { timeout: 5000 });
});

async function clockEvents(page, cfg) {
  await mount(page, cfg);
  await attachFakeStream(page);
  await page.evaluate(() => { window.__clock = true; window.__card._motionStart(window.__video); });
  await page.waitForTimeout(2500);
  return page.evaluate(() => window.__events);
}

test("motion_overlay: burned-in clock corner is ignored by default", async ({ page }) => {
  expect(await clockEvents(page, { motion_overlay: true })).toEqual([]);
});

test("motion_overlay: clock corner counts as motion when ignore is disabled", async ({ page }) => {
  expect(await clockEvents(page, { motion_overlay: true, motion_overlay_ignore_timestamp: false })).toContain(true);
});

test("motion_overlay: custom ignore zone masks that area; bad zones are dropped", async ({ page }) => {
  const zone = { x: 0.78, y: 0.8, w: 0.22, h: 0.2 };
  expect(await clockEvents(page, {
    motion_overlay: true, motion_overlay_ignore_timestamp: false, motion_overlay_ignore_zones: [zone],
  })).toEqual([]);
  await mount(page, { motion_overlay: true, motion_overlay_ignore_zones: [{ x: "a" }, null, { x: 0, y: 0, w: 0, h: 1 }, zone] });
  expect(await page.evaluate(() => window.__card._config.motion_overlay_ignore_zones.length)).toBe(1);
});

test("motion_overlay: measured defaults (threshold 20, min area 0.25 %, clock zone)", async ({ page }) => {
  await mount(page, { motion_overlay: true });
  const c = await page.evaluate(() => window.__card._config);
  expect([c.motion_overlay_threshold, c.motion_overlay_min_area, c.motion_overlay_ignore_timestamp]).toEqual([20, 0.25, true]);
});

// Scene variants for the false-positive filters (global brightness, scattered noise).
async function attachScene(page, draw) {
  await page.evaluate(async (draw) => {
    const src = document.createElement("canvas");
    src.width = 320; src.height = 180;
    const c = src.getContext("2d");
    window.__phase = 0;
    const fn = new Function("c", "phase", draw);
    setInterval(() => { window.__phase++; fn(c, window.__phase); }, 40);
    const video = window.__card.shadowRoot.getElementById("cam-video");
    video.style.display = "block";
    video.srcObject = src.captureStream(25);
    await video.play();
    window.__video = video;
  }, draw);
  await page.waitForFunction(() => window.__video.readyState >= 2 && window.__video.videoWidth > 0);
}

test("motion_overlay: a global brightness change is not motion", async ({ page }) => {
  await mount(page, { motion_overlay: true });
  // whole frame flips between two grey levels every ~1 s (auto exposure / IR switch stand-in)
  await attachScene(page, 'c.fillStyle = (Math.floor(phase / 25) % 2) ? "#343434" : "#222"; c.fillRect(0, 0, 320, 180);');
  await page.evaluate(() => window.__card._motionStart(window.__video));
  await page.waitForTimeout(3000);
  expect(await page.evaluate(() => window.__events)).toEqual([]);
});

test("motion_overlay: scattered single-pixel noise is not motion", async ({ page }) => {
  await mount(page, { motion_overlay: true });
  await attachScene(page, 'c.fillStyle = "#222"; c.fillRect(0, 0, 320, 180); c.fillStyle = "#fff"; for (let i = 0; i < 12; i++) c.fillRect(Math.random() * 316, Math.random() * 176, 2, 2);');
  await page.evaluate(() => window.__card._motionStart(window.__video));
  await page.waitForTimeout(3000);
  expect(await page.evaluate(() => window.__events)).toEqual([]);
});

test("motion_overlay: the box stays visible for a moment after motion stops", async ({ page }) => {
  await mount(page, { motion_overlay: true });
  await attachFakeStream(page);
  await page.evaluate(() => { window.__moving = true; window.__card._motionStart(window.__video); });
  await page.waitForFunction(() => window.__card._motionActive === true, null, { timeout: 5000 });
  await page.waitForTimeout(600);
  await page.evaluate(() => { window.__moving = false; });
  // within ~2 ticks of the last motion the held box must still be drawn
  const held = await page.evaluate(() => new Promise((res) => {
    const ov = window.__card._motionCanvas;
    let seen = false, left = 12;
    const t = setInterval(() => {
      const d = ov.getContext("2d").getImageData(0, 0, ov.width, ov.height).data;
      for (let i = 0; i < d.length; i += 4) if (d[i] > 200 && d[i + 1] < 100 && d[i + 3] > 0) { seen = true; break; }
      if (--left === 0) { clearInterval(t); res(seen); }
    }, 100);
  }));
  expect(held).toBe(true);
});

test("motion_overlay: a large object covering most of the frame is still motion", async ({ page }) => {
  await mount(page, { motion_overlay: true });
  // 60 % of the frame lights up for a second, then goes dark again (big object entering/leaving)
  await attachScene(page, 'c.fillStyle = "#222"; c.fillRect(0, 0, 320, 180); if (Math.floor(phase / 25) % 2) { c.fillStyle = "#4a4a4a"; c.fillRect(0, 0, 192, 180); }');
  await page.evaluate(() => window.__card._motionStart(window.__video));
  await page.waitForFunction(() => window.__events.includes(true), null, { timeout: 5000 });
});
