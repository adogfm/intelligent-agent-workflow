"""Agent orchestration layer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from code_checker import CodeCheckFinding, CodeChecker
from file_editor import FileEditResult, FileEditor
from llm import Reviewer
from models import ReviewContext, ReviewReport
from planner import DEFAULT_REVIEW_TASK, ReviewPlan, TaskPlanner
from review_policy import apply_review_policy
from workflow import AgentResult, BaseAgent, StateView, WorkflowEngine


@dataclass(frozen=True)
class ReviewRun:
    report: ReviewReport
    plan: ReviewPlan
    tool_findings: list[CodeCheckFinding]


@dataclass(frozen=True)
class ReviewAndFixRun:
    review: ReviewRun
    edit: FileEditResult
    verification_findings: list[CodeCheckFinding]


class ReviewAgent(BaseAgent):
    """Coordinates a code review without depending on a concrete LLM vendor."""

    name = "code-review"

    def __init__(
        self,
        reviewer: Reviewer,
        checker: CodeChecker | None = None,
        planner: TaskPlanner | None = None,
        editor: FileEditor | None = None,
    ) -> None:
        self._reviewer = reviewer
        self._checker = checker or CodeChecker()
        self._planner = planner or TaskPlanner()
        self._editor = editor or FileEditor()

    def run(self, state: StateView) -> AgentResult:
        """Run a review as a workflow step through the unified Agent API."""

        code = state.require("code", str)
        task = state.get("task", DEFAULT_REVIEW_TASK)
        if not isinstance(task, str):
            raise TypeError("Workflow 状态字段 'task' 必须是 str")
        review_context = ReviewContext(
            requirements=state.get("requirements", ""), context=state.get("context", ""),
            test_result=state.get("test_result"), test_cases=state.get("test_cases", []),
        )
        run = self.review_detailed(code, task, review_context=review_context)
        return AgentResult(
            updates={
                "review_run": run,
                "report": run.report,
                "plan": run.plan,
                "tool_findings": run.tool_findings,
            },
            output=run.report,
        )

    def review(self, code: str, task: str = DEFAULT_REVIEW_TASK) -> ReviewReport:
        return self.review_detailed(code, task).report

    def review_detailed(
        self, code: str, task: str = DEFAULT_REVIEW_TASK, *, review_context: ReviewContext | None = None,
    ) -> ReviewRun:
        if not code.strip():
            raise ValueError("Python 代码不能为空。")

        normalized_task = task.strip() or DEFAULT_REVIEW_TASK
        plan = self._planner.plan(normalized_task)
        tool_findings = self._checker.check(code) if plan.use_code_checker else []
        review_context = review_context if review_context is not None else ReviewContext()
        # A declared optional method avoids treating dynamically generated Mock
        # attributes as support for the new contextual Reviewer interface.
        contextual = getattr(type(self._reviewer), "review_with_context", None)
        if callable(contextual):
            reviewer_report = self._reviewer.review_with_context(
                code, tool_findings, task=normalized_task, plan=plan, review_context=review_context,
            )
        else:
            reviewer_report = self._reviewer.review(
                code, tool_findings, task=normalized_task, plan=plan,
            )
        tool_issues = [finding.to_review_issue() for finding in tool_findings]
        scoped_report = apply_review_policy(reviewer_report, tool_issues, review_context, normalized_task)
        if plan.use_code_checker:
            planner_summary = (
                f"Task Planner 决定调用 CodeChecker（{plan.reason}）。"
                f"CodeChecker 检出 {len(tool_findings)} 个基础问题。"
            )
        else:
            planner_summary = f"Task Planner 决定跳过 CodeChecker（{plan.reason}）。"

        scoped_report.summary = f"{planner_summary} {scoped_report.summary}"
        return ReviewRun(
            report=scoped_report,
            plan=plan,
            tool_findings=tool_findings,
        )

    def review_and_fix(
        self,
        path: Path,
        code: str,
        task: str = DEFAULT_REVIEW_TASK,
    ) -> ReviewAndFixRun:
        review = self.review_detailed(code, task)
        edit = self._editor.apply(path, code, review.report, review.tool_findings)
        verification_findings = self._checker.check(edit.updated_code) if edit.applied else []
        return ReviewAndFixRun(
            review=review,
            edit=edit,
            verification_findings=verification_findings,
        )


class CodeReviewAgent(ReviewAgent):
    """Backward-compatible name for :class:`ReviewAgent`."""


def create_review_workflow(agent: ReviewAgent) -> WorkflowEngine:
    """Build the default one-step review workflow.

    More agents can be appended by callers without changing ReviewAgent's
    existing direct APIs.
    """

    return WorkflowEngine("code-review").add_step("review", agent)
