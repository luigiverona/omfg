#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tomllib
from pathlib import Path


def build_site(
    root: Path, assets: Path, output: Path, tag: str, *, skip_runtime_validation: bool = False
) -> None:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    if tag != f"v{version}":
        raise ValueError(f"site tag must be v{version}")
    archive_name = f"omfg-{version}.tar.gz"
    expected_assets = {archive_name, f"{archive_name}.sha256", "SHA256SUMS"}
    observed_assets = {path.name for path in assets.iterdir() if path.is_file()}
    if observed_assets != expected_assets:
        raise ValueError(
            f"release assets differ; expected={sorted(expected_assets)}, got={sorted(observed_assets)}"
        )
    validation_command = [
        "python",
        str(root / "tools/validate_release.py"),
        str(assets / archive_name),
        "--project-root",
        str(root),
        "--checksum",
        str(assets / f"{archive_name}.sha256"),
        "--sums",
        str(assets / "SHA256SUMS"),
    ]
    if skip_runtime_validation:
        validation_command.append("--skip-runtime")
    subprocess.run(
        validation_command,
        cwd=root,
        check=True,
    )
    installer = (root / "bootstrap/install").read_bytes()
    text = installer.decode("utf-8")
    if f'readonly OMFG_VERSION="{version}"' not in text:
        raise ValueError("installer version differs from site version")
    if not re.search(
        r'^readonly RELEASE_BASE="\$\{OMFG_RELEASE_BASE:-https://omfg\.luigiverona\.dev/releases\}"$',
        text,
        re.MULTILINE,
    ):
        raise ValueError("installer release base is not the intended custom domain")
    if output.exists():
        shutil.rmtree(output)
    release_dir = output / "releases" / tag
    release_dir.mkdir(parents=True)
    (output / "install").write_bytes(installer)
    for name in sorted(expected_assets):
        shutil.copyfile(assets / name, release_dir / name)
    (output / "index.html").write_text(
        "<!doctype html>\n"
        '<html lang="en"><meta charset="utf-8">\n'
        "<title>omfg</title>\n"
        "<h1>omfg</h1>\n"
        "<p>Arch Linux workstation setup. "
        '<a href="https://github.com/luigiverona/omfg">Source on GitHub</a>.</p>\n',
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the omfg GitHub Pages distribution tree")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--skip-runtime-validation", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        build_site(
            args.project_root.resolve(),
            args.assets.resolve(),
            args.output.resolve(),
            args.tag,
            skip_runtime_validation=args.skip_runtime_validation,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"site build failed: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
