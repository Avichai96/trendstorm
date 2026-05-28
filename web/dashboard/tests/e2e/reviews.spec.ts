import { test, expect, type Page } from "@playwright/test";

async function requireAuth(page: Page) {
  if (!process.env.PLAYWRIGHT_AUTH_TOKEN) {
    test.skip();
  }
}

test.describe("Reviews page", () => {
  test("shows reviews heading for reviewer role", async ({ page }) => {
    await requireAuth(page);
    await page.goto("/reviews");
    await expect(page.getByRole("heading", { name: "Reviews" })).toBeVisible();
  });

  test("opens decision confirmation dialog on Approve click", async ({ page }) => {
    await requireAuth(page);
    await page.goto("/reviews");

    // Only proceed if there are pending reviews
    const firstReview = page.locator("a[href^='/reviews/']").first();
    const count = await firstReview.count();
    if (count === 0) {
      test.skip();
      return;
    }

    await firstReview.click();
    await page.getByRole("button", { name: "Approve" }).click();
    await expect(page.getByText("Approve this analysis?")).toBeVisible();
  });
});
