import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Card source (unminified) — some regression tests assert on the ordering of
// statements that survive intact in source but get mangled by the bundler, so
// they read src directly rather than the served www/ bundle.
const CARD_SRC = readFileSync(
  fileURLToPath(new URL("../../src/bosch-camera-card.js", import.meta.url)),
  "utf8",
);

// Remove any mounted cards after each test so their refresh timers + hls.js
// loader stop (disconnectedCallback → _stopRefreshTimer). Leaving them running
// kept the page busy and hung the WebKit-on-Windows worker at context-close
// ("worker process did not exit … force-killed", CI). Idle page → clean close.
test.afterEach(async ({ page }) => {
  await page
    .evaluate(() => {
      document
        .querySelectorAll("bosch-camera-card, bosch-camera-overview-card")
        .forEach((c) => c.remove());
    })
    .catch(() => {});
});

// Minimal cross-engine smoke: the bundle must parse and register all custom
// elements, and mounting an idle single card with a mock `hass` must not throw
// an uncaught exception, in every engine. No real Home Assistant needed.
test("bundle registers custom elements + mounts idle card without uncaught errors", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });

  const defined = await page.evaluate(() =>
    ["bosch-camera-card", "bosch-camera-overview-card", "bosch-camera-card-editor"]
      .map((t) => !!customElements.get(t)));
  expect(defined, "all custom elements registered").toEqual([true, true, true]);

  // Mount + configure a single card with a minimal mock hass (idle camera).
  await page.evaluate(() => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card.hass = {
      states: { "camera.test": { state: "idle", attributes: { friendly_name: "Test" }, last_updated: "2026-01-01T00:00:00Z" } },
      config: { internal_url: "http://localhost:4321" },
      language: "en",
      localize: () => "",
      callService: () => {},
      callApi: async () => ({}),
      callWS: async () => ({}),
    };
    document.body.appendChild(card);
  });

  // Let one render tick + any sync work settle.
  await page.waitForTimeout(800);

  // The element should have rendered a shadow root with content.
  const hasShadow = await page.evaluate(() => {
    const c = document.querySelector("bosch-camera-card");
    return !!(c && c.shadowRoot && c.shadowRoot.childElementCount > 0);
  });
  expect(hasShadow, "card rendered a shadow DOM").toBe(true);

  expect(pageErrors, "no uncaught page errors during mount").toEqual([]);
});

// Regression (issue #21, 2026-05-30): the cards follow the dashboard's standard
// --ha-card-border-radius theme token by DEFAULT (so one theme change applies to
// every card at once, which is what users expect), and the optional
// border_radius card config overrides the theme per-card. Fallback when the
// theme sets nothing is the apple-style value (22px).
test("card follows the dashboard theme radius; the card option overrides it", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });

  const result = await page.evaluate(async () => {
    // A themed dashboard sets the standard token on an ancestor.
    document.body.style.setProperty("--ha-card-border-radius", "10px");
    const mkHass = () => ({
      states: { "camera.test": { state: "idle", attributes: { friendly_name: "Test" }, last_updated: "2026-01-01T00:00:00Z" } },
      config: { internal_url: "http://localhost:4321" },
      language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
    });
    const mk = (cfg) => { const c = document.createElement("bosch-camera-card"); c.setConfig(cfg); c.hass = mkHass(); document.body.appendChild(c); return c; };
    const themed = mk({ camera_entity: "camera.test", apple_style: true });
    const overridden = mk({ camera_entity: "camera.test", apple_style: true, border_radius: "4px" });
    await new Promise((r) => setTimeout(r, 600));
    const radius = (el) => { const hc = el.shadowRoot && el.shadowRoot.querySelector("ha-card"); return hc ? getComputedStyle(hc).borderTopLeftRadius : null; };
    return { themed: radius(themed), overridden: radius(overridden) };
  });

  expect(result.themed, "default follows the dashboard --ha-card-border-radius").toBe("10px");
  expect(result.overridden, "border_radius card option overrides the theme").toBe("4px");
});

// Proof that interactive card UX is testable headlessly with a mock hass +
// Playwright's REAL-browser event simulation (no live Home Assistant / stream
// needed). This pins the single-card hover as SHADOW-ONLY (issue #15, RkcCorian):
// at rest the ha-card has no scale transform; on hover it gains an elevation
// box-shadow and STILL no transform (no geometry change → no edge shimmer, no
// fullscreen-clip). The old scale-based lift was replaced in v13.5.0.
test("single card lifts via shadow-only on hover (no scale)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });

  // Skip on engines that don't advertise a fine hover pointer — the lift is
  // deliberately gated behind @media (hover: hover) and (pointer: fine).
  const fineHover = await page.evaluate(() => matchMedia("(hover: hover) and (pointer: fine)").matches);
  test.skip(!fineHover, "engine reports no fine-hover pointer; hover lift is gated to those");

  await page.evaluate(() => {
    const card = document.createElement("bosch-camera-card");
    card.id = "hovercard";
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = {
      states: { "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" } },
      config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
    };
    document.body.appendChild(card);
  });
  await page.waitForTimeout(400);

  const haCard = page.locator("#hovercard ha-card");
  const atRest = await haCard.evaluate((el) => ({
    transform: getComputedStyle(el).transform,
    shadow: getComputedStyle(el).boxShadow,
  }));
  await haCard.hover();
  await page.waitForTimeout(250); // let the .18s transition settle
  const onHover = await haCard.evaluate((el) => {
    const cs = getComputedStyle(el);
    const t = cs.transform;
    // Parse the scale factor out of the 2D matrix(a,b,c,d,e,f) → a is x-scale.
    const m = t.match(/matrix\(([^,]+),/);
    return { transform: t, scale: m ? parseFloat(m[1]) : 1, shadow: cs.boxShadow };
  });

  // SHADOW-ONLY: no scale transform on hover (identity / none), and an
  // elevation box-shadow that differs from the at-rest shadow.
  expect(onHover.scale, "no scale transform on hover (shadow-only)").toBe(1);
  expect(
    onHover.transform === "none" || onHover.transform === "matrix(1, 0, 0, 1, 0, 0)",
    "no transform on hover",
  ).toBeTruthy();
  expect(onHover.shadow !== "none", "box-shadow elevation appears on hover").toBeTruthy();
  expect(onHover.shadow !== atRest.shadow, "box-shadow changes on hover").toBeTruthy();
});

// Shared minimal hass factory for the interaction tests below.
const HASS_BASE = `{ config:{}, language:"en", localize:()=>"", callService:()=>{}, callApi:async()=>({}), callWS:async()=>({}) }`;

// #22: privacy ON must stop THIS session's <video> so the HLS buffer doesn't
// keep playing video+sound after the backend tears the stream down.
test("privacy ON stops the live video (mock hass)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const stopCalled = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const mk = (priv) => ({ ...base, states: {
      "camera.test": { state: "streaming", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      "switch.test_privacy_mode": { state: priv, attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
    } });
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = mk("off");
    document.body.appendChild(card);
    await new Promise((r) => setTimeout(r, 300));
    let called = false;
    card._liveVideoActive = true;     // pretend the stream is playing
    card._lastPrivacy = false;        // ensure an OFF→ON transition
    card._stopLiveVideo = () => { called = true; card._liveVideoActive = false; };
    card.hass = mk("on");             // privacy turns ON
    await new Promise((r) => setTimeout(r, 200));
    return called;
  });
  expect(stopCalled, "privacy ON triggers _stopLiveVideo").toBe(true);
});

// Privacy ON → the Live-Stream button must be greyed out + disabled (starting a
// stream is blocked backend-side while the shutter is closed). Both layouts.
test("privacy ON disables the live-stream button (mock hass)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const res = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const mk = (priv) => ({ ...base, states: {
      "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      "switch.test_live_stream": { state: "off", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
      "switch.test_privacy_mode": { state: priv, attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
    } });
    const probe = async (streamId, snapId, appleStyle) => {
      const card = document.createElement("bosch-camera-card");
      card.setConfig({ camera_entity: "camera.test", apple_style: appleStyle });
      card.hass = mk("off");
      document.body.appendChild(card);
      await new Promise((r) => setTimeout(r, 300));
      const off = { stream: card.shadowRoot.getElementById(streamId)?.disabled, snap: card.shadowRoot.getElementById(snapId)?.disabled };
      card.hass = mk("on");
      await new Promise((r) => setTimeout(r, 200));
      const on = { stream: card.shadowRoot.getElementById(streamId)?.disabled, snap: card.shadowRoot.getElementById(snapId)?.disabled };
      card.remove();
      return { off, on };
    };
    return {
      legacy: await probe("btn-stream", "btn-snapshot", false),
      apple: await probe("ap-btn-stream", "ap-btn-snapshot", true),
    };
  });
  expect(res.legacy.off.stream, "privacy OFF → legacy stream button enabled").toBe(false);
  expect(res.legacy.on.stream, "privacy ON → legacy stream button disabled").toBe(true);
  expect(res.legacy.off.snap, "privacy OFF → legacy snapshot button enabled").toBe(false);
  expect(res.legacy.on.snap, "privacy ON → legacy snapshot button disabled").toBe(true);
  expect(res.apple.off.stream, "privacy OFF → apple stream pill enabled").toBe(false);
  expect(res.apple.on.stream, "privacy ON → apple stream pill disabled").toBe(true);
  expect(res.apple.off.snap, "privacy OFF → apple snapshot pill enabled").toBe(false);
  expect(res.apple.on.snap, "privacy ON → apple snapshot pill disabled").toBe(true);
});

// #22: while streaming, one tap on the Ton toggle unmutes the <video> instantly
// (audibility toggle) — no 2-tap off/on dance.
test("audio toggle unmutes the playing video (mock hass)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const calls = [];
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { config: {}, language: "en", localize: () => "",
      callService: (d, s, data) => { calls.push(`${d}.${s}:${data?.entity_id}`); return Promise.resolve(); },
      callApi: async () => ({}), callWS: async () => ({}), states: {
      "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      "switch.test_audio": { state: "off", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
    } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    const video = card.shadowRoot.getElementById("cam-video");
    if (!video) return { error: "no cam-video element" };
    card._liveVideoActive = true;
    video.muted = true;            // browser autoplay starts muted
    card._toggleAudio();           // one tap
    return { mutedAfter: video.muted, calls };
  });
  expect(r.error, "card renders a <video id=cam-video>").toBeUndefined();
  expect(r.mutedAfter, "one tap unmutes the video").toBe(false);
  // The tap also drives the backend switch so the choice syncs to every device.
  expect(r.calls, "tap toggles switch.test_audio on").toContain("switch.turn_on:switch.test_audio");
});

// use_card_audio_settings:true decouples the pill from the global audio entities
// — a tap toggles ONLY this browser's video.muted (localStorage), never the
// backend switch / other devices.
test("decoupled audio (use_card_audio_settings) toggles locally, not the switch", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const calls = [];
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true, use_card_audio_settings: true });
    card.hass = { config: {}, language: "en", localize: () => "",
      callService: (d, s, data) => { calls.push(`${d}.${s}:${data?.entity_id}`); return Promise.resolve(); },
      callApi: async () => ({}), callWS: async () => ({}), states: {
      "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      "switch.test_audio": { state: "on", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
    } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    const video = card.shadowRoot.getElementById("cam-video");
    if (!video) return { error: "no cam-video element" };
    card._liveVideoActive = true;
    video.muted = true;
    card._toggleAudio();
    return { mutedAfter: video.muted, calls };
  });
  expect(r.error).toBeUndefined();
  expect(r.mutedAfter, "tap still unmutes this video locally").toBe(false);
  expect(r.calls, "decoupled tap must NOT call any switch service").not.toContain("switch.turn_on:switch.test_audio");
  expect(r.calls.filter((c) => c.startsWith("switch.")), "no backend switch writes in decoupled mode").toHaveLength(0);
});

