"""Run the deterministic CodeChecker benchmark."""

from __future__ import annotations

from dataclasses import dataclass

from code_checker import CodeChecker

from benchmarks.cases import BENCHMARK_CASES, BenchmarkCase


@dataclass(frozen=True)
class BenchmarkCaseResult:
    case: BenchmarkCase
    actual_rule_ids: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.actual_rule_ids == self.case.expected_rule_ids


@dataclass(frozen=True)
class BenchmarkResult:
    cases: tuple[BenchmarkCaseResult, ...]

    @property
    def passed_count(self) -> int:
        return sum(case.passed for case in self.cases)

    @property
    def total_count(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> bool:
        return self.passed_count == self.total_count


def run_benchmark(
    cases: tuple[BenchmarkCase, ...] = BENCHMARK_CASES,
    checker: CodeChecker | None = None,
) -> BenchmarkResult:
    active_checker = checker or CodeChecker()
    results = tuple(
        BenchmarkCaseResult(
            case=case,
            actual_rule_ids=tuple(
                finding.rule_id for finding in active_checker.check(case.code)
            ),
        )
        for case in cases
    )
    return BenchmarkResult(cases=results)


def main() -> int:
    result = run_benchmark()
    print("CodeChecker Benchmark")
    print("=" * 72)
    for item in result.cases:
        status = "PASS" if item.passed else "FAIL"
        expected = ", ".join(item.case.expected_rule_ids) or "none"
        actual = ", ".join(item.actual_rule_ids) or "none"
        print(f"[{status}] {item.case.case_id}: {item.case.description}")
        print(f"       expected={expected}; actual={actual}")
    print("-" * 72)
    print(f"Result: {result.passed_count}/{result.total_count} cases passed")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
