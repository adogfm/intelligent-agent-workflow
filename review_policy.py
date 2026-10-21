"""Task-bounded review classification and deterministic scoring."""

from __future__ import annotations

import re

from models import ReviewContext, ReviewIssue, ReviewReport
from planner import DEFAULT_REVIEW_TASK


def _standard_required(issue: ReviewIssue, context: ReviewContext, task: str) -> bool:
    reference = issue.requirement_ref.strip()
    if not reference:
        return False
    explicit_task = task if task != DEFAULT_REVIEW_TASK else ""
    reference = reference.casefold()
    # A generic quote such as "password" or "register_user" does not request
    # a production security standard. Nor does "bcrypt is not required".
    standards = (
        "安全规范", "安全标准", "合规", "生产级", "哈希", "散列", "加盐", "加密",
        "bcrypt", "argon", "scrypt", "pbkdf", "hash", "encrypt", "salt",
        "security standard", "compliance", "owasp", "nist",
    )
    if not any(term in reference for term in standards):
        return False
    clauses = re.split(r"[\n，,。；;！？!?]", (context.requirements + "\n" + explicit_task).casefold())
    excluded = ("不要求", "不需要", "无需", "不用", "不使用", "禁止", "不得", "不必",
                "not required", "do not", "don't", "without", "no need")
    return any(reference in clause and not any(term in clause for term in excluded) for clause in clauses)


def apply_review_policy(
    report: ReviewReport,
    tool_issues: list[ReviewIssue],
    context: ReviewContext,
    task: str,
) -> ReviewReport:
    """Keep concrete blockers, move optional advice out of the fix loop."""

    # Local deterministic findings cannot be downgraded by the Reviewer.
    blockers = list(tool_issues)
    recommendations = list(report.recommendations)
    failed_tests = context.test_result is not None and context.test_result.status != "passed"
    for issue in report.issues:
        optional = (
            issue.scope == "out_of_scope" or issue.category == "improvement"
            or (issue.category == "security_standard" and not _standard_required(issue, context, task))
            or (issue.category == "test_failure" and not failed_tests)
        )
        if optional:
            recommendations.append(issue)
        else:
            blockers.append(issue)

    if failed_tests and not any(issue.category == "test_failure" for issue in blockers):
        result = context.test_result
        names = [failure.test_name or failure.test for failure in [*result.failures, *result.error_details]]
        evidence = (f"status={result.status}; tests_run={result.tests_run}; "
                    f"failed={result.failed}; errors={result.errors}; "
                    f"tests={', '.join(names)}; message={result.message}")
        blockers.append(ReviewIssue(
            problem=f"Test Agent 测试未通过（{result.status}）",
            severity="high", reason="当前代码尚未通过已有测试，不能判定任务完成。",
            suggestion="根据原用例、期望错误提示和 traceback 修复代码，保留原断言并重新测试。",
            category="test_failure", evidence=evidence,
        ))

    penalties = {"critical": 40, "high": 25, "medium": 15, "low": 5}
    deduction = sum(
        40 if issue.category in {"requirement", "test_failure", "security_standard"}
        else penalties[issue.severity]
        for issue in blockers
    )
    return ReviewReport(
        summary=report.summary,
        issues=blockers,
        recommendations=recommendations,
        score=max(0, 100 - deduction),
        test_status=context.test_result.status if context.test_result is not None else "not_run",
    )
