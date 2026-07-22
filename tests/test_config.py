from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omfg.config.codex import CodexManager
from omfg.config.git import GitConfigurator, GitIdentity
from omfg.config.ssh import SSHManager
from omfg.config.ssh_inventory import (
    LocalKey,
    RemoteKey,
    eligible_for_deletion,
    github_correlated_local_keys,
    inventory_local,
)
from omfg.ui import Terminal
from tests.helpers import FakeRunner


class ConfigTests(unittest.TestCase):
    def test_git_preserves_unrelated_configuration(self) -> None:
        runner = FakeRunner()
        GitConfigurator(runner).configure(GitIdentity("A", "a@example.com"))  # type: ignore[arg-type]
        self.assertTrue(
            all(
                command.argv[3] in {"user.name", "user.email", "init.defaultBranch"}
                for command in runner.commands
                if command.mutate
            )
        )

    def test_ssh_inventory_and_protected_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            ssh = Path(raw) / ".ssh"
            ssh.mkdir()
            private = ssh / "id_ed25519_old"
            public = ssh / "id_ed25519_old.pub"
            private.write_text("private", encoding="utf-8")
            public.write_text("ssh-ed25519 invalid", encoding="utf-8")
            keys = inventory_local(ssh)
            self.assertEqual(len(keys), 1)
            self.assertTrue(eligible_for_deletion(keys[0], ssh, ssh / "id_ed25519_omfg_github"))
            protected = LocalKey(ssh / "config", ssh / "known_hosts", None)
            self.assertFalse(eligible_for_deletion(protected, ssh, ssh / "new"))

    def test_ssh_delete_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manager = SSHManager(FakeRunner(), Path(raw))  # type: ignore[arg-type]
            with self.assertRaises(PermissionError):
                manager.delete((), explicit_confirmation=False)

    def test_ssh_host_config_preserves_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            (ssh / "config").write_text("Host example.com\n    User me\n", encoding="utf-8")
            manager = SSHManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager._configure_host()
            config = (ssh / "config").read_text(encoding="utf-8")
            self.assertIn("Host example.com", config)
            self.assertEqual(config.count("Include ~/.ssh/config.d/omfg-github.conf"), 1)
            self.assertIn("Host github.com", (ssh / "config.d/omfg-github.conf").read_text())

    def test_remote_deletion_requires_matching_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manager = SSHManager(FakeRunner(), Path(raw))  # type: ignore[arg-type]
            with self.assertRaises(PermissionError):
                manager.delete_remote(
                    (RemoteKey(1, "unrelated", "SHA256:no"),),
                    eligible_fingerprints=frozenset({"SHA256:yes"}),
                    explicit_confirmation=True,
                )

    def test_only_github_correlated_local_keys_are_cleanup_candidates(self) -> None:
        local = (
            LocalKey(Path("/tmp/github"), Path("/tmp/github.pub"), "SHA256:github"),
            LocalKey(Path("/tmp/server"), Path("/tmp/server.pub"), "SHA256:server"),
        )
        remote = (RemoteKey(1, "github", "SHA256:github"),)
        self.assertEqual(github_correlated_local_keys(local, remote), (local[0],))

    def test_dedicated_key_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            target = home / "unrelated"
            target.write_text("keep", encoding="utf-8")
            (ssh / "id_ed25519_omfg_github").symlink_to(target)
            manager = SSHManager(FakeRunner(), home)  # type: ignore[arg-type]
            with self.assertRaises(FileExistsError):
                manager.create("example@example.com")
            self.assertEqual(target.read_text(), "keep")

    def test_yes_never_approves_destructive_prompt(self) -> None:
        terminal = Terminal(input_fn=lambda _: "", output=lambda _: None)
        self.assertFalse(terminal.confirm("Delete?", assume_yes=True, destructive=True))

    def test_codex_launchers_have_separate_homes_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager.create_profiles()
            one = (home / ".local/bin/codex-01").read_text()
            two = (home / ".local/bin/codex-02").read_text()
            self.assertIn("/01", one)
            self.assertIn("/02", two)
            self.assertNotEqual(one, two)
            self.assertEqual((home / ".local/share/omfg/codex/01").stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (home / ".local/share/omfg/codex/01/config.toml").stat().st_mode & 0o777, 0o600
            )
            self.assertFalse((home / ".local/bin/codex").exists())
