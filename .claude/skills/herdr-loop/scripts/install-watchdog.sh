#!/bin/sh
# Install, or with --remove take away, the watchdog LaunchAgent for one program.
# Run on the loop host as the user the loops run as:
#
#   install-watchdog.sh <loop root>
#   install-watchdog.sh --remove <loop root>
#
# The agent runs `hl-watchdog tick` every five minutes. It starts disarmed and
# does nothing until `hl-watchdog --root <root> arm`.
#
# Its label is `<prefix>.watchdog.<program>`, the program's name in lower case
# with anything but letters and digits made a dash. The prefix is
# HL_LAUNCHD_PREFIX, `local.herdr-loop` unless it is set; `--remove` must be
# given the same one the agent was installed with.
set -eu

remove=0
if [ "${1:-}" = "--remove" ]; then
  remove=1
  shift
fi
[ $# -eq 1 ] || { echo "usage: install-watchdog.sh [--remove] <loop root>" >&2; exit 64; }
root=$(cd "$1" && pwd -P)
kit="$root/.claude/skills/herdr-loop"
[ -f "$root/program.json" ] || { echo "install-watchdog: $root/program.json does not exist" >&2; exit 1; }
[ -x "$kit/scripts/hl-watchdog" ] || { echo "install-watchdog: $kit/scripts/hl-watchdog is missing" >&2; exit 1; }

slug=$(/usr/bin/python3 -c 'import json,re,sys; print(re.sub(r"[^a-z0-9]+", "-", json.load(open(sys.argv[1]))["program"].lower()).strip("-"))' "$root/program.json")
label="${HL_LAUNCHD_PREFIX:-local.herdr-loop}.watchdog.$slug"
plist="$HOME/Library/LaunchAgents/$label.plist"
domain="gui/$(id -u)"

launchctl bootout "$domain/$label" 2>/dev/null || true
if [ "$remove" -eq 1 ]; then
  rm -f "$plist"
  echo "removed $label"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$kit/scripts/hl-watchdog</string>
    <string>--root</string>
    <string>$root</string>
    <string>tick</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><false/>
  <key>KeepAlive</key><false/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$root/watchdog.out</string>
  <key>StandardErrorPath</key><string>$root/watchdog.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
PLIST
plutil -lint "$plist" >/dev/null
launchctl bootstrap "$domain" "$plist"
echo "installed $label (every 300 s, disarmed until: $kit/scripts/hl-watchdog --root $root arm --note ...)"
