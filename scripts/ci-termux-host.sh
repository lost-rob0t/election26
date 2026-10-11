#!/usr/bin/env bash
# Fresh disposable emulator only. No user phone or credentials are used.
set -euo pipefail
[[ "$(adb shell getprop ro.kernel.qemu | tr -d '\r')" == 1 ]] || { echo 'Expected an emulator'; exit 2; }
[[ -z "$(adb shell pm path com.termux | tr -d '\r')" ]] || { echo 'Expected a fresh emulator without Termux'; exit 2; }
version=v0.118.3
asset="termux-app_${version}+github-debug_x86_64.apk"
base="https://github.com/termux/termux-app/releases/download/$version"
work=$(mktemp -d)
curl --fail --location --retry 2 --max-time 180 "$base/${asset/+/%2B}" -o "$work/$asset"
curl --fail --location --retry 2 --max-time 60 "$base/termux-app_${version}%2Bgithub-debug_sha256sums" -o "$work/SHA256SUMS"
python3 - "$work" "$asset" <<'PY'
import hashlib, pathlib, sys
root, asset = pathlib.Path(sys.argv[1]), sys.argv[2]
expected = [line.split()[0] for line in (root/'SHA256SUMS').read_text().splitlines() if line.split()[-1].lstrip('*') == asset]
assert len(expected) == 1, 'Missing/ambiguous official release checksum'
actual = hashlib.sha256((root/asset).read_bytes()).hexdigest()
assert actual == expected[0], 'Official APK checksum mismatch'
print('Official Termux APK SHA256:', actual)
PY
adb install "$work/$asset"
adb logcat -c # disposable fresh-emulator logs only
adb shell am start -n com.termux/.app.TermuxActivity
# Pinned TermuxInstaller.java emits this only AFTER successful second stage.
# bash/HOME presence alone is insufficient and could race bootstrap package work.
ready=false
for attempt in $(seq 1 120); do
  if adb logcat -d -v tag '*:I' | grep -Fx 'I/Termux:TermuxInstaller: Bootstrap packages installed successfully.' >/dev/null &&
     adb shell run-as com.termux test -x /data/data/com.termux/files/usr/bin/bash &&
     adb shell run-as com.termux test -d /data/data/com.termux/files/home; then
    ready=true
    break
  fi
  sleep 2
done
$ready || { echo 'Termux did not confirm completed bootstrap within 240s'; exit 1; }
adb shell run-as com.termux mkdir -p /data/data/com.termux/files/home/election26-ci
# Only committed public source enters the emulator, no Git credential/config files.
git archive HEAD | adb shell -T run-as com.termux /system/bin/tar -xf - -C /data/data/com.termux/files/home/election26-ci
adb shell -T run-as com.termux /system/bin/sh <<'ANDROID'
set -eu
export HOME=/data/data/com.termux/files/home
export PREFIX=/data/data/com.termux/files/usr
export TMPDIR=$PREFIX/tmp
export PATH=$PREFIX/bin:/system/bin
export LANG=C.UTF-8
if [ -f "$PREFIX/lib/libtermux-exec.so" ]; then export LD_PRELOAD=$PREFIX/lib/libtermux-exec.so; fi
cd "$HOME/election26-ci"
exec "$PREFIX/bin/bash" scripts/ci-termux-device.sh
ANDROID
