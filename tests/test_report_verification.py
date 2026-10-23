"""Reports must not accept pre-fix evidence for modified source code."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from fix_agent import FixAgent, create_fix_workflow
from models import CodeGenerationResult, ReviewIssue, ReviewReport, TestExecutionResult as ExecutionResult
from workflow import (
    AgentResult, BaseAgent, StateView, WorkflowExecutionError, WorkflowOutcome,
    WorkflowEngine, WorkflowStatus, generate_workflow_report,
)


ORIGINAL = "def add(a, b): return a + b\n"
MODIFIED = "def add(a, b): return 999\n"
BLOCKED = ReviewReport(summary="Requires repair", issues=[ReviewIssue(
    problem="Incorrect behavior", severity="high", reason="Requirement mismatch",
    suggestion="Repair and verify",
)])
CLEAN = ReviewReport(summary="Verified", issues=[])
PASSED = ExecutionResult(status="passed", tests_run=1, passed=1)
FAILED = ExecutionResult(status="failed", tests_run=1, failed=1)


class VerificationAgent(BaseAgent):
    name = "verification"

    def __init__(self, updates):
        self.updates = updates

    def run(self, state: StateView) -> AgentResult:
        return AgentResult(updates=self.updates)


class ReportVerificationTests(unittest.TestCase):
    def fix_agent(self, code=MODIFIED):
        fixer = Mock()
        fixer.fix.return_value = CodeGenerationResult(code=code, summary="Changed source")
        return FixAgent(fixer)

    def workflow(self, code=MODIFIED):
        return create_fix_workflow(self.fix_agent(code))

    def test_changed_code_cannot_reuse_a_historical_pass(self):
        run = self.workflow().run({
            "code": ORIGINAL, "report": BLOCKED, "test_result": PASSED,
        })
        report = run.report
        self.assertEqual(run.state.status, WorkflowStatus.COMPLETED)
        self.assertEqual(run.state.get("code"), MODIFIED)
        self.assertEqual(report.outcome, WorkflowOutcome.UNRESOLVED)
        self.assertFalse(report.succeeded)
        self.assertIsNone(report.final_review_result)
        self.assertIsNone(report.final_test_result)
        self.assertEqual(report.test_results, (PASSED,))
        self.assertEqual(run.state.get("test_result_before_fix"), PASSED)
        self.assertIn("待复验", report.summary)
        payload = report.to_dict()
        self.assertEqual(payload["outcome"], "unresolved")
        self.assertIsNone(payload["final_test_result"])
        self.assertEqual(payload["test_results"][0]["status"], "passed")

    def test_changed_code_with_only_review_feedback_remains_unresolved(self):
        report = self.workflow().run({"code": ORIGINAL, "report": BLOCKED}).report
        self.assertEqual(report.outcome, WorkflowOutcome.UNRESOLVED)
        self.assertIsNone(report.final_test_result)

    def test_changed_code_with_only_test_feedback_remains_unresolved(self):
        report = self.workflow().run({"code": ORIGINAL, "test_result": FAILED}).report
        self.assertEqual(report.outcome, WorkflowOutcome.UNRESOLVED)
        self.assertIsNone(report.final_test_result)
        self.assertEqual(report.test_results, (FAILED,))

    def test_partial_verification_does_not_resolve_both_requirements(self):
        for updates in ({"report": CLEAN}, {"test_result": PASSED}):
            with self.subTest(updates=updates):
                workflow = WorkflowEngine().add_step(
                    "fix", self.fix_agent(), next_step="verify",
                )
                workflow.add_step("verify", VerificationAgent(updates))
                run = workflow.run({"code": ORIGINAL, "report": BLOCKED, "test_result": PASSED})
                self.assertEqual(run.report.outcome, WorkflowOutcome.UNRESOLVED)

    def test_fresh_verification_resolves_the_enabled_checks(self):
        scenarios = (
            ({"report": BLOCKED}, {"report": CLEAN}),
            ({"test_result": FAILED}, {"test_result": PASSED}),
            ({"report": BLOCKED, "test_result": PASSED},
             {"report": CLEAN, "test_result": PASSED}),
        )
        for feedback, updates in scenarios:
            with self.subTest(feedback=feedback):
                workflow = WorkflowEngine().add_step(
                    "fix", self.fix_agent(), next_step="verify",
                )
                workflow.add_step("verify", VerificationAgent(updates))
                report = workflow.run({"code": ORIGINAL, **feedback}).report
                self.assertEqual(report.outcome, WorkflowOutcome.SUCCEEDED)
                self.assertTrue(report.succeeded)
                self.assertEqual(report.final_test_result, updates.get("test_result"))

    def test_unchanged_code_retains_valid_current_evidence(self):
        report = self.workflow(ORIGINAL).run({
            "code": ORIGINAL, "report": CLEAN, "test_result": PASSED,
        }).report
        self.assertTrue(report.succeeded)
        self.assertEqual(report.final_test_result, PASSED)

    def test_unchanged_code_does_not_hide_existing_blockers(self):
        report = self.workflow(ORIGINAL).run({
            "code": ORIGINAL, "report": BLOCKED, "test_result": PASSED,
        }).report
        self.assertEqual(report.outcome, WorkflowOutcome.UNRESOLVED)
        self.assertEqual(report.final_review_result, BLOCKED)
        self.assertEqual(report.final_test_result, PASSED)

    def test_fresh_but_unsuccessful_verification_remains_unresolved(self):
        for updates in (
            {"report": BLOCKED, "test_result": PASSED},
            {"report": CLEAN, "test_result": FAILED},
        ):
            with self.subTest(updates=updates):
                workflow = WorkflowEngine().add_step(
                    "fix", self.fix_agent(), next_step="verify",
                )
                workflow.add_step("verify", VerificationAgent(updates))
                report = workflow.run({
                    "code": ORIGINAL, "report": BLOCKED, "test_result": PASSED,
                }).report
                self.assertEqual(report.outcome, WorkflowOutcome.UNRESOLVED)
                self.assertEqual(report.final_test_result, updates["test_result"])

    def test_second_fix_keeps_an_outstanding_review_requirement(self):
        workflow = WorkflowEngine().add_step("fix", self.fix_agent(), next_step="test")
        workflow.add_step("test", VerificationAgent({"test_result": FAILED}), next_step="fix_again")
        workflow.add_step("fix_again", self.fix_agent(ORIGINAL), next_step="retest")
        workflow.add_step("retest", VerificationAgent({"test_result": PASSED}))
        run = workflow.run({"code": ORIGINAL, "report": BLOCKED, "test_result": PASSED})
        self.assertEqual(run.state.get("code"), ORIGINAL)
        self.assertIsNone(run.state.get("report_before_fix"))
        self.assertEqual(run.report.outcome, WorkflowOutcome.UNRESOLVED)
        self.assertEqual(run.report.final_test_result, PASSED)
        self.assertIn("代码审查", run.report.summary)

    def test_failure_after_fix_retains_history_without_a_current_pass(self):
        class BrokenVerification(BaseAgent):
            name = "broken-verification"

            def run(self, state):
                raise RuntimeError("Verification unavailable")

        workflow = WorkflowEngine().add_step("fix", self.fix_agent(), next_step="verify")
        workflow.add_step("verify", BrokenVerification())
        with self.assertRaises(WorkflowExecutionError) as caught:
            workflow.run({"code": ORIGINAL, "report": BLOCKED, "test_result": PASSED})
        report = generate_workflow_report(caught.exception.state)
        self.assertEqual(report.outcome, WorkflowOutcome.FAILED)
        self.assertIsNone(report.final_test_result)
        self.assertEqual(report.test_results, (PASSED,))


if __name__ == "__main__":
    unittest.main()
