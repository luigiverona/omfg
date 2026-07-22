from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from omfg.errors import ValidationError
from omfg.execution import Command, CommandRunner


class PacmanManager:
    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    def full_update(self) -> None:
        self.runner.run(Command(("sudo", "pacman", "-Syu", "--noconfirm", "--needed")))

    def install(self, packages: Iterable[str]) -> None:
        names = tuple(sorted(set(packages)))
        if names:
            self.runner.run(Command(("sudo", "pacman", "-S", "--needed", "--noconfirm", *names)))


class AurManager:
    AUR_BASE = "https://aur.archlinux.org"

    def __init__(self, runner: CommandRunner, workspace: Path) -> None:
        self.runner = runner
        self.workspace = workspace

    def bootstrap_yay(self) -> None:
        if os.geteuid() == 0:
            raise ValidationError("aur", "bootstrap yay", "makepkg must not run as root")
        clone = self.workspace / "aur" / "yay-bin"
        self.runner.run(
            Command(("git", "clone", "--depth", "1", f"{self.AUR_BASE}/yay-bin.git", str(clone)))
        )
        origin = self.runner.run(
            Command(("git", "-C", str(clone), "remote", "get-url", "origin"), mutate=False)
        )
        if origin.stdout.strip() != f"{self.AUR_BASE}/yay-bin.git" and not self.runner.dry_run:
            raise ValidationError("aur", "validate yay", "unexpected AUR repository origin")
        self.runner.run(
            Command(("makepkg", "--syncdeps", "--cleanbuild", "--noconfirm"), cwd=clone)
        )
        package_list = self.runner.run(
            Command(("makepkg", "--packagelist"), cwd=clone, mutate=False)
        )
        artifacts = tuple(line.strip() for line in package_list.stdout.splitlines() if line.strip())
        if not artifacts and not self.runner.dry_run:
            raise ValidationError("aur", "bootstrap yay", "makepkg did not produce a package")
        if artifacts:
            self.runner.run(Command(("sudo", "pacman", "-U", "--noconfirm", *artifacts)))
        verification = self.runner.run(Command(("yay", "--version"), mutate=False), check=False)
        if verification.returncode and not self.runner.dry_run:
            raise ValidationError("aur", "verify yay", "yay is unavailable after installation")

    def install(self, packages: Iterable[str]) -> None:
        names = tuple(sorted(set(packages)))
        if names:
            self.runner.run(
                Command(
                    (
                        "yay",
                        "-S",
                        "--needed",
                        "--noconfirm",
                        "--builddir",
                        str(self.workspace / "aur"),
                        *names,
                    )
                )
            )


class FlatpakManager:
    REMOTE = "https://dl.flathub.org/repo/flathub.flatpakrepo"

    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    def ensure_flathub(self) -> None:
        present = self.runner.run(
            Command(("flatpak", "remotes", "--user", "--columns=name"), mutate=False), check=False
        )
        if "flathub" not in present.stdout.split():
            self.runner.run(
                Command(
                    ("flatpak", "remote-add", "--user", "--if-not-exists", "flathub", self.REMOTE)
                )
            )
        self.runner.run(Command(("flatpak", "update", "--user", "--appstream", "--noninteractive")))

    def install(self, applications: Iterable[str]) -> None:
        names = tuple(sorted(set(applications)))
        if names:
            self.runner.run(
                Command(
                    (
                        "flatpak",
                        "install",
                        "--user",
                        "--noninteractive",
                        "--or-update",
                        "flathub",
                        *names,
                    )
                )
            )
