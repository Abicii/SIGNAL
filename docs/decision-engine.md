# SIGNAL Deterministic Decision Engine

Status: design specification; no application code is defined here. This document depends on the frozen logical schema in [`data-model.md`](data-model.md) and the product principles in [`SIGNAL_SPEC.md`](../SIGNAL_SPEC.md). Domain contracts below are typed service values, not additional tables.

## 1. Decision-engine contract

The product loop is **Plan → Life happens → Replan → Learn**. The engine owns fact normalization, state calculation, constraint/conflict derivation, candidate generation, scoring, and validation. An optional LLM can only choose among valid deterministic candidates and may offer constrained rationale codes. It cannot create or edit a plan.

### Facts, derived state, constraints, conflicts, decisions

| Layer | Meaning | Examples | Authority |
|---|---|---|---|
| Facts | User-entered or observed source records, with provenance and time | Sleep-duration observation; completed workout; immutable planned workout; LifeEvent kind and interval; active Goal; explicit Preference | Stored source entities; never rewritten by an LLM |
| Derived state | Deterministic time-bounded interpretation of facts | Sleep deviation from baseline; training-load trend; recovery availability estimate; schedule availability; current plan resolved through `Profile.current_plan_version_id`; RecentHistory | Versioned deterministic transformations; preserve source references and freshness |
| Constraints / Context | Feasibility rules or relevant contextual conditions derived from facts/state | Event interval unavailable; required session ordering; recovery requirement; goal deadline; explicit hard Preference | Constructed by deterministic rules; hard constraints are inviolable |
| Conflicts | A detected incompatibility between a plan/session and constraints, goals, or other demands | Run overlaps unavailable time; lower-body session conflicts with recovery constraint; two goals compete for limited capacity | Deterministically detected and tied to source/constraint codes |
| Decisions | A choice among feasible schedules and authorized changes | Move, reduce, remove, preserve, insert recovery, reorder | Deterministic optimizer chooses highest score; constrained LLM selection is optional; validator owns acceptance |

An event is a fact about what happened or what the user says will happen. Consequences are not event facts. For example, a late social event is a `LifeEvent`; “Saturday morning recovery availability is reduced” is derived `Context` if policy/evidence supports that interpretation. It is not written back as `LifeEvent.impact` (the frozen schema has no such field). Poor sleep is a fact only when separately observed/reported as a `PhysiologyObservation`.

Core boundary: **Facts → deterministic code; Decisions → deterministic optimization + constrained reasoning; Language → LLM/templates.** LLM output is never the source of facts, constraints, session content, or validation.

### Core Data Invariants

- Every user-owned record and every planner read/write is scoped to the authenticated user.
- Historical `PlanVersion` schedule content and its `PlannedSession` rows are immutable; corrections create a new version.
- `Profile.current_plan_version_id` is the sole current-plan pointer, with at most one current plan per user.
- A proposed plan cannot become current unless deterministic validation succeeds and the user explicitly accepts it.
- LLM output cannot introduce facts, sessions, constraints, or candidate IDs absent from the deterministic input/candidate set.
- `AIContext` contains only fields explicitly allowed by its versioned serializer schema.
- `LearnedPattern` cannot outrank a current instruction or explicit stable `Preference`.
- Historical decisions remain reproducible through versioned engine, policy, validator, input/context schema, and provenance metadata.
- Raw free-text user content never enters an LLM prompt without explicit sanitization and structuring; default behavior is exclusion.

#### MVP hard and soft constraints

Every `Constraint.kind` is explicitly `hard` or `soft`. Hard constraints include exact event-unavailable intervals, a session outside the plan horizon, impossible session overlap/capacity, unsupported session data, configured safety rules, explicit hard Preferences, and current instructions explicitly phrased as requirements when feasible. Any hard violation makes the candidate invalid and it is excluded before scoring. Soft constraints include recovery buffers, soft Preferences, goal-target tradeoffs, preferred schedule windows, and continuity heuristics; a miss incurs a registered, bounded score penalty. A soft conflict never becomes a hard failure simply because its severity is high. The run records the rule code and provenance for both kinds.

#### Deterministic recovery heuristic (MVP policy v1)

This is a scheduling heuristic, not a medical assessment and not ML. Eligible inputs are (a) latest `sleep_duration` observation in hours, no older than 48 hours, with a personal median baseline from the preceding 28 days and at least 7 valid observations; (b) recent training load from `TrainingSession.load_value` normalized to `session_rpe_load`, latest 7-day sum compared with the median weekly total over up to 4 preceding complete weeks (at least 3 weeks required); and (c) latest self-reported `soreness` on a 0–10 scale, no older than 48 hours. Out-of-range, wrong-unit, synthetic-in-production, or stale values are excluded and flagged.

Normalize available inputs to `[0,1]`: `sleep_deficit = clip((baseline_sleep - latest_sleep) / max(baseline_sleep, 0.1) / 0.5, 0, 1)`; `load_excess = clip((recent_7d_load - baseline_weekly_load) / max(baseline_weekly_load, 1), 0, 1)`; `soreness_norm = clip(soreness / 10, 0, 1)`. Compute `recovery_stress` as the weighted mean of available terms with weights 0.50, 0.30, 0.20 respectively, renormalized over available terms; `recovery_fit = 1 - recovery_stress`. If none are available, `recovery_fit=0.5` neutral and confidence is `unknown`; never interpret missing as good recovery. Report which inputs and freshness were used in deterministic provenance. There is no learned coefficient or model training.

Separately, a late LifeEvent can create a deterministic **soft schedule buffer**, not a claim of poor sleep: if an event ends between 04:00 and 09:00 local time, make the following interval from event end to `min(end + 6 hours, local noon)` a `REDUCED_RECOVERY_WINDOW` soft constraint. For MVP policy v1, a high-load session placed in that interval incurs 25 points once per affected day; recovery fit above remains about measured/derived physiology so the same rule is not double-counted. The buffer rule is versioned, transparent, and user-overridable when no hard constraint applies.

## 2. Domain contracts

All UUIDs below are internal opaque references. They may be used by deterministic code and audit records but are excluded from `AIContext` unless converted to request-local pseudonyms explicitly allowed by the serializer. Scheduled session windows and life-event windows use explicit timezone-aware start/end datetimes plus an IANA timezone; store instants in UTC and derive local dates through that zone. There is no recurrence engine in the MVP: each planned occurrence is materialized as a dated session. `Decimal` values are used for scores. These are Pydantic-style conceptual schemas, not runnable Python or persistence models. Every field table specifies its type, presence, domain, meaning, producer, consumer, and AI eligibility.

Shared value types are themselves closed/validated structures: `DateRange(start: date, end: date)` with `start <= end`; `TimeWindow(start: aware datetime, end: aware datetime, timezone: valid IANA str)` with `start < end` and both instants resolved against the named zone; `ScheduleWindow(start_at: aware datetime, end_at: aware datetime, timezone: valid IANA str)` with `end_at > start_at`; `PlannedSlot(logical_session_id: UUID, activity_type: SupportedActivity, schedule_window: ScheduleWindow, duration_minutes: int, intensity: SupportedIntensity, attributes: typed mapping)`; `SourceRef(source_type: registered str, source_id: UUID)`; `GoalSummary(kind: STRENGTH|RUNNING|ENDURANCE|RECOVERY|GENERAL_FITNESS, target: typed value/unit, target_date: date | None, priority: int[1..5], role: primary|secondary|unspecified)`; `PreferenceSummary(key: registered str, value: key-specific typed value, strength: hard|soft)`; `MetricPoint(metric: registered str, value: Decimal, unit: canonical str, observed_at: datetime, quality: self_reported|measured|synthetic)`; `ValidationViolation(code: registered str, severity: low|medium|high|blocking, affected_slot: str | None, expected_summary: str, actual_summary: str)`; `ReasonCode`, `ConstraintCode`, and `ConflictCode` are finite versioned registries. `SupportedActivity`, intensity values, and structured attributes are a closed MVP catalog defined by policy; unknown values fail validation.

#### MVP goal catalog and priority interpretation

`Goal.kind` uses exactly `STRENGTH`, `RUNNING`, `ENDURANCE`, `RECOVERY`, or `GENERAL_FITNESS` for MVP. Goals use existing `Goal` fields; no separate goal table or role field is added. `priority` is an integer 1–5; the maximum-priority active goal(s) are summarized as `primary`, lower-priority goals as `secondary`; no active goals yields `unspecified`. MVP `target_unit` values are `sessions/week` for STRENGTH; `km/week` or `sessions/week` for RUNNING; `minutes/week` or `sessions/week` for ENDURANCE; `days/week` for RECOVERY; and `sessions/week` for GENERAL_FITNESS. When a supported target is absent, use the corresponding parent-plan dose within the horizon as reference. Doses are deterministic: strength session-equivalents; running distance when canonical distance is present, else run session-equivalents; endurance minutes when duration is present, else endurance session-equivalents; recovery days/windows; general fitness active-session equivalents with modality coverage. An unsupported unit is a validation/data-quality error, not an invitation to guess. An absent target and zero parent dose is neutral (`1.0`) and flagged; the engine must not invent a target.

### CurrentState

Point-in-time normalized decision input. It is built for one authenticated owner and planning horizon; it is not the generic Profile/User object. Persist only the minimized audit projection in `DecisionRun.input_snapshot`.

| Field | Type; required?; allowed values | Meaning | Producer → consumer | AIContext? |
|---|---|---|---|---|
| `as_of` | `datetime`; required | Snapshot reference time | Context loader → all later stages | No raw timestamp; only relative windows when needed |
| `user_id` | `UUID`; required | Internal owner scope | Authenticated request → repository/planner | Never |
| `timezone` | `str`; required, valid IANA zone | Local date boundaries | Profile normalization → availability/planner | Yes, only when needed for horizon interpretation |
| `horizon` | `DateRange`; required, `start <= end` | Planning window | Request policy → all planner stages | Yes |
| `current_plan_version_id` | `UUID | None`; optional before first plan | Current pointer from Profile | Repository → candidate generation | Never; use request-local candidate references |
| `current_plan` | `tuple[PlannedSlot,...]`; required, empty only for no current plan | Read-only projection of current immutable version | Repository → conflict detection/candidate generation | Only minimized candidate/plan projection |
| `goals` | `tuple[GoalSummary,...]`; required | Active goals with priority and target window | Goal repository → scoring | Relevant minimized summaries only |
| `preferences` | `tuple[PreferenceSummary,...]`; required | Effective explicit stable preferences | Preference repository → constraints/scoring | Relevant normalized values only |
| `physiology` | `PhysiologySummary | None`; optional | Latest relevant measurements, baseline, deviation, freshness | Physiology derivation → constraints/scoring | Derived bands only when task-relevant |
| `training_load` | `LoadSummary | None`; optional | Recent load/trend and freshness | Training history derivation → recovery/scoring | Minimized band/count only when relevant |
| `availability` | `tuple[AvailabilityWindow,...]`; required, may be empty | Known unavailable/free/preferred windows derived from events/preferences and schedule rules | Normalizer → constraints/conflicts/generator | Relative windows only, when relevant |
| `recovery_context` | `RecoverySummary | None`; optional | Derived recovery availability/risk with evidence and confidence; not a new fact | State derivation from physiology/history/event context → constraints/scoring | Derived band/confidence only when relevant |
| `life_events` | `tuple[NormalizedLifeEvent,...]`; required, may be empty | Active/future event facts normalized from LifeEvent | Event normalizer → constraint/context derivation | Minimal event category/time window only |
| `learned_patterns` | `tuple[PatternSummary,...]`; required, may be empty | Only active, non-stale patterns that meet policy threshold | Pattern evaluator → soft scoring | Only relevant evidence-backed statement/confidence band |
| `data_quality` | `tuple[FreshnessFlag,...]`; required, may be empty | Missing/stale/low-confidence input flags | Load/normalize stage → scoring/fallback/explanation | Safe flags only if relevant |
| `source_versions` | `Mapping[str,str]`; required | Source/derivation schema versions used for reproducibility | Loader → DecisionRun provenance | No |

