from __future__ import annotations

import shutil
from pathlib import Path

from omfg.config.files import atomic_write
from omfg.execution import Command, CommandRunner


class CodexManager:
    def __init__(self, runner: CommandRunner, home: Path, workspace: Path | None = None) -> None:
        self.runner = runner
        self.home = home
        self.state_root = home / ".local/share/omfg/codex"
        self.bin_dir = home / ".local/bin"
        self.shared_bin = home / ".local/share/omfg/bin/codex"
        self.workspace = workspace

    def install(self) -> None:
        self.shared_bin.parent.mkdir(parents=True, exist_ok=True)
        env = {
            "CODEX_INSTALL_DIR": str(self.shared_bin.parent),
            "CODEX_HOME": str(self.state_root / "installer"),
        }
        (self.state_root / "installer").mkdir(parents=True, mode=0o700, exist_ok=True)
        download_root = (
            self.workspace / "downloads" if self.workspace else self.state_root / "installer"
        )
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
                    "https://chatgpt.com/codex/install.sh",
                )
            )
        )
        self.runner.run(Command(("sh", str(installer)), env={**env, "CODEX_NON_INTERACTIVE": "1"}))

    def create_profiles(self) -> None:
        for number in ("01", "02"):
            profile = self.state_root / number
            if not self.runner.dry_run:
                profile.mkdir(parents=True, mode=0o700, exist_ok=True)
                profile.chmod(0o700)
                atomic_write(
                    profile / "config.toml", 'cli_auth_credentials_store = "file"\n', 0o600
                )
                launcher = (
                    f'#!/bin/sh\nexport CODEX_HOME="{profile}"\nexec "{self.shared_bin}" "$@"\n'
                )
                atomic_write(self.bin_dir / f"codex-{number}", launcher, 0o700)

    def authenticate(self, number: str) -> None:
        self.runner.run(Command((str(self.bin_dir / f"codex-{number}"), "login")))

    def verified(self, number: str) -> bool:
        profile = self.state_root / number
        launcher = self.bin_dir / f"codex-{number}"
        if not (profile.is_dir() and launcher.is_file()):
            return False
        result = self.runner.run(
            Command((str(launcher), "login", "status"), mutate=False), check=False
        )
        return result.returncode == 0

    def no_unscoped_launcher(self) -> bool:
        target = self.bin_dir / "codex"
        return not target.exists() or (
            shutil.which("codex") is not None
            and target.resolve() != Path(shutil.which("codex") or "")
        )
