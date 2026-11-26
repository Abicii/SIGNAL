from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import StrEnum
import os
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

load_dotenv()

from service.models import ReplanResult, ReplanStatus
from persistence import PersistenceError, ProposalNotFound, SignalRepository, StaleProposal
from service.lifecycle import ReplanApplicationService
from service.gemini import GeminiLLMProvider
from service.llm import MockLLMProvider
from signal_engine.models import LifeEvent, PhysiologyObservation, PlannedSession, RecentHistory

from .sample_data import WEEK_END, WEEK_START, ZONE, make_replan_request


class EventKind(StrEnum):
    LATE_NIGHT = "late_night"
    SOCIAL = "social"
    WORK = "work"
    TRAVEL = "travel"
    POOR_SLEEP = "poor_sleep"
    SORENESS = "soreness"
    MISSED_WORKOUT = "missed_workout"


class ReplanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EventKind
    starts_at: datetime
    ends_at: datetime
    impact: Literal["low", "medium", "high"] = "medium"


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["accept", "reject"]
    decision_id: str | None = None


class SignalMoment(BaseModel):
    type: str
    message: str


class ReplanResponse(BaseModel):
    result: ReplanResult
    explanation: str
    signal_moment: SignalMoment
    trigger: ReplanBody


class FeedbackResponse(BaseModel):
    action: str
    status: str
    current_plan: list[dict]


try:
    repository: SignalRepository | None = SignalRepository.from_environment()
except PersistenceError:
    repository = None
_completed_session_ids = {"upper-01", "run-01"}


@asynccontextmanager
async def _lifespan(_app):
    if repository is None:
        raise RuntimeError("Set DATABASE_URL to a PostgreSQL database before starting SIGNAL")
    repository.initialize()
    yield


app = FastAPI(title="SIGNAL API", version="0.1.0", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["content-type"],
)


def _repository() -> SignalRepository:
    if repository is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    return repository


def _application_service() -> ReplanApplicationService:
    provider_mode = os.environ.get("SIGNAL_LLM_PROVIDER", "mock").lower()
    if provider_mode not in {"mock", "disabled"}:
        if provider_mode != "gemini":
            raise HTTPException(status_code=503, detail="Unsupported SIGNAL_LLM_PROVIDER")
        return ReplanApplicationService(
            _repository(),
            provider=GeminiLLMProvider(os.environ.get("GEMINI_API_KEY")),
        )
    if provider_mode == "mock":
        return ReplanApplicationService(_repository(), provider=MockLLMProvider())
    return ReplanApplicationService(
        _repository(),
        adjudication_enabled=False,
    )


def _session_label(session: PlannedSession) -> str:
    return {
        "upper": "Upper",
        "lower_body": "Lower",
        "run": "Run",
        "long_run": "Long Run",
        "recovery": "Recovery",
    }.get(session.activity_type.lower(), session.activity_type.replace("_", " ").title())


def _plan_days(plan: tuple[PlannedSession, ...]) -> list[dict]:
    by_day = {session.local_date: session for session in plan}
    result = []
    day = WEEK_START
    while day <= WEEK_END:
        session = by_day.get(day)
        result.append({
            "date": day.isoformat(),
            "weekday": day.strftime("%A"),
            "label": _session_label(session) if session else "Rest",
            "session": session.model_dump(mode="json") if session else None,
            "completed": bool(session and session.logical_session_id in _completed_session_ids),
        })
        from datetime import timedelta
        day += timedelta(days=1)
    return result


def _day(session: PlannedSession) -> str:
    return session.local_date.strftime("%A")


def _explanation(result: ReplanResult) -> str:
    if result.status != ReplanStatus.PROPOSED:
        return "No safe plan change was produced. Your current plan is unchanged."
    moved = [change for change in result.changes if change.change_type == "moved"]
    added = [change for change in result.changes if change.change_type == "added"]
    if "REDUCE_RECOVERY_CONFLICT" in result.reason_codes:
        pieces = []
        for change in moved:
            if change.before and change.after:
                pieces.append(f"{_session_label(change.before)} moved from {_day(change.before)} to {_day(change.after)}")
        if added:
            pieces.append("a recovery session was added")
        if pieces:
            return " and ".join(pieces) + " to protect the recovery window after your event."
        return "The plan changed to reduce a conflict with your recovery window."
    if "HANDLE_TIME_CONFLICT" in result.reason_codes and moved:
        return "A session moved to fit the time conflict in your updated schedule."
    if moved:
        return "A session moved to fit your updated schedule."
    if not result.changes:
        return "Your plan still fits the information provided."
    return "The plan was adjusted to fit the change you shared."


