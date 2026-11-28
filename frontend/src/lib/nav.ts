import type { AppConfig, Me } from "./config";

export interface NavItem {
  path: string;
  label: string;
  group: "Build" | "Admin";
}

interface NavRule extends NavItem {
  permission?: string;
  feature?: keyof AppConfig["features"];
}

const RULES: NavRule[] = [
  { path: "/dashboards", label: "Dashboards", group: "Build", permission: "dashboards:view" },
  { path: "/datasets", label: "Datasets", group: "Build", permission: "datasets:edit" },
  { path: "/model", label: "Model", group: "Build", permission: "models:edit" },
  { path: "/connections", label: "Connections", group: "Admin", permission: "datasets:edit" },
  { path: "/jobs", label: "Jobs", group: "Admin", permission: "datasets:edit" },
  {
    path: "/schedules",
    label: "Schedules",
    group: "Admin",
    permission: "datasets:edit",
    feature: "scheduledRefresh",
  },
  { path: "/users", label: "Users", group: "Admin", permission: "users:manage" },
  { path: "/audit", label: "Audit log", group: "Admin", permission: "audit:view" },
  { path: "/settings", label: "Settings", group: "Admin", permission: "settings:manage" },
];

/**
 * Navigation the current user may see. This only hides links: the backend
 * enforces the same flags and permissions on every route.
 */
export function navItems(config: AppConfig, me: Me | null): NavItem[] {
  if (!me) return [];
  return RULES.filter((rule) => {
    if (rule.feature && !config.features[rule.feature]) return false;
    if (rule.permission && !me.permissions.includes(rule.permission)) return false;
    // User management has nothing to manage when there is a single built-in admin.
    if (rule.path === "/users" && config.authMode === "none") return false;
    return true;
  }).map(({ path, label, group }) => ({ path, label, group }));
}
