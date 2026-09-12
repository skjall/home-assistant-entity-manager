// End to end: the real app, the real templates, a real browser.
//
// The screenshot test next door compares pixels and needs baselines generated
// in CI. This one needs nothing: it asks whether each page answers at all,
// whether Alpine binds the markup - an unbound page looks fine in the HTML and
// is dead in the browser - and whether any JavaScript actually faults. A
// template edit that breaks a handler shows up here and in no unit test.
//
// There is no Home Assistant behind the app, so every data call fails and the
// app says so in the console. That is the environment, not a defect, and those
// messages are ignored; only genuine code faults count.
const { test, expect } = require("@playwright/test");

const PAGES = ["/", "/settings/naming", "/settings/rules", "/settings/system", "/api/docs"];

const CODE_FAULT = /ReferenceError|TypeError|SyntaxError|is not a function|is not defined|Alpine (?:Expression )?Error/i;

function faultsOf(page) {
  const faults = [];
  page.on("console", (message) => {
    if (message.type() === "error" && CODE_FAULT.test(message.text())) faults.push(message.text());
  });
  page.on("pageerror", (error) => faults.push(`pageerror: ${error.message}`));
  return faults;
}

for (const path of PAGES) {
  test(`renders ${path}`, async ({ page }) => {
    const faults = faultsOf(page);

    const response = await page.goto(path);

    expect(response.status(), `${path} answered ${response.status()}`).toBeLessThan(400);
    await page.waitForTimeout(1200);
    expect(faults, `JavaScript faults on ${path}`).toEqual([]);
  });
}

test("the main page binds Alpine", async ({ page }) => {
  await page.goto("/");

  await expect(page.locator("header.desktop-header")).toBeVisible();
  await expect(page.locator("[x-data]").first()).toBeVisible();
  expect(await page.evaluate(() => !!window.Alpine), "Alpine did not start").toBe(true);
});

test("the settings page shows the section of the open tab", async ({ page }) => {
  await page.goto("/settings/rules");

  await expect(page.locator("nav.settings-tabs")).toBeVisible();
  // Only the open tab's section is shown; the others stay in the markup.
  await expect(page.locator(".mapping-section:visible").first()).toBeVisible();
  // The exceptions area fills itself from a live call and must render its
  // empty state rather than stay blank. Anchored on the icon, so the assertion
  // survives whichever interface language the browser asks for.
  await expect(page.locator("i.ri-user-star-line").first()).toBeVisible();
});

test("the API documentation renders the description", async ({ page }) => {
  await page.goto("/api/docs");

  // Swagger UI builds the operation list from the served document; an empty
  // page means either the document or the bundle did not arrive.
  await expect(page.locator(".swagger-ui")).toBeVisible({ timeout: 20000 });
  await expect(page.locator(".opblock").first()).toBeVisible({ timeout: 20000 });
});
