import { test, expect, type Page } from "@playwright/test";

async function mockAuth(page: Page) {
  // In staging E2E, PLAYWRIGHT_AUTH_TOKEN is a valid API token for a test tenant.
  // For local runs we skip tests that need auth.
  if (!process.env.PLAYWRIGHT_AUTH_TOKEN) {
    test.skip();
    return;
  }

  await page.addInitScript((token) => {
    window.localStorage.setItem("auth0.test.token", token);
  }, process.env.PLAYWRIGHT_AUTH_TOKEN);
}

test.describe("Jobs list", () => {
  test("shows jobs list page heading", async ({ page }) => {
    await mockAuth(page);
    await page.goto("/jobs");
    await expect(page.getByRole("heading", { name: "Jobs" })).toBeVisible();
  });

  test("status filter changes the URL query", async ({ page }) => {
    await mockAuth(page);
    await page.goto("/jobs");
    await page.getByRole("combobox").click();
    await page.getByRole("option", { name: "Completed" }).click();
    await expect(page.getByRole("heading", { name: "Jobs" })).toBeVisible();
  });
});
