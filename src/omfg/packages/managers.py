from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from omfg.config.files import atomic_write
from omfg.errors import ValidationError
from omfg.execution import Command, CommandRunner


class PacmanManager:
    def __init__(self, runner: CommandRunner, workspace: Path | None = None) -> None:
        self.runner = runner
        self.workspace = workspace

    def _command(self, argv: tuple[str, ...], packages: tuple[str, ...]) -> Command:
        log_path = self.workspace / "logs/pacman.log" if self.workspace else None
        return Command(
            argv,
            failure_component="Package installation",
            failure_operation="install packages",
            failure_packages=packages,
            log_path=log_path,
        )

    def full_update(self) -> None:
        self.runner.run(self._command(("sudo", "pacman", "-Syu", "--noconfirm", "--needed"), ()))

    def install(self, packages: Iterable[str]) -> None:
        names = tuple(sorted(set(packages)))
        if names:
            self.runner.run(
                self._command(("sudo", "pacman", "-S", "--needed", "--noconfirm", *names), names)
            )


class AurManager:
    AUR_BASE = "https://aur.archlinux.org"

    def __init__(self, runner: CommandRunner, workspace: Path) -> None:
        self.runner = runner
        self.workspace = workspace

    def _makepkg_config(self) -> Path:
        source = Path("/etc/makepkg.conf")
        try:
            content = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValidationError("aur", "configure makepkg", str(exc)) from exc
        content += """

# omfg builds only requested top-level packages; debug packages are not release requirements.
for _omfg_index in "${!OPTIONS[@]}"; do
  case "${OPTIONS[_omfg_index]}" in
    debug|!debug) OPTIONS[_omfg_index]=!debug ;;
  esac
done
"""
        path = self.workspace / "state/makepkg.conf"
        atomic_write(path, content, 0o600)
        return path

    def bootstrap_yay(self) -> None:
        if os.geteuid() == 0:
            raise ValidationError("aur", "bootstrap yay", "makepkg must not run as root")
        clone = self.workspace / "aur" / "yay-bin"
        makepkg_config = self._makepkg_config()
        self.runner.run(
            Command(("git", "clone", "--depth", "1", f"{self.AUR_BASE}/yay-bin.git", str(clone)))
        )
        origin = self.runner.run(
            Command(("git", "-C", str(clone), "remote", "get-url", "origin"), mutate=False)
        )
        if origin.stdout.strip() != f"{self.AUR_BASE}/yay-bin.git" and not self.runner.dry_run:
            raise ValidationError("aur", "validate yay", "unexpected AUR repository origin")
        metadata = self.runner.run(
            Command(
                ("makepkg", "--config", str(makepkg_config), "--printsrcinfo"),
                cwd=clone,
                mutate=False,
            )
        )
        if not self.runner.dry_run and not (
            "pkgbase = yay-bin" in metadata.stdout and "pkgname = yay-bin" in metadata.stdout
        ):
            raise ValidationError("aur", "validate yay", "unexpected AUR package metadata")
        self.runner.run(
            Command(
                ("makepkg", "--config", str(makepkg_config), "--cleanbuild", "--noconfirm"),
                cwd=clone,
            )
        )
        package_list = self.runner.run(
            Command(
                ("makepkg", "--config", str(makepkg_config), "--packagelist"),
                cwd=clone,
                mutate=False,
            )
        )
        candidates = tuple(
            line.strip() for line in package_list.stdout.splitlines() if line.strip()
        )
        artifacts = tuple(
            artifact
            for artifact in candidates
            if Path(artifact).name.startswith("yay-bin-")
            and not Path(artifact).name.startswith("yay-bin-debug-")
        )
        if not artifacts and not self.runner.dry_run:
            raise ValidationError("aur", "bootstrap yay", "makepkg did not produce a package")
        if artifacts:
            self.runner.run(
                Command(
                    ("sudo", "pacman", "-U", "--noconfirm", *artifacts),
                    failure_component="AUR bootstrap",
                    failure_operation="install yay",
                    failure_packages=("yay-bin",),
                    log_path=self.workspace / "logs/aur-bootstrap.log",
                )
            )
        verification = self.runner.run(Command(("yay", "--version"), mutate=False), check=False)
        if verification.returncode and not self.runner.dry_run:
            raise ValidationError("aur", "verify yay", "yay is unavailable after installation")

    def install(self, packages: Iterable[str]) -> None:
        names = tuple(sorted(set(packages)))
        if not names:
            return
        makepkg_config = self._makepkg_config()
        for name in names:
            self.runner.run(
                Command(
                    (
                        "yay",
                        "-S",
                        "--needed",
                        "--noconfirm",
                        "--builddir",
                        str(self.workspace / "aur"),
                        "--makepkgconf",
                        str(makepkg_config),
                        name,
                    ),
                    failure_component="AUR installation",
                    failure_operation="install packages",
                    failure_packages=(name,),
                    log_path=self.workspace / "logs/aur.log",
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
