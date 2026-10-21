"""Unified interface implemented by workflow agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, final, runtime_checkable

from workflow.state import StateView


@dataclass(frozen=True)
class AgentResult:
    """The values and routing decision produced by an agent."""

    updates: Mapping[str, Any] = field(default_factory=dict)
    output: Any = None
    next_step: str | None = None


@runtime_checkable
class Agent(Protocol):
    """Structural interface accepted by :class:`WorkflowEngine`."""

    @property
    def name(self) -> str:
        ...

    def execute(self, state: StateView) -> AgentResult:
        ...


class BaseAgent(ABC):
    """Template-method base shared by all built-in workflow agents.

    Subclasses implement :meth:`run`; this class keeps the public
    ``execute(StateView) -> AgentResult`` contract consistent and validates
    both sides of the call. Third-party structural ``Agent`` implementations
    may still implement ``execute`` directly without inheriting this class.
    """

    name = "agent"

    @final
    def execute(self, state: StateView) -> AgentResult:
        """Validate the shared contract and delegate domain work to ``run``."""

        if not isinstance(state, StateView):
            raise TypeError(
                f"Agent {self.name!r} 只能接收 StateView，"
                f"实际为 {type(state).__name__}"
            )
        result = self.run(state)
        if not isinstance(result, AgentResult):
            raise TypeError(
                f"Agent {self.name!r} 必须返回 AgentResult，"
                f"实际返回 {type(result).__name__}"
            )
        if not isinstance(result.updates, Mapping):
            raise TypeError(f"Agent {self.name!r} 的 updates 必须是 Mapping")
        return result

    @abstractmethod
    def run(self, state: StateView) -> AgentResult:
        """Perform domain work using a validated, read-only state snapshot."""