def _moment(result: ReplanResult, trigger: ReplanBody) -> SignalMoment:
    recovery_change = "REDUCE_RECOVERY_CONFLICT" in result.reason_codes and any(
        change.change_type == "added" and change.after and change.after.activity_type.lower() == "recovery"
        for change in result.changes
    )
    if recovery_change and trigger.kind in {EventKind.LATE_NIGHT, EventKind.SOCIAL}:
        return SignalMoment(type="recovery_after_late_night", message="You survived last night. Let’s not pretend today is leg day.")
    if result.status == ReplanStatus.PROPOSED and result.changes:
        return SignalMoment(type="schedule_adjustment", message="Your plan made room for the change you shared.")
    return SignalMoment(type="steady_plan", message="A steady week beats a perfect one.")


def _localize(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=ZONE)
    return value.astimezone(ZONE)


def _replan(body: ReplanBody) -> ReplanResponse:
    if body.ends_at <= body.starts_at:
        raise HTTPException(status_code=422, detail="End time must be after start time.")
    starts_at, ends_at = _localize(body.starts_at), _localize(body.ends_at)
    is_observation = body.kind in {EventKind.POOR_SLEEP, EventKind.SORENESS}
    application = _application_service()
    try:
        current_plan, parent_version_id, profile_timezone = application.current_plan()
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    life_event = None if is_observation else LifeEvent(
        kind=body.kind.value,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=profile_timezone,
    )
    request = make_replan_request(plan=current_plan, life_event=life_event)
    state = request.current_state.model_copy(update={"as_of": datetime.now(timezone.utc)})
    if body.kind == EventKind.POOR_SLEEP:
        observation = PhysiologyObservation(
            metric="sleep_duration",
            value=(ends_at - starts_at).total_seconds() / 3600,
            unit="h",
            observed_at=ends_at,
            quality="self_reported",
        )
        history = state.recent_history.model_copy(update={"physiology": state.recent_history.physiology + (observation,)})
        state = state.model_copy(update={"recent_history": history})
    elif body.kind == EventKind.SORENESS:
        # The form offers only low/medium/high; map those explicit choices to
        # conservative points on the engine's existing 0–10 soreness scale.
        value = {"low": 2.0, "medium": 5.0, "high": 8.0}[body.impact]
        observation = PhysiologyObservation(
            metric="soreness", value=value, unit="0-10", observed_at=ends_at, quality="self_reported",
        )
        history = state.recent_history.model_copy(update={"physiology": state.recent_history.physiology + (observation,)})
        state = state.model_copy(update={"recent_history": history})
    request = request.model_copy(update={"current_state": state})
    trigger_kind = "life_event" if life_event is not None else "other"
    try:
        result = application.replan(request, trigger_kind=trigger_kind, parent_version_id=parent_version_id)
    except StaleProposal as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(status_code=500, detail="Unable to persist replan decision") from exc
    return ReplanResponse(result=result, explanation=_explanation(result), signal_moment=_moment(result, body), trigger=body)


@app.get("/api/v1/today")
def today() -> dict:
    today_date = datetime.now(ZONE).date()
    current_plan, _, _ = _application_service().current_plan()
    current = next((session for session in current_plan if session.local_date == today_date), None)
    planned_count = sum(1 for session in current_plan if session.intensity.value not in {"rest", "recovery"})
    return {
        "date": today_date.isoformat(),
        "weekday": today_date.strftime("%A"),
        "session": ({"label": _session_label(current), "session": current.model_dump(mode="json")} if current else None),
        "recovery": {"sleep_hours": 7.3, "sleep_label": "7h 18m", "summary": "A useful signal, alongside how you feel."},
        "signal_message": "Keep today simple. One good session is enough.",
        "weekly_progress": {"completed": len(_completed_session_ids), "total": planned_count},
    }


@app.get("/api/v1/plan")
def plan() -> dict:
    application = _application_service()
    current_plan, _, profile_timezone = application.current_plan()
    return {
        "timezone": profile_timezone,
        "week_start": WEEK_START.isoformat(),
        "week_end": WEEK_END.isoformat(),
        "days": _plan_days(current_plan),
        "proposal_pending": application.proposal_pending(),
    }


@app.post("/api/v1/replan", response_model=ReplanResponse)
def create_replan(body: ReplanBody) -> ReplanResponse:
    return _replan(body)


@app.post("/api/v1/feedback", response_model=FeedbackResponse)
def feedback(body: FeedbackBody) -> FeedbackResponse:
    try:
        status, current_plan = _application_service().decide(body.decision_id, body.action)
    except StaleProposal as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProposalNotFound as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(status_code=500, detail="Unable to save proposal decision") from exc
    return FeedbackResponse(action=body.action, status=status, current_plan=[session.model_dump(mode="json") for session in current_plan])
