#!/bin/sh
# Install a versioned fm-telegram release and optionally reconcile a user service.
# This script intentionally never stores a Telegram token.
set -eu

usage() {
  echo "usage: $0 [--data-dir DIR] [--version VERSION] [--python PATH] [--launchd | --systemd [--standby]]" >&2
  exit 2
}

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python=$(command -v python3) || { echo "$0: python3 is required" >&2; exit 1; }
data_dir=${FM_TELEGRAM_DATA_DIR:-"${XDG_DATA_HOME:-$HOME/.local/share}/fm-telegram"}
label=${FM_TELEGRAM_LABEL:-}
# The interpreter the LaunchAgent runs. TCC grants ~/Documents access to the launched
# binary, so a dedicated copy keeps that grant away from every other python3 user.
service_python=${FM_TELEGRAM_PYTHON:-$python}
service=""
version=""
command=run

while [ $# -gt 0 ]; do
  case "$1" in
    --data-dir) data_dir=${2:?missing data directory}; shift 2 ;;
    --version) version=${2:?missing version}; shift 2 ;;
    --python) service_python=${2:?missing python}; shift 2 ;;
    --launchd) [ -z "$service" ] || usage; service=launchd; shift ;;
    --systemd) [ -z "$service" ] || usage; service=systemd; shift ;;
    # The fallback poller for an always-on second machine (docs/standby-fallback.md).
    --standby) command="standby run"; shift ;;
    *) usage ;;
  esac
done

if [ "$command" != run ] && [ "$service" != systemd ]; then usage; fi
[ -n "$label" ] || case "$command" in
  run) label=io.github.fm-telegram.fm-telegram ;;
  *) label=io.github.fm-telegram.fm-telegram-standby ;;
esac

if [ "$service" = launchd ]; then
  case "$service_python" in
    /*) [ -f "$service_python" ] && [ -x "$service_python" ] ;;
    *) false ;;
  esac || { echo "$0: --python must be an absolute path to an executable file" >&2; exit 2; }
fi

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
  "$python" - "$template" "$output" "$label" "$app_dir" "$log_dir" "$service_python" "$command" <<'PY'
import pathlib
import sys
from xml.sax.saxutils import escape
template, output, label, app_dir, log_dir, service_python, command = sys.argv[1:]
text = pathlib.Path(template).read_text()
for key, value in {"@LABEL@": label, "@APP_DIR@": app_dir, "@LOG_DIR@": log_dir,
                   "@PYTHON@": service_python, "@COMMAND@": command}.items():
    text = text.replace(key, escape(value) if output.endswith(".plist") else value)
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
    domain="gui/$(id -u)"
    loaded() { launchctl print "$domain/$label" >/dev/null 2>&1; }
    agent_pid() { launchctl print "$domain/$label" 2>/dev/null | awk '$1 == "pid" { print $3; exit }'; }
    wait_until() {  # <seconds> <command...>: poll once a second
      n=$1; shift
      while [ "$n" -gt 0 ]; do "$@" && return 0; n=$((n - 1)); sleep 1; done
      "$@"
    }
    unloaded() { ! loaded; }
    running() { [ -n "$(agent_pid)" ]; }
    launchctl bootout "$domain/$label" 2>/dev/null || true
    # bootout returns before launchd forgets the job; bootstrapping too early fails with
    # "Bootstrap failed: 5" and leaves the agent stopped.
    wait_until 30 unloaded || { echo "$0: LaunchAgent $label still loaded 30s after bootout" >&2; exit 1; }
    attempt=1
    until launchctl bootstrap "$domain" "$plist"; do
      [ "$attempt" -lt 5 ] || { echo "$0: launchctl bootstrap $label failed $attempt times; the agent is not running" >&2; exit 1; }
      echo "bootstrap of $label failed; retrying ($attempt/5)" >&2
      attempt=$((attempt + 1)); sleep 2
    done
    if ! wait_until 10 running; then
      echo "LaunchAgent $label has no pid after bootstrap; kickstarting" >&2
      launchctl kickstart -k "$domain/$label" || true
      wait_until 10 running || { echo "$0: LaunchAgent $label is loaded but not running; see $logs/fm-telegram.log" >&2; exit 1; }
    fi
    echo "reconciled LaunchAgent $label (pid $(agent_pid))"
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
