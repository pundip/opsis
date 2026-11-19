import { describe, expect, it } from "vitest";
import type { AppConfig, Me } from "./config";
import { navItems } from "./nav";

const config: AppConfig = {
  appName: "Opsis",
  version: "0.1.0",
  deployMode: "local",
  authMode: "none",
  jobRunner: "inline",
  mcpAuth: "none",
  assistantProvider: "ollama",
  publicBaseUrl: "http://localhost:8080",
  basePath: "",
  maxUploadMb: 200,
  features: {
    scheduledRefresh: false,
    publicShareLinks: false,
    email: false,
    assistant: true,
    sqlLab: true,
    backups: false,
  },
  calendar: {
    timezone: "Australia/Melbourne",
    weekStart: "monday",
    fiscalYearStartMonth: 7,
    fiscalYearLabel: "fy_end_year",
  },
  branding: { name: "Opsis" },
};

const admin: Me = {
  id: "1",
  email: "admin@localhost",
  displayName: "Local admin",
  role: "admin",
  authMethod: "none",
  groups: [],
  mustChangePassword: false,
  hasPassword: false,
  google: false,
  permissions: [
    "dashboards:view", "datasets:edit", "models:edit", "connections:manage",
    "sql_lab:use", "users:manage", "settings:manage",
  ],
};

const paths = (c: AppConfig, me: Me | null) => navItems(c, me).map((item) => item.path);

describe("navItems", () => {
  it("shows nothing when nobody is signed in", () => {
    expect(paths(config, null)).toEqual([]);
  });

  it("follows feature flags", () => {
    expect(paths(config, admin)).toContain("/sql-lab");
    expect(paths(config, admin)).not.toContain("/schedules");
    const flipped = {
      ...config,
      features: { ...config.features, sqlLab: false, scheduledRefresh: true },
    };
    expect(paths(flipped, admin)).not.toContain("/sql-lab");
    expect(paths(flipped, admin)).toContain("/schedules");
  });

  it("hides user management in single-admin mode only", () => {
    expect(paths(config, admin)).not.toContain("/users");
    expect(paths({ ...config, authMode: "full" }, admin)).toContain("/users");
  });

  it("follows permissions", () => {
    const viewer: Me = { ...admin, role: "viewer", permissions: ["dashboards:view"] };
    expect(paths(config, viewer)).toEqual(["/dashboards"]);
  });
});

