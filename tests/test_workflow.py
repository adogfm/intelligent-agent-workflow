"""Offline tests for the reusable workflow framework."""

from __future__ import annotations

import unittest

from agent import CodeReviewAgent, ReviewAgent, create_review_workflow
from coding_agent import CodingAgent
from fix_agent import FixAgent
from llm import MockReviewer
from test_agent import TestAgent
from workflow import (
    END,
    AgentResult,
    BaseAgent,
    StageStatus,
    StateView,
    WorkflowEngine,
    WorkflowExecutionError,
    WorkflowOutcome,
    WorkflowState,
    WorkflowStatus,
    generate_workflow_report,
)


class FunctionAgent(BaseAgent):
    def __init__(self, name: str, function) -> None:
        self.name = name
        self._function = function

    def run(self, state: StateView) -> AgentResult:
        return self._function(state)


class WorkflowStateTests(unittest.TestCase):
    def test_snapshot_is_read_only_and_tracks_atomic_changes(self) -> None:
        state = WorkflowState({"code": "print('ok')"})
        state.apply({"count": 1, "ready": True}, source="prepare")

        snapshot = state.snapshot()

        self.assertEqual(snapshot.require("count", int), 1)
        self.assertEqual(state.version, 1)
        self.assertEqual(state.history[0].source, "prepare")
        self.assertEqual(state.history[0].updated_keys, ("count", "ready"))
        with self.assertRaises(TypeError):
            snapshot._values["count"] = 2
        with self.assertRaisesRegex(ValueError, "已完成或失败"):
            generate_workflow_report(state)

    def test_tracks_test_results_repair_history_and_domain_iterations(self) -> None:
        first = object()
        second = object()
        state = WorkflowState({"test_result": first})

        state.apply({"test_result": None}, source="fix")
        state.apply({"test_result": second}, source="test")
        state.apply({"repair_history": ("round-1",), "fix_attempts": 1}, source="repair")

        self.assertEqual(state.test_results, (first, second))
        self.assertIs(state.latest_test_result, second)
        self.assertEqual(state.repair_history, ("round-1",))
        self.assertEqual(state.repair_iteration_count, 1)
        self.assertEqual(state.snapshot().repair_history, ("round-1",))

    def test_runtime_metadata_collections_are_read_only_snapshots(self) -> None:
        state = WorkflowState()
        state.register_stages({"prepare": "prepare-agent"})
        state.start()
        state.begin_step("prepare", agent="prepare-agent")
        snapshot = state.snapshot()

        self.assertEqual(snapshot.stage_statuses["prepare"], StageStatus.RUNNING)
        self.assertEqual(snapshot.iteration_count, 1)
        with self.assertRaises(TypeError):
            snapshot.stages["other"] = snapshot.stages["prepare"]


