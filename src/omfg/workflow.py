from __future__ import annotations

import getpass
import os
import shutil
from collections.abc import Callable
from functools import partial
from pathlib import Path

from omfg.config.codex import CodexManager
from omfg.config.git import GitConfigurator, GitIdentity
from omfg.config.github import GitHubConfigurator
from omfg.config.shell import ShellInfo, configure_path, detect_shell
from omfg.config.ssh import SSHManager
from omfg.config.ssh_inventory import github_correlated_local_keys
from omfg.errors import OmfgError, ValidationError
from omfg.execution import Command, CommandRunner, TemporaryWorkspace
from omfg.models import (
    STAGE_ORDER,
    Capability,
    Package,
    PackageKind,
    Plan,
    RunOptions,
    Source,
    WorkflowProgress,
    WorkflowStage,
)
from omfg.packages import AurManager, FlatpakManager, PacmanManager
from omfg.planning import StateInspector
from omfg.system import validate_system
from omfg.ui import Terminal
from omfg.verification.checks import CheckResult, Verifier

STAGE_CAPABILITIES: dict[WorkflowStage, frozenset[Capability]] = {
    WorkflowStage.SYSTEM: frozenset({Capability.SYSTEM}),
    WorkflowStage.APPLICATIONS: frozenset({Capability.APPS}),
    WorkflowStage.FLATPAK: frozenset({Capability.FLATPAK, Capability.FLATHUB}),
    WorkflowStage.GIT: frozenset({Capability.GIT}),
    WorkflowStage.GITHUB: frozenset({Capability.GITHUB}),
    WorkflowStage.SSH: frozenset({Capability.SSH}),
    WorkflowStage.CODEX: frozenset({Capability.CODEX}),
    WorkflowStage.SHELL: frozenset({Capability.SHELL}),
    WorkflowStage.VERIFICATION: frozenset({Capability.CHECK}),
}

SOURCE_LABELS = {
    Source.PACMAN: "Arch Linux",
    Source.AUR: "the AUR",
    Source.FLATPAK: "Flatpak",
    Source.UPSTREAM: "the official upstream release",
}


