from __future__ import annotations

from collections.abc import Callable


class Terminal:
    def __init__(
        self, *, input_fn: Callable[[str], str] = input, output: Callable[[str], None] = print
    ) -> None:
        self.input = input_fn
        self.output = output

    def section(self, title: str) -> None:
        self.output("")
        self.output(title)
        self.output("")

    def confirm(
        self,
        prompt: str,
        *,
        default: bool = False,
        assume_yes: bool = False,
        destructive: bool = False,
    ) -> bool:
        if assume_yes and not destructive:
            return True
        suffix = " [Y/n] " if default else " [y/N] "
        answer = self.input(prompt + suffix).strip().lower()
        if not answer:
            return default
        return answer in {"y", "yes"}

    def error(
        self, component: str, operation: str, reason: str, log_path: str | None = None
    ) -> None:
        self.output(f"{component} failed while trying to {operation}: {reason}")
        if log_path:
            self.output(f"Log: {log_path}")
        self.output("Rerun with --verbose for details.")
