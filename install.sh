#!/bin/sh
# Copies the programs to a stable user-owned location and optionally loads per-user LaunchAgents.
# Usage: ./install.sh [--agent bridge] [--agent oss]
set -eu
usage() { echo "usage: $0 [--agent bridge] [--agent oss]" >&2; exit 2; }
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
data="${XDG_DATA_HOME:-$HOME/.local/share}/fm-telegram"
prefix="${FM_TELEGRAM_LABEL_PREFIX:-io.github.fm-telegram}"
agents=""
while [ $# -gt 0 ]; do
  case "$1" in
    --agent)
      case "${2:-}" in
        bridge) agents="$agents fm-telegram" ;;
        oss) agents="$agents oss-watch oss-weekly" ;;
        *) usage ;;
      esac
      shift 2 ;;
    *) usage ;;
  esac
done
if [ -n "$agents" ] && ! command -v launchctl >/dev/null 2>&1; then
  echo "$0: --agent is macOS-only (no launchctl); on Linux run the bridge in a terminal, tmux or a systemd user service" >&2
  exit 1
fi
mkdir -p "$data"
chmod 700 "$data"
cp "$root/fm_telegram.py" "$root/oss_watch.py" "$data/"
chmod 700 "$data/fm_telegram.py" "$data/oss_watch.py"
echo "installed fm_telegram.py and oss_watch.py in $data"
[ -n "$agents" ] || exit 0
# Resolve python3 now so the agent never depends on launchd's minimal PATH.
python=$(command -v python3)
logs="$HOME/Library/Logs"
mkdir -p "$HOME/Library/LaunchAgents" "$logs"
for name in $agents; do
  plist="$HOME/Library/LaunchAgents/$prefix.$name.plist"
  # shortcut: sed substitution breaks on paths containing | or &; such paths are not supported.
  sed -e "s|@LABEL@|$prefix|g" -e "s|@PYTHON@|$python|g" -e "s|@DATA_DIR@|$data|g" \
    -e "s|@LOG_DIR@|$logs|g" "$root/launchd/$name.plist" > "$plist"
  chmod 644 "$plist"
  launchctl bootout "gui/$(id -u)/$prefix.$name" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "loaded LaunchAgent $prefix.$name"
done
