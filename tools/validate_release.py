#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import re
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

CHECKSUM_PATTERN = re.compile(r"^([0-9a-f]{64})  (omfg-[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz)\n$")
VERSION_PATTERN = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.MULTILINE)


def source_version(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = data["project"]["version"]
    if not isinstance(version, str):
        raise ValueError("project version is not a string")
    return version


def expected_files(root: Path) -> set[str]:
    files = {"LICENSE", "README.md", "pyproject.toml"}
    source = root / "src/omfg"
    files.update(
        path.relative_to(root).as_posix()
        for path in source.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and (path.suffix == ".py" or path.name == "py.typed")
    )
    for kind in ("apps", "deps"):
        files.update(
            path.relative_to(root).as_posix()
            for path in (root / kind).glob("*/manifest.toml")
            if path.is_file() and not path.is_symlink()
        )
    return files


def expected_directories(files: set[str]) -> set[str]:
    result: set[str] = set()
    for name in files:
        parent = PurePosixPath(name).parent
        while str(parent) != ".":
            result.add(parent.as_posix())
            parent = parent.parent
    return result


def commit_epoch(root: Path) -> int:
    result = subprocess.run(
        ["git", "show", "-s", "--format=%ct", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip())


def validate_checksum(path: Path, archive: Path, digest: str) -> None:
    content = path.read_text(encoding="ascii")
    match = CHECKSUM_PATTERN.fullmatch(content)
    if not match:
        raise ValueError(f"invalid checksum syntax: {path.name}")
    if match.group(1) != digest or match.group(2) != archive.name:
        raise ValueError(f"checksum does not match archive: {path.name}")


def validate_gzip_header(archive: Path, epoch: int) -> None:
    with archive.open("rb") as handle:
        header = handle.read(10)
    if len(header) != 10 or header[:3] != b"\x1f\x8b\x08":
        raise ValueError("archive is not gzip data")
    if header[3] != 0:
        raise ValueError("gzip header contains optional metadata")
    if struct.unpack("<I", header[4:8])[0] != epoch:
        raise ValueError("gzip timestamp is not the release epoch")
    if header[8:] != b"\x02\xff":
        raise ValueError("gzip compression or operating-system metadata is not normalized")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_archive(
    root: Path,
    archive: Path,
    checksum: Path,
    sums: Path,
    *,
    run_runtime: bool = True,
) -> str:
    version = source_version(root)
    archive_root = f"omfg-{version}"
    expected_name = f"{archive_root}.tar.gz"
    if archive.name != expected_name:
        raise ValueError(f"archive must be named {expected_name}")
    digest = file_digest(archive)
    validate_checksum(checksum, archive, digest)
    validate_checksum(sums, archive, digest)
    epoch = commit_epoch(root)
    validate_gzip_header(archive, epoch)
    files = expected_files(root)
    directories = expected_directories(files)
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    seen: set[str] = set()
    extracted: dict[str, bytes] = {}
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            raw_name = member.name
            path = PurePosixPath(raw_name)
            if not path.parts or raw_name.startswith("/") or ".." in path.parts:
                raise ValueError(f"unsafe archive path: {raw_name}")
            canonical_name = path.as_posix()
            if raw_name.rstrip("/") != canonical_name:
                raise ValueError(f"noncanonical archive path: {raw_name}")
            if canonical_name in seen:
                raise ValueError(f"duplicate archive member: {raw_name}")
            seen.add(canonical_name)
            if path.parts[0] != archive_root:
                raise ValueError("archive must contain exactly one expected top-level directory")
            if member.uid != 0 or member.gid != 0 or member.uname or member.gname:
                raise ValueError(f"owner metadata is not normalized: {raw_name}")
            if member.mtime != epoch:
                raise ValueError(f"timestamp is not normalized: {raw_name}")
            relative = PurePosixPath(*path.parts[1:]).as_posix() if len(path.parts) > 1 else ""
            if member.isdir():
                if member.mode != 0o755:
                    raise ValueError(f"directory mode is not 0755: {raw_name}")
                if relative:
                    observed_directories.add(relative)
            elif member.isfile():
                if member.mode != 0o644:
                    raise ValueError(f"file mode is not 0644: {raw_name}")
                if not relative:
                    raise ValueError("archive root must be a directory")
                handle = bundle.extractfile(member)
                if handle is None:
                    raise ValueError(f"cannot read archive member: {raw_name}")
                observed_files.add(relative)
                extracted[relative] = handle.read()
            else:
                raise ValueError(f"links and special files are forbidden: {raw_name}")
    if observed_files != files:
        missing = sorted(files - observed_files)
        extra = sorted(observed_files - files)
        raise ValueError(f"runtime file set differs; missing={missing}, unexpected={extra}")
    if observed_directories != directories:
        raise ValueError("archive directory set differs from the required runtime layout")
    archived_project = tomllib.loads(extracted["pyproject.toml"].decode("utf-8"))
    archived_source = VERSION_PATTERN.search(extracted["src/omfg/__init__.py"].decode("utf-8"))
    if archived_project["project"]["version"] != version:
        raise ValueError("archived project version differs")
    if not archived_source or archived_source.group(1) != version:
        raise ValueError("archived package version differs")
    if run_runtime:
        exercise_runtime(extracted, directories, archive_root, version)
    return digest


def exercise_runtime(
    extracted: dict[str, bytes], directories: set[str], archive_root: str, version: str
) -> None:
    with tempfile.TemporaryDirectory(prefix="omfg-release-validation-") as raw:
        temporary = Path(raw)
        extracted_root = temporary / archive_root
        extracted_root.mkdir(mode=0o755)
        for directory in sorted(directories):
            (extracted_root / directory).mkdir(parents=True, exist_ok=True, mode=0o755)
        for name, data in sorted(extracted.items()):
            target = extracted_root / name
            target.write_bytes(data)
            target.chmod(0o644)
        home = temporary / "home"
        home.mkdir(mode=0o700)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "PYTHONPATH": str(extracted_root / "src"),
                "OMFG_CATALOG_ROOT": str(extracted_root),
            }
        )
        commands = (
            (("--version",), f"Omfg {version}"),
            (("--help",), "usage: omfg"),
            (("--dry-run",), "Dry run: no changes will be made."),
        )
        for arguments, expected in commands:
            result = subprocess.run(
                [sys.executable, "-m", "omfg", *arguments],
                cwd=extracted_root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            combined = result.stdout + result.stderr
            if result.returncode or expected not in combined:
                raise ValueError(f"isolated CLI {' '.join(arguments)} failed: {combined.strip()}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Independently validate an omfg runtime release")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--checksum", type=Path)
    parser.add_argument("--sums", type=Path)
    parser.add_argument("--skip-runtime", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    archive = args.archive.resolve()
    checksum = (args.checksum or Path(f"{archive}.sha256")).resolve()
    sums = (args.sums or archive.parent / "SHA256SUMS").resolve()
    try:
        digest = validate_archive(
            args.project_root.resolve(),
            archive,
            checksum,
            sums,
            run_runtime=not args.skip_runtime,
        )
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        raise SystemExit(f"release validation failed: {exc}") from exc
    print(f"release archive verified: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