// audio_default (per-card YAML override): "backend" (default) follows
// switch.<cam>_audio (the source of truth); "on"/"off" PIN this card's start
// mute state and opt out of the backend live-sync. Regression for the
// 2026-06-02 request: a card declared `audio_default: off` must NEVER
// auto-unmute on stream start even when the backend audio switch is ON.
test("audio_default override pins the start mute state independent of the backend switch", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const probe = async (audioDefault) => {
      const card = document.createElement("bosch-camera-card");
      card.setConfig({ camera_entity: "camera.test", apple_style: true, audio_default: audioDefault });
      card.hass = { config: {}, language: "en", localize: () => "",
        callService: () => Promise.resolve(), callApi: async () => ({}), callWS: async () => ({}), states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
        // backend audio switch is ON in EVERY case — only the YAML override decides:
        "switch.test_audio": { state: "on", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
        "number.test_audio_volume": { state: "50", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
      } };
      document.body.appendChild(card);
      await new Promise((res) => setTimeout(res, 200));
      const video = card.shadowRoot.getElementById("cam-video");
      if (!video) return { error: "no cam-video" };
      card._isIOS = () => false;
      video.muted = true;  // element starts muted (autoplay policy)
      // _applyAudioPreference must seed VOLUME only and NEVER programmatically
      // unmute — a gesture-less unmute makes Chrome pause the live stream
      // ("Unmuting failed and the element was paused instead"). Sound is only
      // enabled by a real pill tap (gesture). 2026-06-03 stream-drop fix.
      card._applyAudioPreference(video);
      return { mode: card._audioDefaultMode(), decoupled: card._audioDecoupled(),
               mutedAfter: video.muted, vol: video.volume };
    };
    return { off: await probe("off"), on: await probe("on"), backend: await probe("backend"), bad: await probe("garbage") };
  });
  expect(r.off?.error || r.on?.error || r.backend?.error || r.bad?.error).toBeUndefined();
  expect(r.off.mode, "audio_default:off normalizes").toBe("off");
  expect(r.off.decoupled, "off opts out of the backend live-sync").toBe(true);
  expect(r.on.mode).toBe("on");
  expect(r.backend.mode, "backend is the default mode").toBe("backend");
  expect(r.backend.decoupled, "backend mode follows the switch (not decoupled)").toBe(false);
  expect(r.bad.mode, "an invalid value falls back to backend").toBe("backend");
  // The element stays MUTED on start in EVERY mode — no auto-unmute, regardless
  // of the backend switch being ON. Sound is gesture-only (stream-drop fix).
  expect(r.off.mutedAfter, "off stays muted on start").toBe(true);
  expect(r.on.mutedAfter, "on stays muted on start (no gesture-less unmute)").toBe(true);
  expect(r.backend.mutedAfter, "backend stays muted on start (no gesture-less unmute)").toBe(true);
  // Volume IS seeded from the backend number entity (50 → 0.5) for the sync modes.
  expect(r.backend.vol, "backend seeds volume from the number entity").toBeCloseTo(0.5, 2);
  expect(r.on.vol, "on seeds volume from the number entity").toBeCloseTo(0.5, 2);
});

// audio_default:off behaves like decoupled mode for taps — the pill toggles
// ONLY this browser's video.muted and never writes the backend switch.
test("audio_default:off toggles locally without touching the backend switch", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const calls = [];
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true, audio_default: "off" });
    card.hass = { config: {}, language: "en", localize: () => "",
      callService: (d, s, data) => { calls.push(`${d}.${s}:${data?.entity_id}`); return Promise.resolve(); },
      callApi: async () => ({}), callWS: async () => ({}), states: {
      "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      "switch.test_audio": { state: "on", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
    } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    const video = card.shadowRoot.getElementById("cam-video");
    if (!video) return { error: "no cam-video element" };
    card._liveVideoActive = true;
    video.muted = true;
    card._toggleAudio();
    return { mutedAfter: video.muted, calls };
  });
  expect(r.error).toBeUndefined();
  expect(r.mutedAfter, "tap unmutes this video locally").toBe(false);
  expect(r.calls.filter((c) => c.startsWith("switch.")), "no backend switch writes for a YAML-pinned card").toHaveLength(0);
});

// #27: the apple-style privacy pill (#ap-btn-privacy) must clear its "on"
// marking as soon as HA reports privacy back OFF — without a second tap. The
// pill used to read raw hass and ignore the optimistic override, so on a Gen1
// LOCAL camera (slow status push) it stayed marked after toggling off
// (RkcCorian). This pins: privacy ON → pill.on; privacy OFF (same render) →
// pill no longer .on; and the optimistic OFF set on tap already clears it.
test("privacy pill clears its marked state after privacy turns off (#27)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const mk = (priv) => ({ ...base, states: {
      "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      "switch.test_privacy_mode": { state: priv, attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
    } });
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = mk("on");
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    const pill = () => card.shadowRoot.getElementById("ap-btn-privacy");
    if (!pill()) return { error: "no ap-btn-privacy element" };
    const onWhilePrivacy = pill().classList.contains("on");
    // Optimistic OFF (what the tap handler sets) must immediately clear the mark
    // even before HA confirms the new state.
    card._optimistic["switch.test_privacy_mode"] = "off";
    card._update();
    const onAfterOptimisticOff = pill().classList.contains("on");
    // And once HA actually reports OFF (optimistic cleared), it stays cleared.
    delete card._optimistic["switch.test_privacy_mode"];
    card.hass = mk("off");
    await new Promise((res) => setTimeout(res, 100));
    const onAfterHassOff = pill().classList.contains("on");
    return { onWhilePrivacy, onAfterOptimisticOff, onAfterHassOff };
  });
  expect(r.error, "card renders #ap-btn-privacy").toBeUndefined();
  expect(r.onWhilePrivacy, "pill is marked while privacy is on").toBe(true);
  expect(r.onAfterOptimisticOff, "pill clears on optimistic off (the tap)").toBe(false);
  expect(r.onAfterHassOff, "pill stays cleared once HA reports off").toBe(false);
});

// #27: after a privacy toggle the backend enforces a 5s cooldown (and now
// rejects an early toggle). The card blocks the tap during that window and
// shows a countdown so the user waits instead of hammering the button.
test("privacy cooldown blocks rapid re-toggle and shows a countdown (#27)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    let calls = 0;
    const base = { config: {}, language: "en", localize: () => "", callService: () => { calls++; return Promise.resolve(); }, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { ...base, states: {
      "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      "switch.test_privacy_mode": { state: "off", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
    } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    // Shorten the cooldown so the test leaves no long-lived interval running —
    // a 10s interval kept the Playwright worker alive on the Windows runner and
    // tripped the "worker did not exit" force-kill (CI flake, not a real bug).
    card._PRIVACY_COOLDOWN_MS = 400;
    const pill = () => card.shadowRoot.getElementById("ap-btn-privacy");
    if (!pill()) return { error: "no ap-btn-privacy element" };
    card._togglePrivacy(); // first tap → fires the service + starts the cooldown
    await new Promise((res) => setTimeout(res, 50));
    const callsAfterFirst = calls;
    const cooldownClass = pill().classList.contains("cooldown");
    const cdAttr = pill().getAttribute("data-cd");
    const ariaDisabled = pill().getAttribute("aria-disabled");
    card._togglePrivacy(); // second tap within the window must be blocked
    await new Promise((res) => setTimeout(res, 50));
    const callsAfterSecond = calls;
    // Belt-and-suspenders cleanup: stop the interval explicitly, then remove the
    // card (disconnectedCallback also clears it) so no timer outlives the test.
    if (card._privacyCooldownTimer) { clearInterval(card._privacyCooldownTimer); card._privacyCooldownTimer = null; }
    card._privacyCooldownUntil = 0;
    card.remove();
    return { callsAfterFirst, callsAfterSecond, cooldownClass, cdAttr, ariaDisabled };
  });
  expect(r.error, "card renders #ap-btn-privacy").toBeUndefined();
  expect(r.callsAfterFirst, "first privacy toggle calls the service once").toBe(1);
  expect(r.cooldownClass, "privacy pill enters the cooldown state").toBe(true);
  expect(Number(r.cdAttr), "countdown badge shows remaining seconds").toBeGreaterThan(0);
  expect(r.ariaDisabled, "pill is aria-disabled during cooldown").toBe("true");
  expect(r.callsAfterSecond, "second toggle within cooldown is blocked (no extra call)").toBe(1);
});

// Live-stream cooldown (2026-06-01): the backend switch.py enforces a 5s
// _STREAM_COOLDOWN — a turn_on within 5s of the last turn_off is rejected. The
// card mirrors privacy: after a stop it blocks the restart tap for that window
// and shows a countdown badge so the user waits instead of hammering the button.
test("stream cooldown blocks rapid restart and shows a countdown", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    let calls = 0;
    const base = { config: {}, language: "en", localize: () => "", callService: () => { calls++; return Promise.resolve(); }, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { ...base, states: {
      "camera.test": { state: "streaming", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      "switch.test_live_stream": { state: "on", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
    } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    // Keep the cooldown comfortably longer than the two 50 ms gaps below so the
    // "second tap is blocked" assertion can't flake when a loaded CI runner
    // (seen on macOS 2026-06-13) stretches a 50 ms setTimeout past a short
    // window via event-loop starvation. The interval is explicitly cleared at
    // the end of this evaluate(), so a longer value leaves nothing running
    // (same Windows-worker-teardown guard as the privacy cooldown test).
    card._STREAM_COOLDOWN_MS = 5000;
    const pill = () => card.shadowRoot.getElementById("ap-btn-stream");
    if (!pill()) return { error: "no ap-btn-stream element" };
    await card._toggleStream(); // first tap = STOP → fires turn_off + starts the cooldown
    await new Promise((res) => setTimeout(res, 50));
    const callsAfterFirst = calls;
    const cooldownClass = pill().classList.contains("cooldown");
    const cdAttr = pill().getAttribute("data-cd");
    const ariaDisabled = pill().getAttribute("aria-disabled");
    await card._toggleStream(); // second tap (restart) within the window must be blocked
    await new Promise((res) => setTimeout(res, 50));
    const callsAfterSecond = calls;
    if (card._streamCooldownTimer) { clearInterval(card._streamCooldownTimer); card._streamCooldownTimer = null; }
    card._streamCooldownUntil = 0;
    card.remove();
    return { callsAfterFirst, callsAfterSecond, cooldownClass, cdAttr, ariaDisabled };
  });
  expect(r.error, "card renders #ap-btn-stream").toBeUndefined();
  expect(r.callsAfterFirst, "first tap stops the stream (one service call)").toBe(1);
  expect(r.cooldownClass, "stream pill enters the cooldown state after stopping").toBe(true);
  expect(Number(r.cdAttr), "countdown badge shows remaining seconds").toBeGreaterThan(0);
  expect(r.ariaDisabled, "pill is aria-disabled during cooldown").toBe("true");
  expect(r.callsAfterSecond, "restart within cooldown is blocked (no extra call)").toBe(1);
});

// Overlay-stuck deadlock (2026-06-01): clearOverlay (fired on the video
// "playing" event) must clear the startup-suppression flags BEFORE it calls
// _setLoadingOverlay(false). _setLoadingOverlay refuses to hide while
// _streamConnecting is set (anti-flicker gate); the flag used to be cleared
// AFTER the hide call, so the gate swallowed it and the spinner stayed forever
// on a fresh start (a reload masked it because the flag inits false). This pins
// the ordering in source — a bundler can reorder nothing here, but a careless
// edit could move the reset back below the hide and reintroduce the deadlock.
test("clearOverlay clears _streamConnecting before hiding the overlay (deadlock regression)", () => {
  const start = CARD_SRC.indexOf("const clearOverlay = () => {");
  expect(start, "clearOverlay closure exists").toBeGreaterThan(-1);
  const body = CARD_SRC.slice(start, CARD_SRC.indexOf("video.removeEventListener(\"playing\", clearOverlay)", start));
  const flagIdx = body.indexOf("this._streamConnecting = false");
  const hideIdx = body.indexOf("this._setLoadingOverlay(false)");
  expect(flagIdx, "clearOverlay resets _streamConnecting").toBeGreaterThan(-1);
  expect(hideIdx, "clearOverlay hides the loading overlay").toBeGreaterThan(-1);
  expect(flagIdx, "_streamConnecting is cleared BEFORE the overlay is hidden").toBeLessThan(hideIdx);
});

// mobile fullscreen: only one card may be in the CSS-fullscreen overlay — a
// second card entering closes the first (single-owner; part of the "closing one
// opened another" fix).
test("fullscreen is single-owner across cards (mock hass)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const mk = (id) => {
      const c = document.createElement("bosch-camera-card");
      c.setConfig({ camera_entity: "camera." + id, apple_style: true });
      c.hass = { ...base, states: { ["camera." + id]: { state: "idle", attributes: { friendly_name: id }, last_updated: "2026-01-01T00:00:00Z" } } };
      document.body.appendChild(c);
      return c;
    };
    const A = mk("a"), B = mk("b");
    await new Promise((res) => setTimeout(res, 300));
    A._enterCssFullscreen();
    B._enterCssFullscreen(); // should close A
    return { aActive: A.classList.contains("fs-active"), bActive: B.classList.contains("fs-active") };
  });
  expect(r.bActive, "second card is fullscreen").toBe(true);
  expect(r.aActive, "first card was closed when the second opened").toBe(false);
});

