"use client";

import React, { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useAuth } from "@/components/providers/auth-provider";
import { ApiError, NetworkError } from "@/lib/api/problem";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/**
 * Login screen. The only unauthenticated route. On success it sends the operator
 * to the page they were trying to reach (`?next=`) or the dashboard. Errors are
 * surfaced inline rather than as a toast so they persist beside the form: a 401
 * is deliberately generic (username-enumeration-resistant, matching the backend),
 * a 429 tells them to wait, and a transport failure is distinguished from a
 * rejection so "server down" doesn't read as "wrong password".
 */
function LoginForm() {
  const { login, status } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const next = searchParams.get("next") || "/";

  // If already authenticated (e.g. navigated here directly), bounce onward.
  useEffect(() => {
    if (status === "authenticated") {
      router.replace(next);
    }
  }, [status, next, router]);

  async function onSubmit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login({ username, password });
      router.replace(next);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 429) {
          setError("Too many attempts. Please wait a few minutes and try again.");
        } else if (err.status === 401) {
          setError("Incorrect username or password.");
        } else {
          setError(err.problem.detail || err.problem.title || "Login failed.");
        }
      } else if (err instanceof NetworkError) {
        setError("Can't reach the server. Check your connection and try again.");
      } else {
        setError("An unexpected error occurred.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-base p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="items-center text-center">
          <div className="mb-1 flex items-center gap-2">
            <span className="inline-block h-5 w-1.5 rounded-full bg-accent" aria-hidden="true" />
            <CardTitle className="text-base">ProxSync</CardTitle>
          </div>
          <p className="text-xs text-fg-muted">Sign in to continue</p>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="username">Username</Label>
              <Input
                id="username"
                name="username"
                autoComplete="username"
                autoFocus
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                disabled={submitting}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={submitting}
              />
            </div>
            {error && (
              <p
                role="alert"
                className="rounded-md border border-danger bg-danger-muted px-3 py-2 text-xs text-danger"
              >
                {error}
              </p>
            )}
            <Button type="submit" disabled={submitting || !username || !password}>
              {submitting ? "Signing in\u2026" : "Sign in"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}

/**
 * The page default export wraps LoginForm in a Suspense boundary. This is
 * required by Next.js App Router because useSearchParams() triggers a
 * client-side bailout during static prerendering; the Suspense boundary
 * tells Next.js to render a fallback on the server and the real form on
 * the client.
 */
export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <main className="flex min-h-screen items-center justify-center bg-base p-4">
          <div className="text-sm text-fg-muted">Loading sign-in form&hellip;</div>
        </main>
      }
    >
      <LoginForm />
    </Suspense>
  );
}
