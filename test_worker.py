"""Standard-library-only worker executed in a separate Python process."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import traceback
import unittest


def run_tests() -> int:
    result_path = Path("result.json")
    try:
        spec = importlib.util.spec_from_file_location("generated_code", Path("generated_code.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules["generated_code"] = module
        spec.loader.exec_module(module)

        namespace = {"module": module, "unittest": unittest}
        source = Path("generated_tests.py").read_text(encoding="utf-8")
        exec(compile(source, "generated_tests.py", "exec"), namespace)
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(namespace["GeneratedTests"])
        result = unittest.TextTestRunner(verbosity=2).run(suite)

        def case_id(test: unittest.TestCase) -> str:
            return getattr(test, "test_case", test).id()

        failed_ids = {case_id(test) for test, _ in result.failures}
        error_ids = {case_id(test) for test, _ in result.errors}
        not_passed = (
            failed_ids | error_ids
            | {case_id(test) for test, _ in result.skipped}
            | {case_id(test) for test, _ in result.expectedFailures}
            | {case_id(test) for test in result.unexpectedSuccesses}
        )
        passed = max(0, result.testsRun - len(not_passed))
        status = "passed" if result.wasSuccessful() else "failed"
        message = ""
        if result.testsRun == 0 or (result.wasSuccessful() and passed == 0):
            status = "error"
            message = "没有实际通过的测试；空测试或全部跳过不视为通过。"
        payload = {
            "status": status,
            "tests_run": result.testsRun,
            "passed": passed,
            "failed": len(failed_ids),
            "errors": len(error_ids),
            "skipped": len(result.skipped),
            "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
            "failures": [{"test": str(test), "traceback": detail} for test, detail in result.failures],
            "error_details": [{"test": str(test), "traceback": detail} for test, detail in result.errors],
            "message": message,
        }
    except BaseException:
        payload = {
            "status": "error", "errors": 1,
            "error_details": [{"test": "test_setup", "traceback": traceback.format_exc()}],
            "message": "被测模块导入或测试加载失败。",
        }
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(run_tests())