// Single-PiP greying: the browser allows only ONE picture-in-picture window
// globally, so while one camera floats every OTHER card greys out (disables)
// its PiP button and the active one lights up; releasing PiP re-enables all.
// Driven by the enterpictureinpicture/leavepictureinpicture broadcast — we
// dispatch those events directly (real PiP can't be entered headless). Asserts
// `disabled`, which _reflectPipState() sets regardless of capability-hide, so
// the test is browser-independent.
test("PiP is single-owner across cards — others grey out (mock hass)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const mk = (id) => {
      const c = document.createElement("bosch-camera-card");
      c.setConfig({ camera_entity: "camera." + id, apple_style: true });
      c.hass = { ...base, states: { ["camera." + id]: { state: "idle", attributes: { friendly_name: id }, last_updated: "2026-01-01T00:00:00Z" } } };
      document.body.appendChild(c);
      return c;
    };
    const A = mk("a"), B = mk("b");
    await new Promise((res) => setTimeout(res, 300));
    const btn = (c) => c.shadowRoot.getElementById("ap-btn-pip");
    const vid = (c) => c.shadowRoot.getElementById("cam-video");
    // Force PiP capability so `disabled` is set deterministically across the e2e
    // browser matrix (WebKit/Firefox headless report pictureInPictureEnabled
    // false → the button would be hidden, not greyed).
    Object.defineProperty(document, "pictureInPictureEnabled", { value: true, configurable: true });
    // Both streams LIVE — this test isolates the cross-card single-owner greying,
    // which is a separate concern from the "no live stream → hidden" rule (that
    // has its own test). Without a live stream BOTH buttons are hidden by design.
    A._liveVideoActive = true; B._liveVideoActive = true;
    A._reflectPipState(); B._reflectPipState();
    const snap = () => ({
      aOn: btn(A).classList.contains("on"), aDisabled: btn(A).disabled,
      bOn: btn(B).classList.contains("on"), bDisabled: btn(B).disabled,
    });
    const idle = snap();
    vid(A).dispatchEvent(new Event("enterpictureinpicture"));
    const aFloating = snap();
    vid(A).dispatchEvent(new Event("leavepictureinpicture"));
    const released = snap();
    return { idle, aFloating, released };
  });
  // Nothing floating → no card disabled, none lit.
  expect(r.idle.aDisabled || r.idle.bDisabled, "idle: no card greyed").toBe(false);
  // A floating → A lit + enabled, B greyed out.
  expect(r.aFloating.aOn, "A lights up while floating").toBe(true);
  expect(r.aFloating.aDisabled, "A stays enabled while floating").toBe(false);
  expect(r.aFloating.bDisabled, "B greys out while A floats").toBe(true);
  // Released → both restored.
  expect(r.released.aOn, "A no longer lit after release").toBe(false);
  expect(r.released.bDisabled, "B re-enabled after release").toBe(false);
});

// #21: the overview tile (.bco-cell) carries the themed box-shadow itself,
// because its overflow:hidden (corner-cropping) would clip the inner card's
// shadow. So a dashboard `ha-card-box-shadow` must appear on the cell.
test("overview tile follows the theme box-shadow (#21)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-overview-card"), null, { timeout: 10000 });
  const shadow = await page.evaluate(async () => {
    document.body.style.setProperty("--ha-card-box-shadow", "0px 0px 4px 1.5px rgba(255, 255, 255, 0.5)");
    const ov = document.createElement("bosch-camera-overview-card");
    ov.setConfig({});
    ov.hass = {
      config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
      states: { "camera.demo": { state: "idle", attributes: { friendly_name: "Demo", brand: "Bosch" }, last_updated: "2026-01-01T00:00:00Z" } },
    };
    document.body.appendChild(ov);
    await new Promise((r) => setTimeout(r, 500));
    const cell = ov.shadowRoot && ov.shadowRoot.querySelector(".bco-cell");
    return cell ? getComputedStyle(cell).boxShadow : "no-cell";
  });
  // The cell's resolved box-shadow must reflect the theme value (non-"none").
  expect(shadow === "no-cell" || shadow === "none", "cell carries the themed shadow").toBe(false);
});

// Stale go2rtc source (live fix 2026-05-31): after Bosch rotates the LOCAL
// session creds the backend rebuilds the TLS proxy on a NEW port. go2rtc/the
// card's WebRTC PC stay pinned to the dead port → "connection refused" /
// "wrong user/pass" → frozen image. The card must classify those errors
// distinctly from the benign HA stream-type race ("does not support WebRTC").
test("stale-source errors are classified distinctly from the HA stream-type race", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
      states: { "camera.test": { state: "idle", attributes: {}, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));
    return {
      refused:  card._isStaleSourceError("webrtc: streams: dial tcp 127.0.0.1:41325: connect: connection refused, exec/rtsp"),
      userpass: card._isStaleSourceError("webrtc: streams: wrong user/pass, exec/rtsp"),
      describe: card._isStaleSourceError("method DESCRIBE failed: 404 Not Found"),
      race:     card._isStaleSourceError("Camera does not support WebRTC, frontend_stream_types={HLS}"),
      iceFail:  card._isStaleSourceError("WebRTC: no track within 5s"),
    };
  });
  expect(r.refused, "connection refused → stale source").toBe(true);
  expect(r.userpass, "wrong user/pass → stale source").toBe(true);
  expect(r.describe, "DESCRIBE 404 → stale source").toBe(true);
  expect(r.race, "the benign HA stream-type race is NOT a stale source").toBe(false);
  expect(r.iceFail, "a plain ICE/track timeout is NOT a stale source").toBe(false);
});

// The forced backend rebuild cycles the live-stream switch off→on (fresh PUT
// /connection + new proxy port + go2rtc re-register) and is cooldown-guarded so
// a burst of stale-source errors triggers at most one rebuild.
test("stale go2rtc source forces a cooldown-guarded backend stream rebuild", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const calls = [];
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
      states: {
        "camera.test": { state: "streaming", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
        "switch.test_live_stream": { state: "on", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
      } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));
    card._callService = (d, s) => calls.push(`${d}.${s}`);
    card._stopLiveVideo = () => {};
    card._waitForStreamReady = () => {};
    const first = card._maybeForceBackendRewarm();
    const second = card._maybeForceBackendRewarm(); // within the 20s cooldown
    await new Promise((res) => setTimeout(res, 1800)); // let the off→on timeouts fire
    return { first, second, calls };
  });
  expect(r.first, "first stale-source rebuild is initiated").toBe(true);
  expect(r.second, "second call within cooldown is suppressed").toBe(false);
  expect(r.calls.includes("switch.turn_off"), "switch turned off").toBe(true);
  expect(r.calls.includes("switch.turn_on"), "switch turned back on after delay").toBe(true);
});

// Fullscreen digital zoom: double-tap zooms 2x, fullscreen exit resets it.
test("fullscreen double-tap zooms the video and exit resets it", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { ...base, states: { "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    const wrap = card.shadowRoot.getElementById("img-wrapper");
    if (!wrap) return { error: "no img-wrapper" };
    card._enterCssFullscreen();
    const fire = (type, id, x) => wrap.dispatchEvent(new PointerEvent(type, { pointerId: id, clientX: x, clientY: 120, bubbles: true, cancelable: true }));
    fire("pointerdown", 1, 200); fire("pointerup", 1, 200);          // tap 1
    fire("pointerdown", 2, 200); fire("pointerup", 2, 200);          // tap 2 → double-tap
    const scaleAfterDouble = card._zoom.scale;
    const vt = (card.shadowRoot.getElementById("cam-video") || {}).style?.transform || "";
    card._exitCssFullscreen();
    const scaleAfterExit = card._zoom.scale;
    const vtAfter = (card.shadowRoot.getElementById("cam-video") || {}).style?.transform || "";
    card.remove();
    return { scaleAfterDouble, vt, scaleAfterExit, vtAfter };
  });
  expect(r.error, "card renders img-wrapper").toBeUndefined();
  expect(r.scaleAfterDouble, "double-tap zooms to 2x").toBe(2);
  expect(r.vt.includes("scale(2"), "video carries the zoom transform").toBe(true);
  expect(r.scaleAfterExit, "exit resets zoom to 1x").toBe(1);
  expect(r.vtAfter, "transform cleared on exit").toBe("");
});

test("tap-to-play / loading overlay sits UNDER the control pill bar (Stop stays tappable)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { ...base, states: { "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" } } };
    card.style.width = "400px"; card.style.display = "block";
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    const sr = card.shadowRoot;
    const pill = sr.querySelector(".ap-pill-bar");
    const overlay = sr.getElementById("tap-to-play-overlay");
    const stream = sr.getElementById("ap-btn-stream");
    if (!pill || !overlay || !stream) return { error: "missing pill/overlay/button" };
    // Show the remote tap-to-play gate (the "antippen zum Starten" overlay).
    overlay.classList.add("visible");
    const z = (el) => parseInt(getComputedStyle(el).zIndex || "0", 10);
    const rect = stream.getBoundingClientRect();
    const hit = sr.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    const hitInPill = !!(hit && hit.closest && hit.closest(".ap-pill-bar"));
    return { pillZ: z(pill), overlayZ: z(overlay), hitTag: hit && hit.tagName, hitInPill };
  });
  expect(r.error, "card renders pill bar + overlay + stream button").toBeUndefined();
  expect(r.pillZ, "pill bar sits above the tap-to-play overlay").toBeGreaterThan(r.overlayZ);
  expect(r.hitInPill, "a tap at the Stop button reaches the pill bar, not the overlay").toBe(true);
});

