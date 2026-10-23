"""Workflow state, immutable views, and auditable runtime metadata."""

from __future__ import annotations

from abc import ABCMeta
from array import array
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Iterator, Mapping
from copy import copy, deepcopy
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from dis import get_instructions
from enum import Enum, Flag
from fractions import Fraction
from functools import partial
from re import Pattern
from types import (
    BuiltinFunctionType, CodeType, FunctionType, GetSetDescriptorType,
    MappingProxyType, MemberDescriptorType, MethodType, ModuleType,
)
from typing import Any, TypeVar
from weakref import ProxyTypes, ReferenceType
from zoneinfo import ZoneInfo

from pydantic import BaseModel


T = TypeVar("T")
_StateClassLayout = tuple[tuple[str, ...], tuple[MemberDescriptorType, ...]]

# Class declarations are shared runtime metadata, rather than instance data.
# Other inherited attributes, including private names, must still be checked.
_CLASS_METADATA = frozenset({
    "__annotations__", "__annotate__", "__classcell__", "__classdictcell__",
    "__dataclass_fields__", "__dataclass_params__", "__new__", "__orig_bases__",
    "__parameters__", "__slotnames__", "__slots__", "__type_params__",
})

# Enum's member tables, construction helpers, and Flag caches describe the
# class implementation. User-defined inherited data is checked separately.
_ENUM_CLASS_METADATA = frozenset({
    "_member_names_", "_member_map_", "_value2member_map_", "_hashable_values_",
    "_unhashable_values_", "_unhashable_values_map_", "_new_member_",
    "_member_type_", "_use_args_", "_value_repr_", "_boundary_",
    "_flag_mask_", "_singles_mask_", "_all_bits_", "_inverted_",
})


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
    """Detached point-in-time view passed to agents and routers.

    The mapping is read-only; nested values retain their original types but
    belong to this snapshot. Edits reach workflow state only through updates
    explicitly returned by an Agent.
    """

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
        # Share a memo across payloads and runtime records so aliases inside
        # the snapshot remain coherent without referring to live state.
        copied_values, metadata = _copy_state_data((dict(values), {
            "version": version, "status": status, "current_step": current_step,
            "iteration_count": iteration_count, "current_repair_round": current_repair_round,
            "stages": dict(stages or {}), "stage_history": stage_history,
            "agent_results": agent_results, "test_results": test_results,
            "execution_logs": execution_logs,
        }))
        self._values = MappingProxyType(copied_values)
        self.version = metadata["version"]
        self.status = metadata["status"]
        self.current_step = metadata["current_step"]
        self.iteration_count = metadata["iteration_count"]
        self.current_repair_round = metadata["current_repair_round"]
        self.stages = MappingProxyType(metadata["stages"])
        self.stage_history = metadata["stage_history"]
        self.agent_results = metadata["agent_results"]
        self.test_results = metadata["test_results"]
        self.execution_logs = metadata["execution_logs"]

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
        """Return an independent mutable copy of the snapshot values."""

        return _copy_state_data(dict(self._values))


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
        """Read an engine-owned value; agents must use ``snapshot()``."""

        return self._values.get(key, default)

    def require(self, key: str, expected_type: type[T] | None = None) -> T | Any:
        return self.snapshot().require(key, expected_type)

    def to_dict(self) -> dict[str, Any]:
        """Return an owner-side shallow mapping, not an isolated export."""

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
        self._apply_copied_updates(_copy_state_data(copied_updates), source=source)

    def _apply_copied_updates(self, copied_updates: dict[str, Any], *, source: str) -> None:
        """Commit already validated, detached values without copying again."""

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

    def commit_agent_result(
        self,
        *,
        agent: str,
        updates: Mapping[str, Any],
        output: Any,
        updated_keys: tuple[str, ...],
        next_step: str | None,
    ) -> StateView:
        """Prepare business values and history before committing either.

        Live values and retained history receive separate copies. All copying
        and validation finish before values, versions, or logs are changed.
        """

        if self._active_stage is None:
            raise RuntimeError("没有正在运行的阶段可记录 Agent 结果")
        proposed_updates = dict(updates)
        self._validate_updates(proposed_updates)
        copied_updates = _copy_state_data(proposed_updates)
        record = self._prepare_agent_result(
            agent=agent, updates=proposed_updates, output=output,
            updated_keys=updated_keys, next_step=next_step,
            state_version=self.version + bool(copied_updates),
        )
        # Stage the append-only changes on private containers. Even the view
        # used for routing must be copyable before publishing this commit.
        candidate = copy(self)
        candidate._values = dict(self._values)
        candidate._history = list(self._history)
        candidate._agent_results = list(self._agent_results)
        candidate._test_results = list(self._test_results)
        candidate._execution_logs = list(self._execution_logs)
        candidate._apply_copied_updates(copied_updates, source=self._active_stage)
        candidate._append_agent_result(record)
        routing_view = candidate.snapshot()

        self._values = candidate._values
        self.version = candidate.version
        self._history = candidate._history
        self._agent_results = candidate._agent_results
        self._test_results = candidate._test_results
        self._execution_logs = candidate._execution_logs
        return routing_view

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

        record = self._prepare_agent_result(
            agent=agent, updates=updates, output=output,
            updated_keys=updated_keys, next_step=next_step, state_version=self.version,
        )
        self._append_agent_result(record)

    def _prepare_agent_result(
        self,
        *,
        agent: str,
        updates: Mapping[str, Any],
        output: Any,
        updated_keys: tuple[str, ...],
        next_step: str | None,
        state_version: int,
    ) -> AgentResultRecord:
        if self._active_stage is None:
            raise RuntimeError("没有正在运行的阶段可记录 Agent 结果")
        record = _copy_state_data(AgentResultRecord(
            stage=self._active_stage,
            agent=agent,
            iteration=self.iteration_count,
            repair_round=self._active_repair_round,
            state_version=state_version,
            updates=dict(updates),
            output=output,
            updated_keys=updated_keys,
            next_step=next_step,
        ))
        return replace(record, updates=MappingProxyType(record.updates))

    def _append_agent_result(self, record: AgentResultRecord) -> None:
        self._agent_results.append(record)
        self.log(
            "agent.completed",
            f"Agent {record.agent!r} 执行完成",
            stage=record.stage,
            agent=record.agent,
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


def _copy_state_data(value: T, memo: dict[int, Any] | None = None) -> T:
    """Copy supported data graphs and reject shared mutable references.

    Mapping proxies retain their type, aliases, and cycles. Executable or
    opaque references that cannot be detached are rejected. Copy protocols
    must not return mutable originals or retain mutable source children.
    """

    if memo is None:
        memo = {}
    source_nodes, proxies = _inspect_state_data(value)
    pending_proxies = []
    for original in proxies:
        if id(original) not in memo:
            contents: dict[Any, Any] = {}
            memo[id(original)] = MappingProxyType(contents)
            pending_proxies.append((original, contents))
    for original, contents in pending_proxies:
        contents.update(deepcopy(dict(original), memo))
    copied = deepcopy(value, memo)
    copied_nodes, _ = _inspect_state_data(copied)
    if source_nodes.keys() & copied_nodes.keys():
        raise TypeError("Workflow 状态复制仍共享可变对象，无法保证快照隔离")
    return copied


def _inspect_state_data(value: Any) -> tuple[dict[int, Any], list[MappingProxyType]]:
    """Inspect data edges without calling callbacks or consuming iterators."""

    pending = [value]
    seen: set[int] = set()
    # Keep inspected nodes alive so temporary mapping projections cannot
    # reuse object IDs while the copied graph is being validated.
    mutable_nodes: dict[int, Any] = {}
    proxies: list[MappingProxyType] = []
    class_layouts: dict[int, _StateClassLayout] = {}
    enum_layouts: dict[int, _StateClassLayout] = {}
    shared_enums: dict[int, Enum] = {}
    while pending:
        current = pending.pop()
        if type(current) in ProxyTypes:
            raise TypeError("Workflow 状态不支持弱引用代理")
        if id(current) in shared_enums:
            continue
        if _is_shareable_state_value(current, enum_layouts=enum_layouts):
            # Inspect repeated immutable members once per graph, retaining
            # references so their IDs cannot be reused during validation.
            if issubclass(type(current), Enum):
                shared_enums[id(current)] = current
            continue
        if isinstance(current, ReferenceType):
            raise TypeError("Workflow 状态不支持 weakref.ref；请传入可复制的实际值")
        if isinstance(current, (FunctionType, type, ModuleType, CodeType)):
            raise TypeError(f"Workflow 状态不支持 {type(current).__name__} 引用")
        if isinstance(current, (Iterator, AsyncIterator)):
            raise TypeError("Workflow 状态不支持迭代器；请先转换为 list 或 tuple")
        if isinstance(current, BuiltinFunctionType):
            raise TypeError("Workflow 状态不支持内建绑定方法")
        if isinstance(current, Enum):
            raise TypeError("Workflow 状态不支持包含可变数据的 Enum")
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        mutable_nodes[identity] = current
        if isinstance(current, MethodType):
            if isinstance(current.__self__, type):
                raise TypeError("Workflow 状态不支持绑定到类的方法")
            pending.append(current.__self__)
            pending.extend(_function_state_data(current.__func__))
        elif isinstance(current, partial):
            pending.extend((current.args, current.keywords))
            pending.extend(_callable_state_data(current.func))
        elif callable(current):
            implementation = getattr(type(current), "__call__", None)
            if isinstance(implementation, FunctionType):
                pending.extend(_function_state_data(implementation))
            else:
                raise TypeError(f"Workflow 状态不支持无法检查内部引用的可调用对象 {type(current).__name__}")
        if isinstance(current, defaultdict):
            pending.extend(_callable_state_data(current.default_factory))
        if isinstance(current, MappingProxyType):
            proxies.append(current)
        if isinstance(current, Mapping):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, (list, tuple, set, frozenset, deque)):
            pending.extend(current)
        elif isinstance(current, slice):
            pending.extend((current.start, current.stop, current.step))
        elif isinstance(current, BaseException):
            pending.extend(_exception_state_data(current))
        elif isinstance(current, (datetime, time)):
            pending.append(current.tzinfo)
        elif type(current) is timezone:
            pending.extend((current.utcoffset(None), current.tzname(None)))
        elif type(current) is ZoneInfo:
            pending.append(current.key)
        elif type(current) is Pattern:
            pending.append(current.pattern)
        elif type(current) is Fraction:
            pending.extend((current.numerator, current.denominator))
        # Container subclasses can also retain references in attributes.
        attributes = None if isinstance(current, MethodType) else getattr(current, "__dict__", None)
        if attributes is not None:
            pending.append(attributes)
        supported_storage = isinstance(current, (
            Mapping, list, tuple, set, frozenset, deque, bytearray, array,
            MethodType, partial, slice, BaseException, datetime, time,
            timezone, ZoneInfo, Pattern, Fraction,
        ))
        if attributes is None and not supported_storage and type(current) is not object:
            if not any("__slots__" in cls.__dict__ for cls in type(current).__mro__):
                raise TypeError(f"Workflow 状态不支持无法检查内部数据的 {type(current).__name__}")
        # Reuse declarations only within this traversal. Read each instance's
        # actual values, and rebuild layouts for the next source/copy check.
        kind = type(current)
        if id(kind) not in class_layouts:
            class_layouts[id(kind)] = _state_class_layout(kind)
        inherited_names, slots = class_layouts[id(kind)]
        for name in inherited_names:
            if name not in (attributes or {}):
                pending.append(object.__getattribute__(current, name))
        for slot in slots:
            try:
                pending.append(slot.__get__(current, kind))
            except AttributeError:
                pass
    return mutable_nodes, proxies


