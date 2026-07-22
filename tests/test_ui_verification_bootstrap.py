from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omfg.ui import Terminal
from omfg.verification.checks import Verifier
from tests.helpers import FakeRunner


class UiVerificationBootstrapTests(unittest.TestCase):
    def test_terminal_has_plain_sections_and_defaults(self) -> None:
        output: list[str] = []
        terminal = Terminal(input_fn=lambda _: "", output=output.append)
        terminal.section("Git configuration")
        self.assertEqual(output, ["", "Git configuration", ""])
        self.assertTrue(terminal.confirm("Keep?", default=True))
        self.assertFalse(terminal.confirm("Delete?", default=False))

    @patch("omfg.verification.checks.platform.machine", return_value="wrong")
    def test_verification_failure(self, _: object) -> None:
        result = Verifier(FakeRunner(), Path.home()).system()  # type: ignore[arg-type]
        self.assertFalse(result.passed)

    def test_path_verification_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch.dict("os.environ", {"PATH": "/usr/bin"}):
                self.assertFalse(Verifier(FakeRunner(), Path(raw)).path().passed)  # type: ignore[arg-type]

    def test_bootstrap_syntax(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(("bash", "-n", str(root / "bootstrap/install")), check=False)
        self.assertEqual(result.returncode, 0)

    def test_bootstrap_does_not_execute_omfg(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "bootstrap/install").read_text()
        self.assertIn("Run omfg when you are ready", text)
        self.assertNotIn("exec omfg", text)
