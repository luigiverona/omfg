from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omfg.ui import Terminal
from omfg.verification.checks import Verifier
from tests.helpers import FakeRunner


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
        self.assertEqual(output, ["", "Git configuration", ""])
        self.assertTrue(terminal.confirm("Keep?", default=True))
        self.assertFalse(terminal.confirm("Delete?", default=False))

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
        result = subprocess.run(("bash", "-n", str(root / "bootstrap/install")), check=False)
        self.assertEqual(result.returncode, 0)

    def test_bootstrap_does_not_execute_omfg(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "bootstrap/install").read_text()
        self.assertIn("Run omfg when you are ready", text)
        self.assertNotIn("exec omfg", text)

    def _bootstrap_fixture(
        self, root: Path, *, unsafe_link: bool = False
    ) -> tuple[Path, dict[str, str]]:
        archive = root / "release.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for directory in (
                "omfg-0.1.0",
                "omfg-0.1.0/apps",
                "omfg-0.1.0/deps",
                "omfg-0.1.0/src",
                "omfg-0.1.0/src/omfg",
            ):
                info = tarfile.TarInfo(directory)
                info.type = tarfile.DIRTYPE
                bundle.addfile(info)
            for name, content in (
                ("omfg-0.1.0/pyproject.toml", b"[project]\nname='omfg'\n"),
                ("omfg-0.1.0/src/omfg/__init__.py", b""),
                (
                    "omfg-0.1.0/src/omfg/__main__.py",
                    b"from omfg.cli import main\nraise SystemExit(main())\n",
                ),
                (
                    "omfg-0.1.0/src/omfg/cli.py",
                    b"def main():\n print('Omfg 0.1.0')\n return 0\n",
                ),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                bundle.addfile(info, io.BytesIO(content))
            if unsafe_link:
                link = tarfile.TarInfo("omfg-0.1.0/escape")
                link.type = tarfile.SYMTYPE
                link.linkname = "/tmp/escape"
                bundle.addfile(link)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum = root / "release.tar.gz.sha256"
        checksum.write_text(f"{digest}  release.tar.gz\n", encoding="utf-8")
        fake_bin = root / "bin"
        fake_bin.mkdir()
        scripts = {
            "getent": '#!/bin/sh\nprintf \'%s:x:1000:1000:test:%s:/usr/bin/fish\\n\' "$2" "$HOME"\n',
            "ps": "#!/bin/sh\ncase \" $* \" in *' comm='*) echo fish;; *' args='*) echo fish;; *' tty='*) echo pts/1;; *' ppid='*) echo 0;; esac\n",
            "pacman": "#!/bin/sh\nexit 0\n",
            "curl": '#!/bin/sh\nout=\'\'\nfor arg in "$@"; do if [ "$previous" = -o ]; then out=$arg; fi; previous=$arg; done\ncase "$out" in *.sha256) cp "$FIXTURE_CHECKSUM" "$out";; *) cp "$FIXTURE_ARCHIVE" "$out";; esac\n',
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
                "FIXTURE_CHECKSUM": str(checksum),
            }
        )
        Path(env["HOME"]).mkdir()
        return archive, env

    def test_bootstrap_fish_detection_atomic_install_and_idempotent_path(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, env = self._bootstrap_fixture(root)
            installer = Path(__file__).resolve().parents[1] / "bootstrap/install"
            first = subprocess.run(
                ("bash", str(installer)), env=env, text=True, capture_output=True
            )
            second = subprocess.run(
                ("bash", str(installer)), env=env, text=True, capture_output=True
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            home = Path(env["HOME"])
            self.assertTrue((home / ".local/bin/omfg").is_file())
            self.assertTrue((home / ".local/share/omfg/current").is_symlink())
            self.assertTrue((home / ".local/share/omfg/current/src/omfg/cli.py").is_file())
            fish = home / ".config/fish/conf.d/omfg.fish"
            self.assertEqual(fish.read_text().count("fish_add_path"), 1)
            self.assertFalse((home / ".bashrc").exists())

    def test_bootstrap_rejects_archive_links(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, env = self._bootstrap_fixture(root, unsafe_link=True)
            installer = Path(__file__).resolve().parents[1] / "bootstrap/install"
            result = subprocess.run(
                ("bash", str(installer)), env=env, text=True, capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsafe or invalid", result.stderr)
            self.assertFalse((Path(env["HOME"]) / ".local/share/omfg/current").exists())
