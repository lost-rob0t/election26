#!/data/data/com.termux/files/usr/bin/bash
# One foreground, bounded acquisition. Run with bash after extracting in Termux HOME.
set +x
set -euo pipefail
umask 077
usage() {
  printf '%s\n' 'Usage: bash /path/to/start-archive.sh [--prepare-only | --status | --brave-query QUERY]'
  printf '%s\n' 'Default: install if needed, create ./archive and ./archive-config.json, then resume one bounded public-source pass.'
  printf '%s\n' 'Run inside Termux private HOME, outside Git. No cron. Media links only unless your config explicitly opts in.'
}
case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  '') [[ $# == 0 ]] || { usage >&2; exit 2; } ;;
  --prepare-only|--status) [[ $# == 1 ]] || { usage >&2; exit 2; } ;;
  --brave-query) [[ $# == 2 && -n "$2" ]] || { usage >&2; exit 2; } ;;
  *) usage >&2; exit 2 ;;
esac
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
home=${HOME:?HOME must be set}
prefix=${PREFIX:-/data/data/com.termux/files/usr}
if [[ "$home" != /data/data/com.termux/files/home || "$prefix" != /data/data/com.termux/files/usr ]] || ! /system/bin/getprop ro.build.version.sdk >/dev/null 2>&1; then
  printf '%s\n' 'Use the standard Termux Android app and its private HOME/PREFIX.' >&2; exit 2
fi
cwd=$(pwd -P)
if [[ "$PWD" != "$cwd" || "$cwd/" != "$home/"* || -L "$cwd/archive" || -L "$cwd/archive-config.json" ]]; then
  printf '%s\n' 'Refusing a symlink or non-private launch path. Extract and run under Termux HOME.' >&2; exit 2
fi
cursor=$cwd
while :; do
  if [[ -e "$cursor/.git" || -L "$cursor/.git" ]]; then
    printf '%s\n' 'Archive must be outside any Git checkout. Use an extracted source ZIP in private HOME.' >&2; exit 2
  fi
  [[ "$cursor" == / ]] && break
  cursor=$(dirname -- "$cursor")
done
if [[ -e "$cwd/archive" && ! -d "$cwd/archive" ]]; then
  printf '%s\n' './archive exists and is not a directory; nothing was replaced.' >&2; exit 2
fi
if [[ "${1:-}" == --brave-query && -z "${BRAVE_SEARCH_API_KEY:-}" ]]; then
  printf '%s\n' 'Brave requires BRAVE_SEARCH_API_KEY in the environment. Never put a key in arguments or config.' >&2; exit 2
fi
if ! bash "$root/scripts/install-termux.sh" --install --if-needed; then
  printf '%s\n' 'Setup failed. Check connectivity/package errors, then rerun the same command. Existing archive/config were not replaced.' >&2
  exit 2
fi
# Invoke the managed interpreter directly; no PATH edits or installed CLI lookup.
exec "$home/.local/share/election26/venv/bin/python" -m election26.kit --source "$root" "$@"
