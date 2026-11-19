// Sign-in, people and groups, sharing, share links and the audit log.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from "./api";

// ---- signing in --------------------------------------------------------------

export function useSignIn() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { email: string; password: string }) =>
      apiPost<{ signedIn: boolean; mustChangePassword: boolean }>("/api/auth/login", input),
    onSuccess: () => client.invalidateQueries(),
  });
}

export function useSignOut(everywhere = false) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost(everywhere ? "/api/auth/logout-everywhere" : "/api/auth/logout"),
    onSuccess: () => {
      client.clear();
      void client.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

export function useChangePassword() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { currentPassword: string; newPassword: string }) => apiPost("/api/auth/password", input),
    onSuccess: () => client.invalidateQueries({ queryKey: ["me"] }),
  });
}

export interface PasswordLink {
  email: string;
  displayName: string;
  purpose: "invite" | "reset";
}

export function usePasswordLink(token: string) {
  return useQuery({
    queryKey: ["password-link", token],
    queryFn: () => apiGet<PasswordLink>(`/api/auth/links/${token}`),
    retry: false,
  });
}

export function useUsePasswordLink(token: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { password: string; displayName?: string }) => apiPost(`/api/auth/links/${token}`, input),
    onSuccess: () => client.invalidateQueries(),
  });
}

// ---- people and groups -----------------------------------------------------------

export type Role = "admin" | "editor" | "viewer";

export interface UserRow {
  id: string;
  email: string;
  displayName: string;
  role: Role;
  status: "active" | "invited" | "deactivated";
  isActive: boolean;
  hasPassword: boolean;
  google: boolean;
  attributes: Record<string, unknown>;
  groups: { id: string; name: string }[];
  lastLoginAt: string | null;
}

export interface Group {
  id: string;
  name: string;
  description: string | null;
  members: { id: string; name: string; email: string }[];
}

export const useUsers = () => useQuery({ queryKey: ["users"], queryFn: () => apiGet<UserRow[]>("/api/users") });
export const useGroups = () => useQuery({ queryKey: ["groups"], queryFn: () => apiGet<Group[]>("/api/groups") });

function usePeopleMutation<Input, Output>(run: (input: Input) => Promise<Output>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: run,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["users"] });
      void client.invalidateQueries({ queryKey: ["groups"] });
      void client.invalidateQueries({ queryKey: ["directory"] });
    },
  });
}

export const useInvite = () =>
  usePeopleMutation((input: { email: string; displayName: string; role: Role }) =>
    apiPost<{ user: UserRow; link: string }>("/api/users", input),
  );

export const useUpdateUser = () =>
  usePeopleMutation(({ id, ...changes }: { id: string } & Partial<{ displayName: string; role: Role; attributes: Record<string, unknown>; groupIds: string[]; isActive: boolean }>) =>
    apiPatch<UserRow>(`/api/users/${id}`, changes),
  );

export const useNewPasswordLink = () =>
  usePeopleMutation((id: string) => apiPost<{ link: string }>(`/api/users/${id}/password-link`));

export const useSaveGroup = () =>
  usePeopleMutation(({ id, ...body }: { id?: string; name: string; description?: string | null; memberIds?: string[] }) =>
    id ? apiPut<Group>(`/api/groups/${id}`, body) : apiPost<Group>("/api/groups", body),
  );

export const useDeleteGroup = () => usePeopleMutation((id: string) => apiDelete(`/api/groups/${id}`));

// ---- sharing ---------------------------------------------------------------------

export interface Directory {
  users: { id: string; name: string; email: string; role: Role }[];
  groups: { id: string; name: string; members: number }[];
}

export const useDirectory = (enabled = true) =>
  useQuery({ queryKey: ["directory"], queryFn: () => apiGet<Directory>("/api/directory"), enabled, staleTime: 60_000 });

export interface Share {
  grantee: string;
  role: "viewer" | "editor";
  name: string;
  kind: "user" | "group" | "everyone";
  email?: string;
}

export interface DashboardAccess {
  owner: { id: string; name: string; email: string } | null;
  shares: Share[];
}

export function useDashboardAccess(id: string) {
  return useQuery({ queryKey: ["dashboard-access", id], queryFn: () => apiGet<DashboardAccess>(`/api/dashboards/${id}/access`) });
}

export function useSetDashboardAccess(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (shares: { grantee: string; role: string }[]) =>
      apiPut<DashboardAccess>(`/api/dashboards/${id}/access`, { shares }),
    onSuccess: (saved) => client.setQueryData(["dashboard-access", id], saved),
  });
}

export interface DatasetAccess {
  restricted: boolean;
  grants: { grantee: string; name: string; kind: string; email?: string }[];
}

export function useDatasetAccess(id: string, enabled: boolean) {
  return useQuery({
    queryKey: ["dataset-access", id],
    queryFn: () => apiGet<DatasetAccess>(`/api/datasets/${id}/access`),
    enabled,
  });
}

export function useSetDatasetAccess(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { restricted: boolean; grants: string[] }) => apiPut<DatasetAccess>(`/api/datasets/${id}/access`, input),
    onSuccess: (saved) => {
      client.setQueryData(["dataset-access", id], saved);
      void client.invalidateQueries({ queryKey: ["datasets"] });
    },
  });
}

// ---- share links -------------------------------------------------------------------

export interface ShareLink {
  id: string;
  name: string | null;
  prefix: string;
  status: "active" | "expired" | "revoked";
  hasPassword: boolean;
  attributes: Record<string, unknown>;
  expiresAt: string | null;
  viewCount: number;
  lastViewedAt: string | null;
  createdBy: string | null;
  createdAt: string;
}

export function useShareLinks(dashboardId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["share-links", dashboardId],
    queryFn: () => apiGet<ShareLink[]>(`/api/dashboards/${dashboardId}/links`),
    enabled,
  });
}

export function useCreateShareLink(dashboardId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: {
      name?: string;
      expiresInDays?: number | null;
      password?: string;
      attributes?: Record<string, unknown> | "mine" | null;
    }) => apiPost<ShareLink & { url: string }>(`/api/dashboards/${dashboardId}/links`, input),
    onSuccess: () => client.invalidateQueries({ queryKey: ["share-links", dashboardId] }),
  });
}

export function useRevokeShareLink(dashboardId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (linkId: string) => apiPost<ShareLink>(`/api/links/${linkId}/revoke`),
    onSuccess: () => client.invalidateQueries({ queryKey: ["share-links", dashboardId] }),
  });
}

// ---- the audit log -------------------------------------------------------------------

export interface AuditEntry {
  id: number;
  ts: string;
  actorType: string;
  actorName: string | null;
  actorEmail: string | null;
  action: string;
  targetType: string | null;
  targetId: string | null;
  detail: Record<string, unknown>;
  ip: string | null;
}

export function useAudit(filters: { action: string; actor: string; before: number | null }) {
  const params = new URLSearchParams();
  if (filters.action) params.set("action", filters.action);
  if (filters.actor) params.set("actor", filters.actor);
  if (filters.before !== null) params.set("before", String(filters.before));
  params.set("limit", "100");
  return useQuery({
    queryKey: ["audit", filters],
    queryFn: () => apiGet<{ entries: AuditEntry[]; next: number | null }>(`/api/audit?${params.toString()}`),
  });
}
