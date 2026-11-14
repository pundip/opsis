import { useEffect } from "react";
import { Route, Routes } from "react-router-dom";
import { useConfig, useMe } from "../lib/config";
import { navItems } from "../lib/nav";
import { Home } from "../pages/Home";
import { Placeholder } from "../pages/Placeholder";
import { Layout } from "./Layout";

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

  const items = navItems(config.data, me.data);
  return (
    <Layout config={config.data} me={me.data} items={items}>
      <Routes>
        <Route path="/" element={<Home config={config.data} me={me.data} />} />
        {items.map((item) => (
          <Route key={item.path} path={item.path} element={<Placeholder title={item.label} />} />
        ))}
        <Route path="*" element={<Placeholder title="Page not found" missing />} />
      </Routes>
    </Layout>
  );
}
