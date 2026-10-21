"""Preserve original test assertions as repair evidence without executing them."""

from __future__ import annotations

import ast
import re

from models import TestCase, TestExecutionResult, TestExpectation, TestFailure


def extract_test_expectations(case: TestCase) -> list[TestExpectation]:
    """Extract literal string expectations and unambiguous local constants.

    Dynamic expressions are intentionally not evaluated. The full method body
    is retained on failures, so unsupported assertions still remain available.
    """

    tree = ast.parse(case.code)
    bindings: dict[str, str | None] = {}

    def bind(target: ast.AST, value: ast.AST | None) -> None:
        names = [node.id for node in ast.walk(target) if isinstance(node, ast.Name)]
        literal = (value.value if isinstance(target, ast.Name) and isinstance(value, ast.Constant)
                   and isinstance(value.value, str) else None)
        for name in names:
            if name in bindings and bindings[name] != literal:
                bindings[name] = None
            else:
                bindings[name] = literal

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                bind(node.target, node.value)
        elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
            bind(node.target, None)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            bind(node.target, None)

    def strings(node: ast.AST) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.Name) and bindings.get(node.id) is not None:
            return [bindings[node.id]]
        if isinstance(node, ast.Dict):
            return [value for key, item in zip(node.keys, node.values)
                    if isinstance(key, ast.Constant) and key.value in {"error", "message", "detail"}
                    for value in strings(item)]
        return []

    assertions = {
        "assertRaisesRegex": (1, "regex", "expected_regex"),
        "assertRegex": (1, "regex", "expected_regex"),
        "assertNotRegex": (1, "regex", "unexpected_regex"),
        "assertEqual": (1, "exact", "second"),
        "assertNotEqual": (1, "exact", "second"),
        "assertIn": (0, "contains", "member"),
        "assertNotIn": (0, "contains", "member"),
    }
    expectations: list[TestExpectation] = []
    seen: set[tuple[str, str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "self":
            continue
        assertion = node.func.attr
        if assertion not in assertions:
            continue
        index, kind, keyword = assertions[assertion]
        expected = node.args[index] if len(node.args) > index else next(
            (item.value for item in node.keywords if item.arg == keyword), None
        )
        if expected is None:
            continue
        for value in strings(expected):
            key = (assertion, kind, value)
            if key not in seen:
                expectations.append(TestExpectation(assertion=assertion, expected=value, kind=kind))
                seen.add(key)
    return expectations


def enrich_test_result(result: TestExecutionResult, test_cases: list[TestCase]) -> TestExecutionResult:
    """Attach original method bodies/expectations to failures and runtime errors."""

    cases = {case.name: case for case in test_cases}
    contracts = {case.name: extract_test_expectations(case) for case in test_cases}

    def enrich(failure: TestFailure) -> TestFailure:
        name = failure.test_name if failure.test_name in cases else next(
            (candidate for candidate in re.findall(r"\btest_[A-Za-z0-9_]+\b", failure.test) if candidate in cases),
            None,
        )
        if name is None:
            return failure.model_copy(deep=True)
        return failure.model_copy(update={
            "test_name": name, "test_code": cases[name].code,
            "expectations": contracts[name],
        }, deep=True)

    enriched = result.model_copy(deep=True)
    enriched.failures = [enrich(failure) for failure in enriched.failures]
    enriched.error_details = [enrich(failure) for failure in enriched.error_details]
    # Preserve embedded contracts when callers have only a saved failure result.
    enriched.test_expectations.update(contracts)
    return enriched
