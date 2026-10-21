"""Homework2: deterministic end-to-end Workflow demonstration.

The demo intentionally starts with a defective implementation and uses the
real Workflow, Agents, TestRunner, bounded repair loop, State records, and
Workflow Report.  Providers are deterministic so the CLI can be demonstrated
offline without an API key.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from agent import ReviewAgent
from coding_agent import CodingAgent, create_coding_workflow
from fix_agent import FixAgent, FixRun
from models import (
    CodeGenerationResult,
    ReviewIssue,
    ReviewReport,
    TestExecutionResult,
    TestGenerationResult,
)
from test_agent import TestAgent, TestRunner
from workflow import (
    END,
    RepairDecision,
    WorkflowExecutionError,
    WorkflowReport,
    WorkflowState,
    generate_workflow_report,
)


INITIAL_CODE = """def add(a, b):
    return a - b
"""

INTERMEDIATE_CODE = """def add(a, b):
    return a + b + 1
"""

FINAL_CODE = """def add(a, b):
    return a + b
"""


class DemoCodeGenerator:
    """Produce the intentionally defective baseline used by the demo."""

    is_mock = True

    def generate(self, requirements: str, *, context: str = "") -> CodeGenerationResult:
        return CodeGenerationResult(
            code=INITIAL_CODE,
            summary="生成初始 add 实现；演示场景故意保留减法缺陷。",
        )


class DemoReviewer:
    """Return deterministic requirement findings for each source revision."""

    is_mock = True

    def review(
        self,
        code: str,
        tool_findings: list[Any],
        *,
        task: str,
        plan: Any,
    ) -> ReviewReport:
        if "return a - b" in code:
            issue = ReviewIssue(
                problem="add 实现执行了减法",
                severity="high",
                reason="实现与“返回两数之和”的需求不一致。",
                suggestion="将返回表达式改为加法，并用测试验证。",
                category="requirement",
                evidence="return a - b",
                requirement_ref="返回两个数字之和",
            )
        elif "return a + b + 1" in code:
            issue = ReviewIssue(
                problem="add 的返回值额外增加了 1",
                severity="high",
                reason="结果仍不满足精确求和要求。",
                suggestion="移除额外的 + 1。",
                category="requirement",
                evidence="return a + b + 1",
                requirement_ref="返回两个数字之和",
            )
        else:
            issue = None
        issues = [] if issue is None else [issue]
        return ReviewReport(
            summary="实现满足需求。" if not issues else "发现阻断性需求偏差。",
            issues=issues,
        )


class DemoTestGenerator:
    """Generate business assertions that are reused in every repair round."""

    is_mock = True

    def generate(
        self,
        code: str,
        *,
        requirements: str = "",
        context: str = "",
    ) -> TestGenerationResult:
        return TestGenerationResult(
            test_cases=[
                {
                    "name": "test_add_positive_numbers",
                    "code": "self.assertEqual(module.add(2, 3), 5)",
                },
                {
                    "name": "test_add_negative_number",
                    "code": "self.assertEqual(module.add(-2, 1), -1)",
                },
                {
                    "name": "test_add_zero",
                    "code": "self.assertEqual(module.add(0, 0), 0)",
                },
            ],
            summary="生成 3 个 add 需求测试；后续修复轮次复用同一组断言。",
        )


class DemoCodeFixer:
    """Converge in two visible repair rounds."""

    is_mock = True

    def fix(
        self,
        code: str,
        *,
        report: ReviewReport | None,
        test_result: TestExecutionResult | None,
        test_cases: list[Any],
        requirements: str = "",
        context: str = "",
    ) -> CodeGenerationResult:
        if "return a - b" in code:
            return CodeGenerationResult(
                code=INTERMEDIATE_CODE,
                summary="第 1 轮改为加法，但故意保留 +1 偏差以展示继续循环。",
            )
        if "return a + b + 1" in code:
            return CodeGenerationResult(
                code=FINAL_CODE,
                summary="第 2 轮移除额外 +1，完成精确求和。",
            )
        return CodeGenerationResult(code=code, summary="代码已满足演示需求，无需修改。")


def build_demo_workflow(
    *,
    max_fix_attempts: int = 3,
    test_runner: TestRunner | None = None,
):
    """Build the Homework2 demo from the project's production workflow pieces."""

    return create_coding_workflow(
        CodingAgent(DemoCodeGenerator()),
        ReviewAgent(DemoReviewer()),
        test_agent=TestAgent(
            DemoTestGenerator(),
            test_runner if test_runner is not None else TestRunner(timeout_seconds=5),
        ),
        fix_agent=FixAgent(DemoCodeFixer()),
        max_fix_attempts=max_fix_attempts,
    )


def _heading(index: int, title: str) -> None:
    print(f"\n{'=' * 78}")
    print(f"{index}. {title}")
    print("=" * 78)


def _round_label(repair_round: int | None) -> str:
    if repair_round is None:
        return "setup"
    if repair_round == 0:
        return "baseline"
    return f"repair-{repair_round}"


def _target_name(target: str) -> str:
    return "END" if target == END else target


def _output_summary(output: Any) -> str:
    if isinstance(output, CodeGenerationResult):
        return output.summary
    if isinstance(output, ReviewReport):
        return (
            f"score={output.score if output.score is not None else '-'}, "
            f"issues={len(output.issues)}, test={output.test_status}"
        )
    if isinstance(output, TestExecutionResult):
        return (
            f"status={output.status}, run={output.tests_run}, passed={output.passed}, "
            f"failed={output.failed}, errors={output.errors}"
        )
    if isinstance(output, FixRun):
        return f"changed={output.changed}; {output.summary}"
    return type(output).__name__


