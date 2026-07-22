from __future__ import annotations

import getpass
import os
import shutil
from pathlib import Path

from omfg.config.codex import CodexManager
from omfg.config.git import GitConfigurator, GitIdentity
from omfg.config.github import GitHubConfigurator
from omfg.config.shell import ShellInfo, configure_path, detect_shell
from omfg.config.ssh import SSHManager
from omfg.config.ssh_inventory import github_correlated_local_keys
from omfg.errors import ValidationError
from omfg.execution import Command, CommandRunner, TemporaryWorkspace
from omfg.models import Capability, ExecutionSummary, Package, Plan, RunOptions, Source
from omfg.packages import AurManager, FlatpakManager, PacmanManager
from omfg.planning import StateInspector
from omfg.system import validate_system
from omfg.ui import Terminal
from omfg.verification.checks import CheckResult, Verifier


class Workflow:
    def __init__(
        self,
        plan: Plan,
        options: RunOptions,
        terminal: Terminal,
        *,
        runner: CommandRunner | None = None,
        target_shell: ShellInfo | None = None,
    ) -> None:
        self.plan = plan
        self.options = options
        self.terminal = terminal
        self.runner = runner or CommandRunner(
            dry_run=options.dry_run,
            verbose=options.verbose,
            output=terminal.output,
        )
        self.target_shell = target_shell
        self.summary = ExecutionSummary(requirements=len(plan.packages))

    def _shell(self) -> ShellInfo:
        if self.target_shell is None:
            self.target_shell = detect_shell()
        return self.target_shell

    def run(self) -> int:
        validate_system(require_network=not self.options.dry_run)
        inspector = StateInspector(self.runner, self.options.home)
        pending = inspector.pending(self.plan.packages)
        self.summary.pending_before = len(pending)
        capabilities = set(self.plan.selected) | set(self.plan.prerequisites)
        if Capability.SHELL in capabilities:
            self._shell()
        if self.plan.selected == (Capability.CHECK,) and not self.plan.prerequisites:
            self.terminal.output("Checking workstation...")
            self._verify()
            return 0
        if self.options.dry_run:
            self._render_dry_run(pending)
            return 0
        shell = self._shell()
        self.terminal.output(f"Arch Linux, {shell.name}")
        self.terminal.output("")
        self.terminal.output("Checking workstation...")
        self.terminal.output("")
        self._render_change_summary(pending)
        if not self.terminal.confirm("Continue?", assume_yes=self.options.assume_yes):
            self.terminal.output("No changes made.")
            return 0
        with TemporaryWorkspace(keep=self.options.keep_temp) as workspace:
            self._mutate(workspace.path or Path("/tmp"), pending)
            self.summary.installed = len(pending) - len(inspector.pending(pending))
            self._verify()
        self._render_final_summary()
        return 0

    def _render_final_summary(self) -> None:
        self.terminal.section("Setup complete")
        self.terminal.output(f"Software installed        {self.summary.installed}")
        self.terminal.output(f"Components configured     {self.summary.components_configured}")
        self.terminal.output(f"Existing keys preserved   {self.summary.existing_keys_preserved}")
        self.terminal.output("Failures                  0")
        self.terminal.output("")
        self.terminal.output("Workstation ready.")

    def _render_dry_run(self, pending: tuple[Package, ...]) -> None:
        self.terminal.output("Dry run: no changes will be made.")
        self.terminal.output("Selected: " + ", ".join(c.value for c in self.plan.selected))
        if self.plan.prerequisites:
            self.terminal.output(
                "Prerequisites: " + ", ".join(c.value for c in self.plan.prerequisites)
            )
        self.terminal.output(f"Software requirements: {len(self.plan.packages)}")
        self.terminal.output(f"Pending installations: {len(pending)}")
        if self.options.verbose:
            for package in self.plan.packages:
                state = "pending" if package in pending else "present"
                self.terminal.output(f"{package.source.value}: {package.identifier} ({state})")

    @staticmethod
    def _noun(count: int, singular: str, plural: str | None = None) -> str:
        return singular if count == 1 else (plural or singular + "s")

    def _render_change_summary(self, pending: tuple[Package, ...]) -> None:
        counts = {source: sum(package.source is source for package in pending) for source in Source}
        parts: list[str] = []
        system_packages = counts[Source.PACMAN] + counts[Source.AUR]
        if system_packages:
            parts.append(f"{system_packages} {self._noun(system_packages, 'system/AUR package')}")
        if counts[Source.FLATPAK]:
            count = counts[Source.FLATPAK]
            parts.append(f"{count} Flatpak {self._noun(count, 'application')}")
        if counts[Source.UPSTREAM]:
            count = counts[Source.UPSTREAM]
            parts.append(f"{count} upstream {self._noun(count, 'tool')}")
        installations = ", ".join(parts) if parts else "no missing software"
        capabilities = set(self.plan.selected) | set(self.plan.prerequisites)
        components: list[str] = []
        if Capability.FLATPAK in capabilities or Capability.FLATHUB in capabilities:
            components.append("Flatpak")
        if Capability.GIT in capabilities:
            components.append("Git")
        if Capability.GITHUB in capabilities:
            components.append("GitHub")
        if Capability.SSH in capabilities:
            components.append("SSH")
        if Capability.CODEX in capabilities:
            components.append("two Codex profiles")
        if Capability.SHELL in capabilities:
            components.append("the shell PATH")
        configuration = ", ".join(components) if components else "the selected components"
        prefix = "update the system, " if Capability.SYSTEM in self.plan.selected else ""
        self.terminal.output(
            f"This setup will {prefix}install {installations}, configure {configuration}, then verify the result."
        )
        already = len(self.plan.packages) - len(pending)
        if already:
            self.terminal.output(
                f"{already} of {len(self.plan.packages)} software requirements already present."
            )

    def _mutate(self, workspace: Path, pending: tuple[Package, ...]) -> None:
        capabilities = set(self.plan.selected) | set(self.plan.prerequisites)
        pacman = PacmanManager(self.runner)
        privileged = any(p.source in {Source.PACMAN, Source.AUR} for p in pending)
        if Capability.SYSTEM in capabilities or privileged:
            self.terminal.output("Administrator access is required.")
            self.terminal.output("Password:")
            self.runner.run(Command(("sudo", "-v")))
        if Capability.SYSTEM in capabilities:
            pacman.full_update()
            self.terminal.output("System updated")
        native = [p.identifier for p in pending if p.source is Source.PACMAN]
        aur = [
            p.identifier for p in pending if p.source is Source.AUR and p.identifier != "yay-bin"
        ]
        yay_pending = any(p.source is Source.AUR and p.identifier == "yay-bin" for p in pending)
        pacman.install(native)
        if aur or yay_pending:
            manager = AurManager(self.runner, workspace)
            if not shutil.which("yay"):
                pacman.install(("git", "base-devel"))
                manager.bootstrap_yay()
            if aur:
                manager.install(aur)
        package_pending = len(native) + len(aur) + int(yay_pending)
        if package_pending:
            package_requirements = tuple(
                p for p in pending if p.source in {Source.PACMAN, Source.AUR}
            )
            installed_now = package_pending - len(
                StateInspector(self.runner, self.options.home).pending(package_requirements)
            )
            self.terminal.output(
                f"{installed_now} {self._noun(installed_now, 'package')} installed"
            )
        if Capability.FLATHUB in capabilities:
            flatpak = FlatpakManager(self.runner)
            flatpak.ensure_flathub()
            flatpak.install(p.identifier for p in pending if p.source is Source.FLATPAK)
            self.terminal.output("Flatpak and Flathub configured")
            self.summary.components_configured += 1
        if Capability.GIT in capabilities:
            self.terminal.section("Git configuration")
            self._git()
            self.terminal.output("Git configured")
            self.summary.components_configured += 1
        if Capability.GITHUB in capabilities:
            self.terminal.section("GitHub authentication")
            github = GitHubConfigurator(self.runner)
            if not github.authenticated():
                self.terminal.output("Complete the authentication in your browser.")
            github.authenticate()
            self.terminal.output("GitHub authenticated")
            self.terminal.output("Git protocol set to SSH")
            self.summary.components_configured += 1
        if Capability.SSH in capabilities:
            self.terminal.section("SSH configuration")
            self._ssh()
            self.summary.components_configured += 1
        if Capability.CODEX in capabilities:
            self.terminal.section("Codex configuration")
            self._codex(
                workspace,
                install_required=any(
                    p.source is Source.UPSTREAM and p.identifier == "codex" for p in pending
                ),
            )
            self.summary.components_configured += 2
        if Capability.SHELL in capabilities:
            self.terminal.section("Shell configuration")
            shell = self._shell()
            update = configure_path(self.options.home, shell)
            self.terminal.output(
                f"{shell.name} PATH {'updated' if update.changed else 'already configured'}"
            )
            if update.new_session_required:
                self.terminal.output("A new shell session is required")
            else:
                self.terminal.output("Current shell session already has ~/.local/bin in PATH")
            self.summary.components_configured += 1

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
        created = manager.create(
            GitConfigurator(self.runner).get("user.email") or f"{account}@users.noreply.github.com"
        )
        self.terminal.output("SSH key created" if created else "Dedicated SSH key already present")
        dedicated = next((key for key in manager.inventory() if key.private == manager.key), None)
        registered = bool(
            dedicated
            and dedicated.fingerprint
            and any(key.fingerprint == dedicated.fingerprint for key in remote_existing)
        )
        if not registered:
            manager.upload(f"omfg-{os.uname().nodename}")
            self.terminal.output("Key uploaded to GitHub")
        else:
            self.terminal.output("Key already registered with GitHub")
        if not manager.verify():
            raise RuntimeError("GitHub SSH authentication did not verify")
        self.terminal.output("GitHub connection verified")
        old = tuple(key for key in existing if key.private != manager.key)
        if old:
            count = len(old)
            self.terminal.output("")
            self.terminal.output(
                f"{count} existing SSH {self._noun(count, 'key was', 'keys were')} found."
            )
        deleted_count = 0
        if old and not self.terminal.confirm(
            "Keep existing keys?", default=True, assume_yes=self.options.assume_yes
        ):
            eligible_old = github_correlated_local_keys(old, remote_existing)
            for key in eligible_old:
                self.terminal.output(
                    f"Eligible local key: {key.private} ({key.fingerprint or 'unknown fingerprint'})"
                )
            if not eligible_old:
                self.terminal.output(
                    "No old local keys are correlated with GitHub; nothing deleted"
                )
            elif self.terminal.confirm("Delete these keys?", destructive=True):
                old_fingerprints = frozenset(
                    key.fingerprint for key in eligible_old if key.fingerprint
                )
                remote_old = tuple(
                    key for key in remote_existing if key.fingerprint in old_fingerprints
                )
                for remote_key in remote_old:
                    self.terminal.output(
                        f"Eligible GitHub key: {remote_key.title} ({remote_key.fingerprint})"
                    )
                manager.delete_remote(
                    remote_old,
                    eligible_fingerprints=old_fingerprints,
                    explicit_confirmation=True,
                )
                manager.delete(eligible_old, explicit_confirmation=True)
                deleted_count = len(eligible_old)
                if not manager.verify():
                    raise RuntimeError("new SSH key failed reverification")
        if old and len(old) > deleted_count:
            self.summary.existing_keys_preserved = len(old) - deleted_count
            self.terminal.output("Existing SSH keys preserved")

    def _codex(self, workspace: Path, *, install_required: bool) -> None:
        codex = CodexManager(self.runner, self.options.home, workspace)
        if install_required or not codex.shared_bin.is_file():
            codex.install()
        codex.create_profiles()
        for number in ("01", "02"):
            if codex.verified(number):
                self.terminal.output(f"codex-{number} already authenticated")
                continue
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
                    ("gh", "config", "get", "git_protocol", "--host", "github.com"),
                    mutate=False,
                ),
                check=False,
            )
            results.append(
                CheckResult(
                    "GitHub SSH protocol",
                    protocol.stdout.strip() == "ssh",
                    "protocol is not SSH",
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
                    codex.profiles_distinct(),
                    "launcher CODEX_HOME values are not distinct",
                )
            )
            results.append(
                CheckResult(
                    "unscoped Codex launcher",
                    codex.no_unscoped_launcher(),
                    "~/.local/bin/codex must not exist",
                )
            )
        if Capability.SHELL in capabilities:
            results.append(verifier.shell_configuration(self._shell()))
        failures = [result for result in results if not result.passed]
        if failures:
            visible = failures if self.options.verbose else failures[:3]
            reason = "; ".join(f"{item.name}: {item.reason}" for item in visible)
            if len(visible) < len(failures):
                reason += f"; and {len(failures) - len(visible)} more checks"
            raise ValidationError("verification", "inspect workstation", reason)
        self.terminal.output("All checks passed")
