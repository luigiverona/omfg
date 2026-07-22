# omfg

`omfg` is a production-minded Arch Linux workstation setup tool. A plain `omfg` run validates the host, asks before changing it, performs a supported full system update, installs the declared software, configures Flatpak/Flathub, Git, GitHub SSH access, two isolated Codex profiles, the active shell path, and then independently verifies the result.

Version 0.1.1 supports Arch Linux on x86-64 with fish, Bash, or Zsh. Run it as a normal user with sudo access; the program refuses to run as root.

## Installation

Install the current release with:

```bash
curl -fsSL https://omfg.luigiverona.dev/install | bash
```

The canonical source is [`bootstrap/install`](bootstrap/install). It requires Python 3.11 or newer, downloads an immutable versioned archive, checks its SHA-256 digest, rejects links, special files, and escaping archive paths, extracts it to `~/.local/share/omfg/releases/<version>`, atomically changes the `current` symlink, and creates `~/.local/bin/omfg`. It exits without running setup.

Piping an installer into a shell gives the server control of your user account and is inherently risky. To inspect it before running:

```bash
curl -fsSL https://omfg.luigiverona.dev/install -o install
less install
bash install
```

You can pin the expected release archive digest explicitly:

```bash
OMFG_RELEASE_SHA256=4358b62ca9479ce95fb80020c4aed5e95d0aaa83e1bafb4685de0ad3ebfbc47a bash install
```

The installer otherwise obtains the adjacent published `.sha256` file. Archive verification protects against corruption or an archive differing from the expected digest, but a checksum fetched from the same compromised domain does not provide independent authenticity. For independent verification, compare the digest with the immutable GitHub release and its artifact attestation before running the installer.

For development:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
omfg --dry-run
```

## Workflow selection

No workflow-selection flags means the complete workflow. Flags restrict work to selected capabilities and automatically add only their prerequisites; there are no user-facing subcommands.

```text
--system           full pacman system update
--deps             all dependency categories
--dep CATEGORY     one dependency category; repeatable
--apps             all application categories
--app CATEGORY     one application category; repeatable
--flatpak          Flatpak capability
--flathub          Flathub configuration
--git              Git identity and defaults
--github           GitHub CLI authentication and SSH protocol
--ssh              dedicated GitHub SSH key
--codex            official Codex and both profiles
--check            read-only verification
--dry-run          validate and print a mutation-free plan
--yes              approve normal confirmations, never key deletion
--verbose          show identifiers, sources, commands, and details
--keep-temp        preserve the temporary workspace
```

Unknown application or dependency categories fail with the valid choices. Ordering and source/identifier deduplication are deterministic. Plans describe host-independent top-level software requirements. Before confirmation, read-only state inspection separately reports how many requirements are already present and how many installations remain. Final installed totals count observed absent-to-present transitions, not the size of the original catalog.

## Application catalog

Manifests are strict TOML files under `apps/<category>/manifest.toml`; dependencies live separately under `deps/<category>/manifest.toml`. Every package records its source explicitly.

| Category | Application | Source | Identifier |
|---|---|---|---|
| Browser | LibreWolf | AUR | `librewolf-bin` |
| Browser | Mullvad Browser | AUR | `mullvad-browser-bin` |
| Editor | Microsoft Visual Studio Code | AUR | `visual-studio-code-bin` |
| Game | Sober | Flathub | `org.vinegarhq.Sober` |
| Media | Spotify | Arch `extra` | `spotify-launcher` |
| Social | Discord | Arch `extra` | `discord` |
| VPN | Mullvad VPN | Arch `extra` | `mullvad-vpn` |
| Development | OpenAI Codex CLI | official OpenAI standalone release | `codex` |

These identifiers were checked against the Arch package database, AUR RPC metadata, Flathub, and official OpenAI documentation on 2026-07-22. SoundCloud is intentionally absent. Package managers resolve transitive dependencies such as `mullvad-vpn-daemon`; they are not duplicated as application requirements.

## System and package safety

System updates use `pacman -Syu`; partial Arch upgrades are not performed. Package managers resolve transitive dependencies. Flatpak uses a consistent per-user scope and configures Flathub idempotently.

All disposable downloads, extraction, logs, state, packages, and AUR clones use a race-safe `omfg-*` directory beneath `${TMPDIR:-/tmp}`. Successful runs remove it; failed runs and `--keep-temp` preserve it. AUR bootstrap installs `git` and `base-devel`, validates the `yay-bin` clone origin, runs `makepkg` only as the normal user, and elevates only `pacman -U` for the selected built package. A dedicated makepkg configuration disables automatic debug-package outputs so they cannot become unintended top-level installations.

## Git, GitHub, and SSH

Git reads existing global values before changing `user.name`, `user.email`, and `init.defaultBranch=main`; unrelated configuration is preserved. GitHub CLI reuses valid authentication or opens its browser flow, verifies the account, and sets GitHub’s Git protocol to SSH. Tokens are not printed or logged.

SSH management inventories local key pairs and registered GitHub keys, creates `~/.ssh/id_ed25519_omfg_github`, updates only an `omfg`-owned GitHub block in SSH config, uploads the public key, and verifies GitHub authentication before considering cleanup. Existing keys are preserved by default. Choosing not to preserve them shows exact eligible keys and requires a second `[y/N]` confirmation. `--yes` cannot approve deletion. Protected SSH files and unrelated-host keys are never deletion targets.

## Dual Codex profiles

One shared executable is installed using OpenAI’s official standalone Linux installer at `https://chatgpt.com/codex/install.sh`. `omfg` pins the audited installer SHA-256 and fails closed when OpenAI changes it. The verified installer resolves the latest official `openai/codex` release, maps x86-64 Linux to `x86_64-unknown-linux-musl`, verifies the release metadata and SHA-256 manifests, installs a versioned release atomically, and retains versioned state for upgrades and rollback. `omfg` constrains `CODEX_INSTALL_DIR`, `CODEX_HOME`, `HOME`, and `PATH` so the upstream installer cannot create a public unscoped launcher, write to `~/.codex`, or modify another shell file. Only `codex-01` and `codex-02` are public. They set distinct homes:

