from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omfg.errors import ValidationError
from omfg.execution import Command, CommandRunner, TemporaryWorkspace
from omfg.packages import AurManager, FlatpakManager, PacmanManager
from tests.helpers import FakeRunner


class ExecutionTests(unittest.TestCase):
    def test_dry_run_does_not_execute(self) -> None:
        runner = CommandRunner(dry_run=True)
        result = runner.run(Command(("definitely-missing-command",)))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(runner.history), 1)

    def test_redaction(self) -> None:
        self.assertEqual(CommandRunner.redact("token=secret", ("secret",)), "token=[REDACTED]")

    def test_workspace_success_cleanup_and_keep(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with TemporaryWorkspace(temp_root=root) as workspace:
                path = workspace.path
                self.assertTrue((path / "aur").is_dir())
            self.assertFalse(path.exists())
            with TemporaryWorkspace(temp_root=root, keep=True) as kept:
                kept_path = kept.path
            self.assertTrue(kept_path.exists())
            TemporaryWorkspace.safe_cleanup(kept_path, root)

    def test_failed_workspace_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaises(RuntimeError):
                with TemporaryWorkspace(temp_root=root) as workspace:
                    path = workspace.path
                    raise RuntimeError("fail")
            self.assertTrue(path.exists())

    def test_cleanup_guards(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaises(ValueError):
                TemporaryWorkspace.safe_cleanup(root, root)

    def test_pacman_command_is_sorted_and_full_update(self) -> None:
        runner = FakeRunner()
        manager = PacmanManager(runner)  # type: ignore[arg-type]
        manager.full_update()
        manager.install(("z", "a", "a"))
        self.assertEqual(
            runner.commands[0].argv, ("sudo", "pacman", "-Syu", "--noconfirm", "--needed")
        )
        self.assertEqual(runner.commands[1].argv[-2:], ("a", "z"))

    @patch("omfg.packages.managers.os.geteuid", return_value=0)
    def test_aur_never_builds_as_root(self, _: object) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ValidationError):
                AurManager(FakeRunner(), Path(raw)).bootstrap_yay()  # type: ignore[arg-type]

    def test_flatpak_remote_idempotent(self) -> None:
        from omfg.execution import CommandResult

        argv = ("flatpak", "remotes", "--user", "--columns=name")
        runner = FakeRunner({argv: CommandResult(argv, 0, "flathub\n", "")})
        FlatpakManager(runner).ensure_flathub()  # type: ignore[arg-type]
        self.assertFalse(any(c.argv[1] == "remote-add" for c in runner.commands))
