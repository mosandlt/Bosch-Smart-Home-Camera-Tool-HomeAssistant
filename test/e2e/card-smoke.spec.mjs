import { test, expect } from "@playwright/test";

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

// Regression (issue #21, 2026-05-30): a dashboard theme that zeroes the global
// --ha-card-border-radius / --ha-card-box-shadow tokens must NOT strip the
// card's own rounding. The card uses its own --bosch-card-* vars; the optional
// `border_radius:` card config opts into a custom value.
test("card keeps its rounding under a theme that zeroes --ha-card-border-radius", async ({ page }) => {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-card"), null, { timeout: 10000 });

  const result = await page.evaluate(async () => {
    // Simulate Thomas's theme: zero the global card tokens on an ancestor.
    document.body.style.setProperty("--ha-card-border-radius", "0");
    document.body.style.setProperty("--ha-card-box-shadow", "none");
    const mkHass = () => ({
      states: { "camera.test": { state: "idle", attributes: { friendly_name: "Test" }, last_updated: "2026-01-01T00:00:00Z" } },
      config: { internal_url: "http://localhost:4321" },
      language: "en", localize: () => "", callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
    });
    const mk = (cfg) => { const c = document.createElement("bosch-camera-card"); c.setConfig(cfg); c.hass = mkHass(); document.body.appendChild(c); return c; };
    const def = mk({ camera_entity: "camera.test", apple_style: true });
    const opt = mk({ camera_entity: "camera.test", apple_style: true, border_radius: "4px" });
    await new Promise((r) => setTimeout(r, 600));
    const radius = (el) => { const hc = el.shadowRoot && el.shadowRoot.querySelector("ha-card"); return hc ? getComputedStyle(hc).borderTopLeftRadius : null; };
    return { def: radius(def), opt: radius(opt) };
  });

  expect(result.def, "apple-style default rounding survives a zeroing theme").toBe("22px");
  expect(result.opt, "border_radius card config opts into a custom value").toBe("4px");
});
