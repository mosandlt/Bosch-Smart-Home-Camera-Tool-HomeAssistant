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

// Overlay flicker during stream start (2026-07-17, Thomas: "stream wird
// gestartet .. dann verschwindet das overlay komplett .. bild wird geladen
// .. dann ist stream plötzlich da"): a background/periodic snapshot fetch
// that happens to land mid-connect (e.g. snapshot_during_warmup) must not
// prematurely clear _streamConnecting or hide the connect overlay via
// _onImageLoaded — only clearOverlay() (fired on the video "playing" event)
// owns the real hide. Before the fix, any fresh (non-cache) image load
// unconditionally cleared _streamConnecting and called
// _setLoadingOverlay(false), silently killing the progressive
// _connectSteps text timeline and (once _waitingForStream also cleared a
// beat later) letting the overlay hide/reappear with a different message.
test("a fresh snapshot arriving mid-connect does not hide the overlay or clear _streamConnecting", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));

    // Simulate the mid-connect state: a stream start is in flight and the
    // overlay is showing the progressive "Stream wird gestartet…" text.
    card._imageLoaded = true;
    card._streamConnecting = true;
    card._waitingForStream = true;
    card._connectSteps = [setTimeout(() => {}, 999999)]; // non-empty, must survive
    const connectText = "Stream wird gestartet…";
    card._setLoadingOverlay(true, connectText);

    const overlay = card.shadowRoot.getElementById("loading-overlay");
    const loadText = card.shadowRoot.getElementById("loading-text");
    const visibleBefore = overlay.classList.contains("visible");
    const textBefore = loadText.textContent;

    // A background snapshot fetch delivers a fresh (non-cache) frame while
    // the connect sequence is still in flight.
    const img = card.shadowRoot.getElementById("cam-img");
    img.src = "https://example.invalid/snapshot.jpg"; // non-"data:" → isCache=false
    card._onImageLoaded();

    const visibleAfter = overlay.classList.contains("visible");
    const textAfter = loadText.textContent;
    const streamConnectingAfter = card._streamConnecting;
    const connectStepsSurvived = card._connectSteps !== null;

    card._connectSteps.forEach((t) => clearTimeout(t));
    card.remove();
    return {
      visibleBefore, textBefore, visibleAfter, textAfter,
      streamConnectingAfter, connectStepsSurvived, connectText,
    };
  });
  expect(r.visibleBefore, "overlay starts visible with connect text").toBe(true);
  expect(r.textBefore, "overlay shows the connect-phase text before the snapshot lands").toBe(r.connectText);
  expect(r.visibleAfter, "overlay stays visible after a mid-connect snapshot load").toBe(true);
  expect(r.textAfter, "connect-phase text is not stomped by the default loading text").toBe(r.connectText);
  expect(r.streamConnectingAfter, "_streamConnecting is not cleared while _waitingForStream is still true").toBe(true);
  expect(r.connectStepsSurvived, "_connectSteps timeline is not killed by an incidental snapshot load").toBe(true);
});

// Same bug, narrower window (bug-hunt finding, 2026-07-17 follow-up): right
// after a tap, _toggleStream sets _streamConnecting=true synchronously, but
// _waitingForStream only flips true on the NEXT async _update() pass (after
// HA pushes the switch state back over the websocket) — a real single-tick
// window where ONLY _streamConnecting is true. The first version of the
// overlay fix gated on `_waitingForStream || _startingLiveVideo` and missed
// this window entirely, so a snapshot landing at exactly this moment still
// reproduced the original flicker. This test replicates that exact narrow
// state (unlike the test above, which sets both flags together) to prove
// the fix's `stillConnecting` check also covers _streamConnecting itself.
test("a fresh snapshot arriving in the single-tick window right after tap (before _waitingForStream flips) still does not hide the overlay", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));

    card._imageLoaded = true;
    card._streamConnecting = true;
    card._waitingForStream = false;
    card._startingLiveVideo = false;
    const connectText = "Stream wird gestartet…";
    card._setLoadingOverlay(true, connectText);

    const overlay = card.shadowRoot.getElementById("loading-overlay");
    const loadText = card.shadowRoot.getElementById("loading-text");

    const img = card.shadowRoot.getElementById("cam-img");
    img.src = "https://example.invalid/snapshot.jpg"; // non-"data:" → isCache=false
    card._onImageLoaded();

    const visibleAfter = overlay.classList.contains("visible");
    const textAfter = loadText.textContent;
    const streamConnectingAfter = card._streamConnecting;

    card.remove();
    return { visibleAfter, textAfter, streamConnectingAfter, connectText };
  });
  expect(r.visibleAfter, "overlay stays visible in the tap-only window (before _waitingForStream flips)").toBe(true);
  expect(r.textAfter, "connect text is not stomped in the tap-only window").toBe(r.connectText);
  expect(r.streamConnectingAfter, "_streamConnecting survives a snapshot landing in the tap-only window").toBe(true);
});

// _waitForStreamReady's 90s dead-end must clear _startingLiveVideo too, not
// just _waitingForStream/_streamConnecting (bug-hunt finding, 2026-07-17,
// maintenance round). _startLiveVideo's own catch-block can delegate to
// _waitForStreamReady while leaving _startingLiveVideo=true (set at the top
// of _startLiveVideo) — every auto-recovery gate in the file (_update()'s
// auto-start gate, the 30s retry timer this same dead-end arms,
// _resumeLiveStreamIfNeeded) requires !_startingLiveVideo, so leaving it
// stuck true here permanently defeats every one of them despite this being
// the exact "B6" dead-end fix (2026-06-22) meant to prevent that class of
// bug in the first place.
test("_waitForStreamReady's 90s dead-end clears _startingLiveVideo (not just _waitingForStream)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card._hass = {
      states: { "camera.test": { state: "idle", attributes: {} } },
      config: {}, language: "en", localize: () => "",
      callService: () => {}, callWS: async () => ({}),
    };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));

    // Simulate the exact stuck-flag precondition: a stream-start attempt
    // delegated to _waitForStreamReady while _startingLiveVideo is still
    // true from _startLiveVideo's own top.
    card._waitingForStream = true;
    card._streamConnecting = true;
    card._startingLiveVideo = true;

    // attempt=91 drives straight into the ">90" dead-end branch — camera
    // state stays "idle" (never reports "streaming"), matching the timeout
    // scenario.
    card._waitForStreamReady(91);

    const startingLiveVideoAfter = card._startingLiveVideo;
    const waitingForStreamAfter = card._waitingForStream;
    const streamConnectingAfter = card._streamConnecting;
    // 2026-07-19: the dead-end used to silently hide the overlay with zero
    // feedback that the expected ~10-35s wait was blown 2-3x over. It should
    // now briefly show a distinct "taking longer than expected" message
    // instead of jumping straight to idle.
    const overlayTextRightAfter = card.shadowRoot.getElementById("loading-text")?.textContent;
    const overlayVisibleRightAfter = card.shadowRoot.getElementById("loading-overlay")?.classList.contains("visible");

    if (card._waitForStreamRetryTimer) clearTimeout(card._waitForStreamRetryTimer);
    card.remove();
    return { startingLiveVideoAfter, waitingForStreamAfter, streamConnectingAfter, overlayTextRightAfter, overlayVisibleRightAfter };
  });
  expect(r.waitingForStreamAfter, "_waitingForStream is cleared by the dead-end (pre-existing behavior)").toBe(false);
  expect(r.streamConnectingAfter, "_streamConnecting is cleared by the dead-end (pre-existing behavior)").toBe(false);
  expect(r.startingLiveVideoAfter, "_startingLiveVideo must also be cleared, or every auto-recovery gate stays defeated forever").toBe(false);
  expect(r.overlayVisibleRightAfter, "the dead-end briefly shows a message instead of silently hiding").toBe(true);
  expect(r.overlayTextRightAfter, "the dead-end message is the distinct 'taking longer' text, not silence").toBe("Taking longer than expected, still trying…");
});

// Companion test: the brief "taking longer than expected" message must
// actually clear itself (not get stuck) after its short timeout.
test("_waitForStreamReady's 90s dead-end message clears itself after a few seconds", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card._hass = {
      states: { "camera.test": { state: "idle", attributes: {} } },
      config: {}, language: "en", localize: () => "",
      callService: () => {}, callWS: async () => ({}),
    };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));
    card._waitingForStream = true;
    card._waitForStreamReady(91);
    // Wait past the message's own short auto-hide timeout.
    await new Promise((res) => setTimeout(res, 3000));
    const overlayVisibleAfter = card.shadowRoot.getElementById("loading-overlay")?.classList.contains("visible");
    if (card._waitForStreamRetryTimer) clearTimeout(card._waitForStreamRetryTimer);
    card.remove();
    return { overlayVisibleAfter };
  });
  expect(r.overlayVisibleAfter, "the message does not stick around forever").toBe(false);
});

// firstHass refresh-overlay must not stomp connect-phase text (bug-hunt
// finding, 2026-07-17 follow-up): the `hass` setter's firstHass block calls
// _setLoadingOverlay(true, loading_refreshing) unconditionally whenever a
// cached image is already showing — reproducible by reloading the page (or
// re-adding the card) while a stream is already connecting/warming, which
// stomped "Stream wird gestartet…" with "Aktualisiere…". Source-pin (rather
// than a full runtime firstHass simulation, which needs a prepopulated
// localStorage cache keyed to the card's storageKey before first mount)
// that the guard is present at the exact call site — same convention as
// the existing "clearOverlay clears _streamConnecting before hiding"
// ordering-pin test above.
test("firstHass refresh-overlay call is gated on no connect-sequence in flight (source pin)", () => {
  const marker = "if (this._imageLoaded && !_privacyNow && !_connectingNow) {";
  const idx = CARD_SRC.indexOf(marker);
  expect(idx, "firstHass refresh-overlay call is gated on _connectingNow").toBeGreaterThan(-1);
  const nearby = CARD_SRC.slice(idx, idx + 200);
  expect(nearby, "the gated call still shows the refreshing overlay when not connecting").toContain(
    'this._setLoadingOverlay(true, this._t("loading_refreshing"))',
  );
  const gateDefIdx = CARD_SRC.lastIndexOf("const _connectingNow =", idx);
  expect(gateDefIdx, "_connectingNow is defined before the gated call").toBeGreaterThan(-1);
  expect(CARD_SRC.slice(gateDefIdx, idx)).toMatch(
    /_connectingNow = this\._streamConnecting \|\| this\._waitingForStream \|\| this\._startingLiveVideo/,
  );
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
    // Re-arm delay is 5500ms (must clear backend switch.py's 5s _STREAM_COOLDOWN
    // — see REWARM_TURN_ON_DELAY_MS), not the old 1500ms.
    await new Promise((res) => setTimeout(res, 5800)); // let the off→on timeouts fire
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

// Fullscreen controls auto-hide (Thomas, 2026-07-13): the bottom pill-bar
// fades out after 10s of no pointer movement/touch while in fullscreen, and
// must NEVER affect the non-fullscreen view. Uses Playwright's clock API to
// fast-forward virtual time instead of sleeping for real seconds — the idle
// check runs on a 1s _armInterval (see _wireFsAutoHide), not a per-mousemove
// setTimeout, so ticking the clock forward exercises the real code path.
test("outside fullscreen the pill bar stays visible no matter how long we wait", async ({ page }) => {
  await page.clock.install();
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { ...base, states: { "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    return { wired: card._fsAutoHideWired, hidden: card.classList.contains("fs-controls-hidden") };
  });
  await page.clock.fastForward(60000); // 60s, well past the 10s fullscreen timeout
  const after = await page.evaluate(() => {
    const card = document.querySelector("bosch-camera-card");
    const hidden = card.classList.contains("fs-controls-hidden");
    card.remove();
    return hidden;
  });
  expect(r.wired, "auto-hide is never wired outside fullscreen").toBe(false);
  expect(r.hidden, "controls start visible").toBe(false);
  expect(after, "controls stay visible after 60s outside fullscreen").toBe(false);
});

test("fullscreen: pill bar auto-hides after 10s idle, reappears on pointermove/tap, and un-hides on exit", async ({ page }) => {
  await page.clock.install();
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });

  const enter = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { ...base, states: { "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    // Force the pill-bar's opacity transition to apply synchronously
    // (inline style wins over the stylesheet's ".3s ease" rule on
    // specificity). page.clock.install() replaces this page's timers with
    // virtual ones, and WebKit's implementation appears to also stall
    // requestAnimationFrame / compositor-driven CSS transitions while a
    // fake clock is installed — under that condition the real transition
    // never progresses at all (not just slowly), so no amount of real-wall-
    // clock waiting/polling can observe it settle. The class toggle itself
    // (the actual application logic under test) is unaffected either way;
    // only the decorative fade's timing depends on rAF.
    card.shadowRoot.querySelector(".ap-pill-bar").style.transition = "none";
    card._enterCssFullscreen();
    return {
      wired: card._fsAutoHideWired,
      hiddenOnEnter: card.classList.contains("fs-controls-hidden"),
    };
  });
  expect(enter.wired, "auto-hide watcher wired on fullscreen enter").toBe(true);
  expect(enter.hiddenOnEnter, "controls visible right after entering fullscreen").toBe(false);

  // Idle 10s+ → hidden. With the transition disabled above, the opacity
  // change lands in the same tick as the class toggle — no real-wall-clock
  // wait needed.
  await page.clock.fastForward(10500);
  const idleState = await page.evaluate(() => ({
    hidden: document.querySelector("bosch-camera-card").classList.contains("fs-controls-hidden"),
    pillOpacity: getComputedStyle(
      document.querySelector("bosch-camera-card").shadowRoot.querySelector(".ap-pill-bar"),
    ).opacity,
  }));
  expect(idleState.hidden, "controls hidden after 10s idle in fullscreen").toBe(true);
  expect(idleState.pillOpacity, "pill bar faded to opacity 0").toBe("0");

  // Pointer movement resets it immediately.
  const afterMove = await page.evaluate(() => {
    const card = document.querySelector("bosch-camera-card");
    const wrap = card.shadowRoot.getElementById("img-wrapper");
    wrap.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, clientX: 50, clientY: 50 }));
    return card.classList.contains("fs-controls-hidden");
  });
  expect(afterMove, "pointermove immediately reshows the controls").toBe(false);

  // Idle again, this time verify a touch tap on the video reshows it too.
  await page.clock.fastForward(10500);
  const afterTouch = await page.evaluate(() => {
    const card = document.querySelector("bosch-camera-card");
    const wasHidden = card.classList.contains("fs-controls-hidden");
    const wrap = card.shadowRoot.getElementById("img-wrapper");
    const video = card.shadowRoot.getElementById("cam-video");
    // Dispatch on the video itself (not a pill-bar button) to match a real tap.
    const ev = new Event("touchstart", { bubbles: true, cancelable: true });
    Object.defineProperty(ev, "target", { value: video, configurable: true });
    wrap.dispatchEvent(ev);
    return { wasHidden, nowHidden: card.classList.contains("fs-controls-hidden") };
  });
  expect(afterTouch.wasHidden, "controls were hidden again after another 10s idle").toBe(true);
  expect(afterTouch.nowHidden, "tap on the video reshows the controls").toBe(false);

  // Exiting fullscreen must ALWAYS leave the controls visible + stop the timer,
  // even if we exit while mid-idle (no state may bleed into the normal view).
  await page.clock.fastForward(10500);
  const afterExit = await page.evaluate(() => {
    const card = document.querySelector("bosch-camera-card");
    const wasHiddenBeforeExit = card.classList.contains("fs-controls-hidden");
    card._exitCssFullscreen();
    const r = {
      wasHiddenBeforeExit,
      wired: card._fsAutoHideWired,
      hidden: card.classList.contains("fs-controls-hidden"),
    };
    card.remove();
    return r;
  });
  expect(afterExit.wasHiddenBeforeExit, "sanity: controls were hidden right before exit").toBe(true);
  expect(afterExit.wired, "auto-hide watcher torn down on fullscreen exit").toBe(false);
  expect(afterExit.hidden, "controls forced visible again on fullscreen exit").toBe(false);
});

test("fullscreen auto-hide timer is cleared by disconnectedCallback (no leak)", async ({ page }) => {
  await page.clock.install();
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { ...base, states: { "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    card._enterCssFullscreen();
    const idBefore = card._fsIdleTimer;
    card.remove(); // triggers disconnectedCallback while fullscreen-idle-watching
    return { idBefore, wiredAfter: card._fsAutoHideWired, timerAfter: card._fsIdleTimer };
  });
  expect(r.idBefore, "an interval id was armed while in fullscreen").not.toBeNull();
  expect(r.wiredAfter, "watcher flag cleared on removal").toBe(false);
  expect(r.timerAfter, "timer id cleared on removal").toBeNull();
});

// `fullscreen_auto_hide_controls` opt-out (Thomas, 2026-07-13 follow-up):
// default true (auto-hide as above, no YAML needed), set to false to keep the
// pre-fb3410c behavior — icons always visible in fullscreen AND no idle timer
// running in the background at all (not just visually suppressed).
test("fullscreen_auto_hide_controls defaults to enabled when unset", async ({ page }) => {
  await page.clock.install();
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { ...base, states: { "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    return { configValue: card._config.fullscreen_auto_hide_controls };
  });
  expect(r.configValue, "fullscreen_auto_hide_controls defaults to true (opt-out)").toBe(true);

  const enter = await page.evaluate(() => {
    const card = document.querySelector("bosch-camera-card");
    card._enterCssFullscreen();
    return { wired: card._fsAutoHideWired };
  });
  expect(enter.wired, "default config still wires the auto-hide watcher").toBe(true);

  await page.clock.fastForward(10500);
  await page.waitForTimeout(400);
  const after = await page.evaluate(() => {
    const card = document.querySelector("bosch-camera-card");
    const hidden = card.classList.contains("fs-controls-hidden");
    card._exitCssFullscreen();
    card.remove();
    return hidden;
  });
  expect(after, "default config still auto-hides after 10s idle").toBe(true);
});

