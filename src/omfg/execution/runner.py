from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from omfg.errors import CommandError


@dataclass(frozen=True, slots=True)
class Command:
    argv: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    sensitive_values: tuple[str, ...] = ()
    mutate: bool = True


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def __init__(
        self,
        *,
        dry_run: bool = False,
        verbose: bool = False,
        output: Callable[[str], None] = print,
    ) -> None:
        self.dry_run = dry_run
        self.verbose = verbose
        self.output = output
        self.history: list[Command] = []

    @staticmethod
    def redact(value: str, secrets: Sequence[str]) -> str:
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value

    def run(self, command: Command, *, check: bool = True) -> CommandResult:
        if not command.argv or any("\0" in arg for arg in command.argv):
            raise CommandError("command", "validate", "invalid argument vector")
        self.history.append(command)
        if self.verbose:
            rendered = shlex.join(command.argv)
            self.output("$ " + self.redact(rendered, command.sensitive_values))
        if self.dry_run and command.mutate:
            return CommandResult(command.argv, 0, "", "")
        env = os.environ.copy()
        if command.env:
            env.update(command.env)
        completed = subprocess.run(
            command.argv,
            cwd=command.cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        result = CommandResult(
            command.argv,
            completed.returncode,
            self.redact(completed.stdout, command.sensitive_values),
            self.redact(completed.stderr, command.sensitive_values),
        )
        if self.verbose:
            if result.stdout:
                self.output(result.stdout.rstrip())
            if result.stderr:
                self.output(result.stderr.rstrip())
        if check and result.returncode:
            reason = (
                result.stderr.strip().splitlines()[-1]
                if result.stderr.strip()
                else "command exited nonzero"
            )
            raise CommandError("command", "execute", reason, result.returncode)
        return result
