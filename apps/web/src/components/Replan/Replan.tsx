import { useState } from "react";
import { api } from "../../api/client";
import type { PlanResponse, ReplanResponse } from "../../types/api";
import { SignalMoment } from "../SignalMoment";

const eventOptions = [
  ["late_night", "Late night"], ["social", "Social event"], ["work", "Work"], ["travel", "Travel"],
  ["poor_sleep", "Poor sleep"], ["soreness", "Soreness"], ["missed_workout", "Missed workout"],
];

type Props = { onClose: () => void; onPlanChanged: (plan: PlanResponse) => void };

function sessionName(session: ReplanResponse["result"]["original_plan"][number] | null | undefined) {
  if (!session) return "—";
  return ({ upper: "Upper", lower_body: "Lower", run: "Run", long_run: "Long Run", recovery: "Recovery" } as Record<string, string>)[session.activity_type] ?? session.activity_type;
}

function shortPlan(plan: ReplanResponse["result"]["original_plan"]) {
  const byDate = new Map(plan.map((session) => [session.schedule_window.start_at.slice(0, 10), session]));
  return ["2026-10-02", "2026-10-03", "2026-10-04"].map((day) => ({ day, session: byDate.get(day) }));
}

export function Replan({ onClose, onPlanChanged }: Props) {
  const [kind, setKind] = useState("social");
  const [startsAt, setStartsAt] = useState("2026-10-02T23:00");
  const [endsAt, setEndsAt] = useState("2026-10-03T06:00");
  const [impact, setImpact] = useState("high");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [response, setResponse] = useState<ReplanResponse | null>(null);
  const [feedbackMessage, setFeedbackMessage] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setResponse(null);
    try {
      const result = await api.replan({ kind, starts_at: startsAt, ends_at: endsAt, impact });
      setResponse(result);
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : "Unable to replan right now.");
    } finally {
      setBusy(false);
    }
  }

  async function decide(action: "accept" | "reject") {
    setBusy(true);
    setError("");
    try {
      await api.feedback(action, response?.result.decision_id);
      if (action === "accept") onPlanChanged(await api.plan());
      setFeedbackMessage(action === "accept" ? "Plan accepted. Your week is updated." : "Kept your original plan.");
      setResponse(null);
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : "Unable to save your choice.");
    } finally {
      setBusy(false);
    }
  }

  const before = response ? shortPlan(response.result.original_plan) : [];
  const after = response?.result.proposed_plan ? shortPlan(response.result.proposed_plan) : [];

  return (
    <div className="replan-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="replan-panel" role="dialog" aria-modal="true" aria-labelledby="replan-title">
        <div className="replan-top"><span className="eyebrow">LIFE REPLAN</span><button className="close-button" onClick={onClose} aria-label="Close">×</button></div>
        {!response ? (
          <>
            <h2 id="replan-title">What changed?</h2>
            <p className="muted replan-intro">A little context helps us find a week that still works.</p>
            <form onSubmit={submit} className="replan-form">
              <label>What happened?
                <select value={kind} onChange={(event) => setKind(event.target.value)}>
                  {eventOptions.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
                </select>
              </label>
              <div className="field-grid">
                <label>Starts<input type="datetime-local" value={startsAt} onChange={(event) => setStartsAt(event.target.value)} required /></label>
                <label>Ends<input type="datetime-local" value={endsAt} onChange={(event) => setEndsAt(event.target.value)} required /></label>
              </div>
              {kind === "soreness" && <label>How sore?
                <select value={impact} onChange={(event) => setImpact(event.target.value)}>
                  <option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option>
                </select>
              </label>}
              {error && <p className="notice error">{error}</p>}
              <button className="button button-dark button-wide" disabled={busy}>{busy ? <><span className="spinner" /> Finding a better fit…</> : "Replan my week"}</button>
            </form>
          </>
        ) : (
          <div className="proposal-view">
            <p className="eyebrow">{response.result.status === "proposed" ? "YOUR WEEK CHANGED" : "NO PLAN CHANGE"}</p>
            <h2 id="replan-title">{response.result.status === "proposed" ? "A plan with room for real life." : "Your current plan stays in place."}</h2>
            {response.result.status === "proposed" ? <>
            <div className="before-after">
              <div><span className="eyebrow">BEFORE</span>{before.map(({ day, session }) => <div className="compare-row" key={day}><span>{new Date(`${day}T12:00:00`).toLocaleDateString([], { weekday: "long" })}</span><strong>{sessionName(session)}</strong></div>)}</div>
              <div className="compare-divider" />
              <div><span className="eyebrow">AFTER</span>{after.map(({ day, session }) => <div className="compare-row" key={day}><span>{new Date(`${day}T12:00:00`).toLocaleDateString([], { weekday: "long" })}</span><strong>{sessionName(session)}</strong></div>)}</div>
            </div>
            <div className="why-card"><span className="eyebrow">WHY DID THIS CHANGE?</span><p>{response.explanation}</p></div>
            <SignalMoment type={response.signal_moment.type} message={response.signal_moment.message} />
            </> : <div className="why-card"><span className="eyebrow">WHAT HAPPENED?</span><p>{response.explanation}</p></div>}
            {error && <p className="notice error">{error}</p>}
            {feedbackMessage ? <p className="notice success">{feedbackMessage}</p> : response.result.status === "proposed" ? <div className="proposal-actions">
              <button className="button button-dark button-wide" onClick={() => decide("accept")} disabled={busy}>{busy ? "Saving…" : "Accept plan"}</button>
              <button className="button button-quiet button-wide" onClick={() => decide("reject")} disabled={busy}>Keep original</button>
            </div> : <button className="button button-quiet button-wide" onClick={onClose}>Back to your plan</button>}
            <p className="validation-note">{response.result.validation.valid ? "Checked against your schedule and plan constraints." : "No validated change was found."}</p>
          </div>
        )}
      </section>
    </div>
  );
}