class WorkflowEngineTests(unittest.TestCase):
    def test_executes_steps_and_routes_from_result(self) -> None:
        prepare = FunctionAgent(
            "prepare-agent",
            lambda state: AgentResult(updates={"value": 2}, next_step="double"),
        )
        double = FunctionAgent(
            "double-agent",
            lambda state: AgentResult(
                updates={"value": state.require("value", int) * 2},
                output="done",
                next_step=END,
            ),
        )
        engine = WorkflowEngine().add_step("prepare", prepare).add_step("double", double)

        run = engine.run()

        self.assertEqual(run.state.get("value"), 4)
        self.assertEqual(run.state.status, WorkflowStatus.COMPLETED)
        self.assertEqual([item.step for item in run.executions], ["prepare", "double"])
        self.assertEqual(run.output, "done")
        report = run.report
        self.assertEqual(report.outcome, WorkflowOutcome.SUCCEEDED)
        self.assertTrue(report.succeeded)
        self.assertEqual(report.iteration_count, 2)
        self.assertEqual([item.name for item in report.stages], ["prepare", "double"])
        self.assertEqual(len(report.agent_results), 2)
        payload = report.to_dict()
        self.assertEqual(payload["workflow_status"], "completed")
        self.assertEqual(payload["outcome"], "succeeded")
        self.assertEqual(payload["agent_results"][-1]["output"], "done")

    def test_state_retains_stage_agent_and_execution_metadata(self) -> None:
        prepare = FunctionAgent(
            "prepare-agent",
            lambda state: AgentResult(updates={"value": 2}, output="prepared"),
        )
        finish = FunctionAgent(
            "finish-agent",
            lambda state: AgentResult(output=state["value"]),
        )
        run = WorkflowEngine().add_step(
            "prepare", prepare, next_step="finish",
        ).add_step("finish", finish).run()
        state = run.state

        self.assertEqual(state.iteration_count, 2)
        self.assertEqual(state.stage_statuses["prepare"], StageStatus.COMPLETED)
        self.assertEqual(state.stage_statuses["finish"], StageStatus.COMPLETED)
        self.assertEqual([item.name for item in state.stage_history], ["prepare", "finish"])
        self.assertEqual([item.output for item in state.agent_results], ["prepared", 2])
        self.assertEqual(state.agent_results[0].updates["value"], 2)
        self.assertEqual(state.agent_results[0].updated_keys, ("value",))
        with self.assertRaises(TypeError):
            state.agent_results[0].updates["value"] = 3
        self.assertEqual(
            [item.sequence for item in state.execution_logs],
            list(range(1, len(state.execution_logs) + 1)),
        )
        self.assertEqual(state.execution_logs[-1].event, "workflow.completed")

    def test_router_can_branch_using_committed_state(self) -> None:
        decide = FunctionAgent(
            "decider",
            lambda state: AgentResult(updates={"approved": state["score"] >= 60}),
        )
        passed = FunctionAgent("passed", lambda state: AgentResult(output="pass"))
        failed = FunctionAgent("failed", lambda state: AgentResult(output="fail"))
        engine = WorkflowEngine().add_step(
            "decide",
            decide,
            router=lambda state, result: "passed" if state["approved"] else "failed",
        )
        engine.add_step("passed", passed).add_step("failed", failed)

        self.assertEqual(engine.run({"score": 80}).output, "pass")
        self.assertEqual(engine.run({"score": 20}).output, "fail")

    def test_failure_is_wrapped_and_preserved_on_state(self) -> None:
        state = WorkflowState()

        def fail(_state: StateView) -> AgentResult:
            raise ValueError("boom")

        engine = WorkflowEngine().add_step("fail", FunctionAgent("broken", fail))

        with self.assertRaisesRegex(WorkflowExecutionError, "boom"):
            engine.run(state)

        self.assertEqual(state.status, WorkflowStatus.FAILED)
        self.assertEqual(state.current_step, "fail")
        self.assertEqual(state.error, "boom")
        self.assertEqual(state.stage_statuses["fail"], StageStatus.FAILED)
        self.assertEqual(state.stage_history[-1].error, "boom")
        self.assertEqual(state.execution_logs[-1].event, "workflow.failed")
        report = generate_workflow_report(state)
        self.assertEqual(report.outcome, WorkflowOutcome.FAILED)
        self.assertFalse(report.succeeded)
        self.assertEqual(report.error, "boom")
        self.assertEqual(report.stages[0].status, StageStatus.FAILED)

    def test_max_steps_stops_cycles(self) -> None:
        looping = FunctionAgent(
            "looping",
            lambda state: AgentResult(next_step="loop"),
        )
        engine = WorkflowEngine(max_steps=2).add_step("loop", looping)

        with self.assertRaisesRegex(WorkflowExecutionError, "最大步骤数"):
            engine.run()


class CodeReviewWorkflowTests(unittest.TestCase):
    def test_code_review_agent_implements_workflow_agent_interface(self) -> None:
        self.assertTrue(all(
            issubclass(agent_type, BaseAgent)
            for agent_type in (CodingAgent, ReviewAgent, FixAgent, TestAgent)
        ))
        self.assertTrue(issubclass(CodeReviewAgent, ReviewAgent))
        with self.assertRaisesRegex(TypeError, "只能接收 StateView"):
            CodingAgent(object()).execute(WorkflowState())

        class InvalidResultAgent(BaseAgent):
            name = "invalid-result"

            def run(self, state: StateView) -> AgentResult:
                return None

        with self.assertRaisesRegex(TypeError, "必须返回 AgentResult"):
            InvalidResultAgent().execute(WorkflowState().snapshot())
        workflow = create_review_workflow(CodeReviewAgent(MockReviewer()))

        run = workflow.run({"code": "value = 10 / 0", "task": "全面审查"})

        self.assertEqual(run.state.status, WorkflowStatus.COMPLETED)
        self.assertEqual(run.output, run.state.get("report"))
        self.assertEqual(len(run.state.get("tool_findings")), 1)
        self.assertEqual(run.state.get("tool_findings")[0].rule_id, "division-by-zero")
        self.assertEqual(run.report.outcome, WorkflowOutcome.UNRESOLVED)
        self.assertEqual(run.report.final_review_result, run.state.get("report"))


if __name__ == "__main__":
    unittest.main()