test("fullscreen_auto_hide_controls:false keeps the pill bar always visible and never arms the idle timer", async ({ page }) => {
  await page.clock.install();
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const enter = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true, fullscreen_auto_hide_controls: false });
    card.hass = { ...base, states: { "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    card._enterCssFullscreen();
    return { wired: card._fsAutoHideWired, timer: card._fsIdleTimer };
  });
  // The whole point of the opt-out: no watcher, no listeners, no background
  // interval — not merely "hidden class never applied".
  expect(enter.wired, "watcher is never wired when the option is disabled").toBe(false);
  expect(enter.timer, "no idle interval is armed when the option is disabled").toBeNull();

  await page.clock.fastForward(60000); // well past the 10s timeout
  const after = await page.evaluate(() => {
    const card = document.querySelector("bosch-camera-card");
    const r = {
      hidden: card.classList.contains("fs-controls-hidden"),
      pillOpacity: getComputedStyle(card.shadowRoot.querySelector(".ap-pill-bar")).opacity,
      wired: card._fsAutoHideWired,
    };
    card._exitCssFullscreen();
    card.remove();
    return r;
  });
  expect(after.hidden, "controls never gain fs-controls-hidden with the option disabled").toBe(false);
  expect(after.pillOpacity, "pill bar stays fully opaque").toBe("1");
  expect(after.wired, "watcher stays unwired for the whole fullscreen session").toBe(false);
});

// Regression (bug-hunt agent #1, 2026-07-13): setConfig() previously only
// stored the new fullscreen_auto_hide_controls value into _config without
// re-syncing the watcher — _syncFsAutoHide() was normally only ever invoked
// from a real fullscreen enter/exit transition (_updateFullscreenButtonState),
// so toggling the option via the visual editor's live "config-changed" preview
// WHILE the card was already fullscreen had zero effect until the user
// exited and re-entered fullscreen. Fixed by re-syncing inside setConfig()
// whenever the card is already in fullscreen when it runs.
test("toggling fullscreen_auto_hide_controls via setConfig while already fullscreen takes effect immediately", async ({ page }) => {
  await page.clock.install();
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });

  // Case A: enabled -> disabled mid-fullscreen. The watcher must unwire and
  // the pill bar must never go on to hide even after another 10s+ idle.
  const caseA = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true }); // default: enabled
    card.hass = { ...base, states: { "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    card._enterCssFullscreen();
    const wiredBefore = card._fsAutoHideWired;
    // Simulate the editor's live-preview firing a config-changed event with
    // the option now disabled, WITHOUT leaving fullscreen first.
    card.setConfig({ camera_entity: "camera.test", apple_style: true, fullscreen_auto_hide_controls: false });
    return { wiredBefore, wiredAfterDisable: card._fsAutoHideWired, timerAfterDisable: card._fsIdleTimer };
  });
  expect(caseA.wiredBefore, "sanity: watcher was wired before the mid-fullscreen setConfig").toBe(true);
  expect(caseA.wiredAfterDisable, "watcher unwires immediately once disabled mid-fullscreen").toBe(false);
  expect(caseA.timerAfterDisable, "idle interval is cleared immediately once disabled mid-fullscreen").toBeNull();

  await page.clock.fastForward(60000);
  const caseAAfterIdle = await page.evaluate(() => {
    const card = document.querySelector("bosch-camera-card");
    const hidden = card.classList.contains("fs-controls-hidden");
    card._exitCssFullscreen();
    card.remove();
    return hidden;
  });
  expect(caseAAfterIdle, "controls never hide after being disabled mid-fullscreen, even after 60s idle").toBe(false);

  // Case B: disabled -> enabled mid-fullscreen. The watcher must wire up and
  // the idle-hide behavior must start working without a fullscreen re-entry.
  const caseB = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true, fullscreen_auto_hide_controls: false });
    card.hass = { ...base, states: { "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    card._enterCssFullscreen();
    const wiredBefore = card._fsAutoHideWired;
    card.setConfig({ camera_entity: "camera.test", apple_style: true, fullscreen_auto_hide_controls: true });
    return { wiredBefore, wiredAfterEnable: card._fsAutoHideWired };
  });
  expect(caseB.wiredBefore, "sanity: watcher was NOT wired before the mid-fullscreen setConfig").toBe(false);
  expect(caseB.wiredAfterEnable, "watcher wires immediately once re-enabled mid-fullscreen").toBe(true);

  await page.clock.fastForward(10500);
  await page.waitForTimeout(400);
  const caseBAfterIdle = await page.evaluate(() => {
    const card = document.querySelector("bosch-camera-card");
    const hidden = card.classList.contains("fs-controls-hidden");
    card._exitCssFullscreen();
    card.remove();
    return hidden;
  });
  expect(caseBAfterIdle, "controls auto-hide after 10s idle once re-enabled mid-fullscreen, no re-entry needed").toBe(true);
});

// Regression (bug-hunt agent #3, 2026-07-13): pointer-events is an INHERITED
// property, not a compositing one like opacity — the ancestor pill-bar's
// `:host(.fs-controls-hidden) .ap-pill-bar { pointer-events: none !important }`
// hides the volume popup VISUALLY via opacity (opacity applies to the whole
// subtree regardless of a descendant's own value), but .ap-vol-pop's own
// `:hover`/`.show` rule directly declares `pointer-events: auto` on itself —
// a direct declaration always wins over an inherited value, no matter the
// ancestor's !important. Without the fix, hovering the audio button and then
// going idle (mouse stationary, so pointermove never fires to reset the
// timer) left the invisible slider still draggable/clickable at that screen
// position.
test("fullscreen auto-hide also strips pointer-events from the (inherited-only) volume popup", async ({ page }) => {
  await page.clock.install();
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { ...base, states: { "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    card._enterCssFullscreen();
    const volPop = card.shadowRoot.getElementById("ap-vol-pop");
    if (!volPop) return { error: "no ap-vol-pop" };
    // Simulate the popup being open (mirrors the CSS :hover state that keeps
    // it interactive) via the pre-existing .show class hook, independent of
    // real :hover so the test doesn't depend on synthesizing mouse hover.
    volPop.classList.add("show");
    return { error: null };
  });
  expect(r.error).toBeNull();

  await page.clock.fastForward(10500);
  await page.waitForTimeout(400); // let the opacity transition settle

  const after = await page.evaluate(() => {
    const card = document.querySelector("bosch-camera-card");
    const volPop = card.shadowRoot.getElementById("ap-vol-pop");
    const pointerEvents = getComputedStyle(volPop).pointerEvents;
    // Read the hidden state BEFORE remove() — disconnectedCallback tears down
    // the auto-hide watcher and force-shows the controls again, which would
    // otherwise flip this back to false if read afterward.
    const hidden = card.classList.contains("fs-controls-hidden");
    card.remove();
    return { hidden, pointerEvents };
  });
  expect(after.hidden, "controls hidden after 10s idle").toBe(true);
  expect(after.pointerEvents, "volume popup is not clickable while faded, even with .show set").toBe("none");
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

// Regression (issue #45, realKim-dotcom): the Apple-style stream pill's title
// attribute was updated on every stream-state change via a hardcoded German
// ternary (`isStreaming ? "Live-Stream stoppen" : "Live-Stream starten"`),
// which silently overwrote the localized title `_render()` had set on mount.
// A German-profile user would never notice; any other language always saw
// German the moment the stream state first changed. Pins that the dynamic
// update now follows hass.language like the rest of the pill bar.
test("stream pill title follows hass.language on state change, not hardcoded German (#45)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });

  const r = await page.evaluate(async () => {
    const mkHass = (lang, streaming) => ({
      language: lang, localize: () => "", config: {}, callService: () => Promise.resolve(),
      states: {
        "camera.test": { state: streaming ? "streaming" : "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
        // _isStreaming() reads this switch first — must reflect the intended
        // idle/streaming state, not the camera entity's own state.
        "switch.test_live_stream": { state: streaming ? "on" : "off", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
      },
    });
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = mkHass("en", false);
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));
    const pill = card.shadowRoot.getElementById("ap-btn-stream");
    const enIdleTitle = pill?.getAttribute("title");
    // Force a genuine state change (idle -> streaming) so the dynamic-update
    // branch (not just the initial _render()) sets the title.
    card.hass = mkHass("en", true);
    await new Promise((res) => setTimeout(res, 200));
    const enStreamingTitle = pill?.getAttribute("title");
    // Same transition in German.
    card.hass = mkHass("de", false);
    await new Promise((res) => setTimeout(res, 200));
    card.hass = mkHass("de", true);
    await new Promise((res) => setTimeout(res, 200));
    const deStreamingTitle = pill?.getAttribute("title");
    card.remove();
    return { enIdleTitle, enStreamingTitle, deStreamingTitle };
  });

  expect(r.enIdleTitle, "English, idle: title in English").toBe("Start live stream");
  expect(r.enStreamingTitle, "English, streaming: title in English, not German").toBe("Stop live stream");
  expect(r.enStreamingTitle, "must not leak the old hardcoded German string").not.toContain("Live-Stream");
  expect(r.deStreamingTitle, "German, streaming: title in German").toBe("Live-Stream stoppen");
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

test("_scheduleLiveRecovery is a no-op while the tab is hidden and not PiP-owned, but runs when visible (keep-stream-alive fix)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));

    let stopCount = 0;
    card._stopLiveVideo = () => { stopCount++; card._liveVideoActive = false; };
    card._startLiveVideo = () => {};
    card._isStreaming = () => true;

    const setVis = (state) => Object.defineProperty(document, "visibilityState",
      { configurable: true, get: () => state });
    const origDesc = Object.getOwnPropertyDescriptor(Document.prototype, "visibilityState");

    // Hidden + not PiP-owned → suppressed (no teardown; the stream stays alive and
    // resumes on return). This is the whole point of the keep-alive change.
    setVis("hidden");
    card._liveVideoActive = true; card._reconnectingLiveVideo = false; card._stoppingLiveVideo = false;
    card._scheduleLiveRecovery("background blip");
    const stoppedWhileHidden = stopCount; // expect 0

    // Visible → recovery proceeds (someone is watching).
    setVis("visible");
    card._liveVideoActive = true; card._reconnectingLiveVideo = false; card._stoppingLiveVideo = false;
    card._scheduleLiveRecovery("real freeze on return");
    const stoppedWhileVisible = stopCount; // expect 1

    if (origDesc) Object.defineProperty(document, "visibilityState", origDesc);
    return { stoppedWhileHidden, stoppedWhileVisible };
  });
  expect(r.stoppedWhileHidden, "hidden non-PiP tab does NOT tear down the kept-alive stream").toBe(0);
  expect(r.stoppedWhileVisible, "a visible tab still recovers a genuinely frozen stream").toBe(1);
});

// Source pin: the teardown grace is long enough to keep the stream alive across an
// ordinary tab switch (no visible reconnect) — bumped 8s→60s on 2026-06-24.
test("background teardown grace keeps a quick tab switch static (source pin)", () => {
  expect(CARD_SRC).toMatch(/const BACKGROUND_TEARDOWN_GRACE_MS = 60000;/);
  // _scheduleLiveRecovery runs only when visible AND not PiP/native-fullscreen-owned.
  expect(CARD_SRC).toMatch(/if \(this\._ownsNativePresentation\(v\)\) return;\s*\n\s*if \(document\.visibilityState === "hidden"\) return;/);
});

// Source pin: the REAL freeze fix — `pagehide` (Chrome freezes a tab hidden ~5 min and
// fires it) must NOT tear down a stream this card is showing in a PiP window. Proven by
// a live getStats trace: a healthy stream was killed by the pagehide handler at the
// 5-min hidden mark. Real PiP can't be tested headless, so pin the guard. 2026-06-24.
test("pagehide does not tear down a PiP-owned stream (source pin)", () => {
  expect(CARD_SRC).toMatch(/_pagehideHandler = \(\) => \{[\s\S]*?if \(this\._ownsNativePresentation\(v\)\) return;[\s\S]*?this\._stopLiveVideo\(\);/);
});

// Behavioral: the PRIMARY PiP fix — while PiP is open the card dispatches HA's
// `hass-suspend-when-hidden` event with suspend:false so HA does NOT unmount the
// Lovelace panel after 5 min hidden (which was killing the stream + PiP). On PiP
// exit it restores, but never re-enables suspend for a user who had it off.
// LIVE-CONFIRMED: suspendWhenHidden flips true→false on PiP open. 2026-06-24.
test("_setBackgroundKeepAlive dispatches hass-suspend-when-hidden and restores correctly", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));

    const events = [];
    card.addEventListener("hass-suspend-when-hidden", (e) => events.push(e.detail.suspend));

    // Case A: prior setting = suspend ON (default) → keep-alive on fires false, off restores true.
    card._hass = { suspendWhenHidden: true };
    card._setBackgroundKeepAlive(true);
    const onFiredFalse = events[events.length - 1]; // expect false
    card._setBackgroundKeepAlive(false);
    const offRestoredTrue = events[events.length - 1]; // expect true (restore default)

    // Case B: user had suspend OFF in their profile → keep-alive on fires false,
    // off must NOT re-enable (no event fired by the off-call).
    card._hass = { suspendWhenHidden: false };
    const before = events.length;
    card._setBackgroundKeepAlive(true);   // fires false
    const onCount = events.length - before; // expect 1
    card._setBackgroundKeepAlive(false);  // must NOT fire (respect user's off)
    const offCount = events.length - before - 1; // expect 0

    return { onFiredFalse, offRestoredTrue, onCount, offCount, total: events.length };
  });
  expect(r.onFiredFalse, "keep-alive ON dispatches suspend:false").toBe(false);
  expect(r.offRestoredTrue, "keep-alive OFF restores suspend:true when default").toBe(true);
  expect(r.onCount, "keep-alive ON always dispatches once").toBe(1);
  expect(r.offCount, "keep-alive OFF does NOT re-enable suspend for a user who had it off").toBe(0);
});

// Source pin: keep-alive is wired into both PiP enter paths (W3C + webkit) and restored
// on both exit paths. 2026-06-24.
test("background keep-alive is wired on PiP enter/exit (source pin)", () => {
  expect(CARD_SRC).toMatch(/_setBackgroundKeepAlive\(on\) \{[\s\S]*?hass-suspend-when-hidden[\s\S]*?suspend: !on/);
  expect(CARD_SRC).toMatch(/enterpictureinpicture[\s\S]*?_setBackgroundKeepAlive\(true\)/);
  expect(CARD_SRC).toMatch(/leavepictureinpicture[\s\S]*?_setBackgroundKeepAlive\(false\)/);
});

// Regression: an intentional stop / privacy-ON must NOT be revived by the resume
// path. _resumeLiveStreamIfNeeded bails when privacy is ON even though the live
// stream switch (a SEPARATE entity) still reads "on". 2026-06-24 (kill-zombies).
test("_resumeLiveStreamIfNeeded does not revive the stream while privacy mode is ON", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));

    let startCount = 0;
    card._hass = {};                       // _resumeLiveStreamIfNeeded bails on !_hass otherwise
    card._startLiveVideo = () => { startCount++; };
    card._isStreaming = () => true;        // stream switch still "on"
    card._liveVideoActive = false;
    card._startingLiveVideo = false; card._waitingForStream = false; card._reconnectingLiveVideo = false;

    // privacy ON → must bail before _startLiveVideo, even with _isStreaming()=true.
    card._getEffectiveState = (id) => (id === card._entities.privacy ? "on" : "off");
    card._resumeLiveStreamIfNeeded();
    await new Promise((res) => setTimeout(res, 700)); // outlast the 500ms defer
    const startedUnderPrivacy = startCount; // expect 0

    // privacy OFF → resume is allowed again.
    card._getEffectiveState = () => "off";
    card._resumeLiveStreamIfNeeded();
    await new Promise((res) => setTimeout(res, 700));
    const startedWithoutPrivacy = startCount; // expect 1

    return { startedUnderPrivacy, startedWithoutPrivacy };
  });
  expect(r.startedUnderPrivacy, "privacy ON suppresses any live-video revival").toBe(0);
  expect(r.startedWithoutPrivacy, "privacy OFF allows the normal resume").toBe(1);
});

// Behavioral: with PiP simulated (document.pictureInPictureElement === cam-video),
// _scheduleLiveRecovery is a no-op (its null+load rebuild would freeze the PiP
// window); once PiP is gone it recovers normally. Matches HA core ha-web-rtc-player.
// 2026-06-24.
test("_scheduleLiveRecovery is suppressed while PiP-owned, runs once PiP is gone", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));

    let stopCount = 0;
    card._stopLiveVideo = () => { stopCount++; card._liveVideoActive = false; };
    card._startLiveVideo = () => {};
    card._isStreaming = () => true;

    const v = card.shadowRoot && card.shadowRoot.getElementById("cam-video");
    if (!v) return { skipped: true };
    // Simulate this card owning the PiP window (browser-authoritative path).
    Object.defineProperty(document, "pictureInPictureElement",
      { configurable: true, get: () => v });

    // PiP-owned → suppressed even though visible.
    card._liveVideoActive = true; card._reconnectingLiveVideo = false; card._stoppingLiveVideo = false;
    card._scheduleLiveRecovery("pip blip");
    const stoppedWhilePip = stopCount; // expect 0

    // PiP gone → recovery runs again.
    Object.defineProperty(document, "pictureInPictureElement",
      { configurable: true, get: () => null });
    card._liveVideoActive = true; card._reconnectingLiveVideo = false; card._stoppingLiveVideo = false;
    card._scheduleLiveRecovery("blip after pip closed");
    const stoppedAfterPip = stopCount; // expect 1

    return { stoppedWhilePip, stoppedAfterPip };
  });
  if (r.skipped) return; // cam-video not rendered in this harness build — source pins cover the wiring
  expect(r.stoppedWhilePip, "PiP-owned → no teardown (would freeze the floating window)").toBe(0);
  expect(r.stoppedAfterPip, "once PiP is gone, recovery runs normally").toBe(1);
});

