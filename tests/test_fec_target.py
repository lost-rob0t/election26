"""Synthetic executable boundary tests; never invoke a real FEC producer."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from election26.fec_target import main


class FECTargetTests(unittest.TestCase):
    def test_missing_or_relative_tool_fails_without_exec(self):
        for value in ("", "sa-fec"):
            with patch.dict(os.environ, {"ELECTION26_FEC_EXECUTABLE": value}), patch("os.execv") as execute, contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main(["--", "--help"]), 2)
                self.assertIn("absolute executable path", stderr.getvalue())
                execute.assert_not_called()

    def test_missing_file_or_non_executable_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "external-tool"
            for create in (False, True):
                if create:
                    path.write_text("synthetic fixture")
                with patch.dict(os.environ, {"ELECTION26_FEC_EXECUTABLE": str(path)}), patch("os.execv") as execute, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--", "--help"]), 2)
                    execute.assert_not_called()

    def test_literal_arguments_are_forwarded_without_shell_or_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "external tool"
            path.write_text("synthetic fixture")
            path.chmod(0o700)
            arguments = ["--query", "name with spaces; $(no-shell)", "--server", "https://example.invalid"]
            with patch.dict(os.environ, {"ELECTION26_FEC_EXECUTABLE": str(path)}), patch("os.execv") as execute:
                main(["--", *arguments])
                execute.assert_called_once_with(str(path), [str(path), *arguments])

    def test_requires_separator_and_explicit_arguments(self):
        for arguments in ([], ["--"], ["search"]):
            with patch("os.execv") as execute, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(arguments)
            self.assertEqual(error.exception.code, 2)
            execute.assert_not_called()

    def test_help_does_not_require_or_invoke_tool(self):
        with patch("os.execv") as execute, contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as error:
            main(["--help"])
        self.assertEqual(error.exception.code, 0)
        self.assertIn("not implement or verify raw FEC", output.getvalue())
        execute.assert_not_called()

    def test_failed_exec_is_a_clear_sanitized_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "external-tool"
            path.write_text("fixture")
            path.chmod(0o700)
            with patch.dict(os.environ, {"ELECTION26_FEC_EXECUTABLE": str(path)}), patch("os.execv", side_effect=OSError("private details")), contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main(["--", "--help"]), 2)
                self.assertNotIn("private details", stderr.getvalue())
