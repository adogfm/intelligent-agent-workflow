"""CLI regressions for legacy Windows output encodings."""

from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from main import main


class CLIEncodingTests(unittest.TestCase):
    def test_help_writes_utf8_before_argument_parsing_exits(self) -> None:
        with io.BytesIO() as output, io.BytesIO() as errors:
            with io.TextIOWrapper(output, encoding="cp1252") as stdout, \
                    io.TextIOWrapper(errors, encoding="cp1252") as stderr, \
                    patch("sys.stdout", stdout), patch("sys.stderr", stderr), \
                    patch("sys.argv", ["code-review-agent", "--help"]):
                with self.assertRaises(SystemExit) as caught:
                    main()

                stdout.flush()
                stderr.flush()
                self.assertEqual(caught.exception.code, 0)
                self.assertIn("使用 Agent 审查 Python 代码", output.getvalue().decode("utf-8"))
                self.assertIn(b"--requirements", output.getvalue())
                self.assertEqual(errors.getvalue(), b"")

    def test_input_error_writes_utf8_to_legacy_stderr(self) -> None:
        with io.BytesIO() as output, io.BytesIO() as errors:
            with io.TextIOWrapper(output, encoding="cp1252") as stdout, \
                    io.TextIOWrapper(errors, encoding="cp1252") as stderr, \
                    patch("sys.stdout", stdout), patch("sys.stderr", stderr), \
                    patch("sys.argv", ["code-review-agent", "--fix-code"]):
                self.assertEqual(main(), 2)

                stderr.flush()
                self.assertIn("配置或输入错误", errors.getvalue().decode("utf-8"))
                self.assertIn(b"--requirements", errors.getvalue())

    def test_help_supports_redirected_text_capture(self) -> None:
        with patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                patch("sys.argv", ["code-review-agent", "--help"]):
            with self.assertRaises(SystemExit) as caught:
                main()

            self.assertEqual(caught.exception.code, 0)
            self.assertIn("使用 Agent 审查 Python 代码", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

    def test_help_in_subprocess_with_legacy_encoding(self) -> None:
        root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment.update({"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"})
        # Exercise both script execution and the callable used by the installed
        # console entry point, without requiring an editable installation.
        entrypoints = (
            ["main.py"],
            ["-c", "from main import main; raise SystemExit(main())"],
        )
        for entrypoint in entrypoints:
            with self.subTest(entrypoint=entrypoint):
                result = subprocess.run(
                    [sys.executable, "-B", *entrypoint, "--help"],
                    cwd=root, env=environment, capture_output=True, timeout=30,
                )

                self.assertEqual(result.returncode, 0, result.stderr.decode("ascii", errors="replace"))
                self.assertIn("使用 Agent 审查 Python 代码", result.stdout.decode("utf-8"))
                self.assertIn(b"--requirements", result.stdout)
                self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
