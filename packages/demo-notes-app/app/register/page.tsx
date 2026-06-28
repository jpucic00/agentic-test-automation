"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type SyntheticEvent, useState } from "react";

import { register } from "@/lib/auth";

export default function RegisterPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");

  function handleSubmit(event: SyntheticEvent) {
    event.preventDefault();
    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    const result = register(email, password);
    if (result.ok) {
      router.push("/notes");
    } else {
      setError(result.error);
    }
  }

  return (
    <section className="card">
      <h1>Create an account</h1>
      {/* Intentionally degraded for the resilience-ladder fixture: inputs have no id (implicit
          label association only → getByLabel), the submit is a non-semantic div, and the error
          has no id/role. See "Accessibility profile" in README.md. The form keeps onSubmit so
          Enter still submits. */}
      <form onSubmit={handleSubmit} noValidate>
        <label>
          Email
          <input
            name="email"
            type="email"
            value={email}
            autoComplete="username"
            onChange={(event) => setEmail(event.target.value)}
          />
        </label>
        <label>
          Password
          <input
            name="password"
            type="password"
            value={password}
            autoComplete="new-password"
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        <label>
          Confirm password
          <input
            name="confirm"
            type="password"
            value={confirm}
            autoComplete="new-password"
            onChange={(event) => setConfirm(event.target.value)}
          />
        </label>
        {error ? <p className="error">{error}</p> : null}
        <div className="btn" onClick={handleSubmit}>
          Register
        </div>
      </form>
      <p className="hint">
        Already have an account?{" "}
        <Link id="register-to-login" href="/login">
          Log in
        </Link>
      </p>
    </section>
  );
}
