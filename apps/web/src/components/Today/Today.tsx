import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { TodayResponse } from "../../types/api";

type Props = { onReplan: () => void };

export function Today({ onReplan }: Props) {
  const [data, setData] = useState<TodayResponse | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.today().then(setData).catch((issue: Error) => setError(issue.message));
  }, []);

  if (error) return <p className="notice error">{error}</p>;
  if (!data) return <p className="loading">Getting today ready…</p>;

  const progress = data.weekly_progress.total
    ? Math.round((data.weekly_progress.completed / data.weekly_progress.total) * 100)
    : 0;

  return (
    <main className="page-shell today-page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">{data.weekday.toUpperCase()} · {data.date}</p>
          <h1>Make today count.</h1>
          <p className="subheading">A plan that moves with your life.</p>
        </div>
        <button className="button button-dark" onClick={onReplan}>Something changed? <span aria-hidden="true">↗</span></button>
      </div>

      <section className="today-grid">
        <article className="card session-card">
          <div className="card-topline"><span className="eyebrow">TODAY’S SESSION</span><span className="status-dot">Planned</span></div>
          {data.session ? (
            <>
              <h2>{data.session.label}</h2>
              <p className="muted">{data.session.session.duration_minutes} minutes · {data.session.session.intensity}</p>
              <div className="session-time">
                {new Date(data.session.session.schedule_window.start_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}
                <span className="time-line" />
                {new Date(data.session.session.schedule_window.end_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}
              </div>
            </>
          ) : <><h2>A rest day</h2><p className="muted">Make room to recover.</p></>}
        </article>

        <article className="card recovery-card">
          <span className="eyebrow">RECOVERY CHECK-IN</span>
          <p className="sleep-number">{data.recovery.sleep_label}<span> sleep</span></p>
          <p className="muted">{data.recovery.summary}</p>
          <div className="recovery-rule"><span /></div>
          <p className="micro-copy">One signal, not the whole story.</p>
        </article>
      </section>

      <section className="card week-progress-card">
        <div className="progress-copy"><div><span className="eyebrow">THIS WEEK</span><p>{data.weekly_progress.completed} of {data.weekly_progress.total} sessions complete</p></div><strong>{progress}%</strong></div>
        <div className="progress-track"><span style={{ width: `${progress}%` }} /></div>
        <p className="daily-note">{data.signal_message}</p>
      </section>
    </main>
  );
}
