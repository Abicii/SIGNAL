import { useEffect, useState } from "react";
import { api } from "./api/client";
import { Plan } from "./components/Plan/Plan";
import { Replan } from "./components/Replan/Replan";
import { Today } from "./components/Today/Today";
import type { PlanResponse } from "./types/api";

type Page = "today" | "plan";

export default function App() {
  const [page, setPage] = useState<Page>("today");
  const [showReplan, setShowReplan] = useState(false);
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [planLoading, setPlanLoading] = useState(true);
  const [planError, setPlanError] = useState("");

  async function loadPlan() {
    setPlanLoading(true);
    try {
      setPlan(await api.plan());
      setPlanError("");
    } catch (issue) {
      setPlanError(issue instanceof Error ? issue.message : "Unable to load your plan.");
    } finally {
      setPlanLoading(false);
    }
  }

  useEffect(() => { void loadPlan(); }, []);

  return (
    <div className="app-frame">
      <header className="app-header">
        <button className="brand" onClick={() => setPage("today")} aria-label="SIGNAL home"><span className="brand-symbol">s</span><span>SIGNAL</span></button>
        <nav aria-label="Main navigation">
          <span className="sr-only">Primary pages</span>
          <button className={page === "today" ? "nav-link active" : "nav-link"} onClick={() => setPage("today")}>Today</button>
          <button className={page === "plan" ? "nav-link active" : "nav-link"} onClick={() => setPage("plan")}>Plan</button>
        </nav>
        <div className="header-note"><span className="online-dot" /> YOUR PLAN, IN MOTION</div>
      </header>
      {page === "today" ? <Today onReplan={() => setShowReplan(true)} /> : <Plan plan={plan} loading={planLoading} error={planError} onReplan={() => setShowReplan(true)} />}
      <footer className="app-footer"><span>TRAIN WITH YOUR LIFE, NOT AGAINST IT.</span><span>Asia / Kolkata</span></footer>
      {showReplan && <Replan onClose={() => setShowReplan(false)} onPlanChanged={(nextPlan) => { setPlan(nextPlan); setPage("plan"); void loadPlan(); }} />}
    </div>
  );
}
