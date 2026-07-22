#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

readonly TAG="${OMFG_TEST_TAG:-v0.1.0}"
readonly VERSION="${TAG#v}"
readonly PAGES_BASE="${OMFG_TEST_PAGES_BASE:-https://luigiverona.github.io/omfg}"
readonly RELEASE_BASE="${PAGES_BASE}/releases"
readonly EXPECTED_SHA256="${OMFG_TEST_SHA256:?OMFG_TEST_SHA256 is required}"
readonly INSTALLER="/tmp/omfg-bootstrap-test-install"
readonly ARCHIVE="/tmp/omfg-bootstrap-test-${VERSION}.tar.gz"

[[ ${EUID} -eq 0 ]] || { printf 'bootstrap test must prepare its container as root\n' >&2; exit 1; }
[[ ${VERSION} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { printf 'invalid test tag\n' >&2; exit 1; }
[[ ${EXPECTED_SHA256} =~ ^[0-9a-f]{64}$ ]] || { printf 'invalid expected SHA-256\n' >&2; exit 1; }

curl -fsSL --proto '=https' "${PAGES_BASE}/install" -o "${INSTALLER}"
curl -fsSL --proto '=https' "${RELEASE_BASE}/${TAG}/omfg-${VERSION}.tar.gz" -o "${ARCHIVE}"
cmp "${INSTALLER}" bootstrap/install
printf '%s  %s\n' "${EXPECTED_SHA256}" "${ARCHIVE}" | sha256sum -c -
bash -n "${INSTALLER}"
shellcheck "${INSTALLER}"

assert_common_install() {
  local user="$1"
  local home="/home/${user}"
  local release="${home}/.local/share/omfg/releases/${VERSION}"

  [[ -x ${home}/.local/bin/omfg ]]
  [[ -L ${home}/.local/share/omfg/current ]]
  [[ $(readlink "${home}/.local/share/omfg/current") == "releases/${VERSION}" ]]
  [[ -f ${release}/pyproject.toml ]]
  [[ -f ${release}/src/omfg/cli.py ]]
  runuser -u "${user}" -- env HOME="${home}" "${home}/.local/bin/omfg" --version
  runuser -u "${user}" -- env HOME="${home}" "${home}/.local/bin/omfg" --help >/dev/null
  runuser -u "${user}" -- env HOME="${home}" "${home}/.local/bin/omfg" --dry-run >/dev/null
  [[ ! -e ${home}/.local/bin/codex ]]
  [[ ! -e ${home}/.local/bin/codex-01 ]]
  [[ ! -e ${home}/.local/bin/codex-02 ]]
  [[ ! -e ${home}/.codex ]]
  [[ ! -e ${home}/.local/share/omfg/codex ]]
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
    OMFG_RELEASE_BASE="${RELEASE_BASE}" OMFG_RELEASE_SHA256="${EXPECTED_SHA256}" \
    bash "${INSTALLER}")"
  grep -Fq "Omfg ${VERSION} installed" <<<"${output}"
  grep -Fq 'A new shell session is required.' <<<"${output}"
  grep -Fq 'Run omfg when you are ready.' <<<"${output}"
  runuser -u "${user}" -- env HOME="${home}" \
    OMFG_RELEASE_BASE="${RELEASE_BASE}" OMFG_RELEASE_SHA256="${EXPECTED_SHA256}" \
    bash "${INSTALLER}" >/dev/null
  assert_common_install "${user}"
}

useradd --create-home --shell /usr/bin/fish omfg-fish
useradd --create-home --shell /bin/bash omfg-bash
useradd --create-home --shell /usr/bin/zsh omfg-zsh
useradd --create-home --shell /bin/bash omfg-bad-checksum

install_twice omfg-fish
# Compare the literal line written by the installer.
# shellcheck disable=SC2016
[[ $(grep -Fxc 'fish_add_path --global --move $HOME/.local/bin' /home/omfg-fish/.config/fish/conf.d/omfg.fish) -eq 1 ]]
[[ ! -e /home/omfg-fish/.bashrc && ! -e /home/omfg-fish/.zshrc ]]

install_twice omfg-bash
# Compare the literal line written by the installer.
# shellcheck disable=SC2016
[[ $(grep -Fxc 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac' /home/omfg-bash/.bashrc) -eq 1 ]]
[[ ! -e /home/omfg-bash/.config/fish/conf.d/omfg.fish && ! -e /home/omfg-bash/.zshrc ]]

install_twice omfg-zsh
# Compare the literal line written by the installer.
# shellcheck disable=SC2016
[[ $(grep -Fxc 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac' /home/omfg-zsh/.zshrc) -eq 1 ]]
[[ ! -e /home/omfg-zsh/.config/fish/conf.d/omfg.fish && ! -e /home/omfg-zsh/.bashrc ]]

if runuser -u omfg-bad-checksum -- env HOME=/home/omfg-bad-checksum \
  OMFG_RELEASE_BASE="${RELEASE_BASE}" OMFG_RELEASE_SHA256="$(printf '0%.0s' {1..64})" \
  bash "${INSTALLER}"; then
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