### RecentHistory

Bounded evidence window, not a second permanent history store.

| Field | Type; required?; allowed values | Meaning | Producer → consumer | AIContext? |
|---|---|---|---|---|
| `window_start` | `datetime`; required | Inclusive history bound | Policy → history loader | No |
| `window_end` | `datetime`; required | Exclusive/as-of bound | Policy → history loader | No |
| `training_outcomes` | `tuple[TrainingOutcomeSummary,...]`; required | Completed/modified/missed counts and normalized outcomes | TrainingSession repository → state/pattern derivation | Aggregate only, if relevant |
| `physiology_series` | `tuple[MetricPoint,...]`; required | Relevant metric values with canonical units/provenance | PhysiologyObservation repository → baseline/deviation calculation | Raw points no; derived summary only |
| `plan_outcomes` | `tuple[PlanOutcomeSummary,...]`; required | Accepted/rejected/edited plan outcomes and trace refs | DecisionRun/Feedback/PlanVersion → pattern evidence | Summary only if specifically needed |
| `event_history` | `tuple[EventSummary,...]`; required | Normalized event classes and windows, without raw labels | LifeEvent repository → context derivation | Category/window summary only |
| `completeness` | `float`; required, `[0,1]` | Coverage of expected observations in the window | History loader → confidence/fallback | Band only if relevant |
| `source_refs` | `tuple[SourceRef,...]`; required | Internal typed provenance links | Loader → audit/pattern evidence | Never |

### Constraint

| Field | Type; required?; allowed values | Meaning | Producer → consumer | AIContext? |
|---|---|---|---|---|
| `constraint_id` | `UUID`; required | Internal identity | Deriver → audit/diagnostics | Never |
| `code` | `ConstraintCode`; required, registered code | Stable rule such as `EVENT_UNAVAILABLE` | Rule registry → conflicts/candidates | Yes, code only if allowlisted |
| `kind` | `str`; required, `hard` or `soft` | Whether violation invalidates a candidate | Rule policy → generator/validator/scorer | Yes |
| `scope` | `Scope`; required, `instant`, `interval`, `date`, `session`, `week` | Temporal/session extent | Deriver → generator/validator | Minimal form only |
| `window` | `TimeWindow | None`; optional | UTC/local bounded interval | Event/state normalization → overlap logic | Minimized relative window only |
| `target_logical_session_id` | `UUID | None`; optional | Intended session affected | Planner/goal constraints → generator/validator | Never; pseudonym only if required |
| `parameters` | `TypedConstraintParams`; required, code-specific schema | Canonical numeric/category operands | Deterministic rule → validator/scorer | Only explicit per-code projection |
| `source_type` | `str`; required, registered source types | `life_event`, `goal`, `preference`, `physiology`, `policy`, etc. | Deriver → trace/explanation | Safe source category only |
| `source_ref` | `UUID | None`; optional | Internal source record reference | Deriver → audit/diff | Never |
| `reason_code` | `ReasonCode`; required | Explanation vocabulary linked to rule | Rule registry → score/explanation | Yes |

### Conflict

| Field | Type; required?; allowed values | Meaning | Producer → consumer | AIContext? |
|---|---|---|---|---|
| `conflict_id` | `UUID`; required | Run-local internal identity | Detector → audit | Never |
| `code` | `ConflictCode`; required, registered code | Stable conflict class | Detector → generator/explanation | Yes, allowlisted |
| `severity` | `str`; required, `low`, `medium`, `high`, `blocking` | Impact before resolution | Detector → generation/prioritization | Yes |
| `affected_logical_session_ids` | `tuple[UUID,...]`; required, may be empty | Intended sessions affected | Detector → edit operators | Never; candidate pseudonyms only |
| `constraint_ids` | `tuple[UUID,...]`; required | Constraints causing conflict | Detector → trace/validator | Never |
| `date_window` | `TimeWindow | None`; optional | Affected period | Detector → generation/explanation | Minimized relative window only |
| `goal_ids` | `tuple[UUID,...]`; required | Goals in competition | Detector → score/explanation | Never; normalized goal summaries instead |
| `reason_codes` | `tuple[ReasonCode,...]`; required | Deterministic supporting reasons | Detector → CandidatePlanScore/PlanChange | Yes, allowlisted |
| `evidence_refs` | `tuple[SourceRef,...]`; required | Provenance to facts/derived context | Detector → DecisionRun | Never |

### CandidatePlan

Complete candidate over the requested horizon. Candidate IDs are request-local random/pseudonymous values, not database IDs. Candidate content is frozen after generation.

| Field | Type; required?; allowed values | Meaning | Producer → consumer | AIContext? |
|---|---|---|---|---|
| `candidate_id` | `str`; required, unique within run, opaque | Membership key | Generator → scoring/adjudication/validator | Yes |
| `parent_plan_version_id` | `UUID | None`; optional | Source version | Generator → save/provenance | Never |
| `horizon` | `DateRange`; required | Covered plan dates | Generator → validator/build | Yes |
| `sessions` | `tuple[CandidateSession,...]`; required, immutable | Candidate placements and supported activities | Generator → score/LLM/validator/build | Yes, minimized candidate projection |
| `operations` | `tuple[CandidateOperation,...]`; required | Bounded edit sequence from source plan | Generator → scoring/explanation/diff | Safe operation names only |
| `satisfied_hard_constraint_ids` | `tuple[UUID,...]`; required | Internal audit proof | Generator/constraint checker → validator | Never |
| `violated_hard_constraint_ids` | `tuple[UUID,...]`; required, must be empty for eligible candidate | Eligibility proof | Generator/checker → validator | Never |
| `reason_codes` | `tuple[ReasonCode,...]`; required | Reasons supported by constraints/goals | Generator → scoring/explanation | Yes, allowlisted |
| `content_hash` | `str`; required | Hash of canonical candidate content | Generator → tamper check/cache | No |

`CandidateSession` includes `logical_session_id`, `activity_type`, required typed `schedule_window` (`start_at`, `end_at`, `timezone`), derived `local_date`, `duration_minutes`, `intensity`, and structured attributes. Every persisted `PlannedSession`, including rest/recovery entries, has this required value object. In the frozen persistence model its components are stored in typed columns (`schedule_start_at`, `schedule_end_at`, `schedule_timezone`), separate from extensible `attributes`. Reject missing/naive timestamps, require `end_at > start_at`, and validate the IANA zone. Keep `local_date` equal to the start instant's local date. Overlap/conflict detection compares normalized absolute instants represented by start/end, not local-date or wall-clock strings. A dated PlanVersion materializes every occurrence; no recurrence rule is stored or expanded. The stable `logical_session_id` is carried into its version-specific `PlannedSession`; multiple versions never share a mutable session row.

### CandidatePlanScore

| Field | Type; required?; allowed values | Meaning | Producer → consumer | AIContext? |
|---|---|---|---|---|
| `candidate_id` | `str`; required | Candidate membership key | Scorer → adjudicator/fallback | Yes |
| `policy_version` | `str`; required | Scoring weights/definitions | Scorer → DecisionRun | No |
| `valid` | `bool`; required | Hard constraints and structural checks pass | Feasibility checker → scorer/adjudicator | Yes (valid candidates only) |
| `goal_alignment` | `Decimal`; required, `[0,1]` | Weighted retention/progress toward active goals | Scorer → ranker | Yes |
| `preference_adherence` | `Decimal`; required, `[0,1]` | Fit to explicit effective preferences and thresholded patterns | Scorer → ranker | Yes |
| `schedule_fit` | `Decimal`; required, `[0,1]` | Feasible placements relative to availability and preferred windows | Scorer → ranker | Yes |
| `recovery_fit` | `Decimal`; required, `[0,1]` | Fit to observed/derived recovery context and rest constraints | Scorer → ranker | Yes |
| `training_continuity` | `Decimal`; required, `[0,1]` | Continuity of reasonable session frequency/order/volume | Scorer → ranker | Yes |
| `disruption_cost` | `Decimal`; required, `[0,100]` points | Cost of changed dates, removed sessions, changed intensity/duration | Scorer → ranker | Yes |
| `soft_constraint_penalty` | `Decimal`; required, `[0,100]` points | Sum of penalties for soft constraint misses | Scorer → ranker | Yes |
| `total` | `Decimal`; required, `[-100,100]` | Deterministic scalar score | Scorer → ranker/fallback | Yes |
| `reason_codes` | `tuple[ReasonCode,...]`; required | Score explanation trace | Scorer → PlanChange/explanation | Yes, allowlisted |

### AIContext

This is a closed request DTO created only by the allowlist serializer. It is not an application entity and is never persisted wholesale.

| Field | Type; required?; allowed values | Meaning | Producer → consumer | AIContext? |
|---|---|---|---|---|
| `schema_version` | `str`; required, registered version | Serializer contract | Serializer → provider adapter | Yes |
| `task` | `str`; required, allowlisted task name | Narrow instruction (e.g. `select_valid_candidate`) | Replan service → model | Yes |
| `horizon` | `DateRange + timezone`; required | Local plan window | Serializer → adjudicator | Yes |
| `goal_summaries` | `tuple[GoalSummary,...]`; required, bounded | Relevant active goals | Allowlist projection → adjudicator | Yes |
| `preference_summaries` | `tuple[PreferenceSummary,...]`; required, bounded | Relevant explicit preferences and eligible learned evidence summaries | Allowlist projection → adjudicator | Yes |
| `context_summaries` | `tuple[SafeContextSummary,...]`; required, bounded | Relevant derived event/context categories and relative windows | Derivation/projection → adjudicator | Yes |
| `constraints` | `tuple[SafeConstraintSummary,...]`; required, bounded | Relevant hard/soft rules without source IDs | Projection → adjudicator | Yes |
| `conflicts` | `tuple[SafeConflictSummary,...]`; required, bounded | Detected tradeoffs without internal references | Projection → adjudicator | Yes |
| `valid_candidates` | `tuple[SafeCandidateSummary,...]`; required, non-empty | Complete choice set and deterministic score/tradeoff summaries | Candidate projector → adjudicator | Yes |
| `allowed_rationale_codes` | `tuple[ReasonCode,...]`; required | Closed output vocabulary | Policy → output validator | Yes |

