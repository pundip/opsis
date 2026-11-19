// Accept an invitation, or reset a password, from a one-time link (/set-password/:token).
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Button, ErrorNote, Field, Input } from "../components/ui";
import { usePasswordLink, useUsePasswordLink } from "../lib/access";
import type { AppConfig } from "../lib/config";
import { AuthFrame } from "./Login";

export const MIN_PASSWORD = 12;

export function SetPassword({ config }: { config: AppConfig }) {
  const { token = "" } = useParams();
  const link = usePasswordLink(token);
  const use = useUsePasswordLink(token);
  const navigate = useNavigate();
  const [name, setName] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");

  if (link.isPending) return <AuthFrame config={config} title="One moment">Checking the link...</AuthFrame>;
  if (link.isError) {
    return (
      <AuthFrame config={config} title="This link does not work">
        <ErrorNote error={link.error} />
      </AuthFrame>
    );
  }
  const invite = link.data.purpose === "invite";
  const mismatch = repeat.length > 0 && password !== repeat;
  return (
    <AuthFrame config={config} title={invite ? "Welcome" : "Choose a new password"}>
      <p className="mb-4 text-sm text-zinc-600 dark:text-zinc-400">
        {invite ? "You have been invited as " : "For "}
        <strong>{link.data.email}</strong>. Choose a password of at least {MIN_PASSWORD} characters.
      </p>
      <form
        className="space-y-3"
        onSubmit={(event) => {
          event.preventDefault();
          if (password !== repeat) return;
          use.mutate(
            { password, displayName: invite ? (name ?? link.data.displayName) : undefined },
            { onSuccess: () => navigate("/", { replace: true }) },
          );
        }}
      >
        {invite && (
          <Field label="Your name" htmlFor="sp-name">
            <Input id="sp-name" value={name ?? link.data.displayName} maxLength={200} onChange={(e) => setName(e.target.value)} />
          </Field>
        )}
        <Field label="New password" htmlFor="sp-password">
          <Input id="sp-password" type="password" autoComplete="new-password" required minLength={MIN_PASSWORD} value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Field label="Repeat the password" htmlFor="sp-repeat" error={mismatch ? "The two passwords are different." : undefined}>
          <Input id="sp-repeat" type="password" autoComplete="new-password" required value={repeat} onChange={(e) => setRepeat(e.target.value)} />
        </Field>
        <ErrorNote error={use.error} />
        <Button type="submit" variant="primary" busy={use.isPending} disabled={mismatch} className="w-full">
          {invite ? "Set password and sign in" : "Save and sign in"}
        </Button>
      </form>
    </AuthFrame>
  );
}
