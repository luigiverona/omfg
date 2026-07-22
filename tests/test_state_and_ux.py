from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omfg.catalog import load_catalog
from omfg.config.codex import CodexManager
from omfg.config.shell import ShellInfo, configure_path
from omfg.execution import Command, CommandResult
from omfg.models import Capability, Package, Plan, RunOptions, Source
from omfg.planning import StateInspector
from omfg.ui import Terminal
from omfg.verification.checks import CheckResult
from omfg.workflow import Workflow
from tests.helpers import FakeRunner


def response_for(package: Package, installed: bool) -> tuple[tuple[str, ...], CommandResult] | None:
    code = 0 if installed else 1
    if package.source in {Source.PACMAN, Source.AUR}:
        argv = ("pacman", "-Q", package.identifier)
    elif package.source is Source.FLATPAK:
        argv = ("flatpak", "info", "--user", package.identifier)
    else:
        return None
    return argv, CommandResult(argv, code, "", "")


class StateAndUxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_catalog()
        self.packages = tuple(sorted((*self.catalog.apps, *self.catalog.deps)))

    def inspector(self, home: Path, installed_ids: set[str]) -> StateInspector:
        responses = dict(
            item
            for package in self.packages
            if (item := response_for(package, package.identifier in installed_ids)) is not None
        )
        if "codex" in installed_ids:
            shared = home / ".local/share/omfg/bin/codex"
            shared.parent.mkdir(parents=True)
            shared.write_text("binary", encoding="utf-8")
        return StateInspector(FakeRunner(responses), home)  # type: ignore[arg-type]

    def test_no_packages_installed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            pending = self.inspector(Path(raw), set()).pending(self.packages)
            self.assertEqual(len(pending), 15)

    def test_all_packages_installed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            ids = {package.identifier for package in self.packages}
            self.assertEqual(self.inspector(Path(raw), ids).pending(self.packages), ())

    def test_only_git_installed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            pending = self.inspector(Path(raw), {"git"}).pending(self.packages)
            self.assertNotIn("git", {package.identifier for package in pending})
            self.assertEqual(len(pending), 14)

    def test_rerun_skips_requirements_completed_before_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            completed = {"discord", "mullvad-vpn", "spotify-launcher"}
            pending = self.inspector(Path(raw), completed).pending(self.packages)
            pending_ids = {package.identifier for package in pending}
            self.assertTrue(completed.isdisjoint(pending_ids))
            self.assertEqual(len(pending), len(self.packages) - len(completed))

    def test_git_and_openssh_present_github_cli_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            github_requirements = tuple(
                package
                for package in self.packages
                if package.identifier in {"git", "openssh", "github-cli"}
            )
            pending = self.inspector(Path(raw), {"git", "openssh"}).pending(github_requirements)
            self.assertEqual([package.identifier for package in pending], ["github-cli"])

    def test_codex_executable_present_but_launchers_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            codex = next(package for package in self.packages if package.identifier == "codex")
            self.assertEqual(self.inspector(home, {"codex"}).pending((codex,)), ())
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            self.assertFalse(manager.profiles_distinct())

    def test_invalid_codex_executable_remains_pending(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            codex = next(package for package in self.packages if package.identifier == "codex")
            shared = home / ".local/share/omfg/bin/codex"
            shared.parent.mkdir(parents=True)
            shared.write_text("broken", encoding="utf-8")
            argv = (str(shared), "--version")
            runner = FakeRunner({argv: CommandResult(argv, 1, "", "not executable")})
            self.assertEqual(StateInspector(runner, home).pending((codex,)), (codex,))  # type: ignore[arg-type]

    def test_launchers_present_but_one_profile_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("binary", encoding="utf-8")
            manager.create_profiles()
            (manager.state_root / "02").rename(manager.state_root / "missing-02")
            self.assertFalse(manager.verified("02"))

    def test_codex_rerun_reuses_binary_and_skips_authenticated_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            workspace = home / "workspace"
            workspace.mkdir()
            runner = FakeRunner()
            manager = CodexManager(runner, home)  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("binary", encoding="utf-8")
            manager.create_profiles()
            workflow = Workflow(
                Plan((Capability.CODEX,), (), (), (), ()),
                RunOptions(home=home),
                Terminal(output=lambda _: None),
                runner=runner,  # type: ignore[arg-type]
            )
            workflow._codex(workspace)
            argv = [command.argv for command in runner.commands]
            self.assertFalse(any(command[0] == "curl" for command in argv))
            self.assertFalse(any(command[-1] == "login" for command in argv))

    def test_codex_profiles_are_checked_and_authenticated_independently(self) -> None:
        class ProfileRunner(FakeRunner):
            def __init__(self) -> None:
                super().__init__()
                self.profile_two_authenticated = False

            def run(self, command: Command, *, check: bool = True) -> CommandResult:
                self.commands.append(command)
                argv = command.argv
                if argv[-2:] == ("login", "status"):
                    if "codex-01" in argv[0] or self.profile_two_authenticated:
                        return CommandResult(argv, 0, "", "")
                    return CommandResult(argv, 1, "", "not logged in")
                if argv[-1:] == ("login",):
                    self.profile_two_authenticated = True
                return CommandResult(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            workspace = home / "workspace"
            workspace.mkdir()
            runner = ProfileRunner()
            manager = CodexManager(runner, home)  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("binary", encoding="utf-8")
            manager.create_profiles()
            output: list[str] = []
            workflow = Workflow(
                Plan((Capability.CODEX,), (), (), (), ()),
                RunOptions(home=home),
                Terminal(output=output.append),
                runner=runner,  # type: ignore[arg-type]
            )
            workflow._codex(workspace)
            login_commands = [
                command.argv for command in runner.commands if command.argv[-1:] == ("login",)
            ]
            self.assertEqual(login_commands, [(str(manager.bin_dir / "codex-02"), "login")])
            self.assertIn("codex-01 already authenticated", output)
            self.assertIn("codex-02 authenticated", output)

    def test_complete_workstation_package_inventory_has_no_pending_items(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            ids = {package.identifier for package in self.packages}
            inspector = self.inspector(home, ids)
            self.assertEqual(inspector.pending(self.packages), ())

    def test_complete_workstation_configuration_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            responses: dict[tuple[str, ...], CommandResult] = {}
            for package in self.packages:
                item = response_for(package, True)
                if item:
                    responses[item[0]] = item[1]
            remotes = ("flatpak", "remotes", "--user", "--columns=name")
            responses[remotes] = CommandResult(remotes, 0, "flathub\n", "")
            for key, value in (
                ("user.name", "Person"),
                ("user.email", "person@example.com"),
                ("init.defaultBranch", "main"),
            ):
                argv = ("git", "config", "--global", "--get", key)
                responses[argv] = CommandResult(argv, 0, value + "\n", "")
            protocol = ("gh", "config", "get", "git_protocol", "--host", "github.com")
            responses[protocol] = CommandResult(protocol, 0, "ssh\n", "")
            ssh = ("ssh", "-T", "-o", "BatchMode=yes", "git@github.com")
            responses[ssh] = CommandResult(ssh, 1, "", "successfully authenticated")
            runner = FakeRunner(responses)
            codex = CodexManager(runner, home)  # type: ignore[arg-type]
            codex.shared_bin.parent.mkdir(parents=True)
            codex.shared_bin.write_text("binary", encoding="utf-8")
            codex.create_profiles()
            fish = ShellInfo("fish", Path("/usr/bin/fish"), "test")
            configure_path(home, fish, env={"PATH": ""})
            output: list[str] = []
            plan = Plan((Capability.CHECK,), (), self.packages, (), ())
            workflow = Workflow(
                plan,
                RunOptions(home=home),
                Terminal(output=output.append),
                runner=runner,  # type: ignore[arg-type]
                target_shell=fish,
            )
            with patch(
                "omfg.workflow.Verifier.system",
                return_value=CheckResult("supported system", True),
            ):
                workflow._verify()
            self.assertIn("All checks passed", output)

    def test_dry_run_distinguishes_requirements_and_pending_installations(self) -> None:
        output: list[str] = []
        plan = Plan((), (), self.packages, (), ())
        workflow = Workflow(
            plan,
            RunOptions(dry_run=True),
            Terminal(output=output.append),
            runner=FakeRunner(),  # type: ignore[arg-type]
        )
        workflow._render_dry_run(self.packages[:2])
        self.assertIn("Software requirements: 15", output)
        self.assertIn("Pending installations: 2", output)

    def test_vpn_dry_run_groups_official_package_as_system_software(self) -> None:
        vpn = next(package for package in self.packages if package.identifier == "mullvad-vpn")
        output: list[str] = []
        workflow = Workflow(
            Plan((Capability.APPS,), (), (vpn,), (), ()),
            RunOptions(dry_run=True, verbose=True),
            Terminal(output=output.append),
            runner=FakeRunner(),  # type: ignore[arg-type]
        )
        workflow._render_dry_run((vpn,))
        rendered = "\n".join(output)
        self.assertIn("Software requirements: 1", rendered)
        self.assertIn("Pending installations: 1", rendered)
        self.assertIn("pacman: mullvad-vpn (pending)", rendered)
        self.assertNotIn("mullvad-vpn-bin", rendered)

    def test_normal_summary_uses_source_specific_plain_language(self) -> None:
        output: list[str] = []
        plan = Plan((), (), self.packages, (), ())
        workflow = Workflow(
            plan,
            RunOptions(),
            Terminal(output=output.append),
            runner=FakeRunner(),  # type: ignore[arg-type]
        )
        pending = tuple(
            package
            for package in self.packages
            if package.identifier in {"discord", "org.vinegarhq.Sober", "codex"}
        )
        workflow._render_change_summary(pending)
        rendered = "\n".join(output)
        self.assertIn("1 system/AUR package", rendered)
        self.assertIn("1 Flatpak application", rendered)
        self.assertIn("1 upstream tool", rendered)
        self.assertNotIn("✓", rendered)

    def test_final_summary_uses_execution_results(self) -> None:
        output: list[str] = []
        workflow = Workflow(
            Plan((), (), (), (), ()),
            RunOptions(),
            Terminal(output=output.append),
            runner=FakeRunner(),  # type: ignore[arg-type]
        )
        workflow.summary.installed = 2
        workflow.summary.components_configured = 3
        workflow.summary.existing_keys_preserved = 1
        workflow._render_final_summary()
        self.assertEqual(
            output,
            [
                "",
                "Setup complete",
                "",
                "Software installed        2",
                "Components configured     3",
                "Existing keys preserved   1",
                "Failures                  0",
                "",
                "Workstation ready.",
            ],
        )
