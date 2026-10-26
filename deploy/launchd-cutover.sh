#!/bin/sh
# Move a running fm-telegram from a shell restart loop (for example a Herdr pane running
# `while true; do python3 .../fm_telegram.py run; sleep 5; done`) to a LaunchAgent.
#
#   check      read-only preflight; safe at any time
#   apply      stop the loop, install and bootstrap the agent, verify one poller and a
#              Telegram round trip; rolls back automatically on any failure
#   rollback   bootout the agent, park its plist, restart the loop in its pane
#   stop-loop  only stop the restart loop (a step of apply)
#
# Exactly one getUpdates poller may exist at any moment: a new one is never started
# while another still runs. Run it from the reviewed checkout that should be installed.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
label=${FM_TELEGRAM_LABEL:-io.github.fm-telegram.fm-telegram}
case "$label" in ''|*[!A-Za-z0-9.-]*) echo "$0: label must be a plain reverse-DNS name" >&2; exit 2 ;; esac
data_dir=${FM_TELEGRAM_DATA_DIR:-"${XDG_DATA_HOME:-$HOME/.local/share}/fm-telegram"}
state_dir=${FM_TELEGRAM_STATE_DIR:-"${XDG_STATE_HOME:-$HOME/.local/state}/fm-telegram"}
# The dedicated interpreter the agent runs (see deploy/README.md); required by check/apply.
python=${FM_TELEGRAM_PYTHON:-}
match=${FM_TELEGRAM_MATCH:-fm_telegram.py run}
# Where rollback restarts the old loop; without both, rollback prints the manual step.
pane=${FM_TELEGRAM_PANE:-}
loop_cmd=${FM_TELEGRAM_LOOP:-}
wait_note=${FM_TELEGRAM_WAIT_NOTE:-300}
uid=$(id -u)
plist="$HOME/Library/LaunchAgents/$label.plist"
log="$HOME/Library/Logs/fm-telegram.log"

say() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { say "FAIL: $*" >&2; exit 1; }
pollers() { pgrep -f "$match" || true; }
agent_pid() { launchctl print "gui/$uid/$label" 2>/dev/null | awk '$1 == "pid" { print $3; exit }'; }
wait_until() {  # <seconds> <command...>: poll once a second
  n=$1; shift
  while [ "$n" -gt 0 ]; do "$@" && return 0; n=$((n - 1)); sleep 1; done
  return 1
}
no_pollers() { [ -z "$(pollers)" ]; }
one_poller() { [ "$(pollers | grep -c .)" -eq 1 ]; }
gone() { ! kill -0 "$1" 2>/dev/null; }
loop_sleep() { s=$(pgrep -P "$shell" -x sleep || true); [ -n "$s" ]; }
agent_running() { [ -n "$(agent_pid)" ]; }
note_ids() {
  "$python" -I -c 'import json, sys
try: print("\n".join(json.load(open(sys.argv[1])).get("telegram_note_ids", [])))
except FileNotFoundError: pass' "$state_dir/state.json" | sort
}

check() {
  ok=0
  if [ -z "$python" ]; then
    say "set FM_TELEGRAM_PYTHON to the dedicated interpreter"; ok=1
  elif [ ! -x "$python" ]; then
    say "runtime missing: $python"; ok=1
  else
    codesign -d -r- "$python" 2>&1 | grep -q 'designated => cdhash' \
      || { say "runtime is not ad-hoc signed (a TCC grant would not pin to it)"; ok=1; }
    "$python" -I -c 'import ssl' || { say "runtime cannot import ssl"; ok=1; }
    "$python" -I "$root/fm_telegram.py" --help >/dev/null || { say "checkout does not run on the runtime"; ok=1; }
  fi
  for tool in whisper-cli herdr; do
    PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin command -v "$tool" >/dev/null \
      || say "warning: $tool is not on the agent PATH"
  done
  if launchctl print "gui/$uid/$label" >/dev/null 2>&1; then say "LaunchAgent $label is already loaded"; ok=1; fi
  pids=$(pollers)
  case "$(printf '%s\n' "$pids" | grep -c .)" in
    0) say "no poller running" ;;
    1) say "one poller: pid $pids under $(ps -o comm= -p "$(ps -o ppid= -p "$pids" | tr -d ' ')")" ;;
    *) say "more than one poller: $pids"; ok=1 ;;
  esac
  if [ -z "$pane" ] || [ -z "$loop_cmd" ]; then
    say "warning: FM_TELEGRAM_PANE/FM_TELEGRAM_LOOP unset; rollback must restart the loop by hand"
  elif ! herdr pane get "$pane" >/dev/null 2>&1; then
    say "warning: pane $pane not found; rollback must restart the loop by hand"
  fi
  if [ "$ok" -eq 0 ]; then say "check passed"; else say "check FAILED"; fi
  return "$ok"
}

