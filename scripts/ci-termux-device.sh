#!/data/data/com.termux/files/usr/bin/bash
# Called only inside a disposable Android emulator by ci-termux-host.sh.
set -euo pipefail
[[ "$(/system/bin/getprop ro.kernel.qemu)" == 1 ]] || { echo 'Expected an emulator'; exit 2; }
printf 'Android SDK=%s ABI=%s\n' "$(/system/bin/getprop ro.build.version.sdk)" "$(/system/bin/getprop ro.product.cpu.abi)"
bash scripts/install-termux.sh --install --with-yt-dlp
config="$HOME/.config/election26/config.json"
before=$(sha256sum "$config")
bash scripts/install-termux.sh --install --with-yt-dlp
[[ "$before" == "$(sha256sum "$config")" ]]
# Cron must remain absent unless explicitly selected by the operator.
! dpkg-query -W -f='${Status}' cronie 2>/dev/null | grep -q 'install ok installed'
export PATH="$HOME/.local/share/election26/venv/bin:$PATH"
python -c 'import sys, sysconfig; print("TERMUX_PYTHON", sys.executable, sys.version, sysconfig.get_platform()); assert sys.executable.startswith("/data/data/com.termux/files/home/")'
python -m unittest discover -s tests -v
python -m compileall -q src scripts
python scripts/ci-media-fixture.py
"$HOME/.local/bin/election26" --config "$config" --archive "$HOME/.local/share/election26/archive" status
printf '%s\n' 'ANDROID_TERMUX_FIXTURES_PASSED: install twice, config retained, no cron, archive fixtures, offline yt-dlp'
