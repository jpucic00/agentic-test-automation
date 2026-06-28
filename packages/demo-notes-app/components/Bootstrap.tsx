"use client";

import { useEffect } from "react";

import { ensureSeed } from "@/lib/auth";

/**
 * Re-seeds the demo user into localStorage on every page load. Automated test runs
 * start with a fresh, empty browser context, so seeding must be idempotent and happen
 * every load — not once — for the "log in as the demo user" scenarios to be deterministic.
 */
export default function Bootstrap() {
  useEffect(() => {
    ensureSeed();
  }, []);
  return null;
}
