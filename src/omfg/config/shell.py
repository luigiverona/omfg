from __future__ import annotations

import os
import pwd
from dataclasses import dataclass
from pathlib import Path

from omfg.config.files import atomic_write


@dataclass(frozen=True, slots=True)
class ShellInfo:
    name: str
    executable: Path
    source: str


def detect_shell(
    *, env: dict[str, str] | None = None, uid: int | None = None, proc_comm: str | None = None
) -> ShellInfo:
    env = env or dict(os.environ)
    uid = os.getuid() if uid is None else uid
    candidates: list[tuple[str, str]] = []
    if proc_comm:
        candidates.append((proc_comm.lstrip("-"), "process"))
    shell_env = env.get("SHELL")
    try:
        login = pwd.getpwuid(uid).pw_shell
    except KeyError:
        login = ""
    if login:
        candidates.append((login, "login"))
    if shell_env:
        candidates.append((shell_env, "environment"))
    for candidate, source in candidates:
        path = Path(candidate)
        name = path.name
        if name in {"fish", "bash", "zsh"}:
            return ShellInfo(name, path if path.is_absolute() else Path("/usr/bin") / name, source)
    raise ValueError("supported interactive shell not detected (fish, bash, or zsh required)")


def configure_path(home: Path, shell: ShellInfo, *, dry_run: bool = False) -> tuple[Path, bool]:
    if shell.name == "fish":
        path = home / ".config/fish/conf.d/omfg.fish"
        line = "fish_add_path --global --move $HOME/.local/bin\n"
    elif shell.name == "zsh":
        path = home / ".zprofile"
        line = 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac\n'
    else:
        path = home / ".bash_profile"
        line = 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac\n'
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if line.strip() in existing:
        return path, False
    content = existing
    if content and not content.endswith("\n"):
        content += "\n"
    content += "\n# Added by omfg\n" + line
    if not dry_run:
        atomic_write(path, content, 0o644)
    return path, True


def path_configured(home: Path, shell: ShellInfo) -> bool:
    path, _ = configure_path(home, shell, dry_run=True)
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if shell.name == "fish":
        return "fish_add_path --global --move $HOME/.local/bin" in text.splitlines()
    return "$HOME/.local/bin:$PATH" in text