All nested safe-summary types have field-level allowlists, size limits, and schema validation. Excluded fields include identity, opaque database IDs, raw LifeEvent labels, free text, credentials, raw measurements, unrestricted attributes, and audit snapshots. Candidate IDs are random request-local references mapped server-side to generated candidates.

### PlannerDecision

| Field | Type; required?; allowed values | Meaning | Producer → consumer | AIContext? |
|---|---|---|---|---|
| `selected_candidate_id` | `str`; required, must be a candidate in this run | Selected valid option | LLM adapter or deterministic fallback → validator | Output only; never part of input context |
| `rationale_codes` | `tuple[ReasonCode,...]`; required, may be empty, registered subset | Safe reasons for selection | LLM output validated against trace → explanation | Output; not a fact |

LLM output has no fields for facts, session edits, constraints, scores, confidence, or arbitrary prose. A rationale code is retained only if supported by the conflict/score trace. Optional explanation prose is not part of the MVP contract; user language comes from deterministic templates over validated reason codes.

### ValidationResult

| Field | Type; required?; allowed values | Meaning | Producer → consumer | AIContext? |
|---|---|---|---|---|
| `valid` | `bool`; required | Candidate passes all validation checks | Deterministic validator → persistence/acceptance | Never |
| `violations` | `tuple[ValidationViolation,...]`; required, empty when valid | Codes, severity, affected slot, safe expected/actual summary | Validator → fallback/observability/explanation | Never |
| `validator_version` | `str`; required | Reproduction key | Validator → DecisionRun | Never |
| `candidate_content_hash` | `str`; required | Ensures validated content equals generated content | Validator → build/save gate | Never |
| `validated_at` | `datetime`; required | Audit time | Validator → DecisionRun | Never |

## 3. Deterministic planning algorithm

The required stage ordering is:

`LOAD_CONTEXT → NORMALIZE_STATE → DETECT_CONSTRAINTS → DETECT_CONFLICTS → GENERATE_CANDIDATES → SCORE_CANDIDATES → OPTIONAL_LLM_ADJUDICATION → VALIDATE_DECISION → BUILD_PLAN → BUILD_EXPLANATION → SELECT_SIGNAL_MOMENT → SAVE_DECISION`

Each stage is a pure transformation where practical. Persistence occurs only at defined transaction boundaries. The whole run records engine, policy, validator, input schema, context schema, and prompt versions in `DecisionRun`, per the frozen data model.

| Stage | Input → output | Deterministic logic | Failure conditions | Invariants |
|---|---|---|---|---|
| `LOAD_CONTEXT` | Authenticated owner + request → CurrentState source set, RecentHistory | Query current plan through `Profile.current_plan_version_id`; fetch active goals/preferences, relevant observation/session windows, active future LifeEvents, and eligible LearnedPatterns; enforce `user_id` on every relation; bound history window | Unauthorized/cross-user row; missing/corrupt current pointer; repository unavailable | Identity never enters later model context; historical plans are read-only; no implicit broad database dump |
| `NORMALIZE_STATE` | Source records → canonical CurrentState/RecentHistory | Convert units; normalize local times with IANA timezone; sort/deduplicate; compute metric baselines and deviations with versioned rules; apply freshness/confidence flags; normalize raw LifeEvent fields without turning inferred effects into event facts | Invalid unit/range/timezone; inconsistent interval; required state unavailable | No LLM repairs facts; missing remains missing; derivations retain provenance and schema/policy version |
| `DETECT_CONSTRAINTS` | CurrentState + normalized events/preferences/goals → Constraint[] and Context[] | Build hard event unavailability intervals from known event duration; explicit hard preferences; goal windows/deadlines; required ordering/rest; soft recovery/context conditions only where configured evidence exists | Contradictory hard rules; unsupported constraint parameters | Distinguish hard vs soft; no inferred recovery claim is represented as an observation; stable preference and current instruction precedence applied |
| `DETECT_CONFLICTS` | Current plan + Constraint[]/Context[] + goals → Conflict[] | Interval overlap and date checks; session ordering/rest checks; goal capacity conflicts; recovery context conflicts with planned intensity; deterministic severity and reason codes | Incomplete temporal data prevents reliable check; incompatible hard constraints | Every conflict cites inputs/rule codes; conflict does not itself mutate plan or source facts |
| `GENERATE_CANDIDATES` | Current plan, conflicts, constraints, horizon → finite CandidatePlan[] | Enumerate base plan plus only legal operations and bounded combinations; see §4; reject any hard violation before scoring | No valid candidate; combinatorial cap exceeded; no feasible slot | No LLM candidate generation; preserve stable logical session IDs; candidate content/hash frozen |
| `SCORE_CANDIDATES` | Valid candidates + goals/preferences/state/policy → CandidatePlanScore[] | Compute normalized components and fixed weighted score (see §5); no randomness or external state | Missing metric makes component unavailable; invalid numeric values | Hard-invalid candidates never receive a competitive score; score has version and full component trace |
| `OPTIONAL_LLM_ADJUDICATION` | Safe AIContext + valid candidates/scores → PlannerDecision | At most one provider call; invoke only when the deterministic top two valid candidates are within 8.000 points and have materially different tradeoffs; return candidate membership key and rationale codes only; candidate ordering can be shuffled deterministically per request hash | Timeout, provider error, malformed JSON, unknown ID/code | LLM cannot alter candidate or score; otherwise use deterministic ranking; timeout/provider/schema/membership failure falls back to top-ranked valid candidate |
| `VALIDATE_DECISION` | PlannerDecision + generated candidate set + constraints → ValidationResult | Verify membership, owner/run scope, content hash, hard constraints, bounds, overlap/order/type/completeness | Any mismatch/violation | Always post-adjudication; failure cannot be overridden by LLM |
| `BUILD_PLAN` | Valid selected CandidatePlan + parent version → proposed immutable PlanVersion, PlannedSessions, PlanChanges | Verify `Profile.current_plan_version_id` still equals the pointer read at `LOAD_CONTEXT`; assign next version and save within an owner-scoped transaction; diff sessions by logical_session_id; create new version-specific row IDs and persist each typed `schedule_window` into `PlannedSession.schedule_start_at`, `schedule_end_at`, and `schedule_timezone`; do not change current pointer yet | Parent has become superseded/current pointer changed; invalid candidate; persistence conflict | Save as `proposed`; current plan remains unchanged until user explicitly accepts; plan/session content immutable |
| `BUILD_EXPLANATION` | Conflict/score/decision/PlanChange trace → explanation model | For every displayed change, map supported reason codes to approved templates and relevant before/after values | No supporting reason code; inconsistent diff | Never invent rationale; explanations cannot change decision |
| `SELECT_SIGNAL_MOMENT` | Validated plan + supported reasons → SignalMoment | Choose template by reason-code/day context; fill only known safe values | No matching approved template | Copy/personality is non-authoritative and not fed back to planner |
| `SAVE_DECISION` | Run artifacts + proposed plan + explanation → persistent audit state | Persist minimized DecisionRun snapshots, score summaries, adjudication, validation, PlanVersion/Changes/Sessions, SignalMoment atomically where possible | Transaction failure; duplicate version; stale parent; validation absent | No raw prompt/response/full AIContext persisted; failure leaves no partially current plan; proposal is not current |

Every replan is proposed first. The user explicitly accepts or rejects it; silence/timeout is not acceptance. A rejection transitions the proposal to `rejected`, records outcome on DecisionRun/Feedback, and leaves `Profile.current_plan_version_id` and prior plan unchanged. On acceptance, apply optimistic concurrency using the `current_plan_version_id` observed at run start as the expected parent: in one transaction, verify the proposal is still proposed and validated, transition the old accepted current version to `superseded`, transition the proposal to `accepted`, and conditionally update the Profile pointer only where it still equals the expected version. The conditional update is the compare-and-swap; if it affects no row, roll back all transitions and treat the proposal as stale. Only then is the accepted immutable PlanVersion current. This provides the atomic pointer transition required by the data model without silently overwriting a newer plan.

## 4. Candidate generation

Candidate generation is deterministic, finite, and explainable. It starts from a canonical immutable snapshot of the current plan, not from model-written JSON.

### Slot and legality model

1. Convert every planned session's required explicit timezone-aware `schedule_window.start_at/end_at` and each hard event interval into normalized absolute instants plus local display windows. Validate `local_date` against the session start instant in its IANA zone. Reject missing/naive timestamps, `end_at <= start_at`, invalid IANA zones, or inconsistent schedule windows; never infer a precise time from date alone.
2. Define candidate slots from the plan horizon's eligible windows: existing session windows, genuinely free windows allowed by schedule policy, and the remaining portion of a day after hard blocked intervals. Do not make up arbitrary availability outside known windows.
3. Start with baseline candidate `NO_CHANGE`. If it violates any hard constraint, it is not eligible. A soft-constraint miss leaves it valid and incurs its registered penalty. It may still be retained as a diagnostic rejected candidate when hard-invalid, but is never scored/adjudicated as valid.
4. For each conflicting logical session, enumerate legal one-operation alternatives in canonical order. Add paired moves/swaps only where one edit creates the slot needed by the other. Bound combinations by `max_changed_sessions` and `max_candidate_count` in the versioned policy; baseline MVP defaults: 3 changed logical sessions, 32 candidates, breadth-first enumeration by operation count.
5. Normalize duplicate schedules (same logical IDs, dates, type, duration, intensity, attributes) to one candidate hash. Run the hard-constraint feasibility checker on every complete candidate before scoring.

### Operations and legality

| Operation | Legal when | Changes and reason trace |
|---|---|---|
| `MOVE_SESSION` | Session can be assigned to a known free timezone-aware start/end window inside horizon; all ordering, capacity, hard recovery rules and hard preferences hold after move | Retain `logical_session_id`; new version row gets new `PlannedSession.id`; add `HANDLE_TIME_CONFLICT` or `REDUCE_RECOVERY_CONFLICT` as evidenced |
| `REMOVE_SESSION` | Session is optional under goal/plan policy, or no feasible placement exists and scoring can evaluate the loss; never silently remove a mandatory session | Session absent from candidate; `PRESERVE_PRIMARY_GOAL` is not emitted; removal penalty is explicit |
| `ADD_RECOVERY` | Recovery/rest is allowed on that date and does not overwrite a required activity; may replace an optional session only through explicit remove/move operation | Create a recovery slot/session with new logical identity only if the persisted plan represents it as a planned session; reason `REDUCE_RECOVERY_CONFLICT` |
| `REDUCE_INTENSITY` | Activity type supports a lower intensity; result remains in the supported activity/intensity vocabulary and satisfies goal minimums | Same logical session; reason `REDUCE_OVERLOAD` or recovery reason |
| `REDUCE_DURATION` | Duration is flexible and resulting minimum duration still counts toward goal/continuity rules | Same logical session; reason tied to capacity/recovery tradeoff |
| `SWAP_SESSIONS` | Both sessions are movable, slots are compatible, and the resulting schedule satisfies all constraints; enumerate as atomic two-session operation | Retain both logical IDs; trace both before/after placements |
| `PRESERVE_SESSION` | Session already feasible, or other changes resolve its conflict without modifying it | Preserve logical ID and session content in new plan version; reason code must be trace-backed |
| `NO_CHANGE` | All hard constraints remain satisfied and no required user instruction demands a change | Candidate equals parent; may be selected where changes would only add disruption |

