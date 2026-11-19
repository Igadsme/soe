import { expect, test } from "@playwright/test";
import { expectAccessible, login } from "./helpers";

const signedOut = { storageState: { cookies: [], origins: [] } };

const MEDIA = process.env.SOE_E2E_MEDIA;
const PAGES = [["/", "Overview"], ["/discovery", "Discovery"], ["/library", "Content library"], ["/studio", "AI studio"], ["/publishing", "Publishing"],
  ["/analytics", "Analytics"], ["/revenue", "Revenue"], ["/agents", "AI agents"], ["/settings", "Settings"]] as const;

test.describe.configure({ mode: "serial" });

test.describe("signed out", () => {
  test.use(signedOut);
  test("unauthenticated visitors are sent to login and bad credentials are rejected", async ({ page }) => {
  await page.goto("/library");
  await expect(page).toHaveURL(/\/login\?next=%2Flibrary/);
  await page.getByLabel("Username").fill("operator");
  await page.getByLabel("Password").fill("wrong-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.locator("main").getByRole("alert")).toHaveText("invalid credentials");
  await expectAccessible(page);
  });

  test("signing in, signing out and revoked sessions return to login", async ({ page }) => {
    await page.goto("/agents");
    await expect(page).toHaveURL(/\/login/);
    await login(page, false);
    await expect(page).toHaveURL(/\/agents$/);
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/login/);
    await page.goto("/");
    await expect(page).toHaveURL(/\/login/);
  });
});

test("every page loads real data with empty states and passes axe", async ({ page }) => {
  await page.goto("/");
  for (const [path, heading] of PAGES) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
    await expect(page.getByText("Loading…")).toHaveCount(0);
    await expectAccessible(page);
  }
  await page.goto("/analytics");
  await expect(page.getByText(/No metrics collected yet/)).toBeVisible();
  await page.goto("/revenue");
  await expect(page.getByText("No ledger entries yet.")).toBeVisible();
  await expect(page.locator("section", { hasText: "Platform earnings" }).getByText("unavailable", { exact: true })).toHaveCount(3);
});

test("discovery without provider credentials shows an honest error", async ({ page }) => {
  await page.goto("/");
  await page.goto("/discovery");
  await page.getByLabel("Query").first().fill("cooking");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page.locator("main").getByRole("alert")).toContainText(/unavailable|not configured/i);
  await expect(page.getByText("No discovered candidates yet.")).toBeVisible();
});

test("upload, rights, real processing, preview and approval", async ({ page }) => {
  test.skip(!MEDIA, "SOE_E2E_MEDIA not set");
  await page.goto("/");
  await page.goto("/studio");
  await page.getByLabel("Title").fill("E2E talk");
  await page.getByLabel("Video file").setInputFiles(MEDIA!);
  await page.getByRole("button", { name: "Upload" }).click();
  await expect(page.getByText("Upload complete. Record rights authorization before processing.")).toBeVisible({ timeout: 120_000 });
  await page.getByRole("button", { name: "Start AI processing" }).click();
  await expect(page.locator("main").getByRole("alert")).toContainText(/rights/);
  await page.getByLabel("Evidence").fill("I recorded this talk myself and own all rights to it.");
  await page.getByLabel("youtube").check();
  await page.getByLabel("Commercial use").check();
  await page.getByLabel("Publishing allowed").check();
  await page.getByRole("button", { name: "Approve rights" }).click();
  await expect(page.getByText("Rights recorded.")).toBeVisible();
  await page.getByRole("button", { name: "Start AI processing" }).click();
  const outcome = page.getByText("Processing finished. Review the clips below.").or(page.locator("main").getByRole("alert"));
  await expect(outcome).toBeVisible({ timeout: 540_000 });
  await expect(page.locator("main").getByRole("alert")).toHaveCount(0);
  const preview = page.locator("video").first();
  await expect(preview).toBeVisible();
  await expect.poll(() => preview.evaluate((video: HTMLVideoElement) => video.readyState), { timeout: 30_000 }).toBeGreaterThan(0);
  const duration = await preview.evaluate((video: HTMLVideoElement) => video.duration);
  expect(duration).toBeGreaterThan(1);
  // Quality checks run as a separate job; the studio polls until each clip leaves qc_pending.
  await expect(page.getByText("qc pending")).toHaveCount(0, { timeout: 180_000 });
  const reviewable = page.locator("article.clip-card", { hasText: "pending review" });
  expect(await reviewable.count(), "at least one clip passes real QC").toBeGreaterThan(0);
  await reviewable.first().getByRole("button", { name: "Approve", exact: true }).click();
  await expect(page.getByText(/Clip \d+ approved\./)).toBeVisible();
  await expectAccessible(page);
  await page.goto("/library");
  await expect(page.locator("article.clip-card").first()).toBeVisible();
  await page.goto("/agents");
  await expect(page.getByRole("cell", { name: /^process_source/ }).first()).toBeVisible();
});

