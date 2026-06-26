// Visual regression for the main UI. The header and layout shell are static
// and render without a Home Assistant backend; the data panels are not, so
// they are masked. A dependency bump that changes the rendered styling (e.g. a
// Tailwind minor altering generated CSS) shifts the screenshot and fails here.
const { test, expect } = require("@playwright/test");

test("home page shell renders with expected styling", async ({ page }) => {
  await page.goto("/");

  // Static, data-independent anchor — must be visible before we snapshot.
  await expect(page.locator("header.desktop-header")).toBeVisible();

  // Let fonts and layout settle (data fetches fail without HA — that's fine).
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(1000);

  await expect(page).toHaveScreenshot("home.png", {
    fullPage: true,
    // Data-dependent regions are empty/error without a backend → mask them.
    mask: [page.locator(".panel-container")],
  });
});