// 2026-07-13 iOS PiP-after-fullscreen bug (Thomas, with screenshot): a native iOS
// PiP window hangs forever in its loading spinner after the user had previously
// been in iOS' own native <video> fullscreen presentation mode (webkitPresentationMode
// === "fullscreen" — distinct from our own CSS-fullscreen button path, which never
// touches this property). Root cause: `_scheduleLiveRecovery`'s ownsPip guard only
// checked PiP, not native fullscreen, so a stall/blip during native fullscreen ran
// srcObject=null + video.load() — the same teardown that corrupts PiP's compositor
// link (crbug 894317) — silently corrupting the <video> element for a LATER PiP
// request even after fullscreen was long since exited. `_ownsNativePresentation()`
// now also suppresses recovery while `webkitPresentationMode === "fullscreen"`.
test("_scheduleLiveRecovery is suppressed during iOS native <video> fullscreen, runs once it exits", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));

    let stopCount = 0;
    card._stopLiveVideo = () => { stopCount++; card._liveVideoActive = false; };
    card._startLiveVideo = () => {};
    card._isStreaming = () => true;

    const v = card.shadowRoot && card.shadowRoot.getElementById("cam-video");
    if (!v) return { skipped: true };

    // Simulate iOS native <video> fullscreen. On real WebKit (macOS/iOS),
    // webkitPresentationMode is a native getter-only IDL attribute — plain
    // assignment silently no-ops there (only Linux's WebKit build, which
    // doesn't implement this Apple-only property, let a plain expando
    // assignment "work" by accident). Object.defineProperty shadows the
    // accessor with an own data property on every engine.
    const definePresentationMode = (video, mode) =>
      Object.defineProperty(video, "webkitPresentationMode", { configurable: true, value: mode });
    definePresentationMode(v, "fullscreen");

    // Native-fullscreen-owned → suppressed even though visible and not in PiP.
    card._liveVideoActive = true; card._reconnectingLiveVideo = false; card._stoppingLiveVideo = false;
    card._scheduleLiveRecovery("blip during native fullscreen");
    const stoppedWhileFullscreen = stopCount; // expect 0

    // Back to inline ("" on most engines when not in any special presentation mode)
    // → recovery runs again, same as the PiP-gone case.
    definePresentationMode(v, "inline");
    card._liveVideoActive = true; card._reconnectingLiveVideo = false; card._stoppingLiveVideo = false;
    card._scheduleLiveRecovery("blip after fullscreen exit");
    const stoppedAfterFullscreen = stopCount; // expect 1

    return { stoppedWhileFullscreen, stoppedAfterFullscreen };
  });
  if (r.skipped) return; // cam-video not rendered in this harness build — source pins cover the wiring
  expect(r.stoppedWhileFullscreen, "native-fullscreen-owned → no teardown (would corrupt the compositor link, hanging a later PiP)").toBe(0);
  expect(r.stoppedAfterFullscreen, "once native fullscreen is exited, recovery runs normally").toBe(1);
});

// Unit-level pin for the shared helper itself: exercises all three ownership
// signals independently, plus the "none" case, plus the non-WebKit case where
// webkitPresentationMode is undefined (Chrome/Firefox) — must not throw or
// false-positive there. 2026-07-13.
test("_ownsNativePresentation recognizes PiP (W3C + iOS-webkit mirror) and iOS native fullscreen, is false and crash-safe otherwise", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));

    const results = {};

    // No video element at all → false, no throw.
    results.nullVideo = card._ownsNativePresentation(null);

    const v = document.createElement("video"); // detached fake element, not cam-video

    // Nothing active → false.
    results.plain = card._ownsNativePresentation(v);

    // W3C PiP (document.pictureInPictureElement === this exact element).
    const origDesc = Object.getOwnPropertyDescriptor(document, "pictureInPictureElement");
    Object.defineProperty(document, "pictureInPictureElement", { configurable: true, get: () => v });
    results.w3cPip = card._ownsNativePresentation(v);
    Object.defineProperty(document, "pictureInPictureElement", { configurable: true, get: () => null });

    // A DIFFERENT element in PiP must not falsely claim ownership for `v`.
    const other = document.createElement("video");
    Object.defineProperty(document, "pictureInPictureElement", { configurable: true, get: () => other });
    results.otherElementPip = card._ownsNativePresentation(v);
    Object.defineProperty(document, "pictureInPictureElement", { configurable: true, get: () => null });
    if (origDesc) Object.defineProperty(document, "pictureInPictureElement", origDesc);

    // Cross-browser safety: webkitPresentationMode undefined (Chrome/Firefox) must
    // not throw and must not be mistaken for "fullscreen".
    results.undefinedPresentationMode = card._ownsNativePresentation(v); // v.webkitPresentationMode is undefined here

    // iOS native fullscreen. On real WebKit (macOS/iOS), webkitPresentationMode
    // is a native getter-only IDL attribute — plain assignment silently no-ops
    // there (only Linux's WebKit build, which doesn't implement this Apple-only
    // property, let a plain expando assignment "work" by accident).
    // Object.defineProperty shadows the accessor with an own data property on
    // every engine.
    const definePresentationMode = (video, mode) =>
      Object.defineProperty(video, "webkitPresentationMode", { configurable: true, value: mode });
    definePresentationMode(v, "fullscreen");
    results.iosFullscreen = card._ownsNativePresentation(v);

    // iOS inline (not fullscreen, not PiP) → false.
    definePresentationMode(v, "inline");
    results.iosInline = card._ownsNativePresentation(v);

    return results;
  });
  expect(r.nullVideo, "no video element → false, no throw").toBe(false);
  expect(r.plain, "nothing active → false").toBe(false);
  expect(r.w3cPip, "this element in document.pictureInPictureElement → true").toBe(true);
  expect(r.otherElementPip, "a DIFFERENT element in PiP must not claim ownership of this one").toBe(false);
  expect(r.undefinedPresentationMode, "webkitPresentationMode undefined (non-WebKit) → false, no throw").toBe(false);
  expect(r.iosFullscreen, "iOS native <video> fullscreen → true").toBe(true);
  expect(r.iosInline, "iOS inline presentation mode → false").toBe(false);
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
  // (2026-07-13: timer clearing now routes through the shared _clearTimer
  // registry helper — Phase 4 point 14 — instead of a raw clearTimeout guard.
  // Scoped to the _stopLiveVideo body specifically: `_clearTimer(this._trackMuteTimer)`
  // also appears in the onmute/onunmute handlers, so an unscoped match would stay
  // green even if the actual teardown call were accidentally removed — bug-hunt
  // finding 2026-07-13.)
  expect(CARD_SRC).toMatch(/cancelVideoFrameCallback/);
  const stopLiveVideoStart = CARD_SRC.indexOf("_stopLiveVideo() {");
  const stopLiveVideoBody = CARD_SRC.slice(stopLiveVideoStart, CARD_SRC.indexOf("_onSnapshotClick()", stopLiveVideoStart));
  expect(stopLiveVideoBody).toMatch(/_clearTimer\(this\._trackMuteTimer\)/);
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

// 2026-08-08 bug fix (GitHub multi-camera "was streaming, then stopped"
// report). Two real, independently confirmed bugs (3-agent adversarial
// bug-hunt) in _maybeForceBackendRewarm's off→on self-heal cycle:
//   1. Its re-arm turn_on fired after only 1500ms, but backend switch.py's
//      _STREAM_COOLDOWN (5s) silently rejects any turn_on within 5s of the
//      last turn_off (WARNING-logged only, no exception) — so the rewarm's
//      own turn_on was rejected on EVERY cycle, foreground or not.
//   2. On top of that, a tab/app backgrounding right after turn_off could
//      throttle/drop the plain setTimeout entirely, and
//      _resumeLiveStreamIfNeeded's !_isStreaming() guard then reads the
//      stuck-OFF switch as "left off on purpose" and never revives it.
// Fixed via a single source of truth (_rewarmPending/_rewarmSwitchEntity)
// funnelled through _fireRewarmTurnOn from either the (now cooldown-safe)
// timer or _completeRewarmIfPending (tab-return path), each guarding so
// only one can ever actually call turn_on.
test("_maybeForceBackendRewarm re-arms past the backend's 5s cooldown, not before", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", switch_entity: "switch.test_live" });
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}), states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
        "switch.test_live": { state: "on", attributes: {} },
      } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));

    const calls = [];
    card._callService = (dom, svc, data) => calls.push({ t: Date.now(), call: `${dom}.${svc}:${data.entity_id}` });

    const started = Date.now();
    const initiated = card._maybeForceBackendRewarm();
    const pendingRightAfterCall = card._rewarmPending;
    const switchCapturedRightAfterCall = card._rewarmSwitchEntity;

    // Just after the backend's 5s cooldown should have cleared, turn_on
    // must have fired — and not a moment before 5000ms.
    await new Promise((res) => setTimeout(res, 5700));

    const turnOnCall = calls.find((c) => c.call === "switch.turn_on:switch.test_live");
    return {
      initiated,
      pendingRightAfterCall,
      switchCapturedRightAfterCall,
      turnOffFired: calls.some((c) => c.call === "switch.turn_off:switch.test_live"),
      turnOnDelayMs: turnOnCall ? turnOnCall.t - started : null,
      rewarmPendingAfter: card._rewarmPending,
    };
  });
  expect(r.initiated, "rewarm was actually initiated (not skipped by cooldown/missing switch)").toBe(true);
  expect(r.pendingRightAfterCall, "flags a pending turn_on immediately after turn_off").toBe(true);
  expect(r.switchCapturedRightAfterCall, "captures the switch entity at turn_off time").toBe("switch.test_live");
  expect(r.turnOffFired, "turn_off fired immediately").toBe(true);
  expect(r.turnOnDelayMs, "turn_on fires, and only after clearing the 5s backend cooldown").not.toBeNull();
  expect(r.turnOnDelayMs).toBeGreaterThanOrEqual(5000);
  expect(r.rewarmPendingAfter, "clears the pending flag once the turn_on actually fires").toBe(false);
});

test("_resumeLiveStreamIfNeeded completes a rewarm interrupted by backgrounding, without violating the cooldown", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", switch_entity: "switch.test_live" });
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {},
      callApi: async () => ({}), callWS: async () => ({}), states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));

    const calls = [];
    card._callService = (dom, svc, data) => calls.push({ t: Date.now(), call: `${dom}.${svc}:${data.entity_id}` });
    let start = 0; card._startLiveVideo = () => { start++; };

    // Simulate a rewarm's turn_off having fired 1s ago (well inside the
    // backend's 5s cooldown), then the tab backgrounding before the re-arm
    // timer could run — switch reads OFF now, but turn_on this instant
    // would still be rejected by the backend.
    const turnOffAt = Date.now();
    card._lastRewarmAt = turnOffAt - 1000;
    card._rewarmPending = true;
    card._rewarmSwitchEntity = "switch.test_live";
    card._rewarmTurnOnTimer = null; // simulate the original timer having been dropped entirely
    card._isStreaming = () => false;
    card._liveVideoActive = false;
    card._waitingForStream = false;

    card._resumeLiveStreamIfNeeded();
    // Immediately after: must NOT have fired turn_on yet (still inside cooldown).
    const firedImmediately = calls.some((c) => c.call === "switch.turn_on:switch.test_live");
    const stillPendingImmediately = card._rewarmPending;

    // Wait past the re-armed remaining wait (cooldown - 1s elapsed + margin).
    await new Promise((res) => setTimeout(res, 5200));

    return {
      firedImmediately,
      stillPendingImmediately,
      calls: calls.map((c) => c.call),
      rewarmPendingAfter: card._rewarmPending,
    };
  });
  expect(r.firedImmediately, "does not fire turn_on immediately while still inside the backend cooldown").toBe(false);
  expect(r.stillPendingImmediately, "leaves the rewarm pending, re-armed for the remaining wait").toBe(true);
  expect(r.calls, "turns the switch back on once the cooldown has genuinely cleared")
    .toContain("switch.turn_on:switch.test_live");
  expect(r.rewarmPendingAfter, "clears the pending flag once completed").toBe(false);
});

// 2026-07-19 bug-hunt finding: _resumeLiveStreamIfNeeded is called not just
// from visibilitychange→visible but also from leavepictureinpicture /
// webkitpresentationmodechanged→inline — neither implies the tab itself is
// visible. Starting/restarting a stream while genuinely hidden leaves it
// with no hidden-teardown timer ever armed (that's only armed from
// _onVisibilityChange's hidden branch), so it would run unwatched in the
// background indefinitely. Pin: hidden at any point (entry, or during the
// 500ms defer via a hide→show→hide race) must suppress the restart.
test("_resumeLiveStreamIfNeeded never starts/restarts a stream while the tab is hidden", async ({ page }) => {
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

    const setVis = (state) => Object.defineProperty(document, "visibilityState",
      { configurable: true, get: () => state });
    const origDesc = Object.getOwnPropertyDescriptor(Document.prototype, "visibilityState");

    // Case 1: hidden at the moment of the call (e.g. leavepictureinpicture
    // firing while the tab is backgrounded) → must not even queue a restart.
    let start1 = 0; card._startLiveVideo = () => { start1++; };
    card._isStreaming = () => true;
    card._liveVideoActive = false; card._startingLiveVideo = false; card._waitingForStream = false;
    setVis("hidden");
    card._resumeLiveStreamIfNeeded();
    await new Promise((res) => setTimeout(res, 650));
    const startedWhileHidden = start1;

    // Case 2: visible at the call, but goes hidden again during the 500ms
    // defer (rapid hide→show→hide) → the deferred restart must be suppressed.
    let start2 = 0; card._startLiveVideo = () => { start2++; };
    card._liveVideoActive = false; card._startingLiveVideo = false; card._waitingForStream = false;
    setVis("visible");
    card._resumeLiveStreamIfNeeded();
    setVis("hidden"); // flips back within the 500ms defer window
    await new Promise((res) => setTimeout(res, 650));
    const startedAfterHideDuringDefer = start2;

    // Control: genuinely visible throughout → restarts normally.
    let start3 = 0; card._startLiveVideo = () => { start3++; };
    card._liveVideoActive = false; card._startingLiveVideo = false; card._waitingForStream = false;
    setVis("visible");
    card._resumeLiveStreamIfNeeded();
    await new Promise((res) => setTimeout(res, 650));
    const startedWhileVisible = start3;

    if (origDesc) Object.defineProperty(document, "visibilityState", origDesc);
    card.remove();
    return { startedWhileHidden, startedAfterHideDuringDefer, startedWhileVisible };
  });
  expect(r.startedWhileHidden, "never starts a stream while the tab is hidden at call time").toBe(0);
  expect(r.startedAfterHideDuringDefer, "never starts a stream if the tab went hidden during the 500ms defer").toBe(0);
  expect(r.startedWhileVisible, "still restarts normally while genuinely visible (control)").toBe(1);
});

// 2026-07-19 bug-hunt finding: _showPlayGate's own header comment says it
// "hides the loading overlay so the spinner doesn't bleed through the gate"
// — but it only cleared _streamConnecting/_waitingForStream, not
// _startingLiveVideo. _setLoadingOverlay's anti-flicker guard suppresses
// ANY hide call while any of the three connect-phase flags is still true,
// so if a stream-stop lands while _startLiveVideo() is genuinely mid-flight
// (still holding _startingLiveVideo=true), _showPlayGate's own hide call
// silently no-ops — the exact "spinner bleeds through the gate" bug the
// function exists to prevent.
test("_showPlayGate actually hides the loading overlay even when _startingLiveVideo is still true", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 200));

    // Simulate a genuinely in-flight _startLiveVideo() call (e.g. still
    // awaiting the WebRTC offer round trip) plus a showing connect spinner.
    card._startingLiveVideo = true;
    card._streamConnecting = true;
    card._waitingForStream = true;
    card._setLoadingOverlay(true, "Stream wird gestartet…");
    const overlayVisibleBefore = card.shadowRoot.getElementById("loading-overlay")?.classList.contains("visible");

    card._showPlayGate();

    const startingLiveVideoAfter = card._startingLiveVideo;
    const overlayVisibleAfter = card.shadowRoot.getElementById("loading-overlay")?.classList.contains("visible");

    card.remove();
    return { overlayVisibleBefore, startingLiveVideoAfter, overlayVisibleAfter };
  });
  expect(r.overlayVisibleBefore, "sanity: the spinner was genuinely showing before the gate").toBe(true);
  expect(r.startingLiveVideoAfter, "_showPlayGate must also clear _startingLiveVideo").toBe(false);
  expect(r.overlayVisibleAfter, "the connect spinner must not bleed through the play gate").toBe(false);
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

// 2026-07-19: the legacy (non-apple-style) stream-label used to dump the raw
// internal state token ("offline"/"connecting"/"idle") straight into the DOM —
// not just unlocalized, but not even friendly text, unlike the apple-style
// badge a few lines below it which already mapped these to "Live"/"Offline"/
// _t("badge_connecting"). It must now use that same friendly mapping.
test("legacy stream-label shows friendly text, never a raw state token", async ({ page }) => {
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
    await new Promise((res) => setTimeout(res, 250));
    const label = () => card.shadowRoot.getElementById("stream-label").textContent;

    // idle (default, camera off / privacy on) → empty, matching the hidden
    // apple-badge counterpart, never the raw "idle" token.
    card._liveVideoActive = false;
    card._startingLiveVideo = false;
    card._update();
    const idleText = label();

    // connecting (stream starting) → localized "Connecting" (en), never "connecting".
    card._startingLiveVideo = true;
    card._update();
    const connectingText = label();
    card._startingLiveVideo = false;

    card.remove();
    return { idleText, connectingText };
  });
  expect(r.idleText, "idle state shows nothing, not the raw 'idle' token").toBe("");
  expect(r.connectingText, "connecting state shows friendly localized text").toBe("Connecting");
  expect(r.connectingText, "connecting state never shows the raw internal token").not.toBe("connecting");
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
  expect(CARD_SRC).toMatch(/if\s*\(this\._ownsNativePresentation\(video\)\)\s*return;\s*\/\/ PiP\/native-fullscreen IS being watched/);
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
  // 2026-07-13: armed via the shared _armTimer registry helper now (Phase 4
  // point 14) instead of a raw setTimeout.
  expect(debounceSlice).toMatch(/_trackMuteTimer\s*=\s*this\._armTimer\([\s\S]*?if\s*\(this\._webrtcPc\s*!==\s*pc\)\s*return;/);

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
  // Widened to 2200 for 2026-07-17 (maintenance-round bug-hunt): the dead-end
  // block gained a `this._startingLiveVideo = false;` reset (with explanatory
  // comment) so this auto-recovery gate doesn't stay defeated forever, pushing
  // the retry-timer match further down (distance measured at 2024).
  const wfsIdx = CARD_SRC.indexOf("if (attempt > 90)");
  const wfsSlice = CARD_SRC.slice(wfsIdx, wfsIdx + 2700);
  expect(wfsSlice).toMatch(/this\._waitingForStream\s*=\s*true;\s*\n\s*this\._waitForStreamReady\(\);/);
  // Same bug-hunt: pin that _startingLiveVideo is actually reset in this
  // block, not just _waitingForStream/_streamConnecting (distance measured
  // at 1063, past the explanatory comment; window widened generously past
  // that per a bug-hunt fragility flag, 2026-07-17).
  const deadEndSlice = CARD_SRC.slice(wfsIdx, wfsIdx + 1800);
  expect(deadEndSlice).toMatch(/this\._startingLiveVideo\s*=\s*false;/);
});

