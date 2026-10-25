#!/bin/sh
# Install a versioned fm-telegram release and optionally reconcile a user service.
# This script intentionally never stores a Telegram token.
set -eu

usage() {
  echo "usage: $0 [--data-dir DIR] [--version VERSION] [--launchd | --systemd]" >&2
  exit 2
}

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python=$(command -v python3) || { echo "$0: python3 is required" >&2; exit 1; }
data_dir=${FM_TELEGRAM_DATA_DIR:-"${XDG_DATA_HOME:-$HOME/.local/share}/fm-telegram"}
label=${FM_TELEGRAM_LABEL:-io.github.fm-telegram.fm-telegram}
service=""
version=""

while [ $# -gt 0 ]; do
  case "$1" in
    --data-dir) data_dir=${2:?missing data directory}; shift 2 ;;
    --version) version=${2:?missing version}; shift 2 ;;
    --launchd) [ -z "$service" ] || usage; service=launchd; shift ;;
    --systemd) [ -z "$service" ] || usage; service=systemd; shift ;;
    *) usage ;;
  esac
done

if [ -z "$version" ]; then
  version=$(git -C "$root" rev-parse --verify --short=12 HEAD 2>/dev/null || true)
fi
case "$version" in
  ''|*[!A-Za-z0-9._-]*) echo "$0: version must contain only letters, digits, ., _, or -" >&2; exit 2 ;;
esac

releases="$data_dir/releases"
release="$releases/$version"
mkdir -p "$releases"
chmod 700 "$data_dir" "$releases"
if [ ! -f "$release/fm_telegram.py" ]; then
  staging="$releases/.$version.$$"
  trap 'rm -rf "$staging"' EXIT HUP INT TERM
  mkdir "$staging"
  chmod 700 "$staging"
  cp "$root/fm_telegram.py" "$root/oss_watch.py" "$staging/"
  chmod 700 "$staging/fm_telegram.py" "$staging/oss_watch.py"
  mv "$staging" "$release"
  trap - EXIT HUP INT TERM
fi

# A fresh symlink makes switching releases atomic for a newly started service.
next="$data_dir/.current.$$"
ln -s "releases/$version" "$next"
"$python" - "$next" "$data_dir/current" <<'PY'
import os
import sys
os.replace(sys.argv[1], sys.argv[2])
PY
echo "installed fm-telegram release $version in $release"

[ -n "$service" ] || exit 0

render() {
  template=$1 output=$2 app_dir=$3 log_dir=${4:-}
  "$python" - "$template" "$output" "$label" "$app_dir" "$log_dir" <<'PY'
import pathlib
import sys
template, output, label, app_dir, log_dir = sys.argv[1:]
text = pathlib.Path(template).read_text()
for key, value in {"@LABEL@": label, "@APP_DIR@": app_dir, "@LOG_DIR@": log_dir}.items():
    text = text.replace(key, value)
path = pathlib.Path(output)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(text)
PY
}

app_dir="$data_dir/current"
case "$service" in
  launchd)
    command -v launchctl >/dev/null 2>&1 || { echo "$0: --launchd requires macOS launchctl" >&2; exit 1; }
    plist="$HOME/Library/LaunchAgents/$label.plist"
    logs="$HOME/Library/Logs"
    mkdir -p "$logs"
    render "$root/deploy/launchd/fm-telegram.plist" "$plist" "$app_dir" "$logs"
    chmod 644 "$plist"
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    echo "reconciled LaunchAgent $label"
    ;;
  systemd)
    command -v systemctl >/dev/null 2>&1 || { echo "$0: --systemd requires systemctl" >&2; exit 1; }
    unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$label.service"
    render "$root/deploy/systemd/fm-telegram.service" "$unit" "$app_dir"
    chmod 644 "$unit"
    systemctl --user daemon-reload
    systemctl --user enable --now "$label.service"
    echo "reconciled systemd user unit $label"
    ;;
esac
