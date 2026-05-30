import { defineConfig, devices } from "@playwright/test";

// Cross-browser smoke for the Lovelace card bundle. Serves the repo statically
// and loads /www/bosch-camera-card.js in Chromium, Firefox and WebKit so a JS
// parse/registration error that only happens in one engine is caught. Pixel
// screenshots are intentionally NOT used here — cross-OS font rendering makes
// them noisy (see research); this is a functional smoke, run on the CI OS matrix.
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
    { name: "webkit",   use: { ...devices["Desktop Safari"] } },
  ],
});
