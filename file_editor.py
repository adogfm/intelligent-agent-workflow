"""Local file editing tool for safe, deterministic review fixes."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

from code_checker import CodeCheckFinding
from models import ReviewReport


class FileEditError(RuntimeError):
    """Raised when a patch cannot be generated or safely applied."""


@dataclass(frozen=True)
class GeneratedPatch:
    updated_code: str
    diff: str
    fixed_rules: tuple[str, ...]
    skipped_reasons: tuple[str, ...]


@dataclass(frozen=True)
class FileEditResult:
    path: Path
    updated_code: str
    diff: str
    applied: bool
    fixed_rules: tuple[str, ...]
    skipped_reasons: tuple[str, ...]


class FileEditor:
    """Generate and apply conservative source edits from structured findings."""

    def generate_patch(
        self,
        path: Path,
        code: str,
        report: ReviewReport,
        findings: list[CodeCheckFinding],
    ) -> GeneratedPatch:
        lines = code.splitlines(keepends=True)
        fixed_rules: list[str] = []
        skipped_reasons: list[str] = []
        reported_problems = "\n".join(issue.problem for issue in report.issues)

        for finding in sorted(findings, key=lambda item: item.line or 0, reverse=True):
            if finding.problem not in reported_problems:
                skipped_reasons.append(f"规则 {finding.rule_id} 未出现在审查报告中，拒绝应用")
                continue
            if finding.rule_id == "bare-except" and finding.line is not None:
                index = finding.line - 1
                if not 0 <= index < len(lines):
                    skipped_reasons.append(f"bare-except 第 {finding.line} 行超出文件范围")
                    continue
                updated_line, count = re.subn(
                    r"^(\s*)except(\s*):(.*)$",
                    r"\1except Exception\2:\3",
                    lines[index],
                    count=1,
                )
                if count:
                    lines[index] = updated_line
                    fixed_rules.append(f"bare-except@{finding.line}")
                else:
                    skipped_reasons.append(f"bare-except 第 {finding.line} 行不符合安全替换格式")
            elif finding.rule_id == "syntax-error":
                skipped_reasons.append("语法错误需要结合代码意图人工修复")
            elif finding.rule_id == "division-by-zero":
                skipped_reasons.append(
                    f"第 {finding.line or '未知'} 行除零问题需要明确合法分母，未自动改写业务逻辑"
                )
            else:
                skipped_reasons.append(f"规则 {finding.rule_id} 暂无安全自动修复")

        updated_code = "".join(lines)
        diff = "".join(
            difflib.unified_diff(
                code.splitlines(keepends=True),
                updated_code.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path),
            )
        )
        return GeneratedPatch(
            updated_code=updated_code,
            diff=diff,
            fixed_rules=tuple(fixed_rules),
            skipped_reasons=tuple(skipped_reasons),
        )

    def apply(
        self,
        path: Path,
        original_code: str,
        report: ReviewReport,
        findings: list[CodeCheckFinding],
    ) -> FileEditResult:
        if path.suffix.lower() != ".py":
            raise FileEditError("FileEditor 仅允许修改 .py 文件。")

        try:
            current_code = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise FileEditError(f"无法读取待修改文件：{exc}") from exc
        if current_code != original_code:
            raise FileEditError("文件内容在审查后已发生变化，拒绝应用过期补丁。")

        patch = self.generate_patch(path, original_code, report, findings)
        if not patch.diff:
            return FileEditResult(
                path=path,
                updated_code=original_code,
                diff="",
                applied=False,
                fixed_rules=patch.fixed_rules,
                skipped_reasons=patch.skipped_reasons,
            )

        try:
            path.write_text(patch.updated_code, encoding="utf-8")
        except OSError as exc:
            raise FileEditError(f"无法应用补丁：{exc}") from exc

        return FileEditResult(
            path=path,
            updated_code=patch.updated_code,
            diff=patch.diff,
            applied=True,
            fixed_rules=patch.fixed_rules,
            skipped_reasons=patch.skipped_reasons,
        )
