export type AgentRole = {
  name: string;
  mission: string;
  systemPersona: string;
  preferredModel?: string;
};

export type Roster = {
  agents: AgentRole[];
  castedAt: number;
  reason: string;
};

export type ProgressKind =
  | "cast"
  | "dispatch"
  | "checkpoint"
  | "revert"
  | "branch"
  | "merge"
  | "consensus"
  | "ralph"
  | "autoresearch"
  | "done"
  | "note";

export type ProgressEntry = {
  ts: number;
  kind: ProgressKind;
  message: string;
};

export type ClkState = {
  idea?: string;
  roster?: Roster;
  progress: ProgressEntry[];
  doneReason?: string;
  startedAt?: number;
  homeBranch?: string;
};
