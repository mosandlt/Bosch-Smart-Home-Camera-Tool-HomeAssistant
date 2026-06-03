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
    // Shorten the cooldown so the test leaves no long-lived interval running
    // (same Windows-worker-teardown guard as the privacy cooldown test).
    card._STREAM_COOLDOWN_MS = 400;
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