Candidate generation does not turn an event into an activity. A social event is not a planned workout called “Social”; it remains LifeEvent context. Add recovery as a planned rest/recovery slot only when the planner's activity catalog treats it as a supported planning entry.

## 5. Scoring function

Hard feasibility is evaluated first. Any candidate with a hard-constraint violation is `valid=false` and excluded from ranking and LLM input; hard violations are not numeric penalties.

For valid candidates, each fit component is normalized to `[0,1]`. A deterministic weighted geometric fit preserves the multiplicative intuition of the initial proposal while making weights explicit:

```text
benefit(c) = 100 × goal_alignment(c)^0.30
                 × preference_adherence(c)^0.15
                 × schedule_fit(c)^0.15
                 × recovery_fit(c)^0.20
                 × training_continuity(c)^0.20

Score(c) = benefit(c) - disruption_cost(c) - soft_constraint_penalties(c)
```

Zero in an essential fit component yields zero benefit; policy distinguishes a legitimate zero from missing evidence. If no goal or preference applies, its component is neutral `1.0` (no unmet obligation), with a provenance flag as appropriate. Each cost is in points `[0,100]`; total is `[-100,100]`. Missing/stale physiology does not become zero recovery fit: mark it unknown, use neutral `0.5`, widen uncertainty, and emit a data-quality flag. These defaults are the adopted **MVP policy v1** and must be versioned.

| Component | Deterministic normalized rule |
|---|---|
| `goal_alignment` | For each active goal compute `min(1, candidate_dose / target_dose)` using the goal's supported explicit target, or the parent-plan dose when no target is set. Weight goal ratios by `priority / sum(active priorities)`; supported doses are defined in §2. |
| `preference_adherence` | Weighted fraction of applicable effective explicit preferences satisfied. Hard preferences are feasibility constraints. Eligible LearnedPattern may influence only this soft component after its evidence threshold and at most 20% of this component; it can never override explicit current instructions or stable Preferences. Learned evidence never edits a stable Preference automatically. |
| `schedule_fit` | 1 minus weighted fraction of sessions outside preferred windows; only soft-window deviations are scored here. Hard unavailable overlap invalidates a candidate. With no preferred-window constraints, this is 1 for feasible schedules. |
| `recovery_fit` | `1 - recovery_stress` from the deterministic physiological heuristic in §1; it covers sleep/load/soreness only, not the separately penalized late-event buffer. With all measures absent, it is neutral 0.5. |
| `training_continuity` | Candidate exercise-dose units retained / parent-plan exercise-dose units, capped at 1, with ordering violations handled as constraints/penalties. Easy intensity contributes 0.75 dose, moderate/hard 1.0; rest/recovery contributes 0 exercise dose. No parent dose means neutral 1. |
| `disruption_cost` | Add fixed points per logical session moved (4), removed (15), added (5), intensity changed (5), duration reduced (0–6 proportional), plus 2 per unique local date touched by a move/swap; cap 100. Count each operation once per logical ID. Any unnecessary extra move costs at least 4 plus affected-date cost; if all fit components/penalties are otherwise unchanged, it strictly lowers score. In §6, A = 2 moves (8) + 3 moved dates (6) + recovery add (5) = 19; B = remove (15) + recovery add (5) = 20; C = 2 moves (8) + 3 dates (6) + recovery add (5) + intensity reduction (5) = 24. |
| `soft_constraint_penalties` | Sum registered soft-rule penalties not already represented by a fit component; cap 100. MVP v1: soft preference miss 0–10 by strength; a high-load session in the derived late-event buffer = 25 points per affected day; other registered rules 0–25 by severity. Do not charge the same rule in both recovery fit and this term. Store each penalty and reason code. |

Tie-break order, independent of call order: higher total score; then higher goal alignment; then higher recovery fit; then lower disruption cost; then lexicographically smaller canonical candidate content hash. A score never overrides validity. Weights, sub-rules, penalties, tie-break policy, and rounding (decimal quantized to 0.001 using half-even) are versioned together as `policy_version` and scorer version in `CandidatePlanScore`/DecisionRun.

## 6. Canonical Friday-night replan trace

### Scenario and explicit assumptions

Plan v1 (Monday 2026-09-28 through Sunday 2026-10-04): Monday Upper; Tuesday Run; Wednesday Lower; Thursday Rest; Friday Run; Saturday Lower; Sunday Long Run. A social LifeEvent spans Friday 23:00 through Saturday 06:00 local time. Strength is the primary goal, running is secondary. Saturday morning recovery availability is reduced. Sunday Long Run should be preserved as a decision-scoped explicit instruction.

For this worked policy trace, use these timezone-aware local windows in `Asia/Calcutta`: Upper/lower strength Monday/Wednesday/Thursday 18:00–19:00; Run Tuesday/Friday 18:00–18:45; Saturday Lower 07:00–08:00; Sunday Long Run 07:00–09:00; Saturday recovery 06:00–12:00. Monday Lower and Thursday Upper fit the available explicit windows; moving Upper to Thursday creates the free Monday slot for Lower. No session repeats by recurrence. The late-event buffer from 06:00 to noon Saturday is a **soft** recovery constraint; exact event time 23:00 Friday–06:00 Saturday is hard unavailable. These are scenario fixture windows, not universal biological rules. Persist each planned session's explicit `schedule_window` as typed schedule columns in the frozen `PlannedSession` schema defined in §2 and `data-model.md`.

The selected solution preserves all three strength sessions and both runs, preserves Sunday's long run, clears Saturday recovery time, and relocates the two displaced sessions with minimal feasible changes: Saturday Lower → Monday; Monday Upper → Thursday; add Saturday recovery. It deliberately preserves Wednesday Lower and Friday Run.

### 1. LifeEvent fact

```json
{
  "id": "event-opaque-1",
  "user_id": "internal-user-ref",
  "kind": "social",
  "label": "Late social event",
  "starts_at": "2026-10-02T17:30:00Z",
  "ends_at": "2026-10-03T00:30:00Z",
  "timezone": "Asia/Calcutta",
  "status": "active"
}
```

Dates/UTC in this trace are illustrative; display-day conversion uses the user's timezone. `LifeEvent` records only user-stated kind/time/label. Label is private and excluded from AI.

Readable aliases such as `pv-1`, `lower-02`, and `event-opaque-1` stand for UUIDs in persisted domain records; `cand-A` through `cand-D` are request-local candidate IDs. They are used to make the trace readable, not as literal UUID values.

### 2. Normalized event

```json
{
  "event_ref": "internal-event-ref",
  "event_class": "social",
  "local_start": "2026-10-02T23:00:00+05:30",
  "local_end": "2026-10-03T06:00:00+05:30",
  "hard_unavailable_window": {"start": "2026-10-02T23:00:00+05:30", "end": "2026-10-03T06:00:00+05:30"},
  "derived_context": [{"code": "REDUCED_SATURDAY_MORNING_RECOVERY_AVAILABILITY", "strength": "soft", "confidence": 0.8}],
  "source_fact_unchanged": true
}
```

### 3. Constraint[]

```json
[
  {"code":"EVENT_UNAVAILABLE","kind":"hard","scope":"interval","window":{"start":"2026-10-02T23:00:00+05:30","end":"2026-10-03T06:00:00+05:30"},"source_type":"life_event","reason_code":"HANDLE_TIME_CONFLICT"},
  {"code":"REDUCED_RECOVERY_WINDOW","kind":"soft","scope":"interval","window":{"start":"2026-10-03T06:00:00+05:30","end":"2026-10-03T12:00:00+05:30"},"source_type":"derived_context","reason_code":"REDUCE_RECOVERY_CONFLICT"},
  {"code":"PRESERVE_LONG_RUN","kind":"hard","scope":"session","target_logical_session":"run-long-01","source_type":"user_instruction","reason_code":"RESPECT_CURRENT_INSTRUCTION"},
  {"code":"STRENGTH_PRIMARY","kind":"soft","scope":"week","source_type":"goal","reason_code":"PRESERVE_PRIMARY_GOAL"}
]
```

The Long Run preservation requirement is a hard, decision-scoped explicit instruction in this scenario, so any candidate that drops or changes that logical session is invalid. It is not a new stable Preference or Goal row.

### 4. Conflict[]

```json
[
  {"conflict_id":"conflict-1","code":"RECOVERY_CONFLICT","severity":"high","affected_logical_session_ids":["lower-02"],"constraint_codes":["REDUCED_RECOVERY_WINDOW"],"date_window":{"start":"Saturday morning","end":"Saturday noon"},"goal_codes":["STRENGTH_PRIMARY"],"reason_codes":["REDUCE_RECOVERY_CONFLICT","PRESERVE_PRIMARY_GOAL"]}
]
```

The conflict is derived from the planned Lower session and reduced-recovery context. It does not rewrite the event fact or assert poor sleep as an observation.

### 5. CurrentState

```json
{
  "as_of":"2026-09-28T12:00:00Z",
  "timezone":"Asia/Calcutta",
  "horizon":{"start":"2026-09-28","end":"2026-10-04"},
  "current_plan_version_id":"pv-1",
  "goals":[{"kind":"STRENGTH","priority":5,"role":"primary"},{"kind":"RUNNING","priority":3,"role":"secondary"}],
  "preferences":[],
  "physiology":null,
  "recovery_context":{"status":"reduced_availability_saturday_morning","confidence":"medium","source":"derived_from_late_event_context"},
  "availability":[{"window":"Friday 23:00-Saturday 06:00 local","kind":"unavailable","source":"life_event"}],
  "training_load":{"band":"moderate","confidence":"medium"},
  "current_plan":[
    {"logical_session_id":"upper-01","local_date":"2026-09-28","start_at":"2026-09-28T18:00:00+05:30","end_at":"2026-09-28T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"upper"},
    {"logical_session_id":"run-01","local_date":"2026-09-29","start_at":"2026-09-29T18:00:00+05:30","end_at":"2026-09-29T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run"},
    {"logical_session_id":"lower-01","local_date":"2026-09-30","start_at":"2026-09-30T18:00:00+05:30","end_at":"2026-09-30T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body"},
    {"logical_session_id":"run-02","local_date":"2026-10-02","start_at":"2026-10-02T18:00:00+05:30","end_at":"2026-10-02T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run"},
    {"logical_session_id":"lower-02","local_date":"2026-10-03","start_at":"2026-10-03T07:00:00+05:30","end_at":"2026-10-03T08:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body"},
    {"logical_session_id":"run-long-01","local_date":"2026-10-04","start_at":"2026-10-04T07:00:00+05:30","end_at":"2026-10-04T09:00:00+05:30","timezone":"Asia/Calcutta","activity":"long_run"}
  ],
  "recent_history":{"completed_sessions":4,"missed_sessions":0,"window_days":14},
  "data_quality":[]
}
```

