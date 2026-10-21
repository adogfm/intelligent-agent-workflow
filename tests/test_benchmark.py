"""Offline tests for the benchmark definitions and runner."""

from __future__ import annotations

import unittest

from benchmarks.cases import BENCHMARK_CASES
from benchmarks.run import run_benchmark


class BenchmarkTests(unittest.TestCase):
    def test_contains_at_least_five_problem_cases(self) -> None:
        problem_cases = [case for case in BENCHMARK_CASES if case.expected_rule_ids]

        self.assertGreaterEqual(len(problem_cases), 5)

    def test_all_benchmark_expectations_pass(self) -> None:
        result = run_benchmark()

        self.assertTrue(result.passed)
        self.assertEqual(result.passed_count, result.total_count)


if __name__ == "__main__":
    unittest.main()
