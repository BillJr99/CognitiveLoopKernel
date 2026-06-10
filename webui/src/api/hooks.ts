// React Query hooks for every endpoint the UI touches.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from "./client";
import type {
  DoctorResponse,
  EnvResponse,
  FileContent,
  FilesResponse,
  GitCommitDetail,
  GitFileAt,
  GitLogResponse,
  ProvidersConfig,
  Snapshot,
  StopWhenResponse,
  TaskRef,
  TaskStatus,
  Workspace,
  WorkflowInfo,
} from "./types";

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: () => apiGet<{ ok: boolean; version: string; uptime_s: number }>("/api/healthz"),
    refetchInterval: 30_000,
  });
}

export function useWorkspaces() {
  return useQuery({
    queryKey: ["workspaces"],
    queryFn: () => apiGet<{ ok: boolean; workspaces: Workspace[] }>("/api/workspaces"),
    refetchInterval: 10_000,
  });
}

export function useCreateWorkspace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) =>
      apiPost<{ ok: boolean; workspace_id: string }>("/api/workspaces", { name }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workspaces"] }),
  });
}

export function useDeleteWorkspace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiDelete<{ ok: boolean }>(`/api/workspaces/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workspaces"] }),
  });
}

export function useRenameWorkspace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) =>
      apiPatch<{ ok: boolean; workspace: Workspace }>(`/api/workspaces/${id}`, { name }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workspaces"] }),
  });
}

export function useWorkflows() {
  return useQuery({
    queryKey: ["workflows"],
    queryFn: () => apiGet<{ ok: boolean; workflows: WorkflowInfo[] }>("/api/workflows"),
  });
}

export function useEnv() {
  return useQuery({
    queryKey: ["env"],
    queryFn: () => apiGet<EnvResponse>("/api/env"),
  });
}

export function useSaveEnv() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (values: Record<string, string | null>) =>
      apiPut<EnvResponse>("/api/env", { values }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["env"] }),
  });
}

export function useClkConfig(ws: string | null) {
  return useQuery({
    enabled: !!ws,
    queryKey: ["clkConfig", ws],
    queryFn: () => apiGet<{ ok: boolean; config: Record<string, any> }>(`/api/workspaces/${ws}/config/clk`),
  });
}

export function useSaveClkConfig(ws: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (config: Record<string, any>) =>
      apiPut(`/api/workspaces/${ws}/config/clk`, { config }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["clkConfig", ws] }),
  });
}

export function useProviders(ws: string | null) {
  return useQuery({
    enabled: !!ws,
    queryKey: ["providers", ws],
    queryFn: () => apiGet<ProvidersConfig>(`/api/workspaces/${ws}/config/providers`),
  });
}

export function useSaveProviders(ws: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { providers: Record<string, any>; active?: string }) =>
      apiPut<ProvidersConfig>(`/api/workspaces/${ws}/config/providers`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["providers", ws] }),
  });
}

export function useAgents(ws: string | null) {
  return useQuery({
    enabled: !!ws,
    queryKey: ["agents", ws],
    queryFn: () => apiGet<{ ok: boolean; agents: Record<string, any> }>(`/api/workspaces/${ws}/config/agents`),
  });
}

export function useSaveAgents(ws: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (agents: Record<string, any>) =>
      apiPut(`/api/workspaces/${ws}/config/agents`, { agents }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agents", ws] }),
  });
}

export function useDoctor(ws: string | null) {
  return useQuery({
    enabled: !!ws,
    queryKey: ["doctor", ws],
    queryFn: () => apiGet<DoctorResponse>(`/api/workspaces/${ws}/doctor`),
    refetchInterval: 15_000,
  });
}

export function useSnapshot(ws: string | null) {
  return useQuery({
    enabled: !!ws,
    queryKey: ["snapshot", ws],
    queryFn: () => apiGet<{ ok: boolean; snapshot: Snapshot }>(`/api/workspaces/${ws}/snapshot`),
    refetchInterval: 5_000,
  });
}

export function useStartTask() {
  return useMutation({
    mutationFn: (body: { command: string; args?: string[]; workspace_id: string; workflow?: string; stop_when?: string }) =>
      apiPost<TaskRef>("/api/research", body),
  });
}

export function useStopWhen(ws: string | null) {
  return useQuery({
    enabled: !!ws,
    queryKey: ["stopWhen", ws],
    queryFn: () => apiGet<StopWhenResponse>(`/api/workspaces/${ws}/stop-when`),
    staleTime: 5_000,
  });
}

export function useSetStopWhen(ws: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (condition: string | null) =>
      apiPut<StopWhenResponse>(`/api/workspaces/${ws}/stop-when`, { condition }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["stopWhen", ws] }),
  });
}

export function useCancelTask() {
  return useMutation({
    mutationFn: (taskId: string) => apiPost<{ ok: boolean }>(`/api/research/${taskId}/cancel`),
  });
}

export function useWorkspaceFiles(ws: string | null) {
  return useQuery({
    enabled: !!ws,
    queryKey: ["files", ws],
    queryFn: () => apiGet<FilesResponse>(`/api/workspaces/${ws}/files`),
    refetchInterval: 2_000,
    refetchOnMount: "always",
    staleTime: 0,
  });
}

export function useFileContent(ws: string | null, path: string | null) {
  return useQuery({
    enabled: !!ws && !!path,
    queryKey: ["file", ws, path],
    queryFn: () => apiGet<FileContent>(`/api/workspaces/${ws}/file?path=${encodeURIComponent(path!)}`),
  });
}

export function useGitLog(ws: string | null, path?: string | null) {
  return useQuery({
    enabled: !!ws,
    queryKey: ["git-log", ws, path ?? ""],
    queryFn: () =>
      apiGet<GitLogResponse>(
        `/api/workspaces/${ws}/git/log${path ? `?path=${encodeURIComponent(path)}` : ""}`,
      ),
    refetchInterval: 10_000,
  });
}

export function useCommitDetail(ws: string | null, sha: string | null) {
  return useQuery({
    enabled: !!ws && !!sha,
    queryKey: ["git-commit", ws, sha],
    queryFn: () => apiGet<GitCommitDetail>(`/api/workspaces/${ws}/git/commit/${sha}`),
    staleTime: Infinity, // commits are immutable
  });
}

export function useFileAtCommit(ws: string | null, sha: string | null, path: string | null) {
  return useQuery({
    enabled: !!ws && !!sha && !!path,
    queryKey: ["git-file", ws, sha, path],
    queryFn: () =>
      apiGet<GitFileAt>(
        `/api/workspaces/${ws}/git/file?sha=${sha}&path=${encodeURIComponent(path!)}`,
      ),
    staleTime: Infinity,
  });
}

export function useSaveFile(ws: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { path: string; content: string }) =>
      apiPut<{ ok: boolean; path: string; size: number }>(`/api/workspaces/${ws}/file`, body),
    onSuccess: (_d, vars) => {
      qc.invalidateQueries({ queryKey: ["files", ws] });
      qc.invalidateQueries({ queryKey: ["file", ws, vars.path] });
    },
  });
}

export function useSaveIdea(ws: string | null) {
  return useMutation({
    mutationFn: (body: { statement: string; title?: string; tags?: string[] }) =>
      apiPut<{ ok: boolean; title: string }>(`/api/workspaces/${ws}/idea`, body),
  });
}

export function useDiscoverProviders(enabled = true) {
  return useQuery({
    enabled,
    queryKey: ["discover"],
    queryFn: () => apiGet<import("./types").DiscoverResponse>("/api/providers/discover"),
    staleTime: 10_000,
  });
}

export function useProbeModels() {
  return useMutation({
    mutationFn: (body: { type: string; endpoint?: string; api_key?: string }) =>
      apiPost<import("./types").ProbeResponse>("/api/providers/probe", body),
  });
}

export function useTaskStatus(taskId: string | null) {
  return useQuery({
    enabled: !!taskId,
    queryKey: ["task", taskId],
    queryFn: () => apiGet<TaskStatus>(`/api/research/${taskId}`),
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "done" || s === "failed" || s === "cancelled" ? false : 1500;
    },
  });
}

export function useHarnessLogs(wsId: string | null, enabled = true) {
  return useQuery({
    enabled: enabled && !!wsId,
    queryKey: ["harness-logs", wsId],
    queryFn: () => apiGet<import("./types").HarnessLogResponse>(`/api/workspaces/${wsId}/logs?tail=600`),
    refetchInterval: 3000,
  });
}
