// Shapes mirrored from the Python REST surface (clk_harness/api.py +
// webui_router.py). Kept intentionally permissive where the backend is.

export interface Workspace {
  id: string;
  name: string;
  path?: string;
  created_at?: string;
}

export interface WorkflowInfo {
  name: string;
  path: string;
  description: string;
}

export interface EnvVar {
  key: string;
  group: string;
  label: string;
  type: "bool" | "int" | "float" | "enum" | "secret" | "string";
  choices: string[];
  default: string;
  help: string;
  is_secret: boolean;
  masked: boolean;
  set: boolean;
  value: string;
}

export interface EnvResponse {
  ok: boolean;
  path: string;
  groups: string[];
  vars: EnvVar[];
}

export interface ProvidersConfig {
  ok: boolean;
  active: string | null;
  providers: Record<string, Record<string, unknown>>;
  available: Record<string, boolean>;
}

export interface DoctorFinding {
  level: "ok" | "warn" | "fail";
  name: string;
  message: string;
}

export interface DoctorResponse {
  ok: boolean;
  active_provider: string;
  auth_mode: string;
  findings: DoctorFinding[];
}

export type EventSeverity = "info" | "muted" | "warn" | "error" | "success";

export interface ActivityEvent {
  seq: number;
  ts: string;
  kind: string;
  agent: string;
  run_id: string;
  severity: EventSeverity;
  category: string;
  summary: string;
  payload: Record<string, any>;
}

export interface AgentCard {
  name: string;
  status: "idle" | "working" | "recovering" | "done" | "failed" | "provider";
  role: string;
  provider: string;
  runs: number;
  tokens_in: number;
  tokens_out: number;
  tokens_total: number;
  last_run_tokens: number;
  usd: number;
  files: string[];
  last_thought: string;
  last_prompt_path: string;
  last_response_path: string;
  last_error: string;
  error_kind: string;
}

export interface SnapshotTotals {
  total_tokens: number;
  total_usd: number;
  peak_run_tokens: number;
  total_files: number;
  commits: number;
  cost_per_provider: Record<string, number>;
}

export interface Snapshot {
  idea: string;
  provider: string;
  phase: string;
  busy: boolean;
  agents: Record<string, AgentCard>;
  totals: SnapshotTotals;
  files_changed: string[];
  event_count: number;
}

export interface TaskRef {
  ok: boolean;
  task_id: string;
  workspace_id: string;
}

export interface TaskStatus {
  ok: boolean;
  task_id: string;
  workspace_id: string;
  command: string;
  status: "pending" | "running" | "done" | "failed" | "cancelled";
  exit_code: number | null;
  started_at: string | null;
  finished_at: string | null;
  line_count: number;
}
