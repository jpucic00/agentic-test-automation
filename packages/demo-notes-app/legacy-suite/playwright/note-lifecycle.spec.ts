// Hand-written migration example for the demo notes app.
// @Xray(testCase = "NOTE-3")
import { expect, test } from "@playwright/test";

test.describe("Note lifecycle", () => {
  test("create, edit, then delete a note", async ({ page }) => {
    await page.goto("http://localhost:3000/login");
    await page.getByTestId("login-email").fill("demo@demo.test");
    await page.getByTestId("login-password").fill("Passw0rd!");
    await page.getByTestId("login-submit").click();
    await page.waitForURL("**/notes");

    const suffix = Date.now().toString(36);
    const title = `Errands ${suffix}`;

    await test.step("create", async () => {
      await page.getByText("New note", { exact: true }).click();
      await page.locator("css=input[name='title']").fill(title);
      await page.locator("css=textarea[name='body']").fill("Post office, bank");
      await page.getByText("Save note", { exact: true }).click();
      await expect(page.locator(".notes-list .note-item h3", { hasText: title })).toBeVisible();
    });

    await test.step("edit", async () => {
      const item = page.locator(".note-item", { hasText: title });
      await item.getByText("Edit", { exact: true }).click();
      await page.locator("css=textarea[name='body']").fill("Post office only");
      await page.getByText("Save note", { exact: true }).click();
      await expect(item.locator(".note-body")).toHaveText("Post office only");
    });

    await test.step("delete", async () => {
      const item = page.locator(".note-item", { hasText: title });
      await item.getByText("Delete", { exact: true }).click();
      await expect(page.locator(".note-item", { hasText: title })).toHaveCount(0);
    });
  });
});
