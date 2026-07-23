from __future__ import annotations

import io
import os
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omfg.ui import Terminal
from omfg.verification.checks import Verifier
from tests.helpers import FakeRunner
from tools.build_installer import build_installer


class UiVerificationBootstrapTests(unittest.TestCase):
    def _require_arch_nonroot(self) -> None:
        release = Path("/etc/os-release").read_text(encoding="utf-8")
        if "ID=arch" not in release:
            self.skipTest("bootstrap integration requires Arch Linux")
        if os.geteuid() == 0:
            self.skipTest("bootstrap intentionally rejects root")

    def test_terminal_has_plain_sections_and_defaults(self) -> None:
        output: list[str] = []
        terminal = Terminal(input_fn=lambda _: "", output=output.append)
        terminal.section("Git configuration")
        terminal.output("Content.")
        terminal.section("Next")
        self.assertEqual(output, ["Git configuration", "Content.", "", "Next"])
        self.assertTrue(terminal.confirm("Keep?", default=True))
        self.assertFalse(terminal.confirm("Delete?", default=False))

    def test_package_failure_is_compact_and_actionable(self) -> None:
        output: list[str] = []
        Terminal(output=output.append).error(
            "AUR installation",
            "install packages",
            "old and new are in conflict",
            "/tmp/omfg-test/logs/aur.log",
            ("mullvad-browser-bin",),
        )
        self.assertEqual(
            output,
            [
                "AUR installation failed.",
                "Packages: mullvad-browser-bin.",
                "Reason: old and new are in conflict.",
                "Details: /tmp/omfg-test/logs/aur.log.",
                "Run omfg --verbose for complete command output.",
            ],
        )

    @patch("omfg.verification.checks.platform.machine", return_value="wrong")
    def test_verification_failure(self, _: object) -> None:
        result = Verifier(FakeRunner(), Path.home()).system()  # type: ignore[arg-type]
        self.assertFalse(result.passed)

    def test_path_verification_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch.dict("os.environ", {"PATH": "/usr/bin"}):
                self.assertFalse(Verifier(FakeRunner(), Path(raw)).path().passed)  # type: ignore[arg-type]

    def test_bootstrap_syntax(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(("bash", "-n", str(root / "bootstrap/install.in")), check=False)
        self.assertEqual(result.returncode, 0)

    def test_bootstrap_does_not_execute_omfg(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "bootstrap/install.in").read_text()
        self.assertIn("Run omfg to set up the workstation", text)
        self.assertNotIn("exec omfg", text)

    def _bootstrap_fixture(
        self, root: Path, *, unsafe_link: bool = False
    ) -> tuple[Path, Path, dict[str, str]]:
        archive = root / "release.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for directory in (
                "omfg-0.2.1",
                "omfg-0.2.1/apps",
                "omfg-0.2.1/deps",
                "omfg-0.2.1/src",
                "omfg-0.2.1/src/omfg",
            ):
                info = tarfile.TarInfo(directory)
                info.type = tarfile.DIRTYPE
                bundle.addfile(info)
            for name, content in (
                ("omfg-0.2.1/pyproject.toml", b"[project]\nname='omfg'\n"),
                ("omfg-0.2.1/src/omfg/__init__.py", b""),
                (
                    "omfg-0.2.1/src/omfg/__main__.py",
                    b"from omfg.cli import main\nraise SystemExit(main())\n",
                ),
                (
                    "omfg-0.2.1/src/omfg/cli.py",
                    b"def main():\n print('Omfg 0.2.1')\n return 0\n",
                ),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                bundle.addfile(info, io.BytesIO(content))
            if unsafe_link:
                link = tarfile.TarInfo("omfg-0.2.1/escape")
                link.type = tarfile.SYMTYPE
                link.linkname = "/tmp/escape"
                bundle.addfile(link)
        named_archive = root / "omfg-0.2.1.tar.gz"
        archive.rename(named_archive)
        archive = named_archive
        installer = root / "install"
        build_installer(
            Path(__file__).resolve().parents[1] / "bootstrap/install.in",
            "0.2.1",
            archive,
            installer,
        )
        fake_bin = root / "bin"
        fake_bin.mkdir()
        scripts = {
            "getent": '#!/bin/sh\nprintf \'%s:x:1000:1000:test:%s:%s\\n\' "$2" "$HOME" "$FAKE_LOGIN_SHELL"\n',
            "ps": "#!/bin/sh\ncase \" $* \" in *' comm='*) echo \"$FAKE_PROCESS_SHELL\";; *' args='*) echo \"$FAKE_PROCESS_SHELL\";; *' tty='*) echo pts/1;; *' ppid='*) echo 0;; esac\n",
            "pacman": "#!/bin/sh\nexit 0\n",
            "curl": '#!/bin/sh\nout=\'\'\nurl=\'\'\nprevious=\'\'\nfor arg in "$@"; do if [ "$previous" = -o ]; then out=$arg; fi; previous=$arg; url=$arg; done\nprintf \'%s\\n\' "$url" >>"$CURL_LOG"\ncase "$url" in *.sha256) exit 97;; *) cp "$FIXTURE_ARCHIVE" "$out";; esac\n',
        }
        for name, text in scripts.items():
            path = fake_bin / name
            path.write_text(text, encoding="utf-8")
            path.chmod(0o700)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(root / "home"),
                "USER": "bootstrap-user",
                "SHELL": "/bin/bash",
                "PATH": f"{fake_bin}:{env['PATH']}",
                "FIXTURE_ARCHIVE": str(archive),
                "FAKE_LOGIN_SHELL": "/usr/bin/fish",
                "FAKE_PROCESS_SHELL": "fish",
                "CURL_LOG": str(root / "curl.log"),
            }
        )
        Path(env["HOME"]).mkdir()
        return archive, installer, env

    def _assert_fixture_cli(self, env: dict[str, str]) -> None:
        launcher = Path(env["HOME"]) / ".local/bin/omfg"
        for argument in ("--version", "--help", "--dry-run"):
            result = subprocess.run(
                (str(launcher), argument), env=env, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Omfg 0.2.1", result.stdout)

    def test_bootstrap_fish_detection_atomic_install_and_idempotent_path(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root)
            first = subprocess.run(
                ("bash", str(installer)), env=env, text=True, capture_output=True
            )
            second = subprocess.run(
                ("bash", str(installer)), env=env, text=True, capture_output=True
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                first.stdout,
                "Installing omfg.\n"
                "Downloading the release... done.\n"
                "Verifying the release... done.\n"
                "Installing the command... done.\n"
                "Configuring the fish PATH... done.\n\n"
                "The omfg command is installed.\n"
                "Run omfg to set up the workstation.\n",
            )
            self.assertEqual(
                second.stdout,
                "Installing omfg.\n"
                "Downloading the release... done.\n"
                "Verifying the release... done.\n"
                "The release is already installed.\n"
                "The command is already available.\n"
                "The fish PATH is already configured.\n\n"
                "The omfg command is ready.\n"
                "Run omfg to set up the workstation.\n",
            )
            self.assertNotIn("% Total", first.stdout + first.stderr)
            home = Path(env["HOME"])
            self.assertTrue((home / ".local/bin/omfg").is_file())
            self.assertTrue((home / ".local/share/omfg/current").is_symlink())
            self.assertTrue((home / ".local/share/omfg/current/src/omfg/cli.py").is_file())
            fish = home / ".config/fish/conf.d/omfg.fish"
            self.assertEqual(fish.read_text().count("fish_add_path"), 1)
            self.assertFalse((home / ".bashrc").exists())
            self.assertNotIn(".sha256", (root / "curl.log").read_text())
            self.assertNotRegex(installer.read_text(encoding="utf-8"), r"(?m)^\s*sudo\s")
            self._assert_fixture_cli(env)

    def test_bootstrap_bash_and_zsh_are_shell_specific(self) -> None:
        self._require_arch_nonroot()
        for shell in ("bash", "zsh"):
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _, installer, env = self._bootstrap_fixture(root)
                env["FAKE_LOGIN_SHELL"] = f"/usr/bin/{shell}"
                env["FAKE_PROCESS_SHELL"] = shell
                result = subprocess.run(
                    ("bash", str(installer)), env=env, text=True, capture_output=True
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                repeated = subprocess.run(
                    ("bash", str(installer)), env=env, text=True, capture_output=True
                )
                self.assertEqual(repeated.returncode, 0, repeated.stderr)
                home = Path(env["HOME"])
                selected = home / f".{shell}rc"
                self.assertEqual(selected.read_text().count("Added by omfg"), 1)
                other = home / (".zshrc" if shell == "bash" else ".bashrc")
                self.assertFalse(other.exists())
                self.assertFalse((home / ".config/fish/conf.d/omfg.fish").exists())
                self._assert_fixture_cli(env)

    def test_bootstrap_rejects_archive_links(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root, unsafe_link=True)
            result = subprocess.run(
                ("bash", str(installer)), env=env, text=True, capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsafe or invalid", result.stderr)
            self.assertFalse((Path(env["HOME"]) / ".local/share/omfg/current").exists())

    def test_bootstrap_download_failure_transcript(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root)
            curl = root / "bin/curl"
            curl.write_text("#!/bin/sh\nexit 22\n", encoding="utf-8")
            curl.chmod(0o700)
            result = subprocess.run(
                ("bash", str(installer)), env=env, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                result.stdout,
                "Installing omfg.\nDownloading the release... failed.\n",
            )
            self.assertEqual(result.stderr, "omfg installer: release download failed\n")

    def test_bootstrap_rejects_tampered_archive(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive, installer, env = self._bootstrap_fixture(root)
            with archive.open("ab") as handle:
                handle.write(b"tampered")
            result = subprocess.run(
                ("bash", str(installer)), env=env, text=True, capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("release checksum mismatch", result.stderr)
            self.assertFalse((Path(env["HOME"]) / ".local/share/omfg/current").exists())

    def test_bootstrap_rejects_tampered_embedded_hash(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root)
            content = installer.read_text(encoding="utf-8")
            content = re.sub(
                r'(?m)^readonly EXPECTED_SHA256="[0-9a-f]{64}"$',
                'readonly EXPECTED_SHA256="' + "0" * 64 + '"',
                content,
            )
            installer.write_text(content, encoding="utf-8")
            result = subprocess.run(
                ("bash", str(installer)), env=env, text=True, capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("release checksum mismatch", result.stderr)
