from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path

from omfg.config.files import atomic_write
from omfg.errors import CommandError, ValidationError
from omfg.execution import Command, CommandRunner


class CodexManager:
    INSTALLER_URL = "https://chatgpt.com/codex/install.sh"
    # Audited against the official installer on 2026-07-21. Upstream changes fail closed.
    INSTALLER_SHA256 = "1154e9daf713aacd1534efca8042bfd6665ad24bc1d1dfd86b8f439fe60a7a5d"

    def __init__(self, runner: CommandRunner, home: Path, workspace: Path | None = None) -> None:
        self.runner = runner
        self.home = home
        self.state_root = home / ".local/share/omfg/codex"
        self.bin_dir = home / ".local/bin"
        self.shared_bin = home / ".local/share/omfg/bin/codex"
        self.workspace = workspace

    def install(self) -> None:
        if self.state_root.is_symlink() or self.shared_bin.parent.is_symlink():
            raise OSError("refusing Codex installation through symbolic state directories")
        self.shared_bin.parent.mkdir(parents=True, exist_ok=True)
        installer_state = self.state_root / "installer"
        isolated_home = installer_state / "environment-home"
        for directory in (installer_state, isolated_home):
            if directory.is_symlink():
                raise OSError(f"refusing symbolic Codex installer directory: {directory}")
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            directory.chmod(0o700)
        env = {
            "CODEX_INSTALL_DIR": str(self.shared_bin.parent),
            "CODEX_HOME": str(installer_state),
            "CODEX_RELEASE": "latest",
            "CODEX_NON_INTERACTIVE": "1",
            "HOME": str(isolated_home),
            "SHELL": "/bin/sh",
            "PATH": os.defpath,
            "LC_ALL": "C",
        }
        for name in (
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "https_proxy",
            "http_proxy",
            "all_proxy",
            "no_proxy",
        ):
            if value := os.environ.get(name):
                env[name] = value
        download_root = self.workspace / "downloads" if self.workspace else installer_state
        installer = download_root / "codex-install.sh"
        self.runner.run(
            Command(
                (
                    "curl",
                    "-fsSL",
                    "--proto",
                    "=https",
                    "--tlsv1.2",
                    "-o",
                    str(installer),
                    self.INSTALLER_URL,
                )
            )
        )
        if not self.runner.dry_run:
            digest = hashlib.sha256(installer.read_bytes()).hexdigest()
            if digest != self.INSTALLER_SHA256:
                raise ValidationError(
                    "codex",
                    "verify official installer",
                    "installer checksum mismatch; omfg must audit the upstream change",
                )
        self.runner.run(Command(("sh", str(installer)), env=env, replace_env=True))
        if not self.runner.dry_run and not self.shared_bin.is_file():
            raise ValidationError(
                "codex",
                "install official release",
                "installer did not create the shared executable",
            )
        version = self.runner.run(
            Command((str(self.shared_bin), "--version"), mutate=False), check=False
        )
        if version.returncode and not self.runner.dry_run:
            raise ValidationError("codex", "verify executable", "shared executable is not runnable")
        self.remove_owned_unscoped_launcher()

    def executable_valid(self) -> bool:
        if not self.shared_bin.is_file():
            return False
        result = self.runner.run(
            Command((str(self.shared_bin), "--version"), mutate=False), check=False
        )
        return result.returncode == 0

    def unrelated_codex(self) -> Path | None:
        found = shutil.which("codex")
        if not found:
            return None
        path = Path(found)
        try:
            if path.resolve(strict=False) == self.shared_bin.resolve(strict=False):
                return None
        except OSError:
            pass
        return path

    def create_profiles(self) -> None:
        for number in ("01", "02"):
            profile = self.state_root / number
            if not self.runner.dry_run:
                if profile.is_symlink():
                    raise OSError(f"refusing symbolic Codex profile: {profile}")
                profile.mkdir(parents=True, mode=0o700, exist_ok=True)
                profile.chmod(0o700)
                config = profile / "config.toml"
                existing = config.read_text(encoding="utf-8") if config.exists() else ""
                setting = 'cli_auth_credentials_store = "file"'
                pattern = re.compile(r"(?m)^\s*cli_auth_credentials_store\s*=.*$")
                if pattern.search(existing):
                    content = pattern.sub(setting, existing, count=1)
                else:
                    content = existing
                    if content and not content.endswith("\n"):
                        content += "\n"
                    content += setting + "\n"
                atomic_write(config, content, 0o600)
                launcher = (
                    f'#!/bin/sh\nexport CODEX_HOME="{profile}"\nexec "{self.shared_bin}" "$@"\n'
                )
                atomic_write(self.bin_dir / f"codex-{number}", launcher, 0o700)
        self.remove_owned_unscoped_launcher()

    def authenticate(self, number: str) -> None:
        launcher = str(self.bin_dir / f"codex-{number}")
        try:
            self.runner.run(
                Command(
                    (launcher, "login"),
                    failure_component="codex",
                    failure_operation=f"authenticate codex-{number}",
                )
            )
        except CommandError as exc:
            raise ValidationError(
                "codex",
                f"authenticate codex-{number}",
                "sign-in was cancelled or did not complete",
                exc.exit_code,
            ) from exc

    def verified(self, number: str) -> bool:
        profile = self.state_root / number
        launcher = self.bin_dir / f"codex-{number}"
        if not (profile.is_dir() and launcher.is_file() and self.shared_bin.is_file()):
            return False
        if profile.stat().st_mode & 0o077 or launcher.stat().st_mode & 0o077:
            return False
        expected = f'export CODEX_HOME="{profile}"'
        if expected not in launcher.read_text(encoding="utf-8").splitlines():
            return False
        auth_file = profile / "auth.json"
        if auth_file.is_symlink() or (auth_file.exists() and auth_file.stat().st_mode & 0o077):
            return False
        result = self.runner.run(
            Command((str(launcher), "login", "status"), mutate=False), check=False
        )
        return result.returncode == 0

    def no_unscoped_launcher(self) -> bool:
        target = self.bin_dir / "codex"
        return not target.exists() and not target.is_symlink()

    def remove_owned_unscoped_launcher(self) -> None:
        target = self.bin_dir / "codex"
        if not target.is_symlink():
            return
        try:
            resolved = target.resolve(strict=False)
        except OSError:
            return
        if resolved == self.shared_bin.resolve(strict=False):
            target.unlink()

    def profiles_distinct(self) -> bool:
        launchers = [self.bin_dir / f"codex-{number}" for number in ("01", "02")]
        if not all(path.is_file() for path in launchers):
            return False
        contents = [path.read_text(encoding="utf-8") for path in launchers]
        return (
            str(self.state_root / "01") in contents[0]
            and str(self.state_root / "02") in contents[1]
            and contents[0] != contents[1]
        )
