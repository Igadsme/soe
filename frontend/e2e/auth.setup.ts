import { test as setup } from "@playwright/test";
import { login } from "./helpers";

// One sign-in for the whole run: the API enforces 5 attempts/min per user+IP, which is the real policy.
setup("sign in once", async ({ page }) => {
  await login(page);
  await page.context().storageState({ path: "playwright/.auth/operator.json" });
});