### 6. CandidatePlan[]

The example below shows the generated feasible set after hard-constraint filtering. Since Saturday recovery is a soft constraint in this scenario, unchanged plan `cand-D` remains valid but scores poorly; it is included so the tradeoff is explicit.

```json
[
  {
    "candidate_id":"cand-A",
    "operations":["MOVE_SESSION:upper-01:Monday->Thursday","MOVE_SESSION:lower-02:Saturday->Monday","ADD_RECOVERY:Saturday"],
    "sessions":[
      {"logical_session_id":"lower-02","local_date":"2026-09-28","start_at":"2026-09-28T18:00:00+05:30","end_at":"2026-09-28T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body","intensity":"moderate"},
      {"logical_session_id":"run-01","local_date":"2026-09-29","start_at":"2026-09-29T18:00:00+05:30","end_at":"2026-09-29T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"moderate"},
      {"logical_session_id":"lower-01","local_date":"2026-09-30","start_at":"2026-09-30T18:00:00+05:30","end_at":"2026-09-30T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body","intensity":"moderate"},
      {"logical_session_id":"upper-01","local_date":"2026-10-01","start_at":"2026-10-01T18:00:00+05:30","end_at":"2026-10-01T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"upper","intensity":"moderate"},
      {"logical_session_id":"run-02","local_date":"2026-10-02","start_at":"2026-10-02T18:00:00+05:30","end_at":"2026-10-02T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"moderate"},
      {"logical_session_id":"recovery-01","local_date":"2026-10-03","start_at":"2026-10-03T06:00:00+05:30","end_at":"2026-10-03T12:00:00+05:30","timezone":"Asia/Calcutta","activity":"recovery","intensity":"recovery"},
      {"logical_session_id":"run-long-01","local_date":"2026-10-04","start_at":"2026-10-04T07:00:00+05:30","end_at":"2026-10-04T09:00:00+05:30","timezone":"Asia/Calcutta","activity":"long_run","intensity":"moderate"}
    ],
    "valid":true,
    "reason_codes":["PRESERVE_PRIMARY_GOAL","REDUCE_RECOVERY_CONFLICT","MINIMIZE_SCHEDULE_DISRUPTION","PRESERVE_TRAINING_VOLUME"]
  },
  {
    "candidate_id":"cand-B",
    "operations":["REMOVE_SESSION:lower-02","ADD_RECOVERY:Saturday"],
    "sessions":[
      {"logical_session_id":"upper-01","local_date":"2026-09-28","start_at":"2026-09-28T18:00:00+05:30","end_at":"2026-09-28T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"upper","intensity":"moderate"},
      {"logical_session_id":"run-01","local_date":"2026-09-29","start_at":"2026-09-29T18:00:00+05:30","end_at":"2026-09-29T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"moderate"},
      {"logical_session_id":"lower-01","local_date":"2026-09-30","start_at":"2026-09-30T18:00:00+05:30","end_at":"2026-09-30T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body","intensity":"moderate"},
      {"logical_session_id":"run-02","local_date":"2026-10-02","start_at":"2026-10-02T18:00:00+05:30","end_at":"2026-10-02T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"moderate"},
      {"logical_session_id":"recovery-01","local_date":"2026-10-03","start_at":"2026-10-03T06:00:00+05:30","end_at":"2026-10-03T12:00:00+05:30","timezone":"Asia/Calcutta","activity":"recovery","intensity":"recovery"},
      {"logical_session_id":"run-long-01","local_date":"2026-10-04","start_at":"2026-10-04T07:00:00+05:30","end_at":"2026-10-04T09:00:00+05:30","timezone":"Asia/Calcutta","activity":"long_run","intensity":"moderate"}
    ],
    "valid":true,
    "reason_codes":["REDUCE_RECOVERY_CONFLICT"]
  },
  {
    "candidate_id":"cand-C",
    "operations":["MOVE_SESSION:upper-01:Monday->Thursday","MOVE_SESSION:lower-02:Saturday->Monday","ADD_RECOVERY:Saturday","REDUCE_INTENSITY:run-02:moderate->easy"],
    "sessions":[
      {"logical_session_id":"lower-02","local_date":"2026-09-28","start_at":"2026-09-28T18:00:00+05:30","end_at":"2026-09-28T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body","intensity":"moderate"},
      {"logical_session_id":"run-01","local_date":"2026-09-29","start_at":"2026-09-29T18:00:00+05:30","end_at":"2026-09-29T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"moderate"},
      {"logical_session_id":"lower-01","local_date":"2026-09-30","start_at":"2026-09-30T18:00:00+05:30","end_at":"2026-09-30T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body","intensity":"moderate"},
      {"logical_session_id":"upper-01","local_date":"2026-10-01","start_at":"2026-10-01T18:00:00+05:30","end_at":"2026-10-01T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"upper","intensity":"moderate"},
      {"logical_session_id":"run-02","local_date":"2026-10-02","start_at":"2026-10-02T18:00:00+05:30","end_at":"2026-10-02T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"easy"},
      {"logical_session_id":"recovery-01","local_date":"2026-10-03","start_at":"2026-10-03T06:00:00+05:30","end_at":"2026-10-03T12:00:00+05:30","timezone":"Asia/Calcutta","activity":"recovery","intensity":"recovery"},
      {"logical_session_id":"run-long-01","local_date":"2026-10-04","start_at":"2026-10-04T07:00:00+05:30","end_at":"2026-10-04T09:00:00+05:30","timezone":"Asia/Calcutta","activity":"long_run","intensity":"moderate"}
    ],
    "valid":true,
    "reason_codes":["PRESERVE_PRIMARY_GOAL","REDUCE_RECOVERY_CONFLICT","REDUCE_OVERLOAD"]
  },
  {
    "candidate_id":"cand-D",
    "operations":["NO_CHANGE"],
    "sessions":[
      {"logical_session_id":"upper-01","local_date":"2026-09-28","start_at":"2026-09-28T18:00:00+05:30","end_at":"2026-09-28T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"upper","intensity":"moderate"},
      {"logical_session_id":"run-01","local_date":"2026-09-29","start_at":"2026-09-29T18:00:00+05:30","end_at":"2026-09-29T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"moderate"},
      {"logical_session_id":"lower-01","local_date":"2026-09-30","start_at":"2026-09-30T18:00:00+05:30","end_at":"2026-09-30T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body","intensity":"moderate"},
      {"logical_session_id":"run-02","local_date":"2026-10-02","start_at":"2026-10-02T18:00:00+05:30","end_at":"2026-10-02T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"moderate"},
      {"logical_session_id":"lower-02","local_date":"2026-10-03","start_at":"2026-10-03T07:00:00+05:30","end_at":"2026-10-03T08:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body","intensity":"moderate"},
      {"logical_session_id":"run-long-01","local_date":"2026-10-04","start_at":"2026-10-04T07:00:00+05:30","end_at":"2026-10-04T09:00:00+05:30","timezone":"Asia/Calcutta","activity":"long_run","intensity":"moderate"}
    ],
    "valid":true,
    "reason_codes":["PRESERVE_PRIMARY_GOAL","PRESERVE_TRAINING_VOLUME"]
  }
]
```

The generator enumerates by fewest operations first, then canonical session order and deduplicates by content hash. The JSON is reordered into A–D strategic comparison labels for readability; an actual run assigns request-local candidate IDs deterministically after canonical ordering. The generator also considers legal reductions and swaps, but only emits them when they change a conflict/goal tradeoff and remain within the operation/candidate caps.

### 7. CandidatePlanScore[]

These values follow the policy rules above. There are no active preferences, so preference adherence is 1.0. Physiology measures are absent, so recovery fit is neutral 0.5 for all candidates. Strength priority is 5 and Running priority is 3; base doses are three strength sessions and three running sessions (including the Long Run). Candidate B preserves 2/3 strength and all running dose, hence goal alignment `(5×2/3 + 3×1)/8 = 0.791667`; it retains 5/6 planned training sessions. Candidate C preserves all strength and 2.75/3 running dose because Friday Run becomes easy at 0.75 dose, hence goal alignment `(5×1 + 3×(2.75/3))/8 = 0.96875`; it retains 5.75/6 session-dose continuity. The late-event buffer penalty applies once to unchanged D (25 points), while A clears it. The scorer calculates these values; it never accepts scores from a user/model.

```json
[
  {"candidate_id":"cand-A","valid":true,"goal_alignment":1.000000,"preference_adherence":1.000000,"schedule_fit":1.000000,"recovery_fit":0.500000,"training_continuity":1.000000,"disruption_cost":19,"soft_constraint_penalty":0,"total":68.055,"reason_codes":["PRESERVE_PRIMARY_GOAL","REDUCE_RECOVERY_CONFLICT","MINIMIZE_SCHEDULE_DISRUPTION","PRESERVE_TRAINING_VOLUME"]},
  {"candidate_id":"cand-B","valid":true,"goal_alignment":0.791667,"preference_adherence":1.000000,"schedule_fit":1.000000,"recovery_fit":0.500000,"training_continuity":0.833333,"disruption_cost":20,"soft_constraint_penalty":0,"total":58.256,"reason_codes":["REDUCE_RECOVERY_CONFLICT"]},
  {"candidate_id":"cand-C","valid":true,"goal_alignment":0.968750,"preference_adherence":1.000000,"schedule_fit":1.000000,"recovery_fit":0.500000,"training_continuity":0.958333,"disruption_cost":24,"soft_constraint_penalty":0,"total":61.499,"reason_codes":["PRESERVE_PRIMARY_GOAL","REDUCE_RECOVERY_CONFLICT","REDUCE_OVERLOAD"]},
  {"candidate_id":"cand-D","valid":true,"goal_alignment":1.000000,"preference_adherence":1.000000,"schedule_fit":1.000000,"recovery_fit":0.500000,"training_continuity":1.000000,"disruption_cost":0,"soft_constraint_penalty":25,"total":62.055,"reason_codes":["PRESERVE_PRIMARY_GOAL","PRESERVE_TRAINING_VOLUME","REDUCE_RECOVERY_CONFLICT"]}
]
```

Worked numerical comparison (weighted geometric benefit, then costs):

