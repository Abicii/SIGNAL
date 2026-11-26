from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from service.models import ApplicationState, ReplanRequest
from signal_engine.models import Goal, GoalKind, Intensity, PlannedSession, Preference, RecentHistory, ScheduleWindow


PROFILE_TIMEZONE = "Asia/Calcutta"
ZONE = ZoneInfo(PROFILE_TIMEZONE)
WEEK_START = date(2026, 9, 28)
WEEK_END = date(2026, 10, 4)


def _session(logical_id: str, activity: str, day: date, start: time, end: time, intensity: Intensity = Intensity.MODERATE) -> PlannedSession:
    start_at = datetime.combine(day, start, tzinfo=ZONE)
    end_at = datetime.combine(day, end, tzinfo=ZONE)
    return PlannedSession(
        logical_session_id=logical_id,
        activity_type=activity,
        schedule_window=ScheduleWindow(start_at=start_at, end_at=end_at, timezone=PROFILE_TIMEZONE),
        intensity=intensity,
        duration_minutes=int((end_at - start_at).total_seconds() // 60),
    )


def canonical_plan() -> tuple[PlannedSession, ...]:
    return (
        _session("upper-01", "upper", date(2026, 9, 28), time(18), time(19)),
        _session("run-01", "run", date(2026, 9, 29), time(18), time(18, 45)),
        _session("lower-01", "lower_body", date(2026, 9, 30), time(18), time(19)),
        _session("run-02", "run", date(2026, 10, 2), time(18), time(18, 45)),
        _session("lower-02", "lower_body", date(2026, 10, 3), time(7), time(8)),
        _session("run-long-01", "long_run", date(2026, 10, 4), time(7), time(9)),
    )


def make_replan_request(plan=None, life_event=None) -> ReplanRequest:
    if plan is None:
        plan = canonical_plan()
    monday_window = ScheduleWindow(
        start_at=datetime(2026, 9, 28, 18, tzinfo=ZONE),
        end_at=datetime(2026, 9, 28, 19, tzinfo=ZONE),
        timezone=PROFILE_TIMEZONE,
    )
    thursday_window = ScheduleWindow(
        start_at=datetime(2026, 10, 1, 18, tzinfo=ZONE),
        end_at=datetime(2026, 10, 1, 19, tzinfo=ZONE),
        timezone=PROFILE_TIMEZONE,
    )
    state = ApplicationState(
        as_of=datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        timezone=PROFILE_TIMEZONE,
        horizon_start=WEEK_START,
        horizon_end=WEEK_END,
        recent_history=RecentHistory(),
        available_windows=(monday_window, thursday_window),
        must_preserve_session_ids=("run-long-01",),
    )
    return ReplanRequest(
        current_plan=tuple(plan),
        current_state=state,
        goals=(Goal(kind=GoalKind.STRENGTH, priority=5), Goal(kind=GoalKind.RUNNING, priority=3)),
        preferences=(Preference(),),
        life_event=life_event,
    )

