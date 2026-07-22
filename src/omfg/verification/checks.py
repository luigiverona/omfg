from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

from omfg.config.shell import ShellInfo, path_configured
from omfg.execution import Command, CommandRunner
from omfg.models import Package
from omfg.planning.state import StateInspector


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    reason: str = ""


class Verifier:
    def __init__(self, runner: CommandRunner, home: Path) -> None:
        self.runner = runner
        self.home = home

    def system(self) -> CheckResult:
        os_release = Path("/etc/os-release").read_text(encoding="utf-8")
        good = "ID=arch" in os_release and platform.machine() == "x86_64"
        return CheckResult("supported system", good, "Arch Linux x86_64 required")

    def package(self, package: Package) -> CheckResult:
        installed = StateInspector(self.runner, self.home).package_installed(package)
        return CheckResult(package.name, installed, "not installed")

    def path(self) -> CheckResult:
        expected = str(self.home / ".local/bin")
        import os

        return CheckResult(
            "shell PATH",
            expected in os.environ.get("PATH", "").split(":"),
            "new shell session required",
        )

    def shell_configuration(self, shell: ShellInfo) -> CheckResult:
        return CheckResult(
            "shell PATH configuration",
            path_configured(self.home, shell),
            f"~/.local/bin is not configured for {shell.name}",
        )

    def flathub(self) -> CheckResult:
        try:
            result = self.runner.run(
                Command(("flatpak", "remotes", "--user", "--columns=name"), mutate=False),
                check=False,
            )
        except FileNotFoundError:
            return CheckResult("Flathub remote", False, "Flatpak is not installed")
        return CheckResult("Flathub remote", "flathub" in result.stdout.split(), "missing")
