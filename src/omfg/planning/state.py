from __future__ import annotations

from pathlib import Path

from omfg.execution import Command, CommandRunner
from omfg.models import Package, Source


class StateInspector:
    def __init__(self, runner: CommandRunner, home: Path) -> None:
        self.runner = runner
        self.home = home

    def package_installed(self, package: Package) -> bool:
        if package.source in {Source.PACMAN, Source.AUR}:
            try:
                return (
                    self.runner.run(
                        Command(("pacman", "-Q", package.identifier), mutate=False), check=False
                    ).returncode
                    == 0
                )
            except FileNotFoundError:
                return False
        if package.source is Source.FLATPAK:
            try:
                return (
                    self.runner.run(
                        Command(("flatpak", "info", "--user", package.identifier), mutate=False),
                        check=False,
                    ).returncode
                    == 0
                )
            except FileNotFoundError:
                return False
        if package.source is Source.UPSTREAM and package.identifier == "codex":
            executable = self.home / ".local/share/omfg/bin/codex"
            if not executable.is_file():
                return False
            return (
                self.runner.run(
                    Command((str(executable), "--version"), mutate=False), check=False
                ).returncode
                == 0
            )
        return False

    def pending(self, packages: tuple[Package, ...]) -> tuple[Package, ...]:
        return tuple(package for package in packages if not self.package_installed(package))
