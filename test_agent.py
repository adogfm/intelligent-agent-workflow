"""Generate and execute Python tests as one stateful workflow node."""

from __future__ import annotations

import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

from pydantic import ValidationError

from llm import LLMTestGenerationError, TestGenerator
from models import TestCase, TestExecutionResult, TestGenerationResult
from test_feedback import enrich_test_result
from workflow import AgentResult, BaseAgent, StateView, WorkflowEngine


class TestRunner:
    """Execute tests in a temporary directory with a time limit.

    This is process isolation, not an OS security sandbox. Generated code is
    actually executed and retains the current user's OS permissions.
    """

    def __init__(self, *, timeout_seconds: float = 10.0, max_output_bytes: int = 64 * 1024) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("测试超时必须是有限的正数。")
        if not isinstance(max_output_bytes, int) or max_output_bytes < 1:
            raise ValueError("输出限制必须是正整数。")
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    def run(self, code: str, test_cases: list[TestCase]) -> TestExecutionResult:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("被测代码必须是非空字符串。")
        # Validate standalone runner callers as well as workflow providers.
        cases = TestGenerationResult(
            test_cases=[case.model_dump() if isinstance(case, TestCase) else case for case in test_cases],
            summary="执行测试",
        ).test_cases
        source = "class GeneratedTests(unittest.TestCase):\n"
        for case in cases:
            source += f"    def {case.name}(self):\n" + textwrap.indent(case.code, "        ") + "\n\n"

        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="agent_tests_") as directory:
            root = Path(directory)
            (root / "generated_code.py").write_text(code, encoding="utf-8")
            (root / "generated_tests.py").write_text(source, encoding="utf-8")
            environment = {
                key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ
            }
            environment.update({"TEMP": directory, "TMP": directory, "TMPDIR": directory})
            timed_out = False
            with (root / "stdout.log").open("wb") as stdout, (root / "stderr.log").open("wb") as stderr:
                process = subprocess.Popen(
                    [sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).with_name("test_worker.py"))],
                    cwd=root,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=os.name != "nt",
                )
                try:
                    process.wait(timeout=self.timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self._stop_process(process)
                except BaseException:
                    self._stop_process(process)
                    raise

            common = {
                "stdout": self._read_output(root / "stdout.log"),
                "stderr": self._read_output(root / "stderr.log"),
                "duration_seconds": time.monotonic() - started,
                "return_code": process.returncode,
            }
            if timed_out:
                return TestExecutionResult(
                    status="timeout", message=f"测试执行超过 {self.timeout_seconds:g} 秒，已终止。", **common
                )
            result_path = root / "result.json"
            if not result_path.exists() or result_path.stat().st_size > 1024 * 1024:
                return TestExecutionResult(
                    status="error", errors=1, message="测试进程未返回有效结果。", **common
                )
            try:
                result = TestExecutionResult.model_validate_json(result_path.read_text(encoding="utf-8"))
            except (ValidationError, UnicodeError, OSError):
                return TestExecutionResult(
                    status="error", errors=1, message="测试进程返回的结果无法解析。", **common
                )
            if result.status == "passed" and (
                process.returncode != 0 or result.tests_run != len(cases) or result.passed == 0
            ):
                result.status = "error"
                result.message = "测试结果与进程退出状态或预期用例数量不一致。"
            return result.model_copy(update=common)

    def _read_output(self, path: Path) -> str:
        with path.open("rb") as stream:
            data = stream.read(self.max_output_bytes + 1)
        truncated = len(data) > self.max_output_bytes
        output = data[:self.max_output_bytes].decode("utf-8", errors="replace")
        return output + ("\n[输出已截断]" if truncated else "")

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if os.name == "nt":
            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            try:
                subprocess.run(
                    [str(Path(system_root) / "System32" / "taskkill.exe"),
                     "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait()


class TestAgent(BaseAgent):
    """Publish generated test cases and their actual execution outcome."""

    name = "test"

    def __init__(self, generator: TestGenerator, runner: TestRunner | None = None) -> None:
        self._generator = generator
        self._runner = runner if runner is not None else TestRunner()

    def run(self, state: StateView) -> AgentResult:
        code = state.require("code", str)
        if not code.strip():
            raise ValueError("被测代码不能为空。")
        requirements = state.get("requirements", "")
        context = state.get("context", "")
        if not isinstance(requirements, str) or not isinstance(context, str):
            raise TypeError("requirements 和 context 必须是字符串。")
        reuse_cases = state.get("reuse_test_cases", False)
        if not isinstance(reuse_cases, bool):
            raise TypeError("reuse_test_cases 必须是 bool。")
        if reuse_cases:
            cases = state.require("test_cases", list)
            previous = state.get("test_generation_result")
            summary = previous.summary if isinstance(previous, TestGenerationResult) else "已有测试用例"
            generation = TestGenerationResult(
                test_cases=[case.model_dump() if isinstance(case, TestCase) else case for case in cases],
                summary=f"{summary} 复用原有测试用例进行复验，未重新生成测试。",
            )
        else:
            generation = self._generator.generate(code, requirements=requirements, context=context)
        if not isinstance(generation, TestGenerationResult):
            raise LLMTestGenerationError("TestGenerator 必须返回 TestGenerationResult。")
        try:
            generation = TestGenerationResult.model_validate(generation.model_dump())
        except ValidationError as exc:
            raise LLMTestGenerationError(f"测试生成结果无效：{exc}") from exc
        result = self._runner.run(code, generation.test_cases)
        if not isinstance(result, TestExecutionResult):
            raise TypeError("TestRunner 必须返回 TestExecutionResult。")
        result = enrich_test_result(result, generation.test_cases)
        updates = {
            "test_cases": generation.test_cases,
            "test_result": result,
            "test_generation_result": generation,
        }
        if reuse_cases:
            updates["reuse_test_cases"] = False
        return AgentResult(updates=updates, output=result)


def create_test_workflow(agent: TestAgent) -> WorkflowEngine:
    """Build a test-only workflow from an existing ``code`` state value."""

    return WorkflowEngine("test").add_step("test", agent)
