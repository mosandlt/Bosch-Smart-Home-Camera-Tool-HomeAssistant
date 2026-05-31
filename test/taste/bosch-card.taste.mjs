/**
 * Full end-to-end smoke via hass-taste-test (https://github.com/rianadon/hass-taste-test).
 *
 * Unlike the lightweight Playwright mock-hass tests in test/e2e/, this spins up a
 * REAL Home Assistant Core instance (own venv), serves the built card as a
 * Lovelace resource, renders it on a real dashboard through HA's own frontend,
 * and asserts on the rendered shadow DOM. It catches frontend-integration
 * breakage the mock can't (resource registration, custom-element upgrade inside
 * HA's card picker, real WebSocket `hass`).
 *
 * Functional assertions only — NO pixel snapshots (cross-OS font noise, per
 * CLAUDE.md FRONTEND_CROSS_OS_CHECKS). Run: `npm run test:taste`.
 * First run builds a HA venv (slow); subsequent runs reuse it.
 */
import { HomeAssistant, PlaywrightBrowser } from "hass-taste-test";
import assert from "node:assert";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CARD = resolve(__dirname, "../../www/bosch-camera-card.js");

// default_config gives the full frontend + auth + websocket that hass-taste-test
// needs to log in, register the resource and build a dashboard. The card only
// needs the `camera_entity` config key (not a live entity) to render its chrome.
//
// PLATFORM NOTE: this runs cleanly on Linux CI. On macOS dev it fails at HA
// startup because default_config's dhcp/network discovery imports `pyroute2`
// (Linux-only netlink) — an HA-core / aiodiscover limitation, not our code.
// A trimmed config that drops dhcp/network also fails (hass-taste-test then
// can't complete onboarding/auth). So layer-5 E2E is a Linux/CI job — see
// .github/workflows/e2e-taste.yml and docs/ci-cd.md.
const CONFIG = `
default_config:
`;

let hass;
try {
  hass = await HomeAssistant.create(CONFIG, { browser: new PlaywrightBrowser("chromium") });
  await hass.addResource(CARD, "module");

  const dashboard = await hass.Dashboard([
    { type: "custom:bosch-camera-card", camera_entity: "camera.test", apple_style: true },
  ]);

  const html = await dashboard.cards[0].html();

  // The custom element must have upgraded + rendered a shadow DOM in real HA…
  assert.ok(html && html.length > 80, "card rendered shadow content");
  // …and HA must NOT have fallen back to its error card.
  assert.ok(
    !/hui-error-card|Custom element doesn't exist|custom element doesn't exist/i.test(html),
    "no HA error card (custom element loaded)",
  );
  // sanity: an <ha-card> (or our wrapper) is present
  assert.ok(/ha-card|bosch|img-wrapper|ap-/i.test(html), "card chrome present in shadow DOM");

  console.log("✓ hass-taste-test: bosch-camera-card loaded + rendered in a real HA frontend");
  console.log(`  rendered shadow HTML: ${html.length} chars`);
} catch (e) {
  console.error("✗ hass-taste-test failed:", e && e.message ? e.message : e);
  process.exitCode = 1;
} finally {
  if (hass) {
    try {
      await hass.close();
    } catch {
      /* ignore teardown errors */
    }
  }
}