test("a stream that plays then repeatedly freezes escalates to a backend rewarm, not just an infinite same-source rebuild loop (2026-08-09)", async ({ page }) => {
  // Real-world report: over a remote connection, the stream plays fine, then
  // at some point starts looping "stalled/frozen, recovering" -> "no presented
  // frame >10s" forever, ONLY while HLS is the active transport, and never
  // self-heals. Root cause (fable-agent investigation): _deadStreamRecoveryStreak
  // only tracks "never rendered ANY frame this session" — a stream that played
  // successfully and THEN froze has neverRendered=false, so that streak (and
  // its backoff) never engages. The generic stall/freeze path had NO
  // escalation to a backend rewarm at all — only HLS's own FATAL error
  // handler could trigger one, and a silently-frozen manifest (e.g. a Bosch
  // LOCAL session renewal killing the backend source) reports only a
  // non-fatal bufferStall, never a fatal error — so the card looped
  // teardown+rebuild against the same dead source forever. This test verifies
  // the unconditional _stallRecoveryTimestamps window escalates to
  // _maybeForceBackendRewarm() after repeated recoveries even when frames DID
  // render previously (neverRendered=false throughout).
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "de", localize: (k) => k, callService: () => {}, callApi: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card.hass = { ...base, states: { "camera.test": { state: "streaming", attributes: {}, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));

    let rewarmCalls = 0;
    card._maybeForceBackendRewarm = () => { rewarmCalls++; return true; };
    card._stopLiveVideo = () => {};
    card._armTimer = () => 0;
    // neverRendered=false throughout — this stream DID render frames, matching
    // the reported "worked, then broke" case, not a "never worked" one.
    card._hasEverDecodedFrames = true;
    card._liveVideoActive = true;
    card._streamTransport = "hls";

    for (let i = 0; i < 3; i++) {
      card._reconnectingLiveVideo = false;
      card._liveVideoActive = true;
      card._scheduleLiveRecovery("no presented frame >10s");
    }
    const streakAfter3 = card._stallRecoveryTimestamps.length;

    card.remove();
    return { rewarmCalls, streakAfter3 };
  });
  expect(r.rewarmCalls, "backend rewarm triggered exactly once, on the 3rd consecutive stall recovery").toBe(1);
  expect(r.streakAfter3, "timestamp window clears once a rewarm is triggered").toBe(0);
});

test("stall escalation survives a brief real frame render between each stall (2026-08-09 regression)", async ({ page }) => {
  // The FIRST version of the fix above used a plain counter reset whenever a
  // frame rendered (clearOverlay's "playing" listener). Live-reproduced same
  // day: a flapping remote source plays ~8s every cycle (firing that exact
  // "playing" event) before stalling again — wiping a reset-on-frame counter
  // every single cycle, so it never reached 3 and the rewarm never fired,
  // even after 5+ consecutive stall/HLS-restart cycles. This test reproduces
  // that exact interleaving (recovery -> real "playing" event -> recovery)
  // and asserts the rewarm still fires, since the fix must survive brief
  // successful renders, not just a fully-dead source.
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "de", localize: (k) => k, callService: () => {}, callApi: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card.hass = { ...base, states: { "camera.test": { state: "streaming", attributes: {}, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));

    let rewarmCalls = 0;
    card._maybeForceBackendRewarm = () => { rewarmCalls++; return true; };
    card._stopLiveVideo = () => {};
    card._armTimer = () => 0;
    card._hasEverDecodedFrames = true;
    card._streamTransport = "hls";

    for (let i = 0; i < 3; i++) {
      card._reconnectingLiveVideo = false;
      card._liveVideoActive = true;
      card._scheduleLiveRecovery("no presented frame >10s");
      // Simulate the brief real render each cycle gets before stalling again
      // — the video element's "playing" event fires, running clearOverlay's
      // reset logic on every OTHER streak this class of code tracks.
      card._deadStreamRecoveryStreak = 0;
      card._streamDropCount = 0;
    }
    const streakAfter3 = card._stallRecoveryTimestamps.length;

    card.remove();
    return { rewarmCalls, streakAfter3 };
  });
  expect(r.rewarmCalls, "rewarm still fires after 3 stalls even with a brief real render between each").toBe(1);
  expect(r.streakAfter3).toBe(0);
});

test("a backend rewarm's own switch turn-on does not get stuck behind the manual tap-to-play gate (2026-08-09)", async ({ page }) => {
  // Live report same day: the rewarm escalation above fired correctly (backend
  // logs confirmed the switch cycled off/on and a fresh LOCAL RTSP session
  // opened) but the card never resumed playback on its own — the user had to
  // manually tap play, and even then it kept re-flapping. Root cause:
  // _evaluateGateForStreamTransition() runs on every _update() pass and shows
  // the manual tap-to-play gate on ANY switch OFF→ON transition unless
  // autoplay mode is "always" (or "lan" on a LAN session) — it can't tell an
  // internal recovery apart from an unrelated/automation-driven turn-on. On a
  // remote/tunneled session (autoplay mode "lan", the common default — and
  // exactly the case this rewarm exists for) that gate swallowed the
  // automatic resume completely. Fix: _fireRewarmTurnOn() sets a one-shot
  // _rewarmSkipGate flag consumed by _evaluateGateForStreamTransition() to
  // bypass the gate for that specific transition.
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "de", localize: (k) => k, callService: () => {}, callApi: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", switch_entity: "switch.test_live" });
    card.hass = {
      ...base,
      states: {
        "camera.test": { state: "streaming", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
        "switch.test_live": { state: "off", attributes: {} },
      },
    };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));

    // Force "lan"-only autoplay mode on a non-LAN (remote) session — the
    // exact combination that showed the gate for every unrelated turn-on.
    card._getAutoPlayMode = () => "lan";
    card._isLanSession = () => false;

    let showGateCalls = 0;
    card._showPlayGate = () => { showGateCalls++; };

    // Simulate _fireRewarmTurnOn()'s flag set (without going through the
    // full rewarm/service-call machinery, which isn't wired in this fixture).
    card._rewarmSkipGate = true;
    card._lastEvaluatedSwitchState = "off";
    card.hass = {
      ...base,
      states: {
        "camera.test": { state: "streaming", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
        "switch.test_live": { state: "on", attributes: {} },
      },
    };
    card._evaluateGateForStreamTransition();
    const gateSkippedOnRewarmTurnOn = showGateCalls === 0;
    const flagConsumed = card._rewarmSkipGate === false;

    // A SEPARATE, later OFF→ON transition (a real unrelated turn-on, not a
    // rewarm) must still show the gate normally — the flag is one-shot only.
    card._lastEvaluatedSwitchState = "off";
    card.hass = {
      ...base,
      states: {
        "camera.test": { state: "streaming", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
        "switch.test_live": { state: "on", attributes: {} },
      },
    };
    card._evaluateGateForStreamTransition();
    const gateStillShownForRealTurnOn = showGateCalls === 1;

    card.remove();
    return { gateSkippedOnRewarmTurnOn, flagConsumed, gateStillShownForRealTurnOn };
  });
  expect(r.gateSkippedOnRewarmTurnOn, "rewarm's own turn-on must not show the tap-to-play gate").toBe(true);
  expect(r.flagConsumed, "_rewarmSkipGate is one-shot").toBe(true);
  expect(r.gateStillShownForRealTurnOn, "an unrelated later turn-on still gates normally").toBe(true);
});

test("_rewarmSkipGate is consumed even when the camera is unavailable or the switch was already \"on\" (2026-08-09 bug-hunt)", async ({ page }) => {
  // 3 independent opus bug-hunt agents (2026-08-09) found the same leak: the
  // original placement consumed the flag AFTER the `prev === "on"` and
  // camera-unavailable early returns, so a rewarm landing while the camera
  // entity was still momentarily unavailable (very plausible — the rewarm
  // just tore the backend session down) — or HA's own state-push coalescing
  // skipping over the intermediate "off" so `prev` was already "on" — left
  // the flag stuck `true` forever, later silently un-gating some UNRELATED
  // automation-driven turn-on on a remote/never-autoplay session. Fix: the
  // flag is now consumed on the very first "on" observation, before either
  // of those returns.
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "de", localize: (k) => k, callService: () => {}, callApi: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", switch_entity: "switch.test_live" });
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    card._getAutoPlayMode = () => "lan";
    card._isLanSession = () => false;
    let showGateCalls = 0;
    card._showPlayGate = () => { showGateCalls++; };

    // Case A: camera is unavailable when the rewarm's "on" lands.
    card._rewarmSkipGate = true;
    card._lastEvaluatedSwitchState = "off";
    card.hass = {
      ...base,
      states: {
        "camera.test": { state: "unavailable", attributes: {} },
        "switch.test_live": { state: "on", attributes: {} },
      },
    };
    card._evaluateGateForStreamTransition();
    const flagConsumedDespiteUnavailableCamera = card._rewarmSkipGate === false;

    // Case B: prev was already "on" (state-push coalescing skipped "off").
    card._rewarmSkipGate = true;
    card._lastEvaluatedSwitchState = "on";
    card.hass = {
      ...base,
      states: {
        "camera.test": { state: "streaming", attributes: {} },
        "switch.test_live": { state: "on", attributes: {} },
      },
    };
    card._evaluateGateForStreamTransition();
    const flagConsumedDespitePrevAlreadyOn = card._rewarmSkipGate === false;

    // Neither case should ever have shown the gate.
    const gateNeverShown = showGateCalls === 0;

    card.remove();
    return { flagConsumedDespiteUnavailableCamera, flagConsumedDespitePrevAlreadyOn, gateNeverShown };
  });
  expect(r.flagConsumedDespiteUnavailableCamera, "flag must not leak past an unavailable-camera observation").toBe(true);
  expect(r.flagConsumedDespitePrevAlreadyOn, "flag must not leak past a prev-already-on observation").toBe(true);
  expect(r.gateNeverShown).toBe(true);
});

test("a quality-mode restart does not count toward the stall-recovery escalation window (2026-08-09 bug-hunt)", async ({ page }) => {
  // 2 independent bug-hunt agents found that "quality changed" — a
  // deliberate, user/config-driven restart of an otherwise HEALTHY stream —
  // was being counted by the same unconditional stall-timestamp push as a
  // genuine failure. 3 quick buffer-mode/quality toggles within the 90s
  // window would trip a full disruptive backend rewarm (switch off, ~5.5s
  // dead time, fresh session) instead of the intended cheap ~1s reconnect.
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "de", localize: (k) => k, callService: () => {}, callApi: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card.hass = { ...base, states: { "camera.test": { state: "streaming", attributes: {}, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));

    let rewarmCalls = 0;
    card._maybeForceBackendRewarm = () => { rewarmCalls++; return true; };
    card._stopLiveVideo = () => {};
    card._armTimer = () => 0;
    card._hasEverDecodedFrames = true;
    card._liveVideoActive = true;

    for (let i = 0; i < 5; i++) {
      card._reconnectingLiveVideo = false;
      card._liveVideoActive = true;
      card._scheduleLiveRecovery("quality changed");
    }
    const timestampsAfterFiveQualityChanges = card._stallRecoveryTimestamps.length;

    card.remove();
    return { rewarmCalls, timestampsAfterFiveQualityChanges };
  });
  expect(r.rewarmCalls, "5 quality-change restarts must never trigger a backend rewarm").toBe(0);
  expect(r.timestampsAfterFiveQualityChanges, "quality-change restarts must not push into the stall window").toBe(0);
});

test("the rewarm cooldown is not wiped by a brief render while a stall is still recent (2026-08-09 bug-hunt)", async ({ page }) => {
  // Bug-hunt finding: clearOverlay's "playing" handler unconditionally reset
  // _lastRewarmAt=0 on EVERY real frame — including the brief ~8s render a
  // flapping remote source gets every cycle before stalling again. That wiped
  // _maybeForceBackendRewarm()'s own 20s cooldown guard on every single
  // cycle, defeating it for exactly the case it exists to protect and
  // settling into a rewarm roughly every 20-45s indefinitely with no real
  // backoff. Fix: only release the cooldown early once the stall-recovery
  // timestamp window has actually gone quiet (length 0).
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "de", localize: (k) => k, callService: () => {}, callApi: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card.hass = { ...base, states: { "camera.test": { state: "streaming", attributes: {}, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));

    card._lastRewarmAt = 12345; // simulate a rewarm having just fired
    card._stallRecoveryTimestamps = [Date.now()]; // a stall is still "recent"

    // Simulate clearOverlay's real-frame path directly (constructing the full
    // WebRTC/video pipeline isn't needed to test this one guard).
    const video = document.createElement("video");
    document.body.appendChild(video);
    card._staleSourceSeen = true;
    if (!card._stallRecoveryTimestamps || card._stallRecoveryTimestamps.length === 0) {
      card._lastRewarmAt = 0;
    }
    const cooldownPreservedWhileStallRecent = card._lastRewarmAt === 12345;

    // Once the stall window is actually empty, the cooldown SHOULD release.
    card._stallRecoveryTimestamps = [];
    if (!card._stallRecoveryTimestamps || card._stallRecoveryTimestamps.length === 0) {
      card._lastRewarmAt = 0;
    }
    const cooldownReleasedOnceQuiet = card._lastRewarmAt === 0;

    video.remove();
    card.remove();
    return { cooldownPreservedWhileStallRecent, cooldownReleasedOnceQuiet };
  });
  expect(r.cooldownPreservedWhileStallRecent, "cooldown must survive a brief render while a stall is still recent").toBe(true);
  expect(r.cooldownReleasedOnceQuiet, "cooldown releases once the stall window is actually empty").toBe(true);

  // Pin that clearOverlay's REAL code contains this exact guard (the behavioral
  // test above exercises the same logic standalone, not through the actual
  // "playing" event listener) — source-anchor on the surrounding comment to
  // stay resilient to reformatting.
  const idx = CARD_SRC.indexOf("EXCEPT while a stall/freeze is still recent");
  expect(idx, "clearOverlay's cooldown-preservation comment must exist").toBeGreaterThan(-1);
  const slice = CARD_SRC.slice(idx, idx + 1000);
  expect(slice).toMatch(/if\s*\(\s*!this\._stallRecoveryTimestamps\s*\|\|\s*this\._stallRecoveryTimestamps\.length\s*===\s*0\s*\)\s*\{\s*\n\s*this\._lastRewarmAt\s*=\s*0;/);
});

test("_startLiveVideo re-entrancy guard prevents an orphaned attempt from destroying a newer, healthy hls.js instance (2026-08-09)", async ({ page }) => {
  // Real-world report: even after the LL-HLS/backoff/ontrack fixes, the
  // stream kept flashing to the snapshot and resuming. Root cause found via
  // opus audit: _startLiveVideo has two long awaits (camera/stream WS call,
  // hls.js CDN load) with no guard against a NEWER call superseding it while
  // parked there. A teardown+fresh-start racing an old attempt's pending
  // await let the OLD one resume afterward, destroy the NEW already-healthy
  // hls.js instance, and reattach media on top of it — producing exactly
  // the reported flash-then-resume cycle. This test reproduces the exact
  // race against the real _startLiveVideo()/_stopLiveVideo() code path.
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    let resolveFirstStreamCall;
    let callCount = 0;
    class FakeHls {
      static isSupported() { return true; }
      static get Events() { return { MANIFEST_PARSED: "m", FRAG_LOADED: "f", FRAG_BUFFERED: "b", ERROR: "e" }; }
      static get ErrorTypes() { return { NETWORK_ERROR: "net", MEDIA_ERROR: "media" }; }
      static get DefaultConfig() { return { loader: class {} }; }
      constructor() { this.destroyed = false; }
      on() {}
      loadSource() {}
      attachMedia() {}
      destroy() { this.destroyed = true; }
    }
    window.Hls = FakeHls;

    const base = { config: {}, language: "de", localize: (k) => k, callService: () => {}, callApi: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    // Block _update()'s own auto-start gate during setup — it would otherwise
    // race the manual _startLiveVideo() calls below the moment `hass` (with a
    // "streaming" camera state) is assigned, confusing the controlled race
    // this test is deliberately constructing.
    card._startingLiveVideo = true;
    card._waitingForStream = true;
    card.hass = {
      ...base,
      callWS: async (msg) => {
        if (msg.type === "camera/stream") {
          callCount++;
          if (callCount === 1) {
            return new Promise((res) => { resolveFirstStreamCall = () => res({ url: "http://x/stale.m3u8" }); });
          }
          return { url: "http://x/fresh.m3u8" };
        }
        return {};
      },
      states: { "camera.test": { state: "streaming", attributes: {}, last_updated: "2026-01-01T00:00:00Z" } },
    };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    card._preferHlsThisSession = true; // skip WebRTC, go straight to the HLS branch
    card._liveVideoActive = false;
    card._startingLiveVideo = false;
    card._waitingForStream = false;

    // Attempt #1: starts, gets stuck awaiting camera/stream (the WS call
    // never resolves until we call resolveFirstStreamCall() below).
    const p1 = card._startLiveVideo();
    await new Promise((res) => setTimeout(res, 20)); // let it reach the await

    // Simulate a teardown+fresh start superseding attempt #1 — exactly what
    // a real reconnect cycle does (_scheduleLiveRecovery -> _stopLiveVideo
    // -> _startLiveVideo) while the old attempt is still parked.
    card._stopLiveVideo();
    card._liveVideoActive = false;
    const p2 = card._startLiveVideo(); // attempt #2 resolves camera/stream immediately
    await p2;
    const healthyHls = card._hls;
    const healthyDestroyedBeforeResume = healthyHls?.destroyed;

    // Now let attempt #1 resume — this is the exact race that used to
    // destroy the healthy instance created by attempt #2.
    resolveFirstStreamCall();
    await p1;
    await new Promise((res) => setTimeout(res, 20));

    const healthyDestroyedAfterResume = healthyHls?.destroyed;
    const stillSameInstance = card._hls === healthyHls;

    card.remove();
    return { healthyDestroyedBeforeResume, healthyDestroyedAfterResume, stillSameInstance };
  });
  expect(r.healthyDestroyedBeforeResume, "sanity: healthy instance not destroyed yet").toBe(false);
  expect(r.healthyDestroyedAfterResume, "orphaned attempt #1 must NOT destroy the newer healthy hls instance").toBe(false);
  expect(r.stillSameInstance, "this._hls must still be the healthy instance, not replaced by the orphan").toBe(true);
});

