import { NavLink } from "react-router-dom";
import { useSignOut } from "../lib/access";
import type { AppConfig, Me } from "../lib/config";
import type { NavItem } from "../lib/nav";

interface Props {
  config: AppConfig;
  me: Me;
  items: NavItem[];
  children: React.ReactNode;
}

const linkClass = ({ isActive }: { isActive: boolean }) =>
  [
    "block rounded-md px-3 py-1.5 text-sm",
    isActive
      ? "bg-zinc-200/70 font-medium text-zinc-950 dark:bg-zinc-800 dark:text-white"
      : "text-zinc-600 hover:bg-zinc-200/50 dark:text-zinc-400 dark:hover:bg-zinc-800/60",
  ].join(" ");

export function Layout({ config, me, items, children }: Props) {
  const groups = ["Build", "Admin"] as const;
  const signOut = useSignOut();
  return (
    <div className="flex min-h-screen">
      <aside className="flex w-56 shrink-0 flex-col border-r border-zinc-200 bg-white px-3 py-4 dark:border-zinc-800 dark:bg-zinc-900">
        <NavLink to="/" className="mb-5 flex items-center gap-2 px-2">
          <span className="grid size-7 place-items-center rounded-md bg-brand text-sm font-bold text-white dark:text-zinc-950">
            {config.appName.slice(0, 1)}
          </span>
          <span className="text-base font-semibold">{config.appName}</span>
        </NavLink>
        <nav aria-label="Main" className="flex-1 space-y-5">
          <NavLink to="/" end className={linkClass}>
            Home
          </NavLink>
          {groups.map((group) => {
            const links = items.filter((item) => item.group === group);
            if (links.length === 0) return null;
            return (
              <div key={group}>
                <p className="px-3 pb-1 text-xs font-medium uppercase tracking-wide text-zinc-500">
                  {group}
                </p>
                {links.map((item) => (
                  <NavLink key={item.path} to={item.path} className={linkClass}>
                    {item.label}
                  </NavLink>
                ))}
              </div>
            );
          })}
          <NavLink to="/docs/guide" className={linkClass}>
            Help
          </NavLink>
        </nav>
        <div className="border-t border-zinc-200 px-2 pt-3 text-xs text-zinc-500 dark:border-zinc-800">
          <p className="truncate font-medium text-zinc-700 dark:text-zinc-300">{me.displayName}</p>
          <p className="truncate">
            {me.role} · v{config.version}
          </p>
          <nav aria-label="Your account" className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
            <NavLink to="/account" className="text-brand underline">
              Account
            </NavLink>
            <NavLink to="/tokens" className="text-brand underline">
              Access tokens
            </NavLink>
            {config.authMode !== "none" && (
              <button type="button" className="text-brand underline" onClick={() => signOut.mutate()}>
                Sign out
              </button>
            )}
          </nav>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 items-center justify-end gap-2 border-b border-zinc-200 bg-white px-5 dark:border-zinc-800 dark:bg-zinc-900">
          <span className="rounded-full border border-zinc-300 px-2.5 py-0.5 text-xs text-zinc-600 dark:border-zinc-700 dark:text-zinc-400">
            {config.deployMode} mode
          </span>
        </header>
        <main className="min-w-0 flex-1 p-6">{children}</main>
      </div>
    </div>
  );
}
