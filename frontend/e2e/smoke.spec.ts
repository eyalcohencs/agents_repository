import { expect, test } from '@playwright/test';

test.describe('OnTrack SPA bootstrap smoke', () => {
  test('redirects root to /login without horizontal overflow', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole('heading', { name: 'OnTrack' })).toBeVisible();

    const hasHorizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    );
    expect(hasHorizontalOverflow).toBe(false);
  });

  test('supports direct navigation and reload on /privacy-and-terms-of-use', async ({ page }) => {
    const response = await page.goto('/privacy-and-terms-of-use');
    expect(response?.status()).toBe(200);
    await expect(page.getByRole('heading', { name: 'Privacy & Terms of Use' })).toBeVisible();

    const reloadResponse = await page.reload();
    expect(reloadResponse?.status()).toBe(200);
    await expect(page.getByRole('heading', { name: 'Privacy & Terms of Use' })).toBeVisible();

    await page.getByRole('link', { name: /back to login page/i }).click();
    await expect(page).toHaveURL(/\/login$/);
  });

  test('renders 404 placeholder for unknown routes', async ({ page }) => {
    await page.goto('/non-existent-route');
    await expect(page.getByRole('heading', { name: 'Page not found!' })).toBeVisible();

    await page.getByRole('link', { name: /return to home/i }).click();
    await expect(page).toHaveURL(/\/login$/);
  });
});
