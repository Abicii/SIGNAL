export type PlannedSession = {
  logical_session_id: string;
  activity_type: string;
  schedule_window: { start_at: string; end_at: string; timezone: string };
  intensity: string;
  duration_minutes: number;
};

export type PlanDay = {
  date: string;
  weekday: string;
  label: string;
  session: PlannedSession | null;
  completed: boolean;
};

export type TodayResponse = {
  date: string;
  weekday: string;
  session: { label: string; session: PlannedSession } | null;
  recovery: { sleep_hours: number; sleep_label: string; summary: string };
  signal_message: string;
  weekly_progress: { completed: number; total: number };
};

export type PlanResponse = {
  timezone: string;
  week_start: string;
  week_end: string;
  days: PlanDay[];
  proposal_pending: boolean;
};

export type ReplanResult = {
  decision_id: string | null;
  status: string;
  original_plan: PlannedSession[];
  proposed_plan: PlannedSession[] | null;
  selected_candidate: { candidate_id: string } | null;
  candidate_scores: { candidate_id: string; total: number; goal_alignment: number }[];
  conflicts: { code: string; severity: string; affected_logical_session_ids: string[] }[];
  changes: { logical_session_id: string; change_type: string; before: PlannedSession | null; after: PlannedSession | null }[];
  reason_codes: string[];
  validation: { valid: boolean; violations: string[] };
};

export type ReplanResponse = {
  result: ReplanResult;
  explanation: string;
  signal_moment: { type: string; message: string };
  trigger: { kind: string; starts_at: string; ends_at: string; impact: string };
};