```text
cand-A benefit = 100 × 1^.30 × 1^.15 × 1^.15 × 0.5^.20 × 1^.20 = 87.055
cand-A score   = 87.055 - 19 disruption - 0 soft penalty = 68.055

cand-D benefit = 100 × 1^.30 × 1^.15 × 1^.15 × 0.5^.20 × 1^.20 = 87.055
cand-D score   = 87.055 - 0 disruption - 25 recovery-buffer penalty = 62.055
```

`cand-D` is strictly less disruptive (zero changes) and preserves the same goal dose, but leaves a high-load Lower session inside the user's derived Saturday recovery buffer. Clearing that one soft constraint is worth 25 points; the necessary two moves plus recovery insertion cost 19, so A wins by 6 points. This makes the cascade preferable for a traceable reason, while the disruption term still makes every unnecessary additional move reduce score when benefits are unchanged. Values are rounded to 0.001; the policy recomputes and rounds deterministically.

### 8. AIContext

```json
{
  "schema_version":"1",
  "task":"select_valid_candidate",
  "horizon":{"start":"Monday","end":"Sunday","timezone":"Asia/Calcutta"},
  "goals":[{"kind":"STRENGTH","role":"primary","priority":5},{"kind":"RUNNING","role":"secondary","priority":3}],
  "context":[{"code":"REDUCED_SATURDAY_MORNING_RECOVERY_AVAILABILITY","window":"Saturday morning","confidence":"medium"}],
  "constraints":[{"code":"EVENT_UNAVAILABLE","hard":true,"window":"Friday 23:00-Saturday 06:00 local"},{"code":"REDUCED_RECOVERY_WINDOW","hard":false,"window":"Saturday 06:00-12:00 local"},{"code":"PRESERVE_LONG_RUN","hard":true}],
  "conflicts":[{"code":"RECOVERY_CONFLICT","severity":"high","reason_codes":["REDUCE_RECOVERY_CONFLICT"]}],
  "valid_candidates":[
    {"candidate_id":"cand-A","sessions":[{"session_ref":"s1","local_date":"2026-09-28","start_at":"2026-09-28T18:00:00+05:30","end_at":"2026-09-28T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body","intensity":"moderate"},{"session_ref":"s2","local_date":"2026-09-29","start_at":"2026-09-29T18:00:00+05:30","end_at":"2026-09-29T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"moderate"},{"session_ref":"s3","local_date":"2026-09-30","start_at":"2026-09-30T18:00:00+05:30","end_at":"2026-09-30T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body","intensity":"moderate"},{"session_ref":"s4","local_date":"2026-10-01","start_at":"2026-10-01T18:00:00+05:30","end_at":"2026-10-01T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"upper","intensity":"moderate"},{"session_ref":"s5","local_date":"2026-10-02","start_at":"2026-10-02T18:00:00+05:30","end_at":"2026-10-02T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"moderate"},{"session_ref":"s6","local_date":"2026-10-03","start_at":"2026-10-03T06:00:00+05:30","end_at":"2026-10-03T12:00:00+05:30","timezone":"Asia/Calcutta","activity":"recovery","intensity":"recovery"},{"session_ref":"s7","local_date":"2026-10-04","start_at":"2026-10-04T07:00:00+05:30","end_at":"2026-10-04T09:00:00+05:30","timezone":"Asia/Calcutta","activity":"long_run","intensity":"moderate"}],"scores":{"total":68.055,"goal_alignment":1.0,"preference_adherence":1.0,"schedule_fit":1.0,"recovery_fit":0.5,"training_continuity":1.0,"disruption_cost":19,"soft_constraint_penalty":0},"tradeoffs":["preserves all strength and running dose","moves two sessions","clears Saturday recovery buffer"]},
    {"candidate_id":"cand-B","sessions":[{"session_ref":"s1","local_date":"2026-09-28","start_at":"2026-09-28T18:00:00+05:30","end_at":"2026-09-28T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"upper","intensity":"moderate"},{"session_ref":"s2","local_date":"2026-09-29","start_at":"2026-09-29T18:00:00+05:30","end_at":"2026-09-29T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"moderate"},{"session_ref":"s3","local_date":"2026-09-30","start_at":"2026-09-30T18:00:00+05:30","end_at":"2026-09-30T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body","intensity":"moderate"},{"session_ref":"s4","local_date":"2026-10-02","start_at":"2026-10-02T18:00:00+05:30","end_at":"2026-10-02T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"moderate"},{"session_ref":"s5","local_date":"2026-10-03","start_at":"2026-10-03T06:00:00+05:30","end_at":"2026-10-03T12:00:00+05:30","timezone":"Asia/Calcutta","activity":"recovery","intensity":"recovery"},{"session_ref":"s6","local_date":"2026-10-04","start_at":"2026-10-04T07:00:00+05:30","end_at":"2026-10-04T09:00:00+05:30","timezone":"Asia/Calcutta","activity":"long_run","intensity":"moderate"}],"scores":{"total":58.256,"goal_alignment":0.791667,"preference_adherence":1.0,"schedule_fit":1.0,"recovery_fit":0.5,"training_continuity":0.833333,"disruption_cost":20,"soft_constraint_penalty":0},"tradeoffs":["no session relocation","reduces primary strength dose"]},
    {"candidate_id":"cand-C","sessions":[{"session_ref":"s1","local_date":"2026-09-28","start_at":"2026-09-28T18:00:00+05:30","end_at":"2026-09-28T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body","intensity":"moderate"},{"session_ref":"s2","local_date":"2026-09-29","start_at":"2026-09-29T18:00:00+05:30","end_at":"2026-09-29T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"moderate"},{"session_ref":"s3","local_date":"2026-09-30","start_at":"2026-09-30T18:00:00+05:30","end_at":"2026-09-30T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body","intensity":"moderate"},{"session_ref":"s4","local_date":"2026-10-01","start_at":"2026-10-01T18:00:00+05:30","end_at":"2026-10-01T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"upper","intensity":"moderate"},{"session_ref":"s5","local_date":"2026-10-02","start_at":"2026-10-02T18:00:00+05:30","end_at":"2026-10-02T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"easy"},{"session_ref":"s6","local_date":"2026-10-03","start_at":"2026-10-03T06:00:00+05:30","end_at":"2026-10-03T12:00:00+05:30","timezone":"Asia/Calcutta","activity":"recovery","intensity":"recovery"},{"session_ref":"s7","local_date":"2026-10-04","start_at":"2026-10-04T07:00:00+05:30","end_at":"2026-10-04T09:00:00+05:30","timezone":"Asia/Calcutta","activity":"long_run","intensity":"moderate"}],"scores":{"total":61.499,"goal_alignment":0.96875,"preference_adherence":1.0,"schedule_fit":1.0,"recovery_fit":0.5,"training_continuity":0.958333,"disruption_cost":24,"soft_constraint_penalty":0},"tradeoffs":["preserves strength dose","reduces secondary running stimulus and adds an unnecessary change"]},
    {"candidate_id":"cand-D","sessions":[{"session_ref":"s1","local_date":"2026-09-28","start_at":"2026-09-28T18:00:00+05:30","end_at":"2026-09-28T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"upper","intensity":"moderate"},{"session_ref":"s2","local_date":"2026-09-29","start_at":"2026-09-29T18:00:00+05:30","end_at":"2026-09-29T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"moderate"},{"session_ref":"s3","local_date":"2026-09-30","start_at":"2026-09-30T18:00:00+05:30","end_at":"2026-09-30T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body","intensity":"moderate"},{"session_ref":"s4","local_date":"2026-10-02","start_at":"2026-10-02T18:00:00+05:30","end_at":"2026-10-02T18:45:00+05:30","timezone":"Asia/Calcutta","activity":"run","intensity":"moderate"},{"session_ref":"s5","local_date":"2026-10-03","start_at":"2026-10-03T07:00:00+05:30","end_at":"2026-10-03T08:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body","intensity":"moderate"},{"session_ref":"s6","local_date":"2026-10-04","start_at":"2026-10-04T07:00:00+05:30","end_at":"2026-10-04T09:00:00+05:30","timezone":"Asia/Calcutta","activity":"long_run","intensity":"moderate"}],"scores":{"total":62.055,"goal_alignment":1.0,"preference_adherence":1.0,"schedule_fit":1.0,"recovery_fit":0.5,"training_continuity":1.0,"disruption_cost":0,"soft_constraint_penalty":25},"tradeoffs":["no training disruption","keeps Lower inside reduced recovery window"]}
  ],
  "allowed_rationale_codes":["PRESERVE_PRIMARY_GOAL","RESPECT_CURRENT_INSTRUCTION","REDUCE_RECOVERY_CONFLICT","MINIMIZE_SCHEDULE_DISRUPTION","PRESERVE_TRAINING_VOLUME","REDUCE_OVERLOAD"]
}
```

This contains no user/account/record IDs, names, raw event label, free text, raw observations, or unrestricted plan object. A serializer may omit LLM adjudication entirely when deterministic ranking is decisive.

### 9. PlannerDecision

```json
{"selected_candidate_id":"cand-A","rationale_codes":["PRESERVE_PRIMARY_GOAL","RESPECT_CURRENT_INSTRUCTION","REDUCE_RECOVERY_CONFLICT","PRESERVE_TRAINING_VOLUME"]}
```

### 10. ValidationResult

```json
{
  "valid":true,
  "violations":[],
  "validator_version":"1",
  "candidate_content_hash":"sha256:fixture-cand-A",
  "validated_at":"2026-09-28T12:00:01Z"
}
```

### 11. PlanChange[]

```json
[
  {"change_type":"moved","logical_session_id":"lower-02","before":{"local_date":"2026-10-03","start_at":"2026-10-03T07:00:00+05:30","end_at":"2026-10-03T08:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body"},"after":{"local_date":"2026-09-28","start_at":"2026-09-28T18:00:00+05:30","end_at":"2026-09-28T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"lower_body"},"reason_code":"PRESERVE_PRIMARY_GOAL"},
  {"change_type":"moved","logical_session_id":"upper-01","before":{"local_date":"2026-09-28","start_at":"2026-09-28T18:00:00+05:30","end_at":"2026-09-28T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"upper"},"after":{"local_date":"2026-10-01","start_at":"2026-10-01T18:00:00+05:30","end_at":"2026-10-01T19:00:00+05:30","timezone":"Asia/Calcutta","activity":"upper"},"reason_code":"MINIMIZE_SCHEDULE_DISRUPTION"},
  {"change_type":"added","logical_session_id":"recovery-01","before":null,"after":{"local_date":"2026-10-03","start_at":"2026-10-03T06:00:00+05:30","end_at":"2026-10-03T12:00:00+05:30","timezone":"Asia/Calcutta","activity":"recovery"},"reason_code":"REDUCE_RECOVERY_CONFLICT"}
]
```

All unchanged sessions remain in Plan v2 but do not need a PlanChange row. The old Saturday Lower instance and new Monday Lower instance have distinct `PlannedSession.id` values and the same `logical_session_id`.

### 12. Final PlanVersion (proposed)

