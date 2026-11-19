// A dashboard opened through a public share link (/s/:token). No sign-in, read-only,
// always the published version.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { Canvas } from "../components/dashboard/Canvas";
import { FiltersBar } from "../components/dashboard/FiltersBar";
import { Button, ErrorNote, Field, Input } from "../components/ui";
import { ApiError, apiGet, apiPost } from "../lib/api";
import type { AppConfig } from "../lib/config";
import type { DataSource, Spec } from "../lib/dashboard";
import { useChartTheme } from "../lib/theme";
import { useUrlViewState } from "../lib/urlState";
import { AuthFrame } from "./Login";

type Opened = { needsPassword: true } | { needsPassword: false; title: string; spec: Spec };

export function PublicDashboard({ config }: { config: AppConfig }) {
  const { token = "" } = useParams();
  const client = useQueryClient();
  const opened = useQuery({
    queryKey: ["public-link", token],
    queryFn: () => apiGet<Opened>(`/api/public/links/${token}`),
    retry: false,
  });
  const unlock = useMutation({
    mutationFn: (password: string) => apiPost(`/api/public/links/${token}/unlock`, { password }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["public-link", token] }),
  });
  const [password, setPassword] = useState("");
  const [state, setState] = useUrlViewState();
  const spec = opened.data && !opened.data.needsPassword ? opened.data.spec : undefined;
  const theme = useChartTheme(spec?.theme);

  if (opened.isPending) return <AuthFrame config={config} title="One moment">Opening the dashboard...</AuthFrame>;
  if (opened.isError) {
    const gone = opened.error instanceof ApiError && opened.error.status === 404;
    return (
      <AuthFrame config={config} title={gone ? "This link is not available" : "Something went wrong"}>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          {gone ? "It may have expired or been revoked. Ask the person who sent it for a new one." : String(opened.error.message)}
        </p>
      </AuthFrame>
    );
  }
  if (opened.data.needsPassword || !spec) {
    return (
      <AuthFrame config={config} title="This dashboard needs a password">
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            unlock.mutate(password);
          }}
        >
          <Field label="Password" htmlFor="link-unlock">
            <Input id="link-unlock" type="password" required value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
          <ErrorNote error={unlock.error} />
          <Button type="submit" variant="primary" busy={unlock.isPending} className="w-full">
            Open
          </Button>
        </form>
      </AuthFrame>
    );
  }

  const source: DataSource = { kind: "link", token };
  const key = `link:${token}`;
  return (
    <div className="min-h-screen bg-zinc-50 p-6 dark:bg-zinc-950">
      <header className="mb-4">
        <p className="text-xs text-zinc-500">{config.appName} · shared dashboard</p>
        <h1 className="text-2xl font-semibold">{spec.title}</h1>
        {spec.description && <p className="text-sm text-zinc-600 dark:text-zinc-400">{spec.description}</p>}
      </header>
      <FiltersBar dashboardId={key} spec={spec} source={source} state={state} onState={setState} />
      {spec.items.length === 0 ? (
        <p className="text-sm text-zinc-500">This dashboard is empty.</p>
      ) : (
        <Canvas dashboardId={key} spec={spec} source={source} state={state} onState={setState} theme={theme} calendar={config.calendar} />
      )}
    </div>
  );
}
