import type { PlanResponse } from "../../types/api";

type Props = { plan: PlanResponse | null; loading: boolean; error: string; onReplan: () => void };

export function Plan({ plan, loading, error, onReplan }: Props) {
  if (loading) return <p className="loading">Loading your week…</p>;
  if (error) return <p className="notice error">{error}</p>;
  if (!plan) return null;

  return (
    <main className="page-shell plan-page">
      <div className="page-heading">
        <div><p className="eyebrow">YOUR TRAINING, IN CONTEXT</p><h1>The week ahead.</h1><p className="subheading">{plan.week_start} — {plan.week_end}</p></div>
        <button className="button button-dark" onClick={onReplan}>Something changed? <span aria-hidden="true">↗</span></button>
      </div>
      <section className="week-list" aria-label="Seven-day training plan">
        {plan.days.map((day, index) => (
          <article className={`day-row ${day.completed ? "day-completed" : ""}`} key={day.date}>
            <span className="day-index">0{index + 1}</span>
            <div className="day-date"><strong>{day.weekday}</strong><span>{new Date(`${day.date}T12:00:00`).toLocaleDateString([], { month: "short", day: "numeric" })}</span></div>
            <div className={`activity-pill ${day.label === "Rest" ? "activity-rest" : ""}`}><span className="activity-indicator" />{day.label}</div>
            {day.completed && <span className="completed-mark" aria-label="Completed">✓</span>}
            {!day.completed && <span className="row-arrow" aria-hidden="true">↗</span>}
          </article>
        ))}
      </section>
      <p className="plan-footnote">Plans are a guide. Life gets a vote.</p>
    </main>
  );
}