test("pc.ontrack guards on pc identity like its sibling onmute/onunmute handlers (2026-08-09)", () => {
  // Real-world report: with sticky-HLS correctly active and hls.js confirmed
  // successfully fetching/playing the (LL-HLS-stripped) manifest, the stream
  // STILL flapped forever. Root cause: pc.ontrack had NO staleness guard,
  // unlike ev.track.onmute/onunmute right below it (the 2026-06-22 B1 fix) —
  // on a slow/lossy connection, an EARLIER WebRTC negotiation attempt (already
  // abandoned in favor of HLS) can complete asynchronously and fire ontrack
  // late, unconditionally overwriting video.srcObject/_streamTransport back to
  // "webrtc" and hijacking the video element out from under the working HLS
  // session — re-triggering the exact same dead-transport stall loop forever,
  // even though sticky-HLS correctly told _startLiveVideo not to retry WebRTC.
  const idx = CARD_SRC.indexOf("pc.ontrack = (ev) => {");
  expect(idx, "pc.ontrack handler found").toBeGreaterThan(-1);
  const body = CARD_SRC.slice(idx, idx + 1500);
  expect(body).toMatch(/pc\.ontrack\s*=\s*\(ev\)\s*=>\s*\{\s*(?:\/\/[^\n]*\n\s*)*if\s*\(this\._webrtcPc\s*!==\s*pc\)\s*return;/);
});

test("an abandoned _startWebRTC attempt cleans up its OWN pc, not a newer attempt's (2026-08-09)", () => {
  // verify-agent finding on the ontrack fix above: with ontrack now correctly
  // no-oping for a stale pc, an abandoned attempt's timeout/ICE-failed reject
  // becomes reliably reachable (previously a late ontrack could silently
  // resolve it instead). _startLiveVideo's catch block used to clean up via
  // `this._webrtcPc`/`this._webrtcUnsub` — instance fields that, for an
  // abandoned attempt, may already belong to a NEWER attempt (e.g. a
  // _stopLiveVideo()+_startLiveVideo() cycle raced this one's pending
  // timeout). That closed the new, LIVE pc out from under itself and then
  // fell through to start a duplicate HLS session on top of it. Fixed by
  // detecting the mismatch inside _startWebRTC itself (which still has its
  // own local `pc`/`unsub` closure) and marking the thrown error so the
  // caller does nothing further instead of touching state that no longer
  // belongs to it.
  const startIdx = CARD_SRC.indexOf("async _startWebRTC(");
  expect(startIdx, "_startWebRTC exists").toBeGreaterThan(-1);
  const webrtcBody = CARD_SRC.slice(startIdx, startIdx + 14000);
  expect(webrtcBody).toMatch(/if\s*\(this\._webrtcPc\s*!==\s*pc\)\s*\{\s*(?:\/\/[^\n]*\n\s*)*try\s*\{\s*pc\.close\(\);\s*\}\s*catch\s*\{\}/);
  expect(webrtcBody.includes("abandoned.webrtcAbandoned = true;"),
    "abandoned attempts are marked so the caller can distinguish them from a real failure").toBe(true);

  const callerIdx = CARD_SRC.indexOf("await this._startWebRTC(video, activateVideo);");
  expect(callerIdx, "_startWebRTC call site found").toBeGreaterThan(-1);
  const callerBody = CARD_SRC.slice(callerIdx, callerIdx + 2600);
  const abandonedCheckIdx = callerBody.indexOf("if (webrtcErr?.webrtcAbandoned) return;");
  expect(abandonedCheckIdx, "caller checks the abandoned marker").toBeGreaterThan(-1);
  const pcCleanupIdx = callerBody.indexOf("this._webrtcPc.close()");
  expect(pcCleanupIdx, "caller's normal pc cleanup found").toBeGreaterThan(-1);
  expect(abandonedCheckIdx, "abandoned check runs BEFORE the caller touches this._webrtcPc").toBeLessThan(pcCleanupIdx);
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
    // Frames advance → healthy again, baseline resets. Advance the clock a
    // little first (2026-07-13, Phase 4 point 13): _getStatsCached() now
    // shares one pc.getStats() snapshot for ~1s across callers, so back-to-back
    // calls with a literally unchanged clock (as real 5s-apart poll ticks never
    // are) would otherwise replay the previous cached (still-frozen) snapshot
    // instead of observing the frame advance.
    clock += 1_200;
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

// 2026-06-24: The old iOS+http guard (isSecureContext + _isIOS()) was REMOVED — it
// caused the Companion App on LAN (http://192.168.x.x) to instantly bail to HLS.
// Only the RTCPeerConnection-unavailable guard remains; the 5 s timeout + dead-track
// watchdog handle stalls gracefully. Source pins for this intentional removal.
test("_startWebRTC no longer has an iOS+http early-throw (source pin)", () => {
  const idx = CARD_SRC.indexOf("async _startWebRTC(");
  expect(idx, "_startWebRTC exists").toBeGreaterThan(-1);
  const body = CARD_SRC.slice(idx, idx + 1900);
  // The only remaining early guard is RTCPeerConnection undefined — no iOS-specific throw
  expect(body, "RTCPeerConnection guard still present").toContain("RTCPeerConnection");
  // isSecureContext guard intentionally removed — must not regress
  expect(body, "isSecureContext guard must not come back (breaks iOS LAN WebRTC)").not.toContain("isSecureContext");
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

// 2026-07-19 bug-hunt finding: every OTHER unmute path
// (_applyAudioPreference, _armStartUnmute, _tryStartUnmute) checks
// _isSecondaryAudio() before allowing sound — _toggleAudio() (the audio
// pill's own click handler) did not, so tapping the pill on a SECONDARY
// card instance for the same camera_entity (e.g. the same camera shown on
// an overview tile AND a detail view) directly unmuted that instance's
// own <video>, producing two audible instances at once (echo). Pin: a
// secondary instance's video stays muted regardless of tap direction, in
// both the decoupled (per-card) and shared-switch audio modes.
test("_toggleAudio never unmutes a secondary card instance for the same camera (anti-echo)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const hass = { config: {}, language: "en", localize: () => "", callService: async () => ({}),
      callApi: async () => ({}), callWS: async () => ({}), states: {
        "camera.echo": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      } };
    const mk = () => {
      const c = document.createElement("bosch-camera-card");
      c.setConfig({ camera_entity: "camera.echo" });
      c.hass = hass;
      document.body.appendChild(c);
      return c;
    };
    const primary = mk();
    const secondary = mk(); // registers second → _isSecondaryAudio() === true
    await new Promise((res) => setTimeout(res, 250));

    const rigVideo = (card) => {
      card._liveVideoActive = true;
      const v = card.shadowRoot.getElementById("cam-video");
      v.muted = true; // starts muted, user taps to unmute
      return v;
    };

    // Case 1: decoupled (per-card) audio mode.
    secondary._audioDecoupled = () => true;
    const vDecoupled = rigVideo(secondary);
    secondary._toggleAudio();
    const decoupledStaysMuted = vDecoupled.muted;

    // Case 2: shared-switch audio mode.
    secondary._audioDecoupled = () => false;
    secondary._entities = { ...secondary._entities, audio: "switch.echo_audio" };
    hass.states["switch.echo_audio"] = { state: "off", attributes: {} };
    const vShared = rigVideo(secondary);
    secondary._toggleAudio();
    const sharedStaysMuted = vShared.muted;

    // Case 3: desktop volume slider (_setVideoVolume) — a THIRD unmute path
    // found in a follow-up bug-hunt round, missed by the first two fixes
    // above since it's a separate control, not routed through _toggleAudio.
    const vSlider = rigVideo(secondary);
    secondary._setVideoVolume(0.8); // drag the slider up
    const sliderStaysMuted = vSlider.muted;

    // Control: the PRIMARY instance can genuinely unmute (not muted-forever).
    primary._audioDecoupled = () => true;
    const vPrimary = rigVideo(primary);
    primary._toggleAudio();
    const primaryUnmutes = !vPrimary.muted;

    primary.remove();
    secondary.remove();
    return { decoupledStaysMuted, sharedStaysMuted, sliderStaysMuted, primaryUnmutes };
  });
  expect(r.decoupledStaysMuted, "secondary stays muted in decoupled audio mode").toBe(true);
  expect(r.sharedStaysMuted, "secondary stays muted in shared-switch audio mode").toBe(true);
  expect(r.sliderStaysMuted, "secondary stays muted even via the volume slider").toBe(true);
  expect(r.primaryUnmutes, "control: the primary instance can still genuinely unmute").toBe(true);
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

// 2026-07-20: quality-selector REMOTE-fallback caveat. Bosch's REMOTE proxy
// rejects inst=4 ("low", ~1.9 Mbps) with a 400, so a REMOTE session clamps
// to inst=2 (~7.5 Mbps) — select.py's get_quality_remote_fallback_active()
// surfaces this as a `remote_fallback_active` attribute on the quality
// select entity so the card can warn the user instead of silently showing
// "Low" while streaming ~4x the promised bandwidth.
test("quality section shows a remote-fallback hint only when the entity attribute is set", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const mkHass = (attrs) => ({ config: {}, language: "en", localize: () => "",
      callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
      states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
        "select.test_video_quality": { state: "low", attributes: attrs, last_updated: "2026-01-01T00:00:00Z" },
      } });
    const run = async (attrs) => {
      const card = document.createElement("bosch-camera-card");
      card.setConfig({ camera_entity: "camera.test", quality_entity: "select.test_video_quality" });
      card.hass = mkHass(attrs);
      document.body.appendChild(card);
      await new Promise((res) => setTimeout(res, 250));
      card.hass = mkHass(attrs);   // re-push so _update runs
      const hint = card.shadowRoot.getElementById("quality-remote-fallback-hint");
      const visible = hint ? hint.style.display !== "none" : null;
      card.remove();
      return visible;
    };
    const withFallback = await run({ remote_fallback_active: true });
    const withoutFallback = await run({});
    return { withFallback, withoutFallback };
  });
  expect(r.withFallback, "hint shown when remote_fallback_active:true").toBe(true);
  expect(r.withoutFallback, "hint hidden when the attribute is absent").toBe(false);
});

// 2026-07-20 (Thomas, follow-up): quality_entity was opt-in-only with no
// default (unlike every sibling entity), so BoschCameraOverviewCard's
// child tiles never got a Quality control at all — the overview's
// per-camera setConfig calls never pass quality_entity. Now auto-derived
// as select.<camera>_video_quality, same pattern as switch/audio/light/etc.
test("quality select auto-derives select.<camera>_video_quality, reaching the overview's child tiles too", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card")
    && !!customElements.get("bosch-camera-overview-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const mkHass = () => ({ config: {}, language: "en", localize: () => "",
      callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
      states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
        "select.test_video_quality": { state: "auto", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
      } });

    const single = document.createElement("bosch-camera-card");
    single.setConfig({ camera_entity: "camera.test" });
    const singleDerived = single._entities.quality;
    single.remove();

    const overview = document.createElement("bosch-camera-overview-card");
    overview.setConfig({ include: ["camera.test"] });
    overview.hass = mkHass();
    document.body.appendChild(overview);
    await new Promise((res) => setTimeout(res, 250));
    const childCard = overview.shadowRoot.querySelector("bosch-camera-card");
    const overviewChildDerived = childCard ? childCard._entities.quality : null;
    const qualitySection = childCard ? childCard.shadowRoot.getElementById("quality-section") : null;
    const overviewChildQualityVisible = qualitySection ? qualitySection.style.display !== "none" : null;
    overview.remove();

    return { singleDerived, overviewChildDerived, overviewChildQualityVisible };
  });
  expect(r.singleDerived, "single card auto-derives the quality entity id").toBe("select.test_video_quality");
  expect(r.overviewChildDerived, "overview's child tile independently auto-derives its own quality entity id").toBe("select.test_video_quality");
  expect(r.overviewChildQualityVisible, "overview's child tile shows the quality section once the entity exists").toBe(true);
});

// Thomas (2026-07-28): LAN-fallback tiles (shown when a Bosch camera is
// unavailable/cloud-degraded but reachable on LAN) used to render in their
// own narrower grid ABOVE the main tier-sorted camera grid, so an offline
// camera always appeared first and at a different width than the online
// ones — breaking the existing "sort rule" (tier 0 live -> 1 privacy -> 2
// offline). The LAN-tiles slot now shares .bco-grid's column track and
// renders after it in the DOM.
test("LAN-fallback tiles share the main grid's width and render after it", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-overview-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const mkHass = () => ({ config: {}, language: "en", localize: () => "",
      callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
      states: {
        "camera.bosch_terrasse": { entity_id: "camera.bosch_terrasse", state: "idle", attributes: {
          brand: "Bosch", status: "ONLINE", friendly_name: "Terrasse",
        }, last_updated: "2026-01-01T00:00:00Z" },
        // Not named in `include:` below, so _discover() never surfaces it
        // as a real card — the LAN-fallback panel is the only place this
        // camera can show up, which is exactly what this test wants to
        // exercise (an explicit include list is the one remaining case
        // the LAN-fallback panel is still useful for, per the 2026-07-28
        // de-dup fix below).
        "camera.bosch_eingang": { entity_id: "camera.bosch_eingang", state: "unavailable", attributes: {},
          last_updated: "2026-01-01T00:00:00Z" },
        "binary_sensor.bosch_eingang_lan_reachable": { entity_id: "binary_sensor.bosch_eingang_lan_reachable", state: "off", attributes: {
          friendly_name: "Eingang LAN",
        }, last_updated: "2026-01-01T00:00:00Z" },
      } });

    const overview = document.createElement("bosch-camera-overview-card");
    overview.setConfig({ include: ["camera.bosch_terrasse"] });
    overview.hass = mkHass();
    document.body.appendChild(overview);
    await new Promise((res) => setTimeout(res, 250));

    const root = overview.shadowRoot;
    const wrap = root.querySelector(".bco-wrap");
    const grid = root.getElementById("bco-grid");
    const lanSlot = root.getElementById("bco-lan-tiles-slot");
    const children = [...wrap.children];
    const gridIndex = children.indexOf(grid);
    const lanIndex = children.indexOf(lanSlot);
    const lanTileCount = lanSlot.querySelectorAll(".bco-lan-tile").length;
    const gridColumnsMatch =
      getComputedStyle(grid).gridTemplateColumns === getComputedStyle(lanSlot.firstElementChild).gridTemplateColumns;

    overview.remove();
    return { gridIndex, lanIndex, lanTileCount, gridColumnsMatch };
  });
  expect(r.lanTileCount, "the offline camera got a LAN-fallback tile").toBe(1);
  expect(r.gridIndex, "main grid must come before the LAN-tiles slot").toBeLessThan(r.lanIndex);
  expect(r.gridColumnsMatch, "LAN tiles must use the same column track as the main grid").toBe(true);
});

// Thomas (2026-07-28 follow-up): without an explicit `include:` list (the
// real-world default — the dashboard's own card config never sets one),
// _discover() dropped a camera the moment it went truly HA-unavailable: HA
// strips attributes.brand along with everything else, so the brand==="Bosch"
// filter silently excluded it from the main grid forever — it only ever got
// the bare, image-less LAN-fallback tile, never a real <bosch-camera-card>
// with its own cached-frame + offline-overlay treatment. Every entity this
// integration creates is named camera.bosch_*, so that's now an accepted
// fallback for the brand check (mirrors the same convention
// _renderLanTiles() already uses for its own unavailable-camera detection).
test("a camera going unavailable keeps its real card (with cached image) instead of being dropped", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-overview-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const mkHass = (eingangState) => ({ config: {}, language: "en", localize: () => "",
      callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
      states: {
        "camera.bosch_terrasse": { entity_id: "camera.bosch_terrasse", state: "idle", attributes: {
          brand: "Bosch", status: "ONLINE", friendly_name: "Terrasse",
        }, last_updated: "2026-01-01T00:00:00Z" },
        "camera.bosch_eingang": eingangState,
      } });

    const overview = document.createElement("bosch-camera-overview-card");
    // No `include:` — matches the real dashboard's default config, which
    // relies entirely on the brand==="Bosch" attribute to find cameras.
    overview.setConfig({});
    overview.hass = mkHass({
      entity_id: "camera.bosch_eingang", state: "idle",
      attributes: { brand: "Bosch", status: "ONLINE", friendly_name: "Bosch Eingang" },
      last_updated: "2026-01-01T00:00:00Z",
    });
    document.body.appendChild(overview);
    await new Promise((res) => setTimeout(res, 250));

    const root = overview.shadowRoot;
    const findEingangCell = () => [...root.querySelectorAll(".bco-cell")]
      .find((cell) => cell._innerCard?._config?.camera_entity === "camera.bosch_eingang");
    const beforeCell = findEingangCell();
    const beforeTier = beforeCell ? beforeCell.dataset.tier : null;
    const beforeTitle = beforeCell ? beforeCell._innerCard._config.title : null;

    // Camera drops offline — HA strips ALL attributes (including brand)
    // once an entity is genuinely unavailable.
    overview.hass = mkHass({
      entity_id: "camera.bosch_eingang", state: "unavailable", attributes: {},
      last_updated: "2026-01-01T00:00:01Z",
    });
    await new Promise((res) => setTimeout(res, 50));

    const afterCell = findEingangCell();
    const afterTier = afterCell ? afterCell.dataset.tier : null;
    const sameCellReused = beforeCell === afterCell;

    overview.remove();
    return { beforeTier, beforeTitle, afterTier, cellSurvived: !!afterCell, sameCellReused };
  });
  expect(r.beforeTier, "camera starts online, tier 0").toBe("0");
  expect(r.beforeTitle, "card title derived from the friendly name").toBe("Eingang");
  expect(r.cellSurvived, "the card must NOT be pruned once the camera goes unavailable").toBe(true);
  expect(r.afterTier, "an offline camera moves to tier 2, it is not removed").toBe("2");
  expect(r.sameCellReused, "the existing cell/card DOM node is reused, not destroyed+recreated (keeps any cached image visible)").toBe(true);
});

