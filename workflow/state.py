"""Workflow state, immutable views, and auditable runtime metadata."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, TypeVar


T = TypeVar("T")


class WorkflowStatus(str, Enum):
    """Lifecycle states for one workflow run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class StageStatus(str, Enum):
    """Lifecycle states for one registered workflow stage."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class StateChange:
    """One atomic set of values committed by a workflow step."""

    source: str
    version: int
    updated_keys: tuple[str, ...]


@dataclass(frozen=True)
class StageState:
    """Latest lifecycle snapshot for a stage.

    ``iteration`` is the workflow-wide execution number. A stage used by a
    loop therefore gets a new immutable record on every invocation.
    """

    name: str
    status: StageStatus = StageStatus.PENDING
    agent: str | None = None
    iteration: int = 0
    repair_round: int | None = None
    state_version: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.completed_at is None:
            return None
        return max(0.0, (self.completed_at - self.started_at).total_seconds())


@dataclass(frozen=True)
class AgentResultRecord:
    """Result metadata retained after one Agent invocation."""

    stage: str
    agent: str
    iteration: int
    state_version: int
    repair_round: int | None = None
    updates: Mapping[str, Any] = field(default_factory=dict)
    output: Any = None
    updated_keys: tuple[str, ...] = ()
    next_step: str | None = None


@dataclass(frozen=True)
class ExecutionLog:
    """One ordered workflow runtime event."""

    sequence: int
    timestamp: datetime
    level: str
    event: str
    message: str
    state_version: int
    stage: str | None = None
    agent: str | None = None
    iteration: int = 0
    repair_round: int | None = None


class StateView(Mapping[str, Any]):
    """Read-only point-in-time view passed to agents and routers."""

    def __init__(
        self,
        values: Mapping[str, Any],
        *,
        version: int,
        status: WorkflowStatus,
        current_step: str | None,
        iteration_count: int = 0,
        current_repair_round: int | None = None,
        stages: Mapping[str, StageState] | None = None,
        stage_history: tuple[StageState, ...] = (),
        agent_results: tuple[AgentResultRecord, ...] = (),
        test_results: tuple[Any, ...] = (),
        execution_logs: tuple[ExecutionLog, ...] = (),
    ) -> None:
        self._values = MappingProxyType(dict(values))
        self.version = version
        self.status = status
        self.current_step = current_step
        self.iteration_count = iteration_count
        self.current_repair_round = current_repair_round
        self.stages = MappingProxyType(dict(stages or {}))
        self.stage_history = stage_history
        self.agent_results = agent_results
        self.test_results = test_results
        self.execution_logs = execution_logs

    @property
    def stage_statuses(self) -> Mapping[str, StageStatus]:
        return MappingProxyType({name: stage.status for name, stage in self.stages.items()})

    @property
    def repair_history(self) -> tuple[Any, ...]:
        return _as_history(self._values.get("repair_history"))

    @property
    def latest_test_result(self) -> Any:
        return self.test_results[-1] if self.test_results else None

    @property
    def repair_iteration_count(self) -> int:
        value = self._values.get("fix_attempts", 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def require(self, key: str, expected_type: type[T] | None = None) -> T | Any:
        """Read a required value and optionally validate its runtime type."""

        if key not in self._values:
            raise KeyError(f"Workflow 状态缺少必需字段：{key}")
        value = self._values[key]
        if expected_type is not None and not isinstance(value, expected_type):
            raise TypeError(
                f"Workflow 状态字段 {key!r} 应为 {expected_type.__name__}，"
                f"实际为 {type(value).__name__}"
            )
        return value

    def to_dict(self) -> dict[str, Any]:
        """Return a shallow mutable copy of the snapshot values."""

        return dict(self._values)


class WorkflowState:
    """Mutable values and runtime metadata owned by the engine.

    Business values keep their original mapping API. Stage lifecycle, Agent
    outputs, test-result history, repair history, iteration count, and ordered
    execution logs are exposed separately so observability does not pollute
    Agent updates or alter state-version semantics.
    """

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        self._values = dict(values or {})
        self._validate_updates(self._values)
        self._history: list[StateChange] = []
        self._stages: dict[str, StageState] = {}
        self._stage_history: list[StageState] = []
        self._agent_results: list[AgentResultRecord] = []
        initial_test_result = self._values.get("test_result")
        self._test_results: list[Any] = (
            [] if initial_test_result is None else [initial_test_result]
        )
        self._execution_logs: list[ExecutionLog] = []
        self._active_stage: str | None = None
        self._active_repair_round: int | None = None
        self.status = WorkflowStatus.PENDING
        self.current_step: str | None = None
        self.error: str | None = None
        self.version = 0
        self.iteration_count = 0

    @property
    def history(self) -> tuple[StateChange, ...]:
        return tuple(self._history)

    @property
    def stages(self) -> Mapping[str, StageState]:
        """Return immutable latest snapshots indexed by stage name."""

        return MappingProxyType(dict(self._stages))

    @property
    def stage_statuses(self) -> Mapping[str, StageStatus]:
        return MappingProxyType({name: stage.status for name, stage in self._stages.items()})

    @property
    def stage_history(self) -> tuple[StageState, ...]:
        """Return every completed or failed stage invocation in order."""

        return tuple(self._stage_history)

    @property
    def agent_results(self) -> tuple[AgentResultRecord, ...]:
        return tuple(self._agent_results)

    @property
    def test_results(self) -> tuple[Any, ...]:
        """Return all non-null ``test_result`` values committed over the run."""

        return tuple(self._test_results)

    @property
    def latest_test_result(self) -> Any:
        return self._test_results[-1] if self._test_results else None

    @property
    def repair_history(self) -> tuple[Any, ...]:
        """Return repair records using the existing ``repair_history`` value."""

        return _as_history(self._values.get("repair_history"))

    @property
    def repair_iteration_count(self) -> int:
        """Return domain repair count while ``iteration_count`` stays generic."""

        value = self._values.get("fix_attempts", 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    @property
    def execution_logs(self) -> tuple[ExecutionLog, ...]:
        return tuple(self._execution_logs)

    def get(self, key: str, default: T | None = None) -> Any | T | None:
        return self._values.get(key, default)

    def require(self, key: str, expected_type: type[T] | None = None) -> T | Any:
        return self.snapshot().require(key, expected_type)

    def to_dict(self) -> dict[str, Any]:
        return dict(self._values)

    def snapshot(self) -> StateView:
        return StateView(
            self._values,
            version=self.version,
            status=self.status,
            current_step=self.current_step,
            iteration_count=self.iteration_count,
            current_repair_round=self._active_repair_round,
            stages=self._stages,
            stage_history=tuple(self._stage_history),
            agent_results=tuple(self._agent_results),
            test_results=tuple(self._test_results),
            execution_logs=tuple(self._execution_logs),
        )

    def register_stages(self, stages: Mapping[str, str]) -> None:
        """Register graph stages without changing business-state version."""

        if self.status is not WorkflowStatus.PENDING:
            raise RuntimeError("只能为尚未启动的 Workflow 注册阶段")
        for name, agent in stages.items():
            if not isinstance(name, str) or not name:
                raise ValueError("Workflow 阶段名必须是非空字符串")
            if name not in self._stages:
                self._stages[name] = StageState(name=name, agent=agent)

    def apply(self, updates: Mapping[str, Any], *, source: str) -> None:
        """Atomically merge updates and append one business-state change."""

        copied_updates = dict(updates)
        self._validate_updates(copied_updates)
        if not copied_updates:
            return
        self._values.update(copied_updates)
        self.version += 1
        self._history.append(
            StateChange(
                source=source,
                version=self.version,
                updated_keys=tuple(copied_updates),
            )
        )
        test_result = copied_updates.get("test_result")
        if test_result is not None:
            self._test_results.append(test_result)
        self.log(
            "state.updated",
            f"阶段 {source!r} 提交了 {len(copied_updates)} 个状态字段",
            stage=self.current_step or source,
        )

    def start(self) -> None:
        if self.status is not WorkflowStatus.PENDING:
            raise RuntimeError(f"Workflow 状态不能从 {self.status.value} 重新启动")
        self.status = WorkflowStatus.RUNNING
        self.error = None
        self.log("workflow.started", "Workflow 开始执行")

    def begin_step(
        self,
        step: str,
        *,
        agent: str | None = None,
        repair_round: int | None = None,
    ) -> None:
        if self.status is not WorkflowStatus.RUNNING:
            raise RuntimeError("只有运行中的 Workflow 才能开始步骤")
        if self._active_stage is not None:
            raise RuntimeError(f"阶段 {self._active_stage!r} 尚未结束")
        self.iteration_count += 1
        self.current_step = step
        self._active_stage = step
        self._active_repair_round = repair_round
        previous = self._stages.get(step)
        resolved_agent = agent if agent is not None else (previous.agent if previous else None)
        stage = StageState(
            name=step,
            status=StageStatus.RUNNING,
            agent=resolved_agent,
            iteration=self.iteration_count,
            repair_round=repair_round,
            state_version=self.version,
            started_at=_utc_now(),
        )
        self._stages[step] = stage
        self.log(
            "stage.started",
            f"阶段 {step!r} 开始执行",
            stage=step,
            agent=resolved_agent,
        )

    def record_agent_result(
        self,
        *,
        agent: str,
        updates: Mapping[str, Any],
        output: Any,
        updated_keys: tuple[str, ...],
        next_step: str | None,
    ) -> None:
        """Retain one successful Agent result without changing state version."""

        if self._active_stage is None:
            raise RuntimeError("没有正在运行的阶段可记录 Agent 结果")
        self._agent_results.append(
            AgentResultRecord(
                stage=self._active_stage,
                agent=agent,
                iteration=self.iteration_count,
                repair_round=self._active_repair_round,
                state_version=self.version,
                updates=MappingProxyType(dict(updates)),
                output=output,
                updated_keys=updated_keys,
                next_step=next_step,
            )
        )
        self.log(
            "agent.completed",
            f"Agent {agent!r} 执行完成",
            stage=self._active_stage,
            agent=agent,
        )

    def complete_step(self) -> None:
        if self._active_stage is None:
            raise RuntimeError("没有正在运行的阶段可完成")
        stage = self._stages[self._active_stage]
        completed = replace(
            stage,
            status=StageStatus.COMPLETED,
            state_version=self.version,
            completed_at=_utc_now(),
        )
        self._stages[stage.name] = completed
        self._stage_history.append(completed)
        self.log(
            "stage.completed",
            f"阶段 {stage.name!r} 执行完成",
            stage=stage.name,
            agent=stage.agent,
        )
        self._active_stage = None
        self._active_repair_round = None

    def complete(self) -> None:
        if self._active_stage is not None:
            raise RuntimeError(f"阶段 {self._active_stage!r} 尚未结束")
        self.status = WorkflowStatus.COMPLETED
        self.current_step = None
        self.error = None
        self.log("workflow.completed", "Workflow 执行完成")

    def fail(self, error: BaseException | str) -> None:
        message = str(error)
        if self._active_stage is not None:
            stage = self._stages[self._active_stage]
            failed = replace(
                stage,
                status=StageStatus.FAILED,
                state_version=self.version,
                completed_at=_utc_now(),
                error=message,
            )
            self._stages[stage.name] = failed
            self._stage_history.append(failed)
            self.log(
                "stage.failed",
                f"阶段 {stage.name!r} 执行失败：{message}",
                level="error",
                stage=stage.name,
                agent=stage.agent,
            )
            self._active_stage = None
            self._active_repair_round = None
        self.status = WorkflowStatus.FAILED
        self.error = message
        self.log("workflow.failed", f"Workflow 执行失败：{message}", level="error")

    def log(
        self,
        event: str,
        message: str,
        *,
        level: str = "info",
        stage: str | None = None,
        agent: str | None = None,
        repair_round: int | None = None,
    ) -> None:
        """Append a structured, ordered runtime event."""

        if not event or not message:
            raise ValueError("执行日志的 event 和 message 不能为空")
        self._execution_logs.append(
            ExecutionLog(
                sequence=len(self._execution_logs) + 1,
                timestamp=_utc_now(),
                level=level,
                event=event,
                message=message,
                state_version=self.version,
                stage=stage,
                agent=agent,
                iteration=self.iteration_count,
                repair_round=(
                    self._active_repair_round
                    if repair_round is None
                    else repair_round
                ),
            )
        )

    def logs_for_round(self, repair_round: int) -> tuple[ExecutionLog, ...]:
        """Return ordered events for one repair round (baseline is round 0)."""

        if isinstance(repair_round, bool) or not isinstance(repair_round, int):
            raise TypeError("repair_round 必须是整数")
        if repair_round < 0:
            raise ValueError("repair_round 不能小于 0")
        return tuple(
            item for item in self._execution_logs
            if item.repair_round == repair_round
        )

    @staticmethod
    def _validate_updates(updates: Mapping[str, Any]) -> None:
        invalid_keys = [key for key in updates if not isinstance(key, str) or not key]
        if invalid_keys:
            raise ValueError("Workflow 状态字段名必须是非空字符串")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_history(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return (value,)
