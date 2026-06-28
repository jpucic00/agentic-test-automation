"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type SyntheticEvent, useState } from "react";

import { login } from "@/lib/auth";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  function handleSubmit(event: SyntheticEvent) {
    event.preventDefault();
    const result = login(email, password);
    if (result.ok) {
      router.push("/notes");
    } else {
      setError(result.error);
    }
  }

  return (
    <section className="card">
      <h1>Log in</h1>
      <form onSubmit={handleSubmit} noValidate>
        <label htmlFor="login-email">Email</label>
        <input
          id="login-email"
          name="email"
          type="email"
          value={email}
          autoComplete="username"
          onChange={(event) => setEmail(event.target.value)}
        />
        <label htmlFor="login-password">Password</label>
        <input
          id="login-password"
          name="password"
          type="password"
          value={password}
          autoComplete="current-password"
          onChange={(event) => setPassword(event.target.value)}
        />
        {error ? (
          <p id="login-error" role="alert" className="error">
            {error}
          </p>
        ) : null}
        <button id="login-submit" type="submit">
          Log in
        </button>
      </form>
      <p className="hint">
        No account?{" "}
        <Link id="login-to-register" href="/register">
          Register here
        </Link>
      </p>
    </section>
  );
}
