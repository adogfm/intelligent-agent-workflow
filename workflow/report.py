"""Final, serializable reports built from terminal workflow state."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any

from workflow.state import (
    AgentResultRecord,
    ExecutionLog,
    StageState,
    StageStatus,
    WorkflowState,
    WorkflowStatus,
)


class WorkflowOutcome(str, Enum):
    """Business outcome derived from lifecycle, repair, and test evidence."""

    SUCCEEDED = "succeeded"
    UNRESOLVED = "unresolved"
    FAILED = "failed"


@dataclass(frozen=True)
class StageReport:
    """Aggregate plus immutable invocation records for one workflow stage."""

    name: str
    agent: str | None
    status: StageStatus
    executions: int
    state_version: int
    first_started_at: datetime | None
    last_completed_at: datetime | None
    duration_seconds: float
    error: str | None
    records: tuple[StageState, ...]


@dataclass(frozen=True)
class WorkflowReport:
    """Final report assembled without mutating the source state."""

    generated_at: datetime
    workflow_status: WorkflowStatus
    outcome: WorkflowOutcome
    succeeded: bool
    summary: str
    state_version: int
    iteration_count: int
    repair_iteration_count: int
    started_at: datetime | None
    completed_at: datetime | None
    duration_seconds: float | None
    error: str | None
    stages: tuple[StageReport, ...]
    stage_records: tuple[StageState, ...]
    agent_results: tuple[AgentResultRecord, ...]
    repair_history: tuple[Any, ...]
    test_results: tuple[Any, ...]
    final_review_result: Any
    final_test_result: Any
    execution_logs: tuple[ExecutionLog, ...]

    def logs_for_round(self, repair_round: int) -> tuple[ExecutionLog, ...]:
        """Return report events belonging to one repair round."""

        _validate_round(repair_round)
        return tuple(
            item for item in self.execution_logs
            if item.repair_round == repair_round
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation of the complete report."""

        return _serialize(self)


def generate_workflow_report(state: WorkflowState) -> WorkflowReport:
    """Build a final report from a completed or failed ``WorkflowState``."""

    if not isinstance(state, WorkflowState):
        raise TypeError("state 必须是 WorkflowState")
    if state.status not in {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED}:
        raise ValueError("只能为已完成或失败的 Workflow 生成最终报告")

    logs = state.execution_logs
    started_at = _event_time(logs, "workflow.started", first=True)
    completed_at = (
        _event_time(logs, "workflow.completed")
        or _event_time(logs, "workflow.failed")
    )
    duration = None
    if started_at is not None and completed_at is not None:
        duration = max(0.0, (completed_at - started_at).total_seconds())

    outcome = _derive_outcome(state)
    test_results = tuple(_safe_copy(item) for item in state.test_results)
    final_test_result = test_results[-1] if test_results else None
    agent_results = tuple(
        replace(
            item,
            updates=MappingProxyType(_safe_copy(dict(item.updates))),
            output=_safe_copy(item.output),
        )
        for item in state.agent_results
    )
    return WorkflowReport(
        generated_at=datetime.now(timezone.utc),
        workflow_status=state.status,
        outcome=outcome,
        succeeded=outcome is WorkflowOutcome.SUCCEEDED,
        summary=_summary(state, outcome, final_test_result),
        state_version=state.version,
        iteration_count=state.iteration_count,
        repair_iteration_count=state.repair_iteration_count,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration,
        error=state.error,
        stages=_stage_reports(state),
        stage_records=state.stage_history,
        agent_results=agent_results,
        repair_history=_safe_copy(state.repair_history),
        test_results=test_results,
        final_review_result=_safe_copy(state.get("report")),
        final_test_result=final_test_result,
        execution_logs=logs,
    )


def _stage_reports(state: WorkflowState) -> tuple[StageReport, ...]:
    reports: list[StageReport] = []
    for name, latest in state.stages.items():
        records = tuple(item for item in state.stage_history if item.name == name)
        first_started = records[0].started_at if records else latest.started_at
        last_completed = records[-1].completed_at if records else latest.completed_at
        duration = sum(item.duration_seconds or 0.0 for item in records)
        reports.append(StageReport(
            name=name,
            agent=latest.agent,
            status=latest.status,
            executions=len(records),
            state_version=latest.state_version,
            first_started_at=first_started,
            last_completed_at=last_completed,
            duration_seconds=duration,
            error=latest.error,
            records=records,
        ))
    return tuple(reports)


def _derive_outcome(state: WorkflowState) -> WorkflowOutcome:
    if state.status is WorkflowStatus.FAILED:
        return WorkflowOutcome.FAILED
    if state.get("repair_succeeded") is False:
        return WorkflowOutcome.UNRESOLVED
    final_review = state.get("report")
    if final_review is not None and getattr(final_review, "issues", ()):
        return WorkflowOutcome.UNRESOLVED
    final_test = state.latest_test_result
    if final_test is not None and not _test_succeeded(final_test):
        return WorkflowOutcome.UNRESOLVED
    return WorkflowOutcome.SUCCEEDED


def _summary(
    state: WorkflowState,
    outcome: WorkflowOutcome,
    final_test_result: Any,
) -> str:
    if outcome is WorkflowOutcome.FAILED:
        return f"Workflow 执行失败：{state.error or '未知错误'}"
    if outcome is WorkflowOutcome.UNRESOLVED:
        reason = state.get("repair_stop_reason")
        if reason:
            return f"Workflow 已结束，但问题未解决：{reason}"
        review = state.get("report")
        issues = getattr(review, "issues", ()) if review is not None else ()
        if issues:
            return f"Workflow 已结束，但仍有 {len(issues)} 个 Review 问题。"
        status = getattr(final_test_result, "status", "unknown")
        return f"Workflow 已结束，但最终测试状态为：{status}"
    return (
        f"Workflow 执行成功，共执行 {state.iteration_count} 个阶段，"
        f"修复 {state.repair_iteration_count} 轮。"
    )


def _event_time(
    logs: tuple[ExecutionLog, ...],
    event: str,
    *,
    first: bool = False,
) -> datetime | None:
    matches = (item.timestamp for item in logs if item.event == event)
    values = tuple(matches)
    if not values:
        return None
    return values[0] if first else values[-1]


def _validate_round(repair_round: int) -> None:
    if isinstance(repair_round, bool) or not isinstance(repair_round, int):
        raise TypeError("repair_round 必须是整数")
    if repair_round < 0:
        raise ValueError("repair_round 不能小于 0")


def _test_succeeded(result: Any) -> bool:
    if getattr(result, "status", None) != "passed":
        return False
    tests_run = getattr(result, "tests_run", None)
    passed = getattr(result, "passed", None)
    if tests_run is not None and tests_run <= 0:
        return False
    if passed is not None and passed <= 0:
        return False
    return not any(
        getattr(result, name, 0)
        for name in ("failed", "errors", "unexpected_successes")
    )


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _serialize(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_serialize(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _serialize(model_dump(mode="json"))
    return repr(value)


def _safe_copy(value: Any) -> Any:
    try:
        return deepcopy(value)
    except Exception:
        # Reporting must remain available for arbitrary third-party Agent
        # outputs, including handles that cannot be deep-copied.
        return value
