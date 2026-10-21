"""Offline tests that never call a remote LLM API."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from agent import CodeReviewAgent
from code_checker import CodeCheckFinding, CodeChecker
from file_editor import FileEditError, FileEditor
from llm import (
    LLMReviewError,
    MockReviewer,
    OpenAIReviewer,
    create_reviewer,
    parse_review_report,
)
from models import ReviewReport
from planner import ReviewPlan, TaskPlanner


class RecordingReviewer:
    is_mock = True

    def __init__(self) -> None:
        self.received_findings: list[CodeCheckFinding] = []
        self.received_task = ""
        self.received_plan: ReviewPlan | None = None

    def review(
        self,
        code: str,
        tool_findings: list[CodeCheckFinding],
        *,
        task: str,
        plan: ReviewPlan,
    ) -> ReviewReport:
        self.received_findings = tool_findings
        self.received_task = task
        self.received_plan = plan
        return ReviewReport(summary="Reviewer 已运行。", issues=[])


class OfflineReviewerTests(unittest.TestCase):
    def test_missing_api_key_selects_mock_reviewer(self) -> None:
        with patch("llm.load_dotenv"), patch.dict(os.environ, {}, clear=True):
            reviewer = create_reviewer()

        self.assertIsInstance(reviewer, MockReviewer)
        self.assertTrue(reviewer.is_mock)

    def test_mock_review_returns_fixed_structured_result(self) -> None:
        report = CodeReviewAgent(MockReviewer()).review("print('hello')")

        self.assertIn("[MOCK MODE]", report.summary)
        self.assertIn("CodeChecker 检出 0 个基础问题", report.summary)
        self.assertEqual(len(report.issues), 1)
        self.assertEqual(report.issues[0].severity, "low")
        self.assertTrue(report.issues[0].problem)
        self.assertTrue(report.issues[0].reason)
        self.assertTrue(report.issues[0].suggestion)

    def test_api_key_keeps_real_reviewer_available(self) -> None:
        with patch("llm.load_dotenv"), patch.dict(
            os.environ,
            {
                "DASHSCOPE_API_KEY": "test-api-key",
                "DASHSCOPE_BASE_URL": "https://example.com/compatible-mode/v1",
                "QWEN_MODEL": "qwen-test",
            },
            clear=True,
        ):
            reviewer = create_reviewer()

        self.assertIsInstance(reviewer, OpenAIReviewer)
        self.assertFalse(reviewer.is_mock)
        self.assertEqual(reviewer._model, "qwen-test")
        self.assertEqual(reviewer._base_url, "https://example.com/compatible-mode/v1")

    def test_legacy_openai_api_key_remains_supported(self) -> None:
        with patch("llm.load_dotenv"), patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "legacy-test-key", "OPENAI_MODEL": "qwen-plus"},
            clear=True,
        ):
            reviewer = create_reviewer()

        self.assertIsInstance(reviewer, OpenAIReviewer)
        self.assertEqual(reviewer._model, "qwen-plus")


class ChatCompletionReviewerTests(unittest.TestCase):
    valid_json = (
        '{"summary":"审查完成","issues":['
        '{"problem":"示例问题","severity":"low",'
        '"reason":"示例原因","suggestion":"示例建议"}]}'
    )

    def test_parse_valid_json_as_review_report(self) -> None:
        report = parse_review_report(self.valid_json)

        self.assertEqual(report.summary, "审查完成")
        self.assertEqual(report.issues[0].severity, "low")

    def test_parse_json_inside_markdown_fence(self) -> None:
        report = parse_review_report(f"```json\n{self.valid_json}\n```")

        self.assertEqual(report.issues[0].problem, "示例问题")

    def test_invalid_json_raises_review_error(self) -> None:
        with self.assertRaisesRegex(LLMReviewError, "JSON"):
            parse_review_report("not-json")

    def test_wrong_json_schema_raises_review_error(self) -> None:
        with self.assertRaisesRegex(LLMReviewError, "ReviewReport"):
            parse_review_report('{"message":"missing required fields"}')

    def test_review_uses_chat_completions_and_parses_content(self) -> None:
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=self.valid_json, refusal=None),
                )
            ]
        )
        reviewer = OpenAIReviewer(
            api_key="test-api-key",
            base_url="https://example.com/compatible-mode/v1",
            model="qwen-test",
            client=fake_client,
        )

        report = reviewer.review(
            "print('hello')",
            [],
            task="全面审查",
            plan=ReviewPlan(use_code_checker=True, reason="测试"),
        )

        self.assertEqual(report.summary, "审查完成")
        request = fake_client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "qwen-test")
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual(request["extra_body"], {"enable_thinking": False})
        self.assertEqual(request["messages"][0]["role"], "system")

    def test_truncated_chat_completion_raises_review_error(self) -> None:
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="length",
                    message=SimpleNamespace(content=self.valid_json, refusal=None),
                )
            ]
        )
        reviewer = OpenAIReviewer(api_key="test-api-key", client=fake_client)

        with self.assertRaisesRegex(LLMReviewError, "截断"):
            reviewer.review(
                "print('hello')",
                [],
                task="全面审查",
                plan=ReviewPlan(use_code_checker=True, reason="测试"),
            )


class CodeCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checker = CodeChecker()

    def test_reports_syntax_error_without_raising(self) -> None:
        findings = self.checker.check("def broken(:\n    pass")

        self.assertEqual([finding.rule_id for finding in findings], ["syntax-error"])
        self.assertEqual(findings[0].line, 1)

    def test_reports_bare_except(self) -> None:
        findings = self.checker.check("try:\n    work()\nexcept:\n    pass")

        self.assertEqual([finding.rule_id for finding in findings], ["bare-except"])
        self.assertEqual(findings[0].line, 3)

    def test_reports_literal_division_floor_division_and_modulo_by_zero(self) -> None:
        findings = self.checker.check("a = 1 / 0\nb = 2 // -0.0\nc = 3 % +0")

        self.assertEqual(
            [finding.rule_id for finding in findings],
            ["division-by-zero", "division-by-zero", "division-by-zero"],
        )

    def test_ignores_nonzero_or_dynamic_denominator(self) -> None:
        findings = self.checker.check("a = 1 / 2\nb = 2 / value")

        self.assertEqual(findings, [])

    def test_agent_merges_tool_findings_into_mock_report(self) -> None:
        report = CodeReviewAgent(MockReviewer()).review("value = 10 / 0")

        self.assertIn("CodeChecker 检出 1 个基础问题", report.summary)
        self.assertEqual(len(report.issues), 2)
        self.assertIn("第 1 行", report.issues[0].problem)
        self.assertIn("常量零", report.issues[0].problem)

    def test_agent_passes_tool_findings_to_reviewer(self) -> None:
        reviewer = RecordingReviewer()

        CodeReviewAgent(reviewer).review("try:\n    work()\nexcept:\n    pass")

        self.assertEqual(len(reviewer.received_findings), 1)
        self.assertEqual(reviewer.received_findings[0].rule_id, "bare-except")
        self.assertIsNotNone(reviewer.received_plan)
        self.assertTrue(reviewer.received_plan.use_code_checker)


class TaskPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = TaskPlanner()

    def test_comprehensive_review_uses_code_checker(self) -> None:
        plan = self.planner.plan("全面审查代码正确性和安全性")

        self.assertTrue(plan.use_code_checker)

    def test_style_only_task_skips_code_checker(self) -> None:
        plan = self.planner.plan("只检查代码风格和命名")

        self.assertFalse(plan.use_code_checker)

    def test_static_intent_overrides_style_intent(self) -> None:
        plan = self.planner.plan("检查代码风格，同时查找语法错误")

        self.assertTrue(plan.use_code_checker)

    def test_unknown_task_defaults_to_code_checker(self) -> None:
        plan = self.planner.plan("给我一些改进建议")

        self.assertTrue(plan.use_code_checker)

    def test_agent_does_not_execute_checker_when_plan_skips_it(self) -> None:
        reviewer = RecordingReviewer()
        checker = Mock(spec=CodeChecker)

        report = CodeReviewAgent(reviewer, checker=checker).review(
            "value = 10 / 0",
            task="只检查代码风格和命名",
        )

        checker.check.assert_not_called()
        self.assertEqual(reviewer.received_findings, [])
        self.assertIsNotNone(reviewer.received_plan)
        self.assertFalse(reviewer.received_plan.use_code_checker)
        self.assertIn("Task Planner 决定跳过 CodeChecker", report.summary)


class CountingCodeChecker(CodeChecker):
    def __init__(self) -> None:
        self.call_count = 0

    def check(self, code: str) -> list[CodeCheckFinding]:
        self.call_count += 1
        return super().check(code)


class FileEditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path.cwd() / f".file_editor_test_{uuid4().hex}.py"

    def tearDown(self) -> None:
        self.path.unlink(missing_ok=True)

    def test_review_and_fix_applies_patch_then_rechecks(self) -> None:
        code = "try:\n    work()\nexcept:\n    pass\n"
        checker = CountingCodeChecker()
        self.path.write_text(code, encoding="utf-8")

        run = CodeReviewAgent(MockReviewer(), checker=checker).review_and_fix(self.path, code)

        self.assertTrue(run.edit.applied)
        self.assertIn("except Exception:", self.path.read_text(encoding="utf-8"))
        self.assertIn("-except:", run.edit.diff)
        self.assertIn("+except Exception:", run.edit.diff)
        self.assertEqual(run.verification_findings, [])
        self.assertEqual(checker.call_count, 2)

    def test_division_by_zero_is_not_unsafely_rewritten(self) -> None:
        code = "value = 10 / 0\n"
        self.path.write_text(code, encoding="utf-8")

        run = CodeReviewAgent(MockReviewer()).review_and_fix(self.path, code)

        self.assertFalse(run.edit.applied)
        self.assertEqual(self.path.read_text(encoding="utf-8"), code)
        self.assertTrue(any("除零问题" in reason for reason in run.edit.skipped_reasons))

    def test_rejects_stale_file_before_applying_patch(self) -> None:
        original = "try:\n    work()\nexcept:\n    pass\n"
        changed = "print('changed')\n"
        findings = CodeChecker().check(original)
        report = CodeReviewAgent(MockReviewer()).review(original)
        self.path.write_text(changed, encoding="utf-8")

        with self.assertRaisesRegex(FileEditError, "过期补丁"):
            FileEditor().apply(self.path, original, report, findings)


if __name__ == "__main__":
    unittest.main()
