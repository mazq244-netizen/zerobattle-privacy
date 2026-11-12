#!/bin/zsh
# Free market watcher: no Claude runs. Runs the trader skills' own Python on
# your Mac and pops a macOS notification (with a sound) when something happens.
#
#   tools/watch.sh AAPL 356.00          watch AAPL for a 5-min close above 356.00
#   tools/watch.sh                      alerts only (stop / target / flat-by / bell)
#
# Leave the Terminal window open; Ctrl+C stops everything. It keeps the Mac
# awake while it runs and stops by itself at 4:05 PM New York time.
# Only after a notification do you open Claude: "/trader-exit", then size,
# risk and broker. Nothing here ever places, changes or cancels an order.

set -u
cd "${0:A:h}/.." || exit 1
SKILLS=.claude/skills
SYM=${1:-}
LEVEL=${2:-}
mkdir -p trader

notify() {  # title, message
  local t=${1//\"/\'} m=${2//\"/\'}
  osascript -e "display notification \"${m:0:200}\" with title \"${t}\" sound name \"Glass\"" 2>/dev/null
  print -r -- "[$(TZ=America/New_York date +%H:%M) ET] $t: $m"
}

command -v caffeinate >/dev/null && caffeinate -dims -w $$ &   # keep the Mac awake while this runs
killtree() { local c; for c in $(pgrep -P $1 2>/dev/null); do killtree $c; done; kill $1 2>/dev/null; }
cleanup() { trap - EXIT INT TERM; local c; for c in $(pgrep -P $$ 2>/dev/null); do killtree $c; done; exit 0; }
trap cleanup EXIT INT TERM

if [[ -n $SYM ]]; then
  if [[ -z $LEVEL ]]; then print "usage: tools/watch.sh SYMBOL LEVEL"; exit 2; fi
  (
    python3 $SKILLS/trader-entry/entry.py watch "$SYM" --setup level --level "$LEVEL" \
      --until 15:30 > trader/entry-watch.log 2>&1
    rc=$?
    last=$(grep -v '^\s*$' trader/entry-watch.log | tail -1)
    if (( rc == 0 )); then
      notify "TRIGGER $SYM" "$last  Open Claude: /trader-exit"
    else
      notify "$SYM watch ended" "$last"
    fi
  ) &
  print "Watching $SYM for a 5-minute close above $LEVEL (log: trader/entry-watch.log)."
fi

print "Checking alerts every 5 minutes until 4:05 PM ET. Ctrl+C to stop."
while true; do
  hm=$(TZ=America/New_York date +%H%M)
  (( 10#$hm >= 1605 )) && break
  if out=$(python3 $SKILLS/trader-alerts/alerts.py check 2>&1); then
    msg=$(print -r -- "$out" | grep -m1 'ALERT' || print -r -- "$out" | head -1)
    notify "Trader alert" "$msg"
  fi
  sleep 300 & wait $!              # backgrounded so Ctrl+C stops it at once
done
notify "Watcher" "Stopped for the day (after 4:05 PM ET)."
