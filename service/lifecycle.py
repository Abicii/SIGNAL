from __future__ import annotations

from persistence import SignalRepository
from signal_engine.models import PlannedSession

from .models import ReplanRequest, ReplanResult
from .llm import LLMProvider, MockLLMProvider
from .replan import ReplanService


class ReplanApplicationService:
    """Coordinate domain replanning and persisted proposal lifecycle."""

    def __init__(
        self, repository: SignalRepository, *, provider: LLMProvider | None = None,
        adjudication_enabled: bool = True,
    ) -> None:
        self._repository = repository
        selected_provider = provider if provider is not None else (MockLLMProvider() if adjudication_enabled else None)
        self._replanner = ReplanService(provider=selected_provider)

    def current_plan(self) -> tuple[tuple[PlannedSession, ...], str, str]:
        return self._repository.current_plan()

    def proposal_pending(self) -> bool:
        return self._repository.proposal_pending()

    def replan(self, application_input: ReplanRequest, *, trigger_kind: str, parent_version_id: str) -> ReplanResult:
        result = self._replanner.replan(application_input)
        if result.status.value == "proposed":
            return self._repository.create_proposal(
                result,
                expected_parent_id=parent_version_id,
                trigger_kind=trigger_kind,
            )
        return self._repository.record_decision(
            result,
            parent_version_id=parent_version_id,
            trigger_kind=trigger_kind,
        )

    def decide(self, decision_id: str | None, action: str) -> tuple[str, tuple[PlannedSession, ...]]:
        return self._repository.decide_proposal(decision_id, action)
