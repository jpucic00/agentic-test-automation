// Playwright test harness for AI-generated tests (Phase 1.B, guide §3.11).
//
// Generated tests are written to ./tests by the orchestrator and executed via
// `npx playwright test` from this directory. retries=0 — the Healer agent owns
// retries, not the runner. Each test logs itself in (context-driven auth) using the
// disposable staging dummy creds from project_context.md — no saved session.
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  use: {
    headless: true,
    ignoreHTTPSErrors: true,
    trace: 'on-first-retry',
  },
  retries: 0, // we handle retries via the Healer
  reporter: [['json'], ['list']],
});