// Thomas (2026-07-28, live report right after the fix above shipped): an
// unavailable camera now showed up TWICE — once as a real card in the main
// grid (the fix above), and AGAIN as the bare LAN-fallback tile, since
// _renderLanTiles() had no idea the main grid already covered it. Without
// an explicit include: list this now affects every offline camera, since
// _discover() itself always keeps a camera.bosch_* entity.
test("an unavailable camera does not also get a duplicate LAN-fallback tile", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-overview-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const mkHass = () => ({ config: {}, language: "en", localize: () => "",
      callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
      states: {
        "camera.bosch_terrasse": { entity_id: "camera.bosch_terrasse", state: "idle", attributes: {
          brand: "Bosch", status: "ONLINE", friendly_name: "Terrasse",
        }, last_updated: "2026-01-01T00:00:00Z" },
        "camera.bosch_eingang": { entity_id: "camera.bosch_eingang", state: "unavailable", attributes: {},
          last_updated: "2026-01-01T00:00:00Z" },
        "binary_sensor.bosch_eingang_lan_reachable": { entity_id: "binary_sensor.bosch_eingang_lan_reachable", state: "off", attributes: {
          friendly_name: "Eingang LAN",
        }, last_updated: "2026-01-01T00:00:00Z" },
      } });

    const overview = document.createElement("bosch-camera-overview-card");
    // No `include:` — the real dashboard's default config.
    overview.setConfig({});
    overview.hass = mkHass();
    document.body.appendChild(overview);
    await new Promise((res) => setTimeout(res, 250));

    const root = overview.shadowRoot;
    const realCardCount = [...root.querySelectorAll(".bco-cell")]
      .filter((cell) => cell._innerCard?._config?.camera_entity === "camera.bosch_eingang").length;
    const lanTileCount = root.getElementById("bco-lan-tiles-slot").querySelectorAll(".bco-lan-tile").length;

    overview.remove();
    return { realCardCount, lanTileCount };
  });
  expect(r.realCardCount, "the offline camera gets exactly one real card").toBe(1);
  expect(r.lanTileCount, "and NOT also a LAN-fallback tile — that would be a duplicate").toBe(0);
});

// 2026-07-20 follow-up bug-hunt: a plain `config.quality_entity || default`
// fallback has no way to opt out — "", null, and unset are all equally
// falsy and fall through to the auto-derived id. quality_entity: false must
// be a real, distinct "disable this section" signal.
test("quality_entity: false opts out of the auto-derived default", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(() => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", quality_entity: false });
    const derived = card._entities.quality;
    card.remove();
    return derived;
  });
  expect(r, "quality_entity:false must produce null, not the auto-derived default").toBe(null);
});

// 2026-07-20 follow-up (Thomas, live screenshot of the overview grid): the
// auto-appearing Quality section showed as its own always-visible row on
// minimal (overview-tile) cards, breaking the "glanceable grid, controls
// behind ⋮" design every other secondary control already follows.
test("quality section stays behind the ⋮ menu in minimal mode, like other secondary controls", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const mkHass = () => ({ config: {}, language: "en", localize: () => "",
      callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
      states: {
        "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
        "select.test_video_quality": { state: "auto", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
      } });
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", minimal: true });
    card.hass = mkHass();
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 250));
    card.hass = mkHass();

    const section = card.shadowRoot.getElementById("quality-section");
    const visibleClosed = section ? getComputedStyle(section).display !== "none" : null;
    card.classList.add("overflow-open");
    const visibleOpen = section ? getComputedStyle(section).display !== "none" : null;
    card.remove();
    return { visibleClosed, visibleOpen };
  });
  expect(r.visibleClosed, "quality section is hidden in minimal mode until ⋮ is opened").toBe(false);
  expect(r.visibleOpen, "quality section reveals in the overflow tray, same as other secondary controls").toBe(true);
});

// 2026-07-20 follow-up (Thomas, live report: "quality switch changes nothing
// on my livestream"). select.py's reconnect updates the backend's LOCAL
// RTSP/HLS source (Stream.update_source), but go2rtc dedups WebRTC stream
// registrations by exact URL string — an already-connected WebRTC viewer's
// PeerConnection just keeps decoding the OLD-quality stream until it
// renegotiates, which nothing did automatically. _onQualityChange must await
// the service call, then force a stop+restart of the card's own live view
// so a fresh WebRTC offer picks up the new quality.
test("_onQualityChange restarts the live view after the quality service call resolves", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", quality_entity: "select.test_video_quality" });
    let resolveCall;
    const callPromise = new Promise((res) => { resolveCall = res; });
    let calledWith = null;
    card._hass = {
      callService: (domain, service, data) => {
        calledWith = { domain, service, data };
        return callPromise;
      },
    };
    let recoveryCalledBeforeResolve = false;
    let recoveryCalledAfterResolve = false;
    card._liveVideoActive = true;
    card._scheduleLiveRecovery = (reason) => {
      if (calledWith && !resolved) recoveryCalledBeforeResolve = true;
      else recoveryCalledAfterResolve = true;
    };
    let resolved = false;
    const changePromise = card._onQualityChange("low");
    // Give the microtask queue a tick — recovery must NOT fire yet, the
    // service call promise hasn't resolved.
    await Promise.resolve();
    const firedTooEarly = recoveryCalledBeforeResolve;
    resolved = true;
    resolveCall();
    await changePromise;
    card.remove();
    return { calledWith, firedTooEarly, recoveryCalledAfterResolve };
  });
  expect(r.calledWith?.domain).toBe("select");
  expect(r.calledWith?.service).toBe("select_option");
  expect(r.calledWith?.data).toEqual({ entity_id: "select.test_video_quality", option: "low" });
  expect(r.firedTooEarly, "recovery must not fire before the service call resolves").toBe(false);
  expect(r.recoveryCalledAfterResolve, "recovery must fire once the backend reconnect completes").toBe(true);
});

// ── Dead-WebRTC-track → sticky HLS fallback (2026-06-23) ───────────────────────
// Regression for the "VERBINDE↔LIVE" flip on the iOS Companion app / cellular
// CGNAT: a WebRTC track arrives (ontrack fires, badge "Live") but never decodes a
// frame; every recovery path re-tried WebRTC into the same dark transport → HLS
// never reached, banner never shown (HA-core #158178). The dead-track watchdog
// (getStats framesDecoded + bytesReceived) must escalate to a STICKY HLS fallback.

// 2026-07-19 (forum 998974/42): user-controllable WebRTC connect escape
// hatches — disable_webrtc (skip the attempt entirely, always HLS) and
// webrtc_connect_timeout_ms (tune how long to wait before falling back),
// for setups (e.g. WireGuard) where the default 4s attempt is either
// never going to succeed or needs a different bound. Both card-level
// config helpers first, then the _startLiveVideo gate ordering, then the
// two editors (single-card + overview) and the overview's card_defaults
// propagation.

test("_disableWebRTC/_webrtcConnectTimeoutMs config helpers", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const mk = (config) => {
      const c = document.createElement("bosch-camera-card");
      c.setConfig({ camera_entity: "camera.test", ...config });
      return c;
    };
    const defaultOff = mk({});
    const explicitOn = mk({ disable_webrtc: true });
    const explicitOff = mk({ disable_webrtc: false });

    const defaultTimeout = mk({});
    const customTimeout = mk({ webrtc_connect_timeout_ms: 8000 });
    const tooLow = mk({ webrtc_connect_timeout_ms: 10 });        // must clamp up to 1000
    const tooHigh = mk({ webrtc_connect_timeout_ms: 999999 });   // must clamp down to 30000
    const nonNumeric = mk({ webrtc_connect_timeout_ms: "not-a-number" });
    const zero = mk({ webrtc_connect_timeout_ms: 0 });           // falsy but a real configured value

    return {
      defaultOff: defaultOff._disableWebRTC(),
      explicitOn: explicitOn._disableWebRTC(),
      explicitOff: explicitOff._disableWebRTC(),
      defaultTimeout: defaultTimeout._webrtcConnectTimeoutMs(),
      customTimeout: customTimeout._webrtcConnectTimeoutMs(),
      tooLow: tooLow._webrtcConnectTimeoutMs(),
      tooHigh: tooHigh._webrtcConnectTimeoutMs(),
      nonNumeric: nonNumeric._webrtcConnectTimeoutMs(),
      zero: zero._webrtcConnectTimeoutMs(),
    };
  });
  expect(r.defaultOff, "default is disabled=false (WebRTC attempted)").toBe(false);
  expect(r.explicitOn, "disable_webrtc:true is respected").toBe(true);
  expect(r.explicitOff, "disable_webrtc:false is respected").toBe(false);
  expect(r.defaultTimeout, "default timeout stays the flat 5000ms (a 4000ms default was tried and reverted after bug-hunt review)").toBe(5000);
  expect(r.customTimeout, "a valid custom timeout is used as-is").toBe(8000);
  expect(r.tooLow, "a too-low value clamps up to the 1000ms floor").toBe(1000);
  expect(r.tooHigh, "a too-high value clamps down to the 30000ms ceiling").toBe(30000);
  expect(r.nonNumeric, "a non-numeric value falls back to the 5000ms default").toBe(5000);
  expect(r.zero, "an explicit 0 is still clamped to the 1000ms floor, not treated as unset").toBe(1000);
});

test("_startLiveVideo skips WebRTC entirely when disable_webrtc is configured", () => {
  const start = CARD_SRC.indexOf("async _startLiveVideo(");
  expect(start, "_startLiveVideo exists").toBeGreaterThan(-1);
  const body = CARD_SRC.slice(start, CARD_SRC.indexOf("async _startWebRTC(", start));
  const disableIdx = body.indexOf("if (this._disableWebRTC()) {");
  const stickyIdx = body.indexOf("if (this._preferHlsThisSession) {");
  const webrtcIdx = body.indexOf("await this._startWebRTC(");
  expect(disableIdx, "disable_webrtc gate present").toBeGreaterThan(-1);
  expect(stickyIdx, "sticky-HLS gate still present").toBeGreaterThan(-1);
  expect(webrtcIdx, "WebRTC attempt present").toBeGreaterThan(-1);
  // Both gates must come before the WebRTC attempt, disable_webrtc first
  // (an explicit user opt-out should short-circuit before the sticky
  // runtime-detected flag is even checked).
  expect(disableIdx, "disable_webrtc gate precedes the sticky-HLS gate").toBeLessThan(stickyIdx);
  expect(stickyIdx, "sticky-HLS gate precedes the WebRTC attempt").toBeLessThan(webrtcIdx);
});

test("_startWebRTC uses the configurable timeout, not a hardcoded 5000", () => {
  const start = CARD_SRC.indexOf("async _startWebRTC(");
  expect(start, "_startWebRTC exists").toBeGreaterThan(-1);
  const body = CARD_SRC.slice(start, start + 14000);
  expect(body.includes("const attemptMs = this._webrtcConnectTimeoutMs();"),
    "attemptMs is sourced from the configurable helper").toBe(true);
  expect(body.includes("const attemptMs = 5000;"),
    "the old hardcoded 5000ms literal is gone").toBe(false);
});

test("single-card editor exposes disable_webrtc + webrtc_connect_timeout_ms and writes correct config", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card-editor"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const editor = document.createElement("bosch-camera-card-editor");
    editor.hass = { config: {}, language: "en", localize: () => "", states: {} };
    editor.setConfig({ camera_entity: "camera.test" });
    document.body.appendChild(editor);
    await new Promise((res) => setTimeout(res, 100));

    let lastConfig = null;
    editor.addEventListener("config-changed", (e) => { lastConfig = e.detail.config; });

    const chk = editor.shadowRoot.querySelector('input[name="disable_webrtc"]');
    const num = editor.shadowRoot.querySelector('input[name="webrtc_connect_timeout_ms"]');
    const hasFields = !!chk && !!num;
    const numDefaultValue = num ? Number(num.value) : null;

    chk.checked = true;
    chk.dispatchEvent(new Event("change"));
    const afterCheckbox = lastConfig ? { ...lastConfig } : null;

    num.value = "3000";
    num.dispatchEvent(new Event("change"));
    const afterNumber = lastConfig ? { ...lastConfig } : null;

    editor.remove();
    return { hasFields, numDefaultValue, afterCheckbox, afterNumber };
  });
  expect(r.hasFields, "editor renders both new fields").toBe(true);
  expect(r.numDefaultValue, "number field defaults to 5000").toBe(5000);
  expect(r.afterCheckbox?.disable_webrtc, "checkbox toggle fires disable_webrtc:true").toBe(true);
  expect(r.afterNumber?.webrtc_connect_timeout_ms, "number input fires the parsed integer").toBe(3000);
});

test("editors display the clamped webrtc_connect_timeout_ms value, not a stale out-of-range raw value", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card-editor")
    && !!customElements.get("bosch-camera-overview-card-editor"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const single = document.createElement("bosch-camera-card-editor");
    single.hass = { config: {}, language: "en", localize: () => "", states: {} };
    single.setConfig({ camera_entity: "camera.test", webrtc_connect_timeout_ms: 500 });
    document.body.appendChild(single);
    await new Promise((res) => setTimeout(res, 100));
    const singleValue = Number(single.shadowRoot.querySelector('input[name="webrtc_connect_timeout_ms"]').value);
    single.remove();

    const overview = document.createElement("bosch-camera-overview-card-editor");
    overview.hass = { config: {}, language: "en", localize: () => "", states: {} };
    overview.setConfig({ entities: ["camera.test"], webrtc_connect_timeout_ms: 999999 });
    document.body.appendChild(overview);
    await new Promise((res) => setTimeout(res, 100));
    const overviewValue = Number(overview.shadowRoot.querySelector('input[name="webrtc_connect_timeout_ms"]').value);
    overview.remove();

    return { singleValue, overviewValue };
  });
  expect(r.singleValue, "single-card editor shows the clamped 1000ms floor, not the raw 500").toBe(1000);
  expect(r.overviewValue, "overview editor shows the clamped 30000ms ceiling, not the raw 999999").toBe(30000);
});

test("overview editor exposes disable_webrtc + webrtc_connect_timeout_ms and propagates via card_defaults", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-overview-card-editor")
    && !!customElements.get("bosch-camera-overview-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const editor = document.createElement("bosch-camera-overview-card-editor");
    editor.hass = { config: {}, language: "en", localize: () => "", states: {} };
    editor.setConfig({});
    document.body.appendChild(editor);
    await new Promise((res) => setTimeout(res, 100));

    let lastConfig = null;
    editor.addEventListener("config-changed", (e) => { lastConfig = e.detail.config; });

    const chk = editor.shadowRoot.querySelector('input[name="disable_webrtc"]');
    const num = editor.shadowRoot.querySelector('input[name="webrtc_connect_timeout_ms"]');
    const hasFields = !!chk && !!num;

    chk.checked = true;
    chk.dispatchEvent(new Event("change"));
    num.value = "6000";
    num.dispatchEvent(new Event("change"));
    editor.remove();

    // Feed the resulting config into the actual overview card and confirm
    // it lands in card_defaults (what every child <bosch-camera-card>
    // tile actually receives via setConfig).
    const overview = document.createElement("bosch-camera-overview-card");
    overview.setConfig(lastConfig || {});
    const propagated = {
      disable_webrtc: overview._config.card_defaults.disable_webrtc,
      webrtc_connect_timeout_ms: overview._config.card_defaults.webrtc_connect_timeout_ms,
    };
    overview.remove();

    return { hasFields, propagated };
  });
  expect(r.hasFields, "overview editor renders both new fields").toBe(true);
  expect(r.propagated.disable_webrtc, "disable_webrtc propagates into card_defaults").toBe(true);
  expect(r.propagated.webrtc_connect_timeout_ms, "webrtc_connect_timeout_ms propagates into card_defaults").toBe(6000);
});

test("dead-track watchdog: _startLiveVideo skips WebRTC when sticky HLS is set", () => {
  const start = CARD_SRC.indexOf("async _startLiveVideo(");
  expect(start, "_startLiveVideo exists").toBeGreaterThan(-1);
  const body = CARD_SRC.slice(start, CARD_SRC.indexOf("async _startWebRTC(", start));
  const gateIdx = body.indexOf("if (this._preferHlsThisSession) {");
  const webrtcIdx = body.indexOf("await this._startWebRTC(");
  expect(gateIdx, "sticky-HLS gate present in _startLiveVideo").toBeGreaterThan(-1);
  expect(webrtcIdx, "WebRTC attempt present").toBeGreaterThan(-1);
  // The gate must come BEFORE the WebRTC attempt so the attempt is skipped.
  expect(gateIdx, "sticky gate precedes the WebRTC attempt").toBeLessThan(webrtcIdx);
  // The skip is implemented as `else try` so the existing try/catch is gated.
  expect(body.includes("} else try {"), "WebRTC attempt is gated behind the sticky check").toBe(true);
});

