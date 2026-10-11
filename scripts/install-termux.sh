#!/data/data/com.termux/files/usr/bin/bash
# Preview by default. Run explicitly inside Termux with --install to make changes.
set -euo pipefail
install=false
with_ytdlp=false
with_ffmpeg=false
with_cron=false
if_needed=false
for arg in "$@"; do
  case "$arg" in
    --install) install=true ;;
    --with-yt-dlp) with_ytdlp=true ;;
    --with-ffmpeg) with_ffmpeg=true ;;
    --with-cron) with_cron=true ;;
    --if-needed) if_needed=true ;;
    --help|-h)
      printf '%s\n' 'Usage: bash scripts/install-termux.sh [--install] [--if-needed] [--with-yt-dlp] [--with-ffmpeg] [--with-cron]'
      printf '%s\n' 'Default: print the plan. Existing config/archive/cron entries are preserved.'
      exit 0 ;;
    *) printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
  esac
done
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
prefix=${PREFIX:-/data/data/com.termux/files/usr}
home=${HOME:?HOME must be set}
packages=(python python-pip python-ensurepip-wheels)
$with_ytdlp && packages+=(python-yt-dlp)
$with_ffmpeg && packages+=(ffmpeg)
$with_cron && packages+=(cronie)
printf 'Official Termux packages: %s\n' "${packages[*]}"
printf 'Private application root: %s/.local/share/election26\n' "$home"
printf 'Config (create only if absent): %s/.config/election26/config.json\n' "$home"
printf '%s\n' 'No cron table, daemon, shared-storage permission, or phone setting will be changed.'
if ! $install; then
  printf '%s\n' 'Preview only. Add --install from a Termux session to apply.'
  exit 0
fi
if [[ "$prefix" != /data/data/com.termux/files/usr ]] || [[ "$home" != /data/data/com.termux/files/home ]] || ! /system/bin/getprop ro.build.version.sdk >/dev/null 2>&1; then
  printf '%s\n' 'Installation requires the standard Termux Android app and its private HOME/PREFIX.' >&2
  exit 2
fi
umask 077
missing=()
for package in "${packages[@]}"; do
  if [[ "$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)" != 'install ok installed' ]]; then
    missing+=("$package")
  fi
done
if ((${#missing[@]})); then
  apt-get update
  apt-get install -y --no-install-recommends -o Dpkg::Options::=--force-confold "${missing[@]}"
fi
venv="$home/.local/share/election26/venv"
if $if_needed && [[ -x "$venv/bin/python" ]] && "$venv/bin/python" "$root/scripts/termux_setup.py" --home "$home" --source "$root" --check-install; then
  printf '%s\n' 'Verified installed collector matches this source; no package download or reinstall needed.'
  exit 0
fi
python "$root/scripts/termux_setup.py" --home "$home" --source "$root"
# Never upgrade Termux's system pip. Build/install only inside the managed venv.
"$venv/bin/python" -m pip install --disable-pip-version-check --no-deps "$root"
"$venv/bin/python" "$root/scripts/termux_setup.py" --home "$home" --source "$root" --record-install
"$venv/bin/election26" --config "$home/.config/election26/config.json" --archive "$home/.local/share/election26/archive" status
printf '\nInstalled. Local command: %s/.local/bin/election26\n' "$home"
printf '%s\n' 'Choose source hosts and limits in your private config, then run a bounded pass.'
