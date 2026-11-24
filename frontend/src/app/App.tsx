import { useEffect } from "react";
import { Navigate, Route, Routes, matchPath, useLocation } from "react-router-dom";
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
import { Placeholder } from "../pages/Placeholder";
import { Schedules } from "../pages/Schedules";
import { Settings } from "../pages/Settings";
import { Account, ForcedPasswordChange } from "../pages/Account";
import { Audit } from "../pages/Audit";
import { Login } from "../pages/Login";
import { PublicDashboard } from "../pages/PublicDashboard";
import { SetPassword } from "../pages/SetPassword";
import { Tokens } from "../pages/Tokens";
import { Users } from "../pages/Users";
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
  const location = useLocation();
  // Pages that work without signing in: a public share link and a password link.
  const shareLink = matchPath("/s/:token", location.pathname);
  const passwordLink = matchPath("/set-password/:token", location.pathname);
  const me = useMe(!shareLink && !passwordLink);

  useEffect(() => {
    if (!config.data) return;
    document.title = config.data.appName;
    const colors = config.data.branding.colors;
    const root = document.documentElement;
    if (colors?.primary) root.style.setProperty("--brand", colors.primary);
    if (colors?.primaryDark) root.style.setProperty("--brand-dark", colors.primaryDark);
  }, [config.data]);

  if (config.data && (shareLink || passwordLink)) {
    // Inside Routes, so the pages can read :token with useParams.
    return (
      <Routes>
        <Route path="/s/:token" element={<PublicDashboard config={config.data} />} />
        <Route path="/set-password/:token" element={<SetPassword config={config.data} />} />
      </Routes>
    );
  }
  if (config.isPending || me.isPending) return <FullPage title="Loading" />;
  if (config.isError || me.isError) {
    return (
      <FullPage title="Cannot reach the server">
        {(config.error ?? me.error)?.message}. Check that the stack is running, then reload.
      </FullPage>
    );
  }
  if (me.data === null) return <Login config={config.data} />;
  if (me.data.mustChangePassword) return <ForcedPasswordChange config={config.data} />;

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
        return <Models me={user} />;
      case "/dashboards":
        return <Dashboards me={user} />;
      case "/users":
        return <Users me={user} />;
      case "/audit":
        return <Audit config={appConfig} />;
      case "/schedules":
        return <Schedules />;
      case "/settings":
        return <Settings config={appConfig} />;
      default:
        return null;
    }
  };
  return (
    <Layout config={config.data} me={me.data} items={items}>
      <Routes>
        <Route path="/" element={<Home config={config.data} me={me.data} />} />
        <Route path="/login" element={<Navigate to="/" replace />} />
        <Route path="/account" element={<Account config={appConfig} me={user} />} />
        <Route path="/tokens" element={<Tokens />} />
        {items.map((item) => (
          <Route
            key={item.path}
            path={item.path}
            element={built(item.path) ?? <Placeholder title={item.label} />}
          />
        ))}
        {allowed("/datasets") && (
          <Route path="/datasets/:id" element={<DatasetDetail config={config.data} me={user} />} />
        )}
        {allowed("/dashboards") && (
          <>
            <Route path="/dashboards/:id" element={<DashboardPage config={appConfig} me={user} />} />
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