test("dedupe: 'Privat' row stays hidden in the minimal ⋮ overflow tray (#15/#27 RkcCorian)", async ({ page }) => {
  // Regression: an overview tile (minimal, apple-style, hide_redundant_privacy)
  // hid the redundant Privat row until the user tapped ⋮. The overflow-open
  // reveal `:host(.minimal.overflow-open) .switch-rows > .sw-row {display:flex}`
  // (0,5,0) out-specified the dedupe rule (0,4,0), bringing the row back. A
  // 0,6,0 guard now keeps it hidden in the tray too. Both reporter screenshots
  // had ⋮ open. 2026-06-03.
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true, minimal: true, hide_redundant_privacy: true });
    card.hass = { ...base, states: {
      "camera.test":               { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      "switch.test_privacy_mode":  { state: "off", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
      "switch.test_notifications": { state: "on",  attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
    } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    const sr = card.shadowRoot;
    const pr = sr.querySelector(".privacy-row");
    const notif = sr.getElementById("btn-notifications");
    if (!pr || !notif) return { error: "missing privacy/notif row" };
    const hasDedupe = card.classList.contains("dedupe-privacy");
    // Open the ⋮ overflow tray — exactly what the user taps on a minimal tile.
    card.classList.add("overflow-open");
    const prOpen = getComputedStyle(pr).display;
    const notifOpen = getComputedStyle(notif).display;
    card.remove();
    return { hasDedupe, prOpen, notifOpen };
  });
  expect(r.error, "card renders privacy + notifications rows").toBeUndefined();
  expect(r.hasDedupe, "dedupe-privacy applies for a minimal apple-style card").toBe(true);
  expect(r.prOpen, "Privat row stays hidden even with the ⋮ tray open (#15/#27)").toBe("none");
  expect(r.notifOpen, "other switch rows still appear in the ⋮ tray").toBe("flex");
});

test("fullscreen: double-tap ON an overlay control does NOT zoom (button click survives) (#16)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { ...base, states: { "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    const wrap = card.shadowRoot.getElementById("img-wrapper");
    const btn = card.shadowRoot.getElementById("ap-btn-fullscreen");
    if (!wrap || !btn) return { error: "no wrapper/button" };
    card._enterCssFullscreen();
    // Two quick taps that ORIGINATE on the fullscreen pill button. They bubble
    // up to the wrapper's zoom pointerdown listener — which must IGNORE them
    // (control target) so the wrapper never captures the pointer (the click that
    // exits fullscreen would otherwise retarget to the wrapper) and a double-tap
    // on the button never toggles the digital zoom. 2026-06-03 (#16 / zoom).
    const fire = (type, id) => btn.dispatchEvent(new PointerEvent(type, { pointerId: id, clientX: 10, clientY: 10, bubbles: true, cancelable: true }));
    fire("pointerdown", 1); fire("pointerup", 1);
    fire("pointerdown", 2); fire("pointerup", 2);
    const scale = card._zoom.scale;
    const captured = card._zoomPointers.size;
    card._exitCssFullscreen();
    card.remove();
    return { scale, captured };
  });
  expect(r.error, "card renders wrapper + fullscreen button").toBeUndefined();
  expect(r.scale, "double-tap on the button must NOT zoom").toBe(1);
  expect(r.captured, "the wrapper must not capture the button's pointer").toBe(0);
});

// Audio pill: renders in the pill bar, reflects the backend audio state, the tap
// toggles it, and show_audio:false hides the whole control.
test("audio pill: grayed off-stream, active + state-true while streaming, hideable", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const states = { "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" } };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { ...base, states };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    const pill = () => card.shadowRoot.getElementById("ap-btn-audio");
    const hasPill = !!pill();
    const grayedOffStream = card.classList.contains("audio-inactive");
    // Simulate a live stream → pill activates and reflects mute state.
    card._liveVideoActive = true;
    const v = card.shadowRoot.getElementById("cam-video");
    if (v) v.muted = true;
    card._refreshAudioPill();
    const grayedOnStream = card.classList.contains("audio-inactive");
    const onWhileMuted = pill().classList.contains("on");
    if (v) { v.muted = false; v.volume = 1; }
    card._refreshAudioPill();
    const onAfterUnmute = pill().classList.contains("on");
    // second card with the option off → control hidden
    const card2 = document.createElement("bosch-camera-card");
    card2.setConfig({ camera_entity: "camera.test", apple_style: true, show_audio: false });
    card2.hass = { ...base, states };
    document.body.appendChild(card2);
    await new Promise((res) => setTimeout(res, 300));
    const hidden = card2.classList.contains("audio-hidden");
    card.remove(); card2.remove();
    return { hasPill, grayedOffStream, grayedOnStream, onWhileMuted, onAfterUnmute, hidden };
  });
  expect(r.hasPill, "audio pill is rendered in the control bar").toBe(true);
  expect(r.grayedOffStream, "pill is grayed out (audio-inactive) when not streaming").toBe(true);
  expect(r.grayedOnStream, "pill becomes active once a live stream plays").toBe(false);
  expect(r.onWhileMuted, "pill is off while the video is muted").toBe(false);
  expect(r.onAfterUnmute, "pill turns on once the video is unmuted").toBe(true);
  expect(r.hidden, "show_audio:false hides the audio control").toBe(true);
});

// Both config editors must localise their labels from hass.language: German
// for "de*", English for everything else (the universal fallback). Regression
// for the editors previously being hard-coded German only.
test("config editors localise labels by hass.language (de/en)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card-editor"), null, { timeout: 10000 });

  const r = await page.evaluate(async () => {
    const mkHass = (lang) => ({ language: lang, localize: () => "", states: {}, config: {}, callService: () => {} });
    const labels = async (tag, lang) => {
      const ed = document.createElement(tag);
      ed.setConfig({});
      ed.hass = mkHass(lang);
      document.body.appendChild(ed);
      await new Promise((res) => setTimeout(res, 120));
      const text = ed.shadowRoot ? ed.shadowRoot.textContent : "";
      ed.remove();
      return text;
    };
    return {
      singleEn: await labels("bosch-camera-card-editor", "en"),
      singleDe: await labels("bosch-camera-card-editor", "de"),
      singleFr: await labels("bosch-camera-card-editor", "fr"),
      singleZh: await labels("bosch-camera-card-editor", "zh-Hans"),
      singlePtBr: await labels("bosch-camera-card-editor", "pt-BR"),
      overviewEn: await labels("bosch-camera-overview-card-editor", "en"),
      overviewDe: await labels("bosch-camera-overview-card-editor", "de"),
    };
  });

  expect(r.singleEn, "single editor in English").toContain("Show audio button");
  expect(r.singleEn).toContain("Camera entity");
  expect(r.singleDe, "single editor in German").toContain("Audio-Button anzeigen");
  expect(r.singleDe).not.toContain("Show audio button");
  // Any of the integration's 11 languages resolves; region suffix (pt-BR) and
  // Chinese variants map onto the base table key.
  expect(r.singleFr, "single editor in French").toContain("Afficher le bouton audio");
  expect(r.singleFr).not.toContain("Show audio button");
  expect(r.singleZh, "single editor in Chinese (zh-Hans)").toContain("显示音频按钮");
  expect(r.singlePtBr, "pt-BR falls back to pt").toContain("Mostrar botão de áudio");
  expect(r.overviewEn, "overview editor in English").toContain("Columns");
  expect(r.overviewEn).toContain("Show audio button");
  expect(r.overviewDe, "overview editor in German").toContain("Spalten");
  expect(r.overviewDe).not.toContain("Columns");
});

// Regression (bug-hunt 2026-06-02): the schedule-rule list interpolates rule
// values into innerHTML. Text values must be HTML-escaped (_escHtml) and values
// placed inside double-quoted attributes must additionally escape the quote
// char (_escAttr) — otherwise a malicious rule id / time / weekday from the
// cloud API could break out and inject markup or attributes.
test("rule-list escaping helpers neutralise HTML and attribute injection", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });

  const r = await page.evaluate(() => {
    const card = document.createElement("bosch-camera-card");
    const html = card._escHtml('<img src=x onerror=alert(1)>');
    const attr = card._escAttr('" onmouseover="alert(1)');
    const attrNull = card._escAttr(null);
    return { html, attr, attrNull };
  });

  // Text context: angle brackets encoded → no live element.
  expect(r.html).not.toContain("<img");
  expect(r.html).toContain("&lt;img");
  // Attribute context: the double-quote that would close the attribute is encoded.
  expect(r.attr).not.toContain('"');
  expect(r.attr).toContain("&quot;");
  // Null/undefined is coerced safely, never the string "null"/"undefined" markup.
  expect(r.attrNull).toBe("");
});

// B1 regression: _stopLiveVideo() during auto-reconnect must NOT call
// exitPictureInPicture() — the PiP window should survive stall-recovery and
// HLS-reconnect cycles so the user's floating video stays open.
test("_reconnectingLiveVideo flag suppresses PiP exit during auto-reconnect", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });

  const r = await page.evaluate(() => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);

    // Track exitPictureInPicture calls via a mock
    let pipExitCalled = 0;
    const origDescriptor = Object.getOwnPropertyDescriptor(document, "pictureInPictureElement");

    // Simulate a PiP-active video element
    const fakeVideo = document.createElement("video");
    Object.defineProperty(document, "pictureInPictureElement", {
      configurable: true,
      get: () => fakeVideo,
    });
    const origExit = document.exitPictureInPicture;
    document.exitPictureInPicture = () => { pipExitCalled++; return Promise.resolve(); };

    // Stub internal video lookup to return our fakeVideo
    const sr = card.shadowRoot;
    const origGetById = sr.getElementById.bind(sr);
    sr.getElementById = (id) => id === "cam-video" ? fakeVideo : origGetById(id);

    // Case 1: normal teardown — PiP MUST be closed
    card._reconnectingLiveVideo = false;
    card._stopLiveVideo();
    const exitOnNormalStop = pipExitCalled; // expect 1

    pipExitCalled = 0;

    // Case 2: reconnect teardown — PiP must NOT be closed
    card._reconnectingLiveVideo = true;
    card._stopLiveVideo();
    const exitOnReconnect = pipExitCalled; // expect 0

    // Restore
    document.exitPictureInPicture = origExit;
    if (origDescriptor) Object.defineProperty(document, "pictureInPictureElement", origDescriptor);
    else delete document.pictureInPictureElement;

    return { exitOnNormalStop, exitOnReconnect };
  });

  expect(r.exitOnNormalStop, "normal teardown closes PiP (session expiry/privacy/unload)").toBe(1);
  expect(r.exitOnReconnect, "auto-reconnect does NOT close PiP — floating window survives").toBe(0);
});

