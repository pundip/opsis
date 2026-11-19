// Sign-in (AUTH_MODE=local or full): email and password, and Sign in with Google.
import { useState } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { Button, ErrorNote, Field, Input } from "../components/ui";
import { useSignIn } from "../lib/access";
import type { AppConfig } from "../lib/config";
import { url } from "../lib/runtime";

// Reasons the server gives when Google sign-in is refused (?error=...).
const GOOGLE_ERRORS: Record<string, string> = {
  google_not_allowed: "That Google account is not allowed to sign in to this workspace.",
  google_not_invited: "That Google account has not been invited. Ask an admin to invite your email address.",
  google_unverified: "Google has not verified the email address of that account.",
  google_deactivated: "Your account has been deactivated. Ask an admin.",
  google_expired: "The sign-in took too long or was started in another browser. Try again.",
  google_cancelled: "Sign-in with Google was cancelled.",
  google_unavailable: "Google could not be reached. Try again in a moment.",
  google_failed: "Google sign-in did not work. Try again, or ask an admin.",
};

export function AuthFrame({ config, title, children }: { config: AppConfig; title: string; children: React.ReactNode }) {
  return (
    <main className="grid min-h-screen place-items-center bg-zinc-50 p-6 dark:bg-zinc-950">
      <div className="w-full max-w-sm rounded-lg border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
        <div className="mb-5 flex items-center gap-2">
          <span className="grid size-8 place-items-center rounded-md bg-brand text-sm font-bold text-white dark:text-zinc-950">
            {config.appName.slice(0, 1)}
          </span>
          <span className="text-lg font-semibold">{config.appName}</span>
        </div>
        <h1 className="mb-4 text-xl font-semibold">{title}</h1>
        {children}
      </div>
    </main>
  );
}

export function Login({ config }: { config: AppConfig }) {
  const signIn = useSignIn();
  const location = useLocation();
  const [params] = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const googleError = params.get("error");
  // Come back to the page that asked for sign-in, never to the sign-in page itself.
  const next = location.pathname === "/login" ? "/" : `${location.pathname}${location.search}`;

  return (
    <AuthFrame config={config} title="Sign in">
      {googleError && (
        <p role="alert" className="mb-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/50 dark:text-red-300">
          {GOOGLE_ERRORS[googleError] ?? GOOGLE_ERRORS.google_failed}
        </p>
      )}
      {config.authMode === "full" && (
        <>
          <a
            href={url(`/api/auth/google/start?next=${encodeURIComponent(next)}`)}
            className="flex w-full items-center justify-center gap-2 rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm font-medium hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:hover:bg-zinc-800"
          >
            Sign in with Google
          </a>
          <p className="my-4 text-center text-xs text-zinc-500">or with your email and password</p>
        </>
      )}
      <form
        className="space-y-3"
        onSubmit={(event) => {
          event.preventDefault();
          signIn.mutate({ email: email.trim(), password });
        }}
      >
        <Field label="Email" htmlFor="login-email">
          <Input id="login-email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field label="Password" htmlFor="login-password">
          <Input
            id="login-password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <ErrorNote error={signIn.error} />
        <Button type="submit" variant="primary" busy={signIn.isPending} className="w-full">
          Sign in
        </Button>
      </form>
      <p className="mt-4 text-xs text-zinc-500">
        Forgotten your password? Ask an admin for a reset link.
      </p>
    </AuthFrame>
  );
}
