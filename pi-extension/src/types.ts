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

/**
 * Watchdog counters — the supervise loop's memory between chief turns.
 * Mirrors the no-progress / rescue ladder in the Python harness's
 * WorkflowRunner.run().
 */
export type SuperviseState = {
  /** Consecutive turns without commits or progress entries. */
  noProgress: number;
  /** Total auto-continuations this run (capped). */
  continuations: number;
  /** The one-shot stall-rescue prompt has fired. */
  rescueAttempted: boolean;
  /** Baseline for material-progress detection. */
  lastHead?: string | null;
  lastProgressCount?: number;
};

/** Outcome of one Ralph iteration — plateau detection input. */
export type RalphOutcome = {
  branch: string;
  outcome: "merged" | "reverted";
  ts: number;
};

export type ClkState = {
  idea?: string;
  roster?: Roster;
  progress: ProgressEntry[];
  doneReason?: string;
  startedAt?: number;
  homeBranch?: string;
  supervise?: SuperviseState;
  ralphOutcomes?: RalphOutcome[];
};
