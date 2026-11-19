import { expect, test } from "@playwright/test";
import { expectAccessible } from "./helpers";

test("mobile navigation, no horizontal overflow, skip link", async ({ page }) => {
  await page.goto("/");
  const toggle = page.getByRole("button", { name: "Menu" });
  await expect(toggle).toBeVisible();
  await expect(page.getByRole("link", { name: "Publishing" })).toBeHidden();
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await page.getByRole("link", { name: "Publishing" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Publishing" })).toBeVisible();
  for (const path of ["/", "/library", "/publishing", "/settings", "/agents"]) {
    await page.goto(path);
    await expect(page.getByText("Loading…")).toHaveCount(0);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow, `horizontal overflow on ${path}`).toBeLessThanOrEqual(1);
    await expectAccessible(page);
  }
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
});