stop_loop() {
  pids=$(pollers)
  [ -n "$pids" ] || { say "no poller running"; return 0; }
  [ "$(printf '%s\n' "$pids" | grep -c .)" -eq 1 ] || die "more than one poller: $pids"
  shell=$(ps -o ppid= -p "$pids" | tr -d ' ')
  case "$(ps -o comm= -p "$shell")" in *sh) ;; *) die "poller $pids is not run by a shell loop" ;; esac
  bridge=$pids
  for attempt in 1 2 3; do
    # The bridge exits 0 on SIGINT after its current long poll (<= 40s), so the loop
    # survives it; SIGINT to the loop's `sleep` is what makes the shell break the loop.
    say "stopping bridge pid $bridge (attempt $attempt)"
    kill -INT "$bridge" 2>/dev/null || true
    wait_until 60 gone "$bridge" || die "bridge $bridge did not exit"
    if wait_until 10 loop_sleep; then kill -INT "$s" 2>/dev/null || true; fi
    sleep 7
    bridge=$(pgrep -P "$shell" -f "$match" || true)
    [ -n "$bridge" ] || { no_pollers || die "a poller outside the loop runs: $(pollers)"; say "loop stopped"; return 0; }
  done
  die "the loop kept restarting the bridge"
}

restart_loop() {
  no_pollers || die "a poller still runs ($(pollers)); not restarting the loop"
  if [ -n "$pane" ] && [ -n "$loop_cmd" ] && herdr pane get "$pane" >/dev/null 2>&1 \
    && herdr pane run "$pane" "$loop_cmd" >/dev/null && wait_until 20 one_poller; then
    say "pane loop restarted in $pane: pid $(pollers)"
  else
    say "restart the previous loop by hand${loop_cmd:+:}"; [ -z "$loop_cmd" ] || printf '  %s\n' "$loop_cmd"
    return 1
  fi
}

rollback() {
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  wait_until 60 no_pollers || die "poller still running after bootout: $(pollers)"
  # Parked outside LaunchAgents so the next login cannot start a second poller.
  if [ -f "$plist" ]; then
    mkdir -p "$data_dir"
    mv -f -- "$plist" "$data_dir/$label.plist.rolled-back"
    say "parked $plist in $data_dir"
  fi
  restart_loop
}

verify() {
  wait_until 30 agent_running || { say "agent did not start"; return 1; }
  pid=$(agent_pid)
  sleep 15  # a crash loop shows up as a new pid or a fatal log line
  [ "$(agent_pid)" = "$pid" ] || { say "agent restarted: crash loop?"; return 1; }
  [ "$(pollers)" = "$pid" ] || { say "expected only pid $pid, pollers: $(pollers)"; return 1; }
  if tail -c "+$(($1 + 1))" "$log" 2>/dev/null | grep -E 'HTTP 409|Traceback|^fm-telegram: '; then
    say "log shows a conflict or a fatal error"; return 1
  fi
  say "one poller: LaunchAgent pid $pid"
  before=$(mktemp); note_ids > "$before"
  "$python" -I "$data_dir/current/fm_telegram.py" notify \
    "fm-telegram launchd cutover check: send any message within ${wait_note}s." \
    || { rm -f -- "$before"; say "notify failed"; return 1; }
  say "waiting up to ${wait_note}s for a Telegram message to become a note"
  n=$wait_note
  while [ "$n" -gt 0 ]; do
    new=$(note_ids | comm -13 "$before" -)
    [ -z "$new" ] || { rm -f -- "$before"; say "round trip works: note $new (fm-inbox.sh ran under the LaunchAgent)"; return 0; }
    n=$((n - 2)); sleep 2
  done
  rm -f -- "$before"; say "no note arrived"; return 1
}

apply() {
  check || die "preflight failed"
  offset=0
  [ ! -f "$log" ] || offset=$(wc -c < "$log" | tr -d ' ')
  stop_loop
  no_pollers || die "a poller still runs; refusing to start a second one"
  if ! FM_TELEGRAM_LABEL=$label sh "$root/deploy/install.sh" --data-dir "$data_dir" --launchd --python "$python"; then
    say "install failed; rolling back"; rollback; exit 1
  fi
  if ! verify "$offset"; then
    say "verification failed; rolling back"; rollback; exit 1
  fi
  say "cutover done. Roll back any time with: $0 rollback"
}

case "${1:-}" in
  check) check ;;
  apply) apply ;;
  rollback) rollback ;;
  stop-loop) stop_loop ;;
  *) echo "usage: $0 check|apply|rollback|stop-loop" >&2; exit 2 ;;
esac
