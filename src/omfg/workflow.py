from __future__ import annotations

import getpass
import os
import shutil
from pathlib import Path

from omfg.config.codex import CodexManager
from omfg.config.git import GitConfigurator, GitIdentity
from omfg.config.github import GitHubConfigurator
from omfg.config.shell import configure_path, detect_shell
from omfg.config.ssh import SSHManager
from omfg.execution import Command, CommandRunner, TemporaryWorkspace
from omfg.models import Capability, Plan, RunOptions, Source
from omfg.packages import AurManager, FlatpakManager, PacmanManager
from omfg.system import validate_system
from omfg.ui import Terminal
from omfg.verification.checks import CheckResult, Verifier


class Workflow:
    def __init__(self, plan: Plan, options: RunOptions, terminal: Terminal) -> None:
        self.plan = plan
        self.options = options
        self.terminal = terminal
        self.runner = CommandRunner(dry_run=options.dry_run, verbose=options.verbose)

    def run(self) -> int:
        validate_system(require_network=not self.options.dry_run)
        package_count = len(self.plan.packages)
        if self.plan.selected == (Capability.CHECK,) and not self.plan.prerequisites:
            self.terminal.output("Checking workstation...")
            self._verify()
            return 0
        if self.options.dry_run:
            self._render_dry_run()
            return 0
        shell = detect_shell(proc_comm=Path(f"/proc/{os.getppid()}/comm").read_text().strip())
        self.terminal.output(f"Arch Linux, {shell.name}")
        self.terminal.output("")
        self.terminal.output("Checking workstation...")
        self.terminal.output("")
        self.terminal.output(
            f"This setup will install {package_count} configured packages and apply the selected workstation configuration."
        )
        if not self.terminal.confirm("Continue?", assume_yes=self.options.assume_yes):
            self.terminal.output("No changes made.")
            return 0
        with TemporaryWorkspace(keep=self.options.keep_temp) as workspace:
            try:
                self._mutate(workspace.path or Path("/tmp"))
                self._verify()
            except BaseException:
                workspace.mark_failed()
                raise
        self.terminal.section("Setup complete")
        self.terminal.output(f"Packages configured       {package_count}")
        self.terminal.output("Failures                  0")
        self.terminal.output("")
        self.terminal.output("Workstation ready.")
        return 0

    def _render_dry_run(self) -> None:
        self.terminal.output("Dry run: no changes will be made.")
        self.terminal.output("Selected: " + ", ".join(c.value for c in self.plan.selected))
        if self.plan.prerequisites:
            self.terminal.output(
                "Prerequisites: " + ", ".join(c.value for c in self.plan.prerequisites)
            )
        self.terminal.output(f"Packages: {len(self.plan.packages)}")
        if self.options.verbose:
            for package in self.plan.packages:
                self.terminal.output(f"{package.source.value}: {package.identifier}")

    def _mutate(self, workspace: Path) -> None:
        capabilities = set(self.plan.selected) | set(self.plan.prerequisites)
        pacman = PacmanManager(self.runner)
        privileged_packages = any(
            package.source in {Source.PACMAN, Source.AUR} for package in self.plan.packages
        )
        if Capability.SYSTEM in capabilities or privileged_packages:
            self.terminal.output("Administrator access is required.")
            self.terminal.output("Password:")
            self.runner.run(Command(("sudo", "-v")))
        if Capability.SYSTEM in capabilities:
            pacman.full_update()
            self.terminal.output("System updated")
        native = [p.identifier for p in self.plan.packages if p.source is Source.PACMAN]
        aur = [
            p.identifier
            for p in self.plan.packages
            if p.source is Source.AUR and p.identifier != "yay-bin"
        ]
        pacman.install(native)
        if aur:
            manager = AurManager(self.runner, workspace)
            if not shutil.which("yay"):
                pacman.install(("git", "base-devel"))
                manager.bootstrap_yay()
            manager.install(aur)
        if native or aur:
            self.terminal.output(f"{len(native) + len(aur)} packages installed")
        if Capability.FLATPAK in capabilities or Capability.FLATHUB in capabilities:
            flatpak = FlatpakManager(self.runner)
            flatpak.ensure_flathub()
            flatpak.install(p.identifier for p in self.plan.packages if p.source is Source.FLATPAK)
            self.terminal.output("Flatpak and Flathub configured")
        if Capability.GIT in capabilities:
            self.terminal.section("Git configuration")
            self._git()
            self.terminal.output("Git configured")
        if Capability.GITHUB in capabilities:
            self.terminal.section("GitHub authentication")
            if not GitHubConfigurator(self.runner).authenticated():
                self.terminal.output("Complete the authentication in your browser.")
            GitHubConfigurator(self.runner).authenticate()
            self.terminal.output("GitHub authenticated")
            self.terminal.output("Git protocol set to SSH")
        if Capability.SSH in capabilities:
            self.terminal.section("SSH configuration")
            self._ssh()
        if Capability.CODEX in capabilities:
            self.terminal.section("Codex configuration")
            self._codex(workspace)
        if Capability.SHELL in capabilities:
            self.terminal.section("Shell configuration")
            shell = detect_shell(proc_comm=Path(f"/proc/{os.getppid()}/comm").read_text().strip())
            _, changed = configure_path(self.options.home, shell)
            self.terminal.output(
                f"{shell.name} PATH {'updated' if changed else 'already configured'}"
            )

    def _git(self) -> None:
        git = GitConfigurator(self.runner)
        name = git.get("user.name") or self.terminal.input("Name:  ").strip()
        email = git.get("user.email") or self.terminal.input("Email: ").strip()
        self.terminal.output(f"Name:  {name}")
        self.terminal.output(f"Email: {email}")
        if self.terminal.confirm(
            "Use this identity?", default=True, assume_yes=self.options.assume_yes
        ):
            git.configure(GitIdentity(name, email))

    def _ssh(self) -> None:
        manager = SSHManager(self.runner, self.options.home)
        existing = manager.inventory()
        remote_existing = manager.inventory_remote()
        account = GitHubConfigurator(self.runner).account() or getpass.getuser()
        manager.create(
            GitConfigurator(self.runner).get("user.email") or f"{account}@users.noreply.github.com"
        )
        self.terminal.output("SSH key created")
        manager.upload(f"omfg-{os.uname().nodename}")
        self.terminal.output("Key uploaded to GitHub")
        if not manager.verify():
            raise RuntimeError("GitHub SSH authentication did not verify")
        self.terminal.output("GitHub connection verified")
        old = tuple(key for key in existing if key.private != manager.key)
        if old and not self.terminal.confirm(
            "Keep existing keys?", default=True, assume_yes=self.options.assume_yes
        ):
            for key in old:
                self.terminal.output(
                    f"Eligible local key: {key.private} ({key.fingerprint or 'unknown fingerprint'})"
                )
            confirmed = self.terminal.confirm("Delete these keys?", destructive=True)
            if confirmed:
                old_fingerprints = frozenset(key.fingerprint for key in old if key.fingerprint)
                remote_old = tuple(
                    key for key in remote_existing if key.fingerprint in old_fingerprints
                )
                for remote_key in remote_old:
                    self.terminal.output(
                        f"Eligible GitHub key: {remote_key.title} ({remote_key.fingerprint})"
                    )
                manager.delete_remote(
                    remote_old, eligible_fingerprints=old_fingerprints, explicit_confirmation=True
                )
                manager.delete(old, explicit_confirmation=True)
                if not manager.verify():
                    raise RuntimeError("new SSH key failed reverification")

    def _codex(self, workspace: Path) -> None:
        codex = CodexManager(self.runner, self.options.home, workspace)
        codex.install()
        codex.create_profiles()
        for number in ("01", "02"):
            self.terminal.output(f"Sign in to codex-{number}.")
            codex.authenticate(number)
            if not codex.verified(number):
                raise RuntimeError(f"codex-{number} authentication did not verify")
            self.terminal.output(f"codex-{number} authenticated")
        self.terminal.output("Both Codex profiles verified")

    def _verify(self) -> None:
        self.terminal.section("Verifying workstation...")
        verifier = Verifier(self.runner, self.options.home)
        capabilities = set(self.plan.selected) | set(self.plan.prerequisites)
        if Capability.CHECK in capabilities:
            capabilities = set(Capability)
        results = [verifier.system(), *(verifier.package(p) for p in self.plan.packages)]
        if Capability.FLATHUB in capabilities:
            results.append(verifier.flathub())
        if Capability.GIT in capabilities:
            results.append(
                CheckResult(
                    "Git configuration",
                    GitConfigurator(self.runner).verify(),
                    "identity or default branch missing",
                )
            )
        if Capability.GITHUB in capabilities:
            results.append(
                CheckResult(
                    "GitHub authentication",
                    GitHubConfigurator(self.runner).authenticated(),
                    "not authenticated",
                )
            )
            protocol = self.runner.run(
                Command(
                    ("gh", "config", "get", "git_protocol", "--host", "github.com"), mutate=False
                ),
                check=False,
            )
            results.append(
                CheckResult(
                    "GitHub SSH protocol", protocol.stdout.strip() == "ssh", "protocol is not SSH"
                )
            )
        if Capability.SSH in capabilities:
            results.append(
                CheckResult(
                    "GitHub SSH connection",
                    SSHManager(self.runner, self.options.home).verify(),
                    "connection failed",
                )
            )
        if Capability.CODEX in capabilities:
            codex = CodexManager(self.runner, self.options.home)
            results.extend(
                CheckResult(f"codex-{number}", codex.verified(number), "profile not authenticated")
                for number in ("01", "02")
            )
            results.append(
                CheckResult(
                    "Codex profile isolation",
                    codex.state_root.joinpath("01").resolve()
                    != codex.state_root.joinpath("02").resolve(),
                    "profile homes overlap",
                )
            )
        if Capability.SHELL in capabilities:
            shell = detect_shell(proc_comm=Path(f"/proc/{os.getppid()}/comm").read_text().strip())
            results.append(verifier.shell_configuration(shell))
        failures = [result for result in results if not result.passed]
        if failures:
            raise RuntimeError("; ".join(f"{item.name}: {item.reason}" for item in failures))
        self.terminal.output("All checks passed")