```text
~/.local/share/omfg/codex/01
~/.local/share/omfg/codex/02
```

Each home is mode `0700`, uses file credential storage, and isolates configuration, credentials, sessions, history, logs, skills, packages, and caches. Credential-file permissions are checked without reading credential values. The complete flow signs in and verifies each profile independently; credential contents are never inspected or displayed. An omfg-owned legacy `~/.local/bin/codex` symlink is removed, while an unrelated user-owned file is reported by verification rather than overwritten.

## Shell PATH

Shell detection walks process ancestry for an actually interactive fish, Bash, or Zsh process. Non-interactive Bash wrappers such as `curl ... | bash` and `bash -lc` are ignored; the target account's login shell is preferred over a misleading `$SHELL` fallback. fish receives one `conf.d/omfg.fish` file, Bash uses `.bashrc`, and Zsh uses `.zshrc`. These are the files read by normal interactive terminal sessions on Arch; the default Arch Bash login profile also sources `.bashrc`. Updates are atomic and idempotent, preserve unrelated content, reject symbolic startup files, and never modify multiple shells. Output states whether the current process already sees `~/.local/bin` or a new session is required.

## Development and validation

Tests use temporary homes, fake runners, injected prompts, and mocks. They do not call sudo, mutate the real package database, touch real Git/SSH/shell configuration, or authenticate accounts. CI covers Python 3.11/3.13, Ruff, mypy, ShellCheck, and an Arch container.

```bash
python -m compileall src tests
python -m unittest discover
ruff check .
ruff format --check .
mypy src
bash -n bootstrap/install
shellcheck bootstrap/install
omfg --help
omfg --version
omfg --dry-run
```

Release artifacts are explicit runtime archives rather than GitHub-generated source archives.
From a clean tagged checkout, maintainers build and independently validate one with:

```bash
python tools/build_release.py --tag v0.1.1
python tools/validate_release.py dist/omfg-0.1.1.tar.gz
```

The builder selects only tracked runtime files and normalizes archive ordering, ownership,
permissions, timestamps, and gzip metadata. `dist/` remains ignored. The release workflow builds
twice from independent clean checkouts, verifies identical SHA-256 values, publishes a complete
draft, verifies uploaded bytes, and only then publishes it. Pages deployment downloads those
published assets; it does not rebuild them.

Normal output is intentionally plain: no symbols, boxes, stage numbers, package-manager diffs, or raw package-manager output. `--verbose` exposes operational detail but redacts configured secrets. Atomic writes protect launchers and owned configuration blocks from partial replacement.
