// Visual regression config. Renders the real web UI (Flask app, no Home
// Assistant backend needed for the static shell) and screenshots it so that a
// dependency bump (e.g. a Tailwind minor) changing the rendered output is
// caught. Baselines must be generated in CI — see .github/workflows/visual-baselines.yml.
const { defineConfig, devices } = require("@playwright/test");

const PORT = 5057;

module.exports = defineConfig({
  testDir: "./tests/visual",
  snapshotPathTemplate: "{testDir}/__screenshots__/{testFilePath}/{arg}{ext}",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  expect: {
    // Absorb sub-pixel anti-aliasing noise; a real CSS regression diffs far more.
    toHaveScreenshot: { maxDiffPixelRatio: 0.02, animations: "disabled" },
  },
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
  ],
  webServer: {
    // DATA_DIR points the app's persistent storage at a throwaway dir (no /data
    // mount outside the add-on). No HA_URL/HA_TOKEN: the static shell renders
    // without a backend; data panels are masked in the test.
    command: `DATA_DIR=$(mktemp -d) WEB_UI_PORT=${PORT} LOG_LEVEL=ERROR python3 web_ui.py`,
    url: `http://127.0.0.1:${PORT}/`,
    timeout: 60_000,
    reuseExistingServer: !process.env.CI,
    stdout: "pipe",
    stderr: "pipe",
  },
});
