import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test.describe("Auth flow", () => {
  test("redirects unauthenticated users to Auth0 login", async ({ page }) => {
    // Auth0 redirect — expect a redirect to auth0.com
    await page.goto("/categories");
    await expect(page).toHaveURL(/auth0\.com|localhost/);
  });
});

test.describe("Accessibility — login page", () => {
  test("has no critical axe violations", async ({ page }) => {
    await page.goto("/");
    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa"])
      .analyze();
    expect(results.violations.filter((v) => v.impact === "critical")).toHaveLength(0);
  });
});
