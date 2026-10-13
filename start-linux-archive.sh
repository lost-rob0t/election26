#!/usr/bin/env bash
# Foreground Linux entrypoint over the shared source-kit collector.
set +x
set -euo pipefail
umask 077
if [[ "${1:-}" == --nix ]]; then
  shift
  command -v nix >/dev/null || { printf '%s\n' 'Install Nix first, or omit --nix to use Python 3.11+.' >&2; exit 2; }
  # Remote pinned runtime only: never copy the extracted archive directory to Nix.
  exec nix --extra-experimental-features 'nix-command flakes' shell \
    github:NixOS/nixpkgs/c59305bab2065cfecc4944690d9eedbb56f3a9fa#python312 \
    --command bash "${BASH_SOURCE[0]}" "$@"
fi
case "${1:-}" in
  -h|--help)
    printf '%s\n' 'Usage: bash ./start-linux-archive.sh [--nix] [--prepare-only | --status | --brave-query QUERY]'
    printf '%s\n' 'Run under your private HOME outside Git. Default: resume one bounded pass into ./archive.'
    exit 0 ;;
  '') [[ $# == 0 ]] || exit 2 ;;
  --prepare-only|--status) [[ $# == 1 ]] || exit 2 ;;
  --brave-query) [[ $# == 2 && -n "$2" ]] || exit 2 ;;
  *) printf '%s\n' 'Unknown option; use --help.' >&2; exit 2 ;;
esac
[[ "$(uname -s)" == Linux && ! -e /system/bin/getprop ]] || {
  printf '%s\n' 'This launcher requires desktop Linux.' >&2; exit 2;
}
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
[[ "$PWD" == "$(pwd -P)" ]] || {
  printf '%s\n' 'Refusing a symlink launch path.' >&2; exit 2;
}
command -v python3 >/dev/null || {
  printf '%s\n' 'Python 3.11+ is required. On Arch: sudo pacman -S python (run yourself), or use --nix.' >&2; exit 2;
}
# The core uses only the Python standard library. No pip, pkg, activation or install.
# Use only bundled modules; prevent cwd/user-site modules from shadowing the source.
unset PYTHONHOME
export PYTHONPATH="$root/src"
export PYTHONSAFEPATH=1
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
python3 -P -s -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11+ is required")'
exec python3 -I "$root/scripts/linux-runtime.py" "$@"