class Workflow:
    def __init__(
        self,
        plan: Plan,
        options: RunOptions,
        terminal: Terminal,
        *,
        runner: CommandRunner | None = None,
        target_shell: ShellInfo | None = None,
        system_release: Path = Path("/etc/os-release"),
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
        self.system_release = system_release
        self.progress = WorkflowProgress(())
        self._pending_before: tuple[Package, ...] = ()
        self._pending_after_update: tuple[Package, ...] = ()

    def _shell(self) -> ShellInfo:
        if self.target_shell is None:
            self.target_shell = detect_shell()
        return self.target_shell

    def _capabilities(self) -> set[Capability]:
        capabilities = set(self.plan.selected) | set(self.plan.prerequisites)
        if Capability.CHECK in capabilities:
            return set(Capability)
        return capabilities

    def _selected_stages(self, pending: tuple[Package, ...]) -> tuple[WorkflowStage, ...]:
        capabilities = self._capabilities()
        privileged = any(package.source in {Source.PACMAN, Source.AUR} for package in pending)
        stages: set[WorkflowStage] = {WorkflowStage.VERIFICATION}
        if Capability.SYSTEM in capabilities or privileged:
            stages.add(WorkflowStage.ADMINISTRATOR)
        for stage, requirements in STAGE_CAPABILITIES.items():
            if capabilities & requirements:
                stages.add(stage)
        if pending and any(package.source in {Source.PACMAN, Source.AUR} for package in pending):
            stages.add(WorkflowStage.APPLICATIONS)
        return tuple(stage for stage in STAGE_ORDER if stage in stages)

    def run(self) -> int:
        try:
            validate_system(require_network=not self.options.dry_run)
            if self.options.verbose:
                shell = self._shell()
                self.terminal.output(f"Environment: Arch Linux, {shell.name}.")
            inspector = StateInspector(self.runner, self.options.home)
            self._pending_before = inspector.pending(self.plan.packages)
            self._pending_after_update = self._pending_before
            self.progress = WorkflowProgress(self._selected_stages(self._pending_before))
            if self.plan.selected == (Capability.CHECK,) and not self.plan.prerequisites:
                self._verify()
                return 0
            self._render_plan(self._pending_before)
            if self.options.dry_run:
                self.terminal.output("Dry run: no changes will be made.")
                self._render_verbose_plan(self._pending_before)
                return 0
            if not self.terminal.confirm("Continue?", assume_yes=self.options.assume_yes):
                self.terminal.output("")
                self.terminal.output("No changes were made.")
                return 0
            with TemporaryWorkspace(keep=self.options.keep_temp) as workspace:
                if self.options.verbose and workspace.path is not None:
                    self.terminal.output(f"Temporary workspace: {workspace.path}.")
                self._mutate(workspace.path or Path("/tmp"), inspector)
                self._verify()
            self._render_completion()
            return 0
        except KeyboardInterrupt:
            self._render_interruption()
            return 130
        except OmfgError as exc:
            self._render_error(exc)
            return exc.exit_code

    @staticmethod
    def _noun(count: int, singular: str, plural: str | None = None) -> str:
        return singular if count == 1 else (plural or singular + "s")

    @staticmethod
    def _number(count: int) -> str:
        words = {
            0: "zero",
            1: "one",
            2: "two",
            3: "three",
            4: "four",
            5: "five",
            6: "six",
            7: "seven",
            8: "eight",
            9: "nine",
            10: "ten",
            11: "eleven",
            12: "twelve",
            13: "thirteen",
            14: "fourteen",
            15: "fifteen",
        }
        return words.get(count, str(count))

    @staticmethod
    def _join(items: list[str]) -> str:
        if len(items) == 1:
            return items[0]
        if len(items) == 2:
            return f"{items[0]} and {items[1]}"
        return ", ".join(items[:-1]) + f", and {items[-1]}"

    def _applications(self) -> tuple[Package, ...]:
        return tuple(
            package for package in self.plan.packages if package.kind is PackageKind.APPLICATION
        )

    def _render_plan(self, pending: tuple[Package, ...]) -> None:
        self.terminal.section("Plan")
        for stage in self.progress.selected:
            self.terminal.output(f"{stage.value}.")
        applications = set(self._applications())
        source_order = {Source.PACMAN: 0, Source.AUR: 1, Source.FLATPAK: 2, Source.UPSTREAM: 3}
        missing = tuple(
            sorted(
                (package for package in pending if package in applications),
                key=lambda package: (source_order[package.source], package.name),
            )
        )
        self.terminal.output("")
        if len(missing) == 1:
            package = missing[0]
            self.terminal.output(
                f"Missing application: {package.name} from {SOURCE_LABELS[package.source]}."
            )
        elif missing:
            self.terminal.output("Missing applications:")
            self.terminal.output("")
            for package in missing:
                self.terminal.output(f"{package.name} from {SOURCE_LABELS[package.source]}.")
        total = len(self.plan.packages)
        present = total - len(pending)
        if not pending:
            self.terminal.output("All software requirements are already present.")
        elif total:
            self.terminal.output(
                f"{self._number(present).capitalize()} of {self._number(total)} software "
                f"{self._noun(total, 'requirement')} are already present."
            )
        self.terminal.output("")

    def _render_verbose_plan(self, pending: tuple[Package, ...]) -> None:
        if not self.options.verbose:
            return
        self.terminal.output(
            "Selected capabilities: " + ", ".join(c.value for c in self.plan.selected) + "."
        )
        for package in self.plan.packages:
            state = "pending" if package in pending else "present"
            self.terminal.output(f"{package.source.value}: {package.identifier} ({state}).")

    def _begin(self, stage: WorkflowStage) -> None:
        self.progress.begin(stage)
        self.terminal.section(stage.value)

    def _finish(self, stage: WorkflowStage) -> None:
        self.progress.finish(stage)

    def _mutate(self, workspace: Path, inspector: StateInspector) -> None:
        pacman = PacmanManager(self.runner, workspace)
        if WorkflowStage.ADMINISTRATOR in self.progress.selected:
            self._begin(WorkflowStage.ADMINISTRATOR)
            self.terminal.output("Sudo will ask for your password.")
            self.progress.mutation_started = True
            self.runner.run(Command(("sudo", "-v")))
            self._finish(WorkflowStage.ADMINISTRATOR)
        if WorkflowStage.SYSTEM in self.progress.selected:
            self._begin(WorkflowStage.SYSTEM)
            self.terminal.output("Updating Arch Linux...")
            changed = pacman.full_update()
            self.terminal.output(
                "System updated." if changed else "The system is already up to date."
            )
            self._pending_after_update = inspector.pending(self._pending_before)
            self._finish(WorkflowStage.SYSTEM)
        if WorkflowStage.APPLICATIONS in self.progress.selected:
            self._begin(WorkflowStage.APPLICATIONS)
            self._install_applications(workspace, pacman, inspector)
            self._finish(WorkflowStage.APPLICATIONS)
        if WorkflowStage.FLATPAK in self.progress.selected:
            self._begin(WorkflowStage.FLATPAK)
            self._flatpak(inspector)
            self._finish(WorkflowStage.FLATPAK)
        if WorkflowStage.GIT in self.progress.selected:
            self._begin(WorkflowStage.GIT)
            self._git()
            self._finish(WorkflowStage.GIT)
        if WorkflowStage.GITHUB in self.progress.selected:
            self._begin(WorkflowStage.GITHUB)
            self._github()
            self._finish(WorkflowStage.GITHUB)
        if WorkflowStage.SSH in self.progress.selected:
            self._begin(WorkflowStage.SSH)
            self._ssh()
            self._finish(WorkflowStage.SSH)
        if WorkflowStage.CODEX in self.progress.selected:
            self._begin(WorkflowStage.CODEX)
            self._codex(workspace)
            self._finish(WorkflowStage.CODEX)
        if WorkflowStage.SHELL in self.progress.selected:
            self._begin(WorkflowStage.SHELL)
            shell = self._shell()
            update = configure_path(self.options.home, shell)
            if update.changed:
                self.terminal.output(f"Added ~/.local/bin to the {shell.name} PATH.")
                if update.new_session_required:
                    self.terminal.output("Open a new shell session to use the command.")
                else:
                    self.terminal.output("The current shell can already use the command.")
            else:
                self.terminal.output(f"The {shell.name} PATH already includes ~/.local/bin.")
            self._finish(WorkflowStage.SHELL)

    def _install_applications(
        self, workspace: Path, pacman: PacmanManager, inspector: StateInspector
    ) -> None:
        initial_apps = tuple(
            package
            for package in self._pending_before
            if package.kind is PackageKind.APPLICATION
            and package.source in {Source.PACMAN, Source.AUR}
        )
        pending = inspector.pending(self._pending_after_update)
        current_apps = tuple(
            package
            for package in pending
            if package.kind is PackageKind.APPLICATION
            and package.source in {Source.PACMAN, Source.AUR}
        )
        satisfied = tuple(package for package in initial_apps if package not in current_apps)
        for package in satisfied:
            self.terminal.output(f"{package.name} was installed during the system update.")
        native = tuple(p for p in pending if p.source is Source.PACMAN)
        aur = tuple(p for p in pending if p.source is Source.AUR and p.identifier != "yay-bin")
        yay_pending = any(p.source is Source.AUR and p.identifier == "yay-bin" for p in pending)
        install_apps = tuple(p for p in (*native, *aur) if p.kind is PackageKind.APPLICATION)
        if not initial_apps and not install_apps:
            self.terminal.output("All selected applications are already installed.")
        elif satisfied and not install_apps:
            self.terminal.output("No additional package installation was needed.")
        elif len(install_apps) == 1:
            self.terminal.output(f"Installing {install_apps[0].name}...")
        elif install_apps:
            self.terminal.output(f"Installing {self._number(len(install_apps))} applications.")
        elif pending:
            self.terminal.output("Installing required software...")
        self.progress.mutation_started = self.progress.mutation_started or bool(pending)
        pacman.install(p.identifier for p in native)
        if aur or yay_pending:
            manager = AurManager(self.runner, workspace)
            if not shutil.which("yay"):
                pacman.install(("git", "base-devel"))
                manager.bootstrap_yay()
            manager.install(p.identifier for p in aur)
        remaining = inspector.pending(tuple((*native, *aur)))
        for package in install_apps:
            if package not in remaining:
                self.terminal.output(f"{package.name} installed.")

    def _flatpak(self, inspector: StateInspector) -> None:
        manager = FlatpakManager(self.runner)
        remotes = self.runner.run(
            Command(("flatpak", "remotes", "--user", "--columns=name"), mutate=False),
            check=False,
        )
        configured = "flathub" in remotes.stdout.split()
        if configured:
            self.terminal.output("Flathub is already configured.")
        else:
            self.terminal.output("Configuring Flathub...")
        changed = manager.ensure_flathub()
        if changed:
            self.terminal.output("Flathub configured.")
        applications = tuple(
            p
            for p in self.plan.packages
            if p.kind is PackageKind.APPLICATION and p.source is Source.FLATPAK
        )
        pending = inspector.pending(applications)
        if not pending:
            self.terminal.output("All selected Flatpak applications are already installed.")
            return
        for package in pending:
            self.terminal.output(f"Installing {package.name}...")
        manager.install(p.identifier for p in pending)
        remaining = inspector.pending(pending)
        for package in pending:
            if package not in remaining:
                self.terminal.output(f"{package.name} installed.")

    def _git(self) -> None:
        git = GitConfigurator(self.runner)
        existing_name = git.get("user.name")
        existing_email = git.get("user.email")
        existing = bool(existing_name and existing_email)
        if existing:
            original = GitIdentity(existing_name or "", existing_email or "")
            self.terminal.output(f"Name: {original.name}")
            self.terminal.output(f"Email: {original.email}")
            if self.terminal.confirm(
                "Keep this identity?", default=True, assume_yes=self.options.assume_yes
            ):
                git.configure(original)
                self.terminal.output("Git identity unchanged.")
                return
            replacement = GitIdentity(
                self._identity_value("New name: ", "name"),
                self._identity_value("New email: ", "email"),
            )
            if self.terminal.confirm("Use this identity?", default=True):
                git.configure(replacement)
                self.terminal.output("Git identity updated.")
            else:
                git.configure(original)
                self.terminal.output("Git identity unchanged.")
            return

        identity = GitIdentity(
            self._identity_value("Name: ", "name"),
            self._identity_value("Email: ", "email"),
        )
        if self.terminal.confirm(
            "Use this identity?", default=True, assume_yes=self.options.assume_yes
        ):
            git.configure(identity)
            self.terminal.output("Git identity saved.")

    def _identity_value(self, prompt: str, field: str) -> str:
        value = self.terminal.input(prompt).strip()
        if not value:
            raise ValidationError("Git", "configure identity", f"{field} cannot be empty")
        return value

    def _github(self) -> None:
        github = GitHubConfigurator(self.runner)
        authenticated = github.authenticated()
        protocol = github.protocol()
        if authenticated:
            account = github.account() or "the configured account"
            self.terminal.output(f"Already signed in as {account}.")
        else:
            self.terminal.output("Starting browser authentication...")
        github.authenticate()
        if not authenticated:
            account = github.account() or "the authenticated account"
            self.terminal.output(f"Signed in as {account}.")
        if protocol == "ssh":
            self.terminal.output("Git protocol already uses SSH.")
        else:
            self.terminal.output("Git protocol changed to SSH.")

    def _ssh(self) -> None:
        manager = SSHManager(self.runner, self.options.home)
        existing = manager.inventory()
        remote_existing = manager.inventory_remote()
        account = GitHubConfigurator(self.runner).account() or getpass.getuser()
        dedicated_before = any(key.private == manager.key for key in existing)
        if dedicated_before:
            self.terminal.output("The dedicated key already exists.")
        else:
            self.terminal.output("Creating a dedicated SSH key...")
        created = manager.create(
            GitConfigurator(self.runner).get("user.email") or f"{account}@users.noreply.github.com"
        )
        if created:
            self.terminal.output("The dedicated key was created.")
        dedicated = next((key for key in manager.inventory() if key.private == manager.key), None)
        registered = bool(
            dedicated
            and dedicated.fingerprint
            and any(key.fingerprint == dedicated.fingerprint for key in remote_existing)
        )
        if registered:
            self.terminal.output("The key is registered with GitHub.")
        else:
            self.terminal.output("Registering the key with GitHub...")
            manager.upload(f"omfg-{os.uname().nodename}")
            self.terminal.output("The key was registered with GitHub.")
        if not manager.verify():
            raise ValidationError("SSH", "verify GitHub connection", "authentication failed")
        self.terminal.output("The GitHub connection was verified.")
        old = tuple(key for key in existing if key.private != manager.key)
        deleted_count = 0
        if old and not self.terminal.confirm(
            "Keep existing keys?", default=True, assume_yes=self.options.assume_yes
        ):
            eligible_old = github_correlated_local_keys(old, remote_existing)
            for key in eligible_old:
                self.terminal.output(
                    f"Eligible local key: {key.private} ({key.fingerprint or 'unknown fingerprint'})."
                )
            if not eligible_old:
                self.terminal.output(
                    "No old local keys are correlated with GitHub; nothing was deleted."
                )
            elif self.terminal.confirm("Delete these keys?", destructive=True):
                fingerprints = frozenset(key.fingerprint for key in eligible_old if key.fingerprint)
                remote_old = tuple(k for k in remote_existing if k.fingerprint in fingerprints)
                manager.delete_remote(
                    remote_old,
                    eligible_fingerprints=fingerprints,
                    explicit_confirmation=True,
                )
                manager.delete(eligible_old, explicit_confirmation=True)
                deleted_count = len(eligible_old)
                if not manager.verify():
                    raise ValidationError("SSH", "reverify dedicated key", "authentication failed")
        if old and len(old) > deleted_count:
            self.terminal.output("Existing SSH keys were preserved.")

    def _codex(self, workspace: Path) -> None:
        codex = CodexManager(self.runner, self.options.home, workspace)
        unrelated = codex.unrelated_codex()
        if unrelated is not None and self.options.verbose:
            self.terminal.output(f"Unrelated Codex installation preserved: {unrelated}.")
        if not codex.executable_valid():
            codex.install()
        codex.create_profiles()
        for number in ("01", "02"):
            self.progress.codex_profile = number
            if codex.verified(number):
                self.terminal.output(f"codex-{number} is already signed in.")
                continue
            self.terminal.output(f"codex-{number} is not signed in.")
            self.terminal.output(f"Starting sign-in for codex-{number}...")
            codex.authenticate(number)
            if not codex.verified(number):
                raise ValidationError(
                    "Codex",
                    f"authenticate codex-{number}",
                    "sign-in was cancelled or did not complete",
                )
            self.terminal.output(f"codex-{number} signed in.")
        self.progress.codex_profile = None
        self.terminal.output("Both Codex profiles are ready.")

    def _verify(self) -> None:
        self._begin(WorkflowStage.VERIFICATION)
        results = self.verification_results()
        failures = [result for result in results if not result.passed]
        if failures:
            visible = failures if self.options.verbose else failures[:3]
            reason = "; ".join(f"{item.name}: {item.reason}" for item in visible)
            if len(visible) < len(failures):
                reason += f"; and {len(failures) - len(visible)} more checks"
            raise ValidationError("Verification", "inspect workstation", reason)
        self.render_readiness()
        self.terminal.output("All verification checks passed.")
        self._finish(WorkflowStage.VERIFICATION)

    @staticmethod
    def _availability_check(
        name: str,
        operation: Callable[[], bool],
        reason: str,
        unavailable: str,
        *,
        tolerate_missing: bool,
    ) -> CheckResult:
        try:
            passed = operation()
        except FileNotFoundError:
            if not tolerate_missing:
                raise
            return CheckResult(name, False, unavailable)
        return CheckResult(name, passed, reason)

    def verification_results(self, *, read_only: bool = False) -> list[CheckResult]:
        verifier = Verifier(self.runner, self.options.home)
        capabilities = self._capabilities()
        results = [
            verifier.system(self.system_release),
            *(verifier.package(p) for p in self.plan.packages),
        ]
        if Capability.FLATHUB in capabilities:
            results.append(verifier.flathub())
        if Capability.GIT in capabilities:
            results.append(
                self._availability_check(
                    "Git identity",
                    GitConfigurator(self.runner).verify,
                    "identity or default branch missing",
                    "Git is not installed",
                    tolerate_missing=read_only,
                )
            )
        if Capability.GITHUB in capabilities:
            github = GitHubConfigurator(self.runner)
            results.append(
                self._availability_check(
                    "GitHub authentication",
                    github.authenticated,
                    "not authenticated",
                    "GitHub CLI is not installed",
                    tolerate_missing=read_only,
                )
            )
            results.append(
                self._availability_check(
                    "GitHub SSH protocol",
                    lambda: github.protocol() == "ssh",
                    "protocol is not SSH",
                    "GitHub CLI is not installed",
                    tolerate_missing=read_only,
                )
            )
        if Capability.SSH in capabilities:
            results.append(
                self._availability_check(
                    "SSH connection",
                    lambda: SSHManager(self.runner, self.options.home).verify(read_only=read_only),
                    "connection failed",
                    "OpenSSH client is not installed",
                    tolerate_missing=read_only,
                )
            )
        if Capability.CODEX in capabilities:
            codex = CodexManager(self.runner, self.options.home)
            for number in ("01", "02"):
                results.append(
                    self._availability_check(
                        f"codex-{number}",
                        partial(codex.verified, number),
                        "profile not authenticated",
                        "managed Codex executable is not available",
                        tolerate_missing=read_only,
                    )
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
        return results

    def render_readiness(self) -> None:
        capabilities = self._capabilities()
        if self.plan.packages:
            self.terminal.output("All software requirements are ready.")
        if Capability.GIT in capabilities:
            self.terminal.output("The Git identity is ready.")
        if Capability.GITHUB in capabilities:
            self.terminal.output("GitHub authentication is ready.")
        if Capability.SSH in capabilities:
            self.terminal.output("The SSH connection is ready.")
        if Capability.CODEX in capabilities:
            self.terminal.output("Both Codex profiles are ready.")
        if Capability.SHELL in capabilities:
            self.terminal.output("The shell PATH is ready.")

    def _render_completion(self) -> None:
        self.terminal.output("")
        self.terminal.output("Setup complete.")
        self.terminal.output("Workstation ready.")

    @staticmethod
    def _sentence_stage(stage: WorkflowStage, first: bool) -> str:
        names = {
            WorkflowStage.ADMINISTRATOR: "administrator access",
            WorkflowStage.SYSTEM: "system update",
            WorkflowStage.APPLICATIONS: "applications",
            WorkflowStage.FLATPAK: "Flatpak",
            WorkflowStage.GIT: "Git",
            WorkflowStage.GITHUB: "GitHub",
            WorkflowStage.SSH: "SSH",
            WorkflowStage.CODEX: "Codex",
            WorkflowStage.SHELL: "shell PATH",
            WorkflowStage.VERIFICATION: "verification",
        }
        value = names[stage]
        return value[0].upper() + value[1:] if first else value

    def _render_interruption(self) -> None:
        current = self.progress.current or next(iter(self.progress.remaining), None)
        if current is not None:
            current_names = {
                WorkflowStage.CODEX: "Codex configuration",
                WorkflowStage.ADMINISTRATOR: "administrator access",
                WorkflowStage.SYSTEM: "system update",
                WorkflowStage.APPLICATIONS: "application installation",
                WorkflowStage.FLATPAK: "Flatpak configuration",
                WorkflowStage.GIT: "Git configuration",
                WorkflowStage.GITHUB: "GitHub configuration",
                WorkflowStage.SSH: "SSH configuration",
                WorkflowStage.SHELL: "shell PATH configuration",
                WorkflowStage.VERIFICATION: "verification",
            }
            self.terminal.output("")
            self.terminal.output(f"Setup paused during {current_names[current]}.")
        if self.progress.completed:
            completed = [
                self._sentence_stage(stage, index == 0)
                for index, stage in enumerate(self.progress.completed)
            ]
            self.terminal.output(f"Completed: {self._join(completed)}.")
        remaining = [
            self._sentence_stage(stage, index == 0)
            for index, stage in enumerate(self.progress.remaining)
        ]
        if remaining:
            self.terminal.output(f"Remaining: {self._join(remaining)}.")
        if self.progress.completed or self.progress.mutation_started:
            self.terminal.output("Completed changes were preserved.")
        else:
            self.terminal.output("No setup stages were completed.")
        self.terminal.output("Run omfg again to continue.")

    def _render_error(self, error: OmfgError) -> None:
        if error.packages:
            by_identifier = {package.identifier: package.name for package in self.plan.packages}
            names = tuple(by_identifier.get(package, package) for package in error.packages)
            if self.progress.current is WorkflowStage.APPLICATIONS and len(names) == 1:
                self.terminal.output(f"{names[0]} could not be installed.")
            else:
                self.terminal.output(f"{error.component.rstrip('.')} failed.")
            self.terminal.output(f"Reason: {error.reason.rstrip('.')}.")
            if error.log_path:
                self.terminal.output(f"Details: {error.log_path}.")
            self.terminal.output("Run omfg --verbose for complete command output.")
            return
        self.terminal.error(error.component, error.operation, error.reason, error.log_path)
