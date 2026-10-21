"""Deterministic synchronous workflow execution engine."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from workflow.agent import Agent, AgentResult
from workflow.state import StateView, WorkflowState

if TYPE_CHECKING:
    from workflow.report import WorkflowReport


END = "__end__"
Router = Callable[[StateView, AgentResult], str | None]


@dataclass(frozen=True)
class WorkflowStep:
    name: str
    agent: Agent
    next_step: str = END
    router: Router | None = None


@dataclass(frozen=True)
class StepExecution:
    step: str
    agent: str
    state_version: int
    output: Any = None


@dataclass(frozen=True)
class WorkflowRun:
    state: WorkflowState
    executions: tuple[StepExecution, ...]

    @property
    def output(self) -> Any:
        """Return the last step output, or ``None`` for an empty run."""

        return self.executions[-1].output if self.executions else None

    @property
    def report(self) -> WorkflowReport:
        """Build the final structured report for this run."""

        from workflow.report import generate_workflow_report

        return generate_workflow_report(self.state)


class WorkflowError(RuntimeError):
    """Base error raised by workflow configuration or execution."""


class WorkflowConfigurationError(WorkflowError):
    """The workflow graph is incomplete or invalid."""


class WorkflowExecutionError(WorkflowError):
    """An agent or router failed while the workflow was running."""

    def __init__(self, message: str, *, state: WorkflowState) -> None:
        super().__init__(message)
        self.state = state


class WorkflowEngine:
    """Execute registered agents as a stateful directed workflow."""

    def __init__(self, name: str = "workflow", *, max_steps: int = 100) -> None:
        if max_steps < 1:
            raise ValueError("max_steps 必须大于 0")
        self.name = name
        self.max_steps = max_steps
        self._steps: dict[str, WorkflowStep] = {}
        self._entrypoint: str | None = None

    def add_step(
        self,
        name: str,
        agent: Agent,
        *,
        next_step: str = END,
        router: Router | None = None,
    ) -> WorkflowEngine:
        """Register a step and return the engine for fluent construction."""

        if not name or name == END:
            raise WorkflowConfigurationError("步骤名必须是非空字符串，且不能使用保留名 __end__")
        if name in self._steps:
            raise WorkflowConfigurationError(f"步骤已存在：{name}")
        if not isinstance(agent, Agent):
            raise WorkflowConfigurationError(f"步骤 {name!r} 未实现统一 Agent 接口")
        self._steps[name] = WorkflowStep(name, agent, next_step, router)
        if self._entrypoint is None:
            self._entrypoint = name
        return self

    def set_entrypoint(self, name: str) -> WorkflowEngine:
        if name not in self._steps:
            raise WorkflowConfigurationError(f"入口步骤不存在：{name}")
        self._entrypoint = name
        return self

    def run(
        self,
        initial_state: WorkflowState | Mapping[str, Any] | None = None,
    ) -> WorkflowRun:
        """Run until ``END`` and return final state plus execution records."""

        if self._entrypoint is None:
            raise WorkflowConfigurationError("Workflow 至少需要一个步骤")
        state = (
            initial_state
            if isinstance(initial_state, WorkflowState)
            else WorkflowState(initial_state)
        )
        state.register_stages({name: step.agent.name for name, step in self._steps.items()})
        state.start()
        executions: list[StepExecution] = []
        current = self._entrypoint

        while current != END:
            if len(executions) >= self.max_steps:
                error = WorkflowExecutionError(
                    f"Workflow 超过最大步骤数 {self.max_steps}，可能存在无限循环",
                    state=state,
                )
                state.fail(error)
                raise error

            step = self._steps.get(current)
            if step is None:
                error = WorkflowConfigurationError(f"路由目标步骤不存在：{current}")
                state.fail(error)
                raise error

            state.begin_step(
                step.name,
                agent=step.agent.name,
                repair_round=self._repair_round_for_step(state, step),
            )
            try:
                result = step.agent.execute(state.snapshot())
                if not isinstance(result, AgentResult):
                    raise TypeError(
                        f"Agent {step.agent.name!r} 必须返回 AgentResult，"
                        f"实际返回 {type(result).__name__}"
                    )
                state.apply(result.updates, source=step.name)
                state.record_agent_result(
                    agent=step.agent.name,
                    updates=result.updates,
                    output=result.output,
                    updated_keys=tuple(result.updates),
                    next_step=result.next_step,
                )
                next_step = self._resolve_next(step, state.snapshot(), result)
                self._record_route(state, step, next_step)
                state.complete_step()
                executions.append(
                    StepExecution(
                        step=step.name,
                        agent=step.agent.name,
                        state_version=state.version,
                        output=result.output,
                    )
                )
                current = next_step
            except WorkflowConfigurationError as exc:
                state.fail(exc)
                raise
            except Exception as exc:
                state.fail(exc)
                raise WorkflowExecutionError(
                    f"步骤 {step.name!r} 执行失败：{exc}",
                    state=state,
                ) from exc

        state.complete()
        return WorkflowRun(state=state, executions=tuple(executions))

    def _resolve_next(
        self,
        step: WorkflowStep,
        state: StateView,
        result: AgentResult,
    ) -> str:
        target = result.next_step
        if target is None and step.router is not None:
            target = step.router(state, result)
        if target is None:
            target = step.next_step
        if target != END and target not in self._steps:
            raise WorkflowConfigurationError(f"路由目标步骤不存在：{target}")
        return target

    def _repair_round_for_step(
        self,
        state: WorkflowState,
        step: WorkflowStep,
    ) -> int | None:
        """Return optional repair-round context for runtime records."""

        return None

    def _record_route(
        self,
        state: WorkflowState,
        step: WorkflowStep,
        next_step: str,
    ) -> None:
        """Record one resolved route while the stage context is still active."""

        state.log(
            "route.selected",
            f"阶段 {step.name!r} 路由到 {next_step!r}",
            stage=step.name,
            agent=step.agent.name,
        )