def _exception_state_data(error: BaseException) -> Iterator[Any]:
    """Include native exception fields without evaluating Python properties."""

    metadata = {"__weakref__", "__class__"}
    for cls in type(error).__mro__:
        for name, descriptor in vars(cls).items():
            if name in metadata or not isinstance(descriptor, (MemberDescriptorType, GetSetDescriptorType)):
                continue
            try:
                # Check every declaring layer, including BaseException.args
                # and __dict__: a same-named subclass slot cannot replace the
                # native storage, and both may retain distinct source data.
                yield descriptor.__get__(error, type(error))
            except AttributeError:
                # Optional fields (for example characters_written) may be unset.
                pass


def _state_class_layout(
    kind: type, *, excluded_names: frozenset[str] = frozenset(), include_immutable: bool = False,
) -> _StateClassLayout:
    inherited_names = set()
    slots = []
    is_model = issubclass(kind, BaseModel)
    for cls in kind.__mro__:
        declares_slots = "__slots__" in cls.__dict__
        for name, member in cls.__dict__.items():
            # Slot declarations may be exhausted iterators. The descriptors
            # created by type retain the actual storage and Python's mangled
            # names; retain each declaring layer so shadowing cannot hide it.
            if declares_slots and isinstance(member, MemberDescriptorType) and member.__objclass__ is cls:
                if name not in {"__dict__", "__weakref__"}:
                    slots.append(member)
                continue
            if name in _CLASS_METADATA or name in excluded_names:
                continue
            if name == "_abc_impl" and isinstance(cls, ABCMeta):
                continue
            if is_model and (
                name in {"model_config", "__class_vars__", "__private_attributes__"}
                or name.startswith("__pydantic_")
            ):
                continue
            # Descriptors and method implementations are runtime metadata;
            # inherited data defaults are still reachable instance data.
            if isinstance(member, (type, FunctionType, classmethod, staticmethod)) or hasattr(member, "__get__"):
                continue
            if include_immutable or not _is_shareable_state_value(member):
                inherited_names.add(name)
    return tuple(inherited_names), tuple(slots)


