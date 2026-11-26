# SIGNAL Product and Technical Specification

## Purpose

This document is the canonical product and technical specification for SIGNAL. It consolidates the product direction, MVP scope, architecture, safety principles, and evaluation requirements that should guide implementation.

The existing README describes SIGNAL as: “Your training has a plan. Your life has other plans. SIGNAL makes them work together.” This specification expands that direction into an actionable product and engineering plan.

## 1. Product

- **Name:** SIGNAL
- **Category:** Personal performance orchestration
- **Core thesis:** “SIGNAL reconciles your training, physiology, goals and real-world commitments into a plan you can actually live with.”
- **Core loop:** Plan → Life happens → Replan → Learn
- **Killer workflow:** “Tell SIGNAL what changed. It figures out what needs to change.”
- **Flagship feature:** Life Replan

SIGNAL helps a person maintain a realistic plan as their body, goals, preferences, work, social life, schedule, and unexpected events interact. Its job is to resolve conflicts across those domains and explain the resulting plan.

### Product experience

Primary screens:

- **Today** — the current day's plan and relevant context.
- **Plan** — the weekly plan and its versions.
- **Life Replan** — capture a change and review its effects on the plan.
- **Me** — the user's profile, preferences, and learned patterns.

Core interaction:

> **User:** “Something changed.”  
> **SIGNAL:** “What happened?”  
> **User:** “Late social event Friday.”  
> **SIGNAL:** Recalculates the week and presents the result.

A replan result should show:

- The plan before the change.
- The event or change that triggered replanning.
- Detected conflicts.
- The proposed plan after replanning.
- Why the plan changed.
- A **SIGNAL Moment**: a concise, contextual line that makes the adjustment feel human. For example: “You survived last night. Let's not pretend today is leg day.”

Personality is a brand and user-experience layer, not the technical moat.

## 2. Differentiation

SIGNAL is not:

- A fitness tracker.
- A wearable dashboard.
- An AI fitness coach.
- A recovery score.
- A workout generator.

Its core job is resolving conflicts among physiology, training, goals, preferences, work, social life, schedule, and unexpected events.

- **MVP moat:** Cross-domain conflict resolution.
- **Long-term moat:** Learning an individual's preferences and responses over time.

## 3. MVP Scope

Build only:

- Onboarding.
- Synthetic longitudinal data.
- Today screen.
- Weekly plan.
- Life Replan.
- Conflict detection.
- Candidate plan generation.
- Deterministic scoring.
- LLM adjudication among valid candidates.
- Deterministic validation.
- Explanations.
- SIGNAL Moments.
- Basic learned patterns.
- Evaluation suite.

Do not build for the MVP:

- Garmin, WHOOP, Oura, or Apple Health integrations.
- Nutrition tracking.
- Retrieval-augmented generation (RAG) or a vector database.
- Kafka or Kubernetes.
- Complex machine learning.
- A large multi-agent architecture.

## 4. Architecture

### Backend

- Python
- FastAPI
- Pydantic
- SQLAlchemy
- PostgreSQL

### Frontend

- Next.js
- TypeScript

### AI

- LangGraph for bounded reasoning workflows.
- An `LLMProvider` abstraction so business logic does not depend on a specific provider.

### Infrastructure

- Docker Compose for local development and the MVP deployment shape.
- Redis is optional; it is not required for the MVP.

## 5. Core Principle

**Facts → deterministic code**  
**Decisions → deterministic optimization + constrained reasoning**  
**Language → LLM/templates**

The LLM is not the source of truth. Deterministic application logic owns facts, calculations, constraints, and validation. The LLM may help adjudicate between valid options and express explanations, but it must not override the system's factual or safety boundaries.

## 6. Privacy and AI Boundary

Keep three planes separate:

1. **Identity plane** — direct identity and account information.
2. **Data plane** — user profile, physiology, plans, events, and history.
3. **AI reasoning plane** — only the minimum derived or contextual information needed for a specific reasoning task.

The LLM must never receive names, email addresses, phone numbers, addresses, authentication credentials, API keys, raw account IDs, or unnecessary identifying information. Age, sex, height, weight, and physiological information may be used internally when relevant, but only the minimum task-specific derived/contextual information may cross the AI boundary.

Requirements:

- Use an allowlist-based `AIContext` serializer.
- Never serialize entire user or database objects into prompts.
- Do not give the LLM direct database access.
- Validate AI input and output.
- Keep secrets in environment variables; never commit API keys to source control.
- Do not add unnecessary external integrations or model access to internal tools/database in the MVP.

## 7. Memory and Preference Precedence

Use four conceptual memory levels:

1. Current state.
2. Recent history.
3. Stable preferences.
4. Learned patterns.

Memory must be structured and evidence-based. Each learned pattern must include an observation, confidence, evidence count, and timestamps.

When instructions conflict, apply this precedence:

1. Explicit current user instruction.
2. Explicit stable preference.
3. Learned preference.
4. Generic heuristic.

## 8. Planning and Replanning

The deterministic planning engine calculates baselines, deviations, conflicts, constraints, candidate plans, and candidate scores. The LLM adjudicates between valid candidate plans; it does not invent arbitrary plans.

Normal replanning should require zero or one LLM call. After any LLM adjudication, deterministic validation must run before a plan is accepted. Invalid or unavailable model output must follow a defined safe fallback path.

## 9. Plan Versioning

Plans are immutable versions. Never overwrite a historical plan. Store at least:

- Plan version.
- Parent version.
- Changes made.
- Trigger for the replan.
- Decision metadata.

## 10. Cost and Performance

Development should be possible at approximately zero API cost. Support:

- `MockLLM` for deterministic development and evaluation.
- Ollama or other local models.
- A hosted provider adapter.

Use the `LLMProvider` abstraction; do not hard-code a provider into business logic.

Targets:

- **Today:** under 300 ms.
- **Replan:** under 2.5 seconds.
- **LLM usage:** normally no more than one call per replan.

Use caching, context compression, and deterministic computation to reduce latency and cost.

## 11. Caching

Potential cache targets:

- Current state.
- Derived metrics.
- Plan.
- Exact LLM request/result.

Use versioned cache keys and targeted invalidation. Redis may be added if a demonstrated need emerges, but it is not required for the MVP.

## 12. Observability

Track:

- Latency.
- Cache hit rate.
- Model.
- Token usage.
- Cost.
- Validation failures.
- Fallback rate.
- Decision status.

Do not log raw sensitive user data by default.

## 13. Evaluation

Create a golden scenario suite covering:

- Poor sleep.
- Social events.
- Travel.
- Work conflicts.
- Missed workouts.
- Conflicting goals.
- High training load.
- User overrides.
- Multiple simultaneous events.

Evaluate:

- Constraint satisfaction.
- Goal preservation.
- Preference adherence.
- Context accuracy.
- Plan validity.
- Fallback behavior.
- Latency.
- Cost.

## 14. Engineering Philosophy

Prioritize:

**Correctness > explainability > simplicity > latency > cost > sophistication**

Do not introduce infrastructure or dependencies without a demonstrated need. Keep the MVP small enough to build and evaluate quickly while allowing the architecture to expand in the future.