// 2026-06-15 sound-after-reconnect regression: an auto-reconnect (stall/HLS
// fatal/Bosch 60-min session rotation) sets _reconnectingLiveVideo=true and
// re-starts the SAME stream with no new gesture. _stopLiveVideo() used to clear
// _unmuteOnStart unconditionally → the reconnected stream came back MUTED even
// with sound on. It must now PRESERVE the flag across a reconnect (and still
// clear it on a real, user-driven stop).
test("_unmuteOnStart survives an auto-reconnect teardown but clears on a real stop", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(() => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);
    // Reconnect teardown — flag must SURVIVE so the new stream's `playing`
    // re-applies sound.
    card._unmuteOnStart = true;
    card._reconnectingLiveVideo = true;
    card._stopLiveVideo();
    const afterReconnect = card._unmuteOnStart;
    // Real user stop — flag must CLEAR so a later auto-play stream can't unmute
    // itself outside a gesture.
    card._unmuteOnStart = true;
    card._reconnectingLiveVideo = false;
    card._stopLiveVideo();
    const afterRealStop = card._unmuteOnStart;
    return { afterReconnect, afterRealStop };
  });
  expect(r.afterReconnect, "reconnect preserves the armed start-unmute → sound comes back").toBe(true);
  expect(r.afterRealStop, "a real stop clears the start-unmute flag").toBe(false);
});

// 2026-06-15: the PiP button can only float a PLAYING <video> — there is nothing
// to pop out of a snapshot. So it must be greyed (disabled) until a live stream
// is running, then enable, then grey again on stop. (Thomas: PiP nur aktiv wenn
// Livestream läuft.)
test("PiP button is HIDDEN until a live stream runs (single card)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    // Force PiP capability so the test is deterministic across the e2e browser
    // matrix (WebKit/Firefox headless report document.pictureInPictureEnabled
    // false; we are testing the card's hide-until-live logic, not the engine).
    Object.defineProperty(document, "pictureInPictureEnabled", { value: true, configurable: true });
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}), states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    const btn = card.shadowRoot.getElementById("ap-btn-pip");
    const idleHidden = btn.hidden;            // no stream → hidden entirely
    card._liveVideoActive = true; card._reflectPipState();
    const liveHidden = btn.hidden;            // streaming → shown
    const liveDisabled = btn.disabled;        // shown + usable (no other PiP)
    card._liveVideoActive = false; card._reflectPipState();
    const stoppedHidden = btn.hidden;         // stopped → hidden again
    return { idleHidden, liveHidden, liveDisabled, stoppedHidden };
  });
  expect(r.idleHidden, "PiP hidden while no stream runs").toBe(true);
  expect(r.liveHidden, "PiP appears once the live stream is playing").toBe(false);
  expect(r.liveDisabled, "PiP is usable (not greyed) while live and no other camera floats").toBe(false);
  expect(r.stoppedHidden, "PiP hidden again after the stream stops").toBe(true);
});

// 2026-06-15: the first-interaction auto-unmute listener must only treat real
// activation keys (Enter/Space) as the gesture. A non-activation key (Tab,
// arrows, Esc, F-keys) is NOT a user activation — acting on it would unmute
// gesture-lessly (Chrome pauses → pause-guard re-mutes → silent loss) AND consume
// the one-shot listener, blocking recovery on the next real click. Those keys
// must be ignored and the listener must STAY armed.
test("auto-unmute ignores non-activation keys and stays armed; Enter disarms", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}), states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
        "switch.test_audio": { state: "on", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
      } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    card._armAutoUnmute();
    const armed = !!card._autoUnmuteHandler;
    // A navigation key must NOT disarm the one-shot listener.
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    const stillArmedAfterTab = !!card._autoUnmuteHandler;
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    const stillArmedAfterArrow = !!card._autoUnmuteHandler;
    // A real activation key consumes (disarms) the listener.
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    const armedAfterEnter = !!card._autoUnmuteHandler;
    return { armed, stillArmedAfterTab, stillArmedAfterArrow, armedAfterEnter };
  });
  expect(r.armed, "auto-unmute arms a document listener").toBe(true);
  expect(r.stillArmedAfterTab, "Tab does not disarm (not an activation key)").toBe(true);
  expect(r.stillArmedAfterArrow, "Arrow does not disarm (not an activation key)").toBe(true);
  expect(r.armedAfterEnter, "Enter (a real activation key) disarms the one-shot").toBe(false);
});

// 2026-06-15 multi-instance stale-flag regression: secondary (echo-muted) status
// must be re-evaluated LIVE from the registry, not cached once at register time.
// If the primary card is removed at runtime, the survivor used to keep its stale
// `secondary` flag and stay permanently muted with no way back.
test("secondary-audio status re-evaluates after the primary card is removed", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}),
      states: { "camera.shared": { state: "idle", attributes: { friendly_name: "S" }, last_updated: "2026-01-01T00:00:00Z" } } };
    const mk = () => {
      const c = document.createElement("bosch-camera-card");
      c.setConfig({ camera_entity: "camera.shared", apple_style: true });
      c.hass = base;
      document.body.appendChild(c);
      return c;
    };
    const A = mk(); // primary (registered first)
    const B = mk(); // secondary (same camera_entity)
    await new Promise((res) => setTimeout(res, 200));
    const aSecondaryInit = A._isSecondaryAudio();
    const bSecondaryInit = B._isSecondaryAudio();
    A.remove(); // primary leaves the dashboard → B should become primary
    const bSecondaryAfter = B._isSecondaryAudio();
    return { aSecondaryInit, bSecondaryInit, bSecondaryAfter };
  });
  expect(r.aSecondaryInit, "first card is primary (not secondary)").toBe(false);
  expect(r.bSecondaryInit, "second card for same entity starts secondary (echo-muted)").toBe(true);
  expect(r.bSecondaryAfter, "after the primary is removed the survivor is no longer secondary").toBe(false);
});

// 2026-06-15 tab-switch / bfcache recovery: _resumeLiveStreamIfNeeded restarts a
// stream that was torn down while hidden, but ONLY when the backend stream switch
// is still on, the card is connected, and nothing is already starting. It never
// auto-starts a stream the user stopped.

// 2026-06-19 PiP-freeze-on-tab-switch fix (Thomas: pip mac chrome — switch tab,
// video freezes after a while, return to tab resumes the page video but the PiP
// window stays frozen). Root cause: the 5s stall-checker setInterval is throttled
// to ~1×/min in a hidden tab, so a dead go2rtc track (AlexxIT/WebRTC#121 WS i/o
// timeout) is only recovered on tab-return — leaving PiP frozen. _scheduleLiveRecovery
// centralises a PiP-safe reconnect that the unthrottled WebRTC `mute` / connection-
// `failed` EVENTS can fire while hidden. These tests pin its gating + idempotency.
test("_scheduleLiveRecovery reconnects PiP-safely only when live+streaming+connected, and is idempotent", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));

    // Stub teardown so we don't touch a real <video>/hls; mimic the real one's
    // side effect of clearing _liveVideoActive, and capture the reconnect flag
    // value AT teardown time (must be true so _stopLiveVideo keeps the PiP window).
    const install = () => {
      let stopCount = 0, startCount = 0, flagAtStop = null;
      card._stopLiveVideo = () => { stopCount++; flagAtStop = card._reconnectingLiveVideo; card._liveVideoActive = false; };
      card._startLiveVideo = () => { startCount++; };
      return { stops: () => stopCount, starts: () => startCount, flagAtStop: () => flagAtStop };
    };

    // Case 1: live + streaming + connected → PiP-safe reconnect.
    let c1 = install();
    card._isStreaming = () => true;
    card._liveVideoActive = true; card._reconnectingLiveVideo = false; card._stoppingLiveVideo = false;
    card._scheduleLiveRecovery("test");
    const flagAtStop = c1.flagAtStop();          // expect true (PiP survives)
    await new Promise((res) => setTimeout(res, 2100));
    const restarted = c1.starts();               // expect 1 (after the 2s defer)
    const flagCleared = card._reconnectingLiveVideo; // expect false (cleared after restart)

    // Case 2: backend stream switch OFF → tears down but never restarts.
    let c2 = install();
    card._isStreaming = () => false;
    card._liveVideoActive = true; card._reconnectingLiveVideo = false;
    card._scheduleLiveRecovery("test");
    await new Promise((res) => setTimeout(res, 2100));
    const stoppedWhenNotStreaming = c2.stops();  // expect 1
    const restartedWhenNotStreaming = c2.starts(); // expect 0

    // Case 3: already reconnecting → idempotent no-op (no double teardown/reconnect).
    let c3 = install();
    card._isStreaming = () => true;
    card._liveVideoActive = true; card._reconnectingLiveVideo = true;
    card._scheduleLiveRecovery("test");
    const stoppedWhenAlreadyReconnecting = c3.stops(); // expect 0

    // Case 4: not live → no-op.
    let c4 = install();
    card._isStreaming = () => true;
    card._liveVideoActive = false; card._reconnectingLiveVideo = false;
    card._scheduleLiveRecovery("test");
    const stoppedWhenNotLive = c4.stops();       // expect 0

    return { flagAtStop, restarted, flagCleared, stoppedWhenNotStreaming,
      restartedWhenNotStreaming, stoppedWhenAlreadyReconnecting, stoppedWhenNotLive };
  });
  expect(r.flagAtStop, "_reconnectingLiveVideo is true at teardown so PiP survives the reconnect").toBe(true);
  expect(r.restarted, "restarts the stream after the 2s reconnect defer").toBe(1);
  expect(r.flagCleared, "clears the reconnect flag once the new stream is started").toBe(false);
  expect(r.stoppedWhenNotStreaming, "still tears the dead stream down even when the switch is off").toBe(1);
  expect(r.restartedWhenNotStreaming, "never restarts when the backend stream switch is off").toBe(0);
  expect(r.stoppedWhenAlreadyReconnecting, "idempotent: no second teardown while a reconnect is in flight").toBe(0);
  expect(r.stoppedWhenNotLive, "no-op when no live stream is active").toBe(0);
});

