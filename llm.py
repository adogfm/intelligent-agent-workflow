"""LLM abstraction and Qwen OpenAI-compatible implementation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Protocol

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, AuthenticationError, OpenAI, RateLimitError
from pydantic import ValidationError

from code_checker import CodeCheckFinding
from models import (
    CodeGenerationResult,
    ReviewContext,
    ReviewReport,
    TestCase,
    TestExecutionResult,
    TestGenerationResult,
    generated_code_syntax_error,
)
from planner import ReviewPlan
from prompt import (
    CODING_SYSTEM_PROMPT,
    FIX_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    TEST_SYSTEM_PROMPT,
    build_coding_input,
    build_fix_input,
    build_review_input,
    build_test_input,
)


class ConfigurationError(RuntimeError):
    """Raised when required local configuration is missing."""


class LLMReviewError(RuntimeError):
    """Raised when the remote review cannot be completed."""


class LLMGenerationError(RuntimeError):
    """Raised when code generation or output validation fails."""


class LLMCodeSyntaxError(LLMGenerationError):
    """A parsed code response failed Python syntax validation."""

    def __init__(self, message: str, *, generated_code: str, reason: str) -> None:
        super().__init__(message)
        self.generated_code = generated_code
        self.reason = reason


class LLMTestGenerationError(RuntimeError):
    """Raised when test generation or output validation fails."""


class LLMFixError(RuntimeError):
    """Raised when code repair or its output validation fails."""


class LLMFixSyntaxError(LLMFixError):
    """Retryable syntax failure, distinguished from transport/schema errors."""

    def __init__(self, message: str, *, generated_code: str, reason: str) -> None:
        super().__init__(message)
        self.generated_code = generated_code
        self.reason = reason


DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_QWEN_MODEL = "qwen-plus"


@dataclass(frozen=True)
class LLMSettings:
    api_key: str
    base_url: str
    model: str


class Reviewer(Protocol):
    is_mock: bool

    def review(
        self,
        code: str,
        tool_findings: list[CodeCheckFinding],
        *,
        task: str,
        plan: ReviewPlan,
    ) -> ReviewReport:
        """Review Python source code and return a structured report."""


class ContextualReviewer(Reviewer, Protocol):
    """Optional extension; existing Reviewer implementations remain valid."""

    def review_with_context(
        self, code: str, tool_findings: list[CodeCheckFinding], *, task: str,
        plan: ReviewPlan, review_context: ReviewContext,
    ) -> ReviewReport:
        """Review the current task and actual test evidence, not generic standards."""


class CodeGenerator(Protocol):
    """Provider-independent Python code generation interface."""

    is_mock: bool

    def generate(self, requirements: str, *, context: str = "") -> CodeGenerationResult:
        """Generate source code from a requirement and optional background."""


class CodeFixer(Protocol):
    is_mock: bool

    def fix(
        self,
        code: str,
        *,
        report: ReviewReport | None,
        test_result: TestExecutionResult | None,
        test_cases: list[TestCase],
        requirements: str = "",
        context: str = "",
    ) -> CodeGenerationResult:
        """Repair source using review findings and test diagnostics."""


class MockCodeFixer:
    """Offline repair using the existing conservative FileEditor rules."""

    is_mock = True

    def fix(
        self,
        code: str,
        *,
        report: ReviewReport | None,
        test_result: TestExecutionResult | None,
        test_cases: list[TestCase],
        requirements: str = "",
        context: str = "",
    ) -> CodeGenerationResult:
        from pathlib import Path
        from code_checker import CodeChecker
        from file_editor import FileEditor

        patch = FileEditor().generate_patch(
            Path("generated_code.py"), code,
            report if report is not None else ReviewReport(summary="没有审查问题", issues=[]),
            CodeChecker().check(code),
        )
        return CodeGenerationResult(
            code=patch.updated_code,
            summary=(
                "[MOCK MODE] 已按本地白名单规则修改裸 except，需后续审查和测试确认。"
                if patch.fixed_rules else
                "[MOCK MODE] 没有可按本地白名单规则修改的问题，代码保持不变；"
                "业务逻辑或测试失败需真实模型修复。"
            ),
        )


class OpenAICodeFixer:
    """Repair Python source through the existing Qwen-compatible Chat API."""

    is_mock = False

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        model: str | None = None,
        client: OpenAI | None = None,
    ) -> None:
        load_dotenv()
        settings = _load_settings(api_key=api_key, base_url=base_url, model=model)
        self._model = settings.model
        self._base_url = settings.base_url
        self._client = client or OpenAI(api_key=settings.api_key, base_url=settings.base_url)

    def fix(
        self,
        code: str,
        *,
        report: ReviewReport | None,
        test_result: TestExecutionResult | None,
        test_cases: list[TestCase],
        requirements: str = "",
        context: str = "",
    ) -> CodeGenerationResult:
        if not code.strip():
            raise ValueError("待修复代码不能为空。")
        try:
            completion = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": FIX_SYSTEM_PROMPT},
                    {"role": "user", "content": build_fix_input(
                        code, report, test_result, test_cases, requirements, context
                    )},
                ],
                response_format={"type": "json_object"},
                extra_body={"enable_thinking": False},
            )
            if not completion.choices:
                raise LLMFixError("模型未返回任何修复结果。")
            choice = completion.choices[0]
            if choice.finish_reason == "length":
                raise LLMFixError("修复响应因长度限制被截断。")
            if choice.finish_reason == "content_filter":
                raise LLMFixError("修复响应被内容过滤器中断。")
            refusal = getattr(choice.message, "refusal", None)
            if refusal:
                raise LLMFixError(f"模型拒绝修复代码：{refusal}")
            return parse_code_generation(choice.message.content)
        except AuthenticationError as exc:
            raise LLMFixError("API Key 无效或无权访问所选模型。") from exc
        except RateLimitError as exc:
            raise LLMFixError("API 请求达到速率或额度限制，请稍后重试。") from exc
        except APIConnectionError as exc:
            raise LLMFixError("无法连接 LLM 服务，请检查网络。") from exc
        except APIStatusError as exc:
            raise LLMFixError(f"LLM 服务返回错误（HTTP {exc.status_code}）。") from exc
        except LLMCodeSyntaxError as exc:
            raise LLMFixSyntaxError(
                f"修复输出无效：{exc}", generated_code=exc.generated_code, reason=exc.reason,
            ) from exc
        except LLMGenerationError as exc:
            raise LLMFixError(f"修复输出无效：{exc}") from exc
        except LLMFixError:
            raise
        except Exception as exc:
            raise LLMFixError(f"无法解析代码修复响应：{exc}") from exc


class TestGenerator(Protocol):
    is_mock: bool

    def generate(
        self, code: str, *, requirements: str = "", context: str = ""
    ) -> TestGenerationResult:
        """Generate unittest method bodies for the supplied source."""


class MockTestGenerator:
    """Offline import smoke test; does not verify business requirements."""

    is_mock = True

    def generate(
        self, code: str, *, requirements: str = "", context: str = ""
    ) -> TestGenerationResult:
        if not code.strip():
            raise ValueError("被测代码不能为空。")
        return TestGenerationResult(
            test_cases=[{"name": "test_module_loaded", "code": "self.assertIsNotNone(module)"}],
            summary="[MOCK MODE] 仅验证被测模块能够导入，不验证用户需求或业务正确性。",
        )


class OpenAITestGenerator:
    """Generate structured tests through the existing Qwen-compatible API."""

    is_mock = False

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        model: str | None = None,
        client: OpenAI | None = None,
    ) -> None:
        load_dotenv()
        settings = _load_settings(api_key=api_key, base_url=base_url, model=model)
        self._model = settings.model
        self._base_url = settings.base_url
        self._client = client or OpenAI(api_key=settings.api_key, base_url=settings.base_url)

    def generate(
        self, code: str, *, requirements: str = "", context: str = ""
    ) -> TestGenerationResult:
        if not code.strip():
            raise ValueError("被测代码不能为空。")
        try:
            completion = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": TEST_SYSTEM_PROMPT},
                    {"role": "user", "content": build_test_input(code, requirements, context)},
                ],
                response_format={"type": "json_object"},
                extra_body={"enable_thinking": False},
            )
            if not completion.choices:
                raise LLMTestGenerationError("模型未返回任何测试用例。")
            choice = completion.choices[0]
            if choice.finish_reason == "length":
                raise LLMTestGenerationError("测试生成响应因长度限制被截断。")
            if choice.finish_reason == "content_filter":
                raise LLMTestGenerationError("测试生成响应被内容过滤器中断。")
            refusal = getattr(choice.message, "refusal", None)
            if refusal:
                raise LLMTestGenerationError(f"模型拒绝生成测试：{refusal}")
            return parse_test_generation(choice.message.content)
        except AuthenticationError as exc:
            raise LLMTestGenerationError("API Key 无效或无权访问所选模型。") from exc
        except RateLimitError as exc:
            raise LLMTestGenerationError("API 请求达到速率或额度限制，请稍后重试。") from exc
        except APIConnectionError as exc:
            raise LLMTestGenerationError("无法连接 LLM 服务，请检查网络。") from exc
        except APIStatusError as exc:
            raise LLMTestGenerationError(f"LLM 服务返回错误（HTTP {exc.status_code}）。") from exc
        except LLMTestGenerationError:
            raise
        except Exception as exc:
            raise LLMTestGenerationError(f"无法解析测试生成响应：{exc}") from exc


def parse_test_generation(content: str | None) -> TestGenerationResult:
    if not content or not content.strip():
        raise LLMTestGenerationError("模型返回了空测试内容。")
    try:
        return TestGenerationResult.model_validate_json(_strip_json_fence(content.strip()))
    except ValidationError as exc:
        raise LLMTestGenerationError(f"模型 JSON 不符合 TestGenerationResult：{exc}") from exc


class MockCodeGenerator:
    """Offline scaffold for workflow testing, not a requirements implementation."""

    is_mock = True

    def generate(self, requirements: str, *, context: str = "") -> CodeGenerationResult:
        if not requirements.strip():
            raise ValueError("用户需求不能为空。")
        return CodeGenerationResult(
            code=(
                '"""[MOCK MODE] Offline workflow scaffold."""\n\n'
                "def get_requirements() -> str:\n"
                '    """Return the supplied requirement for offline workflow testing."""\n'
                f"    return {requirements!r}\n"
            ),
            summary=(
                "[MOCK MODE] 仅生成保存需求的离线示例代码，未实现需求中的业务功能。"
                "配置有效 DASHSCOPE_API_KEY 后可使用真实代码生成。"
            ),
        )


class OpenAICodeGenerator:
    """Qwen code generation using the project's existing compatible Chat API."""

    is_mock = False

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        model: str | None = None,
        client: OpenAI | None = None,
    ) -> None:
        load_dotenv()
        settings = _load_settings(api_key=api_key, base_url=base_url, model=model)
        self._model = settings.model
        self._base_url = settings.base_url
        self._client = client or OpenAI(api_key=settings.api_key, base_url=settings.base_url)

    def generate(self, requirements: str, *, context: str = "") -> CodeGenerationResult:
        if not requirements.strip():
            raise ValueError("用户需求不能为空。")
        try:
            completion = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": CODING_SYSTEM_PROMPT},
                    {"role": "user", "content": build_coding_input(requirements, context)},
                ],
                response_format={"type": "json_object"},
                extra_body={"enable_thinking": False},
            )
            if not completion.choices:
                raise LLMGenerationError("模型未返回任何候选结果。")
            choice = completion.choices[0]
            if choice.finish_reason == "length":
                raise LLMGenerationError("生成代码响应因长度限制被截断。")
            if choice.finish_reason == "content_filter":
                raise LLMGenerationError("生成代码响应被内容过滤器中断。")
            refusal = getattr(choice.message, "refusal", None)
            if refusal:
                raise LLMGenerationError(f"模型拒绝生成代码：{refusal}")
            return parse_code_generation(choice.message.content)
        except AuthenticationError as exc:
            raise LLMGenerationError("API Key 无效或无权访问所选模型。") from exc
        except RateLimitError as exc:
            raise LLMGenerationError("API 请求达到速率或额度限制，请稍后重试。") from exc
        except APIConnectionError as exc:
            raise LLMGenerationError("无法连接 LLM 服务，请检查网络。") from exc
        except APIStatusError as exc:
            raise LLMGenerationError(f"LLM 服务返回错误（HTTP {exc.status_code}）。") from exc
        except LLMGenerationError:
            raise
        except Exception as exc:
            raise LLMGenerationError(f"无法解析代码生成响应：{exc}") from exc