def _callable_state_data(callback: Any) -> Iterator[Any]:
    if isinstance(callback, FunctionType):
        yield from _function_state_data(callback)
    elif isinstance(callback, type) and any(callback is factory for factory in (
        list, dict, set, tuple, frozenset, str, bytes, bytearray, int, float, bool,
    )):
        return
    else:
        yield callback


def _function_state_data(function: FunctionType) -> Iterator[Any]:
    """Expose captured data while treating function code as implementation."""

    if function.__defaults__:
        yield function.__defaults__
    if function.__kwdefaults__:
        yield from function.__kwdefaults__.values()
    if function.__dict__:
        yield function.__dict__
    for cell in function.__closure__ or ():
        try:
            yield cell.cell_contents
        except ValueError:
            pass
    codes = [function.__code__]
    names = set()
    while codes:
        code = codes.pop()
        codes.extend(item for item in code.co_consts if isinstance(item, CodeType))
        for instruction in get_instructions(code):
            name = instruction.argval
            if instruction.opname == "LOAD_GLOBAL" and name not in names and name in function.__globals__:
                names.add(name)
                yield function.__globals__[name]


def _is_shareable_state_value(
    value: Any, active: set[int] | None = None,
    enum_layouts: dict[int, _StateClassLayout] | None = None,
    enum_groups: set[int] | None = None,
) -> bool:
    """Recognize immutable data, rather than trusting deepcopy identity."""

    kind = type(value)
    if kind in (type(None), bool, int, float, complex, str, bytes, range,
                type(Ellipsis), type(NotImplemented), Decimal, timedelta, date):
        return True
    if kind in (datetime, time):
        children = (value.tzinfo,)
    elif kind is timezone:
        children = (value.utcoffset(None), value.tzname(None))
    elif kind is ZoneInfo:
        children = (value.key,)
    elif kind is Pattern:
        children = (value.pattern, value.flags)
    elif kind is Fraction:
        children = (value.numerator, value.denominator)
    elif kind is BuiltinFunctionType:
        return value.__self__ is None or issubclass(type(value.__self__), ModuleType)
    elif issubclass(kind, Enum):
        if enum_layouts is None:
            enum_layouts = {}
        if id(kind) not in enum_layouts:
            enum_layouts[id(kind)] = _state_class_layout(
                kind, excluded_names=_ENUM_CLASS_METADATA.union(kind.__members__), include_immutable=True,
            )
        inherited_names, slots = enum_layouts[id(kind)]
        # Other members are reachable through the same enum instance. Their
        # data must also be immutable; aliases are inspected only once.
        if enum_groups is None:
            enum_groups = set()
        members = {id(value): value}
        if id(kind) not in enum_groups:
            # Expand the sibling group once per recursion path. Nested enum
            # references still validate their own fields, including uncached
            # members, without repeatedly expanding Flag inversion cycles.
            members.update((id(member), member) for member in kind.__members__.values())
            if issubclass(kind, Flag):
                # Bitwise operations return cached pseudo-members that are
                # absent from __members__; their data must also be checked.
                members.update((id(member), member) for member in kind._value2member_map_.values())
        children = []
        for member in members.values():
            attributes = vars(member)
            children.extend(item for name, item in attributes.items() if name != "__objclass__")
            for name in inherited_names:
                if name not in attributes:
                    children.append(object.__getattribute__(member, name))
            for slot in slots:
                try:
                    children.append(slot.__get__(member, kind))
                except AttributeError:
                    pass
    elif kind in (tuple, frozenset):
        children = value
    else:
        return False
    if active is None:
        active = set()
    identity = id(value)
    if identity in active:
        # Flag caches and immutable enum peers can form cycles. Every other
        # edge is still checked by the surrounding traversal before sharing.
        return issubclass(kind, Enum)
    active.add(identity)
    group = id(kind) if issubclass(kind, Enum) and id(kind) not in enum_groups else None
    if group is not None:
        enum_groups.add(group)
    try:
        return all(_is_shareable_state_value(child, active, enum_layouts, enum_groups) for child in children)
    finally:
        if group is not None:
            enum_groups.remove(group)
        active.remove(identity)


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
