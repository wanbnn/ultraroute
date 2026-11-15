#!/usr/bin/env bash
# Install UltraRoute into an isolated uv tool environment. No Git required.
set -euo pipefail
if [[ "${1:-}" == "--help" ]]; then
  echo 'Usage: bash install.sh [--no-start]'
  exit 0
fi
if [[ "${1:-}" != "" && "${1:-}" != "--no-start" ]]; then
  echo 'Unknown option. Use --help.' >&2
  exit 2
fi
if [[ "$(uname -s)" != "Linux" ]]; then
  echo 'This installer supports Linux. On Windows, use install.ps1.' >&2
  exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  echo 'Installing uv…'
  installer="$(mktemp)"
  trap 'rm -f "$installer"' EXIT
  if command -v curl >/dev/null 2>&1; then
    curl --fail --show-error --silent --location https://astral.sh/uv/install.sh --output "$installer"
  elif command -v wget >/dev/null 2>&1; then
    wget --quiet https://astral.sh/uv/install.sh --output-document="$installer"
  else
    echo 'Install curl or wget and run this installer again.' >&2
    exit 1
  fi
  sh "$installer"
  export PATH="$HOME/.local/bin:$PATH"
fi
uv tool install --upgrade --python 3.13 'https://github.com/wanbnn/ultraroute/archive/refs/heads/main.zip'
tool_bin="$(uv tool dir --bin)"
echo
echo 'UltraRoute installed. Dashboard: http://127.0.0.1:8989'
echo 'Next time, run: ultraroute'
echo 'If the command is not found, restart your terminal or run: uv tool update-shell'
if [[ "${1:-}" != "--no-start" ]]; then
  exec "$tool_bin/ultraroute"
fi