```json
{
  "id":"pv-2",
  "user_id":"internal-user-ref",
  "version_number":2,
  "parent_version_id":"pv-1",
  "decision_run_id":"decision-1",
  "trigger_kind":"life_event",
  "trigger_ref":"internal-event-ref",
  "horizon_start":"2026-09-28",
  "horizon_end":"2026-10-04",
  "status":"proposed",
  "sessions":[
    {"logical_session_id":"lower-02","local_date":"2026-09-28","schedule_window":{"start_at":"2026-09-28T12:30:00Z","end_at":"2026-09-28T13:30:00Z","timezone":"Asia/Calcutta"},"activity_type":"lower_body","intensity":"moderate","duration_minutes":60,"attributes":null},
    {"logical_session_id":"run-01","local_date":"2026-09-29","schedule_window":{"start_at":"2026-09-29T12:30:00Z","end_at":"2026-09-29T13:15:00Z","timezone":"Asia/Calcutta"},"activity_type":"run","intensity":"moderate","duration_minutes":45,"attributes":null},
    {"logical_session_id":"lower-01","local_date":"2026-09-30","schedule_window":{"start_at":"2026-09-30T12:30:00Z","end_at":"2026-09-30T13:30:00Z","timezone":"Asia/Calcutta"},"activity_type":"lower_body","intensity":"moderate","duration_minutes":60,"attributes":null},
    {"logical_session_id":"upper-01","local_date":"2026-10-01","schedule_window":{"start_at":"2026-10-01T12:30:00Z","end_at":"2026-10-01T13:30:00Z","timezone":"Asia/Calcutta"},"activity_type":"upper","intensity":"moderate","duration_minutes":60,"attributes":null},
    {"logical_session_id":"run-02","local_date":"2026-10-02","schedule_window":{"start_at":"2026-10-02T12:30:00Z","end_at":"2026-10-02T13:15:00Z","timezone":"Asia/Calcutta"},"activity_type":"run","intensity":"moderate","duration_minutes":45,"attributes":null},
    {"logical_session_id":"recovery-01","local_date":"2026-10-03","schedule_window":{"start_at":"2026-10-03T00:30:00Z","end_at":"2026-10-03T06:30:00Z","timezone":"Asia/Calcutta"},"activity_type":"recovery","intensity":"recovery","duration_minutes":360,"attributes":null},
    {"logical_session_id":"run-long-01","local_date":"2026-10-04","schedule_window":{"start_at":"2026-10-04T01:30:00Z","end_at":"2026-10-04T03:30:00Z","timezone":"Asia/Calcutta"},"activity_type":"long_run","intensity":"moderate","duration_minutes":120,"attributes":null}
  ]
}
```

This is a proposal, not current until accepted. Acceptance updates the Profile pointer transactionally; rejection leaves v1 current. The exact rationale chain is: reduced Saturday morning availability → Saturday Lower/recovery conflict → `cand-A` preserves primary strength volume, running, and Sunday Long Run while moving only two sessions and adding recovery → validated `PlanChange`s → explanation from their reason codes.

## 7. LLM adjudication

The LLM receives **only** the versioned minimized `AIContext`: valid candidates, deterministic scores, safe tradeoffs, and relevant allowlisted constraints/conflicts. It receives no DB objects, direct identifiers, raw event labels/notes, credentials, raw observations, or internal tools. Normal path uses zero or one LLM call.

Strict output schema:

```json
{
  "type":"object",
  "required":["selected_candidate_id","rationale_codes"],
  "additionalProperties":false,
  "properties":{
    "selected_candidate_id":{"type":"string","minLength":1,"maxLength":80},
    "rationale_codes":{"type":"array","maxItems":8,"uniqueItems":true,"items":{"type":"string","enum":["PRESERVE_PRIMARY_GOAL","RESPECT_CURRENT_INSTRUCTION","REDUCE_RECOVERY_CONFLICT","MINIMIZE_SCHEDULE_DISRUPTION","RESPECT_USER_PREFERENCE","PRESERVE_TRAINING_VOLUME","REDUCE_OVERLOAD","HANDLE_TIME_CONFLICT"]}}
  }
}
```

After parse/schema validation, require selected ID membership in the valid candidate set and require every rationale code to be both allowed and supported by that candidate's score/conflict trace. Any failure, timeout, malformed output, provider failure, unknown candidate, or unsupported rationale records a specific fallback code and selects the highest-scoring valid candidate using deterministic tie-breaks. The provider deadline is configurable but must leave time for validation/build and remain inside the total 2.5 s replan budget. If no valid candidate exists, do not call the LLM. Adjudication cannot modify the immutable candidate content/hash. Provider/model/token/cost/latency metadata may be stored in `DecisionRun`; raw prompt/response and full AIContext are not stored by default.

## 8. Deterministic validation

Validation always runs after any LLM selection and before PlanVersion construction/acceptance. It verifies:

1. Selected candidate exists and belongs to this exact DecisionRun's generated valid set.
2. Candidate owner and parent plan match the authenticated user and current run scope.
3. Candidate content hash matches the generator's frozen hash; no LLM or intermediary modified sessions/operations/scores.
4. Every hard constraint is satisfied; no session occupies a hard unavailable interval.
5. Every session date is within plan horizon and timezone interpretation is valid.
6. No impossible overlaps or duplicate logical session placements occur; daily capacity rules hold.
7. Required session ordering/rest constraints hold.
8. Activity type, intensity, duration, and attributes use supported schemas and valid ranges.
9. Required plan completeness is met, including preserved hard goal requirements and explicit current instruction.
10. Persisted `user_id`, PlanVersion, PlannedSession, PlanChange, DecisionRun, and trigger provenance all belong to the same user.

If validation fails, mark the run validation failure, record violation codes, do not save or activate the invalid proposal, and invalidate request-result caches. Try the highest-ranked remaining unselected valid candidate only if it independently passes the same validator; otherwise use the deterministic highest-scoring valid candidate and revalidate. If none validate, produce no new PlanVersion, keep the existing current plan, and return a clear safe explanation asking for the missing/conflicting information. Validation failure is never repaired by asking the LLM to invent a fix.

## 9. Explainability

SIGNAL answers **“What changed?”** from the structural `PlanChange` diff, then **“Why did you change it?”** from the trace:

`Conflict → CandidatePlanScore → selected CandidatePlan → PlanChange → user-facing explanation`

Registered deterministic reason codes:

- `PRESERVE_PRIMARY_GOAL`
- `RESPECT_CURRENT_INSTRUCTION`
- `REDUCE_RECOVERY_CONFLICT`
- `MINIMIZE_SCHEDULE_DISRUPTION`
- `RESPECT_USER_PREFERENCE`
- `PRESERVE_TRAINING_VOLUME`
- `REDUCE_OVERLOAD`
- `HANDLE_TIME_CONFLICT`

Each code has a rule definition and allowed display template. A reason code can appear on a PlanChange only if a corresponding constraint/conflict or score component supports it, and every displayed explanation is composed from those codes plus actual before/after fields. LLM rationale may select/reorder supported codes but cannot create a new rationale. If the trace is incomplete, use a neutral explanation (“This session moved to make room for your updated schedule”) or report an internal explanation failure; never invent a physiological reason. A SignalMoment is selected from template keys backed by the same trace and does not affect the plan.

## 10. User override behavior

An explicit current instruction is decision-scoped, stored as user-originated structured input/Feedback or normalized override context with provenance, and ranks above stable Preferences and LearnedPatterns. Do not silently rewrite the persistent Preference row.

For “I feel fine. I'm training.” after SIGNAL recommends rest:

1. Sanitize/structure it into a constrained intent such as `{kind:"train_despite_recovery_suggestion", session_ref:<internal ref>, source:"explicit_current_instruction", recorded_at:<timestamp>}`. Store only this minimized override object under the existing `DecisionRun.input_snapshot` and set `trigger_kind="user_instruction"`; do not forward raw wording to the LLM. It is decision-scoped, not a new Preference/entity.
2. Start a new `DecisionRun` referencing the current PlanVersion and override trigger. Rebuild CurrentState/constraints/candidates; do not mutate the earlier run or proposal.
3. Generate alternatives satisfying hard feasibility/safety constraints: preserve original session, lower intensity/duration, move to feasible time, or no feasible recommendation. If rest was only a soft recovery preference, user request can outrank it. An actual hard event overlap or hard safety constraint remains binding.
4. Score with explicit current instruction at precedence tier 1; exclude learned patterns/preferences that contradict it from steering this decision. Do not turn the instruction into a durable Preference unless user explicitly asks for that.
5. Validate, then create a new immutable proposed PlanVersion and PlanChanges. Acceptance/rejection follows the normal lifecycle transaction.

If the instruction conflicts with an inviolable hard constraint, explain which feasible options remain and let the user choose among them; do not falsely claim the user is forbidden or that the LLM overruled them.

## 11. Learned feedback

Learning is deterministic evidence accumulation, not ML and not a separate memory store:

`DecisionRun → user Feedback → observed outcome (TrainingSession / physiology if provided) → qualifying evidence → LearnedPattern update`

Qualifying evidence is a traceable, completed behavior or explicit structured outcome linked to a recommendation or relevant event, e.g. user accepted a recovery change and then completed it; user repeatedly chose easier sessions after late events. Repeated evidence is deduplicated by source/outcome so one action cannot count twice. An explicit user correction/rejection can immediately disable a pattern regardless of count.

Not qualifying: model rationale, generated SignalMoment text, an unaccepted proposal, missing data, synthetic test data mixed with real-user evidence, correlation inferred from one ambiguous event, or raw user prose without structured user confirmation. Do not infer personality traits or medical effects.

Policy v1 learning proposal:

- Pattern starts `candidate`; minimum evidence count `n ≥ 3` distinct qualifying observations and confidence `≥ 0.70` required for active influence.
- Confidence is deterministic and explainable: `support / (support + contradiction + 2)` with counts from deduplicated evidence, rounded to 0.001. A single event/outcome is insufficient. This is a shrinkage heuristic, not a calibrated probability; call it confidence only as a relative evidence strength.
- Staleness: evidence older than 180 days is excluded from active-window support; no qualifying evidence for 180 days sets status `stale` unless explicit confirmation is recent. Exact windows remain policy-versioned/open for calibration.
- Synthetic evidence never activates user patterns in production.
- User may inspect, confirm, correct, or reject. Rejection sets `status=rejected`; correction creates reviewed interpretation/provenance, recomputes future evidence rather than rewriting historical source records. Repeated evidence (`n ≥ 3`, confidence ≥0.70) is required before SIGNAL surfaces a learned pattern as a possible preference update; the stable `Preference` changes only after the user's explicit confirmation/edit. Evidence never auto-modifies a stable Preference.

LearnedPattern is below any current explicit instruction and any explicit stable Preference. It may influence only soft ranking, never hard feasibility.

## 12. Edge cases

