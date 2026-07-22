from __future__ import annotations

import os
from pathlib import Path

from omfg.config.files import atomic_write
from omfg.config.ssh_inventory import (
    LocalKey,
    RemoteKey,
    eligible_for_deletion,
    fingerprint_text,
    inventory_local,
)
from omfg.execution import Command, CommandRunner


class SSHManager:
    def __init__(self, runner: CommandRunner, home: Path) -> None:
        self.runner = runner
        self.ssh_dir = home / ".ssh"
        self.key = self.ssh_dir / "id_ed25519_omfg_github"

    def inventory(self) -> tuple[LocalKey, ...]:
        return inventory_local(self.ssh_dir)

    def inventory_remote(self) -> tuple[RemoteKey, ...]:
        result = self.runner.run(
            Command(
                (
                    "gh",
                    "api",
                    "user/keys",
                    "--paginate",
                    "--jq",
                    '.[] | "\\(.id)\\t\\(.title)\\t\\(.key)"',
                ),
                mutate=False,
            )
        )
        keys: list[RemoteKey] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[0].isdigit():
                keys.append(RemoteKey(int(parts[0]), parts[1], fingerprint_text(parts[2])))
        return tuple(sorted(keys, key=lambda key: key.key_id))

    def create(self, email: str) -> bool:
        if self.ssh_dir.is_symlink():
            raise FileExistsError("~/.ssh must not be a symbolic link")
        self.ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.ssh_dir.chmod(0o700)
        created = False
        if self.key.is_symlink() or self.key.with_suffix(".pub").is_symlink():
            raise FileExistsError("dedicated SSH key paths must not be symbolic links")
        if self.key.exists() != self.key.with_suffix(".pub").exists():
            raise FileExistsError("dedicated SSH key pair is incomplete; refusing to overwrite it")
        if not self.key.exists():
            self.runner.run(
                Command(("ssh-keygen", "-t", "ed25519", "-f", str(self.key), "-C", email, "-N", ""))
            )
            created = True
        if not self.runner.dry_run:
            self.key.chmod(0o600)
            self.key.with_suffix(".pub").chmod(0o644)
        self._configure_host()
        return created

    def _configure_host(self) -> None:
        config = self.ssh_dir / "config"
        include = "Include ~/.ssh/config.d/omfg-github.conf"
        existing = config.read_text(encoding="utf-8") if config.exists() else ""
        content = existing if include in existing.splitlines() else include + "\n" + existing
        owned = self.ssh_dir / "config.d/omfg-github.conf"
        if owned.parent.is_symlink():
            raise FileExistsError("~/.ssh/config.d must not be a symbolic link")
        block = (
            "Host github.com\n"
            "    HostName github.com\n"
            "    User git\n"
            f"    IdentityFile {self.key}\n"
            "    IdentitiesOnly yes\n"
        )
        if not self.runner.dry_run:
            atomic_write(config, content, 0o600)
            atomic_write(owned, block, 0o600)

    def upload(self, title: str) -> None:
        self.runner.run(
            Command(("gh", "ssh-key", "add", str(self.key.with_suffix(".pub")), "--title", title))
        )

    def verify(self, *, read_only: bool = False) -> bool:
        argv: tuple[str, ...] = ("ssh", "-T", "-o", "BatchMode=yes", "git@github.com")
        if read_only:
            argv = (
                "ssh",
                "-T",
                "-F",
                "/dev/null",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UpdateHostKeys=no",
                "-o",
                "ControlMaster=no",
                "-o",
                "ControlPersist=no",
                "-o",
                "ControlPath=none",
                "-o",
                "PermitLocalCommand=no",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                f"UserKnownHostsFile={self.ssh_dir / 'known_hosts'}",
                "-i",
                str(self.key),
                "git@github.com",
            )
        result = self.runner.run(
            Command(argv, mutate=False),
            check=False,
        )
        return result.returncode == 1 and "successfully authenticated" in (
            result.stdout + result.stderr
        )

    def delete(self, keys: tuple[LocalKey, ...], *, explicit_confirmation: bool) -> None:
        if not explicit_confirmation:
            raise PermissionError("SSH key deletion requires explicit confirmation")
        for key in keys:
            if not eligible_for_deletion(key, self.ssh_dir, self.key):
                raise PermissionError(f"ineligible SSH key: {key.private}")
        for key in keys:
            os.unlink(key.public)
            os.unlink(key.private)

    def delete_remote(
        self,
        keys: tuple[RemoteKey, ...],
        *,
        eligible_fingerprints: frozenset[str],
        explicit_confirmation: bool,
    ) -> None:
        if not explicit_confirmation:
            raise PermissionError("GitHub key deletion requires explicit confirmation")
        for key in keys:
            if not key.fingerprint or key.fingerprint not in eligible_fingerprints:
                raise PermissionError(f"ineligible GitHub key: {key.title}")
        for key in keys:
            self.runner.run(Command(("gh", "api", "--method", "DELETE", f"user/keys/{key.key_id}")))
