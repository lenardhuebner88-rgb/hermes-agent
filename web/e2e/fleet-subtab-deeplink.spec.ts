import { expect, test, type Page } from "@playwright/test";

function watchPage(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    if (message.text().includes("403")) return;
    errors.push(message.text());
  });
  page.on("response", (response) => {
    if (response.status() < 400) return;
    if (response.url().includes("/@fs/")) return;
    errors.push(`${response.status()} ${response.request().method()} ${response.url()}`);
  });
  return errors;
}

const SUBTABS = [
  ["heute", "Heute"],
  ["worker", "Worker"],
  ["ketten", "Ketten"],
  ["board", "Board"],
  ["plan", "Plan"],
  ["risiko", "Risiko"],
] as const;

for (const [subtab, label] of SUBTABS) {
  test(`URL öffnet Fleet-Unter-Tab ${label}`, async ({ page }) => {
    const errors = watchPage(page);

    await page.goto(`/control/fleet?subtab=${subtab}`);
    await expect(page.locator(".fleet-subtabs")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole("button", { name: `Subtab ${label}` })).toHaveAttribute("aria-pressed", "true");
    await expect.poll(() => new URL(page.url()).searchParams.has("subtab")).toBe(false);

    expect(errors, "Konsolen-/Netzfehler").toEqual([]);
  });
}

test("unbekannter Fleet-Unter-Tab bleibt fehlerfrei auf Heute", async ({ page }) => {
  const errors = watchPage(page);

  await page.goto("/control/fleet?subtab=quatsch");
  await expect(page.locator(".fleet-subtabs")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("button", { name: "Subtab Heute" })).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => new URL(page.url()).searchParams.has("subtab")).toBe(false);

  expect(errors, "Konsolen-/Netzfehler").toEqual([]);
});

test("Board-Deep-Link gewinnt gegen generischen Fleet-Unter-Tab", async ({ page }) => {
  const errors = watchPage(page);

  await page.goto("/control/fleet?board=irgendwas&subtab=risiko");
  await expect(page.locator(".fleet-subtabs")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("button", { name: "Subtab Board" })).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => new URL(page.url()).searchParams.has("subtab")).toBe(false);

  expect(errors, "Konsolen-/Netzfehler").toEqual([]);
});
