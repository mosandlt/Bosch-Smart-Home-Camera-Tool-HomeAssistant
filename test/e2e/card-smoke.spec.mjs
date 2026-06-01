import { test, expect } from "@playwright/test";

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
// needed). This pins the single-card hover lift+scale (issue #15.1, RkcCorian):
// at rest the ha-card has no transform; on hover it gains a scale>1 transform.
test("single card scales up on hover (mock hass + real hover)", async ({ page }) => {
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
  const atRest = await haCard.evaluate((el) => getComputedStyle(el).transform);
  await haCard.hover();
  await page.waitForTimeout(250); // let the .18s transition settle
  const onHover = await haCard.evaluate((el) => {
    const t = getComputedStyle(el).transform;
    // Parse the scale factor out of the 2D matrix(a,b,c,d,e,f) → a is x-scale.
    const m = t.match(/matrix\(([^,]+),/);
    return { transform: t, scale: m ? parseFloat(m[1]) : 1 };
  });

  expect(atRest === "none" || atRest === "matrix(1, 0, 0, 1, 0, 0)", "no transform at rest").toBeTruthy();
  expect(onHover.scale, "card scales up on hover").toBeGreaterThan(1);
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

// #22: while streaming, one tap on the Ton toggle unmutes the <video> instantly
// (audibility toggle) — no 2-tap off/on dance.
test("audio toggle unmutes the playing video (mock hass)", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });
  const r = await page.evaluate(async () => {
    const card = document.createElement("bosch-camera-card");
    card.setConfig({ camera_entity: "camera.test", apple_style: true });
    card.hass = { config: {}, language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}), states: {
      "camera.test": { state: "idle", attributes: { friendly_name: "T" }, last_updated: "2026-01-01T00:00:00Z" },
      "switch.test_audio": { state: "on", attributes: {}, last_updated: "2026-01-01T00:00:00Z" },
    } };
    document.body.appendChild(card);
    await new Promise((res) => setTimeout(res, 300));
    const video = card.shadowRoot.getElementById("cam-video");
    if (!video) return { error: "no cam-video element" };
    card._liveVideoActive = true;
    video.muted = true;            // browser autoplay starts muted
    card._toggleAudio();           // one tap
    return { mutedAfter: video.muted };
  });
  expect(r.error, "card renders a <video id=cam-video>").toBeUndefined();
  expect(r.mutedAfter, "one tap unmutes the video").toBe(false);
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

// #27: after a privacy toggle the backend enforces a 10s cooldown (and now
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
