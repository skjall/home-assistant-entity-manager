// "Only changes" together with "show disabled": a switched-off entity is work
// to do, so it belongs in the list the first filter leaves behind.
//
// The two filters used to cancel each other out. "Show disabled" brought the
// switched-off rows in, "only changes" threw them straight back out because
// they had no rename pending, and the combination showed nothing at all.
//
// Driven against the real page with a hierarchy planted in the component, so
// the assertions are about the rendered list, not about a copy of the rules.
const { test, expect } = require("@playwright/test");

// _previewName and _previewId are the component's own cache, the one
// precomputeEntityPreviews fills before the list is drawn. Setting them here
// puts both entities in the state "already named as the rules want", without
// standing a rule engine up behind the page.
const HIERARCHY = {
  floors: [],
  areas: [{ id: "bad", name: "Badezimmer" }],
  devices: [
    { id: "d1", name: "Badezimmer Lampe", base_name: "Lampe", area_id: "bad" },
  ],
  entities: [
    // Nothing outstanding: named as it should be, switched on.
    {
      registry_id: "r-settled",
      id: "light.badezimmer_lampe_licht",
      device_id: "d1",
      original_name: "Licht",
      base_name: "Licht",
      _previewId: "light.badezimmer_lampe_licht",
      _previewName: "Licht",
    },
    // Switched off, and its name is already right: the only thing left to do
    // about it is to switch it on.
    {
      registry_id: "r-off",
      id: "light.badezimmer_lampe_nachtlicht",
      device_id: "d1",
      original_name: "Nachtlicht",
      base_name: "Nachtlicht",
      disabled_by: "user",
      _previewId: "light.badezimmer_lampe_nachtlicht",
      _previewName: "Nachtlicht",
    },
  ],
};

async function plant(page) {
  await page.goto("/");
  await page.waitForFunction(() => window.Alpine && document.querySelector("[x-data]"));
  await page.evaluate((hierarchy) => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.loading = false;
    d.coreStarting = false;
    d.hierarchy = hierarchy;
    d.selectedArea = "bad";
    d.selectedDevice = "d1";
    d.selectedDeviceData = hierarchy.devices[0];
  }, HIERARCHY);
}

async function idsInList(page, { onlyChanges, showDisabled }) {
  return page.evaluate(
    ({ onlyChanges, showDisabled }) => {
      const d = window.Alpine.$data(document.querySelector("[x-data]"));
      d.onlyChanges = onlyChanges;
      d.showDisabled = showDisabled;
      return d.filteredEntities.map((e) => e.id);
    },
    { onlyChanges, showDisabled },
  );
}

test("a switched-off entity counts as a change once disabled ones are shown", async ({ page }) => {
  await plant(page);

  expect(await idsInList(page, { onlyChanges: true, showDisabled: true })).toEqual([
    "light.badezimmer_lampe_nachtlicht",
  ]);
});

test("without show-disabled the switched-off entity stays out", async ({ page }) => {
  await plant(page);

  expect(await idsInList(page, { onlyChanges: true, showDisabled: false })).toEqual([]);
});

test("show-disabled alone still lists everything on the device", async ({ page }) => {
  await plant(page);

  expect(await idsInList(page, { onlyChanges: false, showDisabled: true })).toEqual([
    "light.badezimmer_lampe_licht",
    "light.badezimmer_lampe_nachtlicht",
  ]);
});

test("the device and its area stay reachable so the row can be got at", async ({ page }) => {
  await plant(page);

  const reachable = await page.evaluate(() => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.onlyChanges = true;
    d.showDisabled = true;
    return {
      devices: d.filteredDevices.map((x) => x.id),
      areas: d.sortedAreas.map((a) => a.id),
    };
  });

  expect(reachable.devices).toContain("d1");
  expect(reachable.areas).toContain("bad");
});

test("a switched-off row is not offered a rename it does not need", async ({ page }) => {
  await plant(page);

  const verdicts = await page.evaluate(() => {
    const d = window.Alpine.$data(document.querySelector("[x-data]"));
    d.onlyChanges = true;
    d.showDisabled = true;
    const off = d.hierarchy.entities.find((e) => e.disabled_by);
    return {
      // The tick on the row and "apply all" ask the narrower question.
      tickOffered: d.staysInChangesFilter(off),
      pendingChanges: d.pendingChangesCount,
      // The list asks the wider one.
      listed: d.showsUnderChangesFilter(off),
    };
  });

  expect(verdicts.tickOffered).toBe(false);
  expect(verdicts.pendingChanges).toBe(0);
  expect(verdicts.listed).toBe(true);
});
