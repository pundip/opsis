import { useEffect } from "react";
import { Route, Routes } from "react-router-dom";
import { useConfig, useMe } from "../lib/config";
import { navItems } from "../lib/nav";
import { Connections } from "../pages/Connections";
import { DashboardEditor } from "../pages/DashboardEditor";
import { DashboardPage } from "../pages/DashboardPage";
import { Dashboards } from "../pages/Dashboards";
import { DatasetDetail } from "../pages/DatasetDetail";
import { Datasets } from "../pages/Datasets";
import { Home } from "../pages/Home";
import { Jobs } from "../pages/Jobs";
import { ModelDetail } from "../pages/ModelDetail";
import { Models } from "../pages/Models";
import { Placeholder } from "../pages/Placeholder";import { Layout } from "./Layout";

function FullPage({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <main className="grid min-h-screen place-items-center p-6">
      <div className="max-w-md text-center">
        <h1 className="text-xl font-semibold">{title}</h1>
        {children && <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">{children}</p>}
      </div>
    </main>
  );
}

export function App() {
  const config = useConfig();
  const me = useMe();

  useEffect(() => {
    if (!config.data) return;
    document.title = config.data.appName;
    const colors = config.data.branding.colors;
    const root = document.documentElement;
    if (colors?.primary) root.style.setProperty("--brand", colors.primary);
    if (colors?.primaryDark) root.style.setProperty("--brand-dark", colors.primaryDark);
  }, [config.data]);

  if (config.isPending || me.isPending) return <FullPage title="Loading" />;
  if (config.isError || me.isError) {
    return (
      <FullPage title="Cannot reach the server">
        {(config.error ?? me.error)?.message}. Check that the stack is running, then reload.
      </FullPage>
    );
  }
  if (me.data === null) {
    return (
      <FullPage title={`Sign in to ${config.data.appName}`}>
        This server requires sign-in (AUTH_MODE={config.data.authMode}). The sign-in screen is not
        part of this build yet.
      </FullPage>
    );
  }

  const appConfig = config.data;
  const user = me.data;
  const items = navItems(appConfig, user);
  const allowed = (path: string) => items.some((item) => item.path === path);
  // Pages that exist so far. Other navigation entries show a placeholder.
  const built = (path: string) => {
    switch (path) {
      case "/connections":
        return <Connections config={appConfig} me={user} />;
      case "/datasets":
        return <Datasets />;
      case "/jobs":
        return <Jobs config={appConfig} />;
      case "/models":
        return <Models />;
      case "/dashboards":
        return <Dashboards me={user} />;
      default:
        return null;
    }
  };
  return (
    <Layout config={config.data} me={me.data} items={items}>
      <Routes>
        <Route path="/" element={<Home config={config.data} me={me.data} />} />
        {items.map((item) => (
          <Route
            key={item.path}
            path={item.path}
            element={built(item.path) ?? <Placeholder title={item.label} />}
          />
        ))}
        {allowed("/datasets") && (
          <Route path="/datasets/:id" element={<DatasetDetail config={config.data} />} />
        )}
        {allowed("/dashboards") && (
          <>
            <Route path="/dashboards/:id" element={<DashboardPage config={appConfig} />} />
            <Route path="/dashboards/:id/edit" element={<DashboardEditor config={appConfig} />} />
          </>
        )}
        {allowed("/models") && (
          <Route path="/models/:dataset" element={<ModelDetail config={appConfig} me={user} />} />
        )}
        <Route path="*" element={<Placeholder title="Page not found" missing />} />
      </Routes>
    </Layout>
  );
}
