import { test, expect } from "@playwright/test";

// scratch probe — deleted before commit
test("probe stacking + sticky", async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 900 });
  await page.goto("/control/fleet");
  await expect(page.locator(".fleet-subtabs")).toBeVisible({ timeout: 20_000 });
  await page.locator(".fleet-subtabs button", { hasText: "Plan" }).first().click();
  await expect(page.locator(".fleet-plan-row").first()).toBeVisible({ timeout: 25_000 });
  await page.locator(".fleet-plan-row").first().click();
  await page.waitForTimeout(1500);

  const chain = await page.evaluate(() => {
    const el = document.querySelector<HTMLElement>(".fleet-subtabs-layer");
    const out: string[] = [];
    let node: HTMLElement | null = el;
    while (node && node !== document.documentElement) {
      const cs = getComputedStyle(node);
      const makesCtx =
        cs.position === "fixed" ||
        cs.position === "sticky" ||
        (cs.zIndex !== "auto" && cs.position !== "static") ||
        cs.transform !== "none" ||
        cs.filter !== "none" ||
        cs.backdropFilter !== "none" ||
        cs.isolation === "isolate" ||
        cs.contain.includes("paint") ||
        cs.contain.includes("layout") ||
        cs.willChange !== "auto" ||
        (cs.opacity !== "1");
      out.push(`${node.tagName}.${String(node.className).slice(0, 44)} | pos=${cs.position} z=${cs.zIndex} tf=${cs.transform !== "none"} filt=${cs.filter !== "none"} bdf=${cs.backdropFilter !== "none"} iso=${cs.isolation} contain=${cs.contain} op=${cs.opacity} CTX=${makesCtx}`);
      node = node.parentElement;
    }
    const backdrop = document.querySelector<HTMLElement>(".hc-backdrop-in");
    return { chain: out, backdropParent: backdrop?.parentElement?.tagName ?? null };
  });
  console.log("STACKING CHAIN (subtab layer -> up):\n" + chain.chain.join("\n"));
  console.log("backdrop parent:", chain.backdropParent);
});

test("probe ingest sticky geometry", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/control/fleet");
  await expect(page.locator(".fleet-subtabs")).toBeVisible({ timeout: 20_000 });
  await page.locator(".fleet-subtabs button", { hasText: "Plan" }).first().click();
  await expect(page.locator(".fleet-plan-row").first()).toBeVisible({ timeout: 25_000 });
  await page.locator(".fleet-plan-row", { hasText: "Landing-Loop" }).first().click();
  await page.waitForTimeout(1200);
  await page.locator("#fleet-plan-tab-check").click();
  await page.getByRole("button", { name: "Übergabe prüfen" }).click();
  await expect(page.locator(".fleet-plan-ingest")).toBeVisible({ timeout: 20_000 });

  const geo = await page.evaluate(() => {
    const b = document.querySelector<HTMLElement>(".fleet-plan-ingest")!;
    const out: string[] = [];
    let node: HTMLElement | null = b;
    while (node && node !== document.documentElement) {
      const cs = getComputedStyle(node);
      const r = node.getBoundingClientRect();
      out.push(`${node.tagName}.${String(node.className).slice(0, 40)} | pos=${cs.position} of=${cs.overflow}/${cs.overflowY} h=${Math.round(r.height)} top=${Math.round(r.top)} bot=${Math.round(r.bottom)}`);
      node = node.parentElement;
    }
    const rect = b.getBoundingClientRect();
    return {
      chain: out,
      btn: { top: Math.round(rect.top), bottom: Math.round(rect.bottom) },
      innerH: window.innerHeight,
      scrollY: window.scrollY,
      docH: document.documentElement.scrollHeight,
      cs: getComputedStyle(b).position,
    };
  });
  console.log("INGEST GEOMETRY:", JSON.stringify({ btn: geo.btn, innerH: geo.innerH, scrollY: geo.scrollY, docH: geo.docH, pos: geo.cs }, null, 1));
  console.log("ANCESTORS:\n" + geo.chain.join("\n"));
});