def parse_code_generation(content: str | None) -> CodeGenerationResult:
    """Validate structured output and Python syntax without executing source."""

    if not content or not content.strip():
        raise LLMGenerationError("模型返回了空内容。")
    try:
        return CodeGenerationResult.model_validate_json(_strip_json_fence(content.strip()))
    except ValidationError as exc:
        syntax = generated_code_syntax_error(exc)
        if syntax is not None:
            raise LLMCodeSyntaxError(
                f"模型 JSON 不符合 CodeGenerationResult：{exc}",
                generated_code=syntax.code, reason=syntax.reason,
            ) from exc
        raise LLMGenerationError(f"模型 JSON 不符合 CodeGenerationResult：{exc}") from exc


class MockReviewer:
    """Deterministic offline reviewer used when no API key is configured."""

    is_mock = True

    def review_with_context(
        self, code: str, tool_findings: list[CodeCheckFinding], *, task: str,
        plan: ReviewPlan, review_context: ReviewContext,
    ) -> ReviewReport:
        return self.review(code, tool_findings, task=task, plan=plan)

    def review(
        self,
        code: str,
        tool_findings: list[CodeCheckFinding],
        *,
        task: str,
        plan: ReviewPlan,
    ) -> ReviewReport:
        if not code.strip():
            raise ValueError("Python 代码不能为空。")

        return ReviewReport.model_validate(
            {
                "summary": "[MOCK MODE] 离线审查已完成；这是固定测试结果，不代表真实代码分析。",
                "issues": [
                    {
                        "problem": "示例问题：关键逻辑缺少自动化测试",
                        "severity": "low",
                        "reason": "这是用于验证离线审查流程和输出格式的固定 Mock 结果。",
                        "suggestion": "配置真实 DASHSCOPE_API_KEY 后重新运行，以获得针对代码内容的审查。",
                    }
                ],
            }
        )