test("dead-track watchdog: armed only on the WebRTC transport", () => {
  expect(CARD_SRC.includes("_armWebrtcDeadTrackWatchdog(video)"), "watchdog is armed").toBe(true);
  const armIdx = CARD_SRC.indexOf("this._armWebrtcDeadTrackWatchdog(video);");
  expect(armIdx, "watchdog arm call exists").toBeGreaterThan(-1);
  // The arm site is gated on the WebRTC transport (HLS sets _streamTransport="hls").
  const guard = CARD_SRC.slice(CARD_SRC.lastIndexOf("if (", armIdx), armIdx);
  expect(guard.includes('this._streamTransport === "webrtc"'),
    "watchdog only arms for WebRTC").toBe(true);
});

test("dead-track watchdog: getStats snapshot keys off framesDecoded + bytesReceived, never framesReceived", () => {
  const start = CARD_SRC.indexOf("async _webrtcStatsSnapshot()");
  expect(start, "_webrtcStatsSnapshot exists").toBeGreaterThan(-1);
  const body = CARD_SRC.slice(start, CARD_SRC.indexOf("_armWebrtcDeadTrackWatchdog", start));
  expect(body.includes("framesDecoded"), "uses framesDecoded").toBe(true);
  expect(body.includes("bytesReceived"), "uses bytesReceived").toBe(true);
  // framesReceived is FAIL on iOS WKWebView (single impl) — must NOT be used.
  expect(body.includes("framesReceived"), "never keys off framesReceived").toBe(false);
  expect(body.includes('r.type === "inbound-rtp"'), "reads inbound-rtp reports").toBe(true);
});

test("dead-track watchdog: zero-byte poll = immediate HLS, byte-flow-zero-frame = HLS after 2 polls", () => {
  const start = CARD_SRC.indexOf("_armWebrtcDeadTrackWatchdog(video) {");
  expect(start, "watchdog body exists").toBeGreaterThan(-1);
  const body = CARD_SRC.slice(start, CARD_SRC.indexOf("_forceHlsFallback(reason)", start));
  // bytesReceived delta <= 0 → CGNAT cut → fall back now.
  expect(body.includes("CGNAT cut"), "CGNAT byte-flat path forces HLS").toBe(true);
  expect(body.includes("dBytes <= 0"), "byte delta <=0 is the trigger").toBe(true);
  // bytes flowing but 0 frames decoded → decoder stall after 2 polls.
  expect(body.includes("this._webrtcDeadPolls >= 2"), "decoder-stall needs 2 polls").toBe(true);
  // Visible-only: never false-positives on a hidden tab (iOS thread suspend).
  expect(body.includes('document.visibilityState !== "visible"'),
    "watchdog re-arms instead of firing while hidden").toBe(true);
  // A real presented frame cancels the watchdog.
  expect(body.includes("video._boschLastFrameAt != null"), "rVFC frame cancels the watchdog").toBe(true);
});

test("dead-track watchdog: _forceHlsFallback sets sticky HLS then recovers", () => {
  const start = CARD_SRC.indexOf("_forceHlsFallback(reason) {");
  expect(start, "_forceHlsFallback exists").toBeGreaterThan(-1);
  const body = CARD_SRC.slice(start, CARD_SRC.indexOf("_scheduleLiveRecovery(reason) {", start));
  const stickyIdx = body.indexOf("this._preferHlsThisSession = true;");
  const recoverIdx = body.indexOf("this._scheduleLiveRecovery(");
  expect(stickyIdx, "sets sticky HLS").toBeGreaterThan(-1);
  expect(recoverIdx, "triggers a PiP-safe recovery").toBeGreaterThan(-1);
  // Must set the sticky flag BEFORE the recovery so the rebuild skips WebRTC.
  expect(stickyIdx, "sticky flag set before recovery rebuild").toBeLessThan(recoverIdx);
  // Idempotent: bails if already escalated.
  expect(body.includes("if (this._preferHlsThisSession) return;"), "idempotent guard").toBe(true);
});

test("repeated WebRTC recovery escalates to sticky HLS; a presented frame resets the streak", () => {
  const recStart = CARD_SRC.indexOf("_scheduleLiveRecovery(reason) {");
  const recBody = CARD_SRC.slice(recStart, CARD_SRC.indexOf("_stopLiveVideo() {", recStart));
  expect(recBody.includes("this._webrtcRecoveryStreak"), "recovery streak counted").toBe(true);
  expect(recBody.includes("this._webrtcRecoveryStreak >= 2"), "escalates at 2 recoveries").toBe(true);
  expect(recBody.includes("this._preferHlsThisSession = true"), "escalation sets sticky HLS").toBe(true);
  // rVFC onFrame resets the streak so a one-off blip never trips it.
  const onFrameIdx = CARD_SRC.indexOf("video._boschLastFrameAt = performance.now();");
  const onFrameBody = CARD_SRC.slice(onFrameIdx, onFrameIdx + 400);
  expect(onFrameBody.includes("this._webrtcRecoveryStreak = 0;"),
    "a presented frame resets the recovery streak").toBe(true);
});

test("sticky HLS is reset on a fresh mount (connectedCallback)", () => {
  const idx = CARD_SRC.indexOf("connectedCallback() {");
  const body = CARD_SRC.slice(idx, idx + 600);
  expect(body.includes("this._preferHlsThisSession = false;"),
    "fresh mount re-probes WebRTC").toBe(true);
  expect(body.includes("this._webrtcRecoveryStreak = 0;"), "streak reset on mount").toBe(true);
});

test("_isExternalHost detects non-RFC1918/.local hostnames (2026-08-09)", () => {
  const start = CARD_SRC.indexOf("_isExternalHost() {");
  const body = CARD_SRC.slice(start, CARD_SRC.indexOf("_isMobileClient() {", start));
  expect(start, "_isExternalHost method exists").toBeGreaterThan(-1);
  for (const needle of ['h === "localhost"', 'h.endsWith(".local")', "/^10\\./", "/^192\\.168\\./",
    "/^172\\.(1[6-9]|2\\d|3[01])\\./", "/^fe80:/i"]) {
    expect(body.includes(needle), `_isExternalHost excludes ${needle}`).toBe(true);
  }
});

test("_remoteSkipWebRTC reuses _isExternalHost instead of duplicating the hostname check (2026-08-09)", () => {
  const start = CARD_SRC.indexOf("this._remoteSkipWebRTC = (() => {");
  const body = CARD_SRC.slice(start, start + 800);
  expect(body.includes("this._isExternalHost()"), "reuses the shared helper").toBe(true);
  expect(body.includes('h === "localhost"'), "no longer duplicates the raw hostname regexes here").toBe(false);
});

test("external host's HLS pLoader strips LL-HLS advertisement from the real manifest text (2026-08-09)", async ({ page }) => {
  // Regression: lowLatencyMode:false alone did NOT stop hls.js from sending
  // blocking-reload (_HLS_msn/_HLS_part) requests in production (confirmed
  // live 2026-08-09 — same requests still fired, just with a different
  // error code through the reverse proxy). The pLoader override is the
  // actual fix: it must rewrite the manifest text BEFORE hls.js's parser
  // ever sees it, so hls.js can never construct a blocking-reload URL in
  // the first place. This exercises the real makeLLHlsStrippingPlaylistLoader
  // function against a real LL-HLS sample manifest, via the real
  // _startLiveVideo() HLS code path (WebRTC skipped via sticky-HLS so the
  // test doesn't need to mock RTCPeerConnection).
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const LL_HLS_SAMPLE = [
      "#EXTM3U",
      "#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,CAN-SKIP-UNTIL=6.0,PART-HOLD-BACK=1.5",
      "#EXT-X-PART-INF:PART-TARGET=0.75",
      "#EXT-X-SKIP:SKIPPED-SEGMENTS=2",
      "#EXTINF:2.0,",
      "seg1.m4s",
      "#EXT-X-PART:DURATION=0.75,URI=\"part1.m4s\"",
      "#EXT-X-PRELOAD-HINT:TYPE=PART,URI=\"part2.m4s\"",
      "",
    ].join("\n");

    class FakeBaseLoader {
      constructor(config) { this.config = config; }
      destroy() {}
      abort() {}
      load(context, config, callbacks) {
        callbacks.onSuccess({ url: context.url, data: LL_HLS_SAMPLE }, {}, context, null);
      }
    }
    let capturedPlaylistData = null;
    window.Hls = class FakeHls {
      static isSupported() { return true; }
      static get Events() {
        return { MANIFEST_PARSED: "m", FRAG_LOADED: "f", FRAG_BUFFERED: "b", ERROR: "e" };
      }
      static get ErrorTypes() { return { NETWORK_ERROR: "net", MEDIA_ERROR: "media" }; }
      static get DefaultConfig() { return { loader: FakeBaseLoader }; }
      constructor(config) {
        if (config.pLoader) {
          const loader = new config.pLoader(config);
          loader.load({ url: "https://remote-example.invalid/api/hls/x/playlist.m3u8" }, {}, {
            onSuccess: (response) => { capturedPlaylistData = response.data; },
          });
        }
      }
      on() {}
      loadSource() {}
      attachMedia() {}
      destroy() {}
    };

    const base = { config: {}, language: "de", localize: (k) => k, callService: () => {}, callApi: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card.hass = {
      ...base,
      callWS: async () => ({ url: "https://remote-example.invalid/api/hls/x/playlist.m3u8" }),
      states: { "camera.test": { state: "streaming", attributes: {}, last_updated: "2026-01-01T00:00:00Z" } },
    };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    card._isExternalHost = () => true;
    card._preferHlsThisSession = true; // skip WebRTC negotiation entirely, go straight to the HLS branch
    card._liveVideoActive = false;
    await card._startLiveVideo();
    await new Promise((res) => setTimeout(res, 50));
    card.remove();
    return { capturedPlaylistData };
  });
  expect(r.capturedPlaylistData, "pLoader ran and captured the sanitized manifest").not.toBeNull();
  expect(r.capturedPlaylistData.includes("CAN-BLOCK-RELOAD=YES"), "CAN-BLOCK-RELOAD=YES rewritten to NO").toBe(false);
  expect(r.capturedPlaylistData.includes("CAN-BLOCK-RELOAD=NO"), "CAN-BLOCK-RELOAD explicitly disabled").toBe(true);
  expect(r.capturedPlaylistData.includes("#EXT-X-PART:"), "#EXT-X-PART lines stripped").toBe(false);
  expect(r.capturedPlaylistData.includes("#EXT-X-PRELOAD-HINT:"), "#EXT-X-PRELOAD-HINT lines stripped").toBe(false);
  expect(r.capturedPlaylistData.includes("#EXT-X-PART-INF:"), "#EXT-X-PART-INF lines stripped").toBe(false);
  expect(r.capturedPlaylistData.includes("CAN-SKIP-UNTIL"), "CAN-SKIP-UNTIL attribute stripped (canSkipUntil is the other blocking-reload gate)").toBe(false);
  expect(r.capturedPlaylistData.includes("#EXT-X-SKIP:"), "#EXT-X-SKIP lines stripped").toBe(false);
  expect(r.capturedPlaylistData.includes("#EXTINF:2.0,"), "unrelated manifest lines left untouched").toBe(true);
});

test("HLS falls back to plain (non-low-latency) mode on an external host (2026-08-09)", () => {
  // Real-world report: LL-HLS blocking-reload requests (_HLS_msn/_HLS_part)
  // 404ed repeatedly through a remote/tunneled connection while the exact
  // same camera stayed rock-solid over LAN — the reverse proxy/tunnel doesn't
  // reliably serve that long-held request pattern. Force plain HLS reload for
  // any external host regardless of device type (desktop included).
  const idx = CARD_SRC.indexOf("const hls = new Hls({");
  expect(idx, "hls.js constructor call found").toBeGreaterThan(-1);
  const before = CARD_SRC.slice(CARD_SRC.lastIndexOf("const isExternal = this._isExternalHost();", idx), idx);
  expect(before.length, "isExternal computed before the Hls() call").toBeGreaterThan(0);
  const ctorBody = CARD_SRC.slice(idx, idx + 300);
  expect(ctorBody.includes("...(isExternal ? { lowLatencyMode: false } : {})"),
    "lowLatencyMode explicitly disabled for external hosts").toBe(true);
});

test("HLS-mode banner shows for a mobile client on HLS (not just the remote-skip case)", () => {
  // _update() banner gate + activateVideo banner — both must include the mobile +
  // sticky-HLS conditions, not only _remoteSkipWebRTC.
  const showHlsIdx = CARD_SRC.indexOf("const showHls = ");
  const showHlsBody = CARD_SRC.slice(showHlsIdx, showHlsIdx + 220);
  expect(showHlsBody.includes("this._preferHlsThisSession"), "banner respects sticky HLS").toBe(true);
  expect(showHlsBody.includes("this._isMobileClient()"), "banner shows for mobile clients").toBe(true);
  expect(showHlsBody.includes('this._streamTransport === "hls"'), "banner only on HLS transport").toBe(true);
  // _isMobileClient must detect the Companion app + iOS + Android.
  const mc = CARD_SRC.slice(CARD_SRC.indexOf("_isMobileClient() {"), CARD_SRC.indexOf("_isMobileClient() {") + 600);
  expect(mc.includes("external_auth=1"), "detects Companion via external_auth").toBe(true);
  expect(mc.includes("getExternalAuth"), "detects iOS Companion via webkit bridge").toBe(true);
});

test("HLS-mode banner does not overlap the title-pill/status-badge row (2026-07-18 mobile report)", async ({ page }) => {
  // Regression: .ios-hls-banner and .ap-top were both position:absolute at the
  // same top offset (banner z-index 5 UNDER .ap-top's z-index 13), so the banner
  // text rendered squeezed/hidden behind the camera-name pill and VERBINDE/LIVE
  // badge on mobile/remote-tunnel HLS fallback — reported live on iOS Companion
  // App screenshots. Fixed by pushing the banner below the pill row (top:54px)
  // except when .ap-top itself is hidden (:host(.no-title)).
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "de", localize: (k) => (k === "hls_mode_banner" ? "HLS-Modus, höhere Latenz" : k), callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { ...base, states: { "camera.test": { state: "streaming", attributes: { friendly_name: "Innenbereich" }, last_updated: "2026-01-01T00:00:00Z" } } };
    card.style.width = "400px"; card.style.display = "block";
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    // Force the exact reported state: mobile client, HLS fallback, live video active.
    card._isMobileClient = () => true;
    card._streamTransport = "hls";
    card._liveVideoActive = true;
    card._update();
    const sr = card.shadowRoot;
    const banner = sr.getElementById("ios-hls-banner");
    const top = sr.querySelector(".ap-top");
    if (!banner || !top) return { error: "missing banner/ap-top" };
    const visible = banner.classList.contains("visible");
    const bRect = banner.getBoundingClientRect();
    const tRect = top.getBoundingClientRect();
    card.remove();
    return { visible, bTop: bRect.top, bBottom: bRect.bottom, tTop: tRect.top, tBottom: tRect.bottom };
  });
  expect(r.error, "card renders banner + title-pill row").toBeUndefined();
  expect(r.visible, "banner shows for mobile client on HLS with live video").toBe(true);
  expect(r.bTop, "banner starts at/below the title-pill row's bottom edge (no overlap)").toBeGreaterThanOrEqual(r.tBottom);
});

