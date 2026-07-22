#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import subprocess
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

VERSION_PATTERN = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
INSTALLER_VERSION_PATTERN = re.compile(r'^readonly OMFG_VERSION="([^"]+)"$', re.MULTILINE)
REQUIRED_FILES = {"LICENSE", "README.md", "pyproject.toml"}


def project_version(root: Path) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("project.version must be a semantic X.Y.Z version")
    source_match = VERSION_PATTERN.search(
        (root / "src/omfg/__init__.py").read_text(encoding="utf-8")
    )
    installer_match = INSTALLER_VERSION_PATTERN.search(
        (root / "bootstrap/install").read_text(encoding="utf-8")
    )
    versions = {
        "pyproject.toml": version,
        "src/omfg/__init__.py": source_match.group(1) if source_match else "missing",
        "bootstrap/install": installer_match.group(1) if installer_match else "missing",
    }
    if len(set(versions.values())) != 1:
        detail = ", ".join(f"{path}={value}" for path, value in versions.items())
        raise ValueError(f"version declarations disagree: {detail}")
    return version


def ensure_clean(root: Path) -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        raise ValueError("release source tree is dirty")


def commit_epoch(root: Path) -> int:
    result = subprocess.run(
        ["git", "show", "-s", "--format=%ct", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    epoch = int(result.stdout.strip())
    if epoch < 1:
        raise ValueError("invalid release commit timestamp")
    return epoch


def tracked_files(root: Path) -> tuple[Path, ...]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
    paths = [Path(raw.decode("utf-8")) for raw in result.stdout.split(b"\0") if raw]
    return tuple(sorted(paths, key=lambda item: item.as_posix()))


def is_runtime_file(path: Path) -> bool:
    value = path.as_posix()
    if value in REQUIRED_FILES:
        return True
    if value.startswith("src/omfg/"):
        return path.suffix == ".py" or path.name == "py.typed"
    parts = path.parts
    return len(parts) == 3 and parts[0] in {"apps", "deps"} and parts[2] == "manifest.toml"


def runtime_files(root: Path) -> tuple[Path, ...]:
    selected = tuple(path for path in tracked_files(root) if is_runtime_file(path))
    selected_names = {path.as_posix() for path in selected}
    missing = REQUIRED_FILES - selected_names
    if missing:
        raise ValueError(f"required runtime files are not tracked: {', '.join(sorted(missing))}")
    if not any(path.as_posix() == "src/omfg/cli.py" for path in selected):
        raise ValueError("src/omfg/cli.py is not tracked")
    if not any(path.parts[0] == "apps" for path in selected):
        raise ValueError("application manifests are missing")
    if not any(path.parts[0] == "deps" for path in selected):
        raise ValueError("dependency manifests are missing")
    for path in selected:
        source = root / path
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"runtime path must be a regular file: {path.as_posix()}")
    return selected


def archive_directories(files: tuple[Path, ...]) -> tuple[PurePosixPath, ...]:
    directories: set[PurePosixPath] = set()
    for path in files:
        parent = PurePosixPath(path.as_posix()).parent
        while str(parent) != ".":
            directories.add(parent)
            parent = parent.parent
    return tuple(sorted(directories, key=lambda item: item.as_posix()))


def tar_info(name: str, *, epoch: int, directory: bool, size: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.size = 0 if directory else size
    info.mode = 0o755 if directory else 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = epoch
    return info


def atomic_text(path: Path, content: str) -> None:
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build(root: Path, output: Path, tag: str, *, allow_dirty: bool = False) -> tuple[Path, str]:
    version = project_version(root)
    expected_tag = f"v{version}"
    if tag != expected_tag:
        raise ValueError(f"release tag must be {expected_tag}, got {tag}")
    if not allow_dirty:
        ensure_clean(root)
    epoch = commit_epoch(root)
    files = runtime_files(root)
    archive_root = f"omfg-{version}"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{archive_root}.tar.gz"
    tar_descriptor, tar_raw = tempfile.mkstemp(prefix=".omfg-release.", dir=output)
    os.close(tar_descriptor)
    tar_path = Path(tar_raw)
    gzip_descriptor, gzip_raw = tempfile.mkstemp(prefix=f".{archive.name}.", dir=output)
    os.close(gzip_descriptor)
    gzip_path = Path(gzip_raw)
    try:
        with tarfile.open(tar_path, "w", format=tarfile.USTAR_FORMAT) as bundle:
            bundle.addfile(tar_info(archive_root, epoch=epoch, directory=True))
            for directory in archive_directories(files):
                bundle.addfile(
                    tar_info(
                        f"{archive_root}/{directory.as_posix()}",
                        epoch=epoch,
                        directory=True,
                    )
                )
            for relative in files:
                data = (root / relative).read_bytes()
                info = tar_info(
                    f"{archive_root}/{relative.as_posix()}",
                    epoch=epoch,
                    directory=False,
                    size=len(data),
                )
                import io

                bundle.addfile(info, io.BytesIO(data))
        with tar_path.open("rb") as source, gzip_path.open("wb") as raw_output:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw_output, mtime=epoch
            ) as compressed:
                while chunk := source.read(1024 * 1024):
                    compressed.write(chunk)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        os.chmod(gzip_path, 0o644)
        os.replace(gzip_path, archive)
    finally:
        tar_path.unlink(missing_ok=True)
        gzip_path.unlink(missing_ok=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum_line = f"{digest}  {archive.name}\n"
    atomic_text(output / f"{archive.name}.sha256", checksum_line)
    atomic_text(output / "SHA256SUMS", checksum_line)
    return archive, digest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a reproducible omfg runtime release")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-dirty", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.source.resolve()
    version = project_version(root)
    if args.tag != f"v{version}":
        raise SystemExit(f"release tag must be v{version}, got {args.tag}")
    if args.check_only:
        print(f"release contract valid for v{version}")
        return 0
    output = (args.output or root / "dist").resolve()
    try:
        archive, digest = build(root, output, args.tag, allow_dirty=args.allow_dirty)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"release build failed: {exc}") from exc
    print(f"{digest}  {archive.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
