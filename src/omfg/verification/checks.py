from __future__ import annotations

import platform
import shutil
from dataclasses import dataclass
from pathlib import Path

from omfg.config.shell import ShellInfo, path_configured
from omfg.execution import Command, CommandRunner
from omfg.models import Package, Source


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
        if package.source in {Source.PACMAN, Source.AUR}:
            result = self.runner.run(
                Command(("pacman", "-Q", package.identifier), mutate=False), check=False
            )
        elif package.source is Source.FLATPAK:
            result = self.runner.run(
                Command(("flatpak", "info", "--user", package.identifier), mutate=False),
                check=False,
            )
        else:
            executable = package.executable or package.identifier
            found = (self.home / ".local/bin" / executable).is_file() or shutil.which(
                executable
            ) is not None
            return CheckResult(package.name, found, "not installed")
        return CheckResult(package.name, result.returncode == 0, "not installed")

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
        result = self.runner.run(
            Command(("flatpak", "remotes", "--user", "--columns=name"), mutate=False),
            check=False,
        )
        return CheckResult("Flathub remote", "flathub" in result.stdout.split(), "missing")