| Case | Expected behavior / fallback | New PlanVersion? | Notify user? |
|---|---|---:|---|
| No valid candidate | Preserve current plan; explain incompatible constraints and request input or manual choice | No | Yes, blocking explanation |
| No current plan | Run initial deterministic plan generation from onboarding/goals/preferences; same validation; do not fabricate prior plan or PlanChanges | Proposed version only if valid | Yes, show initial plan for acceptance |
| Multiple simultaneous LifeEvents | Normalize all independently, derive union of windows/context, deduplicate same constraints, detect combined conflicts, generate bounded candidates | If valid choice accepted/proposed | Yes, summarize all material triggers |
| Conflicting hard preferences | Deterministic conflict; do not choose an arbitrary winner; ask user to resolve/clarify. Current explicit instruction may resolve if it applies to this decision | No until resolved and valid | Yes |
| Two equally scored candidates | Apply fixed tie-break tuple then content hash; if still distinct and material, optional LLM among tie set only; no call if deterministic equivalence | Proposal if validated | Yes if user-facing choice matters |
| Stale physiology | Mark stale; exclude from strong recovery claims; use neutral uncertainty and safe schedule constraints; do not treat stale as current fact | Valid plan may be proposed | Mention uncertainty only if decision-relevant |
| Missing physiology | Use schedule/goals/preferences; neutral recovery score and flag; avoid physiological recommendation | Valid plan may be proposed | Only if missing input materially limits result |
| User override | New DecisionRun; current instruction outranks learned/stable soft signals; hard constraints remain; regenerate and validate | Proposed version if valid | Yes, acknowledge applied/blocked part |
| Rejected proposal | Mark rejected and record Feedback/outcome; current pointer remains unchanged | No additional version | Confirm rejection; allow next instruction |
| Concurrent replans | Each run records its starting `Profile.current_plan_version_id`. Before saving a proposal and again at acceptance, compare the current pointer to that expected parent. On mismatch, abort/roll back and rerun from the newer plan; a proposal already shown is marked `superseded`, with stale/expired run outcome. Acceptance uses an atomic conditional pointer update (compare-and-swap); no unconditional overwrite or silent last-writer-wins | No stale proposal becomes current; regenerated proposal only after rerun | Yes if user-visible proposal changed/expired |
| LLM unavailable/timeout | Deterministic highest-scoring valid candidate | Yes, proposed if validation succeeds | No special notice unless useful; record fallback |
| Malformed LLM output/unknown candidate | Reject output; deterministic top valid fallback; record fallback reason | Yes, proposed if validation succeeds | No unless repeated/provider issue surfaced operationally |
| Validation failure | Reject candidate; try remaining candidates deterministically and revalidate; if none pass, retain current plan and explain safe failure | No invalid version | Yes, explain no change and next step |
| Plan already superseded before accept | Recheck parent under transaction; do not accept stale proposal; recompute from new current version | No for stale plan; new proposal only after rerun | Yes |

## 13. Complexity, latency, and caching

Targets:

- Today decision: **<300 ms excluding optional LLM**.
- Replan: **<2.5 s including normally zero/one LLM call**.
- **Normally ≤1 LLM call per replan.**

Candidate search is bounded by a finite horizon, canonical operation ordering, `max_changed_sessions`, and `max_candidate_count`. Complexity is approximately `O(C × (S + K))` for `C` bounded candidates, `S` sessions, and `K` constraints, plus bounded history aggregation; the MVP does not need a generic optimizer service.

Safe caching:

- CurrentState/derived metrics by authenticated user scope, current plan version, source-data version/freshness, horizon, timezone, engine/policy/schema version.
- Current plan by immutable PlanVersion ID; separate pointer cache by Profile row/version. Never overwrite historical cache values.
- Exact AI request/result by hash of canonical AIContext, candidate content hashes, model/provider, prompt/schema, and policy versions. A cache hit must still pass deterministic validation before persistence/use.
- Targeted invalidation on physiology/session/event/goal/preference/feedback changes, current pointer changes, and version changes. TTL is a secondary safeguard, not correctness mechanism.

Do not cache raw identity data or use stale values as facts. In-memory/local caching is sufficient for MVP; Redis is not required. Measure stage latency and cache hit/miss rate without logging sensitive payloads.

## 14. Evaluation hooks and unresolved decisions

### Metrics and golden suite

The engine must run without an LLM using a deterministic selection mode or `MockLLM`. Each scenario has versioned input fixtures, expected hard constraints, expected allowed candidate set, score ranges/order where deterministic, expected PlanChanges/reason codes, and whether a user prompt is required.

Track at minimum:

- **Hard constraint satisfaction:** valid selected plans / all produced plans; target 100%.
- **Goal preservation:** weighted primary/secondary goal alignment by plan and scenario.
- **Preference adherence:** explicit preference satisfaction; report current instruction and stable preference separately.
- **Recovery/context fit:** expected rule outcome and evidence freshness handling.
- **Plan disruption:** number of moved/removed/modified sessions and disruption-cost distribution.
- **Validation failure rate:** invalid adjudications and invalid deterministic candidates, separated by stage.
- **Fallback rate:** LLM timeout/provider/schema/membership fallback, plus no-candidate failures.
- **LLM agreement:** fraction where LLM selection equals deterministic top-ranked candidate; report tie cases separately, not as errors.
- **Decision latency:** p50/p95/p99 total and per stage; token/cost and cache hit rate when applicable.

A **30–50 scenario** golden suite should cover poor sleep, social events, travel, work conflicts, missed sessions, conflicting goals, high training load, current user overrides, simultaneous events, stale/missing data, infeasible constraints, ties, rejected proposals, concurrency/stale-parent behavior, malformed/unavailable LLM, and validation tampering. Synthetic fixture data must remain labelled and isolated from learned user patterns. Run suite deterministically on every engine/policy/schema change; use a separate optional provider agreement evaluation.

### Resolved MVP policy

The policy choices below are the MVP defaults for implementation and golden-suite evaluation, not additional database entities:

1. **Schedule:** Every scheduled session and event has an explicit timezone-aware start/end instant and IANA timezone. Persist instants in UTC and validate the local date against the zone. There is no recurrence engine; each occurrence is materialized.
2. **Goals:** The closed goal catalog is `STRENGTH`, `RUNNING`, `ENDURANCE`, `RECOVERY`, `GENERAL_FITNESS`; target units and dose rules are specified in §2.
3. **Constraints:** Hard violations invalidate candidates before scoring. Soft violations incur versioned bounded penalties. The MVP hard/soft examples and late-event buffer are specified in §1 and §5.
4. **Recovery:** Use only the deterministic, versioned heuristic in §1; no ML or clinical inference.
5. **Scoring:** Use only the normalized weighted geometric fit and explicit point costs in §5. All weights, thresholds, penalties, tie-break order, rounding and candidate scores are versioned. The worked scenario in §6 is calculated numerically.
6. **Acceptance/concurrency:** Every replan is proposed, then explicitly accepted or rejected. Acceptance requires validation and compare-and-swap against the `Profile.current_plan_version_id` observed at run start. A stale proposal does not overwrite or become current; re-run against the newer version to produce a fresh proposal. Rejection leaves current plan untouched.
7. **Learning:** No ML. A candidate pattern needs at least 3 distinct qualifying observations and confidence ≥0.70 before it can influence soft scoring; explicit user review can correct/reject it. It does not automatically rewrite a stable Preference.
8. **LLM:** Invoke at most once, only if the top two deterministic candidates are within 8.000 points and expose materially different tradeoffs. Provider budget is within the 2.5-second replan target. The LLM may select only a supplied candidate and supported rationale codes; deterministic validation always follows adjudication. On any failure, choose the highest-ranked valid candidate.
9. **Bounded search:** MVP defaults are at most 3 changed logical sessions and 32 candidates, breadth-first by operation count. Version these caps and evaluate coverage with the golden suite.

Operational calibration—such as tuning weights/thresholds from a reviewed golden suite, expanding the supported constraint/activity vocabulary, selecting reviewed explanation templates, and provider-specific retention terms—can be handled as versioned policy/product work. These do not block the conceptual model or permit changing its privacy, validation, lifecycle, or concurrency guarantees.

### Final consistency review against `data-model.md`

| Check | Decision-engine treatment | Consistency |
|---|---|---|
| Concepts/entities | Source facts use the frozen User/Profile, Goal, Preference, PhysiologyObservation, TrainingSession, PlannedSession, LifeEvent, PlanVersion, PlanChange, DecisionRun, LearnedPattern, Feedback, and SignalMoment schema. `CurrentState`, `RecentHistory`, `Constraint`, `Conflict`, `CandidatePlan`, `CandidatePlanScore`, `AIContext`, `PlannerDecision`, and `ValidationResult` are typed derived/service contracts, not new tables. | Consistent |
| Current plan and lifecycle | Currentness is exclusively `Profile.current_plan_version_id`; lifecycle is `proposed`, `accepted`, `rejected`, `superseded`. Proposed plans do not become current before validated explicit acceptance and transactional compare-and-swap. | Consistent |
| Stable logical session identity | Every candidate and versioned planned session retains `logical_session_id`; `PlanChange` diff matches it across versions while concrete `PlannedSession.id` changes per version. | Consistent |
| LifeEvent facts | LifeEvent stores what happened/is planned to happen. Deterministic normalization derives availability/context, then constraints, conflicts, and candidates; derived consequences are not written as event facts. | Consistent |
| DecisionRun purpose | Run stores minimized provenance/audit artifacts only: minimized input snapshot, conflict and score summaries, provider metadata, validated adjudication, validation, and outcome. It is not a state dump/cache; no raw prompt/response or full AIContext by default. | Consistent |
| AI privacy boundary | Identity data is excluded; `AIContext` is freshly built by its explicit versioned allowlist serializer. Raw free text is excluded unless sanitized/structured explicitly; full database objects never pass through. | Consistent |
| Candidate authority and validation | Candidates are generated deterministically; LLM cannot generate/edit candidates or facts. Deterministic membership/content/hard-rule validation follows LLM adjudication; failure uses deterministic fallback or safe failure. | Consistent |
| User overrides | An override creates a new DecisionRun and proposed immutable version; earlier runs/proposals remain unchanged. The structured, minimized instruction is decision-scoped, wins precedence for that decision, and is not automatically made a stable Preference. | Consistent |
| Learning/preferences | LearnedPattern is evidence-backed behavioral evidence, not authoritative preference or generic AI memory. Thresholded influence remains below explicit stable Preference; user can correct/reject it; no ML. | Consistent |

The policies and entity concepts are consistent. `PlannedSession.schedule_window` is required for every planned session and is represented by typed `schedule_start_at`, `schedule_end_at`, and `schedule_timezone` columns. Both documents require timezone-aware instants, strict positive duration, IANA timezone validation, UTC normalization, and overlap checks against normalized absolute instants. Extensible `attributes` remain separate and contain no duplicate scheduling data.

### Readiness

The cross-document schedule schema is resolved, and the architecture is ready to freeze for implementation planning. No application or database implementation is authorized by this design review.
