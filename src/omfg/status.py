from __future__ import annotations

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
    ) -> None:
        self.plan = plan
        self.options = options
        self.terminal = terminal
        self.runner = runner or ReadOnlyRunner(verbose=options.verbose, output=terminal.output)

    def run(self) -> int:
        workflow = Workflow(self.plan, self.options, self.terminal, runner=self.runner)
        self.terminal.section("Status")
        try:
            results = workflow.verification_results()
        except (OSError, RuntimeError) as exc:
            self.terminal.output(f"Status check failed: {exc}.")
            self.terminal.output("")
            self.terminal.output("Workstation is not ready.")
            return 1
        failures = [result for result in results if not result.passed]
        if failures:
            visible = failures if self.options.verbose else failures[:5]
            for result in visible:
                self.terminal.output(f"{result.name}: {result.reason}.")
            if len(visible) < len(failures):
                self.terminal.output(f"{len(failures) - len(visible)} additional checks failed.")
            self.terminal.output("")
            self.terminal.output("Workstation is not ready.")
            return 1
        workflow.render_readiness()
        self.terminal.output("")
        self.terminal.output("Workstation ready.")
        return 0
