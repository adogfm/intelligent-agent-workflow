"""Final, serializable reports built from terminal workflow state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, TypeVar

from workflow.state import (
    AgentResultRecord,
    ExecutionLog,
    StageState,
    StageStatus,
    WorkflowState,
    WorkflowStatus,
    _copy_state_data,
)


RecordT = TypeVar("RecordT", AgentResultRecord, StageState, ExecutionLog)


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
        """Return JSON-compatible data, replacing cycles with diagnostics."""

        return _serialize(self)


def generate_workflow_report(state: WorkflowState) -> WorkflowReport:
    """Build a final report from a completed or failed ``WorkflowState``."""

    if not isinstance(state, WorkflowState):
        raise TypeError("state 必须是 WorkflowState")
    if state.status not in {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED}:
        raise ValueError("只能为已完成或失败的 Workflow 生成最终报告")

    logs = tuple(_copy_report_record(item) for item in state.execution_logs)
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
    agent_results = tuple(_copy_report_record(item) for item in state.agent_results)
    stage_records = tuple(_copy_report_record(item) for item in state.stage_history)
    latest_stages = tuple(_copy_report_record(item) for item in state.stages.values())
    return WorkflowReport(
        generated_at=datetime.now(timezone.utc),
        workflow_status=_safe_copy(state.status),
        outcome=outcome,
        succeeded=outcome is WorkflowOutcome.SUCCEEDED,
        summary=_summary(state, outcome, final_test_result),
        state_version=_safe_copy(state.version),
        iteration_count=_safe_copy(state.iteration_count),
        repair_iteration_count=_safe_copy(state.repair_iteration_count),
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration,
        error=_safe_copy(state.error),
        stages=_stage_reports(latest_stages, stage_records),
        stage_records=stage_records,
        agent_results=agent_results,
        repair_history=tuple(_safe_copy(item) for item in state.repair_history),
        test_results=test_results,
        final_review_result=_safe_copy(state.get("report")),
        final_test_result=final_test_result,
        execution_logs=logs,
    )


def _stage_reports(
    stages: tuple[StageState, ...], stage_history: tuple[StageState, ...],
) -> tuple[StageReport, ...]:
    reports: list[StageReport] = []
    for latest in stages:
        name = latest.name
        records = tuple(item for item in stage_history if item.name == name)
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


def _copy_report_record(record: RecordT) -> RecordT:
    """Detach complete records, retaining structured failure diagnostics."""

    try:
        # Copy together so update keys, updated_keys, and output keep aliases.
        return _copy_state_data(record)
    except Exception:
        # An unsafe field must not make the rest of a failure report unusable.
        values = {}
        for item in fields(record):
            value = getattr(record, item.name)
            if isinstance(record, AgentResultRecord) and item.name == "updates":
                values[item.name] = MappingProxyType({
                    _safe_copy(key): _safe_copy(item) for key, item in value.items()
                })
            elif isinstance(record, AgentResultRecord) and item.name == "updated_keys":
                values[item.name] = tuple(_safe_copy(key) for key in value)
            else:
                values[item.name] = _safe_copy(value)
        return replace(record, **values)


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


def _serialize(value: Any, active: set[int] | None = None) -> Any:
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if active is None:
        active = set()
    identity = id(value)
    if identity in active:
        return f"<circular reference: {type(value).__name__}>"
    active.add(identity)
    try:
        if is_dataclass(value) and not isinstance(value, type):
            return {
                item.name: _serialize(getattr(value, item.name), active)
                for item in fields(value)
            }
        if isinstance(value, Mapping):
            return {str(key): _serialize(item, active) for key, item in value.items()}
        if isinstance(value, (tuple, list, set, frozenset)):
            return [_serialize(item, active) for item in value]
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                payload = model_dump(mode="json")
            except (ValueError, RecursionError):
                # Model serializers may reject cycles before we can walk them.
                return f"<unserializable {type(value).__name__}>"
            return _serialize(payload, active)
        return _safe_repr(value)
    finally:
        # A shared value in another branch is not a circular reference.
        active.remove(identity)


def _safe_copy(value: Any) -> Any:
    try:
        return _copy_state_data(value)
    except Exception:
        # Keep failure reports available without exposing mutable originals.
        return _safe_repr(value)


def _safe_repr(value: Any) -> str:
    try:
        # repr may return a str subclass with references in instance data.
        # Call str's implementation directly to bypass an overridden __str__.
        return str.__str__(repr(value))
    except Exception:
        return f"<uncopyable {type(value).__name__}>"
