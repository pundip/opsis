import { useQuery } from "@tanstack/react-query";
import { ApiError, apiGet } from "./api";

export interface Features {
  scheduledRefresh: boolean;
  publicShareLinks: boolean;
  email: boolean;
  assistant: boolean;
  sqlLab: boolean;
  backups: boolean;
}

export interface AppConfig {
  appName: string;
  version: string;
  deployMode: "local" | "server";
  authMode: "none" | "local" | "full";
  jobRunner: "inline" | "worker";
  mcpAuth: "none" | "token";
  assistantProvider: "anthropic" | "ollama" | null;
  publicBaseUrl: string;
  basePath: string;
  maxUploadMb: number;
  features: Features;
  calendar: {
    timezone: string;
    weekStart: "monday" | "sunday";
    fiscalYearStartMonth: number;
    fiscalYearLabel: "fy_end_year" | "year_span";
  };
  branding: {
    name: string;
    tagline?: string;
    colors?: { primary?: string; primaryDark?: string };
  };
}

export interface Me {
  id: string;
  email: string;
  displayName: string;
  role: "admin" | "editor" | "viewer";
  authMethod: string;
  permissions: string[];
}

export function useConfig() {
  return useQuery({
    queryKey: ["config"],
    queryFn: () => apiGet<AppConfig>("/api/config"),
    staleTime: Infinity,
  });
}

/** The signed-in user, or null when nobody is signed in. */
export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        return await apiGet<Me>("/api/me");
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null;
        throw error;
      }
    },
    staleTime: 60_000,
  });
}