// Source pins for the un-throttled freeze-detection wiring (these survive in src
// even though the runtime paths need a real PiP window + go2rtc to exercise).
test("PiP-freeze fix is wired: rVFC heartbeat, track-mute + connection-failed recovery, frameFrozen escalation (source pin)", () => {
  // rVFC liveness heartbeat re-arms itself and stamps _boschLastFrameAt.
  expect(CARD_SRC).toMatch(/requestVideoFrameCallback/);
  expect(CARD_SRC).toMatch(/_boschLastFrameAt\s*=\s*performance\.now\(\)/);
  // The stall checker escalates on a presented-frame freeze, not only currentTime.
  expect(CARD_SRC).toMatch(/const\s+frameFrozen\s*=/);
  expect(CARD_SRC).toMatch(/frozen\s*\|\|\s*pausedWhileLive\s*\|\|\s*frameFrozen/);
  // WebRTC remote video-track `mute` → debounced PiP-safe recovery.
  expect(CARD_SRC).toMatch(/ev\.track\.onmute\s*=/);
  expect(CARD_SRC).toMatch(/_scheduleLiveRecovery\("webrtc video track muted/);
  // Persistent connection-state `failed` → recovery (live phase, not connect).
  expect(CARD_SRC).toMatch(/connectionstatechange/);
  expect(CARD_SRC).toMatch(/connectionState\s*===\s*"failed"/);
  // Teardown cancels the rVFC heartbeat and the track-mute debounce timer.
  expect(CARD_SRC).toMatch(/cancelVideoFrameCallback/);
  expect(CARD_SRC).toMatch(/this\._trackMuteTimer\s*\)\s*\{\s*clearTimeout/);
});

test("background-freeze fix is wired: un-throttled Web Worker stall heartbeat (source pin)", () => {
  // A Web Worker timer is NOT subject to Chrome's hidden-tab intensive
  // throttling, so it reads the un-throttled rVFC freeze signal every 5s even in
  // the background → PiP freeze caught in ~10s instead of ~60s. 2026-06-21.
  // Worker created from an inline Blob URL (no extra file, CSP worker-src blob:).
  expect(CARD_SRC).toMatch(/_startLiveStallWorker\s*\(\)/);
  expect(CARD_SRC).toMatch(/new\s+Worker\(/);
  expect(CARD_SRC).toMatch(/URL\.createObjectURL\(/);
  expect(CARD_SRC).toMatch(/this\._stallWorker\.onmessage\s*=\s*\(\)\s*=>\s*this\._liveStallTickFromWorker\(\)/);
  // The worker tick only acts while hidden + this card owns the PiP element, and
  // only on a real rVFC presented-frame freeze (iOS excepted), via the idempotent
  // _scheduleLiveRecovery — so it never double-fires with the setInterval checker.
  expect(CARD_SRC).toMatch(/_liveStallTickFromWorker\s*\(\)\s*\{/);
  expect(CARD_SRC).toMatch(/document\.visibilityState\s*!==\s*"hidden"/);
  expect(CARD_SRC).toMatch(/_scheduleLiveRecovery\("no presented frame >10s \(bg worker\)"\)/);
  // Teardown terminates the worker so it can't outlive the stream.
  expect(CARD_SRC).toMatch(/_stopLiveStallWorker\s*\(\)/);
  expect(CARD_SRC).toMatch(/this\._stallWorker\.terminate\(\)/);
});

test("_resumeLiveStreamIfNeeded restarts a torn-down stream only when streaming+connected", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}), states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));
    const mk = () => { let n = 0; const f = () => { n++; }; f.count = () => n; return f; };

    // Case 1: streaming + connected + not live → restarts (after the 500ms defer).
    let start1 = mk(); card._startLiveVideo = start1;
    card._isStreaming = () => true;
    card._liveVideoActive = false; card._startingLiveVideo = false; card._waitingForStream = false;
    card._resumeLiveStreamIfNeeded();
    await new Promise((res) => setTimeout(res, 650));
    const restartedWhenStreaming = start1.count();

    // Case 2: backend stream switch OFF → must NOT restart.
    let start2 = mk(); card._startLiveVideo = start2;
    card._isStreaming = () => false;
    card._liveVideoActive = false;
    card._resumeLiveStreamIfNeeded();
    await new Promise((res) => setTimeout(res, 650));
    const restartedWhenNotStreaming = start2.count();

    // Case 3: card disconnected → must NOT restart even if streaming.
    let start3 = mk(); card._startLiveVideo = start3;
    card._isStreaming = () => true;
    card._liveVideoActive = false;
    card.remove();
    card._resumeLiveStreamIfNeeded();
    await new Promise((res) => setTimeout(res, 650));
    const restartedWhenDetached = start3.count();

    return { restartedWhenStreaming, restartedWhenNotStreaming, restartedWhenDetached };
  });
  expect(r.restartedWhenStreaming, "restarts a torn-down stream when backend still streaming").toBe(1);
  expect(r.restartedWhenNotStreaming, "never restarts when the backend stream switch is off").toBe(0);
  expect(r.restartedWhenDetached, "never restarts on a disconnected card").toBe(0);
});

// 2026-06-22 background-tab freeze: a plain hidden (non-PiP) tab whose go2rtc
// signaling WS times out leaves the <video> on a frozen still with paused===false,
// which the resume path missed → "showed Live but was a standbild, only a browser
// reload fixed it". Like HA core's ha-web-rtc-player + AlexxIT/go2rtc, tear an
// unwatched hidden stream down after a grace and rebuild it fresh on return.
test("hidden non-PiP live stream schedules a teardown; tab-return cancels it (teardown-on-hidden)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}), states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));
    card._isStreaming = () => false; // keep the visible-branch resume a no-op

    // Case A: nothing live → no teardown scheduled.
    card._liveVideoActive = false;
    card._scheduleHiddenTeardown();
    const armedWhenIdle = !!card._hiddenTeardownTimer;

    // Case B: a live hidden stream → teardown scheduled.
    card._liveVideoActive = true;
    card._scheduleHiddenTeardown();
    const armedWhenLive = !!card._hiddenTeardownTimer;

    // Case C: a second schedule is idempotent (no stacked timers).
    const t1 = card._hiddenTeardownTimer;
    card._scheduleHiddenTeardown();
    const sameTimer = card._hiddenTeardownTimer === t1;

    // Case D: a visible tab cancels the pending teardown (the test page is visible).
    card._onVisibilityChange();
    const cancelledOnReturn = !card._hiddenTeardownTimer;

    card.remove();
    return { armedWhenIdle, armedWhenLive, sameTimer, cancelledOnReturn };
  });
  expect(r.armedWhenIdle, "no teardown is scheduled when nothing is live").toBe(false);
  expect(r.armedWhenLive, "a live hidden stream schedules a teardown").toBe(true);
  expect(r.sameTimer, "a second schedule call is idempotent (no stacked timers)").toBe(true);
  expect(r.cancelledOnReturn, "returning to the tab before the grace cancels the teardown").toBe(true);
});

// 2026-06-22 badge decoupling: "Live" must track REAL frame liveness, not just
// "a peer connection once existed". A frozen still (_liveStreamStalled) shows
// "Verbinde", never a lying "Live".
test("stream badge is decoupled from liveness: a stalled live stream shows connecting, not Live", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}), states: {
        "camera.test": { state: "streaming", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 250));
    const badge = () => card.shadowRoot.getElementById("stream-badge");

    // Live + fresh frames → "streaming".
    card._liveVideoActive = true;
    card._liveStreamStalled = false;
    card._update();
    const liveClass = badge().className;

    // Same live flag but frames frozen → must NOT be "streaming".
    card._liveStreamStalled = true;
    card._update();
    const stalledClass = badge().className;

    // A fresh frame clears the stall flag and re-renders while visible.
    card._setLiveStalled(false);
    const clearedClass = badge().className;

    card.remove();
    return { liveClass, stalledClass, clearedClass };
  });
  expect(r.liveClass, "a truly live stream shows the streaming badge").toContain("streaming");
  expect(r.stalledClass, "a frozen (stalled) stream is NOT shown as Live").not.toContain("streaming");
  expect(r.stalledClass, "a frozen stream shows the connecting badge instead").toContain("connecting");
  expect(r.clearedClass, "a fresh frame restores the streaming badge").toContain("streaming");
});

// Source pins for the background-tab freeze wiring (the runtime paths need a real
// hidden tab + go2rtc transport to fully exercise, so pin the structure in src).
test("background-tab freeze fix is wired: teardown-on-hidden, freeze-on-return, badge decoupling (source pin)", () => {
  // visibilitychange→hidden schedules a teardown of the unwatched stream.
  expect(CARD_SRC).toMatch(/document\.visibilityState\s*===\s*"hidden"/);
  expect(CARD_SRC).toMatch(/this\._scheduleHiddenTeardown\(\)/);
  expect(CARD_SRC).toMatch(/const\s+BACKGROUND_TEARDOWN_GRACE_MS\s*=\s*\d+/);
  // The teardown fires only while still hidden and exempts a watched PiP window.
  expect(CARD_SRC).toMatch(/if\s*\(document\.visibilityState\s*!==\s*"hidden"\)\s*return;\s*\/\/ came back during grace/);
  expect(CARD_SRC).toMatch(/if\s*\(ownsPip\)\s*return;\s*\/\/ PiP IS being watched/);
  // Freeze-on-return safety net: no fresh frame within 3s of return → reconnect.
  expect(CARD_SRC).toMatch(/_scheduleLiveRecovery\("no fresh frame within 3s of tab-return"\)/);
  expect(CARD_SRC).toMatch(/const\s+freshFrame\s*=/);
  expect(CARD_SRC).toMatch(/const\s+timeAdvanced\s*=/);
  // Badge decoupled from a bare live flag — a stalled still falls through to connecting.
  expect(CARD_SRC).toMatch(/this\._liveVideoActive\s*&&\s*!this\._liveStreamStalled/);
  expect(CARD_SRC).toMatch(/_setLiveStalled\s*\(\s*true\s*\)/);
  expect(CARD_SRC).toMatch(/_setLiveStalled\s*\(\s*false\s*\)/);
});

// 2026-06-22 bug hunt — PiP-freeze + livestream-stop fixes (source pins; the
// runtime paths need a real go2rtc transport + hidden tab to fully exercise).
test("2026-06-22 bug-hunt fixes are wired: stale-pc guard, getStats oracle, HLS escalate, B5/B6 (source pin)", () => {
  // B1: ev.track.onmute/onunmute must guard on pc identity so a CLOSED old pc's
  // late `mute` can't recover (= tear down) the healthy NEW stream. The guard must
  // appear in the onmute path AND inside its 6s debounce callback.
  expect(CARD_SRC).toMatch(/ev\.track\.onmute\s*=\s*\(\)\s*=>\s*\{[\s\S]*?if\s*\(this\._webrtcPc\s*!==\s*pc\)\s*return;/);
  expect(CARD_SRC).toMatch(/ev\.track\.onunmute\s*=\s*\(\)\s*=>\s*\{[\s\S]*?if\s*\(this\._webrtcPc\s*!==\s*pc\)\s*return;/);
  // The pc-identity guard must also be re-checked INSIDE the 6s mute debounce.
  const onmuteIdx = CARD_SRC.indexOf("ev.track.onmute =");
  const debounceSlice = CARD_SRC.slice(onmuteIdx, onmuteIdx + 1200);
  expect(debounceSlice).toMatch(/_trackMuteTimer\s*=\s*setTimeout\([\s\S]*?if\s*\(this\._webrtcPc\s*!==\s*pc\)\s*return;/);

  // getStats() framesDecoded freeze oracle — the cross-browser decoder-level signal.
  expect(CARD_SRC).toMatch(/async\s+_checkWebrtcFreeze\s*\(\)\s*\{/);
  expect(CARD_SRC).toMatch(/pc\.getStats\(\)/);
  expect(CARD_SRC).toMatch(/r\.type\s*===\s*"inbound-rtp"/);
  expect(CARD_SRC).toMatch(/framesDecoded/);
  expect(CARD_SRC).toMatch(/_scheduleLiveRecovery\("webrtc framesDecoded frozen >10s"\)/);
  expect(CARD_SRC).toMatch(/_scheduleLiveRecovery\("webrtc framesDecoded frozen >10s \(bg worker\)"\)/);
  // Oracle is iOS-excepted (thread-suspend false positive) and single-flight.
  expect(CARD_SRC).toMatch(/this\._statsCheckInFlight/);

  // B3: fatal HLS NETWORK_ERROR retries a few times then ESCALATES (no infinite loop).
  expect(CARD_SRC).toMatch(/this\._hlsNetworkErrorCount\s*=\s*\(this\._hlsNetworkErrorCount\s*\|\|\s*0\)\s*\+\s*1/);
  expect(CARD_SRC).toMatch(/_hlsNetworkErrorCount\s*<=\s*3/);

  // B5: WebRTC liveness on tab-return must NOT trust audio-clock currentTime on
  // iOS (HLS path may; non-iOS WebRTC-without-rVFC may as a last resort).
  expect(CARD_SRC).toMatch(/const\s+liveProof\s*=\s*freshFrame[\s\S]{0,80}?timeAdvanced\s*&&\s*\(!!this\._hls\s*\|\|\s*\(!rvfcSupported\s*&&\s*!this\._isIOS\(\)\)\)/);

  // B6: _waitForStreamReady must NOT dead-end at 90s — it re-arms a delayed re-poll.
  const wfsIdx = CARD_SRC.indexOf("if (attempt > 90)");
  const wfsSlice = CARD_SRC.slice(wfsIdx, wfsIdx + 1500);
  expect(wfsSlice).toMatch(/this\._waitingForStream\s*=\s*true;\s*\n\s*this\._waitForStreamReady\(\);/);
});

// 2026-06-22 runtime: the getStats() oracle returns "frozen" only when
// framesDecoded stops advancing for >10s on a live WebRTC stream, and resets its
// baseline as soon as frames advance again.
test("_checkWebrtcFreeze flags a decoder freeze and clears when frames advance", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));
    if (card._stopRefreshTimer) card._stopRefreshTimer();   // no timer can reset _streamTransport mid-await
    // Controllable clock so a ">10s ago" baseline stays a POSITIVE timestamp
    // (performance.now() is tiny in a fresh page → real-minus-11000 would go
    // negative and trip the code's `seenAt > 0` baseline guard).
    const realNow = performance.now.bind(performance);
    let clock = 1_000_000;
    performance.now = () => clock;
    let frames = 100;
    card._webrtcPc = {
      getStats: async () => new Map([
        ["a", { type: "inbound-rtp", kind: "video", framesDecoded: frames, bytesReceived: 9999 }],
      ]),
    };
    // First call: establishes the baseline at clock → never "frozen".
    card._streamTransport = "webrtc";
    const first = await card._checkWebrtcFreeze();
    // Advance the clock 11s with frames flat → frozen.
    clock += 11_000;
    card._streamTransport = "webrtc";
    const frozen = await card._checkWebrtcFreeze();
    // Frames advance → healthy again, baseline resets.
    card._streamTransport = "webrtc";
    frames = 130;
    const healthyAgain = await card._checkWebrtcFreeze();
    // HLS transport must never use this oracle, even with a stale baseline.
    clock += 11_000;
    card._streamTransport = "hls";
    const hlsNever = await card._checkWebrtcFreeze();
    performance.now = realNow;
    return { first, frozen, healthyAgain, hlsNever };
  });
  expect(r.first).toBe(false);
  expect(r.frozen).toBe(true);
  expect(r.healthyAgain).toBe(false);
  expect(r.hlsNever).toBe(false);
});

