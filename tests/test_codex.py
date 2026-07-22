from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from omfg.config.codex import CodexManager
from omfg.errors import ValidationError
from omfg.execution import Command, CommandResult
from tests.helpers import FakeRunner


class InstallingRunner(FakeRunner):
    INSTALLER = b"audited installer"

    def __init__(self, shared: Path) -> None:
        super().__init__()
        self.shared = shared

    def run(self, command: Command, *, check: bool = True) -> CommandResult:
        result = super().run(command, check=check)
        if command.argv[0] == "curl":
            output = Path(command.argv[command.argv.index("-o") + 1])
            output.write_bytes(self.INSTALLER)
        if command.argv[0] == "sh":
            self.shared.parent.mkdir(parents=True, exist_ok=True)
            self.shared.write_text("binary", encoding="utf-8")
            self.shared.chmod(0o700)
        return result


class CodexTests(unittest.TestCase):
    def test_two_launchers_share_executable_forward_arguments_and_isolate_home(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("binary", encoding="utf-8")
            manager.create_profiles()
            one = (manager.bin_dir / "codex-01").read_text(encoding="utf-8")
            two = (manager.bin_dir / "codex-02").read_text(encoding="utf-8")
            self.assertIn(f'exec "{manager.shared_bin}" "$@"', one)
            self.assertIn(f'exec "{manager.shared_bin}" "$@"', two)
            self.assertIn(str(manager.state_root / "01"), one)
            self.assertIn(str(manager.state_root / "02"), two)
            self.assertTrue(manager.profiles_distinct())
            self.assertTrue(manager.no_unscoped_launcher())

    def test_profile_permissions_and_existing_configuration_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            profile = manager.state_root / "01"
            profile.mkdir(parents=True)
            (profile / "config.toml").write_text('model = "example"\n', encoding="utf-8")
            manager.create_profiles()
            self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
            self.assertEqual((profile / "config.toml").stat().st_mode & 0o777, 0o600)
            self.assertIn('model = "example"', (profile / "config.toml").read_text())
            self.assertEqual((manager.bin_dir / "codex-01").stat().st_mode & 0o777, 0o700)

    def test_owned_unscoped_launcher_removed_but_unrelated_file_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("binary", encoding="utf-8")
            manager.bin_dir.mkdir(parents=True)
            public = manager.bin_dir / "codex"
            public.symlink_to(manager.shared_bin)
            manager.remove_owned_unscoped_launcher()
            self.assertFalse(public.exists())
            public.write_text("unrelated", encoding="utf-8")
            manager.remove_owned_unscoped_launcher()
            self.assertTrue(public.exists())
            self.assertFalse(manager.no_unscoped_launcher())

    def test_official_installer_is_constrained_to_private_state_and_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            workspace = home / "workspace"
            (workspace / "downloads").mkdir(parents=True)
            manager = CodexManager(FakeRunner(), home, workspace)  # type: ignore[arg-type]
            runner = InstallingRunner(manager.shared_bin)
            manager.runner = runner  # type: ignore[assignment]
            manager.INSTALLER_SHA256 = hashlib.sha256(runner.INSTALLER).hexdigest()
            manager.install()
            installer_command = next(
                command for command in runner.commands if command.argv[0] == "sh"
            )
            self.assertEqual(
                installer_command.env["CODEX_HOME"], str(manager.state_root / "installer")
            )
            self.assertEqual(
                installer_command.env["CODEX_INSTALL_DIR"], str(manager.shared_bin.parent)
            )
            self.assertEqual(installer_command.env["CODEX_RELEASE"], "latest")
            self.assertTrue(
                installer_command.env["PATH"].startswith(str(manager.shared_bin.parent))
            )
            self.assertFalse((home / ".codex").exists())
            self.assertFalse((home / ".local/bin/codex").exists())

    def test_failed_artifact_verification_aborts_install(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            workspace = home / "workspace"
            (workspace / "downloads").mkdir(parents=True)
            runner = InstallingRunner(home / "never-created")
            manager = CodexManager(runner, home, workspace)  # type: ignore[arg-type]
            with self.assertRaisesRegex(ValidationError, "installer checksum mismatch"):
                manager.install()
            self.assertFalse(manager.shared_bin.exists())

    def test_profile_specific_login_and_status_commands(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            runner = FakeRunner()
            manager = CodexManager(runner, home)  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("binary", encoding="utf-8")
            manager.create_profiles()
            manager.authenticate("01")
            manager.authenticate("02")
            self.assertTrue(manager.verified("01"))
            self.assertTrue(manager.verified("02"))
            argv = [command.argv for command in runner.commands]
            self.assertIn((str(manager.bin_dir / "codex-01"), "login"), argv)
            self.assertIn((str(manager.bin_dir / "codex-02"), "login"), argv)
            self.assertIn((str(manager.bin_dir / "codex-01"), "login", "status"), argv)
            self.assertIn((str(manager.bin_dir / "codex-02"), "login", "status"), argv)

    def test_insecure_credential_permissions_fail_verification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("binary", encoding="utf-8")
            manager.create_profiles()
            auth = manager.state_root / "01/auth.json"
            auth.write_text("not-a-real-credential", encoding="utf-8")
            auth.chmod(0o644)
            self.assertFalse(manager.verified("01"))

    def test_symbolic_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager.state_root.mkdir(parents=True)
            unrelated = home / "unrelated"
            unrelated.mkdir()
            (manager.state_root / "01").symlink_to(unrelated)
            with self.assertRaises(OSError):
                manager.create_profiles()

    def test_launcher_updates_leave_no_partial_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager.create_profiles()
            manager.create_profiles()
            leftovers = list(manager.bin_dir.glob(".codex-*.new*"))
            self.assertEqual(leftovers, [])
