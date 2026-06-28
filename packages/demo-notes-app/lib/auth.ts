// Mock authentication backed entirely by localStorage — no server, no database.
// Users and the current session live under the keys below. This is a DEMO target app
// for the test-generation pipeline; never model real auth on this.

export type User = { email: string; password: string };

const USERS_KEY = "demo.users";
const SESSION_KEY = "demo.session";

// A pre-seeded account so the "log in" scenarios are deterministic without first
// registering. It is re-seeded on every page load (see components/Bootstrap.tsx)
// because each automated test run starts with an empty localStorage.
export const DEMO_USER: User = { email: "demo@demo.test", password: "Passw0rd!" };

function hasStorage(): boolean {
  return typeof window !== "undefined" && !!window.localStorage;
}

function readUsers(): User[] {
  if (!hasStorage()) return [];
  try {
    const raw = window.localStorage.getItem(USERS_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function writeUsers(users: User[]): void {
  if (!hasStorage()) return;
  window.localStorage.setItem(USERS_KEY, JSON.stringify(users));
}

function setSession(email: string): void {
  if (!hasStorage()) return;
  window.localStorage.setItem(SESSION_KEY, email);
}

/** Idempotently ensure the demo user exists. Safe to call on every render/load. */
export function ensureSeed(): void {
  if (!hasStorage()) return;
  const users = readUsers();
  if (!users.some((u) => u.email.toLowerCase() === DEMO_USER.email.toLowerCase())) {
    writeUsers([...users, DEMO_USER]);
  }
}

export type AuthResult = { ok: true } | { ok: false; error: string };

export function register(email: string, password: string): AuthResult {
  ensureSeed();
  const trimmed = email.trim();
  if (!trimmed || !password) {
    return { ok: false, error: "Email and password are required." };
  }
  const users = readUsers();
  if (users.some((u) => u.email.toLowerCase() === trimmed.toLowerCase())) {
    return { ok: false, error: "An account with that email already exists." };
  }
  writeUsers([...users, { email: trimmed, password }]);
  setSession(trimmed);
  return { ok: true };
}

export function login(email: string, password: string): AuthResult {
  ensureSeed();
  const trimmed = email.trim().toLowerCase();
  const user = readUsers().find((u) => u.email.toLowerCase() === trimmed);
  if (!user || user.password !== password) {
    return { ok: false, error: "Invalid email or password." };
  }
  setSession(user.email);
  return { ok: true };
}

export function logout(): void {
  if (!hasStorage()) return;
  window.localStorage.removeItem(SESSION_KEY);
}

export function currentUser(): string | null {
  if (!hasStorage()) return null;
  return window.localStorage.getItem(SESSION_KEY);
}