def _render_agent_process(state: WorkflowState) -> None:
    _heading(1, "Agent 执行过程")
    for record in state.agent_results:
        print(
            f"[{record.iteration:02d}] {_round_label(record.repair_round):<10} "
            f"{record.stage:<7} -> {record.agent}"
        )
        print(f"     结果: {_output_summary(record.output)}")
        print(f"     State v{record.state_version}: {', '.join(record.updated_keys)}")


def _render_branches(state: WorkflowState) -> None:
    _heading(2, "Review / Test 条件分支")
    decisions = [
        record.updates.get("repair_decision")
        for record in state.agent_results
        if isinstance(record.updates.get("repair_decision"), RepairDecision)
    ]
    for decision in decisions:
        print(
            f"round={decision.attempt:<2} after={decision.after_stage:<6} "
            f"review={str(decision.review_passed):<5} "
            f"test={str(decision.test_status):<7} "
            f"=> {_target_name(decision.next_step):<6} ({decision.reason})"
        )


def _render_repair_loop(state: WorkflowState) -> None:
    _heading(3, "有限修复循环")
    history = state.repair_history
    if not history:
        print("未进入 Fix 阶段。")
        return
    for item in history:
        before = item.test_result_before.status if item.test_result_before else "not_run"
        after = item.test_result_after.status if item.test_result_after else "not_run"
        print(
            f"第 {item.attempt} 轮 | {item.status:<10} | "
            f"test: {before} -> {after} | code_changed={item.code_before != item.code_after}"
        )
        for execution in item.executions:
            detail = execution.error or _output_summary(execution.output)
            print(
                f"  - {execution.step:<6} / {execution.agent:<11} "
                f"{execution.duration_seconds * 1000:7.2f} ms | {detail}"
            )
    print(
        f"停止原因: {state.get('repair_stop_reason')} | "
        f"已用 {state.repair_iteration_count}/{state.get('max_fix_attempts')} 轮"
    )


def _render_logs(report: WorkflowReport) -> None:
    _heading(4, "逐轮执行日志")
    for item in report.execution_logs:
        scope = "/".join(part for part in (item.stage, item.agent) if part) or "workflow"
        print(
            f"#{item.sequence:02d} [{_round_label(item.repair_round):<10}] "
            f"v{item.state_version:<2} {item.event:<18} {scope:<20} {item.message}"
        )


def _render_report(report: WorkflowReport) -> None:
    _heading(5, "最终 Workflow Report")
    print(f"结果       : {report.outcome.value}")
    print(f"Workflow   : {report.workflow_status.value}")
    print(f"摘要       : {report.summary}")
    print(f"State 版本 : {report.state_version}")
    print(f"阶段执行   : {report.iteration_count}")
    print(f"修复轮数   : {report.repair_iteration_count}")
    print(f"测试记录   : {len(report.test_results)}")
    print(f"日志记录   : {len(report.execution_logs)}")
    if report.duration_seconds is not None:
        print(f"总耗时     : {report.duration_seconds * 1000:.2f} ms")

    print("\n阶段汇总:")
    print(f"  {'stage':<10} {'agent':<14} {'status':<10} {'runs':>4} {'duration':>11}")
    for stage in report.stages:
        print(
            f"  {stage.name:<10} {(stage.agent or '-'):<14} {stage.status.value:<10} "
            f"{stage.executions:>4} {stage.duration_seconds * 1000:>8.2f} ms"
        )

    final_review = report.final_review_result
    final_test = report.final_test_result
    if isinstance(final_review, ReviewReport):
        print(
            f"\n最终 Review: issues={len(final_review.issues)}, "
            f"score={final_review.score}, test={final_review.test_status}"
        )
    if isinstance(final_test, TestExecutionResult):
        print(
            f"最终 Test  : {final_test.status}, "
            f"{final_test.passed}/{final_test.tests_run} passed"
        )


def render_demo(state: WorkflowState, report: WorkflowReport, *, as_json: bool = False) -> None:
    """Render all observable Workflow artifacts in a stable CLI layout."""

    _render_agent_process(state)
    _render_branches(state)
    _render_repair_loop(state)
    _render_logs(report)
    _render_report(report)

    _heading(6, "最终代码")
    print(state.get("code", ""), end="" if str(state.get("code", "")).endswith("\n") else "\n")

    if as_json:
        _heading(7, "Workflow Report JSON")
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Homework2：离线演示 Coding → Review → Test → Fix 有限循环与报告。"
    )
    parser.add_argument(
        "--max-fix-attempts",
        type=int,
        default=3,
        help="最大修复次数（默认 3；本演示正常收敛需要 2 次）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="在人类可读输出后附加完整 Workflow Report JSON",
    )
    return parser


def _configure_output() -> None:
    """Keep Chinese CLI output stable when Windows inherits a legacy code page."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _configure_output()
    parser = _parser()
    args = parser.parse_args(argv)
    if args.max_fix_attempts < 0:
        parser.error("--max-fix-attempts 不能小于 0")

    print("Homework2 | Multi-Agent Workflow Demo")
    print("场景: 实现 add(a, b)，通过 Review/Test 条件分支在有限次数内自动修复。")
    print("说明: 演示供应商完全离线；测试由真实 TestRunner 子进程执行。")

    workflow = build_demo_workflow(max_fix_attempts=args.max_fix_attempts)
    initial_state = {
        "requirements": "实现 add(a, b)，返回两个数字的精确和。",
        "context": "Homework2 确定性 CLI 演示；不得引入第三方依赖。",
        "task": "检查实现是否满足精确求和需求，并结合真实测试结果判断。",
    }
    try:
        run = workflow.run(initial_state)
        state = run.state
        report = run.report
    except WorkflowExecutionError as exc:
        state = exc.state
        report = generate_workflow_report(state)

    render_demo(state, report, as_json=args.json)
    return 0 if report.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
