#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

readonly TAG="${OMFG_TEST_TAG:-v0.1.2}"
readonly VERSION="${TAG#v}"
readonly PAGES_BASE="${OMFG_TEST_PAGES_BASE:-https://luigiverona.github.io/omfg}"
readonly RELEASE_BASE="${PAGES_BASE}/releases"
readonly INSTALLER="/tmp/omfg-bootstrap-test-install"
readonly TAMPERED_INSTALLER="/tmp/omfg-bootstrap-test-install-tampered"
readonly ARCHIVE="/tmp/omfg-bootstrap-test-${VERSION}.tar.gz"

[[ ${EUID} -eq 0 ]] || { printf 'bootstrap test must prepare its container as root\n' >&2; exit 1; }
[[ ${VERSION} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { printf 'invalid test tag\n' >&2; exit 1; }
curl -fsSL --proto '=https' "${PAGES_BASE}/install" -o "${INSTALLER}"
curl -fsSL --proto '=https' "${RELEASE_BASE}/${TAG}/omfg-${VERSION}.tar.gz" -o "${ARCHIVE}"
expected_sha256="$(sed -n 's/^readonly EXPECTED_SHA256="\([0-9a-f]\{64\}\)"$/\1/p' "${INSTALLER}")"
[[ ${expected_sha256} =~ ^[0-9a-f]{64}$ ]] || { printf 'invalid embedded SHA-256\n' >&2; exit 1; }
printf '%s  %s\n' "${expected_sha256}" "${ARCHIVE}" | sha256sum -c -
! grep -Fq '.sha256' "${INSTALLER}"
bash -n "${INSTALLER}"
shellcheck "${INSTALLER}"

assert_common_install() {
  local user="$1"
  local home="/home/${user}"
  local release="${home}/.local/share/omfg/releases/${VERSION}"

  [[ -x ${home}/.local/bin/omfg ]] || { printf 'launcher is missing for %s\n' "${user}" >&2; exit 1; }
  [[ -L ${home}/.local/share/omfg/current ]] || { printf 'current link is missing for %s\n' "${user}" >&2; exit 1; }
  [[ $(readlink "${home}/.local/share/omfg/current") == "releases/${VERSION}" ]] || { printf 'current link has the wrong target for %s\n' "${user}" >&2; exit 1; }
  [[ -f ${release}/pyproject.toml ]] || { printf 'installed project metadata is missing for %s\n' "${user}" >&2; exit 1; }
  [[ -f ${release}/src/omfg/cli.py ]] || { printf 'installed CLI is missing for %s\n' "${user}" >&2; exit 1; }
  runuser -u "${user}" -- env HOME="${home}" "${home}/.local/bin/omfg" --version
  runuser -u "${user}" -- env HOME="${home}" "${home}/.local/bin/omfg" --help
  runuser -u "${user}" -- env HOME="${home}" "${home}/.local/bin/omfg" --dry-run
  [[ ! -e ${home}/.local/bin/codex ]] || { printf 'unscoped Codex launcher exists for %s\n' "${user}" >&2; exit 1; }
  [[ ! -e ${home}/.local/bin/codex-01 ]] || { printf 'Codex profile launcher exists before setup for %s\n' "${user}" >&2; exit 1; }
  [[ ! -e ${home}/.local/bin/codex-02 ]] || { printf 'Codex profile launcher exists before setup for %s\n' "${user}" >&2; exit 1; }
  [[ ! -e ${home}/.codex ]] || { printf 'default Codex state exists for %s\n' "${user}" >&2; exit 1; }
  [[ ! -e ${home}/.local/share/omfg/codex ]] || { printf 'Codex profile state exists before setup for %s\n' "${user}" >&2; exit 1; }
  if find "${home}" -xdev -user root -print -quit | grep -q .; then
    printf 'root-owned file found in %s\n' "${home}" >&2
    exit 1
  fi
}

install_twice() {
  local user="$1"
  local home="/home/${user}"
  local output

  output="$(runuser -u "${user}" -- env HOME="${home}" \
    OMFG_RELEASE_BASE="${RELEASE_BASE}" \
    bash "${INSTALLER}")"
  grep -Fq "Omfg ${VERSION} installed" <<<"${output}"
  grep -Fq 'A new shell session is required.' <<<"${output}"
  grep -Fq 'Run omfg when you are ready.' <<<"${output}"
  runuser -u "${user}" -- env HOME="${home}" \
    OMFG_RELEASE_BASE="${RELEASE_BASE}" \
    bash "${INSTALLER}" >/dev/null
  assert_common_install "${user}"
}

assert_unmodified_shell_file() {
  local path="$1"
  if [[ -f ${path} ]] && grep -Eq '(Added by omfg|\.local/bin)' "${path}"; then
    printf 'unrelated shell file was modified: %s\n' "${path}" >&2
    exit 1
  fi
}

useradd --create-home --shell /usr/bin/fish omfg-fish
useradd --create-home --shell /bin/bash omfg-bash
useradd --create-home --shell /usr/bin/zsh omfg-zsh
useradd --create-home --shell /bin/bash omfg-bad-checksum

install_twice omfg-fish
# Compare the literal line written by the installer.
# shellcheck disable=SC2016
[[ $(grep -Fxc 'fish_add_path --global --move $HOME/.local/bin' /home/omfg-fish/.config/fish/conf.d/omfg.fish || true) -eq 1 ]] || { printf 'fish PATH entry is missing or duplicated\n' >&2; exit 1; }
assert_unmodified_shell_file /home/omfg-fish/.bashrc
assert_unmodified_shell_file /home/omfg-fish/.zshrc

install_twice omfg-bash
# Compare the literal line written by the installer.
# shellcheck disable=SC2016
[[ $(grep -Fxc 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac' /home/omfg-bash/.bashrc || true) -eq 1 ]] || { printf 'Bash PATH entry is missing or duplicated\n' >&2; exit 1; }
assert_unmodified_shell_file /home/omfg-bash/.config/fish/conf.d/omfg.fish
assert_unmodified_shell_file /home/omfg-bash/.zshrc

install_twice omfg-zsh
# Compare the literal line written by the installer.
# shellcheck disable=SC2016
[[ $(grep -Fxc 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac' /home/omfg-zsh/.zshrc || true) -eq 1 ]] || { printf 'Zsh PATH entry is missing or duplicated\n' >&2; exit 1; }
assert_unmodified_shell_file /home/omfg-zsh/.config/fish/conf.d/omfg.fish
assert_unmodified_shell_file /home/omfg-zsh/.bashrc

sed "s/${expected_sha256}/$(printf '0%.0s' {1..64})/" "${INSTALLER}" >"${TAMPERED_INSTALLER}"
if runuser -u omfg-bad-checksum -- env HOME=/home/omfg-bad-checksum \
  OMFG_RELEASE_BASE="${RELEASE_BASE}" bash "${TAMPERED_INSTALLER}"; then
  printf 'installer accepted an incorrect pinned checksum\n' >&2
  exit 1
fi
[[ ! -e /home/omfg-bad-checksum/.local/bin/omfg ]]
[[ ! -e /home/omfg-bad-checksum/.local/share/omfg/current ]]
[[ ! -e /home/omfg-bad-checksum/.local/share/omfg/releases/${VERSION} ]]

if compgen -G '/tmp/omfg-bootstrap.*' >/dev/null; then
  printf 'temporary bootstrap paths remain\n' >&2
  exit 1
fi

printf 'isolated Arch bootstrap checks passed\n'
