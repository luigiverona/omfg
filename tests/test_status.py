from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from omfg.cli import main
from omfg.execution import Command
from omfg.models import Capability, Plan, RunOptions
from omfg.status import ReadOnlyRunner, StatusWorkflow
from omfg.ui import Terminal
from omfg.verification.checks import CheckResult
from omfg.workflow import Workflow


class StatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = Plan((Capability.CHECK,), (), (), (), ())
        self.lines: list[str] = []
        self.terminal = Terminal(
            input_fn=Mock(side_effect=AssertionError("status prompted")), output=self.lines.append
        )

    def test_ready_status_has_status_heading_and_no_setup_output(self) -> None:
        with (
            patch.object(
                Workflow, "verification_results", return_value=[CheckResult("system", True)]
            ),
            patch.object(
                Workflow,
                "render_readiness",
                side_effect=lambda: self.lines.append("All software requirements are ready."),
            ),
        ):
            status = StatusWorkflow(
                self.plan, RunOptions(home=Path("/tmp/status")), self.terminal
            ).run()
        self.assertEqual(status, 0)
        self.assertEqual(
            self.lines, ["Status", "All software requirements are ready.", "", "Workstation ready."]
        )
        self.assertNotIn("Plan", self.lines)
        self.assertNotIn("Setup complete.", self.lines)

    def test_unready_status_is_actionable_and_returns_one(self) -> None:
        checks = [CheckResult("Mullvad Browser", False, "not installed")]
        with patch.object(Workflow, "verification_results", return_value=checks):
            status = StatusWorkflow(
                self.plan, RunOptions(home=Path("/tmp/status")), self.terminal
            ).run()
        self.assertEqual(status, 1)
        self.assertEqual(
            self.lines,
            ["Status", "Mullvad Browser: not installed.", "", "Workstation is not ready."],
        )

    def test_status_runner_rejects_every_mutating_command(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "status refused mutating command"):
            ReadOnlyRunner().run(Command(("sudo", "pacman", "-Syu")))

    def test_dry_run_does_not_change_status_semantics(self) -> None:
        checks = [CheckResult("system", False, "unsupported")]
        with patch.object(Workflow, "verification_results", return_value=checks):
            status = StatusWorkflow(
                self.plan, RunOptions(dry_run=True, home=Path("/tmp/status")), self.terminal
            ).run()
        self.assertEqual(status, 1)
        self.assertEqual(self.lines[-1], "Workstation is not ready.")

    def test_setup_interruption_keeps_setup_specific_message(self) -> None:
        with patch.object(Workflow, "run", side_effect=KeyboardInterrupt):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["setup", "--dry-run"])
        self.assertEqual(code, 130)
        rendered = output.getvalue().splitlines()
        self.assertEqual(rendered[-2:], ["Setup paused.", "Run omfg again to continue."])
