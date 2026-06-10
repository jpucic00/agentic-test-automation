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
    // The target app's manually-written `id=` attributes ARE the test id: the Planner's
    // browser_generate_locator emits getByTestId('login-submit'), which resolves to
    // [id="login-submit"] only because of this line. Must stay in sync with
    // "testIdAttribute": "id" in playwright-mcp-config.json (the read side).
    testIdAttribute: 'id',
    headless: true,
    ignoreHTTPSErrors: true,
    // retain-on-failure, NOT on-first-retry: retries stay 0 (the Healer owns retries),
    // so an on-first-retry trace would never be produced. Failed runs leave a
    // test-results/**/trace.zip that the runner surfaces as TestRunResult.trace_path.
    trace: 'retain-on-failure',
  },
  retries: 0, // we handle retries via the Healer
  reporter: [['json'], ['list']],
});