class OpenAIReviewer:
    """Qwen reviewer implemented through its OpenAI-compatible Chat API."""

    is_mock = False

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        model: str | None = None,
        client: OpenAI | None = None,
    ) -> None:
        load_dotenv()
        settings = _load_settings(api_key=api_key, base_url=base_url, model=model)
        self._model = settings.model
        self._base_url = settings.base_url
        self._client = client or OpenAI(api_key=settings.api_key, base_url=settings.base_url)

    def review(
        self,
        code: str,
        tool_findings: list[CodeCheckFinding],
        *,
        task: str,
        plan: ReviewPlan,
    ) -> ReviewReport:
        return self.review_with_context(
            code, tool_findings, task=task, plan=plan, review_context=ReviewContext(),
        )

    def review_with_context(
        self, code: str, tool_findings: list[CodeCheckFinding], *, task: str,
        plan: ReviewPlan, review_context: ReviewContext,
    ) -> ReviewReport:
        try:
            completion = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": build_review_input(
                            code, tool_findings, task, plan, review_context=review_context,
                        ),
                    },
                ],
                response_format={"type": "json_object"},
                extra_body={"enable_thinking": False},
            )
            if not completion.choices:
                raise LLMReviewError("模型未返回任何候选结果。")

            choice = completion.choices[0]
            if choice.finish_reason == "length":
                raise LLMReviewError("模型响应因长度限制被截断。")
            if choice.finish_reason == "content_filter":
                raise LLMReviewError("模型响应被内容过滤器中断。")

            refusal = getattr(choice.message, "refusal", None)
            if refusal:
                raise LLMReviewError(f"模型拒绝执行审查：{refusal}")
            return parse_review_report(choice.message.content)
        except AuthenticationError as exc:
            raise LLMReviewError("API Key 无效或无权访问所选模型。") from exc
        except RateLimitError as exc:
            raise LLMReviewError("API 请求达到速率或额度限制，请稍后重试。") from exc
        except APIConnectionError as exc:
            raise LLMReviewError("无法连接 LLM 服务，请检查网络。") from exc
        except APIStatusError as exc:
            raise LLMReviewError(f"LLM 服务返回错误（HTTP {exc.status_code}）。") from exc
        except LLMReviewError:
            raise
        except Exception as exc:
            raise LLMReviewError(f"无法解析模型响应：{exc}") from exc


