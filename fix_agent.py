"""Feedback-driven code repair node for the workflow engine."""

from __future__ import annotations

from dataclasses import dataclass
import difflib
import json

from pydantic import ValidationError

from llm import CodeFixer, LLMFixError, LLMFixSyntaxError
from models import CodeGenerationResult, ReviewReport, TestCase, TestExecutionResult, generated_code_syntax_error
from workflow import AgentResult, BaseAgent, StateView, WorkflowEngine


@dataclass(frozen=True)
class FixRun:
    """Actual source change, not a claim that the diagnostics are resolved."""

    original_code: str
    code: str
    summary: str
    diff: str
    changed: bool
    skipped: bool = False
    generation_attempts: int = 1
    syntax_errors: tuple[str, ...] = ()

    def to_text(self) -> str:
        status = "已修改代码" if self.changed else "代码未变更"
        retry = f"\n代码生成调用 {self.generation_attempts} 次，语法重试 {len(self.syntax_errors)} 次。" if self.syntax_errors else ""
        return f"\n=== Fix Agent 修复结果 ===\n{status}：{self.summary}{retry}\n{self.diff}".rstrip()


class FixAgent(BaseAgent):
    """Repair code using review findings and test failures from State."""

    name = "fix"

    def __init__(self, fixer: CodeFixer, *, max_syntax_retries: int = 3) -> None:
        if isinstance(max_syntax_retries, bool) or not isinstance(max_syntax_retries, int) or max_syntax_retries < 0:
            raise ValueError("max_syntax_retries 必须是非负整数。")
        self._fixer = fixer
        self._max_syntax_retries = max_syntax_retries

    def run(self, state: StateView) -> AgentResult:
        code = state.require("code", str)
        if not code.strip():
            raise ValueError("待修复代码不能为空。")
        report = state.get("report")
        test_result = state.get("test_result")
        if report is not None and not isinstance(report, ReviewReport):
            raise TypeError("report 必须是 ReviewReport。")
        if test_result is not None and not isinstance(test_result, TestExecutionResult):
            raise TypeError("test_result 必须是 TestExecutionResult。")
        if report is None and test_result is None:
            raise ValueError("Fix Agent 需要当前代码的 report 或 test_result。")
        requirements = state.get("requirements", "")
        context = state.get("context", "")
        if not isinstance(requirements, str) or not isinstance(context, str):
            raise TypeError("requirements 和 context 必须是字符串。")
        raw_cases = state.get("test_cases", [])
        if not isinstance(raw_cases, list):
            raise TypeError("test_cases 必须是列表。")
        cases = [TestCase.model_validate(case.model_dump() if isinstance(case, TestCase) else case)
                 for case in raw_cases]
        if report is not None:
            report = ReviewReport.model_validate(report.model_dump())
        if test_result is not None:
            test_result = TestExecutionResult.model_validate(test_result.model_dump())

        has_issues = report is not None and bool(report.issues)
        has_test_failure = test_result is not None and test_result.status != "passed"
        if not has_issues and not has_test_failure:
            run = FixRun(code, code, "没有审查问题或测试失败，跳过修复。", "", False,
                         skipped=True, generation_attempts=0)
        else:
            result, generation_attempts, syntax_errors = self._generate_with_syntax_retries(
                code, report, test_result, cases, requirements, context,
            )
            diff = "".join(difflib.unified_diff(
                code.splitlines(keepends=True), result.code.splitlines(keepends=True),
                fromfile="before_fix.py", tofile="after_fix.py",
            ))
            run = FixRun(code, result.code, result.summary, diff, code != result.code,
                         generation_attempts=generation_attempts, syntax_errors=syntax_errors)

        updates = {
            "code": run.code,
            "fixed_code": run.code,
            "code_before_fix": code,
            "fix_result": run,
            "fix_changed": run.changed,
        }
        if run.changed:
            # Diagnostics refer to the old code until downstream verification runs.
            updates.update({
                # Retain obligations across consecutive fixes, even if only
                # one of the previous diagnostics has been refreshed so far.
                "required_verifications": tuple(
                    key for key in ("report", "test_result")
                    if state.get(key) is not None
                    or key in state.get("required_verifications", ())
                ),
                "report_before_fix": report,
                "review_run_before_fix": state.get("review_run"),
                "test_result_before_fix": test_result,
                "report": None,
                "review_run": None,
                "plan": None,
                "tool_findings": None,
                "test_result": None,
                "reuse_test_cases": bool(cases),
            })
        return AgentResult(updates=updates, output=run)

    def _generate_with_syntax_retries(
        self, code: str, report: ReviewReport | None, test_result: TestExecutionResult | None,
        cases: list[TestCase], requirements: str, context: str,
    ) -> tuple[CodeGenerationResult, int, tuple[str, ...]]:
        retry_context = context
        errors: list[str] = []
        for retry in range(self._max_syntax_retries + 1):
            try:
                result = self._generate_once(code, report, test_result, cases, requirements, retry_context)
                return result, retry + 1, tuple(errors)
            except LLMFixSyntaxError as exc:
                errors.append(exc.reason)
                if retry == self._max_syntax_retries:
                    raise LLMFixSyntaxError(
                        f"修复代码在 {retry + 1} 次生成调用后仍未通过 Python 语法检查"
                        f"（最多 {self._max_syntax_retries} 次重试）：{exc.reason}",
                        generated_code=exc.generated_code, reason=exc.reason,
                    ) from exc
                retry_context = context + "\n\n以下为上一次生成的语法失败诊断数据，请修正语法并重新返回完整代码：\n" + json.dumps({
                    "syntax_retry": retry + 1, "syntax_error": exc.reason,
                    "invalid_generated_code": exc.generated_code,
                }, ensure_ascii=False)
        raise AssertionError("语法重试循环未返回结果")

    def _generate_once(
        self, code: str, report: ReviewReport | None, test_result: TestExecutionResult | None,
        cases: list[TestCase], requirements: str, context: str,
    ) -> CodeGenerationResult:
        try:
            result = self._fixer.fix(
                code, report=report, test_result=test_result, test_cases=cases,
                requirements=requirements, context=context,
            )
            if not isinstance(result, CodeGenerationResult):
                raise LLMFixError("CodeFixer 必须返回 CodeGenerationResult。")
            result = CodeGenerationResult.model_validate(result.model_dump())
        except ValidationError as exc:
            syntax = generated_code_syntax_error(exc)
            if syntax is not None:
                raise LLMFixSyntaxError(
                    f"修复输出无效：{exc}", generated_code=syntax.code, reason=syntax.reason,
                ) from exc
            raise LLMFixError(f"修复输出无效：{exc}") from exc
        try:
            # AST parsing alone accepts return/break outside a valid scope.
            # Compilation checks those constraints without executing the source.
            compile(result.code, "generated_fix.py", "exec")
        except (SyntaxError, ValueError) as exc:
            raise LLMFixSyntaxError(
                f"修复输出未通过 Python 语法检查：{exc}", generated_code=result.code, reason=str(exc),
            ) from exc
        return result


def create_fix_workflow(agent: FixAgent) -> WorkflowEngine:
    """Build a one-step repair workflow; diagnostics need subsequent refresh."""

    return WorkflowEngine("fix").add_step("fix", agent)
