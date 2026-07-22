from __future__ import annotations

from omfg.execution import Command, CommandRunner


class GitHubConfigurator:
    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    def authenticated(self) -> bool:
        return (
            self.runner.run(
                Command(("gh", "auth", "status", "--hostname", "github.com"), mutate=False),
                check=False,
            ).returncode
            == 0
        )

    def authenticate(self) -> None:
        if not self.authenticated():
            self.runner.run(
                Command(
                    (
                        "gh",
                        "auth",
                        "login",
                        "--hostname",
                        "github.com",
                        "--web",
                        "--git-protocol",
                        "ssh",
                    )
                )
            )
        self.runner.run(
            Command(("gh", "config", "set", "git_protocol", "ssh", "--host", "github.com"))
        )

    def account(self) -> str | None:
        result = self.runner.run(
            Command(("gh", "api", "user", "--jq", ".login"), mutate=False), check=False
        )
        return result.stdout.strip() or None