test("publishing shows unconfigured OAuth and validates the schedule form", async ({ page }) => {
  await page.goto("/");
  await page.goto("/publishing");
  await expect(page.getByText("Publishing is disabled by configuration. Nothing will be uploaded.")).toBeVisible();
  for (const name of ["YouTube", "TikTok", "Instagram"]) await expect(page.getByRole("button", { name: `Connect ${name}` })).toBeDisabled();
  await expect(page.getByText("No accounts connected.")).toBeVisible();
  const form = page.getByRole("button", { name: "Queue publication" });
  if (await form.count()) {
    await page.getByLabel("Title").fill("x".repeat(120));
    await expect(page.getByText("120/100")).toBeVisible();
    await form.click();
    await expect(page.locator("main").getByRole("alert")).toContainText("Title must be 100 characters or fewer.");
    await expect(page.locator("main").getByRole("alert")).toContainText("Choose a connected account.");
  }
  await expectAccessible(page);
  await page.goto("/publishing?oauth=failure&platform=youtube");
  await expect(page.getByText("Account connection failed. Try again.")).toBeVisible();
});

test("policy editing, emergency stop with confirmation, and audit trail", async ({ page }) => {
  await page.goto("/");
  await page.goto("/settings");
  await page.getByLabel("Minimum clip score").fill("65");
  await page.getByLabel("Publishing windows (hours)").fill("9-12, 18-22");
  await page.getByRole("button", { name: "Save policy" }).click();
  await expect(page.getByText("Automation policy saved.")).toBeVisible();
  await page.getByLabel("Publishing windows (hours)").fill("bad");
  await page.getByRole("button", { name: "Save policy" }).click();
  await expect(page.locator("main").getByRole("alert")).toContainText("Invalid window");
  await page.getByRole("button", { name: "Emergency stop" }).click();
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByText("Emergency stop engaged.")).toBeVisible();
  await expect(page.getByText("emergency stopped")).toBeVisible();
  await page.getByRole("button", { name: "Resume automation" }).click();
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByText("Automation resumed.")).toBeVisible();
  await page.goto("/agents");
  await expect(page.getByText(/system\.emergency_stop/).first()).toBeVisible();
  await expect(page.getByText(/policy/).first()).toBeVisible();
  await expectAccessible(page);
});

test("revenue: manual entry, estimated cost excluded from revenue, CSV import, duplicate rejected", async ({ page }) => {
  await page.goto("/revenue");
  const form = page.locator("section", { hasText: "Record an entry" });
  await form.getByLabel("Amount (USD)").fill("120.50");
  await form.getByLabel("Category").selectOption("sponsorship");
  await form.getByLabel("Reference").fill("Synthetic sponsorship invoice E2E-1");
  await form.getByRole("button", { name: "Record" }).click();
  await expect(page.getByText("Ledger entry recorded.")).toBeVisible();
  await form.getByLabel("Kind").selectOption("expense");
  await form.getByLabel("Category").selectOption("cloud_cost");
  await form.getByLabel("Amount (USD)").fill("20");
  await form.getByLabel("Reference").fill("Synthetic hosting estimate");
  await form.getByLabel("Cost is an estimate").check();
  await form.getByRole("button", { name: "Record" }).click();
  await expect(page.getByText("Ledger entry recorded.")).toBeVisible();
  const csv = "date,kind,category,amount,currency,platform,reference\n2026-10-01,revenue,platform_earnings,9.99,USD,tiktok,Synthetic statement\n";
  const file = { name: "statement.csv", mimeType: "text/csv", buffer: Buffer.from(csv) };
  const importer = page.locator("section", { hasText: "Import a statement" });
  await importer.getByLabel("CSV file").setInputFiles(file);
  await importer.getByRole("button", { name: "Import" }).click();
  await expect(page.getByText("Statement imported.")).toBeVisible();
  await expect(page.locator(".card", { hasText: "Revenue (verified + imported + manual)" })).toContainText("$130.49");
  await expect(page.locator(".card", { hasText: "Estimated costs" })).toContainText("$20.00");
  await expect(page.locator(".card", { hasText: /^Net/ })).toContainText("$110.49");
  await importer.getByLabel("CSV file").setInputFiles(file);
  await importer.getByRole("button", { name: "Import" }).click();
  await expect(page.locator("main").getByRole("alert")).toContainText("already imported");
  await expectAccessible(page);
});

test("populated pages stay accessible; evidence screenshots", async ({ page }) => {
  const dir = process.env.SOE_E2E_SCREENSHOTS;
  for (const [path, heading] of PAGES) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
    await expect(page.getByText("Loading…")).toHaveCount(0);
    await expectAccessible(page);
    if (dir) await page.screenshot({ path: `${dir}/${path === "/" ? "overview" : path.slice(1)}.png`, fullPage: true });
  }
});
