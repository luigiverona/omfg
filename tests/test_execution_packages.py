from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omfg.errors import CommandError, ValidationError
from omfg.execution import Command, CommandResult, CommandRunner, TemporaryWorkspace
from omfg.packages import AurManager, FlatpakManager, PacmanManager
from tests.helpers import FakeRunner


class ExecutionTests(unittest.TestCase):
    def test_dry_run_does_not_execute(self) -> None:
        runner = CommandRunner(dry_run=True)
        result = runner.run(Command(("definitely-missing-command",)))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(runner.history), 1)

    def test_command_can_replace_inherited_environment(self) -> None:
        result = CommandRunner().run(
            Command(
                (sys.executable, "-c", "import os; print(os.environ.get('OMFG_POISON', ''))"),
                env={"PATH": "/usr/bin:/bin"},
                replace_env=True,
                mutate=False,
            )
        )
        self.assertEqual(result.stdout, "\n")

    def test_redaction(self) -> None:
        self.assertEqual(CommandRunner.redact("token=secret", ("secret",)), "token=[REDACTED]")

    def test_verbose_command_and_output_are_redacted(self) -> None:
        output: list[str] = []
        runner = CommandRunner(verbose=True, output=output.append)
        runner.run(
            Command(
                ("printf", "%s", "secret"),
                sensitive_values=("secret",),
                mutate=False,
            )
        )
        self.assertNotIn("secret", "\n".join(output))
        self.assertIn("[REDACTED]", "\n".join(output))

    def test_package_failure_has_compact_reason_and_full_log(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "logs/aur.log"
            command = Command(
                (
                    sys.executable,
                    "-c",
                    "import sys; print(':: old and new are in conflict'); "
                    "print('error: failed to prepare transaction', file=sys.stderr); sys.exit(1)",
                ),
                mutate=False,
                failure_component="AUR installation",
                failure_operation="install packages",
                failure_packages=("example-bin",),
                log_path=log,
            )
            with self.assertRaises(CommandError) as caught:
                CommandRunner().run(command)
            self.assertEqual(caught.exception.reason, "old and new are in conflict")
            self.assertEqual(caught.exception.packages, ("example-bin",))
            self.assertEqual(caught.exception.log_path, str(log))
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            content = log.read_text(encoding="utf-8")
            self.assertIn("old and new are in conflict", content)
            self.assertIn("failed to prepare transaction", content)

    def test_successful_command_output_stays_quiet_without_verbose(self) -> None:
        output: list[str] = []
        CommandRunner(output=output.append).run(
            Command((sys.executable, "-c", "print('ordinary package output')"), mutate=False)
        )
        self.assertEqual(output, [])

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

    @patch("omfg.packages.managers.os.geteuid", return_value=1000)
    def test_aur_validates_metadata_builds_unprivileged_and_elevates_only_install(
        self, _: object
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            clone = workspace / "aur/yay-bin"
            config = workspace / "state/makepkg.conf"
            system_config = workspace / "system-makepkg.conf"
            system_config.write_text("OPTIONS=(strip debug)\n", encoding="utf-8")
            origin_argv = ("git", "-C", str(clone), "remote", "get-url", "origin")
            metadata_argv = ("makepkg", "--config", str(config), "--printsrcinfo")
            package_argv = ("makepkg", "--config", str(config), "--packagelist")
            artifact = str(clone / "yay-bin-13.0.1-1-x86_64.pkg.tar.zst")
            debug_artifact = str(clone / "yay-bin-debug-13.0.1-1-x86_64.pkg.tar.zst")
            unrelated_artifact = str(clone / "yay-helper-13.0.1-1-x86_64.pkg.tar.zst")
            responses = {
                origin_argv: CommandResult(
                    origin_argv, 0, "https://aur.archlinux.org/yay-bin.git\n", ""
                ),
                metadata_argv: CommandResult(
                    metadata_argv, 0, "pkgbase = yay-bin\npkgname = yay-bin\n", ""
                ),
                package_argv: CommandResult(
                    package_argv,
                    0,
                    artifact + "\n" + debug_artifact + "\n" + unrelated_artifact + "\n",
                    "",
                ),
            }
            runner = FakeRunner(responses)
            AurManager(runner, workspace, system_config).bootstrap_yay()  # type: ignore[arg-type]
            commands = [command.argv for command in runner.commands]
            self.assertIn(
                ("makepkg", "--config", str(config), "--cleanbuild", "--noconfirm"),
                commands,
            )
            self.assertNotIn(("makepkg", "--syncdeps", "--cleanbuild", "--noconfirm"), commands)
            self.assertIn(("sudo", "pacman", "-U", "--noconfirm", artifact), commands)
            self.assertNotIn(debug_artifact, commands[-2])
            self.assertNotIn(unrelated_artifact, commands[-2])
            self.assertIn("OPTIONS[_omfg_index]=!debug", config.read_text(encoding="utf-8"))

    def test_aur_installs_only_requested_identifiers_one_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            system_config = workspace / "system-makepkg.conf"
            system_config.write_text("OPTIONS=(strip debug)\n", encoding="utf-8")
            runner = FakeRunner()
            AurManager(runner, workspace, system_config).install(  # type: ignore[arg-type]
                ("z-bin", "a-bin", "a-bin")
            )
            installs = [command for command in runner.commands if command.argv[:2] == ("yay", "-S")]
            self.assertEqual([command.argv[-1] for command in installs], ["a-bin", "z-bin"])
            self.assertTrue(all(len(command.failure_packages) == 1 for command in installs))
            self.assertFalse(
                any("-debug" in argument for command in installs for argument in command.argv)
            )
            self.assertTrue(all("--makepkgconf" in command.argv for command in installs))

    def test_flatpak_remote_idempotent(self) -> None:
        argv = ("flatpak", "remotes", "--user", "--columns=name")
        runner = FakeRunner({argv: CommandResult(argv, 0, "flathub\n", "")})
        FlatpakManager(runner).ensure_flathub()  # type: ignore[arg-type]
        self.assertFalse(any(c.argv[1] == "remote-add" for c in runner.commands))
