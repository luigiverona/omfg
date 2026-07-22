from __future__ import annotations

import gzip
import hashlib
import io
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.build_release import build, ensure_clean, project_version
from tools.build_site import build_site
from tools.validate_release import validate_archive


class ReleaseToolTests(unittest.TestCase):
    def test_version_declarations_agree(self) -> None:
        self.assertEqual(project_version(Path.cwd()), "0.1.1")

    def test_version_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "src/omfg").mkdir(parents=True)
            (root / "bootstrap").mkdir()
            (root / "pyproject.toml").write_text(
                '[project]\nname = "omfg"\nversion = "0.1.0"\n', encoding="utf-8"
            )
            (root / "src/omfg/__init__.py").write_text('__version__ = "0.1.1"\n', encoding="utf-8")
            (root / "bootstrap/install").write_text(
                'readonly OMFG_VERSION="0.1.0"\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "version declarations disagree"):
                project_version(root)

    def test_build_is_reproducible_and_independently_validated(self) -> None:
        root = Path.cwd()
        with (
            tempfile.TemporaryDirectory() as first_raw,
            tempfile.TemporaryDirectory() as second_raw,
        ):
            first, first_digest = build(root, Path(first_raw), "v0.1.1", allow_dirty=True)
            second, second_digest = build(root, Path(second_raw), "v0.1.1", allow_dirty=True)
            self.assertEqual(first_digest, second_digest)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            validated = validate_archive(
                root,
                first,
                Path(f"{first}.sha256"),
                first.parent / "SHA256SUMS",
                run_runtime=False,
            )
            self.assertEqual(validated, first_digest)
            with tarfile.open(first, "r:gz") as bundle:
                names = {member.name for member in bundle.getmembers()}
            self.assertFalse(any(name.startswith("omfg-0.1.1/tests/") for name in names))
            self.assertFalse(any(name.startswith("omfg-0.1.1/.github/") for name in names))

    def test_site_contains_only_distribution_surface(self) -> None:
        root = Path.cwd()
        with tempfile.TemporaryDirectory() as assets_raw, tempfile.TemporaryDirectory() as site_raw:
            assets = Path(assets_raw)
            build(root, assets, "v0.1.1", allow_dirty=True)
            site = Path(site_raw) / "site"
            build_site(root, assets, site, "v0.1.1", skip_runtime_validation=True)
            files = {
                path.relative_to(site).as_posix() for path in site.rglob("*") if path.is_file()
            }
            self.assertEqual(
                files,
                {
                    "index.html",
                    "install",
                    "releases/v0.1.1/SHA256SUMS",
                    "releases/v0.1.1/omfg-0.1.1.tar.gz",
                    "releases/v0.1.1/omfg-0.1.1.tar.gz.sha256",
                },
            )
            self.assertEqual(
                (site / "install").read_bytes(), (root / "bootstrap/install").read_bytes()
            )

    def test_builder_rejects_dirty_actual_release(self) -> None:
        completed = type("Result", (), {"stdout": " M README.md\n"})()
        with patch("tools.build_release.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(ValueError, "dirty"):
                ensure_clean(Path.cwd())

    def test_validator_rejects_links(self) -> None:
        root = Path.cwd()
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            archive = directory / "omfg-0.1.1.tar.gz"
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
                link = tarfile.TarInfo("omfg-0.1.1/link")
                link.type = tarfile.SYMTYPE
                link.linkname = "/etc/passwd"
                epoch = int(
                    subprocess.run(
                        ["git", "show", "-s", "--format=%ct", "HEAD"],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip()
                )
                link.mtime = epoch
                link.mode = 0o644
                bundle.addfile(link)
            with archive.open("wb") as output:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=epoch) as bundle:
                    bundle.write(payload.getvalue())
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            line = f"{digest}  {archive.name}\n"
            checksum = Path(f"{archive}.sha256")
            sums = directory / "SHA256SUMS"
            checksum.write_text(line, encoding="ascii")
            sums.write_text(line, encoding="ascii")
            with self.assertRaisesRegex(ValueError, "links and special files"):
                validate_archive(root, archive, checksum, sums, run_runtime=False)
