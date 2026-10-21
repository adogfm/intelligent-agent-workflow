"""Local AST-based checks used as a deterministic Agent tool."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from models import ReviewIssue, Severity


@dataclass(frozen=True)
class CodeCheckFinding:
    """One deterministic finding produced by CodeChecker."""

    rule_id: str
    line: int | None
    problem: str
    severity: Severity
    reason: str
    suggestion: str

    def to_review_issue(self) -> ReviewIssue:
        location = f"第 {self.line} 行：" if self.line is not None else ""
        return ReviewIssue(
            problem=f"{location}{self.problem}",
            severity=self.severity,
            reason=self.reason,
            suggestion=self.suggestion,
        )


class CodeChecker:
    """Parse Python source without executing it and run basic AST checks."""

    def check(self, code: str) -> list[CodeCheckFinding]:
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            return [
                CodeCheckFinding(
                    rule_id="syntax-error",
                    line=exc.lineno,
                    problem=f"语法错误：{exc.msg}",
                    severity="high",
                    reason="代码无法被 Python 解释器正确解析，因此不能正常运行。",
                    suggestion="根据报错行和偏移位置修正语法后重新审查。",
                )
            ]

        visitor = _CodeCheckVisitor()
        visitor.visit(tree)
        return visitor.findings


class _CodeCheckVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[CodeCheckFinding] = []

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.findings.append(
                CodeCheckFinding(
                    rule_id="bare-except",
                    line=node.lineno,
                    problem="使用了裸 except",
                    severity="medium",
                    reason="裸 except 会捕获 KeyboardInterrupt、SystemExit 等异常，可能掩盖程序退出信号和真实故障。",
                    suggestion="只捕获预期异常，例如 `except ValueError:`；确需兜底时使用 `except Exception:` 并记录异常。",
                )
            )
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and _is_zero_literal(node.right):
            operator = {
                ast.Div: "/",
                ast.FloorDiv: "//",
                ast.Mod: "%",
            }[type(node.op)]
            self.findings.append(
                CodeCheckFinding(
                    rule_id="division-by-zero",
                    line=node.lineno,
                    problem=f"运算符 `{operator}` 的右操作数是常量零",
                    severity="high",
                    reason="执行到该表达式时会抛出 ZeroDivisionError。",
                    suggestion="修正分母来源，或在运算前检查分母不为零并明确处理零值情况。",
                )
            )
        self.generic_visit(node)


def _is_zero_literal(node: ast.expr) -> bool:
    """Return True for numeric zero literals, including signed zero."""

    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float, complex)) and not isinstance(node.value, bool) and node.value == 0
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _is_zero_literal(node.operand)
    return False