// 2026-06-15: the bfcache `pageshow` handler only acts on a real bfcache restore
// (event.persisted === true), not a normal load.
test("pageshow restarts the stream only when restored from bfcache (persisted)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));
    let calls = 0;
    card._resumeLiveStreamIfNeeded = () => { calls++; };
    window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: false }));
    const afterPlainLoad = calls;
    window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
    const afterBfcache = calls;
    return { afterPlainLoad, afterBfcache };
  });
  expect(r.afterPlainLoad, "a normal (non-bfcache) pageshow does not restart").toBe(0);
  expect(r.afterBfcache, "a bfcache restore (persisted) triggers a resume").toBe(1);
});

// 2026-06-15 leak fixes: _stopLiveVideo must detach the tap-to-play resume listener
// and the pause-guard listener so neither survives teardown / stacks on restart.
test("_stopLiveVideo detaches the tap-to-play and pause-guard listeners", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));
    const video = card.shadowRoot.getElementById("cam-video");
    // Simulate an armed tap-to-play resume + an attached pause-guard.
    card._tapToPlayResume = () => {};
    video._boschPauseGuard = true;
    video._boschPauseGuardFn = () => {};
    card._stopLiveVideo();
    return {
      tapResumeCleared: card._tapToPlayResume === null,
      pauseGuardCleared: video._boschPauseGuardFn === null && video._boschPauseGuard === false,
    };
  });
  expect(r.tapResumeCleared, "tap-to-play resume listener ref cleared on stop").toBe(true);
  expect(r.pauseGuardCleared, "pause-guard listener detached + flag reset on stop").toBe(true);
});

// 2026-06-15: the privacy placeholder must stack above the loading overlay
// (z-index 10) so toggling privacy while connecting shows the lock, not a spinner.
test("privacy placeholder sits above the loading overlay (z-index)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));
    const ph = card.shadowRoot.querySelector(".privacy-placeholder");
    const lo = card.shadowRoot.querySelector(".loading-overlay");
    return {
      privacyZ: ph ? parseInt(getComputedStyle(ph).zIndex, 10) : null,
      loadingZ: lo ? parseInt(getComputedStyle(lo).zIndex, 10) : null,
    };
  });
  expect(r.privacyZ, "privacy placeholder has an explicit z-index").toBe(11);
  expect(r.privacyZ > r.loadingZ, "privacy placeholder stacks above the loading overlay").toBe(true);
});

// 2026-06-17: offline card showed the "Bild wird geladen…" loading spinner ON TOP
// of the "Kamera Offline" overlay (screenshot: spinner + text bleeding through the
// offline message). Root cause: the "stream just stopped" transition re-raised the
// loading overlay AFTER the offline block hid it; the spinner only cleared on the
// 15 s safety timer. Fix: _setLoadingOverlay(true) is a hard no-op while _isOffline,
// and the stream-stop block is skipped when offline. Also pins the offline overlay
// is localized (title + last-seen prefix were hardcoded German for all languages).
test("offline overlay suppresses the loading spinner and localizes its text (#offline-overlap)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", status_entity: "sensor.test_status", apple_style: true });
    card.hass = {
      config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}),
      states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "Eingang" }, last_updated: "2026-01-01T00:00:00Z" },
        "sensor.test_status": { state: "offline", attributes: {}, last_changed: "2026-06-17T06:22:00Z", last_updated: "2026-06-17T06:22:00Z" },
      },
    };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 250));
    const sr = card.shadowRoot;
    const lo = sr.getElementById("loading-overlay");
    // Simulate a late refresh/stream-stop callback trying to raise the spinner
    // while offline — the JS guard must keep it hidden (this is the actual bug).
    card._setLoadingOverlay(true, "Bild wird geladen…");
    // AND simulate the path that bypasses _setLoadingOverlay entirely
    // (_restoreCachedImage / the template's default `visible` class): force the
    // class on directly — the CSS :host(.cam-offline) rule must still hide it.
    lo.classList.add("visible");
    const forcedOpacity = getComputedStyle(lo).opacity;
    return {
      isOffline: card._isOffline === true,
      offlineVisible: sr.getElementById("offline-overlay").classList.contains("visible"),
      loadingVisibleAfterGuard: lo ? lo.classList.contains("visible") : null,
      forcedOpacity,
      title: sr.getElementById("offline-title")?.textContent,
      subtitle: sr.getElementById("offline-subtitle")?.textContent,
    };
  });
  expect(r.isOffline, "card detected OFFLINE status").toBe(true);
  expect(r.offlineVisible, "offline overlay is shown").toBe(true);
  // The JS guard blocks _setLoadingOverlay(true); but even if a bypassing path
  // forces the .visible class, the CSS rule keeps the spinner invisible (opacity 0).
  expect(r.forcedOpacity, "loading spinner is forced to opacity 0 while offline (CSS guard, even when .visible)").toBe("0");
  expect(r.title, "offline title localized to active card language (en)").toBe("Camera Offline");
  expect(r.subtitle, "offline subtitle uses localized 'Last seen:' prefix, not hardcoded German").toContain("Last seen:");
});

// Source pin: the "stream just stopped" transition block must be gated on
// !this._isOffline so it never re-raises the refresh spinner / fires a snapshot
// against an unreachable camera (companion to the runtime test above).
test("stream-stop refresh block is skipped while the camera is offline (source pin)", () => {
  const marker = 'if (!isStreaming && this._lastStreaming !== null && this._lastStreaming !== isStreaming';
  const idx = CARD_SRC.indexOf(marker);
  expect(idx, "stream-stop transition block exists").toBeGreaterThan(-1);
  const line = CARD_SRC.slice(idx, CARD_SRC.indexOf("\n", idx));
  expect(line, "stream-stop block is guarded by !this._isOffline").toContain("!this._isOffline");
});

// 2026-06-17: _awaitingFresh was never cleared on an image-load error. With a
// cached image showing, a failed fresh fetch left _awaitingFresh=true, so the
// next cached-image load re-raised the semi-transparent "refreshing" overlay and
// it stuck on every 60s refresh cycle. Both error terminal paths now clear it.
test("_onImageError clears _awaitingFresh so the refreshing overlay can't stick", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}),
      states: { "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));
    // Case A: a cached image is already showing, fresh fetch then errors.
    card._imageLoaded = true;
    card._awaitingFresh = true;
    card._onImageError();
    const clearedWithImage = card._awaitingFresh === false;
    // Case B: cold start, exhaust the retry budget.
    card._imageLoaded = false;
    card._awaitingFresh = true;
    card._loadRetries = 99; // already past MAX_RETRIES → give-up branch
    card._onImageError();
    const clearedOnGiveUp = card._awaitingFresh === false;
    return { clearedWithImage, clearedOnGiveUp };
  });
  expect(r.clearedWithImage, "awaitingFresh cleared when error hits with an existing image").toBe(true);
  expect(r.clearedOnGiveUp, "awaitingFresh cleared when the retry budget is exhausted").toBe(true);
});

// 2026-06-17: when a camera drops OFFLINE mid-stream, the "stream just stopped"
// block is skipped (it is !this._isOffline-guarded), so _stopLiveVideo() was
// never called — a frozen <video> frame lingered under the overlay and
// _liveVideoActive stayed true, blocking the fresh start after recovery. The
// offline block now tears the live video down itself.
test("offline mid-stream tears down the live video so recovery isn't blocked", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", status_entity: "sensor.test_status", apple_style: true });
    const onlineHass = { config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}),
      states: {
        "camera.test": { state: "streaming", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
        "sensor.test_status": { state: "online", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
      } };
    card.hass = onlineHass;
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));
    // Pretend a live video is running, and spy on the teardown.
    let stopCalled = false;
    card._liveVideoActive = true;
    const realStop = card._stopLiveVideo.bind(card);
    card._stopLiveVideo = () => { stopCalled = true; card._liveVideoActive = false; realStop(); };
    // Camera drops offline.
    card.hass = { ...onlineHass, states: {
      "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:01Z" },
      "sensor.test_status": { state: "offline", attributes: {}, last_changed: "2026-06-17T06:22:00Z", last_updated: "2026-06-17T06:22:00Z" },
    } };
    await new Promise((res) => setTimeout(res, 150));
    return { stopCalled, liveVideoActive: card._liveVideoActive, isOffline: card._isOffline === true };
  });
  expect(r.isOffline, "camera detected offline").toBe(true);
  expect(r.stopCalled, "_stopLiveVideo was called when the camera dropped offline mid-stream").toBe(true);
  expect(r.liveVideoActive, "_liveVideoActive reset so post-recovery start isn't blocked").toBe(false);
});

