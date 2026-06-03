import { defineConfig, devices } from "@playwright/test";

// Cross-browser smoke for the Lovelace card bundle. Serves the repo statically
// and loads /www/bosch-camera-card.js in Chromium, Firefox and WebKit so a JS
// parse/registration error that only happens in one engine is caught. Pixel
// screenshots are intentionally NOT used here — cross-OS font rendering makes
// them noisy (see research); this is a functional smoke, run on the CI OS matrix.
//
// WebKit is skipped on the Windows CI leg ONLY. WebKit's real-world target is
// Safari (macOS/iOS), which the macOS + ubuntu legs already cover; Windows has
// no Safari, so the leg adds ~zero coverage. Meanwhile the Playwright WebKit
// build on Windows repeatedly hangs its worker at browser-process teardown -
// the browser doesn't exit after Browser.close, so the worker is force-killed
// after 300s and the run exits 1 even though all tests passed (Playwright
// #39753; bounded-but-still-failing on 1.60). Prior per-test cleanup guards
// (afterEach card removal + cooldown-shortening) reduced but never eliminated
// it, so we drop the flaky combination at the harness level. WebKit still runs
// on macOS + ubuntu and locally everywhere.
const skipWebkit = !!process.env.CI && process.platform === "win32";
export default defineConfig({
  testDir: "./test/e2e",
  timeout: 30000,
  fullyParallel: true,
  reporter: process.env.CI ? "github" : "list",
  use: { baseURL: "http://localhost:4321" },
  webServer: {
    command: "node scripts/serve-static.mjs",
    url: "http://localhost:4321/www/bosch-camera-card.js",
    reuseExistingServer: !process.env.CI,
    timeout: 30000,
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox",  use: { ...devices["Desktop Firefox"] } },
    ...(skipWebkit ? [] : [{ name: "webkit", use: { ...devices["Desktop Safari"] } }]),
  ],
});
