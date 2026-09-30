// Playwright test harness for AI-generated tests (Phase 1.B, guide §3.11).
//
// Generated tests are written to ./tests by the orchestrator and executed via
// `npx playwright test` from this directory. retries=0 — the Healer agent owns
// retries, not the runner. Each test logs itself in (context-driven auth) using the
// disposable staging dummy creds from project_context.md — no saved session.
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { defineConfig } from '@playwright/test';

// STAGING_BASE_URL from the environment, else from the repo's ../.env (Node doesn't load it;
// the real environment wins, as with the pipeline's load_dotenv). Unquoted values drop an
// inline " # comment" and a repeated key takes its last value, like python-dotenv.
function stagingBaseUrl(): string | undefined {
  if (process.env.STAGING_BASE_URL) return process.env.STAGING_BASE_URL;
  const envFile = resolve(__dirname, '..', '.env');
  if (!existsSync(envFile)) return undefined;
  let found: string | undefined;
  for (const line of readFileSync(envFile, 'utf8').split(/\r?\n/)) {
    const match = line.match(/^\s*(?:export\s+)?STAGING_BASE_URL\s*=\s*(.*)$/);
    if (!match) continue;
    const value = match[1].trim();
    const quoted = value.match(/^(['"])(.*)\1/);
    found = quoted ? quoted[2] : value.replace(/\s+#.*$/, '');
  }
  return found;
}

// The first STAGING_BASE_URL entry is the primary environment, as in the pipeline.
function primaryBaseUrl(): string | undefined {
  return stagingBaseUrl()
    ?.split(',')
    .map((url) => url.trim())
    .find(Boolean);
}

export default defineConfig({
  testDir: './tests',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  use: {
    // The environment under test. The runner (src/ai_test_gen/browser/runner.py) sets BASE_URL
    // per run — the primary STAGING_BASE_URL entry, then each secondary one — so a
    // baseURL-relative page.goto('/notes') follows the run. Absolute URLs ignore it.
    // A manual `npx playwright test` without BASE_URL runs against the primary environment.
    baseURL: process.env.BASE_URL || primaryBaseUrl(),
    // The target app's manually-written `id=` attributes ARE the test id: the Planner's
    // browser_generate_locator emits getByTestId('login-submit'), which resolves to
    // [id="login-submit"] only because of this line. Must stay in sync with
    // "testIdAttribute": "id" in playwright-mcp-config.json (the read side).
    testIdAttribute: 'id',
    headless: true,
    // Full desktop resolution so the app renders at its full layout, not a cramped default.
    // Keep in sync with the "viewport" in playwright-mcp-config.json (what the agents drive).
    viewport: { width: 1920, height: 1080 },
    ignoreHTTPSErrors: true,
    // retain-on-failure, NOT on-first-retry: retries stay 0 (the Healer owns retries),
    // so an on-first-retry trace would never be produced. Failed runs leave a
    // test-results/**/trace.zip that the runner surfaces as TestRunResult.trace_path.
    trace: 'retain-on-failure',
  },
  retries: 0, // we handle retries via the Healer
  reporter: [['json'], ['list']],
});