// 2026-06-17: mobile app showed a BLACK tile for offline cameras while the
// desktop browser showed the last frame. Root cause: _updateImage early-returned
// for ANY offline camera, so the only image shown came from the card's
// localStorage cache (which the app's webview lacks). The backend still serves
// the last good frame for an offline camera, so the card must still fetch it once
// when it has nothing to show. It must NOT keep re-fetching once a frame is loaded.
test("offline camera still loads the backend frame once (mobile black-image fix)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", status_entity: "sensor.test_status" });
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}),
      states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T", access_token: "TOK" }, last_updated: "2026-01-01T00:00:00Z" },
        "sensor.test_status": { state: "offline", attributes: {}, last_changed: "2026-06-17T06:22:00Z", last_updated: "2026-06-17T06:22:00Z" },
      } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));
    const img = card.shadowRoot.getElementById("cam-img");
    // App scenario: no localStorage cache → nothing loaded yet.
    card._imageLoaded = false;
    card._imgTimestamp = 999;
    img.src = "";
    card._updateImage();
    const loadedWhenEmpty = (img.getAttribute("src") || "").includes("/api/camera_proxy/camera.test");
    // Once a frame is loaded, an offline camera won't produce a newer one → skip.
    card._imageLoaded = true;
    img.src = "data:image/jpeg;base64,SENTINEL";
    card._updateImage();
    const skippedWhenLoaded = img.getAttribute("src") === "data:image/jpeg;base64,SENTINEL";
    return { isOffline: card._isOffline === true, loadedWhenEmpty, skippedWhenLoaded };
  });
  expect(r.isOffline, "camera detected offline").toBe(true);
  expect(r.loadedWhenEmpty, "offline + no cached frame → still fetch the backend proxy image").toBe(true);
  expect(r.skippedWhenLoaded, "offline + already have a frame → no pointless re-fetch").toBe(true);
});

// 2026-06-17: a fresh mount with NO localStorage cache (the HA mobile app) left
// offline AND privacy tiles BLACK/GREY because _triggerFreshSnapshot() skips the
// image load under its privacy/connectivity guards, so #cam-img never got a src
// and the overlay (which blurs the image behind it) had nothing to blur. The
// offline and privacy blocks in _update() now schedule a load when !_imageLoaded.
test("offline camera schedules a backdrop image load on mount (no localStorage)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    try { Object.keys(localStorage).forEach((k) => k.startsWith("bosch_cam_") && localStorage.removeItem(k)); } catch (_) { /* */ }
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", status_entity: "sensor.test_status" });
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}), connected: true, connection: { connected: true },
      states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T", access_token: "TOK" }, last_updated: "2026-01-01T00:00:00Z" },
        "sensor.test_status": { state: "offline", attributes: {}, last_changed: "2026-06-17T06:22:00Z", last_updated: "2026-06-17T06:22:00Z" },
      } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 250));
    const img = card.shadowRoot.getElementById("cam-img");
    return { isOffline: card._isOffline === true, src: img?.getAttribute("src") || "" };
  });
  expect(r.isOffline, "camera offline").toBe(true);
  expect(r.src.includes("/api/camera_proxy/camera.test"), "offline mount scheduled a backdrop image load").toBe(true);
});

test("privacy mode schedules a backdrop image load on mount (no localStorage)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    try { Object.keys(localStorage).forEach((k) => k.startsWith("bosch_cam_") && localStorage.removeItem(k)); } catch (_) { /* */ }
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", privacy_entity: "switch.test_privacy" });
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}), connected: true, connection: { connected: true },
      states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T", access_token: "TOK" }, last_updated: "2026-01-01T00:00:00Z" },
        "switch.test_privacy": { state: "on", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
      } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 250));
    const img = card.shadowRoot.getElementById("cam-img");
    const ph = card.shadowRoot.getElementById("privacy-placeholder");
    return { privacyVisible: ph?.classList.contains("visible") === true, src: img?.getAttribute("src") || "" };
  });
  expect(r.privacyVisible, "privacy placeholder shown").toBe(true);
  expect(r.src.includes("/api/camera_proxy/camera.test"), "privacy mount scheduled a backdrop image load").toBe(true);
});

// 2026-06-17 (Thomas: "offline fullscreen werden die icons angezeigt"): the
// fullscreen rule force-shows .ap-pill-bar (display:flex !important, to restore it
// on overview tiles), which overrode the cam-offline hiding (no !important) → an
// offline camera showed its control icons in fullscreen. A higher-specificity
// .fs-active.cam-offline rule now keeps them hidden.
test("offline camera hides the control pill bar in fullscreen", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", status_entity: "sensor.test_status", apple_style: true });
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}),
      states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T", access_token: "TOK" }, last_updated: "2026-01-01T00:00:00Z" },
        "sensor.test_status": { state: "offline", attributes: {}, last_changed: "2026-06-17T06:22:00Z", last_updated: "2026-06-17T06:22:00Z" },
      } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));
    card.classList.add("fs-active"); // simulate CSS-fullscreen
    await new Promise((res) => setTimeout(res, 50));
    const pill = card.shadowRoot.querySelector(".ap-pill-bar");
    return { offline: card._isOffline === true, display: pill ? getComputedStyle(pill).display : "no-pill" };
  });
  expect(r.offline, "camera offline").toBe(true);
  expect(r.display, "control pill bar is hidden in fullscreen while offline").toBe("none");
});

// 2026-06-17: WebRTC over a plain-http LAN URL is broken in the iOS Companion app
// (WKWebView needs a secure context) and HA does NOT auto-fall-back to HLS once a
// camera claims WebRTC. _startWebRTC must bail fast on iOS+insecure so the caller
// drops to HLS. Desktop http keeps WebRTC (not iOS). Source pin.
test("WebRTC bails fast on iOS over an insecure context (source pin)", () => {
  const idx = CARD_SRC.indexOf("async _startWebRTC(");
  expect(idx, "_startWebRTC exists").toBeGreaterThan(-1);
  const body = CARD_SRC.slice(idx, idx + 1900);
  expect(body, "guards on isSecureContext").toContain("isSecureContext");
  expect(body, "iOS-specific guard (won't regress desktop http)").toContain("_isIOS()");
});

// 2026-06-17: trigger_snapshot fired for ALL cameras (no entity_id), so an
// overview with N cameras refreshed every camera on every tile's tick. The card
// now passes its own camera so the service can target just that one.
test("the card targets its own camera in trigger_snapshot calls", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    const calls = [];
    card._callService = (dom, svc, data) => { calls.push({ dom, svc, data }); };
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}), connected: true,
      connection: { connected: true },
      services: { bosch_shc_camera: { trigger_snapshot: {} } },
      states: { "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 150));
    card._triggerFreshSnapshot();
    const snap = calls.find((c) => c.svc === "trigger_snapshot");
    return { hasEntityId: !!snap && snap.data && snap.data.entity_id === "camera.test" };
  });
  expect(r.hasEntityId, "trigger_snapshot is called with entity_id === this card's camera").toBe(true);
});

// 2026-06-17: decoupled volume/mute were stored under global localStorage keys, so
// muting one camera overwrote every other card's saved state. Keys are now per-camera.
test("decoupled volume/mute localStorage keys are per-camera", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const mk = (id) => { const c = document.createElement("bosch-camera-card"); c.setConfig({ camera_entity: "camera." + id }); return c; };
    const a = mk("alpha"), b = mk("beta");
    return {
      volDiffer: a._cardVolKey() !== b._cardVolKey(),
      muteDiffer: a._cardMuteKey() !== b._cardMuteKey(),
      volHasCam: a._cardVolKey().includes("camera.alpha"),
    };
  });
  expect(r.volDiffer, "two cameras have different volume keys").toBe(true);
  expect(r.muteDiffer, "two cameras have different mute keys").toBe(true);
  expect(r.volHasCam, "volume key includes the camera entity").toBe(true);
});

// 2026-06-15: the maintenance banner gained a × dismiss button. The dismiss key is
// title + raw ISO window (NOT the formatted display string, NOT mState), so it
// survives a scheduled→active flap of the same window and locale formatting, and a
// genuinely new window re-shows.
test("maintenance banner × dismisses the window, survives state flap, re-shows for a new window", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-overview-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    try { localStorage.removeItem("bosch-maint-dismissed"); } catch (_) { /* blocked */ }
    const mkHass = (state, start, end) => ({ config: {}, language: "en", localize: () => "",
      callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
      states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T", brand: "Bosch" }, last_updated: "2026-01-01T00:00:00Z" },
        "sensor.bosch_maint": { state, attributes: {
          source: "rss:bosch", camera_relevant: true, title: "Kameras",
          scheduled_start: start, scheduled_end: end,
        }, last_updated: "2026-01-01T00:00:00Z" },
      } });
    const W1S = "2026-06-16T07:00:00Z", W1E = "2026-06-16T10:00:00Z";
    const card = document.createElement("bosch-camera-overview-card");
    card.setConfig({});
    card.hass = mkHass("scheduled", W1S, W1E);
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    const sr = card.shadowRoot;
    card._renderMaintenanceBanner();
    const shown = !!sr.querySelector(".bco-banner");
    const closeBtn = sr.querySelector(".bco-banner-close");
    const hasClose = !!closeBtn;
    if (closeBtn) closeBtn.click();
    const afterClose = !!sr.querySelector(".bco-banner");
    let stored = null; try { stored = localStorage.getItem("bosch-maint-dismissed"); } catch (_) { /* blocked */ }
    // Same window, state flaps scheduled → active → must STAY dismissed.
    card.hass = mkHass("active", W1S, W1E);
    card._renderMaintenanceBanner();
    const afterFlap = !!sr.querySelector(".bco-banner");
    // A genuinely new window (different ISO times) → must re-appear.
    card.hass = mkHass("scheduled", "2026-07-01T07:00:00Z", "2026-07-01T10:00:00Z");
    card._renderMaintenanceBanner();
    const newWindow = !!sr.querySelector(".bco-banner");
    try { localStorage.removeItem("bosch-maint-dismissed"); } catch (_) { /* blocked */ }
    return { shown, hasClose, afterClose, stored, afterFlap, newWindow };
  });
  expect(r.shown, "banner shows for a scheduled, camera-relevant maintenance").toBe(true);
  expect(r.hasClose, "banner has a × close button").toBe(true);
  expect(r.afterClose, "× hides the banner").toBe(false);
  expect(r.stored, "dismiss key = title|startISO|endISO (no state, no formatted text)")
    .toBe("Kameras|2026-06-16T07:00:00Z|2026-06-16T10:00:00Z");
  expect(r.afterFlap, "stays dismissed when state flaps scheduled→active (same window)").toBe(false);
  expect(r.newWindow, "re-appears for a genuinely new maintenance window").toBe(true);
});

// 2026-06-15: the privacy placeholder badge shows the last SNAPSHOT time by default
// (more recent than the last motion event); privacy_stale_source:"event" restores
// the old last-event behaviour.
test("privacy badge: last-snapshot by default, last-event when configured", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const mkHass = () => ({ config: {}, language: "en", localize: () => "",
      callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
      states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
        "switch.test_privacy_mode": { state: "on", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
        "sensor.test_last_event": { state: "2026-06-14T10:12:00Z", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
      } });
    const run = async (cfg) => {
      const card = document.createElement("bosch-camera-card");
      card.setConfig({ camera_entity: "camera.test", apple_style: true, ...cfg });
      card._lastSnapshotAt = Date.now();
      card.hass = mkHass();
      document.body.appendChild(card);
      await new Promise((res) => setTimeout(res, 250));
      card.hass = mkHass();   // re-push so _update runs with privacy ON
      const badge = card.shadowRoot.getElementById("privacy-stale-badge");
      const text = badge ? badge.textContent : "";
      card.remove();
      return text;
    };
    const snapText  = await run({});                              // default = snapshot
    const eventText = await run({ privacy_stale_source: "event" });
    return { snapText, eventText };
  });
  expect(r.snapText, "default badge uses the last-snapshot prefix").toContain("Last snapshot:");
  expect(r.eventText, "privacy_stale_source:event uses the last-event prefix").toContain("Last event:");
});
