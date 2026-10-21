"""Simple deterministic task planner for review tool routing."""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_REVIEW_TASK = "检查用户需求一致性、已有测试失败和明确的 Python 代码正确性问题"


@dataclass(frozen=True)
class ReviewPlan:
    """The Agent's decision about whether local static analysis is useful."""

    use_code_checker: bool
    reason: str


class TaskPlanner:
    """Route a review task using transparent keyword-based intent rules."""

    _STATIC_ANALYSIS_INTENTS = (
        "全面",
        "审查",
        "语法",
        "错误",
        "异常",
        "bug",
        "漏洞",
        "安全",
        "正确性",
        "可靠性",
        "除零",
        "review",
    )
    _NON_STATIC_INTENTS = (
        "解释代码",
        "代码解释",
        "总结代码",
        "代码总结",
        "风格",
        "格式",
        "命名",
        "注释",
        "文档",
        "可读性",
    )

    def plan(self, task: str) -> ReviewPlan:
        normalized = (task or DEFAULT_REVIEW_TASK).strip().lower()
        has_static_intent = any(keyword in normalized for keyword in self._STATIC_ANALYSIS_INTENTS)
        has_non_static_intent = any(keyword in normalized for keyword in self._NON_STATIC_INTENTS)

        if has_static_intent:
            return ReviewPlan(
                use_code_checker=True,
                reason="任务涉及正确性、风险或全面审查，AST 静态检查有助于发现确定性问题",
            )
        if has_non_static_intent:
            return ReviewPlan(
                use_code_checker=False,
                reason="任务仅关注解释、风格或文档，不需要 AST 正确性检查",
            )
        return ReviewPlan(
            use_code_checker=True,
            reason="任务意图不明确，默认执行低成本静态检查以减少漏检",
        )
