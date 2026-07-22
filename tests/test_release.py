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

from tools.build_installer import (
    DIGEST_TOKEN,
    VERSION_TOKEN,
    build_installer,
    render_installer,
    validate_installer,
    validate_template,
)
from tools.build_release import (
    build,
    ensure_clean,
    project_version,
    validate_release_contract,
    validate_release_notes,
)
from tools.build_site import build_site
from tools.validate_release import validate_archive


class ReleaseToolTests(unittest.TestCase):
    def test_version_declarations_agree(self) -> None:
        self.assertEqual(project_version(Path.cwd()), "0.1.3")
        self.assertEqual(validate_release_contract(Path.cwd(), "v0.1.3"), "0.1.3")

    def test_version_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "src/omfg").mkdir(parents=True)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "omfg"\nversion = "0.1.0"\n', encoding="utf-8"
            )
            (root / "src/omfg/__init__.py").write_text('__version__ = "0.1.3"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "version declarations disagree"):
                project_version(root)

    def test_build_is_reproducible_and_independently_validated(self) -> None:
        root = Path.cwd()
        with (
            tempfile.TemporaryDirectory() as first_raw,
            tempfile.TemporaryDirectory() as second_raw,
        ):
            first_dir = Path(first_raw)
            second_dir = Path(second_raw)
            first, first_digest = build(root, first_dir, "v0.1.3", allow_dirty=True)
            second, second_digest = build(root, second_dir, "v0.1.3", allow_dirty=True)
            self.assertEqual(first_digest, second_digest)
            for name in ("omfg-0.1.3.tar.gz", "omfg-0.1.3.tar.gz.sha256", "SHA256SUMS", "install"):
                self.assertEqual((first_dir / name).read_bytes(), (second_dir / name).read_bytes())
            validated = validate_archive(
                root,
                first,
                Path(f"{first}.sha256"),
                first.parent / "SHA256SUMS",
                first.parent / "install",
                run_runtime=False,
            )
            self.assertEqual(validated, first_digest)
            self.assertEqual((first_dir / "install").stat().st_mode & 0o777, 0o755)
            with tarfile.open(first, "r:gz") as bundle:
                names = {member.name for member in bundle.getmembers()}
            self.assertFalse(any(name.startswith("omfg-0.1.3/tests/") for name in names))
            self.assertFalse(any(name.startswith("omfg-0.1.3/.github/") for name in names))

    def test_sha256sums_covers_archive_and_installer_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            archive, archive_digest = build(Path.cwd(), output, "v0.1.3", allow_dirty=True)
            installer_digest = hashlib.sha256((output / "install").read_bytes()).hexdigest()
            self.assertEqual(
                (output / "SHA256SUMS").read_text(encoding="ascii"),
                f"{archive_digest}  {archive.name}\n{installer_digest}  install\n",
            )
            self.assertEqual(
                (output / f"{archive.name}.sha256").read_text(encoding="ascii"),
                f"{archive_digest}  {archive.name}\n",
            )

    def test_installer_rendering_is_deterministic_and_recomputes_archive_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "omfg-0.1.3.tar.gz"
            archive.write_bytes(b"actual archive bytes")
            template = root / "install.in"
            template.write_text(
                f'#!/bin/sh\nreadonly OMFG_VERSION="{VERSION_TOKEN}"\n'
                f'readonly EXPECTED_SHA256="{DIGEST_TOKEN}"\n',
                encoding="utf-8",
            )
            first = root / "first"
            second = root / "second"
            first_digest = build_installer(template, "0.1.3", archive, first)
            second_digest = build_installer(template, "0.1.3", archive, second)
            expected_archive = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(first_digest, second_digest)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertIn(f'EXPECTED_SHA256="{expected_archive}"', first.read_text())
            self.assertEqual(first.stat().st_mode & 0o777, 0o755)

    def test_installer_template_rejects_missing_duplicate_and_unresolved_placeholders(self) -> None:
        digest = "a" * 64
        with self.assertRaisesRegex(ValueError, "version placeholder"):
            validate_template(DIGEST_TOKEN)
        with self.assertRaisesRegex(ValueError, "checksum placeholder"):
            validate_template(VERSION_TOKEN + DIGEST_TOKEN + DIGEST_TOKEN)
        with self.assertRaisesRegex(ValueError, "unresolved placeholders"):
            validate_template(VERSION_TOKEN + DIGEST_TOKEN + "@UNKNOWN@")
        with self.assertRaisesRegex(ValueError, "unresolved placeholder"):
            validate_installer(
                f'readonly OMFG_VERSION="0.1.3"\nreadonly EXPECTED_SHA256="{digest}"\n@UNKNOWN@',
                "0.1.3",
                digest,
            )

    def test_installer_rejects_invalid_digest_and_duplicate_declarations(self) -> None:
        template = (
            f'readonly OMFG_VERSION="{VERSION_TOKEN}"\nreadonly EXPECTED_SHA256="{DIGEST_TOKEN}"\n'
        )
        with self.assertRaisesRegex(ValueError, "64 lowercase"):
            render_installer(template, "0.1.3", "A" * 64)
        rendered = render_installer(template, "0.1.3", "a" * 64)
        with self.assertRaisesRegex(ValueError, "one checksum declaration"):
            validate_installer(
                rendered + 'readonly EXPECTED_SHA256="' + "a" * 64 + '"\n', "0.1.3", "a" * 64
            )

    def test_installer_rejects_wrong_archive_filename(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "wrong.tar.gz"
            archive.write_bytes(b"archive")
            template = root / "install.in"
            template.write_text(VERSION_TOKEN + DIGEST_TOKEN, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be named"):
                build_installer(template, "0.1.3", archive, root / "install")

    def test_published_installer_has_literal_digest_and_no_checksum_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            archive, digest = build(Path.cwd(), output, "v0.1.3", allow_dirty=True)
            installer = (output / "install").read_text(encoding="utf-8")
            self.assertEqual(installer.count('readonly OMFG_VERSION="0.1.3"'), 1)
            self.assertEqual(installer.count(f'readonly EXPECTED_SHA256="{digest}"'), 1)
            self.assertNotIn("OMFG_RELEASE_SHA256", installer)
            self.assertNotIn(f"{archive.name}.sha256", installer)
            self.assertNotRegex(installer, r"curl[^\n]*\.sha256")

    def test_site_uses_release_installer_and_contains_only_distribution_surface(self) -> None:
        root = Path.cwd()
        with tempfile.TemporaryDirectory() as assets_raw, tempfile.TemporaryDirectory() as site_raw:
            assets = Path(assets_raw)
            build(root, assets, "v0.1.3", allow_dirty=True)
            site = Path(site_raw) / "site"
            build_site(root, assets, site, "v0.1.3", skip_runtime_validation=True)
            files = {
                path.relative_to(site).as_posix() for path in site.rglob("*") if path.is_file()
            }
            self.assertEqual(
                files,
                {
                    "index.html",
                    "install",
                    "releases/v0.1.3/install",
                    "releases/v0.1.3/SHA256SUMS",
                    "releases/v0.1.3/omfg-0.1.3.tar.gz",
                    "releases/v0.1.3/omfg-0.1.3.tar.gz.sha256",
                },
            )
            self.assertEqual((site / "install").read_bytes(), (assets / "install").read_bytes())
            self.assertNotEqual(
                (site / "install").read_bytes(), (root / "bootstrap/install.in").read_bytes()
            )

    def test_site_rejects_installer_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as assets_raw, tempfile.TemporaryDirectory() as site_raw:
            assets = Path(assets_raw)
            build(Path.cwd(), assets, "v0.1.3", allow_dirty=True)
            installer = assets / "install"
            installer.write_text(
                installer.read_text().replace(
                    'readonly EXPECTED_SHA256="', 'readonly EXPECTED_SHA256="0'
                ),
                encoding="utf-8",
            )
            installer.chmod(0o755)
            with self.assertRaisesRegex(
                (ValueError, subprocess.CalledProcessError), "digest|checksum|validation"
            ):
                build_site(
                    Path.cwd(),
                    assets,
                    Path(site_raw) / "site",
                    "v0.1.3",
                    skip_runtime_validation=True,
                )

    def test_site_rejects_missing_or_extra_assets(self) -> None:
        for mutation in ("missing", "extra"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as assets_raw,
                tempfile.TemporaryDirectory() as site_raw,
            ):
                assets = Path(assets_raw)
                build(Path.cwd(), assets, "v0.1.3", allow_dirty=True)
                if mutation == "missing":
                    (assets / "install").unlink()
                else:
                    (assets / "unexpected").write_text("extra", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "release assets differ"):
                    build_site(
                        Path.cwd(),
                        assets,
                        Path(site_raw) / "site",
                        "v0.1.3",
                        skip_runtime_validation=True,
                    )

    def test_release_notes_preflight_rejects_missing_empty_and_wrong_version(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaisesRegex(ValueError, "missing"):
                validate_release_notes(root, "0.1.3")
            notes = root / "docs/releases/v0.1.3.md"
            notes.parent.mkdir(parents=True)
            notes.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "empty"):
                validate_release_notes(root, "0.1.3")
            notes.write_text("# Omfg 0.1.1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "heading"):
                validate_release_notes(root, "0.1.3")

    def test_workflow_contract_uses_four_assets_and_main_dispatch(self) -> None:
        release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
        pages = Path(".github/workflows/pages.yml").read_text(encoding="utf-8")
        self.assertIn("dist/install", release)
        self.assertIn("-eq 4", release)
        self.assertIn("release-assets/install site/install", pages)
        self.assertIn("-eq 4", pages)
        self.assertNotIn("types: [published]", pages)
        self.assertIn("workflow_dispatch:", pages)

    def test_builder_rejects_dirty_actual_release(self) -> None:
        completed = type("Result", (), {"stdout": " M README.md\n"})()
        with patch("tools.build_release.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(ValueError, "dirty"):
                ensure_clean(Path.cwd())

    def test_validator_rejects_links(self) -> None:
        root = Path.cwd()
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            archive = directory / "omfg-0.1.3.tar.gz"
            payload = io.BytesIO()
            epoch = int(
                subprocess.run(
                    ["git", "show", "-s", "--format=%ct", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
            with tarfile.open(fileobj=payload, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
                link = tarfile.TarInfo("omfg-0.1.3/link")
                link.type = tarfile.SYMTYPE
                link.linkname = "/etc/passwd"
                link.mtime = epoch
                link.mode = 0o644
                bundle.addfile(link)
            with archive.open("wb") as output:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=epoch) as bundle:
                    bundle.write(payload.getvalue())
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            checksum = Path(f"{archive}.sha256")
            checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
            template = directory / "install.in"
            template.write_text(
                f'#!/bin/sh\nreadonly OMFG_VERSION="{VERSION_TOKEN}"\nreadonly EXPECTED_SHA256="{DIGEST_TOKEN}"\n',
                encoding="utf-8",
            )
            build_installer(template, "0.1.3", archive, directory / "install")
            installer_digest = hashlib.sha256((directory / "install").read_bytes()).hexdigest()
            sums = directory / "SHA256SUMS"
            sums.write_text(
                f"{digest}  {archive.name}\n{installer_digest}  install\n", encoding="ascii"
            )
            with self.assertRaisesRegex(ValueError, "links and special files"):
                validate_archive(
                    root,
                    archive,
                    checksum,
                    sums,
                    directory / "install",
                    run_runtime=False,
                )
