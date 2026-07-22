from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LocalKey:
    private: Path
    public: Path
    fingerprint: str | None


@dataclass(frozen=True, slots=True)
class RemoteKey:
    key_id: int
    title: str
    fingerprint: str | None


PROTECTED = {"config", "known_hosts", "authorized_keys"}


def fingerprint(public_key: Path) -> str | None:
    try:
        parts = public_key.read_text(encoding="utf-8").strip().split()
        digest = hashlib.sha256(base64.b64decode(parts[1])).digest()
        return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
    except (OSError, ValueError, IndexError):
        return None


def fingerprint_text(text: str) -> str | None:
    try:
        parts = text.strip().split()
        digest = hashlib.sha256(base64.b64decode(parts[1])).digest()
        return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
    except (ValueError, IndexError):
        return None


def inventory_local(ssh_dir: Path) -> tuple[LocalKey, ...]:
    keys: list[LocalKey] = []
    if not ssh_dir.is_dir():
        return ()
    for public in sorted(ssh_dir.glob("*.pub")):
        private = public.with_suffix("")
        if private.is_file() and private.name not in PROTECTED and not private.is_symlink():
            keys.append(LocalKey(private, public, fingerprint(public)))
    return tuple(keys)


def eligible_for_deletion(key: LocalKey, ssh_dir: Path, dedicated: Path) -> bool:
    try:
        private = key.private.resolve(strict=True)
        public = key.public.resolve(strict=True)
        root = ssh_dir.resolve(strict=True)
    except OSError:
        return False
    return (
        private.parent == root
        and public.parent == root
        and key.private != dedicated
        and private.name not in PROTECTED
        and public.name not in PROTECTED
    )
