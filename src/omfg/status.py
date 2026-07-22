from __future__ import annotations

from pathlib import Path

from omfg.config.shell import ShellInfo
from omfg.execution import Command, CommandResult, CommandRunner
from omfg.models import Plan, RunOptions
from omfg.ui import Terminal
from omfg.workflow import Workflow


class ReadOnlyRunner(CommandRunner):
    def run(self, command: Command, *, check: bool = True) -> CommandResult:
        if command.mutate:
            raise RuntimeError(f"status refused mutating command: {command.argv[0]}")
        return super().run(command, check=check)


class StatusWorkflow:
    def __init__(
        self,
        plan: Plan,
        options: RunOptions,
        terminal: Terminal,
        *,
        runner: CommandRunner | None = None,
        target_shell: ShellInfo | None = None,
        system_release: Path = Path("/etc/os-release"),
    ) -> None:
        self.plan = plan
        self.options = options
        self.terminal = terminal
        self.runner = runner or ReadOnlyRunner(verbose=options.verbose, output=terminal.output)
        self.target_shell = target_shell
        self.system_release = system_release

    def run(self) -> int:
        workflow = Workflow(
            self.plan,
            self.options,
            self.terminal,
            runner=self.runner,
            target_shell=self.target_shell,
            system_release=self.system_release,
        )
        self.terminal.section("Status")
        try:
            results = workflow.verification_results(read_only=True)
        except KeyboardInterrupt:
            self.terminal.output("Status check paused.")
            self.terminal.output("Run omfg status again to continue.")
            return 130
        failures = [result for result in results if not result.passed]
        if failures:
            for result in failures:
                self.terminal.output(f"{result.name}: {result.reason}.")
            self.terminal.output("")
            self.terminal.output("Workstation is not ready.")
            return 1
        workflow.render_readiness()
        self.terminal.output("")
        self.terminal.output("Workstation ready.")
        return 0