test("HLS-mode banner auto-hides a few minutes after first showing (2026-08-09)", async ({ page }) => {
  // Feature: the banner is an informational one-time note, not something that
  // should sit over the video for the whole session — it self-dismisses via
  // _HLS_BANNER_AUTO_HIDE_MS after first appearing.
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "de", localize: (k) => (k === "hls_mode_banner" ? "HLS-Modus, höhere Latenz" : k), callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { ...base, states: { "camera.test": { state: "streaming", attributes: { friendly_name: "Innenbereich" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    card._isMobileClient = () => true;
    card._streamTransport = "hls";
    card._liveVideoActive = true;
    card._HLS_BANNER_AUTO_HIDE_MS = 0; // fire on next tick instead of waiting the real 3 min
    card._update();
    const banner = card.shadowRoot.getElementById("ios-hls-banner");
    const visibleBefore = banner.classList.contains("visible");
    await new Promise((res) => setTimeout(res, 50));
    const visibleAfterTimeout = banner.classList.contains("visible");
    const dismissedFlag = card._hlsBannerDismissed;
    // Transport leaving HLS must reset the dismissed flag so a later fallback re-shows it.
    card._streamTransport = "webrtc";
    card._update();
    const resetAfterLeavingHls = card._hlsBannerDismissed;
    card.remove();
    return { visibleBefore, visibleAfterTimeout, dismissedFlag, resetAfterLeavingHls };
  });
  expect(r.visibleBefore, "banner shows immediately when HLS fallback starts").toBe(true);
  expect(r.visibleAfterTimeout, "banner hides itself after the auto-hide timeout").toBe(false);
  expect(r.dismissedFlag, "dismissed flag set once the timer fires").toBe(true);
  expect(r.resetAfterLeavingHls, "dismissed flag resets once transport leaves HLS").toBe(false);
});

test("_stopLiveVideo clears the dead-track watchdog timer + resets its baseline", () => {
  const start = CARD_SRC.indexOf("_stopLiveVideo() {");
  const body = CARD_SRC.slice(start, CARD_SRC.indexOf("_onSnapshotClick()", start));
  // 2026-07-13: cleared via the shared _clearTimer registry helper now
  // (Phase 4 point 14) instead of a raw clearTimeout guard.
  expect(body.includes("_clearTimer(this._webrtcFirstFrameTimer)"),
    "watchdog timer cleared on teardown").toBe(true);
  expect(body.includes("this._webrtcStatsPrev     = null;") || body.includes("this._webrtcStatsPrev = null;"),
    "stats baseline reset").toBe(true);
});

// ── Dead-track watchdog hardening (verify-agent findings, 2026-06-23) ──────────

test("watchdog never forces HLS on a null getStats report (no false-positive)", () => {
  const start = CARD_SRC.indexOf("_armWebrtcDeadTrackWatchdog(video) {");
  const body = CARD_SRC.slice(start, CARD_SRC.indexOf("_forceHlsFallback(reason) {", start));
  // The null-report branch at the deadline must `return` (give up), NOT force HLS —
  // a getStats-less browser on a healthy stream looks identical to a dead one.
  const nullIdx = body.indexOf("if (snap == null) {");
  expect(nullIdx, "null-report branch exists").toBeGreaterThan(-1);
  const nullBlock = body.slice(nullIdx, body.indexOf("if (snap.frames > 0)", nullIdx));
  expect(nullBlock.includes("if (pastDeadline) return;"), "null report at deadline gives up silently").toBe(true);
  expect(nullBlock.includes("_forceHlsFallback"), "null report NEVER forces HLS").toBe(false);
  // The deadline fallback only fires with a REAL report still at 0 frames.
  expect(body.includes('"0 frames decoded within deadline"'), "deadline fallback needs frame evidence").toBe(true);
});

test("recovery streak only counts when the dying stream never rendered (no 60-min-rotation false-positive)", () => {
  const recStart = CARD_SRC.indexOf("_scheduleLiveRecovery(reason) {");
  const recBody = CARD_SRC.slice(recStart, CARD_SRC.indexOf("_stopLiveVideo() {", recStart));
  expect(recBody.includes("const neverRendered"), "streak gated on neverRendered").toBe(true);
  expect(recBody.includes("_boschLastFrameAt == null"), "neverRendered keys off the rVFC frame timestamp").toBe(true);
  // iOS WKWebView has no requestVideoFrameCallback so _boschLastFrameAt is always null.
  // _hasEverDecodedFrames (set by getStats in the dead-track watchdog) is the fallback.
  expect(recBody.includes("_hasEverDecodedFrames"), "neverRendered has iOS fallback via _hasEverDecodedFrames").toBe(true);
  expect(recBody.includes("&& neverRendered"), "streak increment requires neverRendered").toBe(true);
});

test("dead-stream recovery backs off instead of hammering rebuilds forever (2026-08-09)", async ({ page }) => {
  // Real-world report: on a remote connection with no VPN, WebRTC never rendered
  // a frame, escalated to sticky HLS, but HLS ALSO never rendered a frame — the
  // generic stall checker (transport-agnostic) kept calling _scheduleLiveRecovery
  // every ~15s forever with a fixed 1000ms rebuild delay, visibly flapping
  // "Live"/"Connecting" without end. _deadStreamRecoveryStreak must back off the
  // rebuild delay exponentially when NO transport ever renders a frame, and reset
  // to a fast 1000ms retry the instant a frame actually plays.
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "de", localize: (k) => k, callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card.hass = { ...base, states: { "camera.test": { state: "streaming", attributes: { friendly_name: "Test" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    const delays = [];
    card._armTimer = (fn, delay) => { delays.push(delay); return 0; };
    card._stopLiveVideo = () => {};
    card._liveVideoActive = true;
    for (let i = 0; i < 5; i++) {
      card._reconnectingLiveVideo = false;
      card._scheduleLiveRecovery("test cycle " + i);
    }
    const streakAfterFailures = card._deadStreamRecoveryStreak;
    // A stream that renders a frame between recoveries must drop straight back
    // to the fast retry, same as a one-off blip. Exercise the REAL neverRendered
    // gate here (not a manual streak reset) — set the same iOS-fallback flag the
    // dead-track watchdog sets on a confirmed frame, matching the exact
    // condition _scheduleLiveRecovery reads (verify-agent finding, 2026-08-09:
    // an earlier version of this test reset the streak manually and never
    // actually exercised this branch).
    card._hasEverDecodedFrames = true;
    card._reconnectingLiveVideo = false;
    card._scheduleLiveRecovery("test cycle after recovery");
    const streakAfterRealFrame = card._deadStreamRecoveryStreak;
    const delayAfterRealFrame = delays[delays.length - 1];
    card.remove();
    return { delays, streakAfterFailures, streakAfterRealFrame, delayAfterRealFrame };
  });
  expect(r.delays[0], "first dead-stream recovery still uses the fast 1000ms delay").toBe(1000);
  expect(r.delays[1], "second consecutive dead-stream recovery backs off").toBeGreaterThan(1000);
  expect(r.delays[4], "delay keeps growing across consecutive dead cycles").toBeGreaterThan(r.delays[1]);
  expect(Math.max(...r.delays), "backoff is capped at 16s").toBeLessThanOrEqual(16000);
  expect(r.streakAfterFailures, "streak accumulated across the 5 dead cycles").toBe(5);
  expect(r.streakAfterRealFrame, "neverRendered=false (a confirmed frame) resets the streak for real").toBe(0);
  expect(r.delayAfterRealFrame, "delay is back to the fast 1000ms once the streak resets").toBe(1000);
});

test("dead-stream backoff does not defeat itself via _update()'s auto-start gate (2026-08-09 regression)", async ({ page }) => {
  // verify-agent finding: _stopLiveVideo() (called from _scheduleLiveRecovery)
  // synchronously clears _reconnectingLiveVideo/_waitingForStream/_liveVideoActive
  // — exactly the all-false state _update()'s auto-start gate looks for. Without
  // _deadStreamRebuildPending, a hass push arriving during the backoff window
  // (near-certain — the teardown itself churns stream_status) restarts the
  // stream immediately, making the whole backoff a no-op.
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "de", localize: (k) => k, callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card.hass = { ...base, states: { "camera.test": { state: "streaming", attributes: { friendly_name: "Test" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    let startCalls = 0;
    card._startLiveVideo = async () => { startCalls++; };
    const capturedCallbacks = [];
    card._armTimer = (fn) => { capturedCallbacks.push(fn); return 0; };
    card._liveVideoActive = true;
    // Build a real backoff (streak > 1) via genuine never-rendered cycles.
    for (let i = 0; i < 3; i++) {
      card._reconnectingLiveVideo = false;
      card._liveVideoActive = true;
      card._scheduleLiveRecovery("cycle " + i);
    }
    const pendingAfterSchedule = card._deadStreamRebuildPending;
    // Simulate a hass push landing mid-backoff — this is the exact call site
    // (_update()'s auto-start gate) that used to restart the stream early.
    card._update();
    const startCallsWhilePending = startCalls;
    // Now let the backoff timer itself fire.
    capturedCallbacks[capturedCallbacks.length - 1]();
    const pendingAfterCallback = card._deadStreamRebuildPending;
    const startCallsAfterCallback = startCalls;
    card.remove();
    return { pendingAfterSchedule, startCallsWhilePending, pendingAfterCallback, startCallsAfterCallback };
  });
  expect(r.pendingAfterSchedule, "backoff flag set once a rebuild timer is armed").toBe(true);
  expect(r.startCallsWhilePending, "_update() must NOT restart the stream while backoff is pending").toBe(0);
  expect(r.pendingAfterCallback, "backoff flag clears once the timer fires").toBe(false);
  expect(r.startCallsAfterCallback, "the timer's own callback performs the restart once it fires").toBe(1);
});

test("dead-stream rebuild callback does not start a stream while the tab is hidden (2026-08-09)", async ({ page }) => {
  // verify-agent finding: on a long backoff window the tab can go hidden before
  // the timer fires; starting a stream nobody can see leaks an untracked
  // session (the same class of bug the 2026-07-19 hidden-tab guard elsewhere
  // in this file exists to prevent).
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const base = { config: {}, language: "de", localize: (k) => k, callService: () => {}, callApi: async () => ({}), callWS: async () => ({}) };
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test" });
    card.hass = { ...base, states: { "camera.test": { state: "streaming", attributes: { friendly_name: "Test" }, last_updated: "2026-01-01T00:00:00Z" } } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    let startCalls = 0;
    card._startLiveVideo = async () => { startCalls++; };
    let capturedCallback = null;
    card._armTimer = (fn) => { capturedCallback = fn; return 0; };
    card._liveVideoActive = true;
    card._scheduleLiveRecovery("hidden-tab test");
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    capturedCallback();
    const startCallsWhileHidden = startCalls;
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    card.remove();
    return { startCallsWhileHidden };
  });
  expect(r.startCallsWhileHidden, "rebuild callback must not start a stream while the tab is hidden").toBe(0);
});

test("dead-stream backoff streak is reset once a frame actually renders (2026-08-09)", () => {
  const start = CARD_SRC.indexOf("const clearOverlay = () => {");
  const body = CARD_SRC.slice(start, CARD_SRC.indexOf("video.addEventListener(\"playing\", clearOverlay", start));
  expect(body.includes("this._deadStreamRecoveryStreak = 0;"),
    "a rendered frame resets the dead-stream backoff streak").toBe(true);
});

test("dead-track watchdog sets _hasEverDecodedFrames when getStats sees frames (iOS rVFC fallback)", () => {
  const start = CARD_SRC.indexOf("_armWebrtcDeadTrackWatchdog(video) {");
  const body = CARD_SRC.slice(start, CARD_SRC.indexOf("_forceHlsFallback(reason) {", start));
  // When getStats reports frames > 0, watchdog must set _hasEverDecodedFrames so that
  // neverRendered stays false on iOS WKWebView (no requestVideoFrameCallback).
  expect(body.includes("this._hasEverDecodedFrames = true"), "watchdog sets _hasEverDecodedFrames on frames > 0").toBe(true);
});

test("native iOS HLS load watchdog: armed on the native path, cleared on teardown", () => {
  expect(CARD_SRC.includes("this._armNativeHlsLoadWatchdog(video, result.url);"),
    "native HLS watchdog armed on the canPlayType path").toBe(true);
  const m = CARD_SRC.indexOf("_armNativeHlsLoadWatchdog(video, url) {");
  expect(m, "watchdog method exists").toBeGreaterThan(-1);
  const body = CARD_SRC.slice(m, m + 1600);
  expect(body.includes('addEventListener("playing"'), "cancels on playing").toBe(true);
  expect(body.includes("v.removeAttribute(\"src\")") && body.includes("v.src = url"),
    "hard-reloads the element once").toBe(true);
  expect(body.includes("8000"), "8s load deadline").toBe(true);
  // Cleared in _stopLiveVideo.
  const stop = CARD_SRC.indexOf("_stopLiveVideo() {");
  const stopBody = CARD_SRC.slice(stop, CARD_SRC.indexOf("_onSnapshotClick()", stop));
  // 2026-07-13: cleared via the shared _clearTimer registry helper now
  // (Phase 4 point 14) instead of a raw clearTimeout guard.
  expect(stopBody.includes("_clearTimer(this._nativeHlsLoadTimer)"), "timer cleared on teardown").toBe(true);
});

// ── Multi-instance audio registry: redundant setConfig must not swap roles ────
// Regression (bug-hunt 2026-07-03): Lovelace re-invokes setConfig on unchanged
// card instances for reasons unrelated to this card (editing any other card on
// the same view, storage-collection updates, etc.). The registry is a Set,
// which re-inserts at the end on delete+re-add, and "primary" = first-in-Set —
// so a no-op setConfig on the primary instance used to silently move it to the
// end, swapping primary/secondary (mute) roles between two cards showing the
// same camera with no user action.
test("redundant setConfig (same camera_entity) does not swap audio primary/secondary roles", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });

  const result = await page.evaluate(async () => {
    const hass = {
      states: { "camera.dup": { state: "idle", attributes: { friendly_name: "Dup" }, last_updated: "2026-01-01T00:00:00Z" } },
      config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
    };

    const cardA = document.createElement("bosch-camera-card");
    cardA.setConfig({ camera_entity: "camera.dup" });
    cardA.hass = hass;
    document.body.appendChild(cardA);

    const cardB = document.createElement("bosch-camera-card");
    cardB.setConfig({ camera_entity: "camera.dup" });
    cardB.hass = hass;
    document.body.appendChild(cardB);

    await new Promise((r) => setTimeout(r, 100));

    const before = {
      aSecondary: cardA._isSecondaryAudioInstance,
      bSecondary: cardB._isSecondaryAudioInstance,
      // _isSecondaryAudioInstance is a cached snapshot set only at register
      // time; the LIVE mute decision at runtime always re-derives from the
      // registry Set order via _isSecondaryAudio(). Assert both so the test
      // proves the actual runtime behavior, not just the cached field.
      aSecondaryLive: cardA._isSecondaryAudio(),
      bSecondaryLive: cardB._isSecondaryAudio(),
    };

    // Simulate a redundant Lovelace re-invoke on the PRIMARY card (cardA) with
    // the SAME camera_entity — nothing about this card actually changed.
    cardA.setConfig({ camera_entity: "camera.dup", title: "still the same camera" });

    await new Promise((r) => setTimeout(r, 100));

    const after = {
      aSecondary: cardA._isSecondaryAudioInstance,
      bSecondary: cardB._isSecondaryAudioInstance,
      aSecondaryLive: cardA._isSecondaryAudio(),
      bSecondaryLive: cardB._isSecondaryAudio(),
    };

    cardA.remove();
    cardB.remove();
    return { before, after };
  });

  const expected = { aSecondary: false, bSecondary: true, aSecondaryLive: false, bSecondaryLive: true };
  expect(result.before, "cardA registers first → primary (not secondary)").toEqual(expected);
  expect(result.after, "roles must be unchanged after a redundant setConfig").toEqual(expected);
});

// Companion to the redundant-setConfig test above: a GENUINE camera_entity
// change (the user re-points the card at a different camera, e.g. via the
// dashboard editor) must still unregister from the old entity's group and
// register into the new one — the guard must only skip the no-op case, not
// break real re-registration.
test("a genuine camera_entity change still re-registers the audio group", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });

  const result = await page.evaluate(async () => {
    const hass = {
      states: {
        "camera.one": { state: "idle", attributes: { friendly_name: "One" }, last_updated: "2026-01-01T00:00:00Z" },
        "camera.two": { state: "idle", attributes: { friendly_name: "Two" }, last_updated: "2026-01-01T00:00:00Z" },
      },
      config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
    };

    // A second, permanent card already anchoring camera.two as primary.
    const anchorTwo = document.createElement("bosch-camera-card");
    anchorTwo.setConfig({ camera_entity: "camera.two" });
    anchorTwo.hass = hass;
    document.body.appendChild(anchorTwo);

    // The card under test starts on camera.one (alone → primary there).
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.one" });
    card.hass = hass;
    document.body.appendChild(card);
    await new Promise((r) => setTimeout(r, 100));
    const onCameraOne = {
      secondary: card._isSecondaryAudioInstance,
      secondaryLive: card._isSecondaryAudio(),
      registeredEntity: card._audioRegisteredEntity,
    };

    // User re-points the card at camera.two, which already has a primary
    // (anchorTwo) — the newly-arriving card must become secondary there,
    // and camera.one's group must lose it (so a later card on camera.one
    // would become primary again, not find a stale secondary occupying it).
    card.setConfig({ camera_entity: "camera.two" });
    await new Promise((r) => setTimeout(r, 100));
    const onCameraTwo = {
      secondary: card._isSecondaryAudioInstance,
      secondaryLive: card._isSecondaryAudio(),
      registeredEntity: card._audioRegisteredEntity,
    };

    card.remove();
    anchorTwo.remove();
    return { onCameraOne, onCameraTwo };
  });

  expect(result.onCameraOne, "alone on camera.one → primary")
    .toEqual({ secondary: false, secondaryLive: false, registeredEntity: "camera.one" });
  expect(result.onCameraTwo, "re-pointed to camera.two (already has a primary) → becomes secondary there")
    .toEqual({ secondary: true, secondaryLive: true, registeredEntity: "camera.two" });
});

// Thomas (2026-07-28, live report): a single camera going HA-`unavailable`
// showed a misleading "Bosch Cloud token invalid — sign in again" banner,
// as if the WHOLE integration's auth had failed — even though sibling
// cameras were working fine on the same login. The auth-overlay's
// `isIntegrationDown` check treated ANY unavailable camera as a global
// token failure, which is only one of many possible per-camera-unavailable
// causes. This overlay had never actually been exercised for an
// unavailable camera inside the overview grid before the same-day fix that
// made _discover() keep such cameras (see the "keeps its real card" test
// above) — so the bad assumption was never visible until then. Now an
// unavailable camera just gets the plain "Offline" treatment (also
// suppressing the privacy-placeholder, per the existing cam-offline CSS
// exclusivity rules), and the auth/re-login banner is reserved for a
// genuinely wrong/missing camera_entity.
test("an unavailable camera shows the neutral offline overlay, not a misleading auth/re-login banner", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const mkHass = (camState) => ({
      states: {
        "camera.test": { state: camState, attributes: camState === "unavailable" ? {} : { friendly_name: "Test" }, last_updated: "2026-01-01T00:00:00Z" },
      },
      config: { internal_url: "http://localhost:4321" },
      language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
    });

    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = mkHass("unavailable");
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));

    const root = card.shadowRoot;
    const offlineVisible = root.getElementById("offline-overlay")?.classList.contains("visible");
    const authVisible = root.getElementById("auth-overlay")?.classList.contains("visible");
    const privacyVisible = getComputedStyle(root.getElementById("privacy-placeholder")).display !== "none";
    const camOfflineClass = card.classList.contains("cam-offline");

    card.remove();
    return { offlineVisible, authVisible, privacyVisible, camOfflineClass };
  });
  expect(r.camOfflineClass, "cam-offline class applied for an unavailable camera").toBe(true);
  expect(r.offlineVisible, "offline overlay shows for an unavailable camera").toBe(true);
  expect(r.authVisible, "auth/re-login overlay must NOT show — this isn't necessarily an auth failure").toBe(false);
  expect(r.privacyVisible, "privacy-placeholder is hidden while the offline overlay is up (single source of truth)").toBe(false);
});

test("a missing camera_entity (wrong/removed entity id) still shows the auth overlay's 'fix your config' message", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.does_not_exist", apple_style: true });
    card.hass = {
      states: {},
      config: { internal_url: "http://localhost:4321" },
      language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
    };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));

    const root = card.shadowRoot;
    const authVisible = root.getElementById("auth-overlay")?.classList.contains("visible");
    const offlineVisible = root.getElementById("offline-overlay")?.classList.contains("visible");
    const reauthBtnHidden = root.getElementById("auth-reauth-btn")?.style.display === "none";

    card.remove();
    return { authVisible, offlineVisible, reauthBtnHidden };
  });
  expect(r.authVisible, "a missing entity still shows the auth overlay").toBe(true);
  expect(r.offlineVisible, "offline overlay must not also show").toBe(false);
  expect(r.reauthBtnHidden, "re-login button hidden — it can't fix a wrong entity id").toBe(true);
});
