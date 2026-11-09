import { test, expect } from "@playwright/test";

// Overview card `mobile_columns`: phones are forced to one column by default;
// mobile_columns: 2 opts into two tiles side by side, and the tiles go compact
// (host class) on those screens only. No real HA/camera needed.
test.afterEach(async ({ page }) => {
  await page
    .evaluate(() => document.querySelectorAll("bosch-camera-overview-card").forEach((c) => c.remove()))
    .catch(() => {});
});

const CAMS = ["camera.bosch_a", "camera.bosch_b", "camera.bosch_c"];

async function mount(page, config) {
  await page.goto("/test/e2e/fixtures/card.html");
  await page.waitForFunction(() => !!customElements.get("bosch-camera-overview-card"), null, { timeout: 10000 });
  await page.evaluate(({ config, cams }) => {
    const states = {};
    for (const id of cams) {
      states[id] = {
        state: "idle",
        attributes: { friendly_name: id, brand: "Bosch" },
        last_updated: "2026-01-01T00:00:00Z",
      };
    }
    const card = document.createElement("bosch-camera-overview-card");
    card.setConfig(config);
    card.hass = {
      states,
      config: { internal_url: "http://localhost:4321" },
      language: "en", localize: () => "",
      callService: () => {}, callApi: async () => ({}), callWS: async () => ({}),
    };
    document.body.appendChild(card);
    window.__ov = card;
  }, { config, cams: CAMS });
  await page.waitForFunction(() => window.__ov.shadowRoot.querySelectorAll(".bco-cell").length === 3);
}

const columns = (page) =>
  page.evaluate(() => {
    const g = window.__ov.shadowRoot.querySelector(".bco-grid");
    return getComputedStyle(g).gridTemplateColumns.split(" ").length;
  });

const compactCount = (page) =>
  page.evaluate(
    () => [...window.__ov.shadowRoot.querySelectorAll("bosch-camera-card")].filter((c) => c.classList.contains("compact")).length,
  );

test("phone: default is one column, tiles not compact", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mount(page, {});
  expect(await columns(page)).toBe(1);
  expect(await compactCount(page)).toBe(0);
});

test("phone: mobile_columns 2 gives two columns and compact tiles", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mount(page, { mobile_columns: 2 });
  expect(await columns(page)).toBe(2);
  expect(await compactCount(page)).toBe(3);
});

test("phone: garbage mobile_columns falls back to one column", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const bad of [3, "x", 0, null]) {
    await mount(page, { mobile_columns: bad });
    expect(await columns(page)).toBe(1);
    await page.evaluate(() => window.__ov.remove());
  }
});

test("desktop: mobile_columns 2 does not make tiles compact", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await mount(page, { mobile_columns: 2, min_width: "480px" });
  expect(await compactCount(page)).toBe(0);
  expect(await columns(page)).toBeGreaterThanOrEqual(2);
});

test("resizing from desktop to phone toggles compact without remounting", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await mount(page, { mobile_columns: 2 });
  await page.evaluate(() => { window.__firstTile = window.__ov.shadowRoot.querySelector("bosch-camera-card"); });
  expect(await compactCount(page)).toBe(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForFunction(
    () => [...window.__ov.shadowRoot.querySelectorAll("bosch-camera-card")].every((c) => c.classList.contains("compact")),
  );
  expect(await columns(page)).toBe(2);
  expect(await page.evaluate(() => window.__firstTile === window.__ov.shadowRoot.querySelector("bosch-camera-card"))).toBe(true);
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.waitForFunction(
    () => [...window.__ov.shadowRoot.querySelectorAll("bosch-camera-card")].every((c) => !c.classList.contains("compact")),
  );
});

test("explicit compact: true is never removed by the phone logic", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await mount(page, { mobile_columns: 2, compact: true });
  expect(await compactCount(page)).toBe(3);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(300);
  expect(await compactCount(page)).toBe(3);
});

test("phone-compact tiles drop the expanded control block and restore it on desktop", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mount(page, { mobile_columns: 2, minimal: false });
  const flags = () =>
    page.evaluate(() =>
      [...window.__ov.shadowRoot.querySelectorAll("bosch-camera-card")].map((c) => ({
        compact: c.classList.contains("compact"),
        open: c.classList.contains("overflow-open"),
        dedupe: c.classList.contains("dedupe-privacy"),
      })),
    );
  for (const f of await flags()) expect(f).toEqual({ compact: true, open: false, dedupe: false });
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.waitForFunction(
    () => [...window.__ov.shadowRoot.querySelectorAll("bosch-camera-card")].every((c) => !c.classList.contains("compact")),
  );
  for (const f of await flags()) expect(f).toEqual({ compact: false, open: true, dedupe: true });
});