def parse_review_report(content: str | None) -> ReviewReport:
    """Parse Chat Completions text into a validated ReviewReport."""

    if not content or not content.strip():
        raise LLMReviewError("模型返回了空内容。")

    candidate = _strip_json_fence(content.strip())
    try:
        return ReviewReport.model_validate_json(candidate)
    except (JSONDecodeError, ValidationError):
        pass

    object_start = candidate.find("{")
    if object_start < 0:
        raise LLMReviewError("模型响应不包含 JSON 对象。")
    try:
        payload, _ = json.JSONDecoder().raw_decode(candidate[object_start:])
        return ReviewReport.model_validate(payload)
    except (JSONDecodeError, ValidationError, TypeError) as exc:
        raise LLMReviewError(f"模型 JSON 不符合 ReviewReport：{exc}") from exc


def _strip_json_fence(content: str) -> str:
    if not content.startswith("```"):
        return content
    lines = content.splitlines()
    if lines and lines[0].strip().lower() in {"```", "```json"}:
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _load_settings(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> LLMSettings:
    resolved_api_key = (api_key or _first_env("DASHSCOPE_API_KEY", "OPENAI_API_KEY")).strip()
    if not _has_real_api_key(resolved_api_key):
        raise ConfigurationError(
            "请在 .env 中设置有效的 DASHSCOPE_API_KEY（或兼容的 OPENAI_API_KEY）。"
        )
    resolved_base_url = (
        base_url
        or _first_env("DASHSCOPE_BASE_URL", "OPENAI_BASE_URL")
        or DEFAULT_QWEN_BASE_URL
    ).rstrip("/")
    resolved_model = (
        model or _first_env("QWEN_MODEL", "OPENAI_MODEL") or DEFAULT_QWEN_MODEL
    )
    return LLMSettings(
        api_key=resolved_api_key,
        base_url=resolved_base_url,
        model=resolved_model,
    )


def _has_real_api_key(api_key: str) -> bool:
    return bool(api_key) and api_key.lower() not in {
        "your_api_key_here",
        "sk-你的_api_key",
        "sk-your_api_key",
    }


def create_reviewer() -> Reviewer:
    """Select the real LLM when configured, otherwise use deterministic Mock mode."""

    load_dotenv()
    api_key = _first_env("DASHSCOPE_API_KEY", "OPENAI_API_KEY")
    if not _has_real_api_key(api_key):
        return MockReviewer()
    return OpenAIReviewer(api_key=api_key)


def create_code_generator() -> CodeGenerator:
    """Select the code generation provider using the existing LLM settings."""

    load_dotenv()
    api_key = _first_env("DASHSCOPE_API_KEY", "OPENAI_API_KEY")
    if not _has_real_api_key(api_key):
        return MockCodeGenerator()
    return OpenAICodeGenerator(api_key=api_key)


def create_test_generator() -> TestGenerator:
    load_dotenv()
    api_key = _first_env("DASHSCOPE_API_KEY", "OPENAI_API_KEY")
    if not _has_real_api_key(api_key):
        return MockTestGenerator()
    return OpenAITestGenerator(api_key=api_key)


def create_code_fixer() -> CodeFixer:
    load_dotenv()
    api_key = _first_env("DASHSCOPE_API_KEY", "OPENAI_API_KEY")
    if not _has_real_api_key(api_key):
        return MockCodeFixer()
    return OpenAICodeFixer(api_key=api_key)
